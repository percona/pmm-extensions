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

"""Define tests for inventory service routes."""

from datetime import datetime, UTC

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette import status
from starlette.testclient import TestClient

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.inventory.crud import SchemaManager
from app.inventory.models import (
    Node,
    Schema,
    Service,
    ServiceSystemObservation,
    SyncOutcomeEnum,
    Table,
)
from tests.app.factories import (
    NodeWriteFactory,
    SchemaWriteFactory,
    ServiceSystemObservationWriteFactory,
    ServiceWriteFactory,
)
from tests.app.inventory.conftest import (
    INVALID_SYNC_HEALTH_BODIES,
    INVALID_SYNC_HEALTH_BODY_IDS,
    sync_health_payload,
    SYNC_HEALTH_RESPONSE_KEYS,
)

OFFSET_BEYOND_TOTAL = 999
LIST_QUERY_MATCH_TOTAL = 2

# Pinned verbatim rather than imported from app.inventory.constants: the wording is
# part of the API contract, so an edit to the constant must fail the test.
UNCOLLECTED_SERVICE_DETAIL = "System observation not collected yet for this service"


class TestListServices:
    """Test GET /services/ endpoint."""

    def test_list_services_empty(self, test_client: TestClient) -> None:
        """Return an empty paginated response when no services exist."""
        response = test_client.get("/services/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    def test_list_services_excludes_retired(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Omit a retired service from the default list."""
        response = test_client.get("/services/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_services_rejects_unknown_sort_key(
        self, test_client: TestClient
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get("/services/", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_services_multiple(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return all services with schemas and node loaded."""
        response = test_client.get("/services/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["id"] == service.id
        assert "schemas" in data["items"][0]
        assert "node" in data["items"][0]

    def test_list_services_filter_by_service_type(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return only services matching the requested service type."""
        response = test_client.get(
            "/services/",
            params={"service_type": service.type},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) >= 1
        assert all(s["type"] == service.type for s in data["items"])

    def test_list_services_custom_offset(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get("/services/", params={"offset": OFFSET_BEYOND_TOTAL})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_services_custom_limit(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get("/services/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_services_filter_with_pagination(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return filtered total with pagination params."""
        response = test_client.get(
            "/services/",
            params={"service_type": service.type, "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] >= 1
        assert len(data["items"]) <= 1

    def test_list_services_search_ilike(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return only services whose name matches the search case-insensitively."""
        match = ServiceWriteFactory.build(name="AlphaSearchService", port=4401)
        other = ServiceWriteFactory.build(name="OtherService", port=4402)
        for payload in (match, other):
            create_response = test_client.post(
                f"/nodes/{node.id}/services/", json=payload.model_dump(mode="json")
            )
            assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get("/services/", params={"search": "alphasearch"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == match.name

    def test_list_services_search_reports_filtered_total(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for index, suffix in enumerate(("a", "b")):
            payload = ServiceWriteFactory.build(
                name=f"FilterMatchService_{suffix}",
                port=4500 + index,
            )
            create_response = test_client.post(
                f"/nodes/{node.id}/services/", json=payload.model_dump(mode="json")
            )
            assert create_response.status_code == status.HTTP_201_CREATED
        other = ServiceWriteFactory.build(name="UnrelatedService", port=4599)
        create_response = test_client.post(
            f"/nodes/{node.id}/services/", json=other.model_dump(mode="json")
        )
        assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get(
            "/services/", params={"search": "filtermatchservice", "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    def test_list_services_deterministic_ordering_across_pages(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Sort equal names stably across pages via the id tie-breaker."""
        shared_name = "SameSortService"
        created_ids: list[int] = []
        for index in range(LIST_QUERY_MATCH_TOTAL):
            payload = ServiceWriteFactory.build(name=shared_name, port=4600 + index)
            create_response = test_client.post(
                f"/nodes/{node.id}/services/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
            created_ids.append(create_response.json()["id"])
        created_ids.sort()

        first_page = test_client.get(
            "/services/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            "/services/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]


class TestRetrieveService:
    """Test GET /services/{service_id} endpoint."""

    def test_retrieve_service(self, test_client: TestClient, service: Service) -> None:
        """Return the service with schemas and node."""
        response = test_client.get(f"/services/{service.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == service.id
        assert "schemas" in data
        assert "node" in data

    def test_retrieve_service_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent service ID."""
        response = test_client.get("/services/9999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_service_retired_returns_404(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Hide a retired service from the default read."""
        response = test_client.get(f"/services/{retired_service.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_service_hides_retired_schema_nested_in_active_service(
        self, test_client: TestClient, retired_schema: Schema
    ) -> None:
        """Drop a retired schema from the schemas nested in an active service.

        The sync engine's ``prepare_sync`` recursion reads its schemas from this
        route, so what it nests decides which entities get a ``SyncItem``.
        """
        response = test_client.get(f"/services/{retired_schema.service_id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["schemas"] == []


class TestUpdateService:
    """Test PUT /services/{service_id} endpoint."""

    def test_update_service(
        self, test_client: TestClient, service: Service, node: Node
    ) -> None:
        """Update a service name and return the updated service."""
        payload = ServiceWriteFactory.build(node_id=node.id)
        payload.name = "updated-service-name"
        response = test_client.put(
            f"/services/{service.id}",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "updated-service-name"

    def test_update_service_not_found(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 404 when updating a nonexistent service."""
        payload = ServiceWriteFactory.build(node_id=node.id)
        response = test_client.put(
            "/services/9999",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_service_missing_external_id(
        self, test_client: TestClient, service: Service, node: Node
    ) -> None:
        """Return 422 when the update body omits external_id."""
        data = ServiceWriteFactory.build(node_id=node.id).model_dump(mode="json")
        del data["external_id"]
        response = test_client.put(f"/services/{service.id}", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_update_service_null_external_id_leaves_row_intact(
        self, test_client: TestClient, service: Service, node: Node
    ) -> None:
        """Reject an explicit-null external_id without clearing the stored origin."""
        data = ServiceWriteFactory.build(node_id=node.id).model_dump(mode="json")
        data["external_id"] = None
        response = test_client.put(f"/services/{service.id}", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        stored = test_client.get(f"/services/{service.id}")
        assert stored.status_code == status.HTTP_200_OK
        assert stored.json()["external_id"] == service.external_id

    def test_update_service_invalid_node_id(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 400 when updating with a nonexistent node_id."""
        payload = ServiceWriteFactory.build(node_id=9999)
        response = test_client.put(
            f"/services/{service.id}",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid node_id: 9999"

    def test_update_service_omitting_node_id_preserves_parent(
        self, test_client: TestClient, service: Service, node: Node
    ) -> None:
        """Apply a partial update that omits node_id, leaving the parent unchanged.

        A second node must exist: the omitted FK previously resolved to ``None``,
        and the parent pre-check drops ``None`` filters and matched every node, so
        with more than one node it raised ``MultipleResultsFound`` (HTTP 500).
        """
        second = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert second.status_code == status.HTTP_201_CREATED
        body = ServiceWriteFactory.build().model_dump(mode="json", exclude={"node_id"})
        body["name"] = "renamed-service"
        assert "node_id" not in body
        response = test_client.put(f"/services/{service.id}", json=body)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed-service"
        assert data["node_id"] == node.id

    def test_update_setting_external_id_releases_the_port(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Admit a move onto a held port when the mover gains an identity."""
        incumbent = test_client.post(
            f"/nodes/{service.node_id}/services/",
            json=ServiceWriteFactory.build().model_dump(
                mode="json", exclude={"node_id"}
            ),
        )
        assert incumbent.status_code == status.HTTP_201_CREATED

        response = test_client.put(
            f"/services/{service.id}",
            json=ServiceWriteFactory.build(
                node_id=service.node_id,
                port=incumbent.json()["port"],
                external_id="svc-identified",
            ).model_dump(mode="json"),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["port"] == incumbent.json()["port"]

    def test_update_service_change_node_id_to_valid_parent(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Reparent a service to another existing node and return 200."""
        new_node = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert new_node.status_code == status.HTTP_201_CREATED
        new_node_id = new_node.json()["id"]
        payload = ServiceWriteFactory.build(node_id=new_node_id)
        response = test_client.put(
            f"/services/{service.id}",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["node_id"] == new_node_id

    def test_update_service_explicit_null_node_id_rejected(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Reject an explicit null node_id (the FK column is non-nullable)."""
        body = ServiceWriteFactory.build().model_dump(mode="json")
        body["node_id"] = None
        response = test_client.put(f"/services/{service.id}", json=body)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid node_id: None"

    def test_update_service_omitting_node_id_preserves_association(
        self, test_client: TestClient, service: Service, node: Node
    ) -> None:
        """Partial update without node_id succeeds and leaves the FK unchanged.

        A second node exists so the omitted-FK path cannot accidentally resolve a
        single arbitrary parent: the old override skipped the ``id=None`` filter and
        crashed with ``MultipleResultsFound`` once more than one parent was present.
        """
        test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        payload = ServiceWriteFactory.build()
        body = payload.model_dump(mode="json", exclude={"node_id"})
        body["name"] = "renamed-without-node-id"
        response = test_client.put(f"/services/{service.id}", json=body)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed-without-node-id"
        assert data["node_id"] == node.id

    def test_update_service_explicit_null_node_id(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 400 when node_id is explicitly null on a non-nullable relationship."""
        payload = ServiceWriteFactory.build()
        body = payload.model_dump(mode="json")
        body["node_id"] = None
        response = test_client.put(f"/services/{service.id}", json=body)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid node_id: None"


class TestDeleteService:
    """Test DELETE /services/{service_id} endpoint."""

    def test_delete_service(self, test_client: TestClient, service: Service) -> None:
        """Retire a service and confirm the default read no longer resolves it."""
        response = test_client.delete(f"/services/{service.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        response = test_client.get(f"/services/{service.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_service_not_found(self, test_client: TestClient) -> None:
        """Return 404 when deleting a nonexistent service."""
        response = test_client.delete("/services/9999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_service_retires_schemas_and_tables(
        self,
        test_client: TestClient,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Retire the service's schemas and tables along with it."""
        response = test_client.delete(f"/services/{service.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        assert test_client.get(f"/schemas/{schema.id}").status_code == (
            status.HTTP_404_NOT_FOUND
        )
        assert test_client.get(f"/tables/{table.id}").status_code == (
            status.HTTP_404_NOT_FOUND
        )
        retired_table = test_client.get(
            f"/tables/{table.id}", params={"include_retired": True}
        )
        assert retired_table.status_code == status.HTTP_200_OK
        assert retired_table.json()["retired_at"] is not None

    def test_delete_service_leaves_the_node_active(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Retire downward only: the service's node keeps serving."""
        assert (
            test_client.delete(f"/services/{service.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        assert test_client.get(f"/nodes/{service.node_id}").status_code == (
            status.HTTP_200_OK
        )


class TestReviveService:
    """Test the POST /services/{service_id}/revive endpoint."""

    def test_revive_service_revives_node(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Revive the retired ancestor chain along with the service."""
        assert (
            test_client.delete(f"/nodes/{service.node_id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        response = test_client.post(f"/services/{service.id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert test_client.get(f"/services/{service.id}").status_code == (
            status.HTTP_200_OK
        )
        assert test_client.get(f"/nodes/{service.node_id}").status_code == (
            status.HTTP_200_OK
        )

    def test_revive_service_onto_a_port_a_replacement_now_holds(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Admit a revive sharing a port with an active replacement.

        Port is no longer a uniqueness key, so a replacement taking over the
        retired service's port reserves nothing the revive could collide with.
        """
        assert (
            test_client.delete(f"/services/{service.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        replacement = ServiceWriteFactory.build(port=service.port)
        assert (
            test_client.post(
                f"/nodes/{service.node_id}/services/",
                json=replacement.model_dump(mode="json", exclude={"node_id"}),
            ).status_code
            == status.HTTP_201_CREATED
        )

        response = test_client.post(f"/services/{service.id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_revive_identified_service_onto_a_live_port(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Admit a revive whose port an identified replacement holds."""
        assert (
            test_client.put(
                f"/services/{service.id}",
                json=ServiceWriteFactory.build(
                    node_id=service.node_id, external_id="svc-retired"
                ).model_dump(mode="json"),
            ).status_code
            == status.HTTP_200_OK
        )
        assert (
            test_client.delete(f"/services/{service.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        replacement = test_client.post(
            f"/nodes/{service.node_id}/services/",
            json=ServiceWriteFactory.build(port=service.port).model_dump(
                mode="json", exclude={"node_id"}
            ),
        )
        assert replacement.status_code == status.HTTP_201_CREATED
        assert (
            test_client.put(
                f"/services/{replacement.json()['id']}",
                json=ServiceWriteFactory.build(
                    node_id=service.node_id,
                    port=service.port,
                    external_id="svc-replacement",
                ).model_dump(mode="json"),
            ).status_code
            == status.HTTP_200_OK
        )

        response = test_client.post(f"/services/{service.id}/revive")

        assert response.status_code == status.HTTP_204_NO_CONTENT


class TestListSchemasByService:
    """Test GET /services/{service_id}/schemas/ endpoint."""

    def test_list_schemas_by_service_empty(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return an empty paginated response when the service has no schemas."""
        response = test_client.get(f"/services/{service.id}/schemas/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_schemas_by_service_excludes_retired(
        self,
        test_client: TestClient,
        retired_schema: Schema,
        params: dict[str, bool],
    ) -> None:
        """Omit a retired schema from an active service's schemas."""
        response = test_client.get(
            f"/services/{retired_schema.service_id}/schemas/", params=params
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_schemas_by_service_include_retired_resolves_retired_service(
        self, test_client: TestClient, schema: Schema, retired_service: Service
    ) -> None:
        """List a retired service's schemas through the opt-in."""
        response = test_client.get(
            f"/services/{retired_service.id}/schemas/",
            params={"include_retired": True},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [item["id"] for item in data["items"]] == [schema.id]
        assert data["total"] == 1

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_schemas_by_service_hides_retired_service_by_default(
        self,
        test_client: TestClient,
        schema: Schema,
        retired_service: Service,
        params: dict[str, bool],
    ) -> None:
        """Return 404 for a retired service unless the opt-in is set."""
        response = test_client.get(
            f"/services/{retired_service.id}/schemas/", params=params
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_schemas_by_service_include_retired_after_retire_route(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """List the schemas the retire route cascaded into, marked retired."""
        assert (
            test_client.delete(f"/services/{service.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        hidden = test_client.get(f"/services/{service.id}/schemas/")
        assert hidden.status_code == status.HTTP_404_NOT_FOUND

        response = test_client.get(
            f"/services/{service.id}/schemas/", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["id"] for item in items] == [schema.id]
        assert items[0]["retired_at"] is not None

    def test_list_schemas_by_service_include_retired_nests_tables(
        self,
        test_client: TestClient,
        table: Table,
        retired_service: Service,
    ) -> None:
        """Nest a retired service's tables when include_tables is also set."""
        response = test_client.get(
            f"/services/{retired_service.id}/schemas/",
            params={"include_retired": True, "include_tables": "true"},
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [t["id"] for t in items[0]["tables"]] == [table.id]

    def test_list_schemas_by_service_include_retired_on_active_service(
        self, test_client: TestClient, retired_schema: Schema
    ) -> None:
        """Include a retired schema of an active service through the opt-in."""
        response = test_client.get(
            f"/services/{retired_schema.service_id}/schemas/",
            params={"include_retired": True},
        )
        assert response.status_code == status.HTTP_200_OK
        assert [item["id"] for item in response.json()["items"]] == [retired_schema.id]

    def test_list_schemas_by_service_rejects_invalid_include_retired(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Reject a non-boolean include_retired with HTTP 422."""
        response = test_client.get(
            f"/services/{service.id}/schemas/", params={"include_retired": "maybe"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_schemas_by_service_rejects_unknown_sort_key(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"sort": "evil"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_schemas_by_service(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return compact schemas without tables for the given service."""
        response = test_client.get(f"/services/{service.id}/schemas/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["id"] == schema.id
        assert "tables" not in data["items"][0]

    def test_list_schemas_by_service_search(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return only schemas whose name matches the search case-insensitively."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"search": schema.name[:3]},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == schema.id

    def test_list_schemas_by_service_search_no_match(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return empty paginated response when search does not match any schema."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"search": "nonexistent_schema_xyz"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_schemas_by_service_include_tables(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return schemas with nested tables when include_tables is set."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"include_tables": "true"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["id"] == schema.id
        assert "tables" in data["items"][0]

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": True}], ids=["active", "include_retired"]
    )
    def test_list_schemas_by_service_not_found(
        self, test_client: TestClient, params: dict[str, bool]
    ) -> None:
        """Return 404 for a nonexistent service ID in either retirement scope."""
        response = test_client.get("/services/9999/schemas/", params=params)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_schemas_by_service_custom_offset(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"offset": OFFSET_BEYOND_TOTAL},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_schemas_by_service_custom_limit(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_schemas_by_service_search_with_pagination(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return search-filtered total with pagination params."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"search": schema.name[:3], "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] >= 1
        assert len(data["items"]) <= 1

    def test_list_schemas_by_service_search_reports_filtered_total(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for suffix in ("a", "b"):
            payload = SchemaWriteFactory.build(name=f"FilterMatchSchema_{suffix}")
            create_response = test_client.post(
                f"/services/{service.id}/schemas/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
        other = SchemaWriteFactory.build(name="UnrelatedSchema")
        test_client.post(
            f"/services/{service.id}/schemas/",
            json=other.model_dump(mode="json"),
        )

        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"search": "filtermatchschema", "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_list_schemas_by_service_deterministic_ordering_across_pages(
        self, test_client: TestClient, session: AsyncSession, service: Service
    ) -> None:
        """Sort equal created_at stably across pages via the id tie-breaker.

        Schema names are unique per service, so name ties cannot occur in this
        nested list; exercise the tie-breaker on created_at instead.
        """
        shared_created_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        created_ids: list[int] = []
        for name in ("tie_schema_a", "tie_schema_b"):
            row = await SchemaManager.create(
                session,
                SchemaWriteFactory.build(name=name),
                service_id=service.id,
            )
            row.created_at = shared_created_at
            session.add(row)
            await session.commit()
            await session.refresh(row)
            created_ids.append(row.id)
        created_ids.sort()

        first_page = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"sort": "created_at", "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"sort": "created_at", "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]

    def test_list_schemas_by_service_include_tables_with_pagination(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return paginated SchemaResponse with tables."""
        response = test_client.get(
            f"/services/{service.id}/schemas/",
            params={"include_tables": "true", "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert "tables" in data["items"][0]


class TestCreateSchemaForService:
    """Test POST /services/{service_id}/schemas/ endpoint."""

    def test_create_schema_for_service(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Create a schema for a service and return 201."""
        payload = SchemaWriteFactory.build()
        response = test_client.post(
            f"/services/{service.id}/schemas/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["name"] == payload.name

    def test_create_schema_for_service_duplicate_name(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Return 409 when creating a schema with a duplicate name."""
        payload = SchemaWriteFactory.build()
        payload.name = schema.name
        response = test_client.post(
            f"/services/{service.id}/schemas/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_create_schema_for_service_not_found(self, test_client: TestClient) -> None:
        """Return 404 when creating a schema for a nonexistent service."""
        payload = SchemaWriteFactory.build()
        response = test_client.post(
            "/services/9999/schemas/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_schema_for_retired_service_ignores_include_retired(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Refuse a schema under a retired service, whatever the read opt-in says."""
        payload = SchemaWriteFactory.build()
        response = test_client.post(
            f"/services/{retired_service.id}/schemas/",
            params={"include_retired": True},
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRetrieveServiceSystemObservation:
    """Test GET /services/{service_id}/system-observation endpoint."""

    def test_retrieve_service_system_observation(
        self,
        test_client: TestClient,
        service: Service,
        service_observation: ServiceSystemObservation,
    ) -> None:
        """Return service observation with all fields for a service that has one."""
        response = test_client.get(f"/services/{service.id}/system-observation")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["service_id"] == service.id
        assert data["db_engine_version"] == service_observation.db_engine_version
        assert "observed_at" in data
        assert "id" in data

    def test_retrieve_service_system_observation_404_when_no_observation(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 404 with the uncollected detail when the service has no observation."""
        response = test_client.get(f"/services/{service.id}/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == UNCOLLECTED_SERVICE_DETAIL

    def test_retrieve_service_system_observation_404_when_service_not_found(
        self, test_client: TestClient
    ) -> None:
        """Return 404 with the default detail when the service ID does not exist."""
        response = test_client.get("/services/99999/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == "Not Found"

    def test_retrieve_service_system_observation_404_for_another_services_observation(
        self,
        test_client: TestClient,
        node: Node,
        service: Service,
        service_observation: ServiceSystemObservation,
    ) -> None:
        """Keep one service's observation from surfacing when reading a sibling."""
        other = test_client.post(
            f"/nodes/{node.id}/services/",
            json=ServiceWriteFactory.build().model_dump(mode="json"),
        )
        assert other.status_code == status.HTTP_201_CREATED
        other_service_id = other.json()["id"]
        assert other_service_id != service.id

        response = test_client.get(f"/services/{other_service_id}/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == UNCOLLECTED_SERVICE_DETAIL

    def test_retrieve_service_system_observation_after_upsert_returns_200(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Leave the uncollected state once the syncer writes an observation.

        The uncollected 404 must be a transient state, not a sticky one: the same
        GET that reported it has to answer 200 after a PUT lands.
        """
        before = test_client.get(f"/services/{service.id}/system-observation")
        assert before.status_code == status.HTTP_404_NOT_FOUND
        assert before.json()["detail"] == UNCOLLECTED_SERVICE_DETAIL

        payload = ServiceSystemObservationWriteFactory.build()
        upsert = test_client.put(
            f"/services/{service.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert upsert.status_code == status.HTTP_200_OK

        after = test_client.get(f"/services/{service.id}/system-observation")
        assert after.status_code == status.HTTP_200_OK
        assert after.json()["service_id"] == service.id
        assert after.json()["db_engine_version"] == payload.db_engine_version


class TestUpsertServiceSystemObservation:
    """Test PUT /services/{service_id}/system-observation endpoint."""

    def test_upsert_creates_new_observation(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Create a new observation when none exists and return 200."""
        payload = ServiceSystemObservationWriteFactory.build()
        response = test_client.put(
            f"/services/{service.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["service_id"] == service.id
        assert data["db_engine_version"] == payload.db_engine_version
        assert "id" in data

    def test_upsert_updates_existing_observation(
        self,
        test_client: TestClient,
        node: Node,
        service: Service,
        service_observation: ServiceSystemObservation,
    ) -> None:
        """Update existing observation in place and return 200 with updated fields."""
        test_client.post(
            f"/nodes/{node.id}/services/",
            json=ServiceWriteFactory.build().model_dump(mode="json"),
        )
        payload = ServiceSystemObservationWriteFactory.build(db_engine_version="8.4.0")
        response = test_client.put(
            f"/services/{service.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["db_engine_version"] == "8.4.0"
        assert response.json()["id"] == service_observation.id

    def test_upsert_idempotent_no_conflict_on_second_put(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 200 on a repeat PUT with the same payload (no unique-constraint error)."""
        payload = ServiceSystemObservationWriteFactory.build()
        json_payload = payload.model_dump(mode="json")
        first = test_client.put(
            f"/services/{service.id}/system-observation", json=json_payload
        )
        assert first.status_code == status.HTTP_200_OK
        second = test_client.put(
            f"/services/{service.id}/system-observation", json=json_payload
        )
        assert second.status_code == status.HTTP_200_OK

    def test_upsert_preserves_same_id_across_updates(
        self,
        test_client: TestClient,
        service: Service,
        service_observation: ServiceSystemObservation,
    ) -> None:
        """Update an existing observation in place on PUT — same DB row, same id."""
        payload = ServiceSystemObservationWriteFactory.build(db_engine_version="5.7.44")
        response = test_client.put(
            f"/services/{service.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == service_observation.id

    def test_upsert_404_when_service_not_found(self, test_client: TestClient) -> None:
        """Return 404 when the service ID does not exist."""
        payload = ServiceSystemObservationWriteFactory.build()
        response = test_client.put(
            "/services/99999/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_upsert_422_missing_observed_at(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 422 when required field observed_at is absent."""
        data = ServiceSystemObservationWriteFactory.build().model_dump(mode="json")
        del data["observed_at"]
        response = test_client.put(
            f"/services/{service.id}/system-observation", json=data
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_upsert_422_missing_db_engine_version(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 422 when required field db_engine_version is absent."""
        data = ServiceSystemObservationWriteFactory.build().model_dump(mode="json")
        del data["db_engine_version"]
        response = test_client.put(
            f"/services/{service.id}/system-observation", json=data
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestRecordServiceSyncHealth:
    """Test the POST /services/{service_id}/sync-health endpoint."""

    @pytest.mark.asyncio
    async def test_success_outcome_clears_the_failure_state(
        self, test_client: TestClient, session: AsyncSession, service: Service
    ) -> None:
        """Record the freshness and answer 204."""
        response = test_client.post(
            f"/services/{service.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(service)
        assert service.last_synced_at is not None
        assert service.last_sync_error is None
        assert service.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_outcome_opens_the_failure_run(
        self, test_client: TestClient, session: AsyncSession, service: Service
    ) -> None:
        """Record the failure columns and answer 204."""
        response = test_client.post(
            f"/services/{service.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(service)
        assert service.last_synced_at is None
        assert service.last_sync_error == "boom"
        assert service.sync_failing_since is not None
        assert service.consecutive_failures == 1

    @pytest.mark.parametrize(
        "body", INVALID_SYNC_HEALTH_BODIES, ids=INVALID_SYNC_HEALTH_BODY_IDS
    )
    def test_rejects_an_inconsistent_or_incomplete_body(
        self, test_client: TestClient, service: Service, body: dict[str, str]
    ) -> None:
        """Refuse every body shape the write model declares invalid."""
        response = test_client.post(f"/services/{service.id}/sync-health", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_service_is_not_found(self, test_client: TestClient) -> None:
        """Answer 404 when the addressed service does not exist."""
        response = test_client.post(
            "/services/99999/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_retired_service_still_records_the_outcome(
        self,
        test_client: TestClient,
        session: AsyncSession,
        retired_service: Service,
    ) -> None:
        """Record the attempt against a service retired concurrently with the sync."""
        response = test_client.post(
            f"/services/{retired_service.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(retired_service)
        assert retired_service.consecutive_failures == 1


class TestServiceSyncHealthReads:
    """Test that the service read responses expose the sync-health columns."""

    def test_detail_exposes_the_columns(
        self, test_client: TestClient, service: Service, schema: Schema
    ) -> None:
        """Carry the sync-health fields on the service detail response.

        The nested schemas carry them through the table model the response
        nests, so a schema read from inside a service reports the same state.
        """
        response = test_client.get(f"/services/{service.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.keys() >= SYNC_HEALTH_RESPONSE_KEYS
        assert data["schemas"][0].keys() >= SYNC_HEALTH_RESPONSE_KEYS

    def test_list_items_expose_the_columns(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Carry the sync-health fields on every row of the paginated list."""
        response = test_client.get("/services/")

        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert items, "the service fixture should have produced a row to read back"
        assert items[0].keys() >= SYNC_HEALTH_RESPONSE_KEYS
