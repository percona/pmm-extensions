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

"""Define tests for the app.api.routes.oauth module."""

from unittest.mock import AsyncMock

import pytest
from faker import Faker
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import RefreshTokenCookie
from app.core.auth.exceptions import HTTPUnauthorizedException
from app.core.auth.providers.grafana.models import GrafanaUser
from app.core.auth.providers.grafana.sdk import GrafanaException
from app.core.auth.utils import get_user_model
from app.extensions.config import extensions_settings
from app.main import app
from tests.app.conftest import make_roleless_grafana_assertion

User = get_user_model()


@pytest.fixture
def test_client():
    """Create a test client for the app."""
    return TestClient(app)


def _build_user(faker: Faker, valid_username: str, *, active: bool = True) -> "User":  # ty: ignore[invalid-type-form]
    """Build a ``User`` instance with a configurable active/forbidden state."""
    return User(
        id=faker.uuid4(),
        owner="organization",
        username=valid_username,
        is_forbidden=not active,
    )


def _set_cookies_matching(set_cookie_headers: list[str], name: str) -> list[str]:
    """Return every ``Set-Cookie`` header whose cookie name is ``name``."""
    return [h for h in set_cookie_headers if h.split("=", 1)[0].strip() == name]


def test_create_oauth_token_success(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /token returns the full OAuthToken on valid credentials."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt, return_value=_build_user(faker, valid_username)
        ),
    )

    data = {
        "username": valid_username,
        "password": "valid_password",
    }
    response = test_client.post("/api/oauth/token", data=data)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == oauth_token.model_dump()


def test_create_oauth_token_inactive_user(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /token returns 403 when the user is inactive."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt,
            return_value=_build_user(faker, valid_username, active=False),
        ),
    )

    data = {
        "username": valid_username,
        "password": "valid_password",
    }
    response = test_client.post("/api/oauth/token", data=data)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "User is not active"


def test_spa_login_success(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /login returns the slim response and sets the refresh cookie."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt, return_value=_build_user(faker, valid_username)
        ),
    )

    response = test_client.post(
        "/api/oauth/login",
        json={"username": valid_username, "password": "valid_password"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "access_token": oauth_token.access_token,
        "expires_in": int(oauth_token.expires_in.total_seconds()),
    }
    assert "refresh_token" not in response.json()
    assert "id_token" not in response.json()
    assert "scope" not in response.json()
    assert "token_type" not in response.json()

    set_cookie_headers = response.headers.get_list("set-cookie")
    refresh_headers = _set_cookies_matching(set_cookie_headers, "refreshToken")
    assert len(refresh_headers) == 1
    refresh_header = refresh_headers[0]
    assert f"refreshToken={oauth_token.refresh_token}" in refresh_header
    assert "HttpOnly" in refresh_header
    assert "Path=/api/oauth" in refresh_header
    assert "SameSite=lax" in refresh_header


def test_spa_login_scopes_the_refresh_cookie_under_the_prefix(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert the refresh cookie is reachable from a prefixed refresh endpoint.

    The cookie ``Path`` is derived from the configured prefix rather than from
    the request, so the request itself needs no prefix to exercise it.
    """
    mocker.patch.object(extensions_settings, "ROOT_PATH", new="/extensions")
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt, return_value=_build_user(faker, valid_username)
        ),
    )

    response = test_client.post(
        "/api/oauth/login",
        json={"username": valid_username, "password": "valid_password"},
    )

    assert response.status_code == status.HTTP_200_OK
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert "Path=/extensions/api/oauth" in refresh_headers[0]


def test_spa_login_inactive_user(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /login returns 403 and does not set the refresh cookie."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt,
            return_value=_build_user(faker, valid_username, active=False),
        ),
    )

    response = test_client.post(
        "/api/oauth/login",
        json={"username": valid_username, "password": "valid_password"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "User is not active"
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_spa_login_invalid_credentials(test_client, valid_username, mocker):
    """Assert /login returns 401 when Casdoor rejects credentials."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(
            spec=User.get_oauth_token,
            side_effect=HTTPUnauthorizedException("Invalid username or password."),
        ),
    )

    response = test_client.post(
        "/api/oauth/login",
        json={"username": valid_username, "password": "wrong"},
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_spa_login_missing_body(test_client):
    """Assert /login returns 422 on a missing JSON body."""
    response = test_client.post("/api/oauth/login")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_refresh_from_cookie_success(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /refresh returns the slim response and rotates the cookie."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt, return_value=_build_user(faker, valid_username)
        ),
    )

    test_client.cookies.set("refreshToken", "prior-valid-refresh")
    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "access_token": oauth_token.access_token,
        "expires_in": int(oauth_token.expires_in.total_seconds()),
    }
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert f"refreshToken={oauth_token.refresh_token}" in refresh_headers[0]
    assert "Path=/api/oauth" in refresh_headers[0]
    assert "HttpOnly" in refresh_headers[0]


