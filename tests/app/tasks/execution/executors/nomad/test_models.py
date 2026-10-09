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

"""Define tests for the app.tasks.execution.executors.nomad.models module."""

import asyncio
import io
import json
import logging
import re
import tarfile
import threading
import time
from base64 import b64encode
from binascii import b2a_base64
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
from itertools import pairwise
from typing import Any
from unittest.mock import AsyncMock, call, MagicMock, patch

import pytest
import requests
from aiohttp import (
    ClientError,
    ClientRequest,
    ClientResponseError,
    ClientTimeout,
    ContentTypeError,
)
from aioresponses import aioresponses
from fastapi import status
from nomad.api.exceptions import BaseNomadException, URLNotFoundNomadException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from yarl import URL

from app.core.exceptions import HTTPBadRequestException
from app.core.settings_override.registry import ReloadClassification
from app.core.settings_override.resolution import resolve_nested_field_metadata
from app.core.utils import slugify, utc_now
from app.core.utils.fields import PRESERVE_CREDENTIALS_CONTEXT
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.crud import (
    TaskHistoryLogManager,
    TaskHistoryLogStateManager,
    TaskHistoryManager,
)
from app.tasks.execution.exceptions import TaskNotStartedInExecutorError
from app.tasks.execution.executors.nomad.exceptions import (
    AllocationNotFoundError,
    JobNotFoundError,
    NomadRequestError,
)
from app.tasks.execution.executors.nomad.models import (
    _alloc_step_state,
    _alloc_task_states,
    _ANONYMIZED_STEPS,
    _CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS,
    _CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS,
    _capture_hold_step_state,
    _detect_capture_hold_ready,
    _detect_stale_skip,
    _detect_unlaunchable,
    _failed_step_reason,
    _LAUNCH_CHECK_TASK_NAME,
    _NOMAD_LOG_STREAM_CLIENT_ERROR,
    _NOMAD_LOG_STREAM_SOCK_TIMEOUT,
    _should_anonymize,
    _split_nomad_frames,
    _STALE_SKIP_TASK_NAME,
    _status_from_step_states,
    NODE_STATUS_READY,
    NOMAD_DEAD_JOB_STATUS,
    nomad_task_states_to_execution_events,
    NomadAllocStatusEnum,
    NomadEvalStatusEnum,
    NomadExecutor,
    RAW_EXEC_DRIVER,
)
from app.tasks.execution.executors.nomad.steps import (
    LAUNCH_CHECK_EXIT_CODE,
    NomadStep,
)
from app.tasks.execution.utils import gzip_compress, minify_file_content
from app.tasks.logs.line_split import WithheldLineBuffer
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    ExecutionEvent,
    FileMetadata,
    LogCaptureStatusEnum,
    Task,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
)
from app.tasks.routes import stream_task_history_logs
from app.tasks.run_result import RUN_RESULT_FILENAME

EXPECTED_ALLOC_STATUS_COUNT = 6
NOMAD_DEFAULT_TIMEOUT = 10
INITIAL_LOG_OFFSET = 50
# One started step times (stdout + stderr) when another step has StartedAt None.
EXPECTED_GET_LOGS_STREAM_CALLS_ONE_READY_STEP = 2
EXPECTED_HOLD_READS_UNTIL_RUNNING = 3
EXPECTED_HOLD_READS_MID_POLL_FAILURE = 2
# A stop reads the allocation itself before the release re-reads it.
EXPECTED_STOP_ALLOC_READS_UNTIL_RUNNING = 3
EXPECTED_STOP_ALLOC_READS_ON_DEAD_HOLD = 2
# The release budget has to hold across every supported drain tuning, since
# borrowing the drain's is what used to forfeit it.
DRAIN_SETTINGS_VARIANTS = [
    pytest.param({}, id="default-drain"),
    pytest.param({"terminal_log_drain_max_attempts": 0}, id="drain-disabled"),
    pytest.param(
        {"terminal_log_drain_max_attempts": 99, "terminal_log_drain_interval": 99},
        id="drain-inflated",
    ),
]
MOCK_LOG_STREAM_BODY_START_MONOTONIC = 1000.0
STALENESS_THRESHOLD_OVERRIDE = 300
PENDING_ALLOCATION_TIMEOUT_OVERRIDE = 60
PENDING_ALLOCATION_WITHIN_BOUND_AGE = 30
PENDING_ALLOCATION_BOUNDARY_AGE = 30
PENDING_ALLOCATION_PAST_BOUND_AGE = 120
MULTI_CHUNK_LOG_FIRST_OFFSET = 17
MULTI_CHUNK_LOG_SECOND_OFFSET = 42
EXPECTED_MULTI_CHUNK_LOG_COUNT = 2
SPLIT_FRAME_LOG_OFFSET = 99
EXPECTED_SINGLE_TASK_LOG_COUNT = 1
EMPTY_DATA_FRAME_DATA_OFFSET = 10
EMPTY_DATA_FRAME_OFFSET_ONLY = 25
RECHECK_LOG_SOCKET_READ_TIMEOUT = 2
EXPECTED_EMPTY_FRAMES_BEFORE_RECHECK = 3
RECHECKED_TASK_STATE = "dead"
ALLOCATION_CREATE_INDEX = 100
SUPERSEDED_ALLOCATION_EPOCH = 100
CURRENT_ALLOCATION_EPOCH = 200
SEED_OFFSET = 3
LEGACY_SEED_PRODUCER_OFFSET = 5
# Line-split anonymization fixtures: a 16-digit card token straddling frames.
SPLIT_TOKEN_FIRST_FRAME_OFFSET = 13  # raw EOF after "card=41111111"
SPLIT_TOKEN_LINE_EOF_OFFSET = 22  # raw EOF after the completed "card=...\n" line
WITHHELD_PARTIAL_FRAME_EOF_OFFSET = 12  # raw EOF of "ok\ncard=41"
WITHHELD_PARTIAL_RESUME_OFFSET = 5  # cursor rolled back over the withheld "card=41"
MULTIBYTE_LINE_EOF_OFFSET = 12  # raw EOF of "café\n€uro" (6 + 6 UTF-8 bytes)
MULTIBYTE_WITHHELD_BYTES = 6  # raw byte length of the withheld "€uro"
NEWLINELESS_TAIL_FRAME_EOF_OFFSET = 21  # raw EOF of "card=4111111111111111"
# Ceiling low enough that the 21-byte newline-less card line forces a flush.
FORCED_FLUSH_CEILING_BYTES = 10
CARD_LINE_WITH_TAIL_EOF_OFFSET = 26  # raw EOF of the card line plus a trailing "tail"
CARD_LINE_TAIL_WITHHELD_BYTES = 4  # raw byte length of the withheld "tail"
RECONNECT_RESUME_FRAME_EOF_OFFSET = 19  # raw EOF of the frame after a reconnect
NOMAD_MODELS_LOGGER = "app.tasks.execution.executors.nomad.models"


def _redact_card_token(text: str, _entities: set[PIIEntity]) -> str:
    """Redact a full 16-digit card token, matching only whole lines.

    Stand-in for ``anonymize_text`` that only matches the complete number, so a
    token split across chunks is redacted only once the line is reassembled.
    """
    return text.replace("4111111111111111", "[REDACTED]")


def _build_task(
    task_id: str = "my-job",
    *,
    parameterized: bool = False,
    constraints: list | None = None,
    declares_staleness_meta: bool = True,
) -> Task:
    """Build a minimal Task instance for testing.

    :param task_id: The job ID to use in the task data.
    :type task_id: str
    :param parameterized: Whether to include a ParameterizedJob field.
    :type parameterized: bool
    :param constraints: Optional constraints list.
    :type constraints: list | None
    :param declares_staleness_meta: Whether the ParameterizedJob declares the
        ``scheduled_at``/``staleness_threshold_seconds`` meta keys (matches the
        shape of the centralized templates). Only effective when
        ``parameterized=True``.
    :type declares_staleness_meta: bool
    :return: A Task instance with minimal fields.
    :rtype: Task
    """
    data = {"ID": task_id, "Constraints": constraints or []}
    if parameterized:
        parameterized_spec = {"Payload": "required"}
        if declares_staleness_meta:
            parameterized_spec["MetaOptional"] = [
                "scheduled_at",
                "staleness_threshold_seconds",
            ]
        data["ParameterizedJob"] = parameterized_spec
    return Task(
        id=1,
        name="test-task",
        data=data,
        backend="nomad",
        owner="any",
    )


def _build_queue_item(
    task: Task | None = None,
    tracking: dict | None = None,
    meta: dict | None = None,
    payload: str | None = None,
    status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.RUNNING,
) -> TaskHistory:
    """Build a minimal TaskHistory instance for testing.

    :param task: The task to associate with the history.
    :type task: Task | None
    :param tracking: Tracking dictionary.
    :type tracking: dict | None
    :param meta: Metadata dictionary.
    :type meta: dict | None
    :param payload: Payload string.
    :type payload: str | None
    :param status: The status of the task history.
    :type status: TaskHistoryStatusEnum
    :return: A TaskHistory instance.
    :rtype: TaskHistory
    """
    task = task or _build_task()
    return TaskHistory(
        id=10,
        task_id=task.id,
        task=task,
        execution_request=TaskExecutionRequest(
            task=task.name,
            target="node-1",
            meta=meta or {"target": "node-1"},
            payload=payload,
            tracking=tracking
            or {"allocation_id": None, "evaluation_id": "eval-1", "job_id": "job-1"},
        ),
        status=status,
        anonymize_mask=0,
    )


#: A per-call Nomad timeout distinct from both the field default and the
#: shared session's, so a test cannot pass by picking up either.
CONFIGURED_NOMAD_TIMEOUT = 7

#: What an unstubbed ``GET /v1/job/{id}`` answers: a job that is alive and not
#: stopped, so a test reaching it incidentally sees nothing terminal.
_DEFAULT_NOMAD_JOB = {"ID": "job-1", "Status": "running", "Stop": False}

#: Nomad stubs installed by :func:`_build_executor`, undone after each test by
#: :func:`_stop_nomad_api_stubs`. Tracked here rather than through
#: ``patch.stopall`` so stopping them cannot reach a patch someone else started.
_ACTIVE_NOMAD_STUBS: list[Any] = []


@pytest.fixture(autouse=True)
def _stop_nomad_api_stubs() -> Generator[None, None, None]:
    """Undo any Nomad HTTP stub a test installed through :func:`_build_executor`."""
    yield
    while _ACTIVE_NOMAD_STUBS:
        _ACTIVE_NOMAD_STUBS.pop().stop()


def _build_executor(
    *,
    nomad_job: Any = None,
    nomad_nodes: Any = None,
    nomad_register: Any = None,
    nomad_dispatch: Any = None,
    stub_nomad: bool = True,
    **kwargs,
) -> NomadExecutor:
    """Build a NomadExecutor with default test settings.

    The ``nomad_*`` arguments stub the five dispatch-path calls that moved off
    python-nomad onto the aiohttp session, for the tests whose subject is what
    the executor does with Nomad's answers rather than the calls themselves.
    Where a test asserts on the calls, use :func:`_stub_nomad_api` instead.

    :param nomad_job: Answer for ``GET /v1/job/{id}``.
    :param nomad_nodes: Answer for ``GET /v1/nodes``.
    :param nomad_register: Answer for ``POST /v1/job/{id}``.
    :param nomad_dispatch: Answer for ``POST /v1/job/{id}/dispatch``.
    :param stub_nomad: Pass ``False`` to leave the Nomad calls unstubbed, for a
        test whose subject is the HTTP request the executor builds.
    :return: A NomadExecutor instance.
    :rtype: NomadExecutor
    """
    defaults = {
        "endpoint": "http://localhost:4646",
        "verify_ssl": False,
    }
    defaults.update(kwargs)
    if (nomad_job, nomad_nodes, nomad_register, nomad_dispatch) != (None,) * 4:
        _stub_nomad_calls(
            job=nomad_job,
            nodes=nomad_nodes,
            register=nomad_register,
            dispatch=nomad_dispatch,
        )
    elif stub_nomad and not _ACTIVE_NOMAD_STUBS:
        # No stub asked for and none already installed by a ``_backend`` helper.
        # Install a benign one anyway: the ported calls now go out over the
        # aiohttp session, which is None until the executor is entered as an async
        # context manager, so a test that reaches one only incidentally would fail
        # on the session rather than on its own subject.
        _stub_nomad_calls()
    return NomadExecutor(**defaults)


def _nomad_error(status_code: int) -> NomadRequestError:
    """Build the error ``nomad_json`` raises for ``status_code``.

    Deliberately the executor's own error rather than the underlying
    ``aiohttp.ClientResponseError``: ``nomad_json`` converts every failure so
    that it stays a ``BaseNomadException``, which the Tasks exception handler and
    the periodic-dispatch alert both depend on.

    :param status_code: The HTTP status Nomad answered with.
    :return: The matching error.
    """
    return NomadRequestError(f"Nomad answered {status_code}", status_code=status_code)


def _serve(canned: Any, fallback: Any, *args: Any) -> Any:
    """Resolve one stubbed Nomad answer, raising it when it is an exception.

    :param canned: The configured answer: a body, a callable, an exception, or ``None``.
    :param fallback: What to answer when ``canned`` is ``None``.
    :param args: Passed to ``canned`` when it is callable.
    :return: The body to answer with.
    """
    if canned is None:
        return fallback
    if hasattr(canned, "__next__"):
        # An explicit iterator answers successive calls in turn, the way a
        # ``side_effect`` list did. A plain list is left alone: ``nodes`` is one.
        return _serve(next(canned), fallback, *args)
    if isinstance(canned, BaseException):
        raise canned
    if callable(canned):
        return canned(*args)
    return canned


def _nomad_router(
    *,
    job: Any = None,
    nodes: Any = None,
    register: Any = None,
    dispatch: Any = None,
) -> tuple[Callable[..., Any], list[tuple[str, str, dict[str, Any]]]]:
    """Build a stand-in for :meth:`NomadExecutor.nomad_json` and its call log.

    The dispatch path's five Nomad calls moved off the synchronous python-nomad
    client onto the inherited aiohttp session, so a test can no longer set
    ``mock_backend.job.<call>.return_value``. This routes by method and path
    instead, and each argument takes a body, a callable receiving the job id, or
    an exception to raise.

    Deliberately a stand-in for ``nomad_json`` rather than for ``_request``:
    most of these tests are about what the executor does with Nomad's answers,
    and routing them through a mock HTTP response would only restate aiohttp. The
    wire format those calls actually put on the socket - method, path and body -
    is pinned separately, and once, by
    :class:`TestPortedNomadCallsUseTheDocumentedEndpoints`.

    :param job: Answer for ``GET /v1/job/{id}``.
    :param nodes: Answer for ``GET /v1/nodes``.
    :param register: Answer for ``POST /v1/job/{id}``.
    :param dispatch: Answer for ``POST /v1/job/{id}/dispatch``.
    :return: The replacement coroutine function, and the list its calls land in.
    """
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def nomad_json(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        if method == "GET" and path == "/v1/nodes":
            return _serve(nodes, [])
        if method == "GET" and path.startswith("/v1/job/"):
            return _serve(job, _DEFAULT_NOMAD_JOB, path.removeprefix("/v1/job/"))
        if method == "POST" and path.endswith("/dispatch"):
            return _serve(dispatch, {"EvalID": "eval-1"})
        if method == "POST" and path.startswith("/v1/job/"):
            return _serve(register, {"EvalID": "eval-1"})
        raise AssertionError(f"unrouted Nomad call: {method} {path}")

    return nomad_json, calls


def _stub_nomad_calls(**answers: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Stub the ported Nomad calls for every executor built during this test.

    Patched on :class:`NomadExecutor` rather than on an instance so the
    ``_backend`` helpers, which wire Nomad up before the executor exists, can
    install one too. Undone by :func:`_stop_nomad_api_stubs`.

    :param answers: As :func:`_nomad_router` takes them.
    :return: The list the recorded calls land in.
    """
    router, calls = _nomad_router(**answers)

    async def nomad_json(
        _self: NomadExecutor, method: str, path: str, **kwargs: Any
    ) -> Any:
        return await router(method, path, **kwargs)

    patcher = patch.object(NomadExecutor, "nomad_json", nomad_json)
    patcher.start()
    _ACTIVE_NOMAD_STUBS.append(patcher)
    return calls


@contextmanager
def _stub_nomad_api(
    _executor: NomadExecutor,
    *,
    job: Any = None,
    nodes: Any = None,
    register: Any = None,
    dispatch: Any = None,
) -> Generator[list[tuple[str, str, dict[str, Any]]], None, None]:
    """Answer the executor's ported Nomad calls, yielding the calls it recorded.

    See :func:`_nomad_router` for the routing and for what each argument accepts.
    Use this where the test asserts on the calls; where the stub is only setup,
    ``_build_executor(nomad_job=...)`` keeps it out of the test body.

    Patched on the class rather than the instance: ``nomad_json`` is public, and
    pydantic refuses an instance attribute that is not a field. Scoping is not
    lost in practice - a test builds the one executor it exercises - and the
    un-entered path runs its call on a private executor the test never sees.

    :param _executor: The executor the caller is exercising, kept for call-site
        clarity about which one the stub is for.
    :param job: Answer for ``GET /v1/job/{id}``.
    :param nodes: Answer for ``GET /v1/nodes``.
    :param register: Answer for ``POST /v1/job/{id}``.
    :param dispatch: Answer for ``POST /v1/job/{id}/dispatch``.
    :return: The recorded ``(method, path, kwargs)`` of every call made.
    """
    router, calls = _nomad_router(
        job=job, nodes=nodes, register=register, dispatch=dispatch
    )
    with patch.object(NomadExecutor, "nomad_json", side_effect=router):
        yield calls


def _dispatch_body(calls: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    """Return the JSON body of the one dispatch call recorded in ``calls``.

    :param calls: The calls a :func:`_stub_nomad_api` block recorded.
    :return: The ``Payload``/``Meta``/``IdPrefixTemplate`` body that was sent.
    """
    bodies = [kwargs["json"] for _, path, kwargs in calls if path.endswith("/dispatch")]
    assert len(bodies) == 1, f"expected exactly one dispatch call, got {len(bodies)}"
    return bodies[0]


class TestAnonymizedStepClassification:
    """Assert the redaction guard follows the NomadStep anonymization map."""

    def test_anonymized_steps_stay_run_script_and_step1(self) -> None:
        """Assert the effective anonymized set is exactly run-script and step1."""
        assert frozenset({"run-script", "step1"}) == _ANONYMIZED_STEPS

    @pytest.mark.parametrize("step", [NomadStep.RUN_SCRIPT, NomadStep.STEP1])
    def test_should_anonymize_fires_for_anonymized_steps(self, step: NomadStep) -> None:
        """Assert the guard fires for every step classified as anonymized."""
        assert _should_anonymize(step, {PIIEntity.PERSON}) is True

    @pytest.mark.parametrize(
        "step",
        [NomadStep.PREPARE_ENV, NomadStep.CLEAN_UP, NomadStep.CHECK_STALENESS],
    )
    def test_should_anonymize_stays_off_for_unanonymized_steps(
        self, step: NomadStep
    ) -> None:
        """Assert an unanonymized step stays unredacted even with entities requested.

        Pins the behaviour the classification exists to drive. A set listing only
        the anonymized steps leaves this arm unstated, so nothing fails when a
        newly-added step silently joins the wrong side.
        """
        assert _should_anonymize(step, {PIIEntity.PERSON}) is False

    @pytest.mark.parametrize("step", list(NomadStep))
    def test_no_step_is_anonymized_without_entities(self, step: NomadStep) -> None:
        """Assert no step is redacted when no PII entities were requested."""
        assert _should_anonymize(step, None) is False
        assert _should_anonymize(step, set()) is False

    def test_stale_skip_task_name_is_nomad_step(self) -> None:
        """Assert the stale-skip sentinel is NomadStep.CHECK_STALENESS."""
        assert _STALE_SKIP_TASK_NAME is NomadStep.CHECK_STALENESS
        assert _STALE_SKIP_TASK_NAME == "check-staleness"

    def test_launch_check_task_name_is_nomad_step(self) -> None:
        """Assert the launch-check sentinel is NomadStep.CHECK_LAUNCHABLE."""
        assert _LAUNCH_CHECK_TASK_NAME is NomadStep.CHECK_LAUNCHABLE
        assert _LAUNCH_CHECK_TASK_NAME == "check-launchable"


class TestNomadExecutorTlsClassification:
    """Assert the inherited TLS leaves classify as advanced + HOT via the overlay.

    These fields are inherited (frozen) from ``BaseRemoteAPI`` and marked only
    through ``NomadExecutor.INHERITED_MARKERS`` -- not by redeclaration.
    """

    @pytest.mark.parametrize(
        "leaf", ["VERIFY_SSL", "SSL_CAFILE", "SSL_KEYFILE", "SSL_CERTFILE"]
    )
    def test_tls_leaves_are_advanced_and_hot(self, leaf: str) -> None:
        """Assert each TLS leaf classifies ``advanced`` + HOT through the settings API."""
        meta = resolve_nested_field_metadata(TasksSettings, f"NOMAD__{leaf}")
        assert meta is not None
        assert meta.is_advanced is True
        assert meta.reload is ReloadClassification.HOT

    def test_endpoint_stays_unmarked(self) -> None:
        """Assert the inherited ``endpoint`` has no overlay entry, so it stays non-advanced."""
        meta = resolve_nested_field_metadata(TasksSettings, "NOMAD__ENDPOINT")
        assert meta is not None
        assert meta.is_advanced is False

    def test_tls_leaves_remain_frozen(self) -> None:
        """Assert dropping the redeclarations keeps ``frozen=True`` inherited from the base."""
        executor = _build_executor()
        with pytest.raises(ValidationError):
            executor.verify_ssl = True

    def test_hash_is_value_stable(self) -> None:
        """Assert two executors with identical config hash equal (identity hash unchanged)."""
        assert hash(_build_executor()) == hash(_build_executor())


class TestNomadAllocStatusEnum:
    """Test NomadAllocStatusEnum values."""

    def test_alloc_status_enum_values(self):
        """Assert all enum members have expected string values."""
        assert NomadAllocStatusEnum.PENDING == "pending"
        assert NomadAllocStatusEnum.RUNNING == "running"
        assert NomadAllocStatusEnum.COMPLETE == "complete"
        assert NomadAllocStatusEnum.FAILED == "failed"
        assert NomadAllocStatusEnum.LOST == "lost"
        assert NomadAllocStatusEnum.UNKNOWN == "unknown"

    def test_alloc_status_enum_count(self):
        """Assert there are exactly 6 enum members."""
        assert len(NomadAllocStatusEnum) == EXPECTED_ALLOC_STATUS_COUNT


class TestTimestampToDatetime:
    """Test NomadExecutor.timestamp_to_datetime."""

    def test_timestamp_to_datetime(self):
        """Assert nanosecond timestamp converts to correct UTC datetime."""
        ns = 1_700_000_000_000_000_000
        result = NomadExecutor.timestamp_to_datetime(ns)
        assert isinstance(result, datetime)
        assert result.tzinfo == UTC
        expected = datetime.fromtimestamp(1_700_000_000, UTC)
        assert result == expected

    def test_timestamp_to_datetime_zero(self):
        """Assert zero nanoseconds converts to epoch."""
        result = NomadExecutor.timestamp_to_datetime(0)
        assert result == datetime.fromtimestamp(0, UTC)


class TestGetTaskHistoryStatusFromAllocStatus:
    """Test NomadExecutor.get_task_history_status_from_alloc_status."""

    def test_complete_not_stopped(self):
        """Assert COMPLETE without stop maps to SUCCESS."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.COMPLETE,
        )
        assert result == TaskHistoryStatusEnum.SUCCESS

    def test_complete_stopped(self):
        """Assert COMPLETE with stopped maps to STOPPED."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.COMPLETE,
            stopped=True,
        )
        assert result == TaskHistoryStatusEnum.STOPPED

    def test_failed(self):
        """Assert FAILED maps to FAILED."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.FAILED,
        )
        assert result == TaskHistoryStatusEnum.FAILED

    def test_lost(self):
        """Assert LOST maps to LOST."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.LOST,
        )
        assert result == TaskHistoryStatusEnum.LOST

    def test_unknown(self):
        """Assert UNKNOWN maps to LOST."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.UNKNOWN,
        )
        assert result == TaskHistoryStatusEnum.LOST

    def test_running_returns_default(self):
        """Assert RUNNING returns default value."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.RUNNING,
        )
        assert result is None

    def test_running_returns_custom_default(self):
        """Assert RUNNING returns the provided default."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.RUNNING,
            default=TaskHistoryStatusEnum.RUNNING,
        )
        assert result == TaskHistoryStatusEnum.RUNNING

    def test_pending_returns_default(self):
        """Assert PENDING returns default value."""
        result = NomadExecutor.get_task_history_status_from_alloc_status(
            NomadAllocStatusEnum.PENDING,
        )
        assert result is None


class TestPrepareTask:
    """Test NomadExecutor.prepare_task."""

    def test_prepare_task_sets_id_suffix(self):
        """Assert prepare_task appends target slug to task data ID."""
        task = _build_task(task_id="base-job")
        queue_item = _build_queue_item(task=task)
        result = NomadExecutor.prepare_task(queue_item)
        assert result.data is not None
        assert result.data["ID"] == f"base-job-{slugify('node-1')}"

    def test_prepare_task_with_explicit_task(self):
        """Assert prepare_task uses explicit task argument over queue_item.task."""
        queue_item = _build_queue_item()
        explicit_task = _build_task(task_id="explicit-job")
        result = NomadExecutor.prepare_task(queue_item, task=explicit_task)
        assert result.data is not None
        assert result.data["ID"].startswith("explicit-job-")

    def test_prepare_task_meta_substitution(self):
        """Assert prepare_task substitutes meta variables in constraints."""
        constraints = [{"Operand": "${NOMAD_META_target}"}]
        task = _build_task(task_id="meta-job", constraints=constraints)
        meta = {"target": "node-1", "dc": "dc1"}
        queue_item = _build_queue_item(task=task, meta=meta)
        result = NomadExecutor.prepare_task(queue_item)
        assert result.data is not None
        assert result.data["Constraints"][0]["Operand"] == "node-1"

    def test_prepare_task_no_meta(self):
        """Assert prepare_task works when meta is None."""
        task = _build_task(task_id="no-meta-job")
        queue_item = _build_queue_item(task=task, meta=None)
        queue_item.execution_request.meta = None
        result = NomadExecutor.prepare_task(queue_item)
        assert result.data is not None
        assert result.data["ID"] == f"no-meta-job-{slugify('node-1')}"


class TestBackendProperty:
    """Test NomadExecutor.backend cached property."""

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_backend_creates_nomad_client(self, mock_nomad_cls):
        """Assert backend property creates a Nomad client with correct args."""
        executor = _build_executor()
        _ = executor.backend
        mock_nomad_cls.assert_called_once()
        call_kwargs = mock_nomad_cls.call_args[1]
        assert call_kwargs["address"] == "http://localhost:4646"
        assert call_kwargs["secure"] is False
        assert call_kwargs["timeout"] == NOMAD_DEFAULT_TIMEOUT
        assert call_kwargs["verify"] is False
        assert call_kwargs["cert"] == ()

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_backend_strips_trailing_slash_from_endpoint(self, mock_nomad_cls):
        """Strip the trailing slash off a host-only Nomad endpoint.

        ``HttpUrl`` normalises a host-only URL by appending ``/``. python-nomad
        joins paths as ``f"{address}/v1/..."``, so a trailing slash yields
        ``//v1/nodes``; Nomad 307-redirects that to an HTML body and python-nomad
        (no redirect following) calls ``.json()`` on it, raising
        ``Expecting value: line 1 column 1 (char 0)``. The executor must strip the
        slash so the request path stays single-slashed.
        """
        executor = _build_executor(endpoint="https://nomad.example:4646")
        _ = executor.backend
        address = mock_nomad_cls.call_args[1]["address"]
        assert address == "https://nomad.example:4646"
        assert not address.endswith("/")

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_backend_ssl_config_certfile_only(self, mock_nomad_cls):
        """Assert backend passes single-element cert tuple when only certfile set."""
        executor = _build_executor(verify_ssl=True, secure=True)
        object.__setattr__(executor, "ssl_certfile", "/path/cert.pem")
        object.__setattr__(executor, "ssl_keyfile", None)
        _ = executor.backend
        call_kwargs = mock_nomad_cls.call_args[1]
        assert call_kwargs["cert"] == ("/path/cert.pem",)

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_backend_ssl_config_cert_and_key(self, mock_nomad_cls):
        """Assert backend passes cert+key tuple when both are set."""
        executor = _build_executor(verify_ssl=True, secure=True)
        object.__setattr__(executor, "ssl_certfile", "/path/cert.pem")
        object.__setattr__(executor, "ssl_keyfile", "/path/key.pem")
        _ = executor.backend
        call_kwargs = mock_nomad_cls.call_args[1]
        assert call_kwargs["cert"] == ("/path/cert.pem", "/path/key.pem")

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_backend_verify_with_cafile(self, mock_nomad_cls):
        """Assert backend passes cafile as verify when ssl is fully configured."""
        executor = _build_executor(verify_ssl=True, secure=True)
        object.__setattr__(executor, "ssl_cafile", "/path/ca.pem")
        _ = executor.backend
        call_kwargs = mock_nomad_cls.call_args[1]
        assert call_kwargs["verify"] == "/path/ca.pem"


class TestNomadExecutorApiKey:
    """Cover the configured API key on both executor request paths.

    The synchronous python-nomad client and the asynchronous aiohttp session
    each snapshot their headers once per session, so the credential is model
    state rather than a per-call context.
    """

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_the_sync_session_carries_the_bearer_header(self, mock_nomad_cls) -> None:
        """Assert ``backend`` hands python-nomad a session carrying the header."""
        executor = _build_executor(api_key="glsa_supersecret")
        _ = executor.backend
        session = mock_nomad_cls.call_args[1]["session"]
        assert session.headers["Authorization"] == "Bearer glsa_supersecret"

    @pytest.mark.asyncio
    async def test_the_async_session_carries_the_bearer_header(self) -> None:
        """Assert the entered aiohttp session defaults to the bearer header."""
        executor = _build_executor(api_key="glsa_supersecret")
        async with executor:
            assert executor._session.headers["Authorization"] == (
                "Bearer glsa_supersecret"
            )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_exit_closes_the_sync_session_and_drops_the_backend(
        self, mock_nomad_cls
    ) -> None:
        """Assert retirement releases the session the executor owns."""
        executor = _build_executor(api_key="glsa_supersecret")
        with patch.object(requests.Session, "close", autospec=True) as mock_close:
            async with executor:
                _ = executor.backend
                session = mock_nomad_cls.call_args[1]["session"]
                mock_close.assert_not_called()

            mock_close.assert_called_once_with(session)

        assert executor._sync_session is None
        assert "backend" not in executor.__dict__

        rebuilt_from = mock_nomad_cls.call_count
        async with executor:
            _ = executor.backend
            assert mock_nomad_cls.call_count == rebuilt_from + 1
            assert mock_nomad_cls.call_args[1]["session"] is not session

    def test_the_configured_scheme_is_honoured(self) -> None:
        """Assert ``auth_scheme`` selects the scheme the header announces."""
        executor = _build_executor(api_key="glsa_supersecret", auth_scheme="Basic")
        assert executor.headers["Authorization"] == "Basic glsa_supersecret"

    def test_no_key_emits_no_header(self) -> None:
        """Assert an unconfigured key leaves the header set byte-identical to today."""
        assert _build_executor().headers == {}

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_no_key_leaves_the_sync_session_unauthenticated(
        self, mock_nomad_cls
    ) -> None:
        """Assert the session handed to python-nomad carries no authorization header."""
        _ = _build_executor().backend
        session = mock_nomad_cls.call_args[1]["session"]
        assert "Authorization" not in session.headers

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_userinfo_alone_still_authenticates(self, mock_nomad_cls) -> None:
        """Assert an endpoint credential keeps working when no key is configured."""
        executor = _build_executor(endpoint="http://admin:hunter2@localhost:4646")
        _ = executor.backend
        assert "hunter2" in mock_nomad_cls.call_args[1]["address"]
        assert "hunter2" in executor.base_url

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_the_key_wins_over_userinfo_on_the_sync_path(self, mock_nomad_cls) -> None:
        """Assert the address loses its userinfo so the header is the credential sent."""
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646",
            api_key="glsa_supersecret",
        )
        _ = executor.backend
        call_kwargs = mock_nomad_cls.call_args[1]
        assert call_kwargs["address"] == "http://localhost:4646"
        assert call_kwargs["session"].headers["Authorization"] == (
            "Bearer glsa_supersecret"
        )

    @pytest.mark.asyncio
    async def test_the_key_wins_over_userinfo_on_the_async_path(self) -> None:
        """Assert ``base_url`` loses its userinfo so aiohttp cannot derive basic auth."""
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646",
            api_key="glsa_supersecret",
        )
        assert executor.base_url == "http://localhost:4646"
        async with executor:
            assert executor._session.headers["Authorization"] == (
                "Bearer glsa_supersecret"
            )

    @pytest.mark.asyncio
    async def test_the_async_request_url_yields_the_bearer(self) -> None:
        """Assert the header survives on the URL aiohttp actually requests.

        ``aiohttp`` derives basic auth in :class:`~aiohttp.ClientRequest` from the
        *joined* per-request URL, not from ``base_url``, and lets it overwrite an
        explicit header. Asserting on ``base_url`` alone would stay green if
        userinfo were ever reintroduced during the join.
        """
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646",
            api_key="glsa_supersecret",
        )
        async with executor:
            request = ClientRequest(
                "GET",
                URL(executor.base_url + executor.prepare_path("/v1/jobs")),
                headers=executor._session.headers,
            )
        assert request.headers["Authorization"] == "Bearer glsa_supersecret"

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_the_sync_request_url_yields_the_bearer(self, mock_nomad_cls) -> None:
        """Assert the header survives once ``requests`` has prepared the request.

        ``requests`` applies URL userinfo in ``Session.prepare_request``, after
        the session default header is set, so the prepared request is the only
        place the precedence is observable.
        """
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646",
            api_key="glsa_supersecret",
        )
        _ = executor.backend
        call_kwargs = mock_nomad_cls.call_args[1]
        session = call_kwargs["session"]
        prepared = session.prepare_request(
            requests.Request("GET", f"{call_kwargs['address']}/v1/jobs")
        )
        assert prepared.headers["Authorization"] == "Bearer glsa_supersecret"

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_an_empty_key_counts_as_unset_on_both_paths(self, mock_nomad_cls) -> None:
        """Assert a blank mounted secret falls through to whatever the URL carries."""
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646", api_key=""
        )
        _ = executor.backend
        assert executor.headers == {}
        assert "Authorization" not in mock_nomad_cls.call_args[1]["session"].headers
        assert "hunter2" in mock_nomad_cls.call_args[1]["address"]
        assert "hunter2" in executor.base_url

    def test_the_json_dump_masks_the_endpoint_password_without_a_key(self) -> None:
        """Mask the password the base URL still carries when no key is set."""
        executor = _build_executor(endpoint="http://admin:hunter2@localhost:4646")
        assert "hunter2" in executor.base_url
        assert "hunter2" not in executor.model_dump_json()
        assert executor.model_dump(mode="json")["base_url"] == (
            "http://admin:****@localhost:4646"
        )

    def test_the_preserve_context_dump_keeps_the_endpoint_password(self) -> None:
        """Keep the real value for the config fingerprint, which compares it."""
        executor = _build_executor(endpoint="http://admin:hunter2@localhost:4646")
        dumped = executor.model_dump(mode="json", context=PRESERVE_CREDENTIALS_CONTEXT)
        assert dumped["base_url"] == "http://admin:hunter2@localhost:4646"

    def test_a_configured_key_leaves_no_userinfo_to_mask(self) -> None:
        """Strip the userinfo entirely once a key supersedes it."""
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646", api_key="glsa_supersecret"
        )
        assert executor.model_dump(mode="json")["base_url"] == "http://localhost:4646"

    def test_an_empty_key_still_masks_the_endpoint_password(self) -> None:
        """Treat a blank key as unset for redaction as well as for the header."""
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646", api_key=""
        )
        assert executor.model_dump(mode="json")["base_url"] == (
            "http://admin:****@localhost:4646"
        )

    @pytest.mark.parametrize(
        "scheme", ["", " ", "Bearer x\r\nX-Injected: yes", "Bea rer", "Bearer\x00"]
    )
    def test_a_non_token_auth_scheme_is_rejected(self, scheme: str) -> None:
        """Refuse a scheme no ``Authorization`` header value can carry.

        Both HTTP clients raise at send time on such a value, so accepting it
        here would trade a settings-validation error for every later Nomad
        request failing.
        """
        with pytest.raises(ValidationError):
            _build_executor(api_key="glsa_supersecret", auth_scheme=scheme)

    @pytest.mark.parametrize(
        "key",
        [
            "glsa_tok\n",
            "glsa\r\nX-Injected: yes",
            "glsa\x00tok",
            "glsa\x0btok",
            "a\x7f",
        ],
    )
    def test_a_key_one_client_refuses_to_send_is_rejected(self, key: str) -> None:
        """Refuse a credential the HTTP clients will not put on the wire.

        The scheme is constrained for the same reason; the key is the half an
        operator pastes, so a trailing newline is the ordinary way one arrives.
        """
        with pytest.raises(ValidationError):
            _build_executor(api_key=key)

    @pytest.mark.parametrize(
        "key", ["glsa_tok", "eyJhbGci.eyJzdWIi.Sf-Kx==", "a b", "tok+/=~", "glsa\ttok"]
    )
    def test_a_key_both_clients_will_send_is_accepted(self, key: str) -> None:
        """Accept every credential shape both clients put on the wire.

        A key is not held to RFC 7230's ``token``: base64 padding, spaces and
        ``HTAB`` are all sent unchanged by both, so none of them is rejected.
        """
        assert _build_executor(api_key=key).headers["Authorization"] == f"Bearer {key}"

    @pytest.mark.parametrize("scheme", ["Bearer", "Basic", "Token", "X-Custom.v1"])
    def test_a_token_auth_scheme_is_accepted(self, scheme: str) -> None:
        """Accept every scheme shape RFC 7230's ``token`` production allows."""
        executor = _build_executor(api_key="glsa_supersecret", auth_scheme=scheme)
        assert executor.headers["Authorization"] == f"{scheme} glsa_supersecret"

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_the_address_never_carries_a_credential(self, mock_nomad_cls) -> None:
        """Assert neither credential reaches the address python-nomad embeds in URLs.

        ``BaseNomadException`` renders the response body only, so keeping both
        credentials out of the address is what keeps the synchronous path's
        errors and request URLs free of them.
        """
        executor = _build_executor(
            endpoint="http://admin:hunter2@localhost:4646",
            api_key="glsa_supersecret",
        )
        _ = executor.backend
        address = mock_nomad_cls.call_args[1]["address"]
        assert address == "http://localhost:4646"
        assert "glsa_supersecret" not in address
        assert "hunter2" not in address

    @pytest.mark.asyncio
    async def test_the_request_debug_log_withholds_the_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert the per-request debug line never renders the configured key."""
        executor = _build_executor(api_key="glsa_supersecret")
        async with executor:
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=MagicMock())
            context.__aexit__ = AsyncMock(return_value=None)
            executor._session.request = MagicMock(return_value=context)
            with caplog.at_level(logging.DEBUG, logger=executor.logger.name):
                async with executor._request("GET", "/v1/jobs"):
                    pass
        assert "Sending GET request" in caplog.text
        assert "glsa_supersecret" not in caplog.text


class TestRegisterJob:
    """Test NomadExecutor.register_job."""

    @pytest.mark.asyncio
    async def test_register_job_posts_the_spec_and_returns_status(self) -> None:
        """Assert register_job posts the job spec and returns Nomad's status."""
        executor = _build_executor()
        task = _build_task(task_id="reg-job")

        with _stub_nomad_api(executor, register={"EvalID": "eval-1"}) as calls:
            result = await executor.register_job(task)

        assert calls == [("POST", "/v1/job/reg-job", {"json": {"Job": task.data}})]
        assert result == {"EvalID": "eval-1"}

    @pytest.mark.asyncio
    async def test_register_job_empty_status_raises(self) -> None:
        """Assert register_job raises ValueError when backend returns empty status."""
        executor = _build_executor()
        task = _build_task()

        with (
            _stub_nomad_api(executor, register={}),
            pytest.raises(ValueError, match="job status could not be determined"),
        ):
            await executor.register_job(task)


