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

"""Tests for the PMM Extensions settings REST API at ``/api/extensions/admin/settings``."""

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from string import Template
from typing import Annotated, Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.alerts.config import alert_settings
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.config import PMMSettings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.encryption import marked_ciphertext
from app.core.requests import RemoteAPI
from app.core.settings_override.api import build_settings_router
from app.core.settings_override.api import routes as settings_routes
from app.core.settings_override.cache import build_snapshot
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingClassEnum
from app.core.settings_override.registry import ReloadClassification, SECRET_STR_MASK
from app.core.utils import json_serializer
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.extensions.api.routes.settings import EXTENSIONS_ADMIN_SETTINGS_CLASSES
from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.framework.registry import (
    collect_app_owned_settings_classes,
    resolve_app_settings_metadata,
)
from app.extensions.bundle_upload.plan import DeliveryPlan
from app.extensions.config import extensions_settings, ExtensionsSettings
from app.extensions.deps import (
    get_current_user,
    get_session,
    get_tasks_api,
    require_bearer_for_unsafe_methods,
    TaskAPI,
)
from app.extensions.main import extensions_app, extensions_overrides_lifespan
from app.extensions.models import AppLifecycleEnum, AppState
from app.extensions.snippets.config import (
    SnippetFilter,
    SnippetFilterType,
    snippets_settings,
)
from tests.app.core.settings_override.conftest import (
    ALERT_SETTINGS_TOKEN,
    EXTENSIONS_SETTINGS_TOKEN,
    insert_override_row,
    PMM_API_KEY,
    ROUTING_KEY,
    SETTINGS_TOKEN,
    SNIPPETS_SETTINGS_TOKEN,
)
from tests.app.db_schema import apply_schema
from tests.app.encryption_fixtures import is_stored_ciphertext, stored_plaintext
from tests.app.extensions.conftest import REDUCED_ACTIVATION

REDUCED_SETTINGS_PREFIX = "/settings"

_DELIVERY_PLAN_PAYLOAD: dict[str, Any] = {
    "endpoint": "https://snow.example.com/",
    "secrets": {"api_key": "plan-secret"},
    "upload": {
        "path": "attachment/upload",
        "headers": {"x-sn-apikey": {"source": "secret", "name": "api_key"}},
        "fields": {"table_name": {"source": "literal", "value": "case"}},
    },
}

_DELIVERY_SKELETON_PAYLOAD: dict[str, Any] = {
    "endpoint": "https://snow.example.com/",
    "secrets": {"sn_api_key": "", "client_token": ""},
    "upload": {
        "path": "attachment/upload",
        "headers": {"x-sn-apikey": {"source": "secret", "name": "sn_api_key"}},
        "fields": {"client_token": {"source": "secret", "name": "client_token"}},
    },
}

_DELIVERY_INPUTS_KEY = "DIAGNOSTICS_DELIVERY_INPUTS"
_DELIVERY_INPUTS_SECRETS = {"sn_api_key": "key-value", "client_token": "token-value"}

#: The base64 prefix every Fernet token carries, for asserting none reach a response.
_FERNET_TOKEN_PREFIX = "gAAAAA"


def _decrypted_secrets(stored: dict[str, str]) -> dict[str, str]:
    """Return the plaintext behind each value of a persisted ``secrets`` mapping.

    :param stored: The ``secrets`` mapping as the override row holds it.
    :return: The same secret names mapped to their decrypted values.
    """
    return {name: stored_plaintext(value) for name, value in stored.items()}


def _renamed_skeleton() -> dict[str, Any]:
    """Return the baked plan an upgrade ships with ``client_token`` renamed.

    :return: The skeleton payload declaring ``case_token`` in its place.
    """
    return {
        **_DELIVERY_SKELETON_PAYLOAD,
        "secrets": {"sn_api_key": "", "case_token": ""},
        "upload": {
            **_DELIVERY_SKELETON_PAYLOAD["upload"],
            "fields": {"case_token": {"source": "secret", "name": "case_token"}},
        },
    }


def _mock_tasks_api() -> AsyncMock:
    """Return an ``AsyncMock`` Tasks API client serving an empty TasksSettings group.

    The PMM Extensions settings LIST proxies the ``TasksSettings`` group from the Tasks
    sub-app, so every authenticated LIST needs ``get_tasks_api`` stubbed.
    An empty group keeps the local-class assertions in this module
    unaffected. Proxy dispatch / aggregation behaviour is covered in
    ``test_settings_proxy.py``.
    """
    mock = AsyncMock(spec=RemoteAPI)
    mock.get.return_value = {
        "groups": [{"setting_class": "TasksSettings", "settings": []}]
    }
    return mock


@pytest_asyncio.fixture(name="override_session")
async def override_session_fixture() -> AsyncIterator[AsyncSession]:
    """Provide an in-memory SQLite PMM Extensions session pre-loaded with the override table."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture(name="api_admin_client")
def api_admin_client_fixture(
    admin_user: CasdoorUser, override_session: AsyncSession
) -> Iterator[TestClient]:
    """Yield an admin-authenticated PMM Extensions TestClient with the in-memory PMM Extensions session."""
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_tasks_api] = lambda: _mock_tasks_api()
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_non_admin_client")
def api_non_admin_client_fixture(
    regular_user: CasdoorUser, override_session: AsyncSession
) -> Iterator[TestClient]:
    """Yield a non-admin PMM Extensions TestClient with the in-memory PMM Extensions session."""
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_tasks_api] = lambda: _mock_tasks_api()
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_admin_cookie_client")
def api_admin_cookie_client_fixture(
    admin_user: CasdoorUser, override_session: AsyncSession
) -> Iterator[TestClient]:
    """Yield an admin authenticated by cookie session (no Bearer header).

    The Bearer guard runs as in production; mutations should reject the
    client with 401 while reads succeed.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[get_tasks_api] = lambda: _mock_tasks_api()
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_unauthenticated_client")
def api_unauthenticated_client_fixture(
    override_session: AsyncSession,
) -> Iterator[TestClient]:
    """Yield an unauthenticated PMM Extensions TestClient — settings calls should 401."""
    extensions_app.dependency_overrides = {}
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


def _find_group(payload: dict[str, Any], setting_class: str) -> dict[str, Any]:
    """Locate one settings-class group in the LIST response payload."""
    for group in payload["groups"]:
        if group["setting_class"] == setting_class:
            return group
    raise AssertionError(f"group {setting_class!r} not in payload")


@pytest.fixture(name="delivery_skeleton")
def delivery_skeleton_fixture(mocker) -> Iterator[None]:
    """Bake a delivery plan declaring two secrets, both left empty.

    Clears the proxy snapshot on teardown, since the PATCH handler refreshes it
    inline and a stored inputs row would otherwise leak into sibling tests.
    """
    mocker.patch.object(
        extensions_settings,
        "DIAGNOSTICS_DELIVERY",
        DeliveryPlan(**_DELIVERY_SKELETON_PAYLOAD),
    )
    yield
    extensions_settings._set_snapshot({})


def _find_setting(
    payload: dict[str, Any], setting_class: str, key: str
) -> dict[str, Any]:
    """Locate one setting entry in the LIST response payload."""
    for group in payload["groups"]:
        if group["setting_class"] == setting_class:
            for entry in group["settings"]:
                if entry["key"] == key:
                    return entry
    raise AssertionError(f"setting {setting_class}/{key} not in payload")


