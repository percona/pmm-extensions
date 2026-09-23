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

"""Tests for the backup_mongo plugin JSON API routes under /api/apps/backup_mongo/."""

from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException, status

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.exceptions import HTTPNotFoundException
from app.core.pagination import MAX_PAGINATION_LIMIT
from app.extensions.apps.backup_mongo.models import BackupType, OWNER
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.inventory import CreatedService
from app.tasks.models import TaskBackendEnum, TaskHistoryStatusEnum
from tests.app.factories import TaskFactory

API_BASE = "/api/apps/backup_mongo"
EXPECTED_CASCADE_POSTS = 5
EXPECTED_CASCADE_PUTS = 5
DEFAULT_PAGE_LIMIT = 50
THREE_PARENT_FIXTURE_TOTAL = 3
TWO_PARENT_FIXTURE_TOTAL = 2
TWO_DERIVED_SIBLINGS = 2
THREE_DERIVED_SIBLINGS = 3


def build_backup_task(name: str = "mongo-backup-task", **overrides: Any) -> dict:
    """Build a fake backup_mongo task payload for route tests.

    :param name: The task name to embed in the payload.
    :type name: str
    :param overrides: Field overrides passed to ``TaskFactory.build``.
    :type overrides: Any
    :return: A task payload shaped like the Tasks API response.
    :rtype: dict
    """
    data_overrides = overrides.pop("data", {})
    backup_type = data_overrides.pop("backup_type", BackupType.PBM_CONFIG.value)
    parent = data_overrides.pop("parent", None)
    task = TaskFactory.build(
        name=name,
        owner="BACKUP_MONGO",
        backend=TaskBackendEnum.PROXY,
        **overrides,
    )
    payload = task.model_dump(mode="json")
    payload["data"] = {
        "task": "run-python",
        "meta": {
            "target": "mongo-host",
            "config": "storage:\n  type: filesystem\n",
            "requirements": "packaging\nPyYAML",
        },
        "payload": f"file:///plugins/backup_mongo/{backup_type}_payload",
        "backup_type": backup_type,
        **data_overrides,
    }
    if parent is not None:
        payload["data"]["parent"] = parent
    return payload


def build_backup_write_body(
    task_name: str = "mongo-backup-task",
    hostname: str = "mongo-host",
    service_id: int = 1,
    **kwargs: Any,
) -> dict:
    """Build a valid BackupTaskWrite-compatible request body."""
    return {
        "task_name": task_name,
        "hostname": hostname,
        "service_id": service_id,
        "storage_type": "filesystem",
        "storage_filesystem_path": "/var/backups/mongo",
        "pitr_compression": "snappy",
        **kwargs,
    }


