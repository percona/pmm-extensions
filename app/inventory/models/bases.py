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

"""Define the base models and field sets the Inventory tables share with their payloads."""

from typing import Any, Self

from pydantic import model_validator, NonNegativeInt
from sqlalchemy import Column, JSON, Text, text
from sqlalchemy import Enum as EnumField
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from app.core.db import BaseSQLModel
from app.core.db.models import DateTimeWithTimezone
from app.core.utils.fields import ArbitraryMapping, NonEmptyStr, UTCDatetime
from app.inventory.constants import ACTIVE_RETIREMENT_KEY, RetirableEntityName
from app.inventory.models.enums import LinkageMethodEnum, ServiceTypeEnum, SourceEnum

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
    external_id: NonEmptyStr
    source: SourceEnum = SQLField(
        sa_column=Column(
            EnumField(SourceEnum, native_enum=False, create_constraint=True),
            nullable=False,
        ),
    )
    type: NonEmptyStr = SQLField(
        default="generic"
    )  # TODO: Enum with allowed values  # noqa: TD002, TD003


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

    external_id: NonEmptyStr
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


class SchemaBase(SQLModel):
    """Define the base structure for schema-related operations.

    :param name: The name of the schema.
    :param service_id: The foreign key referencing the service to which the schema
        belongs.
    """

    name: NonEmptyStr
    service_id: int = SQLField(foreign_key="service.id", index=True, ondelete="CASCADE")


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
