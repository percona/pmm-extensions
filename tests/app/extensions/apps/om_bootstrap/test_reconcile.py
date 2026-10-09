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

"""Assert reconciliation maps TaskHistory status onto StepRecord, and nothing more."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import aiohttp
import pytest
from fastapi import HTTPException

from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPGoneException,
    HTTPNotFoundException,
)
from app.extensions.apps.om_bootstrap import reconcile
from app.extensions.apps.om_bootstrap.models import BootstrapRun, BootstrapRunStatus
from app.extensions.apps.om_bootstrap.persistence import (
    dump_host_states,
    dump_run_steps,
    parse_host_states,
    parse_run_steps,
)
from app.extensions.apps.om_bootstrap.strategy import (
    HostBootstrapState,
    StepRecord,
    StepStatus,
)
from app.extensions.apps.shared.om.task_failure import MAX_ERROR_DETAIL
from tests.app.extensions.apps.om_bootstrap.factories import BootstrapRunFactory

TASK_HISTORY_ID = 99

#: The reason the executor writes for ``run-script`` exiting 123, word for word;
#: the executor's own tests pin that it writes this sentence.
XARGS_REASON = "Step 'run-script' failed (exit code 123)."


def _tasks_api(
    history_status: str,
    *,
    failure_reason: str | None = None,
    logs: list[str] | None = None,
    logs_error: Exception | None = None,
) -> AsyncMock:
    """Build a Tasks API stub answering one ``TaskHistory`` and its log stream.

    :param history_status: What ``GET /history/{id}`` says the status is.
    :param failure_reason: Its ``failure_reason``.
    :param logs: The NDJSON lines ``GET /history/{id}/logs/`` streams.
    :param logs_error: Raised by the log stream instead of yielding ``logs``.
    :return: The stub.
    """
    api = AsyncMock()
    api.get.return_value = {"status": history_status, "failure_reason": failure_reason}

    async def stream(path: str, **_: Any) -> AsyncIterator[str]:
        assert path == f"/history/{TASK_HISTORY_ID}/logs/"
        if logs_error is not None:
            raise logs_error
        for line in logs or []:
            yield line

    api.stream = MagicMock(side_effect=stream)
    return api


def _log_line(stream: str, msg: str, step: str = "run-script") -> str:
    """Build one line of the Tasks API's log stream.

    :param stream: ``stdout`` or ``stderr``.
    :param msg: What the step wrote.
    :param step: The Nomad step that wrote it.
    :return: The NDJSON line.
    """
    return json.dumps({"step": step, "type": stream, "msg": msg})


class TestReconcileStep:
    """Assert reconcile_step's four outcomes: no-op x3, and a real transition."""

    @pytest.mark.asyncio
    async def test_a_pending_step_is_untouched(self) -> None:
        """Leave a step that hasn't started dispatching alone: nothing to reconcile."""
        step = StepRecord(name="pre_check", status=StepStatus.PENDING)

        result = await reconcile.reconcile_step(AsyncMock(), step)

        assert result is step

    @pytest.mark.asyncio
    async def test_a_running_step_with_no_task_history_id_is_untouched(self) -> None:
        """Leave a step marked running but never actually dispatched alone."""
        step = StepRecord(name="pre_check", status=StepStatus.RUNNING)

        result = await reconcile.reconcile_step(AsyncMock(), step)

        assert result is step

    @pytest.mark.asyncio
    async def test_a_still_running_dispatch_is_untouched(self) -> None:
        """Change nothing while the polled dispatch hasn't finished."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )

        result = await reconcile.reconcile_step(_tasks_api("running"), step)

        assert result is step

    @pytest.mark.asyncio
    async def test_a_succeeded_dispatch_marks_the_step_succeeded(self) -> None:
        """Map SUCCESS to StepStatus.SUCCEEDED, with a finish time."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )

        result = await reconcile.reconcile_step(_tasks_api("success"), step)

        assert result.status == StepStatus.SUCCEEDED
        assert result.finished_at is not None
        assert result.task_history_id == TASK_HISTORY_ID

    @pytest.mark.asyncio
    async def test_a_failed_dispatch_marks_the_step_failed_with_detail(self) -> None:
        """Map a non-SUCCESS terminal status to StepStatus.FAILED, with a detail."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )

        result = await reconcile.reconcile_step(_tasks_api("failed"), step)

        assert result.status == StepStatus.FAILED
        assert result.detail is not None
        assert str(TASK_HISTORY_ID) in result.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stream", "msg", "expected"),
        [
            (
                "stderr",
                (
                    "pre_check: /var/lib/mongo has 1.0 GiB free, but the data "
                    "directory /var/lib/mongo needs at least 5 GiB\n"
                ),
                (
                    "pre_check: /var/lib/mongo has 1.0 GiB free, but the data "
                    "directory /var/lib/mongo needs at least 5 GiB"
                ),
            ),
            (
                "stderr",
                (
                    "Curl error (7): Couldn't connect to server for "
                    "http://repo.percona.com/ [Failed to connect: Connection refused]\n"
                    "Error: Failed to download metadata for repo: All mirrors were tried\n"
                ),
                (
                    "Curl error (7): Couldn't connect to server for "
                    "http://repo.percona.com/ [Failed to connect: Connection refused]\n"
                    "Error: Failed to download metadata for repo: All mirrors were tried"
                ),
            ),
        ],
    )
    async def test_a_failed_script_s_detail_is_its_own_output(
        self, stream: str, msg: str, expected: str
    ) -> None:
        """Say why the script failed in its own words, naming the task history.

        Without the tasks service's "Step 'run-script' failed." in front: that names
        the job's internal step and says only that the script failed, which its
        output already says.

        :param stream: The stream the step wrote to.
        :param msg: What it wrote.
        :param expected: The part of it the detail should carry.
        """
        step = StepRecord(
            name="pre_check", status=StepStatus.RUNNING, task_history_id=TASK_HISTORY_ID
        )
        api = _tasks_api(
            "failed", failure_reason=XARGS_REASON, logs=[_log_line(stream, msg)]
        )

        result = await reconcile.reconcile_step(api, step)

        assert result.status == StepStatus.FAILED
        assert result.detail == f"{expected} (task history {TASK_HISTORY_ID})"

    @pytest.mark.asyncio
    async def test_a_silent_script_s_failure_is_still_said(self) -> None:
        """Say the script failed when it printed nothing, without ``xargs``' 123."""
        step = StepRecord(
            name="pre_check", status=StepStatus.RUNNING, task_history_id=TASK_HISTORY_ID
        )
        api = _tasks_api("failed", failure_reason=XARGS_REASON)

        result = await reconcile.reconcile_step(api, step)

        assert result.detail == (
            f"Step 'run-script' failed with no output. (task history {TASK_HISTORY_ID})"
        )

    @pytest.mark.asyncio
    async def test_another_step_s_failure_is_reported_from_that_step(self) -> None:
        """Keep a code other than 123 and read the step the reason names."""
        step = StepRecord(
            name="pre_check", status=StepStatus.RUNNING, task_history_id=TASK_HISTORY_ID
        )
        api = _tasks_api(
            "failed",
            failure_reason="Step 'check-launchable' failed (exit code 1).",
            logs=[
                _log_line("stderr", "sudo: not found\n", step="check-launchable"),
                _log_line("stderr", "unrelated\n"),
            ],
        )

        result = await reconcile.reconcile_step(api, step)

        assert result.detail == (
            "Step 'check-launchable' failed (exit code 1). sudo: not found "
            f"(task history {TASK_HISTORY_ID})"
        )

    @pytest.mark.asyncio
    async def test_a_long_output_keeps_its_end_within_the_cap(self) -> None:
        """Keep the end of a long stderr, where the error is, and bound it."""
        step = StepRecord(
            name="start_service",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        noise = "journal line\n" * 200
        error = '"errmsg":"Address already in use"'
        api = _tasks_api(
            "failed",
            failure_reason=XARGS_REASON,
            logs=[_log_line("stderr", noise + error + "\n")],
        )

        result = await reconcile.reconcile_step(api, step)

        assert result.detail is not None
        assert result.detail.endswith(f"{error} (task history {TASK_HISTORY_ID})")
        assert len(result.detail) <= MAX_ERROR_DETAIL + 100

    @pytest.mark.asyncio
    async def test_unreadable_logs_still_fail_the_step_with_its_reason(self) -> None:
        """Fail with the reason alone when the logs cannot be read."""
        step = StepRecord(
            name="pre_check", status=StepStatus.RUNNING, task_history_id=TASK_HISTORY_ID
        )
        api = _tasks_api(
            "failed",
            failure_reason="Step 'run-script' failed (exit code 124).",
            logs_error=HTTPGoneException(detail="gone"),
        )

        result = await reconcile.reconcile_step(api, step)

        assert result.status == StepStatus.FAILED
        assert result.detail == (
            "Step 'run-script' failed (exit code 124). "
            f"(task history {TASK_HISTORY_ID})"
        )

    @pytest.mark.asyncio
    async def test_a_failure_with_nothing_to_say_still_names_its_status(
        self,
    ) -> None:
        """Say how it ended when there is neither a reason nor any output."""
        step = StepRecord(
            name="pre_check", status=StepStatus.RUNNING, task_history_id=TASK_HISTORY_ID
        )

        result = await reconcile.reconcile_step(_tasks_api("lost"), step)

        assert result.detail == (
            f"Ended lost with no output (task history {TASK_HISTORY_ID})"
        )

    @pytest.mark.asyncio
    async def test_a_lost_dispatch_is_also_treated_as_failed(self) -> None:
        """Fail on LOST/STOPPED/STALE too: terminal but not success, never ignored."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )

        result = await reconcile.reconcile_step(_tasks_api("lost"), step)

        assert result.status == StepStatus.FAILED

    @pytest.mark.asyncio
    async def test_a_malformed_history_payload_raises_instead_of_hanging_forever(
        self,
    ) -> None:
        """Treat a non-dict TaskHistory body as a bad answer, not "still running"."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        tasks_api = AsyncMock()
        tasks_api.get.return_value = None

        with pytest.raises(HTTPBadGatewayException):
            await reconcile.reconcile_step(tasks_api, step)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{}, {"status": None}, {"status": 3}])
    async def test_a_history_payload_without_a_string_status_raises(
        self, payload: dict[str, object]
    ) -> None:
        """Reject a TaskHistory without a readable status as a bad upstream answer."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        tasks_api = AsyncMock()
        tasks_api.get.return_value = payload

        with pytest.raises(HTTPBadGatewayException):
            await reconcile.reconcile_step(tasks_api, step)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_reason", [3, ["Step 'run-script' failed."]])
    async def test_a_failed_history_with_a_non_string_reason_raises(
        self, failure_reason: int | list[str]
    ) -> None:
        """Reject a ``failure_reason`` that is neither a string nor null.

        :param failure_reason: The malformed reason.
        """
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        tasks_api = AsyncMock()
        tasks_api.get.return_value = {
            "status": "failed",
            "failure_reason": failure_reason,
        }

        with pytest.raises(HTTPBadGatewayException):
            await reconcile.reconcile_step(tasks_api, step)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [HTTPNotFoundException(detail="gone"), HTTPGoneException(detail="gone")],
    )
    async def test_a_vanished_history_record_marks_the_step_failed(
        self, error: HTTPException
    ) -> None:
        """Fail the step on a 404/410, since its dispatch can never be read again."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        tasks_api = AsyncMock()
        tasks_api.get.side_effect = error

        result = await reconcile.reconcile_step(tasks_api, step)

        assert result.status == StepStatus.FAILED
        assert result.finished_at is not None
        assert result.detail is not None
        assert "no longer exists" in result.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            aiohttp.ClientConnectionError("refused"),
            TimeoutError(),
            HTTPException(status_code=503, detail="unavailable"),
        ],
    )
    async def test_a_transient_read_failure_leaves_the_step_running(
        self, error: Exception
    ) -> None:
        """Leave the step running when the Tasks API is unreachable or failing."""
        step = StepRecord(
            name="install_package",
            status=StepStatus.RUNNING,
            task_history_id=TASK_HISTORY_ID,
        )
        tasks_api = AsyncMock()
        tasks_api.get.side_effect = error

        result = await reconcile.reconcile_step(tasks_api, step)

        assert result is step


