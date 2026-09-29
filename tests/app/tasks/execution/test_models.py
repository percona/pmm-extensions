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

"""Define tests for the app.tasks.execution.models module."""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import PrivateAttr
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import undefer
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils import utc_now
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.execution.models import _TERMINAL_STATUS_EVENT_MAP, BaseExecutor
from app.tasks.models import (
    FileMetadata,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskWrite,
)
from tests.app.factories import TaskFactory


class ConcreteExecutor(BaseExecutor):
    """Provide a minimal concrete implementation of BaseExecutor for testing."""

    async def dispatch_task(
        self,
        session: Any,
        queue_item: TaskHistory,
        task: Any = None,
    ) -> TaskHistory:
        """Return the queue item unchanged."""
        return queue_item

    async def _stop_task(self, queue_item: TaskHistory) -> None:
        """Do nothing."""

    async def validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Return the job unchanged."""
        return job

    async def get_hosts(self) -> dict[str, str]:
        """Return an empty host map."""
        return {}

    async def stream_logs(
        self,
        queue_item: TaskHistory,
        start_offsets: dict[str, dict[str, int]] | None = None,
    ) -> AsyncGenerator[TaskLog, None]:
        """Yield nothing."""
        return
        yield

    async def stream_file(
        self,
        queue_item: TaskHistory,
        path: str,
        chunk_size: int = 1024 * 1024,
        *,
        anonymize: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Yield nothing."""
        return
        yield

    async def list_files(
        self, queue_item: TaskHistory, path: str
    ) -> dict[str, FileMetadata]:
        """Return an empty file listing."""
        return {}

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        """Return the queue item unchanged."""
        return queue_item


@pytest.fixture
def executor() -> ConcreteExecutor:
    """Create a ConcreteExecutor instance."""
    return ConcreteExecutor()


class TestTransformPayload:
    """Test BaseExecutor.transform_payload."""

    @pytest.mark.asyncio
    async def test_calls_parse_then_validate(self, executor: ConcreteExecutor):
        """Assert transform_payload calls parse_payload then validate_job."""
        parsed = {"key": "value"}
        validated = {"key": "value", "validated": True}
        mock_parse = AsyncMock(return_value=parsed)
        mock_validate = AsyncMock(return_value=validated)

        with (
            patch.object(ConcreteExecutor, "parse_payload", mock_parse),
            patch.object(ConcreteExecutor, "validate_job", mock_validate),
        ):
            result = await executor.transform_payload('{"key": "value"}', "json")

        mock_parse.assert_awaited_once_with('{"key": "value"}', "json")
        mock_validate.assert_awaited_once_with(parsed)
        assert result == validated

    @pytest.mark.asyncio
    async def test_returns_validate_job_result(self, executor: ConcreteExecutor):
        """Assert transform_payload returns the result of validate_job."""
        expected = {"result": "validated"}

        with (
            patch.object(ConcreteExecutor, "parse_payload", AsyncMock(return_value={})),
            patch.object(
                ConcreteExecutor, "validate_job", AsyncMock(return_value=expected)
            ),
        ):
            result = await executor.transform_payload("{}", "json")

        assert result == expected

    @pytest.mark.asyncio
    async def test_propagates_parse_error(self, executor: ConcreteExecutor):
        """Assert transform_payload propagates a parse failure without validating."""
        mock_validate = AsyncMock()
        with (
            patch.object(ConcreteExecutor, "validate_job", mock_validate),
            pytest.raises(ValueError, match="unsupported format"),
        ):
            await executor.transform_payload("{}", "xml")

        mock_validate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_validate_error(self, executor: ConcreteExecutor):
        """Assert transform_payload propagates a validate_job failure."""
        with (
            patch.object(
                ConcreteExecutor,
                "validate_job",
                AsyncMock(side_effect=ValueError("invalid job")),
            ),
            pytest.raises(ValueError, match="invalid job"),
        ):
            await executor.transform_payload("{}", "json")


