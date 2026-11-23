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

"""Tests for restore JSON API routes under /api/apps/backup_mongo/restore/."""

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, call, patch

import pytest
import yaml
from fastapi import HTTPException, status

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.exceptions import HTTPNotFoundException
from app.core.pagination import MAX_PAGINATION_LIMIT
from app.extensions.apps.backup_mongo.models import BackupType
from app.extensions.apps.backup_mongo.restore.spec import RESTORE_CONFIG_PAYLOAD_MARKER
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.inventory import CreatedService
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.models import TaskBackendEnum, TaskHistoryStatusEnum
from tests.app.extensions.apps.framework.kit import EXECUTE_CREATED_AT, EXECUTE_STATUS
from tests.app.extensions.unknown_body_field import (
    assert_unknown_field_rejected,
    UNKNOWN_FIELD,
)
from tests.app.factories import TaskFactory

API_BASE = "/api/apps/backup_mongo/restore"
EMAIL_MASK = PIIEntity.encode_selection({PIIEntity.EMAIL_ADDRESS})
EXPECTED_EMAIL_ENTITIES = [PIIEntity.EMAIL_ADDRESS.name]
# Pin a non-empty fallback set so mask=None coverage stays meaningful under the
# empty development DEFAULT_ENTITIES profile.
FALLBACK_DEFAULT_ENTITY_SET = {PIIEntity.CREDIT_CARD, PIIEntity.EMAIL_ADDRESS}
EXPECTED_FALLBACK_DEFAULT_ENTITIES = sorted(
    entity.name for entity in FALLBACK_DEFAULT_ENTITY_SET
)


@contextmanager
def patch_fallback_default_entities() -> Iterator[None]:
    """Patch response defaults to a known non-empty entity set for mask=None tests."""
    with patch(
        "app.extensions.apps.framework.responses.anonymizer_settings"
    ) as mock_settings:
        mock_settings.DEFAULT_ENTITIES = defaultdict(
            lambda: FALLBACK_DEFAULT_ENTITY_SET
        )
        yield


EXPECTED_LOGICAL_RESTORE_POSTS = 3
EXPECTED_PHYSICAL_RESTORE_POSTS = 4
DEFAULT_PAGE_LIMIT = 50
EXPECTED_LOGICAL_RESTORE_PUTS = 3
THREE_PARENT_FIXTURE_TOTAL = 3
TWO_PARENT_FIXTURE_TOTAL = 2
EXPECTED_RESTORE_PARENT_LIST_GETS = 2


def build_restore_task(name: str = "mongo-restore-task", **overrides: Any) -> dict:
    """Build a fake restore_mongo task payload for route tests."""
    data_overrides = overrides.pop("data", {})
    parent = data_overrides.pop("parent", None)
    backup_type = data_overrides.pop("backup_type", BackupType.PBM_LOGICAL.value)
    is_parent = parent is None and RESTORE_CONFIG_PAYLOAD_MARKER in str(
        data_overrides.get("payload", RESTORE_CONFIG_PAYLOAD_MARKER)
    )
    if is_parent and "payload" not in data_overrides:
        payload_file = RESTORE_CONFIG_PAYLOAD_MARKER
    elif parent:
        if backup_type == BackupType.PBM_LOGICAL.value:
            payload_file = "pbm_logical_restore_payload"
        else:
            payload_file = "pbm_physical_restore_payload"
    elif "pbm-list" in name:
        payload_file = "pbm_list_payload"
    elif "pbm-force-resync" in name:
        payload_file = "pbm_force_resync_payload"
    else:
        payload_file = RESTORE_CONFIG_PAYLOAD_MARKER

    config = yaml.dump(
        {
            "backupType": backup_type,
            "backupSource": "2026-04-29T10:00:00",
        },
        default_flow_style=False,
    )
    task = TaskFactory.build(
        name=name,
        owner="RESTORE_MONGO",
        backend=TaskBackendEnum.PROXY,
        **overrides,
    )
    payload = task.model_dump(mode="json")
    payload["data"] = {
        "task": "run-python",
        "meta": {
            "target": "mongo-restore-host",
            "config": config,
            "requirements": "packaging\nPyYAML",
        },
        "payload": f"file:///plugins/backup_mongo/restore/{payload_file}",
        **data_overrides,
    }
    if parent is not None:
        payload["data"]["parent"] = parent
    return payload


