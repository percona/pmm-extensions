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

"""Define test cases for the task routes in the FastAPI application."""

import base64
import gzip
import json as json_lib
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

from app.api.deps import (
    get_current_user,
    require_minimum_role_for_unsafe_methods,
    SERVICE_PRINCIPAL_ID,
)
from app.core.celery.deps import get_session as get_celery_beat_session
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.encryption import is_encrypted
from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.core.pmm import _background_tasks
from app.core.utils import utc_now
from app.core.utils.date_time import make_datetime_utc
from app.extensions.apps.archives.alerts import ALERT_DETAIL_BUILDER
from app.extensions.apps.mysql_backups.recorder import RUN_RESULT_RECORDER
from app.tasks import hook_resolver
from app.tasks.config import PreExecutionCheckMode, tasks_settings
from app.tasks.connectivity.models import ConnectivityServiceType
from app.tasks.connectivity.service import _cached_check_connectivity
from app.tasks.crud import TaskHistoryLogManager, TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.exceptions import TaskNotStartedInExecutorError
from app.tasks.execution.executors.nomad.exceptions import (
    AllocationNotFoundError,
    NomadRequestError,
)
from app.tasks.execution.executors.nomad.steps import (
    NomadStep,
    RUN_SCRIPT_OUTPUT_FILES_PATH,
)
from app.tasks.execution.models import BaseExecutor
from app.tasks.execution_request_secrets import (
    ENCRYPTED_META_KEYS,
    PAYLOAD_LEAF,
)
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.main import tasks_app
from app.tasks.models import (
    ExecutionEvent,
    ExecutorHostState,
    LogCaptureStatusEnum,
    MAX_FAILURE_REASON_LENGTH,
    SYSTEM_USER,
    Task,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.factories import build_task_history, TaskFactory
from tests.app.tasks.conftest import (
    HOOK_PATH_FIELDS,
    overwrite_execution_request,
    REJECTED_HOOK_PATHS,
    stored_execution_request,
    undecryptable_document,
)

MOCK_FILE_SIZE = 1024
# What ``app.tasks.main.nomad_exception_handler`` answers with. The routes no
# longer phrase their own 502 detail, so this is the one a client now sees.
NOMAD_UNREACHABLE_DETAIL = (
    "Failed to get a response from Nomad, make sure the agent is online."
)
# Derived rather than spelled out: ``_chain_on_failure`` chains on any terminal
# status but SUCCESS, so a literal list silently stops covering the policy the
# moment a terminal status is added.
NON_SUCCESS_TERMINAL_STATUSES = sorted(
    status
    for status in TaskHistoryStatusEnum
    if status.is_terminal() and status is not TaskHistoryStatusEnum.SUCCESS
)
PAGINATION_TASK_COUNT = 3
PARENT_FILTER_TASK_COUNT = 3
SEARCH_MATCH_TOTAL = 2


@pytest_asyncio.fixture
async def created_task(session) -> Task:
    """Return a fake created task saved in the database."""
    return await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="test-task")),
    )


@pytest.mark.asyncio
async def test_list_tasks_only_returns_active(test_client, session, created_task):
    """Assert listing tasks only returns active (non-deleted) tasks."""
    response = test_client.get("/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert [task["name"] for task in data["items"]] == [created_task.name]
    assert data["total"] == 1
    assert data["offset"] == 0
    assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    await TaskManager.delete_by_name(session, created_task.name)

    response = test_client.get("/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_task_active_and_get_task_deleted_404(
    test_client, session, created_task
):
    """Assert retrieving an active task works and a deleted task returns 404."""
    response = test_client.get(f"/{created_task.name}")
    assert response.status_code == status.HTTP_200_OK
    task_data = response.json()
    assert "name" in task_data
    assert task_data["name"] == created_task.name

    await TaskManager.delete_by_name(session, created_task.name)

    response = test_client.get("/baz")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_delete_task_success_and_cannot_delete_twice(
    mocker, test_client, created_task
):
    """Assert deleting a task works and cannot be deleted twice."""
    mocker.patch("app.tasks.routes.PeriodicTaskManager.delete_where", return_value=True)
    response = test_client.delete(f"/{created_task.name}")
    assert response.status_code == status.HTTP_200_OK
    task_data = response.json()
    assert "name" in task_data
    assert task_data["name"] == created_task.name

    response = test_client.delete(f"/{created_task.name}")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_delete_task_forbidden_when_protected(test_client, session):
    """Assert deleting a protected task returns 403 Forbidden."""
    task_name = "protected-task"
    await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name=task_name, protected=True)),
    )

    resp = test_client.delete(f"/{task_name}")
    assert resp.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_delete_running_task_returns_409(test_client, session, created_task):
    """Assert deleting a task with a running execution returns 409, not 500.

    ``PeriodicTaskManager.delete_where`` is deliberately left unmocked and its
    session pointed at the test database, which has no ``celery_periodictask``
    table: if the running guard failed to short-circuit, the delete would fall
    through to that call and surface the reported 500. A clean 409 therefore
    proves the guard fires at the HTTP boundary before any downstream that could
    raise.
    """
    tasks_app.dependency_overrides[get_celery_beat_session] = lambda: session
    await TaskHistoryManager.save(
        session,
        build_task_history(created_task, status=TaskHistoryStatusEnum.RUNNING),
    )

    response = test_client.delete(f"/{created_task.name}")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "running" in response.json()["detail"]


@pytest.mark.asyncio
async def test_delete_pending_task_returns_409(test_client, session, created_task):
    """Assert deleting a task with a pending execution returns 409, not 500.

    Mirrors :func:`test_delete_running_task_returns_409` for the pending branch:
    ``delete_where`` is unmocked against the celery-less test database, so a 409
    (rather than a 500) proves the pending guard short-circuits the delete.
    """
    tasks_app.dependency_overrides[get_celery_beat_session] = lambda: session
    await TaskHistoryManager.save(
        session,
        build_task_history(created_task, status=TaskHistoryStatusEnum.PENDING),
    )

    response = test_client.delete(f"/{created_task.name}")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "pending" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_task_success(test_client):
    """Assert creating a valid task returns 201."""
    task_data = TaskFactory.build(name="new-task")
    payload = TaskWrite.model_validate(task_data).model_dump(mode="json")
    response = test_client.post("/", json=payload)
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["name"] == "new-task"


@pytest.mark.asyncio
async def test_create_task_persists_run_result_recorder(test_client):
    """Assert a created task's run_result_recorder round-trips through the POST body."""
    task_data = TaskFactory.build(
        name="recorder-task", run_result_recorder="app.extensions.apps.pkg.mod:recorder"
    )
    payload = TaskWrite.model_validate(task_data).model_dump(mode="json")
    response = test_client.post("/", json=payload)
    assert response.status_code == status.HTTP_201_CREATED
    assert (
        response.json()["run_result_recorder"] == "app.extensions.apps.pkg.mod:recorder"
    )


@pytest.mark.asyncio
async def test_create_task_duplicate_name_conflict(test_client, created_task):
    """Assert creating a task with a duplicate name returns a conflict error."""
    payload = TaskWrite.model_validate(
        TaskFactory.build(name=created_task.name)
    ).model_dump(mode="json")
    response = test_client.post("/", json=payload)
    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_update_task_success(test_client, created_task):
    """Assert updating an existing task returns 201."""
    updated_data = TaskWrite.model_validate(
        TaskFactory.build(name=created_task.name)
    ).model_dump(mode="json")
    updated_data["owner"] = "BACKUPS"
    response = test_client.put(f"/{created_task.name}", json=updated_data)
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["owner"] == "BACKUPS"


@pytest.mark.asyncio
async def test_update_task_not_found(test_client):
    """Assert updating a non-existing task returns 404."""
    payload = TaskWrite.model_validate(
        TaskFactory.build(name="nonexistent")
    ).model_dump(mode="json")
    response = test_client.put("/nonexistent", json=payload)
    assert response.status_code == status.HTTP_404_NOT_FOUND


class TestTaskHookPathAllowList:
    """Cover the hook-path allow-list as enforced across the task write endpoints."""

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    @pytest.mark.parametrize("hook_path", REJECTED_HOOK_PATHS)
    @pytest.mark.asyncio
    async def test_create_rejects_hook_path_outside_allow_list(
        self, test_client: TestClient, field: str, hook_path: str
    ) -> None:
        """Assert POST rejects a hook path the allow-list denies."""
        payload = TaskWrite.model_validate(
            TaskFactory.build(name="evil-task")
        ).model_dump(mode="json")
        payload[field] = hook_path

        response = test_client.post("/", json=payload)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "app.extensions.apps" in response.text

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    @pytest.mark.parametrize("hook_path", REJECTED_HOOK_PATHS)
    @pytest.mark.asyncio
    async def test_update_rejects_hook_path_outside_allow_list(
        self,
        test_client: TestClient,
        created_task: Task,
        field: str,
        hook_path: str,
    ) -> None:
        """Assert PUT rejects a hook path the allow-list denies."""
        payload = TaskWrite.model_validate(
            TaskFactory.build(name=created_task.name)
        ).model_dump(mode="json")
        payload[field] = hook_path

        response = test_client.put(f"/{created_task.name}", json=payload)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert field in response.text

    @pytest.mark.parametrize(
        ("field", "hook_path"),
        [
            ("alert_detail_builder", ALERT_DETAIL_BUILDER),
            ("run_result_recorder", RUN_RESULT_RECORDER),
        ],
    )
    @pytest.mark.asyncio
    async def test_create_accepts_in_tree_hook_path(
        self, test_client: TestClient, field: str, hook_path: str
    ) -> None:
        """Assert a non-admin caller can still create a task stamping a shipped hook."""
        payload = TaskWrite.model_validate(
            TaskFactory.build(name="app-task")
        ).model_dump(mode="json")
        payload[field] = hook_path

        response = test_client.post("/", json=payload)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()[field] == hook_path


async def _seed_running_over_success(session, name: str, finished):
    """Seed ``name`` with an earlier SUCCESS run then a newer in-progress RUNNING.

    :param session: The asynchronous session used to persist the rows.
    :param name: The task name to create history for.
    :param finished: The completion time of the SUCCESS run.
    :return: The created task.
    """
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name=name)),
    )
    for task_status, finished_at in (
        (TaskHistoryStatusEnum.SUCCESS, finished),
        (TaskHistoryStatusEnum.RUNNING, None),
    ):
        await TaskHistoryManager.save(
            session,
            TaskHistory(
                task_id=task.id,
                status=task_status,
                finished_at=finished_at,
                execution_request={
                    "task": task.name,
                    "target": "localhost",
                    "meta": {},
                    "tracking": {"allocation_id": None, "evaluation_id": None},
                },
            ),
        )
    return task