def _backup_group_by_path(parent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map every task path of a backup group to its payload, the parent first.

    :param parent: The parent ``pbm_config`` task payload the group hangs off.
    :return: The parent and its four derived legs keyed by ``/<task name>``, in
        the order the cascade creates them.
    """
    name = parent["name"]
    legs = (
        ("logical", BackupType.PBM_LOGICAL),
        ("physical", BackupType.PBM_PHYSICAL),
        ("status", BackupType.PBM_STATUS),
        ("incremental", BackupType.PBM_INCREMENTAL),
    )
    return {
        f"/{name}": parent,
        **{
            f"/{name}-{suffix}": build_backup_task(
                f"{name}-{suffix}",
                data={"backup_type": backup_type.value, "parent": name},
            )
            for suffix, backup_type in legs
        },
    }


def mock_task_api_get_by_path(
    tasks_by_path: dict[str, Any],
    *,
    not_found: tuple[str, ...] = (),
) -> AsyncMock:
    """Return a path-keyed ``tasks_api.get`` mock safe for parallel fetches.

    Unknown paths raise ``AssertionError`` by default so typos fail loudly.
    Pass ``not_found`` for paths that should surface as ``HTTPNotFoundException``
    (for example a missing ``-incremental`` sibling during backfill).
    """

    async def _mock_get(path: str, **kwargs: Any) -> Any:
        if path.endswith("/history/"):
            return {"items": []}
        if path in tasks_by_path:
            return tasks_by_path[path]
        if path in not_found:
            raise HTTPNotFoundException(f"Task not found: {path}")
        raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

    return AsyncMock(side_effect=_mock_get)


def _running_group_get_mock(
    parent_name: str,
    parent: dict,
    *,
    extra_tasks: dict[str, Any] | None = None,
) -> AsyncMock:
    """Return a ``tasks_api.get`` mock reporting the parent group as running."""
    tasks_by_path = {f"/{parent_name}": parent, **(extra_tasks or {})}

    async def _mock_get(path: str, params: dict | None = None, **kwargs: Any) -> Any:
        if path == f"/{parent_name}/history/":
            if params and params.get("status") == TaskHistoryStatusEnum.RUNNING:
                return {"items": [{"id": 1}]}
            return {"items": []}
        if path.endswith("/history/"):
            return {"items": []}
        if path in tasks_by_path:
            return tasks_by_path[path]
        raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

    return AsyncMock(side_effect=_mock_get)


def _running_leg_get_mock(
    parent_name: str, parent: dict, running_leg: str
) -> AsyncMock:
    """Return a ``tasks_api.get`` mock reporting a derived leg (not the parent) as running."""

    async def _mock_get(path: str, params: dict | None = None, **kwargs: Any) -> Any:
        if path == f"/{running_leg}/history/":
            if params and params.get("status") == TaskHistoryStatusEnum.RUNNING:
                return {"items": [{"id": 1}]}
            return {"items": []}
        if path.endswith("/history/"):
            return {"items": []}
        if path == f"/{parent_name}":
            return parent
        raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

    return AsyncMock(side_effect=_mock_get)


class TestBackupMongoAppSchemaEndpoint:
    """Tests for GET /api/apps/backup_mongo/schema."""

    def test_schema_returns_200(self, test_client):
        """Ensure the schema endpoint returns HTTP 200 with JSON content."""
        response = test_client.get(f"{API_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]

    def test_schema_declares_derived_cascade(self, test_client):
        """Ensure the schema exposes the four derived backup legs."""
        response = test_client.get(f"{API_BASE}/schema")

        derived = response.json()["derived"]
        assert [item["name_suffix"] for item in derived] == [
            "-logical",
            "-physical",
            "-status",
            "-incremental",
        ]

    def test_schema_offers_only_per_run_sections(self, test_client):
        """Ask only what one backup decides.

        Storage, Point-in-Time Recovery and the configuration-shaped backup options
        moved to the PBM Configuration tab; the gates that used to be asserted here
        are covered against that schema instead. What a run still chooses is how to
        compress it and which namespaces to include.
        """
        forms = test_client.get(f"{API_BASE}/schema").json()["forms"]

        assert [section["title"] for section in forms] == ["Task", "Backup Options"]
        assert [field["name"] for field in forms[1]["fields"]] == [
            "backup_compression",
            "backup_compression_level",
            "backup_namespaces",
            "backup_with_users_and_roles",
        ]


def build_execute_response(
    task_id: int | None = 99, task_name: str = "mongo-backup-task"
) -> dict:
    """Build a minimal TaskHistoryResponse-shaped dict for execute endpoint tests."""
    return {
        "id": task_id,
        "execution_request": {"task": task_name, "target": "mongo-host"},
        "task": {**build_backup_task(task_name), "deleted_at": None},
    }


class TestBackupMongoApiList:
    """Tests for GET /api/apps/backup_mongo/."""

    def test_list_returns_parent_tasks_only(
        self, test_client, mock_task_api_dep
    ) -> None:
        """List parent config tasks via upstream filters and batch status lookup."""
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = AsyncMock(
            return_value={"items": [parent], "total": 1, "offset": 0, "limit": 50}
        )
        mock_task_api_dep.post = AsyncMock(
            return_value={"parent-backup": {"status": "success", "finished_at": None}},
        )

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert body["offset"] == 0
        assert body["limit"] == DEFAULT_PAGE_LIMIT
        assert len(body["items"]) == 1
        row = body["items"][0]
        assert row["name"] == "parent-backup"
        assert row["status"] == "success"
        assert "service_type" not in row
        assert "owner" not in row
        assert "anonymize_mask" in row
        assert "anonymized_entities" in row
        mock_task_api_dep.get.assert_awaited_once_with(
            "/",
            params={
                "owner": "BACKUP_MONGO",
                "parent_is_null": "true",
                "backup_type": BackupType.PBM_CONFIG.value,
                "offset": 0,
                "limit": DEFAULT_PAGE_LIMIT,
            },
        )
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-backup"]},
        )

    def test_list_paginates_with_offset_and_limit(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Pass offset and limit to the upstream filtered task list."""
        parent_page = build_backup_task("parent-backup-b")
        mock_task_api_dep.get = AsyncMock(
            return_value={
                "items": [parent_page],
                "total": THREE_PARENT_FIXTURE_TOTAL,
                "offset": 1,
                "limit": 1,
            }
        )
        mock_task_api_dep.post = AsyncMock(
            return_value={"parent-backup-b": {"status": "running", "finished_at": None}}
        )

        response = test_client.get(f"{API_BASE}/?offset=1&limit=1")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == THREE_PARENT_FIXTURE_TOTAL
        assert body["offset"] == 1
        assert body["limit"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "parent-backup-b"
        mock_task_api_dep.get.assert_awaited_once_with(
            "/",
            params={
                "owner": "BACKUP_MONGO",
                "parent_is_null": "true",
                "backup_type": BackupType.PBM_CONFIG.value,
                "offset": 1,
                "limit": 1,
            },
        )
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-backup-b"]},
        )

    def test_list_tolerates_batch_status_fetch_failure(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 200 with unknown status when batch latest-status lookup fails."""
        parent_a = build_backup_task("parent-backup-a")
        parent_b = build_backup_task("parent-backup-b")
        mock_task_api_dep.get = AsyncMock(
            return_value={
                "items": [parent_a, parent_b],
                "total": TWO_PARENT_FIXTURE_TOTAL,
                "offset": 0,
                "limit": DEFAULT_PAGE_LIMIT,
            }
        )
        mock_task_api_dep.post = AsyncMock(side_effect=HTTPNotFoundException)

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == TWO_PARENT_FIXTURE_TOTAL
        assert len(body["items"]) == TWO_PARENT_FIXTURE_TOTAL
        by_name = {item["name"]: item for item in body["items"]}
        assert by_name["parent-backup-a"]["status"] is None
        assert by_name["parent-backup-b"]["status"] is None
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-backup-a", "parent-backup-b"]},
        )

    def test_list_with_status_filter_uses_bounded_limit(
        self, test_client, mock_task_api_dep
    ) -> None:
        """When ``status`` is set, fetch parents with bounded limit (not 0)."""
        parent_a = build_backup_task("parent-backup-a")
        parent_b = build_backup_task("parent-backup-b")
        mock_task_api_dep.get = AsyncMock(
            return_value={
                "items": [parent_a, parent_b],
                "total": TWO_PARENT_FIXTURE_TOTAL,
                "offset": 0,
                "limit": MAX_PAGINATION_LIMIT,
            }
        )
        mock_task_api_dep.post = AsyncMock(
            return_value={
                "parent-backup-a": {"status": "success", "finished_at": None},
                "parent-backup-b": {"status": "running", "finished_at": None},
            },
        )

        response = test_client.get(f"{API_BASE}/?status=success")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "parent-backup-a"
        mock_task_api_dep.get.assert_awaited_once_with(
            "/",
            params={
                "owner": OWNER,
                "parent_is_null": "true",
                "backup_type": BackupType.PBM_CONFIG.value,
                "offset": 0,
                "limit": DEFAULT_PAGE_LIMIT,
            },
        )

    @pytest.mark.parametrize(
        "query",
        ["limit=0", "limit=-1", f"limit={MAX_PAGINATION_LIMIT + 1}"],
    )
    def test_list_rejects_invalid_limit(
        self, test_client, mock_task_api_dep, query: str
    ) -> None:
        """List endpoint returns 422 for invalid limits."""
        response = test_client.get(f"{API_BASE}/?{query}")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()


class TestBackupMongoApiCreate:
    """Tests for POST /api/apps/backup_mongo/."""

    def test_create_posts_parent_and_derived_tasks(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """POST creates the parent and four derived tasks via cascade."""
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(
            side_effect=[
                build_backup_task("mongo-backup-task"),
                build_backup_task(
                    "mongo-backup-task-logical",
                    data={
                        "backup_type": BackupType.PBM_LOGICAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                build_backup_task(
                    "mongo-backup-task-physical",
                    data={
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                build_backup_task(
                    "mongo-backup-task-status",
                    data={
                        "backup_type": BackupType.PBM_STATUS.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                build_backup_task(
                    "mongo-backup-task-incremental",
                    data={
                        "backup_type": BackupType.PBM_INCREMENTAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
            ]
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/mongo-backup-task": build_backup_task("mongo-backup-task"),
                "/mongo-backup-task-logical": build_backup_task(
                    "mongo-backup-task-logical",
                    data={
                        "backup_type": BackupType.PBM_LOGICAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                "/mongo-backup-task-physical": build_backup_task(
                    "mongo-backup-task-physical",
                    data={
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                "/mongo-backup-task-status": build_backup_task(
                    "mongo-backup-task-status",
                    data={
                        "backup_type": BackupType.PBM_STATUS.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                "/mongo-backup-task-incremental": build_backup_task(
                    "mongo-backup-task-incremental",
                    data={
                        "backup_type": BackupType.PBM_INCREMENTAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
            }
        )

        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_201_CREATED
        create_body = response.json()
        assert "service_type" not in create_body
        assert "owner" not in create_body
        assert "anonymize_mask" in create_body
        assert "anonymized_entities" in create_body
        assert "connectivity_warning" in create_body
        assert mock_task_api_dep.post.await_count == EXPECTED_CASCADE_POSTS
        first_post = mock_task_api_dep.post.await_args_list[0].kwargs["json"]
        assert first_post["owner"] == "BACKUP_MONGO"
        assert first_post["data"]["backup_type"] == BackupType.PBM_CONFIG.value
        assert "service_id" in first_post["data"][RESERVED_FORM_KEY]
        logical_post = mock_task_api_dep.post.await_args_list[1].kwargs["json"]
        assert logical_post["data"]["payload"].endswith("pbm_logical_payload")
        assert logical_post["data"]["parent"] == "mongo-backup-task"
        assert RESERVED_FORM_KEY not in logical_post["data"]

    def test_create_resolves_actor_usernames(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
        known_actors: tuple[CasdoorUser, CasdoorUser],
    ) -> None:
        """Render both actors' usernames on the cascade-create response."""
        creator, updater = known_actors
        group = _backup_group_by_path(
            build_backup_task(
                "mongo-backup-task",
                created_by=str(creator.id),
                last_updated_by=str(updater.id),
            )
        )
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(side_effect=list(group.values()))
        mock_task_api_dep.get = mock_task_api_get_by_path(group)

        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert (body["created_by"], body["last_updated_by"]) == (
            creator.username,
            updater.username,
        )

    def test_create_rolls_back_on_mid_chain_failure(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Rollback DELETEs created tasks when a derived POST fails."""
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(
            side_effect=[
                build_backup_task("mongo-backup-task"),
                build_backup_task(
                    "mongo-backup-task-logical",
                    data={
                        "backup_type": BackupType.PBM_LOGICAL.value,
                        "parent": "mongo-backup-task",
                    },
                ),
                HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR),
            ]
        )
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert mock_task_api_dep.delete.await_args_list == [
            call("/mongo-backup-task-logical"),
            call("/mongo-backup-task"),
        ]

    def test_create_rejects_malformed_priority_yaml(self, test_client) -> None:
        """Reject malformed Node Priority YAML with 422 before touching the APIs."""
        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(
                backup_priority='"h1:27018": 2 "h2:27018": 2',
            ),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_rejects_s3_storage_without_bucket(self, test_client) -> None:
        """Reject an S3 storage config missing a bucket with 422 on the JSON path."""
        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(
                storage_type="s3",
                storage_s3_region="eu-west-1",
                storage_filesystem_path=None,
            ),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestBackupMongoApiDetail:
    """Tests for GET /api/apps/backup_mongo/{task_name}."""

    def test_detail_includes_derived_tasks(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Detail aggregates derived sibling statuses."""
        parent = build_backup_task("parent-backup")
        logical = build_backup_task(
            "parent-backup-logical",
            data={
                "backup_type": BackupType.PBM_LOGICAL.value,
                "parent": "parent-backup",
            },
        )
        physical = build_backup_task(
            "parent-backup-physical",
            data={
                "backup_type": BackupType.PBM_PHYSICAL.value,
                "parent": "parent-backup",
            },
        )
        status_task = build_backup_task(
            "parent-backup-status",
            data={
                "backup_type": BackupType.PBM_STATUS.value,
                "parent": "parent-backup",
            },
        )
        incremental = build_backup_task(
            "parent-backup-incremental",
            data={
                "backup_type": BackupType.PBM_INCREMENTAL.value,
                "parent": "parent-backup",
            },
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-backup": parent,
                "/parent-backup-logical": logical,
                "/parent-backup-physical": physical,
                "/parent-backup-status": status_task,
                "/parent-backup-incremental": incremental,
            }
        )

        response = test_client.get(f"{API_BASE}/parent-backup")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["name"] == "parent-backup"
        assert "service_type" not in body
        assert "owner" not in body
        assert "anonymize_mask" in body
        assert "anonymized_entities" in body
        derived_names = {item["name"] for item in body["derived_tasks"]}
        assert derived_names == {
            "parent-backup-logical",
            "parent-backup-physical",
            "parent-backup-status",
            "parent-backup-incremental",
        }

    def test_detail_tolerates_history_fetch_failure_for_one_derived(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 200 when one derived history fetch fails."""
        parent = build_backup_task("parent-backup")
        logical = build_backup_task(
            "parent-backup-logical",
            data={
                "backup_type": BackupType.PBM_LOGICAL.value,
                "parent": "parent-backup",
            },
        )
        physical = build_backup_task(
            "parent-backup-physical",
            data={
                "backup_type": BackupType.PBM_PHYSICAL.value,
                "parent": "parent-backup",
            },
        )
        status_task = build_backup_task(
            "parent-backup-status",
            data={
                "backup_type": BackupType.PBM_STATUS.value,
                "parent": "parent-backup",
            },
        )
        incremental = build_backup_task(
            "parent-backup-incremental",
            data={
                "backup_type": BackupType.PBM_INCREMENTAL.value,
                "parent": "parent-backup",
            },
        )
        history_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        tasks_by_path = {
            "/parent-backup": parent,
            "/parent-backup-logical": logical,
            "/parent-backup-physical": physical,
            "/parent-backup-status": status_task,
            "/parent-backup-incremental": incremental,
        }

        async def _mock_get(path: str, **kwargs: Any) -> Any:
            if path == "/parent-backup-physical/history/":
                raise history_exc
            if path.endswith("/history/"):
                return {"items": []}
            if path in tasks_by_path:
                return tasks_by_path[path]
            raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_mock_get)

        response = test_client.get(f"{API_BASE}/parent-backup")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["derived_tasks"]) == THREE_DERIVED_SIBLINGS
        derived_names = {item["name"] for item in body["derived_tasks"]}
        assert derived_names == {
            "parent-backup-logical",
            "parent-backup-status",
            "parent-backup-incremental",
        }


class TestBackupMongoApiDelete:
    """Tests for DELETE /api/apps/backup_mongo/{task_name}."""

    def test_delete_removes_parent_and_all_derived_tasks(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE cascades to every derived sibling before the parent."""
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = AsyncMock(return_value=parent)
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.delete(f"{API_BASE}/parent-backup")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert mock_task_api_dep.delete.await_args_list == [
            call("/parent-backup-logical"),
            call("/parent-backup-physical"),
            call("/parent-backup-status"),
            call("/parent-backup-incremental"),
            call("/parent-backup"),
        ]

    def test_delete_returns_500_when_cascade_delete_partially_fails(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 500 when a derived sibling DELETE fails with a non-404 error."""
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = AsyncMock(return_value=parent)
        derived_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        async def _delete(path: str) -> None:
            if path == "/parent-backup-physical":
                raise derived_exc

        mock_task_api_dep.delete = AsyncMock(side_effect=_delete)

        response = test_client.delete(f"{API_BASE}/parent-backup")

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = response.json()["detail"]
        assert detail["message"] == "Partial delete failure; orphaned tasks"
        assert "parent-backup-physical" in detail["errors"]


class TestBackupMongoApiUpdate:
    """Tests for PUT /api/apps/backup_mongo/{task_name}."""

    def test_update_puts_parent_and_derived_and_restamps_form(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Cascade-update the parent plus derived legs and re-stamp ``_form``."""
        parent = build_backup_task("parent-backup")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-backup": parent,
                "/parent-backup-logical": build_backup_task(
                    "parent-backup-logical",
                    data={
                        "backup_type": BackupType.PBM_LOGICAL.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-physical": build_backup_task(
                    "parent-backup-physical",
                    data={
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-status": build_backup_task(
                    "parent-backup-status",
                    data={
                        "backup_type": BackupType.PBM_STATUS.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-incremental": build_backup_task(
                    "parent-backup-incremental",
                    data={
                        "backup_type": BackupType.PBM_INCREMENTAL.value,
                        "parent": "parent-backup",
                    },
                ),
            }
        )
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.put.await_count == EXPECTED_CASCADE_PUTS
        parent_put = mock_task_api_dep.put.await_args_list[0]
        assert parent_put.args == ("/parent-backup",)
        parent_payload = parent_put.kwargs["json"]
        assert parent_payload["name"] == "parent-backup"
        assert parent_payload["data"]["backup_type"] == BackupType.PBM_CONFIG.value
        assert "service_id" in parent_payload["data"][RESERVED_FORM_KEY]
        logical_put = mock_task_api_dep.put.await_args_list[1]
        assert logical_put.args == ("/parent-backup-logical",)
        assert logical_put.kwargs["json"]["data"]["parent"] == "parent-backup"
        assert RESERVED_FORM_KEY not in logical_put.kwargs["json"]["data"]

    def test_update_resolves_actor_usernames(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
        known_actors: tuple[CasdoorUser, CasdoorUser],
    ) -> None:
        """Render both actors' usernames on the cascade-update response."""
        creator, updater = known_actors
        parent = build_backup_task(
            "parent-backup",
            created_by=str(creator.id),
            last_updated_by=str(updater.id),
        )
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path(_backup_group_by_path(parent))
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup", service_id=mongo_service.id
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert (body["created_by"], body["last_updated_by"]) == (
            creator.username,
            updater.username,
        )

    def test_update_resolves_satellite_url_to_parent(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Resolve a derived-sibling URL to the parent and update the group."""
        parent = build_backup_task("parent-backup")
        satellite = build_backup_task(
            "parent-backup-logical",
            data={
                "backup_type": BackupType.PBM_LOGICAL.value,
                "parent": "parent-backup",
            },
        )
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-backup": parent,
                "/parent-backup-logical": satellite,
                "/parent-backup-physical": build_backup_task(
                    "parent-backup-physical",
                    data={
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-status": build_backup_task(
                    "parent-backup-status",
                    data={
                        "backup_type": BackupType.PBM_STATUS.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-incremental": build_backup_task(
                    "parent-backup-incremental",
                    data={
                        "backup_type": BackupType.PBM_INCREMENTAL.value,
                        "parent": "parent-backup",
                    },
                ),
            }
        )
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup-logical",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.put.await_args_list[0].args == ("/parent-backup",)

    def test_update_rejects_parent_rename_with_409(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Reject a submitted task_name differing from the path parent."""
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-backup": parent})
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="renamed-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    def test_update_protected_task_returns_409(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Reject updates to protected backup parent tasks."""
        parent = build_backup_task("parent-backup", protected=True)
        mock_task_api_dep.get = AsyncMock(return_value=parent)
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    def test_update_running_conflict_returns_409(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Reject an update while a conflicting task run is in flight."""
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = _running_group_get_mock("parent-backup", parent)
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    def test_update_via_satellite_blocked_when_parent_running(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Block a satellite-addressed update when the resolved parent is running.

        A ``PUT`` to an idle derived leg must resolve to the parent and check the
        parent's history, not the leg's, so a running group cannot be edited.
        """
        parent = build_backup_task("parent-backup")
        satellite = build_backup_task(
            "parent-backup-logical",
            data={
                "backup_type": BackupType.PBM_LOGICAL.value,
                "parent": "parent-backup",
            },
        )
        mock_task_api_dep.get = _running_group_get_mock(
            "parent-backup", parent, extra_tasks={"/parent-backup-logical": satellite}
        )
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup-logical",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()
        history_paths = [c.args[0] for c in mock_task_api_dep.get.await_args_list]
        assert "/parent-backup/history/" in history_paths

    def test_update_blocked_when_derived_leg_running(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Block an edit while a derived leg is running though the parent is idle.

        Backups execute on the ``-logical`` / ``-physical`` legs, not the parent
        config task, so checking only the parent's history would let a ``PUT``
        mutate a running group.
        """
        parent = build_backup_task("parent-backup")
        mock_task_api_dep.get = _running_leg_get_mock(
            "parent-backup", parent, "parent-backup-logical"
        )
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    def test_update_returns_500_when_cascade_partially_fails(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Return 500 naming the failed leg when a derived PUT fails mid-cascade."""
        parent = build_backup_task("parent-backup")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {"/parent-backup": parent},
            not_found=(
                "/parent-backup-logical",
                "/parent-backup-physical",
                "/parent-backup-status",
                "/parent-backup-incremental",
            ),
        )
        mock_task_api_dep.post = AsyncMock(return_value={})
        derived_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        async def _put(path: str, **kwargs: Any) -> Any:
            if path == "/parent-backup-physical":
                raise derived_exc
            return parent

        mock_task_api_dep.put = AsyncMock(side_effect=_put)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "parent-backup-physical" in response.json()["detail"]

    def test_update_backfills_missing_incremental_sibling(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Create a missing ``-incremental`` sibling before cascading PUTs.

        Groups created before the incremental leg existed lack it. Updating must
        backfill it rather than partially mutate the group and then 500 on a
        missing PUT.
        """
        parent = build_backup_task("parent-backup")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-backup": parent,
                "/parent-backup-logical": build_backup_task(
                    "parent-backup-logical",
                    data={
                        "backup_type": BackupType.PBM_LOGICAL.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-physical": build_backup_task(
                    "parent-backup-physical",
                    data={
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                        "parent": "parent-backup",
                    },
                ),
                "/parent-backup-status": build_backup_task(
                    "parent-backup-status",
                    data={
                        "backup_type": BackupType.PBM_STATUS.value,
                        "parent": "parent-backup",
                    },
                ),
            },
            not_found=("/parent-backup-incremental",),
        )
        mock_task_api_dep.post = AsyncMock(return_value={})
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-backup",
            json=build_backup_write_body(
                task_name="parent-backup",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        mock_task_api_dep.post.assert_awaited_once()
        posted = mock_task_api_dep.post.await_args.kwargs["json"]
        assert posted["name"] == "parent-backup-incremental"
        assert posted["data"]["backup_type"] == BackupType.PBM_INCREMENTAL.value
        assert mock_task_api_dep.put.await_count == EXPECTED_CASCADE_PUTS
        put_paths = [call.args[0] for call in mock_task_api_dep.put.await_args_list]
        assert "/parent-backup-incremental" in put_paths

    def test_update_returns_404_for_unknown_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 404 when the PUT addresses an unknown task name."""
        mock_task_api_dep.get = AsyncMock(side_effect=HTTPNotFoundException())
        mock_task_api_dep.put = AsyncMock()

        response = test_client.put(
            f"{API_BASE}/ghost-task",
            json=build_backup_write_body(task_name="ghost-task", service_id=1),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_task_api_dep.put.assert_not_awaited()


class TestBackupMongoApiExecute:
    """Tests for POST /api/apps/backup_mongo/{task_name}/execute."""

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_201_with_task_name_and_id(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing a backup task returns 201 with task_name and task_id."""
        expected_task_id = 42
        task = build_backup_task("mongo-backup-task")
        mock_task_api_dep.get = AsyncMock(return_value=task)
        mock_task_api_dep.post = AsyncMock(
            return_value=build_execute_response(expected_task_id)
        )

        response = test_client.post(f"{API_BASE}/mongo-backup-task/execute", json={})

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["task_name"] == "mongo-backup-task"
        assert data["task_id"] == expected_task_id
        mock_task_api_dep.post.assert_awaited_once_with(
            "/execute/mongo-backup-task", json={}
        )

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_404_for_unknown_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing an unknown task name returns 404."""
        mock_task_api_dep.get = AsyncMock(side_effect=HTTPNotFoundException())

        response = test_client.post(f"{API_BASE}/ghost-task/execute", json={})

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestBackupMongoApiAuth:
    """Tests for API authentication."""

    def test_unauthenticated_list_returns_401(self, unauthenticated_client) -> None:
        """Reject unauthenticated access to the backup_mongo API."""
        response = unauthenticated_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
