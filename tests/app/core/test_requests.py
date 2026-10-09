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

"""Define tests for the app.core.requests module."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientResponseError, ClientSession, ContentTypeError
from aioresponses import aioresponses
from fastapi import HTTPException, status
from pydantic import HttpUrl

from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPGoneException,
    HTTPInternalServerErrorException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.requests import RemoteAPI
from app.core.requests.remote_api import (
    BaseRemoteAPI,
    UPSTREAM_NON_JSON_HEADER,
)


@pytest.fixture
def base_url():
    """Fixture to provide the base URL for the API."""
    return "http://localhost:8000/"


@pytest.fixture
def api_key():
    """Fixture to provide a test API key."""
    return "test_api_key"


@pytest.fixture
def remote_api(base_url):
    """Fixture to initialize the RemoteAPI instance."""
    return RemoteAPI(
        endpoint=base_url,
        api_key="test_api_key",
        auth_scheme="Bearer",
        error_detail_key="detail",
        error_code_key="code",
    )


@pytest.fixture(autouse=True)
def clear_ssl_context_cache():
    """Reset SSL context lru_cache so certfile tests do not share state."""
    BaseRemoteAPI.create_ssl_context.cache_clear()
    yield
    BaseRemoteAPI.create_ssl_context.cache_clear()


@pytest.mark.asyncio
async def test_context_manager_open_close(remote_api, base_url):
    """Test the RemoteAPI context manager for opening and closing the session."""
    with aioresponses() as m:
        m.get(base_url, status=200, payload={})

        assert remote_api.session is None

        async with remote_api:
            assert remote_api.session is not None
            assert isinstance(remote_api.session, ClientSession)
            response = await remote_api.session.get("/")
            assert response.status == status.HTTP_200_OK

        assert remote_api.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method, test_path, request_payload, response_data, status_code"),  # noqa: PT006
    [
        ("GET", "test/get", None, {"key": "value"}, 200),
        ("POST", "test/post", {"input": "data"}, {"result": "success"}, 201),
        ("PUT", "test/put", {"update": "value"}, {"result": "updated"}, 200),
        ("PATCH", "test/patch", {"patch": "value"}, {"result": "patched"}, 200),
        ("DELETE", "test/delete", None, {"result": "deleted"}, 200),
    ],
)
async def test_request_methods(
    remote_api, base_url, method, test_path, request_payload, response_data, status_code
):
    """Test various HTTP request methods supported by RemoteAPI."""
    full_url = base_url + test_path

    with aioresponses() as m:
        m.add(full_url, method, status=status_code, payload=response_data)

        async with remote_api:
            if method == "GET":
                response = await remote_api.get(test_path)
            elif method == "POST":
                response = await remote_api.post(test_path, json=request_payload)
            elif method == "PUT":
                response = await remote_api.put(test_path, json=request_payload)
            elif method == "PATCH":
                response = await remote_api.patch(test_path, json=request_payload)
            elif method == "DELETE":
                response = await remote_api.delete(test_path)
            else:
                raise AssertionError(f"unparametrized method: {method}")

            assert response == response_data


@pytest.mark.asyncio
async def test_delete_204_no_content_returns_none(remote_api, base_url):
    """Ensure HTTP 204 yields ``None`` so callers do not confuse no body with ``{}``."""
    test_path = "test/delete-no-body"
    full_url = base_url + test_path
    with aioresponses() as m:
        m.delete(full_url, status=status.HTTP_204_NO_CONTENT)
        async with remote_api:
            response = await remote_api.delete(test_path)
            assert response is None


@pytest.mark.parametrize(
    ("endpoint, input_path, expected_path"),  # noqa: PT006
    [
        ("http://localhost:8000/", "users", "/users"),
        ("http://localhost:8000/api", "v1/users", "/api/v1/users"),
    ],
)
def test_prepare_path(endpoint: str, input_path: str, expected_path: str):
    """Test the path preparation logic in RemoteAPI."""
    remote_api = RemoteAPI(
        endpoint=endpoint,
        api_key="test_api_key",
        auth_scheme="Bearer",
    )
    assert remote_api.prepare_path(input_path) == expected_path


@pytest.mark.asyncio
async def test_request_error(remote_api):
    """Map a coded 409 to HTTPConflictException with the X-Error-Code header intact.

    Every mapped class now accepts a ``headers`` kwarg, so a coded body no longer
    forces the bare-``HTTPException`` fallback: a JSON 409 surfaces as
    :class:`HTTPConflictException` and the error-code header survives.
    """
    conflict_error_msg = "Task with the same name already exists."
    conflict_error_code = "DUP_ERROR"
    mock_response = AsyncMock()
    mock_response.json.return_value = {
        "detail": conflict_error_msg,
        "code": conflict_error_code,
    }
    mock_response.status = status.HTTP_409_CONFLICT
    error = ClientResponseError(
        request_info=None,
        history=None,
        status=status.HTTP_409_CONFLICT,
        message="Conflict",
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPConflictException) as exc_info:
            await remote_api.request("GET", "/non-existent-path")
        assert type(exc_info.value) is HTTPConflictException
        assert exc_info.value.status_code == status.HTTP_409_CONFLICT
        assert exc_info.value.detail == conflict_error_msg
        assert exc_info.value.headers == {"X-Error-Code": conflict_error_code}


@pytest.mark.parametrize(
    ("error_status", "exc_class"),
    [
        (status.HTTP_400_BAD_REQUEST, HTTPBadRequestException),
        (status.HTTP_404_NOT_FOUND, HTTPNotFoundException),
        (status.HTTP_409_CONFLICT, HTTPConflictException),
        (status.HTTP_410_GONE, HTTPGoneException),
        (status.HTTP_422_UNPROCESSABLE_CONTENT, HTTPUnprocessableEntityException),
        (status.HTTP_500_INTERNAL_SERVER_ERROR, HTTPInternalServerErrorException),
        (status.HTTP_502_BAD_GATEWAY, HTTPBadGatewayException),
        (status.HTTP_503_SERVICE_UNAVAILABLE, HTTPServiceUnavailableException),
    ],
)
@pytest.mark.asyncio
async def test_request_maps_error_status_to_project_exception(
    remote_api, error_status, exc_class
):
    """Raise the mapped project exception for each mapped upstream error status."""
    detail_msg = "upstream detail"
    mock_response = AsyncMock()
    mock_response.json.return_value = {"detail": detail_msg}
    mock_response.status = error_status
    error = ClientResponseError(
        request_info=None, history=None, status=error_status, message="err"
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    no_code_api = remote_api.model_copy(update={"error_code_key": None})

    with patch.object(no_code_api, "_request", return_value=mock_context_manager):
        with pytest.raises(exc_class) as exc_info:
            await no_code_api.request("GET", "/mapped-error")
        assert type(exc_info.value) is exc_class
        assert exc_info.value.status_code == error_status
        assert exc_info.value.detail == detail_msg


@pytest.mark.asyncio
async def test_request_410_with_error_code_preserves_headers_as_gone(remote_api):
    """Map a 410 carrying an error code to HTTPGoneException with the header intact."""
    mock_response = AsyncMock()
    mock_response.json.return_value = {"detail": "gone", "code": "TASK_GONE"}
    mock_response.status = status.HTTP_410_GONE
    error = ClientResponseError(
        request_info=None, history=None, status=status.HTTP_410_GONE, message="Gone"
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPGoneException) as exc_info:
            await remote_api.request("GET", "/gone")
        assert type(exc_info.value) is HTTPGoneException
        assert exc_info.value.status_code == status.HTTP_410_GONE
        assert exc_info.value.headers == {"X-Error-Code": "TASK_GONE"}


@pytest.mark.asyncio
async def test_request_404_with_error_code_preserves_headers_as_not_found(remote_api):
    """Map a 404 carrying an error code to HTTPNotFoundException with the header intact.

    PMM's gRPC-gateway 404 returns ``{"code": 5, ...}``; the coded body must still
    surface as :class:`HTTPNotFoundException` (not a bare HTTPException) so consumers
    can narrow to ``except HTTPNotFoundException``.
    """
    mock_response = AsyncMock()
    mock_response.json.return_value = {"detail": "missing", "code": "NOT_FOUND"}
    mock_response.status = status.HTTP_404_NOT_FOUND
    error = ClientResponseError(
        request_info=None,
        history=None,
        status=status.HTTP_404_NOT_FOUND,
        message="Not Found",
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPNotFoundException) as exc_info:
            await remote_api.request("GET", "/missing")
        assert type(exc_info.value) is HTTPNotFoundException
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert exc_info.value.detail == "missing"
        assert exc_info.value.headers == {"X-Error-Code": "NOT_FOUND"}


@pytest.mark.asyncio
async def test_request_non_json_404_stays_bare_http_exception(remote_api):
    """Keep a non-JSON (proxy) 404 a bare HTTPException, not HTTPNotFoundException.

    A non-JSON body marks a proxy/gateway failure. It must not surface as
    :class:`HTTPNotFoundException`, or callers narrowing to
    ``except HTTPNotFoundException`` (e.g. the inventory selector proxy) would
    mistake an upstream infra failure for a real resource-absent response and
    silently swallow it.
    """
    mock_response = AsyncMock()
    mock_response.json = AsyncMock(
        side_effect=ContentTypeError(
            request_info=None,
            history=(),
            status=status.HTTP_404_NOT_FOUND,
        )
    )
    mock_response.status = status.HTTP_404_NOT_FOUND
    mock_response.text = AsyncMock(return_value="<html>404 Not Found</html>")

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPException) as exc_info:
            await remote_api.request("GET", "/proxy-404")
        assert type(exc_info.value) is HTTPException
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}


@pytest.mark.parametrize(
    ("error_status", "exc_class"),
    [
        (status.HTTP_502_BAD_GATEWAY, HTTPBadGatewayException),
        (status.HTTP_503_SERVICE_UNAVAILABLE, HTTPServiceUnavailableException),
    ],
)
@pytest.mark.asyncio
async def test_request_non_json_mapped_non_404_keeps_mapping_with_header(
    remote_api, error_status, exc_class
):
    """Map a non-JSON error at a mapped non-404 status to its class, header stamped.

    Only a non-JSON 404 degrades to a bare HTTPException (a proxy 404 must not be
    mistaken for a real "resource absent"). Every other mapped status keeps its
    class on a non-JSON body just as it does on a JSON body, carrying
    ``UPSTREAM_NON_JSON_HEADER`` -- so an nginx-in-front-of-PMM 502/503 surfaces as
    ``HTTPBadGatewayException`` / ``HTTPServiceUnavailableException`` and reaches
    ``json_exception_handler`` rather than falling back to a flash-and-redirect.
    """
    mock_response = AsyncMock()
    mock_response.json = AsyncMock(
        side_effect=ContentTypeError(
            request_info=None,
            history=(),
            status=error_status,
        )
    )
    mock_response.status = error_status
    mock_response.text = AsyncMock(return_value="<html>bad gateway</html>")

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(exc_class) as exc_info:
            await remote_api.request("GET", "/proxy-5xx")
        assert type(exc_info.value) is exc_class
        assert exc_info.value.status_code == error_status
        assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}


@pytest.mark.asyncio
async def test_request_unmapped_status_raises_bare_http_exception(remote_api):
    """Raise a bare HTTPException for an error status with no mapped project class."""
    mock_response = AsyncMock()
    mock_response.json.return_value = {"detail": "slow down"}
    mock_response.status = status.HTTP_429_TOO_MANY_REQUESTS
    error = ClientResponseError(
        request_info=None,
        history=None,
        status=status.HTTP_429_TOO_MANY_REQUESTS,
        message="Too Many Requests",
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    no_code_api = remote_api.model_copy(update={"error_code_key": None})

    with patch.object(no_code_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPException) as exc_info:
            await no_code_api.request("GET", "/rate-limited")
        assert type(exc_info.value) is HTTPException
        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS


def test_base_remote_api_hash_stable():
    """BaseRemoteAPI instances with the same config share the same hash."""
    a = BaseRemoteAPI(endpoint=HttpUrl("http://example.com/"))
    b = BaseRemoteAPI(endpoint=HttpUrl("http://example.com/"))
    assert hash(a) == hash(b)


def test_base_remote_api_headers_default():
    """BaseRemoteAPI exposes empty default headers."""
    api = BaseRemoteAPI(endpoint=HttpUrl("http://example.com/"))
    assert api.headers == {}


def test_base_url_strips_api_path():
    """base_url drops the API path segment from the origin."""
    api = RemoteAPI(endpoint=HttpUrl("http://localhost:8000/api"))
    assert api.base_path == "/api"
    assert api.base_url == "http://localhost:8000"


def test_prepare_path_trailing_slash():
    """prepare_path preserves a trailing slash on the requested path."""
    api = RemoteAPI(endpoint=HttpUrl("http://localhost:8000/api/"))
    assert api.prepare_path("v1/users/") == "/api/v1/users/"


def test_prepare_path_when_base_path_is_root():
    """When the endpoint path is root, paths join against ``/``."""
    api = RemoteAPI(endpoint=HttpUrl("http://localhost:8000/"))
    assert api.prepare_path("items") == "/items"


@pytest.mark.asyncio
async def test_aexit_when_session_already_closed(remote_api):
    """Second __aexit__ hits the branch when the session is already cleared."""
    async with remote_api:
        pass
    await remote_api.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_open_and_close(remote_api, base_url):
    """open() / close() mirror async context manager lifecycle."""
    with aioresponses() as m:
        m.get(base_url, status=status.HTTP_200_OK, payload={})
        await remote_api.open()
        assert remote_api.session is not None
        await remote_api.session.get("/")
        await remote_api.close()
        assert remote_api.session is None


@pytest.mark.asyncio
async def test_nested_async_with_reuses_session(remote_api):
    """Nested async with does not create a second ClientSession."""
    with aioresponses() as m:
        m.get("http://localhost:8000/", status=status.HTTP_200_OK, payload={})
        async with remote_api:
            s1 = remote_api.session
            async with remote_api:
                assert remote_api.session is s1


@pytest.mark.asyncio
async def test_extra_headers_context_and_merge(remote_api, base_url):
    """extra_headers, set/reset, and per-request headers merge in _request."""
    with aioresponses() as m:
        m.get(
            f"{base_url}merge",
            status=status.HTTP_200_OK,
            payload={"ok": True},
        )

        async with remote_api:
            with remote_api.extra_headers({"X-Extra": "1"}):
                token = remote_api.set_extra_headers({"X-Temp": "2"})
                try:
                    await remote_api.get("merge", headers={"X-Base": "a"})
                finally:
                    remote_api.reset_extra_headers(token)


@pytest.mark.asyncio
async def test_session_setter(remote_api):
    """Session setter replaces the aiohttp ClientSession used for requests."""
    mock_session = AsyncMock(spec=ClientSession)
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = AsyncMock(status=status.HTTP_200_OK)
    mock_cm.__aexit__.return_value = None
    mock_session.request.return_value = mock_cm

    async with remote_api:
        remote_api.session = mock_session
        async with remote_api._request("GET", "/x") as resp:
            assert resp.status == status.HTTP_200_OK
        mock_session.request.assert_called()


@pytest.mark.asyncio
async def test_stream_yields_lines(remote_api, base_url):
    """stream() yields one bytes object per newline-terminated line, with the trailing newline preserved."""
    body = b"chunk1\nchunk2\n"
    with aioresponses() as m:
        m.get(f"{base_url}stream", body=body)
        async with remote_api:
            out = [c async for c in remote_api.stream("/stream")]
    assert out == [b"chunk1\n", b"chunk2\n"]


@pytest.mark.asyncio
async def test_auth_context(remote_api, base_url):
    """auth() wraps extra_headers with an Authorization value."""
    with aioresponses() as m:
        m.get(f"{base_url}auth", status=status.HTTP_200_OK, payload={})
        async with remote_api:
            with remote_api.auth("secret-token"):
                await remote_api.get("auth")


@pytest.mark.asyncio
async def test_request_debug_log_redacts_authorization_header(
    remote_api, base_url, caplog
):
    """The request debug log masks the Authorization header injected by auth()."""
    secret = "super-secret-token"
    with aioresponses() as m:
        m.get(f"{base_url}auth", status=status.HTTP_200_OK, payload={})
        with caplog.at_level("DEBUG", logger=remote_api.logger.name):
            async with remote_api:
                with remote_api.auth(secret):
                    await remote_api.get("auth")
    sending = [r.getMessage() for r in caplog.records if "Sending" in r.getMessage()]
    assert sending, "expected a request debug log line"
    assert all(secret not in msg for msg in sending)
    assert any("****" in msg for msg in sending)


@pytest.mark.asyncio
async def test_request_debug_log_redacts_url_credentials(caplog):
    """The request debug log masks a password embedded in the endpoint URL.

    The mock carries no userinfo because the session is built from the
    credential-free URL, which is what the request is matched against.
    """
    password = "hunter2"
    api = RemoteAPI(endpoint=f"http://user:{password}@localhost:8000/")
    with aioresponses() as m:
        m.get(
            "http://localhost:8000/ping",
            status=status.HTTP_200_OK,
            payload={},
        )
        with caplog.at_level("DEBUG", logger=api.logger.name):
            async with api:
                await api.get("ping")
    sending = [r.getMessage() for r in caplog.records if "Sending" in r.getMessage()]
    assert sending, "expected a request debug log line"
    assert all(password not in msg for msg in sending)


@pytest.mark.asyncio
async def test_response_debug_log_redacts_url_credentials(caplog):
    """The response debug log masks a password embedded in the endpoint URL.

    The mock carries no userinfo because the session is built from the
    credential-free URL, which is what the request is matched against.
    """
    password = "hunter2"
    api = RemoteAPI(endpoint=f"http://user:{password}@localhost:8000/")
    with aioresponses() as m:
        m.get(
            "http://localhost:8000/ping",
            status=status.HTTP_200_OK,
            payload={"ok": True},
        )
        with caplog.at_level("DEBUG", logger=api.logger.name):
            async with api:
                await api.get("ping")
    response = [r.getMessage() for r in caplog.records if "response" in r.getMessage()]
    assert response, "expected a response debug log line"
    assert all(password not in msg for msg in response)
    assert any("****" in msg for msg in response)


def test_create_ssl_context_without_certfile():
    """create_ssl_context returns a context without load_cert_chain when no cert."""
    ctx = BaseRemoteAPI.create_ssl_context()
    assert ctx is not None


def test_create_ssl_context_with_certfile(monkeypatch):
    """When certfile is set, load_cert_chain is invoked on the SSL context."""
    mock_ctx = Mock()

    def fake_create_default_context(*_args: object, **_kwargs: object) -> Mock:
        return mock_ctx

    monkeypatch.setattr(
        "app.core.requests.remote_api.create_default_context",
        fake_create_default_context,
    )
    BaseRemoteAPI.create_ssl_context.cache_clear()
    result = BaseRemoteAPI.create_ssl_context(
        cafile=None,
        certfile="/fake/cert.pem",
        keyfile="/fake/key.pem",
    )
    mock_ctx.load_cert_chain.assert_called_once_with(
        certfile="/fake/cert.pem",
        keyfile="/fake/key.pem",
    )
    assert result is mock_ctx


@pytest.mark.asyncio
async def test_request_content_type_error(remote_api):
    """Non-JSON bodies raise HTTPException when json() raises ContentTypeError."""
    mock_response = AsyncMock()
    mock_response.json = AsyncMock(
        side_effect=ContentTypeError(
            request_info=None,
            history=(),
            status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    )
    mock_response.status = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    mock_response.text = AsyncMock(return_value="not-json")

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    with patch.object(remote_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPException) as exc_info:
            await remote_api.request("GET", "/bad-content-type")
        assert exc_info.value.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        assert exc_info.value.detail == "An unexpected error occurred on the server."
        # A non-JSON body (e.g. an nginx proxy page) is marked so callers can
        # tell it apart from an app-level JSON error at the same status code.
        assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}


@pytest.mark.asyncio
async def test_request_client_response_error_without_error_code_header(remote_api):
    """ClientResponseError omits X-Error-Code when error_code_key is unset."""
    mock_response = AsyncMock()
    mock_response.json.return_value = {"detail": "gone"}
    mock_response.status = status.HTTP_410_GONE
    error = ClientResponseError(
        request_info=None,
        history=None,
        status=status.HTTP_410_GONE,
        message="Gone",
    )
    mock_response.raise_for_status = Mock(side_effect=error)

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    no_code_api = remote_api.model_copy(update={"error_code_key": None})

    with patch.object(no_code_api, "_request", return_value=mock_context_manager):
        with pytest.raises(HTTPException) as exc_info:
            await no_code_api.request("GET", "/gone")
        assert exc_info.value.status_code == status.HTTP_410_GONE
        assert exc_info.value.headers is None


@pytest.mark.asyncio
async def test_stream_yields_content_chunks_and_logs_debug_lifecycle(remote_api):
    """Stream yields aiohttp body chunks and logs start/end at DEBUG."""

    async def body():
        yield b"chunk-a\n"
        yield b"chunk-b\n"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(remote_api.logger, "debug") as mock_debug:
        async with remote_api:
            with patch.object(remote_api, "_request", return_value=mock_ctx):
                chunks = [
                    c async for c in remote_api.stream("/stream/path/", method="GET")
                ]

    assert chunks == [b"chunk-a\n", b"chunk-b\n"]
    mock_debug.assert_any_call(
        "Stream started path=%s method=%s", "/stream/path/", "GET"
    )
    mock_debug.assert_any_call(
        "Stream ended normally path=%s method=%s", "/stream/path/", "GET"
    )


@pytest.mark.asyncio
async def test_stream_logs_warning_with_exc_info_and_reraises_on_content_error(
    remote_api,
):
    """On iteration failure, stream logs WARNING with exc_info and re-raises."""

    class FailingContent:
        """Response content stub whose iter_any() raises on first iteration."""

        def iter_any(self):
            async def _gen():
                msg = "connection reset"
                raise ConnectionResetError(msg)
                yield  # pragma: no cover  # makes _gen an async generator

            return _gen()

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content = FailingContent()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(remote_api.logger, "warning") as mock_warning:
        async with remote_api:
            with (
                patch.object(remote_api, "_request", return_value=mock_ctx),
                pytest.raises(ConnectionResetError, match="connection reset"),
            ):
                [_ async for _ in remote_api.stream("/history/1/logs/", method="GET")]

    mock_warning.assert_called_once()
    args, kwargs = mock_warning.call_args
    assert args[0] == "Stream error path=%s method=%s: %s"
    assert args[1] == "/history/1/logs/"
    assert args[2] == "GET"
    assert isinstance(args[3], ConnectionResetError)
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_stream_logs_warning_when_request_context_raises(remote_api):
    """Errors from opening the HTTP context are logged and re-raised."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("session failed"))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(remote_api.logger, "warning") as mock_warning:
        async with remote_api:
            with (
                patch.object(remote_api, "_request", return_value=mock_ctx),
                pytest.raises(RuntimeError, match="session failed"),
            ):
                [_ async for _ in remote_api.stream("/x/", method="POST")]

    mock_warning.assert_called_once()
    wargs, wkwargs = mock_warning.call_args
    assert wargs[1] == "/x/"
    assert wargs[2] == "POST"
    assert isinstance(wargs[3], RuntimeError)
    assert wkwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_stream_yields_long_line_above_default_aiohttp_limit(remote_api):
    """Lines larger than aiohttp's default 128 KiB readline cap are delivered intact."""
    long_payload = b"x" * (200 * 1024)

    async def body():
        yield long_payload + b"\n"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with patch.object(remote_api, "_request", return_value=mock_ctx):
            chunks = [c async for c in remote_api.stream("/long-line/")]

    assert chunks == [long_payload + b"\n"]


