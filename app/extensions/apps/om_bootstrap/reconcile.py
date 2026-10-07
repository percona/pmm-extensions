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

"""Reconcile dispatched steps against their Nomad dispatch's real status.

Deliberately narrow: this module only ever *translates* a ``TaskHistory``'s
status onto the :class:`~app.extensions.apps.om_bootstrap.strategy.StepRecord` that
dispatched it — it never decides to dispatch the *next* step, retry a failed
one, or roll a run back. Those are PMM's ``om`` service's job, driving the state
machine as the HA-leader-only stepper: it reads a run's current state through
the API and decides what happens next.
``om_bootstrap`` only ever answers "is this step still running", mechanically,
so PMM's driver has something true to read.

One exception, not a contradiction of the above: :func:`reconcile_run` also
flips a ``RUNNING`` run to
:attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.SUCCEEDED` once
every host and every run-level step has actually succeeded. That is not a
decision — there is nothing left to decide once everything succeeded, only a
fact to record — unlike
:attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.FAILED` (retries
exhausted) and
:attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.ROLLED_BACK`
(rollback finished), both real calls only the stepper makes, which reach
``run.status`` through ``api_routes.py``'s explicit ``:finish`` route instead.

``finished_at`` is stamped with the reconciliation's own clock, not read back
from ``TaskHistory``: this runs on a polling interval, not a push, so the two
times differ by at most that interval — an acceptable approximation for a
receipt, not a claim of exact timing.

``detail`` on a non-``SUCCESS`` terminal status says why, in the node's own
words: the ``TaskHistory``'s ``failure_reason`` and the end of the failed step's
output, read the same way ``om_inventory`` reads a failed scan's
(:mod:`~app.extensions.apps.shared.om.task_failure`). The status alone said
"ended failed" for a full disk, an unreachable repository and a taken port alike,
and PMM shows ``detail`` to the operator as it is. Reading the logs is bounded by
:data:`LOG_READ_TIMEOUT_S` and best-effort: a step whose logs cannot be read still
fails, with the reason alone, rather than staying ``running`` until they can.

A Tasks API read that fails does not fail the reconcile: a ``404``/``410`` means
the step's ``TaskHistory`` record is gone and it can never finish, so the step
is recorded ``failed``; any other failure (an upstream error, a transport error,
a timeout) is transient, so the step stays ``running`` and the next poll tries
again. A readable answer that is not a ``TaskHistory`` — not a JSON object, or
without a string ``status`` — is a ``502``, as it would otherwise leave the step
looking in flight forever.
"""

import asyncio
import logging
import re

import aiohttp
from fastapi import HTTPException

from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPGoneException,
    HTTPNotFoundException,
)
from app.core.requests import as_json_object, RemoteAPI
from app.core.utils.date_time import utc_now
from app.extensions.apps.om_bootstrap.dispatch import cleanup_step_script
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
from app.extensions.apps.shared.om.task_failure import (
    describe_task_failure,
    read_step_logs,
)
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.models import TaskHistoryStatusEnum

__all__ = ["reconcile_run", "reconcile_step"]

logger = logging.getLogger(__name__)

#: Statuses that mean "nothing more to do here" for the purpose of deciding a
#: run is fully done — a skipped step is as final as a succeeded one.
_DONE_STATUSES = frozenset({StepStatus.SUCCEEDED, StepStatus.SKIPPED})

#: ``TaskHistory`` status values that mean "still in flight" — everything else
#: is terminal.
_IN_FLIGHT_STATUS_VALUES = frozenset(
    status.value for status in TaskHistoryStatusEnum.active_statuses()
)

#: How long reading a failed step's logs may take before the step is failed with
#: its ``failure_reason`` alone. Reconciling runs inside ``GET /runs/{id}``, under
#: the run's row lock, and PMM gives that whole request 15 seconds
#: (``bootstrapRequestTimeout`` in ``managed/services/om/bootstrap_client.go``).
LOG_READ_TIMEOUT_S = 5.0

#: The reason the tasks service writes when ``exec-artifact``'s ``run-script``
#: exits 123. That step launches the script through GNU ``xargs`` (see
#: ``NOMAD_EXEC_ARTIFACT`` in ``app/tasks/db/seed.py``), and ``xargs`` exits 123
#: whenever the command it ran exited with any status from 1 to 125 - so 123 is
#: ``xargs``' code, not the step's, and stating it as the step's exit code sends
#: the reader looking for a meaning it does not have. The other codes ``xargs``
#: can exit with (124 for a 255, 125 for a signal, 126 and 127 when it could not
#: run the command) say something real and are left as they are.
_XARGS_ANY_FAILURE = re.compile(
    rf"^Step '{re.escape(NomadStep.RUN_SCRIPT)}' failed \(exit code 123\)\.$"
)


