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

"""Define models for the Inventory API."""

from datetime import datetime
from enum import auto, StrEnum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
    NonNegativeInt,
    PositiveInt,
)
from sqlalchemy import (
    CheckConstraint,
    Column,
    column,
    Index,
    JSON,
    or_,
    Text,
    text,
)
from sqlalchemy import Enum as EnumField
from sqlmodel import Field as SQLField
from sqlmodel import Relationship, SQLModel

from app.core.db import BaseSQLModel
from app.core.db.models import DateTimeWithTimezone
from app.core.utils.date_time import utc_now
from app.core.utils.fields import ArbitraryMapping, NonEmptyStr, UTCDatetime
from app.inventory.constants import (
    ACTIVE_RETIREMENT_KEY,
    RetirableEntityName,
    SYNC_ATTEMPT_MAX_CLOCK_SKEW,
)

#: Predicate narrowing the collection-scan indexes to the tombstones alone.
#: Active rows are the overwhelming majority and can never be returned by that
#: scan, so keeping them out holds the index size to the retired set.
RETIRED_ROWS_ONLY = text("retired_at IS NOT NULL")


class RetiredAtBase(SQLModel):
    """Expose the retirement timestamp of an entity that can be tombstoned.

    :param retired_at: When the entity stopped being reported by its upstream
        source, or None while it is active.
    """

    retired_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )


class SyncHealthBase(SQLModel):
    """Expose how recently, and how successfully, a syncer last confirmed an entity.

    Written only by the syncer that mirrors the entity's own fields, so
    ``last_synced_at`` reads as "these mirrored values were confirmed against
    their source at T" rather than "some syncer walked past this row".

    :param last_synced_at: When a syncer last compared this entity against its
        source and updated it, or None if that has never happened.
    :param last_sync_error: The message from the most recent failed attempt, or
        None while the entity is syncing cleanly.
    :param sync_failing_since: When the current run of failures began — the
        first failure after the last success — or None while not failing.
    :param consecutive_failures: Failed attempts since the last success.
    :param newest_attempt_at: When the newest accepted attempt began, whatever
        its outcome, or ``None`` if none has been reported, or the upgrade
        time for a row that was failing when the column was added.
    """

    last_synced_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    last_sync_error: str | None = SQLField(default=None, sa_type=Text)
    sync_failing_since: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    consecutive_failures: NonNegativeInt = SQLField(default=0, nullable=False)
    newest_attempt_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )


class RetirableSQLModel(RetiredAtBase, BaseSQLModel):
    """Store the retirement state of a tombstoned entity.

    An entity that vanishes upstream is retired rather than deleted, so the
    references PMM Extensions persisted to it keep resolving. Because a replacement may
    reuse the retired row's unique key, ``retirement_key`` joins every unique
    index: it holds :data:`ACTIVE_RETIREMENT_KEY` while the row is active and
    the row's own primary key once retired, which keeps active rows mutually
    exclusive while letting any number of tombstones share their key.

    :param retired_at: When the entity stopped being reported by its upstream
        source, or None while it is active.
    :param retirement_key: The discriminator carried inside every unique index.
        Excluded from serialization: the responses nest these table models, and a
        database-internal discriminator has no business in the published schema.
    """

    retirement_key: int = SQLField(
        default=ACTIVE_RETIREMENT_KEY, nullable=False, exclude=True
    )


class SourceEnum(StrEnum):
    """Enumeration of possible data sources for a node.

    :cvar PMM: Represents the PMM data source.
    :vartype PMM: str
    """

    PMM = auto()


class ServiceTypeEnum(StrEnum):
    """Enumerate the supported service types.

    :cvar MYSQL: Represents the MySQL service type.
    :vartype MYSQL: str
    :cvar POSTGRESQL: Represents the PostgreSQL service type.
    :vartype POSTGRESQL: str
    :cvar MONGODB: Represents the MongoDB service type.
    :vartype MONGODB: str
    :cvar PROXYSQL: Represents the ProxySQL service type.
    :vartype PROXYSQL: str
    :cvar HAPROXY: Represents the HAProxy service type.
    :vartype HAPROXY: str
    :cvar EXTERNAL: Represents an external service type.
    :vartype EXTERNAL: str
    :cvar VALKEY: Represents the Valkey service type.
    :vartype VALKEY: str
    """

    MYSQL = auto()
    POSTGRESQL = auto()
    MONGODB = auto()
    PROXYSQL = auto()
    HAPROXY = auto()
    EXTERNAL = auto()
    VALKEY = auto()


