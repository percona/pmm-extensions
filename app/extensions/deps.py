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

"""Define PMM Extensions dependencies."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import suppress
from time import monotonic
from typing import Annotated, Any, Literal, overload

from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel.ext.asyncio.session import AsyncSession

from app import __summary__, __version__
from app.api.deps import get_current_user as get_current_user_api
from app.api.deps import oauth2_scheme
from app.core.auth import config as auth_config
from app.core.auth.base import BaseAuthProvider
from app.core.auth.exceptions import HTTPForbiddenException, HTTPUnauthorizedException
from app.core.auth.models import (
    BaseUser,
    OAuthToken,
    SessionExchangeTokenResponse,
)
from app.core.auth.utils import get_user_model
from app.core.config import settings
from app.core.exceptions import (
    HTTPConflictException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.pagination import fetch_all_dict_items
from app.core.requests import as_json_object, RemoteAPI
from app.core.security import is_bearer_authenticated, SAFE_HTTP_METHODS
from app.core.utils.cache import TTLCache
from app.core.utils.fields import URL
from app.extensions.clients.pmm import PMMRemoteAPI
from app.extensions.config import extensions_settings
from app.extensions.crud import AppStateManager
from app.extensions.db import get_async_session_maker
from app.extensions.inventory import (
    CreatedEntity,
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
    ENTITY_MAPPING,
)
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.config import inventory_settings
from app.tasks.config import tasks_settings
from app.tasks.models import (
    Task,
    TaskHistoryResponse,
    TaskHistoryStatusEnum,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def get_base_url(request: Request) -> URL:
    """Return the application's base URL.

    If the ``BASE_URL`` setting is defined, returns it. Otherwise the base is
    derived from the incoming request, and carries the URL prefix the application
    is served under, so a URL composed on it stays inside that prefix. It always
    ends in a trailing slash, which callers joining a path onto it must absorb.

    :param request: The HTTP request object from which the base URL is derived.
    :return: The application's base URL.
    """
    if settings.BASE_URL is not None:
        return settings.BASE_URL
    return URL(str(request.base_url))


async def get_current_user(
    request: Request,
) -> BaseUser:
    """Return the authenticated user from the ``Authorization: Bearer`` header.

    The header is checked explicitly before delegating to ``oauth2_scheme``:
    that scheme carries ``auto_error=True`` and would raise a bare Starlette
    ``HTTPException`` for a header-less request, bypassing PMM Extensions' project
    exceptions.

    The request travels on with the token, so a credential something else on the
    same request already authenticated is served from that resolution rather than
    authenticated again.

    :param request: The incoming HTTP request.
    :return: The authenticated user.
    :raises HTTPUnauthorizedException: If no Bearer token is present, or the
        token is invalid.
    :raises HTTPForbiddenException: If the resolved user is inactive.
    """
    if not is_bearer_authenticated(request):
        raise HTTPUnauthorizedException
    bearer_token = await oauth2_scheme(request)
    return await get_current_user_api(request, bearer_token)


IsApiAuthenticated = Depends(get_current_user)
ApiCurrentUser = Annotated[BaseUser, IsApiAuthenticated]


BEARER_REQUIRED_DETAIL = "Bearer authentication required for state-changing requests."


async def require_bearer_for_unsafe_methods(request: Request) -> None:
    """Require a Bearer Authorization header on mutating HTTP methods.

    Browsers never attach an ``Authorization`` header automatically, so
    requiring a Bearer token on mutating routes blocks cross-site JSON POSTs
    from a malicious origin. ``GET``, ``HEAD`` and ``OPTIONS`` pass through
    (reads and CORS preflights are unaffected). ``POST``, ``PUT``, ``PATCH``
    and ``DELETE`` require ``Authorization: Bearer ...``.

    This is a backstop rather than the primary control: :func:`get_current_user`
    already rejects a header-less request on every method, so wherever
    ``IsApiAuthenticated`` is also attached this dependency cannot be the one
    that rejects. It is kept at router level so that a route reachable without
    that alias, or a future non-Bearer authentication path, still inherits the
    guard uniformly.

    :param request: The incoming HTTP request.
    :raises HTTPUnauthorizedException: When the method is unsafe and the
        request lacks an ``Authorization: Bearer`` header.
    """
    if request.method in SAFE_HTTP_METHODS:
        return
    if not is_bearer_authenticated(request):
        raise HTTPUnauthorizedException(detail=BEARER_REQUIRED_DETAIL)


RequireBearerForUnsafeMethods = Depends(require_bearer_for_unsafe_methods)


async def get_api_authenticated_admin(api_user: ApiCurrentUser) -> BaseUser:
    """Return the authenticated API admin user.

    :param api_user: The current API-authenticated user.
    :type api_user: ApiCurrentUser
    :return: The authenticated admin user.
    :rtype: User
    :raises HTTPForbiddenException: If the user is not an admin.
    """
    if not api_user.is_admin:
        raise HTTPForbiddenException
    return api_user


IsApiAdmin = Depends(get_api_authenticated_admin)
ApiAdminUser = Annotated[BaseUser, IsApiAdmin]


async def get_api_admin_username(admin: ApiAdminUser) -> str:
    """Get the authenticated API admin's username.

    The settings router records it as the actor on each override row it writes.
    It takes the username rather than the id because the id is a UUID a consumer
    would have to resolve back through the auth provider to render.

    :param admin: The current authenticated API admin.
    :return: The admin's username.
    """
    return admin.username


ApiAdminUsername = Annotated[str, Depends(get_api_admin_username)]


def _ambient_session_provider() -> BaseAuthProvider | None:
    """Return the active auth provider when ambient-session auth is available.

    :return: The active provider, or ``None`` when ambient SSO is disabled or the
        provider carries no ambient session.
    """
    if not extensions_settings.AMBIENT_SESSION_SSO_ENABLED:
        return None
    provider = auth_config.get_active_auth_provider()
    return provider if provider.supports_ambient_session else None


async def resolve_ambient_session_token(request: Request) -> OAuthToken | None:
    """Resolve an ambient provider session on the request into a PMM Extensions token pair.

    A no-op (``None``) unless ambient SSO is enabled and the active auth provider
    supports ambient sessions. Operational or upstream failures are logged and
    swallowed so auto-login degrades silently to the login form; a rejected
    session (upstream 401) likewise resolves to ``None`` in the provider.

    :param request: The incoming request, whose provider session cookie carries
        the ambient session.
    :return: A minted ``OAuthToken`` on a valid ambient session, else ``None``.
    """
    provider = _ambient_session_provider()
    if provider is None:
        return None
    try:
        return await provider.resolve_ambient_session(request.cookies)
    except HTTPException:
        logger.warning(
            "Ambient auto-login failed (upstream/operational); "
            "falling back to the login form.",
            exc_info=True,
        )
        return None


async def resolve_ambient_exchange_token(
    request: Request,
) -> SessionExchangeTokenResponse | None:
    """Resolve an ambient provider session on the request into a short-lived bearer.

    Gate on the same ambient-session availability as
    :func:`resolve_ambient_session_token`, and swallow upstream failures the same
    way, so an absent session, a rejected session, and a provider outage are
    indistinguishable to the caller and all deny.

    :param request: The incoming request, whose provider session cookie carries
        the ambient session.
    :return: The minted bearer on a valid ambient session, else ``None``.
    """
    provider = _ambient_session_provider()
    if provider is None:
        return None
    try:
        return await provider.exchange_ambient_session(request.cookies)
    except HTTPException:
        logger.warning(
            "Ambient session exchange failed (upstream/operational); denying.",
            exc_info=True,
        )
        return None


#: Seconds a failed actor listing is answered with an empty map without a retry.
USERNAME_MAPPING_FAILURE_WINDOW = 30.0


_username_mapping_failure: TTLCache[bool] = TTLCache(
    ttl=USERNAME_MAPPING_FAILURE_WINDOW, maxsize=1, typed=False
)
_USERNAME_MAPPING_FAILURE_KEY = ()


def reset_username_mapping_failure_window() -> None:
    """Forget any failure ``get_username_mapping`` is remembering."""
    _username_mapping_failure.clear()


async def get_username_mapping() -> dict[str, str]:
    """Create a mapping from actor ID to username using the active auth provider.

    Fetch every actor from the active provider (its users plus any identity
    that can run tasks without being listed as a user, such as a Grafana service
    account) and map each actor's ID to their username. A successful listing is
    not cached here: the provider SDKs already cache it for 300 s.

    Any provider failure yields an empty mapping (logged with its traceback) so
    a display-name lookup never fails the request that asked for it. The SDKs
    never cache a failure, so it is remembered here for
    ``USERNAME_MAPPING_FAILURE_WINDOW`` (30 s): calls inside the window return
    the empty mapping at once instead of each waiting on a downed provider.

    :return: A dictionary mapping actor IDs to usernames.
    """
    with _username_mapping_failure.lock, suppress(KeyError):
        _username_mapping_failure.get(_USERNAME_MAPPING_FAILURE_KEY, monotonic())
        return {}
    try:
        users = await User.get_actors()
    except Exception:
        logger.exception("Failed to get username mapping from the auth provider")
        with _username_mapping_failure.lock:
            now = monotonic()
            _username_mapping_failure.set(
                _USERNAME_MAPPING_FAILURE_KEY, value=True, now=now
            )
            _username_mapping_failure.evict_if_needed(now)
        return {}
    # A concurrent call may have opened the window while this one was in flight;
    # the provider has answered since, so stop serving raw ids.
    reset_username_mapping_failure_window()
    return {str(user.id): user.username for user in users}


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an asynchronous database session for FastAPI routes.

    This function provides a dependency for FastAPI routes that yields an ``AsyncSession``
    for interacting with the database. The session is properly closed after use.

    :yield: An asynchronous session for database operations.
    :rtype: AsyncSession
    """
    async_session_maker = get_async_session_maker()
    async with async_session_maker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


