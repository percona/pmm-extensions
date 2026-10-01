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

"""Define tests for RemoteAPI request-logging helpers and the upload primitive."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from aiohttp import encode_basic_auth, web
from aioresponses import aioresponses
from fastapi import HTTPException, status
from pydantic import computed_field, HttpUrl

from app.core.auth.providers.casdoor.provider import CasdoorAuthProvider
from app.core.auth.providers.casdoor.sdk import CasdoorSDK
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.auth.providers.grafana.sdk import GrafanaSDK
from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPConflictException,
    HTTPNotFoundException,
)
from app.core.requests import RemoteAPI
from app.core.requests.remote_api import (
    _iter_lines_from_chunks,
    _NON_JSON_LOG_MAX_CHARS,
    _REDACTED_VALUE,
    _sanitize_request_kwargs,
    _TRUNCATION_MARKER,
    _WITHHELD_BODY,
    as_json_array,
    as_json_object,
    BaseRemoteAPI,
    is_non_json_success,
    PendingCloses,
    UPSTREAM_NON_JSON_HEADER,
)
from app.core.requests.remote_api import (
    _MAX_STREAM_LINE_BYTES as _REAL_CAP,
)
from app.core.utils.fields import (
    CREDENTIAL_URL_MASK,
    PRESERVE_CREDENTIALS_CONTEXT,
    strip_credential_url_userinfo,
)
from app.extensions.clients.pmm import PMMRemoteAPI
from app.tasks.execution.executors.nomad.models import NomadExecutor
from tests.app.scan_recording import ScanRecordingBytearray

_UPLOAD_URL = "http://localhost:8000/upload"
_RESPONSE_URL = "http://localhost:8000/body"
_BODY_SENTINEL = "sentinel-response-value"
_LATER_BODY_SENTINEL = "later-response-value"
_CREDENTIAL_ENDPOINT = "http://svcuser:svcpass@remote.internal:9000/api/inventory"
_CREDENTIAL_SECRET = "svcpass"
_REDACTED_BASE_URL = "http://svcuser:****@remote.internal:9000"
_LIVE_BASE_URL = "http://svcuser:svcpass@remote.internal:9000"


@pytest.fixture
def remote_api() -> RemoteAPI:
    """Provide a real RemoteAPI client pointed at a local base URL."""
    return RemoteAPI(endpoint="http://localhost:8000/")


def _logged_non_json_body(records: list[logging.LogRecord]) -> str:
    """Return the body argument of the non-JSON response log record.

    Reads the record's own argument rather than the rendered line, so the
    assertion does not restate the format string under test.

    :param records: Records captured while the request was issued.
    :return: The body the record carries as its last argument.
    :raises AssertionError: If no non-JSON response record was emitted.
    """
    for record in records:
        if "response content" in record.msg and isinstance(record.args, tuple):
            return str(record.args[-1])
    raise AssertionError("no non-JSON response log record was emitted")


def _one_file() -> dict:
    """Return a single-file multipart mapping for upload tests."""
    return {"file": ("bundle.tar.gz", b"bundle-bytes", "application/octet-stream")}


def test_redacts_sensitive_headers():
    """Verify credential-bearing headers are masked, others preserved."""
    safe = _sanitize_request_kwargs(
        {"headers": {"Authorization": "Bearer x", "Accept": "application/json"}}
    )

    assert safe["headers"]["Authorization"] == _REDACTED_VALUE
    assert safe["headers"]["Accept"] == "application/json"


def test_redacts_password_in_json_body():
    """Verify a password in a JSON body is masked in the logged copy."""
    safe = _sanitize_request_kwargs({"json": {"user": "alice", "password": "secret"}})

    assert safe["json"]["password"] == _REDACTED_VALUE
    assert safe["json"]["user"] == "alice"


def test_redacts_password_in_form_data_body():
    """Verify a password in a form ``data`` body is masked in the logged copy."""
    safe = _sanitize_request_kwargs(
        {"data": {"grant_type": "password", "password": "secret"}}
    )

    assert safe["data"]["password"] == _REDACTED_VALUE
    assert safe["data"]["grant_type"] == "password"


def test_does_not_mutate_the_original_kwargs():
    """Verify the outgoing request keeps its real credentials (copy is masked)."""
    kwargs = {
        "headers": {"Authorization": "Bearer x"},
        "json": {"password": "secret"},
    }

    _sanitize_request_kwargs(kwargs)

    assert kwargs["headers"]["Authorization"] == "Bearer x"
    assert kwargs["json"]["password"] == "secret"


def test_passes_through_non_dict_body():
    """Verify a non-mapping body is left untouched."""
    safe = _sanitize_request_kwargs({"data": b"raw-bytes"})

    assert safe["data"] == b"raw-bytes"


def test_extra_sensitive_headers_masked():
    """Mask a caller-supplied custom header name via the extra-redaction keyword."""
    safe = _sanitize_request_kwargs(
        {"headers": {"X-Custom-Token": "raw-secret", "Accept": "application/json"}},
        extra_sensitive_headers=frozenset({"x-custom-token"}),
    )

    assert safe["headers"]["X-Custom-Token"] == _REDACTED_VALUE
    assert safe["headers"]["Accept"] == "application/json"


def test_extra_sensitive_headers_defaults_to_existing_behavior():
    """Keep a custom header untouched when no extra redaction is requested."""
    safe = _sanitize_request_kwargs({"headers": {"X-Custom-Token": "raw-secret"}})

    assert safe["headers"]["X-Custom-Token"] == "raw-secret"


def test_extra_sensitive_body_fields_masked():
    """Mask a caller-supplied custom body key via the extra-redaction keyword."""
    safe = _sanitize_request_kwargs(
        {"json": {"client_token": "raw-secret", "ticket_number": "CS0001"}},
        extra_sensitive_body_fields=frozenset({"client_token"}),
    )

    assert safe["json"]["client_token"] == _REDACTED_VALUE
    assert safe["json"]["ticket_number"] == "CS0001"


def test_extra_sensitive_body_fields_defaults_to_existing_behavior():
    """Keep a custom body key untouched when no extra redaction is requested."""
    safe = _sanitize_request_kwargs({"json": {"client_token": "raw-secret"}})

    assert safe["json"]["client_token"] == "raw-secret"


def test_extra_sensitive_body_fields_keeps_built_in_masking():
    """Mask the always-sensitive body keys alongside the caller-supplied ones."""
    safe = _sanitize_request_kwargs(
        {"json": {"password": "pw", "client_token": "raw-secret"}},
        extra_sensitive_body_fields=frozenset({"client_token"}),
    )

    assert safe["json"]["password"] == _REDACTED_VALUE
    assert safe["json"]["client_token"] == _REDACTED_VALUE


def test_redact_headers_masks_within_context_only(remote_api):
    """Mask extra header names only for the duration of the ``redact_headers`` block."""
    with remote_api.redact_headers(["X-Custom-Token"]):
        active = remote_api._extra_sensitive_headers.get()
    after = remote_api._extra_sensitive_headers.get()

    assert active == frozenset({"x-custom-token"})
    assert after == frozenset()


def test_redact_headers_nesting_unions_with_outer_context(remote_api):
    """Accumulate an inner ``redact_headers`` block's names on top of the outer set."""
    with remote_api.redact_headers(["X-Outer"]):
        with remote_api.redact_headers(["X-Inner"]):
            nested = remote_api._extra_sensitive_headers.get()
        restored = remote_api._extra_sensitive_headers.get()

    assert nested == frozenset({"x-outer", "x-inner"})
    assert restored == frozenset({"x-outer"})


