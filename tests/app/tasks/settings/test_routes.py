# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for the Tasks settings REST API at ``/admin/settings/`` (mounted at ``/api/tasks``)."""

from collections.abc import Iterator
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.encryption import marked_ciphertext
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingClassEnum
from app.core.settings_override.registry import (
    ReloadClassification,
    SECRET_STR_MASK,
)
from app.tasks.config import tasks_settings
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.executors.nomad import NomadExecutor
from app.tasks.execution.nomad_lifecycle import normalize_nomad_config_value
from app.tasks.main import tasks_app
from tests.app.core.settings_override.conftest import (
    ANONYMIZER_SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)
from tests.app.encryption_fixtures import is_stored_ciphertext, stored_plaintext


def _nomad_endpoint_value() -> str:
    """Return the effective Nomad ``endpoint`` as a string."""
    return str(normalize_nomad_config_value(tasks_settings.NOMAD).endpoint)


@pytest.fixture(name="admin_test_client")
def admin_test_client_fixture(
    admin_user: CasdoorUser,
    session: AsyncSession,
    mock_executor: AsyncMock,
) -> Iterator[TestClient]:
    """Yield an admin-authenticated Tasks TestClient bound to the test session."""
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: admin_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


@pytest.fixture(name="non_admin_client")
def non_admin_client_fixture(
    regular_user: CasdoorUser,
    session: AsyncSession,
    mock_executor: AsyncMock,
) -> Iterator[TestClient]:
    """Yield a non-admin Tasks TestClient bound to the test session.

    The router-level gate is overridden so the refusal under test comes from the
    route's own ``IsAdminDep``, not from the gate that precedes it.
    """
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


@pytest.fixture(name="unauthenticated_client")
def unauthenticated_client_fixture(
    session: AsyncSession,
    mock_executor: AsyncMock,
) -> Iterator[TestClient]:
    """Yield an unauthenticated Tasks TestClient bound to the test session."""
    tasks_app.dependency_overrides = {}
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


