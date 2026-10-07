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

"""Cover the database connection a streaming task-history route holds.

Every request runs the real ``get_session`` dependency over a file-backed,
pooled SQLite engine sized below the number of concurrent streams, so a stream
that kept its request session open for its whole length would starve the pool.
Responses are read with :func:`~tests.app.asgi_stream.asgi_stream`, because both
HTTP test clients buffer the whole body and so cannot hold a stream open.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlmodel import SQLModel

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.main import tasks_app
from app.tasks.models import (
    TaskExecutionRequest,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
    TaskWrite,
)
from tests.app.asgi_stream import asgi_stream, ASGIStream
from tests.app.db_schema import apply_schema
from tests.app.factories import build_task_history, TaskFactory
from tests.app.tasks.nomad_log_stub import NomadLogStub

pytestmark = pytest.mark.asyncio

POOL_SIZE = 1
MAX_OVERFLOW = 1
POOL_TIMEOUT = 1

#: More concurrent streams than the pool has connections.
STREAMS_BEYOND_POOL = POOL_SIZE + MAX_OVERFLOW + 1

#: Upper bound on a whole run, so a stream that never ends fails the test.
RUN_TIMEOUT = 60.0


@pytest_asyncio.fixture
async def pooled_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncEngine]:
    """Serve the Tasks API's sessions from a small file-backed connection pool.

    An in-memory SQLite database would force ``StaticPool``, which has no
    sizing, so the database lives in a file.

    :return: The pooled engine every request's ``get_session`` draws from.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}",
        json_serializer=json_serializer,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    session_maker = get_async_session_maker_from_engine(engine)
    monkeypatch.setattr("app.tasks.deps.get_async_session_maker", lambda: session_maker)
    yield engine
    await engine.dispose()


async def _persist_history(
    engine: AsyncEngine,
    status_: TaskHistoryStatusEnum,
    tracking: dict[str, str] | None = None,
) -> int:
    """Persist a task and one history of it through a session closed on return.

    The task anonymizes nothing and the history inherits that, so a stream reads
    the history's ``task`` relationship to resolve what to anonymize. A finished
    history also gets ten persisted stdout lines, ``line0`` to ``line9``.

    :param engine: The engine to persist through.
    :param status_: The history's status.
    :param tracking: The executor tracking to store, if any.
    :return: The history's id.
    """
    async with get_async_session_maker_from_engine(engine)() as session:
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(output_files_path="/output", anonymize_mask=0)
            ),
        )
        history = build_task_history(task, status_)
        if tracking is not None:
            history.execution_request = TaskExecutionRequest(
                task=task.name,
                target="node1",
                meta={"target": "node1"},
                tracking=tracking,
            )
        history.anonymize_mask = None
        saved = await TaskHistoryManager.save(session, history)
        if status_.is_finished():
            payload = "".join(f"line{i}\n" for i in range(10)).encode()
            await TaskHistoryLogWriter.append(
                session,
                saved.id,
                source="run-script",
                stream=TaskLogType.STDOUT,
                new_bytes=payload,
                force_flush=True,
                producer_offset_after=len(payload),
            )
        return saved.id


@pytest.fixture
def held_stream() -> asyncio.Event:
    """Return the event that ends a mocked executor stream once set."""
    return asyncio.Event()


@pytest.fixture
def blocking_executor(held_stream: asyncio.Event) -> AsyncMock:
    """Return an executor whose streams send one chunk, then wait for the event.

    :param held_stream: Set to let each stream finish.
    :return: The mock executor.
    """

    async def stream_logs(*_args: object) -> AsyncGenerator[TaskLog]:
        yield TaskLog(step="run-script", type=TaskLogType.STDOUT, msg="line0\n")
        await held_stream.wait()

    async def stream_file(*_args: object) -> AsyncGenerator[bytes]:
        yield b"file content"
        await held_stream.wait()

    executor = AsyncMock(spec=NomadExecutor)
    executor.preflight_stream_logs = AsyncMock(return_value=None)
    executor.stream_logs = MagicMock(side_effect=stream_logs)
    executor.stream_file = MagicMock(side_effect=stream_file)
    return executor


def _override_auth(regular_user: CasdoorUser) -> None:
    """Authenticate every request as ``regular_user``.

    :param regular_user: The user the requests run as.
    """
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user


@pytest.fixture
def mocked_executor_routes(
    regular_user: CasdoorUser, pooled_engine: AsyncEngine, blocking_executor: AsyncMock
) -> Iterator[None]:
    """Override auth and the executor; the real ``get_session`` stays in place."""
    _override_auth(regular_user)
    tasks_app.dependency_overrides[get_request_executor] = lambda: blocking_executor
    yield
    tasks_app.dependency_overrides = {}


@pytest.fixture
def live_executor_routes(
    regular_user: CasdoorUser, pooled_engine: AsyncEngine, live_executor: NomadExecutor
) -> Iterator[None]:
    """Override auth and resolve the real entered Nomad executor.

    The executor dependency is replaced only because no lifecycle runs here; it
    holds the executor for the response the way ``get_request_executor`` does.
    """

    async def held_executor() -> AsyncGenerator[NomadExecutor]:
        async with live_executor.hold():
            yield live_executor

    _override_auth(regular_user)
    tasks_app.dependency_overrides[get_request_executor] = held_executor
    yield
    tasks_app.dependency_overrides = {}