def test_redact_body_fields_masks_within_context_only(remote_api):
    """Mask extra body keys only for the duration of the ``redact_body_fields`` block."""
    with remote_api.redact_body_fields(["Client_Token"]):
        active = remote_api._extra_sensitive_body_fields.get()
    after = remote_api._extra_sensitive_body_fields.get()

    assert active == frozenset({"client_token"})
    assert after == frozenset()


def test_redact_body_fields_nesting_unions_with_outer_context(remote_api):
    """Accumulate an inner ``redact_body_fields`` block's keys on top of the outer set."""
    with remote_api.redact_body_fields(["outer_token"]):
        with remote_api.redact_body_fields(["inner_token"]):
            nested = remote_api._extra_sensitive_body_fields.get()
        restored = remote_api._extra_sensitive_body_fields.get()

    assert nested == frozenset({"outer_token", "inner_token"})
    assert restored == frozenset({"outer_token"})


class TestSuppressResponseLog:
    """Cover withholding a response body from the transport's debug log."""

    @pytest.mark.asyncio
    async def test_the_response_body_is_logged_by_default(self, remote_api, caplog):
        """Log the parsed response body when no suppression is in effect."""
        with aioresponses() as mock:
            mock.get(_RESPONSE_URL, payload={"token": _BODY_SENTINEL})
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    await remote_api.get("body")

        assert any(_BODY_SENTINEL in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_the_response_body_is_withheld_inside_the_block(
        self, remote_api, caplog
    ):
        """Log the placeholder in place of the body inside the block."""
        with aioresponses() as mock:
            mock.get(_RESPONSE_URL, payload={"token": _BODY_SENTINEL})
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with remote_api.suppress_response_log():
                        await remote_api.get("body")

        messages = [record.getMessage() for record in caplog.records]
        assert all(_BODY_SENTINEL not in message for message in messages)
        assert any(_WITHHELD_BODY in message for message in messages)

    @pytest.mark.asyncio
    async def test_non_json_response_content_is_withheld(self, remote_api, caplog):
        """Render the placeholder on the non-JSON exception line as well."""
        with aioresponses() as mock:
            mock.get(
                _RESPONSE_URL,
                status=status.HTTP_200_OK,
                body=_BODY_SENTINEL,
                content_type="text/plain",
            )
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with remote_api.suppress_response_log():
                        with pytest.raises(HTTPException):
                            await remote_api.get("body")

        messages = [record.getMessage() for record in caplog.records]
        assert all(_BODY_SENTINEL not in message for message in messages)
        assert any(_WITHHELD_BODY in message for message in messages)

    @pytest.mark.asyncio
    async def test_a_later_call_logs_its_body_again(self, remote_api, caplog):
        """Restore body logging for a call issued after the block exits."""
        with aioresponses() as mock:
            mock.get(_RESPONSE_URL, payload={"token": _BODY_SENTINEL})
            mock.get(_RESPONSE_URL, payload={"token": _LATER_BODY_SENTINEL})
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with remote_api.suppress_response_log():
                        await remote_api.get("body")
                    await remote_api.get("body")

        messages = [record.getMessage() for record in caplog.records]
        assert all(_BODY_SENTINEL not in message for message in messages)
        assert any(_LATER_BODY_SENTINEL in message for message in messages)

    def test_nesting_restores_the_outer_suppression(self, remote_api):
        """Leave an outer block's suppression standing when an inner one exits."""
        with remote_api.suppress_response_log():
            with remote_api.suppress_response_log():
                pass
            nested = remote_api._suppress_response_log.get()
        after = remote_api._suppress_response_log.get()

        assert nested is True
        assert after is False


class TestNonJsonResponseLogging:
    """Cover the body a non-JSON response contributes to the exception log."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(_BODY_SENTINEL, _BODY_SENTINEL, id="short"),
            pytest.param(
                "y" * _NON_JSON_LOG_MAX_CHARS,
                "y" * _NON_JSON_LOG_MAX_CHARS,
                id="at-the-cap",
            ),
            pytest.param(
                f"{'x' * _NON_JSON_LOG_MAX_CHARS}{_BODY_SENTINEL}",
                f"{'x' * (_NON_JSON_LOG_MAX_CHARS - len(_TRUNCATION_MARKER))}"
                f"{_TRUNCATION_MARKER}",
                id="over-the-cap",
            ),
            pytest.param("", "", id="empty"),
            pytest.param(
                f"<html>\r\n  {_BODY_SENTINEL}\r\n</html>",
                f"<html>\r\n  {_BODY_SENTINEL}\r\n</html>",
                id="multiline",
            ),
        ],
    )
    async def test_the_response_text_is_logged(
        self, remote_api, caplog, body, expected
    ):
        """Log the decoded body, bounded, when no suppression is in effect."""
        with aioresponses() as mock:
            mock.get(
                _RESPONSE_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                body=body,
                content_type="text/plain",
            )
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with pytest.raises(HTTPBadGatewayException) as exc_info:
                        await remote_api.get("body")

        assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}
        assert _logged_non_json_body(caplog.records) == expected
        # Scope these guards to the non-JSON response record: the capture window
        # also includes session open/close debug lines on the same logger.
        messages = [
            record.getMessage()
            for record in caplog.records
            if "response content" in record.msg
        ]
        assert messages
        assert any(repr(expected) in message for message in messages)
        assert all("StreamReader" not in message for message in messages)
        assert all("\n" not in message for message in messages)

    async def test_a_non_json_success_text_is_logged(self, remote_api, caplog):
        """Log the body of a 2xx answer that was not JSON."""
        with aioresponses() as mock:
            mock.get(
                _RESPONSE_URL,
                status=status.HTTP_200_OK,
                body=_BODY_SENTINEL,
                content_type="text/plain",
            )
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with pytest.raises(HTTPException) as exc_info:
                        await remote_api.get("body")

        assert is_non_json_success(exc_info.value)
        assert _logged_non_json_body(caplog.records) == _BODY_SENTINEL

    async def test_a_long_response_text_is_withheld_whole(self, remote_api, caplog):
        """Withhold an oversized body outright rather than truncating it."""
        body = f"{'x' * _NON_JSON_LOG_MAX_CHARS}{_BODY_SENTINEL}"
        with aioresponses() as mock:
            mock.get(
                _RESPONSE_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                body=body,
                content_type="text/plain",
            )
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with remote_api.suppress_response_log():
                        with pytest.raises(HTTPBadGatewayException):
                            await remote_api.get("body")

        assert _logged_non_json_body(caplog.records) == _WITHHELD_BODY

    async def test_an_undecodable_response_text_is_still_logged(
        self, remote_api, caplog
    ):
        """Log an undecodable body instead of raising out of the handler.

        aiohttp falls back to UTF-8 for a body that declares no charset, so a
        strict decode here would replace the upstream failure with a
        ``UnicodeDecodeError`` raised from the logging call itself.
        """
        with aioresponses() as mock:
            mock.get(
                _RESPONSE_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                body=b"\xff\xfe\x00broken",
                content_type="text/plain",
            )
            with caplog.at_level("DEBUG", logger=remote_api.logger.name):
                async with remote_api:
                    with pytest.raises(HTTPBadGatewayException) as exc_info:
                        await remote_api.get("body")

        assert exc_info.value.headers == {UPSTREAM_NON_JSON_HEADER: "1"}
        assert "broken" in _logged_non_json_body(caplog.records)


class TestUpload:
    """Cover the multipart ``RemoteAPI.upload`` primitive."""

    pytestmark = pytest.mark.asyncio

    async def test_returns_parsed_json_body(self, remote_api):
        """Return the parsed JSON body on a 2xx JSON response."""
        with aioresponses() as mock:
            mock.post(_UPLOAD_URL, status=status.HTTP_200_OK, payload={"ok": True})
            async with remote_api:
                result = await remote_api.upload(
                    "upload", files=_one_file(), fields={"client_id": "acme"}
                )

        assert result == {"ok": True}

    async def test_sends_multipart_content_type_with_boundary(self, remote_api):
        """Send a ``multipart/form-data`` Content-Type carrying a boundary, not JSON."""
        with aioresponses() as mock:
            mock.post(_UPLOAD_URL, status=status.HTTP_200_OK, payload={"ok": True})
            async with remote_api:
                await remote_api.upload(
                    "upload", files=_one_file(), fields={"client_id": "acme"}
                )
            request = next(iter(mock.requests.values()))[0]

        content_type = request.kwargs["headers"]["Content-Type"]
        assert content_type.startswith("multipart/form-data")
        assert "boundary=" in content_type

    async def test_maps_conflict_to_project_exception(self, remote_api):
        """Map a 409 response to ``HTTPConflictException`` via ``request``."""
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_409_CONFLICT,
                payload={"detail": "already ingested"},
            )
            async with remote_api:
                with pytest.raises(HTTPConflictException):
                    await remote_api.upload("upload", files=_one_file())

    async def test_maps_bad_gateway_to_project_exception(self, remote_api):
        """Map a 502 response to ``HTTPBadGatewayException`` via ``request``."""
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                payload={"detail": "upstream down"},
            )
            async with remote_api:
                with pytest.raises(HTTPBadGatewayException):
                    await remote_api.upload("upload", files=_one_file())

    async def test_carries_upstream_not_found_detail(self, remote_api):
        """Carry an upstream 404's body ``detail`` onto ``HTTPNotFoundException``.

        Sub-app routes discriminate two 404 conditions by ``detail`` alone, so a
        proxy route can only relay that distinction if the upstream string survives
        the mapping rather than collapsing to the exception's default.
        """
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_404_NOT_FOUND,
                payload={
                    "detail": "System observation not collected yet for this node"
                },
            )
            async with remote_api:
                with pytest.raises(HTTPNotFoundException) as exc_info:
                    await remote_api.upload("upload", files=_one_file())

        assert (
            exc_info.value.detail
            == "System observation not collected yet for this node"
        )

    async def test_non_json_not_found_stays_unmapped(self, remote_api):
        """Leave a non-JSON 404 as a bare ``HTTPException``.

        A 404 with a non-JSON body comes from proxy or gateway infrastructure, not
        from an app route answering "this resource is absent". Mapping it would let
        a caller narrowing to ``HTTPNotFoundException`` to read an uncollected
        observation treat an infrastructure failure as a real absence.
        """
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_404_NOT_FOUND,
                body="<html>404 not found</html>",
                content_type="text/html",
            )
            async with remote_api:
                with pytest.raises(HTTPException) as exc_info:
                    await remote_api.upload("upload", files=_one_file())

        assert not isinstance(exc_info.value, HTTPNotFoundException)
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert exc_info.value.headers.get(UPSTREAM_NON_JSON_HEADER) == "1"

    async def test_not_found_without_detail_key_falls_back(self, remote_api):
        """Fall back to the generic detail for a JSON 404 carrying no ``detail``.

        Routes that discriminate 404 conditions by ``detail`` must not read the
        fallback as one of their own strings, so pin what a detail-less upstream
        body produces.
        """
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_404_NOT_FOUND,
                payload={"message": "gone"},
            )
            async with remote_api:
                with pytest.raises(HTTPNotFoundException) as exc_info:
                    await remote_api.upload("upload", files=_one_file())

        assert exc_info.value.detail == "An unexpected error occurred on the server."

    async def test_non_json_success_body_returns_none(self, remote_api):
        """Return ``None`` for a 2xx response whose body is not JSON."""
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_201_CREATED,
                body="OK",
                content_type="text/plain",
            )
            async with remote_api:
                result = await remote_api.upload("upload", files=_one_file())

        assert result is None

    async def test_non_json_error_body_still_raises_stamped(self, remote_api):
        """Raise the stamped upstream error for a non-JSON 4xx/5xx body."""
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                body="<html>bad gateway</html>",
                content_type="text/html",
            )
            async with remote_api:
                with pytest.raises(HTTPException) as exc_info:
                    await remote_api.upload("upload", files=_one_file())

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
        assert exc_info.value.headers.get(UPSTREAM_NON_JSON_HEADER) == "1"

    async def test_error_status_with_non_dict_json_body(self, remote_api):
        """Map an error status whose JSON body is a list, not an object."""
        with aioresponses() as mock:
            mock.post(
                _UPLOAD_URL,
                status=status.HTTP_502_BAD_GATEWAY,
                payload=[{"loc": "body", "msg": "invalid"}],
            )
            async with remote_api:
                with pytest.raises(HTTPBadGatewayException) as exc_info:
                    await remote_api.upload("upload", files=_one_file())

        assert exc_info.value.detail == "An unexpected error occurred on the server."


class TestIsNonJsonSuccess:
    """Cover the predicate that tells a non-JSON 2xx from a real upstream error."""

    @pytest.mark.parametrize(
        "status_code", [status.HTTP_200_OK, status.HTTP_201_CREATED]
    )
    def test_a_stamped_success_status_is_a_success(self, status_code):
        """Read a stamped 2xx as the success ``request`` parsed the body out of."""
        exc = HTTPException(
            status_code=status_code,
            detail="An unexpected error occurred on the server.",
            headers={UPSTREAM_NON_JSON_HEADER: "1"},
        )

        assert is_non_json_success(exc) is True

    def test_a_stamped_error_status_stays_an_error(self):
        """Keep a non-JSON 502 an error; the stamp alone does not excuse it."""
        exc = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="An unexpected error occurred on the server.",
            headers={UPSTREAM_NON_JSON_HEADER: "1"},
        )

        assert is_non_json_success(exc) is False

    def test_an_unstamped_success_status_is_not_one(self):
        """Reject a 2xx raised for another reason, such as an unfollowed redirect."""
        exc = HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            detail="The server answered with an unfollowed redirect.",
        )

        assert is_non_json_success(exc) is False


class TestDrainOnRebind:
    """Cover the in-flight accounting behind ``hold`` and ``close_when_idle``."""

    pytestmark = pytest.mark.asyncio

    async def test_idle_client_closes_immediately(self, remote_api):
        """Close synchronously when no consumer holds the client."""
        await remote_api.open()

        await remote_api.close_when_idle()

        assert remote_api._session is None

    async def test_active_hold_defers_the_close(self, remote_api):
        """Keep the session open until the holder releases it."""
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close_when_idle()
            assert remote_api._session is not None

        assert remote_api._session is None

    async def test_nested_holds_close_once_at_zero(self, remote_api):
        """Close on the outermost release, not on an inner one."""
        await remote_api.open()

        async with remote_api.hold():
            async with remote_api.hold():
                await remote_api.close_when_idle()
            assert remote_api._session is not None

        assert remote_api._session is None

    async def test_request_takes_its_own_hold(self, remote_api):
        """Keep the session open when a rebind lands mid-call with no outer hold."""
        with aioresponses() as mock:
            mock.post(_UPLOAD_URL, status=status.HTTP_200_OK, payload={"ok": True})
            await remote_api.open()
            async with remote_api._request("POST", "upload"):
                await remote_api.close_when_idle()
                assert remote_api._session is not None

        assert remote_api._session is None

    async def test_release_on_exception_still_closes(self, remote_api):
        """Perform the deferred close even when the held block raises."""
        await remote_api.open()

        async def consumer() -> None:
            async with remote_api.hold():
                await remote_api.close_when_idle()
                raise RuntimeError("consumer blew up")

        with pytest.raises(RuntimeError, match="consumer blew up"):
            await consumer()

        assert remote_api._session is None

    async def test_release_on_cancellation_still_closes(self, remote_api):
        """Perform the deferred close when the consuming task is cancelled."""
        await remote_api.open()
        held = asyncio.Event()

        async def consumer() -> None:
            async with remote_api.hold():
                held.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(consumer())
        await asyncio.wait_for(held.wait(), timeout=5)
        await remote_api.close_when_idle()
        assert remote_api._session is not None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert remote_api._session is None

    async def test_hold_alone_never_closes(self, remote_api):
        """Leave the session open when no rebind asked for a close."""
        await remote_api.open()

        async with remote_api.hold():
            pass

        assert remote_api._session is not None
        await remote_api.close()

    async def test_repeated_close_when_idle_closes_once(self, remote_api):
        """Treat a second rebind before the first drains as a no-op."""
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close_when_idle()
            await remote_api.close_when_idle()
            assert remote_api._session is not None

        assert remote_api._session is None

    async def test_flag_is_cleared_after_the_deferred_close(self, remote_api):
        """Leave a reopened client unaffected by the drain that already fired."""
        await remote_api.open()
        async with remote_api.hold():
            await remote_api.close_when_idle()

        await remote_api.open()
        async with remote_api.hold():
            pass

        assert remote_api._session is not None
        await remote_api.close()

    async def test_close_still_closes_unconditionally(self, remote_api):
        """Keep ``close`` immediate so ``close_all`` and shutdown are unchanged."""
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close()
            assert remote_api._session is None

    async def test_deferred_close_registers_then_discards_on_drain(self, remote_api):
        """Track a deferred close on the owner's pending set until the hold ends."""
        pending = PendingCloses()
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            assert id(remote_api) in pending._clients

        assert id(remote_api) not in pending._clients
        assert remote_api._session is None

    async def test_force_close_closes_a_still_held_client(self, remote_api):
        """Force-close a deferred client whose holder has not released yet."""
        pending = PendingCloses()
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            await pending.force_close()
            assert remote_api._session is None
            assert pending._clients == {}
            assert remote_api._close_when_idle is False

        await pending.force_close()

    async def test_force_close_clears_flag_so_hold_does_not_close_again(
        self, remote_api, mocker
    ):
        """Avoid a second close when force_close races a draining hold."""
        pending = PendingCloses()
        await remote_api.open()
        closes = 0
        original = BaseRemoteAPI.close

        async def counting_close(self: BaseRemoteAPI) -> None:
            nonlocal closes
            closes += 1
            await original(self)

        mocker.patch.object(BaseRemoteAPI, "close", counting_close)

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            await pending.force_close()

        assert closes == 1

    async def test_force_close_awaits_in_progress_drain_close(self, remote_api, mocker):
        """Join a draining hold's session.close instead of missing it mid-teardown."""
        pending = PendingCloses()
        await remote_api.open()
        session = remote_api._session
        assert session is not None
        entered_close = asyncio.Event()
        finish_close = asyncio.Event()
        real_close = session.close

        async def paused_session_close() -> None:
            entered_close.set()
            await finish_close.wait()
            await real_close()

        mocker.patch.object(session, "close", paused_session_close)
        released = asyncio.Event()

        async def consumer() -> None:
            async with remote_api.hold():
                await remote_api.close_when_idle(pending=pending)
                released.set()
                await asyncio.Event().wait()

        consumer_task = asyncio.create_task(consumer())
        await asyncio.wait_for(released.wait(), timeout=5)
        consumer_task.cancel()
        # hold finally starts shielded close(); pause inside ClientSession.close.
        await asyncio.wait_for(entered_close.wait(), timeout=5)
        assert id(remote_api) in pending._clients

        force_task = asyncio.create_task(pending.force_close())
        await asyncio.sleep(0)
        assert not force_task.done()

        finish_close.set()
        await force_task
        with pytest.raises(asyncio.CancelledError):
            await consumer_task
        assert remote_api._session is None
        assert pending._clients == {}

    async def test_failed_session_close_stays_retryable(self, remote_api, mocker):
        """Keep a raised ``ClientSession.close`` from looking done or leaving pending."""
        pending = PendingCloses()
        await remote_api.open()
        session = remote_api._session
        assert session is not None
        real_close = session.close
        fail_once = True

        async def flaky_close() -> None:
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("close boom")
            await real_close()

        mocker.patch.object(session, "close", flaky_close)

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            with pytest.raises(RuntimeError, match="close boom"):
                await remote_api.close()
            assert remote_api._session is session
            assert id(remote_api) in pending._clients
            assert remote_api._close_done is None

            await remote_api.close()
            assert remote_api._session is None
            assert id(remote_api) not in pending._clients

    async def test_force_close_keeps_failed_client_for_retry(self, remote_api, mocker):
        """Leave a failed sweep's client registered for a later retry."""
        pending = PendingCloses()
        await remote_api.open()
        session = remote_api._session
        assert session is not None
        real_close = session.close
        fail_once = True

        async def flaky_close() -> None:
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("close boom")
            await real_close()

        mocker.patch.object(session, "close", flaky_close)

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            await pending.force_close()

            assert remote_api._session is session
            assert id(remote_api) in pending._clients
            assert remote_api._close_when_idle is False
            assert remote_api._close_done is None

            await pending.force_close()
            assert remote_api._session is None
            assert pending._clients == {}

    async def test_cancelling_close_waiter_does_not_cancel_shared_future(
        self, remote_api, mocker
    ):
        """Shield the shared _close_done so one cancelled joiner cannot abort it."""
        await remote_api.open()
        session = remote_api._session
        assert session is not None
        entered_close = asyncio.Event()
        finish_close = asyncio.Event()
        real_close = session.close

        async def paused_session_close() -> None:
            entered_close.set()
            await finish_close.wait()
            await real_close()

        mocker.patch.object(session, "close", paused_session_close)

        closer = asyncio.create_task(remote_api.close())
        await asyncio.wait_for(entered_close.wait(), timeout=5)
        shared = remote_api._close_done
        assert shared is not None

        waiter = asyncio.create_task(remote_api.close())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert not shared.cancelled()
        finish_close.set()
        await closer
        assert remote_api._session is None

    async def test_idle_close_does_not_register_on_pending(self, remote_api):
        """Skip pending registration when the close runs immediately."""
        pending = PendingCloses()
        await remote_api.open()

        await remote_api.close_when_idle(pending=pending)

        assert pending._clients == {}
        assert remote_api._session is None

    async def test_sealed_pending_closes_immediately_mid_hold(self, remote_api):
        """Close now when the owner's pending set is already sealed by shutdown."""
        pending = PendingCloses()
        pending.seal()
        await remote_api.open()

        async with remote_api.hold():
            await remote_api.close_when_idle(pending=pending)
            assert remote_api._session is None
            assert pending._clients == {}

    async def test_add_after_force_close_is_rejected(self, remote_api):
        """Refuse a post-sweep registration so a late rebind cannot leak."""
        pending = PendingCloses()
        await remote_api.open()
        await pending.force_close()

        assert pending.add(remote_api) is False
        await remote_api.close()


async def _achunks(chunks: list[bytes]) -> AsyncGenerator[bytes, None]:
    """Yield each chunk from ``chunks`` as an async iterator.

    :param chunks: The chunk payloads, in arrival order.
    :yield: Each chunk unchanged.
    """
    for chunk in chunks:
        yield chunk


def _replay_with_full_scans(
    chunks: list[bytes], cap: int
) -> tuple[list[bytes], int | None, bytes]:
    """Replay ``chunks`` through the unnarrowed loop as an equivalence oracle.

    Mirrors what ``_iter_lines_from_chunks`` did before the search was narrowed:
    one cursor, restarting the search at ``0`` on every chunk.

    :param chunks: The chunk payloads, in arrival order.
    :param cap: The per-line byte cap to enforce.
    :return: The lines yielded, the size reported by the cap violation that
        stopped the replay (``None`` when none did), and the bytes still
        buffered when the replay ended.
    """
    lines: list[bytes] = []
    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        offset = 0
        while True:
            newline_pos = buffer.find(b"\n", offset)
            if newline_pos == -1:
                break
            line_end = newline_pos + 1
            line_size = line_end - offset
            if line_size > cap:
                return lines, line_size, bytes(buffer)
            lines.append(bytes(buffer[offset:line_end]))
            offset = line_end
        if offset:
            del buffer[:offset]
        if len(buffer) > cap:
            return lines, len(buffer), bytes(buffer)
    if buffer:
        if len(buffer) > cap:
            return lines, len(buffer), bytes(buffer)
        lines.append(bytes(buffer))
    return lines, None, bytes(buffer)


@pytest.fixture
def recorded_buffers(monkeypatch: pytest.MonkeyPatch) -> list[ScanRecordingBytearray]:
    """Make ``_iter_lines_from_chunks`` build scan-recording buffers.

    The function owns its buffer and takes no injection point, so the module
    global shadows the builtin for the duration of the test.

    :param monkeypatch: The pytest monkeypatch fixture.
    :return: The list the factory appends each buffer it builds to.
    """
    created: list[ScanRecordingBytearray] = []

    def factory(*args: object) -> ScanRecordingBytearray:
        buffer = ScanRecordingBytearray(*args)
        created.append(buffer)
        return buffer

    monkeypatch.setattr(
        "app.core.requests.remote_api.bytearray", factory, raising=False
    )
    return created


CHUNK_SEQUENCES = [
    pytest.param([], id="no-chunks"),
    pytest.param([b""], id="empty-chunk"),
    pytest.param([b"", b"", b""], id="only-empty-chunks"),
    pytest.param([b"line\n"], id="single-terminated"),
    pytest.param([b"no-newline"], id="single-unterminated"),
    pytest.param([b"a", b"b", b"c"], id="newline-free-run"),
    pytest.param([b"x" * 16] * 8 + [b"end\n"], id="long-run-then-completion"),
    pytest.param([b"one-", b"line-", b"split\nnext\n"], id="straddles-three-chunks"),
    pytest.param([b"tail", b"\nlead"], id="newline-is-first-arriving-byte"),
    pytest.param([b"a\nb\nc\n"], id="multiple-terminators-one-chunk"),
    pytest.param([b"\n\n\n"], id="only-terminators"),
    pytest.param([b"x\n", b"\n"], id="empty-line-in-its-own-chunk"),
    pytest.param([b"a\r", b"\nb"], id="carriage-return-is-not-a-terminator"),
    pytest.param([b"tail", b"", b"\n"], id="empty-chunk-mid-run"),
    pytest.param(
        ["café=x\n".encode()[:5], "café=x\n".encode()[5:]], id="multibyte-split"
    ),
]

CAP_BOUNDARIES = [
    pytest.param([b"x" * 8], 8, id="remainder-exactly-at-cap"),
    pytest.param([b"x" * 9], 8, id="remainder-over-cap"),
    pytest.param([b"x" * 7 + b"\n"], 8, id="line-exactly-at-cap"),
    pytest.param([b"x" * 8 + b"\n"], 8, id="line-over-cap"),
    pytest.param([b"x" * 4, b"x" * 4], 8, id="remainder-reaches-cap-across-chunks"),
    pytest.param([b"x" * 5, b"x" * 5], 8, id="remainder-passes-cap-across-chunks"),
    pytest.param([b"x" * 4, b"x" * 4 + b"\n"], 8, id="line-over-cap-across-chunks"),
]


class TestIterLinesFromChunks:
    """Test the narrowed newline search in ``_iter_lines_from_chunks``."""

    @staticmethod
    async def _collect(
        chunks: list[bytes], cap: int
    ) -> tuple[list[bytes], ValueError | None]:
        """Run ``chunks`` through ``_iter_lines_from_chunks`` under ``cap``.

        :param chunks: The chunk payloads, in arrival order.
        :param cap: The per-line byte cap to patch in for the run.
        :return: The lines yielded, and the ``ValueError`` that stopped the run
            (``None`` when none did).
        """
        lines: list[bytes] = []
        with patch("app.core.requests.remote_api._MAX_STREAM_LINE_BYTES", cap):
            try:
                async for line in _iter_lines_from_chunks(_achunks(chunks), "/p/"):
                    # A comprehension would discard the lines yielded before the
                    # cap raised, which is half of what these tests compare.
                    lines.append(line)  # noqa: PERF401
            except ValueError as exc:
                return lines, exc
        return lines, None

    async def _assert_matches_the_oracle(
        self,
        chunks: list[bytes],
        cap: int,
        buffers: list[ScanRecordingBytearray],
    ) -> None:
        """Assert a narrowed run is indistinguishable from an unnarrowed one.

        The remainder is compared through the recorded buffer because the
        generator owns it: on a cap violation it never reaches the end-of-stream
        flush, so the bytes left behind are otherwise unobservable.

        :param chunks: The chunk payloads, in arrival order.
        :param cap: The per-line byte cap to enforce on both runs.
        :param buffers: The buffers the narrowed run built.
        """
        expected_lines, expected_size, expected_buffer = _replay_with_full_scans(
            chunks, cap
        )
        lines, exc = await self._collect(chunks, cap)

        assert lines == expected_lines
        assert bytes(buffers[0]) == expected_buffer
        if expected_size is None:
            assert exc is None
        else:
            assert f"size={expected_size}, path=/p/" in str(exc)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunks", CHUNK_SEQUENCES)
    @pytest.mark.parametrize("cap", [8, 64, _REAL_CAP], ids=["tiny", "small", "real"])
    async def test_matches_the_unnarrowed_loop(
        self,
        chunks: list[bytes],
        cap: int,
        recorded_buffers: list[ScanRecordingBytearray],
    ) -> None:
        """Assert the narrowed search yields what a search from zero yields."""
        await self._assert_matches_the_oracle(chunks, cap, recorded_buffers)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("chunks", "cap"), CAP_BOUNDARIES)
    async def test_cap_boundary_matches_the_unnarrowed_loop(
        self,
        chunks: list[bytes],
        cap: int,
        recorded_buffers: list[ScanRecordingBytearray],
    ) -> None:
        """Assert the cap fires on the same inputs, naming the same size."""
        await self._assert_matches_the_oracle(chunks, cap, recorded_buffers)

    @pytest.mark.asyncio
    async def test_straddling_line_keeps_the_carried_remainder(self) -> None:
        """Assert the first line a chunk completes still carries earlier bytes.

        Collapsing the search cursor into the line-start cursor drops the
        remainder from this line and hands a truncated line to the consumer.
        """
        lines, exc = await self._collect([b"head-", b"tail\n"], 64)

        assert lines == [b"head-tail\n"]
        assert exc is None

    @pytest.mark.asyncio
    async def test_cap_measures_the_whole_line_not_the_arriving_chunk(self) -> None:
        """Assert an oversized line built from several chunks still raises.

        Measuring the line from the search cursor would report only the arriving
        chunk's share, letting an over-cap line through.
        """
        lines, exc = await self._collect([b"x" * 700, b"y" * 700 + b"\n"], 1024)

        assert lines == []
        assert "size=1401, path=/p/" in str(exc)

    @pytest.mark.asyncio
    async def test_scan_starts_at_the_pre_append_length(
        self, recorded_buffers: list[ScanRecordingBytearray]
    ) -> None:
        """Assert each chunk's search begins where the previous one stopped."""
        lines, _ = await self._collect([b"x" * 4] * 4, 64)

        assert lines == [b"x" * 16]
        assert [start for start, _ in recorded_buffers[0].scans] == [0, 4, 8, 12]

    @pytest.mark.asyncio
    async def test_total_scan_work_is_linear_in_the_delivered_bytes(
        self, recorded_buffers: list[ScanRecordingBytearray]
    ) -> None:
        """Assert a newline-free run never re-examines the carried remainder."""
        chunks = [b"x" * 32] * 16 + [b"end\n"]
        await self._collect(chunks, _REAL_CAP)

        scanned = sum(end - start for start, end in recorded_buffers[0].scans)
        assert scanned == sum(map(len, chunks))

    @pytest.mark.asyncio
    async def test_releasing_chunk_still_scans_only_its_own_bytes(
        self, recorded_buffers: list[ScanRecordingBytearray]
    ) -> None:
        """Assert the chunk that yields the buffer narrows its search too.

        Work proportional to the buffer is legitimate on the chunk that hands
        those bytes to the consumer; the search for the terminator is not.
        """
        lines, _ = await self._collect([b"x" * 64, b"end\n"], _REAL_CAP)

        assert lines == [b"x" * 64 + b"end\n"]
        assert recorded_buffers[0].scans[-2:] == [(64, 68), (68, 68)]


