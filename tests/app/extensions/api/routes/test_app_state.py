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

"""Tests for the admin app-state API at ``/api/admin/apps``."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy_celery_beat import IntervalSchedule
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel import SQLModel

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.celery.crud import BasePeriodicTaskManager
from app.core.celery.deps import get_session as get_celery_beat_session
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.extensions.apps.framework.registry import get_app_registry
from app.extensions.crud import AppRunningTaskManager, AppStateManager
from app.extensions.deps import (
    get_current_user,
    get_session,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app
from app.extensions.models import (
    AppLifecycleEnum,
    AppRunningTask,
    AppState,
    ExtensionsAppPeriodicTask,
)
from tests.app.db_schema import apply_schema

SNIPPETS_TASK = "extensions__sync_snippets"


async def _seed_periodic_task(
    session: AsyncSession, name: str, *, enabled: bool
) -> PeriodicTask:
    """Create a celery-beat ``PeriodicTask`` row with its interval schedule."""
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    session.add(schedule)
    await session.flush()
    task = PeriodicTask(
        name=name,
        task="app.extensions.snippets.celery.sync_snippets",
        enabled=enabled,
        schedule_model=schedule,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest_asyncio.fixture(name="override_session")
async def override_session_fixture() -> AsyncIterator[AsyncSession]:
    """Provide an in-memory SQLite PMM Extensions session pre-loaded with all tables."""
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
    admin_user: CasdoorUser,
    override_session: AsyncSession,
    celery_beat_session: AsyncSession,
) -> Iterator[TestClient]:
    """Yield an admin-authenticated client with the Bearer gate satisfied.

    Overrides the celery-beat session too, so the toggle's periodic-task gate
    runs against the in-memory beat DB instead of the real scheduler database.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[get_celery_beat_session] = lambda: (
        celery_beat_session
    )
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_non_admin_client")
def api_non_admin_client_fixture(
    regular_user: CasdoorUser, override_session: AsyncSession
) -> Iterator[TestClient]:
    """Yield a non-admin client with the in-memory PMM Extensions session."""
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_admin_cookie_client")
def api_admin_cookie_client_fixture(
    admin_user: CasdoorUser, override_session: AsyncSession
) -> Iterator[TestClient]:
    """Yield a cookie-authenticated admin with the Bearer gate left intact."""
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="api_unauthenticated_client")
def api_unauthenticated_client_fixture(
    override_session: AsyncSession,
) -> Iterator[TestClient]:
    """Yield an unauthenticated client — admin calls should 401 (JSON)."""
    extensions_app.dependency_overrides = {}
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.mark.asyncio
class TestListApps:
    """Tests for ``GET /api/admin/apps/``."""

    async def test_lists_every_configured_plugin(
        self, api_admin_client: TestClient
    ) -> None:
        """Return one entry per registered app (parents plus children) with the expected shape."""
        response = api_admin_client.get("/api/admin/apps/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == len(get_app_registry().keys())
        entry = payload[0]
        assert set(entry) == {
            "app_key",
            "name",
            "enabled",
            "lifecycle_state",
            "toggleable",
            "uri_path",
            "css_class",
            "sidebar",
            "has_api_router",
        }

    async def test_child_app_is_not_toggleable(
        self, api_admin_client: TestClient
    ) -> None:
        """Return a child app marked non-toggleable — its state is managed by the parent."""
        response = api_admin_client.get("/api/admin/apps/")
        entries = {entry["app_key"]: entry for entry in response.json()}
        assert entries["mysql_backups/restore"]["toggleable"] is False
        assert entries["backup_mongo/restore"]["toggleable"] is False

    async def test_inventory_is_not_toggleable_and_enabled(
        self, api_admin_client: TestClient
    ) -> None:
        """The protected ``inventory`` app reports enabled, non-toggleable."""
        response = api_admin_client.get("/api/admin/apps/")
        inventory = next(e for e in response.json() if e["app_key"] == "inventory")
        assert inventory["toggleable"] is False
        assert inventory["enabled"] is True
        assert inventory["lifecycle_state"] == AppLifecycleEnum.ENABLED

    async def test_reflects_seeded_state(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """A non-protected app reflects its DB ``lifecycle_state`` and derived flag."""
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        await override_session.commit()

        response = api_admin_client.get("/api/admin/apps/")
        snippets = next(e for e in response.json() if e["app_key"] == "snippets")
        assert snippets["lifecycle_state"] == AppLifecycleEnum.DISABLING
        assert snippets["enabled"] is False
        assert snippets["toggleable"] is True

    async def test_non_admin_returns_403(
        self, api_non_admin_client: TestClient
    ) -> None:
        """A non-admin user is rejected with 403."""
        response = api_non_admin_client.get("/api/admin/apps/")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_unauthenticated_returns_json_401(
        self, api_unauthenticated_client: TestClient
    ) -> None:
        """An unauthenticated GET responds with a JSON 401, not an HTML redirect."""
        response = api_unauthenticated_client.get(
            "/api/admin/apps/", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


_VALID_EDGES = [
    (AppLifecycleEnum.ENABLED, AppLifecycleEnum.DISABLING),
    (AppLifecycleEnum.DISABLED, AppLifecycleEnum.ENABLING),
    (AppLifecycleEnum.DISABLING, AppLifecycleEnum.DISABLED),
    (AppLifecycleEnum.ENABLING, AppLifecycleEnum.ENABLED),
]

_ILLEGAL_EDGES = [
    (AppLifecycleEnum.ENABLED, AppLifecycleEnum.ENABLED),
    (AppLifecycleEnum.ENABLED, AppLifecycleEnum.DISABLED),
    (AppLifecycleEnum.DISABLED, AppLifecycleEnum.ENABLED),
    (AppLifecycleEnum.DISABLING, AppLifecycleEnum.ENABLED),
    (AppLifecycleEnum.DISABLING, AppLifecycleEnum.DISABLING),
    (AppLifecycleEnum.ENABLING, AppLifecycleEnum.DISABLED),
]


@pytest.mark.asyncio
class TestUpdateAppState:
    """Tests for ``PUT /api/admin/apps/{app_key}/state``."""

    @pytest.mark.parametrize(("current", "target"), _VALID_EDGES)
    async def test_valid_edge_returns_200_and_echoes_state(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        current: AppLifecycleEnum,
        target: AppLifecycleEnum,
    ) -> None:
        """Each reachable edge updates the row and echoes the resulting state.

        An ``ENABLING`` request resolves synchronously to ``ENABLED`` (enabling
        has no warm-up to wait for), so that is the expected resulting state.
        """
        override_session.add(AppState(app_key="snippets", lifecycle_state=current))
        override_session.add(
            AppRunningTask(app_key="snippets", celery_task_id="running")
        )
        await override_session.commit()

        expected = (
            AppLifecycleEnum.ENABLED if target == AppLifecycleEnum.ENABLING else target
        )

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state", json={"lifecycle_state": target}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "app_key": "snippets",
            "enabled": expected == AppLifecycleEnum.ENABLED,
            "lifecycle_state": expected,
        }
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is expected
        )

    @pytest.mark.parametrize(("current", "target"), _ILLEGAL_EDGES)
    async def test_illegal_edge_returns_409(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        current: AppLifecycleEnum,
        target: AppLifecycleEnum,
    ) -> None:
        """Every illegal edge is rejected with 409 and leaves the row unchanged."""
        override_session.add(AppState(app_key="snippets", lifecycle_state=current))
        await override_session.commit()

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state", json={"lifecycle_state": target}
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is current
        )

    async def test_missing_row_disabling_creates_row(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """A configured app with no row (current=ENABLED) advances to DISABLING."""
        override_session.add(
            AppRunningTask(app_key="snippets", celery_task_id="running")
        )
        await override_session.commit()

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "app_key": "snippets",
            "enabled": False,
            "lifecycle_state": AppLifecycleEnum.DISABLING,
        }
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.DISABLING
        )

    async def test_scoped_child_key_resolves_through_path_param_and_is_409(
        self, api_admin_client: TestClient
    ) -> None:
        """Resolve a scoped child key through the ``:path`` route param, then return 409.

        The ``{app_key:path}`` converter captures the ``/`` so the request reaches
        the handler (a non-resolving path would 404); a child app is managed by its
        parent and cannot be toggled independently, so the toggle returns 409.
        """
        response = api_admin_client.put(
            "/api/admin/apps/mysql_backups/restore/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "mysql_backups" in response.json()["detail"]

    async def test_missing_row_disabled_target_returns_409(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """A missing row is ENABLED, so a direct ENABLED→DISABLED move is 409."""
        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLED},
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_concurrent_first_toggle_returns_idempotent_200(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A concurrent first toggle returns idempotent 200, never a 400.

        Simulates the TOCTOU race on a configured plugin: a concurrent winner
        has already committed the ``snippets`` row, but this request's
        ``get_or_create`` existence check ran before that commit. The first
        ``AppStateManager.first`` call (the transition-gate ``current_lifecycle``
        read) delegates and sees the real ``ENABLED`` row; the next call (the
        ``get_or_create`` existence check) is forced to miss; later calls (the
        post-conflict refetch) delegate again.
        """
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        override_session.add(
            AppRunningTask(app_key="snippets", celery_task_id="running")
        )
        await override_session.commit()

        original_first = AppStateManager.first.__func__
        progress = {"gate_read_done": False, "existence_check_missed": False}

        async def first_returns_none_on_existence_check(
            cls: type[AppStateManager], *args: object, **kwargs: object
        ) -> AppState | None:
            if not progress["gate_read_done"]:
                progress["gate_read_done"] = True
                return await original_first(cls, *args, **kwargs)
            if not progress["existence_check_missed"]:
                progress["existence_check_missed"] = True
                return None
            return await original_first(cls, *args, **kwargs)

        monkeypatch.setattr(
            AppStateManager,
            "first",
            classmethod(first_returns_none_on_existence_check),
        )

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "app_key": "snippets",
            "enabled": False,
            "lifecycle_state": AppLifecycleEnum.DISABLING,
        }

    async def test_protected_app_returns_409(
        self, api_admin_client: TestClient
    ) -> None:
        """Toggling the protected ``inventory`` app returns 409."""
        response = api_admin_client.put(
            "/api/admin/apps/inventory/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "protected" in response.json()["detail"].lower()

    async def test_unknown_key_returns_404(self, api_admin_client: TestClient) -> None:
        """Toggling a key that matches no configured plugin returns 404."""
        response = api_admin_client.put(
            "/api/admin/apps/nonexistent/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_missing_lifecycle_state_returns_422(
        self, api_admin_client: TestClient
    ) -> None:
        """An empty body fails ``AppStateWrite`` validation."""
        response = api_admin_client.put("/api/admin/apps/snippets/state", json={})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_invalid_lifecycle_state_returns_422(
        self, api_admin_client: TestClient
    ) -> None:
        """A value outside ``AppLifecycleEnum`` fails validation."""
        response = api_admin_client.put(
            "/api/admin/apps/snippets/state", json={"lifecycle_state": "BOGUS"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_non_admin_returns_403(
        self, api_non_admin_client: TestClient
    ) -> None:
        """A non-admin user cannot toggle app state."""
        response = api_non_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_unauthenticated_returns_401(
        self, api_unauthenticated_client: TestClient
    ) -> None:
        """An unauthenticated PUT responds with a JSON 401."""
        response = api_unauthenticated_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
            follow_redirects=False,
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")

    async def test_cookie_admin_without_bearer_returns_401(
        self, api_admin_cookie_client: TestClient
    ) -> None:
        """Cookie-authenticated admin cannot PUT without a Bearer header (CSRF defense)."""
        response = api_admin_cookie_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_cookie_admin_can_still_read(
        self, api_admin_cookie_client: TestClient
    ) -> None:
        """GET listing remains accessible via cookie auth — only PUT needs Bearer."""
        response = api_admin_cookie_client.get("/api/admin/apps/")
        assert response.status_code == status.HTTP_200_OK

    async def test_disabling_edge_gates_owned_periodic_task(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        celery_beat_session: AsyncSession,
    ) -> None:
        """The ENABLED→DISABLING edge flips an owned ``PeriodicTask.enabled`` off.

        Walking back to ENABLED through the transitional states re-enables it.
        The ``ENABLING`` request finalizes synchronously to ``ENABLED``, which is
        what re-arms the schedule.
        """
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        override_session.add(
            AppRunningTask(app_key="snippets", celery_task_id="running")
        )
        override_session.add(
            ExtensionsAppPeriodicTask(
                periodic_task_name=SNIPPETS_TASK, app_key="snippets", user_enabled=True
            )
        )
        await override_session.commit()
        await _seed_periodic_task(celery_beat_session, SNIPPETS_TASK, enabled=True)

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_200_OK
        task = await BasePeriodicTaskManager.first(
            celery_beat_session, name=SNIPPETS_TASK
        )
        assert task.enabled is False

        for target in (
            AppLifecycleEnum.DISABLED,
            AppLifecycleEnum.ENABLING,
        ):
            response = api_admin_client.put(
                "/api/admin/apps/snippets/state", json={"lifecycle_state": target}
            )
            assert response.status_code == status.HTTP_200_OK
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.ENABLED
        )
        task = await BasePeriodicTaskManager.first(
            celery_beat_session, name=SNIPPETS_TASK
        )
        assert task.enabled is True

    async def test_enabling_finalizes_to_enabled_immediately(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """A DISABLED→ENABLING request settles straight to ENABLED in one call.

        Enabling has no warm-up to wait for, so the app never lingers in
        ENABLING (which has no driver to advance it) and the response reflects
        the terminal ENABLED state.
        """
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLED)
        )
        await override_session.commit()

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.ENABLING},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "app_key": "snippets",
            "enabled": True,
            "lifecycle_state": AppLifecycleEnum.ENABLED,
        }
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.ENABLED
        )

    async def test_first_toggle_creates_row_and_gates(
        self,
        api_admin_client: TestClient,
        override_session: AsyncSession,
        celery_beat_session: AsyncSession,
    ) -> None:
        """The gate fires on the ``created=True`` branch (no pre-existing AppState row)."""
        override_session.add(
            ExtensionsAppPeriodicTask(
                periodic_task_name=SNIPPETS_TASK, app_key="snippets", user_enabled=True
            )
        )
        await override_session.commit()
        await _seed_periodic_task(celery_beat_session, SNIPPETS_TASK, enabled=True)

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )
        assert response.status_code == status.HTTP_200_OK
        task = await BasePeriodicTaskManager.first(
            celery_beat_session, name=SNIPPETS_TASK
        )
        assert task.enabled is False


@pytest.mark.asyncio
class TestDisableDrainFinalize:
    """Toggle-time cooperative-drain finalization on ``PUT .../state``."""

    async def test_disabling_idle_app_finalizes_to_disabled(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Disabling an app with no running tasks drains it to DISABLED at once."""
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        await override_session.commit()

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["lifecycle_state"] == AppLifecycleEnum.DISABLED
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    async def test_disabling_busy_app_stays_disabling(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """An app with a running task stays DISABLING until its tasks drain."""
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        override_session.add(AppRunningTask(app_key="snippets", celery_task_id="t1"))
        await override_session.commit()

        response = api_admin_client.put(
            "/api/admin/apps/snippets/state",
            json={"lifecycle_state": AppLifecycleEnum.DISABLING},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["lifecycle_state"] == AppLifecycleEnum.DISABLING
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.DISABLING
        )


@pytest.mark.asyncio
class TestForceDisableApp:
    """Tests for ``POST /api/admin/apps/{app_key}/force-disable``."""

    async def test_revokes_running_tasks_and_finalizes(
        self, api_admin_client: TestClient, override_session: AsyncSession, mocker
    ) -> None:
        """Each in-flight task is terminated, its row deleted, app left DISABLED."""
        control = mocker.patch("app.extensions.api.routes.app_state.celery.control")
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        override_session.add(AppRunningTask(app_key="snippets", celery_task_id="t1"))
        override_session.add(AppRunningTask(app_key="snippets", celery_task_id="t2"))
        await override_session.commit()

        response = api_admin_client.post("/api/admin/apps/snippets/force-disable")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["lifecycle_state"] == AppLifecycleEnum.DISABLED
        revoked = {call.args[0] for call in control.revoke.call_args_list}
        assert revoked == {"t1", "t2"}
        assert all(
            call.kwargs["terminate"] is True for call in control.revoke.call_args_list
        )
        assert (
            await AppRunningTaskManager.count(override_session, app_key="snippets") == 0
        )
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    async def test_non_disabling_app_returns_409(
        self, api_admin_client: TestClient, override_session: AsyncSession
    ) -> None:
        """Force-disable only applies mid-drain; an ENABLED app is rejected."""
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        await override_session.commit()

        response = api_admin_client.post("/api/admin/apps/snippets/force-disable")

        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_no_running_rows_skips_revoke(
        self, api_admin_client: TestClient, override_session: AsyncSession, mocker
    ) -> None:
        """A DISABLING app with no running tasks finalizes without any revoke."""
        control = mocker.patch("app.extensions.api.routes.app_state.celery.control")
        override_session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        await override_session.commit()

        response = api_admin_client.post("/api/admin/apps/snippets/force-disable")

        assert response.status_code == status.HTTP_200_OK
        control.revoke.assert_not_called()
        assert (
            await AppStateManager.current_lifecycle(override_session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    async def test_protected_app_returns_409(
        self, api_admin_client: TestClient
    ) -> None:
        """A protected app cannot be force-disabled (409 via the key dep)."""
        response = api_admin_client.post("/api/admin/apps/inventory/force-disable")
        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_unknown_key_returns_404(self, api_admin_client: TestClient) -> None:
        """An unknown app key is rejected with 404 via the key dep."""
        response = api_admin_client.post("/api/admin/apps/nonexistent/force-disable")
        assert response.status_code == status.HTTP_404_NOT_FOUND
