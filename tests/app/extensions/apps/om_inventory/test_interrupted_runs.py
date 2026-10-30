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

"""Test that a scan its worker never finished does not hold every later one.

Only the task running a scan writes its terminal status. When the worker restarts
mid-scan, :func:`reap_interrupted_runs` fails the run, at worker start and before
every scan. When PMM Server, which holds the database, restarts instead, the failure
the sweep could not write is retried by ``fail_om_run``.
"""

import socket
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from kombu.exceptions import OperationalError as KombuOperationalError
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette import status

from app.core.utils.date_time import utc_now
from app.extensions.apps.om_inventory import celery as celery_module
from app.extensions.apps.om_inventory import service as service_module
from app.extensions.apps.om_inventory.api_routes import QUEUE_UNAVAILABLE_DETAIL
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import (
    claim_run,
    fail_running_run,
    ProbeRunManager,
    reap_interrupted_runs,
    RESTARTED_DETAIL,
)
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from app.extensions.apps.om_inventory.service import (
    database_unreachable,
    FailureNotRecordedError,
    run_probe,
    UNCLAIMED_GRACE,
    WorkerBoot,
)
from tests.app.extensions.apps.om_inventory.conftest import BASE, CLEAN_OUTCOME

#: A run whose failure its sweep could not write.
RUN_ID = uuid4()


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> WorkerBoot:
    """Make this process a worker that booted longer ago than ``UNCLAIMED_GRACE``.

    :param monkeypatch: Restores the real boot after the test.
    :return: The boot.
    """
    current = WorkerBoot(
        boot_id=uuid4(), booted_at=utc_now() - UNCLAIMED_GRACE - timedelta(minutes=1)
    )
    monkeypatch.setattr(WorkerBoot, "current", current)
    return current


@pytest.fixture
def swept() -> Iterator[AsyncMock]:
    """Stand in for the sweep with one that answers cleanly.

    :return: The stub, while the patch is in place.
    """
    stub = AsyncMock(return_value=CLEAN_OUTCOME)
    with patch.object(service_module, "sweep", stub):
        yield stub


async def _runs_left_by_restarts(
    session: AsyncSession, boot: WorkerBoot
) -> dict[str, ProbeRun]:
    """Store one run of each kind the reaper has to tell apart.

    :param session: The database session.
    :param boot: This worker's boot.
    :return: The runs, by kind.
    """
    before = boot.booted_at - timedelta(minutes=1)
    runs = {
        "other boot": ProbeRun(worker_boot_id=uuid4(), started_at=before),
        "this boot": ProbeRun(worker_boot_id=boot.boot_id, started_at=before),
        "unclaimed before boot": ProbeRun(started_at=before),
        "unclaimed since boot": ProbeRun(started_at=boot.booted_at),
        "finished": ProbeRun(
            worker_boot_id=uuid4(), started_at=before, status=ProbeRunStatus.SUCCESS
        ),
    }
    for run in runs.values():
        await ProbeRunManager.save(session, run)
    return runs


async def _assert_reaped(
    session: AsyncSession,
    runs: dict[str, ProbeRun],
    reaped: list[UUID],
    orphaned: set[str],
) -> None:
    """Assert only the runs a restart orphaned were failed, and how.

    :param session: The database session.
    :param runs: The runs :func:`_runs_left_by_restarts` stored.
    :param reaped: What :func:`reap_interrupted_runs` returned.
    :param orphaned: The kinds of run expected failed.
    """
    assert set(reaped) == {runs[kind].id for kind in orphaned}
    for kind, run in runs.items():
        stored = await ProbeRunManager.get(session, id=run.id)
        if kind in orphaned:
            assert (stored.status, stored.error) == (
                ProbeRunStatus.FAILED,
                RESTARTED_DETAIL,
            ), kind
            assert stored.finished_at is not None, kind
        else:
            assert stored.status is run.status, kind


#: The runs failed with and without the unclaimed ones older than the boot.
REAPED = [
    pytest.param(True, {"other boot", "unclaimed before boot"}, id="with-unclaimed"),
    pytest.param(False, {"other boot"}, id="claimed-only"),
]


