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

"""Define models for the Backups plugin."""

from enum import nonmember, StrEnum
from typing import Annotated, Any, Literal

from annotated_types import Ge
from pydantic import (
    model_validator,
    StringConstraints,
)

from app.core.models import BaseCaseInsensitiveModel
from app.core.utils.fields import EmptyStrToNone, EnumFieldMixin, NonEmptyStr
from app.core.utils.pydantic import blank_str_values_to_none
from app.extensions.apps.framework.form_dsl import (
    Choices,
    ServiceRef,
    TaskFormModel,
    Ui,
)
from app.extensions.apps.shared.backups.responses import BackupTaskBase
from app.inventory.models import ServiceTypeEnum

OWNER = "BACKUP_PG"


class BackupType(EnumFieldMixin, StrEnum):
    """Represent the backup tools a run can be taken with.

    :cvar LABELS: Display text for each stored value, keyed as the value is
        stored on the wire. A value with no entry is rendered as-is by the
        caller. Wrapped in :func:`enum.nonmember` because ``enum`` would
        otherwise treat a class-body dict as a member candidate.
    """

    PGBACKREST = "P"

    LABELS = nonmember({"P": "pgBackRest"})


class PgBackRestBackupType(EnumFieldMixin, StrEnum):
    """PgBackRest backup types."""

    INCR = "incr"
    DIFF = "diff"


SafeStanza = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    ),
]
"""Define a safe pgBackRest stanza name."""


class BackupConfigAll(BaseCaseInsensitiveModel):
    """Represent the general configuration for the backup task."""

    logging_dir: NonEmptyStr | EmptyStrToNone = None
    backup_dir: NonEmptyStr | EmptyStrToNone = None
    pgbackrest_bin: NonEmptyStr | EmptyStrToNone = None
    pgbackrest_config_file: NonEmptyStr | EmptyStrToNone = None
    pgbackrest_backup_type: PgBackRestBackupType | EmptyStrToNone = None
    pgbackrest_datadir: NonEmptyStr | EmptyStrToNone = None
    pgbackrest_retention_full: int | EmptyStrToNone = None
    pgbackrest_retention_archive: int | EmptyStrToNone = None
    # Spelled out on three surfaces: this union, BackupPgForm's union, and its Choices
    # labels (the standalone payload carries a fourth, since it runs on the DB host).
    # A StrEnum would collapse the unions but republish the field as a named OpenAPI
    # component rather than an inline enum, and the weekday labels would still need
    # Choices; the vocabulary parity tests fail on a partial edit meanwhile.
    pgbackrest_incremental_cycle: (
        Literal["daily", "weekly", "1", "2", "3", "4", "5", "6", "7"] | EmptyStrToNone
    ) = None


class BackupConfigServer(BaseCaseInsensitiveModel):
    """Represent an individual server configuration.

    :param alias: A unique alias for the server.
    :param backup_type: The type of the backup.
    :param host: The hostname or address of the server.
    """

    alias: NonEmptyStr
    backup_type: str
    host: NonEmptyStr


class BackupConfig(BaseCaseInsensitiveModel):
    """Represent the overall backup configuration.

    :param all_servers: General settings for the backup.
    :type all_servers: BackupConfigAll
    :param server_list: A list of backup configuration for each server.
    :type server_list: list[BackupConfigServer]
    """

    all_servers: BackupConfigAll
    server_list: list[BackupConfigServer]