class LinkageMethodEnum(StrEnum):
    """Enumerate how an external-identity binding came to be recorded.

    Every member names an operator action: nothing here records a binding the
    syncer made on its own, because ordinary sync creation writes no alias row.

    Values are spelled out rather than derived with ``auto()``. The column
    persists the member *name*, but the API serializes the *value*, so under
    ``auto()`` the two move in opposite ways when a member is renamed: the
    stored form follows the rename while the published one silently changes
    with it. Pinning the value holds the wire contract — this enum reaches the
    generated API client — and leaves a rename a database concern alone.
    """

    OPERATOR_CONFIRMATION = "operator_confirmation"
    OPERATOR_UNLINK = "operator_unlink"


class IdentityLinkDecisionEnum(StrEnum):
    """Enumerate the decisions an operator may record against a candidate pairing.

    :cvar CONFIRMED: The pairing names one machine, and the link stands.
    :cvar REJECTED: The pairing names two machines, so stop suggesting it.
    :cvar UNLINKED: A standing confirmation was reversed.

    Values are spelled out for the reason given on
    :class:`LinkageMethodEnum`, and it binds harder here: this enum is also a
    *request* body field, so a rename would reject the payloads callers were
    already sending.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNLINKED = "unlinked"


class SyncOutcomeEnum(StrEnum):
    """Enumerate the outcomes a syncer reports for one entity's sync attempt.

    :cvar SUCCESS: The entity was compared against its source and updated.
    :cvar FAILURE: The attempt raised before the comparison completed.
    """

    SUCCESS = "success"
    FAILURE = "failure"


class SyncHealthWrite(SQLModel):
    """Define the body reporting one entity's sync outcome.

    :param outcome: Whether the attempt succeeded or failed.
    :param error: The failure's message, never empty. Required on FAILURE,
        absent on SUCCESS.
    :param attempted_at: When the syncer began this attempt. Stamped as
        ``last_synced_at`` on success and as ``newest_attempt_at`` when newer,
        and used to order reports so a late-arriving one from an older attempt
        cannot overwrite a newer one. Refused when it sits further ahead of
        this service's clock than the tolerated skew, since nothing later could
        then supersede it.
    """

    outcome: SyncOutcomeEnum
    error: NonEmptyStr | None = None
    attempted_at: UTCDatetime

    @model_validator(mode="after")
    def _validate_error_matches_outcome(self) -> Self:
        if self.outcome is SyncOutcomeEnum.FAILURE and self.error is None:
            raise ValueError("error is required when outcome is failure")
        if self.outcome is SyncOutcomeEnum.SUCCESS and self.error is not None:
            raise ValueError("error must be omitted when outcome is success")
        return self

    @field_validator("attempted_at")
    @classmethod
    def _validate_attempted_at_is_not_ahead(cls, value: datetime) -> datetime:
        if value > utc_now() + SYNC_ATTEMPT_MAX_CLOCK_SKEW:
            raise ValueError(
                "attempted_at is further ahead of server time than the "
                "tolerated clock skew"
            )
        return value


class NodeBase(SQLModel):
    """Define the base structure for node-related operations.

    :param address: The network address of the node.
    :param name: The name of the node.
    :param external_id: An external identifier for the node, indexed for quick lookup.
    :param source: The source from which the node information is derived. Indexed for
        quick lookup.
    :param type: The type of the node (e.g., remote, generic). Defaults to "generic".
    """

    address: NonEmptyStr
    name: NonEmptyStr
    external_id: NonEmptyStr = SQLField(index=True)
    source: SourceEnum = SQLField(
        sa_column=Column(EnumField(SourceEnum), nullable=False),
    )
    type: NonEmptyStr = SQLField(
        default="generic"
    )  # TODO: Enum with allowed values  # noqa: TD002, TD003


class Node(NodeBase, SyncHealthBase, RetirableSQLModel, table=True):
    """Represent a node in the inventory.

    :param address: The network address of the node.
    :param name: The name of the node.
    :param external_id: An external identifier for the node. Must be unique for source,
        as defined by composite index ix_node_external_id_source.
    :param source: The source from which the node information is derived. Must be unique
        for external_id, as defined by composite index ix_node_external_id_source.
    :param type: The type of the node (e.g., remote, generic).
    :param retired_at: When the node stopped being reported upstream, or None while
        it is active.
    :param retirement_key: The discriminator carried inside every unique index.
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

    __table_args__ = (
        Index(
            "ix_node_external_id_source",
            "external_id",
            "source",
            "retirement_key",
            unique=True,
        ),
        Index(
            "ix_node_retired_at_not_null",
            "retired_at",
            postgresql_where=RETIRED_ROWS_ONLY,
            sqlite_where=RETIRED_ROWS_ONLY,
        ),
    )
    services: list["Service"] = Relationship(back_populates="node", cascade_delete=True)


