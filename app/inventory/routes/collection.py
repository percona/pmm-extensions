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
from app.inventory.crud import COLLECTION_ORDER
from app.inventory.deps import SessionDep
from app.inventory.models import InventoryCollectResponse, InventoryCollectWrite

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collection", tags=["collection"])


@router.post("/collect", dependencies=[IsAuthenticatedDep])
async def collect_retired_entities(
    session: SessionDep, body: InventoryCollectWrite
) -> InventoryCollectResponse:
    """Delete the tombstones the caller's retained set does not cover.

    Entities are walked deepest-first — table, schema, service, node — so an
    interrupted run can only leave deleted descendants under a surviving
    ancestor rather than an orphan.

    A type that fills its ``limit`` ends the walk. Deleting an ancestor cascades
    to descendants the cap had excluded, and those ids would then be missing from
    ``deleted`` — leaving the caller unable to clear their bookkeeping and making
    the reported set a false record of what was removed. Stopping keeps
    ``deleted`` exhaustive; the ancestors are collected on the next batch, which
    ``remaining`` asks for.

    :param session: The asynchronous database session.
    :param body: The cutoffs, the retained ids, and the batch controls.
    :return: The collected ids per entity type, and whether more are waiting.
    """
    keep_by_model = {
        manager.Model: body.keep.get(name, ()) for name, manager in COLLECTION_ORDER
    }
    deleted = {name: [] for name, _ in COLLECTION_ORDER}
    remaining = False
    for name, manager in COLLECTION_ORDER:
        entity_ids = await manager.collectible_ids(
            session,
            retired_before=body.retired_before,
            link_pin_retired_before=body.link_pin_retired_before,
            keep_by_model=keep_by_model,
            limit=body.limit,
        )
        deleted[name] = entity_ids
        if entity_ids and not body.dry_run:
            collected = await manager.collect(session, entity_ids)
            logger.info("Collected %s retired %s entities", collected, name)
        if len(entity_ids) >= body.limit:
            remaining = True
            break
    return InventoryCollectResponse(deleted=deleted, remaining=remaining)
