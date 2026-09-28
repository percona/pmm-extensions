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

"""Define shared fixtures for sync tests."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest_asyncio
from pydantic import UUID4
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.extensions.crud import SyncInstanceManager
from app.extensions.inventory import CreatedNode
from tests.app.db_schema import apply_schema

#: The path suffix every per-entity sync-health write carries.
SYNC_HEALTH_PATH_SUFFIX = "/sync-health"


def sync_health_posts(inventory_api: AsyncMock) -> list[tuple[str, dict[str, Any]]]:
    """Return the path and body of every sync-health POST the syncer issued.

    :param inventory_api: The mocked inventory client the syncer posts through.
    :return: One entry per sync-health write, in the order they were issued.
    """
    return [
        (call.args[0], call.kwargs["json"])
        for call in inventory_api.post.await_args_list
        if call.args and call.args[0].endswith(SYNC_HEALTH_PATH_SUFFIX)
    ]


def entity_posts(inventory_api: AsyncMock) -> list[str]:
    """Return the paths of the POSTs that are not sync-health bookkeeping.

    Sync-health writes ride the same client as the creates and revives a syncer
    issues, so a test asserting on those has to say which POSTs it means.

    :param inventory_api: The mocked inventory client the syncer posts through.
    :return: The remaining POST paths, in the order they were issued.
    """
    return [
        call.args[0]
        for call in inventory_api.post.await_args_list
        if call.args and not call.args[0].endswith(SYNC_HEALTH_PATH_SUFFIX)
    ]


def local_nodes_payload(*created_nodes: CreatedNode) -> dict[str, Any]:
    """Build the paginated inventory-API payload listing ``created_nodes``.

    :param created_nodes: The nodes the inventory reports, in order.
    :return: One page holding every node.
    """
    return {
        "items": [node.model_dump() for node in created_nodes],
        "total": len(created_nodes),
        "offset": 0,
        "limit": 50,
    }


async def close_run(
    session: AsyncSession, sync_instance_id: UUID4, *, snapshot_complete: bool | None
) -> None:
    """Close a run the way ``BaseSyncer.__aexit__`` does, keeping the session open.

    Without it a following run on the same session is refused as concurrent.

    :param session: The session the run wrote through.
    :param sync_instance_id: The run to close.
    :param snapshot_complete: The completeness verdict to record on the run.
    """
    await SyncInstanceManager.finish_hanging_items(session, sync_instance_id)
    await SyncInstanceManager.finalize_run(
        session, sync_instance_id, failed=False, snapshot_complete=snapshot_complete
    )


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async db session for testing."""
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