PROTECTED_APP_KEYS: frozenset[str] = frozenset({"inventory"})
"""Apps that cannot be disabled at runtime.

These are foundational to PMM Extensions and are excluded from every plugin-state
mechanism: never seeded, never guarded, always present in the sidebar, and the
toggle endpoint returns 409 for them. The frozenset is the single source of
truth -- the seed, both mount loops, the default-context filter, and both
admin endpoints all consult it.

The parallel exclusion category is a **child app** (``parent_key`` set): it too
is never seeded and cannot be toggled independently (the toggle endpoint returns
409), but -- unlike a protected app -- its runtime state is not forced on; it
*derives* from its parent's ``AppState`` via :attr:`BaseApp.state_key`, so its
mount gate, sidebar visibility, and admin lifecycle all follow the parent. Every
consumer that special-cases a protected key also carries a ``parent_key`` branch.
"""


def require_app_enabled(app_key: str) -> Callable[[AsyncSession], Awaitable[None]]:
    """Build a route-level dependency that gates requests by app state.

    Used as ``dependencies=[Depends(require_app_enabled(<key>))]`` on each
    non-protected app's router at mount time. Raises
    :class:`app.core.exceptions.HTTPServiceUnavailableException` (HTTP 503) when
    the app is not *effectively* enabled -- that is, when its own
    :class:`app.extensions.models.AppState` is disabled **or** any app it declares in
    ``requires_apps`` is disabled. Resolution is delegated to
    :meth:`AppRegistry.resolve_effective_enabled`, the single source of truth
    shared with the sidebar filter and the ``GET /api/apps`` projection, so the
    gate is passed the app's ``key`` (not ``state_key``) to resolve dependencies.

    The factory closure-captures ``app_key`` at router-mount time; the returned
    coroutine is invoked per request and queries the DB via the standard
    :data:`SessionDep` dependency. Callers must not invoke this factory for keys
    in :data:`PROTECTED_APP_KEYS` -- the mount loops enforce that skip.

    A failed app-state read degrades to ENABLED rather than failing the request:
    a configured plugin stays reachable when the DB is unreachable (or the
    ``appstate`` table is missing). This is fail-open by design: the gate is an
    operator convenience, not a security boundary, so a transient DB fault must
    not 500 every guarded route.

    :param app_key: The plugin module key to gate on.
    :return: A FastAPI dependency coroutine that raises 503 when disabled.
    """

    async def _gate(session: SessionDep) -> None:
        from app.extensions.apps.framework.registry import (  # noqa: PLC0415 - framework's __init__ imports this (cycle)
            get_app_registry,
        )

        try:
            states = await AppStateManager.all_lifecycle_states(session)
        except SQLAlchemyError:
            logger.warning(
                "Could not read app state for '%s'; allowing the request.",
                app_key,
                exc_info=True,
            )
            return
        if not get_app_registry().resolve_effective_enabled(app_key, states):
            raise HTTPServiceUnavailableException(
                detail=f"App '{app_key}' is currently disabled.",
            )

    return _gate


