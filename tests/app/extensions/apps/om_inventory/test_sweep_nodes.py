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

"""Test the per-host receipt a sweep writes.

The counters answer "5 of 14 answered"; the receipt answers "which five, on which
host, and why did that one take a minute". It is the only place a sweep says where
it probed, so what it claims has to survive the cases that make it interesting: a
service with no executor, a host whose probe failed, and a service PMM does not know.
"""

from contextlib import nullcontext
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError

from app.extensions.apps.om_inventory.dispatch import HostProbeResult
from app.extensions.apps.om_inventory.enumeration import InventoryHost
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import ExecutorState, MappedService
from app.extensions.apps.om_inventory.models import NodeResolution
from app.extensions.apps.om_inventory.service import (
    enumerate_estate,
    STARTUP_RETRIES,
    sweep,
)
from tests.app.extensions.apps.om_inventory.conftest import FREE_BYTES

OBSERVED_AT = "2026-08-12T12:00:00+00:00"

#: A host's wall-clock, as the dispatcher would have measured it.
HOST_SECONDS = 12.5
#: Two services on one host, the smallest case that can show a duration being
#: repeated per service rather than reported once for the dispatch.
TWO_SERVICES = 2
#: A failing host's, which is the case where the number matters most.
FAILED_HOST_SECONDS = 61.0
#: One dispatch's, shared by every service that host served.
SHARED_HOST_SECONDS = 8.25
#: A dispatched host's task history id, as the dispatcher would have returned it.
TASK_HISTORY_ID = 4711
#: One refused connection then an answer: the cold start this workspace measured.
RETRIED_ONCE = 2

#: A probe record shaped like the payload's NDJSON, trimmed to the fields asserted.
RECORD: dict[str, Any] = {
    "binary_version": "7.0.39-21",
    "database": {"db_version": "7.0.39-21", "storage_engine": "wiredTiger"},
    "process": {"config_path": "/etc/mongod.conf", "argv": "mongod --config ..."},
    "system": {"os_name": "Ubuntu 24.04.3 LTS", "kernel": "6.17.0-35-generic"},
}


#: PMM's service UUID :func:`make_service` and :func:`mapped` default to. Shared as a
#: constant because it is also what dispatch.py's ``record_key`` now keys a probe
#: result's records on, so a ``HostProbeResult.records`` fixture keyed by name alone
#: would never be found by ``_record_for`` for a service using this default.
DEFAULT_EXTERNAL_ID = "ff0275b6-3633-474a-8068-3c39d3c7a4da"


def make_service(
    name: str, external_id: str | None = DEFAULT_EXTERNAL_ID
) -> InventoryService:
    """Build an inventory service.

    :param name: The service name.
    :param external_id: PMM's service UUID, or ``None`` where inventory holds none.
    :return: The service.
    """
    return InventoryService(
        service_id=1,
        external_id=external_id,
        name=name,
        port=27017,
        node_name="node00",
        node_address="10.0.0.1",
    )


def mapped(
    name: str,
    host: str | None,
    resolution: NodeResolution,
    external_id: str | None = DEFAULT_EXTERNAL_ID,
) -> MappedService:
    """Pair a service with the executor host resolved for it.

    :param name: The service name.
    :param host: The executor host, or ``None``.
    :param resolution: How that host was matched.
    :param external_id: PMM's service UUID.
    :return: The mapped service.
    """
    return MappedService(
        service=make_service(name, external_id),
        executor_host=host,
        resolution=resolution,
    )


def host(name: str, *, orphaned: bool = False) -> InventoryHost:
    """Build one enumerated host.

    The receipt is host-oriented, so these tests have to supply hosts: a sweep that
    enumerated nothing attempted nothing, and its receipt is empty however many
    services were mapped.

    :param name: The host's name, which is also its node id here.
    :param orphaned: Whether no executor matched it, so nothing could run there.
    :return: The host.
    """
    return InventoryHost(
        node_id=name,
        name=name,
        address=None,
        executor_host=None if orphaned else name,
        resolution=NodeResolution.ORPHANED if orphaned else NodeResolution.NAME,
        executor_state=None
        if orphaned
        else ExecutorState(name, "10.0.0.1", reachable=True, driver_healthy=True),
    )


