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

"""Define dependencies for the Tasks API."""

import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import nullcontext
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import undefer
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request

from app.api.deps import CurrentUserID
from app.core.db import ListQuery, make_list_query_dep
from app.core.exceptions import HTTPBadRequestException, HTTPNotFoundException
from app.core.requests import BaseRemoteAPI
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.config import tasks_settings
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.db import get_async_session_maker
from app.tasks.execution.executors.celery.models import CeleryExecutor
from app.tasks.execution.models import BaseExecutor
from app.tasks.execution.nomad_lifecycle import normalize_nomad_config_value
from app.tasks.models import (
    Task,
    TaskBackendEnum,
    TaskExecuteRequest,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
)

logger = logging.getLogger(__name__)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an asynchronous database session for FastAPI routes.

    This function provides a dependency for FastAPI routes that yields an
    ``AsyncSession`` for interacting with the database. The session is
    properly closed after use.

    :return: An async generator yielding an asynchronous session for database
        operations.
    :rtype: AsyncGenerator[AsyncSession, None]
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_executor(backend: TaskBackendEnum = TaskBackendEnum.NOMAD) -> BaseExecutor:
    """Get a task executor for the given backend without a request context.

    For NOMAD, returns an **un-entered** executor normalised from the override
    snapshot: when a ``NOMAD`` override is active the snapshot holds a config
    fingerprint, so it is reconstructed into a usable :class:`NomadExecutor`;
    with no override the live YAML executor passes through unchanged.

    Un-entered is sufficient for *any* reader, including the ported Nomad calls
    that do use aiohttp: those open a private executor of their own for the one
    call rather than touching this one's session (see
    ``NomadExecutor._calling_executor``). Request-scoped callers that want the
    process's shared session - the log streaming and file reads that keep a
    connection across many calls - should still use the :data:`TaskExecutor`
    dependency, which reaches the entered executor held by
    :class:`NomadLifecycle`.

    :param backend: The backend type to get an executor for.
    :type backend: TaskBackendEnum
    :return: The task executor.
    :rtype: BaseExecutor
    :raises ValueError: If the backend is not supported.
    """
    if backend == TaskBackendEnum.NOMAD:
        return normalize_nomad_config_value(tasks_settings.NOMAD)
    if backend == TaskBackendEnum.CELERY:
        return CeleryExecutor()
    raise ValueError(f"Unsupported backend {backend}")


def resolve_request_executor(
    request: Request, backend: TaskBackendEnum = TaskBackendEnum.NOMAD
) -> BaseExecutor:
    """Resolve the request-scoped task executor for the given backend.

    For NOMAD, returns the live **entered** executor owned by
    :class:`NomadLifecycle` (``app.state.nomad_lifecycle``) so routes that
    stream logs or list files use an open aiohttp session. When no holder is
    present (e.g. a unit test without the Tasks lifespan) or the holder is not
    started, falls back to the request-less :func:`get_executor`. Other backends defer to
    :func:`get_executor` unchanged.

    :param request: The incoming request, injected by FastAPI.
    :param backend: The backend type to get an executor for.
    :return: The task executor.
    :raises ValueError: If the backend is not supported.
    """
    if backend == TaskBackendEnum.NOMAD:
        holder = getattr(request.app.state, "nomad_lifecycle", None)
        if holder is not None:
            try:
                return holder.current
            except RuntimeError:
                # Stale holder left on app.state post-shutdown; degrade here
                # instead of failing the request.
                pass
    return get_executor(backend)


async def get_request_executor(
    request: Request, backend: TaskBackendEnum = TaskBackendEnum.NOMAD
) -> AsyncGenerator[BaseExecutor]:
    """Hold the resolved executor for the duration of the request.

    A ``NOMAD`` reconcile retires the entered executor the moment its config
    changes; holding it here keeps this request's executor open until the
    response has been sent, so a log stream or a file transfer already running
    on it finishes. A :class:`CeleryExecutor` owns no session and is yielded
    unchanged.

    :param request: The incoming request, injected by FastAPI.
    :param backend: The backend type to get an executor for.
    :return: The task executor.
    :raises ValueError: If the backend is not supported.
    """
    executor = resolve_request_executor(request, backend)
    hold = (
        executor.hold()
        if isinstance(executor, BaseRemoteAPI)
        else nullcontext(executor)
    )
    async with hold:
        yield executor