@pytest.fixture(name="reduced_activation_client")
def reduced_activation_client_fixture(
    override_session: AsyncSession,
) -> Iterator[TestClient]:
    """Return a settings router built as if the alerts app were never activated.

    Reloading ``app.extensions.main`` cannot reach this surface: ``settings.py`` captures
    the app-owned classes and builds ``router`` at module import, and
    ``app/extensions/api/router.py`` imports that built object once. Building a fresh
    router over PMM Extensions' real core list plus a genuinely reduced app-owned
    collection is what exercises the composition.
    """
    app = FastAPI()

    async def get_reduced_session() -> AsyncSession:
        return override_session

    router = build_settings_router(
        classes=EXTENSIONS_ADMIN_SETTINGS_CLASSES,
        session_dep=Annotated[AsyncSession, Depends(get_reduced_session)],
        admin_dep=Depends(lambda: None),
        actor_dep=Annotated[str, Depends(lambda: "test-admin")],
        remote_classes=[(SettingClassEnum.TASKS_SETTINGS, "/admin/settings")],
        remote_api_dep=TaskAPI,
        app_owned_classes=collect_app_owned_settings_classes(REDUCED_ACTIVATION),
        resolve_app_metadata=resolve_app_settings_metadata,
    )
    app.include_router(router, prefix=REDUCED_SETTINGS_PREFIX)
    app.dependency_overrides[get_tasks_api] = _mock_tasks_api
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.asyncio
class TestReducedActivationSettings:
    """Serve the settings API as the PMM-embedded image wires it."""

    async def test_alert_settings_still_served_as_core_group(
        self, reduced_activation_client: TestClient
    ) -> None:
        """Serve the core ``AlertSettings`` group with the alerts app deactivated."""
        response = reduced_activation_client.get(f"{REDUCED_SETTINGS_PREFIX}/")
        assert response.status_code == status.HTTP_200_OK
        alert_group = _find_group(
            response.json(),
            SettingClassEnum.ALERT_SETTINGS.value,
        )
        assert alert_group["is_app_owned"] is False
        assert alert_group["app_id"] is None
        assert alert_group["settings"]

    async def test_alerts_settings_not_wired_at_all(
        self, reduced_activation_client: TestClient
    ) -> None:
        """Omit ``AlertsSettings`` entirely when the alerts app is deactivated."""
        response = reduced_activation_client.get(f"{REDUCED_SETTINGS_PREFIX}/")
        assert response.status_code == status.HTTP_200_OK
        groups = {group["setting_class"] for group in response.json()["groups"]}
        assert "AlertsSettings" not in groups
        assert SettingClassEnum.ALERT_SETTINGS.value in groups

    async def test_health_report_settings_not_wired_at_all(
        self, reduced_activation_client: TestClient
    ) -> None:
        """Omit ``HealthReportSettings`` entirely when the report app is deactivated."""
        response = reduced_activation_client.get(f"{REDUCED_SETTINGS_PREFIX}/")
        assert response.status_code == status.HTTP_200_OK
        groups = {group["setting_class"] for group in response.json()["groups"]}
        assert "HealthReportSettings" not in groups

    async def test_patch_on_deactivated_class_is_not_found(
        self, reduced_activation_client: TestClient
    ) -> None:
        """Reject a PATCH against the deactivated ``AlertsSettings`` class."""
        response = reduced_activation_client.patch(
            f"{REDUCED_SETTINGS_PREFIX}/{'AlertsSettings'}",
            json={"ALERT_FOLDER_NAME": "Nope"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_on_deactivated_class_is_not_found(
        self, reduced_activation_client: TestClient
    ) -> None:
        """Reject a DELETE against the deactivated ``AlertsSettings`` class."""
        response = reduced_activation_client.delete(
            f"{REDUCED_SETTINGS_PREFIX}/{'AlertsSettings'}/ALERT_FOLDER_NAME",
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
class TestExtensionsSettingsList:
    """Cover ``GET /api/extensions/admin/settings/``."""

    async def test_returns_local_proxied_and_app_owned_groups(
        self, api_admin_client: TestClient
    ) -> None:
        """Return core, proxied TasksSettings, and app-owned groups.

        PMM Extensions serves its own classes locally (including ``AlertSettings``),
        proxies ``TasksSettings`` from the Tasks sub-app, and appends
        app-owned classes such as ``AlertsSettings`` and ``OmInventorySettings``.

        An app-owned class appears here *and* on its own app's endpoint. That is
        deliberate rather than duplication: this router is admin-gated, and an app
        whose configuration a non-admin service principal must reach serves its own
        (``om_inventory`` does, for PMM). Both read and write the same override
        rows through the same manager, so they cannot disagree.
        """
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        groups = {group["setting_class"] for group in payload["groups"]}
        assert groups == {
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            SettingClassEnum.SNIPPETS_SETTINGS.value,
            "AlertsSettings",
            "HealthReportSettings",
            "InventoryAppSettings",
            SettingClassEnum.SETTINGS.value,
            SettingClassEnum.TASKS_SETTINGS.value,
            SettingClassEnum.ALERT_SETTINGS.value,
            "OmInventorySettings",
        }

    async def test_all_sealed_nested_parent_is_listed_whole(
        self, api_admin_client: TestClient
    ) -> None:
        """List the inputs object itself, not the leaves no PATCH can target."""
        payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        keys = {
            entry["key"]
            for group in payload["groups"]
            if group["setting_class"] == SettingClassEnum.EXTENSIONS_SETTINGS.value
            for entry in group["settings"]
        }

        assert _DELIVERY_INPUTS_KEY in keys
        assert not [key for key in keys if key.startswith(f"{_DELIVERY_INPUTS_KEY}__")]

    async def test_core_groups_are_not_app_owned(
        self, api_admin_client: TestClient
    ) -> None:
        """Leave core and proxied groups free of app-ownership metadata."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        core_and_remote = {
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            SettingClassEnum.SNIPPETS_SETTINGS.value,
            SettingClassEnum.ALERT_SETTINGS.value,
            SettingClassEnum.TASKS_SETTINGS.value,
        }
        for group in response.json()["groups"]:
            if group["setting_class"] in core_and_remote:
                assert group["is_app_owned"] is False
                assert group["app_id"] is None
                assert group["app_display_name"] is None
                assert group["app_enabled"] is None

    async def test_alerts_settings_group_carries_app_metadata(
        self, api_admin_client: TestClient
    ) -> None:
        """Tag ``AlertsSettings`` as owned by the alerts app when enabled."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        alerts_group = _find_group(
            response.json(),
            "AlertsSettings",
        )
        assert alerts_group["is_app_owned"] is True
        assert alerts_group["app_id"] == "alerts"
        assert alerts_group["app_display_name"] == "Alert Templates"
        assert alerts_group["app_enabled"] is True

    async def test_alerts_settings_group_reports_disabled_app(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """List a disabled owning app with ``app_enabled=False``."""
        override_session.add(
            AppState(app_key="alerts", lifecycle_state=AppLifecycleEnum.DISABLED)
        )
        await override_session.commit()

        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        alerts_group = _find_group(
            response.json(),
            "AlertsSettings",
        )
        assert alerts_group["is_app_owned"] is True
        assert alerts_group["app_id"] == "alerts"
        assert alerts_group["app_enabled"] is False

    async def test_alert_settings_stays_core_when_alerts_app_disabled(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Keep the ``ALERTING`` delivery config core-owned and ungated.

        Seven non-alerts apps and the Tasks worker read ``AlertSettings``, so
        the frontend's app-owned filter must never be able to hide it.
        """
        override_session.add(
            AppState(app_key="alerts", lifecycle_state=AppLifecycleEnum.DISABLED)
        )
        await override_session.commit()

        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        alert_group = _find_group(
            response.json(),
            SettingClassEnum.ALERT_SETTINGS.value,
        )
        assert alert_group["is_app_owned"] is False
        assert alert_group["app_id"] is None
        assert alert_group["app_enabled"] is None

    async def test_lists_hot_and_not_overridable_entries(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert an ExtensionsSettings group exposes HOT and NOT_OVERRIDABLE entries.

        The NESTED_ONLY parent ``SESSION_REFRESH`` is expanded into its per-leaf
        entries, each classified ``HOT``, so the LIST projection no longer carries
        a ``NESTED_ONLY`` parent summary.
        """
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        extensions_entry = next(
            group
            for group in payload["groups"]
            if group["setting_class"] == SettingClassEnum.EXTENSIONS_SETTINGS.value
        )
        reloads = {entry["reload"] for entry in extensions_entry["settings"]}
        assert reloads == {
            ReloadClassification.HOT.value,
            ReloadClassification.NOT_OVERRIDABLE.value,
        }

    async def test_no_override_marks_has_override_false(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert a field with no override row reports ``has_override=False``."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        extensions_setting = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SYNC_REFRESH_TIME",
        )
        assert extensions_setting["has_override"] is False

    async def test_connectivity_check_default_advertises_false(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert the LIST payload advertises the declared default as ``False``.

        ``default_value`` is dumped from the declared field default rather than
        the resolved value, so it reports what a settings profile that omits the
        key resolves to.
        """
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        extensions_setting = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "CONNECTIVITY_CHECK_DEFAULT",
        )
        assert extensions_setting["default_value"] is False

    async def test_session_refresh_parent_expanded_into_leaves(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert ``SESSION_REFRESH`` is replaced by one entry per leaf, no summary entry."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        extensions_settings_group = next(
            group
            for group in response.json()["groups"]
            if group["setting_class"] == SettingClassEnum.EXTENSIONS_SETTINGS.value
        )
        keys = {entry["key"] for entry in extensions_settings_group["settings"]}
        assert "SESSION_REFRESH" not in keys
        expected_leaves = {
            "SESSION_REFRESH__COOKIE_NAME",
            "SESSION_REFRESH__MAX_AGE",
            "SESSION_REFRESH__SAMESITE",
            "SESSION_REFRESH__SECURE",
            "SESSION_REFRESH__PATH",
        }
        assert expected_leaves <= keys
        for entry in extensions_settings_group["settings"]:
            if entry["key"] in expected_leaves:
                assert entry["is_complex"] is False
                assert entry["reload"] == ReloadClassification.HOT.value
                assert "__".join(entry["key_path"]) == entry["key"]

    async def test_scalar_hot_field_kept_single(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert a scalar HOT field stays one entry — expansion must not drop it.

        ``SnippetsSettings.PREVIEW_MAX_CHARS`` is a nested-overridable parent
        (HOT) with no submodel, so the enumerator yields nothing; the entry must
        survive as a single non-complex row.
        """
        response = api_admin_client.get("/api/extensions/admin/settings/")
        entry = _find_setting(
            response.json(),
            SettingClassEnum.SNIPPETS_SETTINGS.value,
            "PREVIEW_MAX_CHARS",
        )
        assert entry["is_complex"] is False
        assert entry["reload"] == ReloadClassification.HOT.value
        assert entry["key_path"] == ["PREVIEW_MAX_CHARS"]


@pytest.mark.asyncio
class TestExtensionsSettingsGet:
    """Cover ``GET /api/extensions/admin/settings/{setting_class}/{key}``."""

    async def test_existing_field_returns_metadata(
        self, api_admin_client: TestClient
    ) -> None:
        """Return a single setting's metadata and current value."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["key"] == "SYNC_REFRESH_TIME"
        assert body["setting_class"] == SettingClassEnum.EXTENSIONS_SETTINGS.value
        assert body["reload"] == ReloadClassification.HOT.value
        assert body["has_override"] is False

    async def test_top_level_field_carries_single_element_key_path(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert a top-level DETAIL response carries a single-element ``key_path``."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["key_path"] == ["SYNC_REFRESH_TIME"]

    async def test_nested_leaf_detail_carries_key_path(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert a nested-leaf DETAIL response carries its canonical ``key_path`` chain."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH__MAX_AGE"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["key_path"] == ["SESSION_REFRESH", "MAX_AGE"]
        assert "__".join(body["key_path"]) == body["key"]

    async def test_unknown_class_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject an unknown settings class with 404; the path param is an unconstrained str."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/NonExistentSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_storage_token_as_path_param_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject the storage token ``EXTENSIONS_SETTINGS``; the path speaks the class ``__name__``."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/EXTENSIONS_SETTINGS/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_unknown_key_returns_404(self, api_admin_client: TestClient) -> None:
        """Return 404 for an unknown key on a wired class."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/DOES_NOT_EXIST"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
class TestExtensionsSettingsPatch:
    """Cover ``PATCH /api/extensions/admin/settings/{setting_class}``."""

    async def test_single_key_creates_override_row(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Persist one key, creating exactly one row that reflects in next read."""
        new_value = 10
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": new_value},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body) == 1
        assert body[0]["key"] == "SYNC_REFRESH_TIME"
        assert body[0]["value"] == new_value
        assert body[0]["has_override"] is True

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
        )
        assert len(rows) == 1
        assert rows[0].key == "SYNC_REFRESH_TIME"
        assert rows[0].value == new_value

    async def test_storage_token_as_path_param_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject PATCH against the storage token; the path speaks the class ``__name__``."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/EXTENSIONS_SETTINGS",
            json={"SYNC_REFRESH_TIME": 10},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_materializer_field_footer_template_is_patchable(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Accept a ``FOOTER_TEMPLATE`` override and store it as raw JSON.

        Regression: ``FOOTER_TEMPLATE`` declares a materializer because
        ``TypeAdapter(Template)`` raises ``PydanticSchemaGenerationError``; the
        PATCH validation must route through the materializer (not the bare
        coercion that returned HTTP 500) and persist the raw string so the
        snapshot loader re-materializes it to a ``Template``.
        """
        raw_template = "$summary custom $version"
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"FOOTER_TEMPLATE": raw_template},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="FOOTER_TEMPLATE",
        )
        assert len(rows) == 1
        assert rows[0].value == raw_template

        snapshot = await build_snapshot(override_session, ExtensionsSettings)
        assert isinstance(snapshot["FOOTER_TEMPLATE"], Template)
        assert snapshot["FOOTER_TEMPLATE"].template == raw_template

    async def test_materializer_field_footer_template_rejects_non_string(
        self,
        api_admin_client: TestClient,
    ) -> None:
        """Reject a non-string ``FOOTER_TEMPLATE`` override with HTTP 422.

        Regression: the materializer must reject a non-string payload (which
        would otherwise be published and crash the next ``safe_substitute`` read)
        and the API must surface it as 422, not 500.
        """
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"FOOTER_TEMPLATE": 123},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_existing_override_is_updated(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Update the row instead of inserting when patching an already-overridden key."""
        first_value = 10
        second_value = 20
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": first_value},
        )
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": second_value},
        )
        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
        )
        assert len(rows) == 1
        assert rows[0].value == second_value

    async def test_multiple_keys_persist_atomically(
        self, api_admin_client: TestClient
    ) -> None:
        """Persist three valid keys as three rows, all visible on the next GET."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                "SYNC_REFRESH_TIME": 12,
                "ARTIFACT_DOWNLOAD_TTL": 1200,
                "CONNECTIVITY_CHECK_DEFAULT": False,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        expected_keys = 3
        assert len(response.json()) == expected_keys

        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        sync = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SYNC_REFRESH_TIME",
        )
        ttl = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "ARTIFACT_DOWNLOAD_TTL",
        )
        check = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "CONNECTIVITY_CHECK_DEFAULT",
        )
        expected_ttl = 1200
        expected_sync = 12
        assert sync["value"] == expected_sync
        assert ttl["value"] == expected_ttl
        assert check["value"] is False

    async def test_partial_failure_rolls_back(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Reject the whole batch on a single invalid key — zero rows are written."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10, "ARTIFACT_DOWNLOAD_TTL": "not-a-number"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_inline_refresh_reflects_in_proxy(
        self, api_admin_client: TestClient
    ) -> None:
        """Return the new value from the proxy after PATCH without the background refresher."""
        original = extensions_settings.SYNC_REFRESH_TIME
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": original + 5},
        )
        try:
            assert original + 5 == extensions_settings.SYNC_REFRESH_TIME
        finally:
            extensions_settings._set_snapshot({})

    async def test_unknown_key_returns_422(self, api_admin_client: TestClient) -> None:
        """Reject an unknown key with ``type='unknown_key'`` in the per-key error."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"NONEXISTENT": 1},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(entry["type"] == "unknown_key" for entry in detail)

    async def test_not_overridable_field_returns_422(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject a NOT_OVERRIDABLE field PATCH with 422 and ``type='not_overridable'``."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"PROXY_HEADERS": True},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == ReloadClassification.NOT_OVERRIDABLE.value
            for entry in detail
        )

    async def test_constraint_violation_returns_422(
        self, api_admin_client: TestClient
    ) -> None:
        """Surface the Pydantic constraint error on a ``PositiveInt`` violation."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"ARTIFACT_DOWNLOAD_TTL": -1},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("DIAGNOSTICS_DELIVERY", _DELIVERY_PLAN_PAYLOAD),
            ("DIAGNOSTICS_DELIVERY__secrets", {"api_key": "plan-secret"}),
            ("DIAGNOSTICS_DELIVERY__upload__path", "elsewhere"),
        ],
    )
    async def test_diagnostics_delivery_patch_rejected(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        key: str,
        value: Any,
    ) -> None:
        """Reject whole-plan and per-leaf overrides of the delivery plan alike.

        A per-leaf override would merge without re-running the plan's
        cross-reference validator, so no row may be written for this block.
        """
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={key: value},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == ReloadClassification.NOT_OVERRIDABLE.value
            for entry in detail
        )

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_diagnostics_delivery_secrets_are_masked_on_read(
        self, api_admin_client: TestClient, mocker
    ) -> None:
        """Mask every plan secret in the settings LIST projection."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            DeliveryPlan(**_DELIVERY_PLAN_PAYLOAD),
        )
        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        entry = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "DIAGNOSTICS_DELIVERY",
        )

        assert entry["value"]["secrets"]["api_key"] == "**********"
        assert "plan-secret" not in json_serializer(list_payload)

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_whole_object_patch_persists_and_masks(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Accept the atomic write an operator uses to turn delivery on."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": _DELIVERY_INPUTS_SECRETS}},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        assert len(rows) == 1

        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        entry = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            _DELIVERY_INPUTS_KEY,
        )
        assert entry["reload"] == ReloadClassification.HOT.value
        assert entry["value"]["secrets"]["sn_api_key"] == SECRET_STR_MASK
        assert "key-value" not in json_serializer(list_payload)

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_patch_carries_the_endpoint(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Store the receiver an operator names alongside the credentials."""
        endpoint = "https://elsewhere.example.com/"
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "endpoint": endpoint,
                    "secrets": _DELIVERY_INPUTS_SECRETS,
                }
            },
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        assert rows[0].value["endpoint"] == endpoint

        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        entry = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            _DELIVERY_INPUTS_KEY,
        )
        assert entry["value"]["endpoint"] == endpoint

    @pytest.mark.usefixtures("delivery_skeleton")
    @pytest.mark.parametrize(
        ("secrets", "expected"),
        [
            ({**_DELIVERY_INPUTS_SECRETS, "extra_key": "c"}, "extra_key"),
            ({"sn_api_key": "key-value"}, "client_token"),
        ],
    )
    async def test_delivery_inputs_secret_names_must_match_the_plan(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        secrets: dict[str, str],
        expected: str,
    ) -> None:
        """Refuse a payload whose secret names are not exactly the declared ones."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": secrets}},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == "value_error" and expected in entry["msg"]
            for entry in detail
        )
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (f"{_DELIVERY_INPUTS_KEY}__secrets", {"sn_api_key": "key-value"}),
            (f"{_DELIVERY_INPUTS_KEY}__endpoint", "https://elsewhere.example.com/"),
        ],
    )
    async def test_delivery_inputs_leaf_patch_rejected(
        self,
        api_admin_client: TestClient,
        key: str,
        value: Any,
    ) -> None:
        """Refuse a per-leaf write, which would bypass the materializer entirely."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={key: value},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == ReloadClassification.NOT_OVERRIDABLE.value
            for entry in detail
        )

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_resubmitted_mask_keeps_the_stored_secret(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Keep the stored credential when an operator re-submits the masked read."""
        stored = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": _DELIVERY_INPUTS_SECRETS}},
        )
        assert stored.status_code == status.HTTP_200_OK
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "secrets": dict.fromkeys(_DELIVERY_INPUTS_SECRETS, SECRET_STR_MASK)
                }
            },
        )

        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        assert _decrypted_secrets(rows[0].value["secrets"]) == _DELIVERY_INPUTS_SECRETS

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_mask_without_a_stored_row_is_rejected(
        self, api_admin_client: TestClient
    ) -> None:
        """Refuse the mask when restoration had nothing to put back."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "secrets": dict.fromkeys(_DELIVERY_INPUTS_SECRETS, SECRET_STR_MASK)
                }
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_masked_endpoint_preserves_stored_password(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Restore the stored endpoint password on a masked whole-object resubmit.

        A materializer-backed field takes the model-payload branch of
        ``preserve_patch_credential_url_value``, a third call shape beside the
        top-level and nested-leaf legs. Now that the shared type rejects the
        mask, preserve running before validation is what keeps this round-trip
        a 200 rather than a 422.
        """
        endpoint = "https://sn-user:sn-secret@snow.example.com/"
        stored = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "endpoint": endpoint,
                    "secrets": _DELIVERY_INPUTS_SECRETS,
                }
            },
        )
        assert stored.status_code == status.HTTP_200_OK

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "endpoint": "https://sn-user:****@snow.example.com/",
                    "secrets": dict.fromkeys(_DELIVERY_INPUTS_SECRETS, SECRET_STR_MASK),
                }
            },
        )

        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        stored_endpoint = rows[0].value["endpoint"]
        assert stored_plaintext(urlparse(stored_endpoint).password) == "sn-secret"
        assert "****" not in stored_endpoint

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_row_that_stops_matching_the_plan_survives(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        mocker,
    ) -> None:
        """Keep a stale row readable after an upgrade renames a declared secret.

        Inverts the previous contract, under which the row was dropped from the
        snapshot: the resolver then read exactly what a never-configured
        deployment reads, and the evidence of the rename survived only as a log
        line in whichever process rebuilt the snapshot.
        """
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": _DELIVERY_INPUTS_SECRETS}},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        assert len(rows) == 1
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            DeliveryPlan(**_renamed_skeleton()),
        )

        snapshot = await build_snapshot(override_session, ExtensionsSettings)

        assert set(snapshot[_DELIVERY_INPUTS_KEY].secrets) == set(
            _DELIVERY_INPUTS_SECRETS
        )

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_patch_after_a_rename_is_still_rejected(
        self, api_admin_client: TestClient, mocker
    ) -> None:
        """Refuse a payload naming what the plan declares today, not yesterday.

        Read-time leniency is scoped to rows stored earlier; a payload submitted
        now against a plan it does not match is still a client error.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            DeliveryPlan(**_renamed_skeleton()),
        )

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": _DELIVERY_INPUTS_SECRETS}},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_mask_restores_across_a_surviving_drifted_row(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        mocker,
    ) -> None:
        """Restore a masked secret by name, never by position, once a row drifts.

        A drifted row now reaches the proxy where the PATCH handler previously
        saw ``None``, so mask restoration has a stored value to consult again.
        It is keyed by secret name, so the credential of a name the rename kept
        cannot be restored under the name it introduced.
        """
        stored = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={_DELIVERY_INPUTS_KEY: {"secrets": _DELIVERY_INPUTS_SECRETS}},
        )
        assert stored.status_code == status.HTTP_200_OK
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            DeliveryPlan(**_renamed_skeleton()),
        )

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "secrets": {
                        "sn_api_key": SECRET_STR_MASK,
                        "case_token": "fresh-token",
                    }
                }
            },
        )

        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        assert _decrypted_secrets(rows[0].value["secrets"]) == {
            "sn_api_key": _DELIVERY_INPUTS_SECRETS["sn_api_key"],
            "case_token": "fresh-token",
        }

    async def test_app_drain_nested_leaf_patch_creates_override(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert ``APP_DRAIN`` is NESTED_ONLY, so a leaf PATCH persists a row."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"APP_DRAIN__stale_task_ttl": 7200},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert [r.key for r in rows] == ["APP_DRAIN__stale_task_ttl"]

    async def test_app_drain_whole_object_patch_rejected(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Reject a whole-object PATCH of the NESTED_ONLY ``APP_DRAIN`` parent."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"APP_DRAIN": {"stale_task_ttl": 7200}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_app_drain_non_positive_ttl_rejected(
        self, api_admin_client: TestClient
    ) -> None:
        """Surface the ``stale_task_ttl`` positive-duration validator as 422."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"APP_DRAIN__stale_task_ttl": 0},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_snippets_base_url_hot_patch(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert ``SNIPPETS_BASE_URL`` is HOT and accepts a PATCH."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/SnippetsSettings",
            json={"SNIPPETS_BASE_URL": "https://snippets.example.com/"},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SNIPPETS_SETTINGS_TOKEN
        )
        assert [r.key for r in rows] == ["SNIPPETS_BASE_URL"]

    async def test_sync_filter_hot_patch_round_trip(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert ``SYNC_FILTER`` is HOT; a valid PATCH persists and reflects."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/SnippetsSettings",
            json={"SYNC_FILTER": [".sh"]},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            rows = await SettingsOverrideManager.list(
                override_session, setting_class=SNIPPETS_SETTINGS_TOKEN
            )
            assert [r.key for r in rows] == ["SYNC_FILTER"]
            assert {
                SnippetFilter(".sh", SnippetFilterType.EXTENSION)
            } == snippets_settings.SYNC_FILTER
        finally:
            snippets_settings._set_snapshot({})

    async def test_sync_filter_bad_member_rejected(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Reject a malformed ``SYNC_FILTER`` set member with 422 and no row."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/SnippetsSettings",
            json={"SYNC_FILTER": [12345]},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SNIPPETS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_mixed_failure_modes_aggregate_in_detail(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Aggregate three error types in one batch into three matching ``detail`` entries."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                "SYNC_REFRESH_TIME": 10,
                "BOGUS_KEY": 1,
                "PROXY_HEADERS": True,
                "ARTIFACT_DOWNLOAD_TTL": -1,
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert "unknown_key" in types
        assert ReloadClassification.NOT_OVERRIDABLE.value in types
        assert any("greater_than" in t for t in types)

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_empty_body_returns_422(self, api_admin_client: TestClient) -> None:
        """Reject an empty PATCH body via the ``min_length=1`` root model constraint."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings", json={}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_integrity_error_triggers_single_retry(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        admin_user: CasdoorUser,
    ) -> None:
        """Retry once on a concurrent-PATCH IntegrityError (one rollback + replay); the row lands.

        The reported provenance must come from the replay: the first attempt's
        writes were rolled back, so returning its stamp would report a write that
        never happened.
        """
        new_value = 17
        original = settings_routes._stage_and_commit_overrides
        raised = False

        async def flaky(**kwargs: Any) -> dict[str, Any]:
            nonlocal raised
            if not raised:
                raised = True
                raise IntegrityError("statement", "params", Exception("dup"))
            return await original(**kwargs)

        with patch.object(
            settings_routes, "_stage_and_commit_overrides", side_effect=flaky
        ) as spy:
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/ExtensionsSettings",
                json={"SYNC_REFRESH_TIME": new_value},
            )
        assert response.status_code == status.HTTP_200_OK

        expected_call_count = 2
        assert spy.call_count == expected_call_count

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert len(rows) == 1
        assert rows[0].value == new_value
        assert rows[0].is_active is True
        assert rows[0].updated_by == admin_user.username
        entry = response.json()[0]
        assert entry["updated_by"] == admin_user.username
        assert datetime.fromisoformat(entry["updated_at"]) == make_datetime_utc(
            rows[0].updated_at
        )


