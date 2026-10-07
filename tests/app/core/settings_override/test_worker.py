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

"""Tests for the reusable prefork-child settings-override boundary refresher."""

import asyncio
from collections.abc import Iterator
from contextlib import suppress
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.pool import StaticPool

from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.constants import (
    EXTENSIONS_SETTINGS,
    SETTINGS,
    TASKS_SETTINGS,
)
from app.core.settings_override.lifecycle import (
    CallbackRegistry,
    fire_on_boot,
    ProxyEntry,
    ProxyRegistry,
    SnapshotChange,
)
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.worker import SEED_TIMEOUT_FRACTION, WorkerRefresher
from app.core.utils import json_serializer
from app.extensions.config import ExtensionsSettings
from app.tasks.config import TasksSettings
from tests.app.core.settings_override.conftest import (
    BOUNDED_SEED,
    clear_connectivity_override,
    CONNECTIVITY_CALLBACK_KEY,
    HangingSession,
    recording_bounded_seed,
    recording_callback,
    seed_connectivity_override,
    WORKER_REFRESH_ALL,
)
from tests.app.db_schema import apply_schema

INTERVAL = timedelta(seconds=30)
SHORT_INTERVAL = timedelta(milliseconds=50)


class _FakeClock:
    """Monotonic clock stand-in so tests can advance the due-check."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now += seconds

    def advance_past(self, interval: timedelta) -> None:
        """Advance far enough that a due-check against ``interval`` succeeds."""
        self.advance(interval.total_seconds() + 1.0)


async def _noop_callback(_: SnapshotChange) -> None:
    """Accept a snapshot change and do nothing; a stand-in registry entry."""


def _make_registry() -> ProxyRegistry:
    """Compose a two-entry proxy registry over freshly-built proxies."""
    return {
        EXTENSIONS_SETTINGS: ProxyEntry(
            OverridableSettingsProxy(ExtensionsSettings),
            ExtensionsSettings,
        ),
        TASKS_SETTINGS: ProxyEntry(
            OverridableSettingsProxy(TasksSettings),
            TasksSettings,
        ),
    }


CALLBACKS: CallbackRegistry = {(SETTINGS, "PMM"): _noop_callback}


class _CountingRegistry:
    """Compose the registry through :func:`_make_registry`, counting each build."""

    def __init__(self) -> None:
        self.builds = 0

    def __call__(self) -> ProxyRegistry:
        self.builds += 1
        return _make_registry()


def _unreachable_loop() -> asyncio.AbstractEventLoop:
    """Fail the test if the refresher resolves the event loop."""
    raise AssertionError("the event loop must not be resolved")


def _unreachable_session_maker() -> async_sessionmaker:
    """Fail the test if the refresher resolves the session maker."""
    raise AssertionError("the session maker must not be resolved")


@pytest.fixture(name="loop")
def loop_fixture() -> Iterator[asyncio.AbstractEventLoop]:
    """Provide a dedicated event loop standing in for a prefork child's."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(name="session_maker")
