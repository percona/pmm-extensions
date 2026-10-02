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

"""Define PMM Extensions routes."""

import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __summary__, __version__
from app.api.main import api_router as top_level_api_router
from app.core.auth import config as auth_config
from app.core.auth.exceptions import BaseAuthProviderException
from app.core.auth.utils import get_user_model
from app.core.celery.utils import init_periodic_tasks_db
from app.core.config import create_app, default_lifespan, settings
from app.core.exceptions import HTTPBadGatewayException, HTTPServiceUnavailableException
from app.core.health import build_health_router
from app.core.requests import RemoteAPI
from app.core.requests.remote_api import PendingCloses
from app.core.settings_override.lifecycle import (
    CallbackRegistry,
    previous_or_base,
    RefreshCallback,
    settings_override_refresher,
    SnapshotChange,
)
from app.core.settings_override.models import SettingClassEnum
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.utils.fields import CredentialHttpUrl
from app.extensions.api.router import api_router
from app.extensions.apps.framework.registry import (
    collect_app_owned_settings_classes,
    get_app_registry,
)
from app.extensions.config import extensions_settings, warn_if_base_url_lacks_root_path
from app.extensions.db import get_async_session_maker
from app.extensions.db.seed import get_system_periodic_tasks, init_extensions_db
from app.extensions.periodic_tasks import sync_app_periodic_task_gating
from app.extensions.routes.artifacts import router as artifacts_router
from app.extensions.settings_override import (
    apply_logging_dictconfig,
    build_extensions_override_proxies,
    invalidate_pmm_clients,
)
from app.extensions.snippets.celery import sync_snippets
from app.extensions.snippets.config import snippets_settings
from app.inventory.config import inventory_settings
from app.tasks.config import tasks_settings

logger = logging.getLogger(__name__)


def warn_if_ambient_sso_inert() -> None:
    """Emit a warning when ambient SSO is enabled but the active provider can't honor it.

    Catch the static (env/YAML) misconfiguration at startup; the admin-UI
    applicability toggle covers the live case. Advisory only -- the runtime
    resolver no-ops regardless.
    """
    if (
        extensions_settings.AMBIENT_SESSION_SSO_ENABLED
        and not auth_config.get_active_auth_provider().supports_ambient_session
    ):
        logger.warning(
            "AMBIENT_SESSION_SSO_ENABLED is on but the active auth provider does "
            "not support ambient sessions; ambient auto-login will not occur."
        )


def warn_if_external_base_lacks_prefix() -> None:
    """Emit a warning when a configured external base URL omits the URL prefix.

    Catch the static (env/YAML) misconfiguration at startup; a hot
    ``SNIPPETS_BASE_URL`` override is checked where the URL is built. Advisory
    only: startup continues on a mismatch.
    """
    warn_if_base_url_lacks_root_path(settings.BASE_URL, "BASE_URL")
    warn_if_base_url_lacks_root_path(
        snippets_settings.SNIPPETS_BASE_URL, "SNIPPETS_BASE_URL"
    )


async def extensions_startup() -> None:
    """Define actions to perform on PMM Extensions startup.

    Initialize the PMM Extensions periodic task database, trigger the initial snippets
    synchronization if configured, warn when ambient SSO is enabled under a
    provider that cannot honor it, and warn when a configured external base URL
    omits the URL prefix PMM Extensions is served under.
    """
    await init_extensions_db()
    if snippets_settings.SYNC_ON_STARTUP:
        sync_snippets.delay()
    warn_if_ambient_sso_inert()
    warn_if_external_base_lacks_prefix()


