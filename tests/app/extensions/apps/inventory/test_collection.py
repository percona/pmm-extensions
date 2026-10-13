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

"""Test the scheduled inventory-collection job."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
from typing import Any

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient, Response
from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine
from sqlalchemy_celery_beat.models import IntervalSchedule, Period, PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import AdvisoryLockLostError, AdvisoryLockTimeoutError
from app.extensions.apps.inventory import collection
from app.extensions.apps.inventory.collection import (
    run_inventory_collection,
    run_scheduled_inventory_collection,
)
from app.extensions.apps.meta_keys import SERVICE_ID_META_KEY
from app.extensions.apps.mysql_backups.crud import MysqlBackupRunManager
from app.extensions.apps.mysql_backups.inventory_references import (
    referenced_inventory_entities,
)
from app.extensions.apps.mysql_backups.models import MysqlBackupRun
from app.extensions.crud import SyncEntityAbsenceManager
from app.extensions.models import SyncEntityAbsenceWrite, SyncInventoryEntityTypeEnum
from app.inventory.constants import RetirableEntityName
from app.inventory.crud import (
    collect_retirable_entities,
    CollectionBatch,
    NodeManager,
    RetiredInclusiveNodeManager,
    RetiredInclusiveServiceManager,
    ServiceManager,
)
from app.inventory.models import Node, RetirableSQLModel, Service
from app.tasks import reference_fence
from app.tasks.crud import TaskManager
from app.tasks.models import EXECUTE_TASK_BY_NAME_TASK, TaskWrite
from app.tasks.periodic.crud import PeriodicTaskManager
from app.tasks.reference_fence import (
    hold_reference_write_fence,
    REFERENCE_FENCE_LOCK_NAMESPACE,
)
from tests.app.advisory_locks import isolate_reference_fence, wait_for_advisory_waiters
from tests.app.factories import NodeWriteFactory, ServiceWriteFactory, TaskFactory
from tests.app.tasks_api_client import tasks_api_client

BATCH_SIZE = 42
BATCH_CAP = 3
SYNCER = "app.extensions.sync.syncers.pmm.PMMSyncer"
NOW = datetime(2026, 6, 1, tzinfo=UTC)
#: Older than any retention a test configures, so every tombstone is eligible.
RETIRED_AT = datetime(2020, 1, 1, tzinfo=UTC)
#: Bounds every wait on a writer in the race tests, so a hang fails the test.
WRITER_WAIT = 10.0

Hook = Callable[[], Awaitable[None]]


def _session_maker(session: AsyncSession) -> Callable[[], Any]:
    """Return a session-maker factory yielding one already-built session.

    :param session: The session every ``async with`` block should receive.
    :return: A zero-argument callable standing in for a session maker.
    """

    @asynccontextmanager
    async def maker():
        yield session

    return maker


class CollectRecorder:
    """Delegate to the real inventory collect, recording each call's arguments.

    An injection point, not a replacement: every delete the job asks for still
    runs. The hooks let a test act at a precise point of a batch — just before
    its dry run, just before or after its real delete.

    :ivar calls: The keyword arguments of every call, in order.
    :ivar on_dry_run: Awaited before each dry run is delegated.
    :ivar on_delete: Awaited before each real delete is delegated.
    :ivar after_delete: Awaited after each real delete returns.
    """

    def __init__(self) -> None:
        """Start with no calls and no hooks."""
        self.calls: list[dict[str, Any]] = []
        self.on_dry_run: Hook | None = None
        self.on_delete: Hook | None = None
        self.after_delete: Hook | None = None

    async def __call__(self, session: AsyncSession, **kwargs: Any) -> CollectionBatch:
        """Record the call, run the matching hook, and delegate.

        :param session: The inventory session the job opened.
        :param kwargs: The job's keyword arguments, passed through unchanged.
        :return: What the real collect returned.
        """
        self.calls.append(kwargs)
        hook = self.on_dry_run if kwargs["dry_run"] else self.on_delete
        if hook is not None:
            await hook()
        batch = await collect_retirable_entities(session, **kwargs)
        if not kwargs["dry_run"] and self.after_delete is not None:
            await self.after_delete()
        return batch

    @property
    def dry_runs(self) -> list[dict[str, Any]]:
        """Return the calls issued with ``dry_run`` set."""
        return [call for call in self.calls if call["dry_run"]]

    @property
    def deletes(self) -> list[dict[str, Any]]:
        """Return the calls that actually delete."""
        return [call for call in self.calls if not call["dry_run"]]


async def _retire(session: AsyncSession, *entities: RetirableSQLModel) -> None:
    """Retire each entity in place at ``RETIRED_AT``.

    :param session: The session owning the entities.
    :param entities: The rows to mark retired.
    """
    for entity in entities:
        entity.retired_at = RETIRED_AT
        entity.retirement_key = entity.id
        session.add(entity)
    await session.commit()
    for entity in entities:
        await session.refresh(entity)


async def _node(session: AsyncSession, *, retired: bool) -> Node:
    """Create a node, tombstoned when asked.

    :param session: The inventory session.
    :param retired: Whether to retire the node.
    :return: The node.
    """
    node = await NodeManager.create(session, NodeWriteFactory.build())
    if retired:
        await _retire(session, node)
    return node


async def _retired_service(session: AsyncSession) -> Service:
    """Create a tombstoned service under a live node.

    The node stays live so the service is the only thing a batch can collect,
    which keeps the number of batches a run takes predictable.

    :param session: The inventory session.
    :return: The retired service.
    """
    node = await _node(session, retired=False)
    service = await ServiceManager.create(
        session, ServiceWriteFactory.build(), node_id=node.id
    )
    await _retire(session, service)
    return service


async def _node_exists(session: AsyncSession, node: Node) -> bool:
    """Return whether the node's row, tombstone included, is still there."""
    return await RetiredInclusiveNodeManager.exists(session, id=node.id)


