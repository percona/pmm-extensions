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

"""Test that ``run_scan`` dispatches per subject and records the right terminal status.

The probe dispatch and the PL/pgSQL observation write are mocked here: this pins the
orchestration - which subjects are dispatched, what each subject is ingested with, and
how the run's terminal status follows from how many answered. The ingest itself is
covered against real PostgreSQL in ``test_om_observation_postgres.py``.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory import service as service_module
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.dispatch import build_config, HostProbeResult
from app.extensions.apps.om_inventory.inventory import DEFAULT_MONGODB_PORT
from app.extensions.apps.om_inventory.models import (
    ObservationSubjectKind,
    ProbeRun,
    ProbeRunStatus,
    ScanProfile,
    ScanSubject,
)
from app.extensions.apps.om_inventory.service import (
    _probe_facts_from_record,
    _scan_target,
    _scan_terminal_status,
    run_scan,
)
from app.extensions.apps.shared.om.classification import ingestible_facts

_SUBJECT_PORT = 27019
_CACHED_PORT = 27018


def _session_maker(
    session: AsyncSession,
):  # scaffolding-dup-ok: mirrors test_run_probe_failure
    """Build the ``get_async_session_maker`` stand-in the run functions expect.

    :param session: The test session every call should reuse.
    :return: The stand-in callable.
    """
    return lambda: nullcontext(session)


@asynccontextmanager
async def _no_heartbeat(_run_id: object) -> AsyncIterator[None]:
    """Stand in for the run heartbeat, which needs no background writes under test.

    :param _run_id: The run, unused.
    :return: Nothing; the body runs without a heartbeat.
    """
    yield


@asynccontextmanager
async def _held_clients(*_args: object, **_kwargs: object) -> AsyncIterator[tuple]:
    """Yield a tasks client whose ``auth`` is a no-op, since the dispatch is mocked.

    :return: An inventory and a tasks client stand-in.
    """
    tasks = MagicMock()
    tasks.auth = lambda _token: nullcontext()
    yield MagicMock(), tasks


def _subject(
    subject_key: str,
    host: str,
    *,
    with_service: bool = True,
    port: int | None = None,
    connect_host: str | None = None,
) -> dict:
    """Build one stored scan subject, as pmm-managed sends and the run persists.

    :param subject_key: The key the subject's observation lands under.
    :param host: The subject's host address.
    :param with_service: Whether the subject carries a service id, so it becomes a
        probe target; ``False`` makes it a host-only subject.
    :param port: The port pmm-managed sent for the member, if any.
    :param connect_host: The MongoDB connect host overriding the dispatch host, if any.
    :return: The subject as a JSON-shaped dict.
    """
    subject = {
        "subject_kind": ObservationSubjectKind.MEMBER.value,
        "subject_key": subject_key,
        "host": host,
        "service_id": f"svc-{subject_key}" if with_service else None,
    }
    if port is not None:
        subject["port"] = port
    if connect_host is not None:
        subject["connect_host"] = connect_host
    return subject


def _answered_record() -> dict:
    """Build a service probe record that answered, with the accepted-state facts.

    :return: A record whose status is not a failure, so it counts as an answer.
    """
    return {
        "status": "ok",
        "binary_version": "7.0.14",
        "database": {"set_name": "rs0", "state": 1, "config_sha256": "a" * 64},
    }


def _failed_record() -> dict:
    """Build a service probe record that reports a failed database query.

    :return: A record whose status marks it a failed attempt, so it is not an answer.
    """
    return {"status": "failed", "error": "connection refused"}


def _host_result(host: str, records: dict[str, dict] | None = None) -> HostProbeResult:
    """Build a probe result carrying a host record with scan output.

    :param host: The executor host the result is for.
    :param records: The per-target records keyed by service id, if any.
    :return: The result, as :func:`probe_all` returns one.
    """
    return HostProbeResult(
        executor_host=host,
        host_record={
            "systemd_units": {"mongod": "ActiveState=active"},
            "pbm_status": '{"running": {}}',
        },
        records=records or {},
    )


@asynccontextmanager
async def _patched(
    session: AsyncSession,
    results: dict[str, HostProbeResult],
    service: object | None = None,
) -> AsyncIterator[AsyncMock]:
    """Patch the run's dispatch and ingest seams, yielding the ingest mock.

    :param session: The test session the run reuses.
    :param results: What the mocked ``probe_all`` returns, keyed by host.
    :param service: The service ``get_service`` resolves for every subject, or
        ``None`` so a subject contributes no probe target.
    :return: The ``ingest_collected`` mock, so a test can assert on it.
    """
    ingest = AsyncMock(return_value=True)
    with (
        patch.object(
            service_module,
            "get_async_session_maker",
            return_value=_session_maker(session),
        ),
        patch.object(service_module, "_heartbeat", _no_heartbeat),
        patch.object(service_module, "_held_clients", _held_clients),
        patch.object(service_module, "get_internal_token", return_value="tok"),
        patch.object(service_module, "get_service", AsyncMock(return_value=service)),
        patch.object(
            service_module, "probe_all", AsyncMock(return_value=results)
        ) as probe_all,
        patch.object(service_module, "ingest_collected", ingest),
    ):
        ingest.probe_all = probe_all
        yield ingest


class TestRunScan:
    """Assert the per-subject dispatch and the terminal status it yields."""

    @pytest.mark.asyncio
    async def test_empty_subjects_terminate_without_dispatch(
        self, session: AsyncSession
    ) -> None:
        """Finish a subjectless scan ``FAILED`` with nothing dispatched.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session, ProbeRun(profile=ScanProfile.MANAGED, subjects=[])
        )

        async with _patched(session, {}) as ingest:
            assert await run_scan(run.id) == run.id
            ingest.probe_all.assert_not_awaited()
            ingest.assert_not_awaited()

        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.finished_at is not None

    @pytest.mark.asyncio
    async def test_stores_an_observation_per_subject(
        self, session: AsyncSession
    ) -> None:
        """Ingest each subject's facts and finish ``SUCCESS`` when all answered.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(
                profile=ScanProfile.MANAGED,
                subjects=[_subject("m-1", "host-a"), _subject("m-2", "host-b")],
            ),
        )
        results = {
            "host-a": _host_result("host-a", records={"svc-m-1": _answered_record()}),
            "host-b": _host_result("host-b", records={"svc-m-2": _answered_record()}),
        }

        async with _patched(session, results) as ingest:
            await run_scan(run.id)

        assert ingest.await_count == len(results)
        keyed = {call.kwargs["subject_key"] for call in ingest.await_args_list}
        assert keyed == {"m-1", "m-2"}
        first = ingest.await_args_list[0].kwargs
        assert first["profile"] is ScanProfile.MANAGED
        assert first["subject_kind"] is ObservationSubjectKind.MEMBER
        assert first["systemd_units"] == {"mongod": "ActiveState=active"}
        assert first["pbm_status"] == '{"running": {}}'
        assert first["probe_facts"] is not None
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS
        assert stored.services_answered == len(run.subjects)
        assert stored.services_total == len(run.subjects)

    @pytest.mark.asyncio
    async def test_folds_in_classified_probe_facts_for_a_known_service(
        self, session: AsyncSession
    ) -> None:
        """Carry a subject's classified probe facts, dropping its secret-ish fields.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )
        record = {
            "binary_version": "7.0.14",
            "database": {"set_name": "rs0", "state": 1, "config_sha256": "a" * 64},
            "process": {"running": True, "argv": ["--keyFile", "/etc/mongod.key"]},
        }
        results = {"host-a": _host_result("host-a", records={"svc-m-1": record})}
        service = SimpleNamespace(name="rs0-svc", port=27017)

        async with _patched(session, results, service=service) as ingest:
            await run_scan(run.id)

        probe_facts = ingest.await_args.kwargs["probe_facts"]
        assert probe_facts is not None
        kept = ingestible_facts(probe_facts["facts"], probe_facts["classes"])
        assert kept["database.set_name"] == "rs0"
        assert kept["database.config_sha256"] == "a" * 64
        assert kept["binary_version"] == "7.0.14"
        assert "process.argv" not in kept

    @pytest.mark.asyncio
    async def test_partial_when_a_subject_host_did_not_answer(
        self, session: AsyncSession
    ) -> None:
        """Finish ``PARTIAL`` when some subjects answer and some do not.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(
                profile=ScanProfile.MANAGED,
                subjects=[_subject("m-1", "host-a"), _subject("m-2", "host-b")],
            ),
        )
        results = {
            "host-a": _host_result("host-a", records={"svc-m-1": _answered_record()})
        }

        async with _patched(session, results) as ingest:
            await run_scan(run.id)

        assert ingest.await_count == 1
        assert ingest.await_args.kwargs["subject_key"] == "m-1"
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.PARTIAL
        assert stored.services_answered == 1
        assert stored.services_total == len(run.subjects)

    @pytest.mark.asyncio
    async def test_failed_when_no_subject_answered(self, session: AsyncSession) -> None:
        """Finish ``FAILED`` when no subject's host answered.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )

        async with _patched(session, {}) as ingest:
            await run_scan(run.id)

        ingest.assert_not_awaited()
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_service_without_a_record_is_not_stored_or_counted(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skip a subject whose host answered but whose service returned no record.

        A host-only write for such a member would replace a prior complete
        observation, since the store keeps the newer result, so it is neither stored
        nor counted and the run does not read ``SUCCESS``.

        :param session: The database session.
        :param caplog: Captures the log line that names the skipped subject.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )
        results = {"host-a": _host_result("host-a")}

        with caplog.at_level(logging.INFO, logger=service_module.logger.name):
            async with _patched(session, results) as ingest:
                await run_scan(run.id)

        ingest.assert_not_awaited()
        assert any(
            "returned no usable record" in r.getMessage() for r in caplog.records
        )
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.services_answered == 0

    @pytest.mark.asyncio
    async def test_a_failed_service_record_is_not_an_answer(
        self, session: AsyncSession
    ) -> None:
        """Treat a failed database probe as no answer rather than a masked success.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )
        results = {
            "host-a": _host_result("host-a", records={"svc-m-1": _failed_record()})
        }

        async with _patched(session, results) as ingest:
            await run_scan(run.id)

        ingest.assert_not_awaited()
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_member_absent_from_the_cache_is_still_probed(
        self, session: AsyncSession
    ) -> None:
        """Dispatch a subject whose service the monitored sweep has not cached.

        ``_patched`` resolves ``get_service`` to ``None`` for every subject, standing
        in for a member the monitored sweep has not recorded; it must still become a
        target and, once its record comes back, answer.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )
        results = {
            "host-a": _host_result("host-a", records={"svc-m-1": _answered_record()})
        }

        async with _patched(session, results) as ingest:
            await run_scan(run.id)
            ingest.probe_all.assert_awaited_once()
            dispatched = ingest.probe_all.await_args.args[1]
            assert [target.service.external_id for target in dispatched] == ["svc-m-1"]

        ingest.assert_awaited_once()
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS
        assert stored.services_answered == 1

    @pytest.mark.asyncio
    async def test_a_host_only_subject_is_stored_and_counts(
        self, session: AsyncSession
    ) -> None:
        """Store a subject carrying no service id from its host record alone.

        It is not a probe target and does not count toward the service totals, but its
        host record is its answer, so the run reads ``SUCCESS``.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(
                profile=ScanProfile.MANAGED,
                subjects=[_subject("m-1", "host-a", with_service=False)],
            ),
        )
        results = {"host-a": _host_result("host-a")}

        async with _patched(session, results) as ingest:
            await run_scan(run.id)
            assert ingest.probe_all.await_args.args[1] == []

        ingest.assert_awaited_once()
        assert ingest.await_args.kwargs["probe_facts"] is None
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS
        assert stored.services_total == 0
        assert stored.services_answered == 0

    @pytest.mark.asyncio
    async def test_a_dispatch_failure_fails_the_run(
        self, session: AsyncSession
    ) -> None:
        """Mark the run ``FAILED`` when the dispatch raises, not leave it ``RUNNING``.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(
            session,
            ProbeRun(profile=ScanProfile.MANAGED, subjects=[_subject("m-1", "host-a")]),
        )
        fail_run = AsyncMock()

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(service_module, "_heartbeat", _no_heartbeat),
            patch.object(service_module, "_held_clients", _held_clients),
            patch.object(service_module, "get_internal_token", return_value="tok"),
            patch.object(
                service_module,
                "probe_all",
                AsyncMock(side_effect=RuntimeError("nomad down")),
            ),
            patch.object(service_module, "_fail_run", fail_run),
        ):
            assert await run_scan(run.id) == run.id

        fail_run.assert_awaited_once()
        assert fail_run.await_args.args[1] == "nomad down"