async def _list_tasks_status() -> int:
    """Run a short database-backed request to completion and return its status.

    :return: The status code of ``GET /``.
    """
    async with asgi_stream(tasks_app, "/") as response:
        await response.drain()
        return response.status_code


async def _assert_pool_free_while_open(
    engine: AsyncEngine, path: str, query_string: bytes = b""
) -> None:
    """Open a stream, then check the pool from inside its first chunk.

    :param engine: The pooled engine the request draws from.
    :param path: The streaming route to open.
    :param query_string: The raw query string to send.
    """
    async with asgi_stream(tasks_app, path, query_string=query_string) as stream:
        assert stream.status_code == status.HTTP_200_OK
        assert await stream.next_chunk() is not None

        assert engine.pool.checkedout() == 0
        assert await _list_tasks_status() == status.HTTP_200_OK


@pytest.mark.usefixtures("mocked_executor_routes")
async def test_running_log_stream_releases_connection_before_streaming(
    pooled_engine: AsyncEngine, held_stream: asyncio.Event
) -> None:
    """Return a running history's connection to the pool while its log streams."""
    history_id = await _persist_history(pooled_engine, TaskHistoryStatusEnum.RUNNING)

    try:
        await _assert_pool_free_while_open(
            pooled_engine, f"/history/{history_id}/logs/"
        )
    finally:
        held_stream.set()


@pytest.mark.usefixtures("mocked_executor_routes")
async def test_file_stream_releases_connection_before_streaming(
    pooled_engine: AsyncEngine, held_stream: asyncio.Event
) -> None:
    """Return a finished history's connection to the pool while its file streams."""
    history_id = await _persist_history(pooled_engine, TaskHistoryStatusEnum.SUCCESS)

    try:
        await _assert_pool_free_while_open(
            pooled_engine, f"/history/{history_id}/file/", query_string=b"path=x"
        )
    finally:
        held_stream.set()


async def _read_lines(stream: ASGIStream) -> set[tuple[str, str]]:
    """Read a log stream to its end.

    :param stream: The in-flight response.
    :return: Every ``(log type, line)`` the stream delivered.
    """
    body = await stream.drain()
    return {
        (log["type"], line)
        for record in filter(None, body.split(b"\n"))
        for log in [json.loads(record)]
        for line in log["msg"].splitlines()
    }


@pytest.mark.usefixtures("live_executor_routes")
async def test_more_live_log_streams_than_pool_all_stream_and_short_calls_succeed(
    pooled_engine: AsyncEngine, nomad_stub: NomadLogStub
) -> None:
    """Serve more live log viewers than the pool has connections, and other calls.

    The history inherits its task's anonymization, so each stream reads the
    history's ``task`` after the route released the session it was loaded in.
    """
    history_id = await _persist_history(
        pooled_engine,
        TaskHistoryStatusEnum.RUNNING,
        tracking={"job_id": nomad_stub.job_id, "evaluation_id": nomad_stub.eval_id},
    )

    async with AsyncExitStack() as stack:
        streams = [
            await stack.enter_async_context(
                asgi_stream(tasks_app, f"/history/{history_id}/logs/")
            )
            for _ in range(STREAMS_BEYOND_POOL)
        ]
        assert [stream.status_code for stream in streams] == [
            status.HTTP_200_OK
        ] * STREAMS_BEYOND_POOL
        await asyncio.wait_for(
            nomad_stub.wait_for_follows(STREAMS_BEYOND_POOL * len(TaskLogType)),
            RUN_TIMEOUT,
        )

        short_status = await asyncio.wait_for(_list_tasks_status(), POOL_TIMEOUT)

        await nomad_stub.emit()
        received = await asyncio.wait_for(
            asyncio.gather(*(_read_lines(stream) for stream in streams)), RUN_TIMEOUT
        )

    assert short_status == status.HTTP_200_OK
    for lines in received:
        assert lines == nomad_stub.emitted.keys()


@pytest.mark.usefixtures("mocked_executor_routes")
@pytest.mark.parametrize(
    ("query_string", "expected"),
    [
        pytest.param(b"tail=2", {"line8", "line9"}, id="tail"),
        pytest.param(b"step=run-script", {f"line{i}" for i in range(10)}, id="step"),
    ],
)
async def test_finished_log_stream_still_reads_persisted_logs_with_real_session(
    pooled_engine: AsyncEngine, query_string: bytes, expected: set[str]
) -> None:
    """Stream a finished history's persisted logs through the real request session.

    The finished branch reads the database while it streams, so its session
    must stay open for the response.
    """
    history_id = await _persist_history(pooled_engine, TaskHistoryStatusEnum.SUCCESS)

    async with asgi_stream(
        tasks_app, f"/history/{history_id}/logs/", query_string=query_string
    ) as stream:
        assert stream.status_code == status.HTTP_200_OK
        lines = await _read_lines(stream)

    assert {line for _type, line in lines} == expected
