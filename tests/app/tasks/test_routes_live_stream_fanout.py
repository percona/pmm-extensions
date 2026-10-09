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

"""Cover concurrent subscribers to one running execution's live log stream.

Every subscriber runs the real Tasks API route and the real entered
:class:`~app.tasks.execution.executors.nomad.models.NomadExecutor`; only Nomad
is replaced, at its HTTP boundary, by :class:`~tests.app.tasks.nomad_log_stub.NomadLogStub`.
Responses are read with :func:`~tests.app.asgi_stream.asgi_stream`, because both
HTTP test clients buffer the whole body and so cannot time a single line.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import AsyncExitStack, suppress

import pytest
import pytest_asyncio
from fastapi import status
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.main import tasks_app
from app.tasks.models import (
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.asgi_stream import asgi_stream, ASGIStream
from tests.app.factories import build_task_history, TaskFactory
from tests.app.tasks.nomad_log_stub import NomadLogStub

pytestmark = pytest.mark.asyncio

#: The acceptance bound: each line reaches every subscriber this soon after
#: Nomad wrote it.
LIVE_LATENCY_BOUND = 2.0

#: How long the test waits for every subscriber's follows to open before it
#: starts emitting; a starved follow never opens, so this is not asserted.
FOLLOWS_OPEN_GRACE = 1.0

#: Seconds the stub's allocation lookup takes in the loop-stall test.
SLOW_ALLOCATION_LOOKUP = 1.0

#: Seconds the stand-in anonymizer takes per frame in the loop-stall test.
SLOW_ANONYMIZATION = 1.0

#: How often the loop-stall probe wakes up.
LOOP_TICK = 0.02

#: The executor's retry wait for a step Nomad reports as not started.
NOT_STARTED_RETRY_WAIT = 2

#: Upper bound on a whole run, so a stream that never ends fails the test.
RUN_TIMEOUT = 60.0


@pytest_asyncio.fixture
async def running_history(
    session: AsyncSession, nomad_stub: NomadLogStub
) -> TaskHistory:
    """Persist a RUNNING, non-anonymized history tracking the stub's allocation.

    Anonymization is off so every line reaches the viewer verbatim and the
    timing measures delivery alone; its concurrency has a test of its own.

    :return: The saved history.
    """
    task = await TaskManager.create(
        session, TaskWrite.model_validate(TaskFactory.build(name="live-log-task"))
    )
    history = build_task_history(task, TaskHistoryStatusEnum.RUNNING)
    history.execution_request = TaskExecutionRequest(
        task=task.name,
        target="node1",
        meta={"target": "node1"},
        tracking={"job_id": nomad_stub.job_id, "evaluation_id": nomad_stub.eval_id},
    )
    history.anonymize_mask = 0
    saved = await TaskHistoryManager.save(session, history)
    assert saved.anonymized_entities == set()
    return saved


@pytest.fixture
def live_route_overrides(
    regular_user: CasdoorUser, session: AsyncSession, live_executor: NomadExecutor
) -> Iterator[None]:
    """Override auth and the session, and resolve the real entered executor.

    The executor dependency is replaced only because no lifecycle runs here; it
    holds the executor for the response the way ``get_request_executor`` does.
    Nothing on the body or stream path is overridden.
    """

    async def held_executor() -> AsyncGenerator[NomadExecutor]:
        async with live_executor.hold():
            yield live_executor

    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = lambda: (
        None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = held_executor
    yield
    tasks_app.dependency_overrides = {}


async def _record_arrivals(
    stream: ASGIStream, arrivals: dict[tuple[str, str], float]
) -> None:
    """Record when each log line first reached this subscriber.

    :param stream: The subscriber's in-flight response.
    :param arrivals: Filled with ``(log type, line) -> monotonic receipt time``.
    """
    buffer = b""
    while (chunk := await stream.next_chunk()) is not None:
        received = time.monotonic()
        buffer += chunk
        *records, buffer = buffer.split(b"\n")
        for record in filter(None, records):
            log = json.loads(record)
            for line in log["msg"].splitlines():
                arrivals.setdefault((log["type"], line), received)


async def _run_subscribers(
    nomad_stub: NomadLogStub, history_id: int, subscribers: int
) -> list[dict[tuple[str, str], float]]:
    """Open ``subscribers`` live log streams, emit the step's output, read it all.

    The streams are opened one after another, since they share the test's one
    database session, and read concurrently once all are open.

    :param nomad_stub: The stub serving the running step.
    :param history_id: The RUNNING history to stream.
    :param subscribers: The number of concurrent subscribers.
    :return: Each subscriber's arrival times, in opening order.
    """
    arrivals = [{} for _ in range(subscribers)]
    async with AsyncExitStack() as stack:
        streams = [
            await stack.enter_async_context(
                asgi_stream(tasks_app, f"/history/{history_id}/logs/")
            )
            for _ in range(subscribers)
        ]
        assert [stream.status_code for stream in streams] == [
            status.HTTP_200_OK
        ] * subscribers
        readers = [
            asyncio.create_task(_record_arrivals(stream, received))
            for stream, received in zip(streams, arrivals, strict=True)
        ]
        with suppress(TimeoutError):
            await asyncio.wait_for(
                nomad_stub.wait_for_follows(subscribers * len(TaskLogType)),
                FOLLOWS_OPEN_GRACE,
            )
        await nomad_stub.emit()
        await asyncio.wait_for(asyncio.gather(*readers), RUN_TIMEOUT)
    return arrivals


@pytest.mark.usefixtures("live_route_overrides")
@pytest.mark.parametrize(
    "subscribers",
    [
        pytest.param(2, id="two-subscribers"),
        pytest.param(6, id="more-follows-than-the-old-per-host-cap"),
    ],
)
async def test_every_subscriber_receives_each_line_live(
    nomad_stub: NomadLogStub,
    running_history: TaskHistory,
    subscribers: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deliver every line to every concurrent subscriber within the live bound.

    Each subscriber opens a follow per log type, so six subscribers need twelve
    concurrent Nomad connections to the one host. None of them may queue for a
    connection, and closing the responses leaves no stream task behind.
    """
    with caplog.at_level(logging.WARNING):
        arrivals = await _run_subscribers(nomad_stub, running_history.id, subscribers)
    leftover = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]

    assert leftover == []
    assert "for a pooled connection" not in caplog.text
    assert len(nomad_stub.emitted) == nomad_stub.line_count * len(TaskLogType)
    for received in arrivals:
        assert received.keys() == nomad_stub.emitted.keys()
    lags = {
        (subscriber, key): received[key] - emitted_at
        for subscriber, received in enumerate(arrivals)
        for key, emitted_at in nomad_stub.emitted.items()
    }
    worst = max(lags, key=lags.__getitem__)
    assert lags[worst] <= LIVE_LATENCY_BOUND, (
        f"subscriber {worst[0]} got {worst[1]} {lags[worst]:.2f}s after Nomad wrote it"
    )


