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

"""Cover the shared live-log follow against a scripted producer and reader.

The producer emits slices of :data:`RAW` whose ends the test chooses, and the
catch-up reader serves any range of it, so a subscriber's text can be compared
with the raw bytes it asked for and every catch-up it needed is on record.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

import pytest

from app.tasks.execution.executors.nomad.log_follow import (
    LogCatchUpError,
    LogFollowRegistry,
    SharedLogFollow,
    SUBSCRIBER_BACKLOG,
)
from app.tasks.models import TaskLog, TaskLogType
from tests.app.tasks.live_log_viewer import until

pytestmark = pytest.mark.asyncio

#: Fixed-width lines, so every multiple of :data:`LINE` is a line boundary.
LINE = 8
RAW = "".join(f"line-{index:02d}\n" for index in range(100)) * 2
STEP = "run-script"
KEY = ("alloc", STEP, TaskLogType.STDOUT, frozenset())

#: Upper bound on any wait, so a subscriber that never gets its text fails.
WAIT_TIMEOUT = 2.0


def _log(msg: str | None, offset: int = 0) -> TaskLog:
    """Build a log line of the one stream these tests follow.

    :param msg: The text, or ``None`` for the end marker.
    :param offset: The raw offset the text ends at.
    :return: The log line.
    """
    return TaskLog(step=STEP, type=TaskLogType.STDOUT, msg=msg, offset=offset)


class ScriptedProducer:
    """Emit the releases the test commands, as the executor's producer would."""

    def __init__(self, *, slow_cancel: bool = False) -> None:
        self.starts: list[int] = []
        self.cancelled = 0
        self.ended = asyncio.Event()
        self.release_end = asyncio.Event()
        self._commands: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._slow_cancel = slow_cancel

    def emit(self, end: int) -> None:
        """Release the raw text up to ``end``."""
        self._commands.put_nowait(("emit", end))

    def force(self, end: int) -> None:
        """Release the raw text up to ``end`` as a forced flush."""
        self._commands.put_nowait(("forced", end))

    def finish(self) -> None:
        """Send the end marker, then wait for :attr:`release_end` to return."""
        self._commands.put_nowait(("end", 0))

    def fail(self) -> None:
        """Raise an unexpected error."""
        self._commands.put_nowait(("raise", 0))

    async def __call__(self, sink: SharedLogFollow, start_offset: int) -> None:
        """Run the script against ``sink`` from ``start_offset``.

        :param sink: The shared follow the releases go to.
        :param start_offset: The raw offset the follow starts at.
        """
        self.starts.append(start_offset)
        position = start_offset
        try:
            while True:
                kind, end = await self._commands.get()
                if kind == "raise":
                    raise RuntimeError("the producer broke")
                if kind == "end":
                    await sink.put(_log(None))
                    self.ended.set()
                    await self.release_end.wait()
                    return
                if kind == "forced":
                    sink.mark_forced()
                await sink.put(_log(RAW[position:end], end))
                position = end
        except asyncio.CancelledError:
            self.cancelled += 1
            if self._slow_cancel:
                await asyncio.sleep(0.05)
            raise