@pytest.mark.asyncio
async def test_stream_flushes_trailing_partial_line_on_eof(remote_api):
    """Last line is delivered even when upstream ends without a trailing newline."""

    async def body():
        yield b"first\nsecond-no-newline"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with patch.object(remote_api, "_request", return_value=mock_ctx):
            chunks = [c async for c in remote_api.stream("/no-trailing/")]

    assert chunks == [b"first\n", b"second-no-newline"]


@pytest.mark.asyncio
async def test_stream_splits_lines_across_chunk_boundaries(remote_api):
    """A single logical line that arrives across multiple chunks is reassembled."""

    async def body():
        yield b"line-"
        yield b"split-across-"
        yield b"chunks\nnext\n"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with patch.object(remote_api, "_request", return_value=mock_ctx):
            chunks = [c async for c in remote_api.stream("/split/")]

    assert chunks == [b"line-split-across-chunks\n", b"next\n"]


@pytest.mark.asyncio
async def test_stream_raises_when_single_line_exceeds_cap(remote_api, monkeypatch):
    """A line without a newline exceeding _MAX_STREAM_LINE_BYTES raises ValueError."""
    monkeypatch.setattr("app.core.requests.remote_api._MAX_STREAM_LINE_BYTES", 1024)

    async def body():
        yield b"x" * 2048

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(ValueError, match="exceeded"),
        ):
            [_ async for _ in remote_api.stream("/runaway/")]


