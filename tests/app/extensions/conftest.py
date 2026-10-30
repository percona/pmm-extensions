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

"""Re-export the shared PMM Extensions test fixtures for the ``tests/app/extensions`` subtree.

These fixtures are defined in the always-loaded ancestor ``tests/app/conftest.py`` so they
resolve regardless of single-process pytest collection order. They are re-exported
here so app-subtree conftests can keep importing them from ``tests.app.extensions.conftest``.

The snippet-seeding fixtures below are defined here rather than in a leaf conftest
because the snippets engine is library-owned (``app/extensions/snippets/``) while its HTTP
surface stays in the app package, so ``tests/app/extensions/snippets/`` and
``tests/app/extensions/apps/snippets/`` both consume them and this is their nearest common
ancestor.

This module also holds shared PMM Extensions subtree test constants. ``REDUCED_ACTIVATION``
mirrors the side-car embedded activation profile
(``sidecar/settings.yaml``), which several modules across the subtree
assert against; defining it once here keeps those copies from drifting away from
the profile as apps are activated or dropped.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from alembic.config import Config
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.requests import RemoteAPI
from app.core.requests.registry import ClientRegistry
from app.extensions.config import App, extensions_settings
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.models import Snippet
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.conftest import (  # noqa: F401
    api_admin_client_no_bearer,
    async_test_client,
    celery_beat_session_fixture,
    dummy_request,
    mock_inventory_api_dep,
    mock_task_api_dep,
    session_fixture,
    test_client,
    unauthenticated_client,
)

REDUCED_ACTIVATION = [
    App(module_name=name)
    for name in ("inventory", "atw", "mysql_backups", "om_inventory", "om_bootstrap")
]
"""The PMM-embedded side-car activation list (``sidecar/settings.yaml``)."""


@pytest.fixture
def extensions_alembic_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, str]:
    """Return an Alembic ``Config`` pointing at a temp SQLite file.

    Patch ``extensions_settings.DATABASE.HOST`` and ``NAME`` so that the
    computed ``DATABASE.URL`` property evaluates to a temp SQLite path
    when ``env.py`` reads it.

    :param tmp_path: Pytest's per-test temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :return: A tuple of (Config, sync sqlite URL) for the test DB.
    """
    db_path = tmp_path / "test_extensions.sqlite"
    sync_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(extensions_settings.DATABASE, "HOST", "")
    monkeypatch.setattr(extensions_settings.DATABASE, "NAME", str(db_path))

    cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    return cfg, sync_url


@pytest.fixture
def snippets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect :attr:`Snippet.BASE_DIR` to a temporary directory for the test."""
    monkeypatch.setattr(Snippet, "BASE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def create_snippet(
    session: AsyncSession, snippets_dir: Path
) -> Callable[..., Awaitable[Snippet]]:
    """Return an async callable that seeds a Snippet row + its file on disk.

    The callable accepts ``filename`` plus keyword arguments ``approved`` and
    ``create_file`` (both default-friendly) and returns the persisted instance.

    :param session: The in-memory test session.
    :param snippets_dir: The tmp directory aliased as ``Snippet.BASE_DIR``.
    :return: An async factory function.
    """

    async def _factory(
        filename: str, *, approved: bool = False, create_file: bool = True
    ) -> Snippet:
        if create_file:
            target = snippets_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\necho hi\n")
        snippet = Snippet(filename=filename, size=20, md5_digest="a" * 32)
        if approved:
            snippet.approve("Seeded as approved", "seed-user")
        return await SnippetManager.create(session, snippet)

    return _factory


@pytest.fixture
def request_less_session(session: AsyncSession, mocker: MockerFixture) -> AsyncSession:
    """Bind the snippets request-less session maker to the test session.

    The derived listing / per-snippet / execute routes open their own
    request-less session via ``get_async_session_maker`` rather than the
    request-scoped ``get_session`` the other fixtures override, so the maker is
    patched to yield the same in-memory ``session`` the rows are seeded through
    (mirroring ``tests/app/extensions/snippets/test_celery.py``). The two snippets
    subtrees opt in autouse; it is not autouse here so unrelated PMM Extensions tests do not
    pay for a session they never touch.
    """
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "app.extensions.snippets.script_source.get_async_session_maker",
        return_value=maker,
    )
    return session


@pytest_asyncio.fixture
async def isolated_client_registry(
    mocker: MockerFixture,
) -> AsyncGenerator[ClientRegistry]:
    """Resolve and retire every settings-built client through a registry of the test's own.

    A test that rebinds a client mid-run needs real clients whose retirement cannot
    reach the process-wide registry other tests share. Only the registry is swapped;
    the consumer under test still resolves its clients through ``settings``.
    """
    registry = ClientRegistry()

    async def get_remote_api(
        _settings: Settings, cls: type[RemoteAPI] = RemoteAPI, **kwargs: Any
    ) -> RemoteAPI:
        return await registry.get(cls, **kwargs)

    async def invalidate_client(_settings: Settings, endpoint: str) -> None:
        await registry.invalidate(endpoint)

    mocker.patch.object(Settings, "get_remote_api", new=get_remote_api)
    mocker.patch.object(Settings, "invalidate_client", new=invalidate_client)
    yield registry
    await registry.close_all()
