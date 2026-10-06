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

"""Test refreshing named hosts rather than the whole estate.

A full sweep dispatches a Nomad job to every executor host and takes a minute and a
half in this sandbox. "I just did something to this host, is it healthy now" should
not cost that, and it is the question every action PMM grows will ask next.

Two rules make it work, and both are easy to get subtly wrong:

**Conflict is per host.** The old guard refused any run while any other was in
flight. Keep that and a scoped refresh is useless exactly when the estate is busiest,
because the ten-minute schedule is often mid-sweep. Two runs collide only when they
would touch the same host — and a full refresh touches all of them.

**Nothing outside the scope is written.** This is where "only a run that attempted
an entity touches its timestamps" stops being a principle and starts being
load-bearing: get it wrong and refreshing one host marks the rest of the estate
failed, which is worse than not having the feature.
"""

from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import utc_now
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import (
    conflict_detail,
    conflicting_run,
    ProbeRunManager,
    prune_runs,
    recent_runs,
    upsert_host,
)
from app.extensions.apps.om_inventory.enumeration import InventoryHost
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import ExecutorState, MappedService
from app.extensions.apps.om_inventory.models import (
    NodeResolution,
    ProbeRun,
    ProbeRunStatus,
)
from app.extensions.apps.om_inventory.service import (
    finalise,
    narrow_to_scope,
    SweepOutcome,
    terminal_status,
)
from tests.app.extensions.apps.om_inventory.conftest import BASE

