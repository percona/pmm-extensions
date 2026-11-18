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

"""Test the app.extensions.sync.syncers.mysql.syncer module.

These tests drive a real ``MySQLSyncer`` through its public surface against canned
``RemoteAPI`` responses. The orchestration methods (``perform_*_sync``) and their
real downstream cascade (``sync_*`` / ``retire_*`` / ``update_*`` / ``get_inventory_*``)
are exercised end-to-end and asserted behaviourally on the ``inventory_api`` traffic,
on ``_inventory_index_cache`` state, and on the resulting ``SyncItem`` lifecycle —
never by patching the subject class. External boundaries (e.g. ``wait_for_task_output``,
the ``/hosts/`` lookup behind ``get_available_hosts``, and low-level stream/decompression
helpers) are stubbed, at the ``RemoteAPI`` seam where possible.
"""

import gzip
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.core.alerts.config import alert_service
from app.core.utils.date_time import utc_now
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
    Node,
    Service,
    Table,
)
from app.extensions.models import (
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.exceptions import ExecutorHostNotFoundError
from app.extensions.sync.models import TaskRunResult
from app.extensions.sync.syncers.mysql.syncer import (
    _MySQLSyncResultEntityTypeEnum,
    MySQLSchema,
    MySQLService,
    MySQLSyncer,
)
from app.inventory.models import ServiceTypeEnum
from tests.app.extensions.sync.conftest import entity_posts, sync_health_posts
from tests.app.factories import (
    CreatedNodeFactory,
    CreatedSchemaFactory,
    CreatedServiceFactory,
    CreatedTableFactory,
    MOCK_CREATED_NODE_ID,
)
from tests.app.scan_recording import ScanRecordingBytearray

# Pinned verbatim rather than imported: this syncer opting into tombstones is what
# lets it match a reappearing entity, so losing the parameter must fail the test.
RETIRED_INCLUSIVE_PARAMS = {"include_retired": "true"}


@pytest.fixture
def mock_mysql_syncer(mock_remote_api) -> MySQLSyncer:
    """Test fixture: return a MySQLSyncer with mocked APIs."""
    return MySQLSyncer(  # ty: ignore[missing-argument]
        tasks_api=mock_remote_api, inventory_api=mock_remote_api
    )


@pytest_asyncio.fixture
async def bound_mysql_syncer(session, mock_remote_api) -> MySQLSyncer:
    """Return a MySQLSyncer bound to a real sqlite session and persisted SyncInstance.

    Cascade tests whose subject method reaches base ``manage_sync_item`` (which needs a
    live session and a ``SyncInstance``) use this fixture, mirroring the
    ``_build_syncer`` pattern in ``tests/app/extensions/sync/test_models.py``.
    """
    sync_instance = await SyncInstanceManager.create(
        session,
        SyncInstanceWrite(syncer=MySQLSyncer.get_name()),
    )
    syncer = MySQLSyncer(  # ty: ignore[missing-argument]
        tasks_api=mock_remote_api,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )
    syncer._session = session
    return syncer


async def _seed_sync_item(
    syncer: MySQLSyncer,
    session,
    entity_type: SyncInventoryEntityTypeEnum,
    entity_id: int | None,
) -> None:
    """Pre-seed a SyncItem so ``manage_sync_item`` skips the ``prepare_sync`` recursion.

    Without this, ``prepare_sync`` traverses children via ``get_inventory_service`` and
    injects stray ``inventory_api`` GET calls that pollute the behavioural assertions.
    """
    sync_item, _ = await SyncItemManager.get_or_create(
        session,
        SyncItemWrite(
            entity_type=entity_type,
            entity_id=entity_id,
            sync_instance_id=syncer.sync_instance.id,
        ),
    )
    syncer.sync_items[(entity_type, entity_id)] = sync_item


@pytest.fixture
def created_service() -> CreatedService:
    """Test fixture: return a fake created service."""
    created_service = CreatedServiceFactory.build()
    created_service.node_id = MOCK_CREATED_NODE_ID
    created_service.type = ServiceTypeEnum.MYSQL
    created_service.node = CreatedNode(
        address="localhost",
        id=MOCK_CREATED_NODE_ID,
        node_id="/node_id/mock-1",
        node_name="localhost",
    )
    created_service.port = 8000
    created_service.schemas = []
    return created_service


@pytest.fixture
def created_node(created_service) -> CreatedNode:
    """Test fixture: return a fake created node."""
    created_node = CreatedNodeFactory.build()
    created_node.address = "localhost:8000"
    created_node.services = [created_service]
    return created_node


@pytest.fixture
def created_schema(created_service) -> CreatedSchema:
    """Test fixture: return a fake created schema."""
    created_schema = CreatedSchemaFactory.build(name="test_schema")
    created_schema.service = created_service
    created_schema.tables = []
    return created_schema


@pytest.fixture
def created_table(created_schema) -> CreatedTable:
    """Test fixture: return a fake created table."""
    created_table = CreatedTableFactory.build(name="test_table")
    created_table.database = created_schema
    return created_table


class TestConfigAndTargets:
    """Test script config building and target resolution."""

    @pytest.mark.asyncio
    async def test_build_script_config(self, mock_mysql_syncer):
        """Test building script config with schema and table."""
        cfg = mock_mysql_syncer.build_script_config(
            "127.0.0.1", "192.168.1.10", schema="test_schema", table="test_table"
        )
        data = json.loads(cfg)
        assert data["hosts"] == ["127.0.0.1", "192.168.1.10"]
        assert data["ignore_schemas"] == []
        assert data["resolve_localhost"] is True
        assert data["schema"] == "test_schema"
        assert data["table"] == "test_table"

    @pytest.mark.asyncio
    async def test_get_task_target(self, mock_mysql_syncer):
        """Test resolving forced executor host as target."""
        mock_mysql_syncer.force_executor_host = "some_executor_host"
        target = await mock_mysql_syncer.get_task_target("127.0.0.1")
        assert target == "some_executor_host"
        # force short-circuits before any /hosts/ lookup.
        mock_mysql_syncer.tasks_api.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_task_target_default_executor_fallback(self, mock_mysql_syncer):
        """Test fallback to default_executor_host when no name/address match (e.g. RDS)."""
        mock_mysql_syncer.tasks_api.get.return_value = {
            "on-prem-host": "192.168.1.10:3306",
            "monitor-host": "10.0.0.5:3306",
        }
        mock_mysql_syncer.force_executor_host = None
        mock_mysql_syncer.default_executor_host = "monitor-host"
        target = await mock_mysql_syncer.get_task_target(
            "rds-cluster.region.rds.amazonaws.com"
        )
        assert target == "monitor-host"

    @pytest.mark.asyncio
    async def test_get_task_target_first_available_when_no_default(
        self, mock_mysql_syncer
    ):
        """Test first available host when no match and default_executor_host not set."""
        mock_mysql_syncer.tasks_api.get.return_value = {
            "first-host": "1.2.3.4:3306",
            "second-host": "5.6.7.8:3306",
        }
        mock_mysql_syncer.force_executor_host = None
        mock_mysql_syncer.default_executor_host = None
        target = await mock_mysql_syncer.get_task_target("unknown-rds.endpoint.com")
        assert target == "first-host"

    @pytest.mark.asyncio
    async def test_get_task_target_force_takes_precedence_over_default(
        self, mock_mysql_syncer
    ):
        """Test force_executor_host overrides default_executor_host."""
        mock_mysql_syncer.force_executor_host = "forced-host"
        mock_mysql_syncer.default_executor_host = "default-host"
        target = await mock_mysql_syncer.get_task_target("any-host")
        assert target == "forced-host"
        mock_mysql_syncer.tasks_api.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_task_target_fallback_when_default_not_in_available_hosts(
        self, mock_mysql_syncer
    ):
        """Test fallback to first available when default_executor_host is stale."""
        mock_mysql_syncer.tasks_api.get.return_value = {
            "live-host": "10.0.0.1:3306",
            "other-host": "10.0.0.2:3306",
        }
        mock_mysql_syncer.force_executor_host = None
        mock_mysql_syncer.default_executor_host = "stale-host"
        target = await mock_mysql_syncer.get_task_target("rds.example.com")
        assert target == "live-host"

    def test_payload_path_points_to_payload_py(self, mock_mysql_syncer):
        """Test payload_path returns payload.py path."""
        p = mock_mysql_syncer.payload_path
        assert p.name == "payload.py"
        assert str(p).endswith("/payload.py")

    def test_build_entity_address_service_only(self):
        """Test returning service address when only service is provided."""
        addr = MySQLSyncer._build_entity_address("host:3306")
        assert addr == "host:3306"

    @pytest.mark.asyncio
    async def test_get_task_target_strict_raises_on_no_match(self, mock_remote_api):
        """Assert ``ExecutorHostNotFoundError`` is raised when strict and no match."""
        syncer = MySQLSyncer(  # ty: ignore[missing-argument]
            tasks_api=mock_remote_api,
            inventory_api=mock_remote_api,
            strict_executor_matching=True,
        )
        mock_remote_api.get.return_value = {
            "executor-1": "10.0.0.1",
            "executor-2": "10.0.0.2",
        }
        with pytest.raises(ExecutorHostNotFoundError) as exc_info:
            await syncer.get_task_target("192.168.1.99", name="unknown-node")
        assert exc_info.value.node_name == "unknown-node"
        assert exc_info.value.node_address == "192.168.1.99"
        assert exc_info.value.available_hosts == {
            "executor-1": "10.0.0.1",
            "executor-2": "10.0.0.2",
        }

    @pytest.mark.asyncio
    async def test_get_task_target_strict_name_matches(self, mock_remote_api):
        """Assert matched name is returned when strict and name is in hosts."""
        syncer = MySQLSyncer(  # ty: ignore[missing-argument]
            tasks_api=mock_remote_api,
            inventory_api=mock_remote_api,
            strict_executor_matching=True,
        )
        mock_remote_api.get.return_value = {
            "my-node": "10.0.0.1",
            "executor-2": "10.0.0.2",
        }
        target = await syncer.get_task_target("192.168.1.99", name="my-node")
        assert target == "my-node"

    @pytest.mark.asyncio
    async def test_get_task_target_strict_address_matches(self, mock_remote_api):
        """Assert matched target is returned when strict and address matches."""
        syncer = MySQLSyncer(  # ty: ignore[missing-argument]
            tasks_api=mock_remote_api,
            inventory_api=mock_remote_api,
            strict_executor_matching=True,
        )
        mock_remote_api.get.return_value = {
            "executor-1": "192.168.1.99",
            "executor-2": "10.0.0.2",
        }
        target = await syncer.get_task_target("192.168.1.99", name="unknown")
        assert target == "executor-1"

    @pytest.mark.asyncio
    async def test_get_task_target_non_strict_fallback(self, mock_remote_api):
        """Assert first host is returned as fallback when non-strict and no match."""
        syncer = MySQLSyncer(  # ty: ignore[missing-argument]
            tasks_api=mock_remote_api,
            inventory_api=mock_remote_api,
        )
        mock_remote_api.get.return_value = {
            "executor-1": "10.0.0.1",
            "executor-2": "10.0.0.2",
        }
        target = await syncer.get_task_target("192.168.1.99", name="unknown")
        assert target == "executor-1"

    @pytest.mark.asyncio
    async def test_get_task_target_force_overrides_strict(self, mock_remote_api):
        """Assert ``force_executor_host`` takes priority over strict matching."""
        syncer = MySQLSyncer(  # ty: ignore[missing-argument]
            tasks_api=mock_remote_api,
            inventory_api=mock_remote_api,
            strict_executor_matching=True,
            force_executor_host="forced-host",
        )
        target = await syncer.get_task_target("192.168.1.99", name="unknown")
        assert target == "forced-host"
        # force short-circuits: the /hosts/ lookup must never run.
        mock_remote_api.get.assert_not_called()


class TestFetchMethods:
    """Test fetch methods for node, service, schema, and table."""

    @pytest.mark.asyncio
    async def test_fetch_node(self, created_node, mock_mysql_syncer, mocker):
        """Test fetching node with services index."""
        mock_mysql_syncer.tasks_api.get.return_value = {"localhost:8000": "hostname"}
        services_manifest = {
            _MySQLSyncResultEntityTypeEnum.SERVICES: {
                "localhost:8000": {
                    "schemas_path": "out/schemas.ndjson.gz",
                    "schemas_count": 1,
                }
            }
        }
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(123, json.dumps(services_manifest))
        updated_node = await mock_mysql_syncer.fetch_node(created_node)
        assert isinstance(updated_node, Node)
        assert len(updated_node.services) == 1
        assert isinstance(updated_node.services[0], Service)

    @pytest.mark.asyncio
    async def test_fetch_service(self, created_service, mock_mysql_syncer, mocker):
        """Test fetching service to return schemas index iterator."""
        mock_mysql_syncer.tasks_api.get.return_value = {"localhost:8000": "hostname"}
        services_manifest = {
            _MySQLSyncResultEntityTypeEnum.SERVICES: {
                "localhost:8000": {
                    "schemas_path": "out/localhost%3A8000/schemas.ndjson.gz",
                    "schemas_count": 1,
                }
            }
        }
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(456, json.dumps(services_manifest))
        updated_service = await mock_mysql_syncer.fetch_service(created_service)
        assert isinstance(updated_service, MySQLService)
        assert updated_service.schemas_index is not None

    @pytest.mark.asyncio
    async def test_fetch_schema(self, created_schema, mock_mysql_syncer, mocker):
        """Test fetching schema to return tables iterator."""
        mock_mysql_syncer.tasks_api.get.return_value = {"localhost:8000": "hostname"}
        schema_address = "localhost:8000/test_schema"
        schemas_manifest = {
            _MySQLSyncResultEntityTypeEnum.SCHEMAS: {
                schema_address: {
                    "tables_path": "out/localhost%3A8000/test_schema_tables.ndjson.gz",
                    "tables_count": 1,
                }
            }
        }
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(789, json.dumps(schemas_manifest))
        updated_schema = await mock_mysql_syncer.fetch_schema(created_schema)
        assert isinstance(updated_schema, MySQLSchema)
        assert updated_schema.tables_aiter is not None

    @pytest.mark.asyncio
    async def test_fetch_table(self, created_table, mock_mysql_syncer, mocker):
        """Test fetching table from tables payload."""
        mock_mysql_syncer.tasks_api.get.return_value = {"localhost:8000": "hostname"}
        table_key = f"localhost:8000/{created_table.database.name}.{created_table.name}"
        payload = {
            _MySQLSyncResultEntityTypeEnum.TABLES: {
                table_key: {
                    "name": created_table.name,
                    "create": "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(50))",
                    "keys": {
                        "PRIMARY": {
                            "columns": ["id"],
                            "unique": True,
                            "nullable": False,
                        }
                    },
                }
            }
        }
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(321, json.dumps(payload))
        updated_table = await mock_mysql_syncer.fetch_table(created_table)
        assert isinstance(updated_table, Table)
        assert updated_table.name == created_table.name

    @pytest.mark.asyncio
    async def test_fetch_schema_resolves_unattached_service(
        self, created_service, mock_mysql_syncer, mocker
    ):
        """Test fetch_schema resolves an unattached service via the inventory API.

        Covers the unattached-service fallback in ``fetch_schema``: when the schema
        carries no attached ``service`` (or a service with no address), it must look the
        service up through ``get_inventory_service(service_id)`` rather than relying on
        a pre-attached parent.
        """
        schema = CreatedSchemaFactory.build(name="test_schema")
        schema.service = None
        schema.service_id = created_service.id
        schema.tables = []
        schema_address = f"{created_service.address}/test_schema"
        schemas_manifest = {
            _MySQLSyncResultEntityTypeEnum.SCHEMAS: {
                schema_address: {
                    "tables_path": "out/test_schema_tables.ndjson.gz",
                    "tables_count": 1,
                }
            }
        }
        mock_mysql_syncer.inventory_api.get.side_effect = [
            created_service.model_dump(),  # GET /services/{id} — the fallback resolve
            {"localhost:8000": "hostname"},  # GET /hosts/ via get_available_hosts
        ]
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(789, json.dumps(schemas_manifest))
        updated_schema = await mock_mysql_syncer.fetch_schema(schema)
        assert isinstance(updated_schema, MySQLSchema)
        assert updated_schema.tables_aiter is not None
        # Behavioural proof the fallback fired: the unattached service was resolved by id.
        mock_mysql_syncer.inventory_api.get.assert_any_await(
            f"/services/{created_service.id}", params={}
        )

    @pytest.mark.asyncio
    async def test_fetch_table_resolves_service_when_address_missing(
        self, created_service, created_table, mock_mysql_syncer, mocker
    ):
        """Test fetch_table re-resolves the service when its address is missing.

        Covers the missing-address re-resolve in ``fetch_table``: the table's schema has
        an attached service, but that service has no usable address (no node), so it must
        re-fetch it via ``get_inventory_service(service.id)``.
        """
        resolved_dump = created_service.model_dump()  # capture a copy with a node
        created_service.node = None  # address -> None, forces the re-resolve
        table_key = f"localhost:8000/{created_table.database.name}.{created_table.name}"
        payload = {
            _MySQLSyncResultEntityTypeEnum.TABLES: {
                table_key: {
                    "name": created_table.name,
                    "create": "CREATE TABLE t (id INT PRIMARY KEY)",
                    "keys": {},
                }
            }
        }
        mock_mysql_syncer.inventory_api.get.side_effect = [
            resolved_dump,  # GET /services/{id} — the fallback resolve
            {"localhost:8000": "hostname"},  # GET /hosts/ via get_available_hosts
        ]
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(321, json.dumps(payload))
        updated_table = await mock_mysql_syncer.fetch_table(created_table)
        assert isinstance(updated_table, Table)
        assert updated_table.name == created_table.name
        mock_mysql_syncer.inventory_api.get.assert_any_await(
            f"/services/{created_service.id}", params={}
        )

    @pytest.mark.asyncio
    async def test_fetch_table_resolves_schema_and_service_when_unattached(
        self, created_service, created_table, mock_mysql_syncer, mocker
    ):
        """Test fetch_table resolves both schema and service when neither is attached.

        Covers the unattached-parent ``else`` branch in ``fetch_table``: the table's
        schema has no attached service, so it resolves the schema via
        ``get_inventory_schema(schema_id)`` and then the service via
        ``get_inventory_service(service_id)``.
        """
        created_table.database.service = None  # forces the else branch
        resolved_schema = CreatedSchemaFactory.build(name="test_schema")
        resolved_schema.service = None
        resolved_schema.service_id = created_service.id
        resolved_schema.tables = []
        table_key = f"localhost:8000/test_schema.{created_table.name}"
        payload = {
            _MySQLSyncResultEntityTypeEnum.TABLES: {
                table_key: {
                    "name": created_table.name,
                    "create": "CREATE TABLE t (id INT PRIMARY KEY)",
                    "keys": {},
                }
            }
        }
        mock_mysql_syncer.inventory_api.get.side_effect = [
            resolved_schema.model_dump(),  # GET /schemas/{id}
            created_service.model_dump(),  # GET /services/{id}
            {"localhost:8000": "hostname"},  # GET /hosts/ via get_available_hosts
        ]
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(654, json.dumps(payload))
        updated_table = await mock_mysql_syncer.fetch_table(created_table)
        assert isinstance(updated_table, Table)
        assert updated_table.name == created_table.name
        mock_mysql_syncer.inventory_api.get.assert_any_await(
            f"/schemas/{created_table.schema_id}", params=RETIRED_INCLUSIVE_PARAMS
        )
        mock_mysql_syncer.inventory_api.get.assert_any_await(
            f"/services/{created_service.id}", params={}
        )

    @pytest.mark.asyncio
    async def test_fetch_service_uses_cached_fetch_result(
        self, created_service, mock_mysql_syncer
    ):
        """Test using cached fetch result for service."""
        mock_mysql_syncer._inventory_index_cache[
            _MySQLSyncResultEntityTypeEnum.SERVICES
        ][created_service.address] = (
            999,
            {"schemas_path": "out/schemas.ndjson.gz", "schemas_count": 1},
        )
        svc = await mock_mysql_syncer.fetch_service(created_service)
        assert isinstance(svc, MySQLService)
        assert svc.schemas_index is not None
        # Cache hit short-circuits the task path: no /hosts/, no task execution.
        mock_mysql_syncer.tasks_api.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_schema_uses_cached_fetch_result(
        self, created_schema, mock_mysql_syncer
    ):
        """Test using cached fetch result for schema."""
        addr = f"{created_schema.service.address}/{created_schema.name}"
        mock_mysql_syncer._inventory_index_cache[
            _MySQLSyncResultEntityTypeEnum.SCHEMAS
        ][addr] = (
            222,
            {"tables_path": "out/tables.ndjson.gz", "tables_count": 1},
        )
        sch = await mock_mysql_syncer.fetch_schema(created_schema)
        assert isinstance(sch, MySQLSchema)
        assert sch.tables_aiter is not None
        mock_mysql_syncer.tasks_api.get.assert_not_called()


class TestPerformMethods:
    """Test perform methods drive the real sync cascade and inventory_api traffic."""

    @pytest.mark.asyncio
    async def test_perform_node_sync(
        self, created_node, created_service, bound_mysql_syncer, session, mocker
    ):
        """Drive perform_node_sync: every service on a port reaches real sync_service."""
        trigger = mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)
        # Two distinct services share port 8000. The grouping rule must sync BOTH when
        # the updated node reports that port, so both produce inventory traffic.
        second_service = CreatedServiceFactory.build()
        second_service.id = created_service.id + 1
        second_service.node_id = MOCK_CREATED_NODE_ID
        second_service.type = ServiceTypeEnum.MYSQL
        second_service.node = CreatedNode(
            address="otherhost",
            id=MOCK_CREATED_NODE_ID + 1,
            node_id="/node_id/mock-2",
            node_name="otherhost",
        )
        second_service.port = 8000
        second_service.schemas = []
        created_node.services = [created_service, second_service]
        updated_node = created_node.model_copy()
        updated_node.services = [created_service]

        for service in (created_service, second_service):
            await _seed_sync_item(
                bound_mysql_syncer,
                session,
                SyncInventoryEntityTypeEnum.SERVICE,
                service.id,
            )
            # Seed each SERVICES index with no schemas_path so real fetch_service yields
            # an empty schemas_index (the model's safe empty async generator).
            bound_mysql_syncer._inventory_index_cache[
                _MySQLSyncResultEntityTypeEnum.SERVICES
            ][service.address] = (None, {})
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {"items": [], "total": 0, "offset": 0, "limit": 50},
            {"items": [], "total": 0, "offset": 0, "limit": 50},
        ]

        await bound_mysql_syncer.perform_node_sync(created_node, updated_node)

        # Both services on the port are synced: one schema-listing GET per distinct id.
        get_urls = sorted(
            call.args[0]
            for call in bound_mysql_syncer.inventory_api.get.await_args_list
        )
        assert get_urls == sorted(
            [
                f"/services/{created_service.id}/schemas/",
                f"/services/{second_service.id}/schemas/",
            ]
        )
        bound_mysql_syncer.inventory_api.post.assert_not_called()
        bound_mysql_syncer.inventory_api.put.assert_not_called()
        bound_mysql_syncer.inventory_api.delete.assert_not_called()
        trigger.assert_not_called()
        for service in (created_service, second_service):
            assert (
                bound_mysql_syncer.sync_items[
                    (SyncInventoryEntityTypeEnum.SERVICE, service.id)
                ].status
                == SyncStatusEnum.SUCCESS
            )

    @pytest.mark.asyncio
    async def test_perform_service_sync_sets_schema_cache(
        self, created_service, created_schema, bound_mysql_syncer, session, mocker
    ):
        """Drive perform_service_sync: a streamed schema is created, cached, and synced.

        The method writes the SERVICES task-history id into the SCHEMAS index cache so
        the downstream ``fetch_schema`` can reuse it instead of running another task.
        That cache entry is *transient* — the real ``sync_schema`` immediately pops it —
        so the mutation is asserted by its effect: ``wait_for_task_output`` is never
        invoked for the schema (the cache fed ``fetch_schema``).
        """
        trigger = mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)
        wait_for_task_output = mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        schema_data = created_schema.model_dump()
        # An existing inventory schema absent from the streamed set must be retired.
        stale_schema = CreatedSchemaFactory.build(name="stale_schema")
        stale_schema.id = created_schema.id + 1
        stale_schema.service = None
        stale_schema.tables = []
        for entity_id in (created_schema.id, stale_schema.id):
            await _seed_sync_item(
                bound_mysql_syncer,
                session,
                SyncInventoryEntityTypeEnum.SCHEMA,
                entity_id,
            )
        bound_mysql_syncer._inventory_index_cache[
            _MySQLSyncResultEntityTypeEnum.SERVICES
        ][created_service.address] = (111, {"schemas_path": "p", "schemas_count": 1})
        # The stale schema is returned by the inventory listing; the streamed schema is
        # new -> created via POST, while the stale one is retired.
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {
                "items": [stale_schema.model_dump()],
                "total": 1,
                "offset": 0,
                "limit": 50,
            },
        ]
        bound_mysql_syncer.inventory_api.post.side_effect = [schema_data]

        async def schemas_idx():
            yield schema_data

        updated = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        updated.schemas_index = schemas_idx()

        await bound_mysql_syncer.perform_service_sync(created_service, updated)

        assert entity_posts(bound_mysql_syncer.inventory_api) == [
            f"/services/{created_service.id}/schemas/"
        ]
        # The stale schema is retired in inventory.
        bound_mysql_syncer.inventory_api.delete.assert_awaited_once_with(
            f"/schemas/{stale_schema.id}"
        )
        # The SCHEMAS cache mutation fed fetch_schema: no extra task ran for the schema.
        wait_for_task_output.assert_not_awaited()
        trigger.assert_not_called()
        assert (
            bound_mysql_syncer.sync_items[
                (SyncInventoryEntityTypeEnum.SCHEMA, created_schema.id)
            ].status
            == SyncStatusEnum.SUCCESS
        )

    @pytest.mark.asyncio
    async def test_perform_schema_sync(
        self, created_schema, created_table, bound_mysql_syncer, session, mocker
    ):
        """Drive perform_schema_sync: a new table is created and a stale one retired."""
        trigger = mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)
        table_data = created_table.model_dump()
        # A stale table not present in the streamed set must be retired. No back-ref to
        # created_schema (that would create a serialization cycle in model_dump).
        stale_table = CreatedTableFactory.build(name="stale_table")
        stale_table.id = created_table.id + 1
        stale_table.schema_id = created_schema.id
        created_schema.tables = [stale_table]

        await _seed_sync_item(
            bound_mysql_syncer,
            session,
            SyncInventoryEntityTypeEnum.TABLE,
            created_table.id,
        )
        await _seed_sync_item(
            bound_mysql_syncer,
            session,
            SyncInventoryEntityTypeEnum.TABLE,
            stale_table.id,
        )
        bound_mysql_syncer.inventory_api.post.side_effect = [table_data]

        async def tables_iter() -> AsyncGenerator[dict[str, Any]]:
            yield table_data

        mysql_schema = MySQLSchema.model_validate(
            created_schema.model_dump(exclude={"tables"})
            | {"address": "localhost:8000/test_schema"}
        )
        mysql_schema.tables_aiter = tables_iter()

        await bound_mysql_syncer.perform_schema_sync(created_schema, mysql_schema)

        assert entity_posts(bound_mysql_syncer.inventory_api) == [
            f"/schemas/{created_schema.id}/tables/"
        ]
        bound_mysql_syncer.inventory_api.delete.assert_awaited_once_with(
            f"/tables/{stale_table.id}"
        )
        trigger.assert_not_called()
        assert (
            bound_mysql_syncer.sync_items[
                (SyncInventoryEntityTypeEnum.TABLE, created_table.id)
            ].status
            == SyncStatusEnum.SUCCESS
        )
        assert (
            bound_mysql_syncer.sync_items[
                (SyncInventoryEntityTypeEnum.TABLE, stale_table.id)
            ].status
            == SyncStatusEnum.SUCCESS
        )

    @pytest.mark.asyncio
    async def test_perform_table_sync_updates_via_inventory_api(
        self, created_table, mock_mysql_syncer
    ):
        """Drive perform_table_sync: the changed table is PUT to inventory_api."""
        updated = Table(name=created_table.name, create="CREATE TABLE x()")
        mock_mysql_syncer.inventory_api.put.side_effect = [created_table.model_dump()]

        await mock_mysql_syncer.perform_table_sync(created_table, updated)

        mock_mysql_syncer.inventory_api.put.assert_awaited_once()
        assert (
            mock_mysql_syncer.inventory_api.put.await_args.args[0]
            == f"/tables/{created_table.id}"
        )
        mock_mysql_syncer.inventory_api.post.assert_not_called()
        mock_mysql_syncer.inventory_api.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_perform_inventory_sync_drives_discovered_node(
        self, created_node, created_service, bound_mysql_syncer, session, mocker
    ):
        """Drive perform_inventory_sync: each discovered node is synced through the seam."""
        trigger = mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)
        # Keep the service address stable across re-validation ("localhost" + :8000).
        created_node.address = "localhost"
        # force_executor_host removes the /hosts/ lookup from the shared GET queue.
        bound_mysql_syncer.force_executor_host = "exec-host"
        # The fetch_node task returns a manifest with the service but no schemas, so the
        # downstream service sync does only the schema-listing GET (no streaming).
        services_manifest = {
            _MySQLSyncResultEntityTypeEnum.SERVICES: {created_service.address: {}}
        }
        mocker.patch.object(
            MySQLSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(555, json.dumps(services_manifest))
        await _seed_sync_item(
            bound_mysql_syncer,
            session,
            SyncInventoryEntityTypeEnum.NODE,
            created_node.id,
        )
        await _seed_sync_item(
            bound_mysql_syncer,
            session,
            SyncInventoryEntityTypeEnum.SERVICE,
            created_service.id,
        )
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {
                "items": [created_node.model_dump()],
                "total": 1,
                "offset": 0,
                "limit": 50,
            },
            {"items": [], "total": 0, "offset": 0, "limit": 50},
        ]

        await bound_mysql_syncer.perform_inventory_sync()

        # The discovered node is fetched then synced: first GET is the node listing,
        # and the cascade reaches the per-service schema listing.
        assert (
            bound_mysql_syncer.inventory_api.get.await_args_list[0].args[0] == "/nodes/"
        )
        get_urls = [
            call.args[0]
            for call in bound_mysql_syncer.inventory_api.get.await_args_list
        ]
        assert f"/services/{created_service.id}/schemas/" in get_urls
        trigger.assert_not_called()
        assert (
            bound_mysql_syncer.sync_items[
                (SyncInventoryEntityTypeEnum.NODE, created_node.id)
            ].status
            == SyncStatusEnum.SUCCESS
        )