@pytest.mark.asyncio
async def test_latest_task_history_batch(test_client, session):
    """Assert POST /history/latest returns the latest projection per name.

    The newest row is an in-progress RUNNING run (no finish time), while an
    earlier SUCCESS run has one — so the projection reports RUNNING status but
    the prior (max) finish time.
    """
    finished = utc_now() - timedelta(hours=1)
    task = await _seed_running_over_success(session, "route-latest-full", finished)

    response = test_client.post(
        "/history/latest",
        json={"names": [task.name, "route-latest-missing"]},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["route-latest-missing"] is None
    assert body[task.name]["status"] == TaskHistoryStatusEnum.RUNNING.value
    # The newest (RUNNING) row has no finish time; a non-null value proves the
    # projection reports the prior run's max(finished_at), not the newest row.
    assert body[task.name]["finished_at"] is not None
    assert datetime.fromisoformat(body[task.name]["finished_at"]).replace(
        tzinfo=None
    ) == finished.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_latest_task_history_status_rejects_too_many_names(test_client) -> None:
    """Assert POST /history/latest rejects more than 200 task names."""
    response = test_client.post(
        "/history/latest",
        json={"names": [f"task-{index}" for index in range(201)]},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_list_task_history(test_client, created_task_with_history):
    """Assert listing task history returns paginated history records."""
    response = test_client.get("/history/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert data["offset"] == 0
    assert data["limit"] == DEFAULT_PAGINATION_LIMIT
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == created_task_with_history.id


@pytest.mark.asyncio
async def test_list_task_history_reports_null_failure_reason(
    test_client, created_task_with_history
):
    """Assert a row with no recorded reason serializes the key as null, not absent.

    A consumer has to distinguish "no reason recorded" from a missing field, so
    the key is always present.
    """
    response = test_client.get("/history/")
    assert response.status_code == status.HTTP_200_OK
    item = response.json()["items"][0]
    assert "failure_reason" in item
    assert item["failure_reason"] is None


@pytest.mark.asyncio
async def test_task_history_serializes_a_recorded_failure_reason(
    test_client, session, created_task_with_history
):
    """Assert a recorded reason reaches both the list and retrieve payloads."""
    created_task_with_history.set_failure_reason("Step 'run-script' failed.")
    await TaskHistoryManager.save(session, created_task_with_history)

    listed = test_client.get("/history/")
    retrieved = test_client.get(f"/history/{created_task_with_history.id}")

    assert listed.status_code == status.HTTP_200_OK
    assert retrieved.status_code == status.HTTP_200_OK
    assert listed.json()["items"][0]["failure_reason"] == "Step 'run-script' failed."
    assert retrieved.json()["failure_reason"] == "Step 'run-script' failed."


@pytest.mark.asyncio
async def test_create_task_history_normalizes_failure_reason(
    test_client, created_task_with_history
):
    """Assert a caller-supplied reason is collapsed to one line and bounded.

    The create route takes the ``TaskHistory`` table model as its body, so the
    field is settable over HTTP; the bound is a property of the column, not just
    of the reasons PMM Extensions composes. Driven as a real request because only an actual
    POST delivers the body to the handler the way FastAPI does.

    The posted row carries ``failed`` so the fixture models a pair PMM Extensions' own
    writers can produce.
    """
    response = test_client.post(
        "/history/",
        json={
            "task_id": created_task_with_history.task.id,
            "execution_request": {
                "task": created_task_with_history.task.name,
                "target": "node-1",
            },
            "status": "failed",
            "failure_reason": "line one\n  line   two " + ("x" * 900),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    reason = response.json()["failure_reason"]
    assert "\n" not in reason
    assert reason.startswith("line one line two ")
    assert len(reason) == MAX_FAILURE_REASON_LENGTH


@pytest.mark.parametrize(
    "task_status",
    [
        task_status
        for task_status in TaskHistoryStatusEnum
        if task_status.operator_summary() is None
    ],
)
@pytest.mark.asyncio
async def test_create_task_history_rejects_failure_reason_without_summary(
    test_client, created_task_with_history, task_status
):
    """Assert a reason conflicts with a status carrying no operator summary."""
    response = test_client.post(
        "/history/",
        json={
            "task_id": created_task_with_history.task.id,
            "execution_request": {
                "task": created_task_with_history.task.name,
                "target": "node-1",
            },
            "status": task_status.value,
            "failure_reason": "Task execution failed.",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["detail"][0]
    assert "failure_reason" in error["loc"]
    assert "status" in error["msg"]


@pytest.mark.asyncio
async def test_create_task_history_returns_a_serializable_row(
    test_client, created_task_with_history
):
    """Assert the create response carries the joined task and execution request.

    ``save`` re-defers ``execution_request``, so returning its result directly
    made the response model attempt lazy IO from the async context.
    """
    response = test_client.post(
        "/history/",
        json={
            "task_id": created_task_with_history.task.id,
            "execution_request": {
                "task": created_task_with_history.task.name,
                "target": "node-1",
            },
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["task"]["name"] == created_task_with_history.task.name
    assert body["execution_request"]["target"] == "node-1"
    assert body["failure_reason"] is None


@pytest.mark.asyncio
async def test_list_task_history_filter_by_status(
    test_client, created_task_with_history
):
    """Assert filtering task history by status works with pagination."""
    response = test_client.get("/history/", params={"status": "success"})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1

    response = test_client.get("/history/", params={"status": "failed"})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 0
    assert len(data["items"]) == 0


@pytest.mark.asyncio
async def test_get_task_history_by_task_name(test_client, created_task_with_history):
    """Assert retrieving task history by task name returns paginated records."""
    task_name = created_task_with_history.task.name
    response = test_client.get(f"/{task_name}/history/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert data["offset"] == 0
    assert data["limit"] == DEFAULT_PAGINATION_LIMIT
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == created_task_with_history.id


@pytest.mark.asyncio
async def test_get_task_history_by_task_name_filter_by_status(
    test_client, created_task_with_history
):
    """Assert filtering task history by task name and status returns paginated results."""
    task_name = created_task_with_history.task.name
    response = test_client.get(f"/{task_name}/history/", params={"status": "running"})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 0
    assert len(data["items"]) == 0


@pytest.mark.asyncio
async def test_retrieve_task_history_by_id(test_client, created_task_with_history):
    """Assert retrieving task history by ID returns the correct record."""
    history_id = created_task_with_history.id
    response = test_client.get(f"/history/{history_id}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == history_id


@pytest.mark.asyncio
async def test_retrieve_task_history_by_id_not_found(test_client):
    """Assert retrieving a non-existing task history returns 404."""
    response = test_client.get("/history/99999")
    assert response.status_code == status.HTTP_404_NOT_FOUND


async def _seed_legacy_blob(
    session: AsyncSession, history: TaskHistory, legacy: dict
) -> None:
    """Encode ``legacy`` as gzip+base64 and persist it in ``tracking["task_logs"]``.

    Mirrors the seeding pattern used by the stream-logs legacy fallback tests so
    both code paths exercise the same blob shape.
    """
    encoded = base64.b64encode(gzip.compress(json_lib.dumps(legacy).encode())).decode()
    history.execution_request.tracking["task_logs"] = encoded
    await TaskHistoryManager.save(
        session, history, flag_modified_fields=["execution_request"]
    )


@pytest.mark.asyncio
async def test_list_task_history_has_logs_false_by_default(
    test_client, created_task_with_history
):
    """Assert ``has_logs`` is ``False`` when no chunks and no legacy blob exist."""
    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["has_logs"] is False


@pytest.mark.asyncio
async def test_list_task_history_has_logs_true_from_chunk_store(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is ``True`` when the chunk store has rows."""
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["has_logs"] is True


@pytest.mark.asyncio
async def test_list_task_history_has_logs_true_from_legacy_blob(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is ``True`` when only the legacy blob is present."""
    await _seed_legacy_blob(
        session,
        created_task_with_history,
        {"run-script": {"stdout": "legacy stdout", "stderr": ""}},
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["has_logs"] is True


@pytest.mark.asyncio
async def test_list_task_history_has_logs_mixed_rows(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is independently populated per row (chunk / legacy / none)."""
    task = created_task_with_history.task
    with_chunks = await TaskHistoryManager.save(
        session,
        TaskHistory(
            task_id=task.id,
            execution_request={
                "task": task.name,
                "target": "node2",
                "meta": {"target": "node2"},
                "tracking": {"allocation_id": None, "evaluation_id": None},
            },
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by="test-user",
        ),
    )
    without_logs = await TaskHistoryManager.save(
        session,
        TaskHistory(
            task_id=task.id,
            execution_request={
                "task": task.name,
                "target": "node3",
                "meta": {"target": "node3"},
                "tracking": {"allocation_id": None, "evaluation_id": None},
            },
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by="test-user",
        ),
    )
    await TaskHistoryLogWriter.append(
        session,
        with_chunks.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )
    await _seed_legacy_blob(
        session,
        created_task_with_history,
        {"run-script": {"stdout": "legacy", "stderr": ""}},
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    items_by_id = {item["id"]: item for item in response.json()["items"]}
    assert items_by_id[with_chunks.id]["has_logs"] is True
    assert items_by_id[created_task_with_history.id]["has_logs"] is True
    assert items_by_id[without_logs.id]["has_logs"] is False


@pytest.mark.asyncio
async def test_list_task_history_empty_does_not_crash(test_client):
    """Assert the empty-list case returns 200 and no SQL from ``ids_with_chunks``."""
    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_list_task_history_excludes_internal_rows_before_pagination(
    test_client, session
) -> None:
    """Assert ``exclude_internal=true`` omits internal task names before applying ``limit``.

    Creates two user-facing tasks followed by one internal task
    (``inventory-sync``). With ``limit=2`` and no filtering, the newest internal
    row occupies one page slot. With ``exclude_internal=true`` and ``limit=2``,
    both user-facing rows are returned and the total reflects the filtered count.
    """
    user_task_a = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="user-task-a")),
    )
    user_task_b = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="user-task-b")),
    )
    internal_task = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="inventory-sync")),
    )

    # Distinct created_at values: utc_now() zeroes microseconds, so same-second
    # inserts tie and the id ASC tie-breaker would keep the newest internal row
    # off the first page under -created_at.
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for task, created_at in (
        (user_task_a, base),
        (user_task_b, base + timedelta(minutes=1)),
        (internal_task, base + timedelta(minutes=2)),
    ):
        history = build_task_history(task)
        history.created_at = created_at
        await TaskHistoryManager.save(session, history)

    unfiltered_response = test_client.get("/history/", params={"limit": 2})
    assert unfiltered_response.status_code == status.HTTP_200_OK
    unfiltered_names = {
        item["task"]["name"] for item in unfiltered_response.json()["items"]
    }
    assert "inventory-sync" in unfiltered_names

    response = test_client.get(
        "/history/", params={"exclude_internal": "true", "limit": 2}
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    returned_names = {item["task"]["name"] for item in data["items"]}
    expected_user_task_count = 2
    assert "inventory-sync" not in returned_names
    assert "user-task-a" in returned_names
    assert "user-task-b" in returned_names
    assert data["total"] == expected_user_task_count


@pytest.mark.asyncio
async def test_list_task_history_internal_rows_visible_without_flag(
    test_client, session
) -> None:
    """Assert the default ``GET /history/`` still returns internal task rows."""
    internal_task = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="inventory-sync")),
    )

    await TaskHistoryManager.save(session, build_task_history(internal_task))

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    returned_names = {item["task"]["name"] for item in data["items"]}
    assert "inventory-sync" in returned_names


@pytest.mark.asyncio
async def test_list_task_history_excludes_system_run_generic_executor_rows(
    test_client, session
) -> None:
    """Assert ``exclude_internal`` drops system-run generic-executor rows, keeps user ones.

    A ``run-python`` execution by a real user is a snippet run and stays visible;
    the same template run by a system identity (``SYSTEM`` or the service
    principal, e.g. connectivity checks and scheduler syncs) is dropped.
    """
    user_task = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="user-task")),
    )
    run_python = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="run-python")),
    )

    user_snippet_run = build_task_history(run_python)
    user_snippet_run.executed_by = "alice"
    await TaskHistoryManager.save(session, user_snippet_run)

    system_run = build_task_history(run_python)
    system_run.executed_by = SYSTEM_USER
    await TaskHistoryManager.save(session, system_run)

    service_run = build_task_history(run_python)
    service_run.executed_by = str(SERVICE_PRINCIPAL_ID)
    await TaskHistoryManager.save(session, service_run)

    await TaskHistoryManager.save(session, build_task_history(user_task))

    unfiltered = test_client.get("/history/", params={"limit": 10})
    assert unfiltered.status_code == status.HTTP_200_OK
    unfiltered_executors = {item["executed_by"] for item in unfiltered.json()["items"]}
    assert SYSTEM_USER in unfiltered_executors
    assert str(SERVICE_PRINCIPAL_ID) in unfiltered_executors

    response = test_client.get(
        "/history/", params={"exclude_internal": "true", "limit": 10}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    executors = [item["executed_by"] for item in data["items"]]
    names = [item["task"]["name"] for item in data["items"]]
    expected_visible_count = 2
    assert SYSTEM_USER not in executors
    assert str(SERVICE_PRINCIPAL_ID) not in executors
    assert "alice" in executors
    assert names.count("run-python") == 1
    assert "user-task" in names
    assert data["total"] == expected_visible_count


@pytest.mark.asyncio
async def test_get_task_history_populates_has_logs(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is populated on the ``/{task}/history/`` list endpoint."""
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )

    response = test_client.get(f"/{created_task_with_history.task.name}/history/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"][0]["has_logs"] is True


@pytest.mark.asyncio
async def test_retrieve_task_history_populates_has_logs_from_chunk_store(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is ``True`` on the retrieve-by-id endpoint for chunk rows."""
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )

    response = test_client.get(f"/history/{created_task_with_history.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["has_logs"] is True


@pytest.mark.asyncio
async def test_retrieve_task_history_populates_has_logs_from_legacy_blob(
    test_client, session, created_task_with_history
):
    """Assert ``has_logs`` is ``True`` on the retrieve endpoint for legacy-only rows."""
    await _seed_legacy_blob(
        session,
        created_task_with_history,
        {"run-script": {"stdout": "legacy", "stderr": ""}},
    )

    response = test_client.get(f"/history/{created_task_with_history.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["has_logs"] is True


@pytest.mark.asyncio
async def test_retrieve_task_history_has_logs_false_when_neither(
    test_client, created_task_with_history
):
    """Assert ``has_logs`` is ``False`` when neither chunks nor legacy blob exist."""
    response = test_client.get(f"/history/{created_task_with_history.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["has_logs"] is False


@pytest.mark.asyncio
async def test_list_task_history_events(
    test_client, created_task_with_history, mock_executor
):
    """Assert the events endpoint returns executor-provided events (oldest-first order)."""
    dt = utc_now()
    mock_executor.get_events.return_value = [
        ExecutionEvent(
            timestamp=dt,
            event_type="Terminated",
            description="Exit 1",
            step="run-script",
        )
    ]
    response = test_client.get(f"/history/{created_task_with_history.id}/events")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data) == 1
    assert data[0]["type"] == "Terminated"
    assert data[0]["description"] == "Exit 1"
    assert data[0]["step"] == "run-script"
    assert "timestamp" in data[0]
    mock_executor.get_events.assert_called_once()


@pytest.mark.asyncio
async def test_list_task_history_events_empty_by_default(
    test_client, created_task_with_history, mock_executor
):
    """Assert the mock executor returns no events unless configured."""
    response = test_client.get(f"/history/{created_task_with_history.id}/events")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


@pytest.mark.asyncio
async def test_stream_logs_pending_returns_409(
    test_client, session, created_task_with_history
):
    """Assert streaming logs for a PENDING task history returns 409."""
    created_task_with_history.status = TaskHistoryStatusEnum.PENDING
    await TaskHistoryManager.save(session, created_task_with_history)

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_stream_logs_finished_returns_logs(
    test_client, created_task_with_history
):
    """Assert streaming logs for a finished task history returns log content."""
    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_stream_logs_finished_reads_chunks_from_taskhistory_log(
    test_client, session, created_task_with_history
):
    """Assert the finished branch streams from the chunk store when available."""
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk store output",
        force_flush=True,
        producer_offset_after=18,
    )

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_200_OK
    assert "chunk store output" in response.text


@pytest.mark.asyncio
async def test_stream_logs_finished_honors_tail_query(
    test_client, session, created_task_with_history
):
    """Assert the finished branch honours ``?tail=`` on the logs endpoint."""
    payload = "".join(f"line{i}\n" for i in range(20)).encode()
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=payload,
        force_flush=True,
        producer_offset_after=len(payload),
    )

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/?tail=5")
    assert response.status_code == status.HTTP_200_OK
    assert "line15" in response.text
    assert "line19" in response.text
    assert "line14" not in response.text


@pytest.mark.asyncio
async def test_stream_logs_finished_without_tail_returns_full_stream(
    test_client, session, created_task_with_history
):
    """Assert omitting ``tail`` returns the full log stream (backwards compatible)."""
    payload = "".join(f"line{i}\n" for i in range(10)).encode()
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=payload,
        force_flush=True,
        producer_offset_after=len(payload),
    )

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_200_OK
    assert "line0" in response.text
    assert "line9" in response.text


@pytest.mark.asyncio
async def test_stream_logs_finished_tail_covers_entire_log_when_large_enough(
    test_client, session, created_task_with_history
):
    """Assert ``tail`` equal to line count returns the same content as no tail."""
    line_count = 10
    payload = "".join(f"line{i}\n" for i in range(line_count)).encode()
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=payload,
        force_flush=True,
        producer_offset_after=len(payload),
    )

    full_response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    tail_response = test_client.get(
        f"/history/{created_task_with_history.id}/logs/?tail={line_count}"
    )

    assert full_response.status_code == status.HTTP_200_OK
    assert tail_response.status_code == status.HTTP_200_OK
    assert full_response.text == tail_response.text


@pytest.mark.asyncio
async def test_stream_logs_finished_legacy_honors_tail_query(
    test_client, session, created_task_with_history
):
    """Assert ``?tail=`` applies on the legacy blob fallback route path."""
    legacy = {
        "run-script": {
            "stdout": "line0\nline1\nline2\nline3\n",
            "stderr": "",
        }
    }
    encoded = base64.b64encode(gzip.compress(json_lib.dumps(legacy).encode())).decode()
    created_task_with_history.execution_request.tracking["task_logs"] = encoded
    await TaskHistoryManager.save(
        session,
        created_task_with_history,
        flag_modified_fields=["execution_request"],
    )

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/?tail=2")
    assert response.status_code == status.HTTP_200_OK
    assert "line2" in response.text
    assert "line3" in response.text
    assert "line0" not in response.text
    assert "line1" not in response.text


@pytest.mark.asyncio
async def test_stream_logs_tail_invalid_returns_422(
    test_client, created_task_with_history
):
    """Assert non-positive ``tail`` values are rejected at validation time."""
    response = test_client.get(f"/history/{created_task_with_history.id}/logs/?tail=0")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio
async def test_stream_logs_finished_falls_back_to_legacy_blob(
    test_client, session, created_task_with_history
):
    """Assert the finished branch falls back to the legacy blob when no chunks exist."""
    legacy = {"run-script": {"stdout": "legacy from tracking", "stderr": ""}}
    encoded = base64.b64encode(gzip.compress(json_lib.dumps(legacy).encode())).decode()
    created_task_with_history.execution_request.tracking["task_logs"] = encoded
    await TaskHistoryManager.save(
        session,
        created_task_with_history,
        flag_modified_fields=["execution_request"],
    )
    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_200_OK
    assert "legacy from tracking" in response.text


@pytest.mark.asyncio
async def test_stream_logs_running_uses_executor(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert streaming logs for a RUNNING task delegates to executor."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)

    async def mock_stream():
        return
        yield

    mock_executor.stream_logs = MagicMock(return_value=mock_stream())
    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_200_OK
    mock_executor.preflight_stream_logs.assert_called_once_with(
        created_task_with_history
    )


@pytest.mark.asyncio
async def test_stream_logs_running_preflight_allocation_gone_returns_410(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert TaskDataNotFound during preflight returns 410 before streaming starts."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)
    mock_executor.preflight_stream_logs.side_effect = AllocationNotFoundError(
        "No allocations",
        executor_name="nomad",
        resource_type="allocation",
        resource_id='JobID == "j" and EvalID == "e"',
    )
    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")
    assert response.status_code == status.HTTP_410_GONE
    detail = response.json()["detail"]
    assert detail["resource_type"] == "allocation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "not_started_reason",
    [
        "Nomad has not placed an allocation for job j yet",
        "Allocation a has not started a task yet",
    ],
)
async def test_stream_logs_running_not_started_returns_409(
    test_client, session, mock_executor, created_task_with_history, not_started_reason
):
    """Assert a RUNNING history the executor has not started yet answers 409.

    The conflict is decided before streaming starts, so a client can retry it,
    and it stays apart from the 410 an expired run answers.
    """
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)
    mock_executor.preflight_stream_logs.side_effect = TaskNotStartedInExecutorError(
        not_started_reason
    )

    response = test_client.get(f"/history/{created_task_with_history.id}/logs/")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"] == not_started_reason
    mock_executor.stream_logs.assert_not_called()


@pytest.mark.asyncio
async def test_list_files_not_finished_returns_409(
    test_client, session, created_task_with_history
):
    """Assert listing files for a non-finished task returns 409."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)

    response = test_client.get(f"/history/{created_task_with_history.id}/files/")
    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_list_files_no_output_path_returns_400(
    test_client, session, created_task_with_history
):
    """Assert listing files when output_files_path is None returns 400."""
    created_task_with_history.task.output_files_path = None
    await TaskManager.save(session, created_task_with_history.task)

    response = test_client.get(f"/history/{created_task_with_history.id}/files/")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_list_files_success(
    test_client, mock_executor, created_task_with_history
):
    """Assert listing files for a finished task with output path returns file metadata."""
    mock_executor.list_files.return_value = {
        "backup.sql": {"size": MOCK_FILE_SIZE, "is_dir": False}
    }
    response = test_client.get(f"/history/{created_task_with_history.id}/files/")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "backup.sql" in data
    assert data["backup.sql"]["size"] == MOCK_FILE_SIZE


@pytest.mark.asyncio
async def test_stream_file_not_finished_returns_409(
    test_client, session, created_task_with_history
):
    """Assert streaming a file for a non-finished task returns 409."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)

    response = test_client.get(
        f"/history/{created_task_with_history.id}/file/",
        params={"path": "backup.sql"},
    )
    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_stream_file_no_output_path_returns_400(
    test_client, session, created_task_with_history
):
    """Assert streaming a file when output_files_path is None returns 400."""
    created_task_with_history.task.output_files_path = None
    await TaskManager.save(session, created_task_with_history.task)

    response = test_client.get(
        f"/history/{created_task_with_history.id}/file/",
        params={"path": "backup.sql"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_stream_file_success(
    test_client, mock_executor, created_task_with_history
):
    """Assert streaming a file for a finished task returns file content."""

    async def mock_stream():
        yield b"file content"

    mock_executor.stream_file = MagicMock(return_value=mock_stream())
    response = test_client.get(
        f"/history/{created_task_with_history.id}/file/",
        params={"path": "backup.sql"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == b"file content"


@pytest.mark.asyncio
async def test_stop_task_pending_with_celery_id(
    test_client, session, created_task_with_history
):
    """Assert stopping a PENDING task with a celery_task_id revokes and deletes it."""
    created_task_with_history.status = TaskHistoryStatusEnum.PENDING
    created_task_with_history.execution_request.tracking["celery_task_id"] = (
        "celery-123"
    )
    await TaskHistoryManager.save(
        session,
        created_task_with_history,
        flag_modified_fields=["execution_request"],
    )

    with patch("app.tasks.routes.celery") as mock_celery:
        response = test_client.post(f"/history/{created_task_with_history.id}/stop/")

    assert response.status_code == status.HTTP_200_OK
    mock_celery.control.revoke.assert_called_once_with("celery-123")


@pytest.mark.asyncio
async def test_stop_task_pending_without_celery_id(
    test_client, session, created_task_with_history
):
    """Assert stopping a PENDING task without celery_task_id just deletes it."""
    created_task_with_history.status = TaskHistoryStatusEnum.PENDING
    created_task_with_history.execution_request.tracking.pop("celery_task_id", None)
    await TaskHistoryManager.save(
        session,
        created_task_with_history,
        flag_modified_fields=["execution_request"],
    )

    response = test_client.post(f"/history/{created_task_with_history.id}/stop/")
    assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_stop_task_running_calls_executor(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert stopping a RUNNING task calls executor.stop_task."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)

    mock_executor.stop_task.return_value = created_task_with_history
    response = test_client.post(f"/history/{created_task_with_history.id}/stop/")
    assert response.status_code == status.HTTP_200_OK
    mock_executor.stop_task.assert_called_once()


@pytest.mark.asyncio
async def test_stop_task_finished_returns_400(test_client, created_task_with_history):
    """Assert stopping a finished task returns 400."""
    response = test_client.post(f"/history/{created_task_with_history.id}/stop/")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_sync_task_history_running_calls_executor(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert syncing a RUNNING task calls executor.sync_task_history and persists status."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)

    async def fake_sync(item, writer_session=None):
        item.status = TaskHistoryStatusEnum.SUCCESS
        item.finished_at = utc_now()
        return item, None

    mock_executor.sync_task_history = AsyncMock(side_effect=fake_sync)
    response = test_client.post(f"/history/{created_task_with_history.id}/sync/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == TaskHistoryStatusEnum.SUCCESS.value
    mock_executor.sync_task_history.assert_called_once()


@pytest.mark.asyncio
async def test_sync_task_history_not_running_skips_executor(
    test_client, mock_executor, created_task_with_history
):
    """Assert syncing a non-running task returns current status without calling the executor."""
    response = test_client.post(f"/history/{created_task_with_history.id}/sync/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == TaskHistoryStatusEnum.SUCCESS.value
    mock_executor.sync_task_history.assert_not_called()


@pytest.mark.parametrize("running", [False, True], ids=["already_finished", "running"])
@pytest.mark.asyncio
async def test_sync_task_history_populates_has_logs(
    test_client, session, mock_executor, created_task_with_history, running
):
    """Assert the sync endpoint populates ``has_logs`` when chunks exist.

    Covers both the already-finished early-return branch and the RUNNING
    full-sync path. Regression: the sync endpoint short-circuits when the task
    has already finished. Before the fix, the early return skipped
    ``_populate_log_metadata`` so the response always had ``has_logs=False`` even
    when chunks existed, which broke the API contract relative to
    ``GET /history/{id}``.
    """
    if running:
        created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
        await TaskHistoryManager.save(session, created_task_with_history)

    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )

    if running:

        async def fake_sync(item, writer_session=None):
            item.status = TaskHistoryStatusEnum.SUCCESS
            item.finished_at = utc_now()
            return item, None

        # Only the RUNNING path reaches the executor; the early-return branch
        # short-circuits before it, so this wiring is a no-op there.
        mock_executor.sync_task_history = AsyncMock(side_effect=fake_sync)

    response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["has_logs"] is True


@pytest.mark.asyncio
class TestSyncTaskHistoryChainDispatch:
    """Cover chain dispatch and sync-lock semantics on POST /history/{id}/sync/.

    When the PMM Extensions log-stream SSE finishes and posts to the sync route, the
    route must claim the celery sync lock, save the terminal status, and
    dispatch any chained task. Without these, the celery
    ``sync_running_tasks`` periodic loses the race against the HTTP route
    and the chain is silently dropped.
    """

    async def _seed_chain_target(self, session: AsyncSession) -> Task:
        return await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="chain-target")),
        )

    async def _arm_parent_chain(
        self,
        session: AsyncSession,
        parent: TaskHistory,
        chain_target_name: str | None,
        *,
        chain_on_failure: bool = False,
        sync_lock=None,
        chain_value: list[str] | None = None,
    ) -> tuple[TaskHistory, str | None]:
        parent.status = TaskHistoryStatusEnum.RUNNING
        if chain_value is not None:
            parent.execution_request.meta["_chain_task_names"] = chain_value
        elif chain_target_name is not None:
            parent.execution_request.meta["_chain_task_names"] = [chain_target_name]
        if chain_on_failure:
            parent.execution_request.meta["_chain_on_failure"] = True
        parent.sync_in_progress_started_at = sync_lock
        saved = await TaskHistoryManager.save(
            session,
            parent,
            flag_modified_fields=[
                "execution_request",
                "sync_in_progress_started_at",
            ],
        )
        # ``session.refresh`` after save reloads the lock column from sqlite as
        # a naive datetime (sqlite has no native tz). The route's update_where
        # builds an or_(IS NULL, < aware_dt) predicate; SQLAlchemy's
        # synchronize_session evaluator runs that predicate against in-memory
        # objects and would compare naive vs aware. Re-tag the in-memory value
        # to UTC-aware so the evaluator stays happy. Production runs against
        # postgres where the column round-trips tz-aware natively.
        if saved.sync_in_progress_started_at is not None:
            saved.sync_in_progress_started_at = make_datetime_utc(
                saved.sync_in_progress_started_at
            )
        return saved

    @staticmethod
    def _executor_flips_to(status_value: TaskHistoryStatusEnum):
        async def fake_sync(item, writer_session=None):
            item.status = status_value
            item.finished_at = utc_now()
            return item, None

        return fake_sync

    async def test_dispatches_chain_on_running_to_success(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert RUNNING → SUCCESS with chain meta dispatches the chain target."""
        chain_target = await self._seed_chain_target(session)
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target.name
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.SUCCESS)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == TaskHistoryStatusEnum.SUCCESS.value
        mock_chain.assert_awaited_once()
        args = mock_chain.await_args.args
        assert args[0] == chain_target.name
        assert args[2] == []

    @pytest.mark.parametrize("terminal_status", NON_SUCCESS_TERMINAL_STATUSES)
    async def test_dispatches_chain_on_failure_with_flag(
        self,
        test_client,
        session,
        mock_executor,
        created_task_with_history,
        mocker,
        terminal_status,
    ):
        """Assert non-success terminal statuses dispatch the chain when the flag is set."""
        chain_target = await self._seed_chain_target(session)
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target.name,
            chain_on_failure=True,
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(terminal_status)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_chain.assert_awaited_once()

    async def test_no_chain_on_failure_without_flag(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert FAILED without ``_chain_on_failure`` does not dispatch the chain."""
        chain_target = await self._seed_chain_target(session)
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target.name
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.FAILED)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_chain.assert_not_awaited()

    async def test_no_chain_when_still_running(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert chain is not dispatched when the executor returns RUNNING."""
        chain_target = await self._seed_chain_target(session)
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target.name
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.RUNNING)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_chain.assert_not_awaited()

    async def test_no_chain_when_no_chain_task_names(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert chain is not dispatched when ``_chain_task_names`` is unset."""
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target_name=None,
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.SUCCESS)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_chain.assert_not_awaited()

    async def test_no_chain_when_chain_task_names_is_empty_list(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert chain is not dispatched when ``_chain_task_names`` is an empty list."""
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target_name=None,
            chain_value=[],
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.SUCCESS)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_chain.assert_not_awaited()

    async def test_no_chain_when_already_terminal_at_dep_load(
        self, test_client, mock_executor, created_task_with_history, mocker
    ):
        """Assert the early-return branch does not call the executor or dispatch a chain."""
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_executor.sync_task_history.assert_not_called()
        mock_chain.assert_not_awaited()

    async def test_skips_executor_when_lock_held(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert a fresh sync lock causes the route to skip the executor and chain dispatch."""
        chain_target = await self._seed_chain_target(session)
        lock_pin = utc_now()
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target.name,
            sync_lock=lock_pin,
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == TaskHistoryStatusEnum.RUNNING.value
        mock_executor.sync_task_history.assert_not_called()
        mock_chain.assert_not_awaited()
        refreshed = await TaskHistoryManager.get_or_404(
            session, id=created_task_with_history.id
        )
        assert refreshed.sync_in_progress_started_at is not None
        assert make_datetime_utc(refreshed.sync_in_progress_started_at) == lock_pin

    async def test_claims_expired_lock(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert an expired sync lock is reclaimed: executor runs, chain dispatches, lock clears."""
        chain_target = await self._seed_chain_target(session)
        expired = utc_now() - 2 * tasks_settings.SYNC_LOCK_TTL
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target.name,
            sync_lock=expired,
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.SUCCESS)
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        mock_executor.sync_task_history.assert_called_once()
        mock_chain.assert_awaited_once()
        refreshed = await TaskHistoryManager.get_or_404(
            session, id=created_task_with_history.id
        )
        assert refreshed.sync_in_progress_started_at is None

    async def test_clears_lock_after_successful_sync(
        self, test_client, session, mock_executor, created_task_with_history
    ):
        """Assert the lock is cleared in the DB after a successful RUNNING → SUCCESS sync."""
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target_name=None
        )
        mock_executor.sync_task_history = AsyncMock(
            side_effect=self._executor_flips_to(TaskHistoryStatusEnum.SUCCESS)
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        refreshed = await TaskHistoryManager.get_or_404(
            session, id=created_task_with_history.id
        )
        assert refreshed.sync_in_progress_started_at is None

    async def test_lock_held_path_populates_has_logs(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert the lock-held early-return populates ``has_logs`` on the response."""
        chain_target = await self._seed_chain_target(session)
        await self._arm_parent_chain(
            session,
            created_task_with_history,
            chain_target.name,
            sync_lock=utc_now(),
        )
        await TaskHistoryLogWriter.append(
            session,
            created_task_with_history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"chunk output",
            force_flush=True,
            producer_offset_after=12,
        )
        mocker.patch("app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock)

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["has_logs"] is True

    async def test_returns_fresh_status_when_terminal_between_dep_load_and_claim(
        self, test_client, session, mock_executor, created_task_with_history, mocker
    ):
        """Assert the lock-claim's status=RUNNING filter returns the fresh terminal status.

        The dep-loads ``task_history`` while it is RUNNING. Between dep load
        and the lock-claim ``update_where``, the celery sync_running_tasks
        path completes and flips the row to SUCCESS, clearing the lock.
        The route's ``status=RUNNING`` filter rejects the claim
        (rowcount=0); the route then refreshes the in-memory row and
        returns the fresh terminal status rather than the stale RUNNING.
        """
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target_name=None
        )
        original_update_where = TaskHistoryManager.update_where

        async def flip_to_terminal_then_zero(*args, **kwargs):
            await original_update_where(
                session,
                {
                    "status": TaskHistoryStatusEnum.SUCCESS,
                    "sync_in_progress_started_at": None,
                },
                id=created_task_with_history.id,
            )
            return SimpleNamespace(rowcount=0)

        mocker.patch.object(
            TaskHistoryManager,
            "update_where",
            side_effect=flip_to_terminal_then_zero,
        )
        mock_chain = mocker.patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        )

        response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == TaskHistoryStatusEnum.SUCCESS.value
        mock_executor.sync_task_history.assert_not_called()
        mock_chain.assert_not_awaited()

    async def test_clears_lock_when_executor_raises(
        self, test_client, session, mock_executor, created_task_with_history
    ):
        """Assert the sync lock is cleared when ``executor.sync_task_history`` raises.

        Without the cleanup, a transient executor error pins the row's
        ``sync_in_progress_started_at`` until ``SYNC_LOCK_TTL`` elapses,
        blocking both the celery ``sync_running_tasks`` periodic and any
        subsequent UI sync — and silently delaying chain dispatch.
        """
        await self._arm_parent_chain(
            session, created_task_with_history, chain_target_name=None
        )
        mock_executor.sync_task_history = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            test_client.post(f"/history/{created_task_with_history.id}/sync/")

        refreshed = await TaskHistoryManager.get_or_404(
            session, id=created_task_with_history.id
        )
        assert refreshed.sync_in_progress_started_at is None


@pytest.mark.asyncio
async def test_stop_task_running_populates_has_logs(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert the stop endpoint populates ``has_logs`` on the running → stopped response."""
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)
    await TaskHistoryLogWriter.append(
        session,
        created_task_with_history.id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=b"chunk output",
        force_flush=True,
        producer_offset_after=12,
    )
    mock_executor.stop_task.return_value = created_task_with_history

    response = test_client.post(f"/history/{created_task_with_history.id}/stop/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["has_logs"] is True


@pytest.mark.asyncio
async def test_get_task_stats(test_client, created_task_with_history):
    """Assert retrieving task stats returns computed statistics."""
    task_name = created_task_with_history.task.name
    response = test_client.get(f"/stats/{task_name}")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "total" in data
    assert data["total"] == 1
    assert "status" in data
    assert "duration" in data


@pytest.mark.asyncio
async def test_get_task_stats_empty(test_client, created_task):
    """Assert retrieving stats for a task with no history returns zero total."""
    response = test_client.get(f"/stats/{created_task.name}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_executor_hosts(test_client, mock_executor):
    """Assert retrieving executor hosts returns the expected hosts dict."""
    response = test_client.get("/hosts/")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"node1": "10.0.0.1"}


@pytest.mark.asyncio
async def test_get_executor_host_states(test_client, mock_executor):
    """Assert /hosts/states/ reports unusable hosts, which /hosts/ can only omit."""
    mock_executor.get_host_states = AsyncMock(
        return_value=[
            ExecutorHostState(
                name="node1", address="10.0.0.1", reachable=True, driver_healthy=True
            ),
            ExecutorHostState(
                name="node2",
                address="10.0.0.2",
                reachable=True,
                driver_healthy=False,
                status="ready",
                detail="Failed to find raw_exec",
            ),
        ]
    )
    response = test_client.get("/hosts/states/")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [entry["name"] for entry in body] == ["node1", "node2"]
    # The reason travels with the row: an operator asking why node2 takes no jobs
    # should not have to go and read Nomad's own API to find out.
    assert body[1]["driver_healthy"] is False
    assert body[1]["detail"] == "Failed to find raw_exec"


@pytest.mark.asyncio
async def test_get_executor_host_states_unreachable(test_client, mock_executor):
    """Assert /hosts/states/ answers 502 rather than 500 when the backend is down.

    A request that never got an answer arrives as ``NomadRequestError`` with no
    status now that this call is off python-nomad, and the app-level
    ``BaseNomadException`` handler is what turns it into the 502.
    """
    mock_executor.get_host_states = AsyncMock(
        side_effect=NomadRequestError("GET /v1/nodes failed: boom")
    )
    response = test_client.get("/hosts/states/")
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["detail"] == NOMAD_UNREACHABLE_DETAIL


@pytest.mark.asyncio
async def test_get_executor_host_states_nomad_returns_non_json(
    test_client, mock_executor
):
    """Assert /hosts/states/ returns 502 JSON when the body will not parse.

    ``nomad_json`` catches the ``JSONDecodeError`` and re-raises it as a
    ``NomadRequestError``, so the route sees the same shape as a transport
    failure rather than a bare ``ValueError`` that would answer 500.
    """
    mock_executor.get_host_states.side_effect = NomadRequestError(
        "GET /v1/nodes failed: JSONDecodeError('Expecting value')"
    )
    response = test_client.get("/hosts/states/")
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == NOMAD_UNREACHABLE_DETAIL


@pytest.mark.asyncio
async def test_get_executor_hosts_nomad_returns_non_json(test_client, mock_executor):
    """Assert /hosts/ returns 502 JSON when the body will not parse."""
    mock_executor.get_hosts.side_effect = NomadRequestError(
        "GET /v1/nodes failed: JSONDecodeError('Expecting value')"
    )
    response = test_client.get("/hosts/")
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == NOMAD_UNREACHABLE_DETAIL


@pytest.mark.asyncio
async def test_get_executor_hosts_nomad_unreachable(test_client, mock_executor):
    """Assert /hosts/ returns 502 JSON when the request gets no answer."""
    mock_executor.get_hosts.side_effect = NomadRequestError(
        "GET /v1/nodes failed: ClientConnectorError('Connection refused')"
    )
    response = test_client.get("/hosts/")
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == NOMAD_UNREACHABLE_DETAIL


@pytest.mark.asyncio
async def test_transform_payload_proxy_backend(test_client):
    """Assert transforming a payload with PROXY backend calls parse_payload."""
    with patch(
        "app.tasks.routes.parse_payload", return_value={"key": "value"}
    ) as mock_parse:
        response = test_client.post(
            "/transform/",
            json={"payload": '{"key": "value"}', "fmt": "json"},
            params={"backend": "proxy"},
        )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"key": "value"}
    mock_parse.assert_called_once_with('{"key": "value"}', "json")


@pytest.mark.asyncio
async def test_transform_payload_nomad_backend(test_client, mock_executor):
    """Assert transforming a payload with NOMAD backend calls executor."""
    with patch("app.tasks.routes.get_executor", return_value=mock_executor):
        mock_executor.transform_payload.return_value = {"parsed": True}
        response = test_client.post(
            "/transform/",
            json={"payload": '{"job": {}}', "fmt": "json"},
            params={"backend": "nomad"},
        )
    assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_task_data_with_execution_request_keys_returned_as_dict(
    test_client, session
):
    """Test that Task.data with TaskExecutionRequest-like keys is returned as a dict."""
    conflicting_data = {
        "task": "run-python",
        "target": "mariadb",
        "payload": "SELECT 1",
        "meta": {"env": "staging"},
    }
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="conflicting-task", data=conflicting_data),
        ),
    )

    response = test_client.get("/conflicting-task")
    assert response.status_code == status.HTTP_200_OK
    task_json = response.json()
    assert task_json["data"] == conflicting_data
    assert isinstance(task_json["data"], dict)


@pytest.mark.asyncio
async def test_list_tasks_custom_pagination(test_client, session):
    """Assert custom offset and limit are respected for task listing."""
    for i in range(PAGINATION_TASK_COUNT):
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name=f"task-{i}")),
        )

    response = test_client.get("/", params={"offset": 0, "limit": 1})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == 1
    assert data["total"] == PAGINATION_TASK_COUNT
    assert data["offset"] == 0
    assert data["limit"] == 1


@pytest.mark.asyncio
async def test_list_tasks_offset_beyond_total(test_client, created_task):
    """Assert offset beyond total returns empty items with correct total."""
    response = test_client.get("/", params={"offset": 999})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_list_task_history_custom_pagination(
    test_client, created_task_with_history
):
    """Assert custom offset and limit are respected for task history listing."""
    response = test_client.get("/history/", params={"offset": 0, "limit": 1})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == 1
    assert data["total"] == 1

    response = test_client.get("/history/", params={"offset": 999})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_get_task_history_custom_pagination(
    test_client, created_task_with_history
):
    """Assert custom offset and limit are respected for per-task history."""
    task_name = created_task_with_history.task.name
    response = test_client.get(
        f"/{task_name}/history/", params={"offset": 0, "limit": 1}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data["items"]) == 1
    assert data["total"] == 1

    response = test_client.get(f"/{task_name}/history/", params={"offset": 999})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_list_tasks_filter_with_pagination(test_client, session):
    """Assert owner filter works together with pagination."""
    await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="backup-1", owner="BACKUPS")),
    )
    await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="alter-1", owner="ALTERS")),
    )

    response = test_client.get("/", params={"owner": "BACKUPS", "limit": 1})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "backup-1"


@pytest.mark.asyncio
async def test_list_tasks_parent_is_null_filter(test_client, session):
    """Assert parent_is_null query param filters on data.parent."""
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="route-parent",
                data={"backup_type": "pbm_config"},
            )
        ),
    )
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="route-child",
                data={"backup_type": "pbm_logical", "parent": "route-parent"},
            )
        ),
    )

    response = test_client.get("/", params={"parent_is_null": True})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "route-parent"


