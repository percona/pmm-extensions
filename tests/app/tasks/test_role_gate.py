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

"""Define tests for the unsafe-method role gate on the Tasks sub-app."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import deps as api_deps
from app.core.celery.deps import get_session as get_celery_beat_session
from app.core.config import settings
from app.core.settings_override.constants import TASKS_SETTINGS
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.models import BaseExecutor
from app.tasks.main import tasks_app
from app.tasks.models import TaskHistoryStatusEnum, TaskWrite
from tests.app.factories import TaskFactory

BEARER_HEADERS = {"Authorization": "Bearer valid_token"}
SERVICE_TOKEN = "supersecret"


@pytest.fixture
def bearer_client(
    session: AsyncSession,
    celery_beat_session: AsyncSession,
    mock_executor: AsyncMock,
    casdoor_mock,
) -> Iterator[TestClient]:
    """Yield a Tasks TestClient that authenticates every request by Bearer token.

    No authentication dependency is overridden: the gate resolves the user
    imperatively, so an override could not reach it, and leaving the chain real
    is what makes the gate the thing under test.

    The celery-beat session is overridden so a periodic route answers from the
    in-memory beat tables. Without it that route raises on a missing table, and
    an assertion that the gate let the request through cannot tell a route's own
    answer from a failure on the way to it.
    """
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_celery_beat_session] = (
        lambda: celery_beat_session
    )
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app, raise_server_exceptions=False)
    tasks_app.dependency_overrides = {}


@pytest.fixture
def admin_bearer_client(
    bearer_client: TestClient, casdoor_mock, casdoor_user_data
) -> TestClient:
    """Return the Bearer client whose credential resolves to an admin."""
    casdoor_mock.get_user.return_value = {**casdoor_user_data, "is_admin": True}
    return bearer_client


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/"),
        ("PUT", "/periodic/1"),
        ("POST", "/connectivity-check/"),
    ],
    ids=["tasks", "periodic", "connectivity"],
)
def test_mutations_are_refused_for_a_non_admin(
    bearer_client: TestClient, method: str, path: str
) -> None:
    """Refuse a non-admin's mutation on each router the gate now covers."""
    response = bearer_client.request(method, path, json={}, headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("POST", "/", status.HTTP_422_UNPROCESSABLE_CONTENT),
        ("PUT", "/periodic/1", status.HTTP_404_NOT_FOUND),
        ("POST", "/connectivity-check/", status.HTTP_422_UNPROCESSABLE_CONTENT),
    ],
    ids=["tasks", "periodic", "connectivity"],
)
def test_mutations_pass_the_gate_for_an_admin(
    admin_bearer_client: TestClient, method: str, path: str, expected_status: int
) -> None:
    """Admit an admin's mutation on each router, leaving the route to answer.

    The answer each route gives the empty body is the assertion. "Not 403" also
    passes when the request never reached the handler, which is how a route that
    raises on the way in reads as an admitted mutation.
    """
    response = admin_bearer_client.request(
        method, path, json={}, headers=BEARER_HEADERS
    )

    assert response.status_code == expected_status


def test_reads_are_unaffected_for_a_non_admin(bearer_client: TestClient) -> None:
    """Serve a non-admin's list request unchanged — the gate is method-scoped."""
    response = bearer_client.get("/", headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_200_OK


def test_the_service_principal_is_still_refused_by_a_route_admin_check(
    bearer_client: TestClient, mocker: MockerFixture
) -> None:
    """Refuse the principal on a route carrying its own ``IsAdminDep``."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.patch(
        f"/admin/settings/{TASKS_SETTINGS}",
        json={"overrides": {}},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_the_service_principal_can_still_dispatch_an_execution(
    bearer_client: TestClient, session: AsyncSession, mocker: MockerFixture
) -> None:
    """Dispatch a task as the principal, which scheduled execution depends on.

    The concrete 200 and the dispatched row are the assertion — "not 403" would
    also pass on a 401 or a 500, which is exactly the silent breakage this gate
    risks for the scheduled writer.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="principal-task", anonymize_mask=0)
        ),
    )

    async def fake_dispatch_queue_item(queue_item, passed_session):
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        return await TaskHistoryManager.save(
            passed_session, queue_item, flag_modified_fields=["execution_request"]
        )

    fake_executor = MagicMock(spec=BaseExecutor)
    fake_executor.get_hosts.return_value = {"node1": "10.0.0.1"}
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=fake_executor)
    mocker.patch(
        "app.tasks.routes.dispatch_queue_item", side_effect=fake_dispatch_queue_item
    )

    response = bearer_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1"},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == TaskHistoryStatusEnum.RUNNING.value


