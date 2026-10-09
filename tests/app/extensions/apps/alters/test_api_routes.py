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

"""Tests for the alters plugin JSON API routes under /api/apps/alters/."""

from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException, status
from pytest_mock import MockerFixture

from app.api.deps import SERVICE_PRINCIPAL_ID
from app.core.exceptions import HTTPNotFoundException
from app.extensions.apps.framework.schema import EXECUTION_HOST_LABEL
from app.extensions.connectivity import clear_connectivity_caches
from app.extensions.inventory import CreatedService
from app.tasks.models import TaskBackendEnum, TaskHistoryStatusEnum
from tests.app.factories import TaskFactory

API_BASE = "/api/apps/alters"
DEFAULT_TASK_NAME = "alter-task"
DEFAULT_PARENT_NAME = "parent-alter"
EXPECTED_CASCADE_CREATE_POSTS = 3
EXPECTED_CASCADE_UPDATE_PUTS = 3
NOMAD_HOSTS = {"host1": "127.0.0.1"}


def build_alters_task(
    name: str = DEFAULT_TASK_NAME,
    *,
    parent: str | None = None,
    protected: bool = False,
    **overrides: Any,
) -> dict:
    """Build a fake alters task payload shaped like the Tasks API response."""
    data_overrides = overrides.pop("data", {})
    meta_overrides = data_overrides.pop("meta", {})
    task = TaskFactory.build(
        name=name,
        owner="ALTERS",
        backend=TaskBackendEnum.PROXY,
        protected=protected,
        **overrides,
    )
    payload = task.model_dump(mode="json")
    payload["data"] = {
        "task": "run-command",
        "meta": {
            "command": "pt-online-schema-change",
            "args": (
                "--alter=ADD COLUMN x INT "
                "h=127.0.0.1,P=3306,D=test_schema,t=test_table "
                "--recursion-method=processlist --execute"
            ),
            "target": "host1",
            "_schema_name": "test_schema",
            "_table_name": "test_table",
            "_service_host": "127.0.0.1",
            "_service_port": 3306,
            **meta_overrides,
        },
        **data_overrides,
    }
    if parent is not None:
        payload["data"]["parent"] = parent
    return payload


def build_alters_task_group(
    parent_name: str = DEFAULT_PARENT_NAME,
    *,
    created_by: str | None = None,
) -> dict[str, dict]:
    """Return parent, dry-run, and pre-checks task payloads for one group.

    :param parent_name: The parent task name.
    :param created_by: The actor identifier to stamp on every task, or ``None`` to
        use the task factory default.
    :return: The three task payloads keyed by their cascade roles.
    """
    actor = {"created_by": created_by} if created_by is not None else {}
    return {
        "parent": build_alters_task(parent_name, **actor),
        "dry_run": build_alters_task(
            f"{parent_name}-dry-run", parent=parent_name, **actor
        ),
        "pre_checks": build_alters_task(
            f"{parent_name}-pre-checks", parent=parent_name, **actor
        ),
    }


def build_alters_write_body(
    task_name: str = DEFAULT_TASK_NAME,
    hostname: str = "host1",
    service_id: int = 1,
    **kwargs: Any,
) -> dict:
    """Build a valid AltersCreate-compatible request body."""
    return {
        "task_name": task_name,
        "hostname": hostname,
        "service_id": service_id,
        "db_schema": "test_schema",
        "db_table": "test_table",
        "alter": "ADD COLUMN x INT",
        **kwargs,
    }


def build_execute_response(
    task_id: int | None = 99, task_name: str = DEFAULT_TASK_NAME
) -> dict:
    """Build a minimal TaskHistoryResponse-shaped dict for execute endpoint tests."""
    return {
        "id": task_id,
        "execution_request": {"task": "run-command", "target": "host1"},
        "task": {**build_alters_task(task_name), "deleted_at": None},
    }


def cascade_create_post_side_effect(
    task_name: str,
    *,
    execute_result: Any = None,
    created_by: str | None = None,
) -> list[Any]:
    """Build ``tasks_api.post`` side effects for a successful cascade create.

    :param task_name: The parent task name.
    :param execute_result: The response for an optional immediate execution.
    :param created_by: The actor identifier to stamp on every task, or ``None`` to
        use the task factory default.
    :return: The ordered Tasks API responses for the cascade.
    """
    actor = {"created_by": created_by} if created_by is not None else {}
    return [
        build_alters_task(task_name, **actor),
        build_alters_task(f"{task_name}-dry-run", parent=task_name, **actor),
        build_alters_task(f"{task_name}-pre-checks", parent=task_name, **actor),
        execute_result,
    ]


