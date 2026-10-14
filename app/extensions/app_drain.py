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

"""Cooperative-cancellation drain machinery for PMM Extensions plugin tasks.

When an app is disabled it enters ``DISABLING`` (one of the four lifecycle
states). This module drains the app's already-running Celery work
cooperatively rather than killing it: migrated tasks observe :func:`should_cancel`
at safe points and exit early (preserving committed partial progress); a per-app
:class:`app.extensions.models.AppRunningTask` counter — maintained by ``task_prerun`` /
``task_postrun`` receivers — drives the terminal ``DISABLING`` -> ``DISABLED``
transition once the count reaches zero; and a periodic reconciler prunes rows
orphaned by a worker crash or a force-disable ``revoke(terminate=True)`` (where
``task_postrun`` never runs).

This module is named directly in ``STATIC_CELERY_INCLUDE`` so its Celery task and
signal receivers register at worker startup even in an image that ships no app
with a ``celery.py``. Registration therefore no longer rides on a transitive
import from the app-owned Celery modules (:mod:`app.extensions.apps.alerts.celery`,
:mod:`app.extensions.apps.report.celery`), which would leave the unconditionally-seeded
``extensions__reconcile_disabling_apps`` beat row pointing at an unregistered task.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from celery.signals import task_postrun, task_prerun
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.celery import celery
from app.core.utils import utc_now
from app.extensions.config import extensions_settings
from app.extensions.crud import AppRunningTaskManager, AppStateManager
from app.extensions.db import get_async_session_maker
from app.extensions.models import AppLifecycleEnum, AppRunningTask

logger = logging.getLogger(__name__)

_OWNER_APP_KEY_ATTR = "owner_app_key"


def owned_by(app_key: str) -> Callable[[Any], Any]:
    """Tag a migrated Celery task with the app key that owns it.

    Apply *above* ``@celery.task`` so it stamps the registered task object::

        @owned_by("alerts")
        @celery.task
        def backup_alert_config() -> None: ...

    The ``task_prerun`` / ``task_postrun`` receivers read the tag off the running
    task to count it toward its app's drain. A task without the tag (every
    ``app.tasks.*`` task and the reconciler itself) is never counted, cancelled,
    or used to drive a lifecycle transition. Carrying the key on the task -- not
    in a name-indexed map -- means renaming or moving the task function can't
    silently desync it from its owner.

    :param app_key: The owning app's key.
    :return: A decorator that stamps the owner on the task and returns it.
    """

    def tag(task: Any) -> Any:
        setattr(task, _OWNER_APP_KEY_ATTR, app_key)
        return task

    return tag


async def should_cancel(app_key: str, session: AsyncSession | None = None) -> bool:
    """Return whether a running task for ``app_key`` should cooperatively exit.

    The single fail-soft entry point for every safe-point check: a transient DB
    error is swallowed and reported as ``False`` so a running task drains
    naturally instead of aborting or retrying, mirroring the fail-open of
    :func:`app.extensions.deps.require_app_enabled`. Session-owning tasks pass their open
    ``session``; tasks without one omit it and a short-lived session is opened for
    the read.

    :param app_key: The owning app's key.
    :param session: An already-open session to reuse, or ``None`` to open one.
    :return: ``True`` when the app is ``DISABLING`` / ``DISABLED`` and reachable.
    """
    try:
        if session is not None:
            return await AppStateManager.should_cancel(session, app_key)
        async with get_async_session_maker()() as own_session:
            return await AppStateManager.should_cancel(own_session, app_key)
    except SQLAlchemyError:
        logger.warning("Could not read app state for %s; not cancelling.", app_key)
        return False


async def finalize_drain_if_complete(session: AsyncSession, app_key: str) -> bool:
    """Transition ``app_key`` ``DISABLING`` -> ``DISABLED`` when fully drained.

    Idempotent and concurrency-safe: the state flip is a conditional
    ``UPDATE ... WHERE lifecycle_state = DISABLING`` so exactly one of several
    concurrent callers (the toggle endpoint, ``task_postrun``, the reconciler)
    commits the transition. A no-op when the app is not ``DISABLING`` or still has
    running-task rows.

    :param session: The PMM Extensions database session.
    :param app_key: The app to finalize.
    :return: ``True`` iff this call performed the transition.
    """
    if (
        await AppStateManager.current_lifecycle(session, app_key)
        != AppLifecycleEnum.DISABLING
    ):
        return False
    if await AppRunningTaskManager.exists(session, app_key=app_key):
        return False
    result = await AppStateManager.update_where(
        session,
        {"lifecycle_state": AppLifecycleEnum.DISABLED},
        app_key=app_key,
        lifecycle_state=AppLifecycleEnum.DISABLING,
    )
    transitioned = bool(result.rowcount)
    if transitioned:
        logger.info("App %s drained; transitioned DISABLING -> DISABLED.", app_key)
    return transitioned


async def _record_start(app_key: str, task_id: str) -> None:
    """Insert the running-task row for a starting migrated task."""
    async with get_async_session_maker()() as session:
        await AppRunningTaskManager.get_or_create(
            session,
            AppRunningTask(app_key=app_key, celery_task_id=task_id),
            filter_include={"celery_task_id"},
        )


async def _record_end(app_key: str, task_id: str) -> None:
    """Delete the running-task row and finalize the drain if it was the last."""
    async with get_async_session_maker()() as session:
        await AppRunningTaskManager.delete_where(session, celery_task_id=task_id)
        await finalize_drain_if_complete(session, app_key)


@asynccontextmanager
async def track_app_task(session: AsyncSession, app_key: str) -> AsyncIterator[None]:
    """Count a non-Celery, in-request unit of work in the drain counter.

    Wraps a direct (non-Celery) call to a drainable coroutine -- e.g. the manual
    snippet-refresh routes awaiting :func:`app.extensions.snippets.celery.update_snippets` -- so
    it shows up in the per-app :class:`app.extensions.models.AppRunningTask` count
    exactly as a Celery task does via the ``task_prerun``/``task_postrun``
    receivers. Without it a concurrent disable would see zero in-flight work and
    finalize the app to ``DISABLED`` while the request is still running. The
    counter row is committed on enter (so a concurrent finalize sees it) and
    deleted on exit, then the drain is finalized. A synthetic ``celery_task_id``
    keeps the row distinct from any real Celery row; a force-disable ``revoke`` of
    that id is a harmless no-op (no such Celery task), and the in-request
    coroutine still self-cancels at its next safe point.

    :param session: The request-scoped PMM Extensions database session.
    :param app_key: The owning app's key.
    """
    task_id = f"manual-{uuid4()}"
    await AppRunningTaskManager.get_or_create(
        session,
        AppRunningTask(app_key=app_key, celery_task_id=task_id),
        filter_include={"celery_task_id"},
    )
    try:
        yield
    finally:
        await AppRunningTaskManager.delete_where(session, celery_task_id=task_id)
        await finalize_drain_if_complete(session, app_key)


@task_prerun.connect
def record_task_start(task_id: str, task: Any, **_: Any) -> None:
    """Count a migrated PMM Extensions app task as it starts (``task_prerun`` receiver)."""
    app_key = getattr(task, _OWNER_APP_KEY_ATTR, None)
    if app_key is None:
        return
    celery.loop.run_until_complete(_record_start(app_key, task_id))


@task_postrun.connect
def record_task_end(task_id: str, task: Any, **_: Any) -> None:
    """Uncount a migrated task and finalize its app (``task_postrun`` receiver)."""
    app_key = getattr(task, _OWNER_APP_KEY_ATTR, None)
    if app_key is None:
        return
    celery.loop.run_until_complete(_record_end(app_key, task_id))


@celery.task
def reconcile_disabling_apps() -> None:
    """Prune orphaned running-task rows and finalize drained apps (safety net)."""
    celery.loop.run_until_complete(_reconcile_disabling_apps())


async def _reconcile_disabling_apps() -> None:
    """Prune stale running-task rows and finalize every drained ``DISABLING`` app.

    Backstops the two event-driven drivers: it cleans up rows orphaned when
    ``task_postrun`` never fired (worker crash, force-disable termination) and
    finalizes idle apps whose disable produced no task events. Each app is
    finalized on its own freshly opened session so one app's failure can't poison
    the rest — on PostgreSQL the first error in a shared transaction aborts every
    later statement in it.
    """
    session_maker = get_async_session_maker()
    cutoff = utc_now() - extensions_settings.APP_DRAIN.stale_task_ttl
    async with session_maker() as session:
        await AppRunningTaskManager.delete_where(
            session, col(AppRunningTask.created_at) < cutoff
        )
        disabling_keys = [
            app_key
            for app_key, state in (
                await AppStateManager.all_lifecycle_states(session)
            ).items()
            if state == AppLifecycleEnum.DISABLING
        ]
    for app_key in disabling_keys:
        try:
            async with session_maker() as session:
                await finalize_drain_if_complete(session, app_key)
        except SQLAlchemyError:
            logger.exception(
                "Reconciler failed to finalize app %s; continuing.", app_key
            )