class BackupPgForm(TaskFormModel):
    """Define the model-first create/update body and schema source for backup_pg.

    The single source of the JSON request body (the field types and defaults the
    server validates) *and* the derived ``GET /schema`` form (driven by the
    :class:`Ui` / reference / :class:`Choices` markers). Field set, types, and
    model defaults match the JSON create contract; the form-display defaults that
    differ from the model default (the pgBackRest tool paths, the default backup
    type) are carried on ``Ui(default=...)`` so the runtime payload stays
    unchanged while the schema renders those form defaults.

    Field declaration order is load-bearing: it drives the derived form's
    section and field order. ``backup_type`` is not a form field — the spec
    builder injects :attr:`BackupType.PGBACKREST`. The ``task_name`` / ``hostname``
    Task-section fields and the ``alert_on_fail`` capability control are inherited
    from :class:`TaskFormModel` (``alert_on_fail`` is ``Hidden``, off-schema).
    Unknown fields are rejected by the inherited ``extra="forbid"`` (for example a
    stale FE submitting ``host`` / ``port``, which the payload pins itself).
    """

    service_id: Annotated[
        int,
        ServiceRef(
            service_types=(ServiceTypeEnum.POSTGRESQL,), check_connectivity=True
        ),
        Ui(label="Database Service", section="Task"),
    ]
    stanza: Annotated[
        SafeStanza,
        Ui(
            section="pgBackRest",
            description=(
                "pgBackRest stanza name as defined in pgbackrest.conf on the "
                "host (e.g. ``extensions-test``). Passed verbatim as ``--stanza`` to "
                "every pgbackrest invocation."
            ),
        ),
    ]
    pgbackrest_backup_type: Annotated[
        PgBackRestBackupType | None,
        Choices(
            (
                (PgBackRestBackupType.INCR, "Incremental"),
                (PgBackRestBackupType.DIFF, "Differential"),
            )
        ),
        Ui(
            label="pgBackRest Backup Type",
            section="pgBackRest",
            default=PgBackRestBackupType.INCR.value,
        ),
    ] = None
    pgbackrest_bin: Annotated[
        str | None,
        Ui(
            label="pgBackRest Binary",
            section="pgBackRest",
            default="/usr/bin/pgbackrest",
            description="Absolute path to the pgbackrest binary on the host.",
        ),
    ] = None
    pgbackrest_config_file: Annotated[
        str | None,
        Ui(
            label="pgBackRest Config File",
            section="pgBackRest",
            default="/etc/pgbackrest.conf",
            description="Path to the pgbackrest.conf used by the task.",
        ),
    ] = None
    pgbackrest_datadir: Annotated[
        str | None,
        Ui(label="Postgres Data Directory", section="pgBackRest"),
    ] = None
    pgbackrest_retention_full: Annotated[
        int | None,
        Ge(0),
        Ui(label="Full Backup Retention", section="pgBackRest"),
    ] = None
    pgbackrest_retention_archive: Annotated[
        int | None,
        Ge(0),
        Ui(label="Archive Retention", section="pgBackRest"),
    ] = None
    # Vocabulary duplicated -- see the note on BackupConfigAll.pgbackrest_incremental_cycle.
    pgbackrest_incremental_cycle: Annotated[
        Literal["daily", "weekly", "1", "2", "3", "4", "5", "6", "7"] | EmptyStrToNone,
        Choices(
            (
                ("daily", "Daily"),
                ("weekly", "Weekly (Monday)"),
                ("1", "Monday"),
                ("2", "Tuesday"),
                ("3", "Wednesday"),
                ("4", "Thursday"),
                ("5", "Friday"),
                ("6", "Saturday"),
                ("7", "Sunday"),
            )
        ),
        Ui(
            label="Incremental Cycle",
            section="pgBackRest",
            description=(
                "``daily``, ``weekly``, or an ISO weekday number (1-7, "
                "Monday-Sunday) controlling when the FULL backup runs."
            ),
        ),
    ] = None
    logging_dir: Annotated[
        str | None,
        Ui(label="Logging Directory", section="pgBackRest"),
    ] = None
    backup_dir: Annotated[
        NonEmptyStr,
        Ui(label="Backup Directory", section="pgBackRest"),
    ]

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        """Coerce empty-string submissions to ``None`` before field validation.

        :param data: The raw pre-validation submission body.
        :return: The submission with empty-string values coerced to ``None``.
        """
        return blank_str_values_to_none(data)


class BackupTaskResponse(BackupTaskBase):
    """Represent a pgBackRest backup task API response.

    :param backup_type: The ``backup_type`` discriminator stored on the task.
    """

    backup_type: str


class BackupTaskDetailResponse(BackupTaskResponse):
    """Represent a single pgBackRest backup task detail response.

    Add the executor host and port resolved from the task's YAML config so the
    FE detail view can render them alongside the parity Overview block; list
    rows omit these to keep the table response compact.

    :param host: The PostgreSQL host the task connects to.
    :param port: The PostgreSQL port the task connects to.
    """

    host: str | None = None
    port: int | None = None
