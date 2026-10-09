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

"""Define test fixtures and shared helpers for mysql_backups plugin tests."""

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role_for_unsafe_methods
from app.core.requests import RemoteAPI
from app.extensions.deps import (
    get_current_user,
    get_inventory_api,
    get_session,
    get_tasks_api,
    require_bearer_for_unsafe_methods,
)
from app.extensions.main import extensions_app
from app.inventory.models import ServiceTypeEnum
from tests.app.extensions.conftest import (  # noqa: F401
    mock_inventory_api_dep,
    mock_task_api_dep,
    unauthenticated_client,
)

_APPS_DIR = pathlib.Path(__file__).parents[5] / "app/extensions/apps/mysql_backups"
XTRABACKUP_PAYLOAD_PATH = _APPS_DIR / "xtrabackup_payload"
MYDUMPER_PAYLOAD_PATH = _APPS_DIR / "mydumper_payload"
BINLOG_PAYLOAD_PATH = _APPS_DIR / "binlog_payload"

# Spelled out on purpose: this is the cadence vocabulary the product promises, so a
# test that read it back off a model or the payload would assert a surface against
# itself. ``literal_members`` answers the separate question of whether two surfaces
# agree with each other.
XTRABACKUP_INCREMENTAL_CYCLES = (
    "daily",
    "weekly",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
)


@pytest.fixture
def readable_cnf(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a readable MySQL option file, the state the payload's guard accepts.

    Shared rather than written per test: several modules need a usable option file
    only so that some other failure is the one raised, and a copy per module is a
    copy of the guard's precondition.

    :param tmp_path: The test's temporary directory, holding the option file.
    :return: The path to the written option file.
    """
    cnf = tmp_path / "my.cnf"
    cnf.write_text("[client]\n")
    return cnf


def payload_tree(path: pathlib.Path) -> ast.Module:
    """Parse and return a payload script's AST, fresh on every call.

    Centralizes the parse so the per-file AST-extraction helpers in this
    directory's test modules do not each re-derive it independently.

    :param path: Path to the payload script.
    :return: The parsed module.
    """
    return ast.parse(path.read_text(encoding="utf-8"))


def xtrabackup_payload_tree() -> ast.Module:
    """Parse and return the xtrabackup payload's AST, fresh on every call."""
    return payload_tree(XTRABACKUP_PAYLOAD_PATH)


def mydumper_payload_tree() -> ast.Module:
    """Parse and return the mydumper payload's AST, fresh on every call."""
    return payload_tree(MYDUMPER_PAYLOAD_PATH)


def binlog_payload_tree() -> ast.Module:
    """Parse and return the binlog payload's AST, fresh on every call."""
    return payload_tree(BINLOG_PAYLOAD_PATH)


def xtrabackup_binary_default(tree: ast.Module) -> str:
    """Return the binary a payload falls back to when its config omits one.

    Shared rather than extracted per module: two surfaces are pinned to this one
    value — the backup and restore payloads to each other, and the form's
    blank-field resolution to the payload's — so a second reader of the same call
    could match differently and let one of those pins pass while the other drifted.

    :param tree: The parsed payload whose ``XTRABACKUP_BIN_CMD`` fallback to read.
    :return: The fallback binary spelling.
    :raises AssertionError: If the payload no longer defaults the key.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        match node.func.attr, node.args:
            case "get", [
                ast.Constant(value="XTRABACKUP_BIN_CMD"),
                ast.Constant(value=str() as default),
            ]:
                return default
    raise AssertionError("the payload no longer defaults XTRABACKUP_BIN_CMD")


def service_payload(
    name: str,
    service_id: int = 1,
    service_type: ServiceTypeEnum = ServiceTypeEnum.MYSQL,
) -> dict:
    """Build a minimal inventory service payload a service-resolving route accepts."""
    return {
        "id": service_id,
        "service_id": f"/service_id/{service_id}",
        "name": name,
        "type": service_type.value,
        "node_id": 1,
    }


def inventory_mock(
    returns: dict | None = None, *, raises: Exception | None = None
) -> AsyncMock:
    """Build a mock InventoryAPI whose ``get`` returns or raises."""
    mock = AsyncMock(spec=RemoteAPI)
    if raises is not None:
        mock.get.side_effect = raises
    else:
        mock.get.return_value = returns
    return mock


async def authenticated_get(
    url: str,
    *,
    session: AsyncSession,
    inventory: AsyncMock,
    user: object,
    params: dict[str, Any] | None = None,
    tasks: AsyncMock | None = None,
) -> Response:
    """GET ``url`` against the extensions app with the given session + inventory mock.

    Installs the authentication overrides an ``/api/apps/*`` route needs, so a
    route test asserts on the route's own behavior rather than on the auth gate,
    and restores any previously installed overrides afterwards. Only a route that
    reaches the Tasks API needs ``tasks``; the dependency is left un-overridden
    otherwise.
    """
    previous_overrides = extensions_app.dependency_overrides.copy()
    extensions_app.dependency_overrides[get_session] = lambda: session
    extensions_app.dependency_overrides[get_current_user] = lambda: user
    extensions_app.dependency_overrides[require_bearer_for_unsafe_methods] = lambda: (
        None
    )
    extensions_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    extensions_app.dependency_overrides[get_inventory_api] = lambda: inventory
    if tasks is not None:
        extensions_app.dependency_overrides[get_tasks_api] = lambda: tasks
    try:
        transport = ASGITransport(app=extensions_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(url, params=params)
    finally:
        extensions_app.dependency_overrides.clear()
        extensions_app.dependency_overrides.update(previous_overrides)