@pytest.mark.asyncio
class TestExtensionsSettingsDelete:
    """Cover ``DELETE /api/extensions/admin/settings/{setting_class}/{key}``."""

    async def test_delete_existing_override(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Delete an override row, returning 204 and clearing ``has_override``."""
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 11},
        )
        assert patched.status_code == status.HTTP_200_OK
        assert await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )

        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert rows == []

    async def test_delete_idempotent_when_no_row(
        self, api_admin_client: TestClient
    ) -> None:
        """Return 204 when deleting a HOT field with no override row."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    async def test_delete_not_overridable_returns_409(
        self, api_admin_client: TestClient
    ) -> None:
        """Return 409 when deleting a NOT_OVERRIDABLE field — the row can't exist."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/PROXY_HEADERS"
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_delete_unknown_key_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Return 404 when deleting an unknown key."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/DOES_NOT_EXIST"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_storage_token_as_path_param_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject DELETE against the storage token; the path speaks the class ``__name__``."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/EXTENSIONS_SETTINGS/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
class TestExtensionsSettingsAppOwnedAlerts:
    """Resolve the app-owned ``AlertsSettings`` class by its ``__name__``."""

    async def test_get_alerts_setting(self, api_admin_client: TestClient) -> None:
        """Return one field from ``GET /settings/AlertsSettings/{key}``."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/AlertsSettings/ALERT_FOLDER_NAME"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["setting_class"] == "AlertsSettings"
        assert body["key"] == "ALERT_FOLDER_NAME"

    async def test_storage_token_as_path_param_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject the storage token ``ALERTS_SETTINGS`` the same way as ``EXTENSIONS_SETTINGS``."""
        get_response = api_admin_client.get(
            "/api/extensions/admin/settings/ALERTS_SETTINGS/ALERT_FOLDER_NAME"
        )
        assert get_response.status_code == status.HTTP_404_NOT_FOUND

        patch_response = api_admin_client.patch(
            "/api/extensions/admin/settings/ALERTS_SETTINGS",
            json={"ALERT_FOLDER_NAME": "Should Not Persist"},
        )
        assert patch_response.status_code == status.HTTP_404_NOT_FOUND

        delete_response = api_admin_client.delete(
            "/api/extensions/admin/settings/ALERTS_SETTINGS/ALERT_FOLDER_NAME"
        )
        assert delete_response.status_code == status.HTTP_404_NOT_FOUND

    async def test_patch_and_delete_alerts_setting(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Persist and clear an ``AlertsSettings`` override through value-form paths."""
        try:
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/AlertsSettings",
                json={"ALERT_FOLDER_NAME": "Patched Alerts"},
            )
            assert response.status_code == status.HTTP_200_OK
            assert alerts_settings.ALERT_FOLDER_NAME == "Patched Alerts"
            rows = await SettingsOverrideManager.list(
                override_session, setting_class="ALERTS_SETTINGS"
            )
            assert len(rows) == 1
            assert rows[0].key == "ALERT_FOLDER_NAME"
            assert rows[0].value == "Patched Alerts"

            deleted = api_admin_client.delete(
                "/api/extensions/admin/settings/AlertsSettings/ALERT_FOLDER_NAME"
            )
            assert deleted.status_code == status.HTTP_204_NO_CONTENT
            rows = await SettingsOverrideManager.list(
                override_session, setting_class="ALERTS_SETTINGS"
            )
            assert rows == []
        finally:
            alerts_settings._set_snapshot({})