def configure_cascade_create_mocks(
    mock_task_api_dep: AsyncMock,
    mock_inventory_api_dep: AsyncMock,
    created_service: CreatedService,
    task_name: str = DEFAULT_TASK_NAME,
    *,
    execute_result: Any = None,
    fetch_created_task: bool = True,
    created_by: str | None = None,
) -> None:
    """Wire inventory, cascade POST, and Nomad hosts mocks for create-route tests.

    :param mock_task_api_dep: The Tasks API boundary mock.
    :param mock_inventory_api_dep: The Inventory API boundary mock.
    :param created_service: The inventory service selected by the request.
    :param task_name: The parent task name.
    :param execute_result: The response for an optional immediate execution.
    :param fetch_created_task: Whether rendering fetches the created parent task.
    :param created_by: The actor identifier to stamp on every task, or ``None`` to
        use the task factory default.
    """
    mock_inventory_api_dep.get = AsyncMock(return_value=created_service.model_dump())
    mock_task_api_dep.post = AsyncMock(
        side_effect=cascade_create_post_side_effect(
            task_name,
            execute_result=execute_result,
            created_by=created_by,
        )
    )
    get_side_effects: list[Any] = [NOMAD_HOSTS]
    if fetch_created_task:
        actor = {"created_by": created_by} if created_by is not None else {}
        get_side_effects.append(build_alters_task(task_name, **actor))
    mock_task_api_dep.get = AsyncMock(side_effect=get_side_effects)


@pytest.fixture(autouse=True)
def _clear_connectivity_caches():
    """Clear the connectivity alru_cache and snapshot between tests."""
    clear_connectivity_caches()
    yield
    clear_connectivity_caches()


class TestAltersAppSchemaEndpoint:
    """Tests for GET /api/apps/alters/schema."""

    def test_schema_returns_200(self, test_client):
        """Ensure the schema endpoint returns HTTP 200 with JSON content."""
        response = test_client.get(f"{API_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]

    def test_schema_declares_cascade_primitives(self, test_client):
        """Ensure the schema exposes dry-run derived and pre-checks predecessor legs."""
        response = test_client.get(f"{API_BASE}/schema")

        payload = response.json()
        assert [item["name_suffix"] for item in payload["derived"]] == ["-dry-run"]
        assert payload["derived"][0]["arg_substitutions"] == {"--execute": "--dry-run"}
        assert [item["name_suffix"] for item in payload["predecessors"]] == [
            "-pre-checks"
        ]
        assert payload["predecessors"][0]["on_failure"] == "halt"

    def test_schema_surfaces_execution_and_database_host_labels(self, test_client):
        """Ensure detail_view and create form use Execution Host / Database Host labels."""
        response = test_client.get(f"{API_BASE}/schema")

        payload = response.json()
        execution = next(
            section
            for section in payload["detail_view"]["sections"]
            if section["title"] == "Execution"
        )
        detail_labels = {field["label"]: field["path"] for field in execution["fields"]}
        assert detail_labels[EXECUTION_HOST_LABEL] == "data.meta.target"
        assert detail_labels["Database Host"] == "data.meta._service_host"
        assert "Target" not in detail_labels

        task_section = next(
            section for section in payload["forms"] if section["title"] == "Task"
        )
        hostname = next(
            field for field in task_section["fields"] if field["name"] == "hostname"
        )
        assert hostname["label"] == EXECUTION_HOST_LABEL


class TestAltersApiList:
    """Cover the derived paginated roots-only list route GET /api/apps/alters/."""

    def test_list_returns_roots_only_paginated(
        self, test_client, mock_task_api_dep
    ) -> None:
        """List parent tasks through the derived paginated ``roots_only`` route.

        The server-side ``parent_is_null=true`` filter hides the satellites, so
        the Tasks API is queried with that filter and returns only parents; the
        rows keep the same builder-stamped fields as the detail surface.
        """
        group = build_alters_task_group(
            DEFAULT_PARENT_NAME, created_by=str(SERVICE_PRINCIPAL_ID)
        )
        mock_task_api_dep.get = AsyncMock(
            return_value={
                "items": [group["parent"]],
                "total": 1,
                "offset": 0,
                "limit": 50,
            }
        )
        mock_task_api_dep.post = AsyncMock(
            return_value={
                DEFAULT_PARENT_NAME: {
                    "status": TaskHistoryStatusEnum.SUCCESS.value,
                    "finished_at": "2026-07-07T09:00:00",
                }
            }
        )

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {"items", "total", "offset", "limit"} <= body.keys()
        assert body["total"] == 1
        [row] = body["items"]
        assert row["name"] == DEFAULT_PARENT_NAME
        assert row["created_by"] == "Service account"
        assert "service_type" not in row
        assert "owner" not in row
        assert row["status"] == TaskHistoryStatusEnum.SUCCESS.value
        assert row["last_executed_at"] == "2026-07-07T09:00:00Z"
        assert "anonymize_mask" in row
        assert isinstance(row["anonymized_entities"], list)
        assert "connectivity_warning" in row
        assert row["connectivity_warning"] is None
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest", json={"names": [DEFAULT_PARENT_NAME]}
        )
        list_call = next(
            call
            for call in mock_task_api_dep.get.await_args_list
            if call.args[0] == "/"
        )
        assert list_call.kwargs["params"]["parent_is_null"] == "true"
        assert list_call.kwargs["params"]["owner"] == "ALTERS"


