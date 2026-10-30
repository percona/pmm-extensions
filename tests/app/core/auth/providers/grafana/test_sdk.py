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

"""Define tests for the Grafana SDK."""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import (
    ClientConnectionError,
    ClientPayloadError,
    ClientResponseError,
    ClientTimeout,
    ContentTypeError,
    web,
)
from fastapi import HTTPException, status
from pydantic import ValidationError

from app.core.auth.exceptions import HTTPUnauthorizedException
from app.core.auth.providers.grafana.sdk import GrafanaException, GrafanaSDK
from app.core.requests import RemoteAPI
from tests.app.conftest import GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN

_ENDPOINT = "https://grafana.example.com"
_SERVICE_TOKEN = "test-service-account-token"
_SESSION_VALUE = "grafana-session-cookie-value"


def _sdk() -> GrafanaSDK:
    """Build a ``GrafanaSDK`` with minimal config."""
    return GrafanaSDK(endpoint=_ENDPOINT, service_account_token=_SERVICE_TOKEN)


def _mock_response(*, status=200, json_data=None, cookies=None):
    """Build a mock aiohttp response usable as an async context manager."""
    response = AsyncMock()
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    response.raise_for_status = MagicMock()
    response.cookies = cookies if cookies is not None else SimpleCookie()
    return response


def _attach_session(sdk, response):
    """Attach a mock ``ClientSession`` returning ``response`` to ``sdk``."""
    session = MagicMock()
    session.request = MagicMock(return_value=response)
    sdk._session = session
    return session


def test_service_account_token_masked_in_repr():
    """Verify the service-account token is masked in repr."""
    assert _SERVICE_TOKEN not in repr(_sdk())


def test_headers_carry_no_default_authorization():
    """Verify the SDK sets no default ``Authorization`` header."""
    assert "Authorization" not in _sdk().headers


@pytest.mark.asyncio
async def test_login_returns_session_cookie():
    """Verify login posts credentials and returns the session cookie value."""
    sdk = _sdk()
    cookies = SimpleCookie()
    cookies["grafana_session"] = _SESSION_VALUE
    session = _attach_session(sdk, _mock_response(status=200, cookies=cookies))

    result = await sdk.login("alice", "secret")

    assert result == _SESSION_VALUE
    args, kwargs = session.request.call_args
    assert args[:2] == ("POST", "/login")
    assert kwargs["json"] == {"user": "alice", "password": "secret"}
    assert "Authorization" not in kwargs.get("headers", {})


@pytest.mark.asyncio
async def test_login_raises_unauthorized_on_bad_credentials():
    """Verify a 401 login response raises ``HTTPUnauthorizedException``."""
    sdk = _sdk()
    _attach_session(sdk, _mock_response(status=401))

    with pytest.raises(HTTPUnauthorizedException):
        await sdk.login("alice", "wrong")


@pytest.mark.asyncio
async def test_login_raises_grafana_error_on_upstream_failure():
    """Verify a non-401 error status surfaces as ``GrafanaException``, not a 401.

    A Grafana outage (5xx) or throttling (429) must not be reported to the user
    as bad credentials.
    """
    sdk = _sdk()
    _attach_session(sdk, _mock_response(status=500))

    with pytest.raises(GrafanaException, match="login failed"):
        await sdk.login("alice", "secret")


@pytest.mark.asyncio
async def test_login_does_not_log_password(caplog):
    """Verify the login password never reaches the debug request log."""
    sdk = _sdk()
    cookies = SimpleCookie()
    cookies["grafana_session"] = _SESSION_VALUE
    _attach_session(sdk, _mock_response(status=200, cookies=cookies))

    with caplog.at_level(logging.DEBUG, logger=sdk.logger_name):
        await sdk.login("alice", "super-secret-pw")

    assert "super-secret-pw" not in caplog.text
    assert "****" in caplog.text


@pytest.mark.asyncio
async def test_login_raises_when_no_session_established():
    """Verify a 200 login with no session cookie raises ``GrafanaException``."""
    sdk = _sdk()
    _attach_session(sdk, _mock_response(status=200, cookies=SimpleCookie()))

    with pytest.raises(GrafanaException, match="did not establish a session"):
        await sdk.login("alice", "secret")


