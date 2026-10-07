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

"""Provide synchronization functions for the PMM Extensions inventory."""

import logging
from collections.abc import Sequence

from kombu.exceptions import KombuError

from app.celery import celery
from app.core.security import get_internal_token
from app.extensions.apps.inventory.deps import (
    filter_syncers_by_name,
    get_syncers_standalone,
)
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.db import get_async_session_maker
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
)
from app.extensions.sync.exceptions import SyncInstanceAlreadyInProgressError
from app.extensions.sync.models import BaseSyncer
from app.tasks.models import (
    EXECUTE_TASK_BY_NAME_TASK,
    INVENTORY_SYNC_FIRST_RUN_KEY,
    INVENTORY_SYNC_TASK_NAME,
)

logger = logging.getLogger(__name__)


async def run_scheduled_inventory_sync(
    syncer: str | None = None,
    after_syncer: str | None = None,
    follower_syncers: Sequence[str] = (),
    *,
    first_run_only: bool = False,
) -> str | None:
    """Execute scheduled inventory sync using configured internal token and syncers.

    Read the internal token from ``settings.EXTENSIONS_INTERNAL_TOKEN`` and construct
    syncers from application settings. When ``syncer`` is set, only that syncer
    runs; when ``None`` or empty, every configured syncer runs.

    Designed for the CeleryExecutor: the executor forwards
    ``execution_request.meta`` from the periodic-task row as ``**kwargs``, so a
    row whose meta is ``{"syncer": "<qualified>"}`` resolves to the targeted
    single-syncer path while a row with empty meta resolves to the sync-all
    path.

    The tasks seeder names the per-syncer schedules on the pinned default's row,
    forwarded here as ``follower_syncers``. Each follower runs on its own
    schedule from bring-up and never waits on the default; after each run of the
    default, :func:`start_followers` starts any follower that has not run since
    the default's first completed pass, so its next useful run reads the
    inventory that pass produced.

    A started run skips, rather than fails, when another run of its syncer is
    already in progress, and returns a note saying so, which the executor writes
    to that run's log, so a run that synced nothing does not read as one that
    synced.

    :param syncer: Fully qualified syncer name (e.g.
        ``"app.extensions.sync.syncers.pmm.PMMSyncer"``), or ``None`` / empty for the
        sync-all path.
    :param after_syncer: Ignored. A request queued by an earlier build, whose
        per-syncer schedules waited on the pinned default, still carries it, and
        rejecting the keyword would fail that run.
    :param follower_syncers: The per-syncer schedules to start after this run
        when they have not run since this syncer's first completed pass. Defaults
        to none.
    :param first_run_only: Whether this run was started by the pinned default,
        and so skips when another run of ``syncer`` is in progress. Defaults to
        ``False``.
    :return: The note saying why the run was skipped, otherwise ``None``.
    :raises ValueError: If ``syncer`` matches no configured syncer able to sync
        inventory.
    :raises sqlalchemy.exc.SQLAlchemyError: When the PMM Extensions database cannot be read
        to decide which followers to start.
    :raises app.extensions.sync.exceptions.SyncInstanceAlreadyInProgressError: If a run
        that was not started by the pinned default overlaps another run of its
        syncer.
    """
    del after_syncer
    syncers = await get_syncers_standalone()
    selected = filter_syncers_by_name(
        syncers,
        syncer,
        lambda candidate: candidate.can_sync_inventory(),
    )
    try:
        await run_inventory_sync(get_internal_token(), *selected)
    except SyncInstanceAlreadyInProgressError:
        if not first_run_only:
            raise
        return _report_skip(
            f"Skipped the started run of {syncer}: another of its runs is already "
            "in progress."
        )
    if syncer and follower_syncers:
        await start_followers(syncer, follower_syncers, syncers)
    return None


def _report_skip(note: str) -> str:
    """Log why a scheduled run was skipped and return the note for its task log.

    :param note: Why the run was skipped.
    :return: ``note``, which the executor writes to the run's log.
    """
    logger.info("%s", note)
    return note