def session_maker_fixture(
    loop: asyncio.AbstractEventLoop,
) -> Iterator[async_sessionmaker]:
    """Provide an in-memory SQLite session maker driven through ``loop``."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await apply_schema(conn, SQLModel.metadata)

    loop.run_until_complete(_create_schema())
    yield get_async_session_maker_from_engine(engine)
    loop.run_until_complete(engine.dispose())


class TestWorkerRefresherStart:
    """Cover the start path: the enabled gate, idempotency, and late resolution."""

    def test_start_arms_without_creating_a_periodic_task(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Seed and arm the child; no background asyncio.Task is created."""
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)

        refresher.start(INTERVAL, enabled=True)

        try:
            assert refresher._armed
            assert refresher._proxies is not None
            assert not hasattr(refresher, "task")
        finally:
            refresher.stop()

    def test_disabled_arms_nothing_and_builds_no_registry(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Skip the proxy registry entirely when the refresher is disabled."""
        registry = _CountingRegistry()
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, registry)

        refresher.start(INTERVAL, enabled=False)

        assert not refresher._armed
        assert refresher._proxies is None
        assert registry.builds == 0

    def test_construction_does_not_build_the_registry(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Defer the registry build past construction of the singleton."""
        registry = _CountingRegistry()

        WorkerRefresher(lambda: loop, lambda: session_maker, registry)

        assert registry.builds == 0

    def test_second_start_keeps_the_armed_state(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Return early on re-entry rather than re-seeding."""
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)
        refresher.start(INTERVAL, enabled=True)
        first_proxies = refresher._proxies
        first_stamp = refresher._last_refresh

        refresher.start(INTERVAL, enabled=True)

        try:
            assert refresher._armed
            assert refresher._proxies is first_proxies
            assert refresher._last_refresh == first_stamp
        finally:
            refresher.stop()

    def test_second_start_does_not_rebuild_the_registry(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Skip the registry build on a start that short-circuits."""
        registry = _CountingRegistry()
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, registry)
        refresher.start(INTERVAL, enabled=True)

        refresher.start(INTERVAL, enabled=True)

        try:
            assert registry.builds == 1
        finally:
            refresher.stop()

    def test_start_after_stop_rearms(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Allow a fresh start after disarm rather than leaving the child idle."""
        registry = _CountingRegistry()
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, registry)
        refresher.start(INTERVAL, enabled=True)
        builds_after_first_start = registry.builds
        refresher.stop()

        refresher.start(INTERVAL, enabled=True)

        try:
            assert refresher._armed
            assert registry.builds == builds_after_first_start + 1
        finally:
            refresher.stop()

    def test_start_resolves_its_dependencies_at_call_time(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Read the loop and session maker rebound after construction.

        Mirrors the prefork child, where ``init_child_event_loop`` replaces the
        loop and the tests rebind the session maker, both after the module-level
        singleton was constructed.
        """
        stale_loop = asyncio.new_event_loop()
        current_loop = stale_loop
        current_session_maker = None
        refresher = WorkerRefresher(
            lambda: current_loop,
            lambda: current_session_maker,
            _make_registry,
        )
        current_loop = loop
        current_session_maker = session_maker

        refresher.start(INTERVAL, enabled=True)

        try:
            assert refresher._armed
            assert refresher._proxies is not None
        finally:
            refresher.stop()
            stale_loop.close()

    @pytest.mark.parametrize(
        ("start_kwargs", "expected"),
        [
            pytest.param({"callbacks": CALLBACKS}, CALLBACKS, id="registry-forwarded"),
            pytest.param({}, None, id="omitted-defaults-to-none"),
        ],
    )
    def test_start_stores_the_callback_registry_for_boundary_refresh(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        start_kwargs: dict[str, CallbackRegistry],
        expected: CallbackRegistry | None,
    ) -> None:
        """Keep the caller's callbacks for ``maybe_refresh``, or ``None`` if omitted."""
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)

        refresher.start(INTERVAL, enabled=True, **start_kwargs)

        try:
            assert refresher._callbacks is expected
        finally:
            refresher.stop()

    def test_start_seed_fires_a_marked_callback_for_a_seeded_override(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
    ) -> None:
        """Fire a marked callback from the child's inline seed, then again on change.

        A freshly-forked child that finds an override already in the database
        must rebind at once; a later boundary refresh still fires on a real diff.
        """
        proxy = OverridableSettingsProxy(ExtensionsSettings)
        registry = {EXTENSIONS_SETTINGS: ProxyEntry(proxy, ExtensionsSettings)}
        fired: list[SnapshotChange] = []
        clock = _FakeClock()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        callbacks = {CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))}

        loop.run_until_complete(
            seed_connectivity_override(session_maker, value=override_value)
        )
        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, lambda: registry, now=clock
        )

        refresher.start(INTERVAL, enabled=True, callbacks=callbacks)

        try:
            assert len(fired) == 1
            assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value

            loop.run_until_complete(clear_connectivity_override(session_maker))
            clock.advance_past(INTERVAL)
            refresher.maybe_refresh()

            assert [change.current for change in fired] == [
                {"CONNECTIVITY_CHECK_DEFAULT": override_value},
                {},
            ]
        finally:
            refresher.stop()

    def test_start_seed_leaves_an_unmarked_callback_silent(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
    ) -> None:
        """Keep the child's inline seed silent for a boot-reproducing callback."""
        proxy = OverridableSettingsProxy(ExtensionsSettings)
        registry = {EXTENSIONS_SETTINGS: ProxyEntry(proxy, ExtensionsSettings)}
        fired: list[SnapshotChange] = []
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        callbacks = {CONNECTIVITY_CALLBACK_KEY: recording_callback(fired)}

        loop.run_until_complete(
            seed_connectivity_override(session_maker, value=override_value)
        )
        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, lambda: registry
        )

        refresher.start(INTERVAL, enabled=True, callbacks=callbacks)

        try:
            assert fired == []
            assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
        finally:
            refresher.stop()

    def test_start_forwards_seed_timeout_from_proc_alive_timeout(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Apply the safety fraction once in ``start`` when a deadline is set."""
        recorded: dict[str, object] = {}
        monkeypatch.setattr(BOUNDED_SEED, recording_bounded_seed(recorded))
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)

        refresher.start(INTERVAL, enabled=True, proc_alive_timeout=4.0)

        try:
            assert recorded["seed_timeout"] == pytest.approx(
                4.0 * SEED_TIMEOUT_FRACTION
            )
            assert refresher._armed
        finally:
            refresher.stop()

    def test_start_passes_unbounded_seed_when_proc_alive_timeout_unset(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hand ``seed_timeout=None`` to ``bounded_seed`` when no deadline is set."""
        recorded: dict[str, object] = {}
        monkeypatch.setattr(BOUNDED_SEED, recording_bounded_seed(recorded))
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)

        refresher.start(INTERVAL, enabled=True)

        try:
            assert recorded["seed_timeout"] is None
            assert refresher._armed
        finally:
            refresher.stop()

    def test_start_arms_after_a_hanging_seed_hits_its_budget(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Keep the child armed and refresh at the next boundary after seed expiry."""
        maker_holder: list[object] = [HangingSession]
        refresher = WorkerRefresher(
            lambda: loop, lambda: maker_holder[0], _make_registry
        )
        calls: list[object] = []

        async def _counting_refresh(*_args: object, **_kwargs: object) -> None:
            calls.append(True)

        with caplog.at_level("ERROR", logger="app.core.settings_override.lifecycle"):
            refresher.start(INTERVAL, enabled=True, proc_alive_timeout=0.1)

        maker_holder[0] = session_maker
        monkeypatch.setattr(WORKER_REFRESH_ALL, _counting_refresh)
        # Let a cancelled seed finish unwinding so the pending guard clears.
        if refresher._pending_refresh is not None:
            with suppress(asyncio.CancelledError):
                loop.run_until_complete(refresher._pending_refresh)

        refresher.maybe_refresh()

        try:
            assert refresher._armed
            assert calls == [True]
            assert any(
                record.levelname == "ERROR" and "incomplete" in record.message
                for record in caplog.records
            )
        finally:
            refresher.stop()


class TestWorkerRefresherMaybeRefresh:
    """Cover the task-boundary due-check, budget, and failure isolation."""

    def test_maybe_refresh_noops_inside_the_interval(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skip I/O for tasks arriving before the interval elapses."""
        calls: list[object] = []

        async def _counting_refresh(*_args: object, **_kwargs: object) -> None:
            calls.append(True)

        monkeypatch.setattr(WORKER_REFRESH_ALL, _counting_refresh)
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)
        refresher.start(INTERVAL, enabled=True)
        calls.clear()

        refresher.maybe_refresh()

        try:
            assert calls == []
        finally:
            refresher.stop()

    def test_maybe_refresh_runs_when_due(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drive a refresh to completion inside one ``run_until_complete`` window."""
        calls: list[object] = []
        clock = _FakeClock()

        async def _counting_refresh(*_args: object, **_kwargs: object) -> None:
            calls.append(True)

        monkeypatch.setattr(WORKER_REFRESH_ALL, _counting_refresh)
        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, _make_registry, now=clock
        )
        refresher.start(INTERVAL, enabled=True)
        calls.clear()
        clock.advance_past(INTERVAL)

        refresher.maybe_refresh()

        try:
            assert calls == [True]
        finally:
            refresher.stop()

    def test_maybe_refresh_noops_when_disarmed(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ignore task boundaries after shutdown."""
        calls: list[object] = []

        async def _counting_refresh(*_args: object, **_kwargs: object) -> None:
            calls.append(True)

        monkeypatch.setattr(WORKER_REFRESH_ALL, _counting_refresh)
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)
        refresher.start(INTERVAL, enabled=True)
        refresher.stop()
        calls.clear()

        refresher.maybe_refresh()

        assert calls == []
        assert not refresher._armed

    def test_budget_expiry_logs_warning_not_error(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Bound a hanging boundary refresh; leave state as-is and keep the task."""
        maker_holder: list[object] = [session_maker]
        clock = _FakeClock()
        refresher = WorkerRefresher(
            lambda: loop, lambda: maker_holder[0], _make_registry, now=clock
        )
        refresher.start(SHORT_INTERVAL, enabled=True)
        maker_holder[0] = HangingSession
        stamp_before = refresher._last_refresh
        clock.advance_past(SHORT_INTERVAL)

        with caplog.at_level("WARNING", logger="app.core.settings_override.worker"):
            refresher.maybe_refresh()

        try:
            assert refresher._last_refresh > stamp_before
            assert any(
                record.levelname == "WARNING" and "budget" in record.message
                for record in caplog.records
            )
            assert not any(record.levelname == "ERROR" for record in caplog.records)
        finally:
            refresher.stop()

    def test_budget_holds_when_refresh_hangs_on_unwind(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Cancel without awaiting unwind so a hung ``__aexit__`` cannot blow the budget.

        ``asyncio.wait_for`` would await the cancelled coroutine's ``finally`` /
        ``__aexit__`` and hang past the interval; ``bounded_refresh`` must not.
        The try body blocks until cancellation; ``finally`` then blocks on a
        separately releasable gate so cleanup stays stuck after cancel — the
        hang-safe path, not ordinary cancellation of an idle wait.
        """
        clock = _FakeClock()
        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, _make_registry, now=clock
        )
        refresher.start(SHORT_INTERVAL, enabled=True)
        unwind_gate = asyncio.Event()
        calls: list[object] = []

        async def _hang_on_unwind(*_args: object, **_kwargs: object) -> None:
            calls.append(True)
            try:
                await asyncio.Event().wait()
            finally:
                await unwind_gate.wait()

        # Patch after the inline seed so only the boundary refresh hangs on unwind.
        monkeypatch.setattr(WORKER_REFRESH_ALL, _hang_on_unwind)
        clock.advance_past(SHORT_INTERVAL)

        with caplog.at_level("WARNING", logger="app.core.settings_override.worker"):
            # A wait_for-based bound would hang here indefinitely on unwind.
            refresher.maybe_refresh()

        pending = refresher._pending_refresh
        try:
            assert any(
                record.levelname == "WARNING" and "budget" in record.message
                for record in caplog.records
            )
            assert pending is not None
            assert not pending.done()
            assert calls == [True]

            # Still unwinding: a later due boundary must not open another refresh.
            clock.advance_past(SHORT_INTERVAL)
            refresher.maybe_refresh()
            assert calls == [True]
            assert refresher._pending_refresh is pending
        finally:
            unwind_gate.set()
            if pending is not None:
                with suppress(asyncio.CancelledError):
                    loop.run_until_complete(pending)
            refresher.stop()

    def test_refresh_failure_does_not_propagate(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Swallow a boundary failure so the triggering task keeps running."""
        clock = _FakeClock()
        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, _make_registry, now=clock
        )
        refresher.start(INTERVAL, enabled=True)

        async def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("db unreachable")

        monkeypatch.setattr(WORKER_REFRESH_ALL, _boom)
        clock.advance_past(INTERVAL)

        with caplog.at_level("ERROR", logger="app.core.settings_override.worker"):
            refresher.maybe_refresh()

        try:
            assert any(
                "boundary refresh failed" in record.message for record in caplog.records
            )
        finally:
            refresher.stop()

    def test_callbacks_fire_on_a_boundary_refresh(
        self,
        loop: asyncio.AbstractEventLoop,
        session_maker: async_sessionmaker,
    ) -> None:
        """Fire rebind callbacks when a watched override changes at the boundary."""
        proxy = OverridableSettingsProxy(ExtensionsSettings)
        registry = {
            EXTENSIONS_SETTINGS: ProxyEntry(proxy, ExtensionsSettings),
        }
        fired: list[bool] = []
        clock = _FakeClock()

        async def _callback(_: SnapshotChange) -> None:
            fired.append(True)

        refresher = WorkerRefresher(
            lambda: loop, lambda: session_maker, lambda: registry, now=clock
        )
        refresher.start(
            INTERVAL, enabled=True, callbacks={CONNECTIVITY_CALLBACK_KEY: _callback}
        )
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT

        loop.run_until_complete(
            seed_connectivity_override(session_maker, value=override_value)
        )
        clock.advance_past(INTERVAL)

        refresher.maybe_refresh()

        try:
            assert fired == [True]
            assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
        finally:
            refresher.stop()


class TestWorkerRefresherStop:
    """Cover the shutdown path: disarm and the never-started no-op."""

    def test_stop_disarms_and_clears_proxies(
        self, loop: asyncio.AbstractEventLoop, session_maker: async_sessionmaker
    ) -> None:
        """Disarm the child and drop the held proxy/callback state."""
        refresher = WorkerRefresher(lambda: loop, lambda: session_maker, _make_registry)
        refresher.start(INTERVAL, enabled=True, callbacks=CALLBACKS)

        refresher.stop()

        assert not refresher._armed
        assert refresher._proxies is None
        assert refresher._callbacks is None
        assert refresher._pending_refresh is None

    def test_stop_without_start_resolves_nothing(self) -> None:
        """Return without touching the loop when never started."""
        refresher = WorkerRefresher(
            _unreachable_loop, _unreachable_session_maker, _make_registry
        )

        refresher.stop()

        assert not refresher._armed