def get_toggleable_app_key(app_key: str) -> str:
    """Resolve a toggleable, configured app key.

    :param app_key: The plugin key from the path parameter.
    :return: The validated app key.
    :raises HTTPConflictException: If the key is protected, or a child app whose
        state is managed by its parent -- neither can be toggled independently.
    :raises HTTPNotFoundException: If the key is not in configured plugins.
    """
    if app_key in PROTECTED_APP_KEYS:
        raise HTTPConflictException(
            detail=f"App '{app_key}' is protected and cannot be disabled.",
        )
    from app.extensions.apps.framework.registry import (  # noqa: PLC0415 - framework's __init__ imports this (cycle)
        get_app_registry,
    )

    app = get_app_registry().get(app_key)
    if app is None:
        raise HTTPNotFoundException(detail="App not found")
    if app.parent_key is not None:
        raise HTTPConflictException(
            detail=f"App '{app_key}' is managed by its parent "
            f"'{app.parent_key}' and cannot be toggled independently.",
        )
    return app_key


ToggleableAppKeyDep = Annotated[str, Depends(get_toggleable_app_key)]


def render_footer_text() -> str:
    """Render the sidebar footer text from the live ``FOOTER_TEMPLATE`` setting.

    Read :attr:`extensions_settings.FOOTER_TEMPLATE` per call (it is a hot,
    materializer-backed setting) so a live ``EXTENSIONS__FOOTER_TEMPLATE`` override is
    reflected without restarting the application.

    :return: The rendered footer text (application summary and version by default).
    """
    return extensions_settings.FOOTER_TEMPLATE.safe_substitute(
        version=__version__, summary=__summary__
    )


