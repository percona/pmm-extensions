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

"""Define routes for database connectivity checks."""

from fastapi import APIRouter, status

from app.api.deps import IsAuthenticatedDep
from app.core.exceptions import HTTPBadRequestException
from app.tasks.celery import get_executor_for_task
from app.tasks.connectivity.models import (
    ConnectivityCheckResponse,
    ConnectivityCheckWrite,
)
from app.tasks.connectivity.service import check_connectivity
from app.tasks.deps import get_executable_task_by_name, SessionDep

router = APIRouter(tags=["connectivity"])


@router.post(
    "/connectivity-check/",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_200_OK,
)
async def connectivity_check(
    session: SessionDep,
    request: ConnectivityCheckWrite,
) -> ConnectivityCheckResponse:
    """Check database connectivity via a Nomad task.

    :param session: The async database session.
    :type session: AsyncSession
    :param request: The connectivity check request parameters.
    :type request: ConnectivityCheckWrite
    :return: The connectivity check result.
    :rtype: ConnectivityCheckResponse
    """
    task = await get_executable_task_by_name(session, "run-python")
    executor = get_executor_for_task(task)
    registered_targets = await executor.get_hosts()
    if request.target not in registered_targets:
        raise HTTPBadRequestException(
            f"Target {request.target!r} is not available in "
            f"{executor.__class__.__name__} "
            f"(registered targets: {len(registered_targets)}). "
            f"The dispatch caller must pass the executor node name, not an "
            f"inventory display name."
        )
    return await check_connectivity(session, request)
