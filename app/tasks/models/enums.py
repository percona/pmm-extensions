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

"""Define the enums of the Task API models."""

from enum import auto, StrEnum


class TaskBackendEnum(StrEnum):
    """Control the choice of backends.

    :cvar NOMAD: Enum value for Nomad backend.
    :vartype NOMAD: str
    :cvar PROXY: Enum value for Proxy backend.
    :vartype PROXY: str
    :cvar CELERY: Enum value for Celery backend.
    :vartype CELERY: str
    """

    NOMAD = auto()
    PROXY = auto()
    CELERY = auto()


class TaskLogType(StrEnum):
    """Define the type of task log.

    :cvar STDOUT: Enum value for standard output logs.
    :vartype STDOUT: str
    :cvar STDERR: Enum value for standard error logs.
    :vartype STDERR: str
    """

    STDOUT = "stdout"
    STDERR = "stderr"


class LogCaptureStatusEnum(StrEnum):
    """Describe how completely PMM Extensions captured a task's log stream.

    Distinguishes a stream that genuinely produced nothing from one whose bytes
    were lost before PMM Extensions could read them — the stored offsets alone cannot tell
    those apart, since both leave the cursors at zero.

    ``UNKNOWN`` is the honest verdict where no evidence survives: rows written
    before the column existed, and histories carrying no state rows at all.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


#: Severity order for reducing a task's per-stream verdicts to one. A single
#: lost stream makes the whole execution's logs untrustworthy, so ``INCOMPLETE``
#: outranks the rest; ``UNKNOWN`` outranks ``COMPLETE`` so an unclassified
#: stream is never rounded up into a clean bill of health.
CAPTURE_STATUS_PRECEDENCE: tuple[LogCaptureStatusEnum, ...] = (
    LogCaptureStatusEnum.INCOMPLETE,
    LogCaptureStatusEnum.UNKNOWN,
    LogCaptureStatusEnum.COMPLETE,
)
