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

"""Tests for the cooperative app-drain machinery in ``app.extensions.app_drain``."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import utc_now
from app.extensions import app_drain
from app.extensions.app_drain import (
    finalize_drain_if_complete,
    record_task_end,
    record_task_start,
    should_cancel,
    track_app_task,
)
from app.extensions.crud import AppRunningTaskManager, AppStateManager
from app.extensions.models import AppLifecycleEnum, AppRunningTask, AppState
from app.extensions.snippets.celery import sync_snippets


def _patch_session_maker(mocker, session: AsyncSession) -> MagicMock:
    """Patch ``app_drain.get_async_session_maker`` to yield ``session``.

    Returns the maker mock so a test can assert how many times a session was
    opened (one ``maker()`` call per opened session).
    """
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("app.extensions.app_drain.get_async_session_maker", return_value=maker)
    return maker


async def _seed_app(
    session: AsyncSession, app_key: str, state: AppLifecycleEnum
) -> None:
    session.add(AppState(app_key=app_key, lifecycle_state=state))
    await session.commit()


class TestAppStateManagerShouldCancel:
    """The pure ``AppStateManager.should_cancel`` predicate."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state", [AppLifecycleEnum.DISABLING, AppLifecycleEnum.DISABLED]
    )
    async def test_cancels_when_disabling_or_disabled(
        self, session: AsyncSession, state: AppLifecycleEnum
    ) -> None:
        """True when the app is mid- or post-disable."""
        await _seed_app(session, "snippets", state)
        assert await AppStateManager.should_cancel(session, "snippets") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state", [AppLifecycleEnum.ENABLED, AppLifecycleEnum.ENABLING]
    )
    async def test_does_not_cancel_when_active(
        self, session: AsyncSession, state: AppLifecycleEnum
    ) -> None:
        """False when the app is active (enabled or enabling)."""
        await _seed_app(session, "snippets", state)
        assert await AppStateManager.should_cancel(session, "snippets") is False

    @pytest.mark.asyncio
    async def test_missing_row_is_not_cancelled(self, session: AsyncSession) -> None:
        """A missing row reports ENABLED, so it should not cancel."""
        assert await AppStateManager.should_cancel(session, "snippets") is False


class TestShouldCancelHelper:
    """The fail-soft module-level ``should_cancel`` helper."""

    @pytest.mark.asyncio
    async def test_true_with_passed_session(self, session: AsyncSession) -> None:
        """Returns True for a DISABLING app using the caller's session."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        assert await should_cancel("snippets", session=session) is True

    @pytest.mark.asyncio
    async def test_true_with_own_session(self, session: AsyncSession, mocker) -> None:
        """Opens its own session when none is passed and reads the state."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        _patch_session_maker(mocker, session)
        assert await should_cancel("snippets") is True

    @pytest.mark.asyncio
    async def test_fail_soft_with_passed_session(
        self, session: AsyncSession, mocker
    ) -> None:
        """A DB error on the passed-session path is swallowed → False."""
        mocker.patch.object(
            AppStateManager, "should_cancel", side_effect=SQLAlchemyError("boom")
        )
        assert await should_cancel("snippets", session=session) is False

    @pytest.mark.asyncio
    async def test_fail_soft_without_session(self, mocker) -> None:
        """A DB error on the own-session path is swallowed → False."""
        mocker.patch(
            "app.extensions.app_drain.get_async_session_maker",
            side_effect=SQLAlchemyError("boom"),
        )
        assert await should_cancel("snippets") is False


