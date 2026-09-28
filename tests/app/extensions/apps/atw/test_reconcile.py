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

"""Tests for the sweep that fills in run outcomes the recorder never observed."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.exceptions import (
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
)
from app.core.requests import RemoteAPI
from app.core.utils.date_time import utc_now
from app.extensions.apps.atw.crud import AtwIncidentExecutionManager, AtwIncidentManager
from app.extensions.apps.atw.models import AtwIncident, AtwIncidentExecution
from app.extensions.apps.atw.reconcile import reconcile_executions
from app.tasks.models import TaskHistoryStatusEnum

_HISTORY_ID = 501
_BATCH_SIZE = 10
_TWO_ROWS = 2


@pytest.fixture
def tasks_api(mocker: MockerFixture, session: AsyncSession) -> AsyncMock:
    """Point the sweep at a mock Tasks client and the test's in-memory session."""
    api = AsyncMock(spec=RemoteAPI)
    client = MagicMock()
    client.auth.return_value.__enter__.return_value = api
    mocker.patch(
        "app.extensions.apps.atw.reconcile.get_tasks_api",
        new=AsyncMock(return_value=client),
    )
    mocker.patch(
        "app.extensions.apps.atw.reconcile.get_internal_token", return_value="token"
    )
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "app.extensions.apps.atw.reconcile.get_async_session_maker", return_value=maker
    )
    return api


def _upstream_run(
    status_value: TaskHistoryStatusEnum = TaskHistoryStatusEnum.FAILED,
) -> dict[str, Any]:
    """Build an upstream task-history payload reporting ``status_value``."""
    return {
        "id": _HISTORY_ID,
        "status": status_value.value,
        "finished_at": utc_now().isoformat(),
    }


async def _seed_execution(
    session: AsyncSession,
    *,
    task_history_id: int = _HISTORY_ID,
    outcome_unrecoverable: bool = False,
    terminal_status: TaskHistoryStatusEnum | None = None,
    created_at: datetime | None = None,
) -> AtwIncidentExecution:
    """Seed one execution row the sweep may or may not be expected to pick up."""
    incident = await AtwIncidentManager.save(session, AtwIncident(created_by="alice"))
    execution = AtwIncidentExecution(
        incident_id=incident.id,
        task_history_id=task_history_id,
        snippet_filename="diag.sh",
        outcome_unrecoverable=outcome_unrecoverable,
        terminal_status=None if terminal_status is None else terminal_status.value,
    )
    if created_at is not None:
        execution.created_at = created_at
    return await AtwIncidentExecutionManager.save(session, execution)