class TestScanTarget:
    """Assert the probe target is built from the subject with a port fallback chain."""

    def test_prefers_the_subject_port_over_the_cached_one(self) -> None:
        """Use the port pmm-managed sent over the cached service's port."""
        subject = ScanSubject.model_validate(
            _subject("m-1", "host-a", port=_SUBJECT_PORT)
        )
        target = _scan_target(subject, SimpleNamespace(name="svc", port=_CACHED_PORT))
        assert target.service.port == _SUBJECT_PORT
        assert target.service.external_id == "svc-m-1"
        assert target.service.node_address == "host-a"

    def test_falls_back_to_the_cached_port_then_the_default(self) -> None:
        """Take the cached port when the subject sent none, then the MongoDB default."""
        subject = ScanSubject.model_validate(_subject("m-1", "host-a"))
        cached = _scan_target(subject, SimpleNamespace(name="svc", port=_CACHED_PORT))
        assert cached.service.port == _CACHED_PORT
        uncached = _scan_target(subject, None)
        assert uncached.service.port == DEFAULT_MONGODB_PORT
        assert uncached.service.name == "m-1"

    def test_connects_to_the_connect_host_but_dispatches_to_the_host(self) -> None:
        """Connect to the subject's connect host while dispatching to its host."""
        subject = ScanSubject.model_validate(
            _subject("m-1", "exec-host", connect_host="db-host")
        )
        target = _scan_target(subject, None)
        assert target.executor_host == "exec-host"
        assert target.service.node_address == "db-host"


