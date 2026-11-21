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

"""Test the LogContextMiddleware."""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse

from app.core.log import (
    _CONTEXT_VARS,
    correlation_id_var,
    endpoint_var,
    request_id_var,
)
from app.core.middleware.log_context import CORRELATION_ID_HEADER, LogContextMiddleware

UUID4_HEX_LENGTH = 32


def _create_app() -> FastAPI:
    """Create a minimal FastAPI app with the LogContextMiddleware."""
    test_app = FastAPI()
    test_app.add_middleware(LogContextMiddleware)

    @test_app.get("/test")
    async def test_route() -> JSONResponse:
        return JSONResponse(
            {
                "request_id": request_id_var.get(),
                "correlation_id": correlation_id_var.get(),
                "endpoint": endpoint_var.get(),
            }
        )

    @test_app.get("/error")
    async def error_route() -> None:
        raise ValueError("boom")

    return test_app


@pytest.fixture
def app() -> FastAPI:
    """Provide a test app with LogContextMiddleware."""
    return _create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Provide an async HTTP client for the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_generates_request_id(client: AsyncClient) -> None:
    """Assert a request_id is generated and set in the log context."""
    response = await client.get("/test")
    data = response.json()

    assert data["request_id"] != "-"
    assert len(data["request_id"]) == UUID4_HEX_LENGTH


@pytest.mark.asyncio
async def test_generates_correlation_id_when_missing(client: AsyncClient) -> None:
    """Assert a new correlation_id is generated when no header is present."""
    response = await client.get("/test")
    data = response.json()

    assert data["correlation_id"] != "-"
    assert len(data["correlation_id"]) == UUID4_HEX_LENGTH


@pytest.mark.asyncio
async def test_propagates_incoming_correlation_id(client: AsyncClient) -> None:
    """Assert an existing X-Correlation-ID header is reused."""
    response = await client.get("/test", headers={CORRELATION_ID_HEADER: "my-corr-id"})
    data = response.json()

    assert data["correlation_id"] == "my-corr-id"


@pytest.mark.asyncio
async def test_rejects_invalid_correlation_id(client: AsyncClient) -> None:
    """Assert an invalid X-Correlation-ID header is replaced with a generated one."""
    response = await client.get(
        "/test", headers={CORRELATION_ID_HEADER: "<script>alert(1)</script>"}
    )
    data = response.json()

    assert data["correlation_id"] != "<script>alert(1)</script>"
    assert len(data["correlation_id"]) == UUID4_HEX_LENGTH


@pytest.mark.asyncio
async def test_rejects_oversized_correlation_id(client: AsyncClient) -> None:
    """Assert an oversized X-Correlation-ID header is replaced with a generated one."""
    oversized = "a" * 100
    response = await client.get("/test", headers={CORRELATION_ID_HEADER: oversized})
    data = response.json()

    assert data["correlation_id"] != oversized
    assert len(data["correlation_id"]) == UUID4_HEX_LENGTH


@pytest.mark.asyncio
async def test_response_includes_correlation_id(client: AsyncClient) -> None:
    """Assert the response contains the X-Correlation-ID header."""
    response = await client.get("/test")

    assert CORRELATION_ID_HEADER in response.headers
    assert len(response.headers[CORRELATION_ID_HEADER]) == UUID4_HEX_LENGTH


@pytest.mark.asyncio
async def test_response_echoes_incoming_correlation_id(client: AsyncClient) -> None:
    """Assert the response echoes the incoming correlation ID."""
    response = await client.get("/test", headers={CORRELATION_ID_HEADER: "echo-me"})

    assert response.headers[CORRELATION_ID_HEADER] == "echo-me"


@pytest.mark.asyncio
async def test_sets_endpoint_context(client: AsyncClient) -> None:
    """Assert the endpoint path is set in the log context."""
    response = await client.get("/test")
    data = response.json()

    assert data["endpoint"] == "/test"


@pytest.mark.asyncio
async def test_clears_context_after_request(client: AsyncClient) -> None:
    """Assert clear_log_context is scheduled as a background task on the response."""
    with patch("app.core.middleware.log_context.clear_log_context") as mock_clear:
        await client.get("/test")

    mock_clear.assert_called_once()


@pytest.mark.asyncio
async def test_clears_context_on_error(client: AsyncClient) -> None:
    """Assert context vars are reset even when handler raises."""
    with pytest.raises(ValueError, match="boom"):
        await client.get("/error")

    for var in _CONTEXT_VARS.values():
        assert var.get() == "-"
