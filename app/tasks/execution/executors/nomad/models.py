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

"""Provide task execution management for Nomad jobs."""

import asyncio
import io
import json
import logging
import tarfile
import time
from base64 import b64decode
from binascii import b2a_base64
from collections import defaultdict
from collections.abc import AsyncGenerator
from datetime import datetime, UTC
from enum import StrEnum
from functools import cached_property
from itertools import product
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, NamedTuple

import requests
from aiohttp import (
    ClientError,
    ClientTimeout,
)
from fastapi import status
from nomad import Nomad
from nomad.api.exceptions import BaseNomadException, URLNotFoundNomadException
from sqlalchemy_celery_beat.models import Period
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.celery.models import IntervalSchedule
from app.core.exceptions import HTTPBadRequestException
from app.core.requests import BaseRemoteAPI, StoredCredentialHeaderMixin
from app.core.settings_override.registry import (
    hot_field,
    InheritedMarkers,
    ReloadClassification,
    REMOTE_API_TLS_MARKERS,
)
from app.core.utils import (
    async_run,
    b64decode_str,
    make_datetime_utc,
    slugify,
    sort_dict,
    utc_now,
)
from app.core.utils.fields import (
    AuthCredentialSecretStr,
    AuthSchemeStr,
    strip_credential_url_userinfo,
)
from app.core.utils.pydantic import field_with_metadata
from app.tasks.anonymizer import anonymize_text
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.crud import TaskHistoryLogStateManager, TaskHistoryManager
from app.tasks.execution.executors.nomad.exceptions import (
    AllocationNotFoundError,
    JobNotFoundError,
)
from app.tasks.execution.executors.nomad.steps import (
    LAUNCH_CHECK_EXIT_CODE,
    LOG_CAPTURE_HOLD_DEFAULT_SECONDS,
    NomadStep,
)
from app.tasks.execution.models import BaseExecutor
from app.tasks.execution.utils import gzip_compress, minify_file_content
from app.tasks.logs.line_split import split_complete_lines, WithheldLineBuffer
from app.tasks.logs.log_reader import decompress_legacy_logs
from app.tasks.logs.log_writer import (
    backfill_legacy_logs,
    TaskHistoryLogWriter,
)
from app.tasks.models import (
    ExecutionEvent,
    ExecutorHostState,
    FileMetadata,
    LogCaptureStatusEnum,
    Task,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
)

logger = logging.getLogger(__name__)

_ONE_MEBIBYTE = 1024 * 1024
NOMAD_DEAD_JOB_STATUS = "dead"
NOMAD_DEAD_TASK_STATE = "dead"
NOMAD_RUNNING_TASK_STATE = "running"
_CAPTURE_HOLD_RELEASE_SIGNAL = "SIGTERM"
# The hold is a poststop step, so it stays ``pending`` for a sub-second after
# Nomad kills the payload, and a signal delivered then is dropped. These bound
# that start window and are deliberately not operator-settable: a budget that
# could reach zero would silently forfeit the release, leaving the allocation
# held until its own deadline.
_CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS = 5
_CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS = 0.5
#: The Nomad task driver every PMM Extensions payload runs under. Named once because the
#: dispatch filter and the reporting in ``get_host_states`` must agree on it: if they
#: drift, a host is reported healthy and jobs still refuse to place on it.
RAW_EXEC_DRIVER = "raw_exec"
#: Nomad's own word for a client it currently has contact with.
NODE_STATUS_READY = "ready"
# Internal states returned by :meth:`NomadExecutor._consume_nomad_log_stream` (not Nomad task states).
_NOMAD_LOG_STREAM_SOCK_TIMEOUT = "nomad-log-stream-sock-timeout"
_NOMAD_LOG_STREAM_CLIENT_ERROR = "nomad-log-stream-client-error"

_ANONYMIZED_STEPS: frozenset[NomadStep] = NomadStep.anonymized()


class StepLogDelta(NamedTuple):
    """Carry one fetched log delta and the advanced cursor metadata.

    :param text: The anonymized text fetched this cycle.
    :param nomad_offset: The advanced raw-byte Nomad cursor for the next fetch.
    :param producer_offset: The advanced post-anonymization byte cursor.
    :param withheld_bytes: Raw-byte count withheld this cycle.
    :param fetch_failed: Whether the underlying Nomad stream fetch failed.
    """

    text: str
    nomad_offset: int
    producer_offset: int
    withheld_bytes: int
    fetch_failed: bool


def _should_anonymize(step: str, anonymize_entities: set[PIIEntity] | None) -> bool:
    """Return whether ``step`` output should be anonymized for the given entities.

    :param step: The Nomad task name within the allocation.
    :param anonymize_entities: The requested PII entities, or ``None``.
    :return: ``True`` when the step is anonymized and entities were requested.
    """
    return step in _ANONYMIZED_STEPS and bool(anonymize_entities)


def _decode_and_anonymize(raw: bytes, anonymize_entities: set[PIIEntity] | None) -> str:
    """Decode raw log bytes as UTF-8 and redact PII when entities are requested.

    :param raw: The raw (pre-anonymization) log bytes.
    :param anonymize_entities: PII entities to redact, or ``None`` to skip
        anonymization.
    :return: The decoded text, anonymized when entities were requested.
    """
    text = raw.decode("utf-8", errors="replace")
    if anonymize_entities and text:
        text = anonymize_text(text, anonymize_entities)
    return text


def _warn_forced_flush(
    alloc_id: str, step: str, log_type: TaskLogType, ceiling: int, consequence: str
) -> None:
    """Warn that anonymization flushed an un-terminated line at the ceiling.

    :param alloc_id: The Nomad allocation identifier.
    :param step: The Nomad task name within the allocation.
    :param log_type: The log stream type (stdout or stderr).
    :param ceiling: The configured withheld-bytes ceiling that was exceeded.
    :param consequence: What advancing past the flushed tail unblocks.
    """
    logger.warning(
        "Forced anonymization flush of un-terminated log line "
        "alloc_id=%s step=%s log_type=%s withheld_ceiling=%s; "
        "accepting a redaction-boundary leak so %s",
        alloc_id,
        step,
        log_type,
        ceiling,
        consequence,
    )


def _nomad_event_body_text(ev: dict) -> str:
    raw = ev.get("DisplayMessage") or ev.get("Message") or ev.get("Description") or ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip() if raw else ""


def _nomad_event_exit_code(ev: dict) -> Any:
    exit_code = ev.get("ExitCode")
    details = ev.get("Details")
    if exit_code is None and isinstance(details, dict):
        return details.get("exit_code", details.get("ExitCode"))
    return exit_code


_STALE_SKIP_TASK_NAME = NomadStep.CHECK_STALENESS
_STALE_SKIP_EXIT_CODE = 75
_LAUNCH_CHECK_TASK_NAME = NomadStep.CHECK_LAUNCHABLE

# Statuses a RUNNING row may reach on the allocation status alone, when the
# allocation carries no task states to corroborate it. SUCCESS is excluded: an
# allocation that started no task produced no output and no exit code, so
# reporting a successful run off ``complete`` would both mislead operators and
# release any chained task waiting on this one. A dead job cannot leave the row
# RUNNING either, so anything outside this set resolves to LOST there.
_DEAD_END_ALLOC_STATUSES = frozenset(
    {
        TaskHistoryStatusEnum.FAILED,
        TaskHistoryStatusEnum.LOST,
        TaskHistoryStatusEnum.STOPPED,
    }
)


def _alloc_task_states(alloc: dict[str, Any]) -> dict[str, Any]:
    """Return an allocation's Nomad task states as a mapping.

    An allocation that has not started any task carries no ``TaskStates`` at
    all, the shape a reschedule chain produces once the client running the
    original allocation goes away, so the read degrades to an empty mapping
    instead of raising. Anything present but not a mapping is upstream shape
    drift rather than a task that never started, so it is logged before
    degrading the same way: raising here aborts the very sync, stop and log
    paths this guard exists to keep alive.

    :param alloc: The allocation details from Nomad.
    :return: The allocation's task states, empty when absent or malformed.
    """
    task_states = alloc.get("TaskStates")
    if task_states is not None and not isinstance(task_states, dict):
        logger.warning(
            "Allocation %s reports non-mapping task states: %r",
            alloc.get("ID"),
            task_states,
        )
        return {}
    return task_states or {}


def _alloc_step_state(alloc: dict[str, Any], step: str) -> dict[str, Any]:
    """Return one step's Nomad task state, or an empty dict when unavailable.

    A rescheduled allocation may no longer carry a given step, and a malformed
    state is treated the same way :func:`_alloc_task_states` treats a malformed
    container.

    :param alloc: The allocation details from Nomad.
    :param step: The Nomad task (step) name to look up.
    :return: The step's task state, empty when absent or malformed.
    """
    state = _alloc_task_states(alloc).get(step)
    if state is not None and not isinstance(state, dict):
        logger.warning(
            "Allocation %s reports a non-mapping task state for step %r: %r",
            alloc.get("ID"),
            step,
            state,
        )
        return {}
    return state or {}


def _alloc_step_sort_key(alloc: dict[str, Any], step: str) -> tuple[Any, Any, str]:
    """Return the ordering key for one step within an allocation's task states.

    :param alloc: The allocation details from Nomad.
    :param step: The Nomad task (step) name to key.
    :return: The step's start time, finish time and name, with ``"9"`` standing
        in for an absent timestamp so an unstarted step sorts last.
    """
    state = _alloc_step_state(alloc, step)
    return state.get("StartedAt") or "9", state.get("FinishedAt") or "9", step


def _capture_hold_step_state(alloc: dict[str, Any]) -> str | None:
    """Return the log-capture-hold step's Nomad state, or ``None`` when unreadable.

    Answers "may the hold be signalled", not "does the allocation have one":
    ``None`` covers both a step that is absent and one whose state is missing or
    malformed, and neither is safe to signal. Use a membership test against the
    task states to decide *presence*.

    :param alloc: The allocation details from Nomad.
    :return: The hold step's ``State``, or ``None`` when it cannot be read.
    """
    if NomadStep.LOG_CAPTURE_HOLD not in _alloc_task_states(alloc):
        return None
    return _alloc_step_state(alloc, NomadStep.LOG_CAPTURE_HOLD).get("State")


def _detect_capture_hold_ready(alloc: dict[str, Any]) -> bool:
    """Return ``True`` when every producing step is done and a hold is present.

    This is the signal that the full producer set has stopped writing, which is
    what makes "drained to EOF" knowable and the hold safe to release. It is
    deliberately *not* the same question as whether the task history is
    terminal: an allocation can be terminal by job status while a main task
    still sits at ``pending``, and releasing there would signal the hold before
    the producers finished, re-opening the race the hold exists to close.

    :param alloc: The allocation details from Nomad.
    :return: ``True`` when a hold step is present and every non-hold step
        reports ``dead``; ``False`` otherwise.
    """
    task_states = _alloc_task_states(alloc)
    if NomadStep.LOG_CAPTURE_HOLD not in task_states:
        return False
    producing_steps = [step for step in task_states if NomadStep.is_persistable(step)]
    return bool(producing_steps) and all(
        _alloc_step_state(alloc, step).get("State") == NOMAD_DEAD_TASK_STATE
        for step in producing_steps
    )


def _status_from_step_states(alloc: dict[str, Any]) -> TaskHistoryStatusEnum:
    """Return the task status implied by the non-hold steps' own outcomes.

    Derived per-step rather than from ``ClientStatus`` because the allocation is
    still ``running`` while the hold holds it — the client status cannot yet
    report the payload's outcome. The hold's own exit is excluded so a failed
    hold cannot re-label a task whose work succeeded.

    :param alloc: The allocation details from Nomad.
    :return: ``FAILED`` when any non-hold step reports a failure; ``SUCCESS``
        otherwise.
    """
    task_states = _alloc_task_states(alloc)
    failed = any(
        _alloc_step_state(alloc, step).get("Failed")
        for step in task_states
        if NomadStep.is_persistable(step)
    )
    return TaskHistoryStatusEnum.FAILED if failed else TaskHistoryStatusEnum.SUCCESS


def _detect_step_sentinel(
    task_states: dict[str, Any] | None, *, task_name: str, exit_code: int
) -> bool:
    """Return whether one prestart task terminated with its sentinel exit code.

    Walk the Nomad ``TaskStates`` dict produced by an allocation sync and look
    for a ``Terminated`` event on ``task_name`` whose exit code matches. Shape
    drift across Nomad API responses is tolerated — missing keys or unexpected
    types simply short circuit to ``False``.

    Each caller reads only its own step's state, so which sentinel fired is
    decided per step rather than by the order Nomad happened to run them in.

    :param task_states: The ``TaskStates`` object from a Nomad allocation.
    :param task_name: The Nomad task whose termination carries the sentinel.
    :param exit_code: The sentinel exit code that step aborts with.
    :return: ``True`` when that step terminated with that code.
    """
    if not isinstance(task_states, dict):
        return False
    state = task_states.get(task_name)
    if not isinstance(state, dict):
        return False
    events = state.get("Events")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("Type") != "Terminated":
            continue
        event_exit_code = _nomad_event_exit_code(event)
        try:
            if int(event_exit_code) == exit_code:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _detect_stale_skip(task_states: dict[str, Any] | None) -> bool:
    """Return ``True`` when the ``check-staleness`` prestart task exited 75.

    :param task_states: The ``TaskStates`` object from a Nomad allocation.
    :return: ``True`` when the allocation aborted because of the staleness
        preamble; ``False`` otherwise.
    """
    return _detect_step_sentinel(
        task_states,
        task_name=_STALE_SKIP_TASK_NAME,
        exit_code=_STALE_SKIP_EXIT_CODE,
    )