@pytest.mark.asyncio
async def test_list_tasks_backup_type_filter_with_pagination(test_client, session):
    """Assert backup_type and pagination params compose on GET /."""
    for index in range(PARENT_FILTER_TASK_COUNT):
        await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name=f"route-pbm-config-{index}",
                    data={"backup_type": "pbm_config"},
                )
            ),
        )
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="route-pbm-logical",
                data={"backup_type": "pbm_logical", "parent": "route-pbm-config-0"},
            )
        ),
    )

    common_params = {"parent_is_null": True, "backup_type": "pbm_config"}
    page_1 = test_client.get("/", params={**common_params, "offset": 0, "limit": 1})
    page_2 = test_client.get("/", params={**common_params, "offset": 1, "limit": 1})
    page_3 = test_client.get("/", params={**common_params, "offset": 2, "limit": 1})

    assert page_2.status_code == status.HTTP_200_OK
    data = page_2.json()
    assert data["total"] == PARENT_FILTER_TASK_COUNT
    assert data["offset"] == 1
    assert data["limit"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["data"]["backup_type"] == "pbm_config"
    paginated_names = {
        page_1.json()["items"][0]["name"],
        page_2.json()["items"][0]["name"],
        page_3.json()["items"][0]["name"],
    }
    assert paginated_names == {
        f"route-pbm-config-{index}" for index in range(PARENT_FILTER_TASK_COUNT)
    }


@pytest.mark.asyncio
async def test_list_tasks_self_parent_filter(test_client, session):
    """Assert self_parent query param keeps rows where parent equals task name."""
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="route-self-parent",
                data={"backup_type": "pbm_logical", "parent": "route-self-parent"},
            )
        ),
    )
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="route-child-parent",
                data={"backup_type": "pbm_logical", "parent": "route-self-parent"},
            )
        ),
    )

    response = test_client.get("/", params={"self_parent": True})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "route-self-parent"


