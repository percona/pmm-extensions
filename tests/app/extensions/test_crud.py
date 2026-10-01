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

"""Define tests for the app.extensions.crud module."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Update

from app.core.db.utils import advisory_lock_key
from app.core.exceptions import HTTPConflictException
from app.core.utils.date_time import utc_now
from app.extensions.crud import (
    AppStateManager,
    SyncEntityAbsenceManager,
    SyncInstanceManager,
    SyncItemManager,
)
from app.extensions.models import (
    AppLifecycleEnum,
    AppState,
    SyncInstance,
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItem,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.constants import SYNC_RUN_LOCK_NAMESPACE
from app.extensions.sync.exceptions import (
    SyncInstanceAlreadyInProgressError,
    SyncItemAlreadyInProgressError,
)

_NO_AGE = timedelta()


def _build_sync_item(
    *,
    entity_id: int = 1,
    entity_type: SyncInventoryEntityTypeEnum = SyncInventoryEntityTypeEnum.NODE,
    status: SyncStatusEnum = SyncStatusEnum.PENDING,
    sync_instance_id: str | None = None,
) -> SyncItem:
    """Build a SyncItem instance for testing.

    :param entity_id: The entity ID.
    :type entity_id: int
    :param entity_type: The entity type.
    :type entity_type: SyncInventoryEntityTypeEnum
    :param status: The sync status.
    :type status: SyncStatusEnum
    :param sync_instance_id: The sync instance ID.
    :type sync_instance_id: str | None
    :return: A SyncItem instance.
    :rtype: SyncItem
    """
    return SyncItem(
        id=uuid4(),
        entity_id=entity_id,
        entity_type=entity_type,
        status=status,
        sync_instance_id=sync_instance_id or uuid4(),
    )


# ---------------------------------------------------------------------------
# SyncItemManager.create
# ---------------------------------------------------------------------------


class TestSyncItemManagerCreate:
    """Test SyncItemManager.create."""

    @pytest.mark.asyncio
    async def test_duplicate_raises(self) -> None:
        """Assert SyncItemAlreadyInProgressError is raised for a duplicate item."""
        session = AsyncMock()
        instance_id = uuid4()
        existing_item = _build_sync_item(sync_instance_id=instance_id)
        item_write = SyncItemWrite(
            entity_id=1,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            sync_instance_id=instance_id,
        )

        with patch.object(
            SyncItemManager, "first", return_value=existing_item
        ) as mock_first:
            with pytest.raises(SyncItemAlreadyInProgressError):
                await SyncItemManager.create(session, item_write)

            mock_first.assert_awaited_once_with(
                session,
                entity_id=item_write.entity_id,
                entity_type=item_write.entity_type,
                sync_instance_id=item_write.sync_instance_id,
            )

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Assert a new SyncItem is created when no duplicate exists."""
        session = AsyncMock()
        instance_id = uuid4()
        item_write = SyncItemWrite(
            entity_id=2,
            entity_type=SyncInventoryEntityTypeEnum.SERVICE,
            sync_instance_id=instance_id,
        )
        expected_item = _build_sync_item(
            entity_id=2,
            entity_type=SyncInventoryEntityTypeEnum.SERVICE,
            sync_instance_id=instance_id,
        )

        with (
            patch.object(SyncItemManager, "first", return_value=None),
            patch(
                "app.extensions.crud.BaseSQLModelManager.create",
                return_value=expected_item,
            ) as mock_super_create,
        ):
            result = await SyncItemManager.create(session, item_write)

            assert result == expected_item
            mock_super_create.assert_awaited_once_with(session, item_write)


# ---------------------------------------------------------------------------
# SyncItemManager.get_or_create (overridden-create() guard, real session)
# ---------------------------------------------------------------------------


class TestSyncItemManagerGetOrCreate:
    """Test that get_or_create respects SyncItemManager's overridden create()."""

    @pytest.mark.asyncio
    async def test_respects_overridden_create_guard(self, session) -> None:
        """Assert the in-progress guard fires and no duplicate row is written.

        ``get_or_create``'s default existence filter includes ``status`` (PENDING),
        so it misses an already-``RUNNING`` item and falls into the create branch.
        That branch must route through ``SyncItemManager.create`` (not the
        conflict-tolerant fast path), whose guard raises
        ``SyncItemAlreadyInProgressError`` instead of silently inserting a second row.
        """
        instance = SyncInstance(syncer="test-syncer")
        session.add(instance)
        await session.commit()
        await session.refresh(instance)

        running_item = SyncItem(
            entity_id=1,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            status=SyncStatusEnum.RUNNING,
            sync_instance_id=instance.id,
        )
        session.add(running_item)
        await session.commit()

        item_write = SyncItemWrite(
            entity_id=1,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            sync_instance_id=instance.id,
        )

        with pytest.raises(SyncItemAlreadyInProgressError):
            await SyncItemManager.get_or_create(session, item_write)

        assert len(await SyncItemManager.list(session)) == 1


# ---------------------------------------------------------------------------
# SyncItemManager.sync_is_running
# ---------------------------------------------------------------------------