async def get_inventory_client(request: Request) -> AsyncGenerator[RemoteAPI]:
    """Construct a ``RemoteAPI`` instance for interacting with the Inventory API.

    The client is held for the whole request, so a hot endpoint override that
    retires it mid-response leaves this request's client open until the response
    has been sent.

    :param request: The HTTP request object.
    :return: An instance of ``RemoteAPI`` configured for the Inventory service,
        including the endpoint, API key, and SSL settings.
    """
    client = getattr(
        request.app.state, "inventory_api", None
    ) or await settings.get_remote_api(
        endpoint=extensions_settings.INVENTORY_ENDPOINT,
        ssl_cafile=settings.SSL_CAFILE,
        ssl_keyfile=inventory_settings.SSL_KEYFILE,
        ssl_certfile=inventory_settings.SSL_CERTFILE,
        logger_name="inventory_api",
    )
    async with client.hold():
        yield client


InventoryClient = Annotated[RemoteAPI, Depends(get_inventory_client)]


# TODO(yan): Proper SDK
# SEP-130
async def get_inventory_api(
    inventory_client: InventoryClient, user: ApiCurrentUser
) -> AsyncGenerator[RemoteAPI]:
    """Construct a `RemoteAPI` instance for interacting with the Inventory API.

    :param inventory_client: The Inventory API client.
    :param user: The current authenticated user, from which the access token is
        extracted.
    :return: An instance of ``RemoteAPI`` configured for the Inventory service,
        including the endpoint, API key, and SSL settings.
    """
    with inventory_client.auth(user.access_token) as authenticated_api:
        yield authenticated_api


InventoryAPI = Annotated[RemoteAPI, Depends(get_inventory_api)]


async def get_tasks_client(request: Request) -> AsyncGenerator[RemoteAPI]:
    """Construct a ``RemoteAPI`` instance for interacting with the Tasks API.

    The client is held for the whole request, so a hot endpoint override that
    retires it mid-response leaves this request's client open until the response
    has been sent: long enough for a live log stream or a file download to run
    to completion on it.

    :param request: The HTTP request object.
    :return: An instance of ``RemoteAPI`` configured for the Tasks service,
        including the endpoint, API key, and SSL settings.
    """
    client = getattr(
        request.app.state, "tasks_api", None
    ) or await settings.get_remote_api(
        endpoint=extensions_settings.TASKS_ENDPOINT,
        ssl_cafile=settings.SSL_CAFILE,
        ssl_keyfile=tasks_settings.SSL_KEYFILE,
        ssl_certfile=tasks_settings.SSL_CERTFILE,
        logger_name="tasks_api",
    )
    async with client.hold():
        yield client


TasksClient = Annotated[RemoteAPI, Depends(get_tasks_client)]


async def get_tasks_api(
    tasks_client: TasksClient, user: ApiCurrentUser
) -> AsyncGenerator[RemoteAPI]:
    """Construct a `RemoteAPI` instance for interacting with the Tasks API.

    :param tasks_client: The Tasks API client.
    :param user: The current authenticated user, from which the access token is
        extracted.
    :return: An instance of `RemoteAPI` configured for the Tasks service, including
        the endpoint, API key, and SSL settings.
    """
    with tasks_client.auth(user.access_token) as authenticated_api:
        yield authenticated_api


TaskAPI = Annotated[RemoteAPI, Depends(get_tasks_api)]


async def resolve_pmm_api() -> PMMRemoteAPI | None:
    """Return a ``PMMRemoteAPI`` client, or ``None`` when PMM is not configured.

    Construct the PMM Extensions wide PMM client from settings, sitting alongside the
    sibling Inventory / Tasks client deps so core Extensions code never reaches into a
    plugin for it. A caller with a request scope takes :func:`get_pmm_api`,
    which wraps this in a request-scoped hold; a caller with no request to scope
    a hold to takes the client directly and relies on the per-call hold.

    :return: The PMM API client, or ``None`` if endpoint or API key is missing.
    """
    if not settings.PMM.endpoint or not settings.PMM.api_key:
        return None
    return await settings.get_remote_api(
        PMMRemoteAPI,
        endpoint=settings.PMM.endpoint,
        api_key=settings.PMM.api_key,
        verify_ssl=settings.PMM.verify_ssl,
        ssl_cafile=settings.SSL_CAFILE,
    )


async def get_pmm_api() -> AsyncGenerator[PMMRemoteAPI | None]:
    """Hold the shared PMM client for the duration of the request.

    A hot ``PMM`` override retires the cached client; holding it here keeps this
    request's client open until the response has been sent.

    :return: The PMM API client, or ``None`` if PMM is not configured.
    """
    client = await resolve_pmm_api()
    if client is None:
        yield None
        return
    async with client.hold():
        yield client


PMMAPIDep = Annotated[PMMRemoteAPI | None, Depends(get_pmm_api)]


async def require_pmm_api(pmm_api: PMMAPIDep) -> PMMRemoteAPI:
    """Return the PMM API client or raise if PMM is not configured.

    :param pmm_api: The PMM API client dependency, or ``None`` if PMM is not
        configured.
    :return: The PMM API client.
    :raises HTTPServiceUnavailableException: If PMM is not configured.
    """
    if pmm_api is None:
        raise HTTPServiceUnavailableException(detail="PMM is not configured")
    return pmm_api


