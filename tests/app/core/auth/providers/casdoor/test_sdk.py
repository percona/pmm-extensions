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

import pytest
from pydantic import SecretStr

from app.core.auth.providers.casdoor.sdk import CasdoorSDK
from app.core.exceptions import HTTPBadGatewayException


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
