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

"""Define tests for inventory table routes."""

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette import status
from starlette.testclient import TestClient

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.inventory.models import Schema, Service, SyncOutcomeEnum, Table
from tests.app.factories import SchemaWriteFactory, TableWriteFactory
from tests.app.inventory.conftest import (
    INVALID_SYNC_HEALTH_BODIES,
    INVALID_SYNC_HEALTH_BODY_IDS,
    sync_health_payload,
    SYNC_HEALTH_RESPONSE_KEYS,
)

EXPECTED_TABLE_COUNT = 2
OFFSET_BEYOND_TOTAL = 999
LIST_QUERY_MATCH_TOTAL = 2


class TestListTables:
    """Test the GET /tables/ endpoint."""

    def test_list_tables_empty(self, test_client: TestClient) -> None:
        """Return an empty paginated response when no tables exist."""
        response = test_client.get("/tables/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT

    def test_list_tables_excludes_retired(
        self, test_client: TestClient, retired_table: Table
    ) -> None:
        """Omit a retired table from the default list."""
        response = test_client.get("/tables/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_tables_rejects_unknown_sort_key(
        self, test_client: TestClient
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = test_client.get("/tables/", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_list_tables_multiple(
        self, test_client: TestClient, table: Table, second_table: Table
    ) -> None:
        """Return a list of all tables."""
        response = test_client.get("/tables/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == EXPECTED_TABLE_COUNT
        assert data["total"] == EXPECTED_TABLE_COUNT
        returned_ids = {t["id"] for t in data["items"]}
        assert returned_ids == {table.id, second_table.id}

    def test_list_tables_custom_offset(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Return empty items when offset is beyond total."""
        response = test_client.get("/tables/", params={"offset": OFFSET_BEYOND_TOTAL})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 1
        assert data["offset"] == OFFSET_BEYOND_TOTAL

    def test_list_tables_custom_limit(
        self, test_client: TestClient, table: Table, second_table: Table
    ) -> None:
        """Return limited items while total remains unchanged."""
        response = test_client.get("/tables/", params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["total"] == EXPECTED_TABLE_COUNT
        assert data["limit"] == 1

    def test_list_tables_search_ilike(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return only tables whose name matches the search case-insensitively."""
        match = TableWriteFactory.build(name="AlphaSearchTable")
        other = TableWriteFactory.build(name="OtherTable")
        test_client.post(
            f"/schemas/{schema.id}/tables/", json=match.model_dump(mode="json")
        )
        test_client.post(
            f"/schemas/{schema.id}/tables/", json=other.model_dump(mode="json")
        )

        response = test_client.get("/tables/", params={"search": "alphasearch"})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == match.name

    def test_list_tables_search_reports_filtered_total(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        for suffix in ("a", "b"):
            payload = TableWriteFactory.build(name=f"FilterMatchTable_{suffix}")
            test_client.post(
                f"/schemas/{schema.id}/tables/",
                json=payload.model_dump(mode="json"),
            )
        other = TableWriteFactory.build(name="UnrelatedTable")
        test_client.post(
            f"/schemas/{schema.id}/tables/",
            json=other.model_dump(mode="json"),
        )

        response = test_client.get(
            "/tables/", params={"search": "filtermatchtable", "limit": 1}
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == LIST_QUERY_MATCH_TOTAL
        assert len(data["items"]) == 1

    def test_list_tables_deterministic_ordering_across_pages(
        self, test_client: TestClient, schema: Schema, service: Service
    ) -> None:
        """Sort equal names stably across pages via the id tie-breaker.

        Table names are unique per schema, so the shared name is created on a
        second schema to produce a name tie in the top-level list.
        """
        shared_name = "SameSortTable"
        other_schema_response = test_client.post(
            f"/services/{service.id}/schemas/",
            json=SchemaWriteFactory.build(name="other_same_sort_schema").model_dump(
                mode="json"
            ),
        )
        assert other_schema_response.status_code == status.HTTP_201_CREATED
        other_schema_id = other_schema_response.json()["id"]

        created_ids: list[int] = []
        for schema_id in (schema.id, other_schema_id):
            payload = TableWriteFactory.build(name=shared_name)
            create_response = test_client.post(
                f"/schemas/{schema_id}/tables/",
                json=payload.model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED
            created_ids.append(create_response.json()["id"])
        created_ids.sort()

        first_page = test_client.get(
            "/tables/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 0},
        )
        second_page = test_client.get(
            "/tables/",
            params={"sort": "name", "search": shared_name, "limit": 1, "offset": 1},
        )
        assert first_page.status_code == status.HTTP_200_OK
        assert second_page.status_code == status.HTTP_200_OK
        assert first_page.json()["items"][0]["id"] == created_ids[0]
        assert second_page.json()["items"][0]["id"] == created_ids[1]

    def test_list_tables_sort_by_schema_id(
        self, test_client: TestClient, schema: Schema, service: Service
    ) -> None:
        """Order tables by the allowlisted ``schema_id`` sort key."""
        other_schema_response = test_client.post(
            f"/services/{service.id}/schemas/",
            json=SchemaWriteFactory.build(name="other_schema_id_sort").model_dump(
                mode="json"
            ),
        )
        assert other_schema_response.status_code == status.HTTP_201_CREATED
        other_schema_id = other_schema_response.json()["id"]

        low_schema_id, high_schema_id = sorted((schema.id, other_schema_id))
        for schema_id, name in (
            (high_schema_id, "HighSchemaTable"),
            (low_schema_id, "LowSchemaTable"),
        ):
            create_response = test_client.post(
                f"/schemas/{schema_id}/tables/",
                json=TableWriteFactory.build(name=name).model_dump(mode="json"),
            )
            assert create_response.status_code == status.HTTP_201_CREATED

        response = test_client.get(
            "/tables/",
            params={"sort": "schema_id", "search": "SchemaTable"},
        )
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert [item["schema_id"] for item in items] == [low_schema_id, high_schema_id]


class TestRetrieveTable:
    """Test the GET /tables/{table_id} endpoint."""

    def test_retrieve_table(
        self, test_client: TestClient, table: Table, schema: Schema
    ) -> None:
        """Return the table with its parent schema in the database field."""
        response = test_client.get(f"/tables/{table.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == table.id
        assert data["name"] == table.name
        assert "database" in data
        assert data["database"]["id"] == schema.id

    def test_retrieve_table_not_found(self, test_client: TestClient) -> None:
        """Return 404 when the table does not exist."""
        response = test_client.get("/tables/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_retrieve_table_retired_returns_404(
        self, test_client: TestClient, retired_table: Table
    ) -> None:
        """Hide a retired table from the default read."""
        response = test_client.get(f"/tables/{retired_table.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestUpdateTable:
    """Test the PUT /tables/{table_id} endpoint."""

    def test_update_table(
        self, test_client: TestClient, table: Table, schema: Schema
    ) -> None:
        """Update the table name and return the updated table."""
        payload = TableWriteFactory.build(
            name="updated_table_name",
            create=table.create,
            keys=table.keys,
            schema_id=schema.id,
        ).model_dump(mode="json")
        response = test_client.put(f"/tables/{table.id}", json=payload)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "updated_table_name"

    def test_update_table_not_found(
        self, test_client: TestClient, schema: Schema
    ) -> None:
        """Return 404 when updating a nonexistent table."""
        payload = TableWriteFactory.build(schema_id=schema.id).model_dump(mode="json")
        response = test_client.put("/tables/99999", json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_table_invalid_schema_id(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Return 400 when schema_id references a nonexistent schema."""
        payload = TableWriteFactory.build(schema_id=99999).model_dump(mode="json")
        response = test_client.put(f"/tables/{table.id}", json=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid schema_id: 99999"

    def test_update_table_omitting_schema_id_preserves_parent(
        self, test_client: TestClient, service: Service, schema: Schema, table: Table
    ) -> None:
        """Apply a partial update that omits schema_id, leaving the parent unchanged.

        A second schema must exist so the previously-dropped ``None`` FK filter
        would match more than one parent (HTTP 500) rather than silently pass.
        """
        second = test_client.post(
            f"/services/{service.id}/schemas/",
            json=SchemaWriteFactory.build(name=f"second_schema_{schema.id}").model_dump(
                mode="json"
            ),
        )
        assert second.status_code == status.HTTP_201_CREATED
        response = test_client.put(
            f"/tables/{table.id}",
            json={
                "name": "renamed_table",
                "create": table.create,
                "keys": table.keys,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed_table"
        assert data["schema_id"] == schema.id

    def test_update_table_omitting_schema_id_preserves_association(
        self, test_client: TestClient, table: Table, schema: Schema, service: Service
    ) -> None:
        """Partial update without schema_id succeeds and leaves the FK unchanged."""
        test_client.post(
            "/schemas/",
            json=SchemaWriteFactory.build(service_id=service.id).model_dump(
                mode="json"
            ),
        )
        payload = {
            "name": "renamed_table",
            "create": table.create,
            "keys": table.keys,
        }
        response = test_client.put(f"/tables/{table.id}", json=payload)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "renamed_table"
        assert data["schema_id"] == schema.id

    def test_update_table_explicit_null_schema_id(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Return 400 when schema_id is explicitly null on a non-nullable relationship."""
        payload = {
            "name": table.name,
            "create": table.create,
            "keys": table.keys,
            "schema_id": None,
        }
        response = test_client.put(f"/tables/{table.id}", json=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Invalid schema_id: None"


class TestDeleteTable:
    """Test the DELETE /tables/{table_id} endpoint."""

    def test_delete_table(self, test_client: TestClient, table: Table) -> None:
        """Retire a table and confirm the default read no longer resolves it."""
        response = test_client.delete(f"/tables/{table.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

        response = test_client.get(f"/tables/{table.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_table_not_found(self, test_client: TestClient) -> None:
        """Return 404 when deleting a nonexistent table."""
        response = test_client.delete("/tables/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_table_keeps_the_row(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Keep a retired table resolvable through the opt-in."""
        assert (
            test_client.delete(f"/tables/{table.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )
        retired = test_client.get(
            f"/tables/{table.id}", params={"include_retired": True}
        )
        assert retired.status_code == status.HTTP_200_OK
        assert retired.json()["id"] == table.id
        assert retired.json()["retired_at"] is not None


class TestReviveTable:
    """Test the POST /tables/{table_id}/revive endpoint."""

    def test_revive_table_revives_ancestors(
        self,
        test_client: TestClient,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Revive the table's schema, service and node along with it."""
        assert (
            test_client.delete(f"/services/{service.id}").status_code
            == status.HTTP_204_NO_CONTENT
        )

        response = test_client.post(f"/tables/{table.id}/revive")
        assert response.status_code == status.HTTP_204_NO_CONTENT
        for path, entity_id in (
            ("services", service.id),
            ("schemas", schema.id),
            ("tables", table.id),
        ):
            assert test_client.get(f"/{path}/{entity_id}").status_code == (
                status.HTTP_200_OK
            )


class TestRecordTableSyncHealth:
    """Test the POST /tables/{table_id}/sync-health endpoint."""

    @pytest.mark.asyncio
    async def test_success_outcome_clears_the_failure_state(
        self, test_client: TestClient, session: AsyncSession, table: Table
    ) -> None:
        """Record the freshness and answer 204."""
        response = test_client.post(
            f"/tables/{table.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(table)
        assert table.last_synced_at is not None
        assert table.last_sync_error is None
        assert table.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_outcome_opens_the_failure_run(
        self, test_client: TestClient, session: AsyncSession, table: Table
    ) -> None:
        """Record the failure columns and answer 204."""
        response = test_client.post(
            f"/tables/{table.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(table)
        assert table.last_synced_at is None
        assert table.last_sync_error == "boom"
        assert table.sync_failing_since is not None
        assert table.consecutive_failures == 1

    @pytest.mark.parametrize(
        "body", INVALID_SYNC_HEALTH_BODIES, ids=INVALID_SYNC_HEALTH_BODY_IDS
    )
    def test_rejects_an_inconsistent_or_incomplete_body(
        self, test_client: TestClient, table: Table, body: dict[str, str]
    ) -> None:
        """Refuse every body shape the write model declares invalid."""
        response = test_client.post(f"/tables/{table.id}/sync-health", json=body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_table_is_not_found(self, test_client: TestClient) -> None:
        """Answer 404 when the addressed table does not exist."""
        response = test_client.post(
            "/tables/99999/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_retired_table_still_records_the_outcome(
        self,
        test_client: TestClient,
        session: AsyncSession,
        retired_table: Table,
    ) -> None:
        """Record the attempt against a table retired concurrently with the sync."""
        response = test_client.post(
            f"/tables/{retired_table.id}/sync-health",
            json=sync_health_payload(SyncOutcomeEnum.FAILURE, "boom"),
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        await session.refresh(retired_table)
        assert retired_table.consecutive_failures == 1


class TestTableSyncHealthReads:
    """Test that the table read responses expose the sync-health columns."""

    def test_detail_exposes_the_columns(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Carry the sync-health fields on the table detail response."""
        response = test_client.get(f"/tables/{table.id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.keys() >= SYNC_HEALTH_RESPONSE_KEYS

    def test_list_items_expose_the_columns(
        self, test_client: TestClient, table: Table
    ) -> None:
        """Carry the sync-health fields on every row of the paginated list."""
        response = test_client.get("/tables/")

        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert items, "the table fixture should have produced a row to read back"
        assert items[0].keys() >= SYNC_HEALTH_RESPONSE_KEYS
