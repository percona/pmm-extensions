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

"""Implement the system facts inventory sync.

The :class:`SystemFactsSyncer` enriches existing inventory entities with system facts
(OS version, installed packages, EOL-relevant config, and DB engine version) collected
by a task-based payload executed on the co-located executor host -- the same mechanism
as ``MySQLSyncer``. Facts are persisted as host/service *system observations* through the
inventory HTTP API; the syncer never creates or deletes nodes or services.
"""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any, ClassVar

from app.core.pagination import fetch_all_items, PaginatedResponse, Pagination
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.extensions.crud import SyncItemManager
from app.extensions.inventory import (
    CreatedEntity,
    CreatedEntityBase,
    CreatedNode,
    CreatedService,
    Node,
    Service,
)
from app.extensions.models import SyncInventoryEntityTypeEnum, SyncItem, SyncStatusEnum
from app.extensions.sync.exceptions import (
    ExecutorHostNotFoundError,
    HostNotMeasuredError,
    IncompleteObservationsReadError,
)
from app.extensions.sync.models import BaseTaskSyncer, TaskRunResult
from app.inventory.models import (
    HOST_OBSERVATION_FIELD_NAMES,
    HostSystemObservationSummaryResponse,
    HostSystemObservationWrite,
    ServiceSystemObservationWrite,
    ServiceTypeEnum,
)

logger = logging.getLogger(__name__)


class SystemFactsService(Service):
    """Carry the collected engine version for a service through the sync lifecycle.

    Extends the base :class:`~app.extensions.inventory.Service` with the facts gathered by the
    payload so that ``fetch_service`` can hand them to ``perform_service_sync``.

    :param db_engine_version: The observed database engine version, if collected.
    :type db_engine_version: str | None
    :param collected_at: The provenance timestamp emitted by the payload (UTC ISO-8601).
    :type collected_at: str | None
    """

    db_engine_version: str | None = None
    collected_at: str | None = None


