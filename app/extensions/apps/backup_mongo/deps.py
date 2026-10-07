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

"""Define dependencies for the Backups plugin."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated, Any

from aiohttp import ClientResponseError
from fastapi import Depends, HTTPException

from app.core.exceptions import HTTPConflictException, HTTPNotFoundException
from app.core.requests import as_json_object
from app.extensions.api.task_history_actors import task_actor_fields
from app.extensions.apps.backup_mongo.models import (
    BackupCreate,
    BackupDerivedTaskSummary,
    BackupTaskDetailResponse,
    BackupTaskResponse,
    BackupTaskWrite,
    BackupType,
    OWNER,
)
from app.extensions.apps.backup_mongo.schema import BACKUP_MONGO_DERIVED
from app.extensions.apps.backup_mongo.spec import (
    BackupMongoResolved,
    build_backup_mongo_spec,
)
from app.extensions.apps.framework import (
    build_default_task_response,
    extract_latest_task_status,
    get_task_latest_history,
    make_parent_resolver,
    make_task_dep,
)
from app.extensions.apps.framework.api import CascadeCreatePlan
from app.extensions.apps.framework.cascade import (
    build_derived_payload,
    cascade_create_tasks,
    cascade_update_tasks,
    CascadeResult,
    require_addressable_names,
)
from app.extensions.apps.framework.spec import stamp_form_input
from app.extensions.deps import (
    check_group_for_conflicted_running_tasks,
    get_created_entity,
    get_username_mapping,
    InventoryAPI,
    reject_if_protected,
    task_path,
    TaskAPI,
)
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import (
    Task,
    TaskHistoryLatestStatus,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)

logger = logging.getLogger(__name__)

PBM_LATEST_STATUS_TAIL_BYTES = 4096
BACKUP_DERIVED_SUFFIXES = tuple(spec.name_suffix for spec in BACKUP_MONGO_DERIVED)


def backup_derived_task_names(parent_name: str) -> list[str]:
    """Return derived task names for a parent backup config task.

    :param parent_name: The name of the parent ``pbm_config`` task.
    :type parent_name: str
    :return: Derived task names in schema declaration order.
    :rtype: list[str]
    """
    return [f"{parent_name}{suffix}" for suffix in BACKUP_DERIVED_SUFFIXES]


def backup_create_from_write(body: BackupTaskWrite) -> BackupCreate:
    """Convert a :class:`BackupTaskWrite` body into a :class:`BackupCreate` model.

    Always sets ``backup_type`` to ``pbm_config``; POST creates the parent
    config task and derived logical, physical, status, and incremental siblings.

    :param body: The JSON request body for backup task creation.
    :type body: BackupTaskWrite
    :return: A :class:`BackupCreate` instance for payload construction.
    :rtype: BackupCreate
    """
    return BackupCreate.model_validate(
        {
            **body.model_dump(mode="json"),
            "backup_type": BackupType.PBM_CONFIG,
        },
        from_attributes=False,
    )


async def build_backup_task_payload(
    form: BackupCreate,
    inventory_api: InventoryAPI,
) -> TaskWrite:
    """Build the payload for a Backups task to be executed.

    :param form: The form data for the Backups creation.
    :type form: BackupCreate
    :param inventory_api: The Inventory API to get entities from.
    :type inventory_api: InventoryAPI
    :return: A fully constructed ``TaskWrite`` object containing all the
        necessary configuration to create the Backup task.
    :rtype: TaskWrite
    """
    try:
        service = await get_created_entity(
            inventory_api,
            SyncInventoryEntityTypeEnum.SERVICE,
            form.service_id,
            type=ServiceTypeEnum.MONGODB,
        )
    except HTTPNotFoundException:
        # PBM tasks run off ``form.hostname`` and the generated config; the
        # service is only fetched to populate ``_service_name`` for PMM, so a
        # service deleted between form load and submit degrades to a node-only
        # annotation.
        service = None

    resolved = BackupMongoResolved(
        service_name=service.name if service is not None else None
    )
    return build_backup_mongo_spec(form, resolved)


async def build_backup_cascade_plan(
    body: BackupTaskWrite,
    inventory_api: InventoryAPI,
) -> CascadeCreatePlan:
    """Build the cascade create plan for a backup_mongo task group.

    Convert the body to a :class:`BackupCreate` form, assemble the parent
    ``pbm_config`` write, and bind a cascade closure that POSTs the parent plus
    its derived ``pbm_logical`` / ``pbm_physical`` / ``pbm_status`` /
    ``pbm_incremental`` siblings. The
    parent is re-serialised *inside* the closure so it carries the form stamp
    :func:`~app.extensions.apps.framework.api.derive_cascade_create_route` applies first.

    :param body: The JSON request body for backup task creation.
    :param inventory_api: The Inventory API to look up the backup service.
    :return: The plan carrying the parent write, form, and cascade closure.
    """
    form = backup_create_from_write(body)
    task_write = await build_backup_task_payload(form, inventory_api)
    return CascadeCreatePlan(
        parent_write=task_write,
        form=form,
        cascade=lambda api: cascade_create_tasks(
            api, task_write.model_dump(), BACKUP_MONGO_DERIVED
        ),
    )


BackupCascadePlan = Annotated[CascadeCreatePlan, Depends(build_backup_cascade_plan)]


def build_backup_mongo_api_task_response(
    task: Task,
    *,
    status: TaskHistoryStatusEnum | None = None,
    last_executed_at: datetime | None = None,
    context: dict[str, str] | None = None,
) -> BackupTaskResponse:
    """Build a backup task response object for the JSON API.

    :param task: The backup task retrieved from the Tasks API.
    :param status: The latest known execution status for the task.
    :param last_executed_at: The task's most recent finish time (``max``
        ``finished_at``), or ``None`` until it has finished once.
    :param context: The username map used to resolve ``created_by`` /
        ``last_updated_by`` to system labels or provider usernames; falls back to
        the raw id when neither resolves it.
    :return: A validated backup task API response object.
    """
    data = task.data
    meta = data.get("meta") or {}
    return build_default_task_response(
        BackupTaskResponse,
        task,
        status,
        last_executed_at=last_executed_at,
        extras={
            "hostname": meta.get("target"),
            "backup_type": str(data.get("backup_type", "")),
            "service_type": ServiceTypeEnum.MONGODB,
            **task_actor_fields(task, context or {}),
        },
    )


def _gathered_latest_history(
    result: TaskHistoryLatestStatus | BaseException | None,
) -> TaskHistoryLatestStatus | None:
    """Map a ``gather`` result to a latest-history projection, failures -> None."""
    return None if isinstance(result, BaseException) else result


async def _fetch_latest_pbm_status(
    tasks_api: TaskAPI, pbm_status_tasks: list[dict[str, Any]]
) -> str | None:
    """Return the tail of the latest PBM status task's stdout.

    Streams the ``run-script`` logs for the most recent PBM status history
    record through the tasks API and returns at most
    ``PBM_LATEST_STATUS_TAIL_BYTES`` characters of the concatenated stdout
    content. The rolling buffer is truncated to that window on every append
    so long-running PBM status tasks do not materialize their full log in
    memory for this best-effort UI panel.

    :param tasks_api: The TaskAPI instance used to stream task logs.
    :type tasks_api: TaskAPI
    :param pbm_status_tasks: The list of PBM status history records returned by
        the tasks API, or an empty list when no history exists.
    :type pbm_status_tasks: list[dict[str, Any]]
    :return: The tail of the latest PBM status stdout, or ``None`` when no
        history exists or the stream cannot be read.
    :rtype: str | None
    """
    try:
        pbm_status_id = pbm_status_tasks[0]["id"]
    except (IndexError, KeyError, TypeError):
        return None
    tail = ""
    try:
        async for log_entry in tasks_api.stream(
            f"/history/{pbm_status_id}/logs/",
            params={"step": "run-script"},
        ):
            if not log_entry:
                continue
            log_data = json.loads(log_entry)
            if log_data.get("type") == TaskLogType.STDOUT and log_data.get("msg"):
                tail = (tail + log_data["msg"])[-PBM_LATEST_STATUS_TAIL_BYTES:]
    except (ClientResponseError, HTTPException, ValueError, KeyError):
        logger.exception(
            "Failed to fetch latest_status for backup_mongo task %s",
            pbm_status_id,
        )
        return None
    if not tail:
        return None
    return tail


async def _fetch_backup_derived_detail(
    derived_name: str,
    tasks_api: TaskAPI,
) -> tuple[Task, list[dict[str, Any]]] | None:
    """Fetch a derived backup task and its history, or ``None`` when missing."""
    try:
        derived = await get_backups_task(derived_name, tasks_api)
    except HTTPNotFoundException:
        return None
    history_response = as_json_object(
        await tasks_api.get(task_path(derived.name, "/history/"))
    )
    return derived, history_response["items"]


async def build_backup_mongo_api_detail_response(
    task: Task,
    tasks_api: TaskAPI,
) -> BackupTaskDetailResponse:
    """Build a backup task detail response for the JSON API.

    Aggregates latest execution status for the parent ``pbm_config`` task and
    each derived logical, physical, status, and incremental sibling. When a status
    sibling exists, includes a tail of its latest stdout for the PBM status panel.
    Resolves the parent's actor fields through the active provider's username map.

    :param task: The parent backup config task.
    :param tasks_api: The TaskAPI instance used to query tasks and history.
    :return: A validated backup task detail API response object.
    """
    derived_names = backup_derived_task_names(task.name)
    gather_results = await asyncio.gather(
        get_task_latest_history(tasks_api, task.name),
        *(_fetch_backup_derived_detail(name, tasks_api) for name in derived_names),
        return_exceptions=True,
    )
    parent_latest = _gathered_latest_history(gather_results[0])
    derived_results = gather_results[1:]
    derived_tasks = []
    latest_pbm_status = None

    for derived_detail in derived_results:
        if isinstance(derived_detail, BaseException) or derived_detail is None:
            continue
        derived, history_items = derived_detail
        derived_status = extract_latest_task_status(history_items)
        derived_tasks.append(
            BackupDerivedTaskSummary(
                name=derived.name,
                backup_type=str(derived.data.get("backup_type", "")),
                status=derived_status,
            )
        )
        if derived.data.get("backup_type") == BackupType.PBM_STATUS.value:
            latest_pbm_status = await _fetch_latest_pbm_status(tasks_api, history_items)

    base = build_backup_mongo_api_task_response(
        task,
        status=parent_latest.status if parent_latest else None,
        last_executed_at=parent_latest.finished_at if parent_latest else None,
        context=await get_username_mapping(),
    )
    return BackupTaskDetailResponse(
        **base.model_dump_with_excluded_fields(),
        derived_tasks=derived_tasks,
        latest_pbm_status=latest_pbm_status,
    )


get_backups_task = make_task_dep(OWNER)

resolve_backup_parent_task = make_parent_resolver(get_backups_task)

BackupsTask = Annotated[Task, Depends(get_backups_task)]


async def get_editable_backup_parent_task(
    task_name: str,
    tasks_api: TaskAPI,
) -> Task:
    """Resolve the parent backup task or raise when it is not editable.

    Resolves a derived sibling name (``-logical`` / ``-physical`` / ``-status``)
    to the parent, rejects protected tasks, then blocks the edit while a run of
    *any* group member is in flight. Backups execute on the derived legs, not the
    parent config task, so checking the parent's history alone would let a ``PUT``
    mutate a running group.

    :param task_name: The task name from the URL path; may be a derived sibling.
    :param tasks_api: The TaskAPI instance used to resolve and gate the parent.
    :raises HTTPConflictException: If the parent is protected or any group member
        has a running/pending run.
    :return: The editable parent ``pbm_config`` task.
    """
    parent_task = await resolve_backup_parent_task(task_name, tasks_api)
    reject_if_protected(parent_task)
    group_names = [parent_task.name, *backup_derived_task_names(parent_task.name)]
    await check_group_for_conflicted_running_tasks(group_names, tasks_api)
    return parent_task


EditableBackupParent = Annotated[
    Task,
    Depends(get_editable_backup_parent_task),
]


_BACKUP_GROUP_RENAME_MESSAGE = (
    "Renaming a backup task group is not supported; the parent config task and "
    "its derived siblings are wired by name at create time. Submit the update "
    "with the existing task name."
)


def ensure_backup_group_update_preserves_names(
    parent_existing_name: str,
    updated_parent_name: str,
) -> None:
    """Reject a backup group update that renames the parent task.

    :param parent_existing_name: The parent config task name from the URL path.
    :param updated_parent_name: The ``task_name`` submitted in the request body.
    :raises HTTPConflictException: When the submitted name differs from the
        existing parent name.
    """
    if updated_parent_name != parent_existing_name:
        raise HTTPConflictException(_BACKUP_GROUP_RENAME_MESSAGE)


async def ensure_backup_derived_siblings(
    tasks_api: TaskAPI,
    parent_name: str,
    parent_payload: dict[str, Any],
) -> None:
    """POST any missing derived siblings before a cascade update.

    Groups created before incremental was added lack ``-incremental``. Creating
    the missing sibling first keeps :func:`cascade_update_tasks` from partially
    mutating the group and then failing on a 404 PUT.

    :param tasks_api: The TaskAPI used to GET existing legs and POST missing ones.
    :param parent_name: The parent ``pbm_config`` task name.
    :param parent_payload: The updated parent payload used to build missing children.
    :raises HTTPUnprocessableEntityException: If a derived sibling's name — the
        one probed, or the one an update's rename would create — is not a single
        plain URL path segment. Raised before the first request, so a later
        sibling's refusal cannot leave an earlier one created.
    """
    derived_paths = [
        task_path(f"{parent_name}{spec.name_suffix}") for spec in BACKUP_MONGO_DERIVED
    ]
    child_payloads = [
        build_derived_payload(parent_payload, spec) for spec in BACKUP_MONGO_DERIVED
    ]
    require_addressable_names(child_payloads)
    for derived_path, child_payload in zip(derived_paths, child_payloads, strict=True):
        try:
            await tasks_api.get(derived_path)
        except HTTPNotFoundException:
            await tasks_api.post("/", json=child_payload)


async def update_backup_task_group(
    tasks_api: TaskAPI,
    parent_task: Task,
    form: BackupCreate,
    inventory_api: InventoryAPI,
) -> CascadeResult:
    """Cascade-update the parent backup task and its derived siblings.

    Rebuilds the parent ``pbm_config`` payload, re-stamps ``_form`` so the edit
    page keeps prefilling across repeated edits, backfills any missing derived
    siblings (for example ``-incremental`` on groups created before that leg
    existed), and PUTs the parent plus its derived logical, physical, status,
    and incremental legs in place. Returns the per-leg outcome; the caller
    raises on partial failure.

    :param tasks_api: The TaskAPI instance used to update tasks.
    :param parent_task: The parent backup config task.
    :param form: The validated update form (``backup_type`` pinned to
        ``pbm_config``).
    :param inventory_api: The Inventory API to look up the backup service.
    :return: The cascade outcome across the parent and derived legs.
    """
    updated_parent = await build_backup_task_payload(form, inventory_api)
    stamp_form_input(updated_parent, form)
    parent_payload = updated_parent.model_dump()
    await ensure_backup_derived_siblings(tasks_api, parent_task.name, parent_payload)
    return await cascade_update_tasks(
        tasks_api,
        parent_task.name,
        parent_payload,
        backup_derived_task_names(parent_task.name),
        BACKUP_MONGO_DERIVED,
    )
