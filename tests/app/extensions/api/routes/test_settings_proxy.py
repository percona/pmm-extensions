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

"""Tests for the PMM Extensions level ``TasksSettings`` proxy.

``TasksSettings`` storage lives in the Tasks sub-app, so the PMM Extensions settings router
registers it as a *remote* class: the LIST appends its group server-side and the
DETAIL / PATCH / DELETE paths dispatch to the Tasks API via ``get_tasks_api``.
These tests mock that dependency and assert aggregation, dispatch, the
4xx-passthrough / 5xx-502 error split, auth parity, and the unhappy paths.

Token forwarding (the proxied request carries the caller's Bearer token) is
covered by ``tests/app/extensions/test_deps.py::TestGetTasksApi`` and not duplicated here.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, UTC
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.requests import RemoteAPI
from app.core.settings_override.constants import EXTENSIONS_SETTINGS, TASKS_SETTINGS
from app.core.utils import json_serializer
from app.extensions.deps import (
    get_current_user,
    get_session,
    get_tasks_api,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app
from tests.app.db_schema import apply_schema

TASKS_KEY = "STALENESS_THRESHOLD_SECONDS"
REMOTE_BASE = "/admin/settings"


def _tasks_setting(value: Any = 3600, *, has_override: bool = False) -> dict[str, Any]:
    """Return a Tasks-group :class:`SettingResponse` payload as the proxy sees it."""
    return {
        "setting_class": "TasksSettings",
        "key": TASKS_KEY,
        "key_path": [TASKS_KEY],
        "value": value,
        "default_value": 3600,
        "type": "int",
        "reload": "hot",
        "description": "How long before a task is considered stale.",
        "is_secret": False,
        "is_complex": False,
        "has_override": has_override,
    }


def _tasks_list(settings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a remote LIST payload (``SettingsListResponse``) for the Tasks app."""
    return {
        "groups": [
            {
                "setting_class": "TasksSettings",
                "settings": [_tasks_setting()] if settings is None else settings,
            }
        ]
    }


@pytest_asyncio.fixture(name="override_session")
async def override_session_fixture() -> AsyncIterator[AsyncSession]:
    """Provide an in-memory PMM Extensions session pre-loaded with the override table."""
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


@pytest.fixture(name="mock_tasks")
def mock_tasks_fixture() -> Iterator[AsyncMock]:
    """Override ``get_tasks_api`` with an :class:`AsyncMock` Tasks API client."""
    mock = AsyncMock(spec=RemoteAPI)
    extensions_app.dependency_overrides[get_tasks_api] = lambda: mock
    yield mock
    extensions_app.dependency_overrides.pop(get_tasks_api, None)


@pytest.fixture(name="admin_client")
def admin_client_fixture(
    admin_user: CasdoorUser, override_session: AsyncSession, mock_tasks: AsyncMock
) -> Iterator[TestClient]:
    """Yield an admin client (Bearer gate satisfied) with the Tasks API mocked."""
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture(name="non_admin_client")
def non_admin_client_fixture(
    regular_user: CasdoorUser, override_session: AsyncSession, mock_tasks: AsyncMock
) -> Iterator[TestClient]:
    """Yield a non-admin client (role gate should reject) with the Tasks API mocked."""
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