@pytest.mark.asyncio
async def test_stream_raises_when_single_chunk_yields_oversized_line(
    remote_api, monkeypatch
):
    """A newline-terminated line exceeding the cap is rejected before yielding."""
    monkeypatch.setattr("app.core.requests.remote_api._MAX_STREAM_LINE_BYTES", 1024)

    async def body():
        yield b"x" * 2048 + b"\n"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(ValueError, match="exceeded"),
        ):
            [_ async for _ in remote_api.stream("/oversized-line/")]


@pytest.mark.asyncio
async def test_stream_logs_warning_when_line_cap_exceeded(remote_api, monkeypatch):
    """Line-cap ValueError is logged at WARNING with exc_info before re-raising."""
    monkeypatch.setattr("app.core.requests.remote_api._MAX_STREAM_LINE_BYTES", 1024)

    async def body():
        yield b"x" * 2048 + b"\n"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(remote_api.logger, "warning") as mock_warning:
        async with remote_api:
            with (
                patch.object(remote_api, "_request", return_value=mock_ctx),
                pytest.raises(ValueError, match="exceeded"),
            ):
                [_ async for _ in remote_api.stream("/cap-log/", method="GET")]

    mock_warning.assert_called_once()
    args, kwargs = mock_warning.call_args
    assert args[0] == "Stream line cap exceeded path=%s method=%s: %s"
    assert args[1] == "/cap-log/"
    assert args[2] == "GET"
    assert isinstance(args[3], ValueError)
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_stream_chunks_yields_raw_bytes_without_line_splitting(remote_api):
    """stream_chunks() yields raw chunks from iter_any() preserving boundaries."""

    async def body():
        yield b"binary-without-newline-"
        yield b"more-binary"
        yield b"\n-and-some-after"

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with patch.object(remote_api, "_request", return_value=mock_ctx):
            chunks = [c async for c in remote_api.stream_chunks("/binary/")]

    assert chunks == [
        b"binary-without-newline-",
        b"more-binary",
        b"\n-and-some-after",
    ]