class TestScanTerminalStatus:
    """Assert the terminal status reflects how many subjects a scan observed."""

    @pytest.mark.parametrize(
        ("observed", "total", "expected"),
        [
            (0, 0, ProbeRunStatus.FAILED),
            (0, 2, ProbeRunStatus.FAILED),
            (1, 2, ProbeRunStatus.PARTIAL),
            (2, 2, ProbeRunStatus.SUCCESS),
        ],
        ids=["no-subjects", "none-observed", "some-observed", "all-observed"],
    )
    def test_maps_observed_counts_to_a_status(
        self, observed: int, total: int, expected: ProbeRunStatus
    ) -> None:
        """Map observed-of-total subject counts to the run's terminal status.

        :param observed: Subjects whose facts were stored.
        :param total: Subjects the scan was asked to collect.
        :param expected: The terminal status the counts must yield.
        """
        assert _scan_terminal_status(observed, total) is expected


class TestBuildConfigScanUnits:
    """Assert the scan units ride the payload config only on a scan."""

    def test_scan_units_are_added_when_present(self) -> None:
        """Place the scan units in the config a scan dispatches."""
        config = json.loads(build_config([], scan_units=["mongod", "pbm-agent"]))
        assert config["scan_units"] == ["mongod", "pbm-agent"]

    def test_the_monitored_sweep_config_is_unchanged(self) -> None:
        """Leave ``scan_units`` out of the config when the sweep sends none."""
        assert "scan_units" not in json.loads(build_config([]))