async def start_followers(
    leader: str, followers: Sequence[str], syncers: list[BaseSyncer]
) -> None:
    """Start each follower of ``leader`` that has not run since its first pass.

    Nothing starts until ``leader`` has completed a whole-inventory pass. A
    follower with a run that began after that pass has read its inventory and is
    left to its own schedule; one whose runs all began earlier, on the inventory
    of a fresh install, is started. The decision reads the recorded runs rather
    than the outcome of the run that called it, so a first pass made through the
    manual sync endpoint, or a start whose enqueue failed, is caught up by the
    leader's next run.

    The start carries the follower's beat-row meta plus the started-run flag.
    The identical-task guard matches meta by containment, so the follower's beat
    fire is held back while the start is in flight, and a start that meets a run
    of the follower already in progress is skipped. An enqueue failure is logged
    rather than raised.

    :param leader: The fully qualified name of the syncer the followers follow.
    :param followers: The fully qualified names of the followers to consider.
    :param syncers: The configured syncers, which a follower must resolve against.
    :raises sqlalchemy.exc.SQLAlchemyError: When the PMM Extensions database cannot be read.
    """
    async with get_async_session_maker()() as session:
        first_pass_at = await SyncItemManager.first_inventory_sync_completed_at(
            session, leader
        )
        if first_pass_at is None:
            return
        behind = [
            follower
            for follower in followers
            if not await SyncInstanceManager.has_run_since(
                session, follower, first_pass_at
            )
        ]
    for follower in behind:
        try:
            filter_syncers_by_name(
                syncers, follower, lambda candidate: candidate.can_sync_inventory()
            )
        except ValueError:
            logger.warning(
                "Not starting %s after %s: it is not a configured inventory syncer",
                follower,
                leader,
            )
            continue
        try:
            celery.send_task(
                EXECUTE_TASK_BY_NAME_TASK,
                kwargs={
                    "task_name": INVENTORY_SYNC_TASK_NAME,
                    "execution_data": {
                        "meta": {"syncer": follower, INVENTORY_SYNC_FIRST_RUN_KEY: True}
                    },
                },
            )
        except (OSError, KombuError):
            logger.exception(
                "Could not start %s after %s's first inventory sync", follower, leader
            )


async def run_inventory_sync(api_key: str, *syncers: BaseSyncer) -> None:
    """Execute inventory synchronization using the provided syncers.

    Iterates over each ``BaseSyncer`` instance and invokes the ``sync_inventory`` method
    to perform inventory synchronization tasks asynchronously.

    :param syncers: One or more instances of ``BaseSyncer`` to perform inventory
        synchronization.
    :type syncers: BaseSyncer
    """
    for syncer in syncers:
        async with syncer.api_auth(api_key) as sync:
            await sync.sync_inventory()


async def run_node_sync(
    created_node: CreatedNode,
    api_key: str,
    *syncers: BaseSyncer,
) -> None:
    """Execute node synchronization for a created node using the provided syncers.

    Iterates over each ``BaseSyncer`` instance and invokes the ``sync_node`` method
    with the specified ``CreatedNode`` to perform node synchronization tasks
    asynchronously.

    :param created_node: The node that has been created and needs to be synchronized.
    :type created_node: CreatedNode
    :param syncers: One or more instances of ``BaseSyncer`` to perform node
        synchronization.
    :type syncers: BaseSyncer
    """
    for syncer_index, syncer in enumerate(syncers):
        async with syncer.api_auth(api_key) as sync:
            await sync.sync_node(created_node, refresh_at_start=bool(syncer_index))


async def run_service_sync(
    created_service: CreatedService,
    api_key: str,
    *syncers: BaseSyncer,
) -> None:
    """Execute service synchronization for a created service using the provided syncers.

    Iterates over each ``BaseSyncer`` instance and invokes the ``sync_service`` method
    with the specified ``CreatedService`` to perform service synchronization tasks
    asynchronously.

    :param created_service: The service that has been created and needs to be
        synchronized.
    :type created_service: CreatedService
    :param syncers: One or more instances of ``BaseSyncer`` to perform service
        synchronization.
    :type syncers: BaseSyncer
    """
    for syncer_index, syncer in enumerate(syncers):
        async with syncer.api_auth(api_key) as sync:
            await sync.sync_service(
                created_service,
                refresh_at_start=bool(syncer_index),
            )


async def run_schema_sync(
    created_schema: CreatedSchema,
    api_key: str,
    *syncers: BaseSyncer,
) -> None:
    """Execute schema synchronization for a created schema using the provided syncers.

    Iterates over each ``BaseSyncer`` instance and invokes the ``sync_schema`` method
    with the specified ``CreatedSchema`` to perform schema synchronization tasks
    asynchronously.

    :param created_schema: The schema that has been created and needs to be
        synchronized.
    :type created_schema: CreatedSchema
    :param syncers: One or more instances of ``BaseSyncer`` to perform schema
        synchronization.
    :type syncers: BaseSyncer
    """
    for syncer_index, syncer in enumerate(syncers):
        async with syncer.api_auth(api_key) as sync:
            await sync.sync_schema(created_schema, refresh_at_start=bool(syncer_index))


async def run_table_sync(
    created_table: CreatedTable,
    api_key: str,
    *syncers: BaseSyncer,
) -> None:
    """Execute table synchronization for a created table using the provided syncers.

    Iterates over each ``BaseSyncer`` instance and invokes the ``sync_table`` method
    with the specified ``CreatedTable`` to perform table synchronization tasks
    asynchronously.

    :param created_table: The table that has been created and needs to be synchronized.
    :type created_table: CreatedTable
    :param syncers: One or more instances of ``BaseSyncer`` to perform table
        synchronization.
    :type syncers: BaseSyncer
    """
    for syncer_index, syncer in enumerate(syncers):
        async with syncer.api_auth(api_key) as sync:
            await sync.sync_table(created_table, refresh_at_start=bool(syncer_index))
