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

"""Test the app.extensions.sync.syncers.system_facts.syncer module."""

import asyncio
import json
from datetime import datetime, timedelta, UTC
from typing import Any
from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import UUID4, ValidationError

from app.core.alerts.config import alert_service
from app.core.exceptions import HTTPBadGatewayException
from app.core.requests import RemoteAPI
from app.core.utils.date_time import utc_now
from app.extensions.crud import SyncInstanceManager, SyncItemManager
from app.extensions.inventory import CreatedNode, CreatedService
from app.extensions.models import (
    SyncInstance,
    SyncInstanceWrite,
    SyncInventoryEntityTypeEnum,
    SyncItem,
    SyncStatusEnum,
)
from app.extensions.sync.exceptions import (
    IncompleteObservationsReadError,
    SyncFailError,
)
from app.extensions.sync.models import TaskRunResult
from app.extensions.sync.syncers.system_facts.syncer import (
    first_measurement_due,
    SystemFactsService,
    SystemFactsSyncer,
    UnmeasuredHostFactsSyncer,
)
from app.inventory.models import ServiceTypeEnum
from tests.app.extensions.sync.conftest import sync_health_posts
from tests.app.factories import (
    CreatedNodeFactory,
    CreatedServiceFactory,
    MOCK_CREATED_NODE_ID,
)

NODE_NAME = "db-node-1"
NODE_ADDRESS = "10.0.0.5"
COLLECTED_AT = "2026-06-01T12:00:00+00:00"
# observed_at as serialized by pydantic model_dump(mode="json") (UTC -> trailing "Z").
COLLECTED_AT_JSON = "2026-06-01T12:00:00Z"
MYSQL_PORT = 3306
MYSQL_VERSION = "8.0.35"
OS_VERSION = "Ubuntu 22.04"
INSTALLED_PACKAGES = [{"name": "glibc", "version": "2.35"}]
HOST_CONFIG = {"kernel": "5.15.0"}
EXECUTOR_NAME = "executor-1"
EXECUTOR_ADDRESS = "10.0.0.99"


def _make_service(
    service_type: ServiceTypeEnum, port: int, node: CreatedNode, service_id: int
) -> CreatedService:
    """Build a fake created service attached to ``node``."""
    service = CreatedServiceFactory.build()
    service.id = service_id
    service.node_id = node.id
    service.type = service_type
    service.port = port
    service.node = node
    return service


@pytest.fixture
def mock_syncer(mock_remote_api) -> SystemFactsSyncer:
    """Return a SystemFactsSyncer with mocked tasks/inventory APIs."""
    return SystemFactsSyncer(tasks_api=mock_remote_api, inventory_api=mock_remote_api)


@pytest_asyncio.fixture
async def bound_system_facts_syncer(session, mock_remote_api) -> SystemFactsSyncer:
    """Return a SystemFactsSyncer bound to a real session and persisted SyncInstance.

    A test whose subject reaches the base ``manage_sync_item`` needs both, the
    same way ``bound_mysql_syncer`` does for the MySQL syncer's cascade tests.
    """
    sync_instance = await SyncInstanceManager.create(
        session,
        SyncInstanceWrite(syncer=SystemFactsSyncer.get_name()),
    )
    syncer = SystemFactsSyncer(
        tasks_api=mock_remote_api,
        inventory_api=mock_remote_api,
        sync_instance=sync_instance,
    )
    syncer._session = session
    return syncer


@pytest.fixture
def created_node() -> CreatedNode:
    """Return a node with one MySQL service on a known address."""
    node = CreatedNodeFactory.build()
    node.id = MOCK_CREATED_NODE_ID
    node.name = NODE_NAME
    node.address = NODE_ADDRESS
    node.services = [_make_service(ServiceTypeEnum.MYSQL, MYSQL_PORT, node, 1)]
    return node


@pytest.fixture
def created_service(created_node) -> CreatedService:
    """Return the MySQL service from ``created_node``."""
    return created_node.services[0]


class TestConfigAndCanSync:
    """Test config building and sync eligibility predicates."""

    def test_sync_to_limit_is_service(self):
        """SystemFactsSyncer stops recursion at the service level."""
        assert SystemFactsSyncer.SYNC_TO_LIMIT == SyncInventoryEntityTypeEnum.SERVICE

    def test_build_script_config(self, mock_syncer, created_service):
        """Script config carries probe targets and the collect_host flag."""
        cfg = json.loads(
            mock_syncer.build_script_config([created_service], collect_host=True)
        )
        assert cfg["collect_host"] is True
        assert cfg["services"] == [
            {"address": created_service.address, "type": ServiceTypeEnum.MYSQL}
        ]

    @pytest.mark.parametrize(
        ("service_type", "expected"),
        [
            (ServiceTypeEnum.MYSQL, True),
            (ServiceTypeEnum.POSTGRESQL, True),
            (ServiceTypeEnum.MONGODB, True),
            (ServiceTypeEnum.PROXYSQL, False),
            (ServiceTypeEnum.HAPROXY, False),
            (ServiceTypeEnum.EXTERNAL, False),
        ],
    )
    def test_can_sync_service(self, created_node, service_type, expected):
        """Only DB engine services are collected for facts."""
        service = _make_service(service_type, MYSQL_PORT, created_node, 1)
        assert SystemFactsSyncer.can_sync_service(service) is expected

    def test_can_sync_node_true_with_db_service(self, created_node):
        """A node with at least one DB engine service is collectable."""
        assert SystemFactsSyncer.can_sync_node(created_node) is True

    def test_can_sync_node_true_without_db_service(self, created_node):
        """Host facts are node-level: a proxy-only node is still collectable.

        Host facts (``os_version``/``installed_packages``/``config``) are independent of
        the services on the node, so node eligibility must not be gated on a DB engine.
        """
        created_node.services = [
            _make_service(ServiceTypeEnum.HAPROXY, MYSQL_PORT, created_node, 1)
        ]
        assert SystemFactsSyncer.can_sync_node(created_node) is True


