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

"""Define models for the Archives plugin."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.models import BaseCaseInsensitiveModel
from app.core.utils.fields import (
    dsn_safe,
    EmptyStrToNone,
    NonEmptyStr,
    TcpPort,
)
from app.extensions.apps.archives.constants import SwapDropEnum
from app.extensions.apps.framework import (
    Choices,
    Forbidden,
    FormRules,
    Requires,
    SchemaRef,
    ServiceRef,
    TableRef,
    TaskFormModel,
    Ui,
)
from app.extensions.apps.framework.form_dsl import Hidden
from app.extensions.apps.framework.rules import (
    F,
    FailRule,
)
from app.inventory.models import ServiceTypeEnum

OWNER = "ARCHIVER"


class SourceByTable(BaseModel):
    """Represent a schema+table source selection (collapsed free-solo references).

    :param mode: The one-of discriminator (``"table"``).
    :param source_db: The source schema — an inventory id or a free-typed name.
    :param source_table: The source table — an inventory id or a free-typed name.
    """

    mode: Literal["table"] = "table"
    source_db: Annotated[
        int | NonEmptyStr,
        SchemaRef(allow_custom=True),
        Ui(
            label="Source Schema",
            section="Source",
            depends_on="service_id",
            description="Schema holding the source table; pick from inventory or type a name.",
        ),
    ]
    source_table: Annotated[
        int | NonEmptyStr,
        TableRef(allow_custom=True),
        Ui(
            label="Source Table",
            section="Source",
            depends_on="source.source_db",
            description="Table whose rows are archived; pick from inventory or type a name.",
        ),
    ]


class SourceByQuery(BaseModel):
    """Represent a custom-query source selection.

    :param mode: The one-of discriminator (``"query"``).
    :param source_query: The query defining the rows to archive.
    """

    mode: Literal["query"] = "query"
    source_query: Annotated[
        NonEmptyStr,
        Ui(
            label="Source Query",
            section="Source",
            description="Custom query defining source rows.",
        ),
    ]


class DestByTable(BaseModel):
    """Represent a table destination (collapsed free-solo references).

    :param mode: The one-of discriminator (``"table"``).
    :param dest_db: The destination schema — an inventory id, a free-typed name, or
        ``None`` to reuse the source schema.
    :param dest_table: The destination table — an inventory id or a free-typed name.
    """

    mode: Literal["table"] = "table"
    dest_db: Annotated[
        int | NonEmptyStr | None,
        SchemaRef(allow_custom=True),
        Ui(
            label="Destination Schema",
            section="Destination",
            depends_on="host.dest_service",
            description="Destination schema; leave empty to reuse the source schema.",
        ),
    ] = None
    dest_table: Annotated[
        int | NonEmptyStr,
        TableRef(allow_custom=True),
        Ui(
            label="Destination Table",
            section="Destination",
            depends_on="destination.dest_db",
            description="Table the archived rows are written to.",
        ),
    ]

    @field_validator("dest_db")
    @classmethod
    def _dest_db_dsn_safe(cls, value: int | str | None) -> int | str | None:
        """Reject DSN delimiters in a free-typed destination schema name.

        :param value: The submitted destination-schema value (id, name, or None).
        :return: The value unchanged when it carries no DSN delimiter.
        """
        return dsn_safe(value) if isinstance(value, str) else value


class DestByFile(BaseModel):
    """Represent a file destination.

    :param mode: The one-of discriminator (``"file"``).
    :param dest_file: The file path the archived rows are written to.
    """

    mode: Literal["file"] = "file"
    dest_file: Annotated[
        NonEmptyStr,
        Ui(
            label="Destination File",
            section="Destination",
            description="File path for archiving rows instead of a table.",
        ),
    ]


class HostByService(BaseModel):
    """Represent a destination host taken from an inventory service.

    :param mode: The one-of discriminator (``"service"``).
    :param dest_service: The inventory id of the destination MySQL service; its
        node address and port supply the destination host and port.
    """

    mode: Literal["service"] = "service"
    dest_service: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,)),
        Ui(
            label="Destination Service",
            section="Destination Host",
            description="Inventory lookup for the destination host and port.",
        ),
    ]


class HostManual(BaseModel):
    """Represent a manually-entered destination host.

    :param mode: The one-of discriminator (``"manual"``).
    :param dest_host: The destination host address.
    :param dest_port: The destination port (1-65535); defaults to the MySQL port.
    """

    mode: Literal["manual"] = "manual"
    dest_host: Annotated[
        NonEmptyStr,
        Ui(
            label="Destination Host",
            section="Destination Host",
            description="Manual destination host address.",
        ),
    ]
    dest_port: Annotated[
        TcpPort | None,
        Ui(
            label="Destination Port",
            section="Destination Host",
            description="Destination port (1-65535); defaults to the MySQL port.",
        ),
    ] = None

    @field_validator("dest_host")
    @classmethod
    def _dest_host_dsn_safe(cls, value: str) -> str:
        """Reject DSN delimiters in the manual destination host.

        :param value: The submitted manual destination-host address.
        :return: The value unchanged when it carries no DSN delimiter.
        """
        return dsn_safe(value)


_ARCHIVES_FORM_RULES = FormRules(
    fail_when=(
        # Only Purge Only is supported. Rejects any crafted payload (or a resubmitted
        # pre-existing SWAP_DROP / SWAP_ARCHIVE_DROP task) that bypasses the UI.
        FailRule(
            fail_when=F("swap_drop") != SwapDropEnum.PURGE_ONLY,
            error_fields=["swap_drop"],
            message="Not available yet. Only Purge Only is currently supported.",
        ),
    ),
)


class ArchivesCreate(TaskFormModel):
    """Represent an Archives creation form as a model-first ``TaskFormModel``.

    Source, destination, and destination-host are discriminated-union one-of groups;
    the schema / table / database references are collapsed free-solo fields
    (``int`` inventory id or free-typed ``str`` name). The ``task_name`` /
    ``hostname`` Task-section fields and the ``alert_on_fail`` capability control
    are inherited from :class:`TaskFormModel`.

    :param task_name: The human-readable task name; required and non-empty
        (inherited from :class:`TaskFormModel`).
    :param hostname: The executor host the task runs on; required and non-empty
        (inherited from :class:`TaskFormModel`).
    :param service_id: The inventory id of the source MySQL service (the host whose
        rows are archived; the connectivity probe targets it).
    :param swap_drop: The archive type; only ``PURGE_ONLY`` is currently supported.
    :param source: The source rows, by schema+table or by query.
    :param destination: The destination table or file; ``None`` when ``delete_data``.
    :param host: The destination host, by service or manual entry; ``None`` reuses
        the source host.
    :param swp_table_suffix: The swap-table date suffix (SWAP_ARCHIVE_DROP only).
    :param where: The WHERE clause filtering rows; required unless SWAP_DROP.
    :param use_index: An index hint to optimise the query.
    :param extra_args: Additional pt-archiver CLI arguments.
    :param limit: The maximum rows per archiver run.
    :param sleep: The sleep duration between chunk operations (seconds).
    :param disable_binlog: Whether to disable binary logging for the operation.
    :param disable_bulk_insert: Whether to disable the bulk-insert optimisation.
    :param delete_data: Whether to delete source rows without archiving them.
    """

    __form_rules__ = _ARCHIVES_FORM_RULES

    service_id: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="Source Service", section="Task"),
    ]
    swap_drop: Annotated[
        int,
        Field(ge=0, le=2),
        Choices(
            options=(
                (SwapDropEnum.PURGE_ONLY, "Purge Only"),
                (SwapDropEnum.SWAP_DROP, "Swap & Drop"),
                (SwapDropEnum.SWAP_ARCHIVE_DROP, "Swap Archive & Drop"),
            )
        ),
        Ui(
            label="Archive Type",
            section="Archive Type",
            default=SwapDropEnum.PURGE_ONLY,
        ),
    ] = SwapDropEnum.PURGE_ONLY
    source: Annotated[
        SourceByTable | SourceByQuery,
        Field(discriminator="mode"),
        Ui(section="Source"),
    ]
    destination: Annotated[
        DestByTable | DestByFile | None,
        Field(discriminator="mode"),
        Ui(section="Destination"),
    ] = None
    host: Annotated[
        HostByService | HostManual | None,
        Field(discriminator="mode"),
        Ui(label="Destination Host", section="Destination Host"),
    ] = None
    # Kept on the model for the config/legacy-form mapping, but hidden from the
    # schema: it only applies to SWAP_ARCHIVE_DROP, which the swap_drop rule rejects
    # while only Purge Only is supported.
    swp_table_suffix: Annotated[date | None, Hidden()] = None
    where: Annotated[
        NonEmptyStr | None,
        Ui(label="WHERE Clause", section="Options"),
        Requires(when=F("swap_drop") != SwapDropEnum.SWAP_DROP),
        Forbidden(when=F("swap_drop") == SwapDropEnum.SWAP_DROP),
    ] = None
    use_index: Annotated[NonEmptyStr | EmptyStrToNone, Ui(section="Advanced")] = None
    extra_args: Annotated[NonEmptyStr | EmptyStrToNone, Ui(section="Advanced")] = None
    limit: Annotated[
        int | None,
        Field(ge=1),
        Ui(section="Advanced", default=1000),
    ] = None
    sleep: Annotated[
        int | None,
        Field(ge=0),
        Ui(label="Sleep (s)", section="Advanced", default=1),
    ] = None
    disable_binlog: Annotated[bool | None, Ui(section="Advanced")] = None
    disable_bulk_insert: Annotated[bool | None, Ui(section="Advanced")] = None
    delete_data: Annotated[
        bool | None,
        Ui(
            label="Delete Without Archiving",
            section="Advanced",
            destructive=(
                "The matched source rows are deleted without being archived "
                "anywhere. There is no copy to restore from."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def _check_destination_presence(self) -> ArchivesCreate:
        """Require a destination for an archiving run; forbid one when deleting.

        The destination one-of is a group, so this self-vs-``delete_data``
        invariant is enforced here rather than as a field-scoped rule.

        :return: The validated model.
        :raises ValueError: When a destination is missing for an archiving run, or
            present alongside ``delete_data``.
        """
        if self.delete_data and self.destination is not None:
            raise ValueError(
                "A destination cannot be set when deleting without archiving."
            )
        if not self.delete_data and self.destination is None:
            raise ValueError(
                "A destination table or file is required unless deleting without "
                "archiving."
            )
        return self


class PurgeConfigAll(BaseCaseInsensitiveModel):
    """Represents the general configuration for the archive task.

    :param source_host: The hostname or IP address of the source where the data will
        be archived from.
    :type source_host: NonEmptyStr
    :param source_port: The port number used to connect to the source host.
    :type source_port: int
    """

    source_host: NonEmptyStr
    source_port: int


class PurgeConfigItem(BaseCaseInsensitiveModel):
    """Represents an individual purge configuration item.

    :param alias: A unique alias for the task being created, identifying it
        within the system.
    :type alias: NonEmptyStr
    :param source_db: The name of the source database schema from which the data
        will be archived.
    :type source_db: NonEmptyStr | None
    :param source_table: The name of the source table within the specified schema.
    :type source_table: NonEmptyStr | None
    :param source_query: Optional; a query defining the source data to be purged.
    :type source_query: NonEmptyStr | None
    :param where: Optional; The WHERE condition that defines which data will be purged
        from the source table. Must be None when swap_drop is SWAP_DROP.
    :type where: NonEmptyStr | None
    :param dest_table: Optional; The name of the destination table where the purged data
        will be archived. Must be None if dest_file is set.
    :type dest_table: NonEmptyStr | None
    :param dest_file: Optional; The destination file path.
        Must be None if dest_table_id is set.
    :type dest_file: NonEmptyStr | None
    :param swap_drop: Integer field (0-2) indicating the swap/drop behavior.
    :type swap_drop: int
    :param swp_table_suffix: Optional; Date suffix for the swap table.
    :type swp_table_suffix: date | None
    :param use_index: Optional; The index to be used for optimizing the query.
    :type use_index: NonEmptyStr | None
    :param extra_args: Optional; Additional arguments for the archive task.
    :type extra_args: NonEmptyStr | None
    :param limit: Optional; The maximum number of records to be processed.
    :type limit: int | None
    :param sleep: Optional; Sleep duration between operations for rate limiting.
    :type sleep: int | None
    :param disable_binlog: Optional integer flag (0 or 1) to disable binary logging.
        ``None`` means the checkbox was left unset (binary logging stays enabled).
    :type disable_binlog: int | None
    :param disable_bulk_insert: Optional integer flag (0 or 1) to disable bulk
        insert. If ``None``, the setting is left unset so existing/default behavior is
        preserved.
    :type disable_bulk_insert: int | None
    :param delete_data: Optional integer flag (0 or 1). When set to 1, source
        rows are deleted without being written to any destination; dest_table
        and dest_file must not be set, and vice versa.
    :type delete_data: int | None
    :param dest_host: Optional; The destination host address.
    :type dest_host: NonEmptyStr | None
    :param dest_port: Optional; The destination port number.
    :type dest_port: int | None
    :param dest_db: Optional; The destination database schema name.
    :type dest_db: NonEmptyStr | None
    """

    alias: NonEmptyStr
    source_db: NonEmptyStr | None = None
    source_table: NonEmptyStr | None = None
    source_query: NonEmptyStr | None = None
    where: NonEmptyStr | None = None
    dest_table: NonEmptyStr | None = None
    dest_file: NonEmptyStr | None = None
    swap_drop: int = Field(..., ge=0, le=2)
    swp_table_suffix: date | None = None
    use_index: NonEmptyStr | None = None
    extra_args: NonEmptyStr | None = None
    limit: int | None = None
    sleep: int | None = None
    disable_binlog: int | None = Field(
        None,
        ge=0,
        le=1,
        description="Optional flag to disable binary logging; set to 0 or 1",
    )
    disable_bulk_insert: int | None = Field(
        None,
        ge=0,
        le=1,
        description="Optional flag to disable bulk insert; set to 0 or 1",
    )
    delete_data: int | None = Field(
        None,
        ge=0,
        le=1,
        title="Delete Without Archiving",
        description=(
            "Delete source rows without writing them to any destination; "
            "the destination table/file fields must be left unset."
        ),
    )
    dest_host: NonEmptyStr | None = None
    dest_port: int | None = None
    dest_db: NonEmptyStr | None = None


class PurgeConfig(BaseCaseInsensitiveModel):
    """Represents the overall purge configuration.

    :param all: General settings for the purge, including source host and port
        information.
    :type all: PurgeConfigAll
    :param purge_list: A list of purge configuration items specifying individual
        archive tasks.
    :type purge_list: list[PurgeConfigItem]
    """

    all: PurgeConfigAll
    purge_list: list[PurgeConfigItem]
