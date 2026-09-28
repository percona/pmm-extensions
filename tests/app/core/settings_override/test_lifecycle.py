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

"""Cover the override-snapshot lifecycle helpers and the background refresher."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import timedelta

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool

from app.core.config import BaseYamlSettings, Settings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.encryption import encrypt
from app.core.settings_override.cache import build_snapshot
from app.core.settings_override.lifecycle import (
    bounded_refresh,
    bounded_seed,
    fire_change_callbacks,
    fire_on_boot,
    previous_or_base,
    ProxyEntry,
    refresh_all,
    resolve_refresher_options,
    settings_override_refresher,
    SnapshotChange,
    start_refresh_task,
)
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingClassEnum, SettingOverride
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.utils import json_serializer
from app.extensions.config import ExtensionsSettings
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.execution.executors.nomad import NomadExecutor
from app.tasks.execution.nomad_lifecycle import NomadLifecycle
from app.tasks.main import _reconcile_nomad, tasks_app
from tests.app.core.settings_override.conftest import (
    clear_connectivity_override,
    CONNECTIVITY_CALLBACK_KEY,
    EXTENSIONS_SETTINGS_TOKEN,
    hanging_session_maker_factory,
    recording_callback,
    seed_connectivity_override,
    SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)
from tests.app.db_schema import apply_schema


@pytest_asyncio.fixture(name="session_maker")
async def session_maker_fixture() -> AsyncGenerator[async_sessionmaker, None]:
    """Provide an in-memory SQLite session maker bound to a fresh schema."""
    # scaffolding-dup-ok: this duplication predates the change that
    # re-annotated the fixture's return type; promoting it against
    # its sibling bootstrap is a cross-tree refactor of its own.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


def _make_proxies() -> tuple[OverridableSettingsProxy, dict]:
    """Construct a PMM Extensions proxy and a registry mapping for refresh tests."""
    proxy: OverridableSettingsProxy = OverridableSettingsProxy(
        ExtensionsSettings, setting_class=ExtensionsSettings.__name__
    )
    registry = {
        SettingClassEnum.EXTENSIONS_SETTINGS: ProxyEntry(proxy, ExtensionsSettings),
    }
    return proxy, registry


def test_previous_or_base_returns_snapshot_value() -> None:
    """Return the previous snapshot value when the key is present."""
    proxy, _ = _make_proxies()
    change = SnapshotChange({"INVENTORY_ENDPOINT": "https://prev.example.org"}, {})
    assert (
        previous_or_base(change, proxy, "INVENTORY_ENDPOINT")
        == "https://prev.example.org"
    )


def test_previous_or_base_falls_back_to_wrapped_instance() -> None:
    """Return the YAML/env value when the key is absent from previous."""
    proxy, _ = _make_proxies()
    expected = proxy._resolve().INVENTORY_ENDPOINT
    assert (
        previous_or_base(SnapshotChange({}, {}), proxy, "INVENTORY_ENDPOINT")
        == expected
    )


def test_previous_or_base_keeps_an_explicit_none_override() -> None:
    """Return ``None`` when the key is present in previous holding ``None``.

    Membership decides the fallback, not truthiness: a nullable field whose
    override was explicitly ``None`` had ``None`` as its previous effective
    value, so falling back to the YAML/env base here would report a value that
    was never in effect.
    """
    proxy, _ = _make_proxies()
    change = SnapshotChange({"INVENTORY_ENDPOINT": None}, {})
    assert previous_or_base(change, proxy, "INVENTORY_ENDPOINT") is None


@pytest.mark.asyncio
async def test_refresh_all_swaps_snapshot(
    session_maker: async_sessionmaker,
) -> None:
    """``refresh_all`` populates the proxy snapshot from the override table."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="CONNECTIVITY_CHECK_DEFAULT",
                value=override_value,
            ),
        )

    await refresh_all(lambda: session_maker, registry)
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value


