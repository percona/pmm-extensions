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

"""Define the route deleting the tombstones nothing refers to any more.

The inventory service owns the *mechanism* only. It cannot decide whether a
tombstone is still referenced — every persisted holder of an inventory id lives
in a database this service must not reach into — so the caller supplies the
retained ids and this route enforces the rest of the safety condition: the age
cutoff, the subtree walk, and the retired-inclusive access that alone can reach
a tombstone.
"""

import logging

from fastapi import APIRouter

from app.api.deps import IsAuthenticatedDep
from app.inventory.constants import RetirableEntityName
from app.inventory.crud import collect_retirable_entities
from app.inventory.deps import SessionDep
from app.inventory.models import InventoryCollectResponse, InventoryCollectWrite

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collection", tags=["collection"])


def _log_collected(name: RetirableEntityName, collected: int) -> None:
    """Log the rows one entity type's delete removed.

    :param name: The entity type the delete ran on.
    :param collected: The number of rows it removed.
    """
    logger.info("Collected %s retired %s entities", collected, name)


@router.post("/collect", dependencies=[IsAuthenticatedDep])
async def collect_retired_entities(
    session: SessionDep, body: InventoryCollectWrite
) -> InventoryCollectResponse:
    """Delete the tombstones the caller's retained set does not cover.

    Entities are walked deepest-first, so an interrupted run never leaves a
    live row beneath a deleted ancestor. A type that fills its ``limit`` ends
    the walk, so ``deleted`` is exhaustive and ``remaining`` asks for the next
    batch.

    :param session: The asynchronous database session.
    :param body: The cutoff, the retained ids, and the batch controls.
    :return: The collected ids per entity type, and whether more are waiting.
    """
    batch = await collect_retirable_entities(
        session,
        retired_before=body.retired_before,
        keep=body.keep,
        limit=body.limit,
        dry_run=body.dry_run,
        on_collected=_log_collected,
    )
    return InventoryCollectResponse(deleted=batch.deleted, remaining=batch.remaining)