@pytest.mark.asyncio
class TestExtensionsSettingsNestedOverrides:
    """Cover ``__``-delimited nested overrides on ``ExtensionsSettings.SESSION_REFRESH``."""

    @pytest.fixture(autouse=True)
    def _reset_proxy_snapshot(self) -> Iterator[None]:
        """Clear the global proxy snapshot after each nested test."""
        yield
        extensions_settings._set_snapshot({})

    async def test_patch_nested_override_persists_and_marks_parent(
        self, api_admin_client: TestClient
    ) -> None:
        """Persist a nested PATCH, echo the nested key, and mark the parent."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__SAMESITE": "strict"},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body[0]["key"] == "SESSION_REFRESH__SAMESITE"
        assert body[0]["value"] == "strict"
        assert body[0]["has_override"] is True
        # The parent reads back as having an override.
        parent = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH"
        )
        assert parent.json()["has_override"] is True
        # The nested leaf reads back its current value.
        leaf = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH__SAMESITE"
        )
        assert leaf.json()["value"] == "strict"

    async def test_patch_nested_echoes_key_path(
        self, api_admin_client: TestClient
    ) -> None:
        """Echo the leaf's canonical ``key_path`` chain on a nested PATCH."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__SAMESITE": "strict"},
        )
        assert response.status_code == status.HTTP_200_OK
        echoed = response.json()[0]
        assert echoed["key_path"] == ["SESSION_REFRESH", "SAMESITE"]
        assert "__".join(echoed["key_path"]) == echoed["key"]

    async def test_patch_nested_coerces_int_to_timedelta(
        self, api_admin_client: TestClient
    ) -> None:
        """Coerce a JSON int on ``SESSION_REFRESH__MAX_AGE`` to a timedelta."""
        override_seconds = 7200
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__MAX_AGE": override_seconds},
        )
        assert response.status_code == status.HTTP_200_OK
        assert (
            extensions_settings.SESSION_REFRESH.MAX_AGE.total_seconds()
            == override_seconds
        )

    async def test_patch_nested_rejects_unknown_nested_field(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject an unknown nested leaf with ``unknown_nested_field``."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__BOGUS": 1},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(entry["type"] == "unknown_nested_field" for entry in detail)

    async def test_patch_nested_rejects_not_overridable_parent(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject a nested key under a non-overridable parent as not_overridable."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"DATABASE__NAME": "other.db"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(entry["type"] == "not_overridable" for entry in detail)

    async def test_patch_whole_parent_rejected_for_nested_only(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject replacing the whole NESTED_ONLY parent object."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH": {"MAX_AGE": 3600}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == "not_overridable"
            and entry["loc"] == ["body", "SESSION_REFRESH"]
            for entry in detail
        )

    async def test_delete_whole_parent_rejected_for_nested_only(
        self, api_admin_client: TestClient
    ) -> None:
        """Return 422 (not 404) on DELETE of the whole NESTED_ONLY parent."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH"
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(entry["type"] == "not_overridable" for entry in detail)

    async def test_get_whole_parent_returns_merged_value(
        self, api_admin_client: TestClient
    ) -> None:
        """Allow GET on the whole parent and return the merged value."""
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__SAMESITE": "strict"},
        )
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["value"]["SAMESITE"] == "strict"

    async def test_delete_nested_override_clears_merged_value(
        self, api_admin_client: TestClient
    ) -> None:
        """Revert the leaf to its YAML/env value when deleting a nested override (AC #3)."""
        override_seconds = 7200
        original = extensions_settings.SESSION_REFRESH.MAX_AGE
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__MAX_AGE": override_seconds},
        )
        assert (
            extensions_settings.SESSION_REFRESH.MAX_AGE.total_seconds()
            == override_seconds
        )
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH__MAX_AGE"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert original == extensions_settings.SESSION_REFRESH.MAX_AGE

    async def test_delete_nested_override_idempotent_when_absent(
        self, api_admin_client: TestClient
    ) -> None:
        """Return 204 when deleting a never-set nested override."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SESSION_REFRESH__MAX_AGE"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

    async def test_list_marks_overridden_leaf_independently_of_siblings(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert each leaf carries its own ``has_override``; a sibling stays ``False``."""
        api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SESSION_REFRESH__SAMESITE": "strict"},
        )
        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        overridden = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SESSION_REFRESH__SAMESITE",
        )
        sibling = _find_setting(
            list_payload,
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SESSION_REFRESH__MAX_AGE",
        )
        assert overridden["has_override"] is True
        assert sibling["has_override"] is False


