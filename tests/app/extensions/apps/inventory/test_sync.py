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

"""Define tests for the app.extensions.apps.inventory.sync module."""

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from kombu.exceptions import KombuError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.celery import celery
from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils.date_time import utc_now
from app.extensions.apps.inventory.sync import (
    run_inventory_sync,
    run_node_sync,
    run_scheduled_inventory_sync,
    run_schema_sync,
    run_service_sync,
    run_table_sync,
    start_followers,
)
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
)
from app.extensions.models import (
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.exceptions import SyncInstanceAlreadyInProgressError
from app.extensions.sync.models import BaseSyncer
from app.tasks.models import (
    EXECUTE_TASK_BY_NAME_TASK,
    INVENTORY_SYNC_FIRST_RUN_KEY,
    INVENTORY_SYNC_TASK_NAME,
)
from tests.app.factories import (
    CreatedNodeFactory,
    CreatedSchemaFactory,
    CreatedServiceFactory,
    CreatedTableFactory,
)


class _StubPMMSyncer:
    """Stand-in syncer used to exercise ``filter_syncers_by_name`` matching."""

    def can_sync_inventory(self) -> bool:
        return True


class _StubMySQLSyncer:
    """Second stand-in syncer used to exercise multi-syncer selection."""

    def can_sync_inventory(self) -> bool:
        return True


class _StubFactsSyncer:
    """Stand in for a follower listed after another one."""

    def can_sync_inventory(self) -> bool:
        return True


_PMM_STUB_NAME = f"{_StubPMMSyncer.__module__}.{_StubPMMSyncer.__name__}"
_MYSQL_STUB_NAME = f"{_StubMySQLSyncer.__module__}.{_StubMySQLSyncer.__name__}"
_FACTS_STUB_NAME = f"{_StubFactsSyncer.__module__}.{_StubFactsSyncer.__name__}"


@pytest.fixture
def mock_base_syncer_factory():
    """Create a mock BaseSyncer whose api_auth is an async context manager."""

    def _create_mock_syncer():
        syncer = AsyncMock(spec=BaseSyncer)

        @asynccontextmanager
        async def api_auth(_api_key: str):
            yield syncer

        syncer.api_auth = api_auth
        return syncer

    return _create_mock_syncer


@pytest.fixture
def created_node() -> CreatedNode:
    """Return a fake created node."""
    return CreatedNodeFactory.build()


@pytest.fixture
def created_service(created_node) -> CreatedService:
    """Return a fake created service."""
    created_service = CreatedServiceFactory.build()
    created_service.node = created_node
    return created_service


@pytest.fixture
def created_schema(created_service) -> CreatedSchema:
    """Return a fake created Schema."""
    created_schema = CreatedSchemaFactory.build()
    created_schema.service = created_service
    return created_schema


@pytest.fixture
def created_table() -> CreatedTable:
    """Return a fake created Table."""
    return CreatedTableFactory.build()


@pytest.mark.asyncio
async def test_run_inventory_sync(mock_base_syncer_factory):
    """Test executing inventory synchronization using the provided syncers."""
    syncer1 = mock_base_syncer_factory()
    syncer2 = mock_base_syncer_factory()

    await run_inventory_sync("test-key", syncer1, syncer2)

    syncer1.sync_inventory.assert_awaited_once()
    syncer2.sync_inventory.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_node_sync(created_node, mock_base_syncer_factory):
    """Test executing node synchronization for a created node."""
    syncer1 = mock_base_syncer_factory()
    syncer2 = mock_base_syncer_factory()

    await run_node_sync(created_node, "test-key", syncer1, syncer2)

    syncer1.sync_node.assert_awaited_once_with(created_node, refresh_at_start=False)
    syncer2.sync_node.assert_awaited_once_with(created_node, refresh_at_start=True)


@pytest.mark.asyncio
async def test_run_service_sync(created_service, mock_base_syncer_factory):
    """Test executing service synchronization for a created service."""
    syncer1 = mock_base_syncer_factory()
    syncer2 = mock_base_syncer_factory()

    await run_service_sync(created_service, "test-key", syncer1, syncer2)

    syncer1.sync_service.assert_awaited_once_with(
        created_service, refresh_at_start=False
    )
    syncer2.sync_service.assert_awaited_once_with(
        created_service, refresh_at_start=True
    )


@pytest.mark.asyncio
async def test_run_schema_sync(created_schema, mock_base_syncer_factory):
    """Test executing schema synchronization for a created schema."""
    syncer1 = mock_base_syncer_factory()
    syncer2 = mock_base_syncer_factory()

    await run_schema_sync(created_schema, "test-key", syncer1, syncer2)

    syncer1.sync_schema.assert_awaited_once_with(created_schema, refresh_at_start=False)
    syncer2.sync_schema.assert_awaited_once_with(created_schema, refresh_at_start=True)


@pytest.mark.asyncio
async def test_run_table_sync(created_table, mock_base_syncer_factory):
    """Test executing table synchronization for a created table."""
    syncer1 = mock_base_syncer_factory()
    syncer2 = mock_base_syncer_factory()

    await run_table_sync(created_table, "test-key", syncer1, syncer2)

    syncer1.sync_table.assert_awaited_once_with(created_table, refresh_at_start=False)
    syncer2.sync_table.assert_awaited_once_with(created_table, refresh_at_start=True)


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync(mocker, mock_base_syncer_factory):
    """Assert run_scheduled_inventory_sync reads internal token and constructs syncers."""
    syncer = mock_base_syncer_factory()
    mocker.patch.object(
        settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr("test-api-key")
    )
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[syncer],
    )
    mock_run = mocker.patch(
        "app.extensions.apps.inventory.sync.run_inventory_sync",
        new=AsyncMock(),
    )
    await run_scheduled_inventory_sync()
    mock_run.assert_awaited_once_with("test-api-key", syncer)


