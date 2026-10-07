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

"""Share the scaffolding every om_inventory API test module needs."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.service import SweepOutcome
from app.extensions.deps import (
    get_current_user,
    get_session,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app

#: The app's mount point, which every route in these tests hangs off.
BASE = "/api/apps/om_inventory"

#: A host's free data-directory space, as ``collect_install_readiness`` would report it.
FREE_BYTES = 107374182400

#: The Nomad client the dispatch suites probe, named as PMM registers it.
HOST = "replicaset-cluster-node00"

#: One resolved, one answered: a stubbed sweep ``terminal_status`` reads as a clean
#: ``SUCCESS``, so a run that ends in any other status was changed by something else.
CLEAN_OUTCOME = SweepOutcome(resolved=1, answered=1)


@pytest.fixture(autouse=True)
def _reset_proxy_snapshot() -> Iterator[None]:
    """Drop any snapshot a test published on ``om_inventory_settings``.

    The proxy is a module singleton shared across the whole session, and a
    published snapshot overrides ``monkeypatch.setattr``, so without this one test's
    stored override leaks into every later test in the same xdist worker. The
    autouse reset in ``tests/app/conftest.py`` clears the core proxies by name, not
    this one.
    """
    yield
    # ty-attr-ok: annotated as the settings class; the proxy owns ``_set_snapshot``.
    om_inventory_settings._set_snapshot({})  # noqa: SLF001


@pytest_asyncio.fixture
async def api(
    regular_user: CasdoorUser, session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield an authenticated client sharing the test session.

    Async rather than a sync ``TestClient`` so a test can await a database read after
    the request — asserting the cascade on delete needs the request and the check to
    run on one event loop and one session.

    :param regular_user: The authenticated user.
    :param session: The database session the routes should use.
    :return: The client.
    """
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    client = AsyncClient(
        transport=ASGITransport(app=extensions_app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    )
    try:
        yield client
    finally:
        await client.aclose()
        extensions_app.dependency_overrides = {}
