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

"""Define the API dependencies."""

import logging
import secrets
from collections.abc import Callable
from hashlib import sha256
from typing import Annotated, Any, Final, TypeVar
from uuid import UUID

from fastapi import Cookie, Depends, Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError

from app.core.auth.exceptions import (
    HTTPForbiddenException,
    HTTPUnauthorizedException,
    InactiveUserException,
)
from app.core.auth.models import BaseUser, UserRole
from app.core.auth.utils import get_user_model
from app.core.config import settings
from app.core.log import set_log_context
from app.core.security import is_bearer_authenticated, SAFE_HTTP_METHODS
from app.extensions.config import extensions_settings

logger = logging.getLogger(__name__)
#: The provider-selected concrete user class, resolved from configuration at
#: import time. Annotations below name ``BaseUser`` instead, which is the
#: strongest type that is statically knowable here; nothing reads those
#: annotations at runtime.
User = get_user_model()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/oauth/token")

AuthToken = Annotated[str, Depends(oauth2_scheme)]

RefreshTokenCookie = Annotated[
    str | None, Cookie(alias=extensions_settings.SESSION_REFRESH.COOKIE_NAME)
]

SERVICE_PRINCIPAL_ID = UUID("00000000-0000-4000-8000-000000000000")
SERVICE_PRINCIPAL = User.build_service_principal(
    user_id=SERVICE_PRINCIPAL_ID,
    username="extensions-service",
    first_name="PMM Extensions",
    last_name="Service",
    role=UserRole.VIEWER,
)


def _build_service_principal(secret: str) -> BaseUser:
    """Return a per-request copy of the service principal with ``access_token`` set.

    ``access_token`` is a property-backed private attribute on ``BaseUser``;
    Pydantic v2's ``model_copy(update=...)`` silently drops non-field keys, so
    the value must be assigned via the property setter after the copy.

    :param secret: The unwrapped ``EXTENSIONS_INTERNAL_TOKEN`` value.
    :return: A fresh copy of the singleton with ``access_token`` populated.
    """
    user = SERVICE_PRINCIPAL.model_copy()
    user.access_token = secret
    return user


async def authenticate_bearer_token(token: str) -> BaseUser:
    """Return the authenticated user from an OAuth2 token.

    When ``settings.EXTENSIONS_INTERNAL_TOKEN`` is non-empty and the incoming Bearer
    token matches it (constant-time comparison), return a synthetic non-admin
    "service principal" user instead of contacting the OAuth provider. This
    allows PMM Extensions internal service-to-service calls (e.g. scheduled inventory
    sync) to authenticate with a stable deployment-level secret rather than a
    short-lived personal access token.

    Otherwise the token is validated through ``User.from_bearer``, which accepts
    the credential types the active provider honors on its Bearer surface — for
    Grafana, an access assertion or a short-lived session-exchange assertion. The
    session-cookie surface stays narrower and validates through ``from_jwt``.

    :param token: The OAuth2 token to authenticate the user.
    :return: The authenticated user.
    :raises HTTPUnauthorizedException: If the token is invalid and authentication fails.
    :raises InactiveUserException: If authentication succeeds but the user is not
        active.
    :raises BaseAuthProviderException: If the auth provider errors while
        validating the credential.
    """
    secret = settings.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
    if secret and secrets.compare_digest(token, secret):
        set_log_context(user=SERVICE_PRINCIPAL.username)
        return _build_service_principal(secret)
    try:
        user = await User.from_bearer(token)
    except ValidationError:
        logger.exception("Failed to authenticate user")
        raise HTTPUnauthorizedException from None
    if not user.is_active:
        raise InactiveUserException
    set_log_context(user=user.username)
    return user


#: Scope key under which a request's resolved users are cached. Kept out of
#: ``scope["state"]``, which a request inherits as a shallow copy of the ASGI
#: lifespan state: a cache under a key anything publishes there would be shared
#: process-wide. Entries key on a digest of the credential rather than the
#: credential itself, so no structure the request carries grows a key that is a
#: secret.
_RESOLVED_USERS_KEY: Final = "app.api.deps.resolved_users"


