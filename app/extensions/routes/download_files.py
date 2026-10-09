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

"""Define routes for listing and downloading files from tasks."""

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from starlette.responses import StreamingResponse
from starlette.types import Send

from app.core.requests import as_json_object
from app.extensions.deps import (
    ApiCurrentUser,
    get_task_history,
    IsApiAuthenticated,
    TaskAPI,
    TasksClient,
)
from app.extensions.routes import STREAMING_PROXY_HEADERS
from app.tasks.models import FileMetadata, TaskHistoryResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])

_ERROR_RESPONSE_HEADERS = frozenset(
    {key.lower() for key in STREAMING_PROXY_HEADERS} | {"content-disposition"}
)


class ErrorPrimingStreamingResponse(StreamingResponse):
    """Prime the body iterator before committing the response status.

    Standard StreamingResponse sends HTTP 200 before iterating the body. This
    subclass pulls the first chunk first so any upstream ``HTTPException`` raises
    before ``http.response.start``. That lets ExceptionMiddleware turn the error
    into a real status response instead of a misleading 200 with an empty body.

    Tradeoff: time-to-first-byte waits on the first upstream chunk (or error)
    before headers are sent. That is intentional for this proxy-timing-sensitive
    download route — correct status on upstream rejection matters more than
    speculative early headers.

    FastAPI installs ``@app.exception_handler(500)`` on ServerErrorMiddleware,
    which only sees non-``HTTPException`` failures. Upstream 5xx arrives as
    ``HTTPException``, so this class logs those explicitly before re-raising —
    otherwise downloads can fail silently from an on-call/observability
    standpoint. Proxy/disposition headers from this response are copied onto the
    raised ``HTTPException`` so they survive ExceptionMiddleware.
    """

    async def stream_response(self, send: Send) -> None:
        """Prime the body iterator before sending the ASGI start message.

        :param send: ASGI send callable used to emit response start, body, and
            end messages.
        """
        body_iter = aiter(self.body_iterator)

        try:
            first_chunk = await anext(body_iter)
        except HTTPException as exc:
            self._prepare_primed_http_exception(exc)
            raise
        except StopAsyncIteration:
            self.body_iterator = body_iter
        else:

            async def primed() -> AsyncGenerator[Any, None]:
                yield first_chunk
                async for chunk in body_iter:
                    yield chunk

            self.body_iterator = primed()
        await super().stream_response(send)

    def _prepare_primed_http_exception(self, exc: HTTPException) -> None:
        """Log 5xx and attach proxy/disposition headers before re-raise.

        :param exc: Upstream ``HTTPException`` raised by the first pull; the
            preserved response headers are merged into ``exc.headers``.
        """
        if exc.status_code >= http_status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error(
                "Upstream error while priming file download stream:",
                exc_info=exc,
            )
        preserve = {
            key: value
            for key, value in self.headers.items()
            if key in _ERROR_RESPONSE_HEADERS
        }
        if preserve:
            exc.headers = {**(exc.headers or {}), **preserve}


@router.get(
    "/{task_history_id}",
    dependencies=[IsApiAuthenticated],
)
async def list_task_history_files(
    request: Request,  # noqa: ARG001
    task_history: Annotated[TaskHistoryResponse, Depends(get_task_history)],
    tasks_api: TaskAPI,
) -> dict[str, FileMetadata]:
    """Return files available for the given task history."""
    try:
        listing = await tasks_api.get(f"/history/{task_history.id}/files/")
        return {
            name: FileMetadata.model_validate(metadata)
            for name, metadata in (
                as_json_object(listing) if listing is not None else {}
            ).items()
        }
    except HTTPException as exc:
        if exc.status_code in (
            http_status.HTTP_400_BAD_REQUEST,
            http_status.HTTP_409_CONFLICT,
        ):
            return {}
        raise


@router.get("/{task_history_id}/download", dependencies=[IsApiAuthenticated])
async def download_task_history_file(
    request: Request,
    user: ApiCurrentUser,
    task_history: Annotated[TaskHistoryResponse, Depends(get_task_history)],
    tasks_client: TasksClient,
) -> StreamingResponse:
    """Stream a task history's archived file as a binary download.

    Upstream errors raised while priming the stream surface as the real status
    before any response is committed, rather than as a misleading 200 with an
    empty body.

    :param request: Incoming download request; ``path`` selects the archived
        file.
    :param user: Authenticated viewer whose access token authorizes the stream.
    :param task_history: Task history whose archived file is downloaded.
    :param tasks_client: Tasks API client used to list metadata and stream
        bytes.
    :return: Streaming response of the archived file as
        ``application/octet-stream``.
    """
    headers = dict(STREAMING_PROXY_HEADERS)
    path = request.query_params.get("path")
    with tasks_client.auth(user.access_token) as tasks_api:
        if path:
            filename = Path(path.rstrip("/")).name or path
            is_dir = False
            try:
                files = as_json_object(
                    await tasks_api.get(f"/history/{task_history.id}/files/")
                )
                meta = files.get(path) or files.get(path.rstrip("/"))
                if isinstance(meta, dict):
                    is_dir = bool(meta.get("is_dir") or meta.get("isDir"))
            except HTTPException:
                logger.debug(
                    "Could not resolve file metadata for %s", path, exc_info=True
                )
            attachment = f"{filename}.tar.gz" if is_dir else filename
            headers["Content-Disposition"] = f'attachment; filename="{attachment}"'

    return ErrorPrimingStreamingResponse(
        task_history_file_stream(
            tasks_client, task_history.id, request, user.access_token
        ),
        media_type="application/octet-stream",
        headers=headers,
    )


async def task_history_file_stream(
    tasks_client: TasksClient,
    task_history_id: int,
    request: Request,
    access_token: str,
) -> AsyncGenerator[bytes, None]:
    """Stream a task history's archived file content as raw bytes.

    Yields the file payload chunk-by-chunk from ``/history/{id}/file/`` without
    any line buffering, so binary, gzip, and tar payloads pass through intact.

    The payload request is authenticated as the user named by ``access_token``,
    under a context this body enters itself rather than inheriting from the
    route.

    :param tasks_client: The Tasks API client to use for streaming the file.
    :param task_history_id: The ID of the task history whose file is to be streamed.
    :param request: The incoming HTTP request.
    :param access_token: Bearer token authenticating the download as the viewing
        user.
    :return: Raw byte chunks of the file payload (binary-safe).
    """
    with tasks_client.auth(access_token) as tasks_api:
        async for chunk in tasks_api.stream_chunks(
            f"/history/{task_history_id}/file/",
            params=request.query_params,
        ):
            yield chunk