async def _service_exists(session: AsyncSession, service: Service) -> bool:
    """Return whether the service's row, tombstone included, is still there."""
    return await RetiredInclusiveServiceManager.exists(session, id=service.id)


async def _create_holder_task(
    session: AsyncSession, name: str, service_id: int
) -> None:
    """Save a task whose envelope names a service.

    :param session: The tasks session.
    :param name: The task name.
    :param service_id: The service id to put in its meta.
    """
    await TaskManager.save(
        session,
        TaskFactory.build(
            name=name,
            data={"task": "run-command", "meta": {SERVICE_ID_META_KEY: service_id}},
            deleted_at=None,
        ),
    )


async def _create_beat_row(
    session: AsyncSession, task_name: str, meta: dict[str, Any]
) -> PeriodicTask:
    """Save a beat schedule running ``task_name`` with the given envelope meta.

    :param session: The celery-beat session.
    :param task_name: The tasks-database task the schedule runs.
    :param meta: The meta of its execution envelope.
    :return: The saved schedule.
    """
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    session.add(schedule)
    await session.flush()
    row = PeriodicTask(
        name=f"schedule-{task_name}",
        task=EXECUTE_TASK_BY_NAME_TASK,
        kwargs=json.dumps({"task_name": task_name, "execution_data": {"meta": meta}}),
        enabled=True,
        description="",
        schedule_model=schedule,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.fixture
def recorder(mocker: MockerFixture) -> CollectRecorder:
    """Route the job's inventory collect through a delegating recorder."""
    recorder = CollectRecorder()
    mocker.patch.object(collection, "collect_retirable_entities", recorder)
    return recorder


@pytest.fixture
def no_providers(mocker: MockerFixture) -> None:
    """Declare that no app contributes inventory references."""
    mocker.patch.object(collection, "collect_inventory_reference_providers", list)


@pytest.fixture
def task_databases(
    mocker: MockerFixture,
    session: AsyncSession,
    celery_beat_session: AsyncSession,
) -> None:
    """Point the built-in task-envelope scan and the fence at the test databases.

    Real but empty by default, so a test that wants an envelope in the retained
    set creates the row rather than replacing the scan.
    """
    mocker.patch.object(
        collection, "get_tasks_session_maker", return_value=_session_maker(session)
    )
    mocker.patch.object(
        collection,
        "get_beat_session_maker",
        return_value=_session_maker(celery_beat_session),
    )


@pytest.fixture
def extensions_database(mocker: MockerFixture, session: AsyncSession) -> None:
    """Point the job's PMM Extensions reads and ledger writes at the test database."""
    mocker.patch.object(
        collection, "get_extensions_session_maker", return_value=_session_maker(session)
    )


@pytest.fixture
def inventory_database(mocker: MockerFixture, session: AsyncSession) -> None:
    """Point the job's in-process collect at the test database's inventory tables."""
    mocker.patch.object(
        collection,
        "get_inventory_session_maker",
        return_value=_session_maker(session),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
class TestRunInventoryCollection:
    """Cover the batching, ordering and cutoff discipline of one run."""

    async def test_a_single_batch_ends_the_run(
        self, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Stop after one batch when nothing is left, having deleted the tombstone."""
        node = await _node(session, retired=True)

        await run_inventory_collection()

        assert len(recorder.dry_runs) == 1
        assert len(recorder.deletes) == 1
        assert not await _node_exists(session, node)

    async def test_the_run_continues_while_entities_remain(
        self, mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Issue another batch while the last one filled its limit."""
        mocker.patch.object(
            collection.inventory_app_settings, "COLLECTION_BATCH_SIZE", 1
        )
        nodes = [await _node(session, retired=True) for _ in range(2)]

        await run_inventory_collection()

        assert len(recorder.deletes) > 1
        for node in nodes:
            assert not await _node_exists(session, node)

    async def test_the_run_stops_at_the_batch_cap(
        self, mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """End normally at the cap, leaving the rest for the next run."""
        mocker.patch.object(
            collection.inventory_app_settings, "COLLECTION_MAX_BATCHES", BATCH_CAP
        )
        mocker.patch.object(
            collection.inventory_app_settings, "COLLECTION_BATCH_SIZE", 1
        )
        nodes = [await _node(session, retired=True) for _ in range(BATCH_CAP + 2)]

        await run_inventory_collection()

        assert len(recorder.deletes) == BATCH_CAP
        surviving = [node for node in nodes if await _node_exists(session, node)]
        assert len(surviving) == len(nodes) - BATCH_CAP

    async def test_the_cutoffs_are_pinned_for_the_whole_run(
        self, mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Pass one identical pair of cutoffs on every call of a multi-batch run."""
        mocker.patch.object(
            collection.inventory_app_settings, "COLLECTION_BATCH_SIZE", 1
        )
        for _ in range(2):
            await _node(session, retired=True)

        await run_inventory_collection()

        assert len(recorder.deletes) > 1
        assert len({call["retired_before"] for call in recorder.calls}) == 1
        assert len({call["link_pin_retired_before"] for call in recorder.calls}) == 1

    async def test_each_cutoff_follows_its_own_retention(
        self, mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Derive the pin cutoff from its own setting, independent of the age one."""
        mocker.patch.object(collection, "utc_now", return_value=NOW)
        mocker.patch.object(
            collection.inventory_app_settings,
            "COLLECTION_RETENTION",
            timedelta(days=10),
        )
        mocker.patch.object(
            collection.inventory_app_settings,
            "IDENTITY_LINK_PIN_RETENTION",
            timedelta(days=90),
        )
        await _node(session, retired=True)

        await run_inventory_collection()

        assert recorder.calls[0]["retired_before"] == NOW - timedelta(days=10)
        assert recorder.calls[0]["link_pin_retired_before"] == NOW - timedelta(days=90)

    async def test_the_batch_size_is_passed_as_the_limit(
        self, mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Ask for no more than the configured batch size."""
        mocker.patch.object(
            collection.inventory_app_settings, "COLLECTION_BATCH_SIZE", BATCH_SIZE
        )
        await _node(session, retired=True)

        await run_inventory_collection()

        assert {call["limit"] for call in recorder.calls} == {BATCH_SIZE}

    async def test_the_real_call_asks_for_a_delete_explicitly(
        self, session: AsyncSession, recorder: CollectRecorder
    ) -> None:
        """Run the dry run first and the delete second, each said explicitly."""
        await _node(session, retired=True)

        await run_inventory_collection()

        assert [call["dry_run"] for call in recorder.calls] == [True, False]


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_the_ledger_is_cleared_before_the_delete(
    session: AsyncSession, recorder: CollectRecorder
) -> None:
    """Drop the absence rows before the delete, not after it.

    Clearing first is the only order with no crash window, and it is safe
    because the syncer cannot re-record an absence for an already-retired
    entity. The ledger is counted as each call arrives, so this asserts the
    ordering rather than only the end state.
    """
    node = await _node(session, retired=True)
    await SyncEntityAbsenceManager.create(
        session,
        SyncEntityAbsenceWrite(
            syncer=SYNCER,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            entity_id=node.id,
        ),
    )
    ledger_at_call: list[int] = []

    async def probe() -> None:
        ledger_at_call.append(await SyncEntityAbsenceManager.count(session))

    recorder.on_dry_run = probe
    recorder.on_delete = probe

    await run_inventory_collection()

    assert ledger_at_call == [1, 0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("task_databases", "extensions_database", "inventory_database")
async def test_a_raising_provider_deletes_nothing(
    mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
) -> None:
    """Abort the run instead of deleting against a partial retained set."""

    async def broken_provider(_session: AsyncSession) -> Mapping:
        raise RuntimeError("provider is down")

    mocker.patch.object(
        collection,
        "collect_inventory_reference_providers",
        return_value=[broken_provider],
    )
    node = await _node(session, retired=True)

    with pytest.raises(RuntimeError, match="provider is down"):
        await run_inventory_collection()

    assert recorder.calls == []
    assert await _node_exists(session, node)


@pytest.mark.asyncio
@pytest.mark.usefixtures("task_databases", "extensions_database", "inventory_database")
async def test_an_empty_provider_result_is_not_a_skip(
    mocker: MockerFixture, session: AsyncSession
) -> None:
    """Treat "nothing referenced" as a licence to collect, not as an abort."""

    async def empty_provider(_session: AsyncSession) -> Mapping:
        return {}

    mocker.patch.object(
        collection,
        "collect_inventory_reference_providers",
        return_value=[empty_provider],
    )
    node = await _node(session, retired=True)

    await run_inventory_collection()

    assert not await _node_exists(session, node)


@pytest.mark.asyncio
@pytest.mark.usefixtures("task_databases", "extensions_database", "inventory_database")
async def test_provider_ids_are_kept(
    mocker: MockerFixture, session: AsyncSession
) -> None:
    """Spare every service a declared provider still references."""
    kept, collected = await _retired_service(session), await _retired_service(session)

    async def provider(_session: AsyncSession) -> Mapping:
        return {RetirableEntityName.SERVICE: {kept.id}}

    mocker.patch.object(
        collection, "collect_inventory_reference_providers", return_value=[provider]
    )

    await run_inventory_collection()

    assert await _service_exists(session, kept)
    assert not await _service_exists(session, collected)


@pytest.mark.asyncio
@pytest.mark.usefixtures("task_databases", "extensions_database", "inventory_database")
async def test_a_run_recorded_after_the_dry_run_still_survives(
    mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
) -> None:
    """Keep a service whose backup run lands between the two calls of a batch.

    The retained set is read before the dry run, while the catalog is still
    empty, so the id can only be retained by the ``Task`` envelope scan. This is
    the half of the window that holds without the fence: the recorder fires for
    a task the scan had already read.
    """
    service = await _retired_service(session)
    await _create_holder_task(session, "backup-a", service.id)
    mocker.patch.object(
        collection,
        "collect_inventory_reference_providers",
        return_value=[referenced_inventory_entities],
    )

    async def record_run() -> None:
        await MysqlBackupRunManager.save(
            session,
            MysqlBackupRun(
                task_history_id=1,
                service_name="svc-a",
                service_id=service.id,
                backup_type="M",
            ),
        )

    recorder.on_dry_run = record_run

    await run_inventory_collection()

    assert await MysqlBackupRunManager.count(session) == 1
    assert await _service_exists(session, service)


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_the_clear_reaches_a_syncer_no_longer_configured(
    session: AsyncSession,
) -> None:
    """Clear the ledger row of a syncer the deployment has since dropped.

    A row outlives the configuration that wrote it, so a delete scoped to
    ``EXTENSIONS.SYNCERS`` would strand it permanently: the entity it names is gone and
    no configured syncer carries its key any more.
    """
    node = await _node(session, retired=True)
    await SyncEntityAbsenceManager.create(
        session,
        SyncEntityAbsenceWrite(
            syncer="app.extensions.sync.syncers.legacy.LegacySyncer",
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            entity_id=node.id,
        ),
    )

    await run_inventory_collection()

    assert not await _node_exists(session, node)
    assert await SyncEntityAbsenceManager.list(session) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_the_scheduled_run_collects(session: AsyncSession) -> None:
    """Run a whole collection from the scheduled entry point."""
    node = await _node(session, retired=True)

    await run_scheduled_inventory_collection()

    assert not await _node_exists(session, node)


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_clearing_one_entity_type_leaves_a_colliding_id_alone(
    session: AsyncSession,
) -> None:
    """Clear only the node's ledger row when a service shares its id.

    Nodes and services are numbered from separate sequences and collide freely,
    so the ledger key is the full ``(syncer, entity_type, entity_id)`` triple.
    The first node and the first service of a fresh database both get id 1;
    only the node is retired.
    """
    node = await _node(session, retired=True)
    live_node = await _node(session, retired=False)
    service = await ServiceManager.create(
        session, ServiceWriteFactory.build(), node_id=live_node.id
    )
    assert service.id == node.id
    for entity_type in (
        SyncInventoryEntityTypeEnum.NODE,
        SyncInventoryEntityTypeEnum.SERVICE,
    ):
        await SyncEntityAbsenceManager.create(
            session,
            SyncEntityAbsenceWrite(
                syncer=SYNCER, entity_type=entity_type, entity_id=node.id
            ),
        )

    await run_inventory_collection()

    surviving = await SyncEntityAbsenceManager.list(session)
    assert [row.entity_type for row in surviving] == [
        SyncInventoryEntityTypeEnum.SERVICE
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_the_retained_set_is_re_read_for_every_batch(
    mocker: MockerFixture, session: AsyncSession, recorder: CollectRecorder
) -> None:
    """Spare a service whose holder was written after an earlier batch's delete.

    The batch size is one, so each retired service is its own batch. The holder
    is created right after the first delete, naming whichever service is left:
    only a retained set read again for the second batch can see it.
    """
    mocker.patch.object(collection.inventory_app_settings, "COLLECTION_BATCH_SIZE", 1)
    services = [await _retired_service(session) for _ in range(2)]
    survivors: list[Service] = []

    async def add_holder_for_the_survivor() -> None:
        if survivors:
            return
        survivors.extend(
            [service for service in services if await _service_exists(session, service)]
        )
        await _create_holder_task(session, "late-holder", survivors[0].id)

    recorder.after_delete = add_holder_for_the_survivor

    await run_inventory_collection()

    assert len(recorder.deletes) > 1
    assert len(survivors) == 1
    assert await _service_exists(session, survivors[0])


@pytest.mark.asyncio
@pytest.mark.usefixtures(
    "no_providers", "task_databases", "extensions_database", "inventory_database"
)
async def test_a_batch_dry_runs_and_deletes_against_one_retained_set(
    session: AsyncSession, celery_beat_session: AsyncSession, recorder: CollectRecorder
) -> None:
    """Keep a service whose only holder disappears between a batch's two calls.

    The ledger clear is computed from the dry run, so the delete must not
    widen past it: a holder present when the batch read its retained set
    protects its service through that batch's delete.
    """
    service = await _retired_service(session)
    holder = await _create_beat_row(
        celery_beat_session, "backup-a", {SERVICE_ID_META_KEY: service.id}
    )

    async def drop_the_holder() -> None:
        await PeriodicTaskManager.delete(celery_beat_session, holder)

    recorder.on_dry_run = drop_the_holder

    await run_inventory_collection()

    assert await _service_exists(session, service)


@pytest.fixture
def pg_collection_databases(
    mocker: MockerFixture,
    postgres_session_maker: async_sessionmaker[AsyncSession],
    celery_beat_session: AsyncSession,
) -> int:
    """Run the job against real PostgreSQL, on its own fence key.

    The tasks, PMM Extensions and inventory reads each draw fresh sessions from
    one PostgreSQL schema, as the deployed job does from one shared database.
    The fence is taken on a connection drawn from that engine, so it is real.
    The beat schedules stay on SQLite: no fence lives there.

    :return: The fence key the job and the writers now use.
    """
    for name in (
        "get_tasks_session_maker",
        "get_extensions_session_maker",
        "get_inventory_session_maker",
    ):
        mocker.patch.object(collection, name, return_value=postgres_session_maker)
    mocker.patch.object(
        collection,
        "get_beat_session_maker",
        return_value=_session_maker(celery_beat_session),
    )
    return isolate_reference_fence(mocker)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.usefixtures("no_providers")
class TestCollectionFenceOnPostgres:
    """Prove the fence closes the scan-to-delete window against each writer.

    A writer is sent while the batch holds the fence — from the dry-run hook,
    after the batch has read its retained set — and is observed queued on the
    lock before the batch goes on. The batch then deletes the service the
    writer names. The guarantee under test is the ordering: at the moment of
    the delete, the writer has not finished, so the service was never deleted
    while a holder naming it existed. The fence no-ops on SQLite, so a SQLite
    version of these would pass with the fence deleted.
    """

    async def _race(
        self,
        postgres_engine: AsyncEngine,
        fence_key: int,
        recorder: CollectRecorder,
        send: Callable[[], Coroutine[Any, Any, Response]],
    ) -> tuple[Response, list[bool]]:
        """Run the job with ``send`` fired mid-batch.

        :param postgres_engine: The engine to watch the lock queue through.
        :param fence_key: The fence key the job uses.
        :param recorder: The recorder to hook the writer into.
        :param send: Sends the writer's request.
        :return: The writer's response, and whether it had finished at each
            delete.
        """
        writer: asyncio.Task[Response] | None = None
        done_at_delete: list[bool] = []

        async def start_writer() -> None:
            nonlocal writer
            if writer is not None:
                return
            writer = asyncio.create_task(send())
            await wait_for_advisory_waiters(
                postgres_engine, REFERENCE_FENCE_LOCK_NAMESPACE, fence_key
            )

        async def observe_writer() -> None:
            done_at_delete.append(writer is not None and writer.done())

        recorder.on_dry_run = start_writer
        recorder.after_delete = observe_writer
        try:
            await run_inventory_collection()
            assert writer is not None
            response = await asyncio.wait_for(writer, WRITER_WAIT)
        finally:
            if writer is not None and not writer.done():
                writer.cancel()
        return response, done_at_delete

    @pytest_asyncio.fixture
    async def retired_service(
        self, postgres_session_maker: async_sessionmaker[AsyncSession]
    ) -> Service:
        """Create one retired service no holder names yet."""
        async with postgres_session_maker() as session:
            return await _retired_service(session)

    @pytest_asyncio.fixture
    async def client(
        self,
        postgres_session_maker: async_sessionmaker[AsyncSession],
        celery_beat_session: AsyncSession,
        regular_user: CasdoorUser,
    ) -> AsyncIterator[AsyncClient]:
        """Drive the real Tasks API routes against the same databases."""
        async with tasks_api_client(
            regular_user, postgres_session_maker, celery_beat_session
        ) as client:
            yield client

    async def _assert_deleted_before_the_holder_landed(
        self,
        postgres_session_maker: async_sessionmaker[AsyncSession],
        service: Service,
        response: Response,
        done_at_delete: list[bool],
    ) -> None:
        """Check the writer succeeded only after the service's delete.

        :param postgres_session_maker: The maker to read the result through.
        :param service: The service the writer named.
        :param response: The writer's response.
        :param done_at_delete: Whether the writer had finished at each delete.
        """
        assert response.status_code < status.HTTP_300_MULTIPLE_CHOICES, response.text
        assert done_at_delete == [False]
        async with postgres_session_maker() as session:
            assert not await _service_exists(session, service)

    async def test_create_task_waits_for_the_batch(
        self,
        postgres_engine,
        postgres_session_maker,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Hold a new task naming the service until the batch has deleted it."""
        payload = TaskWrite.model_validate(
            TaskFactory.build(
                name="racing-task",
                data={
                    "task": "run-command",
                    "meta": {SERVICE_ID_META_KEY: retired_service.id},
                },
            )
        ).model_dump(mode="json")

        response, done_at_delete = await self._race(
            postgres_engine,
            pg_collection_databases,
            recorder,
            lambda: client.post("/", json=payload),
        )

        await self._assert_deleted_before_the_holder_landed(
            postgres_session_maker, retired_service, response, done_at_delete
        )

    async def test_update_task_waits_for_the_batch(
        self,
        postgres_engine,
        postgres_session_maker,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Hold a task update that adds the service until the batch has deleted it."""
        async with postgres_session_maker() as session:
            await TaskManager.create(
                session,
                TaskWrite.model_validate(
                    TaskFactory.build(
                        name="racing-task", data={"task": "run-command", "meta": {}}
                    )
                ),
            )
        payload = TaskWrite.model_validate(
            TaskFactory.build(
                name="racing-task",
                data={
                    "task": "run-command",
                    "meta": {SERVICE_ID_META_KEY: retired_service.id},
                },
            )
        ).model_dump(mode="json")

        response, done_at_delete = await self._race(
            postgres_engine,
            pg_collection_databases,
            recorder,
            lambda: client.put("/racing-task", json=payload),
        )

        await self._assert_deleted_before_the_holder_landed(
            postgres_session_maker, retired_service, response, done_at_delete
        )

    async def test_create_periodic_task_waits_for_the_batch(
        self,
        postgres_engine,
        postgres_session_maker,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Hold a new schedule naming the service until the batch has deleted it."""
        async with postgres_session_maker() as session:
            await TaskManager.create(
                session, TaskWrite.model_validate(TaskFactory.build(name="my-task"))
            )
        payload = {
            "interval": {"every": 10, "period": "minutes"},
            "execute_request": {"meta": {SERVICE_ID_META_KEY: retired_service.id}},
        }

        response, done_at_delete = await self._race(
            postgres_engine,
            pg_collection_databases,
            recorder,
            lambda: client.post("/my-task/periodic/", json=payload),
        )

        await self._assert_deleted_before_the_holder_landed(
            postgres_session_maker, retired_service, response, done_at_delete
        )

    async def test_update_periodic_task_waits_for_the_batch(
        self,
        postgres_engine,
        postgres_session_maker,
        celery_beat_session,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Hold a schedule update that adds the service until the batch deleted it."""
        schedule = await _create_beat_row(celery_beat_session, "my-task", {})
        payload = {
            "name": schedule.name,
            "task": "my-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "interval": {"every": 10, "period": "minutes"},
            "execute_request": {"meta": {SERVICE_ID_META_KEY: retired_service.id}},
        }

        response, done_at_delete = await self._race(
            postgres_engine,
            pg_collection_databases,
            recorder,
            lambda: client.put(f"/periodic/{schedule.id}", json=payload),
        )

        await self._assert_deleted_before_the_holder_landed(
            postgres_session_maker, retired_service, response, done_at_delete
        )

    async def test_a_fence_held_by_writers_past_the_bound_aborts_the_run(
        self,
        mocker,
        postgres_session_maker,
        pg_collection_databases,
        recorder,
        retired_service,
    ):
        """Fail the run before any ledger clear or delete when writers outlast it."""
        mocker.patch.object(
            reference_fence, "REFERENCE_FENCE_TIMEOUT", timedelta(milliseconds=200)
        )
        async with postgres_session_maker() as session:
            await SyncEntityAbsenceManager.create(
                session,
                SyncEntityAbsenceWrite(
                    syncer=SYNCER,
                    entity_type=SyncInventoryEntityTypeEnum.SERVICE,
                    entity_id=retired_service.id,
                ),
            )

        async with (
            postgres_session_maker() as writer_session,
            hold_reference_write_fence(writer_session),
        ):
            with pytest.raises(AdvisoryLockTimeoutError):
                await run_inventory_collection()

        assert recorder.calls == []
        async with postgres_session_maker() as session:
            assert await SyncEntityAbsenceManager.count(session) == 1
            assert await _service_exists(session, retired_service)

    async def test_a_delete_failure_holds_the_fence_for_the_grace(
        self,
        mocker,
        postgres_engine,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Keep writers out for the grace after a failed delete, then re-raise.

        A COMMIT the server already received can still land after the client
        has given up on it, so a writer let in at once could write a holder for
        a service that delete then removes.
        """
        grace = timedelta(milliseconds=500)
        mocker.patch.object(collection, "REFERENCE_FENCE_COMMIT_GRACE", grace)
        payload = TaskWrite.model_validate(
            TaskFactory.build(name="racing-task")
        ).model_dump(mode="json")
        timings: dict[str, float] = {}
        writer: asyncio.Task[None] | None = None

        async def send_and_time() -> None:
            await client.post("/", json=payload)
            timings["writer_done"] = time.monotonic()

        async def fail_the_delete() -> None:
            nonlocal writer
            writer = asyncio.create_task(send_and_time())
            await wait_for_advisory_waiters(
                postgres_engine, REFERENCE_FENCE_LOCK_NAMESPACE, pg_collection_databases
            )
            timings["delete_failed"] = time.monotonic()
            raise ConnectionError("lost the inventory database mid-delete")

        recorder.on_delete = fail_the_delete
        try:
            with pytest.raises(ConnectionError):
                await run_inventory_collection()
            assert writer is not None
            await asyncio.wait_for(writer, WRITER_WAIT)
        finally:
            if writer is not None and not writer.done():
                writer.cancel()

        held = timings["writer_done"] - timings["delete_failed"]
        assert held >= grace.total_seconds()

    async def test_a_fence_lost_mid_batch_aborts_before_the_delete(
        self,
        postgres_engine,
        postgres_session_maker,
        pg_collection_databases,
        recorder,
        retired_service,
        client,
    ):
        """Refuse to delete once the fence's backend is gone, whatever came in.

        The lock's backend is terminated after the batch read its retained set,
        which releases the lock with the collector still inside its block. A
        writer naming the service then commits at once. Only checking the fence
        before the delete stops that delete from removing the service.
        """
        holder_pids = text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND mode = 'ExclusiveLock' AND objsubid = 2 "
            "AND classid::bigint = :classid AND objid::bigint = :objid"
        )
        payload = TaskWrite.model_validate(
            TaskFactory.build(
                name="racing-task",
                data={
                    "task": "run-command",
                    "meta": {SERVICE_ID_META_KEY: retired_service.id},
                },
            )
        ).model_dump(mode="json")
        responses: list[Response] = []

        async def lose_the_fence_then_write() -> None:
            async with postgres_engine.connect() as admin:
                pids = (
                    await admin.scalars(
                        holder_pids,
                        {
                            "classid": REFERENCE_FENCE_LOCK_NAMESPACE,
                            "objid": pg_collection_databases & 0xFFFFFFFF,
                        },
                    )
                ).all()
                assert len(pids) == 1
                assert await admin.scalar(
                    text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pids[0]}
                )
            responses.append(await client.post("/", json=payload))

        recorder.on_dry_run = lose_the_fence_then_write

        with pytest.raises(AdvisoryLockLostError):
            await run_inventory_collection()

        assert responses[0].status_code == status.HTTP_201_CREATED
        assert recorder.deletes == []
        async with postgres_session_maker() as session:
            assert await _service_exists(session, retired_service)
