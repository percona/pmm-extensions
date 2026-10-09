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

"""Test the connectivity check route endpoint."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.db.utils import get_async_session_maker_from_engine
from app.tasks.connectivity.models import (
    ConnectivityCheckResponse,
    ConnectivityServiceType,
)
from app.tasks.connectivity.service import _cached_check_connectivity, POLL_INTERVAL
from app.tasks.crud import TaskHistoryLogManager, TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.models import BaseExecutor
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.main import tasks_app
from app.tasks.models import (
    Task,
    TaskBackendEnum,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.factories import TaskFactory

MOCK_TASK_HISTORY_ID = 42
MIN_POLL_ITERATIONS = 2
#: ``sync_task_history`` call on which the fake executor reports the
#: ``run-script`` task's ``StartedAt``. Chosen so the connect phase begins only
#: after several provisioning polls have elapsed — more than the small connect
#: budget the facet-(a) test grants — proving provisioning time is not charged
#: to it.
CONNECT_START_POLL = 4


def _mark_run_script_started(queue_item: TaskHistory) -> None:
    """Simulate the ``run-script`` task reporting ``StartedAt``.

    Mirrors what the Nomad executor syncs into ``tracking["task_states"]`` once
    the payload task starts — the provisioning/connect boundary the poll loop
    keys off, in place of the removed stderr marker.
    """
    task_states = queue_item.execution_request.tracking.setdefault("task_states", {})
    task_states["run-script"] = {"StartedAt": "2026-06-26T00:00:00.000000000Z"}


@pytest.fixture(autouse=True)
def _clear_connectivity_cache():
    """Clear the ``alru_cache`` before and after every test in this module."""
    _cached_check_connectivity.cache_clear()
    yield
    _cached_check_connectivity.cache_clear()


@pytest.fixture
def mock_executor() -> MagicMock:
    """Return a mock executor with node1 available."""
    executor = MagicMock(spec=BaseExecutor)
    executor.get_hosts = AsyncMock(return_value={"node1": "10.0.0.1"})
    return executor


@pytest.fixture
def test_client(regular_user, mock_executor) -> Iterator[TestClient]:
    """Create an authenticated test client for the Tasks API."""
    session = AsyncMock()
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = lambda: (
        None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


class TestConnectivityCheckEndpoint:
    """Test POST /connectivity-check/ endpoint."""

    def test_success(self, test_client, mock_executor):
        """Verify successful connectivity check returns 200."""
        mock_task = MagicMock(spec=Task)
        mock_task.id = 1
        mock_task.name = "run-python"
        expected_response = ConnectivityCheckResponse(
            success=True, error=None, task_history_id=MOCK_TASK_HISTORY_ID
        )

        with (
            patch(
                "app.tasks.connectivity.routes.get_executable_task_by_name",
                new=AsyncMock(return_value=mock_task),
            ),
            patch(
                "app.tasks.connectivity.routes.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.connectivity.routes.check_connectivity",
                new=AsyncMock(return_value=expected_response),
            ),
        ):
            response = test_client.post(
                "/connectivity-check/",
                json={
                    "target": "node1",
                    "host": "db-host",
                    "port": 3306,
                    "service_type": ConnectivityServiceType.MYSQL.value,
                },
            )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["success"] is True
        assert data["task_history_id"] == MOCK_TASK_HISTORY_ID

    def test_invalid_target_returns_400(self, test_client, mock_executor):
        """Verify 400 when target is not in available Nomad hosts.

        The error detail must disclose the looked-up target verbatim and the
        registered-target count so a future inventory-vs-executor name
        mismatch is diagnosable from the error alone.
        """
        mock_task = MagicMock(spec=Task)
        mock_task.id = 1
        mock_task.name = "run-python"

        with (
            patch(
                "app.tasks.connectivity.routes.get_executable_task_by_name",
                new=AsyncMock(return_value=mock_task),
            ),
            patch(
                "app.tasks.connectivity.routes.get_executor_for_task",
                return_value=mock_executor,
            ),
        ):
            response = test_client.post(
                "/connectivity-check/",
                json={
                    "target": "unknown-node",
                    "host": "db-host",
                    "port": 3306,
                    "service_type": ConnectivityServiceType.MYSQL.value,
                },
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        detail = response.json()["detail"]
        assert "'unknown-node'" in detail
        assert "registered targets: 1" in detail
        assert "executor node name" in detail

    def test_invalid_service_type_returns_422(self, test_client):
        """Verify 422 when an unsupported service_type is provided."""
        response = test_client.post(
            "/connectivity-check/",
            json={
                "target": "node1",
                "host": "db-host",
                "port": 3306,
                "service_type": "REDIS",
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_invalid_timeout_returns_422(self, test_client):
        """Verify 422 when timeout exceeds the maximum."""
        response = test_client.post(
            "/connectivity-check/",
            json={
                "target": "node1",
                "host": "db-host",
                "port": 3306,
                "service_type": ConnectivityServiceType.MYSQL.value,
                "timeout": 120,
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_zero_timeout_returns_422(self, test_client):
        """Verify 422 when timeout is zero."""
        response = test_client.post(
            "/connectivity-check/",
            json={
                "target": "node1",
                "host": "db-host",
                "port": 3306,
                "service_type": ConnectivityServiceType.MYSQL.value,
                "timeout": 0,
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_unauthenticated_returns_401(self):
        """Verify 401 when no authentication is provided."""
        tasks_app.dependency_overrides = {}
        client = TestClient(tasks_app, raise_server_exceptions=False)
        response = client.post(
            "/connectivity-check/",
            json={
                "target": "node1",
                "host": "db-host",
                "port": 3306,
                "service_type": ConnectivityServiceType.MYSQL.value,
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
class TestConnectivityCheckEndpointRealSession:
    """Integration coverage for ``POST /connectivity-check/`` with a real session.

    The tests in :class:`TestConnectivityCheckEndpoint` above patch
    ``app.tasks.connectivity.routes.check_connectivity`` itself, so the
    service-layer writer-session wiring is never exercised from the HTTP
    boundary. As a regression guard, a real HTTP POST must run the real
    ``check_connectivity`` polling loop and pick up the ``run-script`` stdout
    chunk persisted by the executor's writer session.
    """

    async def test_success_runs_real_service_path(
        self,
        regular_user,
        session: AsyncSession,
        mock_executor: MagicMock,
    ):
        """Verify POST /connectivity-check/ returns success end-to-end.

        Persist a ``run-python`` task row, install a real async session as
        the ``get_session`` override, and run the real ``check_connectivity``
        code path. The fake executor writes a ``{"success": true}`` stdout
        chunk through the supplied ``writer_session`` on the second poll
        iteration and flips the queue item to SUCCESS. The response must
        parse the chunk as ``success=True`` rather than the generic
        parse-error path.
        """
        test_session_maker = get_async_session_maker_from_engine(session.bind)

        task_write = TaskWrite.model_validate(
            TaskFactory.build(
                name="run-python",
                backend=TaskBackendEnum.NOMAD,
                is_template=False,
                protected=False,
                alert_on_fail=False,
            )
        )
        await TaskManager.create(session, task_write)

        tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
            lambda: None
        )
        tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
        tasks_app.dependency_overrides[get_session] = lambda: session
        tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor

        stdout_bytes = b'{"success": true}'
        call_count = {"n": 0}

        async def real_dispatch(
            queue_item: TaskHistory, db: AsyncSession
        ) -> TaskHistory:
            queue_item.status = TaskHistoryStatusEnum.RUNNING
            queue_item.execution_request.tracking.update(
                evaluation_id="eval-1", job_id="job-1"
            )
            saved = await TaskHistoryManager.save(
                db, queue_item, flag_modified_fields=["execution_request"]
            )
            await db.refresh(saved)
            return saved

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            call_count["n"] += 1
            assert writer_session is not None
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

        fake_service_executor = MagicMock(spec=BaseExecutor)
        fake_service_executor.sync_task_history = sync_task_history

        try:
            with (
                patch(
                    "app.tasks.connectivity.routes.get_executor_for_task",
                    return_value=mock_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.dispatch_queue_item",
                    side_effect=real_dispatch,
                ),
                patch(
                    "app.tasks.connectivity.service.get_executor_for_task",
                    return_value=fake_service_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.get_async_session_maker",
                    return_value=test_session_maker,
                ),
                patch("app.tasks.connectivity.service.asyncio.sleep", new=AsyncMock()),
            ):
                transport = ASGITransport(app=tasks_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/connectivity-check/",
                        json={
                            "target": "node1",
                            "host": "db-host",
                            "port": 3306,
                            "service_type": ConnectivityServiceType.MYSQL.value,
                            "timeout": POLL_INTERVAL * 4,
                        },
                    )
        finally:
            tasks_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["success"] is True
        assert data["error"] is None
        assert call_count["n"] >= MIN_POLL_ITERATIONS
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=data["task_history_id"]
        )

    async def test_provisioning_latency_does_not_false_negative_over_http(
        self,
        regular_user,
        session: AsyncSession,
        mock_executor: MagicMock,
    ):
        """Verify a slow-to-provision but reachable DB returns success over HTTP.

        The two-phase budget regression, exercised end-to-end through the route:
        the fake executor holds the task RUNNING across several provisioning
        polls before the ``run-script`` task reports ``StartedAt``, then flushes
        a ``{"success": true}`` stdout chunk. The POST grants a deliberately
        small connect budget (``POLL_INTERVAL * 2``) that the provisioning polls
        exceed. A pre-fix single-budget loop would have timed out before the
        connect even started; the decoupled budget must still return
        ``success=True``.
        """
        test_session_maker = get_async_session_maker_from_engine(session.bind)

        task_write = TaskWrite.model_validate(
            TaskFactory.build(
                name="run-python",
                backend=TaskBackendEnum.NOMAD,
                is_template=False,
                protected=False,
                alert_on_fail=False,
            )
        )
        await TaskManager.create(session, task_write)

        tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
            lambda: None
        )
        tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
        tasks_app.dependency_overrides[get_session] = lambda: session
        tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor

        stdout_bytes = b'{"success": true}'
        connect_budget = POLL_INTERVAL * 2
        call_count = {"n": 0}

        async def real_dispatch(
            queue_item: TaskHistory, db: AsyncSession
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.RUNNING
            queue_item.execution_request.tracking.update(
                evaluation_id="eval-1", job_id="job-1"
            )
            saved = await TaskHistoryManager.save(
                db, queue_item, flag_modified_fields=["execution_request"]
            )
            await db.refresh(saved)
            return saved

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            call_count["n"] += 1
            assert writer_session is not None
            n = call_count["n"]
            if n < CONNECT_START_POLL:
                # Provisioning: still RUNNING, run-script task not yet started.
                return queue_item, None
            if n == CONNECT_START_POLL:
                _mark_run_script_started(queue_item)
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

        fake_service_executor = MagicMock(spec=BaseExecutor)
        fake_service_executor.sync_task_history = sync_task_history

        try:
            with (
                patch(
                    "app.tasks.connectivity.routes.get_executor_for_task",
                    return_value=mock_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.dispatch_queue_item",
                    side_effect=real_dispatch,
                ),
                patch(
                    "app.tasks.connectivity.service.get_executor_for_task",
                    return_value=fake_service_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.get_async_session_maker",
                    return_value=test_session_maker,
                ),
                patch("app.tasks.connectivity.service.asyncio.sleep", new=AsyncMock()),
            ):
                transport = ASGITransport(app=tasks_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/connectivity-check/",
                        json={
                            "target": "node1",
                            "host": "db-host",
                            "port": 3306,
                            "service_type": ConnectivityServiceType.MYSQL.value,
                            "timeout": connect_budget,
                        },
                    )
        finally:
            tasks_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["success"] is True
        assert data["error"] is None
        # Provisioning spanned more polls than the connect budget alone permits,
        # yet the check still succeeded — the budgets are independent.
        assert call_count["n"] > connect_budget // POLL_INTERVAL
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=data["task_history_id"]
        )

    async def test_timeout_surfaces_partial_logs_and_id_over_http(
        self,
        regular_user,
        session: AsyncSession,
        mock_executor: MagicMock,
    ):
        """Verify a timed-out check surfaces partial run-script output over HTTP.

        End-to-end coverage of the diagnostics fix: the fake executor reports
        ``StartedAt`` and writes a partial run-script chunk, then never finishes,
        exhausting the connect budget. The response must carry ``success=False``,
        the captured ``installing deps...`` output, and the ``task_history_id``
        whose persisted log the GUI links — the path that previously discarded
        the captured output.
        """
        test_session_maker = get_async_session_maker_from_engine(session.bind)

        task_write = TaskWrite.model_validate(
            TaskFactory.build(
                name="run-python",
                backend=TaskBackendEnum.NOMAD,
                is_template=False,
                protected=False,
                alert_on_fail=False,
            )
        )
        await TaskManager.create(session, task_write)

        tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
            lambda: None
        )
        tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
        tasks_app.dependency_overrides[get_session] = lambda: session
        tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor

        partial_bytes = b"installing deps...\n"
        call_count = {"n": 0}

        async def real_dispatch(
            queue_item: TaskHistory, db: AsyncSession
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.RUNNING
            queue_item.execution_request.tracking.update(
                evaluation_id="eval-1", job_id="job-1"
            )
            saved = await TaskHistoryManager.save(
                db, queue_item, flag_modified_fields=["execution_request"]
            )
            await db.refresh(saved)
            return saved

        async def sync_task_history(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            call_count["n"] += 1
            assert writer_session is not None
            if call_count["n"] == 1:
                _mark_run_script_started(queue_item)
                await TaskHistoryLogWriter.append(
                    writer_session,
                    queue_item.id,
                    source="run-script",
                    stream=TaskLogType.STDERR,
                    new_bytes=partial_bytes,
                    force_flush=True,
                    producer_offset_after=len(partial_bytes),
                )
            # Never flip the status: the connect budget exhausts and times out.
            return queue_item, None

        fake_service_executor = MagicMock(spec=BaseExecutor)
        fake_service_executor.sync_task_history = sync_task_history

        try:
            with (
                patch(
                    "app.tasks.connectivity.routes.get_executor_for_task",
                    return_value=mock_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.dispatch_queue_item",
                    side_effect=real_dispatch,
                ),
                patch(
                    "app.tasks.connectivity.service.get_executor_for_task",
                    return_value=fake_service_executor,
                ),
                patch(
                    "app.tasks.connectivity.service.get_async_session_maker",
                    return_value=test_session_maker,
                ),
                patch("app.tasks.connectivity.service.asyncio.sleep", new=AsyncMock()),
            ):
                transport = ASGITransport(app=tasks_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/connectivity-check/",
                        json={
                            "target": "node1",
                            "host": "db-host",
                            "port": 3306,
                            "service_type": ConnectivityServiceType.MYSQL.value,
                            "timeout": POLL_INTERVAL * 2,
                        },
                    )
        finally:
            tasks_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["success"] is False
        assert "timed out" in data["error"]
        assert "installing deps..." in data["error"]
        assert data["task_history_id"] is not None
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=data["task_history_id"]
        )
