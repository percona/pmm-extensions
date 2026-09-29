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

"""Cover the settings HTTP surface under an active override restriction.

Drive a real ``build_settings_router`` through ``TestClient`` so the PATCH,
LIST, DETAIL and DELETE gates are exercised end to end, including the rework
that keeps a stale row deletable while its key is locked.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.api.routes import build_settings_router
from app.core.settings_override.constants import (
    EXTENSIONS_SETTINGS,
    INVENTORY_SETTINGS,
    SETTINGS,
    TASKS_SETTINGS,
)
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.registry import ReloadClassification
from app.core.utils import json_serializer
from app.extensions.config import extensions_settings, ExtensionsSettings
from app.inventory.config import inventory_settings, InventorySettings
from app.tasks.config import tasks_settings, TasksSettings
from tests.app.core.settings_override.conftest import (
    EXTENSIONS_SETTINGS_TOKEN,
    insert_override_row,
    SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)
from tests.app.db_schema import apply_schema

ANNOTATIONS_KEY = "Settings.PMM__annotations_enabled"
LOGGING_KEY = "Settings.LOGGING"
SETTINGS_URL = f"/settings/{SETTINGS}"
EXTENSIONS_URL = f"/settings/{EXTENSIONS_SETTINGS}"
TASKS_URL = f"/settings/{TASKS_SETTINGS}"
INVENTORY_URL = f"/settings/{INVENTORY_SETTINGS}"


@pytest_asyncio.fixture(name="override_session")
async def override_session_fixture() -> AsyncIterator[AsyncSession]:
    """Provide an in-memory SQLite session with the override table."""
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


@pytest.fixture(name="client")
def client_fixture(override_session: AsyncSession) -> Iterator[TestClient]:
    """Return a client whose router wires every locally-owned settings class."""

    async def get_session() -> AsyncSession:
        return override_session

    session_dep = Annotated[AsyncSession, Depends(get_session)]

    def allow_admin() -> None:
        return None

    classes = [
        (SETTINGS, Settings, settings),
        (EXTENSIONS_SETTINGS, ExtensionsSettings, extensions_settings),
        (TASKS_SETTINGS, TasksSettings, tasks_settings),
        (INVENTORY_SETTINGS, InventorySettings, inventory_settings),
    ]
    router = build_settings_router(
        classes=classes,
        session_dep=session_dep,
        admin_dep=Depends(allow_admin),
        actor_dep=Annotated[str, Depends(lambda: "test-admin")],
    )
    app = FastAPI()
    app.include_router(router, prefix="/settings")
    return TestClient(app, raise_server_exceptions=False)


def _error_types(response_json: dict) -> set[str]:
    """Return the distinct ``type`` values of a structured 422 detail payload."""
    return {entry["type"] for entry in response_json["detail"]}


def _nomad_leaf_reloads(client: TestClient) -> dict[str, str]:
    """Return the reload classification LIST reports for each ``NOMAD__`` leaf."""
    response = client.get("/settings/")
    assert response.status_code == status.HTTP_200_OK
    groups = {group["setting_class"]: group for group in response.json()["groups"]}
    return {
        field["key"]: field["reload"]
        for field in groups[TASKS_SETTINGS]["settings"]
        if field["key"].startswith("NOMAD__")
    }


class TestRemotePassThrough:
    """Cover the proxy carrying another service's lockdown refusal back verbatim."""

    def test_upstream_refusal_reaches_the_caller_unchanged(
        self, override_session: AsyncSession
    ) -> None:
        """Assert an upstream 422 keeps its status and structured detail."""
        upstream_detail = [
            {
                "loc": ["body", "NOMAD__endpoint"],
                "msg": "Setting cannot be overridden from the API.",
                "type": "not_overridable",
            }
        ]

        class _RefusingRemoteAPI:
            """Stand in for the Tasks service refusing a withheld key."""

            async def patch(self, path: str, **kwargs: object) -> None:
                """Raise the 422 the owning service's gate would return."""
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=upstream_detail,
                )

        async def get_session() -> AsyncSession:
            return override_session

        async def get_remote_api() -> _RefusingRemoteAPI:
            return _RefusingRemoteAPI()

        router = build_settings_router(
            classes=[(SETTINGS, Settings, settings)],
            session_dep=Annotated[AsyncSession, Depends(get_session)],
            admin_dep=Depends(lambda: None),
            actor_dep=Annotated[str, Depends(lambda: "test-admin")],
            remote_classes=[(TASKS_SETTINGS, "/admin/settings")],
            remote_api_dep=Annotated[_RefusingRemoteAPI, Depends(get_remote_api)],
        )
        app = FastAPI()
        app.include_router(router, prefix="/settings")
        client = TestClient(app, raise_server_exceptions=False)

        response = client.patch(TASKS_URL, json={"NOMAD__endpoint": "http://evil:4646"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["detail"] == upstream_detail


class TestPatchGate:
    """Cover the three PATCH guards under an active restriction."""

    def test_locked_top_level_key_is_rejected(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a locked top-level field refuses an override with 422."""
        restrict(LOGGING_KEY)
        response = client.patch(
            EXTENSIONS_URL, json={"INVENTORY_ENDPOINT": "https://evil.example.com"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}

    def test_locked_nested_key_is_rejected(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a locked leaf under an open parent refuses an override with 422."""
        restrict(ANNOTATIONS_KEY)
        response = client.patch(
            SETTINGS_URL, json={"PMM__endpoint": "https://evil.example.com"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}

    def test_whole_locked_parent_is_rejected(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a whole-object write to a partially open parent is refused."""
        restrict(ANNOTATIONS_KEY)
        response = client.patch(
            SETTINGS_URL, json={"PMM": {"annotations_enabled": True}}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}

    def test_locked_nested_key_under_locked_parent_is_rejected(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a leaf under a fully locked parent is refused by the parent guard."""
        restrict(LOGGING_KEY)
        response = client.patch(TASKS_URL, json={"NOMAD__endpoint": "http://evil:4646"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}

    def test_allowed_top_level_key_is_applied(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed field still accepts an override and echoes it HOT."""
        restrict(LOGGING_KEY)
        response = client.patch(SETTINGS_URL, json={"LOGGING": "INFO"})
        assert response.status_code == status.HTTP_200_OK
        applied = response.json()[0]
        assert applied["key"] == "LOGGING"
        assert applied["reload"] == ReloadClassification.HOT.value

    def test_allowed_nested_key_is_applied(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed leaf still accepts an override and echoes it HOT."""
        restrict(ANNOTATIONS_KEY)
        response = client.patch(SETTINGS_URL, json={"PMM__annotations_enabled": True})
        assert response.status_code == status.HTTP_200_OK
        applied = response.json()[0]
        assert applied["key"] == "PMM__annotations_enabled"
        assert applied["reload"] == ReloadClassification.HOT.value

    def test_inventory_router_exposes_nothing(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert the third service, which declares no HOT field, refuses writes."""
        restrict(LOGGING_KEY)
        response = client.patch(INVENTORY_URL, json={"UVICORN_PORT": 9999})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}


class TestSettingsOverrideSelfLockdown:
    """Refuse writes that would let the override layer rewrite its own config.

    ``SETTINGS_OVERRIDE`` is unmarked on purpose: marking it ``NESTED_ONLY``
    would make its leaves reachable and let an operator rewrite
    ``ALLOWED_KEYS`` from the database, lifting their own restriction.
    """

    @pytest.mark.parametrize(
        "body",
        [
            {"SETTINGS_OVERRIDE__REFRESH_INTERVAL": 60},
            {"SETTINGS_OVERRIDE__REFRESHER_ENABLED": False},
            {"SETTINGS_OVERRIDE__ALLOWED_KEYS": ["Settings.LOGGING"]},
        ],
    )
    def test_each_leaf_patch_is_rejected(
        self, client: TestClient, body: dict[str, object]
    ) -> None:
        """Assert a PATCH of each settings-override leaf answers 422."""
        response = client.patch(SETTINGS_URL, json=body)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}

    def test_whole_parent_patch_is_rejected(self, client: TestClient) -> None:
        """Assert a whole-object write of the settings-override block is refused."""
        response = client.patch(
            SETTINGS_URL,
            json={
                "SETTINGS_OVERRIDE": {
                    "REFRESH_INTERVAL": 60,
                    "REFRESHER_ENABLED": False,
                    "ALLOWED_KEYS": ["Settings.LOGGING"],
                }
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}


class TestPatchUnrestricted:
    """Assert the default restriction leaves every PATCH status unchanged."""

    def test_top_level_key_is_applied(self, client: TestClient) -> None:
        """Assert an unrestricted deployment still accepts the locked-down key."""
        response = client.patch(
            EXTENSIONS_URL, json={"INVENTORY_ENDPOINT": "https://inventory.example.com"}
        )
        assert response.status_code == status.HTTP_200_OK

    def test_nested_key_is_applied(self, client: TestClient) -> None:
        """Assert an unrestricted deployment still accepts the locked-down leaf."""
        response = client.patch(
            SETTINGS_URL, json={"PMM__endpoint": "https://pmm.example.com"}
        )
        assert response.status_code == status.HTTP_200_OK


class TestReporting:
    """Cover what LIST and DETAIL report while the restriction is active."""

    def test_detail_reports_locked_leaf_as_not_overridable(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert DETAIL reports a locked leaf as not overridable."""
        restrict(ANNOTATIONS_KEY)
        response = client.get(f"{SETTINGS_URL}/PMM__endpoint")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["reload"] == (ReloadClassification.NOT_OVERRIDABLE.value)

    def test_detail_reports_allowed_leaf_as_hot(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert DETAIL still reports an allowed leaf as HOT."""
        restrict(ANNOTATIONS_KEY)
        response = client.get(f"{SETTINGS_URL}/PMM__annotations_enabled")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["reload"] == ReloadClassification.HOT.value

    def test_list_reports_the_gated_classification(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert LIST reports locked and allowed fields with their gated reload."""
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        response = client.get("/settings/")
        assert response.status_code == status.HTTP_200_OK
        groups = {group["setting_class"]: group for group in response.json()["groups"]}
        reloads = {
            field["key"]: field["reload"]
            for field in groups[EXTENSIONS_SETTINGS]["settings"]
        }
        assert reloads["INVENTORY_ENDPOINT"] == (
            ReloadClassification.NOT_OVERRIDABLE.value
        )
        assert reloads["SYNC_REFRESH_TIME"] == ReloadClassification.HOT.value

    def test_list_still_enumerates_leaves_of_a_fully_locked_parent(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a withheld parent's leaves stay listed, each reported read-only."""
        unrestricted = set(_nomad_leaf_reloads(client))
        assert unrestricted

        restrict(ANNOTATIONS_KEY)
        restricted = _nomad_leaf_reloads(client)
        assert set(restricted) == unrestricted
        assert set(restricted.values()) == {ReloadClassification.NOT_OVERRIDABLE.value}


class TestDeleteGate:
    """Cover the delete rework: stale rows stay deletable, absent rows 409."""

    @pytest.mark.asyncio
    async def test_locked_key_with_row_is_deleted(
        self,
        client: TestClient,
        override_session: AsyncSession,
        restrict: Callable[..., None],
    ) -> None:
        """Assert a stale row for a now-locked key is removable."""
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="INVENTORY_ENDPOINT",
            value="https://stale.example.com",
            is_active=True,
        )
        restrict(LOGGING_KEY)
        response = client.delete(f"{EXTENSIONS_URL}/INVENTORY_ENDPOINT")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="INVENTORY_ENDPOINT",
            )
            == 0
        )

    def test_locked_key_without_row_conflicts(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert deleting a locked key with nothing to delete reports a conflict."""
        restrict(LOGGING_KEY)
        response = client.delete(f"{EXTENSIONS_URL}/INVENTORY_ENDPOINT")
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_statically_locked_key_with_row_still_conflicts(
        self,
        client: TestClient,
        restrict: Callable[..., None],
    ) -> None:
        """Assert an explicitly not-overridable field keeps its 409 answer."""
        restrict("ExtensionsSettings.DIAGNOSTICS_DELIVERY")
        response = client.delete(f"{EXTENSIONS_URL}/DIAGNOSTICS_DELIVERY")
        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_row_under_fully_locked_parent_is_deleted(
        self,
        client: TestClient,
        override_session: AsyncSession,
        restrict: Callable[..., None],
    ) -> None:
        """Assert a stale leaf row survives its parent becoming unaddressable."""
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__timeout",
            value=30,
            is_active=True,
        )
        restrict(LOGGING_KEY)
        response = client.delete(f"{TASKS_URL}/NOMAD__timeout")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=TASKS_SETTINGS_TOKEN,
                key="NOMAD__timeout",
            )
            == 0
        )

    def test_unrestricted_delete_of_missing_row_is_idempotent(
        self, client: TestClient
    ) -> None:
        """Assert the default keeps DELETE idempotent for an overridable key."""
        response = client.delete(f"{EXTENSIONS_URL}/INVENTORY_ENDPOINT")
        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.asyncio
    async def test_fully_locked_parent_with_row_is_deleted(
        self,
        client: TestClient,
        override_session: AsyncSession,
        restrict: Callable[..., None],
    ) -> None:
        """Assert a whole-parent row survives every leaf beneath it being withheld."""
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD",
            value={"timeout": 30},
            is_active=True,
        )
        restrict(LOGGING_KEY)
        response = client.delete(f"{TASKS_URL}/NOMAD")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=TASKS_SETTINGS_TOKEN,
                key="NOMAD",
            )
            == 0
        )

    def test_fully_locked_parent_without_row_conflicts(
        self, client: TestClient, restrict: Callable[..., None]
    ) -> None:
        """Assert a withheld parent with nothing to remove reports a conflict."""
        restrict(LOGGING_KEY)
        response = client.delete(f"{TASKS_URL}/NOMAD")
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_unrestricted_whole_parent_delete_is_rejected(
        self, client: TestClient
    ) -> None:
        """Assert the default keeps whole-parent deletion a 422, not a conflict."""
        response = client.delete(f"{TASKS_URL}/NOMAD")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert _error_types(response.json()) == {"not_overridable"}


class TestLegacyCasedOverrideRows:
    """Cover DELETE and PATCH resolving rows whose stored key is not canonical.

    The API never writes a non-canonical key; these rows are seeded directly
    to stand in for a hand-written or historically re-cased override.
    """

    _CANONICAL_NESTED = "NOMAD__timeout"
    _LEGACY_NESTED = "nomad__TIMEOUT"
    _CANONICAL_PMM = "PMM__endpoint"
    _LEGACY_PMM = "pmm__ENDPOINT"
    _CANONICAL_TOP = "INVENTORY_ENDPOINT"
    _LEGACY_TOP = "inventory_endpoint"

    @pytest.mark.asyncio
    async def test_delete_removes_legacy_cased_row(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert DELETE of the canonical key removes a mixed-case stored row."""
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key=self._LEGACY_NESTED,
            value=30,
            is_active=True,
        )
        response = client.delete(f"{TASKS_URL}/{self._CANONICAL_NESTED}")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=TASKS_SETTINGS_TOKEN,
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_delete_legacy_row_under_allowlist_is_not_conflict(
        self,
        client: TestClient,
        override_session: AsyncSession,
        restrict: Callable[..., None],
    ) -> None:
        """Assert a withheld field's legacy row is still found and deleted."""
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key=self._LEGACY_NESTED,
            value=30,
            is_active=True,
        )
        restrict(LOGGING_KEY)
        response = client.delete(f"{TASKS_URL}/{self._CANONICAL_NESTED}")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=TASKS_SETTINGS_TOKEN,
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_delete_removes_legacy_and_canonical_duplicates(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert DELETE of the canonical key removes every matching stored row."""
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key=self._LEGACY_NESTED,
            value=30,
            is_active=True,
        )
        await insert_override_row(
            override_session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key=self._CANONICAL_NESTED,
            value=45,
            is_active=True,
        )
        response = client.delete(f"{TASKS_URL}/{self._CANONICAL_NESTED}")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=TASKS_SETTINGS_TOKEN,
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_delete_removes_legacy_cased_top_level_row(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert DELETE of a top-level key removes a mixed-case stored row.

        Mixed-case stored keys stay visible to DELETE now that the lookup moved
        from SQL into Python.
        """
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=self._LEGACY_TOP,
            value="https://stale.example.com",
            is_active=True,
        )
        response = client.delete(f"{EXTENSIONS_URL}/{self._CANONICAL_TOP}")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_delete_legacy_top_level_under_allowlist_is_not_conflict(
        self,
        client: TestClient,
        override_session: AsyncSession,
        restrict: Callable[..., None],
    ) -> None:
        """Assert a withheld top-level legacy row is still found and deleted."""
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=self._LEGACY_TOP,
            value="https://stale.example.com",
            is_active=True,
        )
        restrict(LOGGING_KEY)
        response = client.delete(f"{EXTENSIONS_URL}/{self._CANONICAL_TOP}")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert (
            await SettingsOverrideManager.count(
                override_session,
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
            )
            == 0
        )

    @pytest.mark.asyncio
    async def test_patch_updates_legacy_row_instead_of_duplicating(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert PATCH heals a legacy nested key to the canonical spelling."""
        await insert_override_row(
            override_session,
            setting_class=SETTINGS_TOKEN,
            key=self._LEGACY_PMM,
            value="https://stale.example.com",
            is_active=True,
        )
        new_value = "https://pmm.example.com"
        response = client.patch(SETTINGS_URL, json={self._CANONICAL_PMM: new_value})
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN
        )
        assert len(rows) == 1
        assert rows[0].key == self._CANONICAL_PMM
        assert rows[0].value == new_value
        assert rows[0].is_active is True

    @pytest.mark.asyncio
    async def test_patch_updates_legacy_and_canonical_duplicates(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert PATCH collapses duplicate case-variants to one canonical row."""
        await insert_override_row(
            override_session,
            setting_class=SETTINGS_TOKEN,
            key=self._LEGACY_PMM,
            value="https://legacy.example.com",
            is_active=True,
        )
        await insert_override_row(
            override_session,
            setting_class=SETTINGS_TOKEN,
            key=self._CANONICAL_PMM,
            value="https://canonical.example.com",
            is_active=True,
        )
        new_value = "https://pmm.example.com"
        response = client.patch(SETTINGS_URL, json={self._CANONICAL_PMM: new_value})
        assert response.status_code == status.HTTP_200_OK
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=SETTINGS_TOKEN
        )
        assert len(rows) == 1
        assert rows[0].key == self._CANONICAL_PMM
        assert rows[0].value == new_value
        assert rows[0].is_active is True

    @pytest.mark.asyncio
    async def test_patch_updates_legacy_top_level_row_instead_of_duplicating(
        self,
        client: TestClient,
        override_session: AsyncSession,
    ) -> None:
        """Assert PATCH heals a mixed-case top-level row so the snapshot can read it.

        Leaving the legacy spelling would update a row ``_apply_top_level_row``
        never looks up, yielding a 200 that silently discards the override.
        """
        await insert_override_row(
            override_session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key=self._LEGACY_TOP,
            value="https://stale.example.com",
            is_active=True,
        )
        new_value = "https://inventory.example.com/"
        response = client.patch(EXTENSIONS_URL, json={self._CANONICAL_TOP: new_value})
        assert response.status_code == status.HTTP_200_OK
        applied = response.json()[0]
        assert applied["key"] == self._CANONICAL_TOP
        assert applied["value"] == new_value
        assert applied["has_override"] is True
        rows = await SettingsOverrideManager.list(
            override_session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
        assert len(rows) == 1
        assert rows[0].key == self._CANONICAL_TOP
        assert rows[0].value == new_value
        assert rows[0].is_active is True
        detail = client.get(f"{EXTENSIONS_URL}/{self._CANONICAL_TOP}")
        assert detail.status_code == status.HTTP_200_OK
        assert detail.json()["has_override"] is True
        assert detail.json()["value"] == new_value