class RangeReader:
    """Serve catch-up reads of :data:`RAW` and record every range asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail = False

    def __call__(self, start: int, end: int) -> AsyncGenerator[TaskLog]:
        """Record the range, then read it.

        :param start: The raw offset the range starts at.
        :param end: The raw offset the range ends at.
        :return: The range's text as one log line.
        """
        self.calls.append((start, end))
        return self._read(start, end)

    async def _read(self, start: int, end: int) -> AsyncGenerator[TaskLog]:
        """Yield the range, or fail when told to.

        :param start: The raw offset the range starts at.
        :param end: The raw offset the range ends at.
        :return: The range's text as one log line.
        :raises LogCatchUpError: When :attr:`fail` is set.
        """
        if self.fail:
            raise LogCatchUpError("the catch-up read failed")
        yield _log(RAW[start:end], end)


class Subscriber:
    """Read one subscription in a task of its own, pausable between lines."""

    def __init__(self, logs: AsyncGenerator[TaskLog]) -> None:
        self.received: list[TaskLog] = []
        self.finished = False
        self.reading = asyncio.Event()
        self.reading.set()
        self.task = asyncio.create_task(self._run(logs))

    async def _run(self, logs: AsyncGenerator[TaskLog]) -> None:
        """Collect every line, waiting on :attr:`reading` before each one.

        :param logs: The subscription.
        """
        async with aclosing(logs) as stream:
            while True:
                await self.reading.wait()
                try:
                    log = await anext(stream)
                except StopAsyncIteration:
                    break
                self.received.append(log)
        self.finished = True

    @property
    def text(self) -> str:
        """Return everything received, joined."""
        return "".join(log.msg or "" for log in self.received)

    @property
    def position(self) -> int:
        """Return the offset of the last line received, or ``-1`` for none."""
        return self.received[-1].offset if self.received else -1

    async def leave(self) -> None:
        """Disconnect, as a viewer closing its response does."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def _until(predicate: Callable[[], bool]) -> None:
    """Wait until ``predicate`` holds, within the in-process bound.

    :param predicate: The condition to wait for.
    """
    await until(predicate, within=WAIT_TIMEOUT)


async def _settle() -> None:
    """Let every runnable task reach its next wait."""
    for _ in range(20):
        await asyncio.sleep(0)


def _subscribe(
    registry: LogFollowRegistry,
    start: int,
    producer: ScriptedProducer,
    reader: RangeReader,
) -> Subscriber:
    """Start a subscriber of :data:`KEY` from ``start``.

    :param registry: The registry to subscribe through.
    :param start: The subscriber's own start offset.
    :param producer: The producer a new follow runs.
    :param reader: The catch-up reader.
    :return: The running subscriber.
    """
    return Subscriber(registry.subscribe(KEY, start, producer, reader))


async def test_subscribers_attached_before_the_first_release_need_no_catch_up() -> None:
    """Deliver every release directly to subscribers that were there from the start."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    second = _subscribe(registry, 0, producer, reader)
    await _settle()

    for end in (LINE, 3 * LINE, 4 * LINE):
        producer.emit(end)
    await _until(lambda: first.position == second.position == 4 * LINE)

    assert first.text == second.text == RAW[: 4 * LINE]
    assert reader.calls == []
    assert producer.starts == [0]
    await first.leave()
    await second.leave()


async def test_a_joiner_from_zero_catches_up_to_the_frontier_then_follows() -> None:
    """Read the range a late joiner missed once, then hand it the shared feed."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    for end in (LINE, 2 * LINE, 3 * LINE):
        producer.emit(end)
    await _until(lambda: first.position == 3 * LINE)

    joiner = _subscribe(registry, 0, producer, reader)
    await _until(lambda: joiner.position == 3 * LINE)
    producer.emit(5 * LINE)
    await _until(lambda: joiner.position == first.position == 5 * LINE)

    assert reader.calls == [(0, 3 * LINE)]
    assert joiner.text == first.text == RAW[: 5 * LINE]
    assert producer.starts == [0]
    await first.leave()
    await joiner.leave()


async def test_a_resume_from_a_sent_boundary_catches_up_from_there() -> None:
    """Catch a resuming subscriber up from the offset it was last sent."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    for end in (2 * LINE, 5 * LINE):
        producer.emit(end)
    await _until(lambda: first.position == 5 * LINE)

    resumed = _subscribe(registry, 2 * LINE, producer, reader)
    await _until(lambda: resumed.position == 5 * LINE)
    producer.emit(6 * LINE)
    await _until(lambda: resumed.position == 6 * LINE)

    assert reader.calls == [(2 * LINE, 5 * LINE)]
    assert resumed.text == RAW[2 * LINE : 6 * LINE]
    await first.leave()
    await resumed.leave()


async def test_a_resume_inside_a_release_reads_that_release_instead() -> None:
    """Replace the release straddling a subscriber's offset with a catch-up to its end."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    producer.emit(LINE)
    await _until(lambda: first.position == LINE)

    resumed = _subscribe(registry, LINE + 4, producer, reader)
    await _settle()
    producer.emit(3 * LINE)
    producer.emit(4 * LINE)
    await _until(lambda: resumed.position == 4 * LINE)

    assert reader.calls == [(LINE + 4, 3 * LINE)]
    assert resumed.text == RAW[LINE + 4 : 4 * LINE]
    await first.leave()
    await resumed.leave()


