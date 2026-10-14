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

"""Read why a finished task history failed, in the node's own words.

The reason is in two places: the run's ``failure_reason`` names the step that
failed and its exit code, and that step's own output says why. The status alone
says neither.
"""

import re
from collections import defaultdict

from pydantic import ValidationError

from app.core.requests import RemoteAPI
from app.tasks.models import TaskLog, TaskLogType

__all__ = [
    "MAX_ERROR_DETAIL",
    "STEP_LOG_TAIL",
    "describe_task_failure",
    "failed_step",
    "read_step_logs",
]

#: Bound on a failure detail an OM app stores or reports, whether the dispatch
#: failed or the database refused the payload. The row only needs the part that
#: says what happened, and dispatch stderr, a driver error the payload does not
#: recognise, or a package manager printing its whole transaction before the line
#: saying it gave up can run to several kilobytes.
MAX_ERROR_DETAIL = 500
#: How much of a step's stream :func:`read_step_logs` keeps, from its end, unless
#: told to keep it whole. Far more than the ``MAX_ERROR_DETAIL`` a failure detail
#: takes from it.
STEP_LOG_TAIL = 8192

#: The step the tasks service names in a run's ``failure_reason``, in the sentence
#: the Nomad executor's ``failed_step_reason`` writes. That function's tests pin
#: the sentence and read it back through :func:`failed_step`, so a change to its
#: wording fails a test there.
_FAILED_STEP = re.compile(r"^Step '(?P<step>[^']+)' failed")


def failed_step(failure_reason: str | None) -> str | None:
    """Return the step a run's ``failure_reason`` names, if it names one.

    :param failure_reason: The tasks service's account of the run, when it has one.
    :return: The failed step's name, or ``None`` when the reason names no step.
    """
    match = _FAILED_STEP.match(failure_reason or "")
    return match.group("step") if match else None


async def read_step_logs(
    tasks_api: RemoteAPI,
    task_history_id: int,
    *,
    keep_whole: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, dict[str, str]]:
    """Stream a finished run's logs, every step, and return them by step and stream.

    Every step rather than only the payload's: when ``prepare-env`` fails, the
    payload never runs and its own streams are empty, so the failed step's output is
    the only account there is of why.

    Only the streams in ``keep_whole`` are kept in full. Every other one keeps its
    last :data:`STEP_LOG_TAIL` characters as it is read: a failure detail takes only
    the end of one, and a verbose step would otherwise be held in full for every
    run read at once.

    :param tasks_api: The tasks API client.
    :param task_history_id: The run to read.
    :param keep_whole: The ``(step, stream)`` pairs to keep in full, such as a
        payload's ``stdout`` that is parsed rather than quoted.
    :return: The concatenated output, keyed by step and then by ``stdout`` or
        ``stderr``. A step or stream with no output is absent.
    """
    steps: dict[str, dict[str, str]] = defaultdict(lambda: defaultdict(str))
    async for entry in tasks_api.stream(f"/history/{task_history_id}/logs/"):
        if not entry:
            continue
        # Through the tasks API's own model, so a line of the wrong shape is skipped
        # like any other noise rather than failing the read.
        try:
            log = TaskLog.model_validate_json(entry)
        except ValidationError:
            continue
        step, stream = log.step, log.type.value
        text = steps[step][stream] + (log.msg or "")
        # Cut at twice the tail, so a stream of short lines is not copied per line.
        if (step, stream) not in keep_whole and len(text) > 2 * STEP_LOG_TAIL:
            text = text[-STEP_LOG_TAIL:]
        steps[step][stream] = text
    return {
        step: {
            stream: text if (step, stream) in keep_whole else text[-STEP_LOG_TAIL:]
            for stream, text in streams.items()
        }
        for step, streams in steps.items()
    }


def _excerpt(text: str) -> str:
    """Return the end of a stream, bounded, which is where the error usually is.

    The end rather than the start: a traceback ends with the exception, and ``pip``
    or ``dnf`` print their whole resolution before the line saying they gave up.

    :param text: The stream.
    :return: At most :data:`MAX_ERROR_DETAIL` characters of its end, stripped.
    """
    return text.strip()[-MAX_ERROR_DETAIL:].strip()


def describe_task_failure(
    failure_reason: str | None,
    logs: dict[str, dict[str, str]],
    *,
    default_step: str,
) -> str:
    """Say why a run failed: the tasks service's reason, then the step's own output.

    The failed step's output where the tasks service named one, since that is where
    the reason is; otherwise ``default_step``'s. ``stderr`` first, and the step's
    ``stdout`` only when it said nothing on ``stderr`` - ``python3 -m venv`` reports
    a missing ``ensurepip`` there.

    :param failure_reason: The tasks service's account of the run, when it has one.
    :param logs: The run's output, by step and stream, as :func:`read_step_logs`
        returns it.
    :param default_step: The step whose output to report when the reason names
        none - the one that does the run's actual work.
    :return: The reason, kept whole, and the last :data:`MAX_ERROR_DETAIL`
        characters of the output, joined by a space; either one alone when the
        other is missing, or an empty string when there is neither.
    """
    streams = logs.get(failed_step(failure_reason) or default_step) or {}
    output = _excerpt(streams.get(TaskLogType.STDERR, "")) or _excerpt(
        streams.get(TaskLogType.STDOUT, "")
    )
    return " ".join(part for part in (failure_reason, output) if part)
