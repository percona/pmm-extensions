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

"""Define dependencies for the Inventory plugin."""

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import settings
from app.core.exceptions import HTTPServiceUnavailableException
from app.core.requests import RemoteAPI
from app.core.security import get_internal_token
from app.core.utils import import_var
from app.extensions.apps.inventory.models import SyncRunSummary
from app.extensions.config import extensions_settings, SyncOptions
from app.extensions.deps import InventoryClient, TasksClient
from app.extensions.sync.exceptions import SyncerConfigurationError
from app.extensions.sync.models import BaseSyncer
from app.inventory.config import inventory_settings
from app.tasks.config import tasks_settings

logger = logging.getLogger(__name__)


class InventorySyncTriggerWrite(BaseModel):
    """Carry the optional JSON body for the ad-hoc inventory sync trigger.

    A ``None`` or empty ``syncer`` selects the sync-all path; a non-empty
    string targets a single configured syncer by its fully qualified
    ``"module.ClassName"`` identifier. Unknown fields are rejected with
    HTTP 422 so a typo on the client never silently degrades to sync-all.

    :param syncer: Fully qualified syncer name, or ``None`` for sync-all.
    :type syncer: str | None
    """

    model_config = ConfigDict(extra="forbid")

    syncer: str | None = None


class InventorySyncStatusResponse(BaseModel):
    """Represent the inventory sync status response.

    :param is_running: ``True`` when an inventory-wide sync is currently
        in progress; ``False`` otherwise.
    :param last_runs: The most recently recorded synchronization runs, newest first.
    """

    is_running: bool
    last_runs: list[SyncRunSummary] = []


class AvailableSyncer(BaseModel):
    """Provide template-facing metadata for an available syncer.

    :param name: The fully qualified ``"module.ClassName"`` identifier matching
        ``BaseSyncer.get_name()`` and the value persisted in
        ``SyncInstance.syncer``. Used as the wire identifier in form payloads.
    :type name: str
    :param display_name: The human-readable label rendered in the dropdown
        (the syncer class's short name with any trailing ``Syncer`` suffix
        stripped).
    :type display_name: str
    """

    name: str
    display_name: str


def _get_syncer_qualified_name(syncer: BaseSyncer) -> str:
    """Return the canonical fully qualified identifier for a syncer instance.

    Equivalent to ``BaseSyncer.get_name()`` but implemented as a free function
    so test stubs do not need to expose the ``get_name`` classmethod — any
    Python class exposes ``__module__`` and ``__name__``.

    :param syncer: The syncer instance to identify.
    :type syncer: BaseSyncer
    :return: The fully qualified ``"module.ClassName"`` identifier matching
        ``BaseSyncer.get_name()`` and the value stored in
        ``SyncInstance.syncer``.
    :rtype: str
    """
    cls = type(syncer)
    return f"{cls.__module__}.{cls.__name__}"


def build_available_syncers(
    syncers: list[BaseSyncer],
    can_sync_check: Callable[[BaseSyncer], bool],
) -> list[AvailableSyncer]:
    """Return metadata for the syncers that can handle the current entity.

    Disambiguate display labels when two or more matching syncers share the
    same short class name (e.g. ``app.extensions.sync.syncers.legacy.MySQLSyncer``
    and ``app.extensions.sync.syncers.new.MySQLSyncer``). In that case the colliding
    entries fall back to the fully qualified ``module.ClassName`` label so
    operators can tell the menu items apart; uncolliding entries keep their
    short, stripped display name.

    :param syncers: The configured ``BaseSyncer`` instances resolved by
        ``get_syncers``.
    :type syncers: list[BaseSyncer]
    :param can_sync_check: Callable invoked with each syncer instance that
        returns ``True`` when the syncer can sync the current entity.
    :type can_sync_check: Callable[[BaseSyncer], bool]
    :return: One ``AvailableSyncer`` per matching syncer, in declaration order.
    :rtype: list[AvailableSyncer]
    """
    matching = [syncer for syncer in syncers if can_sync_check(syncer)]
    short_display_names = []
    for syncer in matching:
        short = type(syncer).__name__
        short_display_names.append(short.removesuffix("Syncer") or short)
    display_counts = {}
    for short_display in short_display_names:
        display_counts[short_display] = display_counts.get(short_display, 0) + 1
    available = []
    for syncer, short_display in zip(matching, short_display_names, strict=True):
        qualified = _get_syncer_qualified_name(syncer)
        display_name = qualified if display_counts[short_display] > 1 else short_display
        available.append(
            AvailableSyncer(name=qualified, display_name=display_name),
        )
    return available