async def _longest_loop_gap(stop: asyncio.Event) -> float:
    """Measure the longest stretch the event loop went without running this task.

    :param stop: Set by the caller to end the measurement.
    :return: The longest gap between two consecutive wake-ups, in seconds.
    """
    longest = 0.0
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(LOOP_TICK)
        now = time.monotonic()
        longest = max(longest, now - last)
        last = now
    return longest


@pytest.mark.usefixtures("live_route_overrides")
async def test_a_slow_allocation_lookup_does_not_stall_the_loop(
    nomad_stub: NomadLogStub, running_history: TaskHistory
) -> None:
    """Keep the event loop serving other streams while Nomad answers slowly.

    The route's preflight and the stream's own allocation lookup both go through
    the synchronous Nomad client, so each must wait off the event loop.
    """
    nomad_stub.allocation_delay = SLOW_ALLOCATION_LOOKUP
    stop = asyncio.Event()
    gap = asyncio.create_task(_longest_loop_gap(stop))
    async with asgi_stream(tasks_app, f"/history/{running_history.id}/logs/") as stream:
        assert stream.status_code == status.HTTP_200_OK
        await asyncio.wait_for(
            nomad_stub.wait_for_follows(len(TaskLogType)), RUN_TIMEOUT
        )
        stop.set()
        longest = await gap

    assert longest < SLOW_ALLOCATION_LOOKUP / 2


