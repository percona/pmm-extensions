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

"""Tests for the PMM Extensions execution-events proxy route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.status import HTTP_200_OK, HTTP_500_INTERNAL_SERVER_ERROR

from app.core.requests import RemoteAPI
from app.extensions.deps import get_current_user, get_task_history, get_tasks_api
from app.extensions.main import extensions_app


@pytest.fixture
def mock_tasks_api_dep(task_history_response):
    """Override the TaskAPI dependency with an AsyncMock."""
    mock = AsyncMock(spec=RemoteAPI)
    extensions_app.dependency_overrides[get_tasks_api] = lambda: mock
    extensions_app.dependency_overrides[get_task_history] = lambda: (
        task_history_response
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        access_token="test-token"
    )
    yield mock
    extensions_app.dependency_overrides = {}


class TestListTaskExecutionEvents:
    """Test the list_task_execution_events endpoint."""

    def test_proxies_tasks_api(
        self, test_client, mock_tasks_api_dep, task_history_response
    ):
        """Assert GET /execution-events/{id} forwards to the Tasks API."""
        mock_tasks_api_dep.get.return_value = [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "type": "Started",
                "description": "Task received",
                "step": "prepare-env",
            },
        ]

        response = test_client.get(f"/execution-events/{task_history_response.id}")

        assert response.status_code == HTTP_200_OK
        body = response.json()
        assert len(body) == 1
        assert body[0]["type"] == "Started"
        assert body[0]["description"] == "Task received"
        assert body[0]["step"] == "prepare-env"
        assert "timestamp" in body[0]
        mock_tasks_api_dep.get.assert_awaited_once_with(
            f"/history/{task_history_response.id}/events"
        )

    def test_returns_empty_when_tasks_api_returns_none(
        self, test_client, mock_tasks_api_dep, task_history_response
    ):
        """Assert GET /execution-events/{id} returns [] when the Tasks API yields None."""
        mock_tasks_api_dep.get.return_value = None

        response = test_client.get(f"/execution-events/{task_history_response.id}")

        assert response.status_code == HTTP_200_OK
        assert response.json() == []
        mock_tasks_api_dep.get.assert_awaited_once_with(
            f"/history/{task_history_response.id}/events"
        )

    def test_propagates_tasks_api_error(
        self,
        mocker,
        test_client,
        regular_user,
        mock_tasks_api_dep,
        task_history_response,
    ):
        """Assert an upstream Tasks API server error propagates to the response."""
        mock_tasks_api_dep.get.side_effect = HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR
        )

        response = test_client.get(f"/execution-events/{task_history_response.id}")

        assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR
