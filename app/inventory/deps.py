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

"""Define dependencies for the Inventory API."""

from collections.abc import AsyncGenerator, Callable
from typing import Annotated

from fastapi import Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db import ListQuery, make_list_query_dep
from app.core.exceptions import HTTPNotFoundException
from app.inventory.constants import (
    UNCOLLECTED_HOST_OBSERVATION_DETAIL,
    UNCOLLECTED_SERVICE_OBSERVATION_DETAIL,
)
from app.inventory.crud import (
    HostSystemObservationManager,
    NodeManager,
    RetirableManagerMixin,
    RetiredInclusiveNodeManager,
    RetiredInclusiveSchemaManager,
    RetiredInclusiveServiceManager,
    RetiredInclusiveTableManager,
    SchemaManager,
    ServiceManager,
    ServiceSystemObservationManager,
    TableManager,
)
from app.inventory.db import get_async_session_maker
from app.inventory.models import (
    HostSystemObservation,
    Node,
    Schema,
    Service,
    ServiceSystemObservation,
    Table,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an asynchronous database session for FastAPI routes.

    This function provides a dependency for FastAPI routes that yields an `AsyncSession`
    for interacting with the database. The session is properly closed after use.

    :yield: An asynchronous session for database operations.
    :rtype: AsyncGenerator[AsyncSession, None]
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_node(session: SessionDep, node_id: int) -> Node:
    """Retrieve a node by its unique identifier.

    Fetch the node corresponding to the provided `node_id`. If no such node exists,
    raise an HTTP 404 Not Found exception.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param node_id: The unique identifier of the node to retrieve.
    :type node_id: int
    :return: The node instance corresponding to the provided `node_id`.
    :rtype: Node
    :raises HTTPNotFoundException: If no node with the specified `node_id` exists.
    """
    return await NodeManager.get_or_404(session, id=node_id)


async def get_service(session: SessionDep, service_id: int) -> Service:
    """Retrieve a service by its unique identifier.

    Fetch the service corresponding to the provided `service_id`.
    If no such service exists, raise an HTTP 404 Not Found exception.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param service_id: The unique identifier of the service to retrieve.
    :type service_id: int
    :return: The service instance corresponding to the provided `service_id`.
    :rtype: Service
    :raises HTTPNotFoundException: If no service with the specified `service_id` exists.
    """
    return await ServiceManager.get_or_404(session, id=service_id)


async def get_schema(session: SessionDep, schema_id: int) -> Schema:
    """Retrieve a schema by its unique identifier.

    Fetch the schema corresponding to the provided `schema_id`.
    If no such schema exists, raise an HTTP 404 Not Found exception.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param schema_id: The unique identifier of the schema to retrieve.
    :type schema_id: int
    :return: The schema instance corresponding to the provided `schema_id`.
    :rtype: Schema
    :raises HTTPNotFoundException: If no schema with the specified `schema_id` exists.
    """
    return await SchemaManager.get_or_404(session, id=schema_id)


async def get_table(session: SessionDep, table_id: int) -> Table:
    """Retrieve a table by its unique identifier.

    Fetch the table corresponding to the provided `table_id`. If no such table exists,
    raise an HTTP 404 Not Found exception.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param table_id: The unique identifier of the table to retrieve.
    :type table_id: int
    :return: The table instance corresponding to the provided `table_id`.
    :rtype: Table
    :raises HTTPNotFoundException: If no table with the specified `table_id` exists.
    """
    return await TableManager.get_or_404(session, id=table_id)


NodeDep = Annotated[Node, Depends(get_node)]
ServiceDep = Annotated[Service, Depends(get_service)]
SchemaDep = Annotated[Schema, Depends(get_schema)]
TableDep = Annotated[Table, Depends(get_table)]


async def get_node_including_retired(session: SessionDep, node_id: int) -> Node:
    """Retrieve a node by its identifier, retired or not.

    Serves the routes whose subject is legitimately a tombstone: re-retiring an
    already-retired node is a no-op rather than a 404, a tombstone stays
    revivable, and an identity link's predecessor is routinely one.

    :param session: The asynchronous database session.
    :param node_id: The unique identifier of the node to retrieve.
    :return: The node instance corresponding to the provided ``node_id``.
    :raises HTTPNotFoundException: If no node with the specified ``node_id`` exists.
    """
    return await RetiredInclusiveNodeManager.get_or_404(session, id=node_id)


async def get_service_including_retired(
    session: SessionDep, service_id: int
) -> Service:
    """Retrieve a service by its identifier, retired or not.

    Service-level counterpart of :func:`get_node_including_retired`.

    :param session: The asynchronous database session.
    :param service_id: The unique identifier of the service to retrieve.
    :return: The service instance corresponding to the provided ``service_id``.
    :raises HTTPNotFoundException: If no service with the specified ``service_id``
        exists.
    """
    return await RetiredInclusiveServiceManager.get_or_404(session, id=service_id)


async def get_schema_including_retired(session: SessionDep, schema_id: int) -> Schema:
    """Retrieve a schema by its identifier, retired or not.

    Schema-level counterpart of :func:`get_node_including_retired`.

    :param session: The asynchronous database session.
    :param schema_id: The unique identifier of the schema to retrieve.
    :return: The schema instance corresponding to the provided ``schema_id``.
    :raises HTTPNotFoundException: If no schema with the specified ``schema_id``
        exists.
    """
    return await RetiredInclusiveSchemaManager.get_or_404(session, id=schema_id)


async def get_table_including_retired(session: SessionDep, table_id: int) -> Table:
    """Retrieve a table by its identifier, retired or not.

    Table-level counterpart of :func:`get_node_including_retired`.

    :param session: The asynchronous database session.
    :param table_id: The unique identifier of the table to retrieve.
    :return: The table instance corresponding to the provided ``table_id``.
    :raises HTTPNotFoundException: If no table with the specified ``table_id`` exists.
    """
    return await RetiredInclusiveTableManager.get_or_404(session, id=table_id)


RetirableNodeDep = Annotated[Node, Depends(get_node_including_retired)]
RetirableServiceDep = Annotated[Service, Depends(get_service_including_retired)]
RetirableSchemaDep = Annotated[Schema, Depends(get_schema_including_retired)]
RetirableTableDep = Annotated[Table, Depends(get_table_including_retired)]


def make_retirement_scope_dep(
    active: type[RetirableManagerMixin],
    retired_inclusive: type[RetirableManagerMixin],
) -> Callable[..., type[RetirableManagerMixin]]:
    """Build the dependency choosing which manager a read goes through.

    The opt-in rides the manager class rather than a call argument, so every read
    route needs the same pairing. Declaring it once here also declares the
    ``include_retired`` query parameter once, instead of per handler.

    :param active: The manager whose reads exclude retired rows.
    :param retired_inclusive: Its sibling whose reads include them.
    :return: The ``Depends()`` callable resolving one of the two.
    """

    def resolve(
        *,
        include_retired: Annotated[bool, Query()] = False,
    ) -> type[RetirableManagerMixin]:
        """Return the manager matching the request's retirement scope.

        :param include_retired: Whether the read should see retired rows.
        :return: The manager to read through.
        """
        return retired_inclusive if include_retired else active

    return resolve


NodeScopeDep = Annotated[
    type[NodeManager],
    Depends(make_retirement_scope_dep(NodeManager, RetiredInclusiveNodeManager)),
]
ServiceScopeDep = Annotated[
    type[ServiceManager],
    Depends(make_retirement_scope_dep(ServiceManager, RetiredInclusiveServiceManager)),
]
SchemaScopeDep = Annotated[
    type[SchemaManager],
    Depends(make_retirement_scope_dep(SchemaManager, RetiredInclusiveSchemaManager)),
]
TableScopeDep = Annotated[
    type[TableManager],
    Depends(make_retirement_scope_dep(TableManager, RetiredInclusiveTableManager)),
]


async def get_in_scope_node(
    session: SessionDep, node_id: int, manager: NodeScopeDep
) -> Node:
    """Resolve the node addressed by the path within the request's retirement scope.

    Share the children's retirement scope, so a nested-list route resolves its parent
    and its children from one ``include_retired`` value.

    :param session: The asynchronous database session.
    :param node_id: The unique identifier of the node to retrieve.
    :param manager: The node manager the request's retirement scope selected.
    :return: The node instance corresponding to the provided ``node_id``.
    :raises HTTPNotFoundException: If no node in scope has the given ``node_id``.
    """
    return await manager.get_or_404(session, id=node_id)


async def get_in_scope_service(
    session: SessionDep, service_id: int, manager: ServiceScopeDep
) -> Service:
    """Resolve the service addressed by the path within the request's retirement scope.

    Service-level counterpart of :func:`get_in_scope_node`.

    :param session: The asynchronous database session.
    :param service_id: The unique identifier of the service to retrieve.
    :param manager: The service manager the request's retirement scope selected.
    :return: The service instance corresponding to the provided ``service_id``.
    :raises HTTPNotFoundException: If no service in scope has the given
        ``service_id``.
    """
    return await manager.get_or_404(session, id=service_id)


async def get_in_scope_schema(
    session: SessionDep, schema_id: int, manager: SchemaScopeDep
) -> Schema:
    """Resolve the schema addressed by the path within the request's retirement scope.

    Schema-level counterpart of :func:`get_in_scope_node`.

    :param session: The asynchronous database session.
    :param schema_id: The unique identifier of the schema to retrieve.
    :param manager: The schema manager the request's retirement scope selected.
    :return: The schema instance corresponding to the provided ``schema_id``.
    :raises HTTPNotFoundException: If no schema in scope has the given
        ``schema_id``.
    """
    return await manager.get_or_404(session, id=schema_id)


InScopeNodeDep = Annotated[Node, Depends(get_in_scope_node)]
InScopeServiceDep = Annotated[Service, Depends(get_in_scope_service)]
InScopeSchemaDep = Annotated[Schema, Depends(get_in_scope_schema)]


async def get_host_system_observation(
    session: SessionDep, node: NodeDep
) -> HostSystemObservation:
    """Retrieve the host system observation collected for a node.

    A node that exists but was never visited by the system-facts syncer is a
    normal state, so raise with :data:`UNCOLLECTED_HOST_OBSERVATION_DETAIL`
    rather than through ``get_or_404()``, whose default detail is
    indistinguishable from the missing-node 404 that ``NodeDep`` raises first.

    :param session: The asynchronous database session.
    :param node: The resolved node the observation belongs to.
    :return: The host system observation collected for the node.
    :raises HTTPNotFoundException: If no observation has been collected yet.
    """
    observation = await HostSystemObservationManager.first(session, node_id=node.id)
    if observation is None:
        raise HTTPNotFoundException(detail=UNCOLLECTED_HOST_OBSERVATION_DETAIL)
    return observation


async def get_service_system_observation(
    session: SessionDep, service: ServiceDep
) -> ServiceSystemObservation:
    """Retrieve the service system observation collected for a service.

    Service-level counterpart of :func:`get_host_system_observation`, raising
    :data:`UNCOLLECTED_SERVICE_OBSERVATION_DETAIL` for the same reason.

    :param session: The asynchronous database session.
    :param service: The resolved service the observation belongs to.
    :return: The service system observation collected for the service.
    :raises HTTPNotFoundException: If no observation has been collected yet.
    """
    observation = await ServiceSystemObservationManager.first(
        session, service_id=service.id
    )
    if observation is None:
        raise HTTPNotFoundException(detail=UNCOLLECTED_SERVICE_OBSERVATION_DETAIL)
    return observation


HostSystemObservationDep = Annotated[
    HostSystemObservation, Depends(get_host_system_observation)
]
ServiceSystemObservationDep = Annotated[
    ServiceSystemObservation, Depends(get_service_system_observation)
]

NodeListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(NodeManager))]
ServiceListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(ServiceManager))]
SchemaListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(SchemaManager))]
TableListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(TableManager))]
