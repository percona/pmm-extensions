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

"""Define tests for the app.inventory.main module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.core.settings_override.constants import ALERT_SETTINGS, SETTINGS
from app.inventory.main import (
    inventory_app,
    inventory_lifespan,
    inventory_overrides_lifespan,
)
from app.inventory.main import lifespan as inventory_module_lifespan
from tests.app.conftest import null_async_cm
from tests.app.core.settings_override.conftest import (
    assert_registry_keyed_by_class_name,
)


def test_inventory_app_lifespan_is_always_set():
    """Assert the Inventory lifespan is always assigned at module level.

    The lifespan must not be gated behind a ``__name__`` check, because uvicorn
    re-imports the module with ``__name__ == "app.inventory.main"`` rather than
    ``"__main__"``, which would leave the lifespan as ``None``. The Inventory app
    now wraps ``default_lifespan`` with the settings-override refresher.
    """
    assert inventory_module_lifespan is inventory_lifespan


class TestInventoryStandaloneLifespan:
    """Pin which refreshers each Inventory lifespan enters."""

    @pytest.mark.asyncio
    async def test_standalone_refreshes_settings_only(self) -> None:
        """Refresh ``Settings`` from the PMM Extensions database when served alone."""
        shared = MagicMock(return_value=null_async_cm())
        with (
            patch("app.inventory.main.extensions_owned_overrides_refresher", shared),
            patch(
                "app.inventory.main.settings_override_refresher",
                MagicMock(return_value=null_async_cm()),
            ),
            patch("app.inventory.main.default_lifespan", return_value=null_async_cm()),
        ):
            async with inventory_lifespan(FastAPI()):
                pass

        shared.assert_called_once_with(SETTINGS)

    @pytest.mark.asyncio
    async def test_the_combined_part_adds_no_shared_class_publisher(self) -> None:
        """Leave the shared classes to the PMM Extensions refresher under ``app.main:app``."""
        shared = MagicMock(return_value=null_async_cm())
        refresher = MagicMock(return_value=null_async_cm())
        with (
            patch("app.inventory.main.extensions_owned_overrides_refresher", shared),
            patch("app.inventory.main.settings_override_refresher", refresher),
        ):
            async with inventory_overrides_lifespan(FastAPI()):
                pass

        shared.assert_not_called()
        registry = refresher.call_args.args[1]
        assert SETTINGS not in registry
        assert ALERT_SETTINGS not in registry


class TestInventoryOverridesLifespanRegistry:
    """Pin the registry the Inventory override refresher is started with."""

    @pytest.mark.asyncio
    async def test_registry_is_keyed_by_class_name(self) -> None:
        """Key each entry by the class ``__name__``, never the storage token."""
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        refresher = MagicMock(return_value=cm)
        with patch("app.inventory.main.settings_override_refresher", refresher):
            async with inventory_overrides_lifespan(FastAPI()):
                pass

        assert_registry_keyed_by_class_name(refresher.call_args.args[1])


class TestNestedListOpenAPI:
    """Test the OpenAPI contract of the routes listing a parent's children."""

    @pytest.mark.parametrize(
        "path",
        [
            "/nodes/{node_id}/services/",
            "/services/{service_id}/schemas/",
            "/schemas/{schema_id}/tables/",
        ],
    )
    def test_include_retired_is_declared_once(self, path: str) -> None:
        """Declare ``include_retired`` once though parent and children both read it.

        The parent lookup and the children manager each depend on the query
        parameter, so a duplicate entry would break the generated API client.
        """
        parameters = inventory_app.openapi()["paths"][path]["get"]["parameters"]
        names = [parameter["name"] for parameter in parameters]
        assert names.count("include_retired") == 1
