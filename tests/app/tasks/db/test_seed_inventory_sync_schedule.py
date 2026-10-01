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

"""Define tests for seeding the default inventory-sync schedule.

Drive :func:`app.tasks.db.seed.seed_system_periodic_tasks` against an in-memory
celery-beat database. Both session-maker seams are redirected to the same maker:
``init_periodic_tasks_db`` resolves its own module binding while the pre-seed
lookup resolves ``app.tasks.db.seed``'s alias, so patching only one would leave
the read path and the write path on different databases.
"""

import inspect
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy_celery_beat.models import IntervalSchedule, Period, PeriodicTask
from sqlmodel import SQLModel

import app.tasks.db.seed as seed_module
from app.celery import celery
from app.core.celery import utils as celery_utils
from app.core.celery.crud import BasePeriodicTaskManager
from app.core.celery.models import IntervalSchedule as IntervalScheduleOption
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.extensions.apps.inventory.sync import (
    run_scheduled_inventory_sync,
    start_follower_first_runs,
)
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.models import (
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.syncers.system_facts.syncer import SystemFactsSyncer
from app.tasks.celery import execute_task_by_name
from app.tasks.config import InventorySyncSchedule, tasks_settings
from app.tasks.models import (
    EXECUTE_TASK_BY_NAME_TASK,
    INVENTORY_SYNC_AFTER_KEY,
    INVENTORY_SYNC_FIRST_RUN_KEY,
    INVENTORY_SYNC_FOLLOWERS_KEY,
    INVENTORY_SYNC_TASK_NAME,
)
from tests.app.db_schema import apply_schema
from tests.app.tasks.conftest import (
    MYSQL_SYNCER,
    PMM_SYNCER,
    SYSTEM_FACTS_SYNCER,
    UNMEASURED_HOST_FACTS_SYNCER,
)

FIFTEEN_MINUTES = IntervalScheduleOption(every=15, period=Period.MINUTES)
ONE_DAY = IntervalScheduleOption(every=1, period=Period.DAYS)
OPERATOR_TASK_NAME = "run_inventory-sync_15_minutes"


@pytest_asyncio.fixture(name="tasks_maker")
async def tasks_maker_fixture() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a session maker bound to an in-memory tasks DB."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


@pytest.fixture(name="configured")
def configured_fixture(mocker, beat_maker) -> None:
    """Pin both session-maker seams and the PMM-targeted default settings."""
    mocker.patch.object(
        celery_utils, "get_async_session_maker", return_value=beat_maker
    )
    mocker.patch.object(
        seed_module, "get_celery_beat_session_maker", return_value=beat_maker
    )
    mocker.patch.object(tasks_settings, "INVENTORY_SYNC_INTERVAL", FIFTEEN_MINUTES)
    mocker.patch.object(tasks_settings, "INVENTORY_SYNC_SYNCER", PMM_SYNCER)


async def _insert_operator_row(
    session: AsyncSession,
    kwargs: str | None,
    *,
    name: str = OPERATOR_TASK_NAME,
    enabled: bool = True,
) -> None:
    """Insert a beat row shaped like one the operator attached through the API."""
    schedule = IntervalSchedule(every=15, period=Period.MINUTES)
    session.add(schedule)
    await session.flush()
    session.add(
        PeriodicTask(
            name=name,
            task="app.tasks.celery.execute_task_by_name",
            schedule_model=schedule,
            kwargs=kwargs,
            enabled=enabled,
        )
    )
    await session.commit()


def _operator_kwargs(syncer: str | None = None) -> str:
    """Build the ``kwargs`` payload an operator-created schedule carries."""
    return json.dumps(
        {
            "task_name": INVENTORY_SYNC_TASK_NAME,
            "periodic_task_name": OPERATOR_TASK_NAME,
            **({"execution_data": {"meta": {"syncer": syncer}}} if syncer else {}),
        }
    )


async def _stamp_seeded_row(
    beat_maker: async_sessionmaker[AsyncSession], **fields: datetime
) -> None:
    """Write timing fields onto the seeded row, standing in for a beat dispatch."""
    async with beat_maker() as session:
        row = await BasePeriodicTaskManager.first(
            session, name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME
        )
        assert row is not None
        for field, value in fields.items():
            setattr(row, field, value)
        session.add(row)
        await session.commit()


async def _seeded_rows(
    beat_maker: async_sessionmaker[AsyncSession],
) -> list[PeriodicTask]:
    """Return the beat rows carrying the seeded inventory-sync schedule name."""
    async with beat_maker() as session:
        return await BasePeriodicTaskManager.list(
            session, name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME
        )


@pytest.mark.asyncio
async def test_fresh_seed_creates_the_pinned_schedule(configured, beat_maker) -> None:
    """Assert an empty beat store gets exactly one PMM-pinned schedule."""
    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert row.task == "app.tasks.celery.execute_task_by_name"
    kwargs = json.loads(row.kwargs)
    assert kwargs["task_name"] == INVENTORY_SYNC_TASK_NAME
    assert kwargs["execution_data"]["meta"]["syncer"] == PMM_SYNCER
    assert (row.schedule_model.every, row.schedule_model.period) == (
        15,
        Period.MINUTES,
    )


@pytest.mark.asyncio
async def test_seeding_twice_is_idempotent(configured, beat_maker) -> None:
    """Assert the seeder does not read its own row as an operator schedule."""
    await seed_module.seed_system_periodic_tasks()
    (first,) = await _seeded_rows(beat_maker)

    await seed_module.seed_system_periodic_tasks()

    (second,) = await _seeded_rows(beat_maker)
    assert second.id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize("syncer", [PMM_SYNCER, None], ids=["pinned", "sync-all"])
async def test_operator_row_covering_the_syncer_blocks_the_default(
    configured, beat_maker, syncer: str | None
) -> None:
    """Assert an operator schedule covering PMM suppresses the seeded default."""
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(syncer))

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []
    async with beat_maker() as session:
        operator_row = await BasePeriodicTaskManager.first(
            session, name=OPERATOR_TASK_NAME
        )
    assert operator_row is not None
    assert json.loads(operator_row.kwargs) == json.loads(_operator_kwargs(syncer))


