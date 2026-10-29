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

"""Define the Inventory API response models."""

from pydantic import BaseModel
from sqlmodel import SQLModel

from app.core.db import BaseSQLModel
from app.core.utils.fields import UTCDatetime
from app.inventory.constants import RetirableEntityName
from app.inventory.models.bases import (
    ExternalIdentityAliasBase,
    HostSystemObservationBase,
    NodeBase,
    RetiredAtBase,
    SchemaBase,
    ServiceBase,
    ServiceSystemObservationBase,
    SyncHealthBase,
    TableBase,
)
from app.inventory.models.tables import Node, Schema, Service, Table


class SyncHealthBulkResult(SQLModel):
    """Define the response to a bulk sync-health report.

    :param unresolved: The ids in the request that named no entity, in request
        order. Their entries were skipped; every other entry was applied.
    """

    unresolved: list[int] = []


class NodeResponse(BaseSQLModel, RetiredAtBase, SyncHealthBase, NodeBase):
    """Represent a node API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param address: The network address of the node.
    :param name: The name of the node.
    :param external_id: An external identifier for the node.
    :param source: The source from which the node information is derived.
    :param type: The type of the node (e.g., remote, generic).
    :param retired_at: When the node stopped being reported upstream, or None while
        it is active.
    :param last_synced_at: When a syncer last confirmed the node against its
        source, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the node is syncing cleanly.
    :param sync_failing_since: When the current run of failures began, or None
        while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    :param services: A list of services associated with the node.
    """

    services: list["Service"]


class ServiceResponse(BaseSQLModel, RetiredAtBase, SyncHealthBase, ServiceBase):
    """Define the service API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param external_id: An external identifier for the service.
    :param name: The name of the service.
    :param type: The type of the service (e.g., MYSQL, POSTGRESQL).
    :param port: The port number on which the service is running.
    :param environment: The environment in which the service is running, if set.
    :param cluster: The cluster in which the service is running, if set.
    :param replication_set: The replication set in which the service is running, if set.
    :param custom_labels: Custom labels associated with the service, if set.
    :param node_id: The unique identifier of the node on which the service is running.
    :param schemas: A list of schemas associated with the service.
    :param node: The node to which the service is associated.
    :param retired_at: When the service stopped being reported upstream, or None
        while it is active.
    :param last_synced_at: When a syncer last confirmed the service against its
        source, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the service is syncing cleanly.
    :param sync_failing_since: When the current run of failures began, or None
        while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    """

    schemas: list["Schema"]
    node: Node


class ServiceDetailResponse(ServiceResponse):
    """Define the service retrieve API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param external_id: An external identifier for the service.
    :param name: The name of the service.
    :param type: The type of the service (e.g., MYSQL, POSTGRESQL).
    :param port: The port number on which the service is running.
    :param environment: The environment in which the service is running, if set.
    :param cluster: The cluster in which the service is running, if set.
    :param replication_set: The replication set in which the service is running, if set.
    :param custom_labels: Custom labels associated with the service, if set.
    :param node_id: The unique identifier of the node on which the service is running.
    :param schemas: A list of schemas associated with the service.
    :param node: The service's node.
    """

    node: Node


class SchemaCompactResponse(BaseSQLModel, RetiredAtBase, SyncHealthBase, SchemaBase):
    """Define a compact schema response without nested tables.

    :param id: The primary key for the schema. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the schema.
    :param service_id: The unique identifier of the service to which the schema belongs.
    :param retired_at: When the schema stopped being reported upstream, or None while
        it is active.
    :param last_synced_at: When a syncer last confirmed the schema against its
        source, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the schema is syncing cleanly.
    :param sync_failing_since: When the current run of failures began, or None
        while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    """


class SchemaResponse(BaseSQLModel, RetiredAtBase, SyncHealthBase, SchemaBase):
    """Define the schema API response.

    :param id: The primary key for the schema. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the schema.
    :param service_id: The unique identifier of the service to which the schema belongs.
    :param retired_at: When the schema stopped being reported upstream, or None while
        it is active.
    :param last_synced_at: When a syncer last confirmed the schema against its
        source, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the schema is syncing cleanly.
    :param sync_failing_since: When the current run of failures began, or None
        while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    :param tables: A list of tables within the schema.
    """

    tables: list["Table"]


class SchemaDetailResponse(SchemaResponse):
    """Define the schema retrieve API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the schema.
    :param service_id: The unique identifier of the service to which the schema belongs.
    :param service: The schema's service.
    """

    service: Service