@pytest.mark.asyncio
async def test_refresh_all_falls_back_when_a_row_becomes_undecryptable(
    session_maker: async_sessionmaker,
) -> None:
    """Serve the YAML/env value once a published override stops decrypting.

    ``publish_snapshot`` replaces the snapshot wholesale, so a row that can no
    longer be read is *absent* rather than stale, and the proxy defers to the
    wrapped instance. Falling back to configuration beats serving a credential
    the deployment can no longer verify.
    """
    proxy: OverridableSettingsProxy = OverridableSettingsProxy(
        Settings, setting_class=Settings.__name__
    )
    registry = {SettingClassEnum.SETTINGS: ProxyEntry(proxy, Settings)}
    api_key = "pmm-api-key-published"
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=SETTINGS_TOKEN,
                key="PMM__api_key",
                value=encrypt(api_key),
            ),
        )

    await refresh_all(lambda: session_maker, registry)
    published = proxy.PMM.api_key
    assert published.get_secret_value() == api_key

    foreign = Fernet(Fernet.generate_key()).encrypt(api_key.encode()).decode("ascii")
    async with session_maker() as session:
        stored = await SettingsOverrideManager.first(session, key="PMM__api_key")
        stored.value = foreign
        stored.updated_by = "tester"
        await SettingsOverrideManager.save(session, stored)

    await refresh_all(lambda: session_maker, registry)

    assert proxy.PMM.api_key != published
    assert proxy.PMM.api_key == proxy._resolve().PMM.api_key


@pytest.mark.asyncio
async def test_refresh_all_skips_unregistered_class_row(
    session_maker: async_sessionmaker,
) -> None:
    """Leave an override row for an unwired class in the table.

    Startup still publishes the wired class's snapshot; the unregistered row
    is neither applied nor deleted.
    """
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="CONNECTIVITY_CHECK_DEFAULT",
                value=override_value,
            ),
        )
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class="UNREGISTERED_SETTINGS",
                key="WHATEVER",
                value=True,
            ),
        )

    await refresh_all(lambda: session_maker, registry)
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value

    async with session_maker() as session:
        leftover = await SettingsOverrideManager.list(
            session, setting_class="UNREGISTERED_SETTINGS"
        )
        wired = await SettingsOverrideManager.list(
            session, setting_class=EXTENSIONS_SETTINGS_TOKEN
        )
    assert len(leftover) == 1
    assert leftover[0].key == "WHATEVER"
    assert len(wired) == 1


