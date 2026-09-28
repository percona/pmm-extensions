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

"""Define test fixtures."""

import inspect
import os
import socket
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aioresponses.core
import pytest
import pytest_asyncio
from aiohttp import ClientResponse
from faker import Faker
from fastapi import Request, status
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from pytest_mock import MockerFixture
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy_celery_beat.models import PeriodicTask
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.alerts.config import alert_settings
from app.core.auth.base import BaseAuthProvider
from app.core.auth.config import get_active_auth_provider
from app.core.auth.models import OAuthToken, UserRole
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.auth.providers.grafana.models import ASSERTION_SALT
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.health import HEALTH_PATH
from app.core.requests import RemoteAPI
from app.core.utils import json_serializer
from app.extensions.config import extensions_settings
from app.extensions.deps import (
    get_current_user,
    get_inventory_api,
    get_session,
    get_tasks_api,
    require_bearer_for_unsafe_methods,
)
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
)
from app.extensions.main import extensions_app
from app.extensions.snippets.config import snippets_settings
from app.inventory.models import ServiceTypeEnum
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.config import tasks_settings
from tests.app.db_schema import apply_schema
from tests.app.factories import (
    CasdoorUserFactory,
    CreatedNodeFactory,
    CreatedSchemaFactory,
    CreatedServiceFactory,
    CreatedTableFactory,
    OAuthTokenFactory,
)

# aiohttp 3.14 made ``stream_writer`` a required arg of ``ClientResponse``, but
# aioresponses (<=0.7.8) still constructs mocks without it. Default it here until
# aioresponses ships a fix; guarded on the param so it's a no-op on aiohttp < 3.14.
if "stream_writer" in inspect.signature(ClientResponse.__init__).parameters:

    class _StubStreamWriter:
        # aioresponses passes writer=None, so ClientResponse reads output_size.
        output_size: int = 0

    class _CompatClientResponse(ClientResponse):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("stream_writer", _StubStreamWriter())
            super().__init__(*args, **kwargs)

    aioresponses.core.ClientResponse = _CompatClientResponse


class HealthProbeServer:
    """Drive a real HTTP listener on loopback answering the shared health path.

    The readiness gate in :mod:`app.core.health` polls over a real socket, so the
    failures worth covering — connection refused, a listener that accepts and
    never answers, a host-header rejection — only reproduce against a real
    listener. The port is reserved and released in ``__init__`` so a test can
    probe a closed port before calling :meth:`start`.
    """

    def __init__(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            self.port: int = probe.getsockname()[1]
        self.statuses: list[int] = []
        self.default_status: int = status.HTTP_200_OK
        self.headers_to_send: dict[str, str] = {}
        self.required_host: str | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.listening = threading.Event()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            """Answer the health path from the controller's script."""

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
                """Answer with the next scripted status, honouring the host rule."""
                server.requests.append((self.path, dict(self.headers.items())))
                if self.path != HEALTH_PATH:
                    self.send_error(status.HTTP_404_NOT_FOUND)
                    return
                if (
                    server.required_host is not None
                    and self.headers.get("Host", "").split(":")[0]
                    != server.required_host
                ):
                    self.send_error(status.HTTP_400_BAD_REQUEST)
                    return
                status_code = (
                    server.statuses.pop(0) if server.statuses else server.default_status
                )
                self.send_response(status_code)
                for name, value in server.headers_to_send.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any, **kwargs: Any) -> None:
                """Suppress the handler's stderr access log."""

        return Handler

    def start(self) -> None:
        """Start serving on the reserved port."""
        for _ in range(5):
            try:
                self._server = HTTPServer(
                    ("127.0.0.1", self.port), self._build_handler()
                )
            except OSError:
                continue
            break
        else:
            pytest.skip(f"could not bind 127.0.0.1:{self.port} for a probe test")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.listening.set()

    def stop(self) -> None:
        """Stop serving and join the serving thread."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture(name="health_probe_server")
def health_probe_server_fixture() -> Iterator[HealthProbeServer]:
    """Yield a controller for a real loopback listener on the health path."""
    server = HealthProbeServer()
    yield server
    server.stop()


@event.listens_for(Engine, "connect")
def _attach_declared_schemas_on_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Make every declared symbolic schema a real one on every SQLite connection.

    An app's tables declare a token under ``extensions_settings.DATABASE.
    SCHEMA_TRANSLATE_MAP`` and the application engine translates it — to a real
    schema on PostgreSQL, to the default schema on SQLite, where PMM Extensions and inventory
    are separate database files and cannot collide. This suite is the one place
    they *can*: it creates every service's metadata in a single in-memory
    database, where a token-scoped ``service`` table would be PMM Extensions inventory's
    ``service``.

    So rather than translate the token here, satisfy it: SQLite has no schemas but it
    has ``ATTACH``, and an attached in-memory database *is* a schema as far as SQL is
    concerned. Attaching one under each declared token's own name means the
    untranslated ``<token>.service`` resolves, and no fixture needs a
    ``schema_translate_map`` — which matters because the suite builds engines in
    some thirty places, and each one that forgot would fail with "unknown database
    <token>".

    Registered on the ``Engine`` class so it covers engines that do not exist yet.
    It fires when the DBAPI connection is opened, before any statement, so
    ``create_all`` already sees the schema. Every SQLite engine here is in-memory and
    single-connection, so the attached database lives exactly as long as the main
    one. Non-SQLite binds are left alone — they have real schemas, and the
    PostgreSQL fixture below routes each token into its per-worker one.

    :param dbapi_connection: The freshly opened DBAPI connection.
    :param _record: The pool's record for it. Unused.
    """
    if "sqlite" not in type(dbapi_connection).__module__:
        return
    cursor = dbapi_connection.cursor()
    for token in extensions_settings.DATABASE.SCHEMA_TRANSLATE_MAP:
        cursor.execute(f"ATTACH DATABASE ':memory:' AS {token}")
    cursor.close()


