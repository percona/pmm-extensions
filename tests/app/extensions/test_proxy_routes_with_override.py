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

"""PMM Extensions side proxy coverage for HOT overrides at the production call sites.

Each case inserts an override row, runs ``refresh_all`` (the same call path
the PMM Extensions lifespan refresher uses), and then asserts the override is visible
at the consumer's call shape -- which varies by consumer:

* ``test_snippets_refresh_route_observes_enable_manual_sync_override`` issues
  a real ``TestClient`` request and spies on the sync helper.
* ``test_extensions_proxy_visible_after_refresh`` reads the proxy attribute
  directly -- the exact shape ``app/extensions/deps.py`` uses per request.

One representative consumer per PMM Extensions side wrapped settings class is
exercised. The Tasks-side equivalent lives next to the Tasks app at
``tests/app/tasks/test_settings_override_integration.py``.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.pool import StaticPool

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.config import BaseYamlSettings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.constants import EXTENSIONS_SETTINGS, SNIPPETS_SETTINGS
from app.core.settings_override.lifecycle import ProxyEntry, refresh_all
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import (
    setting_class_token,
    SettingOverride,
)
from app.core.utils import json_serializer
from app.extensions.config import extensions_settings, ExtensionsSettings
from app.extensions.deps import (
    get_current_user,
    get_session,
)
from app.extensions.main import extensions_app
from app.extensions.snippets.config import snippets_settings, SnippetsSettings
from tests.app.db_schema import apply_schema


@pytest_asyncio.fixture
async def override_session_maker() -> AsyncGenerator[async_sessionmaker, None]:
    """Provide an in-memory SQLite session maker for the override store."""
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


def _extensions_proxies() -> dict:
    """Return the PMM Extensions side proxy registry mirroring the PMM Extensions lifespan wiring."""
    return {
        EXTENSIONS_SETTINGS: ProxyEntry(extensions_settings, ExtensionsSettings),
        SNIPPETS_SETTINGS: ProxyEntry(snippets_settings, SnippetsSettings),
    }


async def _insert_override(
    session_maker: async_sessionmaker,
    settings_cls: type[BaseYamlSettings],
    key: str,
    value: object,
) -> None:
    """Insert one active override row into the override store.

    :param session_maker: Async session maker bound to the override store.
    :param settings_cls: Settings class whose :func:`~app.core.settings_override.models.setting_class_token`
        is persisted as ``setting_class`` on the row.
    :param key: Canonical override key (``SCREAMING_SNAKE`` or nested path).
    :param value: JSON-serializable override payload.
    """
    token = setting_class_token(settings_cls)
    async with session_maker() as session:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(setting_class=token, key=key, value=value),
        )


@pytest.mark.asyncio
async def test_snippets_refresh_route_observes_enable_manual_sync_override(
    override_session_maker: async_sessionmaker,
    admin_user: CasdoorUser,
    mocker: MockerFixture,
) -> None:
    """``POST /api/apps/snippets/refresh`` reads ``ENABLE_MANUAL_SYNC`` via the proxy.

    The repository's ``settings.yaml`` sets ``ENABLE_MANUAL_SYNC: true``, so
    the baseline request invokes the actual sync. Inserting an override row
    that flips the flag to ``false`` and running ``refresh_all`` must cause
    the route's ``IsManualSyncEnabled`` guard to reject the request before the
    sync helper runs, leaving the spy's await count unchanged.
    """
    update_snippets_spy = mocker.patch(
        "app.extensions.apps.snippets.extra_routes.update_snippets",
        new=AsyncMock(return_value=None),
    )

    async def _guard_session() -> AsyncIterator[AsyncSession]:
        # The ``require_app_enabled("snippets")`` route guard reads ``appstate``
        # via ``get_session``; point it at the in-memory store (all tables, no
        # rows -> snippets enabled) so the gate is deterministic and never
        # blocks on a shared, order-dependent DB.
        async with override_session_maker() as guard_session:
            yield guard_session

    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: admin_user
    extensions_app.dependency_overrides[get_session] = _guard_session
    try:
        # Per tests/CLAUDE.md: never wrap TestClient(extensions_app) in `with` --
        # that triggers ``extensions_lifespan`` and queries the celery beat
        # ``schedule.db`` which is absent in fresh CI. Instantiate
        # directly; the snapshot is wired manually via ``refresh_all``
        # below, so the route observes the override without needing the
        # lifespan to start the refresher.
        client = TestClient(extensions_app, raise_server_exceptions=False)
        client.post("/api/apps/snippets/refresh", headers={"Authorization": "Bearer t"})
        assert update_snippets_spy.await_count == 1

        await _insert_override(
            override_session_maker,
            SnippetsSettings,
            "ENABLE_MANUAL_SYNC",
            value=False,
        )
        await refresh_all(lambda: override_session_maker, _extensions_proxies())

        client.post("/api/apps/snippets/refresh", headers={"Authorization": "Bearer t"})
        # The HOT override must short-circuit the route before update_snippets.
        assert update_snippets_spy.await_count == 1
    finally:
        extensions_app.dependency_overrides = {}


@pytest.mark.asyncio
async def test_extensions_proxy_visible_after_refresh(
    override_session_maker: async_sessionmaker,
) -> None:
    """``extensions_settings.CONNECTIVITY_CHECK_DEFAULT`` swaps after refresh.

    Asserting via the proxy (rather than a full route round-trip) is
    sufficient end-to-end coverage because the consumer ``app/extensions/deps.py``
    reads the field per-request via attribute access -- exactly the call
    shape the proxy intercepts. The proxy contract itself is tested in
    ``test_proxy.py``; this test fills the PMM Extensions side proxy registry coverage
    gap by exercising the full ``refresh_all`` path against the production
    ``extensions_settings`` proxy instance.
    """
    yaml_default = extensions_settings.CONNECTIVITY_CHECK_DEFAULT
    override_value = not yaml_default
    await _insert_override(
        override_session_maker,
        ExtensionsSettings,
        "CONNECTIVITY_CHECK_DEFAULT",
        value=override_value,
    )
    await refresh_all(lambda: override_session_maker, _extensions_proxies())
    assert extensions_settings.CONNECTIVITY_CHECK_DEFAULT is override_value
