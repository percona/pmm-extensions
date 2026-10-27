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

"""PMM annotation helpers for task lifecycle events."""

import asyncio
import copy
import logging
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm.attributes import NO_VALUE

from app.core.config import settings
from app.core.requests.remote_api import RemoteAPI
from app.tasks.models import CELERY_LOCAL_TARGET, TaskExecutionRequest, TaskHistory

_EXECUTION_REQUEST_ATTR = "execution_request"

logger = logging.getLogger(__name__)

_background_tasks = set()


def execution_request_for_pmm_snapshot(queue_item: TaskHistory) -> TaskExecutionRequest:
    """Return the task execution request used to build a PMM annotation.

    The deferred ``TaskHistory.execution_request`` can raise
    ``MissingGreenlet`` on async engines if read through the attribute after the load
    session has closed. When the column is already in memory, use the ORM
    :attr:`loaded_value`; otherwise read ``queue_item.execution_request``.

    :param queue_item: The task history record.
    :type queue_item: TaskHistory
    :return: The request whose ``task``, ``target``, and ``meta`` are snapshotted.
    :rtype: TaskExecutionRequest
    """
    state = sa_inspect(queue_item).attrs.execution_request
    if state.loaded_value is not NO_VALUE:
        return state.loaded_value
    return queue_item.execution_request


async def create_pmm_annotation(
    text: str,
    node_name: str,
    tags: list[str] | None = None,
    service_names: list[str] | None = None,
) -> None:
    """Create a PMM annotation via POST ``/v1/management/annotations``.

    Best-effort: errors are logged but never raised. Callers should
    schedule this via ``asyncio.create_task()`` for non-blocking behavior;
    awaiting directly blocks for up to ``settings.PMM.annotations_timeout`` seconds.

    :param text: Annotation text (e.g. ``"PMM Extensions: backup_data - STARTED"``).
    :param node_name: PMM node name from execution target.
    :param tags: Optional tags for the annotation.
    :param service_names: PMM service names from task metadata.
    """
    if not settings.PMM.annotations_enabled:
        return
    if not settings.PMM.endpoint or not settings.PMM.api_key:
        logger.debug("PMM annotation skipped: endpoint or api_key not configured")
        return

    try:
        api = await settings.get_remote_api(
            RemoteAPI,
            endpoint=settings.PMM.endpoint,
            verify_ssl=settings.PMM.verify_ssl,
            error_detail_key="message",
            error_code_key="code",
        )
        with api.auth(settings.PMM.api_key.get_secret_value()):
            await asyncio.wait_for(
                api.post(
                    "/v1/management/annotations",
                    json={
                        "text": text,
                        "tags": tags or [],
                        "node_name": node_name,
                        "service_names": service_names or [],
                    },
                ),
                timeout=settings.PMM.annotations_timeout,
            )
        logger.info("PMM annotation created: %s (node=%s)", text, node_name)
    except Exception:
        logger.exception("Failed to create PMM annotation: %s", text)


async def annotate_task_event(
    *,
    task_name: str,
    target: str,
    meta: dict[str, Any] | None,
    event: str,
) -> None:
    """Create a PMM annotation for a task lifecycle event.

    Read ``_service_names`` (list) or ``_service_name`` (str) from the
    task execution metadata. Read ``_pmm_node_name`` to override ``target``
    as the PMM node name when the executor differs from the source node —
    e.g. the archiver runs on a separate executor host. Accept primitives
    rather than an ORM instance so the coroutine is safe to run after the
    originating request session has closed.

    Skip a task whose ``target`` is :data:`~app.tasks.models.CELERY_LOCAL_TARGET`
    and that sets no ``_pmm_node_name`` override: it ran on PMM Extensions' own
    worker (the scheduled inventory sync and collection), which is no PMM node,
    so PMM would refuse the annotation. An explicit override always annotates,
    even one naming a node called ``local``.

    :param task_name: The task name (from ``TaskExecutionRequest.task``).
    :param target: The execution target node (from ``TaskExecutionRequest.target``).
    :param meta: The task execution metadata (from ``TaskExecutionRequest.meta``).
    :param event: The event label (e.g. ``"STARTED"``, ``"COMPLETED"``, ``"FAILED"``).
    """
    safe_meta = meta or {}
    service_names = safe_meta.get("_service_names")
    if service_names is None:
        single = safe_meta.get("_service_name")
        service_names = [single] if single else []

    pmm_node_name = safe_meta.get("_pmm_node_name")
    if not pmm_node_name and target == CELERY_LOCAL_TARGET:
        logger.debug(
            "PMM annotation skipped for %s: it ran on the local worker", task_name
        )
        return
    node_name = pmm_node_name or target

    await create_pmm_annotation(
        text=f"PMM Extensions: {task_name} - {event}",
        node_name=node_name,
        tags=["pmm-extensions", task_name, event.lower()],
        service_names=service_names,
    )