class TestDispatchJob:
    """Test NomadExecutor.dispatch_job."""

    @pytest.mark.asyncio
    async def test_dispatch_job_with_payload(self) -> None:
        """Assert dispatch_job encodes and dispatches payload correctly."""
        executor = _build_executor()
        task = _build_task(task_id="dispatch-job", parameterized=True)
        queue_item = _build_queue_item(
            task=task,
            payload="SELECT 1;",
            meta={"target": "node-1", "_job_id_prefix": "custom"},
        )

        with _stub_nomad_api(
            executor,
            dispatch={"DispatchedJobID": "dispatched-1", "EvalID": "eval-2"},
        ) as calls:
            result = await executor.dispatch_job(queue_item, task)

        assert result["DispatchedJobID"] == "dispatched-1"
        method, path, kwargs = calls[0]
        assert (method, path) == ("POST", "/v1/job/dispatch-job/dispatch")
        body = kwargs["json"]
        assert body["Payload"] is not None
        assert body["Meta"]["target"] == "node-1"
        assert "_job_id_prefix" not in body["Meta"]
        assert "staleness_threshold_seconds" in body["Meta"]

    @pytest.mark.asyncio
    async def test_dispatch_job_no_payload(self) -> None:
        """Assert dispatch_job sends None payload when not provided."""
        executor = _build_executor()
        task = _build_task(task_id="no-payload-job", parameterized=True)
        queue_item = _build_queue_item(task=task, payload=None)

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        assert _dispatch_body(calls)["Payload"] is None

    @pytest.mark.asyncio
    async def test_dispatch_job_empty_status_raises(self) -> None:
        """Assert dispatch_job raises ValueError when status is empty."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        queue_item = _build_queue_item(task=task)

        with (
            _stub_nomad_api(executor, dispatch={}),
            pytest.raises(ValueError, match="job status could not be determined"),
        ):
            await executor.dispatch_job(queue_item, task)

    @pytest.mark.asyncio
    async def test_dispatch_job_payload_is_base64_gzip(self) -> None:
        """Assert payload is gzip-compressed and base64-encoded."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        raw_payload = "SELECT 1;"
        queue_item = _build_queue_item(task=task, payload=raw_payload)

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        expected = b2a_base64(gzip_compress(minify_file_content(raw_payload))).decode(
            "utf-8"
        )
        assert _dispatch_body(calls)["Payload"] == expected

    @pytest.mark.asyncio
    async def test_dispatch_job_custom_prefix(self) -> None:
        """Assert dispatch_job uses custom job_id_prefix from meta."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        queue_item = _build_queue_item(
            task=task,
            meta={"target": "n1", "_job_id_prefix": "prefix"},
        )

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        expected_prefix = f"{slugify(task.name)}-{task.id}-{slugify('prefix')}"
        assert _dispatch_body(calls)["IdPrefixTemplate"] == expected_prefix

    @pytest.mark.asyncio
    async def test_dispatch_job_injects_threshold_from_settings(self) -> None:
        """Assert dispatch_job injects the configured staleness threshold."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        queue_item = _build_queue_item(task=task, meta={"target": "n"})

        original = tasks_settings.STALENESS_THRESHOLD_SECONDS
        tasks_settings.STALENESS_THRESHOLD_SECONDS = STALENESS_THRESHOLD_OVERRIDE
        try:
            with _stub_nomad_api(executor) as calls:
                await executor.dispatch_job(queue_item, task)
        finally:
            tasks_settings.STALENESS_THRESHOLD_SECONDS = original

        meta = _dispatch_body(calls)["Meta"]
        assert meta["staleness_threshold_seconds"] == str(STALENESS_THRESHOLD_OVERRIDE)

    @pytest.mark.asyncio
    async def test_dispatch_job_strips_underscore_meta_but_preserves_staleness(
        self,
    ) -> None:
        """Assert underscore keys are stripped while staleness meta is injected."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        queue_item = _build_queue_item(
            task=task,
            meta={
                "target": "n",
                "_chain_task_names": ["next"],
            },
        )

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        meta = _dispatch_body(calls)["Meta"]
        assert "_chain_task_names" not in meta
        assert "scheduled_at" in meta
        assert isinstance(meta["scheduled_at"], str)
        assert "staleness_threshold_seconds" in meta
        assert isinstance(meta["staleness_threshold_seconds"], str)

    @pytest.mark.asyncio
    async def test_dispatch_job_skips_staleness_meta_when_job_does_not_declare_it(
        self,
    ) -> None:
        """Assert staleness meta is NOT injected into jobs that don't declare it.

        Custom user-defined parameterized jobs that haven't been updated to
        include the staleness meta keys would otherwise be rejected by Nomad,
        so ``dispatch_job`` must preserve backward compatibility by only
        injecting the staleness meta when the job spec declares it.
        """
        executor = _build_executor()
        task = _build_task(parameterized=True, declares_staleness_meta=False)
        queue_item = _build_queue_item(task=task, meta={"target": "n"})

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        meta = _dispatch_body(calls)["Meta"]
        assert "scheduled_at" not in meta
        assert "staleness_threshold_seconds" not in meta

    @pytest.mark.asyncio
    async def test_dispatch_job_scheduled_at_uses_eta_when_set(self) -> None:
        """Assert ``scheduled_at`` derives from ``eta`` when the ETA is set."""
        executor = _build_executor()
        task = _build_task(parameterized=True)
        queue_item = _build_queue_item(task=task, meta={"target": "n"})
        eta = datetime(2030, 1, 1, tzinfo=UTC)
        queue_item.execution_request.eta = eta

        with _stub_nomad_api(executor) as calls:
            await executor.dispatch_job(queue_item, task)

        meta = _dispatch_body(calls)["Meta"]
        assert meta["scheduled_at"] == str(int(eta.timestamp()))


class TestAllocStepState:
    """Exercise the module-level ``_alloc_step_state`` helper."""

    def test_returns_state_when_present(self):
        """Assert the step's state is returned untouched when well-formed."""
        state = {"State": "running", "StartedAt": "1"}
        assert _alloc_step_state({"TaskStates": {"step1": state}}, "step1") is state

    @pytest.mark.parametrize(
        "alloc",
        [
            {"ID": "alloc-1"},
            {"ID": "alloc-1", "TaskStates": None},
            {"ID": "alloc-1", "TaskStates": {}},
            {"ID": "alloc-1", "TaskStates": {"other-step": {"State": "running"}}},
            {"ID": "alloc-1", "TaskStates": {"step1": None}},
        ],
    )
    def test_absent_state_degrades_to_empty(self, alloc: dict[str, Any]):
        """Assert every shape that lacks the step yields an empty state."""
        assert _alloc_step_state(alloc, "step1") == {}

    @pytest.mark.parametrize(
        ("alloc", "expected_log"),
        [
            (
                {"ID": "alloc-1", "TaskStates": {"step1": "not a mapping"}},
                "non-mapping task state",
            ),
            (
                {"ID": "alloc-1", "TaskStates": "not a mapping"},
                "non-mapping task states",
            ),
            (
                {"ID": "alloc-1", "TaskStates": ["step1"]},
                "non-mapping task states",
            ),
        ],
    )
    def test_malformed_shape_is_logged_and_degrades_to_empty(
        self,
        alloc: dict[str, Any],
        expected_log: str,
        caplog: pytest.LogCaptureFixture,
    ):
        """Assert non-mapping task states are reported instead of raising.

        A missing state is the expected shape for a task that never started;
        a container or member that is present but not a mapping is upstream
        shape drift, so it must leave a trace rather than either look identical
        to not-started or crash the sync, stop and log paths.
        """
        with caplog.at_level(logging.WARNING):
            assert _alloc_step_state(alloc, "step1") == {}

        assert expected_log in caplog.text
        assert "alloc-1" in caplog.text

    @pytest.mark.parametrize(
        "task_states", ["not a mapping", ["step1"], 7], ids=["str", "list", "int"]
    )
    def test_alloc_task_states_tolerates_non_mapping_container(
        self, task_states: Any, caplog: pytest.LogCaptureFixture
    ):
        """Assert a non-mapping ``TaskStates`` degrades to an empty mapping."""
        with caplog.at_level(logging.WARNING):
            assert (
                _alloc_task_states({"ID": "alloc-1", "TaskStates": task_states}) == {}
            )

        assert "non-mapping task states" in caplog.text


class TestFailedStepReason:
    """Test _failed_step_reason's composition and shape tolerance."""

    def test_names_the_failed_step_and_exit_code(self):
        """Assert the reason names the failing producing step and its exit code."""
        alloc = {
            "TaskStates": {
                "run-script": {
                    "Failed": True,
                    "Events": [{"Type": "Terminated", "ExitCode": 1}],
                },
            },
        }
        assert _failed_step_reason(alloc) == "Step 'run-script' failed (exit code 1)."

    def test_omits_exit_code_when_no_terminated_event(self):
        """Assert a failed step with no Terminated event still names the step."""
        alloc = {"TaskStates": {"run-script": {"Failed": True, "Events": []}}}
        assert _failed_step_reason(alloc) == "Step 'run-script' failed."

    def test_ignores_the_non_producing_hold_step(self):
        """Assert a failed log-capture hold does not become the reason.

        The hold is the one step ``NomadStep.is_persistable`` excludes, so a
        failure of PMM Extensions' own capture machinery cannot be reported as the run's.
        """
        alloc = {
            "TaskStates": {
                "log-capture-hold": {
                    "Failed": True,
                    "Events": [{"Type": "Terminated", "ExitCode": 1}],
                },
            },
        }
        assert _failed_step_reason(alloc) is None

    def test_reports_the_earliest_failed_step_not_the_first_serialized(self):
        """Assert the failing step is chosen by execution order, not key order.

        Nomad serializes task states with the keys sorted, so ``clean-up`` is
        emitted before ``run-script``. A payload that failed and a cleanup that
        then failed after it must be reported against the payload.
        """
        alloc = {
            "TaskStates": {
                "clean-up": {
                    "Failed": True,
                    "StartedAt": "2026-01-01T10:05:00Z",
                    "Events": [{"Type": "Terminated", "ExitCode": 7}],
                },
                "run-script": {
                    "Failed": True,
                    "StartedAt": "2026-01-01T10:00:00Z",
                    "Events": [{"Type": "Terminated", "ExitCode": 2}],
                },
            },
        }
        assert _failed_step_reason(alloc) == "Step 'run-script' failed (exit code 2)."

    def test_returns_none_when_no_step_failed(self):
        """Assert an allocation whose producing steps all succeeded has no reason."""
        alloc = {"TaskStates": {"run-script": {"Failed": False, "Events": []}}}
        assert _failed_step_reason(alloc) is None

    def test_reports_the_last_termination_of_a_restarted_step(self):
        """Assert a restarted step reports the code that decided its outcome.

        Nomad appends one ``Terminated`` event per attempt, oldest first, so
        the final one is the failure the allocation actually ended on.
        """
        alloc = {
            "TaskStates": {
                "run-script": {
                    "Failed": True,
                    "Events": [
                        {"Type": "Terminated", "ExitCode": 1},
                        {"Type": "Restarting"},
                        {"Type": "Terminated", "ExitCode": 137},
                    ],
                },
            },
        }
        assert _failed_step_reason(alloc) == "Step 'run-script' failed (exit code 137)."

    @pytest.mark.parametrize(
        ("alloc", "expected"),
        [
            ({}, None),
            ({"TaskStates": None}, None),
            ({"TaskStates": []}, None),
            ({"TaskStates": "broken"}, None),
            ({"TaskStates": {"run-script": "not-a-dict"}}, None),
            (
                {
                    "TaskStates": {
                        "run-script": {"Failed": True, "Events": "not-a-list"}
                    }
                },
                "Step 'run-script' failed.",
            ),
            (
                {
                    "TaskStates": {
                        "run-script": {"Failed": True, "Events": ["not-a-dict"]}
                    }
                },
                "Step 'run-script' failed.",
            ),
        ],
    )
    def test_tolerates_malformed_allocations(self, alloc, expected):
        """Assert shape drift costs the exit code, not the reason or the sync."""
        assert _failed_step_reason(alloc) == expected


class TestDetectUnlaunchable:
    """Test the module-level ``_detect_unlaunchable`` helper.

    Mirrors :class:`TestDetectStaleSkip` — both sentinels are read off the same
    defensive walk, so both need the same shape-drift coverage.
    """

    def test_returns_false_when_task_states_none(self):
        """Assert a missing ``TaskStates`` object classifies as not unlaunchable."""
        assert _detect_unlaunchable(None) is False

    def test_returns_false_when_task_states_not_dict(self):
        """Assert ``_detect_unlaunchable`` tolerates a non-dict input."""
        assert _detect_unlaunchable("not a dict") is False

    def test_returns_false_when_task_absent(self):
        """Assert an allocation with no check step classifies as not unlaunchable.

        Allocations dispatched from a job registered before this step existed
        carry no such key, and must keep resolving as they did.
        """
        assert _detect_unlaunchable({"other-task": {"Events": []}}) is False

    def test_returns_false_when_events_missing(self):
        """Assert a check step with no ``Events`` short-circuits to ``False``."""
        assert _detect_unlaunchable({"check-launchable": {"State": "dead"}}) is False

    def test_returns_true_on_terminated_sentinel_exit(self):
        """Assert the sentinel exit code on a ``Terminated`` event classifies."""
        task_states = {
            "check-launchable": {
                "Events": [
                    {"Type": "Started"},
                    {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE},
                ],
            }
        }
        assert _detect_unlaunchable(task_states) is True

    def test_returns_false_on_terminated_exit_1(self):
        """Assert a check step that failed for another reason is not classified."""
        task_states = {
            "check-launchable": {
                "Events": [{"Type": "Terminated", "ExitCode": 1}],
            }
        }
        assert _detect_unlaunchable(task_states) is False

    def test_returns_false_on_non_terminated_event(self):
        """Assert the sentinel is only read off a ``Terminated`` event."""
        task_states = {
            "check-launchable": {
                "Events": [{"Type": "Started", "ExitCode": LAUNCH_CHECK_EXIT_CODE}],
            }
        }
        assert _detect_unlaunchable(task_states) is False

    def test_reads_exit_code_from_details_nested_shape(self):
        """Assert exit-code falls back to the ``Details.exit_code`` shape."""
        task_states = {
            "check-launchable": {
                "Events": [
                    {
                        "Type": "Terminated",
                        "Details": {"exit_code": LAUNCH_CHECK_EXIT_CODE},
                    },
                ],
            }
        }
        assert _detect_unlaunchable(task_states) is True

    def test_does_not_read_the_staleness_step(self):
        """Assert each detector reads only its own step's task state.

        The two sentinels differ, but reading the wrong step would still
        misreport whenever the exit codes happened to coincide.
        """
        task_states = {
            "check-staleness": {
                "Events": [
                    {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE},
                ],
            }
        }
        assert _detect_unlaunchable(task_states) is False


class TestDetectStaleSkip:
    """Test the module-level ``_detect_stale_skip`` helper."""

    def test_returns_false_when_task_states_none(self):
        """Assert ``_detect_stale_skip`` returns ``False`` when input is ``None``."""
        assert _detect_stale_skip(None) is False

    def test_returns_false_when_task_states_not_dict(self):
        """Assert ``_detect_stale_skip`` tolerates a non-dict input."""
        assert _detect_stale_skip("not a dict") is False

    def test_returns_false_when_task_absent(self):
        """Assert ``_detect_stale_skip`` returns ``False`` when the task key is absent."""
        assert _detect_stale_skip({"other-task": {"Events": []}}) is False

    def test_returns_false_when_events_missing(self):
        """Assert ``_detect_stale_skip`` returns ``False`` when ``Events`` is missing."""
        assert _detect_stale_skip({"check-staleness": {"State": "dead"}}) is False

    def test_returns_true_on_terminated_exit_75(self):
        """Assert exit-75 ``Terminated`` event classifies as stale."""
        task_states = {
            "check-staleness": {
                "Events": [
                    {"Type": "Started"},
                    {"Type": "Terminated", "ExitCode": 75},
                ],
            }
        }
        assert _detect_stale_skip(task_states) is True

    def test_returns_false_on_terminated_exit_1(self):
        """Assert non-75 ``Terminated`` exit does NOT classify as stale."""
        task_states = {
            "check-staleness": {
                "Events": [{"Type": "Terminated", "ExitCode": 1}],
            }
        }
        assert _detect_stale_skip(task_states) is False

    def test_returns_false_when_exit_code_missing(self):
        """Assert a ``Terminated`` event with no exit code short-circuits to ``False``."""
        task_states = {
            "check-staleness": {
                "Events": [{"Type": "Terminated"}],
            }
        }
        assert _detect_stale_skip(task_states) is False

    def test_reads_exit_code_from_details_nested_shape(self):
        """Assert exit-code falls back to ``Details.exit_code`` shape."""
        task_states = {
            "check-staleness": {
                "Events": [
                    {"Type": "Terminated", "Details": {"exit_code": 75}},
                ],
            }
        }
        assert _detect_stale_skip(task_states) is True

    def test_returns_false_when_events_not_a_list(self):
        """Assert a non-list ``Events`` value short-circuits to ``False``."""
        assert (
            _detect_stale_skip({"check-staleness": {"Events": "not-a-list"}}) is False
        )

    def test_returns_false_on_non_terminated_and_missing_type(self):
        """Assert non-``Terminated`` (including missing) ``Type`` is not a match.

        The walk keeps scanning and returns ``False`` once every event has been
        checked without a sentinel hit.
        """
        task_states = {
            "check-staleness": {
                "Events": [
                    {"Type": "Started", "ExitCode": 75},
                    {"ExitCode": 75},
                ],
            }
        }
        assert _detect_stale_skip(task_states) is False

    def test_skips_non_dict_event_then_matches_terminated(self):
        """Assert a non-dict event is skipped while a later sentinel still matches."""
        task_states = {
            "check-staleness": {
                "Events": [
                    "not-a-dict",
                    {"Type": "Terminated", "ExitCode": 75},
                ],
            }
        }
        assert _detect_stale_skip(task_states) is True


class TestGetJob:
    """Test NomadExecutor.get_job."""

    @pytest.mark.asyncio
    async def test_get_job_success(self) -> None:
        """Assert get_job returns job details."""
        executor = _build_executor()

        with _stub_nomad_api(
            executor, job={"ID": "job-1", "Status": "running"}
        ) as calls:
            result = await executor.get_job("job-1")

        assert result["ID"] == "job-1"
        assert calls == [("GET", "/v1/job/job-1", {})]

    @pytest.mark.asyncio
    async def test_get_job_not_found_raises(self) -> None:
        """Assert get_job maps Nomad's 404 to JobNotFoundError."""
        executor = _build_executor()

        with (
            _stub_nomad_api(executor, job=_nomad_error(status.HTTP_404_NOT_FOUND)),
            pytest.raises(JobNotFoundError),
        ):
            await executor.get_job("missing-job")

    @pytest.mark.asyncio
    async def test_get_job_propagates_a_non_404_status(self) -> None:
        """Assert only a 404 means "gone"; other statuses stay transport errors.

        python-nomad drew this line by raising ``URLNotFoundNomadException``
        apart from ``BaseNomadException``, and callers act on it: ``_sync_task_
        history`` marks a history LOST on :class:`JobNotFoundError`, so reading a
        Nomad 500 or 503 as absence would retire a task whose job is still there.
        """
        executor = _build_executor()

        with (
            _stub_nomad_api(
                executor, job=_nomad_error(status.HTTP_503_SERVICE_UNAVAILABLE)
            ),
            pytest.raises(NomadRequestError) as exc_info,
        ):
            await executor.get_job("job-1")

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


class TestGetJobForTaskHistory:
    """Test NomadExecutor.get_job_for_task_history."""

    @pytest.mark.asyncio
    async def test_get_job_for_task_history_success(self) -> None:
        """Assert get_job_for_task_history retrieves job by tracking job_id."""
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={"job_id": "job-1", "allocation_id": None, "evaluation_id": "e-1"}
        )

        with _stub_nomad_api(executor, job={"ID": "job-1"}):
            result = await executor.get_job_for_task_history(queue_item)

        assert result["ID"] == "job-1"

    @pytest.mark.asyncio
    async def test_get_job_for_task_history_missing_job_id(self) -> None:
        """Assert get_job_for_task_history raises when job_id is missing."""
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={"allocation_id": None, "evaluation_id": "e-1"}
        )

        with pytest.raises(JobNotFoundError, match="Missing job_id"):
            await executor.get_job_for_task_history(queue_item)


class TestGetHosts:
    """Test NomadExecutor.get_hosts."""

    @pytest.mark.asyncio
    async def test_get_hosts(self) -> None:
        """Assert get_hosts returns filtered healthy nodes."""
        executor = _build_executor()

        with _stub_nomad_api(
            executor,
            nodes=[
                {"Name": "node-a", "Address": "10.0.0.1"},
                {"Name": "node-b", "Address": "10.0.0.2"},
            ],
        ) as calls:
            result = await executor.get_hosts()

        assert result == {"node-a": "10.0.0.1", "node-b": "10.0.0.2"}
        assert len(calls) == 1
        method, path, kwargs = calls[0]
        assert (method, path) == ("GET", "/v1/nodes")
        # A dropped clause or flipped operator here changes only the filter
        # expression, which the fixture above never exercises - pin its structure
        # directly rather than relying on the healthy-node fixtures to catch it.
        assert kwargs["params"]["filter"] == (
            f"Status == {NODE_STATUS_READY} "
            f"and {RAW_EXEC_DRIVER} in Drivers "
            f"and Drivers.{RAW_EXEC_DRIVER}.Healthy == true"
        )


class TestGetHostStates:
    """Test NomadExecutor.get_host_states.

    The point of this method is telling apart the three ways a machine ends up
    absent from ``get_hosts``. Each is asserted separately, because collapsing them
    is the behaviour being replaced and it would pass a test that only checked "not
    usable".
    """

    @pytest.mark.asyncio
    async def test_reports_every_node_not_only_the_usable_ones(self) -> None:
        """Assert an unusable node is a row here rather than an omission."""
        executor = _build_executor()
        nodes = [
            {
                "Name": "healthy",
                "Address": "10.0.0.1",
                "Status": "ready",
                "Drivers": {"raw_exec": {"Healthy": True}},
            },
            {
                "Name": "down",
                "Address": "10.0.0.2",
                "Status": "down",
                "Drivers": {"raw_exec": {"Healthy": True}},
            },
        ]

        with _stub_nomad_api(executor, nodes=nodes) as calls:
            states = {state.name: state for state in await executor.get_host_states()}

        assert set(states) == {"healthy", "down"}
        assert (states["healthy"].reachable, states["healthy"].driver_healthy) == (
            True,
            True,
        )
        assert (states["down"].reachable, states["down"].driver_healthy) == (
            False,
            True,
        )
        # No filter: this call must see what get_hosts filters out.
        assert calls == [("GET", "/v1/nodes", {"params": None})]

    @pytest.mark.asyncio
    async def test_separates_unreachable_from_driver_unhealthy(self) -> None:
        """Assert down and broken-driver are different answers, not one.

        Never onboarded and onboarded-but-broken need different people to fix them,
        so a single "unusable" flag sends the reader to the wrong place half the
        time.
        """
        executor = _build_executor()
        nodes = [
            {
                "Name": "down",
                "Address": "10.0.0.1",
                "Status": "down",
                "Drivers": {
                    "raw_exec": {"Healthy": True, "HealthDescription": "Healthy"}
                },
            },
            {
                "Name": "broken-driver",
                "Address": "10.0.0.2",
                "Status": "ready",
                "Drivers": {
                    "raw_exec": {
                        "Healthy": False,
                        "HealthDescription": "Failed to find raw_exec",
                    }
                },
            },
        ]

        with _stub_nomad_api(executor, nodes=nodes):
            states = {state.name: state for state in await executor.get_host_states()}

        assert (states["down"].reachable, states["down"].driver_healthy) == (
            False,
            True,
        )
        assert (
            states["broken-driver"].reachable,
            states["broken-driver"].driver_healthy,
        ) == (True, False)
        assert states["broken-driver"].detail == "Failed to find raw_exec"
        # No StatusDescription in this fixture, so nothing to report; see
        # test_unreachable_node_detail_comes_from_status_description for the case
        # where Nomad does supply one.
        assert states["down"].detail is None

    @pytest.mark.asyncio
    async def test_a_missing_driver_entry_is_not_healthy(self) -> None:
        """Assert an undetected driver reads as unhealthy rather than absent-so-fine.

        Nomad omits drivers it has not detected, so the never-onboarded host has no
        ``raw_exec`` key at all. Treating a missing key as anything but unhealthy
        would report the emptiest case as the healthiest.
        """
        executor = _build_executor()
        nodes = [
            {"Name": "bare", "Address": "10.0.0.1", "Status": "ready", "Drivers": {}},
            {"Name": "no-key", "Address": "10.0.0.2", "Status": "ready"},
        ]

        with _stub_nomad_api(executor, nodes=nodes):
            states = {state.name: state for state in await executor.get_host_states()}

        assert states["bare"].driver_healthy is False
        assert states["no-key"].driver_healthy is False
        assert all(state.reachable for state in states.values())

    @pytest.mark.asyncio
    async def test_unreachable_node_detail_comes_from_status_description(self) -> None:
        """Assert a down node explains itself instead of reporting nothing.

        The driver fields are a stale pre-disconnect snapshot once the node itself
        is unreachable, so ``detail`` has to come from the node's own status text,
        not from a driver reading that predates the outage.
        """
        executor = _build_executor()
        nodes = [
            {
                "Name": "down",
                "Address": "10.0.0.1",
                "Status": "down",
                "StatusDescription": "Node heartbeat missed",
                "Drivers": {
                    "raw_exec": {"Healthy": True, "HealthDescription": "Healthy"}
                },
            }
        ]

        with _stub_nomad_api(executor, nodes=nodes):
            states = {state.name: state for state in await executor.get_host_states()}

        assert states["down"].reachable is False
        assert states["down"].detail == "Node heartbeat missed"

    @pytest.mark.asyncio
    async def test_malformed_healthy_value_is_not_healthy(self) -> None:
        """Assert a non-boolean ``Healthy`` value cannot read as healthy.

        ``bool()`` would turn any non-empty malformed value - including the string
        ``"false"`` - into ``True``. The identity check guards against exactly this
        regression, to ``bool(driver.get("Healthy"))``, which would otherwise keep
        the whole suite green.
        """
        executor = _build_executor()
        nodes = [
            {
                "Name": "string-false",
                "Address": "10.0.0.1",
                "Status": "ready",
                "Drivers": {"raw_exec": {"Healthy": "false"}},
            },
            {
                "Name": "truthy-int",
                "Address": "10.0.0.2",
                "Status": "ready",
                "Drivers": {"raw_exec": {"Healthy": 1}},
            },
        ]

        with _stub_nomad_api(executor, nodes=nodes):
            states = {state.name: state for state in await executor.get_host_states()}

        assert states["string-false"].driver_healthy is False
        assert states["truthy-int"].driver_healthy is False


class TestGetAllocationForTaskHistory:
    """Test NomadExecutor.get_allocation_for_task_history."""

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_allocation_by_allocation_id(self, mock_nomad_cls):
        """Assert allocation is fetched directly when allocation_id is in tracking."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        result = executor.get_allocation_for_task_history(queue_item)
        assert result["ID"] == "alloc-1"

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_allocation_fallback_to_last_allocation(self, mock_nomad_cls):
        """Assert fallback to get_last_allocation when allocation_id lookup fails."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.return_value = [
            {
                "ID": "alloc-fallback",
                "JobID": "job-1",
                "EvalID": "eval-1",
                "TaskStates": None,
            }
        ]

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-missing",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        result = executor.get_allocation_for_task_history(queue_item)
        assert result["ID"] == "alloc-fallback"


class TestGetLastAllocation:
    """Test NomadExecutor.get_last_allocation."""

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_success(self, mock_nomad_cls):
        """Assert get_last_allocation returns first allocation with sorted task states."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.return_value = [
            {
                "ID": "alloc-1",
                "JobID": "job-1",
                "TaskStates": {
                    "zz-step": {"StartedAt": "2", "FinishedAt": "3"},
                    "aa-step": {"StartedAt": "1", "FinishedAt": "2"},
                },
            }
        ]

        executor = _build_executor()
        result = executor.get_last_allocation(job_id="job-1", eval_id="eval-1")

        assert result["ID"] == "alloc-1"
        keys = list(result["TaskStates"].keys())
        assert keys == ["aa-step", "zz-step"]

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_no_filters_raises(self, mock_nomad_cls):
        """Assert get_last_allocation raises ValueError without any filter."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        with pytest.raises(ValueError, match="Either job_id or eval_id"):
            executor.get_last_allocation()

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_not_found(self, mock_nomad_cls):
        """Assert get_last_allocation raises AllocationNotFoundError when empty."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.return_value = []

        executor = _build_executor()
        with pytest.raises(AllocationNotFoundError) as ctx:
            executor.get_last_allocation(job_id="missing-job")
        err = ctx.value
        assert err.job_id == "missing-job"
        assert err.evaluation_id is None
        assert err.resource_type == "allocation"

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_null_task_states(self, mock_nomad_cls):
        """Assert get_last_allocation handles None TaskStates."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.return_value = [
            {"ID": "alloc-1", "JobID": "job-1", "TaskStates": None}
        ]

        executor = _build_executor()
        result = executor.get_last_allocation(job_id="job-1")
        assert result["TaskStates"] is None

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_absent_task_states(self, mock_nomad_cls):
        """Assert an allocation stub without a ``TaskStates`` key is returned as-is."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.return_value = [
            {"ID": "alloc-1", "JobID": "job-1"}
        ]

        executor = _build_executor()
        result = executor.get_last_allocation(job_id="job-1")
        assert result == {"ID": "alloc-1", "JobID": "job-1"}

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_last_allocation_non_mapping_step_state(
        self, mock_nomad_cls, caplog: pytest.LogCaptureFixture
    ):
        """Assert a non-mapping task state still sorts instead of raising.

        This read walks the ``FollowupEvalID`` reschedule chain, so raising here
        wedges the sync and stop paths the container guard already protects. A
        step with no usable timestamps sorts last, behind the well-formed one.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.return_value = [
            {
                "ID": "alloc-1",
                "JobID": "job-1",
                "TaskStates": {
                    "broken-step": "not a mapping",
                    "step1": {"StartedAt": "1", "FinishedAt": "2"},
                },
            }
        ]

        executor = _build_executor()
        with caplog.at_level(logging.WARNING):
            result = executor.get_last_allocation(job_id="job-1")

        assert list(result["TaskStates"]) == ["step1", "broken-step"]
        assert "non-mapping task state" in caplog.text


