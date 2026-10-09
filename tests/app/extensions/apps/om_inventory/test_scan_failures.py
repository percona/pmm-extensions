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

"""Test that a failed scan says what kind of failure it was, and why.

A resolution hint is keyed off ``last_error_code``, so each way a scan can fail has
to land on its own code, and the message beside it has to carry the node's own
account of the failure. The case that matters most is a node that cannot build the
scan's environment: the payload never starts, so the payload's streams are empty,
and the reason is only in the ``prepare-env`` step's output and the run's
``failure_reason``.
"""

import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import String, type_coerce
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.requests import RemoteAPI
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import list_hosts, upsert_host
from app.extensions.apps.om_inventory.dispatch import (
    classify_terminal_failure,
    probe_host,
    TRUNCATION_MARK,
)
from app.extensions.apps.om_inventory.enumeration import InventoryHost
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import ExecutorState, MappedService
from app.extensions.apps.om_inventory.models import NodeResolution, OmHost, ScanFailure
from app.extensions.apps.om_inventory.service import (
    classify_record_failure,
    persist_estate,
    SweepOutcome,
)
from app.tasks.models import TaskHistoryStatusEnum
from tests.app.extensions.apps.om_inventory.conftest import (
    BASE,
    ERROR_DETAIL_CAP,
    HOST,
)

HISTORY_ID = 811
NODE_ID = "id-db00"
#: MongoDB's ``AuthenticationFailed``.
AUTH_FAILED = 18
#: The payload's record for the host itself, which carries a null ``service``.
HOST_RECORD = {"service": None, "system": {"os_name": "Ubuntu 24.04"}}
#: What the tasks service says when ``prepare-env`` cannot find ``python3``.
NO_PYTHON = "Step 'prepare-env' failed (exit code 127)."


def entries() -> list[MappedService]:
    """Build the one resolved service a host serves.

    :return: The mapping the dispatch is built from.
    """
    return [
        MappedService(
            service=InventoryService(
                service_id=1,
                external_id="ff0275b6-3633-474a-8068-3c39d3c7a4da",
                name="svc",
                port=27017,
                node_name=HOST,
                node_address="10.0.0.1",
            ),
            executor_host=HOST,
            resolution=NodeResolution.NAME,
        )
    ]


def log_line(step: str, stream: str, msg: str) -> str:
    """Build one line of the tasks API's log stream.

    :param step: The step that wrote it.
    :param stream: ``stdout`` or ``stderr``.
    :param msg: What it wrote.
    :return: The NDJSON line.
    """
    return json.dumps({"step": step, "type": stream, "msg": msg})


def make_api(history: dict[str, Any], logs: list[str] | None = None) -> MagicMock:
    """Build a tasks API stub for a dispatch that reaches one terminal history.

    :param history: What ``GET /history/{id}`` answers, ``id`` aside.
    :param logs: The lines the log stream yields, every step's.
    :return: The stub.
    """
    api = MagicMock(spec=RemoteAPI)

    async def get(path: str, **_: Any) -> dict[str, Any]:
        return {"id": HISTORY_ID, **history}

    async def stream(path: str, **_: Any) -> AsyncIterator[str]:
        for line in logs or []:
            yield line

    api.get = AsyncMock(side_effect=get)
    api.post = AsyncMock(return_value={"id": HISTORY_ID})
    api.stream = MagicMock(side_effect=stream)
    return api


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the poll loop take a test's worth of time."""
    monkeypatch.setattr(om_inventory_settings, "TASK_TIMEOUT", 1)
    monkeypatch.setattr(om_inventory_settings, "POLL_INTERVAL", 1)