@pytest.fixture(scope="session", autouse=True)
def _disable_settings_override_refresher_for_session() -> Iterator[None]:
    """Disable the DB-override background refresher for the entire test session.

    ``TestClient(...)`` enters each FastAPI app's lifespan, which would otherwise
    open a session against the production engine via the real refresher. We use
    ``pytest.MonkeyPatch()`` directly (not the function-scoped ``monkeypatch``
    fixture, which raises ``ScopeMismatch`` at session scope) so the patch
    survives across tests. Tests that need to exercise the refresher re-enable
    it locally via the function-scoped ``monkeypatch`` fixture.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", False)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _override_snapshot_cleared() -> None:
    """Clear every wired ``OverridableSettingsProxy`` snapshot before each test.

    Tests that monkey-patch ``*_settings`` attributes assume the proxy's
    ``__getattr__`` falls through to the wrapped Pydantic instance. An active
    snapshot entry would shadow the monkey-patched value and confuse the test.
    """
    settings._set_snapshot({})  # noqa: SLF001
    extensions_settings._set_snapshot({})  # noqa: SLF001
    tasks_settings._set_snapshot({})  # noqa: SLF001
    snippets_settings._set_snapshot({})  # noqa: SLF001
    alert_settings._set_snapshot({})  # noqa: SLF001
    anonymizer_settings._set_snapshot({})  # noqa: SLF001


@pytest.fixture(scope="session")
def faker() -> Faker:
    """Provide a Faker instance for generating fake data."""
    return Faker()


@pytest.fixture
def casdoor_allowed_issuer() -> str:
    """Provide a Casdoor allowed issuer URL."""
    return "https://allowed-issuer.com"


@pytest.fixture
def casdoor_disallowed_issuer() -> str:
    """Provide a Casdoor disallowed issuer URL."""
    return "https://disallowed-issuer.com"


@pytest.fixture
def casdoor_client_id() -> str:
    """Provide a fake Casdoor client ID."""
    return "test-client-id"


@pytest.fixture
def valid_username() -> str:
    """Provide a valid username for testing."""
    return "valid-username"


@pytest.fixture
def casdoor_token_payload_data(
    casdoor_allowed_issuer: str,
    casdoor_client_id: str,
    valid_username: str,
    faker: Faker,
) -> dict[str, Any]:
    """Provide mock data for a Casdoor token payload."""
    return {
        "iss": casdoor_allowed_issuer,
        "sub": faker.pystr(),
        "aud": [casdoor_client_id],
        "exp": round(faker.unix_time(end_datetime="+30d", start_datetime="+7d")),
        "nbf": round(faker.unix_time(end_datetime="-1d", start_datetime="-3d")),
        "jti": faker.pystr(),
        "username": valid_username,
        "active": True,
    }


@pytest.fixture
def casdoor_user_data(valid_username: str, faker: Faker) -> dict[str, Any]:
    """Provide mock data for a Casdoor user."""
    return {
        "id": faker.uuid4(),
        "username": valid_username,
        "email": faker.email(),
        "first_name": faker.first_name(),
        "last_name": faker.last_name(),
        "is_admin": False,
        "created_time": faker.date_time().isoformat(),
        "updated_time": "",
        "owner": "organization",
        "is_forbidden": False,
        "is_deleted": False,
    }


@pytest.fixture(scope="class")
def oauth_token() -> OAuthToken:
    """Provide a mock OAuthToken instance."""
    return OAuthTokenFactory.build()


@pytest.fixture
def refresh_token() -> str:
    """Provide a mock refresh token."""
    return "test_refresh_token"


@pytest.fixture
def casdoor_mock(
    casdoor_token_payload_data: dict[str, Any],
    oauth_token: OAuthToken,
    refresh_token: str,
    casdoor_user_data: dict[str, Any],
    mocker: MockerFixture,
) -> BaseAuthProvider:
    """Mock CasdoorSDK methods to simulate Casdoor service interactions."""
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.introspect_token",
        new=mocker.AsyncMock(return_value=casdoor_token_payload_data),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.get_access_token",
        new=mocker.AsyncMock(return_value=oauth_token.model_dump()),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.refresh_token_request",
        new=mocker.AsyncMock(return_value=oauth_token.model_dump()),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.get_token",
        new=mocker.AsyncMock(return_value={"data": {"refreshToken": refresh_token}}),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.get_user",
        new=mocker.AsyncMock(return_value=casdoor_user_data),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.get_users",
        new=mocker.AsyncMock(return_value=[casdoor_user_data]),
    )
    mocker.patch(
        "app.core.auth.providers.casdoor.sdk.CasdoorSDK.delete_token",
        new=mocker.AsyncMock(return_value=True),
    )
    return get_active_auth_provider()


@pytest.fixture
def resolve_casdoor_as_role(
    casdoor_mock: BaseAuthProvider,
    casdoor_user_data: dict[str, Any],
    mocker: MockerFixture,
) -> Callable[[UserRole], None]:
    """Return a callable resolving the Bearer credential to a given rank.

    A Casdoor payload carrying a ``role`` of its own bypasses the admin-flag
    derivation, so the caller resolves at exactly the requested rank rather than
    one inferred from ``is_admin`` — which is what makes a rank below
    administrator constructible at all.
    """

    def resolve_as(role: UserRole) -> None:
        mocker.patch(
            "app.core.auth.providers.casdoor.sdk.CasdoorSDK.get_user",
            new=mocker.AsyncMock(
                return_value={**casdoor_user_data, "role": role.value}
            ),
        )

    return resolve_as


#: A made-up token in the measured ``glsa_`` shape, presented as a caller's
#: Bearer credential (not PMM Extensions' own service-account token). It is assembled
#: from its parts so no committed line carries a string secret scanners would
#: report as a live Grafana credential.
_FAKE_GRAFANA_TOKEN_SECRET: Final = "Qx7Rk2mZp9LwYc4Vb8Nt3Hs6Jd1Fg5Ae"
_FAKE_GRAFANA_TOKEN_CHECKSUM: Final = "3f9c01ab"
GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN: Final = (
    f"glsa_{_FAKE_GRAFANA_TOKEN_SECRET}_{_FAKE_GRAFANA_TOKEN_CHECKSUM}"
)


@pytest.fixture
def grafana_service_account_token() -> str:
    """Provide a fake Grafana service-account token."""
    return "test-service-account-token"


@pytest.fixture
def grafana_user_record(valid_username: str, faker: Faker) -> dict[str, Any]:
    """Provide a mock Grafana ``/api/user`` record."""
    return {
        "id": faker.random_int(min=1),
        "login": valid_username,
        "email": faker.email(),
        "isGrafanaAdmin": False,
    }


@pytest.fixture
def grafana_user_orgs() -> list[dict[str, Any]]:
    """Provide a mock Grafana ``/api/user/orgs`` payload."""
    return [{"orgId": 1, "name": "Main Org.", "role": "Viewer"}]


@pytest.fixture
def grafana_org_users(valid_username: str, faker: Faker) -> list[dict[str, Any]]:
    """Provide a mock Grafana ``/api/org/users`` payload."""
    return [
        {
            "userId": faker.random_int(min=1),
            "login": valid_username,
            "email": faker.email(),
            "role": "Viewer",
        }
    ]


@pytest.fixture
def grafana_provider(grafana_service_account_token: str) -> GrafanaAuthProvider:
    """Provide a ``GrafanaAuthProvider`` built with test config."""
    return GrafanaAuthProvider(
        endpoint="https://grafana.example.com",
        service_account_token=grafana_service_account_token,
    )


@pytest.fixture
def grafana_mock(
    grafana_provider: GrafanaAuthProvider,
    grafana_user_record: dict[str, Any],
    grafana_user_orgs: list[dict[str, Any]],
    grafana_org_users: list[dict[str, Any]],
    mocker: MockerFixture,
) -> GrafanaAuthProvider:
    """Mock GrafanaSDK methods and pin Grafana as the active auth provider."""
    mocker.patch(
        "app.core.auth.config.get_active_auth_provider",
        return_value=grafana_provider,
    )
    mocker.patch(
        "app.core.auth.providers.grafana.sdk.GrafanaSDK.login",
        new=mocker.AsyncMock(return_value="grafana-session"),
    )
    mocker.patch(
        "app.core.auth.providers.grafana.sdk.GrafanaSDK.get_current_user",
        new=mocker.AsyncMock(return_value=grafana_user_record),
    )
    mocker.patch(
        "app.core.auth.providers.grafana.sdk.GrafanaSDK.get_current_user_orgs",
        new=mocker.AsyncMock(return_value=grafana_user_orgs),
    )
    mocker.patch(
        "app.core.auth.providers.grafana.sdk.GrafanaSDK.get_org_users",
        new=mocker.AsyncMock(return_value=grafana_org_users),
    )
    mocker.patch(
        "app.core.auth.providers.grafana.sdk.GrafanaSDK.lookup_user",
        new=mocker.AsyncMock(return_value=grafana_user_record),
    )
    return grafana_provider


@pytest.fixture
def admin_user(valid_username: str, faker: Faker) -> CasdoorUser:
    """Create a mock admin user with active status."""
    return CasdoorUserFactory.build(role=UserRole.ADMIN)


@pytest.fixture
def regular_user(valid_username: str, faker: Faker) -> CasdoorUser:
    """Create a mock regular user with active status."""
    return CasdoorUserFactory.build(
        username=valid_username,
        role=UserRole.VIEWER,
    )


@pytest.fixture
def created_node() -> CreatedNode:
    """Return a fake created node."""
    return CreatedNodeFactory.build(address="localhost")


@pytest.fixture
def created_service(created_node: CreatedNode) -> CreatedService:
    """Return a fake created service."""
    return CreatedServiceFactory.build(node=created_node, type=ServiceTypeEnum.MYSQL)


@pytest.fixture
def created_schema() -> CreatedSchema:
    """Return a fake created Schema."""
    return CreatedSchemaFactory.build()


@pytest.fixture
def created_table() -> CreatedTable:
    """Return a fake created Table."""
    return CreatedTableFactory.build()


@pytest.fixture
def mock_remote_api() -> AsyncMock:
    """Mock a RemoteAPI object."""
    return AsyncMock(spec=RemoteAPI)


POSTGRES_DSN_ENV = "EXTENSIONS_TEST_POSTGRES_DSN"


def postgres_dsn_or_skip() -> str:
    """Return the real-PostgreSQL test DSN, skipping the test when it is unset.

    Local runs without PostgreSQL skip; the dedicated ``test_postgres`` CI job
    supplies ``$EXTENSIONS_TEST_POSTGRES_DSN``.

    :return: The DSN from the environment.
    """
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(f"{POSTGRES_DSN_ENV} not set; skipping real-PostgreSQL tests")
    return dsn


def postgres_worker_schema() -> str:
    """Return the per-xdist-worker schema name for real-PostgreSQL tests.

    Each xdist worker gets its own schema so parallel workers never collide on
    ``CREATE``/``DROP`` against a shared database.
    """
    return f"extensions_test_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"


@pytest_asyncio.fixture
async def postgres_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provide a real-PostgreSQL ``AsyncEngine`` for dialect-specific SQL tests.

    Connect through the already-present ``asyncpg`` driver to the DSN in
    ``$EXTENSIONS_TEST_POSTGRES_DSN``. This is chosen over ``pytest-postgresql`` or
    ``testcontainers`` because it adds no dependency and the CI
    ``services: postgres`` container supplies the server. An unset env var skips
    the test (local runs without PostgreSQL); a set env var must connect, so a
    misconfigured CI service fails loudly instead of silently skipping — the
    connect is deliberately not wrapped in a try/skip.

    Function-scoped to match every other async fixture in the suite: the
    session-default event-loop scope makes a session-scoped async fixture bind to
    the wrong loop. Parallel-safe by construction — each xdist worker gets its own
    schema via ``schema_translate_map``, so reuse under xdist needs no serial-run
    convention.

    Pair with ``postgres_session`` for a real-PG-bound ``AsyncSession`` — the
    reusable seam for any code that dispatches on a real PostgreSQL bind.
    """
    dsn = postgres_dsn_or_skip()
    schema = postgres_worker_schema()
    base = create_async_engine(dsn, json_serializer=json_serializer)
    try:
        async with base.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        # Every declared symbolic schema token (OM's ``om_schema``, and any other
        # app's) has to translate somewhere. PostgreSQL has no ``ATTACH`` for the
        # SQLite trick above, so translate them -- into the *same* per-worker schema
        # as everything else, because that is what teardown drops. Routing one to
        # its literal resolved name would escape the worker schema and collide
        # across xdist workers.
        yield base.execution_options(
            schema_translate_map={
                None: schema,
                **dict.fromkeys(
                    extensions_settings.DATABASE.SCHEMA_TRANSLATE_MAP, schema
                ),
            }
        )
    finally:
        try:
            async with base.begin() as conn:
                await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await base.dispose()


