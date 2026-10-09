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

"""Define shared fixtures for the snippets plugin tests."""

from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.datastructures import URL

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.extensions.deps import (
    get_current_user,
    get_session,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app
from app.extensions.snippets.config import snippets_settings
from tests.app.db_schema import apply_schema


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async database session backed by in-memory SQLite."""
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


@pytest.fixture(autouse=True)
def _bind_request_less_session(request_less_session: AsyncSession) -> None:
    """Apply the request-less session binding to every test in this subtree.

    Autouse so every derived-route test sees the seeded data; a no-op for tests
    that never hit that path.
    """


@pytest.fixture(autouse=True)
def snippets_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a base URL so the request-less execute path can build artifact URLs.

    The migrated execute hook reads ``SNIPPETS_BASE_URL`` / ``BASE_URL`` instead of
    deriving the artifact URL from the request, so a base URL must be set or
    ``build_snippet_source`` 400s. The value matches the ``TestClient`` host the
    legacy request-derived fallback produced, keeping artifact URLs unchanged.
    """
    monkeypatch.setattr(
        snippets_settings, "SNIPPETS_BASE_URL", URL("http://testserver")
    )


@pytest.fixture
def admin_client(
    admin_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Return a TestClient authenticated as an admin with the real session."""
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def non_admin_client(
    regular_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Return a TestClient authenticated as a non-admin user."""
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def api_admin_client(
    admin_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Return a TestClient authenticated as an admin via the JSON API auth path."""
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def api_non_admin_client(
    regular_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Return a TestClient authenticated as a non-admin via the JSON API auth path."""
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def api_admin_client_no_bearer(
    admin_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Return a cookie-auth API admin TestClient with the Bearer gate intact.

    Diverges from the central :func:`tests.app.extensions.conftest.api_admin_client_no_bearer`
    by additionally pinning ``get_session`` to the in-memory test ``session``:
    the snippets ``Snippet`` table only exists on that session, so the
    central fixture (no session override) would 500 inside the route. Keep
    this local override; do not delete in favour of the central one without
    first solving the snippets table-visibility constraint.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def api_unauthenticated_client(session: AsyncSession) -> Iterator[TestClient]:
    """Return a TestClient with no auth overrides — every JSON call should 401."""
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}