RequiredPMMAPIDep = Annotated[PMMRemoteAPI, Depends(require_pmm_api)]


@overload
async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: Literal[SyncInventoryEntityTypeEnum.NODE],
    entity_id: int,
    **filters: Any,
) -> CreatedNode: ...


@overload
async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: Literal[SyncInventoryEntityTypeEnum.SERVICE],
    entity_id: int,
    **filters: Any,
) -> CreatedService: ...


@overload
async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: Literal[SyncInventoryEntityTypeEnum.SCHEMA],
    entity_id: int,
    **filters: Any,
) -> CreatedSchema: ...


@overload
async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: Literal[SyncInventoryEntityTypeEnum.TABLE],
    entity_id: int,
    **filters: Any,
) -> CreatedTable: ...


@overload
async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: SyncInventoryEntityTypeEnum,
    entity_id: int,
    **filters: Any,
) -> CreatedEntity: ...


async def get_created_entity(
    inventory_api: InventoryAPI,
    entity_type: SyncInventoryEntityTypeEnum,
    entity_id: int,
    **filters: Any,
) -> CreatedEntity:
    """Retrieve a created entity instance based on the given entity type and ID.

    The overloads restate ``ENTITY_MAPPING``'s entity-type to model pairing, so a
    caller naming one type gets that model rather than the whole union.

    Fetches the entity data from the Inventory API and validates it into a
    `CreatedEntityBase` model. Additional filters might be passed for extra validation.

    :param inventory_api: The API client used to interact with the inventory service.
    :param entity_type: The type of the entity to retrieve.
    :param entity_id: The ID of the entity to retrieve.
    :param filters: Fields filters to check for the retrieved entity.
    :return: The validated `CreatedEntity` instance.
    :raises ValueError: If one of the optional filters fail.
    """
    entity_path, entity_model = ENTITY_MAPPING[entity_type]
    entity_data = as_json_object(await inventory_api.get(f"{entity_path}/{entity_id}"))
    entity = entity_model.model_validate(entity_data)
    for field, expected_value in filters.items():
        if (value := entity_data.get(field)) != expected_value:
            raise ValueError(
                f"{field} is not valid for {entity_type.name.lower()} {entity_id} ({value})"
            )
    return entity


async def get_created_node(inventory_api: InventoryAPI, node_id: int) -> CreatedNode:
    """Retrieve a CreatedNode instance based on the given node ID.

    Fetches the node data from the Inventory API and validates it into a `CreatedNode`
    model.

    :param inventory_api: The API client used to interact with the inventory service.
    :type inventory_api: InventoryAPI
    :param node_id: The ID of the node to retrieve.
    :type node_id: int
    :return: The validated `CreatedNode` instance.
    :rtype: CreatedNode
    """
    return await get_created_entity(
        inventory_api, SyncInventoryEntityTypeEnum.NODE, node_id
    )


async def get_created_service(
    inventory_api: InventoryAPI,
    service_id: int,
) -> CreatedService:
    """Retrieve a CreatedService instance based on the given service ID.

    Fetches the service data from the Inventory API and validates it into a
    `CreatedService` model.

    :param inventory_api: The API client used to interact with the inventory service.
    :type inventory_api: InventoryAPI
    :param service_id: The ID of the service to retrieve.
    :type service_id: int
    :return: The validated `CreatedService` instance.
    :rtype: CreatedService
    """
    return await get_created_entity(
        inventory_api, SyncInventoryEntityTypeEnum.SERVICE, service_id
    )


CreatedServiceDep = Annotated[CreatedService, Depends(get_created_service)]


async def get_created_schema(
    inventory_api: InventoryAPI,
    schema_id: int,
) -> CreatedSchema:
    """Retrieve a CreatedSchema instance based on the given schema ID.

    Fetches the schema data from the Inventory API and validates it into a
    `CreatedSchema` model.

    :param inventory_api: The API client used to interact with the inventory service.
    :type inventory_api: InventoryAPI
    :param schema_id: The ID of the schema to retrieve.
    :type schema_id: int
    :return: The validated `CreatedSchema` instance.
    :rtype: CreatedSchema
    """
    schema = await get_created_entity(
        inventory_api, SyncInventoryEntityTypeEnum.SCHEMA, schema_id
    )

    if schema.service and schema.service.node_id and not schema.service.node:
        node = await get_created_entity(
            inventory_api, SyncInventoryEntityTypeEnum.NODE, schema.service.node_id
        )
        schema.service.node = node
    return schema