async def run_sweep(
    mapped_services: list[MappedService],
    host_results: dict[str, HostProbeResult],
    hosts: list[InventoryHost] | None = None,
):
    """Run a sweep with the mapping, hosts and probe results stubbed.

    Everything above the mapping is I/O — two authenticated clients, the inventory
    and Nomad — so it is replaced wholesale; what is under test is what the sweep
    concludes from a mapping and its probe results.

    :param mapped_services: The mapping the sweep should see.
    :param host_results: The probe results, keyed by executor host.
    :param hosts: The enumerated hosts. Defaults to one per executor the results
        mention, which is what the real enumeration would have produced for them.
    :return: The sweep's outcome.
    """
    if hosts is None:
        hosts = [host(name) for name in host_results]
    # `auth` is a sync context manager setting a header for its block, so the stub
    # clients have to be usable in a `with`, not merely present.
    clients = (MagicMock(), MagicMock())
    for client in clients:
        client.auth.return_value = nullcontext()

    base = "app.extensions.apps.om_inventory.service"
    with (
        patch(f"{base}._build_clients", AsyncMock(return_value=clients)),
        patch(f"{base}.get_internal_token", return_value="token"),
        patch(f"{base}.list_mongodb_services", AsyncMock(return_value=[])),
        # The host half of enumeration, stubbed empty for the same reason as the
        # service half: these tests are about what a sweep concludes from a mapping,
        # and the hosts it would write have their own tests in test_enumeration.py.
        patch(f"{base}.list_inventory_nodes", AsyncMock(return_value=[])),
        patch(f"{base}.build_hosts", return_value=hosts),
        patch(f"{base}.get_executor_states", AsyncMock(return_value={})),
        patch(f"{base}.map_services", return_value=mapped_services),
        patch(f"{base}.probe_all", AsyncMock(return_value=host_results)),
    ):
        return await sweep(OBSERVED_AT)


@pytest.mark.asyncio
async def test_records_the_host_it_probed_and_the_services_on_it() -> None:
    """Name the host, how it was matched, how long it took, and what was on it."""
    outcome = await run_sweep(
        [mapped("svc-a", "node00", NodeResolution.NAME)],
        {
            "node00": HostProbeResult(
                executor_host="node00",
                host_record={"os": "Ubuntu 24.04"},
                # Keyed by PMM's service id, as dispatch.py's record_key produces —
                # not by the name "svc-a" the mapping carries.
                records={DEFAULT_EXTERNAL_ID: RECORD},
                duration_seconds=HOST_SECONDS,
            )
        },
    )

    assert outcome.answered == 1
    assert len(outcome.nodes) == 1
    node = outcome.nodes[0]
    assert node["host_name"] == "node00"
    assert node["executor_host"] == "node00"
    assert node["resolution"] == NodeResolution.NAME
    assert node["answered"] is True
    assert node["duration_seconds"] == HOST_SECONDS
    assert node["error"] is None
    assert [s["service_name"] for s in node["services"]] == ["svc-a"]
    assert node["services"][0]["answered"] is True


@pytest.mark.asyncio
async def test_an_answering_service_s_role_is_classified() -> None:
    """Classify an answering service's role from the record, keyed by PMM's id."""
    record = {**RECORD, "process": {**RECORD["process"], "running": True}}
    outcome = await run_sweep(
        [mapped("svc-a", "node00", NodeResolution.NAME)],
        {
            "node00": HostProbeResult(
                executor_host="node00", records={DEFAULT_EXTERNAL_ID: record}
            )
        },
    )

    assert outcome.service_roles == {DEFAULT_EXTERNAL_ID: "mongod"}


