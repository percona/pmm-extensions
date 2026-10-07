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

"""Define models for the PMM Extensions app."""

from datetime import datetime
from enum import auto, IntEnum, StrEnum
from typing import Self

from pydantic import (
    model_validator,
    UUID4,
)
from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy import Enum as EnumField
from sqlmodel import Field as SQLField
from sqlmodel import Relationship, SQLModel

from app.core.db.models import BaseSQLModel, BaseUUIDSQLModel
from app.core.utils.date_time import make_datetime_utc
from app.core.utils.fields import NonEmptyStr


class AlertServiceType(StrEnum):
    """Enumerate the supported service types for alert-related features.

    :cvar GENERIC: Generic service type.
    :vartype GENERIC: str
    :cvar MYSQL: MySQL service type.
    :vartype MYSQL: str
    :cvar MONGODB: MongoDB service type.
    :vartype MONGODB: str
    :cvar POSTGRESQL: PostgreSQL service type.
    :vartype POSTGRESQL: str
    """

    GENERIC = "generic"
    MYSQL = "mysql"
    MONGODB = "mongodb"
    POSTGRESQL = "postgresql"

    @property
    def label(self) -> str:
        """Return the display label with correct product-name capitalization.

        :return: The human-readable service type name.
        :rtype: str
        """
        return _ALERT_SERVICE_TYPE_LABELS[self.value]


_ALERT_SERVICE_TYPE_LABELS = {
    "generic": "Generic",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "postgresql": "PostgreSQL",
}


class SyncInventoryEntityTypeEnum(IntEnum):
    """Enumerate the types of inventory entities that can be synchronized.

    :cvar INVENTORY: Represents the entire inventory.
    :vartype INVENTORY: int
    :cvar NODE: Represents a node within the inventory.
    :vartype NODE: int
    :cvar SERVICE: Represents a service within a node.
    :vartype SERVICE: int
    :cvar SCHEMA: Represents a schema within a service.
    :vartype SCHEMA: int
    :cvar TABLE: Represents a table within a schema.
    :vartype TABLE: int
    """

    INVENTORY = auto()
    NODE = auto()
    SERVICE = auto()
    SCHEMA = auto()
    TABLE = auto()


class SyncStatusEnum(StrEnum):
    """Enumerate the possible statuses of a synchronization process.

    :cvar PENDING: The synchronization is pending.
    :vartype PENDING: str
    :cvar RUNNING: The synchronization is currently running.
    :vartype RUNNING: str
    :cvar SUCCESS: The synchronization completed successfully.
    :vartype SUCCESS: str
    :cvar FAILED: The synchronization failed.
    :vartype FAILED: str
    """

    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()


class SyncInstanceBase(SQLModel):
    """Define the base structure for a synchronization instance.

    This model provides the foundational fields required for tracking a synchronization
    instance, including the synchronizer responsible for the synchronization.

    :param syncer: The name of the synchronizer responsible for this synchronization.
        Indexed for quick lookup.
    :param status: The run-level status of the synchronization. Defaults to PENDING.
    :param snapshot_complete: Whether the run observed a complete generation of the
        remote inventory, or ``None`` when the syncer does not produce one.
    """

    syncer: NonEmptyStr = SQLField(index=True)
    status: SyncStatusEnum = SQLField(
        default=SyncStatusEnum.PENDING,
        sa_column=Column(
            EnumField(SyncStatusEnum, native_enum=False, create_constraint=True),
            nullable=False,
            index=True,
        ),
    )
    snapshot_complete: bool | None = SQLField(default=None, index=True)


