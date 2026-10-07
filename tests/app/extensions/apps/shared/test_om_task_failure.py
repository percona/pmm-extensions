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

"""Test the task-history failure reading every OM app shares.

``om_inventory``'s scan tests pin what a scan reports end to end; these pin the
pieces both it and ``om_bootstrap`` build on, apart from either app.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.extensions.apps.shared.om.task_failure import (
    describe_task_failure,
    excerpt,
    failed_step,
    MAX_ERROR_DETAIL,
    read_step_logs,
)
from app.tasks.execution.executors.nomad.models import _failed_step_reason


def _line(step: str, stream: str, msg: str) -> str:
    """Build one line of the tasks API's log stream.

    :param step: The step that wrote it.
    :param stream: ``stdout`` or ``stderr``.
    :param msg: What it wrote.
    :return: The NDJSON line.
    """
    return json.dumps({"step": step, "type": stream, "msg": msg})


class TestFailedStep:
    """Read the failed step off the reason the executor really writes."""

    @pytest.mark.parametrize("step", ["prepare-env", "run-script", "check-launchable"])
    def test_names_the_step_from_the_executor_s_own_sentence(self, step: str) -> None:
        """Parse the step out of ``_failed_step_reason``'s output.

        :param step: The step that failed.
        """
        reason = _failed_step_reason(
            {
                "TaskStates": {
                    step: {
                        "Failed": True,
                        "Events": [{"Type": "Terminated", "ExitCode": 123}],
                    }
                }
            }
        )

        assert failed_step(reason) == step

    @pytest.mark.parametrize("reason", [None, "", "Execution tracking lost."])
    def test_names_no_step_for_a_reason_without_one(self, reason: str | None) -> None:
        """Answer ``None`` where the reason names no step.

        :param reason: The reason.
        """
        assert failed_step(reason) is None


class TestReadStepLogs:
    """Group a run's log stream by step and stream."""

    @pytest.mark.asyncio
    async def test_groups_and_concatenates_and_skips_noise(self) -> None:
        """Concatenate per step and stream, skipping blank and non-JSON lines."""
        lines = [
            _line("prepare-env", "stderr", "a"),
            "",
            "not json",
            _line("run-script", "stdout", "b"),
            _line("prepare-env", "stderr", "c"),
        ]

        async def stream(path: str, **_: Any) -> AsyncIterator[str]:
            assert path == "/history/7/logs/"
            for line in lines:
                yield line

        api = MagicMock()
        api.stream = MagicMock(side_effect=stream)

        logs = await read_step_logs(api, 7)

        assert logs == {"prepare-env": {"stderr": "ac"}, "run-script": {"stdout": "b"}}


class TestDescribeTaskFailure:
    """Say why a run failed from its reason and the failed step's output."""

    def test_prefers_the_failed_step_s_stderr(self) -> None:
        """Report the step the reason names, from its stderr."""
        logs = {
            "prepare-env": {"stdout": "progress", "stderr": "python3: not found\n"},
            "run-script": {"stderr": "unrelated"},
        }

        detail = describe_task_failure(
            "Step 'prepare-env' failed (exit code 127).",
            logs,
            default_step="run-script",
        )

        assert detail == "Step 'prepare-env' failed (exit code 127). python3: not found"

    def test_falls_back_to_stdout_then_to_the_default_step(self) -> None:
        """Use stdout when stderr is empty, and the default step with no reason."""
        logs = {"run-script": {"stdout": "said it here\n"}}

        assert describe_task_failure(None, logs, default_step="run-script") == (
            "said it here"
        )

    def test_is_empty_with_nothing_to_say(self) -> None:
        """Return an empty string so the caller can choose its own fallback."""
        assert describe_task_failure(None, {}, default_step="run-script") == ""

    def test_keeps_the_end_of_a_long_stream_within_the_cap(self) -> None:
        """Bound the excerpt, keeping its end where the error is."""
        stream = "x" * (MAX_ERROR_DETAIL * 3) + "the error"

        assert excerpt(stream).endswith("the error")
        assert len(excerpt(stream)) == MAX_ERROR_DETAIL
        assert excerpt(stream, 9) == "the error"
