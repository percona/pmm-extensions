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

"""Delete the inventory tombstones no persisted reference resolves any more.

A tombstoned inventory entity may be deleted when all three of these hold:

1. **Age.** It has been retired for at least ``COLLECTION_RETENTION``.
2. **No live referent.** No artifact that can still *resolve or re-emit* its id
   survives. For a service that means: no ``MysqlBackupRun`` row, no ``Task.data``
   meta (soft-deleted tasks included), no in-flight ``TaskHistory``, and no
   Celery-beat ``PeriodicTask.kwargs`` naming it. Nodes, schemas and tables have
   no external persisted holder at all, so this clause is vacuous for them.
3. **Subtree safety.** Every descendant in its retirement subtree is itself
   collectible — enforced by the Inventory API, which owns the walk.

Purely *historical* references do not block collection. A terminal
``TaskHistory.execution_request`` meta is the only such holder: it can never
create a new dangling reference, and it already carries ``_service_name``
unconditionally. The one path that resolves its id — the legacy form backfill —
reads inventory through the retired-hiding default manager, so a retired
service's id stops matching there the moment it is tombstoned and deleting the
row later changes nothing. An *in-flight* history is not historical and does
block, because it can still complete and write a new ``MysqlBackupRun`` row
naming the id.

The scan and the delete are not one transaction. For every holder that already
existed when the scan ran the window is closed: ``app/tasks/run_result.py``
resolves a recorder off the live ``Task`` row, so a recorder firing mid-window
belongs to a task the scan already read — which is why the ``Task`` scan reads
every row rather than only the active ones.

A holder persisted *during* the window is not covered. The Tasks API takes
arbitrary envelope meta, so a task or beat schedule created between a batch's
dry run and its delete can name an id that batch removes, and a later run of it
would record a backup run pointing at a service the catalog can no longer
resolve. Fencing that off needs a lease or epoch spanning four databases; it is
left open instead, bounded by how narrow it is — the id has to have been
tombstoned for at least ``COLLECTION_RETENTION``, and the write has to land
between two consecutive calls of one batch.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.celery.db import get_async_session_maker as get_beat_session_maker
from app.core.security import get_internal_token
from app.core.utils.date_time import utc_now
from app.extensions.apps.framework.registry import collect_inventory_reference_providers
from app.extensions.apps.inventory.config import inventory_app_settings
from app.extensions.apps.inventory.deps import get_inventory_api_standalone
from app.extensions.apps.meta_keys import SERVICE_ID_META_KEY
from app.extensions.crud import SyncEntityAbsenceManager
from app.extensions.db import get_async_session_maker as get_extensions_session_maker
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.constants import RetirableEntityName
from app.inventory.models import InventoryCollectResponse
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.db import get_async_session_maker as get_tasks_session_maker
from app.tasks.periodic.crud import PeriodicTaskManager

logger = logging.getLogger(__name__)

COLLECT_PATH = "/collection/collect"

#: The ledger's entity-type spelling for each collectable inventory entity. The
#: two enums are deliberately separate — one is PMM Extensions', one is the Inventory
#: service's — so the crossing is written down once, here.
_LEDGER_ENTITY_TYPES: Mapping[RetirableEntityName, SyncInventoryEntityTypeEnum] = {
    RetirableEntityName.NODE: SyncInventoryEntityTypeEnum.NODE,
    RetirableEntityName.SERVICE: SyncInventoryEntityTypeEnum.SERVICE,
    RetirableEntityName.SCHEMA: SyncInventoryEntityTypeEnum.SCHEMA,
    RetirableEntityName.TABLE: SyncInventoryEntityTypeEnum.TABLE,
}


def _positive_ids(values: Iterable[Any]) -> set[int]:
    """Coerce raw JSON-extracted values to the positive integers among them.

    The extraction crosses a dialect boundary — PostgreSQL hands back text while
    SQLite hands back the native scalar — and the source is a free-form JSON
    blob, so anything that is not a positive integer is a malformed envelope
    rather than an error to raise on.

    :param values: The raw values read out of a ``meta`` key.
    :return: The positive integers among them.
    """
    ids: set[int] = set()
    for value in values:
        try:
            entity_id = int(value)
        except (TypeError, ValueError):
            logger.debug(
                "Ignoring non-integer %s meta value %r", SERVICE_ID_META_KEY, value
            )
            continue
        if entity_id > 0:
            ids.add(entity_id)
    return ids


def _beat_kwargs_service_ids(payloads: Iterable[str | None]) -> set[int]:
    """Extract the service ids the beat schedules still carry.

    ``PeriodicTask.kwargs`` is a JSON *string* column declared by
    ``sqlalchemy-celery-beat``, so the payload is parsed in Python rather than by
    a JSON operator. Every level is ``isinstance``-guarded: the column is
    operator-populated free text, so a malformed row is skipped rather than
    raised on. The extracted leaves are gathered into a list rather than a set
    for the same reason — the value is unconstrained JSON and may be unhashable,
    and judging it is :func:`_positive_ids`'s job.

    Only the rows :class:`PeriodicTaskManager` surfaces reach here, which is the
    ``execute_task_by_name`` indirection. That is the sole beat shape carrying an
    ``execution_data`` envelope, so a row pointing straight at a Celery function
    cannot name a service id in the first place.

    :param payloads: The raw ``kwargs`` strings of the beat schedules.
    :return: The service ids named by their envelopes.
    """
    ids: list[Any] = []
    for payload in payloads:
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Skipping periodic-task kwargs that is not valid JSON")
            continue
        if not isinstance(decoded, dict):
            continue
        execution_data = decoded.get("execution_data")
        meta = execution_data.get("meta") if isinstance(execution_data, dict) else None
        if isinstance(meta, dict) and SERVICE_ID_META_KEY in meta:
            ids.append(meta[SERVICE_ID_META_KEY])
    return _positive_ids(ids)


async def collect_task_envelope_service_ids() -> set[int]:
    """Return every inventory service id a task envelope can still re-emit.

    Three reads, in two databases. The first two are what closes the scan-to-
    delete window; the third covers a schedule that has not fired yet.

    A read that cannot complete propagates rather than returning what it managed
    to gather: a holder that could not be read is not a holder that is empty, and
    treating it as empty would collect the entities it names.

    :return: The service ids the task envelopes still name.
    :raises Exception: Whatever a database this reads raises, unchanged.
    """
    tasks_session_maker = get_tasks_session_maker()
    async with tasks_session_maker() as session:
        service_ids = _positive_ids(
            await TaskManager.envelope_meta_values(session, SERVICE_ID_META_KEY)
        )
        service_ids |= _positive_ids(
            await TaskHistoryManager.in_flight_meta_values(session, SERVICE_ID_META_KEY)
        )
    beat_session_maker = get_beat_session_maker()
    async with beat_session_maker() as session:
        service_ids |= _beat_kwargs_service_ids(
            await PeriodicTaskManager.values_list(session, ["kwargs"])
        )
    return service_ids


async def collect_referenced_entities() -> dict[RetirableEntityName, set[int]]:
    """Return every inventory id PMM Extensions still holds a resolvable reference to.

    Any failure propagates: issuing the delete with a partial retained set would
    collect an entity something still resolves, which is exactly the outcome this
    job exists to prevent.

    :return: The referenced ids, keyed by inventory entity type.
    :raises Exception: Whatever a declared provider or the task-envelope scan
        raises, unchanged.
    """
    referenced = {
        RetirableEntityName.SERVICE: await collect_task_envelope_service_ids()
    }
    extensions_session_maker = get_extensions_session_maker()
    async with extensions_session_maker() as session:
        for provider in collect_inventory_reference_providers():
            for entity_name, entity_ids in (await provider(session)).items():
                referenced.setdefault(entity_name, set()).update(entity_ids)
    return referenced


async def clear_absence_ledger(
    collected: Mapping[RetirableEntityName, list[int]],
) -> None:
    """Drop the missing-grace ledger rows of the entities about to be deleted.

    Cleared *before* the delete rather than after, because that is the only
    order with no crash window, and it is safe: the syncer short-circuits on an
    already-retired entity before it can record a fresh absence, so an early
    clear cannot be undone, and clearing a superset is harmless.

    Cleared for every syncer rather than the configured ones, which is the same
    licence read the other way: a ledger row outlives the configuration that
    wrote it, so scoping the delete to ``EXTENSIONS.SYNCERS`` would leave the rows of a
    syncer since removed or renamed behind permanently, with the entity they
    name gone and nothing left that could ever reach them.

    :param collected: The ids the dry run reported, per entity type.
    """
    extensions_session_maker = get_extensions_session_maker()
    async with extensions_session_maker() as session:
        for entity_name, entity_ids in collected.items():
            if not entity_ids:
                continue
            await SyncEntityAbsenceManager.clear(
                session,
                None,
                _LEDGER_ENTITY_TYPES[entity_name],
                *entity_ids,
            )


def _parse_batch(
    payload: Any,
) -> tuple[dict[RetirableEntityName, list[int]], bool]:
    """Read one collection response into its collected ids and remaining flag.

    Parsed through the response model the Inventory API answers with, so every
    level of the body is checked rather than only its outermost type.

    :param payload: The decoded JSON body the Inventory API answered with.
    :return: The collected ids per entity type, and whether more are waiting.
    :raises ValidationError: If the body is not the documented object, or names
        an entity type this service does not know.
    """
    parsed = InventoryCollectResponse.model_validate(payload)
    return parsed.deleted, parsed.remaining


async def run_inventory_collection(api_key: str) -> None:
    """Collect tombstoned inventory entities no live reference resolves.

    Each batch calls the Inventory API twice with the same ``retired_before`` and
    retained set: a dry run naming the ids, then — once their ledger rows are
    gone — the real delete. The cutoff is computed once for the whole run so
    successive batches cannot drift.

    :param api_key: The internal token authenticating the Inventory API calls.
    :raises HTTPException: Whatever :meth:`RemoteAPI.post` raises for a non-2xx
        answer from the Inventory API — an expired token or a restarting service
        is the likeliest runtime failure of this job.
    :raises ValidationError: If the Inventory API answers a collect call with
        something other than the documented object, or names an unknown entity
        type.
    :raises Exception: Whatever computing the retained set raises, unchanged —
        deleting against a partial retained set is the failure this prevents.
    """
    retired_before = utc_now() - inventory_app_settings.COLLECTION_RETENTION
    keep = {
        name.value: sorted(entity_ids)
        for name, entity_ids in (await collect_referenced_entities()).items()
    }
    client = await get_inventory_api_standalone()
    body = {
        "retired_before": retired_before.isoformat(),
        "keep": keep,
        "limit": inventory_app_settings.COLLECTION_BATCH_SIZE,
        "dry_run": False,
    }
    with client.auth(api_key):
        async with client.hold():
            for batch in range(inventory_app_settings.COLLECTION_MAX_BATCHES):
                candidates, _ = _parse_batch(
                    await client.post(COLLECT_PATH, json={**body, "dry_run": True})
                )
                await clear_absence_ledger(candidates)
                deleted, remaining = _parse_batch(
                    await client.post(COLLECT_PATH, json=body)
                )
                logger.info(
                    "Inventory collection batch %s deleted %s entities",
                    batch + 1,
                    sum(len(ids) for ids in deleted.values()),
                )
                if not remaining:
                    return
    logger.info(
        "Inventory collection stopped at its %s-batch cap; the next run continues.",
        inventory_app_settings.COLLECTION_MAX_BATCHES,
    )


async def run_scheduled_inventory_collection() -> None:
    """Run inventory collection using the configured internal token.

    :raises HTTPException: Whatever the Inventory API's non-2xx answers raise.
    :raises ValidationError: If the Inventory API answers a collect call with
        something other than the documented object.
    :raises Exception: Whatever computing the retained set raises, unchanged.
        Every failure is logged and re-raised so the Celery run records it and
        the task's failure alert fires; aborting deletes nothing, except in the
        window between clearing a batch's ledger rows and its delete.
    """
    try:
        await run_inventory_collection(get_internal_token())
    except Exception:
        logger.exception("Scheduled inventory collection failed")
        raise