class TestAltersApiDetail:
    """Tests for GET /api/apps/alters/{task_name}."""

    def test_detail_returns_parent_task(self, test_client, mock_task_api_dep) -> None:
        """Ensure the detail endpoint returns the parent task with status."""
        group = build_alters_task_group(
            DEFAULT_PARENT_NAME, created_by=str(SERVICE_PRINCIPAL_ID)
        )
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                group["parent"],
                {"items": [{"status": TaskHistoryStatusEnum.RUNNING.value}]},
            ]
        )

        response = test_client.get(f"{API_BASE}/{DEFAULT_PARENT_NAME}")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["name"] == DEFAULT_PARENT_NAME
        assert body["created_by"] == "Service account"
        assert body["status"] == TaskHistoryStatusEnum.RUNNING.value
        assert "service_type" not in body
        assert "owner" not in body
        assert "anonymize_mask" in body
        assert isinstance(body["anonymized_entities"], list)
        assert "connectivity_warning" in body
        assert body["connectivity_warning"] is None

    def test_detail_resolves_satellite_name_to_parent(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Requesting a satellite name returns the parent task."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                group["dry_run"],
                group["parent"],
                {"items": []},
            ]
        )

        response = test_client.get(f"{API_BASE}/{DEFAULT_PARENT_NAME}-dry-run")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == DEFAULT_PARENT_NAME