async def reconcile_step(tasks_api: RemoteAPI, step: StepRecord) -> StepRecord:
    """Check one step's dispatch and translate it if it reached a terminal status.

    A no-op, returning ``step`` unchanged, for a step that is not
    :attr:`~app.extensions.apps.om_bootstrap.strategy.StepStatus.RUNNING`, carries no
    :attr:`~app.extensions.apps.om_bootstrap.strategy.StepRecord.task_history_id` (not
    yet dispatched), or whose dispatch is still in flight.

    Also returns ``step`` unchanged when the Tasks API cannot be read right
    now; see the module docstring for which failures count as that.

    :param tasks_api: The Tasks API client.
    :param step: The step to check.
    :return: ``step`` itself when nothing changed, or a new
        :class:`~app.extensions.apps.om_bootstrap.strategy.StepRecord` reflecting the
        dispatch's terminal status, or ``failed`` when its ``TaskHistory``
        record no longer exists.
    :raises HTTPBadGatewayException: When the Tasks API answers with something
        that is not a ``TaskHistory`` — not a JSON object, or without a string
        ``status``.
    """
    if step.status != StepStatus.RUNNING or step.task_history_id is None:
        return step
    try:
        payload = await tasks_api.get(f"/history/{step.task_history_id}")
    except (HTTPNotFoundException, HTTPGoneException):
        return step.model_copy(
            update={
                "status": StepStatus.FAILED,
                "finished_at": utc_now(),
                "detail": f"Task history {step.task_history_id} no longer exists; "
                "the dispatch's outcome is unknown",
            }
        )
    except (HTTPException, aiohttp.ClientError, OSError):
        logger.warning(
            "Could not read task history %s for step %r; leaving it running",
            step.task_history_id,
            step.name,
            exc_info=True,
        )
        return step
    task_status = as_json_object(payload).get("status")
    if not isinstance(task_status, str):
        raise HTTPBadGatewayException(
            detail=f"Task history {step.task_history_id} has no status"
        )
    if task_status in _IN_FLIGHT_STATUS_VALUES:
        return step
    if task_status == TaskHistoryStatusEnum.SUCCESS.value:
        return step.model_copy(
            update={"status": StepStatus.SUCCEEDED, "finished_at": utc_now()}
        )
    failure_reason = as_json_object(payload).get("failure_reason")
    detail = await _failure_detail(
        tasks_api,
        step.task_history_id,
        task_status,
        failure_reason if isinstance(failure_reason, str) else None,
    )
    return step.model_copy(
        update={"status": StepStatus.FAILED, "finished_at": utc_now(), "detail": detail}
    )


async def _failure_detail(
    tasks_api: RemoteAPI,
    task_history_id: int,
    task_status: str,
    failure_reason: str | None,
) -> str:
    """Say why a step's dispatch failed, naming the task history it ran as.

    The tasks service's reason first, then the end of the failed step's output,
    which is where a step body says what it refused or what broke - ``pre_check``
    names the check that failed, ``dnf`` prints the repository it could not reach.
    The task history id stays in the text, last, so the full logs can still be
    found from it.

    :param tasks_api: The Tasks API client.
    :param task_history_id: The step's dispatch.
    :param task_status: Its terminal, non-success status.
    :param failure_reason: The tasks service's account of it, when it has one.
    :return: The detail.
    """
    try:
        logs = await asyncio.wait_for(
            read_step_logs(tasks_api, task_history_id), LOG_READ_TIMEOUT_S
        )
    except (HTTPException, aiohttp.ClientError, OSError):
        # The reason alone is still better than leaving the step running: logs that
        # cannot be read now (gone from the executor, or slow) may never be. The
        # timeout lands here too, since TimeoutError is an OSError.
        logger.warning(
            "Could not read the logs of task history %s; reporting its reason alone",
            task_history_id,
            exc_info=True,
        )
        logs = {}
    if failure_reason is not None and _XARGS_ANY_FAILURE.match(failure_reason):
        failure_reason = f"Step '{NomadStep.RUN_SCRIPT}' failed."
    described = describe_task_failure(
        failure_reason, logs, default_step=NomadStep.RUN_SCRIPT
    )
    described = described or f"Ended {task_status} with no output"
    return f"{described} (task history {task_history_id})"


