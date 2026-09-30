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

"""Provide helpers shared by the periodic-task routes."""

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy_celery_beat import PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import make_datetime_utc
from app.tasks.crud import TaskHistoryManager
from app.tasks.periodic.models import resolve_task_name

if TYPE_CHECKING:
    from datetime import datetime


def resolve_schedule_task_name(periodic_task: PeriodicTask) -> str | None:
    """Return the PMM Extensions task name a beat-store schedule runs, or ``None``.

    Decode the row's ``args``/``kwargs`` and hand them to
    :func:`~app.tasks.periodic.models.resolve_task_name`. A row whose arguments
    are not JSON of the expected shape, or that names no task, resolves to
    ``None`` rather than raising, so one unreadable row cannot fail a caller
    iterating the whole beat store.

    :param periodic_task: The beat-store row to inspect.
    :return: The resolved PMM Extensions task name, or ``None`` when it cannot be derived.
    """
    try:
        args = json.loads(periodic_task.args) if periodic_task.args else None
        kwargs = json.loads(periodic_task.kwargs) if periodic_task.kwargs else None
    except json.JSONDecodeError:
        return None
    if not isinstance(args, list | None) or not isinstance(kwargs, dict | None):
        return None
    name = resolve_task_name(args, kwargs)
    return name if isinstance(name, str) and name else None


def generate_periodic_task_name(task_name: str, period: str, kwargs: str) -> str:
    """Derive a stable auto-generated name for an unnamed periodic task.

    Digest rather than :func:`hash`: the builtin is salted per process, so two
    processes computing the same unnamed create request would derive two
    different names and the database's uniqueness check would never catch
    the duplicate. Eight digest bytes, not the four
    :func:`~app.core.db.utils.advisory_lock_key` uses — that size fits a
    signed 32-bit PostgreSQL advisory-lock key, a constraint that doesn't
    apply here, and ``PeriodicTask.name`` is a 255-character column that also
    has to fit ``task_name``. The digest covers all three inputs, not just
    ``kwargs``: the returned name's visible prefix collapses every space to
    an underscore, so two task names differing only by that character
    (``"a b"`` vs ``"a_b"``) would otherwise render identically once a
    digest over ``kwargs`` alone happened to match.

    :param task_name: Name of the task the periodic schedule executes.
    :param period: The schedule's period.
    :param kwargs: The periodic task's JSON-encoded ``kwargs`` string.
    :return: A name stable across processes and ``PYTHONHASHSEED`` values for
        this ``(task_name, period, kwargs)`` triple.
    """
    digest_input = f"{task_name}\x00{period}\x00{kwargs}".encode()
    digest = hashlib.blake2b(digest_input, digest_size=8).hexdigest()
    return f"run_{task_name}_{period}_{digest}".replace(" ", "_")


async def attach_last_run_status(
    tasks_session: AsyncSession, periodic_tasks: list[PeriodicTask]
) -> list[PeriodicTask]:
    """Stamp each schedule's own last-run result onto its beat-store rows.

    Resolve the PMM Extensions task name behind every schedule, fetch system-triggered
    history points for those names in a single bulk query bounded by the earliest
    dispatch time in play, then attribute to each schedule the earliest point
    whose ``created_at`` is at or after that schedule's ``last_run_at`` -- the
    beat store records ``last_run_at`` when it dispatches, so the schedule's own
    run is the first system row at or after that instant. A schedule that has
    never run (``last_run_at is None``) is forced to ``None``. Correlating on the
    schedule's own dispatch time keeps a later, unrelated system run of the same
    task name (a chain child or connectivity check) from being reported as this
    schedule's result. Two schedules that last ran at the same instant on the
    same task name still resolve to the same point.

    ``last_run_at`` is floored to whole seconds before comparison because
    ``TaskHistory.created_at`` is stored at whole-second granularity (``utc_now``)
    while the scheduler writes ``last_run_at`` with microseconds intact --
    comparing at the finer granularity would systematically miss a schedule's own
    row. The trade-off is a sub-second window admitting an unrelated same-second
    system row dispatched just before this schedule, which is preferred over the
    otherwise-constant miss.

    :param tasks_session: The tasks-database session used for the history lookup.
    :param periodic_tasks: The beat-store rows to annotate in place.
    :return: The same rows, each carrying a ``last_run_status`` attribute.
    """
    ran = [
        (task, name, make_datetime_utc(task.last_run_at).replace(microsecond=0))
        for task in periodic_tasks
        if (name := resolve_schedule_task_name(task)) and task.last_run_at is not None
    ]
    thresholds: dict[str, datetime] = {}
    for _, name, run_at in ran:
        thresholds[name] = min(run_at, thresholds.get(name, run_at))
    points = await TaskHistoryManager.recent_system_status_points_by_task_names(
        tasks_session, thresholds
    )
    for task in periodic_tasks:
        task.last_run_status = None
    for task, name, run_at in ran:
        task.last_run_status = next(
            (
                point.status
                for point in points.get(name, ())
                if make_datetime_utc(point.created_at) >= run_at
            ),
            None,
        )
    return periodic_tasks
