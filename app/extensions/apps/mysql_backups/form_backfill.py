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

"""Legacy ``data['_form']`` reconstruction for the MySQL Backups plugin."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, TYPE_CHECKING

import yaml

from app.core.utils.fields import EmptyStrToNone, NonEmptyStr
from app.extensions.apps.framework.form_backfill_guards import require_run_python_meta
from app.extensions.apps.framework.form_backfill_inventory import (
    resolve_service_from_meta,
)
from app.extensions.apps.framework.form_backfill_registry import FormBackfillEntry
from app.extensions.apps.framework.form_dsl import FormRules
from app.extensions.apps.mysql_backups.deps import parse_backup_task_data
from app.extensions.apps.mysql_backups.forms import (
    BACKUP_DIR_UI,
    BackupCreate,
    encryption_format_for_passes,
    LENIENT_BACKUP_FORM_RULES,
    OWNER,
    UploadProvider,
)
from app.extensions.apps.mysql_backups.restore.form_backfill import (
    FORM_BACKFILL_ENTRY as RESTORE_FORM_BACKFILL_ENTRY,
)
from app.inventory.models import ServiceTypeEnum

if TYPE_CHECKING:
    from app.extensions.apps.framework.form_backfill_registry import FormBackfillContext
    from app.tasks.models import Task

__all__ = [
    "FORM_BACKFILL_ENTRIES",
    "LegacyBackupCreate",
    "reconstruct_mysql_backups_form",
    "repair_mysql_backups_stamp",
]

_MYSQL_BACKUPS_FORM_FIELDS = frozenset(BackupCreate.model_fields)
_UPLOAD_PROVIDER_BY_ALIAS = {provider.name: provider for provider in UploadProvider}
# Spellings older stored configs use that are not the enum member name. The
# xtrabackup payload's own upload docstring documents "GS" for Google Cloud
# Storage, which no member name matches.
_UPLOAD_PROVIDER_BY_ALIAS["GS"] = UploadProvider.GSUTIL
_EXPLICIT_FORM_KEYS = frozenset(
    {
        "task_name",
        "hostname",
        "service_id",
        "backup_type",
        "alias",
        "upload",
        "alert_on_fail",
    }
)
_PARSE_ONLY_KEYS = frozenset({"name", "host", "port"})


class LegacyBackupCreate(BackupCreate):
    """Validate a reconstructed form body against the create form's older contract.

    A stored config written before the create form required a backup directory has
    no ``BACKUP_DIR`` key, so the reconstructed body omits ``backup_dir``.
    Validating that against the strict create model would skip the task, and a task
    with no ``_form`` stamp has no Edit affordance at all — leaving an operator able
    to delete and recreate it but not to repair it. The rejection belongs on the
    create and update routes, which keep
    :class:`~app.extensions.apps.mysql_backups.forms.BackupCreate`.

    The field is declared exactly as the create model declared it before the
    tightening, ``NonEmptyStr`` and not ``StrippedNonEmptyStr``: the older form
    accepted a whitespace-only directory, the payload joined it as a relative path,
    and those tasks ran and reported success, so they are part of the population
    that has to reconstruct rather than be skipped.

    The upload-reachability rules are dropped for the same reason: a GPG timing no
    backup script reaches without an upload target is exactly the shape the
    create form rejects, and it is also the commonest shape among the tasks that
    still need a stamp. Dropping the rules is not a downgrade risk — a stamp
    re-saved at the schema default is still rejected by the timing-versus-format
    rule and by ``xtrabackup_aes256_keyfile``'s own ``Forbidden``.

    The binary/compression rules go with them, since the lenient bundle declares no
    sections: a task saved before the form gated compression on the backup binary
    can hold a pairing the create model now rejects, and the operator needs the edit
    form to load in order to correct it. The rejection stays on the create and
    update routes, so saving the reopened form still fails until the algorithm
    matches the binary.

    :param backup_dir: The backup root directory; optional here and un-stripped,
        unlike on the create model.
    :cvar __form_rules__: The create model's rules minus the upload-reachability
        pair and the binary/compression section.
    """

    __form_rules__: ClassVar[FormRules] = LENIENT_BACKUP_FORM_RULES

    backup_dir: Annotated[NonEmptyStr | EmptyStrToNone, BACKUP_DIR_UI] = None


def _extract_upload_from_meta(meta: dict[str, Any]) -> list[str]:
    """Return normalized upload providers from ``SERVER_LIST[0].UPLOAD``.

    A spelling no alias matches is passed through unchanged rather than dropped.
    Dropping it narrows the reconstructed selection, and the narrowed form
    dispatches a payload variant that cannot reach the missing provider; passing it
    through fails the form's own enum validation instead, which is visible.

    :param meta: The stored task meta whose ``config`` holds the server list.
    :return: The provider values for the form's ``upload`` field.
    """
    providers: list[str] = []
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
                    upload_raw = first_server.get("UPLOAD") or first_server.get(
                        "upload"
                    )
                    if isinstance(upload_raw, list):
                        for provider in upload_raw:
                            if not isinstance(provider, str):
                                continue
                            stripped = provider.strip()
                            member = _UPLOAD_PROVIDER_BY_ALIAS.get(stripped.upper())
                            providers.append(
                                stripped if member is None else member.value
                            )
    return providers


def reconstruct_mysql_backups_form(
    task: Task,
    ctx: FormBackfillContext,
) -> dict[str, Any] | None:
    """Rebuild a :class:`~app.extensions.apps.mysql_backups.forms.BackupCreate` body from a legacy task.

    Wraps :func:`~app.extensions.apps.mysql_backups.deps.parse_backup_task_data`, resolves
    ``service_id`` from inventory, reads ``upload`` from ``SERVER_LIST[0].UPLOAD``, and
    drops parse keys that are not on the create model (for example ``host`` / ``port`` /
    ``name``).

    :param task: The persisted mysql_backups task row.
    :param ctx: Shared backfill context carrying the inventory lookup table.
    :return: A create-model-shaped dict, or ``None`` when reconstruction fails.
    """
    meta = require_run_python_meta(task)
    if meta is None:
        return None

    try:
        parsed = parse_backup_task_data({"name": task.name, "data": task.data})
    except KeyError, TypeError, yaml.YAMLError:
        return None

    hostname = parsed.get("hostname")
    backup_type = parsed.get("backup_type")
    if (
        not isinstance(hostname, str)
        or not hostname.strip()
        or not isinstance(backup_type, str)
        or not backup_type.strip()
    ):
        return None

    service_id = resolve_service_from_meta(
        ctx,
        meta,
        ServiceTypeEnum.MYSQL,
        host=parsed.get("host"),
        port=parsed.get("port"),
    )
    if service_id is None:
        return None

    form_fields = {
        key: value
        for key, value in parsed.items()
        if key in _MYSQL_BACKUPS_FORM_FIELDS
        and key not in _EXPLICIT_FORM_KEYS
        and key not in _PARSE_ONLY_KEYS
        and value is not None
    }

    return {
        "task_name": task.name,
        "hostname": hostname.strip(),
        "service_id": service_id,
        "backup_type": backup_type.strip(),
        "alias": parsed.get("alias"),
        "upload": _extract_upload_from_meta(meta),
        "alert_on_fail": task.alert_on_fail,
        **form_fields,
    }


def repair_mysql_backups_stamp(
    stored_form: dict[str, Any],
    _task: Task,
    _ctx: FormBackfillContext,
) -> dict[str, Any] | None:
    """Add ``encryption_format`` to a stamp written before the selector existed.

    A stamp created through the schema form predating the selector names the GPG
    timings and the AES-256 key file but not the format they add up to, and the
    edit form fills that gap from the schema default — ``none`` — so an encrypted
    task reloads looking unencrypted. The format is derived from the stamp's own
    fields rather than from the task config, because the stamp is the record of
    what the operator submitted. Neither the task row nor the backfill context is
    read: the stamp carries every field the derivation needs.

    :param stored_form: A copy of the task's existing ``data['_form']``.
    :param _task: The stamped task row.
    :param _ctx: Shared backfill context.
    :return: The repaired form, or ``None`` when the stamp already names a format.
    """
    if stored_form.get("encryption_format") is not None:
        return None

    stored_form["encryption_format"] = encryption_format_for_passes(
        aes256=bool(stored_form.get("xtrabackup_aes256_keyfile")),
        gpg=bool(stored_form.get("encrypt") or stored_form.get("post_run_encrypt")),
    )
    return stored_form


FORM_BACKFILL_ENTRIES = [
    FormBackfillEntry(
        app_key="mysql_backups",
        owner=OWNER,
        create_model=LegacyBackupCreate,
        reconstructor=reconstruct_mysql_backups_form,
        stamp_repairer=repair_mysql_backups_stamp,
    ),
    RESTORE_FORM_BACKFILL_ENTRY,
]