class TestJSONShapeNarrowing:
    """Cover the helpers that narrow a verb method's JSON return union."""

    def test_object_passes_a_mapping_through(self) -> None:
        """Assert a JSON object is returned as a plain dict."""
        assert as_json_object({"a": 1}) == {"a": 1}

    def test_object_accepts_an_empty_mapping(self) -> None:
        """Assert an empty object is a valid payload, not a fault."""
        assert as_json_object({}) == {}

    def test_object_rejects_an_array(self) -> None:
        """Assert a JSON array is reported as an upstream fault."""
        with pytest.raises(HTTPBadGatewayException) as exc_info:
            as_json_object([{"a": 1}])

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    def test_object_rejects_no_content(self) -> None:
        """Assert HTTP 204's ``None`` is reported rather than returned."""
        with pytest.raises(HTTPBadGatewayException) as exc_info:
            as_json_object(None)

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    def test_array_passes_a_list_of_objects_through(self) -> None:
        """Assert a JSON array of objects is returned unchanged."""
        assert as_json_array([{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]

    def test_array_accepts_an_empty_list(self) -> None:
        """Assert an empty array is a valid payload, not a fault."""
        assert as_json_array([]) == []

    def test_array_rejects_non_object_elements(self) -> None:
        """Assert the declared ``list[dict]`` is checked, not merely asserted."""
        with pytest.raises(HTTPBadGatewayException) as exc_info:
            as_json_array([1, 2])

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    def test_array_rejects_an_object(self) -> None:
        """Assert a JSON object is reported as an upstream fault."""
        with pytest.raises(HTTPBadGatewayException):
            as_json_array({"a": 1})

    def test_array_rejects_no_content(self) -> None:
        """Assert HTTP 204's ``None`` is reported rather than returned."""
        with pytest.raises(HTTPBadGatewayException):
            as_json_array(None)


@asynccontextmanager
async def _recording_server() -> AsyncGenerator[tuple[str, list[dict[str, str]]]]:
    """Serve a catch-all JSON route locally and record each request's headers.

    ``aioresponses`` patches ``ClientSession._request``, which is where aiohttp
    reconciles URL-embedded credentials against an explicit ``Authorization``
    header, so only a real socket exercises that reconciliation.

    :yield: The ``host:port`` the server listens on, and the list its handler
        appends one header mapping to per received request.
    """
    received: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        received.append(dict(request.headers))
        return web.json_response({"ok": True})

    server = web.Application()
    server.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(server)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        _, port = runner.addresses[0][:2]
        yield f"127.0.0.1:{port}", received
    finally:
        await runner.cleanup()


class _KeyedRemoteAPI(RemoteAPI):
    """Stand in for a client that carries a fixed credential in every request.

    ``PMMRemoteAPI`` is the production shape: its ``headers`` property adds an
    ``Authorization`` header unconditionally, so the header reaches the session
    defaults rather than a per-call kwarg.
    """

    @property
    def headers(self) -> dict[str, str]:
        """Return the base headers plus a fixed API-key authorization."""
        return {**super().headers, "Authorization": "Bearer configured-api-key"}


class TestEndpointCredentialAndExplicitAuthHeader:
    """Cover a credential-bearing endpoint alongside an explicit auth header."""

    @pytest.mark.asyncio
    async def test_a_forwarded_token_wins_over_the_endpoint_credential(self) -> None:
        """Send the caller's token when the endpoint also embeds a credential."""
        async with _recording_server() as (netloc, received):
            api = RemoteAPI(endpoint=f"http://svcuser:svcpass@{netloc}/api/inventory")
            async with api:
                with api.auth("forwarded-user-token"):
                    await api.get("/summary/")

        assert received[0]["Authorization"] == "Bearer forwarded-user-token"

    @pytest.mark.asyncio
    async def test_a_client_api_key_wins_over_the_endpoint_credential(self) -> None:
        """Send a subclass's own header when the endpoint also embeds a credential."""
        async with _recording_server() as (netloc, received):
            api = _KeyedRemoteAPI(endpoint=f"http://svcuser:svcpass@{netloc}/graph")
            async with api:
                await api.get("/api/folders/")

        assert received[0]["Authorization"] == "Bearer configured-api-key"

    @pytest.mark.asyncio
    async def test_the_endpoint_credential_is_sent_when_no_header_competes(
        self,
    ) -> None:
        """Keep basic auth from the endpoint for a client that sets no header."""
        async with _recording_server() as (netloc, received):
            api = RemoteAPI(endpoint=f"http://svcuser:svcpass@{netloc}/api/inventory")
            async with api:
                await api.get("/summary/")

        assert received[0]["Authorization"] == encode_basic_auth("svcuser", "svcpass")

    @pytest.mark.asyncio
    async def test_no_authorization_is_sent_for_a_credential_free_endpoint(
        self,
    ) -> None:
        """Leave the header off entirely when neither source supplies one."""
        async with _recording_server() as (netloc, received):
            api = RemoteAPI(endpoint=f"http://{netloc}/api/inventory")
            async with api:
                await api.get("/summary/")

        assert "Authorization" not in received[0]

    @pytest.mark.asyncio
    async def test_a_percent_encoded_endpoint_credential_is_decoded(self) -> None:
        """Send the decoded credential, as parsing the URL itself would have."""
        async with _recording_server() as (netloc, received):
            api = RemoteAPI(endpoint=f"http://svc%2Fuser:p%40ss@{netloc}/api/inventory")
            async with api:
                await api.get("/summary/")

        assert received[0]["Authorization"] == encode_basic_auth("svc/user", "p@ss")


class TestBaseUrlRedaction:
    """Cover the credential redaction on the derived base URL."""

    @pytest.fixture
    def api(self) -> RemoteAPI:
        """Return a client whose endpoint embeds a password."""
        return RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)

    def test_json_dump_masks_the_password(self, api: RemoteAPI) -> None:
        """Mask the password in a JSON-mode dump, as the endpoint field does."""
        assert api.model_dump(mode="json")["base_url"] == _REDACTED_BASE_URL

    def test_model_dump_json_masks_the_password(self, api: RemoteAPI) -> None:
        """Mask the password in the serialized JSON string too."""
        assert _CREDENTIAL_SECRET not in api.model_dump_json()
        assert CREDENTIAL_URL_MASK in api.model_dump_json()

    def test_python_dump_keeps_the_password(self, api: RemoteAPI) -> None:
        """Keep the real credential in a python-mode dump."""
        assert api.model_dump()["base_url"] == _LIVE_BASE_URL

    def test_preserve_context_keeps_the_password(self, api: RemoteAPI) -> None:
        """Keep the real credential for a caller that opts out of redaction."""
        dumped = api.model_dump(mode="json", context=PRESERVE_CREDENTIALS_CONTEXT)
        assert dumped["base_url"] == _LIVE_BASE_URL

    def test_the_attribute_keeps_the_password(self, api: RemoteAPI) -> None:
        """Leave the live attribute untouched; only serialization redacts."""
        assert api.base_url == _LIVE_BASE_URL

    def test_the_session_url_drops_the_userinfo(self, api: RemoteAPI) -> None:
        """Build the session from a URL carrying no userinfo at all."""
        assert api.session_base_url == "http://remote.internal:9000"

    def test_a_username_only_endpoint_is_unchanged(self) -> None:
        """Leave a URL with a username but no password as it is."""
        api = RemoteAPI(endpoint="http://svcuser@remote.internal:9000/api")
        assert (
            api.model_dump(mode="json")["base_url"]
            == "http://svcuser@remote.internal:9000"
        )

    def test_a_credential_free_endpoint_is_unchanged(self) -> None:
        """Dump a credential-free endpoint exactly as the live value reads."""
        api = RemoteAPI(endpoint="http://remote.internal:9000/api")
        assert api.model_dump(mode="json")["base_url"] == api.base_url

    def test_a_percent_encoded_password_is_masked(self) -> None:
        """Mask a percent-encoded password and still send the decoded credential."""
        api = RemoteAPI(endpoint="http://svc%2Fuser:p%40ss@remote.internal:9000/api")
        assert (
            api.model_dump(mode="json")["base_url"]
            == "http://svc%2Fuser:****@remote.internal:9000"
        )
        assert api._endpoint_credential_header == encode_basic_auth("svc/user", "p@ss")

    def test_an_ipv6_host_keeps_its_brackets_and_port(self) -> None:
        """Preserve a bracketed IPv6 host and its port while masking."""
        api = RemoteAPI(endpoint="http://svcuser:svcpass@[::1]:4646/api")
        assert (
            api.model_dump(mode="json")["base_url"] == "http://svcuser:****@[::1]:4646"
        )

    def test_the_base_path_is_dropped_from_the_base_url(self, api: RemoteAPI) -> None:
        """Strip the endpoint's own path, which the base URL exists to remove."""
        assert api.base_path == "/api/inventory"
        assert "/api/inventory" not in api.base_url

    def test_a_query_string_survives_the_base_path_removal(self) -> None:
        """Remove the base path from the path alone, not from the whole URL.

        A query value repeating the base path is the reachable case: dropping
        every occurrence corrupts the URL, and a corrupted URL is one the
        redaction helper can refuse to parse.
        """
        api = RemoteAPI(endpoint="http://remote.internal:9000/api?next=/api")
        assert api.base_url == "http://remote.internal:9000?next=/api"

    def test_a_path_params_segment_survives_the_base_path_removal(self) -> None:
        """Remove a base path whose last segment carries ``;params``.

        Pydantic keeps ``;v=2`` inside the path, so a parser that split it out
        would miss the suffix and return the whole endpoint.
        """
        api = RemoteAPI(endpoint="http://h.io:9000/api;v=2")
        assert api.base_path == "/api;v=2"
        assert api.base_url == "http://h.io:9000"

    @pytest.mark.parametrize(
        "client_class",
        [
            RemoteAPI,
            CasdoorSDK,
            CasdoorAuthProvider,
            GrafanaSDK,
            GrafanaAuthProvider,
            PMMRemoteAPI,
            NomadExecutor,
        ],
    )
    def test_every_client_class_masks_its_base_url(
        self, client_class: type[BaseRemoteAPI]
    ) -> None:
        """Mask the password for every production client.

        ``model_construct`` skips validation, so a client with required
        credentials of its own still takes part without the test knowing what
        they are.
        """
        client = client_class.model_construct(endpoint=HttpUrl(_CREDENTIAL_ENDPOINT))
        assert _CREDENTIAL_SECRET not in client.model_dump_json()


class _HookOverrideRemoteAPI(RemoteAPI):
    """Stand in for a client that derives its base URL differently."""

    def _compute_base_url(self) -> str:
        """Return the inherited base URL with the userinfo removed."""
        return strip_credential_url_userinfo(super()._compute_base_url())


class _UnparseableRemoteAPI(RemoteAPI):
    """Stand in for a client whose derived base URL cannot be parsed."""

    def _compute_base_url(self) -> str:
        """Return a URL whose bracketed host is unterminated."""
        return "http://[::1:4646/"


class _RaisingHookRemoteAPI(RemoteAPI):
    """Stand in for a client whose base-URL hook itself fails to parse."""

    def _compute_base_url(self) -> str:
        """Raise the way a hook parsing a malformed URL does."""
        return strip_credential_url_userinfo("http://[::1:4646/")


class TestBaseUrlSubclassing:
    """Cover how a subclass customises the base URL without dropping redaction."""

    def test_redeclaring_the_computed_field_is_rejected(self) -> None:
        """Refuse a subclass that redeclares ``base_url`` at class creation.

        A redeclared computed field shadows the return annotation the serializer
        rides on, so the password would return to every dump of that class while
        every other class stays clean.
        """
        with pytest.raises(TypeError, match="_compute_base_url"):

            class _RedeclaringRemoteAPI(RemoteAPI):
                @computed_field
                @property
                def base_url(self) -> str:
                    return str(self.endpoint)

    def test_a_plain_attribute_named_base_url_is_rejected(self) -> None:
        """Refuse any class-level ``base_url``, not only a computed field."""
        with pytest.raises(TypeError, match="_compute_base_url"):

            class _ShadowingRemoteAPI(RemoteAPI):
                @property
                def base_url(self) -> str:
                    return str(self.endpoint)

    def test_a_hook_override_is_still_masked(self) -> None:
        """Redact a subclass's derived value through the inherited computed field."""
        client = _HookOverrideRemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        assert client.base_url == "http://remote.internal:9000"
        assert _CREDENTIAL_SECRET not in client.model_dump_json()

    def test_the_serialization_schema_still_declares_a_string(self) -> None:
        """Keep the derived field typed in the serialization schema.

        A wrap serializer with no declared return type erases it, which would
        publish an untyped property to every consumer of the schema.
        """
        schema = RemoteAPI.model_json_schema(mode="serialization")
        assert schema["properties"]["BASE_URL"]["type"] == "string"


class TestRedactedBaseUrl:
    """Cover the logging-safe view of the derived base URL."""

    def test_masks_the_password(self) -> None:
        """Mask an embedded password for a log line."""
        api = RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        assert api.redacted_base_url == _REDACTED_BASE_URL

    def test_leaves_a_credential_free_url_unchanged(self) -> None:
        """Return a URL with nothing to mask as it is."""
        api = RemoteAPI(endpoint="http://remote.internal:9000/api")
        assert api.redacted_base_url == "http://remote.internal:9000"

    def test_falls_back_to_the_mask_when_the_url_cannot_be_parsed(self) -> None:
        """Return the bare mask rather than raise over the failure being reported."""
        api = _UnparseableRemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        assert api.redacted_base_url == CREDENTIAL_URL_MASK

    def test_falls_back_to_the_mask_when_the_hook_raises(self) -> None:
        """Return the bare mask when the base-URL hook, not the redaction, fails."""
        api = _RaisingHookRemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        assert api.redacted_base_url == CREDENTIAL_URL_MASK


class TestSessionLifecycleLogging:
    """Cover the credential redaction on the session-lifecycle debug logs."""

    @pytest.mark.asyncio
    async def test_the_opening_log_masks_the_password(self, caplog) -> None:
        """Mask the password on the line announcing a new session."""
        api = RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        with caplog.at_level("DEBUG", logger=api.logger.name):
            async with api:
                pass

        assert _CREDENTIAL_SECRET not in caplog.text
        assert f"Opening ClientSession for {_REDACTED_BASE_URL}" in caplog.text

    @pytest.mark.asyncio
    async def test_the_closing_log_masks_the_password(self, caplog) -> None:
        """Mask the password on the line announcing a session close."""
        api = RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        with caplog.at_level("DEBUG", logger=api.logger.name):
            async with api:
                pass

        assert _CREDENTIAL_SECRET not in caplog.text
        assert f"Closing ClientSession for {_REDACTED_BASE_URL}" in caplog.text

    @pytest.mark.asyncio
    async def test_the_already_closed_log_masks_the_password(self, caplog) -> None:
        """Mask the password on the line taken when there is nothing to close."""
        api = RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        async with api:
            pass
        with caplog.at_level("DEBUG", logger=api.logger.name):
            await api.__aexit__(None, None, None)

        assert _CREDENTIAL_SECRET not in caplog.text
        assert f"ClientSession already closed for {_REDACTED_BASE_URL}" in caplog.text

    @pytest.mark.asyncio
    async def test_the_session_is_built_without_the_userinfo(self) -> None:
        """Keep the credential out of the session URL, masked or not."""
        api = RemoteAPI(endpoint=_CREDENTIAL_ENDPOINT)
        async with api:
            session_url = str(api._session._base_url)

        assert _CREDENTIAL_SECRET not in session_url
        assert CREDENTIAL_URL_MASK not in session_url
