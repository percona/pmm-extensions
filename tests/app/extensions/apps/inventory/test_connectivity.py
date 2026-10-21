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

"""Define tests for the app.extensions.apps.inventory.connectivity module.

The helper backs both the legacy Jinja2 handler and the JSON API route, so
every guard is covered here directly; the two callers are covered by their own
route tests.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.core.exceptions import HTTPBadGatewayException, HTTPBadRequestException
from app.core.requests import RemoteAPI
from app.extensions.apps.inventory.connectivity import probe_service_connectivity
from app.extensions.apps.inventory.constants import CONNECTABLE_SERVICE_TYPES
from app.extensions.inventory import CreatedNode, CreatedService
from app.inventory.models import ServiceTypeEnum
from app.tasks.connectivity.models import ConnectivityServiceType
from tests.app.factories import CreatedNodeFactory, CreatedServiceFactory

_MYSQL_PORT = 3306
_TASK_HISTORY_ID = 42
_UNSUPPORTED_SERVICE_TYPES = sorted(
    set(ServiceTypeEnum) - CONNECTABLE_SERVICE_TYPES, key=str
)


@pytest.fixture
def created_node() -> CreatedNode:
    """Return a fake inventory node."""
    return CreatedNodeFactory.build()


@pytest.fixture
def mysql_service(created_node: CreatedNode) -> CreatedService:
    """Return a MySQL service attached to ``created_node`` with a port set."""
    service = CreatedServiceFactory.build(type=ServiceTypeEnum.MYSQL, port=_MYSQL_PORT)
    service.node = created_node
    return service


@pytest.fixture
def tasks_api(created_node: CreatedNode) -> AsyncMock:
    """Return a Tasks-API client whose host mapping resolves ``created_node``."""
    api = AsyncMock(spec=RemoteAPI)
    api.get.return_value = {created_node.name: created_node.address}
    return api


def test_connectable_types_match_the_upstream_probe_enum():
    """Pin the connectable set to the service types the upstream probe supports.

    The two sets are declared independently — ``CONNECTABLE_SERVICE_TYPES`` gates
    the request, ``ConnectivityServiceType`` is what the Tasks API validates
    against. A type present here but not upstream would reach the probe and come
    back as HTTP 422, which the helper reports as a gateway error rather than the
    local misconfiguration it is; a type present upstream but not here is silently
    unreachable.
    """
    assert {service_type.value for service_type in CONNECTABLE_SERVICE_TYPES} == {
        probe_type.value for probe_type in ConnectivityServiceType
    }


@pytest.mark.asyncio
async def test_returns_upstream_response_on_success(mysql_service, tasks_api):
    """Return the upstream model unchanged and post the resolved executor target."""
    tasks_api.post.return_value = {
        "success": True,
        "error": None,
        "task_history_id": _TASK_HISTORY_ID,
    }

    result = await probe_service_connectivity(mysql_service, tasks_api)

    assert result.success is True
    assert result.task_history_id == _TASK_HISTORY_ID
    tasks_api.get.assert_awaited_once_with("/hosts/")
    tasks_api.post.assert_awaited_once_with(
        "/connectivity-check/",
        json={
            "target": mysql_service.node.name,
            "host": mysql_service.node.address,
            "port": mysql_service.port,
            "service_type": ServiceTypeEnum.MYSQL.value,
        },
    )


@pytest.mark.asyncio
async def test_failed_probe_returns_response_instead_of_raising(
    mysql_service, tasks_api
):
    """Return the result as a value when the check fails but the call succeeds."""
    tasks_api.post.return_value = {
        "success": False,
        "error": "Connection refused",
        "task_history_id": _TASK_HISTORY_ID,
    }

    result = await probe_service_connectivity(mysql_service, tasks_api)

    assert result.success is False
    assert result.error == "Connection refused"


@pytest.mark.asyncio
async def test_resolves_executor_target_by_address(
    mysql_service, tasks_api, created_node
):
    """Resolve the executor by node address rather than by the inventory name."""
    executor_name = f"{created_node.name}-nomad-mismatch"
    tasks_api.get.return_value = {executor_name: created_node.address}
    tasks_api.post.return_value = {
        "success": True,
        "error": None,
        "task_history_id": _TASK_HISTORY_ID,
    }

    await probe_service_connectivity(mysql_service, tasks_api)

    assert tasks_api.post.await_args.kwargs["json"]["target"] == executor_name


@pytest.mark.parametrize("service_type", _UNSUPPORTED_SERVICE_TYPES)
@pytest.mark.asyncio
async def test_rejects_unsupported_service_type(created_node, tasks_api, service_type):
    """Reject a non-connectable service type before contacting the Tasks API."""
    service = CreatedServiceFactory.build(type=service_type, port=_MYSQL_PORT)
    service.node = created_node

    with pytest.raises(HTTPBadRequestException) as exc_info:
        await probe_service_connectivity(service, tasks_api)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert service_type.name in exc_info.value.detail
    tasks_api.get.assert_not_awaited()
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_service_without_node(mysql_service, tasks_api):
    """Reject a service with no node, naming the service in the message."""
    mysql_service.node = None

    with pytest.raises(HTTPBadRequestException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert mysql_service.name in exc_info.value.detail
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_service_without_port(mysql_service, tasks_api):
    """Reject a service with no port, naming the service in the message."""
    mysql_service.port = None

    with pytest.raises(HTTPBadRequestException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert mysql_service.name in exc_info.value.detail
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_address_without_registered_executor(mysql_service, tasks_api):
    """Reject an unregistered node address, naming that address in the message."""
    tasks_api.get.return_value = {"some-other-nomad": "10.0.0.99"}

    with pytest.raises(HTTPBadRequestException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert mysql_service.node.address in exc_info.value.detail
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwards_upstream_detail_when_host_fetch_raises_http_error(
    mysql_service, tasks_api
):
    """Map an HTTP failure of the host fetch onto 502 carrying the upstream detail."""
    tasks_api.get.side_effect = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Nomad unavailable"
    )

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Nomad unavailable" in exc_info.value.detail
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_maps_unreachable_host_fetch_onto_bad_gateway(mysql_service, tasks_api):
    """Map a transport failure of the host fetch onto 502 without leaking internals."""
    tasks_api.get.side_effect = ConnectionError("tasks unreachable")

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert "tasks unreachable" not in exc_info.value.detail
    tasks_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwards_upstream_detail_when_probe_raises_http_error(
    mysql_service, tasks_api
):
    """Map an HTTP failure of the probe onto 502 carrying the upstream detail."""
    tasks_api.post.side_effect = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Nomad unavailable"
    )

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert "Nomad unavailable" in exc_info.value.detail


@pytest.mark.asyncio
async def test_maps_unreachable_probe_onto_bad_gateway(mysql_service, tasks_api):
    """Map a transport failure of the probe onto 502 without leaking internals."""
    tasks_api.post.side_effect = ConnectionError("timeout")

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert "timeout" not in exc_info.value.detail
    assert "could not reach" in exc_info.value.detail


@pytest.mark.asyncio
async def test_maps_malformed_upstream_payload_onto_bad_gateway(
    mysql_service, tasks_api
):
    """Convert an unparseable upstream body into 502 rather than a validation error.

    Both this and an unreachable Tasks API are documented as 502s, so the status
    alone cannot tell them apart; only the detail distinguishes a response-shape
    regression from a transport failure.
    """
    tasks_api.post.return_value = {"unexpected": "shape"}

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "could not reach" not in exc_info.value.detail
    assert "unrecognized result" in exc_info.value.detail


@pytest.mark.asyncio
async def test_maps_empty_upstream_body_onto_unrecognized_result(
    mysql_service, tasks_api
):
    """Treat a 204-style empty body as an unrecognized result, not a reachability failure."""
    tasks_api.post.return_value = None

    with pytest.raises(HTTPBadGatewayException) as exc_info:
        await probe_service_connectivity(mysql_service, tasks_api)

    assert "unrecognized result" in exc_info.value.detail


class TestUpstreamDetailAsText:
    """Cover how an upstream HTTP failure's detail reaches the 502 message."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["get", "post"], ids=["host-fetch", "probe"])
    async def test_structured_detail_reaches_502_as_text(
        self, mysql_service, tasks_api, verb: str
    ) -> None:
        """Render a structured upstream detail in the service-scoped message."""
        getattr(tasks_api, verb).side_effect = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "Nomad unavailable"},
        )

        with pytest.raises(HTTPBadGatewayException) as exc_info:
            await probe_service_connectivity(mysql_service, tasks_api)

        assert exc_info.value.detail.endswith(": {'message': 'Nomad unavailable'}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["get", "post"], ids=["host-fetch", "probe"])
    async def test_empty_detail_falls_back_to_status_text(
        self, mysql_service, tasks_api, verb: str
    ) -> None:
        """Name the upstream status rather than ending the message with a blank cause."""
        upstream = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=""
        )
        getattr(tasks_api, verb).side_effect = upstream

        with pytest.raises(HTTPBadGatewayException) as exc_info:
            await probe_service_connectivity(mysql_service, tasks_api)

        assert exc_info.value.detail.endswith(f": {upstream}")
