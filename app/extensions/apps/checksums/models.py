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

"""Define models for the Checksums plugin."""

from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from app.core.utils.fields import dsn_safe, NonEmptyStr
from app.extensions.apps.framework.form_dsl import (
    ArgFormat,
    Choices,
    DSN_TABLE_DEFAULT,
    SchemaRef,
    ServiceRef,
    TableRef,
    TaskFormModel,
    Ui,
)
from app.inventory.models import ServiceTypeEnum

OWNER = "CHECKSUMS"


def coerce_target_list(value: Any) -> Any:
    """Normalize a legacy or wire-shaped target field into a reference list.

    Accepts the new ``list[int | str]`` shape, a legacy comma-separated string,
    a bare inventory id, or an empty selection. Annotated ``Any`` because this
    is a ``mode="before"`` validator: it normalizes shape, and pydantic checks
    the elements against the field type afterwards.

    :param value: The submitted field value before element validation.
    :return: A list of inventory ids and/or free-typed names.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, set):
        return list(value)
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [part.strip() for part in value.strip().split(",") if part.strip()]
    return value


class ChecksumsForm(TaskFormModel):
    """Define the model-first create/update body and schema source for Checksums.

    The single source of the JSON request body (the field types and defaults the
    server validates) *and* the derived ``GET /schema`` form (driven by the
    :class:`Ui` / reference / :class:`Choices` markers). Field set, types, and
    model defaults match the previous hand-written request body; the form-display
    defaults that differ from the model default are carried on ``Ui(default=...)``.

    Field declaration order is load-bearing. The spec builder assembles
    ``--databases`` / ``--tables`` from the multi-value reference fields, then the
    framework's ``build_command_args`` emits the remaining ``ArgFormat`` value args
    (in field order) followed by all flag args (in field order). Section order
    (Task, Data, Recursion, Flags, Advanced) follows each section's first field.
    ``progress`` is declared last to land at the end of the value args, and
    ``Ui(order=...)`` pins the Advanced section's display order where it diverges
    from declaration order. The ``task_name`` / ``hostname`` Task-section fields
    and the ``alert_on_fail`` capability control are inherited from
    :class:`TaskFormModel` (``alert_on_fail`` is ``Hidden``, off-schema).
    """

    service_id: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="Database Host", section="Task"),
    ]
    databases: Annotated[
        list[int | NonEmptyStr],
        SchemaRef(multiple=True, allow_custom=True),
        Ui(
            label="Databases",
            section="Data",
            depends_on="service_id",
            description="Schemas to checksum; pick from inventory or type names.",
        ),
    ] = Field(default_factory=list)
    tables: Annotated[
        list[int | NonEmptyStr],
        TableRef(multiple=True, allow_custom=True),
        Ui(
            label="Tables",
            section="Data",
            depends_on="databases",
            description="Tables as schema.table; pick from inventory or type names.",
        ),
    ] = Field(default_factory=list)

    recursion_method: Annotated[
        str,
        Choices(
            (
                ("default", "Default"),
                ("processlist", "Processlist"),
                ("hosts", "Hosts"),
                ("dsn", "DSN"),
                ("none", "None"),
            )
        ),
        Ui(section="Recursion", required=True),
    ] = "processlist"
    dsn_table: Annotated[
        str,
        Ui(
            label="DSN Table",
            section="Recursion",
            default=DSN_TABLE_DEFAULT,
            description="Only used when recursion method is 'dsn'",
        ),
    ] = ""
    binary_index: Annotated[
        bool,
        ArgFormat(),
        Ui(
            section="Flags",
            description="Use BLOB type for replicate-table boundary columns",
        ),
    ] = False
    explain_arg: Annotated[
        bool,
        ArgFormat("--explain"),
        Ui(
            label="Explain (dry run)",
            section="Flags",
            description="Show but do not execute checksum queries",
        ),
    ] = False
    fail_on_stopped_replication: Annotated[
        bool,
        ArgFormat(),
        Ui(
            label="Fail on Stopped Replication",
            section="Flags",
            description="Fail with an error if replication is stopped",
        ),
    ] = False
    truncate_replicate_table: Annotated[
        bool,
        ArgFormat(),
        Ui(
            section="Flags",
            description="Truncate the replicate table before starting",
        ),
    ] = False
    pause_file: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            default=None,
            description="Execution pauses while this file exists",
        ),
    ] = ""
    set_vars: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=2,
            default="transaction_isolation='READ-COMMITTED',lock_wait_timeout=5",
            description="MySQL variables to set (comma-separated key=value pairs)",
        ),
    ] = ""
    max_load: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=3,
            default="Threads_running=50",
            description="Pause when any GLOBAL STATUS variable exceeds this threshold",
        ),
    ] = ""
    chunk_time: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=4,
            default="0.5",
            description="Target execution time per chunk in seconds",
        ),
    ] = ""
    max_lag: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=5,
            default="150",
            description="Pause until replica lag falls below this value (seconds)",
        ),
    ] = ""
    defaults_file: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=6,
            description=(
                "Absolute path to a MySQL options file on the executor node. "
                "pt-table-checksum reads MySQL options only from this file. "
                "The path must already exist on the executor; PMM Extensions does "
                "not verify it."
            ),
        ),
    ] = ""
    progress: Annotated[
        str,
        ArgFormat(),
        Ui(
            section="Advanced",
            order=1,
            default="time,10",
            description="Print progress reports to STDERR (e.g. time,10)",
        ),
    ] = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_target_fields(cls, data: Any) -> Any:
        """Accept legacy comma-separated strings for ``databases`` / ``tables``."""
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        for key in ("databases", "tables"):
            if key in updated:
                updated[key] = coerce_target_list(updated[key])
        return updated

    @field_validator("databases", "tables")
    @classmethod
    def _target_dsn_safe(cls, value: list[int | str]) -> list[int | str]:
        """Reject DSN delimiters in a free-typed schema or table name.

        :param value: The submitted target list (inventory ids and/or names).
        :return: The value unchanged when no element carries a DSN delimiter.
        """
        return [dsn_safe(item) if isinstance(item, str) else item for item in value]

    @field_validator("defaults_file")
    @classmethod
    def _defaults_file_absolute(cls, value: str) -> str:
        """Require an absolute executor path when ``defaults_file`` is non-empty.

        :param value: The submitted defaults-file path.
        :return: The normalized path, or ``""`` when omitted or empty.
        :raises ValueError: When ``value`` is non-empty but not an absolute path.
        """
        if not value:
            return ""
        if not PurePosixPath(value).is_absolute():
            raise ValueError(
                "defaults_file must be an absolute path on the executor node; "
                "tilde (~) is not expanded and relative paths resolve against an "
                "unpredictable working directory"
            )
        return value
