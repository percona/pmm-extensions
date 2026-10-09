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

"""Test the connectivity check service function."""

import ast
import json
from itertools import product
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

import app.tasks.connectivity.service as connectivity_service
from app.core.db.utils import get_async_session_maker_from_engine
from app.tasks import hook_resolver
from app.tasks.connectivity.constants import (
    PROVISIONING_TIMEOUT,
)
from app.tasks.connectivity.models import (
    ConnectivityCheckWrite,
    ConnectivityServiceType,
)
from app.tasks.connectivity.service import (
    _cached_check_connectivity,
    _parse_check_result,
    check_connectivity,
    check_connectivity_with_cache,
    POLL_INTERVAL,
)
from app.tasks.crud import TaskHistoryLogManager, TaskHistoryManager, TaskManager
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.execution.models import BaseExecutor
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    Task,
    TaskBackendEnum,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
    TaskWrite,
)
from tests.app.factories import TaskFactory

MOCK_TASK_HISTORY_ID = 42
EXPECTED_INDEPENDENT_CALL_COUNT = 2
MIN_POLL_ITERATIONS = 2
# The sync call on which the ``run-script`` task reports ``StartedAt`` (earlier
# calls are the provisioning phase before the payload task starts).
CONNECT_START_CALL = 3
# ``_expire_and_fetch`` call sequence: 1 post-dispatch (RUNNING),
# 2 post-terminal-sync (SUCCESS, no logs), 3-4 ``_fetch_fresh_task_history``
# retries (empty, then populated).
FRESH_FETCH_POPULATE_CALL = 4