def _make_remote_api_rebinder(
    app: FastAPI,
    name: str,
    proxy: OverridableSettingsProxy,
    key: Literal["INVENTORY_ENDPOINT", "TASKS_ENDPOINT"],
    *,
    pending: PendingCloses | None = None,
    **ssl: Any,
) -> RefreshCallback:
    """Build a rebind callback for an ``app.state`` RemoteAPI endpoint override.

    The returned callback handles both deployment shapes. Under standalone
    ``extensions_lifespan`` the client lives in ``app.state.<name>``: it is rebuilt on
    the new endpoint and the old one retired. Under the combined ``app.main:app``
    no ``app.state`` client exists — ``get_*_client`` falls back to the
    registry-cached ``get_remote_api`` per request, which already key-misses to
    the new HOT endpoint, so the callback evicts the ordered de-duplicated set
    of previous-and-current endpoints (covering endpoint moves as well as
    same-endpoint credential/SSL changes). When ``key`` is absent from
    ``change.previous`` (override created), :func:`previous_or_base` supplies
    the YAML/env value from the proxy's wrapped instance.

    Both shapes retire the outgoing client rather than closing it outright: the
    swap and the eviction stop it being handed to new work, and it closes once
    the consumers still holding it (an open log stream, a running download)
    release. App-state retirements are registered on ``pending`` *before* the
    replacement is published, so :func:`extensions_lifespan` can force-close
    them at shutdown even on the idle path (where
    :meth:`~app.core.requests.remote_api.BaseRemoteAPI.close_when_idle` would
    otherwise skip pending) or if this callback is cancelled mid-close. When
    ``pending`` is already sealed (teardown has begun while the override
    refresher is still nested above the close ``finally``), the replacement is
    closed and discarded instead of published — sealing alone only forces the
    *outgoing* client; a post-teardown ``setattr`` would leak the new session.
    Discarded replacements are :meth:`~app.core.requests.remote_api.BaseRemoteAPI.track_pending_close`
    registered before that close so a failed or cancelled discard stays
    visible to the shutdown sweep.

    :param app: The FastAPI application whose ``state`` holds the client.
    :param name: The ``app.state`` attribute name (``inventory_api`` /
        ``tasks_api``).
    :param proxy: The overridable settings proxy that owns the endpoint field.
    :param key: The top-level snapshot key for the endpoint field.
    :param pending: Owner-scoped deferred-close collection for app-state
        retirements, or ``None`` when the caller does not track them.
    :param ssl: SSL keyword arguments forwarded to :class:`RemoteAPI` (not HOT,
        captured once at wiring time).
    :return: The rebind callback.
    """

    async def _rebind(change: SnapshotChange) -> None:
        new_endpoint = cast(CredentialHttpUrl, getattr(proxy, key))
        old = getattr(app.state, name, None)
        if old is None:
            previous_endpoint = previous_or_base(change, proxy, key)
            for endpoint in dict.fromkeys(
                str(ep) for ep in (previous_endpoint, new_endpoint) if ep is not None
            ):
                await settings.invalidate_client(endpoint)
            return
        try:
            new_api = await RemoteAPI(endpoint=new_endpoint, **ssl).open()
        except Exception:
            logger.exception("Failed to rebind %s; keeping previous client", name)
            return
        # Check after the await: teardown may have sealed while we were opening.
        if pending is not None and pending.sealed:
            # Track before close so a failed/cancelled discard stays visible
            # to the shutdown sweep.
            new_api.track_pending_close(pending)
            await new_api.close()
            return
        # Register before publishing so shutdown can find the old client even
        # on the idle path, or if this task is cancelled mid-close.
        if pending is not None and not old.remember_pending_close(pending):
            new_api.track_pending_close(pending)
            await new_api.close()
            return
        setattr(app.state, name, new_api)
        await old.close_when_idle(pending=pending)

    return _rebind


