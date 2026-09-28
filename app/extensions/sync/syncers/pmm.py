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

"""Implement models and utilities for the PMM Inventory Sync."""

import logging
from collections.abc import Awaitable, Callable, Iterable
from types import TracebackType
from typing import Any, ClassVar, Self, TypeVar

from async_lru import alru_cache

from app.core.config import settings
from app.extensions.clients.pmm import (
    PMMInventorySnapshot,
    PMMRemoteAPI,
    PMMService,
)
from app.extensions.crud import SyncEntityAbsenceManager, SyncInstanceManager
from app.extensions.inventory import (
    CreatedEntity,
    CreatedNode,
    CreatedService,
    Node,
    Service,
)
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.extensions.sync.fields import MissingGraceGenerations
from app.extensions.sync.models import BaseSyncer, claim_identity
from app.inventory.models import SourceEnum

logger = logging.getLogger(__name__)

#: Ties an absent-entity batch to the call that retires it. A plain
#: ``Callable[[CreatedEntity], ...]`` would reject both retirers, whose parameters
#: are the narrower subclasses.
_Retirable = TypeVar("_Retirable", bound=CreatedEntity)


class PMMSyncer(BaseSyncer):
    """Manage synchronization of PMM inventory entities.

    This class extends `BaseSyncer` to handle synchronization operations specific to PMM
    entities such as nodes and services. It interacts with the PMM remote API to
    retrieve, update, and retire inventory data, ensuring that the local inventory is
    consistent with the remote source.

    :cvar SYNC_TO_LIMIT: The highest entity type that can be synchronized.
        Set to `SyncInventoryEntityTypeEnum.SERVICE`.
    :cvar mirrors_entity_levels: The node and service levels, whose own fields
        this syncer compares against PMM and writes back through ``update_node``
        and ``update_service``.
    :cvar reads_retired_entities: The node and service levels, whose incoming
        reports this syncer matches against the local inventory by external id.
    :param inventory_api: The remote API interface for interacting with the inventory
        system.
    :param access_token: The access token used for authenticating with the inventory
        API.
    :param sync_instance: The synchronization instance used to track sync processes.
    :param sync_items: A dictionary mapping tuples of entity type and ID to SyncItem
        objects.
    :param sync_id: The unique identifier for this synchronization.
    :param keepalive_api: Whether to keep the PMMRemoteAPI instance alive after
        synchronization. Defaults to True.
    """

    SYNC_TO_LIMIT: ClassVar[SyncInventoryEntityTypeEnum] = (
        SyncInventoryEntityTypeEnum.SERVICE
    )
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = frozenset(
        {SyncInventoryEntityTypeEnum.NODE, SyncInventoryEntityTypeEnum.SERVICE}
    )
    reads_retired_entities: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
        frozenset(
            {SyncInventoryEntityTypeEnum.NODE, SyncInventoryEntityTypeEnum.SERVICE}
        )
    )
    keepalive_api: bool = True
    missing_grace_generations: MissingGraceGenerations = 2
    _pmm_api: PMMRemoteAPI | None = None
    _generation: PMMInventorySnapshot | None = None

    async def __aenter__(self) -> Self:
        """Enter the asynchronous context manager.

        Overrides from BaseSyncer to also get the PMMRemoteAPI instance from the global
        client registry.

        :return: The `BaseRemoteAPI` instance.
        """
        if getattr(self, "_pmm_api", None) is None:
            self._pmm_api = await settings.get_remote_api(
                PMMRemoteAPI, **settings.PMM.model_dump()
            )
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the asynchronous context manager.

        Overrides from BaseSyncer to also close the PMMRemoteAPI instance if it was not
        set to be kept alive.

        :param exc_type: The type of exception raised, if any.
        :type exc_type: type[BaseException]
        :param exc_val: The exception instance raised, if any.
        :type exc_val: BaseException
        :param exc_tb: The traceback of the exception raised, if any.
        :type exc_tb: TracebackType
        """
        await super().__aexit__(exc_type, exc_val, exc_tb)
        if not self.keepalive_api and self._pmm_api is not None:
            await settings.invalidate_client(str(self._pmm_api.endpoint))
            self._pmm_api = None

    @property
    def pmm_api(self) -> PMMRemoteAPI:
        """Get the PMMRemoteAPI instance, initializing it if necessary.

        :return: The PMMRemoteAPI instance.
        :rtype: PMMRemoteAPI
        :raises ValueError: If the PMM API client has not been initialized.
        """
        if self._pmm_api is None:
            raise ValueError("PMM API client has not been initialized.")
        return self._pmm_api

    @staticmethod
    def _filter_extensions_sync_disabled(item: dict[str, Any]) -> bool:
        """Filter to exclude items (nodes or services) with extensions_sync: disabled label.

        Works for both node and service dicts: nodes use "labels" or "custom_labels",
        services use "custom_labels".

        :param item: The node or service dictionary to check.
        :type item: dict[str, Any]
        :return: True if the item should be included, False if it should be filtered out.
        :rtype: bool
        """
        labels = {**item.get("labels", {}), **item.get("custom_labels", {})}
        return labels.get("extensions_sync") != "disabled"

    @alru_cache
    async def get_inventory_nodes(
        self,
        external_id: str | None = None,
        node_type: str | None = None,
    ) -> list[CreatedNode]:
        """Retrieve PMM inventory nodes.

        Override the base method to fetch nodes from the PMM inventory system by
        always specifying the source to be PMM.

        :param external_id: The external identifier of the node. Defaults to None,
            meaning it won't be used as a filter.
        :type external_id: str | None
        :param node_type: The type of the node (e.g., "generic"). Defaults to None,
            meaning it won't be used as a filter.
        :type node_type: str | None
        :return: A list of retrieved CreatedNode instances.
        :rtype: list[CreatedNode]
        """
        return await super().get_inventory_nodes(external_id, SourceEnum.PMM, node_type)

    async def _fetch_snapshot(self) -> PMMInventorySnapshot:
        """Fetch an inventory snapshot, retrying once on cross-list disagreement.

        Orphaned services mean the node list and the service list were read at
        moments PMM did not agree about. A single re-read settles a transient
        disagreement; a persistent one leaves the generation incomplete.

        :return: The snapshot to reconcile this generation against.
        :raises ValidationError: If an entity fails validation and ``break_on_error``
            is set.
        """
        snapshot = await self.pmm_api.get_inventory_snapshot(
            skip_failed=not self.break_on_error,
            filter_=self._filter_extensions_sync_disabled,
        )
        if snapshot.diagnostics.orphan_service_node_ids:
            logger.warning(
                "PMM node and service lists disagree (orphan node ids: %s); refetching",
                snapshot.diagnostics.orphan_service_node_ids,
            )
            snapshot = await self.pmm_api.get_inventory_snapshot(
                skip_failed=not self.break_on_error,
                filter_=self._filter_extensions_sync_disabled,
            )
        return snapshot

    async def _owns_run(self) -> bool:
        """Check whether this run's SyncInstance has been reclaimed out from under it.

        Every ledger write and every retirement is gated on this: a run reclaimed
        while it worked must perform neither. Under the conditions
        ``SyncInstanceManager.is_still_owned`` documents — which the syncer's own
        ``stale_run_after`` satisfies on PostgreSQL — it also establishes that this is
        the syncer's only run, so a missing-grace counter cannot be spent twice over
        by two runs racing in one moment.

        :return: ``True`` while this run's ``SyncInstance`` is still ``RUNNING``.
        """
        return await SyncInstanceManager.is_still_owned(
            self._session,
            self.sync_instance.id,
        )

    def _generation_is_complete(self) -> bool:
        """Check whether a complete generation is driving this call.

        Only absence needs a complete generation to be believed. An entity the
        fetch *did* return was observed regardless of what else the fetch missed,
        which is why clearing a counter is not gated on this.

        :return: ``True`` only when a generation is in progress and its fetch
            represented the remote inventory faithfully.
        """
        return self._generation is not None and self._generation.diagnostics.is_complete

    async def _retire_absent(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        absent_entities: Iterable[_Retirable],
        retire: Callable[[_Retirable], Awaitable[None]],
        *,
        permitted: bool,
        filtered_external_ids: set[str],
    ) -> None:
        """Advance the missing-grace counter for absent entities and retire the spent.

        An entity excluded by the caller's filter is held without its counter moving:
        an operator exclusion is evidence in neither direction, exactly like an
        incomplete generation. An entity that is *already* retired is skipped
        outright, neither held nor counted: it is in the state this method exists to
        reach, and it has no ``SyncItem`` in this run to close.

        :param entity_type: The type of the absent entities.
        :param absent_entities: The local entities this generation did not report.
        :param retire: The retirement call to make once grace is spent.
        :param permitted: Whether this generation may retire anything.
        :param filtered_external_ids: External IDs excluded by the fetch filter.
        :raises SyncFailError: If holding or retiring an entity fails and
            ``break_on_error`` is set.
        :raises HTTPBadRequestException: If a ledger write hits a database error.
        """
        for created_entity in absent_entities:
            if created_entity.is_retired:
                continue
            excluded = created_entity.external_id in filtered_external_ids
            if not permitted or excluded:
                await self.hold_entity(entity_type, created_entity)
                continue
            missing = await SyncEntityAbsenceManager.record_missing(
                self._session,
                self.get_name(),
                entity_type,
                created_entity.id,
            )
            if missing < self.missing_grace_generations:
                await self.hold_entity(entity_type, created_entity)
                continue
            await retire(created_entity)
            await SyncEntityAbsenceManager.clear(
                self._session,
                self.get_name(),
                entity_type,
                created_entity.id,
            )

    async def perform_inventory_sync(self) -> None:
        """Perform the inventory synchronization process.

        Synchronize the entire inventory by fetching nodes from the PMM API and
        creating or updating corresponding nodes in the local inventory. A node that
        the fetch did not report is retired only once a complete generation has
        reported it absent ``missing_grace_generations`` times in a row. A partial
        read is not evidence that anything disappeared from PMM.

        :raises ValidationError: If an entity fails validation and ``break_on_error``
            is set.
        :raises SyncFailError: If synchronizing an entity fails and ``break_on_error``
            is set.
        :raises HTTPBadRequestException: If a ledger write hits a database error.
        """
        # Keyed by primary key, not external id: a tombstone and the replacement
        # that took its external id both come back from a retired-inclusive read,
        # and an external-id key would let whichever came last shadow the other.
        syncable_nodes: dict[int | None, CreatedNode] = {}
        external_id_to_id: dict[str, int | None] = {}
        for node in await self.get_inventory_nodes():
            syncable_nodes[node.id] = node
            claim_identity(external_id_to_id, node.external_id, node, syncable_nodes)
        logger.debug("Syncable nodes: %s", syncable_nodes)
        snapshot = await self._fetch_snapshot()
        self._generation = snapshot
        self._snapshot_complete = snapshot.diagnostics.is_complete
        try:
            present_ids: list[int | None] = []
            for node in snapshot.nodes:
                matched_id = external_id_to_id.get(node.external_id)
                if (created_node := syncable_nodes.pop(matched_id, None)) is None:
                    logger.debug("Creating new node: %r", node)
                    created_node = CreatedNode.model_validate(
                        await self.inventory_api.post(
                            "/nodes/",
                            json=node.model_dump(exclude={"services"}),
                        ),
                    )
                else:
                    await self._revive_if_retired(
                        SyncInventoryEntityTypeEnum.NODE, created_node
                    )
                present_ids.append(created_node.id)
                await self.sync_node(created_node, node)
            logger.debug("Nodes absent from PMM: %s", syncable_nodes)
            owns_run = await self._owns_run()
            if owns_run:
                await SyncEntityAbsenceManager.clear(
                    self._session,
                    self.get_name(),
                    SyncInventoryEntityTypeEnum.NODE,
                    *present_ids,
                )
            await self._retire_absent(
                SyncInventoryEntityTypeEnum.NODE,
                syncable_nodes.values(),
                self.retire_node,
                permitted=owns_run and self._generation_is_complete(),
                filtered_external_ids=snapshot.diagnostics.filtered_node_ids,
            )
        finally:
            self._generation = None

    async def fetch_node(self, created_node: CreatedNode) -> Node | None:
        """Fetch updated data for a specific node.

        Retrieve the latest information for the specified node from the PMM API.
        Returns None if the node is filtered out (e.g., has extensions_sync: disabled).

        :param created_node: The node instance to fetch updated data for.
        :return: The updated node data, or None if filtered out.
        """
        logger.debug(
            "Fetching node from PMM with external id %s",
            created_node.external_id,
        )
        return await self.pmm_api.get_node(
            created_node.external_id,
            skip_failed_services=not self.break_on_error,
            filter_=self._filter_extensions_sync_disabled,
        )

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,
    ) -> None:
        """Synchronize data for a specific node.

        Update the local inventory node with data from the PMM API and handle associated
        services.

        Services are retired under the same generation gate as nodes. A call that did
        not arrive from a full inventory generation, such as an operator-triggered
        single-node refresh, has no generation to judge absence against, so it is
        upsert-only.

        An incoming service is matched by external id alone, with no natural-key
        fallback: several databases behind one server legally share that server's
        port, so matching on port would attach one service's sync history to
        another's row.

        :param created_node: The local node instance to synchronize.
        :param updated_node: The updated node data fetched from the PMM API.
        :raises SyncFailError: If synchronizing, holding or retiring a service fails
            and ``break_on_error`` is set.
        :raises HTTPBadRequestException: If a ledger write hits a database error.
        """
        await self.update_node(created_node, updated_node)
        external_id_to_id: dict[str, int | None] = {}
        syncable_services: dict[int | None, CreatedService] = {}
        for service in created_node.services:
            syncable_services[service.id] = service
            claim_identity(
                external_id_to_id,
                service.external_id,
                service,
                syncable_services,
            )
        present_ids: list[int | None] = []
        for service in updated_node.services:
            matched_id = external_id_to_id.get(service.external_id)
            if (created_service := syncable_services.pop(matched_id, None)) is None:
                logger.info("Creating new service: %r", service)
                created_service = CreatedService.model_validate(
                    await self.inventory_api.post(
                        f"/nodes/{created_node.id}/services/",
                        json=service.model_dump(exclude={"node_id"}),
                    ),
                )
                created_service.node = created_node.model_copy(update={"services": []})
            else:
                await self._revive_if_retired(
                    SyncInventoryEntityTypeEnum.SERVICE, created_service
                )
            present_ids.append(created_service.id)
            await self.sync_service(created_service, service)
        owns_run = await self._owns_run()
        filtered_service_ids = (
            self._generation.diagnostics.filtered_service_ids
            if self._generation is not None
            else set()
        )
        if owns_run:
            await SyncEntityAbsenceManager.clear(
                self._session,
                self.get_name(),
                SyncInventoryEntityTypeEnum.SERVICE,
                *present_ids,
            )
        await self._retire_absent(
            SyncInventoryEntityTypeEnum.SERVICE,
            syncable_services.values(),
            self.retire_service,
            permitted=owns_run and self._generation_is_complete(),
            filtered_external_ids=filtered_service_ids,
        )

    async def fetch_service(self, created_service: CreatedService) -> PMMService | None:
        """Fetch updated data for a specific service.

        Retrieve the latest information for the specified service from the PMM API.
        Returns None if the service is filtered out (e.g., has extensions_sync: disabled).

        :param created_service: The service instance for which to fetch updated data.
        :return: The updated service data, or None if filtered out.
        """
        logger.debug(
            "Fetching service from PMM with external id %s",
            created_service.external_id,
        )
        return await self.pmm_api.get_service(
            created_service.external_id,
            filter_=self._filter_extensions_sync_disabled,
        )

    async def perform_service_sync(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> None:
        """Synchronize data for a specific service.

        Update the local inventory service with data from the PMM API.

        :param created_service: The local service instance to synchronize.
        :param updated_service: The updated service data fetched from the PMM API.
        :raises TypeError: If ``updated_service`` is not a ``PMMService``.
        """
        if not isinstance(updated_service, PMMService):
            raise TypeError(
                f"PMMSyncer requires a PMMService, got {type(updated_service).__name__}"
            )
        if created_service.node.external_id != updated_service.node_id:
            nodes = await self.get_inventory_nodes(
                external_id=created_service.node.external_id,
            )
            created_service.node_id = nodes[0].id
        await self.update_service(created_service, updated_service)

    @classmethod
    def can_sync_node(cls, node: CreatedNode) -> bool:
        """Determine if a specific node can be synchronized.

        Override the base method to check if the node's source is PMM.

        :param node: The node instance to check.
        :type node: CreatedNode
        :return: `True` if the node can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return super().can_sync_node(node) and node.source == SourceEnum.PMM

    @classmethod
    def can_sync_service(cls, service: CreatedService) -> bool:
        """Determine if a specific service can be synchronized.

        Override the base method to check if the service's node's source is PMM.

        :param service: The service instance to check.
        :type service: CreatedService
        :return: `True` if the service can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return (
            super().can_sync_service(service) and service.node.source == SourceEnum.PMM
        )