class TestFetchNode:
    """Test fetching node-level facts via the task payload."""

    @pytest.mark.asyncio
    async def test_fetch_node_colocated_collects_host_and_services(
        self, mock_syncer, created_node, created_service, mocker
    ):
        """A co-located node collects host facts and caches service versions."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        wait = mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        wait.return_value = TaskRunResult(
            1,
            json.dumps(
                {
                    "host": {
                        "os_version": OS_VERSION,
                        "installed_packages": INSTALLED_PACKAGES,
                        "config": HOST_CONFIG,
                        "collected_at": COLLECTED_AT,
                    },
                    "services": {
                        created_service.address: {
                            "db_engine_version": MYSQL_VERSION,
                            "collected_at": COLLECTED_AT,
                        }
                    },
                }
            ),
        )
        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None
        wait.assert_awaited_once()
        # collect_host True is sent to the payload for a co-located node.
        assert json.loads(wait.await_args.kwargs["config"])["collect_host"] is True
        assert (
            mock_syncer._host_facts_cache[created_node.id]["os_version"] == OS_VERSION
        )
        assert (
            mock_syncer._service_facts_cache[created_service.id]["db_engine_version"]
            == MYSQL_VERSION
        )

    @pytest.mark.asyncio
    async def test_fetch_node_rds_skips_host_but_keeps_services(
        self, mock_syncer, created_node, created_service, mocker
    ):
        """A node with no co-located executor collects service facts only."""
        mock_syncer.default_executor_host = EXECUTOR_NAME
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {EXECUTOR_NAME: EXECUTOR_ADDRESS}
        wait = mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        wait.return_value = TaskRunResult(
            1,
            json.dumps(
                {
                    "host": None,
                    "services": {
                        created_service.address: {
                            "db_engine_version": MYSQL_VERSION,
                            "collected_at": COLLECTED_AT,
                        }
                    },
                }
            ),
        )
        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None  # must NOT skip the whole node
        assert json.loads(wait.await_args.kwargs["config"])["collect_host"] is False
        assert created_node.id not in mock_syncer._host_facts_cache
        assert created_service.id in mock_syncer._service_facts_cache

    @pytest.mark.asyncio
    async def test_fetch_node_colocated_proxy_only_collects_host(
        self, mock_syncer, created_node, mocker
    ):
        """A co-located node with no DB service still collects host facts."""
        created_node.services = [
            _make_service(ServiceTypeEnum.HAPROXY, MYSQL_PORT, created_node, 1)
        ]
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        wait = mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        wait.return_value = TaskRunResult(
            1,
            json.dumps(
                {
                    "host": {
                        "os_version": OS_VERSION,
                        "collected_at": COLLECTED_AT,
                    },
                    "services": {},
                }
            ),
        )
        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None
        wait.assert_awaited_once()
        cfg = json.loads(wait.await_args.kwargs["config"])
        assert cfg["collect_host"] is True
        assert cfg["services"] == []  # no DB engine services to probe
        assert mock_syncer._host_facts_cache[created_node.id]["os_version"] == (
            OS_VERSION
        )

    @pytest.mark.asyncio
    async def test_fetch_node_no_host_no_services_skips_task_run(
        self, mock_syncer, created_node, mocker
    ):
        """A non-co-located node with no DB service runs no task at all."""
        created_node.services = [
            _make_service(ServiceTypeEnum.HAPROXY, MYSQL_PORT, created_node, 1)
        ]
        mock_syncer.default_executor_host = EXECUTOR_NAME
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {EXECUTOR_NAME: EXECUTOR_ADDRESS}
        wait = mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None
        wait.assert_not_awaited()
        assert not mock_syncer._host_facts_cache
        assert not mock_syncer._service_facts_cache

    @pytest.mark.asyncio
    async def test_fetch_node_malformed_output_skips_cleanly(
        self, mock_syncer, created_node, mocker
    ):
        """Malformed payload stdout yields empty caches, not an exception."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(1, "not-json")

        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None
        assert not mock_syncer._host_facts_cache
        assert not mock_syncer._service_facts_cache

    @pytest.mark.asyncio
    async def test_fetch_node_wrong_shape_output_skips_cleanly(
        self, mock_syncer, created_node, mocker
    ):
        """Valid JSON with non-dict host/services degrades to empty caches, no crash."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(
            1, json.dumps({"host": "oops", "services": ["not-a-dict"]})
        )

        updated = await mock_syncer.fetch_node(created_node)

        assert updated is not None
        assert not mock_syncer._host_facts_cache
        assert not mock_syncer._service_facts_cache


class TestPerformNodeSync:
    """Test upserting host observations and recursing into services."""

    @pytest.mark.asyncio
    async def test_perform_node_sync_colocated_upserts_host(
        self, mock_syncer, created_node, mock_remote_api, mocker
    ):
        """Co-located host facts are PUT to the system-observation endpoint."""
        mock_syncer._host_facts_cache[created_node.id] = {
            "os_version": OS_VERSION,
            "installed_packages": INSTALLED_PACKAGES,
            "config": HOST_CONFIG,
            "collected_at": COLLECTED_AT,
        }
        sync_service = mocker.patch.object(
            SystemFactsSyncer, "sync_service", new_callable=AsyncMock
        )
        await mock_syncer.perform_node_sync(created_node, created_node)

        mock_remote_api.put.assert_awaited_once()
        url = mock_remote_api.put.await_args.args[0]
        body = mock_remote_api.put.await_args.kwargs["json"]
        assert url == f"/nodes/{created_node.id}/system-observation"
        assert body["os_version"] == OS_VERSION
        assert body["observed_at"] == COLLECTED_AT_JSON
        sync_service.assert_awaited_once_with(created_node.services[0])

    @pytest.mark.asyncio
    async def test_perform_node_sync_rds_skips_host_put(
        self, mock_syncer, created_node, mock_remote_api, mocker
    ):
        """With no host facts cached, no host observation is written."""
        sync_service = mocker.patch.object(
            SystemFactsSyncer, "sync_service", new_callable=AsyncMock
        )
        await mock_syncer.perform_node_sync(created_node, created_node)

        mock_remote_api.put.assert_not_awaited()
        sync_service.assert_awaited_once_with(created_node.services[0])

    @pytest.mark.asyncio
    async def test_perform_node_sync_empty_host_facts_skips_put(
        self, mock_syncer, created_node, mock_remote_api, mocker
    ):
        """Host facts with no usable field never write a half-empty snapshot."""
        mock_syncer._host_facts_cache[created_node.id] = {"collected_at": COLLECTED_AT}
        mocker.patch.object(SystemFactsSyncer, "sync_service", new_callable=AsyncMock)
        await mock_syncer.perform_node_sync(created_node, created_node)

        mock_remote_api.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_perform_node_sync_writes_a_measured_inability(
        self, mock_syncer, created_node, mock_remote_api, mocker
    ):
        """Write a node measured unable to elevate as ``False``, not dropped.

        Were the observation builder to filter on truthiness, it would read this
        measurement as never-observed and invert the signal being published.
        """
        mock_syncer._host_facts_cache[created_node.id] = {
            "can_elevate": False,
            "collected_at": COLLECTED_AT,
        }
        mocker.patch.object(SystemFactsSyncer, "sync_service", new_callable=AsyncMock)
        await mock_syncer.perform_node_sync(created_node, created_node)

        mock_remote_api.put.assert_awaited_once()
        body = mock_remote_api.put.await_args.kwargs["json"]
        assert body["can_elevate"] is False


class TestFetchService:
    """Test resolving cached service facts."""

    @pytest.mark.asyncio
    async def test_fetch_service_returns_cached_version(
        self, mock_syncer, created_service
    ):
        """The cached engine version is surfaced on the carrier."""
        mock_syncer._service_facts_cache[created_service.id] = {
            "db_engine_version": MYSQL_VERSION,
            "collected_at": COLLECTED_AT,
        }
        svc = await mock_syncer.fetch_service(created_service)
        assert isinstance(svc, SystemFactsService)
        assert svc.db_engine_version == MYSQL_VERSION
        assert svc.collected_at == COLLECTED_AT

    @pytest.mark.asyncio
    async def test_fetch_service_consumes_cache_entry(
        self, mock_syncer, created_service
    ):
        """A cache hit is popped so a later failed collection cannot resurface it."""
        mock_syncer._service_facts_cache[created_service.id] = {
            "db_engine_version": MYSQL_VERSION,
            "collected_at": COLLECTED_AT,
        }
        await mock_syncer.fetch_service(created_service)
        assert created_service.id not in mock_syncer._service_facts_cache

    @pytest.mark.asyncio
    async def test_fetch_service_stale_address_not_reused(
        self, mock_syncer, created_node, created_service, mocker
    ):
        """A sibling sharing an address never reads another service's cached fact.

        Service A's fact is cached by A's id. Service B shares A's address but has its own
        id; its fallback collection yields nothing, so B must resolve to ``None`` rather
        than reusing A's version.
        """
        service_b = _make_service(
            ServiceTypeEnum.MYSQL, MYSQL_PORT, created_node, created_service.id + 1
        )
        mock_syncer._service_facts_cache[created_service.id] = {
            "db_engine_version": MYSQL_VERSION,
            "collected_at": COLLECTED_AT,
        }
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(1, json.dumps({"host": None, "services": {}}))

        assert await mock_syncer.fetch_service(service_b) is None
        # Service A's fact stays untouched (B never popped it).
        assert created_service.id in mock_syncer._service_facts_cache

    @pytest.mark.asyncio
    async def test_fetch_service_cache_miss_collects_single_service(
        self, mock_syncer, created_service, mocker
    ):
        """A standalone service sync (empty cache) collects that one service.

        The scheduled run primes the cache in ``fetch_node``, but a per-service sync
        triggered from the UI starts empty; ``fetch_service`` must fall back to running
        the payload for just that service rather than silently no-op.
        """
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        wait = mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        )
        wait.return_value = TaskRunResult(
            1,
            json.dumps(
                {
                    "host": None,
                    "services": {
                        created_service.address: {
                            "db_engine_version": MYSQL_VERSION,
                            "collected_at": COLLECTED_AT,
                        }
                    },
                }
            ),
        )
        svc = await mock_syncer.fetch_service(created_service)

        assert isinstance(svc, SystemFactsService)
        assert svc.db_engine_version == MYSQL_VERSION
        wait.assert_awaited_once()
        # A standalone service collection never collects host facts.
        assert json.loads(wait.await_args.kwargs["config"])["collect_host"] is False

    @pytest.mark.asyncio
    async def test_fetch_service_cache_miss_collection_fails_returns_none(
        self, mock_syncer, created_service, mocker
    ):
        """A fallback collection that yields no version skips the service."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(1, json.dumps({"host": None, "services": {}}))

        assert await mock_syncer.fetch_service(created_service) is None

    @pytest.mark.asyncio
    async def test_fetch_service_uncollected_no_node_returns_none(
        self, mock_syncer, created_service, mocker
    ):
        """A cache miss with no collectable version returns None, never a crash."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(1, "not-json")
        assert await mock_syncer.fetch_service(created_service) is None

    @pytest.mark.asyncio
    async def test_fetch_service_batch_primed_failure_skips_standalone_run(
        self, mock_syncer, created_service, mocker
    ):
        """A batch-attempted service with no version is skipped, not re-collected.

        ``fetch_node`` marks every probed service as batch-attempted. One that failed in
        the batch (no cached version) must resolve to ``None`` without dispatching a fresh
        per-service task -- otherwise a node with K dead services issues ``1 + K`` runs.
        """
        mock_syncer._batch_attempted_services.add(created_service.id)
        collect = mocker.patch.object(
            SystemFactsSyncer, "_collect_single_service", new_callable=AsyncMock
        )

        assert await mock_syncer.fetch_service(created_service) is None
        collect.assert_not_awaited()
        # The marker is consumed so a later standalone sync is free to collect.
        assert created_service.id not in mock_syncer._batch_attempted_services

    @pytest.mark.asyncio
    async def test_fetch_service_unprimed_collects_single_service(
        self, mock_syncer, created_service, mocker
    ):
        """A service never primed by a batch run falls back to standalone collection."""
        collect = mocker.patch.object(
            SystemFactsSyncer, "_collect_single_service", new_callable=AsyncMock
        )
        collect.return_value = {
            "db_engine_version": MYSQL_VERSION,
            "collected_at": COLLECTED_AT,
        }

        svc = await mock_syncer.fetch_service(created_service)

        collect.assert_awaited_once_with(created_service)
        assert isinstance(svc, SystemFactsService)
        assert svc.db_engine_version == MYSQL_VERSION


class TestPerformServiceSync:
    """Test upserting service observations."""

    @pytest.mark.asyncio
    async def test_perform_service_sync_upserts_version(
        self, mock_syncer, created_service, mock_remote_api
    ):
        """The engine version is PUT to the service system-observation endpoint."""
        updated = SystemFactsService.model_validate(
            created_service.model_dump(exclude={"node", "schemas"})
        )
        updated.db_engine_version = MYSQL_VERSION
        updated.collected_at = COLLECTED_AT

        await mock_syncer.perform_service_sync(created_service, updated)

        mock_remote_api.put.assert_awaited_once()
        url = mock_remote_api.put.await_args.args[0]
        body = mock_remote_api.put.await_args.kwargs["json"]
        assert url == f"/services/{created_service.id}/system-observation"
        assert body["db_engine_version"] == MYSQL_VERSION
        assert body["observed_at"] == COLLECTED_AT_JSON

    @pytest.mark.asyncio
    async def test_perform_service_sync_is_idempotent_put_not_post(
        self, mock_syncer, created_service, mock_remote_api
    ):
        """Re-running upserts via PUT (server-side), never POST."""
        updated = SystemFactsService.model_validate(
            created_service.model_dump(exclude={"node", "schemas"})
        )
        updated.db_engine_version = MYSQL_VERSION
        updated.collected_at = COLLECTED_AT

        expected_put_calls = 2
        await mock_syncer.perform_service_sync(created_service, updated)
        await mock_syncer.perform_service_sync(created_service, updated)

        assert mock_remote_api.put.await_count == expected_put_calls
        mock_remote_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_perform_service_sync_without_version_skips(
        self, mock_syncer, created_service, mock_remote_api
    ):
        """A carrier missing the version never writes an empty db_engine_version."""
        updated = SystemFactsService.model_validate(
            created_service.model_dump(exclude={"node", "schemas"})
        )
        await mock_syncer.perform_service_sync(created_service, updated)
        mock_remote_api.put.assert_not_awaited()


class TestResolveTaskTarget:
    """Test executor-host resolution and the co-location flag it reports."""

    @pytest.mark.asyncio
    async def test_colocated_by_name(self, mock_syncer, mocker):
        """An executor matching the node name is co-located."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == (NODE_NAME, True)

    @pytest.mark.asyncio
    async def test_fallback_not_colocated(self, mock_syncer, mocker):
        """A default-executor fallback (RDS) is not co-located."""
        mock_syncer.default_executor_host = EXECUTOR_NAME
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {EXECUTOR_NAME: EXECUTOR_ADDRESS}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == (EXECUTOR_NAME, False)

    @pytest.mark.asyncio
    async def test_force_executor_not_matching_node_is_not_colocated(
        self, mock_syncer, mocker
    ):
        """A forced executor that is not the node must NOT report co-location.

        Otherwise host facts gathered on the forced executor would be misattributed to
        the node (writing executor-2's OS/packages onto db-node-1).
        """
        mock_syncer.force_executor_host = "executor-2"
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS, "executor-2": "10.0.0.77"}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == ("executor-2", False)

    @pytest.mark.asyncio
    async def test_force_executor_matching_node_name_is_colocated(
        self, mock_syncer, mocker
    ):
        """A forced executor that IS the node (by name) is co-located."""
        mock_syncer.force_executor_host = NODE_NAME
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == (NODE_NAME, True)

    @pytest.mark.asyncio
    async def test_force_executor_matching_node_address_is_colocated(
        self, mock_syncer, mocker
    ):
        """A forced executor whose address is the node's is co-located."""
        mock_syncer.force_executor_host = "executor-2"
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {"executor-2": NODE_ADDRESS}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == ("executor-2", True)

    @pytest.mark.asyncio
    async def test_no_available_hosts_raises_value_error(self, mock_syncer, mocker):
        """An empty ``/hosts/`` response cannot yield a target and raises."""
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {}
        with pytest.raises(ValueError, match="No executor hosts available"):
            await mock_syncer.resolve_task_target(NODE_ADDRESS, NODE_NAME)

    @pytest.mark.asyncio
    async def test_default_executor_not_in_hosts_falls_back_to_first(
        self, mock_syncer, mocker
    ):
        """A configured default executor absent from the host list falls back."""
        mock_syncer.default_executor_host = "ghost-executor"
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {EXECUTOR_NAME: EXECUTOR_ADDRESS}
        target, colocated = await mock_syncer.resolve_task_target(
            NODE_ADDRESS, NODE_NAME
        )
        assert (target, colocated) == (EXECUTOR_NAME, False)