class TestReapInterruptedRuns:
    """Fail exactly the running rows that lost their worker to a restart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("with_unclaimed", "orphaned"), REAPED)
    async def test_fails_only_the_runs_a_restart_orphaned(
        self,
        session: AsyncSession,
        boot: WorkerBoot,
        *,
        with_unclaimed: bool,
        orphaned: set[str],
    ) -> None:
        """Fail another boot's run, and when asked an unclaimed run older than it.

        An unclaimed run started since this boot is a scan still waiting for this
        worker, and this boot's own run is live.

        :param session: The database session.
        :param boot: This worker's boot.
        :param with_unclaimed: Whether unclaimed runs older than the boot go too.
        :param orphaned: The kinds of run expected failed.
        """
        runs = await _runs_left_by_restarts(session, boot)

        reaped = await reap_interrupted_runs(
            session, boot.boot_id, boot.booted_at if with_unclaimed else None
        )

        await _assert_reaped(session, runs, reaped, orphaned)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_fails_only_the_runs_a_restart_orphaned_on_postgres(
        self, postgres_session: AsyncSession, boot: WorkerBoot
    ) -> None:
        """Run the same predicate and ``RETURNING`` against a real PostgreSQL.

        :param postgres_session: A session on real PostgreSQL.
        :param boot: This worker's boot.
        """
        runs = await _runs_left_by_restarts(postgres_session, boot)

        reaped = await reap_interrupted_runs(
            postgres_session, boot.boot_id, boot.booted_at
        )

        await _assert_reaped(
            postgres_session, runs, reaped, {"other boot", "unclaimed before boot"}
        )


class TestClaimRun:
    """Let exactly one boot take a run, and only while it is still waiting."""

    @pytest.mark.asyncio
    async def test_an_unclaimed_running_run_is_claimed(
        self, session: AsyncSession, boot: WorkerBoot
    ) -> None:
        """Stamp the run with this boot.

        :param session: The database session.
        :param boot: This worker's boot.
        """
        run = await ProbeRunManager.save(session, ProbeRun())

        assert await claim_run(session, run.id, boot.boot_id)

        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.worker_boot_id == boot.boot_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("holder", "run_status"),
        [
            pytest.param(uuid4(), ProbeRunStatus.RUNNING, id="claimed-by-another-boot"),
            pytest.param(None, ProbeRunStatus.FAILED, id="already-reaped"),
        ],
    )
    async def test_a_run_not_waiting_is_left_alone(
        self,
        session: AsyncSession,
        boot: WorkerBoot,
        holder: UUID | None,
        run_status: ProbeRunStatus,
    ) -> None:
        """Refuse the claim and keep the row as it was.

        :param session: The database session.
        :param boot: This worker's boot.
        :param holder: The boot already holding it, if any.
        :param run_status: Its status.
        """
        run = await ProbeRunManager.save(
            session, ProbeRun(worker_boot_id=holder, status=run_status)
        )

        assert not await claim_run(session, run.id, boot.boot_id)

        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.worker_boot_id == holder

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_the_claim_returns_its_row_on_postgres(
        self, postgres_session: AsyncSession, boot: WorkerBoot
    ) -> None:
        """Claim once and refuse a second time through ``RETURNING`` on PostgreSQL.

        :param postgres_session: A session on real PostgreSQL.
        :param boot: This worker's boot.
        """
        run = await ProbeRunManager.save(postgres_session, ProbeRun())

        assert await claim_run(postgres_session, run.id, boot.boot_id)
        assert not await claim_run(postgres_session, run.id, uuid4())


class TestFailRunningRun:
    """Fail a run only while it is still running."""

    @pytest.mark.asyncio
    async def test_a_running_run_is_failed(self, session: AsyncSession) -> None:
        """Record the failure on a run still in flight.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())

        assert await fail_running_run(session, run.id, "connection refused")

        stored = await ProbeRunManager.get(session, id=run.id)
        assert (stored.status, stored.error) == (
            ProbeRunStatus.FAILED,
            "connection refused",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "run_status",
        [ProbeRunStatus.SUCCESS, ProbeRunStatus.PARTIAL, ProbeRunStatus.FAILED],
    )
    async def test_a_finished_run_is_not_rewritten(
        self, session: AsyncSession, run_status: ProbeRunStatus
    ) -> None:
        """Leave a run that already reached a terminal status as it is.

        :param session: The database session.
        :param run_status: Its terminal status.
        """
        run = await ProbeRunManager.save(
            session, ProbeRun(status=run_status, error="its own")
        )

        assert not await fail_running_run(session, run.id, "late")

        stored = await ProbeRunManager.get(session, id=run.id)
        assert (stored.status, stored.error) == (run_status, "its own")


