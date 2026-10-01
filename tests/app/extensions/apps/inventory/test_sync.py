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
from datetime import datetime, timedelta, UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from kombu.exceptions import KombuError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.celery import celery
from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.extensions.apps.inventory.sync import (
    run_inventory_sync,
    run_node_sync,
    run_scheduled_inventory_sync,
    run_schema_sync,
    run_service_sync,
    run_table_sync,
    start_follower_first_runs,
)
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
)
from app.extensions.models import (
    SyncInstance,
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.exceptions import SyncInstanceAlreadyInProgressError
from app.extensions.sync.models import BaseSyncer
from app.tasks.models import (
    EXECUTE_TASK_BY_NAME_TASK,
    INVENTORY_SYNC_AFTER_KEY,
    INVENTORY_SYNC_FIRST_RUN_KEY,
    INVENTORY_SYNC_MAX_WAIT_KEY,
    INVENTORY_SYNC_SINCE_KEY,
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
    """Stand in for a per-syncer schedule whose first run follows the default."""


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
    """Point both the syncer lifecycle and the ordering gate at ``maker``.

    Each module binds ``get_async_session_maker`` itself, so patching one would
    leave the run writing one database while the gate reads another.
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
) -> None:
    """Persist a finished run of ``syncer`` whose INVENTORY item ended as given."""
    async with maker() as session:
        instance = await SyncInstanceManager.create(
            session, SyncInstanceWrite(syncer=syncer, status=inventory_status)
        )
        await SyncItemManager.create(
            session,
            SyncItemWrite(
                entity_type=SyncInventoryEntityTypeEnum.INVENTORY,
                entity_id=None,
                sync_instance_id=instance.id,
                status=inventory_status,
            ),
        )


_SEEDED_AT = datetime(2026, 9, 1, tzinfo=UTC)
_SEEDED_AT_ISO = _SEEDED_AT.isoformat()
_ONE_DAY = timedelta(days=1)
_ONE_DAY_SECONDS = int(_ONE_DAY.total_seconds())


def _follower_kick(
    follower: str,
    leader: str,
    since: str | None = None,
    max_wait: int | None = None,
) -> dict[str, object]:
    """Return the ``execute_task_by_name`` kwargs that start ``follower`` once."""
    return {
        "task_name": INVENTORY_SYNC_TASK_NAME,
        "execution_data": {
            "meta": {
                "syncer": follower,
                INVENTORY_SYNC_AFTER_KEY: leader,
                **({INVENTORY_SYNC_SINCE_KEY: since} if since is not None else {}),
                **(
                    {INVENTORY_SYNC_MAX_WAIT_KEY: max_wait}
                    if max_wait is not None
                    else {}
                ),
                INVENTORY_SYNC_FIRST_RUN_KEY: True,
            }
        },
    }


@pytest.mark.asyncio
async def test_a_follower_defers_until_the_leader_completes(
    extensions_maker, mocker, mock_remote_api
):
    """Skip the follower's run, opening no run and saying why, while the leader waits.

    A deferred run must not leave a ``SyncInstance`` behind: the kick starts only
    followers with none, so a stray row would stop the follower ever being started.
    The returned note is what the executor writes to the run's log, so a skipped
    run does not read as a sync that succeeded.
    """
    _route_sessions(mocker, extensions_maker)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[_FollowerSyncer(inventory_api=mock_remote_api)],
    )

    note = await run_scheduled_inventory_sync(
        syncer=_FOLLOWER,
        after_syncer=_LEADER,
        after_syncer_since=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        after_syncer_max_wait=_ONE_DAY_SECONDS,
    )

    assert note is not None
    assert _LEADER in note
    async with extensions_maker() as session:
        assert await SyncInstanceManager.list(session) == []


@pytest.mark.parametrize("first_run_only", [False, True])
@pytest.mark.asyncio
async def test_a_follower_runs_once_the_leader_completed(
    extensions_maker, mocker, mock_remote_api, *, first_run_only: bool
):
    """Run the follower's sync once the leader has finished a whole pass.

    A leader-started first run of a follower that has never run goes ahead too.
    """
    _route_sessions(mocker, extensions_maker)
    await _record_run(extensions_maker, _LEADER)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[_FollowerSyncer(inventory_api=mock_remote_api)],
    )

    note = await run_scheduled_inventory_sync(
        syncer=_FOLLOWER, after_syncer=_LEADER, first_run_only=first_run_only
    )

    assert note is None
    async with extensions_maker() as session:
        (run,) = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
    assert run.status == SyncStatusEnum.SUCCESS


@pytest.mark.parametrize(
    "earlier_status", [SyncStatusEnum.SUCCESS, SyncStatusEnum.FAILED]
)
@pytest.mark.asyncio
async def test_a_started_first_run_skips_a_follower_that_already_started(
    extensions_maker, mocker, mock_remote_api, earlier_status
):
    """Skip a leader-started first run once the follower has any run of its own.

    A beat fire of the follower queued behind the leader runs first when the
    worker takes one task at a time, so the start the leader sent meanwhile must
    not repeat it. A failed run counts, since it too was the follower's own.
    """
    _route_sessions(mocker, extensions_maker)
    await _record_run(extensions_maker, _LEADER)
    await _record_run(extensions_maker, _FOLLOWER, earlier_status)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[_FollowerSyncer(inventory_api=mock_remote_api)],
    )

    note = await run_scheduled_inventory_sync(
        syncer=_FOLLOWER, after_syncer=_LEADER, first_run_only=True
    )

    assert note is not None
    assert _FOLLOWER in note
    async with extensions_maker() as session:
        (run,) = await SyncInstanceManager.list(session, syncer=_FOLLOWER)
    assert run.status == earlier_status


def _claim_the_follower_before_listing(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Start a competing follower run between the first-run recheck and the claim.

    The syncers are listed after the recheck and before the run claims its
    syncer, so recording a running follower there is the overlap in which both a
    started first run and a beat fire of the follower passed their checks.
    """

    async def _claim_then_list() -> list[BaseSyncer]:
        await _record_run(maker, _FOLLOWER, SyncStatusEnum.RUNNING)
        return [_FollowerSyncer(inventory_api=mock_remote_api)]

    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        side_effect=_claim_then_list,
    )


@pytest.mark.asyncio
async def test_a_started_first_run_that_loses_the_claim_is_skipped(
    extensions_maker, mocker, mock_remote_api
):
    """Report a started first run refused by an overlapping run as skipped.

    Raising would record a failed host-facts run on a bring-up where the two
    happened to overlap, although the follower's first run is the one running.
    """
    _route_sessions(mocker, extensions_maker)
    await _record_run(extensions_maker, _LEADER)
    _claim_the_follower_before_listing(extensions_maker, mocker, mock_remote_api)

    note = await run_scheduled_inventory_sync(
        syncer=_FOLLOWER, after_syncer=_LEADER, first_run_only=True
    )

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

    Only a leader-started first run treats the refusal as a skip: any other
    overlapping run of one syncer fails as it always has.
    """
    _route_sessions(mocker, extensions_maker)
    await _record_run(extensions_maker, _LEADER)
    _claim_the_follower_before_listing(extensions_maker, mocker, mock_remote_api)

    with pytest.raises(SyncInstanceAlreadyInProgressError):
        await run_scheduled_inventory_sync(syncer=_FOLLOWER, after_syncer=_LEADER)


class TestBoundedFollowerWait:
    """Test that a follower stops waiting on a leader that never completes a pass."""

    @pytest.fixture
    def follower_run(self, mocker, extensions_maker, mock_remote_api):
        """Route the run at the in-memory DB with only the follower configured."""
        _route_sessions(mocker, extensions_maker)
        mocker.patch(
            "app.extensions.apps.inventory.sync.get_syncers_standalone",
            return_value=[_FollowerSyncer(inventory_api=mock_remote_api)],
        )

    @staticmethod
    def _at(mocker, now: datetime) -> None:
        """Pin the clock the wait is measured against."""
        mocker.patch("app.extensions.apps.inventory.sync.utc_now", return_value=now)

    @staticmethod
    async def _follower_runs(
        maker: async_sessionmaker[AsyncSession],
    ) -> list[SyncInstance]:
        """Return every recorded run of the follower."""
        async with maker() as session:
            return await SyncInstanceManager.list(session, syncer=_FOLLOWER)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_skips_inside_the_window_and_says_when_it_ends(
        self, extensions_maker, mocker
    ) -> None:
        """Skip the run while the window is open, naming the leader and the deadline.

        The note must also say the follower starts earlier if the leader
        completes first, so an operator reading the log knows both ways out.
        """
        self._at(mocker, _SEEDED_AT + _ONE_DAY - timedelta(seconds=1))

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        assert note is not None
        assert f"once {_LEADER} completes" in note
        assert "or at 2026-09-02T00:00:00Z" in note
        assert await self._follower_runs(extensions_maker) == []

    @pytest.mark.parametrize(
        "elapsed",
        [
            pytest.param(_ONE_DAY, id="at-the-deadline"),
            pytest.param(_ONE_DAY + timedelta(days=30), id="long-after"),
        ],
    )
    @pytest.mark.parametrize(
        "leader_history",
        [
            pytest.param(None, id="leader-never-ran"),
            pytest.param(SyncStatusEnum.FAILED, id="leader-always-failed"),
        ],
    )
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_runs_once_the_window_has_elapsed(
        self,
        extensions_maker,
        mocker,
        elapsed: timedelta,
        leader_history: SyncStatusEnum | None,
    ) -> None:
        """Run the follower's sync with no skip note once the window has passed.

        It applies whether the leader has no recorded run at all, as with a
        disabled row, or only runs that could not list the inventory.
        """
        if leader_history is not None:
            await _record_run(extensions_maker, _LEADER, leader_history)
        self._at(mocker, _SEEDED_AT + elapsed)

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        assert note is None
        (run,) = await self._follower_runs(extensions_maker)
        assert run.status == SyncStatusEnum.SUCCESS

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_logs_the_release(self, mocker, caplog) -> None:
        """Log that the follower ran without its leader, naming both."""
        self._at(mocker, _SEEDED_AT + _ONE_DAY)

        with caplog.at_level(logging.INFO, logger="app.extensions.apps.inventory.sync"):
            await run_scheduled_inventory_sync(
                syncer=_FOLLOWER,
                after_syncer=_LEADER,
                after_syncer_since=_SEEDED_AT_ISO,
                after_syncer_max_wait=_ONE_DAY_SECONDS,
            )

        assert _FOLLOWER in caplog.text
        assert _LEADER in caplog.text

    @pytest.mark.parametrize(
        "anchor",
        [
            pytest.param(None, id="missing"),
            pytest.param("", id="empty"),
            pytest.param("yesterday", id="garbage"),
            pytest.param("2026-09-01T00:00:00", id="naive"),
            pytest.param(12345, id="not-a-string"),
            pytest.param("9998-01-01T00:00:00+00:00", id="future"),
            pytest.param(
                (_SEEDED_AT + timedelta(minutes=5, seconds=1)).isoformat(),
                id="beyond-skew",
            ),
        ],
    )
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_an_unreadable_anchor_does_not_wedge_the_follower(
        self, extensions_maker, mocker, caplog, anchor: Any
    ) -> None:
        """Run the follower, with a warning, when the anchor is unusable.

        The anchor sits in operator-editable row ``kwargs``, and a request queued
        by an older seeder has none; neither may make the wait unbounded. An
        anchor further ahead than a small clock skew, as an edit leaves it, is
        unusable too, since it would push the deadline arbitrarily far out.
        """
        self._at(mocker, _SEEDED_AT)

        with caplog.at_level(
            logging.WARNING, logger="app.extensions.apps.inventory.sync"
        ):
            note = await run_scheduled_inventory_sync(
                syncer=_FOLLOWER,
                after_syncer=_LEADER,
                after_syncer_since=anchor,
                after_syncer_max_wait=_ONE_DAY_SECONDS,
            )

        assert note is None
        assert len(await self._follower_runs(extensions_maker)) == 1
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_tolerates_an_anchor_stamped_on_a_slightly_fast_clock(
        self, extensions_maker, mocker
    ) -> None:
        """Keep waiting on an anchor a little ahead of this worker's clock.

        The seeder stamps the anchor on another process's clock, and the first
        beat fire follows the seed at once, so a small skew must not release the
        follower ahead of its leader at bring-up.
        """
        self._at(mocker, _SEEDED_AT - timedelta(minutes=1))

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        assert note is not None
        assert "or at 2026-09-02T00:00:00Z" in note
        assert await self._follower_runs(extensions_maker) == []

    @pytest.mark.parametrize(
        "max_wait",
        [
            pytest.param(None, id="missing"),
            pytest.param("86400", id="not-an-int"),
            pytest.param(True, id="bool"),
            pytest.param(0, id="zero"),
            pytest.param(-1, id="negative"),
            pytest.param(10**12, id="deadline-overflows"),
            pytest.param(10**20, id="window-overflows"),
        ],
    )
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_an_unreadable_window_does_not_wedge_the_follower(
        self, extensions_maker, mocker, caplog, max_wait: Any
    ) -> None:
        """Run the follower, with a warning, when the window is unusable.

        The window sits in the same operator-editable ``kwargs`` as the anchor,
        and a request queued before the seeder wrote it has none.
        """
        self._at(mocker, _SEEDED_AT + timedelta(seconds=1))

        with caplog.at_level(
            logging.WARNING, logger="app.extensions.apps.inventory.sync"
        ):
            note = await run_scheduled_inventory_sync(
                syncer=_FOLLOWER,
                after_syncer=_LEADER,
                after_syncer_since=_SEEDED_AT_ISO,
                after_syncer_max_wait=max_wait,
            )

        assert note is None
        assert len(await self._follower_runs(extensions_maker)) == 1
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_normalises_an_offset_anchor_to_utc(self, mocker) -> None:
        """Measure an anchor written with a non-UTC offset from the same instant."""
        self._at(mocker, _SEEDED_AT + _ONE_DAY - timedelta(seconds=1))

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since="2026-09-01T02:00:00+02:00",
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        assert note is not None
        assert "2026-09-02T00:00:00Z" in note

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_honours_a_configured_window(self, extensions_maker, mocker) -> None:
        """Release the follower after the window its request carries."""
        self._at(mocker, _SEEDED_AT + timedelta(hours=1))

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=3600,
        )

        assert note is None
        assert len(await self._follower_runs(extensions_maker)) == 1

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_a_completed_leader_releases_the_follower_inside_the_window(
        self, extensions_maker, mocker
    ) -> None:
        """Run the follower as soon as the leader completes, before the deadline."""
        await _record_run(extensions_maker, _LEADER)
        self._at(mocker, _SEEDED_AT)

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        assert note is None
        assert len(await self._follower_runs(extensions_maker)) == 1

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("follower_run")
    async def test_a_started_first_run_after_the_window_still_defers_to_a_prior_run(
        self, extensions_maker, mocker
    ) -> None:
        """Skip a started first run past the window once the follower has run."""
        await _record_run(extensions_maker, _FOLLOWER)
        self._at(mocker, _SEEDED_AT + _ONE_DAY)

        note = await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
            first_run_only=True,
        )

        assert note is not None
        assert _FOLLOWER in note
        assert len(await self._follower_runs(extensions_maker)) == 1

    @pytest.mark.asyncio
    async def test_a_late_leader_does_not_start_a_released_follower_again(
        self, extensions_maker, mocker, mock_remote_api
    ) -> None:
        """Send no first-run start when the leader completes after the release.

        The follower ran on its own once the window passed, so its own history
        is what stops the leader's run from starting it a second time.
        """
        _route_sessions(mocker, extensions_maker)
        mocker.patch(
            "app.extensions.apps.inventory.sync.get_syncers_standalone",
            return_value=[
                _LeaderSyncer(inventory_api=mock_remote_api),
                _FollowerSyncer(inventory_api=mock_remote_api),
            ],
        )
        send_task = mocker.patch.object(celery, "send_task")
        self._at(mocker, _SEEDED_AT + _ONE_DAY)
        await run_scheduled_inventory_sync(
            syncer=_FOLLOWER,
            after_syncer=_LEADER,
            after_syncer_since=_SEEDED_AT_ISO,
            after_syncer_max_wait=_ONE_DAY_SECONDS,
        )

        await run_scheduled_inventory_sync(
            syncer=_LEADER,
            follower_syncers=[_FOLLOWER],
            follower_syncers_since={_FOLLOWER: _SEEDED_AT_ISO},
            follower_syncers_max_wait=_ONE_DAY_SECONDS,
        )

        send_task.assert_not_called()
        assert len(await self._follower_runs(extensions_maker)) == 1


class TestStartFollowerFirstRuns:
    """Test the leader-side start of followers that have never run."""

    @pytest.fixture
    def send_task(self, mocker, extensions_maker) -> MagicMock:
        """Route the gate at the in-memory DB and replace the broker call."""
        _route_sessions(mocker, extensions_maker)
        return mocker.patch.object(celery, "send_task")

    @pytest.mark.asyncio
    async def test_starts_a_never_run_follower_once(self, extensions_maker, send_task):
        """Start a configured follower with no run of its own, exactly once."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        await start_follower_first_runs(
            _PMM_STUB_NAME, [_MYSQL_STUB_NAME], [_StubPMMSyncer(), _StubMySQLSyncer()]
        )

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK,
            kwargs=_follower_kick(_MYSQL_STUB_NAME, _PMM_STUB_NAME),
        )

    @pytest.mark.asyncio
    async def test_forwards_the_follower_wait_anchor(self, extensions_maker, send_task):
        """Carry the anchor and window so the start holds every key its fire has.

        The identical-task guard matches meta by containment, so a start without
        either would stop refusing a beat fire of the follower in flight.
        """
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        await start_follower_first_runs(
            _PMM_STUB_NAME,
            [_MYSQL_STUB_NAME],
            [_StubPMMSyncer(), _StubMySQLSyncer()],
            {_MYSQL_STUB_NAME: _SEEDED_AT_ISO},
            _ONE_DAY_SECONDS,
        )

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK,
            kwargs=_follower_kick(
                _MYSQL_STUB_NAME, _PMM_STUB_NAME, _SEEDED_AT_ISO, _ONE_DAY_SECONDS
            ),
        )

    @pytest.mark.parametrize(
        "since",
        [
            pytest.param({}, id="follower-absent"),
            pytest.param({_MYSQL_STUB_NAME: 42}, id="anchor-not-a-string"),
            pytest.param(["not", "a", "mapping"], id="not-a-mapping"),
        ],
    )
    @pytest.mark.asyncio
    async def test_omits_an_anchor_it_does_not_hold(
        self, extensions_maker, send_task, since: Any
    ):
        """Start the follower without an anchor when none usable was handed over.

        An operator-covered follower is listed by the leader but not seeded, so
        it has no anchor; operator-edited meta may hold anything at all.
        """
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        await start_follower_first_runs(
            _PMM_STUB_NAME,
            [_MYSQL_STUB_NAME],
            [_StubPMMSyncer(), _StubMySQLSyncer()],
            since,
        )

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK,
            kwargs=_follower_kick(_MYSQL_STUB_NAME, _PMM_STUB_NAME),
        )

    @pytest.mark.parametrize(
        "max_wait",
        [
            pytest.param(None, id="missing"),
            pytest.param("86400", id="not-an-int"),
            pytest.param(True, id="bool"),
        ],
    )
    @pytest.mark.asyncio
    async def test_omits_a_window_it_does_not_hold(
        self, extensions_maker, send_task, max_wait: Any
    ):
        """Start the follower without a window when none usable was handed over."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)

        await start_follower_first_runs(
            _PMM_STUB_NAME,
            [_MYSQL_STUB_NAME],
            [_StubPMMSyncer(), _StubMySQLSyncer()],
            {_MYSQL_STUB_NAME: _SEEDED_AT_ISO},
            max_wait,
        )

        send_task.assert_called_once_with(
            EXECUTE_TASK_BY_NAME_TASK,
            kwargs=_follower_kick(_MYSQL_STUB_NAME, _PMM_STUB_NAME, _SEEDED_AT_ISO),
        )

    @pytest.mark.asyncio
    async def test_leaves_a_follower_that_already_ran(
        self, extensions_maker, send_task, mocker
    ):
        """Leave a follower alone once it has any run, without reading the leader.

        Once every follower has run, the leader's pass is not looked up, so a
        steady-state leader run adds no scan over its sync items.
        """
        await _record_run(extensions_maker, _PMM_STUB_NAME)
        await _record_run(extensions_maker, _MYSQL_STUB_NAME, SyncStatusEnum.FAILED)
        leader_lookup = mocker.spy(SyncItemManager, "inventory_sync_completed")

        await start_follower_first_runs(
            _PMM_STUB_NAME, [_MYSQL_STUB_NAME], [_StubPMMSyncer(), _StubMySQLSyncer()]
        )

        send_task.assert_not_called()
        leader_lookup.assert_not_called()

    @pytest.mark.asyncio
    async def test_waits_for_a_completed_leader_pass(self, extensions_maker, send_task):
        """Start nothing while the leader's only pass could not list the inventory."""
        await _record_run(extensions_maker, _PMM_STUB_NAME, SyncStatusEnum.FAILED)

        await start_follower_first_runs(
            _PMM_STUB_NAME, [_MYSQL_STUB_NAME], [_StubPMMSyncer(), _StubMySQLSyncer()]
        )

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
            await start_follower_first_runs(
                _PMM_STUB_NAME, [_MYSQL_STUB_NAME], [_StubPMMSyncer()]
            )

        send_task.assert_not_called()
        assert _MYSQL_STUB_NAME in caplog.text

    @pytest.mark.asyncio
    async def test_logs_rather_than_raises_when_the_broker_refuses(
        self, extensions_maker, send_task, caplog
    ):
        """Log an enqueue failure and return, leaving the follower's own schedule."""
        await _record_run(extensions_maker, _PMM_STUB_NAME)
        send_task.side_effect = KombuError("broker unreachable")

        with caplog.at_level(
            logging.ERROR, logger="app.extensions.apps.inventory.sync"
        ):
            await start_follower_first_runs(
                _PMM_STUB_NAME,
                [_MYSQL_STUB_NAME],
                [_StubPMMSyncer(), _StubMySQLSyncer()],
            )

        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert record.exc_info is not None
        assert _MYSQL_STUB_NAME in record.getMessage()

    @pytest.mark.parametrize(
        ("configured", "broker_replies"),
        [
            pytest.param([_StubFactsSyncer], [None], id="first-is-unconfigured"),
            pytest.param(
                [_StubMySQLSyncer, _StubFactsSyncer],
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
        configured: list[type],
        broker_replies: list[KombuError | None],
    ):
        """Start the next follower after one is passed over, on either skip branch.

        Both branches log and move on, so a follower listed after the skipped one
        is still started in the same call.
        """
        await _record_run(extensions_maker, _PMM_STUB_NAME)
        send_task.side_effect = broker_replies

        await start_follower_first_runs(
            _PMM_STUB_NAME,
            [_MYSQL_STUB_NAME, _FACTS_STUB_NAME],
            [syncer_cls() for syncer_cls in configured],
        )

        assert send_task.call_count == len(broker_replies)
        send_task.assert_called_with(
            EXECUTE_TASK_BY_NAME_TASK,
            kwargs=_follower_kick(_FACTS_STUB_NAME, _PMM_STUB_NAME),
        )


async def _assert_the_leader_run_starts_its_follower(
    maker: async_sessionmaker[AsyncSession], mocker, mock_remote_api
) -> None:
    """Run the leader through the public call and check it starts the follower.

    The kick is gated on the leader's completed pass, read through a session of its
    own. On a fresh database only the run this call makes can satisfy that gate, so
    a kick at all proves the pass was persisted before the gate read it.
    """
    _route_sessions(mocker, maker)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_syncers_standalone",
        return_value=[
            _LeaderSyncer(inventory_api=mock_remote_api),
            _FollowerSyncer(inventory_api=mock_remote_api),
        ],
    )
    send_task = mocker.patch.object(celery, "send_task")

    await run_scheduled_inventory_sync(
        syncer=_LEADER,
        follower_syncers=[_FOLLOWER],
        follower_syncers_since={_FOLLOWER: _SEEDED_AT_ISO},
        follower_syncers_max_wait=_ONE_DAY_SECONDS,
    )

    async with maker() as session:
        assert await SyncItemManager.inventory_sync_completed(session, _LEADER)
        assert await SyncInstanceManager.list(session, syncer=_FOLLOWER) == []
    send_task.assert_called_once_with(
        EXECUTE_TASK_BY_NAME_TASK,
        kwargs=_follower_kick(_FOLLOWER, _LEADER, _SEEDED_AT_ISO, _ONE_DAY_SECONDS),
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