@pytest.mark.asyncio
async def test_refresh_all_retains_previous_snapshot_on_error(
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors during ``build_snapshot`` keep the previous snapshot intact."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    proxy._set_snapshot({"CONNECTIVITY_CHECK_DEFAULT": override_value})

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.core.settings_override.lifecycle.build_snapshot", _boom)
    await refresh_all(lambda: session_maker, registry)
    # Previous snapshot is retained -- the refresh did not clobber it.
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value


@pytest.mark.asyncio
async def test_refresh_all_rolls_back_session_between_proxies(
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-cycle failure rolls the session back so the next proxy can still load.

    Without the rollback, Postgres would leave the session in
    ``InFailedSqlTransaction`` after the first proxy's failed query and
    every subsequent proxy on the same session would also fail. SQLite
    doesn't reproduce that aborted-tx state, so we simulate it by spying
    on ``session.rollback`` and asserting it fires between proxies, and by
    making the first proxy fail while asserting the second still picks up
    its row from the DB.
    """
    extensions_proxy: OverridableSettingsProxy = OverridableSettingsProxy(
        ExtensionsSettings, setting_class=ExtensionsSettings.__name__
    )
    tasks_proxy: OverridableSettingsProxy = OverridableSettingsProxy(
        TasksSettings, setting_class=TasksSettings.__name__
    )
    registry = {
        SettingClassEnum.EXTENSIONS_SETTINGS: ProxyEntry(
            extensions_proxy, ExtensionsSettings
        ),
        SettingClassEnum.TASKS_SETTINGS: ProxyEntry(tasks_proxy, TasksSettings),
    }
    tasks_override = 7200
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="STALENESS_THRESHOLD_SECONDS",
                value=tasks_override,
            ),
        )

    real_build_snapshot = build_snapshot
    call_count = {"n": 0}

    async def _fail_first(
        session: object,
        settings_cls: type,
        base_settings: object = None,
    ) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first proxy explodes mid-cycle")
        return await real_build_snapshot(session, settings_cls, base_settings)

    monkeypatch.setattr(
        "app.core.settings_override.lifecycle.build_snapshot", _fail_first
    )

    rollback_calls = {"n": 0}
    real_rollback = AsyncSession.rollback

    async def _counting_rollback(self: AsyncSession) -> None:
        rollback_calls["n"] += 1
        await real_rollback(self)

    monkeypatch.setattr(AsyncSession, "rollback", _counting_rollback)

    await refresh_all(lambda: session_maker, registry)

    assert rollback_calls["n"] >= 1, "session.rollback() was not called after failure"
    # Second proxy must have loaded successfully after the rollback -- the
    # failure on proxy 1 must NOT block the manager.list() call on proxy 2.
    assert tasks_override == tasks_proxy.STALENESS_THRESHOLD_SECONDS


@pytest.mark.asyncio
async def test_start_refresh_task_runs_initial_load(
    session_maker: async_sessionmaker,
) -> None:
    """``start_refresh_task`` awaits an initial refresh before returning the task."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="CONNECTIVITY_CHECK_DEFAULT",
                value=override_value,
            ),
        )

    task = await start_refresh_task(
        lambda: session_maker, registry, interval=timedelta(seconds=3600)
    )
    try:
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_start_refresh_task_cancellable(
    session_maker: async_sessionmaker,
) -> None:
    """The background task is cancellable and shuts down cleanly."""
    _proxy, registry = _make_proxies()
    task = await start_refresh_task(
        lambda: session_maker, registry, interval=timedelta(seconds=3600)
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_start_refresh_task_creates_a_named_periodic_task(
    session_maker: async_sessionmaker,
) -> None:
    """Keep the web-path periodic ``_loop`` task after the worker pull rewrite."""
    _proxy, registry = _make_proxies()
    task = await start_refresh_task(
        lambda: session_maker, registry, interval=timedelta(seconds=3600)
    )
    try:
        assert task.get_name() == "settings-override-refresher"
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_settings_override_refresher_keeps_the_periodic_task(
    session_maker: async_sessionmaker,
) -> None:
    """Assert the web lifespan still drives a timer-based background refresher."""
    _proxy, registry = _make_proxies()
    async with settings_override_refresher(
        lambda: session_maker,
        registry,
        interval=timedelta(seconds=3600),
        enabled=True,
    ):
        named = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "settings-override-refresher"
        ]
        assert len(named) == 1
        assert not named[0].done()


@pytest.mark.asyncio
async def test_settings_override_refresher_disabled_creates_no_periodic_task(
    session_maker: async_sessionmaker,
) -> None:
    """Leave the web lifespan a no-op when the refresher is disabled."""
    _proxy, registry = _make_proxies()
    async with settings_override_refresher(
        lambda: session_maker,
        registry,
        interval=timedelta(seconds=3600),
        enabled=False,
    ):
        named = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "settings-override-refresher"
        ]
        assert named == []