async def get_created_table(inventory_api: InventoryAPI, table_id: int) -> CreatedTable:
    """Retrieve a CreatedTable instance based on the given table ID.

    Fetches the table data from the Inventory API and validates it into a `CreatedTable`
    model.

    :param inventory_api: The API client used to interact with the inventory service.
    :type inventory_api: InventoryAPI
    :param table_id: The ID of the table to retrieve.
    :type table_id: int
    :return: The validated `CreatedTable` instance.
    :rtype: CreatedTable
    """
    return await get_created_entity(
        inventory_api, SyncInventoryEntityTypeEnum.TABLE, table_id
    )


class ExecutorHostsContext:
    """Wrap executor hosts with inventory-based display name mapping.

    :param hosts: Raw executor hosts dictionary mapping nomad names to addresses.
    :type hosts: dict[str, str]
    :param display_names: Mapping from addresses to inventory node names.
    :type display_names: dict[str, str]
    """

    def __init__(self, hosts: dict[str, str], display_names: dict[str, str]) -> None:
        self._hosts = hosts
        self._display_names = display_names

    @property
    def hosts(self) -> dict[str, str]:
        """Return the raw hosts dictionary.

        :return: Dictionary mapping nomad names to addresses.
        :rtype: dict[str, str]
        """
        return self._hosts

    def display_name(self, nomad_name: str) -> str:
        """Return the inventory display name for a nomad host, with fallback.

        :param nomad_name: The nomad node name to look up.
        :type nomad_name: str
        :return: The inventory node name if found, otherwise the nomad name.
        :rtype: str
        """
        address = self._hosts.get(nomad_name)
        if address and address in self._display_names:
            return self._display_names[address]
        return nomad_name

    def as_template_list(self) -> list[dict[str, str]]:
        """Return a sorted list of value/label dicts for template rendering.

        :return: List of dicts with `value` and `label` keys, sorted by value.
        :rtype: list[dict[str, str]]
        """
        return sorted(
            ({"value": name, "label": self.display_name(name)} for name in self._hosts),
            key=lambda item: item["value"],
        )

    def as_form_hosts(self) -> frozenset[tuple[str, str]]:
        """Return a frozenset of (value, label) tuples for cached snippet forms.

        :return: Frozenset of tuples suitable for snippet form caching.
        :rtype: frozenset[tuple[str, str]]
        """
        return frozenset((name, self.display_name(name)) for name in self._hosts)

    def as_host_metrics(self) -> list[tuple[str, str]]:
        """Return a sorted list of (display_name, address) tuples for host metrics.

        :return: List of tuples with display name and address, sorted by display name.
        :rtype: list[tuple[str, str]]
        """
        return sorted(
            (self.display_name(name), address) for name, address in self._hosts.items()
        )

    def with_host(self, hostname: str) -> "ExecutorHostsContext":
        """Return a new context with the additional host included.

        :param hostname: The hostname to add to the context.
        :type hostname: str
        :return: A new context with the host added, or self if already present.
        :rtype: ExecutorHostsContext
        """
        if hostname in self._hosts:
            return self
        new_hosts = {**self._hosts, hostname: ""}
        return ExecutorHostsContext(hosts=new_hosts, display_names=self._display_names)


async def get_executor_hosts_context(
    executor_hosts: "ExecutorHosts",
    inventory_api: InventoryAPI,
) -> ExecutorHostsContext:
    """Build an enriched executor hosts context with inventory display names.

    :param executor_hosts: The raw executor hosts dictionary.
    :type executor_hosts: dict[str, str]
    :param inventory_api: The Inventory API client.
    :type inventory_api: RemoteAPI
    :return: An executor hosts context with display name mapping.
    :rtype: ExecutorHostsContext
    """
    try:
        nodes = await fetch_all_dict_items(
            lambda pagination: inventory_api.get(
                "/nodes/", params=pagination.model_dump()
            )
        )
        display_names = {node["address"]: node["name"] for node in nodes}
    except (HTTPException, TypeError, KeyError, OSError):
        logger.warning(
            "Failed to fetch inventory nodes for display names", exc_info=True
        )
        display_names = {}
    return ExecutorHostsContext(hosts=executor_hosts, display_names=display_names)


ExecutorHostsCtx = Annotated[ExecutorHostsContext, Depends(get_executor_hosts_context)]


async def get_executor_hosts(tasks_api: TaskAPI) -> dict[str, str]:
    """Retrieve executor hosts from the Tasks API.

    An upstream failure degrades to an empty mapping rather than propagating, so
    a Tasks-service outage leaves the consuming surfaces renderable.

    :param tasks_api: The API client used to interact with the tasks service.
    :return: A dictionary of executor hosts.
    """
    try:
        return as_json_object(await tasks_api.get("/hosts/"))
    except HTTPException:
        logger.warning(
            "Could not read executor hosts from the Tasks API.", exc_info=True
        )
    return {}


ExecutorHosts = Annotated[dict[str, str], Depends(get_executor_hosts)]


_PATH_UNSAFE_CHARACTERS = frozenset("/?#%:;")
"""Characters that let a task name restructure an outbound request URL."""

_PATH_UNSAFE_NAMES = frozenset({"", ".", ".."})
"""Names that resolve to a different upstream path rather than a task."""