class TestStreamsAndParsing:
    """Test gzip line iteration and NDJSON streaming."""

    def test_split_lines_from_buffer_variants(self, mock_mysql_syncer):
        """Test splitting lines from buffer with tail handling."""
        buf = bytearray()
        data1 = b"one\ntwo\nthr"
        data2 = b"ee\n"
        got = list(MySQLSyncer._split_lines_from_buffer(buf, data1, "utf-8"))
        assert got == ["one", "two"]
        got2 = list(MySQLSyncer._split_lines_from_buffer(buf, data2, "utf-8"))
        assert got2 == ["three"]
        assert buf == bytearray()

    @pytest.mark.asyncio
    async def test_iter_lines_gzip_stream_chunks(self, mock_mysql_syncer):
        """Test iterating gzip stream lines across chunks."""
        ndjson = b"a\nb\nc"
        comp = gzip.compress(ndjson, mtime=0)

        async def chunks():
            yield comp[:7]
            yield comp[7:13]
            yield comp[13:]

        got = [
            line async for line in mock_mysql_syncer._iter_lines_gzip_stream(chunks())
        ]
        assert got == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_iter_lines_gzip_stream_uses_remaining_flush(
        self, mock_mysql_syncer, mocker
    ):
        """Test yielding lines from remaining bytes produced by flush()."""

        class DummyDecompressor:
            def decompress(self, _data):
                return b""

            def flush(self):
                return b"l1\nl2\n"

        mocker.patch(
            "app.extensions.sync.syncers.mysql.syncer.zlib.decompressobj",
            return_value=DummyDecompressor(),
        )

        async def chunks():
            yield b"ignored-1"
            yield b"ignored-2"

        lines = [
            line async for line in mock_mysql_syncer._iter_lines_gzip_stream(chunks())
        ]

        assert lines == ["l1", "l2"]

    @pytest.mark.asyncio
    async def test_stream_ndjson_file_with_bad_line(self, mock_mysql_syncer, mocker):
        """Test streaming NDJSON and skipping invalid line."""
        ndjson = b'{"x":1}\nthis-is-not-json\n'
        comp = gzip.compress(ndjson, mtime=0)

        async def fake_stream(_url, params=None):
            yield comp[:10]
            yield comp[10:]

        mocker.patch.object(
            mock_mysql_syncer.tasks_api,
            "stream_chunks",
            return_value=fake_stream("", {}),
        )
        out = [obj async for obj in mock_mysql_syncer.stream_ndjson_file(1, "x.gz")]
        assert out == [{"x": 1}]

    @pytest.mark.asyncio
    async def test_iter_lines_gzip_stream_reassembles_a_line_across_many_pieces(
        self, mock_mysql_syncer, mocker
    ):
        """Test yielding a line whole after many newline-free decompressed pieces."""
        pieces = [b"x" * 32] * 16 + [b"end\n"]

        class QueuedDecompressor:
            """Return one queued piece per ``decompress`` call, then nothing."""

            def __init__(self) -> None:
                """Queue the pieces this stub hands out."""
                self._pieces = iter(pieces)

            def decompress(self, _data: bytes) -> bytes:
                """Return the next queued piece.

                :param _data: The compressed bytes, ignored by this stub.
                :return: The next piece, or ``b""`` once the queue is empty.
                """
                return next(self._pieces, b"")

            def flush(self) -> bytes:
                """Return no trailing bytes; every piece arrives by decompress."""
                return b""

        mocker.patch(
            "app.extensions.sync.syncers.mysql.syncer.zlib.decompressobj",
            return_value=QueuedDecompressor(),
        )

        async def chunks() -> AsyncGenerator[bytes]:
            for _ in pieces:
                yield b"ignored"

        lines = [
            line async for line in mock_mysql_syncer._iter_lines_gzip_stream(chunks())
        ]

        assert lines == ["x" * 512 + "end"]

    @pytest.mark.asyncio
    async def test_stream_ndjson_file_reassembles_object_split_across_chunks(
        self, mock_mysql_syncer, mocker
    ):
        """Test parsing an NDJSON object that straddles many chunk boundaries.

        A dropped remainder truncates the line into invalid JSON, which this path
        logs and skips rather than failing on — so the row would vanish from the
        sync with nothing but a log line to say so.
        """
        row = {"name": "x" * 200}
        comp = gzip.compress(json.dumps(row).encode() + b"\n", mtime=0)

        async def fake_stream(
            _url: str, params: dict | None = None
        ) -> AsyncGenerator[bytes]:
            for start in range(0, len(comp), 8):
                yield comp[start : start + 8]

        mocker.patch.object(
            mock_mysql_syncer.tasks_api,
            "stream_chunks",
            return_value=fake_stream("", {}),
        )
        logged = mocker.patch(
            "app.extensions.sync.syncers.mysql.syncer.logger.exception"
        )

        out = [obj async for obj in mock_mysql_syncer.stream_ndjson_file(1, "x.gz")]

        assert out == [row]
        logged.assert_not_called()

    @pytest.mark.asyncio
    async def test_wait_for_task_output_default_payload(
        self, mock_mysql_syncer, mocker
    ):
        """Test using default file payload and delegating to super."""

        async def fake_super(_, *args, **kwargs):
            return TaskRunResult(1, "{}")

        mocker.patch(
            "app.extensions.sync.models.BaseTaskSyncer.wait_for_task_output",
            side_effect=fake_super,
        )
        res = await mock_mysql_syncer.wait_for_task_output(config="{}", target="host")
        assert isinstance(res, TaskRunResult)