@pytest.mark.asyncio
async def test_operator_row_for_another_syncer_does_not_block(
    configured, beat_maker
) -> None:
    """Assert a MySQL-pinned operator schedule leaves the PMM default seeded."""
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(MYSQL_SYNCER))

    await seed_module.seed_system_periodic_tasks()

    assert len(await _seeded_rows(beat_maker)) == 1
    async with beat_maker() as session:
        operator_row = await BasePeriodicTaskManager.first(
            session, name=OPERATOR_TASK_NAME
        )
    assert operator_row is not None
    assert json.loads(operator_row.kwargs)["execution_data"]["meta"]["syncer"] == (
        MYSQL_SYNCER
    )


@pytest.mark.asyncio
async def test_disabled_operator_row_blocks_the_default(configured, beat_maker) -> None:
    """Assert a paused operator schedule is honoured rather than resumed."""
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(PMM_SYNCER), enabled=False)

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []


@pytest.mark.asyncio
async def test_operator_attaching_after_upgrade_converges_to_one_row(
    configured, beat_maker
) -> None:
    """Assert a post-upgrade operator schedule replaces the seeded default."""
    await seed_module.seed_system_periodic_tasks()
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(PMM_SYNCER))

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []
    async with beat_maker() as session:
        operator_row = await BasePeriodicTaskManager.first(
            session, name=OPERATOR_TASK_NAME
        )
    assert operator_row is not None


@pytest.mark.asyncio
async def test_unsetting_the_interval_removes_a_seeded_row(
    configured, mocker, beat_maker
) -> None:
    """Assert clearing INVENTORY_SYNC_INTERVAL removes the seeded schedule."""
    await seed_module.seed_system_periodic_tasks()
    mocker.patch.object(tasks_settings, "INVENTORY_SYNC_INTERVAL", None)

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []


@pytest.mark.asyncio
async def test_disabling_the_seeded_row_survives_a_reseed(
    configured, beat_maker
) -> None:
    """Assert an operator-disabled seeded schedule is not silently resumed."""
    await seed_module.seed_system_periodic_tasks()
    async with beat_maker() as session:
        row = await BasePeriodicTaskManager.first(
            session, name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME
        )
        row.enabled = False
        session.add(row)
        await session.commit()

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert row.enabled is False


