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

"""Tests for the PBM Configuration child app's routes under /api/apps/backup_mongo/config/.

The child exists so that applying cluster-wide PBM configuration is a deliberate
act rather than a side effect of creating a backup, and its HTTP surface is
*derived* from ``AppCapabilities(execute=True, update=False, delete=False)``
rather than written by hand. These cover both halves of that: the verbs the
capability flags are supposed to withhold, and the single-task create that
distinguishes this app from its cascading parent.
"""

from unittest.mock import AsyncMock

from fastapi import status

from app.extensions.inventory import CreatedService
from tests.app.extensions.apps.backup_mongo.test_api_routes import (
    build_backup_task,
    build_backup_write_body,
    build_execute_response,
    mock_task_api_get_by_path,
)

API_BASE = "/api/apps/backup_mongo/config"
CONFIG_TASK = "mongo-pbm-config"


class TestConfigSchemaEndpoint:
    """Tests for GET /api/apps/backup_mongo/config/schema."""

    def test_schema_returns_200(self, test_client) -> None:
        """Serve a schema payload: this is what the Configuration tab renders from."""
        response = test_client.get(f"{API_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]

    def test_schema_declares_no_derived_cascade(self, test_client) -> None:
        """Fan out to nothing.

        The parent declares four derived legs; a config task applies configuration
        and produces no logical / physical / status / incremental siblings, which is
        the whole reason this app is separate.
        """
        assert not test_client.get(f"{API_BASE}/schema").json().get("derived")

    def test_schema_names_the_record_it_creates(self, test_client) -> None:
        """Name its records rather than serving ``display_name`` under both keys."""
        body = test_client.get(f"{API_BASE}/schema").json()

        assert body["item_display_name"] == "configuration"
        assert body["item_display_name_plural"] == "configurations"

    def test_schema_offers_credentials_path_under_advanced(self, test_client) -> None:
        """Carry the one field the backups form gives up, behind "Show advanced"."""
        sections = test_client.get(f"{API_BASE}/schema").json()["forms"]
        advanced = next(s for s in sections if s["title"] == "Advanced")

        assert [field["name"] for field in advanced["fields"]] == ["credentials_path"]
        assert advanced["advanced"] is True

    def test_backups_schema_does_not_offer_credentials_path(self, test_client) -> None:
        """Keep it off the parent: it describes the host, not one backup."""
        sections = test_client.get("/api/apps/backup_mongo/schema").json()["forms"]
        names = [f["name"] for s in sections for f in s["fields"]]

        assert "credentials_path" not in names


class TestConfigApiCreate:
    """Tests for POST /api/apps/backup_mongo/config/."""

    def test_create_posts_exactly_one_task(
        self,
        test_client,
        mock_task_api_dep,
        mock_inventory_api_dep,
        mongo_service: CreatedService,
    ) -> None:
        """Create the config task alone, where the parent creates five.

        The parent's POST cascades a ``pbm_config`` task plus four backup legs. This
        app shares the parent's payload builder but declares no ``derived`` block, so
        one POST reaches the Tasks API and no sibling is invented.
        """
        mock_inventory_api_dep.get = AsyncMock(return_value=mongo_service.model_dump())
        mock_task_api_dep.post = AsyncMock(return_value=build_backup_task(CONFIG_TASK))
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {f"/{CONFIG_TASK}": build_backup_task(CONFIG_TASK)}
        )

        response = test_client.post(
            f"{API_BASE}/",
            json=build_backup_write_body(
                task_name=CONFIG_TASK, service_id=mongo_service.id
            ),
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert mock_task_api_dep.post.await_count == 1


class TestConfigApiWithheldVerbs:
    """Tests that ``update=False`` / ``delete=False`` really remove the routes.

    A capability flag that only greys out a button in the UI is not a capability
    flag; these assert the framework never mounted the verbs at all, so the
    read-merge-write apply stays the single way config is written.
    """

    def test_put_is_not_routed(self, test_client) -> None:
        """Reject edit: a config task is re-applied, not amended in place."""
        response = test_client.put(
            f"{API_BASE}/{CONFIG_TASK}", json=build_backup_write_body()
        )

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_delete_is_not_routed(self, test_client) -> None:
        """Reject delete for the same reason the parent owns the task's lifecycle."""
        response = test_client.delete(f"{API_BASE}/{CONFIG_TASK}")

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestConfigApiExecute:
    """Tests for POST /api/apps/backup_mongo/config/{task_name}/execute."""

    def test_execute_dispatches_the_config_task(
        self, test_client, mock_task_api_dep
    ) -> None:
        """Run it: execute is the capability this app does declare."""
        mock_task_api_dep.get = mock_task_api_get_by_path(
            {f"/{CONFIG_TASK}": build_backup_task(CONFIG_TASK)}
        )
        mock_task_api_dep.post = AsyncMock(
            return_value=build_execute_response(task_name=CONFIG_TASK)
        )

        # An empty JSON body, as the parent's own execute tests send: the route
        # takes a body even when there is nothing to put in it.
        response = test_client.post(f"{API_BASE}/{CONFIG_TASK}/execute", json={})

        assert response.status_code in (
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
            status.HTTP_202_ACCEPTED,
        )
        assert mock_task_api_dep.post.await_count == 1