TaskExecutor = Annotated[BaseExecutor, Depends(get_request_executor)]


async def get_active_task_by_name(session: SessionDep, task_name: str) -> Task:
    """Get an active (not deleted) Task object by task name.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param task_name: The name of the task to retrieve.
    :type task_name: str
    :return: The retrieved Task object.
    :rtype: Task
    """
    logger.debug("Requesting task %s", task_name)
    return await TaskManager.retrieve_by_name(
        session=session, name=task_name, is_active=True
    )


TaskDep = Annotated[Task, Depends(get_active_task_by_name)]


async def get_executable_task_by_name(session: SessionDep, task_name: str) -> Task:
    """Get non-template Task object by task name.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param task_name: The name of the task to retrieve.
    :type task_name: str
    :return: The Task object if valid.
    :rtype: Task
    """
    logger.debug("Requesting executable task %s", task_name)
    return await TaskManager.retrieve_by_name(
        session=session, name=task_name, is_template=False, is_active=True
    )


ExecutableTaskDep = Annotated[Task, Depends(get_executable_task_by_name)]


async def validate_chain_task_names(
    session: AsyncSession,
    chain_task_names: list[str],
    parent_task: Task,
) -> None:
    """Validate that all chain task names exist, no cycles are present, and owners/targets match.

    :param session: The async database session.
    :type session: AsyncSession
    :param chain_task_names: Ordered list of task names to chain.
    :type chain_task_names: list[str]
    :param parent_task: The parent task (to prevent cycles and enforce owner/target matching).
    :type parent_task: Task
    :raises HTTPBadRequestException: If a cycle, owner mismatch, or target mismatch is found.
    :raises HTTPNotFoundException: If any chain task does not exist.
    """
    parent_target = parent_task.data.get("Constraints", [{}])[0].get("RTarget")
    seen = {parent_task.name}
    for name in chain_task_names:
        if name in seen:
            raise HTTPBadRequestException(
                f"Cycle detected in task chain: {name!r} already appears in the chain."
            )
        seen.add(name)
        chain_task = await TaskManager.first(
            session,
            col(Task.deleted_at).is_(None),
            name=name,
        )
        if chain_task is None:
            raise HTTPNotFoundException(f"Chained task {name!r} not found.")
        if chain_task.owner != parent_task.owner:
            raise HTTPBadRequestException(
                f"Chained task {name!r} has owner {chain_task.owner!r},"
                f" expected {parent_task.owner!r}."
            )
        chain_target = chain_task.data.get("Constraints", [{}])[0].get("RTarget")
        if chain_target != parent_target:
            raise HTTPBadRequestException(
                f"Chained task {name!r} has target {chain_target!r},"
                f" expected {parent_target!r}."
            )