class TestParsePayload:
    """Test BaseExecutor.parse_payload."""

    @pytest.mark.asyncio
    async def test_delegates_to_utils_parse_payload(self, executor: ConcreteExecutor):
        """Assert parse_payload delegates to utils.parse_payload."""
        with patch(
            "app.tasks.execution.models.parse_payload",
            return_value={"parsed": True},
        ) as mock_parse:
            result = await executor.parse_payload('{"parsed": true}', "json")

        mock_parse.assert_called_once_with('{"parsed": true}', "json")
        assert result == {"parsed": True}

    @pytest.mark.asyncio
    async def test_raises_on_unsupported_format(self, executor: ConcreteExecutor):
        """Assert parse_payload raises ValueError for an unsupported format."""
        with pytest.raises(ValueError, match="unsupported format"):
            await executor.parse_payload("{}", "xml")


class StopStubExecutor(ConcreteExecutor):
    """Provide an executor whose backend answers from a configured state.

    Lets ``stop_task`` run against real persistence with PMM as the only
    patched boundary: the backend stop and the executor-specific sync report
    what the executor was built with instead of reaching Nomad or Celery.
    """

    _stop_error: BaseException | None = PrivateAttr(default=None)
    _synced_status: TaskHistoryStatusEnum | None = PrivateAttr(default=None)
    _synced_finished_at: datetime | None = PrivateAttr(default=None)
    _stop_calls: list[TaskHistory] = PrivateAttr(default_factory=list)
    _sync_calls: list[tuple[TaskHistory, AsyncSession | None]] = PrivateAttr(
        default_factory=list
    )

    @classmethod
    def resolving_to(
        cls,
        status: TaskHistoryStatusEnum | None = None,
        finished_at: datetime | None = None,
        stop_error: BaseException | None = None,
    ) -> "StopStubExecutor":
        """Build an executor whose backend reports the given state.

        :param status: The status the sync resolves the record to, or None to
            leave the record as it stands.
        :param finished_at: The finish time the sync establishes, or None to
            leave it untouched.
        :param stop_error: The error the backend raises instead of stopping.
        :return: The configured executor.
        """
        executor = cls()
        executor._stop_error = stop_error
        executor._synced_status = status
        executor._synced_finished_at = finished_at
        return executor

    async def _stop_task(self, queue_item: TaskHistory) -> None:
        """Record the stop request, or fail the way an unreachable backend does."""
        if self._stop_error is not None:
            raise self._stop_error
        self._stop_calls.append(queue_item)

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        """Return the record as the configured backend state resolves it."""
        self._sync_calls.append((queue_item, writer_session))
        if self._synced_status is not None:
            queue_item.status = self._synced_status
        if self._synced_finished_at is not None:
            queue_item.finished_at = self._synced_finished_at
        return queue_item


