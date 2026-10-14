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

"""Share one upstream live-log follow among every viewer of a running step.

A follow is keyed by whatever identifies the text it produces, so viewers
whose requests resolve the same key read one upstream request and one
anonymization pass. Each viewer still starts at its own offset and reads at
its own pace: one that starts behind the shared follow, or falls too far
behind it, is caught up by a bounded read of the range it is missing and
then rejoins the shared feed at a line boundary.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Hashable
from contextlib import aclosing
from typing import NamedTuple, Protocol

from app.tasks.models import TaskLog

logger = logging.getLogger(__name__)

#: Most emissions buffered for one subscriber of one stream before it is
#: dropped from the shared feed and re-enters through a catch-up read.
SUBSCRIBER_BACKLOG = 64


class LogSink(Protocol):
    """Receive the log lines one producer emits, ``msg=None`` last."""

    async def put(self, item: TaskLog) -> None:
        """Accept one log line.

        :param item: The log line, or the ``msg=None`` end marker.
        """


class LogCatchUpError(Exception):
    """Signal that a catch-up read could not deliver its whole range."""


#: Reads the raw range ``[start, end)`` as log lines, anonymized afresh.
RangeReader = Callable[[int, int], AsyncGenerator[TaskLog]]

#: Runs the upstream follow into a shared follow from a raw start offset.
Producer = Callable[["SharedLogFollow", int], Awaitable[None]]


class _Emission(NamedTuple):
    """Carry one producer release with the raw offsets that bound it.

    :param start: The cumulative raw offset the release starts at.
    :param log: The release, whose ``offset`` is where it ends.
    :param aligned: Whether it ends on a natural line boundary, so a
        catch-up may hand a subscriber off there.
    """

    start: int
    log: TaskLog
    aligned: bool


class _Subscriber:
    """Track one subscriber's place in a shared follow.

    :param position: The raw offset the subscriber's text has reached.
    """

    __slots__ = ("buffer", "position", "wake")

    def __init__(self, position: int) -> None:
        self.position = position
        self.buffer: deque[_Emission] = deque()
        self.wake = asyncio.Event()


class SharedLogFollow:
    """Fan one producer's releases out to every subscriber's own buffer.

    The follow is the producer's sink. :meth:`put` never waits on a subscriber,
    so one that stops reading cannot hold back the producer or the others: once
    its buffer is full it is dropped from delivery, and it catches up through a
    read of the range it missed when it reads again.

    :param start_offset: The raw offset the producer starts reading at.
    """

    def __init__(self, start_offset: int) -> None:
        self.start_offset = start_offset
        self.frontier = start_offset
        self.frontier_aligned = start_offset == 0
        self.ended = False
        self.closing = False
        self.members = 0
        self._forced = False
        self._delivering: set[_Subscriber] = set()
        self._subscribers: set[_Subscriber] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self, produce: Producer, on_done: Callable[[], None]) -> None:
        """Run the producer in a task of its own.

        :param produce: The upstream follow to run into this sink.
        :param on_done: Called once the producer's task has finished.
        """
        self._task = asyncio.create_task(self._produce(produce))
        self._task.add_done_callback(lambda _task: on_done())

    async def stop(self) -> None:
        """Cancel the producer and wait for it to unwind."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _produce(self, produce: Producer) -> None:
        """Run the producer, ending the follow however it finishes.

        :param produce: The upstream follow to run into this sink.
        """
        try:
            await produce(self, self.start_offset)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Shared live log follow failed at offset=%s", self.frontier
            )
        finally:
            self._end()

    def mark_forced(self) -> None:
        """Flag the next release as a forced flush, never a handoff point."""
        self._forced = True

    async def put(self, item: TaskLog) -> None:
        """Record one release and hand it to every delivering subscriber.

        :param item: The release, or the ``msg=None`` end marker.
        """
        if item.msg is None:
            self._end()
            return
        aligned = not self._forced
        self._forced = False
        emission = _Emission(self.frontier, item, aligned)
        self.frontier = item.offset
        self.frontier_aligned = aligned
        for subscriber in list(self._delivering):
            if len(subscriber.buffer) >= SUBSCRIBER_BACKLOG:
                self._delivering.discard(subscriber)
            else:
                subscriber.buffer.append(emission)
            subscriber.wake.set()

    def _end(self) -> None:
        """Mark the follow ended and wake every subscriber."""
        self.ended = True
        for subscriber in self._subscribers:
            subscriber.wake.set()

    async def follow_from(
        self, position: int, read_range: RangeReader
    ) -> AsyncGenerator[TaskLog]:
        """Yield one subscriber's text from ``position`` until the follow ends.

        A failed catch-up read ends this subscriber's text and no other's.

        :param position: The raw offset the subscriber starts at.
        :param read_range: Reads a raw range the shared feed cannot supply.
        :return: The subscriber's log lines, in order, without gap or repeat.
        """
        subscriber = _Subscriber(position)
        self._subscribers.add(subscriber)
        try:
            async for log in self._deliver(subscriber, read_range):
                yield log
        except LogCatchUpError:
            return
        finally:
            self._subscribers.discard(subscriber)
            self._delivering.discard(subscriber)

    async def _deliver(
        self, subscriber: _Subscriber, read_range: RangeReader
    ) -> AsyncGenerator[TaskLog]:
        """Feed one subscriber from its buffer and from catch-up reads.

        :param subscriber: The subscriber to feed.
        :param read_range: Reads a raw range the shared feed cannot supply.
        :return: The subscriber's log lines.
        """
        while True:
            subscriber.wake.clear()
            if subscriber.buffer:
                emission = subscriber.buffer.popleft()
                async for log in self._take(subscriber, emission, read_range):
                    yield log
            elif self.ended:
                async for log in self._catch_up(subscriber, self.frontier, read_range):
                    yield log
                return
            elif subscriber not in self._delivering:
                frontier, aligned = self.frontier, self.frontier_aligned
                self._delivering.add(subscriber)
                if aligned:
                    async for log in self._catch_up(subscriber, frontier, read_range):
                        yield log
            else:
                await subscriber.wake.wait()

    async def _take(
        self, subscriber: _Subscriber, emission: _Emission, read_range: RangeReader
    ) -> AsyncGenerator[TaskLog]:
        """Deliver one buffered release, or the catch-up read that replaces it.

        :param subscriber: The subscriber the release was buffered for.
        :param emission: The release.
        :param read_range: Reads a raw range the shared feed cannot supply.
        :return: The log lines to deliver for this release.
        """
        end = emission.log.offset
        if end <= subscriber.position:
            return
        if emission.start == subscriber.position:
            subscriber.position = end
            yield emission.log
        elif emission.aligned:
            async for log in self._catch_up(subscriber, end, read_range):
                yield log

    @staticmethod
    async def _catch_up(
        subscriber: _Subscriber, end: int, read_range: RangeReader
    ) -> AsyncGenerator[TaskLog]:
        """Read the subscriber's position up to ``end`` and advance it there.

        :param subscriber: The subscriber to catch up.
        :param end: The raw offset to read up to.
        :param read_range: Reads a raw range the shared feed cannot supply.
        :return: The log lines of the range.
        """
        if subscriber.position >= end:
            return
        async with aclosing(read_range(subscriber.position, end)) as logs:
            async for log in logs:
                yield log
        subscriber.position = end