class NodeWrite(NodeBase):
    """Define the model for writing node data to the inventory.

    :param address: The network address of the node.
    :param name: The name of the node.
    :param external_id: An external identifier for the node, indexed for quick lookup.
    :param source: The source from which the node information is derived. Indexed for
        quick lookup.
    :param type: The type of the node (e.g., remote, generic). Defaults to "generic".
    """


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


class ServiceBase(SQLModel):
    """Define the base structure for service-related operations.

    :param external_id: An external identifier for the service, indexed for quick
        lookup.
    :param name: The name of the service.
    :param type: The type of the service (e.g., MYSQL, POSTGRESQL).
    :param port: The port number on which the service is running. Defaults to None.
    :param environment: The environment in which the service is running (e.g.,
        production, staging). Defaults to None.
    :param cluster: The cluster in which the service is running. Defaults to None.
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :param node_id: The foreign key referencing the node to which the service belongs.
    """

    external_id: NonEmptyStr = SQLField(index=True)
    name: NonEmptyStr
    type: ServiceTypeEnum = SQLField(
        sa_column=Column(EnumField(ServiceTypeEnum, native_enum=False), nullable=False),
    )
    port: int | None = None
    environment: str | None = (
        None  # TODO: Enum with allowed values  # noqa: TD002, TD003
    )
    cluster: str | None = None
    replication_set: str | None = None
    custom_labels: ArbitraryMapping | None = SQLField(
        default=None,
        sa_column=Column(JSON),
    )
    node_id: int = SQLField(foreign_key="node.id", index=True, ondelete="CASCADE")


class ServiceWrite(ServiceBase):
    """Define the model for writing service data to the inventory.

    :param external_id: An external identifier for the service, indexed for quick
        lookup.
    :param name: The name of the service.
    :param type: The type of the service (e.g., MYSQL, POSTGRESQL).
    :param port: The port number on which the service is running. Defaults to None.
    :param environment: The environment in which the service is running (e.g.,
        production, staging). Defaults to None.
    :param cluster: The cluster in which the service is running. Defaults to None.
    :param replication_set: The replication set in which the service is running. Defaults to None.
    :param custom_labels: Custom labels associated with the service. Defaults to None.
    :param node_id: The foreign key referencing the node to which the service belongs.
        Defaults to None.
    """

    node_id: int | None = SQLField(
        default=None,
        foreign_key="node.id",
        index=True,
        ondelete="CASCADE",
    )


class Service(RetirableSQLModel, SyncHealthBase, ServiceBase, table=True):
    """Represent a service running on a node in the inventory.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param external_id: An external identifier for the service. Must be unique for
        node_id, as defined by composite index ix_service_external_id_node_id.
    :param name: The name of the service.
    :param type: The type of the service (e.g., MYSQL, POSTGRESQL).
    :param port: The port number on which the service is running.
    :param environment: The environment in which the service is running, if set.
    :param cluster: The cluster in which the service is running, if set.
    :param replication_set: The replication set in which the service is running, if set.
    :param custom_labels: Custom labels associated with the service, if set.
    :param node_id: The unique identifier of the node on which the service is running.
        Must be unique for external_id, as defined by composite index
        ix_service_external_id_node_id.
    :param node: The node to which the service is associated.
    :param retired_at: When the service stopped being reported upstream, or None
        while it is active.
    :param retirement_key: The discriminator carried inside every unique index.
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
    :param schemas: A list of schemas associated with the service.
    """

    __table_args__ = (
        Index(
            "ix_service_external_id_node_id",
            "external_id",
            "node_id",
            "retirement_key",
            unique=True,
        ),
        Index(
            "ix_service_retired_at_not_null",
            "retired_at",
            postgresql_where=RETIRED_ROWS_ONLY,
            sqlite_where=RETIRED_ROWS_ONLY,
        ),
    )

    node: Node = Relationship(back_populates="services")
    schemas: list["Schema"] = Relationship(
        back_populates="service",
        cascade_delete=True,
    )


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


