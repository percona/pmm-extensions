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

"""Define tests for the inventory plugin JSON API routes under ``/api/apps/inventory/``.

The plugin is an operator API with no browsable entities, so these cover the
ad-hoc sync trigger and its status, plugin-task and syncer discovery, the
per-service connectivity probe, and that the retired entity and schema paths
stay unregistered.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status
from pydantic import SecretStr

from app.core.config import settings
from app.core.requests import RemoteAPI
from app.core.utils.date_time import utc_now
from app.core.utils.fields import UniqueList
from app.extensions.apps.inventory.deps import get_syncers
from app.extensions.config import extensions_settings, SyncOptions
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.deps import (
    BEARER_REQUIRED_DETAIL,
    get_created_service,
    get_current_user,
    get_inventory_client,
    get_session,
    get_tasks_api,
    get_tasks_client,
)
from app.extensions.main import extensions_app
from app.extensions.models import (
    SyncInstance,
    SyncInventoryEntityTypeEnum,
    SyncItem,
    SyncStatusEnum,
)
from app.inventory.models import ServiceTypeEnum
from tests.app.extensions.apps.inventory.conftest import no_syncers, PMM_STUB_NAME
from tests.app.factories import CreatedNodeFactory, CreatedServiceFactory

_MYSQL_PORT = 3306
_TASK_HISTORY_ID = 42


class TestInventoryResponseModelsInOpenAPI:
    """Ensure new endpoints expose typed Pydantic models in the OpenAPI schema."""

    def test_plugin_tasks_response_schema_is_defined(self, test_client):
        """Ensure the app-namespaced PluginTaskResponse is a named schema in the OpenAPI spec."""
        response = test_client.get("/openapi.json")
        assert response.status_code == status.HTTP_200_OK
        schemas = response.json().get("components", {}).get("schemas", {})
        assert "inventory__PluginTaskResponse" in schemas

    def test_available_syncers_response_schema_is_defined(self, test_client):
        """Ensure the app-namespaced AvailableSyncer is a named schema in the OpenAPI spec."""
        response = test_client.get("/openapi.json")
        assert response.status_code == status.HTTP_200_OK
        schemas = response.json().get("components", {}).get("schemas", {})
        assert "inventory__AvailableSyncer" in schemas

    def test_plugin_tasks_openapi_response_references_model(self, test_client):
        """Ensure GET /api/apps/inventory/ response body references PluginTaskResponse."""
        response = test_client.get("/openapi.json")
        spec = response.json()
        get_op = spec["paths"]["/api/apps/inventory/"]["get"]
        response_schema = get_op["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert response_schema.get("type") == "array"
        ref = response_schema.get("items", {}).get("$ref", "")
        assert "PluginTaskResponse" in ref

    def test_available_syncers_openapi_response_references_model(self, test_client):
        """Ensure GET /api/apps/inventory/available-syncers/ response references AvailableSyncer."""
        response = test_client.get("/openapi.json")
        spec = response.json()
        get_op = spec["paths"]["/api/apps/inventory/available-syncers/"]["get"]
        response_schema = get_op["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert response_schema.get("type") == "array"
        ref = response_schema.get("items", {}).get("$ref", "")
        assert "AvailableSyncer" in ref


class TestInventoryPluginTasksEndpoint:
    """Tests for GET /api/apps/inventory/ (plugin task discovery)."""

    def test_returns_200_with_inventory_sync_task(self, test_client):
        """Ensure endpoint returns 200 and includes the inventory-sync task."""
        response = test_client.get("/api/apps/inventory/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert isinstance(body, list)
        assert any(t["name"] == "inventory-sync" for t in body)

    def test_returns_the_inventory_collection_task(self, test_client):
        """Ensure the endpoint offers collection alongside sync.

        An operator schedules and toggles both from the same UI, so the hook
        needs to discover the pair rather than sync alone.
        """
        response = test_client.get("/api/apps/inventory/")
        assert response.status_code == status.HTTP_200_OK
        assert any(t["name"] == "inventory-collection" for t in response.json())

    def test_response_shape_matches_use_plugin_tasks_contract(self, test_client):
        """Ensure every item has at minimum a ``name`` key for the React hook."""
        response = test_client.get("/api/apps/inventory/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        for task in body:
            assert "name" in task


class TestInventoryAvailableSyncersEndpoint:
    """Tests for GET /api/apps/inventory/available-syncers/."""

    def test_returns_200_with_syncers_list(self, test_client, mock_syncers_dep):
        """Ensure endpoint returns 200 and a list."""
        response = test_client.get("/api/apps/inventory/available-syncers/")
        assert response.status_code == status.HTTP_200_OK
        assert isinstance(response.json(), list)

    def test_filters_to_can_sync_inventory_only(self, test_client, mock_syncers_dep):
        """Ensure only syncers where ``can_sync_inventory()`` is True are returned."""
        response = test_client.get("/api/apps/inventory/available-syncers/")
        body = response.json()
        assert len(body) == 1

    def test_response_items_have_name_and_display_name(
        self, test_client, mock_syncers_dep
    ):
        """Ensure each syncer item carries ``name`` and ``display_name``."""
        response = test_client.get("/api/apps/inventory/available-syncers/")
        for item in response.json():
            assert "name" in item
            assert "display_name" in item

    def test_display_name_and_name_are_strings(self, test_client, mock_syncers_dep):
        """Ensure ``name`` and ``display_name`` are plain strings, not callables or objects."""
        response = test_client.get("/api/apps/inventory/available-syncers/")
        for item in response.json():
            assert isinstance(item["name"], str)
            assert isinstance(item["display_name"], str)

    def test_no_matching_syncers_returns_empty_list(self, test_client):
        """When all syncers return ``can_sync_inventory=False``, endpoint returns ``[]``."""

        class _NoSync:
            def can_sync_inventory(self) -> bool:
                return False

        extensions_app.dependency_overrides[get_syncers] = lambda: [_NoSync()]
        try:
            response = test_client.get("/api/apps/inventory/available-syncers/")
            assert response.status_code == status.HTTP_200_OK
            assert response.json() == []
        finally:
            extensions_app.dependency_overrides.pop(get_syncers, None)

    def test_syncers_dep_error_surfaces_500(self, test_client):
        """If ``get_syncers`` raises (e.g. bad config), the endpoint returns 500."""

        def _broken():
            raise RuntimeError("broken syncer import")

        extensions_app.dependency_overrides[get_syncers] = _broken
        try:
            response = test_client.get("/api/apps/inventory/available-syncers/")
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        finally:
            extensions_app.dependency_overrides.pop(get_syncers, None)


class TestInventoryNewRoutesAuthentication:
    """Ensure new plugin discovery routes enforce API authentication."""

    def test_plugin_tasks_rejects_unauthenticated(self, unauthenticated_client):
        """``GET /api/apps/inventory/`` must return 401 without a valid token."""
        response = unauthenticated_client.get("/api/apps/inventory/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_available_syncers_rejects_unauthenticated(self, unauthenticated_client):
        """``GET /api/apps/inventory/available-syncers/`` must return 401 without a valid token."""
        response = unauthenticated_client.get("/api/apps/inventory/available-syncers/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestInventoryBearerGate:
    """Cover Bearer-gate behavior on inventory JSON mutations."""

    def test_cookie_only_sync_post_returns_401(
        self, api_admin_client_no_bearer, mock_run_sync_funcs
    ):
        """Cookie-auth POST without Bearer header is 401'd by the framework gate."""
        response = api_admin_client_no_bearer.post("/api/apps/inventory/sync/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["detail"] == BEARER_REQUIRED_DETAIL
        mock_run_sync_funcs["inventory"].assert_not_called()


class TestInventoryEntitySurfaceRemoved:
    """Cover the removal of the inventory plugin's entity and schema routes."""

    @pytest.mark.parametrize(
        ("method", "path", "json_body"),
        [
            ("GET", "/api/apps/inventory/nodes/", None),
            ("POST", "/api/apps/inventory/services/", {"name": "db"}),
            ("GET", "/api/apps/inventory/nodes/3", None),
            ("PUT", "/api/apps/inventory/nodes/3", {"name": "x"}),
            ("DELETE", "/api/apps/inventory/nodes/3", None),
            ("POST", "/api/apps/inventory/unknown/", {"name": "db"}),
            ("GET", "/api/apps/inventory/nodes/3/system-observation", None),
            ("GET", "/api/apps/inventory/services/9/system-observation", None),
            ("GET", "/api/apps/inventory/schema", None),
        ],
    )
    def test_inventory_entity_routes_are_gone(
        self,
        test_client,
        mock_inventory_api_dep,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
    ) -> None:
        """Assert every retired path 404s and never reaches the inventory API.

        404 rather than 405: nothing is registered under these paths any more,
        so Starlette fails to resolve them before it ever compares methods. The
        whole entity surface is covered here — list, detail, both
        system-observation sub-resources and the app schema — which is what
        leaves ``/api/apps/inventory/`` an operator API with no browsable
        entities.

        The client is authenticated on purpose: that the route is gone for an
        authorized caller is strictly stronger than rejecting an unauthorized
        one. The bearer gate is a router-level dependency, so an unregistered
        path never reaches it and cannot answer 401 instead.
        """
        kwargs = {"json": json_body} if json_body is not None else {}
        response = test_client.request(method, path, **kwargs)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        mock_inventory_api_dep.get.assert_not_called()
        mock_inventory_api_dep.post.assert_not_called()
        mock_inventory_api_dep.put.assert_not_called()
        mock_inventory_api_dep.delete.assert_not_called()


class TestInventorySyncTrigger:
    """Tests for POST ``/api/apps/inventory/sync/``.

    Mirrors the Jinja2 contract at ``app/extensions/apps/inventory/routes.py``
    ``POST /sync/`` but delivers a JSON-API surface: a 202 on accepted
    triggers and a 400 on unknown/inapplicable syncer names. The
    ``run_inventory_sync`` background task is never actually executed; it
    is patched to an ``AsyncMock`` so tests can assert what arguments
    ``BackgroundTasks.add_task`` would have scheduled.
    """

    def test_no_body_schedules_sync_all(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """POST with no body returns 202 and schedules every configured syncer."""
        response = test_client.post("/api/apps/inventory/sync/")
        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.content == b""
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == mock_syncers

    def test_sync_uses_internal_token(
        self, test_client, mock_syncers, mock_run_sync_funcs, mocker
    ):
        """The API sync background task is scheduled with the internal token."""
        mocker.patch.object(
            settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr("api-internal-token")
        )
        response = test_client.post("/api/apps/inventory/sync/")
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert args[0] == "api-internal-token"

    def test_empty_object_body_schedules_sync_all(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """POST ``{}`` returns 202 and schedules every configured syncer."""
        response = test_client.post("/api/apps/inventory/sync/", json={})
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == mock_syncers

    def test_null_syncer_schedules_sync_all(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """POST ``{"syncer": null}`` returns 202 and schedules every configured syncer."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": None},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == mock_syncers

    def test_empty_syncer_string_schedules_sync_all(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """POST ``{"syncer": ""}`` matches the Jinja2 contract: sync-all path."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": ""},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == mock_syncers

    def test_named_syncer_schedules_only_that_syncer(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """POST ``{"syncer": "<name>"}`` schedules only the matching syncer."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": PMM_STUB_NAME},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == [mock_syncers[0]]

    def test_unknown_syncer_returns_400_and_does_not_schedule(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """A syncer name that matches no configured syncer yields HTTP 400."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": "not.a.real.Syncer"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_incapable_syncer_returns_400(
        self, test_client, mock_syncers, mock_run_sync_funcs, mocker
    ):
        """A syncer matching by name but failing the capability check yields 400."""
        mocker.patch.object(
            type(mock_syncers[0]), "can_sync_inventory", return_value=False
        )
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": PMM_STUB_NAME},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_no_syncers_configured_with_named_returns_400(
        self, test_client, mock_run_sync_funcs
    ):
        """With zero configured syncers, a named target still 400s."""
        extensions_app.dependency_overrides[get_syncers] = no_syncers
        try:
            response = test_client.post(
                "/api/apps/inventory/sync/",
                json={"syncer": PMM_STUB_NAME},
            )
        finally:
            extensions_app.dependency_overrides.pop(get_syncers, None)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_no_syncers_configured_sync_all_still_202(
        self, test_client, mock_run_sync_funcs
    ):
        """With zero configured syncers, sync-all returns 202 (matches Jinja2)."""
        extensions_app.dependency_overrides[get_syncers] = no_syncers
        try:
            response = test_client.post("/api/apps/inventory/sync/")
        finally:
            extensions_app.dependency_overrides.pop(get_syncers, None)
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_run_sync_funcs["inventory"].assert_awaited_once()
        args = mock_run_sync_funcs["inventory"].await_args.args
        assert list(args[1:]) == []

    def test_wrong_type_for_syncer_returns_422(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """A non-string ``syncer`` value is rejected by Pydantic with 422."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": 123},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_extra_fields_rejected_with_422(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """Unknown body fields are rejected so client typos cannot silently sync-all."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            json={"syncer": None, "extra": "x"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_malformed_json_returns_422(
        self, test_client, mock_syncers, mock_run_sync_funcs
    ):
        """A malformed JSON body returns 422 (FastAPI default) and does not schedule."""
        response = test_client.post(
            "/api/apps/inventory/sync/",
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_requires_authentication(self, unauthenticated_client, mock_run_sync_funcs):
        """Without API auth the gateway returns 401."""
        response = unauthenticated_client.post("/api/apps/inventory/sync/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        mock_run_sync_funcs["inventory"].assert_not_awaited()

    def test_unusable_syncer_settings_return_503(
        self, test_client, mock_run_sync_funcs, mocker
    ):
        """Report a misconfigured syncer as unavailable, and schedule nothing.

        Settings load rejects such a value, so this covers the residual path: a bare
        500 repeated on every trigger tells an operator nothing about the cause.
        """
        option = SyncOptions(
            syncer="app.extensions.sync.syncers.pmm.PMMSyncer", stale_run_after="60"
        )
        mocker.patch.object(extensions_settings, "SYNCERS", UniqueList([option]))
        extensions_app.dependency_overrides[get_inventory_client] = lambda: AsyncMock(
            spec=RemoteAPI
        )
        extensions_app.dependency_overrides[get_tasks_client] = lambda: AsyncMock(
            spec=RemoteAPI
        )
        try:
            response = test_client.post("/api/apps/inventory/sync/")
        finally:
            extensions_app.dependency_overrides.pop(get_inventory_client, None)
            extensions_app.dependency_overrides.pop(get_tasks_client, None)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "STALE_RUN_AFTER" in response.json()["detail"]
        mock_run_sync_funcs["inventory"].assert_not_awaited()


class TestInventorySyncStatus:
    """Tests for GET ``/api/apps/inventory/sync/status/``."""

    def test_returns_false_when_idle(self, test_client, mocker):
        """Returns ``{"is_running": false}`` when ``SyncItemManager`` reports idle."""
        spy = mocker.patch.object(
            SyncItemManager, "sync_is_running", new=AsyncMock(return_value=False)
        )
        response = test_client.get("/api/apps/inventory/sync/status/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_running"] is False
        # Regression guard: this endpoint reports INVENTORY status, not
        # NODE/SERVICE/SCHEMA — copy-paste bugs would change the enum.
        assert spy.await_args.args[1] == SyncInventoryEntityTypeEnum.INVENTORY

    def test_returns_no_runs_when_none_recorded(self, test_client, mocker):
        """``last_runs`` is an empty list before any sync has ever run."""
        mocker.patch.object(
            SyncItemManager, "sync_is_running", new=AsyncMock(return_value=False)
        )
        response = test_client.get("/api/apps/inventory/sync/status/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["last_runs"] == []

    @pytest.mark.asyncio
    async def test_returns_last_runs(self, async_test_client, session, mocker):
        """Surface recorded runs with their status and completeness verdict."""
        mocker.patch.object(
            SyncItemManager, "sync_is_running", new=AsyncMock(return_value=False)
        )
        extensions_app.dependency_overrides[get_session] = lambda: session
        run = await SyncInstanceManager.save(
            session,
            SyncInstance(
                syncer=PMM_STUB_NAME,
                status=SyncStatusEnum.SUCCESS,
                snapshot_complete=True,
            ),
        )
        await SyncItemManager.save(
            session,
            SyncItem(
                entity_id=None,
                entity_type=SyncInventoryEntityTypeEnum.INVENTORY,
                sync_instance_id=run.id,
                status=SyncStatusEnum.SUCCESS,
            ),
        )

        response = await async_test_client.get("/api/apps/inventory/sync/status/")

        assert response.status_code == status.HTTP_200_OK
        assert [
            (run["syncer"], run["status"], run["snapshot_complete"])
            for run in response.json()["last_runs"]
        ] == [(PMM_STUB_NAME, SyncStatusEnum.SUCCESS.value, True)]

    @pytest.mark.asyncio
    async def test_last_runs_only_contains_latest_inventory_runs(
        self, async_test_client, session, mocker
    ):
        """Exclude entity-only runs and retain the ten newest inventory-wide runs."""
        mocker.patch.object(
            SyncItemManager, "sync_is_running", new=AsyncMock(return_value=False)
        )
        extensions_app.dependency_overrides[get_session] = lambda: session
        start = utc_now()
        entity_types = (
            SyncInventoryEntityTypeEnum.NODE,
            SyncInventoryEntityTypeEnum.SERVICE,
            SyncInventoryEntityTypeEnum.SCHEMA,
            SyncInventoryEntityTypeEnum.TABLE,
        )

        for index in range(12):
            run = await SyncInstanceManager.save(
                session,
                SyncInstance(
                    syncer=f"inventory-{index:02}",
                    status=SyncStatusEnum.SUCCESS,
                    snapshot_complete=True,
                    created_at=start + timedelta(seconds=index),
                ),
            )
            await SyncItemManager.save(
                session,
                SyncItem(
                    entity_id=None,
                    entity_type=SyncInventoryEntityTypeEnum.INVENTORY,
                    sync_instance_id=run.id,
                    status=SyncStatusEnum.SUCCESS,
                ),
            )

        for index in range(12):
            run = await SyncInstanceManager.save(
                session,
                SyncInstance(
                    syncer=f"entity-only-{index:02}",
                    status=SyncStatusEnum.SUCCESS,
                    created_at=start + timedelta(seconds=12 + index),
                ),
            )
            await SyncItemManager.save(
                session,
                SyncItem(
                    entity_id=index + 1,
                    entity_type=entity_types[index % len(entity_types)],
                    sync_instance_id=run.id,
                    status=SyncStatusEnum.SUCCESS,
                ),
            )

        response = await async_test_client.get("/api/apps/inventory/sync/status/")

        assert response.status_code == status.HTTP_200_OK
        assert [run["syncer"] for run in response.json()["last_runs"]] == [
            f"inventory-{index:02}" for index in range(11, 1, -1)
        ]

    def test_returns_true_when_running(self, test_client, mocker):
        """Returns ``{"is_running": true}`` when ``SyncItemManager`` reports active."""
        mocker.patch.object(
            SyncItemManager, "sync_is_running", new=AsyncMock(return_value=True)
        )
        response = test_client.get("/api/apps/inventory/sync/status/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["is_running"] is True

    def test_requires_authentication(self, unauthenticated_client):
        """Without API auth the status endpoint returns 401."""
        response = unauthenticated_client.get("/api/apps/inventory/sync/status/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestInventorySyncStatusOpenAPI:
    """Cover the response-model shape of GET /api/apps/inventory/sync/status/."""

    def test_openapi_schema_is_named_model_not_inline_dict(self):
        """The 200 response schema must reference a named model, not an inline dict.

        ``dict[str, bool]`` generates a free-form ``additionalProperties``
        schema; ``InventorySyncStatusResponse`` generates a ``$ref`` to a named
        component. The generated ``@pmm-extensions/api`` types depend on a stable schema
        name, so this assertion guards against regression to the untyped form.
        """
        prior_schema = extensions_app.openapi_schema
        extensions_app.openapi_schema = None
        try:
            openapi = extensions_app.openapi()
            response_schema = openapi["paths"]["/api/apps/inventory/sync/status/"][
                "get"
            ]["responses"]["200"]["content"]["application/json"]["schema"]
            assert "$ref" in response_schema, (
                "Expected a $ref to InventorySyncStatusResponse; got inline schema. "
                "Change the return type annotation back to InventorySyncStatusResponse."
            )
        finally:
            extensions_app.openapi_schema = prior_schema


class TestInventoryServiceCheckConnectivity:
    """Cover POST /api/apps/inventory/services/{service_id}/check-connectivity/."""

    @pytest.fixture(autouse=True)
    def _admin_identity(self, test_client, admin_user):
        """Resolve the API identity to an admin for the probe's own ``IsApiAdmin``.

        Declared after ``test_client`` so it replaces the non-admin identity that
        fixture installs; :meth:`test_non_admin_is_refused` opts out by
        re-installing the non-admin.
        """
        extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
        yield
        extensions_app.dependency_overrides.pop(get_current_user, None)

    @pytest.fixture
    def created_node(self):
        """Return a fake inventory node."""
        return CreatedNodeFactory.build()

    @pytest.fixture
    def mysql_service(self, created_node):
        """Return a MySQL service attached to ``created_node`` with a port set."""
        service = CreatedServiceFactory.build(
            type=ServiceTypeEnum.MYSQL, port=_MYSQL_PORT
        )
        service.node = created_node
        return service

    @pytest.fixture
    def _mock_mysql_service_dep(self, mysql_service):
        """Resolve ``CreatedServiceDep`` to the MySQL service."""
        extensions_app.dependency_overrides[get_created_service] = lambda: mysql_service
        yield
        extensions_app.dependency_overrides.pop(get_created_service, None)

    @pytest.fixture
    def mock_tasks_api_dep(self, created_node) -> Iterator[AsyncMock]:
        """Mock ``TaskAPI`` with a host mapping that resolves ``created_node``."""
        mock = AsyncMock(spec=RemoteAPI)
        mock.get.return_value = {created_node.name: created_node.address}
        extensions_app.dependency_overrides[get_tasks_api] = lambda: mock
        yield mock
        extensions_app.dependency_overrides.pop(get_tasks_api, None)

    def _url(self, service) -> str:
        return f"/api/apps/inventory/services/{service.id}/check-connectivity/"

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_non_admin_is_refused(
        self, test_client, mysql_service, mock_tasks_api_dep, regular_user
    ):
        """Refuse a non-admin: the probe opens a connection with stored credentials.

        Its already-admin sibling is ``POST /api/extensions/admin/connectivity-check/``,
        and the declaration is on the route rather than left to the router-level
        gate so the posture is readable where the route is defined.
        """
        extensions_app.dependency_overrides[get_current_user] = lambda: regular_user

        response = test_client.post(self._url(mysql_service))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        mock_tasks_api_dep.post.assert_not_awaited()

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_successful_probe_returns_200(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Return HTTP 200 carrying the upstream result body when the probe passes."""
        mock_tasks_api_dep.post.return_value = {
            "success": True,
            "error": None,
            "task_history_id": _TASK_HISTORY_ID,
        }
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "success": True,
            "error": None,
            "task_history_id": _TASK_HISTORY_ID,
        }
        mock_tasks_api_dep.post.assert_awaited_once_with(
            "/connectivity-check/",
            json={
                "target": mysql_service.node.name,
                "host": mysql_service.node.address,
                "port": mysql_service.port,
                "service_type": ServiceTypeEnum.MYSQL.value,
            },
        )

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_failed_probe_returns_200_with_error_body(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Return HTTP 200 with ``success=false`` when the check fails but the call succeeds."""
        mock_tasks_api_dep.post.return_value = {
            "success": False,
            "error": "Connection refused",
            "task_history_id": _TASK_HISTORY_ID,
        }
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["success"] is False
        assert body["error"] == "Connection refused"

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_failed_probe_without_error_returns_null_error(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Keep ``error`` null on an errorless failure so the client owns the fallback text."""
        mock_tasks_api_dep.post.return_value = {
            "success": False,
            "error": None,
            "task_history_id": _TASK_HISTORY_ID,
        }
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["success"] is False
        assert body["error"] is None

    @pytest.mark.usefixtures("mock_tasks_api_dep")
    def test_unsupported_service_type_returns_400(
        self, test_client, created_node, mock_tasks_api_dep
    ):
        """Reject a non-connectable service type server-side with HTTP 400."""
        service = CreatedServiceFactory.build(
            type=ServiceTypeEnum.PROXYSQL, port=_MYSQL_PORT
        )
        service.node = created_node
        extensions_app.dependency_overrides[get_created_service] = lambda: service
        try:
            response = test_client.post(self._url(service))
        finally:
            extensions_app.dependency_overrides.pop(get_created_service, None)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert ServiceTypeEnum.PROXYSQL.name in response.json()["detail"]
        mock_tasks_api_dep.post.assert_not_awaited()

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_missing_port_returns_400(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Reject a service with no port, which cannot be probed, with HTTP 400."""
        mysql_service.port = None
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        mock_tasks_api_dep.post.assert_not_awaited()

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_unregistered_node_address_returns_400_naming_the_address(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Return HTTP 400 naming the node address when no executor is registered for it."""
        mock_tasks_api_dep.get.return_value = {"some-other-nomad": "10.0.0.99"}
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert mysql_service.node.address in response.json()["detail"]
        mock_tasks_api_dep.post.assert_not_awaited()

    @pytest.mark.usefixtures("mock_tasks_api_dep")
    def test_unknown_service_propagates_404(self, test_client, mock_inventory_api_dep):
        """Propagate the inventory 404 from ``CreatedServiceDep`` for an unknown service id."""
        mock_inventory_api_dep.get.side_effect = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Service not found"
        )
        response = test_client.post(
            "/api/apps/inventory/services/999999/check-connectivity/"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_unreachable_tasks_api_returns_502_without_leaking_internals(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Surface a transport failure as HTTP 502 with no internal error text."""
        mock_tasks_api_dep.post.side_effect = ConnectionError("boom at socket layer")
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert "boom at socket layer" not in response.text

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_malformed_upstream_payload_returns_502(
        self, test_client, mysql_service, mock_tasks_api_dep
    ):
        """Return HTTP 502 for an unparseable upstream body, not a 500 validation escape."""
        mock_tasks_api_dep.post.return_value = {"unexpected": "shape"}
        response = test_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_502_BAD_GATEWAY

    @pytest.mark.usefixtures("_mock_mysql_service_dep")
    def test_requires_authentication(self, unauthenticated_client, mysql_service):
        """Return 401 when the request carries no API auth."""
        response = unauthenticated_client.post(self._url(mysql_service))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_openapi_response_references_connectivity_model(self, test_client):
        """Reference the upstream connectivity model from the 200 response schema."""
        spec = test_client.get("/openapi.json").json()
        post_op = spec["paths"][
            "/api/apps/inventory/services/{service_id}/check-connectivity/"
        ]["post"]
        schema = post_op["responses"]["200"]["content"]["application/json"]["schema"]
        assert "ConnectivityCheckResponse" in schema["$ref"]