class LogFollowRegistry:
    """Hold the shared follows one executor is running, one per key."""

    def __init__(self) -> None:
        self._follows: dict[Hashable, SharedLogFollow] = {}

    async def subscribe(
        self,
        key: Hashable,
        start_offset: int,
        produce: Producer,
        read_range: RangeReader,
    ) -> AsyncGenerator[TaskLog]:
        """Yield the text of the follow for ``key`` from ``start_offset``.

        Attaches to the live follow for ``key``, or starts one from
        ``start_offset`` when there is none, or only one that has ended or is
        being torn down. The last subscriber to leave cancels the producer.

        :param key: What identifies the text the follow produces.
        :param start_offset: The raw offset this subscriber starts at.
        :param produce: The upstream follow a new shared follow runs.
        :param read_range: Reads a raw range for this subscriber's catch-up.
        :return: This subscriber's log lines, ending when the follow does.
        """
        follow = self._follows.get(key)
        if follow is None or follow.ended or follow.closing:
            follow = SharedLogFollow(start_offset)
            self._follows[key] = follow
            follow.start(produce, lambda: self._forget(key, follow))
        follow.members += 1
        try:
            async with aclosing(follow.follow_from(start_offset, read_range)) as logs:
                async for log in logs:
                    yield log
        finally:
            follow.members -= 1
            if not follow.members:
                follow.closing = True
                self._forget(key, follow)
                await follow.stop()

    def _forget(self, key: Hashable, follow: SharedLogFollow) -> None:
        """Remove ``follow`` from the registry unless a successor replaced it.

        :param key: The follow's key.
        :param follow: The follow to remove.
        """
        if self._follows.get(key) is follow:
            del self._follows[key]
