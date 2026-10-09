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

"""Define the ``/api/extensions/hosts/`` JSON endpoint exposing executor targets.

Mirror the executor-host data already used to render Jinja templates so the
React frontend can populate its host selector through PMM Extensions rather than calling
the Tasks and Inventory APIs directly.
"""

from typing import Any, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.exceptions import HTTPBadGatewayException
from app.core.pagination import fetch_all_dict_items
from app.extensions.api.host_resolution import address_to_name_index
from app.extensions.api.openapi import UPSTREAM_TASKS_502_RESPONSE
from app.extensions.deps import InventoryAPI, TaskAPI

router = APIRouter()


class HostResponse(BaseModel):
    """Represent a single executor target enriched with an inventory display name.

    :param id: The executor (Nomad / Celery) node name. This is the value
        consumed by dispatch payloads as ``executor_host``.
    :param name: Human-readable label sourced from inventory when available;
        falls back to ``id`` if the host has no inventory match.
    :param address: The network address reported by the executor.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        host: ``True`` when the task user is uid 0 or a bare ``sudo`` resolves,
        ``False`` when neither holds, ``None`` when never observed. A ``True``
        does not promise the task user is in sudoers, only that the launch check
        lets the command through. ``None`` is permanent, not transient, for an
        executor host with no inventory match.
    """

    id: str
    name: str
    address: str
    can_elevate: bool | None = None


#: Validator for the capability value read off an upstream observation row. An
#: upstream page's ``items`` is ``list[Any]``, so without it a non-boolean reaches
#: ``HostResponse`` outside the degradation ``try`` and answers 500 — the one
#: inventory failure this route would not absorb. Lax mode, so it accepts exactly
#: what ``HostResponse`` itself would have coerced.
_CAN_ELEVATE_ADAPTER = TypeAdapter(bool | None)


def _capabilities_by_executor(
    nodes: list[Any],
    observations: list[Any],
    executor_hosts: dict[str, str],
) -> dict[str, bool | None]:
    """Index each node's measured elevation capability by the executor that measured it.

    Follows the name-before-address matching
    :meth:`~app.extensions.sync.models.BaseTaskSyncer.get_task_target` applies when choosing
    the host to probe, so a measurement is published on the executor it was collected
    from. Joining by address alone publishes one executor's measurement on another
    whenever an inventory node's name matches one executor and its address another.
    The forced and fallback targets that method can also return are out of scope
    here: neither is co-located, so neither ever produces host facts.

    Each node contributes to exactly one pass, and only a node that actually carries
    an observation claims an executor. Both a name match and an address match count
    as co-located, so two nodes can be probed on one executor; were an unobserved
    name match to claim the slot, it would suppress the sibling row holding the real
    measurement of that same executor and publish never-observed instead.

    Reads ``can_elevate`` with ``get``, the one field the observation contract
    declares optional: a row omitting it was observed without a usable answer,
    which stays distinct from carrying no observation at all. What the row carries
    is validated here rather than left to the response model, because the response
    is built outside the caller's degradation ``try`` — a malformed value would
    answer 500 there instead of falling back to never-observed.

    :param nodes: Every inventory node row.
    :param observations: Every host observation summary.
    :param executor_hosts: Executor node name to address, as the Tasks API returns it.
    :return: Executor node name to its measured capability, absent when unmeasured.
    :raises KeyError: If an upstream row omits a required identifying field.
    :raises TypeError: If an upstream row is not a mapping.
    :raises ValidationError: If an upstream row carries a non-boolean capability.
        The caller treats all three alike as an inventory outage and degrades on them.
    """
    by_node = {
        observation["node_id"]: _CAN_ELEVATE_ADAPTER.validate_python(
            observation.get("can_elevate")
        )
        for observation in observations
    }
    capabilities: dict[str, bool | None] = {}
    unmatched: list[Any] = []
    for node in nodes:
        if node["name"] in executor_hosts:
            if node["id"] in by_node:
                capabilities.setdefault(node["name"], by_node[node["id"]])
        else:
            unmatched.append(node)
    executors_by_address = address_to_name_index(executor_hosts.items())
    for node in unmatched:
        target = executors_by_address.get(node["address"])
        if target is not None and target not in capabilities and node["id"] in by_node:
            capabilities[target] = by_node[node["id"]]
    return capabilities


@router.get(
    "/",
    responses=UPSTREAM_TASKS_502_RESPONSE,
)
async def list_hosts(
    tasks_api: TaskAPI,
    inventory_api: InventoryAPI,
) -> list[HostResponse]:
    """Return executor hosts merged with inventory display names.

    Call ``tasks_api.get('/hosts/')`` for executor targets and the Inventory
    API for display-name and capability enrichment. The Tasks and Inventory
    calls degrade differently: Inventory failures cause hosts without a match
    to keep the raw executor node name (the response still returns ``200``),
    but a Tasks-API failure (HTTP or connection error) is re-raised as
    :class:`~app.core.exceptions.HTTPBadGatewayException` so the PMM Extensions exception
    handler emits a ``502`` JSON body ``{"detail": "<upstream detail>"}`` that
    the React frontend surfaces through its React Query error slot.

    The two Inventory enrichments degrade **independently**, and on the same
    terms. The capability is the newer of them, and an Inventory old enough to
    answer 422 on its route — or failing it for any other reason — must not cost
    the host selector the display names it has always had. Both blocks also
    admit a malformed page, which fails envelope validation rather than raising
    a transport error: every way an Inventory read can fail leaves this route
    answering ``200`` with whatever it still resolved.

    The display name and the elevation capability are joined on **different
    keys** — address and executor name respectively — and may resolve to
    different inventory nodes. They answer different questions: the display name
    asks what inventory calls this address, the capability asks which executor
    was measured. Collapsing them into one index misattributes the measurement.

    :param tasks_api: The Tasks API client used to fetch executor hosts.
    :param inventory_api: The Inventory API client used to enrich the hosts
        with their display names and measured capabilities.
    :return: Sorted list of hosts, each with executor id, friendly name,
        network address, and elevation capability.
    :raises HTTPBadGatewayException: If the Tasks API call fails with an
        ``HTTPException`` (e.g. an upstream non-2xx response) or an
        ``OSError`` (e.g. a connection failure).
    """
    try:
        executor_hosts = cast("dict[str, str]", await tasks_api.get("/hosts/"))
    except (HTTPException, OSError) as exc:
        detail = getattr(exc, "detail", str(exc))
        raise HTTPBadGatewayException(detail=str(detail)) from exc

    nodes: list[Any] = []
    try:
        nodes = await fetch_all_dict_items(
            lambda pagination: inventory_api.get(
                "/nodes/", params=pagination.model_dump()
            )
        )
        display_names = address_to_name_index(
            (node["name"], node["address"]) for node in nodes
        )
    except (HTTPException, TypeError, KeyError, OSError, ValidationError):
        display_names = {}

    capabilities: dict[str, bool | None] = {}
    try:
        observations = await fetch_all_dict_items(
            lambda pagination: inventory_api.get(
                "/nodes/system-observations", params=pagination.model_dump()
            )
        )
        capabilities = _capabilities_by_executor(nodes, observations, executor_hosts)
    except (HTTPException, TypeError, KeyError, OSError, ValidationError):
        capabilities = {}

    return sorted(
        [
            HostResponse(
                id=node_name,
                name=display_names.get(address, node_name),
                address=address,
                can_elevate=capabilities.get(node_name),
            )
            for node_name, address in executor_hosts.items()
        ],
        key=lambda host: host.name.casefold(),
    )
