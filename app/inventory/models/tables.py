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

"""Define the Inventory database tables."""

from sqlalchemy import CheckConstraint, Column, column, Index, or_
from sqlalchemy import Enum as EnumField
from sqlmodel import Field as SQLField
from sqlmodel import Relationship

from app.core.db import BaseSQLModel
from app.core.db.models import DateTimeWithTimezone
from app.core.utils.fields import NonEmptyStr, UTCDatetime
from app.inventory.constants import RetirableEntityName
from app.inventory.models.bases import (
    ExternalIdentityAliasBase,
    HOST_OBSERVATION_FIELD_NAMES,
    HostSystemObservationBase,
    NodeBase,
    RetirableSQLModel,
    RETIRED_ROWS_ONLY,
    SchemaBase,
    ServiceBase,
    ServiceSystemObservationBase,
    SyncHealthBase,
    TableBase,
)
from app.inventory.models.enums import IdentityLinkDecisionEnum


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
