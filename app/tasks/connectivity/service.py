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

"""Define the connectivity check service logic."""

import asyncio
import contextvars
import json
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

from async_lru import alru_cache
from sqlalchemy.orm import QueryableAttribute, undefer
from sqlmodel.ext.asyncio.session import AsyncSession

from app.tasks.celery import dispatch_queue_item, get_executor_for_task
from app.tasks.connectivity.constants import (
    CONNECTIVITY_CHECK_TIMEOUT,
    PROVISIONING_TIMEOUT,
)
from app.tasks.connectivity.models import (
    ConnectivityCheckResponse,
    ConnectivityCheckWrite,
    ConnectivityServiceType,
    REQUIREMENTS_BY_SERVICE_TYPE,
)
from app.tasks.crud import TaskHistoryManager
from app.tasks.db import get_async_session_maker
from app.tasks.deps import get_executable_task_by_name
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.logs.log_reader import iter_task_history_logs
from app.tasks.models import (
    SYSTEM_USER,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
)

PAYLOAD_PATH = Path(__file__).parent / "payload.py"
POLL_INTERVAL = 2
FRESH_FETCH_MAX_ATTEMPTS = 3
FRESH_FETCH_INTERVAL = 0.5
RESULT_CACHE_TTL = 300
RESULT_CACHE_MAXSIZE = 128

#: Prefix of Nomad's zero-time sentinel for an unstarted task's ``StartedAt``
#: (``0001-01-01T00:00:00Z``); treated the same as an absent value.
_NOMAD_ZERO_TIME = "0001-01-01"

_cached_check_session_ctx: contextvars.ContextVar[AsyncSession] = (
    contextvars.ContextVar("_cached_check_session_ctx")
)


@alru_cache(maxsize=RESULT_CACHE_MAXSIZE, ttl=RESULT_CACHE_TTL)
async def _cached_check_connectivity(
    target: str,
    host: str,
    port: int,
    service_type: ConnectivityServiceType,
) -> tuple[bool, str | None]:
    """Run ``check_connectivity`` and cache the (success, error) result.

    The ``AsyncSession`` is NOT part of the cache key — it is resolved at
    call time via :data:`_cached_check_session_ctx`, which the public
    :func:`check_connectivity_with_cache` wrapper sets on each invocation.
    Using ``alru_cache`` keeps the cache consistent with the rest of the
    codebase (see ``app/core/auth/providers/casdoor.py``).

    :param target: The Nomad node name.
    :param host: The database host address.
    :param port: The database port.
    :param service_type: The database service type.
    :return: A tuple of ``(success, error)``.
    """
    session = _cached_check_session_ctx.get()
    request = ConnectivityCheckWrite(
        target=target,
        host=host,
        port=port,
        service_type=service_type,
        timeout=CONNECTIVITY_CHECK_TIMEOUT,
    )
    result = await check_connectivity(session, request)
    return result.success, result.error


async def check_connectivity_with_cache(
    session: AsyncSession,
    *,
    target: str,
    host: str,
    port: int,
    service_type: ConnectivityServiceType,
) -> tuple[bool, str | None]:
    """Run :func:`check_connectivity` with results cached by target+type.

    Results for a given ``(target, host, port, service_type)`` tuple are
    cached for ``RESULT_CACHE_TTL`` seconds via :func:`async_lru.alru_cache`.
    The session is passed through a :class:`~contextvars.ContextVar` so it
    does not participate in the cache key.

    :param session: The async database session.
    :param target: The Nomad node name.
    :param host: The database host address.
    :param port: The database port.
    :param service_type: The database service type.
    :return: A tuple of ``(success, error)``.
    """
    token = _cached_check_session_ctx.set(session)
    try:
        return await _cached_check_connectivity(target, host, port, service_type)
    finally:
        _cached_check_session_ctx.reset(token)


