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

"""Define tests for the app.extensions.apps.framework.connectivity module."""

from unittest.mock import AsyncMock

import pytest

from app.core.requests import RemoteAPI
from app.extensions.apps.framework.connectivity import (
    ConnectivityWarning,
    maybe_record_connectivity_warning,
    record_connectivity_warning,
)
from app.extensions.connectivity import (
    clear_connectivity_caches,
)
from app.inventory.models import ServiceTypeEnum


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear the alru_cache between tests."""
    clear_connectivity_caches()
    yield
    clear_connectivity_caches()


@pytest.fixture
def mock_tasks_api():
    """Return a mock Tasks API client."""
    return AsyncMock(spec=RemoteAPI)


class TestRecordConnectivityWarning:
    """Test the record_connectivity_warning helper."""

    @pytest.mark.asyncio
    async def test_returns_none_on_success(self, mock_tasks_api):
        """Return ``None`` when the check passes."""
        mock_tasks_api.post.return_value = {"success": True, "error": None}

        result = await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        assert result is None
        mock_tasks_api.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_warning_on_failure(self, mock_tasks_api):
        """Return a populated warning when the check fails."""
        mock_tasks_api.post.return_value = {
            "success": False,
            "error": "connection refused",
        }

        result = await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        assert result == ConnectivityWarning(
            target="node1",
            service_type="mysql",
            message="connection refused",
        )

    @pytest.mark.asyncio
    async def test_threads_task_history_id_into_warning(self, mock_tasks_api):
        """Carry ``task_history_id`` from the API response onto the warning.

        Regression: the warning must expose the run-script
        ``task_history_id`` so the detail page can link the log.
        """
        expected_id = 321
        mock_tasks_api.post.return_value = {
            "success": False,
            "error": "Connectivity check timed out after 30s",
            "task_history_id": expected_id,
        }

        result = await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        assert result is not None
        assert result.task_history_id == expected_id

    @pytest.mark.asyncio
    async def test_task_history_id_defaults_to_none(self, mock_tasks_api):
        """Default ``task_history_id`` to ``None`` when the response omits it.

        Back-compat: existing plugin consumers and responses that predate the
        field must keep working.
        """
        mock_tasks_api.post.return_value = {
            "success": False,
            "error": "connection refused",
        }

        result = await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        assert result is not None
        assert result.task_history_id is None

    @pytest.mark.asyncio
    async def test_uses_fallback_message_when_error_is_none(self, mock_tasks_api):
        """Fall back to a generic message when the API reports failure without an error string."""
        mock_tasks_api.post.return_value = {"success": False, "error": None}

        result = await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        assert result is not None
        assert result.message == "Connectivity check failed"

    @pytest.mark.asyncio
    async def test_uses_alru_cache_on_repeated_call(self, mock_tasks_api):
        """Skip the Tasks API call when the alru_cache already has a hit."""
        mock_tasks_api.post.return_value = {"success": True, "error": None}

        await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )
        await record_connectivity_warning(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

        mock_tasks_api.post.assert_awaited_once()


class TestMaybeRecordConnectivityWarning:
    """Test the maybe_record_connectivity_warning guard helper."""

    @pytest.mark.asyncio
    async def test_opt_out_returns_none_no_call(self, mock_tasks_api, mocker):
        """Skip the inner helper when opted out."""
        mock_record = mocker.patch(
            "app.extensions.apps.framework.connectivity.record_connectivity_warning",
            new_callable=AsyncMock,
        )
        meta = {
            "target": "node1",
            "_connectivity_host": "10.0.0.1",
            "_connectivity_port": 3306,
            "_connectivity_service_type": ServiceTypeEnum.MYSQL.value,
        }

        result = await maybe_record_connectivity_warning(
            mock_tasks_api, meta, check_connectivity=False
        )

        assert result is None
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_meta_returns_none(self, mock_tasks_api, mocker):
        """Return ``None`` and skip the inner helper when ``meta`` is empty."""
        mock_record = mocker.patch(
            "app.extensions.apps.framework.connectivity.record_connectivity_warning",
            new_callable=AsyncMock,
        )

        result = await maybe_record_connectivity_warning(mock_tasks_api, {})

        assert result is None
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "meta",
        [
            {"target": "node1"},
            {"target": "node1", "_connectivity_host": "10.0.0.1"},
            {
                "target": "node1",
                "_connectivity_host": "10.0.0.1",
                "_connectivity_port": 3306,
            },
            {
                "_connectivity_host": "10.0.0.1",
                "_connectivity_port": 3306,
                "_connectivity_service_type": ServiceTypeEnum.MYSQL.value,
            },
        ],
    )
    async def test_partial_meta_returns_none(self, mock_tasks_api, mocker, meta):
        """Skip the inner helper when any required meta key is missing."""
        mock_record = mocker.patch(
            "app.extensions.apps.framework.connectivity.record_connectivity_warning",
            new_callable=AsyncMock,
        )

        result = await maybe_record_connectivity_warning(mock_tasks_api, meta)

        assert result is None
        mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_meta_delegates_with_correct_kwargs(
        self, mock_tasks_api, mocker
    ):
        """Delegate to ``record_connectivity_warning`` with parsed meta values."""
        mock_record = mocker.patch(
            "app.extensions.apps.framework.connectivity.record_connectivity_warning",
            new_callable=AsyncMock,
            return_value=None,
        )
        meta = {
            "target": "node1",
            "_connectivity_host": "10.0.0.1",
            "_connectivity_port": 3306,
            "_connectivity_service_type": ServiceTypeEnum.MYSQL.value,
        }

        result = await maybe_record_connectivity_warning(mock_tasks_api, meta)

        assert result is None
        mock_record.assert_awaited_once_with(
            mock_tasks_api,
            target="node1",
            host="10.0.0.1",
            port=3306,
            service_type="mysql",
        )

    @pytest.mark.asyncio
    async def test_opt_in_explicit_true_runs_check(self, mock_tasks_api):
        """Run the check when ``check_connectivity`` is explicitly ``True``."""
        mock_tasks_api.post.return_value = {"success": True, "error": None}
        meta = {
            "target": "node1",
            "_connectivity_host": "10.0.0.1",
            "_connectivity_port": 3306,
            "_connectivity_service_type": ServiceTypeEnum.MYSQL.value,
        }

        result = await maybe_record_connectivity_warning(
            mock_tasks_api, meta, check_connectivity=True
        )

        assert result is None
        mock_tasks_api.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_meta_returns_warning_on_failure(self, mock_tasks_api):
        """Return a populated warning when the inner check fails."""
        mock_tasks_api.post.return_value = {
            "success": False,
            "error": "connection refused",
        }
        meta = {
            "target": "node1",
            "_connectivity_host": "10.0.0.1",
            "_connectivity_port": 3306,
            "_connectivity_service_type": ServiceTypeEnum.MYSQL.value,
        }

        result = await maybe_record_connectivity_warning(mock_tasks_api, meta)

        assert result == ConnectivityWarning(
            target="node1",
            service_type="mysql",
            message="connection refused",
        )