@pytest.fixture(name="cookie_admin_client")
def cookie_admin_client_fixture(
    admin_user: CasdoorUser, override_session: AsyncSession, mock_tasks: AsyncMock
) -> Iterator[TestClient]:
    """Yield a cookie-only admin client (Bearer gate intact) with the Tasks API mocked.

    ``require_bearer_for_unsafe_methods`` is deliberately left un-overridden so
    unsafe methods (PATCH / DELETE) without a Bearer header reject with 401.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: override_session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


class TestListAggregation:
    """Check that ``GET /api/extensions/admin/settings/`` aggregates the Tasks group."""

    def test_appends_tasks_group_fetched_server_side(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Return core, proxied TasksSettings, and app-owned groups in order."""
        mock_tasks.get.return_value = _tasks_list([_tasks_setting(has_override=True)])
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        classes = [g["setting_class"] for g in response.json()["groups"]]
        assert {"ExtensionsSettings", "SnippetsSettings", "AlertSettings"}.issubset(
            set(classes)
        )
        assert classes[-5:] == [
            TASKS_SETTINGS,
            "InventoryAppSettings",
            "AlertsSettings",
            "HealthReportSettings",
            "OmInventorySettings",
        ]
        mock_tasks.get.assert_awaited_once_with(f"{REMOTE_BASE}/")

    def test_list_emits_is_advanced_for_extensions_settings(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Flag advanced settings (incl. every SESSION_REFRESH leaf) and only those."""
        mock_tasks.get.return_value = _tasks_list()
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        extensions_group = next(
            g
            for g in response.json()["groups"]
            if g["setting_class"] == EXTENSIONS_SETTINGS
        )
        advanced = {s["key"]: s["is_advanced"] for s in extensions_group["settings"]}
        # Top-level advanced settings.
        assert advanced["INVENTORY_ENDPOINT"] is True
        assert advanced["TASKS_ENDPOINT"] is True
        assert advanced["FOOTER_TEMPLATE"] is True
        # Every expanded SESSION_REFRESH leaf inherits the flag.
        session_leaves = [k for k in advanced if k.startswith("SESSION_REFRESH__")]
        assert session_leaves  # the parent expands into leaves, not a single entry
        assert all(advanced[k] is True for k in session_leaves)
        # A basic setting stays False.
        assert advanced["SYNC_REFRESH_TIME"] is False

    def test_list_marks_ambient_sso_not_applicable_under_non_grafana(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Mark AMBIENT_SESSION_SSO_ENABLED not applicable under a non-Grafana provider."""
        mock_tasks.get.return_value = _tasks_list()
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        extensions_group = next(
            g
            for g in response.json()["groups"]
            if g["setting_class"] == EXTENSIONS_SETTINGS
        )
        applicable = {
            s["key"]: s["is_applicable"] for s in extensions_group["settings"]
        }
        assert applicable["AMBIENT_SESSION_SSO_ENABLED"] is False
        # Every other field stays applicable by default.
        assert applicable["SYNC_REFRESH_TIME"] is True

    def test_list_marks_ambient_sso_applicable_under_grafana(
        self, admin_client: TestClient, mock_tasks: AsyncMock, grafana_mock
    ) -> None:
        """Mark AMBIENT_SESSION_SSO_ENABLED applicable under the Grafana provider."""
        mock_tasks.get.return_value = _tasks_list()
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        extensions_group = next(
            g
            for g in response.json()["groups"]
            if g["setting_class"] == EXTENSIONS_SETTINGS
        )
        applicable = {
            s["key"]: s["is_applicable"] for s in extensions_group["settings"]
        }
        assert applicable["AMBIENT_SESSION_SSO_ENABLED"] is True

    def test_empty_tasks_group_still_renders(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A Tasks group with no settings is appended without error."""
        mock_tasks.get.return_value = _tasks_list([])
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        tasks_group = next(
            g
            for g in response.json()["groups"]
            if g["setting_class"] == "TasksSettings"
        )
        assert tasks_group["settings"] == []

    def test_upstream_http_error_fails_closed_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A failed Tasks-group fetch fails the whole LIST with 502."""
        mock_tasks.get.side_effect = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="tasks down"
        )
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "tasks down"}

    def test_upstream_oserror_fails_closed_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A connection-level failure on the Tasks-group fetch becomes a 502."""
        mock_tasks.get.side_effect = OSError("connection refused")
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "connection refused"}

    def test_upstream_shape_drift_returns_500(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream payload that fails validation surfaces as 500, not 502."""
        mock_tasks.get.return_value = {
            "groups": [{"setting_class": "TasksSettings", "settings": [{"bad": "x"}]}]
        }
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_upstream_omits_tasks_group_fails_closed_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A valid upstream payload that omits the Tasks group fails closed with 502.

        The fetch succeeds and validates, but ``_remote_list_group`` finds no
        ``TasksSettings`` group to splice in -- the LIST must not silently drop
        the remote group, so it 502s rather than returning three groups.
        """
        mock_tasks.get.return_value = {"groups": []}
        response = admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "TasksSettings" in response.json()["detail"]


class TestDispatch:
    """DETAIL / PATCH / DELETE dispatch to the Tasks API for the remote class."""

    def test_detail_dispatches_to_proxy(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """``GET .../TasksSettings/{key}`` proxies to the Tasks API and validates."""
        mock_tasks.get.return_value = _tasks_setting(has_override=True)
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["setting_class"] == TASKS_SETTINGS
        assert body["key"] == TASKS_KEY
        mock_tasks.get.assert_awaited_once_with(
            f"{REMOTE_BASE}/TasksSettings/{TASKS_KEY}"
        )

    def test_detail_defaults_is_advanced_when_upstream_omits(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream payload without ``is_advanced`` validates and defaults to False.

        Guards the additive contract: a Tasks app that predates the field still
        proxies cleanly rather than 500ing on a missing key.
        """
        mock_tasks.get.return_value = _tasks_setting()
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_advanced"] is False

    def test_patch_dispatches_to_proxy(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """``PATCH .../TasksSettings`` forwards the body and validates the response."""
        new_value = 7200
        mock_tasks.patch.return_value = [
            _tasks_setting(value=new_value, has_override=True)
        ]
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: new_value}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["value"] == new_value
        mock_tasks.patch.assert_awaited_once_with(
            f"{REMOTE_BASE}/TasksSettings", json={TASKS_KEY: new_value}
        )

    def test_delete_dispatches_to_proxy(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """``DELETE .../TasksSettings/{key}`` proxies and returns 204."""
        mock_tasks.delete.return_value = None
        response = admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        mock_tasks.delete.assert_awaited_once_with(
            f"{REMOTE_BASE}/TasksSettings/{TASKS_KEY}"
        )

    def test_patch_passes_through_upstream_422(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream 422 keeps its status and ``detail`` (FE inline validation)."""
        mock_tasks.patch.side_effect = HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "loc": ["body", TASKS_KEY, "greater_than"],
                    "msg": "Input should be greater than 0",
                    "type": "greater_than",
                }
            ],
        )
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 0}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["detail"][0]["msg"] == "Input should be greater than 0"

    def test_detail_unknown_key_passes_through_upstream_404(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A key the Tasks app rejects passes back its 404, with no path escape.

        FastAPI's ``{key}`` path converter never matches ``/``, so a remote-path
        escape is impossible; the proxy forwards the key verbatim and the upstream
        404 is preserved (not masked as a 502).
        """
        mock_tasks.get.side_effect = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown key"
        )
        response = admin_client.get(
            "/api/extensions/admin/settings/TasksSettings/DOES_NOT_EXIST"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json() == {"detail": "unknown key"}
        mock_tasks.get.assert_awaited_once_with(
            f"{REMOTE_BASE}/TasksSettings/DOES_NOT_EXIST"
        )

    def test_local_class_is_not_proxied(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A PATCH to a local class hits the PMM Extensions DB and never touches the Tasks API."""
        response = admin_client.patch(
            "/api/extensions/admin/settings/ExtensionsSettings",
            json={"SYNC_REFRESH_TIME": 7},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["key"] == "SYNC_REFRESH_TIME"
        mock_tasks.patch.assert_not_awaited()
        mock_tasks.get.assert_not_awaited()

    def test_detail_passes_through_upstream_400(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A non-404 upstream client error (400) is also preserved, not masked."""
        mock_tasks.get.side_effect = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bad request"
        )
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == {"detail": "bad request"}

    def test_unknown_class_is_not_proxied(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Return 404 for a core class neither local nor remote, never proxying it.

        ``Settings`` is a real core settings class but is not registered on the
        PMM Extensions router (local or remote), so ``_resolve`` rejects it
        before any dispatch. The remote branch must not swallow an unknown class.
        """
        response = admin_client.get(
            f"/api/extensions/admin/settings/Settings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_tasks.get.assert_not_awaited()


class TestAuthParity:
    """The remote paths inherit the same admin + Bearer gates as the local ones."""

    def test_non_admin_detail_forbidden(
        self, non_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A non-admin is rejected before the proxy runs."""
        response = non_admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_tasks.get.assert_not_awaited()

    def test_cookie_only_patch_unauthorized(
        self, cookie_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A cookie-only admin cannot PATCH the remote class (no Bearer -> 401)."""
        response = cookie_admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        mock_tasks.patch.assert_not_awaited()

    def test_cookie_only_delete_unauthorized(
        self, cookie_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A cookie-only admin cannot DELETE the remote class (no Bearer -> 401)."""
        response = cookie_admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        mock_tasks.delete.assert_not_awaited()

    def test_non_admin_list_forbidden(
        self, non_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A non-admin LIST is rejected before any remote group is fetched."""
        response = non_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_tasks.get.assert_not_awaited()

    def test_non_admin_patch_forbidden(
        self, non_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A non-admin PATCH is rejected before the proxy runs."""
        response = non_admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_tasks.patch.assert_not_awaited()

    def test_non_admin_delete_forbidden(
        self, non_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A non-admin DELETE is rejected before the proxy runs."""
        response = non_admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_tasks.delete.assert_not_awaited()

    def test_cookie_only_list_allowed(
        self, cookie_admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A cookie-only admin can LIST: the Bearer gate covers mutations only.

        The safe ``GET`` carries no ``mutation_deps``, so the absent Bearer must
        not block a read -- guards against over-gating the list behind the
        unsafe-method Bearer requirement.
        """
        mock_tasks.get.return_value = _tasks_list()
        response = cookie_admin_client.get("/api/extensions/admin/settings/")
        assert response.status_code == status.HTTP_200_OK
        classes = [g["setting_class"] for g in response.json()["groups"]]
        assert "TasksSettings" in classes


class TestProxyErrorSplit:
    """DETAIL / PATCH / DELETE route upstream failures through the 4xx/5xx split.

    Existing tests cover the split only on the LIST path (``_remote_list_group``);
    these drive the shared ``_proxy_settings_request`` dispatcher, where an
    upstream server error (>= 500) or connection ``OSError`` becomes a 502 while
    a client error (< 500) is re-raised unchanged.
    """

    def test_detail_upstream_5xx_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream 5xx on a DETAIL read fails the proxy with 502."""
        mock_tasks.get.side_effect = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tasks down"
        )
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "tasks down"}

    def test_detail_upstream_oserror_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A connection-level failure on a DETAIL read becomes a 502."""
        mock_tasks.get.side_effect = OSError("conn refused")
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "conn refused"}

    def test_patch_upstream_5xx_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream 5xx on a PATCH fails the proxy with 502."""
        mock_tasks.patch.side_effect = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom"
        )
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "boom"}

    def test_patch_upstream_oserror_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A connection-level failure on a PATCH becomes a 502."""
        mock_tasks.patch.side_effect = OSError("conn refused")
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "conn refused"}

    def test_delete_upstream_5xx_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream 5xx on a DELETE fails the proxy with 502."""
        mock_tasks.delete.side_effect = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom"
        )
        response = admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "boom"}

    def test_delete_upstream_oserror_becomes_502(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A connection-level failure on a DELETE becomes a 502."""
        mock_tasks.delete.side_effect = OSError("conn refused")
        response = admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "conn refused"}

    def test_delete_passes_through_upstream_409(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """An upstream 409 (NOT_OVERRIDABLE) on DELETE keeps its status + detail.

        DELETE is the only verb that can surface a NOT_OVERRIDABLE 409 from the
        Tasks app; the proxy must preserve it (< 500) rather than mask it as 502
        so the UI shows the real reason the reset was refused.
        """
        mock_tasks.delete.side_effect = HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="cannot be overridden"
        )
        response = admin_client.delete(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json() == {"detail": "cannot be overridden"}


class TestProxyResponseValidation:
    """Upstream contract drift surfaces as 500 (a real bug), not 502 or a crash.

    A 502 means "the upstream is unavailable"; a malformed-but-200 response is a
    contract violation between co-deployed services, so it must surface as a 500
    rather than be mistaken for an availability blip.
    """

    def test_detail_shape_drift_returns_500(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A DETAIL payload that fails ``SettingResponse`` validation is a 500."""
        mock_tasks.get.return_value = {"bad": "x"}
        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_patch_non_list_payload_returns_500(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A PATCH response that is not a list (here ``None``) surfaces as 500."""
        mock_tasks.patch.return_value = None
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        mock_tasks.patch.assert_awaited_once()

    def test_patch_item_shape_drift_returns_500(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """A PATCH list whose items fail validation surfaces as 500."""
        mock_tasks.patch.return_value = [{"bad": "x"}]
        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestRemoteProvenance:
    """Cover a remote class, whose provenance is stamped upstream and forwarded back."""

    def test_patch_returns_the_upstream_stamp(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Return the actor the owning sub-app recorded, not one PMM Extensions invents.

        The Tasks client carries the caller's own bearer token, so the Tasks
        sub-app resolves the same principal and stamps the row locally.
        """
        upstream = _tasks_setting(value=7200, has_override=True)
        upstream["updated_at"] = "2026-09-04T12:00:00Z"
        upstream["updated_by"] = "remote-admin"
        mock_tasks.patch.return_value = [upstream]

        response = admin_client.patch(
            "/api/extensions/admin/settings/TasksSettings", json={TASKS_KEY: 7200}
        )

        assert response.status_code == status.HTTP_200_OK
        entry = response.json()[0]
        assert entry["updated_by"] == "remote-admin"
        assert datetime.fromisoformat(entry["updated_at"]) == datetime(
            2026, 9, 4, 12, 0, tzinfo=UTC
        )

    def test_detail_defaults_provenance_when_upstream_omits(
        self, admin_client: TestClient, mock_tasks: AsyncMock
    ) -> None:
        """Default both fields to ``null`` for an upstream that predates them.

        Guards the mixed-version case: a newer PMM Extensions proxying an older Tasks
        sub-app must keep working rather than 500 on the missing keys.
        """
        mock_tasks.get.return_value = _tasks_setting(has_override=True)

        response = admin_client.get(
            f"/api/extensions/admin/settings/TasksSettings/{TASKS_KEY}"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["updated_at"] is None
        assert body["updated_by"] is None