class SystemFactsSyncer(BaseTaskSyncer):
    """Collect host and service system facts and upsert them as inventory observations.

    The base ``BaseSyncer`` lifecycle drives this syncer: ``perform_inventory_sync``
    iterates existing inventory nodes, and for each node a single payload run collects
    host facts plus every co-located service's engine version. Results are cached so the
    per-service lifecycle steps do not trigger additional task runs.

    :cvar SYNC_TO_LIMIT: The highest entity type synchronized. Set to
        ``SyncInventoryEntityTypeEnum.SERVICE`` (no schema/table recursion).
    :cvar mirrors_entity_levels: Empty, stated rather than inherited. This syncer
        walks nodes and services but writes a separate observation resource
        instead of their own fields, so refreshing either level's freshness would
        report a confirmation it never made — and would mask a failing PMM mirror
        of the same row.
    :cvar EOL_ENGINE_TYPES: Service types whose engine version is collected.
    """

    SYNC_TO_LIMIT: ClassVar[SyncInventoryEntityTypeEnum] = (
        SyncInventoryEntityTypeEnum.SERVICE
    )
    mirrors_entity_levels: ClassVar[frozenset[SyncInventoryEntityTypeEnum]] = (
        frozenset()
    )
    EOL_ENGINE_TYPES: ClassVar[frozenset[ServiceTypeEnum]] = frozenset(
        {
            ServiceTypeEnum.MYSQL,
            ServiceTypeEnum.POSTGRESQL,
            ServiceTypeEnum.MONGODB,
        }
    )
    _host_facts_cache: dict[int, dict[str, Any]] = {}
    _service_facts_cache: dict[int, dict[str, Any]] = {}
    #: Service ids probed by a ``fetch_node`` batch; lets ``fetch_service`` skip a
    #: batch-failed service instead of re-running a standalone task for it.
    _batch_attempted_services: set[int] = set()

    @property
    def payload_path(self) -> Path:
        """Determine the path to the payload script.

        :return: The ``Path`` to the ``payload.py`` collection script.
        :rtype: Path
        """
        return Path(__file__).parent / "payload.py"

    def build_script_config(
        self,
        services: list[CreatedService],
        *,
        collect_host: bool,
    ) -> str:
        """Build the JSON configuration for the collection payload.

        :param services: The DB engine services whose version should be probed.
        :type services: list[CreatedService]
        :param collect_host: Whether the payload should collect host-level facts (only
            true when the executor is co-located with the node).
        :type collect_host: bool
        :return: A JSON string describing the collection targets.
        :rtype: str
        """
        config = {
            "services": [
                {"address": service.address, "type": service.type}
                for service in services
            ],
            "collect_host": collect_host,
        }
        return json.dumps(config)

    async def build_meta(self, config: str, target: str) -> dict[str, str]:
        """Build metadata for task execution.

        :param config: The JSON configuration for the payload script.
        :type config: str
        :param target: The executor host on which to run the task.
        :type target: str
        :return: The metadata dictionary for the task execution request.
        :rtype: dict[str, str]
        """
        return {
            "config": config,
            "target": target,
            "requirements": "PyMySQL[rsa,ed25519]\nmyloginpath\npsycopg[binary]\npymongo",
            "_job_id_prefix": "system-facts-sync",
        }

    async def wait_for_task_output(
        self,
        task_name: str = "run-python",
        stdout_step: str = "run-script",
        payload: str | None = None,
        **meta: Any,
    ) -> TaskRunResult:
        """Run the collection payload and wait for its output.

        :param task_name: The root task to execute. Defaults to ``"run-python"``.
        :type task_name: str
        :param stdout_step: The step whose stdout is collected. Defaults to
            ``"run-script"``.
        :type stdout_step: str
        :param payload: The payload reference. Defaults to ``file://`` the payload path.
        :type payload: str | None
        :param meta: Additional metadata forwarded to the task execution request.
        :type meta: Any
        :return: The task execution result.
        :rtype: TaskRunResult
        """
        payload = f"file://{self.payload_path}" if payload is None else payload
        return await super().wait_for_task_output(
            task_name,
            stdout_step,
            payload,
            **meta,
        )

    async def resolve_task_target(
        self, host: str, name: str | None = None
    ) -> tuple[str, bool]:
        """Resolve the executor host and whether it is co-located with the node.

        Mirrors ``get_task_target`` selection but additionally reports co-location: a
        node is co-located when an executor host matches it by name or address. When no
        executor matches (e.g. RDS / managed instances), the task still runs on a
        fallback host but host-level facts must be skipped.

        :param host: The node's network address.
        :type host: str
        :param name: The node's name. Defaults to ``None``.
        :type name: str | None
        :return: A tuple of ``(target_host, is_colocated)``.
        :rtype: tuple[str, bool]
        :raises ExecutorHostNotFoundError: If ``strict_executor_matching`` is enabled and
            no executor host matches the node's name or address.
        """
        # Co-location must reflect the *selected* target, not any matching host, so facts
        # from a forced/fallback executor aren't misattributed to the node.
        available_hosts = await self.get_available_hosts()
        target = await self.get_task_target(host, name)
        is_colocated = target == name or available_hosts.get(target) == host
        return target, is_colocated

    async def perform_inventory_sync(self) -> None:
        """Synchronize system facts for every inventory node."""
        for node in await self.get_inventory_nodes():
            await self.sync_node(node)

    async def fetch_node(self, created_node: CreatedNode) -> Node | None:
        """Collect host and service facts for a node via the task payload.

        Runs the payload once on the resolved executor, caching the host facts (only when
        co-located) and every service's engine version for the per-service lifecycle. The
        node is always returned (never ``None``) so that service facts are still synced
        even when host facts are unavailable.

        :param created_node: The node to collect facts for.
        :type created_node: CreatedNode
        :return: The node (unchanged); facts are stashed in the syncer caches.
        :rtype: Node | None
        """
        services = [
            service
            for service in created_node.services
            if self.can_sync_service(service)
        ]
        target, colocated = await self.resolve_task_target(
            created_node.address, created_node.name
        )
        if not colocated and not services:
            # Nothing to collect: not the node's own host (no host facts) and no DB
            # engine service to probe. Skip the task run entirely.
            return created_node
        config = self.build_script_config(services, collect_host=colocated)
        meta = await self.build_meta(config, target)
        result = await self.wait_for_task_output(**meta)
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "System facts payload returned non-JSON output for node %s",
                created_node.id,
            )
            data = {}
        if not isinstance(data, dict):
            data = {}
        host_facts = data.get("host")
        if isinstance(host_facts, dict) and host_facts:
            self._host_facts_cache[created_node.id] = host_facts
        collected = data.get("services")
        if isinstance(collected, dict):
            # The payload keys facts by address; cache them by the unique service id so a
            # later sibling or failed collection cannot read another service's fact.
            service_by_address = {service.address: service for service in services}
            for address, fact in collected.items():
                service = service_by_address.get(address)
                if service is not None and isinstance(fact, dict):
                    self._service_facts_cache[service.id] = fact
        # Mark probed services (even versionless ones) so fetch_service won't re-collect.
        self._batch_attempted_services.update(service.id for service in services)
        return created_node

    def _build_host_observation(
        self, host_facts: dict[str, Any]
    ) -> HostSystemObservationWrite | None:
        """Build a host observation, or ``None`` if no usable fact was collected.

        Avoids writing a half-empty snapshot: at least one observation field must
        have been collected. Admission is on ``is not None`` rather than truthiness
        because ``can_elevate`` is the first field whose ``False`` is a measurement.
        Dropping it would read back as never-observed, the inverse of what was
        measured.

        :param host_facts: The host facts emitted by the payload.
        :return: A validated host observation, or ``None`` when nothing was collected.
        """
        fields = {
            key: value
            for key in HOST_OBSERVATION_FIELD_NAMES
            if (value := host_facts.get(key)) is not None
        }
        if not fields:
            return None
        return HostSystemObservationWrite(
            observed_at=host_facts.get("collected_at") or datetime.now(UTC),
            **fields,
        )

    async def _upsert_host_observation(self, created_node: CreatedNode) -> bool:
        """Upsert the node's collected host facts as its observation, best-effort.

        :param created_node: The node whose cached host facts to write.
        :return: Whether a host observation was written.
        """
        if (
            not (host_facts := self._host_facts_cache.pop(created_node.id, None))
            or (observation := self._build_host_observation(host_facts)) is None
        ):
            return False
        logger.info("Upserting host system observation for node %s", created_node.id)
        try:
            await self.inventory_api.put(
                f"/nodes/{created_node.id}/system-observation",
                json=observation.model_dump(mode="json", exclude_none=True),
            )
        except Exception:  # noqa: BLE001 - best-effort; must not block service syncs
            logger.warning(
                "Failed to upsert host system observation for node %s",
                created_node.id,
                exc_info=True,
            )
            return False
        return True

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,  # noqa: ARG002 - required by the BaseSyncer interface
    ) -> None:
        """Upsert the host observation (best-effort) and sync each service.

        :param created_node: The node being synchronized.
        :type created_node: CreatedNode
        :param updated_node: The node data returned by ``fetch_node``.
        :type updated_node: Node
        """
        await self._upsert_host_observation(created_node)
        for service in created_node.services:
            await self.sync_service(service)

    async def _collect_single_service(
        self, created_service: CreatedService
    ) -> dict[str, Any]:
        """Collect the engine version for one service via a dedicated payload run.

        Used when ``fetch_service`` is invoked outside a node sync (e.g. a per-service
        sync triggered from the UI), so the ``fetch_node`` cache was never primed. Never
        collects host facts -- a standalone service sync writes only the service
        observation.

        :param created_service: The service to collect the engine version for.
        :type created_service: CreatedService
        :return: The fact dict for the service (``{}`` if collection yielded nothing).
        :rtype: dict[str, Any]
        """
        target, _ = await self.resolve_task_target(
            created_service.node.address, created_service.node.name
        )
        config = self.build_script_config([created_service], collect_host=False)
        meta = await self.build_meta(config, target)
        result = await self.wait_for_task_output(**meta)
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "System facts payload returned non-JSON output for service %s",
                created_service.id,
            )
            return {}
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            return {}
        fact = services.get(created_service.address)
        return fact if isinstance(fact, dict) else {}

    async def fetch_service(
        self, created_service: CreatedService
    ) -> SystemFactsService | None:
        """Resolve the engine version for a service.

        Uses the ``fetch_node`` cache when present (the scheduled-sync path). A standalone
        per-service sync (the service was never primed by a ``fetch_node`` batch) falls
        back to collecting just this service. A service that *was* batch-primed but whose
        version failed is skipped rather than re-collected, so a node with K dead services
        issues one task run, not ``1 + K``.

        :param created_service: The service to fetch facts for.
        :type created_service: CreatedService
        :return: A carrier with the engine version, or ``None`` if none was collected
            (the service is then skipped, never written with an empty version).
        :rtype: SystemFactsService | None
        """
        primed = created_service.id in self._batch_attempted_services
        self._batch_attempted_services.discard(created_service.id)
        fact = self._service_facts_cache.pop(created_service.id, None)
        if (not fact or not fact.get("db_engine_version")) and not primed:
            # Never primed by a batch run -> standalone per-service sync, collect directly.
            fact = await self._collect_single_service(created_service)
        if not fact or not fact.get("db_engine_version"):
            return None
        service = SystemFactsService.model_validate(
            created_service.model_dump(exclude={"node", "schemas"})
        )
        service.db_engine_version = fact["db_engine_version"]
        service.collected_at = fact.get("collected_at")
        return service

    async def perform_service_sync(
        self,
        created_service: CreatedService,
        updated_service: Service,
    ) -> None:
        """Upsert the service engine version as a system observation.

        :param created_service: The service being synchronized.
        :type created_service: CreatedService
        :param updated_service: The carrier returned by ``fetch_service``.
        :type updated_service: Service
        """
        db_engine_version = getattr(updated_service, "db_engine_version", None)
        if not db_engine_version:
            return
        collected_at = getattr(updated_service, "collected_at", None)
        observation = ServiceSystemObservationWrite(
            db_engine_version=db_engine_version,
            observed_at=collected_at or datetime.now(UTC),
        )
        logger.info(
            "Upserting service system observation for service %s", created_service.id
        )
        await self.inventory_api.put(
            f"/services/{created_service.id}/system-observation",
            json=observation.model_dump(mode="json", exclude_none=True),
        )

    @classmethod
    def can_sync_service(cls, service: CreatedService) -> bool:
        """Determine if a service is a collectable DB engine type.

        :param service: The service to check.
        :type service: CreatedService
        :return: ``True`` if the service is MySQL, PostgreSQL, or MongoDB.
        :rtype: bool
        """
        return (
            super().can_sync_service(service) and service.type in cls.EOL_ENGINE_TYPES
        )


