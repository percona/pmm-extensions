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

"""Define tests for the Casdoor SDK."""

import base64
import logging
import re

import pytest
from aioresponses import aioresponses
from fastapi import HTTPException, status
from pydantic import SecretStr, ValidationError

from app.core.auth.providers.casdoor.sdk import CasdoorSDK
from app.core.exceptions import HTTPBadGatewayException
from tests.app.remote_api_log import WITHHELD_BODY_LOG

_ENDPOINT = "https://casdoor.example.com"
_SENTINEL = "casdoor-body-sentinel"


def _sdk() -> CasdoorSDK:
    """Build a ``CasdoorSDK`` with minimal config."""
    return CasdoorSDK(
        endpoint=_ENDPOINT, client_id="test-id", client_secret="test-secret"
    )


def test_casdoor_credentials_masked_in_repr():
    """Test that client_id and client_secret are masked in repr."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="my-client-id",
        client_secret="my-client-secret",
    )
    repr_str = repr(sdk)
    assert "my-client-id" not in repr_str
    assert "my-client-secret" not in repr_str


def test_casdoor_headers_encode_secret_values():
    """Assert Authorization encodes the secret credentials."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    expected = base64.b64encode(b"test-id:test-secret").decode("utf-8")
    assert sdk.headers["Authorization"] == f"Basic {expected}"


def test_casdoor_headers_with_empty_credentials():
    """Assert empty credentials encode without raising (no validation guard)."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="",
        client_secret="",
    )
    expected = base64.b64encode(b":").decode("utf-8")
    assert sdk.headers["Authorization"] == f"Basic {expected}"


def test_casdoor_headers_recompute_after_credentials_change():
    """Assert Authorization reflects mutated credentials (it is not cached)."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    original = sdk.headers["Authorization"]

    sdk.client_id = SecretStr("new-id")
    sdk.client_secret = SecretStr("new-secret")

    expected = base64.b64encode(b"new-id:new-secret").decode("utf-8")
    assert sdk.headers["Authorization"] == f"Basic {expected}"
    assert sdk.headers["Authorization"] != original


def test_casdoor_headers_carry_basic_authorization():
    """Assert the complete headers including Basic Authorization."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    expected = base64.b64encode(b"test-id:test-secret").decode("utf-8")
    assert sdk.headers == {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Basic {expected}",
    }


def test_casdoor_declares_no_stored_credential_settings():
    """Assert ``api_key`` and ``auth_scheme`` are absent from Casdoor provider settings."""
    assert "api_key" not in CasdoorSDK.model_fields
    assert "auth_scheme" not in CasdoorSDK.model_fields


@pytest.mark.asyncio
async def test_get_users_returns_the_listed_users(mocker):
    """Return the ``data`` array of a successful user listing."""
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    listed = [{"id": "u1", "name": "alice"}]
    mocker.patch.object(
        CasdoorSDK,
        "get",
        new=mocker.AsyncMock(return_value={"status": "ok", "data": listed}),
    )

    assert await sdk.get_users() == listed


@pytest.mark.asyncio
async def test_get_users_raises_on_an_error_body_without_caching_it(mocker):
    """Raise on Casdoor's HTTP-200 error body and retry the listing on the next call.

    Casdoor answers a denied or failed ``/api/get-users`` with HTTP 200 and
    ``"data": null``. Returning that ``None`` would reach callers as a user list
    and be cached for the listing's whole TTL.
    """
    sdk = CasdoorSDK(
        endpoint="https://casdoor.example.com",
        client_id="test-id",
        client_secret="test-secret",
    )
    error_body = {"status": "error", "msg": "Unauthorized operation", "data": None}
    get_mock = mocker.patch.object(
        CasdoorSDK, "get", new=mocker.AsyncMock(return_value=error_body)
    )
    attempts = 2

    for _ in range(attempts):
        with pytest.raises(HTTPBadGatewayException):
            await sdk.get_users()

    assert get_mock.await_count == attempts


def test_casdoor_rejects_disabling_body_withholding():
    """Refuse a client that would log Casdoor's token and user bodies."""
    with pytest.raises(ValidationError):
        CasdoorSDK(
            endpoint=_ENDPOINT,
            client_id="test-id",
            client_secret="test-secret",
            withhold_body_log=False,
        )


class TestCasdoorBodyLog:
    """Cover Casdoor's bodies staying out of the transport's log records."""

    @pytest.mark.asyncio
    async def test_token_exchange_body_is_withheld(self, caplog):
        """Log the exchange's method and path but neither body."""
        sdk = _sdk()
        with aioresponses() as mock:
            mock.post(
                f"{_ENDPOINT}/api/login/oauth/access_token",
                payload={"access_token": _SENTINEL, "token_type": "Bearer"},
            )
            with caplog.at_level(logging.DEBUG, logger=sdk.logger.name):
                async with sdk:
                    token = await sdk.get_access_token(code="auth-code-value")

        assert token["access_token"] == _SENTINEL
        assert _SENTINEL not in caplog.text
        assert "test-secret" not in caplog.text
        assert "auth-code-value" not in caplog.text
        assert "/api/login/oauth/access_token" in caplog.text
        assert WITHHELD_BODY_LOG in caplog.text

    @pytest.mark.asyncio
    async def test_non_json_error_body_is_withheld(self, caplog):
        """Keep an upstream error page off the non-JSON ERROR record."""
        sdk = _sdk()
        with aioresponses() as mock:
            mock.get(
                re.compile(rf"{re.escape(_ENDPOINT)}/api/get-users.*"),
                status=status.HTTP_502_BAD_GATEWAY,
                body=f"<html>{_SENTINEL}</html>",
                content_type="text/html",
            )
            with caplog.at_level(logging.DEBUG, logger=sdk.logger.name):
                async with sdk:
                    with pytest.raises(HTTPException):
                        await sdk.get_users()

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert _SENTINEL not in caplog.text
        assert len(errors) == 1
        assert WITHHELD_BODY_LOG in errors[0].getMessage()