async def test_a_resume_ahead_of_the_frontier_skips_what_it_already_has() -> None:
    """Skip releases ending at or before the offset, then catch up the straddle."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    resumed = _subscribe(registry, 3 * LINE + 2, producer, reader)
    await _settle()

    for end in (LINE, 2 * LINE, 3 * LINE, 5 * LINE, 6 * LINE):
        producer.emit(end)
    await _until(lambda: first.position == resumed.position == 6 * LINE)

    assert reader.calls == [(3 * LINE + 2, 5 * LINE)]
    assert resumed.text == RAW[3 * LINE + 2 : 6 * LINE]
    await first.leave()
    await resumed.leave()


async def test_a_mid_line_producer_start_is_never_a_handoff_point() -> None:
    """Make a joiner wait for the first release rather than read up to a mid-line start."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 7, producer, reader)
    joiner = _subscribe(registry, 0, producer, reader)
    await _settle()
    assert reader.calls == []

    producer.emit(2 * LINE)
    producer.emit(3 * LINE)
    await _until(lambda: first.position == joiner.position == 3 * LINE)

    assert producer.starts == [7]
    assert reader.calls == [(0, 2 * LINE)]
    assert first.text == RAW[7 : 3 * LINE]
    assert joiner.text == RAW[: 3 * LINE]
    await first.leave()
    await joiner.leave()


async def test_a_forced_flush_end_is_never_a_handoff_point() -> None:
    """Catch a subscriber up past a forced release to the next natural boundary."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    producer.emit(LINE)
    producer.force(2 * LINE + 4)
    await _until(lambda: first.position == 2 * LINE + 4)

    joiner = _subscribe(registry, LINE + 4, producer, reader)
    await _settle()
    assert reader.calls == []
    producer.emit(4 * LINE)
    await _until(lambda: joiner.position == 4 * LINE)

    assert reader.calls == [(LINE + 4, 4 * LINE)]
    assert joiner.text == RAW[LINE + 4 : 4 * LINE]
    await first.leave()
    await joiner.leave()


async def test_a_stalled_subscriber_neither_delays_others_nor_loses_text() -> None:
    """Drop a subscriber that stopped reading, and catch it up once it reads again."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    stalled = _subscribe(registry, 0, producer, reader)
    reading = _subscribe(registry, 0, producer, reader)
    await _settle()
    stalled.reading.clear()
    producer.emit(LINE)
    await _until(lambda: stalled.position == reading.position == LINE)

    releases = SUBSCRIBER_BACKLOG + 5
    for index in range(2, releases + 2):
        producer.emit(index * LINE)
        await _until(lambda index=index: reading.position == index * LINE)
    frontier = (releases + 1) * LINE
    assert stalled.position == LINE

    stalled.reading.set()
    await _until(lambda: stalled.position == frontier)
    producer.emit(frontier + LINE)
    await _until(lambda: stalled.position == frontier + LINE)

    caught_up_from = (SUBSCRIBER_BACKLOG + 1) * LINE
    assert reader.calls == [(caught_up_from, frontier)]
    assert stalled.text == reading.text == RAW[: frontier + LINE]
    await stalled.leave()
    await reading.leave()