class SchemaBase(SQLModel):
    """Define the base structure for schema-related operations.

    :param name: The name of the schema.
    :param service_id: The foreign key referencing the service to which the schema
        belongs.
    """

    name: NonEmptyStr
    service_id: int = SQLField(foreign_key="service.id", index=True, ondelete="CASCADE")


class Schema(RetirableSQLModel, SyncHealthBase, SchemaBase, table=True):
    """Represent a database schema within a service.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the schema. Must be unique for service_id, as defined by
        composite index ix_schema_name_service_id.
    :param service_id: The unique identifier of the service to which the schema belongs.
        Must be unique for name, as defined by composite index
        ix_schema_name_service_id.
    :param service: The service to which the schema is associated.
    :param retired_at: When the schema stopped being reported upstream, or None
        while it is active.
    :param retirement_key: The discriminator carried inside every unique index.
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

    __table_args__ = (
        Index(
            "ix_schema_name_service_id",
            "name",
            "service_id",
            "retirement_key",
            unique=True,
        ),
        Index(
            "ix_schema_retired_at_not_null",
            "retired_at",
            postgresql_where=RETIRED_ROWS_ONLY,
            sqlite_where=RETIRED_ROWS_ONLY,
        ),
    )
    service: Service = Relationship(back_populates="schemas")
    tables: list["Table"] = Relationship(back_populates="database", cascade_delete=True)


class SchemaWrite(SchemaBase):
    """Define the model for writing schema data to the inventory.

    :param name: The name of the schema.
    :type name: NonEmptyStr
    :param service_id: The foreign key referencing the service to which the schema
        belongs. Defaults to None.
    :type service_id: int | None
    """

    service_id: int | None = SQLField(
        default=None,
        foreign_key="service.id",
        index=True,
        ondelete="CASCADE",
    )


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


class TableBase(SQLModel):
    """Define the base structure for table-related operations.

    :param name: The name of the table.
    :param create: The SQL statement used to create the table.
    :param schema_id: The foreign key referencing the schema to which the table belongs.
    :param keys: A dictionary containing details about table keys (e.g., primary,
        unique).
    """

    name: NonEmptyStr
    create: NonEmptyStr = SQLField(sa_type=Text)
    schema_id: int = SQLField(foreign_key="schema.id", index=True, ondelete="CASCADE")
    keys: dict[str, Any] = SQLField(
        sa_column=Column(JSON, nullable=False),
        schema_extra={"json_schema_extra": {"additionalProperties": True}},
    )


class Table(RetirableSQLModel, SyncHealthBase, TableBase, table=True):
    """Represent a table within a schema.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param name: The name of the table. Must be unique for schema_id, as defined by
        composite index ix_table_name_schema_id.
    :param create: The SQL statement used to create the table.
    :param schema_id: The unique identifier of the schema to which the table belongs.
        Must be unique for name, as defined by composite index ix_table_name_schema_id.
    :param retired_at: When the table stopped being reported upstream, or None while
        it is active.
    :param retirement_key: The discriminator carried inside every unique index.
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
    :param database: The schema to which the table is associated.
    """

    __table_args__ = (
        Index(
            "ix_table_name_schema_id",
            "name",
            "schema_id",
            "retirement_key",
            unique=True,
        ),
        Index(
            "ix_table_retired_at_not_null",
            "retired_at",
            postgresql_where=RETIRED_ROWS_ONLY,
            sqlite_where=RETIRED_ROWS_ONLY,
        ),
    )
    database: Schema = Relationship(back_populates="tables")


class TableWrite(TableBase):
    """Define the model for writing table data to the inventory.

    :param name: The name of the table.
    :param create: The SQL statement used to create the table.
    :param schema_id: The foreign key referencing the schema to which the table belongs.
    """

    schema_id: int | None = SQLField(
        default=None,
        foreign_key="schema.id",
        index=True,
        ondelete="CASCADE",
    )


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


class ExternalIdentityAliasBase(SQLModel):
    """Define the base structure for an external-identity binding record.

    :param entity_type: The inventory entity type the binding names.
    :param entity_id: The primary key of the row the upstream id resolves to.
    :param source: The upstream system the identifier belongs to.
    :param external_id: The upstream identifier being bound.
    :param valid_from: When the binding took effect.
    :param valid_to: When the binding stopped applying, or None while it stands.
    :param linkage_method: How the binding came to be recorded.
    :param principal: The caller that recorded the binding.
    """

    entity_type: RetirableEntityName = SQLField(
        sa_column=Column(
            EnumField(
                RetirableEntityName,
                native_enum=False,
                create_constraint=True,
                name="alias_entity_type",
            ),
            nullable=False,
        )
    )
    entity_id: int
    source: SourceEnum = SQLField(
        sa_column=Column(
            EnumField(
                SourceEnum,
                native_enum=False,
                create_constraint=True,
                name="alias_source",
            ),
            nullable=False,
        )
    )
    external_id: NonEmptyStr
    valid_from: UTCDatetime = SQLField(sa_type=DateTimeWithTimezone)
    valid_to: UTCDatetime | None = SQLField(default=None, sa_type=DateTimeWithTimezone)
    linkage_method: LinkageMethodEnum = SQLField(
        sa_column=Column(
            EnumField(
                LinkageMethodEnum,
                native_enum=False,
                create_constraint=True,
                name="alias_linkage_method",
            ),
            nullable=False,
        )
    )
    principal: NonEmptyStr


class ExternalIdentityAlias(ExternalIdentityAliasBase, BaseSQLModel, table=True):
    """Bind one upstream identifier to one inventory row over a validity interval.

    Append-only. A binding that is open at write time carries ``valid_to = None``
    and is closed by appending a superseding record rather than by an update, so
    a confirmation and its reversal both stay readable afterwards. An identifier
    is bound to the row named by its record with the greatest
    ``(valid_from, id)``, and then resolves to whichever row has since absorbed
    that one through a standing confirmation — a confirmation transfers only the
    identifier its successor currently holds, so the rest of that successor's
    bindings stay where they are and are followed rather than rewritten. An
    identifier with no record at all resolves by the ``external_id`` column,
    which is the overwhelming majority.

    ``entity_id`` carries no foreign key on purpose: the column is polymorphic
    over ``node`` and ``service``, so no single target exists, and its absence
    keeps the trail readable once collection deletes a row the history names.
    Neither index is unique, equally on purpose —
    ``BaseSQLModelManager.save`` rebuilds equality filters from every unique
    index and would refuse the superseding record that expresses closure.

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

    __table_args__ = (
        Index("ix_alias_source_external_id", "source", "external_id"),
        Index("ix_alias_entity", "entity_type", "entity_id"),
    )


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


