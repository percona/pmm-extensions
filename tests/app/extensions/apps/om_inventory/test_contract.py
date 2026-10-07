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

"""Smoke-test the API-first surface of the Operations Inventory app.

A ``BaseApp`` exposes a declared ``api_router`` rather than the derived task
contract, so this mounts that router behind the production auth guard and asserts it
answers. Deliberately only ``GET /schema``, which needs no database: what the estate
routes serve is tested against a real session in test_estate_api.py.
"""

from fastapi import APIRouter, FastAPI, status
from fastapi.testclient import TestClient

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.apps.om_inventory.app import app as om_inventory_app
from app.extensions.deps import get_current_user, IsApiAuthenticated
from tests.app.extensions.apps.om_inventory.conftest import BASE


def _client(user: CasdoorUser) -> TestClient:
    """Mount the app's API router behind the production auth guard."""
    apps_router = APIRouter(prefix="/apps")
    apps_router.include_router(
        om_inventory_app.api_router, prefix=om_inventory_app.uri_path
    )
    api_router = APIRouter(prefix="/api", dependencies=[IsApiAuthenticated])
    api_router.include_router(apps_router)
    fastapi_app = FastAPI()
    fastapi_app.include_router(api_router)
    fastapi_app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(fastapi_app, raise_server_exceptions=False)


def test_schema_200(regular_user: CasdoorUser) -> None:
    """Serve the plugin schema at ``GET /schema``."""
    response = _client(regular_user).get(f"{BASE}/schema")

    assert response.status_code == status.HTTP_200_OK


def test_declares_no_custom_ui() -> None:
    """Assert ``custom_ui`` is False, since this app ships no bespoke React UI.

    Unlike ``atw``/``topology``, which register one, this app has no ``react_route``
    and no component of its own — ``sidebar=False`` already says there is nothing to
    navigate to. ``custom_ui=True`` here would claim a UI that does not exist.
    """
    assert om_inventory_app.custom_ui is False


# The scaffold's ``test_list_200`` was removed rather than repaired. It asserted a
# ``GET /`` list route this app has never had — it declares a purpose-built router
# instead of the derived task contract — so it had been failing since the app was
# written, asserting a shape nobody intended. What it meant to prove, that the router
# is mounted and reachable behind the auth guard, is proved by ``test_schema_200``
# above without needing a database; what the routes actually serve is pinned in
# ``test_estate_api.py``.