@pytest.mark.asyncio
async def test_list_task_history_status_filter_with_pagination(
    test_client, created_task_with_history
):
    """Assert status filter and pagination work together for task history."""
    response = test_client.get("/history/", params={"status": "success", "limit": 1})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1

    response = test_client.get("/history/", params={"status": "failed", "offset": 0})
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_get_task_history_status_filter_with_pagination(
    test_client, created_task_with_history
):
    """Assert status filter and pagination work together for per-task history."""
    task_name = created_task_with_history.task.name
    response = test_client.get(
        f"/{task_name}/history/", params={"status": "success", "limit": 1}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


def _openapi_param_names(path: str) -> set[str]:
    """Return query/path parameter names for a tasks OpenAPI GET path.

    :param path: OpenAPI path template (for example ``/history/``).
    :return: The set of parameter names declared on the GET operation.
    """
    params = tasks_app.openapi()["paths"][path]["get"].get("parameters", [])
    return {param["name"] for param in params}


def _openapi_param(path: str, name: str) -> dict[str, object]:
    """Return one named OpenAPI parameter object for a tasks GET path.

    :param path: OpenAPI path template (for example ``/{task}/history/``).
    :param name: Parameter name to look up (for example ``sort``).
    :return: The matching OpenAPI parameter object.
    :raises StopIteration: If no parameter with ``name`` exists on the path.
    """
    params = tasks_app.openapi()["paths"][path]["get"].get("parameters", [])
    return next(param for param in params if param["name"] == name)


class TestListQueryRouteParams:
    """Cover list-query sort/search on the three tasks-service list endpoints."""

    def test_sort_and_search_params_exposed_on_list_endpoints(self) -> None:
        """Expose ``sort`` and ``search`` on each of the three tasks-service list endpoints."""
        for path in ("/", "/history/", "/{task}/history/"):
            names = _openapi_param_names(path)
            assert "sort" in names
            assert "search" in names

    def test_task_history_endpoints_share_one_sort_search_surface(self) -> None:
        """Drive both TaskHistory list routes from the same sort/search OpenAPI surface."""
        all_history = {
            "sort": _openapi_param("/history/", "sort"),
            "search": _openapi_param("/history/", "search"),
        }
        by_task = {
            "sort": _openapi_param("/{task}/history/", "sort"),
            "search": _openapi_param("/{task}/history/", "search"),
        }
        assert all_history["sort"]["schema"] == by_task["sort"]["schema"]
        assert all_history["sort"].get("default") == by_task["sort"].get("default")
        assert all_history["search"]["schema"] == by_task["search"]["schema"]

    @pytest.mark.asyncio
    async def test_out_of_allowlist_sort_returns_422_on_list_tasks(
        self, test_client
    ) -> None:
        """Reject an out-of-allowlist sort key on GET / with HTTP 422."""
        response = test_client.get("/", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.asyncio
    async def test_out_of_allowlist_sort_returns_422_on_both_history_endpoints(
        self, test_client, created_task_with_history
    ) -> None:
        """Reject an out-of-allowlist sort key on both TaskHistory list endpoints."""
        task_name = created_task_with_history.task.name
        all_history = test_client.get("/history/", params={"sort": "evil"})
        by_task = test_client.get(f"/{task_name}/history/", params={"sort": "evil"})
        assert all_history.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert by_task.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.asyncio
    async def test_list_tasks_search_filters_and_reports_filtered_total(
        self, test_client, session
    ) -> None:
        """Filter GET / by search and report the filtered total, not the page size."""
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="match-alpha")),
        )
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="match-beta")),
        )
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="other-gamma")),
        )

        response = test_client.get(
            "/", params={"search": "match", "offset": 0, "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == SEARCH_MATCH_TOTAL
        assert len(data["items"]) == 1
        assert "match" in data["items"][0]["name"]

    @pytest.mark.asyncio
    async def test_list_tasks_sort_by_name_ascending(
        self, test_client, session
    ) -> None:
        """Apply the mapped ``name`` sort key on GET /."""
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="zeta-route")),
        )
        await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="alpha-route")),
        )

        response = test_client.get("/", params={"sort": "name"})
        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()["items"]] == [
            "alpha-route",
            "zeta-route",
        ]

    @pytest.mark.asyncio
    async def test_list_tasks_search_composes_with_owner_filter(
        self, test_client, session
    ) -> None:
        """Keep the owner base restriction working alongside search on GET /."""
        await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="keep-match", owner="BACKUPS")
            ),
        )
        await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="drop-match", owner="ALTERS")
            ),
        )

        response = test_client.get("/", params={"owner": "BACKUPS", "search": "match"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "keep-match"

    @pytest.mark.asyncio
    async def test_history_search_filters_and_reports_filtered_total(
        self, test_client, session
    ) -> None:
        """Filter both history list endpoints by executed_by search with filtered total."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="history-search-task")),
        )
        for executed_by in ("alice", "alice-ops", "bob"):
            history = build_task_history(task)
            history.executed_by = executed_by
            await TaskHistoryManager.save(session, history)

        all_history = test_client.get(
            "/history/", params={"search": "alice", "limit": 1}
        )
        by_task = test_client.get(
            f"/{task.name}/history/", params={"search": "alice", "limit": 1}
        )
        assert all_history.status_code == status.HTTP_200_OK
        assert by_task.status_code == status.HTTP_200_OK
        assert all_history.json()["total"] == SEARCH_MATCH_TOTAL
        assert by_task.json()["total"] == SEARCH_MATCH_TOTAL
        assert len(all_history.json()["items"]) == 1
        assert len(by_task.json()["items"]) == 1

    @pytest.mark.asyncio
    async def test_history_tie_broken_ordering_is_deterministic(
        self, test_client, session
    ) -> None:
        """Keep TaskHistory list order deterministic across equal created_at values."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="history-tie-task")),
        )
        tied_at = datetime(2026, 1, 1, tzinfo=UTC)
        ids = []
        for status_value in (
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.RUNNING,
        ):
            history = build_task_history(task, status=status_value)
            history.created_at = tied_at
            saved = await TaskHistoryManager.save(session, history)
            ids.append(saved.id)

        response = test_client.get(
            f"/{task.name}/history/", params={"sort": "-created_at"}
        )
        assert response.status_code == status.HTTP_200_OK
        assert [item["id"] for item in response.json()["items"]] == ids


@pytest.mark.asyncio
async def test_execute_task_name_response_serializes_deferred_execution_request(
    test_client, session, mocker
):
    """Assert POST /execute/{task_name} serializes the deferred ``execution_request``.

    Regression for a ``MissingGreenlet`` error: ``TaskHistory.execution_request`` is
    mapped as a deferred column, and ``TaskHistoryManager.save`` expires it via
    ``session.refresh`` after commit. The route must load it eagerly before
    returning, otherwise FastAPI serialization triggers a lazy load after the
    async session has closed.
    """
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="execute-task", anonymize_mask=0)
        ),
    )

    async def fake_dispatch_queue_item(queue_item, passed_session):
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        return await TaskHistoryManager.save(
            passed_session,
            queue_item,
            flag_modified_fields=["execution_request"],
        )

    fake_executor = MagicMock(spec=BaseExecutor)
    fake_executor.get_hosts.return_value = {"node1": "10.0.0.1"}
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=fake_executor)
    mocker.patch(
        "app.tasks.routes.dispatch_queue_item",
        side_effect=fake_dispatch_queue_item,
    )

    response = test_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1"},
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == TaskHistoryStatusEnum.RUNNING.value
    assert data["execution_request"]["task"] == task.name
    assert data["execution_request"]["target"] == "node1"