@pytest.mark.asyncio
async def test_stream_chunks_does_not_apply_line_size_cap(remote_api, monkeypatch):
    """stream_chunks() does not enforce the per-line cap (binary payloads have no newlines)."""
    monkeypatch.setattr("app.core.requests.remote_api._MAX_STREAM_LINE_BYTES", 1024)

    async def body():
        yield b"x" * 4096

    mock_response = MagicMock()
    mock_response.status = status.HTTP_200_OK
    mock_response.content.iter_any = body
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with patch.object(remote_api, "_request", return_value=mock_ctx):
            chunks = [c async for c in remote_api.stream_chunks("/binary-large/")]

    assert chunks == [b"x" * 4096]


@pytest.mark.asyncio
async def test_stream_chunks_raises_http_exception_on_error_status(remote_api):
    """Raise the mapped HTTPNotFoundException for a 404 stream without yielding anything."""
    mock_response = MagicMock()
    mock_response.status = status.HTTP_404_NOT_FOUND
    mock_response.json = AsyncMock(return_value={"detail": "missing"})
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(HTTPNotFoundException) as exc_info,
        ):
            [_ async for _ in remote_api.stream_chunks("/missing/")]

    assert type(exc_info.value) is HTTPNotFoundException
    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_stream_chunks_maps_410_to_gone_with_headers(remote_api):
    """Map a 410 stream carrying an error code to HTTPGoneException, header intact."""
    mock_response = MagicMock()
    mock_response.status = status.HTTP_410_GONE
    mock_response.json = AsyncMock(return_value={"detail": "gone", "code": "TASK_GONE"})
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(HTTPGoneException) as exc_info,
        ):
            [_ async for _ in remote_api.stream_chunks("/gone/")]

    assert type(exc_info.value) is HTTPGoneException
    assert exc_info.value.status_code == status.HTTP_410_GONE
    assert exc_info.value.headers == {"X-Error-Code": "TASK_GONE"}