def _detect_unlaunchable(task_states: dict[str, Any] | None) -> bool:
    """Return ``True`` when the ``check-launchable`` prestart task aborted.

    An allocation from a job registered before that step existed carries no
    such task state and resolves ``False``, so in-flight runs are unaffected.

    :param task_states: The ``TaskStates`` object from a Nomad allocation.
    :return: ``True`` when the node could not resolve the launch command chain;
        ``False`` otherwise.
    """
    return _detect_step_sentinel(
        task_states,
        task_name=_LAUNCH_CHECK_TASK_NAME,
        exit_code=LAUNCH_CHECK_EXIT_CODE,
    )


def _sentinel_status(
    *, stale_skip: bool, unlaunchable: bool
) -> TaskHistoryStatusEnum | None:
    """Return the terminal status a prestart sentinel abort implies, if any.

    Staleness wins where both fired: a run that should not have been dispatched
    at all outranks anything learned about the node it landed on. Stating the
    precedence once keeps the two branches of
    :meth:`NomadExecutor._apply_terminal_status` from drifting apart.

    :param stale_skip: Whether the staleness preamble aborted the run.
    :param unlaunchable: Whether the launch check aborted the run.
    :return: The status to stamp, or ``None`` when no sentinel fired.
    """
    if stale_skip:
        return TaskHistoryStatusEnum.STALE
    if unlaunchable:
        return TaskHistoryStatusEnum.UNLAUNCHABLE
    return None


def _append_exit_code_suffix(
    parts: list[str], description: str, exit_code: Any
) -> None:
    desc_lower = description.lower()
    try:
        code_int = int(exit_code)
    except (TypeError, ValueError):
        if str(exit_code).lower() not in desc_lower:
            parts.append(f"(exit code {exit_code})")
        return
    if f"exit code {code_int}" not in desc_lower:
        parts.append(f"(exit code {exit_code})")


def _failed_step_reason(alloc: dict[str, Any]) -> str | None:
    """Return prose naming the first producing step that failed, if any.

    Reads the failing step off the allocation's task states: those carry a
    per-step ``Failed`` flag and the ``Terminated`` events holding exit codes,
    while :func:`_status_from_step_states` answers only failed-vs-success and
    names no step. Steps are walked in execution order rather than the order
    Nomad serialized them in, so a payload failure is reported ahead of a
    cleanup step that failed after it.

    The exit code is read off the step's *last* ``Terminated`` event. A step
    Nomad restarted carries one per attempt, oldest first, so the last is the
    termination the allocation ended on rather than the first attempt's.

    A malformed allocation, task-state container or step state is skipped
    rather than raised on, so shape drift costs a reason rather than the whole
    sync.

    :param alloc: The allocation details from Nomad.
    :return: The reason, or ``None`` when no producing step reports a failure.
    """
    task_states = _alloc_task_states(alloc)
    for step in sorted(task_states, key=lambda name: _alloc_step_sort_key(alloc, name)):
        if not NomadStep.is_persistable(step):
            continue
        state = task_states.get(step)
        if not isinstance(state, dict) or not state.get("Failed"):
            continue
        description = f"Step {step!r} failed"
        parts = [description]
        events = state.get("Events")
        if isinstance(events, list):
            for event in reversed(events):
                if not isinstance(event, dict):
                    continue
                if event.get("Type") != "Terminated":
                    continue
                exit_code = _nomad_event_exit_code(event)
                if exit_code is not None:
                    _append_exit_code_suffix(parts, description, exit_code)
                    break
        return f"{' '.join(parts)}."
    return None


def _terminal_status_reason(
    status: TaskHistoryStatusEnum, alloc: dict[str, Any]
) -> str | None:
    """Return the stored reason for a status the Nomad sync just resolved.

    ``FAILED`` reports the failing producing step and its exit code, and stores
    ``None`` when the allocation names none — the shape a client-status-derived
    failure has. Restating the status as "Failed." there would spend the
    column's ``None`` on a value that adds nothing to the status, where ``None``
    is what the column documents as "the reason is unknown". Every other status
    takes its prose from the enum, and a status carrying none stores ``None``.

    The enum's fragments are verb phrases written for the mid-sentence slot in
    :meth:`~app.tasks.models.TaskHistory.alert_for_status`, so they are rendered
    as standalone sentences here rather than given a subject: "The run
    execution tracking lost" is not a sentence.

    :param status: The terminal status the sync resolved.
    :param alloc: The allocation details from Nomad.
    :return: The reason to store, or ``None`` when the status carries none.
    """
    if status == TaskHistoryStatusEnum.FAILED:
        return _failed_step_reason(alloc)
    summary = status.operator_summary()
    return f"{summary[:1].upper()}{summary[1:]}." if summary else None


def _sortable_nomad_tracking_event(
    task_name: str,
    list_index: int,
    ev: dict,
) -> tuple[datetime, str, int, ExecutionEvent] | None:
    if not isinstance(ev, dict):
        return None
    ts_raw = ev.get("Time")
    try:
        ts_ns = int(ts_raw)
    except (TypeError, ValueError):
        return None

    event_type = ev.get("Type")
    if not isinstance(event_type, str):
        event_type = str(event_type) if event_type is not None else "Unknown"

    description = _nomad_event_body_text(ev)
    parts = []
    parts.append(description if description else event_type)

    exit_code = _nomad_event_exit_code(ev)
    if exit_code is not None:
        _append_exit_code_suffix(parts, description, exit_code)

    dt = NomadExecutor.timestamp_to_datetime(ts_ns)
    return (
        dt,
        task_name,
        list_index,
        ExecutionEvent(
            timestamp=make_datetime_utc(dt),
            event_type=event_type,
            description=" ".join(parts).strip(),
            step=task_name,
        ),
    )


def nomad_task_states_to_execution_events(
    task_states: dict[str, Any] | None,
) -> list[ExecutionEvent]:
    """Map Nomad allocation ``TaskStates`` (from tracking) to execution events.

    Parses each task's ``Events`` array defensively for Nomad API shape drift.

    :param task_states: The ``task_states`` object from execution tracking.
    :type task_states: dict[str, Any] | None
    :return: Events sorted by timestamp ascending (oldest first).
    :rtype: list[ExecutionEvent]
    """
    if not isinstance(task_states, dict):
        return []

    collected = []

    for task_name, state in task_states.items():
        if not isinstance(task_name, str) or not isinstance(state, dict):
            continue
        raw_events = state.get("Events")
        if not isinstance(raw_events, list):
            continue
        for idx, ev in enumerate(raw_events):
            row = _sortable_nomad_tracking_event(task_name, idx, ev)
            if row is not None:
                collected.append(row)

    collected.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in collected]