class IdentityLinkDecision(BaseSQLModel, table=True):
    """Record one operator decision over one candidate pairing, append-only.

    A pairing's current state is its most recent row, so nothing is ever updated
    or deleted here either. ``predecessor_external_id`` and
    ``predecessor_retired_at`` describe the predecessor immediately before a
    confirmation and are read back only off a ``CONFIRMED`` row; they are what
    lets a reversal restore the exact pre-confirmation state rather than an
    approximation of it.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the
        current time in UTC.
    :param updated_at: The timestamp when the record is last updated.
        Automatically updated on changes.
    :param entity_type: The inventory entity type the pairing names.
    :param predecessor_id: The older row of the pairing, the one a confirmation
        keeps.
    :param successor_id: The newer row of the pairing.
    :param decision: What the operator decided.
    :param principal: The caller that recorded the decision.
    :param predecessor_external_id: The identifier the predecessor held before a
        confirmation transferred the successor's onto it, or None on a decision
        that transferred nothing.
    :param predecessor_retired_at: The predecessor's retirement timestamp before
        a confirmation revived it, or None when it was active or nothing was
        revived.
    """

    __table_args__ = (
        Index(
            "ix_link_decision_pair",
            "entity_type",
            "predecessor_id",
            "successor_id",
        ),
        Index("ix_link_decision_successor", "entity_type", "successor_id"),
    )

    entity_type: RetirableEntityName = SQLField(
        sa_column=Column(
            EnumField(
                RetirableEntityName,
                native_enum=False,
                create_constraint=True,
                name="link_decision_entity_type",
            ),
            nullable=False,
        )
    )
    predecessor_id: int
    successor_id: int
    decision: IdentityLinkDecisionEnum = SQLField(
        sa_column=Column(
            EnumField(
                IdentityLinkDecisionEnum,
                native_enum=False,
                create_constraint=True,
                name="link_decision_decision",
            ),
            nullable=False,
        )
    )
    principal: NonEmptyStr
    predecessor_external_id: NonEmptyStr | None = SQLField(default=None)
    predecessor_retired_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )


