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

"""Define routes for the Tasks API."""

import json
import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from celery.utils.log import get_task_logger
from fastapi import FastAPI, Request, status
from nomad.api.exceptions import BaseNomadException

from app import __summary__, __version__
from app.api.deps import RequireMinimumRoleForUnsafeMethods
from app.core.config import create_app, default_lifespan, settings
from app.core.exceptions import HTTPBadGatewayException, HTTPGoneException
from app.core.health import build_health_router
from app.core.settings_override.constants import ANONYMIZER_SETTINGS, TASKS_SETTINGS
from app.core.settings_override.lifecycle import (
    CallbackRegistry,
    ProxyEntry,
    settings_override_refresher,
    SnapshotChange,
)
from app.tasks.anonymizer.config import anonymizer_settings, AnonymizerSettings
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.connectivity.routes import router as connectivity_router
from app.tasks.db import get_async_session_maker
from app.tasks.db.seed import (
    init_tasks_db,
    verify_taskhistory_execution_request_is_jsonb,
)
from app.tasks.execution.exceptions import TaskDataNotFoundInExecutorError
from app.tasks.execution.nomad_lifecycle import NomadLifecycle
from app.tasks.periodic.routes import router as periodic_router
from app.tasks.routes import router as tasks_router
from app.tasks.settings.routes import router as settings_router

logger = logging.getLogger(__name__)
celery_logger = get_task_logger(__name__)


async def _reconcile_nomad(_: SnapshotChange) -> None:
    """Rebind the live Nomad executor when its override changed.

    Registered as the ``(TASKS_SETTINGS, NOMAD)`` rebind callback by
    :func:`tasks_lifespan`. :meth:`NomadLifecycle.__aexit__` clears the holder
    before the override refresher task is cancelled, so a refresh cycle racing
    shutdown can find ``tasks_app.state.nomad_lifecycle`` already gone; the
    rebind is skipped in that window rather than raising a noisy callback error.

    :param _: The override snapshots on either side of the republish. Unused; the
        holder reads the live ``NOMAD`` config itself when reconciling.
    """
    holder = getattr(tasks_app.state, "nomad_lifecycle", None)
    if holder is not None:
        await holder.reconcile()


#: Rebind callbacks for watched Tasks overrides, fired by both the background
#: refresher and the settings-API handlers; published on ``tasks_app.state`` below.
_OVERRIDE_REBIND_CALLBACKS: CallbackRegistry = {
    (TASKS_SETTINGS, "NOMAD"): _reconcile_nomad,
}


