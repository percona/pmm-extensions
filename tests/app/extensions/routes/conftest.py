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

"""Shared pytest fixtures for ``app.extensions.routes`` tests."""

from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from faker import Faker

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.requests import RemoteAPI
from app.extensions.deps import get_current_user, get_task_history, get_tasks_client
from app.extensions.main import extensions_app
from app.tasks.models import (
    Task,
    TaskExecutionRequest,
    TaskHistoryResponse,
    TaskHistoryStatusEnum,
)
from tests.app.factories import TaskFactory

TASKS_ENDPOINT = "http://tasks.example.org"


@pytest.fixture
def created_task() -> Task:
    """Return a fake created task."""
    return TaskFactory.build()


@pytest.fixture
def task_history_response(faker: Faker, created_task: Task) -> TaskHistoryResponse:
    """Return a fake task history response."""
    started_at = faker.past_datetime(start_date="-15d")
    return TaskHistoryResponse(
        id=faker.random_int(min=1),
        execution_request=TaskExecutionRequest(
            task="example-task",
            target="example-target",
            meta={"key": "value"},
            tracking={"allocation_id": "12345", "evaluation_id": "67890"},
        ),
        status=TaskHistoryStatusEnum.SUCCESS,
        task=created_task,
        started_at=started_at,
        finished_at=started_at + faker.time_delta(end_datetime="+1h"),
        executed_by=None,
    )


@pytest.fixture
def real_client_route_overrides(
    task_history_response: TaskHistoryResponse, regular_user: CasdoorUser
) -> Generator[None]:
    """Stub the routes' own dependencies, leaving the client one live.

    Tests of the request-scoped client hold must let ``get_tasks_client`` run,
    so they stub the task history and the current user only. The token is set on
    ``regular_user`` rather than on a stand-in user, because ``async_test_client``
    installs that same object as the ``get_current_user`` override and either
    fixture may be the one to set it up last.
    """
    regular_user.access_token = "test-token"
    extensions_app.dependency_overrides[get_task_history] = lambda: (
        task_history_response
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: regular_user
    yield
    extensions_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def app_state_tasks_client(
    real_client_route_overrides: None,
) -> AsyncGenerator[RemoteAPI]:
    """Publish a real ``app.state.tasks_api``: the standalone deployment shape.

    :return: The client the PMM Extensions routes will resolve.
    """
    client = await RemoteAPI(endpoint=TASKS_ENDPOINT).open()
    extensions_app.state.tasks_api = client
    yield client
    del extensions_app.state.tasks_api
    await client.close()


async def resolve_registry_tasks_client() -> RemoteAPI:
    """Return the registry-cached Tasks client the PMM Extensions routes would resolve.

    Goes through the dependency itself rather than rebuilding it from copied
    kwargs, so the registry entry a test evicts is the one the routes will hit.
    Requires ``extensions_settings.TASKS_ENDPOINT`` to already point at the stubbed
    upstream and no ``app.state.tasks_api`` to be published.

    :return: The registry-cached client.
    """
    resolver = get_tasks_client(SimpleNamespace(app=extensions_app))
    client = await anext(resolver)
    await resolver.aclose()
    return client
