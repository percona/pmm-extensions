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

"""Define Celery tasks and utilities for the Tasks app.

This module defines functions for executing tasks asynchronously via Celery,
along with utility functions to process queue items.
"""

import json
import logging
import typing
from contextlib import AsyncExitStack
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from celery import Task as CeleryTask
from celery.app.task import Context
from celery.signals import (
    task_prerun,
    task_revoked,
    worker_process_init,
    worker_process_shutdown,
)
from cryptography import x509
from fastapi.encoders import jsonable_encoder
from nomad.api.exceptions import BaseNomadException
from sqlalchemy import cast, func, literal, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import QueryableAttribute, undefer
from sqlalchemy.sql import ColumnElement
from sqlmodel import col, or_
from sqlmodel.ext.asyncio.session import AsyncSession

from app.celery import celery
from app.core.alerts.config import alert_service
from app.core.alerts.models import AlertSeverity
from app.core.db.utils import (
    func_json_extract,
    prepare_unsafe_value_for_json_comparison,
)
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
)
from app.core.pmm import await_annotation, schedule_annotation
from app.core.settings_override.constants import ANONYMIZER_SETTINGS, TASKS_SETTINGS
from app.core.settings_override.lifecycle import ProxyEntry, ProxyRegistry
from app.core.settings_override.worker import WorkerRefresher
from app.core.utils import utc_now
from app.core.utils.fields import DatabaseDialect
from app.core.utils.path import PayloadReferenceError
from app.tasks.anonymizer.config import anonymizer_settings, AnonymizerSettings
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.crud import (
    DispatchLockManager,
    TaskHistoryLogManager,
    TaskHistoryManager,
    TaskManager,
)
from app.tasks.db import get_async_session_maker
from app.tasks.deps import (
    get_executable_task_by_name,
    get_executor,
    prepare_task_history,
)
from app.tasks.execution.models import BaseExecutor
from app.tasks.execution.nomad_lifecycle import normalize_nomad_config_value
from app.tasks.execution_request_secrets import ENCRYPTED_META_KEYS
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    DispatchLock,
    SYSTEM_USER,
    Task,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLogType,
)
from app.tasks.periodic.models import PeriodicTaskExecuteRequest
from app.tasks.run_result import maybe_record_run

logger = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10

#: How old a dispatch-lock row must be before a later dispatch may clear it.
#: Named because two places depend on the same number: the sweep that reclaims a
#: lock whose dispatch died holding it, and the release path, which tolerates its
#: own failure precisely because that sweep will reclaim the row.
_DISPATCH_LOCK_STALE_SECONDS = 30


@task_revoked.connect
def task_revoked_handler(*, request: Context, expired: bool, **kwargs: Any) -> None:
    """Handle task revocation by logging the event."""
    queue_id = request.args[0] if request.args else request.kwargs.get("queue_id")
    if (
        request.get("task") == "app.tasks.celery.execute_task_queue"
        and queue_id
        and expired
    ):
        logger.info("Deleting expired TaskHistory %s", queue_id)
        celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
            delete_task_history(queue_id)
        )


def build_tasks_override_proxies() -> ProxyRegistry:
    """Compose the Tasks-side proxy registry.

    :return: The Tasks and Anonymizer proxy entries keyed by class identifier.
    """
    return {
        TASKS_SETTINGS: ProxyEntry(tasks_settings, TasksSettings),
        ANONYMIZER_SETTINGS: ProxyEntry(anonymizer_settings, AnonymizerSettings),
    }


_refresher = WorkerRefresher(
    lambda: celery.loop,  # ty: ignore[unresolved-attribute]
    lambda: get_async_session_maker(),
    build_tasks_override_proxies,
)


@worker_process_init.connect
def start_settings_override_refresher(**kwargs: Any) -> None:
    """Seed and arm the Tasks worker's DB-backed settings-override refresher.

    Wired to ``worker_process_init`` so each prefork child runs its own refresher
    bound to that child's event loop. ``app.celery`` registers
    ``init_child_event_loop`` first (it is imported before this module), so Celery
    dispatches it first and the child loop is recreated before ``WorkerRefresher``
    resolves it. The enabled gate, the idempotent early-return, the initial inline
    seed and the shutdown disarm all live in :class:`WorkerRefresher`; after the
    seed, refreshes are pulled from ``task_prerun`` via
    :func:`refresh_tasks_overrides_if_due`. The inline seed is bounded by a
    fraction of ``celery.conf.worker_proc_alive_timeout`` so a hanging database
    cannot push the child past the prefork pool's liveness deadline; on expiry
    the child is still armed and may retain a possibly incomplete seed until
    the next due task boundary.

    ``anonymizer_settings._resolve()`` runs unconditionally for validation even
    when the refresher is disabled. Celery catches and logs whatever a signal
    receiver raises, so an invalid config here leaves the child running without
    this refresher.

    :param kwargs: The ``worker_process_init`` signal keyword arguments (unused).
    :raises ValidationError: Propagates from ``anonymizer_settings._resolve()``
        when the anonymizer config is invalid. Celery logs it and carries on
        dispatching; the child starts without this refresher.
    :raises Exception: Propagates a session-maker failure from the initial
        inline refresh, absorbed the same way. Per-proxy refresh failures and
        a bounded-seed expiry are caught and logged inside the refresher; the
        latter still arms the child for boundary refresh.
    """
    anonymizer_settings._resolve()  # noqa: SLF001  # ty: ignore[unresolved-attribute]
    _refresher.start(proc_alive_timeout=celery.conf.worker_proc_alive_timeout)


