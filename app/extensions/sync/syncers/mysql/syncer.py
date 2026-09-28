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

"""Implement the MySQL Inventory Sync."""

import json
import logging
import zlib
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from pydantic import Field

from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
    Node,
    Schema,
    Service,
    Table,
)
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.extensions.sync.models import BaseTaskSyncer, claim_identity, TaskRunResult
from app.inventory.models import ServiceTypeEnum

GZIP_WBITS = 16 + zlib.MAX_WBITS

logger = logging.getLogger(__name__)


class _MySQLSyncResultEntityTypeEnum(StrEnum):
    """Define enumeration of MySQL sync result types."""

    SERVICES = "services"
    SCHEMAS = "schemas"
    TABLES = "tables"


class _MySQLFetchResult(NamedTuple):
    """Represent the result of a MySQL fetch task.

    :param task_history_id: The ID of the task history record.
    :type task_history_id: int
    :param index: The index data returned from the sync task.
    :type index: dict[str, Any]
    """

    task_history_id: int
    index: dict[str, Any]


class MySQLService(Service):
    """Represent a MySQL service with an async schema index iterator.

    Overrides the base Service model to include an asynchronous iterator
    for fetching schema index data.

    param environment: The environment in which the service is running (e.g.,
        "production", "staging"). Defaults to None.
    :type environment: str | None
    :param cluster: The cluster in which the service is running. Defaults to None.
    :type cluster: str | None
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :type replication_set: str | None
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :type custom_labels: dict[str, Any] | None
    :param external_id: The external identifier for the service, aliased as
        "service_id". Defaults to None.
    :type external_id: NonEmptyStr | EmptyStrToNone
    :param name: The name of the service, aliased as "service_name".
    :type name: NonEmptyStr
    :param port: The port number on which the service is running. Defaults to None.
    :type port: int | EmptyStrToNone
    :param type: The type of the service (e.g., "service_type"), aliased as
        "service_type".
    :type type: ServiceTypeEnum
    :param schemas: The schemas associated with the service.
    :type schemas: list[Schema]
    """

    _schemas_index: AsyncIterator[dict[str, Any]] | None = None

    @property
    def schemas_index(self) -> AsyncIterator[dict[str, Any]]:
        """Return the asynchronous iterator for schema index data.

        :return: An asynchronous iterator yielding schema index data, or an empty
            iterator if no schema index is set.
        """
        if self._schemas_index is None:

            async def empty_iterator() -> AsyncGenerator[dict[str, Any]]:
                for _ in ():
                    yield _

            return empty_iterator()
        return self._schemas_index

    @schemas_index.setter
    def schemas_index(self, value: AsyncIterator[dict[str, Any]] | None) -> None:
        """Set the asynchronous iterator for schema index data.

        :param value: An asynchronous iterator yielding schema index data, or None.
        :type value: AsyncIterator[dict[str, Any]] | None
        """
        self._schemas_index = value


class MySQLSchema(Schema):
    """Represent a MySQL schema with an async table iterator.

    Overrides the base Schema model to include an asynchronous iterator
    for fetching table data and an `address` field.

    :param name: The name of the schema.
    :type name: NonEmptyStr
    :param tables: The tables associated with the schema.
    :type tables: list[Table]
    :param address: The unique address of the schema within the inventory system.
    :type address: str
    """

    address: str = Field(..., exclude=True)
    _tables_aiter: AsyncIterator[dict[str, Any]] | None = None

    @property
    def tables_aiter(self) -> AsyncIterator[dict[str, Any]] | None:
        """Return the asynchronous iterator for table data.

        :return: An asynchronous iterator yielding table data, or None if no table
            iterator is set.
        :rtype: AsyncIterator[dict[str, Any]] | None
        """
        return self._tables_aiter

    @tables_aiter.setter
    def tables_aiter(self, value: AsyncIterator[dict[str, Any]] | None) -> None:
        """Set the asynchronous iterator for table data.

        :param value: An asynchronous iterator yielding table data, or None.
        :type value: AsyncIterator[dict[str, Any]] | None
        """
        self._tables_aiter = value

    async def iter_tables(self) -> AsyncGenerator[Table]:
        """Iterate over the tables in the schema asynchronously.

        :yield: Each table in the schema as a `Table` instance.
        :rtype: AsyncGenerator[Table]
        """
        if self._tables_aiter is None:
            for table in self.tables:
                yield table
        else:
            async for table_data in self._tables_aiter:
                yield Table.model_validate(table_data)