class TestPreflightStreamLogs:
    """Test NomadExecutor.preflight_stream_logs."""

    @staticmethod
    def _alloc(client_status: str, **extra: Any) -> dict[str, Any]:
        """Build an allocation for ``job-1``/``eval-1`` in ``client_status``.

        :param client_status: The allocation's Nomad ``ClientStatus``.
        :param extra: Further allocation fields, such as ``TaskStates``.
        :return: The allocation payload Nomad would list.
        """
        return {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": client_status,
            **extra,
        }

    @staticmethod
    def _executor(
        mock_nomad_cls: MagicMock,
        allocations: list[list[dict[str, Any]]],
        evaluation_status: str = NomadEvalStatusEnum.COMPLETE,
        evaluations: list[dict[str, Any]] | None = None,
        job: Any = None,
    ) -> tuple[NomadExecutor, MagicMock]:
        """Build an executor whose Nomad backend answers the preflight's reads.

        The preflight's job read goes through ``nomad_json``, not
        ``backend.job``, so it is stubbed with :func:`_stub_nomad_calls`; the
        allocation and evaluation listings are still python-nomad.

        :param mock_nomad_cls: The patched ``Nomad`` class.
        :param allocations: One allocation listing per successive read.
        :param evaluation_status: The ``Status`` of the job's only evaluation,
            used when ``evaluations`` is not given.
        :param evaluations: The job's full evaluation listing, for a chain.
        :param job: Answer for ``GET /v1/job/{id}``, an exception included.
        :return: The executor and its mocked backend.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.side_effect = allocations
        mock_backend.job.get_evaluations.return_value = (
            evaluations
            if evaluations is not None
            else [{"ID": "eval-1", "Status": evaluation_status}]
        )
        _stub_nomad_calls(job=job or {"ID": "job-1"})
        return _build_executor(), mock_backend

    @pytest.mark.parametrize(
        "evaluation_status",
        [NomadEvalStatusEnum.PENDING, NomadEvalStatusEnum.BLOCKED],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_no_allocation_with_live_evaluation_is_not_started(
        self, mock_nomad_cls, evaluation_status
    ):
        """Assert a job Nomad is still placing or holding for capacity is not started.

        A ``blocked`` evaluation is work queued until the cluster frees
        capacity, so its run is alive and the viewer should keep retrying.
        """
        executor, mock_backend = self._executor(
            mock_nomad_cls, [[], []], evaluation_status=evaluation_status
        )

        with pytest.raises(TaskNotStartedInExecutorError):
            await executor.preflight_stream_logs(_build_queue_item())

        mock_backend.job.get_evaluations.assert_called_once_with("job-1")
        allocation_read = call(
            filter_='JobID == "job-1" and EvalID == "eval-1"', reverse=True
        )
        assert mock_backend.allocations.get_allocations.call_args_list == [
            allocation_read,
            allocation_read,
        ]

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_blocking_reads_stay_off_the_event_loop(self, mock_nomad_cls):
        """Assert the python-nomad reads run in worker threads, not on the loop.

        The route awaits this on the event loop for every live-log viewer, and the
        allocation and evaluation listings still go through the blocking client,
        so one slow Nomad answer would otherwise stall every other request.
        """
        executor, mock_backend = self._executor(
            mock_nomad_cls, [[], []], evaluation_status=NomadEvalStatusEnum.PENDING
        )
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def record(answers: list[Any]) -> Any:
            """Answer each call with the next of ``answers``, noting its thread."""

            def read(*_args: Any, **_kwargs: Any) -> Any:
                threads.append(threading.get_ident())
                return answers.pop(0)

            return read

        mock_backend.allocations.get_allocations.side_effect = record([[], []])
        mock_backend.job.get_evaluations.side_effect = record(
            [[{"ID": "eval-1", "Status": NomadEvalStatusEnum.PENDING}]]
        )

        with pytest.raises(TaskNotStartedInExecutorError):
            await executor.preflight_stream_logs(_build_queue_item())

        # Every read was recorded - two allocation listings and the evaluation
        # listing - and none of them ran on the loop.
        assert len(threads) == (
            mock_backend.allocations.get_allocations.call_count
            + mock_backend.job.get_evaluations.call_count
        )
        assert mock_backend.job.get_evaluations.called
        assert loop_thread not in threads

    @pytest.mark.parametrize(
        "other_evaluations",
        [[], [{"ID": "eval-9", "Status": NomadEvalStatusEnum.BLOCKED}]],
        ids=["sole-run", "another-run-blocked"],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_placed_by_blocked_evaluation_is_found(
        self, mock_nomad_cls, other_evaluations
    ):
        """Assert work placed after waiting for capacity is streamed, not gone.

        Nomad places it under the ``blocked`` evaluation the tracked one spawned,
        while the history still tracks the dispatch evaluation. A non-parameterized
        job is shared by every run of its task and target, so another run's
        ``blocked`` evaluation must not hide this run's placed allocation.
        """
        started = self._alloc(
            NomadAllocStatusEnum.RUNNING,
            EvalID="eval-2",
            TaskStates={"step1": {"State": "running", "StartedAt": "1"}},
        )
        executor, mock_backend = self._executor(
            mock_nomad_cls,
            [[], [started]],
            evaluations=[
                {
                    "ID": "eval-1",
                    "Status": NomadEvalStatusEnum.COMPLETE,
                    "BlockedEval": "eval-2",
                },
                {"ID": "eval-2", "Status": NomadEvalStatusEnum.COMPLETE},
                *other_evaluations,
            ],
        )

        assert await executor.preflight_stream_logs(_build_queue_item()) is None

        assert mock_backend.allocations.get_allocations.call_args_list == [
            call(filter_='JobID == "job-1" and EvalID == "eval-1"', reverse=True),
            call(filter_='JobID == "job-1" and EvalID == "eval-2"', reverse=True),
        ]

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_no_allocation_and_nothing_pending_stays_gone(self, mock_nomad_cls):
        """Assert a job with no allocation and no pending evaluation is gone.

        The allocation is re-read once after the evaluations are seen to be
        settled, and only a second miss is final.
        """
        executor, mock_backend = self._executor(mock_nomad_cls, [[], []])

        with pytest.raises(AllocationNotFoundError):
            await executor.preflight_stream_logs(_build_queue_item())

        allocation_read = call(
            filter_='JobID == "job-1" and EvalID == "eval-1"', reverse=True
        )
        assert mock_backend.allocations.get_allocations.call_args_list == [
            allocation_read,
            allocation_read,
        ]

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_placed_between_reads_is_not_started(self, mock_nomad_cls):
        """Assert an allocation placed while the evaluation completed is not gone.

        The first read misses the allocation and the evaluation read then sees
        nothing pending; the re-read finds the freshly placed allocation.
        """
        executor, _ = self._executor(
            mock_nomad_cls, [[], [self._alloc(NomadAllocStatusEnum.PENDING)]]
        )

        with pytest.raises(TaskNotStartedInExecutorError):
            await executor.preflight_stream_logs(_build_queue_item())

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_placed_between_reads_with_task_states_passes(
        self, mock_nomad_cls
    ):
        """Assert a re-read allocation that already lists its steps lets the stream open."""
        started = self._alloc(
            NomadAllocStatusEnum.RUNNING,
            TaskStates={"step1": {"State": "running", "StartedAt": "1"}},
        )
        executor, _ = self._executor(mock_nomad_cls, [[], [started]])

        assert await executor.preflight_stream_logs(_build_queue_item()) is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_no_allocation_and_job_gone_raises_job_not_found(
        self, mock_nomad_cls
    ):
        """Assert a vanished job keeps answering as gone, not as not-yet-started."""
        executor, mock_backend = self._executor(
            mock_nomad_cls, [[]], job=_nomad_error(status.HTTP_404_NOT_FOUND)
        )

        with pytest.raises(JobNotFoundError):
            await executor.preflight_stream_logs(_build_queue_item())

        mock_backend.job.get_evaluations.assert_not_called()

    @pytest.mark.parametrize(
        "client_status",
        [NomadAllocStatusEnum.PENDING, NomadAllocStatusEnum.RUNNING],
    )
    @pytest.mark.asyncio
    @pytest.mark.parametrize("task_states", [{}, None])
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_live_allocation_without_task_states_is_not_started(
        self, mock_nomad_cls, task_states, client_status
    ):
        """Assert a live allocation that reports no task state yet is not started."""
        alloc = self._alloc(client_status)
        if task_states is not None:
            alloc["TaskStates"] = task_states
        executor, _ = self._executor(mock_nomad_cls, [[alloc]])

        with pytest.raises(TaskNotStartedInExecutorError):
            await executor.preflight_stream_logs(_build_queue_item())

    @pytest.mark.parametrize(
        "client_status",
        [
            NomadAllocStatusEnum.FAILED,
            NomadAllocStatusEnum.LOST,
            NomadAllocStatusEnum.COMPLETE,
        ],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dead_allocation_without_task_states_passes(
        self, mock_nomad_cls, client_status
    ):
        """Assert a dead allocation with no task state falls through to the stream.

        The stream then ends at once and the proxy's reconcile settles the
        history to its terminal status.
        """
        executor, _ = self._executor(mock_nomad_cls, [[self._alloc(client_status)]])

        assert await executor.preflight_stream_logs(_build_queue_item()) is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_with_pending_steps_passes(self, mock_nomad_cls):
        """Assert an allocation listing steps that have not started yet passes.

        The live stream waits for the first step in-stream, so the preflight
        does not cut that wait short with a retryable conflict.
        """
        alloc = self._alloc(
            NomadAllocStatusEnum.PENDING, TaskStates={"step1": {"State": "pending"}}
        )
        executor, _ = self._executor(mock_nomad_cls, [[alloc]])

        assert await executor.preflight_stream_logs(_build_queue_item()) is None


class TestStreamAllocation:
    """Test NomadExecutor._stream_allocation."""

    @staticmethod
    def _backend(
        mock_nomad_cls: MagicMock, allocations: list[list[dict[str, Any]]]
    ) -> MagicMock:
        """Wire a backend whose dispatch evaluation spawned a ``blocked`` one.

        :param mock_nomad_cls: The patched ``Nomad`` class.
        :param allocations: One allocation listing per successive read.
        :return: The mocked backend.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocations.get_allocations.side_effect = allocations
        mock_backend.job.get_evaluations.return_value = [
            {
                "ID": "eval-1",
                "Status": NomadEvalStatusEnum.COMPLETE,
                "BlockedEval": "eval-2",
            },
            {"ID": "eval-2", "Status": NomadEvalStatusEnum.COMPLETE},
        ]
        return mock_backend

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_tracked_allocation_skips_the_evaluation_read(self, mock_nomad_cls):
        """Assert an allocation under the tracked evaluation costs one read."""
        alloc = {"ID": "alloc-1", "JobID": "job-1", "EvalID": "eval-1"}
        mock_backend = self._backend(mock_nomad_cls, [[alloc]])

        assert _build_executor()._stream_allocation("job-1", "eval-1") == alloc

        mock_backend.job.get_evaluations.assert_not_called()

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_allocation_placed_by_blocked_evaluation_is_found(self, mock_nomad_cls):
        """Assert a miss on the tracked evaluation follows its ``BlockedEval``."""
        alloc = {"ID": "alloc-2", "JobID": "job-1", "EvalID": "eval-2"}
        mock_backend = self._backend(mock_nomad_cls, [[], [alloc]])

        assert _build_executor()._stream_allocation("job-1", "eval-1") == alloc

        mock_backend.job.get_evaluations.assert_called_once_with("job-1")
        assert mock_backend.allocations.get_allocations.call_args_list[-1] == call(
            filter_='JobID == "job-1" and EvalID == "eval-2"', reverse=True
        )

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_nothing_placed_along_the_chain_raises(self, mock_nomad_cls):
        """Assert the stream still reports a missing allocation when none exists."""
        self._backend(mock_nomad_cls, [[], [], []])

        with pytest.raises(AllocationNotFoundError):
            _build_executor()._stream_allocation("job-1", "eval-1")


class TestDispatchTask:
    """Test NomadExecutor.dispatch_task."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.TaskHistoryManager")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dispatch_task_parameterized(self, mock_nomad_cls, mock_th_manager):
        """Assert dispatch_task handles parameterized job flow correctly."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        nomad_register = {"EvalID": "eval-reg"}
        # In order: task_needs_job_register's lookup misses, the freshly
        # registered job is read back, then the dispatched child job is.
        nomad_job = iter(
            [
                _nomad_error(status.HTTP_404_NOT_FOUND),
                {"ID": "param-job-node-1", "SubmitTime": None},
                {"ID": "dispatched-job-1", "SubmitTime": 1_700_000_000_000_000_000},
            ]
        )
        nomad_dispatch = {
            "DispatchedJobID": "dispatched-job-1",
            "EvalID": "eval-disp",
        }

        mock_th_manager.save = AsyncMock(side_effect=lambda _s, qi, **_kw: qi)

        executor = _build_executor(
            nomad_job=nomad_job,
            nomad_register=nomad_register,
            nomad_dispatch=nomad_dispatch,
        )
        task = _build_task(task_id="param-job", parameterized=True)
        queue_item = _build_queue_item(
            task=task,
            status=TaskHistoryStatusEnum.PENDING,
        )
        session = AsyncMock()

        result = await executor.dispatch_task(session, queue_item, task)

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.execution_request.tracking is not None
        assert result.execution_request.tracking["job_id"] == "dispatched-job-1"
        assert result.execution_request.tracking["evaluation_id"] == "eval-disp"
        assert result.started_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.TaskHistoryManager")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dispatch_task_non_parameterized(
        self, mock_nomad_cls, mock_th_manager
    ):
        """Assert dispatch_task handles non-parameterized job registration."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        nomad_register = {"EvalID": "eval-1"}
        nomad_job = {
            "ID": "non-param-job-node-1",
            "SubmitTime": 1_700_000_000_000_000_000,
        }

        mock_th_manager.save = AsyncMock(side_effect=lambda _s, qi, **_kw: qi)

        calls = _stub_nomad_calls(job=nomad_job, register=nomad_register)
        executor = _build_executor()
        task = _build_task(task_id="non-param-job", parameterized=False)
        queue_item = _build_queue_item(
            task=task,
            status=TaskHistoryStatusEnum.PENDING,
        )
        session = AsyncMock()

        result = await executor.dispatch_task(session, queue_item, task)

        assert result.status == TaskHistoryStatusEnum.RUNNING
        # The prepared task carries the per-node job id, so the registration
        # posts to that rather than to the task's own name.
        registered = [path for method, path, _ in calls if method == "POST"]
        assert registered == ["/v1/job/non-param-job-node-1"]

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.TaskHistoryManager")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dispatch_task_uses_submit_time(
        self, mock_nomad_cls, mock_th_manager
    ):
        """Assert dispatch_task uses SubmitTime for started_at when available."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        submit_ns = 1_700_000_000_000_000_000
        nomad_register = {"EvalID": "eval-1"}
        nomad_job = {
            "ID": "job-1",
            "SubmitTime": submit_ns,
        }

        mock_th_manager.save = AsyncMock(side_effect=lambda _s, qi, **_kw: qi)

        executor = _build_executor(nomad_job=nomad_job, nomad_register=nomad_register)
        task = _build_task(task_id="ts-job", parameterized=False)
        queue_item = _build_queue_item(
            task=task,
            status=TaskHistoryStatusEnum.PENDING,
        )
        session = AsyncMock()

        result = await executor.dispatch_task(session, queue_item, task)

        expected_dt = datetime.fromtimestamp(submit_ns / 10**9, UTC)
        assert result.started_at == expected_dt

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dispatch_task_raises_when_job_cannot_be_determined(
        self, mock_nomad_cls
    ):
        """Assert ValueError when neither register nor dispatch yields a job."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        task = _build_task(task_id="orphan-job", parameterized=False)
        queue_item = _build_queue_item(
            task=task,
            status=TaskHistoryStatusEnum.PENDING,
        )
        session = AsyncMock()

        with (
            patch.object(
                NomadExecutor,
                "task_needs_job_register",
                AsyncMock(return_value=False),
            ),
            pytest.raises(ValueError, match="job could not be determined"),
        ):
            await executor.dispatch_task(session, queue_item, task)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.utc_now")
    @patch("app.tasks.execution.executors.nomad.models.TaskHistoryManager")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dispatch_task_falls_back_to_utc_now_without_submit_time(
        self, mock_nomad_cls, mock_th_manager, mock_utc_now
    ):
        """Assert started_at uses utc_now when the Nomad job has no SubmitTime."""
        mock_nomad_cls.return_value = MagicMock()
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        mock_utc_now.return_value = now
        mock_th_manager.save = AsyncMock(side_effect=lambda _s, qi, **_kw: qi)

        nomad_register = {"EvalID": "eval-1"}
        nomad_job = {"ID": "job-no-submit", "SubmitTime": None}
        executor = _build_executor(nomad_job=nomad_job, nomad_register=nomad_register)
        task = _build_task(task_id="no-submit-job", parameterized=False)
        queue_item = _build_queue_item(
            task=task,
            status=TaskHistoryStatusEnum.PENDING,
        )
        session = AsyncMock()

        result = await executor.dispatch_task(session, queue_item, task)

        assert result.started_at == now
        mock_utc_now.assert_called()


class TestStopTask:
    """Test NomadExecutor._stop_task."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_task(self, mock_nomad_cls):
        """Assert _stop_task deregisters the job."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "job_id": "job-to-stop",
                "allocation_id": None,
                "evaluation_id": "eval-1",
            }
        )

        await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-to-stop")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_task_missing_job_id_raises(self, mock_nomad_cls):
        """Assert _stop_task raises ValueError when job_id is missing."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={"allocation_id": None, "evaluation_id": "eval-1"}
        )

        with pytest.raises(ValueError, match="job ID could not be determined"):
            await executor._stop_task(queue_item)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lookup_error",
        [
            pytest.param(
                AllocationNotFoundError(
                    "gone",
                    executor_name="nomad",
                    resource_type="allocation",
                ),
                id="allocation-not-found",
            ),
            pytest.param(
                BaseNomadException(MagicMock(text="nomad unavailable")),
                id="base-nomad-exception",
            ),
        ],
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_task_skips_hold_release_when_allocation_lookup_fails(
        self, mock_nomad_cls, lookup_error
    ):
        """Assert post-deregister hold release is skipped when alloc lookup fails."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "job_id": "job-to-stop",
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
            }
        )

        with (
            patch.object(
                NomadExecutor,
                "get_allocation_for_task_history",
                side_effect=lookup_error,
            ),
            patch.object(
                NomadExecutor,
                "_release_capture_hold",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-to-stop")
        mock_release.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    @patch("app.tasks.execution.models.schedule_annotation")
    async def test_stop_task_with_task_states_less_allocation_reaches_stopped(
        self,
        mock_annotation: MagicMock,
        mock_nomad_cls: MagicMock,
        session: AsyncSession,
        created_task_with_history: TaskHistory,
    ):
        """Assert stopping a row backed by a ``TaskStates``-less allocation ends it.

        ``stop_task`` syncs before it writes the status, so a sync that raised
        left the row RUNNING and answered the request with a 500 — the row could
        then never be cleared through the API. Nomad is the only mocked
        boundary: the real ``TaskHistoryManager.save`` / ``session.refresh``
        lifecycle runs, and the row is refetched to prove it persisted.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-2",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.PENDING,
        }
        nomad_job = {
            "ID": "job-1",
            "Status": "running",
            "Stop": False,
        }

        queue_item = created_task_with_history
        queue_item.task.alert_on_fail = False
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.execution_request.tracking = {
            "allocation_id": "alloc-1",
            "evaluation_id": "eval-1",
            "job_id": "job-1",
        }

        result = await _build_executor(nomad_job=nomad_job).stop_task(
            session, queue_item
        )

        assert result.status == TaskHistoryStatusEnum.STOPPED
        assert result.finished_at is not None
        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        mock_annotation.assert_called_once_with(result, "STOPPED")

        result_id = result.id
        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == TaskHistoryStatusEnum.STOPPED
        assert refetched.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    @patch("app.tasks.execution.models.schedule_annotation")
    async def test_stop_task_keeps_a_payload_failure(
        self,
        mock_annotation: MagicMock,
        mock_nomad_cls: MagicMock,
        session: AsyncSession,
        created_task_with_history: TaskHistory,
    ):
        """Assert a stop landing on an already-failed run records the failure.

        A stop request can reach a row whose payload has already exited
        non-zero, because the row stays RUNNING until the next sync.
        """
        exited_at_ns = 1_700_000_000_000_000_000
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "ModifyTime": exited_at_ns,
            "TaskStates": {NomadStep.RUN_SCRIPT: {"State": "dead", "Events": []}},
        }
        nomad_job = {
            "ID": "job-1",
            "Status": "dead",
            "Stop": True,
        }

        queue_item = created_task_with_history
        queue_item.task.alert_on_fail = False
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.execution_request.tracking = {
            "allocation_id": "alloc-1",
            "evaluation_id": "eval-1",
            "job_id": "job-1",
        }

        result = await _build_executor(nomad_job=nomad_job).stop_task(
            session, queue_item
        )

        assert result.status == TaskHistoryStatusEnum.FAILED
        exited_at = datetime.fromtimestamp(exited_at_ns / 10**9, UTC)
        # SQLite returns the value tz-naive, so compare without tzinfo.
        assert result.finished_at.replace(tzinfo=None) == exited_at.replace(tzinfo=None)
        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        mock_annotation.assert_called_once_with(result, "FAILED")

        result_id = result.id
        await session.rollback()
        refetched = await TaskHistoryManager.get_or_404(session, id=result_id)
        assert refetched.status == TaskHistoryStatusEnum.FAILED


class TestSyncTaskHistory:
    """Test NomadExecutor._sync_task_history."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_not_running_returns_early(self, mock_nomad_cls):
        """Assert _sync_task_history returns immediately if status is not RUNNING."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item(status=TaskHistoryStatusEnum.SUCCESS)

        result = await executor._sync_task_history(queue_item)
        assert result.status == TaskHistoryStatusEnum.SUCCESS

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_complete(self, mock_nomad_cls):
        """Assert _sync_task_history updates to SUCCESS when job is dead and alloc complete."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.COMPLETE,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_stale_override(self, mock_nomad_cls):
        """Assert _sync_task_history maps to STALE when check-staleness exited 75."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "TaskStates": {
                "check-staleness": {
                    "Events": [{"Type": "Terminated", "ExitCode": 75}],
                },
            },
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.STALE
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_unlaunchable_override(self, mock_nomad_cls):
        """Assert an aborted launch check maps to UNLAUNCHABLE, not FAILED.

        The prestart step is persistable, so without the arm the failed step
        would derive an ordinary ``FAILED`` — indistinguishable from a script
        that ran and exited non-zero on its own terms.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "TaskStates": {
                "check-launchable": {
                    "Events": [
                        {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE}
                    ],
                },
            },
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.UNLAUNCHABLE
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_stale_wins_over_unlaunchable(self, mock_nomad_cls):
        """Assert a run that was both stale and unlaunchable reports STALE.

        A stale run should not have been dispatched at all, so its verdict
        outranks anything learned about the node it happened to land on.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "TaskStates": {
                "check-staleness": {
                    "Events": [{"Type": "Terminated", "ExitCode": 75}],
                },
                "check-launchable": {
                    "Events": [
                        {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE}
                    ],
                },
            },
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.STALE

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_non_stale_exit_code_preserves_failed(
        self, mock_nomad_cls
    ):
        """Assert _sync_task_history keeps FAILED mapping for non-75 prestart exits."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "TaskStates": {
                "check-staleness": {
                    "Events": [{"Type": "Terminated", "ExitCode": 1}],
                },
            },
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_failed(self, mock_nomad_cls):
        """Assert _sync_task_history updates to FAILED when alloc is failed."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_allocation_not_found_lost(self, mock_nomad_cls):
        """Assert _sync_task_history sets LOST when both allocation and job are gone."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.return_value = []
        nomad_job = _nomad_error(status.HTTP_404_NOT_FOUND)

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-gone",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.LOST

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_allocation_not_found_no_pending_evals(
        self, mock_nomad_cls
    ):
        """Assert _sync_task_history sets FAILED when no alloc and no pending evals."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.return_value = []
        nomad_job = {"ID": "job-1"}
        mock_backend.job.get_evaluations.return_value = [
            {"Status": "complete"},
        ]

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-gone",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.started_at is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_complete_no_modify_time(self, mock_nomad_cls):
        """Assert _sync_task_history uses utc_now when ModifyTime is missing."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.COMPLETE,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": None,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_followup_eval_chain(self, mock_nomad_cls):
        """Assert _sync_task_history follows FollowupEvalID chain."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        first_alloc = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "FollowupEvalID": "eval-followup",
            "ClientStatus": NomadAllocStatusEnum.RUNNING,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": None}},
        }
        followup_alloc = {
            "ID": "alloc-2",
            "JobID": "job-1",
            "EvalID": "eval-followup",
            "ClientStatus": NomadAllocStatusEnum.COMPLETE,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": 1_700_000_000_000_000_000,
        }

        mock_backend.allocation.get_allocation.return_value = first_alloc
        mock_backend.allocations.get_allocations.return_value = [followup_alloc]
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.execution_request.tracking is not None
        assert result.execution_request.tracking["allocation_id"] == "alloc-2"

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_stopped(self, mock_nomad_cls):
        """Assert _sync_task_history maps COMPLETE with Stop=True to STOPPED."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.COMPLETE,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": True,
        }

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)
        assert result.status == TaskHistoryStatusEnum.STOPPED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_sync_task_history_job_lost_after_alloc_found(self, mock_nomad_cls):
        """Assert _sync_task_history sets LOST when job disappears after alloc lookup."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.RUNNING,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": None}},
            "ModifyTime": None,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = _nomad_error(status.HTTP_404_NOT_FOUND)

        executor = _build_executor(nomad_job=nomad_job)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

        result = await executor._sync_task_history(queue_item)
        assert result.status == TaskHistoryStatusEnum.LOST


