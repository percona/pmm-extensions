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

import json
import logging
from typing import Any

from fastapi import APIRouter, status
from sqlalchemy_celery_beat import PeriodicTask

from app.api.deps import IsAuthenticatedDep
from app.core.celery.deps import CeleryBeatSessionDep
from app.core.celery.schedules import next_run_times, schedule_timezone
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.core.utils.iterators import unique_everseen
from app.tasks.crud import TaskManager
from app.tasks.deps import (
    get_executable_task_by_name,
    SessionDep,
    validate_chain_task_names,
)
from app.tasks.periodic.crud import PeriodicTaskManager
from app.tasks.periodic.deps import PeriodicTaskDep
from app.tasks.periodic.models import (
    PeriodicTaskExecuteRequest,
    PeriodicTaskResponse,
    PeriodicTaskUpdate,
    SchedulePreviewResponse,
    SchedulePreviewWrite,
)
from app.tasks.periodic.utils import attach_last_run_status

router = APIRouter(prefix="/periodic", tags=["periodic", "schedule", "tasks"])

logger = logging.getLogger(__name__)


@router.get(
    "/",
    dependencies=[IsAuthenticatedDep],
    response_model=PaginatedResponse[PeriodicTaskResponse],
)
async def list_periodic_tasks(
    session: CeleryBeatSessionDep,
    tasks_session: SessionDep,
    pagination: PaginationDep,
    owner: str | None = None,
    *,
    enabled: bool | None = None,
) -> PaginatedResponse[Any]:
    """List periodic tasks for the requested page window.

    :param session: The beat-store session the schedules are read from.
    :param tasks_session: The tasks-database session used to resolve the
        ``owner`` filter and to stamp each row's last-run status.
    :param pagination: Validated offset/limit window for this page.
    :param owner: Optional owner whose active tasks scope the page; omit to
        page every PMM Extensions managed schedule.
    :param enabled: Optional enabled-state filter.
    :return: A page of beat-store schedules, each carrying ``last_run_status``.
    """
    if owner is None:
        page = await PeriodicTaskManager.list_paginated(
            session, enabled=enabled, pagination=pagination
        )
    else:
        tasks_names = [
            task.name
            for task in await TaskManager.list_active(
                session=tasks_session, owner=owner
            )
        ]
        page = await PeriodicTaskManager.list_paginated(
            session,
            PeriodicTaskManager.build_where_clause_by_task_names(*tasks_names),
            enabled=enabled,
            pagination=pagination,
        )
    await attach_last_run_status(tasks_session, page.items)
    return page


@router.post("/schedule/preview/", dependencies=[IsAuthenticatedDep])
async def preview_schedule(preview: SchedulePreviewWrite) -> SchedulePreviewResponse:
    """Report the upcoming runs of a schedule without saving it.

    Answers for an unsaved request body what
    :class:`~app.tasks.periodic.models.PeriodicTaskResponse` answers for a stored
    schedule, through the same code path, so a client can preview a cron
    expression while it is still being typed.

    Two path segments so no single-segment ``/{param}`` sibling can match it,
    which is what keeps the PMM Extensions side twin from reserving a task name.

    :param preview: The schedule to preview. Persisted nowhere.
    :return: The schedule's zone and its upcoming runs.
    """
    runs = next_run_times(
        interval=preview.interval,
        crontab=preview.crontab,
        start_time=preview.start_time,
    )
    return SchedulePreviewResponse(
        timezone=schedule_timezone(preview.interval, preview.crontab),
        next_run_at=runs[0] if runs else None,
        next_runs=runs,
    )


@router.get(
    "/{periodic_task_id}",
    dependencies=[IsAuthenticatedDep],
    response_model=PeriodicTaskResponse,
)
async def retrieve_periodic_task(
    periodic_task: PeriodicTaskDep,
    tasks_session: SessionDep,
) -> PeriodicTask:
    """Retrieve a periodic task by ID."""
    await attach_last_run_status(tasks_session, [periodic_task])
    return periodic_task


@router.put(
    "/{periodic_task_id}",
    dependencies=[IsAuthenticatedDep],
    response_model=PeriodicTaskResponse,
)
async def update_periodic_task(
    session: CeleryBeatSessionDep,
    tasks_session: SessionDep,
    existing_task: PeriodicTaskDep,
    updated_task: PeriodicTaskUpdate,
) -> PeriodicTask:
    """Update a periodic task."""
    updated_kwargs = json.loads(updated_task.kwargs)
    existing_kwargs = json.loads(existing_task.kwargs)
    task_name = updated_kwargs.get("task_name") or existing_kwargs["task_name"]
    if task_name != existing_kwargs["task_name"]:
        await get_executable_task_by_name(tasks_session, task_name)

    updated_chain = (
        updated_task.execute_request.chain_task_names
        if updated_task.execute_request
        and updated_task.execute_request.chain_task_names
        else None
    )
    existing_execution_data = existing_kwargs.get("execution_data") or {}
    existing_chain = existing_execution_data.get("chain_task_names")

    if updated_chain:
        unique_chain = list(unique_everseen(updated_chain))

        if updated_task.execute_request:
            updated_task.execute_request.chain_task_names = unique_chain
        updated_chain = unique_chain

    if updated_task.execute_request is None and existing_execution_data:
        updated_task.execute_request = PeriodicTaskExecuteRequest(
            **existing_execution_data
        )
        kwargs_dict = json.loads(updated_task.kwargs)
        kwargs_dict["execution_data"] = existing_execution_data
        updated_task.kwargs = json.dumps(kwargs_dict)
        logger.debug(
            "UPDATE PERIODIC TASK - Reconstructed execute_request and kwargs from existing data"
        )

    effective_chain = updated_chain if updated_chain is not None else existing_chain

    # Validate whenever the effective chain exists and either the chain changed
    # or the effective parent task changed.
    if effective_chain and (
        effective_chain != existing_chain or task_name != existing_kwargs["task_name"]
    ):
        logger.debug(
            "UPDATE PERIODIC TASK - VALIDATING CHAIN (chain or parent changed)"
        )
        await validate_chain_task_names(
            tasks_session,
            effective_chain,
            await get_executable_task_by_name(tasks_session, task_name),
        )
    else:
        logger.debug(
            "UPDATE PERIODIC TASK - SKIPPING CHAIN VALIDATION (effective chain and parent unchanged)"
        )

    logger.debug("Updating periodic task %s", existing_task.id)
    updated = await PeriodicTaskManager.update(session, existing_task, updated_task)
    await attach_last_run_status(tasks_session, [updated])
    return updated


@router.delete(
    "/{periodic_task_id}",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_periodic_task(
    session: CeleryBeatSessionDep, periodic_task: PeriodicTaskDep
) -> None:
    """Delete a periodic task."""
    logger.debug("Deleting periodic task %s", periodic_task)
    await PeriodicTaskManager.delete(session, periodic_task)