async def prepare_task_history(
    task: ExecutableTaskDep,
    executed_by: CurrentUserID,
    session: SessionDep,
    execution_data: TaskExecuteRequest | None = None,
) -> TaskHistory:
    """Prepare the history of a task execution request.

    :param task: The task to execute.
    :type task: Task
    :param executed_by: The ID of the user executing the task.
    :type executed_by: CurrentUserID
    :param session: The async database session for validation queries.
    :type session: AsyncSession
    :param execution_data: Execution details and parameters, if any.
    :type execution_data: TaskExecuteRequest | None
    :return: The logged TaskHistory entry.
    :rtype: TaskHistory
    :raises HTTPNotFoundException: If the specified chain task does not exist.
    """
    logger.debug("Preparing TaskHistory for %s", task.name)
    execution_data = TaskExecuteRequest() if execution_data is None else execution_data
    if task.backend == TaskBackendEnum.PROXY:
        execution_data.meta |= task.data.get("meta", {})
        execution_data.payload = task.data.get("payload", execution_data.payload)
    if execution_data.chain_task_names:
        await validate_chain_task_names(session, execution_data.chain_task_names, task)
        execution_data.meta["_chain_task_names"] = execution_data.chain_task_names
        execution_data.meta["_chain_on_failure"] = execution_data.chain_on_failure
    if task.backend == TaskBackendEnum.CELERY:
        target = task.data.get("target")
    else:
        target = execution_data.meta.get("target") or task.data.get(
            "Constraints", [{}]
        )[0].get("RTarget")
    if not target:
        raise HTTPBadRequestException(
            "Execution target is required in execution data meta."
        )

    anonymize_mask = (
        execution_data.anonymize_mask
        if task.anonymize_mask is None
        else task.anonymize_mask
    )
    return TaskHistory(
        task_id=task.id,
        task=task,
        execution_request=TaskExecutionRequest(
            task=task.name,
            target=target,
            meta=execution_data.meta,
            payload=execution_data.payload,
            tracking={"evaluation_id": ""},
            eta=execution_data.eta,
        ),
        status=TaskHistoryStatusEnum.PENDING,
        executed_by=executed_by,
        anonymize_mask=anonymizer_settings.get_anonymize_mask(task.owner)
        if anonymize_mask is None
        else anonymize_mask,
    )


PreparedTaskHistory = Annotated[TaskHistory, Depends(prepare_task_history)]


async def get_task_history(
    session: SessionDep,
    task_history_id: int,
) -> TaskHistory:
    """Get TaskHistory object by task history ID.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param task_history_id: The ID of the task history to retrieve.
    :type task_history_id: int
    :return: The retrieved TaskHistory object.
    :rtype: TaskHistory
    """
    logger.debug("Requesting task history %s", task_history_id)
    return await TaskHistoryManager.get_or_404(session=session, id=task_history_id)


async def get_task_history_with_task(
    session: SessionDep,
    task_history_id: int,
) -> TaskHistory:
    """Get TaskHistory object by task history ID with related task.

    :param session: The asynchronous database session.
    :type session: AsyncSession
    :param task_history_id: The ID of the task history to retrieve.
    :type task_history_id: int
    :return: The retrieved TaskHistory object.
    :rtype: TaskHistory
    """
    logger.debug("Requesting task history %s", task_history_id)
    return await TaskHistoryManager.get_or_404(
        session=session,
        select_related=(TaskHistory.task,),
        query_options=[undefer(TaskHistory.execution_request)],
        id=task_history_id,
    )


TaskHistoryWithTaskDep = Annotated[TaskHistory, Depends(get_task_history_with_task)]


def get_logs_offsets(request: Request) -> defaultdict[str, dict[str, int]]:
    """Extract log offsets from the request query parameters.

    This function looks for query parameters that end with "_offset" and extracts
    the step and log type from the parameter name. It returns a dictionary where
    the keys are steps and the values are dictionaries mapping log types to their
    corresponding offsets.

    :param request: The FastAPI request object containing query parameters.
    :type request: Request
    :return: A dictionary mapping steps to log types and their offsets.
    :rtype: defaultdict[str, dict[str, int]]
    """
    offsets = defaultdict(dict)
    for raw_key, raw_val in request.query_params.items():
        if raw_key.endswith("_offset"):
            try:
                step, log_type, _ = raw_key.rsplit("_", 2)
                offsets[step][log_type] = max(0, int(raw_val))
            except ValueError:
                continue
    return offsets


LogsOffsetsDep = Annotated[
    defaultdict[str, dict[str, int]],
    Depends(get_logs_offsets),
]

TaskListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(TaskManager))]
TaskHistoryListQueryDep = Annotated[
    ListQuery, Depends(make_list_query_dep(TaskHistoryManager))
]