class TestPerformInventorySync:
    """Test the top-level sync entrypoint."""

    @pytest.mark.asyncio
    async def test_perform_inventory_sync_invokes_sync_node(
        self, mock_syncer, created_node, mocker
    ):
        """Every inventory node is handed to sync_node (loop fans out to all nodes)."""
        second_node = CreatedNodeFactory.build()
        second_node.id = MOCK_CREATED_NODE_ID + 1
        mocker.patch.object(
            SystemFactsSyncer,
            "get_inventory_nodes",
            new_callable=AsyncMock,
            return_value=[created_node, second_node],
        )
        sync_node = mocker.patch.object(
            SystemFactsSyncer, "sync_node", new_callable=AsyncMock
        )
        await mock_syncer.perform_inventory_sync()
        assert sync_node.await_args_list == [call(created_node), call(second_node)]


class TestTombstoneBlindness:
    """Test that this syncer never sees a tombstone, and so never acts on one.

    ``perform_inventory_sync`` walks every inventory node unconditionally and holds
    no upstream diff, so it has no match site and no evidence that anything
    reappeared. Opting it into retired reads would only feed a scheduled run
    entities it cannot judge, driving observation writes at parents whose own
    active-only dependencies reject them.
    """

    @pytest.mark.asyncio
    async def test_inventory_reads_omit_the_opt_in(self, mock_syncer, mock_remote_api):
        """Ask the inventory for active nodes only."""
        mock_remote_api.get.return_value = {
            "items": [],
            "total": 0,
            "offset": 0,
            "limit": 50,
        }

        await mock_syncer.get_inventory_nodes()

        params = mock_remote_api.get.await_args.kwargs["params"]
        assert "include_retired" not in params

    def test_no_entity_level_opts_into_retired_reads(self):
        """Keep every entity level out of retired reads."""
        assert SystemFactsSyncer.reads_retired_entities == frozenset()