class TestSyncTaskHistoryWithoutTaskStates:
    """Exercise ``_sync_task_history`` against an allocation with no ``TaskStates``.

    Nomad's reschedule chain lands on an allocation that exists but has not
    started any task once the client running the original allocation goes away.
    Reading ``TaskStates`` off that shape used to raise ``KeyError``, which left
    the row RUNNING forever and made ``stop``, the endpoint meant to clear it,
    fail with a 500.
    """

    @staticmethod
    def _alloc(**overrides: Any) -> dict[str, Any]:
        """Return an allocation dict with no ``TaskStates`` key at all.

        :param overrides: Fields to add to or replace on the allocation.
        :return: The allocation dict as Nomad returned it, ``TaskStates``-less.
        """
        return {
            "ID": "alloc-2",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.PENDING,
        } | overrides

    @staticmethod
    def _queue_item(*, started_at: datetime | None = None) -> TaskHistory:
        """Return a RUNNING task history tracking ``alloc-1``/``job-1``.

        :param started_at: Optional RUNNING entry time used by the pending-
            allocation age bound.
        :return: The task history the sync under test starts from.
        """
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )
        queue_item.started_at = started_at
        return queue_item

    @staticmethod
    def _backend(
        mock_nomad_cls: MagicMock,
        alloc: dict[str, Any],
        job: dict[str, Any] | None = None,
    ) -> MagicMock:
        """Wire a Nomad backend mock returning ``alloc`` and a live job.

        :param mock_nomad_cls: The patched ``Nomad`` class.
        :param alloc: The allocation to return from both lookup paths.
        :param job: The job to return; defaults to a still-running job.
        :return: The backend mock.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = alloc
        mock_backend.allocations.get_allocations.return_value = [alloc]
        mock_backend.client.stream_logs.stream.return_value = ""
        _stub_nomad_calls(
            job=job
            or {
                "ID": "job-1",
                "Status": "running",
                "Stop": False,
            }
        )
        return mock_backend

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_stays_running(self, mock_nomad_cls):
        """Assert a still-starting allocation does not raise and remains RUNNING."""
        mock_backend = self._backend(mock_nomad_cls, self._alloc())
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.finished_at is None
        assert result.execution_request.tracking is not None
        assert result.execution_request.tracking["task_states"] == {}
        assert result.execution_request.tracking["allocation_id"] == "alloc-2"
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_followup_eval_chain_does_not_raise(self, mock_nomad_cls):
        """Assert the reschedule chain tolerates a ``TaskStates``-less successor."""
        first_alloc = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "FollowupEvalID": "eval-followup",
            "ClientStatus": NomadAllocStatusEnum.LOST,
            "TaskStates": {"step1": {"StartedAt": "1", "FinishedAt": None}},
        }
        mock_backend = self._backend(
            mock_nomad_cls, self._alloc(EvalID="eval-followup")
        )
        mock_backend.allocation.get_allocation.return_value = first_alloc

        executor = _build_executor()
        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.execution_request.tracking is not None
        assert result.execution_request.tracking["allocation_id"] == "alloc-2"

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lost_client_status_reaches_terminal_status(self, mock_nomad_cls):
        """Assert a lost allocation moves the row to LOST with a finish time."""
        self._backend(
            mock_nomad_cls,
            self._alloc(
                ClientStatus=NomadAllocStatusEnum.LOST,
                ModifyTime=1_700_000_000_000_000_000,
            ),
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_unknown_client_status_without_modify_time(self, mock_nomad_cls):
        """Assert an unknown allocation lands LOST and still stamps ``finished_at``."""
        self._backend(
            mock_nomad_cls, self._alloc(ClientStatus=NomadAllocStatusEnum.UNKNOWN)
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_failed_client_status_reaches_failed(self, mock_nomad_cls):
        """Assert a failed allocation with no task states maps to FAILED."""
        self._backend(
            mock_nomad_cls, self._alloc(ClientStatus=NomadAllocStatusEnum.FAILED)
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_absent_client_status_stays_running(self, mock_nomad_cls):
        """Assert a stub without ``ClientStatus`` is never given a terminal status."""
        alloc = self._alloc()
        del alloc["ClientStatus"]
        self._backend(mock_nomad_cls, alloc)
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_populated_task_states_keep_live_row_running(self, mock_nomad_cls):
        """Assert the empty-task-states branch does not touch a started allocation.

        A ``lost`` client status on an allocation that *did* start is left to the
        existing dead-job path, so a live row is not terminated early.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(
                ClientStatus=NomadAllocStatusEnum.LOST,
                TaskStates={"step1": {"StartedAt": "1", "FinishedAt": None}},
            ),
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.finished_at is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_complete_client_status_on_live_job_stays_running(
        self, mock_nomad_cls
    ):
        """Assert ``complete`` alone is never read as a successful run.

        An allocation that started no task produced no output and no exit code,
        so reporting SUCCESS would mislead operators and release any chained
        task waiting on this one. While the job lives the allocation may still
        be starting, so the row is left RUNNING rather than terminated.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(
                ClientStatus=NomadAllocStatusEnum.COMPLETE,
                ModifyTime=1_700_000_000_000_000_000,
            ),
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.finished_at is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stopped_job_reaches_stopped(self, mock_nomad_cls):
        """Assert a deliberately stopped job terminates the row as STOPPED.

        Unlike a bare ``complete``, a stopped job is an operator decision, so
        the row is safe to terminate without task states to corroborate it.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(ClientStatus=NomadAllocStatusEnum.COMPLETE),
            job={"ID": "job-1", "Status": "running", "Stop": True},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STOPPED
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_transition_is_logged(
        self, mock_nomad_cls, caplog: pytest.LogCaptureFixture
    ):
        """Assert the dead-end transition leaves a trace naming the allocation.

        The row changes status without any task state to explain why, so the
        allocation ID and the client status behind the decision must be
        recoverable from the worker log.
        """
        self._backend(
            mock_nomad_cls, self._alloc(ClientStatus=NomadAllocStatusEnum.LOST)
        )
        executor = _build_executor()

        with caplog.at_level(logging.WARNING):
            await executor._sync_task_history(self._queue_item())

        assert "alloc-2" in caplog.text
        assert "no task states" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_periodic_sync_with_writer_session_terminates_and_persists_nothing(
        self,
        mock_nomad_cls,
        session: AsyncSession,
        created_task_with_history: TaskHistory,
    ):
        """Assert the periodic sync leg runs end to end with log persistence on.

        ``sync_running_items`` always supplies a ``writer_session``, so the
        reschedule-frontier reset, the log fetch and the terminal drain all run
        against a ``TaskStates``-less allocation on every tick, the exact call
        shape that raised ``KeyError`` in production. Nothing can be persisted
        for an allocation that started no task, but the row must still leave
        RUNNING.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(
                ClientStatus=NomadAllocStatusEnum.LOST,
                CreateIndex=ALLOCATION_CREATE_INDEX,
            ),
        )
        queue_item = created_task_with_history
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.anonymize_mask = 0
        queue_item.execution_request.tracking = {
            "allocation_id": "alloc-1",
            "evaluation_id": "eval-1",
            "job_id": "job-1",
        }
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        result = await executor._sync_task_history(queue_item, writer_session=session)

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.execution_request.tracking is not None
        assert result.execution_request.tracking["allocation_id"] == "alloc-2"
        chunks = await TaskHistoryLogManager.list_chunks_for_task(session, result.id)
        assert chunks == []

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dead_job_with_complete_alloc_reaches_lost_not_success(
        self, mock_nomad_cls
    ):
        """Assert a dead job never reports SUCCESS for an allocation that ran nothing.

        Once the job is dead nothing will advance the row, so it must reach a
        terminal status; but there is no exit code behind ``complete`` here, and
        SUCCESS would release any chained task and silence ``alert_on_fail``.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(
                ClientStatus=NomadAllocStatusEnum.COMPLETE,
                ModifyTime=1_700_000_000_000_000_000,
            ),
            job={"ID": "job-1", "Status": NOMAD_DEAD_JOB_STATUS, "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dead_job_without_client_status_reaches_lost(self, mock_nomad_cls):
        """Assert a dead job resolves the row even with no ``ClientStatus`` to read.

        The live-job branch reads this key defensively; reading it unguarded
        here would raise ``KeyError`` one line below the ``TaskStates`` read
        this class exists to guard, wedging the row exactly as before.
        """
        alloc = self._alloc(ModifyTime=1_700_000_000_000_000_000)
        del alloc["ClientStatus"]
        self._backend(
            mock_nomad_cls,
            alloc,
            job={"ID": "job-1", "Status": NOMAD_DEAD_JOB_STATUS, "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dead_job_with_lost_alloc_keeps_its_own_status(self, mock_nomad_cls):
        """Assert a dead-end status the allocation does report is carried through.

        Only a status outside the dead-end set is rewritten to LOST, so a
        ``failed`` allocation still lands FAILED rather than being flattened.
        """
        self._backend(
            mock_nomad_cls,
            self._alloc(ClientStatus=NomadAllocStatusEnum.FAILED),
            job={"ID": "job-1", "Status": NOMAD_DEAD_JOB_STATUS, "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_within_bound_stays_running(
        self, mock_nomad_cls, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert a still-starting pending allocation is left RUNNING before the bound."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = self._backend(mock_nomad_cls, self._alloc())
        executor = _build_executor()
        started_at = utc_now() - timedelta(seconds=PENDING_ALLOCATION_WITHIN_BOUND_AGE)

        result = await executor._sync_task_history(
            self._queue_item(started_at=started_at)
        )

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.finished_at is None
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.utc_now")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_exceeds_bound_escalates_to_lost(
        self, mock_nomad_cls, mock_utc_now: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert a TaskStates-less pending allocation past the bound becomes LOST."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = self._backend(
            mock_nomad_cls,
            self._alloc(ModifyTime=1_700_000_000_000_000_000),
        )
        executor = _build_executor()
        now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        mock_utc_now.return_value = now
        started_at = now - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)

        result = await executor._sync_task_history(
            self._queue_item(started_at=started_at)
        )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at == now
        assert result.failure_reason == "Execution tracking lost."
        mock_backend.job.deregister_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_escalation_is_logged(
        self,
        mock_nomad_cls,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Assert the age-bound escalation leaves a recoverable worker-log trace."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        self._backend(mock_nomad_cls, self._alloc())
        executor = _build_executor()
        started_at = utc_now() - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)

        with caplog.at_level(logging.WARNING):
            await executor._sync_task_history(self._queue_item(started_at=started_at))

        assert "alloc-2" in caplog.text
        assert "pending-allocation timeout" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_escalation_uses_configured_bound(
        self, mock_nomad_cls, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert an age exactly equal to the configured bound escalates."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_BOUNDARY_AGE,
        )
        mock_backend = self._backend(mock_nomad_cls, self._alloc())
        executor = _build_executor()
        started_at = utc_now() - timedelta(seconds=PENDING_ALLOCATION_BOUNDARY_AGE)

        result = await executor._sync_task_history(
            self._queue_item(started_at=started_at)
        )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at is not None
        mock_backend.job.deregister_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_escalation_deregister_failure_still_lands_lost(
        self,
        mock_nomad_cls,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Assert a Nomad hiccup on deregister still stamps LOST.

        Swallowing keeps the duplicate-dispatch 409 from surviving a failed
        reap; the job may still be placed, but that is logged rather than
        re-blocking the row.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = self._backend(mock_nomad_cls, self._alloc())
        mock_backend.job.deregister_job.side_effect = BaseNomadException(
            MagicMock(text="gone")
        )
        executor = _build_executor()
        started_at = utc_now() - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)

        with caplog.at_level(logging.WARNING):
            result = await executor._sync_task_history(
                self._queue_item(started_at=started_at)
            )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at is not None
        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        assert "Could not deregister job job-1" in caplog.text
        assert "marking LOST anyway" in caplog.text

    @pytest.mark.asyncio
    async def test_should_escalate_pending_allocation_coerces_naive_started_at(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session: AsyncSession,
        created_task_with_history: TaskHistory,
    ):
        """Assert the age bound compares safely after an ORM load strips tzinfo.

        ``DateTimeWithTimezone`` does not coerce on load; SQLite and MySQL return
        ``started_at`` tz-naive. The sync path loads through ``get_or_404``, so the
        predicate must tolerate that shape.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        started_at = utc_now() - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)
        queue_item = created_task_with_history
        queue_item.status = TaskHistoryStatusEnum.RUNNING
        queue_item.started_at = started_at
        await TaskHistoryManager.save(session, queue_item)

        reloaded = await TaskHistoryManager.get_or_404(session, id=queue_item.id)
        assert reloaded.started_at is not None
        assert reloaded.started_at.tzinfo is None

        assert _build_executor()._should_escalate_pending_allocation(reloaded) is True


class TestSyncTaskHistoryQueuedEvaluations:
    """Test ``_sync_task_history`` on a row whose job has no allocation yet.

    A Nomad client without capacity completes the dispatch evaluation and parks
    the work in a ``blocked`` evaluation, recorded as the dispatch eval's
    ``BlockedEval``. Once capacity frees, the allocation Nomad places carries the
    *blocked* eval's id, not the dispatch eval the row tracks.
    """

    _QUEUED_EVALUATIONS = [
        {"ID": "eval-1", "Status": "complete", "BlockedEval": "eval-2"},
        {"ID": "eval-2", "Status": "blocked"},
    ]

    @staticmethod
    def _queue_item(
        *,
        started_at: datetime | None = None,
        tracking: dict[str, Any] | None = None,
    ) -> TaskHistory:
        """Return a RUNNING task history tracking dispatch eval ``eval-1``.

        :param started_at: Optional RUNNING entry time used by the age bound.
        :param tracking: Tracking to use instead of the allocation-less default.
        :return: The task history the sync under test starts from.
        """
        queue_item = _build_queue_item(
            tracking=tracking
            or {"allocation_id": None, "evaluation_id": "eval-1", "job_id": "job-1"},
            status=TaskHistoryStatusEnum.RUNNING,
        )
        queue_item.started_at = started_at
        return queue_item

    @staticmethod
    def _backend(
        mock_nomad_cls: MagicMock,
        evaluations: list[dict[str, Any]],
        placed: dict[str, list[dict[str, Any]]] | None = None,
        job: Any = None,
    ) -> MagicMock:
        """Wire a Nomad backend whose allocations are keyed by evaluation id.

        The job read goes through ``nomad_json`` rather than ``backend.job``, so
        it is stubbed with :func:`_stub_nomad_calls` like the other ``_backend``
        helpers here; the evaluations and allocations are still python-nomad.

        :param mock_nomad_cls: The patched ``Nomad`` class.
        :param evaluations: The job's evaluations.
        :param placed: Allocations Nomad returns per ``EvalID`` filter; any
            other filter finds none.
        :param job: Answer for ``GET /v1/job/{id}``, an exception included.
            Defaults to a still-running dispatched job.
        :return: The backend mock.
        """
        placed = placed or {}

        def get_allocations(*, filter_: str, reverse: bool) -> list[dict[str, Any]]:
            for eval_id, allocations in placed.items():
                if f'EvalID == "{eval_id}"' in filter_:
                    return allocations
            return []

        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.side_effect = get_allocations
        mock_backend.client.stream_logs.stream.return_value = ""
        mock_backend.job.get_evaluations.return_value = evaluations
        _stub_nomad_calls(
            job=job
            or {
                "ID": "job-1",
                "Status": "running",
                "Stop": False,
                "Dispatched": True,
            }
        )
        return mock_backend

    @staticmethod
    def _running_alloc(eval_id: str, alloc_id: str = "alloc-9") -> dict[str, Any]:
        """Return a running allocation placed by ``eval_id``.

        :param eval_id: The evaluation that placed the allocation.
        :param alloc_id: The allocation id.
        :return: The allocation dict as Nomad returned it.
        """
        return {
            "ID": alloc_id,
            "JobID": "job-1",
            "EvalID": eval_id,
            "ClientStatus": NomadAllocStatusEnum.RUNNING,
            "TaskStates": {"step1": {"State": "running", "StartedAt": "1"}},
        }

    @staticmethod
    def _within_bound(monkeypatch: pytest.MonkeyPatch) -> datetime:
        """Shorten the age bound and return a ``started_at`` still inside it.

        :param monkeypatch: The pytest monkeypatch fixture.
        :return: The RUNNING entry time.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        return utc_now() - timedelta(seconds=PENDING_ALLOCATION_WITHIN_BOUND_AGE)

    @staticmethod
    def _past_bound(monkeypatch: pytest.MonkeyPatch) -> datetime:
        """Shorten the age bound and return a ``started_at`` already past it.

        :param monkeypatch: The pytest monkeypatch fixture.
        :return: The RUNNING entry time.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        return utc_now() - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_blocked_evaluation_within_bound_stays_running(
        self, mock_nomad_cls, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert work queued behind a blocked evaluation waits its turn."""
        mock_backend = self._backend(mock_nomad_cls, self._QUEUED_EVALUATIONS)
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(started_at=self._within_bound(monkeypatch))
        )

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert result.failure_reason is None
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_blocked_evaluation_without_tracked_eval_stays_running(
        self, mock_nomad_cls, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert liveness alone keeps a row RUNNING when tracking names no eval."""
        mock_backend = self._backend(mock_nomad_cls, self._QUEUED_EVALUATIONS)
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(
                started_at=self._within_bound(monkeypatch),
                tracking={"allocation_id": None, "job_id": "job-1"},
            )
        )

        assert result.status == TaskHistoryStatusEnum.RUNNING
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_unblocked_allocation_is_adopted(self, mock_nomad_cls):
        """Assert the allocation a blocked evaluation places becomes the row's."""
        mock_backend = self._backend(
            mock_nomad_cls,
            self._QUEUED_EVALUATIONS,
            placed={"eval-2": [self._running_alloc("eval-2")]},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        tracking = result.execution_request.tracking
        assert tracking is not None
        assert tracking["evaluation_id"] == "eval-2"
        assert tracking["allocation_id"] == "alloc-9"
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_two_hop_blocked_chain_allocation_is_adopted(self, mock_nomad_cls):
        """Assert the walk follows ``BlockedEval`` past the first hop."""
        mock_backend = self._backend(
            mock_nomad_cls,
            [
                {"ID": "eval-1", "Status": "complete", "BlockedEval": "eval-2"},
                {"ID": "eval-2", "Status": "complete", "BlockedEval": "eval-3"},
                {"ID": "eval-3", "Status": "complete"},
            ],
            placed={"eval-3": [self._running_alloc("eval-3")]},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        tracking = result.execution_request.tracking
        assert tracking is not None
        assert tracking["evaluation_id"] == "eval-3"
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_placed_after_first_lookup_is_adopted(
        self, mock_nomad_cls
    ):
        """Assert a placement landing between the two reads is not withdrawn.

        The first lookup by the tracked eval finds nothing; by the time the
        evaluations are read the eval is complete and its allocation exists.
        """
        mock_backend = self._backend(
            mock_nomad_cls, [{"ID": "eval-1", "Status": "complete"}]
        )
        mock_backend.allocations.get_allocations.side_effect = [
            [],
            [self._running_alloc("eval-1")],
        ]
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.RUNNING
        tracking = result.execution_request.tracking
        assert tracking is not None
        assert tracking["allocation_id"] == "alloc-9"
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.parametrize(
        "evaluations",
        [
            pytest.param(_QUEUED_EVALUATIONS, id="blocked"),
            pytest.param([{"ID": "eval-1", "Status": "pending"}], id="pending"),
        ],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.utc_now")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_live_evaluation_past_bound_withdraws_and_lands_lost(
        self,
        mock_nomad_cls,
        mock_utc_now: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        evaluations: list[dict[str, Any]],
    ):
        """Assert work still queued at the bound is withdrawn and ends LOST."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = self._backend(mock_nomad_cls, evaluations)
        now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
        mock_utc_now.return_value = now
        started_at = now - timedelta(seconds=PENDING_ALLOCATION_PAST_BOUND_AGE)
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(started_at=started_at)
        )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.finished_at == now
        assert result.failure_reason == "Execution tracking lost."
        mock_backend.job.deregister_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_live_evaluation_withdraw_failure_still_lands_lost(
        self,
        mock_nomad_cls,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Assert a Nomad error on the withdrawal is logged and the row still ends."""
        mock_backend = self._backend(mock_nomad_cls, self._QUEUED_EVALUATIONS)
        mock_backend.job.deregister_job.side_effect = BaseNomadException(
            MagicMock(text="unavailable")
        )
        executor = _build_executor()

        with caplog.at_level(logging.WARNING):
            result = await executor._sync_task_history(
                self._queue_item(started_at=self._past_bound(monkeypatch))
            )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert "Could not deregister job job-1 for task history 10" in caplog.text
        assert "marking LOST anyway" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_evaluations_withdraw_then_fail(self, mock_nomad_cls):
        """Assert a job with no allocation and no live eval is withdrawn, then FAILED."""
        mock_backend = self._backend(
            mock_nomad_cls,
            [{"Status": "complete"}, {"Status": "failed"}, {"Status": "canceled"}],
        )
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(started_at=utc_now())
        )

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason == (
            "The executor job produced no allocation and has no pending evaluation."
        )
        assert result.started_at is None
        mock_backend.job.deregister_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_evaluations_withdraw_failure_still_fails(
        self, mock_nomad_cls, caplog: pytest.LogCaptureFixture
    ):
        """Assert a Nomad error on the withdrawal is logged and the row still FAILS."""
        mock_backend = self._backend(mock_nomad_cls, [{"Status": "complete"}])
        mock_backend.job.deregister_job.side_effect = BaseNomadException(
            MagicMock(text="unavailable")
        )
        executor = _build_executor()

        with caplog.at_level(logging.WARNING):
            result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert "Could not deregister job job-1 for task history 10" in caplog.text
        assert "marking FAILED anyway" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_blocked_eval_cycle_terminates_and_fails(self, mock_nomad_cls):
        """Assert a ``BlockedEval`` cycle ends the walk instead of looping."""
        mock_backend = self._backend(
            mock_nomad_cls,
            [
                {"ID": "eval-1", "Status": "complete", "BlockedEval": "eval-2"},
                {"ID": "eval-2", "Status": "complete", "BlockedEval": "eval-1"},
            ],
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED
        mock_backend.job.deregister_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lost_job_is_not_withdrawn(self, mock_nomad_cls):
        """Assert a job Nomad no longer has ends LOST with nothing to deregister."""
        mock_backend = self._backend(
            mock_nomad_cls, [], job=_nomad_error(status.HTTP_404_NOT_FOUND)
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.LOST
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.parametrize(
        ("age", "expected_status", "expected_deregisters"),
        [
            pytest.param(
                PENDING_ALLOCATION_WITHIN_BOUND_AGE,
                TaskHistoryStatusEnum.RUNNING,
                0,
                id="within-bound",
            ),
            pytest.param(
                PENDING_ALLOCATION_PAST_BOUND_AGE,
                TaskHistoryStatusEnum.LOST,
                1,
                id="past-bound",
            ),
        ],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_unplaced_reschedule_falls_through_to_liveness(
        self,
        mock_nomad_cls,
        monkeypatch: pytest.MonkeyPatch,
        age: int,
        expected_status: TaskHistoryStatusEnum,
        expected_deregisters: int,
    ):
        """Assert a reschedule whose successor is still blocked obeys the bound."""
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        rescheduled = self._running_alloc("eval-1", alloc_id="alloc-1") | {
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "FollowupEvalID": "eval-f1",
        }
        mock_backend = self._backend(
            mock_nomad_cls,
            [
                {"ID": "eval-1", "Status": "complete"},
                {"ID": "eval-f1", "Status": "complete", "BlockedEval": "eval-f2"},
                {"ID": "eval-f2", "Status": "blocked"},
            ],
            placed={"eval-1": [rescheduled]},
        )
        mock_backend.allocation.get_allocation.side_effect = None
        mock_backend.allocation.get_allocation.return_value = rescheduled
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(
                started_at=utc_now() - timedelta(seconds=age),
                tracking={
                    "allocation_id": "alloc-1",
                    "evaluation_id": "eval-1",
                    "job_id": "job-1",
                },
            )
        )

        assert result.status == expected_status
        assert mock_backend.job.deregister_job.call_count == expected_deregisters

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_reschedule_placed_via_blocked_followup_is_adopted(
        self, mock_nomad_cls
    ):
        """Assert a reschedule queued for capacity is followed to its placement.

        The follow-up evaluation finds no capacity, completes and parks the
        replacement in a blocked evaluation, which places it once capacity
        frees, so the replacement carries the blocked evaluation's id.
        """
        rescheduled = self._running_alloc("eval-1", alloc_id="alloc-1") | {
            "ClientStatus": NomadAllocStatusEnum.FAILED,
            "FollowupEvalID": "eval-f1",
        }
        mock_backend = self._backend(
            mock_nomad_cls,
            [
                {"ID": "eval-1", "Status": "complete"},
                {"ID": "eval-f1", "Status": "complete", "BlockedEval": "eval-f2"},
                {"ID": "eval-f2", "Status": "complete"},
            ],
            placed={
                "eval-1": [rescheduled],
                "eval-f2": [self._running_alloc("eval-f2")],
            },
        )
        mock_backend.allocation.get_allocation.side_effect = None
        mock_backend.allocation.get_allocation.return_value = rescheduled
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(
                tracking={
                    "allocation_id": "alloc-1",
                    "evaluation_id": "eval-1",
                    "job_id": "job-1",
                },
            )
        )

        assert result.status == TaskHistoryStatusEnum.RUNNING
        tracking = result.execution_request.tracking
        assert tracking is not None
        assert tracking["allocation_id"] == "alloc-9"
        assert tracking["evaluation_id"] == "eval-f2"
        mock_backend.job.deregister_job.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_deepest_chain_allocation_wins(self, mock_nomad_cls):
        """Assert the blocked evaluation's placement is preferred to the tracked one's.

        The tracked evaluation's allocation lands only after the first lookup,
        so both are visible to the chain walk and only its order decides.
        """
        mock_backend = self._backend(
            mock_nomad_cls,
            self._QUEUED_EVALUATIONS,
            placed={"eval-2": [self._running_alloc("eval-2")]},
        )
        keyed_lookup = mock_backend.allocations.get_allocations.side_effect
        tracked_lookups: list[str] = []

        def get_allocations(*, filter_: str, reverse: bool) -> list[dict[str, Any]]:
            if 'EvalID == "eval-1"' in filter_:
                tracked_lookups.append(filter_)
                if len(tracked_lookups) > 1:
                    return [self._running_alloc("eval-1", alloc_id="alloc-old")]
                return []
            return keyed_lookup(filter_=filter_, reverse=reverse)

        mock_backend.allocations.get_allocations.side_effect = get_allocations
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        tracking = result.execution_request.tracking
        assert tracking is not None
        assert tracking["allocation_id"] == "alloc-9"

    @pytest.mark.parametrize(
        ("evaluations", "age", "expected_status"),
        [
            pytest.param(
                [{"ID": "eval-1", "Status": "complete"}],
                PENDING_ALLOCATION_WITHIN_BOUND_AGE,
                TaskHistoryStatusEnum.FAILED,
                id="terminal-evaluations",
            ),
            pytest.param(
                _QUEUED_EVALUATIONS,
                PENDING_ALLOCATION_PAST_BOUND_AGE,
                TaskHistoryStatusEnum.LOST,
                id="live-evaluation-past-bound",
            ),
        ],
    )
    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_job_shared_across_runs_is_not_withdrawn(
        self,
        mock_nomad_cls,
        monkeypatch: pytest.MonkeyPatch,
        evaluations: list[dict[str, Any]],
        age: int,
        expected_status: TaskHistoryStatusEnum,
    ):
        """Assert a job that is not a per-run dispatch is left registered.

        A non-parameterized task registers one job per task and target, so its
        other runs' work lives in the same job and deregistering it would stop
        them too.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = self._backend(
            mock_nomad_cls,
            evaluations,
            job={
                "ID": "job-1",
                "Status": "running",
                "Stop": False,
                "Dispatched": False,
            },
        )
        executor = _build_executor()

        result = await executor._sync_task_history(
            self._queue_item(started_at=utc_now() - timedelta(seconds=age))
        )

        assert result.status == expected_status
        mock_backend.job.deregister_job.assert_not_called()


class TestSyncTaskHistoryFailureReason:
    """Test the reason ``_sync_task_history`` stores alongside each terminal status."""

    @staticmethod
    def _queue_item() -> TaskHistory:
        """Return a RUNNING task history tracking ``alloc-1``/``job-1``."""
        return _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

    @staticmethod
    def _backend(
        mock_nomad_cls: MagicMock,
        task_states: dict[str, Any] | None,
        client_status: str = NomadAllocStatusEnum.FAILED,
        *,
        stop: bool = False,
    ) -> None:
        """Wire a backend returning a dead job and one allocation."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        alloc = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": client_status,
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        if task_states is not None:
            alloc["TaskStates"] = task_states
        mock_backend.allocation.get_allocation.return_value = alloc
        mock_backend.allocations.get_allocations.return_value = [alloc]
        mock_backend.client.stream_logs.stream.return_value = ""
        _stub_nomad_calls(
            job={
                "ID": "job-1",
                "Status": NOMAD_DEAD_JOB_STATUS,
                "Stop": stop,
            }
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_failed_step_reason_is_stored(self, mock_nomad_cls):
        """Assert a failed producing step becomes the stored reason."""
        self._backend(
            mock_nomad_cls,
            {
                "run-script": {
                    "Failed": True,
                    "Events": [{"Type": "Terminated", "ExitCode": 1}],
                },
            },
        )

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason == "Step 'run-script' failed (exit code 1)."

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_failed_without_named_step_stores_no_reason(self, mock_nomad_cls):
        """Assert a failure naming no failed producing step records no reason."""
        self._backend(mock_nomad_cls, {"run-script": {"StartedAt": "1"}})

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stale_sentinel_stores_canned_prose(self, mock_nomad_cls):
        """Assert a stale-skip sentinel stores the STALE prose."""
        self._backend(
            mock_nomad_cls,
            {"check-staleness": {"Events": [{"Type": "Terminated", "ExitCode": 75}]}},
        )

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STALE
        assert result.failure_reason == (
            "Skipped as stale (executor placement delayed past threshold)."
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_unlaunchable_sentinel_stores_canned_prose(self, mock_nomad_cls):
        """Assert an unlaunchable sentinel stores the UNLAUNCHABLE prose."""
        self._backend(
            mock_nomad_cls,
            {
                _LAUNCH_CHECK_TASK_NAME: {
                    "Events": [
                        {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE}
                    ]
                }
            },
        )

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.UNLAUNCHABLE
        assert result.failure_reason == (
            "Could not be launched (the executor node cannot run the "
            "requested command)."
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_success_stores_no_reason(self, mock_nomad_cls):
        """Assert a successful sync leaves failure_reason unset."""
        self._backend(
            mock_nomad_cls,
            {"run-script": {"StartedAt": "1", "FinishedAt": "2"}},
            client_status=NomadAllocStatusEnum.COMPLETE,
        )

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.failure_reason is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stopped_stores_no_reason(self, mock_nomad_cls):
        """Assert a stopped run stores no reason."""
        self._backend(
            mock_nomad_cls,
            {"run-script": {"StartedAt": "1", "FinishedAt": "2"}},
            client_status=NomadAllocStatusEnum.COMPLETE,
            stop=True,
        )

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STOPPED
        assert result.failure_reason is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_malformed_allocation_does_not_break_the_sync(self, mock_nomad_cls):
        """Assert shape drift costs a precise reason, not the sync itself."""
        self._backend(mock_nomad_cls, None)

        result = await _build_executor()._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.failure_reason is None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_less_failure_states_its_own_reason(self, mock_nomad_cls):
        """Assert the allocation-less failure names the missing allocation."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.return_value = []
        nomad_job = {"ID": "job-1"}
        mock_backend.job.get_evaluations.return_value = [{"Status": "complete"}]

        result = await _build_executor(nomad_job=nomad_job)._sync_task_history(
            self._queue_item()
        )

        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.started_at is None
        assert result.failure_reason == (
            "The executor job produced no allocation and has no pending evaluation."
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lost_job_stores_the_lost_prose(self, mock_nomad_cls):
        """Assert a job that vanished stores the LOST prose."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="not found")
        )
        mock_backend.allocations.get_allocations.return_value = []
        nomad_job = _nomad_error(status.HTTP_404_NOT_FOUND)

        result = await _build_executor(nomad_job=nomad_job)._sync_task_history(
            self._queue_item()
        )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.failure_reason == "Execution tracking lost."

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pending_allocation_timeout_stores_the_lost_prose(
        self, mock_nomad_cls, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert pending-allocation escalation stores the LOST prose.

        This is the only ``_apply_terminal_status`` arm that previously stamped
        LOST without ``set_failure_reason``; operators see ``failure_reason``
        through the history field list, and ``None`` there means unknown.
        """
        monkeypatch.setattr(
            tasks_settings,
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            PENDING_ALLOCATION_TIMEOUT_OVERRIDE,
        )
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        alloc = {
            "ID": "alloc-2",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.PENDING,
        }
        mock_backend.allocation.get_allocation.return_value = alloc
        mock_backend.allocations.get_allocations.return_value = [alloc]
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": "running",
            "Stop": False,
        }
        queue_item = self._queue_item()
        queue_item.started_at = utc_now() - timedelta(
            seconds=PENDING_ALLOCATION_PAST_BOUND_AGE
        )

        result = await _build_executor(nomad_job=nomad_job)._sync_task_history(
            queue_item
        )

        assert result.status == TaskHistoryStatusEnum.LOST
        assert result.failure_reason == "Execution tracking lost."


class TestStampFinishedAt:
    """Exercise ``NomadExecutor._stamp_finished_at``."""

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_uses_allocation_modify_time(self, mock_nomad_cls):
        """Assert a modification time is converted to the finish timestamp."""
        mock_nomad_cls.return_value = MagicMock()
        queue_item = _build_queue_item()

        _build_executor()._stamp_finished_at(
            queue_item, {"ID": "alloc-1", "ModifyTime": 1_700_000_000_000_000_000}
        )

        assert queue_item.finished_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)

    @pytest.mark.parametrize(
        "alloc",
        [
            {"ID": "alloc-1"},
            {"ID": "alloc-1", "ModifyTime": None},
            {"ID": "alloc-1", "ModifyTime": 0},
        ],
        ids=["absent", "none", "epoch-zero"],
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_falls_back_to_now_without_usable_modify_time(
        self, mock_nomad_cls, alloc: dict[str, Any]
    ):
        """Assert an unusable modification time still stamps a finish time.

        ``ModifyTime: 0`` would convert to the Unix epoch, which reads as a task
        that finished in 1970; the fallback keeps the row's finish time close to
        when the executor actually observed the allocation.
        """
        mock_nomad_cls.return_value = MagicMock()
        queue_item = _build_queue_item()
        before = utc_now()

        _build_executor()._stamp_finished_at(queue_item, alloc)

        assert queue_item.finished_at is not None
        assert queue_item.finished_at >= before


class TestTaskNeedsJobRegister:
    """Test NomadExecutor.task_needs_job_register."""

    @pytest.mark.asyncio
    async def test_non_parameterized_always_true(self) -> None:
        """Assert non-parameterized tasks always need registration."""
        executor = _build_executor()
        task = _build_task(parameterized=False)
        assert await executor.task_needs_job_register(task) is True

    @pytest.mark.asyncio
    async def test_parameterized_job_not_found(self) -> None:
        """Assert True when parameterized job doesn't exist."""
        nomad_job = _nomad_error(status.HTTP_404_NOT_FOUND)

        executor = _build_executor(nomad_job=nomad_job)
        task = _build_task(parameterized=True)
        assert await executor.task_needs_job_register(task) is True

    @pytest.mark.asyncio
    async def test_parameterized_job_stale(self) -> None:
        """Assert True when existing job is older than task update time."""
        old_timestamp = 1_600_000_000_000_000_000
        nomad_job = {
            "ID": "job-1",
            "SubmitTime": old_timestamp,
        }

        executor = _build_executor(nomad_job=nomad_job)
        task = _build_task(parameterized=True)
        task.updated_at = datetime(2024, 1, 1, tzinfo=UTC)
        assert await executor.task_needs_job_register(task) is True

    @pytest.mark.asyncio
    async def test_parameterized_job_fresh(self) -> None:
        """Assert False when existing job is newer than task update time."""
        recent_timestamp = 2_000_000_000_000_000_000
        nomad_job = {
            "ID": "job-1",
            "SubmitTime": recent_timestamp,
        }

        executor = _build_executor(nomad_job=nomad_job)
        task = _build_task(parameterized=True)
        task.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
        assert await executor.task_needs_job_register(task) is False

    @pytest.mark.asyncio
    async def test_parameterized_job_no_submit_time(self) -> None:
        """Assert True when existing job has no SubmitTime."""
        nomad_job = {
            "ID": "job-1",
            "SubmitTime": None,
        }

        executor = _build_executor(nomad_job=nomad_job)
        task = _build_task(parameterized=True)
        assert await executor.task_needs_job_register(task) is True


class TestValidateJob:
    """Test NomadExecutor.validate_job."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.async_run")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_validate_job_success(self, mock_nomad_cls, mock_async_run):
        """Assert validate_job returns job when validation passes."""
        mock_response = MagicMock()
        mock_response.status_code = status.HTTP_200_OK
        mock_response.text = json.dumps({"ValidationErrors": []})
        mock_async_run.return_value = mock_response

        executor = _build_executor()
        job = {"ID": "test-job", "Type": "batch"}
        result = await executor.validate_job(job)
        assert result == job

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.async_run")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_validate_job_validation_errors(self, mock_nomad_cls, mock_async_run):
        """Assert validate_job raises HTTPBadRequestException on validation errors."""
        mock_response = MagicMock()
        mock_response.status_code = status.HTTP_200_OK
        mock_response.text = json.dumps(
            {"ValidationErrors": ["missing required field"]}
        )
        mock_async_run.return_value = mock_response

        executor = _build_executor()
        with pytest.raises(HTTPBadRequestException):
            await executor.validate_job({"ID": "bad-job"})

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.async_run")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_validate_job_non_200_raises(self, mock_nomad_cls, mock_async_run):
        """Assert validate_job raises HTTPBadRequestException on non-200 status."""
        mock_response = MagicMock()
        mock_response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        mock_async_run.return_value = mock_response

        executor = _build_executor()
        with pytest.raises(HTTPBadRequestException):
            await executor.validate_job({"ID": "error-job"})


class TestGetLogsForAllocation:
    """Test NomadExecutor.get_logs_for_allocation."""

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation(self, mock_nomad_cls):
        """Assert get_logs_for_allocation decodes base64 logs for each step."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        raw_msg = b64encode(b"Hello from step1").decode()
        log_data = json.dumps({"Data": raw_msg, "Offset": 100})
        mock_backend.client.stream_logs.stream.return_value = log_data

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"step1": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(alloc)

        assert "step1" in result
        assert "Hello from step1" in result["step1"]["stdout"]

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_no_task_states(self, mock_nomad_cls):
        """Assert get_logs_for_allocation returns empty when no task states."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        alloc = {"ID": "alloc-1", "TaskStates": None}
        result = executor.get_logs_for_allocation(alloc)
        assert dict(result) == {}

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_absent_task_states(self, mock_nomad_cls):
        """Assert an allocation without a ``TaskStates`` key yields no logs.

        This read runs on the Celery sync path (``writer_session`` is supplied
        there), so an absent key must not raise either.
        """
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        result = executor.get_logs_for_allocation({"ID": "alloc-1"})
        assert dict(result) == {}

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_non_mapping_step_state(
        self, mock_nomad_cls, caplog: pytest.LogCaptureFixture
    ):
        """Assert a non-mapping task state is skipped rather than raising.

        Reading ``StartedAt`` off it directly would raise ``AttributeError`` on
        the Celery sync path, which is the failure mode the container guard
        already removes one level up.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        executor = _build_executor()

        with caplog.at_level(logging.WARNING):
            result = executor.get_logs_for_allocation(
                {"ID": "alloc-1", "TaskStates": {"broken-step": "not a mapping"}}
            )

        assert dict(result) == {}
        mock_backend.client.stream_logs.stream.assert_not_called()
        assert "non-mapping task state" in caplog.text

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_exception_handling(self, mock_nomad_cls):
        """Assert get_logs_for_allocation returns empty streams when stream_logs raises."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = BaseNomadException(
            MagicMock(text="stream error")
        )

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"step1": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(alloc)

        assert "step1" in result
        assert result["step1"]["stdout"] == ""
        assert result["step1"]["stderr"] == ""

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_with_anonymization(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert get_logs_for_allocation applies anonymization for run-script step."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.return_value = "REDACTED"

        raw_msg = b64encode(b"sensitive data\n").decode()
        log_data = json.dumps({"Data": raw_msg, "Offset": 100})
        mock_backend.client.stream_logs.stream.return_value = log_data

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.PERSON}
        )

        assert "REDACTED" in result["run-script"]["stdout"]
        mock_anonymize.assert_called()

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_with_initial_offsets(self, mock_nomad_cls):
        """Assert get_logs_for_allocation starts Nomad reads at the given offsets.

        The fetcher returns only the delta for this cycle — content keys in
        ``initial_logs`` are ignored; only ``f"{log_type}_last_offset"`` keys
        are read.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"step1": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }
        initial_logs = {
            "step1": {
                "stdout_last_offset": INITIAL_LOG_OFFSET,
                "stderr_last_offset": INITIAL_LOG_OFFSET,
            }
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(alloc, initial_logs=initial_logs)

        assert result["step1"]["stdout"] == ""
        assert result["step1"]["stderr"] == ""
        assert result["step1"]["stdout_last_offset"] == INITIAL_LOG_OFFSET
        assert result["step1"]["stderr_last_offset"] == INITIAL_LOG_OFFSET
        stream_call_kwargs = [
            call.kwargs
            for call in mock_backend.client.stream_logs.stream.call_args_list
        ]
        assert all(
            kwargs["offset"] == INITIAL_LOG_OFFSET for kwargs in stream_call_kwargs
        )

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_drops_carried_over_content(self, mock_nomad_cls):
        """Assert content carried in ``initial_logs`` is dropped, not returned again.

        Only the offset bookkeeping is carried forward between cycles: a caller
        that hands back the content it already persisted must not receive it in
        this cycle's delta, or the writer appends the same bytes twice. A step
        that never started makes that visible, since no fresh delta overwrites
        the carried key.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"pending-step": {"StartedAt": None}},
        }
        initial_logs = {
            "pending-step": {
                "stdout": "already persisted",
                TaskLogType.STDERR: "already persisted",
                "stdout_last_offset": INITIAL_LOG_OFFSET,
            }
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(alloc, initial_logs=initial_logs)

        assert dict(result["pending-step"]) == {
            "stdout_last_offset": INITIAL_LOG_OFFSET
        }
        mock_backend.client.stream_logs.stream.assert_not_called()

    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_skips_step_when_not_started(self, mock_nomad_cls):
        """Assert steps with StartedAt None do not call stream_logs."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""

        alloc = {
            "ID": "alloc-1",
            "TaskStates": {
                "pending-step": {"StartedAt": None},
                "ready-step": {"StartedAt": "2024-01-01T00:00:00Z"},
            },
        }

        executor = _build_executor()
        executor.get_logs_for_allocation(alloc)

        stream = mock_backend.client.stream_logs.stream
        assert stream.call_count == EXPECTED_GET_LOGS_STREAM_CALLS_ONE_READY_STEP
        tasks = {c.kwargs["task"] for c in stream.call_args_list}
        types = {c.kwargs["type_"] for c in stream.call_args_list}
        assert tasks == {"ready-step"}
        assert types == {TaskLogType.STDOUT, TaskLogType.STDERR}

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_anonymized_offsets_track_producer_space(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert producer offset tracks anonymized bytes, not Nomad bytes.

        Regression test: when anonymization replaces raw bytes
        with a shorter or longer string, the producer-space offset returned
        alongside the delta must track the post-anonymization byte length
        so the writer dedup window does not mix Nomad-space and
        anonymized-space counts on retry.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.return_value = "[REDACTED]"
        raw_bytes = b"4111-1111-1111-1111\n"
        raw_msg = b64encode(raw_bytes).decode()
        log_data = json.dumps({"Data": raw_msg, "Offset": len(raw_bytes)})
        mock_backend.client.stream_logs.stream.return_value = log_data
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )

        assert result["run-script"]["stdout"] == "[REDACTED]"
        assert result["run-script"]["stdout_last_offset"] == len(raw_bytes)
        assert result["run-script"]["stdout_producer_offset"] == len("[REDACTED]")

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_anonymized_retry_dedups_correctly(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert a second fetch at the advanced offsets yields no new bytes.

        Simulates a retry after a successful first fetch: the second cycle
        starts at the advanced Nomad offset (so Nomad returns nothing new)
        and passes the advanced producer offset through; the writer caller
        therefore sees a zero-length delta and never re-writes already
        persisted anonymized bytes.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.return_value = "[REDACTED]"
        raw_bytes = b"4111-1111-1111-1111\n"
        raw_msg = b64encode(raw_bytes).decode()
        first_log_data = json.dumps({"Data": raw_msg, "Offset": len(raw_bytes)})
        mock_backend.client.stream_logs.stream.side_effect = [
            first_log_data,
            "",
            "",
            "",
        ]
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        first = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )
        second = executor.get_logs_for_allocation(
            alloc,
            initial_logs={
                "run-script": {
                    "stdout_last_offset": first["run-script"]["stdout_last_offset"],
                    "stdout_producer_offset": first["run-script"][
                        "stdout_producer_offset"
                    ],
                    "stderr_last_offset": first["run-script"]["stderr_last_offset"],
                    "stderr_producer_offset": first["run-script"][
                        "stderr_producer_offset"
                    ],
                }
            },
            anonymize_entities={PIIEntity.CREDIT_CARD},
        )

        assert second["run-script"]["stdout"] == ""
        assert second["run-script"]["stdout_last_offset"] == len(raw_bytes)
        assert second["run-script"]["stdout_producer_offset"] == len("[REDACTED]")

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_redacts_token_split_across_frames(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert a token split across two frames of one fetch is redacted.

        Per-frame anonymization would see ``card=41111111`` and
        ``11111111`` separately and match neither; joining the frames before
        anonymization lets Presidio see the whole token.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        frame_one = json.dumps(
            {
                "Data": b64encode(b"card=41111111").decode(),
                "Offset": SPLIT_TOKEN_FIRST_FRAME_OFFSET,
            }
        )
        frame_two = json.dumps(
            {
                "Data": b64encode(b"11111111\n").decode(),
                "Offset": SPLIT_TOKEN_LINE_EOF_OFFSET,
            }
        )
        mock_backend.client.stream_logs.stream.return_value = frame_one + frame_two
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )

        assert result["run-script"]["stdout"] == "card=[REDACTED]\n"
        assert "4111" not in result["run-script"]["stdout"]
        assert result["run-script"]["stdout_last_offset"] == SPLIT_TOKEN_LINE_EOF_OFFSET
        assert result["run-script"]["stdout_withheld"] == 0

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_withholds_partial_line_across_cycles(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert a token straddling a sync-cycle boundary is redacted.

        Cycle one ends mid-token with no newline: the partial line is withheld
        and the Nomad cursor is rolled back by its raw byte length, so cycle two
        re-fetches the bytes, joins the completed line, and redacts it.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        cycle_one = json.dumps(
            {
                "Data": b64encode(b"card=41111111").decode(),
                "Offset": SPLIT_TOKEN_FIRST_FRAME_OFFSET,
            }
        )
        cycle_two = json.dumps(
            {
                "Data": b64encode(b"card=4111111111111111\n").decode(),
                "Offset": SPLIT_TOKEN_LINE_EOF_OFFSET,
            }
        )
        mock_backend.client.stream_logs.stream.side_effect = [
            cycle_one,  # run-script stdout, cycle 1
            "",  # run-script stderr, cycle 1
            cycle_two,  # run-script stdout, cycle 2
            "",  # run-script stderr, cycle 2
        ]
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        first = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )

        assert first["run-script"]["stdout"] == ""
        assert first["run-script"]["stdout_last_offset"] == 0  # rolled back past token
        assert first["run-script"]["stdout_withheld"] == SPLIT_TOKEN_FIRST_FRAME_OFFSET

        second = executor.get_logs_for_allocation(
            alloc,
            initial_logs={
                "run-script": {
                    "stdout_last_offset": first["run-script"]["stdout_last_offset"],
                    "stdout_producer_offset": first["run-script"][
                        "stdout_producer_offset"
                    ],
                }
            },
            anonymize_entities={PIIEntity.CREDIT_CARD},
        )

        assert second["run-script"]["stdout"] == "card=[REDACTED]\n"
        assert "4111" not in second["run-script"]["stdout"]
        assert second["run-script"]["stdout_last_offset"] == SPLIT_TOKEN_LINE_EOF_OFFSET
        assert second["run-script"]["stdout_withheld"] == 0

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_multibyte_split_not_corrupted(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert the line split never cleaves a multi-byte UTF-8 codepoint.

        The complete portion ends after a multi-byte char and the withheld
        remainder both starts and continues with multi-byte chars; the emitted
        delta must decode cleanly and the cursor roll back by the exact raw byte
        length of the remainder.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        raw_bytes = "café\n€uro".encode()
        frame = json.dumps(
            {
                "Data": b64encode(raw_bytes).decode(),
                "Offset": MULTIBYTE_LINE_EOF_OFFSET,
            }
        )
        mock_backend.client.stream_logs.stream.return_value = frame
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }

        executor = _build_executor()
        result = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )

        assert result["run-script"]["stdout"] == "café\n"
        assert result["run-script"]["stdout_withheld"] == MULTIBYTE_WITHHELD_BYTES
        assert result["run-script"]["stdout_last_offset"] == (
            MULTIBYTE_LINE_EOF_OFFSET - MULTIBYTE_WITHHELD_BYTES
        )

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_flush_partial_emits_newlineless_tail(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert flush_partial emits a trailing line that never gets a newline."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        raw_bytes = b"card=4111111111111111"  # no terminating newline
        frame = json.dumps(
            {"Data": b64encode(raw_bytes).decode(), "Offset": len(raw_bytes)}
        )
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }
        executor = _build_executor()

        mock_backend.client.stream_logs.stream.return_value = frame
        withheld = executor.get_logs_for_allocation(
            alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
        )
        assert withheld["run-script"]["stdout"] == ""
        assert withheld["run-script"]["stdout_withheld"] == len(raw_bytes)

        mock_backend.client.stream_logs.stream.return_value = frame
        flushed = executor.get_logs_for_allocation(
            alloc,
            anonymize_entities={PIIEntity.CREDIT_CARD},
            flush_partial=True,
        )
        assert flushed["run-script"]["stdout"] == "card=[REDACTED]"
        assert flushed["run-script"]["stdout_withheld"] == 0
        assert flushed["run-script"]["stdout_last_offset"] == len(raw_bytes)

    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    def test_get_logs_for_allocation_forced_flush_advances_cursor(
        self, mock_nomad_cls, mock_anonymize, caplog
    ):
        """Assert an over-ceiling partial is emitted and the Nomad cursor advances.

        Without the ceiling the newline-less frame would be withheld and the
        offset rolled back to 0, so the next sync cycle re-fetches the same
        tail. A forced flush must leave withheld at 0 and keep the frame's
        raw EOF as the next cursor.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        raw_bytes = b"card=4111111111111111"  # no terminating newline; 21 bytes
        frame = json.dumps(
            {"Data": b64encode(raw_bytes).decode(), "Offset": len(raw_bytes)}
        )
        mock_backend.client.stream_logs.stream.return_value = frame
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {"run-script": {"StartedAt": "2024-01-01T00:00:00Z"}},
        }
        executor = _build_executor(
            log_anonymization_max_withheld_bytes=FORCED_FLUSH_CEILING_BYTES
        )

        with caplog.at_level(logging.WARNING, logger=NOMAD_MODELS_LOGGER):
            result = executor.get_logs_for_allocation(
                alloc, anonymize_entities={PIIEntity.CREDIT_CARD}
            )

        assert result["run-script"]["stdout"] == "card=[REDACTED]"
        assert "4111" not in result["run-script"]["stdout"]
        assert result["run-script"]["stdout_withheld"] == 0
        assert result["run-script"]["stdout_last_offset"] == len(raw_bytes)
        assert any(
            "Forced anonymization flush" in record.message
            and "alloc-1" in record.message
            and "run-script" in record.message
            for record in caplog.records
        )


class TestNomadLogStreaming:
    """Regression tests for Nomad HTTP log streaming helpers."""

    @staticmethod
    def _alloc_for_logs(step: str = "step1") -> dict[str, Any]:
        """Build a running allocation whose only task state is ``step``.

        :param step: The Nomad task name to mark running.
        :return: The allocation payload the log-stream helpers read.
        """
        return {
            "ID": "alloc-stream",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "TaskStates": {
                step: {"StartedAt": "2024-01-01T00:00:00Z", "State": "running"},
            },
        }

    @staticmethod
    def _log_stream_params(step: str) -> dict[str, Any]:
        """Build the follow-mode log-stream query params for ``step``.

        :param step: The Nomad task name to stream.
        :return: The query params the executor mutates as the cursor advances.
        """
        return {
            "task": step,
            "type": TaskLogType.STDOUT,
            "follow": "true",
            "offset": 0,
        }

    @staticmethod
    def _nomad_log_frame(*, msg: str | None, offset: int) -> bytes:
        frame = {"Offset": offset}
        if msg is not None:
            frame["Data"] = b64encode(msg.encode()).decode()
        return json.dumps(frame).encode()

    @staticmethod
    def _make_iter_chunks(chunks: list[bytes]):
        async def iter_chunks():
            for chunk in chunks:
                yield chunk, None

        return iter_chunks

    @staticmethod
    async def _drain_task_logs(queue: asyncio.Queue) -> list[TaskLog]:
        logs = []
        while not queue.empty():
            logs.append(await queue.get())
        return logs

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_consume_nomad_log_stream_404_before_task_starts_waits(
        self, mock_sleep
    ):
        """404 while StartedAt is None should sleep and return running with no stream start."""
        mock_response = MagicMock()
        mock_response.status = 404
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        executor = _build_executor()
        alloc = {
            "ID": "alloc-stream",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "TaskStates": {
                "step1": {"StartedAt": None, "State": "pending"},
            },
        }
        params = self._log_stream_params("step1")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step1",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        assert state == "running"
        assert out_alloc is alloc
        assert stream_start is None
        mock_sleep.assert_awaited_once()

    def test_log_stream_timeout_emits_warning(self, caplog: pytest.LogCaptureFixture):
        """Direct call logs a WARNING naming the sock_read timeout."""
        executor = _build_executor()
        params = {"offset": 42}
        with caplog.at_level(logging.WARNING, logger=NOMAD_MODELS_LOGGER):
            executor._log_stream_timeout(
                "alloc-stream",
                "step1",
                TaskLogType.STDOUT,
                MOCK_LOG_STREAM_BODY_START_MONOTONIC,
                params,
                start_offset=0,
            )

        assert any(record.levelno == logging.WARNING for record in caplog.records)
        assert "sock_read timeout" in caplog.text
        assert "alloc-stream" in caplog.text
        assert "step1" in caplog.text

    def test_log_stream_cancelled_emits_info(self, caplog: pytest.LogCaptureFixture):
        """Direct call logs an INFO that the stream was cancelled."""
        executor = _build_executor()
        params = {"offset": 7}
        with caplog.at_level(logging.INFO, logger=NOMAD_MODELS_LOGGER):
            executor._log_stream_cancelled(
                "alloc-stream",
                "step1",
                TaskLogType.STDERR,
                None,
                params,
                start_offset=3,
            )

        assert any(record.levelno == logging.INFO for record in caplog.records)
        assert "cancelled" in caplog.text
        assert "alloc-stream" in caplog.text

    def test_log_stream_client_error_emits_exception(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Direct call logs an ERROR for a ClientError via logger.exception."""
        executor = _build_executor()
        params = {"offset": 11}
        with caplog.at_level(logging.ERROR, logger=NOMAD_MODELS_LOGGER):
            executor._log_stream_client_error(
                "alloc-stream",
                "step1",
                TaskLogType.STDOUT,
                MOCK_LOG_STREAM_BODY_START_MONOTONIC,
                params,
                start_offset=0,
            )

        assert any(record.levelno == logging.ERROR for record in caplog.records)
        assert "ClientError" in caplog.text
        assert "alloc-stream" in caplog.text

    @pytest.mark.asyncio
    @patch.object(NomadExecutor, "_consume_nomad_log_stream", new_callable=AsyncMock)
    async def test_push_logs_queue_sock_timeout_logs_and_stops(self, mock_consume):
        """Sock read timeout sentinel ends the push loop and calls _log_stream_timeout."""
        alloc = self._alloc_for_logs()
        mock_consume.return_value = (
            _NOMAD_LOG_STREAM_SOCK_TIMEOUT,
            alloc,
            MOCK_LOG_STREAM_BODY_START_MONOTONIC,
        )

        executor = _build_executor()
        queue = asyncio.Queue()

        with patch.object(executor, "_log_stream_timeout") as mock_log_timeout:
            await executor._push_logs_to_queue(
                alloc, "step1", TaskLogType.STDOUT, queue, start_offset=0
            )

        mock_log_timeout.assert_called_once()
        args, _kwargs = mock_log_timeout.call_args
        assert args[0] == "alloc-stream"
        assert args[1] == "step1"
        assert args[2] == TaskLogType.STDOUT
        assert args[3] == MOCK_LOG_STREAM_BODY_START_MONOTONIC
        sentinel = await queue.get()
        assert sentinel.msg is None

    @pytest.mark.asyncio
    @patch.object(NomadExecutor, "_consume_nomad_log_stream", new_callable=AsyncMock)
    async def test_push_logs_queue_client_error_logs_and_stops(self, mock_consume):
        """Client error sentinel ends the push loop and calls _log_stream_client_error."""
        alloc = self._alloc_for_logs()
        mock_consume.return_value = (_NOMAD_LOG_STREAM_CLIENT_ERROR, alloc, None)

        executor = _build_executor()
        queue = asyncio.Queue()

        with patch.object(executor, "_log_stream_client_error") as mock_log_client:
            await executor._push_logs_to_queue(
                alloc, "step1", TaskLogType.STDERR, queue, start_offset=3
            )

        mock_log_client.assert_called_once()
        sentinel = await queue.get()
        assert sentinel.msg is None

    @pytest.mark.asyncio
    @patch.object(NomadExecutor, "_consume_nomad_log_stream", new_callable=AsyncMock)
    async def test_push_logs_queue_cancelled_logs_and_reraises(self, mock_consume):
        """CancelledError must propagate after _log_stream_cancelled."""
        alloc = self._alloc_for_logs()
        mock_consume.side_effect = asyncio.CancelledError()

        executor = _build_executor()
        queue = asyncio.Queue()

        with (
            patch.object(executor, "_log_stream_cancelled") as mock_log_cancel,
            pytest.raises(asyncio.CancelledError),
        ):
            await executor._push_logs_to_queue(
                alloc, "step1", TaskLogType.STDOUT, queue
            )

        mock_log_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_client_error_from_raise_for_status(self):
        """ClientError from raise_for_status returns client-error sentinel."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.raise_for_status.side_effect = ClientError("boom")
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        executor = _build_executor()
        alloc = self._alloc_for_logs()
        params = self._log_stream_params("step1")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step1",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        assert state == _NOMAD_LOG_STREAM_CLIENT_ERROR
        assert out_alloc is alloc
        assert stream_start is None

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_timeout_during_iter_chunks(self):
        """TimeoutError while reading the body returns sock-timeout with stream_start set."""

        async def iter_chunks():
            raise TimeoutError
            yield (b"", None)  # pragma: no cover

        mock_ctx = self._stream_response(iter_chunks)

        executor = _build_executor()
        alloc = self._alloc_for_logs()
        params = self._log_stream_params("step1")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step1",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        assert state == _NOMAD_LOG_STREAM_SOCK_TIMEOUT
        assert out_alloc is alloc
        assert stream_start is not None

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_advances_offset_across_chunks(self):
        """Two data frames advance params offset and enqueue TaskLogs in order (step2)."""
        chunks = [
            self._nomad_log_frame(msg="line-one", offset=MULTI_CHUNK_LOG_FIRST_OFFSET),
            self._nomad_log_frame(msg="line-two", offset=MULTI_CHUNK_LOG_SECOND_OFFSET),
        ]

        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor()
        alloc = self._alloc_for_logs("step2")
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)

        assert state == "running"
        assert out_alloc is alloc
        assert stream_start is not None
        assert params["offset"] == MULTI_CHUNK_LOG_SECOND_OFFSET
        assert len(logs) == EXPECTED_MULTI_CHUNK_LOG_COUNT
        assert [log.msg for log in logs] == ["line-one", "line-two"]
        assert [log.offset for log in logs] == [
            MULTI_CHUNK_LOG_FIRST_OFFSET,
            MULTI_CHUNK_LOG_SECOND_OFFSET,
        ]
        assert all(log.step == "step2" for log in logs)
        assert all(log.type == TaskLogType.STDOUT for log in logs)
        offsets = [log.offset for log in logs]
        assert offsets == sorted(offsets)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_redacts_token_split_across_frames(
        self, mock_anonymize
    ):
        """Assert a token split across two live frames is redacted once whole."""
        mock_anonymize.side_effect = _redact_card_token
        chunks = [
            self._nomad_log_frame(
                msg="card=41111111", offset=SPLIT_TOKEN_FIRST_FRAME_OFFSET
            ),
            self._nomad_log_frame(msg="11111111\n", offset=SPLIT_TOKEN_LINE_EOF_OFFSET),
        ]
        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor()
        alloc = self._alloc_for_logs("run-script")
        params = self._log_stream_params("run-script")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="run-script",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["card=[REDACTED]\n"]
        first_message = logs[0].msg
        assert first_message is not None
        assert "4111" not in first_message
        assert logs[0].offset == SPLIT_TOKEN_LINE_EOF_OFFSET

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_withholds_partial_and_rolls_back_offset(
        self, mock_anonymize
    ):
        """Assert the emitted offset is rolled back over a withheld partial line."""
        mock_anonymize.side_effect = _redact_card_token
        chunks = [
            self._nomad_log_frame(
                msg="ok\ncard=41", offset=WITHHELD_PARTIAL_FRAME_EOF_OFFSET
            )
        ]
        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor()
        alloc = self._alloc_for_logs("run-script")
        params = self._log_stream_params("run-script")
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()

        with patch.object(executor, "_request", return_value=mock_ctx):
            await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="run-script",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=pending,
            )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["ok\n"]
        # raw EOF (12) minus the 7 withheld bytes of "card=41"
        assert logs[0].offset == WITHHELD_PARTIAL_RESUME_OFFSET
        assert pending.drain() == b"card=41"
        assert (
            params["offset"] == WITHHELD_PARTIAL_FRAME_EOF_OFFSET
        )  # raw resume cursor

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_forced_flush_clears_pending_and_advances(
        self, mock_anonymize, caplog
    ):
        """Assert an over-ceiling live partial is pushed and pending is cleared.

        Without the ceiling the newline-less frame would leave pending growing
        and emit nothing. A forced flush must anonymize, push to the queue,
        clear pending, and leave the emit offset un-rolled-back.
        """
        mock_anonymize.side_effect = _redact_card_token
        chunks = [
            self._nomad_log_frame(
                msg="card=4111111111111111",
                offset=NEWLINELESS_TAIL_FRAME_EOF_OFFSET,
            )
        ]
        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor(
            log_anonymization_max_withheld_bytes=FORCED_FLUSH_CEILING_BYTES
        )
        alloc = self._alloc_for_logs("run-script")
        params = self._log_stream_params("run-script")
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()

        with (
            patch.object(executor, "_request", return_value=mock_ctx),
            caplog.at_level(logging.WARNING, logger=NOMAD_MODELS_LOGGER),
        ):
            await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="run-script",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=pending,
            )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["card=[REDACTED]"]
        first_message = logs[0].msg
        assert first_message is not None
        assert "4111" not in first_message
        assert logs[0].offset == NEWLINELESS_TAIL_FRAME_EOF_OFFSET
        assert not pending
        assert any(
            "Forced anonymization flush" in record.message
            and "alloc-stream" in record.message
            and "run-script" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_push_logs_flushes_withheld_tail_on_stream_end(self, mock_anonymize):
        """Assert a newline-less withheld tail is flushed before the end sentinel."""
        mock_anonymize.side_effect = _redact_card_token

        async def iter_chunks():
            yield (
                self._nomad_log_frame(
                    msg="card=4111111111111111",
                    offset=NEWLINELESS_TAIL_FRAME_EOF_OFFSET,
                ),
                None,
            )
            raise TimeoutError

        mock_ctx = self._stream_response(iter_chunks)

        executor = _build_executor()
        queue = asyncio.Queue()

        with (
            patch.object(executor, "_request", return_value=mock_ctx),
            patch.object(executor, "_log_stream_timeout"),
        ):
            await executor._push_logs_to_queue(
                self._alloc_for_logs(),
                "run-script",
                TaskLogType.STDOUT,
                queue,
                start_offset=0,
                anonymize_entities={PIIEntity.CREDIT_CARD},
            )

        logs = await self._drain_task_logs(queue)
        assert logs[0].msg == "card=[REDACTED]"
        first_message = logs[0].msg
        assert first_message is not None
        assert "4111" not in first_message
        assert logs[-1].msg is None  # end-of-stream sentinel comes last

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_split_frame_reassembly(self):
        """Split JSON across chunks reassembles via raw_data before json.loads (step2)."""
        full = self._nomad_log_frame(msg="split-msg", offset=SPLIT_FRAME_LOG_OFFSET)
        split_at = full.rfind(b"}")
        assert b"}" not in full[:split_at]
        chunks = [full[:split_at], full[split_at:]]

        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor()
        alloc = self._alloc_for_logs("step2")
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with patch.object(executor, "_request", return_value=mock_ctx):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)

        assert state == "running"
        assert out_alloc is alloc
        assert stream_start is not None
        assert params["offset"] == SPLIT_FRAME_LOG_OFFSET
        assert len(logs) == EXPECTED_SINGLE_TASK_LOG_COUNT
        assert logs[0].msg == "split-msg"
        assert logs[0].offset == SPLIT_FRAME_LOG_OFFSET
        assert logs[0].step == "step2"
        assert logs[0].type == TaskLogType.STDOUT

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_reads_frames_sharing_a_chunk(self):
        """Read every frame when a proxy forwards several in one HTTP chunk.

        A heartbeat directly followed by a data frame, two data frames, and a
        frame whose end shares a chunk with the next frame's start must all be
        read, in order, with the offset ending at the last frame's.
        """
        first = self._nomad_log_frame(msg="one\n", offset=4)
        second = self._nomad_log_frame(msg="two\n", offset=8)
        third = self._nomad_log_frame(msg="three\n", offset=14)
        last_offset = 19
        fourth = self._nomad_log_frame(msg="four\n", offset=last_offset)
        split_at = len(fourth) // 2
        chunks = [
            b"{}" + first,
            second + third + fourth[:split_at],
            fourth[split_at:],
        ]
        executor = _build_executor()
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with patch.object(
            executor,
            "_request",
            return_value=self._stream_response(self._make_iter_chunks(chunks)),
        ):
            state, _alloc, _start = await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs("step2"),
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)
        assert state == "running"
        assert [log.msg for log in logs] == ["one\n", "two\n", "three\n", "four\n"]
        assert params["offset"] == last_offset

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_reads_a_character_split_across_chunks(self):
        """Read a frame whose non-ASCII text ends one chunk mid-character."""
        frame = json.dumps(
            {"Offset": 4, "File": "tâche", "Data": b64encode(b"ok\n").decode()},
            ensure_ascii=False,
        ).encode()
        split_at = frame.index("â".encode()) + 1
        chunks = [frame[:split_at], frame[split_at:]]
        executor = _build_executor()
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with patch.object(
            executor,
            "_request",
            return_value=self._stream_response(self._make_iter_chunks(chunks)),
        ):
            await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs("step2"),
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["ok\n"]

    @pytest.mark.parametrize(
        "truncated",
        [
            b'{"Offset":4,"Data":"b25',
            b'{"Offset":4',
            b'{"Offset":4,"Fi',
            b'{"Offset":4,"File":"a\\u00',
            b'{"Offset":4,"File":"\\ud83d',
        ],
        ids=["mid-string", "mid-number", "mid-key", "mid-escape", "mid-surrogate"],
    )
    def test_split_nomad_frames_carries_a_truncated_frame_over(self, truncated: bytes):
        """Carry a frame the buffer cut off over, after the frames before it."""
        complete = self._nomad_log_frame(msg="one\n", offset=2)

        frames, tail = _split_nomad_frames(complete + truncated)

        assert frames == [json.loads(complete)]
        assert tail == truncated

    @pytest.mark.parametrize(
        "malformed",
        [
            b'{"Offset":!}',
            b'{"Offset":4,"Data":"a\x01"}',
            b'{"Offset":4,"Data":"\\q"}',
            b'{"Offset":4,"Data":"\\u12"}',
            b"{Offset:4}",
        ],
        ids=[
            "bad-value",
            "control-character",
            "bad-escape",
            "short-unicode-escape",
            "unquoted-key",
        ],
    )
    def test_split_nomad_frames_raises_on_a_malformed_frame(self, malformed: bytes):
        """Raise on a frame no further bytes could make valid, not buffer it."""
        complete = self._nomad_log_frame(msg="one\n", offset=2)

        with pytest.raises(json.JSONDecodeError):
            _split_nomad_frames(malformed + complete)

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_raises_on_a_malformed_frame(self):
        """Surface a malformed frame instead of buffering every later frame."""
        chunks = [b'{"Offset":!}' + self._nomad_log_frame(msg="one\n", offset=4)]
        executor = _build_executor()
        queue = asyncio.Queue()

        with (
            patch.object(
                executor,
                "_request",
                return_value=self._stream_response(self._make_iter_chunks(chunks)),
            ),
            pytest.raises(json.JSONDecodeError),
        ):
            await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs("step2"),
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=self._log_stream_params("step2"),
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        assert queue.empty()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pieces", [1, 2], ids=["one-chunk", "split-frame"])
    async def test_consume_nomad_log_stream_queues_output_sharing_a_recheck_chunk(
        self, pieces: int
    ):
        """Queue the data frame that arrives with the heartbeat due for a recheck.

        The recheck waits for the chunk's remaining frames, and for a frame
        split across chunks to complete, so the output is queued before a later
        run of heartbeats rechecks and ends the stream on the terminal state.
        """
        heartbeats = b"".join(
            self._nomad_log_frame(msg=None, offset=1)
            for _ in range(EXPECTED_EMPTY_FRAMES_BEFORE_RECHECK)
        )
        last_offset = 5
        output = self._nomad_log_frame(msg="last\n", offset=last_offset)
        split_at = len(output) // pieces
        chunks = [
            heartbeats + output[:split_at],
            output[split_at:],
            self._nomad_log_frame(msg=None, offset=last_offset)
            * (EXPECTED_EMPTY_FRAMES_BEFORE_RECHECK),
        ]
        executor = _build_executor(
            log_socket_read_timeout=RECHECK_LOG_SOCKET_READ_TIMEOUT
        )
        alloc = self._alloc_for_logs("step2")
        refreshed_alloc = {
            **alloc,
            "TaskStates": {"step2": {"State": RECHECKED_TASK_STATE}},
        }
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with (
            patch.object(
                executor,
                "_request",
                return_value=self._stream_response(self._make_iter_chunks(chunks)),
            ),
            patch.object(
                NomadExecutor, "get_last_allocation", return_value=refreshed_alloc
            ) as mock_get_last_allocation,
        ):
            state, _alloc, _start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)
        assert state == RECHECKED_TASK_STATE
        assert [log.msg for log in logs] == ["last\n"]
        assert params["offset"] == last_offset
        mock_get_last_allocation.assert_called_once_with("job-1", "eval-1")

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_empty_data_increments_without_recheck(self):
        """Data frame then empty frame increments empty_data_count without recheck (step2)."""
        chunks = [
            self._nomad_log_frame(msg="has-data", offset=EMPTY_DATA_FRAME_DATA_OFFSET),
            self._nomad_log_frame(msg=None, offset=EMPTY_DATA_FRAME_OFFSET_ONLY),
        ]

        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor()
        alloc = self._alloc_for_logs("step2")
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with (
            patch.object(executor, "_request", return_value=mock_ctx),
            patch.object(
                NomadExecutor, "get_last_allocation"
            ) as mock_get_last_allocation,
        ):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)

        assert state == "running"
        assert out_alloc is alloc
        assert stream_start is not None
        assert params["offset"] == EMPTY_DATA_FRAME_OFFSET_ONLY
        mock_get_last_allocation.assert_not_called()
        assert len(logs) == EXPECTED_SINGLE_TASK_LOG_COUNT
        assert logs[0].msg == "has-data"
        assert logs[0].offset == EMPTY_DATA_FRAME_DATA_OFFSET

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_empty_data_triggers_recheck(self):
        """Consecutive empty frames trigger get_last_allocation and return task state (step2)."""
        empty_offsets = list(range(1, EXPECTED_EMPTY_FRAMES_BEFORE_RECHECK + 1))
        chunks = [
            self._nomad_log_frame(msg=None, offset=offset) for offset in empty_offsets
        ]

        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor(
            log_socket_read_timeout=RECHECK_LOG_SOCKET_READ_TIMEOUT
        )
        alloc = self._alloc_for_logs("step2")
        refreshed_alloc = {
            **alloc,
            "TaskStates": {
                "step2": {
                    "StartedAt": "2024-01-01T00:00:00Z",
                    "State": RECHECKED_TASK_STATE,
                },
            },
        }
        params = self._log_stream_params("step2")
        queue = asyncio.Queue()

        with (
            patch.object(executor, "_request", return_value=mock_ctx),
            patch.object(
                NomadExecutor,
                "get_last_allocation",
                return_value=refreshed_alloc,
            ) as mock_get_last_allocation,
        ):
            state, out_alloc, stream_start = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        logs = await self._drain_task_logs(queue)

        assert state == RECHECKED_TASK_STATE
        assert out_alloc is refreshed_alloc
        assert stream_start is not None
        mock_get_last_allocation.assert_called_once_with("job-1", "eval-1")
        assert logs == []

    @staticmethod
    async def _consume_404(
        executor: NomadExecutor, alloc: dict[str, Any]
    ) -> tuple[str, dict[str, Any], float | None]:
        """Drive one ``_consume_nomad_log_stream`` cycle against a 404 response.

        :param executor: The executor under test.
        :param alloc: The allocation the stream is reading from.
        :return: The ``(state, alloc, stream_start)`` tuple the method returned.
        """
        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.raise_for_status = MagicMock(
            side_effect=ClientResponseError(MagicMock(), (), status=404)
        )
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        params = TestNomadLogStreaming._log_stream_params("step1")

        with patch.object(executor, "_request", return_value=mock_ctx):
            return await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step1",
                log_type=TaskLogType.STDOUT,
                queue=asyncio.Queue(),
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_consume_nomad_log_stream_404_unstarted_step_waits(self, mock_sleep):
        """Retry a 404 while the allocation still lists the step as unstarted."""
        executor = _build_executor()
        alloc = {
            "ID": "alloc-stream",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "TaskStates": {"step1": {"StartedAt": None}},
        }

        state, out_alloc, stream_start = await self._consume_404(executor, alloc)

        assert state == "running"
        assert out_alloc is alloc
        assert stream_start is None
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    async def test_consume_nomad_log_stream_404_absent_task_states_ends_stream(
        self, mock_sleep
    ):
        """End the stream on a 404 for a step the allocation does not carry.

        The 404 clears only once Nomad starts serving that step's logs, so an
        allocation that lists no task states at all would otherwise be polled
        forever. Falling through to ``raise_for_status`` ends the caller's loop
        through the existing client-error sentinel instead.
        """
        executor = _build_executor()
        alloc = {"ID": "alloc-stream", "JobID": "job-1", "EvalID": "eval-1"}

        state, out_alloc, stream_start = await self._consume_404(executor, alloc)

        assert state == _NOMAD_LOG_STREAM_CLIENT_ERROR
        assert state != "running"
        assert out_alloc is alloc
        assert stream_start is None
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consume_nomad_log_stream_recheck_missing_step_ends_stream(self):
        """End the stream loop when a refreshed allocation dropped the step.

        ``_push_logs_to_queue`` loops while the returned state is ``running``, so
        a rescheduled allocation whose task states no longer carry this step must
        yield a non-``running`` state rather than spin forever.
        """
        empty_offsets = list(range(1, EXPECTED_EMPTY_FRAMES_BEFORE_RECHECK + 1))
        chunks = [
            self._nomad_log_frame(msg=None, offset=offset) for offset in empty_offsets
        ]

        mock_ctx = self._stream_response(self._make_iter_chunks(chunks))

        executor = _build_executor(
            log_socket_read_timeout=RECHECK_LOG_SOCKET_READ_TIMEOUT
        )
        alloc = self._alloc_for_logs("step2")
        refreshed_alloc = {"ID": "alloc-rescheduled", "JobID": "job-1", "EvalID": "e-2"}
        params = self._log_stream_params("step2")

        with (
            patch.object(executor, "_request", return_value=mock_ctx),
            patch.object(
                NomadExecutor, "get_last_allocation", return_value=refreshed_alloc
            ),
        ):
            state, out_alloc, _ = await executor._consume_nomad_log_stream(
                alloc=alloc,
                step="step2",
                log_type=TaskLogType.STDOUT,
                queue=asyncio.Queue(),
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities=None,
                pending=WithheldLineBuffer(),
            )

        assert state != "running"
        assert out_alloc is refreshed_alloc

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_logs_fans_out_one_producer_per_step_and_type(
        self, mock_nomad_cls
    ):
        """Assert a started allocation streams both log types of every step.

        Each producer signals its own end with a ``msg=None`` sentinel, which the
        generator consumes rather than yields; it returns once the last stream
        has signalled. Per-step start offsets are forwarded so a resumed stream
        does not replay what the client already received, and a stream with no
        recorded offset starts at ``0``.
        """
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-stream",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )
        alloc = self._alloc_for_logs() | {
            "TaskStates": {
                "step1": {"StartedAt": "2024-01-01T00:00:00Z", "State": "running"},
                "step2": {"StartedAt": "2024-01-01T00:00:00Z", "State": "running"},
            }
        }
        start_offsets = {"step1": {TaskLogType.STDOUT: INITIAL_LOG_OFFSET}}
        forwarded_offsets = {}

        async def fake_push(
            _self, _alloc, step, log_type, queue, start_offset, _anonymize_entities
        ):
            forwarded_offsets[(step, log_type)] = start_offset
            await queue.put(TaskLog(step=step, type=log_type, msg=f"{step}:{log_type}"))
            await queue.put(TaskLog(step=step, type=log_type, msg=None))

        with (
            patch.object(NomadExecutor, "get_last_allocation", return_value=alloc),
            patch.object(NomadExecutor, "_push_logs_to_queue", fake_push),
        ):
            emitted = [
                log async for log in executor.stream_logs(queue_item, start_offsets)
            ]

        expected_streams = {
            (step, log_type) for step in ("step1", "step2") for log_type in TaskLogType
        }
        assert {(log.step, log.type) for log in emitted} == expected_streams
        assert all(log.msg for log in emitted)
        assert forwarded_offsets == {
            ("step1", TaskLogType.STDOUT): INITIAL_LOG_OFFSET,
            ("step1", TaskLogType.STDERR): 0,
            ("step2", TaskLogType.STDOUT): 0,
            ("step2", TaskLogType.STDERR): 0,
        }

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_logs_absent_task_states_yields_sentinel(self, mock_nomad_cls):
        """Assert streaming an allocation with no ``TaskStates`` yields the sentinel."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        with patch.object(
            NomadExecutor,
            "get_last_allocation",
            return_value={"ID": "alloc-1", "JobID": "job-1", "EvalID": "eval-1"},
        ):
            emitted = [log async for log in executor.stream_logs(queue_item)]

        assert emitted == [None]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("anonymize_mask", "expected_card"),
        [(0, "4111111111111111"), (PIIEntity.CREDIT_CARD.value, "[REDACTED]")],
    )
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text", autospec=True)
    @patch("app.tasks.execution.executors.nomad.models.Nomad", autospec=True)
    async def test_completed_live_stream_matches_stored_text(
        self,
        mock_nomad_cls,
        mock_anonymize,
        session: AsyncSession,
        created_task_with_history: TaskHistory,
        anonymize_mask: int,
        expected_card: str,
    ):
        """Preserve each stream's stored text through live completion and tail flush."""
        mock_anonymize.side_effect = _redact_card_token
        payloads = {
            ("run-script", TaskLogType.STDOUT): [
                "card=41111111",
                "11111111\nfinal\n",
                "\n",
            ],
            ("run-script", TaskLogType.STDERR): ["warning\n", "final partial"],
            ("setup", TaskLogType.STDOUT): ["café\n", "literal\x00\n"],
            ("setup", TaskLogType.STDERR): [],
        }
        alloc = self._alloc_for_logs("run-script")
        alloc["CreateIndex"] = ALLOCATION_CREATE_INDEX
        alloc["TaskStates"].update(self._alloc_for_logs("setup")["TaskStates"])
        backend = mock_nomad_cls.return_value
        backend.allocations.get_allocations.return_value = [alloc]
        executor = _build_executor(
            log_socket_read_timeout=RECHECK_LOG_SOCKET_READ_TIMEOUT,
            terminal_log_drain_max_attempts=0,
        )
        history = created_task_with_history
        history.status = TaskHistoryStatusEnum.RUNNING
        history.anonymize_mask = anonymize_mask
        history.execution_request.tracking.update(
            job_id=alloc["JobID"], evaluation_id=alloc["EvalID"]
        )

        def follow_response(_session, _method, _url, *, params, **_kwargs):
            assert params["follow"] == "true"
            step = params["task"]
            frames = self._frames_with_running_offsets(payloads[step, params["type"]])

            async def iter_chunks():
                for frame in frames:
                    yield frame, None
                alloc["TaskStates"][step]["State"] = "dead"
                for _ in range(executor.log_socket_read_timeout + 1):
                    yield b"{}", None

            return self._stream_response(iter_chunks)

        live_text: dict[tuple[str, TaskLogType], str] = dict.fromkeys(payloads, "")
        with patch(
            "aiohttp.ClientSession.request", autospec=True, side_effect=follow_response
        ):
            async with executor, asyncio.timeout(NOMAD_DEFAULT_TIMEOUT):
                response = await stream_task_history_logs(
                    session, executor, history, {}
                )
                async for entry in response.body_iterator:
                    log = TaskLog.model_validate_json(entry)
                    assert log.msg is not None
                    live_text[log.step, log.type] += log.msg

        def stored_response(_alloc_id, *, task, type_, offset):
            content = "".join(payloads[task, type_]).encode()
            return json.dumps(
                {
                    "Data": b64encode(content[offset:]).decode(),
                    "Offset": len(content),
                }
            )

        backend.client.stream_logs.stream.side_effect = stored_response
        history.status = TaskHistoryStatusEnum.SUCCESS
        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id=alloc["ID"],
        )
        stored_text: dict[tuple[str, TaskLogType], str] = dict.fromkeys(payloads, "")
        response = await stream_task_history_logs(session, executor, history, {})
        async for entry in response.body_iterator:
            log = TaskLog.model_validate_json(entry)
            assert log.msg is not None
            stored_text[log.step, log.type] += log.msg

        assert live_text == stored_text
        assert live_text["run-script", TaskLogType.STDOUT].endswith("final\n\n")
        assert live_text["run-script", TaskLogType.STDERR] == "warning\nfinal partial"
        assert live_text["setup", TaskLogType.STDOUT] == "café\nliteral\x00\n"
        assert live_text["setup", TaskLogType.STDERR] == ""
        assert live_text["run-script", TaskLogType.STDOUT] == (
            f"card={expected_card}\nfinal\n\n"
        )

    @staticmethod
    def _frames_with_running_offsets(payloads: list[str]) -> list[bytes]:
        """Build framed payloads carrying the raw EOF offset each one reaches.

        :param payloads: The frame payloads, in arrival order.
        :return: The encoded Nomad log frames.
        """
        frames = []
        offset = 0
        for payload in payloads:
            offset += len(payload.encode())
            frames.append(
                TestNomadLogStreaming._nomad_log_frame(msg=payload, offset=offset)
            )
        return frames

    @staticmethod
    def _stream_response(
        iter_chunks: Callable[[], AsyncIterator[tuple[bytes, bool | None]]],
    ) -> AsyncMock:
        """Build a 200 log-stream response whose body iterates ``iter_chunks``.

        :param iter_chunks: The zero-argument async generator function the
            response's ``content.iter_chunks`` becomes.
        :return: An async context manager standing in for ``_request``.
        """
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.content.iter_chunks = iter_chunks
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_completion_frame_releases_the_whole_run(
        self, mock_anonymize
    ):
        """Assert a terminator-free run is emitted whole by the frame that ends it.

        Each frame searches only its own bytes, so the frame carrying the
        terminator must still release everything the earlier frames withheld.
        """
        mock_anonymize.side_effect = _redact_card_token
        chunks = self._frames_with_running_offsets(
            ["card=41", "111111", "111111", "11\ntail"]
        )
        executor = _build_executor()
        params = self._log_stream_params("run-script")
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()

        with patch.object(
            executor,
            "_request",
            return_value=self._stream_response(self._make_iter_chunks(chunks)),
        ):
            await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs("run-script"),
                step="run-script",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=pending,
            )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["card=[REDACTED]\n"]
        assert (
            logs[0].offset
            == CARD_LINE_WITH_TAIL_EOF_OFFSET - CARD_LINE_TAIL_WITHHELD_BYTES
        )
        assert pending.drain() == b"tail"

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_withheld_remainder_survives_a_reconnect(
        self, mock_anonymize
    ):
        """Assert a remainder withheld by one request is completed by the next.

        The buffer outlives a single HTTP request, so a reconnect resumes
        mid-line and the frame carrying the terminator must release exactly the
        carried remainder plus the new bytes.
        """
        mock_anonymize.side_effect = _redact_card_token
        first_stream = self._frames_with_running_offsets(["card=41", "111111"])
        params = self._log_stream_params("run-script")
        executor = _build_executor()
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()
        responses = [
            self._stream_response(self._make_iter_chunks(first_stream)),
            self._stream_response(
                self._make_iter_chunks(
                    [
                        self._nomad_log_frame(
                            msg="111111", offset=RECONNECT_RESUME_FRAME_EOF_OFFSET
                        ),
                        self._nomad_log_frame(
                            msg="11\n", offset=SPLIT_TOKEN_LINE_EOF_OFFSET
                        ),
                    ]
                )
            ),
        ]

        with patch.object(executor, "_request", side_effect=responses):
            for _ in responses:
                await executor._consume_nomad_log_stream(
                    alloc=self._alloc_for_logs("run-script"),
                    step="run-script",
                    log_type=TaskLogType.STDOUT,
                    queue=queue,
                    params=params,
                    client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                    anonymize_entities={PIIEntity.CREDIT_CARD},
                    pending=pending,
                )

        logs = await self._drain_task_logs(queue)
        assert [log.msg for log in logs] == ["card=[REDACTED]\n"]
        assert logs[0].offset == SPLIT_TOKEN_LINE_EOF_OFFSET
        assert not pending

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    async def test_consume_stream_leaves_no_terminator_withheld(self, mock_anonymize):
        """Assert every frame releases the lines it completed and withholds no more.

        A terminator left in the buffer would silently stall a line that was
        already complete until the ceiling flushed it.
        """
        mock_anonymize.side_effect = _redact_card_token
        chunks = self._frames_with_running_offsets(["a\nb", "c\nd", "e"])
        executor = _build_executor()
        params = self._log_stream_params("run-script")
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()

        with patch.object(
            executor,
            "_request",
            return_value=self._stream_response(self._make_iter_chunks(chunks)),
        ):
            await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs("run-script"),
                step="run-script",
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=pending,
            )

        logs = await self._drain_task_logs(queue)
        assert [(log.msg, log.offset) for log in logs] == [("a\n", 2), ("bc\n", 5)]
        withheld = pending.drain()
        assert withheld == b"de"
        assert b"\n" not in withheld
        assert b"\r" not in withheld

    @pytest.mark.asyncio
    async def test_consume_stream_unanonymized_step_withholds_nothing(self):
        """Assert a step outside the anonymized set emits each frame untouched.

        Requested entities do not make a step eligible, so a partial line must
        reach the queue whole, with the raw offset and nothing withheld.
        """
        payload = "partial-no-terminator"
        chunks = self._frames_with_running_offsets([payload])
        executor = _build_executor()
        params = self._log_stream_params(NomadStep.PREPARE_ENV)
        queue = asyncio.Queue()
        pending = WithheldLineBuffer()

        with patch.object(
            executor,
            "_request",
            return_value=self._stream_response(self._make_iter_chunks(chunks)),
        ):
            await executor._consume_nomad_log_stream(
                alloc=self._alloc_for_logs(NomadStep.PREPARE_ENV),
                step=NomadStep.PREPARE_ENV,
                log_type=TaskLogType.STDOUT,
                queue=queue,
                params=params,
                client_timeout=ClientTimeout(sock_read=NOMAD_DEFAULT_TIMEOUT),
                anonymize_entities={PIIEntity.CREDIT_CARD},
                pending=pending,
            )

        logs = await self._drain_task_logs(queue)
        assert [(log.msg, log.offset) for log in logs] == [(payload, len(payload))]
        assert not pending


class TestListFiles:
    """Test NomadExecutor.list_files."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_list_files(self, mock_nomad_cls):
        """Assert list_files returns FileMetadata dict excluding hidden files."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value=[
                {"Name": "output.sql", "Size": 1024, "IsDir": False},
                {"Name": ".hidden", "Size": 0, "IsDir": False},
                {"Name": "subdir", "Size": 0, "IsDir": True},
            ]
        )

        mock_ctx_manager = AsyncMock()
        mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with patch.object(executor, "_request", return_value=mock_ctx_manager):
            result = await executor.list_files(queue_item, "/alloc/data")

        assert "output.sql" in result
        assert result["output.sql"] == FileMetadata(size=1024, is_dir=False)
        assert ".hidden" not in result
        assert "subdir" in result
        assert result["subdir"].is_dir is True

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_list_files_excludes_the_run_result_file(self, mock_nomad_cls):
        """Assert PMM Extensions' own run-result file never reaches the output-files browser."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(
            return_value=[
                {"Name": "backup.sql", "Size": 1024, "IsDir": False},
                {"Name": RUN_RESULT_FILENAME, "Size": 96, "IsDir": False},
            ]
        )

        mock_ctx_manager = AsyncMock()
        mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with patch.object(executor, "_request", return_value=mock_ctx_manager):
            result = await executor.list_files(queue_item, "/alloc/data")

        assert RUN_RESULT_FILENAME not in result
        assert "backup.sql" in result

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_list_files_no_filesystem_returns_empty_dict(self, mock_nomad_cls):
        """Assert list_files returns {} when allocation has no filesystem (prestart 404)."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "ClientStatus": "failed",
        }

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        mock_response = AsyncMock()
        mock_response.status = status.HTTP_404_NOT_FOUND
        mock_response.raise_for_status = MagicMock()

        mock_ctx_manager = AsyncMock()
        mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with patch.object(executor, "_request", return_value=mock_ctx_manager):
            result = await executor.list_files(queue_item, "/alloc/data")

        assert result == {}
        mock_response.raise_for_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_list_files_404_non_failed_alloc_propagates(self, mock_nomad_cls):
        """Assert list_files raises on 404 when alloc status is not failed/lost.

        A 404 on a completed allocation means the output path is misconfigured —
        that error must surface, not be swallowed.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "ClientStatus": "complete",
        }

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        mock_response = AsyncMock()
        mock_response.status = status.HTTP_404_NOT_FOUND
        mock_response.raise_for_status = MagicMock(side_effect=ClientError("not found"))

        mock_ctx_manager = AsyncMock()
        mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(executor, "_request", return_value=mock_ctx_manager),
            pytest.raises(ClientError),
        ):
            await executor.list_files(queue_item, "/alloc/data")

        mock_response.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_status",
        [status.HTTP_500_INTERNAL_SERVER_ERROR, status.HTTP_403_FORBIDDEN],
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_list_files_non_404_http_errors_propagate(
        self, mock_nomad_cls, error_status
    ):
        """Assert list_files propagates non-404 HTTP errors (outage/auth errors must surface)."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        mock_response = AsyncMock()
        mock_response.status = error_status
        mock_response.raise_for_status = MagicMock(side_effect=ClientError("error"))

        mock_ctx_manager = AsyncMock()
        mock_ctx_manager.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(executor, "_request", return_value=mock_ctx_manager),
            pytest.raises(ClientError),
        ):
            await executor.list_files(queue_item, "/alloc/data")


