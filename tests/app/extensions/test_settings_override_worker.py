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

"""Tests for the PMM Extensions worker's settings-override wiring."""

import asyncio
import logging
import logging.config
import time
from collections.abc import Iterator
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    create_async_engine,
)
from sqlmodel import SQLModel
from sqlmodel.pool import StaticPool

from app.core.alerts.config import alert_settings, AlertSettings
from app.core.config import BaseYamlSettings, LogLevel, Settings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override import lifecycle
from app.core.settings_override.api.routes import AppOwnedClassEntry
from app.core.settings_override.constants import (
    ALERT_SETTINGS,
    EXTENSIONS_SETTINGS,
    SETTINGS,
    SNIPPETS_SETTINGS,
)
from app.core.settings_override.lifecycle import refresh_all
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import (
    setting_class_token,
    SettingOverride,
)
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field
from app.core.settings_override.worker import SEED_TIMEOUT_FRACTION
from app.core.utils import json_serializer
from app.extensions import settings_override as extensions_worker
from app.extensions.config import extensions_settings, ExtensionsSettings
from app.extensions.deps import resolve_pmm_api
from app.extensions.settings_override import (
    build_extensions_override_proxies,
    refresh_extensions_overrides_if_due,
    republish_extensions_settings_snapshot,
    start_extensions_settings_override_refresher,
    stop_extensions_settings_override_refresher,
    WORKER_OVERRIDE_CALLBACKS,
)
from app.tasks.celery import build_tasks_override_proxies
from tests.app.core.settings_override.conftest import (
    assert_registry_keyed_by_class_name,
    BOUNDED_SEED,
    HangingSession,
    recording_bounded_seed,
)
from tests.app.db_schema import apply_schema

EXTENSIONS_CORE_CLASSES = frozenset(
    {
        EXTENSIONS_SETTINGS,
        SNIPPETS_SETTINGS,
        SETTINGS,
        ALERT_SETTINGS,
    }
)
PMM_ENDPOINT = "https://pmm-worker.example.org"
EXTENSIONS_OVERRIDE_KEY = "SYNC_REFRESH_TIME"
EXTENSIONS_OVERRIDE_VALUE = 42
WorkerLoopEnv = tuple[asyncio.AbstractEventLoop, async_sessionmaker]


class _AppOwnedSettings(BaseYamlSettings):
    """Stand in for a settings class an activated app declares.

    :param LABEL: An arbitrary hot field; the builder never reads it.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["TEST_APP_OWNED"]
    LABEL: str = hot_field("default")


def _app_owned_entry(setting_class: str) -> AppOwnedClassEntry:
    """Build an app-owned registration for ``setting_class``."""
    return AppOwnedClassEntry(
        setting_class=setting_class,
        settings_cls=_AppOwnedSettings,
        proxy=OverridableSettingsProxy(_AppOwnedSettings),
        app_key="test-app",
    )


async def _create_schema(engine: AsyncEngine) -> None:
    """Create every SQLModel table on ``engine``."""
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)


async def _upsert_override(
    maker: async_sessionmaker,
    *,
    settings_cls: type[BaseYamlSettings],
    key: str,
    value: object,
) -> None:
    """Insert or replace a single active ``SettingOverride`` row through ``maker``.

    :param maker: Async session maker bound to the override store.
    :param settings_cls: Settings class whose :func:`~app.core.settings_override.models.setting_class_token`
        is persisted as ``setting_class`` on the row.
    :param key: Canonical override key (``SCREAMING_SNAKE`` or nested path).
    :param value: JSON-serializable override payload.
    """
    token = setting_class_token(settings_cls)
    async with maker() as session:
        await SettingsOverrideManager.delete_where(
            session, setting_class=token, key=key
        )
        await SettingsOverrideManager.create(
            session,
            SettingOverride(setting_class=token, key=key, value=value),
        )


@pytest.fixture(name="no_app_owned_classes")
def no_app_owned_classes_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose the PMM Extensions registry as if no activated app declared a class."""
    monkeypatch.setattr(extensions_worker, "collect_app_owned_settings_classes", list)


