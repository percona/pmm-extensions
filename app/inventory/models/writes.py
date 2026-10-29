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

"""Define the Inventory API write payloads."""

from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    PositiveInt,
)
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from app.core.utils.date_time import utc_now
from app.core.utils.fields import NonEmptyStr, UTCDatetime
from app.inventory.constants import (
    RetirableEntityName,
    SYNC_ATTEMPT_MAX_CLOCK_SKEW,
    SYNC_HEALTH_BULK_MAX_ENTRIES,
)
from app.inventory.models.bases import (
    HostSystemObservationBase,
    NodeBase,
    SchemaBase,
    ServiceBase,
    ServiceSystemObservationBase,
    TableBase,
)
from app.inventory.models.enums import IdentityLinkDecisionEnum, SyncOutcomeEnum


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


class SyncHealthEntityWrite(SyncHealthWrite):
    """Define one entry of a bulk sync-health report.

    :param id: The entity the outcome was observed for.
    """

    id: int


#: The body of a bulk sync-health report: one entry per entity, each carrying
#: the body the single-entity route takes. Bounded so one request cannot hold
#: the session for an arbitrary number of writes.
SyncHealthBulkWrite = Annotated[
    list[SyncHealthEntityWrite],
    Field(min_length=1, max_length=SYNC_HEALTH_BULK_MAX_ENTRIES),
]


class NodeWrite(NodeBase):
    """Define the model for writing node data to the inventory.

    :param address: The network address of the node.
    :param name: The name of the node.
    :param external_id: An external identifier for the node, indexed for quick lookup.
    :param source: The source from which the node information is derived. Indexed for
        quick lookup.
    :param type: The type of the node (e.g., remote, generic). Defaults to "generic".
    """


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


class IdentityLinkDecisionWrite(SQLModel):
    """Define the request body recording one operator decision over a pairing.

    :param successor_id: The row the addressed predecessor is being paired with.
    :param decision: What the operator decided.
    """

    successor_id: int
    decision: IdentityLinkDecisionEnum


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


class InventoryCollectWrite(BaseModel):
    """Ask the inventory service to collect the tombstones nothing resolves.

    Unknown fields are rejected with HTTP 422. This request deletes rows
    irreversibly, so a client typo must never be read as an omitted field: a
    misspelled ``keep`` would otherwise arrive as an empty retained set and a
    misspelled ``dry_run`` as a real delete.

    :param retired_before: The cutoff a tombstone must predate to be eligible.
        The caller pins one value for a whole run so successive batches cannot
        drift into collecting a tombstone that was too young a moment earlier.
    :param link_pin_retired_before: The cutoff a linked tombstone must predate
        for its link to stop pinning it. Required rather than defaulted: either
        extreme a default could pick silently keeps every link's successor
        forever or releases it at once.
    :param keep: The ids the caller knows are still referenced, per entity type.
        Ancestors of a kept entity are retained without being listed.
    :param limit: The most entities to collect per type in this call.
    :param dry_run: Whether to report the eligible ids without deleting them.
        Defaults to reporting: on an irreversible endpoint the mode a caller
        reaches by omission is the one that cannot destroy anything.
    """

    model_config = ConfigDict(extra="forbid")
    retired_before: UTCDatetime
    link_pin_retired_before: UTCDatetime
    keep: dict[RetirableEntityName, list[int]] = {}
    limit: PositiveInt = 500
    dry_run: bool = True