async def test_a_lagging_subscriber_catches_up_to_the_final_frontier_at_the_end() -> (
    None
):
    """Read what a dropped subscriber missed once the follow has ended, then end it."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    stalled = _subscribe(registry, 0, producer, reader)
    await _settle()
    stalled.reading.clear()
    producer.emit(LINE)
    await _until(lambda: stalled.position == LINE)
    releases = SUBSCRIBER_BACKLOG + 3
    for index in range(2, releases + 2):
        producer.emit(index * LINE)
    producer.finish()
    await producer.ended.wait()

    stalled.reading.set()
    await _until(lambda: stalled.finished)

    frontier = (releases + 1) * LINE
    assert reader.calls == [((SUBSCRIBER_BACKLOG + 1) * LINE, frontier)]
    assert stalled.text == RAW[:frontier]
    producer.release_end.set()


async def test_a_producer_crash_ends_every_subscriber_after_catching_it_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End each subscriber once it has the frontier's text, and log the crash."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 4, producer, reader)
    joiner = _subscribe(registry, 0, producer, reader)
    await _settle()

    with caplog.at_level(logging.ERROR):
        producer.fail()
        await _until(lambda: first.finished and joiner.finished)

    assert reader.calls == [(0, 4)]
    assert joiner.text == RAW[:4]
    assert first.text == ""
    assert "the producer broke" in caplog.text


async def test_a_failed_catch_up_ends_only_that_subscriber() -> None:
    """End the subscriber whose catch-up failed and keep feeding the others."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    producer.emit(2 * LINE)
    await _until(lambda: first.position == 2 * LINE)

    reader.fail = True
    joiner = _subscribe(registry, 0, producer, reader)
    await _until(lambda: joiner.finished)
    producer.emit(3 * LINE)
    await _until(lambda: first.position == 3 * LINE)

    assert joiner.received == []
    assert first.text == RAW[: 3 * LINE]
    await first.leave()


async def test_a_join_racing_the_last_leave_gets_a_fresh_follow() -> None:
    """Start a new follow for a subscriber arriving while the old one tears down."""
    registry, reader = LogFollowRegistry(), RangeReader()
    producer = ScriptedProducer(slow_cancel=True)
    first = _subscribe(registry, 0, producer, reader)
    await _settle()

    leaving = asyncio.create_task(first.leave())
    await _until(lambda: producer.cancelled == 1)
    joiner = _subscribe(registry, 0, producer, reader)
    await _until(lambda: producer.starts == [0, 0])
    await leaving
    producer.emit(LINE)
    await _until(lambda: joiner.position == LINE)

    assert producer.starts == [0, 0]
    assert joiner.text == RAW[:LINE]
    await joiner.leave()
    assert producer.cancelled == len(producer.starts)


async def test_a_join_after_the_end_marker_gets_a_fresh_follow() -> None:
    """Treat a follow that sent its end marker as gone, even before its task ends."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    first = _subscribe(registry, 0, producer, reader)
    await _settle()
    producer.finish()
    await producer.ended.wait()
    await _until(lambda: first.finished)

    joiner = _subscribe(registry, 0, producer, reader)
    await _until(lambda: producer.starts == [0, 0])
    producer.release_end.set()
    producer.emit(LINE)
    await _until(lambda: joiner.position == LINE)

    assert joiner.text == RAW[:LINE]
    await joiner.leave()


async def test_one_leaving_subscriber_does_not_disturb_the_rest() -> None:
    """Keep the follow running for the subscribers that stay."""
    registry, producer, reader = LogFollowRegistry(), ScriptedProducer(), RangeReader()
    leaving = _subscribe(registry, 0, producer, reader)
    staying = _subscribe(registry, 0, producer, reader)
    await _settle()
    producer.emit(LINE)
    await _until(lambda: leaving.position == staying.position == LINE)

    await leaving.leave()
    producer.emit(2 * LINE)
    await _until(lambda: staying.position == 2 * LINE)

    assert producer.cancelled == 0
    assert staying.text == RAW[: 2 * LINE]
    await staying.leave()
    assert producer.cancelled == 1