def _replay_split_with_full_scans(
    buffer: bytearray, data: bytes, encoding: str = "utf-8"
) -> list[str]:
    """Replay one call through the unnarrowed loop as an equivalence oracle.

    Mirrors what ``_split_lines_from_buffer`` did before the search was
    narrowed: one cursor, restarting the search at ``0`` on every call.

    :param buffer: The carried buffer, mutated in place as the real call would.
    :param data: The arriving bytes.
    :param encoding: The character encoding to decode each line with.
    :return: The lines the call would have yielded.
    """
    lines: list[str] = []
    if data:
        buffer.extend(data)
        offset = 0
        while True:
            newline_pos = buffer.find(b"\n", offset)
            if newline_pos == -1:
                if offset:
                    del buffer[:offset]
                break
            line_bytes = buffer[offset:newline_pos]
            if line_bytes:
                lines.append(line_bytes.decode(encoding))
            offset = newline_pos + 1
    return lines


DATA_SEQUENCES = [
    pytest.param([], id="no-calls"),
    pytest.param([b""], id="empty-data"),
    pytest.param([b"", b"", b""], id="only-empty-data"),
    pytest.param([b"line\n"], id="single-terminated"),
    pytest.param([b"no-newline"], id="single-unterminated"),
    pytest.param([b"a", b"b", b"c"], id="newline-free-run"),
    pytest.param([b"x" * 16] * 8 + [b"end\n"], id="long-run-then-completion"),
    pytest.param([b"one-", b"line-", b"split\nnext\n"], id="straddles-three-calls"),
    pytest.param([b"tail", b"\nlead"], id="newline-is-first-arriving-byte"),
    pytest.param([b"a\nb\nc\n"], id="multiple-terminators-one-call"),
    pytest.param([b"\n\n\n"], id="only-terminators"),
    pytest.param([b"x\n", b"\n"], id="empty-line-in-its-own-call"),
    pytest.param([b"a\r", b"\nb"], id="carriage-return-is-not-a-terminator"),
    pytest.param([b"tail", b"", b"\n"], id="empty-data-mid-run"),
    pytest.param(
        ["caf\u00e9=x\n".encode()[:5], "caf\u00e9=x\n".encode()[5:]],
        id="multibyte-split",
    ),
]