class IdentityLinkDecisionWrite(SQLModel):
    """Define the request body recording one operator decision over a pairing.

    :param successor_id: The row the addressed predecessor is being paired with.
    :param decision: What the operator decided.
    """

    successor_id: int
    decision: IdentityLinkDecisionEnum


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


class HostSystemObservationBase(SQLModel):
    """Define the base structure for host-level system observation data.

    :param node_id: The foreign key referencing the node this observation belongs to.
    :param os_version: The observed operating system version. Defaults to None.
    :param installed_packages: Snapshot of installed packages. Defaults to None.
    :param config: Snapshot of host configuration. Defaults to None.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        node: uid 0, or a bare ``sudo`` on PATH. ``None`` when never observed.
        Defaults to ``None``.
    :param observed_at: When this observation was collected (domain provenance).
    """

    node_id: int = SQLField(
        foreign_key="node.id",
        unique=True,
        index=True,
        ondelete="CASCADE",
    )
    os_version: str | None = None
    installed_packages: list[ArbitraryMapping] | None = SQLField(
        default=None,
        sa_column=Column(JSON(none_as_null=True)),
    )
    config: ArbitraryMapping | None = SQLField(
        default=None,
        sa_column=Column(JSON(none_as_null=True)),
    )
    can_elevate: bool | None = None
    observed_at: UTCDatetime = SQLField(sa_type=DateTimeWithTimezone)

    @model_validator(mode="after")
    def validate_at_least_one_observation_field(self) -> Self:
        """Ensure at least one observed fact is set.

        :return: The validated instance.
        :raises ValueError: If every observation field is unset.
        """
        if all(getattr(self, name) is None for name in HOST_OBSERVATION_FIELD_NAMES):
            raise ValueError(
                f"At least one of {', '.join(sorted(HOST_OBSERVATION_FIELD_NAMES))} "
                "must be set",
            )
        return self


#: Fields on a host observation that record a fact rather than identify or date one.
#: Derived from the declaring base so the model stays the single source of truth as
#: fields change. It must NOT be derived from a concrete subclass: the table and
#: response models also inherit ``BaseSQLModel``'s ``id`` / ``created_at`` /
#: ``updated_at``, and an always-populated ``created_at`` would satisfy the validator
#: for an otherwise empty observation.
HOST_OBSERVATION_FIELD_NAMES = frozenset(HostSystemObservationBase.model_fields) - {
    "node_id",
    "observed_at",
}

#: Name of the CHECK enforcing the host observation's minimum content, shared with
#: the revision that creates it so a downgrade can drop it by name.
HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT = "ck_hostsystemobservation_at_least_one_fact"


def host_observation_min_content_check() -> CheckConstraint:
    """Build the table-level guard mirroring the at-least-one-fact validator.

    The columns come from :data:`HOST_OBSERVATION_FIELD_NAMES`, so this
    declaration and the Pydantic validator stay in lockstep as fields are added.
    A migrated database does not: the revision that creates the CHECK spells its
    columns out, so a new fact column reaches an existing deployment only
    through a revision of its own. Sorting makes the rendered expression
    deterministic across interpreter runs, which a frozenset's iteration order
    is not.

    A plain ``IS NOT NULL`` disjunction covers the JSON-typed facts only because
    they are declared ``JSON(none_as_null=True)``. SQLAlchemy's default persists
    an unset JSON value as the JSON text ``null``, which is not SQL NULL and so
    would satisfy this CHECK on a row carrying no fact at all.

    :return: A CHECK requiring at least one observed fact to be non-NULL.
    """
    return CheckConstraint(
        or_(
            *(
                column(name).is_not(None)
                for name in sorted(HOST_OBSERVATION_FIELD_NAMES)
            )
        ),
        name=HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT,
    )