class TestParsePayload:
    """Test NomadExecutor.parse_payload."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.async_run")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_parse_payload_hcl(self, mock_nomad_cls, mock_async_run):
        """Assert parse_payload delegates HCL to backend.jobs.parse."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_async_run.return_value = {"Job": {"ID": "parsed"}}

        executor = _build_executor()
        result = await executor.parse_payload("job {}", "hcl")

        mock_async_run.assert_called_once_with(mock_backend.jobs.parse, "job {}")
        assert result == {"Job": {"ID": "parsed"}}

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_parse_payload_json(self, mock_nomad_cls):
        """Assert parse_payload delegates JSON to parent class."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()

        json_payload = json.dumps({"Job": {"ID": "test"}})
        result = await executor.parse_payload(json_payload, "json")

        assert result == {"Job": {"ID": "test"}}


class TestBackendFromWorkerThreads:
    """Cover the Nomad client being first reached from several worker threads."""

    @pytest.mark.asyncio
    async def test_concurrent_first_lookups_build_one_client(self) -> None:
        """Build the client and its ``requests.Session`` once for racing threads."""
        builds = 0

        def slow_nomad(**_kwargs: Any) -> MagicMock:
            nonlocal builds
            builds += 1
            time.sleep(0.2)
            return MagicMock()

        executor = _build_executor()
        with patch(
            "app.tasks.execution.executors.nomad.models.Nomad", side_effect=slow_nomad
        ):
            clients = await asyncio.gather(
                asyncio.to_thread(lambda: executor.backend),
                asyncio.to_thread(lambda: executor.backend),
            )

        assert builds == 1
        assert clients[0] is clients[1]


class TestConcurrentAnonymizedLiveStreams:
    """Cover live anonymization when several streams redact at the same time."""

    @staticmethod
    def _request_serving(frames: dict[TaskLogType, list[str]]) -> Callable[..., Any]:
        """Build a ``_request`` stand-in serving each log type's frames once.

        The second request for a log type fails to connect, which ends that
        stream and makes it drain whatever it still withholds.

        :param frames: The frame payloads to serve per log type, in order.
        :return: The ``side_effect`` for a patched ``_request``.
        """
        served: set[TaskLogType] = set()

        def request(_method: str, _path: str, **kwargs: Any) -> AsyncMock:
            log_type = kwargs["params"]["type"]
            if log_type in served:
                failing = AsyncMock()
                failing.__aenter__ = AsyncMock(side_effect=ClientError("gone"))
                return failing
            served.add(log_type)
            chunks = TestNomadLogStreaming._frames_with_running_offsets(
                frames[log_type]
            )
            return TestNomadLogStreaming._stream_response(
                TestNomadLogStreaming._make_iter_chunks(chunks)
            )

        return request

    @pytest.mark.asyncio
    async def test_concurrent_streams_each_redact_their_own_split_tokens(self):
        """Redact every address whole in both streams, in each stream's own order.

        Both streams run the real anonymizer at once, and each address is split
        across two frames, so a stream's output is correct only when its own
        withheld remainder was joined with its own next frame.
        """
        frames = {
            TaskLogType.STDOUT: [
                "mail alice@exam",
                "ple.com now\nthen bob@exa",
                "mple.org",
            ],
            TaskLogType.STDERR: [
                "warn carol@exa",
                "mple.net\nfrom dave@exam",
                "ple.com\n",
            ],
        }
        expected = {
            TaskLogType.STDOUT: "mail <EMAIL_ADDRESS> now\nthen <EMAIL_ADDRESS>",
            TaskLogType.STDERR: "warn <EMAIL_ADDRESS>\nfrom <EMAIL_ADDRESS>\n",
        }
        executor = _build_executor()
        alloc = TestNomadLogStreaming._alloc_for_logs(NomadStep.RUN_SCRIPT)
        queues = {log_type: asyncio.Queue() for log_type in frames}

        with patch.object(
            executor, "_request", side_effect=self._request_serving(frames)
        ):
            await asyncio.gather(
                *(
                    executor._push_logs_to_queue(
                        alloc,
                        NomadStep.RUN_SCRIPT,
                        log_type,
                        queue,
                        anonymize_entities={PIIEntity.EMAIL_ADDRESS},
                    )
                    for log_type, queue in queues.items()
                )
            )

        for log_type, queue in queues.items():
            logs = await TestNomadLogStreaming._drain_task_logs(queue)
            assert logs[-1].msg is None
            assert "".join(log.msg for log in logs[:-1]) == expected[log_type]


class TestStreamFile:
    """Test NomadExecutor.stream_file."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_regular(self, mock_nomad_cls):
        """Assert stream_file reads a regular file in chunks."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        file_content = b"file content here"
        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(
            return_value={"Size": len(file_content), "IsDir": False}
        )

        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=file_content)

        call_count = 0

        def mock_request(method, path, **kwargs):
            nonlocal call_count
            call_count += 1
            ctx = AsyncMock()
            if "stat" in path:
                ctx.__aenter__ = AsyncMock(return_value=stat_response)
            else:
                ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/dump.sql")
            ]

        assert b"".join(chunks) == file_content

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_anonymizes_off_the_event_loop(self, mock_nomad_cls):
        """Keep the event loop free while a downloaded chunk is anonymized.

        A slow analysis, such as the first one, which loads the language model,
        would otherwise stall every stream the worker serves.
        """
        slow_anonymization = 1.0
        mock_nomad_cls.return_value.allocation.get_allocation.return_value = {
            "ID": "alloc-1"
        }
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={"allocation_id": "alloc-1"},
        )
        queue_item.anonymize_mask = PIIEntity.EMAIL_ADDRESS
        file_content = b"mail me\n"
        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(
            return_value={"Size": len(file_content), "IsDir": False}
        )
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=file_content)

        def mock_request(_method, path, **_kwargs):
            ctx = AsyncMock()
            response = stat_response if "stat" in path else read_response
            ctx.__aenter__ = AsyncMock(return_value=response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        def slow_anonymize(text: str, _entities: set[PIIEntity]) -> str:
            time.sleep(slow_anonymization)
            return text

        ticks: list[float] = []
        stop = asyncio.Event()

        async def tick() -> None:
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0)
        with (
            patch.object(executor, "_request", side_effect=mock_request),
            patch(
                "app.tasks.execution.executors.nomad.models.anonymize_text",
                side_effect=slow_anonymize,
            ),
        ):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/a.log")
            ]
        ticks.append(time.monotonic())
        stop.set()
        await ticker

        assert b"".join(chunks) == file_content
        assert max(b - a for a, b in pairwise(ticks)) < slow_anonymization / 2

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_empty(self, mock_nomad_cls):
        """Assert stream_file yields empty bytes for zero-size file."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(return_value={"Size": 0, "IsDir": False})

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=stat_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/empty.txt")
            ]

        assert chunks == [b""]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("history_mask", "task_mask", "expected_entities"),
        [
            (int(PIIEntity.CREDIT_CARD), None, {PIIEntity.CREDIT_CARD}),
            (None, int(PIIEntity.PERSON), {PIIEntity.PERSON}),
        ],
    )
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_with_anonymization(
        self,
        mock_nomad_cls,
        mock_anonymize,
        history_mask,
        task_mask,
        expected_entities,
    ):
        """Assert stream_file anonymizes via history mask, or task mask when history is ``None``."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}
        mock_anonymize.return_value = "REDACTED"

        task = _build_task()
        task.anonymize_mask = task_mask
        executor = _build_executor()
        queue_item = _build_queue_item(
            task=task,
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
        )
        queue_item.anonymize_mask = history_mask

        file_content = b"sensitive data"
        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(
            return_value={"Size": len(file_content), "IsDir": False}
        )

        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=file_content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            if "stat" in path:
                ctx.__aenter__ = AsyncMock(return_value=stat_response)
            else:
                ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/dump.sql")
            ]

        assert b"".join(chunks) == b"REDACTED"
        mock_anonymize.assert_called_once_with("sensitive data", expected_entities)

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_undecodable_sends_raw_bytes(
        self, mock_nomad_cls, mock_anonymize, caplog: pytest.LogCaptureFixture
    ):
        """Assert UnicodeDecodeError falls back to raw bytes on the stream path."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
        )
        queue_item.anonymize_mask = int(PIIEntity.EMAIL_ADDRESS)

        file_content = b"\xff\xfe binary"
        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(
            return_value={"Size": len(file_content), "IsDir": False}
        )
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=file_content)

        def mock_request(_method, path, **_kwargs):
            ctx = AsyncMock()
            response = stat_response if "stat" in path else read_response
            ctx.__aenter__ = AsyncMock(return_value=response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with (
            patch.object(executor, "_request", side_effect=mock_request),
            caplog.at_level(logging.DEBUG, logger=NOMAD_MODELS_LOGGER),
        ):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/bin.dat")
            ]

        assert b"".join(chunks) == file_content
        mock_anonymize.assert_not_called()
        assert "Could not decode file content" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_without_anonymization(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert anonymize=False returns content verbatim despite set entities."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}
        mock_anonymize.return_value = "REDACTED"

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )
        queue_item.anonymize_mask = 1

        file_content = b'{"size_bytes": 20260725}'
        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(
            return_value={"Size": len(file_content), "IsDir": False}
        )

        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=file_content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            if "stat" in path:
                ctx.__aenter__ = AsyncMock(return_value=stat_response)
            else:
                ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            chunks = [
                chunk
                async for chunk in executor.stream_file(
                    queue_item,
                    "/output/.pmm-extensions-run-result.json",
                    anonymize=False,
                )
            ]

        assert b"".join(chunks) == file_content
        mock_anonymize.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_directory_delegates(self, mock_nomad_cls):
        """Assert stream_file delegates to _stream_directory_as_tar_gz for directories."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(return_value={"IsDir": True, "Size": 0})

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=stat_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        async def fake_tar_gz(*args, **kwargs):
            yield b"fake-tar-data"

        with (
            patch.object(executor, "_request", side_effect=mock_request),
            patch.object(
                executor,
                "_stream_directory_as_tar_gz",
                side_effect=fake_tar_gz,
            ),
        ):
            chunks = [
                chunk
                async for chunk in executor.stream_file(queue_item, "/output/subdir")
            ]

        assert chunks == [b"fake-tar-data"]

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stream_file_directory_forwards_anonymize_flag(self, mock_nomad_cls):
        """Assert the anonymize flag reaches the directory archiving path."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {"ID": "alloc-1"}

        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        stat_response = AsyncMock()
        stat_response.raise_for_status = MagicMock()
        stat_response.json = AsyncMock(return_value={"IsDir": True, "Size": 0})

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=stat_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        async def fake_tar_gz(*args, **kwargs):
            yield b"fake-tar-data"

        tar_gz = MagicMock(side_effect=fake_tar_gz)
        with (
            patch.object(executor, "_request", side_effect=mock_request),
            patch.object(executor, "_stream_directory_as_tar_gz", tar_gz),
        ):
            [
                chunk
                async for chunk in executor.stream_file(
                    queue_item, "/output/subdir", anonymize=False
                )
            ]

        assert tar_gz.call_args.kwargs is not None
        assert tar_gz.call_args.kwargs["anonymize"] is False