class TestMirroredEntityLevels:
    """Test which entity levels this syncer's attempts write sync health for."""

    def test_no_entity_level_is_mirrored(self):
        """Own no level: observations are a separate resource, not the entity's fields.

        The declaration is deliberately explicit rather than inherited. This
        syncer walks the same ``sync_node`` / ``sync_service`` path the mirroring
        syncers do, so a reader has to be able to see that its walks confirm
        nothing about the values PMM is responsible for.
        """
        assert SystemFactsSyncer.mirrors_entity_levels == frozenset()

    @pytest.mark.asyncio
    async def test_a_full_run_reports_no_sync_health(
        self, bound_system_facts_syncer, created_node, created_service, mocker
    ):
        """Leave both walked levels' columns untouched across a whole run.

        This is the regression guard for the masking hazard: a system-facts run
        interleaves with PMM's on the same rows, so refreshing their freshness
        here would report a failing PMM mirror as healthy. Only the two external
        boundaries are mocked — the executor host list and the task output — so
        the run drives the real ``sync_node`` / ``sync_service`` path the
        mirroring syncers share.
        """
        mocker.patch.object(
            SystemFactsSyncer, "get_available_hosts", new_callable=AsyncMock
        ).return_value = {NODE_NAME: NODE_ADDRESS}
        mocker.patch.object(
            SystemFactsSyncer, "wait_for_task_output", new_callable=AsyncMock
        ).return_value = TaskRunResult(
            1,
            json.dumps(
                {
                    "host": {
                        "os_version": OS_VERSION,
                        "installed_packages": INSTALLED_PACKAGES,
                        "config": HOST_CONFIG,
                        "collected_at": COLLECTED_AT,
                    },
                    "services": {
                        created_service.address: {
                            "db_engine_version": MYSQL_VERSION,
                            "collected_at": COLLECTED_AT,
                        }
                    },
                }
            ),
        )
        mocker.patch.object(
            SystemFactsSyncer,
            "get_inventory_nodes",
            new_callable=AsyncMock,
            return_value=[created_node],
        )

        await bound_system_facts_syncer.perform_inventory_sync()

        assert sync_health_posts(bound_system_facts_syncer.inventory_api) == []