class TestSyncItemManagerSyncIsRunning:
    """Test SyncItemManager.sync_is_running."""

    @pytest.mark.asyncio
    async def test_returns_true_when_active(self) -> None:
        """Assert True is returned when a PENDING or RUNNING item exists."""
        session = AsyncMock()
        active_item = _build_sync_item(status=SyncStatusEnum.RUNNING)

        with patch.object(SyncItemManager, "first", return_value=active_item):
            result = await SyncItemManager.sync_is_running(
                session, SyncInventoryEntityTypeEnum.NODE, entity_id=1
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_active(self) -> None:
        """Assert False is returned when no active items exist."""
        session = AsyncMock()

        with patch.object(SyncItemManager, "first", return_value=None):
            result = await SyncItemManager.sync_is_running(
                session, SyncInventoryEntityTypeEnum.NODE, entity_id=1
            )

            assert result is False


# ---------------------------------------------------------------------------
# SyncItemManager.inventory_sync_completed (real session)
# ---------------------------------------------------------------------------

_LEADER = "leader-syncer"
_OTHER_SYNCER = "other-syncer"


async def _record_run(
    session,
    syncer: str,
    items: list[tuple[SyncInventoryEntityTypeEnum, SyncStatusEnum]],
    *,
    run_status: SyncStatusEnum = SyncStatusEnum.SUCCESS,
) -> None:
    """Persist one run of ``syncer`` carrying ``items``, as a finished sync leaves it.

    :param session: The real PMM Extensions session the rows are written through.
    :param syncer: The ``get_name()`` form stored on the run.
    :param items: The ``(entity_type, status)`` of each item the run recorded.
    :param run_status: The run-level verdict ``finalize_run`` would have written.
    """
    instance = SyncInstance(syncer=syncer, status=run_status)
    session.add(instance)
    await session.commit()
    await session.refresh(instance)
    for entity_id, (entity_type, status) in enumerate(items, start=1):
        session.add(
            SyncItem(
                entity_id=(
                    None
                    if entity_type == SyncInventoryEntityTypeEnum.INVENTORY
                    else entity_id
                ),
                entity_type=entity_type,
                status=status,
                sync_instance_id=instance.id,
            )
        )
    await session.commit()


class TestSyncItemManagerInventorySyncCompleted:
    """Test SyncItemManager.inventory_sync_completed against persisted runs."""

    @pytest.mark.asyncio
    async def test_true_once_the_inventory_item_succeeded(self, session) -> None:
        """Report a completed pass when the syncer's INVENTORY item reached SUCCESS."""
        await _record_run(
            session,
            _LEADER,
            [
                (SyncInventoryEntityTypeEnum.INVENTORY, SyncStatusEnum.SUCCESS),
                (SyncInventoryEntityTypeEnum.NODE, SyncStatusEnum.SUCCESS),
            ],
        )

        assert await SyncItemManager.inventory_sync_completed(session, _LEADER) is True

    @pytest.mark.asyncio
    async def test_true_when_only_an_entity_failed(self, session) -> None:
        """Count a pass whose run is FAILED only because one node failed.

        ``finalize_run`` marks the run ``FAILED`` for any failed item, so gating on
        the run-level status would defer the follower forever on an install where
        one PMM entity always fails.
        """
        await _record_run(
            session,
            _LEADER,
            [
                (SyncInventoryEntityTypeEnum.INVENTORY, SyncStatusEnum.SUCCESS),
                (SyncInventoryEntityTypeEnum.NODE, SyncStatusEnum.FAILED),
            ],
            run_status=SyncStatusEnum.FAILED,
        )

        assert await SyncItemManager.inventory_sync_completed(session, _LEADER) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("recorded_syncer", "items"),
        [
            (_LEADER, []),
            (
                _LEADER,
                [(SyncInventoryEntityTypeEnum.NODE, SyncStatusEnum.SUCCESS)],
            ),
            (
                _LEADER,
                [(SyncInventoryEntityTypeEnum.INVENTORY, SyncStatusEnum.FAILED)],
            ),
            (
                _LEADER,
                [(SyncInventoryEntityTypeEnum.INVENTORY, SyncStatusEnum.RUNNING)],
            ),
            (
                _OTHER_SYNCER,
                [(SyncInventoryEntityTypeEnum.INVENTORY, SyncStatusEnum.SUCCESS)],
            ),
        ],
        ids=[
            "run-without-items",
            "only-a-node-succeeded",
            "inventory-item-failed",
            "inventory-item-still-running",
            "another-syncer-completed",
        ],
    )
    async def test_false_without_a_successful_inventory_item_for_the_syncer(
        self,
        session,
        recorded_syncer: str,
        items: list[tuple[SyncInventoryEntityTypeEnum, SyncStatusEnum]],
    ) -> None:
        """Report no completed pass unless this syncer's INVENTORY item succeeded."""
        await _record_run(session, recorded_syncer, items)

        assert await SyncItemManager.inventory_sync_completed(session, _LEADER) is False


# ---------------------------------------------------------------------------
# SyncItemManager.finished_entity_attempts (real session)
# ---------------------------------------------------------------------------


async def _record_attempt(
    session,
    syncer: str,
    entity_id: int,
    status: SyncStatusEnum,
    *,
    entity_type: SyncInventoryEntityTypeEnum = SyncInventoryEntityTypeEnum.NODE,
    age: timedelta = _NO_AGE,
) -> SyncItem:
    """Persist one run of ``syncer`` holding a single item on ``entity_id``.

    :param session: The real PMM Extensions session the rows are written through.
    :param syncer: The ``get_name()`` form stored on the run.
    :param entity_id: The entity the item attempted.
    :param status: The item's status.
    :param entity_type: The entity level of the item.
    :param age: How long before now the item was created.
    :return: The persisted item.
    """
    instance = SyncInstance(syncer=syncer, status=SyncStatusEnum.SUCCESS)
    session.add(instance)
    await session.commit()
    await session.refresh(instance)
    item = SyncItem(
        entity_id=entity_id,
        entity_type=entity_type,
        status=status,
        sync_instance_id=instance.id,
        created_at=utc_now() - age,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


class TestSyncItemManagerFinishedEntityAttempts:
    """Test SyncItemManager.finished_entity_attempts against persisted runs."""

    @pytest.mark.asyncio
    async def test_returns_finished_attempts_oldest_first(self, session) -> None:
        """Return the syncer's SUCCESS and FAILED items on the entities, by age."""
        newer = await _record_attempt(session, _LEADER, 1, SyncStatusEnum.SUCCESS)
        older = await _record_attempt(
            session, _LEADER, 1, SyncStatusEnum.FAILED, age=timedelta(hours=1)
        )

        attempts = await SyncItemManager.finished_entity_attempts(
            session, _LEADER, SyncInventoryEntityTypeEnum.NODE, [1]
        )

        assert [item.id for item in attempts] == [older.id, newer.id]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("syncer", "entity_id", "status", "entity_type"),
        [
            (
                _OTHER_SYNCER,
                1,
                SyncStatusEnum.FAILED,
                SyncInventoryEntityTypeEnum.NODE,
            ),
            (_LEADER, 2, SyncStatusEnum.FAILED, SyncInventoryEntityTypeEnum.NODE),
            (_LEADER, 1, SyncStatusEnum.PENDING, SyncInventoryEntityTypeEnum.NODE),
            (_LEADER, 1, SyncStatusEnum.RUNNING, SyncInventoryEntityTypeEnum.NODE),
            (
                _LEADER,
                1,
                SyncStatusEnum.FAILED,
                SyncInventoryEntityTypeEnum.SERVICE,
            ),
        ],
        ids=[
            "another-syncer",
            "another-entity",
            "still-pending",
            "still-running",
            "another-entity-level",
        ],
    )
    async def test_excludes_items_outside_the_filter(
        self,
        session,
        syncer: str,
        entity_id: int,
        status: SyncStatusEnum,
        entity_type: SyncInventoryEntityTypeEnum,
    ) -> None:
        """Leave out items of another syncer, entity or level, and unfinished ones."""
        await _record_attempt(
            session, syncer, entity_id, status, entity_type=entity_type
        )

        attempts = await SyncItemManager.finished_entity_attempts(
            session, _LEADER, SyncInventoryEntityTypeEnum.NODE, [1]
        )

        assert attempts == []

    @pytest.mark.asyncio
    async def test_no_entities_issues_no_query(self, session, mocker) -> None:
        """Answer an empty entity set without reaching the database."""
        exec_ = mocker.spy(session, "exec")

        attempts = await SyncItemManager.finished_entity_attempts(
            session, _LEADER, SyncInventoryEntityTypeEnum.NODE, []
        )

        assert attempts == []
        exec_.assert_not_called()


