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

"""Define dependencies for the Restores plugin."""

from datetime import datetime
from typing import Annotated, Any

import yaml
from fastapi import Body
from pydantic import ValidationError

from app.core.exceptions import (
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.extensions.api.task_history_actors import task_actor_fields
from app.extensions.apps.framework import build_default_task_response
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY, stamp_form_input
from app.extensions.apps.mysql_backups.models import (
    BackupType,
    UNKNOWN_SERVICE_SENTINEL,
)
from app.extensions.apps.mysql_backups.restore.models import (
    repair_source_declaration,
    RestoreCreate,
    RestoresResponse,
)
from app.extensions.apps.mysql_backups.restore.spec import (
    build_restore_spec,
    RestoreResolved,
)
from app.extensions.deps import get_created_entity, InventoryAPI
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import Task, TaskHistoryStatusEnum, TaskWrite


async def resolve_restore_entities(
    form: RestoreCreate, inventory_api: InventoryAPI
) -> RestoreResolved:
    """Resolve the inventory entities a restore form references.

    MyDumper always resolves its MySQL service (eager-raising when ``service_id``
    is unset or stale), splits the service address into ``dest_host`` / ``dest_port``,
    and resolves the optional schema into the restore ``database``. XtraBackup and
    Binlog have no destination service: a ``service_id`` of ``None`` or the
    ``UNKNOWN_SERVICE_SENTINEL`` placeholder skips the lookup, and a stale id whose
    service was deleted degrades to a node-only annotation on a 404.

    ``ServiceRef(allow_custom=True)`` also lets the form submit a typed service
    name instead of an inventory id. A name is never a valid detail-route segment,
    so it annotates XtraBackup and Binlog restores as-is and is rejected for
    MyDumper, which needs the service address to derive its destination.

    A resolved service whose address carries no port yields ``dest_host`` alone,
    and the payload applies its own ``3306`` default. One resolving to no address
    at all is rejected rather than tolerated: an unset ``dest_host`` drops
    ``DEST_HOST`` from the emitted config, which the payload reads as
    ``localhost`` and loads into whatever MySQL runs on the executor instead of
    the destination the operator chose.

    :param form: The validated restore create form.
    :param inventory_api: The Inventory API used to resolve the references.
    :return: The resolved facts fed into :func:`build_restore_spec`.
    :raises HTTPException: When a MyDumper service reference is a typed name or the
        unknown-service placeholder, its lookup fails, or it resolves to a service
        carrying no address; or when a non-MyDumper lookup fails with a status
        other than 404.
    """
    if form.backup_type == BackupType.MYDUMPER:
        if form.service_id is None or not form.service_id.isdecimal():
            raise HTTPUnprocessableEntityException(
                detail=(
                    "Destination Database Service must be an existing MySQL service "
                    "for a MyDumper restore"
                )
            )
        service = await get_created_entity(
            inventory_api,
            SyncInventoryEntityTypeEnum.SERVICE,
            int(form.service_id),
            type=ServiceTypeEnum.MYSQL,
        )
        host, _, port = (service.address or "").partition(":")
        dest_host = host.strip()
        if not dest_host:
            raise HTTPUnprocessableEntityException(
                detail=(
                    "Destination Database Service must resolve to a network address "
                    "for a MyDumper restore"
                )
            )
        port = port.strip()
        dest_port = int(port) if port else None
        database = None
        if (
            form.schema_id is not None
            and form.schema_id.isdigit()
            and int(form.schema_id) > 0
        ):
            schema = await get_created_entity(
                inventory_api,
                SyncInventoryEntityTypeEnum.SCHEMA,
                int(form.schema_id),
                service_id=service.id,
            )
            database = schema.name
        return RestoreResolved(
            service_name=service.name,
            dest_host=dest_host,
            dest_port=dest_port,
            database=database,
        )

    if form.service_id and form.service_id != UNKNOWN_SERVICE_SENTINEL:
        if not form.service_id.isdigit():
            return RestoreResolved(service_name=form.service_id)
        try:
            service = await get_created_entity(
                inventory_api,
                SyncInventoryEntityTypeEnum.SERVICE,
                int(form.service_id),
                type=ServiceTypeEnum.MYSQL,
            )
        except HTTPNotFoundException:
            return RestoreResolved()
        return RestoreResolved(service_name=service.name)

    return RestoreResolved()


async def build_restore_payload(
    form: Annotated[RestoreCreate, Body()],
    inventory_api: InventoryAPI,
) -> TaskWrite:
    """Build the restore task payload for the derived JSON create route.

    The ``payload_builder`` the framework uses verbatim as the create dependency:
    resolve the form's references, then feed the shared pure
    :func:`build_restore_spec`, so a JSON-created task's payload stays
    byte-identical to a Jinja-form-created one.

    :param form: The JSON restore create body.
    :param inventory_api: The Inventory API used to resolve references.
    :return: The restore ``TaskWrite``.
    """
    resolved = await resolve_restore_entities(form, inventory_api)
    write = build_restore_spec(form, resolved)
    stamp_form_input(write, form)
    return write


def _extract_restore_config(task: Task) -> tuple[BackupType | None, Any, Any]:
    """Read backup type and destination host/port out of a restore task's config.

    :param task: The restore task to inspect.
    :return: The ``(backup_type, dest_host, dest_port)`` triple, each ``None`` when
        the config is absent or unparseable.
    """
    meta = task.data.get("meta") if task.data else None
    raw_config = meta.get("config") if meta else None
    if not raw_config:
        return None, None, None
    try:
        config = yaml.safe_load(raw_config)
    except yaml.YAMLError:
        config = None
    server_list = config.get("SERVER_LIST") if isinstance(config, dict) else None
    server = server_list[0] if isinstance(server_list, list) and server_list else None
    if not isinstance(server, dict):
        return None, None, None
    host = server.get("DEST_HOST")
    port = server.get("DEST_PORT")
    raw_type = server.get("BACKUP_TYPE")
    if raw_type is None:
        return None, host, port
    try:
        return BackupType(raw_type), host, port
    except ValueError:
        return None, host, port


def _declared_source_override(task: Task) -> dict[str, Any]:
    """Return a ``data`` override declaring the source of a stamp that describes it poorly.

    The edit form seeds each field from the served stamp and falls back to the
    schema default where the stamp has no value, so a stamp written before the
    source controls existed would seed ``source_transport`` to ``local``. The
    gates then hide the SSH and object-store fields, and a hidden field is
    dropped from the submission entirely, so saving that form would discard
    credentials the restore still needs. The same holds for an AES-256 key file a
    stamp names without declaring the format that reveals it. Repairing the stamp
    here means the form opens on the source its stored values imply, keeping a key
    file the engine can read and dropping one it cannot.

    Re-validating through :class:`RestoreCreate` rather than serving the repair
    directly keeps the served stamp exactly what a subsequent ``PUT`` would
    accept. It is tolerant of a stamp that cannot be validated at all, because
    this builder also serves the list route, where one unparseable task must not
    take out the whole page. A key the form no longer declares is ignored rather
    than failing the validation: the edit form never submits it, and the served
    stamp is the validated model, which leaves it out.

    :param task: The restore task being serialized.
    :return: A single-key ``data`` override, or an empty mapping when the stamp
        already describes its source, is absent, or does not validate.
    """
    data = task.data
    if not data:
        return {}
    stored_form = data.get(RESERVED_FORM_KEY)
    if not isinstance(stored_form, dict):
        return {}
    repaired = repair_source_declaration(stored_form)
    if repaired is None:
        return {}
    try:
        declared = RestoreCreate.model_validate(repaired, extra="ignore").model_dump(
            mode="json"
        )
    except ValidationError:
        return {}
    return {"data": {**data, RESERVED_FORM_KEY: declared}}


def build_restore_api_task_response(
    task: Task,
    status: TaskHistoryStatusEnum | None = None,
    *,
    last_executed_at: datetime | None = None,
    context: dict[str, str] | None = None,
) -> RestoresResponse:
    """Build a ``RestoresResponse`` for the JSON API list/detail routes.

    :param task: The restore task retrieved from the Tasks API.
    :param status: The latest known execution status for the task.
    :param last_executed_at: The task's most recent finish time (``max``
        ``finished_at``), or ``None`` until it has finished once.
    :param context: The username map bound by ``response_context_provider``, used
        to resolve ``created_by`` / ``last_updated_by`` to system labels or
        provider usernames; falls back to the raw id when neither resolves it.
    :return: A validated restore task API response object.
    """
    backup_type, host, port = _extract_restore_config(task)
    meta = task.data.get("meta") if task.data else None
    return build_default_task_response(
        RestoresResponse,
        task,
        status,
        last_executed_at=last_executed_at,
        extras={
            "backup_type": backup_type,
            "host": host,
            "port": port,
            "hostname": meta.get("target") if meta else None,
            **task_actor_fields(task, context or {}),
            **_declared_source_override(task),
        },
    )


def parse_restore_task_data(task: dict[str, Any]) -> dict[str, Any]:
    """Parse restore task data for editing.

    Extracts configuration from an existing restore task to populate the edit form.

    :param task: The task data retrieved from the Tasks API.
    :type task: dict[str, Any]
    :return: A dictionary containing parsed restore configuration.
    :rtype: dict[str, Any]
    """
    data = task["data"]
    meta = data["meta"]
    task_config = yaml.safe_load(meta["config"])
    server_config = task_config["SERVER_LIST"][0]
    all_servers_config = task_config.get("ALL_SERVERS", {})

    result = {
        "name": task["name"],
        "hostname": meta["target"],
        "backup_type": server_config["BACKUP_TYPE"],
        "service_id": None,
        "host": server_config.get("DEST_HOST"),
        "port": server_config.get("DEST_PORT") or 3306,
        "database": server_config.get("DATABASE"),
    }

    for config in [server_config, all_servers_config]:
        result.update(
            {k.lower(): v for k, v in config.items() if k.lower() not in result}
        )

    return result