class TestFinalizeDrainIfComplete:
    """``finalize_drain_if_complete`` terminal-transition logic."""

    @pytest.mark.asyncio
    async def test_transitions_when_drained(self, session: AsyncSession) -> None:
        """A DISABLING app with zero running tasks flips to DISABLED."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        assert await finalize_drain_if_complete(session, "snippets") is True
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    @pytest.mark.asyncio
    async def test_noop_when_tasks_still_running(self, session: AsyncSession) -> None:
        """A DISABLING app with a running-task row stays DISABLING."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        session.add(AppRunningTask(app_key="snippets", celery_task_id="t1"))
        await session.commit()
        assert await finalize_drain_if_complete(session, "snippets") is False
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLING
        )

    @pytest.mark.asyncio
    async def test_noop_when_not_disabling(self, session: AsyncSession) -> None:
        """An app that is not DISABLING is never finalized."""
        await _seed_app(session, "snippets", AppLifecycleEnum.ENABLED)
        assert await finalize_drain_if_complete(session, "snippets") is False

    @pytest.mark.asyncio
    async def test_idempotent_under_repeated_calls(self, session: AsyncSession) -> None:
        """Exactly one of two calls performs the transition."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        first = await finalize_drain_if_complete(session, "snippets")
        second = await finalize_drain_if_complete(session, "snippets")
        assert (first, second) == (True, False)
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )


class TestRecordTaskSignals:
    """``task_prerun``/``task_postrun`` receivers and their coroutines."""

    @pytest.mark.asyncio
    async def test_record_start_inserts_row(
        self, session: AsyncSession, mocker
    ) -> None:
        """``_record_start`` inserts one running-task row."""
        _patch_session_maker(mocker, session)
        await app_drain._record_start("snippets", "task-1")
        rows = await AppRunningTaskManager.list(session, app_key="snippets")
        assert [row.celery_task_id for row in rows] == ["task-1"]

    @pytest.mark.asyncio
    async def test_record_end_deletes_row_and_finalizes(
        self, session: AsyncSession, mocker
    ) -> None:
        """``_record_end`` removes the row and finalizes the drained app."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        session.add(AppRunningTask(app_key="snippets", celery_task_id="task-1"))
        await session.commit()
        _patch_session_maker(mocker, session)

        await app_drain._record_end("snippets", "task-1")

        assert await AppRunningTaskManager.count(session, app_key="snippets") == 0
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    def test_receiver_ignores_untagged_task(self, mocker) -> None:
        """An untagged task triggers no session work."""
        run = mocker.patch.object(app_drain.celery.loop, "run_until_complete")
        task = MagicMock()
        task.owner_app_key = None
        record_task_start("task-1", task)
        record_task_end("task-1", task)
        run.assert_not_called()

    def test_receiver_dispatches_tagged_task(self, mocker) -> None:
        """A tagged task drives the counter coroutine for its owning app."""
        record = mocker.patch("app.extensions.app_drain._record_start")
        run = mocker.patch.object(app_drain.celery.loop, "run_until_complete")
        task = MagicMock()
        task.owner_app_key = "alerts"
        record_task_start("task-1", task)
        record.assert_called_once_with("alerts", "task-1")
        run.assert_called_once()

    def test_drainable_tasks_carry_their_owner(self) -> None:
        """Each drainable Celery task is tagged with its owning app key."""
        from app.extensions.apps.alerts.celery import (  # noqa: PLC0415
            backup_alert_config,
        )
        from app.extensions.apps.report.celery import (  # noqa: PLC0415
            generate_health_report,
        )

        assert getattr(generate_health_report, "owner_app_key", None) == "report"
        assert getattr(backup_alert_config, "owner_app_key", None) == "alerts"

    def test_library_snippet_sync_is_unowned(self) -> None:
        """Leave the library-owned snippet sync out of every app's drain.

        It writes library ``Snippet`` rows that consumers read regardless of app
        state, so tagging it would let a snippets disable cancel ingestion.
        """
        assert getattr(sync_snippets, "owner_app_key", None) is None