class HostSystemObservation(BaseSQLModel, HostSystemObservationBase, table=True):
    """Store host-level system facts for a node (one snapshot per node).

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param node_id: The unique identifier of the observed node. At most one observation
        row per node.
    :param os_version: The observed operating system version, if set.
    :param installed_packages: Snapshot of installed packages, if set.
    :param config: Snapshot of host configuration, if set.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        node, if observed.
    :param observed_at: When this observation was collected.
    """

    __table_args__ = (host_observation_min_content_check(),)


class HostSystemObservationWrite(HostSystemObservationBase):
    """Define the model for writing host system observation data to the inventory.

    :param node_id: The foreign key referencing the node. Defaults to None.
    :param os_version: The observed operating system version. Defaults to None.
    :param installed_packages: Snapshot of installed packages. Defaults to None.
    :param config: Snapshot of host configuration. Defaults to None.
    :param can_elevate: Whether a ``sudo``-prefixed command can start on this
        node. Defaults to ``None``.
    :param observed_at: When this observation was collected.
    """

    node_id: int | None = SQLField(
        default=None,
        foreign_key="node.id",
        index=True,
        ondelete="CASCADE",
    )


class ServiceSystemObservationBase(SQLModel):
    """Define the base structure for service-level system observation data.

    :param service_id: The foreign key referencing the service this observation belongs
        to.
    :param db_engine_version: The observed database engine version.
    :param observed_at: When this observation was collected (domain provenance).
    """

    service_id: int = SQLField(
        foreign_key="service.id",
        unique=True,
        index=True,
        ondelete="CASCADE",
    )
    db_engine_version: NonEmptyStr
    observed_at: UTCDatetime = SQLField(sa_type=DateTimeWithTimezone)


class ServiceSystemObservation(BaseSQLModel, ServiceSystemObservationBase, table=True):
    """Store service-level system facts (one snapshot per service).

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param service_id: The unique identifier of the observed service. At most one
        observation row per service.
    :param db_engine_version: The observed database engine version.
    :param observed_at: When this observation was collected.
    """


class ServiceSystemObservationWrite(ServiceSystemObservationBase):
    """Define the model for writing service system observation data to the inventory.

    :param service_id: The foreign key referencing the service. Defaults to None.
    :type service_id: int | None
    :param db_engine_version: The observed database engine version.
    :type db_engine_version: NonEmptyStr
    :param observed_at: When this observation was collected.
    :type observed_at: UTCDatetime
    """

    service_id: int | None = SQLField(
        default=None,
        foreign_key="service.id",
        index=True,
        ondelete="CASCADE",
    )


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


class InventoryCollectWrite(BaseModel):
    """Ask the inventory service to collect the tombstones nothing resolves.

    Unknown fields are rejected with HTTP 422. This request deletes rows
    irreversibly, so a client typo must never be read as an omitted field: a
    misspelled ``keep`` would otherwise arrive as an empty retained set and a
    misspelled ``dry_run`` as a real delete.

    :param retired_before: The cutoff a tombstone must predate to be eligible.
        The caller pins one value for a whole run so successive batches cannot
        drift into collecting a tombstone that was too young a moment earlier.
    :param keep: The ids the caller knows are still referenced, per entity type.
        Ancestors of a kept entity are retained without being listed.
    :param limit: The most entities to collect per type in this call.
    :param dry_run: Whether to report the eligible ids without deleting them.
        Defaults to reporting: on an irreversible endpoint the mode a caller
        reaches by omission is the one that cannot destroy anything.
    """

    model_config = ConfigDict(extra="forbid")
    retired_before: UTCDatetime
    keep: dict[RetirableEntityName, list[int]] = {}
    limit: PositiveInt = 500
    dry_run: bool = True


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