# ---------------------------------------------------------------------------
# SyncItemManager.start_sync
# ---------------------------------------------------------------------------


class TestSyncItemManagerStartSync:
    """Test SyncItemManager.start_sync."""

    @pytest.mark.asyncio
    async def test_sets_status_to_running(self) -> None:
        """Assert the item's status is set to RUNNING and saved."""
        session = AsyncMock()
        item = _build_sync_item(status=SyncStatusEnum.PENDING)

        with patch.object(SyncItemManager, "save", return_value=item) as mock_save:
            result = await SyncItemManager.start_sync(session, item)

            assert item.status == SyncStatusEnum.RUNNING
            mock_save.assert_awaited_once_with(session, item)
            assert result == item


# ---------------------------------------------------------------------------
# SyncItemManager.finish_sync
# ---------------------------------------------------------------------------


class TestSyncItemManagerFinishSync:
    """Test SyncItemManager.finish_sync."""

    @pytest.mark.asyncio
    async def test_sets_given_status(self) -> None:
        """Assert the item's status is set to the given value and saved."""
        session = AsyncMock()
        item = _build_sync_item(status=SyncStatusEnum.RUNNING)

        with patch.object(SyncItemManager, "save", return_value=item) as mock_save:
            result = await SyncItemManager.finish_sync(
                session, item, SyncStatusEnum.SUCCESS
            )

            assert item.status == SyncStatusEnum.SUCCESS
            mock_save.assert_awaited_once_with(session, item)
            assert result == item

    @pytest.mark.asyncio
    async def test_defaults_to_success(self) -> None:
        """Assert the item's status defaults to SUCCESS when no status given."""
        session = AsyncMock()
        item = _build_sync_item(status=SyncStatusEnum.RUNNING)

        with patch.object(SyncItemManager, "save", return_value=item):
            await SyncItemManager.finish_sync(session, item)

            assert item.status == SyncStatusEnum.SUCCESS


# ---------------------------------------------------------------------------
# SyncItemManager.fail_sync
# ---------------------------------------------------------------------------


class TestSyncItemManagerFailSync:
    """Test SyncItemManager.fail_sync."""

    @pytest.mark.asyncio
    async def test_delegates_to_finish_sync_with_failed(self) -> None:
        """Assert fail_sync delegates to finish_sync with FAILED status."""
        session = AsyncMock()
        item = _build_sync_item(status=SyncStatusEnum.RUNNING)
        expected_item = _build_sync_item(status=SyncStatusEnum.FAILED)

        with patch.object(
            SyncItemManager, "finish_sync", return_value=expected_item
        ) as mock_finish:
            result = await SyncItemManager.fail_sync(session, item)

            mock_finish.assert_awaited_once_with(session, item, SyncStatusEnum.FAILED)
            assert result == expected_item


# ---------------------------------------------------------------------------
# SyncInstanceManager.create
# ---------------------------------------------------------------------------


class TestSyncInstanceManagerCreate:
    """Test SyncInstanceManager.create."""

    @pytest.mark.asyncio
    async def test_duplicate_raises(self, session) -> None:
        """Refuse a new run while an item of an earlier one is still in progress.

        Driven without ``stale_after`` so the item-level conflict is the only thing
        that can produce the refusal.
        """
        await _seed_run(session, item_status=SyncStatusEnum.RUNNING)

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            )

    @pytest.mark.asyncio
    async def test_success(self, session) -> None:
        """Persist a new run for a syncer holding no in-progress work."""
        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
        )

        persisted = await SyncInstanceManager.first(session, id=created.id)
        assert persisted.status == SyncStatusEnum.RUNNING


# ---------------------------------------------------------------------------
# SyncInstanceManager.finish_hanging_items
# ---------------------------------------------------------------------------


class TestSyncInstanceManagerFinishHangingItems:
    """Test SyncInstanceManager.finish_hanging_items."""

    @pytest.mark.asyncio
    async def test_marks_active_items_as_failed(self) -> None:
        """Assert all PENDING/RUNNING items are marked FAILED."""
        session = AsyncMock()
        pending_item = _build_sync_item(status=SyncStatusEnum.PENDING)
        running_item = _build_sync_item(status=SyncStatusEnum.RUNNING)
        hanging_items = [pending_item, running_item]

        with (
            patch.object(
                SyncItemManager, "list", return_value=hanging_items
            ) as mock_list,
            patch.object(SyncItemManager, "save_batch") as mock_save_batch,
        ):
            result = await SyncInstanceManager.finish_hanging_items(session, 42)

            mock_list.assert_awaited_once()
            assert pending_item.status == SyncStatusEnum.FAILED
            assert running_item.status == SyncStatusEnum.FAILED
            mock_save_batch.assert_awaited_once_with(
                session, pending_item, running_item
            )
            assert result == hanging_items

    @pytest.mark.asyncio
    async def test_no_hanging_items(self) -> None:
        """Assert empty list is returned when no hanging items exist."""
        session = AsyncMock()

        with (
            patch.object(SyncItemManager, "list", return_value=[]),
            patch.object(SyncItemManager, "save_batch") as mock_save_batch,
        ):
            result = await SyncInstanceManager.finish_hanging_items(session, 99)

            mock_save_batch.assert_awaited_once_with(session)
            assert result == []