async def check_connectivity(
    session: AsyncSession,
    request: ConnectivityCheckWrite,
) -> ConnectivityCheckResponse:
    """Dispatch a Nomad connectivity check and wait for the result.

    :param session: The async database session.
    :param request: The connectivity check request parameters.
    :return: The connectivity check result.
    """
    task = await get_executable_task_by_name(session, "run-python")

    config = json.dumps(
        {
            "host": request.host,
            "port": request.port,
            "service_type": request.service_type,
        }
    )
    queue_item = TaskHistory(
        task_id=task.id,
        task=task,
        execution_request=TaskExecutionRequest(
            task=task.name,
            target=request.target,
            meta={
                "target": request.target,
                "config": config,
                "requirements": REQUIREMENTS_BY_SERVICE_TYPE[request.service_type],
            },
            payload=f"file://{PAYLOAD_PATH}",
            tracking={"evaluation_id": ""},
        ),
        status=TaskHistoryStatusEnum.PENDING,
        executed_by=SYSTEM_USER,
        anonymize_mask=0,
    )

    queue_item = await dispatch_queue_item(queue_item, session)
    if queue_item.id is None:
        raise RuntimeError("dispatch_queue_item returned a queue item without an ID")
    queue_item_id = queue_item.id
    await session.refresh(queue_item, attribute_names=["execution_request"])

    queue_item = await _expire_and_fetch(session, queue_item_id)

    executor = get_executor_for_task(task)
    async_session = get_async_session_maker()
    # Nomad reports ``RUNNING`` from dispatch onward, so the run-script task's
    # ``StartedAt`` (not ``status``) marks where the DB connect begins — see
    # ``_connect_phase_started``. Time before it is charged against
    # ``PROVISIONING_TIMEOUT``, time after against the connect budget below.
    #
    # Budgets are charged against wall-clock (``time.monotonic``), not a count of
    # ``POLL_INTERVAL`` sleeps, so per-iteration ``sync_task_history`` round-trips
    # and DB commits count too and the server hold stays within the read timeout.
    provisioning_started = time.monotonic()
    connect_started_at: float | None = None
    connect_started = False
    while queue_item.status in (
        TaskHistoryStatusEnum.PENDING,
        TaskHistoryStatusEnum.RUNNING,
    ):
        if not connect_started:
            connect_started = _connect_phase_started(queue_item)
            if connect_started:
                connect_started_at = time.monotonic()
        if not connect_started:
            if time.monotonic() - provisioning_started >= PROVISIONING_TIMEOUT:
                break
        elif (
            connect_started_at is not None
            and time.monotonic() - connect_started_at >= request.timeout
        ):
            break
        await asyncio.sleep(POLL_INTERVAL)
        async with async_session() as writer_session:
            queue_item, _pending_event = await executor.sync_task_history(
                queue_item, writer_session=writer_session
            )
        await TaskHistoryManager.save(
            session, queue_item, flag_modified_fields=["execution_request"]
        )
        queue_item = await _expire_and_fetch(session, queue_item_id)

    if queue_item.status in (
        TaskHistoryStatusEnum.PENDING,
        TaskHistoryStatusEnum.RUNNING,
    ):
        return await _build_timeout_response(
            session,
            queue_item_id,
            provisioning=not connect_started,
            connect_timeout=request.timeout,
        )

    fresh_queue_item = await _fetch_fresh_task_history(session, queue_item_id)
    return await _parse_check_result(session, fresh_queue_item)


async def _build_timeout_response(
    session: AsyncSession,
    task_history_id: int,
    *,
    provisioning: bool,
    connect_timeout: int,
) -> ConnectivityCheckResponse:
    """Build a timeout :class:`ConnectivityCheckResponse` with any captured output.

    Distinguish a provisioning timeout (the ``run-script`` task never started, so
    the DB connect never began) from a connect timeout (the connect phase began
    but did not complete) so operators can tell provisioning latency apart from an
    unreachable DB. Surface any ``run-script`` output captured before the timeout
    instead of discarding it, while still carrying ``task_history_id`` so the GUI
    can link the full run-script log.

    :param session: The async database session.
    :param task_history_id: The ID of the timed-out check task.
    :param provisioning: Whether the connect phase never started (provisioning
        timeout) rather than began but stalled (connect timeout) at timeout.
    :param connect_timeout: The connect budget in seconds (``request.timeout``).
    :return: A failure response with a descriptive error and ``task_history_id``.
    """
    if provisioning:
        message = (
            f"Connectivity check timed out after {PROVISIONING_TIMEOUT}s "
            "waiting for the execution host to provision"
        )
    else:
        message = f"Connectivity check timed out after {connect_timeout}s"

    output = await _collect_run_script_output(session, task_history_id)
    error = f"{message}\n\n{output}" if output else message
    return ConnectivityCheckResponse(
        success=False,
        error=error,
        task_history_id=task_history_id,
    )