_PATH_UNSAFE_CONTROL_CHARACTERS = frozenset(chr(code) for code in (*range(0x20), 0x7F))
"""Characters that would be dropped from a composed path, or do not belong in one."""

_PATH_UNSAFE_LEADING_CHARACTERS = frozenset(" ")
"""Characters stripped from the front of a relative reference, so only there.

``urlsplit`` left-strips the C0 range *and* the space; the C0 range is refused
outright above, which leaves the space, and a space anywhere else in a name
survives composition percent-encoded.
"""


def require_one_path_segment(task_name: str) -> None:
    """Refuse a task name that is not exactly one URL path segment.

    A task name is composed into an outbound path, and
    :meth:`~app.core.requests.remote_api.BaseRemoteAPI.prepare_path` resolves that
    with ``urljoin``, which interprets ``/``, ``?``, ``#``, ``:``, ``;`` and
    dot-segments rather than encoding them: a leading ``/`` makes the composed path
    ``//host/…``, which yarl reads as an absolute URL, so the request leaves the
    Tasks API for another host carrying the caller's bearer token. Against an
    endpoint whose ``base_path`` is not ``/``, ``prepare_path`` strips the leading
    slash first, so a ``:`` is read as a URI scheme and reaches ``urljoin`` as a
    scheme-relative reference. ``;`` opens the parameters of the last segment,
    which ``urlunparse`` drops again when they are empty, so ``"foo;"`` composes the
    path of ``foo``. An empty name composes the collection endpoint instead of a
    task. ``%`` is refused with them because a percent-escape re-encodes any of the
    above past this check. None of these are escapable at that layer, and a task
    named with one is unreachable through those endpoints anyway, so the name is
    refused here.

    A control character is refused for the opposite reason: ``urlsplit`` deletes
    tab, carriage return and newline outright rather than encoding or rejecting
    them, so a name carrying one composes the path of the shorter name left once
    it is gone, and the request succeeds against that other task instead of
    failing. The whole C0 range and ``DEL`` go with them; none belongs in a name.
    A *leading* space is refused for that reason too: ``urlsplit`` left-strips it
    from the relative reference ``prepare_path`` builds against a non-root
    ``base_path``, so ``" foo"`` composes the path of ``foo`` there.

    :param task_name: The task name about to be composed into a request path.
    :raises HTTPUnprocessableEntityException: If the name is empty, a dot-segment,
        or carries a character that would restructure the outbound path or be
        dropped from it.
    """
    if (
        task_name in _PATH_UNSAFE_NAMES
        or _PATH_UNSAFE_CHARACTERS.intersection(task_name)
        or _PATH_UNSAFE_CONTROL_CHARACTERS.intersection(task_name)
        or task_name[:1] in _PATH_UNSAFE_LEADING_CHARACTERS
    ):
        raise HTTPUnprocessableEntityException(
            "The task name must be a single plain path segment."
        )


def task_path(task_name: str, suffix: str = "") -> str:
    """Return the guarded outbound Tasks API path for ``task_name``.

    Composing a name through here rather than through an f-string keeps
    :func:`require_one_path_segment` impossible to forget at a new call site.
    Call the check directly only where a name is validated without being composed
    yet — a create that must refuse a name before it POSTs anything it would then
    have to roll back.

    :param task_name: The task name to compose into the path.
    :param suffix: Trailing path appended after the name, e.g. ``"/history/"``.
    :return: The path to pass to a Tasks API client method.
    :raises HTTPUnprocessableEntityException: If the name is not a single plain
        path segment.
    """
    require_one_path_segment(task_name)
    return f"/{task_name}{suffix}"


# TODO(yan): Put get_task in a proper TasksAPI SDK class
# SEP-130
async def get_task_by_name(
    tasks_api: TaskAPI, task_name: str, owner: str | None = None
) -> Task:
    """Fetch and validate a task by name.

    This function retrieves a task by its name from the Tasks API and validates
    that it is owned by the specified owner (if any). If the task does not exist or is
    not owned by the specified owner, it raises a 404 HTTP exception.

    :param tasks_api: The TaskAPI instance used to make requests to the task service.
    :param task_name: The name of the task to retrieve.
    :param owner: The owner filter for retrieving tasks. Defaults to ``None``, meaning
        no filter.
    :return: The retrieved task.
    :raises HTTPUnprocessableEntityException: If ``task_name`` is not a single
        plain URL path segment.
    :raises HTTPNotFoundException: If the task is not found or is not owned by the
        specified owner.
    """
    try:
        task = Task.model_validate(await tasks_api.get(task_path(task_name)))
    except ValidationError:
        raise HTTPNotFoundException from None
    if owner is not None and owner != task.owner:
        raise HTTPNotFoundException
    return task