async def _reseed_system_periodic_tasks(_: SnapshotChange) -> None:
    """Re-seed the PMM Extensions beat schedule after a hot interval override.

    Wired for ``SnippetsSettings.SYNC_INTERVAL`` (``extensions__sync_snippets``),
    ``AlertsSettings.BACKUP_INTERVAL`` (``extensions__backup_alert_config``),
    ``InventoryAppSettings.COLLECTION_INTERVAL`` (``extensions__inventory_collection``)
    and ``OmInventorySettings.ENABLED``/``SCHEDULE`` (``extensions__run_om_probe``), each
    of which the rebuild seeds or drops as the interval is set or cleared (or, for
    ``OmInventorySettings``, as PMM's OpenManager switch turns the sweep on or off
    without touching the configured cadence), since the app's schedule thunk
    contributes nothing while disabled or unset. Rebuilds the system periodic-task
    set via
    :func:`app.extensions.db.seed.get_system_periodic_tasks` -- which re-reads the now-live
    interval from the refreshed proxy snapshot -- and re-invokes
    :func:`app.core.celery.utils.init_periodic_tasks_db` under the ``extensions__`` prefix.
    The seeding is idempotent (get-or-create plus upsert by task name); its update
    path reassigns only ``task`` / ``schedule_model`` / extra kwargs, so the
    ``enabled`` gating state written by
    :func:`app.extensions.periodic_tasks.sync_app_periodic_task_gating` is preserved.
    Updating the ``IntervalSchedule`` bumps ``PeriodicTaskChanged.last_update``, so
    Celery beat reloads the schedule on its next scheduler tick without a restart.

    Gating is then re-applied, because preserving it is only true of the **update**
    path. A schedule an app may set to ``None``, which is how an app-owned sweep is
    turned off and one of two ways ``OmInventorySettings`` turns off the estate
    probe (the other being ``ENABLED``), contributes no task at all while it is null,
    and the orphan cleanup in
    ``init_periodic_tasks_db`` deletes its row. Setting it again takes the *create*
    path, which builds a fresh row at the model's default ``enabled``, so a disabled
    app would start running on the next beat tick. This is the same pair
    :func:`app.extensions.db.seed.init_extensions_db` runs at startup, for the same reason.

    :param _: The override snapshots on either side of the republish (unused; the
        interval is re-read from the proxy by the task-set builder).
    """
    system_tasks = get_system_periodic_tasks()
    await init_periodic_tasks_db(system_tasks, "extensions__")
    await sync_app_periodic_task_gating(system_tasks)