def test_the_log_stream_reconciliation_is_refused_for_a_non_admin(
    bearer_client: TestClient, created_task_with_history
) -> None:
    """Refuse the task-history reconciliation for a non-admin.

    It is a genuine write — it persists ``status``, ``started_at`` and
    ``finished_at`` — so it stays gated rather than joining the exemption
    allowlist. The PMM Extensions log stream that triggers it is open to any authenticated
    user, which is why that caller sends the internal token instead.
    """
    response = bearer_client.post(
        f"/history/{created_task_with_history.id}/sync/", headers=BEARER_HEADERS
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_the_log_stream_reconciliation_is_accepted_for_the_service_principal(
    bearer_client: TestClient,
    created_task_with_history,
    mock_executor: AsyncMock,
    mocker: MockerFixture,
) -> None:
    """Accept the same reconciliation when it carries the internal token.

    This is the identity the PMM Extensions log stream sends, so a non-admin's stream still
    reaches its finish frame.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    mock_executor.sync_task_history.return_value = created_task_with_history

    response = bearer_client.post(
        f"/history/{created_task_with_history.id}/sync/",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_200_OK


def test_the_batch_read_stays_reachable_for_a_non_admin(
    bearer_client: TestClient,
) -> None:
    """Serve the exempted batch-read POST to a non-admin, with its projection.

    Every ``TaskExecutionApp`` list page resolves task statuses through this
    route using the end user's own bearer, and a failed chunk degrades to
    ``None`` rather than erroring — so losing the exemption blanks out every
    status instead of raising. Asserting the body rather than the status is what
    catches that: an empty response would also be 200.
    """
    response = bearer_client.post(
        "/history/latest", json={"names": ["absent-task"]}, headers=BEARER_HEADERS
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"absent-task": None}


def test_a_gated_mutation_resolves_the_credential_once(
    admin_bearer_client: TestClient, casdoor_mock
) -> None:
    """Resolve one credential once across the gate and the route's own dependency.

    The status pins that the request reached the handler, so both consumers ran.
    """
    casdoor_mock.introspect_token.reset_mock()
    casdoor_mock.get_user.reset_mock()

    response = admin_bearer_client.put("/no-such-task", json={}, headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert casdoor_mock.introspect_token.await_count == 1
    assert casdoor_mock.get_user.await_count == 1


def test_the_waived_batch_read_still_resolves_once(
    bearer_client: TestClient, casdoor_mock, mocker: MockerFixture
) -> None:
    """Keep the waived batch read at the one resolution its own dependency makes.

    Its ``UserRole.NONE`` minimum is answered before the gate looks at the
    credential, so the route's ``IsAuthenticatedDep`` is the only thing resolving
    it — and stays so for a non-admin.

    A gate that did reach the credential would be served the route's resolution
    from the cache, leaving the provider counts at one either way; the spy on the
    name the gate looks up at call time is what sees the waiver, since the route
    holds the original captured at import.
    """
    casdoor_mock.introspect_token.reset_mock()
    casdoor_mock.get_user.reset_mock()
    gate = mocker.spy(api_deps, "get_current_user")

    response = bearer_client.post(
        "/history/latest", json={"names": ["absent-task"]}, headers=BEARER_HEADERS
    )

    assert response.status_code == status.HTTP_200_OK
    assert gate.await_count == 0
    assert casdoor_mock.introspect_token.await_count == 1
    assert casdoor_mock.get_user.await_count == 1


def test_the_health_probe_resolves_no_credential(
    bearer_client: TestClient, casdoor_mock
) -> None:
    """Keep the liveness probe unauthenticated, resolving nothing at all."""
    casdoor_mock.introspect_token.reset_mock()

    response = bearer_client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    casdoor_mock.introspect_token.assert_not_awaited()
