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

"""Serve Nomad's allocation and log-follow endpoints from a background thread.

The stub runs on its own event loop in its own thread. python-nomad is a
synchronous ``requests`` client, so an allocation lookup made from the event
loop under test blocks that loop until it is answered: a stub sharing the loop
could never answer it. A separate loop also keeps the stub's frame timing
independent of how busy the loop under test is, which is what a latency
assertion against it needs.
"""

__all__ = ["NomadLogStub"]

import asyncio
import json
import threading
import time
from base64 import b64encode
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any, TypeVar

from aiohttp import web

from app.tasks.models import TaskLogType

_SHUTDOWN_TIMEOUT = 1.0

_T = TypeVar("_T")


def _locate(files: list[bytearray], offset: int) -> tuple[int, int]:
    """Map a cumulative request offset to a file and a position within it.

    Nomad walks the request ``offset`` across a step's rotated files, moving on
    to the next file while the offset reaches past the current one.

    :param files: The log type's files, oldest first.
    :param offset: The cumulative offset the request asked for.
    :return: The file index and the position within that file.
    """
    index = 0
    while index < len(files) - 1 and offset >= len(files[index]):
        offset -= len(files[index])
        index += 1
    return index, min(offset, len(files[index]))


class NomadLogStub:
    """Emit one running step's stdout and stderr through Nomad's log API.

    Every follow is served from its requested ``offset``: whatever was written
    past it arrives as one frame, then each new write as its own frame, then
    ``{}`` heartbeats once emission is done, the way Nomad keeps a finished
    task's follow open. A ``follow=false`` request sends what was written past
    its offset and closes. The allocation reports the step ``running`` until
    :meth:`emit` or :meth:`finish` ends it and ``dead`` afterwards.

    Each log type is a list of files, as Nomad rotates them: a request offset is
    cumulative across the files, while a frame's ``Offset`` is its position
    within the file its ``File`` names.

    :param step: The Nomad task the allocation runs and the follows read.
    :param line_count: Lines :meth:`emit` writes to each log type.
    :param frame_interval: Seconds between two emitted lines.
    :param heartbeat_interval: Seconds a follow waits for data before sending a
        heartbeat while the step is running.
    :param done_heartbeat_interval: Seconds between heartbeats once the step is
        dead, kept short so the executor's empty-frame recheck fires quickly.
    :param allocation_delay: Seconds the allocation list takes to answer.
    """

    alloc_id = "alloc-live"
    job_id = "job-live"
    eval_id = "eval-live"

    def __init__(
        self,
        step: str = "run-script",
        *,
        line_count: int = 8,
        frame_interval: float = 0.4,
        heartbeat_interval: float = 1.0,
        done_heartbeat_interval: float = 0.1,
        allocation_delay: float = 0.0,
    ) -> None:
        self.step = step
        self.line_count = line_count
        self.frame_interval = frame_interval
        self.heartbeat_interval = heartbeat_interval
        self.done_heartbeat_interval = done_heartbeat_interval
        self.allocation_delay = allocation_delay
        self.not_started_steps: set[str] = set()
        self.emitted: dict[tuple[str, str], float] = {}
        self.open_follows = 0
        self.peak_follows = 0
        self.not_found_served = 0
        self.non_follow_requests = 0
        self.open_non_follows = 0
        self._files = {log_type.value: [bytearray()] for log_type in TaskLogType}
        self._follow_transports: set[asyncio.BaseTransport] = set()
        self._done = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._changed = asyncio.Condition()
        self._runner: web.AppRunner | None = None
        self._port = 0

    @property
    def endpoint(self) -> str:
        """Return the Nomad endpoint to configure the executor with.

        :return: The loopback URL the stub listens on.
        """
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        """Start the stub's thread and bind it to an ephemeral loopback port."""
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result()

    def stop(self) -> None:
        """Release the port, abandon open follows and join the stub's thread."""
        asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    async def _on_stub_loop(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        """Run ``coroutine`` on the stub's loop and await it from the caller's.

        :param coroutine: The coroutine to run.
        :return: What the coroutine returns.
        """
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        )

    async def emit(self) -> None:
        """Write every line to both log types, then mark the step dead."""
        await self._on_stub_loop(self._emit())

    async def write(self, log_type: TaskLogType, data: bytes) -> None:
        """Append ``data`` verbatim to one log type's current file.

        :param log_type: The log type to write to.
        :param data: The raw bytes, which need not end a line.
        """
        await self._on_stub_loop(self._write(log_type, data))

    async def rotate(self, log_type: TaskLogType) -> None:
        """Start a new file for one log type, as Nomad does at its size limit.

        :param log_type: The log type to rotate.
        """
        await self._on_stub_loop(self._rotate(log_type))

    async def finish(self) -> None:
        """Mark the step dead without writing anything more."""
        await self._on_stub_loop(self._finish())

    async def drop_follows(self) -> None:
        """Close every open follow's connection abruptly."""
        await self._on_stub_loop(self._drop_follows())

    async def wait_for_follows(self, count: int) -> None:
        """Wait until ``count`` follows are open at once.

        :param count: The number of concurrently open follows to wait for.
        """
        await self._on_stub_loop(self._wait_until(lambda: self.open_follows >= count))

    async def wait_for_not_found(self, count: int) -> None:
        """Wait until ``count`` not-started 404s have been answered in total.

        :param count: The number of 404 answers to wait for.
        """
        await self._on_stub_loop(
            self._wait_until(lambda: self.not_found_served >= count)
        )

    async def _wait_until(self, predicate: Callable[[], bool]) -> None:
        """Block on the stub's loop until ``predicate`` holds.

        :param predicate: The condition over the stub's counters to wait for.
        """
        async with self._changed:
            await self._changed.wait_for(predicate)

    async def _start(self) -> None:
        """Build the application and bind it, on the stub's loop."""
        application = web.Application()
        application.router.add_get("/v1/allocations", self._allocations)
        application.router.add_get("/v1/client/fs/logs/{alloc_id}", self._logs)
        self._runner = web.AppRunner(application, shutdown_timeout=_SHUTDOWN_TIMEOUT)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self._port = self._runner.addresses[0][1]

    async def _stop(self) -> None:
        """Clean the runner up, on the stub's loop."""
        if self._runner is not None:
            await self._runner.cleanup()

    async def _emit(self) -> None:
        """Append one line per log type every ``frame_interval``, on the stub's loop."""
        for index in range(self.line_count):
            text = f"line-{index}"
            async with self._changed:
                for log_type, files in self._files.items():
                    files[-1].extend(f"{text}\n".encode())
                    self.emitted[(log_type, text)] = time.monotonic()
                self._changed.notify_all()
            await asyncio.sleep(self.frame_interval)
        await self._finish()

    async def _write(self, log_type: TaskLogType, data: bytes) -> None:
        """Append ``data`` to the log type's current file, on the stub's loop.

        :param log_type: The log type to write to.
        :param data: The raw bytes.
        """
        async with self._changed:
            self._files[log_type.value][-1].extend(data)
            self._changed.notify_all()

    async def _rotate(self, log_type: TaskLogType) -> None:
        """Start a new file for the log type, on the stub's loop.

        :param log_type: The log type to rotate.
        """
        async with self._changed:
            self._files[log_type.value].append(bytearray())
            self._changed.notify_all()

    async def _finish(self) -> None:
        """Mark the step dead, on the stub's loop."""
        async with self._changed:
            self._done = True
            self._changed.notify_all()

    async def _drop_follows(self) -> None:
        """Close every open follow's transport, on the stub's loop."""
        for transport in list(self._follow_transports):
            transport.close()

    def allocation(self) -> dict[str, Any]:
        """Return the allocation as Nomad's allocation list reports it.

        :return: The allocation, its step ``running`` until emission is done.
        """
        state = {
            "State": "running",
            "StartedAt": "2026-01-01T00:00:00Z",
        }
        if self._done:
            state |= {"State": "dead", "FinishedAt": "2026-01-01T00:01:00Z"}
        return {
            "ID": self.alloc_id,
            "JobID": self.job_id,
            "EvalID": self.eval_id,
            "TaskStates": {self.step: state}
            | {
                step: {"State": "pending", "StartedAt": None}
                for step in self.not_started_steps
            },
        }

    async def _allocations(self, _request: web.Request) -> web.Response:
        """Answer the allocation list with the one allocation, after the delay.

        :return: The JSON list response.
        """
        await asyncio.sleep(self.allocation_delay)
        return web.json_response([self.allocation()])

    async def _logs(self, request: web.Request) -> web.StreamResponse:
        """Serve one log type of one step from the requested offset.

        A step listed in :attr:`not_started_steps` answers a complete 404, as
        Nomad does before the task has started.

        :param request: The log request.
        :return: The streamed log, or the 404.
        """
        if request.query["task"] in self.not_started_steps:
            async with self._changed:
                self.not_found_served += 1
                self._changed.notify_all()
            return web.json_response({"error": "task not started"}, status=404)
        name = f"{request.query['task']}.{request.query['type']}"
        files = self._files[request.query["type"]]
        offset = int(request.query.get("offset", 0))
        follow = request.query.get("follow") == "true"
        transport = request.transport
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        async with self._changed:
            if follow:
                self.open_follows += 1
                self.peak_follows = max(self.peak_follows, self.open_follows)
                if transport is not None:
                    self._follow_transports.add(transport)
            else:
                self.non_follow_requests += 1
                self.open_non_follows += 1
            self._changed.notify_all()
        try:
            if follow:
                await self._follow(response, name, files, offset)
            else:
                await self._read(response, name, files, offset)
        except ConnectionResetError:
            pass
        finally:
            async with self._changed:
                if follow:
                    self.open_follows -= 1
                    self._follow_transports.discard(transport)
                else:
                    self.open_non_follows -= 1
                self._changed.notify_all()
        return response

    @staticmethod
    async def _write_frame(
        response: web.StreamResponse, name: str, index: int, end: int, data: bytes
    ) -> None:
        """Write one data frame the way Nomad frames a file's bytes.

        :param response: The prepared response.
        :param name: The ``<task>.<type>`` prefix of the log's file names.
        :param index: The file the bytes come from.
        :param end: The position within that file where the bytes end.
        :param data: The raw bytes.
        """
        frame = {
            "File": f"{name}.{index}",
            "Offset": end,
            "Data": b64encode(data).decode(),
        }
        await response.write(json.dumps(frame).encode())

    async def _read(
        self,
        response: web.StreamResponse,
        name: str,
        files: list[bytearray],
        offset: int,
    ) -> None:
        """Write everything past ``offset``, one frame per file, then return.

        :param response: The prepared response.
        :param name: The ``<task>.<type>`` prefix of the log's file names.
        :param files: The log type's files.
        :param offset: The cumulative offset the request starts from.
        """
        async with self._changed:
            index, position = _locate(files, offset)
            pieces = [
                (current, len(files[current]), bytes(files[current][start:]))
                for current, start in [
                    (current, position if current == index else 0)
                    for current in range(index, len(files))
                ]
            ]
        for current, end, data in pieces:
            if data:
                await self._write_frame(response, name, current, end, data)

    async def _follow(
        self,
        response: web.StreamResponse,
        name: str,
        files: list[bytearray],
        offset: int,
    ) -> None:
        """Write frames past ``offset`` until the client goes away.

        A follow sends the current file to its end before it moves on to the
        next one, where frame offsets start again from 0.

        :param response: The prepared follow response.
        :param name: The ``<task>.<type>`` prefix of the log's file names.
        :param files: The log type's files, appended to by the writers.
        :param offset: The cumulative offset the follow starts from.
        """
        index, position = _locate(files, offset)
        while True:
            async with self._changed:
                if (
                    len(files[index]) == position
                    and index == len(files) - 1
                    and not self._done
                ):
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._changed.wait(), self.heartbeat_interval
                        )
                written = bytes(files[index][position:])
                rotated = index < len(files) - 1
                done = self._done
            if written:
                position += len(written)
                await self._write_frame(response, name, index, position, written)
            elif rotated:
                index, position = index + 1, 0
            else:
                await response.write(b"{}")
                if done:
                    await asyncio.sleep(self.done_heartbeat_interval)
