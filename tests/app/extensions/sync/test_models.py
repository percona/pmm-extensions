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

"""Define tests for the app.extensions.sync.model module."""

import uuid
from datetime import datetime, timedelta
from typing import ClassVar, TypeVar
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from fastapi import HTTPException, status
from pydantic import PrivateAttr
from pydantic import ValidationError as PydanticValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.models import AlertService, AlertSeverity
from app.core.exceptions import HTTPUnprocessableEntityException
from app.core.utils.date_time import utc_now
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.inventory import (
    CreatedNode,
    CreatedSchema,
    CreatedService,
    CreatedTable,
    Node,
    Schema,
    Service,
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
from app.extensions.sync.exceptions import (
    SyncFailError,
    SyncInstanceAlreadyInProgressError,
)
from app.extensions.sync.models import BaseSyncer, BaseTaskSyncer, TaskRunResult
from app.inventory.models import ServiceTypeEnum, SyncOutcomeEnum
from app.tasks.models import TaskHistoryStatusEnum
from tests.app.extensions.path_unsafe_task_names import PATH_UNSAFE_TASKS
from tests.app.extensions.sync.conftest import (
    close_run,
    local_nodes_payload,
    sync_health_posts,
)
from tests.app.factories import (
    CreatedNodeFactory,
    CreatedSchemaFactory,
    CreatedServiceFactory,
    CreatedTableFactory,
    MOCK_CREATED_NODE_ID,
)

# Pinned verbatim rather than imported: the parameter name is the inventory API's
# contract, so renaming the constant must fail the test.
RETIRED_INCLUSIVE_PARAMS = {"include_retired": "true"}


class StubTestSyncer(BaseSyncer):
    """Implement a minimal ``BaseSyncer`` for model-layer tests."""

    SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.TABLE
    fetch_node_returns_none: ClassVar[bool] = False
    fetch_service_returns_none: ClassVar[bool] = False
    _perform_calls: list[str] = PrivateAttr(default_factory=list)

    @property
    def perform_calls(self) -> list[str]:
        """Return recorded perform-operation markers."""
        return self._perform_calls

    async def perform_inventory_sync(self) -> None:
        """Record inventory-sync invocation."""
        self._perform_calls.append("inventory")

    async def fetch_node(self, created_node: CreatedNode) -> Node | None:
        """Return a node model unless this stub is configured to skip."""
        if self.fetch_node_returns_none:
            return None
        return Node(
            address=created_node.address,
            name=created_node.name,
            external_id=created_node.external_id,
            type=created_node.type,
            source=created_node.source,
        )

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,
    ) -> None:
        """Record node-sync invocation."""
        self._perform_calls.append(f"node:{created_node.id}")

    async def fetch_service(self, created_service: CreatedService) -> Service | None:
        """Return a service model unless this stub is configured to skip."""
        if self.fetch_service_returns_none:
            return None
        return Service(
            name=created_service.name,
            type=created_service.type,
            external_id=created_service.external_id,
            port=created_service.port,
            environment=created_service.environment,
            cluster=created_service.cluster,
        )

    async def perform_service_sync(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> None:
        """Record service-sync invocation."""
        self._perform_calls.append(f"service:{created_service.id}")

    async def fetch_schema(self, created_schema: CreatedSchema) -> Schema:
        """Return a schema model mirroring the created schema."""
        return Schema(name=created_schema.name)

    async def perform_schema_sync(
        self,
        created_schema: CreatedSchema,
        updated_schema: Schema,
    ) -> None:
        """Record schema-sync invocation."""
        self._perform_calls.append(f"schema:{created_schema.id}")

    async def fetch_table(self, created_table: CreatedTable) -> Table:
        """Return a table model mirroring the created table."""
        return Table(
            name=created_table.name,
            create=created_table.create,
            keys=created_table.keys,
        )

    async def perform_table_sync(
        self,
        created_table: CreatedTable,
        updated_table: Table,
    ) -> None:
        """Record table-sync invocation."""
        self._perform_calls.append(f"table:{created_table.id}")


SyncerT = TypeVar("SyncerT", bound=BaseSyncer)


def _build_syncer(
    syncer_cls: type[SyncerT], session: AsyncSession, **kwargs
) -> SyncerT:
    """Construct a syncer and bind a real session (mirrors ``__aenter__`` assignment)."""
    syncer = syncer_cls(**kwargs)
    syncer._session = session
    return syncer


async def _create_sync_instance(
    session: AsyncSession, syncer_cls: type[BaseSyncer]
) -> SyncInstance:
    return await SyncInstanceManager.create(
        session,
        SyncInstanceWrite(syncer=syncer_cls.get_name()),
    )


async def _create_sync_item(
    session: AsyncSession,
    sync_instance: SyncInstance,
    entity_type: SyncInventoryEntityTypeEnum,
    entity_id: int | None,
) -> SyncItem:
    return await SyncItemManager.create(
        session,
        SyncItemWrite(
            entity_type=entity_type,
            entity_id=entity_id,
            sync_instance_id=sync_instance.id,
        ),
    )


async def _sync_item_status(
    session: AsyncSession,
    sync_item_id: uuid.UUID,
) -> SyncStatusEnum:
    sync_item = await session.get(SyncItem, sync_item_id)
    assert sync_item is not None, f"SyncItem {sync_item_id} not found"
    await session.refresh(sync_item)
    return sync_item.status


async def _manage_sync_item_failure_test_setup(
    session: AsyncSession,
    mock_remote_api,
    *,
    entity_type: SyncInventoryEntityTypeEnum,
    entity_id: int | None,
    break_on_error: bool = False,
) -> tuple[StubTestSyncer, SyncItem, AsyncMock]:
    """Return a syncer, persisted SyncItem, and mocked alert trigger for failure tests."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)
    sync_item = await _create_sync_item(
        session,
        sync_instance,
        entity_type,
        entity_id,
    )
    syncer = _build_syncer(
        StubTestSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
        sync_items={(entity_type, entity_id): sync_item},
        break_on_error=break_on_error,
    )
    mock_trigger = AsyncMock()
    return syncer, sync_item, mock_trigger


@pytest.fixture
def created_node() -> CreatedNode:
    """Return a fake created node."""
    created_node = CreatedNodeFactory.build()
    created_node.id = MOCK_CREATED_NODE_ID
    created_node.address = "localhost"
    created_node.external_id = "test-node-ext"
    return created_node


@pytest.fixture
def created_service(created_node) -> CreatedService:
    """Return a fake created service."""
    created_service = CreatedServiceFactory.build()
    created_service.type = ServiceTypeEnum.MYSQL
    created_service.environment = None
    return created_service


@pytest.fixture
def created_schema(created_service) -> CreatedSchema:
    """Return a fake created Schema."""
    created_schema = CreatedSchemaFactory.build()
    created_schema.service = created_service
    return created_schema


@pytest.fixture
def created_table(created_schema) -> CreatedTable:
    """Return a fake created Table."""
    created_table = CreatedTableFactory.build()
    created_table.database = created_schema
    return created_table


@pytest.mark.asyncio
async def test_aenter_initializes_session(mock_remote_api, mocker):
    """Test session init and closure with __aenter__ and __aexit__."""
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_session.exec = AsyncMock()

    mock_session_maker = MagicMock(return_value=mock_session)

    mock_finish_hanging_items = AsyncMock()
    mocker.patch(
        "app.extensions.sync.models.SyncInstanceManager.finish_hanging_items",
        mock_finish_hanging_items,
    )
    mocker.patch(
        "app.extensions.sync.models.SyncInstanceManager.finalize_run",
        new_callable=AsyncMock,
    )

    mock_create_sync_instance = AsyncMock()
    mocker.patch(
        "app.extensions.sync.models.get_async_session_maker",
        return_value=mock_session_maker,
    )
    with patch.object(SyncInstanceManager, "create", mock_create_sync_instance):

        class TestSyncer(BaseSyncer):
            SYNC_TO_LIMIT = MagicMock()

        syncer = TestSyncer(
            inventory_api=mock_remote_api,
            sync_instance=None,
        )
        async with syncer as result:
            mock_session_maker.assert_called_once()
            mock_session.__aenter__.assert_called_once()

        assert result.sync_instance == mock_create_sync_instance.return_value

        mock_finish_hanging_items.assert_awaited_once_with(
            mock_session,
            syncer.sync_instance.id,
        )


@pytest.mark.asyncio
async def test_prepare_sync(session: AsyncSession, created_node, mock_remote_api):
    """Test preparing synchronization for a given entity and its children."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)

    class NodeSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

    syncer = _build_syncer(
        NodeSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )
    mock_remote_api.get.side_effect = [local_nodes_payload(created_node)]

    await syncer.prepare_sync(SyncInventoryEntityTypeEnum.INVENTORY, None)

    result = await session.exec(
        select(SyncItem).where(SyncItem.sync_instance_id == sync_instance.id)
    )
    assert {(s.entity_type, s.entity_id) for s in result.all()} == {
        (SyncInventoryEntityTypeEnum.INVENTORY, None),
        (SyncInventoryEntityTypeEnum.NODE, created_node.id),
    }


class TestPrepareSyncSkipsRetiredEntities:
    """Test that ``prepare_sync`` opens no SyncItem for a tombstoned entity."""

    @staticmethod
    def _tree(retired: SyncInventoryEntityTypeEnum | None = None) -> CreatedNode:
        """Build a single-branch node → service → schema → table tree.

        :param retired: The one level to tombstone, or ``None`` for an all-active tree.
        :return: The node at the root of the tree.
        """
        retired_at = utc_now()

        def retired_at_for(level: SyncInventoryEntityTypeEnum) -> datetime | None:
            return retired_at if level == retired else None

        table = CreatedTableFactory.build(
            id=40, retired_at=retired_at_for(SyncInventoryEntityTypeEnum.TABLE)
        )
        schema = CreatedSchemaFactory.build(
            id=30,
            tables=[table],
            retired_at=retired_at_for(SyncInventoryEntityTypeEnum.SCHEMA),
        )
        service = CreatedServiceFactory.build(
            id=20,
            schemas=[schema],
            retired_at=retired_at_for(SyncInventoryEntityTypeEnum.SERVICE),
        )
        return CreatedNodeFactory.build(
            id=10,
            services=[service],
            retired_at=retired_at_for(SyncInventoryEntityTypeEnum.NODE),
        )

    @staticmethod
    def _serve(mock_remote_api: AsyncMock, *nodes: CreatedNode) -> None:
        """Answer the node list and per-service reads from ``nodes``.

        :param mock_remote_api: The inventory client the syncer reads through.
        :param nodes: The nodes the inventory reports, services nested.
        """
        services = {
            f"/services/{service.id}": service.model_dump()
            for node in nodes
            for service in node.services
        }

        async def get(path: str, **_kwargs) -> dict:
            if path == "/nodes/":
                return local_nodes_payload(*nodes)
            return services[path]

        mock_remote_api.get.side_effect = get

    @staticmethod
    async def _prepared(
        session: AsyncSession,
        syncer_cls: type[BaseSyncer],
        mock_remote_api: AsyncMock,
    ) -> set[tuple[SyncInventoryEntityTypeEnum, int | None]]:
        """Run one generation's ``prepare_sync`` and return the items it opened.

        :param session: The session the run writes through.
        :param syncer_cls: The syncer to prepare with.
        :param mock_remote_api: The inventory client the syncer reads through.
        :return: The ``(entity_type, entity_id)`` of every item the run persisted.
        """
        sync_instance = await _create_sync_instance(session, syncer_cls)
        syncer = _build_syncer(
            syncer_cls,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        await syncer.prepare_sync(SyncInventoryEntityTypeEnum.INVENTORY, None)
        await close_run(session, sync_instance.id, snapshot_complete=None)
        items = await SyncItemManager.list(session, sync_instance_id=sync_instance.id)
        return {(item.entity_type, item.entity_id) for item in items}

    @pytest.mark.asyncio
    async def test_retired_node_subtree_is_not_walked(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Skip an active service under a tombstoned node, without reading it."""

        class ServiceSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SERVICE

        self._serve(mock_remote_api, self._tree(SyncInventoryEntityTypeEnum.NODE))

        assert await self._prepared(session, ServiceSyncer, mock_remote_api) == {
            (SyncInventoryEntityTypeEnum.INVENTORY, None),
        }
        assert [call.args[0] for call in mock_remote_api.get.await_args_list] == [
            "/nodes/"
        ]

    @pytest.mark.asyncio
    async def test_retired_service_under_active_node_gets_no_item(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Open the active node's item but not its tombstoned service's."""

        class ServiceSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SERVICE

        self._serve(mock_remote_api, self._tree(SyncInventoryEntityTypeEnum.SERVICE))

        assert await self._prepared(session, ServiceSyncer, mock_remote_api) == {
            (SyncInventoryEntityTypeEnum.INVENTORY, None),
            (SyncInventoryEntityTypeEnum.NODE, 10),
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("retired_position", [0, 1])
    async def test_only_active_siblings_get_items(
        self, session: AsyncSession, mock_remote_api, retired_position: int
    ) -> None:
        """Open items for the active sibling whichever order the read returns."""

        class NodeSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

        retired = CreatedNodeFactory.build(id=11, services=[], retired_at=utc_now())
        active = CreatedNodeFactory.build(id=12, services=[])
        siblings = [active]
        siblings.insert(retired_position, retired)
        self._serve(mock_remote_api, *siblings)

        assert await self._prepared(session, NodeSyncer, mock_remote_api) == {
            (SyncInventoryEntityTypeEnum.INVENTORY, None),
            (SyncInventoryEntityTypeEnum.NODE, active.id),
        }

    @pytest.mark.asyncio
    async def test_guard_does_not_defer_to_can_sync(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Skip a tombstone even when the syncer's own predicate admits it."""

        class PermissiveSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

            @classmethod
            def can_sync_node(cls, node: CreatedNode) -> bool:  # noqa: ARG003
                return True

        self._serve(mock_remote_api, self._tree(SyncInventoryEntityTypeEnum.NODE))

        assert await self._prepared(session, PermissiveSyncer, mock_remote_api) == {
            (SyncInventoryEntityTypeEnum.INVENTORY, None),
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("retired", "expected_levels"),
        [
            (
                SyncInventoryEntityTypeEnum.SCHEMA,
                {
                    SyncInventoryEntityTypeEnum.INVENTORY,
                    SyncInventoryEntityTypeEnum.NODE,
                    SyncInventoryEntityTypeEnum.SERVICE,
                },
            ),
            (
                SyncInventoryEntityTypeEnum.TABLE,
                {
                    SyncInventoryEntityTypeEnum.INVENTORY,
                    SyncInventoryEntityTypeEnum.NODE,
                    SyncInventoryEntityTypeEnum.SERVICE,
                    SyncInventoryEntityTypeEnum.SCHEMA,
                },
            ),
        ],
    )
    async def test_guard_applies_at_every_level(
        self,
        session: AsyncSession,
        mock_remote_api,
        retired: SyncInventoryEntityTypeEnum,
        expected_levels: set[SyncInventoryEntityTypeEnum],
    ) -> None:
        """Stop the walk at a tombstone wherever it sits in the tree."""
        self._serve(mock_remote_api, self._tree(retired))

        prepared = await self._prepared(session, StubTestSyncer, mock_remote_api)

        assert {entity_type for entity_type, _ in prepared} == expected_levels

    @pytest.mark.asyncio
    async def test_item_count_is_stable_across_generations(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Open the same items each generation while a tombstone persists."""

        class NodeSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

        retired = CreatedNodeFactory.build(id=11, services=[], retired_at=utc_now())
        active = CreatedNodeFactory.build(id=12, services=[])
        self._serve(mock_remote_api, retired, active)

        first = await self._prepared(session, NodeSyncer, mock_remote_api)
        second = await self._prepared(session, NodeSyncer, mock_remote_api)

        assert (
            first
            == second
            == {
                (SyncInventoryEntityTypeEnum.INVENTORY, None),
                (SyncInventoryEntityTypeEnum.NODE, active.id),
            }
        )


class TestManageSyncItemOnRetiredEntity:
    """Test that an explicit sync of a tombstone still tracks its own item."""

    @pytest.mark.asyncio
    async def test_explicitly_managed_retired_entity_still_gets_an_item(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Open and close an item for a tombstone synced by explicit request."""
        sync_instance = await _create_sync_instance(session, StubTestSyncer)
        syncer = _build_syncer(
            StubTestSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        retired = CreatedNodeFactory.build(id=11, services=[], retired_at=utc_now())

        async with syncer.manage_sync_item(SyncInventoryEntityTypeEnum.NODE, retired):
            pass

        item = syncer.sync_items[(SyncInventoryEntityTypeEnum.NODE, retired.id)]
        assert item.status == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_type",
    [
        (SyncInventoryEntityTypeEnum.SERVICE),
        (SyncInventoryEntityTypeEnum.INVENTORY),
        (SyncInventoryEntityTypeEnum.NODE),
    ],
)
async def test_get_children_entities(entity_type, mock_remote_api, created_service):
    """Test retrieving child entities for a given entity type and entity."""
    created_entity = None
    syncer = BaseSyncer(
        inventory_api=mock_remote_api, sync_instance=None, _session=AsyncMock()
    )
    if entity_type == SyncInventoryEntityTypeEnum.SERVICE:
        created_entity = created_service
        mock_remote_api.get.side_effect = [created_service.model_dump()]
    elif entity_type == SyncInventoryEntityTypeEnum.INVENTORY:
        mock_remote_api.get.side_effect = [
            {"items": [], "total": 0, "offset": 0, "limit": 50},
        ]
    await syncer.get_children_entities(entity_type, created_entity)


@pytest.mark.asyncio
async def test_get_sync_items(session: AsyncSession, mock_remote_api):
    """Test retrieving multiple SyncItems for specified entities."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)
    syncer = _build_syncer(
        StubTestSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    sync_items = await syncer.get_sync_items(
        SyncInventoryEntityTypeEnum.INVENTORY, None
    )

    assert len(sync_items) == 1
    assert sync_items[0].entity_type == SyncInventoryEntityTypeEnum.INVENTORY
    assert sync_items[0].entity_id is None
    assert sync_items[0].sync_instance_id == sync_instance.id


@pytest.mark.asyncio
async def test_manage_sync_item(session: AsyncSession, mock_remote_api):
    """Test managing the synchronization lifecycle of a SyncItem."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)
    sync_item = await _create_sync_item(
        session,
        sync_instance,
        SyncInventoryEntityTypeEnum.INVENTORY,
        None,
    )
    syncer = _build_syncer(
        StubTestSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
        sync_items={(SyncInventoryEntityTypeEnum.INVENTORY, None): sync_item},
    )

    async with syncer.manage_sync_item(SyncInventoryEntityTypeEnum.INVENTORY, None):
        pass

    assert len(syncer.sync_items) == 1
    assert await _sync_item_status(session, sync_item.id) == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_manage_sync_item_failure_triggers_inventory_alert(
    session: AsyncSession,
    mock_remote_api,
    mocker,
):
    """Assert inventory sync failure triggers an ERROR alert with the expected dedup_key."""
    entity_type = SyncInventoryEntityTypeEnum.INVENTORY
    syncer, sync_item, mock_trigger = await _manage_sync_item_failure_test_setup(
        session,
        mock_remote_api,
        entity_type=entity_type,
        entity_id=None,
    )
    mocker.patch.object(AlertService, "trigger", mock_trigger)
    syncer_name = StubTestSyncer.get_name()

    async with syncer.manage_sync_item(entity_type, None):
        raise RuntimeError("Simulate inventory sync failure.")

    assert await _sync_item_status(session, sync_item.id) == SyncStatusEnum.FAILED
    mock_trigger.assert_awaited_once()
    alert_data = mock_trigger.call_args[0][0]
    assert alert_data["severity"] == AlertSeverity.ERROR
    assert alert_data["class"] == "inventory_sync_item_failure"
    assert syncer_name in alert_data["summary"]
    assert "top-level inventory sync" in alert_data["summary"]
    assert alert_data["source"] == f"{syncer_name}:{entity_type.name}:top_level"
    assert alert_data["dedup_key"] == f"{syncer_name}:{entity_type.name}:top_level"


@pytest.mark.asyncio
async def test_manage_sync_item_failure_triggers_node_alert(
    session: AsyncSession,
    mock_remote_api,
    mocker,
    created_node,
):
    """Assert node sync failure triggers an ERROR alert with the expected dedup_key."""
    entity_type = SyncInventoryEntityTypeEnum.NODE
    syncer, sync_item, mock_trigger = await _manage_sync_item_failure_test_setup(
        session,
        mock_remote_api,
        entity_type=entity_type,
        entity_id=created_node.id,
    )
    mocker.patch.object(AlertService, "trigger", mock_trigger)
    syncer_name = StubTestSyncer.get_name()
    entity_id_repr = str(created_node.external_id)

    async with syncer.manage_sync_item(entity_type, created_node):
        raise RuntimeError("Simulate node sync failure.")

    assert await _sync_item_status(session, sync_item.id) == SyncStatusEnum.FAILED
    mock_trigger.assert_awaited_once()
    alert_data = mock_trigger.call_args[0][0]
    assert alert_data["severity"] == AlertSeverity.ERROR
    assert alert_data["class"] == "inventory_sync_item_failure"
    assert syncer_name in alert_data["summary"]
    assert f"{entity_type.name} id {created_node.id}" in alert_data["summary"]
    assert f"name={created_node.name!r}" in alert_data["summary"]
    assert "address='localhost'" in alert_data["summary"]
    assert f"external_id={created_node.external_id!r}" in alert_data["summary"]
    assert alert_data["source"] == f"{syncer_name}:{entity_type.name}:{entity_id_repr}"
    assert (
        alert_data["dedup_key"] == f"{syncer_name}:{entity_type.name}:{entity_id_repr}"
    )


@pytest.mark.asyncio
async def test_manage_sync_item_failure_alert_when_break_on_error(
    session: AsyncSession, created_node, mock_remote_api, mocker
):
    """Assert the alert fires before ``SyncFailError`` when ``break_on_error`` is True."""
    syncer, sync_item, mock_trigger = await _manage_sync_item_failure_test_setup(
        session,
        mock_remote_api,
        entity_type=SyncInventoryEntityTypeEnum.NODE,
        entity_id=created_node.id,
        break_on_error=True,
    )
    mocker.patch.object(AlertService, "trigger", mock_trigger)

    with pytest.raises(SyncFailError):
        async with syncer.manage_sync_item(
            SyncInventoryEntityTypeEnum.NODE, created_node
        ):
            raise RuntimeError("Simulate node sync failure.")

    assert await _sync_item_status(session, sync_item.id) == SyncStatusEnum.FAILED
    mock_trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_finish_sync(session: AsyncSession, mock_remote_api):
    """Test finalizing synchronization for a given entity and its children."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)
    sync_item = await _create_sync_item(
        session,
        sync_instance,
        SyncInventoryEntityTypeEnum.INVENTORY,
        None,
    )
    syncer = _build_syncer(
        StubTestSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
        sync_items={(SyncInventoryEntityTypeEnum.INVENTORY, None): sync_item},
    )

    await syncer.finish_sync(SyncInventoryEntityTypeEnum.INVENTORY, None)

    assert await _sync_item_status(session, sync_item.id) == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_retire_node(
    session: AsyncSession,
    created_node,
    created_service,
    created_schema,
    created_table,
    mock_remote_api,
):
    """Test retiring inventories in the inventory system."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)

    class NodeLimitSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

    syncer = _build_syncer(
        NodeLimitSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    mock_remote_api.delete.side_effect = [
        created_node.model_dump(),
        created_service.model_dump(),
        created_schema.model_dump(),
        created_table.model_dump(),
    ]

    await syncer.retire_node(created_node)
    mock_remote_api.delete.assert_awaited_once_with(f"/nodes/{created_node.id}")
    mock_remote_api.delete.reset_mock()
    await syncer.retire_service(created_service)
    mock_remote_api.delete.assert_awaited_once_with(f"/services/{created_service.id}")
    mock_remote_api.delete.reset_mock()
    await syncer.retire_schema(created_schema)
    mock_remote_api.delete.assert_awaited_once_with(f"/schemas/{created_schema.id}")
    mock_remote_api.delete.reset_mock()
    await syncer.retire_table(created_table)
    mock_remote_api.delete.assert_awaited_once_with(f"/tables/{created_table.id}")


class TestRetiredEntityReads:
    """Test how ``reads_retired_entities`` shapes the inventory reads."""

    @pytest.mark.asyncio
    async def test_reads_omit_the_opt_in_by_default(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Leave tombstones invisible to a syncer that did not opt in."""
        syncer = _build_syncer(StubTestSyncer, session, inventory_api=mock_remote_api)
        mock_remote_api.get.return_value = created_node.model_dump()

        await syncer.get_inventory_node(created_node.id)

        mock_remote_api.get.assert_awaited_once_with(
            f"/nodes/{created_node.id}", params={}
        )

    @pytest.mark.asyncio
    async def test_a_level_outside_the_declared_set_stays_active_only(
        self, session: AsyncSession, created_node, created_schema, mock_remote_api
    ) -> None:
        """Scope the opt-in per level, so a walking level keeps hiding tombstones."""

        class SchemaOnlySyncer(StubTestSyncer):
            reads_retired_entities = frozenset({SyncInventoryEntityTypeEnum.SCHEMA})

        syncer = _build_syncer(SchemaOnlySyncer, session, inventory_api=mock_remote_api)

        mock_remote_api.get.return_value = created_node.model_dump()
        await syncer.get_inventory_node(created_node.id)
        assert mock_remote_api.get.await_args.kwargs["params"] == {}

        mock_remote_api.get.return_value = created_schema.model_dump()
        await syncer.get_inventory_schema(created_schema.id)
        assert mock_remote_api.get.await_args.kwargs["params"] == (
            RETIRED_INCLUSIVE_PARAMS
        )

    @pytest.mark.asyncio
    async def test_reads_opt_in_when_the_syncer_declares_it(
        self,
        session: AsyncSession,
        created_node,
        created_service,
        created_schema,
        created_table,
        mock_remote_api,
    ) -> None:
        """Send the opt-in on every per-entity read of a retirement-aware syncer."""

        class RetirementAwareSyncer(StubTestSyncer):
            reads_retired_entities = frozenset(SyncInventoryEntityTypeEnum)

        syncer = _build_syncer(
            RetirementAwareSyncer, session, inventory_api=mock_remote_api
        )
        reads = (
            (syncer.get_inventory_node, created_node, "nodes"),
            (syncer.get_inventory_service, created_service, "services"),
            (syncer.get_inventory_schema, created_schema, "schemas"),
            (syncer.get_inventory_table, created_table, "tables"),
        )
        for read, entity, segment in reads:
            mock_remote_api.get.reset_mock()
            mock_remote_api.get.return_value = entity.model_dump()

            await read(entity.id)

            mock_remote_api.get.assert_awaited_once_with(
                f"/{segment}/{entity.id}", params=RETIRED_INCLUSIVE_PARAMS
            )


class TestReviveIfRetired:
    """Test the revival helper the syncers call at their match sites."""

    @pytest.mark.asyncio
    async def test_active_entity_is_left_alone(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Make no call for an entity that was never retired."""
        syncer = _build_syncer(StubTestSyncer, session, inventory_api=mock_remote_api)
        created_node.retired_at = None

        await syncer._revive_if_retired(SyncInventoryEntityTypeEnum.NODE, created_node)

        mock_remote_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retired_entity_is_revived_and_cleared_locally(
        self, session: AsyncSession, created_service, mock_remote_api
    ) -> None:
        """Revive the remote row and clear the cached copy's retirement."""
        syncer = _build_syncer(StubTestSyncer, session, inventory_api=mock_remote_api)
        created_service.retired_at = utc_now()

        await syncer._revive_if_retired(
            SyncInventoryEntityTypeEnum.SERVICE, created_service
        )

        mock_remote_api.post.assert_awaited_once_with(
            f"/services/{created_service.id}/revive"
        )
        assert created_service.retired_at is None


@pytest.mark.asyncio
async def test_sync_inventory(session: AsyncSession, mock_remote_api):
    """Test synchronizing the entire inventory."""

    class InventorySyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

    sync_instance = await _create_sync_instance(session, InventorySyncer)
    syncer = _build_syncer(
        InventorySyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )
    mock_remote_api.get.side_effect = [
        {"items": [], "total": 0, "offset": 0, "limit": 50},
    ]

    await syncer.sync_inventory()

    assert syncer.perform_calls == ["inventory"]
    result = await session.exec(
        select(SyncItem).where(
            SyncItem.sync_instance_id == sync_instance.id,
            SyncItem.entity_type == SyncInventoryEntityTypeEnum.INVENTORY,
            SyncItem.entity_id.is_(None),
        )
    )
    inventory_sync_item = result.one()
    assert inventory_sync_item.status == SyncStatusEnum.SUCCESS


@pytest.mark.parametrize(
    ("fetch_returns_none", "expected_perform_calls"),
    [
        (False, lambda entity_id: [f"node:{entity_id}"]),
        (True, lambda _entity_id: []),
    ],
    ids=["performs_sync", "skips_when_fetch_returns_none"],
)
@pytest.mark.asyncio
async def test_sync_node(
    session: AsyncSession,
    created_node,
    mock_remote_api,
    fetch_returns_none,
    expected_perform_calls,
):
    """Test synchronizing a node, including when fetch_node returns None.

    When fetch returns None, ``manage_sync_item`` still creates the SyncItem and
    ``finish_sync`` marks it SUCCESS after the early return skips ``perform_node_sync``.
    """

    class NodeSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE
        fetch_node_returns_none: ClassVar[bool] = fetch_returns_none

    sync_instance = await _create_sync_instance(session, NodeSyncer)
    syncer = _build_syncer(
        NodeSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    await syncer.sync_node(created_node, None)

    assert syncer.perform_calls == expected_perform_calls(created_node.id)
    result = await session.exec(
        select(SyncItem).where(
            SyncItem.sync_instance_id == sync_instance.id,
            SyncItem.entity_type == SyncInventoryEntityTypeEnum.NODE,
            SyncItem.entity_id == created_node.id,
        )
    )
    node_sync_item = result.one()
    assert node_sync_item.status == SyncStatusEnum.SUCCESS


@pytest.mark.parametrize(
    ("fetch_returns_none", "expected_perform_calls"),
    [
        (False, lambda entity_id: [f"service:{entity_id}"]),
        (True, lambda _entity_id: []),
    ],
    ids=["performs_sync", "skips_when_fetch_returns_none"],
)
@pytest.mark.asyncio
async def test_sync_service(
    session: AsyncSession,
    created_service,
    mock_remote_api,
    fetch_returns_none,
    expected_perform_calls,
):
    """Test synchronizing a service, including when fetch_service returns None.

    When fetch returns None, ``manage_sync_item`` still creates the SyncItem and
    ``finish_sync`` marks it SUCCESS after the early return skips ``perform_service_sync``.
    """

    class ServiceSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SERVICE
        fetch_service_returns_none: ClassVar[bool] = fetch_returns_none

    sync_instance = await _create_sync_instance(session, ServiceSyncer)
    syncer = _build_syncer(
        ServiceSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    await syncer.sync_service(created_service, None)

    assert syncer.perform_calls == expected_perform_calls(created_service.id)
    result = await session.exec(
        select(SyncItem).where(
            SyncItem.sync_instance_id == sync_instance.id,
            SyncItem.entity_type == SyncInventoryEntityTypeEnum.SERVICE,
            SyncItem.entity_id == created_service.id,
        )
    )
    service_sync_item = result.one()
    assert service_sync_item.status == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_sync_schema(session: AsyncSession, created_schema, mock_remote_api):
    """Test synchronizing data for a specific schema."""

    class SchemaSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SCHEMA

    sync_instance = await _create_sync_instance(session, SchemaSyncer)
    syncer = _build_syncer(
        SchemaSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    await syncer.sync_schema(created_schema, None)

    assert syncer.perform_calls == [f"schema:{created_schema.id}"]
    result = await session.exec(
        select(SyncItem).where(
            SyncItem.sync_instance_id == sync_instance.id,
            SyncItem.entity_type == SyncInventoryEntityTypeEnum.SCHEMA,
            SyncItem.entity_id == created_schema.id,
        )
    )
    schema_sync_item = result.one()
    assert schema_sync_item.status == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_sync_table(session: AsyncSession, created_table, mock_remote_api):
    """Test synchronizing data for a specific table."""

    class TableSyncer(StubTestSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.TABLE

    sync_instance = await _create_sync_instance(session, TableSyncer)
    syncer = _build_syncer(
        TableSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )

    await syncer.sync_table(created_table, None)

    assert syncer.perform_calls == [f"table:{created_table.id}"]
    result = await session.exec(
        select(SyncItem).where(
            SyncItem.sync_instance_id == sync_instance.id,
            SyncItem.entity_type == SyncInventoryEntityTypeEnum.TABLE,
            SyncItem.entity_id == created_table.id,
        )
    )
    table_sync_item = result.one()
    assert table_sync_item.status == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_update_table(created_table, mock_remote_api):
    """Test updating a table in the inventory system."""
    updated_table = created_table.model_copy(
        update={"create": "UPDATED CREATE STATEMENT"}
    )
    syncer = BaseSyncer(
        inventory_api=mock_remote_api, sync_instance=None, _session=AsyncMock()
    )
    mock_remote_api.put.side_effect = [updated_table.model_dump()]
    await syncer.update_table(created_table, updated_table)
    mock_remote_api.put.assert_awaited_once_with(
        f"/tables/{created_table.id}", json=updated_table.model_dump()
    )


@pytest.mark.asyncio
async def test_wait_for_task_output(session: AsyncSession, mock_remote_api, mocker):
    """Test waiting for a task to complete and retrieve its output."""
    step_name = "mock_step_name"
    expected_call_count = 2
    mock_remote_api.post.side_effect = [
        {
            "id": "12345",
            "status": TaskHistoryStatusEnum.PENDING,
        }
    ]

    mock_remote_api.get.side_effect = [
        {"id": "12345", "status": TaskHistoryStatusEnum.RUNNING},
        {
            "id": "12345",
            "status": TaskHistoryStatusEnum.SUCCESS,
            "execution_request": {"tracking": {}},
        },
    ]

    async def empty_stream(*_args, **_kwargs):
        for _ in ():
            yield _

    mock_remote_api.stream = empty_stream
    mocker.patch("app.extensions.sync.models.asyncio.sleep", new_callable=AsyncMock)

    class TaskTestSyncer(BaseTaskSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.INVENTORY

    sync_instance = await _create_sync_instance(session, TaskTestSyncer)
    task_syncer = _build_syncer(
        TaskTestSyncer,
        session,
        inventory_api=mock_remote_api,
        tasks_api=mock_remote_api,
        sync_instance=sync_instance,
        tasks_execution_wait_interval=1,
    )

    await task_syncer.wait_for_task_output(task_name="syncing", stdout_step=step_name)

    mock_remote_api.post.assert_awaited_once()
    assert mock_remote_api.get.await_count == expected_call_count


async def _empty_stream(*_args, **_kwargs):
    for _ in ():
        yield _


def _build_task_test_syncer(session, mock_remote_api, **kwargs):
    class TaskTestSyncer(BaseTaskSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.INVENTORY

    return _build_syncer(
        TaskTestSyncer,
        session,
        inventory_api=mock_remote_api,
        tasks_api=mock_remote_api,
        **kwargs,
    )


@pytest.mark.asyncio
class TestWaitForTaskOutputPathGuard:
    """Test that a task name cannot reshape the execute request."""

    @pytest.mark.parametrize("task_name", PATH_UNSAFE_TASKS)
    async def test_refuses_a_name_that_is_not_one_path_segment(
        self, session: AsyncSession, mock_remote_api, task_name: str
    ) -> None:
        """Refuse an unsafe name before the execute POST is issued.

        No syncer passes a caller-supplied name today — both subclasses default
        it to ``run-python`` — but the parameter is a required positional on this
        base method, so nothing except the current call sites keeps it static.
        """
        task_syncer = _build_task_test_syncer(
            session, mock_remote_api, tasks_execution_wait_interval=1
        )

        with pytest.raises(HTTPUnprocessableEntityException):
            await task_syncer.wait_for_task_output(
                task_name=task_name, stdout_step="step"
            )

        mock_remote_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_task_output_tolerates_http_exception(
    session: AsyncSession, mock_remote_api, mocker
):
    """A transient ``HTTPException`` while polling is tolerated and polling continues."""
    mock_remote_api.post.side_effect = [
        {"id": "12345", "status": TaskHistoryStatusEnum.PENDING}
    ]
    mock_remote_api.get.side_effect = [
        HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ),
        {
            "id": "12345",
            "status": TaskHistoryStatusEnum.SUCCESS,
            "execution_request": {"tracking": {}},
        },
    ]
    mock_remote_api.stream = _empty_stream
    mocker.patch("app.extensions.sync.models.asyncio.sleep", new_callable=AsyncMock)
    log_spy = mocker.patch("app.extensions.sync.models.logger.exception")

    task_syncer = _build_task_test_syncer(
        session, mock_remote_api, tasks_execution_wait_interval=1
    )
    result = await task_syncer.wait_for_task_output(
        task_name="syncing", stdout_step="step"
    )

    assert isinstance(result, TaskRunResult)
    log_spy.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_task_output_tolerates_client_error(
    session: AsyncSession, mock_remote_api, mocker
):
    """A transient aiohttp ``ClientError`` while polling is tolerated."""
    mock_remote_api.post.side_effect = [
        {"id": "12345", "status": TaskHistoryStatusEnum.PENDING}
    ]
    mock_remote_api.get.side_effect = [
        aiohttp.ClientConnectionError("connection blip"),
        {
            "id": "12345",
            "status": TaskHistoryStatusEnum.SUCCESS,
            "execution_request": {"tracking": {}},
        },
    ]
    mock_remote_api.stream = _empty_stream
    mocker.patch("app.extensions.sync.models.asyncio.sleep", new_callable=AsyncMock)
    log_spy = mocker.patch("app.extensions.sync.models.logger.exception")

    task_syncer = _build_task_test_syncer(
        session, mock_remote_api, tasks_execution_wait_interval=1
    )
    result = await task_syncer.wait_for_task_output(
        task_name="syncing", stdout_step="step"
    )

    assert isinstance(result, TaskRunResult)
    log_spy.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_task_output_persistent_error_times_out(
    session: AsyncSession, mock_remote_api, mocker
):
    """A persistent polling error exhausts the timeout window and raises ``TimeoutError``."""
    mock_remote_api.post.side_effect = [
        {"id": "12345", "status": TaskHistoryStatusEnum.PENDING}
    ]
    mock_remote_api.get.side_effect = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    mocker.patch("app.extensions.sync.models.asyncio.sleep", new_callable=AsyncMock)

    task_syncer = _build_task_test_syncer(
        session,
        mock_remote_api,
        tasks_execution_wait_interval=1,
        task_execution_timeout=2,
    )

    with pytest.raises(TimeoutError):
        await task_syncer.wait_for_task_output(task_name="syncing", stdout_step="step")


# ---------------------------------------------------------------------------
# Run-level state and stale-run reclaim plumbing
# ---------------------------------------------------------------------------


@pytest.fixture
def lifecycle_syncer_cls() -> type[BaseSyncer]:
    """Return a minimal concrete syncer usable as an async context manager."""

    class LifecycleSyncer(BaseSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SERVICE

    return LifecycleSyncer


@pytest.fixture
def lifecycle_mocks(mocker) -> dict[str, AsyncMock]:
    """Replace the SyncInstance lifecycle collaborators around a mock session."""
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mocker.patch(
        "app.extensions.sync.models.get_async_session_maker",
        return_value=MagicMock(return_value=mock_session),
    )
    return {
        "session": mock_session,
        "create": mocker.patch.object(
            SyncInstanceManager, "create", new_callable=AsyncMock
        ),
        "finish_hanging_items": mocker.patch.object(
            SyncInstanceManager, "finish_hanging_items", new_callable=AsyncMock
        ),
        "finalize_run": mocker.patch.object(
            SyncInstanceManager, "finalize_run", new_callable=AsyncMock
        ),
    }


@pytest.mark.asyncio
async def test_aenter_passes_stale_run_after(
    mock_remote_api, lifecycle_syncer_cls, lifecycle_mocks
):
    """``__aenter__`` opts the run into age-based stale reclaim."""
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    async with syncer:
        pass

    assert (
        lifecycle_mocks["create"].await_args.kwargs["stale_after"]
        == syncer.stale_run_after
    )


@pytest.mark.asyncio
async def test_aenter_persists_a_running_instance(
    session: AsyncSession, mock_remote_api, lifecycle_syncer_cls, mocker
):
    """Persist a live run as ``RUNNING`` so the fencing read can ever pass.

    The fence is what authorises every retirement, so a run persisted as anything
    else would silently stop the syncer retiring anything at all, with no failed
    item and no error to notice.
    """
    holder = MagicMock()
    holder.__aenter__ = AsyncMock(return_value=session)
    holder.__aexit__ = AsyncMock(return_value=None)
    mocker.patch(
        "app.extensions.sync.models.get_async_session_maker",
        return_value=MagicMock(return_value=holder),
    )
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    async with syncer:
        instance_id = syncer.sync_instance.id
        assert await SyncInstanceManager.is_still_owned(session, instance_id) is True

    finalized = await SyncInstanceManager.first(session, id=instance_id)
    assert finalized.status == SyncStatusEnum.SUCCESS


@pytest.mark.asyncio
async def test_aexit_finalizes_run_after_sweeping_hanging_items(
    mock_remote_api, lifecycle_syncer_cls, lifecycle_mocks, mocker
):
    """Read item statuses the hanging-item sweep already wrote."""
    order = mocker.Mock()
    lifecycle_mocks["finish_hanging_items"].side_effect = lambda *_args, **_kwargs: (
        order("sweep")
    )
    lifecycle_mocks["finalize_run"].side_effect = lambda *_args, **_kwargs: (
        order("finalize")
    )
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    async with syncer:
        pass

    assert [call.args[0] for call in order.call_args_list] == ["sweep", "finalize"]
    assert lifecycle_mocks["finalize_run"].await_args.kwargs == {
        "failed": False,
        "snapshot_complete": None,
    }


@pytest.mark.asyncio
async def test_aexit_reports_failure_when_an_exception_propagates(
    mock_remote_api, lifecycle_syncer_cls, lifecycle_mocks
):
    """Roll the run-level status up to failed when an exception escapes."""
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    with pytest.raises(RuntimeError):
        async with syncer:
            raise RuntimeError("apply blew up")

    assert lifecycle_mocks["finalize_run"].await_args.kwargs["failed"] is True


@pytest.mark.asyncio
async def test_aexit_persists_the_generation_completeness_verdict(
    mock_remote_api, lifecycle_syncer_cls, lifecycle_mocks
):
    """Persist whatever the fetch concluded about completeness."""
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    async with syncer:
        syncer._snapshot_complete = False

    assert lifecycle_mocks["finalize_run"].await_args.kwargs["snapshot_complete"] is (
        False
    )


@pytest.mark.asyncio
async def test_aenter_closes_the_session_when_the_run_is_refused(
    mock_remote_api, lifecycle_syncer_cls, lifecycle_mocks
):
    """Close the session opened for a run the syncer turns out not to own.

    A refusal leaves ``__aexit__`` unreached, so nothing else ever closes it, and
    refusing is the routine outcome of two triggers overlapping rather than a rare
    one.
    """
    lifecycle_mocks["create"].side_effect = SyncInstanceAlreadyInProgressError(
        detail="A run of syncer 'lifecycle' is being created already.",
    )
    syncer = lifecycle_syncer_cls(inventory_api=mock_remote_api)

    with pytest.raises(SyncInstanceAlreadyInProgressError):
        async with syncer:
            pytest.fail("a refused run must not enter the body")

    lifecycle_mocks["session"].close.assert_awaited_once()


@pytest.mark.parametrize("stale_run_after", [timedelta(0), timedelta(seconds=-1)])
def test_syncer_rejects_non_positive_stale_run_after(
    mock_remote_api, lifecycle_syncer_cls, stale_run_after
):
    """Reject a non-positive threshold that makes every run reclaimable."""
    with pytest.raises(PydanticValidationError):
        lifecycle_syncer_cls(
            inventory_api=mock_remote_api, stale_run_after=stale_run_after
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_execution_timeout", 0),
        ("task_execution_timeout", -1),
        ("tasks_execution_wait_interval", 0),
        ("tasks_execution_wait_interval", -1),
    ],
)
def test_task_syncer_rejects_non_positive_timing_value(
    mock_remote_api, field: str, value: int
) -> None:
    """Reject task timing values the polling loop cannot act on.

    The two fields fail differently: a non-positive wait interval never advances the
    elapsed-time counter, so the loop polls without ever timing out, while a
    non-positive timeout makes the loop's guard false on its first evaluation, so the
    task is declared timed out without being polled at all.
    """

    class TaskTestSyncer(BaseTaskSyncer):
        SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.INVENTORY

    with pytest.raises(PydanticValidationError):
        TaskTestSyncer(
            inventory_api=mock_remote_api,
            tasks_api=mock_remote_api,
            **{field: value},
        )


@pytest.mark.asyncio
async def test_hold_entity_closes_the_sync_item(
    session: AsyncSession, created_node, mock_remote_api
):
    """Close a held entity's SyncItem to SUCCESS instead of leaving PENDING."""
    sync_instance = await _create_sync_instance(session, StubTestSyncer)
    syncer = _build_syncer(
        StubTestSyncer,
        session,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )
    created_node.services = []

    await syncer.hold_entity(SyncInventoryEntityTypeEnum.NODE, created_node)

    held = await SyncItemManager.first(
        session,
        entity_id=created_node.id,
        entity_type=SyncInventoryEntityTypeEnum.NODE,
        sync_instance_id=sync_instance.id,
    )
    assert held.status == SyncStatusEnum.SUCCESS


class NodeMirroringSyncer(StubTestSyncer):
    """Declare the node level mirrored, as ``PMMSyncer`` does."""

    SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = frozenset(
        {SyncInventoryEntityTypeEnum.NODE}
    )


class TableMirroringSyncer(StubTestSyncer):
    """Declare the schema and table levels mirrored, as ``MySQLSyncer`` does."""

    SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.TABLE
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = frozenset(
        {SyncInventoryEntityTypeEnum.SCHEMA, SyncInventoryEntityTypeEnum.TABLE}
    )


class TestSyncHealthWiring:
    """Test which of ``BaseSyncer``'s boundaries report an entity's sync health."""

    def test_no_level_is_mirrored_by_default(self) -> None:
        """Own nothing until a subclass says otherwise."""
        assert BaseSyncer.mirrors_entity_levels == frozenset()

    @pytest.mark.asyncio
    async def test_a_mirrored_level_reports_a_clean_sync(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Report one success for the node this syncer just confirmed."""
        sync_instance = await _create_sync_instance(session, NodeMirroringSyncer)
        syncer = _build_syncer(
            NodeMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        posts = sync_health_posts(mock_remote_api)
        assert [path for path, _ in posts] == [f"/nodes/{created_node.id}/sync-health"]
        assert posts[0][1]["outcome"] == SyncOutcomeEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_an_unmirrored_level_reports_nothing(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Leave the columns alone at a level this syncer only traverses."""

        class TraversingSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.NODE

        sync_instance = await _create_sync_instance(session, TraversingSyncer)
        syncer = _build_syncer(
            TraversingSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        assert sync_health_posts(mock_remote_api) == []

    @pytest.mark.asyncio
    async def test_a_filtered_out_entity_reports_nothing(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Report nothing when ``fetch_node`` declines the entity.

        The early return leaves ``manage_sync_item`` on the same clean-exit path
        a real sync takes, so only the unset compared marker separates them.
        """

        class FilteringSyncer(NodeMirroringSyncer):
            fetch_node_returns_none = True

        sync_instance = await _create_sync_instance(session, FilteringSyncer)
        syncer = _build_syncer(
            FilteringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        assert sync_health_posts(mock_remote_api) == []

    @pytest.mark.asyncio
    async def test_an_entity_outside_the_run_scope_reports_nothing(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Report nothing for a node a scoped run declined to visit at all.

        ``can_sync_node`` gates ahead of both context managers, so the entity
        never becomes an attempt — distinct from the filtered-out case below,
        which does enter them.
        """

        class ScopedSyncer(NodeMirroringSyncer):
            @classmethod
            def can_sync_node(cls, node: CreatedNode) -> bool:  # noqa: ARG003
                """Exclude every node from this run's target set."""
                return False

        sync_instance = await _create_sync_instance(session, ScopedSyncer)
        syncer = _build_syncer(
            ScopedSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        assert sync_health_posts(mock_remote_api) == []

    @pytest.mark.asyncio
    async def test_a_fetch_failure_is_reported(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Report the failure a ``fetch_node`` raise ends the attempt with."""

        class FetchFailingSyncer(NodeMirroringSyncer):
            async def fetch_node(self, created_node):
                """Fail before any source data is in hand."""
                raise RuntimeError("upstream unreachable")

        sync_instance = await _create_sync_instance(session, FetchFailingSyncer)
        syncer = _build_syncer(
            FetchFailingSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        posts = sync_health_posts(mock_remote_api)
        assert [body["outcome"] for _, body in posts] == [SyncOutcomeEnum.FAILURE]

    @pytest.mark.asyncio
    async def test_a_perform_failure_is_reported_inside_the_sync_item_boundary(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Report a failure the enclosing SyncItem boundary swallows.

        ``manage_sync_item`` absorbs the exception when ``break_on_error`` is
        off, so a reporter wrapped *outside* it would see a clean exit and post
        a success. The failure body is what pins the nesting order.
        """

        class PerformFailingSyncer(NodeMirroringSyncer):
            async def perform_node_sync(self, created_node, updated_node):
                """Fail after the comparison against source began."""
                raise RuntimeError("write rejected")

        sync_instance = await _create_sync_instance(session, PerformFailingSyncer)
        syncer = _build_syncer(
            PerformFailingSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )

        await syncer.sync_node(created_node, None)

        posts = sync_health_posts(mock_remote_api)
        assert [body["outcome"] for _, body in posts] == [SyncOutcomeEnum.FAILURE]
        item = await SyncItemManager.first(
            session,
            entity_id=created_node.id,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            sync_instance_id=sync_instance.id,
        )
        assert item.status == SyncStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_break_on_error_still_reports_the_failure(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Record the outcome before the run aborts, not instead of aborting."""

        class PerformFailingSyncer(NodeMirroringSyncer):
            async def perform_node_sync(self, created_node, updated_node):
                """Fail after the comparison against source began."""
                raise RuntimeError("write rejected")

        sync_instance = await _create_sync_instance(session, PerformFailingSyncer)
        syncer = _build_syncer(
            PerformFailingSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
            break_on_error=True,
        )

        with pytest.raises(SyncFailError):
            await syncer.sync_node(created_node, None)

        posts = sync_health_posts(mock_remote_api)
        assert [body["outcome"] for _, body in posts] == [SyncOutcomeEnum.FAILURE]

    @pytest.mark.asyncio
    async def test_a_child_failure_does_not_mark_the_parent_failing(
        self, session: AsyncSession, created_node, created_service, mock_remote_api
    ) -> None:
        """Confirm the parent whose own mirror succeeded, even as a child fails.

        The syncers walk to children from inside the parent's ``perform_*_sync``,
        so under ``break_on_error`` the child's ``SyncFailError`` passes through
        the parent's reporter. Attributing it to the parent would report a node
        as failing whose own fields were just confirmed — and would say
        something different from the default mode, where the child's own
        boundary swallows the same failure.
        """

        class ParentSyncer(StubTestSyncer):
            SYNC_TO_LIMIT = SyncInventoryEntityTypeEnum.SERVICE
            mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
                frozenset(
                    {
                        SyncInventoryEntityTypeEnum.NODE,
                        SyncInventoryEntityTypeEnum.SERVICE,
                    }
                )
            )

            async def perform_node_sync(self, created_node, updated_node):
                """Confirm the node, then walk to the service that will fail."""
                await self.sync_service(created_service)

            async def perform_service_sync(self, created_service, updated_service):
                """Fail the child level."""
                raise RuntimeError("child write rejected")

        created_node.services = [created_service]
        sync_instance = await _create_sync_instance(session, ParentSyncer)
        syncer = _build_syncer(
            ParentSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
            break_on_error=True,
        )

        with pytest.raises(SyncFailError):
            await syncer.sync_node(created_node, None)

        posts = dict(sync_health_posts(mock_remote_api))
        assert (
            posts[f"/services/{created_service.id}/sync-health"]["outcome"]
            == SyncOutcomeEnum.FAILURE
        )
        assert (
            posts[f"/nodes/{created_node.id}/sync-health"]["outcome"]
            == SyncOutcomeEnum.SUCCESS
        )

    @pytest.mark.asyncio
    async def test_a_failed_bookkeeping_post_leaves_the_sync_item_successful(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Keep a healthy sync healthy when the freshness write cannot land."""
        sync_instance = await _create_sync_instance(session, NodeMirroringSyncer)
        syncer = _build_syncer(
            NodeMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        mock_remote_api.post.side_effect = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY
        )

        await syncer.sync_node(created_node, None)

        item = await SyncItemManager.first(
            session,
            entity_id=created_node.id,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            sync_instance_id=sync_instance.id,
        )
        assert item.status == SyncStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_schema_and_table_levels_report(
        self, session: AsyncSession, created_schema, created_table, mock_remote_api
    ) -> None:
        """Report both levels the MySQL-shaped syncer mirrors."""
        sync_instance = await _create_sync_instance(session, TableMirroringSyncer)
        syncer = _build_syncer(
            TableMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        created_schema.tables = []

        await syncer.sync_schema(created_schema, None)
        await syncer.sync_table(created_table, None)

        assert [path for path, _ in sync_health_posts(mock_remote_api)] == [
            f"/schemas/{created_schema.id}/sync-health",
            f"/tables/{created_table.id}/sync-health",
        ]

    @pytest.mark.asyncio
    async def test_holding_an_entity_reports_nothing(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Confirm nothing about a node this run only declined to retire."""
        sync_instance = await _create_sync_instance(session, NodeMirroringSyncer)
        syncer = _build_syncer(
            NodeMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        created_node.services = []

        await syncer.hold_entity(SyncInventoryEntityTypeEnum.NODE, created_node)

        assert sync_health_posts(mock_remote_api) == []

    @pytest.mark.asyncio
    async def test_retiring_an_entity_reports_nothing(
        self, session: AsyncSession, created_node, mock_remote_api
    ) -> None:
        """Confirm nothing about a node whose upstream is gone."""
        sync_instance = await _create_sync_instance(session, NodeMirroringSyncer)
        syncer = _build_syncer(
            NodeMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        created_node.services = []

        await syncer.retire_node(created_node)

        assert sync_health_posts(mock_remote_api) == []

    @pytest.mark.asyncio
    async def test_the_inventory_level_reports_nothing(
        self, session: AsyncSession, mock_remote_api
    ) -> None:
        """Report nothing for the run-level boundary, which names no entity."""
        sync_instance = await _create_sync_instance(session, NodeMirroringSyncer)
        syncer = _build_syncer(
            NodeMirroringSyncer,
            session,
            inventory_api=mock_remote_api,
            sync_instance=sync_instance,
        )
        mock_remote_api.get.side_effect = [
            {"items": [], "total": 0, "offset": 0, "limit": 50},
        ]

        await syncer.sync_inventory()

        assert sync_health_posts(mock_remote_api) == []
