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

"""Test that a sweep keeps the clients it started on across a settings rebind."""

import re
from typing import Any

import pytest
from aioresponses import aioresponses, CallbackResult
from pytest_mock import MockerFixture

from app.core.requests.registry import ClientRegistry
from app.extensions.apps.om_inventory.service import sweep
from app.extensions.config import extensions_settings
from tests.app.extensions.apps.om_inventory.conftest import OBSERVED_AT


@pytest.mark.asyncio
async def test_sweep_survives_rebind_between_requests(
    mocker: MockerFixture, isolated_client_registry: ClientRegistry
) -> None:
    """Finish enumeration on the clients the first inventory read retired.

    An empty estate still costs two inventory reads and one tasks read. The first
    read retires both clients, as an override rebind would; the sweep holds them,
    so their close waits for the sweep rather than failing the reads after it.
    """
    resolved = mocker.spy(isolated_client_registry, "get")
    inventory = str(extensions_settings.INVENTORY_ENDPOINT).rstrip("/")
    tasks = str(extensions_settings.TASKS_ENDPOINT).rstrip("/")
    rebinds: list[str] = []

    async def rebind_once(url: Any, **_kwargs: Any) -> CallbackResult:
        if not rebinds:
            rebinds.append(str(url))
            await isolated_client_registry.invalidate(inventory)
            await isolated_client_registry.invalidate(tasks)
        return CallbackResult(body="[]")

    with aioresponses() as upstream:
        for endpoint in (inventory, tasks):
            upstream.get(
                re.compile(rf"{re.escape(endpoint)}/.*"),
                callback=rebind_once,
                repeat=True,
            )
        outcome = await sweep(OBSERVED_AT)

    assert outcome.total == 0
    clients = resolved.spy_return_list
    assert {str(client.endpoint).rstrip("/") for client in clients} == {
        inventory,
        tasks,
    }
    assert all(client._session is None for client in clients)
