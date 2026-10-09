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

"""Test reading why a task history failed, apart from any app that reports it."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.extensions.apps.shared.om.task_failure import (
    describe_task_failure,
    failed_step,
    MAX_ERROR_DETAIL,
    read_step_logs,
)


def _line(step: str, stream: str, msg: str) -> str:
    """Build one line of the tasks API's log stream.

    :param step: The step that wrote it.
    :param stream: ``stdout`` or ``stderr``.
    :param msg: What it wrote.
    :return: The NDJSON line.
    """
    return json.dumps({"step": step, "type": stream, "msg": msg})


class TestFailedStep:
    """Read the failed step off the reason the executor writes."""

    @pytest.mark.parametrize(
        ("reason", "step"),
        [
            ("Step 'prepare-env' failed (exit code 127).", "prepare-env"),
            ("Step 'run-script' failed (exit code 123).", "run-script"),
            ("Step 'check-launchable' failed.", "check-launchable"),
        ],
    )
    def test_names_the_step_the_reason_names(self, reason: str, step: str) -> None:
        """Parse the step out of the executor's sentence, with or without a code.

        The executor's own tests pin that it writes this sentence.

        :param reason: The reason.
        :param step: The step it names.
        """
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
        """Bound the output, keeping its end where the error is, and the reason whole."""
        reason = "Step 'run-script' failed (exit code 124)."
        stream = "x" * (MAX_ERROR_DETAIL * 3) + "the error\n"

        detail = describe_task_failure(
            reason, {"run-script": {"stderr": stream}}, default_step="run-script"
        )

        assert detail.startswith(f"{reason} x")
        assert detail.endswith("the error")
        assert len(detail) == len(reason) + 1 + MAX_ERROR_DETAIL