@pytest.mark.asyncio
async def test_stream_chunks_coerces_numeric_error_code_to_str(remote_api):
    """Coerce a numeric stream error code to str so the X-Error-Code header holds.

    A gRPC-gateway body carries ``code`` as an int; without ``str()`` the header
    build would raise ``AttributeError`` on the stream path, mirroring the
    ``request()`` path covered by ``test_create_rule_raises_not_found_on_coded_404``.
    """
    mock_response = MagicMock()
    mock_response.status = status.HTTP_404_NOT_FOUND
    mock_response.json = AsyncMock(return_value={"detail": "missing", "code": 5})
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(HTTPNotFoundException) as exc_info,
        ):
            [_ async for _ in remote_api.stream_chunks("/missing/")]

    assert exc_info.value.headers == {"X-Error-Code": "5"}


@pytest.mark.asyncio
async def test_stream_chunks_non_json_error_stamps_upstream_header(remote_api):
    """Stamp a non-JSON stream error with UPSTREAM_NON_JSON_HEADER and keep its mapping.

    When the error body is not JSON (an HTML proxy page), ``stream_chunks`` falls
    back to the raw text and marks it non-JSON, mirroring ``RemoteAPI.request`` so
    the stream path classifies a proxy/gateway failure identically -- a 502 stays
    ``HTTPBadGatewayException`` carrying the header, not a bare HTTPException.
    """
    mock_response = MagicMock()
    mock_response.status = status.HTTP_502_BAD_GATEWAY
    mock_response.json = AsyncMock(
        side_effect=ContentTypeError(
            request_info=None,
            history=(),
            status=status.HTTP_502_BAD_GATEWAY,
        )
    )
    mock_response.text = AsyncMock(return_value="<html>502 Bad Gateway</html>")
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_ctx),
            pytest.raises(HTTPBadGatewayException) as exc_info,
        ):
            [_ async for _ in remote_api.stream_chunks("/proxy-5xx/")]

    assert type(exc_info.value) is HTTPBadGatewayException
    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert exc_info.value.detail == "<html>502 Bad Gateway</html>"
    assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}


@pytest.mark.asyncio
async def test_stream_chunks_error_status_with_non_dict_json_body(remote_api):
    """Map a streaming error status whose JSON body is a list, not an object."""
    mock_response = AsyncMock()
    mock_response.status = status.HTTP_502_BAD_GATEWAY
    mock_response.json = AsyncMock(return_value=[{"loc": "body", "msg": "invalid"}])

    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_response
    mock_context_manager.__aexit__.return_value = None

    async with remote_api:
        with (
            patch.object(remote_api, "_request", return_value=mock_context_manager),
            pytest.raises(HTTPBadGatewayException) as exc_info,
        ):
            [_ async for _ in remote_api.stream_chunks("/list-error/")]
    assert exc_info.value.detail == "An unexpected error occurred on the server."