@asynccontextmanager
async def extensions_overrides_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Wire the PMM Extensions side settings override refresher into a lifespan.

    Start the background refresher for the duration of the wrapped block over
    the proxy map :func:`build_extensions_override_proxies` composes, the shared set
    every PMM Extensions process refreshes, so no wiring drifts from another's.
    Endpoint and PMM rebind callbacks are built here -- where ``app`` is
    available -- so both run modes wire them.

    This is extracted from :func:`extensions_lifespan` because ``extensions_app`` is mounted
    under the top-level ``app`` via Starlette's ``Mount``, which only forwards
    ``http``/``websocket`` scopes -- never ``lifespan``. Without calling this
    context manager from :func:`app.main.main_lifespan`, the PMM Extensions refresher
    would never run when ``python -m app.main`` serves ``app.main:app``.

    The two call sites are mutually exclusive at runtime: uvicorn serves
    either ``app.main:app`` (in which case ``main_lifespan`` enters this
    block) or ``app.extensions.main:extensions_app`` standalone (in which case
    ``extensions_lifespan`` enters it). The refresher therefore starts exactly once.

    The app-owned half of the callback registry is collected from the
    activation list, so this function names no app package: the PMM-embedded
    side-car image strips every deactivated app, and an entry spelled by
    importing one cannot resolve there. ``collect_app_owned_settings_classes``
    imports every activated app module, so it runs at call time. Hoisting it
    to this module's scope would relocate the app-tree import to import time.

    :param app: The FastAPI application instance, used to wire endpoint rebind
        callbacks against ``app.state``.
    :return: None
    :raises TypeError: Propagates from ``collect_app_owned_settings_classes``
        when an app's ``APP_OWNED_SETTINGS_CLASSES`` declaration is malformed.
    :raises ValueError: Propagates from ``collect_app_owned_settings_classes``
        when an activated app's declaration is invalid; that function
        enumerates the cases.
    """
    pending = PendingCloses()
    app.state.retired_remote_apis = pending
    callbacks: CallbackRegistry = {
        (entry.setting_class, key): _reseed_system_periodic_tasks
        for entry in collect_app_owned_settings_classes()
        for key in entry.reseed_keys
    }
    callbacks.update(
        {
            (
                SettingClassEnum.EXTENSIONS_SETTINGS,
                "INVENTORY_ENDPOINT",
            ): _make_remote_api_rebinder(
                app,
                "inventory_api",
                extensions_settings,
                "INVENTORY_ENDPOINT",
                pending=pending,
                ssl_cafile=settings.SSL_CAFILE,
                ssl_keyfile=inventory_settings.SSL_KEYFILE,
                ssl_certfile=inventory_settings.SSL_CERTFILE,
            ),
            (
                SettingClassEnum.EXTENSIONS_SETTINGS,
                "TASKS_ENDPOINT",
            ): _make_remote_api_rebinder(
                app,
                "tasks_api",
                extensions_settings,
                "TASKS_ENDPOINT",
                pending=pending,
                ssl_cafile=settings.SSL_CAFILE,
                ssl_keyfile=tasks_settings.SSL_KEYFILE,
                ssl_certfile=tasks_settings.SSL_CERTFILE,
            ),
            (SettingClassEnum.SETTINGS, "PMM"): invalidate_pmm_clients,
            (SettingClassEnum.SETTINGS, "LOGGING"): apply_logging_dictconfig,
            (
                SettingClassEnum.SNIPPETS_SETTINGS,
                "SYNC_INTERVAL",
            ): _reseed_system_periodic_tasks,
            (
                SettingClassEnum.EXTENSIONS_SETTINGS,
                "APP_DRAIN",
            ): _reseed_system_periodic_tasks,
        }
    )
    # On ``extensions_app``'s state, not the lifespan's parent ``app``: requests to
    # ``/api/extensions/...`` resolve ``request.app`` to the mounted ``extensions_app``, where
    # the settings-API handlers read it.
    extensions_app.state.override_callbacks = callbacks
    async with settings_override_refresher(
        get_async_session_maker,
        build_extensions_override_proxies(),
        callbacks=callbacks,
    ):
        yield


async def _close_app_state_remote_apis(app: FastAPI) -> None:
    """Seal and close active plus retired ``app.state`` RemoteAPI clients.

    Nested ``finally`` so a failure closing one client cannot skip the others
    or the deferred :meth:`~app.core.requests.remote_api.PendingCloses.force_close`
    sweep. Seal before any await so a concurrent rebind cannot register after
    the sweep or publish a replacement into a slot teardown already owns.
    """
    app.state.retired_remote_apis.seal()
    try:
        await app.state.tasks_api.__aexit__(None, None, None)
    finally:
        try:
            await app.state.inventory_api.__aexit__(None, None, None)
        finally:
            await app.state.retired_remote_apis.force_close()


@asynccontextmanager
async def extensions_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage PMM Extensions' lifespan.

    Initializes the PMM Extensions periodic task database and the RemoteAPI clients for
    inventory and tasks services, ensuring they are properly managed during the
    application's startup and shutdown phases. The override refresher publishes
    its initial snapshot *before* the ``app.state`` clients are constructed, so
    they read the effective (override-aware) endpoint. The initial refresh
    fires only callbacks marked with :func:`fire_on_boot`, and the endpoint
    rebinders built by ``_make_remote_api_rebinder`` are unmarked, so they
    never dereference not-yet-built ``app.state``. Any callback marked for
    boot must therefore not touch ``app.state``.

    ``extensions_startup()``, which seeds the periodic-task database from each app's
    *current* settings, runs inside the ``async with`` for the same reason:
    an app-owned hot field (e.g. ``OmInventorySettings.ENABLED``) reads its
    class default until the override snapshot's first publish, so seeding
    before that point can seed a sweep as off when a prior run had already
    turned it on, and nothing re-seeds it afterward: the callback that would
    is the one this same initial publish deliberately skips. Running it after
    entry, once that publish has happened, is what makes ``extensions_startup()``
    see the real persisted settings on every restart, not just after the next
    ``PATCH``.

    The clients are closed via ``app.state`` (not via the originals captured
    at startup) on shutdown, so a client a rebind callback swapped in mid-run is
    the one that gets closed. Clients a rebind retired while still held are
    tracked on ``app.state.retired_remote_apis`` and force-closed here if their
    holders never unwound. The override lifespan sits in a ``try`` so its
    exit drains the refresher first; a matching ``finally`` then closes the
    clients even when the body, ``default_lifespan``, or refresher teardown
    raises or is cancelled. Discarded sealed-path replacements stay
    owner-tracked until their close succeeds.

    :param app: The FastAPI application instance.
    :return: ``None``, once the lifespans have been entered.
    """
    clients_opened = False
    try:
        async with extensions_overrides_lifespan(app):
            await extensions_startup()
            app.state.inventory_api = await RemoteAPI(
                endpoint=extensions_settings.INVENTORY_ENDPOINT,
                ssl_cafile=settings.SSL_CAFILE,
                ssl_keyfile=inventory_settings.SSL_KEYFILE,
                ssl_certfile=inventory_settings.SSL_CERTFILE,
            ).open()
            app.state.tasks_api = await RemoteAPI(
                endpoint=extensions_settings.TASKS_ENDPOINT,
                ssl_cafile=settings.SSL_CAFILE,
                ssl_keyfile=tasks_settings.SSL_KEYFILE,
                ssl_certfile=tasks_settings.SSL_CERTFILE,
            ).open()
            clients_opened = True
            async with default_lifespan(app):
                yield
    finally:
        # Refresher drained above (overrides ``__aexit__``); close even when
        # that path raised or was cancelled.
        if clients_opened:
            await _close_app_state_remote_apis(app)