@task_prerun.connect
def refresh_tasks_overrides_if_due(**_: Any) -> None:
    """Refresh Tasks-side overrides at this task boundary when the interval is due.

    Passes through to :meth:`WorkerRefresher.maybe_refresh`, which no-ops when
    disarmed or inside the interval. Each refresher (PMM Extensions side and Tasks-side)
    keeps its own due-check state, so a boundary that is due for both pays two
    refreshes.

    :param _: The ``task_prerun`` signal keyword arguments (unused).
    """
    _refresher.maybe_refresh()


@worker_process_shutdown.connect
def stop_settings_override_refresher(**kwargs: Any) -> None:
    """Disarm the worker's settings-override refresher on shutdown.

    A no-op when the refresher never started (disabled, or shutdown fired before
    init). After disarm, :func:`refresh_tasks_overrides_if_due` no-ops.

    :param kwargs: The ``worker_process_shutdown`` signal keyword arguments
        (unused).
    """
    _refresher.stop()


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": celery.conf.max_retries},
)
def execute_task_queue(self: CeleryTask, queue_id: int) -> dict[str, Any]:
    """Trigger a Celery task by executing a queue item.

    :param self: The Celery task instance.
    :type self: CeleryTask
    :param queue_id: The ID of the queue item to trigger.
    :type queue_id: int
    :return: The data of the processed TaskHistory.
    :rtype: dict[str, Any]
    """
    logger.info("Executing task with queue_id: %s", queue_id)
    queue_item = celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        get_task_history(queue_id)
    )
    return jsonable_encoder(
        celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
            dispatch_queue_item(queue_item, await_annotations=True)
        )
    )


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": celery.conf.max_retries},
)
def execute_task_by_name(
    self: CeleryTask,
    task_name: str,
    periodic_task_name: str | None = None,
    execution_data: PeriodicTaskExecuteRequest | None = None,
) -> dict[str, Any]:
    """Define Celery task to execute a PMM Extensions task by name.

    :param self: The Celery task instance.
    :type self: CeleryTask
    :param task_name: The name of the task to execute.
    :type task_name: int | None
    :param periodic_task_name: The name of the periodic task, if available.
    :type periodic_task_name: str | None
    :param execution_data: Execution details and parameters, if any.
    :type execution_data: PeriodicTaskExecuteRequest | None
    :return: The data of the processed TaskHistory.
    :rtype: dict[str, Any]
    """
    task_history = celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        prepare_periodic_task_history(task_name, execution_data)
    )
    try:
        failed = celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
            _pre_dispatch_payload_check(task_history, task_name, periodic_task_name)
        )
        if failed is not None:
            return jsonable_encoder(failed)
        skipped = celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
            _pre_dispatch_health_check(task_history, task_name, periodic_task_name)
        )
        if skipped is not None:
            return jsonable_encoder(skipped)
        task_history = (
            celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
                dispatch_queue_item(
                    task_history,
                    await_annotations=True,
                    periodic_task_name=periodic_task_name,
                )
            )
        )
    except BaseNomadException:
        alert_msg = (
            f"Failed to dispatch periodic task "
            f"{periodic_task_name or task_name}: "
            f"error getting a response from Nomad"
        )
        logger.exception(alert_msg)
        if task_history.task.alert_on_fail:
            dedup_key = f"task:{task_name}:{task_history.execution_request.target}"
            alert_data = {
                "summary": alert_msg,
                "source": f"{task_name}:{task_history.execution_request.target}",
                "severity": AlertSeverity.ERROR,
                "class": "task_dispatch_failure",
                "dedup_key": dedup_key,
            }
            if periodic_task_name:
                alert_data["source"] = f"{periodic_task_name}:{alert_data['source']}"
            celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
                alert_service.trigger(alert_data)
            )
    return jsonable_encoder(task_history)


