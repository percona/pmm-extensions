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

import pytest

from app.inventory.main import inventory_app, inventory_lifespan
from app.inventory.main import lifespan as inventory_module_lifespan


def test_inventory_app_lifespan_is_always_set():
    """Assert the Inventory lifespan is always assigned at module level.

    The lifespan must not be gated behind a ``__name__`` check, because uvicorn
    re-imports the module with ``__name__ == "app.inventory.main"`` rather than
    ``"__main__"``, which would leave the lifespan as ``None``. The Inventory app
    now wraps ``default_lifespan`` with the settings-override refresher.
    """
    assert inventory_module_lifespan is inventory_lifespan


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