class TestUpsertHostObservation:
    """Test the host-observation write the two host-facts syncers share."""

    @pytest.mark.asyncio
    async def test_reports_a_written_observation(
        self, mock_syncer, created_node, mock_remote_api
    ):
        """Report ``True`` once the observation PUT went through."""
        mock_syncer._host_facts_cache[created_node.id] = {
            "can_elevate": True,
            "collected_at": COLLECTED_AT,
        }

        assert await mock_syncer._upsert_host_observation(created_node) is True
        mock_remote_api.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reports_a_failed_put_without_raising(
        self, mock_syncer, created_node, mock_remote_api
    ):
        """Report ``False`` for a rejected PUT, keeping the daily run best-effort."""
        mock_syncer._host_facts_cache[created_node.id] = {
            "can_elevate": True,
            "collected_at": COLLECTED_AT,
        }
        mock_remote_api.put.side_effect = OSError("inventory unreachable")

        assert await mock_syncer._upsert_host_observation(created_node) is False

    @pytest.mark.asyncio
    async def test_reports_nothing_to_write(self, mock_syncer, created_node):
        """Report ``False`` when no host fact was collected for the node."""
        assert await mock_syncer._upsert_host_observation(created_node) is False


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _attempt(status: SyncStatusEnum) -> SyncItem:
    """Build one finished first-measurement attempt on a node."""
    return SyncItem(
        entity_id=MOCK_CREATED_NODE_ID,
        entity_type=SyncInventoryEntityTypeEnum.NODE,
        status=status,
        sync_instance_id=uuid4(),
    )


def _due(own_attempts: list[SyncItem], finished_ago: timedelta | None) -> bool:
    """Ask the policy whether a host is due, with the production retry values."""
    return first_measurement_due(
        own_attempts,
        None if finished_ago is None else NOW - finished_ago,
        NOW,
        retries=UnmeasuredHostFactsSyncer.FIRST_MEASUREMENT_RETRIES,
        retry_interval=UnmeasuredHostFactsSyncer.FIRST_MEASUREMENT_RETRY_INTERVAL,
    )


class TestFirstMeasurementDue:
    """Test the retry policy applied to a never-measured host."""

    def test_a_host_never_attempted_is_due(self):
        """Measure a host no host-facts syncer has finished an attempt on."""
        assert _due([], None) is True

    @pytest.mark.parametrize(
        ("finished_ago", "expected"),
        [(HOUR - timedelta(minutes=1), False), (HOUR, True)],
        ids=["59-minutes-ago", "60-minutes-ago"],
    )
    def test_retries_at_most_once_per_interval(self, finished_ago, expected):
        """Wait a full retry interval after the last finished attempt."""
        assert _due([_attempt(SyncStatusEnum.FAILED)], finished_ago) is expected

    @pytest.mark.parametrize(
        ("failures", "expected"),
        [(3, True), (4, False)],
        ids=["first-and-two-retries", "first-and-three-retries"],
    )
    def test_stops_after_the_retry_cap(self, failures, expected):
        """Leave a host to the daily run once the first attempt and 3 retries failed."""
        attempts = [_attempt(SyncStatusEnum.FAILED)] * failures

        assert _due(attempts, 2 * HOUR) is expected

    def test_a_success_clears_the_failure_count(self):
        """Count only the failures after the newest success."""
        attempts = [
            *[_attempt(SyncStatusEnum.FAILED)] * 4,
            _attempt(SyncStatusEnum.SUCCESS),
            _attempt(SyncStatusEnum.FAILED),
        ]

        assert _due(attempts, 2 * HOUR) is True


def _node(index: int) -> CreatedNode:
    """Build a service-less node co-located with executor ``probe-host-<index>``."""
    node = CreatedNodeFactory.build()
    node.id = MOCK_CREATED_NODE_ID + index
    node.name = f"probe-host-{index}"
    node.address = f"10.0.1.{index}"
    node.services = []
    return node


def _page(rows: list[Any], total: int | None = None) -> dict[str, Any]:
    """Build one inventory pagination envelope."""
    return {
        "items": rows,
        "total": len(rows) if total is None else total,
        "offset": 0,
        "limit": 200,
    }


def _observation(node_id: int) -> dict[str, Any]:
    """Build an observation summary row as ``/nodes/system-observations`` serves it."""
    return {"node_id": node_id, "can_elevate": None, "observed_at": COLLECTED_AT}