class TestReconcileRun:
    """Assert reconcile_run updates run.hosts in place and reports any change."""

    def _run(
        self,
        steps: list[StepRecord],
        *,
        rollback_steps: list[StepRecord] | None = None,
        run_steps: list[StepRecord] | None = None,
        finalize_steps: list[StepRecord] | None = None,
    ) -> BootstrapRun:
        return BootstrapRunFactory.build(
            hosts=dump_host_states(
                [
                    HostBootstrapState(
                        host="node00",
                        steps=steps,
                        rollback_steps=rollback_steps or [],
                        finalize_steps=finalize_steps or [],
                    )
                ]
            ),
            run_steps=dump_run_steps(run_steps or []),
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_nothing_changed(self) -> None:
        """Report no change for a run with only pending/in-flight steps."""
        run = self._run([StepRecord(name="pre_check", status=StepStatus.PENDING)])

        changed = await reconcile.reconcile_run(AsyncMock(), run)

        assert changed is False

    @pytest.mark.asyncio
    async def test_persists_a_transitioned_step_back_onto_the_run(self) -> None:
        """Persist a completed dispatch's new status to run.hosts, not just memory."""
        run = self._run(
            [
                StepRecord(
                    name="install_package",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ]
        )

        changed = await reconcile.reconcile_run(_tasks_api("success"), run)

        assert changed is True
        reloaded = parse_host_states(run)
        assert reloaded[0].steps[0].status == StepStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_cleans_up_the_scratch_script_for_a_transitioned_step(self) -> None:
        """Remove the scratch script of a step that just reached a terminal status."""
        run = self._run(
            [
                StepRecord(
                    name="install_package",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ]
        )
        run.id = uuid4()

        with patch(
            "app.extensions.apps.om_bootstrap.reconcile.cleanup_step_script"
        ) as cleanup:
            await reconcile.reconcile_run(_tasks_api("success"), run)

        cleanup.assert_called_once_with(str(run.id), "node00", "install_package")

    @pytest.mark.asyncio
    async def test_does_not_clean_up_a_step_still_in_flight(self) -> None:
        """Keep an unfinished dispatch's script; there is nothing to clean up yet."""
        run = self._run(
            [
                StepRecord(
                    name="install_package",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ]
        )

        with patch(
            "app.extensions.apps.om_bootstrap.reconcile.cleanup_step_script"
        ) as cleanup:
            await reconcile.reconcile_run(_tasks_api("running"), run)

        cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconciles_run_level_steps_too(self) -> None:
        """Record a run-level dispatch's outcome in run.run_steps, not just hosts."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="rs_initiate",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ],
        )

        changed = await reconcile.reconcile_run(_tasks_api("success"), run)

        assert changed is True
        assert parse_run_steps(run)[0].status == StepStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_cleans_up_a_run_level_step_under_the_seed_host(self) -> None:
        """Name a run-level step's scratch script under the run's first host."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="rs_initiate",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ],
        )
        run.id = uuid4()

        with patch(
            "app.extensions.apps.om_bootstrap.reconcile.cleanup_step_script"
        ) as cleanup:
            await reconcile.reconcile_run(_tasks_api("success"), run)

        cleanup.assert_called_once_with(str(run.id), "node00", "rs_initiate")

    @pytest.mark.asyncio
    async def test_reconciles_rollback_steps_too(self) -> None:
        """Record a rollback dispatch's outcome in the host's rollback_steps."""
        run = self._run(
            [StepRecord(name="install_package", status=StepStatus.FAILED)],
            rollback_steps=[
                StepRecord(
                    name="stop_service",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ],
        )

        changed = await reconcile.reconcile_run(_tasks_api("success"), run)

        assert changed is True
        assert parse_host_states(run)[0].rollback_steps[0].status == (
            StepStatus.SUCCEEDED
        )

    @pytest.mark.asyncio
    async def test_marks_a_fully_succeeded_run_succeeded(self) -> None:
        """Mark the run succeeded once every host and run-level step succeeds."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="create_pmm_monitoring_user",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ],
        )
        assert run.status == BootstrapRunStatus.RUNNING

        changed = await reconcile.reconcile_run(_tasks_api("success"), run)

        assert changed is True
        assert run.status == BootstrapRunStatus.SUCCEEDED
        assert run.finished_at is not None

    @pytest.mark.asyncio
    async def test_does_not_mark_succeeded_while_a_host_step_is_pending(self) -> None:
        """Keep a run in flight even when its run-level steps finished first."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.PENDING)],
            run_steps=[StepRecord(name="rs_initiate", status=StepStatus.SUCCEEDED)],
        )

        await reconcile.reconcile_run(AsyncMock(), run)

        assert run.status == BootstrapRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_does_not_override_an_already_terminal_status(self) -> None:
        """Leave a run the stepper already marked FAILED/ROLLED_BACK alone."""
        run = self._run([StepRecord(name="verify", status=StepStatus.SUCCEEDED)])
        run.status = BootstrapRunStatus.FAILED

        await reconcile.reconcile_run(AsyncMock(), run)

        assert run.status == BootstrapRunStatus.FAILED

    @pytest.mark.asyncio
    async def test_pending_rollback_steps_do_not_block_success(self) -> None:
        """Ignore a never-triggered rollback list (all PENDING) when judging success."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            rollback_steps=[StepRecord(name="stop_service")],
        )

        await reconcile.reconcile_run(AsyncMock(), run)

        assert run.status == BootstrapRunStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_reconciles_finalize_steps_too(self) -> None:
        """Record a finalize dispatch's outcome in the host's finalize_steps."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="create_pmm_monitoring_user", status=StepStatus.SUCCEEDED
                )
            ],
            finalize_steps=[
                StepRecord(
                    name="enable_auth",
                    status=StepStatus.RUNNING,
                    task_history_id=TASK_HISTORY_ID,
                )
            ],
        )

        changed = await reconcile.reconcile_run(_tasks_api("success"), run)

        assert changed is True
        assert parse_host_states(run)[0].finalize_steps[0].status == (
            StepStatus.SUCCEEDED
        )

    @pytest.mark.asyncio
    async def test_pending_finalize_steps_block_success(self) -> None:
        """Keep a run running while a host still has an undispatched finalize step.

        Distinct from rollback_steps, which stay pending forever on a run that
        never needed rollback: every run needs its finalize steps to actually
        run, so an all-PENDING finalize list must NOT read as vacuously done the
        way an all-PENDING rollback list correctly does.
        """
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="create_pmm_monitoring_user", status=StepStatus.SUCCEEDED
                )
            ],
            finalize_steps=[StepRecord(name="enable_auth")],
        )

        await reconcile.reconcile_run(AsyncMock(), run)

        assert run.status == BootstrapRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_marks_succeeded_once_finalize_steps_succeed(self) -> None:
        """Finish the run only once its finalize steps succeed too."""
        run = self._run(
            [StepRecord(name="verify", status=StepStatus.SUCCEEDED)],
            run_steps=[
                StepRecord(
                    name="create_pmm_monitoring_user", status=StepStatus.SUCCEEDED
                )
            ],
            finalize_steps=[
                StepRecord(name="enable_auth", status=StepStatus.SUCCEEDED)
            ],
        )

        await reconcile.reconcile_run(AsyncMock(), run)

        assert run.status == BootstrapRunStatus.SUCCEEDED
