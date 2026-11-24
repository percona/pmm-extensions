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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory import service as service_module
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.dispatch import build_config, HostProbeResult
from app.extensions.apps.om_inventory.models import (
    ObservationSubjectKind,
    ProbeRun,
    ProbeRunStatus,
    ScanProfile,
)
from app.extensions.apps.om_inventory.service import run_scan


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


def _subject(subject_key: str, host: str) -> dict:
    """Build one stored scan subject, as pmm-managed sends and the run persists.

    :param subject_key: The key the subject's observation lands under.
    :param host: The subject's host address.
    :return: The subject as a JSON-shaped dict.
    """
    return {
        "subject_kind": ObservationSubjectKind.MEMBER.value,
        "subject_key": subject_key,
        "host": host,
        "service_id": f"svc-{subject_key}",
    }


def _host_result(host: str) -> HostProbeResult:
    """Build a probe result carrying a host record with scan output.

    :param host: The executor host the result is for.
    :return: The result, as :func:`probe_all` returns one.
    """
    return HostProbeResult(
        executor_host=host,
        host_record={
            "systemd_units": {"mongod": "ActiveState=active"},
            "pbm_status": '{"running": {}}',
        },
    )


@asynccontextmanager
async def _patched(
    session: AsyncSession, results: dict[str, HostProbeResult]
) -> AsyncIterator[AsyncMock]:
    """Patch the run's dispatch and ingest seams, yielding the ingest mock.

    :param session: The test session the run reuses.
    :param results: What the mocked ``probe_all`` returns, keyed by host.
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
        results = {"host-a": _host_result("host-a"), "host-b": _host_result("host-b")}

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
        assert first["probe_facts"] is None
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS

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
        results = {"host-a": _host_result("host-a")}

        async with _patched(session, results) as ingest:
            await run_scan(run.id)

        assert ingest.await_count == 1
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.PARTIAL

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


class TestBuildConfigScanUnits:
    """Assert the scan units ride the payload config only on a scan."""

    def test_scan_units_are_added_when_present(self) -> None:
        """Place the scan units in the config a scan dispatches."""
        config = json.loads(build_config([], scan_units=["mongod", "pbm-agent"]))
        assert config["scan_units"] == ["mongod", "pbm-agent"]

    def test_the_monitored_sweep_config_is_unchanged(self) -> None:
        """Leave ``scan_units`` out of the config when the sweep sends none."""
        assert "scan_units" not in json.loads(build_config([]))