async def _pre_dispatch_health_check(
    task_history: TaskHistory,
    task_name: str,
    periodic_task_name: str | None,
) -> TaskHistory | None:
    """Gate periodic Nomad dispatches on target-host readiness.

    Resolve the proxy wrapper via :meth:`TaskManager.get_root_task` so the gate
    checks the backend that will actually run. When the resolved backend is
    Nomad and the target is not present in ``executor.get_hosts()``, delegate
    to :func:`_skip_dispatch_unhealthy_target` and return the saved FAILED
    row. Return ``None`` to proceed to normal dispatch (non-Nomad resolved
    backend, or target healthy).

    :param task_history: The unsaved TaskHistory from
        :func:`prepare_periodic_task_history`.
    :type task_history: TaskHistory
    :param task_name: The PMM Extensions task name (used for dedup key and alert source).
    :type task_name: str
    :param periodic_task_name: The periodic-task name, if any (used to enrich
        the alert source).
    :type periodic_task_name: str | None
    :return: The saved FAILED TaskHistory when the gate fires; ``None`` to
        proceed with normal dispatch.
    :rtype: TaskHistory | None
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        root_task = await TaskManager.get_root_task(session, task_history.task)
    if root_task.backend != TaskBackendEnum.NOMAD:
        return None
    executor = get_executor_for_task(root_task)
    if task_history.execution_request.target in executor.get_hosts():
        return None
    return await _skip_dispatch_unhealthy_target(
        task_history, task_name, periodic_task_name
    )


async def _persist_failed_dispatch(
    task_history: TaskHistory,
    task_name: str,
    periodic_task_name: str | None,
    reason: str,
    session: AsyncSession | None = None,
) -> TaskHistory:
    """Persist a terminal FAILED TaskHistory with a stderr chunk and optional alert.

    Mark ``task_history`` FAILED with ``finished_at`` set, commit it via
    :meth:`TaskHistoryManager.save`, re-load the deferred ``execution_request``
    column so the returned instance can be serialized after the session closes,
    append ``reason`` as a best-effort stderr log chunk, and — when
    ``task.alert_on_fail`` is truthy — fire the dispatch-failure alert. Return
    without raising so Celery's ``autoretry_for=(Exception,)`` does not fire.

    :param task_history: The unsaved TaskHistory to fail.
    :param task_name: The PMM Extensions task name (used for the dedup key and alert source).
    :param periodic_task_name: The periodic-task name, if any (enriches the alert
        source).
    :param reason: The operator-facing failure reason, written to stderr and used
        as the alert summary.
    :param session: The caller's session to persist through, if any. Callers that
        already hold ``task_history`` (and its ``task`` relationship) attached to
        an open session must pass it so the save does not attach the instance to a
        second session; ``None`` opens a private internal session for callers whose
        ``task_history`` is detached (the Celery queue, periodic, and chain paths).
    :return: The saved, FAILED TaskHistory.
    """
    target = task_history.execution_request.target
    alert_on_fail = task_history.task.alert_on_fail
    task_history.status = TaskHistoryStatusEnum.FAILED
    task_history.set_failure_reason(reason)
    task_history.finished_at = utc_now()

    async_session = get_async_session_maker()
    async with AsyncExitStack() as stack:
        active = session or await stack.enter_async_context(async_session())
        saved = await TaskHistoryManager.save(active, task_history)
        await active.refresh(saved, attribute_names=["execution_request"])

    async with AsyncExitStack() as stack:
        # Write through the caller's session when passed: its engine may differ
        # from the periodic maker's, and the chunk must land where callers read.
        log_session = session or await stack.enter_async_context(async_session())
        try:
            await TaskHistoryLogWriter.append(
                log_session,
                saved.id,
                source="execution",
                stream=TaskLogType.STDERR,
                new_bytes=reason.encode("utf-8"),
                force_flush=True,
            )
        except Exception:
            await log_session.rollback()
            logger.exception(
                "Failed to write stderr log chunk for failed dispatch of %r on %r",
                task_name,
                target,
            )

    if alert_on_fail:
        dedup_key = f"task:{task_name}:{target}"
        alert_source = f"{task_name}:{target}"
        if periodic_task_name:
            alert_source = f"{periodic_task_name}:{alert_source}"
        await alert_service.trigger(
            {
                "summary": reason,
                "source": alert_source,
                "severity": AlertSeverity.ERROR,
                "class": "task_dispatch_failure",
                "dedup_key": dedup_key,
            }
        )
    return saved


async def _skip_dispatch_unhealthy_target(
    task_history: TaskHistory,
    task_name: str,
    periodic_task_name: str | None,
) -> TaskHistory:
    """Persist FAILED TaskHistory and fire a deduped alert when the target is unhealthy.

    Build the "target not ready" reason, log it at warning level, and delegate
    persistence, logging, and alerting to :func:`_persist_failed_dispatch`.
    Return without raising so Celery's ``autoretry_for=(Exception,)`` does not
    fire; the next Beat tick will retry once the host is healthy.

    :param task_history: The unsaved TaskHistory built by
        :func:`prepare_periodic_task_history`.
    :param task_name: The PMM Extensions task name (used for dedup key and alert source).
    :param periodic_task_name: The periodic-task name, if any (used to enrich
        the alert source).
    :return: The saved, FAILED TaskHistory.
    """
    reason = (
        f"Target host {task_history.execution_request.target!r} is not ready on "
        f"Nomad; skipping dispatch of periodic task "
        f"{periodic_task_name or task_name!r}"
    )
    logger.warning(reason)
    return await _persist_failed_dispatch(
        task_history, task_name, periodic_task_name, reason
    )


async def _pre_dispatch_payload_check(
    task_history: TaskHistory,
    task_name: str,
    periodic_task_name: str | None,
    session: AsyncSession | None = None,
) -> TaskHistory | None:
    """Gate dispatch on a resolvable execution request, failing terminally otherwise.

    Two conditions, both becoming a terminal FAILED via
    :func:`_persist_failed_dispatch` rather than an endless Celery retry that
    leaves the history non-terminal. Return ``None`` to proceed with normal
    dispatch.

    The first is a leaf this deployment's ``ENCRYPTION_KEY`` could not decrypt.
    It is checked before the payload read, and not merely as a precaution: the
    stored ciphertext carries no ``file://`` prefix, so the read below would
    succeed and hand the token itself to the executor as the payload. A row
    loaded by id is re-dispatched, so that is reachable rather than theoretical.

    The second is a ``file://`` payload reference that is unresolvable (orphaned
    or missing file) or unreadable (permission, decode, or a file removed
    between the existence check and the read).

    :param task_history: The TaskHistory to dispatch, from any gated path
        (sync, connectivity, chain, queue, or periodic).
    :param task_name: The PMM Extensions task name (used for dedup key and alert source).
    :param periodic_task_name: The periodic-task name, if any (used to enrich
        the alert source).
    :param session: The caller's session, forwarded to
        :func:`_persist_failed_dispatch` so a caller-attached ``task_history`` is
        persisted through its own session rather than a second one.
    :return: The saved FAILED TaskHistory when the request cannot be resolved;
        ``None`` to proceed with normal dispatch.
    """
    if unreadable := task_history.execution_request.unreadable_leaves:
        reason = (
            f"Task execution request could not be decrypted for "
            f"{periodic_task_name or task_name!r}: {', '.join(unreadable)} "
            f"could not be read with the configured ENCRYPTION_KEY"
        )
        logger.error(reason)
        return await _persist_failed_dispatch(
            task_history, task_name, periodic_task_name, reason, session
        )
    try:
        _ = task_history.execution_request.payload_content
    except (PayloadReferenceError, OSError, UnicodeDecodeError) as exc:
        reason = (
            f"Task payload could not be resolved for "
            f"{periodic_task_name or task_name!r}: {exc}"
        )
        logger.exception(reason)
        return await _persist_failed_dispatch(
            task_history, task_name, periodic_task_name, reason, session
        )
    return None


@celery.task
def sync_running_tasks() -> None:
    """Define Celery task to sync running tasks."""
    celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        sync_running_items()
    )


@celery.task
def purge_task_history_logs() -> None:
    """Define Celery task to purge aged task-execution logs."""
    celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        _purge_task_history_logs()
    )


async def _purge_task_history_logs() -> None:
    """Delete aged, non-active ``taskhistory_log`` rows in bounded batches.

    Read the (runtime-overridable) retention window and batch size from
    :data:`tasks_settings`, then loop committed batches via
    :meth:`TaskHistoryLogManager.delete_aged_batch` until a short batch signals
    that no eligible rows remain. The parent ``taskhistory`` audit rows are
    never touched. Log the start time, retention window, total rows deleted,
    and end time; on any failure, raise a system alert and re-raise so Celery
    records the run as failed.
    """
    retention_days = tasks_settings.LOG_RETENTION_DAYS
    batch_size = tasks_settings.LOG_PURGE_BATCH_SIZE
    started_at = utc_now()
    cutoff = started_at - timedelta(days=retention_days)
    logger.info(
        "Starting task-history-log purge: retention=%s days, cutoff=%s, batch_size=%s",
        retention_days,
        cutoff.isoformat(),
        batch_size,
    )
    total_deleted = 0
    try:
        async_session = get_async_session_maker()
        async with async_session() as session:
            while True:
                deleted = await TaskHistoryLogManager.delete_aged_batch(
                    session, cutoff=cutoff, batch_size=batch_size
                )
                total_deleted += deleted
                if deleted < batch_size:
                    break
    except Exception as exc:
        logger.exception(
            "Task-history-log purge failed after deleting %s rows", total_deleted
        )
        await alert_service.trigger(
            {
                "summary": f"Task-history-log purge failed: {exc}",
                "source": "purge_task_history_logs",
                "severity": AlertSeverity.ERROR,
                "class": "log_purge_failure",
                "dedup_key": "purge_task_history_logs",
            }
        )
        raise
    logger.info(
        "Completed task-history-log purge: deleted %s rows in %s",
        total_deleted,
        utc_now() - started_at,
    )


@celery.task
def sync_task_history(task_history_id: int) -> None:
    """Define Celery task to sync a task history item.

    :param task_history_id: The unique identifier of the task history item to sync.
    :type task_history_id: int
    """
    logger.info("Syncing task history %s", task_history_id)
    celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        sync_queue_item(task_history_id)
    )
    logger.info("Finished syncing task history %s", task_history_id)


async def delete_task_history(queue_id: int) -> None:
    """Delete a TaskHistory object by queue ID.

    :param queue_id: The unique identifier of the queue item to delete.
    :type queue_id: int
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        await TaskHistoryManager.delete_where(session, id=queue_id)