@pytest.mark.asyncio
class TestExtensionsSettingsAuth:
    """Authentication / authorisation tests for the settings router."""

    async def test_unauthenticated_get_returns_401(
        self, api_unauthenticated_client: TestClient
    ) -> None:
        """Respond with a JSON 401 to an unauthenticated GET."""
        response = api_unauthenticated_client.get(
            "/api/extensions/admin/settings/", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")

    async def test_non_admin_get_returns_403(
        self, api_non_admin_client: TestClient
    ) -> None:
        """Reject a non-admin user with 403 on every endpoint."""
        response = api_non_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_non_admin_patch_returns_403(
        self, api_non_admin_client: TestClient
    ) -> None:
        """Reject a non-admin user's attempt to mutate settings."""
        response = api_non_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_cookie_admin_patch_without_bearer_returns_401(
        self, api_admin_cookie_client: TestClient
    ) -> None:
        """Reject a cookie-authenticated admin PATCH without a Bearer header (CSRF defense)."""
        response = api_admin_cookie_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_cookie_admin_delete_without_bearer_returns_401(
        self, api_admin_cookie_client: TestClient
    ) -> None:
        """Reject a cookie-authenticated admin DELETE without a Bearer header (CSRF defense)."""
        response = api_admin_cookie_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_cookie_admin_can_still_read(
        self, api_admin_cookie_client: TestClient
    ) -> None:
        """Allow GET via cookie auth — only mutations require Bearer."""
        response = api_admin_cookie_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
class TestExtensionsSettingsSecondaryClasses:
    """Smoke tests for the Snippets and Messages classes wired alongside PMM Extensions."""

    async def test_patch_snippets_setting(self, api_admin_client: TestClient) -> None:
        """Assert a Snippets HOT field is patchable via the PMM Extensions router."""
        original = snippets_settings.ENABLE_MANUAL_SYNC
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/SnippetsSettings",
            json={"ENABLE_MANUAL_SYNC": not original},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            assert snippets_settings.ENABLE_MANUAL_SYNC is (not original)
        finally:
            snippets_settings._set_snapshot({})


@pytest.mark.asyncio
class TestExtensionsSettingsAlertSettings:
    """Exercise the core ``AlertSettings`` class through the PMM Extensions router."""

    async def test_get_alert_setting(self, api_admin_client: TestClient) -> None:
        """Return one alert field from ``GET /settings/AlertSettings/{key}``."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/AlertSettings/SOURCE_PREFIX"
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["setting_class"] == SettingClassEnum.ALERT_SETTINGS.value
        assert payload["key"] == "SOURCE_PREFIX"

    async def test_patch_alert_setting(self, api_admin_client: TestClient) -> None:
        """Patch an AlertSettings HOT field via the PMM Extensions router."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/AlertSettings",
            json={"SOURCE_PREFIX": "test-prefix-"},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            assert alert_settings.SOURCE_PREFIX == "test-prefix-"
        finally:
            alert_settings._set_snapshot({})

    async def test_providers_masked_patch_preserves_routing_key(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Keep a stored PagerDuty routing key when PROVIDERS is resubmitted masked."""
        secret = "sep-1615-pagerduty-routing-key"
        try:
            assert (
                api_admin_client.patch(
                    "/api/extensions/admin/settings/AlertSettings",
                    json={
                        "PROVIDERS": [
                            {"PROVIDER": "pagerduty", "routing_key": secret},
                        ]
                    },
                ).status_code
                == status.HTTP_200_OK
            )
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/AlertSettings",
                json={
                    "PROVIDERS": [
                        {
                            "PROVIDER": "pagerduty",
                            "routing_key": SECRET_STR_MASK,
                            "api_endpoint": "https://events.pagerduty.com/v2/",
                        }
                    ]
                },
            )
            assert response.status_code == status.HTTP_200_OK
            rows = await SettingsOverrideManager.list(
                override_session, setting_class=ALERT_SETTINGS_TOKEN
            )
            providers_row = next(row for row in rows if row.key == "PROVIDERS")
            assert stored_plaintext(providers_row.value[0]["routing_key"]) == secret
        finally:
            api_admin_client.delete(
                "/api/extensions/admin/settings/AlertSettings/PROVIDERS"
            )
            alert_settings._set_snapshot({})

    async def test_providers_masked_patch_preserves_two_routing_keys(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Keep each PagerDuty routing key when two PROVIDERS are resubmitted masked."""
        secret_a = "sep-1615-pagerduty-routing-key-a"
        secret_b = "sep-1615-pagerduty-routing-key-b"
        endpoint_a = "https://events-a.example/v2/"
        endpoint_b = "https://events-b.example/v2/"
        try:
            assert (
                api_admin_client.patch(
                    "/api/extensions/admin/settings/AlertSettings",
                    json={
                        "PROVIDERS": [
                            {
                                "PROVIDER": "pagerduty",
                                "routing_key": secret_a,
                                "api_endpoint": endpoint_a,
                            },
                            {
                                "PROVIDER": "pagerduty",
                                "routing_key": secret_b,
                                "api_endpoint": endpoint_b,
                            },
                        ]
                    },
                ).status_code
                == status.HTTP_200_OK
            )
            # Resubmit in reverse endpoint order so positional pairing against
            # an unstable set iteration would swap the routing keys.
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/AlertSettings",
                json={
                    "PROVIDERS": [
                        {
                            "PROVIDER": "pagerduty",
                            "routing_key": SECRET_STR_MASK,
                            "api_endpoint": endpoint_b,
                        },
                        {
                            "PROVIDER": "pagerduty",
                            "routing_key": SECRET_STR_MASK,
                            "api_endpoint": endpoint_a,
                        },
                    ]
                },
            )
            assert response.status_code == status.HTTP_200_OK
            rows = await SettingsOverrideManager.list(
                override_session, setting_class=ALERT_SETTINGS_TOKEN
            )
            providers_row = next(row for row in rows if row.key == "PROVIDERS")
            by_endpoint = {
                entry["api_endpoint"]: stored_plaintext(entry["routing_key"])
                for entry in providers_row.value
            }
            assert by_endpoint[endpoint_a] == secret_a
            assert by_endpoint[endpoint_b] == secret_b
        finally:
            api_admin_client.delete(
                "/api/extensions/admin/settings/AlertSettings/PROVIDERS"
            )
            alert_settings._set_snapshot({})


@pytest.mark.asyncio
class TestExtensionsSettingsCredentialUrlRedaction:
    """Verify that LIST and DETAIL redact embedded URL passwords on credential-bearing fields."""

    _FULL_URL = "http://inv-user:inv-secret@inventory.internal:8080"

    @pytest.fixture(autouse=True)
    def _reset_snapshot(self) -> Iterator[None]:
        """Clear override snapshots after each test."""
        yield
        extensions_settings._set_snapshot({})

    async def test_list_redacts_inventory_endpoint(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert ``GET /settings/`` masks ``INVENTORY_ENDPOINT`` password components."""
        extensions_settings._set_snapshot({"INVENTORY_ENDPOINT": self._FULL_URL})
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        entry = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "INVENTORY_ENDPOINT",
        )
        assert "inv-secret" not in entry["value"]
        assert "****" in entry["value"]
        assert "inv-user" in entry["value"]

    async def test_detail_redacts_inventory_endpoint(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert ``GET /settings/{class}/{key}`` masks ``INVENTORY_ENDPOINT`` passwords."""
        extensions_settings._set_snapshot({"INVENTORY_ENDPOINT": self._FULL_URL})
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/INVENTORY_ENDPOINT"
        )
        assert response.status_code == status.HTTP_200_OK
        value = response.json()["value"]
        assert "inv-secret" not in value
        assert "****" in value
        assert "inv-user" in value


@pytest.mark.asyncio
class TestExtensionsSettingsCredentialUrlWriteback:
    """Verify PATCH does not persist redacted URL display values over stored credentials."""

    async def test_patch_redacted_inventory_endpoint_preserves_password(
        self, api_admin_client: TestClient
    ) -> None:
        """Keep the real password when saving an unchanged redacted ``INVENTORY_ENDPOINT``.

        Preserve runs before validation: a masked resubmit is swapped for the
        stored password first, so the shared mask-rejecting validator never
        sees the display value and the round-trip succeeds.
        """
        full_url = "http://inv-user:inv-secret@inventory.internal:8080"
        redacted_url = "http://inv-user:****@inventory.internal:8080"
        try:
            extensions_settings._set_snapshot({"INVENTORY_ENDPOINT": full_url})
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/ExtensionsSettings",
                json={"INVENTORY_ENDPOINT": redacted_url},
            )
            assert response.status_code == status.HTTP_200_OK
            assert "inv-secret" in str(extensions_settings.INVENTORY_ENDPOINT)
            assert "****" not in str(extensions_settings.INVENTORY_ENDPOINT)
        finally:
            extensions_settings._set_snapshot({})

    async def test_patch_masked_endpoint_without_stored_password_is_rejected(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject a masked endpoint when there is no stored password to restore.

        The YAML/env baseline has no userinfo password, so preserve leaves the
        mask intact and the shared type validator fails the PATCH with 422.
        """
        redacted_url = "http://inv-user:****@inventory.internal:8080"
        try:
            extensions_settings._set_snapshot({})
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/ExtensionsSettings",
                json={"INVENTORY_ENDPOINT": redacted_url},
            )
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
            assert "cannot be stored" in response.text
        finally:
            extensions_settings._set_snapshot({})


@pytest.mark.asyncio
class TestSettingsNestedCredentialUrlWriteback:
    """Pin preserve-then-validate on the nested ``PMM__ENDPOINT`` PATCH path.

    ``Settings.PMM`` is a hot nested parent and ``PMMSettings.endpoint`` is a
    ``StrCredentialHttpUrl``, so ``PMM__ENDPOINT`` goes through
    ``_validate_nested_key`` rather than the top-level ``_validate_patch_body``
    leg covered by :class:`TestExtensionsSettingsCredentialUrlWriteback`.
    """

    async def test_patch_redacted_pmm_endpoint_preserves_password(
        self, api_admin_client: TestClient
    ) -> None:
        """Keep the real password when saving an unchanged redacted ``PMM__ENDPOINT``.

        Preserve runs before validation on the nested leaf: a masked resubmit
        is swapped for the stored password first, so the shared mask-rejecting
        validator never sees the display value and the round-trip succeeds.
        """
        full_url = "https://pmm-user:pmm-secret@pmm.example.com:8443"
        redacted_url = "https://pmm-user:****@pmm.example.com:8443"
        try:
            settings._set_snapshot({"PMM": PMMSettings(endpoint=full_url)})
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/Settings",
                json={"PMM__ENDPOINT": redacted_url},
            )
            assert response.status_code == status.HTTP_200_OK
            assert "pmm-secret" in str(settings.PMM.endpoint)
            assert "****" not in str(settings.PMM.endpoint)
        finally:
            settings._set_snapshot({})

    async def test_patch_masked_pmm_endpoint_without_stored_password_is_rejected(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject a masked ``PMM__ENDPOINT`` when there is no stored password.

        The YAML/env baseline has no userinfo password, so preserve leaves the
        mask intact and the shared type validator fails the PATCH with 422.
        """
        redacted_url = "https://pmm-user:****@pmm.example.com:8443"
        try:
            settings._set_snapshot({})
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/Settings",
                json={"PMM__ENDPOINT": redacted_url},
            )
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
            assert "cannot be stored" in response.text
        finally:
            settings._set_snapshot({})