class TestAppStateManager:
    """Test suite for AppStateManager against a real session."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", list(AppLifecycleEnum))
    async def test_is_enabled_only_for_enabled_state(self, session, state) -> None:
        """``is_enabled`` is ``True`` only when the row's state is ``ENABLED``."""
        session.add(AppState(app_key="snippets", lifecycle_state=state))
        await session.commit()
        expected = state == AppLifecycleEnum.ENABLED
        assert await AppStateManager.is_enabled(session, "snippets") is expected

    @pytest.mark.asyncio
    async def test_is_enabled_true_for_missing_row(self, session) -> None:
        """A missing row is treated as enabled (active until explicitly disabled)."""
        assert await AppStateManager.is_enabled(session, "snippets") is True

    @pytest.mark.asyncio
    async def test_all_lifecycle_states_returns_mapping(self, session) -> None:
        """``all_lifecycle_states`` returns the ``app_key`` -> state mapping."""
        session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        session.add(
            AppState(app_key="checksums", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        await session.commit()
        assert await AppStateManager.all_lifecycle_states(session) == {
            "snippets": AppLifecycleEnum.ENABLED,
            "checksums": AppLifecycleEnum.DISABLING,
        }

    @pytest.mark.asyncio
    async def test_all_lifecycle_states_empty_table(self, session) -> None:
        """``all_lifecycle_states`` returns an empty mapping when no rows exist."""
        assert await AppStateManager.all_lifecycle_states(session) == {}

    @pytest.mark.asyncio
    async def test_all_lifecycle_states_returns_full_mapping(self, session) -> None:
        """``all_lifecycle_states`` returns the ``app_key`` -> state mapping."""
        session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.ENABLED)
        )
        session.add(
            AppState(app_key="checksums", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        await session.commit()
        assert await AppStateManager.all_lifecycle_states(session) == {
            "snippets": AppLifecycleEnum.ENABLED,
            "checksums": AppLifecycleEnum.DISABLING,
        }

    @pytest.mark.asyncio
    async def test_current_lifecycle_reads_row_state(self, session) -> None:
        """``current_lifecycle`` returns the persisted state for an existing row."""
        session.add(
            AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLING)
        )
        await session.commit()
        current = await AppStateManager.current_lifecycle(session, "snippets")
        assert current is AppLifecycleEnum.DISABLING

    @pytest.mark.asyncio
    async def test_current_lifecycle_missing_row_is_enabled(self, session) -> None:
        """A missing row reports ``ENABLED`` for the transition gate."""
        current = await AppStateManager.current_lifecycle(session, "snippets")
        assert current is AppLifecycleEnum.ENABLED

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (AppLifecycleEnum.ENABLED, AppLifecycleEnum.DISABLING),
            (AppLifecycleEnum.DISABLED, AppLifecycleEnum.ENABLING),
            (AppLifecycleEnum.DISABLING, AppLifecycleEnum.DISABLED),
            (AppLifecycleEnum.ENABLING, AppLifecycleEnum.ENABLED),
        ],
    )
    def test_assert_transition_allowed_accepts_valid_edges(
        self, current, target
    ) -> None:
        """Each reachable edge passes the transition gate without raising."""
        AppStateManager.assert_transition_allowed(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (AppLifecycleEnum.ENABLED, AppLifecycleEnum.ENABLED),
            (AppLifecycleEnum.ENABLED, AppLifecycleEnum.DISABLED),
            (AppLifecycleEnum.DISABLED, AppLifecycleEnum.ENABLED),
            (AppLifecycleEnum.DISABLING, AppLifecycleEnum.ENABLED),
            (AppLifecycleEnum.DISABLING, AppLifecycleEnum.DISABLING),
            (AppLifecycleEnum.ENABLING, AppLifecycleEnum.DISABLED),
        ],
    )
    def test_assert_transition_allowed_rejects_illegal_edges(
        self, current, target
    ) -> None:
        """Every illegal edge raises ``HTTPConflictException`` (HTTP 409)."""
        with pytest.raises(HTTPConflictException):
            AppStateManager.assert_transition_allowed(current, target)


# ---------------------------------------------------------------------------
# SyncInstanceManager stale-run reclaim
# ---------------------------------------------------------------------------


async def _seed_item(
    session,
    instance: SyncInstance,
    *,
    status: SyncStatusEnum = SyncStatusEnum.RUNNING,
    age: timedelta = _NO_AGE,
    entity_id: int = 1,
) -> SyncItem:
    """Persist one item on ``instance``, aged ``age`` into the past.

    :param session: The database session to persist through.
    :param instance: The run the item belongs to.
    :param status: The item's synchronization status.
    :param age: How far into the past both of the item's timestamps are set.
    :param entity_id: The inventory entity the item covers, unique per instance.
    :return: The persisted item.
    """
    return await SyncItemManager.save(
        session,
        SyncItem(
            entity_id=entity_id,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            status=status,
            sync_instance_id=instance.id,
            created_at=utc_now() - age,
            updated_at=utc_now() - age,
        ),
    )


async def _seed_run(
    session,
    *,
    syncer: str = "test-syncer",
    item_status: SyncStatusEnum = SyncStatusEnum.RUNNING,
    age: timedelta = _NO_AGE,
    instance_status: SyncStatusEnum = SyncStatusEnum.RUNNING,
) -> SyncInstance:
    """Persist a SyncInstance carrying one item aged ``age`` into the past."""
    instance = await SyncInstanceManager.save(
        session,
        SyncInstance(syncer=syncer, status=instance_status),
    )
    await _seed_item(session, instance, status=item_status, age=age)
    return instance


@contextmanager
def _committing_before_the_fence(
    session,
    mutate: Callable[[], Awaitable[None]],
) -> Iterator[None]:
    """Run ``mutate`` against ``session`` just before the reclaim's fencing UPDATE.

    The candidate query and the fence commit separately, so the state the fence
    re-asserts against can move in between. Wrapping the session's own ``exec``
    rather than a manager keeps every statement the reclaim issues, and the rows
    they match, entirely real.

    :param session: The database session the reclaim runs on.
    :param mutate: The concurrent write to commit before the fence executes.
    """
    original = session.exec
    mutated = False

    async def _exec_around_fence(statement, *args, **kwargs):
        nonlocal mutated
        if not mutated and isinstance(statement, Update):
            mutated = True
            await mutate()
        return await original(statement, *args, **kwargs)

    with patch.object(session, "exec", new=_exec_around_fence):
        yield