class FakeInventory:
    """Serve the inventory reads and writes a first-measurement pass makes.

    A successful observation PUT records the node as observed, so a later pass
    sees what an earlier one wrote.

    :param nodes: The active inventory nodes.
    :param observation_pages: Raw pages to answer the observations read with, in
        order, replacing the derived answer. An exception in the list is raised.
    """

    def __init__(
        self,
        nodes: list[CreatedNode],
        observation_pages: list[Any] | None = None,
    ) -> None:
        self.nodes = nodes
        self.observed: set[int] = set()
        self.observation_pages = observation_pages

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Answer a paginated inventory read."""
        if path == "/nodes/":
            return _page(
                [node.model_dump(mode="json", by_alias=True) for node in self.nodes]
            )
        if path == "/nodes/system-observations":
            if self.observation_pages is None:
                return _page([_observation(node_id) for node_id in self.observed])
            page = self.observation_pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page
        raise AssertionError(f"unexpected inventory read {path} {params}")

    async def put(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Accept an observation write."""
        self.observed.add(int(path.split("/")[2]))
        return kwargs["json"]


MEASURED_STDOUT = json.dumps(
    {"host": {"can_elevate": True, "collected_at": COLLECTED_AT}, "services": {}}
)


@pytest.fixture
def fake_inventory() -> FakeInventory:
    """Return an inventory holding one never-measured co-located node."""
    return FakeInventory([_node(1)])


@pytest.fixture
def inventory_api(fake_inventory) -> AsyncMock:
    """Return an inventory client backed by ``fake_inventory``."""
    api = AsyncMock(spec=RemoteAPI)
    api.get.side_effect = fake_inventory.get
    api.put.side_effect = fake_inventory.put
    return api


@pytest.fixture
def tasks_api() -> AsyncMock:
    """Return a tasks client whose ``/hosts/`` lists ten co-located executors."""
    api = AsyncMock(spec=RemoteAPI)
    api.get.return_value = {
        f"probe-host-{index}": f"10.0.1.{index}" for index in range(1, 11)
    }
    return api


@pytest.fixture
def dispatch(mocker) -> AsyncMock:
    """Stand in for the executor, answering every probe with a measurement."""
    return mocker.patch.object(
        UnmeasuredHostFactsSyncer,
        "wait_for_task_output",
        new_callable=AsyncMock,
        return_value=TaskRunResult(1, MEASURED_STDOUT),
    )


@pytest.fixture(autouse=True)
def alerts(mocker) -> AsyncMock:
    """Capture the alerts a failed attempt raises."""
    return mocker.patch.object(alert_service, "trigger", new_callable=AsyncMock)


async def _run_pass(
    session,
    inventory_api: AsyncMock,
    tasks_api: AsyncMock,
    **syncer_options: Any,
) -> UUID4:
    """Run one first-measurement pass the way a scheduled run drives it.

    The run row is inserted directly, as ``bound_system_facts_syncer`` does, and
    the hanging-item sweep ``__aexit__`` performs runs even when the pass raises.

    :return: The id of the pass's run.
    """
    instance = SyncInstance(
        syncer=UnmeasuredHostFactsSyncer.get_name(), status=SyncStatusEnum.RUNNING
    )
    session.add(instance)
    await session.commit()
    await session.refresh(instance)
    syncer = UnmeasuredHostFactsSyncer(
        inventory_api=inventory_api,
        tasks_api=tasks_api,
        sync_instance=instance,
        **syncer_options,
    )
    syncer._session = session
    try:
        await syncer.sync_inventory()
    finally:
        await SyncInstanceManager.finish_hanging_items(session, instance.id)
    return instance.id


async def _statuses(
    session, run_id: UUID4, entity_type: SyncInventoryEntityTypeEnum
) -> dict[int | None, SyncStatusEnum]:
    """Return the status of each item one run recorded at ``entity_type``."""
    items = await SyncItemManager.list(
        session, sync_instance_id=run_id, entity_type=entity_type
    )
    return {item.entity_id: item.status for item in items}


async def _record_attempt(
    session,
    syncer: type[SystemFactsSyncer],
    node_id: int,
    status: SyncStatusEnum,
    finished_ago: timedelta,
) -> None:
    """Persist one run of ``syncer`` with a NODE item last changed ``finished_ago``."""
    instance = SyncInstance(syncer=syncer.get_name(), status=SyncStatusEnum.SUCCESS)
    session.add(instance)
    await session.commit()
    await session.refresh(instance)
    at = utc_now() - finished_ago
    session.add(
        SyncItem(
            entity_id=node_id,
            entity_type=SyncInventoryEntityTypeEnum.NODE,
            status=status,
            sync_instance_id=instance.id,
            created_at=at,
            updated_at=at,
        )
    )
    await session.commit()


NODE = SyncInventoryEntityTypeEnum.NODE
INVENTORY = SyncInventoryEntityTypeEnum.INVENTORY
FIRST_NODE_ID = MOCK_CREATED_NODE_ID + 1


