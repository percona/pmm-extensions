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

"""Define routes for the Tasks API."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Sequence
from datetime import timedelta
from typing import Annotated, cast

import requests.exceptions
from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import undefer
from sqlalchemy_celery_beat import PeriodicTask
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import CurrentUserID, IsAuthenticatedDep, require_minimum_role
from app.core.auth.models import UserRole
from app.core.celery.deps import CeleryBeatSessionDep
from app.core.config import settings
from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPUnprocessableEntityException,
)
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.core.requests import BaseRemoteAPI
from app.core.utils import utc_now
from app.core.utils.fields import NonEmptyStr
from app.tasks.celery import (
    celery,
    dispatch_queue_item,
    execute_task_queue,
    get_executor_for_task,
    maybe_dispatch_chain,
)
from app.tasks.config import PreExecutionCheckMode, tasks_settings
from app.tasks.connectivity.constants import (
    CONNECTIVITY_META_HOST_KEY,
    CONNECTIVITY_META_PORT_KEY,
    CONNECTIVITY_META_SERVICE_TYPE_KEY,
)
from app.tasks.connectivity.models import ConnectivityServiceType
from app.tasks.connectivity.service import check_connectivity_with_cache
from app.tasks.crud import (
    TaskHistoryLogManager,
    TaskHistoryLogStateManager,
    TaskHistoryManager,
    TaskManager,
)
from app.tasks.db import get_async_session_maker
from app.tasks.deps import (
    ExecutableTaskDep,
    get_executor,
    LogsOffsetsDep,
    PreparedTaskHistory,
    SessionDep,
    TaskDep,
    TaskExecutor,
    TaskHistoryListQueryDep,
    TaskHistoryWithTaskDep,
    TaskListQueryDep,
    validate_chain_task_names,
)
from app.tasks.execution.utils import parse_payload
from app.tasks.logs.log_reader import has_legacy_logs, iter_task_history_logs
from app.tasks.models import (
    ExecutionEvent,
    ExecutorHostState,
    FileMetadata,
    LogCaptureStatusEnum,
    Task,
    TaskBackendEnum,
    TaskHistory,
    TaskHistoryLatestStatus,
    TaskHistoryLatestStatusRequest,
    TaskHistoryResponse,
    TaskHistoryStatusEnum,
    TaskResponse,
    TaskStats,
    TaskWrite,
    TransformPayloadRequest,
    TransformPayloadResponse,
)
from app.tasks.periodic.crud import PeriodicTaskManager
from app.tasks.periodic.models import PeriodicTaskCreate, PeriodicTaskResponse
from app.tasks.periodic.utils import (
    attach_last_run_status,
    generate_periodic_task_name,
)
from app.tasks.run_result import maybe_record_run

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


@router.get(
    "/",
    dependencies=[IsAuthenticatedDep],
    response_model=PaginatedResponse[TaskResponse],
)
async def list_tasks(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: TaskListQueryDep,
    owner: str | None = None,
    target: str | None = None,
    parent_is_null: bool | None = None,
    backup_type: str | None = None,
    self_parent: bool | None = None,
) -> PaginatedResponse[Task]:
    """List all active tasks."""
    logger.debug("Listing tasks")
    return await TaskManager.list_active_paginated(
        session=session,
        owner=owner,
        target=target,
        parent_is_null=parent_is_null,
        backup_type=backup_type,
        self_parent=self_parent,
        pagination=pagination,
        list_query=list_query,
    )


@router.delete(
    "/{task_name}",
    dependencies=[IsAuthenticatedDep],
    response_model=TaskResponse,
)
async def delete_task(
    session: SessionDep, celery_beat_session: CeleryBeatSessionDep, task_name: str
) -> Task:
    """Delete a task."""
    logger.debug("Deleting task %s", task_name)
    # TODO(yan): Delete for real
    # SEP-170
    task = await TaskManager.delete_by_name(session=session, name=task_name)
    await PeriodicTaskManager.delete_where(
        celery_beat_session,
        PeriodicTaskManager.build_where_clause_by_task_names(task_name),
    )
    return task


@router.get(
    "/{task_name}", dependencies=[IsAuthenticatedDep], response_model=TaskResponse
)
async def get_task(task: TaskDep) -> Task:
    """Retrieve a task by its name."""
    return task


@router.post(
    "/",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_201_CREATED,
    response_model=TaskResponse,
)
async def create_task(
    session: SessionDep, task: TaskWrite, current_user_id: CurrentUserID
) -> Task:
    """Create a new task."""
    logger.debug("Creating task %s", task.name)
    return await TaskManager.create(
        session,
        task,
        created_by=current_user_id,
    )


@router.put(
    "/{task_name}",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_201_CREATED,
    response_model=TaskResponse,
)
async def update_task(
    session: SessionDep,
    existing_task: TaskDep,
    updated_task: TaskWrite,
    current_user_id: CurrentUserID,
) -> Task:
    """Update an existing task."""
    logger.debug("Updating task %s", existing_task.name)
    return await TaskManager.update(
        session, existing_task, updated_task, last_updated_by=current_user_id
    )


@router.get(
    "/{task_name}/periodic/",
    dependencies=[IsAuthenticatedDep],
    response_model=list[PeriodicTaskResponse],
)
async def list_periodic_tasks_by_task_name(
    celery_beat_session: CeleryBeatSessionDep,
    session: SessionDep,
    task: ExecutableTaskDep,
) -> list[PeriodicTask]:
    """List periodic tasks by task name."""
    periodic_tasks = await PeriodicTaskManager.list_by_task_names(
        celery_beat_session, task.name
    )
    return await attach_last_run_status(session, periodic_tasks)


@router.post(
    "/{task_name}/periodic/",
    dependencies=[IsAuthenticatedDep],
    response_model=PeriodicTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_periodic_task_for_task_name(
    celery_beat_session: CeleryBeatSessionDep,
    session: SessionDep,
    task: ExecutableTaskDep,
    periodic_task: PeriodicTaskCreate,
) -> PeriodicTask:
    """Create a new periodic task for the specified task name."""
    logger.debug("Creating periodic task %s", periodic_task)
    if periodic_task.execute_request and periodic_task.execute_request.chain_task_names:
        await validate_chain_task_names(
            session, periodic_task.execute_request.chain_task_names, task
        )
    kwargs = json.loads(periodic_task.kwargs)
    kwargs["task_name"] = task.name
    if not periodic_task.name:
        periodic_task.name = generate_periodic_task_name(
            task.name, periodic_task.period, periodic_task.kwargs
        )
    kwargs["periodic_task_name"] = periodic_task.name
    return await PeriodicTaskManager.create(
        celery_beat_session, periodic_task, kwargs=json.dumps(kwargs)
    )


@router.post(
    "/execute/{task_name}",
    dependencies=[IsAuthenticatedDep],
    response_model=TaskHistoryResponse,
)
async def execute_task_name(
    session: SessionDep,
    queue_item: PreparedTaskHistory,
    task_name: str,
) -> TaskHistory:
    """Send a task for execution.

    When ``tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK`` is enabled and the
    task carries ``_connectivity_*`` meta fields, run a Nomad-side connectivity
    check against the target before dispatch. In ``block`` mode, dispatch is
    rejected with HTTP 400 on failure; in ``warn`` mode, a warning is logged
    and dispatch proceeds. ETA-scheduled tasks always skip the check.
    """
    logger.debug(
        "Dispatching task %s at %s",
        task_name,
        queue_item.execution_request.eta or utc_now(),
    )

    root_task = await TaskManager.get_root_task(session, queue_item.task)
    executor = get_executor_for_task(root_task)
    if queue_item.execution_request.target not in executor.get_hosts():
        raise HTTPBadRequestException(
            f"Failed to dispatch task: Target {queue_item.execution_request.target!r}"
            f"is not available in {executor.__class__.__name__} for task {task_name!r}"
        )

    check_mode = tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK
    meta = queue_item.task.data.get("Meta", {})
    conn_host = meta.get(CONNECTIVITY_META_HOST_KEY)
    conn_port = meta.get(CONNECTIVITY_META_PORT_KEY)
    conn_type = meta.get(CONNECTIVITY_META_SERVICE_TYPE_KEY)
    if (
        check_mode != PreExecutionCheckMode.DISABLED
        and conn_host
        and conn_port
        and conn_type
        and not queue_item.execution_request.eta
    ):
        target = queue_item.execution_request.target
        try:
            parsed_conn_port = int(conn_port)
            parsed_conn_type = ConnectivityServiceType(conn_type)
        except ValueError:
            logger.warning(
                "Skipping pre-execution connectivity check for task %r on target %r "
                "due to malformed connectivity metadata: port=%r, service_type=%r",
                task_name,
                target,
                conn_port,
                conn_type,
            )
        else:
            success, error = await check_connectivity_with_cache(
                session,
                target=target,
                host=conn_host,
                port=parsed_conn_port,
                service_type=parsed_conn_type,
            )
            if not success:
                msg = (
                    f"Pre-execution connectivity check failed for {task_name!r} "
                    f"on target {target!r}: {error or 'unknown error'}"
                )
                if check_mode == PreExecutionCheckMode.BLOCK:
                    raise HTTPBadRequestException(msg)
                logger.warning(msg)

    if queue_item.execution_request.eta:
        history_recorded = await TaskHistoryManager.save(session, queue_item)
        await session.refresh(history_recorded, attribute_names=["execution_request"])
        recorded_eta = history_recorded.execution_request.eta or (
            queue_item.execution_request.eta
        )
        celery_task = execute_task_queue.apply_async(
            args=[history_recorded.id],
            eta=recorded_eta,
            expires=recorded_eta
            + timedelta(seconds=settings.CELERY.global_expire_seconds),
        )
        history_recorded.execution_request.tracking["celery_task_id"] = celery_task.id
        history_recorded = await TaskHistoryManager.save(
            session, history_recorded, flag_modified_fields=["execution_request"]
        )
    else:
        history_recorded = await dispatch_queue_item(queue_item, session)
    await session.refresh(history_recorded, attribute_names=["execution_request"])
    return history_recorded


def _set_log_metadata(
    history: TaskHistory,
    *,
    has_logs: bool,
    log_capture: LogCaptureStatusEnum,
) -> None:
    """Attach the log metadata to ``history`` for later ``model_validate`` pickup.

    ``TaskHistory`` is a strict Pydantic model (SQLModel default), so a
    plain ``history.has_logs = value`` raises ``ValueError`` for fields
    that are not declared on the ORM. ``object.__setattr__`` writes
    straight to the instance ``__dict__``; FastAPI's response coercion
    then reads it via ``getattr`` because ``TaskHistoryResponse``
    enables ``from_attributes=True`` through SQLModel.

    :param history: The ORM instance to mutate.
    :param has_logs: Whether any readable log content exists for this history.
    :param log_capture: The aggregate capture verdict for this history.
    """
    object.__setattr__(history, "has_logs", has_logs)
    object.__setattr__(history, "log_capture", log_capture)


async def _populate_log_metadata(
    session: AsyncSession,
    histories: Sequence[TaskHistory],
) -> None:
    """Set ``has_logs`` and ``log_capture`` on each history.

    Reads the chunk store and the per-stream capture verdicts in one batched
    query each, so list endpoints avoid an N+1 lookup per row. ``has_logs`` ORs
    the chunk-store result with :func:`has_legacy_logs` so legacy rows keep
    rendering the **View Logs** button until the backfill lands.

    A history carrying no state rows has no evidence either way and reports
    ``UNKNOWN`` rather than being rounded up to complete.

    Every route that reports on an *existing* history goes through here,
    including the single-row ones: the two flags must move together, and a route
    that populated only one would report a default verdict as though it were
    measured. The routes that create or dispatch a history skip it, because a
    row that has not run yet has no evidence to report.

    :param session: The SQLAlchemy asynchronous session.
    :param histories: ``TaskHistory`` instances whose log metadata should be
        populated. Each instance must have ``execution_request`` already loaded
        (all callers undefer it).
    """
    if not histories:
        return
    history_ids = [history.id for history in histories]
    chunk_ids = await TaskHistoryLogManager.ids_with_chunks(session, history_ids)
    capture_statuses = await TaskHistoryLogStateManager.capture_status_by_task(
        session, history_ids
    )
    for history in histories:
        _set_log_metadata(
            history,
            has_logs=history.id in chunk_ids or has_legacy_logs(history),
            log_capture=capture_statuses.get(history.id, LogCaptureStatusEnum.UNKNOWN),
        )


async def _get_history_for_response(
    session: AsyncSession,
    history_id: int,
) -> TaskHistory:
    """Re-read a task history with the columns its response model requires.

    ``execution_request`` is deferred on the column and ``task`` is a lazy
    relationship, so a row handed to :class:`TaskHistoryResponse` without both
    resolved attempts IO from the async context.

    :param session: The SQLAlchemy asynchronous session.
    :param history_id: The task history to re-read.
    :return: The task history with ``task`` joined and ``execution_request``
        undeferred.
    """
    return await TaskHistoryManager.get_or_404(
        session,
        select_related=(TaskHistory.task,),
        query_options=[undefer(TaskHistory.execution_request)],
        id=history_id,
    )


@router.get(
    "/history/",
    dependencies=[IsAuthenticatedDep],
    response_model=PaginatedResponse[TaskHistoryResponse],
)
async def list_task_history(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: TaskHistoryListQueryDep,
    *,
    task_status: Annotated[TaskHistoryStatusEnum | None, Query(alias="status")] = None,
    exclude_internal: Annotated[bool, Query()] = False,
) -> PaginatedResponse[TaskHistory]:
    """List all task history records."""
    logger.debug("Listing task history")
    response = await TaskHistoryManager.list_all_history_paginated(
        session,
        query_options=[undefer(TaskHistory.execution_request)],
        pagination=pagination,
        list_query=list_query,
        status=task_status,
        exclude_internal=exclude_internal,
    )
    await _populate_log_metadata(session, response.items)
    return response


@router.post(
    "/history/latest",
    dependencies=[IsAuthenticatedDep],
)
@require_minimum_role(UserRole.NONE)
async def latest_task_history(
    session: SessionDep,
    body: TaskHistoryLatestStatusRequest,
) -> dict[str, TaskHistoryLatestStatus | None]:
    """Return the latest-history projection (status + finished_at) per task name."""
    logger.debug("Resolving latest history projection for %s task(s)", len(body.names))
    return await TaskHistoryManager.latest_status_by_task_names(session, body.names)


@router.get(
    "/{task}/history/",
    dependencies=[IsAuthenticatedDep],
    response_model=PaginatedResponse[TaskHistoryResponse],
)
async def get_task_history(
    session: SessionDep,
    task: str,
    pagination: PaginationDep,
    list_query: TaskHistoryListQueryDep,
    task_status: Annotated[TaskHistoryStatusEnum | None, Query(alias="status")] = None,
    snippet_filename: NonEmptyStr | None = None,
) -> PaginatedResponse[TaskHistory]:
    """Retrieve task history by task name."""
    logger.debug("Requesting task history for %s", task)
    response = await TaskHistoryManager.list_by_task_name_paginated(
        session=session,
        task_name=task,
        status=task_status,
        select_related_task=True,
        snippet_filename=snippet_filename,
        pagination=pagination,
        list_query=list_query,
        query_options=[undefer(TaskHistory.execution_request)],
    )
    await _populate_log_metadata(session, response.items)
    return response


@router.get(
    "/history/{task_history_id}",
    dependencies=[IsAuthenticatedDep],
    response_model=TaskHistoryResponse,
)
async def retrieve_task_history(
    session: SessionDep,
    task_history: TaskHistoryWithTaskDep,
) -> TaskHistory:
    """Retrieve a task history by id."""
    logger.debug("Requesting task history %s", task_history.id)
    await _populate_log_metadata(session, [task_history])
    return task_history


@router.get(
    "/history/{task_history_id}/events",
    dependencies=[IsAuthenticatedDep],
)
async def list_task_history_events(
    executor: TaskExecutor,
    task_history: TaskHistoryWithTaskDep,
) -> list[ExecutionEvent]:
    """Return structured execution events from executor tracking (oldest first)."""
    logger.debug("Requesting execution events for task history %s", task_history.id)
    return executor.get_events(task_history)


@router.get(
    "/history/{task_history_id}/logs/",
    dependencies=[IsAuthenticatedDep],
    response_model=None,
)
async def stream_task_history_logs(
    session: SessionDep,
    executor: TaskExecutor,
    task_history: TaskHistoryWithTaskDep,
    offsets: LogsOffsetsDep,
    step: str | None = None,
    tail: Annotated[int | None, Query(ge=1)] = None,
) -> StreamingResponse:
    """Stream a task history's logs.

    ``tail`` limits output to the last N lines per stream for finished histories
    only. It is ignored while the task is ``RUNNING`` (live executor stream).
    """
    logger.debug("Requesting logs for task history %s", task_history.id)
    if task_history.status == TaskHistoryStatusEnum.PENDING:
        raise HTTPConflictException("Task history is pending.")
    if task_history.status == TaskHistoryStatusEnum.RUNNING:
        if isinstance(executor, BaseRemoteAPI):
            await executor.run_in_thread_held(
                executor.preflight_stream_logs, task_history
            )
        else:
            await asyncio.to_thread(executor.preflight_stream_logs, task_history)
        stream_logs_generator = (
            f"{log_line.model_dump_json()}\n" if log_line else ""
            async for log_line in executor.stream_logs(task_history, offsets)
        )
    else:

        async def _stream_finished_logs() -> AsyncGenerator[str, None]:
            async for log in iter_task_history_logs(
                session,
                task_history,
                offsets,
                source=step,
                tail_lines=tail,
            ):
                yield log.model_dump_json() + "\n"

        stream_logs_generator = _stream_finished_logs()
    return StreamingResponse(
        stream_logs_generator,
        media_type="application/json",
    )


@router.get(
    "/history/{task_history_id}/files/",
    dependencies=[IsAuthenticatedDep],
)
async def list_task_history_files(
    executor: TaskExecutor,
    task_history: TaskHistoryWithTaskDep,
) -> dict[str, FileMetadata]:
    """List files from a task history."""
    logger.debug("Requesting files for task history %s", task_history.id)
    if not task_history.status.is_finished():
        raise HTTPConflictException(f"Task history is {task_history.status}.")
    if task_history.task.output_files_path is None:
        raise HTTPBadRequestException(
            f"Task {task_history.task.name} does not have output_files_path set."
        )
    return await executor.list_files(task_history, task_history.task.output_files_path)


@router.get(
    "/history/{task_history_id}/file/",
    dependencies=[IsAuthenticatedDep],
    response_model=None,
)
async def stream_task_history_file(
    executor: TaskExecutor,
    task_history: TaskHistoryWithTaskDep,
    path: str,
) -> StreamingResponse:
    """Stream a file from a task history."""
    logger.debug("Requesting file %s for task history %s", path, task_history.id)
    if not task_history.status.is_finished():
        raise HTTPConflictException(f"Task history is {task_history.status}.")
    if task_history.task.output_files_path is None:
        raise HTTPBadRequestException(
            f"Task {task_history.task.name} does not have output_files_path set."
        )
    return StreamingResponse(
        executor.stream_file(
            task_history,
            os.path.join(task_history.task.output_files_path, path.lstrip("/")),  # noqa: PTH118
        ),
        media_type="application/octet-stream",
    )


@router.post(
    "/history/{task_history_id}/stop/",
    dependencies=[IsAuthenticatedDep],
    response_model=TaskHistoryResponse,
)
async def stop_task_history(
    session: SessionDep, executor: TaskExecutor, task_history: TaskHistoryWithTaskDep
) -> TaskHistory:
    """Stop a task history."""
    logger.debug("Stopping task history %s", task_history.id)
    if task_history.status == TaskHistoryStatusEnum.PENDING:
        if celery_task_id := task_history.execution_request.tracking.get(
            "celery_task_id"
        ):
            logger.debug(
                "Cancelling pending task history %s with Celery task ID %s",
                task_history.id,
                celery_task_id,
            )
            celery.control.revoke(celery_task_id)
        return await TaskHistoryManager.delete(session, task_history)
    if task_history.status != TaskHistoryStatusEnum.RUNNING:
        raise HTTPBadRequestException(
            f"Cannot stop task history {task_history.id} ({task_history.task.name}): "
            f"task is not running (current status: {task_history.status})."
        )
    stopped = await executor.stop_task(session, task_history)
    await _populate_log_metadata(session, [stopped])
    return stopped


@router.post(
    "/history/{task_history_id}/sync/",
    dependencies=[IsAuthenticatedDep],
    response_model=TaskHistoryResponse,
)
async def sync_task_history(
    session: SessionDep, executor: TaskExecutor, task_history: TaskHistoryWithTaskDep
) -> TaskHistory:
    """Sync task history with the executor and persist the latest status.

    Atomically claim the ``sync_in_progress_started_at`` lock before calling
    the executor so the celery ``sync_running_tasks`` periodic and this route
    never both progress past the executor call for the same row. When the
    claim returns no rows — either because another syncer holds the lock or
    because the status has already flipped to terminal — refresh the row
    from the DB and return without re-syncing. The holder (or completed
    syncer) is responsible for dispatching any chained task.
    """
    logger.debug("Syncing task history %s", task_history.id)
    if task_history.status != TaskHistoryStatusEnum.RUNNING:
        await _populate_log_metadata(session, [task_history])
        return task_history

    claim_result = await TaskHistoryManager.update_where(
        session,
        {"sync_in_progress_started_at": utc_now()},
        or_(
            col(TaskHistory.sync_in_progress_started_at).is_(None),
            col(TaskHistory.sync_in_progress_started_at)
            < (utc_now() - tasks_settings.SYNC_LOCK_TTL),
        ),
        id=task_history.id,
        status=TaskHistoryStatusEnum.RUNNING,
    )
    if not claim_result.rowcount:
        session.expunge(task_history)
        task_history = await _get_history_for_response(
            session, cast(int, task_history.id)
        )
        await _populate_log_metadata(session, [task_history])
        return task_history

    async_session = get_async_session_maker()
    try:
        async with async_session() as writer_session:
            updated = await executor.sync_task_history(
                task_history, writer_session=writer_session
            )
        updated.sync_in_progress_started_at = None
        saved = await TaskHistoryManager.save(
            session,
            updated,
            flag_modified_fields=[
                "execution_request",
                "status",
                "started_at",
                "finished_at",
                "failure_reason",
                "sync_in_progress_started_at",
            ],
        )
    except Exception:
        await TaskHistoryManager.update_where(
            session,
            {"sync_in_progress_started_at": None},
            id=task_history.id,
        )
        raise
    synced = await _get_history_for_response(session, cast(int, saved.id))
    await maybe_dispatch_chain(synced, was_running=True)
    if synced.status.is_terminal():
        await maybe_record_run(cast(int, synced.id), executor)
    await _populate_log_metadata(session, [synced])
    return synced


@router.post(
    "/history/",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_201_CREATED,
    response_model=TaskHistoryResponse,
)
async def create_task_history(session: SessionDep, task: TaskHistory) -> TaskHistory:
    """Create a new task history.

    Neither ``has_logs`` nor ``log_capture`` is populated on this response — a
    row that was just created has no chunk-store entry, legacy tracking blob or
    capture verdict yet, so both fall back to their serialization defaults.

    A caller-supplied ``failure_reason`` is routed back through
    :meth:`TaskHistory.set_failure_reason` so the single-line and length bounds
    hold on every write path, not only on the reasons PMM Extensions composes itself.

    The saved row is re-read with ``task`` joined and ``execution_request``
    undeferred: ``save`` re-defers that column, and the response model requires
    both, so serializing the save's own return value attempts lazy IO from an
    async context.

    :param session: The SQLAlchemy asynchronous session.
    :param task: The task history to persist.
    :return: The saved task history record.
    :raises HTTPUnprocessableEntityException: If ``failure_reason`` conflicts
        with a status that carries no operator-facing summary.
    """
    logger.debug("Creating task history for task %s", task.task_id)
    if (
        task.failure_reason is not None
        and TaskHistoryStatusEnum(task.status).operator_summary() is None
    ):
        raise HTTPUnprocessableEntityException(
            detail=[
                {
                    "loc": ["body", "failure_reason"],
                    "msg": (
                        "failure_reason must be null when status carries no "
                        "operator-facing summary."
                    ),
                    "type": "value_error",
                }
            ]
        )
    task.set_failure_reason(task.failure_reason)
    saved = await TaskHistoryManager.save(session, task)
    return await _get_history_for_response(session, cast(int, saved.id))


@router.get("/stats/{task}", dependencies=[IsAuthenticatedDep])
async def get_task_stats(session: SessionDep, task: str) -> TaskStats:
    """Calculate the statistics for the task."""
    logger.debug("Requesting task stats for %s", task)
    return TaskStats(
        tasks=await TaskHistoryManager.list_by_task_name(
            session=session,
            task_name=task,
            select_related_task=True,
        ),
    )


@router.get("/hosts/", dependencies=[IsAuthenticatedDep])
async def get_executor_hosts(executor: TaskExecutor) -> dict[str, str]:
    """Return the executor hosts from the executor.

    Wrap the upstream executor call so connection failures or non-JSON
    bodies surface as a 502 JSON response instead of leaking a default
    500 + text/plain that masks the real failure on the dashboard banner.

    :param executor: The task executor backend used to fetch host metadata.
    :type executor: TaskExecutor
    :return: A mapping of executor node name to network address.
    :rtype: dict[str, str]
    :raises HTTPBadGatewayException: If the executor backend raises a
        ``requests.exceptions.RequestException`` (e.g. a non-JSON response
        body or a connection failure outside the Nomad SDK's own wrapping).
    """
    try:
        return executor.get_hosts()
    except requests.exceptions.RequestException as exc:
        raise HTTPBadGatewayException(
            detail=f"Executor backend unreachable: {exc}"
        ) from exc


@router.get("/hosts/states/", dependencies=[IsAuthenticatedDep])
async def get_executor_host_states(executor: TaskExecutor) -> list[ExecutorHostState]:
    """Return every host the executor knows about, usable or not.

    ``GET /hosts/`` answers "where can I place a job", which is what a dispatcher
    needs and all it needs. This answers "what is the state of the fleet", which is a
    different question: a host missing from the other list may never have been
    onboarded, or be onboarded and down, or be up with a broken driver, and those are
    three different things for whoever has to fix it.

    Wrapped the same way as ``/hosts/`` so an unreachable backend surfaces as a 502
    rather than a 500 with a text/plain body.

    :param executor: The task executor backend used to fetch host metadata.
    :return: One entry per host the backend knows about.
    :raises HTTPBadGatewayException: If the executor backend is unreachable or
        answers with something the client cannot parse.
    """
    try:
        return executor.get_host_states()
    except requests.exceptions.RequestException as exc:
        raise HTTPBadGatewayException(
            detail=f"Executor backend unreachable: {exc}"
        ) from exc


@router.post(
    "/transform/",
    dependencies=[IsAuthenticatedDep],
)
async def transform_payload(
    data: TransformPayloadRequest,
    backend: TaskBackendEnum = TaskBackendEnum.NOMAD,
) -> TransformPayloadResponse:
    """Transform a payload string into a dictionary."""
    if backend == TaskBackendEnum.PROXY:
        return TransformPayloadResponse.model_validate(
            parse_payload(data.payload, data.fmt)
        )
    executor = get_executor(backend)
    raw = await executor.transform_payload(data.payload, data.fmt)
    return TransformPayloadResponse.model_validate(raw)