class TestSyncInstanceManagerStaleReclaim:
    """Test the age-based stale-run reclaim on ``SyncInstanceManager.create``."""

    @pytest.mark.asyncio
    async def test_create_refuses_live_concurrent_run(self, session) -> None:
        """Refuse a run whose items are newer than ``stale_after``."""
        await _seed_run(session, age=timedelta(minutes=1))

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer"),
                stale_after=timedelta(hours=1),
            )

    @pytest.mark.asyncio
    async def test_create_does_not_reclaim_paused_live_run(self, session) -> None:
        """Refuse a run blocked mid-fetch whose items are untouched but recent."""
        await _seed_run(
            session, item_status=SyncStatusEnum.PENDING, age=timedelta(minutes=30)
        )

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer"),
                stale_after=timedelta(hours=1),
            )

    @pytest.mark.asyncio
    async def test_create_reclaims_stale_run_and_proceeds(self, session) -> None:
        """Mark a run failed when its newest activity predates ``stale_after``."""
        stale = await _seed_run(session, age=timedelta(hours=3))

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer"),
            stale_after=timedelta(hours=1),
        )

        assert created.id != stale.id
        items = await SyncItemManager.list(session, sync_instance_id=stale.id)
        assert [item.status for item in items] == [SyncStatusEnum.FAILED]
        reclaimed = await SyncInstanceManager.first(session, id=stale.id)
        assert reclaimed.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_reclaim_leaves_snapshot_complete_null(self, session) -> None:
        """Leave ``snapshot_complete`` unset so a reclaimed apply is incomplete."""
        stale = await _seed_run(session, age=timedelta(hours=3))

        await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        reclaimed = await SyncInstanceManager.first(session, id=stale.id)
        assert reclaimed.snapshot_complete is None

    @pytest.mark.asyncio
    async def test_create_without_stale_after_never_reclaims(self, session) -> None:
        """Preserve the existing refusal behaviour when ``stale_after`` is unset."""
        await _seed_run(session, age=timedelta(days=7))

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session, SyncInstanceWrite(syncer="test-syncer")
            )

    @pytest.mark.asyncio
    async def test_reclaim_is_idempotent_under_repeat(self, session) -> None:
        """Match no rows on a second reclaimer's conditional update."""
        stale = await _seed_run(session, age=timedelta(hours=3))

        first_pass = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )
        second_pass = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert first_pass == [stale.id]
        assert second_pass == []

    @pytest.mark.asyncio
    async def test_reclaim_fences_the_instance_before_releasing_items(
        self, session
    ) -> None:
        """Mark the run failed before the item flip commits.

        Each statement commits on its own, so releasing the items first would let
        a reclaimed-but-live worker still read ``RUNNING`` and retire entities.
        """
        instance = await _seed_run(session, age=timedelta(hours=3))
        owned_during_item_flip: list[bool] = []

        original = SyncItemManager.update_where

        async def _record_then_update(*args, **kwargs):
            owned_during_item_flip.append(
                await SyncInstanceManager.is_still_owned(session, instance.id)
            )
            return await original(*args, **kwargs)

        with patch.object(
            SyncItemManager, "update_where", side_effect=_record_then_update
        ):
            await SyncInstanceManager.reclaim_stale_runs(
                session, "test-syncer", timedelta(hours=1)
            )

        assert owned_during_item_flip == [False]

    @pytest.mark.asyncio
    async def test_reclaim_resumes_an_interrupted_release(self, session) -> None:
        """Release items of a stale run already fenced by an interrupted reclaim.

        The fence and the item release commit separately, so a crash between them
        leaves a ``FAILED`` instance whose items are still held. The conditional
        fence matches nothing on the retry, so only covering already-fenced runs
        keeps the syncer from staying blocked forever.
        """
        stale = await _seed_run(
            session,
            age=timedelta(hours=3),
            instance_status=SyncStatusEnum.FAILED,
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        items = await SyncItemManager.list(session, sync_instance_id=stale.id)
        assert [item.status for item in items] == [SyncStatusEnum.FAILED]

    @pytest.mark.asyncio
    async def test_create_proceeds_after_an_interrupted_reclaim(self, session) -> None:
        """Let a new run start once an interrupted reclaim has been resumed."""
        await _seed_run(
            session,
            age=timedelta(hours=3),
            instance_status=SyncStatusEnum.FAILED,
        )

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer"),
            stale_after=timedelta(hours=1),
        )

        assert created.status == SyncStatusEnum.PENDING

    @pytest.mark.asyncio
    async def test_reclaim_ignores_other_syncers(self, session) -> None:
        """Leave a stale run belonging to another syncer untouched."""
        other = await _seed_run(session, syncer="other-syncer", age=timedelta(hours=3))

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=other.id)
        assert untouched.status == SyncStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_reclaim_fails_an_idle_run_with_no_active_items(
        self, session
    ) -> None:
        """Fail a run left behind with every item already terminal.

        A worker killed after its last item write and before the run verdict leaves
        no active item, so the item-conflict path never sees the run at all.
        """
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == [stale.id]
        fenced = await SyncInstanceManager.first(session, id=stale.id)
        assert fenced.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_idle_reclaim_leaves_snapshot_complete_null(self, session) -> None:
        """Leave ``snapshot_complete`` unset on an idle run reclaimed as abandoned."""
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        reclaimed = await SyncInstanceManager.first(session, id=stale.id)
        assert reclaimed.status == SyncStatusEnum.FAILED
        assert reclaimed.snapshot_complete is None

    @pytest.mark.asyncio
    async def test_create_reclaims_an_idle_run_without_an_item_conflict(
        self, session
    ) -> None:
        """Reconcile an idle abandoned run on a path no item conflict triggers."""
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer"),
            stale_after=timedelta(hours=1),
        )

        assert created.id != stale.id
        fenced = await SyncInstanceManager.first(session, id=stale.id)
        assert fenced.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_reclaim_leaves_a_recent_idle_run_running(self, session) -> None:
        """Leave an idle run whose last item write is newer than ``stale_after``."""
        live = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(minutes=1),
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=live.id)
        assert untouched.status == SyncStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_reclaim_measures_idle_time_from_the_newest_item(
        self, session
    ) -> None:
        """Keep a run whose oldest item is stale but whose newest one is not."""
        live = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )
        await _seed_item(
            session,
            live,
            status=SyncStatusEnum.SUCCESS,
            age=timedelta(minutes=1),
            entity_id=2,
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=live.id)
        assert untouched.status == SyncStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_reclaim_ignores_an_idle_run_without_items(self, session) -> None:
        """Leave a run that never wrote an item alone, for want of a heartbeat.

        Its only timestamps live on the instance row, which no item write bumps, so
        reconciling it needs a staleness source this path does not have.
        """
        itemless = await SyncInstanceManager.save(
            session,
            SyncInstance(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=itemless.id)
        assert untouched.status == SyncStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_reclaim_ignores_an_idle_run_of_another_syncer(self, session) -> None:
        """Leave another syncer's idle stale run untouched."""
        other = await _seed_run(
            session,
            syncer="other-syncer",
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=other.id)
        assert untouched.status == SyncStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_reclaim_leaves_a_finished_run_alone(self, session) -> None:
        """Keep the verdict a run wrote for itself before going idle."""
        finished = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
            instance_status=SyncStatusEnum.SUCCESS,
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=finished.id)
        assert untouched.status == SyncStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_idle_reclaim_is_idempotent_under_repeat(self, session) -> None:
        """Reclaim an idle run once, then match nothing on a second attempt."""
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        first = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )
        second = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert first == [stale.id]
        assert second == []
        fenced = await SyncInstanceManager.first(session, id=stale.id)
        assert fenced.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_reclaim_leaves_a_run_that_finished_mid_reclaim(
        self, session
    ) -> None:
        """Keep the verdict of a run that finished after it was picked as stale.

        The candidate query and the fence commit separately, so a run that writes
        its own verdict in between must keep it rather than be failed retroactively.
        """
        stale = await _seed_run(session, age=timedelta(hours=3))

        async def _finish_the_run() -> None:
            finishing = await SyncInstanceManager.first(session, id=stale.id)
            finishing.status = SyncStatusEnum.SUCCESS
            await SyncInstanceManager.save(session, finishing)

        with _committing_before_the_fence(session, _finish_the_run):
            reclaimed = await SyncInstanceManager.reclaim_stale_runs(
                session, "test-syncer", timedelta(hours=1)
            )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=stale.id)
        assert untouched.status == SyncStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_reclaim_spares_a_run_whose_worker_touched_an_item(
        self, session
    ) -> None:
        """Keep a run whose worker wrote to an item after it was picked as stale.

        A resumed worker proves the run is progressing, so the fence must re-read
        item activity rather than trust the candidate query's snapshot.
        """
        stale = await _seed_run(session, age=timedelta(hours=3))

        async def _touch_the_item() -> None:
            item = await SyncItemManager.first(session, sync_instance_id=stale.id)
            item.updated_at = utc_now()
            await SyncItemManager.save(session, item)

        with _committing_before_the_fence(session, _touch_the_item):
            reclaimed = await SyncInstanceManager.reclaim_stale_runs(
                session, "test-syncer", timedelta(hours=1)
            )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=stale.id)
        assert untouched.status == SyncStatusEnum.RUNNING
        items = await SyncItemManager.list(session, sync_instance_id=stale.id)
        assert [item.status for item in items] == [SyncStatusEnum.RUNNING]

    @pytest.mark.asyncio
    async def test_reclaim_spares_an_idle_run_that_started_a_new_item(
        self, session
    ) -> None:
        """Keep an idle run whose worker opened a new item before the fence.

        The new item makes the run active again, and failing it here would strand
        that item under a ``FAILED`` run the worker still believes it owns.
        """
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        async def _start_the_next_item() -> None:
            await _seed_item(session, stale, status=SyncStatusEnum.PENDING, entity_id=2)

        with _committing_before_the_fence(session, _start_the_next_item):
            reclaimed = await SyncInstanceManager.reclaim_stale_runs(
                session, "test-syncer", timedelta(hours=1)
            )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=stale.id)
        assert untouched.status == SyncStatusEnum.RUNNING
        items = await SyncItemManager.list(session, sync_instance_id=stale.id)
        assert sorted(item.status for item in items) == sorted(
            [SyncStatusEnum.SUCCESS, SyncStatusEnum.PENDING]
        )

    @pytest.mark.asyncio
    async def test_create_refuses_a_run_that_resumed_during_the_reclaim(
        self, session
    ) -> None:
        """Refuse to start when a run the reclaim spared opened an item meanwhile.

        The conflict read that precedes the reclaim cannot see an item the resumed
        worker opens during it, so trusting that read would start a second run
        alongside the one the fence just decided to leave alone.
        """
        stale = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        async def _start_the_next_item() -> None:
            await _seed_item(session, stale, status=SyncStatusEnum.PENDING, entity_id=2)

        with (
            _committing_before_the_fence(session, _start_the_next_item),
            pytest.raises(SyncInstanceAlreadyInProgressError),
        ):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer"),
                stale_after=timedelta(hours=1),
            )

        assert [run.id for run in await SyncInstanceManager.list(session)] == [stale.id]

    @pytest.mark.asyncio
    async def test_reclaim_ignores_an_idle_run_still_pending(self, session) -> None:
        """Leave an idle stale run that never reached ``RUNNING`` untouched.

        Only a run that started and stopped reporting is presumed abandoned here;
        a run still ``PENDING`` was never claimed by a worker.
        """
        pending = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
            instance_status=SyncStatusEnum.PENDING,
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert reclaimed == []
        untouched = await SyncInstanceManager.first(session, id=pending.id)
        assert untouched.status == SyncStatusEnum.PENDING

    @pytest.mark.asyncio
    async def test_reclaim_returns_both_stale_classes_once(self, session) -> None:
        """Reclaim an idle run and an item-blocked one together, without repeats."""
        blocked = await _seed_run(session, age=timedelta(hours=3))
        idle = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            age=timedelta(hours=3),
        )

        reclaimed = await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert sorted(reclaimed) == sorted([blocked.id, idle.id])


