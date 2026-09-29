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

"""Fixtures for ``tests/app/core/settings_override/``."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool

from app.core.alerts.config import AlertSettings
from app.core.config import BaseYamlSettings, Settings, settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.api.routes import ClassEntry
from app.core.settings_override.constants import (
    ALERT_SETTINGS,
    ANONYMIZER_SETTINGS,
    EXTENSIONS_SETTINGS,
    INVENTORY_SETTINGS,
    SETTINGS,
    SNIPPETS_SETTINGS,
    TASKS_SETTINGS,
)
from app.core.settings_override.lifecycle import RefreshCallback, SnapshotChange
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import (
    setting_class_token,
    SettingOverride,
)
from app.core.utils import json_serializer
from app.extensions.config import ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.inventory.config import InventorySettings
from app.tasks.anonymizer.config import AnonymizerSettings
from app.tasks.config import TasksSettings
from tests.app.db_schema import apply_schema

#: Importable path patched when tests replace ``bounded_seed``.
BOUNDED_SEED = "app.core.settings_override.worker.bounded_seed"

#: Importable path patched when tests replace ``refresh_all`` under the worker
#: boundary path (``bounded_refresh`` calls into lifecycle).
WORKER_REFRESH_ALL = "app.core.settings_override.lifecycle.refresh_all"

#: Plaintext secrets the encrypt-at-rest suites seed and assert round trips for.
#: Shared so the settings-override and migration suites cannot drift apart on the
#: value a stored ciphertext is expected to decrypt back to.
PMM_API_KEY = "pmm-api-key-at-rest"
PMM_ENDPOINT = "https://pmm.example.com"
ROUTING_KEY = "pagerduty-routing-key-at-rest"

#: Storage tokens for ``SettingOverride.setting_class`` (SCREAMING_SNAKE).
ALERT_SETTINGS_TOKEN = setting_class_token(AlertSettings)
ANONYMIZER_SETTINGS_TOKEN = setting_class_token(AnonymizerSettings)
INVENTORY_SETTINGS_TOKEN = setting_class_token(InventorySettings)
EXTENSIONS_SETTINGS_TOKEN = setting_class_token(ExtensionsSettings)
SETTINGS_TOKEN = setting_class_token(Settings)
SNIPPETS_SETTINGS_TOKEN = setting_class_token(SnippetsSettings)
TASKS_SETTINGS_TOKEN = setting_class_token(TasksSettings)

#: Every core identifier constant paired with the class it names.
CORE_SETTINGS_CLASSES: tuple[tuple[str, type[BaseYamlSettings]], ...] = (
    (EXTENSIONS_SETTINGS, ExtensionsSettings),
    (TASKS_SETTINGS, TasksSettings),
    (SNIPPETS_SETTINGS, SnippetsSettings),
    (SETTINGS, Settings),
    (ALERT_SETTINGS, AlertSettings),
    (ANONYMIZER_SETTINGS, AnonymizerSettings),
    (INVENTORY_SETTINGS, InventorySettings),
)

#: The token every revision up to the class rename stored the service settings'
#: rows under. The frozen revisions predate that rename, so the rows seeded
#: beneath them carry it rather than the live :data:`EXTENSIONS_SETTINGS_TOKEN`.
LEGACY_SEP_SETTINGS_TOKEN = "SEP_SETTINGS"

#: The callback key :func:`seed_connectivity_override` fires on.
CONNECTIVITY_CALLBACK_KEY = (
    EXTENSIONS_SETTINGS,
    "CONNECTIVITY_CHECK_DEFAULT",
)

#: A username far longer than any bounded column would have allowed. Both the
#: SQLite round-trip and its real-PostgreSQL sibling write one this long to
#: prove ``settingoverride.updated_by`` carries no width.
LONG_USERNAME_LENGTH = 512


def assert_entries_keyed_by_class_name(entries: Sequence[ClassEntry]) -> None:
    """Assert each settings-router entry and its proxy use the class ``__name__``.

    An entry keyed by the storage token instead is no error, just a silent
    lookup miss, so only an explicit check catches it.

    :param entries: The ``ClassEntry`` list a service's settings router serves.
    """
    assert entries, "the settings router serves no classes"
    for identifier, settings_cls, proxy in entries:
        assert identifier == settings_cls.__name__
        assert proxy._setting_class == identifier  # noqa: SLF001


async def insert_override_row(
    session: AsyncSession, **kwargs: object
) -> SettingOverride:
    """Insert one override row through the manager, bypassing the API.

    :param session: The async DB session to write through.
    :param kwargs: Fields forwarded to :class:`SettingOverride`.
    :return: The persisted override row.
    """
    return await SettingsOverrideManager.create(session, SettingOverride(**kwargs))


async def seed_connectivity_override(
    session_maker: async_sessionmaker, *, value: bool
) -> None:
    """Insert an ``ExtensionsSettings.CONNECTIVITY_CHECK_DEFAULT`` override row.

    :param session_maker: Async session maker bound to the override store.
    :param value: The overridden boolean to persist.
    """
    async with session_maker() as session:
        await insert_override_row(
            session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="CONNECTIVITY_CHECK_DEFAULT",
            value=value,
        )


async def clear_connectivity_override(session_maker: async_sessionmaker) -> None:
    """Delete the ``ExtensionsSettings.CONNECTIVITY_CHECK_DEFAULT`` override row.

    :param session_maker: Async session maker bound to the override store.
    """
    async with session_maker() as session:
        await SettingsOverrideManager.delete_where(
            session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="CONNECTIVITY_CHECK_DEFAULT",
        )


def recording_callback(fired: list[SnapshotChange]) -> RefreshCallback:
    """Build a rebind callback that appends every change it receives to ``fired``.

    :param fired: The list each received :class:`SnapshotChange` is appended to.
    :return: An async callback matching :data:`RefreshCallback`.
    """

    async def _callback(change: SnapshotChange) -> None:
        fired.append(change)

    return _callback


class HangingSession:
    """Stand in for an async session whose enter hangs until cancelled."""

    async def __aenter__(self) -> "HangingSession":
        await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def hanging_session_maker_factory() -> type[HangingSession]:
    """Return a session maker whose sessions hang on enter."""
    return HangingSession


def recording_bounded_seed(
    recorded: dict[str, object],
) -> Callable[..., Awaitable[tuple[bool, asyncio.Task | None]]]:
    """Build a stand-in ``bounded_seed`` that records the seed budget.

    :param recorded: Mutable mapping filled with ``seed_timeout`` from each
        invocation.
    :return: An async callable matching ``bounded_seed``'s signature.
    """

    async def _fake_seed(
        session_maker_factory: object,
        proxies: object,
        seed_timeout: float | None,
        *,
        callbacks: object = None,
    ) -> tuple[bool, asyncio.Task | None]:
        recorded["seed_timeout"] = seed_timeout
        return True, None

    return _fake_seed


@pytest.fixture(autouse=True)
def _propagate_cache_logs() -> Iterator[None]:
    """Allow ``caplog`` to see ``app.core.settings_override.cache`` warnings.

    The application's ``LOGGING_CONFIG`` sets ``propagate=False`` on the
    ``app`` logger, which prevents pytest's ``caplog`` (attached to root) from
    seeing records emitted by ``app.*`` loggers. Temporarily re-enable
    propagation on the ``app`` parent for the duration of the test.
    """
    app_logger = logging.getLogger("app")
    previous = app_logger.propagate
    app_logger.propagate = True
    yield
    app_logger.propagate = previous


@pytest.fixture(name="restrict")
def restrict_fixture(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Return a callable pinning ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` to given entries.

    Pins the value on the already-constructed proxy rather than through the
    environment, which the singleton no longer reads.

    :param monkeypatch: The pytest patcher whose undo restores the real value.
    :return: A callable taking the ``"ClassName.KEY"`` entries to allow.
    """

    def _restrict(*entries: str) -> None:
        monkeypatch.setattr(settings.SETTINGS_OVERRIDE, "ALLOWED_KEYS", set(entries))

    return _restrict


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an in-memory SQLite async session for override tests."""
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
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()
