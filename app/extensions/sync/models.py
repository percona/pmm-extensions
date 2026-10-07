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

"""Define base sync models for PMM Extensions."""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import cached_property
from types import TracebackType
from typing import Any, ClassVar, NamedTuple, Self, TypeVar
from uuid import uuid4

from aiohttp import ClientError
from async_lru import _LRUCacheWrapper, alru_cache
from fastapi import HTTPException
from pydantic import ConfigDict, Field, model_validator, UUID4, validate_call
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.config import alert_service
from app.core.alerts.models import AlertSeverity
from app.core.models import BaseCaseInsensitiveModel
from app.core.pagination import fetch_all_dict_items
from app.core.requests import as_json_object, RemoteAPI
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.db import get_async_session_maker
from app.extensions.deps import task_path
from app.extensions.inventory import (
    CreatedEntity,
    CreatedEntityBase,
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
    Node,
    Schema,
    Service,
    SourceEnum,
    Table,
)
from app.extensions.models import (
    SyncInstance,
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItem,
    SyncItemWrite,
    SyncStatusEnum,
)
from app.extensions.sync.constants import INVENTORY_PATH_SEGMENTS
from app.extensions.sync.exceptions import (
    ExecutorHostNotFoundError,
    SyncFailError,
    SyncItemAlreadyInProgressError,
)
from app.extensions.sync.fields import (
    StaleRunAfter,
    TaskExecutionTimeout,
    TasksExecutionWaitInterval,
)
from app.extensions.sync.health import SyncHealthReporter
from app.tasks.models import TaskHistoryStatusEnum, TaskLogType

logger = logging.getLogger(__name__)

#: Ties an identity map to the index it points into, so a schema index cannot be
#: handed a table.
_Identifiable = TypeVar("_Identifiable", bound=CreatedEntity)


def claim_identity(
    identities: dict[str, int | None],
    identity: str,
    entity: _Identifiable,
    by_id: dict[int | None, _Identifiable],
) -> None:
    """Point an identity at an entity, letting an active row win.

    An identity — an upstream id, or a name unique within a parent — is unique
    among *active* rows only, so a tombstone and the replacement that took its
    identity both come back from a retired-inclusive read. Matching an incoming
    report against the tombstone would attempt a revive the active row's unique
    key refuses, so the active row claims the identity and the tombstone keeps its
    own entry in ``by_id``, where absence handling still finds it. Without this,
    result ordering would decide.

    :param identities: The identity-to-primary-key map being built.
    :param identity: The identity to claim.
    :param entity: The local entity claiming it.
    :param by_id: The primary-key-keyed index holding every candidate.
    """
    incumbent = by_id.get(identities.get(identity))
    if incumbent is None or incumbent.is_retired:
        identities[identity] = entity.id


def inventory_sync_alert_address(entity: CreatedEntity) -> str | None:
    """Return a host-style address for alerts when the entity or its parents expose one.

    :param entity: The inventory entity that failed to sync.
    :type entity: CreatedEntity
    :return: Address string, or None if it cannot be resolved.
    :rtype: str | None
    """
    if isinstance(entity, CreatedNode | CreatedService):
        return entity.address
    if isinstance(entity, CreatedSchema):
        if entity.service is not None:
            return entity.service.address
        return None
    if isinstance(entity, CreatedTable):
        schema = entity.database
        if schema is not None and schema.service is not None:
            return schema.service.address
        return None
    return None


def inventory_sync_alert_source_id_segment(entity: CreatedEntity) -> str:
    """Return a stable id token for alert ``source`` / ``dedup_key``.

    :param entity: The inventory entity that failed to sync.
    :type entity: CreatedEntity
    :return: Non-empty identifier string.
    :rtype: str
    """
    ext = getattr(entity, "external_id", None)
    if ext not in (None, ""):
        return str(ext)
    if entity.id is not None:
        return str(entity.id)
    return "unknown"


def inventory_sync_alert_summary_scope(
    entity_type: SyncInventoryEntityTypeEnum,
    created_entity: CreatedEntity | None,
) -> tuple[str, str]:
    """Build the alert summary fragment and id segment for routing/dedup.

    :param entity_type: Inventory level being synced.
    :type entity_type: SyncInventoryEntityTypeEnum
    :param created_entity: Entity instance, or None for top-level inventory sync.
    :type created_entity: CreatedEntity | None
    :return: ``(summary_scope, entity_id_repr)`` for the alert payload.
    :rtype: tuple[str, str]
    """
    if created_entity is None:
        if entity_type == SyncInventoryEntityTypeEnum.INVENTORY:
            return "top-level inventory sync", "top_level"
        return f"top-level {entity_type.name.lower()} sync", "top_level"
    entity_id_repr = inventory_sync_alert_source_id_segment(created_entity)
    parts = [
        f"{entity_type.name} id {created_entity.id}",
        f"name={created_entity.name!r}",
    ]
    if addr := inventory_sync_alert_address(created_entity):
        parts.append(f"address={addr!r}")
    ext = getattr(created_entity, "external_id", None)
    if ext not in (None, ""):
        parts.append(f"external_id={ext!r}")
    return ", ".join(parts), entity_id_repr