def test_connectivity_module_has_no_bare_nomad_step_literals() -> None:
    """Assert service.py spells Nomad step names through NomadStep, not literals.

    Asserts the absence of every step wire value rather than the presence of one
    enum reference: a presence check stays green when either call site reverts to
    a literal, which is the only regression this guard exists to catch.
    """
    source = Path(connectivity_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = sorted(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in set(NomadStep)
    )
    assert not literals, f"bare Nomad step literals in service.py: {literals}"


@pytest.fixture(autouse=True)
def _clear_connectivity_cache():
    """Clear the ``alru_cache`` before and after every test in this module."""
    _cached_check_connectivity.cache_clear()
    yield
    _cached_check_connectivity.cache_clear()


def _make_task(name: str = "run-python") -> MagicMock:
    """Build a mock Task with the given name."""
    task = MagicMock()
    task.id = 1
    task.name = name
    task.backend = "nomad"
    task.alert_on_fail = False
    return task


def _make_request(
    service_type: ConnectivityServiceType = ConnectivityServiceType.MYSQL,
    timeout: int = 30,
) -> ConnectivityCheckWrite:
    """Build a ConnectivityCheckWrite request."""
    return ConnectivityCheckWrite(
        target="node1",
        host="db-host",
        port=3306,
        service_type=service_type,
        timeout=timeout,
    )


def _make_task_history(
    task_history_status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.SUCCESS,
    stdout: str = "",
    stderr: str = "",
) -> TaskHistory:
    """Build a TaskHistory with in-memory logs."""
    task = _make_task()
    history = MagicMock()
    history.id = MOCK_TASK_HISTORY_ID
    history.status = task_history_status
    history.task = task

    logs = {}
    if stdout or stderr:
        log_data = {}
        if stdout:
            log_data[TaskLogType.STDOUT] = stdout
        if stderr:
            log_data[TaskLogType.STDERR] = stderr
        logs["run-script"] = log_data

    history.task_logs = logs

    def iter_logs_impl(_start_offsets=None, chunk_size=65536, step=None):
        task_logs = logs
        if step is not None:
            task_logs = {step: task_logs.get(step, {})}
        for (cur_step, log), log_type in product(task_logs.items(), TaskLogType):
            msg = log.get(log_type) or ""
            for chunk_start in range(0, len(msg), chunk_size):
                chunk_end = chunk_start + chunk_size
                yield TaskLog(
                    step=cur_step,
                    type=log_type,
                    msg=msg[chunk_start:chunk_end],
                    offset=chunk_end,
                )

    history.iter_logs = iter_logs_impl
    return history


class _AdvancingClock:
    """Provide a deterministic monotonic clock for the connectivity poll loop.

    Advances only when the loop awaits ``asyncio.sleep``, modelling wall time as
    the sum of the poll intervals slept -- so budget tests stay fast and
    deterministic instead of busy-looping against the real clock.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
class TestCheckConnectivityRealSession:
    """Exercise ``check_connectivity`` against a real ``AsyncSession``.

    ``TaskHistory.execution_request`` is declared ``deferred=True``
    (``app/tasks/models.py``), so a plain ``session.refresh(instance)`` leaves
    the attribute unloaded.
    If the executor's ``sync_task_history`` path then touches
    ``queue_item.execution_request`` from plain sync code, SQLAlchemy fires a
    lazy-load callable outside the async greenlet and raises ``MissingGreenlet``.
    These tests run the service against a real ``aiosqlite`` session so that
    class of bug remains observable.
    """

    @pytest.fixture(autouse=True)
    def advancing_clock(self):
        """Advance the poll loop's wall clock only when it sleeps.

        Patch both ``time.monotonic`` and ``asyncio.sleep`` so a poll iteration
        advances time by exactly its sleep interval, keeping budget-driven tests
        fast and deterministic.
        """
        clock = _AdvancingClock()
        with (
            patch(
                "app.tasks.connectivity.service.time.monotonic",
                new=clock.monotonic,
            ),
            patch(
                "app.tasks.connectivity.service.asyncio.sleep",
                new=clock.sleep,
            ),
        ):
            yield clock

    @pytest_asyncio.fixture
    async def run_python_task(self, session: AsyncSession) -> Task:
        """Persist a ``run-python`` task row in the test DB."""
        task_write = TaskWrite.model_validate(
            TaskFactory.build(
                name="run-python",
                backend=TaskBackendEnum.NOMAD,
                is_template=False,
                protected=False,
                alert_on_fail=False,
            )
        )
        return await TaskManager.create(session, task_write)

    @pytest_asyncio.fixture
    async def recorder_run_python_task(self, session: AsyncSession) -> Task:
        """Persist a ``run-python`` task row that declares a run-result recorder."""
        task_write = TaskWrite.model_validate(
            TaskFactory.build(
                name="run-python",
                backend=TaskBackendEnum.NOMAD,
                is_template=False,
                protected=False,
                alert_on_fail=False,
                run_result_recorder="app.extensions.apps.pkg:rec",
            )
        )
        return await TaskManager.create(session, task_write)

    @pytest_asyncio.fixture
    async def async_session_maker(self, session: AsyncSession):
        """Return a session-maker bound to the current test engine."""
        return get_async_session_maker_from_engine(session.bind)

    async def _real_dispatch_running(
        self,
        queue_item: TaskHistory,
        db: AsyncSession,
    ) -> TaskHistory:
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.execution_request.tracking.update(
            evaluation_id="eval-1",
            job_id="job-1",
        )
        saved = await TaskHistoryManager.save(
            db, queue_item, flag_modified_fields=["execution_request"]
        )
        await db.refresh(saved)
        return saved

    async def _append_log(
        self,
        writer_session: AsyncSession,
        task_history_id: int,
        stream: TaskLogType,
        message: str,
    ) -> None:
        payload = message.encode()
        await TaskHistoryLogWriter.append(
            writer_session,
            task_history_id,
            source="run-script",
            stream=stream,
            new_bytes=payload,
            force_flush=True,
            producer_offset_after=len(payload),
        )

    @staticmethod
    def _mark_run_script_started(queue_item: TaskHistory) -> None:
        """Simulate the ``run-script`` task reporting ``StartedAt``.

        Mirrors what the Nomad executor syncs into
        ``tracking["task_states"]`` once the payload task starts — the
        provisioning/connect boundary the poll loop keys off.
        """
        task_states = queue_item.execution_request.tracking.setdefault(
            "task_states", {}
        )
        task_states["run-script"] = {"StartedAt": "2026-06-26T00:00:00.000000000Z"}

    @pytest.mark.parametrize(
        "service_type",
        list(ConnectivityServiceType),
        ids=[st.value for st in ConnectivityServiceType],
    )
    async def test_success_for_each_service_type(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
        service_type: ConnectivityServiceType,
    ) -> None:
        """Verify successful connectivity checks for each service type."""
        request = _make_request(service_type=service_type, timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDOUT,
                json.dumps({"success": True}),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is True
        assert result.error is None
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=result.task_history_id
        )

    async def test_terminal_run_records_nothing(
        self,
        session: AsyncSession,
        recorder_run_python_task: Task,
        async_session_maker,
        mocker,
    ) -> None:
        """Skip recording for the probe's own run even when it reaches SUCCESS.

        The poll loop drives ``sync_task_history`` directly rather than through
        either sync seam, so a recorder declared on the shared ``run-python`` task
        must not observe it — the probe parses its own verdict.
        """
        recorded = []

        async def _recorder(db, history, result):
            recorded.append(result)

        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDOUT,
                json.dumps({"success": True}),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        mocker.patch.dict(
            hook_resolver._RESOLVED,
            {"app.extensions.apps.pkg:rec": _recorder},
            clear=True,
        )
        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
            patch(
                "app.tasks.run_result.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is True
        assert recorded == []

    async def test_unresolvable_payload_fails_terminally(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Assert an unresolvable payload yields a terminal FAILED history.

        The check builds ``queue_item`` with its ``task`` relationship attached to
        the caller ``session``. The payload gate must persist the FAILED row
        through that same session; persisting through a second one would raise
        ``InvalidRequestError`` and surface as an HTTP 500. Here the real
        ``dispatch_queue_item`` runs (unmocked) so that path stays observable.
        """
        request = _make_request(timeout=POLL_INTERVAL * 2)

        with (
            patch(
                "app.tasks.connectivity.service.PAYLOAD_PATH",
                "/nonexistent/connectivity_payload",
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "payload could not be resolved" in result.error
        saved = await TaskHistoryManager.get_or_404(session, id=result.task_history_id)
        assert saved.status == TaskHistoryStatusEnum.FAILED

    async def test_failure_connection_refused(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify a connection-refused payload maps to a failed response."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDOUT,
                json.dumps({"success": False, "error": "Connection refused"}),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error == "Connection refused"

    async def test_failure_task_failed_status(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify FAILED task status returns stderr as the response error."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDERR,
                "ImportError: No module named 'pymysql'",
            )
            queue_item.status = TaskHistoryStatusEnum.FAILED
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "No module named" in result.error

    async def test_timeout(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify RUNNING tasks time out when they never finish."""
        request = _make_request(timeout=POLL_INTERVAL)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    async def test_malformed_stdout(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify malformed stdout maps to a parse error response."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDOUT,
                "not valid json",
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error == "Failed to parse connectivity check output"

    async def test_failure_logs_arrive_after_status_transition(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify FAILED status still returns stderr written after transition."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.FAILED
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        original_expire_and_fetch = connectivity_service._expire_and_fetch
        logs_written = {"done": False}

        async def delayed_log_expire_and_fetch(db: AsyncSession, task_history_id: int):
            fetched = await original_expire_and_fetch(db, task_history_id)
            if (
                fetched.status == TaskHistoryStatusEnum.FAILED
                and not logs_written["done"]
            ):
                async with async_session_maker() as writer_session:
                    await self._append_log(
                        writer_session,
                        task_history_id,
                        TaskLogType.STDERR,
                        "Traceback: JSONDecodeError: Expecting value",
                    )
                logs_written["done"] = True
            return fetched

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
            patch(
                "app.tasks.connectivity.service._expire_and_fetch",
                side_effect=delayed_log_expire_and_fetch,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "JSONDecodeError" in result.error

    async def test_success_logs_arrive_after_status_transition(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify SUCCESS status still parses stdout written after transition."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        original_expire_and_fetch = connectivity_service._expire_and_fetch
        logs_written = {"done": False}

        async def delayed_log_expire_and_fetch(db: AsyncSession, task_history_id: int):
            fetched = await original_expire_and_fetch(db, task_history_id)
            if (
                fetched.status == TaskHistoryStatusEnum.SUCCESS
                and not logs_written["done"]
            ):
                async with async_session_maker() as writer_session:
                    await self._append_log(
                        writer_session,
                        task_history_id,
                        TaskLogType.STDOUT,
                        json.dumps({"success": True}),
                    )
                logs_written["done"] = True
            return fetched

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
            patch(
                "app.tasks.connectivity.service._expire_and_fetch",
                side_effect=delayed_log_expire_and_fetch,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is True
        assert result.error is None

    async def test_fresh_fetch_retries_until_logs_are_populated(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify fresh-fetch retries until post-sync logs are persisted."""
        request = _make_request(timeout=POLL_INTERVAL * 2)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        original_expire_and_fetch = connectivity_service._expire_and_fetch
        expire_calls = {"n": 0}

        async def delayed_population_expire_and_fetch(
            db: AsyncSession, task_history_id: int
        ):
            expire_calls["n"] += 1
            if expire_calls["n"] == FRESH_FETCH_POPULATE_CALL:
                async with async_session_maker() as writer_session:
                    await self._append_log(
                        writer_session,
                        task_history_id,
                        TaskLogType.STDOUT,
                        json.dumps({"success": True}),
                    )
            return await original_expire_and_fetch(db, task_history_id)

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
            patch(
                "app.tasks.connectivity.service._expire_and_fetch",
                side_effect=delayed_population_expire_and_fetch,
            ),
        ):
            result = await check_connectivity(session, request)

        assert expire_calls["n"] >= FRESH_FETCH_POPULATE_CALL
        assert result.success is True
        assert result.error is None

    async def test_task_stays_pending(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify PENDING tasks time out when no transition occurs."""
        request = _make_request(timeout=POLL_INTERVAL)

        async def pending_dispatch(
            queue_item: TaskHistory, db: AsyncSession
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.PENDING
            saved = await TaskHistoryManager.save(
                db, queue_item, flag_modified_fields=["execution_request"]
            )
            await db.refresh(saved)
            return saved

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=pending_dispatch,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    async def test_deferred_execution_request_survives_sync_executor(
        self,
        session: AsyncSession,
        run_python_task: Task,
    ) -> None:
        """Verify the polling loop survives the deferred-column lazy-load race.

        Reproduces the runtime bug surfaced by e2e QA: after the
        production ``dispatch_queue_item`` commits and refreshes the fresh
        ``TaskHistory``, the ``execution_request`` attribute is **unloaded**
        because of the deferred ``column_property``. The very first polling
        iteration then hands the stale ORM instance to
        ``NomadExecutor._sync_task_history`` → ``get_allocation_for_task_history``,
        which synchronously reads ``queue_item.execution_request.tracking``
        and triggers ``MissingGreenlet`` against the async ``aiosqlite``
        driver.

        The mock executor here replicates that sync attribute touch. The
        test must not raise ``MissingGreenlet`` and must return a
        ``ConnectivityCheckResponse`` derived from the re-fetched DB row.
        """
        request = ConnectivityCheckWrite(
            target="node1",
            host="db-host",
            port=3306,
            service_type=ConnectivityServiceType.MYSQL,
            timeout=POLL_INTERVAL,
        )

        captured_history_id = []

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            # Simulates NomadExecutor._sync_task_history reading tracking
            # from plain sync code within the async call chain — this is
            # where the lazy-load raises MissingGreenlet pre-fix.
            _ = queue_item.execution_request.tracking.get("allocation_id")
            captured_history_id.append(queue_item.id)
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            queue_item.execution_request.tracking["allocation_id"] = "alloc-1"
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
        ):
            result = await check_connectivity(session, request)

        assert captured_history_id, "sync_task_history was never invoked"
        assert result.task_history_id == captured_history_id[0]
        assert result.success is False
        assert result.error == "Failed to parse connectivity check output"

    async def test_connectivity_success_persists_run_script_logs(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Assert log persistence receives a writer session during polling.

        ``check_connectivity`` calls ``executor.sync_task_history`` inside its
        polling loop; the Nomad executor persists ``run-script`` stdout into
        ``taskhistory_log`` only when a ``writer_session`` is supplied. Without
        a writer session no log chunks land, ``_parse_check_result`` reads an
        empty stdout, and ``json.loads("")`` falls through to the generic
        ``Failed to parse connectivity check output`` error — regardless of
        what the underlying task actually produced.

        The fake executor here writes a ``{"success": true}`` stdout chunk
        through the caller-supplied ``writer_session`` on the second polling
        iteration, then flips the queue item to ``SUCCESS``. The test asserts
        the chunk landed in ``taskhistory_log`` and the parsed response is
        ``success=True``.
        """
        request = ConnectivityCheckWrite(
            target="node1",
            host="db-host",
            port=3306,
            service_type=ConnectivityServiceType.MYSQL,
            timeout=POLL_INTERVAL * 4,
        )

        stdout_bytes = b'{"success": true}'
        call_count = {"n": 0}

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            call_count["n"] += 1
            assert writer_session is not None, (
                "check_connectivity must supply a writer_session"
            )
            if call_count["n"] == 1:
                return queue_item, None
            await TaskHistoryLogWriter.append(
                writer_session,
                queue_item.id,
                source="run-script",
                stream=TaskLogType.STDOUT,
                new_bytes=stdout_bytes,
                force_flush=True,
                producer_offset_after=len(stdout_bytes),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert call_count["n"] >= MIN_POLL_ITERATIONS, (
            "polling loop must iterate at least twice"
        )
        assert result.success is True
        assert result.error is None
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=result.task_history_id
        )

    async def test_provisioning_phase_does_not_consume_connect_budget(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify provisioning time is not charged against the connect budget.

        Provisioning latency (Nomad dispatch, ``run-python`` scheduling,
        ``prepare-env`` dependency install) must not count against the DB
        connect budget, otherwise a slow provision false-negatives the check
        even when the DB is reachable. Because Nomad reports ``RUNNING`` from
        dispatch onward, the boundary is the ``run-script`` task's
        ``StartedAt``: here the task is RUNNING throughout but ``StartedAt``
        only appears after several provisioning polls — more than the
        single-poll connect budget (``request.timeout``). The check must still
        succeed because those pre-start polls are charged to the provisioning
        budget, not the connect budget.
        """
        request = _make_request(timeout=POLL_INTERVAL)

        call_count = {"n": 0}

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            call_count["n"] += 1
            if call_count["n"] < CONNECT_START_CALL:
                # Provisioning: RUNNING, but the connect phase has not started.
                queue_item.status = TaskHistoryStatusEnum.RUNNING
            elif call_count["n"] == CONNECT_START_CALL:
                self._mark_run_script_started(queue_item)
                queue_item.status = TaskHistoryStatusEnum.RUNNING
            else:
                await self._append_log(
                    writer_session,
                    queue_item.id,
                    TaskLogType.STDOUT,
                    json.dumps({"success": True}),
                )
                queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is True
        assert result.error is None

    async def test_provisioning_timeout_has_distinct_message(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify a task that never reaches the connect phase times out on provisioning.

        When the ``run-script`` task never reports ``StartedAt`` (stuck
        provisioning) the wait is bounded by ``PROVISIONING_TIMEOUT`` (not the
        connect budget), and the timeout message must distinguish a
        provisioning timeout from a connect timeout so operators can tell
        provisioning latency apart from an unreachable DB.
        """
        request = _make_request(timeout=POLL_INTERVAL)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            # Stays RUNNING and the run-script task never starts.
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error
        assert str(PROVISIONING_TIMEOUT) in result.error
        assert "provision" in result.error.lower()

    async def test_connect_timeout_has_distinct_message(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify a stalled connect (post-marker) times out on the connect budget.

        Once the ``run-script`` task reports ``StartedAt`` the wait is bounded
        by the connect budget (``request.timeout``), and the message must be
        the connect-timeout message rather than the provisioning one.
        """
        request = _make_request(timeout=POLL_INTERVAL)

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            self._mark_run_script_started(queue_item)
            # Connect phase started but never completes.
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert f"timed out after {request.timeout}s" in result.error
        assert "provision" not in result.error.lower()

    async def test_connect_budget_charges_non_sleep_wall_time(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
        advancing_clock: "_AdvancingClock",
    ) -> None:
        """Verify the connect budget is charged for wall time spent off ``sleep``.

        The first ``sync_task_history`` burns the whole connect budget without
        sleeping and would flip the task to SUCCESS on its *second* call. A
        wall-clock budget breaks at the next top-of-loop check -- so the check
        times out and ``sync_task_history`` runs exactly once; a sleep-only
        budget would never expire and observe the second-call SUCCESS instead.
        """
        # timeout >> POLL_INTERVAL so a sleep-only budget would need ~30 polls to
        # expire, while the non-sleep callback time overruns the budget at once.
        request = _make_request(timeout=POLL_INTERVAL * 30)

        call_count = {"n": 0}

        async def started_dispatch(
            queue_item: TaskHistory, db: AsyncSession
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.RUNNING
            queue_item.execution_request.tracking.update(
                evaluation_id="eval-1",
                job_id="job-1",
            )
            self._mark_run_script_started(queue_item)
            saved = await TaskHistoryManager.save(
                db, queue_item, flag_modified_fields=["execution_request"]
            )
            await db.refresh(saved)
            return saved

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Burn more than the entire connect budget in non-sleep wall
                # time (Nomad round-trip + DB commits), staying RUNNING.
                advancing_clock.now += request.timeout + POLL_INTERVAL
                return queue_item, None
            # A sleep-only budget would reach here and succeed; a wall-clock
            # budget must have already broken out on the timeout.
            await self._append_log(
                writer_session,
                queue_item.id,
                TaskLogType.STDOUT,
                json.dumps({"success": True}),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=started_dispatch,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert call_count["n"] == 1, (
            "wall-clock budget must break after the single non-sleep-heavy poll, "
            "before the second-call SUCCESS flip"
        )
        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error
        assert f"timed out after {request.timeout}s" in result.error

    async def test_timeout_surfaces_partial_run_script_logs(
        self,
        session: AsyncSession,
        run_python_task: Task,
        async_session_maker,
    ) -> None:
        """Verify the timeout branch surfaces captured run-script output.

        The timeout branch must include any run-script output already captured
        in the error (rather than a bare ``timed out`` message with no
        diagnostic detail).
        """
        request = _make_request(timeout=POLL_INTERVAL)
        partial_output = "connecting to db-host:3306 ... still waiting"

        call_count = {"n": 0}

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            if call_count["n"] == 0:
                self._mark_run_script_started(queue_item)
                await self._append_log(
                    writer_session,
                    queue_item.id,
                    TaskLogType.STDERR,
                    partial_output,
                )
            call_count["n"] += 1
            # Never finishes — stays RUNNING until the connect budget expires.
            return queue_item, None

        mock_executor = MagicMock(spec=BaseExecutor)
        mock_executor.sync_task_history = sync_task_history

        with (
            patch(
                "app.tasks.connectivity.service.dispatch_queue_item",
                side_effect=self._real_dispatch_running,
            ),
            patch(
                "app.tasks.connectivity.service.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.service.get_async_session_maker",
                return_value=async_session_maker,
            ),
        ):
            result = await check_connectivity(session, request)

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error
        assert partial_output in result.error
        assert result.task_history_id is not None


@pytest.mark.asyncio
class TestParseCheckResult:
    """Test the _parse_check_result helper."""

    # NOTE: these tests intentionally keep a mocked session because
    # ``_parse_check_result`` only inspects ``TaskHistory`` logs and does not
    # call session-bound helpers like TaskHistoryManager.save/get_or_404.

    async def test_success_result(self):
        """Verify successful result parsed from stdout JSON."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.SUCCESS,
            stdout=json.dumps({"success": True}),
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is True
        assert result.error is None

    async def test_failure_result_with_error(self):
        """Verify failure result with error message from stdout JSON."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.SUCCESS,
            stdout=json.dumps({"success": False, "error": "Access denied"}),
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is False
        assert result.error == "Access denied"

    async def test_failed_status_with_stderr(self):
        """Verify stderr is returned as error when task status is FAILED."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.FAILED,
            stderr="Traceback: module not found",
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is False
        assert result.error is not None
        assert "module not found" in result.error

    async def test_failed_status_without_stderr(self):
        """Verify default error message when FAILED task has no stderr."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.FAILED,
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is False
        assert result.error == "Task failed"

    async def test_empty_stdout(self):
        """Verify parse error when stdout is empty."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.SUCCESS,
            stdout="",
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is False
        assert result.error == "Failed to parse connectivity check output"

    async def test_malformed_json(self):
        """Verify parse error when stdout contains invalid JSON."""
        history = _make_task_history(
            task_history_status=TaskHistoryStatusEnum.SUCCESS,
            stdout="{{invalid json",
        )
        result = await _parse_check_result(AsyncMock(spec=AsyncSession), history)
        assert result.success is False
        assert result.error == "Failed to parse connectivity check output"


class TestCheckConnectivityWithCache:
    """Test :func:`check_connectivity_with_cache` caching behaviour."""

    @pytest.mark.asyncio
    async def test_second_call_is_cached(self):
        """Verify a second identical call short-circuits via ``alru_cache``."""
        session = MagicMock(spec=AsyncSession)
        mock_response = MagicMock(success=True, error=None)
        with patch(
            "app.tasks.connectivity.service.check_connectivity",
            new=AsyncMock(return_value=mock_response),
        ) as mock_check:
            first = await check_connectivity_with_cache(
                session,
                target="node1",
                host="10.0.0.1",
                port=3306,
                service_type=ConnectivityServiceType.MYSQL,
            )
            second = await check_connectivity_with_cache(
                session,
                target="node1",
                host="10.0.0.1",
                port=3306,
                service_type=ConnectivityServiceType.MYSQL,
            )

        assert first == (True, None)
        assert second == (True, None)
        assert mock_check.await_count == 1

    @pytest.mark.asyncio
    async def test_different_keys_are_independent(self):
        """Verify entries are keyed by ``(target, host, port, service_type)``."""
        session = MagicMock(spec=AsyncSession)
        responses = [
            MagicMock(success=True, error=None),
            MagicMock(success=False, error="timeout"),
        ]
        with patch(
            "app.tasks.connectivity.service.check_connectivity",
            new=AsyncMock(side_effect=responses),
        ) as mock_check:
            first = await check_connectivity_with_cache(
                session,
                target="node1",
                host="10.0.0.1",
                port=3306,
                service_type=ConnectivityServiceType.MYSQL,
            )
            second = await check_connectivity_with_cache(
                session,
                target="node2",
                host="10.0.0.2",
                port=5432,
                service_type=ConnectivityServiceType.POSTGRESQL,
            )

        assert first == (True, None)
        assert second == (False, "timeout")
        assert mock_check.await_count == EXPECTED_INDEPENDENT_CALL_COUNT
