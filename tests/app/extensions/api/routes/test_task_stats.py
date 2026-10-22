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

"""Tests for the PMM Extensions task-stats JSON API route at ``/api/extensions/task-stats/{task_name}``."""

from collections.abc import Iterator
from urllib.parse import quote

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.exceptions import HTTPBadGatewayException
from app.extensions.main import extensions_app
from tests.app.extensions.path_unsafe_task_names import PATH_PARAM_UNSAFE_TASKS


class TestExtensionsTaskStatsEndpoint:
    """Cover ``GET /api/extensions/task-stats/{task_name}`` proxy behavior."""

    def test_returns_upstream_stats_payload(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Forward the upstream stats payload unchanged on success."""
        payload = {
            "engine": "nomad",
            "total": 3,
            "status": {"pass": 2, "fail": 1},
            "duration": {
                "average_seconds": 12.5,
                "last_seconds": 10.0,
                "total_seconds": 37.5,
            },
            "last_finished_at": "2026-05-21T10:00:00+00:00",
        }
        mock_task_api_dep.get.return_value = payload
        response = test_client.get("/api/extensions/task-stats/my-task")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == payload

    def test_proxies_task_name_to_upstream_path(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Call ``tasks_api.get('/stats/{task_name}')`` with the URL path param."""
        mock_task_api_dep.get.return_value = {}
        test_client.get("/api/extensions/task-stats/some-task-name")
        mock_task_api_dep.get.assert_called_once_with("/stats/some-task-name")

    def test_tasks_failure_returns_502(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return ``502`` + ``{"detail": ...}`` on upstream HTTP failure."""
        mock_task_api_dep.get.side_effect = HTTPBadGatewayException("tasks unreachable")
        response = test_client.get("/api/extensions/task-stats/my-task")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "tasks unreachable"}

    def test_tasks_structured_detail_returns_502_with_text(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Carry a structured upstream detail onto the ``502`` as text."""
        detail = {"message": "tasks unreachable"}
        mock_task_api_dep.get.side_effect = HTTPBadGatewayException(detail)
        response = test_client.get("/api/extensions/task-stats/my-task")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": str(detail)}

    def test_tasks_oserror_returns_502(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return ``502`` + ``{"detail": ...}`` when the Tasks API raises an OSError."""
        mock_task_api_dep.get.side_effect = OSError("connection refused")
        response = test_client.get("/api/extensions/task-stats/my-task")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "connection refused"}


class TestExtensionsTaskStatsAuth:
    """Cover ``/api/extensions/task-stats/{task_name}`` authentication enforcement."""

    @pytest.fixture
    def unauthenticated_client(self) -> Iterator[TestClient]:
        """Yield a TestClient with no auth dependency overrides applied."""
        previous = extensions_app.dependency_overrides
        extensions_app.dependency_overrides = {}
        try:
            yield TestClient(extensions_app, raise_server_exceptions=False)
        finally:
            extensions_app.dependency_overrides = previous

    def test_unauthenticated_returns_json_401(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Reject anonymous requests with a JSON 401 response."""
        response = unauthenticated_client.get(
            "/api/extensions/task-stats/foo", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()


class TestExtensionsTaskStatsPathGuard:
    """Test that a task name is refused before it composes the stats path."""

    @pytest.mark.parametrize("task_name", PATH_PARAM_UNSAFE_TASKS)
    def test_refuses_a_name_that_is_not_one_path_segment(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        task_name: str,
    ) -> None:
        """Reject a name that would restructure the upstream stats path."""
        response = test_client.get(
            f"/api/extensions/task-stats/{quote(task_name, safe='')}"
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()