class TestSplitLinesFromBuffer:
    """Test the narrowed newline search in ``_split_lines_from_buffer``."""

    @staticmethod
    def _drive(
        payloads: list[bytes], buffer: bytearray | None = None
    ) -> tuple[list[list[str]], bytes]:
        """Run ``payloads`` through ``_split_lines_from_buffer`` in order.

        :param payloads: The arriving byte payloads, one per call.
        :param buffer: The buffer to carry across calls; a fresh one by default.
        :return: The lines each call yielded, and the bytes left buffered.
        """
        buffer = bytearray() if buffer is None else buffer
        yielded = [
            list(MySQLSyncer._split_lines_from_buffer(buffer, payload, "utf-8"))
            for payload in payloads
        ]
        return yielded, bytes(buffer)

    @pytest.mark.parametrize("payloads", DATA_SEQUENCES)
    def test_matches_the_unnarrowed_loop(self, payloads: list[bytes]) -> None:
        """Assert the narrowed search yields what a search from zero yields."""
        oracle_buffer = bytearray()
        expected = [
            _replay_split_with_full_scans(oracle_buffer, payload)
            for payload in payloads
        ]

        yielded, buffer = self._drive(payloads)

        assert yielded == expected
        assert buffer == bytes(oracle_buffer)

    @pytest.mark.parametrize("payloads", DATA_SEQUENCES)
    def test_buffer_holds_no_terminator_after_a_call(
        self, payloads: list[bytes]
    ) -> None:
        """Assert the premise the narrowed search rests on holds after each call.

        A future edit that leaves a newline behind makes every later call skip
        it, silently gluing two lines together.
        """
        buffer = bytearray()
        for payload in payloads:
            list(MySQLSyncer._split_lines_from_buffer(buffer, payload, "utf-8"))
            assert b"\n" not in buffer

    def test_abandoned_generator_leaves_the_buffer_fit_for_the_next_call(
        self,
    ) -> None:
        """Assert stopping mid-iteration strands no terminator in the buffer.

        The trim runs before the first yield, so a consumer that stops early
        loses the lines it never took rather than leaving a terminator the next
        call's narrowed search would skip past, gluing two lines into one.
        """
        buffer = bytearray()
        lines = MySQLSyncer._split_lines_from_buffer(buffer, b"a\nb\n", "utf-8")

        assert next(lines) == "a"
        lines.close()
        assert b"\n" not in buffer

        yielded, remainder = self._drive([b"c\n"], buffer=buffer)

        assert yielded == [["c"]]
        assert remainder == b""

    def test_straddling_line_keeps_the_carried_remainder(self) -> None:
        """Assert the first line a call completes still carries earlier bytes.

        Collapsing the search cursor into the line-start cursor drops the
        remainder and hands a truncated line to the JSON decoder, which
        ``stream_ndjson_file`` logs and skips rather than failing on.
        """
        yielded, buffer = self._drive([b"thr", b"ee\n"])

        assert yielded == [[], ["three"]]
        assert buffer == b""

    def test_empty_lines_stay_dropped(self) -> None:
        """Assert consecutive terminators still yield nothing."""
        yielded, buffer = self._drive([b"a\n", b"\n\n", b"b\n"])

        assert yielded == [["a"], [], ["b"]]
        assert buffer == b""

    def test_carriage_return_is_retained_in_the_line(self) -> None:
        """Test retaining a carriage return, which does not terminate a line."""
        yielded, _ = self._drive([b"progress\r", b"done\n"])

        assert yielded == [[], ["progress\rdone"]]

    def test_multibyte_codepoint_split_across_calls_decodes_intact(self) -> None:
        """Assert a codepoint straddling two calls is decoded undamaged."""
        raw = "caf\u00e9=x\n".encode()
        yielded, buffer = self._drive([raw[:4], raw[4:]])

        assert yielded == [[], ["caf\u00e9=x"]]
        assert buffer == b""

    def test_unterminated_partial_codepoint_is_withheld_not_decoded(self) -> None:
        """Assert a call ending mid-codepoint yields nothing and raises nothing."""
        yielded, buffer = self._drive(["caf\u00e9".encode()[:4]])

        assert yielded == [[]]
        assert buffer == b"caf\xc3"

    def test_undecodable_line_raises_like_the_unnarrowed_loop(self) -> None:
        """Assert an invalid byte inside a completed line still raises."""
        payload = b"bad\xffline\n"
        with pytest.raises(UnicodeDecodeError):
            _replay_split_with_full_scans(bytearray(), payload)
        with pytest.raises(UnicodeDecodeError):
            self._drive([payload])

    def test_scan_starts_at_the_pre_append_length(self) -> None:
        """Assert each call's search begins where the previous one stopped."""
        buffer = ScanRecordingBytearray()
        yielded, _ = self._drive([b"x" * 4] * 4, buffer=buffer)

        assert yielded == [[], [], [], []]
        assert [start for start, _ in buffer.scans] == [0, 4, 8, 12]

    def test_total_scan_work_is_linear_in_the_delivered_bytes(self) -> None:
        """Assert a newline-free run never re-examines the carried remainder."""
        buffer = ScanRecordingBytearray()
        payloads = [b"x" * 32] * 16 + [b"end\n"]
        self._drive(payloads, buffer=buffer)

        scanned = sum(end - start for start, end in buffer.scans)
        assert scanned == sum(map(len, payloads))

    def test_releasing_call_still_scans_only_its_own_bytes(self) -> None:
        """Assert the call that yields the buffer narrows its search too.

        Work proportional to the buffer is legitimate on the call that decodes
        and hands those bytes over; the search for the terminator is not.
        """
        buffer = ScanRecordingBytearray()
        yielded, _ = self._drive([b"x" * 64, b"end\n"], buffer=buffer)

        assert yielded == [[], ["x" * 64 + "end"]]
        assert buffer.scans[-2:] == [(64, 68), (68, 68)]


class TestModelIteratorsAndGuards:
    """Test model iterators and sync guards."""

    @pytest.mark.asyncio
    async def test_mysqlservice_empty_iterator_when_none(self, created_service):
        """Test iterating empty schemas_index when unset."""
        svc = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        out = [item async for item in svc.schemas_index]
        assert out == []

    @pytest.mark.asyncio
    async def test_mysqlschema_iter_tables_fallback(
        self, created_schema, created_table
    ):
        """Test iterating existing tables when no async iterator set."""
        sch = MySQLSchema.model_validate(
            created_schema.model_dump(exclude={"tables"})
            | {"address": "localhost:8000/s"}
        )
        sch.tables = [created_table]
        out = [table async for table in sch.iter_tables()]

        assert len(out) == 1
        assert out[0].name == "test_table"

    def test_can_sync_node_true_false(self, mock_mysql_syncer):
        """Test returning True only when node has a MySQL service."""
        node_with_mysql = Node(
            address="x",
            name="x",
            node_id="/node_id/x",
            services=[
                Service(
                    type=ServiceTypeEnum.MYSQL,
                    port=3306,
                    name="s",
                    service_id="/service_id/s",
                )
            ],
        )
        node_without_mysql = Node(
            address="y", name="y", node_id="/node_id/y", services=[]
        )
        assert mock_mysql_syncer.can_sync_node(node_with_mysql) is True
        assert mock_mysql_syncer.can_sync_node(node_without_mysql) is False