@pytest.mark.asyncio
async def test_bounded_seed_completes_and_returns_true(
    session_maker: async_sessionmaker,
) -> None:
    """Seed overrides through the shared helper and report success."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="CONNECTIVITY_CHECK_DEFAULT",
                value=override_value,
            ),
        )

    seeded, pending = await bounded_seed(
        lambda: session_maker, registry, seed_timeout=5.0
    )

    assert seeded is True
    assert pending is None
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value


@pytest.mark.asyncio
async def test_bounded_seed_expiry_returns_false_and_logs_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancel a hanging seed without awaiting unwind; log ERROR naming the budget."""
    _proxy, registry = _make_proxies()
    seed_timeout = 0.05

    with caplog.at_level("ERROR", logger="app.core.settings_override.lifecycle"):
        seeded, pending = await asyncio.wait_for(
            bounded_seed(hanging_session_maker_factory, registry, seed_timeout),
            timeout=1.0,
        )

    assert seeded is False
    # HangingSession blocks on enter; cancel completes promptly once delivered.
    if pending is not None and not pending.done():
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
    assert any(
        record.levelname == "ERROR"
        and f"{seed_timeout:.2f}s" in record.message
        and "incomplete" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_bounded_refresh_logs_exception_raised_while_unwinding(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log a real failure that surfaces after cancel, not only CancelledError."""
    _proxy, registry = _make_proxies()

    async def _fail_after_cancel(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("session cleanup failed") from None

    monkeypatch.setattr(
        "app.core.settings_override.lifecycle.refresh_all", _fail_after_cancel
    )

    with caplog.at_level("WARNING", logger="app.core.settings_override.lifecycle"):
        completed, pending = await bounded_refresh(lambda: None, registry, budget=0.05)
        assert completed is False
        assert pending is not None
        with suppress(RuntimeError):
            await pending

    assert any(
        record.levelname == "WARNING"
        and "unwinding after cancellation" in record.message
        and "session cleanup failed" in record.message
        for record in caplog.records
    )


_NOMAD_CALLBACK_KEY = (SettingClassEnum.TASKS_SETTINGS, "NOMAD")
_NOMAD_LEAF_TIMEOUT = 30


def _make_tasks_proxy_registry() -> tuple[
    TasksSettings, dict[SettingClassEnum, ProxyEntry]
]:
    """Construct the global Tasks proxy and a single-entry registry."""
    registry = {
        SettingClassEnum.TASKS_SETTINGS: ProxyEntry(tasks_settings, TasksSettings),
    }
    return tasks_settings, registry


async def _seed_nomad_timeout_override(
    session_maker: async_sessionmaker, *, value: int = _NOMAD_LEAF_TIMEOUT
) -> None:
    """Insert a ``NOMAD__TIMEOUT`` per-leaf override row."""
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="NOMAD__TIMEOUT",
                value=value,
            ),
        )


@pytest.mark.asyncio
async def test_refresh_all_nomad_reconcile_stable_under_per_leaf_override(
    session_maker: async_sessionmaker,
) -> None:
    """Record reconcile-churn under an active per-child ``NOMAD`` override.

    A per-leaf override stores a merged
    ``NomadExecutor`` in the snapshot (not a fingerprint dict). Each
    ``build_snapshot`` produces a fresh instance, but Pydantic field equality
    makes consecutive snapshots compare equal, so ``fire_change_callbacks`` does
    **not** notify ``_reconcile_nomad`` on every cycle when the effective config
    is unchanged. When a callback does fire,
    :meth:`NomadLifecycle.reconcile` compares ``model_dump(mode="json")`` and
    does not rebuild the entered executor.
    """
    tasks_settings._set_snapshot({})
    try:
        await _seed_nomad_timeout_override(session_maker)
        _proxy, registry = _make_tasks_proxy_registry()

        # Seed the override snapshot without firing rebind callbacks.
        await refresh_all(lambda: session_maker, registry)

        app = FastAPI()
        reconcile_calls = 0

        async with NomadLifecycle(app) as holder:
            executor_before = holder.current
            real_reconcile = holder.reconcile

            async def _counting_reconcile() -> None:
                nonlocal reconcile_calls
                reconcile_calls += 1
                await real_reconcile()

            holder.reconcile = _counting_reconcile

            tasks_app.state.nomad_lifecycle = holder
            try:
                # Baseline refresh under the live holder (not counted).
                await refresh_all(lambda: session_maker, registry)
                snapshot_before = dict(tasks_settings.get_snapshot())
                executor_after_baseline = holder.current

                # Measure one further cycle with rebind callbacks wired.
                await refresh_all(
                    lambda: session_maker,
                    registry,
                    {_NOMAD_CALLBACK_KEY: _reconcile_nomad},
                )
                snapshot_after = dict(tasks_settings.get_snapshot())
                executor_after = holder.current
            finally:
                tasks_app.state.nomad_lifecycle = None

        nomad_before = snapshot_before["NOMAD"]
        nomad_after = snapshot_after["NOMAD"]
        assert isinstance(nomad_before, NomadExecutor)
        assert isinstance(nomad_after, NomadExecutor)
        assert nomad_before.timeout == _NOMAD_LEAF_TIMEOUT
        assert nomad_after.timeout == _NOMAD_LEAF_TIMEOUT

        before_config = nomad_before.model_dump(mode="json")
        after_config = nomad_after.model_dump(mode="json")
        assert before_config == after_config

        # Fresh merged instances compare equal on declared fields, so callbacks
        # stay quiet; if equality ever regresses, reconcile must still no-op.
        snapshots_compare_equal = nomad_before == nomad_after
        assert snapshots_compare_equal

        # reconcile's JSON guard must keep the live entered executor stable.
        assert executor_before is executor_after_baseline
        assert executor_after_baseline is executor_after
        assert reconcile_calls == 0
    finally:
        tasks_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_refresh_all_fires_callback_for_changed_key(
    session_maker: async_sessionmaker,
) -> None:
    """A callback fires for a key whose value changed between snapshots."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    await seed_connectivity_override(session_maker, value=override_value)
    fired = []

    async def _callback(_: object) -> None:
        fired.append(True)

    await refresh_all(
        lambda: session_maker, registry, {CONNECTIVITY_CALLBACK_KEY: _callback}
    )
    assert fired == [True]
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value


@pytest.mark.asyncio
async def test_fire_change_callbacks_delivers_snapshot_change_on_delete() -> None:
    """Deliver a SnapshotChange, with the changed key absent from current on delete."""
    previous = {"CONNECTIVITY_CHECK_DEFAULT": True}
    current: dict[str, object] = {}
    received: list[object] = []

    async def _callback(change: object) -> None:
        received.append(change)

    await fire_change_callbacks(
        {CONNECTIVITY_CALLBACK_KEY: _callback},
        SettingClassEnum.EXTENSIONS_SETTINGS,
        previous,
        current,
    )

    assert len(received) == 1
    change = received[0]
    assert isinstance(change, SnapshotChange)
    assert change.previous == previous
    assert change.current == current
    assert "CONNECTIVITY_CHECK_DEFAULT" not in change.current
    assert change.previous["CONNECTIVITY_CHECK_DEFAULT"] is True


@pytest.mark.asyncio
async def test_fire_change_callbacks_hands_every_callback_the_whole_change() -> None:
    """Hand each changed key's callback both snapshots, not just its own key.

    Two keys change in one republish, so the loop runs twice. The payload is
    snapshot-wide rather than per-key, which only a multi-key cycle can pin: a
    per-key rebuild would pass each callback a mapping holding its own key alone.
    """
    previous: dict[str, object] = {"CONNECTIVITY_CHECK_DEFAULT": True, "APP_DRAIN": 1}
    current: dict[str, object] = {"CONNECTIVITY_CHECK_DEFAULT": False}
    received: dict[str, SnapshotChange] = {}

    def _recorder(name: str):
        async def _callback(change: SnapshotChange) -> None:
            received[name] = change

        return _callback

    await fire_change_callbacks(
        {
            CONNECTIVITY_CALLBACK_KEY: _recorder("connectivity"),
            (SettingClassEnum.EXTENSIONS_SETTINGS, "APP_DRAIN"): _recorder("drain"),
        },
        SettingClassEnum.EXTENSIONS_SETTINGS,
        previous,
        current,
    )

    assert sorted(received) == ["connectivity", "drain"]
    for change in received.values():
        assert change.previous == previous
        assert change.current == current


@pytest.mark.asyncio
async def test_refresh_all_skips_callback_for_unchanged_key(
    session_maker: async_sessionmaker,
) -> None:
    """A callback does not fire when its key's value is unchanged."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    await seed_connectivity_override(session_maker, value=override_value)
    await refresh_all(lambda: session_maker, registry)
    fired = []

    async def _callback(_: object) -> None:
        fired.append(True)

    await refresh_all(
        lambda: session_maker, registry, {CONNECTIVITY_CALLBACK_KEY: _callback}
    )
    assert fired == []


@pytest.mark.asyncio
async def test_refresh_all_isolates_callback_exception(
    session_maker: async_sessionmaker,
) -> None:
    """A raising callback is caught; the snapshot is still published."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    await seed_connectivity_override(session_maker, value=override_value)

    async def _boom(_: object) -> None:
        raise RuntimeError("callback boom")

    await refresh_all(
        lambda: session_maker, registry, {CONNECTIVITY_CALLBACK_KEY: _boom}
    )
    assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value


@pytest.mark.asyncio
async def test_start_refresh_task_initial_does_not_fire_unmarked_callbacks(
    session_maker: async_sessionmaker,
) -> None:
    """The initial inline refresh publishes the snapshot but fires no unmarked callback."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    await seed_connectivity_override(session_maker, value=override_value)
    fired = []

    async def _callback(_: object) -> None:
        fired.append(True)

    task = await start_refresh_task(
        lambda: session_maker,
        registry,
        interval=timedelta(seconds=3600),
        callbacks={CONNECTIVITY_CALLBACK_KEY: _callback},
    )
    try:
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
        assert fired == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_start_refresh_task_fires_callback_on_loop_change(
    session_maker: async_sessionmaker,
) -> None:
    """A change inserted after startup fires the callback on a later loop cycle."""
    proxy, registry = _make_proxies()
    fired = asyncio.Event()

    async def _callback(_: object) -> None:
        fired.set()

    task = await start_refresh_task(
        lambda: session_maker,
        registry,
        interval=timedelta(milliseconds=50),
        callbacks={CONNECTIVITY_CALLBACK_KEY: _callback},
    )
    try:
        assert not fired.is_set()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)
        await asyncio.wait_for(fired.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_refresh_picks_up_changes_on_next_cycle(
    session_maker: async_sessionmaker,
) -> None:
    """Adding an override row between cycles becomes visible after the next refresh."""
    proxy, registry = _make_proxies()
    override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
    task = await start_refresh_task(
        lambda: session_maker, registry, interval=timedelta(milliseconds=50)
    )
    try:
        async with session_maker() as session:
            await SettingsOverrideManager.create(
                session,
                SettingOverride(
                    setting_class=EXTENSIONS_SETTINGS_TOKEN,
                    key="CONNECTIVITY_CHECK_DEFAULT",
                    value=override_value,
                ),
            )
        for _ in range(50):
            await asyncio.sleep(0.05)
            try:
                if proxy.CONNECTIVITY_CHECK_DEFAULT is override_value:
                    return
            except AttributeError:
                continue
        pytest.fail("Refresher did not observe the inserted override row")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestResolveRefresherOptions:
    """Cover the fallback that fills unset refresher options from settings."""

    def test_explicit_values_pass_through(self) -> None:
        """Assert a caller supplying both options gets them back untouched."""
        assert resolve_refresher_options(timedelta(seconds=7), enabled=False) == (
            timedelta(seconds=7),
            False,
        )

    def test_unset_values_read_the_configured_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert both options fall back to the live settings when unset."""
        monkeypatch.setattr(
            settings.SETTINGS_OVERRIDE, "REFRESH_INTERVAL", timedelta(seconds=11)
        )
        monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", True)

        assert resolve_refresher_options(None, enabled=None) == (
            timedelta(seconds=11),
            True,
        )

    def test_each_option_falls_back_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert one supplied option does not suppress the other's fallback."""
        monkeypatch.setattr(
            settings.SETTINGS_OVERRIDE, "REFRESH_INTERVAL", timedelta(seconds=13)
        )
        monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", False)

        assert resolve_refresher_options(timedelta(seconds=3), enabled=None) == (
            timedelta(seconds=3),
            False,
        )
        assert resolve_refresher_options(None, enabled=True) == (
            timedelta(seconds=13),
            True,
        )


class TestFireBootCallbacks:
    """Cover the boot-time firing of callbacks the seed cannot reproduce itself."""

    @pytest.mark.asyncio
    async def test_marked_callback_fires_once_after_the_seed_publishes_its_key(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Fire a marked callback exactly once when the seed publishes its override."""
        proxy, registry = _make_proxies()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)
        fired: list[SnapshotChange] = []

        await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={
                CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))
            },
        )

        assert len(fired) == 1
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value

    @pytest.mark.asyncio
    async def test_unmarked_callback_stays_silent_at_boot(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Keep the seed silent for a callback whose effect boot reproduces."""
        proxy, registry = _make_proxies()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)
        fired: list[SnapshotChange] = []

        await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={CONNECTIVITY_CALLBACK_KEY: recording_callback(fired)},
        )

        assert fired == []
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value

    @pytest.mark.asyncio
    async def test_marked_callback_stays_silent_without_an_override_row(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Skip a marked callback whose key the seed published no override for."""
        _proxy, registry = _make_proxies()
        fired: list[SnapshotChange] = []

        await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={
                CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))
            },
        )

        assert fired == []

    @pytest.mark.asyncio
    async def test_marked_callback_for_an_unwired_class_stays_silent(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Skip a marked callback registered under a class this seed does not refresh."""
        _proxy, registry = _make_proxies()
        fired: list[SnapshotChange] = []

        await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={
                (SettingClassEnum.SETTINGS, "LOGGING"): fire_on_boot(
                    recording_callback(fired)
                )
            },
        )

        assert fired == []

    @pytest.mark.asyncio
    async def test_failed_publish_fires_nothing(
        self, session_maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip the marked callback when the proxy's publish failed and its snapshot stayed put."""
        proxy, registry = _make_proxies()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)
        fired: list[SnapshotChange] = []

        async def _boom(
            _session: AsyncSession,
            _settings_cls: type[BaseYamlSettings],
            _base_settings: object = None,
        ) -> None:
            raise RuntimeError("publish failed")

        monkeypatch.setattr(
            "app.core.settings_override.lifecycle.build_snapshot", _boom
        )

        seeded, _pending = await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={
                CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))
            },
        )

        assert seeded is True
        assert fired == []
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is not override_value

    @pytest.mark.asyncio
    async def test_a_raising_marked_callback_is_logged_and_the_seed_completes(
        self, session_maker: async_sessionmaker, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log a boot callback failure at ERROR and still publish the snapshot."""
        proxy, registry = _make_proxies()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)

        @fire_on_boot
        async def _boom(_: SnapshotChange) -> None:
            raise RuntimeError("callback boom")

        with caplog.at_level("ERROR", logger="app.core.settings_override.lifecycle"):
            seeded, _pending = await bounded_seed(
                lambda: session_maker,
                registry,
                seed_timeout=None,
                callbacks={CONNECTIVITY_CALLBACK_KEY: _boom},
            )

        assert seeded is True
        assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
        assert any(
            record.levelname == "ERROR"
            and "CONNECTIVITY_CHECK_DEFAULT" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_boot_change_carries_an_empty_previous_and_the_seeded_current(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Hand the callback an empty previous so the base falls back to YAML/env."""
        proxy, registry = _make_proxies()
        base_value = ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=not base_value)
        fired: list[SnapshotChange] = []

        await bounded_seed(
            lambda: session_maker,
            registry,
            seed_timeout=None,
            callbacks={
                CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))
            },
        )

        (change,) = fired
        assert change.previous == {}
        assert change.current["CONNECTIVITY_CHECK_DEFAULT"] is not base_value
        assert (
            previous_or_base(change, proxy, "CONNECTIVITY_CHECK_DEFAULT") is base_value
        )

    @pytest.mark.asyncio
    async def test_a_proxy_published_before_the_budget_expired_has_fired(
        self, session_maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fire with the publish, not after the wait: a partial seed still rebinds.

        The first proxy publishes without touching the database, so its marked
        callback runs in the seed task's very first step; the second proxy then
        hangs past the budget. When ``bounded_seed`` gives up, the first
        callback must already have fired rather than being deferred to a fire
        step that never runs for an expired seed.
        """
        extensions_proxy, registry = _make_proxies()
        registry[SettingClassEnum.TASKS_SETTINGS] = ProxyEntry(
            OverridableSettingsProxy(
                TasksSettings, setting_class=TasksSettings.__name__
            ),
            TasksSettings,
        )
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        fired: list[SnapshotChange] = []

        async def _publish_extensions_then_hang(
            proxy: OverridableSettingsProxy,
            _session: AsyncSession,
            settings_cls: type,
        ) -> None:
            if settings_cls is TasksSettings:
                await asyncio.Event().wait()
            proxy._set_snapshot({"CONNECTIVITY_CHECK_DEFAULT": override_value})

        monkeypatch.setattr(
            "app.core.settings_override.lifecycle.publish_snapshot",
            _publish_extensions_then_hang,
        )

        seeded, pending = await asyncio.wait_for(
            bounded_seed(
                lambda: session_maker,
                registry,
                seed_timeout=0.05,
                callbacks={
                    CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))
                },
            ),
            timeout=1.0,
        )

        try:
            assert seeded is False
            assert len(fired) == 1
            assert extensions_proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending

    @pytest.mark.asyncio
    async def test_a_periodic_refresh_fires_on_change_only(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Fire a marked callback at boot, skip an unchanged cycle, fire on a change."""
        _proxy, registry = _make_proxies()
        base_value = ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=not base_value)
        fired: list[SnapshotChange] = []
        callbacks = {CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(fired))}

        await bounded_seed(
            lambda: session_maker, registry, seed_timeout=None, callbacks=callbacks
        )
        await refresh_all(lambda: session_maker, registry, callbacks)
        assert len(fired) == 1

        await clear_connectivity_override(session_maker)
        await refresh_all(lambda: session_maker, registry, callbacks)

        assert [change.current for change in fired] == [
            {"CONNECTIVITY_CHECK_DEFAULT": not base_value},
            {},
        ]

    @pytest.mark.asyncio
    async def test_start_refresh_task_seed_fires_only_marked_callbacks(
        self, session_maker: async_sessionmaker
    ) -> None:
        """Fire the marked subset from the web lifespan's inline seed, nothing else."""
        proxy, registry = _make_proxies()
        override_value = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await seed_connectivity_override(session_maker, value=override_value)
        marked: list[SnapshotChange] = []
        unmarked: list[SnapshotChange] = []

        task = await start_refresh_task(
            lambda: session_maker,
            registry,
            interval=timedelta(seconds=3600),
            callbacks={
                CONNECTIVITY_CALLBACK_KEY: fire_on_boot(recording_callback(marked)),
                (SettingClassEnum.EXTENSIONS_SETTINGS, "INVENTORY_ENDPOINT"): (
                    recording_callback(unmarked)
                ),
            },
        )
        try:
            assert proxy.CONNECTIVITY_CHECK_DEFAULT is override_value
            assert len(marked) == 1
            assert unmarked == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