class MySQLSyncer(BaseTaskSyncer):
    """Synchronize MySQL inventory entities within the PMM Extensions application.

    The `MySQLSyncer` class extends `BaseTaskSyncer` to provide synchronization
    capabilities specifically for MySQL services. It handles fetching and updating
    nodes, services, schemas, and tables from MySQL databases, ensuring that the
    inventory remains consistent with the source data.

    :cvar SYNC_TO_LIMIT: The highest entity type that can be synchronized. Set to
        `SyncInventoryEntityTypeEnum.TABLE`.
    :cvar mirrors_entity_levels: The schema and table levels, the only two whose
        own fields this syncer writes. Its node and service passes are traversal
        to reach the schemas: ``perform_node_sync`` and ``perform_service_sync``
        walk to their children and never call ``update_node`` or
        ``update_service``, so neither level's freshness is theirs to refresh.
    :cvar reads_retired_entities: The schema and table levels, the only two this
        syncer matches by name against a live fetch. Its node and service passes
        walk the inventory unconditionally, so they stay active-only — reading a
        tombstoned node there would launch a fetch payload at a host whose
        upstream is gone.
    :param ignore_schemas: A list of schema names to ignore during synchronization.
    :param resolve_localhost: Resolve the --host IP to 127.0.0.1 if it's the same as
        the executor host. Defaults to True.
    """

    SYNC_TO_LIMIT: ClassVar[SyncInventoryEntityTypeEnum] = (
        SyncInventoryEntityTypeEnum.TABLE
    )
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = frozenset(
        {SyncInventoryEntityTypeEnum.SCHEMA, SyncInventoryEntityTypeEnum.TABLE}
    )
    reads_retired_entities: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
        frozenset(
            {SyncInventoryEntityTypeEnum.SCHEMA, SyncInventoryEntityTypeEnum.TABLE}
        )
    )
    ignore_schemas: list[str] = []
    resolve_localhost: bool = True
    _inventory_index_cache: dict[
        _MySQLSyncResultEntityTypeEnum, dict[str, _MySQLFetchResult]
    ] = defaultdict(dict)

    @property
    def payload_path(self) -> Path:
        """Determine the path to the payload script.

        This property constructs the file path to the `payload.py` script, which is
        used for executing synchronization tasks.

        :return: The `Path` object pointing to the payload script.
        :rtype: Path
        """
        # TODO: Create PAYLOADS_DIR setting and keep payloads/scripts there  # noqa: TD002, TD003
        return Path(__file__).parent / "payload.py"

    @staticmethod
    def _build_entity_address(
        service_address: str,
        schema_name: str | None = None,
        table_name: str | None = None,
    ) -> str:
        """Build the address for an inventory entity.

        Constructs a unique address string for a service, schema, or table based on
        the provided parameters.

        :param service_address: The address of the service.
        :type service_address: str
        :param schema_name: The name of the schema. Defaults to `None`.
        :type schema_name: str | None
        :param table_name: The name of the table. Defaults to `None`.
        :type table_name: str | None
        :return: The constructed entity address.
        :rtype: str
        """
        if schema_name is None:
            return service_address
        if table_name is None:
            return f"{service_address}/{schema_name}"
        return f"{service_address}/{schema_name}.{table_name}"

    @staticmethod
    def _split_lines_from_buffer(
        buffer: bytearray, data: bytes, encoding: str = "utf-8"
    ) -> Generator[str]:
        """Append data to buffer and yield complete lines.

        Each call consumes the newlines ``data`` introduced and drops what
        precedes them, so ``buffer`` holds no newline when the next call
        arrives. Searching only the arriving bytes therefore finds exactly what
        a search from the front finds, at a cost proportional to ``data`` rather
        than to the remainder behind it.

        The narrowing holds only while ``buffer`` is newline-free on entry, so
        this call empties it of terminators before it yields anything: a
        consumer that abandons the generator part-way cannot leave one behind
        for the next call to search past. An abandoned generator therefore drops
        the lines it had not yet yielded instead of corrupting the buffer.
        Nothing but this function may mutate ``buffer`` between calls.

        :param buffer: A bytearray buffer to hold incomplete line data. Must
            hold no newline on entry.
        :param data: Incoming byte data to append to the buffer.
        :param encoding: The character encoding to use for decoding lines. Defaults to
            `"utf-8"`.
        :yield: Each complete line decoded from the buffer, terminator stripped;
            empty lines are dropped.
        """
        if not data:
            return
        # Two cursors, not one: the terminator can only be in the arriving
        # bytes, but the line it ends begins at the front of the buffer, where
        # the remainder carried from earlier calls sits.
        search_from = len(buffer)
        buffer.extend(data)
        line_start = 0
        lines: list[bytearray] = []
        while True:
            newline_pos = buffer.find(b"\n", search_from)
            if newline_pos == -1:
                break
            lines.append(buffer[line_start:newline_pos])
            line_start = search_from = newline_pos + 1
        del buffer[:line_start]
        for line_bytes in lines:
            if line_bytes:
                yield line_bytes.decode(encoding)

    async def _iter_lines_gzip_stream(
        self, chunks: AsyncIterator[bytes], encoding: str = "utf-8"
    ) -> AsyncGenerator[str]:
        """Iterate over lines from a GZIP-compressed byte stream.

        Decompresses GZIP-compressed byte chunks from an asynchronous iterator and
        yields each line decoded using the specified encoding. Handles cases where lines
        may be split across multiple chunks by maintaining a buffer.

        :param chunks: An asynchronous iterator yielding GZIP-compressed byte chunks.
        :type chunks: AsyncIterator[bytes]
        :param encoding: The character encoding to use for decoding lines.
            Defaults to `"utf-8"`.
        :type encoding: str
        :yield: Each line decoded from the GZIP-compressed stream.
        :rtype: AsyncGenerator[str]
        """
        decompressor = zlib.decompressobj(wbits=GZIP_WBITS)
        buffer = bytearray()

        async for chunk in chunks:
            decompressed = decompressor.decompress(chunk)
            for line in self._split_lines_from_buffer(buffer, decompressed, encoding):
                yield line

        remaining = decompressor.flush()
        if remaining:
            for line in self._split_lines_from_buffer(buffer, remaining, encoding):
                yield line

        if buffer:
            yield bytes(buffer).decode(encoding)

    async def stream_ndjson_file(
        self, task_history_id: int, path: str, encoding: str = "utf-8"
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream NDJSON file from a task history.

        Streams a newline-delimited JSON (NDJSON) file from the specified task
        history and yields each JSON object as a dictionary.

        :param task_history_id: The ID of the task history record.
        :type task_history_id: int
        :param path: The path to the NDJSON file within the task history.
        :type path: str
        :param encoding: The character encoding to use for decoding lines.
            Defaults to `"utf-8"`.
        :type encoding: str
        :yield: Each JSON object from the NDJSON file as a dictionary.
        :rtype: AsyncGenerator[dict[str, Any]]
        """
        file_iter = self.tasks_api.stream_chunks(
            f"/history/{task_history_id}/file/", params={"path": path}
        )
        async for line in self._iter_lines_gzip_stream(file_iter, encoding=encoding):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.exception("Failed to decode JSON line: %s", line[:200])

    def build_script_config(
        self,
        *hosts: str,
        schema: str | None = None,
        table: str | None = None,
    ) -> str:
        """Build the configuration for the MySQL sync script.

        Constructs a JSON configuration based on the provided hosts,
        schema, table, and any schemas to ignore.

        :param hosts: Variable length argument list of host addresses.
        :type hosts: str
        :param schema: The name of the schema to synchronize. Defaults to `None`.
        :type schema: str | None
        :param table: The name of the table to synchronize. Defaults to `None`.
        :type table: str | None
        :return: A JSON string containing configuration for the script.
        :rtype: str
        """
        config = {
            "hosts": hosts,
            "ignore_schemas": self.ignore_schemas,
            "resolve_localhost": self.resolve_localhost,
        }
        if schema:
            config["schema"] = schema
        if table:
            config["table"] = table
        return json.dumps(config)

    async def build_meta(self, config: str, target: str) -> dict[str, str]:
        """Build metadata for task execution.

        Creates a metadata dictionary containing the command-line arguments, target
        information, and required dependencies for the synchronization task.

        :param config: The configuration for the task script.
        :type config: str
        :param target: The target host for the task.
        :type target: str
        :return: A dictionary with metadata required for task execution.
        :rtype: dict[str, str]
        """
        # TODO: Figure out a way to keep requirements attached to payloads  # noqa: TD002, TD003
        return {
            "config": config,
            "target": target,
            "requirements": "PyMySQL[rsa,ed25519]\nmyloginpath",
            "_job_id_prefix": "mysql-sync",
        }

    async def wait_for_task_output(
        self,
        task_name: str = "run-python",
        stdout_step: str = "run-script",
        payload: str | None = None,
        **meta: Any,
    ) -> TaskRunResult:
        """Wait for a task to complete and retrieve its output.

        Override `BaseTaskSyncer.wait_for_task_output` to provide default arguments.

        :param task_name: The name of the task to execute. Defaults to `"run-python"`.
        :type task_name: str
        :param stdout_step: The step identifier for stdout retrieval. Defaults to
            `"run-script"`.
        :type stdout_step: str
        :param payload: The payload to send with the task execution request.
            Defaults to `None`. If None, the `payload_path` is used.
        :type payload: str | None
        :param meta: Additional meta variables to send with the task execution request.
        :type meta: Any
        :return: The result of the task execution.
        :rtype: TaskRunResult
        :raises ValueError: If the task fails or times out.
        """
        payload = f"file://{self.payload_path}" if payload is None else payload
        return await super().wait_for_task_output(
            task_name,
            stdout_step,
            payload,
            **meta,
        )

    async def _fetch_inventory_index(
        self,
        config: str,
        target: str,
        entity_type: _MySQLSyncResultEntityTypeEnum,
        *,
        save_to_cache: bool = True,
    ) -> dict[str, _MySQLFetchResult]:
        """Fetch inventory index for a specific entity type.

        Executes a synchronization task to retrieve the inventory index for the
        specified entity type (services, schemas, or tables) and processes the
        returned data.

        :param config: The configuration for the task script.
        :type config: str
        :param target: The target host for the task.
        :type target: str
        :param entity_type: The type of entity to fetch (services, schemas, or tables).
        :type entity_type: _MySQLSyncResultEntityTypeEnum
        :param save_to_cache: Whether to save the fetched index to the cache. Defaults
            to `True`.
        :type save_to_cache: bool
        :return: A dictionary mapping entity addresses to their fetch results.
        :rtype: dict[str, _MySQLFetchResult]
        """
        meta = await self.build_meta(config, target)
        task_result = await self.wait_for_task_output(**meta)
        entity_data = json.loads(task_result.stdout).get(entity_type, {})
        entity_indexes = {
            item_address: _MySQLFetchResult(task_result.task_history_id, item_data)
            for item_address, item_data in entity_data.items()
        }
        if save_to_cache:
            self._inventory_index_cache[entity_type] |= entity_indexes
        return entity_indexes

    async def perform_inventory_sync(self) -> None:
        """Execute the inventory synchronization process.

        Initiates synchronization for all available inventory nodes by iterating
        through each node and invoking the `sync_node` method.
        """
        for node in await self.get_inventory_nodes():
            await self.sync_node(node)

    async def fetch_node(self, created_node: CreatedNode) -> Node:
        """Fetch updated data for a specific node.

        Retrieves the latest information for the specified node by executing a
        synchronization task and processing the returned schema data.

        :param created_node: The node instance to fetch updated data for.
        :type created_node: CreatedNode
        :return: The updated node data.
        :rtype: Node
        """
        hosts = {
            service.address
            for service in created_node.services
            if self.can_sync_service(service)
        }
        script_config = self.build_script_config(*hosts)
        task_target = await self.get_task_target(
            created_node.address, created_node.name
        )
        services_index = await self._fetch_inventory_index(
            script_config, task_target, _MySQLSyncResultEntityTypeEnum.SERVICES
        )

        updated_node_data = created_node.model_dump(exclude={"services"})
        updated_node_data["services"] = [
            service.model_dump(exclude={"schemas"})
            for service in created_node.services
            if service.address in services_index
        ]
        return Node.model_validate(updated_node_data)

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,
    ) -> None:
        """Synchronize data for a specific node.

        Updates the services associated with the node by comparing existing services
        with the updated services and performing necessary synchronization actions.

        :param created_node: The node instance to synchronize.
        :type created_node: CreatedNode
        :param updated_node: The updated node data.
        :type updated_node: Node
        """
        syncable_services = defaultdict(list)
        for service in created_node.services:
            syncable_services[service.port].append(service)
        for service in updated_node.services:
            for created_service in syncable_services[service.port]:
                await self.sync_service(created_service)

    async def fetch_service(self, created_service: CreatedService) -> MySQLService:
        """Fetch updated data for a specific service.

        Retrieves the latest information for the specified service by executing a
        synchronization task and processing the returned schema data. If the service
        data is already cached, it uses the cached data instead of executing a new task.

        :param created_service: The service instance to fetch updated data for.
        :type created_service: CreatedService
        :return: The updated service.
        :rtype: MySQLService
        """
        if (
            fetch_result := self._inventory_index_cache[
                _MySQLSyncResultEntityTypeEnum.SERVICES
            ].get(created_service.address)
        ) is None:
            script_config = self.build_script_config(created_service.address)
            task_target = await self.get_task_target(
                created_service.node.address, created_service.node.name
            )
            services_index = await self._fetch_inventory_index(
                script_config, task_target, _MySQLSyncResultEntityTypeEnum.SERVICES
            )
            task_history_id, service_index = services_index.get(
                created_service.address, (None, {})
            )
        else:
            task_history_id, service_index = fetch_result

        service = MySQLService.model_validate(
            created_service.model_dump(exclude={"schemas"})
        )
        if (schemas_path := service_index.get("schemas_path")) and service_index.get(
            "schemas_count"
        ):
            service.schemas_index = self.stream_ndjson_file(
                task_history_id, schemas_path
            )
        return service

    async def perform_service_sync(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> None:
        """Synchronize data for a specific service.

        Updates the schemas associated with the service by comparing existing schemas
        with the updated schemas and performing necessary synchronization actions.

        :param created_service: The service instance to synchronize.
        :param updated_service: The updated service data.
        :raises TypeError: If ``updated_service`` is not a ``MySQLService``.
        """
        if not isinstance(updated_service, MySQLService):
            raise TypeError(
                f"MySQLSyncer requires a MySQLService, got "
                f"{type(updated_service).__name__}"
            )
        # Keyed by primary key, not name: a tombstone and the replacement that took
        # its name both come back from a retired-inclusive read, and only the
        # primary key tells them apart once the active row has claimed the name.
        syncable_schemas: dict[int | None, CreatedSchema] = {}
        schema_ids_by_name: dict[str, int | None] = {}
        for schema in await self.get_inventory_service_schemas(created_service.id):
            syncable_schemas[schema.id] = schema
            claim_identity(schema_ids_by_name, schema.name, schema, syncable_schemas)

        task_history_id: int | None = self._inventory_index_cache[
            _MySQLSyncResultEntityTypeEnum.SERVICES
        ].pop(created_service.address, (None,))[0]
        async for schema_index in updated_service.schemas_index:
            schema = Schema.model_validate(schema_index)
            matched_id = schema_ids_by_name.get(schema.name)
            if (created_schema := syncable_schemas.pop(matched_id, None)) is None:
                logger.info("Creating new schema: %s", schema)
                created_schema = CreatedSchema.model_validate(
                    await self.inventory_api.post(
                        f"/services/{created_service.id}/schemas/",
                        json=schema.model_dump(),
                    ),
                )
            else:
                await self._revive_if_retired(
                    SyncInventoryEntityTypeEnum.SCHEMA, created_schema
                )
            created_schema.service = created_service.model_copy(update={"schemas": []})
            if task_history_id is not None:
                self._inventory_index_cache[_MySQLSyncResultEntityTypeEnum.SCHEMAS][
                    self._build_entity_address(created_service.address, schema.name)
                ] = _MySQLFetchResult(task_history_id, schema_index)
            await self.sync_schema(created_schema)
        # An already-retired schema is absent from every fetch by construction, so
        # retiring it again would repeat on every run for as long as the tombstone
        # is kept. Nothing else is owed to it: this syncer's SERVICE-level read is
        # active-only, so prepare_sync never opened a SyncItem for a tombstone and
        # there is none to close.
        for schema in syncable_schemas.values():
            if not schema.is_retired:
                await self.retire_schema(schema)

    async def fetch_schema(self, created_schema: CreatedSchema) -> MySQLSchema:
        """Fetch updated data for a specific schema.

        Retrieves the latest information for the specified schema by executing a
        synchronization task and processing the returned table data. If the schema
        data is already cached, it uses the cached data instead of executing a new task.

        :param created_schema: The schema instance to fetch updated data for.
        :return: The updated schema data.
        """
        if (
            not (created_service := created_schema.service)
            or not created_service.address
        ):
            created_service = await self.get_inventory_service(
                created_schema.service_id,
            )
        host = created_service.address
        schema_address = self._build_entity_address(host, created_schema.name)
        if (
            fetch_result := self._inventory_index_cache[
                _MySQLSyncResultEntityTypeEnum.SCHEMAS
            ].pop(self._build_entity_address(host, created_schema.name), None)
        ) is None:
            script_config = self.build_script_config(host, schema=created_schema.name)
            task_target = await self.get_task_target(
                created_service.node.address,
                created_service.node.name,
            )
            schemas_index = await self._fetch_inventory_index(
                script_config,
                task_target,
                _MySQLSyncResultEntityTypeEnum.SCHEMAS,
                save_to_cache=False,
            )
            task_history_id, schema_index = schemas_index.get(
                schema_address, (None, {})
            )
        else:
            task_history_id, schema_index = fetch_result

        schema_data = created_schema.model_dump(exclude={"tables"})
        schema_data["address"] = schema_address
        schema = MySQLSchema.model_validate(schema_data)
        if (tables_path := schema_index.get("tables_path")) and schema_index.get(
            "tables_count"
        ):
            schema.tables_aiter = self.stream_ndjson_file(task_history_id, tables_path)
        return schema

    async def perform_schema_sync(
        self,
        created_schema: CreatedSchema,
        updated_schema: Schema,
    ) -> None:
        """Synchronize data for a specific schema.

        Updates the tables associated with the schema by comparing existing tables
        with the updated tables and performing necessary synchronization actions.

        :param created_schema: The schema instance to synchronize.
        :param updated_schema: The updated schema data.
        :raises TypeError: If ``updated_schema`` is not a ``MySQLSchema``.
        """
        if not isinstance(updated_schema, MySQLSchema):
            raise TypeError(
                f"MySQLSyncer requires a MySQLSchema, got "
                f"{type(updated_schema).__name__}"
            )
        await self.update_schema(created_schema, updated_schema)
        # Keyed by primary key for the reason the schema index above is.
        syncable_tables: dict[int | None, CreatedTable] = {}
        table_ids_by_name: dict[str, int | None] = {}
        for table in created_schema.tables:
            syncable_tables[table.id] = table
            claim_identity(table_ids_by_name, table.name, table, syncable_tables)

        async for table in updated_schema.iter_tables():
            matched_id = table_ids_by_name.get(table.name)
            if (created_table := syncable_tables.pop(matched_id, None)) is None:
                logger.info("Creating new table: %s", table)
                created_table = CreatedTable.model_validate(
                    await self.inventory_api.post(
                        f"/schemas/{created_schema.id}/tables/",
                        json=table.model_dump(),
                    ),
                )
                created_table.database = created_schema.model_copy(
                    update={"tables": []},
                )
            else:
                await self._revive_if_retired(
                    SyncInventoryEntityTypeEnum.TABLE, created_table
                )
            await self.sync_table(created_table, table)
        for table in syncable_tables.values():
            if not table.is_retired:
                await self.retire_table(table)

    async def fetch_table(self, created_table: CreatedTable) -> Table:
        """Fetch updated data for a specific table.

        Retrieves the latest information for the specified table by executing a
        synchronization task.

        :param created_table: The table instance to fetch updated data for.
        :return: The updated table data.
        """
        if (created_schema := created_table.database) and (
            created_service := created_schema.service
        ):
            if created_service.address:
                host = created_service.address
            else:
                created_service = await self.get_inventory_service(
                    created_service.id,
                )
                host = created_service.address
        else:
            created_schema = await self.get_inventory_schema(created_table.schema_id)
            created_service = await self.get_inventory_service(
                created_schema.service_id,
            )
            host = created_service.address
        script_config = self.build_script_config(
            host,
            schema=created_table.database.name,
            table=created_table.name,
        )
        meta = await self.build_meta(
            script_config,
            await self.get_task_target(
                created_service.node.address, created_service.node.name
            ),
        )
        task_result = await self.wait_for_task_output(**meta)
        table_data = json.loads(task_result.stdout)[
            _MySQLSyncResultEntityTypeEnum.TABLES
        ][self._build_entity_address(host, created_schema.name, created_table.name)]
        return Table.model_validate(table_data)

    async def perform_table_sync(
        self,
        created_table: CreatedTable,
        updated_table: Table,
    ) -> None:
        """Synchronize data for a specific table.

        Updates the table in the inventory system with the latest information.

        :param created_table: The table instance to synchronize.
        :type created_table: CreatedTable
        :param updated_table: The updated table data.
        :type updated_table: Table
        """
        await self.update_table(created_table, updated_table)

    @classmethod
    def can_sync_node(cls, node: CreatedNode) -> bool:
        """Determine if a specific node can be synchronized.

        Overrides the base method to check if the node has at least one MySQL service.

        :param node: The node instance to check.
        :type node: CreatedNode
        :return: `True` if the node can be synchronized, `False` otherwise.
        :rtype: bool
        """
        if super().can_sync_node(node):
            for service in node.services:
                if service.type == ServiceTypeEnum.MYSQL:
                    return True
        return False

    @classmethod
    def can_sync_service(cls, service: CreatedService) -> bool:
        """Determine if a specific service can be synchronized.

        Overrides the base method to check if the service is of type MySQL.

        :param service: The service instance to check.
        :type service: CreatedService
        :return: `True` if the service can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return (
            super().can_sync_service(service) and service.type == ServiceTypeEnum.MYSQL
        )

    @classmethod
    def can_sync_schema(cls, schema: CreatedSchema) -> bool:
        """Determine if a specific schema can be synchronized.

        Overrides the base method to check if the schema belongs to a MySQL service.

        :param schema: The schema instance to check.
        :type schema: CreatedSchema
        :return: `True` if the schema can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return (
            super().can_sync_schema(schema)
            and schema.service.type == ServiceTypeEnum.MYSQL
        )