class TestReadFileBytes:
    """Test NomadExecutor._read_file_bytes."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_empty(self, mock_nomad_cls):
        """Assert _read_file_bytes returns empty bytes for zero-size file."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()

        result = await executor._read_file_bytes(queue_item, "alloc-1", "/f.txt", 0)
        assert result == b""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_single_chunk(self, mock_nomad_cls):
        """Assert _read_file_bytes reads a small file in one chunk."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()

        content = b"hello world"
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            result = await executor._read_file_bytes(
                queue_item, "alloc-1", "/f.txt", len(content)
            )

        assert result == content

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_with_anonymization(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert _read_file_bytes applies anonymization when entities are set."""
        mock_nomad_cls.return_value = MagicMock()
        mock_anonymize.return_value = "REDACTED content"

        executor = _build_executor()
        queue_item = _build_queue_item()
        queue_item.anonymize_mask = 1

        content = b"sensitive content"
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            result = await executor._read_file_bytes(
                queue_item, "alloc-1", "/f.txt", len(content)
            )

        assert result == b"REDACTED content"
        mock_anonymize.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_undecodable_keeps_raw_bytes(
        self, mock_nomad_cls, mock_anonymize, caplog: pytest.LogCaptureFixture
    ):
        """Assert UnicodeDecodeError falls back to raw bytes on the tar-read path."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()
        queue_item.anonymize_mask = int(PIIEntity.EMAIL_ADDRESS)

        content = b"\xff\xfe binary"
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=content)

        def mock_request(_method, _path, **_kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with (
            patch.object(executor, "_request", side_effect=mock_request),
            caplog.at_level(logging.DEBUG, logger=NOMAD_MODELS_LOGGER),
        ):
            result = await executor._read_file_bytes(
                queue_item, "alloc-1", "/f.bin", len(content)
            )

        assert result == content
        mock_anonymize.assert_not_called()
        assert "Could not decode file content for anonymization" in caplog.text

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_without_anonymization(
        self, mock_nomad_cls, mock_anonymize
    ):
        """Assert anonymize=False returns content verbatim despite set entities."""
        mock_nomad_cls.return_value = MagicMock()
        mock_anonymize.return_value = "REDACTED content"

        executor = _build_executor()
        queue_item = _build_queue_item()
        queue_item.anonymize_mask = 1

        content = b"sensitive content"
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            result = await executor._read_file_bytes(
                queue_item, "alloc-1", "/f.txt", len(content), anonymize=False
            )

        assert result == content
        mock_anonymize.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_read_file_bytes_size_mismatch_logs_warning(self, mock_nomad_cls):
        """Assert _read_file_bytes returns the short body when the size disagrees."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()

        content = b"short"
        read_response = AsyncMock()
        read_response.raise_for_status = MagicMock()
        read_response.read = AsyncMock(return_value=content)

        def mock_request(method, path, **kwargs):
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=read_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            result = await executor._read_file_bytes(
                queue_item, "alloc-1", "/f.txt", 9999
            )

        assert result == content


class TestIterDirectoryEntries:
    """Test NomadExecutor._iter_directory_entries."""

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_iter_directory_entries_flat(self, mock_nomad_cls):
        """Assert _iter_directory_entries yields entries with correct paths."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()

        ls_response = AsyncMock()
        ls_response.raise_for_status = MagicMock()
        ls_response.json = AsyncMock(
            return_value=[
                {"Name": "file1.txt", "IsDir": False, "Size": 100},
                {"Name": ".hidden", "IsDir": False, "Size": 50},
                {"Name": "sub", "IsDir": True, "Size": 0},
            ]
        )

        sub_response = AsyncMock()
        sub_response.raise_for_status = MagicMock()
        sub_response.json = AsyncMock(
            return_value=[
                {"Name": "nested.txt", "IsDir": False, "Size": 200},
            ]
        )

        call_count = 0

        def mock_request(method, path, **kwargs):
            nonlocal call_count
            call_count += 1
            ctx = AsyncMock()
            if call_count == 1:
                ctx.__aenter__ = AsyncMock(return_value=ls_response)
            else:
                ctx.__aenter__ = AsyncMock(return_value=sub_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(executor, "_request", side_effect=mock_request):
            entries = [
                entry
                async for entry in executor._iter_directory_entries(
                    "alloc-1", "/alloc/data", "root"
                )
            ]

        abs_paths = [e[0] for e in entries]
        rel_paths = [e[1] for e in entries]
        assert "/alloc/data/file1.txt" in abs_paths
        assert "/alloc/data/.hidden" not in abs_paths
        assert "root/file1.txt" in rel_paths
        assert "root/sub/" in rel_paths
        assert "root/sub/nested.txt" in rel_paths


class TestStreamDirectoryAsTarGz:
    """Test NomadExecutor._stream_directory_as_tar_gz."""

    @staticmethod
    async def _drain_archive(
        executor: NomadExecutor,
        queue_item: TaskHistory,
        alloc_id: str,
        path: str,
    ) -> bytes:
        """Consume the async generator and return the full tar.gz bytes."""
        return b"".join(
            [
                chunk
                async for chunk in executor._stream_directory_as_tar_gz(
                    queue_item, alloc_id, path
                )
            ]
        )

    @staticmethod
    def _tar_names(archive_bytes: bytes) -> set[str]:
        """Return member names from a gzipped tar archive."""
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            return {member.name for member in tar.getmembers()}

    @staticmethod
    def _tar_file_content(archive_bytes: bytes, name: str) -> bytes:
        """Read one regular-file member from a gzipped tar archive."""
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            member = tar.getmember(name)
            extracted = tar.extractfile(member)
            assert extracted is not None
            return extracted.read()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_streams_nested_directory_as_tar_gz(self, mock_nomad_cls):
        """Assert a nested directory archive contains every expected entry."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()
        file_contents = {
            "/output/dumps/a.txt": b"alpha",
            "/output/dumps/nested/b.txt": b"beta",
        }

        async def fake_entries(_alloc_id, _path, _prefix):
            yield "/output/dumps/a.txt", "dumps/a.txt", False, 5
            yield "/output/dumps/nested", "dumps/nested/", True, 0
            yield "/output/dumps/nested/b.txt", "dumps/nested/b.txt", False, 4

        async def fake_read(_queue_item, _alloc_id, path, _size, **_kwargs):
            return file_contents[path]

        with (
            patch.object(
                NomadExecutor, "_iter_directory_entries", side_effect=fake_entries
            ),
            patch.object(NomadExecutor, "_read_file_bytes", side_effect=fake_read),
        ):
            archive = await self._drain_archive(
                executor, queue_item, "alloc-1", "/output/dumps"
            )

        names = self._tar_names(archive)
        # tarfile strips trailing slashes from directory member names on read.
        assert "dumps" in names
        assert "dumps/a.txt" in names
        assert "dumps/nested" in names
        assert "dumps/nested/b.txt" in names
        assert self._tar_file_content(archive, "dumps/a.txt") == b"alpha"
        assert self._tar_file_content(archive, "dumps/nested/b.txt") == b"beta"

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_skips_entry_when_read_raises(
        self, mock_nomad_cls, caplog: pytest.LogCaptureFixture
    ):
        """Assert a failed entry is skipped while the rest of the archive completes."""
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        queue_item = _build_queue_item()

        async def fake_entries(_alloc_id, _path, _prefix):
            yield "/output/dumps/good.txt", "dumps/good.txt", False, 4
            yield "/output/dumps/bad.txt", "dumps/bad.txt", False, 3
            yield "/output/dumps/also-good.txt", "dumps/also-good.txt", False, 5

        async def fake_read(_queue_item, _alloc_id, path, _size, **_kwargs):
            if path.endswith("bad.txt"):
                raise RuntimeError("read failed")
            return b"ok-" + path.rsplit("/", 1)[-1].encode()

        with (
            patch.object(
                NomadExecutor, "_iter_directory_entries", side_effect=fake_entries
            ),
            patch.object(NomadExecutor, "_read_file_bytes", side_effect=fake_read),
            caplog.at_level(logging.ERROR, logger=NOMAD_MODELS_LOGGER),
        ):
            archive = await self._drain_archive(
                executor, queue_item, "alloc-1", "/output/dumps"
            )

        names = self._tar_names(archive)
        assert "dumps/good.txt" in names
        assert "dumps/also-good.txt" in names
        assert "dumps/bad.txt" not in names
        assert self._tar_file_content(archive, "dumps/good.txt") == b"ok-good.txt"
        assert (
            self._tar_file_content(archive, "dumps/also-good.txt")
            == b"ok-also-good.txt"
        )
        assert "Failed to add" in caplog.text
        assert "dumps/bad.txt" in caplog.text


class TestIsDirectory:
    """Test NomadExecutor._is_directory."""

    @pytest.mark.parametrize(
        ("stat", "expected"),
        [
            ({"IsDir": True}, True),
            ({"Directory": "some-dir"}, True),
            ({"Type": "directory"}, True),
            ({"Type": "dir"}, True),
            ({"IsDir": False, "Type": "file"}, False),
            ({}, False),
        ],
    )
    def test_is_directory(self, stat, expected):
        """Assert _is_directory correctly detects directory stats."""
        assert NomadExecutor._is_directory(stat) is expected


_NS_EARLY = 1_700_000_000_000_000_000
_NS_LATER = 1_700_000_060_000_000_000