class TestProbeFactClassification:
    """Assert the probe-fact allowlist keeps the safe facts and drops the rest."""

    def test_allowlists_public_and_drops_unclassified(self) -> None:
        """Keep topology and version facts, drop argv, raw config and the host name."""
        record = {
            "status": "ok",
            "binary_version": "7.0.14",
            "system": {"os_name": "Ubuntu 24.04", "hostname": "internal-box-07"},
            "process": {
                "running": True,
                "program": "mongod",
                "argv": ["--keyFile", "/etc/mongod.key"],
                "config_path": "/etc/mongod.conf",
            },
            "database": {
                "set_name": "rs0",
                "state": 1,
                "raw": {"cmd_line_opts": {"security": {"keyFile": "/etc/mongod.key"}}},
            },
        }
        probe_facts = _probe_facts_from_record(record)
        kept = ingestible_facts(probe_facts["facts"], probe_facts["classes"])

        assert kept["binary_version"] == "7.0.14"
        assert kept["system.os_name"] == "Ubuntu 24.04"
        assert kept["process.program"] == "mongod"
        assert kept["process.running"] is True
        assert kept["database.set_name"] == "rs0"
        assert kept["database.state"] == 1
        for dropped in (
            "process.argv",
            "process.config_path",
            "system.hostname",
            "database.raw.cmd_line_opts.security.keyFile",
            "status",
        ):
            assert dropped not in kept
