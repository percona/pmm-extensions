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

"""Provide schema-driven JSON-side connectivity warning helpers."""

from typing import Any

from pydantic import BaseModel

from app.core.requests import RemoteAPI
from app.extensions.connectivity import (
    _fetch_connectivity_result,
    CONNECTIVITY_META_HOST_KEY,
    CONNECTIVITY_META_PORT_KEY,
    CONNECTIVITY_META_SERVICE_TYPE_KEY,
)

__all__ = [
    "CONNECTIVITY_WARNING_FIELD",
    "ConnectivityWarning",
    "maybe_record_connectivity_warning",
    "record_connectivity_warning",
]

_CONNECTIVITY_CHECK_FAILED_FALLBACK = "Connectivity check failed"

#: Name of the response-model field carrying the post-creation connectivity
#: probe result. Shared by the create-route presence check and the derived
#: create-response model so the two cannot drift apart.
CONNECTIVITY_WARNING_FIELD = "connectivity_warning"


class ConnectivityWarning(BaseModel):
    """Represent a connectivity-check failure on a JSON API task-creation response.

    :param target: The Nomad node the task targets.
    :param service_type: The lowercase database service type (e.g. ``mysql``).
    :param message: A human-readable description of the failure.
    :param task_history_id: The run-script task-history id whose log explains
        the failure, or ``None`` when no task was created (e.g. the Tasks API
        was unreachable). Optional for backward compatibility with existing
        plugin consumers.
    """

    target: str
    service_type: str
    message: str
    task_history_id: int | None = None


async def record_connectivity_warning(
    tasks_api: RemoteAPI,
    *,
    target: str,
    host: str,
    port: int,
    service_type: str,
) -> ConnectivityWarning | None:
    """Run a connectivity check and return a JSON-friendly warning on failure.

    Delegate to :func:`app.extensions.connectivity._fetch_connectivity_result`
    (cached via ``alru_cache``).

    :param tasks_api: Authenticated Tasks API client.
    :param target: The Nomad node name.
    :param host: The database host address.
    :param port: The database port.
    :param service_type: The lowercase service type (e.g. ``mysql``).
    :return: ``None`` on success or a populated ``ConnectivityWarning`` on failure.
    """
    success, error, task_history_id = await _fetch_connectivity_result(
        tasks_api, target, host, port, service_type
    )
    if success:
        return None
    return ConnectivityWarning(
        target=target,
        service_type=service_type,
        message=error or _CONNECTIVITY_CHECK_FAILED_FALLBACK,
        task_history_id=task_history_id,
    )


async def maybe_record_connectivity_warning(
    tasks_api: RemoteAPI,
    meta: dict[str, Any],
    *,
    check_connectivity: bool = True,
) -> ConnectivityWarning | None:
    """Run :func:`record_connectivity_warning` when ``meta`` carries connectivity data.

    Short-circuit to ``None`` when ``check_connectivity`` is ``False`` or when
    any of the required meta keys is missing or falsy. This lets task-creation
    routes invoke the helper unconditionally without inspecting ``meta``
    themselves.

    :param tasks_api: Authenticated Tasks API client.
    :param meta: The ``task.data["meta"]`` mapping from the created task.
    :param check_connectivity: If ``False``, skip both the Tasks API call and
        the snapshot write. Honors the per-task opt-out from the JSON API.
    :return: ``None`` when skipped or successful; ``ConnectivityWarning`` on failure.
    """
    if not check_connectivity:
        return None
    target = meta.get("target")
    host = meta.get(CONNECTIVITY_META_HOST_KEY)
    port = meta.get(CONNECTIVITY_META_PORT_KEY)
    service_type = meta.get(CONNECTIVITY_META_SERVICE_TYPE_KEY)
    if not (target and host and port and service_type):
        return None
    return await record_connectivity_warning(
        tasks_api,
        target=target,
        host=host,
        port=port,
        service_type=service_type,
    )