async def reconcile_run(tasks_api: RemoteAPI, run: BootstrapRun) -> bool:
    """Reconcile every running step across ``run`` — per-host, rollback, and run-level.

    Mutates ``run.hosts``/``run.run_steps``/``run.status``/``run.finished_at``
    directly when anything changed; the caller is responsible for committing the
    session. A finished step's scratch script
    (:func:`~app.extensions.apps.om_bootstrap.dispatch.cleanup_step_script`) is removed
    as part of reconciling it, win or lose — nothing downstream needs it once
    the dispatch that read it is done. The run-level steps' scripts are named
    under the run's first host regardless of which host actually ran them —
    see :func:`~app.extensions.apps.om_bootstrap.dispatch.step_script_filename`, and
    :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.build_run_step`'s
    own docstring for why that host is always the target.

    Skips the SUCCEEDED inference once ``run.cancel_requested`` is set: without
    this, a cancel that lands just as the last step finishes — or whose
    best-effort stop failed — would flip to SUCCEEDED on the very next poll,
    permanently recording a run the operator aborted as one that finished
    normally, with no route back to ROLLED_BACK (``finish_run`` refuses to
    override a terminal run).

    :param tasks_api: The Tasks API client.
    :param run: The run to reconcile.
    :return: Whether anything changed — callers use this to skip a write when
        every step was still in flight.
    """
    states = parse_host_states(run)
    run_steps = parse_run_steps(run)
    seed_host = states[0].host if states else None
    changed, run_steps_changed = await asyncio.gather(
        _reconcile_step_list_per_host(tasks_api, run, states),
        _reconcile_step_list(tasks_api, run, seed_host, run_steps),
    )
    if changed:
        run.hosts = dump_host_states(states)
    if run_steps_changed:
        run.run_steps = dump_run_steps(run_steps)

    if (
        run.status == BootstrapRunStatus.RUNNING
        and not run.cancel_requested
        and _fully_succeeded(states, run_steps)
    ):
        run.status = BootstrapRunStatus.SUCCEEDED
        run.finished_at = utc_now()
        changed = True

    return changed or run_steps_changed


async def _reconcile_step_list_per_host(
    tasks_api: RemoteAPI, run: BootstrapRun, states: list[HostBootstrapState]
) -> bool:
    """Reconcile every host's own ``steps`` and ``rollback_steps``, in place.

    :param tasks_api: The Tasks API client.
    :param run: The run these hosts belong to — only read, for its id.
    :param states: The parsed host states to reconcile, mutated in place.
    :return: Whether anything changed.
    """
    results = await asyncio.gather(
        *(
            _reconcile_step_list(tasks_api, run, state.host, step_list)
            for state in states
            for step_list in (state.steps, state.rollback_steps, state.finalize_steps)
        )
    )
    return any(results)


async def _reconcile_step_list(
    tasks_api: RemoteAPI,
    run: BootstrapRun,
    host: str | None,
    steps: list[StepRecord],
) -> bool:
    """Reconcile one flat list of steps against their dispatches, in place.

    Shared by every step list this module reconciles — a host's forward steps,
    its rollback steps, and the run's own run-level steps — since all three are
    the same shape and need the exact same treatment.

    :param tasks_api: The Tasks API client.
    :param run: The run these steps belong to — only read, for its id.
    :param host: The host whose scratch script directory a transitioned step's
        cleanup targets. ``None`` when there is no host to target (an empty
        run — see :func:`reconcile_run`'s ``seed_host``), in which case cleanup
        is skipped rather than guessing a name.
    :param steps: The steps to reconcile, mutated in place.
    :return: Whether anything changed.
    """
    reconciled_steps = await asyncio.gather(
        *(reconcile_step(tasks_api, step) for step in steps)
    )
    changed = False
    for index, (step, reconciled) in enumerate(
        zip(steps, reconciled_steps, strict=True)
    ):
        if reconciled is step:
            continue
        steps[index] = reconciled
        changed = True
        if host is not None:
            cleanup_step_script(str(run.id), host, step.name)
    return changed


def _fully_succeeded(
    states: list[HostBootstrapState], run_steps: list[StepRecord]
) -> bool:
    """Report whether every host, every run-level step, and every finalize step succeeded.

    Rollback steps are deliberately excluded from this check, not reconciled
    into it: every host's ``rollback_steps`` are planned up front alongside its
    forward steps (see
    :class:`~app.extensions.apps.om_bootstrap.strategy.HostBootstrapState`'s own
    docstring) and stay
    :attr:`~app.extensions.apps.om_bootstrap.strategy.StepStatus.PENDING` for the
    entire life of a run that never needed rollback — counting them here
    would mean a normal, fully-succeeded run could never satisfy this check.

    ``finalize_steps`` are checked explicitly, not folded into
    :attr:`~app.extensions.apps.om_bootstrap.strategy.HostBootstrapState.status`: that
    property derives purely from ``steps`` (see its own docstring), by design —
    a host isn't considered done finalizing until its finalize steps have too,
    but a host that hasn't started finalizing yet (every finalize step still
    ``pending``, correctly, until every run-level step succeeds) must not read as
    unfinished in the same way a genuinely stuck forward step would.

    :param states: Every host's current state.
    :param run_steps: The run's current run-level steps.
    :return: Whether the run, as a whole, has nothing left to do but succeed.
    """
    if not all(state.status == StepStatus.SUCCEEDED for state in states):
        return False
    if not all(step.status in _DONE_STATUSES for step in run_steps):
        return False
    return all(
        step.status in _DONE_STATUSES
        for state in states
        for step in state.finalize_steps
    )