@pytest.mark.usefixtures("enabled")
class TestRunProbeRecoversBeforeItSweeps:
    """Recover in the scan task too, since it can run before the startup task."""

    @pytest.mark.asyncio
    async def test_a_scheduled_scan_reaps_the_orphan_and_runs(
        self, service_session: AsyncSession, boot: WorkerBoot, swept: AsyncMock
    ) -> None:
        """Fail another boot's run and sweep, rather than skip as blocked by it.

        :param service_session: The session every sweep function opens.
        :param boot: This worker's boot.
        :param swept: The stubbed sweep.
        """
        orphan = await ProbeRunManager.save(
            service_session,
            ProbeRun(
                worker_boot_id=uuid4(),
                started_at=boot.booted_at - timedelta(minutes=1),
            ),
        )

        run_id = await run_probe()

        swept.assert_awaited_once()
        assert (await ProbeRunManager.get(service_session, id=orphan.id)).error == (
            RESTARTED_DETAIL
        )
        stored = await ProbeRunManager.get(service_session, id=run_id)
        assert (stored.status, stored.worker_boot_id) == (
            ProbeRunStatus.SUCCESS,
            boot.boot_id,
        )

    @pytest.mark.asyncio
    async def test_a_switched_off_scan_still_reaps(
        self,
        service_session: AsyncSession,
        boot: WorkerBoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reap before the ``ENABLED`` check, then skip.

        :param service_session: The session every sweep function opens.
        :param boot: This worker's boot.
        :param monkeypatch: Restores the real value after the test.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", False)
        orphan = await ProbeRunManager.save(
            service_session, ProbeRun(started_at=boot.booted_at - timedelta(minutes=1))
        )

        run_id = await run_probe()

        assert (await ProbeRunManager.get(service_session, id=orphan.id)).status is (
            ProbeRunStatus.FAILED
        )
        stored = await ProbeRunManager.get(service_session, id=run_id)
        assert stored.status is ProbeRunStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_a_young_worker_leaves_unclaimed_runs_to_their_message(
        self, service_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leave a run nobody claimed alone until ``UNCLAIMED_GRACE`` has passed.

        Its message may have survived a restart of the worker alone, and failing the
        run now would make the claim refuse that message.

        :param service_session: The session every sweep function opens.
        :param monkeypatch: Restores the real boot after the test.
        """
        young = WorkerBoot(boot_id=uuid4(), booted_at=utc_now())
        monkeypatch.setattr(WorkerBoot, "current", young)
        queued = await ProbeRunManager.save(
            service_session, ProbeRun(started_at=young.booted_at - timedelta(minutes=1))
        )

        await run_probe()

        stored = await ProbeRunManager.get(service_session, id=queued.id)
        assert stored.status is ProbeRunStatus.RUNNING

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("boot")
    @pytest.mark.parametrize(
        ("run_status", "sweeps"),
        [
            pytest.param(ProbeRunStatus.RUNNING, 1, id="waiting"),
            pytest.param(ProbeRunStatus.FAILED, 0, id="already-reaped"),
        ],
    )
    async def test_a_triggered_run_sweeps_only_if_still_waiting(
        self,
        service_session: AsyncSession,
        swept: AsyncMock,
        run_status: ProbeRunStatus,
        sweeps: int,
    ) -> None:
        """Sweep a run the trigger made, unless the reaper failed it first.

        :param service_session: The session every sweep function opens.
        :param swept: The stubbed sweep.
        :param run_status: The run's status when its message arrives.
        :param sweeps: How many sweeps are expected.
        """
        run = await ProbeRunManager.save(service_session, ProbeRun(status=run_status))

        await run_probe(execution_id=run.id)

        assert swept.await_count == sweeps

    @pytest.mark.asyncio
    async def test_the_sweep_is_told_its_run(
        self, service_session: AsyncSession, swept: AsyncMock
    ) -> None:
        """Pass the run to the sweep, which names it on every probe it dispatches.

        :param service_session: The session every sweep function opens.
        :param swept: The stubbed sweep.
        """
        run_id = await run_probe()

        swept.assert_awaited_once()
        assert swept.await_args_list[0].args[3] == run_id


class TestTriggerThatCannotQueue:
    """Fail the run, rather than leave it running, when its task cannot be queued."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("enabled")
    async def test_answers_503_and_fails_the_run(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer 503 and record why on the run.

        :param api: The authenticated client.
        :param session: The database session.
        """
        with patch.object(
            celery_module.run_om_probe,
            "delay",
            side_effect=KombuOperationalError("Connection refused"),
        ):
            response = await api.post(f"{BASE}/runs")

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        runs = await ProbeRunManager.list(session)
        assert [(run.status, run.error) for run in runs] == [
            (ProbeRunStatus.FAILED, QUEUE_UNAVAILABLE_DETAIL)
        ]


class TestRecoveryAtWorkerStart:
    """Record the boot before the pool forks, and recover once the worker is ready."""

    def test_record_worker_boot_sets_this_process_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record a fresh boot for every child to inherit.

        :param monkeypatch: Restores the real boot after the test.
        """
        monkeypatch.setattr(WorkerBoot, "current", None)

        celery_module.record_worker_boot()

        assert WorkerBoot.current is not None

    def test_recovery_is_queued_now_and_after_the_grace(self) -> None:
        """Queue one pass at once and one once unclaimed runs may be failed."""
        with patch.object(celery_module.recover_om_runs, "apply_async") as queued:
            celery_module.queue_run_recovery()

        countdowns = [call.kwargs["countdown"] for call in queued.call_args_list]
        assert countdowns == [0, UNCLAIMED_GRACE.total_seconds()]

    def test_a_failed_publish_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log a broker that refuses the recovery, and let the worker start.

        :param caplog: Captures the log.
        """
        with patch.object(
            celery_module.recover_om_runs,
            "apply_async",
            side_effect=KombuOperationalError("Connection refused"),
        ):
            celery_module.queue_run_recovery()

        assert "could not queue the recovery" in caplog.text

    @pytest.mark.parametrize(
        "not_yet",
        [
            pytest.param(ConnectionRefusedError(), id="database-down"),
            pytest.param(
                ProgrammingError("SELECT", {}, Exception("no such column")),
                id="not-yet-migrated",
            ),
        ],
    )
    def test_retries_until_the_database_is_ready(
        self, boot: WorkerBoot, not_yet: Exception
    ) -> None:
        """Try again while the database is down or not migrated, then recover.

        :param boot: This worker's boot.
        :param not_yet: What the first attempt raises.
        """
        attempts = [not_yet, None]
        recover = AsyncMock(side_effect=attempts)

        with patch.object(celery_module, "recover_interrupted_runs", recover):
            result = celery_module.recover_om_runs.apply()

        assert result.successful()
        assert recover.await_count == len(attempts)
        recover.assert_awaited_with(boot)

    def test_without_a_boot_there_is_nothing_to_recover(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Do nothing outside a worker.

        :param monkeypatch: Restores the real boot after the test.
        """
        monkeypatch.setattr(WorkerBoot, "current", None)
        recover = AsyncMock()

        with patch.object(celery_module, "recover_interrupted_runs", recover):
            celery_module.recover_om_runs.apply()

        recover.assert_not_awaited()


class TestFailureRetry:
    """Write a failure the sweep could not, once the database answers again."""

    def test_retries_until_the_database_answers(self) -> None:
        """Try again while the database is unreachable, then write the failure."""
        attempts = [ConnectionRefusedError(), TimeoutError(), None]
        fail = AsyncMock(side_effect=attempts)

        with patch.object(celery_module, "fail_run", fail):
            result = celery_module.fail_om_run.apply((str(RUN_ID), "boom"))

        assert result.successful()
        assert fail.await_count == len(attempts)
        fail.assert_awaited_with(RUN_ID, "boom")

    def test_another_database_error_is_not_retried(self) -> None:
        """Let an error that waiting cannot fix fail the task at once."""
        fail = AsyncMock(side_effect=IntegrityError("UPDATE", {}, Exception("dup")))

        with patch.object(celery_module, "fail_run", fail):
            result = celery_module.fail_om_run.apply((str(RUN_ID), "boom"))

        assert result.failed()
        assert fail.await_count == 1

    def test_the_scan_task_queues_it_to_expire_with_stale_run_after(self) -> None:
        """Queue the retry, expiring once the trigger's age check fails the run anyway.

        The scan task still fails, so Celery records the sweep as failed.
        """
        unrecorded = FailureNotRecordedError(RUN_ID, "boom")

        with (
            patch.object(celery_module, "run_probe", AsyncMock(side_effect=unrecorded)),
            patch.object(celery_module.fail_om_run, "apply_async") as queued,
            pytest.raises(FailureNotRecordedError),
        ):
            celery_module.run_om_probe.run()

        assert queued.call_args.args[0] == (str(RUN_ID), "boom")
        expires = queued.call_args.kwargs["expires"] - utc_now()
        assert abs(expires - om_inventory_settings.STALE_RUN_AFTER) < timedelta(
            seconds=5
        )

    def test_a_failed_publish_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log a broker that refuses the retry; the age check is all that is left.

        :param caplog: Captures the log.
        """
        unrecorded = FailureNotRecordedError(RUN_ID, "boom")

        with (
            patch.object(celery_module, "run_probe", AsyncMock(side_effect=unrecorded)),
            patch.object(
                celery_module.fail_om_run,
                "apply_async",
                side_effect=KombuOperationalError("Connection refused"),
            ),
            pytest.raises(FailureNotRecordedError),
        ):
            celery_module.run_om_probe.run()

        assert "could not queue the recovery of the failure" in caplog.text


class _DriverError(Exception):
    """Carry a SQLSTATE the way the PostgreSQL driver's errors do.

    :param sqlstate: The SQLSTATE.
    """

    def __init__(self, sqlstate: str) -> None:
        """Hold the SQLSTATE.

        :param sqlstate: The SQLSTATE.
        """
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _wrapped(orig: Exception, *, invalidated: bool = False) -> DBAPIError:
    """Wrap ``orig`` as SQLAlchemy does a driver error.

    :param orig: The driver error.
    :param invalidated: Whether SQLAlchemy found the connection gone.
    :return: The wrapped error.
    """
    return DBAPIError("UPDATE", {}, orig, connection_invalidated=invalidated)


class TestDatabaseUnreachable:
    """Retry only what waiting for PMM Server's database can fix."""

    @pytest.mark.parametrize(
        ("exc", "unreachable"),
        [
            pytest.param(ConnectionRefusedError(111, "refused"), True, id="refused"),
            pytest.param(TimeoutError(), True, id="timed-out"),
            pytest.param(socket.gaierror(-2, "unknown host"), True, id="no-dns"),
            pytest.param(_DriverError("57P03"), True, id="starting-up-raw"),
            pytest.param(_wrapped(_DriverError("57P01")), True, id="shut-down"),
            pytest.param(_wrapped(_DriverError("08006")), True, id="connection-lost"),
            pytest.param(
                _wrapped(Exception("closed"), invalidated=True), True, id="invalidated"
            ),
            pytest.param(FileNotFoundError("root.crt"), False, id="missing-cert"),
            pytest.param(_wrapped(_DriverError("23505")), False, id="unique-violation"),
            pytest.param(
                IntegrityError("UPDATE", {}, Exception("dup")), False, id="integrity"
            ),
        ],
    )
    def test_classifies_the_error(self, exc: Exception, *, unreachable: bool) -> None:
        """Tell an outage apart from an error that would recur.

        :param exc: What a database call raised.
        :param unreachable: Whether it is an outage.
        """
        assert database_unreachable(exc) is unreachable

    def test_a_wrapped_cause_is_found(self) -> None:
        """Find the outage under an error raised from it."""
        exc = RuntimeError("write failed")
        exc.__cause__ = _DriverError("57P01")

        assert database_unreachable(exc)