def build_restore_write_body(
    task_name: str = "mongo-restore-task",
    hostname: str = "mongo-restore-host",
    service_id: int | None = 1,
    backup_type: str = BackupType.PBM_LOGICAL.value,
    **kwargs: Any,
) -> dict:
    """Build a valid RestoreTaskWrite-compatible request body."""
    return {
        "task_name": task_name,
        "hostname": hostname,
        "service_id": service_id,
        "backup_type": backup_type,
        "backup_source": "2026-04-29T10:00:00",
        **kwargs,
    }


def mock_task_api_get_by_path(tasks_by_path: dict[str, Any]) -> AsyncMock:
    """Return a path-keyed ``tasks_api.get`` mock safe for parallel fetches."""

    async def _mock_get(path: str, **kwargs: Any) -> Any:
        if path.endswith("/history/"):
            return {"items": []}
        if path in tasks_by_path:
            return tasks_by_path[path]
        raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

    return AsyncMock(side_effect=_mock_get)


def _running_group_get_mock(parent_name: str, parent: dict) -> AsyncMock:
    """Return a ``tasks_api.get`` mock reporting the parent group as running."""

    async def _mock_get(path: str, params: dict | None = None, **kwargs: Any) -> Any:
        if path == f"/{parent_name}/history/":
            if params and params.get("status") == TaskHistoryStatusEnum.RUNNING:
                return {"items": [{"id": 1}]}
            return {"items": []}
        if path.endswith("/history/"):
            return {"items": []}
        if path == f"/{parent_name}":
            return parent
        raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

    return AsyncMock(side_effect=_mock_get)


def _running_leg_get_mock(
    parent_name: str, parent: dict, running_leg: str
) -> AsyncMock:
    """Return a ``tasks_api.get`` mock reporting a child leg (not the parent) as running."""

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