def _patch_scheduled_sync_env(mocker, syncers):
    """Patch the internal token and syncer-construction hooks for scheduled-sync tests."""
    mocker.patch.object(
        settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr("test-api-key")
    )
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=syncers,
    )
    return mocker.patch(
        "app.extensions.apps.inventory.sync.run_inventory_sync",
        new=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync_runs_all_when_syncer_none(mocker):
    """Sync-all path: every configured syncer is forwarded in declaration order."""
    syncers = [_StubPMMSyncer(), _StubMySQLSyncer()]
    mock_run = _patch_scheduled_sync_env(mocker, syncers)
    await run_scheduled_inventory_sync()
    mock_run.assert_awaited_once_with("test-api-key", *syncers)


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync_runs_all_when_syncer_empty_string(mocker):
    """An empty syncer string is treated as the sync-all path."""
    syncers = [_StubPMMSyncer(), _StubMySQLSyncer()]
    mock_run = _patch_scheduled_sync_env(mocker, syncers)
    await run_scheduled_inventory_sync(syncer="")
    mock_run.assert_awaited_once_with("test-api-key", *syncers)


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync_targets_named_syncer(mocker):
    """A matching qualified name resolves to a single-syncer call."""
    pmm = _StubPMMSyncer()
    mysql = _StubMySQLSyncer()
    mock_run = _patch_scheduled_sync_env(mocker, [pmm, mysql])
    await run_scheduled_inventory_sync(syncer=_MYSQL_STUB_NAME)
    mock_run.assert_awaited_once_with("test-api-key", mysql)


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync_unknown_syncer_raises(mocker):
    """An unknown syncer raises ``ValueError`` without invoking the run."""
    mock_run = _patch_scheduled_sync_env(mocker, [_StubPMMSyncer()])
    with pytest.raises(ValueError, match=r"app\.fake\.UnknownSyncer"):
        await run_scheduled_inventory_sync(syncer="app.fake.UnknownSyncer")
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scheduled_inventory_sync_incapable_syncer_raises(mocker):
    """A configured syncer that cannot sync inventory is rejected by name."""
    pmm = _StubPMMSyncer()
    mocker.patch.object(pmm, "can_sync_inventory", return_value=False)
    mock_run = _patch_scheduled_sync_env(mocker, [pmm])
    with pytest.raises(ValueError, match=re.escape(_PMM_STUB_NAME)):
        await run_scheduled_inventory_sync(syncer=_PMM_STUB_NAME)
    mock_run.assert_not_awaited()


class _NoopInventorySyncer(BaseSyncer):
    """Complete an inventory pass without reading any remote source."""

    SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.INVENTORY

    async def perform_inventory_sync(self) -> None:
        """Finish the pass at once, leaving the run lifecycle around it real."""


class _LeaderSyncer(_NoopInventorySyncer):
    """Stand in for the pinned default the per-syncer schedules follow."""


class _FollowerSyncer(_NoopInventorySyncer):
    """Stand in for a per-syncer schedule the default starts after its first pass."""


_LEADER = _LeaderSyncer.get_name()
_FOLLOWER = _FollowerSyncer.get_name()


@pytest.fixture(name="extensions_maker")
def extensions_maker_fixture(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Return a session maker over the ``session`` fixture's in-memory PMM Extensions DB.

    The code under test opens its own sessions, so it needs a maker rather than
    the one session the fixture yields.
    """
    return get_async_session_maker_from_engine(session.bind)


def _route_sessions(mocker, maker: async_sessionmaker[AsyncSession]) -> None:
    """Point both the syncer lifecycle and the follower start at ``maker``.

    Each module binds ``get_async_session_maker`` itself, so patching one would
    leave the run writing one database while the start reads another.
    """
    mocker.patch(
        "app.extensions.sync.models.get_async_session_maker", return_value=maker
    )
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_async_session_maker", return_value=maker
    )


async def _record_run(
    maker: async_sessionmaker[AsyncSession],
    syncer: str,
    inventory_status: SyncStatusEnum = SyncStatusEnum.SUCCESS,
    *,
    at: datetime | None = None,
) -> None:
    """Persist a run of ``syncer`` whose INVENTORY item ended as given.

    :param at: When the run began and its item was recorded, or ``None`` for now.
        Set it where the test depends on the order of runs, which a shared clock
        tick would otherwise leave ambiguous.
    """
    stamped = {} if at is None else {"created_at": at}
    async with maker() as session:
        instance = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer=syncer, status=inventory_status),
            **stamped,
        )
        await SyncItemManager.create(
            session,
            SyncItemWrite(
                entity_type=SyncInventoryEntityTypeEnum.INVENTORY,
                entity_id=None,
                sync_instance_id=instance.id,
                status=inventory_status,
            ),
            **stamped,
        )


def _follower_start(follower: str) -> dict[str, object]:
    """Return the ``execute_task_by_name`` kwargs that start ``follower`` once."""
    return {
        "task_name": INVENTORY_SYNC_TASK_NAME,
        "execution_data": {
            "meta": {"syncer": follower, INVENTORY_SYNC_FIRST_RUN_KEY: True}
        },
    }


class TestScheduledFollowerRun:
    """Test a per-syncer schedule's own runs, which wait on no other syncer."""

    @pytest.fixture(autouse=True)
    def _follower_configured(self, extensions_maker, mocker, mock_remote_api) -> None:
        """Route sessions at the in-memory DB and configure only the follower."""
        _route_sessions(mocker, extensions_maker)
        mocker.patch(
            "app.extensions.apps.inventory.sync.get_syncers_standalone",
            return_value=[_FollowerSyncer(inventory_api=mock_remote_api)],
        )

    @pytest.mark.asyncio
    async def test_runs_before_the_leader_ever_completes(self, extensions_maker):
        """Run the follower on a fresh install, with no leader pass recorded."""
        note = await run_scheduled_inventory_sync(syncer=_FOLLOWER)

        assert note is None
        async with extensions_maker() as session:
            (run,) = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
        assert run.status == SyncStatusEnum.SUCCESS

    @pytest.mark.parametrize(
        "leader_status", [None, SyncStatusEnum.FAILED], ids=["never-ran", "failed"]
    )
    @pytest.mark.asyncio
    async def test_a_fire_queued_by_an_earlier_build_still_runs(
        self, extensions_maker, leader_status: SyncStatusEnum | None
    ):
        """Accept and ignore ``after_syncer`` from a request an earlier build queued.

        That build skipped the follower until the leader completed; this one runs
        it whatever the leader's state, rather than failing on the keyword.
        """
        if leader_status is not None:
            await _record_run(extensions_maker, _LEADER, leader_status)

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER, after_syncer=_LEADER
        )

        assert note is None
        async with extensions_maker() as session:
            (run,) = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
        assert run.status == SyncStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_a_started_run_goes_ahead_after_earlier_runs(self, extensions_maker):
        """Run a leader-started follower although it has run before.

        The leader starts it because none of its runs read the leader's first
        pass, so an earlier run is no reason to skip.
        """
        await _record_run(
            extensions_maker, _FOLLOWER, at=utc_now() - timedelta(hours=1)
        )

        note = await run_scheduled_inventory_sync(syncer=_FOLLOWER, first_run_only=True)

        assert note is None
        async with extensions_maker() as session:
            runs = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
        assert sorted(run.status for run in runs) == [SyncStatusEnum.SUCCESS] * 2


def _claim_the_follower_before_listing(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Start a competing follower run before the run under test claims its syncer.

    The syncers are listed before the run claims its syncer, so recording a
    running follower there is the overlap in which both a started run and a beat
    fire of the follower were dispatched.
    """

    async def _claim_then_list() -> list[BaseSyncer]:
        await _record_run(maker, _FOLLOWER, SyncStatusEnum.RUNNING)
        return [_FollowerSyncer(inventory_api=mock_remote_api)]

    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        side_effect=_claim_then_list,
    )


@pytest.mark.asyncio
async def test_a_started_run_that_loses_the_claim_is_skipped(
    extensions_maker, mocker, mock_remote_api
):
    """Report a started run refused by an overlapping run as skipped.

    Raising would record a failed host-facts run on a bring-up where the two
    happened to overlap, although the follower is running. The returned note is
    what the executor writes to the run's log, so the skip does not read as a
    sync that succeeded.
    """
    _route_sessions(mocker, extensions_maker)
    _claim_the_follower_before_listing(extensions_maker, mocker, mock_remote_api)

    note = await run_scheduled_inventory_sync(syncer=_FOLLOWER, first_run_only=True)

    assert note is not None
    assert _FOLLOWER in note
    async with extensions_maker() as session:
        (run,) = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
    assert run.status == SyncStatusEnum.RUNNING


@pytest.mark.asyncio
async def test_a_scheduled_run_that_loses_the_claim_still_raises(
    extensions_maker, mocker, mock_remote_api
):
    """Keep refusing a follower's own scheduled run that overlaps another run.

    Only a leader-started run treats the refusal as a skip: any other
    overlapping run of one syncer fails as it always has.
    """
    _route_sessions(mocker, extensions_maker)
    _claim_the_follower_before_listing(extensions_maker, mocker, mock_remote_api)

    with pytest.raises(SyncInstanceAlreadyInProgressError):
        await run_scheduled_inventory_sync(syncer=_FOLLOWER)


class TestStartFollowers:
    """Test the leader-side start of followers that have not read its first pass."""

    @pytest.fixture
    def send_task(self, mocker, extensions_maker) -> MagicMock:
        """Route the lookups at the in-memory DB and replace the broker call."""
        _route_sessions(mocker, extensions_maker)
        return mocker.patch.object(celery, "send_task")

    async def _start(self, *followers: str, configured: tuple[type, ...] = ()) -> None:
        """Start ``followers`` of the PMM stub, resolving against ``configured``."""
        await start_followers(
            _PMM_STUB_NAME,
            list(followers),
            [
                syncer_cls()
                for syncer_cls in configured or (_StubPMMSyncer, _StubMySQLSyncer)
            ],
        )

    @pytest.mark.asyncio
    async def test_starts_a_never_run_follower(self, extensions_maker, send_task):
        """Start a configured follower with no run of its own, exactly once."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        await self._start(_MYSQL_STUB_NAME)

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK, kwargs=_follower_start(_MYSQL_STUB_NAME)
        )

    @pytest.mark.parametrize(
        "follower_status", [SyncStatusEnum.SUCCESS, SyncStatusEnum.FAILED]
    )
    @pytest.mark.asyncio
    async def test_starts_a_follower_whose_runs_all_predate_the_first_pass(
        self, extensions_maker, send_task, follower_status: SyncStatusEnum
    ):
        """Start a follower that ran from bring-up but before the leader completed.

        Those runs read the inventory of a fresh install, so the follower is
        started again to read what the leader's pass brought in.
        """
        now = utc_now()
        await _record_run(
            extensions_maker,
            _MYSQL_STUB_NAME,
            follower_status,
            at=now - timedelta(hours=2),
        )
        await _record_run(extensions_maker, _PMM_STUB_NAME, at=now - timedelta(hours=1))

        await self._start(_MYSQL_STUB_NAME)

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK, kwargs=_follower_start(_MYSQL_STUB_NAME)
        )

    @pytest.mark.parametrize(
        "follower_status",
        [SyncStatusEnum.SUCCESS, SyncStatusEnum.FAILED, SyncStatusEnum.RUNNING],
    )
    @pytest.mark.asyncio
    async def test_leaves_a_follower_that_ran_since_the_first_pass(
        self, extensions_maker, send_task, follower_status: SyncStatusEnum
    ):
        """Leave a follower to its own schedule once a run began after the pass.

        A run still in progress counts too, since it reads the leader's inventory.
        """
        now = utc_now()
        await _record_run(extensions_maker, _PMM_STUB_NAME, at=now - timedelta(hours=2))
        await _record_run(
            extensions_maker,
            _MYSQL_STUB_NAME,
            follower_status,
            at=now - timedelta(hours=1),
        )

        await self._start(_MYSQL_STUB_NAME)

        send_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_measures_from_the_first_pass_not_the_latest(
        self, extensions_maker, send_task
    ):
        """Keep leaving a follower alone after later leader passes.

        Measuring from the latest pass would start the follower again after every
        leader run, which fires far more often than the follower's own interval.
        """
        now = utc_now()
        await _record_run(extensions_maker, _PMM_STUB_NAME, at=now - timedelta(hours=3))
        await _record_run(
            extensions_maker, _MYSQL_STUB_NAME, at=now - timedelta(hours=2)
        )
        await _record_run(extensions_maker, _PMM_STUB_NAME, at=now - timedelta(hours=1))

        await self._start(_MYSQL_STUB_NAME)

        send_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_waits_for_a_completed_leader_pass(self, extensions_maker, send_task):
        """Start nothing while the leader's only pass could not list the inventory."""
        await _record_run(extensions_maker, _PMM_STUB_NAME, SyncStatusEnum.FAILED)

        await self._start(_MYSQL_STUB_NAME)

        send_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_an_unconfigured_follower_with_a_warning(
        self, extensions_maker, send_task, caplog
    ):
        """Skip a follower no configured syncer resolves, naming it in the log."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        with caplog.at_level(
            logging.WARNING, logger="app.extensions.apps.inventory.sync"
        ):
            await self._start(_MYSQL_STUB_NAME, configured=(_StubPMMSyncer,))

        send_task.assert_not_called()
        assert _MYSQL_STUB_NAME in caplog.text

    @pytest.mark.asyncio
    async def test_logs_rather_than_raises_when_the_broker_refuses(
        self, extensions_maker, send_task, caplog
    ):
        """Log an enqueue failure and return, leaving the start to the next leader run."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)
        send_task.side_effect = KombuError("broker unreachable")

        with caplog.at_level(
            logging.ERROR, logger="app.extensions.apps.inventory.sync"
        ):
            await self._start(_MYSQL_STUB_NAME)

        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.exc_info is not None
        assert _MYSQL_STUB_NAME in record.getMessage()

    @pytest.mark.parametrize(
        ("configured", "broker_replies"),
        [
            pytest.param((_StubFactsSyncer,), [None], id="first-is-unconfigured"),
            pytest.param(
                (_StubMySQLSyncer, _StubFactsSyncer),
                [KombuError("broker unreachable"), None],
                id="broker-refuses-the-first",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_skipped_follower_does_not_stop_the_next(
        self,
        extensions_maker,
        send_task,
        configured: tuple[type, ...],
        broker_replies: list[KombuError | None],
    ):
        """Start the next follower after one is passed over, on either skip branch.

        Both branches log and move on, so a follower listed after the skipped one
        is still started in the same call.
        """
        await _record_run(extensions_maker, _PMM_STUB_NAME)
        send_task.side_effect = broker_replies

        await self._start(_MYSQL_STUB_NAME, _FACTS_STUB_NAME, configured=configured)

        assert send_task.call_count == len(broker_replies)
        send_task.assert_called_with(
            EXECUTE_TASK_BY_NAME_TASK, kwargs=_follower_start(_FACTS_STUB_NAME)
        )


def _configure_leader_and_follower(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> MagicMock:
    """Route sessions at ``maker``, configure both syncers and stub the broker.

    :return: The replaced ``send_task``.
    """
    _route_sessions(mocker, maker)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[
            _LeaderSyncer(inventory_api=mock_remote_api),
            _FollowerSyncer(inventory_api=mock_remote_api),
        ],
    )
    return mocker.patch.object(celery, "send_task")


async def _assert_the_leader_run_starts_its_follower(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Run the leader through the public call and check it starts the follower.

    The start is gated on the leader's completed pass, read through a session of
    its own. On a fresh database only the run this call makes can satisfy that
    gate, so a start at all proves the pass was persisted before the gate read it.
    """
    send_task = _configure_leader_and_follower(maker, mocker, mock_remote_api)

    await run_scheduled_inventory_sync(syncer=_LEADER, follower_syncers=[_FOLLOWER])

    async with maker() as session:
        assert await SyncItemManager.first_inventory_sync_completed_at(session, _LEADER)
        assert await SyncInstanceManager.list(session, syncer=_FOLLOWER) == []
    send_task.assert_called_once_with(
        EXECUTE_TASK_BY_NAME_TASK, kwargs=_follower_start(_FOLLOWER)
    )


@pytest.mark.asyncio
async def test_the_leader_run_starts_its_never_run_follower(
    extensions_maker, mocker, mock_remote_api
):
    """Start the follower after the leader's real run, on SQLite."""
    await _assert_the_leader_run_starts_its_follower(
        extensions_maker, mocker, mock_remote_api
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_leader_run_starts_its_never_run_follower_on_postgres(
    postgres_session_maker, mocker, mock_remote_api
):
    """Start the follower after the leader's real run, across PostgreSQL connections.

    SQLite's ``StaticPool`` shares one connection between the run and the gate, so
    only here does the gate prove the leader's pass was committed, not just written.
    """
    await _assert_the_leader_run_starts_its_follower(
        postgres_session_maker, mocker, mock_remote_api
    )


async def _assert_the_leader_run_catches_up_on_a_manual_first_pass(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Run the leader after a pass it did not start and check the follower starts.

    A pass made through the manual sync endpoint starts no follower itself, so
    the leader's next scheduled run starts the follower that ran only before it.
    """
    send_task = _configure_leader_and_follower(maker, mocker, mock_remote_api)
    now = utc_now()
    await _record_run(maker, _FOLLOWER, at=now - timedelta(hours=2))
    await _record_run(maker, _LEADER, at=now - timedelta(hours=1))

    await run_scheduled_inventory_sync(syncer=_LEADER, follower_syncers=[_FOLLOWER])

    send_task.assert_called_once_with(
        EXECUTE_TASK_BY_NAME_TASK, kwargs=_follower_start(_FOLLOWER)
    )


async def _assert_the_leader_run_leaves_a_caught_up_follower(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Run the leader in steady state and check it starts nothing.

    Once the follower has run after the leader's first pass, the leader runs
    every interval without starting it again.
    """
    send_task = _configure_leader_and_follower(maker, mocker, mock_remote_api)
    now = utc_now()
    await _record_run(maker, _LEADER, at=now - timedelta(hours=2))
    await _record_run(maker, _FOLLOWER, at=now - timedelta(hours=1))

    await run_scheduled_inventory_sync(syncer=_LEADER, follower_syncers=[_FOLLOWER])

    send_task.assert_not_called()


@pytest.mark.asyncio
async def test_the_leader_run_catches_up_on_a_manual_first_pass(
    extensions_maker, mocker, mock_remote_api
):
    """Start a follower whose leader first completed outside the schedule."""
    await _assert_the_leader_run_catches_up_on_a_manual_first_pass(
        extensions_maker, mocker, mock_remote_api
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_leader_run_catches_up_on_a_manual_first_pass_on_postgres(
    postgres_session_maker, mocker, mock_remote_api
):
    """Start a follower that predates the first pass, on PostgreSQL.

    There the pass's finish time is written by the database clock and the
    follower's start by the worker, so both sides of the comparison are real.
    """
    await _assert_the_leader_run_catches_up_on_a_manual_first_pass(
        postgres_session_maker, mocker, mock_remote_api
    )


@pytest.mark.asyncio
async def test_the_leader_run_leaves_a_caught_up_follower(
    extensions_maker, mocker, mock_remote_api
):
    """Start nothing from a steady-state leader run."""
    await _assert_the_leader_run_leaves_a_caught_up_follower(
        extensions_maker, mocker, mock_remote_api
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_leader_run_leaves_a_caught_up_follower_on_postgres(
    postgres_session_maker, mocker, mock_remote_api
):
    """Start nothing from a steady-state leader run, on PostgreSQL.

    There the pass's finish time is written by the database clock and the
    follower's start by the worker, so both sides of the comparison are real.
    """
    await _assert_the_leader_run_leaves_a_caught_up_follower(
        postgres_session_maker, mocker, mock_remote_api
    )