class TestStopTask:
    """Test BaseExecutor.stop_task against the real async session.

    Exercises the real ``TaskHistoryManager.save`` / ``session.refresh``
    lifecycle (the ``MissingGreenlet`` regression class).
    """

    @staticmethod
    async def _persist_history(
        session: AsyncSession,
        status: TaskHistoryStatusEnum,
        name: str,
    ) -> TaskHistory:
        """Create and persist a real ``TaskHistory`` in the given status.

        :param session: The async session fixture to persist against.
        :type session: AsyncSession
        :param status: The initial status of the record.
        :type status: TaskHistoryStatusEnum
        :param name: The task name (unique within the test DB).
        :type name: str
        :return: The persisted record with ``task`` and ``execution_request`` loaded.
        :rtype: TaskHistory
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name=name,
                    backend=TaskBackendEnum.NOMAD,
                    alert_on_fail=False,
                )
            ),
        )
        queue_item = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=status,
            executed_by="test-user",
        )
        saved = await TaskHistoryManager.save(session, queue_item)
        # ``save`` re-defers ``task``/``execution_request``; eager-load them so
        # ``sync_task_history`` reads ``task.alert_on_fail`` without a lazy load
        # (identity map holds only weak refs → ``Task`` else collectible).
        return await TaskHistoryManager.get_or_404(
            session,
            select_related=(TaskHistory.task,),
            query_options=[undefer(TaskHistory.execution_request)],
            id=saved.id,
        )

    @pytest.mark.asyncio
    async def test_sets_stopped_status_and_finished_at(self, session: AsyncSession):
        """Assert stop_task sets status to STOPPED, sets finished_at, and persists."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-1"
        )
        executor = StopStubExecutor()

        with patch("app.tasks.execution.models.schedule_annotation"):
            result = await executor.stop_task(session, saved_history)

        assert executor._stop_calls == [saved_history]
        assert result.status == TaskHistoryStatusEnum.STOPPED
        assert result.finished_at is not None
        result_id = result.id

        # Prove the change persisted across a transaction boundary, not just in
        # the identity map: rollback discards uncommitted state (the committed
        # save survives), then refetch forces a fresh read from the DB. Capture
        # the id first — rollback expires ``result``, so a later attribute read
        # would trigger a sync lazy-load.
        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == TaskHistoryStatusEnum.STOPPED
        assert refetched.finished_at is not None

    @pytest.mark.asyncio
    async def test_forced_stop_clears_a_stale_failure_reason(
        self, session: AsyncSession
    ):
        """Assert a run the sync left non-terminal is STOPPED with no failure reason."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-clear-reason"
        )
        saved_history.set_failure_reason("A reason that predates the stop.")
        executor = StopStubExecutor()

        with patch("app.tasks.execution.models.schedule_annotation"):
            result = await executor.stop_task(session, saved_history)

        result_id = result.id
        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == TaskHistoryStatusEnum.STOPPED
        assert refetched.failure_reason is None

    @pytest.mark.asyncio
    async def test_stop_preserves_reason_of_a_run_the_sync_failed(
        self, session: AsyncSession
    ):
        """Assert a reason the sync resolved survives a stop landing in the same window.

        The executor's own resolution wins over the stop, so a run that failed
        keeps the reason explaining why rather than being relabelled.
        """
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-keep-reason"
        )
        executor = StopStubExecutor.resolving_to(TaskHistoryStatusEnum.FAILED)

        with patch("app.tasks.execution.models.schedule_annotation"):
            saved_history.set_failure_reason("The run failed.")
            result = await executor.stop_task(session, saved_history)

        result_id = result.id
        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == TaskHistoryStatusEnum.FAILED
        assert refetched.failure_reason == "The run failed."

    @pytest.mark.asyncio
    async def test_calls_sync_task_history(self, session: AsyncSession):
        """Assert stop_task drives sync_task_history (via its _sync boundary)."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-2"
        )
        executor = StopStubExecutor()

        with patch("app.tasks.execution.models.schedule_annotation"):
            await executor.stop_task(session, saved_history)

        assert executor._sync_calls == [(saved_history, None)]

    @pytest.mark.asyncio
    async def test_emits_stopped_annotation_when_sync_still_running(
        self, session: AsyncSession
    ):
        """Assert STOPPED annotation is emitted when sync returns still-RUNNING."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-3"
        )

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await StopStubExecutor().stop_task(session, saved_history)

        mock_schedule.assert_called_once_with(result, "STOPPED")

    @pytest.mark.asyncio
    async def test_does_not_double_emit_when_sync_already_stopped(
        self, session: AsyncSession
    ):
        """Assert STOPPED annotation is emitted exactly once, not re-emitted.

        The sync transitions RUNNING -> STOPPED, so the real
        ``sync_task_history`` emits once; ``stop_task`` must detect that and
        skip its own emit.
        """
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-4"
        )
        executor = StopStubExecutor.resolving_to(TaskHistoryStatusEnum.STOPPED)

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            await executor.stop_task(session, saved_history)

        mock_schedule.assert_called_once_with(saved_history, "STOPPED")

    @pytest.mark.asyncio
    async def test_emits_stopped_annotation_when_not_running_initially(
        self, session: AsyncSession
    ):
        """Assert STOPPED annotation is emitted when task was not RUNNING before sync."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.PENDING, "stop-task-5"
        )

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await StopStubExecutor().stop_task(session, saved_history)

        mock_schedule.assert_called_once_with(result, "STOPPED")

    @pytest.mark.asyncio
    async def test_emits_once_when_not_running_but_sync_returns_terminal(
        self, session: AsyncSession
    ):
        """Assert a single STOPPED emit when not RUNNING but sync returns terminal.

        The record was not running, so the real ``sync_task_history`` does not
        emit even though it returns STOPPED; ``stop_task`` owns the single emit.
        """
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.PENDING, "stop-task-6"
        )
        executor = StopStubExecutor.resolving_to(TaskHistoryStatusEnum.STOPPED)

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await executor.stop_task(session, saved_history)

        mock_schedule.assert_called_once_with(result, "STOPPED")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("synced_status", "expected_event"),
        [
            (TaskHistoryStatusEnum.FAILED, "FAILED"),
            (TaskHistoryStatusEnum.SUCCESS, "COMPLETED"),
            (TaskHistoryStatusEnum.LOST, "LOST"),
            (TaskHistoryStatusEnum.STALE, "STALE"),
            (TaskHistoryStatusEnum.UNLAUNCHABLE, "UNLAUNCHABLE"),
        ],
    )
    async def test_keeps_terminal_status_derived_by_sync(
        self,
        session: AsyncSession,
        synced_status: TaskHistoryStatusEnum,
        expected_event: str,
    ):
        """Assert a terminal status the sync derived survives the stop path.

        Whether the stop or the run's own outcome wins is the executor's call,
        so the stop path persists the outcome it was handed and the run keeps a
        single annotation naming it.
        """
        saved_history = await self._persist_history(
            session,
            TaskHistoryStatusEnum.RUNNING,
            f"stop-task-keeps-{synced_status.value}",
        )
        executor = StopStubExecutor.resolving_to(synced_status, finished_at=utc_now())

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await executor.stop_task(session, saved_history)

        assert result.status == synced_status
        mock_schedule.assert_called_once_with(result, expected_event)
        result_id = result.id

        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == synced_status

    @pytest.mark.asyncio
    async def test_keeps_finished_at_established_by_sync(self, session: AsyncSession):
        """Assert the stop path keeps the finish time the sync established."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-keeps-finished-at"
        )
        exited_at = utc_now() - timedelta(minutes=5)
        executor = StopStubExecutor.resolving_to(
            TaskHistoryStatusEnum.FAILED, finished_at=exited_at
        )

        with patch("app.tasks.execution.models.schedule_annotation"):
            result = await executor.stop_task(session, saved_history)

        # SQLite returns the value tz-naive, so compare without tzinfo.
        assert result.finished_at.replace(tzinfo=None) == exited_at.replace(tzinfo=None)
        result_id = result.id

        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.finished_at.replace(tzinfo=None) == exited_at.replace(
            tzinfo=None
        )

    @pytest.mark.asyncio
    async def test_stamps_finished_at_when_sync_left_it_unset(
        self, session: AsyncSession
    ):
        """Assert a terminal status without a finish time still gets one.

        A Nomad job that disappeared resolves LOST without a finish time, and a
        terminal record without one reports no duration.
        """
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-stamps-finished-at"
        )
        executor = StopStubExecutor.resolving_to(TaskHistoryStatusEnum.LOST)

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await executor.stop_task(session, saved_history)

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at is not None
        mock_schedule.assert_called_once_with(result, "LOST")

    @pytest.mark.asyncio
    async def test_emits_derived_event_when_not_running_and_sync_terminal(
        self, session: AsyncSession
    ):
        """Assert the single emit names the outcome persisted, not the stop asked for."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.PENDING, "stop-task-derived-event"
        )
        executor = StopStubExecutor.resolving_to(TaskHistoryStatusEnum.FAILED)

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await executor.stop_task(session, saved_history)

        assert result.status == TaskHistoryStatusEnum.FAILED
        mock_schedule.assert_called_once_with(result, "FAILED")

    @pytest.mark.asyncio
    async def test_propagates_backend_stop_failure_before_writing(
        self, session: AsyncSession
    ):
        """Assert a backend that fails to stop leaves the record untouched."""
        saved_history = await self._persist_history(
            session, TaskHistoryStatusEnum.RUNNING, "stop-task-backend-failure"
        )
        executor = StopStubExecutor.resolving_to(
            stop_error=RuntimeError("backend unreachable")
        )

        with (
            patch("app.tasks.execution.models.schedule_annotation") as mock_schedule,
            pytest.raises(RuntimeError, match="backend unreachable"),
        ):
            await executor.stop_task(session, saved_history)

        assert executor._sync_calls == []
        mock_schedule.assert_not_called()
        refetched = await TaskHistoryManager.get_or_404(session, id=saved_history.id)
        assert refetched.status == TaskHistoryStatusEnum.RUNNING