async def get_task_history(queue_id: int) -> TaskHistory:
    """Get TaskHistory object by queue ID.

    :param queue_id: The unique identifier of the queue item to retrieve.
    :type queue_id: int
    :return: The TaskHistory object.
    :rtype: TaskHistory
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        return await TaskHistoryManager.get_or_404(
            session,
            select_related=[TaskHistory.task],
            query_options=[undefer(TaskHistory.execution_request)],
            id=queue_id,
        )


async def prepare_periodic_task_history(
    task_name: str, execution_data: PeriodicTaskExecuteRequest | None = None
) -> TaskHistory:
    """Prepare and record the history of a periodic task execution request.

    :param task_name: The name of the task to execute.
    :type task_name: str
    :param execution_data: Execution details and parameters, if any.
    :type execution_data: PeriodicTaskExecuteRequest | None
    :return: The logged TaskHistory entry.
    :rtype: TaskHistory
    """
    execution_data = (
        PeriodicTaskExecuteRequest.model_validate(execution_data)
        if execution_data
        else None
    )
    async_session = get_async_session_maker()
    async with async_session() as session:
        task = await get_executable_task_by_name(session, task_name)
        return await prepare_task_history(
            task,
            executed_by=SYSTEM_USER,
            session=session,
            execution_data=execution_data,
        )


async def dispatch_queue_item(
    queue_item: TaskHistory,
    session: AsyncSession | None = None,
    *,
    await_annotations: bool = False,
    periodic_task_name: str | None = None,
) -> TaskHistory:
    """Process an item from the history table.

    Gate every caller on a resolvable execution request via
    :func:`_pre_dispatch_payload_check` before touching the dispatch lock, so an
    unresolvable ``file://`` payload, or a leaf this deployment's
    ``ENCRYPTION_KEY`` cannot decrypt, short-circuits to a terminal FAILED
    TaskHistory instead of surfacing as an unhandled error on the callers that
    do not run the gate themselves (the sync, connectivity, and chain paths).

    :param queue_item: The TaskHistory object to dispatch.
    :param session: Optional SQLAlchemy asynchronous session to use for the operation.
    :param await_annotations: When True, await the STARTED PMM annotation inline
        instead of scheduling it as a fire-and-forget background task. Required
        from Celery contexts that drive the event loop via discrete
        ``celery.loop.run_until_complete(...)`` calls; the FastAPI default
        (``False``) keeps the request path non-blocking.
    :param periodic_task_name: The periodic-task name, if any, forwarded to the
        payload gate so a periodic dispatch failure enriches the failure reason
        and alert source consistently with :func:`_pre_dispatch_health_check`.
    :return: The TaskHistory object post execution, or the FAILED TaskHistory
        persisted by the pre-dispatch gate when the request cannot be resolved.
    :raises HTTPConflictException: If the queue item status is not PENDING,
        raises a 409 Conflict error.
    :raises HTTPBadRequestException: If the task backend is unsupported,
        raises a 400 Bad Request error.
    """
    failed = await _pre_dispatch_payload_check(
        queue_item, queue_item.execution_request.task, periodic_task_name, session
    )
    if failed is not None:
        return failed
    if session is None:
        async_session = get_async_session_maker()
        async with async_session() as async_session:
            return await _dispatch_queue_item(
                queue_item, async_session, await_annotations=await_annotations
            )
    return await _dispatch_queue_item(
        queue_item, session, await_annotations=await_annotations
    )


async def _dispatch_queue_item(
    queue_item: TaskHistory,
    session: AsyncSession,
    *,
    await_annotations: bool = False,
) -> TaskHistory:
    if queue_item.status != TaskHistoryStatusEnum.PENDING:
        raise HTTPConflictException("Queue item is not in a pending state.")

    dispatch_lock_name = sha256(
        json.dumps(
            {
                "task_id": queue_item.task_id,
                "task": queue_item.execution_request.task,
                "target": queue_item.execution_request.target,
                "payload": queue_item.execution_request.payload,
                "meta": queue_item.execution_request.meta,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    lock_session_maker = get_async_session_maker()
    async with lock_session_maker() as lock_session:
        await DispatchLockManager.delete_where(
            lock_session,
            col(DispatchLock.created_at)
            < (utc_now() - timedelta(seconds=_DISPATCH_LOCK_STALE_SECONDS)),
            name=dispatch_lock_name,
        )
        try:
            # ``claim`` rather than ``create``: the latter routes to
            # ``CRUDBase.save``, whose post-commit ``session.refresh`` is a second
            # checkout taken once the row is already committed and before the
            # ``try`` below has been entered, so pool exhaustion there orphans the
            # lock for the whole sweep interval. See ``DispatchLockManager.claim``.
            await DispatchLockManager.claim(lock_session, dispatch_lock_name)
        except IntegrityError as exc:
            raise HTTPConflictException("Identical dispatch in progress.") from exc

    try:
        await _raise_if_identical_task_conflict(queue_item, session)
        task = await TaskManager.get_root_task(session, queue_item.task)
        executor = get_executor_for_task(task)
        result = await executor.dispatch_task(session, queue_item, task)
    except Exception:
        logger.exception("Failed to dispatch queue item")
        raise
    else:
        await session.refresh(result, attribute_names=["execution_request"])
        if await_annotations:
            await await_annotation(result, "STARTED")
        else:
            schedule_annotation(result, "STARTED")
    finally:
        # Releasing the lock must not decide the dispatch's outcome. This runs
        # after the work is done, and it needs its own checkout from a pool the
        # dispatch has just been contending for, so it is exactly where a
        # POOL_TIMEOUT lands. Raising here would replace an already-successful
        # dispatch with an error the client reads as a refusal, and the retry
        # that follows is not idempotent: the task is enqueued, so the retry
        # collides with the item this attempt created. Logged and swallowed
        # instead - the 30-second sweep at the top of this function is the
        # backstop for a lock that outlives its dispatch, and it is the same
        # backstop an orphaned row already relied on.
        try:
            async with lock_session_maker() as release_session:
                await DispatchLockManager.release(release_session, dispatch_lock_name)
        except Exception:
            logger.exception(
                "Could not release dispatch lock %s; it will be swept once it is "
                "older than %ss",
                dispatch_lock_name,
                _DISPATCH_LOCK_STALE_SECONDS,
            )

    if result.status.is_terminal():
        await maybe_record_run(result.id, executor)
    return result


def _postgresql_meta_clauses(queued: TaskExecutionRequest) -> list[ColumnElement[bool]]:
    """Return the ``jsonb`` predicates narrowing candidates by ``queued``'s meta.

    A key in :data:`ENCRYPTED_META_KEYS` carries no predicate: it is ciphertext
    at rest and encryption is non-deterministic, so no SQL comparison can match
    it. Every other key keeps its containment or per-key equality, which is what
    lets the GIN index on ``execution_request->'meta'`` still serve the query.

    :param queued: The execution request being dispatched.
    :return: One predicate per comparable meta key, empty when there is none.
    """
    scalar_subset: dict[str, Any] = {}
    container_items: list[tuple[str, Any]] = []
    for field, raw_value in (queued.meta or {}).items():
        if field in ENCRYPTED_META_KEYS:
            continue
        if isinstance(raw_value, list | dict):
            container_items.append((field, raw_value))
        else:
            scalar_subset[field] = raw_value
    meta_jsonb = col(TaskHistory.execution_request).op("->")(
        literal("meta", Text, literal_execute=True)
    )
    clauses: list[ColumnElement[bool]] = []
    if scalar_subset:
        clauses.append(
            meta_jsonb.op("@>")(cast(literal(json.dumps(scalar_subset), Text), JSONB))
        )
    clauses.extend(
        meta_jsonb.op("->")(literal(field, Text, literal_execute=True))
        == cast(literal(json.dumps(raw_value), Text), JSONB)
        for field, raw_value in container_items
    )
    return clauses


def _json_extract_meta_clauses(
    engine_name: str, queued: TaskExecutionRequest
) -> list[ColumnElement[bool]]:
    """Return the per-key text-equality predicates for a non-PostgreSQL engine.

    An encrypted key is skipped for the same reason as in the ``jsonb`` branch.

    :param engine_name: The bound engine's dialect name.
    :param queued: The execution request being dispatched.
    :return: One predicate per comparable meta key, empty when there is none.
    """
    clauses: list[ColumnElement[bool]] = []
    for field, raw_value in (queued.meta or {}).items():
        if field in ENCRYPTED_META_KEYS:
            continue
        extracted = func_json_extract(
            engine_name, TaskHistory.execution_request, "meta", field
        )
        if isinstance(raw_value, list | dict):
            comparable = json.dumps(raw_value, separators=(",", ":"))
            extracted = cast(extracted, Text)
        else:
            comparable = prepare_unsafe_value_for_json_comparison(
                engine_name, raw_value
            )
        clauses.append(extracted == comparable)
    return clauses


def _encrypted_leaves_match(
    candidate: TaskExecutionRequest, queued: TaskExecutionRequest
) -> bool:
    """Return whether two execution requests agree on their encrypted leaves.

    ``payload`` and every key in :data:`ENCRYPTED_META_KEYS` are stored as
    ciphertext, and encryption derives a fresh IV per call, so two encryptions of
    one value never compare equal in SQL. Both sides here are plaintext, which
    makes this the comparison the SQL predicates used to make, but only because
    the caller has already excluded a candidate carrying unreadable leaves, whose
    values stay ciphertext and would compare unequal to anything.

    :param candidate: The execution request of a row the query narrowed to, with
        every protected leaf readable.
    :param queued: The execution request being dispatched.
    :return: Whether every encrypted leaf holds an equal value on both sides.
    """
    if candidate.payload != queued.payload:
        return False
    candidate_meta = candidate.meta or {}
    queued_meta = queued.meta or {}
    return all(
        candidate_meta.get(key) == queued_meta.get(key) for key in ENCRYPTED_META_KEYS
    )


async def _raise_if_identical_task_conflict(
    queue_item: TaskHistory, session: AsyncSession
) -> None:
    """Refuse a dispatch duplicating one already in flight for the same task.

    Matching runs in two stages because several leaves of the execution request
    are stored encrypted and encryption is non-deterministic: SQL narrows on
    every plaintext leaf (task name, target, active status, ``task_id``, and each
    ``meta`` key the column still holds in the clear), then ``payload`` and every
    encrypted ``meta`` key are compared in Python against the decrypted
    candidates. The candidate set is bounded by how many runs of one task are in
    flight at once, and the deferred ``execution_request`` is undeferred so
    reading it triggers no per-row load.

    The two stages differ in kind, and the encrypted keys changed sides: SQL
    matches a ``meta`` key by containment, which ignores a key the incoming
    request does not carry, while the Python stage compares those keys by
    equality on both sides. A request carrying no ``args`` therefore no longer
    matches an in-flight one that carries some. That is correct, since the two
    are not the same request, but it is a narrowing rather than a straight
    translation.

    A candidate whose stored document does not validate comes back as the raw
    value rather than a request, and is skipped: it cannot be compared, and a
    document that will not parse is not a duplicate of one that did. Only leaves
    the SQL never reads can produce this, since a row malformed in ``task``,
    ``target`` or a compared ``meta`` key fails the narrowing first.

    A candidate whose protected leaves this key cannot read is refused instead of
    skipped. Its values stay ciphertext, so they compare unequal to every
    plaintext and the row would silently stop deduplicating, which for a
    schema-changing task means two concurrent runs against one table. The queued
    side is always readable here, because dispatch is gated on that before this
    runs, so the mismatch is one-sided and cannot be resolved by comparing.

    :param queue_item: The history row about to be dispatched.
    :param session: The session to query candidates through.
    :raises HTTPConflictException: If an active row for the same task carries an
        identical execution request, or carries one that cannot be read and so
        cannot be ruled out as a duplicate.
    """
    engine_name = session.get_bind().name
    queued = queue_item.execution_request
    meta_where_clauses = (
        _postgresql_meta_clauses(queued)
        if engine_name.startswith(DatabaseDialect.POSTGRESQL)
        else _json_extract_meta_clauses(engine_name, queued)
    )
    candidates = await TaskHistoryManager.list(
        session,
        func_json_extract(engine_name, TaskHistory.execution_request, "task")
        == queued.task,
        func_json_extract(engine_name, TaskHistory.execution_request, "target")
        == queued.target,
        *meta_where_clauses,
        col(TaskHistory.status).in_(TaskHistoryStatusEnum.active_statuses()),
        col(TaskHistory.id) != queue_item.id,
        query_options=[
            undefer(
                typing.cast("QueryableAttribute[Any]", TaskHistory.execution_request)
            )
        ],
        task_id=queue_item.task_id,
    )
    for identical_task in candidates:
        candidate = identical_task.execution_request
        if not isinstance(candidate, TaskExecutionRequest):
            continue
        if candidate.unreadable_leaves:
            raise HTTPConflictException(
                f"In-flight queue item ({identical_task.id}) cannot be compared: "
                "its stored execution request could not be read with the "
                "configured ENCRYPTION_KEY."
            )
        if _encrypted_leaves_match(candidate, queued):
            raise HTTPConflictException(
                f"Identical queue item already running ({identical_task.id})."
            )


async def sync_running_items() -> None:
    """Sync running tasks in the task history.

    This function updates the ``sync_in_progress_started_at`` field for tasks that are
    either not currently in progress or have been in progress for longer than the
    configured SYNC_LOCK_TTL. It then dispatches the sync task for those tasks.
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        result = await TaskHistoryManager.update_where(
            session,
            {"sync_in_progress_started_at": func.now()},
            or_(
                col(TaskHistory.sync_in_progress_started_at).is_(None),
                col(TaskHistory.sync_in_progress_started_at)
                < (utc_now() - tasks_settings.SYNC_LOCK_TTL),
            ),
            returning=("id",),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        args = [(item_id,) for item_id in result]
        if args:
            logger.debug("Dispatching sync of %d running tasks", len(args))
            chunk_size = 100
            sync_task_history.chunks(args, chunk_size).apply_async()


async def sync_queue_item(queue_id: int) -> TaskHistory:
    """Sync a task history item.

    :param queue_id: The unique identifier of the queue item to sync.
    :type queue_id: int
    :return: The TaskHistory object post sync.
    :rtype: TaskHistory
    :raises HTTPBadRequestException: If the task backend is unsupported,
        raises a 400 Bad Request error.
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        queue_item = await TaskHistoryManager.get_or_404(
            session,
            select_related=[TaskHistory.task],
            query_options=[undefer(TaskHistory.execution_request)],
            id=queue_id,
        )
        task = await TaskManager.get_root_task(session, queue_item.task)
    was_running = queue_item.is_running
    if not was_running:
        async with async_session() as session:
            result = await TaskHistoryManager.update_where(
                session,
                {"sync_in_progress_started_at": None},
                TaskHistory.status != TaskHistoryStatusEnum.RUNNING,
                id=queue_id,
            )
            if result.rowcount == 0:
                queue_item = await TaskHistoryManager.get_or_404(
                    session,
                    select_related=[TaskHistory.task],
                    query_options=[undefer(TaskHistory.execution_request)],
                    id=queue_id,
                )
                task = await TaskManager.get_root_task(session, queue_item.task)
                was_running = True
            else:
                return queue_item
    executor = get_executor_for_task(task)
    async with async_session() as writer_session:
        queue_item = await executor.sync_task_history(
            queue_item, writer_session=writer_session, await_annotations=True
        )
    queue_item.sync_in_progress_started_at = None
    async with async_session() as session:
        saved = await TaskHistoryManager.save(
            session,
            queue_item,
            flag_modified_fields=[
                "execution_request",
                "status",
                "started_at",
                "finished_at",
                "failure_reason",
                "sync_in_progress_started_at",
            ],
        )
        await session.refresh(saved, attribute_names=["execution_request"])
    await maybe_dispatch_chain(saved, was_running=was_running, await_annotations=True)
    if saved.status.is_terminal():
        await maybe_record_run(saved.id, executor)
    return saved


async def maybe_dispatch_chain(
    saved: TaskHistory,
    *,
    was_running: bool,
    await_annotations: bool = False,
) -> None:
    """Dispatch the next chained task when ``saved`` is in a chain-eligible state.

    Eligibility requires ``was_running`` (the parent was RUNNING when this sync
    started, before the executor call) AND a chain-eligible terminal status:
    SUCCESS, or any finished/LOST status when ``_chain_on_failure`` is set on
    the parent's ``execution_request.meta``. The helper short-circuits when no
    ``_chain_task_names`` pointer is set.

    :param saved: The post-save TaskHistory to inspect.
    :param was_running: Whether the parent was RUNNING when this sync started.
    :param await_annotations: Forwarded to the chained ``dispatch_queue_item``
        call. Celery contexts (``sync_queue_item``) pass ``True`` so the chained
        STARTED annotation reaches PMM before the loop stops; the FastAPI sync
        route keeps the default ``False`` to avoid blocking the response on PMM
        availability.
    """
    if not was_running:
        return
    meta = saved.execution_request.meta or {}
    chain_on_failure = meta.get("_chain_on_failure", False)
    is_terminal = saved.status.is_terminal()
    should_chain = saved.status == TaskHistoryStatusEnum.SUCCESS or (
        chain_on_failure and is_terminal
    )
    chain_task_names = meta.get("_chain_task_names")
    if should_chain and chain_task_names:
        await _dispatch_chained_task(
            chain_task_names[0],
            saved,
            chain_task_names[1:],
            await_annotations=await_annotations,
        )


async def _dispatch_chained_task(
    chain_task_name: str,
    parent: TaskHistory,
    remaining_chain: list[str] | None = None,
    *,
    await_annotations: bool = False,
) -> None:
    """Dispatch the next chained task after the parent completes successfully.

    Load the task by name, build a new TaskHistory inheriting the parent's executor
    target, and call ``dispatch_queue_item``. Any ``chain_task_names`` in the chained
    task's static ``data["meta"]`` is stripped to prevent unintended multi-level
    chaining; the ``remaining_chain`` is set as the next chain steps.

    :param chain_task_name: The name of the next task to dispatch.
    :type chain_task_name: str
    :param parent: The completed parent TaskHistory.
    :type parent: TaskHistory
    :param remaining_chain: Task names to chain after this one, if any.
    :type remaining_chain: list[str] | None
    :param await_annotations: Forwarded to ``dispatch_queue_item``. See
        :func:`maybe_dispatch_chain` for the FastAPI vs Celery rationale.
    :type await_annotations: bool
    """
    if parent.execution_request.meta.get("_chain_depth", 0) >= _MAX_CHAIN_DEPTH:
        logger.warning(
            "Chain depth limit (%d) reached for task %r; skipping chain to %r",
            _MAX_CHAIN_DEPTH,
            parent.execution_request.task,
            chain_task_name,
        )
        return
    try:
        async_session = get_async_session_maker()
        async with async_session() as session:
            chain_task = await TaskManager.first(
                session, col(Task.deleted_at).is_(None), name=chain_task_name
            )
        if chain_task is None:
            logger.warning(
                "Chained task %r not found; skipping chain dispatch", chain_task_name
            )
            return
        if chain_task_name == parent.execution_request.task:
            logger.warning(
                "Chained task %r is the same as the parent task; skipping self-chain",
                chain_task_name,
            )
            return
        chain_meta = dict(chain_task.data.get("meta", {}))
        chain_meta.pop("_chain_task_names", None)
        if remaining_chain:
            chain_meta["_chain_task_names"] = remaining_chain
        chain_meta["target"] = parent.execution_request.target
        chain_meta["_chain_on_failure"] = parent.execution_request.meta.get(
            "_chain_on_failure", False
        )
        chain_meta["_chain_depth"] = (
            parent.execution_request.meta.get("_chain_depth", 0) + 1
        )
        chain_history = TaskHistory(
            task_id=chain_task.id,
            task=chain_task,
            execution_request=TaskExecutionRequest(
                task=chain_task.name,
                target=parent.execution_request.target,
                meta=chain_meta,
                payload=chain_task.data.get("payload"),
                tracking={"evaluation_id": ""},
            ),
            status=TaskHistoryStatusEnum.PENDING,
            executed_by=parent.executed_by,
            anonymize_mask=parent.anonymize_mask,
        )
        await dispatch_queue_item(chain_history, await_annotations=await_annotations)
    except Exception:
        logger.exception(
            "Failed to dispatch chained task %r from parent %r",
            chain_task_name,
            parent.execution_request.task,
        )


def get_executor_for_task(task: Task) -> BaseExecutor:
    """Get the executor for a specific task.

    :param task: The task for which to get the executor.
    :type task: Task
    :return: The executor for the task.
    :rtype: BaseExecutor
    """
    try:
        return get_executor(task.backend)
    except ValueError:
        raise HTTPBadRequestException(
            f"Unsupported task backend: {task.backend}"
        ) from None


@celery.task
def check_nomad_cert_expiry() -> None:
    """Run the Nomad TLS certificate expiry check (CA and client PEM on disk).

    Reads :data:`TASKS.NOMAD.SSL_CAFILE` and :data:`TASKS.NOMAD.SSL_CERTFILE`,
    compares each certificate's ``not_valid_after_utc`` to
    :data:`TASKS.NOMAD.CERT_EXPIRY_WARN_DAYS`, and triggers or resolves alerts
    through :class:`app.core.alerts.models.AlertService`.

    When ``ALERTING.PROVIDERS`` is empty (typical in local development),
    :meth:`~app.core.alerts.models.AlertService.trigger` and ``resolve`` log
    a warning and return without sending anything; this task also writes the
    same certificate summary to this module's logger at warning or error level
    so operators still see the issue in logs.

    The deduplication key for PagerDuty is ``nomad-cert-expiry:<basename>`` so
    incidents are stable across restarts. Renaming a cert file on disk may
    leave a stale open incident under the old basename.

    Celery beat registration uses ``TASKS.NOMAD.CHECK_CERT_EXPIRY_INTERVAL``; when
    it is ``None`` the periodic task is not seeded (see :mod:`app.tasks.db.seed`).
    """
    celery.loop.run_until_complete(  # ty: ignore[unresolved-attribute]
        _check_nomad_cert_expiry()
    )


async def _check_nomad_cert_expiry() -> None:
    """Evaluate Nomad CA and client PEM files and fire or clear expiry alerts."""
    from app.core.alerts.config import alert_service, alert_settings
    from app.core.alerts.models import AlertSeverity
    from app.core.utils import utc_now

    nomad = normalize_nomad_config_value(tasks_settings.NOMAD)
    warn_days = nomad.cert_expiry_warn_days
    now = utc_now()
    for label, raw_path in (
        ("CA", nomad.ssl_cafile),
        ("client", nomad.ssl_certfile),
    ):
        if raw_path is None:
            continue
        path = Path(raw_path)

        dedup_key = f"nomad-cert-expiry:{path.name}"
        try:
            pem = path.read_bytes()
            cert = x509.load_pem_x509_certificate(pem)
        except OSError as exc:
            logger.warning(
                "Could not read Nomad %s certificate at %s: %s",
                label,
                path,
                exc,
            )
            continue
        except ValueError:
            logger.warning(
                "Could not parse Nomad %s certificate PEM at %s", label, path
            )
            continue

        not_after = cert.not_valid_after_utc
        days_left = (not_after - now).days
        if days_left > warn_days:
            await alert_service.resolve(dedup_key)
            continue
        if days_left <= 0:
            severity = AlertSeverity.CRITICAL
            summary = (
                f"Nomad {label} certificate {path.name!r} has expired or expires "
                f"today (not_valid_after_utc={not_after.isoformat()}). "
                "Renew the certificate and restart PMM Extensions so task dispatch continues."
            )
        else:
            severity = AlertSeverity.WARNING
            summary = (
                f"Nomad {label} certificate {path.name!r} expires in {days_left} day(s) "
                f"on {not_after.date().isoformat()} (warning window: {warn_days} day(s))."
            )
        if not alert_settings.PROVIDERS:
            if severity is AlertSeverity.CRITICAL:
                logger.error(
                    "ALERTING.PROVIDERS is empty; Nomad TLS cert alert would fire "
                    "(dedup_key=%s): %s",
                    dedup_key,
                    summary,
                )
            else:
                logger.warning(
                    "ALERTING.PROVIDERS is empty; Nomad TLS cert alert would fire "
                    "(dedup_key=%s): %s",
                    dedup_key,
                    summary,
                )
        await alert_service.trigger(
            {
                "summary": summary,
                "source": f"nomad_cert_expiry:{label}",
                "severity": severity,
                "class": "nomad_cert_expiry",
                "dedup_key": dedup_key,
            }
        )
