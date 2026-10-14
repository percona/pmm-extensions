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

"""Cover viewers of one running execution sharing its upstream log follows.

Every viewer runs the real entered
:class:`~app.tasks.execution.executors.nomad.models.NomadExecutor` against
:class:`~tests.app.tasks.nomad_log_stub.NomadLogStub`, which counts the follows
and the catch-up reads it serves. The route tests read the real Tasks API
response; the rest read ``stream_logs`` directly, which is what a test needs to
start a viewer at chosen offsets and to stop it reading mid-stream.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack

import pytest
import pytest_asyncio
from fastapi import status
from pytest_mock import MockerFixture

from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.execution.executors.nomad import models as nomad_models
from app.tasks.execution.executors.nomad.log_follow import SUBSCRIBER_BACKLOG
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.main import tasks_app
from app.tasks.models import TaskHistory, TaskLogType
from tests.app.asgi_stream import asgi_stream, ASGIStream
from tests.app.tasks.live_log_viewer import attach, live_history, LiveViewer, until
from tests.app.tasks.nomad_log_stub import FRAME_SIZE, NomadLogStub

pytestmark = pytest.mark.asyncio

STDOUT = TaskLogType.STDOUT
EMAIL = {PIIEntity.EMAIL_ADDRESS}

#: Viewers of one run in the multi-viewer tests.
VIEWERS = 4

#: Each line reaches a viewer that keeps reading this soon after Nomad wrote it.
LIVE_LATENCY_BOUND = 2.0

#: How long the not-started test watches for retries beyond the first round;
#: well inside the executor's 5 s retry wait.
RETRY_WATCH = 1.0

#: Upper bound on a whole run, so a stream that never ends fails the test.
RUN_TIMEOUT = 60.0

#: A withheld-bytes ceiling small enough to force a flush mid-address.
SMALL_CEILING = 24


@pytest_asyncio.fixture
async def ceiling_executor(nomad_stub: NomadLogStub) -> AsyncGenerator[NomadExecutor]:
    """Enter an executor whose anonymization flushes past :data:`SMALL_CEILING`.

    :return: The entered executor.
    """
    async with NomadExecutor(
        endpoint=nomad_stub.endpoint,
        verify_ssl=False,
        log_anonymization_max_withheld_bytes=SMALL_CEILING,
    ) as executor:
        yield executor


def _assert_resumable(viewer: LiveViewer, raw: str, start: int) -> None:
    """Assert the viewer got ``raw`` from ``start``, each offset resumable.

    Every emitted offset must be where the text received so far ends in the raw
    log, so passing it back as a start offset continues without gap or repeat.

    :param viewer: The viewer whose stdout is checked.
    :param raw: The whole raw stdout the stub served.
    :param start: The viewer's own start offset.
    """
    received = ""
    for log in viewer.logs[STDOUT]:
        received += log.msg or ""
        assert raw[start : log.offset] == received
    assert received == raw[start:]


async def _stdout_text(stream: ASGIStream) -> str:
    """Read a route response to its end and return its stdout text.

    :param stream: The in-flight response.
    :return: Every stdout ``msg``, joined in arrival order.
    """
    records = (await stream.drain()).splitlines()
    return "".join(
        log["msg"]
        for log in map(json.loads, filter(None, records))
        if log["type"] == STDOUT
    )


async def _watch_route(
    nomad_stub: NomadLogStub, history_id: int | None, viewers: int
) -> list[str]:
    """Open ``viewers`` route responses, emit the step's output, read it all.

    :param nomad_stub: The stub serving the running step.
    :param history_id: The RUNNING history to stream.
    :param viewers: The number of concurrent route viewers.
    :return: Each viewer's stdout text, in opening order.
    """
    async with AsyncExitStack() as stack:
        streams = [
            await stack.enter_async_context(
                asgi_stream(tasks_app, f"/history/{history_id}/logs/")
            )
            for _ in range(viewers)
        ]
        assert [stream.status_code for stream in streams] == [
            status.HTTP_200_OK
        ] * viewers
        readers = [asyncio.create_task(_stdout_text(stream)) for stream in streams]
        await nomad_stub.emit()
        return await asyncio.wait_for(asyncio.gather(*readers), RUN_TIMEOUT)


@pytest.mark.usefixtures("live_route_overrides")
async def test_route_viewers_of_one_run_share_one_follow_per_stream(
    nomad_stub: NomadLogStub, running_history: TaskHistory
) -> None:
    """Serve every route viewer of one run from one Nomad follow per log type.

    A viewer that subscribed after the first frame catches up through a read of
    its own, which has ended by the time its stream has.
    """
    texts = await _watch_route(nomad_stub, running_history.id, VIEWERS)

    expected = "".join(f"line-{index}\n" for index in range(nomad_stub.line_count))
    assert texts == [expected] * VIEWERS
    assert nomad_stub.peak_follows == len(TaskLogType)
    assert nomad_stub.open_non_follows == 0


@pytest.mark.usefixtures("live_route_overrides")
async def test_a_sole_route_viewer_makes_no_catch_up_read(
    nomad_stub: NomadLogStub, running_history: TaskHistory
) -> None:
    """Serve a viewer alone from its follow, with no read besides it."""
    texts = await _watch_route(nomad_stub, running_history.id, 1)

    assert texts == [
        "".join(f"line-{index}\n" for index in range(nomad_stub.line_count))
    ]
    assert nomad_stub.non_follow_requests == 0


@pytest.mark.usefixtures("live_route_overrides")
async def test_a_not_started_step_runs_one_retry_loop_for_every_viewer(
    nomad_stub: NomadLogStub, running_history: TaskHistory
) -> None:
    """Retry a not-started step once per log type, not once per viewer."""
    nomad_stub.not_started_steps = {NomadStep.CLEAN_UP}
    async with AsyncExitStack() as stack:
        for _ in range(VIEWERS):
            stream = await stack.enter_async_context(
                asgi_stream(tasks_app, f"/history/{running_history.id}/logs/")
            )
            assert stream.status_code == status.HTTP_200_OK
        await asyncio.wait_for(
            nomad_stub.wait_for_not_found(len(TaskLogType)), RUN_TIMEOUT
        )
        await asyncio.sleep(RETRY_WATCH)

        assert nomad_stub.not_found_served == len(TaskLogType)


async def test_each_kind_of_joiner_gets_the_raw_text_from_its_offset(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Give every viewer exactly the raw text from its offset, whenever it joined.

    The early viewers are there before the first frame; the others join once
    the first two lines have arrived as one frame: from 0, from the end of
    that frame, and from inside it.
    """
    history = live_history(nomad_stub)
    early = LiveViewer(live_executor, history, nomad_stub.step)
    before_first_frame = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, early, before_first_frame)
    await nomad_stub.write(STDOUT, b"alpha\nbravo\n")
    await until(lambda: early.offset() == len("alpha\nbravo\n"))

    joiners = {
        0: LiveViewer(live_executor, history, nomad_stub.step),
        12: LiveViewer(live_executor, history, nomad_stub.step, {STDOUT: 12}),
        3: LiveViewer(live_executor, history, nomad_stub.step, {STDOUT: 3}),
    }
    await attach(nomad_stub, *joiners.values())
    await nomad_stub.write(STDOUT, b"charlie\n")
    await nomad_stub.finish()
    viewers = [early, before_first_frame, *joiners.values()]
    await until(lambda: all(viewer.finished for viewer in viewers))

    raw = "alpha\nbravo\ncharlie\n"
    _assert_resumable(early, raw, 0)
    _assert_resumable(before_first_frame, raw, 0)
    for start, viewer in joiners.items():
        _assert_resumable(viewer, raw, start)


