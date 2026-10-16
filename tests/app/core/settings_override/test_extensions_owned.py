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

"""Cover the refresher for the PMM Extensions-owned shared settings classes."""

import asyncio
import logging
import logging.config
from collections.abc import AsyncGenerator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine, create_async_engine
from sqlmodel import SQLModel

from app.core.alerts.config import alert_settings
from app.core.config import ExtensionsDatabaseSettings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.constants import ALERT_SETTINGS, SETTINGS
from app.core.settings_override.extensions_owned import (
    apply_logging_dictconfig,
    extensions_override_database,
    EXTENSIONS_OWNED_CALLBACKS,
    extensions_owned_overrides_refresher,
    EXTENSIONS_OWNED_PROXIES,
)
from app.core.settings_override.lifecycle import is_fire_on_boot
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.utils import json_serializer
from app.core.utils.fields import LogLevel
from app.extensions.config import extensions_settings
from tests.app.core.settings_override.conftest import (
    ALERT_SETTINGS_TOKEN,
    assert_registry_keyed_by_class_name,
    HangingSession,
    insert_override_row,
    SETTINGS_TOKEN,
)
from tests.app.db_schema import apply_schema

#: Module whose names the tests below patch.
_MODULE = "app.core.settings_override.extensions_owned"

#: Interval the runtime tests shorten the refresher to, so a change lands fast.
_FAST_INTERVAL = timedelta(milliseconds=50)

#: Seed budget the unreachable-database tests shorten the real one to.
_SHORT_SEED_TIMEOUT = 0.05

#: How long a runtime test waits for a refresh cycle to land a change.
_LANDING_DEADLINE_STEPS = 200


async def _wait_until_level(level: int) -> None:
    """Wait for ``settings.LOGGING`` and both configured loggers to reach ``level``.

    :param level: The logging level the refresher is expected to apply.
    :raises AssertionError: When the level has not landed within the deadline.
    """
    for _ in range(_LANDING_DEADLINE_STEPS):
        if (
            level == settings.LOGGING
            and logging.getLogger().level == level
            and logging.getLogger("app").level == level
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"logging level never reached {level}")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Restore every logger a real ``dictConfig`` of ``LOGGING_CONFIG`` reconfigures."""
    loggers = [logging.getLogger(name) for name in settings.LOGGING_CONFIG["loggers"]]
    saved = [
        (lg, lg.level, list(lg.handlers), lg.propagate, lg.disabled) for lg in loggers
    ]
    yield
    for lg, level, handlers, propagate, disabled in saved:
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.disabled = disabled


@pytest.fixture(name="refresher_enabled")
def refresher_enabled_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the refresher the session-wide test fixture disables.

    :param monkeypatch: The pytest patcher whose undo restores the real value.
    """
    monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESHER_ENABLED", True)
    monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "REFRESH_INTERVAL", _FAST_INTERVAL)


@pytest_asyncio.fixture(name="override_db")
async def override_db_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[async_sessionmaker, None]:
    """Point ``EXTENSIONS__DATABASE__*`` at a fresh SQLite file with the schema.

    :param tmp_path: The per-test directory holding the database file.
    :param monkeypatch: The pytest patcher whose undo restores the environment.
    :yield: A session maker bound to the same database file.
    """
    path = tmp_path / "extensions.db"
    monkeypatch.setenv("EXTENSIONS__DATABASE__NAME", str(path))
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", json_serializer=json_serializer
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


async def _store_logging(session_maker: async_sessionmaker, level: LogLevel) -> None:
    """Replace any stored ``Settings.LOGGING`` override with ``level``.

    :param session_maker: Session maker bound to the PMM Extensions database.
    :param level: The level to store.
    """
    async with session_maker() as session:
        await SettingsOverrideManager.delete_where(
            session, setting_class=SETTINGS_TOKEN, key="LOGGING"
        )
        await insert_override_row(
            session, setting_class=SETTINGS_TOKEN, key="LOGGING", value=level
        )


class _HangOnceSessionMaker:
    """Stand in for a session maker whose first session hangs and later ones are real.

    Models a database that is unreachable at boot and answers afterwards.

    :param real: The real session maker every later session comes from.
    """

    def __init__(self, real: async_sessionmaker) -> None:
        self._real = real
        self._calls = 0

    def __call__(self) -> object:
        self._calls += 1
        return HangingSession() if self._calls == 1 else self._real()