NODE_A = "id-db00"
NODE_B = "id-db01"
#: One host with an executor and one without, which is the smallest estate that can
#: tell hosts_total and hosts_probeable apart.
TWO_HOSTS = 2
#: Three runs kept down to one: the two that are not the survivor.
PRUNED = 2


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the sweep on for this module.

    ``ENABLED`` defaults to off (PMM's Operations for MongoDB switch starts a fresh
    deployment unswept), but every test here posts to ``/runs`` to exercise
    scoping and conflict rules that have nothing to do with that switch.

    :param monkeypatch: Restores the real value after the test.
    """
    monkeypatch.setattr(om_inventory_settings, "ENABLED", True)


@pytest_asyncio.fixture
async def two_hosts(session: AsyncSession) -> AsyncSession:
    """Give the estate two hosts, so "one of them" is a meaningful scope.

    :param session: The database session.
    :return: The same session, with rows.
    """
    for node_id, name in ((NODE_A, "db00"), (NODE_B, "db01")):
        await upsert_host(
            session,
            node_id=node_id,
            name=name,
            address=None,
            executor_host=name,
            observed={"collected_at": "2026-08-17T09:00:00+00:00"},
        )
    await session.commit()
    return session


async def start_run(session: AsyncSession, scope: list[str] | None) -> ProbeRun:
    """Put a run in flight with the given scope.

    :param session: The database session.
    :param scope: Its scope, or ``None`` for the whole estate.
    :return: The run.
    """
    return await ProbeRunManager.save(
        session, ProbeRun(scope=scope, status=ProbeRunStatus.RUNNING)
    )


def host(node_id: str, name: str) -> InventoryHost:
    """Build one enumerated host.

    :param node_id: PMM's node id.
    :param name: Its registered name.
    :return: The host.
    """
    return InventoryHost(
        node_id=node_id,
        name=name,
        address=None,
        executor_host=name,
        resolution=NodeResolution.NAME,
    )


def service_on(name: str) -> InventoryService:
    """Build one service on the named node.

    :param name: The node's name.
    :return: The service.
    """
    return InventoryService(
        service_id=1,
        external_id=f"svc-{name}",
        name=name,
        port=27017,
        node_name=name,
        node_address=None,
    )


class TestNarrowToScope:
    """Assert what a scoped sweep keeps and what it drops."""

    def test_keeps_only_the_named_hosts_and_their_services(self) -> None:
        """Narrow everything downstream of enumeration, in one place."""
        hosts = [host(NODE_A, "db00"), host(NODE_B, "db01")]
        services = [service_on("db00"), service_on("db01")]
        mapped = [MappedService(s, s.node_name, NodeResolution.NAME) for s in services]

        scoped_hosts, scoped_services, scoped_mapped = narrow_to_scope(
            hosts, services, mapped, [NODE_A]
        )

        assert [h.node_id for h in scoped_hosts] == [NODE_A]
        assert [s.name for s in scoped_services] == ["db00"]
        assert [m.service.name for m in scoped_mapped] == ["db00"]

    def test_an_unknown_id_narrows_to_nothing_rather_than_everything(self) -> None:
        """Narrow to nothing, not everything, when the scope id no longer matches any host.

        The endpoint rejects an unknown id before this runs, so reaching here means
        the estate changed underneath the request — and refreshing nothing is the
        safe reading, because refreshing everything would touch timestamps the caller
        never asked about.
        """
        hosts = [host(NODE_A, "db00")]

        scoped_hosts, scoped_services, scoped_mapped = narrow_to_scope(
            hosts, [service_on("db00")], [], ["id-that-vanished"]
        )

        assert (scoped_hosts, scoped_services, scoped_mapped) == ([], [], [])


class TestTerminalStatus:
    """Assert what counts as a successful run now that hosts can be probed alone."""

    def test_a_host_with_no_database_that_answered_is_a_success(self) -> None:
        """Report success when a pmm-client host answers but resolves no services.

        It resolves no services, because there are none, and judging on services
        alone reported ``FAILED`` for a run that did exactly what it was asked.
        Measured against ``standalone-node00`` before the verdict counted hosts.
        """
        outcome = SweepOutcome(
            dispatched={"pmm-client-node00"},
            host_documents={"pmm-client-node00": {"os": "Ubuntu 24.04"}},
        )

        assert terminal_status(outcome) is ProbeRunStatus.SUCCESS

    def test_a_dispatch_that_answered_nothing_is_partial(self) -> None:
        """Report one silent host among several as partial, not total, failure."""
        outcome = SweepOutcome(
            dispatched={"a", "b"}, host_documents={"a": {"os": "Ubuntu 24.04"}}
        )

        assert terminal_status(outcome) is ProbeRunStatus.PARTIAL

    def test_reaching_nothing_at_all_is_a_failure(self) -> None:
        """Fail when no host answers and no service resolves.

        That is the condition the probe exists to surface, so it must stay loud.
        """
        assert terminal_status(SweepOutcome()) is ProbeRunStatus.FAILED

    def test_orphans_alone_do_not_fail_a_run(self) -> None:
        """Treat an orphaned service as an estate fact, not a sweep failure."""
        outcome = SweepOutcome(
            orphaned=3,
            dispatched={"a"},
            host_documents={"a": {"os": "Ubuntu 24.04"}},
        )

        assert terminal_status(outcome) is ProbeRunStatus.SUCCESS

    def test_every_dispatch_failing_is_a_failure_not_a_partial(self) -> None:
        """Report nothing reached when nothing answered, however much was tried.

        ``FAILED`` used to be reserved for having attempted nothing at all, so a run
        that dispatched to the whole estate and got nothing back reported ``PARTIAL``
        — the status that says "some of this is fine" — for a total outage. That is
        the one condition where the loud answer is the point, and it was unreachable
        in the case that needs it: an operator and anything automating against it both
        read `PARTIAL` as a working estate with gaps.
        """
        outcome = SweepOutcome(dispatched={"a", "b", "c"})

        assert terminal_status(outcome) is ProbeRunStatus.FAILED

    def test_no_service_answering_on_a_host_that_did_is_still_partial(self) -> None:
        """Report partial when something answered, so the sweep reached the estate.

        The distinction the previous test rests on is *answered*, not *healthy*. A
        host that reported while every mongod on it refused is a real finding about
        those databases, and calling it a failed sweep would point at OM instead.
        """
        outcome = SweepOutcome(
            resolved=2,
            dispatched={"a"},
            host_documents={"a": {"os": "Ubuntu 24.04"}},
        )

        assert terminal_status(outcome) is ProbeRunStatus.PARTIAL


class TestTriggerScope:
    """Assert the trigger's contract."""

    @pytest.mark.asyncio
    async def test_rejected_while_enabled_is_off(
        self,
        api: AsyncClient,
        two_hosts: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Refuse a manual trigger while PMM's Operations for MongoDB switch is off.

        ``app.py``'s periodic-task thunk already keeps a disabled deployment off
        Celery beat; without this check a caller could still start a sweep on
        request, which is the gap the switch exists to close.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        :param monkeypatch: Restores the real value after the test.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", False)

        response = await api.post(f"{BASE}/runs")

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_no_body_means_the_whole_estate(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Take the same path as the scheduled sweep when the POST body is empty.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        response = await api.post(f"{BASE}/runs")

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["scope"] is None

    @pytest.mark.asyncio
    async def test_a_scope_is_recorded_on_the_run(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Store the scope rather than inferring it: the receipt needs it to be read.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        response = await api.post(f"{BASE}/runs", json={"node_ids": [NODE_A]})

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["scope"] == [NODE_A]

    @pytest.mark.asyncio
    async def test_a_full_estate_run_is_sql_null_not_json_null(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Store SQL NULL for a full sweep's scope, not the JSON null literal.

        SQLAlchemy's JSON types store a Python ``None`` as the JSON scalar ``null``
        unless told otherwise, and the Python side reads back ``None`` either way —
        so nothing complains until someone asks the database which runs were full
        sweeps and gets an empty answer. Measured happening before ``none_as_null``
        was set, which is why this asserts against SQL rather than the response.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        await api.post(f"{BASE}/runs")

        full = await two_hosts.exec(
            text("select count(*) from om_schema.om_inventory_run where scope is null")
        )
        assert full.scalar() == 1

    @pytest.mark.asyncio
    async def test_an_unknown_host_is_404_not_an_empty_refresh(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Handle PMM Extensions' inventory copy lagging PMM's, which is a real case.

        Answering 404 by name beats running a refresh that quietly does nothing and
        reports success.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        response = await api.post(f"{BASE}/runs", json={"node_ids": ["nope"]})

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "nope" in response.json()["detail"]


class TestConflict:
    """Assert that conflict is judged per host."""

    @pytest.mark.asyncio
    async def test_two_different_hosts_do_not_conflict(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Allow refreshing one host while a different host's run is in flight.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        await start_run(two_hosts, [NODE_A])

        response = await api.post(f"{BASE}/runs", json={"node_ids": [NODE_B]})

        assert response.status_code == status.HTTP_202_ACCEPTED

    @pytest.mark.asyncio
    async def test_the_same_host_conflicts(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Refuse two runs that probe the same host at once.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        await start_run(two_hosts, [NODE_A])

        response = await api.post(f"{BASE}/runs", json={"node_ids": [NODE_A]})

        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_a_full_refresh_conflicts_with_a_scoped_one(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Treat a full refresh as conflicting with an existing scoped run.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        await start_run(two_hosts, [NODE_A])

        response = await api.post(f"{BASE}/runs")

        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_a_scoped_refresh_conflicts_with_a_full_one(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Check the same in reverse, which is the easy half to forget.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        await start_run(two_hosts, None)

        response = await api.post(f"{BASE}/runs", json={"node_ids": [NODE_A]})

        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_an_abandoned_run_is_reaped_rather_than_blocking_forever(
        self, api: AsyncClient, two_hosts: AsyncSession
    ) -> None:
        """Reap an abandoned run instead of letting it block refreshes forever.

        Without the reaper that row wedges the app permanently, which is a bug this
        app has already had once.

        :param api: The authenticated client.
        :param two_hosts: The populated session.
        """
        stale = await start_run(two_hosts, [NODE_A])
        stale.started_at = utc_now() - (
            om_inventory_settings.STALE_RUN_AFTER + timedelta(minutes=1)
        )
        await ProbeRunManager.save(two_hosts, stale)

        response = await api.post(f"{BASE}/runs", json={"node_ids": [NODE_A]})

        assert response.status_code == status.HTTP_202_ACCEPTED
        reaped = await ProbeRunManager.get(two_hosts, id=stale.id)
        assert reaped.status == ProbeRunStatus.FAILED
        assert "abandoned" in (reaped.error or "")


class TestHostCounters:
    """Assert a run's receipt counts hosts, not only the services on them.

    A sweep has attempted hosts as well as services since a host became probeable for
    its own sake, and until these columns existed the receipt could not say so. A
    refresh of a pmm-client host read as "0 of 0 services", which is exactly what a
    run that did nothing at all looks like - on the one host OM most exists to
    describe.
    """

    @pytest.mark.asyncio
    async def test_a_host_only_refresh_reports_what_it_reached(
        self, session: AsyncSession
    ) -> None:
        """Produce non-zero host counters even though the host has no database.

        :param session: The database session.
        """
        host = InventoryHost(
            node_id="id-pmm-client-node00",
            name="pmm-client-node00",
            address="10.0.0.2",
            executor_host="pmm-client-node00",
            resolution=NodeResolution.NAME,
            executor_state=ExecutorState(
                "pmm-client-node00",
                "10.0.0.2",
                reachable=True,
                driver_healthy=True,
            ),
        )
        run = await ProbeRunManager.save(session, ProbeRun())
        outcome = SweepOutcome(
            hosts=[host],
            dispatched={"pmm-client-node00"},
            host_documents={"pmm-client-node00": {"os": "Ubuntu 24.04"}},
        )

        await finalise(session, run.id, outcome)

        stored = await ProbeRunManager.get(session, id=run.id)
        assert (stored.hosts_total, stored.hosts_probeable, stored.hosts_answered) == (
            1,
            1,
            1,
        )
        # Zero services is the honest answer here, and it is why the host counters had
        # to exist rather than the service ones being reinterpreted.
        assert stored.services_total == 0

    @pytest.mark.asyncio
    async def test_a_host_with_no_executor_counts_but_is_not_probeable(
        self, session: AsyncSession
    ) -> None:
        """Count a host with no executor as total but not as probeable.

        The gap between them is the estate nothing can be run on - a fact about
        onboarding rather than a failure of the sweep, and one a single "hosts" count
        would hide.

        :param session: The database session.
        """
        reachable = InventoryHost(
            node_id="id-a",
            name="a",
            address=None,
            executor_host="a",
            resolution=NodeResolution.NAME,
            executor_state=ExecutorState(
                "a", "10.0.0.1", reachable=True, driver_healthy=True
            ),
        )
        stranded = InventoryHost(
            node_id="id-b",
            name="b",
            address=None,
            executor_host=None,
            resolution=NodeResolution.ORPHANED,
        )
        run = await ProbeRunManager.save(session, ProbeRun())
        outcome = SweepOutcome(
            hosts=[reachable, stranded],
            dispatched={"a"},
            host_documents={"a": {"os": "Ubuntu 24.04"}},
        )

        await finalise(session, run.id, outcome)

        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.hosts_total == TWO_HOSTS
        assert stored.hosts_probeable == 1
        assert stored.hosts_answered == 1


class TestTheScheduleRespectsSingleFlight:
    """Assert a scheduled sweep cannot start on top of one already running.

    The guard used to live only in the trigger endpoint, and **the schedule does not
    go through it** - Celery beat calls the task directly. So a scheduled sweep would
    start while another was still dispatching, both would enqueue the same job for the
    same host, and the Tasks layer would refuse the duplicate with
    ``409 Identical queue item already running``. The loser recorded that against a
    host that was perfectly healthy.

    Measured on this workspace's sandbox: two full sweeps 31 seconds apart, four
    healthy replicaset hosts marked unanswered with a 409 as their reason. It did no
    lasting damage only because the good sweep happened to finish last and overwrite
    the failure - reverse the order and four fine hosts read as failing.
    """

    @pytest.mark.asyncio
    async def test_a_second_full_sweep_is_skipped_not_run(
        self, session: AsyncSession
    ) -> None:
        """Flag a second full sweep as a conflict with the one already running.

        :param session: The database session.
        """
        in_flight = await ProbeRunManager.save(session, ProbeRun(scope=None))

        blocking = await conflicting_run(
            session, None, stale_after=timedelta(minutes=30)
        )

        assert blocking is not None
        assert blocking.id == in_flight.id

    @pytest.mark.asyncio
    async def test_a_run_does_not_refuse_itself(self, session: AsyncSession) -> None:
        """Exclude a run's own id so it does not conflict with itself.

        Without excluding its own id the task would find that row, conclude a sweep
        was already in flight, and skip every run started through the API - which is
        every manual refresh.

        :param session: The database session.
        """
        mine = await ProbeRunManager.save(session, ProbeRun(scope=None))

        blocking = await conflicting_run(
            session, None, exclude=mine.id, stale_after=timedelta(minutes=30)
        )

        assert blocking is None

    @pytest.mark.asyncio
    async def test_disjoint_scopes_do_not_block_each_other(
        self, session: AsyncSession
    ) -> None:
        """Allow disjoint one-host scopes to run without conflicting.

        Single-flight is per host precisely so a ten-minute schedule does not refuse
        the refresh someone wants.

        :param session: The database session.
        """
        await ProbeRunManager.save(session, ProbeRun(scope=[NODE_A]))

        blocking = await conflicting_run(
            session, [NODE_B], stale_after=timedelta(minutes=30)
        )

        assert blocking is None

    @pytest.mark.asyncio
    async def test_an_abandoned_run_is_reaped_rather_than_honoured(
        self, session: AsyncSession
    ) -> None:
        """Reap an abandoned run rather than honour it as still in flight.

        :param session: The database session.
        """
        stale = await ProbeRunManager.save(session, ProbeRun(scope=None))
        stale.started_at = utc_now() - timedelta(hours=2)
        await ProbeRunManager.save(session, stale)

        blocking = await conflicting_run(
            session, None, stale_after=timedelta(minutes=30)
        )

        assert blocking is None
        reaped = await ProbeRunManager.get(session, id=stale.id)
        assert reaped.status is ProbeRunStatus.FAILED
        assert reaped.error is not None

    @pytest.mark.asyncio
    async def test_the_message_names_the_hosts_that_are_held(
        self, session: AsyncSession
    ) -> None:
        """Name the held hosts rather than saying "a sweep is already running".

        :param session: The database session.
        """
        blocking = await ProbeRunManager.save(session, ProbeRun(scope=[NODE_A]))

        detail = conflict_detail(blocking, [NODE_A, NODE_B])

        assert NODE_A in detail
        assert str(blocking.id) in detail


class TestTwoRunsRacingForTheSameHosts:
    """Assert a race for the same hosts leaves exactly one sweep standing.

    Single-flight is check-then-insert: creating the row and claiming the work are two
    statements, so two overlapping ``POST /runs`` — or beat entering ``run_probe``
    beside one — can both pass the check and both insert ``RUNNING``. Each worker then
    re-checks excluding only itself, finds the other, and *both* skip. The estate does
    not refresh at all, and both callers are already holding a ``202`` and a run id
    that will never sweep.

    That is worse than either outcome the guard was protecting against, and it is
    silent: two ``SKIPPED`` rows look like the guard working. So a run asking on its
    own behalf yields only to the run that was there first, by an order both sides
    compute from the same rows.
    """

    @pytest.mark.asyncio
    async def test_the_older_run_proceeds(self, session: AsyncSession) -> None:
        """Let the older run proceed without yielding to the newcomer.

        :param session: The database session.
        """
        first = await ProbeRunManager.save(session, ProbeRun(scope=None))
        second = await ProbeRunManager.save(session, ProbeRun(scope=None))
        second.started_at = utc_now() + timedelta(seconds=1)
        await ProbeRunManager.save(session, second)

        blocking = await conflicting_run(
            session, None, exclude=first.id, stale_after=timedelta(minutes=30)
        )

        assert blocking is None

    @pytest.mark.asyncio
    async def test_the_newer_run_stands_down(self, session: AsyncSession) -> None:
        """...and the other side of the same race reaches the opposite verdict.

        Exactly one of the pair proceeds, which is the whole property. Both workers
        read the same two rows and neither needs a lock to agree.

        :param session: The database session.
        """
        first = await ProbeRunManager.save(session, ProbeRun(scope=None))
        second = await ProbeRunManager.save(session, ProbeRun(scope=None))
        second.started_at = utc_now() + timedelta(seconds=1)
        await ProbeRunManager.save(session, second)

        blocking = await conflicting_run(
            session, None, exclude=second.id, stale_after=timedelta(minutes=30)
        )

        assert blocking is not None
        assert blocking.id == first.id

    @pytest.mark.asyncio
    async def test_rows_created_in_the_same_tick_are_still_ordered(
        self, session: AsyncSession
    ) -> None:
        """Break a tie between same-timestamp rows so only one proceeds.

        Which of the two wins does not matter; that they cannot both win does. The id
        is the tie-break because both sides can read it.

        :param session: The database session.
        """
        stamp = utc_now()
        one = await ProbeRunManager.save(session, ProbeRun(scope=None))
        two = await ProbeRunManager.save(session, ProbeRun(scope=None))
        for run in (one, two):
            run.started_at = stamp
            await ProbeRunManager.save(session, run)

        verdicts = [
            await conflicting_run(
                session, None, exclude=run.id, stale_after=timedelta(minutes=30)
            )
            for run in (one, two)
        ]

        assert [verdict is None for verdict in verdicts].count(True) == 1

    @pytest.mark.asyncio
    async def test_a_run_still_yields_to_one_already_dispatching(
        self, session: AsyncSession
    ) -> None:
        """Keep the newer run yielding to one already dispatching.

        A sweep 30 seconds into dispatching owns its hosts; a scheduled run arriving
        on top of it enqueues the same job for the same host, the Tasks layer refuses
        the duplicate, and the loser records a 409 against a healthy node. That run
        skipping is the correct outcome and the measured reason the check exists.

        :param session: The database session.
        """
        running = await ProbeRunManager.save(session, ProbeRun(scope=None))
        running.started_at = utc_now() - timedelta(seconds=30)
        await ProbeRunManager.save(session, running)
        scheduled = await ProbeRunManager.save(session, ProbeRun(scope=None))

        blocking = await conflicting_run(
            session, None, exclude=scheduled.id, stale_after=timedelta(minutes=30)
        )

        assert blocking is not None
        assert blocking.id == running.id

    @pytest.mark.asyncio
    async def test_the_endpoint_still_refuses_a_request_outright(
        self, session: AsyncSession
    ) -> None:
        """Refuse the request outright with no exclude and no tie-break.

        The endpoint checks before creating a row, so it has nothing to compare with
        and nothing to gain from comparing: telling the caller the hosts are held is a
        better answer than starting a second sweep.

        :param session: The database session.
        """
        in_flight = await ProbeRunManager.save(session, ProbeRun(scope=[NODE_A]))

        blocking = await conflicting_run(
            session, [NODE_A], stale_after=timedelta(minutes=30)
        )

        assert blocking is not None
        assert blocking.id == in_flight.id


class TestPruningKeepsWhatIsStillRunning:
    """Assert retention cannot delete a row out from under its own worker.

    Retention is by ``started_at`` and a long sweep is by definition the oldest row
    while it runs, so a burst of newer rows — scoped refreshes, or the ``SKIPPED``
    rows a schedule collision leaves behind — could push it past the limit. The
    worker then finalises a run that no longer exists and raises, losing the receipt
    for work that was actually done.
    """

    @pytest.mark.asyncio
    async def test_a_running_row_survives_however_many_are_newer(
        self, session: AsyncSession
    ) -> None:
        """Keep the oldest running row even though newer rows exist.

        :param session: The database session.
        """
        running = await ProbeRunManager.save(session, ProbeRun(scope=None))
        running.started_at = utc_now() - timedelta(minutes=5)
        await ProbeRunManager.save(session, running)
        for _ in range(3):
            skipped = await ProbeRunManager.save(session, ProbeRun(scope=None))
            skipped.status = ProbeRunStatus.SKIPPED
            await ProbeRunManager.save(session, skipped)

        deleted = await prune_runs(session, keep=1)

        assert deleted == PRUNED
        assert await ProbeRunManager.first(session, id=running.id) is not None

    @pytest.mark.asyncio
    async def test_finished_runs_are_still_pruned(self, session: AsyncSession) -> None:
        """Prune on schedule: the table grows by a fact set per sweep.

        :param session: The database session.
        """
        for index in range(3):
            done = await ProbeRunManager.save(session, ProbeRun(scope=None))
            done.status = ProbeRunStatus.SUCCESS
            done.started_at = utc_now() - timedelta(minutes=index)
            await ProbeRunManager.save(session, done)

        deleted = await prune_runs(session, keep=1)

        assert deleted == PRUNED
        assert len(await recent_runs(session)) == 1