# TODO(yan): Put get_task_history in a proper TasksAPI SDK class
# SEP-130
async def get_task_history(
    tasks_api: TaskAPI, task_history_id: int
) -> TaskHistoryResponse:
    """Fetch and validate a task history by ID.

    Retrieve a task history by its ID from the Tasks API. A task history is
    readable by any authenticated user; ``Task.owner`` is an app namespace, not
    a user identity, and is not used as an access filter. Per-execution
    attribution lives on ``executed_by``. If the task history does not exist,
    raise a 404 HTTP exception.

    :param tasks_api: The TaskAPI instance used to make requests to the task service.
    :param task_history_id: The ID of the task history to retrieve.
    :return: The retrieved task history.
    :raises HTTPNotFoundException: If the task history is not found.
    """
    try:
        task_history = TaskHistoryResponse.model_validate(
            await tasks_api.get(f"/history/{task_history_id}")
        )
    except ValidationError:
        logger.debug("ValidationError retrieving task history.", exc_info=True)
        raise HTTPNotFoundException from None
    logger.debug("TASK IS %s", task_history)
    return task_history


async def check_for_conflicted_running_tasks(
    task_name: str, tasks_api: TaskAPI
) -> None:
    """Refuse a task that already has a running or pending execution.

    :param task_name: The name of the task to check for.
    :param tasks_api: The TaskAPI instance used to make requests to the task service.
    :raises HTTPUnprocessableEntityException: If ``task_name`` is not a single
        plain URL path segment.
    :raises HTTPConflictException: If there are running or pending tasks with the same
        name.
    """
    history_path = task_path(task_name, "/history/")
    response = as_json_object(
        await tasks_api.get(
            history_path, params={"status": TaskHistoryStatusEnum.RUNNING}
        )
    )
    running_tasks = response["items"]
    response = as_json_object(
        await tasks_api.get(
            history_path, params={"status": TaskHistoryStatusEnum.PENDING}
        )
    )
    pending_tasks = response["items"]
    if running_tasks or pending_tasks:
        raise HTTPConflictException("Task is already running or pending.")


HasNoConflictedRunningTasks = Depends(check_for_conflicted_running_tasks)


async def check_group_for_conflicted_running_tasks(
    task_names: Sequence[str], tasks_api: TaskAPI
) -> None:
    """Raise if any task in a group has a running or pending run.

    Backup and restore groups run on their derived/child legs, not on the parent
    config task, so gating an edit on the parent name alone lets an edit slip
    through mid-run. Check every group member.

    :param task_names: The parent and derived/child leg names to inspect.
    :param tasks_api: The TaskAPI instance used to make requests to the task service.
    :raises HTTPConflictException: If any named task has a running or pending run.
    """
    for name in task_names:
        await check_for_conflicted_running_tasks(name, tasks_api)


def reject_if_protected(task: Task, *, action: str = "edit") -> Task:
    """Return the task unchanged or raise 409 when it is protected.

    Shared protected-task check for task-based plugins. Composable inline by
    dependencies that must run earlier gates (for example ``alters`` resolving a
    satellite path to its parent) before rejecting protected tasks.

    :param task: The resolved task to gate.
    :param action: The action verb for the 409 detail (``"edit"`` or ``"delete"``).
    :raises HTTPConflictException: If the task is marked as protected.
    :return: The unprotected task.
    """
    if task.protected:
        raise HTTPConflictException(f"Cannot {action} a protected task.")
    return task


def protected_task_guard(
    task_dep: Callable[..., Awaitable[Task]],
    *,
    action: str = "edit",
) -> Callable[..., Awaitable[Task]]:
    """Build a dependency that rejects protected tasks with HTTP 409.

    Parameterized on the plugin's task-fetch dependency and the action verb so a
    single helper serves every task plugin. Attach the returned callable to a
    derived PUT via ``update_guard`` or expose it as an ``Annotated`` type alias.

    :param task_dep: The plugin's task-fetch dependency used to resolve the task.
    :param action: The action verb for the 409 detail (``"edit"`` or ``"delete"``).
    :return: A FastAPI dependency that returns the task or raises 409.
    """

    async def _guard(task: Annotated[Task, Depends(task_dep)]) -> Task:
        return reject_if_protected(task, action=action)

    return _guard


def make_conflict_guard(
    task_dep: Callable[..., Awaitable[Task]],
) -> Callable[..., Awaitable[None]]:
    """Build a dependency that 409s when the resolved task has a running run.

    Parameterized on the plugin's task-fetch dependency so the conflict check
    resolves off the fetched ``task.name`` rather than a fixed ``task_name`` path
    parameter, keeping it decoupled from the route's detail path parameter. Shares
    the cached ``task_dep`` with the protected-task guard and the route handler, so
    the task is fetched once per request.

    :param task_dep: The plugin's task-fetch dependency used to resolve the task.
    :return: A FastAPI dependency that returns ``None`` or raises 409 when a
        running or pending run exists for the resolved task.
    """

    async def _guard(
        task: Annotated[Task, Depends(task_dep)],
        tasks_api: TaskAPI,
    ) -> None:
        await check_for_conflicted_running_tasks(task.name, tasks_api)

    return _guard
