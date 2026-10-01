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

"""Define Inventory routes."""

import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, status

from app import __summary__, __version__
from app.api.deps import IsAuthenticatedDep, RequireMinimumRoleForUnsafeMethods
from app.core.config import create_app, default_lifespan, settings
from app.core.health import build_health_router
from app.core.settings_override.constants import INVENTORY_SETTINGS
from app.core.settings_override.lifecycle import (
    ProxyEntry,
    settings_override_refresher,
)
from app.inventory.config import inventory_settings, InventorySettings
from app.inventory.crud import NodeManager, SchemaManager, ServiceManager, TableManager
from app.inventory.db import get_async_session_maker
from app.inventory.deps import SessionDep
from app.inventory.routes import collection, nodes, schemas, services, tables
from app.inventory.settings.routes import router as settings_router

logger = logging.getLogger(__name__)

summary_router = APIRouter(prefix="/summary", tags=["summary"])


@summary_router.get("/", dependencies=[IsAuthenticatedDep])
async def get_summary_inventory(session: SessionDep) -> dict[str, int]:
    """Retrieve a summary of inventory counts."""
    nodes = await NodeManager.count(session=session)
    services = await ServiceManager.count(session=session)
    schemas = await SchemaManager.count(session=session)
    tables = await TableManager.count(session=session)
    return {
        "nodes": nodes,
        "services": services,
        "schemas": schemas,
        "tables": tables,
    }


@asynccontextmanager
async def inventory_overrides_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Run the ``INVENTORY_SETTINGS`` override refresher for the lifespan.

    Kept separate from :func:`inventory_lifespan` so the combined ``app.main:app``
    can enter it without :func:`default_lifespan` -- ``tasks_lifespan`` already
    enters that, and double-entering re-enters the shared ``settings.CASDOOR`` /
    client registry. Starlette's ``Mount`` never forwards ``lifespan`` scope to a
    mounted sub-app, so the refresher must be entered from whichever app is served.

    :param app: The FastAPI application instance whose lifespan this manages.
    :yield: None
    """
    async with settings_override_refresher(
        get_async_session_maker,
        {
            INVENTORY_SETTINGS: ProxyEntry(inventory_settings, InventorySettings),
        },
        callbacks={},
    ):
        yield


@asynccontextmanager
async def inventory_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the Inventory API's lifespan when it runs standalone.

    Wraps :func:`inventory_overrides_lifespan` with :func:`default_lifespan`.
    Under the combined ``app.main:app`` the refresher is entered from
    :func:`app.main.main_lifespan` and ``default_lifespan`` from
    :func:`app.tasks.main.tasks_lifespan` instead.

    :param app: The FastAPI application instance whose lifespan this manages.
    :yield: None
    """
    async with inventory_overrides_lifespan(app), default_lifespan(app):
        yield


lifespan = inventory_lifespan
inventory_app = create_app(
    build_health_router(get_async_session_maker),
    summary_router,
    nodes.router,
    services.router,
    schemas.router,
    tables.router,
    collection.router,
    settings_router,
    dependencies=[RequireMinimumRoleForUnsafeMethods],
    lifespan=lifespan,
    backend_cors_origins=inventory_settings.BACKEND_CORS_ORIGINS,
    allowed_hosts=inventory_settings.ALLOWED_HOSTS,
    security_headers=inventory_settings.SECURITY_HEADERS,
    title="PMM Extensions Inventory API",
    version=__version__,
    description=f"{__summary__} — inventory (nodes, services, schemas, tables).",
)
# The settings-API handlers read the rebind registry from ``request.app.state``;
# publish an empty one (no HOT field yet) so inline PATCH/DELETE don't KeyError.
inventory_app.state.override_callbacks = {}


@inventory_app.exception_handler(status.HTTP_500_INTERNAL_SERVER_ERROR)
async def internal_error_handler(
    _: Request,
    exc: BaseException,
) -> None:
    """Proper log unhandled exceptions."""
    logger.exception("Unhandled exception:", exc_info=exc)
    raise exc


if __name__ == "__main__":
    logging.config.dictConfig(settings.LOGGING_CONFIG)

    import uvicorn

    uvicorn.run(
        "app.inventory.main:inventory_app",
        host=inventory_settings.UVICORN_HOST,
        port=inventory_settings.UVICORN_PORT,
        ssl_keyfile=inventory_settings.SSL_KEYFILE,
        ssl_certfile=inventory_settings.SSL_CERTFILE,
        log_config=settings.LOGGING_CONFIG,
        reload=inventory_settings.UVICORN_RELOAD,
        reload_dirs=[
            str(settings.BASE_DIR),
            str(settings.BASE_DIR / "app"),
            *inventory_settings.UVICORN_EXTRA_RELOAD_DIRS,
        ],
        reload_includes=[
            f"{settings.BASE_DIR.name}/settings.yaml",
            *inventory_settings.UVICORN_EXTRA_RELOAD_INCLUDES,
        ],
        reload_excludes=[
            f"{settings.BASE_DIR.name}/*.py",
            *inventory_settings.UVICORN_EXTRA_RELOAD_EXCLUDES,
        ],
    )
