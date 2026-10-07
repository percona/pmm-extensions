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

"""Shared fixtures for the inventory plugin test suite.

Hoisted from ``test_routes.py`` so the JSON API tests under
``test_api_routes.py`` can reuse the same syncer stubs and the
``run_*_sync`` patches without re-declaring them.
"""

from collections.abc import Generator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture

from app.extensions.apps.inventory.deps import get_syncers
from app.extensions.inventory import CreatedNode, CreatedSchema, CreatedService
from app.extensions.main import extensions_app


class StubPMMSyncer:
    """Stand in for a PMM syncer; capability checks default to ``True``."""

    def can_sync_inventory(self) -> bool:
        """Return ``True``."""
        return True

    def can_sync_node(self, node: CreatedNode) -> bool:
        """Return ``True``."""
        return True

    def can_sync_service(self, service: CreatedService) -> bool:
        """Return ``True``."""
        return True

    def can_sync_schema(self, schema: CreatedSchema) -> bool:
        """Return ``True``."""
        return True


class StubMySQLSyncer:
    """Stand in for a MySQL syncer; capability checks default to ``True``."""

    def can_sync_inventory(self) -> bool:
        """Return ``True``."""
        return True

    def can_sync_node(self, node: CreatedNode) -> bool:
        """Return ``True``."""
        return True

    def can_sync_service(self, service: CreatedService) -> bool:
        """Return ``True``."""
        return True

    def can_sync_schema(self, schema: CreatedSchema) -> bool:
        """Return ``True``."""
        return True


PMM_STUB_NAME = f"{StubPMMSyncer.__module__}.{StubPMMSyncer.__name__}"
MYSQL_STUB_NAME = f"{StubMySQLSyncer.__module__}.{StubMySQLSyncer.__name__}"
EXPECTED_STUB_COUNT = 2


def no_syncers() -> list:
    """Resolve ``SyncersDep`` to an empty list for the no-syncers test path."""
    return []


class _InventorySyncer:
    """Stub syncer that can sync inventory."""

    def can_sync_inventory(self) -> bool:
        return True


class _NonInventorySyncer:
    """Stub syncer that cannot sync inventory."""

    def can_sync_inventory(self) -> bool:
        return False


@pytest.fixture
def mock_syncers() -> Iterator[list]:
    """Override the SyncersDep with two stub syncers."""
    stubs = [StubPMMSyncer(), StubMySQLSyncer()]
    extensions_app.dependency_overrides[get_syncers] = lambda: stubs
    yield stubs
    extensions_app.dependency_overrides = {}


@pytest.fixture
def mock_run_sync_funcs(mocker: MockerFixture) -> dict[str, AsyncMock]:
    """Replace the ``run_*_sync`` symbols on the ``api_routes`` module.

    The real background-task callables open database sessions and invoke
    ``syncer.api_auth(...)``, which the lightweight stub syncers cannot
    satisfy. Patching them at the module level lets the real
    ``BackgroundTasks`` instance schedule and execute the mocks immediately
    after the response, capturing the args originally passed to ``add_task``.
    """
    inventory_mock = AsyncMock()
    node_mock = AsyncMock()
    service_mock = AsyncMock()
    schema_mock = AsyncMock()
    mocker.patch(
        "app.extensions.apps.inventory.api_routes.run_inventory_sync",
        new=inventory_mock,
    )
    return {
        "inventory": inventory_mock,
        "node": node_mock,
        "service": service_mock,
        "schema": schema_mock,
    }


@pytest.fixture
def mock_syncers_dep() -> Generator[list[Any], None, None]:
    """Mock ``SyncersDep`` with one inventory-capable and one non-capable syncer.

    ``build_available_syncers`` reads ``type(syncer).__module__`` and
    ``type(syncer).__name__`` to build the qualified name, so real stub classes
    are used instead of ``MagicMock`` to avoid fragile ``__class__`` patching.
    """
    syncers = [_InventorySyncer(), _NonInventorySyncer()]
    extensions_app.dependency_overrides[get_syncers] = lambda: syncers
    yield syncers
    extensions_app.dependency_overrides.pop(get_syncers, None)
