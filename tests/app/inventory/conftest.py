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

"""Define test fixtures for inventory tests."""

import sqlite3
from collections.abc import AsyncGenerator, Iterator
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool
from starlette.testclient import TestClient

from app.api.deps import (
    get_current_service_principal,
    get_current_user,
    require_minimum_role_for_unsafe_methods,
)
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.core.utils.date_time import utc_now
from app.inventory.constants import SYNC_ATTEMPT_MAX_CLOCK_SKEW
from app.inventory.crud import (
    HostSystemObservationManager,
    NodeManager,
    SchemaManager,
    ServiceManager,
    ServiceSystemObservationManager,
    TableManager,
)
from app.inventory.deps import get_session
from app.inventory.main import inventory_app
from app.inventory.models import (
    HostSystemObservation,
    Node,
    RetirableSQLModel,
    Schema,
    Service,
    ServiceSystemObservation,
    SyncOutcomeEnum,
    Table,
)
from tests.app.db_schema import apply_schema
from tests.app.factories import (
    HostSystemObservationWriteFactory,
    NodeWriteFactory,
    SchemaWriteFactory,
    ServiceSystemObservationWriteFactory,
    ServiceWriteFactory,
    TableWriteFactory,
)


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async database session for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(
        dbapi_connection: sqlite3.Connection,
        _connection_record: object,
    ) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def test_client(
    regular_user: CasdoorUser, session: AsyncSession
) -> Iterator[TestClient]:
    """Create an authenticated test client for the inventory app.

    Mirrors the PMM Extensions ``test_client``'s ``require_minimum_role_for_unsafe_methods``
    override so the non-admin fixture user can exercise a mutating route, and
    overrides ``get_current_service_principal`` for the same reason: it is the
    outer dependency on the node and service writes, so overriding
    ``get_current_user`` alone leaves it resolving the fixture user and refusing
    it. Authorization itself is covered by ``test_role_gate.py``, which drives a
    real credential chain.
    """
    inventory_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    inventory_app.dependency_overrides[get_current_user] = lambda: regular_user
    inventory_app.dependency_overrides[get_current_service_principal] = (
        lambda: regular_user
    )
    inventory_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(inventory_app)
    inventory_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def node(session: AsyncSession) -> Node:
    """Create a node in the database."""
    return await NodeManager.create(session, NodeWriteFactory.build())


@pytest_asyncio.fixture
async def service(session: AsyncSession, node: Node) -> Service:
    """Create a service in the database."""
    return await ServiceManager.create(
        session, ServiceWriteFactory.build(), node_id=node.id
    )


@pytest_asyncio.fixture
async def schema(session: AsyncSession, service: Service) -> Schema:
    """Create a schema in the database."""
    return await SchemaManager.create(
        session, SchemaWriteFactory.build(), service_id=service.id
    )


@pytest_asyncio.fixture
async def table(session: AsyncSession, schema: Schema) -> Table:
    """Create a table in the database."""
    return await TableManager.create(
        session,
        TableWriteFactory.build(name="inventory_test_table_one"),
        schema_id=schema.id,
    )


@pytest_asyncio.fixture
async def second_table(session: AsyncSession, schema: Schema, table: Table) -> Table:
    """Create a second table in the database (name must differ per ``ix_table_name_schema_id``)."""
    return await TableManager.create(
        session,
        TableWriteFactory.build(name="inventory_test_table_two"),
        schema_id=schema.id,
    )


async def retire_in_place(
    session: AsyncSession,
    instance: RetirableSQLModel,
    retired_at: datetime | None = None,
) -> None:
    """Retire a single row without cascading, bypassing the retire route.

    Read-policy tests need a tombstone that the code under test did not create,
    and several need one whose ancestors stay active.

    :param session: The async database session owning the instance.
    :param instance: The row to mark retired.
    :param retired_at: The timestamp to stamp, defaulting to now. Pass a distinctly
        older one to tell a pre-existing tombstone apart from a fresh cascade,
        which SQLite's second-resolution timestamps otherwise merge.
    """
    instance.retired_at = retired_at or utc_now()
    instance.retirement_key = instance.id
    session.add(instance)
    await session.commit()
    await session.refresh(instance)


@pytest_asyncio.fixture
async def retired_node(session: AsyncSession, node: Node) -> Node:
    """Retire the node, leaving its subtree active."""
    await retire_in_place(session, node)
    return node


@pytest_asyncio.fixture
async def retired_service(session: AsyncSession, service: Service) -> Service:
    """Retire the service, leaving its node and subtree active."""
    await retire_in_place(session, service)
    return service


@pytest_asyncio.fixture
async def retired_schema(session: AsyncSession, schema: Schema) -> Schema:
    """Retire the schema, leaving its ancestors and tables active."""
    await retire_in_place(session, schema)
    return schema


@pytest_asyncio.fixture
async def retired_table(session: AsyncSession, table: Table) -> Table:
    """Retire the table, leaving its ancestors active."""
    await retire_in_place(session, table)
    return table


