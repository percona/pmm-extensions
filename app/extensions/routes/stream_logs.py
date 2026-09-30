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

"""Define routes for streaming task logs and execution events (SSE)."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Annotated

from aiohttp import ClientTimeout
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from app.core.requests import as_json_object
from app.core.security import get_internal_token
from app.extensions.deps import (
    ApiCurrentUser,
    get_task_history,
    IsApiAuthenticated,
    TasksClient,
)
from app.extensions.routes import STREAMING_PROXY_HEADERS
from app.tasks.models import TaskHistoryResponse, TaskHistoryStatusEnum

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])


@router.get("/{task_history_id}/execution-events", dependencies=[IsApiAuthenticated])
async def task_execution_events_stream(
    request: Request,
    user: ApiCurrentUser,
    task_history: Annotated[TaskHistoryResponse, Depends(get_task_history)],
    tasks_client: TasksClient,
) -> StreamingResponse:
    """Stream task execution events as server-sent events."""
    return StreamingResponse(
        task_history_events_event_stream(
            tasks_client, task_history.id, request, user.access_token
        ),
        media_type="text/event-stream",
        headers=STREAMING_PROXY_HEADERS,
    )


async def task_history_events_event_stream(
    tasks_client: TasksClient,
    task_history_id: int,
    request: Request,
    access_token: str,
) -> AsyncGenerator[str, None]:
    """Yield incremental execution events for a task history.

    The Tasks API returns events oldest-first; only the tail after the last emitted
    index is processed each poll so cost stays bounded for long runs.

    :param tasks_client: The TaskAPI client for interacting with the Tasks service.
    :type tasks_client: RemoteAPI
    :param task_history_id: The ID of the task history whose execution events to stream.
    :type task_history_id: int
    :param request: The FastAPI request object, used to detect client disconnect.
    :type request: Request
    :param access_token: Bearer token used to authenticate requests to the Tasks API.
    :type access_token: str
    :yield: Execution event payloads and control messages formatted as server-sent events.
    :rtype: str
    """
    emitted_count = 0
    wait_interval = 2
    try:
        with tasks_client.auth(access_token) as tasks_api:
            while True:
                if await request.is_disconnected():
                    return

                raw_events = await tasks_api.get(f"/history/{task_history_id}/events")
                events = raw_events if isinstance(raw_events, list) else []
                if len(events) < emitted_count:
                    logger.warning(
                        "Execution events list shrank (task_history_id=%s): "
                        "had %s emitted index, now len=%s; resetting cursor",
                        task_history_id,
                        emitted_count,
                        len(events),
                    )
                    emitted_count = 0
                for event in events[emitted_count:]:
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                emitted_count = len(events)

                task_history = as_json_object(
                    await tasks_api.get(f"/history/{task_history_id}")
                )
                if task_history["status"] != TaskHistoryStatusEnum.RUNNING:
                    payload = {"status": task_history["status"]}
                    yield f"event: finish\ndata: {json.dumps(payload)}\n\n"
                    return
                await asyncio.sleep(wait_interval)
    except TimeoutError as exc:
        logger.warning(
            "Timeout while streaming execution events task_history_id=%s: %s",
            task_history_id,
            exc,
        )
        yield f"event: extensions-error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
    except asyncio.CancelledError:
        logger.info(
            "Task execution events stream cancelled task_history_id=%s", task_history_id
        )
        raise
    except HTTPException as exc:
        logger.exception(
            "HTTP error streaming task execution events [%s]", exc.status_code
        )
        payload = {"code": exc.status_code, "detail": exc.detail}
        yield f"event: extensions-error\ndata: {json.dumps(payload)}\n\n"
    except Exception as exc:
        logger.exception(
            "Error streaming task execution events task_history_id=%s",
            task_history_id,
        )
        yield f"event: extensions-error\ndata: {json.dumps({'detail': str(exc)})}\n\n"


@router.get("/{task_history_id}", dependencies=[IsApiAuthenticated])
async def task_logs_event_stream(
    request: Request,
    user: ApiCurrentUser,
    task_history: Annotated[TaskHistoryResponse, Depends(get_task_history)],
    tasks_client: TasksClient,
) -> StreamingResponse:
    """Stream a task history's logs as server-sent events."""
    return StreamingResponse(
        task_history_logs_event_stream(
            tasks_client, task_history.id, request, user.access_token
        ),
        media_type="text/event-stream",
        headers=STREAMING_PROXY_HEADERS,
    )


# TODO(yan): Put stream_task_history_logs in a proper TasksAPI SDK class
# SEP-130
async def task_history_logs_event_stream(
    tasks_client: TasksClient, task_history_id: int, request: Request, access_token: str
) -> AsyncGenerator[str, None]:
    """Stream logs from a task history as server-sent events.

    Streams log lines for a given task history ID from the Tasks API and yields them
    formatted as server-sent events.

    The log read carries ``access_token`` so it is attributed to the viewing user.
    The reconciliation that follows it is a mutating Tasks API call, which the
    unsafe-method admin gate admits only for an admin or the service principal —
    and this stream is open to any authenticated user, so that call carries the
    internal token instead.

    :param tasks_client: The TaskAPI client for interacting with the Tasks service.
    :param task_history_id: The ID of the task history whose logs to stream.
    :param request: The FastAPI request object, used to access query parameters.
    :param access_token: Bearer token authenticating the log read as the viewing
        user.
    :return: Log entries formatted as server-sent events.
    """
    try:
        with tasks_client.auth(access_token) as tasks_api:
            # No read timeout: log stream can stall under backpressure (e.g. ~26MB)
            # and must not be killed by the default sock_read=120.
            async for log_entry in tasks_api.stream(
                f"/history/{task_history_id}/logs/",
                params=request.query_params,
                timeout=ClientTimeout(sock_read=None),
            ):
                if log_entry:
                    yield f"data: {log_entry.decode()}\n\n"
        with tasks_client.auth(get_internal_token()) as sync_api:
            task_history = as_json_object(
                await sync_api.post(f"/history/{task_history_id}/sync/")
            )
        yield f"event: finish\ndata: {json.dumps({'status': task_history['status']})}\n\n"
    except TimeoutError as exc:
        logger.warning(
            "Timeout while streaming task logs task_history_id=%s: %s",
            task_history_id,
            exc,
        )
        yield f"event: extensions-error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
    except asyncio.CancelledError:
        logger.info(
            "Task log stream cancelled task_history_id=%s (e.g. user closed panel or switched tab)",
            task_history_id,
        )
        raise
    except HTTPException as exc:
        logger.exception("HTTP error streaming task logs [%s]", exc.status_code)
        payload = {"code": exc.status_code, "detail": exc.detail}
        yield f"event: extensions-error\ndata: {json.dumps(payload)}\n\n"
    except Exception as exc:
        logger.exception(
            "Error streaming task logs task_history_id=%s (non-timeout, non-cancellation)",
            task_history_id,
        )
        yield f"event: extensions-error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
