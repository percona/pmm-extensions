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

"""End-to-end-ish integration tests for the Tasks-side override layer."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.pool import StaticPool

from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.constants import TASKS_SETTINGS
from app.core.settings_override.lifecycle import ProxyEntry, refresh_all
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingOverride
from app.core.utils import json_serializer
from app.tasks.config import (
    PreExecutionCheckMode,
    tasks_settings,
    TasksSettings,
)
from tests.app.core.settings_override.conftest import TASKS_SETTINGS_TOKEN
from tests.app.db_schema import apply_schema


@pytest_asyncio.fixture(name="override_session_maker")
async def _override_session_maker() -> AsyncGenerator[async_sessionmaker, None]:
    """Provide an in-memory SQLite session maker isolated from the main test DB."""
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


def _tasks_proxies() -> dict:
    """Return the Tasks-side proxy registry mirroring the lifespan wiring."""
    return {
        TASKS_SETTINGS: ProxyEntry(tasks_settings, TasksSettings),
    }


@pytest.mark.asyncio
async def test_pre_execution_connectivity_check_override(
    override_session_maker: async_sessionmaker,
) -> None:
    """An override for ``PRE_EXECUTION_CONNECTIVITY_CHECK`` is reflected on the proxy."""
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="PRE_EXECUTION_CONNECTIVITY_CHECK",
                value="block",
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert (
        tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK is PreExecutionCheckMode.BLOCK
    )


_OVERRIDE_STALENESS_SECONDS = 7200
_OVERRIDE_LOG_RETENTION_DAYS = 30


@pytest.mark.asyncio
async def test_staleness_threshold_override(
    override_session_maker: async_sessionmaker,
) -> None:
    """An override for ``STALENESS_THRESHOLD_SECONDS`` is reflected on the proxy."""
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="STALENESS_THRESHOLD_SECONDS",
                value=_OVERRIDE_STALENESS_SECONDS,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert tasks_settings.STALENESS_THRESHOLD_SECONDS == _OVERRIDE_STALENESS_SECONDS


@pytest.mark.asyncio
async def test_tasks_proxy_visible_after_refresh(
    override_session_maker: async_sessionmaker,
) -> None:
    """``tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK`` swaps after refresh.

    The Tasks dispatch route reads this field per call. The proxy contract
    is unit-tested elsewhere; this test fills the Tasks-side proxy registry
    coverage gap end-to-end through ``refresh_all`` against the production
    ``tasks_settings`` proxy.
    """
    yaml_default = tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK
    target = (
        PreExecutionCheckMode.BLOCK
        if yaml_default is not PreExecutionCheckMode.BLOCK
        else PreExecutionCheckMode.WARN
    )
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="PRE_EXECUTION_CONNECTIVITY_CHECK",
                value=target,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK is target


@pytest.mark.asyncio
async def test_log_retention_days_override_applies_at_runtime(
    override_session_maker: async_sessionmaker,
) -> None:
    """A valid ``LOG_RETENTION_DAYS`` override is reflected on the proxy."""
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="LOG_RETENTION_DAYS",
                value=_OVERRIDE_LOG_RETENTION_DAYS,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert tasks_settings.LOG_RETENTION_DAYS == _OVERRIDE_LOG_RETENTION_DAYS


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [0, -5, 366, "x", 1.5])
async def test_log_retention_days_invalid_override_falls_back(
    override_session_maker: async_sessionmaker,
    invalid_value: object,
) -> None:
    """An out-of-range/non-integer override is skipped; the default is kept."""
    baseline = tasks_settings.LOG_RETENTION_DAYS
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="LOG_RETENTION_DAYS",
                value=invalid_value,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert baseline == tasks_settings.LOG_RETENTION_DAYS


@pytest.mark.asyncio
async def test_restricted_deployment_filters_withheld_rows(
    override_session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert a withheld key's row is filtered while an allowed one still lands."""
    baseline = tasks_settings.STALENESS_THRESHOLD_SECONDS
    monkeypatch.setattr(
        settings.SETTINGS_OVERRIDE,
        "ALLOWED_KEYS",
        {"TasksSettings.LOG_RETENTION_DAYS"},
    )
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="STALENESS_THRESHOLD_SECONDS",
                value=_OVERRIDE_STALENESS_SECONDS,
            ),
        )
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="LOG_RETENTION_DAYS",
                value=_OVERRIDE_LOG_RETENTION_DAYS,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert baseline == tasks_settings.STALENESS_THRESHOLD_SECONDS
    assert tasks_settings.LOG_RETENTION_DAYS == _OVERRIDE_LOG_RETENTION_DAYS


@pytest.mark.asyncio
async def test_inactive_override_falls_back_to_yaml_default(
    override_session_maker: async_sessionmaker,
) -> None:
    """Deactivating the override row returns the proxy to the resolved default."""
    baseline = tasks_settings.STALENESS_THRESHOLD_SECONDS
    async with override_session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=TASKS_SETTINGS_TOKEN,
                key="STALENESS_THRESHOLD_SECONDS",
                value=_OVERRIDE_STALENESS_SECONDS,
            ),
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert tasks_settings.STALENESS_THRESHOLD_SECONDS == _OVERRIDE_STALENESS_SECONDS

    async with override_session_maker() as session:
        await SettingsOverrideManager.update_where(
            session,
            {"is_active": False},
            setting_class=TASKS_SETTINGS_TOKEN,
            key="STALENESS_THRESHOLD_SECONDS",
        )
    await refresh_all(lambda: override_session_maker, _tasks_proxies())
    assert baseline == tasks_settings.STALENESS_THRESHOLD_SECONDS