class TestADispatchSaysWhatKindOfFailureItWas:
    """Pin one code per way a dispatch can fail."""

    @pytest.mark.asyncio
    async def test_a_node_without_python_names_the_failed_step_and_its_output(
        self,
    ) -> None:
        """Report a failed ``prepare-env`` from its own output, not "no output"."""
        api = make_api(
            {"status": TaskHistoryStatusEnum.FAILED.value, "failure_reason": NO_PYTHON},
            [log_line("prepare-env", "stderr", "sh: 1: python3: not found\n")],
        )

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.ENVIRONMENT_SETUP_FAILED
        assert result.error == (
            f"scan failed on {HOST}: {NO_PYTHON} sh: 1: python3: not found"
        )

    @pytest.mark.asyncio
    async def test_a_silent_failed_step_is_still_named_with_its_exit_code(
        self,
    ) -> None:
        """Name the node, the step and its exit code when the step printed nothing."""
        api = make_api(
            {"status": TaskHistoryStatusEnum.FAILED.value, "failure_reason": NO_PYTHON}
        )

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.ENVIRONMENT_SETUP_FAILED
        assert result.error == f"scan failed on {HOST}: {NO_PYTHON}"

    @pytest.mark.asyncio
    async def test_a_step_silent_on_stderr_is_reported_from_its_stdout(self) -> None:
        """Fall back to the failed step's stdout, where ``venv`` reports ensurepip."""
        api = make_api(
            {
                "status": TaskHistoryStatusEnum.FAILED.value,
                "failure_reason": "Step 'prepare-env' failed (exit code 1).",
            },
            [
                log_line(
                    "prepare-env",
                    "stdout",
                    "The virtual environment was not created successfully because "
                    "ensurepip is not available.\n",
                )
            ],
        )

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.ENVIRONMENT_SETUP_FAILED
        assert "ensurepip is not available" in (result.error or "")

    @pytest.mark.asyncio
    async def test_a_crashed_payload_reports_the_end_of_its_traceback(self) -> None:
        """Keep the end of a long stderr, where the exception is."""
        traceback = "Traceback (most recent call last):\n" + "  frame\n" * 200
        api = make_api(
            {
                "status": TaskHistoryStatusEnum.FAILED.value,
                "failure_reason": "Step 'run-script' failed (exit code 1).",
            },
            [
                log_line(
                    "run-script", "stderr", traceback + "SyntaxError: invalid syntax\n"
                )
            ],
        )

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.SCAN_CRASHED
        assert (result.error or "").endswith("SyntaxError: invalid syntax")

    @pytest.mark.asyncio
    async def test_a_lost_run_is_scan_lost(self) -> None:
        """Report a run the node never finished as lost, not as a crash."""
        api = make_api(
            {"status": TaskHistoryStatusEnum.LOST.value, "failure_reason": None}
        )

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.SCAN_LOST
        assert result.error == f"scan lost on {HOST}: no output"

    @pytest.mark.asyncio
    async def test_a_run_that_never_started_is_not_a_timeout_of_the_node(
        self,
    ) -> None:
        """Tell a scan still queued apart from one that ran too long."""
        api = make_api({"status": TaskHistoryStatusEnum.PENDING.value})

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.NOT_STARTED
        assert result.error == (
            f"the scan on {HOST} did not start within 1s and was cancelled (task "
            f"history {HISTORY_ID})"
        )

    @pytest.mark.asyncio
    async def test_a_dispatch_the_tasks_api_refused_was_never_queued(self) -> None:
        """Report a refused dispatch as never queued."""
        api = make_api({"status": TaskHistoryStatusEnum.SUCCESS.value})
        api.post = AsyncMock(side_effect=RuntimeError("connection refused"))

        result = await probe_host(api, HOST, entries())

        assert result.error_code == ScanFailure.DISPATCH_REJECTED
        assert result.error == (
            f"could not queue the scan on {HOST}: RuntimeError: connection refused"
        )

    @pytest.mark.asyncio
    async def test_only_the_payload_s_stdout_is_read_as_records(self) -> None:
        """Parse records from ``run-script`` alone, though every step is streamed."""
        api = make_api(
            {"status": TaskHistoryStatusEnum.SUCCESS.value},
            [
                # A JSON line from another step must not be taken for the payload's.
                log_line("prepare-env", "stdout", '{"service": null}\n'),
                log_line("run-script", "stdout", json.dumps(HOST_RECORD) + "\n"),
            ],
        )

        result = await probe_host(api, HOST, entries())

        assert result.error is None
        assert result.error_code is None
        assert result.host_record == HOST_RECORD