@pytest.mark.asyncio
async def test_a_host_with_no_database_is_in_the_receipt() -> None:
    """Include a host with no database, which a flat service list could not show.

    A machine carrying a PMM client and no database is what OM most exists to
    describe, and a service-oriented receipt omitted it however many times it was
    probed - a reader saw the counters include it and could not find it.
    """
    outcome = await run_sweep(
        [],
        {
            "pmm-client-node00": HostProbeResult(
                executor_host="pmm-client-node00",
                host_record={"os": "Ubuntu 24.04"},
                duration_seconds=HOST_SECONDS,
            )
        },
    )

    assert len(outcome.nodes) == 1
    node = outcome.nodes[0]
    assert node["host_name"] == "pmm-client-node00"
    assert node["answered"] is True
    # Empty is the answer, not a gap: there is no database here.
    assert node["services"] == []


@pytest.mark.asyncio
async def test_a_dispatched_host_s_receipt_carries_its_task_history_id() -> None:
    """Carry the task history id a follow-up needs to stop an abandoned run's allocations.

    The receipt stays outcomes-only, but the task history id is how a reader still
    reaches the raw output of an attempt the receipt itself does not keep.
    """
    outcome = await run_sweep(
        [],
        {
            "node00": HostProbeResult(
                executor_host="node00",
                host_record={"os": "Ubuntu 24.04"},
                task_history_id=TASK_HISTORY_ID,
            )
        },
    )

    assert outcome.nodes[0]["task_history_id"] == TASK_HISTORY_ID


@pytest.mark.asyncio
async def test_a_host_never_dispatched_to_has_no_task_history_id() -> None:
    """Point at no run for an orphan, which was never sent anywhere."""
    outcome = await run_sweep(
        [mapped("svc-b", None, NodeResolution.ORPHANED)],
        {},
        hosts=[host("node00", orphaned=True)],
    )

    assert outcome.nodes[0]["task_history_id"] is None


@pytest.mark.asyncio
async def test_an_orphan_host_is_recorded_with_no_executor() -> None:
    """Keep an entry for a host nothing could run on, and say why."""
    outcome = await run_sweep(
        [mapped("svc-b", None, NodeResolution.ORPHANED)],
        {},
        hosts=[host("node00", orphaned=True)],
    )

    assert (outcome.resolved, outcome.orphaned, outcome.answered) == (0, 1, 0)
    node = outcome.nodes[0]
    assert node["executor_host"] is None
    assert node["resolution"] == NodeResolution.ORPHANED
    assert node["answered"] is False
    assert node["duration_seconds"] is None


@pytest.mark.asyncio
async def test_a_failed_host_carries_its_error_and_its_time() -> None:
    """Report why a host produced nothing, and how long it took to say so."""
    outcome = await run_sweep(
        [mapped("svc-c", "node01", NodeResolution.ADDRESS)],
        {
            "node01": HostProbeResult(
                executor_host="node01",
                error="probe run FAILED: no output",
                duration_seconds=FAILED_HOST_SECONDS,
            )
        },
    )

    assert (outcome.resolved, outcome.answered) == (1, 0)
    node = outcome.nodes[0]
    assert node["answered"] is False
    assert node["error"] == "probe run FAILED: no output"
    assert node["duration_seconds"] == FAILED_HOST_SECONDS


@pytest.mark.asyncio
async def test_a_service_pmm_does_not_know_is_still_listed() -> None:
    """Record a service with no PMM id, which the estate could never key a row on."""
    outcome = await run_sweep(
        [mapped("svc-d", "node00", NodeResolution.NAME, external_id=None)],
        {
            "node00": HostProbeResult(
                executor_host="node00",
                host_record={"os": "Ubuntu 24.04"},
                records={"svc-d": RECORD},
            )
        },
    )

    service = outcome.nodes[0]["services"][0]
    assert service["service_id"] is None
    assert service["service_name"] == "svc-d"
    # It answered — the host ran the payload. What is missing is a key to join on,
    # which is a different failure from the host not answering.
    assert service["answered"] is True