@pytest.mark.asyncio
async def test_execute_task_name_unresolvable_payload_fails_terminally(
    test_client, session, mocker
):
    """Assert POST /execute/{task_name} with an unresolvable payload returns FAILED.

    The prepared ``queue_item`` (and its ``task`` relationship) is attached to the
    request session, so the payload gate must persist the terminal FAILED row
    through that same session — persisting through a second one would raise
    ``InvalidRequestError`` and surface as an HTTP 500. The route must also still
    eagerly load the deferred ``execution_request`` before serialization. The real
    ``dispatch_queue_item`` runs here (only the internal dispatch is stubbed) so
    the gate short-circuit stays observable.
    """
    await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="wrapped-root",
                backend=TaskBackendEnum.NOMAD,
                anonymize_mask=0,
            )
        ),
    )
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(
                name="proxy-task",
                backend=TaskBackendEnum.PROXY,
                alert_on_fail=False,
                anonymize_mask=0,
                data={
                    "task": "wrapped-root",
                    "payload": "file:///nonexistent/x_payload",
                },
            )
        ),
    )

    fake_executor = MagicMock(spec=BaseExecutor)
    fake_executor.get_hosts.return_value = {"node1": "10.0.0.1"}
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=fake_executor)
    spy_internal = mocker.patch(
        "app.tasks.celery._dispatch_queue_item", new_callable=AsyncMock
    )

    response = test_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1"},
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == TaskHistoryStatusEnum.FAILED.value
    assert data["execution_request"]["task"] == task.name
    spy_internal.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_name_with_eta_serializes_deferred_execution_request(
    test_client, session, mocker
):
    """Assert POST /execute/{task_name} with ETA serializes the deferred column.

    Regression for a ``MissingGreenlet`` error on the ETA scheduling path:
    after ``TaskHistoryManager.save`` the deferred ``execution_request`` column
    is expired. Accessing ``history_recorded.execution_request.eta`` before
    refreshing triggers a lazy load outside the greenlet context.
    """
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(TaskFactory.build(name="eta-task", anonymize_mask=0)),
    )

    fake_celery_result = MagicMock()
    fake_celery_result.id = "fake-celery-task-id"

    fake_executor = MagicMock(spec=BaseExecutor)
    fake_executor.get_hosts.return_value = {"node1": "10.0.0.1"}
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=fake_executor)
    mocker.patch(
        "app.tasks.routes.execute_task_queue.apply_async",
        return_value=fake_celery_result,
    )

    eta = (utc_now() + timedelta(seconds=30)).isoformat()
    response = test_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1", "eta": eta},
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["execution_request"]["task"] == task.name
    assert data["execution_request"]["target"] == "node1"
    assert (
        data["execution_request"]["tracking"]["celery_task_id"] == "fake-celery-task-id"
    )


