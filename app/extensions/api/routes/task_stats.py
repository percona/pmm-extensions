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

"""Define the ``/api/extensions/task-stats/{task_name}`` JSON endpoint proxying task statistics.

Expose the Tasks-service ``GET /stats/{task}`` aggregation through the PMM Extensions
gateway so the React frontend (``useTaskStats``) does not bypass the PMM Extensions
``/api/*`` routing layer when fetching per-task execution stats.
"""

from fastapi import APIRouter, HTTPException

from app.core.exceptions import HTTPBadGatewayException
from app.core.requests import as_json_object
from app.core.utils.exceptions import exception_detail
from app.core.utils.fields import ArbitraryMapping
from app.extensions.api.openapi import UPSTREAM_TASKS_502_RESPONSE
from app.extensions.deps import task_path, TaskAPI

router = APIRouter()


@router.get(
    "/{task_name}",
    responses=UPSTREAM_TASKS_502_RESPONSE,
)
async def get_task_stats(
    task_name: str,
    tasks_api: TaskAPI,
) -> ArbitraryMapping:
    """Return aggregated execution statistics for ``task_name``.

    Proxy to the Tasks-service ``GET /stats/{task_name}`` aggregation so the
    React frontend reaches the data through PMM Extensions rather than calling the Tasks
    sub-app directly. On upstream failure, re-raise as
    :class:`~app.core.exceptions.HTTPBadGatewayException` so the PMM Extensions exception
    handler emits a ``502`` JSON body ``{"detail": "<upstream detail>"}`` that
    the React frontend surfaces through React Query's error state.

    :param task_name: The task name (not the database id) whose stats are
        being requested.
    :param tasks_api: The Tasks API client used to fetch the upstream stats.
    :return: The raw upstream stats payload.
    :raises HTTPUnprocessableEntityException: If ``task_name`` is not a single
        plain URL path segment.
    :raises HTTPBadGatewayException: If the Tasks API call fails with an
        ``HTTPException`` (e.g. an upstream non-2xx response) or an
        ``OSError`` (e.g. a connection failure).
    """
    stats_path = f"/stats{task_path(task_name)}"
    try:
        return ArbitraryMapping(as_json_object(await tasks_api.get(stats_path)))
    except (HTTPException, OSError) as exc:
        raise HTTPBadGatewayException(detail=exception_detail(exc)) from exc