@pytest.mark.asyncio
async def test_a_failed_host_s_error_is_kept_for_its_estate_row() -> None:
    """Carry the dispatch's own error as far as ``om.host.last_error``.

    It reached the receipt and stopped there: the estate row was written with a
    hardcoded "returned no probe record" for a timeout, a 409, a payload crash and a
    release that failed alike. ``last_error`` is the one an operator reads, so the
    sweep knew the answer and the row sent them looking for a different fault.
    """
    outcome = await run_sweep(
        [mapped("svc-c", "node01", NodeResolution.ADDRESS)],
        {
            "node01": HostProbeResult(
                executor_host="node01",
                error="TimeoutError: probe task history 123 still RUNNING after 180s",
            )
        },
        hosts=[host("node01")],
    )

    assert (
        outcome.host_errors["node01"]
        == "TimeoutError: probe task history 123 still RUNNING after 180s"
    )


@pytest.mark.asyncio
async def test_a_host_that_answered_nothing_at_all_still_says_something() -> None:
    """State the absence as a finding instead of leaving a null error.

    That absence *is* the finding, so it needs wording rather than a null: the queue
    item ran, the payload said nothing, and nobody can act on an empty column.
    """
    outcome = await run_sweep(
        [],
        {"node00": HostProbeResult(executor_host="node00")},
        hosts=[host("node00")],
    )

    assert outcome.host_errors["node00"] == (
        "the host has an executor but returned no probe record"
    )


@pytest.mark.asyncio
async def test_a_host_that_answered_carries_no_error() -> None:
    """Leave no failure reason behind on success."""
    outcome = await run_sweep(
        [],
        {
            "node00": HostProbeResult(
                executor_host="node00", host_record={"system": {"os_name": "Ubuntu"}}
            )
        },
        hosts=[host("node00")],
    )

    assert outcome.host_errors == {}


@pytest.mark.asyncio
async def test_the_host_document_carries_the_installed_binary() -> None:
    """Carry the installed binary version on the host document, not a service row.

    A host carrying a PMM client and no database is the case OM exists for. The
    payload collects its installed version and used to have it dropped here, because
    only the service fields lifted it — on exactly the hosts with no service to lift.
    """
    outcome = await run_sweep(
        [],
        {
            "pmm-client-node00": HostProbeResult(
                executor_host="pmm-client-node00",
                host_record={
                    "binary_version": "7.0.39-21",
                    "system": {
                        "os_name": "Ubuntu 24.04",
                        "os_id": "ubuntu",
                        "os_version_id": "24.04",
                        "arch": "x86_64",
                    },
                    "install_readiness": {
                        "package_manager": "apt",
                        "data_dir_free_bytes": FREE_BYTES,
                    },
                },
            )
        },
    )

    document = outcome.host_documents["pmm-client-node00"]
    assert document["installed_version"] == "7.0.39-21"
    assert document["os"] == "Ubuntu 24.04"
    # Collected by collect_os_facts, and reaches the document only because
    # HOST_FIELDS carries an entry lifting it.
    assert document["arch"] == "x86_64"
    # Machine-readable, distinct from "os"'s pretty name.
    assert document["os_id"] == "ubuntu"
    assert document["os_version_id"] == "24.04"
    # Install-readiness facts, collected for a host with nothing on it yet.
    assert document["package_manager"] == "apt"
    assert document["data_dir_free_bytes"] == FREE_BYTES


@pytest.mark.asyncio
async def test_one_dispatch_is_timed_once_not_per_service() -> None:
    """Report a host's duration once on the host, not per service.

    It used to be copied onto every service the host served, which read as several
    measurements of several things when it was one measurement of one dispatch.
    """
    outcome = await run_sweep(
        [
            mapped("svc-a", "node00", NodeResolution.NAME, external_id="id-a"),
            mapped("svc-b", "node00", NodeResolution.NAME, external_id="id-b"),
        ],
        {
            "node00": HostProbeResult(
                executor_host="node00",
                host_record={"os": "Ubuntu 24.04"},
                records={"id-a": RECORD, "id-b": RECORD},
                duration_seconds=HOST_SECONDS,
            )
        },
    )

    assert len(outcome.nodes) == 1
    assert outcome.nodes[0]["duration_seconds"] == HOST_SECONDS
    assert len(outcome.nodes[0]["services"]) == TWO_SERVICES
    assert all(service["answered"] for service in outcome.nodes[0]["services"])