@pytest.mark.asyncio
async def test_execute_route_keeps_scheduled_at_out_of_meta(
    test_client, session, mocker
):
    """Assert POST /execute/{task_name} never writes ``scheduled_at`` to meta.

    The dispatch dedup path iterates every meta key to identify identical
    pending/running tasks, so a per-call timestamp would silently disable
    the guard. The staleness value is derived at dispatch time from ``eta``
    / ``created_at`` instead.
    """
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="stale-meta-free", anonymize_mask=0)
        ),
    )

    async def fake_dispatch_queue_item(queue_item, passed_session):
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        return await TaskHistoryManager.save(
            passed_session,
            queue_item,
            flag_modified_fields=["execution_request"],
        )

    fake_executor = MagicMock(spec=BaseExecutor)
    fake_executor.get_hosts.return_value = {"node1": "10.0.0.1"}
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=fake_executor)
    mocker.patch(
        "app.tasks.routes.dispatch_queue_item",
        side_effect=fake_dispatch_queue_item,
    )

    response = test_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1", "meta_foo": "bar"},
    )

    assert response.status_code == status.HTTP_200_OK
    meta = response.json()["execution_request"]["meta"]
    assert meta["target"] == "node1"
    assert meta["foo"] == "bar"
    assert "scheduled_at" not in meta


@pytest.mark.asyncio
async def test_execute_task_name_refreshes_execution_request_before_annotation(
    test_client, session, mock_executor, mocker
):
    """Assert the STARTED annotation fires against a loaded ``execution_request``.

    Exercise the full
    ``POST /execute/{task_name}`` → ``_dispatch_queue_item`` →
    ``schedule_annotation(result, "STARTED")`` flow with a real session
    and a fake executor whose ``dispatch_task`` runs the real
    ``TaskHistoryManager.save`` (which re-defers the deferred
    ``execution_request`` ``column_property``). Let the real
    ``schedule_annotation`` run so its precondition guard exercises
    against the refreshed instance; mock only
    ``create_pmm_annotation`` (the outermost HTTP boundary) and assert
    the annotation was scheduled with the expected primitives.

    Before the fix, this test reproduces ``MissingGreenlet`` against the
    async ``aiosqlite`` driver.
    """
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="annotation-task", anonymize_mask=0)
        ),
    )

    async def fake_dispatch_task(passed_session, queue_item, _task=None):
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        return await TaskHistoryManager.save(
            passed_session, queue_item, flag_modified_fields=["execution_request"]
        )

    mock_executor.dispatch_task = fake_dispatch_task

    mocker.patch(
        "app.tasks.celery.DispatchLockManager.delete_where",
        new_callable=AsyncMock,
    )
    # The lock row is added and committed on its own session now, rather than
    # through ``DispatchLockManager.create``, so the session maker is what has to
    # be stubbed: this test runs against SQLite, which has no dispatchlock table.
    lock_session = AsyncMock()
    lock_session_cm = AsyncMock()
    lock_session_cm.__aenter__ = AsyncMock(return_value=lock_session)
    lock_session_cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "app.tasks.celery.get_async_session_maker",
        return_value=MagicMock(return_value=lock_session_cm),
    )
    mocker.patch(
        "app.tasks.celery._raise_if_identical_task_conflict", new_callable=AsyncMock
    )
    mocker.patch("app.tasks.routes.get_executor_for_task", return_value=mock_executor)
    mocker.patch("app.tasks.celery.get_executor_for_task", return_value=mock_executor)
    mock_pmm_create = mocker.patch(
        "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
    )
    _background_tasks.clear()

    response = test_client.post(
        f"/execute/{task.name}",
        json={"meta_target": "node1"},
    )

    for bg_task in list(_background_tasks):
        await bg_task

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == TaskHistoryStatusEnum.RUNNING.value
    mock_pmm_create.assert_awaited_once()
    assert mock_pmm_create.await_args.kwargs["text"].endswith("- STARTED")
    assert mock_pmm_create.await_args.kwargs["node_name"] == "node1"


# Note: the corresponding regression for ``POST /history/{id}/stop/`` →
# ``BaseExecutor.stop_task`` → ``schedule_annotation(saved, "STOPPED")``
# lives at helper level in
# ``tests/app/tasks/execution/test_models.py::TestStopTaskRegression``.
# That test exercises the real ``BaseExecutor.stop_task`` against a real
# ``AsyncSession`` — the exact code path SEP-1017 fixes. An HTTP-level
# peer would additionally exercise response-model serialization, which
# hits a pre-existing deferred-relationship issue on ``TaskHistory.task``
# that is out of scope for SEP-1017; the existing
# ``test_stop_task_running_calls_executor`` already covers the route's
# 200 contract.


