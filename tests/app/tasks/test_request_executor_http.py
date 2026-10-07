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

"""Real-HTTP coverage of the request-scoped NOMAD executor dependency.

Exercises ``get_request_executor`` through a live ``TestClient`` request --
without overriding the executor dependency -- so FastAPI's resolution of the
``Request`` injection and the ``backend`` query parameter on a ``TaskExecutor``
route is verified at the framework level, not just in unit tests.
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioresponses import aioresponses, CallbackResult
from fastapi import status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import get_current_user
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.main import app as combined_app
from app.tasks.config import tasks_settings
from app.tasks.crud import TaskHistoryManager
from app.tasks.deps import get_session
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.execution.nomad_lifecycle import NomadLifecycle
from app.tasks.main import tasks_app
from app.tasks.models import TaskHistory
from tests.app.asgi_stream import asgi_stream


def _nomad_executor(endpoint: str) -> NomadExecutor:
    """Build an un-entered ``NomadExecutor`` pointed at ``endpoint``.

    :param endpoint: The Nomad endpoint the executor should talk to.
    :return: The executor, ready to be published as a ``NOMAD`` override.
    """
    return NomadExecutor.model_validate({"endpoint": endpoint})


@pytest.fixture
def holder_client(regular_user: CasdoorUser) -> Iterator[TestClient]:
    """Yield a Tasks client whose lifecycle holder serves a stub executor."""
    stub = MagicMock()
    stub.get_hosts = AsyncMock(return_value={"node1": "10.0.0.1"})
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.state.nomad_lifecycle = SimpleNamespace(current=stub)
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}
    if hasattr(tasks_app.state, "nomad_lifecycle"):
        delattr(tasks_app.state, "nomad_lifecycle")


def test_hosts_route_resolves_real_request_executor(holder_client: TestClient) -> None:
    """``GET /hosts/`` reaches the holder's executor through the real dependency."""
    response = holder_client.get("/hosts/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"node1": "10.0.0.1"}


def test_combined_app_resolves_holder_on_mounted_tasks_state(
    regular_user: CasdoorUser,
) -> None:
    """Under the combined app, a mounted ``/api/tasks`` request finds the holder.

    Regression for the mounted-deployment bug: Starlette resolves ``request.app``
    to the mounted ``tasks_app`` for ``/api/tasks/*`` requests, so the
    ``NomadLifecycle`` holder must live on ``tasks_app.state`` (not the parent
    app's state, which the combined lifespan would otherwise receive).
    """
    stub = MagicMock()
    stub.get_hosts = AsyncMock(return_value={"node1": "10.0.0.1"})
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.state.nomad_lifecycle = SimpleNamespace(current=stub)
    try:
        response = TestClient(combined_app).get("/api/tasks/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"node1": "10.0.0.1"}
    finally:
        tasks_app.dependency_overrides = {}
        if hasattr(tasks_app.state, "nomad_lifecycle"):
            delattr(tasks_app.state, "nomad_lifecycle")


NOMAD_ENDPOINT = "http://nomad-drain.example.org"
_ALLOCATION = {"ID": "alloc-1"}


@pytest.mark.asyncio
async def test_file_stream_survives_a_reconcile_mid_transfer(
    mocker: MockerFixture,
    regular_user: CasdoorUser,
    session: AsyncSession,
    created_task_with_history: TaskHistory,
) -> None:
    """Deliver a full file transfer whose executor was retired by a live NOMAD change.

    Runs without a ``get_request_executor`` override, so the request-scoped hold
    is what is exercised. The reconcile is fired while the executor is inside its
    ``stat`` call, before the transfer's second call (the ``readat``) is issued:
    without the request-scoped hold the count would reach zero when ``stat``
    returns and the deferred close would fire before ``readat``.
    """
    release = asyncio.Event()
    stat_started = asyncio.Event()
    history_id = created_task_with_history.id
    payload = b"payload"

    async def held_stat(_url, **_kwargs):
        stat_started.set()
        await release.wait()
        return CallbackResult(
            status=status.HTTP_200_OK, payload={"Size": len(payload), "IsDir": False}
        )

    # The allocation lookup rides the synchronous python-nomad SDK, which
    # ``aioresponses`` cannot intercept; stub it at the same seam the executor's
    # own tests use.
    mock_backend = MagicMock()
    mocker.patch(
        "app.tasks.execution.executors.nomad.models.Nomad",
        return_value=mock_backend,
    )
    mock_backend.allocation.get_allocation.return_value = _ALLOCATION
    created_task_with_history.execution_request = (
        created_task_with_history.execution_request.model_copy(
            update={"tracking": {"allocation_id": _ALLOCATION["ID"]}}
        )
    )
    await TaskHistoryManager.save(session, created_task_with_history)
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"NOMAD": _nomad_executor(NOMAD_ENDPOINT)}
    )

    try:
        async with NomadLifecycle(tasks_app) as holder:
            old = holder.current
            with aioresponses() as nomad:
                nomad.get(
                    f"{NOMAD_ENDPOINT}/v1/client/fs/stat/alloc-1?path=/output/out.txt",
                    callback=held_stat,
                )
                nomad.get(
                    f"{NOMAD_ENDPOINT}/v1/client/fs/readat/alloc-1"
                    f"?path=/output/out.txt&limit=1048576&offset=0",
                    status=status.HTTP_200_OK,
                    body=payload,
                )

                async with asgi_stream(
                    tasks_app,
                    f"/history/{history_id}/file/",
                    query_string=b"path=out.txt",
                ) as response:
                    assert response.status_code == status.HTTP_200_OK
                    await asyncio.wait_for(stat_started.wait(), timeout=10)

                    tasks_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
                        {"NOMAD": _nomad_executor("http://nomad-new.example.org")}
                    )
                    await holder.reconcile()
                    assert holder.current is not old
                    assert old._session is not None

                    release.set()
                    body = await response.drain()
    finally:
        tasks_app.dependency_overrides = {}
        tasks_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    assert body == payload
    assert old._session is None
