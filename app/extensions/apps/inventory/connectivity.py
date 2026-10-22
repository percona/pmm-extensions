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

"""Probe per-service database reachability through the Tasks API.

Not to be confused with ``app.extensions.connectivity``, which probes the platform's
own infrastructure endpoints rather than an inventory service's database.
"""

import logging

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.exceptions import HTTPBadGatewayException, HTTPBadRequestException
from app.core.requests import RemoteAPI
from app.core.utils.exceptions import exception_detail
from app.extensions.api.host_resolution import resolve_executor_name_by_address
from app.extensions.apps.inventory.constants import CONNECTABLE_SERVICE_TYPES
from app.extensions.inventory import CreatedService
from app.tasks.connectivity.models import ConnectivityCheckResponse

__all__ = ["probe_service_connectivity"]

logger = logging.getLogger(__name__)

_UNREACHABLE_TASKS_API = "could not reach the Tasks API"
_UNPARSEABLE_PROBE_RESULT = "the Tasks API answered with an unrecognized result"


def _failure(service: CreatedService, reason: str) -> str:
    """Build the service-scoped failure message shared by every guard.

    :param service: The service the probe was requested for.
    :param reason: The specific cause, appended after the service name.
    :return: A message naming both the service and the cause.
    """
    return f"Connectivity check failed for {service.name}: {reason}"


async def probe_service_connectivity(
    service: CreatedService,
    tasks_api: RemoteAPI,
) -> ConnectivityCheckResponse:
    """Run a database connectivity probe for ``service`` on its executor host.

    Resolve the executor registered for the service node's address, then ask
    the Tasks API to connect to the service's database from there. A probe that
    runs but cannot connect is **not** an error: it comes back as a response
    with ``success=False`` and ``error`` set. Only a request that could not be
    made at all raises.

    :param service: The inventory service to probe.
    :param tasks_api: Client for the Tasks sub-app.
    :return: The upstream probe result, unmodified.
    :raises HTTPBadRequestException: When the service type has no supported
        probe, the service carries no node or port, or no executor host is
        registered for the node's address.
    :raises HTTPBadGatewayException: When the Tasks API cannot be reached or
        answers with a body that is not a probe result.
    """
    if service.type not in CONNECTABLE_SERVICE_TYPES:
        raise HTTPBadRequestException(
            f"Connectivity check is not supported for {service.type.name} services"
        )
    if service.node is None or service.port is None:
        raise HTTPBadRequestException(
            _failure(service, "missing node or port information")
        )

    # Fetch executor hosts here rather than through ``ExecutorHosts`` so a
    # failure aborts with a service-scoped message; the dep continues with an
    # empty mapping and the caller could only report a generic failure.
    try:
        executor_hosts: dict[str, str] = await tasks_api.get("/hosts/")
    except HTTPException as exc:
        logger.warning(
            "Tasks API error fetching executor hosts for service %s: %s",
            service.id,
            exc.detail,
        )
        raise HTTPBadGatewayException(_failure(service, exception_detail(exc))) from exc
    except Exception as exc:
        logger.exception(
            "Failed to fetch executor hosts for connectivity check on service %s",
            service.id,
        )
        raise HTTPBadGatewayException(
            _failure(service, _UNREACHABLE_TASKS_API)
        ) from exc

    executor_target = resolve_executor_name_by_address(
        service.node.address, executor_hosts
    )
    if executor_target is None:
        raise HTTPBadRequestException(
            _failure(
                service,
                f"no executor host is registered for address "
                f"{service.node.address!r} (inventory node {service.node.name!r}).",
            )
        )

    try:
        return ConnectivityCheckResponse.model_validate(
            await tasks_api.post(
                "/connectivity-check/",
                json={
                    "target": executor_target,
                    "host": service.node.address,
                    "port": service.port,
                    "service_type": service.type.value,
                },
            )
        )
    except HTTPException as exc:
        logger.warning(
            "Connectivity check API error for service %s: %s",
            service.id,
            exc.detail,
        )
        raise HTTPBadGatewayException(_failure(service, exception_detail(exc))) from exc
    except ValidationError as exc:
        logger.exception(
            "Tasks API returned an unrecognized connectivity result for service %s",
            service.id,
        )
        raise HTTPBadGatewayException(
            _failure(service, _UNPARSEABLE_PROBE_RESULT)
        ) from exc
    except Exception as exc:
        logger.exception("Connectivity check failed for service %s", service.id)
        raise HTTPBadGatewayException(
            _failure(service, _UNREACHABLE_TASKS_API)
        ) from exc