CONNECTIVITY_META = {
    "_connectivity_host": "db-host",
    "_connectivity_port": "3306",
    "_connectivity_service_type": ConnectivityServiceType.MYSQL.value,
}


async def _fake_dispatch_queue_item(
    queue_item: TaskHistory, passed_session: AsyncSession
) -> tuple[TaskHistory, str | None]:
    """Persist the queue item so the route's ``session.refresh`` call succeeds.

    :param queue_item: The ``TaskHistory`` queue item dispatched by the route.
    :type queue_item: TaskHistory
    :param passed_session: The async database session the route passes along.
    :type passed_session: AsyncSession
    :return: The persisted ``TaskHistory`` instance.
    :rtype: TaskHistory
    """
    queue_item.status = TaskHistoryStatusEnum.RUNNING
    return await TaskHistoryManager.save(
        passed_session,
        queue_item,
        flag_modified_fields=["execution_request"],
    )


class TestPreExecutionConnectivityCheck:
    """Test the pre-execution connectivity check in execute_task_name."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Clear the connectivity result cache around each test."""
        _cached_check_connectivity.cache_clear()
        yield
        _cached_check_connectivity.cache_clear()

    @pytest_asyncio.fixture
    async def conn_task(self, session) -> Task:
        """Return a task with connectivity meta."""
        return await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="conn-task",
                    data={"Meta": CONNECTIVITY_META},
                )
            ),
        )

    @pytest.mark.asyncio
    async def test_disabled_skips_check(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify no connectivity check runs when mode is ``disabled``."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.DISABLED,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )
        mock_check = mocker.patch("app.tasks.routes.check_connectivity_with_cache")

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_not_called()
        mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_warn_success_dispatches(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify dispatch proceeds when warn-mode check passes."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.WARN,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(True, None)),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_warn_failure_logs_warning_and_dispatches(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify dispatch proceeds with a warning when warn-mode check fails."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.WARN,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(False, "Connection refused")),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )
        mock_logger = mocker.patch("app.tasks.routes.logger")

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_dispatch.assert_called_once()
        mock_logger.warning.assert_called_once()
        assert "Connection refused" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio
    async def test_block_success_dispatches(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify dispatch proceeds when block-mode check passes."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(True, None)),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_block_failure_returns_400(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify dispatch is blocked with 400 when block-mode check fails."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(False, "Connection refused")),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            new=AsyncMock(),
        )

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Connection refused" in response.json()["detail"]
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_connectivity_meta_skips_check(
        self, test_client, session, mocker, mock_executor
    ):
        """Verify check is skipped when task has no connectivity meta fields."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="no-meta-task", data={})),
        )

        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_check = mocker.patch("app.tasks.routes.check_connectivity_with_cache")
        mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_called_with_parsed_meta(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify ``check_connectivity_with_cache`` receives parsed meta args."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_check = mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(True, None)),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_awaited_once()
        kwargs = mock_check.await_args.kwargs
        assert kwargs["target"] == "node1"
        assert kwargs["host"] == CONNECTIVITY_META["_connectivity_host"]
        assert kwargs["port"] == int(CONNECTIVITY_META["_connectivity_port"])
        assert kwargs["service_type"] == ConnectivityServiceType(
            CONNECTIVITY_META["_connectivity_service_type"]
        )
        mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_warn_logs_and_dispatches(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify failure from ``check_connectivity_with_cache`` in warn mode logs and proceeds."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.WARN,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(False, "Connection refused")),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )
        mock_logger = mocker.patch("app.tasks.routes.logger")

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_dispatch.assert_called_once()
        mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_block_returns_400(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify failure from ``check_connectivity_with_cache`` in block mode returns 400."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mocker.patch(
            "app.tasks.routes.check_connectivity_with_cache",
            new=AsyncMock(return_value=(False, "Connection refused")),
        )
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            new=AsyncMock(),
        )

        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_eta_task_skips_check(
        self, test_client, mocker, conn_task, mock_executor
    ):
        """Verify check is skipped for ETA-scheduled tasks regardless of mode."""
        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_check = mocker.patch("app.tasks.routes.check_connectivity_with_cache")
        mocker.patch(
            "app.tasks.routes.execute_task_queue.apply_async",
            return_value=MagicMock(id="celery-123"),
        )

        eta = utc_now()
        response = test_client.post(
            f"/execute/{conn_task.name}",
            json={"meta": {"target": "node1"}, "eta": eta.isoformat()},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_meta_override", "case_label"),
        [
            ({"_connectivity_port": "not-a-number"}, "invalid-port"),
            ({"_connectivity_service_type": "bogus-service"}, "invalid-service-type"),
        ],
    )
    async def test_malformed_meta_skips_check_and_dispatches(
        self,
        test_client,
        session,
        mocker,
        mock_executor,
        bad_meta_override,
        case_label,
    ):
        """Verify malformed connectivity meta is logged and dispatch proceeds.

        Regression guard: ``int(conn_port)`` and ``ConnectivityServiceType(conn_type)``
        must not raise ``ValueError`` out to the caller — the route should log a
        warning, skip the check, and dispatch normally even under ``block`` mode.
        """
        bad_meta = {**CONNECTIVITY_META, **bad_meta_override}
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name=f"bad-meta-task-{case_label}",
                    data={"Meta": bad_meta},
                )
            ),
        )

        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_check = mocker.patch("app.tasks.routes.check_connectivity_with_cache")
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )
        mock_logger = mocker.patch("app.tasks.routes.logger")

        response = test_client.post(
            f"/execute/{task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_not_called()
        mock_dispatch.assert_called_once()
        assert any(
            "malformed connectivity metadata" in call.args[0]
            for call in mock_logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_field",
        ["_connectivity_host", "_connectivity_port", "_connectivity_service_type"],
    )
    @pytest.mark.parametrize("empty_value", [None, ""])
    async def test_partial_connectivity_meta_skips_check(
        self,
        test_client,
        session,
        mocker,
        mock_executor,
        missing_field,
        empty_value,
    ):
        """Verify check is skipped when any connectivity meta field is empty or None.

        Regression guard: the truthy short-circuit in ``execute_task_name`` is the
        only defense against half-populated task definitions. If a refactor ever
        replaces ``and conn_host`` with ``and conn_host is not None`` the empty
        string case silently breaks.
        """
        partial_meta = {**CONNECTIVITY_META, missing_field: empty_value}
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name=f"partial-meta-task-{missing_field}-{empty_value!r}",
                    data={"Meta": partial_meta},
                )
            ),
        )

        mocker.patch(
            "app.tasks.routes.tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK",
            PreExecutionCheckMode.BLOCK,
        )
        mocker.patch(
            "app.tasks.routes.get_executor_for_task",
            return_value=mock_executor,
        )
        mock_check = mocker.patch("app.tasks.routes.check_connectivity_with_cache")
        mock_dispatch = mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=_fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{task.name}",
            json={"meta": {"target": "node1"}},
        )

        assert response.status_code == status.HTTP_200_OK
        mock_check.assert_not_called()
        mock_dispatch.assert_called_once()


@pytest.mark.asyncio
class TestSyncTaskHistoryRealSession:
    """Integration coverage for POST /history/{id}/sync/ with a real session.

    A real HTTP POST must open a fresh writer session, forward it to
    ``executor.sync_task_history``, and let the Nomad executor's
    ``_persist_nomad_task_logs`` append chunks to ``taskhistory_log``.
    """

    async def test_sync_running_persists_logs_via_writer_session(
        self,
        regular_user,
        session: AsyncSession,
        mock_executor: AsyncMock,
    ):
        """Verify the sync route drives log persistence through writer_session."""
        test_session_maker = get_async_session_maker_from_engine(session.bind)

        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="run-python",
                    backend=TaskBackendEnum.NOMAD,
                    is_template=False,
                    protected=False,
                    alert_on_fail=False,
                )
            ),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node1",
                meta={"target": "node1"},
                tracking={"evaluation_id": "eval-1", "allocation_id": "alloc-1"},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
            executed_by="test-user",
        )
        saved_history = await TaskHistoryManager.save(session, history)

        stdout_bytes = b"fresh stdout chunk"

        async def fake_sync(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            assert writer_session is not None
            await TaskHistoryLogWriter.append(
                writer_session,
                queue_item.id,
                source="run-script",
                stream=TaskLogType.STDOUT,
                new_bytes=stdout_bytes,
                force_flush=True,
                producer_offset_after=len(stdout_bytes),
            )
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            queue_item.finished_at = utc_now()
            return queue_item, None

        mock_executor.sync_task_history = AsyncMock(side_effect=fake_sync)

        tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
            lambda: None
        )
        tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
        tasks_app.dependency_overrides[get_session] = lambda: session
        tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor

        try:
            with patch(
                "app.tasks.routes.get_async_session_maker",
                return_value=test_session_maker,
            ):
                transport = ASGITransport(app=tasks_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(f"/history/{saved_history.id}/sync/")
        finally:
            tasks_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == TaskHistoryStatusEnum.SUCCESS.value
        mock_executor.sync_task_history.assert_awaited_once()
        call_kwargs = mock_executor.sync_task_history.await_args.kwargs
        assert "writer_session" in call_kwargs
        assert call_kwargs["writer_session"] is not None
        assert await TaskHistoryLogManager.exists(
            session, task_history_id=saved_history.id
        )

    async def test_sync_hands_the_run_result_to_the_recorder(
        self,
        regular_user,
        session: AsyncSession,
        mock_executor: AsyncMock,
        mocker,
    ):
        """Verify a terminal sync reads the run result and feeds it to the recorder."""
        test_session_maker = get_async_session_maker_from_engine(session.bind)
        result = {
            "backup_dir": "/data/backup/20260727",
            "size_bytes": 20260727,
            "upload_destination": "s3://bucket/backup",
        }
        recorded = []

        async def _recorder(recorder_session, history, run_result):
            recorded.append(run_result)

        mocker.patch.dict(
            hook_resolver._RESOLVED,
            {"app.extensions.apps.pkg:rec": _recorder},
            clear=True,
        )

        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="run-python",
                    backend=TaskBackendEnum.NOMAD,
                    is_template=False,
                    protected=False,
                    alert_on_fail=False,
                    run_result_recorder="app.extensions.apps.pkg:rec",
                    output_files_path=RUN_SCRIPT_OUTPUT_FILES_PATH,
                )
            ),
        )
        saved_history = await TaskHistoryManager.save(
            session,
            build_task_history(task, status=TaskHistoryStatusEnum.RUNNING),
        )

        async def fake_sync(
            queue_item: TaskHistory,
            writer_session: AsyncSession | None = None,
        ) -> tuple[TaskHistory, str | None]:
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            queue_item.finished_at = utc_now()
            return queue_item, None

        async def fake_stream_file(*args, **kwargs):
            yield json_lib.dumps(result).encode()

        mock_executor.sync_task_history = AsyncMock(side_effect=fake_sync)
        mock_executor.stream_file = MagicMock(side_effect=fake_stream_file)

        tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
            lambda: None
        )
        tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
        tasks_app.dependency_overrides[get_session] = lambda: session
        tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor

        try:
            with (
                patch(
                    "app.tasks.routes.get_async_session_maker",
                    return_value=test_session_maker,
                ),
                patch(
                    "app.tasks.run_result.get_async_session_maker",
                    return_value=test_session_maker,
                ),
            ):
                transport = ASGITransport(app=tasks_app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(f"/history/{saved_history.id}/sync/")
        finally:
            tasks_app.dependency_overrides = {}

        assert response.status_code == status.HTTP_200_OK
        assert recorded == [result]
        assert mock_executor.stream_file.call_args.kwargs["anonymize"] is False


async def _record_capture(
    session: AsyncSession,
    history_id: int,
    *,
    source: str,
    capture_status: LogCaptureStatusEnum,
) -> None:
    """Record one stream's capture verdict for a history."""
    await TaskHistoryLogWriter.record_capture_status(
        session,
        history_id,
        source=source,
        stream=TaskLogType.STDOUT,
        capture_status=capture_status,
    )


