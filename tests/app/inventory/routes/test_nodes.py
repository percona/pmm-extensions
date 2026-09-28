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

"""Define tests for inventory node routes."""

from datetime import datetime

import pytest
from sqlalchemy import event
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette import status
from starlette.testclient import TestClient

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.inventory.models import (
    HOST_OBSERVATION_FIELD_NAMES,
    HostSystemObservation,
    Node,
    Schema,
    Service,
    ServiceTypeEnum,
    SourceEnum,
    SyncOutcomeEnum,
    Table,
)
from tests.app.factories import (
    HostSystemObservationWriteFactory,
    NodeWriteFactory,
    ServiceWriteFactory,
)
from tests.app.inventory.conftest import (
    INVALID_SYNC_HEALTH_BODIES,
    INVALID_SYNC_HEALTH_BODY_IDS,
    retire_in_place,
    SYNC_HEALTH_ATTEMPTED_AT,
    sync_health_payload,
    SYNC_HEALTH_RESPONSE_KEYS,
)
from tests.app.inventory.legacy_origin import PRE_RENAME_LEGACY_PREFIX

CREATED_NODE_COUNT = 2
OFFSET_BEYOND_TOTAL = 999
LIST_QUERY_MATCH_TOTAL = 2
OBSERVED_NODE_COUNT = 2

# Pinned verbatim rather than imported from app.inventory.constants: the wording is
# part of the API contract, so an edit to the constant must fail the test.
UNCOLLECTED_NODE_DETAIL = "System observation not collected yet for this node"