class TestRegistries:
    """Cover the shared-class registry and its callbacks."""

    def test_proxies_are_keyed_by_class_name_and_bound_to_the_shared_proxies(
        self,
    ) -> None:
        """Wire exactly the two PMM Extensions-owned classes to their module proxies."""
        assert_registry_keyed_by_class_name(EXTENSIONS_OWNED_PROXIES)
        assert set(EXTENSIONS_OWNED_PROXIES) == {SETTINGS, ALERT_SETTINGS}
        assert EXTENSIONS_OWNED_PROXIES[SETTINGS].proxy is settings
        assert EXTENSIONS_OWNED_PROXIES[ALERT_SETTINGS].proxy is alert_settings

    def test_every_callback_is_keyed_by_a_wired_class(self) -> None:
        """Register no callback for a class the registry does not refresh."""
        assert {cls for cls, _ in EXTENSIONS_OWNED_CALLBACKS} <= set(
            EXTENSIONS_OWNED_PROXIES
        )
        assert EXTENSIONS_OWNED_CALLBACKS[(SETTINGS, "LOGGING")] is (
            apply_logging_dictconfig
        )
        assert is_fire_on_boot(apply_logging_dictconfig)


class TestExtensionsOverrideDatabase:
    """Cover the database options the refresher connects with."""

    def test_options_are_capped_at_one_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hold one pooled connection whatever the profile sizes the pool to."""
        monkeypatch.setenv("EXTENSIONS__DATABASE__ENGINE", "postgresql+asyncpg")
        monkeypatch.setenv("EXTENSIONS__DATABASE__HOST", "db")
        monkeypatch.setenv("EXTENSIONS__DATABASE__NAME", "extensions")
        monkeypatch.setenv("EXTENSIONS__DATABASE__USER", "pmmx")
        monkeypatch.setenv("EXTENSIONS__DATABASE__POOL_SIZE", "7")
        monkeypatch.setenv("EXTENSIONS__DATABASE__MAX_OVERFLOW", "5")
        monkeypatch.setenv(
            "EXTENSIONS__DATABASE__SCHEMA_TRANSLATE_MAP", '{"extensions": "pmmx"}'
        )

        database = extensions_override_database()

        assert database.POOL_SIZE == 1
        assert database.MAX_OVERFLOW == 0
        assert database.pool_engine_kwargs["pool_size"] == 1
        assert database.pool_engine_kwargs["max_overflow"] == 0
        resolved = ExtensionsDatabaseSettings().DATABASE
        assert database.SCHEMA_TRANSLATE_MAP == resolved.SCHEMA_TRANSLATE_MAP
        assert database.SCHEMA_TRANSLATE_MAP["extensions"] == "pmmx"
        assert database.URL == resolved.URL

    def test_options_resolve_the_same_database_as_the_extensions_service(
        self,
    ) -> None:
        """Read the very database the PMM Extensions service writes overrides to."""
        assert extensions_override_database().URL == extensions_settings.DATABASE.URL


@pytest.mark.usefixtures("refresher_enabled")
class TestExtensionsOwnedOverridesRefresher:
    """Cover the refresher a standalone Tasks or Inventory process enters."""

    @pytest.mark.asyncio
    async def test_boot_applies_a_stored_logging_override(
        self, override_db: async_sessionmaker
    ) -> None:
        """Publish ``LOGGING`` and re-apply ``dictConfig`` before the lifespan yields."""
        await _store_logging(override_db, LogLevel.ERROR)

        async with extensions_owned_overrides_refresher(SETTINGS):
            assert settings.LOGGING == LogLevel.ERROR
            assert logging.getLogger().level == logging.ERROR
            assert logging.getLogger("app").level == logging.ERROR

    @pytest.mark.asyncio
    async def test_a_runtime_change_lands_without_a_restart(
        self, override_db: async_sessionmaker
    ) -> None:
        """Apply a changed ``LOGGING`` row within a refresh interval."""
        await _store_logging(override_db, LogLevel.ERROR)

        async with extensions_owned_overrides_refresher(SETTINGS):
            await _store_logging(override_db, LogLevel.CRITICAL)
            await _wait_until_level(logging.CRITICAL)

    @pytest.mark.asyncio
    async def test_an_unreachable_database_bounds_boot_and_keeps_yaml_env(
        self,
        override_db: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Yield after the seed budget on YAML/env values, logging the expiry."""
        await _store_logging(override_db, LogLevel.ERROR)
        yaml_env_level = settings.LOGGING
        monkeypatch.setattr(
            f"{_MODULE}.EXTENSIONS_OWNED_SEED_TIMEOUT", _SHORT_SEED_TIMEOUT
        )
        monkeypatch.setattr(
            f"{_MODULE}.get_async_session_maker_from_engine",
            lambda _engine: HangingSession,
        )

        async def _enter() -> None:
            async with extensions_owned_overrides_refresher(SETTINGS):
                assert yaml_env_level == settings.LOGGING

        with caplog.at_level("ERROR", logger="app.core.settings_override.lifecycle"):
            await asyncio.wait_for(_enter(), timeout=1.0)

        assert any(
            record.levelname == "ERROR"
            and f"{_SHORT_SEED_TIMEOUT:.2f}s" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_the_first_cycle_after_an_expired_seed_applies_the_override(
        self,
        override_db: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
    ) -> None:
        """Recover once the database answers, firing the ``LOGGING`` rebind once."""
        await _store_logging(override_db, LogLevel.ERROR)
        yaml_env_level = settings.LOGGING
        monkeypatch.setattr(
            f"{_MODULE}.EXTENSIONS_OWNED_SEED_TIMEOUT", _SHORT_SEED_TIMEOUT
        )
        monkeypatch.setattr(
            f"{_MODULE}.get_async_session_maker_from_engine",
            lambda engine: _HangOnceSessionMaker(
                get_async_session_maker_from_engine(engine)
            ),
        )
        dict_config = mocker.spy(logging.config, "dictConfig")

        async with extensions_owned_overrides_refresher(SETTINGS):
            assert yaml_env_level == settings.LOGGING
            await _wait_until_level(logging.ERROR)
            await asyncio.sleep(3 * _FAST_INTERVAL.total_seconds())

        assert dict_config.call_count == 1

    @pytest.mark.asyncio
    async def test_a_missing_override_table_is_logged_not_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Keep running on YAML/env against a database with no ``settingoverride``."""
        monkeypatch.setenv("EXTENSIONS__DATABASE__NAME", str(tmp_path / "empty.db"))
        yaml_env_level = settings.LOGGING

        with caplog.at_level("ERROR", logger="app.core.settings_override.lifecycle"):
            async with extensions_owned_overrides_refresher(SETTINGS):
                assert yaml_env_level == settings.LOGGING

        assert any(
            "Failed to refresh overrides for Settings" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("override_db")
    async def test_the_engine_is_disposed_on_exit(self, mocker: MockerFixture) -> None:
        """Dispose the refresher's own engine when the lifespan ends."""
        dispose = mocker.spy(AsyncEngine, "dispose")

        async with extensions_owned_overrides_refresher(SETTINGS):
            dispose.assert_not_called()

        assert dispose.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("override_db")
    async def test_the_engine_is_disposed_when_the_lifespan_body_raises(
        self, mocker: MockerFixture
    ) -> None:
        """Dispose the refresher's own engine on a failed lifespan too."""
        dispose = mocker.spy(AsyncEngine, "dispose")

        with pytest.raises(RuntimeError, match="lifespan body failed"):
            async with extensions_owned_overrides_refresher(SETTINGS):
                raise RuntimeError("lifespan body failed")

        assert dispose.call_count == 1

    @pytest.mark.asyncio
    async def test_a_stored_alert_settings_override_is_published(
        self, override_db: async_sessionmaker
    ) -> None:
        """Publish ``AlertSettings`` for a process that wires it."""
        async with override_db() as session:
            await insert_override_row(
                session,
                setting_class=ALERT_SETTINGS_TOKEN,
                key="SOURCE_PREFIX",
                value="pmm-sidecar-",
            )

        async with extensions_owned_overrides_refresher(SETTINGS, ALERT_SETTINGS):
            assert alert_settings.SOURCE_PREFIX == "pmm-sidecar-"