class TestTombstoneReconciliation:
    """Test how this syncer reconciles against schemas and tables it retired."""

    @pytest.mark.asyncio
    async def test_reappearing_schema_is_revived_on_its_existing_row(
        self, created_service, created_schema, bound_mysql_syncer, mocker
    ):
        """Revive the tombstone rather than create a second row for its name."""
        mocker.patch.object(MySQLSyncer, "sync_schema", new_callable=AsyncMock)
        created_schema.service = None
        retired = created_schema.model_copy(update={"retired_at": utc_now()})
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {"items": [retired.model_dump()], "total": 1, "offset": 0, "limit": 50},
        ]

        async def schemas_idx():
            yield created_schema.model_dump()

        updated = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        updated.schemas_index = schemas_idx()

        await bound_mysql_syncer.perform_service_sync(created_service, updated)

        bound_mysql_syncer.inventory_api.post.assert_awaited_once_with(
            f"/schemas/{created_schema.id}/revive"
        )
        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_active_row_wins_the_name_over_a_tombstone(
        self, created_service, created_schema, bound_mysql_syncer, mocker
    ):
        """Match the live schema, not the tombstone that used to hold its name."""
        mocker.patch.object(MySQLSyncer, "sync_schema", new_callable=AsyncMock)
        created_schema.service = None
        tombstone = created_schema.model_copy(
            update={"id": created_schema.id + 1, "retired_at": utc_now()}
        )
        # Tombstone last, so a plain last-write-wins index would pick it.
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {
                "items": [created_schema.model_dump(), tombstone.model_dump()],
                "total": 2,
                "offset": 0,
                "limit": 50,
            },
        ]

        async def schemas_idx():
            yield created_schema.model_dump()

        updated = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        updated.schemas_index = schemas_idx()

        await bound_mysql_syncer.perform_service_sync(created_service, updated)

        # The live schema matched, so nothing was created and nothing revived.
        bound_mysql_syncer.inventory_api.post.assert_not_awaited()
        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_absent_tombstoned_schema_is_not_retired_again(
        self, created_service, created_schema, bound_mysql_syncer, mocker
    ):
        """Leave an already-retired schema alone rather than re-retiring it."""
        mocker.patch.object(MySQLSyncer, "sync_schema", new_callable=AsyncMock)
        created_schema.service = None
        retired = created_schema.model_copy(update={"retired_at": utc_now()})
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {"items": [retired.model_dump()], "total": 1, "offset": 0, "limit": 50},
        ]

        async def schemas_idx():
            return
            yield

        updated = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        updated.schemas_index = schemas_idx()

        await bound_mysql_syncer.perform_service_sync(created_service, updated)

        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()
        assert not bound_mysql_syncer.sync_items

    @pytest.mark.asyncio
    async def test_reappearing_table_is_revived_on_its_existing_row(
        self, created_schema, created_table, bound_mysql_syncer, mocker
    ):
        """Revive the tombstoned table rather than create a second row for its name."""
        mocker.patch.object(MySQLSyncer, "sync_table", new_callable=AsyncMock)
        created_table.database = None
        retired = created_table.model_copy(update={"retired_at": utc_now()})
        created_schema.tables = [retired]
        table_data = created_table.model_dump()

        async def tables_iter() -> AsyncGenerator[dict[str, Any]]:
            yield table_data

        mysql_schema = MySQLSchema.model_validate(
            created_schema.model_dump(exclude={"tables"})
            | {"address": "localhost:8000/test_schema"}
        )
        mysql_schema.tables_aiter = tables_iter()

        await bound_mysql_syncer.perform_schema_sync(created_schema, mysql_schema)

        bound_mysql_syncer.inventory_api.post.assert_awaited_once_with(
            f"/tables/{created_table.id}/revive"
        )
        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_absent_tombstoned_table_is_not_retired_again(
        self, created_schema, created_table, bound_mysql_syncer, mocker
    ):
        """Leave an already-retired table alone rather than re-retiring it."""
        mocker.patch.object(MySQLSyncer, "sync_table", new_callable=AsyncMock)
        created_table.database = None
        created_table.retired_at = utc_now()
        created_schema.tables = [created_table]

        # No ``tables_aiter``: ``iter_tables`` falls back to the model's own empty
        # table list, which is the fetch reporting nothing.
        mysql_schema = MySQLSchema.model_validate(
            created_schema.model_dump(exclude={"tables"})
            | {"address": "localhost:8000/test_schema"}
        )

        await bound_mysql_syncer.perform_schema_sync(created_schema, mysql_schema)

        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()
        assert not bound_mysql_syncer.sync_items

    @pytest.mark.asyncio
    async def test_an_active_row_wins_the_table_name_over_a_tombstone(
        self, created_schema, created_table, bound_mysql_syncer, mocker
    ):
        """Match the live table, not the tombstone that used to hold its name."""
        mocker.patch.object(MySQLSyncer, "sync_table", new_callable=AsyncMock)
        created_table.database = None
        tombstone = created_table.model_copy(
            update={"id": created_table.id + 1, "retired_at": utc_now()}
        )
        # Tombstone last, so a plain last-write-wins index would pick it.
        created_schema.tables = [created_table, tombstone]
        table_data = created_table.model_dump()

        async def tables_iter() -> AsyncGenerator[dict[str, Any]]:
            yield table_data

        mysql_schema = MySQLSchema.model_validate(
            created_schema.model_dump(exclude={"tables"})
            | {"address": "localhost:8000/test_schema"}
        )
        mysql_schema.tables_aiter = tables_iter()

        await bound_mysql_syncer.perform_schema_sync(created_schema, mysql_schema)

        # The live row matched, so nothing was created, revived, or retired.
        assert entity_posts(bound_mysql_syncer.inventory_api) == []
        bound_mysql_syncer.inventory_api.delete.assert_not_awaited()