# ---------------------------------------------------------------------------
# SyncInstanceManager.finalize_run / is_still_owned
# ---------------------------------------------------------------------------


class TestSyncInstanceManagerFinalizeRun:
    """Test the run-level status rollup written at ``__aexit__``."""

    @pytest.mark.asyncio
    async def test_finalize_run_success(self, session) -> None:
        """Roll an all-success run up to ``SUCCESS`` and record completeness."""
        instance = await _seed_run(session, item_status=SyncStatusEnum.SUCCESS)

        await SyncInstanceManager.finalize_run(
            session, instance.id, failed=False, snapshot_complete=True
        )

        finalized = await SyncInstanceManager.first(session, id=instance.id)
        assert finalized.status == SyncStatusEnum.SUCCESS
        assert finalized.snapshot_complete is True

    @pytest.mark.asyncio
    async def test_finalize_run_failed_item(self, session) -> None:
        """Roll the run up to ``FAILED`` when any item failed."""
        instance = await _seed_run(session, item_status=SyncStatusEnum.FAILED)

        await SyncInstanceManager.finalize_run(
            session, instance.id, failed=False, snapshot_complete=True
        )

        finalized = await SyncInstanceManager.first(session, id=instance.id)
        assert finalized.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_finalize_run_propagates_raised_exception(self, session) -> None:
        """``failed=True`` (an exception left ``__aexit__``) rolls up to ``FAILED``."""
        instance = await _seed_run(session, item_status=SyncStatusEnum.SUCCESS)

        await SyncInstanceManager.finalize_run(
            session, instance.id, failed=True, snapshot_complete=False
        )

        finalized = await SyncInstanceManager.first(session, id=instance.id)
        assert finalized.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_finalize_run_leaves_reclaimed_instance_alone(self, session) -> None:
        """Keep the ``FAILED`` verdict a falsely-reclaimed worker would overwrite."""
        instance = await _seed_run(
            session,
            item_status=SyncStatusEnum.SUCCESS,
            instance_status=SyncStatusEnum.FAILED,
        )

        await SyncInstanceManager.finalize_run(
            session, instance.id, failed=False, snapshot_complete=True
        )

        finalized = await SyncInstanceManager.first(session, id=instance.id)
        assert finalized.status == SyncStatusEnum.FAILED
        assert finalized.snapshot_complete is None

    @pytest.mark.asyncio
    async def test_is_still_owned_tracks_the_persisted_status(self, session) -> None:
        """Read the persisted row, not the caller's in-memory copy."""
        instance = await _seed_run(session, age=timedelta(hours=3))

        assert await SyncInstanceManager.is_still_owned(session, instance.id) is True

        await SyncInstanceManager.reclaim_stale_runs(
            session, "test-syncer", timedelta(hours=1)
        )

        assert await SyncInstanceManager.is_still_owned(session, instance.id) is False


