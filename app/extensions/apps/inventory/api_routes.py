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

"""Define the JSON API router for the Inventory plugin.

Mounted at ``/api/apps/inventory/`` via ``apps_router`` in
``app/extensions/api/router.py``, which supplies the API authentication these routes
rely on.

This is an operator API only — the plugin ships no browser surface, so nothing
here proxies entity reads. Read the catalog from the inventory service itself
at ``/api/inventory/*``, which remains its canonical CRUD surface and the one
the syncers write through.

The router mounts the ad-hoc inventory-sync trigger (``POST /sync/``), the
running-state polling endpoint (``GET /sync/status/``), schedule discovery
(``GET /``), available-syncers (``GET /available-syncers/``), and the
per-service connectivity probe. Periodic-task CRUD remains delegated to
``/api/tasks/periodic/*`` as the single source of truth; this router does not
duplicate that surface.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Response, status
from sqlmodel import col, select

from app.core.exceptions import HTTPBadRequestException
from app.extensions.apps.inventory.connectivity import probe_service_connectivity
from app.extensions.apps.inventory.deps import (
    AvailableSyncer,
    filter_syncers_by_name,
    InternalTokenDep,
    InventoryAvailableSyncersDep,
    InventorySyncStatusResponse,
    InventorySyncTriggerWrite,
    SyncersDep,
)
from app.extensions.apps.inventory.models import PluginTaskResponse, SyncRunSummary
from app.extensions.apps.inventory.sync import run_inventory_sync
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.deps import (
    CreatedServiceDep,
    IsApiAdmin,
    SessionDep,
    TaskAPI,
)
from app.extensions.models import SyncInstance, SyncInventoryEntityTypeEnum, SyncItem
from app.tasks.connectivity.models import ConnectivityCheckResponse
from app.tasks.models import (
    INVENTORY_COLLECTION_TASK_NAME,
    INVENTORY_SYNC_TASK_NAME,
)

router = APIRouter()

# Module-level singleton avoids the B008 lint warning about function calls in
# argument defaults; the optional-body semantics are unchanged.
_OPTIONAL_TRIGGER_BODY = Body(default=None)
_SYNC_RUN_HISTORY_LIMIT = 10


@router.post("/sync/", status_code=status.HTTP_202_ACCEPTED, response_class=Response)
async def inventory_sync_trigger(
    syncers: SyncersDep,
    background_tasks: BackgroundTasks,
    internal_token: InternalTokenDep,
    body: InventorySyncTriggerWrite | None = _OPTIONAL_TRIGGER_BODY,
) -> Response:
    """Schedule an ad-hoc inventory sync as a background task.

    Mirrors the Jinja2 ``POST /inventory/sync/`` handler but accepts an
    optional JSON body ``{"syncer": "<qualified_name>"}``. When ``syncer``
    is absent, ``None``, or empty, every configured syncer runs in
    declaration order; otherwise only the named syncer is forwarded. An
    unknown or inapplicable syncer raises HTTP 400 — never a silent no-op.

    Authentication is enforced by the parent ``api_router``'s
    ``IsApiAuthenticated`` dependency, so this handler needs no auth parameter.

    :param syncers: Configured syncers from ``SyncersDep``.
    :type syncers: SyncersDep
    :param background_tasks: FastAPI's background task scheduler.
    :type background_tasks: BackgroundTasks
    :param internal_token: PMM Extensions internal service token injected by
        ``InternalTokenDep`` and forwarded to the background sync task.
    :param body: Optional trigger body.
    :type body: InventorySyncTriggerWrite | None
    :return: Empty 202 Accepted response.
    :rtype: Response
    :raises HTTPBadRequestException: When ``body.syncer`` is set but does
        not match any configured syncer that can sync inventory.
    """
    syncer_name = body.syncer if body is not None else None
    try:
        selected = filter_syncers_by_name(
            syncers,
            syncer_name,
            lambda syncer: syncer.can_sync_inventory(),
        )
    except ValueError as exc:
        raise HTTPBadRequestException(str(exc)) from exc
    background_tasks.add_task(run_inventory_sync, internal_token, *selected)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/sync/status/")
async def inventory_sync_status(session: SessionDep) -> InventorySyncStatusResponse:
    """Return whether an inventory-wide sync is running, plus recent run outcomes.

    Lets an operator poll a sync they triggered through ``POST /sync/``
    without scraping any rendered page.

    :param session: SQLModel async session.
    :return: The running flag and the most recent runs, newest first.
    """
    is_running = await SyncItemManager.sync_is_running(
        session,
        SyncInventoryEntityTypeEnum.INVENTORY,
    )
    runs = await SyncInstanceManager.list(
        session,
        select(SyncItem.id)
        .where(
            col(SyncItem.sync_instance_id) == col(SyncInstance.id),
            col(SyncItem.entity_type) == SyncInventoryEntityTypeEnum.INVENTORY,
        )
        .exists(),
        order_by=[
            col(SyncInstance.created_at).desc(),
            col(SyncInstance.id).desc(),
        ],
        limit=_SYNC_RUN_HISTORY_LIMIT,
    )
    return InventorySyncStatusResponse(
        is_running=is_running,
        last_runs=[
            SyncRunSummary(
                syncer=run.syncer,
                started_at=run.created_at,
                finished_at=run.updated_at,
                status=run.status,
                snapshot_complete=run.snapshot_complete,
            )
            for run in runs
        ],
    )


@router.get("/")
async def inventory_plugin_tasks() -> list[PluginTaskResponse]:
    """Return the list of periodic task names for the Inventory plugin.

    Hard-coded because the Inventory plugin's periodic tasks are a fixed pair
    (``inventory-sync`` and ``inventory-collection``).

    :return: The plugin's periodic tasks, each with its name and display name.
    """
    return [
        PluginTaskResponse(
            name=INVENTORY_SYNC_TASK_NAME, display_name="Inventory Sync"
        ),
        PluginTaskResponse(
            name=INVENTORY_COLLECTION_TASK_NAME,
            display_name="Inventory Collection",
        ),
    ]


@router.get("/available-syncers/")
async def inventory_available_syncers(
    available_syncers: InventoryAvailableSyncersDep,
) -> list[AvailableSyncer]:
    """Return syncers capable of syncing inventory.

    :param available_syncers: Filtered syncer list from ``InventoryAvailableSyncersDep``.
    :type available_syncers: list[AvailableSyncer]
    :return: Filtered list of syncers that can sync inventory.
    :rtype: list[AvailableSyncer]
    """
    return available_syncers


@router.post(
    "/services/{service_id:int}/check-connectivity/",
    dependencies=[IsApiAdmin],
)
async def inventory_service_check_connectivity(
    service: CreatedServiceDep,
    tasks_api: TaskAPI,
) -> ConnectivityCheckResponse:
    """Run a database connectivity probe for a service from its executor host.

    A probe that ran but could not connect is reported as HTTP 200 with
    ``success=false`` and the upstream message in ``error``; only a probe that
    could not be attempted at all is an error status.

    :param service: The service to probe, resolved from the path id.
    :param tasks_api: Authenticated Tasks ``RemoteAPI`` client.
    :return: The upstream probe result.
    :raises HTTPBadRequestException: When the service cannot be probed —
        unsupported type, missing node or port, or no executor registered for
        the node address.
    :raises HTTPBadGatewayException: When the Tasks API is unreachable or
        returns an unparseable body.
    """
    return await probe_service_connectivity(service, tasks_api)