@pytest.mark.asyncio
class TestExtensionsSettingsInlineRebind:
    """Fire the PMM Extensions rebind callbacks on an inline PATCH.

    Same defect as the Tasks ``NOMAD`` rebind: the endpoint rebinders are fired
    only on a refresher-observed snapshot diff, but the PATCH handler publishes
    the new snapshot inline, so the next refresh cycle sees no change. The handler
    must fire the registered callback for the changed key itself.
    """

    @pytest.fixture(name="endpoint_callback_spy")
    def endpoint_callback_spy_fixture(self) -> Iterator[AsyncMock]:
        """Register a spy as the ``(EXTENSIONS_SETTINGS, INVENTORY_ENDPOINT)`` callback on state."""
        spy = AsyncMock()
        original = getattr(extensions_app.state, "override_callbacks", None)
        extensions_app.state.override_callbacks = {
            (SettingClassEnum.EXTENSIONS_SETTINGS, "INVENTORY_ENDPOINT"): spy,
        }
        extensions_settings._set_snapshot({})
        yield spy
        extensions_app.state.override_callbacks = original
        extensions_settings._set_snapshot({})

    async def test_patch_inventory_endpoint_fires_rebind_callback(
        self, api_admin_client: TestClient, endpoint_callback_spy: AsyncMock
    ) -> None:
        """Fire the ``INVENTORY_ENDPOINT`` rebind callback inline on PATCH."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"INVENTORY_ENDPOINT": "https://new-inventory.example.org"},
        )
        assert response.status_code == status.HTTP_200_OK
        endpoint_callback_spy.assert_awaited_once()

    async def test_patch_unrelated_key_does_not_fire_endpoint_callback(
        self, api_admin_client: TestClient, endpoint_callback_spy: AsyncMock
    ) -> None:
        """Leave the endpoint rebinder untouched when PATCHing an unrelated PMM Extensions key."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 17},
        )
        try:
            assert response.status_code == status.HTTP_200_OK
            endpoint_callback_spy.assert_not_awaited()
        finally:
            extensions_settings._set_snapshot({})


@pytest.mark.asyncio
class TestExtensionsOverridesLifespanWiring:
    """Publish the rebind registry on ``extensions_app.state`` from the overrides lifespan."""

    async def test_lifespan_publishes_override_callbacks_on_extensions_app_state(
        self,
    ) -> None:
        """Assert ``extensions_overrides_lifespan`` exposes the endpoint/PMM rebinders on state.

        The handler reads ``request.app.state.override_callbacks``; for PMM Extensions routes
        ``request.app`` resolves to the module-level ``extensions_app`` mount, so the
        registry must be published there -- not on the (parent) ``app`` argument
        threaded through ``extensions_overrides_lifespan`` under the combined app.
        """
        original = getattr(extensions_app.state, "override_callbacks", None)
        try:
            async with extensions_overrides_lifespan(FastAPI()):
                keys = set(extensions_app.state.override_callbacks)
            assert keys == {
                (SettingClassEnum.EXTENSIONS_SETTINGS, "INVENTORY_ENDPOINT"),
                (SettingClassEnum.EXTENSIONS_SETTINGS, "TASKS_ENDPOINT"),
                (SettingClassEnum.SETTINGS, "PMM"),
                (SettingClassEnum.SETTINGS, "LOGGING"),
                (SettingClassEnum.SNIPPETS_SETTINGS, "SYNC_INTERVAL"),
                ("AlertsSettings", "BACKUP_INTERVAL"),
                ("InventoryAppSettings", "COLLECTION_INTERVAL"),
                (SettingClassEnum.EXTENSIONS_SETTINGS, "APP_DRAIN"),
                ("OmInventorySettings", "ENABLED"),
                ("OmInventorySettings", "SCHEDULE"),
            }
        finally:
            extensions_app.state.override_callbacks = original