async def _collect_run_script_output(
    session: AsyncSession, task_history_id: int
) -> str:
    """Return the concatenated ``run-script`` stdout/stderr captured so far.

    :param session: The async database session.
    :param task_history_id: The ID of the task history row to read.
    :return: The concatenated run-script output, or an empty string if none.
    """
    task_history = await _expire_and_fetch(session, task_history_id)
    chunks = [
        log.msg async for log in _iter_run_script_logs(session, task_history) if log.msg
    ]
    return "".join(chunks)


def _connect_phase_started(task_history: TaskHistory) -> bool:
    """Return whether the ``run-script`` Nomad task has started executing.

    ``run-python`` runs the connectivity payload in a ``run-script`` main task
    preceded by a ``prepare-env`` prestart task (Nomad scheduling + dependency
    install). The allocation reports ``RUNNING`` from dispatch onward, so status
    cannot mark when the connect begins; ``run-script``'s ``StartedAt`` can,
    because it flips from unset to a timestamp only once ``prepare-env`` finishes
    and the payload task actually starts — the provisioning/connect boundary.

    ``StartedAt`` is read from ``execution_request.tracking["task_states"]``,
    which the executor refreshes from the Nomad allocation on every poll, so this
    needs no log read or flush. Both ``None`` and Nomad's zero-time sentinel
    (``0001-01-01T00:00:00Z``) count as "not started".

    :param task_history: The task history row being polled.
    :return: ``True`` once the ``run-script`` task reports a real ``StartedAt``.
    """
    tracking = task_history.execution_request.tracking or {}
    task_states = tracking.get("task_states") or {}
    started_at = (task_states.get(NomadStep.RUN_SCRIPT) or {}).get("StartedAt")
    if not started_at:
        return False
    return not started_at.startswith(_NOMAD_ZERO_TIME)


async def _fetch_fresh_task_history(
    session: AsyncSession, task_history_id: int
) -> TaskHistory:
    """Re-read the task history row from the database until its logs land.

    The polling loop's last successful ``sync_task_history`` populates the
    ``task_logs`` column of the DB row, but the in-memory
    ``TaskExecutionRequest`` Pydantic object attached to ``queue_item`` is
    NOT re-hydrated through a subsequent ``session.get``: the identity map
    returns the existing object and SQLAlchemy's refresh leaves nested JSON
    fields untouched. Expunge the row and fetch it again so a brand-new
    object is materialised from the row as it exists in the database.
    A small retry budget covers the narrow window where the final sync has
    not yet committed by the time the handler reaches this point.

    :param session: The async database session.
    :param task_history_id: The ID of the task history row to refresh.
    :return: The freshest task history row available within the retry budget.
    """
    task_history = await _expire_and_fetch(session, task_history_id)
    for _ in range(FRESH_FETCH_MAX_ATTEMPTS - 1):
        if await _has_run_script_logs(session, task_history):
            return task_history
        await asyncio.sleep(FRESH_FETCH_INTERVAL)
        task_history = await _expire_and_fetch(session, task_history_id)
    return task_history


async def _expire_and_fetch(session: AsyncSession, task_history_id: int) -> TaskHistory:
    """Commit, expunge, and re-fetch the task history with ``execution_request`` loaded.

    Fulfill two goals needed by the polling loop:

    - Force a brand-new instance materialisation. The executor's
      ``sync_task_history`` mutates the in-memory ``TaskExecutionRequest``
      Pydantic object, which SQLAlchemy's refresh does not fully re-hydrate
      for nested JSON columns. Commit to end the open snapshot, then expunge
      the row from the identity map so the next fetch does a genuine
      ``SELECT`` and builds a brand-new object.
    - Eagerly load the deferred ``TaskHistory.execution_request`` column.
      Without ``undefer`` the column stays expired on the returned instance
      and any subsequent synchronous attribute read (e.g. from
      ``NomadExecutor.get_allocation_for_task_history``) triggers a
      lazy-load SELECT outside the greenlet bridge, raising
      ``MissingGreenlet`` on aiosqlite/asyncpg.

    The ``execution_request`` column is declared as a deferred
    ``column_property`` on :class:`TaskHistory`, so the re-fetch must
    explicitly ``undefer`` it — otherwise the attribute is left unloaded
    and any downstream sync-context read (e.g. ``iter_logs`` walking
    ``self.execution_request.tracking``) would fire a lazy SELECT and
    raise ``MissingGreenlet`` against the async driver.

    :param session: The async database session.
    :param task_history_id: The ID of the task history row to reload.
    :return: A freshly-loaded task history row with
        ``execution_request`` materialised.
    """
    await session.commit()
    cached = await TaskHistoryManager.get_or_404(session, id=task_history_id)
    session.expunge(cached)
    return await TaskHistoryManager.get_or_404(
        session,
        query_options=[
            undefer(cast("QueryableAttribute[Any]", TaskHistory.execution_request))
        ],
        id=task_history_id,
    )