@asynccontextmanager
async def tasks_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the Tasks API's lifespan.

    Initializes the Tasks database data and starts the settings-override
    refresher, the default lifespan, and the :class:`NomadLifecycle` holder that
    owns the live entered ``NomadExecutor``.

    The holder is anchored to the module-level ``tasks_app`` rather than to
    ``app``: when this lifespan runs under the combined ``app.main:app``,
    ``app`` is the *parent* application, but requests routed to the mounted
    ``/api/tasks`` sub-app resolve ``request.app`` to ``tasks_app`` -- so the
    holder must live on ``tasks_app.state`` for ``get_request_executor`` to find
    it in both standalone and mounted deployments.

    :param app: The FastAPI application instance whose lifespan this manages
        (``tasks_app`` standalone, the parent app when mounted).
    :type app: FastAPI
    :return: An async context manager yielding ``None`` for the lifespan duration.
    :rtype: AsyncGenerator[None, None]
    """
    await init_tasks_db()
    await verify_taskhistory_execution_request_is_jsonb()
    async with (
        settings_override_refresher(
            get_async_session_maker,
            # ALERT_SETTINGS is intentionally NOT wired here: ``alert_settings``
            # is a single shared proxy owned by the PMM Extensions refresher. Wiring it here
            # too would, in the combined ``app.main:app`` process, have both
            # refreshers publish into it from their separate databases and clobber
            # each other every cycle.
            # ANONYMIZER_SETTINGS is safe to wire (unlike ALERT above): its proxy
            # is tasks-track-DB-owned, so no cross-refresher clobber. Without it
            # the API process serves stale defaults until an in-process PATCH.
            {
                TASKS_SETTINGS: ProxyEntry(tasks_settings, TasksSettings),
                ANONYMIZER_SETTINGS: ProxyEntry(
                    anonymizer_settings, AnonymizerSettings
                ),
            },
            callbacks=_OVERRIDE_REBIND_CALLBACKS,
        ),
        default_lifespan(app),
        NomadLifecycle(tasks_app),
    ):
        yield


lifespan = tasks_lifespan
tasks_app = create_app(
    build_health_router(get_async_session_maker),
    tasks_router,
    periodic_router,
    connectivity_router,
    settings_router,
    dependencies=[RequireMinimumRoleForUnsafeMethods],
    lifespan=lifespan,
    backend_cors_origins=tasks_settings.BACKEND_CORS_ORIGINS,
    allowed_hosts=tasks_settings.ALLOWED_HOSTS,
    security_headers=tasks_settings.SECURITY_HEADERS,
    title="PMM Extensions Tasks API",
    version=__version__,
    description=f"{__summary__} — task execution, history, periodic jobs, connectivity.",
)
# On the sub-app's state, not the lifespan's parent ``app``: requests to
# ``/api/tasks`` resolve ``request.app`` to ``tasks_app``, where the handlers read it.
tasks_app.state.override_callbacks = _OVERRIDE_REBIND_CALLBACKS


@tasks_app.exception_handler(status.HTTP_500_INTERNAL_SERVER_ERROR)
async def internal_error_handler(
    _: Request,
    exc: BaseException,
) -> None:
    """Proper log unhandled exceptions."""
    logger.exception("Unhandled exception:", exc_info=exc)
    raise exc


def task_data_not_found_detail(exc: TaskDataNotFoundInExecutorError) -> dict[str, Any]:
    """Build structured detail for HTTP 410 from TaskDataNotFoundInExecutorError.

    :param exc: The exception indicating task data was not found in the executor.
    :type exc: TaskDataNotFoundInExecutorError
    :return: A dictionary with message and optional resource_type, resource_id,
        executor_name, job_id, evaluation_id, and detail keys for use in an HTTP 410
        response body.
    :rtype: dict[str, Any]
    """
    detail = {
        "message": "The requested task data is no longer available in the executor.",
    }
    if exc.resource_type is not None:
        detail["resource_type"] = exc.resource_type
    if exc.resource_id is not None:
        detail["resource_id"] = exc.resource_id
    if exc.executor_name is not None:
        detail["executor_name"] = exc.executor_name
    if exc.job_id is not None:
        detail["job_id"] = exc.job_id
    if exc.evaluation_id is not None:
        detail["evaluation_id"] = exc.evaluation_id
    if str(exc):
        detail["detail"] = str(exc)
    return detail


@tasks_app.exception_handler(TaskDataNotFoundInExecutorError)
async def task_data_not_found_handler(
    _: Request,
    exc: TaskDataNotFoundInExecutorError,
) -> None:
    """Handle exceptions raised when task data is not found in the executor."""
    payload = task_data_not_found_detail(exc)
    logger.debug(
        "Task data not found in executor; HTTP %s body: %s",
        status.HTTP_410_GONE,
        json.dumps(payload, default=str),
    )
    raise HTTPGoneException(detail=payload)


@tasks_app.exception_handler(BaseNomadException)
async def nomad_exception_handler(_: Request, exc: BaseNomadException) -> None:
    """Handle exceptions raised by Nomad."""
    logger.exception("Error getting a response from Nomad", exc_info=exc)
    raise HTTPBadGatewayException(
        detail="Failed to get a response from Nomad, make sure the agent is online.",
    )


if __name__ == "__main__":
    logging.config.dictConfig(settings.LOGGING_CONFIG)

    import uvicorn

    uvicorn.run(
        "app.tasks.main:tasks_app",
        host=tasks_settings.UVICORN_HOST,
        port=tasks_settings.UVICORN_PORT,
        ssl_keyfile=tasks_settings.SSL_KEYFILE,
        ssl_certfile=tasks_settings.SSL_CERTFILE,
        log_config=settings.LOGGING_CONFIG,
        reload=tasks_settings.UVICORN_RELOAD,
        reload_dirs=[
            str(settings.BASE_DIR),
            str(settings.BASE_DIR / "app"),
            *tasks_settings.UVICORN_EXTRA_RELOAD_DIRS,
        ],
        reload_includes=[
            f"{settings.BASE_DIR.name}/settings.yaml",
            *tasks_settings.UVICORN_EXTRA_RELOAD_INCLUDES,
        ],
        reload_excludes=[
            f"{settings.BASE_DIR.name}/*.py",
            *tasks_settings.UVICORN_EXTRA_RELOAD_EXCLUDES,
        ],
    )