async def test_a_slow_anonymization_does_not_stall_the_loop(
    nomad_stub: NomadLogStub, mocker: MockerFixture
) -> None:
    """Keep the event loop serving other streams while a frame is anonymized.

    The anonymizer stands in for a slow analysis, such as the first one, which
    loads the language model; every live frame of an anonymized step passes
    through it.
    """

    def slow_anonymize(text: str, _entities: set[PIIEntity]) -> str:
        time.sleep(SLOW_ANONYMIZATION)
        return text

    mocker.patch(
        "app.tasks.execution.executors.nomad.models.anonymize_text",
        side_effect=slow_anonymize,
    )
    queue = asyncio.Queue()
    stop = asyncio.Event()
    gap = asyncio.create_task(_longest_loop_gap(stop))
    async with NomadExecutor(
        endpoint=nomad_stub.endpoint, verify_ssl=False
    ) as executor:
        stream = asyncio.create_task(
            executor._push_logs_to_queue(
                nomad_stub.allocation(),
                nomad_stub.step,
                TaskLogType.STDOUT,
                queue,
                anonymize_entities={PIIEntity.EMAIL_ADDRESS},
            )
        )
        try:
            await nomad_stub.emit()
            first = await asyncio.wait_for(queue.get(), RUN_TIMEOUT)
        finally:
            stop.set()
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
    longest = await gap

    assert first.msg == "line-0\n"
    assert longest < SLOW_ANONYMIZATION / 2


async def test_a_cancelled_lookup_keeps_its_executor_until_the_thread_ends(
    nomad_stub: NomadLogStub,
) -> None:
    """Defer a retirement close until a cancelled lookup's worker thread is done.

    A viewer disconnecting mid-lookup cancels the awaiting task, under AnyIO
    repeatedly, but the worker thread keeps using the executor's Nomad client.
    """
    nomad_stub.allocation_delay = SLOW_ALLOCATION_LOOKUP
    executor = await NomadExecutor(
        endpoint=nomad_stub.endpoint, verify_ssl=False
    ).open()
    lookup = asyncio.create_task(
        executor.run_in_thread_held(
            executor.get_last_allocation, nomad_stub.job_id, nomad_stub.eval_id
        )
    )
    await asyncio.sleep(SLOW_ALLOCATION_LOOKUP / 4)
    for _ in range(2):
        lookup.cancel()
        await asyncio.sleep(0)
    with suppress(asyncio.CancelledError):
        await lookup
    await executor.close_when_idle()
    open_while_thread_runs = executor.session is not None

    await asyncio.gather(*executor._held_workers)

    assert lookup.cancelled()
    assert open_while_thread_runs
    assert executor.session is None
    assert executor._sync_session is None


async def test_a_not_started_steps_retry_wait_holds_no_stream_slot(
    nomad_stub: NomadLogStub, caplog: pytest.LogCaptureFixture
) -> None:
    """Let a running step's follow start while a not-started step waits to retry.

    With one stream slot, a 404 retry that kept its connection through the
    retry wait would leave the running step's follow queued until the wait ends.
    """
    nomad_stub.not_started_steps = {NomadStep.CLEAN_UP}
    alloc = nomad_stub.allocation()
    async with NomadExecutor(
        endpoint=nomad_stub.endpoint,
        verify_ssl=False,
        log_stream_max_connections=1,
        wait_interval=NOT_STARTED_RETRY_WAIT,
    ) as executor:
        with caplog.at_level(logging.WARNING):
            retrying = asyncio.create_task(
                executor._push_logs_to_queue(
                    alloc, NomadStep.CLEAN_UP, TaskLogType.STDOUT, asyncio.Queue()
                )
            )
            following = asyncio.create_task(asyncio.sleep(0))
            try:
                await asyncio.wait_for(nomad_stub.wait_for_not_found(1), RUN_TIMEOUT)
                following = asyncio.create_task(
                    executor._push_logs_to_queue(
                        alloc, nomad_stub.step, TaskLogType.STDOUT, asyncio.Queue()
                    )
                )
                await asyncio.wait_for(
                    nomad_stub.wait_for_follows(1), NOT_STARTED_RETRY_WAIT / 4
                )
            finally:
                retrying.cancel()
                following.cancel()
                await asyncio.gather(retrying, following, return_exceptions=True)

    assert "for a pooled connection" not in caplog.text