@pytest.mark.asyncio
async def test_login_translates_connection_error():
    """Verify a connection error during login surfaces as ``GrafanaException``."""
    sdk = _sdk()
    session = MagicMock()
    session.request = MagicMock(side_effect=ClientConnectionError("down"))
    sdk._session = session

    with pytest.raises(GrafanaException, match="Cannot connect to Grafana"):
        await sdk.login("alice", "secret")


@pytest.mark.asyncio
async def test_get_current_user_sends_cookie_without_authorization():
    """Verify get_current_user authenticates via the session cookie only."""
    sdk = _sdk()
    record = {"id": 1, "login": "alice"}
    session = _attach_session(sdk, _mock_response(status=200, json_data=record))

    result = await sdk.get_current_user("sess-123")

    assert result == record
    headers = session.request.call_args.kwargs["headers"]
    assert headers["Cookie"] == "grafana_session=sess-123"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_get_current_user_orgs_sends_cookie_without_authorization():
    """Verify get_current_user_orgs authenticates via the session cookie only."""
    sdk = _sdk()
    orgs = [{"orgId": 1, "role": "Admin"}]
    session = _attach_session(sdk, _mock_response(status=200, json_data=orgs))

    result = await sdk.get_current_user_orgs("sess-123")

    assert result == orgs
    headers = session.request.call_args.kwargs["headers"]
    assert headers["Cookie"] == "grafana_session=sess-123"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_get_org_users_sends_service_account_bearer():
    """Verify get_org_users authenticates with the service-account bearer token."""
    GrafanaSDK.get_org_users.cache_clear()
    sdk = _sdk()
    session = _attach_session(sdk, _mock_response(status=200, json_data=[]))

    await sdk.get_org_users()

    headers = session.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert "Cookie" not in headers
    GrafanaSDK.get_org_users.cache_clear()


@pytest.mark.asyncio
async def test_get_org_users_is_cached(mocker):
    """Verify get_org_users caches its result (``@alru_cache``)."""
    GrafanaSDK.get_org_users.cache_clear()
    sdk = _sdk()
    get_mock = mocker.patch.object(
        GrafanaSDK, "get", new=mocker.AsyncMock(return_value=[])
    )

    await sdk.get_org_users()
    await sdk.get_org_users()

    assert get_mock.await_count == 1
    GrafanaSDK.get_org_users.cache_clear()


@pytest.mark.asyncio
async def test_request_translates_connection_error():
    """Verify a connection error on a request surfaces as ``GrafanaException``."""
    sdk = _sdk()
    session = MagicMock()
    session.request = MagicMock(side_effect=ClientConnectionError("down"))
    sdk._session = session

    with pytest.raises(GrafanaException, match="Cannot connect to Grafana"):
        await sdk.get("/api/user")


_SA_TOKEN = GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN
_SA_ID = 7
_SA_LOGIN = "sa-1-ci-runner"
#: Grafana calls one uncached verification makes: the token, then the record.
_VERIFICATION_CALLS = 2
#: Listing pages read when the second comes back empty: the populated page, then
#: the empty one that ends the listing.
_EMPTY_PAGE_CALLS = 2


def _json_error(status_code, json_data=None):
    """Build a mock response carrying a JSON error body at ``status_code``."""
    response = _mock_response(
        status=status_code, json_data=json_data or {"message": "Invalid API key"}
    )
    response.raise_for_status = MagicMock(
        side_effect=ClientResponseError(None, (), status=status_code)
    )
    return response


def _html_error(status_code):
    """Build a mock response whose non-JSON body a proxy answered."""
    response = _mock_response(status=status_code)
    response.json = AsyncMock(
        side_effect=ContentTypeError(None, (), status=status_code)
    )
    response.text = AsyncMock(return_value="<html>blocked</html>")
    return response


def _current_sa(*, uid=f"service-account:{_SA_ID}", login=_SA_LOGIN):
    """Build Grafana's ``/api/user`` answer for a service-account token."""
    return _mock_response(
        json_data={"id": 0, "uid": uid, "login": login, "isDisabled": False}
    )


def _sa_record(**overrides):
    """Build Grafana's ``/api/serviceaccounts/{id}`` record."""
    record = {
        "id": _SA_ID,
        "login": _SA_LOGIN,
        "name": "ci-runner",
        "orgId": 1,
        "isDisabled": False,
        "role": "Editor",
    }
    return {**record, **overrides}