def test_refresh_missing_cookie(test_client):
    """Assert /refresh returns 401 when the refresh cookie is absent."""
    response = test_client.post("/api/oauth/refresh")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        response.json()["detail"]
        == "Refresh token is missing, invalid, expired, or revoked"
    )
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_refresh_invalid_cookie_validation_error(test_client, mocker):
    """Assert /refresh returns 401 on Casdoor malformed 2xx responses."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(
            spec=User.get_oauth_token,
            side_effect=ValidationError.from_exception_data(
                title="Validation Error", line_errors=[]
            ),
        ),
    )

    test_client.cookies.set("refreshToken", "bogus")
    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        response.json()["detail"]
        == "Refresh token is missing, invalid, expired, or revoked"
    )
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_refresh_cookie_rejected_by_casdoor(test_client, mocker):
    """Assert /refresh collapses upstream Casdoor 4xx to 401."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(
            spec=User.get_oauth_token,
            side_effect=HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_grant",
                headers={"X-Error-Code": "invalid_grant"},
            ),
        ),
    )

    test_client.cookies.set("refreshToken", "expired")
    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        response.json()["detail"]
        == "Refresh token is missing, invalid, expired, or revoked"
    )
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_refresh_upstream_failure_propagates(test_client, mocker):
    """Assert /refresh surfaces auth-provider 5xx instead of masking as 401."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(
            spec=User.get_oauth_token,
            side_effect=HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Casdoor unavailable",
            ),
        ),
    )

    test_client.cookies.set("refreshToken", "valid-but-upstream-down")
    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_refresh_cookie_alias_matches_settings():
    """Assert the /refresh Cookie alias tracks SESSION_REFRESH.COOKIE_NAME."""
    cookie_marker = RefreshTokenCookie.__metadata__[0]
    assert cookie_marker.alias == extensions_settings.SESSION_REFRESH.COOKIE_NAME


def test_refresh_body_ignored(test_client):
    """Assert /refresh no longer reads the refresh token from the JSON body."""
    response = test_client.post(
        "/api/oauth/refresh",
        json={"token": "legacy-body-token"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_refresh_inactive_user(
    test_client, valid_username, oauth_token, mocker, faker: Faker
):
    """Assert /refresh returns 403 for inactive users and does not rotate the cookie."""
    mocker.patch.object(
        User,
        "get_oauth_token",
        new=AsyncMock(spec=User.get_oauth_token, return_value=oauth_token),
    )
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(
            spec=User.from_jwt,
            return_value=_build_user(faker, valid_username, active=False),
        ),
    )

    test_client.cookies.set("refreshToken", "valid")
    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "User is not active"
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_logout_success(test_client, valid_username, mocker, faker: Faker):
    """Assert /logout clears the refresh cookie and revokes the access token."""
    access_token = "bearer-access-token"
    logged_in_user = _build_user(faker, valid_username)
    logged_in_user.access_token = access_token
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(spec=User.from_jwt, return_value=logged_in_user),
    )
    invalidate_mock = mocker.patch.object(
        User,
        "invalidate_oauth_token",
        new=AsyncMock(spec=User.invalidate_oauth_token, return_value=None),
    )

    response = test_client.post(
        "/api/oauth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    delete_header = refresh_headers[0]
    assert "Max-Age=0" in delete_header
    assert "Path=/api/oauth" in delete_header
    invalidate_mock.assert_awaited_once_with(access_token)


def test_logout_clears_the_cookie_at_the_prefixed_path(
    test_client, valid_username, mocker, faker: Faker
):
    """Assert deletion targets the same ``Path`` the login response set.

    A delete whose ``Path`` differs from the set leaves the cookie in the
    browser, so the two must derive it identically.
    """
    access_token = "bearer-access-token"
    logged_in_user = _build_user(faker, valid_username)
    logged_in_user.access_token = access_token
    mocker.patch.object(extensions_settings, "ROOT_PATH", new="/extensions")
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(spec=User.from_jwt, return_value=logged_in_user),
    )
    mocker.patch.object(
        User,
        "invalidate_oauth_token",
        new=AsyncMock(spec=User.invalidate_oauth_token, return_value=None),
    )

    response = test_client.post(
        "/api/oauth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert "Path=/extensions/api/oauth" in refresh_headers[0]


def test_logout_invalidate_fails_still_clears_cookie(
    test_client, valid_username, mocker, faker: Faker
):
    """Assert /logout clears the cookie even if upstream revocation fails."""
    access_token = "bearer-access-token"
    logged_in_user = _build_user(faker, valid_username)
    logged_in_user.access_token = access_token
    mocker.patch.object(
        User,
        "from_jwt",
        new=AsyncMock(spec=User.from_jwt, return_value=logged_in_user),
    )
    mocker.patch.object(
        User,
        "invalidate_oauth_token",
        new=AsyncMock(spec=User.invalidate_oauth_token, side_effect=KeyError("jti")),
    )

    response = test_client.post(
        "/api/oauth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert "Max-Age=0" in refresh_headers[0]
    assert "Path=/api/oauth" in refresh_headers[0]


def test_logout_no_bearer(test_client):
    """Assert /logout returns 401 without a Bearer token and does not clear cookies."""
    test_client.cookies.set("refreshToken", "present-but-unauthenticated")
    response = test_client.post("/api/oauth/logout")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_session_refresh_defaults_match_plan():
    """Assert SESSION_REFRESH defaults produce the expected cookie attributes."""
    assert extensions_settings.SESSION_REFRESH.COOKIE_NAME == "refreshToken"
    assert extensions_settings.SESSION_REFRESH.PATH == "/api/oauth"


def test_spa_session_login_success(
    test_client, grafana_mock, grafana_user_record, mocker
):
    """Assert POST /session mints a session from an ambient Grafana cookie."""
    mocker.patch.object(extensions_settings, "AMBIENT_SESSION_SSO_ENABLED", new=True)
    test_client.cookies.set("grafana_session", "ambient")

    response = test_client.post("/api/oauth/session")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["access_token"]
    assert "expires_in" in body
    assert "refresh_token" not in body
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert "HttpOnly" in refresh_headers[0]
    assert "Path=/api/oauth" in refresh_headers[0]


def test_spa_session_login_no_cookie_returns_401(test_client, grafana_mock, mocker):
    """Assert POST /session returns 401 (silent fallback) with no ambient cookie."""
    mocker.patch.object(extensions_settings, "AMBIENT_SESSION_SSO_ENABLED", new=True)

    response = test_client.post("/api/oauth/session")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_spa_session_login_toggle_off_returns_401(test_client, grafana_mock):
    """Assert POST /session returns 401 when ambient SSO is disabled (default)."""
    test_client.cookies.set("grafana_session", "ambient")

    response = test_client.post("/api/oauth/session")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert not _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )


def test_grafana_spa_login_then_refresh(
    test_client, grafana_mock, grafana_user_record, mocker
):
    """Assert the SPA can log in with Grafana active and then refresh its session.

    Drives the real ``GrafanaUser`` through the routes (Grafana HTTP mocked) so a
    non-empty refresh cookie is set at login and reused at refresh -- the flow an
    empty Grafana refresh token would break on every SPA reload.
    """
    mocker.patch("app.api.routes.oauth.User", GrafanaUser)

    login = test_client.post(
        "/api/oauth/login",
        json={"username": grafana_user_record["login"], "password": "secret"},
    )

    assert login.status_code == status.HTTP_200_OK
    assert login.json()["access_token"]
    login_cookies = _set_cookies_matching(
        login.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(login_cookies) == 1
    assert "refreshToken=;" not in login_cookies[0]

    refreshed = test_client.post("/api/oauth/refresh")

    assert refreshed.status_code == status.HTTP_200_OK
    assert refreshed.json()["access_token"]


def test_grafana_spa_refresh_refuses_a_cookie_minted_before_the_role_claim(
    test_client, grafana_mock, mocker
):
    """Assert a refresh cookie predating the role claim ends the SPA session.

    The standalone SPA holds its access token in memory and its refresh
    credential in the cookie; both predate the claim across the upgrade, so the
    session ends loudly and the re-login reads the true org role from Grafana.
    """
    mocker.patch("app.api.routes.oauth.User", GrafanaUser)
    test_client.cookies.set("refreshToken", make_roleless_grafana_assertion("refresh"))

    response = test_client.post("/api/oauth/refresh")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


EXCHANGE_PATH = "/api/oauth/session/exchange"


@pytest.fixture
def _ambient_exchange(grafana_mock, mocker):
    """Enable ambient SSO and point the Bearer path at the real Grafana user model."""
    mocker.patch.object(extensions_settings, "AMBIENT_SESSION_SSO_ENABLED", new=True)
    mocker.patch("app.api.deps.User", GrafanaUser)


@pytest.mark.usefixtures("_ambient_exchange")
class TestSpaSessionExchange:
    """Exercise ``POST /api/oauth/session/exchange`` end to end through the app."""

    def test_success_returns_a_short_lived_token(self, test_client, grafana_mock):
        """Assert a valid ambient session is exchanged for a bearer plus its TTL."""
        test_client.cookies.set("grafana_session", "ambient")

        response = test_client.post(EXCHANGE_PATH)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["access_token"]
        assert body["expires_in"] == grafana_mock.exchange_token_max_age.total_seconds()

    def test_sets_no_cookie_at_all(self, test_client):
        """Assert the exchange sets no cookie of any name, refresh or otherwise."""
        test_client.cookies.set("grafana_session", "ambient")

        response = test_client.post(EXCHANGE_PATH)

        assert response.status_code == status.HTTP_200_OK
        assert response.headers.get_list("set-cookie") == []

    def test_response_is_not_cacheable(self, test_client):
        """Assert the bearer is not stored by an intermediary or the browser."""
        test_client.cookies.set("grafana_session", "ambient")

        response = test_client.post(EXCHANGE_PATH)

        assert response.headers["cache-control"] == "no-store"

    def test_body_carries_no_refresh_token(self, test_client):
        """Assert no refresh credential is issued on the exchange path."""
        test_client.cookies.set("grafana_session", "ambient")

        response = test_client.post(EXCHANGE_PATH)

        assert "refresh_token" not in response.json()

    def test_absent_session_returns_401(self, test_client):
        """Assert a request with no ambient cookie is denied."""
        response = test_client.post(EXCHANGE_PATH)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers.get_list("set-cookie") == []

    def test_grafana_rejects_the_session_returns_401(self, test_client, grafana_mock):
        """Assert a Grafana-rejected session is denied."""
        grafana_mock.get_current_user.side_effect = HTTPUnauthorizedException()
        test_client.cookies.set("grafana_session", "stale")

        response = test_client.post(EXCHANGE_PATH)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_grafana_unreachable_fails_closed(self, test_client, grafana_mock):
        """Assert an upstream outage denies with 401 rather than leaking a 502.

        An absent session and a Grafana outage are deliberately
        indistinguishable to the caller, and neither may yield a token.
        """
        grafana_mock.get_current_user.side_effect = GrafanaException()
        test_client.cookies.set("grafana_session", "ambient")

        response = test_client.post(EXCHANGE_PATH)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert "access_token" not in response.json()

    def test_exchange_token_authenticates_an_api_call(self, test_client):
        """Assert the minted bearer is accepted by an authentication-gated route."""
        test_client.cookies.set("grafana_session", "ambient")
        token = test_client.post(EXCHANGE_PATH).json()["access_token"]

        gated = test_client.get(
            "/api/config/alerts", headers={"Authorization": f"Bearer {token}"}
        )

        assert gated.status_code == status.HTTP_200_OK

    def test_an_unexchanged_request_is_refused_by_the_same_route(self, test_client):
        """Assert the gated route rejects a caller holding no bearer."""
        gated = test_client.get("/api/config/alerts")

        assert gated.status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_bearer_minted_before_the_role_claim_is_refused(self, test_client):
        """Assert a legacy bearer is denied at the API boundary, not a 500."""
        legacy = make_roleless_grafana_assertion("access")

        gated = test_client.get(
            "/api/config/alerts", headers={"Authorization": f"Bearer {legacy}"}
        )

        assert gated.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.parametrize("role", ["Editor", "Viewer"])
    def test_non_admin_role_is_refused_by_an_admin_gated_endpoint(
        self, test_client, grafana_mock, grafana_user_orgs, role
    ):
        """Assert a non-admin Grafana role gains no admin surface via the exchange.

        The admin-granted direction is asserted against ``get_current_admin`` in
        ``tests/app/api/test_deps.py``: every admin-gated route under
        ``app/api`` serializes through ``response_model=User``, which binds to
        the Casdoor model at import and cannot represent a ``GrafanaUser``.
        """
        grafana_mock.get_current_user_orgs.return_value = [
            {**grafana_user_orgs[0], "role": role}
        ]
        test_client.cookies.set("grafana_session", "ambient")
        token = test_client.post(EXCHANGE_PATH).json()["access_token"]

        listed = test_client.get(
            "/api/users/", headers={"Authorization": f"Bearer {token}"}
        )

        assert listed.status_code == status.HTTP_403_FORBIDDEN


def test_spa_session_exchange_toggle_off_returns_401(test_client, grafana_mock):
    """Assert the exchange is denied when ambient SSO is disabled (the default)."""
    test_client.cookies.set("grafana_session", "ambient")

    response = test_client.post(EXCHANGE_PATH)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_spa_session_login_contract_survives_the_extraction(
    test_client, grafana_mock, mocker
):
    """Assert ``POST /session`` is byte-compatible after the shared-read extraction.

    ``oauth_token_from_session`` and the new exchange grant now share one
    ambient-session read; this pins the sibling route's response shape and its
    refresh cookie against drift.
    """
    mocker.patch.object(extensions_settings, "AMBIENT_SESSION_SSO_ENABLED", new=True)
    test_client.cookies.set("grafana_session", "ambient")

    response = test_client.post("/api/oauth/session")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["access_token"]
    assert "expires_in" in body
    assert "refresh_token" not in body
    refresh_headers = _set_cookies_matching(
        response.headers.get_list("set-cookie"), "refreshToken"
    )
    assert len(refresh_headers) == 1
    assert "HttpOnly" in refresh_headers[0]
    assert "Path=/api/oauth" in refresh_headers[0]


@pytest.mark.parametrize(
    "path",
    [
        "/api/oauth/token",
        "/api/oauth/login",
        "/api/oauth/session",
        "/api/oauth/session/exchange",
        "/api/oauth/refresh",
        "/api/oauth/logout",
    ],
)
def test_oauth_routes_stay_outside_the_unsafe_method_admin_gate(
    test_client, casdoor_mock, path
):
    """Assert the identity tree keeps its own authentication semantics.

    These routes are included beside ``api_router`` rather than through it, so
    the admin gate never reaches them — a caller with no prior PMM Extensions identity has
    to be able to mint one, and ``logout`` stays bearer-authenticated by its own
    ``CurrentUser``. Only the absence of a 403 is asserted; each route's own
    status is pinned by the tests above.

    The request carries a credential resolving to a non-admin, which is the only
    input that makes the gate answer 403. A credential-less request answers 401
    whether or not the gate is attached, so it could not falsify the claim.
    """
    response = test_client.post(
        path, headers={"Authorization": "Bearer non-admin-token"}
    )

    assert response.status_code != status.HTTP_403_FORBIDDEN