class TestResolvesOutcomes:
    """Check what the sweep writes when the upstream answers."""

    @pytest.mark.asyncio
    async def test_terminal_run_is_recorded(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a finished run's status and completion time reach the row."""
        execution = await _seed_execution(session)
        tasks_api.get.return_value = _upstream_run()

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.terminal_status == TaskHistoryStatusEnum.FAILED.value
        assert execution.finished_at is not None
        assert execution.reconcile_attempted_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status_value",
        [status for status in TaskHistoryStatusEnum if status.is_terminal()],
    )
    async def test_every_terminal_status_reaches_the_row(
        self,
        session: AsyncSession,
        tasks_api: AsyncMock,
        status_value: TaskHistoryStatusEnum,
    ) -> None:
        """Ensure the sweep records an outcome whatever path drove the run terminal.

        The recorder hook is documented not to observe three transitions — a run
        stopped via the stop route, one that failed before dispatch, and one the
        connectivity probe drives terminal. The sweep reads whatever status upstream
        reports rather than inferring the path, so covering every terminal status is
        what covers all three.
        """
        execution = await _seed_execution(session)
        tasks_api.get.return_value = _upstream_run(status_value)

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.terminal_status == status_value.value

    @pytest.mark.asyncio
    async def test_an_ancient_execution_is_still_reconciled(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure no time cutoff excludes a row that predates the recorder.

        A cutoff would leave older incidents showing an authoritative-looking zero,
        which is the wrong answer for the screen this feature exists to serve.
        """
        execution = await _seed_execution(
            session, created_at=utc_now() - timedelta(days=400)
        )
        tasks_api.get.return_value = _upstream_run()

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.terminal_status == TaskHistoryStatusEnum.FAILED.value

    @pytest.mark.asyncio
    async def test_non_terminal_run_is_left_unresolved(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a still-running run keeps no outcome but yields its batch slot."""
        execution = await _seed_execution(session)
        tasks_api.get.return_value = _upstream_run(TaskHistoryStatusEnum.RUNNING)

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.terminal_status is None
        assert execution.outcome_unrecoverable is False
        assert execution.reconcile_attempted_at is not None

    @pytest.mark.asyncio
    async def test_unreadable_status_is_left_unresolved(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure an unrecognized status is rejected rather than stored verbatim."""
        execution = await _seed_execution(session)
        tasks_api.get.return_value = {"id": _HISTORY_ID, "status": "teleported"}

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.terminal_status is None
        assert execution.reconcile_attempted_at is not None


class TestFailureSurface:
    """Check the 404-versus-everything-else distinction the sweep rests on."""

    @pytest.mark.asyncio
    async def test_vanished_history_is_retired_permanently(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a genuinely absent upstream row is marked unrecoverable."""
        execution = await _seed_execution(session)
        tasks_api.get.side_effect = HTTPNotFoundException("gone")

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.outcome_unrecoverable is True
        assert execution.terminal_status is None

    @pytest.mark.asyncio
    async def test_retired_row_is_never_requeried(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a later tick does not fetch a row already known unrecoverable."""
        await _seed_execution(session, outcome_unrecoverable=True)

        await reconcile_executions(_BATCH_SIZE)

        tasks_api.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolved_row_is_never_requeried(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a row whose outcome is already recorded is not fetched again."""
        await _seed_execution(session, terminal_status=TaskHistoryStatusEnum.SUCCESS)

        await reconcile_executions(_BATCH_SIZE)

        tasks_api.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_upstream_failure_leaves_the_row_retryable(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a 503 is not mistaken for absence, which would retire a live run."""
        execution = await _seed_execution(session)
        tasks_api.get.side_effect = HTTPServiceUnavailableException("try later")

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.outcome_unrecoverable is False
        assert execution.terminal_status is None
        assert execution.reconcile_attempted_at is not None

    @pytest.mark.asyncio
    async def test_gateway_404_leaves_the_row_retryable(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a non-JSON 404 from a proxy is not read as a real absence.

        ``exception_for_status`` deliberately leaves such a response a bare
        ``HTTPException``, precisely so a narrowed handler cannot mistake an
        infrastructure failure for a resource-absent answer.
        """
        execution = await _seed_execution(session)
        tasks_api.get.side_effect = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bad gateway body"
        )

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.outcome_unrecoverable is False
        assert execution.reconcile_attempted_at is not None

    @pytest.mark.asyncio
    async def test_transport_failure_leaves_the_row_retryable(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a connection error leaves the row eligible for a later tick."""
        execution = await _seed_execution(session)
        tasks_api.get.side_effect = OSError("connection reset")

        await reconcile_executions(_BATCH_SIZE)

        await session.refresh(execution)
        assert execution.outcome_unrecoverable is False
        assert execution.reconcile_attempted_at is not None


class TestBatching:
    """Check the tick's bounds and its fair progress through the backlog."""

    @pytest.mark.asyncio
    async def test_batch_size_bounds_the_tick(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure one tick examines no more rows than the configured batch size."""
        for task_history_id in (1, 2, 3):
            await _seed_execution(session, task_history_id=task_history_id)
        tasks_api.get.return_value = _upstream_run()

        await reconcile_executions(_TWO_ROWS)

        assert tasks_api.get.await_count == _TWO_ROWS

    @pytest.mark.asyncio
    async def test_empty_backlog_issues_no_upstream_request(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a tick with nothing to do costs no upstream traffic."""
        await reconcile_executions(_BATCH_SIZE)

        tasks_api.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_overlapping_tick_selects_past_the_running_batch(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a tick started while an earlier one is mid-batch does not repeat it.

        One upstream request may take minutes against a slow tasks service, so a tick
        can outlive the schedule interval. The batch is claimed before its first
        request, so the next tick moves on to the rows behind it instead of issuing a
        second request for every row the first tick is still working through.
        """
        await _seed_execution(
            session, task_history_id=1, created_at=utc_now() - timedelta(hours=2)
        )
        await _seed_execution(
            session, task_history_id=2, created_at=utc_now() - timedelta(hours=1)
        )
        requested: list[str] = []

        async def _slow_get(path: str, **_kwargs: Any) -> dict[str, Any]:
            requested.append(path)
            if len(requested) == 1:
                await reconcile_executions(1)
            return _upstream_run(TaskHistoryStatusEnum.RUNNING)

        tasks_api.get.side_effect = _slow_get

        await reconcile_executions(1)

        assert requested == ["/history/1", "/history/2"]

    @pytest.mark.asyncio
    async def test_successive_ticks_reach_a_starved_row(
        self, session: AsyncSession, tasks_api: AsyncMock
    ) -> None:
        """Ensure a permanently non-terminal older row cannot starve a newer one.

        With ``batch_size=1`` the older row is examined first; because every attempt
        stamps ``reconcile_attempted_at``, the second tick moves on to the newer row
        instead of re-examining the older one forever.
        """
        older = await _seed_execution(
            session, task_history_id=1, created_at=utc_now() - timedelta(hours=2)
        )
        newer = await _seed_execution(
            session, task_history_id=2, created_at=utc_now() - timedelta(hours=1)
        )
        tasks_api.get.return_value = _upstream_run(TaskHistoryStatusEnum.RUNNING)

        await reconcile_executions(1)
        assert tasks_api.get.await_args.args[0].endswith(str(older.task_history_id))

        tasks_api.get.return_value = _upstream_run(TaskHistoryStatusEnum.FAILED)
        await reconcile_executions(1)

        assert tasks_api.get.await_args.args[0].endswith(str(newer.task_history_id))
        await session.refresh(newer)
        assert newer.terminal_status == TaskHistoryStatusEnum.FAILED.value
