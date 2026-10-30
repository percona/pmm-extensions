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

"""Define the Operations Inventory Celery entry point.

``@owned_by("om_inventory")`` tags the task so the app-drain reconciler counts it
toward this app rather than treating it as a core task. The module is included in the
worker's ``include`` list because the app is registered in ``EXTENSIONS.APPS`` and the
registry derives the Celery module path from the package's ``celery.py``.

The other two tasks finish runs whose sweep could not: they are not ``owned_by`` this
app, so they never hold up a drain, and not gated on ``ENABLED``, since a run row
needs finishing with the app switched off too.
"""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from celery import Task as CeleryTask
from celery.signals import worker_init, worker_ready
from celery.utils.time import get_exponential_backoff_interval
from kombu.exceptions import KombuError
from sqlalchemy.exc import ProgrammingError

from app.celery import celery
from app.core.utils.date_time import utc_now
from app.extensions.app_drain import owned_by
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.service import (
    database_unreachable,
    fail_run,
    FailureNotRecordedError,
    recover_interrupted_runs,
    run_probe,
    UNCLAIMED_GRACE,
    WorkerBoot,
)

logger = logging.getLogger(__name__)

#: The longest the two recovery tasks wait between attempts, in seconds.
RETRY_MAX_DELAY = 60


@owned_by("om_inventory")
@celery.task
def run_om_probe(
    execution_id: str | None = None, node_ids: list[str] | None = None
) -> str:
    """Run one probe sweep and return its id.

    Scheduled by this app's ``periodic_task_schedules`` contribution, and invoked by
    the trigger endpoint with an already-created id so the caller can be answered
    before the Nomad work begins.

    :param execution_id: An already-created run's id, or ``None`` to mint one.
    :param node_ids: The hosts to refresh, or ``None`` for the whole estate. The
        scheduled sweep passes nothing, which is what keeps it a full refresh.
    :return: The run's id, as a string.
    :raises FailureNotRecordedError: When the sweep failed and the database could
        not be reached to say so; :func:`fail_om_run` is queued to say it later.
    """
    try:
        # ty-attr-ok: Celery installs `loop` at runtime.
        resolved = celery.loop.run_until_complete(
            run_probe(UUID(execution_id) if execution_id else None, node_ids)
        )
    except FailureNotRecordedError as unrecorded:
        _queue(
            fail_om_run, str(unrecorded.run_id), unrecorded.error, what="the failure"
        )
        raise
    return str(resolved)


def _queue(
    task: CeleryTask, *args: str, what: str, countdown: timedelta = timedelta(0)
) -> None:
    """Queue a recovery task, logging rather than raising if the broker refuses it.

    The message expires after ``STALE_RUN_AFTER``, by when the trigger endpoint's
    age check has failed any run still left running, so it is not tried for ever.

    :param task: The task.
    :param args: Its arguments.
    :param what: What it records, for the log.
    :param countdown: How long to wait before running it.
    """
    try:
        task.apply_async(
            args,
            countdown=countdown.total_seconds(),
            expires=utc_now() + om_inventory_settings.STALE_RUN_AFTER,
        )
    except (OSError, KombuError):
        logger.exception("OM inventory: could not queue the recovery of %s", what)


def _backoff(task: CeleryTask) -> int:
    """Return how long ``task`` waits before its next attempt, doubling to a cap.

    :param task: The bound task.
    :return: The wait, in seconds.
    """
    return get_exponential_backoff_interval(1, task.request.retries, RETRY_MAX_DELAY)


@celery.task(bind=True, max_retries=None)
def fail_om_run(self: CeleryTask, run_id: str, error: str) -> None:
    """Write a sweep's failure once PMM Extensions' database answers again.

    A run that already finished, or was failed by something else meanwhile, is left
    as it is.

    :param self: The bound task, for retrying.
    :param run_id: The run.
    :param error: The failure detail.
    """
    try:
        # ty-attr-ok: Celery installs `loop` at runtime.
        celery.loop.run_until_complete(fail_run(UUID(run_id), error))
    except Exception as exc:
        if not database_unreachable(exc):
            raise
        raise self.retry(exc=exc, countdown=_backoff(self)) from exc


@celery.task(bind=True, max_retries=None)
def recover_om_runs(self: CeleryTask) -> None:
    """Fail the scans a worker restart interrupted.

    Retried while PMM Extensions' database is not reachable or not yet migrated,
    since the worker starts without waiting for either. The boot is the one this
    process inherited, never an argument: a message an earlier boot queued would
    otherwise reap this boot's runs.

    :param self: The bound task, for retrying.
    """
    boot = WorkerBoot.current
    if boot is None:
        logger.warning("OM inventory: no worker boot recorded, nothing to recover")
        return
    try:
        # ty-attr-ok: Celery installs `loop` at runtime.
        celery.loop.run_until_complete(recover_interrupted_runs(boot))
    except ProgrammingError as exc:
        raise self.retry(exc=exc, countdown=RETRY_MAX_DELAY) from exc
    except Exception as exc:
        if not database_unreachable(exc):
            raise
        raise self.retry(exc=exc, countdown=_backoff(self)) from exc


@worker_init.connect
def record_worker_boot(**_: Any) -> None:
    """Record this worker start in its main process, before the pool forks."""
    boot = WorkerBoot.start()
    logger.info("OM inventory: worker boot %s", boot.boot_id)


@worker_ready.connect
def queue_run_recovery(**_: Any) -> None:
    """Queue :func:`recover_om_runs` once the worker can take tasks.

    Twice: at once for the runs another boot claimed, and again after
    ``UNCLAIMED_GRACE`` for the runs nobody claimed. A publish that fails is logged,
    not raised: every scan task recovers the same runs before it sweeps.
    """
    _queue(recover_om_runs, what="interrupted scans")
    _queue(recover_om_runs, what="interrupted scans", countdown=UNCLAIMED_GRACE)
