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

"""Drive an ASGI app directly so a response body can be observed mid-flight.

Both HTTP clients the suite otherwise uses buffer the whole response before
returning it: ``httpx.ASGITransport`` runs the app to completion collecting body
parts, and ``fastapi.testclient.TestClient`` writes every body message into a
``BytesIO`` through its portal. Neither can observe a first chunk, act, and then
read the rest, so a test that has to interleave with a live response (fire a
settings rebind while a stream is open, then assert the stream survived it)
cannot be written on either.

This driver calls the app itself and surfaces each ``http.response.body``
message as the app sends it, giving the test a synchronization point in the
middle of a streaming response with no server and no extra thread.
"""

__all__ = ["ASGIStream", "asgi_stream"]

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

_END_OF_BODY = object()

_STARTUP_TIMEOUT = 10
_CHUNK_TIMEOUT = 10


class ASGIStream:
    """Expose an in-flight ASGI response: its start message and its body chunks."""

    def __init__(
        self,
        task: asyncio.Task,
        queue: asyncio.Queue,
        start: dict[str, Any],
    ) -> None:
        self._task = task
        self._queue = queue
        self._start = start

    @property
    def status_code(self) -> int:
        """Return the status code carried by the ``http.response.start`` message.

        :return: The response status code.
        """
        return self._start["status"]

    @property
    def headers(self) -> dict[str, str]:
        """Return the response headers, lowercased, from the start message.

        :return: A mapping of header name to value.
        """
        return {
            key.decode().lower(): value.decode()
            for key, value in self._start.get("headers", [])
        }

    async def next_chunk(self) -> bytes | None:
        """Return the next body chunk, or ``None`` once the body is complete.

        :return: The next ``http.response.body`` payload, or ``None`` at the end.
        :raises TimeoutError: If no chunk arrives within the harness timeout.
        """
        chunk = await asyncio.wait_for(self._queue.get(), timeout=_CHUNK_TIMEOUT)
        if chunk is _END_OF_BODY:
            return None
        return chunk

    async def drain(self) -> bytes:
        """Read the response to completion and return the concatenated body.

        :return: Every remaining body chunk, joined in arrival order.
        """
        chunks: list[bytes] = []
        while (chunk := await self.next_chunk()) is not None:
            chunks.append(chunk)
        await self._task
        return b"".join(chunks)


class _ResponseCollector:
    """Hold the ``receive`` / ``send`` pair driving one in-flight ASGI response."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        self.start_message: dict[str, Any] = {}
        self._request_sent = False

    async def receive(self) -> dict[str, Any]:
        """Deliver the empty request body once, then wait out the response.

        Starlette's ``BaseHTTPMiddleware`` rejects a second ``http.request``, so
        after the body this mirrors what a real server does: block until the
        response is complete, then report the disconnect.

        :return: The next ASGI request-side message.
        """
        if self._request_sent:
            await self.completed.wait()
            return {"type": "http.disconnect"}
        self._request_sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: Mapping[str, Any]) -> None:
        """Record the response start message and queue each body chunk.

        :param message: The ASGI response-side message the app sent.
        """
        if message["type"] == "http.response.start":
            self.start_message.update(message)
            self.started.set()
        elif message["type"] == "http.response.body":
            if body := message.get("body", b""):
                await self.queue.put(body)
            if not message.get("more_body", False):
                self.completed.set()
                await self.queue.put(_END_OF_BODY)


def _build_scope(
    app: Any,
    method: str,
    path: str,
    query_string: bytes,
    headers: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Build a minimal HTTP ASGI scope for ``method path``.

    :param app: The ASGI application the scope belongs to.
    :param method: The HTTP method.
    :param path: The request path, already url-decoded.
    :param query_string: The raw query string.
    :param headers: Request headers to send, if any.
    :return: The ASGI connection scope.
    """
    sent = {"host": "testserver"} | {
        key.lower(): value for key, value in (headers or {}).items()
    }
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": [(key.encode(), value.encode()) for key, value in sent.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "app": app,
        "state": {},
    }


@asynccontextmanager
async def asgi_stream(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    query_string: bytes = b"",
    headers: Mapping[str, str] | None = None,
) -> AsyncGenerator[ASGIStream]:
    """Send one request into ``app`` and yield its response while it is still open.

    Control returns to the caller as soon as the app has sent its
    ``http.response.start`` message, which for a ``StreamingResponse`` is before
    the body generator has produced anything, so the caller can act on the
    application state the open response is holding, then read the body.

    An application error raised before the response start is re-raised here
    rather than surfacing as a bare startup timeout.

    :param app: The ASGI application to drive.
    :param path: The request path.
    :param method: The HTTP method. Defaults to ``GET``.
    :param query_string: The raw query string. Defaults to empty.
    :param headers: Request headers to send, if any.
    :return: The in-flight response.
    :raises TimeoutError: If the app sends no response start within the harness
        timeout.
    """
    collector = _ResponseCollector()
    scope = _build_scope(app, method, path, query_string, headers)
    task = asyncio.ensure_future(app(scope, collector.receive, collector.send))
    try:
        waiter = asyncio.ensure_future(collector.started.wait())
        done, _pending = await asyncio.wait(
            {waiter, task},
            timeout=_STARTUP_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
        waiter.cancel()
        if task in done:
            task.result()
        if not collector.started.is_set():
            raise TimeoutError(f"{method} {path} sent no http.response.start")

        yield ASGIStream(task, collector.queue, collector.start_message)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