class TestReconciler:
    """``reconcile_disabling_apps`` safety-net behavior."""

    @pytest.mark.asyncio
    async def test_finalizes_drained_disabling_apps(
        self, session: AsyncSession, mocker
    ) -> None:
        """Every zero-count DISABLING app transitions to DISABLED."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        await _seed_app(session, "report", AppLifecycleEnum.DISABLING)
        _patch_session_maker(mocker, session)

        await app_drain._reconcile_disabling_apps()

        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )
        assert (
            await AppStateManager.current_lifecycle(session, "report")
            is AppLifecycleEnum.DISABLED
        )

    @pytest.mark.asyncio
    async def test_prunes_stale_rows_then_finalizes(
        self, session: AsyncSession, mocker
    ) -> None:
        """A row older than the stale TTL is pruned, unblocking the drain."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        session.add(
            AppRunningTask(
                app_key="snippets",
                celery_task_id="orphan",
                created_at=utc_now() - timedelta(hours=2),
            )
        )
        await session.commit()
        _patch_session_maker(mocker, session)

        await app_drain._reconcile_disabling_apps()

        assert await AppRunningTaskManager.count(session, app_key="snippets") == 0
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )

    @pytest.mark.asyncio
    async def test_keeps_fresh_rows_and_stays_disabling(
        self, session: AsyncSession, mocker
    ) -> None:
        """A recent running-task row is kept and blocks finalization."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        session.add(AppRunningTask(app_key="snippets", celery_task_id="live"))
        await session.commit()
        _patch_session_maker(mocker, session)

        await app_drain._reconcile_disabling_apps()

        assert await AppRunningTaskManager.count(session, app_key="snippets") == 1
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLING
        )

    @pytest.mark.asyncio
    async def test_ignores_non_disabling_apps(
        self, session: AsyncSession, mocker
    ) -> None:
        """Apps in other lifecycle states are left untouched."""
        await _seed_app(session, "snippets", AppLifecycleEnum.ENABLED)
        await _seed_app(session, "report", AppLifecycleEnum.ENABLING)
        _patch_session_maker(mocker, session)

        await app_drain._reconcile_disabling_apps()

        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.ENABLED
        )
        assert (
            await AppStateManager.current_lifecycle(session, "report")
            is AppLifecycleEnum.ENABLING
        )

    @pytest.mark.asyncio
    async def test_per_app_failure_does_not_stop_the_loop(
        self, session: AsyncSession, mocker
    ) -> None:
        """One app's finalize failure is isolated; the next app still processes.

        Each app is finalized on its own freshly opened session, so the loop
        keeps going after a per-app error rather than poisoning the rest.
        """
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        await _seed_app(session, "report", AppLifecycleEnum.DISABLING)
        maker = _patch_session_maker(mocker, session)
        finalize = mocker.patch(
            "app.extensions.app_drain.finalize_drain_if_complete",
            new=AsyncMock(side_effect=[SQLAlchemyError("boom"), False]),
        )

        await app_drain._reconcile_disabling_apps()

        disabling_app_count = 2
        prune_sweep_count = 1
        assert finalize.await_count == disabling_app_count
        assert maker.call_count == prune_sweep_count + disabling_app_count

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_per_app_session_isolation_on_real_postgres(
        self,
        postgres_engine: AsyncEngine,
        postgres_session: AsyncSession,
        mocker,
    ) -> None:
        """Guard per-app session isolation when one app aborts its transaction.

        Real-PostgreSQL sibling of ``test_per_app_failure_does_not_stop_the_loop``.
        On PostgreSQL the first error in a transaction aborts every later statement
        in it (``InFailedSqlTransaction``), so this guards the reconciler's per-app
        session isolation against a refactor that reuses one shared session: bind
        the session maker to the real engine (each app gets an independent
        ``asyncpg`` session), force the first-processed app's finalize to abort its
        transaction with a real SQL error, and assert the next app still reaches
        ``DISABLED``. A shared session would carry the abort forward and leave the
        survivor ``DISABLING`` — failing this test.
        """
        # ``postgres_session`` shares ``postgres_engine`` and created the tables.
        await _seed_app(postgres_session, "snippets", AppLifecycleEnum.DISABLING)
        await _seed_app(postgres_session, "report", AppLifecycleEnum.DISABLING)

        # Each ``maker()`` call yields an independent real session.
        mocker.patch(
            "app.extensions.app_drain.get_async_session_maker",
            return_value=get_async_session_maker_from_engine(postgres_engine),
        )

        # Fail on the first app processed: a shared session would carry the abort
        # forward to the survivor; per-app sessions do not.
        real_finalize = app_drain.finalize_drain_if_complete
        processed: list[str] = []

        async def flaky(session: AsyncSession, app_key: str) -> bool:
            processed.append(app_key)
            if len(processed) == 1:
                # Real statement that aborts the PostgreSQL transaction.
                await session.execute(text("SELECT 1 FROM table_that_does_not_exist"))
            return await real_finalize(session, app_key)

        mocker.patch(
            "app.extensions.app_drain.finalize_drain_if_complete", side_effect=flaky
        )

        await app_drain._reconcile_disabling_apps()

        seeded_app_count = 2
        assert len(processed) == seeded_app_count
        survivor = processed[1]
        async with get_async_session_maker_from_engine(postgres_engine)() as verify:
            assert (
                await AppStateManager.current_lifecycle(verify, survivor)
                is AppLifecycleEnum.DISABLED
            )
            # Failed app rolled back; stays DISABLING.
            assert (
                await AppStateManager.current_lifecycle(verify, processed[0])
                is AppLifecycleEnum.DISABLING
            )


class TestTrackAppTask:
    """The ``track_app_task`` counter wrapper for non-Celery in-request work."""

    @pytest.mark.asyncio
    async def test_inserts_row_while_active_and_removes_on_exit(
        self, session: AsyncSession
    ) -> None:
        """A row exists for the app while the block runs and is gone after."""
        async with track_app_task(session, "snippets"):
            assert await AppRunningTaskManager.count(session, app_key="snippets") == 1
        assert await AppRunningTaskManager.count(session, app_key="snippets") == 0

    @pytest.mark.asyncio
    async def test_blocks_premature_finalize_while_active(
        self, session: AsyncSession
    ) -> None:
        """A concurrent finalize is a no-op while the wrapped work is in flight."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        async with track_app_task(session, "snippets"):
            assert await finalize_drain_if_complete(session, "snippets") is False
            assert (
                await AppStateManager.current_lifecycle(session, "snippets")
                is AppLifecycleEnum.DISABLING
            )

    @pytest.mark.asyncio
    async def test_finalizes_drained_app_on_exit(self, session: AsyncSession) -> None:
        """Exiting the block drains the now-idle DISABLING app to DISABLED."""
        await _seed_app(session, "snippets", AppLifecycleEnum.DISABLING)
        async with track_app_task(session, "snippets"):
            pass
        assert (
            await AppStateManager.current_lifecycle(session, "snippets")
            is AppLifecycleEnum.DISABLED
        )