async def get_current_user(request: Request, token: AuthToken) -> BaseUser:
    """Return the authenticated user, resolving each credential once per request.

    The cache is keyed on a digest of the credential, so a resolution is never
    served to a caller presenting a different one, and holds successes only, so a
    refused credential is re-derived rather than remembered. It lives under a
    private key in the request scope, which the ASGI server builds per request, so
    it cannot outlive the request that created it.

    A hit re-establishes the log identity the resolution it replaces would have
    set, since that identity is a context variable the last resolution wrote
    rather than something the returned user carries.

    :param request: The incoming HTTP request, whose scope holds the cache.
    :param token: The OAuth2 token to authenticate the user.
    :return: The authenticated user, resolved here or on an earlier call.
    :raises HTTPUnauthorizedException: If the token is invalid and authentication fails.
    :raises InactiveUserException: If authentication succeeds but the user is not
        active.
    :raises BaseAuthProviderException: If the auth provider errors while
        validating the credential.
    """
    resolved: dict[bytes, BaseUser] = request.scope.setdefault(_RESOLVED_USERS_KEY, {})
    key = sha256(token.encode()).digest()
    if (user := resolved.get(key)) is not None:
        set_log_context(user=user.username)
        return user
    resolved[key] = user = await authenticate_bearer_token(token)
    return user


IsAuthenticatedDep = Depends(get_current_user)
CurrentUser = Annotated[BaseUser, IsAuthenticatedDep]


async def get_current_admin(current_user: CurrentUser) -> BaseUser:
    """Return the authenticated admin from an OAuth2 token.

    :param current_user: The current logged-in user.
    :type current_user: CurrentUser
    :return: The authenticated admin user.
    :rtype: User
    :raises HTTPForbiddenException: If the user is not an admin.
    """
    if not current_user.is_admin:
        raise HTTPForbiddenException
    return current_user


IsAdminDep = Depends(get_current_admin)


async def get_current_service_principal(current_user: CurrentUser) -> BaseUser:
    """Return the authenticated caller only when it is the service principal.

    Gates writes whose rows a syncer owns: those writes authenticate with
    ``EXTENSIONS_INTERNAL_TOKEN``, so a human credential is refused on identity rather
    than ranked. Composing this with :func:`get_current_admin` would refuse the
    principal too, which holds ``UserRole.VIEWER``.

    :param current_user: The current logged-in user.
    :return: The service principal.
    :raises HTTPForbiddenException: If the caller is not the service principal.
    """
    if current_user.id != SERVICE_PRINCIPAL_ID:
        raise HTTPForbiddenException
    return current_user


IsServicePrincipalDep = Depends(get_current_service_principal)

F = TypeVar("F", bound=Callable[..., Any])

#: The rank an unsafe route requires when nothing registers a lower one, so a
#: route added without a thought about authorization ships admin-only.
DEFAULT_MINIMUM_ROLE: Final = UserRole.ADMIN

_ROUTE_MINIMUM_ROLES: dict[Callable[..., Any], UserRole] = {}


def require_minimum_role(role: UserRole) -> Callable[[F], F]:
    """Return a decorator registering the minimum role one route requires.

    The registration keys on the object FastAPI stores as
    ``APIRoute.endpoint``, so this belongs below any decorator that returns a
    *new* function: a wrapper between the two leaves the registry keyed on a
    callable no request reaches, and the route falls back to
    :data:`DEFAULT_MINIMUM_ROLE` with nothing raised to say so. Order against
    the route decorator itself is immaterial — that one returns the endpoint
    unchanged. Registering a rank below ``ADMIN`` opens a surface the gate would
    otherwise close, so it belongs on a route whose operation the named rank is
    trusted with — and ``UserRole.NONE`` on one whose method is unsafe but whose
    operation is a read.

    :param role: The lowest rank the route admits.
    :return: A decorator registering the endpoint it receives, unchanged.
    """

    def register(endpoint: F) -> F:
        _ROUTE_MINIMUM_ROLES[endpoint] = role
        return endpoint

    return register