class SyncInstance(BaseUUIDSQLModel, SyncInstanceBase, table=True):
    """Represent a synchronization instance in the PMM Extensions app.

    This model represents an instance of a synchronization process, tracking its
    inventory item, type, associated task history, synchronizer, and current status.

    :param id: The primary key for the table. Automatically generated using UUID4.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param syncer: The name of the synchronizer responsible for this synchronization.
        Indexed for quick lookup.
    :param status: The run-level status of the synchronization.
    :param snapshot_complete: Whether the run observed a complete generation of the
        remote inventory, or ``None`` when the syncer does not produce one.
    :param items: A list of synchronization items associated with this synchronization
        instance.
    """

    items: list["SyncItem"] = Relationship(
        back_populates="sync_instance",
        cascade_delete=True,
    )


class SyncInstanceWrite(SyncInstanceBase):
    """Define the write model for creating a synchronization instance.

    This model extends `SyncInstanceBase` and is used specifically for creating
    new synchronization instances.

    :param syncer: The name of the synchronizer responsible for this synchronization.
        Indexed for quick lookup.
    :param status: The run-level status of the synchronization. Defaults to PENDING.
    :param snapshot_complete: Whether the run observed a complete generation of the
        remote inventory, or ``None`` when the syncer does not produce one.
    """


class SyncItemBase(SQLModel):
    """Define the base structure for a synchronization item.

    This model provides the foundational fields required for tracking individual
    synchronization items, including the associated inventory item and synchronization
    status.

    :param entity_id: The identifier of the inventory item being synchronized.
    :param entity_type: The type of the inventory item being synchronized.
    :param status: The current status of the synchronization process. Defaults to
        PENDING.
    :param sync_instance_id: The foreign key referencing the associated synchronization
        instance.
    """

    entity_id: int | None = SQLField(index=True)
    entity_type: SyncInventoryEntityTypeEnum = SQLField(
        sa_column=Column(
            EnumField(SyncInventoryEntityTypeEnum),
            nullable=False,
            index=True,
        ),
    )
    status: SyncStatusEnum = SQLField(
        default=SyncStatusEnum.PENDING,
        sa_column=Column(EnumField(SyncStatusEnum), nullable=False, index=True),
    )
    sync_instance_id: UUID4 = SQLField(
        foreign_key="syncinstance.id",
        index=True,
        ondelete="CASCADE",
    )


class SyncItem(BaseUUIDSQLModel, SyncItemBase, table=True):
    """Represent a synchronization item.

    This model represents an individual synchronization task within a synchronization
    instance, tracking its inventory item, type, status, and associated task history.

    :param id: The primary key for the table. Automatically generated using UUID4.
    :type id: UUID4
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :type created_at: UTCDatetime
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :type updated_at: UTCDatetime | None
    :param entity_id: The identifier of the inventory item being synchronized.
    :type entity_id: int | None
    :param entity_type: The type of the inventory item being synchronized.
    :type entity_type: SyncInventoryEntityTypeEnum
    :param status: The current status of the synchronization process.
    :type status: SyncStatusEnum
    :param sync_instance_id: The foreign key referencing the associated synchronization
        instance.
    :type sync_instance_id: UUID4
    :param sync_instance: The synchronization instance to which this item belongs.
    :type sync_instance: SyncInstance
    """

    __table_args__ = (
        Index(
            "ix_syncitem_entity_id_entity_type_status",
            "entity_id",
            "entity_type",
            "status",
        ),
        Index("ix_syncitem_entity_type_status", "entity_type", "status"),
    )
    sync_instance: SyncInstance = Relationship(back_populates="items")

    @model_validator(mode="after")
    def _validate_entity_id_not_null(self) -> Self:
        if (
            self.entity_id is None
            and self.entity_type != SyncInventoryEntityTypeEnum.INVENTORY
        ):
            raise ValueError(
                f"entity_id cannot be None for type {self.entity_type}",
            )
        return self

    @property
    def finished_at(self) -> datetime:
        """Return when a finished item reached its final status, in UTC.

        :return: Its last write time, or its creation time if it was never updated.
        """
        return make_datetime_utc(self.updated_at or self.created_at)


