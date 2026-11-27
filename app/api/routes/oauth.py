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

"""Define the API routes for OAuth authentication."""

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ValidationError

from app.api.deps import CurrentUser, RefreshTokenCookie
from app.core.auth.exceptions import HTTPUnauthorizedException, InactiveUserException
from app.core.auth.models import (
    OAuthToken,
    SessionExchangeTokenResponse,
    SPAOAuthTokenResponse,
)
from app.core.auth.utils import get_user_model
from app.extensions.config import extensions_settings, prefixed_cookie_path
from app.extensions.deps import (
    resolve_ambient_exchange_token,
    resolve_ambient_session_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

User = get_user_model()

_REFRESH_REJECTION_DETAIL = "Refresh token is missing, invalid, expired, or revoked"


class SPALoginBody(BaseModel):
    """Represent the JSON body expected by ``POST /api/oauth/login``.

    :param username: The username of the user.
    :type username: str
    :param password: The password of the user.
    :type password: str
    """

    username: str
    password: str


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Set the ``HttpOnly`` refresh-token cookie on ``response``.

    Pull cookie attributes (name, path, max-age, samesite, secure) from
    ``extensions_settings.SESSION_REFRESH`` and set ``httponly=True`` explicitly. The
    ``path`` is anchored under the configured URL prefix so the browser sends
    the cookie back to the refresh endpoint wherever PMM Extensions is served.

    :param response: The response on which to set the cookie.
    :param refresh_token: The refresh token value to store in the cookie.
    """
    cookie_options = extensions_settings.SESSION_REFRESH.model_dump(by_alias=True)
    cookie_options["path"] = prefixed_cookie_path(cookie_options["path"])
    response.set_cookie(**cookie_options, value=refresh_token, httponly=True)


def _clear_refresh_cookie(response: Response) -> None:
    """Delete the refresh-token cookie.

    Derive ``path`` from ``SESSION_REFRESH.PATH`` exactly as the set does, URL
    prefix included, so deletion mirrors how the cookie was set: when a ``Path``
    is configured, both emit the same one; when ``PATH`` is ``None``, both omit
    the attribute and the browser's default-path rules apply symmetrically. A
    delete whose ``Path`` differs from the set is silently ignored by the
    browser, leaving the cookie in place.

    :param response: The response on which to delete the cookie.
    """
    response.delete_cookie(
        key=extensions_settings.SESSION_REFRESH.COOKIE_NAME,
        path=prefixed_cookie_path(extensions_settings.SESSION_REFRESH.PATH),
    )


# TODO(yan): Prevent malicious account lockout
# SEP-277
@router.post("/token")
async def create_oauth_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> OAuthToken:
    """Generate an OAuth token for a user from their username and password.

    OAuth2-style endpoint kept for non-SPA clients. For the React SPA, use
    ``POST /api/oauth/login`` which returns a slim response body and places
    the refresh token in an ``HttpOnly`` cookie.

    :param form_data: The form data containing username and password.
    :type form_data: OAuth2PasswordRequestForm
    :return: The OAuth token for the user.
    :rtype: OAuthToken
    :raises InactiveUserException: If the user is not active.
    """
    oauth_token = await User.get_oauth_token(
        username=form_data.username,
        password=form_data.password,
    )
    user = await User.from_jwt(oauth_token.access_token)
    if not user.is_active:
        raise InactiveUserException

    return oauth_token


@router.post("/login")
async def spa_login(
    body: Annotated[SPALoginBody, Body()],
    response: Response,
) -> SPAOAuthTokenResponse:
    """Authenticate the SPA user and establish the refresh cookie.

    Run the Casdoor password grant server-side, set the refresh token as an
    ``HttpOnly`` cookie scoped to ``/api/oauth``, and return the access token
    (plus its TTL) in JSON. The refresh token is never included in the
    response body.

    :param body: The JSON body containing username and password.
    :type body: SPALoginBody
    :param response: The HTTP response on which to set the refresh cookie.
    :type response: Response
    :return: The slim OAuth token response for the SPA.
    :rtype: SPAOAuthTokenResponse
    :raises InactiveUserException: If the user is not active.
    """
    oauth_token = await User.get_oauth_token(
        username=body.username,
        password=body.password,
    )
    user = await User.from_jwt(oauth_token.access_token)
    if not user.is_active:
        raise InactiveUserException

    _set_refresh_cookie(response, oauth_token.refresh_token)
    return SPAOAuthTokenResponse(
        access_token=oauth_token.access_token,
        expires_in=oauth_token.expires_in,
    )


@router.post("/session")
async def spa_session_login(
    request: Request,
    response: Response,
) -> SPAOAuthTokenResponse:
    """Authenticate the SPA from an ambient Grafana session cookie.

    Read the ambient Grafana session cookie off the request, validate it against
    Grafana, set the ``HttpOnly`` refresh cookie scoped to ``/api/oauth``, and
    return the slim access assertion in JSON. A ``401`` (no valid ambient
    session) tells the SPA to show its login screen.

    :param request: The incoming request, carrying the ambient Grafana session
        cookie.
    :param response: The HTTP response on which to set the refresh cookie.
    :return: The slim OAuth token response for the SPA.
    :raises HTTPUnauthorizedException: If there is no valid ambient session.
    """
    oauth_token = await resolve_ambient_session_token(request)
    if oauth_token is None:
        raise HTTPUnauthorizedException("No valid ambient session.")
    _set_refresh_cookie(response, oauth_token.refresh_token)
    return SPAOAuthTokenResponse(
        access_token=oauth_token.access_token,
        expires_in=oauth_token.expires_in,
    )


@router.post("/session/exchange")
async def spa_session_exchange(
    request: Request,
    response: Response,
) -> SessionExchangeTokenResponse:
    """Mint a short-lived PMM Extensions bearer from an ambient Grafana session.

    Serves an embedded client that already carries the host's session cookie on
    the same origin. Unlike ``POST /session``, set no cookie and issue no refresh
    token: the caller holds the bearer in memory and exchanges again before it
    expires, so the deployment never shares a long-lived credential.

    Every exchange re-reads the identity from the provider and no validation is
    cached, so a role change takes effect within one bearer lifetime. A
    signed-out browser loses embedded access just as fast, because without the
    session cookie it cannot obtain another bearer -- though whether a
    *previously copied* host cookie stops working depends on the host revoking
    its own session server-side, which is outside PMM Extensions' control.

    Carries no auth dependency by design: the caller is not yet PMM Extensions authenticated,
    and the ambient session cookie is the credential being presented. No CSRF
    primitive applies either: requiring a Bearer token cannot gate an
    endpoint whose purpose is to issue one. That is safe because a cross-origin
    attacker cannot read this response (the CORS posture is an explicit
    per-environment origin allowlist, with the middleware absent when none is
    configured), and the bearer-authenticated calls the token enables are
    CSRF-exempt by design, since browsers never attach an ``Authorization``
    header automatically.

    Every failure -- no cookie, a rejected session, an unreachable provider --
    denies with the same ``401`` rather than reporting a reason the route cannot
    distinguish.

    :param request: The incoming request, carrying the ambient session cookie.
    :param response: The HTTP response, marked uncacheable so no intermediary
        stores the minted bearer.
    :return: The minted bearer and its lifetime.
    :raises HTTPUnauthorizedException: If there is no valid ambient session.
    """
    exchange_token = await resolve_ambient_exchange_token(request)
    if exchange_token is None:
        raise HTTPUnauthorizedException("No valid ambient session.")
    response.headers["Cache-Control"] = "no-store"
    return exchange_token


@router.post("/refresh")
async def refresh_token(
    response: Response,
    refresh_token: RefreshTokenCookie = None,
) -> SPAOAuthTokenResponse:
    """Mint a fresh access token from the ``HttpOnly`` refresh cookie.

    On success, rotate the refresh cookie and return a new access token. On
    any failure (missing cookie, Casdoor-rejected token, malformed Casdoor
    response), return 401 without mutating the cookie so the SPA observes a
    clean invalidation signal. The route never reads the refresh token from
    the request body.

    :param response: The HTTP response on which to rotate the refresh cookie.
    :type response: Response
    :param refresh_token: The refresh token from the ``refreshToken`` cookie.
    :type refresh_token: str | None
    :return: The slim OAuth token response for the SPA.
    :rtype: SPAOAuthTokenResponse
    :raises HTTPUnauthorizedException: If the refresh token is missing,
        invalid, expired, or revoked.
    :raises InactiveUserException: If the user is not active.
    """
    if not refresh_token:
        raise HTTPUnauthorizedException(_REFRESH_REJECTION_DETAIL)
    try:
        oauth_token = await User.get_oauth_token(refresh_token=refresh_token)
    except HTTPException as exc:
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        logger.debug(
            "Casdoor rejected refresh token (status=%s)",
            exc.status_code,
            exc_info=True,
        )
        raise HTTPUnauthorizedException(_REFRESH_REJECTION_DETAIL) from None
    except ValidationError:
        logger.exception("Failed to refresh token")
        raise HTTPUnauthorizedException(_REFRESH_REJECTION_DETAIL) from None

    user = await User.from_jwt(oauth_token.access_token)
    if not user.is_active:
        raise InactiveUserException

    _set_refresh_cookie(response, oauth_token.refresh_token)
    return SPAOAuthTokenResponse(
        access_token=oauth_token.access_token,
        expires_in=oauth_token.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def spa_logout(
    user: CurrentUser,
    response: Response,
) -> None:
    """End the SPA session: revoke the access token and clear the refresh cookie.

    The SPA authenticates this request via the ``Authorization: Bearer``
    header. Clear the refresh cookie first so a Casdoor revocation failure
    does not leave the browser with a stale cookie, then revoke the access
    token upstream. Swallow ``KeyError`` / ``ValidationError`` from the
    upstream revocation so the client always sees a clean 204.

    :param user: The authenticated user resolved from the Bearer token.
    :type user: CurrentUser
    :param response: The HTTP response on which to clear the refresh cookie.
    :type response: Response
    """
    _clear_refresh_cookie(response)
    try:
        await User.invalidate_oauth_token(user.access_token)
    except KeyError, ValidationError:
        logger.debug("Failed to invalidate OAuth token", exc_info=True)
