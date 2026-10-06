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
from contextlib import suppress
from typing import Any

from aiohttp import web

from app.tasks.models import TaskLogType

_SHUTDOWN_TIMEOUT = 1.0


class NomadLogStub:
    """Emit one running step's stdout and stderr through Nomad's follow API.

    Every follow is served from its requested ``offset``: whatever was written
    past it arrives as one frame, then each new write as its own frame, then
    ``{}`` heartbeats once emission is done, the way Nomad keeps a finished
    task's follow open. The allocation reports the step ``running`` until
    :meth:`emit` finishes and ``dead`` afterwards.

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
        self.not_found_served = 0
        self._data = {log_type.value: bytearray() for log_type in TaskLogType}
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

    async def emit(self) -> None:
        """Write every line to both log types, then mark the step dead.

        Awaitable from the loop under test; the writes themselves run on the
        stub's loop.
        """
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(self._emit(), self._loop)
        )

    async def wait_for_follows(self, count: int) -> None:
        """Wait until ``count`` follows are open at once.

        :param count: The number of concurrently open follows to wait for.
        """
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(self._wait_for_follows(count), self._loop)
        )

    async def wait_for_not_found(self, count: int) -> None:
        """Wait until ``count`` not-started 404s have been answered in total.

        :param count: The number of 404 answers to wait for.
        """
        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                self._wait_for_not_found(count), self._loop
            )
        )

    async def _wait_for_follows(self, count: int) -> None:
        """Block on the stub's loop until ``count`` follows are open at once.

        :param count: The number of concurrently open follows to wait for.
        """
        async with self._changed:
            await self._changed.wait_for(lambda: self.open_follows >= count)

    async def _wait_for_not_found(self, count: int) -> None:
        """Block on the stub's loop until ``count`` 404s have been answered.

        :param count: The number of 404 answers to wait for.
        """
        async with self._changed:
            await self._changed.wait_for(lambda: self.not_found_served >= count)

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
                for log_type, data in self._data.items():
                    data.extend(f"{text}\n".encode())
                    self.emitted[(log_type, text)] = time.monotonic()
                self._changed.notify_all()
            await asyncio.sleep(self.frame_interval)
        async with self._changed:
            self._done = True
            self._changed.notify_all()

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
        """Follow one log type of one step from the requested offset.

        A step listed in :attr:`not_started_steps` answers a complete 404, as
        Nomad does before the task has started.

        :param request: The follow request.
        :return: The streamed follow, or the 404.
        """
        if request.query["task"] in self.not_started_steps:
            async with self._changed:
                self.not_found_served += 1
                self._changed.notify_all()
            return web.json_response({"error": "task not started"}, status=404)
        data = self._data[request.query["type"]]
        offset = int(request.query.get("offset", 0))
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        async with self._changed:
            self.open_follows += 1
            self._changed.notify_all()
        try:
            await self._follow(response, data, offset)
        except ConnectionResetError:
            pass
        finally:
            self.open_follows -= 1
        return response

    async def _follow(
        self, response: web.StreamResponse, data: bytearray, offset: int
    ) -> None:
        """Write frames for ``data`` past ``offset`` until the client goes away.

        :param response: The prepared follow response.
        :param data: The log type's buffer, appended to by :meth:`_emit`.
        :param offset: The byte offset the follow starts from.
        """
        while True:
            async with self._changed:
                if len(data) == offset and not self._done:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._changed.wait(), self.heartbeat_interval
                        )
                written = bytes(data[offset:])
                done = self._done
            if written:
                offset += len(written)
                frame = {"Offset": offset, "Data": b64encode(written).decode()}
                await response.write(json.dumps(frame).encode())
            else:
                await response.write(b"{}")
                if done:
                    await asyncio.sleep(self.done_heartbeat_interval)