@pytest.mark.asyncio
async def test_malformed_operator_kwargs_fails_closed(configured, beat_maker) -> None:
    """Assert an undecodable operator row neither breaks boot nor double-schedules."""
    async with beat_maker() as session:
        await _insert_operator_row(session, "{not json")

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []


@pytest.mark.asyncio
async def test_unreadable_beat_store_skips_a_first_time_default(
    configured, mocker
) -> None:
    """Assert a failing pre-seed lookup skips the default instead of raising."""
    mocker.patch.object(
        seed_module.PeriodicTaskManager,
        "list_by_task_names",
        autospec=True,
        side_effect=SQLAlchemyError("beat store unreadable"),
    )

    assert (
        await seed_module._seeded_inventory_sync_schedule(
            seed_module.INVENTORY_SYNC_SCHEDULE_NAME, PMM_SYNCER, FIFTEEN_MINUTES
        )
        is None
    )


@pytest.mark.asyncio
async def test_unreadable_beat_store_keeps_an_already_seeded_default(
    configured, beat_maker, mocker
) -> None:
    """Assert a failing lookup does not let the orphan cleanup drop the default.

    Omitting the entry is not neutral: ``init_periodic_tasks_db`` deletes every
    ``tasks__`` row it was not handed, so a data-level lookup failure would stop
    the sync on a deployment that was already syncing.
    """
    await seed_module.seed_system_periodic_tasks()
    mocker.patch.object(
        seed_module.PeriodicTaskManager,
        "list_by_task_names",
        autospec=True,
        side_effect=SQLAlchemyError("malformed persisted kwargs"),
    )

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert json.loads(row.kwargs)["execution_data"]["meta"]["syncer"] == PMM_SYNCER


@pytest.mark.asyncio
async def test_startup_seeds_the_schedule(configured, beat_maker, tasks_maker, mocker):
    """Assert startup itself provisions the schedule, not just the seeder.

    Every other test in this module drives ``seed_system_periodic_tasks``
    directly, so none of them would notice ``init_tasks_db`` losing the call.
    Both databases are real here: the tasks tables the system-task half writes,
    and the beat store the assertion reads.
    """
    mocker.patch.object(
        seed_module, "get_async_session_maker", return_value=tasks_maker
    )

    await seed_module.init_tasks_db()

    (row,) = await _seeded_rows(beat_maker)
    assert json.loads(row.kwargs)["execution_data"]["meta"]["syncer"] == PMM_SYNCER


@pytest.mark.asyncio
async def test_the_seeded_schedule_is_marked_due_in_the_database(
    configured, beat_maker
) -> None:
    """Assert a fresh seed persists the due-now marker and no fabricated run time."""
    before = utc_now()

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert row.start_time is not None
    assert before <= make_datetime_utc(row.start_time) <= utc_now()
    assert row.last_run_at is None


@pytest.mark.asyncio
async def test_reseeding_preserves_a_recorded_dispatch(configured, beat_maker) -> None:
    """Assert a boot after the first dispatch neither re-arms nor re-dates the row."""
    await seed_module.seed_system_periodic_tasks()
    dispatched_at = utc_now()
    (seeded,) = await _seeded_rows(beat_maker)
    marked_at = seeded.start_time
    await _stamp_seeded_row(beat_maker, last_run_at=dispatched_at)

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert make_datetime_utc(row.last_run_at) == dispatched_at
    assert row.start_time == marked_at


@pytest.mark.asyncio
async def test_the_other_seeded_schedules_keep_their_timing(
    configured, beat_maker
) -> None:
    """Assert the marker is opt-in per entry, so no sibling schedule acquires it."""
    await seed_module.seed_system_periodic_tasks()

    async with beat_maker() as session:
        rows = await BasePeriodicTaskManager.list(session)
    siblings = [
        row for row in rows if row.name != seed_module.INVENTORY_SYNC_SCHEDULE_NAME
    ]
    assert siblings
    assert all(row.start_time is None for row in siblings)
    assert all(row.last_run_at is None for row in siblings)