@pytest.mark.asyncio
async def test_list_task_history_log_capture_unknown_before_migration(
    test_client, created_task_with_history
):
    """Assert a history with no state rows reports ``unknown`` over HTTP.

    This is the pre-migration population: the bytes are gone and the stored
    offsets cannot say whether the step was silent or lost.
    """
    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.UNKNOWN


@pytest.mark.asyncio
async def test_list_task_history_log_capture_complete_for_a_silent_step(
    test_client, session, created_task_with_history
):
    """Assert a step that printed nothing reports ``complete``, not ``unknown``.

    The reader contract the ticket exists for: silent and lost must be
    distinguishable through the response, not merely in the database.
    """
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.COMPLETE


@pytest.mark.asyncio
async def test_list_task_history_log_capture_incomplete_dominates(
    test_client, session, created_task_with_history
):
    """Assert one lost stream reports ``incomplete`` even beside a clean one."""
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )
    await _record_capture(
        session,
        created_task_with_history.id,
        source="clean-up",
        capture_status=LogCaptureStatusEnum.INCOMPLETE,
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.INCOMPLETE


@pytest.mark.asyncio
async def test_list_task_history_log_capture_ignores_the_hold_step(
    test_client, session, created_task_with_history
):
    """Assert the hold's own row cannot drag a clean task to incomplete."""
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )
    await _record_capture(
        session,
        created_task_with_history.id,
        source=NomadStep.LOG_CAPTURE_HOLD,
        capture_status=LogCaptureStatusEnum.INCOMPLETE,
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.COMPLETE


@pytest.mark.asyncio
async def test_list_task_history_log_capture_counts_a_celery_source(
    test_client, session, created_task_with_history
):
    """Assert a Celery-written ``execution`` source reaches the aggregate.

    The Celery executor's source is not a Nomad step name at all, so an
    aggregate filtered by Nomad step names would report ``unknown`` for every
    Celery-backed task while passing every Nomad test.
    """
    await _record_capture(
        session,
        created_task_with_history.id,
        source="execution",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )

    response = test_client.get("/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.COMPLETE


@pytest.mark.asyncio
async def test_retrieve_task_history_reports_log_capture(
    test_client, session, created_task_with_history
):
    """Assert the single-history route carries the verdict too.

    Route wiring is per-endpoint: a list route populating the field proves
    nothing about the retrieve route, which used to compute ``has_logs`` by
    hand.
    """
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.INCOMPLETE,
    )

    response = test_client.get(f"/history/{created_task_with_history.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["log_capture"] == LogCaptureStatusEnum.INCOMPLETE


@pytest.mark.asyncio
async def test_list_task_history_by_task_name_reports_log_capture(
    test_client, session, created_task_with_history
):
    """Assert the per-task history list carries the verdict."""
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )

    response = test_client.get(f"/{created_task_with_history.task.name}/history/")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["items"][0]["log_capture"] == LogCaptureStatusEnum.COMPLETE


@pytest.mark.asyncio
async def test_stop_route_reports_log_capture(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert the stop route carries the capture verdict on its response.

    Route wiring is per-endpoint, and this one computed ``has_logs`` by hand
    before it was folded into the shared helper.
    """
    created_task_with_history.status = TaskHistoryStatusEnum.RUNNING
    await TaskHistoryManager.save(session, created_task_with_history)
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.INCOMPLETE,
    )
    mock_executor.stop_task.return_value = created_task_with_history

    response = test_client.post(f"/history/{created_task_with_history.id}/stop/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["log_capture"] == LogCaptureStatusEnum.INCOMPLETE


@pytest.mark.asyncio
async def test_sync_route_reports_log_capture(
    test_client, session, mock_executor, created_task_with_history
):
    """Assert the sync route carries the capture verdict on its response."""
    await _record_capture(
        session,
        created_task_with_history.id,
        source="run-script",
        capture_status=LogCaptureStatusEnum.COMPLETE,
    )

    response = test_client.post(f"/history/{created_task_with_history.id}/sync/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["log_capture"] == LogCaptureStatusEnum.COMPLETE


class TestExecutionRequestEncryptionOverHTTP:
    """Cover every route carrying ``TaskHistoryResponse`` end to end.

    The route signatures are unchanged, so no unit test reaches what these do:
    each read route serialises the changed response model, and each write route
    drives the column type's encryption through a real body, database round trip
    and reload rather than through a direct call.
    """

    _ARGS = "restore --password hunter2"
    _CONFIG = "master_password: hunter2\n"
    _PAYLOAD = "secret document"

    @classmethod
    async def _seed(cls, session, *, name: str) -> tuple[TaskHistory, str | None]:
        """Persist a task and one history row carrying every protected leaf.

        :param session: The session to persist through.
        :param name: The parent task's name.
        :return: The saved history row.
        """
        task = await TaskManager.create(
            session, TaskWrite.model_validate(TaskFactory.build(name=name))
        )
        return await TaskHistoryManager.save(
            session,
            TaskHistory(
                task_id=task.id,
                status=TaskHistoryStatusEnum.SUCCESS,
                execution_request=TaskExecutionRequest(
                    task=task.name,
                    target="node-1",
                    meta={
                        "target": "node-1",
                        "args": cls._ARGS,
                        "config": cls._CONFIG,
                    },
                    payload=cls._PAYLOAD,
                ),
                executed_by="test-user",
            ),
        )

    @pytest.mark.asyncio
    async def test_list_route_reports_and_redacts_an_unreadable_row(
        self, test_client, session
    ):
        """Assert ``GET /history/`` names the leaf and serialises it as ``null``."""
        history = await self._seed(session, name="crypto-list")
        await overwrite_execution_request(
            session, history.id, undecryptable_document("crypto-list")
        )

        response = test_client.get("/history/")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert item["unreadable_request_leaves"] == [PAYLOAD_LEAF]
        assert item["execution_request"]["payload"] is None

    @pytest.mark.asyncio
    async def test_by_task_name_route_reports_and_redacts_an_unreadable_row(
        self, test_client, session
    ):
        """Assert ``GET /{task}/history/`` reports the same indicator."""
        history = await self._seed(session, name="crypto-by-name")
        await overwrite_execution_request(
            session, history.id, undecryptable_document("crypto-by-name")
        )

        response = test_client.get("/crypto-by-name/history/")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert item["unreadable_request_leaves"] == [PAYLOAD_LEAF]
        assert item["execution_request"]["payload"] is None

    @pytest.mark.asyncio
    async def test_retrieve_route_reports_and_redacts_an_unreadable_row(
        self, test_client, session
    ):
        """Assert ``GET /history/{id}`` reports the same indicator."""
        history = await self._seed(session, name="crypto-retrieve")
        await overwrite_execution_request(
            session, history.id, undecryptable_document("crypto-retrieve")
        )

        response = test_client.get(f"/history/{history.id}")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["unreadable_request_leaves"] == [PAYLOAD_LEAF]
        assert body["execution_request"]["payload"] is None

    @pytest.mark.asyncio
    async def test_read_route_reports_no_leaves_for_a_readable_row(
        self, test_client, session
    ):
        """Assert an ordinary row still serialises its request in full."""
        history = await self._seed(session, name="crypto-readable")

        response = test_client.get(f"/history/{history.id}")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["unreadable_request_leaves"] == []
        assert body["execution_request"]["payload"] == self._PAYLOAD
        assert body["execution_request"]["meta"]["args"] == self._ARGS
        assert body["execution_request"]["meta"]["config"] == self._CONFIG

    @pytest.mark.asyncio
    async def test_create_route_stores_the_submitted_leaves_encrypted(
        self, test_client, session, created_task_with_history
    ):
        """Assert ``POST /history/`` encrypts at rest and answers in plaintext."""
        response = test_client.post(
            "/history/",
            json={
                "task_id": created_task_with_history.task.id,
                "execution_request": {
                    "task": created_task_with_history.task.name,
                    "target": "node-1",
                    "meta": {"args": self._ARGS, "config": self._CONFIG},
                    "payload": self._PAYLOAD,
                },
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["execution_request"]["meta"]["args"] == self._ARGS
        assert body["execution_request"]["meta"]["config"] == self._CONFIG
        assert body["execution_request"]["payload"] == self._PAYLOAD
        stored = await stored_execution_request(session, body["id"])
        for key in ENCRYPTED_META_KEYS:
            assert is_encrypted(stored["meta"][key])
        assert is_encrypted(stored["payload"])
        serialised = json_lib.dumps(stored)
        assert "hunter2" not in serialised
        assert created_task_with_history.task.name in serialised

    @pytest.mark.asyncio
    async def test_execute_route_stores_the_submitted_leaves_encrypted(
        self, test_client, session, mocker
    ):
        """Assert ``POST /execute/{task_name}`` encrypts what the caller submitted.

        :param mocker: The patching fixture standing in for the executor edge.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="crypto-execute", anonymize_mask=0)
            ),
        )

        async def fake_dispatch_queue_item(queue_item, passed_session):
            queue_item.status = TaskHistoryStatusEnum.RUNNING
            return await TaskHistoryManager.save(
                passed_session,
                queue_item,
                flag_modified_fields=["execution_request"],
            )

        fake_executor = MagicMock(spec=BaseExecutor)
        fake_executor.get_hosts.return_value = {"node-1": "10.0.0.1"}
        mocker.patch(
            "app.tasks.routes.get_executor_for_task", return_value=fake_executor
        )
        mocker.patch(
            "app.tasks.routes.dispatch_queue_item",
            side_effect=fake_dispatch_queue_item,
        )

        response = test_client.post(
            f"/execute/{task.name}",
            json={"meta_target": "node-1", "meta_args": self._ARGS},
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["execution_request"]["meta"]["args"] == self._ARGS
        stored = await stored_execution_request(session, body["id"])
        assert is_encrypted(stored["meta"]["args"])

    @pytest.mark.asyncio
    async def test_stop_route_keeps_the_response_shape(
        self, test_client, session, mock_executor
    ):
        """Assert ``POST /history/{id}/stop/`` still answers with a full row."""
        history = await self._seed(session, name="crypto-stop")
        history.status = TaskHistoryStatusEnum.RUNNING
        await TaskHistoryManager.save(session, history)
        mock_executor.stop_task.return_value = history

        response = test_client.post(f"/history/{history.id}/stop/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["unreadable_request_leaves"] == []
        assert body["execution_request"]["payload"] == self._PAYLOAD

    @pytest.mark.asyncio
    async def test_sync_route_preserves_an_unreadable_leaf_on_save_back(
        self, test_client, session, mock_executor
    ):
        """Assert the save-back writes a marked leaf byte-identically.

        The sync path re-saves a row it loaded, so without the write-path guard
        this is where the only copy of an unreadable token would be destroyed.
        """
        history = await self._seed(session, name="crypto-sync")
        document = undecryptable_document("crypto-sync", args=self._ARGS)
        await overwrite_execution_request(session, history.id, document)
        await TaskHistoryManager.update_where(
            session, {"status": TaskHistoryStatusEnum.RUNNING}, id=history.id
        )

        async def fake_sync(item, writer_session=None):
            item.status = TaskHistoryStatusEnum.SUCCESS
            item.finished_at = utc_now()
            return item, None

        mock_executor.sync_task_history = AsyncMock(side_effect=fake_sync)

        response = test_client.post(f"/history/{history.id}/sync/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["unreadable_request_leaves"] == [PAYLOAD_LEAF]
        stored = await stored_execution_request(session, history.id)
        assert stored["payload"] == document["payload"]
        assert is_encrypted(stored["meta"]["args"])