class NomadAllocStatusEnum(StrEnum):
    """Reproduce Nomad's possible allocation statuses.

    :cvar PENDING: Enum value for pending allocations.
    :vartype PENDING: str
    :cvar RUNNING: Enum value for running allocations.
    :vartype RUNNING: str
    :cvar COMPLETE: Enum value for completed allocations.
    :vartype COMPLETE: str
    :cvar FAILED: Enum value for failed allocations.
    :vartype FAILED: str
    :cvar LOST: Enum value for lost allocations.
    :vartype LOST: str
    :cvar UNKNOWN: Enum value for allocations with unknown status.
    :vartype UNKNOWN: str
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    LOST = "lost"
    UNKNOWN = "unknown"


class NomadExecutor(StoredCredentialHeaderMixin, BaseExecutor, BaseRemoteAPI):
    """Represent a Nomad task executor.

    :param wait_interval: The interval in seconds between status checks.
        Defaults to 5 seconds.
    :param endpoint: The base URL for the external API endpoint.
    :param verify_ssl: Whether to verify SSL certificates. Defaults to True.
    :param ssl_cafile: Path to the SSL certificate authority file. Defaults to None.
    :param ssl_keyfile: Path to the SSL key file. Defaults to None.
    :param ssl_certfile: Path to the SSL certificate file. Defaults to None.
    :param logger_name: Name to use for the logger. Defaults to ``__name__``.
    :param secure: Whether to use a secure connection. Defaults to False.
    :param timeout: The timeout in seconds for requests to the Nomad API.
        Defaults to 10 seconds.
    :param minify_payload: Whether to minify payloads before dispatching Parameterized
        Jobs. Defaults to True.
    :param log_socket_read_timeout: Socket read timeout in seconds for log streaming.
        Defaults to 10.
    :param cert_expiry_warn_days: Number of days before ``not_valid_after`` when
        Nomad TLS cert expiry alerts should fire. Used by the periodic
        ``check_nomad_cert_expiry`` task. Defaults to 7.
    :param check_cert_expiry_interval: Beat schedule for ``check_nomad_cert_expiry``
        (e.g. once per day). Set to ``None`` to skip registering the periodic task
        in ``app.tasks.db.seed`` (Celery beat will not run the check).
    :param api_key: Credential sent as ``Authorization: <auth_scheme> <api_key>``
        on both the synchronous and the asynchronous request path. It takes
        precedence over any userinfo embedded in ``endpoint``, which is stripped
        for as long as a key is configured. An empty value counts as unset,
        leaving whatever ``endpoint`` carries. A control character is rejected
        here rather than at send time. Defaults to ``None``.
    :param auth_scheme: Scheme the ``Authorization`` header announces ahead of
        ``api_key``. Defaults to ``"Bearer"``.
    :param terminal_log_drain_max_attempts: Number of bounded re-fetch attempts
        after terminal detection to drain any stdout/stderr tail Nomad's
        ``logmon`` flushes shortly after the task finishes. Each attempt waits
        ``terminal_log_drain_interval`` seconds, then re-reads from the persisted
        offsets. Each finishing task adds up to
        ``terminal_log_drain_max_attempts * terminal_log_drain_interval`` seconds
        to its terminal sync, run inline within the Celery beat cycle, so lower
        this for large fleets where many tasks finish in the same beat and the
        added latency would otherwise approach the beat cadence. Set to ``0`` to
        disable the drain (behaviour identical to the pre-drain terminal sync).
        Defaults to 5.
    :param terminal_log_drain_interval: Seconds to wait before each post-terminal
        drain re-fetch, giving ``logmon`` time to flush the tail. Defaults to 0.5.
    :param log_anonymization_max_withheld_bytes: Maximum raw byte length that
        task-log anonymization may withhold while awaiting a line terminator.
        When the trailing partial exceeds this ceiling it is flushed anyway,
        accepting a single redaction-boundary leak so an un-terminated line
        cannot stall log persistence, stall the live viewer, or drive
        quadratic Nomad re-fetching. Setting the ceiling very low effectively
        disables line-boundary anonymization, letting a PII token that straddles
        two fetched chunks escape redaction, so the value must stay positive.
        Defaults to 1 MiB, generous enough that ordinary line-oriented output
        never trips it.
    :param log_capture_hold_seconds: How long the ``log-capture-hold`` step
        keeps a finished allocation alive so Nomad cannot garbage-collect logs
        PMM Extensions has not read yet. Injected as dispatch meta and enforced on the
        execution host, so it bounds allocation residency even when the tasks
        service never returns; PMM Extensions releases the hold early on the normal path.
        Must outlast at least two sync cadences, or a sub-cadence step's output
        is collected before any sync samples it. Defaults to 90 seconds.
    :cvar INHERITED_MARKERS: Overlay marking the inherited ``BaseRemoteAPI`` TLS
        fields (``verify_ssl`` and the ``ssl_*`` paths) HOT and ``advanced``
        without redeclaring them; set to the shared :data:`REMOTE_API_TLS_MARKERS`.
    """

    # Overlay, not redeclaration: redeclaring would drop the inherited frozen
    # ``FieldInfo`` and force ``frozen=True`` repeated. ``endpoint`` left unmarked.
    # Reuses the shared TLS overlay so every remote-api model marks these the same.
    INHERITED_MARKERS: ClassVar[InheritedMarkers] = REMOTE_API_TLS_MARKERS
    secure: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=False, advanced=True
    )
    timeout: int = hot_field(10, advanced=True)  # ty: ignore[invalid-assignment]
    minify_payload: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=True, advanced=True
    )
    log_socket_read_timeout: int = hot_field(  # ty: ignore[invalid-assignment]
        10, advanced=True
    )
    cert_expiry_warn_days: int = hot_field(  # ty: ignore[invalid-assignment]
        7, ge=1, advanced=True
    )
    terminal_log_drain_max_attempts: int = hot_field(  # ty: ignore[invalid-assignment]
        5, ge=0, advanced=True
    )
    terminal_log_drain_interval: float = hot_field(  # ty: ignore[invalid-assignment]
        0.5, gt=0, advanced=True
    )
    log_anonymization_max_withheld_bytes: int = (  # ty: ignore[invalid-assignment]
        hot_field(_ONE_MEBIBYTE, gt=0, advanced=True)
    )
    log_capture_hold_seconds: int = hot_field(  # ty: ignore[invalid-assignment]
        LOG_CAPTURE_HOLD_DEFAULT_SECONDS, ge=1, advanced=True
    )
    check_cert_expiry_interval: (
        IntervalSchedule | None
    ) = (  # ty: ignore[invalid-assignment]
        field_with_metadata(
            metadata={"reload": ReloadClassification.HOT, "advanced": True},
            default_factory=lambda: IntervalSchedule(every=1, period=Period.DAYS),
        )
    )
    api_key: AuthCredentialSecretStr | None = None
    auth_scheme: AuthSchemeStr = hot_field(  # ty: ignore[invalid-assignment]
        "Bearer", advanced=True
    )

    _sync_session: requests.Session | None = None

    def _compute_base_url(self) -> str:
        """Compute the base URL, dropping userinfo once an API key is configured.

        ``__aenter__`` builds the aiohttp session from this value, so this is
        where the asynchronous path takes the strip that
        :func:`~app.core.utils.fields.strip_credential_url_userinfo` explains.

        Overriding the hook rather than the ``base_url`` computed field keeps
        the base class's JSON redaction in force, and ``base_url`` still appears
        in ``model_dump`` as the inherited computed field.

        :return: The base URL of the Nomad endpoint.
        """
        url = super()._compute_base_url()
        if self._credential_value is None:
            return url
        return strip_credential_url_userinfo(url)

    @cached_property
    def backend(self) -> Nomad:
        """Get the Nomad backend client.

        :return: An instance of the Nomad client configured with the executor's
            settings.
        :rtype: Nomad
        """
        cert = ()
        if self.ssl_certfile:
            if self.ssl_keyfile:
                cert = (self.ssl_certfile, self.ssl_keyfile)
            else:
                cert = (self.ssl_certfile,)
        address = str(self.endpoint).rstrip("/")
        session = requests.Session()
        if self._credential_value is not None:
            address = strip_credential_url_userinfo(address)
            session.headers.update(self.headers)
        self._sync_session = session
        return Nomad(
            address=address,
            secure=self.secure,
            timeout=self.timeout,
            verify=(self.secure and self.verify_ssl and self.ssl_cafile)
            or self.verify_ssl,
            cert=cert,
            session=session,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the asynchronous context manager, releasing both HTTP clients.

        The inherited exit closes the aiohttp session; this one also closes the
        ``requests.Session`` handed to python-nomad, which the executor owns
        rather than the library. Retirement runs through
        :meth:`~app.core.requests.remote_api.BaseRemoteAPI.close_when_idle`, so
        the close lands once the last consumer hold has drained.

        Dropping the cached :attr:`backend` alongside it keeps a re-entered
        executor symmetric with the inherited half, which rebuilds its session
        on the next ``__aenter__``: without the drop, the cache would hand the
        next caller a client whose session is closed.

        :param exc_type: The exception type, if any.
        :param exc_val: The exception value, if any.
        :param exc_tb: The traceback, if any.
        """
        await super().__aexit__(exc_type, exc_val, exc_tb)
        self.__dict__.pop("backend", None)
        if self._sync_session is not None:
            self._sync_session.close()
            self._sync_session = None

    @staticmethod
    def timestamp_to_datetime(timestamp: int) -> datetime:
        """Convert a Nomad timestamp to a datetime object.

        :param timestamp: The Nomad timestamp in nanoseconds.
        :type timestamp: int
        :return: The corresponding datetime object in UTC.
        :rtype: datetime
        """
        return datetime.fromtimestamp(timestamp / 10**9, UTC)

    @staticmethod
    def get_task_history_status_from_alloc_status(
        client_status: NomadAllocStatusEnum | None,
        default: TaskHistoryStatusEnum | None = None,
        *,
        stopped: bool = False,
    ) -> TaskHistoryStatusEnum | None:
        """Get the task history status based on the allocation status.

        :param client_status: The Nomad allocation status, or ``None`` when the
            allocation does not report one.
        :param default: The default status to return if no match is found. Defaults to
            None.
        :param stopped: Whether the Nomad job was stopped.
        :return: The corresponding task history status.
        """
        match client_status:
            case NomadAllocStatusEnum.COMPLETE if not stopped:
                return TaskHistoryStatusEnum.SUCCESS
            case NomadAllocStatusEnum.COMPLETE if stopped:
                return TaskHistoryStatusEnum.STOPPED
            case NomadAllocStatusEnum.FAILED:
                return TaskHistoryStatusEnum.FAILED
            case NomadAllocStatusEnum.LOST | NomadAllocStatusEnum.UNKNOWN:
                return TaskHistoryStatusEnum.LOST
            case _:
                return default

    @staticmethod
    def prepare_task(queue_item: TaskHistory, task: Task | None = None) -> Task:
        """Prepare a Task instance for execution.

        Modify the task data based on the execution request's metadata, such as setting
        target and datacenter information, and applying any necessary template
        substitutions.

        :param queue_item: The task history record containing the task to prepare.
        :type queue_item: TaskHistory
        :param task: The task to be executed. If None, the queue_item's task will be
            used.
        :type task: Task | None
        :return: The prepared ``Task`` instance ready for execution.
        :rtype: Task
        """
        task = queue_item.task if task is None else task
        # TODO: determine scenarios for execution, such as looking up an existing job  # noqa: TD002, TD003
        task.data["ID"] += f"-{slugify(queue_item.execution_request.target)}"
        if queue_item.execution_request.meta:
            # TODO: target is currently pushed in to meta  # noqa: TD002, TD003
            queue_item.execution_request.meta["target"] = (
                queue_item.execution_request.target
            )
            # TODO: allow templates in more fields, currently only for constraints  # noqa: TD002, TD003
            for meta_var, meta_val in queue_item.execution_request.meta.items():
                for i, constraint in enumerate(task.data["Constraints"]):
                    meta = "${NOMAD_META_" + meta_var + "}"
                    task.data["Constraints"][i] = json.loads(
                        json.dumps(constraint).replace(meta, str(meta_val)),
                    )
        return task

    def get_events(self, queue_item: TaskHistory) -> list[ExecutionEvent]:
        """Return task events from stored Nomad ``task_states`` tracking data."""
        tracking = queue_item.execution_request.tracking
        if not isinstance(tracking, dict):
            return []
        return nomad_task_states_to_execution_events(tracking.get("task_states"))

    async def parse_payload(
        self, payload: str | bytes, payload_format: str
    ) -> dict[str, Any]:
        """Parse a job spec payload based on its format.

        This function parses the payload according to the specified format
        (HCL, JSON, or YAML).

        :param payload: The job specification payload to be parsed.
        :type payload: str | bytes
        :param payload_format: The format of the payload, which can be "hcl", "json",
            or "yaml".
        :type payload_format: str
        :return: The parsed job specification.
        :rtype: dict[str, Any]
        :raises ValueError: If the provided payload format is unsupported.
        """
        if payload_format == "hcl":
            return await async_run(self.backend.jobs.parse, payload)
        return await super().parse_payload(payload, payload_format)

    def register_job(self, task: Task) -> dict[str, Any]:
        """Register a new job with the Nomad backend.

        Sends the job specification to Nomad for registration. Raises an error if the
        job status cannot be determined.

        :param task: The task to register as a job.
        :type task: Task
        :return: The status response from Nomad after registering the job.
        :rtype: dict[str, Any]
        :raises ValueError: If the job status cannot be determined.
        """
        job_status = self.backend.job.register_job(
            id_=task.data["ID"],
            job={"Job": task.data},
        )
        if not job_status:
            logger.error("Unable to determine status for task %s", task.id)
            raise ValueError("The job status could not be determined")
        return job_status

    def dispatch_job(
        self, queue_item: TaskHistory, task: Task | None = None
    ) -> dict[str, Any]:
        """Dispatch a parameterized job for execution.

        :param queue_item: The task history containing information about the execution.
        :type queue_item: TaskHistory
        :param task: The task to be executed. If None, the queue_item's task will be
            used.
        :type task: Task | None
        :return: The status response from Nomad after dispatching the job.
        :rtype: dict[str, Any]
        """
        task = queue_item.task if task is None else task
        logger.debug("Dispatching job: %s", queue_item)
        payload = queue_item.execution_request.payload_content
        if payload is not None:
            if self.minify_payload:
                payload = minify_file_content(payload)
            payload = b2a_base64(gzip_compress(payload)).decode("utf-8")

        filtered_meta = (
            {
                key: value
                for key, value in queue_item.execution_request.meta.items()
                if not key.startswith("_")
            }
            if queue_item.execution_request.meta
            else {}
        )
        parameterized_job = task.data.get("ParameterizedJob") or {}
        declared_meta = set(parameterized_job.get("MetaOptional") or []) | set(
            parameterized_job.get("MetaRequired") or []
        )
        if "staleness_threshold_seconds" in declared_meta:
            # Lazy import keeps app.tasks.config out of the nomad.models import
            # chain (config imports NomadExecutor back from this package).
            from app.tasks import config as tasks_config

            filtered_meta["staleness_threshold_seconds"] = str(
                tasks_config.tasks_settings.STALENESS_THRESHOLD_SECONDS
            )
        if "scheduled_at" in declared_meta:
            eta = queue_item.execution_request.eta
            filtered_meta["scheduled_at"] = str(
                int(eta.timestamp())
                if eta is not None
                else int(queue_item.created_at.timestamp())
            )
        if "log_capture_hold_seconds" in declared_meta:
            filtered_meta["log_capture_hold_seconds"] = str(
                self.log_capture_hold_seconds
            )

        custom_prefix = queue_item.execution_request.meta.get("_job_id_prefix", "")
        if custom_prefix:
            custom_prefix = f"-{slugify(custom_prefix)}"

        job_status = self.backend.job.dispatch_job(
            task.data["ID"],
            payload=payload,
            meta=filtered_meta,
            id_prefix_template=f"{slugify(queue_item.task.name)}-{queue_item.task.id}{custom_prefix}",
        )
        if not job_status:
            logger.error("Unable to dispatch task %s", task.id)
            raise ValueError("The job status could not be determined")
        return job_status

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Retrieve a job's details from the Nomad backend.

        Fetches the job information based on the task's ID. Raises an error if the job
        cannot be retrieved.

        :param job_id: The ID of the job to be retrieved.
        :type job_id: str
        :return: The job details retrieved from Nomad.
        :rtype: dict[str, Any]
        :raises JobNotFoundError: If the job could not be determined.
        """
        try:
            return self.backend.job.get_job(job_id)
        except URLNotFoundNomadException as exc:
            raise JobNotFoundError(
                str(exc.nomad_resp),
                executor_name="nomad",
                resource_type="job",
                resource_id=job_id,
            ) from None

    def get_job_for_task_history(self, queue_item: TaskHistory) -> dict[str, Any]:
        """Retrieve the job associated with a task history record.

        This method checks the task history's tracking information for a job ID and
        retrieves the job details from the Nomad backend. Raises an error if the job ID
        is missing or if the job cannot be found.

        :param queue_item: The task history record for which to fetch the job.
        :type queue_item: TaskHistory
        :return: The job details retrieved from Nomad.
        :rtype: dict[str, Any]
        :raises JobNotFoundError: If the job ID is missing or the job cannot be
            found.
        """
        if job_id := queue_item.execution_request.tracking.get("job_id"):
            return self.get_job(job_id)
        raise JobNotFoundError(
            f"Missing job_id in task history tracking ({queue_item.id})",
            executor_name="nomad",
            resource_type="job",
        )

    def get_hosts(self) -> dict[str, str]:
        """Get healthy node names from Nomad backend.

        :return: A dictionary with node names as key and the respective addresses
            as values.
        :rtype: dict[str, str]
        """
        filter_expression = (
            f"Status == {NODE_STATUS_READY} "
            f"and {RAW_EXEC_DRIVER} in Drivers "
            f"and Drivers.{RAW_EXEC_DRIVER}.Healthy == true"
        )
        return {
            node["Name"]: node["Address"]
            for node in self.backend.nodes.get_nodes(filter_=filter_expression)
        }

    def get_host_states(self) -> list[ExecutorHostState]:
        """Describe every node Nomad knows about, including the unusable ones.

        The same three conditions :meth:`get_hosts` filters on, reported separately
        instead of collapsed: ``Status == ready``, ``raw_exec`` present in
        ``Drivers``, and its ``Healthy`` flag. A caller asking "why can I not run
        anything on this machine" gets an answer here, where ``get_hosts`` can only
        omit the row.

        ``resources=True`` is not requested: the stub list carries ``Status`` and
        ``Drivers`` already, and the per-node detail fetch would be one request per
        node against a Nomad that may have hundreds.

        :return: One entry per registered Nomad client.
        """
        states = []
        for node in self.backend.nodes.get_nodes():
            driver = (node.get("Drivers") or {}).get(RAW_EXEC_DRIVER) or {}
            # An absent or non-boolean driver entry is not a healthy one: Nomad
            # omits drivers it has not detected, and "not detected" is exactly the
            # never-onboarded case worth telling apart. An identity check, not a
            # truthiness one, so a malformed upstream value cannot read as healthy.
            healthy = driver.get("Healthy") is True
            reachable = node.get("Status") == NODE_STATUS_READY
            if not reachable:
                # The driver fields are a stale pre-disconnect snapshot once the
                # node itself is unreachable, so explain the outage from the node
                # stub's own status text instead of a driver detail that predates it.
                detail = node.get("StatusDescription") or None
            elif healthy:
                detail = None
            else:
                # Only when it is a problem. Nomad sets HealthDescription to
                # the literal "Healthy" on a working driver, and a field that
                # explains failures must not be full of the word "Healthy" - a
                # reader scanning for the broken ones would find nothing to scan by.
                detail = driver.get("HealthDescription") or None
            states.append(
                ExecutorHostState(
                    name=node.get("Name", ""),
                    address=node.get("Address", ""),
                    reachable=reachable,
                    driver_healthy=healthy,
                    status=node.get("Status"),
                    detail=detail,
                )
            )
        return states

    def get_allocation_for_task_history(
        self, queue_item: TaskHistory
    ) -> dict[str, Any]:
        """Retrieve the allocation for a task history record.

        This method fetches the allocation details based on the job ID and evaluation ID
        associated with the task history. If an allocation ID is present in the tracking
        information, it retrieves the allocation directly using that ID.

        :param queue_item: The task history record for which to fetch the allocation.
        :type queue_item: TaskHistory
        :return: The allocation details from Nomad.
        :rtype: dict[str, Any]
        :raises AllocationNotFoundError: If allocation cannot be found directly
            through an allocation_id and no allocations are found with the job_id and
            evaluation_id attached to the queue_item.
        """
        if alloc_id := queue_item.execution_request.tracking.get("allocation_id"):
            logger.debug("Fetching allocation %r attached to TaskHistory", alloc_id)
            try:
                return self.backend.allocation.get_allocation(alloc_id)
            except URLNotFoundNomadException:
                logger.debug("Allocation %r not found", alloc_id)
        job_id = queue_item.execution_request.tracking.get("job_id")
        eval_id = queue_item.execution_request.tracking.get("evaluation_id")
        logger.debug(
            "Fetching last allocation for Job ID %s and Eval ID %s", job_id, eval_id
        )
        return self.get_last_allocation(job_id, eval_id)

    def get_last_allocation(
        self, job_id: str | None = None, eval_id: str | None = None
    ) -> dict[str, Any]:
        """Retrieve the last allocation for a job evaluation.

        This method fetches and returns details of the most recent allocation that
        matches the specified job ID and/or evaluation ID filters. Sorts task states for
        consistency.

        :param job_id: The ID of the job.
        :type job_id: str | None
        :param eval_id: The evaluation ID associated with the job.
        :type eval_id: str | None
        :return: The allocation details from Nomad, or None if no allocations are found.
        :rtype: dict[str, Any] | None
        :raises ValueError: If neither job_id nor eval_id is provided.
        :raises AllocationNotFoundError: If no allocations are found with the
            specified filters.
        """
        allocation_filters = []
        if job_id:
            allocation_filters.append(f'JobID == "{job_id}"')
        if eval_id:
            allocation_filters.append(f'EvalID == "{eval_id}"')
        if not allocation_filters:
            raise ValueError("Either job_id or eval_id must be provided")
        allocation_filter = " and ".join(allocation_filters)
        allocations = self.backend.allocations.get_allocations(
            filter_=allocation_filter,
            reverse=True,
        )
        if not allocations:
            raise AllocationNotFoundError(
                f"No allocations found with filter {allocation_filter!r}",
                executor_name="nomad",
                resource_type="allocation",
                resource_id=allocation_filter,
                job_id=job_id,
                evaluation_id=eval_id,
            )
        logger.debug("Allocations: %r", [alloc["JobID"] for alloc in allocations])
        alloc = allocations[0]
        if task_states := _alloc_task_states(alloc):
            alloc["TaskStates"] = sort_dict(
                task_states,
                lambda item: _alloc_step_sort_key(alloc, item[0]),
            )
        return alloc

    async def capture_hold_ready_job_ids(self) -> frozenset[str]:
        """Return the job IDs whose running allocation is capture-hold ready.

        One list call covers every running allocation, so detecting finished runs
        costs the same whatever the number of RUNNING histories. The hold keeps
        its allocation ``running`` while it waits to be released, so the
        ``running`` filter covers every run still waiting on its hold; one whose
        hold has already exited is left to the regular sync.

        The call goes through the executor's own HTTP client, which must be open.
        It carries no deadline of its own: the caller bounds it, and cancelling
        it stops the request however slowly Nomad is answering.

        :return: The ``JobID`` of every running allocation for which
            :func:`_detect_capture_hold_ready` holds.
        :raises aiohttp.ClientError: If Nomad cannot be reached or answers with an
            error status.
        :raises ValueError: If Nomad answers with a body that is not JSON.
        """
        async with self._request(
            "GET",
            "/v1/allocations",
            params={
                "filter": f'ClientStatus == "{NomadAllocStatusEnum.RUNNING}"',
                "task_states": "true",
            },
        ) as response:
            response.raise_for_status()
            allocations = await response.json()
        return frozenset(
            alloc["JobID"] for alloc in allocations if _detect_capture_hold_ready(alloc)
        )

    async def dispatch_task(
        self,
        session: AsyncSession,
        queue_item: TaskHistory,
        task: Task | None = None,
    ) -> TaskHistory:
        """Dispatch a task on the Nomad backend and update task history.

        This method starts the task on the Nomad backend, handling job creation and
        dispatching, and updates the task's execution history with tracking information.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param queue_item: The task history record for tracking this execution.
        :type queue_item: TaskHistory
        :param task: The task to be executed. If None, the queue_item's task will be
            used.
        :type task: Task | None
        :return: The updated task history with execution details.
        :rtype: TaskHistory
        :raises ValueError: If the job or job status cannot be determined.
        :raises NotImplementedError: If the job type or certain Nomad features are not
            yet supported.
        """
        task = self.prepare_task(queue_item, task)

        job_status = {}
        job = None
        if self.task_needs_job_register(task):
            job_status = self.register_job(task)
            logger.debug("Job status: %r", job_status)
            job = self.get_job(task.data["ID"])
            logger.debug("Job: %s", job)
        if task.data.get("ParameterizedJob"):
            job_status = self.dispatch_job(queue_item, task)
            job = self.get_job(job_status["DispatchedJobID"])
        if job is None:
            raise ValueError("The job could not be determined")

        submit_timestamp = job.get("SubmitTime")
        if submit_timestamp:
            queue_item.started_at = self.timestamp_to_datetime(submit_timestamp)
        else:
            queue_item.started_at = utc_now()
        queue_item.execution_request.tracking.update(
            evaluation_id=job_status["EvalID"], job_id=job["ID"]
        )
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        return await TaskHistoryManager.save(
            session,
            queue_item,
            flag_modified_fields=["execution_request"],
        )

    async def _stop_task(self, queue_item: TaskHistory) -> None:
        """Stop a task execution in Nomad.

        This method calls the Nomad API to stop the job associated with the given
        task history. It updates the task history with the status of the operation.

        Deregistering does not end a log-capture hold that is already holding the
        allocation: the job goes ``dead`` while the allocation stays ``running``
        for the rest of the hold's deadline. Nothing is being captured on this
        path — the stop route persists no logs — so the hold is released
        explicitly. Signalling straight after the deregister would almost always
        find the poststop step still ``pending``, which is why
        :meth:`_release_capture_hold` polls for it to start. A hold that never
        starts within that budget still expires on its own deadline.

        :param queue_item: The task history record for tracking this execution.
        :raises ValueError: When the task history carries no Nomad job id.
        """
        job_id = queue_item.execution_request.tracking.get("job_id")
        if not job_id:
            raise ValueError("The job ID could not be determined")
        self.backend.job.deregister_job(job_id)
        try:
            alloc = self.get_allocation_for_task_history(queue_item)
        except (AllocationNotFoundError, BaseNomadException):
            logger.debug(
                "No allocation to release a log-capture hold on for task history %s",
                queue_item.id,
            )
            return
        await self._release_capture_hold(alloc)

    def _fetch_step_log_delta(
        self,
        alloc_id: str,
        step: str,
        log_type: TaskLogType,
        nomad_start_offset: int,
        producer_start_offset: int,
        anonymize_entities: set[PIIEntity] | None,
        *,
        flush_partial: bool = False,
    ) -> StepLogDelta:
        """Fetch the delta bytes and advanced offsets for a single step/log_type.

        The Nomad offset tracks the raw (pre-anonymization) byte position in
        the Nomad log file and is used as the ``offset=`` kwarg for the next
        ``stream_logs.stream`` call; the producer offset tracks the byte
        position in the post-anonymization stream that the writer actually
        persists. Keeping them separate prevents the dedup window in
        :meth:`TaskHistoryLogWriter.append` from mixing raw and anonymized
        byte counts on retry.

        Anonymization runs on complete lines only: a PII token split across two
        Nomad frames (or across two sync cycles) is never seen whole by
        Presidio and would escape redaction. The trailing partial line is
        therefore withheld from anonymization -- the returned Nomad offset is
        rolled back by its raw byte length so it is simply re-fetched next cycle
        and joined with the new bytes. That re-fetch is idempotent because the
        withheld bytes were never emitted, so ``producer_offset`` never advanced
        past them; the writer's dedup covers a different case -- a retry after a
        concurrent state update. When the withheld remainder would exceed
        ``log_anonymization_max_withheld_bytes``, the buffer is flushed instead
        (empty remainder, no rollback) so the cursor advances and the next cycle
        does not re-fetch the same tail. ``flush_partial`` disables the
        withholding so a terminal drain can emit a newline-less final line
        instead of losing it forever.

        :param alloc_id: The Nomad allocation identifier.
        :param step: The Nomad task name within the allocation.
        :param log_type: The log stream type (stdout or stderr).
        :param nomad_start_offset: The byte offset to start reading from in
            Nomad-space.
        :param producer_start_offset: The producer-space byte offset
            corresponding to ``nomad_start_offset``.
        :param anonymize_entities: PII entities to anonymize for ``run-script``
            and ``step1`` content. When ``None``, no anonymization is performed.
        :param flush_partial: When ``True``, emit the trailing partial line
            instead of withholding it (terminal flush).
        :return: A :class:`StepLogDelta` carrying the anonymized bytes fetched
            this cycle, the advanced Nomad-space offset for the next fetch, the
            advanced producer-space offset the writer should persist, the raw
            byte length of the partial line withheld this cycle (``0`` when
            nothing was withheld, including after a forced ceiling flush), and
            whether the fetch itself failed.

        A failed fetch is reported rather than raised. Letting it propagate
        would abort the whole sync cycle for every step in the allocation, not
        just the failing stream — trading a silent per-stream loss for a loud
        whole-cycle one. The caller records the stream's capture as incomplete
        instead, so the failure is surfaced without being mistaken for a valid
        empty log.
        """
        try:
            raw_log_data = self.backend.client.stream_logs.stream(
                alloc_id,
                task=step,
                type_=log_type,
                offset=nomad_start_offset,
            )
        except BaseNomadException:
            logger.exception(
                "Error while fetching %s logs for allocation %s (step %s)",
                log_type,
                alloc_id,
                step,
            )
            return StepLogDelta(
                "",
                nomad_start_offset,
                producer_start_offset,
                0,
                fetch_failed=True,
            )
        pieces = []
        nomad_offset = nomad_start_offset
        for raw_log_data_item in (
            "{" + item for item in raw_log_data.split("{") if item
        ):
            log_data = json.loads(raw_log_data_item)
            if raw_msg := log_data.get("Data"):
                nomad_offset = log_data.get("Offset", nomad_offset)
                pieces.append(b64decode(raw_msg))
        raw_buf = b"".join(pieces)
        anonymizing = _should_anonymize(step, anonymize_entities)
        withheld = 0
        if anonymizing and not flush_partial:
            split = split_complete_lines(
                raw_buf, max_withheld=self.log_anonymization_max_withheld_bytes
            )
            complete, remainder = split.complete, split.remainder
            if split.forced:
                _warn_forced_flush(
                    alloc_id,
                    step,
                    log_type,
                    self.log_anonymization_max_withheld_bytes,
                    "the Nomad cursor can advance",
                )
            # A forced flush empties the remainder, so this is the no-rollback
            # path the docstring describes.
            withheld = len(remainder)
            nomad_offset -= withheld
        else:
            complete = raw_buf
        delta = _decode_and_anonymize(
            complete, anonymize_entities if anonymizing else None
        )
        producer_offset = producer_start_offset + len(delta.encode("utf-8"))
        return StepLogDelta(
            delta,
            nomad_offset,
            producer_offset,
            withheld,
            fetch_failed=False,
        )

    def get_logs_for_allocation(
        self,
        alloc: dict[str, Any],
        initial_logs: dict[str, dict[str, Any]] | None = None,
        anonymize_entities: set[PIIEntity] | None = None,
        *,
        flush_partial: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Return the delta logs for an allocation since the previous offsets.

        For every persistable ``(step, log_type)`` with a started task state,
        fetch the Nomad log stream starting at the offset provided in
        ``initial_logs`` (or ``0`` when absent). The returned dict holds ONLY
        the bytes fetched this cycle alongside the advanced
        ``f"{log_type}_last_offset"`` (Nomad-space, used as the next fetch
        cursor) and ``f"{log_type}_producer_offset"`` (post-anonymization byte
        count, used by the writer) for each stream; it is not an accumulator
        over cycles.

        The log-capture hold is skipped: it emits nothing, so its streams never
        advance. Including it would add a pair of candidates the terminal drain
        can never mark advanced, spending that drain's whole attempt budget on
        every terminal sync, and would leave the live viewer waiting on a stream
        that only ends when the hold does.

        :param alloc: The allocation details from Nomad.
        :param initial_logs: Per-step starting offsets in the shape
            ``{step: {f"{log_type}_last_offset": int,
            f"{log_type}_producer_offset": int}}``. Content fields are ignored.
        :param anonymize_entities: PII entities to anonymize in run-script /
            step1 output. When ``None``, no anonymization is performed.
        :param flush_partial: When ``True``, emit each stream's trailing partial
            line instead of withholding it (terminal flush).
        :return: A dict containing this cycle's delta log content and the new
            last offsets, shaped
            ``{step: {log_type: delta_text, f"{log_type}_last_offset": int,
            f"{log_type}_producer_offset": int,
            f"{log_type}_withheld": int,
            f"{log_type}_fetch_failed": bool}}``. ``_withheld`` is the raw byte
            length of the partial line withheld this cycle (``0`` when none);
            ``_fetch_failed`` reports a stream whose fetch raised, so the caller
            can record it as an incomplete capture rather than read the absent
            bytes as a valid empty log.
        """
        alloc_id = alloc["ID"]
        task_logs = defaultdict(dict)
        log_type_values = {log_type.value for log_type in TaskLogType}
        for step, payload in (initial_logs or {}).items():
            for key, value in payload.items():
                if isinstance(key, TaskLogType):
                    continue
                if isinstance(key, str) and key in log_type_values:
                    continue
                task_logs[step][key] = value
        task_states = _alloc_task_states(alloc)
        for step, log_type in product(task_states, TaskLogType):
            if not NomadStep.is_persistable(step):
                continue
            if _alloc_step_state(alloc, step).get("StartedAt") is None:
                continue
            last_offset_key = f"{log_type}_last_offset"
            producer_offset_key = f"{log_type}_producer_offset"
            withheld_key = f"{log_type}_withheld"
            fetch_failed_key = f"{log_type}_fetch_failed"
            nomad_start_offset = task_logs[step].get(last_offset_key) or 0
            producer_start_offset = task_logs[step].get(producer_offset_key) or 0
            step_delta = self._fetch_step_log_delta(
                alloc_id,
                step,
                log_type,
                nomad_start_offset,
                producer_start_offset,
                anonymize_entities,
                flush_partial=flush_partial,
            )
            task_logs[step][last_offset_key] = step_delta.nomad_offset
            task_logs[step][producer_offset_key] = step_delta.producer_offset
            task_logs[step][withheld_key] = step_delta.withheld_bytes
            task_logs[step][fetch_failed_key] = step_delta.fetch_failed
            task_logs[step][log_type] = step_delta.text
        return task_logs

    def _resolve_running_allocation(
        self, queue_item: TaskHistory
    ) -> tuple[dict[str, Any], str] | None:
        """Resolve the current allocation for a running task history.

        Walks any ``FollowupEvalID`` chain to land on the latest allocation. If
        the allocation is gone, mutates ``queue_item.status`` to FAILED or LOST
        and returns ``None`` so the caller knows to bail out without further
        work.

        :param queue_item: The running task history record.
        :type queue_item: TaskHistory
        :return: ``(alloc, job_id)`` when an allocation is found, or ``None``
            when the caller should return early.
        :rtype: tuple[dict[str, Any], str] | None
        """
        try:
            alloc = self.get_allocation_for_task_history(queue_item)
            job_id = alloc["JobID"]
            while followup_eval_id := alloc.get("FollowupEvalID"):
                alloc = self.get_last_allocation(job_id, followup_eval_id)
                queue_item.execution_request.tracking["task_states"] = {}
        except AllocationNotFoundError:
            logger.debug("Allocation not found for task history %s", queue_item.id)
        else:
            return alloc, job_id
        try:
            job = self.get_job_for_task_history(queue_item)
            if all(
                evaluation.get("Status") != NomadAllocStatusEnum.PENDING
                for evaluation in self.backend.job.get_evaluations(job["ID"])
            ):
                logger.warning(
                    "No allocations or pending evaluations found for task history %s",
                    queue_item.id,
                )
                queue_item.status = TaskHistoryStatusEnum.FAILED
                queue_item.set_failure_reason(
                    "The executor job produced no allocation and has no pending "
                    "evaluation."
                )
                queue_item.started_at = None
        except JobNotFoundError:
            logger.warning(
                "Lost job and allocation from task history %s", queue_item.id
            )
            queue_item.status = TaskHistoryStatusEnum.LOST
            queue_item.set_failure_reason(
                _terminal_status_reason(TaskHistoryStatusEnum.LOST, {})
            )
        return None

    def _stamp_finished_at(
        self, queue_item: TaskHistory, alloc: dict[str, Any]
    ) -> None:
        """Set ``finished_at`` from the allocation's last modification time.

        :param queue_item: The task history record reaching a terminal status.
        :param alloc: The allocation details from Nomad.
        """
        last_modified_timestamp = alloc.get("ModifyTime")
        queue_item.finished_at = (
            self.timestamp_to_datetime(last_modified_timestamp)
            if last_modified_timestamp
            else utc_now()
        )

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        """Synchronize the task history with the current state of the task in Nomad.

        Retrieve the latest allocation, update the status and the execution
        tracking metadata, and persist the delta logs fetched from Nomad into
        ``taskhistory_log`` via :class:`TaskHistoryLogWriter`. Legacy
        ``tracking["task_logs"]`` blobs are migrated into the chunk store on
        the first post-deployment sync via :func:`backfill_legacy_logs`, so
        pre-existing records keep a continuous log stream.

        An allocation that started no task carries no ``TaskStates``, leaving its
        client status as the only signal. One of ``_DEAD_END_ALLOC_STATUSES``
        moves the row out of RUNNING on that signal alone, because a row left
        RUNNING blocks every later dispatch of the same task, target and payload
        with a 409. While the job lives, a pending or running allocation is
        simply still starting and is left alone until
        ``PENDING_ALLOCATION_TIMEOUT_SECONDS`` elapses from ``started_at``, after
        which the row escalates to LOST and the job is deregistered so Nomad
        cannot still place it. Once the job is dead nothing will advance it, so
        any other status resolves to LOST: an allocation that started no task
        produced no exit code to have earned SUCCESS.

        :param queue_item: The task history record for tracking this execution.
        :param writer_session: The dedicated session to use for log chunk
            persistence. When ``None``, log persistence is skipped.
        :return: The updated task history with execution details.
        """
        if queue_item.status != TaskHistoryStatusEnum.RUNNING:
            return queue_item

        previous_allocation_id = queue_item.execution_request.tracking.get(
            "allocation_id"
        )

        resolved = self._resolve_running_allocation(queue_item)
        if resolved is None:
            return queue_item
        alloc, job_id = resolved

        task_states = _alloc_task_states(alloc)
        stale_skip = _detect_stale_skip(task_states)
        unlaunchable = _detect_unlaunchable(task_states)
        capture_hold_ready = _detect_capture_hold_ready(alloc)

        try:
            job = self.get_job(job_id)
        except JobNotFoundError:
            queue_item.status = TaskHistoryStatusEnum.LOST
            queue_item.set_failure_reason(
                _terminal_status_reason(TaskHistoryStatusEnum.LOST, alloc)
            )
        else:
            self._apply_terminal_status(
                queue_item,
                alloc,
                job,
                stale_skip=stale_skip,
                unlaunchable=unlaunchable,
                capture_hold_ready=capture_hold_ready,
            )

        if writer_session is not None:
            await self._persist_nomad_task_logs(
                writer_session=writer_session,
                queue_item=queue_item,
                alloc=alloc,
                previous_allocation_id=previous_allocation_id,
                capture_hold_ready=capture_hold_ready,
            )

        queue_item.execution_request.tracking.update(
            allocation_id=alloc["ID"],
            job_id=job_id,
            evaluation_id=alloc["EvalID"],
            task_states=task_states,
        )
        queue_item.execution_request.tracking.pop("task_logs", None)
        return queue_item

    def _apply_terminal_status(
        self,
        queue_item: TaskHistory,
        alloc: dict[str, Any],
        job: dict[str, Any],
        *,
        stale_skip: bool,
        unlaunchable: bool,
        capture_hold_ready: bool,
    ) -> None:
        """Resolve and stamp the task history's status from the allocation.

        Two detection paths, tried in order. The first keys on per-step task
        states and fires while a hold still holds the allocation, so a held task
        reaches its final status without waiting out the hold window. The second
        is the pre-hold behaviour, retained unchanged both for allocations from
        jobs registered before the hold step existed and as the fallback for any
        case where Nomad skips a step rather than marking it ``dead``.

        A prestart sentinel abort overrides the derived status on either path:
        both aborts leave a *failed* prestart step behind and a failed
        allocation, either of which would otherwise be reported as an ordinary
        payload failure. Where both fired, staleness wins — see
        :func:`_sentinel_status`. Each sentinel is read off its own step's task
        state, so the resolved status does not depend on the order Nomad ran the
        two prestart tasks in.

        An operator stop overrides a success but never a failure, matching
        :meth:`get_task_history_status_from_alloc_status`, where ``stopped``
        guards only the ``COMPLETE`` arm: a payload that already failed is
        reported ``FAILED`` even when a stop lands in the same window, so the
        failure is not relabelled as an operator action.

        When the allocation still has no task states and a non-terminal client
        status, :meth:`_should_escalate_pending_allocation` may escalate the
        row to LOST once ``PENDING_ALLOCATION_TIMEOUT_SECONDS`` has elapsed
        since ``started_at``. The job is deregistered first so Nomad cannot
        still place the allocation after the duplicate-dispatch guard drops; a
        Nomad error is swallowed so a hiccup cannot leave the row RUNNING. The
        finish time is ``utc_now()`` rather than the allocation's
        ``ModifyTime``, which may predate the timeout on a never-placed
        allocation and would under-report ``duration``.

        :param queue_item: The running task history record to stamp.
        :param alloc: The current Nomad allocation dict.
        :param job: The Nomad job dict backing the allocation.
        :param stale_skip: Whether the staleness preamble aborted the run.
        :param unlaunchable: Whether the launch check aborted the run because
            the node could not resolve the command chain.
        :param capture_hold_ready: Whether every producing step has stopped
            behind a live hold step.
        """
        task_states = _alloc_task_states(alloc)
        sentinel = _sentinel_status(stale_skip=stale_skip, unlaunchable=unlaunchable)
        if capture_hold_ready:
            self._stamp_finished_at(queue_item, alloc)
            if sentinel is not None:
                queue_item.status = sentinel
                queue_item.set_failure_reason(_terminal_status_reason(sentinel, alloc))
                return
            status = _status_from_step_states(alloc)
            if job.get("Stop", False) and status is not TaskHistoryStatusEnum.FAILED:
                status = TaskHistoryStatusEnum.STOPPED
            queue_item.status = status
            queue_item.set_failure_reason(_terminal_status_reason(status, alloc))
            return

        if job["Status"] == NOMAD_DEAD_JOB_STATUS:
            self._stamp_finished_at(queue_item, alloc)
            if sentinel is not None:
                queue_item.status = sentinel
                queue_item.set_failure_reason(_terminal_status_reason(sentinel, alloc))
                return
            status = self.get_task_history_status_from_alloc_status(
                alloc.get("ClientStatus"),
                queue_item.status,
                stopped=job.get("Stop", False),
            )
            queue_item.status = (
                status
                if task_states or status in _DEAD_END_ALLOC_STATUSES
                else TaskHistoryStatusEnum.LOST
            )
            queue_item.set_failure_reason(
                _terminal_status_reason(queue_item.status, alloc)
            )
            return

        if task_states:
            return
        dead_end_status = self.get_task_history_status_from_alloc_status(
            alloc.get("ClientStatus"),
            stopped=job.get("Stop", False),
        )
        if dead_end_status in _DEAD_END_ALLOC_STATUSES:
            logger.warning(
                "Allocation %s of task history %s reports %r with no task "
                "states; marking %s",
                alloc["ID"],
                queue_item.id,
                alloc.get("ClientStatus"),
                dead_end_status,
            )
            self._stamp_finished_at(queue_item, alloc)
            queue_item.status = dead_end_status
            queue_item.set_failure_reason(
                _terminal_status_reason(dead_end_status, alloc)
            )
        elif self._should_escalate_pending_allocation(queue_item):
            logger.warning(
                "Allocation %s of task history %s has remained without task "
                "states past the pending-allocation timeout; marking LOST",
                alloc["ID"],
                queue_item.id,
            )
            try:
                self.backend.job.deregister_job(job["ID"])
            except BaseNomadException:
                logger.warning(
                    "Could not deregister job %s while escalating task history "
                    "%s past the pending-allocation timeout; marking LOST anyway",
                    job["ID"],
                    queue_item.id,
                    exc_info=True,
                )
            queue_item.finished_at = utc_now()
            queue_item.status = TaskHistoryStatusEnum.LOST
            queue_item.set_failure_reason(
                _terminal_status_reason(TaskHistoryStatusEnum.LOST, alloc)
            )

    def _should_escalate_pending_allocation(self, queue_item: TaskHistory) -> bool:
        """Return whether a TaskStates-less allocation has outlived the age bound.

        Ages from ``queue_item.started_at`` so the bound does not reset across a
        FollowupEvalID reschedule chain. A missing ``started_at`` never
        escalates: the row has not entered RUNNING in a measurable way.

        :param queue_item: The RUNNING task history row under sync.
        :return: ``True`` when the configured bound has elapsed.
        """
        if queue_item.started_at is None:
            return False
        # Lazy import keeps app.tasks.config out of the nomad.models import
        # chain (config imports NomadExecutor back from this package).
        from app.tasks import config as tasks_config

        bound = tasks_config.tasks_settings.PENDING_ALLOCATION_TIMEOUT_SECONDS
        elapsed = utc_now() - make_datetime_utc(queue_item.started_at)
        return elapsed.total_seconds() >= bound

    async def _persist_nomad_task_logs(
        self,
        *,
        writer_session: AsyncSession,
        queue_item: TaskHistory,
        alloc: dict[str, Any],
        previous_allocation_id: str | None,
        capture_hold_ready: bool = False,
    ) -> None:
        """Persist this sync cycle's delta logs into the chunk store.

        Resets the fetch frontier (both cursors zeroed, ``producer_epoch``
        stamped to the new allocation's ``CreateIndex``) for every stream when
        Nomad reschedules to a new allocation so the fetcher reads the new
        allocation from the start; the epoch is threaded into every write so a
        sync that overlapped the switch cannot append superseded bytes.

        :param writer_session: The dedicated session used for log chunk
            persistence, supplied by the caller.
        :param queue_item: The running task history record.
        :param alloc: The current Nomad allocation dict.
        :param previous_allocation_id: The ``allocation_id`` stored in
            ``tracking`` at the start of this sync cycle, or ``None`` when the
            record has not yet landed on an allocation.
        :param capture_hold_ready: Whether the allocation's producing steps have
            all stopped behind a live hold step. Gates both the ``COMPLETE``
            verdict and the release signal: while it is ``False`` some producer
            may still be writing, so no stream can be called drained and the
            hold must not be cut short.
        """
        alloc_id = alloc["ID"]
        alloc_epoch = alloc["CreateIndex"]
        legacy_logs = decompress_legacy_logs(queue_item)
        if legacy_logs:
            await backfill_legacy_logs(writer_session, queue_item.id, legacy_logs)

        if previous_allocation_id is not None and previous_allocation_id != alloc_id:
            await TaskHistoryLogWriter.drain_and_reset_producer_frontier(
                writer_session, queue_item.id, new_producer_epoch=alloc_epoch
            )

        initial_offsets = await self._build_initial_log_offsets(
            writer_session, queue_item.id, alloc_epoch
        )
        task_logs = self.get_logs_for_allocation(
            alloc,
            initial_offsets,
            queue_item.anonymized_entities,
        )

        force_flush = queue_item.status != TaskHistoryStatusEnum.RUNNING
        await self._write_nomad_deltas(
            writer_session,
            queue_item.id,
            alloc_epoch,
            task_logs,
            force_flush=force_flush,
        )
        if force_flush:
            drain_failures = await self._drain_terminal_logs(
                writer_session, queue_item, alloc, alloc_epoch
            )
            await self._force_flush_remaining_streams(writer_session, queue_item.id)
            await self._record_capture_outcomes(
                writer_session,
                queue_item,
                alloc,
                task_logs,
                alloc_epoch,
                drain_failures=drain_failures,
                capture_hold_ready=capture_hold_ready,
            )
            if capture_hold_ready:
                await self._release_capture_hold(alloc)

    async def _record_capture_outcomes(
        self,
        writer_session: AsyncSession,
        queue_item: TaskHistory,
        alloc: dict[str, Any],
        task_logs: dict[str, dict[str, Any]],
        alloc_epoch: int,
        *,
        drain_failures: set[tuple[str, TaskLogType]],
        capture_hold_ready: bool,
    ) -> None:
        """Record a capture verdict for every stream this allocation produced.

        Writes a row per started ``(step, stream)`` pair even when the stream
        emitted nothing, which is what lets a reader tell a silent step from a
        lost one — the ordinary write path creates no row for an empty stream.
        The hold step is skipped: it produces no task output and its own stream
        only ends when the hold does.

        A stream earns ``COMPLETE`` only when its own fetch succeeded *and* the
        producer set is known to be finished. Where a hold is present but not
        yet releasable, some step may still be writing, so "this stream is at
        EOF" is not yet knowable and every verdict is capped at ``INCOMPLETE``.
        An allocation with no hold at all — one registered before the hold step
        existed — has nothing to wait on, so a clean fetch earns ``COMPLETE``
        there on its own.

        :param writer_session: The dedicated session used for log persistence.
        :param queue_item: The task history record reaching a terminal status.
        :param alloc: The current Nomad allocation dict.
        :param task_logs: This cycle's per-step log payloads, carrying the
            per-stream ``_fetch_failed`` flags from the pre-drain fetch.
        :param alloc_epoch: The allocation ``CreateIndex`` the verdicts describe,
            so a verdict from a superseded allocation cannot overwrite a newer.
        :param drain_failures: The ``(step, stream)`` pairs whose re-fetch failed
            during the terminal drain. A first fetch that succeeded says nothing
            about the tail — the drain exists because ``logmon`` flushes
            asynchronously — so a failure there means bytes may be missing.
        :param capture_hold_ready: Whether the producing steps have all stopped
            behind a live hold step.
        """
        # Presence is a membership test, not a readable state: a hold whose
        # state is missing or malformed is still a hold, and treating it as
        # absent would grant COMPLETE while a producer may still be writing.
        hold_present = NomadStep.LOG_CAPTURE_HOLD in _alloc_task_states(alloc)
        capture_known_final = capture_hold_ready or not hold_present
        for step in _alloc_task_states(alloc):
            if not NomadStep.is_persistable(step):
                continue
            if _alloc_step_state(alloc, step).get("StartedAt") is None:
                continue
            for stream in TaskLogType:
                fetch_failed = (
                    bool(task_logs.get(step, {}).get(f"{stream}_fetch_failed"))
                    or (step, stream) in drain_failures
                )
                capture_status = (
                    LogCaptureStatusEnum.COMPLETE
                    if capture_known_final and not fetch_failed
                    else LogCaptureStatusEnum.INCOMPLETE
                )
                await TaskHistoryLogWriter.record_capture_status(
                    writer_session,
                    queue_item.id,
                    source=step,
                    stream=stream,
                    capture_status=capture_status,
                    producer_epoch=alloc_epoch,
                )

    async def _release_capture_hold(self, alloc: dict[str, Any]) -> None:
        """Signal the log-capture-hold step so Nomad may collect the allocation.

        Re-reads the allocation first, because ``alloc`` is stale by the time a
        release is due and the hold only reaches ``running`` shortly after the
        last producing step dies. This is the only chance to release: the caller
        has already stamped a terminal status, and the sync sweep only revisits
        ``RUNNING`` histories. So the re-read polls on
        :data:`_CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS` and
        :data:`_CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS` to wait out the hold's poststop
        start window, which both callers can land inside. The budget is only
        ever spent there: a hold already running is signalled on the first read,
        and one past signalling is abandoned on it. A hold still not running
        when the attempts run out keeps the allocation until its own deadline,
        which is the bound the design accepts.

        A failed release is not a failed capture. The bytes are already
        persisted by the time this runs, and the step self-expires at its
        deadline, so the failure is logged and the verdicts stand.

        :param alloc: The Nomad allocation dict read at the start of the sync.
        """
        alloc_id = alloc["ID"]
        hold_state = None
        for attempt in range(_CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS)
            try:
                current = self.backend.allocation.get_allocation(alloc_id)
            except BaseNomadException:
                logger.warning(
                    "Could not re-read allocation %s to release the log-capture "
                    "hold; it will expire at its own deadline",
                    alloc_id,
                    exc_info=True,
                )
                return
            hold_state = _capture_hold_step_state(current)
            if hold_state == NOMAD_RUNNING_TASK_STATE:
                break
            if hold_state is None or hold_state == NOMAD_DEAD_TASK_STATE:
                # Absent, unreadable, or already over — waiting cannot make any
                # of those signallable.
                break

        if hold_state != NOMAD_RUNNING_TASK_STATE:
            logger.debug(
                "Log-capture hold on allocation %s is %r rather than running; "
                "leaving it to expire at its own deadline",
                alloc_id,
                hold_state,
            )
            return
        try:
            self.backend.client.allocation.signal_allocation(
                alloc_id,
                _CAPTURE_HOLD_RELEASE_SIGNAL,
                task=NomadStep.LOG_CAPTURE_HOLD,
            )
        except (BaseNomadException, ValueError):
            # ``signal_allocation`` decodes the response body, so a non-JSON or
            # empty body surfaces as a ValueError rather than a Nomad error.
            logger.warning(
                "Could not release the log-capture hold on allocation %s; it will "
                "expire at its own deadline",
                alloc_id,
                exc_info=True,
            )

    async def _drain_terminal_logs(
        self,
        writer_session: AsyncSession,
        queue_item: TaskHistory,
        alloc: dict[str, Any],
        alloc_epoch: int,
    ) -> set[tuple[str, TaskLogType]]:
        """Fetch Nomad logs after terminal detection until every stream is quiet.

        Nomad's ``logmon`` flushes stdout/stderr to the readable log file
        asynchronously, so the single terminal fetch in
        :meth:`_persist_nomad_task_logs` can miss a tail ``logmon`` had not yet
        written, most visibly the burst of output right before process exit.
        This bounded loop gives ``logmon`` a short interval to catch up, then
        re-reads from the persisted ``TaskHistoryLogState`` cursors, repeating
        until every started ``(step, stream)`` has advanced at least once and
        then gone quiet, or ``terminal_log_drain_max_attempts`` is reached. The
        writer's producer-offset dedup keeps every re-fetch idempotent, so an
        overlapping read cannot double-write.

        Advancement is tracked per stream, not per task: the loop early-exits
        only once every candidate stream has itself advanced-then-quieted. A
        stream that has not yet produced anything keeps the loop polling to the
        attempt cap, so a stream whose first post-terminal flush lands after a
        sibling stream already drained is not dropped, and a task that writes to
        only one stream polls the full window (correctness over latency; the
        window is ``hot``-tunable).

        Anonymization withholds each stream's trailing partial line until a
        newline completes it, so a stream holding a partial looks quiet. The
        early-exit is suppressed while any stream is still withholding, and a
        final ``flush_partial`` pass after the loop emits any line that never
        received a newline (a process exiting without one) so it is persisted
        rather than dropped.

        :param writer_session: The dedicated log writer session.
        :param queue_item: The terminal task history record being drained.
        :param alloc: The current Nomad allocation dict.
        :param alloc_epoch: The allocation's ``CreateIndex``, threaded into each
            write so a superseded allocation's bytes are discarded.
        :return: The ``(step, stream)`` pairs whose re-fetch failed at any point
            during the drain, so the caller can record their capture as
            incomplete rather than trust the pre-drain fetch alone.
        """
        advanced: set[tuple[str, TaskLogType]] = set()
        candidates: set[tuple[str, TaskLogType]] = set()
        fetch_failures: set[tuple[str, TaskLogType]] = set()
        for _ in range(self.terminal_log_drain_max_attempts):
            await asyncio.sleep(self.terminal_log_drain_interval)
            initial_offsets = await self._build_initial_log_offsets(
                writer_session, queue_item.id, alloc_epoch
            )
            task_logs = self.get_logs_for_allocation(
                alloc, initial_offsets, queue_item.anonymized_entities
            )
            candidates.update(
                (step, log_type) for step in task_logs for log_type in TaskLogType
            )
            fetch_failures.update(
                (step, log_type)
                for step in task_logs
                for log_type in TaskLogType
                if task_logs[step].get(f"{log_type}_fetch_failed")
            )
            produced = {
                (step, log_type)
                for step in task_logs
                for log_type in TaskLogType
                if task_logs[step].get(log_type)
            }
            withholding = {
                (step, log_type)
                for step in task_logs
                for log_type in TaskLogType
                if task_logs[step].get(f"{log_type}_withheld")
            }
            if produced:
                advanced |= produced
                await self._write_nomad_deltas(
                    writer_session,
                    queue_item.id,
                    alloc_epoch,
                    task_logs,
                    force_flush=True,
                )
            elif advanced == candidates and not withholding:
                break

        # Terminal flush: emit any trailing line that never received a newline.
        # Runs on both loop-exit branches so a withheld remainder is never lost.
        # Gated on whether withholding was even possible, not on the drain's
        # polling budget -- ``_should_anonymize`` requires truthy entities, so
        # this is an exact substitution that keeps AC3's no-loss guarantee
        # independent of ``terminal_log_drain_max_attempts``.
        if not queue_item.anonymized_entities:
            return fetch_failures
        final_offsets = await self._build_initial_log_offsets(
            writer_session, queue_item.id, alloc_epoch
        )
        final_logs = self.get_logs_for_allocation(
            alloc,
            final_offsets,
            queue_item.anonymized_entities,
            flush_partial=True,
        )
        fetch_failures.update(
            (step, log_type)
            for step in final_logs
            for log_type in TaskLogType
            if final_logs[step].get(f"{log_type}_fetch_failed")
        )
        await self._write_nomad_deltas(
            writer_session,
            queue_item.id,
            alloc_epoch,
            final_logs,
            force_flush=True,
        )
        return fetch_failures

    @staticmethod
    async def _build_initial_log_offsets(
        writer_session: AsyncSession,
        task_history_id: int,
        current_epoch: int,
    ) -> dict[str, dict[str, Any]]:
        """Return the per-stream ``initial_logs`` dict for the current cycle.

        Seeds each stream's next-fetch cursor from its durable
        ``TaskHistoryLogState`` row, but only when the row belongs to the
        current allocation — its ``producer_epoch`` matches ``current_epoch``
        or is the ``0`` legacy/unknown sentinel that trusts the migration
        backfill. A row stamped to a known *different* allocation holds a cursor
        in that allocation's byte space, so it is skipped and the stream
        restarts from ``0`` rather than reusing a superseded cursor.

        :param writer_session: The dedicated log writer session.
        :param task_history_id: The task history identifier.
        :param current_epoch: The running allocation's ``CreateIndex``.
        :return: A dict shaped
            ``{source: {f"{stream.value}_last_offset": int,
            f"{stream.value}_producer_offset": int}}``.
        """
        state_rows = await TaskHistoryLogStateManager.list_for_task(
            writer_session, task_history_id
        )
        initial_offsets = defaultdict(dict)
        for row in state_rows:
            if row.producer_epoch not in (0, current_epoch):
                continue
            initial_offsets[row.source][f"{row.stream.value}_last_offset"] = (
                row.producer_fetch_offset
            )
            initial_offsets[row.source][f"{row.stream.value}_producer_offset"] = (
                row.producer_offset
            )
        return initial_offsets

    @staticmethod
    async def _write_nomad_deltas(
        writer_session: AsyncSession,
        task_history_id: int,
        alloc_epoch: int,
        task_logs: dict[str, dict[str, Any]],
        *,
        force_flush: bool,
    ) -> None:
        """Append this cycle's delta bytes to the chunk store.

        :param writer_session: The dedicated log writer session.
        :type writer_session: AsyncSession
        :param task_history_id: The task history identifier.
        :type task_history_id: int
        :param alloc_epoch: The current Nomad allocation's ``CreateIndex``,
            stamped onto the state row so a write from a superseded allocation
            is discarded by the writer's epoch guard.
        :param task_logs: The per-step/per-stream delta dict returned by
            :meth:`get_logs_for_allocation`.
        :type task_logs: dict[str, dict[str, Any]]
        :param force_flush: Whether the caller is draining staging on a
            terminal sync.
        :type force_flush: bool
        """
        for step, payload in task_logs.items():
            for log_type in TaskLogType:
                delta_text = payload.get(log_type) or ""
                if not delta_text and not force_flush:
                    continue
                producer_fetch_offset = payload.get(f"{log_type.value}_last_offset", 0)
                producer_offset = payload.get(f"{log_type.value}_producer_offset", 0)
                await TaskHistoryLogWriter.append(
                    writer_session,
                    task_history_id,
                    source=step,
                    stream=log_type,
                    new_bytes=delta_text.encode("utf-8"),
                    producer_offset_after=producer_offset,
                    producer_fetch_offset_after=producer_fetch_offset,
                    producer_epoch=alloc_epoch,
                    force_flush=force_flush,
                )

    @staticmethod
    async def _force_flush_remaining_streams(
        writer_session: AsyncSession, task_history_id: int
    ) -> None:
        """Drain every known stream's staging buffer on a terminal sync.

        :param writer_session: The dedicated log writer session.
        :type writer_session: AsyncSession
        :param task_history_id: The task history identifier.
        :type task_history_id: int
        """
        for row in await TaskHistoryLogStateManager.list_for_task(
            writer_session, task_history_id
        ):
            await TaskHistoryLogWriter.append(
                writer_session,
                task_history_id,
                source=row.source,
                stream=row.stream,
                new_bytes=b"",
                force_flush=True,
            )

    def task_needs_job_register(self, task: Task) -> bool:
        """Determine whether a job needs to be registered for the task.

        Checks the task's configuration to decide if a job should be registered. If the
        task is a parameterized job, it checks if the job exists and if it has been
        updated since the last submission. For other job types, it currently assumes
        that a new job needs to be created.

        :param task: The task to evaluate.
        :type task: Task
        :return: `True` if a new job needs to be created, otherwise ``False``.
        :rtype: bool
        :raises BaseNomadException: If there is an issue communicating with the Nomad
            backend.
        """
        if task.data.get("ParameterizedJob"):
            try:
                job = self.get_job(task.data["ID"])
            except JobNotFoundError:
                return True
            return (submit_time := job.get("SubmitTime")) is None or make_datetime_utc(
                task.updated_at or task.created_at
            ) > self.timestamp_to_datetime(submit_time)
        return True

    # TODO: Use pydantic models instead of dict for job validation  # noqa: TD002, TD003
    async def validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Validate a Nomad job specification.

        This function sends a job specification to the Nomad backend for validation.
        If validation fails, it raises an HTTPException with the corresponding status code.

        :param job: The Nomad job specification to validate.
        :type job: dict[str, Any]
        :return: The original job specification if validation is successful.
        :rtype: dict[str, Any]
        :raises HTTPBadRequestException: If validation fails or Nomad returns an
            error status code.
        """
        valid = await async_run(self.backend.validate.validate_job, {"Job": job})
        if valid.status_code != status.HTTP_200_OK:
            raise HTTPBadRequestException(
                f"Nomad job validation failed with status {valid.status_code}"
            )
        resp = json.loads(valid.text)
        if not resp.get("ValidationErrors", []):
            return job
        logger.error(valid[0].text)
        raise HTTPBadRequestException("Invalid job specification")

    def _stream_log_timing(
        self,
        stream_start: float | None,
        params: dict[str, Any],
        start_offset: int,
    ) -> tuple[float, Any]:
        """Compute elapsed time and offset for Nomad log stream log messages.

        :param stream_start: Monotonic time when the HTTP body read started, or
            ``None`` if the stream had not reached that phase yet.
        :type stream_start: float | None
        :param params: Query parameters for the log request (may include updated
            ``offset`` from framed chunks).
        :type params: dict[str, Any]
        :param start_offset: Initial offset passed into the stream request.
        :type start_offset: int
        :return: Elapsed seconds since ``stream_start`` (0 if unknown) and the
            offset value to include in logs.
        :rtype: tuple[float, Any]
        """
        elapsed = time.monotonic() - stream_start if stream_start is not None else 0
        offset = params.get("offset", start_offset)
        return elapsed, offset

    def _log_stream_timeout(
        self,
        alloc_id: str,
        step: str,
        log_type: TaskLogType,
        stream_start: float | None,
        params: dict[str, Any],
        start_offset: int,
    ) -> None:
        """Log a socket read timeout while streaming Nomad task logs.

        :param alloc_id: Nomad allocation ID.
        :type alloc_id: str
        :param step: Task step name.
        :type step: str
        :param log_type: Log stream type (stdout/stderr).
        :type log_type: TaskLogType
        :param stream_start: Monotonic time when body read started, or ``None``.
        :type stream_start: float | None
        :param params: Request query parameters.
        :type params: dict[str, Any]
        :param start_offset: Initial stream offset.
        :type start_offset: int
        """
        elapsed, offset = self._stream_log_timing(stream_start, params, start_offset)
        logger.warning(
            "Nomad log stream sock_read timeout alloc_id=%s step=%s log_type=%s "
            "offset=%s elapsed=%.2fs timeout=%ss. "
            "Consider increasing TASKS__NOMAD__LOG_SOCKET_READ_TIMEOUT (current: %s)",
            alloc_id,
            step,
            log_type,
            offset,
            elapsed,
            self.log_socket_read_timeout,
            self.log_socket_read_timeout,
        )

    def _log_stream_cancelled(
        self,
        alloc_id: str,
        step: str,
        log_type: TaskLogType,
        stream_start: float | None,
        params: dict[str, Any],
        start_offset: int,
    ) -> None:
        """Log that a Nomad log stream was cancelled.

        :param alloc_id: Nomad allocation ID.
        :type alloc_id: str
        :param step: Task step name.
        :type step: str
        :param log_type: Log stream type (stdout/stderr).
        :type log_type: TaskLogType
        :param stream_start: Monotonic time when body read started, or ``None``.
        :type stream_start: float | None
        :param params: Request query parameters.
        :type params: dict[str, Any]
        :param start_offset: Initial stream offset.
        :type start_offset: int
        """
        elapsed, offset = self._stream_log_timing(stream_start, params, start_offset)
        logger.info(
            "Nomad log stream cancelled alloc_id=%s step=%s log_type=%s "
            "offset=%s elapsed=%.2fs",
            alloc_id,
            step,
            log_type,
            offset,
            elapsed,
        )

    def _log_stream_client_error(
        self,
        alloc_id: str,
        step: str,
        log_type: TaskLogType,
        stream_start: float | None,
        params: dict[str, Any],
        start_offset: int,
    ) -> None:
        """Log an aiohttp :exc:`~aiohttp.ClientError` while streaming Nomad logs.

        :param alloc_id: Nomad allocation ID.
        :type alloc_id: str
        :param step: Task step name.
        :type step: str
        :param log_type: Log stream type (stdout/stderr).
        :type log_type: TaskLogType
        :param stream_start: Monotonic time when body read started, or ``None``.
        :type stream_start: float | None
        :param params: Request query parameters.
        :type params: dict[str, Any]
        :param start_offset: Initial stream offset.
        :type start_offset: int
        """
        elapsed, offset = self._stream_log_timing(stream_start, params, start_offset)
        logger.exception(
            "Error fetching Nomad logs (ClientError) alloc_id=%s step=%s "
            "log_type=%s offset=%s elapsed=%.2fs",
            alloc_id,
            step,
            log_type,
            offset,
            elapsed,
        )

    async def _push_logs_to_queue(
        self,
        alloc: dict[str, Any],
        step: str,
        log_type: TaskLogType,
        queue: asyncio.Queue,
        start_offset: int = 0,
        anonymize_entities: set[PIIEntity] | None = None,
    ) -> None:
        """Push logs to the asynchronous queue for processing.

        Anonymization runs on complete lines only, so the trailing partial line
        of each frame is withheld in ``pending`` and carried across reconnects
        until a newline completes it (see :meth:`_decode_live_frame`). When the
        stream ends with a withheld remainder still pending -- a process
        exiting without a trailing newline -- it is redacted and pushed to the
        queue before the ``msg=None`` sentinel, so it is delivered rather than
        dropped.

        :param alloc: The allocation details containing the task states.
        :param step: The task step name.
        :param log_type: The type of log to stream ('stdout' or 'stderr').
        :param queue: The asyncio queue to push log lines into.
        :param start_offset: The offset to start reading logs from. Defaults to 0.
        :param anonymize_entities: PII entities to anonymize in run-script /
            step1 output. When ``None``, no anonymization is performed.
        """
        timeout = ClientTimeout(sock_read=self.log_socket_read_timeout)
        params = {
            "task": step,
            "type": log_type,
            "follow": "true",
            "offset": start_offset,
        }
        state = "running"
        pending = WithheldLineBuffer()
        while state == "running":
            alloc_id = alloc["ID"]
            stream_start = None
            try:
                state, alloc, stream_start = await self._consume_nomad_log_stream(
                    alloc=alloc,
                    step=step,
                    log_type=log_type,
                    queue=queue,
                    params=params,
                    client_timeout=timeout,
                    anonymize_entities=anonymize_entities,
                    pending=pending,
                )
            except asyncio.CancelledError:
                self._log_stream_cancelled(
                    alloc_id,
                    step,
                    log_type,
                    stream_start,
                    params,
                    start_offset,
                )
                raise

            if state == _NOMAD_LOG_STREAM_SOCK_TIMEOUT:
                self._log_stream_timeout(
                    alloc_id,
                    step,
                    log_type,
                    stream_start,
                    params,
                    start_offset,
                )
                break
            if state == _NOMAD_LOG_STREAM_CLIENT_ERROR:
                self._log_stream_client_error(
                    alloc_id,
                    step,
                    log_type,
                    stream_start,
                    params,
                    start_offset,
                )
                break
        if pending:
            decoded_msg = _decode_and_anonymize(
                pending.drain(), anonymize_entities or None
            )
            await queue.put(
                TaskLog(
                    step=step,
                    type=log_type,
                    msg=decoded_msg,
                    offset=params["offset"],
                )
            )
        await queue.put(TaskLog(step=step, type=log_type, msg=None))

    def _decode_live_frame(
        self,
        pending: WithheldLineBuffer,
        raw_msg: str,
        step: str,
        offset: int,
        anonymize_entities: set[PIIEntity] | None,
        *,
        alloc_id: str,
        log_type: TaskLogType,
    ) -> tuple[str | None, int]:
        """Decode a live log frame, anonymizing per complete line.

        For anonymized steps the trailing partial line is kept in ``pending``
        and withheld until a later frame supplies its newline, so a PII token
        split across frames is anonymized whole rather than per fragment.
        When the withheld remainder would exceed
        ``log_anonymization_max_withheld_bytes``, the whole buffer is flushed
        instead and ``pending`` is cleared so the live viewer keeps advancing.
        Non-anonymized steps decode and emit each frame unchanged.

        :param pending: The withheld-remainder buffer, mutated in place.
        :param raw_msg: This frame's base64-encoded ``Data`` field.
        :param step: The Nomad task name within the allocation.
        :param offset: The raw Nomad byte offset at the end of this frame.
        :param anonymize_entities: PII entities to redact, or ``None``.
        :param alloc_id: The Nomad allocation identifier (for forced-flush
            diagnostics).
        :param log_type: The log stream type (stdout or stderr).
        :return: ``(text, resume_offset)`` to emit, where ``resume_offset`` is
            rolled back over any withheld remainder so a reconnecting client
            re-fetches it; ``(None, offset)`` when the whole buffer is still a
            partial line and nothing should be emitted yet. After a forced
            ceiling flush ``pending`` is empty, so the resume offset is not
            rolled back.
        """
        if not _should_anonymize(step, anonymize_entities):
            return b64decode_str(raw_msg), offset
        release = pending.append(
            b64decode(raw_msg),
            max_withheld=self.log_anonymization_max_withheld_bytes,
        )
        if not release.complete:
            return None, offset
        if release.forced:
            _warn_forced_flush(
                alloc_id,
                step,
                log_type,
                self.log_anonymization_max_withheld_bytes,
                "the live viewer can advance",
            )
        decoded_msg = _decode_and_anonymize(release.complete, anonymize_entities)
        return decoded_msg, offset - len(pending)

    async def _consume_nomad_log_stream(
        self,
        alloc: dict[str, Any],
        step: str,
        log_type: TaskLogType,
        queue: asyncio.Queue,
        params: dict[str, Any],
        client_timeout: ClientTimeout,
        anonymize_entities: set[PIIEntity] | None,
        pending: WithheldLineBuffer,
    ) -> tuple[str, dict[str, Any], float | None]:
        """Perform a single Nomad log streaming HTTP request and consume chunks.

        Opens ``/v1/client/fs/logs/{alloc_id}``, handles 404 while the task has not
        started, streams framed JSON log lines into ``queue``, and returns when the
        stream ends or the allocation task state should be rechecked.

        On socket read timeout or :exc:`~aiohttp.ClientError`, returns internal state
        constants ``_NOMAD_LOG_STREAM_SOCK_TIMEOUT`` or ``_NOMAD_LOG_STREAM_CLIENT_ERROR``
        so the caller can log and stop without misclassifying cancellations.

        :param alloc: Nomad allocation dictionary (must include ``ID``, ``JobID``,
            ``EvalID``, and ``TaskStates``).
        :type alloc: dict[str, Any]
        :param step: Task step name for the ``task`` query parameter.
        :type step: str
        :param log_type: ``stdout`` or ``stderr``.
        :type log_type: TaskLogType
        :param queue: Queue to push :class:`~app.tasks.models.TaskLog` records into.
        :type queue: asyncio.Queue
        :param params: Mutable request query parameters (``offset`` is updated).
        :type params: dict[str, Any]
        :param client_timeout: aiohttp client timeout (e.g. socket read timeout).
        :type client_timeout: ClientTimeout
        :param anonymize_entities: Optional PII entities to redact for specific steps.
        :type anonymize_entities: set[PIIEntity] | None
        :param pending: Caller-owned buffer holding the trailing partial line
            withheld from anonymization; mutated in place and carried across
            reconnects so a token split at a frame boundary is redacted whole.
        :return: Nomad task ``State`` string, ``running`` to continue the outer
            loop, or a ``_NOMAD_LOG_STREAM_*`` sentinel; the allocation dict (possibly
            refreshed); and monotonic time when response body reads began, or
            ``None`` if that phase was not reached.
        :rtype: tuple[str, dict[str, Any], float | None]
        """
        alloc_id = alloc["ID"]
        stream_start = None
        logger.debug("Requesting logs for %s with params %s", alloc_id, params)
        try:
            async with self._request(
                "GET",
                f"/v1/client/fs/logs/{alloc_id}",
                params=params,
                timeout=client_timeout,
            ) as response:
                logger.debug("Log response status: %s", response.status)
                # The 404 clears once Nomad serves this step's logs, so retrying
                # is only useful while the allocation still lists the step.
                step_state = _alloc_step_state(alloc, step)
                if (
                    response.status == status.HTTP_404_NOT_FOUND
                    and step_state
                    and step_state.get("StartedAt") is None
                ):
                    logger.debug(
                        "Task %s of alloc %s has not started yet. Retrying in %s seconds...",
                        step,
                        alloc_id,
                        self.wait_interval,
                    )
                    await asyncio.sleep(self.wait_interval)
                    return ("running", alloc, stream_start)
                response.raise_for_status()
                stream_start = time.monotonic()
                logger.info(
                    "Nomad log stream started alloc_id=%s step=%s log_type=%s "
                    "sock_read_timeout=%ss offset=%s",
                    alloc_id,
                    step,
                    log_type,
                    self.log_socket_read_timeout,
                    params["offset"],
                )
                empty_data_count = 0
                raw_data = b""
                first_chunk_seen = False
                async for chunk, _ in response.content.iter_chunks():
                    raw_data += chunk
                    if b"}" not in chunk:
                        continue
                    data = json.loads(raw_data)
                    raw_data = b""
                    params["offset"] = offset = data.get("Offset", params["offset"])
                    if data and (msg := data.get("Data")):
                        empty_data_count = 0
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            elapsed = (
                                time.monotonic() - stream_start
                                if stream_start is not None
                                else 0
                            )
                            logger.debug(
                                "First log chunk received alloc_id=%s step=%s "
                                "log_type=%s offset=%s elapsed=%.2fs",
                                alloc_id,
                                step,
                                log_type,
                                offset,
                                elapsed,
                            )
                        decoded_msg, emit_offset = self._decode_live_frame(
                            pending,
                            msg,
                            step,
                            offset,
                            anonymize_entities,
                            alloc_id=alloc_id,
                            log_type=log_type,
                        )
                        if decoded_msg is None:
                            continue
                        await queue.put(
                            TaskLog(
                                step=step,
                                type=log_type,
                                msg=decoded_msg,
                                offset=emit_offset,
                            )
                        )
                    elif empty_data_count >= self.log_socket_read_timeout:
                        logger.debug(
                            "No data received for %s seconds, rechecking job status...",
                            self.log_socket_read_timeout,
                        )
                        alloc = self.get_last_allocation(
                            alloc["JobID"], alloc["EvalID"]
                        )
                        return (
                            # An empty state ends the caller's loop, so a step
                            # the refreshed allocation dropped stops the stream.
                            _alloc_step_state(alloc, step).get("State", ""),
                            alloc,
                            stream_start,
                        )
                    else:
                        empty_data_count += 1
                return ("running", alloc, stream_start)
        except TimeoutError:
            return (_NOMAD_LOG_STREAM_SOCK_TIMEOUT, alloc, stream_start)
        except ClientError:
            return (_NOMAD_LOG_STREAM_CLIENT_ERROR, alloc, stream_start)

    def preflight_stream_logs(self, queue_item: TaskHistory) -> None:
        """Resolve allocation for live log streaming before HTTP response headers are sent."""
        job_id, eval_id = self.job_eval_ids_for_stream_logs(queue_item)
        self.get_last_allocation(job_id, eval_id)

    def job_eval_ids_for_stream_logs(self, queue_item: TaskHistory) -> tuple[str, str]:
        """Return job_id and evaluation_id from task history tracking for log streaming.

        :param queue_item: The task history record whose tracking carries the ids.
        :return: The job and evaluation identifiers, in that order.
        """
        tracking = queue_item.execution_request.tracking or {}
        return (tracking["job_id"], tracking["evaluation_id"])

    async def stream_logs(
        self,
        queue_item: TaskHistory,
        start_offsets: dict[str, dict[str, int]] | None = None,
    ) -> AsyncGenerator[TaskLog | None, None]:
        """Stream logs from a task history record.

        Retrieves the allocation details and concurrently streams stdout and stderr logs
        for each task step. Yields ``TaskLog`` instances as log lines are received.

        The log-capture hold is not a step a viewer sees: it emits nothing, and
        its streams stay open for as long as it holds the allocation, so
        following them would keep the viewer waiting past the payload's own end.

        :param queue_item: The task history record for tracking the logs.
        :param start_offsets: A dictionary containing the starting offsets for each
            step and log type. If None, defaults to starting from the beginning.
        :return: An async generator yielding ``TaskLog`` instances containing
            log messages.
        """
        job_id, eval_id = self.job_eval_ids_for_stream_logs(queue_item)
        alloc = self.get_last_allocation(job_id, eval_id)
        active_streams = set()
        queue = asyncio.Queue()
        push_logs_tasks = []
        task_states = _alloc_task_states(alloc)
        if task_states:
            start_offsets = defaultdict(dict, start_offsets or {})
            for step, log_type in product(task_states, TaskLogType):
                if not NomadStep.is_persistable(step):
                    continue
                stream = (step, log_type)
                logger.debug("Adding %s to active_streams", stream)
                active_streams.add(stream)
                push_logs_tasks.append(
                    asyncio.create_task(
                        self._push_logs_to_queue(
                            alloc,
                            step,
                            log_type,
                            queue,
                            start_offsets[step].get(log_type, 0),
                            queue_item.anonymized_entities,
                        )
                    )
                )
            try:
                while active_streams:
                    logger.debug("Waiting for log line for streams %s", active_streams)
                    log_line = await queue.get()
                    logger.debug("Received log line %s", log_line)
                    if log_line.msg is None:
                        stream = (log_line.step, log_line.type)
                        logger.info(
                            "Log stream %s is over, removing it from active_streams",
                            stream,
                        )
                        active_streams.remove(stream)
                        continue
                    yield log_line
                    queue.task_done()
            finally:
                for task in push_logs_tasks:
                    task.cancel()
                await asyncio.gather(*push_logs_tasks, return_exceptions=True)
        else:
            yield None

    async def stream_file(
        self,
        queue_item: TaskHistory,
        path: str,
        chunk_size: int = _ONE_MEBIBYTE,
        *,
        anonymize: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Stream a file from the allocation's filesystem in chunks.

        Directories are archived on the fly and streamed as a tar.gz archive.

        :param queue_item: The task history record for tracking the logs.
        :param path: The path to the file to be streamed.
        :param chunk_size: The size of each chunk to read from the file. Defaults to
            1 MiB.
        :param anonymize: Whether to redact the task's configured entities from the
            streamed content. Defaults to ``True``, as every read served to a user
            must be redacted; internal reads of content PMM Extensions itself produced may opt
            out to get the bytes back verbatim.
        :return: An async generator yielding chunks of the file as bytes.
        """
        alloc = self.get_allocation_for_task_history(queue_item)
        alloc_id = alloc["ID"]
        params = {"path": path}
        async with self._request(
            "GET", f"/v1/client/fs/stat/{alloc_id}", params=params
        ) as response:
            response.raise_for_status()
            stat = await response.json()
        logger.debug("File stat: %r", stat)

        if self._is_directory(stat):
            logger.info("Requested path %s is a directory, streaming as tar.gz", path)
            async for chunk in self._stream_directory_as_tar_gz(
                queue_item, alloc_id, path, anonymize=anonymize
            ):
                yield chunk
            return

        file_size = stat.get("Size", 0)
        if file_size == 0:
            logger.warning("File %s is empty or does not exist", path)
            yield b""
            return
        logger.info("Starting to stream file %s of size %s", path, file_size)
        endpoint = f"/v1/client/fs/readat/{alloc_id}"
        params["limit"] = chunk_size
        for offset in range(0, file_size, chunk_size):
            params["offset"] = offset
            async with self._request("GET", endpoint, params=params) as response:
                response.raise_for_status()
                content = await response.read()
                if anonymize and queue_item.anonymized_entities:
                    try:
                        text = content.decode()
                        yield anonymize_text(
                            text, queue_item.anonymized_entities
                        ).encode()
                        continue
                    except UnicodeDecodeError:
                        logger.debug(
                            "Could not decode file content, sending raw bytes",
                            exc_info=True,
                        )
                yield content

    @staticmethod
    def _is_directory(stat: dict[str, Any]) -> bool:
        """Check whether the given file stat describes a directory."""
        return bool(
            stat.get("IsDir")
            or stat.get("Directory")
            or stat.get("Type") in {"directory", "dir"}
        )

    async def _iter_directory_entries(
        self, alloc_id: str, path: str, relative_prefix: str
    ) -> AsyncGenerator[tuple[str, str, bool, int], None]:
        """Yield directory entries recursively with absolute and relative paths."""
        async with self._request(
            "GET", f"/v1/client/fs/ls/{alloc_id}", params={"path": path}
        ) as response:
            response.raise_for_status()
            entries = await response.json()

        for entry in entries:
            name = entry["Name"]
            if name.startswith("."):
                continue
            is_dir = bool(
                entry.get("IsDir")
                or entry.get("Directory")
                or entry.get("Type") in {"directory", "dir"}
            )
            size = entry.get("Size", 0)
            abs_path = "/".join([path.rstrip("/"), name]).replace("//", "/")
            rel_path = f"{relative_prefix}/{name}".lstrip("/")
            rel_path = (
                f"{rel_path}/" if is_dir and not rel_path.endswith("/") else rel_path
            )
            yield abs_path, rel_path, is_dir, size
            if is_dir:
                async for sub_entry in self._iter_directory_entries(
                    alloc_id, abs_path, rel_path.rstrip("/")
                ):
                    yield sub_entry

    async def _read_file_bytes(
        self,
        queue_item: TaskHistory,
        alloc_id: str,
        path: str,
        file_size: int,
        chunk_size: int = _ONE_MEBIBYTE,
        *,
        anonymize: bool = True,
    ) -> bytes:
        """Read a remote file fully, applying anonymization when needed."""
        if file_size == 0:
            return b""
        endpoint = f"/v1/client/fs/readat/{alloc_id}"
        params = {"path": path, "limit": chunk_size}
        content = bytearray()
        for offset in range(0, file_size, chunk_size):
            params["offset"] = offset
            async with self._request("GET", endpoint, params=params) as response:
                response.raise_for_status()
                chunk = await response.read()
                if anonymize and queue_item.anonymized_entities:
                    try:
                        text = chunk.decode()
                        chunk = anonymize_text(
                            text, queue_item.anonymized_entities
                        ).encode()
                    except UnicodeDecodeError:
                        logger.debug(
                            "Could not decode file content for anonymization, sending raw bytes",
                            exc_info=True,
                        )
                content.extend(chunk)
        if len(content) != file_size:
            logger.warning(
                "Read %s bytes for %s but expected %s; using actual length",
                len(content),
                path,
                file_size,
            )
        return bytes(content)

    async def _stream_directory_as_tar_gz(
        self,
        queue_item: TaskHistory,
        alloc_id: str,
        path: str,
        *,
        anonymize: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Archive a directory on the fly and stream it as a tar.gz archive."""
        queue = asyncio.Queue()
        root_name = Path(path.rstrip("/")).name or "archive"

        class QueueWriter:
            def __init__(self, target_queue: asyncio.Queue[bytes | None]) -> None:
                self.queue = target_queue

            def write(self, data: bytes) -> int:  # pragma: no cover - trivial
                self.queue.put_nowait(data)
                return len(data)

        async def produce() -> None:
            try:
                writer = QueueWriter(queue)
                with tarfile.open(mode="w|gz", fileobj=writer) as tar:
                    root_info = tarfile.TarInfo(name=f"{root_name}/")
                    root_info.type = tarfile.DIRTYPE
                    tar.addfile(root_info)
                    async for (
                        abs_path,
                        rel_path,
                        is_dir,
                        size,
                    ) in self._iter_directory_entries(alloc_id, path, root_name):
                        try:
                            tarinfo = tarfile.TarInfo(name=rel_path)
                            tarinfo.mtime = int(utc_now().timestamp())
                            if is_dir:
                                tarinfo.type = tarfile.DIRTYPE
                                tar.addfile(tarinfo)
                                continue
                            file_bytes = await self._read_file_bytes(
                                queue_item,
                                alloc_id,
                                abs_path,
                                size,
                                anonymize=anonymize,
                            )
                            tarinfo.size = len(file_bytes)
                            tar.addfile(tarinfo, fileobj=io.BytesIO(file_bytes))
                        except Exception:
                            logger.exception(
                                "Failed to add %s to tarball, skipping entry", rel_path
                            )
                            continue
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            await producer

    async def list_files(
        self, queue_item: TaskHistory, path: str
    ) -> dict[str, FileMetadata]:
        """List files in a directory on the allocation's filesystem.

        :param queue_item: The task history record for tracking the logs.
        :type queue_item: TaskHistory
        :param path: The path to the directory to list files from.
        :type path: str
        :return: A dictionary with filenames as keys and their metadata as values.
            The metadata includes the file size and whether it is a directory.
        :rtype: dict[str, FileMetadata]
        """
        alloc = self.get_allocation_for_task_history(queue_item)
        alloc_id = alloc["ID"]
        async with self._request(
            "GET", f"/v1/client/fs/ls/{alloc_id}", params={"path": path}
        ) as response:
            if response.status == status.HTTP_404_NOT_FOUND:
                alloc_status = alloc.get("ClientStatus", "")
                if alloc_status in (
                    NomadAllocStatusEnum.FAILED,
                    NomadAllocStatusEnum.LOST,
                ):
                    logger.debug(
                        "Allocation %r has no filesystem (prestart failure); returning empty files",
                        alloc_id,
                    )
                    return {}
            response.raise_for_status()
            files = await response.json()
        return {
            filename: FileMetadata(
                size=file.get("Size", 0),
                is_dir=bool(
                    file.get("IsDir")
                    or file.get("Directory")
                    or file.get("Type") in {"directory", "dir"}
                ),
            )
            for file in files
            if not (filename := file["Name"]).startswith(".")
        }