@pytest_asyncio.fixture
async def postgres_session_maker(
    postgres_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create every ``SQLModel`` table on real PostgreSQL and yield a session maker.

    The tables (including ``TaskHistory`` with its ``jsonb`` ``execution_request``)
    go into the worker schema and are dropped on teardown. A maker rather than a
    session, so a test racing two callers can put each on its own connection.

    :param postgres_engine: The real-PostgreSQL engine to create the tables on.
    :return: A session maker bound to that engine.
    """
    async with postgres_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    try:
        yield get_async_session_maker_from_engine(postgres_engine)
    finally:
        async with postgres_engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)


@pytest_asyncio.fixture
async def postgres_session(
    postgres_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Provide a real-PostgreSQL ``AsyncSession`` with the tasks-service tables.

    The seam for any test whose subject dispatches on the session bind. Take
    ``postgres_session_maker`` instead where the test needs two sessions at once.

    :param postgres_session_maker: The table-bootstrapped session maker.
    :return: One session on that maker, closed on teardown.
    """
    async with postgres_session_maker() as session:
        yield session


# The client/session fixtures below live here — the always-loaded ancestor conftest —
# rather than in ``tests/app/extensions/conftest.py`` so they resolve regardless of single-process
# collection order. ``tests/app/extensions/conftest.py`` re-exports them for the extensions
# subtree; nearer conftests (tasks, inventory, extensions/apps/*) still shadow them as before.


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async db session for testing."""
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
        # ``StaticPool`` keeps the aiosqlite connection (and its non-daemon
        # thread) alive until the engine is disposed; without this the thread
        # survives to interpreter exit and blocks ``threading._shutdown``.
        await engine.dispose()


@pytest_asyncio.fixture(name="beat_maker")
async def beat_maker_fixture() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a session maker bound to an in-memory celery-beat DB.

    The celery-beat tables are owned by ``sqlalchemy-celery-beat`` and live in
    their own schema, so they are created from that metadata rather than
    ``SQLModel``'s, and the schema is translated away for SQLite.

    :return: A session maker bound to a fresh beat store.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    engine = engine.execution_options(schema_translate_map={"celery_schema": None})
    async with engine.begin() as conn:
        await apply_schema(conn, PeriodicTask.__table__.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="celery_beat_session")
async def celery_beat_session_fixture(
    beat_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Create an async db session backed by the celery-beat tables."""
    async with beat_maker() as session:
        yield session


@pytest.fixture
def test_client(
    regular_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Yield an authenticated cookie-auth TestClient for the PMM Extensions app.

    Overrides ``require_bearer_for_unsafe_methods`` so cookie-only JSON
    mutations under ``/api/apps/*`` are not blocked by the framework
    Bearer gate, and ``require_minimum_role_for_unsafe_methods`` so the fixture user
    need not be an admin to exercise a mutating route. App-local
    ``test_client`` overrides MUST mirror both; see
    :func:`api_admin_client_no_bearer` for the negative-path fixture that
    leaves the Bearer gate intact.

    ``get_session`` is overridden to the in-memory ``session`` so the
    ``require_app_enabled`` route guard reads an isolated, empty ``appstate``
    table (no rows -> every app enabled) instead of a shared, order-dependent
    DB. Tests that exercise the disabled path override ``get_session`` again
    with a session that carries an ``enabled=False`` row.
    """
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def api_admin_client_no_bearer(admin_user: CasdoorUser) -> Iterator[TestClient]:
    """Yield a cookie-auth admin TestClient with the Bearer gate intact.

    Mirrors :func:`test_client` but deliberately leaves
    ``require_bearer_for_unsafe_methods`` un-overridden, so cookie-only
    JSON mutations to ``/api/apps/*`` are rejected by the framework
    Bearer gate. Use in tests that assert the 401 path.
    """
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def unauthenticated_client() -> Iterator[TestClient]:
    """Yield a test client with authentication dependency overrides cleared."""
    previous = extensions_app.dependency_overrides
    extensions_app.dependency_overrides = {}
    try:
        yield TestClient(extensions_app, raise_server_exceptions=False)
    finally:
        extensions_app.dependency_overrides = previous


@pytest_asyncio.fixture
async def async_test_client(
    regular_user: CasdoorUser,
) -> AsyncGenerator[AsyncClient, None]:
    """Yield an authenticated async cookie-auth client for the PMM Extensions app.

    See :func:`test_client` for the gate-override rationale.
    """
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user

    transport = ASGITransport(app=extensions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    extensions_app.dependency_overrides = {}


def make_roleless_grafana_assertion(token_type: str) -> str:
    """Return a signed Grafana identity assertion carrying no ``role`` claim.

    Reproduces the assertion shape minted before the role became a claim of its
    own, which the current minting path can no longer produce. The serializer is
    rebuilt from the signing key and the shared salt constant rather than
    imported (the module-level serializer is private), so the forged assertion
    verifies against the real one.

    :param token_type: The ``typ`` claim to embed (``"access"``, ``"refresh"``
        or ``"exchange"``).
    :return: The signed, URL-safe assertion.
    """
    return URLSafeTimedSerializer(
        settings.SECRET_KEY.get_secret_value(), salt=ASSERTION_SALT
    ).dumps(
        {
            "id": str(uuid4()),
            "username": "alice",
            "email": "",
            "is_admin": True,
            "typ": token_type,
        }
    )


def make_request(
    method: str = "GET",
    authorization: str | None = None,
    endpoint: Callable[..., Any] | None = None,
) -> Request:
    """Build a minimal Request for dependencies that take one.

    :param method: HTTP method to set on the request scope.
    :param authorization: Value for the ``Authorization`` header, if any.
    :param endpoint: Handler to expose as the matched route's ``endpoint``. When
        omitted the scope carries no ``route`` key, which is what an unmatched
        request looks like.
    :return: A ``Request`` over the assembled scope.
    """
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "method": method,
        "client": ("127.0.0.1", "80"),
        "path": "/",
        "app": MagicMock(),
        "router": MagicMock(),
    }
    if endpoint is not None:
        scope["route"] = SimpleNamespace(endpoint=endpoint)
    return Request(scope)


@pytest.fixture
def dummy_request() -> Request:
    """Create a dummy Request for dependencies that take one."""
    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", "80"), "path": "/"}
    return Request(scope)


@pytest.fixture
def mock_task_api_dep(mock_remote_api: RemoteAPI) -> Iterator[AsyncMock]:
    """Mock the TaskAPI dependency."""
    mock = AsyncMock(spec=RemoteAPI)
    extensions_app.dependency_overrides[get_tasks_api] = lambda: mock
    yield mock
    extensions_app.dependency_overrides = {}


@pytest.fixture
def mock_inventory_api_dep(mock_remote_api: RemoteAPI) -> Iterator[AsyncMock]:
    """Mock the InventoryAPI dependency."""
    mock = AsyncMock(spec=RemoteAPI)
    mock.get.return_value = {
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": 50,
    }
    extensions_app.dependency_overrides[get_inventory_api] = lambda: mock
    yield mock
    extensions_app.dependency_overrides = {}