class TestMirroredEntityLevels:
    """Test which entity levels this syncer's attempts write sync health for."""

    def test_schema_and_table_are_the_mirrored_levels(self) -> None:
        """Own exactly the two levels ``update_schema`` and ``update_table`` write."""
        assert MySQLSyncer.mirrors_entity_levels == frozenset(
            {
                SyncInventoryEntityTypeEnum.SCHEMA,
                SyncInventoryEntityTypeEnum.TABLE,
            }
        )

    @pytest.mark.asyncio
    async def test_a_traversed_service_is_never_reported(
        self, bound_mysql_syncer, created_node, created_service, session, mocker
    ) -> None:
        """Confirm nothing about a service this syncer only walks through.

        ``perform_node_sync`` reaches the node's services to get at their
        schemas and never calls ``update_service``, so a service's mirrored
        values are PMM's to confirm — refreshing them here would report a
        confirmation this run never made, and mask a failing PMM mirror.

        The cascade runs for real: only ``alert_service.trigger`` is mocked, so
        the walk reaches the shared ``sync_service`` boundary the reporter is
        wired into.
        """
        mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)
        created_node.services = [created_service]
        await _seed_sync_item(
            bound_mysql_syncer,
            session,
            SyncInventoryEntityTypeEnum.SERVICE,
            created_service.id,
        )
        # No schemas_path, so the real fetch_service yields an empty schemas index.
        bound_mysql_syncer._inventory_index_cache[
            _MySQLSyncResultEntityTypeEnum.SERVICES
        ][created_service.address] = (None, {})
        bound_mysql_syncer.inventory_api.get.side_effect = [
            {"items": [], "total": 0, "offset": 0, "limit": 50},
        ]

        await bound_mysql_syncer.perform_node_sync(created_node, created_node)

        assert (
            bound_mysql_syncer.sync_items[
                (SyncInventoryEntityTypeEnum.SERVICE, created_service.id)
            ].status
            == SyncStatusEnum.SUCCESS
        ), (
            "the walk must have reached the service for the absence below to mean anything"
        )
        assert sync_health_posts(bound_mysql_syncer.inventory_api) == []