@pytest.mark.asyncio
class TestGlobalSettingsClass:
    """The global ``Settings`` class is reachable via the PMM Extensions router."""

    async def test_settings_group_listed(self, api_admin_client: TestClient) -> None:
        """Assert the ``Settings`` group appears in the LIST projection."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        groups = {g["setting_class"] for g in response.json()["groups"]}
        assert SettingClassEnum.SETTINGS.value in groups

    async def test_pmm_leaf_patch_persists(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Accept a per-child PATCH on a PMM leaf (HOT parent)."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM__verify_ssl": False},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN
        )
        assert [r.key for r in rows] == ["PMM__verify_ssl"]

    async def test_pmm_api_key_patch_persists_the_real_secret(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Persist the real secret, not Pydantic's ``**********`` JSON mask.

        The row holds it as ciphertext, so the assertion decrypts rather than
        comparing against the submitted string.
        """
        secret = "pmm-api-key-persisted"
        try:
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/Settings",
                json={"PMM__api_key": secret},
            )
            assert response.status_code == status.HTTP_200_OK
            assert response.json()[0]["value"] == "**********"
            rows = await SettingsOverrideManager.list(
                override_session, setting_class=SETTINGS_TOKEN
            )
            assert len(rows) == 1
            assert rows[0].key == "PMM__api_key"
            assert stored_plaintext(rows[0].value) == secret
        finally:
            api_admin_client.delete(
                "/api/extensions/admin/settings/Settings/PMM__api_key"
            )

    async def test_pmm_api_key_masked_patch_preserves_stored_secret(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Keep the stored secret when the client resubmits the redacted mask."""
        secret = "pmm-api-key-mask-roundtrip"
        try:
            assert (
                api_admin_client.patch(
                    "/api/extensions/admin/settings/Settings",
                    json={"PMM__api_key": secret},
                ).status_code
                == status.HTTP_200_OK
            )
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/Settings",
                json={"PMM__api_key": SECRET_STR_MASK},
            )
            assert response.status_code == status.HTTP_200_OK
            assert response.json()[0]["value"] == "**********"
            rows = await SettingsOverrideManager.list(
                override_session, setting_class=SETTINGS_TOKEN
            )
            assert len(rows) == 1
            assert stored_plaintext(rows[0].value) == secret
        finally:
            api_admin_client.delete(
                "/api/extensions/admin/settings/Settings/PMM__api_key"
            )

    async def test_logging_hot_patch_persists(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Assert ``LOGGING`` is HOT and accepts a PATCH."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"LOGGING": "DEBUG"},
        )
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN
        )
        assert [r.key for r in rows] == ["LOGGING"]

    async def test_logging_options_list_unique_members(
        self, api_admin_client: TestClient
    ) -> None:
        """Expose six LogLevel options (aliases excluded) with int values."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        logging_row = _find_setting(
            response.json(), SettingClassEnum.SETTINGS.value, "LOGGING"
        )
        assert logging_row["options"] == [
            {"label": "CRITICAL", "value": 50},
            {"label": "ERROR", "value": 40},
            {"label": "WARNING", "value": 30},
            {"label": "INFO", "value": 20},
            {"label": "DEBUG", "value": 10},
            {"label": "NOTSET", "value": 0},
        ]
        non_enum = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SYNC_REFRESH_TIME",
        )
        assert non_enum["options"] is None

    async def test_logging_patch_integer_value_still_works(
        self, api_admin_client: TestClient
    ) -> None:
        """Accept an integer PATCH value for LOGGING (the shape the UI sends)."""
        debug_level = 10
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"LOGGING": debug_level},
        )
        assert response.status_code == status.HTTP_200_OK
        get_resp = api_admin_client.get(
            "/api/extensions/admin/settings/Settings/LOGGING"
        )
        assert get_resp.status_code == status.HTTP_200_OK
        body = get_resp.json()
        assert body["value"] == debug_level
        assert {"label": "DEBUG", "value": debug_level} in body["options"]

    async def test_logging_invalid_level_rejected(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Reject an invalid ``LOGGING`` level with 422 and write no row."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"LOGGING": "NOTALEVEL"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN
        )
        assert rows == []

    @pytest.mark.parametrize("field", ["SECRET_KEY", "CELERY", "LOGGING_CONFIG"])
    async def test_restart_only_fields_reject_patch(
        self, api_admin_client: TestClient, field: str
    ) -> None:
        """Assert restart-only fields stay NOT_OVERRIDABLE and reject a PATCH with 422."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={field: "whatever"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        types = {entry["type"] for entry in response.json()["detail"]}
        assert ReloadClassification.NOT_OVERRIDABLE.value in types

    async def test_secret_key_value_not_leaked(
        self, api_admin_client: TestClient
    ) -> None:
        """Assert the ``SECRET_KEY`` value is never serialised in the LIST payload."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        entry = _find_setting(
            response.json(), SettingClassEnum.SETTINGS.value, "SECRET_KEY"
        )
        # SecretStr is redacted by Pydantic's secret-aware JSON dump.
        assert entry["value"] in (None, "**********")

    async def test_pmm_api_key_not_leaked(self, api_admin_client: TestClient) -> None:
        """Assert the nested PMM ``api_key`` secret is not serialised in the LIST."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        entry = _find_setting(
            response.json(), SettingClassEnum.SETTINGS.value, "PMM__api_key"
        )
        assert entry["value"] in (None, "**********")


@pytest.mark.asyncio
class TestExtensionsSettingsSecretsEncryptedAtRest:
    """Verify the write path encrypts every credential-bearing leaf a PATCH persists.

    Covers both leaf kinds: a Pydantic secret leaf, whose whole value is
    encrypted, and a credential-bearing URL, where only the userinfo password is
    and the endpoint stays readable.

    The stored ciphertext is asserted through :func:`decrypt` rather than pinned:
    Fernet derives a fresh IV per call, so two encryptions of one plaintext differ.
    """

    _CREDENTIAL_URL = "https://inv-user:inv-secret@inventory.internal:8080/api"
    _CREDENTIAL_PASSWORD = "inv-secret"

    @pytest.fixture(autouse=True)
    def _reset_snapshots(self) -> Iterator[None]:
        """Clear the snapshots a PATCH republishes so siblings are unaffected."""
        yield
        settings._set_snapshot({})
        alert_settings._set_snapshot({})
        extensions_settings._set_snapshot({})

    async def test_credential_url_patch_encrypts_only_the_password(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt ``INVENTORY_ENDPOINT``'s password while the endpoint stays legible.

        Driven through a real PATCH with no override on the body model, so the
        walker receives whatever the route's own coercion produces — a
        :class:`~pydantic.HttpUrl` for this field, which is the runtime type a
        hand-written string payload cannot reproduce.
        """
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"INVENTORY_ENDPOINT": self._CREDENTIAL_URL},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="INVENTORY_ENDPOINT",
        )
        assert len(rows) == 1, "the PATCH must have persisted exactly one row"
        parsed = urlparse(rows[0].value)
        assert is_stored_ciphertext(parsed.password)
        assert stored_plaintext(parsed.password) == self._CREDENTIAL_PASSWORD
        assert parsed.username == "inv-user"
        assert parsed.hostname == "inventory.internal"
        assert str(extensions_settings.INVENTORY_ENDPOINT).rstrip("/") == (
            self._CREDENTIAL_URL
        )

    async def test_nested_credential_url_patch_encrypts_the_password(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt the nested ``PMM__ENDPOINT`` leaf, which reaches the walker as text."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM__ENDPOINT": self._CREDENTIAL_URL},
        )
        assert response.status_code == status.HTTP_200_OK

        # The row is stored under the canonical field spelling, not the
        # submitted one, so the lookup uses the lowercase leaf name.
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM__endpoint"
        )
        assert len(rows) == 1, "the PATCH must have persisted exactly one row"
        assert (
            stored_plaintext(urlparse(rows[0].value).password)
            == self._CREDENTIAL_PASSWORD
        )
        assert str(settings.PMM.endpoint).rstrip("/") == self._CREDENTIAL_URL

    async def test_whole_object_patch_encrypts_both_leaf_kinds(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt the URL password and the ``api_key`` sibling in one stored row."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM": {"endpoint": self._CREDENTIAL_URL, "api_key": PMM_API_KEY}},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM"
        )
        assert len(rows) == 1, "the PATCH must have persisted exactly one row"
        stored = rows[0].value
        assert stored_plaintext(urlparse(stored["endpoint"]).password) == (
            self._CREDENTIAL_PASSWORD
        )
        assert stored_plaintext(stored["api_key"]) == PMM_API_KEY

    async def test_masked_resubmit_over_encrypted_storage_round_trips(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Restore the mask against an at-rest-encrypted value and re-encrypt it.

        Mask restoration compares the submitted URL against the *decrypted*
        snapshot, so the round trip has to survive the stored value being
        ciphertext between the two PATCHes.
        """
        first = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"INVENTORY_ENDPOINT": self._CREDENTIAL_URL},
        )
        assert first.status_code == status.HTTP_200_OK

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                "INVENTORY_ENDPOINT": (
                    "https://inv-user:****@inventory.internal:8080/api"
                )
            },
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="INVENTORY_ENDPOINT",
        )
        assert len(rows) == 1, "the resubmit must leave exactly one row"
        assert (
            stored_plaintext(urlparse(rows[0].value).password)
            == self._CREDENTIAL_PASSWORD
        )

    async def test_credential_url_read_surface_is_unchanged_by_encryption(
        self, api_admin_client: TestClient
    ) -> None:
        """Keep the API contract: still ``user:****@host``, still ``is_secret: false``.

        The at-rest change must not leak into ``is_secret``, which the settings
        DETAIL response publishes for every field, nor serve the ciphertext.
        """
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"INVENTORY_ENDPOINT": self._CREDENTIAL_URL},
        )
        assert patched.status_code == status.HTTP_200_OK

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/INVENTORY_ENDPOINT"
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert self._CREDENTIAL_PASSWORD not in payload["value"]
        assert _FERNET_TOKEN_PREFIX not in payload["value"]
        assert "****" in payload["value"]
        assert "inv-user" in payload["value"]
        assert payload["is_secret"] is False

    async def test_whole_object_patch_encrypts_the_secret_leaf(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt ``$.api_key`` of a whole-object PMM PATCH, leaving siblings plain."""
        endpoint = "https://pmm.example.com"
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM": {"endpoint": endpoint, "api_key": PMM_API_KEY}},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM"
        )
        assert is_stored_ciphertext(rows[0].value["api_key"])
        assert stored_plaintext(rows[0].value["api_key"]) == PMM_API_KEY
        assert rows[0].value["endpoint"] == endpoint

    async def test_whole_object_patch_marks_what_it_stores(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Stamp the envelope marker on the column a real PATCH writes.

        The rest of this class asserts through the envelope-agnostic accessors,
        which by design pass for a marked *and* an unmarked value — so nothing
        else at the route level notices if the write path stops marking. This is
        the one assertion here that does.
        """
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={
                "PMM": {"endpoint": "https://pmm.example.com", "api_key": PMM_API_KEY}
            },
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM"
        )
        assert marked_ciphertext(rows[0].value["api_key"]) is not None

    async def test_whole_object_patch_still_masks_the_secret_in_the_response(
        self, api_admin_client: TestClient
    ) -> None:
        """Keep the redaction contract: neither plaintext nor ciphertext is served."""
        assert (
            api_admin_client.patch(
                "/api/extensions/admin/settings/Settings",
                json={"PMM": {"api_key": PMM_API_KEY}},
            ).status_code
            == status.HTTP_200_OK
        )

        list_payload = api_admin_client.get("/api/extensions/admin/settings/").json()
        entry = _find_setting(
            list_payload, SettingClassEnum.SETTINGS.value, "PMM__api_key"
        )
        assert entry["value"] == SECRET_STR_MASK
        serialized = json_serializer(list_payload)
        assert serialized
        assert PMM_API_KEY not in serialized
        assert _FERNET_TOKEN_PREFIX not in serialized

    async def test_nested_leaf_patch_encrypts_the_whole_value(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt the row value outright when the nested leaf *is* the secret."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM__api_key": PMM_API_KEY},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM__api_key"
        )
        assert is_stored_ciphertext(rows[0].value)
        assert stored_plaintext(rows[0].value) == PMM_API_KEY

    async def test_materializer_backed_provider_patch_encrypts_routing_key(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt a polymorphic provider's secret, keeping its discriminator readable.

        ``PROVIDERS`` is materializer-backed, so the persisted value is the
        client's raw JSON rather than the coerced provider set, which is the
        case a value-driven walker cannot see.
        """
        endpoint = "https://events.pagerduty.com/v2/"
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/AlertSettings",
            json={
                "PROVIDERS": [
                    {
                        "PROVIDER": "pagerduty",
                        "api_endpoint": endpoint,
                        "routing_key": ROUTING_KEY,
                    }
                ]
            },
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=ALERT_SETTINGS_TOKEN, key="PROVIDERS"
        )
        entry = rows[0].value[0]
        assert is_stored_ciphertext(entry["routing_key"])
        assert stored_plaintext(entry["routing_key"]) == ROUTING_KEY
        assert entry["PROVIDER"] == "pagerduty"
        assert entry["api_endpoint"] == endpoint
        provider = next(iter(alert_settings.PROVIDERS))
        assert provider.routing_key.get_secret_value() == ROUTING_KEY

    @pytest.mark.usefixtures("delivery_skeleton")
    async def test_delivery_inputs_patch_encrypts_every_named_secret(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Encrypt every value of the ``dict[str, SecretStr]`` leaf, never its names."""
        endpoint = "https://elsewhere.example.com/"
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={
                _DELIVERY_INPUTS_KEY: {
                    "endpoint": endpoint,
                    "secrets": _DELIVERY_INPUTS_SECRETS,
                }
            },
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=_DELIVERY_INPUTS_KEY,
        )
        stored = rows[0].value["secrets"]
        assert sorted(stored) == sorted(_DELIVERY_INPUTS_SECRETS)
        for name, plaintext in _DELIVERY_INPUTS_SECRETS.items():
            assert is_stored_ciphertext(stored[name])
            assert stored_plaintext(stored[name]) == plaintext
        assert rows[0].value["endpoint"] == endpoint

    async def test_non_secret_field_is_stored_unencrypted(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Store a field whose annotation reaches no secret exactly as before."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"PMM": {"verify_ssl": False}},
        )
        assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM"
        )
        assert rows[0].value["verify_ssl"] is False
        assert rows[0].value["api_key"] is None

    async def test_repeated_patch_does_not_double_encrypt(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Keep one layer of ciphertext when the same secret is written twice."""
        for _ in range(2):
            assert (
                api_admin_client.patch(
                    "/api/extensions/admin/settings/Settings",
                    json={"PMM__api_key": PMM_API_KEY},
                ).status_code
                == status.HTTP_200_OK
            )

        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN, key="PMM__api_key"
        )
        assert len(rows) == 1
        assert stored_plaintext(rows[0].value) == PMM_API_KEY