def minimum_role_for(route: object | None) -> UserRole:
    """Return the minimum role a matched route requires.

    The single home of the unregistered-route default, read by the gate and by
    the test that classifies PMM Extensions' unsafe surface, so the two cannot disagree
    about what an unregistered route resolves to. A request no route matched
    carries no ``route`` in its scope and resolves the same way.

    :param route: The matched route from the request scope, if any. Typed
        ``object`` because the scope value is untyped and need not be an
        ``APIRoute``; anything without an ``endpoint`` takes the default.
    :return: The registered minimum, or :data:`DEFAULT_MINIMUM_ROLE`.
    """
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return DEFAULT_MINIMUM_ROLE
    return _ROUTE_MINIMUM_ROLES.get(endpoint, DEFAULT_MINIMUM_ROLE)


async def require_minimum_role_for_unsafe_methods(request: Request) -> None:
    """Require each mutating HTTP method's route-registered minimum role.

    The authorization sibling of ``require_bearer_for_unsafe_methods``. Safe
    methods pass untouched, keeping whatever authentication they already carry;
    every other method — ``POST``, ``PUT``, ``PATCH`` and ``DELETE``, and
    equally anything no route declares — resolves the caller and admits it only
    from :func:`minimum_role_for` upwards, ranks comparing on ``UserRole``'s
    declared order rather than on equality.

    A ``UserRole.NONE`` minimum is answered before the credential is looked at,
    so the gate imposes no authentication of its own on a route it waives and
    that route's own ``dependencies`` stay the only thing authenticating it.

    The user is resolved in the body rather than through a sub-dependency: a
    sub-dependency resolves eagerly and would force authentication onto the
    unauthenticated ``GET /health`` that every service exposes. The request is
    passed along so the route's own authentication dependency is served from this
    resolution rather than repeating it.

    ``EXTENSIONS_INTERNAL_TOKEN``'s service principal is admitted by identity so
    scheduled inventory sync and scheduled execution keep working. It holds
    ``UserRole.VIEWER`` and so would fail every minimum above it; the bypass
    stays scoped to this gate, leaving every pre-existing ``IsApiAdmin`` /
    ``IsAdminDep`` check refusing it as before.

    :param request: The incoming HTTP request.
    :raises HTTPUnauthorizedException: When the method is unsafe and the request
        carries no Bearer credential, or the credential does not validate.
    :raises HTTPForbiddenException: When the resolved user ranks below the
        route's minimum, or is inactive.
    :raises BaseAuthProviderException: When the auth provider errors while
        validating the credential.
    """
    if request.method in SAFE_HTTP_METHODS:
        return
    minimum = minimum_role_for(request.scope.get("route"))
    if minimum is UserRole.NONE:
        return
    if not is_bearer_authenticated(request):
        raise HTTPUnauthorizedException
    user = await get_current_user(request, await oauth2_scheme(request))
    if user.id == SERVICE_PRINCIPAL_ID:
        return
    if user.role < minimum:
        raise HTTPForbiddenException


RequireMinimumRoleForUnsafeMethods = Depends(require_minimum_role_for_unsafe_methods)


def get_current_user_id(current_user: CurrentUser) -> str:
    """Get the current user's ID as a string.

    :param current_user: The current authenticated user.
    :type current_user: CurrentUser
    :return: The user's ID as a string.
    :rtype: str
    """
    return str(current_user.id)


CurrentUserID = Annotated[str, Depends(get_current_user_id)]


async def get_current_admin_username(admin: Annotated[BaseUser, IsAdminDep]) -> str:
    """Get the authenticated admin's username.

    The settings router records it as the actor on each override row it writes.
    It takes the username rather than the id because the id is a UUID a consumer
    would have to resolve back through the auth provider to render.

    :param admin: The current authenticated admin.
    :return: The admin's username.
    """
    return admin.username


AdminUsername = Annotated[str, Depends(get_current_admin_username)]