# ---------------------------------------------------------------------------
# SyncEntityAbsenceManager
# ---------------------------------------------------------------------------


class TestSyncEntityAbsenceManager:
    """Test the missing-grace ledger."""

    @pytest.mark.asyncio
    async def test_record_missing_increments_and_clear_deletes(self, session) -> None:
        """Accumulate consecutive absences and drop the row on reappearance."""
        first = await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )
        second = await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )

        assert (first, second) == (1, 2)

        await SyncEntityAbsenceManager.clear(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )

        assert await SyncEntityAbsenceManager.list(session) == []

    @pytest.mark.asyncio
    async def test_clear_without_ids_is_a_noop(self, session) -> None:
        """Delete nothing when the id tuple is empty."""
        await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )

        await SyncEntityAbsenceManager.clear(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE
        )

        assert len(await SyncEntityAbsenceManager.list(session)) == 1

    @pytest.mark.asyncio
    async def test_clear_does_not_cross_entity_type_or_syncer(self, session) -> None:
        """``entity_id`` collides across types and syncers, so the key is all three."""
        await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )
        await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.SERVICE, 7
        )
        await SyncEntityAbsenceManager.record_missing(
            session, "mysql", SyncInventoryEntityTypeEnum.NODE, 7
        )

        await SyncEntityAbsenceManager.clear(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )

        survivors = {
            (row.syncer, row.entity_type)
            for row in await SyncEntityAbsenceManager.list(session)
        }
        assert survivors == {
            ("pmm", SyncInventoryEntityTypeEnum.SERVICE),
            ("mysql", SyncInventoryEntityTypeEnum.NODE),
        }

    @pytest.mark.asyncio
    async def test_a_null_syncer_clears_the_entity_across_every_syncer(
        self, session
    ) -> None:
        """Drop every syncer's row for an entity that is going away for good.

        A row outlives the configuration that wrote it, so a caller collecting
        the entity itself cannot enumerate the syncers that observed it.
        """
        await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.NODE, 7
        )
        await SyncEntityAbsenceManager.record_missing(
            session, "mysql", SyncInventoryEntityTypeEnum.NODE, 7
        )
        await SyncEntityAbsenceManager.record_missing(
            session, "pmm", SyncInventoryEntityTypeEnum.SERVICE, 7
        )

        await SyncEntityAbsenceManager.clear(
            session, None, SyncInventoryEntityTypeEnum.NODE, 7
        )

        survivors = {
            (row.syncer, row.entity_type)
            for row in await SyncEntityAbsenceManager.list(session)
        }
        assert survivors == {("pmm", SyncInventoryEntityTypeEnum.SERVICE)}


# ---------------------------------------------------------------------------
# SyncInstanceManager.create — run exclusivity
# ---------------------------------------------------------------------------