class TestTheDetailIsBounded:
    """Keep the stored detail within the cap, and keep the part that matters."""

    @pytest.mark.asyncio
    async def test_a_long_stream_keeps_the_node_the_step_and_its_end(self) -> None:
        """Cut a long stream from its start, after the node and the failed step."""
        last_line = "ERROR: No matching distribution found for pymongo<5,>=4.6"
        api = make_api(
            {
                "status": TaskHistoryStatusEnum.FAILED.value,
                "failure_reason": "Step 'prepare-env' failed (exit code 1).",
            },
            [
                log_line(
                    "prepare-env",
                    "stderr",
                    "Collecting pymongo\n" * 200 + last_line + "\n",
                )
            ],
        )

        result = await probe_host(api, HOST, entries())

        error = result.error or ""
        assert len(error) == ERROR_DETAIL_CAP
        assert error.startswith(
            f"scan failed on {HOST}: Step 'prepare-env' failed (exit code 1). "
            f"{TRUNCATION_MARK}"
        )
        assert error.endswith(last_line)

    @pytest.mark.asyncio
    async def test_a_failed_release_keeps_its_notice_whole(self) -> None:
        """Cut the failure before an unreleased run, never the notice about it."""
        api = make_api({"status": TaskHistoryStatusEnum.RUNNING.value})
        api.get = AsyncMock(side_effect=RuntimeError("x" * 5000))
        api.post = AsyncMock(
            side_effect=[{"id": HISTORY_ID}, RuntimeError("stop refused")]
        )

        result = await probe_host(api, HOST, entries())

        error = result.error or ""
        assert result.error_code == ScanFailure.BLOCKED
        assert len(error) == ERROR_DETAIL_CAP
        assert error.startswith(f"the scan on {HOST} failed: RuntimeError: xxx")
        assert error.endswith(
            f"{TRUNCATION_MARK} -- and task history {HISTORY_ID} could not be "
            "released, so it will block this node's next scan: RuntimeError: stop "
            "refused"
        )


class TestTheFailedStepIsReadFromTheTasksService:
    """Pin the pattern against the sentence the tasks service actually writes."""

    @pytest.mark.parametrize(
        ("step", "expected"),
        [
            ("prepare-env", ScanFailure.ENVIRONMENT_SETUP_FAILED),
            ("run-script", ScanFailure.SCAN_CRASHED),
            ("clean-up", ScanFailure.UNKNOWN),
        ],
    )
    def test_the_executor_s_own_reason_is_classified(
        self, step: str, expected: ScanFailure
    ) -> None:
        """Classify the sentence the Nomad executor writes for a failed step.

        :param step: The step that failed.
        :param expected: The code it should map to.
        """
        reason = f"Step '{step}' failed (exit code 127)."

        assert (
            classify_terminal_failure(TaskHistoryStatusEnum.FAILED.value, reason)
            == expected
        )

    def test_a_failure_with_no_reason_is_unknown(self) -> None:
        """Give no specific code where nothing says which step failed."""
        assert (
            classify_terminal_failure(TaskHistoryStatusEnum.FAILED.value, None)
            == ScanFailure.UNKNOWN
        )

    @pytest.mark.parametrize(
        "status", sorted(TaskHistoryStatusEnum.interrupted_statuses())
    )
    def test_an_interrupted_run_is_scan_lost_whatever_step_it_names(
        self, status: TaskHistoryStatusEnum
    ) -> None:
        """Report a run ended from outside as lost, not as the step it was in.

        :param status: A status the run was ended in rather than finished.
        """
        assert classify_terminal_failure(status.value, NO_PYTHON) == (
            ScanFailure.SCAN_LOST
        )


