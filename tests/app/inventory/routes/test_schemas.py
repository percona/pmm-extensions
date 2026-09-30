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

"""Define tests for inventory schema routes."""

from datetime import datetime, UTC

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette import status
from starlette.testclient import TestClient

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.inventory.crud import TableManager
from app.inventory.models import Node, Schema, Service, SyncOutcomeEnum, Table
from tests.app.factories import (
    NodeWriteFactory,
    SchemaWriteFactory,
    ServiceWriteFactory,
    TableWriteFactory,
)
from tests.app.inventory.conftest import (
    INVALID_SYNC_HEALTH_BODIES,
    INVALID_SYNC_HEALTH_BODY_IDS,
    sync_health_payload,
    SYNC_HEALTH_RESPONSE_KEYS,
)

SCHEMA_COUNT_WITH_SECOND = 2
OFFSET_BEYOND_TOTAL = 999
LIST_QUERY_MATCH_TOTAL = 2


class TestListSchemas:
    """Test GET /schemas/ endpoint."""

    def test_list_schemas_empty(self, test_client: TestClient) -> None:
        """Return an empty paginated response when no schemas exist."""
        response = test_client.get("/schemas/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    def test_list_schemas_excludes_retired(
        self, test_client: TestClient, retired_schema: Schema
    ) -> None:
        """Omit a retired schema from the default list."""
        response = test_client.get("/schemas/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_schemas_rejects_unknown_sort_key(
        self, test_client: TestClient
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get("/schemas/", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_schemas_multiple(
        self, test_client: TestClient, schema: Schema, service: Service
    ) -> None:
        """Return a list of schemas with tables loaded."""
        second = SchemaWriteFactory.build()
        create_response = test_client.post(
            f"/services/{service.id}/schemas/",
            json=second.model_copy(
                update={"name": f"second_schema_{schema.id}"},
            ).model_dump(),
        )
        assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get("/schemas/")
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert len(data["items"]) == SCHEMA_COUNT_WITH_SECOND
        assert data["total"] == SCHEMA_COUNT_WITH_SECOND
        assert all("tables" in s for s in data["items"])

    def test_list_schemas_custom_offset(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get("/schemas/", params={"offset": OFFSET_BEYOND_TOTAL})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_schemas_custom_limit(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get("/schemas/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_schemas_search_ilike(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return only schemas whose name matches the search case-insensitively."""
        match = SchemaWriteFactory.build(name="AlphaSearchSchema")
        other = SchemaWriteFactory.build(name="OtherSchema")
        test_client.post(
            f"/services/{service.id}/schemas/", json=match.model_dump(mode="json")
        )
        test_client.post(
            f"/services/{service.id}/schemas/", json=other.model_dump(mode="json")
        )

        response = test_client.get("/schemas/", params={"search": "alphasearch"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == match.name

    def test_list_schemas_search_reports_filtered_total(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for suffix in ("a", "b"):
            payload = SchemaWriteFactory.build(name=f"FilterMatchSchema_{suffix}")
            test_client.post(
                f"/services/{service.id}/schemas/",
                json=payload.model_dump(mode="json"),
            )
        other = SchemaWriteFactory.build(name="UnrelatedSchema")
        test_client.post(
            f"/services/{service.id}/schemas/",
            json=other.model_dump(mode="json"),
        )

        response = test_client.get(
            "/schemas/", params={"search": "filtermatchschema", "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    def test_list_schemas_deterministic_ordering_across_pages(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Sort equal names stably across pages via the id tie-breaker.

        Schema names are unique per service, so the shared name is created on a
        second service to produce a name tie in the top-level list.
        """
        shared_name = "SameSortSchema"
        node_response = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert node_response.status_code == status.HTTP_201_CREATED
        other_service_response = test_client.post(
            f"/nodes/{node_response.json()['id']}/services/",
            json=ServiceWriteFactory.build(port=5101).model_dump(mode="json"),
        )
        assert other_service_response.status_code == status.HTTP_201_CREATED
        other_service_id = other_service_response.json()["id"]

        created_ids: list[int] = []
        for service_id in (service.id, other_service_id):
            payload = SchemaWriteFactory.build(name=shared_name)
            create_response = test_client.post(
                f"/services/{service_id}/schemas/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
            created_ids.append(create_response.json()["id"])
        created_ids.sort()

        first_page = test_client.get(
            "/schemas/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            "/schemas/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]

    def test_list_schemas_sort_by_service_id(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Order schemas by the allowlisted ``service_id`` sort key."""
        node_response = test_client.post(
            "/nodes/", json=NodeWriteFactory.build().model_dump(mode="json")
        )
        assert node_response.status_code == status.HTTP_201_CREATED
        other_service_response = test_client.post(
            f"/nodes/{node_response.json()['id']}/services/",
            json=ServiceWriteFactory.build(port=5102).model_dump(mode="json"),
        )
        assert other_service_response.status_code == status.HTTP_201_CREATED
        other_service_id = other_service_response.json()["id"]

        low_service_id, high_service_id = sorted((service.id, other_service_id))
        for service_id, name in (
            (high_service_id, "HighServiceSchema"),
            (low_service_id, "LowServiceSchema"),
        ):
            create_response = test_client.post(
                f"/services/{service_id}/schemas/",
                json=SchemaWriteFactory.build(name=name).model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get(
            "/schemas/",
            params={"sort": "service_id", "search": "ServiceSchema"},
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["service_id"] for item in items] == [
            low_service_id,
            high_service_id,
        ]


class TestRetrieveSchema:
    """Test GET /schemas/{schema_id} endpoint."""

    def test_retrieve_schema(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return schema detail with tables and service."""
        response = test_client.get(f"/schemas/{schema.id}")
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert data["id"] == schema.id
        assert isinstance(data["tables"], list)
        assert len(data["tables"]) == 1
        assert "service" in data
        assert data["service"]["id"] == schema.service_id

    def test_retrieve_schema_not_found(self, test_client: TestClient) -> None:
        """Return 404 for a nonexistent schema."""
        response = test_client.get("/schemas/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_schema_retired_returns_404(
        self, test_client: TestClient, retired_schema: Schema
    ) -> None:
        """Hide a retired schema from the default read."""
        response = test_client.get(f"/schemas/{retired_schema.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_schema_hides_retired_table_nested_in_active_schema(
        self, test_client: TestClient, retired_table: Table
    ) -> None:
        """Drop a retired table from the tables nested in an active schema."""
        response = test_client.get(f"/schemas/{retired_table.schema_id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["tables"] == []


class TestUpdateSchema:
    """Test PUT /schemas/{schema_id} endpoint."""

    def test_update_schema(self, test_client: TestClient, schema: Schema) -> None:
        """Update a schema name successfully."""
        payload = {"name": "updated_schema", "service_id": schema.service_id}
        response = test_client.put(f"/schemas/{schema.id}", json=payload)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "updated_schema"

    def test_update_schema_not_found(
        self, test_client: TestClient, service: Service
    ) -> None:
        """Return 404 for updating a nonexistent schema."""
        payload = {"name": "no_matter", "service_id": service.id}
        response = test_client.put("/schemas/99999", json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_schema_invalid_service_id(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return 400 when service_id references a nonexistent service."""
        payload = {"name": schema.name, "service_id": 99999}
        response = test_client.put(f"/schemas/{schema.id}", json=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid service_id: 99999"

    def test_update_schema_omitting_service_id_preserves_parent(
        self, test_client: TestClient, node: Node, service: Service, schema: Schema
    ) -> None:
        """Apply a partial update that omits service_id, leaving the parent unchanged.

        A second service must exist so the previously-dropped ``None`` FK filter
        would match more than one parent (HTTP 500) rather than silently pass.
        """
        second = test_client.post(
            f"/nodes/{node.id}/services/",
            json=ServiceWriteFactory.build().model_dump(mode="json"),
        )
        assert second.status_code == status.HTTP_201_CREATED
        response = test_client.put(
            f"/schemas/{schema.id}", json={"name": "renamed_schema"}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed_schema"
        assert data["service_id"] == service.id

    def test_update_schema_omitting_service_id_preserves_association(
        self, test_client: TestClient, schema: Schema, service: Service, node: Node
    ) -> None:
        """Partial update without service_id succeeds and leaves the FK unchanged."""
        test_client.post(
            "/services/",
            json=ServiceWriteFactory.build(node_id=node.id).model_dump(mode="json"),
        )
        response = test_client.put(
            f"/schemas/{schema.id}", json={"name": "renamed_schema"}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed_schema"
        assert data["service_id"] == service.id

    def test_update_schema_explicit_null_service_id(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return 400 when service_id is explicitly null on a non-nullable relationship."""
        payload = {"name": schema.name, "service_id": None}
        response = test_client.put(f"/schemas/{schema.id}", json=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid service_id: None"


class TestDeleteSchema:
    """Test DELETE /schemas/{schema_id} endpoint."""

    def test_delete_schema(self, test_client: TestClient, schema: Schema) -> None:
        """Retire a schema and confirm the default read no longer resolves it."""
        response = test_client.delete(f"/schemas/{schema.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        response = test_client.get(f"/schemas/{schema.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_schema_not_found(self, test_client: TestClient) -> None:
        """Return 404 for deleting a nonexistent schema."""
        response = test_client.delete("/schemas/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_schema_retires_tables(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Retire the schema's tables along with it."""
        response = test_client.delete(f"/schemas/{schema.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        assert test_client.get(f"/tables/{table.id}").status_code == (
            status.HTTP_404_NOT_FOUND
        )
        retired = test_client.get(
            f"/tables/{table.id}", params={"include_retired": True}
        )
        assert retired.status_code == status.HTTP_200_OK
        assert retired.json()["retired_at"] is not None


class TestReviveSchema:
    """Test the POST /schemas/{schema_id}/revive endpoint."""

    def test_revive_schema_revives_ancestors(
        self,
        test_client: TestClient,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Revive the schema's service and node, but not its retired tables."""
        assert (
            test_client.delete(f"/nodes/{node.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        response = test_client.post(f"/schemas/{schema.id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        for path, entity_id in (
            ("nodes", node.id),
            ("services", service.id),
            ("schemas", schema.id),
        ):
            assert test_client.get(f"/{path}/{entity_id}").status_code == (
                status.HTTP_200_OK
            )
        assert test_client.get(f"/tables/{table.id}").status_code == (
            status.HTTP_404_NOT_FOUND
        )


class TestListTablesBySchema:
    """Test GET /schemas/{schema_id}/tables/ endpoint."""

    def test_list_tables_by_schema_empty(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return an empty paginated response when schema has no tables."""
        response = test_client.get(f"/schemas/{schema.id}/tables/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_tables_by_schema_excludes_retired(
        self,
        test_client: TestClient,
        retired_table: Table,
        params: dict[str, bool],
    ) -> None:
        """Omit a retired table from an active schema's tables."""
        response = test_client.get(
            f"/schemas/{retired_table.schema_id}/tables/", params=params
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_tables_by_schema_include_retired_resolves_retired_schema(
        self, test_client: TestClient, table: Table, retired_schema: Schema
    ) -> None:
        """List a retired schema's tables through the opt-in."""
        response = test_client.get(
            f"/schemas/{retired_schema.id}/tables/", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [item["id"] for item in data["items"]] == [table.id]
        assert data["total"] == 1

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": False}], ids=["omitted", "false"]
    )
    def test_list_tables_by_schema_hides_retired_schema_by_default(
        self,
        test_client: TestClient,
        table: Table,
        retired_schema: Schema,
        params: dict[str, bool],
    ) -> None:
        """Return 404 for a retired schema unless the opt-in is set."""
        response = test_client.get(
            f"/schemas/{retired_schema.id}/tables/", params=params
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_tables_by_schema_include_retired_after_retire_route(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """List the tables the retire route cascaded into, marked retired."""
        assert (
            test_client.delete(f"/schemas/{schema.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        hidden = test_client.get(f"/schemas/{schema.id}/tables/")
        assert hidden.status_code == status.HTTP_404_NOT_FOUND

        response = test_client.get(
            f"/schemas/{schema.id}/tables/", params={"include_retired": True}
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["id"] for item in items] == [table.id]
        assert items[0]["retired_at"] is not None

    def test_list_tables_by_schema_include_retired_on_active_schema(
        self, test_client: TestClient, retired_table: Table
    ) -> None:
        """Include a retired table of an active schema through the opt-in."""
        response = test_client.get(
            f"/schemas/{retired_table.schema_id}/tables/",
            params={"include_retired": True},
        )
        assert response.status_code == status.HTTP_200_OK
        assert [item["id"] for item in response.json()["items"]] == [retired_table.id]

    def test_list_tables_by_schema_rejects_invalid_include_retired(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Reject a non-boolean include_retired with HTTP 422."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/", params={"include_retired": "maybe"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_tables_by_schema_rejects_unknown_sort_key(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"sort": "evil"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_tables_by_schema(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return tables belonging to the schema."""
        response = test_client.get(f"/schemas/{schema.id}/tables/")
        assert response.status_code == status.HTTP_200_OK

        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["items"][0]["id"] == table.id

    def test_list_tables_by_schema_search(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return only tables whose name matches the search case-insensitively."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"search": table.name[:3]},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == table.id

    def test_list_tables_by_schema_search_no_match(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return empty paginated response when search does not match any table."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"search": "nonexistent_table_xyz"},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.parametrize(
        "params", [{}, {"include_retired": True}], ids=["active", "include_retired"]
    )
    def test_list_tables_by_schema_not_found(
        self, test_client: TestClient, params: dict[str, bool]
    ) -> None:
        """Return 404 for a nonexistent schema in either retirement scope."""
        response = test_client.get("/schemas/99999/tables/", params=params)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_tables_by_schema_custom_offset(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/", params={"offset": OFFSET_BEYOND_TOTAL}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_tables_by_schema_custom_limit(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get(f"/schemas/{schema.id}/tables/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == 1
        assert data["limit"] == 1

    def test_list_tables_by_schema_search_with_pagination(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return search-filtered total with pagination params."""
        response = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"search": table.name[:3], "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] >= 1
        assert len(data["items"]) <= 1

    def test_list_tables_by_schema_search_reports_filtered_total(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for suffix in ("a", "b"):
            payload = TableWriteFactory.build(name=f"FilterMatchTable_{suffix}")
            create_response = test_client.post(
                f"/schemas/{schema.id}/tables/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
        other = TableWriteFactory.build(name="UnrelatedTable")
        test_client.post(
            f"/schemas/{schema.id}/tables/",
            json=other.model_dump(mode="json"),
        )

        response = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"search": "filtermatchtable", "limit": 1},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_list_tables_by_schema_deterministic_ordering_across_pages(
        self, test_client: TestClient, session: AsyncSession, schema: Schema
    ) -> None:
        """Sort equal created_at stably across pages via the id tie-breaker.

        Table names are unique per schema, so name ties cannot occur in this
        nested list; exercise the tie-breaker on created_at instead.
        """
        shared_created_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        created_ids: list[int] = []
        for name in ("tie_table_a", "tie_table_b"):
            row = await TableManager.create(
                session,
                TableWriteFactory.build(name=name),
                schema_id=schema.id,
            )
            row.created_at = shared_created_at
            session.add(row)
            await session.commit()
            await session.refresh(row)
            created_ids.append(row.id)
        created_ids.sort()

        first_page = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"sort": "created_at", "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            f"/schemas/{schema.id}/tables/",
            params={"sort": "created_at", "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]


class TestCreateTableForSchema:
    """Test POST /schemas/{schema_id}/tables/ endpoint."""

    def test_create_table_for_schema(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Create a table under a schema successfully."""
        payload = TableWriteFactory.build().model_dump()
        response = test_client.post(f"/schemas/{schema.id}/tables/", json=payload)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["name"] == payload["name"]
        assert response.json()["schema_id"] == schema.id

    def test_create_table_for_schema_duplicate_name(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Return 409 when creating a table with a duplicate name in the same schema."""
        payload = {"name": table.name, "create": "CREATE TABLE t(id INT)", "keys": {}}
        response = test_client.post(f"/schemas/{schema.id}/tables/", json=payload)
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_create_table_for_schema_not_found(self, test_client: TestClient) -> None:
        """Return 404 when creating a table under a nonexistent schema."""
        payload = TableWriteFactory.build().model_dump()
        response = test_client.post("/schemas/99999/tables/", json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_table_for_retired_schema_ignores_include_retired(
        self, test_client: TestClient, retired_schema: Schema
    ) -> None:
        """Refuse a table under a retired schema, whatever the read opt-in says."""
        payload = TableWriteFactory.build().model_dump()
        response = test_client.post(
            f"/schemas/{retired_schema.id}/tables/",
            params={"include_retired": True},
            json=payload,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestRecordSchemaSyncHealth:
    """Test the POST /schemas/{schema_id}/sync-health endpoint."""

    @pytest.mark.asyncio
    async def test_success_outcome_clears_the_failure_state(
        self, test_client: TestClient, session: AsyncSession, schema: Schema
    ) -> None:
        """Record the freshness and answer 204."""
        response = test_client.post(
            f"/schemas/{schema.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(schema)
        assert schema.last_synced_at is not None
        assert schema.last_sync_error is None
        assert schema.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_outcome_opens_the_failure_run(
        self, test_client: TestClient, session: AsyncSession, schema: Schema
    ) -> None:
        """Record the failure columns and answer 204."""
        response = test_client.post(
            f"/schemas/{schema.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(schema)
        assert schema.last_synced_at is None
        assert schema.last_sync_error == "boom"
        assert schema.sync_failing_since is not None
        assert schema.consecutive_failures == 1

    @pytest.mark.parametrize(
        "body", INVALID_SYNC_HEALTH_BODIES, ids=INVALID_SYNC_HEALTH_BODY_IDS
    )
    def test_rejects_an_inconsistent_or_incomplete_body(
        self, test_client: TestClient, schema: Schema, body: dict[str, str]
    ) -> None:
        """Refuse every body shape the write model declares invalid."""
        response = test_client.post(f"/schemas/{schema.id}/sync-health", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_schema_is_not_found(self, test_client: TestClient) -> None:
        """Answer 404 when the addressed schema does not exist."""
        response = test_client.post(
            "/schemas/99999/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_retired_schema_still_records_the_outcome(
        self,
        test_client: TestClient,
        session: AsyncSession,
        retired_schema: Schema,
    ) -> None:
        """Record the attempt against a schema retired concurrently with the sync."""
        response = test_client.post(
            f"/schemas/{retired_schema.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(retired_schema)
        assert retired_schema.consecutive_failures == 1


class TestSchemaSyncHealthReads:
    """Test that the schema read responses expose the sync-health columns."""

    def test_detail_exposes_the_columns(
        self, test_client: TestClient, schema: Schema, table: Table
    ) -> None:
        """Carry the sync-health fields on the schema detail response.

        The nested tables carry them through the table model the response
        nests, so a table read from inside a schema reports the same state.
        """
        response = test_client.get(f"/schemas/{schema.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.keys() >= SYNC_HEALTH_RESPONSE_KEYS
        assert data["tables"][0].keys() >= SYNC_HEALTH_RESPONSE_KEYS

    def test_list_items_expose_the_columns(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Carry the sync-health fields on every row of the paginated list."""
        response = test_client.get("/schemas/")

        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert items, "the schema fixture should have produced a row to read back"
        assert items[0].keys() >= SYNC_HEALTH_RESPONSE_KEYS
