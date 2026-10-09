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

"""Define the ``/api/extensions/dashboard/`` JSON endpoint for dashboard statistics.

Returns aggregate counts for the four dashboard stat cards in a single round
trip so the React frontend avoids four parallel upstream calls. All four
sources are fetched concurrently via :func:`asyncio.gather`; each degrades
independently — a failure yields ``0`` for that counter and its name is
appended to the ``X-Upstream-Error`` response header so the frontend can
distinguish real zeroes from degraded counts.
"""

import asyncio
from typing import Any, cast

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.core.pagination import Pagination
from app.extensions.api.constants import UPSTREAM_ERROR_HEADER
from app.extensions.deps import InventoryAPI, SessionDep, TaskAPI
from app.extensions.snippets.crud import SnippetManager

router = APIRouter()


class DashboardStatsResponse(BaseModel):
    """Represent aggregate counts for the four dashboard stat cards.

    :param nodes: Total inventory node count.
    :type nodes: int
    :param tasks: Total active task count.
    :type tasks: int
    :param snippets: Total snippet count.
    :type snippets: int
    :param targets: Total executor target (host) count.
    :type targets: int
    """

    nodes: int
    tasks: int
    snippets: int
    targets: int


@router.get("/")
async def get_dashboard_stats(
    response: Response,
    session: SessionDep,
    tasks_api: TaskAPI,
    inventory_api: InventoryAPI,
) -> DashboardStatsResponse:
    """Return aggregate counts for all four dashboard stat cards.

    Sources (each degrades to ``0`` on failure):

    * ``nodes`` — ``GET /summary/`` on the Inventory API.
    * ``tasks`` — ``GET /`` on the Tasks API (``limit=1``; reads ``total``).
    * ``snippets`` — :meth:`SnippetManager.count` on the PMM Extensions database.
    * ``targets`` — ``GET /hosts/`` on the Tasks API; count of returned items.

    When one or more sources fail the names of the failed sources are joined
    with commas and set on the ``X-Upstream-Error`` response header so the
    caller can surface a partial-failure warning without treating all-zero
    counts as healthy data.

    :param response: The outgoing response used to attach the error header.
    :param session: The active database session for snippet queries.
    :param tasks_api: Async client for the Tasks sub-app.
    :param inventory_api: Async client for the Inventory sub-app.
    :return: Aggregate counts for nodes, tasks, snippets, and targets.
    """

    async def _nodes() -> int:
        summary: dict[str, Any] = await inventory_api.get("/summary/")
        return int(summary.get("nodes", 0))

    async def _tasks() -> int:
        task_list: dict[str, Any] = await tasks_api.get(
            "/", params=Pagination(offset=0, limit=1).model_dump()
        )
        return int(task_list.get("total", 0))

    async def _snippets() -> int:
        return await SnippetManager.count(session)

    async def _targets() -> int:
        host_list: list[Any] = await tasks_api.get("/hosts/")
        return len(host_list)

    sources = ("nodes", "tasks", "snippets", "targets")
    results = await asyncio.gather(
        _nodes(),
        _tasks(),
        _snippets(),
        _targets(),
        return_exceptions=True,
    )

    counts: dict[str, int] = {}
    failed: list[str] = []
    for name, result in zip(sources, results, strict=False):
        if isinstance(result, Exception):
            failed.append(name)
            counts[name] = 0
        else:
            counts[name] = cast("int", result)

    if failed:
        response.headers[UPSTREAM_ERROR_HEADER] = ",".join(failed)

    return DashboardStatsResponse(**counts)