class TestAFailedRecordIsClassifiedByItsType:
    """Key a database failure off its exception type and code, not its message."""

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            (
                {"error_type": "OperationFailure", "error_code": AUTH_FAILED},
                ScanFailure.DATABASE_AUTH_FAILED,
            ),
            (
                {"error_type": "ServerSelectionTimeoutError", "error_code": None},
                ScanFailure.DATABASE_UNREACHABLE,
            ),
            (
                {"error_type": "ModuleNotFoundError"},
                ScanFailure.ENVIRONMENT_SETUP_FAILED,
            ),
            (
                # A server error that is not an authentication failure.
                {"error_type": "OperationFailure", "error_code": 13},
                ScanFailure.DATABASE_ERROR,
            ),
            # A payload predating error_type still fails, just without a kind.
            ({}, ScanFailure.DATABASE_ERROR),
        ],
    )
    def test_each_type_maps_to_its_code(
        self, record: dict[str, Any], expected: ScanFailure
    ) -> None:
        """Map each recorded exception type to its code.

        :param record: The failed record's type fields.
        :param expected: The code it should map to.
        """
        assert (
            classify_record_failure({"status": "failed", "error": "x", **record})
            == expected
        )


def failing_host() -> InventoryHost:
    """Build one dispatched host for the write-path tests.

    :return: The host.
    """
    return InventoryHost(
        node_id=NODE_ID,
        name="db00",
        address="10.0.0.1",
        executor_host="db00",
        resolution=NodeResolution.NAME,
        executor_state=ExecutorState(
            "db00", "10.0.0.1", reachable=True, driver_healthy=True
        ),
    )


async def persist(session: AsyncSession, outcome: SweepOutcome) -> None:
    """Write a sweep's outcome into the session's database.

    :param session: The database session.
    :param outcome: What the sweep produced.
    """
    with patch(
        "app.extensions.apps.om_inventory.service.get_async_session_maker",
        return_value=lambda: nullcontext(session),
    ):
        await persist_estate(outcome, uuid4())


class TestTheCodeReachesTheRow:
    """Store the code beside the message, and clear both on recovery."""

    @pytest.mark.asyncio
    async def test_a_failure_stores_its_code_and_a_success_clears_it(
        self, session: AsyncSession
    ) -> None:
        """Keep the code while failing and drop it on the next success.

        :param session: The database session.
        """
        outcome = SweepOutcome(total=0, hosts=[failing_host()])
        outcome.dispatched.add("db00")
        outcome.fail_host(
            NODE_ID,
            f"scan failed on db00: {NO_PYTHON}",
            ScanFailure.ENVIRONMENT_SETUP_FAILED,
        )
        await persist(session, outcome)

        stored = (await list_hosts(session))[0]
        assert stored.last_error_code is ScanFailure.ENVIRONMENT_SETUP_FAILED
        # Stored as the value the API reports, which rows written before the column
        # was an enum already hold, so they read back too.
        raw = await session.exec(
            select(type_coerce(col(OmHost.last_error_code), String))
        )
        assert raw.one() == ScanFailure.ENVIRONMENT_SETUP_FAILED.value

        recovered = SweepOutcome(total=0, hosts=[failing_host()])
        recovered.dispatched.add("db00")
        recovered.host_documents["db00"] = {"collected_at": "2026-10-06T12:00:00+00:00"}
        await persist(session, recovered)

        stored = (await list_hosts(session))[0]
        assert stored.last_error is None
        assert stored.last_error_code is None

    @pytest.mark.asyncio
    async def test_the_api_reports_the_code(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Return the code and the run that produced it, beside ``last_error``.

        The run id is what lets a reader of the failure open the run itself.

        :param api: The authenticated client.
        :param session: The database session.
        """
        run_id = uuid4()
        await upsert_host(
            session,
            node_id=NODE_ID,
            name="db00",
            address="10.0.0.1",
            executor_host="db00",
            error="scan lost on db00: no output",
            error_code=ScanFailure.SCAN_LOST,
            run_id=run_id,
        )
        await session.commit()

        response = await api.get(f"{BASE}/hosts")

        assert response.status_code == status.HTTP_200_OK
        host = response.json()["items"][0]
        assert host["last_error"] == "scan lost on db00: no output"
        assert host["last_error_code"] == ScanFailure.SCAN_LOST.value
        assert host["last_run_id"] == str(run_id)
