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

"""Define shared fixtures for the ATW incident model, CRUD, and route tests."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.extensions.bundle_upload.plan import DeliveryPlan
from app.extensions.deps import (
    get_current_user,
    get_session,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app
from tests.app.db_schema import apply_schema

BEARER_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture
def delivery_plan() -> DeliveryPlan:
    """Provide a ServiceNow-shaped delivery plan with one resolution step."""
    return DeliveryPlan(
        endpoint="https://intake.example.com",
        secrets={"api_key": "real-api-key"},
        resolution_steps=[
            {
                "name": "lookup",
                "method": "GET",
                "path": "ticket_details",
                "headers": {"x-sn-apikey": {"source": "secret", "name": "api_key"}},
                "query": {"number": {"source": "input", "field": "case_ref"}},
                "outputs": {"sys_id": "/result/sys_id"},
            }
        ],
        upload={
            "path": "attachment/upload",
            "headers": {"x-sn-apikey": {"source": "secret", "name": "api_key"}},
            "fields": {
                "table_sys_id": {
                    "source": "output",
                    "step": "lookup",
                    "output": "sys_id",
                }
            },
            "reference_pointer": "/result/sys_id",
        },
    )


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an in-memory async DB session with every SQLModel table created."""
    # scaffolding-dup-ok: this duplication predates the change that
    # re-annotated the fixture's return type; promoting it against
    # its sibling bootstrap is a cross-tree refactor of its own.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
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


@pytest.fixture
def api_client(test_client: TestClient) -> TestClient:
    """Return the shared authenticated client carrying a Bearer header.

    The framework ``RequireBearerForUnsafeMethods`` gate on ``/api/apps`` inspects
    the raw request header, so mutating routes need one even though ``test_client``
    already overrides the gate dependency to a no-op.
    """
    test_client.headers["Authorization"] = BEARER_HEADERS["Authorization"]
    return test_client


@pytest.fixture
def cookie_only_client(test_client: TestClient) -> TestClient:
    """Return a cookie-auth client with the framework Bearer gate left intact.

    Pops ``test_client``'s no-op gate override so mutating routes exercise the real
    ``RequireBearerForUnsafeMethods`` 401 path, and drops any Bearer header.
    """
    extensions_app.dependency_overrides.pop(require_bearer_for_unsafe_methods, None)
    test_client.headers.pop("Authorization", None)
    return test_client


@asynccontextmanager
async def _authenticated_api_client(
    user: CasdoorUser, session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield an authenticated async client acting as ``user``.

    Both method gates are overridden alongside the user, so a fixture built on
    this cannot answer a mutation with the 401 a half-overridden client raises.
    Keeping the overrides here is what stops the two client fixtures drifting
    apart the next time the gate set changes.

    :param user: The user every request authenticates as.
    :param session: The in-memory session the request and any following DB read
        share, so both run on one event loop.
    :return: The configured client, torn down with the overrides on exit.
    """
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: user
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


@pytest_asyncio.fixture
async def admin_api_client(
    admin_user: CasdoorUser, session: AsyncSession
) -> AsyncGenerator[AsyncClient, None]:
    """Yield the authenticated async client as an administrator.

    Mirrors :func:`async_api_client`, whose user is a viewer. Routes declaring
    their own admin dependency answer that client with 403, so a test covering
    one needs this fixture and a test covering the refusal needs the other.
    """
    async with _authenticated_api_client(admin_user, session) as client:
        yield client


@pytest_asyncio.fixture
async def async_api_client(
    regular_user: CasdoorUser, session: AsyncSession
) -> AsyncGenerator[AsyncClient, None]:
    """Yield an authenticated async client sharing the in-memory test session.

    Used by route tests that must ``await`` a DB read after the request (e.g.
    asserting cascade deletes): the request and the DB check then run on the same
    event loop and session, which a sync ``TestClient`` cannot offer.
    """
    async with _authenticated_api_client(regular_user, session) as client:
        yield client
