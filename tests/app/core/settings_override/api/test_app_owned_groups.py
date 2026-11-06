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

"""Unit tests for app-owned settings groups in ``build_settings_router``."""

from collections.abc import AsyncIterator, Iterator
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.api.models import SettingClassAppMetadata
from app.core.settings_override.api.routes import (
    AppOwnedClassEntry,
    build_settings_router,
    ClassEntry,
)
from app.core.utils import json_serializer
from app.extensions.apps.alerts.config import alerts_settings, AlertsSettings
from app.extensions.snippets.config import snippets_settings, SnippetsSettings
from tests.app.db_schema import apply_schema

LIST_URL = "/settings/"


async def _mock_resolve_app_metadata(
    _session: AsyncSession,
    app_key: str,
) -> SettingClassAppMetadata:
    """Return deterministic metadata for unit tests."""
    return SettingClassAppMetadata(
        app_id=app_key,
        app_display_name="Test Alerts",
        app_enabled=True,
    )


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


@pytest.fixture(name="app_owned_client")
def app_owned_client_fixture(
    override_session: AsyncSession,
) -> Iterator[TestClient]:
    """Return a TestClient wired with one core class and one app-owned class."""
    session_holder = {"session": override_session}

    async def get_session() -> AsyncSession:
        return session_holder["session"]

    session_dep = Annotated[AsyncSession, Depends(get_session)]

    def allow_admin() -> None:
        return None

    core_classes: list[ClassEntry] = [
        (
            SnippetsSettings.__name__,
            SnippetsSettings,
            snippets_settings,
        ),
    ]
    app_owned: list[AppOwnedClassEntry] = [
        AppOwnedClassEntry(
            setting_class=AlertsSettings.__name__,
            settings_cls=AlertsSettings,
            proxy=alerts_settings,
            app_key="alerts",
            service_only_keys=frozenset({"BACKUP_INTERVAL"}),
        ),
    ]
    router = build_settings_router(
        classes=core_classes,
        session_dep=session_dep,
        admin_dep=Depends(allow_admin),
        actor_dep=Annotated[str, Depends(lambda: "test-admin")],
        app_owned_classes=app_owned,
        resolve_app_metadata=_mock_resolve_app_metadata,
    )
    app = FastAPI()
    app.include_router(router, prefix="/settings")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(name="core_only_client")
def core_only_client_fixture(
    override_session: AsyncSession,
) -> Iterator[TestClient]:
    """Return a Tasks-style router with no app-owned wiring."""
    session_holder = {"session": override_session}

    async def get_session() -> AsyncSession:
        return session_holder["session"]

    session_dep = Annotated[AsyncSession, Depends(get_session)]

    def allow_admin() -> None:
        return None

    router = build_settings_router(
        classes=[
            (
                SnippetsSettings.__name__,
                SnippetsSettings,
                snippets_settings,
            ),
        ],
        session_dep=session_dep,
        admin_dep=Depends(allow_admin),
        actor_dep=Annotated[str, Depends(lambda: "test-admin")],
    )
    app = FastAPI()
    app.include_router(router, prefix="/settings")
    return TestClient(app, raise_server_exceptions=False)