# TODO: Make it abstract  # noqa: TD002, TD003
class BaseSyncer(BaseCaseInsensitiveModel):
    """Define a base class for syncers in the PMM Extensions app.

    This class serves as a blueprint for all syncer implementations within
    the PMM Extensions application. It provides the foundational structure, including required
    APIs and abstract methods that can be overridden by subclasses.

    :cvar SYNC_TO_LIMIT: The upper limit for entity types that can be synchronized.
    :cvar mirrors_entity_levels: The entity levels whose own fields this syncer
        mirrors, and therefore whose sync-health columns its attempts write. A
        level belongs here only where the syncer compares the entity against its
        source and updates it — a syncer that merely traverses a level to reach
        its children, or that writes a separate observation resource, owns
        nothing there and must not refresh a freshness the mirroring syncer is
        responsible for. Empty by default.
    :cvar reads_retired_entities: The entity levels whose inventory reads include
        retired entities. A level belongs here only if this syncer has a match
        site there — a point where an incoming report is matched against the local
        inventory, so a tombstone can be recognised as reappearing. At any other
        level the opt-in is actively harmful: the level's read feeds an
        unconditional walk, which then drives fetches and writes against entities
        whose upstream is gone and whose parents the inventory's own active-only
        routes reject. The distinction is per level, not per syncer, because a
        syncer can diff at one level and walk at another.
    :param inventory_api: The remote API interface for interacting with the inventory
        system.
    :param sync_instance: The synchronization instance used to track sync processes.
    :param sync_items: A dictionary mapping tuples of entity type and ID to SyncItem
        objects.
    :param sync_id: The unique identifier for this synchronization.
    :param break_on_error: Flag indicating whether to stop synchronization on error.
        Defaults to False.
    :param stale_run_after: The age beyond which an idle in-progress run of this
        syncer is presumed abandoned and reclaimed, unblocking the syncer after a
        worker was killed mid-run. Must exceed the longest expected runtime of a
        sync, or a live run gets reclaimed out from under itself. Defaults to 1 hour.
    :param _session: The asynchronous database session.
    :param _snapshot_complete: Whether this run observed a complete generation of the
        remote inventory, or ``None`` when the syncer does not produce one.
    """

    model_config = ConfigDict(ignored_types=(_LRUCacheWrapper,))
    SYNC_TO_LIMIT: ClassVar[SyncInventoryEntityTypeEnum]
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
        frozenset()
    )
    reads_retired_entities: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
        frozenset()
    )
    inventory_api: RemoteAPI
    sync_instance: SyncInstance | None = None
    sync_items: dict[tuple[SyncInventoryEntityTypeEnum, int | None], SyncItem] = {}
    sync_id: UUID4 = Field(default_factory=uuid4)
    break_on_error: bool = False
    stale_run_after: StaleRunAfter = timedelta(hours=1)
    _session: AsyncSession
    _snapshot_complete: bool | None = None

    def __hash__(self) -> int:
        """Compute the hash based on the synchronization ID.

        :return: The hash value of the syncer instance.
        """
        return hash(self.sync_id)

    async def __aenter__(self) -> Self:
        """Enter the asynchronous context manager.

        Initializes the database session and creates a new SyncInstance if one does not
        exist. A syncer refused the run it asked for releases the session again
        before raising, having entered nothing.

        :return: The syncer instance with an active session and SyncInstance.
        :raises SyncInstanceAlreadyInProgressError: If another run of this syncer
            already owns it.
        """
        session_maker = get_async_session_maker()
        self._session = session_maker()
        self._session = await self._session.__aenter__()
        if self.sync_instance is None:
            try:
                self.sync_instance = await SyncInstanceManager.create(
                    self._session,
                    SyncInstanceWrite(
                        syncer=self.get_name(),
                        status=SyncStatusEnum.RUNNING,
                    ),
                    stale_after=self.stale_run_after,
                    id=self.sync_id,
                )
            except BaseException:
                # Closed here because a failing __aenter__ is never paired with an
                # __aexit__, so nothing else would return the connection until the
                # session is collected.
                await self._session.close()
                raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the asynchronous context manager.

        Marks any hanging SyncItems as failed, records the run-level verdict, and
        closes the database session. The hanging-item sweep runs first because the
        run-level rollup reads the item statuses it writes.

        :param exc_type: The exception type, if any.
        :param exc_val: The exception value, if any.
        :param exc_tb: The traceback, if any.
        """
        await SyncInstanceManager.finish_hanging_items(
            self._session,
            self.sync_instance.id,
        )
        await SyncInstanceManager.finalize_run(
            self._session,
            self.sync_instance.id,
            failed=exc_type is not None,
            snapshot_complete=self._snapshot_complete,
        )
        await self._session.__aexit__(exc_type, exc_val, exc_tb)

    @model_validator(mode="after")
    def set_sync_id_from_sync_instance(self) -> Self:
        """Assign the synchronization ID from the SyncInstance.

        If a SyncInstance exists, its ID is assigned to the sync_id attribute.

        :return: The syncer instance with the updated sync_id.
        """
        if self.sync_instance is not None:
            self.sync_id = self.sync_instance.id
        return self

    @cached_property
    def sync_health(self) -> SyncHealthReporter:
        """Build the reporter for this syncer's API client and mirrored levels.

        :return: The reporter the ``sync_*`` boundaries record through.
        """
        return SyncHealthReporter(self.inventory_api, self.mirrors_entity_levels)

    @cached_property
    def can_sync_mapping(
        self,
    ) -> dict[SyncInventoryEntityTypeEnum, Callable[[Any], bool]]:
        """Map entity types to their corresponding sync permission check methods.

        Each check accepts only its own entity model, so the value type is the
        widest one the heterogeneous table admits — the key is what makes a
        lookup well-typed, and that correlation is not expressible here.

        :return: A dictionary mapping each SyncInventoryEntityTypeEnum to a method that
                 determines if that entity can be synchronized.
        """
        return {
            SyncInventoryEntityTypeEnum.NODE: self.can_sync_node,
            SyncInventoryEntityTypeEnum.SERVICE: self.can_sync_service,
            SyncInventoryEntityTypeEnum.SCHEMA: self.can_sync_schema,
            SyncInventoryEntityTypeEnum.TABLE: self.can_sync_table,
        }

    @asynccontextmanager
    async def api_auth(
        self, api_key: str, auth_scheme: str = "Bearer"
    ) -> AsyncGenerator[Self]:
        """Use a specific API key for inventory API requests within the context.

        This asynchronous context manager temporarily sets the Authorization header
        for the inventory API to use the provided API key. It ensures that all requests
        made within the context use this API key for authentication.

        :param api_key: The API key to be used for authentication.
        :type api_key: str
        :param auth_scheme: The authentication scheme to be used (default is "Bearer").
        :type auth_scheme: str
        :yield: The syncer instance with the updated API key context.
        :rtype: AsyncGenerator[BaseSyncer]
        """
        with self.inventory_api.auth(api_key, auth_scheme):
            async with self as syncer:
                yield syncer

    @validate_call
    async def prepare_sync(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity | None,
    ) -> None:
        """Prepare synchronization for a given entity and its children.

        This method sets up SyncItems for the specified entity and recursively prepares
        synchronization for any child entities if applicable. A retired child is
        skipped together with its subtree, whatever the syncer's ``can_sync_*``
        predicates say about it.

        :param entity_type: The type of the entity to synchronize.
        :param created_entity: The entity instance to synchronize, or None for top-level
            (inventory) synchronization.
        """
        entity_id = None if created_entity is None else created_entity.id
        logger.debug("Preparing sync for %s with ID %s", entity_type.name, entity_id)
        self.sync_items[(entity_type, entity_id)] = await self.get_sync_item(
            entity_type,
            entity_id,
        )
        next_entity_type = entity_type + 1
        if self.can_sync_entity_type(next_entity_type):
            can_sync = self.can_sync_mapping.get(next_entity_type)
            for child in await self.get_children_entities(entity_type, created_entity):
                # Retired-inclusive reads exist for the match sites, where a
                # tombstone can be recognised as reappearing. This walk has nothing
                # to sync for one, so an item opened here would only ever hang.
                if child.is_retired:
                    continue
                if can_sync is not None and can_sync(child):
                    await self.prepare_sync(next_entity_type, child)
        logger.debug(
            "Finished preparing sync for %s with ID %s",
            entity_type.name,
            entity_id,
        )

    async def get_children_entities(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity | None,
    ) -> Sequence[CreatedEntityBase]:
        """Retrieve child entities for a given entity type and entity.

        Depending on the entity type, this method fetches related child entities that
        need to be synchronized.

        :param entity_type: The type of the current entity.
        :param created_entity: The current entity instance, or None for top-level
            synchronization.
        :return: A list of child entities to be synchronized.
        """
        if entity_type == SyncInventoryEntityTypeEnum.INVENTORY:
            return await self.get_inventory_nodes()
        if entity_type == SyncInventoryEntityTypeEnum.SERVICE:
            created_entity = await self.get_inventory_service(created_entity.id)
            return created_entity.children
        if created_entity is not None:
            return created_entity.children
        logger.warning(
            "Unknown entity type %s or missing created_entity: %r",
            entity_type,
            created_entity,
        )
        return []

    async def get_sync_item(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        entity_id: int | None,
    ) -> SyncItem:
        """Retrieve or create a SyncItem for a given entity.

        This method attempts to retrieve an existing SyncItem for the specified entity.
        If none exists, it creates a new SyncItem.

        :param entity_type: The type of the entity.
        :type entity_type: SyncInventoryEntityTypeEnum
        :param entity_id: The unique identifier of the entity, or None for top-level
            (inventory) synchronization.
        :type entity_id: int | None
        :return: The existing or newly created SyncItem.
        :rtype: SyncItem
        """
        sync_item, created = await SyncItemManager.get_or_create(
            self._session,
            SyncItemWrite(
                entity_type=entity_type,
                entity_id=entity_id,
                sync_instance_id=self.sync_instance.id,
            ),
        )
        verb = "Created" if created else "Retrieved"
        logger.debug("%s SyncItem: %s", verb, sync_item)
        return sync_item

    async def get_sync_items(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        *entity_ids: int | None,
    ) -> list[SyncItem]:
        """Retrieve multiple SyncItems for specified entities.

        This method attempts to retrieve or create SyncItems for each provided entity
        ID. If a SyncItem is already in progress for an entity, it logs the exception.

        :param entity_type: The type of the entities.
        :type entity_type: SyncInventoryEntityTypeEnum
        :param entity_ids: The unique identifiers of the entities, or None for top-level
            (inventory) synchronization.
        :type entity_ids: int | None
        :return: A list of SyncItems corresponding to the provided entity IDs.
        :rtype: list[SyncItem]
        """
        sync_items = []
        for entity_id in entity_ids:
            try:
                sync_items.append(await self.get_sync_item(entity_type, entity_id))
            except SyncItemAlreadyInProgressError:
                logger.exception(
                    "Failed to create SyncItem (entity_id: %s, entity_type: %s): "
                    "already in progress",
                    entity_id,
                    entity_type,
                )
        return sync_items

    @asynccontextmanager
    async def manage_sync_item(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity | None,
    ) -> AsyncGenerator[SyncItem, None]:
        """Manage the synchronization lifecycle of a SyncItem.

        This asynchronous context manager handles the synchronization lifecycle of a
        `SyncItem`, including initiating the synchronization process, handling
        exceptions by marking the `SyncItem` as failed, and finalizing the
        synchronization status upon successful completion.

        :param entity_type: The type of the entity to synchronize.
        :type entity_type: SyncInventoryEntityTypeEnum
        :param created_entity: The entity instance to synchronize, or None for top-level
            (inventory) synchronization.
        :type created_entity: CreatedEntity | None
        :yield: The `SyncItem` instance being managed during the synchronization process.
        :rtype: AsyncGenerator[SyncItem, None]
        :raises SyncFailError: If an exception occurs during synchronization.
        """
        entity_id = None if created_entity is None else created_entity.id
        sync_item_entity = (entity_type, entity_id)
        if sync_item_entity not in self.sync_items:
            await self.prepare_sync(entity_type, created_entity)
        sync_item = self.sync_items[sync_item_entity]
        sync_item = await SyncItemManager.start_sync(self._session, sync_item)
        try:
            yield sync_item
        except Exception as exc:
            logger.exception(
                "Failed to sync %s: %r",
                entity_type.name,
                created_entity,
            )
            self.sync_items[sync_item_entity] = await SyncItemManager.fail_sync(
                self._session,
                sync_item,
            )
            syncer_name = self.get_name()
            summary_scope, entity_id_repr = inventory_sync_alert_summary_scope(
                entity_type, created_entity
            )
            alert_data = {
                "summary": (
                    f"Inventory sync failed for {syncer_name} ({summary_scope})."
                ),
                "source": f"{syncer_name}:{entity_type.name}:{entity_id_repr}",
                "severity": AlertSeverity.ERROR,
                "class": "inventory_sync_item_failure",
                "dedup_key": f"{syncer_name}:{entity_type.name}:{entity_id_repr}",
            }
            await alert_service.trigger(alert_data)
            if self.break_on_error:
                raise SyncFailError(
                    entity_type, self.sync_items[sync_item_entity]
                ) from exc
        else:
            await self.finish_sync(entity_type, created_entity)

    async def finish_sync(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity | None,
    ) -> None:
        """Finalize synchronization for a given entity and its children.

        This method marks the SyncItem as finished and recursively finalizes
        synchronization for any child entities.

        :param entity_type: The type of the entity that was synchronized.
        :type entity_type: SyncInventoryEntityTypeEnum
        :param created_entity: The entity instance that was synchronized, or None for
            top-level (inventory) synchronization.
        :type created_entity: CreatedEntity | None
        """
        entity_id = None
        if created_entity is not None:
            entity_id = created_entity.id
            for child in created_entity.children:
                await self.finish_sync(entity_type + 1, child)
        if sync_item := self.sync_items.get((entity_type, entity_id)):
            self.sync_items[
                (entity_type, entity_id)
            ] = await SyncItemManager.finish_sync(
                self._session,
                sync_item,
            )

    def _retirement_params(
        self, entity_type: SyncInventoryEntityTypeEnum
    ) -> dict[str, str]:
        """Return the query parameters opting one read into tombstones.

        :param entity_type: The entity level the read addresses.
        :return: ``{"include_retired": "true"}`` when this syncer has a match site
            at that level, and an empty mapping otherwise.
        """
        return (
            {"include_retired": "true"}
            if entity_type in self.reads_retired_entities
            else {}
        )

    @alru_cache
    async def get_inventory_nodes(
        self,
        external_id: str | None = None,
        source: SourceEnum | None = None,
        node_type: str | None = None,
    ) -> list[CreatedNode]:
        """Retrieve inventory nodes from the remote API.

        This method fetches nodes from the inventory API based on the provided
        filters. Results are cached to optimize performance.

        :param external_id: The external identifier of the node. Defaults to None,
            meaning it won't be used as a filter.
        :type external_id: str | None
        :param source: The source of the node information. Defaults to None, meaning it
            won't be used as a filter
        :type source: SourceEnum | None
        :param node_type: The type of the node (e.g., "generic"). Defaults to None,
            meaning it won't be used as a filter.
        :type node_type: str | None
        :return: A list of retrieved CreatedNode instances.
        :rtype: list[CreatedNode]
        """
        params = {
            "external_id": external_id,
            "source": source,
            "node_type": node_type,
        }
        params = {key: value for key, value in params.items() if value is not None}
        params |= self._retirement_params(SyncInventoryEntityTypeEnum.NODE)
        nodes = await fetch_all_dict_items(
            lambda pagination: self.inventory_api.get(
                "/nodes/", params={**params, **pagination.model_dump()}
            )
        )
        return [CreatedNode.model_validate(node_data) for node_data in nodes]

    @alru_cache
    async def get_inventory_node(self, node_id: int) -> CreatedNode:
        """Retrieve a specific inventory node by its ID.

        This method fetches a single node from the inventory system using its unique ID.
        The result is cached to optimize performance.

        :param node_id: The unique identifier of the node to retrieve.
        :type node_id: int
        :return: The retrieved CreatedNode instance.
        :rtype: CreatedNode
        """
        return CreatedNode.model_validate(
            await self.inventory_api.get(
                f"/nodes/{node_id}",
                params=self._retirement_params(SyncInventoryEntityTypeEnum.NODE),
            )
        )

    @alru_cache
    async def get_inventory_service(self, service_id: int) -> CreatedService:
        """Retrieve a specific inventory service by its ID.

        This method fetches a single service from the inventory system using its unique
        ID. The result is cached to optimize performance.

        :param service_id: The unique identifier of the service to retrieve.
        :type service_id: int
        :return: The retrieved CreatedService instance.
        :rtype: CreatedService
        """
        return CreatedService.model_validate(
            await self.inventory_api.get(
                f"/services/{service_id}",
                params=self._retirement_params(SyncInventoryEntityTypeEnum.SERVICE),
            ),
        )

    @alru_cache
    async def get_inventory_schema(self, schema_id: int) -> CreatedSchema:
        """Retrieve a specific inventory schema by its ID.

        This method fetches a single schema from the inventory system using its unique
        ID. The result is cached to optimize performance.

        :param schema_id: The unique identifier of the schema to retrieve.
        :type schema_id: int
        :return: The retrieved CreatedSchema instance.
        :rtype: CreatedSchema
        """
        return CreatedSchema.model_validate(
            await self.inventory_api.get(
                f"/schemas/{schema_id}",
                params=self._retirement_params(SyncInventoryEntityTypeEnum.SCHEMA),
            ),
        )

    @alru_cache
    async def get_inventory_table(self, table_id: int) -> CreatedTable:
        """Retrieve a specific inventory table by its ID.

        This method fetches a single table from the inventory system using its unique
        ID. The result is cached to optimize performance.

        :param table_id: The unique identifier of the table to retrieve.
        :type table_id: int
        :return: The retrieved CreatedTable instance.
        :rtype: CreatedTable
        """
        return CreatedTable.model_validate(
            await self.inventory_api.get(
                f"/tables/{table_id}",
                params=self._retirement_params(SyncInventoryEntityTypeEnum.TABLE),
            ),
        )

    @alru_cache
    async def get_inventory_service_schemas(
        self,
        service_id: int,
    ) -> list[CreatedSchema]:
        """Retrieve schemas associated with a specific inventory service.

        This method fetches all schemas related to a given service from the inventory
        system. The results are cached to optimize performance.

        :param service_id: The unique identifier of the service whose schemas are to be
            retrieved.
        :type service_id: int
        :return: A list of retrieved CreatedSchema instances.
        :rtype: list[CreatedSchema]
        """
        params = {"include_tables": "true"} | self._retirement_params(
            SyncInventoryEntityTypeEnum.SCHEMA
        )
        schema_data = await fetch_all_dict_items(
            lambda pagination: self.inventory_api.get(
                f"/services/{service_id}/schemas/",
                params={**params, **pagination.model_dump()},
            )
        )
        return [CreatedSchema.model_validate(schema) for schema in schema_data]

    async def hold_entity(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity,
    ) -> None:
        """Close an entity's SyncItem without touching the inventory.

        A held entity is one this run declined to retire. Its ``SyncItem`` was created
        up-front from the local inventory, so leaving it open would let the
        hanging-item sweep fail it and drag the run-level status down on an
        otherwise healthy run.

        :param entity_type: The type of the entity being held.
        :param created_entity: The entity instance being held.
        :raises SyncFailError: If closing the SyncItem fails and ``break_on_error``
            is set.
        """
        logger.info(
            "Holding %s %s: absent, but this generation is not evidence of removal",
            entity_type.name,
            created_entity.id,
        )
        async with self.manage_sync_item(entity_type, created_entity):
            pass

    async def retire_node(self, created_node: CreatedNode) -> None:
        """Retire a node in the inventory system, keeping its row and subtree.

        This method synchronizes the retirement of a node by marking the
        corresponding SyncItem and issuing the retire call to the inventory API.

        :param created_node: The node instance to be retired.
        """
        logger.debug("Retiring node %s in inventory", created_node.id)
        async with self.manage_sync_item(
            SyncInventoryEntityTypeEnum.NODE,
            created_node,
        ):
            await self.inventory_api.delete(f"/nodes/{created_node.id}")

    async def retire_service(self, created_service: CreatedService) -> None:
        """Retire a service in the inventory system, keeping its row and subtree.

        This method synchronizes the retirement of a service by marking the
        corresponding SyncItem and issuing the retire call to the inventory API.

        :param created_service: The service instance to be retired.
        """
        logger.debug("Retiring service %s in inventory", created_service.id)
        async with self.manage_sync_item(
            SyncInventoryEntityTypeEnum.SERVICE,
            created_service,
        ):
            await self.inventory_api.delete(f"/services/{created_service.id}")

    async def retire_schema(self, created_schema: CreatedSchema) -> None:
        """Retire a schema in the inventory system, keeping its row and tables.

        This method synchronizes the retirement of a schema by marking the
        corresponding SyncItem and issuing the retire call to the inventory API.

        :param created_schema: The schema instance to be retired.
        """
        logger.debug("Retiring schema %s in inventory", created_schema.id)
        async with self.manage_sync_item(
            SyncInventoryEntityTypeEnum.SCHEMA,
            created_schema,
        ):
            await self.inventory_api.delete(f"/schemas/{created_schema.id}")

    async def retire_table(self, created_table: CreatedTable) -> None:
        """Retire a table in the inventory system, keeping its row.

        This method synchronizes the retirement of a table by marking the
        corresponding SyncItem and issuing the retire call to the inventory API.

        :param created_table: The table instance to be retired.
        """
        logger.debug("Retiring table %s in inventory", created_table.id)
        async with self.manage_sync_item(
            SyncInventoryEntityTypeEnum.TABLE,
            created_table,
        ):
            await self.inventory_api.delete(f"/tables/{created_table.id}")

    async def _revive_if_retired(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity,
    ) -> None:
        """Revive a tombstoned entity because upstream reported it again.

        Call this only where upstream presence has actually been established —
        where a match against the local inventory succeeded. A hook further up,
        in ``sync_node`` and friends, would fire for every entity a syncer merely
        iterates over, and a syncer that walks the whole inventory would revive
        every tombstone it visits.

        Presence under the same upstream identity is unambiguous, so revival does
        not wait for a complete generation the way retirement does: the
        completeness gate exists to stop retiring on partial evidence, and says
        nothing about an entity that is demonstrably back.

        The in-memory entity is cleared alongside the remote row because the
        ``get_inventory_*`` reads are cached per generation and would otherwise
        keep reporting it retired.

        :param entity_type: The type of the entity that reappeared.
        :param created_entity: The local entity matched against the upstream report.
        :raises HTTPConflictException: If an active entity already holds the unique
            key the revived entity would reclaim. Callers outside a
            ``manage_sync_item`` block propagate it and abort the whole run rather
            than failing the one item.
        """
        if not created_entity.is_retired:
            return
        logger.info(
            "Reviving %s %s: reported again by its source",
            entity_type.name,
            created_entity.id,
        )
        await self.inventory_api.post(
            f"/{INVENTORY_PATH_SEGMENTS[entity_type]}/{created_entity.id}/revive"
        )
        created_entity.retired_at = None

    async def sync_inventory(self) -> None:
        """Synchronize the entire inventory.

        This method initiates the synchronization process for the entire inventory if
        permitted by the synchronization rules. It manages the SyncItem lifecycle and
        delegates the actual synchronization logic to the `perform_inventory_sync`
        method.
        """
        if self.can_sync_inventory():
            logger.info("Starting inventory synchronization (%s)", self.get_name())
            async with self.manage_sync_item(
                SyncInventoryEntityTypeEnum.INVENTORY,
                None,
            ):
                await self.perform_inventory_sync()
            logger.info("Finished inventory synchronization (%s)", self.get_name())

    async def perform_inventory_sync(self) -> None:
        """Perform a full synchronization process.

        Execute the complete synchronization workflow, handling all relevant
        data retrieval, processing, and storage operations. It should be overridden by
        subclasses to implement specific synchronization behavior.

        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".perform_inventory_sync() must be overridden.")

    async def fetch_node(self, created_node: CreatedNode) -> Node | None:
        """Fetch updated data for a specific node.

        Retrieve the latest information for the specified node.
        May return None if the node is filtered out by the implementation.

        :param created_node: The node instance to fetch updated data for.
        :type created_node: CreatedNode
        :return: The updated node data, or None if filtered out.
        :rtype: Node | None
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".fetch_node() must be overridden.")

    async def update_node(
        self,
        created_node: CreatedNode,
        updated_node: Node,
    ) -> CreatedNode:
        """Update a node in the inventory system.

        Send the updated node data to the inventory API and validate the response.

        :param created_node: The node instance to update.
        :type created_node: CreatedNode
        :param updated_node: The updated node data.
        :type updated_node: Node
        :return: The updated `CreatedNode` instance.
        :rtype: CreatedNode
        """
        updated_node_data = updated_node.model_dump(exclude={"services"})
        if updated_node_data.items() <= created_node.model_dump().items():
            logger.info("No changes detected for node %s", created_node.id)
            return created_node
        logger.info("Updating node %s: %r", created_node.id, updated_node)
        return CreatedNode.model_validate(
            await self.inventory_api.put(
                f"/nodes/{created_node.id}",
                json=updated_node.model_dump(exclude={"services"}),
            ),
        )

    async def sync_node(
        self,
        created_node: CreatedNode,
        updated_node: Node | None = None,
        *,
        refresh_at_start: bool = False,
    ) -> None:
        """Synchronize data for a specific node.

        Retrieve and update information related to the node identified by
        `created_node`.

        :param created_node: The node instance to synchronize.
        :type created_node: CreatedNode
        :param updated_node: The updated node data. If None, it will be fetched using
            `.fetch_node()`. Defaults to None.
        :type updated_node: Node | None
        :param refresh_at_start: Whether to refresh the created_node data before
            synchronization. Defaults to `False`.
        :type refresh_at_start: bool
        """
        created_node = (
            await self.get_inventory_node(created_node.id)
            if refresh_at_start
            else created_node
        )
        if self.can_sync_node(created_node):
            logger.info(
                "Starting node synchronization (%s) for node %s",
                self.get_name(),
                created_node.id,
            )
            async with (
                self.manage_sync_item(
                    SyncInventoryEntityTypeEnum.NODE,
                    created_node,
                ),
                self.sync_health.record(
                    SyncInventoryEntityTypeEnum.NODE, created_node
                ) as attempt,
            ):
                updated_node = (
                    await self.fetch_node(created_node)
                    if updated_node is None
                    else updated_node
                )
                if updated_node is None:
                    logger.debug(
                        "Skipping node %s synchronization: node filtered out",
                        created_node.id,
                    )
                    return
                attempt.mark_compared()
                await self.perform_node_sync(created_node, updated_node)
            logger.info(
                "Finished node synchronization (%s) for node %s",
                self.get_name(),
                created_node.id,
            )

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,
    ) -> None:
        """Perform synchronization for a specific node.

        This method contains the logic to synchronize data for the specified node. It
        should be overridden by subclasses to implement specific synchronization
        behavior.

        :param created_node: The node instance to synchronize.
        :type created_node: CreatedNode
        :param updated_node: The updated node data.
        :type updated_node: Node
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".perform_node_sync() must be overridden.")

    async def fetch_service(self, created_service: CreatedService) -> Service | None:
        """Fetch updated data for a specific service.

        Retrieve the latest information for the specified service.
        May return None if the service is filtered out by the implementation.

        :param created_service: The service instance for which to fetch updated data.
        :type created_service: CreatedService
        :return: The updated service data, or None if filtered out.
        :rtype: Service | None
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".fetch_service() must be overridden.")

    async def update_service(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> CreatedService:
        """Update a service in the inventory system.

        Send the updated service data to the inventory API and validate the response.

        :param created_service: The service instance to update.
        :type created_service: CreatedService
        :param updated_service: The updated service data.
        :type updated_service: Service
        :return: The updated `CreatedService` instance.
        :rtype: CreatedService
        """
        updated_service_data = updated_service.model_dump(
            exclude={"schemas", "node_id"}
        )
        if updated_service_data.items() <= created_service.model_dump().items():
            logger.info("No changes detected for service %s", created_service.id)
            return created_service
        logger.info("Updating service %s: %r", created_service.id, updated_service)
        updated_service_data["node_id"] = created_service.node_id
        return CreatedService.model_validate(
            await self.inventory_api.put(
                f"/services/{created_service.id}",
                json=updated_service_data,
            ),
        )

    async def sync_service(
        self,
        created_service: CreatedService,
        updated_service: Service | None = None,
        *,
        refresh_at_start: bool = False,
    ) -> None:
        """Synchronize data for a specific service.

        Retrieve and update information related to the service identified by
        `created_service`.

        :param created_service: The service instance to synchronize.
        :type created_service: CreatedService
        :param updated_service: The updated service data. If None, it will be fetched
            using `.fetch_service()`. Defaults to None.
        :type updated_service: Service | None
        :param refresh_at_start: Whether to refresh the created_service data before
            synchronization. Defaults to `False`.
        :type refresh_at_start: bool
        """
        created_service = (
            await self.get_inventory_service(created_service.id)
            if refresh_at_start
            else created_service
        )
        if self.can_sync_service(created_service):
            logger.info(
                "Starting service synchronization (%s) for service %s",
                self.get_name(),
                created_service.id,
            )
            async with (
                self.manage_sync_item(
                    SyncInventoryEntityTypeEnum.SERVICE,
                    created_service,
                ),
                self.sync_health.record(
                    SyncInventoryEntityTypeEnum.SERVICE, created_service
                ) as attempt,
            ):
                updated_service = (
                    await self.fetch_service(created_service)
                    if updated_service is None
                    else updated_service
                )
                if updated_service is None:
                    logger.debug(
                        "Skipping service %s synchronization: service filtered out",
                        created_service.id,
                    )
                    return
                attempt.mark_compared()
                await self.perform_service_sync(created_service, updated_service)
            logger.info(
                "Finished service synchronization (%s) for service %s",
                self.get_name(),
                created_service.id,
            )

    async def perform_service_sync(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> None:
        """Perform synchronization for a specific service.

        This method contains the logic to synchronize data for the specified service.
        It should be overridden by subclasses to implement specific synchronization
        behavior.

        :param created_service: The service instance to synchronize.
        :type created_service: CreatedService
        :param updated_service: The updated service data.
        :type updated_service: Service
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".perform_service_sync() must be overridden.")

    async def fetch_schema(self, created_schema: CreatedSchema) -> Schema:
        """Fetch updated data for a specific schema.

        Retrieve the latest information for the specified schema.

        :param created_schema: The schema instance for which to fetch updated data.
        :type created_schema: CreatedSchema
        :return: The updated schema data.
        :rtype: Schema
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".fetch_schema() must be overridden.")

    async def update_schema(
        self,
        created_schema: CreatedSchema,
        updated_schema: Schema,
    ) -> CreatedSchema:
        """Update a schema in the inventory system.

        Send the updated schema data to the inventory API and validate the response.

        :param created_schema: The schema instance to update.
        :type created_schema: CreatedSchema
        :param updated_schema: The updated schema data.
        :type updated_schema: Schema
        :return: The updated `CreatedSchema` instance.
        :rtype: CreatedSchema
        """
        updated_schema_data = updated_schema.model_dump(
            exclude={"tables", "service_id"}
        )
        if updated_schema_data.items() <= created_schema.model_dump().items():
            logger.info("No changes detected for schema %s", created_schema.id)
            return created_schema
        logger.info("Updating schema %s: %r", created_schema.id, updated_schema)
        updated_schema_data["service_id"] = created_schema.service_id
        return CreatedSchema.model_validate(
            await self.inventory_api.put(
                f"/schemas/{created_schema.id}",
                json=updated_schema_data,
            ),
        )

    async def sync_schema(
        self,
        created_schema: CreatedSchema,
        updated_schema: Schema | None = None,
        *,
        refresh_at_start: bool = False,
    ) -> None:
        """Synchronize data for a specific schema.

        Retrieve and update information related to the schema identified by
        `created_schema`.

        :param created_schema: The schema instance to synchronize.
        :type created_schema: CreatedSchema
        :param updated_schema: The updated schema data. If None, it will be fetched
            using `.fetch_schema()`. Defaults to None.
        :type updated_schema: Schema | None
        :param refresh_at_start: Whether to refresh the created_schema data before
            synchronization. Defaults to `False`.
        :type refresh_at_start: bool
        """
        created_schema = (
            await self.get_inventory_schema(created_schema.id)
            if refresh_at_start
            else created_schema
        )
        if self.can_sync_schema(created_schema):
            logger.info(
                "Starting schema synchronization (%s) for schema %s",
                self.get_name(),
                created_schema.id,
            )
            async with (
                self.manage_sync_item(
                    SyncInventoryEntityTypeEnum.SCHEMA,
                    created_schema,
                ),
                self.sync_health.record(
                    SyncInventoryEntityTypeEnum.SCHEMA, created_schema
                ) as attempt,
            ):
                updated_schema = (
                    await self.fetch_schema(created_schema)
                    if updated_schema is None
                    else updated_schema
                )
                attempt.mark_compared()
                await self.perform_schema_sync(created_schema, updated_schema)
            logger.info(
                "Finished schema synchronization (%s) for schema %s",
                self.get_name(),
                created_schema.id,
            )

    async def perform_schema_sync(
        self,
        created_schema: CreatedSchema,
        updated_schema: Schema,
    ) -> None:
        """Perform synchronization for a specific schema.

        This method contains the logic to synchronize data for the specified schema.
        It should be overridden by subclasses to implement specific synchronization
        behavior.

        :param created_schema: The schema instance to synchronize.
        :type created_schema: CreatedSchema
        :param updated_schema: The updated schema data.
        :type updated_schema: Schema
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".perform_schema_sync() must be overridden.")

    async def fetch_table(self, created_table: CreatedTable) -> Table:
        """Fetch updated data for a specific table.

        Retrieve the latest information for the specified table.

        :param created_table: The table instance for which to fetch updated data.
        :type created_table: CreatedTable
        :return: The updated table data.
        :rtype: Table
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".fetch_table() must be overridden.")

    async def update_table(
        self,
        created_table: CreatedTable,
        updated_table: Table,
    ) -> CreatedTable:
        """Update a table in the inventory system.

        Send the updated table data to the inventory API and validate the response.

        :param created_table: The table instance to update.
        :type created_table: CreatedTable
        :param updated_table: The updated table data.
        :type updated_table: Table
        :return: The updated `CreatedTable` instance.
        :rtype: CreatedTable
        """
        updated_table_data = updated_table.model_dump(exclude={"schema_id"})
        if updated_table_data.items() <= created_table.model_dump().items():
            logger.info("No changes detected for table %s", created_table.id)
            return created_table
        logger.info("Updating table %s: %r", created_table.id, updated_table)
        updated_table_data["schema_id"] = created_table.schema_id
        return CreatedTable.model_validate(
            await self.inventory_api.put(
                f"/tables/{created_table.id}",
                json=updated_table_data,
            ),
        )

    async def sync_table(
        self,
        created_table: CreatedTable,
        updated_table: Table | None = None,
        *,
        refresh_at_start: bool = False,
    ) -> None:
        """Synchronize data for a specific table.

        Retrieve and update information related to the table identified by
        `created_table`.

        :param created_table: The table instance to synchronize.
        :type created_table: CreatedTable
        :param updated_table: The updated table data. If None, it will be fetched
            using `.fetch_table()`. Defaults to None.
        :type updated_table: Table | None
        :param refresh_at_start: Whether to refresh the created_table data before
            synchronization. Defaults to `False`.
        :type refresh_at_start: bool
        """
        created_table = (
            await self.get_inventory_table(created_table.id)
            if refresh_at_start
            else created_table
        )
        if self.can_sync_table(created_table):
            logger.info(
                "Starting table synchronization (%s) for table %s",
                self.get_name(),
                created_table.id,
            )
            async with (
                self.manage_sync_item(
                    SyncInventoryEntityTypeEnum.TABLE,
                    created_table,
                ),
                self.sync_health.record(
                    SyncInventoryEntityTypeEnum.TABLE, created_table
                ) as attempt,
            ):
                updated_table = (
                    await self.fetch_table(created_table)
                    if updated_table is None
                    else updated_table
                )
                attempt.mark_compared()
                await self.perform_table_sync(created_table, updated_table)
            logger.info(
                "Finished table synchronization (%s) for table %s",
                self.get_name(),
                created_table.id,
            )

    async def perform_table_sync(
        self,
        created_table: CreatedTable,
        updated_table: Table,
    ) -> None:
        """Perform synchronization for a specific table.

        This method contains the logic to synchronize data for the specified table.
        It should be overridden by subclasses to implement specific synchronization
        behavior.

        :param created_table: The table instance to synchronize.
        :type created_table: CreatedTable
        :param updated_table: The updated table data.
        :type updated_table: Table
        :raises NotImplementedError: If the method is not overridden by the subclass.
        """
        raise NotImplementedError(".perform_table_sync() must be overridden.")

    @classmethod
    def get_name(cls) -> str:
        """Compute the fully qualified name of the synchronizer.

        :return: The synchronizer's name in the format "module.ClassName".
        :rtype: str
        """
        return f"{cls.__module__}.{cls.__name__}"

    @classmethod
    def can_sync_entity_type(cls, entity_type: SyncInventoryEntityTypeEnum) -> bool:
        """Determine if a given entity type can be synchronized.

        Checks if the provided entity type is within the permissible synchronization
        limit.

        :param entity_type: The type of the entity to check.
        :type entity_type: SyncInventoryEntityTypeEnum
        :return: `True` if the entity type can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return entity_type <= cls.SYNC_TO_LIMIT

    @classmethod
    def can_sync_inventory(cls) -> bool:
        """Determine if the inventory can be synchronized.

        :return: `True` if inventory synchronization is allowed, `False` otherwise.
        :rtype: bool
        """
        return cls.can_sync_entity_type(SyncInventoryEntityTypeEnum.INVENTORY)

    @classmethod
    def can_sync_node(cls, node: CreatedNode) -> bool:  # noqa: ARG003
        """Determine if a specific node can be synchronized.

        :param node: The node instance to check.
        :type node: CreatedNode
        :return: `True` if the node can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return cls.can_sync_entity_type(SyncInventoryEntityTypeEnum.NODE)

    @classmethod
    def can_sync_service(cls, service: CreatedService) -> bool:  # noqa: ARG003
        """Determine if a specific service can be synchronized.

        :param service: The service instance to check.
        :type service: CreatedService
        :return: `True` if the service can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return cls.can_sync_entity_type(SyncInventoryEntityTypeEnum.SERVICE)

    @classmethod
    def can_sync_schema(cls, schema: CreatedSchema) -> bool:  # noqa: ARG003
        """Determine if a specific schema can be synchronized.

        :param schema: The schema instance to check.
        :type schema: CreatedSchema
        :return: `True` if the schema can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return cls.can_sync_entity_type(SyncInventoryEntityTypeEnum.SCHEMA)

    @classmethod
    def can_sync_table(cls, table: CreatedTable) -> bool:  # noqa: ARG003
        """Determine if a specific table can be synchronized.

        :param table: The table instance to check.
        :type table: CreatedTable
        :return: `True` if the table can be synchronized, `False` otherwise.
        :rtype: bool
        """
        return cls.can_sync_entity_type(SyncInventoryEntityTypeEnum.TABLE)


class TaskRunResult(NamedTuple):
    """Represent the result of a task execution.

    :param task_history_id: The unique identifier of the task history.
    :type task_history_id: int
    :param stdout: The standard output produced by the task.
    :type stdout: str
    """

    task_history_id: int
    stdout: str


class BaseTaskSyncer(BaseSyncer):
    """Provide a base class for task-based synchronizers in the PMM Extensions application.

    This class extends ``BaseSyncer`` by adding task management capabilities through the
    Tasks API, allowing synchronization processes to execute tasks and handle their
    outputs.

    :param tasks_api: The remote API interface for managing synchronization tasks.
    :param task_execution_timeout: The positive maximum time (in seconds) to wait for a
        task to complete. Defaults to ``300`` (5 minutes).
    :param tasks_execution_wait_interval: The positive interval (in seconds) between
        task status checks. Defaults to ``5``.
    :param force_executor_host: The host to force for task execution, if any.
    :param strict_executor_matching: Raise ``ExecutorHostNotFoundError`` instead of
        falling back to an arbitrary host when no executor matches the node.
        Defaults to ``False``.
    :param default_executor_host: The Nomad client host to use when no matching host
        is found for a service (e.g. RDS instances). If set, takes precedence over
        the first available host in the fallback path.
    """

    tasks_api: RemoteAPI
    task_execution_timeout: TaskExecutionTimeout = 300
    tasks_execution_wait_interval: TasksExecutionWaitInterval = 5
    force_executor_host: str | None = None
    strict_executor_matching: bool = False

    default_executor_host: str | None = None

    @asynccontextmanager
    async def api_auth(
        self, api_key: str, auth_scheme: str = "Bearer"
    ) -> AsyncGenerator[Self]:
        """Use a specific API key for the tasks API requests within the context.

        This asynchronous context manager temporarily sets the Authorization header
        for the inventory and tasks API to use the provided API key. It ensures that all
        requests made within the context use this API key for authentication.

        :param api_key: The API key to be used for authentication.
        :type api_key: str
        :param auth_scheme: The authentication scheme to be used (default is "Bearer").
        :type auth_scheme: str
        :yield: The syncer instance with the updated API key context.
        :rtype: AsyncGenerator[BaseSyncer]
        """
        with self.tasks_api.auth(api_key, auth_scheme):
            async with super().api_auth(api_key, auth_scheme) as syncer:
                yield syncer

    @alru_cache
    async def get_available_hosts(self) -> dict[str, str]:
        """Return the available hosts from the Tasks API.

        :return: The available hosts.
        :rtype: dict[str, str]
        """
        return as_json_object(await self.tasks_api.get("/hosts/"))

    @alru_cache
    async def get_task_target(self, host: str, name: str | None = None) -> str:
        """Return the target host for the task from the host.

        This method returns `self.force_executor_host` if set. Otherwise, it tries to
        find a target with the same address as ``host`` or the same name. If no match is
        found and ``strict_executor_matching`` is enabled, raise
        ``ExecutorHostNotFoundError``. Otherwise, return ``default_executor_host`` if
        set, or the first available host.

        :param host: The target host.
        :param name: The target name. Defaults to ``None``.
        :return: The target host for the task.
        :raises ExecutorHostNotFoundError: If ``strict_executor_matching`` is enabled and
            no executor host matches the node's name or address.
        """
        if self.force_executor_host:
            return self.force_executor_host
        available_hosts = await self.get_available_hosts()
        if name and name in available_hosts:
            return name
        for target, address in available_hosts.items():
            if address == host:
                return target
        if self.strict_executor_matching:
            raise ExecutorHostNotFoundError(name, host, available_hosts)
        if not available_hosts:
            raise ValueError(
                "No executor hosts available from /hosts/; cannot determine task target."
            )
        if self.default_executor_host and self.default_executor_host in available_hosts:
            return self.default_executor_host
        if self.default_executor_host:
            logger.warning(
                "default_executor_host %r not in available hosts %s; using first available",
                self.default_executor_host,
                list(available_hosts),
            )
        return next(iter(available_hosts))

    async def wait_for_task_output(
        self,
        task_name: str,
        stdout_step: str,
        payload: str | None = None,
        **meta: Any,
    ) -> TaskRunResult:
        """Wait for a task to complete and retrieve its output.

        This method initiates a task execution via the tasks API and waits for it to
        complete within the specified timeout. It retrieves the task's output upon
        successful completion or raises an error if the task fails or times out.

        :param task_name: The name of the task to execute.
        :type task_name: str
        :param stdout_step: The step identifier for output retrieval.
        :type stdout_step: str
        :param payload: The payload to send with the task execution request.
            Defaults to `None`.
        :type payload: str | None
        :param meta: Additional metadata to send with the task execution request.
        :type meta: Any
        :return: The result of the task execution.
        :rtype: TaskRunResult
        :raises TimeoutError: If the task times out.
        :raises ValueError: If the task fails.
        :raises HTTPUnprocessableEntityException: If ``task_name`` is not a single
            plain URL path segment.
        """
        task_history = as_json_object(
            await self.tasks_api.post(
                f"/execute{task_path(task_name)}",
                json={"meta": meta, "payload": payload, "anonymize_mask": 0},
            )
        )
        task_history_id = task_history["id"]
        status = task_history["status"]
        time_waiting = 0
        while (
            status in [TaskHistoryStatusEnum.PENDING, TaskHistoryStatusEnum.RUNNING]
            and time_waiting < self.task_execution_timeout
        ):
            await asyncio.sleep(self.tasks_execution_wait_interval)
            time_waiting += self.tasks_execution_wait_interval
            try:
                task_history = as_json_object(
                    await self.tasks_api.get(f"/history/{task_history_id}")
                )
                status = task_history["status"]
            except (HTTPException, ClientError):
                logger.exception("Error getting task history")

        if status in [TaskHistoryStatusEnum.PENDING, TaskHistoryStatusEnum.RUNNING]:
            raise TimeoutError(f"Task {task_name} timed out")

        output = defaultdict(str)
        async for log_entry in self.tasks_api.stream(
            f"/history/{task_history_id}/logs/", params={"step": stdout_step}
        ):
            if log_entry:
                log_data = json.loads(log_entry)
                output[log_data["type"]] += log_data["msg"]

        exc_detail = f"Task {task_name} failed"
        error_output = output[TaskLogType.STDERR]
        if error_output:
            logger.warning(
                "Task %s (%s) returned an error message: %s",
                task_name,
                task_history_id,
                error_output,
            )
            exc_detail = f"{exc_detail}: {error_output}"

        if status == TaskHistoryStatusEnum.FAILED:
            # TODO: Create custom exceptions  # noqa: TD002, TD003
            raise ValueError(exc_detail)

        return TaskRunResult(task_history_id, output[TaskLogType.STDOUT])