def filter_syncers_by_name(
    syncers: list[BaseSyncer],
    syncer_name: str | None,
    can_sync_check: Callable[[BaseSyncer], bool],
) -> list[BaseSyncer]:
    """Return the syncer selection for a sync route invocation.

    Distinguish two modes:

    - **Sync-all** (``syncer_name`` is ``None`` or empty) — return a shallow
      copy of ``syncers`` unchanged, in declaration order, with no capability
      pre-filter. Preserve the existing refresh-and-chain contract of
      ``run_*_sync``: the second and later syncers receive
      ``refresh_at_start=True`` and may become capable only after an earlier
      syncer mutates the entity. Pre-filtering would silently break that
      contract.
    - **Targeted** (``syncer_name`` present) — return the single syncer whose
      fully qualified name matches and whose capability check currently
      returns ``True``. Raise a domain-level ``ValueError`` on an empty match
      set so crafted or stale POSTs do not become silent no-ops when
      ``run_*_sync(*[])`` is called. Callers translate the error at the
      appropriate boundary: HTTP routes catch it and re-raise
      ``HTTPBadRequestException``, while the Celery caller lets it propagate
      as a task failure with a clean traceback.

    :param syncers: The syncers injected by ``SyncersDep``.
    :type syncers: list[BaseSyncer]
    :param syncer_name: The fully qualified syncer name submitted via the
        form (e.g. ``"app.extensions.sync.syncers.pmm.PMMSyncer"``), or ``None`` /
        empty string to select the sync-all path.
    :type syncer_name: str | None
    :param can_sync_check: The entity-type capability check for the current
        request. Only consulted in the targeted path.
    :type can_sync_check: Callable[[BaseSyncer], bool]
    :return: The resolved list of syncers to hand to ``run_*_sync``.
    :rtype: list[BaseSyncer]
    :raises ValueError: In targeted mode, when no configured syncer both
        matches ``syncer_name`` and passes the capability check.
    """
    if not syncer_name:
        return list(syncers)
    matched = [
        syncer
        for syncer in syncers
        if _get_syncer_qualified_name(syncer) == syncer_name and can_sync_check(syncer)
    ]
    if not matched:
        raise ValueError(
            f"Unknown or inapplicable syncer: {syncer_name!r}",
        )
    return matched


def _syncer_init_kwargs(sync_option: SyncOptions) -> dict[str, Any]:
    """Build constructor kwargs from a configured ``SyncOptions`` entry.

    Drops ``None`` leaves so optional nested models are not passed as explicit
    ``None``, which would override a syncer's default factory and fail
    validation.

    :param sync_option: One element from ``extensions_settings.SYNCERS``.
    :type sync_option: SyncOptions
    :return: Keyword arguments for the syncer class constructor.
    :rtype: dict[str, Any]
    """
    return sync_option.model_dump(exclude={"syncer"}, exclude_none=True)


