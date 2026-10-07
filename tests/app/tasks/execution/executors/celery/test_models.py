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

"""Define tests for the CeleryExecutor."""

import io
import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.orm import undefer
from sqlmodel import col, select

from app.tasks.crud import (
    TaskHistoryLogStateManager,
    TaskHistoryManager,
    TaskManager,
)
from app.tasks.execution.executors.celery.models import (
    CeleryExecutor,
)
from app.tasks.models import (
    LogCaptureStatusEnum,
    Task,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryLog,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.factories import TaskFactory


@pytest.fixture
def executor() -> CeleryExecutor:
    """Return a CeleryExecutor instance."""
    return CeleryExecutor()


@pytest_asyncio.fixture
async def celery_task(session) -> Task:
    """Return a persisted CELERY backend task."""
    return await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="celery-task",
                backend=TaskBackendEnum.CELERY,
                protected=True,
                data={
                    "callable": "app.extensions.apps.inventory.sync.run_scheduled_inventory_sync",
                    "target": "local",
                },
            )
        ),
    )


@pytest_asyncio.fixture
async def celery_queue_item(session, celery_task) -> TaskHistory:
    """Return a pending TaskHistory for the celery_task fixture."""
    history = TaskHistory(
        task_id=celery_task.id,
        task=celery_task,
        execution_request=TaskExecutionRequest(
            task=celery_task.name,
            target="local",
            meta={},
            tracking={"evaluation_id": ""},
        ),
        status=TaskHistoryStatusEnum.PENDING,
        executed_by="test-user",
    )
    saved = await TaskHistoryManager.save(session, history)
    return await TaskHistoryManager.get_or_404(
        session,
        select_related=(TaskHistory.task,),
        query_options=[undefer(TaskHistory.execution_request)],
        id=saved.id,
    )


async def sample_callable() -> str:
    """Return a sample result for testing."""
    return "sync completed"


class TestCeleryExecutorGetHosts:
    """Test CeleryExecutor.get_hosts."""

    @pytest.mark.asyncio
    async def test_returns_local_host(self, executor) -> None:
        """Assert get_hosts returns a local host entry."""
        hosts = await executor.get_hosts()
        assert "local" in hosts
        assert hosts["local"] == "localhost"

    @pytest.mark.asyncio
    async def test_host_states_inherit_the_usable_default(self, executor) -> None:
        """Assert the base implementation reports the local host as usable.

        Celery has no notion of a host that is registered but cannot run anything —
        the work happens in this process — so it does not override
        ``get_host_states``. Asserted because a backend silently returning an empty
        list here would read to a caller as "the whole fleet is gone".
        """
        states = await executor.get_host_states()

        assert [state.name for state in states] == ["local"]
        assert states[0].reachable is True
        assert states[0].driver_healthy is True
        assert states[0].status is None


class TestCeleryExecutorValidateJob:
    """Test CeleryExecutor.validate_job."""

    @pytest.mark.asyncio
    async def test_valid_callable_path(self, executor) -> None:
        """Assert a valid callable path within the allowed namespace passes."""
        job = {
            "callable": "app.extensions.apps.inventory.sync.run_scheduled_inventory_sync"
        }
        result = await executor.validate_job(job)
        assert result == job

    @pytest.mark.asyncio
    async def test_missing_callable_raises(self, executor) -> None:
        """Assert missing callable key raises ValueError."""
        with pytest.raises(ValueError, match="callable"):
            await executor.validate_job({})

    @pytest.mark.asyncio
    async def test_non_importable_callable_raises(self, executor) -> None:
        """Assert a non-importable callable path raises ValueError."""
        job = {"callable": "app.nonexistent.module.func"}
        with pytest.raises(ValueError, match="import"):
            await executor.validate_job(job)

    @pytest.mark.asyncio
    async def test_outside_allowed_namespace_raises(self, executor) -> None:
        """Assert a callable outside the allowed namespace raises ValueError."""
        job = {"callable": "subprocess.call"}
        with pytest.raises(ValueError, match="allowed namespace"):
            await executor.validate_job(job)

    @pytest.mark.asyncio
    async def test_non_callable_attribute_raises(self, executor) -> None:
        """Assert a non-callable attribute raises ValueError."""
        job = {
            "callable": "app.tasks.execution.executors.celery.models.CELERY_CALLABLE_ALLOWED_PREFIX"
        }
        with pytest.raises(TypeError, match="not callable"):
            await executor.validate_job(job)


