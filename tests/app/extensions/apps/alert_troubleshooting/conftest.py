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

"""Define shared fixtures for the alert_troubleshooting plugin tests."""

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.deps import get_session
from app.extensions.main import extensions_app
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.models.snippet import Snippet


@pytest.fixture
def snippets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect :attr:`Snippet.BASE_DIR` to a temporary directory for the test."""
    monkeypatch.setattr(Snippet, "BASE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def api_client(test_client: TestClient, session: AsyncSession) -> TestClient:
    """Return a TestClient that uses the in-memory test session."""
    extensions_app.dependency_overrides[get_session] = lambda: session
    # test_client fixture already resets dependency_overrides
    return test_client


@pytest.fixture
def unauthenticated_client(session: AsyncSession) -> Iterator[TestClient]:
    """Return a TestClient with no auth overrides — API calls should 401."""
    extensions_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(extensions_app, raise_server_exceptions=False)
    extensions_app.dependency_overrides = {}


@pytest.fixture
def create_snippet_with_alerts(
    session: AsyncSession, snippets_dir: Path
) -> Callable[..., Awaitable[Snippet]]:
    """Return an async factory that seeds snippets with alert metadata.

    :param session: The in-memory test session.
    :type session: AsyncSession
    :param snippets_dir: The tmp directory aliased as ``Snippet.BASE_DIR``.
    :type snippets_dir: Path
    :return: An async factory function.
    :rtype: Callable[..., Awaitable[Snippet]]
    """

    async def _factory(
        filename: str,
        *,
        alerts: list,
        service_type: str = "mysql",
    ) -> Snippet:
        (snippets_dir / filename).write_text("#!/bin/sh\necho hi\n")
        snippet = Snippet(
            filename=filename,
            size=20,
            md5_digest="a" * 32,
            meta={"alerts": alerts, "service_type": service_type},
        )
        return await SnippetManager.create(session, snippet)

    return _factory