async def test_a_joiner_behind_a_mid_line_start_gets_the_line_redacted_whole(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Redact an address that straddles the offset the shared follow started at."""
    history = live_history(nomad_stub, EMAIL)
    await nomad_stub.write(STDOUT, b"mail alice")
    first = LiveViewer(live_executor, history, nomad_stub.step, {STDOUT: 10})
    await attach(nomad_stub, first)
    joiner = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, joiner)

    await nomad_stub.write(STDOUT, b"@example.com now\n")
    await until(lambda: joiner.offset() == len("mail alice@example.com now\n"))

    assert joiner.text() == "mail <EMAIL_ADDRESS> now\n"
    await first.leave()
    await joiner.leave()


async def test_a_handoff_never_lands_on_a_forced_flush(
    nomad_stub: NomadLogStub, ceiling_executor: NomadExecutor
) -> None:
    """Redact an address the shared follow had to force out half-way through.

    Twenty spaces and ``alice@`` exceed the ceiling, so the shared follow
    flushes them unfinished. A viewer resuming from the spaces' end would
    withhold only ``alice@`` alone and redact the address whole.
    """
    history = live_history(nomad_stub, EMAIL)
    first = LiveViewer(ceiling_executor, history, nomad_stub.step)
    await attach(nomad_stub, first)
    await nomad_stub.write(STDOUT, b" " * 20 + b"alice@")
    await until(lambda: first.offset() == len(" " * 20 + "alice@"))

    joiner = LiveViewer(ceiling_executor, history, nomad_stub.step, {STDOUT: 20})
    await attach(nomad_stub, joiner)
    await nomad_stub.write(STDOUT, b"example.com\n")
    await until(lambda: joiner.offset() == len(" " * 20 + "alice@example.com\n"))

    assert joiner.text() == "<EMAIL_ADDRESS>\n"
    await first.leave()
    await joiner.leave()


async def test_viewers_share_a_follow_only_with_their_own_entity_set(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor, mocker: MockerFixture
) -> None:
    """Anonymize each frame once per entity set, never once per viewer."""
    anonymize = mocker.spy(nomad_models, "anonymize_text")
    entity_sets = [EMAIL, {PIIEntity.PHONE_NUMBER}]
    same_set, other_set = (live_history(nomad_stub, set_) for set_ in entity_sets)
    viewers = [
        LiveViewer(live_executor, same_set, nomad_stub.step) for _ in range(VIEWERS - 1)
    ]
    odd_one = LiveViewer(live_executor, other_set, nomad_stub.step)
    await attach(nomad_stub, *viewers, odd_one)
    anonymize.reset_mock()

    line = "mail alice@example.com\n"
    await nomad_stub.write(STDOUT, line.encode())
    await until(
        lambda: all(viewer.offset() == len(line) for viewer in [*viewers, odd_one])
    )

    assert anonymize.call_count == len(entity_sets)
    assert nomad_stub.open_follows == len(entity_sets) * len(TaskLogType)
    assert [viewer.text() for viewer in viewers] == ["mail <EMAIL_ADDRESS>\n"] * len(
        viewers
    )
    assert odd_one.text() == line
    for viewer in [*viewers, odd_one]:
        await viewer.leave()


@pytest.mark.parametrize(
    "stream_limit",
    [
        pytest.param(NomadExecutor.STREAM_CONNECTION_LIMIT, id="default-pool"),
        pytest.param(len(TaskLogType), id="pool-held-by-the-follows"),
    ],
)
async def test_a_stalled_viewer_neither_delays_the_others_nor_loses_text(
    nomad_stub: NomadLogStub, stream_limit: int
) -> None:
    """Keep a reading viewer live while another stops, then catch the other up.

    With a follow pool only as large as one run's follows, the catch-up must
    still complete: it reads through short calls, never the follows' pool.
    """
    lines = [f"line-{index:03d}\n" for index in range(3 * SUBSCRIBER_BACKLOG)]
    async with NomadExecutor(
        endpoint=nomad_stub.endpoint,
        verify_ssl=False,
        log_stream_max_connections=stream_limit,
    ) as executor:
        history = live_history(nomad_stub)
        stalled = LiveViewer(executor, history, nomad_stub.step)
        reading = LiveViewer(executor, history, nomad_stub.step)
        await attach(nomad_stub, stalled, reading)
        stalled.reading.clear()

        lags: list[float] = []
        for line in lines:
            written_at = time.monotonic()
            await nomad_stub.write(STDOUT, line.encode())
            await until(lambda line=line: reading.text().endswith(line))
            lags.append(time.monotonic() - written_at)
        stalled.reading.set()
        await until(lambda: stalled.text() == "".join(lines))
        await until(lambda: nomad_stub.open_non_follows == 0)

        assert lags, "the reading viewer was never timed"
        assert max(lags) <= LIVE_LATENCY_BOUND
        _assert_resumable(stalled, "".join(lines), 0)
        assert nomad_stub.non_follow_requests >= 1
        await stalled.leave()
        await reading.leave()


async def test_a_withheld_tail_reaches_every_viewer_when_the_step_ends(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Deliver an unfinished last line to every viewer, then end their streams."""
    history = live_history(nomad_stub, EMAIL)
    viewers = [LiveViewer(live_executor, history, nomad_stub.step) for _ in range(2)]
    await attach(nomad_stub, *viewers)

    await nomad_stub.write(STDOUT, b"tail-without-newline")
    await nomad_stub.finish()
    await until(lambda: all(viewer.finished for viewer in viewers))

    assert [viewer.text() for viewer in viewers] == ["tail-without-newline"] * 2


async def test_a_dropped_follow_ends_every_viewer_after_its_tail(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """End every viewer's stream with the withheld tail when Nomad drops the follow."""
    history = live_history(nomad_stub, EMAIL)
    viewers = [LiveViewer(live_executor, history, nomad_stub.step) for _ in range(2)]
    await attach(nomad_stub, *viewers)
    await nomad_stub.write(STDOUT, b"done\npartial")
    await until(lambda: all(viewer.offset() == len("done\n") for viewer in viewers))

    await nomad_stub.drop_follows()
    await until(lambda: all(viewer.finished for viewer in viewers))

    assert [viewer.text() for viewer in viewers] == ["done\npartial"] * 2


async def test_leaving_viewers_release_the_follow_only_once_all_have_left(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Keep the follow for the viewers that stay, and close it after the last."""
    history = live_history(nomad_stub)
    viewers = [LiveViewer(live_executor, history, nomad_stub.step) for _ in range(3)]
    await attach(nomad_stub, *viewers)

    await viewers[0].leave()
    await nomad_stub.write(STDOUT, b"after-one-left\n")
    await until(
        lambda: all(viewer.text() == "after-one-left\n" for viewer in viewers[1:])
    )
    for viewer in viewers[1:]:
        await viewer.leave()
    await until(lambda: nomad_stub.open_follows == 0)

    returning = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, returning)
    await until(lambda: returning.text() == "after-one-left\n")
    catching_up = LiveViewer(live_executor, history, nomad_stub.step)
    await until(lambda: nomad_stub.non_follow_requests >= 1)
    await catching_up.leave()

    assert nomad_stub.open_follows == len(TaskLogType)
    await until(lambda: nomad_stub.open_non_follows == 0)
    await returning.leave()


async def test_offsets_stay_cumulative_across_a_log_rotation(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Continue offsets across rotated files so each one resumes where it says."""
    history = live_history(nomad_stub)
    in_sync = [LiveViewer(live_executor, history, nomad_stub.step) for _ in range(2)]
    await attach(nomad_stub, *in_sync)
    await nomad_stub.write(STDOUT, b"one\n")
    await until(lambda: all(viewer.offset() == len("one\n") for viewer in in_sync))
    await nomad_stub.rotate(STDOUT)
    await nomad_stub.write(STDOUT, b"two\n")
    await until(lambda: all(viewer.offset() == len("one\ntwo\n") for viewer in in_sync))

    joiners = {
        0: LiveViewer(live_executor, history, nomad_stub.step),
        4: LiveViewer(live_executor, history, nomad_stub.step, {STDOUT: 4}),
    }
    await attach(nomad_stub, *joiners.values())
    await nomad_stub.write(STDOUT, b"three\n")
    raw = "one\ntwo\nthree\n"
    viewers = [*in_sync, *joiners.values()]
    await until(lambda: all(viewer.offset() == len(raw) for viewer in viewers))

    assert [log.offset for log in in_sync[0].logs[STDOUT]] == [4, 8, 14]
    for viewer in in_sync:
        _assert_resumable(viewer, raw, 0)
    for start, viewer in joiners.items():
        _assert_resumable(viewer, raw, start)
    for viewer in viewers:
        await viewer.leave()

    resumed_past_the_rotation = LiveViewer(
        live_executor, history, nomad_stub.step, {STDOUT: 8}
    )
    await until(lambda: resumed_past_the_rotation.offset() == len(raw))
    _assert_resumable(resumed_past_the_rotation, raw, 8)
    await resumed_past_the_rotation.leave()


async def test_a_joiner_keeps_following_after_nomad_evicts_a_rotated_file(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Hand a joiner off at the frontier once Nomad has deleted the oldest file.

    Eviction shifts Nomad's request offsets down by the deleted file's size, so
    a catch-up from 0 reaches Nomad's end before the shared follow's frontier,
    having read everything Nomad still holds. The joiner then follows on live,
    as its own follow would have.
    """
    history = live_history(nomad_stub)
    first = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, first)
    await nomad_stub.write(STDOUT, b"evicted\n")
    await until(lambda: first.offset() == len("evicted\n"))
    await nomad_stub.rotate(STDOUT)
    await nomad_stub.write(STDOUT, b"retained\n")
    await until(lambda: first.offset() == len("evicted\nretained\n"))
    await nomad_stub.evict_oldest(STDOUT)

    joiner = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, joiner)
    await nomad_stub.write(STDOUT, b"live\n")
    await until(lambda: joiner.text().endswith("live\n"))

    assert joiner.text() == "retained\nlive\n"
    assert first.text() == "evicted\nretained\nlive\n"
    await first.leave()
    await joiner.leave()


async def test_offsets_are_data_ends_however_nomad_frames_the_log(
    nomad_stub: NomadLogStub, live_executor: NomadExecutor
) -> None:
    """Hand a joiner off at true data ends when Nomad merges and splits frames.

    The backlog is many small writes past one frame's size, so Nomad sends it as
    a full frame whose ``Offset`` is where the first write ends, then a frame
    whose ``Offset`` was bumped by the first frame's length. The writes after
    the joiner arrive merged into one frame.
    """
    backlog = [f"backlog-{index:05d}\n".encode() for index in range(FRAME_SIZE // 10)]
    await nomad_stub.write(STDOUT, *backlog)
    history = live_history(nomad_stub)
    first = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, first)
    raw_backlog = b"".join(backlog).decode()
    await until(lambda: first.offset() == len(raw_backlog))

    joiner = LiveViewer(live_executor, history, nomad_stub.step)
    await attach(nomad_stub, joiner)
    await nomad_stub.write(STDOUT, b"merged-one\n", b"merged-two\n")
    raw = raw_backlog + "merged-one\nmerged-two\n"
    await until(lambda: first.offset() == joiner.offset() == len(raw))

    assert len(raw_backlog) > FRAME_SIZE
    _assert_resumable(first, raw, 0)
    _assert_resumable(joiner, raw, 0)
    await first.leave()
    await joiner.leave()