class TestSyncInstanceManagerRunExclusivity:
    """Test that a syncer can only ever hold one run at a time."""

    @pytest.mark.asyncio
    async def test_refuses_a_run_that_has_not_written_an_item_yet(
        self, session
    ) -> None:
        """Refuse a second run while the first is still before its first item.

        A run writes no item until it has fetched its snapshot, so the item-level
        conflict check alone is blind for exactly as long as that fetch takes —
        long enough for a second worker to start and for both runs to believe they
        own the syncer.
        """
        await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
                stale_after=timedelta(hours=1),
            )

    @pytest.mark.asyncio
    async def test_ignores_an_itemless_instance_older_than_stale_after(
        self, session
    ) -> None:
        """Proceed past a run abandoned before it wrote any item.

        Such a run carries no item activity for the reclaim to measure, so it is
        left ``RUNNING`` forever; the age bound on the instance-level check is what
        keeps it from fencing the syncer for good.
        """
        abandoned = await SyncInstanceManager.save(
            session,
            SyncInstance(
                syncer="test-syncer",
                status=SyncStatusEnum.RUNNING,
                created_at=utc_now() - timedelta(hours=3),
                updated_at=utc_now() - timedelta(hours=3),
            ),
        )

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        assert created.id != abandoned.id

    @pytest.mark.asyncio
    async def test_refuses_a_run_whose_row_was_touched_since_the_bound(
        self, session
    ) -> None:
        """Measure a run's age from its last touch, not from when it started.

        The state is constructed, not reachable: no path rewrites an instance row
        and leaves it ``RUNNING``, so a live long sync bumps its items' timestamps
        and is fenced by the item check rather than by this bound. The row-level
        touch is honoured as defence for the day a run's own row is written
        mid-flight, and this test is what would keep it honoured then.
        """
        alive = await SyncInstanceManager.save(
            session,
            SyncInstance(
                syncer="test-syncer",
                status=SyncStatusEnum.RUNNING,
                created_at=utc_now() - timedelta(hours=3),
                updated_at=utc_now(),
            ),
        )

        with pytest.raises(SyncInstanceAlreadyInProgressError) as refused:
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
                stale_after=timedelta(hours=1),
            )

        assert str(alive.id) in str(refused.value)

    @pytest.mark.asyncio
    async def test_names_the_conflicting_run_in_the_refusal(self, session) -> None:
        """Say which run holds the syncer, and nothing a reader may not see.

        This message is stored verbatim on ``last_sync_error`` and served by the
        inventory read routes, so it has to identify the conflict for an operator
        while carrying sync bookkeeping only.
        """
        holder = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        with pytest.raises(SyncInstanceAlreadyInProgressError) as refused:
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
                stale_after=timedelta(hours=1),
            )

        message = str(refused.value)
        assert "test-syncer" in message
        assert str(holder.id) in message

    @pytest.mark.asyncio
    async def test_refuses_a_run_whose_instance_is_still_pending(self, session) -> None:
        """Refuse a second run while the first has not started working yet."""
        await SyncInstanceManager.save(
            session,
            SyncInstance(syncer="test-syncer", status=SyncStatusEnum.PENDING),
        )

        with pytest.raises(SyncInstanceAlreadyInProgressError):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
                stale_after=timedelta(hours=1),
            )

    @pytest.mark.asyncio
    async def test_refuses_only_the_contended_syncer(self, session) -> None:
        """Fence one syncer at a time so an unrelated syncer still starts."""
        await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="other-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        assert created.syncer == "other-syncer"

    @pytest.mark.asyncio
    async def test_allows_a_new_run_once_the_previous_one_finalized(
        self, session
    ) -> None:
        """Keep the sequential path intact for a syncer that ran to completion."""
        finished = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )
        await SyncInstanceManager.finalize_run(
            session, finished.id, failed=False, snapshot_complete=True
        )

        created = await SyncInstanceManager.create(
            session,
            SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
            stale_after=timedelta(hours=1),
        )

        assert created.id != finished.id

    @pytest.mark.asyncio
    async def test_itemless_instance_does_not_refuse_without_stale_after(
        self, session
    ) -> None:
        """Keep the item-only refusal where no reclaim policy is configured.

        Without ``stale_after`` nothing would ever clear an instance-level
        conflict, so the fence would have no bound and could refuse a syncer
        forever.
        """
        await SyncInstanceManager.save(
            session,
            SyncInstance(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
        )

        created = await SyncInstanceManager.create(
            session, SyncInstanceWrite(syncer="test-syncer")
        )

        assert created.status == SyncStatusEnum.PENDING

    @pytest.mark.asyncio
    async def test_refuses_without_writing_when_the_lock_is_contended(
        self, session
    ) -> None:
        """Refuse outright while a peer holds the creation lock.

        Waiting for the peer instead would pin a connection to learn what the
        refusal already says, and the peer is about to own the syncer either way.
        """

        @asynccontextmanager
        async def _contended(*_args, **_kwargs):
            yield False

        with (
            patch("app.extensions.crud.try_pg_advisory_xact_lock", _contended),
            pytest.raises(SyncInstanceAlreadyInProgressError),
        ):
            await SyncInstanceManager.create(
                session,
                SyncInstanceWrite(syncer="test-syncer", status=SyncStatusEnum.RUNNING),
                stale_after=timedelta(hours=1),
            )

        assert await SyncInstanceManager.list(session, syncer="test-syncer") == []

    @pytest.mark.asyncio
    async def test_locks_on_a_key_derived_from_the_syncer(self, session) -> None:
        """Key the lock on the syncer so one syncer does not fence another.

        Two names colliding on the 32-bit digest would cost the loser one scheduled
        run, which the next tick recovers.
        """
        recorded: list[tuple[int, int]] = []

        @asynccontextmanager
        async def _recording(_session, namespace, key):
            recorded.append((namespace, key))
            yield True

        with patch("app.extensions.crud.try_pg_advisory_xact_lock", _recording):
            await SyncInstanceManager.create(
                session, SyncInstanceWrite(syncer="test-syncer")
            )

        assert recorded == [(SYNC_RUN_LOCK_NAMESPACE, advisory_lock_key("test-syncer"))]


class TestSyncInstanceManagerRunExclusivityOnRealPostgres:
    """Race ``SyncInstanceManager.create`` against itself on real PostgreSQL.

    SQLite cannot substitute: its single-writer file locking serializes the two
    callers regardless of whether the fence works, so the race would pass
    vacuously there.
    """

    @staticmethod
    def _unique_syncer() -> str:
        """Return a syncer name no concurrent xdist worker shares.

        Advisory locks are database-wide while the fixtures isolate workers by
        schema, so a shared name would let one worker's lock refuse another's
        caller and the assertions below would pass for the wrong reason.
        """
        return f"race-syncer-{uuid4().hex}"

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_only_one_of_two_racing_callers_creates_a_run(
        self, postgres_session_maker
    ) -> None:
        """Let exactly one of two overlapping callers own the syncer."""
        syncer = self._unique_syncer()
        first_holds_lock = asyncio.Event()
        second_finished = asyncio.Event()
        original = SyncInstanceManager._items_in_progress

        async def _pause_the_first_caller(session, name):
            if not first_holds_lock.is_set():
                first_holds_lock.set()
                await asyncio.wait_for(second_finished.wait(), timeout=30)
            return await original(session, name)

        async def _create() -> SyncInstance:
            async with postgres_session_maker() as session:
                return await SyncInstanceManager.create(
                    session,
                    SyncInstanceWrite(syncer=syncer, status=SyncStatusEnum.RUNNING),
                    stale_after=timedelta(hours=1),
                )

        async def _create_after_the_first_holds_the_lock() -> SyncInstance:
            await asyncio.wait_for(first_holds_lock.wait(), timeout=30)
            try:
                return await _create()
            finally:
                second_finished.set()

        with patch.object(
            SyncInstanceManager, "_items_in_progress", _pause_the_first_caller
        ):
            outcomes = await asyncio.gather(
                _create(),
                _create_after_the_first_holds_the_lock(),
                return_exceptions=True,
            )

        created = [outcome for outcome in outcomes if isinstance(outcome, SyncInstance)]
        refused = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, SyncInstanceAlreadyInProgressError)
        ]
        assert len(created) == 1, outcomes
        assert len(refused) == 1, outcomes
        async with postgres_session_maker() as session:
            assert len(await SyncInstanceManager.list(session, syncer=syncer)) == 1

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_two_syncers_start_concurrently(self, postgres_session_maker) -> None:
        """Let different syncers overlap, since neither can own the other's run."""
        first_syncer, second_syncer = self._unique_syncer(), self._unique_syncer()

        async def _create(syncer: str) -> SyncInstance:
            async with postgres_session_maker() as session:
                return await SyncInstanceManager.create(
                    session,
                    SyncInstanceWrite(syncer=syncer, status=SyncStatusEnum.RUNNING),
                    stale_after=timedelta(hours=1),
                )

        created = await asyncio.gather(_create(first_syncer), _create(second_syncer))

        assert {instance.syncer for instance in created} == {
            first_syncer,
            second_syncer,
        }