@pytest.mark.asyncio
class TestTasksSettingsApi:
    """Cover the Tasks sub-app settings router end-to-end."""

    async def test_list_returns_tasks_class(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert the Tasks router exposes ``TasksSettings`` and ``AnonymizerSettings``."""
        response = admin_test_client.get("/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        groups = response.json()["groups"]
        classes = {group["setting_class"] for group in groups}
        assert classes == {
            SettingClassEnum.TASKS_SETTINGS.value,
            SettingClassEnum.ANONYMIZER_SETTINGS.value,
        }

    async def test_get_single_setting(self, admin_test_client: TestClient) -> None:
        """Assert a single Tasks HOT field returns its metadata."""
        response = admin_test_client.get(
            "/admin/settings/TasksSettings/STALENESS_THRESHOLD_SECONDS"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["key"] == "STALENESS_THRESHOLD_SECONDS"
        assert body["reload"] == ReloadClassification.HOT.value

    async def test_get_anonymizer_default_entities(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert ``AnonymizerSettings.DEFAULT_ENTITIES`` is reachable via GET."""
        response = admin_test_client.get(
            "/admin/settings/AnonymizerSettings/DEFAULT_ENTITIES"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["setting_class"] == (
            SettingClassEnum.ANONYMIZER_SETTINGS.value
        )

    async def test_patch_anonymizer_default_entities(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert a valid ``DEFAULT_ENTITIES`` PATCH persists an override row."""
        response = admin_test_client.patch(
            "/admin/settings/AnonymizerSettings",
            json={"DEFAULT_ENTITIES": ["CREDIT_CARD", "EMAIL_ADDRESS"]},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            session, setting_class=ANONYMIZER_SETTINGS_TOKEN
        )
        assert [r.key for r in rows] == ["DEFAULT_ENTITIES"]

    async def test_patch_hot_field(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert PATCHing a Tasks HOT field creates a row in the Tasks DB."""
        new_value = 7200
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": new_value},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert len(rows) == 1
        assert rows[0].value == new_value

    async def test_patch_whole_nomad_rejected(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Reject replacing the whole NESTED_ONLY ``NOMAD`` parent."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD": {"endpoint": "https://nomad-override.example.org"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_patch_multiple_atomic(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert two HOT Tasks fields persist in a single transaction."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={
                "STALENESS_THRESHOLD_SECONDS": 1800,
                "PRE_EXECUTION_CONNECTIVITY_CHECK": "block",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        expected_rows = 2
        assert len(rows) == expected_rows

    async def test_pre_execution_options_are_str_enum_members(
        self, admin_test_client: TestClient
    ) -> None:
        """Expose PreExecutionCheckMode members with string values."""
        response = admin_test_client.get("/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        row = next(
            s
            for g in response.json()["groups"]
            if g["setting_class"] == SettingClassEnum.TASKS_SETTINGS.value
            for s in g["settings"]
            if s["key"] == "PRE_EXECUTION_CONNECTIVITY_CHECK"
        )
        assert row["options"] == [
            {"label": "DISABLED", "value": "disabled"},
            {"label": "WARN", "value": "warn"},
            {"label": "BLOCK", "value": "block"},
        ]

    async def test_patch_inline_refresh(self, admin_test_client: TestClient) -> None:
        """Assert the proxy returns the new value after PATCH without the background refresher."""
        new_value = 99
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": new_value},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            assert new_value == tasks_settings.STALENESS_THRESHOLD_SECONDS
        finally:
            tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_patch_partial_failure_rolls_back(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert one bad key rejects the batch and writes zero rows."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={
                "STALENESS_THRESHOLD_SECONDS": 1800,
                "PRE_EXECUTION_CONNECTIVITY_CHECK": "not-a-valid-mode",
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_patch_not_overridable_field(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a non-HOT Tasks field is rejected as ``not_overridable``."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"UVICORN_PORT": 9999},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert ReloadClassification.NOT_OVERRIDABLE.value in types

    async def test_patch_type_mismatch(self, admin_test_client: TestClient) -> None:
        """Assert an int field rejects a string value with a structured 422."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": "not-a-number"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize("bad_value", [1.5, 0, 366])
    async def test_patch_log_retention_days_invalid_rejected(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
        bad_value: object,
    ) -> None:
        """Reject non-integer or out-of-range LOG_RETENTION_DAYS with 422.

        Matching the lax sibling int settings, coercible values (``"90"``,
        ``90.0``) are accepted; only a fractional float or a value outside the
        ``1..365`` bounds is rejected. A rejected key writes zero rows.
        """
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"LOG_RETENTION_DAYS": bad_value},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        locs = [tuple(entry["loc"]) for entry in response.json()["detail"]]
        assert any(loc[:2] == ("body", "LOG_RETENTION_DAYS") for loc in locs)
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_patch_log_retention_days_valid_accepted(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert a valid in-range LOG_RETENTION_DAYS PATCH persists and reflects on the proxy."""
        new_value = 90
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"LOG_RETENTION_DAYS": new_value},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            assert new_value == tasks_settings.LOG_RETENTION_DAYS
            rows = await SettingsOverrideManager.list(
                session, setting_class=TASKS_SETTINGS_TOKEN
            )
            assert len(rows) == 1
            assert rows[0].value == new_value
        finally:
            tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_delete_idempotent(self, admin_test_client: TestClient) -> None:
        """Assert deleting a HOT field with no row still succeeds with 204."""
        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/STALENESS_THRESHOLD_SECONDS"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    async def test_unauthenticated_returns_401(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Assert an unauthenticated request to the settings endpoint returns 401."""
        response = unauthenticated_client.get("/admin/settings/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_non_admin_returns_403(self, non_admin_client: TestClient) -> None:
        """Assert a non-admin user is rejected with 403."""
        response = non_admin_client.get("/admin/settings/")
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
class TestTasksSettingsNestedOverrides:
    """Cover ``__``-delimited nested overrides on ``TasksSettings`` parents."""

    @pytest.fixture(autouse=True)
    def _reset_proxy_snapshot(self) -> Iterator[None]:
        """Clear the global proxy snapshot after each nested test."""
        yield
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_patch_per_leaf_nomad_round_trip(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Persist a per-leaf ``NOMAD__TIMEOUT`` override and snapshot an executor."""
        override_timeout = 30
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__TIMEOUT": override_timeout},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body[0]["key"] == "NOMAD__timeout"
        assert body[0]["value"] == override_timeout
        rows = await SettingsOverrideManager.list(
            session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__timeout",
        )
        assert len(rows) == 1
        assert rows[0].value == override_timeout
        snapshot = tasks_settings.get_snapshot()  # ty: ignore[unresolved-attribute]
        assert isinstance(snapshot["NOMAD"], NomadExecutor)
        assert snapshot["NOMAD"].timeout == override_timeout
        assert tasks_settings.NOMAD.timeout == override_timeout

    async def test_patch_nested_security_header_bool(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a nested case-insensitive boolean leaf override applies."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS__X_FRAME_OPTIONS_DENY": False},
        )
        assert response.status_code == status.HTTP_200_OK
        assert tasks_settings.SECURITY_HEADERS.x_frame_options_deny is False

    async def test_patch_multi_level_security_header(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a multi-level override instantiates the nested intermediate model."""
        max_age = 31536000
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE": max_age},
        )
        assert response.status_code == status.HTTP_200_OK
        sts = tasks_settings.SECURITY_HEADERS.strict_transport_security
        assert sts is not None
        assert sts.max_age == max_age

    async def test_patch_whole_parent_security_headers_rejected(
        self, admin_test_client: TestClient
    ) -> None:
        """Reject replacing the whole NESTED_ONLY ``SECURITY_HEADERS`` parent."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS": {"x_frame_options_deny": False}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types

    async def test_delete_nested_nomad_timeout(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert deleting a nested override removes its row and returns 204."""
        patched = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__TIMEOUT": 30},
        )
        assert patched.status_code == status.HTTP_200_OK
        assert await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )

        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/NOMAD__TIMEOUT"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_delete_whole_parent_nomad_rejected(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert DELETE on the whole NESTED_ONLY ``NOMAD`` parent returns 422."""
        response = admin_test_client.delete("/admin/settings/TasksSettings/NOMAD")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types

    async def test_delete_whole_parent_security_headers_rejected(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert DELETE on the whole NESTED_ONLY ``SECURITY_HEADERS`` parent returns 422."""
        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/SECURITY_HEADERS"
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types

    async def test_list_marks_overridden_security_header_leaf(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a nested override marks only its canonical leaf ``has_override`` in LIST."""
        admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS__X_FRAME_OPTIONS_DENY": False},
        )
        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]
        by_key = {s["key"]: s for s in settings}
        assert "SECURITY_HEADERS" not in by_key
        assert by_key["SECURITY_HEADERS__x_frame_options_deny"]["has_override"] is True
        sibling = by_key["SECURITY_HEADERS__x_content_type_options_nosniff"]
        assert sibling["has_override"] is False

    async def test_list_expands_security_headers_two_level_leaf(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert LIST surfaces the two-level HSTS leaf with its canonical ``key_path``.

        The intermediate ``strict_transport_security`` defaults to ``None``, so
        the leaf's ``value`` resolves to ``None`` without raising.
        """
        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]
        by_key = {s["key"]: s for s in settings}
        leaf = by_key["SECURITY_HEADERS__strict_transport_security__max_age"]
        assert leaf["key_path"] == [
            "SECURITY_HEADERS",
            "strict_transport_security",
            "max_age",
        ]
        assert "__".join(leaf["key_path"]) == leaf["key"]
        assert leaf["value"] is None

    async def test_list_marks_security_header_leaves_advanced(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert every expanded ``SECURITY_HEADERS`` leaf inherits ``is_advanced`` in LIST.

        Only the ``SECURITY_HEADERS`` parent is marked advanced; the
        real LIST projection must propagate the flag to every leaf — including the
        two-level HSTS ``max_age`` leaf — while a basic Tasks sibling stays False.
        This exercises the live ``TasksSettings`` projection, not the PMM Extensions proxy's
        mocked upstream payload.
        """
        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]
        by_key = {s["key"]: s for s in settings}
        header_leaves = [k for k in by_key if k.startswith("SECURITY_HEADERS__")]
        assert header_leaves  # the parent expands into leaves, not a single entry
        assert all(by_key[k]["is_advanced"] is True for k in header_leaves)
        assert (
            by_key["SECURITY_HEADERS__strict_transport_security__max_age"][
                "is_advanced"
            ]
            is True
        )
        # A Tasks sibling left basic stays False (STALENESS_THRESHOLD_SECONDS is
        # now promoted to advanced, so it is no longer a valid negative).
        assert by_key["PRE_EXECUTION_CONNECTIVITY_CHECK"]["is_advanced"] is False

    async def test_list_marks_nomad_secondary_leaves_advanced(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert the NOMAD TLS/tuning leaves are advanced, but not ``endpoint``.

        The advanced flag is applied per leaf (the ``NOMAD`` parent is not marked),
        so the connection ``endpoint`` leaf must stay basic while the TLS and tuning
        leaves surface ``is_advanced`` in the LIST projection.
        """
        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]
        by_key = {s["key"]: s for s in settings}
        advanced_leaves = [
            "NOMAD__verify_ssl",
            "NOMAD__ssl_cafile",
            "NOMAD__ssl_keyfile",
            "NOMAD__ssl_certfile",
            "NOMAD__secure",
            "NOMAD__timeout",
            "NOMAD__minify_payload",
            "NOMAD__log_socket_read_timeout",
            "NOMAD__cert_expiry_warn_days",
            "NOMAD__auth_scheme",
        ]
        for key in advanced_leaves:
            assert by_key[key]["is_advanced"] is True, key
        assert by_key["NOMAD__endpoint"]["is_advanced"] is False
        assert by_key["NOMAD__api_key"]["is_advanced"] is False

    async def test_get_multi_level_nested_before_override_returns_200(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert GET on a multi-level key whose intermediate is ``None`` returns 200, not 500."""
        # ``STRICT_TRANSPORT_SECURITY`` defaults to ``None``; reading a leaf
        # under it must not raise.
        response = admin_test_client.get(
            "/admin/settings/TasksSettings"
            "/SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["value"] is None

    async def test_case_insensitive_keys_collapse_to_one_row(
        self,
        admin_test_client: TestClient,
        session: AsyncSession,
    ) -> None:
        """Assert mixed-case spellings of the same nested key map to a single override row."""
        patched = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"security_headers__x_frame_options_deny": False},
        )
        assert patched.status_code == status.HTTP_200_OK
        assert (
            len(
                await SettingsOverrideManager.list(
                    session, setting_class=TASKS_SETTINGS_TOKEN
                )
            )
            == 1
        )

        # An uppercase DELETE removes the row created by the lowercase PATCH.
        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/SECURITY_HEADERS__X_FRAME_OPTIONS_DENY"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_delete_nested_under_non_overridable_parent_rejected(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert DELETE of a nested key under a non-nested-overridable parent returns 422.

        ``DATABASE`` is not promoted to ``nested_overridable_field``, so
        ``DATABASE__HOST`` must be rejected with the same ``not_overridable`` 422
        the PATCH path returns -- not silently accepted with 204.
        """
        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/DATABASE__HOST"
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types

    async def test_patch_nested_under_non_overridable_parent_rejected(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert PATCH of a nested key under a non-nested-overridable parent returns 422.

        Documents the DELETE/PATCH parity asserted above: both reject
        ``DATABASE__HOST`` with ``not_overridable``.
        """
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"DATABASE__HOST": "db.example"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "not_overridable" in types

    async def test_get_intermediate_parent_reports_has_override(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a multi-level override marks the intermediate sub-model ``has_override`` too.

        Patching ``SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE`` must make
        a GET of the intermediate ``SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY``
        report ``has_override=True`` -- not only the top-level ``SECURITY_HEADERS``
        parent.
        """
        admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE": 31536000},
        )
        response = admin_test_client.get(
            "/admin/settings/TasksSettings/SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["has_override"] is True

    async def test_detail_two_level_leaf_carries_key_path(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a two-level DETAIL response carries its canonical lowercase ``key_path``."""
        response = admin_test_client.get(
            "/admin/settings/TasksSettings"
            "/SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["key_path"] == [
            "SECURITY_HEADERS",
            "strict_transport_security",
            "max_age",
        ]
        assert "__".join(body["key_path"]) == body["key"]

    async def test_patch_security_header_echoes_key_path(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert a nested PATCH echoes the leaf's canonical lowercase ``key_path`` chain."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"SECURITY_HEADERS__X_FRAME_OPTIONS_DENY": False},
        )
        assert response.status_code == status.HTTP_200_OK
        echoed = response.json()[0]
        assert echoed["key_path"] == ["SECURITY_HEADERS", "x_frame_options_deny"]
        assert "__".join(echoed["key_path"]) == echoed["key"]

    async def test_list_resolves_nomad_leaf_values_under_per_leaf_override(
        self, admin_test_client: TestClient
    ) -> None:
        """Resolve real leaf values in LIST for a per-leaf ``NOMAD`` override, not null."""
        try:
            admin_test_client.patch(
                "/admin/settings/TasksSettings",
                json={"NOMAD__ENDPOINT": "https://nomad-override.example.org"},
            )
            settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
                "settings"
            ]
            by_key = {s["key"]: s for s in settings}
            assert "nomad-override" in by_key["NOMAD__endpoint"]["value"]
            assert by_key["NOMAD__timeout"]["value"] is not None
            assert (
                by_key["NOMAD__check_cert_expiry_interval__every"]["value"] is not None
            )
        finally:
            tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


def _nomad_snapshot_with_endpoint(full_url: str) -> dict[str, object]:
    """Build a snapshot carrying only a ``NOMAD.endpoint`` override."""
    return {"NOMAD": {"endpoint": full_url}}


@pytest.mark.asyncio
class TestTasksSettingsCredentialUrlRedaction:
    """Verify that LIST and DETAIL redact ``NOMAD__endpoint`` embedded URL passwords."""

    _FULL_URL = "http://nomad-user:nomad-secret@nomad.internal:4646"

    @pytest.fixture(autouse=True)
    def _reset_snapshot(self) -> Iterator[None]:
        """Clear override snapshots after each test."""
        yield
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_list_redacts_nomad_endpoint_leaf(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert ``GET /settings/`` masks ``NOMAD__endpoint`` password components."""
        tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            _nomad_snapshot_with_endpoint(self._FULL_URL)
        )
        response = admin_test_client.get("/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        settings = response.json()["groups"][0]["settings"]
        by_key = {entry["key"]: entry for entry in settings}
        value = by_key["NOMAD__endpoint"]["value"]
        assert "nomad-secret" not in value
        assert "****" in value
        assert "nomad-user" in value

    async def test_detail_redacts_nomad_endpoint_leaf(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert ``GET /settings/{class}/{key}`` masks ``NOMAD__endpoint`` passwords."""
        tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            _nomad_snapshot_with_endpoint(self._FULL_URL)
        )
        response = admin_test_client.get(
            "/admin/settings/TasksSettings/NOMAD__endpoint"
        )
        assert response.status_code == status.HTTP_200_OK
        value = response.json()["value"]
        assert "nomad-secret" not in value
        assert "****" in value
        assert "nomad-user" in value


@pytest.mark.asyncio
class TestTasksSettingsCredentialUrlWriteback:
    """Verify PATCH does not persist redacted URL display values over stored credentials."""

    async def test_patch_redacted_nomad_leaf_preserves_password(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert saving an unchanged redacted ``NOMAD__endpoint`` keeps the real password."""
        full_url = "http://nomad-user:nomad-secret@nomad.internal:4646"
        redacted_url = "http://nomad-user:****@nomad.internal:4646"
        snapshot = _nomad_snapshot_with_endpoint(full_url)
        try:
            tasks_settings._set_snapshot(snapshot)  # ty: ignore[unresolved-attribute]
            response = admin_test_client.patch(
                "/admin/settings/TasksSettings",
                json={"NOMAD__endpoint": redacted_url},
            )
            assert response.status_code == status.HTTP_200_OK
            endpoint = _nomad_endpoint_value()
            assert "nomad-secret" in endpoint
            assert "****" not in endpoint
        finally:
            tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_patch_redacted_whole_nomad_preserves_password(
        self, admin_test_client: TestClient
    ) -> None:
        """Reject a whole ``NOMAD`` PATCH while preserving the stored endpoint password."""
        full_url = "http://nomad-user:nomad-secret@nomad.internal:4646"
        redacted_url = "http://nomad-user:****@nomad.internal:4646"
        snapshot = _nomad_snapshot_with_endpoint(full_url)
        try:
            tasks_settings._set_snapshot(snapshot)  # ty: ignore[unresolved-attribute]
            response = admin_test_client.patch(
                "/admin/settings/TasksSettings",
                json={"NOMAD": {"endpoint": redacted_url}},
            )
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
            endpoint = _nomad_endpoint_value()
            assert "nomad-secret" in endpoint
            assert "****" not in endpoint
        finally:
            tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
class TestTasksSettingsCredentialUrlAtRest:
    """Verify the Tasks write path encrypts ``NOMAD__endpoint``'s embedded password.

    The Tasks service has its own settings router, so PMM Extensions side coverage proves
    nothing about this wiring. ``NomadExecutor.endpoint`` is also the inherited
    non-``Optional`` case whose ``Annotated`` Pydantic hoists onto ``FieldInfo``
    and which the route coerces to a :class:`~pydantic.HttpUrl` — the two
    properties that make a classifier reading ``.annotation``, or a leaf branch
    guarded on ``isinstance(value, str)``, silently skip it.
    """

    _FULL_URL = "http://nomad-user:nomad-secret@nomad.internal:4646/v1"
    _PASSWORD = "nomad-secret"
    _PORT = 4646

    @pytest.fixture(autouse=True)
    def _reset_snapshot(self) -> Iterator[None]:
        """Clear override snapshots after each test."""
        yield
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_patch_encrypts_the_password_and_keeps_the_endpoint_readable(
        self, admin_test_client: TestClient, session: AsyncSession
    ) -> None:
        """Store the password as ciphertext with scheme, user, host and port intact."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__ENDPOINT": self._FULL_URL},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN, key="NOMAD__endpoint"
        )
        assert len(rows) == 1, "the PATCH must have persisted exactly one row"
        parsed = urlparse(rows[0].value)
        assert is_stored_ciphertext(parsed.password)
        assert stored_plaintext(parsed.password) == self._PASSWORD
        assert parsed.username == "nomad-user"
        assert parsed.hostname == "nomad.internal"
        assert parsed.port == self._PORT

    async def test_the_effective_endpoint_still_resolves_to_the_real_credential(
        self, admin_test_client: TestClient
    ) -> None:
        """Restore the password on the read path so the executor gets a usable URL.

        Encrypting at rest is only safe if the snapshot the executor reads is
        the plaintext one; a decrypt that failed here would leave the rebuilt
        executor authenticating with a Fernet token.
        """
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__ENDPOINT": self._FULL_URL},
        )
        assert response.status_code == status.HTTP_200_OK

        password = urlparse(_nomad_endpoint_value()).password
        assert password == self._PASSWORD
        assert not is_stored_ciphertext(password)


@pytest.mark.asyncio
class TestTasksSettingsNomadApiKey:
    """Cover the ``NOMAD__api_key`` leaf across LIST, PATCH and DELETE.

    A model-level test can pass while a live PATCH is refused, stores the mask
    instead of the secret, or never reaches the rebuilt executor, so each
    surface is exercised through the API rather than through the model alone.
    """

    _API_KEY = "glsa_settings_api_key"

    @pytest.fixture(autouse=True)
    def _reset_snapshot(self) -> Iterator[None]:
        """Clear override snapshots after each test."""
        yield
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_list_reports_the_key_as_a_masked_secret(
        self, admin_test_client: TestClient
    ) -> None:
        """Assert LIST flags the leaf secret and never renders the stored value."""
        assert (
            admin_test_client.patch(
                "/admin/settings/TasksSettings",
                json={"NOMAD__API_KEY": self._API_KEY},
            ).status_code
            == status.HTTP_200_OK
        )
        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]
        entry = {item["key"]: item for item in settings}["NOMAD__api_key"]
        assert entry["is_secret"] is True
        assert entry["value"] == SECRET_STR_MASK

    async def test_patch_encrypts_the_row_and_applies_the_key(
        self, admin_test_client: TestClient, session: AsyncSession
    ) -> None:
        """Assert PATCH stores ciphertext and the rebuilt executor carries the secret."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__API_KEY": self._API_KEY},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["value"] == SECRET_STR_MASK

        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN, key="NOMAD__api_key"
        )
        assert is_stored_ciphertext(rows[0].value)
        assert marked_ciphertext(rows[0].value) is not None
        assert stored_plaintext(rows[0].value) == self._API_KEY

        executor = normalize_nomad_config_value(tasks_settings.NOMAD)
        assert executor.api_key is not None
        assert executor.api_key.get_secret_value() == self._API_KEY
        assert executor.headers["Authorization"] == f"Bearer {self._API_KEY}"

    async def test_delete_reverts_the_effective_key(
        self, admin_test_client: TestClient, session: AsyncSession
    ) -> None:
        """Assert DELETE drops the row and the effective executor loses the header."""
        patched = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__API_KEY": self._API_KEY},
        )
        assert patched.status_code == status.HTTP_200_OK
        configured = normalize_nomad_config_value(tasks_settings.NOMAD)
        assert configured.headers["Authorization"] == f"Bearer {self._API_KEY}"

        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/NOMAD__API_KEY"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        rows = await SettingsOverrideManager.list(
            session, setting_class=TASKS_SETTINGS_TOKEN, key="NOMAD__api_key"
        )
        assert rows == []
        executor = normalize_nomad_config_value(tasks_settings.NOMAD)
        assert executor.api_key is None
        assert executor.headers == {}


@pytest.mark.asyncio
class TestTasksSettingsInlineRebind:
    """Fire the registered rebind callbacks on an inline PATCH/DELETE.

    The background refresher only fires a rebind callback when *it* observes a
    snapshot diff (the snapshot before its ``publish_snapshot`` vs the one after).
    But the PATCH/DELETE handlers publish the new snapshot inline, so by the next
    refresh cycle the proxy already holds the new value and the cycle's diff is
    empty -- the rebind never fires, and the live ``NomadExecutor`` held by
    ``NomadLifecycle`` keeps serving the old config until restart. The handler
    must therefore fire the registered callbacks for the keys it just changed.
    """

    @pytest.fixture(name="nomad_callback_spy")
    def nomad_callback_spy_fixture(self) -> Iterator[AsyncMock]:
        """Register a spy as the ``(TASKS_SETTINGS, NOMAD)`` callback on app state.

        Reaches the handler via ``request.app.state.override_callbacks`` (the same
        registry the lifespan publishes), starting from a clean proxy snapshot and
        restoring the prior registry on teardown.
        """
        spy = AsyncMock()
        original = getattr(tasks_app.state, "override_callbacks", None)
        tasks_app.state.override_callbacks = {
            (SettingClassEnum.TASKS_SETTINGS, "NOMAD"): spy,
        }
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]
        yield spy
        tasks_app.state.override_callbacks = original
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    async def test_patch_nomad_fires_rebind_callback(
        self, admin_test_client: TestClient, nomad_callback_spy: AsyncMock
    ) -> None:
        """Fire the parent rebind callback inline when PATCHing a ``NOMAD`` leaf."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__ENDPOINT": "https://nomad-override.example.org"},
        )
        assert response.status_code == status.HTTP_200_OK
        nomad_callback_spy.assert_awaited_once()

    async def test_patch_unrelated_key_does_not_fire_nomad_callback(
        self, admin_test_client: TestClient, nomad_callback_spy: AsyncMock
    ) -> None:
        """Assert PATCHing a non-``NOMAD`` key leaves the ``NOMAD`` rebind callback untouched."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": 4242},
        )
        assert response.status_code == status.HTTP_200_OK
        nomad_callback_spy.assert_not_awaited()

    async def test_delete_nomad_override_fires_rebind_callback(
        self, admin_test_client: TestClient, nomad_callback_spy: AsyncMock
    ) -> None:
        """Fire the parent rebind callback inline when reverting a ``NOMAD`` leaf override."""
        admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"NOMAD__ENDPOINT": "https://nomad-override.example.org"},
        )
        nomad_callback_spy.reset_mock()
        response = admin_test_client.delete(
            "/admin/settings/TasksSettings/NOMAD__ENDPOINT"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        nomad_callback_spy.assert_awaited_once()


@pytest.mark.asyncio
class TestTasksSettingsProvenance:
    """Cover ``updated_at`` / ``updated_by`` on the Tasks router's own dep wiring."""

    async def test_patch_stamps_the_tasks_admin(
        self, admin_test_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Record the Tasks admin principal the router's own ``actor_dep`` resolves."""
        response = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": 4242},
        )

        assert response.status_code == status.HTTP_200_OK
        entry = response.json()[0]
        assert entry["updated_by"] == admin_user.username
        assert entry["updated_at"] is not None

    async def test_detail_reports_the_stamp_the_patch_returned(
        self, admin_test_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Serve the same stamp from DETAIL that the PATCH response carried."""
        patched = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": 4242},
        )

        response = admin_test_client.get(
            "/admin/settings/TasksSettings/STALENESS_THRESHOLD_SECONDS"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["updated_by"] == admin_user.username
        assert body["updated_at"] == patched.json()[0]["updated_at"]

    async def test_list_reports_the_stamp_the_patch_returned(
        self, admin_test_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Serve the same stamp from LIST that the PATCH response carried."""
        patched = admin_test_client.patch(
            "/admin/settings/TasksSettings",
            json={"STALENESS_THRESHOLD_SECONDS": 4242},
        )

        settings = admin_test_client.get("/admin/settings/").json()["groups"][0][
            "settings"
        ]

        entry = next(s for s in settings if s["key"] == "STALENESS_THRESHOLD_SECONDS")
        assert entry["updated_by"] == admin_user.username
        assert entry["updated_at"] == patched.json()[0]["updated_at"]

    async def test_key_without_an_override_reports_no_provenance(
        self, admin_test_client: TestClient
    ) -> None:
        """Report both fields as ``null`` for a key carrying no override row."""
        response = admin_test_client.get(
            "/admin/settings/TasksSettings/STALENESS_THRESHOLD_SECONDS"
        )

        body = response.json()
        assert body["has_override"] is False
        assert body["updated_at"] is None
        assert body["updated_by"] is None