async def _iter_logs(
    session: AsyncSession,
    task_history: TaskHistory,
    source: str,
) -> AsyncGenerator[TaskLog, None]:
    """Yield ``source`` logs from either legacy or chunked storage.

    :param session: The async database session.
    :param task_history: The task history row being inspected.
    :param source: The Nomad task/step name whose log chunks to yield.
    :return: An async generator yielding log chunks for ``source``.
    """
    iter_logs = getattr(task_history, "iter_logs", None)
    if callable(iter_logs):
        for log in iter_logs(step=source):
            yield log
        return

    async for log in iter_task_history_logs(session, task_history, source=source):
        yield log


async def _iter_run_script_logs(
    session: AsyncSession,
    task_history: TaskHistory,
) -> AsyncGenerator[TaskLog, None]:
    """Yield ``run-script`` logs from either legacy or chunked storage.

    :param session: The async database session.
    :param task_history: The task history row being inspected.
    :return: An async generator yielding log chunks for the ``run-script`` source.
    """
    async for log in _iter_logs(session, task_history, NomadStep.RUN_SCRIPT):
        yield log


async def _has_run_script_logs(
    session: AsyncSession, task_history: TaskHistory
) -> bool:
    """Return whether ``task_history`` has any ``run-script`` step output.

    :param session: The async database session.
    :param task_history: The task history record to inspect.
    :return: ``True`` if a non-empty stdout or stderr log exists for the
        ``run-script`` step, ``False`` otherwise.
    """
    async for log in _iter_run_script_logs(session, task_history):
        if log.msg:
            return True
    return False


async def _collect_failed_stderr(
    session: AsyncSession, task_history: TaskHistory
) -> str:
    """Return the stderr behind a FAILED check, preferring run-script output.

    Read the ``run-script`` stderr first; when it is empty fall back to the
    ``execution`` source, where a pre-dispatch gate failure (which never runs
    run-script) writes its reason, so that diagnostic stays reachable here.

    :param session: The async database session.
    :param task_history: The FAILED task history record.
    :return: The concatenated, stripped stderr, or an empty string if none.
    """
    stderr = ""
    async for log in _iter_run_script_logs(session, task_history):
        if log.type == TaskLogType.STDERR:
            stderr += log.msg or ""
    stderr = stderr.strip()
    if stderr:
        return stderr
    async for log in _iter_logs(session, task_history, "execution"):
        if log.type == TaskLogType.STDERR:
            stderr += log.msg or ""
    return stderr.strip()


async def _parse_check_result(
    session: AsyncSession, task_history: TaskHistory
) -> ConnectivityCheckResponse:
    """Extract connectivity result from task logs.

    :param session: The async database session.
    :param task_history: The completed task history record.
    :return: The parsed connectivity check result.
    """
    if task_history.id is None:
        raise RuntimeError("_parse_check_result called with unsaved TaskHistory")

    if task_history.status == TaskHistoryStatusEnum.FAILED:
        stderr = await _collect_failed_stderr(session, task_history)
        return ConnectivityCheckResponse(
            success=False,
            error=stderr or "Task failed",
            task_history_id=task_history.id,
        )

    stdout = ""
    async for log in _iter_run_script_logs(session, task_history):
        if log.type == TaskLogType.STDOUT:
            stdout += log.msg or ""

    try:
        result = json.loads(stdout)
        return ConnectivityCheckResponse(
            success=result.get("success", False),
            error=result.get("error"),
            task_history_id=task_history.id,
        )
    except (json.JSONDecodeError, AttributeError):
        return ConnectivityCheckResponse(
            success=False,
            error="Failed to parse connectivity check output",
            task_history_id=task_history.id,
        )