@pytest_asyncio.fixture
async def host_observation(session: AsyncSession, node: Node) -> HostSystemObservation:
    """Create a host system observation for the node."""
    return await HostSystemObservationManager.create(
        session,
        HostSystemObservationWriteFactory.build(),
        node_id=node.id,
    )


@pytest_asyncio.fixture
async def service_observation(
    session: AsyncSession, service: Service
) -> ServiceSystemObservation:
    """Create a service system observation for the service."""
    return await ServiceSystemObservationManager.create(
        session,
        ServiceSystemObservationWriteFactory.build(),
        service_id=service.id,
    )


@pytest_asyncio.fixture
async def split_nodes(session: AsyncSession, node: Node) -> tuple[Node, Node]:
    """Create the second node a PMM re-registration of ``node`` leaves behind."""
    successor = await NodeManager.create(
        session, NodeWriteFactory.build(name=node.name, address=node.address)
    )
    return node, successor


@pytest_asyncio.fixture
async def tombstoned_split_nodes(
    session: AsyncSession, split_nodes: tuple[Node, Node]
) -> tuple[Node, Node]:
    """Retire the predecessor of a split pair, as its absence grace eventually does."""
    predecessor, successor = split_nodes
    await retire_in_place(session, predecessor)
    return predecessor, successor


@pytest_asyncio.fixture
async def split_services(
    session: AsyncSession, service: Service
) -> tuple[Service, Service]:
    """Create the second service a re-registration leaves on the same node."""
    successor = await ServiceManager.create(
        session,
        ServiceWriteFactory.build(name=service.name, port=service.port),
        node_id=service.node_id,
    )
    return service, successor


@pytest_asyncio.fixture
async def split_nodes_with_services(
    session: AsyncSession, split_nodes: tuple[Node, Node]
) -> tuple[Node, Node, Service, Service]:
    """Give each node of a split pair a service sharing one name across them."""
    predecessor, successor = split_nodes
    predecessor_service = await ServiceManager.create(
        session,
        ServiceWriteFactory.build(name="inventory-test-mysql"),
        node_id=predecessor.id,
    )
    successor_service = await ServiceManager.create(
        session,
        ServiceWriteFactory.build(name="inventory-test-mysql"),
        node_id=successor.id,
    )
    return predecessor, successor, predecessor_service, successor_service


#: The keys every sync-health-carrying read response exposes.
SYNC_HEALTH_RESPONSE_KEYS = frozenset(
    {
        "last_synced_at",
        "last_sync_error",
        "sync_failing_since",
        "consecutive_failures",
        "newest_attempt_at",
    }
)

#: A fixed attempt time for the bodies below, so a rejection is attributable to
#: the field under test rather than to a moving timestamp.
SYNC_HEALTH_ATTEMPTED_AT = "2026-08-31T12:00:00+00:00"

#: An attempt time past the tolerated clock skew. Relative rather than fixed:
#: the rejection is about the distance from *this run's* clock, so a literal
#: would stop testing the boundary the moment it fell into the past. Read once
#: at collection and overshot by a day, because the margin has to survive the
#: whole run — a value only minutes past the tolerance falls back inside it
#: while the suite is still executing, and the route then answers 204.
#: The tolerance boundary itself is pinned precisely in ``test_models.py``.
SYNC_HEALTH_FUTURE_ATTEMPTED_AT = (
    utc_now() + SYNC_ATTEMPT_MAX_CLOCK_SKEW + timedelta(days=1)
).isoformat()

#: Bodies the sync-health routes must refuse, one per rejection ``SyncHealthWrite``
#: declares. Raw dicts rather than model dumps: the wire shape is the contract
#: these route tests exercise, and none of these can be produced by the model
#: that is supposed to refuse them.
INVALID_SYNC_HEALTH_BODIES = [
    {"outcome": "failure", "attempted_at": SYNC_HEALTH_ATTEMPTED_AT},
    {"outcome": "failure", "error": "", "attempted_at": SYNC_HEALTH_ATTEMPTED_AT},
    {"outcome": "success", "error": "boom", "attempted_at": SYNC_HEALTH_ATTEMPTED_AT},
    {"outcome": "nope", "attempted_at": SYNC_HEALTH_ATTEMPTED_AT},
    {"outcome": "success"},
    {"outcome": "success", "attempted_at": SYNC_HEALTH_FUTURE_ATTEMPTED_AT},
]
INVALID_SYNC_HEALTH_BODY_IDS = [
    "failure_without_error",
    "failure_with_empty_error",
    "success_with_error",
    "unknown_outcome",
    "missing_attempted_at",
    "attempted_at_beyond_clock_skew",
]


def sync_health_payload(
    outcome: SyncOutcomeEnum, error: str | None = None
) -> dict[str, str]:
    """Build the JSON body one sync-health POST sends.

    :param outcome: The outcome the syncer reports.
    :param error: The failure description, omitted entirely when None.
    :return: The request body.
    """
    body = {"outcome": outcome.value, "attempted_at": SYNC_HEALTH_ATTEMPTED_AT}
    if error is not None:
        body["error"] = error
    return body