@pytest.fixture(name="override_session_maker")
def override_session_maker_fixture() -> Iterator[async_sessionmaker]:
    """Provide an in-memory SQLite session maker with the PMM Extensions schema created."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_create_schema(engine))
    yield get_async_session_maker_from_engine(engine)
    loop.run_until_complete(engine.dispose())
    loop.close()


@pytest.fixture(name="worker_loop_env")
def worker_loop_env_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[WorkerLoopEnv]:
    """Wire a fresh event loop and in-memory PMM Extensions DB as a prefork worker child.

    Mirrors the ``worker_process_init`` runtime: a dedicated ``celery.loop``, the
    refresher enabled, ``get_async_session_maker`` pointed at an in-memory
    engine, no app-owned classes, and the per-process refresher reset. Yields
    ``(loop, session_maker)`` and stops any started refresher on teardown.
    """
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(extensions_worker.celery, "loop", loop)
    extensions_worker._refresher.stop()
    monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", True)
    monkeypatch.setattr(extensions_worker, "collect_app_owned_settings_classes", list)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    maker = get_async_session_maker_from_engine(engine)
    monkeypatch.setattr(extensions_worker, "get_async_session_maker", lambda: maker)
    loop.run_until_complete(_create_schema(engine))
    yield loop, maker
    stop_extensions_settings_override_refresher()
    loop.run_until_complete(engine.dispose())
    loop.close()


class TestBuildExtensionsOverrideProxies:
    """Cover the shared PMM Extensions proxy-set builder."""

    @pytest.mark.usefixtures("no_app_owned_classes")
    def test_builder_registers_the_extensions_core_classes(self) -> None:
        """Compose exactly PMM Extensions' own entries when no app declares one."""
        assert set(build_extensions_override_proxies()) == EXTENSIONS_CORE_CLASSES

    def test_builder_includes_app_owned_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Carry an app-declared class alongside PMM Extensions' own entries."""
        entry = _app_owned_entry("AlertsSettings")
        monkeypatch.setattr(
            extensions_worker, "collect_app_owned_settings_classes", lambda: [entry]
        )

        proxies = build_extensions_override_proxies()

        assert set(proxies) == EXTENSIONS_CORE_CLASSES | {"AlertsSettings"}
        assert proxies["AlertsSettings"].proxy is entry.proxy

    def test_extensions_entries_win_over_an_app_owned_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the PMM Extensions refresher as the sole owner of a shared core proxy."""
        entry = _app_owned_entry(SETTINGS)
        monkeypatch.setattr(
            extensions_worker, "collect_app_owned_settings_classes", lambda: [entry]
        )

        proxies = build_extensions_override_proxies()

        assert proxies[SETTINGS].proxy is settings

    @pytest.mark.usefixtures("no_app_owned_classes")
    def test_builder_shares_no_keys_with_the_tasks_registry(self) -> None:
        """Keep the two worker refreshers publishing into disjoint proxy sets.

        A prefork child now runs both, and each resolves against a different
        database, so a shared key would have the two publish over each other.
        Both sides are read from their own builder rather than a literal, so
        adding a class to either one is what moves this assertion.
        """
        assert not set(build_extensions_override_proxies()) & set(
            build_tasks_override_proxies()
        )

    def test_builder_keys_every_entry_by_class_name(self) -> None:
        """Key the real app-owned and core entries by the class ``__name__``."""
        assert_registry_keyed_by_class_name(build_extensions_override_proxies())


class TestWorkerOverrideCallbacks:
    """Cover the callback subset the worker refresher registers."""

    def test_registry_is_pmm_and_logging(self) -> None:
        """Pin the disposition: PMM invalidation plus LOGGING dictConfig rebind."""
        assert set(WORKER_OVERRIDE_CALLBACKS) == {
            (SETTINGS, "PMM"),
            (SETTINGS, "LOGGING"),
        }


class TestExtensionsWorkerHandlers:
    """Cover the worker_process_init / worker_process_shutdown PMM Extensions handlers."""

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_arms_a_refresher(self) -> None:
        """Seed and arm this child's PMM Extensions refresher on ``worker_process_init``."""
        start_extensions_settings_override_refresher()

        assert extensions_worker._refresher._armed

    def test_disabled_arms_nothing(
        self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Arm nothing when the refresher is disabled."""
        monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", False)
        extensions_worker._refresher.stop()
        refresh = mocker.patch("app.core.settings_override.lifecycle.refresh_all")

        start_extensions_settings_override_refresher()

        refresh.assert_not_called()
        assert not extensions_worker._refresher._armed

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_is_idempotent_when_already_armed(self) -> None:
        """Keep the armed refresher and skip a second seed on re-entry."""
        start_extensions_settings_override_refresher()
        first_proxies = extensions_worker._refresher._proxies
        first_stamp = extensions_worker._refresher._last_refresh

        start_extensions_settings_override_refresher()

        assert extensions_worker._refresher._armed
        assert extensions_worker._refresher._proxies is first_proxies
        assert extensions_worker._refresher._last_refresh == first_stamp

    @pytest.mark.usefixtures("worker_loop_env")
    def test_shutdown_disarms_started_refresher(self) -> None:
        """Disarm the started refresher so further boundaries no-op."""
        start_extensions_settings_override_refresher()

        stop_extensions_settings_override_refresher()

        assert not extensions_worker._refresher._armed
        assert extensions_worker._refresher._proxies is None

    def test_shutdown_is_noop_when_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handle a never-started refresher as a no-op on shutdown."""
        extensions_worker._refresher.stop()

        stop_extensions_settings_override_refresher()

        assert not extensions_worker._refresher._armed

    def test_init_seeds_a_extensions_override_into_the_worker_proxy(
        self, worker_loop_env: WorkerLoopEnv
    ) -> None:
        """Publish a seeded PMM Extensions side override during the initial inline refresh."""
        loop, maker = worker_loop_env
        loop.run_until_complete(
            _upsert_override(
                maker,
                settings_cls=AlertSettings,
                key="SOURCE_PREFIX",
                value="worker-",
            )
        )

        start_extensions_settings_override_refresher()

        assert alert_settings.SOURCE_PREFIX == "worker-"

    def test_init_forwards_a_budget_from_worker_proc_alive_timeout(
        self,
        worker_loop_env: WorkerLoopEnv,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Derive the seed budget from Celery's prefork liveness deadline."""
        recorded: dict[str, object] = {}
        monkeypatch.setattr(BOUNDED_SEED, recording_bounded_seed(recorded))
        monkeypatch.setattr(
            extensions_worker.celery.conf, "worker_proc_alive_timeout", 6.0
        )

        start_extensions_settings_override_refresher()

        assert recorded["seed_timeout"] == pytest.approx(6.0 * SEED_TIMEOUT_FRACTION)

    def test_init_arms_when_the_seed_hangs(
        self,
        worker_loop_env: WorkerLoopEnv,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep the child armed and due for a boundary refresh after seed expiry."""
        monkeypatch.setattr(
            extensions_worker, "get_async_session_maker", lambda: HangingSession
        )
        monkeypatch.setattr(
            extensions_worker.celery.conf, "worker_proc_alive_timeout", 0.1
        )

        start_extensions_settings_override_refresher()

        refresher = extensions_worker._refresher
        assert refresher._armed
        assert time.monotonic() - refresher._last_refresh >= refresher._interval_seconds

    @pytest.mark.usefixtures("worker_loop_env")
    def test_task_prerun_receiver_calls_maybe_refresh(
        self, mocker: MockerFixture
    ) -> None:
        """Wire ``refresh_extensions_overrides_if_due`` to the boundary due-check."""
        maybe = mocker.spy(extensions_worker._refresher, "maybe_refresh")

        refresh_extensions_overrides_if_due()

        maybe.assert_called_once_with()

    @pytest.mark.usefixtures("worker_loop_env")
    def test_init_registers_worker_override_callbacks(self) -> None:
        """Store WORKER_OVERRIDE_CALLBACKS for boundary rebinds."""
        start_extensions_settings_override_refresher()

        assert extensions_worker._refresher._callbacks is WORKER_OVERRIDE_CALLBACKS


class TestWorkerPmmClientInvalidation:
    """Verify a same-endpoint PMM credential override reaches worker tasks."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_app_owned_classes")
    async def test_api_key_only_override_evicts_the_cached_client(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Hand a fresh client with the new key to the next ``resolve_pmm_api()``.

        ``ClientRegistry.IMMUTABLE_KEYS`` excludes ``api_key``, so republishing
        the ``PMM`` snapshot alone leaves the stale client cached; only the
        ported invalidation callback evicts it.
        """
        proxies = build_extensions_override_proxies()
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="PMM",
            value={"endpoint": PMM_ENDPOINT, "api_key": "old-key"},
        )
        await refresh_all(lambda: override_session_maker, proxies)
        stale = await resolve_pmm_api()
        try:
            await _upsert_override(
                override_session_maker,
                settings_cls=Settings,
                key="PMM",
                value={"endpoint": PMM_ENDPOINT, "api_key": "new-key"},
            )

            await refresh_all(
                lambda: override_session_maker, proxies, WORKER_OVERRIDE_CALLBACKS
            )

            fresh = await resolve_pmm_api()
            assert fresh is not stale
            assert fresh.api_key == SecretStr("new-key")
        finally:
            await settings.invalidate_client(PMM_ENDPOINT)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_app_owned_classes")
    async def test_without_the_callback_the_stale_client_survives(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Pin the cache gap the ported callback exists to close.

        A refresh cycle carrying no callbacks republishes the ``PMM`` snapshot
        and still hands back the client built with the previous key, because the
        registry key covers endpoint and SSL only.
        """
        proxies = build_extensions_override_proxies()
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="PMM",
            value={"endpoint": PMM_ENDPOINT, "api_key": "old-key"},
        )
        await refresh_all(lambda: override_session_maker, proxies)
        stale = await resolve_pmm_api()
        try:
            await _upsert_override(
                override_session_maker,
                settings_cls=Settings,
                key="PMM",
                value={"endpoint": PMM_ENDPOINT, "api_key": "new-key"},
            )

            await refresh_all(lambda: override_session_maker, proxies)

            assert settings.PMM.api_key == SecretStr("new-key")
            assert await resolve_pmm_api() is stale
        finally:
            await settings.invalidate_client(PMM_ENDPOINT)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_app_owned_classes")
    async def test_a_failing_callback_does_not_break_the_cycle(
        self,
        override_session_maker: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep publishing the snapshot when the rebind callback raises."""

        async def _boom(_: object) -> None:
            raise RuntimeError("boom")

        proxies = build_extensions_override_proxies()
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="PMM",
            value={"endpoint": PMM_ENDPOINT, "api_key": "old-key"},
        )
        await refresh_all(lambda: override_session_maker, proxies)
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="PMM",
            value={"endpoint": PMM_ENDPOINT, "api_key": "new-key"},
        )

        await refresh_all(
            lambda: override_session_maker,
            proxies,
            {(SETTINGS, "PMM"): _boom},
        )

        assert settings.PMM.api_key == SecretStr("new-key")


@pytest.fixture(name="worker_logging_boot")
def worker_logging_boot_fixture() -> Iterator[None]:
    """Install a WARNING-level NullHandler config and restore process logging.

    Mutates process-global logging and the ``settings`` snapshot; teardown
    always runs so a leaked ``NullHandler`` root config cannot silence later
    tests. Snapshot restore is belt-and-suspenders with the autouse
    ``_override_snapshot_cleared`` fixture (that one runs only on setup).
    """
    boot_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"default": {"class": "logging.NullHandler"}},
        "loggers": {
            "": {"handlers": ["default"], "level": "WARNING"},
            "app": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
    }
    try:
        logging.config.dictConfig(boot_config)
        settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"LOGGING": LogLevel.WARNING}
        )
        yield
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]
        logging.config.dictConfig(settings.LOGGING_CONFIG)


class TestWorkerLoggingRebind:
    """Verify a LOGGING override re-applies dictConfig in the worker path."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_app_owned_classes", "worker_logging_boot")
    async def test_logging_override_changes_effective_app_level(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Raise the worker's app logger to the overridden level on refresh.

        Also pin ``disable_existing_loggers: False``: a runtime logger outside
        the configured logger tree (not under a name ``LOGGING_CONFIG``
        declares) must stay enabled after the callback re-enters ``dictConfig``.
        Children of configured names -- e.g. ``celery.app.trace`` under
        ``celery`` -- are never disabled either way, so they cannot discriminate.
        """
        runtime_logger = logging.getLogger("kombu.connection")
        proxies = build_extensions_override_proxies()
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="LOGGING",
            value="DEBUG",
        )

        await refresh_all(
            lambda: override_session_maker, proxies, WORKER_OVERRIDE_CALLBACKS
        )

        assert settings.LOGGING == LogLevel.DEBUG
        assert logging.getLogger("app").isEnabledFor(logging.DEBUG)
        assert not runtime_logger.disabled

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_app_owned_classes", "worker_logging_boot")
    async def test_without_the_callback_boot_level_survives(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Pin the gap: snapshot updates LOGGING but handlers stay at boot level."""
        proxies = build_extensions_override_proxies()
        await _upsert_override(
            override_session_maker,
            settings_cls=Settings,
            key="LOGGING",
            value="DEBUG",
        )

        await refresh_all(lambda: override_session_maker, proxies)

        assert settings.LOGGING == LogLevel.DEBUG
        assert not logging.getLogger("app").isEnabledFor(logging.DEBUG)

    @pytest.mark.usefixtures("worker_logging_boot")
    def test_child_boot_applies_an_existing_logging_override(
        self, worker_loop_env: WorkerLoopEnv
    ) -> None:
        """Raise the app logger to a pre-existing override during the child's seed.

        A prefork child that forks after the override row was written never
        sees a diff, so the boot seed itself must re-enter ``dictConfig``.
        """
        loop, maker = worker_loop_env
        runtime_logger = logging.getLogger("kombu.connection")
        loop.run_until_complete(
            _upsert_override(maker, settings_cls=Settings, key="LOGGING", value="DEBUG")
        )

        start_extensions_settings_override_refresher()

        assert settings.LOGGING == LogLevel.DEBUG
        assert logging.getLogger("app").isEnabledFor(logging.DEBUG)
        assert not runtime_logger.disabled

    @pytest.mark.usefixtures("worker_logging_boot")
    def test_child_boot_leaves_the_pmm_callback_silent(
        self, worker_loop_env: WorkerLoopEnv, mocker: MockerFixture
    ) -> None:
        """Fire only the logging rebind from the child's seed, never the PMM eviction.

        A fresh child starts with an empty client registry, so evicting at boot
        would be busywork; the real worker registry must keep that callback
        unmarked even when a ``PMM`` override row is already stored.
        """
        loop, maker = worker_loop_env
        invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())
        loop.run_until_complete(
            _upsert_override(
                maker,
                settings_cls=Settings,
                key="PMM",
                value={"endpoint": PMM_ENDPOINT, "api_key": "boot-key"},
            )
        )
        loop.run_until_complete(
            _upsert_override(maker, settings_cls=Settings, key="LOGGING", value="DEBUG")
        )

        start_extensions_settings_override_refresher()

        invalidate.assert_not_awaited()
        assert str(settings.PMM.endpoint) == PMM_ENDPOINT
        assert logging.getLogger("app").isEnabledFor(logging.DEBUG)


class TestRepublishExtensionsSettingsSnapshot:
    """Cover the forced republish a task takes before deciding on settings."""

    @pytest.mark.asyncio
    async def test_the_helper_publishes_a_seeded_override(
        self, override_session_maker: async_sessionmaker
    ) -> None:
        """Reflect an override written after the snapshot in hand was built."""
        await _upsert_override(
            override_session_maker,
            settings_cls=ExtensionsSettings,
            key=EXTENSIONS_OVERRIDE_KEY,
            value=EXTENSIONS_OVERRIDE_VALUE,
        )

        async with override_session_maker() as session:
            await republish_extensions_settings_snapshot(session)

        assert extensions_settings.SYNC_REFRESH_TIME == EXTENSIONS_OVERRIDE_VALUE
        assert (
            extensions_settings.get_snapshot()[  # ty: ignore[unresolved-attribute]
                EXTENSIONS_OVERRIDE_KEY
            ]
            == EXTENSIONS_OVERRIDE_VALUE
        )

    @pytest.mark.asyncio
    async def test_the_helper_fires_no_rebind_callback(
        self, override_session_maker: async_sessionmaker, mocker: MockerFixture
    ) -> None:
        """Publish the changed value without notifying any rebind callback.

        ``publish_snapshot`` has no callback channel, so the diff-and-fire step
        ``refresh_all`` performs never runs, which is why the helper's
        docstring scopes it to callers whose registry watches another class.
        """
        fire = mocker.spy(lifecycle, "fire_change_callbacks")
        await _upsert_override(
            override_session_maker,
            settings_cls=ExtensionsSettings,
            key=EXTENSIONS_OVERRIDE_KEY,
            value=EXTENSIONS_OVERRIDE_VALUE,
        )

        async with override_session_maker() as session:
            await republish_extensions_settings_snapshot(session)

        assert extensions_settings.SYNC_REFRESH_TIME == EXTENSIONS_OVERRIDE_VALUE
        fire.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_helper_does_not_compose_the_app_owned_registry(
        self, override_session_maker: async_sessionmaker, mocker: MockerFixture
    ) -> None:
        """Publish the one proxy it needs without importing the app tree."""
        collect = mocker.patch.object(
            extensions_worker, "collect_app_owned_settings_classes", return_value=[]
        )

        async with override_session_maker() as session:
            await republish_extensions_settings_snapshot(session)

        collect.assert_not_called()