@pytest.mark.asyncio
async def test_upgrading_an_install_that_already_dispatched_adds_no_marker(
    configured, beat_maker
) -> None:
    """Assert an upgrade leaves an existing row's persisted run time to decide it."""
    dispatched_at = utc_now()
    async with beat_maker() as session:
        await _insert_operator_row(
            session,
            _operator_kwargs(PMM_SYNCER),
            name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME,
        )
    await _stamp_seeded_row(beat_maker, last_run_at=dispatched_at)

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert make_datetime_utc(row.last_run_at) == dispatched_at
    assert row.start_time is None


@pytest.mark.asyncio
async def test_upgrading_an_install_that_never_dispatched_stays_unmarked(
    configured, beat_maker
) -> None:
    """Assert the accepted residual: an existing never-run row keeps today's timing.

    Marking it would mean writing timing onto a row that already exists, which is
    what keeps an upgrade from dispatching a sync it would not otherwise have
    dispatched. Pinned so the residual stays a decision rather than becoming an
    unnoticed regression.
    """
    async with beat_maker() as session:
        await _insert_operator_row(
            session,
            _operator_kwargs(PMM_SYNCER),
            name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME,
        )

    await seed_module.seed_system_periodic_tasks()

    (row,) = await _seeded_rows(beat_maker)
    assert row.start_time is None
    assert row.last_run_at is None


@pytest.fixture(name="with_system_facts_schedule")
def with_system_facts_schedule_fixture(configured, mocker) -> str:
    """Add a daily SystemFacts schedule beside the PMM-pinned scalar default."""
    mocker.patch.object(
        tasks_settings,
        "INVENTORY_SYNC_SCHEDULES",
        [InventorySyncSchedule(syncer=SYSTEM_FACTS_SYNCER, interval=ONE_DAY)],
    )
    return seed_module._inventory_sync_schedule_name(SYSTEM_FACTS_SYNCER)


async def _rows_named(
    beat_maker: async_sessionmaker[AsyncSession], name: str
) -> list[PeriodicTask]:
    """Return the beat rows carrying ``name``."""
    async with beat_maker() as session:
        return await BasePeriodicTaskManager.list(session, name=name)