lifespan = extensions_lifespan
extensions_app = create_app(
    build_health_router(get_async_session_maker),
    lifespan=lifespan,
    allowed_hosts=extensions_settings.ALLOWED_HOSTS,
    security_headers=extensions_settings.SECURITY_HEADERS,
    root_path=extensions_settings.ROOT_PATH,
    title="PMM Extensions Web Application API",
    version=__version__,
    description=(
        f"{__summary__}\n\n"
        "Browser-oriented routes (proxies, streams, downloads). "
        "JSON REST APIs for inventory and tasks live on the mounted sub-apps."
    ),
)


if any(app.uses_task_data for app in get_app_registry()):
    from app.extensions.routes.download_files import router as download_files_router
    from app.extensions.routes.execution_events import router as execution_events_router
    from app.extensions.routes.stream_logs import router as stream_logs_router

    extensions_app.include_router(stream_logs_router, prefix="/stream-logs")
    extensions_app.include_router(download_files_router, prefix="/files")
    extensions_app.include_router(execution_events_router, prefix="/execution-events")

extensions_app.include_router(artifacts_router, prefix="/artifacts")

extensions_app.include_router(api_router)
extensions_app.include_router(top_level_api_router, include_in_schema=False)

User = get_user_model()


@extensions_app.exception_handler(status.HTTP_500_INTERNAL_SERVER_ERROR)
async def internal_error_handler(
    request: Request,  # noqa: ARG001
    exc: BaseException,
) -> JSONResponse:
    """Return a JSON error response for unhandled server errors.

    :param request: The incoming request.
    :param exc: The unhandled exception.
    :return: A JSON response carrying a generic 500 detail.
    """
    logger.exception("Unhandled exception:", exc_info=exc)
    return JSONResponse(
        {"detail": "Internal Server Error"},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


@extensions_app.exception_handler(status.HTTP_404_NOT_FOUND)
async def custom_404_handler(
    request: Request,  # noqa: ARG001
    exc: BaseException,
) -> JSONResponse:
    """Return a JSON error response for unmatched routes.

    ``exc`` is a bare :class:`BaseException` because the handler is registered on
    a status code rather than an exception class, so ``detail`` and ``headers``
    are read defensively.

    :param request: The incoming request.
    :param exc: The exception that produced the 404.
    :return: A JSON response with the error detail.
    """
    return JSONResponse(
        {"detail": getattr(exc, "detail", "Not Found")},
        status_code=status.HTTP_404_NOT_FOUND,
        headers=getattr(exc, "headers", None),
    )


@extensions_app.exception_handler(BaseAuthProviderException)
async def auth_provider_exception_handler(
    request: Request,  # noqa: ARG001
    exc: BaseAuthProviderException,
) -> JSONResponse:
    """Return a JSON error response for auth-provider failures.

    :param request: The incoming request.
    :param exc: The auth-provider exception to handle.
    :return: A JSON response with the error detail and status code.
    """
    logger.exception("Error connecting to auth provider:", exc_info=exc)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@extensions_app.exception_handler(HTTPServiceUnavailableException)
@extensions_app.exception_handler(HTTPBadGatewayException)
async def json_exception_handler(
    request: Request,  # noqa: ARG001
    exc: HTTPException,
) -> JSONResponse:
    """Return a JSON error response for server-side gateway exceptions.

    :param request: The incoming request.
    :type request: Request
    :param exc: The HTTP exception to handle.
    :type exc: HTTPException
    :return: A JSON response with the error detail and status code.
    :rtype: JSONResponse
    """
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@extensions_app.exception_handler(HTTPException)
async def default_exception_handler(
    request: Request,  # noqa: ARG001
    exc: HTTPException,
) -> JSONResponse:
    """Return a JSON error response for any otherwise-unhandled HTTP exception.

    :param request: The incoming request.
    :param exc: The HTTP exception to handle.
    :return: A JSON response with the error detail and status code.
    """
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@extensions_app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request,  # noqa: ARG001
    exc: RequestValidationError,
) -> JSONResponse:
    """Return the serialized validator failures as a JSON 422.

    :param request: The incoming request.
    :param exc: The request-validation error to handle.
    :return: A JSON response carrying the encoded error list.
    """
    return JSONResponse(
        {"detail": jsonable_encoder(exc.errors())},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


if __name__ == "__main__":
    logging.config.dictConfig(settings.LOGGING_CONFIG)

    import uvicorn

    uvicorn.run(
        "app.extensions.main:extensions_app",
        host=extensions_settings.UVICORN_HOST,
        port=extensions_settings.UVICORN_PORT,
        proxy_headers=extensions_settings.PROXY_HEADERS,
        ssl_keyfile=extensions_settings.SSL_KEYFILE,
        ssl_certfile=extensions_settings.SSL_CERTFILE,
        log_config=settings.LOGGING_CONFIG,
        reload=extensions_settings.UVICORN_RELOAD,
        reload_dirs=[
            str(settings.BASE_DIR),
            str(settings.BASE_DIR / "app"),
            *extensions_settings.UVICORN_EXTRA_RELOAD_DIRS,
        ],
        reload_includes=[
            f"{settings.BASE_DIR.name}/settings.yaml",
            *extensions_settings.UVICORN_EXTRA_RELOAD_INCLUDES,
        ],
        reload_excludes=[
            f"{settings.BASE_DIR.name}/*.py",
            *extensions_settings.UVICORN_EXTRA_RELOAD_EXCLUDES,
        ],
    )