class TestNomadTaskStatesToExecutionEvents:
    """Tests for :func:`nomad_task_states_to_execution_events`."""

    def test_non_dict_returns_empty(self):
        """Absent or wrong-type task_states yields no events."""
        assert nomad_task_states_to_execution_events(None) == []
        assert nomad_task_states_to_execution_events([]) == []
        assert nomad_task_states_to_execution_events("bad") == []

    def test_missing_or_empty_events(self):
        """Empty structures yield an empty list."""
        assert nomad_task_states_to_execution_events({}) == []
        assert nomad_task_states_to_execution_events({"step1": {}}) == []
        assert nomad_task_states_to_execution_events({"step1": {"Events": []}}) == []

    def test_malformed_events_skipped(self):
        """Events without valid Time or non-mapping entries are ignored."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": "Started",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "Task received",
                    },
                    {"Type": "Broken", "DisplayMessage": "missing Time"},
                    "not-a-dict",
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "Started"
        assert events[0].step == "step1"
        assert "Task received" in events[0].description

    def test_skips_non_dict_state_and_non_list_events(self):
        """Skip a non-dict task state and a task whose ``Events`` is not a list."""
        task_states = {
            "bad-state": "not-a-dict",
            "bad-events": {"Events": {"Type": "Started"}},
            "good": {
                "Events": [
                    {
                        "Type": "Started",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "ok",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].step == "good"
        assert events[0].event_type == "Started"

    def test_skips_non_string_task_name(self):
        """Ignore non-string task keys rather than raising on them."""
        task_states = {
            1: {
                "Events": [
                    {
                        "Type": "Started",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "ignored",
                    },
                ],
            },
            "step1": {
                "Events": [
                    {
                        "Type": "Started",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "kept",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].step == "step1"
        assert "kept" in events[0].description

    def test_type_missing_coerces_to_unknown(self):
        """Label an event ``Unknown`` when its ``Type`` is missing."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Time": _NS_EARLY,
                        "DisplayMessage": "no type field",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "Unknown"
        assert "no type field" in events[0].description

    def test_type_non_string_is_coerced_with_str(self):
        """Keep a non-string ``Type`` via ``str(value)`` rather than dropping it."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": 42,
                        "Time": _NS_EARLY,
                        "DisplayMessage": "numeric type",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "42"
        assert "numeric type" in events[0].description

    def test_non_numeric_time_drops_event(self):
        """Drop an event whose ``Time`` is present but non-numeric."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": "Started",
                        "Time": "not-a-number",
                        "DisplayMessage": "bad time",
                    },
                    {
                        "Type": "Started",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "kept",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert "kept" in events[0].description

    def test_sorted_oldest_first_across_tasks(self):
        """Events from multiple tasks are merged and sorted by Nomad time."""
        task_states = {
            "b": {
                "Events": [
                    {
                        "Type": "Late",
                        "Time": _NS_LATER,
                        "DisplayMessage": "second",
                    },
                ],
            },
            "a": {
                "Events": [
                    {
                        "Type": "Early",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "first",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == len(task_states)
        assert "first" in events[0].description
        assert events[0].step == "a"
        assert "second" in events[1].description
        assert events[1].step == "b"

    def test_exit_code_from_details(self):
        """Exit code may appear only under Details (defensive parse)."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": "Terminated",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "Exited",
                        "Details": {"exit_code": 123},
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "Terminated"
        assert "123" in events[0].description

    def test_nomad_executor_get_events_reads_tracking(self):
        """NomadExecutor.get_events delegates to stored task_states."""
        tracking = {
            "allocation_id": None,
            "evaluation_id": "eval-1",
            "job_id": "job-1",
            "task_states": {
                "step1": {
                    "Events": [
                        {
                            "Type": "Setup",
                            "Time": _NS_EARLY,
                            "DisplayMessage": "Downloading Artifacts",
                        },
                    ],
                },
            },
        }
        history = _build_queue_item(
            tracking=tracking, status=TaskHistoryStatusEnum.SUCCESS
        )
        executor = _build_executor()
        out = executor.get_events(history)
        assert len(out) == 1
        assert isinstance(out[0], ExecutionEvent)
        assert out[0].event_type == "Setup"
        assert "Downloading Artifacts" in out[0].description
        assert out[0].step == "step1"

    def test_nomad_executor_get_events_non_dict_tracking_returns_empty(self):
        """Degrade non-dict tracking to an empty event list."""
        history = _build_queue_item(status=TaskHistoryStatusEnum.SUCCESS)
        history.execution_request.tracking = "not-a-dict"  # type: ignore[assignment]
        executor = _build_executor()
        assert executor.get_events(history) == []

        history.execution_request.tracking = None  # type: ignore[assignment]
        assert executor.get_events(history) == []

    def test_prestart_artifact_download_failure_event_extracted(self):
        """Assert 'Failed Artifact Download' prestart event surfaces as ExecutionEvent."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": "Failed Artifact Download",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "Failed to download artifact: connection refused",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "Failed Artifact Download"
        assert "connection refused" in events[0].description
        assert events[0].step == "step1"

    def test_prestart_setup_failure_event_extracted(self):
        """Assert 'Setup Failure' prestart event surfaces as ExecutionEvent."""
        task_states = {
            "step1": {
                "Events": [
                    {
                        "Type": "Setup Failure",
                        "Time": _NS_EARLY,
                        "DisplayMessage": "failed to setup alloc: artifact download failed",
                    },
                ],
            },
        }
        events = nomad_task_states_to_execution_events(task_states)
        assert len(events) == 1
        assert events[0].event_type == "Setup Failure"
        assert "artifact download failed" in events[0].description
        assert events[0].step == "step1"


class TestPersistNomadTaskLogsCursorDurability:
    """Test durable Nomad fetch-cursor behavior across sync cycles."""

    @staticmethod
    def _reconstruct_stream(chunks, state) -> str:
        """Return the ordered persisted-plus-staged content for one stream."""
        body = "".join(
            chunk.content for chunk in sorted(chunks, key=lambda c: c.start_offset)
        )
        staged = state.staging.decode("utf-8") if state and state.staging else ""
        return body + staged

    @staticmethod
    def _offset_aware_stream(raw_logs: dict):
        """Return a ``stream_logs.stream`` mock that honors the ``offset`` kwarg.

        The mock returns only the raw bytes at or after ``offset`` so a fetch
        that wrongly restarts from ``0`` re-reads content the caller already
        persisted — the exact regression the durable cursor prevents.
        """

        def fake_stream(alloc_id, *, task, type_, offset):
            content = raw_logs.get((task, type_), "")
            delta = content[offset:]
            if not delta:
                return ""
            return json.dumps(
                {"Data": b64encode(delta.encode()).decode(), "Offset": len(content)}
            )

        return fake_stream

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_absent_task_states_persists_nothing(
        self,
        mock_nomad_cls,
        session,
        created_task_with_history,
    ):
        """Assert the Celery log-persistence path tolerates a ``TaskStates``-less alloc.

        ``sync_running_items`` supplies a ``writer_session``, so this branch runs
        on every periodic sync of a rescheduled allocation.
        """
        mock_nomad_cls.return_value = MagicMock()
        executor = _build_executor()
        created_task_with_history.anonymize_mask = 0

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=created_task_with_history,
            alloc={"ID": "alloc-rescheduled", "CreateIndex": ALLOCATION_CREATE_INDEX},
            previous_allocation_id="alloc-1",
        )

        chunks = await TaskHistoryLogManager.list_chunks_for_task(
            session, created_task_with_history.id
        )
        assert chunks == []

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_cold_worker_second_cycle_resumes_from_db_cursor(
        self,
        mock_nomad_cls,
        mock_anonymize,
        session,
        created_task_with_history,
    ):
        """Assert a cold second cycle resumes from the DB cursor without re-reading.

        With the process-local fetch-offset dict gone, the ``taskhistory_log_state``
        row is the only record of the raw Nomad offset. A second sync cycle that
        lands on a worker without the in-memory cursor must seed from that row;
        otherwise the anonymized run-script stream re-reads from offset ``0`` and
        the producer-offset dedup (``skip == 0``) appends the whole file again.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = lambda text, _entities: text.replace(
            "4111", "[REDACTED]"
        )
        raw_logs = {
            ("prepare", TaskLogType.STDOUT): "prepare-A\n",
            ("run-script", TaskLogType.STDOUT): "cc 4111 here\n",
        }
        mock_backend.client.stream_logs.stream.side_effect = self._offset_aware_stream(
            raw_logs
        )

        history = created_task_with_history
        history.anonymize_mask = PIIEntity.CREDIT_CARD.value
        alloc = {
            "ID": "alloc-1",
            "CreateIndex": ALLOCATION_CREATE_INDEX,
            "TaskStates": {
                "prepare": {"StartedAt": "2024-01-01T00:00:00Z"},
                "run-script": {"StartedAt": "2024-01-01T00:00:00Z"},
            },
        }
        executor = _build_executor()

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id="alloc-1",
        )
        raw_logs[("prepare", TaskLogType.STDOUT)] = "prepare-A\nprepare-B\n"
        raw_logs[("run-script", TaskLogType.STDOUT)] = "cc 4111 here\nsecond\n"
        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id="alloc-1",
        )

        chunks = await TaskHistoryLogManager.list_chunks_for_task(session, history.id)
        chunks_by_stream = defaultdict(list)
        for chunk in chunks:
            chunks_by_stream[(chunk.source, chunk.stream)].append(chunk)
        prepare_state = await TaskHistoryLogStateManager.get_for_stream(
            session, history.id, "prepare", TaskLogType.STDOUT
        )
        run_script_state = await TaskHistoryLogStateManager.get_for_stream(
            session, history.id, "run-script", TaskLogType.STDOUT
        )

        assert (
            self._reconstruct_stream(
                chunks_by_stream[("prepare", TaskLogType.STDOUT)], prepare_state
            )
            == "prepare-A\nprepare-B\n"
        )
        assert (
            self._reconstruct_stream(
                chunks_by_stream[("run-script", TaskLogType.STDOUT)],
                run_script_state,
            )
            == "cc [REDACTED] here\nsecond\n"
        )

    @pytest.mark.asyncio
    async def test_build_initial_log_offsets_skips_superseded_epoch_row(
        self, session, created_task_with_history
    ):
        """Assert a row from a different allocation epoch is not used to seed."""
        history = created_task_with_history
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"old",
            force_flush=True,
            producer_offset_after=SEED_OFFSET,
            producer_fetch_offset_after=SEED_OFFSET,
            producer_epoch=SUPERSEDED_ALLOCATION_EPOCH,
        )

        offsets = await NomadExecutor._build_initial_log_offsets(
            session, history.id, current_epoch=CURRENT_ALLOCATION_EPOCH
        )

        assert "run-script" not in offsets

    @pytest.mark.asyncio
    async def test_build_initial_log_offsets_seeds_matching_epoch_row(
        self, session, created_task_with_history
    ):
        """Assert a row matching the current epoch seeds both cursors."""
        history = created_task_with_history
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"cur",
            force_flush=True,
            producer_offset_after=SEED_OFFSET,
            producer_fetch_offset_after=SEED_OFFSET,
            producer_epoch=CURRENT_ALLOCATION_EPOCH,
        )

        offsets = await NomadExecutor._build_initial_log_offsets(
            session, history.id, current_epoch=CURRENT_ALLOCATION_EPOCH
        )

        assert offsets["run-script"]["stdout_last_offset"] == SEED_OFFSET
        assert offsets["run-script"]["stdout_producer_offset"] == SEED_OFFSET

    @pytest.mark.asyncio
    async def test_build_initial_log_offsets_seeds_legacy_epoch_zero_row(
        self, session, created_task_with_history
    ):
        """Assert a legacy ``producer_epoch == 0`` row is trusted for seeding."""
        history = created_task_with_history
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"legacy",
            force_flush=True,
            producer_offset_after=LEGACY_SEED_PRODUCER_OFFSET,
        )

        offsets = await NomadExecutor._build_initial_log_offsets(
            session, history.id, current_epoch=CURRENT_ALLOCATION_EPOCH
        )

        assert "run-script" in offsets
        assert (
            offsets["run-script"]["stdout_producer_offset"]
            == LEGACY_SEED_PRODUCER_OFFSET
        )


class TestDrainTerminalLogs:
    """Exercise the bounded post-terminal log drain in ``_persist_nomad_task_logs``."""

    DRAIN_MAX_ATTEMPTS = 5
    SHORT_DRAIN_MAX_ATTEMPTS = 3
    EXPECTED_SLEEPS_ALL_STREAMS_DRAINED = 2
    FAILING_STDOUT_CALL = 2

    @staticmethod
    def _reconstruct_stream(chunks, state) -> str:
        """Return the ordered persisted-plus-staged content for one stream."""
        body = "".join(
            chunk.content for chunk in sorted(chunks, key=lambda c: c.start_offset)
        )
        staged = state.staging.decode("utf-8") if state and state.staging else ""
        return body + staged

    @staticmethod
    def _growing_stream(snapshots: dict[tuple, list[str]]):
        """Return a ``stream_logs.stream`` mock that reveals content over calls.

        ``snapshots[(task, type_)]`` is the cumulative on-disk content visible on
        each successive call to that stream; once the list is exhausted the last
        snapshot repeats. Every call honors the ``offset`` kwarg and returns only
        ``content[offset:]`` (or ``""`` when nothing new is on disk), modelling
        Nomad's non-blocking single-shot read as ``logmon`` flushes the tail.
        """
        calls = defaultdict(int)

        def fake_stream(alloc_id, *, task, type_, offset):
            series = snapshots.get((task, type_), [""])
            idx = min(calls[(task, type_)], len(series) - 1)
            calls[(task, type_)] += 1
            content = series[idx]
            delta = content[offset:]
            if not delta:
                return ""
            return json.dumps(
                {"Data": b64encode(delta.encode()).decode(), "Offset": len(content)}
            )

        return fake_stream

    @staticmethod
    def _terminal_alloc(*steps: str) -> dict:
        """Return an allocation dict with the given steps already started."""
        return {
            "ID": "alloc-1",
            "CreateIndex": ALLOCATION_CREATE_INDEX,
            "TaskStates": {
                step: {"StartedAt": "2024-01-01T00:00:00Z"} for step in steps
            },
        }

    async def _stream_content(self, session, history_id, step, stream) -> str:
        """Return the full persisted-plus-staged content for one stream."""
        chunks = [
            chunk
            for chunk in await TaskHistoryLogManager.list_chunks_for_task(
                session, history_id
            )
            if chunk.source == step and chunk.stream == stream
        ]
        state = await TaskHistoryLogStateManager.get_for_stream(
            session, history_id, step, stream
        )
        return self._reconstruct_stream(chunks, state)

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lagging_tail_captured_across_drain_reads(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a tail ``logmon`` flushes after terminal detection is captured."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["partial\n", "partial\ntail\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\ntail\n"
        mock_sleep.assert_awaited()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_empty_first_retry_does_not_end_drain(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert an empty first retry does not end the drain before the tail lands."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {
                ("run-script", TaskLogType.STDOUT): [
                    "partial\n",
                    "partial\n",
                    "partial\ntail\n",
                ]
            }
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\ntail\n"
        assert mock_sleep.await_count == self.DRAIN_MAX_ATTEMPTS

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_early_exit_when_all_streams_drained(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert the drain early-exits once every active stream drains then quiets."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {
                ("run-script", TaskLogType.STDOUT): ["out\n", "out\ntail\n"],
                ("run-script", TaskLogType.STDERR): ["err\n", "err\ndone\n"],
            }
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        stdout = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        stderr = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDERR
        )
        assert stdout == "out\ntail\n"
        assert stderr == "err\ndone\n"
        assert mock_sleep.await_count == self.EXPECTED_SLEEPS_ALL_STREAMS_DRAINED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_drain_flushes_newlineless_anonymized_tail(
        self,
        mock_nomad_cls,
        mock_sleep,
        mock_anonymize,
        session,
        created_task_with_history,
    ):
        """Assert a newline-less anonymized tail is flushed, not withheld forever."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["", "card=4111111111111111"]}
        )
        history = created_task_with_history
        history.anonymize_mask = PIIEntity.CREDIT_CARD.value
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "card=[REDACTED]"
        assert "4111" not in content

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_drain_not_fooled_by_withheld_partial(
        self,
        mock_nomad_cls,
        mock_sleep,
        mock_anonymize,
        session,
        created_task_with_history,
    ):
        """Assert the early-exit does not fire while a stream withholds a partial.

        Both streams advance then quiet, which would normally early-exit; but
        stdout still holds a withheld partial, so the loop must poll the full
        window and the completed tail must be redacted on the final flush.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {
                ("run-script", TaskLogType.STDOUT): [
                    "",
                    "tail1\n",
                    "tail1\ncard=4111111111111111",
                ],
                ("run-script", TaskLogType.STDERR): ["", "eout\n"],
            }
        )
        history = created_task_with_history
        history.anonymize_mask = PIIEntity.CREDIT_CARD.value
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.SHORT_DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        stdout = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert stdout == "tail1\ncard=[REDACTED]"
        assert "4111" not in stdout
        assert mock_sleep.await_count == self.SHORT_DRAIN_MAX_ATTEMPTS

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_fully_flushed_task_polls_full_window(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a task whose tail was already captured polls the full window."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["complete\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "complete\n"
        assert mock_sleep.await_count == self.DRAIN_MAX_ATTEMPTS

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_never_empty_stream_bounded_by_max_attempts(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a pathological never-quiet stream still terminates at the cap."""
        calls = defaultdict(int)

        def ever_growing(alloc_id, *, task, type_, offset):
            length = calls[(task, type_)] + 1
            calls[(task, type_)] += 1
            content = "x" * length
            delta = content[offset:]
            if not delta:
                return ""
            return json.dumps(
                {"Data": b64encode(delta.encode()).decode(), "Offset": len(content)}
            )

        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = ever_growing
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.SHORT_DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        assert mock_sleep.await_count == self.SHORT_DRAIN_MAX_ATTEMPTS

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_max_attempts_zero_disables_drain(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert ``max_attempts=0`` skips the drain entirely — no fetch, no sleep."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["partial\n", "partial\ntail\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\n"
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.anonymize_text")
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_max_attempts_zero_still_flushes_anonymized_tail(
        self,
        mock_nomad_cls,
        mock_sleep,
        mock_anonymize,
        session,
        created_task_with_history,
    ):
        """Assert AC3's no-loss promise holds even when the drain is disabled.

        ``max_attempts=0`` skips the polling loop entirely, but the terminal
        flush is gated on whether withholding was possible at all, not on the
        drain's polling budget -- so an anonymized step's newline-less tail
        must still be persisted rather than dropped.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_anonymize.side_effect = _redact_card_token
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["card=4111111111111111"]}
        )
        history = created_task_with_history
        history.anonymize_mask = PIIEntity.CREDIT_CARD.value
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "card=[REDACTED]"
        assert "4111" not in content
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_drain_resume_is_idempotent_across_terminal_reruns(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert re-running the terminal persist re-reads without duplicating bytes."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["partial\n", "partial\ntail\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )
        alloc = self._terminal_alloc("run-script")

        for _ in range(2):
            await executor._persist_nomad_task_logs(
                writer_session=session,
                queue_item=history,
                alloc=alloc,
                previous_allocation_id="alloc-1",
            )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\ntail\n"

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_running_status_does_not_drain(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a still-running sync (``force_flush=False``) never drains."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["partial\n", "partial\ntail\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.RUNNING
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_allocation_gone_during_drain_degrades_gracefully(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a GC'd allocation mid-drain degrades to empty without crashing."""
        calls = defaultdict(int)

        def gone_after_terminal(alloc_id, *, task, type_, offset):
            index = calls[(task, type_)]
            calls[(task, type_)] += 1
            if index == 0:
                return json.dumps(
                    {
                        "Data": b64encode(b"partial\n").decode(),
                        "Offset": len("partial\n"),
                    }
                )
            raise BaseNomadException(MagicMock())

        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = gone_after_terminal
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\n"

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lagging_stdout_with_settled_stderr(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert the drain captures a lagging stdout tail while stderr is settled."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {
                ("run-script", TaskLogType.STDERR): ["boot ok\n"],
                ("run-script", TaskLogType.STDOUT): ["head\n", "head\ntail\n"],
            }
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        stdout = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        stderr = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDERR
        )
        assert stdout == "head\ntail\n"
        assert stderr == "boot ok\n"

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_terminal_failed_status_drains_lagging_tail(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a non-``SUCCESS`` terminal status (FAILED) still drains the tail."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["partial\n", "partial\ntail\n"]}
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.FAILED
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        content = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert content == "partial\ntail\n"

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_lagging_second_stream_not_dropped_after_sibling_drains(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a stream whose tail lands after a sibling drained is not dropped.

        stdout's tail lands and goes quiet, then a fully-quiet round passes
        before stderr's own tail flushes. A task-wide early-exit gate would
        return on that quiet round and lose the stderr tail; per-stream tracking
        keeps polling because stderr has not advanced yet.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            {
                ("run-script", TaskLogType.STDOUT): ["head\n", "head\ntail\n"],
                ("run-script", TaskLogType.STDERR): [
                    "boot\n",
                    "boot\n",
                    "boot\nlate\n",
                ],
            }
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._terminal_alloc("run-script"),
            previous_allocation_id="alloc-1",
        )

        stdout = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        stderr = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDERR
        )
        assert stdout == "head\ntail\n"
        assert stderr == "boot\nlate\n"

    @staticmethod
    def _hold_ready_alloc() -> dict[str, Any]:
        """Return an allocation whose producer is dead behind a running hold."""
        return {
            "ID": "alloc-1",
            "CreateIndex": ALLOCATION_CREATE_INDEX,
            "TaskStates": {
                "run-script": {
                    "State": "dead",
                    "StartedAt": "2024-01-01T00:00:00Z",
                },
                NomadStep.LOG_CAPTURE_HOLD: {
                    "State": "running",
                    "StartedAt": "2024-01-01T00:00:01Z",
                },
            },
        }

    async def _persist_hold_ready(
        self, mock_nomad_cls, session, history, snapshots
    ) -> None:
        """Run a terminal persist on a hold-ready allocation with ``snapshots``."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._growing_stream(
            snapshots
        )
        alloc = self._hold_ready_alloc()
        mock_backend.allocation.get_allocation.return_value = alloc
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )
        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id="alloc-1",
            capture_hold_ready=True,
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_hold_ready_drain_ends_on_first_quiet_refetch(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a hold-ready drain stops once a re-fetch finds nothing new.

        The producers are dead and the hold keeps the allocation alive, so a
        re-fetch a full interval later that returns no bytes on any stream means
        the tail is read, even for a stream that never produced anything.
        """
        await self._persist_hold_ready(
            mock_nomad_cls,
            session,
            created_task_with_history,
            {("run-script", TaskLogType.STDOUT): ["out\n"]},
        )

        stdout = await self._stream_content(
            session, created_task_with_history.id, "run-script", TaskLogType.STDOUT
        )
        assert stdout == "out\n"
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_hold_ready_drain_reads_a_tail_flushed_after_the_first_fetch(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a hold-ready drain keeps reading while the tail still lands."""
        await self._persist_hold_ready(
            mock_nomad_cls,
            session,
            created_task_with_history,
            {("run-script", TaskLogType.STDOUT): ["out\n", "out\ntail\n"]},
        )

        stdout = await self._stream_content(
            session, created_task_with_history.id, "run-script", TaskLogType.STDOUT
        )
        assert stdout == "out\ntail\n"
        assert mock_sleep.await_count == self.EXPECTED_SLEEPS_ALL_STREAMS_DRAINED

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_hold_ready_drain_retries_after_a_failed_refetch(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a failed re-fetch is not mistaken for a quiet hold-ready stream.

        A read that raises returns no bytes, exactly as a stream at EOF does, so
        the drain must keep polling and still read the tail ``logmon`` flushes
        once the read recovers.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        growing = self._growing_stream(
            {("run-script", TaskLogType.STDOUT): ["out\n", "out\ntail\n"]}
        )
        stdout_calls = {"n": 0}

        def fake_stream(alloc_id, *, task, type_, offset):
            if type_ == TaskLogType.STDOUT:
                stdout_calls["n"] += 1
                if stdout_calls["n"] == self.FAILING_STDOUT_CALL:
                    raise BaseNomadException(MagicMock(text="gone"))
            return growing(alloc_id, task=task, type_=type_, offset=offset)

        mock_backend.client.stream_logs.stream.side_effect = fake_stream
        alloc = self._hold_ready_alloc()
        mock_backend.allocation.get_allocation.return_value = alloc
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(
            terminal_log_drain_max_attempts=self.DRAIN_MAX_ATTEMPTS
        )

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id="alloc-1",
            capture_hold_ready=True,
        )

        stdout = await self._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        assert stdout == "out\ntail\n"
        assert mock_sleep.await_count > 1


class TestNomadCaptureHoldDetection:
    """Cover the log-capture-hold terminal-detection helpers."""

    @staticmethod
    def _alloc(task_states: dict) -> dict:
        """Return an allocation carrying the given task states."""
        return {"ID": "alloc-1", "TaskStates": task_states}

    def test_hold_absent_is_never_ready(self) -> None:
        """Assert an allocation with no hold step never reports ready.

        Jobs registered before the hold existed carry no such step, and their
        logs are already collectable, so there is nothing to release.
        """
        alloc = self._alloc({"run-script": {"State": "dead"}})

        assert _detect_capture_hold_ready(alloc) is False
        assert _capture_hold_step_state(alloc) is None

    def test_ready_once_every_producing_step_is_dead(self) -> None:
        """Assert a live hold plus all-dead producers reports ready."""
        alloc = self._alloc(
            {
                "run-script": {"State": "dead"},
                "clean-up": {"State": "dead"},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
            }
        )

        assert _detect_capture_hold_ready(alloc) is True

    def test_not_ready_while_a_producing_step_still_runs(self) -> None:
        """Assert one live producer keeps the allocation from being ready."""
        alloc = self._alloc(
            {
                "run-script": {"State": "dead"},
                "clean-up": {"State": "running"},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
            }
        )

        assert _detect_capture_hold_ready(alloc) is False

    def test_not_ready_while_a_producing_step_is_still_pending(self) -> None:
        """Assert a producer that never started blocks readiness.

        A ``check-staleness`` abort can leave a main task at ``pending`` rather
        than ``dead``; treating that as drained would signal the hold before
        the producer set finished.
        """
        alloc = self._alloc(
            {
                "check-staleness": {"State": "dead"},
                "run-script": {"State": "pending"},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
            }
        )

        assert _detect_capture_hold_ready(alloc) is False

    def test_hold_alone_is_not_ready(self) -> None:
        """Assert an allocation whose only step is the hold is not ready."""
        alloc = self._alloc({NomadStep.LOG_CAPTURE_HOLD: {"State": "running"}})

        assert _detect_capture_hold_ready(alloc) is False

    def test_status_derives_from_producing_steps_only(self) -> None:
        """Assert a failed hold does not re-label a task whose work succeeded."""
        alloc = self._alloc(
            {
                "run-script": {"State": "dead", "Failed": False},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "dead", "Failed": True},
            }
        )

        assert _status_from_step_states(alloc) == TaskHistoryStatusEnum.SUCCESS

    def test_status_is_failed_when_a_producing_step_failed(self) -> None:
        """Assert a failing producer drives the derived status to FAILED."""
        alloc = self._alloc(
            {
                "run-script": {"State": "dead", "Failed": True},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            }
        )

        assert _status_from_step_states(alloc) == TaskHistoryStatusEnum.FAILED


class TestNomadCaptureHoldReadyJobIds:
    """Cover the listing of capture-hold-ready jobs."""

    @staticmethod
    def _alloc(
        job_id: str,
        task_states: dict[str, dict[str, Any]],
        client_status: NomadAllocStatusEnum = NomadAllocStatusEnum.RUNNING,
        create_index: int = 1,
    ) -> dict[str, Any]:
        """Return an allocation stub for ``job_id``."""
        return {
            "ID": f"alloc-{job_id}-{create_index}",
            "JobID": job_id,
            "ClientStatus": client_status,
            "CreateIndex": create_index,
            "TaskStates": task_states,
        }

    _LISTING_URL = re.compile(r"^http://localhost:4646/v1/allocations\?")

    @pytest.mark.asyncio
    async def test_returns_job_ids_of_hold_ready_allocations(self) -> None:
        """Assert only allocations whose producers are done and hold is up count.

        The listing asks Nomad for pending, running and failed allocations with
        their task states: Nomad reports an allocation ``failed`` as soon as a
        producing step fails, even while its hold still runs.
        """
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(
                self._LISTING_URL,
                payload=[
                    self._alloc(
                        "job-ready",
                        {
                            "run-script": {"State": "dead"},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
                        },
                    ),
                    self._alloc(
                        "job-producing",
                        {
                            "run-script": {"State": "running"},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "pending"},
                        },
                    ),
                    self._alloc("job-no-hold", {"run-script": {"State": "dead"}}),
                    self._alloc(
                        "job-failed",
                        {
                            "run-script": {"State": "dead", "Failed": True},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
                        },
                        client_status=NomadAllocStatusEnum.FAILED,
                    ),
                ],
            )
            async with executor:
                ready = await executor.capture_hold_ready_job_ids(
                    ["job-ready", "job-producing", "job-no-hold", "job-failed"]
                )

        assert ready == frozenset({"job-ready", "job-failed"})

    @pytest.mark.asyncio
    async def test_listing_is_filtered_to_the_given_jobs(self) -> None:
        """Assert Nomad is asked only about the given jobs, never the whole cluster.

        Each job ID is quoted as a JSON string literal, so one carrying a quote
        cannot break out of the filter expression.
        """
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(self._LISTING_URL, payload=[])
            async with executor:
                await executor.capture_hold_ready_job_ids(["job-a", 'job-"b'])

        (_, (request,)) = nomad.requests.popitem()
        assert request.kwargs["params"] == {
            "filter": (
                f'(ClientStatus == "{NomadAllocStatusEnum.PENDING}"'
                f' or ClientStatus == "{NomadAllocStatusEnum.RUNNING}"'
                f' or ClientStatus == "{NomadAllocStatusEnum.FAILED}")'
                ' and (JobID == "job-a" or JobID == "job-\\"b")'
            ),
            "task_states": "true",
        }

    @pytest.mark.asyncio
    async def test_no_job_ids_makes_no_request(self) -> None:
        """Assert an empty job list returns at once without calling Nomad."""
        executor = _build_executor()
        with aioresponses() as nomad:
            async with executor:
                ready = await executor.capture_hold_ready_job_ids([])

        assert ready == frozenset()
        assert nomad.requests == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "replacement_status",
        [NomadAllocStatusEnum.PENDING, NomadAllocStatusEnum.RUNNING],
    )
    async def test_superseded_failed_allocation_does_not_mark_its_job_ready(
        self, replacement_status: NomadAllocStatusEnum
    ) -> None:
        """Assert readiness is judged on the job's newest allocation only.

        A rescheduled job keeps its failed predecessor, which still looks
        hold-ready, beside the replacement every sync resolves; reporting the
        job would re-dispatch a sync of the still-running replacement on every
        tick.
        """
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(
                self._LISTING_URL,
                payload=[
                    self._alloc(
                        "job-retried",
                        {
                            "run-script": {"State": "dead", "Failed": True},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "running"},
                        },
                        client_status=NomadAllocStatusEnum.FAILED,
                        create_index=10,
                    ),
                    self._alloc(
                        "job-retried",
                        {
                            "run-script": {"State": "pending"},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "pending"},
                        },
                        client_status=replacement_status,
                        create_index=20,
                    ),
                ],
            )
            async with executor:
                ready = await executor.capture_hold_ready_job_ids(["job-retried"])

        assert ready == frozenset()

    @pytest.mark.asyncio
    async def test_failed_allocation_with_exited_hold_is_not_listed(self) -> None:
        """Assert a retained failed allocation whose hold has exited is skipped.

        Nomad keeps such an allocation until it collects it; reporting it would
        keep the finishing-run probe from short-circuiting on an empty result
        for as long as the allocation lingers.
        """
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(
                self._LISTING_URL,
                payload=[
                    self._alloc(
                        "job-exited-hold",
                        {
                            "run-script": {"State": "dead", "Failed": True},
                            NomadStep.LOG_CAPTURE_HOLD: {"State": "dead"},
                        },
                        client_status=NomadAllocStatusEnum.FAILED,
                    ),
                ],
            )
            async with executor:
                ready = await executor.capture_hold_ready_job_ids(["job-exited-hold"])

        assert ready == frozenset()

    @pytest.mark.asyncio
    async def test_empty_allocation_list_returns_empty_set(self) -> None:
        """Assert a job with no live allocation is not reported."""
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(self._LISTING_URL, payload=[])
            async with executor:
                ready = await executor.capture_hold_ready_job_ids(["job-a"])

        assert ready == frozenset()

    @pytest.mark.asyncio
    async def test_error_status_propagates(self) -> None:
        """Assert a Nomad error status reaches the caller, which owns the fallback."""
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(self._LISTING_URL, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            async with executor:
                with pytest.raises(ClientResponseError):
                    await executor.capture_hold_ready_job_ids(["job-a"])

    @pytest.mark.asyncio
    async def test_non_json_body_propagates(self) -> None:
        """Assert a 2xx whose body is not JSON reaches the caller as ``ValueError``."""
        executor = _build_executor()
        with aioresponses() as nomad:
            nomad.get(
                self._LISTING_URL,
                body="<html>proxy error</html>",
                content_type="application/json",
            )
            async with executor:
                with pytest.raises(json.JSONDecodeError):
                    await executor.capture_hold_ready_job_ids(["job-a"])


class TestNomadCaptureHoldRelease:
    """Cover the hold-release signal and its guards."""

    @pytest.fixture(autouse=True)
    def mock_sleep(self) -> Iterator[AsyncMock]:
        """Patch the inter-attempt wait so polling costs no wall-clock time."""
        with patch(
            "app.tasks.execution.executors.nomad.models.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock:
            yield mock

    @staticmethod
    def _alloc(hold_state: str | None) -> dict[str, Any]:
        """Return an allocation whose hold step carries ``hold_state``."""
        task_states = {"run-script": {"State": "dead"}}
        if hold_state is not None:
            task_states[NomadStep.LOG_CAPTURE_HOLD] = {"State": hold_state}
        return {"ID": "alloc-1", "TaskStates": task_states}

    @classmethod
    def _backend_serving(cls, mock_nomad_cls, alloc: dict[str, Any]) -> MagicMock:
        """Wire a backend whose allocation re-read returns ``alloc``."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = alloc
        return mock_backend

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_signals_the_hold_task_by_name(self, mock_nomad_cls) -> None:
        """Assert the release targets only the hold step, not the allocation."""
        alloc = self._alloc("running")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drain_settings", DRAIN_SETTINGS_VARIANTS)
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_does_not_signal_a_hold_that_stays_pending(
        self, mock_nomad_cls, mock_sleep, drain_settings: dict[str, float]
    ) -> None:
        """Assert a hold pending for the whole budget is left to expire.

        A signal delivered to a pending step is dropped, so spending the budget
        without seeing it start has to stay a non-event: no signal, no raised
        exception, and the hold's own deadline left as the residency bound. The
        budget bounds an internal Nomad scheduling window, so no drain setting
        may stretch or shrink it — a derived budget is what let a zeroed drain
        forfeit the release in the first place.
        """
        alloc = self._alloc("pending")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor(**drain_settings)

        await executor._release_capture_hold(alloc)

        assert (
            mock_backend.allocation.get_allocation.call_count
            == _CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS
        )
        assert mock_sleep.await_args_list == [
            call(_CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS)
        ] * (_CAPTURE_HOLD_RELEASE_MAX_ATTEMPTS - 1)
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_does_not_signal_an_already_dead_hold(self, mock_nomad_cls) -> None:
        """Assert a hold that already expired is not signalled."""
        alloc = self._alloc("dead")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_does_not_signal_when_no_hold_step_exists(
        self, mock_nomad_cls
    ) -> None:
        """Assert a pre-upgrade allocation is never signalled."""
        alloc = self._alloc(None)
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drain_settings", DRAIN_SETTINGS_VARIANTS)
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_polls_until_the_hold_starts(
        self, mock_nomad_cls, mock_sleep, drain_settings: dict[str, float]
    ) -> None:
        """Assert a hold that has not started yet is waited out, then signalled.

        The hold is a poststop step, so it only starts once Nomad has finished
        killing the payload. Reading once inside that window would forfeit the
        release the method exists to issue. Zeroing the drain is a supported way
        to keep terminal syncs off the beat's critical path, so it must not cost
        the release that chance either.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = [
            self._alloc("pending"),
            self._alloc("pending"),
            self._alloc("running"),
        ]
        executor = _build_executor(**drain_settings)

        await executor._release_capture_hold(self._alloc("pending"))

        assert (
            mock_backend.allocation.get_allocation.call_count
            == EXPECTED_HOLD_READS_UNTIL_RUNNING
        )
        assert mock_sleep.await_count == EXPECTED_HOLD_READS_UNTIL_RUNNING - 1
        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stops_polling_early_on_a_dead_hold(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert polling gives up as soon as the hold is past signalling.

        A hold already ``dead`` — or absent, on a pre-upgrade allocation — will
        never become signallable, so spending the whole attempt budget on it
        would just delay the stop.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = self._alloc("dead")
        executor = _build_executor()

        await executor._release_capture_hold(self._alloc("dead"))

        assert mock_backend.allocation.get_allocation.call_count == 1
        mock_sleep.assert_not_awaited()
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_signals_on_the_re_read_state_not_the_stale_snapshot(
        self, mock_nomad_cls
    ) -> None:
        """Assert a hold that started after the sync began is still released.

        The snapshot the caller holds predates the capture work, so a hold that
        was ``pending`` then is typically running by now. Reading the stale copy
        would forfeit the early release on the common path and pin the
        allocation for its full deadline.
        """
        stale = self._alloc("pending")
        mock_backend = self._backend_serving(mock_nomad_cls, self._alloc("running"))
        executor = _build_executor()

        await executor._release_capture_hold(stale)

        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_re_read_failure_does_not_escape(self, mock_nomad_cls) -> None:
        """Assert a failed allocation re-read degrades instead of raising.

        This runs at the tail of an otherwise-successful sync; letting it
        propagate would lose the terminal status the caller just stamped.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = BaseNomadException(
            MagicMock(text="gone")
        )
        executor = _build_executor()

        await executor._release_capture_hold(self._alloc("running"))

        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_non_json_signal_response_does_not_escape(
        self, mock_nomad_cls
    ) -> None:
        """Assert a non-JSON signal response is swallowed like a Nomad error.

        ``signal_allocation`` decodes the response body, so an empty or
        non-JSON body raises ``ValueError`` rather than a Nomad exception — a
        family that would otherwise escape the handler and abort the sync.
        """
        alloc = self._alloc("running")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        mock_backend.client.allocation.signal_allocation.side_effect = ValueError(
            "Expecting value: line 1 column 1 (char 0)"
        )
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        mock_backend.client.allocation.signal_allocation.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_release_failure_does_not_escape(self, mock_nomad_cls) -> None:
        """Assert a failed signal is swallowed rather than aborting the sync.

        The bytes are already persisted by the time the release runs and the
        step self-expires at its deadline, so a failed release costs residency,
        not data.
        """
        alloc = self._alloc("running")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        mock_backend.client.allocation.signal_allocation.side_effect = (
            BaseNomadException(MagicMock(text="denied"))
        )
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        mock_backend.client.allocation.signal_allocation.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_a_running_hold_is_signalled_without_waiting(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert polling adds no latency to a hold that has already started.

        This is the common case on every path, so the budget may only be spent
        inside the window that would otherwise forfeit the release.
        """
        alloc = self._alloc("running")
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        assert mock_backend.allocation.get_allocation.call_count == 1
        mock_sleep.assert_not_awaited()
        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hold_step", [{"State": None}, {}])
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_an_unreadable_hold_state_costs_a_single_read(
        self, mock_nomad_cls, mock_sleep, hold_step: dict[str, Any]
    ) -> None:
        """Assert a hold whose state cannot be read is never polled for.

        A missing or malformed ``State`` is indistinguishable from an absent
        step as far as signalling goes, and waiting cannot make either
        signallable.
        """
        alloc = {
            "ID": "alloc-1",
            "TaskStates": {
                "run-script": {"State": "dead"},
                NomadStep.LOG_CAPTURE_HOLD: hold_step,
            },
        }
        mock_backend = self._backend_serving(mock_nomad_cls, alloc)
        executor = _build_executor()

        await executor._release_capture_hold(alloc)

        assert mock_backend.allocation.get_allocation.call_count == 1
        mock_sleep.assert_not_awaited()
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_a_re_read_failure_mid_poll_does_not_escape(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert Nomad going away part-way through the poll degrades quietly.

        The polled reads run at the tail of an otherwise-successful sync, so a
        late failure must not lose the terminal status the caller just stamped.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = [
            self._alloc("pending"),
            BaseNomadException(MagicMock(text="gone")),
        ]
        executor = _build_executor()

        await executor._release_capture_hold(self._alloc("pending"))

        assert (
            mock_backend.allocation.get_allocation.call_count
            == EXPECTED_HOLD_READS_MID_POLL_FAILURE
        )
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_a_polled_release_failure_does_not_escape(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert the swallow also covers a signal issued after waiting."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = [
            self._alloc("pending"),
            self._alloc("running"),
        ]
        mock_backend.client.allocation.signal_allocation.side_effect = (
            BaseNomadException(MagicMock(text="denied"))
        )
        executor = _build_executor()

        await executor._release_capture_hold(self._alloc("pending"))

        mock_backend.client.allocation.signal_allocation.assert_called_once()


class TestNomadCaptureOutcomes:
    """Cover per-stream capture verdicts written at terminal sync."""

    HOLD_ALLOC_ID = "alloc-hold"

    @staticmethod
    def _alloc(steps: dict[str, str], *, hold_state: str | None = "running") -> dict:
        """Return a terminal allocation with the given step states."""
        task_states = {
            step: {"StartedAt": "2024-01-01T00:00:00Z", "State": state}
            for step, state in steps.items()
        }
        if hold_state is not None:
            task_states[NomadStep.LOG_CAPTURE_HOLD] = {
                "StartedAt": "2024-01-01T00:00:00Z",
                "State": hold_state,
            }
        return {
            "ID": TestNomadCaptureOutcomes.HOLD_ALLOC_ID,
            "CreateIndex": ALLOCATION_CREATE_INDEX,
            "TaskStates": task_states,
        }

    @staticmethod
    async def _verdicts(session, history_id) -> dict[tuple[str, str], str]:
        """Return the recorded verdict for every ``(source, stream)`` pair."""
        rows = await TaskHistoryLogStateManager.list_for_task(session, history_id)
        return {(row.source, row.stream): row.capture_status for row in rows}

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_silent_stream_is_recorded_complete(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a step that emitted nothing is recorded, not left rowless.

        This is the reader contract the whole change exists for: without a row
        a silent step is indistinguishable from one whose bytes were lost.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "dead"}),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        verdicts = await self._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.COMPLETE
        )
        assert verdicts[("run-script", TaskLogType.STDERR)] == (
            LogCaptureStatusEnum.COMPLETE
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_hold_step_gets_no_verdict_of_its_own(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert the hold step is never recorded as a captured stream."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "dead"}),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        verdicts = await self._verdicts(session, history.id)
        assert not [
            source for source, _ in verdicts if source == NomadStep.LOG_CAPTURE_HOLD
        ]

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_a_live_hold_caps_every_verdict_at_incomplete(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert nothing is called complete while the hold is unreleasable.

        A hold that is present but not yet releasable means some producer has
        not finished, so "this stream is at EOF" is not yet knowable — even
        for a stream whose own fetch succeeded.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "dead", "clean-up": "pending"}),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=False,
        )

        verdicts = await self._verdicts(session, history.id)
        assert set(verdicts.values()) == {LogCaptureStatusEnum.INCOMPLETE}
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pre_upgrade_allocation_still_earns_complete(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert an allocation with no hold at all can still be complete.

        Jobs registered before the hold step existed have nothing to wait on,
        so a clean drain there is as final as it will ever be.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "dead"}, hold_state=None),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=False,
        )

        verdicts = await self._verdicts(session, history.id)
        assert set(verdicts.values()) == {LogCaptureStatusEnum.COMPLETE}
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_failed_fetch_marks_only_its_own_stream_incomplete(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert one stream's fetch failure leaves its siblings complete.

        Letting the failure propagate would abort the cycle for every step in
        the allocation, trading a silent per-stream loss for a loud
        whole-cycle one.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend

        def fake_stream(alloc_id, *, task, type_, offset):
            if type_ == TaskLogType.STDERR:
                raise BaseNomadException(MagicMock(text="gone"))
            return ""

        mock_backend.client.stream_logs.stream.side_effect = fake_stream
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "dead"}),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        verdicts = await self._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.COMPLETE
        )
        assert verdicts[("run-script", TaskLogType.STDERR)] == (
            LogCaptureStatusEnum.INCOMPLETE
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_release_is_issued_once_capture_is_recorded(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert the hold is signalled only after the verdicts are written."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        alloc = self._alloc({"run-script": "dead", "clean-up": "dead"})
        # The release re-reads the allocation before signalling.
        mock_backend.allocation.get_allocation.return_value = alloc
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            self.HOLD_ALLOC_ID, "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )
        mock_sleep.assert_not_awaited()
        verdicts = await self._verdicts(session, history.id)
        assert ("clean-up", TaskLogType.STDOUT) in verdicts

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_release_polls_a_pending_hold_with_the_drain_disabled(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ) -> None:
        """Assert the sync path waits out the hold's start window on its own.

        With the drain disabled the sync has no sleeps of its own, so it can
        reach the release inside the window where the poststop hold exists but
        has not started. The verdicts are already written by then and stay as
        they were.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        alloc = self._alloc({"run-script": "dead"}, hold_state="pending")
        mock_backend.allocation.get_allocation.side_effect = [
            alloc,
            self._alloc({"run-script": "dead"}),
        ]
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            self.HOLD_ALLOC_ID, "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )
        verdicts = await self._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.COMPLETE
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_release_does_not_poll_a_hold_past_signalling(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ) -> None:
        """Assert a hold that died before the release is due costs a single read.

        Readiness is detected on a snapshot taken before the capture work, so
        the hold can reach its own deadline in the meantime. Polling one that
        can never be signalled again would only stretch the beat cycle.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        alloc = self._alloc({"run-script": "dead"})
        mock_backend.allocation.get_allocation.return_value = self._alloc(
            {"run-script": "dead"}, hold_state="dead"
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=alloc,
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=True,
        )

        assert mock_backend.allocation.get_allocation.call_count == 1
        mock_sleep.assert_not_awaited()
        mock_backend.client.allocation.signal_allocation.assert_not_called()
        verdicts = await self._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.COMPLETE
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_running_history_records_nothing_and_does_not_release(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a still-running sync neither classifies nor releases."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.return_value = ""
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.RUNNING
        executor = _build_executor(terminal_log_drain_max_attempts=0)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=self._alloc({"run-script": "running"}),
            previous_allocation_id=self.HOLD_ALLOC_ID,
            capture_hold_ready=False,
        )

        mock_backend.client.allocation.signal_allocation.assert_not_called()
        assert await self._verdicts(session, history.id) == {}


class TestNomadSyncWithCaptureHold:
    """Cover terminal detection when a log-capture-hold step is present."""

    @staticmethod
    def _backend(mock_nomad_cls, task_states: dict, *, job: dict) -> MagicMock:
        """Wire a backend returning one allocation and job."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.RUNNING,
            "TaskStates": task_states,
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        _stub_nomad_calls(job=job)
        return mock_backend

    @staticmethod
    def _queue_item() -> TaskHistory:
        """Return a RUNNING history already landed on the allocation."""
        return _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            },
            status=TaskHistoryStatusEnum.RUNNING,
        )

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_reaches_terminal_status_while_allocation_still_runs(
        self, mock_nomad_cls
    ):
        """Assert the task completes without waiting out the hold.

        The allocation still reports ``running`` because the hold holds it, so
        the status has to come from the producing steps or completion latency
        would inherit the whole hold window.
        """
        self._backend(
            mock_nomad_cls,
            {
                "run-script": {"State": "dead", "Failed": False},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "running", "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        assert result.finished_at is not None

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_failing_step_yields_failed_while_hold_runs(self, mock_nomad_cls):
        """Assert a failed producer is reported FAILED, not SUCCESS."""
        self._backend(
            mock_nomad_cls,
            {
                "run-script": {"State": "dead", "Failed": True},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "running", "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stale_skip_still_wins_over_step_derivation(self, mock_nomad_cls):
        """Assert a stale-skipped allocation stays STALE with a hold present."""
        self._backend(
            mock_nomad_cls,
            {
                "check-staleness": {
                    "State": "dead",
                    "Failed": True,
                    "Events": [{"Type": "Terminated", "ExitCode": 75}],
                },
                "run-script": {"State": "dead", "Failed": False},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "running", "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STALE

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_unlaunchable_wins_over_step_derivation(self, mock_nomad_cls):
        """Assert an unlaunchable allocation resolves behind a live hold too.

        Both branches of the terminal-status resolution special-case the
        sentinels; covering only the dead-job one leaves the held path
        reporting the failed prestart step as an ordinary ``FAILED``.
        """
        self._backend(
            mock_nomad_cls,
            {
                "check-launchable": {
                    "State": "dead",
                    "Failed": True,
                    "Events": [
                        {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE}
                    ],
                },
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "running", "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.UNLAUNCHABLE

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stale_skip_wins_over_unlaunchable_behind_a_hold(
        self, mock_nomad_cls
    ):
        """Assert staleness outranks unlaunchability on the held path as well."""
        self._backend(
            mock_nomad_cls,
            {
                "check-staleness": {
                    "State": "dead",
                    "Failed": True,
                    "Events": [{"Type": "Terminated", "ExitCode": 75}],
                },
                "check-launchable": {
                    "State": "dead",
                    "Failed": True,
                    "Events": [
                        {"Type": "Terminated", "ExitCode": LAUNCH_CHECK_EXIT_CODE}
                    ],
                },
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "running", "Stop": False},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STALE

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_operator_stop_still_wins_over_step_derivation(self, mock_nomad_cls):
        """Assert an operator-stopped job reports STOPPED, not SUCCESS."""
        self._backend(
            mock_nomad_cls,
            {
                "run-script": {"State": "dead", "Failed": False},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "dead", "Stop": True},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.STOPPED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_operator_stop_does_not_relabel_a_failed_step(self, mock_nomad_cls):
        """Assert a stop landing on an already-failed payload still reports FAILED.

        The stop override applies to a success, never to a failure — matching
        ``get_task_history_status_from_alloc_status``, where ``stopped`` guards
        only the ``COMPLETE`` arm. Relabelling here would report an operator
        action where the payload actually errored, and silence anything keyed on
        ``FAILED``.
        """
        self._backend(
            mock_nomad_cls,
            {
                "run-script": {"State": "dead", "Failed": True},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": "dead", "Stop": True},
        )
        executor = _build_executor()

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_dead_job_with_a_pending_producer_does_not_release(
        self, mock_nomad_cls
    ):
        """Assert the two terminal paths overlapping still withholds the signal.

        A ``check-staleness`` abort can leave the job dead while a main task
        sits at ``pending`` and the hold runs on. Releasing there would signal
        before the producer set drained, re-opening the race the hold closes —
        and the job-status path alone cannot catch it.
        """
        mock_backend = self._backend(
            mock_nomad_cls,
            {
                "run-script": {"State": "pending", "Failed": False},
                NomadStep.LOG_CAPTURE_HOLD: {"State": "running", "Failed": False},
            },
            job={"ID": "job-1", "Status": NOMAD_DEAD_JOB_STATUS, "Stop": False},
        )
        executor = _build_executor()

        await executor._sync_task_history(self._queue_item())

        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_pre_upgrade_allocation_keeps_client_status_derivation(
        self, mock_nomad_cls
    ):
        """Assert an allocation with no hold step behaves exactly as before."""
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = {
            "ID": "alloc-1",
            "JobID": "job-1",
            "EvalID": "eval-1",
            "ClientStatus": NomadAllocStatusEnum.COMPLETE,
            "TaskStates": {"run-script": {"StartedAt": "1", "FinishedAt": "2"}},
            "ModifyTime": 1_700_000_000_000_000_000,
        }
        mock_backend.client.stream_logs.stream.return_value = ""
        nomad_job = {
            "ID": "job-1",
            "Status": NOMAD_DEAD_JOB_STATUS,
            "Stop": False,
        }
        executor = _build_executor(nomad_job=nomad_job)

        result = await executor._sync_task_history(self._queue_item())

        assert result.status == TaskHistoryStatusEnum.SUCCESS
        mock_backend.client.allocation.signal_allocation.assert_not_called()


class TestNomadCaptureOutcomeDrainFailures:
    """Cover that a failure during the terminal drain reaches the verdict."""

    DRAIN_ATTEMPTS = 2

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_drain_fetch_failure_downgrades_the_verdict(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a stream whose drain re-fetch fails is not called complete.

        The first fetch succeeding says nothing about the tail: the drain exists
        precisely because ``logmon`` flushes asynchronously, so a failure there
        means bytes may be missing and the honest verdict is incomplete.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        calls = {"n": 0}

        def fake_stream(alloc_id, *, task, type_, offset):
            if type_ != TaskLogType.STDOUT:
                return ""
            calls["n"] += 1
            if calls["n"] > 1:
                raise BaseNomadException(MagicMock(text="gone"))
            return ""

        mock_backend.client.stream_logs.stream.side_effect = fake_stream
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=self.DRAIN_ATTEMPTS)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=TestNomadCaptureOutcomes._alloc({"run-script": "dead"}),
            previous_allocation_id="alloc-hold",
            capture_hold_ready=True,
        )

        verdicts = await TestNomadCaptureOutcomes._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.INCOMPLETE
        )
        assert verdicts[("run-script", TaskLogType.STDERR)] == (
            LogCaptureStatusEnum.COMPLETE
        )


class TestNomadCaptureHoldDispatchMeta:
    """Cover that the hold deadline reaches the job as dispatch meta."""

    HOLD_SECONDS = 45

    @staticmethod
    def _task_declaring_hold_meta(*, declares: bool) -> Task:
        """Return a parameterized task whose template may declare the hold key."""
        parameterized = {"Payload": "required"}
        if declares:
            parameterized["MetaOptional"] = ["log_capture_hold_seconds"]
        return Task(
            id=1,
            name="test-task",
            data={"ID": "dispatch-hold", "ParameterizedJob": parameterized},
            backend="nomad",
            owner="ANY",
        )

    @pytest.mark.asyncio
    async def test_configured_deadline_is_injected_as_meta(self) -> None:
        """Assert the executor setting is passed per dispatch, as a string.

        Enforcement lives on the execution host, so the value has to travel
        with the dispatch rather than being read by the shell from PMM Extensions.
        """
        calls = _stub_nomad_calls(dispatch={"DispatchedJobID": "d-1", "EvalID": "e-1"})
        executor = _build_executor(log_capture_hold_seconds=self.HOLD_SECONDS)
        task = self._task_declaring_hold_meta(declares=True)

        await executor.dispatch_job(_build_queue_item(task=task, payload="x"), task)

        meta = _dispatch_body(calls)["Meta"]
        assert meta["log_capture_hold_seconds"] == str(self.HOLD_SECONDS)

    @pytest.mark.asyncio
    async def test_meta_is_withheld_from_a_template_that_does_not_declare_it(
        self,
    ) -> None:
        """Assert a template predating the hold key is dispatched without it.

        Nomad rejects a dispatch carrying meta the parameterized job never
        declared, so gating on the declaration is what lets the setting be
        hot-reloadable without re-registering every job first.
        """
        calls = _stub_nomad_calls(dispatch={"DispatchedJobID": "d-1", "EvalID": "e-1"})
        executor = _build_executor(log_capture_hold_seconds=self.HOLD_SECONDS)
        task = self._task_declaring_hold_meta(declares=False)

        await executor.dispatch_job(_build_queue_item(task=task, payload="x"), task)

        meta = _dispatch_body(calls)["Meta"]
        assert "log_capture_hold_seconds" not in meta


class TestNomadSubCadenceCapture:
    """Cover the headline guarantee: a step shorter than the sync cadence."""

    SUB_CADENCE_STDOUT = "starting\nworking\ndone\n"
    SUB_CADENCE_STDERR = "warning: slow\n"

    @staticmethod
    def _stream_for(payloads: dict[tuple[str, TaskLogType], str]):
        """Return a fake Nomad stream serving each stream's full content once."""

        def fake_stream(alloc_id, *, task, type_, offset):
            content = payloads.get((task, type_), "")
            delta = content[offset:]
            if not delta:
                return ""
            return json.dumps(
                {
                    "Data": b64encode(delta.encode()).decode(),
                    "Offset": len(content),
                }
            )

        return fake_stream

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_never_sampled_step_persists_every_emitted_byte(
        self, mock_nomad_cls, mock_sleep, session, created_task_with_history
    ):
        """Assert a step never sampled while running loses nothing.

        The allocation is still readable at terminal sync only because the hold
        holds it; before this mechanism the source was collected first and the
        stream persisted zero bytes while looking byte-for-byte like a step
        that legitimately printed nothing.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.client.stream_logs.stream.side_effect = self._stream_for(
            {
                ("run-script", TaskLogType.STDOUT): self.SUB_CADENCE_STDOUT,
                ("run-script", TaskLogType.STDERR): self.SUB_CADENCE_STDERR,
            }
        )
        history = created_task_with_history
        history.anonymize_mask = 0
        history.status = TaskHistoryStatusEnum.SUCCESS
        executor = _build_executor(terminal_log_drain_max_attempts=1)

        await executor._persist_nomad_task_logs(
            writer_session=session,
            queue_item=history,
            alloc=TestNomadCaptureOutcomes._alloc({"run-script": "dead"}),
            previous_allocation_id="alloc-hold",
            capture_hold_ready=True,
        )

        stdout = await TestDrainTerminalLogs()._stream_content(
            session, history.id, "run-script", TaskLogType.STDOUT
        )
        stderr = await TestDrainTerminalLogs()._stream_content(
            session, history.id, "run-script", TaskLogType.STDERR
        )
        assert stdout == self.SUB_CADENCE_STDOUT
        assert stderr == self.SUB_CADENCE_STDERR

        verdicts = await TestNomadCaptureOutcomes._verdicts(session, history.id)
        assert verdicts[("run-script", TaskLogType.STDOUT)] == (
            LogCaptureStatusEnum.COMPLETE
        )


class TestNomadStopReleasesCaptureHold:
    """Cover the hold release on the operator-stop path."""

    @staticmethod
    def _alloc(hold_state: str) -> dict:
        """Return an allocation whose hold step carries ``hold_state``."""
        return {
            "ID": "alloc-1",
            "TaskStates": {
                "run-script": {"State": "dead"},
                NomadStep.LOG_CAPTURE_HOLD: {"State": hold_state},
            },
        }

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_releases_a_hold_that_is_still_holding(
        self, mock_nomad_cls, mock_sleep
    ):
        """Assert stopping a task signals a hold that is holding the allocation.

        Deregistering the job does not end the hold: the job goes ``dead`` while
        the allocation stays ``running`` for the rest of the deadline. Nothing is
        captured on this path, so the hold is pure residency and is released.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = self._alloc("running")
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_polls_a_pending_hold_with_the_drain_disabled(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert the stop path keeps its polling when the log drain is off.

        A stop signals immediately after deregistering, before Nomad has killed
        the payload, so the poststop hold is normally still ``pending`` here —
        the path most exposed to losing the release. The hold has to stay
        pending on the release's own first re-read, not just on the read the
        stop already made, or the poll is never exercised.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = [
            self._alloc("pending"),
            self._alloc("pending"),
            self._alloc("running"),
        ]
        executor = _build_executor(terminal_log_drain_max_attempts=0)
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        assert (
            mock_backend.allocation.get_allocation.call_count
            == EXPECTED_STOP_ALLOC_READS_UNTIL_RUNNING
        )
        mock_sleep.assert_awaited_once_with(_CAPTURE_HOLD_RELEASE_INTERVAL_SECONDS)
        mock_backend.client.allocation.signal_allocation.assert_called_once_with(
            "alloc-1", "SIGTERM", task=NomadStep.LOG_CAPTURE_HOLD
        )

    @pytest.mark.asyncio
    @patch(
        "app.tasks.execution.executors.nomad.models.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_does_not_poll_a_hold_past_signalling(
        self, mock_nomad_cls, mock_sleep
    ) -> None:
        """Assert a stop is not slowed down by a hold that already expired.

        A stop is interactive, so spending the budget on a hold that can never
        be signalled again would delay the operator for nothing.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.return_value = self._alloc("dead")
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        assert (
            mock_backend.allocation.get_allocation.call_count
            == EXPECTED_STOP_ALLOC_READS_ON_DEAD_HOLD
        )
        mock_sleep.assert_not_awaited()
        mock_backend.client.allocation.signal_allocation.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.tasks.execution.executors.nomad.models.Nomad")
    async def test_stop_still_deregisters_when_the_allocation_is_gone(
        self, mock_nomad_cls
    ):
        """Assert a missing allocation does not turn a stop into an error.

        The stop must remain effective even when there is no allocation left to
        release — the deregister is the part that actually stops the task.
        """
        mock_backend = MagicMock()
        mock_nomad_cls.return_value = mock_backend
        mock_backend.allocation.get_allocation.side_effect = URLNotFoundNomadException(
            MagicMock(text="gone")
        )
        mock_backend.job.get_allocations.return_value = []
        executor = _build_executor()
        queue_item = _build_queue_item(
            tracking={
                "allocation_id": "alloc-1",
                "evaluation_id": "eval-1",
                "job_id": "job-1",
            }
        )

        await executor._stop_task(queue_item)

        mock_backend.job.deregister_job.assert_called_once_with("job-1")
        mock_backend.client.allocation.signal_allocation.assert_not_called()


class TestPortedNomadCallsUseTheDocumentedEndpoints:
    """Pin the wire format of the five calls that moved off python-nomad.

    Everything else in this module stubs :meth:`NomadExecutor.nomad_json` and so
    says nothing about what actually reaches the socket. python-nomad used to
    build these requests; now the executor does, and a wrong verb, path or body
    key would reach a live Nomad rather than a test. These assertions are that
    check, and each expectation is the request the library sent for the same
    call: ``Job`` wrapping for a registration, ``Payload``/``Meta``/
    ``IdPrefixTemplate`` for a dispatch, ``filter`` for a node listing.
    """

    @staticmethod
    @contextmanager
    def _capture(
        body: Any,
    ) -> Generator[list[tuple[str, str, dict[str, Any]]], None, None]:
        """Answer every request with ``body``, recording the requests made.

        Patched on the class, not on the executor under test: an un-entered
        executor runs its call on a private instance built by
        ``_private_executor``, so an instance patch would never be reached and
        the request would go to a real socket.

        :param body: The JSON body each call answers with.
        :yield: The recorded ``(method, path, kwargs)`` of every request.
        """
        requests: list[tuple[str, str, dict[str, Any]]] = []
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value=body)

        def _request(method: str, path: str, **kwargs: Any) -> Any:
            requests.append((method, path, kwargs))
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch.object(NomadExecutor, "_request", side_effect=_request):
            yield requests

    @pytest.mark.asyncio
    async def test_register_job_posts_the_spec_to_the_job_endpoint(self) -> None:
        """Assert a registration is ``POST /v1/job/{id}`` with a ``Job`` wrapper."""
        executor = _build_executor(stub_nomad=False)
        task = _build_task(task_id="wire-reg")

        with self._capture({"EvalID": "e-1"}) as requests:
            await executor.register_job(task)

        assert len(requests) == 1
        method, path, kwargs = requests[0]
        assert (method, path) == ("POST", "/v1/job/wire-reg")
        assert kwargs["json"] == {"Job": task.data}

    @pytest.mark.asyncio
    async def test_dispatch_job_posts_to_the_dispatch_subpath(self) -> None:
        """Assert a dispatch is ``POST /v1/job/{id}/dispatch`` with Nomad's keys."""
        executor = _build_executor(stub_nomad=False)
        task = _build_task(task_id="wire-dispatch", parameterized=True)
        queue_item = _build_queue_item(task=task, meta={"target": "n"})

        with self._capture({"DispatchedJobID": "d-1", "EvalID": "e-1"}) as requests:
            await executor.dispatch_job(queue_item, task)

        assert len(requests) == 1
        method, path, kwargs = requests[0]
        assert (method, path) == ("POST", "/v1/job/wire-dispatch/dispatch")
        # The values, not just the key set: ``IdPrefixTemplate`` is the
        # concatenation this port rewrote, and a mangled ``Payload`` reaches
        # Nomad as a job that cannot run.
        body = kwargs["json"]
        assert set(body) == {"Payload", "Meta", "IdPrefixTemplate"}
        assert body["Payload"] is None
        assert body["IdPrefixTemplate"] == f"{slugify(task.name)}-{task.id}"
        assert body["Meta"] == {
            "target": "n",
            "staleness_threshold_seconds": str(
                tasks_settings.STALENESS_THRESHOLD_SECONDS
            ),
            "scheduled_at": str(int(queue_item.created_at.timestamp())),
        }

    @pytest.mark.asyncio
    async def test_get_job_gets_the_job_endpoint(self) -> None:
        """Assert a job read is a bare ``GET /v1/job/{id}``."""
        executor = _build_executor(stub_nomad=False)

        with self._capture({"ID": "wire-get"}) as requests:
            await executor.get_job("wire-get")

        assert len(requests) == 1
        method, path, kwargs = requests[0]
        assert (method, path) == ("GET", "/v1/job/wire-get")
        assert set(kwargs) == {"timeout"}

    @pytest.mark.asyncio
    async def test_get_hosts_filters_through_the_nodes_query_string(self) -> None:
        """Assert the host filter travels as the ``filter`` query parameter.

        python-nomad took the expression as ``filter_`` and renamed it on the way
        out; sending the Python spelling would filter nothing and quietly report
        every node as usable.
        """
        executor = _build_executor(stub_nomad=False)

        with self._capture([]) as requests:
            await executor.get_hosts()

        method, path, kwargs = requests[0]
        assert (method, path) == ("GET", "/v1/nodes")
        # The expression too, not just the key: a filter that names the wrong
        # driver or status would still be a ``filter`` and would still answer.
        assert kwargs["params"] == {
            "filter": (
                f"Status == {NODE_STATUS_READY} "
                f"and {RAW_EXEC_DRIVER} in Drivers "
                f"and Drivers.{RAW_EXEC_DRIVER}.Healthy == true"
            )
        }

    @pytest.mark.asyncio
    async def test_get_host_states_sends_no_filter_at_all(self) -> None:
        """Assert the every-node listing omits the parameter rather than blanking it.

        ``filter=`` with an empty value is not the same request as no ``filter``,
        and this call has to see the nodes ``get_hosts`` screens out.
        """
        executor = _build_executor(stub_nomad=False)

        with self._capture([]) as requests:
            await executor.get_host_states()

        assert len(requests) == 1
        method, path, kwargs = requests[0]
        assert (method, path) == ("GET", "/v1/nodes")
        assert kwargs["params"] is None


class TestPortedNomadCallsKeepTheirErrorContract:
    """Pin that a Nomad failure still arrives as a ``BaseNomadException``.

    Callers outside this package depend on it, and none of them is obvious from
    the executor: ``app.tasks.main.nomad_exception_handler`` answers a route with
    502 and "make sure the agent is online", and ``app.tasks.celery`` raises the
    periodic-dispatch failure alert on ``BaseNomadException``. Moving these calls
    onto aiohttp would have let ``ClientError`` escape instead, turning the first
    into a bare 500 and silencing the second - a monitoring product quietly
    stopping its own alerts.
    """

    @staticmethod
    @contextmanager
    def _failing_request(exc: Exception) -> Generator[None, None, None]:
        """Make the executor's next request fail with ``exc``.

        :param exc: Raised in place of sending the request.
        :yield: Nothing; the patch is active for the block.
        """

        def _request(*_args: Any, **_kwargs: Any) -> Any:
            raise exc

        with patch.object(
            NomadExecutor, "_request", side_effect=_request, autospec=True
        ):
            yield

    @pytest.mark.parametrize(
        "failure",
        [
            ClientError("connection refused"),
            TimeoutError("timed out"),
        ],
        ids=["transport", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_a_request_that_gets_no_answer_is_still_a_nomad_error(
        self, failure: Exception
    ) -> None:
        """Assert a failure with no HTTP answer is a BaseNomadException with no status."""
        executor = _build_executor(stub_nomad=False)

        with (
            self._failing_request(failure),
            pytest.raises(NomadRequestError) as exc_info,
        ):
            await executor.get_hosts()

        assert isinstance(exc_info.value, BaseNomadException)
        assert exc_info.value.status_code is None

    @pytest.mark.asyncio
    async def test_an_error_status_is_a_nomad_error_carrying_that_status(self) -> None:
        """Assert an error status survives as ``status_code`` for callers to act on."""
        executor = _build_executor(stub_nomad=False)
        response = AsyncMock()
        response.raise_for_status = MagicMock(
            side_effect=ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(NomadExecutor, "_request", return_value=ctx),
            pytest.raises(NomadRequestError) as exc_info,
        ):
            await executor.get_hosts()

        assert isinstance(exc_info.value, BaseNomadException)
        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    @pytest.mark.parametrize(
        ("decode_failure", "expected_message"),
        [
            (
                json.JSONDecodeError("Expecting value", "{not json", 0),
                "GET /v1/nodes failed",
            ),
            (
                ContentTypeError(request_info=MagicMock(), history=()),
                "with a body that is not JSON",
            ),
        ],
        ids=["labelled-json-that-does-not-parse", "not-labelled-json-at-all"],
    )
    @pytest.mark.asyncio
    async def test_a_body_that_will_not_decode_is_still_a_nomad_error(
        self, decode_failure: Exception, expected_message: str
    ) -> None:
        """Assert a body the client cannot read does not escape as a bare error.

        ``response.json()`` raises on both of these, and neither is a
        ``ClientError``: ``JSONDecodeError`` is a ``ValueError``, and
        ``ContentTypeError`` is a ``ClientResponseError`` whose status was fine.
        Letting either through would answer a route with 500 instead of 502 and
        skip the periodic-dispatch alert, which is the contract the rest of this
        class is about.

        The message matters for the second one: a ``ContentTypeError`` carries
        the answering status, so reporting it the way a real error status is
        reported would say "Nomad answered 200" to an operator reading the log.
        """
        executor = _build_executor(stub_nomad=False)
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(side_effect=decode_failure)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(NomadExecutor, "_request", return_value=ctx),
            pytest.raises(NomadRequestError) as exc_info,
        ):
            await executor.get_hosts()

        assert isinstance(exc_info.value, BaseNomadException)
        assert expected_message in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_the_calls_use_the_executors_timeout_not_the_sessions(self) -> None:
        """Assert the configured ``timeout`` is applied per call.

        The shared session is built for log streaming and allows 300s total with
        a 120s socket read; python-nomad received ``self.timeout``, which
        defaults to 10s and is hot-reloadable. Inheriting the session's budget
        would ignore an operator's setting and let a hung Nomad hold a dispatch,
        and its database connection, thirty times longer than asked.
        """
        executor = _build_executor(stub_nomad=False, timeout=CONFIGURED_NOMAD_TIMEOUT)
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value=[])
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        seen: list[Any] = []

        def _request(_method: str, _path: str, **kwargs: Any) -> Any:
            seen.append(kwargs.get("timeout"))
            return ctx

        with patch.object(NomadExecutor, "_request", side_effect=_request):
            await executor.get_hosts()

        assert seen[0].total == CONFIGURED_NOMAD_TIMEOUT


class TestPortedNomadCallsRunOnAnUnenteredExecutor:
    """Pin that the ported calls work on an executor nobody entered.

    This is the regression that shipped with the port. The inherited
    ``_request`` reaches straight for ``self._session``, and only the executor
    :class:`~app.tasks.execution.nomad_lifecycle.NomadLifecycle` owns is ever
    entered. :func:`~app.tasks.deps.get_executor` hands back an un-entered one,
    whose session is ``None`` - and ``get_executor_for_task`` wraps it, so the
    dispatch route, every Celery task and the connectivity check all hold one.
    While these calls went through the synchronous ``self.backend`` that was
    fine; as aiohttp calls the same line raised ``AttributeError: 'NoneType'
    object has no attribute 'request'`` and the dispatch answered 500.

    Sending those callers to the entered executor instead is not available: a
    Celery worker has no ``app.state``, so there is no holder there to ask. So
    the call runs on a private executor instead, and these tests are about what
    "private" has to mean: nothing the shared instance holds may be opened,
    closed or mutated on its behalf.

    :cvar NODES: One ready node, in the shape ``GET /v1/nodes`` answers with.
    :cvar CALLERS: Callers to race in
        :meth:`test_concurrent_callers_each_get_a_live_session`.
    """

    NODES = [{"Name": "node-1", "Address": "10.0.0.1"}]
    CALLERS = 20

    @staticmethod
    @contextmanager
    def _answer(body: Any) -> Generator[list[Any], None, None]:
        """Answer every request with ``body``, recording the session used.

        Patches ``aiohttp.ClientSession.request`` rather than the executor's
        ``_request``, so the line that actually reads ``self._session`` runs. A
        test stubbing ``_request`` would pass with the session still ``None``,
        which is precisely the bug.

        Records ``(session, closed)`` as the call is served rather than the
        session alone: by the time the block exits every private session has
        been closed on purpose, so only the state at call time says whether the
        call was served on a usable one.

        :param body: The JSON body each call answers with.
        :yield: The ``(session, closed-at-call-time)`` of each recorded call.
        """
        sessions: list[Any] = []
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value=body)

        def _request(session: Any, *_args: Any, **_kwargs: Any) -> Any:
            sessions.append((session, session.closed))
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch(
            "aiohttp.ClientSession.request", autospec=True, side_effect=_request
        ):
            yield sessions

    @pytest.mark.asyncio
    async def test_get_hosts_answers_without_the_executor_being_entered(self) -> None:
        """Assert the call that the dispatch path makes needs no ``__aenter__``."""
        executor = _build_executor(stub_nomad=False)
        assert executor.session is None

        with self._answer(self.NODES):
            hosts = await executor.get_hosts()

        assert hosts == {"node-1": "10.0.0.1"}

    @pytest.mark.asyncio
    async def test_the_call_leaves_the_shared_executor_untouched(self) -> None:
        """Assert the call opens nothing on the executor it was invoked on.

        With no ``NOMAD`` override that executor *is* the YAML settings object,
        one instance process-wide. Opening a session on it would quietly turn it
        into an entered executor for everybody after it; closing the session
        afterwards would drop the cached :attr:`backend` and the
        ``requests.Session`` with it, so the next synchronous
        ``self.backend.*`` call - the allocation reads on the sync path - would
        rebuild its client for nothing.
        """
        executor = _build_executor(stub_nomad=False)
        backend_before = executor.backend

        with self._answer(self.NODES):
            await executor.get_hosts()

        assert executor.session is None
        assert executor.backend is backend_before

    @pytest.mark.asyncio
    async def test_an_override_copy_taken_mid_call_has_no_session_to_inherit(
        self,
    ) -> None:
        """Assert a ``model_copy`` of the executor cannot inherit a live borrow.

        The settings-override snapshot is built with ``model_copy``, which
        carries pydantic private attributes across. A session left on the shared
        instance while a call is in flight would therefore be inherited by the
        snapshot's copy, which would then raise ``RuntimeError: Session is
        closed`` once the original retired it - not a ``BaseNomadException``, so
        a route would answer a bare 500 and the periodic-dispatch alert would be
        skipped.
        """
        executor = _build_executor(stub_nomad=False)
        copies: list[NomadExecutor] = []
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value=self.NODES)

        def _request(_session: Any, *_args: Any, **_kwargs: Any) -> Any:
            # Snapshot while the request is in flight, which is the window a
            # copy could inherit session state from.
            copies.append(executor.model_copy())
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        with patch(
            "aiohttp.ClientSession.request", autospec=True, side_effect=_request
        ):
            await executor.get_hosts()

        assert len(copies) == 1
        assert copies[0].session is None

    @pytest.mark.asyncio
    async def test_an_entered_executor_keeps_the_session_it_was_given(self) -> None:
        """Assert a borrowed call does not close the lifecycle's own session.

        ``NomadLifecycle`` holds one entered executor for the whole process and
        retires it on its own terms; a call that closed it would take the open
        session out from under every route streaming logs off that instance.
        """
        executor = _build_executor(stub_nomad=False)
        async with executor:
            owned = executor.session
            assert owned is not None

            with self._answer(self.NODES) as sessions:
                await executor.get_hosts()

            assert sessions == [(owned, False)]
            assert executor.session is owned
            assert not owned.closed

    @pytest.mark.asyncio
    async def test_concurrent_callers_each_get_a_live_session(self) -> None:
        """Assert one caller cannot close the session another is still using.

        With no ``NOMAD`` override ``get_executor`` returns the YAML settings
        object itself - one instance shared process-wide - so concurrent
        request-less callers all arrive on the *same* executor. Each one's
        session has to be its own, or the first to finish closes a session the
        others are still serving from.
        """
        executor = _build_executor(stub_nomad=False)

        with self._answer(self.NODES) as sessions:
            results = await asyncio.gather(
                *(executor.get_hosts() for _ in range(self.CALLERS)),
                return_exceptions=True,
            )

        assert [r for r in results if isinstance(r, BaseException)] == []
        assert all(r == {"node-1": "10.0.0.1"} for r in results)
        assert len(sessions) == self.CALLERS
        assert not any(closed for _, closed in sessions)
        # One private session each, so no two calls can have shared one.
        assert len({id(session) for session, _ in sessions}) == self.CALLERS
        assert executor.session is None
