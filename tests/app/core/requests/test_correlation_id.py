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

"""Test correlation ID propagation in BaseRemoteAPI._request."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.log import set_log_context
from app.core.requests.remote_api import BaseRemoteAPI

pytestmark = pytest.mark.usefixtures("reset_log_context")


@pytest.fixture
def api() -> BaseRemoteAPI:
    """Provide a BaseRemoteAPI instance with a mocked session."""
    instance = BaseRemoteAPI(endpoint="https://example.com")
    mock_response = AsyncMock()
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    instance._session = mock_session
    return instance


@pytest.mark.asyncio
async def test_correlation_id_propagated_in_request(api: BaseRemoteAPI) -> None:
    """Assert X-Correlation-ID header is added when ContextVar is set."""
    set_log_context(correlation_id="test-corr-123")

    async with api._request("GET", "/test") as _:
        pass

    call_kwargs = api._session.request.call_args
    assert (
        call_kwargs.kwargs.get("headers", {}).get("X-Correlation-ID") == "test-corr-123"
    )


@pytest.mark.asyncio
async def test_no_correlation_header_when_default(api: BaseRemoteAPI) -> None:
    """Assert no X-Correlation-ID header when ContextVar is default."""
    async with api._request("GET", "/test") as _:
        pass

    call_kwargs = api._session.request.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert "X-Correlation-ID" not in headers


@pytest.mark.asyncio
async def test_correlation_id_merges_with_existing_headers(api: BaseRemoteAPI) -> None:
    """Assert correlation ID header merges with extra_headers."""
    set_log_context(correlation_id="corr-merge")
    api._extra_headers.set({"Authorization": "Bearer token"})

    async with api._request("GET", "/test") as _:
        pass

    call_kwargs = api._session.request.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert headers.get("X-Correlation-ID") == "corr-merge"
    assert headers.get("Authorization") == "Bearer token"

    api._extra_headers.set(None)