class TestUnmeasuredHostFactsPass:
    """Test a first-measurement pass end to end against the real sync ledger."""

    def test_has_its_own_schedule_and_run_lock_name(self):
        """Name the pass apart from the daily syncer it extends."""
        assert UnmeasuredHostFactsSyncer.get_name() == (
            "app.extensions.sync.syncers.system_facts.syncer.UnmeasuredHostFactsSyncer"
        )

    @pytest.mark.asyncio
    async def test_measures_a_new_host(
        self, session, inventory_api, tasks_api, dispatch, fake_inventory
    ):
        """Dispatch once for a new host, write its observation and record SUCCESS."""
        run_id = await _run_pass(session, inventory_api, tasks_api)

        dispatch.assert_awaited_once()
        assert fake_inventory.observed == {FIRST_NODE_ID}
        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    async def test_leaves_a_measured_host_to_the_daily_run(
        self, session, inventory_api, tasks_api, dispatch, fake_inventory
    ):
        """Skip a host with an observation, even one whose ``can_elevate`` is null."""
        fake_inventory.observed.add(FIRST_NODE_ID)

        run_id = await _run_pass(session, inventory_api, tasks_api)

        dispatch.assert_not_awaited()
        assert await _statuses(session, run_id, NODE) == {}
        assert await _statuses(session, run_id, INVENTORY) == {
            None: SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            json.dumps({"host": None, "services": {}}),
            json.dumps({"host": {"collected_at": COLLECTED_AT}, "services": {}}),
        ],
        ids=["no-host-facts", "no-usable-host-fact"],
    )
    async def test_an_attempt_that_writes_nothing_fails_and_waits(
        self, session, inventory_api, tasks_api, dispatch, alerts, stdout
    ):
        """Record a fact-less probe as FAILED, alert, and hold the retry an hour."""
        dispatch.return_value = TaskRunResult(1, stdout)

        run_id = await _run_pass(session, inventory_api, tasks_api)
        await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.FAILED
        }
        alerts.assert_awaited_once()
        dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_rejected_observation_write_fails_the_attempt(
        self, session, inventory_api, tasks_api, dispatch
    ):
        """Record an attempt whose observation PUT failed as FAILED."""
        inventory_api.put.side_effect = OSError("inventory unreachable")

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.FAILED
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("hosts", "syncer_options"),
        [
            ({EXECUTOR_NAME: EXECUTOR_ADDRESS}, {}),
            ({EXECUTOR_NAME: EXECUTOR_ADDRESS}, {"strict_executor_matching": True}),
            ({}, {}),
        ],
        ids=["not-co-located", "strict-unmatched", "no-executor-hosts"],
    )
    async def test_never_probes_a_host_it_cannot_measure(
        self, session, inventory_api, tasks_api, dispatch, hosts, syncer_options
    ):
        """Exclude nodes without a co-located executor, and every node without hosts."""
        tasks_api.get.return_value = hosts

        run_id = await _run_pass(session, inventory_api, tasks_api, **syncer_options)

        dispatch.assert_not_awaited()
        assert await _statuses(session, run_id, NODE) == {}
        assert await _statuses(session, run_id, INVENTORY) == {
            None: SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pages", "error"),
        [
            ([HTTPBadGatewayException("inventory unreachable")], HTTPException),
            ([None], ValidationError),
            ([{}], ValidationError),
            ([{"total": 0, "offset": 0, "limit": 200}], ValidationError),
            (
                [
                    _page([_observation(FIRST_NODE_ID)], total=2),
                    _page([{"node_id": "not-an-id"}], total=2),
                ],
                ValidationError,
            ),
            (
                [
                    _page([_observation(FIRST_NODE_ID + 1)], total=3),
                    _page([], total=3),
                ],
                IncompleteObservationsReadError,
            ),
        ],
        ids=[
            "transport-error",
            "none-page",
            "empty-dict",
            "no-items",
            "malformed-second-page",
            "short-of-total",
        ],
    )
    async def test_an_unreadable_observation_list_dispatches_nothing(
        self, session, tasks_api, dispatch, alerts, pages, error
    ):
        """Fail closed: an untrustworthy observation read fails the pass, probing none.

        ``break_on_error`` surfaces the failure so its cause can be asserted.
        """
        fake = FakeInventory([_node(1)], observation_pages=pages)
        inventory_api = AsyncMock(spec=RemoteAPI)
        inventory_api.get.side_effect = fake.get
        with pytest.raises(SyncFailError) as failure:
            await _run_pass(session, inventory_api, tasks_api, break_on_error=True)

        assert isinstance(failure.value.__cause__, error)
        dispatch.assert_not_awaited()
        alerts.assert_awaited_once()
        (run,) = await SyncInstanceManager.list(
            session, syncer=UnmeasuredHostFactsSyncer.get_name()
        )
        assert await _statuses(session, run.id, INVENTORY) == {
            None: SyncStatusEnum.FAILED
        }
        assert await _statuses(session, run.id, NODE) == {}

    @pytest.mark.asyncio
    async def test_reads_every_observation_page(self, session, tasks_api, dispatch):
        """Walk past the first page, so a host observed on page two is skipped."""
        fake = FakeInventory(
            [_node(1), _node(2)],
            observation_pages=[
                _page([_observation(FIRST_NODE_ID + 5)], total=2),
                _page([_observation(FIRST_NODE_ID + 1)], total=2),
            ],
        )
        inventory_api = AsyncMock(spec=RemoteAPI)
        inventory_api.get.side_effect = fake.get
        inventory_api.put.side_effect = fake.put

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "dispatched"),
        [(SyncStatusEnum.FAILED, False), (SyncStatusEnum.RUNNING, True)],
        ids=["daily-attempt-finished", "daily-attempt-in-flight"],
    )
    async def test_the_window_counts_the_daily_syncers_finished_attempts(
        self, session, inventory_api, tasks_api, dispatch, status, dispatched
    ):
        """Hold a host the daily run just finished probing; ignore one in flight."""
        await _record_attempt(
            session, SystemFactsSyncer, FIRST_NODE_ID, status, timedelta(minutes=10)
        )

        await _run_pass(session, inventory_api, tasks_api)

        assert dispatch.await_count == int(dispatched)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failures", "dispatched"),
        [(3, True), (4, False)],
        ids=["first-and-two-retries-failed", "first-and-three-retries-failed"],
    )
    async def test_the_retry_cap_is_read_from_the_ledger(
        self, session, inventory_api, tasks_api, dispatch, failures, dispatched
    ):
        """Stop attempting a host once its first attempt and three retries failed."""
        for _ in range(failures):
            await _record_attempt(
                session,
                UnmeasuredHostFactsSyncer,
                FIRST_NODE_ID,
                SyncStatusEnum.FAILED,
                2 * HOUR,
            )

        await _run_pass(session, inventory_api, tasks_api)

        assert dispatch.await_count == int(dispatched)

    @pytest.mark.asyncio
    async def test_a_success_in_the_ledger_clears_the_failure_count(
        self, session, inventory_api, tasks_api, dispatch
    ):
        """Attempt a host again when a success followed its capped failures."""
        for _ in range(4):
            await _record_attempt(
                session,
                UnmeasuredHostFactsSyncer,
                FIRST_NODE_ID,
                SyncStatusEnum.FAILED,
                5 * HOUR,
            )
        await _record_attempt(
            session,
            UnmeasuredHostFactsSyncer,
            FIRST_NODE_ID,
            SyncStatusEnum.SUCCESS,
            3 * HOUR,
        )

        await _run_pass(session, inventory_api, tasks_api)

        dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_cap_counts_only_this_hosts_own_failures(
        self, session, inventory_api, tasks_api, dispatch, fake_inventory
    ):
        """Count only a host's own failures, not another host's or the daily run's."""
        fake_inventory.nodes.append(_node(2))
        for _ in range(4):
            await _record_attempt(
                session,
                UnmeasuredHostFactsSyncer,
                FIRST_NODE_ID,
                SyncStatusEnum.FAILED,
                2 * HOUR,
            )
            await _record_attempt(
                session,
                SystemFactsSyncer,
                FIRST_NODE_ID + 1,
                SyncStatusEnum.FAILED,
                2 * HOUR,
            )

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID + 1: SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    async def test_a_host_added_later_is_measured_by_the_next_pass(
        self, session, inventory_api, tasks_api, dispatch, fake_inventory
    ):
        """Probe only the new host once the first one has been measured."""
        await _run_pass(session, inventory_api, tasks_api)
        fake_inventory.nodes.append(_node(2))

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert [probe.kwargs["target"] for probe in dispatch.await_args_list] == [
            "probe-host-1",
            "probe-host-2",
        ]
        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID + 1: SyncStatusEnum.SUCCESS
        }


