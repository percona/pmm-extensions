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

"""Legacy ``data['_form']`` reconstruction for the PostgreSQL Backups plugin."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import yaml

from app.extensions.apps.backup_pg.deps import parse_backup_task_data
from app.extensions.apps.backup_pg.models import BackupPgForm, OWNER
from app.extensions.apps.framework.form_backfill_guards import require_run_python_meta
from app.extensions.apps.framework.form_backfill_inventory import (
    resolve_service_from_meta,
)
from app.extensions.apps.framework.form_backfill_registry import FormBackfillEntry
from app.inventory.models import ServiceTypeEnum

if TYPE_CHECKING:
    from app.extensions.apps.framework.form_backfill_registry import FormBackfillContext
    from app.tasks.models import Task

__all__ = ["FORM_BACKFILL_ENTRIES", "reconstruct_backup_pg_form"]

_BACKUP_PG_FORM_FIELDS = frozenset(BackupPgForm.model_fields)


def _extract_stanza_from_meta(meta: dict[str, Any]) -> str | None:
    """Return the pgBackRest stanza (``SERVER_LIST[0].ALIAS``) from task meta."""
    stanza: str | None = None
    config_raw = meta.get("config")
    if isinstance(config_raw, str) and config_raw.strip():
        try:
            task_config = yaml.safe_load(config_raw)
        except yaml.YAMLError:
            task_config = None
        if isinstance(task_config, dict):
            server_list = task_config.get("SERVER_LIST")
            if isinstance(server_list, list) and server_list:
                first_server = server_list[0]
                if isinstance(first_server, dict):
                    alias = first_server.get("ALIAS") or first_server.get("alias")
                    if isinstance(alias, str) and alias.strip():
                        stanza = alias.strip()
    return stanza


def reconstruct_backup_pg_form(
    task: Task,
    ctx: FormBackfillContext,
) -> dict[str, Any] | None:
    """Rebuild a :class:`~app.extensions.apps.backup_pg.models.BackupPgForm` body from a legacy task.

    Wraps :func:`~app.extensions.apps.backup_pg.deps.parse_backup_task_data`, resolves
    ``service_id`` from inventory, reads the stanza from ``meta['config']``, and
    drops keys forbidden by the create model (for example ``host`` / ``port`` /
    ``backup_type`` and upload-target fields).

    :param task: The persisted backup_pg task row.
    :param ctx: Shared backfill context carrying the inventory lookup table.
    :return: A create-model-shaped dict, or ``None`` when reconstruction fails.
    """
    meta = require_run_python_meta(task)
    if meta is None:
        return None

    stanza = _extract_stanza_from_meta(meta)
    if stanza is None:
        return None

    try:
        parsed = parse_backup_task_data({"name": task.name, "data": task.data})
    except KeyError, TypeError, yaml.YAMLError:
        return None

    hostname = parsed.get("hostname")
    service_id = resolve_service_from_meta(
        ctx,
        meta,
        ServiceTypeEnum.POSTGRESQL,
        host=parsed.get("host"),
        port=parsed.get("port"),
    )
    if not isinstance(hostname, str) or not hostname.strip() or service_id is None:
        return None

    form_fields = {
        key: value
        for key, value in parsed.items()
        if key in _BACKUP_PG_FORM_FIELDS
        and key
        not in {
            "task_name",
            "hostname",
            "service_id",
            "stanza",
            "alert_on_fail",
        }
    }

    return {
        "task_name": task.name,
        "hostname": hostname.strip(),
        "service_id": service_id,
        "stanza": stanza,
        "alert_on_fail": task.alert_on_fail,
        **form_fields,
    }


FORM_BACKFILL_ENTRIES = [
    FormBackfillEntry(
        app_key="backup_pg",
        owner=OWNER,
        create_model=BackupPgForm,
        reconstructor=reconstruct_backup_pg_form,
    ),
]
