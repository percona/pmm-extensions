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

"""Watch a running execution's live log the way one viewer's request does.

:class:`LiveViewer` reads :meth:`NomadExecutor.stream_logs` in a task of its
own, one line at a time, and can stop reading in between, which is how a slow
HTTP client looks from the generator's side. :func:`attach` is the barrier a
test needs before it writes: a viewer whose stream has delivered a marker is
attached to every shared follow of its run.
"""

__all__ = ["LiveViewer", "attach", "live_history", "until"]

import asyncio
import itertools
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

import pytest

from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.models import (
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
)
from tests.app.tasks.nomad_log_stub import NomadLogStub

#: Seconds between two checks of an awaited condition.
POLL_INTERVAL = 0.01

#: Default bound on any wait: the stub's heartbeats and the executor's
#: dead-step recheck take a second or two between them.
WAIT_TIMEOUT = 15.0

_markers = itertools.count()


async def until(predicate: Callable[[], bool], within: float = WAIT_TIMEOUT) -> None:
    """Yield to the loop until ``predicate`` holds, failing after ``within``.

    The conditions span the stub's thread, the executor and several viewers, so
    no one of them owns an event to wait on; a bounded poll covers them all.

    :param predicate: The condition to wait for.
    :param within: Seconds to wait before failing the test.
    """
    for _ in range(int(within / POLL_INTERVAL)):
        if predicate():
            return
        await asyncio.sleep(POLL_INTERVAL)
    pytest.fail(f"condition not reached within {within}s")


def live_history(
    stub: NomadLogStub, entities: set[PIIEntity] | None = None
) -> TaskHistory:
    """Build a RUNNING history tracking the stub's allocation.

    :param stub: The stub whose job and evaluation the history tracks.
    :param entities: The PII entities the history anonymizes; none by default.
    :return: The unsaved history, which is all ``stream_logs`` reads.
    """
    return TaskHistory(
        execution_request=TaskExecutionRequest(
            task="live-log-task",
            target="node1",
            tracking={"job_id": stub.job_id, "evaluation_id": stub.eval_id},
        ),
        anonymize_mask=PIIEntity.encode_selection(entities or set()),
        status=TaskHistoryStatusEnum.RUNNING,
        executed_by="test-user",
    )


class LiveViewer:
    """Read one viewer's live log stream, pausable between lines.

    :param executor: The executor the viewer's request resolved.
    :param history: The history being watched.
    :param step: The step whose start offsets ``offsets`` gives.
    :param offsets: The viewer's start offset per log type; ``0`` otherwise.
    """

    def __init__(
        self,
        executor: NomadExecutor,
        history: TaskHistory,
        step: str,
        offsets: dict[TaskLogType, int] | None = None,
    ) -> None:
        self.offsets = offsets or {}
        self.logs: dict[TaskLogType, list[TaskLog]] = {
            log_type: [] for log_type in TaskLogType
        }
        self.reading = asyncio.Event()
        self.reading.set()
        stream = executor.stream_logs(
            history,
            {step: {log_type.value: start for log_type, start in self.offsets.items()}},
        )
        self.task = asyncio.create_task(self._read(stream))

    async def _read(self, stream: AsyncGenerator[TaskLog | None]) -> None:
        """Record every line, waiting on :attr:`reading` before each one.

        :param stream: The viewer's ``stream_logs`` generator.
        """
        async with aclosing(stream) as logs:
            while True:
                await self.reading.wait()
                try:
                    log = await anext(logs)
                except StopAsyncIteration:
                    return
                if log is not None:
                    self.logs[log.type].append(log)

    def text(self, log_type: TaskLogType = TaskLogType.STDOUT) -> str:
        """Return everything received for ``log_type``, joined.

        :param log_type: The log type to read.
        :return: The concatenated text.
        """
        return "".join(log.msg or "" for log in self.logs[log_type])

    def offset(self, log_type: TaskLogType = TaskLogType.STDOUT) -> int:
        """Return the offset of the last line received for ``log_type``.

        :param log_type: The log type to read.
        :return: The offset, or ``-1`` before any line arrived.
        """
        received = self.logs[log_type]
        return received[-1].offset if received else -1

    @property
    def finished(self) -> bool:
        """Return whether the stream has ended."""
        return self.task.done()

    async def leave(self) -> None:
        """Disconnect, as a viewer closing its response does."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def attach(stub: NomadLogStub, *viewers: LiveViewer) -> None:
    """Wait until every viewer is attached to its run's shared follows.

    A marker written to stderr reaches a viewer only once its forwarders have
    subscribed, and the stdout forwarder subscribes before the stderr one.

    :param stub: The stub serving the run.
    :param viewers: The viewers to wait for.
    """
    marker = f"attached-{next(_markers)}\n"
    await stub.write(TaskLogType.STDERR, marker.encode())
    await until(
        lambda: all(
            viewer.text(TaskLogType.STDERR).endswith(marker) for viewer in viewers
        )
    )