@pytest.mark.asyncio
async def test_each_entry_seeds_its_own_schedule(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert a configured entry seeds a second row beside the scalar default."""
    await seed_module.seed_system_periodic_tasks()

    (scalar,) = await _seeded_rows(beat_maker)
    (entry,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert scalar.name != entry.name
    assert json.loads(entry.kwargs)["execution_data"]["meta"]["syncer"] == (
        SYSTEM_FACTS_SYNCER
    )


@pytest.mark.asyncio
async def test_the_scalar_defaults_seeded_name_is_unchanged(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert the historical row name survives, so an upgrade keeps its schedule.

    A changed name would leave the existing row unmatched, and the orphan cleanup
    would delete it — with ``due_on_first_seed`` firing a sync on the re-create.
    """
    await seed_module.seed_system_periodic_tasks()

    (scalar,) = await _seeded_rows(beat_maker)
    assert scalar.name == seed_module.INVENTORY_SYNC_SCHEDULE_NAME


@pytest.mark.asyncio
async def test_a_per_entry_row_is_stable_across_seeds(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert re-seeding updates the entry's row rather than re-creating it."""
    await seed_module.seed_system_periodic_tasks()
    (first,) = await _rows_named(beat_maker, with_system_facts_schedule)

    await seed_module.seed_system_periodic_tasks()

    (second,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert second.id == first.id


@pytest.mark.asyncio
async def test_an_entry_carries_its_own_interval(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert the entry runs daily while the scalar default stays at 15 minutes."""
    await seed_module.seed_system_periodic_tasks()

    (scalar,) = await _seeded_rows(beat_maker)
    (entry,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert (entry.schedule_model.every, entry.schedule_model.period) == (
        1,
        Period.DAYS,
    )
    assert (scalar.schedule_model.every, scalar.schedule_model.period) == (
        15,
        Period.MINUTES,
    )


@pytest.mark.asyncio
async def test_an_operator_row_suppresses_only_the_entry_it_covers(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert coverage is decided per schedule, not once for all of them."""
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(SYSTEM_FACTS_SYNCER))

    await seed_module.seed_system_periodic_tasks()

    assert await _rows_named(beat_maker, with_system_facts_schedule) == []
    assert len(await _seeded_rows(beat_maker)) == 1


@pytest.mark.asyncio
async def test_a_store_failure_withholds_a_first_time_entry(
    with_system_facts_schedule, beat_maker, mocker
) -> None:
    """Assert an unreadable store neither double-schedules nor un-schedules.

    A first-time entry is withheld; a row this seeder already owns is re-seeded,
    which is what keeps the orphan cleanup from deleting it.
    """
    await seed_module.seed_system_periodic_tasks()
    (owned,) = await _rows_named(beat_maker, with_system_facts_schedule)
    mocker.patch.object(
        seed_module.PeriodicTaskManager,
        "list_by_task_names",
        autospec=True,
        side_effect=SQLAlchemyError("beat store unreadable"),
    )

    assert (
        await seed_module._seeded_inventory_sync_schedule(
            with_system_facts_schedule, SYSTEM_FACTS_SYNCER, ONE_DAY
        )
    ) is not None
    assert (
        await seed_module._seeded_inventory_sync_schedule(
            seed_module._inventory_sync_schedule_name(MYSQL_SYNCER),
            MYSQL_SYNCER,
            ONE_DAY,
        )
        is None
    )
    assert owned.name == with_system_facts_schedule


@pytest.mark.asyncio
async def test_removing_an_entry_orphan_cleans_only_its_row(
    with_system_facts_schedule, beat_maker, mocker
) -> None:
    """Assert dropping an entry stops its collection without touching the scalar."""
    await seed_module.seed_system_periodic_tasks()
    assert len(await _rows_named(beat_maker, with_system_facts_schedule)) == 1

    mocker.patch.object(tasks_settings, "INVENTORY_SYNC_SCHEDULES", [])
    await seed_module.seed_system_periodic_tasks()

    assert await _rows_named(beat_maker, with_system_facts_schedule) == []
    assert len(await _seeded_rows(beat_maker)) == 1


def _meta(row: PeriodicTask) -> dict[str, Any]:
    """Return the ``execution_data.meta`` a beat row hands the executor."""
    return json.loads(row.kwargs)["execution_data"]["meta"]


@pytest.mark.asyncio
async def test_the_pinned_default_names_its_followers(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert the default lists each per-syncer schedule and each names the default.

    Both rows stay due at first seed: the ordering is carried by the meta the
    callable reads, not by delaying either row.
    """
    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert _meta(primary) == {
        "syncer": PMM_SYNCER,
        INVENTORY_SYNC_FOLLOWERS_KEY: [SYSTEM_FACTS_SYNCER],
    }
    assert _meta(follower) == {
        "syncer": SYSTEM_FACTS_SYNCER,
        INVENTORY_SYNC_AFTER_KEY: PMM_SYNCER,
    }
    assert primary.start_time is not None
    assert follower.start_time is not None


@pytest.mark.asyncio
async def test_the_first_measurement_pass_follows_the_default(
    configured, mocker, beat_maker
) -> None:
    """Assert the side-car pair seeds the pass as a follower beside the daily run.

    Following the default is what keeps a fresh install's first pass from probing
    an inventory PMM has not populated yet.
    """
    mocker.patch.object(
        tasks_settings,
        "INVENTORY_SYNC_SCHEDULES",
        [
            InventorySyncSchedule(syncer=SYSTEM_FACTS_SYNCER, interval=ONE_DAY),
            InventorySyncSchedule(
                syncer=UNMEASURED_HOST_FACTS_SYNCER, interval=FIFTEEN_MINUTES
            ),
        ],
    )

    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(
        beat_maker,
        seed_module._inventory_sync_schedule_name(UNMEASURED_HOST_FACTS_SYNCER),
    )
    assert _meta(primary)[INVENTORY_SYNC_FOLLOWERS_KEY] == [
        SYSTEM_FACTS_SYNCER,
        UNMEASURED_HOST_FACTS_SYNCER,
    ]
    assert _meta(follower) == {
        "syncer": UNMEASURED_HOST_FACTS_SYNCER,
        INVENTORY_SYNC_AFTER_KEY: PMM_SYNCER,
    }


@pytest.mark.asyncio
async def test_a_default_without_schedules_carries_no_followers(
    configured, beat_maker
) -> None:
    """Assert the default's meta gains no follower key when nothing follows it."""
    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    assert _meta(primary) == {"syncer": PMM_SYNCER}


@pytest.mark.asyncio
async def test_a_follower_without_a_seeded_default_is_not_gated(
    with_system_facts_schedule, mocker, beat_maker
) -> None:
    """Assert a standalone install's schedule runs at first seed, waiting on nothing."""
    mocker.patch.object(tasks_settings, "INVENTORY_SYNC_INTERVAL", None)

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert _meta(follower) == {"syncer": SYSTEM_FACTS_SYNCER}
    assert follower.start_time is not None


@pytest.mark.asyncio
async def test_an_operator_covered_default_leaves_the_follower_ungated(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert no follower waits on a default this seeder does not own.

    The operator's row is authoritative and carries no ordering, so nothing would
    ever start a follower gated on it.
    """
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(PMM_SYNCER))

    await seed_module.seed_system_periodic_tasks()

    assert await _seeded_rows(beat_maker) == []
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert _meta(follower) == {"syncer": SYSTEM_FACTS_SYNCER}


@pytest.mark.asyncio
async def test_an_operator_covered_follower_stays_listed_on_the_default(
    configured, mocker, beat_maker
) -> None:
    """Assert the default still names a follower whose own schedule is the operator's.

    The kick starts it only if it has never run, so the operator's schedule sees
    at most one extra first run.
    """
    mocker.patch.object(
        tasks_settings,
        "INVENTORY_SYNC_SCHEDULES",
        [InventorySyncSchedule(syncer=SYSTEM_FACTS_SYNCER, interval=ONE_DAY)],
    )
    async with beat_maker() as session:
        await _insert_operator_row(session, _operator_kwargs(SYSTEM_FACTS_SYNCER))

    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    assert _meta(primary)[INVENTORY_SYNC_FOLLOWERS_KEY] == [SYSTEM_FACTS_SYNCER]


@pytest.mark.asyncio
async def test_reseeding_adds_the_ordering_to_existing_rows_only(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert an upgrade reconciles the system rows and leaves the operator's alone.

    The existing system rows keep their recorded timing, so gaining the ordering
    does not move either schedule's next run.
    """
    dispatched_at = utc_now()
    operator_kwargs = _operator_kwargs(MYSQL_SYNCER)
    async with beat_maker() as session:
        await _insert_operator_row(
            session,
            json.dumps(
                {
                    "task_name": INVENTORY_SYNC_TASK_NAME,
                    "execution_data": {"meta": {"syncer": PMM_SYNCER}},
                }
            ),
            name=seed_module.INVENTORY_SYNC_SCHEDULE_NAME,
        )
        await _insert_operator_row(
            session,
            json.dumps(
                {
                    "task_name": INVENTORY_SYNC_TASK_NAME,
                    "execution_data": {"meta": {"syncer": SYSTEM_FACTS_SYNCER}},
                }
            ),
            name=with_system_facts_schedule,
        )
        await _insert_operator_row(session, operator_kwargs)
    async with beat_maker() as session:
        for row in await BasePeriodicTaskManager.list(session):
            row.last_run_at = dispatched_at
            session.add(row)
        await session.commit()

    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    (operator_row,) = await _rows_named(beat_maker, OPERATOR_TASK_NAME)
    assert _meta(primary)[INVENTORY_SYNC_FOLLOWERS_KEY] == [SYSTEM_FACTS_SYNCER]
    assert _meta(follower)[INVENTORY_SYNC_AFTER_KEY] == PMM_SYNCER
    assert operator_row.kwargs == operator_kwargs
    for row in (primary, follower):
        assert make_datetime_utc(row.last_run_at) == dispatched_at
        assert row.start_time is None


@pytest.mark.asyncio
async def test_seeded_rows_run_the_registered_celery_task(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert both rows and the kick name the Celery task that actually exists."""
    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    assert execute_task_by_name.name == EXECUTE_TASK_BY_NAME_TASK
    assert primary.task == EXECUTE_TASK_BY_NAME_TASK
    assert follower.task == EXECUTE_TASK_BY_NAME_TASK


@pytest.mark.asyncio
async def test_the_seeded_meta_binds_to_the_scheduled_callable(
    with_system_facts_schedule, beat_maker
) -> None:
    """Assert every seeded meta key is a parameter of the callable it is forwarded to.

    The CeleryExecutor hands the meta to ``run_scheduled_inventory_sync`` as
    keyword arguments, so a key that names no parameter fails every run with
    ``TypeError`` in the worker, which neither side's unit tests would see.
    """
    await seed_module.seed_system_periodic_tasks()

    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    signature = inspect.signature(run_scheduled_inventory_sync)
    signature.bind(**_meta(primary))
    signature.bind(**_meta(follower))


@pytest.mark.asyncio
async def test_the_leader_kick_is_the_seeded_follower_request_as_a_first_run(
    with_system_facts_schedule, beat_maker, tasks_maker, mocker, mock_remote_api
) -> None:
    """Assert a kicked first run is the follower row's request plus the first-run flag.

    The row's meta carries the ordering the run must honour, and the flag makes
    the started run skip itself if the follower has run by the time it executes.
    Every key is forwarded to the callable as a keyword argument, so each must
    bind to it.
    """
    await seed_module.seed_system_periodic_tasks()
    (primary,) = await _seeded_rows(beat_maker)
    (follower,) = await _rows_named(beat_maker, with_system_facts_schedule)
    mocker.patch(
        "app.extensions.apps.inventory.sync.get_async_session_maker",
        return_value=tasks_maker,
    )
    async with tasks_maker() as session:
        run = await SyncInstanceManager.create(
            session, SyncInstanceWrite(syncer=PMM_SYNCER, status=SyncStatusEnum.SUCCESS)
        )
        await SyncItemManager.create(
            session,
            SyncItemWrite(
                entity_type=SyncInventoryEntityTypeEnum.INVENTORY,
                entity_id=None,
                sync_instance_id=run.id,
                status=SyncStatusEnum.SUCCESS,
            ),
        )
    send_task = mocker.patch.object(celery, "send_task")

    await start_follower_first_runs(
        PMM_SYNCER,
        _meta(primary)[INVENTORY_SYNC_FOLLOWERS_KEY],
        [SystemFactsSyncer(inventory_api=mock_remote_api, tasks_api=mock_remote_api)],
    )

    (kick,) = send_task.call_args_list
    assert kick.args == (follower.task,)
    assert (
        kick.kwargs["kwargs"]["task_name"] == json.loads(follower.kwargs)["task_name"]
    )
    kicked_meta = kick.kwargs["kwargs"]["execution_data"]["meta"]
    assert kicked_meta == {**_meta(follower), INVENTORY_SYNC_FIRST_RUN_KEY: True}
    inspect.signature(run_scheduled_inventory_sync).bind(**kicked_meta)


@pytest.mark.asyncio
async def test_two_syncers_sharing_a_class_name_both_schedule(
    configured, beat_maker, mocker
) -> None:
    """Assert a shared class name across modules yields two distinct rows.

    The derivation uses the full dotted path, so this legal configuration keeps
    both schedules instead of silently collapsing to one.
    """
    mocker.patch.object(
        tasks_settings,
        "INVENTORY_SYNC_SCHEDULES",
        [
            InventorySyncSchedule(syncer="a.b.Syncer", interval=ONE_DAY),
            InventorySyncSchedule(syncer="c.d.Syncer", interval=ONE_DAY),
        ],
    )

    await seed_module.seed_system_periodic_tasks()

    first = await _rows_named(
        beat_maker, seed_module._inventory_sync_schedule_name("a.b.Syncer")
    )
    second = await _rows_named(
        beat_maker, seed_module._inventory_sync_schedule_name("c.d.Syncer")
    )
    assert len(first) == 1
    assert len(second) == 1
