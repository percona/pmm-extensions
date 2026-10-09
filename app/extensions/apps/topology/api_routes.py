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

"""Define the JSON API router for the Topology plugin.

Mounted at ``/api/apps/topology/`` via ``apps_router`` in
``app/extensions/api/router.py``. Enablement is governed by app registration
(``EXTENSIONS.APPS``) and the shared ``require_app_enabled`` dependency, so this
router carries no bespoke feature flag.

Topology collection dispatches ``run-python`` executor tasks (via ``TaskAPI``)
that run ``payloads/topology.py`` against the MySQL hosts sourced from the
inventory service (``InventoryAPI``). Results are polled (``GET /result``); the
merged React-Flow graph is built by the pure helpers in
``app.extensions.apps.topology.topology``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from app.core.auth.exceptions import HTTPForbiddenException
from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPBadRequestException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
)
from app.core.pagination.models import fetch_all_dict_items, Pagination
from app.core.requests import as_json_object, JSONBody, RemoteAPI
from app.extensions.apps.topology import payloads
from app.extensions.apps.topology.models import (
    MAX_TOPOLOGY_SHARDS,
    TopologyCollectResponse,
    TopologyCollectWrite,
    TopologyResultResponse,
)
from app.extensions.apps.topology.topology import (
    build_graph_from_stdouts,
    build_topology_meta,
    shard_hosts,
    TOPOLOGY_JOB_PREFIX,
)
from app.extensions.deps import (
    ApiCurrentUser,
    InventoryAPI,
    TaskAPI,
)
from app.inventory.constants import DEFAULT_MYSQL_PORT
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum

logger = logging.getLogger(__name__)

router = APIRouter()

_TOPOLOGY_PAYLOAD_PATH = Path(payloads.__file__).parent / "topology.py"
_TOPOLOGY_TASK = "run-python"
_TOPOLOGY_STDOUT_STEP = "run-script"


def _format_host_entry(service: dict[str, Any]) -> str | None:
    """Return a collector ``host:port`` entry for an inventory service.

    :param service: Inventory service payload with optional nested node data.
    :return: ``address:port`` or ``name:port`` when address data exists.
    """
    node = service.get("node") or {}
    address = node.get("address") or service.get("name")
    if address:
        port = service.get("port") or DEFAULT_MYSQL_PORT
        return f"{address}:{port}"
    return None


async def _collect_mysql_host_entries(inventory_api: RemoteAPI) -> list[str]:
    """Return ``host:port`` entries for every MySQL service in inventory.

    Walks every page of the inventory ``/services/`` list: the endpoint caps
    ``limit`` at :data:`MAX_PAGINATION_LIMIT`, so a single request would miss
    hosts once the fleet exceeds one page.

    :param inventory_api: Remote inventory API client.
    :return: De-duplicated MySQL ``host:port`` entries in inventory order.
    """

    async def _fetch_page(pagination: Pagination) -> JSONBody:
        return await inventory_api.get(
            "/services/",
            params={
                "service_type": ServiceTypeEnum.MYSQL.value,
                "offset": pagination.offset,
                "limit": pagination.limit,
            },
        )

    items = await fetch_all_dict_items(_fetch_page)
    seen: set[str] = set()
    hosts: list[str] = []
    for service in items:
        host_entry = _format_host_entry(service)
        if host_entry and host_entry not in seen:
            seen.add(host_entry)
            hosts.append(host_entry)
    return hosts


def _select_topology_targets(
    available_hosts: dict[str, str], requested_executor: str | None, shards: int
) -> list[str]:
    """Select executor host names for topology collection.

    :param available_hosts: Mapping of executor host names to host addresses.
    :param requested_executor: Optional explicit executor host name.
    :param shards: Requested number of executor shards.
    :return: Selected executor host names.
    :raises HTTPServiceUnavailableException: When no executor hosts are available.
    :raises HTTPBadRequestException: When the explicit executor is unavailable.
    """
    if not available_hosts:
        raise HTTPServiceUnavailableException(
            "No executor hosts available for topology collection.",
        )
    if requested_executor:
        if requested_executor not in available_hosts:
            raise HTTPBadRequestException(
                f"Executor host {requested_executor!r} is not available.",
            )
        return [requested_executor]
    return list(available_hosts.keys())[:shards]


@router.post("/collect", status_code=status.HTTP_202_ACCEPTED)
async def topology_collect(
    body: TopologyCollectWrite,
    inventory_api: InventoryAPI,
    tasks_api: TaskAPI,
) -> TopologyCollectResponse:
    """Dispatch one or more topology collector tasks. Returns the task ids.

    Hosts are pulled from the inventory MySQL service list; no inventory
    persistence side effects occur. With ``shards > 1`` the host list is
    split round-robin across the first N executor hosts so geographically
    split inventories run in parallel.

    :param body: Collection request (shard count, optional executor, timeouts).
    :param inventory_api: Remote inventory API client used to source MySQL hosts.
    :param tasks_api: Remote Tasks API client used to dispatch collector tasks.
    :return: Dispatched task history ids plus the executor targets and counts.
    """
    hosts = await _collect_mysql_host_entries(inventory_api)
    if not hosts:
        raise HTTPNotFoundException(
            "No MySQL services found in inventory.",
        )
    available_hosts = await tasks_api.get("/hosts/")
    if not isinstance(available_hosts, dict):
        raise HTTPBadGatewayException(
            "Tasks API returned an invalid executor hosts payload.",
        )
    targets = _select_topology_targets(available_hosts, body.executor_host, body.shards)
    chunks = shard_hosts(hosts, len(targets))
    targets = targets[: len(chunks)]

    payload_uri = f"file://{_TOPOLOGY_PAYLOAD_PATH}"
    extras = {
        "connect_timeout": body.connect_timeout,
        "read_timeout": body.read_timeout,
    }
    dispatches = []
    for target, chunk in zip(targets, chunks, strict=True):
        meta = build_topology_meta(target=target, hosts=chunk, extra=extras)
        dispatches.append(
            tasks_api.post(
                f"/execute/{_TOPOLOGY_TASK}",
                json={"meta": meta, "payload": payload_uri, "anonymize_mask": 0},
            )
        )

    created_tasks = await asyncio.gather(*dispatches)
    task_history_ids: list[int] = []
    for created in created_tasks:
        if not isinstance(created, dict) or "id" not in created:
            raise HTTPBadGatewayException(
                "Tasks API did not return a task history id.",
            )
        task_history_ids.append(int(created["id"]))
    logger.info(
        "Dispatched topology collection: hosts=%d shards=%d targets=%s tasks=%s",
        len(hosts),
        len(targets),
        targets,
        task_history_ids,
    )
    return TopologyCollectResponse(
        task_history_ids=task_history_ids,
        targets=targets,
        host_count=len(hosts),
        shard_count=len(targets),
    )


def _parse_ids_param(ids: str) -> list[int]:
    if not ids:
        raise HTTPBadRequestException("Missing required ?ids= query parameter.")
    parsed: list[int] = []
    seen: set[int] = set()
    for raw_chunk in ids.split(","):
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError as exc:
            raise HTTPBadRequestException(
                f"Invalid task history id: {chunk!r}."
            ) from exc
        if value not in seen:
            seen.add(value)
            parsed.append(value)
    if not parsed:
        raise HTTPBadRequestException("No task history ids provided.")
    if len(parsed) > MAX_TOPOLOGY_SHARDS:
        raise HTTPBadRequestException(
            f"At most {MAX_TOPOLOGY_SHARDS} task history ids are allowed."
        )
    return parsed


async def _fetch_task_history(
    tasks_api: RemoteAPI, task_history_id: int
) -> dict[str, Any]:
    return as_json_object(await tasks_api.get(f"/history/{task_history_id}"))


def _is_topology_history(history: dict[str, Any], current_user_id: str) -> bool:
    execution_request = history.get("execution_request") or {}
    meta = execution_request.get("meta") or {}
    return (
        history.get("executed_by") == current_user_id
        and execution_request.get("task") == _TOPOLOGY_TASK
        and meta.get("_job_id_prefix") == TOPOLOGY_JOB_PREFIX
    )


def _require_topology_histories(
    histories: list[dict[str, Any]], current_user: ApiCurrentUser
) -> None:
    """Reject task histories not dispatched by this user for topology."""
    current_user_id = str(current_user.id)
    if not all(_is_topology_history(h, current_user_id) for h in histories):
        raise HTTPForbiddenException("Task history is not accessible.")


async def _fetch_task_stdout(tasks_api: RemoteAPI, task_history_id: int) -> str:
    """Fetch and concatenate the stdout payload for a finished task history."""
    output: list[str] = []
    async for chunk in tasks_api.stream(
        f"/history/{task_history_id}/logs/", params={"step": _TOPOLOGY_STDOUT_STEP}
    ):
        if not chunk:
            continue
        try:
            log_entry = json.loads(chunk)
        except json.JSONDecodeError:
            logger.debug("Non-JSON log line from task %s", task_history_id)
            continue
        if log_entry.get("type") == "stdout":
            output.append(log_entry.get("msg", ""))
    return "".join(output)


def _unsuccessful_terminal_task_ids(histories: list[dict[str, Any]]) -> list[int]:
    return [
        int(h["id"])
        for h in histories
        if h.get("status") != TaskHistoryStatusEnum.SUCCESS.value
    ]


def _is_terminal_task_status(status_value: Any) -> bool:
    try:
        return TaskHistoryStatusEnum(status_value).is_terminal()
    except ValueError:
        return False


@router.get("/result")
async def topology_result(
    tasks_api: TaskAPI,
    current_user: ApiCurrentUser,
    ids: Annotated[str, Query(description="Comma-separated task history ids")],
) -> TopologyResultResponse:
    """Return the merged graph for the supplied task history ids.

    Status mirrors the underlying tasks: ``running`` while any are pending
    or running, ``failed`` when any task failed and produced no usable
    output, ``ok`` once every task is finished. The React client caches
    the response via TanStack Query (long ``staleTime``) and stops
    polling once status flips to ``ok``, so the server doesn't bother
    with HTTP cache validation here.

    :param tasks_api: Remote Tasks API client used to fetch task histories and logs.
    :param current_user: Authenticated caller; results are scoped to their own tasks.
    :param ids: Comma-separated task history ids to merge into one graph.
    :return: Aggregate status plus the merged graph (when ready) and task-id lists.
    """
    task_ids = _parse_ids_param(ids)
    histories = list(
        await asyncio.gather(*(_fetch_task_history(tasks_api, tid) for tid in task_ids))
    )
    _require_topology_histories(histories, current_user)
    pending = [
        int(h["id"]) for h in histories if not _is_terminal_task_status(h.get("status"))
    ]
    if pending:
        return TopologyResultResponse(status="running", pending_task_ids=pending)
    stdouts = list(
        await asyncio.gather(*(_fetch_task_stdout(tasks_api, tid) for tid in task_ids))
    )
    graph = build_graph_from_stdouts(stdouts)
    failed_task_ids = _unsuccessful_terminal_task_ids(histories)
    return TopologyResultResponse(
        status="failed" if failed_task_ids and not graph["nodes"] else "ok",
        graph=graph,
        failed_task_ids=failed_task_ids,
    )