class TestTheColdStartRace:
    """Assert a sweep that starts before PMM Extensions' web server does not report a failure.

    The sweep reads the estate over HTTP from PMM Extensions itself, so a restart races its own
    uvicorn: beat's ``DatabaseScheduler`` sets "last run" to now on startup and fires
    the entry on its first tick, seconds before the API is listening. Measured on this
    workspace — the sweep called the inventory API 6 seconds before "Application
    startup complete" and wrote a terminal ``FAILED`` carrying ``Cannot connect to
    host localhost:8000``, on an estate where a manual sweep moments later answered
    ``SUCCESS`` for all 14 services.

    Both consumers lead with the newest run, so that one row presented a healthy
    estate as a failed sweep for the next ten minutes. Any deployment that restarts
    PMM Extensions has the same race; the harness only makes it deterministic.
    """

    @pytest.mark.asyncio
    async def test_a_refused_connection_is_waited_out(self) -> None:
        """Wait out a refused connection so the run never notices the retry."""
        base = "app.extensions.apps.om_inventory.service"
        services = AsyncMock(
            side_effect=[
                ClientConnectionError("Cannot connect to host localhost:8000"),
                [],
            ]
        )
        with (
            patch(f"{base}.list_mongodb_services", services),
            patch(f"{base}.list_inventory_nodes", AsyncMock(return_value=[])),
            patch(f"{base}.get_executor_states", AsyncMock(return_value={})),
            patch("asyncio.sleep", AsyncMock()),
        ):
            enumerated = await enumerate_estate(MagicMock(), MagicMock())

        assert enumerated == ([], [], {})
        assert services.await_count == RETRIED_ONCE

    @pytest.mark.asyncio
    async def test_the_tasks_api_is_waited_out_too(self) -> None:
        """Retry when either of two processes can be the one still starting."""
        base = "app.extensions.apps.om_inventory.service"
        states = AsyncMock(
            side_effect=[
                ClientConnectionError("Cannot connect to host localhost:8001"),
                {},
            ]
        )
        with (
            patch(f"{base}.list_mongodb_services", AsyncMock(return_value=[])),
            patch(f"{base}.list_inventory_nodes", AsyncMock(return_value=[])),
            patch(f"{base}.get_executor_states", states),
            patch("asyncio.sleep", AsyncMock()),
        ):
            enumerated = await enumerate_estate(MagicMock(), MagicMock())

        assert enumerated == ([], [], {})
        assert states.await_count == RETRIED_ONCE

    @pytest.mark.asyncio
    async def test_an_api_that_never_answers_still_fails_the_run(self) -> None:
        """Stop waiting eventually: a misconfigured endpoint is a real failure.

        The retry exists for a web server that is seconds away, not to make a broken
        deployment look busy.
        """
        base = "app.extensions.apps.om_inventory.service"
        services = AsyncMock(side_effect=ClientConnectionError("refused"))
        with (
            patch(f"{base}.list_mongodb_services", services),
            patch(f"{base}.list_inventory_nodes", AsyncMock(return_value=[])),
            patch(f"{base}.get_executor_states", AsyncMock(return_value={})),
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(ClientConnectionError),
        ):
            await enumerate_estate(MagicMock(), MagicMock())

        assert services.await_count == STARTUP_RETRIES

    @pytest.mark.asyncio
    async def test_a_real_failure_is_not_retried(self) -> None:
        """Wait out only a refused connection.

        A 500 from inventory, or an executor backend too old to serve
        ``/hosts/states/``, is a finding. Retrying it would delay the run by half a
        minute and report the same thing.
        """
        base = "app.extensions.apps.om_inventory.service"
        services = AsyncMock(side_effect=RuntimeError("inventory answered 500"))
        with (
            patch(f"{base}.list_mongodb_services", services),
            patch(f"{base}.list_inventory_nodes", AsyncMock(return_value=[])),
            patch(f"{base}.get_executor_states", AsyncMock(return_value={})),
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(RuntimeError),
        ):
            await enumerate_estate(MagicMock(), MagicMock())

        assert services.await_count == 1