class TestAltersApiCreate:
    """Tests for POST /api/apps/alters/."""

    def test_create_posts_parent_derived_and_predecessor(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        created_service,
    ) -> None:
        """POST creates parent, dry-run, and pre-checks without firing execute."""
        configure_cascade_create_mocks(
            mock_task_api_dep,
            mock_inventory_api_dep,
            created_service,
            DEFAULT_TASK_NAME,
            created_by=str(SERVICE_PRINCIPAL_ID),
        )

        response = test_client.post(
            f"{API_BASE}/?check_connectivity=false",
            json=build_alters_write_body(
                task_name=DEFAULT_TASK_NAME,
                service_id=created_service.id,
            ),
        )

        assert response.status_code == status.HTTP_201_CREATED
        create_body = response.json()
        assert create_body["created_by"] == "Service account"
        assert "service_type" not in create_body
        assert "owner" not in create_body
        assert "anonymize_mask" in create_body
        assert "anonymized_entities" in create_body
        assert "connectivity_warning" in create_body
        assert mock_task_api_dep.post.await_count == EXPECTED_CASCADE_CREATE_POSTS
        first_post = mock_task_api_dep.post.await_args_list[0].kwargs["json"]
        assert first_post["owner"] == "ALTERS"
        assert "--execute" in first_post["data"]["meta"]["args"]
        assert "_form" in first_post["data"]
        dry_run_post = mock_task_api_dep.post.await_args_list[1].kwargs["json"]
        assert dry_run_post["name"] == f"{DEFAULT_TASK_NAME}-dry-run"
        assert "--dry-run" in dry_run_post["data"]["meta"]["args"]
        assert dry_run_post["data"]["parent"] == DEFAULT_TASK_NAME
        assert "_form" not in dry_run_post["data"]
        pre_checks_post = mock_task_api_dep.post.await_args_list[2].kwargs["json"]
        assert pre_checks_post["name"] == f"{DEFAULT_TASK_NAME}-pre-checks"
        assert "_form" not in pre_checks_post["data"]

    def test_create_returns_422_missing_required_fields(
        self, test_client, mock_task_api_dep
    ) -> None:
        """POST returns 422 when required fields are absent."""
        response = test_client.post(f"{API_BASE}/", json={})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        mock_task_api_dep.post.assert_not_called()

    def test_create_returns_422_for_multiline_alter(
        self, test_client, mock_task_api_dep
    ) -> None:
        """POST returns 422 when the alter command contains a newline."""
        body = build_alters_write_body(alter="ADD COLUMN x INT\nDROP COLUMN y")

        response = test_client.post(f"{API_BASE}/", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        mock_task_api_dep.post.assert_not_called()

    def test_create_stamps_form_input_on_parent_task(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        created_service,
    ) -> None:
        """POST stamps _form on the parent task so the React Edit button is enabled."""
        configure_cascade_create_mocks(
            mock_task_api_dep,
            mock_inventory_api_dep,
            created_service,
            DEFAULT_TASK_NAME,
        )

        body = build_alters_write_body(
            task_name=DEFAULT_TASK_NAME,
            service_id=created_service.id,
        )
        response = test_client.post(
            f"{API_BASE}/?check_connectivity=false",
            json=body,
        )

        assert response.status_code == status.HTTP_201_CREATED
        posted_data = mock_task_api_dep.post.await_args_list[0].kwargs["json"]["data"]
        assert "_form" in posted_data
        form_input = posted_data["_form"]
        assert form_input["task_name"] == DEFAULT_TASK_NAME
        assert form_input["db_schema"] == "test_schema"
        assert form_input["db_table"] == "test_table"


class TestAltersApiUpdate:
    """Tests for PUT /api/apps/alters/{task_name}."""

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_puts_parent_and_satellites(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        created_service,
    ) -> None:
        """PUT updates parent, dry-run sibling, and pre-checks predecessor."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        group["parent"]["last_updated_by"] = str(SERVICE_PRINCIPAL_ID)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                group["parent"],
                NOMAD_HOSTS,
                group["parent"],
                {"items": []},
            ]
        )
        mock_inventory_api_dep.get = AsyncMock(
            return_value=created_service.model_dump()
        )
        mock_task_api_dep.put = AsyncMock(return_value=group["parent"])

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}?check_connectivity=false",
            json=build_alters_write_body(
                task_name=DEFAULT_PARENT_NAME,
                service_id=created_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        update_body = response.json()
        assert update_body["last_updated_by"] == "Service account"
        assert "service_type" not in update_body
        assert "owner" not in update_body
        assert "anonymize_mask" in update_body
        assert "anonymized_entities" in update_body
        assert "connectivity_warning" in update_body
        assert mock_task_api_dep.put.await_count == EXPECTED_CASCADE_UPDATE_PUTS
        put_paths = [c.args[0] for c in mock_task_api_dep.put.await_args_list]
        assert put_paths == [
            f"/{DEFAULT_PARENT_NAME}",
            f"/{DEFAULT_PARENT_NAME}-dry-run",
            f"/{DEFAULT_PARENT_NAME}-pre-checks",
        ]

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_stamps_form_input_on_parent_task(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        created_service,
    ) -> None:
        """PUT re-stamps _form on the parent so the React Edit button survives edits."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                group["parent"],
                NOMAD_HOSTS,
                group["parent"],
                {"items": []},
            ]
        )
        mock_inventory_api_dep.get = AsyncMock(
            return_value=created_service.model_dump()
        )
        mock_task_api_dep.put = AsyncMock(return_value=group["parent"])

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}?check_connectivity=false",
            json=build_alters_write_body(
                task_name=DEFAULT_PARENT_NAME,
                service_id=created_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        parent_put = next(
            c
            for c in mock_task_api_dep.put.await_args_list
            if c.args[0] == f"/{DEFAULT_PARENT_NAME}"
        )
        parent_data = parent_put.kwargs["json"]["data"]
        assert "_form" in parent_data
        assert parent_data["_form"]["task_name"] == DEFAULT_PARENT_NAME
        dry_run_put = next(
            c
            for c in mock_task_api_dep.put.await_args_list
            if c.args[0] == f"/{DEFAULT_PARENT_NAME}-dry-run"
        )
        assert "_form" not in dry_run_put.kwargs["json"]["data"]

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_continue_on_pre_check_failure_uses_continue_predecessor_spec(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        created_service,
        mocker: MockerFixture,
    ) -> None:
        """PUT honors continue_on_pre_check_failure when rebuilding the pre-checks task."""
        from app.extensions.apps.framework.schema import (  # noqa: PLC0415
            ChainedPredecessor,
        )

        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                group["parent"],
                NOMAD_HOSTS,
                group["parent"],
                {"items": []},
            ]
        )
        mock_inventory_api_dep.get = AsyncMock(
            return_value=created_service.model_dump()
        )
        mock_task_api_dep.put = AsyncMock(return_value=group["parent"])
        captured_specs: list[ChainedPredecessor] = []
        original_build = __import__(
            "app.extensions.apps.framework.cascade",
            fromlist=["build_predecessor_payload"],
        ).build_predecessor_payload

        def _capture_build(parent_payload, pred_payload, spec):
            captured_specs.append(spec)
            return original_build(parent_payload, pred_payload, spec)

        mocker.patch(
            "app.extensions.apps.alters.deps.build_predecessor_payload",
            side_effect=_capture_build,
        )

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}?check_connectivity=false",
            json=build_alters_write_body(
                task_name=DEFAULT_PARENT_NAME,
                service_id=created_service.id,
                continue_on_pre_check_failure=True,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        assert len(captured_specs) == 1
        assert captured_specs[0].on_failure == "continue"

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_returns_409_for_protected_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """PUT returns 409 when the parent task is protected."""
        parent = build_alters_task("protected-alter", protected=True)
        mock_task_api_dep.get = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/protected-alter",
            json=build_alters_write_body(task_name="protected-alter"),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_called()

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_returns_409_when_renaming_parent(
        self,
        test_client,
        mock_task_api_dep,
    ) -> None:
        """PUT returns 409 when task_name in the body differs from the parent."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(return_value=group["parent"])

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}",
            json=build_alters_write_body(task_name="renamed-alter"),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_called()

    def test_update_via_satellite_returns_400_not_rename_409(
        self,
        test_client,
        mock_task_api_dep,
        created_service,
    ) -> None:
        """PUT by satellite URL returns 400 with a clear message, not 409 rename."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[group["pre_checks"], group["parent"]]
        )

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}-pre-checks",
            json=build_alters_write_body(
                task_name=f"{DEFAULT_PARENT_NAME}-pre-checks",
                service_id=created_service.id,
            ),
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert DEFAULT_PARENT_NAME in response.json()["detail"]
        assert f"{DEFAULT_PARENT_NAME}-pre-checks" in response.json()["detail"]
        mock_task_api_dep.put.assert_not_called()

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_via_satellite_400_beats_protected_409(
        self, test_client, mock_task_api_dep, created_service
    ) -> None:
        """PUT by satellite URL returns 400 even when the parent is protected.

        Pins the gate order: the URL-target assertion (400) runs before the
        protected check (409). A protected parent reached via a satellite URL
        must still surface the 400, not the protected 409.
        """
        parent = build_alters_task(DEFAULT_PARENT_NAME, protected=True)
        pre_checks = build_alters_task(
            f"{DEFAULT_PARENT_NAME}-pre-checks", parent=DEFAULT_PARENT_NAME
        )
        mock_task_api_dep.get = AsyncMock(side_effect=[pre_checks, parent])

        response = test_client.put(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}-pre-checks",
            json=build_alters_write_body(
                task_name=f"{DEFAULT_PARENT_NAME}-pre-checks",
                service_id=created_service.id,
            ),
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "protected" not in response.json()["detail"].lower()
        mock_task_api_dep.put.assert_not_called()