@pytest.mark.asyncio
class TestExtensionsSettingsProvenance:
    """Cover ``updated_at`` / ``updated_by`` across LIST, DETAIL and PATCH."""

    async def test_patch_stamps_the_calling_admin(
        self, api_admin_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Record the calling admin and a write time on a freshly created override."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        assert response.status_code == status.HTTP_200_OK
        entry = response.json()[0]
        assert entry["updated_by"] == admin_user.username
        assert entry["updated_at"] is not None

    async def test_patch_stamp_is_timezone_aware_utc(
        self, api_admin_client: TestClient
    ) -> None:
        """Serialise ``updated_at`` with an explicit UTC offset."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        stamp = datetime.fromisoformat(response.json()[0]["updated_at"])
        assert stamp.tzinfo is not None
        assert stamp.utcoffset() == timedelta(0)

    async def test_repeated_patch_of_the_same_value_restamps(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        admin_user: CasdoorUser,
    ) -> None:
        """Re-stamp a row whose submitted value equals the stored one.

        Submitting the stored value leaves ``value`` and ``is_active`` untouched,
        so the row is not dirty and the column's ``onupdate`` never fires. Only
        an explicit assignment forces the UPDATE.
        """
        stale = utc_now() - timedelta(days=1)
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=10,
            updated_at=stale,
            updated_by="someone-else",
        )

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        assert response.status_code == status.HTTP_200_OK
        entry = response.json()[0]
        assert entry["updated_by"] == admin_user.username
        assert datetime.fromisoformat(entry["updated_at"]) > stale

    async def test_consecutive_patches_by_the_same_admin_are_accepted(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        admin_user: CasdoorUser,
    ) -> None:
        """Accept a second PATCH whose actor restamp equals the stored actor.

        The second write changes ``value`` while assigning ``updated_by`` the
        value it already holds, which the model's actor-stamp guard must still
        count as a restamp.
        """
        values = (10, 20)
        for value in values:
            response = api_admin_client.patch(
                "/api/extensions/admin/settings/ExtensionsSettings",
                json={"SYNC_REFRESH_TIME": value},
            )
            assert response.status_code == status.HTTP_200_OK

        rows = await SettingsOverrideManager.list(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
        )
        assert len(rows) == 1
        assert rows[0].value == values[-1]
        assert rows[0].updated_by == admin_user.username

    @pytest.mark.parametrize(
        "stored_keys",
        [("sync_refresh_time",), ("sync_refresh_time", "SYNC_REFRESH_TIME")],
    )
    async def test_patch_collapsing_a_legacy_spelling_restamps(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        admin_user: CasdoorUser,
        stored_keys: tuple[str, ...],
    ) -> None:
        """Rename or delete legacy-spelled rows and stamp the survivor with the caller.

        Renaming a row changes the tracked ``key`` column, so the collapse must
        restamp ``updated_by`` in the same flush to pass the actor-stamp guard.
        """
        for key in stored_keys:
            await insert_override_row(
                override_session,
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key=key,
                value=5,
                updated_by="someone-else",
            )

        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        assert response.status_code == status.HTTP_200_OK
        override_session.expunge_all()
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert [(row.key, row.value, row.updated_by) for row in rows] == [
            ("SYNC_REFRESH_TIME", 10, admin_user.username)
        ]

    async def test_detail_reports_the_stamp_the_patch_returned(
        self, api_admin_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Serve the same stamp from DETAIL that the PATCH response carried."""
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["has_override"] is True
        assert body["updated_by"] == admin_user.username
        assert body["updated_at"] == patched.json()[0]["updated_at"]

    async def test_list_reports_the_stamp_the_patch_returned(
        self, api_admin_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Serve the same stamp from LIST that the PATCH response carried."""
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )

        response = api_admin_client.get("/api/extensions/admin/settings/")

        entry = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SYNC_REFRESH_TIME",
        )
        assert entry["updated_by"] == admin_user.username
        assert entry["updated_at"] == patched.json()[0]["updated_at"]

    async def test_batch_patch_shares_one_stamp(
        self, api_admin_client: TestClient
    ) -> None:
        """Stamp every key of one atomic batch with a single timestamp."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10, "ARTIFACT_DOWNLOAD_TTL": 900},
        )

        assert response.status_code == status.HTTP_200_OK
        stamps = {entry["updated_at"] for entry in response.json()}
        assert len(stamps) == 1

    async def test_key_without_an_override_reports_no_provenance(
        self, api_admin_client: TestClient
    ) -> None:
        """Report both fields as ``null`` for a key carrying no override row."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )

        body = response.json()
        assert body["has_override"] is False
        assert body["updated_at"] is None
        assert body["updated_by"] is None

    async def test_list_carries_null_provenance_without_an_override(
        self, api_admin_client: TestClient
    ) -> None:
        """Carry both fields as ``null`` on a LIST entry with no override row.

        The fields are present rather than absent, so a consumer reads the same
        shape from LIST as from DETAIL.
        """
        response = api_admin_client.get("/api/extensions/admin/settings/")

        entry = _find_setting(
            response.json(),
            SettingClassEnum.EXTENSIONS_SETTINGS.value,
            "SYNC_REFRESH_TIME",
        )
        assert entry["has_override"] is False
        assert entry["updated_at"] is None
        assert entry["updated_by"] is None

    async def test_legacy_row_falls_back_to_created_at(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Report ``created_at`` for a row written before explicit stamping."""
        row = await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=10,
        )
        assert row.updated_at is None

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )

        body = response.json()
        assert body["updated_by"] is None
        assert datetime.fromisoformat(body["updated_at"]) == make_datetime_utc(
            row.created_at
        )

    async def test_inactive_row_reports_no_provenance(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Ignore an inactive row: no override is reported and no stamp travels.

        An inactive row does not affect the served value, which falls back to the
        declared default, so reporting provenance for one would tell the UI a
        field is overridden while showing it that default.
        """
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=10,
            is_active=False,
            updated_at=utc_now(),
            updated_by="someone-else",
        )

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )

        body = response.json()
        assert body["has_override"] is False
        assert body["updated_at"] is None
        assert body["updated_by"] is None

    async def test_nested_parent_reports_the_leaf_stamp(
        self, api_admin_client: TestClient, admin_user: CasdoorUser
    ) -> None:
        """Promote a nested leaf's provenance to every canonical prefix of its chain."""
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"APP_DRAIN__stale_task_ttl": 7200},
        )
        assert patched.status_code == status.HTTP_200_OK

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/APP_DRAIN"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["has_override"] is True
        assert body["updated_by"] == admin_user.username
        assert body["updated_at"] == patched.json()[0]["updated_at"]

    async def test_delete_clears_the_provenance(
        self, api_admin_client: TestClient
    ) -> None:
        """Clear both fields once the override row is hard-deleted."""
        patched = api_admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 10},
        )
        assert patched.status_code == status.HTTP_200_OK
        assert patched.json()[0]["has_override"] is True

        deleted = api_admin_client.delete(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )
        assert deleted.status_code == status.HTTP_204_NO_CONTENT

        response = api_admin_client.get(
            "/api/extensions/admin/settings/ExtensionsSettings/SYNC_REFRESH_TIME"
        )

        body = response.json()
        assert body["has_override"] is False
        assert body["updated_at"] is None
        assert body["updated_by"] is None


def _class_keys(payload: dict[str, Any], setting_class: str) -> set[str]:
    """Return every key the LIST payload carries for one settings class."""
    for group in payload["groups"]:
        if group["setting_class"] == setting_class:
            return {entry["key"] for entry in group["settings"]}
    raise AssertionError(f"setting class {setting_class} not in payload")


@pytest.mark.asyncio
class TestSettingsComputedKeys:
    """Cover the computed keys ``Settings`` exposes on the admin settings API."""

    async def test_list_advertises_the_resolved_token(
        self, api_admin_client: TestClient
    ) -> None:
        """Serve the computed ``EXTENSIONS_INTERNAL_TOKEN``, never its excluded input."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        keys = _class_keys(response.json(), SettingClassEnum.SETTINGS.value)
        assert "EXTENSIONS_INTERNAL_TOKEN" in keys
        assert "EXTENSIONS_INTERNAL_TOKEN_INPUT" not in keys

    async def test_list_advertises_base_dir(self, api_admin_client: TestClient) -> None:
        """Serve ``BASE_DIR`` too: every computed key is part of the public surface."""
        response = api_admin_client.get("/api/extensions/admin/settings/")
        assert "BASE_DIR" in _class_keys(
            response.json(), SettingClassEnum.SETTINGS.value
        )

    async def test_detail_masks_the_token_and_refuses_overrides(
        self, api_admin_client: TestClient
    ) -> None:
        """Return the computed token masked and classified NOT_OVERRIDABLE."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/Settings/EXTENSIONS_INTERNAL_TOKEN"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["value"] == SECRET_STR_MASK
        assert body["is_secret"] is True
        assert body["reload"] == ReloadClassification.NOT_OVERRIDABLE.value

    async def test_detail_serialises_base_dir(
        self, api_admin_client: TestClient
    ) -> None:
        """Serialise a computed non-secret value rather than raising on lookup."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/Settings/BASE_DIR"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["value"] == str(settings.BASE_DIR)

    async def test_detail_on_the_excluded_input_returns_404(
        self, api_admin_client: TestClient
    ) -> None:
        """Reject the excluded input key; it is not part of the public surface."""
        response = api_admin_client.get(
            "/api/extensions/admin/settings/Settings/EXTENSIONS_INTERNAL_TOKEN_INPUT"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_patch_of_a_computed_key_returns_422(
        self, api_admin_client: TestClient
    ) -> None:
        """Refuse a PATCH of a computed key as not-overridable, not as unknown."""
        response = api_admin_client.patch(
            "/api/extensions/admin/settings/Settings",
            json={"EXTENSIONS_INTERNAL_TOKEN": "rotated"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        detail = response.json()["detail"]
        assert any(
            entry["type"] == ReloadClassification.NOT_OVERRIDABLE.value
            for entry in detail
        )

    async def test_delete_of_a_computed_key_returns_409(
        self, api_admin_client: TestClient
    ) -> None:
        """Refuse a DELETE of a computed key; no override row can ever exist."""
        response = api_admin_client.delete(
            "/api/extensions/admin/settings/Settings/EXTENSIONS_INTERNAL_TOKEN"
        )
        assert response.status_code == status.HTTP_409_CONFLICT
