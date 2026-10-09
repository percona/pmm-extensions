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

"""Define the task-execution status vocabulary.

Split out of :mod:`app.tasks.models` so a table in another service can denormalize
the status and type its column with the enum. Such a table cannot import
``app.tasks.models`` itself: plugin ``models.py`` modules are loaded by the Alembic
plugin-discovery loader, and pulling in another service's models would register its
tables in ``SQLModel.metadata`` and leak them into the ``extensions`` autogenerate. A fixed
value set is not a reason to widen the column to a bare ``str``, so the enum moves to
where both sides can reach it rather than the column losing its constraint.

**Import-safety is the whole point of this module, and it is a property of what the
file does not contain.** Nothing here imports anything but the standard library, and
``app/tasks/__init__.py`` is licence header only, so importing this module defines no
table and executes no service wiring. Adding a SQLModel class, or an import that
reaches one, silently reintroduces the leak this split exists to avoid.

:mod:`app.tasks.models` re-exports :class:`TaskHistoryStatusEnum`, so the established
import path keeps working for every existing consumer.
"""

__all__ = ["TaskHistoryStatusEnum"]

from enum import StrEnum


class TaskHistoryStatusEnum(StrEnum):
    """Define status codes for task executions.

    :cvar FAILED: Enum value for failed tasks.
    :cvar PENDING: Enum value for pending tasks.
    :cvar RUNNING: Enum value for running tasks.
    :cvar SUCCESS: Enum value for successfully completed tasks.
    :cvar STOPPED: Enum value for stopped tasks.
    :cvar LOST: Enum value for tasks that are lost.
    :cvar STALE: Enum value for tasks skipped because executor placement
        exceeded the configured staleness threshold (for example a Nomad
        allocation that never left the queue).
    :cvar UNLAUNCHABLE: Enum value for tasks the executor node could not
        launch at all, because some command in the invocation does not
        resolve there. The payload never ran, so this is not a script
        failure.
    """

    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    STOPPED = "stopped"
    LOST = "lost"
    STALE = "stale"
    UNLAUNCHABLE = "unlaunchable"

    def is_finished(self) -> bool:
        """Check whether this status has an observed run outcome.

        :return: ``True`` when output retrieval is meaningful for this status
            (FAILED, SUCCESS, STOPPED, STALE, or UNLAUNCHABLE); ``False``
            otherwise.
        """
        return self in [
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.STALE,
            TaskHistoryStatusEnum.UNLAUNCHABLE,
        ]

    def is_terminal(self) -> bool:
        """Check if task execution has reached a terminal state.

        :return: ``True`` if task execution will not transition again.
        """
        return self.is_finished() or self == TaskHistoryStatusEnum.LOST

    @classmethod
    def active_statuses(cls) -> frozenset["TaskHistoryStatusEnum"]:
        """Return the statuses whose executions are still in flight.

        These are the non-terminal statuses (``PENDING`` / ``RUNNING``); a new
        non-terminal status only needs adding here.

        :return: The frozen set of in-flight statuses.
        """
        return frozenset({cls.PENDING, cls.RUNNING})

    @classmethod
    def interrupted_statuses(cls) -> frozenset["TaskHistoryStatusEnum"]:
        """Return the terminal statuses of a run that was ended rather than finished.

        Tracking of the run was lost, its placement went stale, or it was stopped:
        none of them is the run's own verdict on its work.

        :return: The frozen set of interrupted statuses.
        """
        return frozenset({cls.LOST, cls.STALE, cls.STOPPED})

    def is_active(self) -> bool:
        """Check whether the task status indicates an in-flight execution.

        :return: ``True`` if the status is ``PENDING`` or ``RUNNING``;
            ``False`` otherwise.
        """
        return self in self.active_statuses()

    def operator_summary(self) -> str | None:
        """Return the operator-facing prose for this status, if it has any.

        The single source both :meth:`TaskHistory.alert_for_status` and the
        ``failure_reason`` composers read, so the alert summary and the stored
        reason cannot drift apart. Phrased as a sentence fragment because the
        alert interpolates it mid-sentence.

        :return: The prose fragment, or ``None`` for a status carrying none.
        """
        return {
            TaskHistoryStatusEnum.FAILED: "failed",
            TaskHistoryStatusEnum.LOST: "execution tracking lost",
            TaskHistoryStatusEnum.STALE: (
                "skipped as stale (executor placement delayed past threshold)"
            ),
            TaskHistoryStatusEnum.UNLAUNCHABLE: (
                "could not be launched (the executor node cannot run the "
                "requested command)"
            ),
        }.get(self)
