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

"""Define the routes for the Schemas resource."""

import logging

from fastapi import APIRouter, status

from app.api.deps import IsAuthenticatedDep, IsServicePrincipalDep
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.inventory.crud import (
    SchemaManager,
    TableManager,
)
from app.inventory.deps import (
    InScopeSchemaDep,
    RetirableSchemaDep,
    SchemaDep,
    SchemaListQueryDep,
    SchemaScopeDep,
    SessionDep,
    TableListQueryDep,
    TableScopeDep,
)
from app.inventory.models import (
    Schema,
    SchemaDetailResponse,
    SchemaResponse,
    SchemaWrite,
    SyncHealthWrite,
    Table,
    TableResponse,
    TableWrite,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schemas", tags=["schemas"])


@router.get("/", dependencies=[IsAuthenticatedDep])
async def list_schemas(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: SchemaListQueryDep,
    manager: SchemaScopeDep,
) -> PaginatedResponse[SchemaResponse]:
    """List Schemas.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request boundary.
    :param manager: The schema manager the request's retirement scope selected.
    :return: A paginated response of schema responses.
    """
    logger.debug("Listing schemas")
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        select_related=[Schema.tables],
        pagination=pagination,
    )


@router.get("/{schema_id}", dependencies=[IsAuthenticatedDep])
async def retrieve_schema(
    session: SessionDep,
    schema_id: int,
    manager: SchemaScopeDep,
) -> SchemaDetailResponse:
    """Retrieve Schema.

    :param session: The async database session.
    :param schema_id: The identifier of the schema to retrieve.
    :param manager: The schema manager the request's retirement scope selected.
    :return: The schema, with its tables and service nested.
    :raises HTTPNotFoundException: If no schema in scope has the given identifier.
    """
    logger.debug("Retrieving schema %s", schema_id)
    return await manager.get_or_404(
        session,
        select_related=[Schema.tables, Schema.service],
        id=schema_id,
    )


@router.put("/{schema_id}", dependencies=[IsAuthenticatedDep])
async def update_schema(
    session: SessionDep,
    existing_schema: SchemaDep,
    updated_schema: SchemaWrite,
) -> Schema:
    """Update Schema."""
    logger.debug("Updating schema %s", existing_schema.id)
    return await SchemaManager.update(session, existing_schema, updated_schema)


@router.delete(
    "/{schema_id}",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def retire_schema(session: SessionDep, schema: RetirableSchemaDep) -> None:
    """Retire Schema and its tables, keeping the rows resolvable.

    :param session: The asynchronous database session.
    :param schema: The schema to retire, retired or not.
    """
    logger.debug("Retiring schema %s", schema.id)
    await SchemaManager.retire(session, schema)


@router.post(
    "/{schema_id}/revive",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revive_schema(session: SessionDep, schema: RetirableSchemaDep) -> None:
    """Revive a retired Schema together with its retired ancestors.

    :param session: The asynchronous database session.
    :param schema: The schema to revive, retired or not.
    :raises HTTPConflictException: If an active entity already holds the unique
        key the revived schema would reclaim.
    """
    logger.debug("Reviving schema %s", schema.id)
    await SchemaManager.revive(session, schema)


@router.post(
    "/{schema_id}/sync-health",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_schema_sync_health(
    session: SessionDep,
    schema: RetirableSchemaDep,
    outcome: SyncHealthWrite,
) -> None:
    """Record the outcome of one syncer attempt on a Schema.

    Addresses the schema whether retired or not: the attempt happened, and a
    concurrent retirement must not turn bookkeeping into a failed sync item.

    :param session: The async database session.
    :param schema: The schema the outcome was observed for, retired or not.
    :param outcome: What the syncer reported.
    """
    logger.debug("Recording %s sync health on schema %s", outcome.outcome, schema.id)
    await SchemaManager.record_sync_health(session, schema, outcome)


@router.get("/{schema_id}/tables/", dependencies=[IsAuthenticatedDep])
async def list_tables_by_schema(
    session: SessionDep,
    schema: InScopeSchemaDep,
    pagination: PaginationDep,
    list_query: TableListQueryDep,
    manager: TableScopeDep,
) -> PaginatedResponse[TableResponse]:
    """List Tables by Schema.

    :param session: The async database session.
    :param schema: The schema addressed by the path, resolved within the
        request's retirement scope.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request
        boundary.
    :param manager: The table manager the request's retirement scope selected.
    :return: A paginated response of the schema's tables.
    """
    logger.debug("Listing tables for schema '%s'", schema.id)
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        pagination=pagination,
        schema_id=schema.id,
    )


@router.post(
    "/{schema_id}/tables/",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_201_CREATED,
)
async def create_table_for_schema(
    session: SessionDep,
    schema: SchemaDep,
    table: TableWrite,
) -> Table:
    """Create Table for Schema."""
    logger.debug("Creating table for schema %s: %s", schema.id, table)
    return await TableManager.create(session, table, schema_id=schema.id)