class TestTerminalStatusEventMap:
    """Test the terminal status to annotation event mapping."""

    def test_covers_every_terminal_status(self):
        """Assert the map enumerates exactly the terminal statuses.

        Every status the stop path may persist needs an annotation event, so a
        terminal status missing from the map raises instead of reaching PMM.
        """
        assert set(_TERMINAL_STATUS_EVENT_MAP) == {
            status for status in TaskHistoryStatusEnum if status.is_terminal()
        }


class TestSyncTaskHistory:
    """Test BaseExecutor.sync_task_history."""

    @pytest.mark.asyncio
    async def test_calls_internal_sync(self, executor: ConcreteExecutor):
        """Assert sync_task_history calls _sync_task_history."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False
        mock_sync = AsyncMock(return_value=queue_item)

        with patch.object(ConcreteExecutor, "_sync_task_history", mock_sync):
            result = await executor.sync_task_history(queue_item)

        mock_sync.assert_awaited_once_with(queue_item, writer_session=None)
        assert result is queue_item

    @pytest.mark.asyncio
    async def test_forwards_writer_session(self, executor: ConcreteExecutor):
        """Assert sync_task_history forwards a writer_session to _sync_task_history."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False
        writer_session = MagicMock(spec=AsyncSession)
        mock_sync = AsyncMock(return_value=queue_item)

        with patch.object(ConcreteExecutor, "_sync_task_history", mock_sync):
            await executor.sync_task_history(queue_item, writer_session)

        mock_sync.assert_awaited_once_with(queue_item, writer_session=writer_session)

    @pytest.mark.asyncio
    async def test_alerts_when_alert_on_fail_is_true(self, executor: ConcreteExecutor):
        """Assert alert_for_status is called when task.alert_on_fail is True."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = True
        queue_item.alert_for_status = AsyncMock()

        with patch.object(
            ConcreteExecutor, "_sync_task_history", AsyncMock(return_value=queue_item)
        ):
            await executor.sync_task_history(queue_item)

        queue_item.alert_for_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_alert_when_alert_on_fail_is_false(
        self, executor: ConcreteExecutor
    ):
        """Assert alert_for_status is not called when task.alert_on_fail is False."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False
        queue_item.alert_for_status = AsyncMock()

        with patch.object(
            ConcreteExecutor, "_sync_task_history", AsyncMock(return_value=queue_item)
        ):
            await executor.sync_task_history(queue_item)

        queue_item.alert_for_status.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("initial_status", "final_status", "expected_event"),
        [
            (TaskHistoryStatusEnum.RUNNING, TaskHistoryStatusEnum.SUCCESS, "COMPLETED"),
            (TaskHistoryStatusEnum.RUNNING, TaskHistoryStatusEnum.FAILED, "FAILED"),
            (TaskHistoryStatusEnum.RUNNING, TaskHistoryStatusEnum.STOPPED, "STOPPED"),
            (TaskHistoryStatusEnum.RUNNING, TaskHistoryStatusEnum.LOST, "LOST"),
            (TaskHistoryStatusEnum.RUNNING, TaskHistoryStatusEnum.STALE, "STALE"),
            (
                TaskHistoryStatusEnum.RUNNING,
                TaskHistoryStatusEnum.UNLAUNCHABLE,
                "UNLAUNCHABLE",
            ),
        ],
    )
    async def test_annotates_terminal_transition(
        self,
        executor: ConcreteExecutor,
        initial_status: TaskHistoryStatusEnum,
        final_status: TaskHistoryStatusEnum,
        expected_event: str,
    ):
        """Assert schedule_annotation is called by default on terminal transitions."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.status = initial_status
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False

        synced_item = MagicMock(spec=TaskHistory)
        synced_item.status = final_status
        synced_item.task = MagicMock()
        synced_item.task.alert_on_fail = False

        with (
            patch.object(
                ConcreteExecutor,
                "_sync_task_history",
                AsyncMock(return_value=synced_item),
            ),
            patch(
                "app.tasks.execution.models.schedule_annotation",
            ) as mock_schedule,
        ):
            await executor.sync_task_history(queue_item)

        mock_schedule.assert_called_once_with(synced_item, expected_event)

    @pytest.mark.asyncio
    async def test_awaits_terminal_transition_when_await_annotations_true(
        self, executor: ConcreteExecutor
    ):
        """Assert await_annotation is awaited when ``await_annotations=True``."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False

        synced_item = MagicMock(spec=TaskHistory)
        synced_item.status = TaskHistoryStatusEnum.SUCCESS
        synced_item.task = MagicMock()
        synced_item.task.alert_on_fail = False

        with (
            patch.object(
                ConcreteExecutor,
                "_sync_task_history",
                AsyncMock(return_value=synced_item),
            ),
            patch(
                "app.tasks.execution.models.await_annotation",
                new_callable=AsyncMock,
            ) as mock_await,
            patch(
                "app.tasks.execution.models.schedule_annotation",
            ) as mock_schedule,
        ):
            await executor.sync_task_history(queue_item, await_annotations=True)

        mock_await.assert_awaited_once_with(synced_item, "COMPLETED")
        mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_annotate_when_still_running(
        self, executor: ConcreteExecutor
    ):
        """Assert no annotation when task remains RUNNING after sync."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False

        synced_item = MagicMock(spec=TaskHistory)
        synced_item.status = TaskHistoryStatusEnum.RUNNING
        synced_item.task = MagicMock()
        synced_item.task.alert_on_fail = False

        with (
            patch.object(
                ConcreteExecutor,
                "_sync_task_history",
                AsyncMock(return_value=synced_item),
            ),
            patch(
                "app.tasks.execution.models.schedule_annotation",
            ) as mock_schedule,
            patch(
                "app.tasks.execution.models.await_annotation",
                new_callable=AsyncMock,
            ) as mock_await,
        ):
            await executor.sync_task_history(queue_item)

        mock_schedule.assert_not_called()
        mock_await.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_annotate_already_terminal(self, executor: ConcreteExecutor):
        """Assert no duplicate annotation on re-sync of already-terminal task."""
        queue_item = MagicMock(spec=TaskHistory)
        queue_item.status = TaskHistoryStatusEnum.SUCCESS
        queue_item.task = MagicMock()
        queue_item.task.alert_on_fail = False

        synced_item = MagicMock(spec=TaskHistory)
        synced_item.status = TaskHistoryStatusEnum.SUCCESS
        synced_item.task = MagicMock()
        synced_item.task.alert_on_fail = False

        with (
            patch.object(
                ConcreteExecutor,
                "_sync_task_history",
                AsyncMock(return_value=synced_item),
            ),
            patch(
                "app.tasks.execution.models.schedule_annotation",
            ) as mock_schedule,
            patch(
                "app.tasks.execution.models.await_annotation",
                new_callable=AsyncMock,
            ) as mock_await,
        ):
            await executor.sync_task_history(queue_item)

        mock_schedule.assert_not_called()
        mock_await.assert_not_awaited()


_DEFAULT_WAIT_INTERVAL = 5


class TestDefaultWaitInterval:
    """Test BaseExecutor default configuration."""

    def test_default_wait_interval(self, executor: ConcreteExecutor):
        """Assert default wait_interval is 5 seconds."""
        assert executor.wait_interval == _DEFAULT_WAIT_INTERVAL


class TestGetEvents:
    """Test BaseExecutor.get_events default behaviour."""

    def test_default_returns_empty_list(self, executor: ConcreteExecutor):
        """Assert the base get_events returns an empty list."""
        assert executor.get_events(MagicMock(spec=TaskHistory)) == []


class TestPreflightStreamLogs:
    """Test BaseExecutor.preflight_stream_logs default behaviour."""

    def test_default_is_noop(self, executor: ConcreteExecutor):
        """Assert the base preflight_stream_logs is a no-op returning None."""
        assert executor.preflight_stream_logs(MagicMock(spec=TaskHistory)) is None


class TestStopTaskRegression:
    """Guard the real-session ``stop_task`` regression.

    ``TaskHistoryManager.save(session, queue_item)`` inside
    ``BaseExecutor.stop_task`` re-defers ``execution_request`` via its
    internal ``session.refresh(instance)``. Before the fix, the
    subsequent ``schedule_annotation(saved, "STOPPED")`` in the
    ``sync_emitted_stopped=False`` branch then touched that deferred
    attribute synchronously and crashed with ``MissingGreenlet`` on
    async drivers.
    """

    @pytest.mark.asyncio
    async def test_stopped_annotation_has_execution_request_loaded(
        self, executor: ConcreteExecutor, session: AsyncSession
    ):
        """Assert the STOPPED annotation receives a loaded instance.

        Arrange a ``TaskHistory`` in RUNNING state, have
        ``_sync_task_history`` keep it RUNNING (so ``sync_emitted_stopped``
        is False), and exercise ``stop_task`` with the real session.
        ``stop_task`` then forces the status to STOPPED and saves; the
        save re-defers ``execution_request``; the explicit refresh
        before ``schedule_annotation`` must reload it.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="stop-task",
                    backend=TaskBackendEnum.NOMAD,
                    alert_on_fail=False,
                )
            ),
        )
        queue_item = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
            executed_by="test-user",
        )
        saved_history = await TaskHistoryManager.save(session, queue_item)

        async def fake_sync(item: TaskHistory, *, writer_session=None) -> TaskHistory:
            return item

        captured = []

        def capture_annotation(arg: TaskHistory, _event: str) -> None:
            captured.append(arg)

        with (
            patch.object(ConcreteExecutor, "_stop_task", AsyncMock()),
            patch.object(
                ConcreteExecutor, "_sync_task_history", AsyncMock(side_effect=fake_sync)
            ),
            patch(
                "app.tasks.execution.models.schedule_annotation",
                side_effect=capture_annotation,
            ),
        ):
            result = await executor.stop_task(session, saved_history)

        assert len(captured) == 1
        annotated = captured[0]
        assert "execution_request" not in sa_inspect(annotated).unloaded
        assert annotated.execution_request.task == task.name
        assert result.status == TaskHistoryStatusEnum.STOPPED
