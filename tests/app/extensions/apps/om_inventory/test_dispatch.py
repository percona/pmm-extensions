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

"""Test how a dispatched scan's logs are read back from the tasks API."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.requests import RemoteAPI
from app.extensions.apps.om_inventory.dispatch import _read_logs, STEP_LOG_TAIL
from tests.app.extensions.apps.om_inventory.conftest import log_line

HISTORY_ID = 811
#: A payload record, as ``run-script`` prints one per line.
RECORD = {"service": None, "system": {"os_name": "Ubuntu 24.04"}}


def streaming(lines: list[str]) -> MagicMock:
    """Build a tasks API stub whose log stream yields ``lines``.

    :param lines: The NDJSON lines the stream yields, every step's.
    :return: The stub.
    """
    api = MagicMock(spec=RemoteAPI)

    async def stream(path: str, **_: Any) -> AsyncIterator[str]:
        for line in lines:
            yield line

    api.stream = MagicMock(side_effect=stream)
    return api


class TestTheLogsReadAreBounded:
    """Keep the payload's records whole and only the end of every other stream."""

    @pytest.mark.asyncio
    async def test_a_verbose_step_keeps_its_tail_and_the_records_stay_whole(
        self,
    ) -> None:
        """Hold no more of a step's output than a failure detail could use."""
        noise = "Collecting pymongo\n" * (3 * STEP_LOG_TAIL // 19)
        records = (json.dumps(RECORD) + "\n") * (3 * STEP_LOG_TAIL // 60)
        api = streaming(
            [
                *(
                    log_line("prepare-env", "stderr", line)
                    for line in noise.splitlines(keepends=True)
                ),
                log_line("prepare-env", "stderr", "the last line\n"),
                log_line("run-script", "stdout", records),
            ]
        )

        logs = await _read_logs(api, HISTORY_ID)

        prepare_env = logs["prepare-env"]["stderr"]
        assert len(prepare_env) == STEP_LOG_TAIL
        assert prepare_env.endswith("the last line\n")
        assert logs["run-script"]["stdout"] == records