class SyncItemWrite(SyncItemBase):
    """Define the write model for creating a synchronization item.

    This model extends `SyncItemBase` and is used specifically for creating
    new synchronization items.

    :param entity_id: The identifier of the inventory item being synchronized.
    :type entity_id: int | None
    :param entity_type: The type of the inventory item being synchronized.
    :type entity_type: SyncInventoryEntityTypeEnum
    :param status: The current status of the synchronization process. Defaults to
        PENDING.
    :type status: SyncStatusEnum
    :param sync_instance_id: The foreign key referencing the associated synchronization
        instance.
    :type sync_instance_id: UUID4
    """


class SyncEntityAbsenceBase(SQLModel):
    """Define the base structure for a missing-grace ledger row.

    :param syncer: The name of the synchronizer that observed the absence.
    :param entity_id: The local identifier of the absent inventory entity.
    :param entity_type: The type of the absent inventory entity.
    """

    syncer: NonEmptyStr
    entity_id: int = SQLField(index=True)
    entity_type: SyncInventoryEntityTypeEnum = SQLField(
        sa_column=Column(
            EnumField(
                SyncInventoryEntityTypeEnum,
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            index=True,
        ),
    )


class SyncEntityAbsence(BaseSQLModel, SyncEntityAbsenceBase, table=True):
    """Count consecutive complete generations that reported an entity absent.

    A row outlives the individual runs that wrote it, so it carries an integer
    primary key and neither a foreign key nor a relationship to ``SyncInstance``.
    ``created_at`` is the first observed absence and ``updated_at`` the most recent.

    :param id: The primary key for the table. Auto-incremented and not nullable.
    :param created_at: The timestamp when the record is created. Defaults to the current
        time in UTC.
    :param updated_at: The timestamp when the record is last updated. Automatically
        updated on changes.
    :param syncer: The name of the synchronizer that observed the absence.
    :param entity_id: The local identifier of the absent inventory entity.
    :param entity_type: The type of the absent inventory entity.
    :param missing_generations: The number of consecutive complete generations that
        did not include the entity.
    """

    __table_args__ = (
        UniqueConstraint(
            "syncer",
            "entity_type",
            "entity_id",
            name="uq_syncentityabsence_entity",
        ),
    )
    missing_generations: int = SQLField(default=0)


class SyncEntityAbsenceWrite(SyncEntityAbsenceBase):
    """Define the write model for creating a missing-grace ledger row.

    Its three fields are exactly the unique key, so an unfiltered ``get_or_create``
    on this model matches at most one row.

    :param syncer: The name of the synchronizer that observed the absence.
    :param entity_id: The local identifier of the absent inventory entity.
    :param entity_type: The type of the absent inventory entity.
    """


class AppLifecycleEnum(StrEnum):
    """Enumerate the runtime lifecycle states of an app.

    Member names equal their values, so the DB column, its CHECK constraint, the
    API wire value, and any operator SQL all share one uppercase token. The
    transitional states ``ENABLING`` and ``DISABLING`` expose an observable
    window for future machinery to finish in-flight work before an app goes
    fully offline; the request guard treats every state other than ``ENABLED``
    as unreachable.
    """

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    ENABLING = "ENABLING"
    DISABLING = "DISABLING"


class AppStateBase(SQLModel):
    """Define the shared fields for :class:`AppState`.

    :param app_key: The plugin's module key (the last dotted segment of its
        ``module_name``). Unique and indexed for per-request lookups.
    :param lifecycle_state: The app's runtime lifecycle state. Defaults to
        ``ENABLED``; the startup seed maps each ``settings.yaml`` flag to
        ``ENABLED`` or ``DISABLED``.
    """

    app_key: NonEmptyStr = SQLField(unique=True, index=True)
    lifecycle_state: AppLifecycleEnum = SQLField(
        default=AppLifecycleEnum.ENABLED,
        sa_column=Column(
            EnumField(AppLifecycleEnum, native_enum=False, create_constraint=True),
            nullable=False,
        ),
    )


class AppState(BaseSQLModel, AppStateBase, table=True):
    """Represent the per-app runtime lifecycle state.

    One row per non-protected plugin in ``settings.yaml EXTENSIONS.APPS``. Seeded on
    startup via :func:`app.extensions.db.seed.init_extensions_db`, mapping each YAML entry's
    ``enabled`` flag to ``ENABLED`` / ``DISABLED``, and transitioned via the
    admin REST endpoint at ``PUT /api/admin/apps/{app_key}/state``.

    Protected apps (currently ``inventory``) are never seeded and have no row;
    the request guard skips injection for them and the toggle endpoint rejects
    them. A missing row for a configured app is treated as ``ENABLED`` by the
    guard (a configured plugin is active until explicitly disabled); rows for
    apps no longer in ``EXTENSIONS.APPS`` are cleaned up on the next startup seed.

    :param id: The auto-incremented primary key.
    :param app_key: The plugin's module key. Unique and indexed.
    :param lifecycle_state: The app's runtime lifecycle state.
    """


class AppStateWrite(SQLModel):
    """Define the write payload for the app-state toggle endpoint.

    :param lifecycle_state: The requested target lifecycle state.
    """

    lifecycle_state: AppLifecycleEnum


class AppRunningTask(BaseSQLModel, table=True):
    """Track one in-flight PMM Extensions app-owned Celery task for cooperative drain.

    One row per running task tagged with its owning app key via
    :func:`app.extensions.app_drain.owned_by`, inserted by the ``task_prerun`` receiver
    and removed by ``task_postrun``. The per-app row
    count drives the terminal ``DISABLING`` -> ``DISABLED`` transition once an
    app's running tasks reach zero. ``created_at`` (inherited) is the task start
    time, used by the reconciler to prune rows orphaned by a worker crash or a
    force-disable ``revoke(terminate=True)`` (where ``task_postrun`` never runs).

    :param id: The auto-incremented primary key.
    :param app_key: The owning app's key. Indexed for per-app count and prune
        queries.
    :param celery_task_id: The in-flight task's execution id -- a Celery task id,
        or a synthetic ``manual-<uuid>`` id for non-Celery in-request work tracked
        via :func:`app.extensions.app_drain.track_app_task`. Unique and indexed.
    """

    app_key: NonEmptyStr = SQLField(index=True)
    celery_task_id: NonEmptyStr = SQLField(unique=True, index=True)


class ExtensionsAppPeriodicTaskBase(SQLModel):
    """Define the shared fields for :class:`ExtensionsAppPeriodicTask`.

    :param periodic_task_name: The library ``PeriodicTask.name`` of the gated
        schedule. Unique and indexed for write-through lookups.
    :param app_key: The owning app's key, joined against
        :class:`AppState.app_key`.
    :param user_enabled: The per-schedule operator override. ``effective_enabled``
        is true when the owning app is ``ENABLED`` and ``user_enabled`` is set;
        defaults to ``True``.
    """

    periodic_task_name: NonEmptyStr = SQLField(unique=True, index=True)
    app_key: NonEmptyStr = SQLField(index=True)
    user_enabled: bool = True


class ExtensionsAppPeriodicTask(
    BaseSQLModel, ExtensionsAppPeriodicTaskBase, table=True
):
    """Map a plugin-owned Celery periodic task to its owning app + user override.

    One row per plugin-owned schedule in ``EXTENSIONS.APPS``. Seeded on startup via
    :func:`app.extensions.periodic_tasks.sync_app_periodic_task_gating` and consulted at
    every :class:`AppState` transition to recompute ``effective_enabled`` and
    write it through to the library ``PeriodicTask.enabled`` column.

    :param id: The auto-incremented primary key.
    :param periodic_task_name: The library ``PeriodicTask.name``. Unique and indexed.
    :param app_key: The owning app's key.
    :param user_enabled: The per-schedule operator override.
    """