class TestAltersApiDelete:
    """Tests for DELETE /api/apps/alters/{task_name}."""

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_delete_removes_satellites_then_parent(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE cascades to dry-run and pre-checks before the parent."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(return_value=group["parent"])
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.delete(f"{API_BASE}/{DEFAULT_PARENT_NAME}")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert mock_task_api_dep.delete.await_args_list == [
            call(f"/{DEFAULT_PARENT_NAME}-dry-run"),
            call(f"/{DEFAULT_PARENT_NAME}-pre-checks"),
            call(f"/{DEFAULT_PARENT_NAME}"),
        ]

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_delete_resolves_satellite_name_to_parent_group(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE by satellite name still removes the whole task group."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(
            side_effect=[group["pre_checks"], group["parent"]]
        )
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.delete(f"{API_BASE}/{DEFAULT_PARENT_NAME}-pre-checks")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert mock_task_api_dep.delete.await_args_list[-1] == call(
            f"/{DEFAULT_PARENT_NAME}"
        )

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_delete_returns_500_when_cascade_delete_partially_fails(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 500 when a satellite DELETE fails with a non-404 error."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(return_value=group["parent"])
        derived_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        async def _delete(path: str) -> None:
            if path == f"/{DEFAULT_PARENT_NAME}-pre-checks":
                raise derived_exc

        mock_task_api_dep.delete = AsyncMock(side_effect=_delete)

        response = test_client.delete(f"{API_BASE}/{DEFAULT_PARENT_NAME}")

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = response.json()["detail"]
        assert detail["message"] == "Partial delete failure; orphaned tasks"
        assert f"{DEFAULT_PARENT_NAME}-pre-checks" in detail["errors"]

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_delete_returns_409_for_protected_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE returns 409 when the parent task is protected."""
        parent = build_alters_task("protected-alter", protected=True)
        mock_task_api_dep.get = AsyncMock(return_value=parent)

        response = test_client.delete(f"{API_BASE}/protected-alter")

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.delete.assert_not_called()

    def test_delete_via_satellite_returns_409_when_parent_running(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE by satellite name must block when the resolved parent is running."""
        group = build_alters_task_group(DEFAULT_PARENT_NAME)
        running_history = {"items": [{"id": 1}], "total": 1, "offset": 0, "limit": 50}
        empty_history = {"items": [], "total": 0, "offset": 0, "limit": 50}

        async def _get(path: str, params: dict | None = None) -> dict:
            if path == f"/{DEFAULT_PARENT_NAME}-pre-checks":
                return group["pre_checks"]
            if path == f"/{DEFAULT_PARENT_NAME}":
                return group["parent"]
            if path == f"/{DEFAULT_PARENT_NAME}/history/":
                if params and params.get("status") == TaskHistoryStatusEnum.RUNNING:
                    return running_history
                return empty_history
            raise AssertionError(f"unexpected GET {path!r} params={params!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_get)

        response = test_client.delete(f"{API_BASE}/{DEFAULT_PARENT_NAME}-pre-checks")

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.delete.assert_not_called()
        history_paths = [c.args[0] for c in mock_task_api_dep.get.await_args_list]
        assert f"/{DEFAULT_PARENT_NAME}/history/" in history_paths

    def test_delete_running_conflict_beats_protected_409(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE of a protected, running task returns the conflict 409, not protected.

        Both gates raise 409, so status alone cannot distinguish them. Pins the
        gate order via the detail message: the running-conflict check runs before
        the protected check, so a parent that is both running and protected must
        surface "already running or pending", not "Cannot delete a protected task."
        """
        parent = build_alters_task(DEFAULT_PARENT_NAME, protected=True)
        running_history = {"items": [{"id": 1}], "total": 1, "offset": 0, "limit": 50}

        async def _get(path: str, params: dict | None = None) -> dict:
            if path == f"/{DEFAULT_PARENT_NAME}":
                return parent
            if path == f"/{DEFAULT_PARENT_NAME}/history/":
                return running_history
            raise AssertionError(f"unexpected GET {path!r} params={params!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_get)

        response = test_client.delete(f"{API_BASE}/{DEFAULT_PARENT_NAME}")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["detail"] == "Task is already running or pending."
        mock_task_api_dep.delete.assert_not_called()


class TestAltersApiExecute:
    """Tests for POST /api/apps/alters/{task_name}/execute."""

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_201_with_task_name_and_id(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing an alters task returns 201 with task_name and task_id."""
        expected_task_id = 42
        task = build_alters_task(DEFAULT_PARENT_NAME)
        mock_task_api_dep.get = AsyncMock(return_value=task)
        mock_task_api_dep.post = AsyncMock(
            return_value=build_execute_response(
                expected_task_id, task_name=DEFAULT_PARENT_NAME
            )
        )

        response = test_client.post(
            f"{API_BASE}/{DEFAULT_PARENT_NAME}/execute", json={}
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["task_name"] == DEFAULT_PARENT_NAME
        assert data["task_id"] == expected_task_id
        mock_task_api_dep.post.assert_awaited_once_with(
            f"/execute/{DEFAULT_PARENT_NAME}", json={}
        )

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_404_for_unknown_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing an unknown task name returns 404."""
        mock_task_api_dep.get = AsyncMock(side_effect=HTTPNotFoundException())

        response = test_client.post(f"{API_BASE}/ghost-task/execute", json={})

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAltersApiAuth:
    """Tests for API authentication."""

    def test_unauthenticated_list_returns_401(self, unauthenticated_client) -> None:
        """Reject unauthenticated access to the alters API."""
        response = unauthenticated_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
