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

"""Legacy ``data['_form']`` reconstruction for the MySQL Restores plugin."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import yaml

from app.extensions.apps.framework.form_backfill_guards import require_run_python_meta
from app.extensions.apps.framework.form_backfill_inventory import (
    resolve_service_from_meta,
)
from app.extensions.apps.framework.form_backfill_registry import FormBackfillEntry
from app.extensions.apps.mysql_backups.models import BackupType
from app.extensions.apps.mysql_backups.restore.deps import parse_restore_task_data
from app.extensions.apps.mysql_backups.restore.models import (
    OWNER,
    repair_source_declaration,
    RestoreCreate,
)
from app.inventory.models import ServiceTypeEnum

if TYPE_CHECKING:
    from app.extensions.apps.framework.form_backfill_registry import FormBackfillContext
    from app.tasks.models import Task

__all__ = [
    "FORM_BACKFILL_ENTRY",
    "reconstruct_mysql_restores_form",
    "repair_mysql_restores_stamp",
]

_RESTORE_FORM_FIELDS = frozenset(RestoreCreate.model_fields)
_EXPLICIT_FORM_KEYS = frozenset(
    {
        "task_name",
        "hostname",
        "backup_type",
        "backup_source",
        "service_id",
        "schema_id",
        "alert_on_fail",
    }
)
_PARSE_ONLY_KEYS = frozenset(
    {
        "name",
        "host",
        "database",
        "dest_host",
        "dest_port",
    }
)


def _resolve_restore_service_id(
    parsed: dict[str, Any],
    meta: dict[str, Any],
    ctx: FormBackfillContext,
) -> str | None:
    """Return the restore form ``service_id``, or ``None`` when unresolved."""
    resolve_meta = meta
    if parsed.get("host"):
        resolve_meta = {**meta, "_service_name": None}
    service_id = resolve_service_from_meta(
        ctx,
        resolve_meta,
        ServiceTypeEnum.MYSQL,
        host=parsed.get("host"),
        port=parsed.get("port"),
    )
    if service_id is None:
        return None
    return str(service_id)


def _resolve_restore_schema_id(
    service_id: str | None,
    database: Any,
    ctx: FormBackfillContext,
) -> str | None:
    """Return the restore form ``schema_id`` when the database name resolves uniquely."""
    if (
        ctx.schema_lookup is None
        or service_id is None
        or not service_id.isdigit()
        or not isinstance(database, str)
        or not database.strip()
    ):
        return None

    schema_id = ctx.schema_lookup.resolve(
        service_id=int(service_id),
        schema_name=database,
    )
    if schema_id is None:
        return None
    return str(schema_id)


def reconstruct_mysql_restores_form(
    task: Task,
    ctx: FormBackfillContext,
) -> dict[str, Any] | None:
    """Rebuild a :class:`~app.extensions.apps.mysql_backups.restore.models.RestoreCreate` body from a legacy task.

    Wraps :func:`~app.extensions.apps.mysql_backups.restore.deps.parse_restore_task_data`,
    resolves ``service_id`` / ``schema_id`` from inventory when possible, and drops
    parse keys that are not on the create model (for example ``host`` / ``database`` /
    ``name``).

    :param task: The persisted mysql restores task row.
    :param ctx: Shared backfill context carrying the inventory lookup tables.
    :return: A create-model-shaped dict, or ``None`` when reconstruction fails.
    """
    meta = require_run_python_meta(task)
    if meta is None:
        return None

    try:
        parsed = parse_restore_task_data({"name": task.name, "data": task.data})
    except KeyError, TypeError, yaml.YAMLError:
        return None

    hostname = parsed.get("hostname")
    backup_type = parsed.get("backup_type")
    backup_source = parsed.get("backup_source")
    if (
        not isinstance(hostname, str)
        or not hostname.strip()
        or not isinstance(backup_type, str)
        or not backup_type.strip()
        or not isinstance(backup_source, str)
        or not backup_source.strip()
    ):
        return None

    normalized_backup_type = backup_type.strip()
    service_id = _resolve_restore_service_id(parsed, meta, ctx)
    if normalized_backup_type == BackupType.MYDUMPER.value and service_id is None:
        return None

    schema_id = _resolve_restore_schema_id(
        service_id,
        parsed.get("database"),
        ctx,
    )

    form_fields = {
        key: value
        for key, value in parsed.items()
        if key in _RESTORE_FORM_FIELDS
        and key not in _EXPLICIT_FORM_KEYS
        and key not in _PARSE_ONLY_KEYS
        and value is not None
    }

    body: dict[str, Any] = {
        "task_name": task.name,
        "hostname": hostname.strip(),
        "backup_type": normalized_backup_type,
        "backup_source": backup_source.strip(),
        "alert_on_fail": task.alert_on_fail,
        **form_fields,
    }
    if service_id is not None:
        body["service_id"] = service_id
    if schema_id is not None:
        body["schema_id"] = schema_id
    # A reconstruction is stored state, not a submission, so it takes the same
    # repair a stamp does rather than only the inference.
    return repair_source_declaration(body) or body


def repair_mysql_restores_stamp(
    stored_form: dict[str, Any],
    _task: Task,
    _ctx: FormBackfillContext,
) -> dict[str, Any] | None:
    """Declare the source of a stamp that does not describe its own values.

    The stamp is a full model dump, so one predating the controls carries the
    ``percona`` / ``22`` / ``s3cmd`` defaults the gates now reject, and one
    predating the key-file gate can name a key file its declared format does not
    admit. The shared repair settles both, which is what makes the returned body
    valid on its own terms: a body still carrying values its declarations forbid
    would fail validation, and the orchestrator records that as
    ``skipped_invalid`` rather than surfacing it.

    What this adds over the re-validation the orchestrator performs anyway is the
    decision of whether a repair is owed at all; the repair call keeps the
    returned dict correct without relying on that downstream pass.

    Neither the task row nor the backfill context is read: the stamp carries every
    field the inference needs.

    :param stored_form: A copy of the task's existing ``data['_form']``.
    :param _task: The stamped task row.
    :param _ctx: Shared backfill context.
    :return: The repaired form, or ``None`` when the stamp already describes its
        source.
    """
    return repair_source_declaration(stored_form)


FORM_BACKFILL_ENTRY = FormBackfillEntry(
    app_key="mysql_backups/restore",
    owner=OWNER,
    create_model=RestoreCreate,
    reconstructor=reconstruct_mysql_restores_form,
    stamp_repairer=repair_mysql_restores_stamp,
)