def mock_task_api_parent_list(*parents: dict) -> AsyncMock:
    """Return a ``tasks_api.get`` mock serving ``parents`` as the null-parent page."""

    async def _mock_get(path: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        if params.get("parent_is_null") == "true":
            return {"items": list(parents), "total": len(parents)}
        return {"items": [], "total": 0}

    return AsyncMock(side_effect=_mock_get)


class TestRestoreMongoAppSchemaEndpoint:
    """Tests for GET /api/apps/backup_mongo/restore/schema."""

    def test_schema_returns_200(self, test_client):
        """Ensure the schema endpoint returns HTTP 200 with JSON content."""
        response = test_client.get(f"{API_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]

    def test_schema_name_is_backup_mongo_restores(self, test_client):
        """Ensure the restores schema uses the dedicated plugin name."""
        response = test_client.get(f"{API_BASE}/schema")

        assert response.json()["name"] == "backup_mongo_restores"

    def test_schema_capabilities(self, test_client):
        """Serve ``scheduling: false`` so every schedule control stays hidden.

        A scheduled restore would re-run a destructive restore of one fixed
        ``backup_source`` on every tick with nobody present to confirm the target.
        """
        response = test_client.get(f"{API_BASE}/schema")

        assert response.json()["capabilities"] == {
            "chaining": True,
            "alert_on_fail": False,
            "scheduling": False,
            "stats": False,
            "pii_anonymization": False,
        }

    def test_schema_collapses_restore_options_and_defaults_task_name(self, test_client):
        """Collapse Restore Options by default and pre-fill task_name."""
        response = test_client.get(f"{API_BASE}/schema")
        body = response.json()
        sections = {section["title"]: section for section in body["forms"]}

        task = sections["Task"]
        assert task["collapsible"] is False
        assert task["collapsed_by_default"] is False
        task_name = next(
            field for field in task["fields"] if field["name"] == "task_name"
        )
        assert task_name["default"] == "mongodb-restore"

        restore_options = sections["Restore Options"]
        assert restore_options["collapsible"] is True
        assert restore_options["collapsed_by_default"] is True
        assert {field["name"] for field in restore_options["fields"]} == {
            "restore_namespace_filter",
            "restore_batch_size",
            "restore_num_insertion_workers",
            "restore_num_parallel_collections",
            "restore_num_download_workers",
            "restore_max_download_buffer_mb",
            "restore_download_chunk_mb",
            "restore_mongod_location",
            "restore_mongod_location_map",
        }


class TestRestoreMongoApiList:
    """Tests for GET /api/apps/backup_mongo/restore/."""

    def test_list_returns_parent_tasks_only(
        self, test_client, mock_task_api_dep
    ) -> None:
        """List parent rows via upstream filters, self-parent merge, and batch status."""
        parent = build_restore_task("parent-restore")
        legacy_self_parent = build_restore_task(
            "legacy-self-parent-restore",
            data={
                "parent": "legacy-self-parent-restore",
                "payload": "pbm_logical_restore_payload",
            },
        )

        async def _mock_get(path: str, **kwargs: Any) -> Any:
            if path != "/":
                raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")
            params = kwargs.get("params") or {}
            if params.get("parent_is_null") == "true":
                return {"items": [parent], "total": 1}
            if (
                params.get("parent_is_null") == "false"
                and params.get("self_parent") == "true"
            ):
                return {"items": [legacy_self_parent], "total": 1}
            raise AssertionError(f"Unexpected list params: {params!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_mock_get)
        mock_task_api_dep.post = AsyncMock(
            return_value={
                "parent-restore": {
                    "status": "success",
                    "finished_at": "2026-05-01T12:00:00",
                }
            }
        )

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == TWO_PARENT_FIXTURE_TOTAL
        assert body["offset"] == 0
        assert body["limit"] == DEFAULT_PAGE_LIMIT
        assert len(body["items"]) == TWO_PARENT_FIXTURE_TOTAL
        assert body["items"][0]["name"] == "parent-restore"
        assert body["items"][0]["status"] == "success"
        assert body["items"][0]["last_executed_at"] == "2026-05-01T12:00:00Z"
        assert body["items"][1]["name"] == "legacy-self-parent-restore"
        assert body["items"][1]["status"] is None
        assert body["items"][1]["last_executed_at"] is None
        assert mock_task_api_dep.get.await_count == EXPECTED_RESTORE_PARENT_LIST_GETS
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-restore", "legacy-self-parent-restore"]},
        )

    def test_list_exposes_framework_task_fields(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Expose the shared BaseTaskResponse fields on list rows for API parity."""
        parent = build_restore_task("parent-restore", anonymize_mask=EMAIL_MASK)

        mock_task_api_dep.get = mock_task_api_parent_list(parent)
        mock_task_api_dep.post = AsyncMock(return_value={})

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert item["anonymize_mask"] == EMAIL_MASK
        assert item["anonymized_entities"] == EXPECTED_EMAIL_ENTITIES
        assert item["connectivity_warning"] is None
        assert "service_type" not in item
        assert "owner" not in item
        assert item["hostname"] == "mongo-restore-host"
        assert item["backup_type"] == "pbm_logical"
        assert item["backup_source"] == "2026-04-29T10:00:00"

    def test_list_anonymized_entities_falls_back_when_mask_none(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Resolve anonymized_entities without erroring when anonymize_mask is null."""
        parent = build_restore_task("parent-restore", anonymize_mask=None)

        mock_task_api_dep.get = mock_task_api_parent_list(parent)
        mock_task_api_dep.post = AsyncMock(return_value={})

        with patch_fallback_default_entities():
            response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert item["anonymize_mask"] is None
        assert item["anonymized_entities"] == EXPECTED_FALLBACK_DEFAULT_ENTITIES

    def test_list_paginates_with_offset_and_limit(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Paginate merged null-parent rows after two upstream list calls."""
        parents = [
            build_restore_task("parent-restore-a"),
            build_restore_task("parent-restore-b"),
            build_restore_task("parent-restore-c"),
        ]

        async def _mock_get(path: str, **kwargs: Any) -> Any:
            if path != "/":
                raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")
            params = kwargs.get("params") or {}
            if params.get("parent_is_null") == "true":
                return {"items": parents, "total": THREE_PARENT_FIXTURE_TOTAL}
            if (
                params.get("parent_is_null") == "false"
                and params.get("self_parent") == "true"
            ):
                return {"items": [], "total": 0}
            raise AssertionError(f"Unexpected list params: {params!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_mock_get)
        mock_task_api_dep.post = AsyncMock(
            return_value={
                "parent-restore-b": {"status": "running", "finished_at": None}
            },
        )

        response = test_client.get(f"{API_BASE}/?offset=1&limit=1")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == THREE_PARENT_FIXTURE_TOTAL
        assert body["offset"] == 1
        assert body["limit"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "parent-restore-b"
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-restore-b"]},
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

    def test_list_tolerates_batch_status_fetch_failure(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 200 with unknown status when batch latest-status lookup fails."""
        parent_a = build_restore_task("parent-restore-a")
        parent_b = build_restore_task("parent-restore-b")

        async def _mock_get(path: str, **kwargs: Any) -> Any:
            if path != "/":
                raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")
            params = kwargs.get("params") or {}
            if params.get("parent_is_null") == "true":
                return {
                    "items": [parent_a, parent_b],
                    "total": TWO_PARENT_FIXTURE_TOTAL,
                }
            if (
                params.get("parent_is_null") == "false"
                and params.get("self_parent") == "true"
            ):
                return {"items": [], "total": 0}
            raise AssertionError(f"Unexpected list params: {params!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_mock_get)
        mock_task_api_dep.post = AsyncMock(side_effect=HTTPNotFoundException)

        response = test_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == TWO_PARENT_FIXTURE_TOTAL
        assert len(body["items"]) == TWO_PARENT_FIXTURE_TOTAL
        by_name = {item["name"]: item for item in body["items"]}
        assert by_name["parent-restore-a"]["status"] is None
        assert by_name["parent-restore-b"]["status"] is None
        mock_task_api_dep.post.assert_awaited_once_with(
            "/history/latest",
            json={"names": ["parent-restore-a", "parent-restore-b"]},
        )

    @pytest.mark.parametrize(
        "query",
        ["", f"?status={TaskHistoryStatusEnum.SUCCESS.value}"],
        ids=["unfiltered", "status-filtered"],
    )
    def test_list_resolves_actor_usernames(
        self,
        test_client,
        mock_task_api_dep,
        known_actors: tuple[CasdoorUser, CasdoorUser],
        query: str,
    ) -> None:
        """Render both actors' usernames on list rows on either list branch."""
        creator, updater = known_actors
        parent = build_restore_task(
            "parent-restore",
            created_by=str(creator.id),
            last_updated_by=str(updater.id),
        )
        mock_task_api_dep.get = mock_task_api_parent_list(parent)
        mock_task_api_dep.post = AsyncMock(
            return_value={
                "parent-restore": {
                    "status": TaskHistoryStatusEnum.SUCCESS.value,
                    "finished_at": None,
                }
            }
        )

        response = test_client.get(f"{API_BASE}/{query}")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert (item["created_by"], item["last_updated_by"]) == (
            creator.username,
            updater.username,
        )


class TestRestoreMongoApiCreate:
    """Tests for POST /api/apps/backup_mongo/restore/."""

    def test_create_logical_posts_three_tasks(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """POST creates config, restore leg, and pbm-list for logical restores."""
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(
            side_effect=[
                build_restore_task("mongo-restore-task"),
                build_restore_task(
                    "mongo-restore-task-pbm_logical",
                    data={"parent": "mongo-restore-task"},
                ),
                build_restore_task(
                    "mongo-restore-task-pbm-list",
                    data={"parent": "mongo-restore-task"},
                ),
            ]
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/mongo-restore-task": build_restore_task("mongo-restore-task"),
                "/mongo-restore-task-pbm_logical": build_restore_task(
                    "mongo-restore-task-pbm_logical",
                    data={"parent": "mongo-restore-task"},
                ),
                "/mongo-restore-task-pbm-list": build_restore_task(
                    "mongo-restore-task-pbm-list",
                    data={"parent": "mongo-restore-task"},
                ),
            }
        )

        response = test_client.post(
            f"{API_BASE}/",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert mock_task_api_dep.post.await_count == EXPECTED_LOGICAL_RESTORE_POSTS
        first_post = mock_task_api_dep.post.await_args_list[0].kwargs["json"]
        assert first_post["owner"] == "RESTORE_MONGO"
        assert RESTORE_CONFIG_PAYLOAD_MARKER in first_post["data"]["payload"]
        assert "service_id" in first_post["data"][RESERVED_FORM_KEY]
        restore_leg_post = mock_task_api_dep.post.await_args_list[1].kwargs["json"]
        assert RESERVED_FORM_KEY not in restore_leg_post["data"]
        body = response.json()
        for field in (
            "anonymize_mask",
            "connectivity_warning",
            "anonymized_entities",
        ):
            assert field in body
        assert body["connectivity_warning"] is None
        assert "service_type" not in body
        assert "owner" not in body

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
        parent = build_restore_task(
            "mongo-restore-task",
            created_by=str(creator.id),
            last_updated_by=str(updater.id),
        )
        restore_leg = build_restore_task(
            "mongo-restore-task-pbm_logical", data={"parent": "mongo-restore-task"}
        )
        pbm_list = build_restore_task(
            "mongo-restore-task-pbm-list", data={"parent": "mongo-restore-task"}
        )
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(side_effect=[parent, restore_leg, pbm_list])
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/mongo-restore-task": parent,
                "/mongo-restore-task-pbm_logical": restore_leg,
                "/mongo-restore-task-pbm-list": pbm_list,
            }
        )

        response = test_client.post(
            f"{API_BASE}/",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert (body["created_by"], body["last_updated_by"]) == (
            creator.username,
            updater.username,
        )

    def test_create_physical_posts_four_tasks(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """POST adds force-resync for physical restores."""
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(
            side_effect=[
                build_restore_task(
                    "mongo-restore-task",
                    data={"backup_type": BackupType.PBM_PHYSICAL.value},
                ),
                build_restore_task(
                    "mongo-restore-task-pbm_physical",
                    data={
                        "parent": "mongo-restore-task",
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                    },
                ),
                build_restore_task(
                    "mongo-restore-task-pbm-list",
                    data={"parent": "mongo-restore-task"},
                ),
                build_restore_task(
                    "mongo-restore-task-pbm-force-resync",
                    data={"parent": "mongo-restore-task"},
                ),
            ]
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/mongo-restore-task": build_restore_task(
                    "mongo-restore-task",
                    data={"backup_type": BackupType.PBM_PHYSICAL.value},
                ),
                "/mongo-restore-task-pbm_physical": build_restore_task(
                    "mongo-restore-task-pbm_physical",
                    data={
                        "parent": "mongo-restore-task",
                        "backup_type": BackupType.PBM_PHYSICAL.value,
                    },
                ),
                "/mongo-restore-task-pbm-list": build_restore_task(
                    "mongo-restore-task-pbm-list",
                    data={"parent": "mongo-restore-task"},
                ),
                "/mongo-restore-task-pbm-force-resync": build_restore_task(
                    "mongo-restore-task-pbm-force-resync",
                    data={"parent": "mongo-restore-task"},
                ),
            }
        )

        response = test_client.post(
            f"{API_BASE}/",
            json=build_restore_write_body(
                service_id=mongo_service.id,
                backup_type=BackupType.PBM_PHYSICAL.value,
            ),
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert mock_task_api_dep.post.await_count == EXPECTED_PHYSICAL_RESTORE_POSTS

    def test_create_rejects_namespace_filter_for_physical_restore(
        self,
        test_client,
        mock_task_api_dep,
    ) -> None:
        """Reject selective physical restores before creating any tasks."""
        response = test_client.post(
            f"{API_BASE}/",
            json=build_restore_write_body(
                backup_type=BackupType.PBM_PHYSICAL.value,
                restore_namespace_filter="db.collection",
            ),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert (
            "Namespace Filter is only supported for logical MongoDB restores"
            in response.text
        )
        mock_task_api_dep.post.assert_not_awaited()

    def test_create_refuses_an_unknown_field(
        self, test_client, mock_task_api_dep, mongo_service: CreatedService
    ) -> None:
        """Answer 422 naming a key the body model does not declare, creating nothing."""
        body = build_restore_write_body(service_id=mongo_service.id)
        body[UNKNOWN_FIELD] = "value"

        response = test_client.post(f"{API_BASE}/", json=body)

        assert_unknown_field_rejected(response)
        mock_task_api_dep.post.assert_not_called()

    def test_create_rolls_back_on_mid_chain_failure(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Rollback DELETEs created tasks when a child POST fails."""
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(
            side_effect=[
                build_restore_task("mongo-restore-task"),
                build_restore_task(
                    "mongo-restore-task-pbm_logical",
                    data={"parent": "mongo-restore-task"},
                ),
                HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR),
            ]
        )
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.post(
            f"{API_BASE}/",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert mock_task_api_dep.delete.await_args_list == [
            call("/mongo-restore-task-pbm_logical"),
            call("/mongo-restore-task"),
        ]


class TestRestoreMongoApiDetail:
    """Tests for GET /api/apps/backup_mongo/restore/{task_name}."""

    def test_detail_includes_child_tasks(self, test_client, mock_task_api_dep) -> None:
        """Detail aggregates child task statuses."""
        parent = build_restore_task("parent-restore")
        restore_leg = build_restore_task(
            "parent-restore-pbm_logical",
            data={"parent": "parent-restore"},
        )
        pbm_list = build_restore_task(
            "parent-restore-pbm-list",
            data={"parent": "parent-restore"},
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-restore": parent,
                "/parent-restore-pbm_logical": restore_leg,
                "/parent-restore-pbm-list": pbm_list,
            }
        )

        response = test_client.get(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["name"] == "parent-restore"
        child_names = {item["name"] for item in body["derived_tasks"]}
        assert child_names == {
            "parent-restore-pbm_logical",
            "parent-restore-pbm-list",
        }

    def test_detail_exposes_framework_task_fields(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Expose the shared BaseTaskResponse fields on detail alongside children."""
        parent = build_restore_task("parent-restore", anonymize_mask=EMAIL_MASK)
        restore_leg = build_restore_task(
            "parent-restore-pbm_logical",
            data={"parent": "parent-restore"},
        )
        pbm_list = build_restore_task(
            "parent-restore-pbm-list",
            data={"parent": "parent-restore"},
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-restore": parent,
                "/parent-restore-pbm_logical": restore_leg,
                "/parent-restore-pbm-list": pbm_list,
            }
        )

        response = test_client.get(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["anonymize_mask"] == EMAIL_MASK
        assert body["anonymized_entities"] == EXPECTED_EMAIL_ENTITIES
        assert body["connectivity_warning"] is None
        assert "service_type" not in body
        assert "owner" not in body
        assert body["hostname"] == "mongo-restore-host"
        assert body["backup_type"] == "pbm_logical"
        assert body["backup_source"] == "2026-04-29T10:00:00"
        assert "derived_tasks" in body

    def test_detail_resolves_actor_usernames(
        self,
        test_client,
        mock_task_api_dep,
        known_actors: tuple[CasdoorUser, CasdoorUser],
    ) -> None:
        """Render both actors' usernames on the sibling-aggregating detail."""
        creator, updater = known_actors
        parent = build_restore_task(
            "parent-restore",
            created_by=str(creator.id),
            last_updated_by=str(updater.id),
        )
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {
                "/parent-restore": parent,
                "/parent-restore-pbm_logical": build_restore_task(
                    "parent-restore-pbm_logical", data={"parent": "parent-restore"}
                ),
                "/parent-restore-pbm-list": build_restore_task(
                    "parent-restore-pbm-list", data={"parent": "parent-restore"}
                ),
            }
        )

        response = test_client.get(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert (body["created_by"], body["last_updated_by"]) == (
            creator.username,
            updater.username,
        )

    def test_detail_anonymized_entities_falls_back_when_mask_none(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Recompute anonymized_entities after the detail dump round-trip when mask is null."""
        parent = build_restore_task("parent-restore", anonymize_mask=None)
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})

        with patch_fallback_default_entities():
            response = test_client.get(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["anonymize_mask"] is None
        assert body["anonymized_entities"] == EXPECTED_FALLBACK_DEFAULT_ENTITIES

    def test_detail_tolerates_history_fetch_failure_for_one_child(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 200 when one child history fetch fails."""
        parent = build_restore_task("parent-restore")
        restore_leg = build_restore_task(
            "parent-restore-pbm_logical",
            data={"parent": "parent-restore"},
        )
        pbm_list = build_restore_task(
            "parent-restore-pbm-list",
            data={"parent": "parent-restore"},
        )
        history_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        tasks_by_path = {
            "/parent-restore": parent,
            "/parent-restore-pbm_logical": restore_leg,
            "/parent-restore-pbm-list": pbm_list,
        }

        async def _mock_get(path: str, **kwargs: Any) -> Any:
            if path == "/parent-restore-pbm-list/history/":
                raise history_exc
            if path.endswith("/history/"):
                return {"items": []}
            if path in tasks_by_path:
                return tasks_by_path[path]
            raise AssertionError(f"Unexpected tasks_api.get path: {path!r}")

        mock_task_api_dep.get = AsyncMock(side_effect=_mock_get)

        response = test_client.get(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        child_names = {item["name"] for item in body["derived_tasks"]}
        assert child_names == {"parent-restore-pbm_logical"}


class TestRestoreMongoApiDelete:
    """Tests for DELETE /api/apps/backup_mongo/restore/{task_name}."""

    def test_delete_removes_parent_and_all_children(
        self, test_client, mock_task_api_dep
    ) -> None:
        """DELETE cascades to every child before the parent."""
        parent = build_restore_task("parent-restore")
        mock_task_api_dep.get = AsyncMock(return_value=parent)
        mock_task_api_dep.delete = AsyncMock(return_value=None)

        response = test_client.delete(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert mock_task_api_dep.delete.await_args_list == [
            call("/parent-restore-pbm_logical"),
            call("/parent-restore-pbm-list"),
            call("/parent-restore"),
        ]

    def test_delete_returns_500_when_cascade_delete_partially_fails(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 500 when a child DELETE fails with a non-404 error."""
        parent = build_restore_task("parent-restore")
        mock_task_api_dep.get = AsyncMock(return_value=parent)
        derived_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        async def _delete(path: str) -> None:
            if path == "/parent-restore-pbm-list":
                raise derived_exc

        mock_task_api_dep.delete = AsyncMock(side_effect=_delete)

        response = test_client.delete(f"{API_BASE}/parent-restore")

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = response.json()["detail"]
        assert detail["message"] == "Partial delete failure; orphaned tasks"
        assert "parent-restore-pbm-list" in detail["errors"]


class TestRestoreMongoApiUpdate:
    """Tests for PUT /api/apps/backup_mongo/restore/{task_name}."""

    def test_update_refuses_an_unknown_field(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Answer 422 naming a key the body model does not declare, changing nothing."""
        parent = build_restore_task("parent-restore")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})
        mock_task_api_dep.put = AsyncMock(return_value=parent)
        body = build_restore_write_body(
            task_name="parent-restore", service_id=mongo_service.id
        )
        body[UNKNOWN_FIELD] = "value"

        response = test_client.put(f"{API_BASE}/parent-restore", json=body)

        assert_unknown_field_rejected(response)
        mock_task_api_dep.put.assert_not_called()

    def test_update_puts_config_payload_to_parent_and_refreshes_children(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Keep the parent as a config task and update child legs in place."""
        parent = build_restore_task("parent-restore")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(
                task_name="wrong-name",
                service_id=mongo_service.id,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.put.await_count == EXPECTED_LOGICAL_RESTORE_PUTS
        parent_put = mock_task_api_dep.put.await_args_list[0]
        assert parent_put.args == ("/parent-restore",)
        parent_payload = parent_put.kwargs["json"]
        assert parent_payload["name"] == "parent-restore"
        assert "parent" not in parent_payload["data"]
        assert RESTORE_CONFIG_PAYLOAD_MARKER in parent_payload["data"]["payload"]

        restore_put = mock_task_api_dep.put.await_args_list[1]
        assert restore_put.args == ("/parent-restore-pbm_logical",)
        restore_payload = restore_put.kwargs["json"]
        assert restore_payload["name"] == "parent-restore-pbm_logical"
        assert restore_payload["data"]["parent"] == "parent-restore"

    def test_update_resolves_actor_usernames(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
        known_actors: tuple[CasdoorUser, CasdoorUser],
    ) -> None:
        """Render both actors' usernames on the update response."""
        creator, updater = known_actors
        parent = build_restore_task(
            "parent-restore",
            created_by=str(creator.id),
            last_updated_by=str(updater.id),
        )
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(
                task_name="parent-restore", service_id=mongo_service.id
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert (body["created_by"], body["last_updated_by"]) == (
            creator.username,
            updater.username,
        )

    def test_update_pins_backup_type_to_path_parent(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Use the parent config backup type for child task identity."""
        parent = build_restore_task("parent-restore")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(
                service_id=mongo_service.id,
                backup_type=BackupType.PBM_PHYSICAL.value,
            ),
        )

        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.put.await_count == EXPECTED_LOGICAL_RESTORE_PUTS
        restore_put = mock_task_api_dep.put.await_args_list[1]
        assert restore_put.args == ("/parent-restore-pbm_logical",)
        assert (
            mock_task_api_dep.put.await_args_list[2].args[0]
            == "/parent-restore-pbm-list"
        )

    def test_update_protected_task_returns_409(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Reject updates to protected restore parent tasks."""
        parent = build_restore_task("parent-restore", protected=True)
        mock_task_api_dep.get = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_restamps_form_on_config_task_across_repeated_edits(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Re-stamp ``_form`` on the parent config task on every PUT.

        Regression: without re-stamping, the stored form is dropped after the
        first edit and the Edit affordance greys out permanently.
        """
        parent = build_restore_task("parent-restore")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        for _ in range(2):
            mock_task_api_dep.get = mock_task_api_get_by_path(
                {"/parent-restore": parent}
            )
            response = test_client.put(
                f"{API_BASE}/parent-restore",
                json=build_restore_write_body(service_id=mongo_service.id),
            )

            assert response.status_code == status.HTTP_200_OK
            config_put = mock_task_api_dep.put.await_args_list[0]
            assert config_put.args == ("/parent-restore",)
            assert RESERVED_FORM_KEY in config_put.kwargs["json"]["data"]
            mock_task_api_dep.put.reset_mock()

    def test_update_running_conflict_returns_409(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Reject a restore update while a conflicting run is in flight.

        The conflict is checked against the resolved parent, not the raw path,
        so an in-flight group cannot be edited through any member URL.
        """
        parent = build_restore_task("parent-restore")
        mock_task_api_dep.get = _running_group_get_mock("parent-restore", parent)
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    def test_update_blocked_when_child_leg_running(
        self,
        test_client,
        mock_task_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Block an edit while a child leg is running though the parent is idle.

        Restores execute on the child legs, not the parent config task, so
        checking only the parent's history would let a ``PUT`` mutate a running
        group.
        """
        parent = build_restore_task("parent-restore")
        mock_task_api_dep.get = _running_leg_get_mock(
            "parent-restore", parent, "parent-restore-pbm-list"
        )
        mock_task_api_dep.put = AsyncMock(return_value=parent)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        mock_task_api_dep.put.assert_not_awaited()

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_update_returns_500_when_a_leg_put_fails(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Return 500 naming the failed leg when a child PUT fails mid-cascade."""
        parent = build_restore_task("parent-restore")
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.get = mock_task_api_get_by_path({"/parent-restore": parent})
        leg_exc = HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        async def _put(path: str, **kwargs: Any) -> Any:
            if path == "/parent-restore-pbm-list":
                raise leg_exc
            return parent

        mock_task_api_dep.put = AsyncMock(side_effect=_put)

        response = test_client.put(
            f"{API_BASE}/parent-restore",
            json=build_restore_write_body(service_id=mongo_service.id),
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "parent-restore-pbm-list" in response.json()["detail"]

    def test_update_returns_404_for_unknown_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return 404 when the PUT addresses an unknown task name."""
        mock_task_api_dep.get = AsyncMock(side_effect=HTTPNotFoundException())
        mock_task_api_dep.put = AsyncMock()

        response = test_client.put(
            f"{API_BASE}/ghost-restore",
            json=build_restore_write_body(service_id=1),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_task_api_dep.put.assert_not_awaited()


def build_restore_execute_response(
    task_id: int | None = 99, task_name: str = "mongo-restore-task"
) -> dict:
    """Build a minimal TaskHistoryResponse-shaped dict for execute endpoint tests.

    ``status`` and ``created_at`` are pinned to values ``TaskHistoryResponse``
    would not itself supply (``PENDING`` and ``utc_now()``), so asserting them
    distinguishes a field forwarded from the upstream row from a defaulted one.
    """
    return {
        "id": task_id,
        "execution_request": {"task": task_name, "target": "mongo-restore-host"},
        "task": {**build_restore_task(task_name), "deleted_at": None},
        "status": EXECUTE_STATUS.value,
        "created_at": EXECUTE_CREATED_AT,
    }


class TestRestoreMongoApiExecute:
    """Tests for POST /api/apps/backup_mongo/restore/{task_name}/execute."""

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_201_with_task_name_and_id(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing a restore task returns 201 with task_name and task_id."""
        expected_task_id = 7
        task = build_restore_task("mongo-restore-task")
        mock_task_api_dep.get = AsyncMock(return_value=task)
        mock_task_api_dep.post = AsyncMock(
            return_value=build_restore_execute_response(expected_task_id)
        )

        response = test_client.post(f"{API_BASE}/mongo-restore-task/execute", json={})

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["task_name"] == "mongo-restore-task"
        assert data["task_id"] == expected_task_id
        mock_task_api_dep.post.assert_awaited_once_with(
            "/execute/mongo-restore-task", json={}
        )

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_the_dispatched_run_state(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Return the dispatched run's status and creation time for a restore.

        This app derives an execute route but binds no contract mixin, so the
        run-state assertions ``DerivedRouterContractTests`` makes for the other
        derived execute routes have to be made here.
        """
        task = build_restore_task("mongo-restore-task")
        mock_task_api_dep.get = AsyncMock(return_value=task)
        mock_task_api_dep.post = AsyncMock(
            return_value=build_restore_execute_response()
        )

        response = test_client.post(f"{API_BASE}/mongo-restore-task/execute", json={})

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert set(data) == {"task_name", "task_id", "status", "created_at"}
        assert data["status"] == EXECUTE_STATUS.value
        assert data["created_at"] == EXECUTE_CREATED_AT

    @pytest.mark.usefixtures("_mock_check_for_conflicted_running_tasks")
    def test_execute_returns_404_for_unknown_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Executing an unknown restore task name returns 404."""
        mock_task_api_dep.get = AsyncMock(side_effect=HTTPNotFoundException())

        response = test_client.post(f"{API_BASE}/ghost-task/execute", json={})

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRestoreMongoApiAuth:
    """Tests for API authentication."""

    def test_unauthenticated_list_returns_401(self, unauthenticated_client) -> None:
        """Reject unauthenticated access to the restores API."""
        response = unauthenticated_client.get(f"{API_BASE}/")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