class TableResponse(BaseSQLModel, RetiredAtBase, SyncHealthBase, TableBase):
    """Define the table API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the table.
    :param create: The SQL statement used to create the table.
    :param schema_id: The foreign key referencing the schema to which the table belongs.
    :param retired_at: When the table stopped being reported upstream, or None while
        it is active.
    :param last_synced_at: When a syncer last confirmed the table against its
        source, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the table is syncing cleanly.
    :param sync_failing_since: When the current run of failures began, or None
        while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    """


class TableDetailResponse(TableResponse):
    """Define the schema retrieve API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the table.
    :param create: The SQL statement used to create the table.
    :param schema_id: The foreign key referencing the schema to which the table belongs.
    :param database: The table's schema.
    """

    database: Schema


class ExternalIdentityAliasResponse(BaseSQLModel, ExternalIdentityAliasBase):
    """Define the external-identity alias API response.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the
        current time in UTC.
    :param updated_at: The timestamp when the record is last updated.
        Automatically updated on changes.
    :param entity_type: The inventory entity type the binding names.
    :param entity_id: The primary key of the row the upstream id resolves to.
    :param source: The upstream system the identifier belongs to.
    :param external_id: The upstream identifier being bound.
    :param valid_from: When the binding took effect.
    :param valid_to: When the binding stopped applying, or None while it stands.
    :param linkage_method: How the binding came to be recorded.
    :param principal: The caller that recorded the binding.
    """


class NodeIdentityCandidateResponse(SQLModel):
    """Pair a node with the successor a re-registration may have split it into.

    :param predecessor: The older row — the one every PMM Extensions reference persisted
        before the re-registration resolves through, and so the one a
        confirmation keeps.
    :param successor: The newer row PMM created when the node re-registered.
    :param matched_on: The signals that agreed, informational only. Detection
        never requires an address match, because PMM discards the address on a
        non-``--force`` re-registration.
    """

    predecessor: NodeResponse
    successor: NodeResponse
    matched_on: list[str]


class ServiceIdentityCandidateResponse(SQLModel):
    """Pair a service with the successor a re-registration may have split it into.

    :param predecessor: The older row a confirmation keeps.
    :param successor: The newer row PMM created.
    :param matched_on: The signals that agreed, informational only.
    """

    predecessor: ServiceResponse
    successor: ServiceResponse
    matched_on: list[str]


class HostSystemObservationResponse(BaseSQLModel, HostSystemObservationBase):
    """Define the response model for host system observation data.

    :param id: The primary key of the observation record.
    :param created_at: When the record was created.
    :param updated_at: When the record was last updated.
    :param node_id: The unique identifier of the observed node.
    :param os_version: The observed operating system version.
    :param installed_packages: Snapshot of installed packages.
    :param config: Snapshot of host configuration.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        node, if observed.
    :param observed_at: When this observation was collected.
    """


class HostSystemObservationSummaryResponse(SQLModel):
    """Project a host observation onto the fields a capability lookup needs.

    Deliberately excludes ``installed_packages`` and ``config``. The first runs to
    hundreds of rows per node, which is what makes the full response unsuitable for
    a fleet-wide fetch; the second is small but has no consumer here.

    :param node_id: The unique identifier of the observed node.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        node, if observed.
    :param observed_at: When this observation was collected.
    """

    node_id: int
    can_elevate: bool | None = None
    observed_at: UTCDatetime


class ServiceSystemObservationResponse(BaseSQLModel, ServiceSystemObservationBase):
    """Define the response model for service system observation data.

    :param id: The primary key of the observation record.
    :type id: int | None
    :param created_at: When the record was created.
    :type created_at: UTCDatetime
    :param updated_at: When the record was last updated.
    :type updated_at: UTCDatetime | None
    :param service_id: The unique identifier of the observed service.
    :type service_id: int
    :param db_engine_version: The observed database engine version.
    :type db_engine_version: NonEmptyStr
    :param observed_at: When this observation was collected.
    :type observed_at: UTCDatetime
    """


class InventoryCollectResponse(BaseModel):
    """Report what a collection call deleted, or would have deleted.

    :param deleted: The collected ids per entity type, exhaustive for this
        call. On a dry run these are the ids the equivalent real call would
        delete. A type the walk stopped before reporting is empty rather than
        absent.
    :param remaining: Whether any entity type filled its ``limit``, meaning more
        tombstones are waiting for the next batch.
    """

    deleted: dict[RetirableEntityName, list[int]]
    remaining: bool
