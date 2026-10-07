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

"""Serve bootstrap runs: create one, dispatch its steps, read its progress.

PMM's ``om`` service is the only intended caller: it drives the state machine
— deciding when to dispatch which step, when to retry, when to give up and
roll back — as the HA-leader-only stepper, reading
this router to know what happened and calling it to make something happen
next. This router itself decides none of that; it only ever does exactly what
it is asked; see ``reconcile.py``'s own docstring for the same boundary stated
from the other side.

Auth is applied at the mount level: ``/api/apps`` carries the
``IsApiAuthenticated`` router guard, and unsafe methods additionally require
the rank :func:`app.api.deps.require_minimum_role` registers per route.
Triggering a run or a step is root execution on a database host — the
decided gate is admin-only, so every mutating route registers
:attr:`~app.core.auth.models.UserRole.ADMIN` explicitly.

Every route that writes a run reads it with a row lock
(:data:`~app.extensions.apps.om_bootstrap.deps.LockedRun`) and commits in the same
transaction, so two concurrent requests against one run serialise rather than
the later save silently overwriting the earlier one's ``hosts``/``run_steps``
document. A ``:dispatch`` route holds that lock across its one Tasks API call on
purpose: a second dispatch of the same step waits, then sees it ``running`` and
gets a 409, instead of dispatching it twice.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import require_minimum_role
from app.core.auth.models import UserRole
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPNotFoundException,
)
from app.core.requests import RemoteAPI
from app.core.security import get_internal_token
from app.core.utils.date_time import utc_now
from app.core.utils.fields import UTCDatetime
from app.extensions.apps.framework.api import schema_endpoint
from app.extensions.apps.om_bootstrap.crud import BootstrapRunManager
from app.extensions.apps.om_bootstrap.deps import LockedRun
from app.extensions.apps.om_bootstrap.dispatch import cleanup_run_scripts, dispatch_step
from app.extensions.apps.om_bootstrap.models import BootstrapRun, BootstrapRunStatus
from app.extensions.apps.om_bootstrap.persistence import (
    dump_host_states,
    dump_run_steps,
    parse_host_states,
    parse_run_steps,
    to_models_install_method,
    to_models_os,
    to_strategy_install_method,
    to_strategy_os,
)
from app.extensions.apps.om_bootstrap.reconcile import reconcile_run
from app.extensions.apps.om_bootstrap.schema import om_bootstrap_schema
from app.extensions.apps.om_bootstrap.strategies import strategy_for
from app.extensions.apps.om_bootstrap.strategy import (
    BootstrapSpec,
    HostBootstrapState,
    InstallMethod,
    InstallStrategy,
    MemberConfig,
    OperatingSystem,
    StepAction,
    StepRecord,
    StepStatus,
)
from app.extensions.deps import SessionDep, TasksClient

logger = logging.getLogger(__name__)

#: BootstrapRunStatus members :func:`finish_run` accepts — a caller declaring the
#: run itself decided to give up. SUCCEEDED is deliberately excluded: reconciling
#: a run to SUCCEEDED is a fact
#: :func:`~app.extensions.apps.om_bootstrap.reconcile.reconcile_run` infers on its own (see its own docstring), never something a caller requests.
_FINISHABLE_STATUSES = frozenset(
    {BootstrapRunStatus.FAILED, BootstrapRunStatus.ROLLED_BACK}
)

#: Step statuses a ``:dispatch`` route accepts: a step never dispatched, or one
#: whose last attempt failed (a retry). A running step would be dispatched
#: twice; a succeeded or skipped one would re-run a step already done.
_DISPATCHABLE_STATUSES = frozenset({StepStatus.PENDING, StepStatus.FAILED})

#: Replica-set sizes a run accepts — the decided phase-1 topologies.
_ALLOWED_HOST_COUNTS = frozenset({1, 3})

#: A node name as Nomad and the executor know it. Excludes anything that could
#: act as a path separator or shell metacharacter in the step-script filename
#: and the dispatch target.
HostName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
]

#: At least two path segments past the root, so a value like ``/var`` or ``/etc``
#: (one segment) is rejected outright — ``_rollback_remove_data`` runs
#: ``rm -rf`` on ``data_path`` (PMM-15347/strategies/packages.py), and a
#: single-segment path is a typo away from an existing, load-bearing directory.
#: No whitespace/NUL either, so the value is safe to interpolate into a shell
#: command once :func:`shlex.quote`-d — this bounds the *shape*, quoting closes
#: the injection vector itself.
_ABSOLUTE_PATH_PATTERN = r"^(?:/[^/\s\x00]+){2,}$"

#: No whitespace or other control characters. ``replica_set_name`` and
#: ``bind_ip`` both land in ``mongod.conf`` via a quoted heredoc
#: (``_mongod_config``, strategies/packages.py) — inert against the *shell*,
#: since the heredoc delimiter is quoted, but a newline in either value would
#: still inject an arbitrary extra line into the YAML mongod parses.
_NO_CONTROL_CHARS_PATTERN = r"^[^\s\x00-\x1f]+$"


class TriggerRunRequest(BaseModel):
    """Request one bootstrap run over a set of hosts, all sharing one spec.

    :param hosts: The hosts to provision — one-member or three-member replica
        sets only, the decided phase-1 scope. Each is a node name: letters,
        digits, ``.``, ``_`` and ``-``, starting with a letter or digit.
    :param install_method: Which strategy provisions every host in this run.
    :param os: Every host's OS. Mixed-OS replica sets are out of phase-1 scope.
    :param mongodb_version: The Percona Server for MongoDB version to install,
        as ``major.minor`` or ``major.minor.patch`` (e.g. ``"8.0"``, ``"8.0.4"``).
    :param replica_set_name: The replica set every host joins: 1-64 letters,
        digits, ``_`` or ``-``.
    :param data_path: Where mongod stores its data on every host. Defaults to
        the same value the column behind it carries
        (``migrations/versions/..._add_run_config_fields.py``), so a caller
        that omits it gets the path the fixed-path contract used.
    :param log_path: Where mongod writes its log file on every host. Same
        default story as ``data_path``.
    :param port: The port mongod listens on, on every host. Same default
        story as ``data_path``.
    :param bind_ip: The interface(s) mongod listens on, on every host.
        Defaults to ``127.0.0.1``, keeping mongod's pre-auth window local to
        the host unless the caller passes a wider address.
    :param member_configs: Per-host election settings for ``rs.initiate``,
        keyed by entries of ``hosts``. A host missing from this mapping —
        including every host, when this is left empty — gets
        :class:`~app.extensions.apps.om_bootstrap.strategy.MemberConfig`'s own
        defaults.
    """

    hosts: list[HostName]
    install_method: InstallMethod
    os: OperatingSystem
    mongodb_version: Annotated[str, Field(pattern=r"^\d+\.\d+(\.\d+)?$")]
    replica_set_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    data_path: str = Field(default="/var/lib/mongo", pattern=_ABSOLUTE_PATH_PATTERN)
    log_path: str = Field(
        default="/var/log/mongodb/mongod.log", pattern=_ABSOLUTE_PATH_PATTERN
    )
    port: int = Field(default=27017, gt=0, le=65535)
    bind_ip: str = Field(default="127.0.0.1", pattern=_NO_CONTROL_CHARS_PATTERN)
    member_configs: dict[str, MemberConfig] = {}


class DispatchStepRequest(BaseModel):
    """Carry the optional body of any ``:dispatch`` route.

    :param params: Per-dispatch values the step being dispatched needs but
        cannot compute itself — a keyFile's content, a generated
        monitoring-user password. See
        :class:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy`'s own
        docstring for why these are never persisted by ``om_bootstrap``: PMM's
        stepper holds their durable, encrypted copy and hands one to a single
        dispatch, transiently, through this field.
        Empty for a step that needs none.
    """

    params: dict[str, str] = {}


class FinishRunRequest(BaseModel):
    """Carry the body of :func:`finish_run`, the stepper recording its own decision.

    :param status: The run's new terminal status. Must be one of
        :data:`_FINISHABLE_STATUSES`;
        :attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.SUCCEEDED`
        is never requested here (see :data:`_FINISHABLE_STATUSES`'s own
        docstring).
    :param error: A human-readable reason, if any — stored on
        :attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRun.error`.
    """

    status: BootstrapRunStatus
    error: str | None = None


class RunResponse(BaseModel):
    """Describe one bootstrap run in full.

    :param id: The run's id.
    :param status: The run's lifecycle state.
    :param install_method: The run's install method.
    :param os: The run's target OS.
    :param mongodb_version: The run's MongoDB version.
    :param replica_set_name: The replica set every host in this run joins.
    :param data_path: Where mongod stores its data on every host in this run.
    :param log_path: Where mongod writes its log file on every host in this run.
    :param port: The port mongod listens on, on every host in this run.
    :param bind_ip: The interface(s) mongod listens on, on every host in this run.
    :param member_configs: Per-host election settings this run was created
        with — see :class:`TriggerRunRequest`'s own docstring.
    :param started_at: When the run began.
    :param finished_at: When it reached a terminal status, if it has.
    :param hosts: Every host's current step-by-step progress — the full,
        run-specific step list each host was planned with (forward steps and
        rollback steps both), not just the steps that have started.
    :param run_steps: This run's run-level steps
        (:meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.plan_run_steps`),
        planned up front the same way ``hosts``' steps are.
    :param error: The run-level failure detail, when the run itself raised
        outside any single host's steps.
    :param cancel_requested: Whether an operator has asked this run to stop —
        see :func:`cancel_run`. PMM's stepper treats this the same as a step
        exhausting its retries (force every host's rollback), never something
        ``om_bootstrap`` itself acts on.
    """

    id: UUID
    status: BootstrapRunStatus
    install_method: InstallMethod
    os: OperatingSystem
    mongodb_version: str
    replica_set_name: str
    data_path: str
    log_path: str
    port: int
    bind_ip: str
    member_configs: dict[str, MemberConfig]
    started_at: UTCDatetime
    finished_at: UTCDatetime | None
    hosts: list[HostBootstrapState]
    run_steps: list[StepRecord]
    error: str | None
    cancel_requested: bool


def _run_response(
    run: BootstrapRun,
    *,
    hosts: list[HostBootstrapState] | None = None,
    run_steps: list[StepRecord] | None = None,
) -> RunResponse:
    """Build the response DTO from a persisted run.

    :param run: The run to serialize.
    :param hosts: The run's host states, when the caller already parsed them
        (e.g. it just built and dumped them) — avoids re-parsing the JSON it
        just dumped from the very same typed value. Parsed from ``run.hosts``
        when omitted.
    :param run_steps: See ``hosts`` — the run-level counterpart.
    :return: The run's full state.
    """
    return RunResponse(
        id=run.id,
        status=run.status,
        install_method=to_strategy_install_method(run.install_method),
        os=to_strategy_os(run.os),
        mongodb_version=run.mongodb_version,
        replica_set_name=run.replica_set_name,
        data_path=run.data_path,
        log_path=run.log_path,
        port=run.port,
        bind_ip=run.bind_ip,
        member_configs={
            host: MemberConfig(**config) for host, config in run.member_configs.items()
        },
        started_at=run.started_at,
        finished_at=run.finished_at,
        hosts=hosts if hosts is not None else parse_host_states(run),
        run_steps=run_steps if run_steps is not None else parse_run_steps(run),
        error=run.error,
        cancel_requested=run.cancel_requested,
    )


def _find_step(steps: list[StepRecord], step_name: str, *, what: str) -> int:
    """Return the index of a dispatchable ``step_name`` in ``steps``.

    Shared by every ``:dispatch`` route below — a host's forward steps, its
    rollback steps, and a run's own run-level steps are all "find this name in
    a flat StepRecord list" the exact same way.

    :param steps: The steps to search.
    :param step_name: The name to find.
    :param what: A human-readable description of what was searched, for the
        404 detail (e.g. ``"host 'node00'"``).
    :raises HTTPNotFoundException: When no step in ``steps`` is named ``step_name``.
    :raises HTTPConflictException: When that step cannot be dispatched now (see
        :func:`_require_dispatchable`).
    :return: The index of the matching step.
    """
    index = next((i for i, step in enumerate(steps) if step.name == step_name), None)
    if index is None:
        raise HTTPNotFoundException(
            detail=f"Step {step_name!r} is not planned for {what}"
        )
    _require_dispatchable(steps[index], step_name, what=what)
    return index


def _require_dispatchable(step: StepRecord, step_name: str, *, what: str) -> None:
    """Raise 409 unless ``step`` is ``pending`` or ``failed``.

    :param step: The step to check.
    :param step_name: Its name, for the conflict detail.
    :param what: See :func:`_find_step`.
    :raises HTTPConflictException: When ``step.status`` is not in
        :data:`_DISPATCHABLE_STATUSES` — already running, succeeded, or skipped.
    """
    if step.status not in _DISPATCHABLE_STATUSES:
        raise HTTPConflictException(
            detail=f"Step {step_name!r} for {what} is already {step.status.value}"
        )


def _host_state(
    states: list[HostBootstrapState], host: str, run: BootstrapRun
) -> HostBootstrapState:
    """Return ``host``'s state from ``states``, or reject a host outside the run.

    :param states: The run's parsed host states.
    :param host: The host named in the route's path.
    :param run: The run, for the error detail.
    :raises HTTPNotFoundException: When ``host`` is not part of ``run``.
    :return: The host's state, the same object held in ``states``.
    """
    host_state = next((state for state in states if state.host == host), None)
    if host_state is None:
        raise HTTPNotFoundException(detail=f"Host {host!r} is not part of run {run.id}")
    return host_state


def _body_params(body: DispatchStepRequest | None) -> dict[str, str] | None:
    """Return a ``:dispatch`` body's ``params``, or ``None`` when no body was sent.

    :param body: The route's optional request body.
    :return: The body's params, or ``None``.
    """
    return body.params if body is not None else None


async def _save_host_states(
    session: AsyncSession, run: BootstrapRun, states: list[HostBootstrapState]
) -> RunResponse:
    """Write ``states`` back onto ``run``, save it, and build the response.

    :param session: The database session.
    :param run: The run the states belong to.
    :param states: The run's host states, as the route just updated them.
    :return: The saved run's full state.
    """
    run.hosts = dump_host_states(states)
    run = await BootstrapRunManager.save(session, run)
    return _run_response(run, hosts=states)


async def _dispatch_and_record(
    tasks_api: RemoteAPI,
    request: Request,
    run: BootstrapRun,
    target_host: str,
    action: StepAction,
    step: StepRecord,
) -> StepRecord:
    """Dispatch ``action`` and return ``step`` updated to reflect it.

    Shared mechanics for every ``:dispatch`` route: only the lookup (see
    :func:`_find_step`) and the action-building differ between a per-host step,
    a rollback step, and a run-level step. The dispatch authenticates with
    PMM Extensions' internal token rather than the caller's.

    :param tasks_api: The Tasks API client.
    :param request: The current request; see
        :func:`~app.extensions.apps.om_bootstrap.dispatch.dispatch_step`.
    :param run: The bootstrap run this step belongs to.
    :param target_host: The node name the dispatch actually runs on.
    :param action: The step's built action.
    :param step: The step's current record, about to be dispatched.
    :return: A new :class:`~app.extensions.apps.om_bootstrap.strategy.StepRecord`.
        ``RUNNING`` when the Tasks API accepted the dispatch, with
        :attr:`~app.extensions.apps.om_bootstrap.strategy.StepRecord.attempt_count`
        incremented either way — PMM's stepper reads this to enforce its
        retry-then-rollback policy. ``FAILED``, also with ``attempt_count``
        incremented and ``detail`` set, when the Tasks API itself rejected the
        dispatch (:class:`~fastapi.HTTPException`, e.g. an unknown or
        unreachable executor target), could not be reached at all
        (:class:`aiohttp.ClientError`, or an :class:`OSError` such as a
        timeout), accepted it without returning a history id
        (:class:`RuntimeError`), or the step's script could not be written
        locally (:class:`OSError`) — a dispatch that never
        starts is as real an outcome as one that starts and later fails, and
        recording it here is what lets the stepper's retry-then-rollback policy
        see it at all. Without this, such a step stays ``PENDING`` forever:
        nothing ever transitions it, so every tick looks like the very first
        attempt, and the stepper retries indefinitely with no failure ever
        reaching a caller.
    """
    try:
        with tasks_api.auth(get_internal_token()):
            task_history_id = await dispatch_step(
                tasks_api, request, str(run.id), target_host, step.name, action
            )
    except (HTTPException, RuntimeError, aiohttp.ClientError, OSError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc) or repr(exc)
        return step.model_copy(
            update={
                "status": StepStatus.FAILED,
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "detail": f"Failed to dispatch: {detail}",
                "task_history_id": None,
                "attempt_count": step.attempt_count + 1,
            }
        )
    return step.model_copy(
        update={
            "status": StepStatus.RUNNING,
            "started_at": utc_now(),
            "finished_at": None,
            "detail": None,
            "task_history_id": task_history_id,
            "attempt_count": step.attempt_count + 1,
        }
    )


router = APIRouter()
schema_endpoint(router=router, plugin_schema=om_bootstrap_schema)


@router.post("/runs", status_code=http_status.HTTP_201_CREATED)
@require_minimum_role(UserRole.ADMIN)
async def trigger_run(session: SessionDep, request: TriggerRunRequest) -> RunResponse:
    """Create a bootstrap run, planning every host's steps up front.

    Dispatches nothing: creating a run only plans it, so the caller (PMM's
    driver) sees the full step list for every host before anything is touched.
    Actually starting a host's first step is a separate call to
    :func:`dispatch_run_step`.

    :param session: The database session.
    :param request: The requested run.
    :raises HTTPBadRequestException: When ``request.hosts`` is empty, lists the
        same host twice, has neither one nor three hosts, names an install
        method with no registered strategy, ``member_configs`` names a host
        outside ``hosts``, or ``member_configs`` leaves no host that both votes
        and has a nonzero priority, since ``rs.initiate`` rejects a config with
        no electable member.
    :return: The created run, every host's steps ``pending``.
    """
    if not request.hosts:
        raise HTTPBadRequestException(detail="At least one host is required")
    if len(set(request.hosts)) != len(request.hosts):
        raise HTTPBadRequestException(detail="hosts must not repeat the same host")
    if len(request.hosts) not in _ALLOWED_HOST_COUNTS:
        raise HTTPBadRequestException(
            detail="A replica set must have exactly 1 or 3 hosts, "
            f"not {len(request.hosts)}"
        )
    unknown_members = set(request.member_configs) - set(request.hosts)
    if unknown_members:
        raise HTTPBadRequestException(
            detail=f"member_configs names hosts not in hosts: {sorted(unknown_members)}"
        )
    effective_configs = [
        request.member_configs.get(host, MemberConfig()) for host in request.hosts
    ]
    if not any(config.votes and config.priority > 0 for config in effective_configs):
        raise HTTPBadRequestException(
            detail="member_configs must leave at least one host that votes "
            "with a nonzero priority"
        )

    spec = BootstrapSpec(
        install_method=request.install_method,
        os=request.os,
        mongodb_version=request.mongodb_version,
        replica_set_name=request.replica_set_name,
        data_path=request.data_path,
        log_path=request.log_path,
        port=request.port,
        bind_ip=request.bind_ip,
        member_configs=request.member_configs,
    )
    strategy = _strategy_for(request.install_method)
    host_states = [
        HostBootstrapState(
            host=host,
            steps=[StepRecord(name=name) for name in strategy.plan_steps(spec)],
            rollback_steps=[
                StepRecord(name=name) for name in strategy.plan_rollback_steps(spec)
            ],
            finalize_steps=[
                StepRecord(name=name) for name in strategy.plan_finalize_steps(spec)
            ],
        )
        for host in request.hosts
    ]
    run_steps = [StepRecord(name=name) for name in strategy.plan_run_steps(spec)]
    run = await BootstrapRunManager.save(
        session,
        BootstrapRun(
            install_method=to_models_install_method(request.install_method),
            os=to_models_os(request.os),
            mongodb_version=request.mongodb_version,
            replica_set_name=request.replica_set_name,
            data_path=request.data_path,
            log_path=request.log_path,
            port=request.port,
            bind_ip=request.bind_ip,
            member_configs={
                host: config.model_dump()
                for host, config in request.member_configs.items()
            },
            hosts=dump_host_states(host_states),
            run_steps=dump_run_steps(run_steps),
        ),
    )
    return _run_response(run, hosts=host_states, run_steps=run_steps)


@router.get("/runs")
async def list_bootstrap_runs(
    session: SessionDep,
    status: BootstrapRunStatus | None = None,
    # pagination-ok: bounded by `limit` (capped at 100) and by the number of
    # concurrently in-flight bootstrap runs.
    limit: int = Query(default=100, ge=1, le=100),
) -> list[RunResponse]:
    """Return runs, newest first, optionally narrowed to one status.

    The intended caller is PMM's HA-leader-only stepper. It does not persist
    its own copy of which runs exist or where they are, so on every tick, and
    especially right after a leader failover, it re-discovers every run still
    in flight from here (``status=running``) rather than from any state of its
    own.

    :param session: The database session.
    :param status: Restrict to runs in this status. Omit for any status.
    :param limit: How many to return.
    :return: The runs. Does **not** reconcile in-flight steps — unlike
        :func:`get_bootstrap_run`, a caller polling a specific run for the
        purpose of driving it forward should use that route instead.
    """
    return [
        _run_response(run)
        for run in await BootstrapRunManager.list_runs(
            session, status=status, limit=limit
        )
    ]


@router.get("/runs/{run_id}")
async def get_bootstrap_run(
    run: LockedRun, session: SessionDep, tasks_client: TasksClient
) -> RunResponse:
    """Return one run, reconciling any of its running steps first.

    Reconciling on every read (rather than relying solely on a periodic task)
    means PMM's driver always sees a step's real outcome on its very next poll,
    not after waiting for a separate schedule to catch up. The run is read under
    its row lock, like every writing route (see the module docstring), since a
    reconcile may write it back.

    :param run: The path's run, read under its row lock.
    :param session: The database session.
    :param tasks_client: The Tasks API client, authenticated here with PMM Extensions'
        internal token rather than the caller's.
    :raises HTTPNotFoundException: When there is no such run.
    :return: The run, with current step status.
    """
    with tasks_client.auth(get_internal_token()):
        changed = await reconcile_run(tasks_client, run)
    if changed:
        run = await BootstrapRunManager.save(session, run)
    return _run_response(run)


def _strategy_for(install_method: InstallMethod) -> InstallStrategy:
    """Return the strategy for ``install_method``, or 400 if none is registered yet.

    :func:`~app.extensions.apps.om_bootstrap.strategies.strategy_for` raises a bare
    ``ValueError`` for an :class:`InstallMethod` declared on the enum but not yet
    implemented (``DOCKER``/``PODMAN``, today) — a caller's request, not a
    server bug, so this turns it into a 400 instead of an opaque 500.

    :param install_method: The install method to look up a strategy for.
    :raises HTTPBadRequestException: When no strategy implements ``install_method``.
    :return: The matching strategy.
    """
    try:
        return strategy_for(install_method)
    except ValueError as exc:
        raise HTTPBadRequestException(detail=str(exc)) from exc


def _build_step_action(builder: Callable[[], StepAction]) -> StepAction:
    """Call a strategy's ``build_*`` method, turning a bad-params error into 400.

    Every ``InstallStrategy.build_*`` method raises a bare ``ValueError`` when
    ``params`` is missing something the step needs (e.g. ``distribute_keyfile``
    without ``key_file_content``) — the caller's mistake, not a server bug, so
    this turns it into a 400 naming the problem instead of an opaque 500.

    :param builder: A zero-argument callable invoking one ``build_*`` method.
    :raises HTTPBadRequestException: When ``builder`` rejects its params.
    :return: The built step action.
    """
    try:
        return builder()
    except ValueError as exc:
        raise HTTPBadRequestException(detail=str(exc)) from exc


def _spec_for(run: BootstrapRun) -> tuple[InstallStrategy, BootstrapSpec]:
    """Rebuild ``run``'s strategy and spec from its persisted, typed fields.

    :param run: The run to rebuild a spec for.
    :return: The run's strategy, and the spec it plans/builds steps from.
    """
    install_method = to_strategy_install_method(run.install_method)
    spec = BootstrapSpec(
        install_method=install_method,
        os=to_strategy_os(run.os),
        mongodb_version=run.mongodb_version,
        replica_set_name=run.replica_set_name,
        run_id=run.id,
        data_path=run.data_path,
        log_path=run.log_path,
        port=run.port,
        bind_ip=run.bind_ip,
        member_configs={
            host: MemberConfig(**config) for host, config in run.member_configs.items()
        },
    )
    return _strategy_for(install_method), spec


@router.post(
    "/runs/{run_id}/hosts/{host}/steps/{step_name}:dispatch",
    status_code=http_status.HTTP_202_ACCEPTED,
)
@require_minimum_role(UserRole.ADMIN)
async def dispatch_run_step(
    run: LockedRun,
    host: str,
    step_name: str,
    session: SessionDep,
    request: Request,
    tasks_client: TasksClient,
    body: DispatchStepRequest | None = None,
) -> RunResponse:
    """Dispatch one host's named step now.

    Does **not** wait for the dispatch to finish — it returns as soon as the
    Tasks API accepts it, the same fire-and-forget shape ``dispatch_step``
    itself commits to. The caller polls :func:`get_bootstrap_run` for progress.

    Only a ``pending`` or ``failed`` step is dispatched. Re-dispatching a
    ``failed`` one is how PMM's driver implements its retry policy — this route
    does not itself decide *whether* to retry, only executes the request.

    :param run: The path's run, read under its row lock.
    :param host: The host to dispatch the step on.
    :param step_name: The step to dispatch — one of the names the run was
        planned with.
    :param session: The database session.
    :param request: The current request, whose host builds the artifact
        download URL the executor fetches the step's script from.
    :param tasks_client: The Tasks API client, authenticated here with PMM Extensions'
        internal token rather than the caller's.
    :param body: ``params`` this step needs — see :class:`DispatchStepRequest`.
    :raises HTTPNotFoundException: When there is no such run, host, or step.
    :raises HTTPConflictException: When the step is running, succeeded, or
        skipped.
    :return: The run, with the dispatched step now ``running``.
    """
    states = parse_host_states(run)
    host_state = _host_state(states, host, run)
    what = f"host {host!r}"
    step_index = _find_step(host_state.steps, step_name, what=what)
    step = host_state.steps[step_index]

    strategy, spec = _spec_for(run)
    params = _body_params(body)
    action = _build_step_action(
        lambda: strategy.build_step(step_name, host, spec, params)
    )

    host_state.steps[step_index] = await _dispatch_and_record(
        tasks_client, request, run, host, action, step
    )

    return await _save_host_states(session, run, states)


@router.post(
    "/runs/{run_id}/hosts/{host}/finalize/{step_name}:dispatch",
    status_code=http_status.HTTP_202_ACCEPTED,
)
@require_minimum_role(UserRole.ADMIN)
async def dispatch_finalize_step(
    run: LockedRun,
    host: str,
    step_name: str,
    session: SessionDep,
    request: Request,
    tasks_client: TasksClient,
    body: DispatchStepRequest | None = None,
) -> RunResponse:
    """Dispatch one host's named finalize step now.

    Same fire-and-forget shape as :func:`dispatch_run_step` — see its own
    docstring; the only difference is which list on
    :class:`~app.extensions.apps.om_bootstrap.strategy.HostBootstrapState` this reads
    and writes. This route does not check that every run-level step has
    succeeded first — deciding *when* it is safe to call this is PMM's
    stepper's job, not this route's (see the module docstring, and
    :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.plan_finalize_steps`'s
    own docstring for why that ordering matters at all).

    :param run: The path's run, read under its row lock.
    :param host: The host to dispatch the step on.
    :param step_name: The finalize step to dispatch — one of the names the run
        was planned with.
    :param session: The database session.
    :param request: See :func:`dispatch_run_step`.
    :param tasks_client: See :func:`dispatch_run_step`.
    :param body: ``params`` this step needs — see :class:`DispatchStepRequest`.
    :raises HTTPNotFoundException: When there is no such run, host, or finalize step.
    :raises HTTPConflictException: When the step is running, succeeded, or
        skipped.
    :return: The run, with the dispatched finalize step now ``running``.
    """
    states = parse_host_states(run)
    host_state = _host_state(states, host, run)
    what = f"host {host!r}"
    step_index = _find_step(host_state.finalize_steps, step_name, what=what)
    step = host_state.finalize_steps[step_index]

    strategy, spec = _spec_for(run)
    params = _body_params(body)
    action = _build_step_action(
        lambda: strategy.build_finalize_step(step_name, host, spec, params)
    )

    host_state.finalize_steps[step_index] = await _dispatch_and_record(
        tasks_client, request, run, host, action, step
    )

    return await _save_host_states(session, run, states)


@router.post(
    "/runs/{run_id}/run-steps/{step_name}:dispatch",
    status_code=http_status.HTTP_202_ACCEPTED,
)
@require_minimum_role(UserRole.ADMIN)
async def dispatch_run_run_step(
    run: LockedRun,
    step_name: str,
    session: SessionDep,
    request: Request,
    tasks_client: TasksClient,
    body: DispatchStepRequest | None = None,
) -> RunResponse:
    """Dispatch one run-level step now, targeting the run's seed host.

    Same fire-and-forget shape as :func:`dispatch_run_step` — see its own
    docstring. "Seed host" is ``run``'s first host, index 0, matching
    :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.build_run_step`'s
    own convention for where a run-level step actually executes.

    This route does not check that every per-host step succeeded first —
    deciding *when* it is safe to call this is PMM's stepper's job, not this
    route's (see the module docstring).

    :param run: The path's run, read under its row lock.
    :param step_name: The run-level step to dispatch — one of the names the
        run was planned with.
    :param session: The database session.
    :param request: See :func:`dispatch_run_step`.
    :param tasks_client: See :func:`dispatch_run_step`.
    :param body: ``params`` this step needs — see :class:`DispatchStepRequest`.
    :raises HTTPNotFoundException: When there is no such run, run-level step, or
        the run has no hosts to target.
    :raises HTTPConflictException: When the step is running, succeeded, or
        skipped.
    :return: The run, with the dispatched run-level step now ``running``.
    """
    states = parse_host_states(run)
    if not states:
        raise HTTPNotFoundException(detail=f"Run {run.id} has no hosts to target")
    seed_host = states[0].host

    run_steps = parse_run_steps(run)
    what = f"run {run.id}"
    step_index = _find_step(run_steps, step_name, what=what)
    step = run_steps[step_index]

    strategy, spec = _spec_for(run)
    hosts = [state.host for state in states]
    params = _body_params(body)
    action = _build_step_action(
        lambda: strategy.build_run_step(step_name, hosts, spec, params)
    )

    run_steps[step_index] = await _dispatch_and_record(
        tasks_client, request, run, seed_host, action, step
    )

    run.run_steps = dump_run_steps(run_steps)
    run = await BootstrapRunManager.save(session, run)
    return _run_response(run, hosts=states, run_steps=run_steps)


@router.post(
    "/runs/{run_id}/hosts/{host}/rollback/{step_name}:dispatch",
    status_code=http_status.HTTP_202_ACCEPTED,
)
@require_minimum_role(UserRole.ADMIN)
async def dispatch_rollback_step(
    run: LockedRun,
    host: str,
    step_name: str,
    session: SessionDep,
    request: Request,
    tasks_client: TasksClient,
) -> RunResponse:
    """Dispatch one host's named rollback step now.

    Same fire-and-forget shape as :func:`dispatch_run_step`. Rollback steps take
    no ``params``: every
    :meth:`~app.extensions.apps.om_bootstrap.strategy.InstallStrategy.build_rollback_step`
    a strategy defines only ever tears down what its own forward steps already
    wrote to the host, needing nothing new from the caller.

    Whether a host should be rolled back at all, and if so whether to dispatch
    its rollback steps in order or all at once, is PMM's stepper's call (its
    partial-failure policy), not this route's. This route only ever dispatches
    the one step it is asked to.

    :param run: The path's run, read under its row lock.
    :param host: The host to roll back.
    :param step_name: The rollback step to dispatch — one of the names the run
        was planned with.
    :param session: The database session.
    :param request: See :func:`dispatch_run_step`.
    :param tasks_client: See :func:`dispatch_run_step`.
    :raises HTTPNotFoundException: When there is no such run, host, or rollback step.
    :raises HTTPConflictException: When the step is running, succeeded, or
        skipped.
    :return: The run, with the dispatched rollback step now ``running``.
    """
    states = parse_host_states(run)
    host_state = _host_state(states, host, run)
    what = f"host {host!r}"
    step_index = _find_step(host_state.rollback_steps, step_name, what=what)
    step = host_state.rollback_steps[step_index]

    strategy, spec = _spec_for(run)
    action = _build_step_action(
        lambda: strategy.build_rollback_step(step_name, host, spec)
    )

    host_state.rollback_steps[step_index] = await _dispatch_and_record(
        tasks_client, request, run, host, action, step
    )

    return await _save_host_states(session, run, states)


@router.post("/runs/{run_id}:finish")
@require_minimum_role(UserRole.ADMIN)
async def finish_run(
    run: LockedRun, session: SessionDep, body: FinishRunRequest
) -> RunResponse:
    """Record the stepper's own decision that a run is done — failed or rolled back.

    The one way ``run.status`` reaches
    :attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.FAILED` or
    :attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRunStatus.ROLLED_BACK`:
    both are real calls only PMM's stepper makes (retries exhausted; rollback
    finished), never something ``om_bootstrap`` infers on its own — see
    ``reconcile.py``'s module docstring for the one status it *does* infer
    (SUCCEEDED) and why that's different.

    Also removes every step script the run still has on disk: no step of a
    finished run is dispatched again, so nothing will download them.

    :param run: The path's run, read under its row lock.
    :param session: The database session.
    :param body: The decided terminal status, and why.
    :raises HTTPNotFoundException: When there is no such run.
    :raises HTTPBadRequestException: When ``body.status`` is not one of
        :data:`_FINISHABLE_STATUSES`.
    :raises HTTPConflictException: When the run is already terminal.
    :return: The run, now terminal.
    """
    if body.status not in _FINISHABLE_STATUSES:
        raise HTTPBadRequestException(
            detail=f"status must be one of {sorted(_FINISHABLE_STATUSES)}"
        )
    if run.status != BootstrapRunStatus.RUNNING:
        raise HTTPConflictException(
            detail=f"Run {run.id} is already {run.status.value}"
        )

    run.status = body.status
    run.finished_at = utc_now()
    run.error = body.error
    run = await BootstrapRunManager.save(session, run)
    await asyncio.to_thread(cleanup_run_scripts, str(run.id))
    return _run_response(run)


async def _stop_running_steps(
    tasks_api: RemoteAPI, states: list[HostBootstrapState], run_steps: list[StepRecord]
) -> None:
    """Stop every currently-dispatching step's Nomad allocation, best-effort.

    Called once, the moment cancellation is first requested — not on every
    poll — so a host mid-install doesn't keep running after an operator asked
    it to stop. A stopped dispatch's ``TaskHistory`` reaches a terminal,
    non-success status, which the next :func:`~app.extensions.apps.om_bootstrap.reconcile.reconcile_step`
    translates onto the step as ``FAILED`` the same way any other interrupted
    dispatch would be — nothing here writes to ``StepRecord`` directly.

    Failures are logged and otherwise ignored: PMM's stepper's rollback
    decision does not depend on this succeeding (it already treats
    ``cancel_requested`` as reason enough on its own), and a step that
    couldn't be stopped here still eventually reaches a terminal status once
    its own dispatch actually finishes. Catches transport failures
    (``aiohttp.ClientError``/``TimeoutError``) alongside ``HTTPException`` — the
    Tasks API being unreachable is exactly the kind of outage an operator is
    likely to be hitting Abort over, and this call happens after
    ``cancel_requested`` is already saved (see :func:`cancel_run`), so an
    outage here must not surface as a failed cancellation.

    :param tasks_api: The Tasks API client.
    :param states: Every host's current state.
    :param run_steps: The run's current run-level steps.
    """
    step_lists = [
        step_list
        for state in states
        for step_list in (state.steps, state.rollback_steps, state.finalize_steps)
    ]
    step_lists.append(run_steps)
    for steps in step_lists:
        for step in steps:
            if step.status != StepStatus.RUNNING or step.task_history_id is None:
                continue
            try:
                await tasks_api.post(f"/history/{step.task_history_id}/stop/")
            except (HTTPException, aiohttp.ClientError, TimeoutError) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                logger.warning(
                    "Failed to stop task history %s while cancelling: %s",
                    step.task_history_id,
                    detail,
                )


@router.post("/runs/{run_id}:cancel", status_code=http_status.HTTP_202_ACCEPTED)
@require_minimum_role(UserRole.ADMIN)
async def cancel_run(  # raises-family-ok: the ordering test injects a fault the contract does not promise
    run: LockedRun, session: SessionDep, tasks_client: TasksClient
) -> RunResponse:
    """Request that a running bootstrap run stop and roll back every host.

    Records the request and best-effort interrupts whatever is currently
    dispatching (:func:`_stop_running_steps`) so it doesn't keep running for
    however long its own timeout is — it does not itself decide to roll
    anything back. That is PMM's stepper's call, exactly like every other
    rollback trigger (see the module docstring): it reads
    ``cancel_requested`` on its next poll and treats it the same as a step
    exhausting its retries.

    Idempotent while the run is still running: calling this again after
    cancellation was already requested is a no-op, not an error — an
    operator clicking Abort twice should never see a failure.

    Saves ``cancel_requested`` *before* attempting to stop anything: a poll
    landing between the two would otherwise still see ``cancel_requested=false``
    and the stepper could keep dispatching. Stopping is best-effort — the
    saved flag is the signal that actually matters (see
    :func:`_stop_running_steps`) — so it runs after, and its own failures
    (including the Tasks API being unreachable) never undo the save above.

    :param run: The path's run, read under its row lock.
    :param session: The database session.
    :param tasks_client: The Tasks API client, authenticated here with PMM Extensions'
        internal token rather than the caller's.
    :raises HTTPNotFoundException: When there is no such run.
    :raises HTTPConflictException: When the run is already terminal —
        rolled back or otherwise, there is nothing left to cancel.
    :return: The run, with ``cancel_requested`` now set.
    """
    if run.status != BootstrapRunStatus.RUNNING:
        raise HTTPConflictException(
            detail=f"Run {run.id} is already {run.status.value}"
        )
    if run.cancel_requested:
        return _run_response(run)

    run.cancel_requested = True
    run = await BootstrapRunManager.save(session, run)

    states = parse_host_states(run)
    run_steps = parse_run_steps(run)
    with tasks_client.auth(get_internal_token()):
        await _stop_running_steps(tasks_client, states, run_steps)

    return _run_response(run)