def _finished_at(item: SyncItem) -> datetime:
    """Return when a finished attempt reached its final status, in UTC.

    :param item: A ``SUCCESS`` or ``FAILED`` item.
    :return: Its last write time, or its creation time if it was never updated.
    """
    return make_datetime_utc(item.updated_at or item.created_at)


def first_measurement_due(
    own_attempts: Sequence[SyncItem],
    latest_finish: datetime | None,
    now: datetime,
    *,
    retries: int,
    retry_interval: timedelta,
) -> bool:
    """Return whether a never-measured host is due a measurement attempt.

    The failure count restarts at every success, so only the failures after the
    newest one count against ``retries``.

    :param own_attempts: The first-measurement syncer's finished attempts on the
        host, oldest first.
    :param latest_finish: When the newest finished host-facts attempt on the host
        ended, by either syncer, or ``None`` if there is none.
    :param now: The current time, timezone-aware.
    :param retries: The retries allowed after a failed first attempt.
    :param retry_interval: The least time between two attempts.
    :return: Whether an attempt is due now.
    """
    failures = 0
    for attempt in own_attempts:
        if attempt.status == SyncStatusEnum.SUCCESS:
            failures = 0
        elif attempt.status == SyncStatusEnum.FAILED:
            failures += 1
    if failures > retries:
        return False
    return latest_finish is None or now - latest_finish >= retry_interval