class TestBuildSettingsRouterAppOwned:
    """App-owned LIST metadata and wiring validation."""

    def test_app_owned_list_group_carries_metadata(
        self, app_owned_client: TestClient
    ) -> None:
        """Populate app metadata on app-owned groups from ``resolve_app_metadata``."""
        response = app_owned_client.get(LIST_URL)
        assert response.status_code == status.HTTP_200_OK
        groups = {group["setting_class"]: group for group in response.json()["groups"]}
        assert set(groups) == {
            SnippetsSettings.__name__,
            AlertsSettings.__name__,
        }
        core = groups[SnippetsSettings.__name__]
        assert core["is_app_owned"] is False
        assert core["app_id"] is None

        alert = groups[AlertsSettings.__name__]
        assert alert["is_app_owned"] is True
        assert alert["app_id"] == "alerts"
        assert alert["app_display_name"] == "Test Alerts"
        assert alert["app_enabled"] is True

    def test_router_without_app_owned_omits_metadata(
        self, core_only_client: TestClient
    ) -> None:
        """Leave app-ownership fields at defaults when no app-owned classes are wired."""
        response = core_only_client.get(LIST_URL)
        assert response.status_code == status.HTTP_200_OK
        group = response.json()["groups"][0]
        assert group["setting_class"] == SnippetsSettings.__name__
        assert group["is_app_owned"] is False
        assert group["app_id"] is None
        assert group["app_display_name"] is None
        assert group["app_enabled"] is None

    def test_app_owned_without_resolver_raises(self) -> None:
        """Reject app-owned wiring that omits ``resolve_app_metadata``."""
        with pytest.raises(
            ValueError,
            match="resolve_app_metadata is required",
        ):
            build_settings_router(
                classes=[],
                session_dep=Annotated[AsyncSession, Depends(lambda: None)],
                admin_dep=Depends(lambda: None),
                actor_dep=Annotated[str, Depends(lambda: "test-admin")],
                app_owned_classes=[
                    AppOwnedClassEntry(
                        setting_class=AlertsSettings.__name__,
                        settings_cls=AlertsSettings,
                        proxy=alerts_settings,
                        app_key="alerts",
                    ),
                ],
            )

    def test_duplicate_core_and_app_owned_raises(self) -> None:
        """Reject the same ``setting_class`` wired as both core and app-owned."""
        with pytest.raises(ValueError, match="core class and an app-owned class"):
            build_settings_router(
                classes=[
                    (
                        AlertsSettings.__name__,
                        AlertsSettings,
                        alerts_settings,
                    ),
                ],
                session_dep=Annotated[AsyncSession, Depends(lambda: None)],
                admin_dep=Depends(lambda: None),
                actor_dep=Annotated[str, Depends(lambda: "test-admin")],
                app_owned_classes=[
                    AppOwnedClassEntry(
                        setting_class=AlertsSettings.__name__,
                        settings_cls=AlertsSettings,
                        proxy=alerts_settings,
                        app_key="alerts",
                    ),
                ],
                resolve_app_metadata=_mock_resolve_app_metadata,
            )


class TestServiceOnlyKeys:
    """Withhold an app-owned class's ``service_only_keys`` from the router."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PATCH", "", {"backup_interval__every": 5}),
            ("DELETE", "/BACKUP_INTERVAL__every", None),
        ],
        ids=["patch-a-nested-leaf-in-any-casing", "delete"],
    )
    def test_writes_are_refused(
        self,
        app_owned_client: TestClient,
        method: str,
        path: str,
        body: dict[str, int] | None,
    ) -> None:
        """Refuse changing or clearing a reserved field, judged by its top level."""
        response = app_owned_client.request(
            method, f"/settings/{AlertsSettings.__name__}{path}", json=body
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert [entry["type"] for entry in response.json()["detail"]] == [
            "not_overridable"
        ]

    def test_reads_report_it_not_overridable(
        self, app_owned_client: TestClient
    ) -> None:
        """Tell a client not to offer the field, while its siblings stay editable."""
        groups = app_owned_client.get(LIST_URL).json()["groups"]
        (alerts,) = [g for g in groups if g["setting_class"] == AlertsSettings.__name__]
        reload_by_key = {row["key"]: row["reload"] for row in alerts["settings"]}
        detail = app_owned_client.get(
            f"/settings/{AlertsSettings.__name__}/BACKUP_INTERVAL__every"
        ).json()

        reserved = {
            key: reload
            for key, reload in reload_by_key.items()
            if key.startswith("BACKUP_INTERVAL")
        }
        assert reserved
        assert set(reserved.values()) == {"not_overridable"}
        assert "hot" in set(reload_by_key.values()) - set(reserved.values())
        assert detail["reload"] == "not_overridable"


class TestBuildSettingsRouterWiring:
    """Cover the factory's required-parameter contract, independent of app ownership."""

    def test_missing_actor_dep_raises(self) -> None:
        """Reject a router built without ``actor_dep`` rather than record no actor.

        The parameter is deliberately source-breaking: a sub-app that forgets the
        wiring fails where the router is built, at import, instead of serving
        PATCHes that silently store no provenance.
        """
        with pytest.raises(TypeError, match="actor_dep"):
            build_settings_router(
                classes=[],
                session_dep=Annotated[AsyncSession, Depends(lambda: None)],
                admin_dep=Depends(lambda: None),
            )