class TestCeleryExecutorDispatchTask:
    """Test CeleryExecutor.dispatch_task."""

    @pytest.mark.asyncio
    async def test_successful_dispatch(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert successful dispatch updates status to SUCCESS."""
        with (
            patch.object(
                executor,
                "_run_callable",
                new_callable=AsyncMock,
                return_value="done",
            ),
            patch(
                "app.tasks.execution.executors.celery.models.TaskHistoryManager.save",
                new_callable=AsyncMock,
                side_effect=lambda _s, qi, **_kw: qi,
            ),
        ):
            result = await executor.dispatch_task(session, celery_queue_item)
        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.started_at is not None
        assert result.finished_at is not None

    @pytest.mark.asyncio
    async def test_dispatch_records_both_streams_complete(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert both streams are recorded ``complete`` rather than defaulted.

        The Celery executor captures each buffer in full and synchronously, so
        its capture is complete by construction and must not inherit the
        pessimistic model default the Nomad path starts from.
        """
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="done",
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        states = await TaskHistoryLogStateManager.list_for_task(session, result.id)
        assert {state.stream for state in states} == {
            TaskLogType.STDOUT,
            TaskLogType.STDERR,
        }
        assert {state.capture_status for state in states} == {
            LogCaptureStatusEnum.COMPLETE
        }

    @pytest.mark.asyncio
    async def test_dispatch_records_complete_for_a_silent_task(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert a task that printed nothing still yields two complete rows.

        Without an unconditional write a zero-output Celery task creates no
        state rows at all and aggregates to ``unknown`` — reintroducing, on
        the Celery side, the empty-versus-lost ambiguity this work removes.
        """
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        states = await TaskHistoryLogStateManager.list_for_task(session, result.id)
        stderr_state = next(
            state for state in states if state.stream == TaskLogType.STDERR
        )
        assert stderr_state.capture_status == LogCaptureStatusEnum.COMPLETE
        assert stderr_state.producer_offset == 0

    @pytest.mark.asyncio
    async def test_failed_dispatch(self, executor, session, celery_queue_item) -> None:
        """Assert failed callable updates status to FAILED and persists stderr chunks."""
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await executor.dispatch_task(session, celery_queue_item)
        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.finished_at is not None
        stderr_chunks = (
            await session.exec(
                select(TaskHistoryLog)
                .where(col(TaskHistoryLog.task_history_id) == result.id)
                .where(col(TaskHistoryLog.stream) == TaskLogType.STDERR)
                .order_by(col(TaskHistoryLog.start_offset))
            )
        ).all()
        stderr = "".join(chunk.content for chunk in stderr_chunks)
        assert "boom" in stderr
        assert "RuntimeError" in stderr

    @pytest.mark.asyncio
    async def test_failed_dispatch_reason_names_callable_and_exception_type(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert the reason names the resolved callable and the exception type.

        The exception's own message is not interpolated; it stays in the
        traceback written to stderr.
        """
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom with secrets"),
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        assert result.failure_reason == (
            "Task callable "
            "'app.extensions.apps.inventory.sync.run_scheduled_inventory_sync' "
            "raised RuntimeError."
        )
        assert "boom with secrets" not in result.failure_reason

    @pytest.mark.asyncio
    async def test_failed_dispatch_reason_without_a_callable_path(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert a non-mapping ``task.data`` still composes a reason, without raising.

        ``Task.data`` is a raw JSON column on a table model, so no validator
        constrains what a stored row holds; the composer must not raise a second
        exception out of the error path.
        """
        celery_queue_item.task.data = "not-a-mapping"
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            side_effect=TypeError("string indices must be integers"),
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason == "Task execution raised TypeError."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored_callable",
        [
            {"dsn": "postgres://user:hunter2@db/prod"},
            "postgres://user:hunter2@db/prod",
            42,
        ],
    )
    async def test_failed_dispatch_reason_omits_an_unvalidated_callable_value(
        self, executor, session, celery_queue_item, stored_callable
    ) -> None:
        """Assert a callable value outside the allowed namespace is not echoed.

        ``Task.data`` holds arbitrary JSON, so a stored ``callable`` can be a
        mapping, a number, or a string carrying credentials that never names a
        module. None may reach this unmasked field.
        """
        celery_queue_item.task.data = {"callable": stored_callable}
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            side_effect=AttributeError("'dict' object has no attribute 'rsplit'"),
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason == "Task execution raised AttributeError."

    @pytest.mark.asyncio
    async def test_successful_dispatch_records_no_failure_reason(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert a run that succeeded carries no failure reason."""
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="ok",
        ):
            result = await executor.dispatch_task(session, celery_queue_item)

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.failure_reason is None

    @pytest.mark.asyncio
    async def test_dispatch_stores_logs(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert dispatch stores captured stdout chunks in taskhistory_log."""
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="output data",
        ):
            result = await executor.dispatch_task(session, celery_queue_item)
        state_rows = await TaskHistoryLogStateManager.list_for_task(session, result.id)
        assert {row.source for row in state_rows} == {"execution"}
        assert TaskLogType.STDOUT in {row.stream for row in state_rows}
        stdout_chunks = (
            await session.exec(
                select(TaskHistoryLog)
                .where(col(TaskHistoryLog.task_history_id) == result.id)
                .where(col(TaskHistoryLog.stream) == TaskLogType.STDOUT)
            )
        ).all()
        assert stdout_chunks
        assert "output data" in "".join(chunk.content for chunk in stdout_chunks)

    @pytest.mark.asyncio
    async def test_dispatch_forwards_meta_as_kwargs(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert run kwargs are populated from ``execution_request.meta``."""
        celery_queue_item.execution_request.meta = {"syncer": "PMM"}
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="ok",
        ) as mock_run:
            await executor.dispatch_task(session, celery_queue_item)
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["kwargs"] == {"syncer": "PMM"}

    @pytest.mark.asyncio
    async def test_dispatch_forwards_empty_kwargs_when_meta_none(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert ``meta = None`` resolves to an empty kwargs forward."""
        celery_queue_item.execution_request.meta = None
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="ok",
        ) as mock_run:
            await executor.dispatch_task(session, celery_queue_item)
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["kwargs"] == {}

    @pytest.mark.asyncio
    async def test_dispatch_skips_underscore_prefixed_meta_keys(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert underscore-prefixed control keys are filtered out before forwarding."""
        celery_queue_item.execution_request.meta = {
            "syncer": "PMM",
            "_chain_task_names": ["other"],
            "_chain_on_failure": False,
        }
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="ok",
        ) as mock_run:
            await executor.dispatch_task(session, celery_queue_item)
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["kwargs"] == {"syncer": "PMM"}

    @pytest.mark.asyncio
    async def test_dispatch_skips_reserved_target_meta_key(
        self, executor, session, celery_queue_item
    ) -> None:
        """Assert ``meta["target"]`` is treated as routing data, not a kwarg.

        ``_dispatch_chained_task`` and the Nomad executor's ``_prepare_task``
        path both inject ``meta["target"]`` carrying the executor host slug.
        Forwarding it would TypeError any Celery callable (e.g.
        ``run_scheduled_inventory_sync``) that does not accept ``target``.
        """
        celery_queue_item.execution_request.meta = {
            "syncer": "PMM",
            "target": "local",
        }
        with patch.object(
            executor,
            "_run_callable",
            new_callable=AsyncMock,
            return_value="ok",
        ) as mock_run:
            await executor.dispatch_task(session, celery_queue_item)
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["kwargs"] == {"syncer": "PMM"}


class TestCeleryExecutorRunCallable:
    """Test CeleryExecutor._run_callable."""

    @pytest.fixture
    def mock_module(self) -> ModuleType:
        """Return a fake module with async and sync callables."""
        mod = ModuleType("app.fake.module")

        async def async_func() -> str:
            sys.stdout.write("async stdout\n")
            return "async result"

        def sync_func() -> str:
            sys.stdout.write("sync stdout\n")
            return "sync result"

        mod.async_func = async_func
        mod.sync_func = sync_func
        mod.NOT_CALLABLE = "a string"
        return mod

    def _make_task(self, callable_path: str) -> Task:
        """Return a minimal Task with the given callable path."""
        return Task(
            name="test",
            backend=TaskBackendEnum.CELERY,
            protected=True,
            data={"callable": callable_path, "target": "local"},
        )

    @pytest.mark.asyncio
    async def test_async_callable_captures_stdout(self, executor, mock_module) -> None:
        """Assert async callable output is captured in stdout buffer."""
        task = self._make_task("app.fake.module.async_func")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("importlib.import_module", return_value=mock_module):
            result = await executor._run_callable(task, stdout, stderr)
        assert result == "async result"
        assert "async stdout" in stdout.getvalue()

    @pytest.mark.asyncio
    async def test_sync_callable_captures_stdout(self, executor, mock_module) -> None:
        """Assert sync callable output is captured in stdout buffer."""
        task = self._make_task("app.fake.module.sync_func")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("importlib.import_module", return_value=mock_module):
            result = await executor._run_callable(task, stdout, stderr)
        assert result == "sync result"
        assert "sync stdout" in stdout.getvalue()

    @pytest.mark.asyncio
    async def test_non_callable_raises_type_error(self, executor, mock_module) -> None:
        """Assert non-callable attribute raises TypeError."""
        task = self._make_task("app.fake.module.NOT_CALLABLE")
        with (
            patch("importlib.import_module", return_value=mock_module),
            pytest.raises(TypeError, match="not callable"),
        ):
            await executor._run_callable(task, io.StringIO(), io.StringIO())

    @pytest.mark.asyncio
    async def test_outside_allowed_namespace_raises_before_import(
        self, executor
    ) -> None:
        """Assert a callable outside the allowed namespace is not imported."""
        task = self._make_task("os.system")
        with (
            patch("importlib.import_module") as mock_import,
            pytest.raises(ValueError, match="allowed namespace"),
        ):
            await executor._run_callable(task, io.StringIO(), io.StringIO())
        mock_import.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_executing_prefix(self, executor, mock_module) -> None:
        """Assert stdout buffer starts with an 'Executing' line."""
        task = self._make_task("app.fake.module.async_func")
        stdout = io.StringIO()
        with patch("importlib.import_module", return_value=mock_module):
            await executor._run_callable(task, stdout, io.StringIO())
        assert stdout.getvalue().startswith("Executing app.fake.module.async_func")

    @pytest.mark.asyncio
    async def test_async_callable_forwards_kwargs(self, executor) -> None:
        """Assert kwargs are unpacked into an async callable as keyword arguments."""
        captured = {}
        mod = ModuleType("app.fake.module")

        async def takes_kwargs(*, syncer: str) -> str:
            captured["syncer"] = syncer
            return syncer

        mod.takes_kwargs = takes_kwargs
        task = self._make_task("app.fake.module.takes_kwargs")
        with patch("importlib.import_module", return_value=mod):
            result = await executor._run_callable(
                task,
                io.StringIO(),
                io.StringIO(),
                kwargs={"syncer": "PMM"},
            )
        assert result == "PMM"
        assert captured == {"syncer": "PMM"}

    @pytest.mark.asyncio
    async def test_sync_callable_forwards_kwargs(self, executor) -> None:
        """Assert kwargs are unpacked into a sync callable executed via to_thread."""
        mod = ModuleType("app.fake.module")

        def takes_kwargs(*, a: int, b: int) -> int:
            return a + b

        mod.takes_kwargs = takes_kwargs
        task = self._make_task("app.fake.module.takes_kwargs")
        expected_sum = 2 + 3
        with patch("importlib.import_module", return_value=mod):
            result = await executor._run_callable(
                task,
                io.StringIO(),
                io.StringIO(),
                kwargs={"a": 2, "b": 3},
            )
        assert result == expected_sum

    @pytest.mark.asyncio
    async def test_kwargs_none_invokes_zero_arg_callable(
        self, executor, mock_module
    ) -> None:
        """Assert ``kwargs=None`` (the default) invokes the callable with no args."""
        task = self._make_task("app.fake.module.async_func")
        with patch("importlib.import_module", return_value=mock_module):
            result = await executor._run_callable(task, io.StringIO(), io.StringIO())
        assert result == "async result"

    @pytest.mark.asyncio
    async def test_unknown_kwarg_raises_type_error(self, executor, mock_module) -> None:
        """Assert an unexpected kwarg surfaces the callable's TypeError."""
        task = self._make_task("app.fake.module.async_func")
        with (
            patch("importlib.import_module", return_value=mock_module),
            pytest.raises(TypeError),
        ):
            await executor._run_callable(
                task,
                io.StringIO(),
                io.StringIO(),
                kwargs={"unexpected": 1},
            )


class TestCeleryExecutorSyncTaskHistory:
    """Test CeleryExecutor._sync_task_history."""

    @pytest.mark.asyncio
    async def test_returns_unchanged(self, executor) -> None:
        """Assert _sync_task_history returns the queue_item unchanged."""
        queue_item = TaskHistory(
            task_id=1,
            execution_request=TaskExecutionRequest(
                task="test", target="local", tracking={}
            ),
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        result = await executor._sync_task_history(queue_item)
        assert result is queue_item


class TestCeleryExecutorStopTask:
    """Test stopping a Celery task."""

    @pytest.mark.asyncio
    async def test_stop_is_noop(self, executor) -> None:
        """Assert _stop_task does not raise."""
        queue_item = TaskHistory(
            task_id=1,
            execution_request=TaskExecutionRequest(
                task="test", target="local", tracking={}
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        await executor._stop_task(queue_item)

    @pytest.mark.asyncio
    async def test_stop_reaches_stopped_status(
        self, executor: CeleryExecutor, session, celery_queue_item: TaskHistory
    ) -> None:
        """Assert stopping a running row terminates it.

        A Celery sync reports no new backend state, so the stop is the only
        thing that can move the row out of RUNNING.
        """
        celery_queue_item.status = TaskHistoryStatusEnum.RUNNING
        celery_queue_item.task.alert_on_fail = False

        with patch("app.tasks.execution.models.schedule_annotation") as mock_schedule:
            result = await executor.stop_task(session, celery_queue_item)

        assert result.status == TaskHistoryStatusEnum.STOPPED
        assert result.finished_at is not None
        mock_schedule.assert_called_once_with(result, "STOPPED")


class TestCeleryExecutorStreamLogs:
    """Test CeleryExecutor.stream_logs."""

    @pytest.mark.asyncio
    async def test_stream_logs_yields_nothing(self, executor) -> None:
        """Assert stream_logs is an empty async generator.

        Celery tasks complete synchronously inside ``dispatch_task``, so the
        route's live-stream branch is unreachable in practice. Finished Celery
        log retrieval goes through the chunk store via ``iter_task_history_logs``.
        """
        queue_item = TaskHistory(
            task_id=1,
            execution_request=TaskExecutionRequest(
                task="test", target="local", tracking={}
            ),
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        logs = [log async for log in executor.stream_logs(queue_item)]
        assert logs == []


class TestCeleryExecutorUnsupportedOps:
    """Test CeleryExecutor unsupported operations."""

    @pytest.mark.asyncio
    async def test_stream_file_raises(self, executor) -> None:
        """Assert stream_file raises NotImplementedError."""
        queue_item = TaskHistory(
            task_id=1,
            execution_request=TaskExecutionRequest(
                task="test", target="local", tracking={}
            ),
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        with pytest.raises(NotImplementedError):
            await executor.stream_file(queue_item, "/path").__anext__()

    @pytest.mark.asyncio
    async def test_list_files_raises(self, executor) -> None:
        """Assert list_files raises NotImplementedError."""
        queue_item = TaskHistory(
            task_id=1,
            execution_request=TaskExecutionRequest(
                task="test", target="local", tracking={}
            ),
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        with pytest.raises(NotImplementedError):
            await executor.list_files(queue_item, "/path")