class UnmeasuredHostFactsSyncer(SystemFactsSyncer):
    """Give each never-measured host its first measurement between daily runs.

    Probes only active nodes that are co-located with an executor and have no host
    observation, so a run with nothing new dispatches no task. A host whose attempt
    writes no observation is retried at most once per
    ``FIRST_MEASUREMENT_RETRY_INTERVAL`` for at most ``FIRST_MEASUREMENT_RETRIES``
    retries, then left to the daily ``SystemFactsSyncer`` refresh. The probes run
    concurrently, so a host does not wait behind the others' probes; every ledger
    write stays serial on the run's one session.

    :cvar FIRST_MEASUREMENT_RETRIES: The retries after a failed first attempt.
    :cvar FIRST_MEASUREMENT_RETRY_INTERVAL: The least time between two attempts on
        one host, counting either host-facts syncer's finished attempts.
    :cvar FIRST_MEASUREMENT_CONCURRENCY: The most first-measurement probes in
        flight at once.
    """

    FIRST_MEASUREMENT_RETRIES: ClassVar[int] = 3
    FIRST_MEASUREMENT_RETRY_INTERVAL: ClassVar[timedelta] = timedelta(hours=1)
    FIRST_MEASUREMENT_CONCURRENCY: ClassVar[int] = 8
    #: Each candidate's probe outcome, gathered before the serial ledger walk and
    #: consumed by ``fetch_node``.
    _prefetched: dict[int | None, Node | BaseException | None] = {}

    async def get_observed_node_ids(self) -> set[int]:
        """Return the ids of every node that has a host observation.

        Fails closed: an answer smaller than the inventory holds would make measured
        hosts look new and probe them again, so every page is validated strictly and
        a walk that ends short of the greatest total any page reported raises.

        :return: The observed node ids.
        :raises pydantic.ValidationError: If a page is not a valid observation page.
        :raises IncompleteObservationsReadError: If fewer rows were read than the
            inventory reported.
        :raises fastapi.HTTPException: If the inventory API request fails.
        """
        total = 0

        async def get_page(
            pagination: Pagination,
        ) -> PaginatedResponse[HostSystemObservationSummaryResponse]:
            nonlocal total
            page = PaginatedResponse[
                HostSystemObservationSummaryResponse
            ].model_validate(
                await self.inventory_api.get(
                    "/nodes/system-observations", params=pagination.model_dump()
                )
            )
            total = max(total, page.total)
            return page

        observations = await fetch_all_items(get_page)
        if len(observations) < total:
            raise IncompleteObservationsReadError(len(observations), total)
        return {observation.node_id for observation in observations}

    async def is_measurable(self, node: CreatedNode) -> bool:
        """Return whether a probe of ``node`` can collect its host facts.

        :param node: The node to check.
        :return: Whether the executor selected for the node is co-located with it.
        """
        try:
            _, colocated = await self.resolve_task_target(node.address, node.name)
        except ExecutorHostNotFoundError:
            return False
        return colocated

    async def _due_by_ledger(self, nodes: list[CreatedNode]) -> list[CreatedNode]:
        """Return the nodes the retry policy lets this run attempt.

        :param nodes: The unmeasured, measurable nodes.
        :return: Those due an attempt now, in the given order.
        """
        node_ids = [node.id for node in nodes]
        own = await SyncItemManager.finished_entity_attempts(
            self._session, self.get_name(), SyncInventoryEntityTypeEnum.NODE, node_ids
        )
        daily = await SyncItemManager.finished_entity_attempts(
            self._session,
            SystemFactsSyncer.get_name(),
            SyncInventoryEntityTypeEnum.NODE,
            node_ids,
        )
        own_by_node: defaultdict[int | None, list[SyncItem]] = defaultdict(list)
        for item in own:
            own_by_node[item.entity_id].append(item)
        latest_finish: dict[int | None, datetime] = {}
        for item in (*own, *daily):
            finished = _finished_at(item)
            latest_finish[item.entity_id] = max(
                finished, latest_finish.get(item.entity_id, finished)
            )
        now = utc_now()
        return [
            node
            for node in nodes
            if first_measurement_due(
                own_by_node[node.id],
                latest_finish.get(node.id),
                now,
                retries=self.FIRST_MEASUREMENT_RETRIES,
                retry_interval=self.FIRST_MEASUREMENT_RETRY_INTERVAL,
            )
        ]

    async def get_unmeasured_candidates(self) -> list[CreatedNode]:
        """Return the nodes this run probes for their first measurement.

        Reads the run's session, so it is only callable inside ``async with``.

        :return: Active, co-located nodes without a host observation that the
            retry policy lets this run attempt.
        :raises pydantic.ValidationError: If an observation page is invalid.
        :raises IncompleteObservationsReadError: If the observation read came up
            short of the inventory's total.
        :raises fastapi.HTTPException: If an inventory or tasks API request fails.
        """
        if not self.force_executor_host and not await self.get_available_hosts():
            logger.info("No executor hosts available; no host can be measured")
            return []
        nodes = await self.get_inventory_nodes()
        observed = await self.get_observed_node_ids()
        unmeasured = [node for node in nodes if node.id not in observed]
        measurable = [node for node in unmeasured if await self.is_measurable(node)]
        candidates = await self._due_by_ledger(measurable)
        logger.info(
            "First host measurement: %d candidate(s); skipped %d measured, "
            "%d without a co-located executor, %d held by the retry policy",
            len(candidates),
            len(nodes) - len(unmeasured),
            len(unmeasured) - len(measurable),
            len(measurable) - len(candidates),
        )
        return candidates

    async def get_children_entities(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity | None,
    ) -> Sequence[CreatedEntityBase]:
        """Open no node item up front, leaving each one to be opened as it is recorded.

        A node item counts against the host's retry budget once it is finished, and
        an interrupted run fails every item it left open. Opening an item only when
        the host's probe has ended keeps a run that stops early from charging the
        hosts it never probed.

        :param entity_type: The type of the current entity.
        :param created_entity: The current entity, or ``None`` at inventory level.
        :return: No children at inventory level; the base children otherwise.
        """
        if entity_type == SyncInventoryEntityTypeEnum.INVENTORY:
            return []
        return await super().get_children_entities(entity_type, created_entity)

    async def _probe(self, node: CreatedNode, limit: asyncio.Semaphore) -> Node | None:
        """Run the daily syncer's probe for ``node`` within the concurrency cap.

        Calls ``SystemFactsSyncer.fetch_node`` explicitly because this class's own
        ``fetch_node`` replays the outcome this probe produces.

        :param node: The candidate to probe.
        :param limit: The semaphore bounding the probes in flight.
        :return: What the probe returned.
        """
        async with limit:
            return await SystemFactsSyncer.fetch_node(self, node)

    async def perform_inventory_sync(self) -> None:
        """Probe every candidate concurrently, recording each one as its probe ends.

        Recording on completion rather than after the last probe keeps the run's
        items progressing, so the stale-run reclaim never mistakes a long pass for an
        abandoned one, and a host's observation is written as soon as it is measured.
        The recording itself stays on this coroutine, the only one using the session.
        A probe still in flight when the walk is interrupted is cancelled and
        awaited before the method returns.
        """
        candidates = await self.get_unmeasured_candidates()
        limit = asyncio.Semaphore(self.FIRST_MEASUREMENT_CONCURRENCY)
        pending = {
            asyncio.create_task(self._probe(node, limit)): node for node in candidates
        }
        try:
            while pending:
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    node = pending.pop(task)
                    self._prefetched[node.id] = task.exception() or task.result()
                    await self.sync_node(node)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def fetch_node(self, created_node: CreatedNode) -> Node | None:
        """Replay the node's probe outcome, raising the error a failed probe raised.

        A node with no recorded outcome, as in a per-node sync, is probed directly,
        outside the candidate filter and the retry policy.

        :param created_node: The node to collect facts for.
        :return: The node, as the probe returned it.
        :raises Exception: Whatever the node's probe raised, such as the
            ``TimeoutError`` or ``ValueError`` of a timed-out or failed task.
        """
        if created_node.id not in self._prefetched:
            return await super().fetch_node(created_node)
        outcome = self._prefetched.pop(created_node.id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def perform_node_sync(
        self,
        created_node: CreatedNode,
        updated_node: Node,  # noqa: ARG002 - required by the BaseSyncer interface
    ) -> None:
        """Upsert the host observation and sync each service, failing if none was written.

        :param created_node: The node being synchronized.
        :param updated_node: The node data returned by ``fetch_node``.
        :raises HostNotMeasuredError: If no host observation was written.
        """
        measured = await self._upsert_host_observation(created_node)
        for service in created_node.services:
            await self.sync_service(service)
        if not measured:
            raise HostNotMeasuredError(created_node.id)