class TestConcurrentFirstMeasurement:
    """Test that a pass dispatches its candidates together rather than in turn."""

    @pytest.mark.asyncio
    async def test_every_new_host_is_dispatched_before_any_probe_ends(
        self, session, inventory_api, tasks_api, fake_inventory, mocker
    ):
        """Start all three probes before the first returns."""
        fake_inventory.nodes[:] = [_node(index) for index in range(1, 4)]
        targets: list[str] = []
        release = asyncio.Event()

        async def probe(self, **meta: Any) -> TaskRunResult:
            targets.append(meta["target"])
            if len(targets) == len(fake_inventory.nodes):
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            return TaskRunResult(1, MEASURED_STDOUT)

        mocker.patch.object(UnmeasuredHostFactsSyncer, "wait_for_task_output", probe)

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert sorted(targets) == ["probe-host-1", "probe-host-2", "probe-host-3"]
        assert set((await _statuses(session, run_id, NODE)).values()) == {
            SyncStatusEnum.SUCCESS
        }

    @pytest.mark.asyncio
    async def test_at_most_the_concurrency_cap_is_in_flight(
        self, session, inventory_api, tasks_api, fake_inventory, mocker
    ):
        """Hold exactly eight probes in flight, then finish all ten."""
        fake_inventory.nodes[:] = [_node(index) for index in range(1, 11)]
        cap = UnmeasuredHostFactsSyncer.FIRST_MEASUREMENT_CONCURRENCY
        in_flight = 0
        peak = 0
        release = asyncio.Event()

        async def probe(self, **meta: Any) -> TaskRunResult:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            if in_flight == cap:
                release.set()
            try:
                await asyncio.wait_for(release.wait(), timeout=1)
            finally:
                in_flight -= 1
            return TaskRunResult(1, MEASURED_STDOUT)

        mocker.patch.object(UnmeasuredHostFactsSyncer, "wait_for_task_output", probe)

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert peak == cap
        statuses = await _statuses(session, run_id, NODE)
        assert len(statuses) == len(fake_inventory.nodes)
        assert set(statuses.values()) == {SyncStatusEnum.SUCCESS}

    @pytest.mark.asyncio
    async def test_a_host_is_recorded_while_slower_probes_run(
        self, session, inventory_api, tasks_api, fake_inventory, mocker
    ):
        """Write a fast host's observation before a slower probe returns.

        The slow probe waits for the fast host's observation, so a pass that
        recorded only after its last probe would time that probe out.
        """
        fake_inventory.nodes[:] = [_node(1), _node(2)]
        first_written = asyncio.Event()

        async def put(path: str, **kwargs: Any) -> dict[str, Any]:
            written = await fake_inventory.put(path, **kwargs)
            first_written.set()
            return written

        inventory_api.put.side_effect = put

        async def probe(self, **meta: Any) -> TaskRunResult:
            if meta["target"] == "probe-host-2":
                await asyncio.wait_for(first_written.wait(), timeout=1)
            return TaskRunResult(1, MEASURED_STDOUT)

        mocker.patch.object(UnmeasuredHostFactsSyncer, "wait_for_task_output", probe)

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.SUCCESS,
            FIRST_NODE_ID + 1: SyncStatusEnum.SUCCESS,
        }

    @pytest.mark.asyncio
    async def test_an_interrupted_pass_charges_no_host_it_did_not_record(
        self, session, inventory_api, tasks_api, fake_inventory, mocker
    ):
        """Leave no ledger row for hosts still queued or in flight at an interruption.

        Ten candidates exceed the concurrency cap, so two never start; the first
        host's failure stops the pass under ``break_on_error`` while the others'
        probes are still running.
        """
        fake_inventory.nodes[:] = [_node(index) for index in range(1, 11)]

        async def probe(self, **meta: Any) -> TaskRunResult:
            if meta["target"] == "probe-host-1":
                raise TimeoutError("Task run-python timed out")
            await asyncio.Event().wait()
            return TaskRunResult(1, MEASURED_STDOUT)

        mocker.patch.object(UnmeasuredHostFactsSyncer, "wait_for_task_output", probe)

        with pytest.raises(SyncFailError):
            await _run_pass(session, inventory_api, tasks_api, break_on_error=True)

        (run,) = await SyncInstanceManager.list(
            session, syncer=UnmeasuredHostFactsSyncer.get_name()
        )
        assert await _statuses(session, run.id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.FAILED
        }

    @pytest.mark.asyncio
    async def test_one_failing_probe_fails_only_its_own_host(
        self, session, inventory_api, tasks_api, fake_inventory, mocker
    ):
        """Record a timed-out probe against its node and keep the others."""
        fake_inventory.nodes[:] = [_node(index) for index in range(1, 4)]

        async def probe(self, **meta: Any) -> TaskRunResult:
            if meta["target"] == "probe-host-2":
                raise TimeoutError("Task run-python timed out")
            return TaskRunResult(1, MEASURED_STDOUT)

        mocker.patch.object(UnmeasuredHostFactsSyncer, "wait_for_task_output", probe)

        run_id = await _run_pass(session, inventory_api, tasks_api)

        assert await _statuses(session, run_id, NODE) == {
            FIRST_NODE_ID: SyncStatusEnum.SUCCESS,
            FIRST_NODE_ID + 1: SyncStatusEnum.FAILED,
            FIRST_NODE_ID + 2: SyncStatusEnum.SUCCESS,
        }