def _snapshot_for_annotation(queue_item: TaskHistory, event: str) -> dict[str, Any]:
    """Validate the precondition and snapshot kwargs for ``annotate_task_event``.

    Enforce the deferred-load and snapshot invariants exactly once: the deferred
    ``execution_request`` column must be loaded, the snapshot is taken from
    :attr:`loaded_value` when available (per :func:`execution_request_for_pmm_snapshot`),
    and ``meta`` is deep-copied so background callers observe the values at
    scheduling time even if the originating request mutates nested structures
    inside the attribute afterwards.

    :param queue_item: The task history record.
    :type queue_item: TaskHistory
    :param event: The event label (e.g. ``"STARTED"``, ``"COMPLETED"``).
    :type event: str
    :return: Keyword arguments for :func:`annotate_task_event`.
    :rtype: dict[str, Any]
    :raises RuntimeError: If ``execution_request`` is not loaded on
        ``queue_item``.
    """
    if _EXECUTION_REQUEST_ATTR in sa_inspect(queue_item).unloaded:
        raise RuntimeError(
            "PMM annotation helpers require queue_item.execution_request "
            "to be loaded. Call "
            '`await session.refresh(obj, attribute_names=["execution_request"])`'
            " before schedule_annotation/await_annotation(obj, ...). The column "
            "is deferred=True and a sync access triggers MissingGreenlet on "
            "asyncpg/aiosqlite (or DetachedInstanceError once the session "
            "has closed)."
        )
    execution_request = execution_request_for_pmm_snapshot(queue_item)
    return {
        "task_name": execution_request.task,
        "target": execution_request.target,
        "meta": copy.deepcopy(execution_request.meta),
        "event": event,
    }


def schedule_annotation(
    queue_item: TaskHistory,
    event: str,
) -> None:
    """Schedule a fire-and-forget PMM annotation as a background task.

    Snapshot the request fields synchronously so the background
    coroutine never touches ORM attributes after the originating
    session has closed.

    Precondition: ``queue_item.execution_request`` must already be
    loaded. The column is declared ``deferred=True`` as a ``column_property``
    (``app/tasks/models.py``); touching it here from sync code triggers a
    lazy SELECT that raises :class:`sqlalchemy.exc.MissingGreenlet` on
    async drivers (asyncpg, aiosqlite) when the instance is still
    attached, and :class:`sqlalchemy.orm.exc.DetachedInstanceError` when
    the session has closed. Callers that hand over a freshly-saved
    instance — ``TaskHistoryManager.save`` re-defers the column via its
    internal ``session.refresh(instance)`` — must run
    ``await session.refresh(obj, attribute_names=["execution_request"])``
    first (see ``app/tasks/routes.py`` and
    ``app/tasks/connectivity/service.py`` for the canonical pattern).

    ``meta`` is deep-copied so the background task observes the values
    at scheduling time even if the originating request mutates nested
    structures inside the attribute afterwards.

    Use this helper from FastAPI request handlers, where the event loop
    keeps running after the response is sent. From Celery contexts that
    drive the loop via discrete ``celery.loop.run_until_complete(...)``
    calls, prefer :func:`await_annotation` — fire-and-forget tasks
    scheduled here are abandoned when the outer coroutine returns and
    never reach PMM.

    :param queue_item: The task history record.
    :type queue_item: TaskHistory
    :param event: The event label (e.g. ``"STARTED"``, ``"COMPLETED"``).
    :type event: str
    :raises RuntimeError: If ``execution_request`` is not loaded on
        ``queue_item``.
    """
    kwargs = _snapshot_for_annotation(queue_item, event)
    task = asyncio.create_task(annotate_task_event(**kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def await_annotation(queue_item: TaskHistory, event: str) -> None:
    """Await a PMM annotation directly.

    Same precondition and snapshot semantics as :func:`schedule_annotation`,
    but ``annotate_task_event`` is awaited inline so the call completes
    before the outer coroutine returns. Use this from Celery worker tasks
    that drive the event loop with ``celery.loop.run_until_complete(...)``:
    a fire-and-forget background task scheduled there is abandoned when the
    outer coroutine returns and never reaches PMM.

    ``annotate_task_event`` is internally bounded by
    ``settings.PMM.annotations_timeout``; errors are swallowed by
    :func:`create_pmm_annotation`'s ``except`` block, so this helper never
    raises on PMM failures.

    :param queue_item: The task history record.
    :type queue_item: TaskHistory
    :param event: The event label (e.g. ``"STARTED"``, ``"COMPLETED"``).
    :type event: str
    :raises RuntimeError: If ``execution_request`` is not loaded on
        ``queue_item``.
    """
    kwargs = _snapshot_for_annotation(queue_item, event)
    await annotate_task_event(**kwargs)