class TestListNodes:
    """Test the GET /nodes/ endpoint."""

    def test_list_nodes_empty(self, test_client: TestClient) -> None:
        """Return an empty paginated response when no nodes exist."""
        response = test_client.get("/nodes/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    def test_list_nodes_excludes_retired(
        self, test_client: TestClient, retired_node: Node
    ) -> None:
        """Omit a retired node from the default list."""
        response = test_client.get("/nodes/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_include_retired_resolves_a_legacy_tombstone(
        self, test_client: TestClient, session: AsyncSession, node: Node
    ) -> None:
        """Serve a tombstone carrying the migration's synthetic origin.

        The migration stamps a legacy-prefixed ``external_id`` onto a brownfield row
        so the NOT NULL constraint can land. ``NodeResponse`` now requires an origin,
        so the stamped value is what keeps such a row readable at all through the
        retired-inclusive route the historical and sync paths use.
        """
        node.external_id = f"{PRE_RENAME_LEGACY_PREFIX}{node.id}"
        node.source = SourceEnum.PMM
        session.add(node)
        await session.commit()
        await retire_in_place(session, node)

        active = test_client.get(f"/nodes/{node.id}")
        assert active.status_code == status.HTTP_404_NOT_FOUND

        retired = test_client.get(f"/nodes/{node.id}", params={"include_retired": True})
        assert retired.status_code == status.HTTP_200_OK
        body = retired.json()
        assert body["external_id"] == f"{PRE_RENAME_LEGACY_PREFIX}{node.id}"
        assert body["source"] == SourceEnum.PMM.value
        assert body["retired_at"] is not None

    def test_list_nodes_include_retired(
        self, test_client: TestClient, retired_node: Node
    ) -> None:
        """List a retired node through the opt-in with a matching total."""
        response = test_client.get("/nodes/", params={"include_retired": True})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [item["id"] for item in data["items"]] == [retired_node.id]
        assert data["total"] == len(data["items"])

    def test_list_nodes_hides_retired_service_nested_in_active_node(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Drop a retired service from the services nested in an active node."""
        response = test_client.get("/nodes/")
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["id"] for item in items] == [retired_service.node_id]
        assert items[0]["services"] == []

    def test_rejects_limit_zero(self, test_client: TestClient) -> None:
        """Return 422 when limit is zero."""
        response = test_client.get("/nodes/", params={"limit": 0})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_rejects_limit_above_cap(self, test_client: TestClient) -> None:
        """Return 422 when limit exceeds the global cap."""
        response = test_client.get("/nodes/", params={"limit": 201})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_rejects_negative_limit(self, test_client: TestClient) -> None:
        """Return 422 when limit is negative."""
        response = test_client.get("/nodes/", params={"limit": -1})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_nodes_rejects_unknown_sort_key(self, test_client: TestClient) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get("/nodes/", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_nodes_multiple(self, test_client: TestClient) -> None:
        """Return a list of nodes with services loaded."""
        payload1 = NodeWriteFactory.build()
        payload2 = NodeWriteFactory.build()
        test_client.post("/nodes/", json=payload1.model_dump(mode="json"))
        test_client.post("/nodes/", json=payload2.model_dump(mode="json"))

        response = test_client.get("/nodes/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == CREATED_NODE_COUNT
        assert data["total"] == CREATED_NODE_COUNT
        assert "services" in data["items"][0]

    def test_list_nodes_filter_by_source(self, test_client: TestClient) -> None:
        """Return the PMM-sourced nodes for the source filter.

        ``SourceEnum`` has a single member and every node now carries it, so the
        filter can no longer be shown to exclude anything through the API. What
        it still proves is that the parameter resolves and narrows to the rows
        carrying that source rather than 422-ing or matching nothing.
        """
        for external_id in ("pmm-node-1", "pmm-node-2"):
            payload = NodeWriteFactory.build(
                source=SourceEnum.PMM, external_id=external_id
            )
            test_client.post("/nodes/", json=payload.model_dump(mode="json"))

        response = test_client.get("/nodes/", params={"source": SourceEnum.PMM.value})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == CREATED_NODE_COUNT
        assert {item["source"] for item in data["items"]} == {SourceEnum.PMM.value}

    def test_list_nodes_filter_by_external_id(self, test_client: TestClient) -> None:
        """Return only nodes matching the given external_id filter."""
        matching = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="abc")
        other = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="xyz")
        test_client.post("/nodes/", json=matching.model_dump(mode="json"))
        test_client.post("/nodes/", json=other.model_dump(mode="json"))

        response = test_client.get("/nodes/", params={"external_id": "abc"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["external_id"] == "abc"

    def test_list_nodes_filter_by_node_type(self, test_client: TestClient) -> None:
        """Return only nodes matching the given node_type filter."""
        generic = NodeWriteFactory.build(type="generic")
        remote = NodeWriteFactory.build(type="remote")
        test_client.post("/nodes/", json=generic.model_dump(mode="json"))
        test_client.post("/nodes/", json=remote.model_dump(mode="json"))

        response = test_client.get("/nodes/", params={"node_type": "generic"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["type"] == "generic"

    def test_list_nodes_custom_offset(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get("/nodes/", params={"offset": OFFSET_BEYOND_TOTAL})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_nodes_custom_limit(self, test_client: TestClient, node: Node) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get("/nodes/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_nodes_filter_with_pagination(
        self,
        test_client: TestClient,
    ) -> None:
        """Return filtered total with pagination params."""
        generic = NodeWriteFactory.build(type="generic")
        remote = NodeWriteFactory.build(type="remote")
        test_client.post("/nodes/", json=generic.model_dump(mode="json"))
        test_client.post("/nodes/", json=remote.model_dump(mode="json"))

        response = test_client.get(
            "/nodes/", params={"node_type": "generic", "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_list_nodes_search_ilike(self, test_client: TestClient) -> None:
        """Return only nodes whose name matches the search case-insensitively."""
        match = NodeWriteFactory.build(name="AlphaSearchNode")
        other = NodeWriteFactory.build(name="OtherNode")
        test_client.post("/nodes/", json=match.model_dump(mode="json"))
        test_client.post("/nodes/", json=other.model_dump(mode="json"))

        response = test_client.get("/nodes/", params={"search": "alphasearch"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == match.name

    def test_list_nodes_search_reports_filtered_total(
        self, test_client: TestClient
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for suffix in ("a", "b"):
            payload = NodeWriteFactory.build(name=f"FilterMatchNode_{suffix}")
            test_client.post("/nodes/", json=payload.model_dump(mode="json"))
        other = NodeWriteFactory.build(name="UnrelatedNode")
        test_client.post("/nodes/", json=other.model_dump(mode="json"))

        response = test_client.get(
            "/nodes/", params={"search": "filtermatchnode", "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    def test_list_nodes_deterministic_ordering_across_pages(
        self, test_client: TestClient
    ) -> None:
        """Sort equal names stably across pages via the id tie-breaker."""
        shared_name = "SameSortNode"
        created_ids: list[int] = []
        for _ in range(LIST_QUERY_MATCH_TOTAL):
            payload = NodeWriteFactory.build(name=shared_name)
            create_response = test_client.post(
                "/nodes/", json=payload.model_dump(mode="json")
            )
            assert create_response.status_code == status.HTTP_201_CREATED
            created_ids.append(create_response.json()["id"])
        created_ids.sort()

        first_page = test_client.get(
            "/nodes/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            "/nodes/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]


class TestRetrieveNode:
    """Test the GET /nodes/{node_id} endpoint."""

    def test_retrieve_node(self, test_client: TestClient, node: Node) -> None:
        """Return a node with its services list."""
        response = test_client.get(f"/nodes/{node.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == node.id
        assert "services" in data

    def test_retrieve_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent node ID."""
        response = test_client.get("/nodes/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_node_retired_returns_404(
        self, test_client: TestClient, retired_node: Node
    ) -> None:
        """Hide a retired node from the default read."""
        response = test_client.get(f"/nodes/{retired_node.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_node_retired_with_opt_in(
        self, test_client: TestClient, retired_node: Node
    ) -> None:
        """Resolve a retired node through the opt-in and expose its timestamp."""
        response = test_client.get(
            f"/nodes/{retired_node.id}", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["retired_at"] is not None


class TestCreateNode:
    """Test the POST /nodes/ endpoint."""

    def test_create_node(self, test_client: TestClient) -> None:
        """Create a node and return 201 with the new ID."""
        payload = NodeWriteFactory.build()
        response = test_client.post("/nodes/", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_201_CREATED
        assert "id" in response.json()

    def test_create_node_with_source(self, test_client: TestClient) -> None:
        """Create a node with source and external_id."""
        payload = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="ext-123")
        response = test_client.post("/nodes/", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["source"] == "pmm"
        assert data["external_id"] == "ext-123"

    def test_create_node_duplicate_external_id_source(
        self, test_client: TestClient
    ) -> None:
        """Return 409 when creating a node with duplicate (external_id, source)."""
        payload = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="dup-ext")
        response = test_client.post("/nodes/", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_201_CREATED

        payload2 = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="dup-ext")
        response2 = test_client.post("/nodes/", json=payload2.model_dump(mode="json"))
        assert response2.status_code == status.HTTP_409_CONFLICT

    def test_create_node_external_id_without_source(
        self, test_client: TestClient
    ) -> None:
        """Return 422 when external_id is set but source is None."""
        payload = NodeWriteFactory.build()
        data = payload.model_dump(mode="json")
        data["external_id"] = "some-id"
        data["source"] = None
        response = test_client.post("/nodes/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_node_missing_external_id(self, test_client: TestClient) -> None:
        """Return 422 when external_id is absent from the body."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        del data["external_id"]
        response = test_client.post("/nodes/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_node_missing_source(self, test_client: TestClient) -> None:
        """Return 422 when source is absent from the body."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        del data["source"]
        response = test_client.post("/nodes/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_node_null_external_id(self, test_client: TestClient) -> None:
        """Return 422 when external_id is explicitly null."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        data["external_id"] = None
        response = test_client.post("/nodes/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_node_with_full_origin(self, test_client: TestClient) -> None:
        """Create a node and echo back the PMM origin it was given."""
        payload = NodeWriteFactory.build(
            source=SourceEnum.PMM, external_id="/node_id/full-origin"
        )
        response = test_client.post("/nodes/", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["external_id"] == "/node_id/full-origin"
        assert body["source"] == SourceEnum.PMM


class TestUpdateNode:
    """Test the PUT /nodes/{node_id} endpoint."""

    def test_update_node(self, test_client: TestClient, node: Node) -> None:
        """Update a node name and return 200."""
        payload = NodeWriteFactory.build(name="updated-name")
        response = test_client.put(
            f"/nodes/{node.id}", json=payload.model_dump(mode="json")
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "updated-name"

    def test_update_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent node ID."""
        payload = NodeWriteFactory.build()
        response = test_client.put("/nodes/99999", json=payload.model_dump(mode="json"))
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_node_missing_external_id(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 422 when the update body omits external_id."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        del data["external_id"]
        response = test_client.put(f"/nodes/{node.id}", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_update_node_null_external_id_leaves_row_intact(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Reject an explicit-null external_id without clearing the stored origin."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        data["external_id"] = None
        response = test_client.put(f"/nodes/{node.id}", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        stored = test_client.get(f"/nodes/{node.id}")
        assert stored.status_code == status.HTTP_200_OK
        assert stored.json()["external_id"] == node.external_id

    def test_update_node_null_source_leaves_row_intact(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Reject an explicit-null source without clearing the stored origin."""
        data = NodeWriteFactory.build().model_dump(mode="json")
        data["source"] = None
        response = test_client.put(f"/nodes/{node.id}", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        stored = test_client.get(f"/nodes/{node.id}")
        assert stored.status_code == status.HTTP_200_OK
        assert stored.json()["source"] == node.source

    def test_update_node_duplicate_external_id_source(
        self, test_client: TestClient
    ) -> None:
        """Return 409 when an update retargets a node onto another node's origin."""
        first = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="ext-first")
        response = test_client.post("/nodes/", json=first.model_dump(mode="json"))
        assert response.status_code == status.HTTP_201_CREATED

        second = NodeWriteFactory.build(source=SourceEnum.PMM, external_id="ext-second")
        created = test_client.post("/nodes/", json=second.model_dump(mode="json"))
        assert created.status_code == status.HTTP_201_CREATED

        retarget = NodeWriteFactory.build(
            source=SourceEnum.PMM, external_id="ext-first"
        )
        response = test_client.put(
            f"/nodes/{created.json()['id']}", json=retarget.model_dump(mode="json")
        )
        assert response.status_code == status.HTTP_409_CONFLICT


class TestDeleteNode:
    """Test the DELETE /nodes/{node_id} endpoint."""

    def test_delete_node(self, test_client: TestClient, node: Node) -> None:
        """Retire a node and confirm the default read no longer resolves it."""
        response = test_client.delete(f"/nodes/{node.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        response = test_client.get(f"/nodes/{node.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent node ID."""
        response = test_client.delete("/nodes/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_node_retires_and_keeps_subtree(
        self,
        test_client: TestClient,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Retire the node's whole subtree on its existing primary keys."""
        response = test_client.delete(f"/nodes/{node.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        for path, entity_id in (
            ("nodes", node.id),
            ("services", service.id),
            ("schemas", schema.id),
            ("tables", table.id),
        ):
            retired = test_client.get(
                f"/{path}/{entity_id}", params={"include_retired": True}
            )
            assert retired.status_code == status.HTTP_200_OK
            assert retired.json()["id"] == entity_id
            assert retired.json()["retired_at"] is not None

    def test_delete_node_is_idempotent(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Leave the original timestamp alone when the node is retired again."""
        assert (
            test_client.delete(f"/nodes/{node.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        first = test_client.get(
            f"/nodes/{node.id}", params={"include_retired": True}
        ).json()["retired_at"]

        assert (
            test_client.delete(f"/nodes/{node.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        second = test_client.get(
            f"/nodes/{node.id}", params={"include_retired": True}
        ).json()["retired_at"]
        assert second == first


class TestReviveNode:
    """Test the POST /nodes/{node_id}/revive endpoint."""

    def test_revive_node(self, test_client: TestClient, service: Service) -> None:
        """Revive the node without resurrecting the services retired with it."""
        node_id = service.node_id
        assert (
            test_client.delete(f"/nodes/{node_id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        response = test_client.post(f"/nodes/{node_id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        revived = test_client.get(f"/nodes/{node_id}")
        assert revived.status_code == status.HTTP_200_OK
        assert revived.json()["retired_at"] is None
        assert test_client.get(f"/services/{service.id}").status_code == (
            status.HTTP_404_NOT_FOUND
        )

    def test_revive_active_node_is_a_noop(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 204 without touching a node that was never retired."""
        response = test_client.post(f"/nodes/{node.id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert test_client.get(f"/nodes/{node.id}").json()["retired_at"] is None

    def test_revive_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent node ID."""
        response = test_client.post("/nodes/99999/revive")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestListServicesByNode:
    """Test the GET /nodes/{node_id}/services/ endpoint."""

    def test_list_services_by_node_empty(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return an empty paginated response when the node has no services."""
        response = test_client.get(f"/nodes/{node.id}/services/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_services_by_node_excludes_retired(
        self,
        test_client: TestClient,
        retired_service: Service,
        params: dict[str, bool],
    ) -> None:
        """Omit a retired service from an active node's services."""
        response = test_client.get(
            f"/nodes/{retired_service.node_id}/services/", params=params
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_services_by_node_include_retired_resolves_retired_node(
        self, test_client: TestClient, service: Service, retired_node: Node
    ) -> None:
        """List a retired node's services through the opt-in."""
        response = test_client.get(
            f"/nodes/{retired_node.id}/services/", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [item["id"] for item in data["items"]] == [service.id]
        assert data["total"] == 1

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_services_by_node_hides_retired_node_by_default(
        self,
        test_client: TestClient,
        service: Service,
        retired_node: Node,
        params: dict[str, bool],
    ) -> None:
        """Return 404 for a retired node unless the opt-in is set."""
        response = test_client.get(f"/nodes/{retired_node.id}/services/", params=params)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_services_by_node_include_retired_after_retire_route(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """List the services the retire route cascaded into, marked retired."""
        assert (
            test_client.delete(f"/nodes/{node.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        hidden = test_client.get(f"/nodes/{node.id}/services/")
        assert hidden.status_code == status.HTTP_404_NOT_FOUND

        response = test_client.get(
            f"/nodes/{node.id}/services/", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["id"] for item in items] == [service.id]
        assert items[0]["retired_at"] is not None

    @pytest.mark.parametrize("same_type", [True, False], ids=["matching", "other"])
    def test_list_services_by_node_include_retired_keeps_type_filter(
        self,
        test_client: TestClient,
        service: Service,
        retired_node: Node,
        *,
        same_type: bool,
    ) -> None:
        """Apply the service_type filter to a retired node's services."""
        service_type = (
            service.type
            if same_type
            else next(t for t in ServiceTypeEnum if t != service.type)
        )
        response = test_client.get(
            f"/nodes/{retired_node.id}/services/",
            params={"include_retired": True, "service_type": service_type.value},
        )
        assert response.status_code == status.HTTP_200_OK
        expected = [service.id] if same_type else []
        assert [item["id"] for item in response.json()["items"]] == expected

    def test_list_services_by_node_include_retired_on_active_node(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Include a retired service of an active node through the opt-in."""
        response = test_client.get(
            f"/nodes/{retired_service.node_id}/services/",
            params={"include_retired": True},
        )
        assert response.status_code == status.HTTP_200_OK
        assert [item["id"] for item in response.json()["items"]] == [retired_service.id]

    def test_list_services_by_node_rejects_invalid_include_retired(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Reject a non-boolean include_retired with HTTP 422."""
        response = test_client.get(
            f"/nodes/{node.id}/services/", params={"include_retired": "maybe"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_services_by_node_rejects_unknown_sort_key(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"sort": "evil"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_services_by_node(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Return services with schemas loaded for a node."""
        response = test_client.get(f"/nodes/{node.id}/services/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["id"] == service.id
        assert "schemas" in data["items"][0]

    def test_list_services_by_node_filter_service_type(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Return only services matching the given service_type filter."""
        response = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"service_type": service.type.value},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == service.id

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": True}], ids=["active", "include_retired"]
    )
    def test_list_services_by_node_not_found(
        self, test_client: TestClient, params: dict[str, bool]
    ) -> None:
        """Return 404 for a nonexistent node ID in either retirement scope."""
        response = test_client.get("/nodes/99999/services/", params=params)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_services_by_node_custom_offset(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get(
            f"/nodes/{node.id}/services/", params={"offset": OFFSET_BEYOND_TOTAL}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_services_by_node_custom_limit(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get(f"/nodes/{node.id}/services/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_services_by_node_search_ilike(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Return only services whose name matches the search case-insensitively."""
        match = ServiceWriteFactory.build(name="AlphaSearchByNode", port=4701)
        other = ServiceWriteFactory.build(name="OtherByNode", port=4702)
        for payload in (match, other):
            create_response = test_client.post(
                f"/nodes/{node.id}/services/", json=payload.model_dump(mode="json")
            )
            assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"search": "alphasearchbynode"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == match.name

    def test_list_services_by_node_search_reports_filtered_total(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for index, suffix in enumerate(("a", "b")):
            payload = ServiceWriteFactory.build(
                name=f"FilterMatchByNode_{suffix}",
                port=4800 + index,
            )
            create_response = test_client.post(
                f"/nodes/{node.id}/services/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
        other = ServiceWriteFactory.build(name="UnrelatedByNode", port=4899)
        create_response = test_client.post(
            f"/nodes/{node.id}/services/",
            json=other.model_dump(mode="json"),
        )
        assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"search": "filtermatchbynode", "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    def test_list_services_by_node_deterministic_ordering_across_pages(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Sort equal names stably across pages via the id tie-breaker."""
        shared_name = "SameSortByNode"
        created_ids: list[int] = []
        for index in range(LIST_QUERY_MATCH_TOTAL):
            payload = ServiceWriteFactory.build(name=shared_name, port=4900 + index)
            create_response = test_client.post(
                f"/nodes/{node.id}/services/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
            created_ids.append(create_response.json()["id"])
        created_ids.sort()

        first_page = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            f"/nodes/{node.id}/services/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]


class TestCreateServiceForNode:
    """Test the POST /nodes/{node_id}/services/ endpoint."""

    def test_create_service_for_node(self, test_client: TestClient, node: Node) -> None:
        """Create a service for a node and return 201."""
        payload = ServiceWriteFactory.build()
        response = test_client.post(
            f"/nodes/{node.id}/services/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["node_id"] == node.id

    def test_create_service_over_retired_predecessor(
        self, test_client: TestClient, retired_service: Service
    ) -> None:
        """Admit a replacement on the node and port a tombstone still holds."""
        payload = ServiceWriteFactory.build(port=retired_service.port)
        response = test_client.post(
            f"/nodes/{retired_service.node_id}/services/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["id"] != retired_service.id

    def test_create_second_active_service_on_same_port_succeeds(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Admit a second active service sharing a node and port with the first."""
        payload = ServiceWriteFactory.build(port=service.port)
        response = test_client.post(
            f"/nodes/{service.node_id}/services/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["id"] != service.id

    def test_create_service_for_node_with_external_id(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Create a service carrying an external_id under a PMM-sourced node."""
        payload = ServiceWriteFactory.build(external_id="svc-ext-123")
        response = test_client.post(
            f"/nodes/{node.id}/services/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["external_id"] == "svc-ext-123"

    def test_create_service_for_node_missing_external_id(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 422 when the service body omits external_id."""
        data = ServiceWriteFactory.build().model_dump(mode="json")
        del data["external_id"]
        response = test_client.post(f"/nodes/{node.id}/services/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_service_for_node_null_external_id(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 422 when the service body carries an explicit-null external_id."""
        data = ServiceWriteFactory.build().model_dump(mode="json")
        data["external_id"] = None
        response = test_client.post(f"/nodes/{node.id}/services/", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_service_for_node_duplicate_external_id(
        self, test_client: TestClient
    ) -> None:
        """Return 409 when creating a service with duplicate (external_id, node_id)."""
        node_payload = NodeWriteFactory.build(
            source=SourceEnum.PMM, external_id="node-ext"
        )
        node_resp = test_client.post(
            "/nodes/", json=node_payload.model_dump(mode="json")
        )
        node_id = node_resp.json()["id"]

        svc_payload = ServiceWriteFactory.build(external_id="svc-dup")
        response = test_client.post(
            f"/nodes/{node_id}/services/",
            json=svc_payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED

        svc_payload2 = ServiceWriteFactory.build(external_id="svc-dup")
        response2 = test_client.post(
            f"/nodes/{node_id}/services/",
            json=svc_payload2.model_dump(mode="json"),
        )
        assert response2.status_code == status.HTTP_409_CONFLICT

    def test_create_two_identified_services_on_same_port(
        self, test_client: TestClient
    ) -> None:
        """Return 201 for both services PMM identifies on one node and port."""
        node_payload = NodeWriteFactory.build(
            source=SourceEnum.PMM, external_id="node-shared-port"
        )
        node_id = test_client.post(
            "/nodes/", json=node_payload.model_dump(mode="json")
        ).json()["id"]

        first = test_client.post(
            f"/nodes/{node_id}/services/",
            json=ServiceWriteFactory.build(external_id="svc-a", port=5432).model_dump(
                mode="json"
            ),
        )
        second = test_client.post(
            f"/nodes/{node_id}/services/",
            json=ServiceWriteFactory.build(external_id="svc-b", port=5432).model_dump(
                mode="json"
            ),
        )

        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_201_CREATED
        assert first.json()["id"] != second.json()["id"]

    def test_create_third_service_on_a_port_two_others_already_hold(
        self, test_client: TestClient
    ) -> None:
        """Return 201 because port is no longer a uniqueness key at all."""
        node_payload = NodeWriteFactory.build(
            source=SourceEnum.PMM, external_id="node-mixed-port"
        )
        node_id = test_client.post(
            "/nodes/", json=node_payload.model_dump(mode="json")
        ).json()["id"]
        assert (
            test_client.post(
                f"/nodes/{node_id}/services/",
                json=ServiceWriteFactory.build(
                    external_id="svc-identified", port=5432
                ).model_dump(mode="json"),
            ).status_code
            == status.HTTP_201_CREATED
        )

        response = test_client.post(
            f"/nodes/{node_id}/services/",
            json=ServiceWriteFactory.build(port=5432).model_dump(mode="json"),
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_create_service_for_node_duplicate_port(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 201 twice for two services sharing one (port, node_id)."""
        svc_payload = ServiceWriteFactory.build(port=3306)
        response = test_client.post(
            f"/nodes/{node.id}/services/",
            json=svc_payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_201_CREATED

        svc_payload2 = ServiceWriteFactory.build(port=3306)
        response2 = test_client.post(
            f"/nodes/{node.id}/services/",
            json=svc_payload2.model_dump(mode="json"),
        )
        assert response2.status_code == status.HTTP_201_CREATED
        assert response2.json()["id"] != response.json()["id"]

    def test_create_service_for_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent node ID."""
        payload = ServiceWriteFactory.build()
        response = test_client.post(
            "/nodes/99999/services/",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_service_for_retired_node_ignores_include_retired(
        self, test_client: TestClient, retired_node: Node
    ) -> None:
        """Refuse a service under a retired node, whatever the read opt-in says."""
        payload = ServiceWriteFactory.build()
        response = test_client.post(
            f"/nodes/{retired_node.id}/services/",
            params={"include_retired": True},
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRetrieveHostSystemObservation:
    """Test GET /nodes/{node_id}/system-observation endpoint."""

    def test_retrieve_host_system_observation(
        self,
        test_client: TestClient,
        node: Node,
        host_observation: HostSystemObservation,
    ) -> None:
        """Return host observation with all fields for a node that has one."""
        response = test_client.get(f"/nodes/{node.id}/system-observation")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["node_id"] == node.id
        assert data["os_version"] == host_observation.os_version
        assert "observed_at" in data
        assert "id" in data

    def test_retrieve_host_system_observation_404_when_no_observation(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 404 with the uncollected detail when the node has no observation."""
        response = test_client.get(f"/nodes/{node.id}/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == UNCOLLECTED_NODE_DETAIL

    def test_retrieve_host_system_observation_404_when_node_not_found(
        self, test_client: TestClient
    ) -> None:
        """Return 404 with the default detail when the node ID does not exist."""
        response = test_client.get("/nodes/99999/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == "Not Found"

    def test_retrieve_host_system_observation_404_for_another_nodes_observation(
        self,
        test_client: TestClient,
        node: Node,
        host_observation: HostSystemObservation,
    ) -> None:
        """Keep one node's observation from surfacing when reading a sibling node."""
        other = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert other.status_code == status.HTTP_201_CREATED
        other_node_id = other.json()["id"]
        assert other_node_id != node.id

        response = test_client.get(f"/nodes/{other_node_id}/system-observation")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == UNCOLLECTED_NODE_DETAIL

    def test_retrieve_host_system_observation_after_upsert_returns_200(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Leave the uncollected state once the syncer writes an observation.

        The uncollected 404 must be a transient state, not a sticky one: the same
        GET that reported it has to answer 200 after a PUT lands.
        """
        before = test_client.get(f"/nodes/{node.id}/system-observation")
        assert before.status_code == status.HTTP_404_NOT_FOUND
        assert before.json()["detail"] == UNCOLLECTED_NODE_DETAIL

        payload = HostSystemObservationWriteFactory.build()
        upsert = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert upsert.status_code == status.HTTP_200_OK

        after = test_client.get(f"/nodes/{node.id}/system-observation")
        assert after.status_code == status.HTTP_200_OK
        assert after.json()["node_id"] == node.id
        assert after.json()["os_version"] == payload.os_version


class TestListHostSystemObservations:
    """Test GET /nodes/system-observations endpoint."""

    def test_lists_observations_across_nodes(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return one item per observed node, leaving unobserved nodes out."""
        second = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert second.status_code == status.HTTP_201_CREATED
        second_id = second.json()["id"]
        third = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert third.status_code == status.HTTP_201_CREATED

        for node_id, can_elevate in ((node.id, True), (second_id, False)):
            upsert = test_client.put(
                f"/nodes/{node_id}/system-observation",
                json=HostSystemObservationWriteFactory.build(
                    can_elevate=can_elevate
                ).model_dump(mode="json"),
            )
            assert upsert.status_code == status.HTTP_200_OK

        response = test_client.get("/nodes/system-observations")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == OBSERVED_NODE_COUNT
        by_node = {item["node_id"]: item["can_elevate"] for item in body["items"]}
        assert by_node == {node.id: True, second_id: False}

    def test_omits_the_json_blob_fields(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Keep the two per-node JSON blobs out of a fleet-wide fetch."""
        payload = HostSystemObservationWriteFactory.build()
        assert payload.installed_packages
        upsert = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert upsert.status_code == status.HTTP_200_OK

        response = test_client.get("/nodes/system-observations")

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert set(item) == {"node_id", "can_elevate", "observed_at"}

    @pytest.mark.asyncio
    async def test_does_not_fetch_the_json_blobs_from_the_database(
        self, test_client: TestClient, session: AsyncSession, node: Node
    ) -> None:
        """Keep the two blob columns out of the query, not just out of the response.

        ``response_model`` drops them after the database has already transferred
        and deserialized them, so asserting on the response shape alone would pass
        while every fleet-wide page still carried each node's full package list.
        """
        upsert = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=HostSystemObservationWriteFactory.build().model_dump(mode="json"),
        )
        assert upsert.status_code == status.HTTP_200_OK

        statements: list[str] = []

        def _record(
            conn: object, cursor: object, statement: str, *args: object
        ) -> None:
            statements.append(statement)

        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", _record)
        try:
            response = test_client.get("/nodes/system-observations")
        finally:
            event.remove(bind, "before_cursor_execute", _record)

        assert response.status_code == status.HTTP_200_OK
        selects = [s for s in statements if "hostsystemobservation" in s.lower()]
        assert selects, "no observation query was captured"
        assert not any("installed_packages" in s for s in selects)
        assert not any("config" in s for s in selects)

    def test_is_not_shadowed_by_the_node_id_route(
        self, test_client: TestClient
    ) -> None:
        """Answer the literal path rather than parsing it as a node identifier.

        ``GET /{node_id}`` would claim this path if it were declared first, and
        answer 422 on the unparseable identifier instead of serving the list.
        """
        response = test_client.get("/nodes/system-observations")

        assert response.status_code == status.HTTP_200_OK


class TestUpsertHostSystemObservation:
    """Test PUT /nodes/{node_id}/system-observation endpoint."""

    def test_upsert_creates_new_observation(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Create a new observation when none exists and return 200."""
        payload = HostSystemObservationWriteFactory.build()
        response = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["node_id"] == node.id
        assert data["os_version"] == payload.os_version
        assert "id" in data

    def test_upsert_updates_existing_observation(
        self,
        test_client: TestClient,
        node: Node,
        host_observation: HostSystemObservation,
    ) -> None:
        """Update existing observation in place and return 200 with updated fields."""
        test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        payload = HostSystemObservationWriteFactory.build(os_version="Debian 12")
        response = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["os_version"] == "Debian 12"
        assert response.json()["id"] == host_observation.id

    def test_upsert_idempotent_no_conflict_on_second_put(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 200 on a repeat PUT with the same payload (no unique-constraint error)."""
        payload = HostSystemObservationWriteFactory.build()
        json_payload = payload.model_dump(mode="json")
        first = test_client.put(
            f"/nodes/{node.id}/system-observation", json=json_payload
        )
        assert first.status_code == status.HTTP_200_OK
        second = test_client.put(
            f"/nodes/{node.id}/system-observation", json=json_payload
        )
        assert second.status_code == status.HTTP_200_OK

    def test_upsert_preserves_same_id_across_updates(
        self,
        test_client: TestClient,
        node: Node,
        host_observation: HostSystemObservation,
    ) -> None:
        """Update an existing observation in place on PUT — same DB row, same id."""
        payload = HostSystemObservationWriteFactory.build(os_version="Rocky Linux 9")
        response = test_client.put(
            f"/nodes/{node.id}/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == host_observation.id

    def test_upsert_404_when_node_not_found(self, test_client: TestClient) -> None:
        """Return 404 when the node ID does not exist."""
        payload = HostSystemObservationWriteFactory.build()
        response = test_client.put(
            "/nodes/99999/system-observation",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_upsert_422_missing_observed_at(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 422 when required field observed_at is absent."""
        data = HostSystemObservationWriteFactory.build().model_dump(mode="json")
        del data["observed_at"]
        response = test_client.put(f"/nodes/{node.id}/system-observation", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_upsert_422_when_all_observation_fields_are_none(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Return 422 when every observation field is None.

        The field set is read off the model rather than restated, so a new
        observation field is covered here the moment it is declared.
        """
        base = HostSystemObservationWriteFactory.build()
        data = base.model_dump(mode="json")
        for field_name in HOST_OBSERVATION_FIELD_NAMES:
            data[field_name] = None
        response = test_client.put(f"/nodes/{node.id}/system-observation", json=data)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestRecordNodeSyncHealth:
    """Test the POST /nodes/{node_id}/sync-health endpoint."""

    @pytest.mark.asyncio
    async def test_success_outcome_clears_the_failure_state(
        self, test_client: TestClient, session: AsyncSession, node: Node
    ) -> None:
        """Record the freshness and answer 204."""
        response = test_client.post(
            f"/nodes/{node.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(node)
        assert node.last_synced_at is not None
        assert node.last_sync_error is None
        assert node.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_outcome_opens_the_failure_run(
        self, test_client: TestClient, session: AsyncSession, node: Node
    ) -> None:
        """Record the failure columns and answer 204."""
        response = test_client.post(
            f"/nodes/{node.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(node)
        assert node.last_synced_at is None
        assert node.last_sync_error == "boom"
        assert node.sync_failing_since is not None
        assert node.consecutive_failures == 1

    @pytest.mark.parametrize(
        "body", INVALID_SYNC_HEALTH_BODIES, ids=INVALID_SYNC_HEALTH_BODY_IDS
    )
    def test_rejects_an_inconsistent_or_incomplete_body(
        self, test_client: TestClient, node: Node, body: dict[str, str]
    ) -> None:
        """Refuse every body shape the write model declares invalid."""
        response = test_client.post(f"/nodes/{node.id}/sync-health", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_node_is_not_found(self, test_client: TestClient) -> None:
        """Answer 404 when the addressed node does not exist."""
        response = test_client.post(
            "/nodes/99999/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_retired_node_still_records_the_outcome(
        self, test_client: TestClient, session: AsyncSession, retired_node: Node
    ) -> None:
        """Record the attempt against a node retired concurrently with the sync."""
        response = test_client.post(
            f"/nodes/{retired_node.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(retired_node)
        assert retired_node.consecutive_failures == 1


class TestNodeSyncHealthReads:
    """Test that the node read responses expose the sync-health columns."""

    def test_detail_and_nested_service_expose_the_columns(
        self, test_client: TestClient, node: Node, service: Service
    ) -> None:
        """Carry the sync-health fields on the node and on the service nested inside it."""
        response = test_client.get(f"/nodes/{node.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.keys() >= SYNC_HEALTH_RESPONSE_KEYS
        assert data["services"][0].keys() >= SYNC_HEALTH_RESPONSE_KEYS

    def test_list_items_expose_the_columns(
        self, test_client: TestClient, node: Node
    ) -> None:
        """Carry the sync-health fields on every row of the paginated list."""
        response = test_client.get("/nodes/")

        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert items, "the node fixture should have produced a row to read back"
        assert items[0].keys() >= SYNC_HEALTH_RESPONSE_KEYS

    @pytest.mark.asyncio
    async def test_reported_attempt_is_served_as_the_newest_attempt(
        self, test_client: TestClient, session: AsyncSession, node: Node
    ) -> None:
        """Serve the reported attempt time back on the detail response."""
        posted = test_client.post(
            f"/nodes/{node.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )
        assert posted.status_code == status.HTTP_204_NO_CONTENT
        # The suite shares one session across requests, and the health write
        # bypasses its identity map, so the read would serve the stale instance.
        await session.refresh(node)

        response = test_client.get(f"/nodes/{node.id}")

        assert response.status_code == status.HTTP_200_OK
        served = datetime.fromisoformat(response.json()["newest_attempt_at"])
        assert served == datetime.fromisoformat(SYNC_HEALTH_ATTEMPTED_AT)