def _validated_sa_record(**overrides):
    """Return the part of :func:`_sa_record` the strict record adapter keeps."""
    record = _sa_record(**overrides)
    return {key: record[key] for key in ("id", "login", "role", "isDisabled")}


def _attach_sequence(sdk, *outcomes):
    """Attach a session answering each request with the next outcome in turn."""
    session = MagicMock()
    session.request = MagicMock(side_effect=list(outcomes))
    sdk._session = session
    return session


def _verified_pair():
    """Return the two responses of a successful verification."""
    return [_current_sa(), _mock_response(json_data=_sa_record())]


class TestVerifyServiceAccountToken:
    """Verify the service-account token check and its verdict cache."""

    @pytest.mark.asyncio
    async def test_returns_the_record_after_both_calls(self):
        """Verify the token is proven by Grafana, then read back through PMM Extensions' SA."""
        sdk = _sdk()
        session = _attach_sequence(sdk, *_verified_pair())

        record = await sdk.verify_service_account_token(_SA_TOKEN)

        assert record == _validated_sa_record()
        first, second = session.request.call_args_list
        assert first.args[:2] == ("GET", "/api/user")
        assert first.kwargs["headers"]["Authorization"] == f"Bearer {_SA_TOKEN}"
        assert second.args[:2] == ("GET", f"/api/serviceaccounts/{_SA_ID}")
        assert second.kwargs["headers"]["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
        for call in (first, second):
            assert isinstance(call.kwargs["timeout"], ClientTimeout)
            assert call.kwargs["timeout"].total is not None

    @pytest.mark.asyncio
    async def test_a_rejected_token_is_a_refusal(self):
        """Verify Grafana's own 401 for the presented token reads as a refusal."""
        sdk = _sdk()
        session = _attach_sequence(sdk, _json_error(401))

        assert await sdk.verify_service_account_token(_SA_TOKEN) is None
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_an_account_outside_seps_org_is_a_refusal(self):
        """Verify a 404 on the account record (other org, deleted) is a refusal."""
        sdk = _sdk()
        _attach_sequence(
            sdk,
            _current_sa(),
            _json_error(404, {"messageId": "serviceaccounts.ErrNotFound"}),
        )

        assert await sdk.verify_service_account_token(_SA_TOKEN) is None

    @pytest.mark.parametrize(
        "outcomes",
        [
            pytest.param(lambda: [_json_error(500)], id="user-500"),
            pytest.param(lambda: [_json_error(503)], id="user-503"),
            pytest.param(lambda: [_json_error(403)], id="user-403"),
            pytest.param(lambda: [_current_sa(), _json_error(401)], id="record-401"),
            pytest.param(lambda: [_current_sa(), _json_error(403)], id="record-403"),
            pytest.param(lambda: [_current_sa(), _json_error(500)], id="record-500"),
            pytest.param(lambda: [TimeoutError()], id="timeout"),
            pytest.param(lambda: [ClientConnectionError("down")], id="connection"),
            pytest.param(lambda: [ClientPayloadError("cut")], id="payload"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_upstream_failure_is_a_bad_gateway(self, outcomes):
        """Verify every non-verdict outcome raises 502 rather than refusing."""
        sdk = _sdk()
        _attach_sequence(sdk, *outcomes())

        with pytest.raises(GrafanaException) as exc_info:
            await sdk.verify_service_account_token(_SA_TOKEN)

        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY

    @pytest.mark.asyncio
    async def test_an_undecodable_body_is_a_bad_gateway(self):
        """Verify a JSON-typed body that does not parse raises 502."""
        sdk = _sdk()
        response = _mock_response()
        response.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "{", 0))
        _attach_sequence(sdk, response)

        with pytest.raises(GrafanaException):
            await sdk.verify_service_account_token(_SA_TOKEN)

    @pytest.mark.parametrize(
        "outcomes",
        [
            pytest.param(lambda: [_html_error(401)], id="user-html-401"),
            pytest.param(
                lambda: [_current_sa(), _html_error(404)], id="record-html-404"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_non_json_refusal_status_is_not_a_verdict(self, outcomes):
        """Verify a proxy's HTML 401/404 raises 502 and is never cached.

        The retry with the same token must reach Grafana again and succeed.
        """
        sdk = _sdk()
        _attach_sequence(sdk, *outcomes(), *_verified_pair())

        with pytest.raises(GrafanaException):
            await sdk.verify_service_account_token(_SA_TOKEN)
        assert (
            await sdk.verify_service_account_token(_SA_TOKEN) == _validated_sa_record()
        )

    @pytest.mark.parametrize(
        ("current", "record"),
        [
            pytest.param({"uid": None}, {}, id="uid-missing"),
            pytest.param({"uid": "user:3"}, {}, id="uid-user"),
            pytest.param({"uid": "service-account:x"}, {}, id="uid-non-int"),
            pytest.param({"login": ""}, {}, id="login-empty"),
            pytest.param({}, {"login": "sa-1-other"}, id="login-mismatch"),
            pytest.param({}, {"id": 8}, id="id-mismatch"),
            pytest.param({}, {"isDisabled": "no"}, id="disabled-non-bool"),
            pytest.param({}, {"isDisabled": 0}, id="disabled-int"),
            pytest.param({}, {"role": 3}, id="role-non-str"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_off_contract_body_is_a_bad_gateway(self, current, record):
        """Verify a record Grafana never promised raises 502."""
        sdk = _sdk()
        _attach_sequence(
            sdk,
            _current_sa(**current),
            _mock_response(json_data=_sa_record(**record)),
        )

        with pytest.raises(GrafanaException):
            await sdk.verify_service_account_token(_SA_TOKEN)

    @pytest.mark.asyncio
    async def test_a_non_object_body_names_the_path_it_came_from(self):
        """Verify a JSON body that is not an object is reported against its path."""
        sdk = _sdk()
        _attach_sequence(sdk, _mock_response(json_data=[]))

        with pytest.raises(GrafanaException) as exc_info:
            await sdk.verify_service_account_token(_SA_TOKEN)

        assert exc_info.value.detail == (
            "Grafana returned an unreadable /api/user response."
        )

    @pytest.mark.asyncio
    async def test_a_verdict_is_reused_within_the_window(self):
        """Verify a second check with the same token makes no Grafana call."""
        sdk = _sdk()
        session = _attach_sequence(sdk, *_verified_pair())

        await sdk.verify_service_account_token(_SA_TOKEN)
        record = await sdk.verify_service_account_token(_SA_TOKEN)

        assert record == _validated_sa_record()
        assert session.request.call_count == _VERIFICATION_CALLS

    @pytest.mark.asyncio
    async def test_a_refusal_is_reused_within_the_window(self):
        """Verify a dead token costs one Grafana round-trip per window."""
        sdk = _sdk()
        session = _attach_sequence(sdk, _json_error(401))

        await sdk.verify_service_account_token(_SA_TOKEN)

        assert await sdk.verify_service_account_token(_SA_TOKEN) is None
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_a_failure_is_not_reused(self):
        """Verify a timeout is retried on the next check rather than remembered."""
        sdk = _sdk()
        _attach_sequence(sdk, TimeoutError(), *_verified_pair())

        with pytest.raises(GrafanaException):
            await sdk.verify_service_account_token(_SA_TOKEN)

        assert (
            await sdk.verify_service_account_token(_SA_TOKEN) == _validated_sa_record()
        )

    @pytest.mark.asyncio
    async def test_a_verdict_expires_after_the_window(self, mocker):
        """Verify a role change is seen once the window has elapsed."""
        sdk = GrafanaSDK(
            endpoint=_ENDPOINT,
            service_account_token=_SERVICE_TOKEN,
            service_account_bearer_revocation_window=timedelta(seconds=1),
        )
        _attach_sequence(
            sdk,
            *_verified_pair(),
            _current_sa(),
            _mock_response(json_data=_sa_record(role="Admin")),
        )
        clock = mocker.patch(
            "app.core.auth.providers.grafana.sdk.monotonic", return_value=100.0
        )
        await sdk.verify_service_account_token(_SA_TOKEN)
        clock.return_value = 101.5

        record = await sdk.verify_service_account_token(_SA_TOKEN)

        assert record is not None
        assert record["role"] == "Admin"

    @pytest.mark.asyncio
    async def test_a_zero_window_verifies_every_check(self):
        """Verify a ``0`` window reaches Grafana on every check."""
        sdk = GrafanaSDK(
            endpoint=_ENDPOINT,
            service_account_token=_SERVICE_TOKEN,
            service_account_bearer_revocation_window=timedelta(0),
        )
        session = _attach_sequence(sdk, _json_error(401), _json_error(401))
        checks = [_SA_TOKEN, _SA_TOKEN]

        for token in checks:
            await sdk.verify_service_account_token(token)

        assert session.request.call_count == len(checks)

    @pytest.mark.asyncio
    async def test_the_window_counts_from_before_grafana_is_asked(self, mocker):
        """Verify a slow verification does not stretch the window past its bound.

        The record is read after the check starts, so the verdict it yields
        must expire one window after the start, not one window after Grafana
        finally answers.
        """
        sdk = GrafanaSDK(
            endpoint=_ENDPOINT,
            service_account_token=_SERVICE_TOKEN,
            service_account_bearer_revocation_window=timedelta(seconds=1),
        )
        now = [100.0]
        mocker.patch(
            "app.core.auth.providers.grafana.sdk.monotonic", side_effect=lambda: now[0]
        )
        responses = iter([*_verified_pair(), *_verified_pair()])

        def slow_grafana(*_args, **_kwargs):
            """Answer the next response after 0.8 s of wall clock."""
            now[0] += 0.8
            return next(responses)

        session = MagicMock()
        session.request = MagicMock(side_effect=slow_grafana)
        sdk._session = session
        await sdk.verify_service_account_token(_SA_TOKEN)
        now[0] = 101.2

        await sdk.verify_service_account_token(_SA_TOKEN)

        assert session.request.call_count == _VERIFICATION_CALLS * 2

    @pytest.mark.asyncio
    async def test_the_cache_never_holds_the_token(self):
        """Verify the verdict cache keys on a digest, never the secret."""
        sdk = _sdk()
        _attach_sequence(sdk, *_verified_pair())

        await sdk.verify_service_account_token(_SA_TOKEN)

        store = sdk._service_account_verdicts.store
        assert list(store) == [(sha256(_SA_TOKEN.encode()).hexdigest(),)]
        assert [record for record, _ in store.values()] == [_validated_sa_record()]

    @pytest.mark.parametrize(
        "outcomes",
        [
            pytest.param(_verified_pair, id="verified"),
            pytest.param(lambda: [_json_error(401)], id="refused"),
            pytest.param(lambda: [_json_error(500)], id="upstream"),
            pytest.param(lambda: [_current_sa(uid="user:3")], id="off-contract"),
            pytest.param(lambda: [TimeoutError()], id="timeout"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_token_is_never_logged_or_raised(self, outcomes, caplog):
        """Verify no log record or exception detail carries the token."""
        sdk = _sdk()
        _attach_sequence(sdk, *outcomes())
        details: list[str] = []

        with caplog.at_level(logging.DEBUG):
            try:
                await sdk.verify_service_account_token(_SA_TOKEN)
            except GrafanaException as exc:
                details.append(str(exc.detail))

        assert _SA_TOKEN not in caplog.text
        assert all(_SA_TOKEN not in detail for detail in details)


class TestServiceAccountBearerRevocationWindow:
    """Verify the revocation-window setting."""

    def test_defaults_to_five_minutes(self):
        """Verify the documented default."""
        assert _sdk().service_account_bearer_revocation_window == timedelta(minutes=5)

    def test_rejects_a_negative_window(self):
        """Verify a negative window fails at construction."""
        with pytest.raises(ValidationError):
            GrafanaSDK(
                endpoint=_ENDPOINT,
                service_account_token=_SERVICE_TOKEN,
                service_account_bearer_revocation_window=-1,
            )


@pytest.mark.asyncio
async def test_get_service_accounts_reads_every_page():
    """Verify the listing follows ``totalCount`` across pages with PMM Extensions' token."""
    GrafanaSDK.get_service_accounts.cache_clear()
    sdk = _sdk()
    first = [{"id": n, "login": f"sa-1-{n}"} for n in range(100)]
    second = [{"id": n, "login": f"sa-1-{n}"} for n in range(100, 150)]
    session = _attach_sequence(
        sdk,
        _mock_response(json_data={"totalCount": 150, "serviceAccounts": first}),
        _mock_response(json_data={"totalCount": 150, "serviceAccounts": second}),
    )

    accounts = await sdk.get_service_accounts()

    assert accounts == [*first, *second]
    calls = session.request.call_args_list
    assert [call.kwargs["params"]["page"] for call in calls] == [1, 2]
    for call in calls:
        assert call.args[:2] == ("GET", "/api/serviceaccounts/search")
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
    GrafanaSDK.get_service_accounts.cache_clear()


@asynccontextmanager
async def _cookie_setting_grafana() -> AsyncGenerator[tuple[str, list[str | None]]]:
    """Serve a local Grafana stand-in whose login sets a session cookie.

    Reached as ``localhost`` rather than an IP literal, because aiohttp's
    default cookie jar refuses to store cookies from an IP-literal host and so
    could not show a replay at all.

    :yield: The base URL, and the ``Cookie`` header each ``/api/user`` request
        carried (``None`` when absent).
    """
    cookies_seen: list[str | None] = []

    async def login(_request: web.Request) -> web.Response:
        response = web.json_response({"message": "Logged in"})
        response.set_cookie("grafana_session", "human-session", path="/")
        return response

    async def current_user(request: web.Request) -> web.Response:
        cookies_seen.append(request.headers.get("Cookie"))
        return web.json_response({"message": "Invalid API key"}, status=401)

    server = web.Application()
    server.router.add_post("/login", login)
    server.router.add_get("/api/user", current_user)
    runner = web.AppRunner(server)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "localhost", 0)
        await site.start()
        _, port = runner.addresses[0][:2]
        yield f"http://localhost:{port}", cookies_seen
    finally:
        await runner.cleanup()


class TestSessionCookiesAreNotReplayed:
    """Verify a Grafana session cookie never rides along on a later call."""

    @pytest.mark.asyncio
    async def test_a_default_client_replays_the_cookie(self):
        """Pin the hazard: an ordinary ``RemoteAPI`` session stores and resends it."""
        async with (
            _cookie_setting_grafana() as (endpoint, cookies_seen),
            RemoteAPI(endpoint=endpoint) as api,
        ):
            await api.post("/login", json={})
            with pytest.raises(HTTPException):
                await api.get("/api/user")

        assert cookies_seen == ["grafana_session=human-session"]

    @pytest.mark.asyncio
    async def test_a_refused_token_stays_refused_after_a_human_login(self):
        """Verify a login's session cookie cannot answer for a service-account token.

        Grafana falls through to the session cookie when the bearer fails, so a
        replayed cookie would turn a revoked token into a human identity.
        """
        async with (
            _cookie_setting_grafana() as (endpoint, cookies_seen),
            GrafanaSDK(endpoint=endpoint, service_account_token=_SERVICE_TOKEN) as sdk,
        ):
            await sdk.login("alice", "secret")
            verdict = await sdk.verify_service_account_token(_SA_TOKEN)

        assert verdict is None
        assert cookies_seen == [None]


@pytest.mark.asyncio
async def test_get_service_accounts_stops_at_an_empty_page():
    """Verify an empty page ends the listing even short of ``totalCount``."""
    GrafanaSDK.get_service_accounts.cache_clear()
    sdk = _sdk()
    first = [{"id": 1, "login": "sa-1-a"}]
    session = _attach_sequence(
        sdk,
        _mock_response(json_data={"totalCount": 5, "serviceAccounts": first}),
        _mock_response(json_data={"totalCount": 5, "serviceAccounts": []}),
    )

    accounts = await sdk.get_service_accounts()

    assert accounts == first
    assert session.request.call_count == _EMPTY_PAGE_CALLS
    GrafanaSDK.get_service_accounts.cache_clear()


@pytest.mark.parametrize("login", ["", "   "])
@pytest.mark.asyncio
async def test_a_blank_login_is_a_bad_gateway(login):
    """Verify a login Grafana never leaves blank is refused as off contract."""
    sdk = _sdk()
    _attach_sequence(sdk, _current_sa(login=login))

    with pytest.raises(GrafanaException):
        await sdk.verify_service_account_token(_SA_TOKEN)


@pytest.mark.asyncio
async def test_get_service_accounts_refuses_an_unpaginable_payload():
    """Verify a listing without its pagination fields raises 502."""
    GrafanaSDK.get_service_accounts.cache_clear()
    sdk = _sdk()
    _attach_sequence(sdk, _mock_response(json_data={"serviceAccounts": "x"}))

    with pytest.raises(GrafanaException):
        await sdk.get_service_accounts()
    GrafanaSDK.get_service_accounts.cache_clear()