def _build_syncers(inventory_api: RemoteAPI, tasks_api: RemoteAPI) -> list[BaseSyncer]:
    """Construct every configured syncer with the given API clients.

    :param inventory_api: The API client used to interact with the inventory service.
    :param tasks_api: The API client used to interact with the task service.
    :return: A list of initialized ``BaseSyncer`` instances.
    :raises SyncerConfigurationError: When a syncer refuses one of its settings.
    :raises ImportError: When a configured syncer's module cannot be imported.
    :raises AttributeError: When a configured syncer's module carries no such
        attribute. ``StrImportableAttribute`` only checks the module at settings
        load, so ``validate_importable_settings`` in ``app.main`` is what normally
        catches this before a request arrives.
    """
    syncers = []
    for sync_option in extensions_settings.SYNCERS:
        syncer_class = import_var(sync_option.syncer)
        try:
            syncers.append(
                syncer_class(
                    inventory_api=inventory_api,
                    tasks_api=tasks_api,
                    **_syncer_init_kwargs(sync_option),
                ),
            )
        except ValidationError as exc:
            raise SyncerConfigurationError(sync_option.syncer, exc) from exc
    return syncers


def get_syncers(
    inventory_api: InventoryClient, tasks_api: TasksClient
) -> list[BaseSyncer]:
    """Initialize and return a list of BaseSyncer instances based on configuration.

    Import and initialize syncer classes as specified in the PMM Extensions settings, providing
    the necessary API clients and configuration parameters.

    :param inventory_api: The API client used to interact with the inventory service.
    :param tasks_api: The API client used to interact with the task service.
    :return: A list of initialized ``BaseSyncer`` instances.
    :raises HTTPServiceUnavailableException: When a syncer refuses one of its settings.
    """
    try:
        return _build_syncers(inventory_api, tasks_api)
    except SyncerConfigurationError as exc:
        logger.exception("Syncer %s rejected its configuration", exc.syncer)
        raise HTTPServiceUnavailableException(detail=str(exc)) from exc


SyncersDep = Annotated[list[BaseSyncer], Depends(get_syncers)]


def get_inventory_available_syncers(syncers: SyncersDep) -> list[AvailableSyncer]:
    """Return syncers capable of syncing inventory.

    :param syncers: Resolved syncer instances from ``SyncersDep``.
    :type syncers: list[BaseSyncer]
    :return: Filtered list of syncers that pass ``can_sync_inventory``.
    :rtype: list[AvailableSyncer]
    """
    return build_available_syncers(syncers, lambda s: s.can_sync_inventory())


InventoryAvailableSyncersDep = Annotated[
    list[AvailableSyncer], Depends(get_inventory_available_syncers)
]


async def get_inventory_api_standalone() -> RemoteAPI:
    """Construct an Inventory API client from settings, outside request context.

    The request-scoped :func:`app.extensions.deps.get_inventory_client` cannot serve a
    scheduled job — it resolves the shared client off ``request.app.state`` —
    so a standalone caller builds its own from the same settings.

    :return: A client addressing the Inventory API.
    """
    return await settings.get_remote_api(
        endpoint=extensions_settings.INVENTORY_ENDPOINT,
        ssl_cafile=settings.SSL_CAFILE,
        ssl_keyfile=inventory_settings.SSL_KEYFILE,
        ssl_certfile=inventory_settings.SSL_CERTFILE,
        logger_name="inventory_api",
    )


async def get_syncers_standalone() -> list[BaseSyncer]:
    """Initialize syncer instances with API clients constructed from settings.

    Construct ``RemoteAPI`` clients for the inventory and tasks services from
    application settings, then build syncers the same way the request-context
    dependency does. Used by scheduled tasks that run outside of request
    context.

    :return: A list of initialized ``BaseSyncer`` instances.
    :raises SyncerConfigurationError: When a syncer refuses one of its settings; a
        scheduled run fails outright rather than syncing a subset.
    """
    inventory_api = await get_inventory_api_standalone()
    tasks_api = await settings.get_remote_api(
        endpoint=extensions_settings.TASKS_ENDPOINT,
        ssl_cafile=settings.SSL_CAFILE,
        ssl_keyfile=tasks_settings.SSL_KEYFILE,
        ssl_certfile=tasks_settings.SSL_CERTFILE,
        logger_name="tasks_api",
    )
    return _build_syncers(inventory_api, tasks_api)


InternalTokenDep = Annotated[str, Depends(get_internal_token)]
