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

"""Define the routes for the Tables resource."""

import logging

from fastapi import APIRouter, status

from app.api.deps import IsAuthenticatedDep, IsServicePrincipalDep
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.inventory.crud import TableManager
from app.inventory.deps import (
    RetirableTableDep,
    SessionDep,
    TableDep,
    TableListQueryDep,
    TableScopeDep,
)
from app.inventory.models import (
    SyncHealthBulkResult,
    SyncHealthBulkWrite,
    SyncHealthWrite,
    Table,
    TableDetailResponse,
    TableResponse,
    TableWrite,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tables", tags=["tables"])


@router.get("/", dependencies=[IsAuthenticatedDep])
async def list_tables(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: TableListQueryDep,
    manager: TableScopeDep,
) -> PaginatedResponse[TableResponse]:
    """List Tables."""
    logger.debug("Listing tables")
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        pagination=pagination,
    )


@router.get("/{table_id}", dependencies=[IsAuthenticatedDep])
async def retrieve_table(
    session: SessionDep,
    table_id: int,
    manager: TableScopeDep,
) -> TableDetailResponse:
    """Retrieve Table.

    :param session: The async database session.
    :param table_id: The identifier of the table to retrieve.
    :param manager: The table manager the request's retirement scope selected.
    :return: The table, with its schema nested.
    :raises HTTPNotFoundException: If no table in scope has the given identifier.
    """
    logger.debug("Retrieving table %s", table_id)
    return await manager.get_or_404(
        session,
        select_related=[Table.database],
        id=table_id,
    )


@router.put("/{table_id}", dependencies=[IsAuthenticatedDep])
async def update_table(
    session: SessionDep,
    existing_table: TableDep,
    updated_table: TableWrite,
) -> Table:
    """Update Table."""
    logger.debug("Updating table %s", existing_table.id)
    return await TableManager.update(session, existing_table, updated_table)


@router.delete(
    "/{table_id}",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def retire_table(session: SessionDep, table: RetirableTableDep) -> None:
    """Retire Table, keeping the row resolvable.

    :param session: The asynchronous database session.
    :param table: The table to retire, retired or not.
    """
    logger.debug("Retiring table %s", table.id)
    await TableManager.retire(session, table)


@router.post(
    "/{table_id}/revive",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revive_table(session: SessionDep, table: RetirableTableDep) -> None:
    """Revive a retired Table together with its retired ancestors.

    :param session: The asynchronous database session.
    :param table: The table to revive, retired or not.
    :raises HTTPConflictException: If an active entity already holds the unique
        key the revived table would reclaim.
    """
    logger.debug("Reviving table %s", table.id)
    await TableManager.revive(session, table)


@router.post("/sync-health", dependencies=[IsServicePrincipalDep])
async def record_tables_sync_health(
    session: SessionDep,
    outcomes: SyncHealthBulkWrite,
) -> SyncHealthBulkResult:
    """Record the outcomes of several syncer attempts, one per Table.

    Each entry gets the transition ``/{table_id}/sync-health`` gives it, retired
    tables included; an id naming no table is skipped and returned rather
    than rejecting the rest of the list.

    :param session: The async database session.
    :param outcomes: What the syncer reported, one entry per table.
    :return: The ids that named no table.
    """
    logger.debug("Recording sync health on %d tables", len(outcomes))
    return await TableManager.record_sync_health_bulk(session, outcomes)


@router.post(
    "/{table_id}/sync-health",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_table_sync_health(
    session: SessionDep,
    table: RetirableTableDep,
    outcome: SyncHealthWrite,
) -> None:
    """Record the outcome of one syncer attempt on a Table.

    Addresses the table whether retired or not: the attempt happened, and a
    concurrent retirement must not turn bookkeeping into a failed sync item.

    :param session: The async database session.
    :param table: The table the outcome was observed for, retired or not.
    :param outcome: What the syncer reported.
    """
    logger.debug("Recording %s sync health on table %s", outcome.outcome, table.id)
    await TableManager.record_sync_health(session, table, outcome)
