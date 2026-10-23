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

"""Share the scaffolding more than one om_inventory test module needs."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from datetime import timedelta
from typing import Protocol
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.apps.om_inventory.dispatch import HostProbeResult
from app.extensions.apps.om_inventory.enumeration import InventoryHost
from app.extensions.apps.om_inventory.mapping import ExecutorState, MappedService
from app.extensions.apps.om_inventory.models import NodeResolution
from app.extensions.apps.om_inventory.service import (
    sweep,
    SweepOutcome,
    SweepProgress,
)
from app.extensions.deps import (
    get_current_user,
    get_session,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app

#: The app's mount point, which every route in these tests hangs off.
BASE = "/api/apps/om_inventory"

#: A ``TASK_TIMEOUT`` a ``PositiveInt`` accepts but ``timedelta`` cannot hold.
BEYOND_TIMEDELTA_SECONDS = (timedelta.max.days + 1) * 86_400

#: A host's free data-directory space, as ``collect_install_readiness`` would report it.
FREE_BYTES = 107374182400

#: The Nomad client the dispatch suites probe, named as PMM registers it.
HOST = "replicaset-cluster-node00"

#: The stored failure detail's length limit. Pinned here rather than imported, so a
#: change to the cap fails a test instead of passing silently.
ERROR_DETAIL_CAP = 500

#: One resolved, one answered: a stubbed sweep ``terminal_status`` reads as a clean
#: ``SUCCESS``, so a run that ends in any other status was changed by something else.
CLEAN_OUTCOME = SweepOutcome(resolved=1, answered=1)

#: The module whose boundaries the sweep tests patch.
SERVICE = "app.extensions.apps.om_inventory.service"

#: When the sweep :func:`run_sweep` runs began, the ``collected_at`` a record without its
#: own falls back to.
OBSERVED_AT = "2026-08-12T12:00:00+00:00"


class RunSweep(Protocol):
    """The runner :func:`run_sweep` returns, typed so a wrong call fails ty."""

    def __call__(
        self,
        mapped_services: list[MappedService],
        host_results: dict[str, HostProbeResult],
        hosts: list[InventoryHost] | None = None,
        progress: SweepProgress | None = None,
    ) -> Awaitable[SweepOutcome]:
        """Run the sweep over this mapping and these probe results.

        :param mapped_services: What the mapping resolved.
        :param host_results: Each executor's probe result.
        :param hosts: The enumerated hosts; one per executor in the results if
            omitted.
        :param progress: Where the sweep reports progress, if anywhere.
        :return: The sweep's outcome.
        """
        ...


def host(
    name: str, *, executor: str | None = None, orphaned: bool = False
) -> InventoryHost:
    """Build one enumerated host.

    :param name: The host's name, which is also its node id here.
    :param executor: The executor serving it, matched by address, or ``None`` for
        one of its own name.
    :param orphaned: Whether no executor matched it, so nothing could run there.
    :return: The host.
    """
    if orphaned:
        return InventoryHost(
            node_id=name,
            name=name,
            address=None,
            executor_host=None,
            resolution=NodeResolution.ORPHANED,
        )
    return InventoryHost(
        node_id=name,
        name=name,
        address=None,
        executor_host=executor or name,
        resolution=NodeResolution.ADDRESS if executor else NodeResolution.NAME,
        executor_state=ExecutorState(
            executor or name, "10.0.0.1", reachable=True, driver_healthy=True
        ),
    )


@pytest.fixture
def run_sweep() -> RunSweep:
    """Return a runner for :func:`sweep` with everything above the mapping stubbed.

    Everything above the mapping is I/O - two authenticated clients, the inventory
    and Nomad - so it is replaced wholesale; what is under test is what the sweep
    concludes from a mapping and its probe results, and what it reports while it
    runs. The stubbed ``probe_all`` hands each result to ``on_host_done`` before
    returning them all, as the real one does.

    :return: An awaitable runner returning the sweep's outcome, called as
        ``(mapped_services, host_results, hosts=None, progress=None)``. ``hosts``
        defaults to one per executor the results mention, which is what the real
        enumeration would have produced for them.
    """

    async def run(
        mapped_services: list[MappedService],
        host_results: dict[str, HostProbeResult],
        hosts: list[InventoryHost] | None = None,
        progress: SweepProgress | None = None,
    ) -> SweepOutcome:
        if hosts is None:
            hosts = [host(name) for name in host_results]
        # `auth` is a sync context manager setting a header for its block, so the
        # stub clients have to be usable in a `with`, not merely present.
        clients = (MagicMock(), MagicMock())
        for client in clients:
            client.auth.return_value = nullcontext()

        async def probe_all(
            *_args: object,
            on_host_done: Callable[[HostProbeResult], Awaitable[None]],
            **_kwargs: object,
        ) -> dict[str, HostProbeResult]:
            for result in host_results.values():
                await on_host_done(result)
            return host_results

        with (
            patch(f"{SERVICE}._build_clients", AsyncMock(return_value=clients)),
            patch(f"{SERVICE}.get_internal_token", return_value="token"),
            patch(f"{SERVICE}.list_mongodb_services", AsyncMock(return_value=[])),
            # The host half of enumeration, stubbed empty for the same reason as the
            # service half: the hosts it would write have their own tests in
            # test_enumeration.py.
            patch(f"{SERVICE}.list_inventory_nodes", AsyncMock(return_value=[])),
            patch(f"{SERVICE}.build_hosts", return_value=hosts),
            patch(f"{SERVICE}.get_executor_states", AsyncMock(return_value={})),
            patch(f"{SERVICE}.map_services", return_value=mapped_services),
            patch(f"{SERVICE}.probe_all", probe_all),
        ):
            return await sweep(OBSERVED_AT, progress=progress)

    return run


def log_line(step: str, stream: str, msg: str) -> str:
    """Build one line of the tasks API's log stream.

    :param step: The step that wrote it.
    :param stream: ``stdout`` or ``stderr``.
    :param msg: What it wrote.
    :return: The NDJSON line.
    """
    return json.dumps({"step": step, "type": stream, "msg": msg})


@asynccontextmanager
async def _client_as(
    user: CasdoorUser, session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated as ``user`` and sharing the test session.

    Async rather than a sync ``TestClient`` so a test can await a database read after
    the request — asserting the cascade on delete needs the request and the check to
    run on one event loop and one session.

    :param user: The authenticated user.
    :param session: The database session the routes should use.
    :return: The client.
    """
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: user
    extensions_app.dependency_overrides[get_session] = lambda: session
    client = AsyncClient(
        transport=ASGITransport(app=extensions_app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    )
    try:
        yield client
    finally:
        await client.aclose()
        extensions_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def api(
    regular_user: CasdoorUser, session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated as a regular user.

    :param regular_user: The authenticated user.
    :param session: The database session the routes should use.
    :return: The client.
    """
    async with _client_as(regular_user, session) as client:
        yield client


@pytest_asyncio.fixture
async def admin_api(
    admin_user: CasdoorUser, session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated as an admin, for the admin-gated routers.

    :param admin_user: The authenticated admin.
    :param session: The database session the routes should use.
    :return: The client.
    """
    async with _client_as(admin_user, session) as client:
        yield client
