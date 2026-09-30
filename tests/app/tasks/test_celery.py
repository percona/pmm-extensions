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

"""Define tests for the app.tasks.celery module."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from nomad.api.exceptions import BaseNomadException
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import undefer
from sqlmodel import col, select, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool

from app.core.alerts.models import AlertService, AlertSeverity
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.encryption import is_encrypted
from app.core.exceptions import HTTPBadRequestException, HTTPConflictException
from app.core.utils import json_serializer, utc_now
from app.tasks import celery as celery_module
from app.tasks.celery import (
    _check_nomad_cert_expiry,
    _dispatch_chained_task,
    _dispatch_queue_item,
    _MAX_CHAIN_DEPTH,
    _purge_task_history_logs,
    _raise_if_identical_task_conflict,
    check_nomad_cert_expiry,
    delete_task_history,
    dispatch_queue_item,
    get_executor_for_task,
    maybe_dispatch_chain,
    prepare_periodic_task_history,
    purge_task_history_logs,
    sync_queue_item,
    sync_running_items,
    task_revoked_handler,
)
from app.tasks.crud import TaskHistoryLogManager, TaskHistoryManager, TaskManager
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.execution.models import BaseExecutor
from app.tasks.execution_request_secrets import (
    CONFIG_META_KEY,
    ENCRYPTED_META_KEYS,
    PAYLOAD_LEAF,
)
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    DispatchLock,
    Task,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryLog,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.db_schema import apply_schema
from tests.app.factories import TaskFactory
from tests.app.tasks.conftest import (
    overwrite_execution_request,
    stored_execution_request,
)

MODULE = "app.tasks.celery"
# Derived rather than spelled out: ``_chain_on_failure`` chains on any terminal
# status but SUCCESS, so a literal list silently stops covering the policy the
# moment a terminal status is added -- which is exactly how the last one landed
# with no test turning red.
NON_SUCCESS_TERMINAL_STATUSES = sorted(
    status
    for status in TaskHistoryStatusEnum
    if status.is_terminal() and status is not TaskHistoryStatusEnum.SUCCESS
)
EXPECTED_NOMAD_CERT_RESOLVE_CALLS = 2
# A plaintext meta value seeded alongside the encrypted ones, asserted on to
# show the `@>` predicate still carries the leaves that stayed queryable.
_PRIORITY_META_VALUE = 5
ANCHOR = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_task(**overrides):
    """Build a fake Task instance with sensible defaults."""
    defaults = {
        "id": 1,
        "name": "test-task",
        "data": {"Constraints": [{"RTarget": "node-1"}]},
        "backend": TaskBackendEnum.NOMAD,
        "owner": "ANY",
        "is_template": False,
        "protected": False,
        "alert_on_fail": False,
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_history(task=None, **overrides):
    """Build a fake TaskHistory instance with sensible defaults."""
    task = task or _make_task()
    defaults = {
        "id": 10,
        "task_id": task.id,
        "task": task,
        "status": TaskHistoryStatusEnum.PENDING,
        "execution_request": TaskExecutionRequest(
            task=task.name,
            target="node-1",
            meta={"key": "value"},
            payload=None,
        ),
        "executed_by": "test-user",
    }
    defaults.update(overrides)
    return TaskHistory(**defaults)


def _make_session_mock(bind_name: str = "sqlite"):
    """Build a mock AsyncSession with a bind that reports the given name."""
    session = AsyncMock()
    bind = MagicMock()
    bind.name = bind_name
    session.get_bind = MagicMock(return_value=bind)
    return session


def _make_lock_session_maker():
    """Build a mock async session maker that yields an async context manager."""
    lock_session = AsyncMock()
    lock_session_cm = AsyncMock()
    lock_session_cm.__aenter__ = AsyncMock(return_value=lock_session)
    lock_session_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=lock_session_cm)


def _make_chain_task(name: str) -> Task:
    """Build an in-memory Task for chain dispatch tests."""
    return TaskFactory.build(
        name=name,
        backend=TaskBackendEnum.NOMAD,
        data={"meta": {"target": "host1"}},
    )


def _make_chain_history(
    task: Task, status: TaskHistoryStatusEnum, meta: dict
) -> TaskHistory:
    """Build an in-memory TaskHistory for chain dispatch tests."""
    history = TaskHistory(
        task_id=task.id or 1,
        task=task,
        execution_request=TaskExecutionRequest(
            task=task.name,
            target="host1",
            meta=meta,
            payload=None,
            tracking={"evaluation_id": ""},
        ),
        status=status,
        executed_by="test-user",
        anonymize_mask=None,
    )
    history.id = 1
    return history


def _make_chain_session_mock() -> tuple[MagicMock, AsyncMock]:
    """Return (session_maker_mock, session_mock) for chain dispatch tests."""
    session_mock = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    session_maker = MagicMock()
    session_maker.return_value = session_mock
    return session_maker, session_mock


class TestGetExecutorForTask:
    """Test get_executor_for_task."""

    def test_nomad_backend_returns_executor(self):
        """Assert NOMAD backend returns an executor from get_executor."""
        task = _make_task(backend=TaskBackendEnum.NOMAD)
        mock_executor = MagicMock()
        with patch(
            "app.tasks.celery.get_executor", return_value=mock_executor
        ) as mock_get:
            result = get_executor_for_task(task)

        mock_get.assert_called_once_with(TaskBackendEnum.NOMAD)
        assert result is mock_executor

    def test_unsupported_backend_raises_bad_request(self):
        """Assert unsupported backend raises HTTPBadRequestException."""
        task = _make_task(backend=TaskBackendEnum.NOMAD)
        with (
            patch(
                "app.tasks.celery.get_executor",
                side_effect=ValueError("Unsupported"),
            ),
            pytest.raises(HTTPBadRequestException, match="Unsupported task backend"),
        ):
            get_executor_for_task(task)


class TestDispatchQueueItem:
    """Test dispatch_queue_item."""

    @pytest.mark.asyncio
    async def test_with_session_provided(self):
        """Assert provided session is passed to _dispatch_queue_item."""
        queue_item = _make_history()
        session = _make_session_mock()
        expected = _make_history(status=TaskHistoryStatusEnum.RUNNING)

        with patch(
            "app.tasks.celery._dispatch_queue_item",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_dispatch:
            result = await dispatch_queue_item(queue_item, session=session)

        mock_dispatch.assert_awaited_once_with(
            queue_item, session, await_annotations=False
        )
        assert result is expected

    @pytest.mark.asyncio
    async def test_without_session_creates_own(self):
        """Assert a new session is created when none is provided."""
        queue_item = _make_history()
        expected = _make_history(status=TaskHistoryStatusEnum.RUNNING)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery._dispatch_queue_item",
                new_callable=AsyncMock,
                return_value=expected,
            ) as mock_dispatch,
        ):
            result = await dispatch_queue_item(queue_item, session=None)

        mock_dispatch.assert_awaited_once()
        assert mock_dispatch.await_args.kwargs == {"await_annotations": False}
        assert result is expected

    @pytest.mark.asyncio
    async def test_await_annotations_flag_propagated_to_internal(self):
        """Assert ``await_annotations=True`` reaches ``_dispatch_queue_item``."""
        queue_item = _make_history()
        session = _make_session_mock()
        expected = _make_history(status=TaskHistoryStatusEnum.RUNNING)

        with patch(
            "app.tasks.celery._dispatch_queue_item",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_dispatch:
            await dispatch_queue_item(
                queue_item, session=session, await_annotations=True
            )

        mock_dispatch.assert_awaited_once_with(
            queue_item, session, await_annotations=True
        )


class TestInternalDispatchQueueItem:
    """Test _dispatch_queue_item."""

    @pytest.mark.asyncio
    async def test_non_pending_raises_conflict(self):
        """Assert non-PENDING status raises HTTPConflictException."""
        queue_item = _make_history(status=TaskHistoryStatusEnum.RUNNING)
        session = _make_session_mock()

        with pytest.raises(HTTPConflictException, match="not in a pending state"):
            await _dispatch_queue_item(queue_item, session)

    @pytest.mark.asyncio
    async def test_happy_path_dispatches_and_cleans_lock(self):
        """Assert happy path creates lock, dispatches, and cleans lock."""
        task = _make_task()
        queue_item = _make_history(task=task)
        session = _make_session_mock()
        mock_executor = AsyncMock()
        dispatched_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)
        mock_executor.dispatch_task.return_value = dispatched_item
        mock_lock = MagicMock(spec=DispatchLock)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                return_value=mock_lock,
            ),
            patch(
                "app.tasks.celery._raise_if_identical_task_conflict",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete",
                new_callable=AsyncMock,
            ) as mock_delete_lock,
        ):
            result = await _dispatch_queue_item(queue_item, session)

        assert result is dispatched_item
        mock_delete_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_integrity_error_on_lock_raises_conflict(self):
        """Assert IntegrityError on lock creation raises HTTPConflictException."""
        queue_item = _make_history()
        session = _make_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                side_effect=IntegrityError(
                    statement="INSERT", params={}, orig=Exception()
                ),
            ),
            pytest.raises(
                HTTPConflictException, match="Identical dispatch in progress"
            ),
        ):
            await _dispatch_queue_item(queue_item, session)

    @pytest.mark.asyncio
    async def test_executor_failure_still_cleans_lock(self):
        """Assert lock is cleaned up even when executor dispatch fails."""
        task = _make_task()
        queue_item = _make_history(task=task)
        session = _make_session_mock()
        mock_executor = AsyncMock()
        mock_executor.dispatch_task.side_effect = RuntimeError("dispatch failed")
        mock_lock = MagicMock(spec=DispatchLock)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                return_value=mock_lock,
            ),
            patch(
                "app.tasks.celery._raise_if_identical_task_conflict",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete",
                new_callable=AsyncMock,
            ) as mock_delete_lock,
            pytest.raises(RuntimeError, match="dispatch failed"),
        ):
            await _dispatch_queue_item(queue_item, session)

        mock_delete_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_identical_task_conflict_raises(self):
        """Assert identical running task conflict raises HTTPConflictException."""
        task = _make_task()
        queue_item = _make_history(task=task)
        session = _make_session_mock()
        mock_lock = MagicMock(spec=DispatchLock)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                return_value=mock_lock,
            ),
            patch(
                "app.tasks.celery._raise_if_identical_task_conflict",
                new_callable=AsyncMock,
                side_effect=HTTPConflictException(
                    "Identical queue item already running"
                ),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete",
                new_callable=AsyncMock,
            ) as mock_delete_lock,
            pytest.raises(
                HTTPConflictException, match="Identical queue item already running"
            ),
        ):
            await _dispatch_queue_item(queue_item, session)

        mock_delete_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_annotates_started_on_successful_dispatch(self):
        """Assert annotate_task_event is called with STARTED after dispatch."""
        task = _make_task()
        queue_item = _make_history(task=task)
        session = _make_session_mock()
        mock_executor = AsyncMock()
        dispatched_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)
        mock_executor.dispatch_task.return_value = dispatched_item
        mock_lock = MagicMock(spec=DispatchLock)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                return_value=mock_lock,
            ),
            patch(
                "app.tasks.celery._raise_if_identical_task_conflict",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.schedule_annotation",
            ) as mock_schedule,
        ):
            await _dispatch_queue_item(queue_item, session)

        mock_schedule.assert_called_once_with(dispatched_item, "STARTED")


class TestRaiseIfIdenticalTaskConflict:
    """Test _raise_if_identical_task_conflict.

    Behavioral coverage (conflict raises, self-exclusion, status/task scoping,
    jsonb type-strictness, injection-safety) lives in
    ``TestRaiseIfIdenticalTaskConflictRealPostgres``, which runs the query on a
    real engine. What remains here is the one dialect-branch shape assertion the
    end-to-end tests cannot observe: that SQLite still uses the ``json_extract``
    text-equality loop rather than the PostgreSQL ``@>``/jsonb path.
    """

    @staticmethod
    async def _capture_meta_clauses(queue_item, bind_name):
        """Run the dispatch dedup helper and return its compiled meta clauses.

        Patch ``TaskHistoryManager.list`` to capture every positional argument,
        drop the two leading (``task``/``target``) and two trailing
        (``status``/``id``) non-meta clauses, then render each remaining clause
        against the PostgreSQL dialect with literal binds so substring assertions
        can inspect the inlined values. ``payload`` is no longer among them: it
        is compared in Python, off the loaded candidate.
        """
        session = _make_session_mock(bind_name=bind_name)
        with patch(
            "app.tasks.celery.TaskHistoryManager.list",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_list:
            await _raise_if_identical_task_conflict(queue_item, session)
        positional_args = mock_list.await_args.args[1:]
        meta_clauses = positional_args[2:-2]
        rendered = [
            str(
                clause.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            for clause in meta_clauses
        ]
        return meta_clauses, rendered

    @pytest.mark.asyncio
    async def test_sqlite_keeps_per_key_text_equality_loop(self):
        """Assert SQLite mixed meta still uses ``json_extract`` text equality."""
        task = _make_task()
        queue_item = _make_history(
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"target": "node-1", "_chain_task_names": ["a"]},
                payload=None,
            ),
        )

        _, rendered = await self._capture_meta_clauses(queue_item, "sqlite")

        expected_clause_count = 2
        assert len(rendered) == expected_clause_count
        for clause in rendered:
            assert "json_extract" in clause.lower()
            assert "@>" not in clause

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bind_name", ["sqlite", "postgresql"])
    async def test_no_encrypted_meta_key_gets_a_sql_predicate(
        self, bind_name: str
    ) -> None:
        """Assert no encrypted ``meta`` key is ever compared in SQL.

        Encryption is non-deterministic, so a predicate over one of these keys
        would stop matching an identical resubmit. The keys come from the leaf
        inventory rather than being listed here, so a leaf added to it is covered
        without editing this test, and the plaintext sibling asserts the
        narrowing was dropped only for the encrypted ones.

        :param bind_name: The dialect the mocked session reports.
        """
        task = _make_task()
        secret = "hunter2"
        queue_item = _make_history(
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"target": "node-1"}
                | {key: f"--password {secret}" for key in ENCRYPTED_META_KEYS},
                payload=None,
            ),
        )

        _, rendered = await self._capture_meta_clauses(queue_item, bind_name)

        assert rendered, "the plaintext key must still be narrowed on"
        assert all(secret not in clause for clause in rendered)
        for key in ENCRYPTED_META_KEYS:
            assert all(f"'{key}'" not in clause for clause in rendered)


async def _create_pg_task(
    session: AsyncSession,
    *,
    name: str = "dedup-task",
) -> Task:
    """Persist a parent ``Task`` for real-PostgreSQL dedup tests."""
    task_data = TaskFactory.build(
        name=name,
        backend=TaskBackendEnum.NOMAD,
        data={"job": "test"},
    )
    return await TaskManager.create(session, TaskWrite.model_validate(task_data))


async def _seed_pg_history(
    session: AsyncSession,
    *,
    task_id: int,
    task_name: str,
    meta: dict | None,
    status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.PENDING,
    target: str = "node-1",
    payload: str | None = None,
) -> TaskHistory:
    """Persist a ``TaskHistory`` row as live DB state for the dedup query.

    The ``execution_request`` is a real ``TaskExecutionRequest`` because the
    guard no longer matches every leaf through SQL: the two encrypted leaves are
    compared in Python off the candidate row, so the seeded row has to expose the
    same attributes a production candidate does.
    """
    row = TaskHistory(
        task_id=task_id,
        status=status,
        execution_request=TaskExecutionRequest(
            task=task_name,
            target=target,
            meta=meta,
            payload=payload,
        ),
        executed_by="seed-user",
    )
    return await TaskHistoryManager.save(session, row)


def _pg_queue_item(
    task: Task,
    *,
    meta: dict | None,
    item_id: int,
    target: str = "node-1",
    payload: str | None = None,
) -> TaskHistory:
    """Build the in-memory candidate passed to the dedup helper.

    It is never persisted — table models skip validation, so the
    ``execution_request`` is a real ``TaskExecutionRequest`` to expose the
    ``.meta``/``.task``/``.target``/``.payload`` attributes the helper reads,
    and a concrete ``id`` drives the ``id != queue_item.id`` self-exclusion.
    """
    return TaskHistory(
        id=item_id,
        task_id=task.id,
        status=TaskHistoryStatusEnum.PENDING,
        execution_request=TaskExecutionRequest(
            task=task.name,
            target=target,
            meta=meta,
            payload=payload,
        ),
        executed_by="queue-user",
    )


# id guaranteed distinct from any serial-assigned seeded row, so a seeded
# duplicate is never excluded as "self".
_UNSEEDED_ITEM_ID = 999_999

# Every status the dedup guard must ignore, derived rather than listed so a
# new terminal status is covered without touching these tests.
_FINISHED_STATUSES = (
    frozenset(TaskHistoryStatusEnum) - TaskHistoryStatusEnum.active_statuses()
)


class TestIdenticalTaskConflictStatusScoping:
    """Exercise the dedup guard's status scoping on the default test engine.

    The guard filters on ``TaskHistoryStatusEnum.active_statuses()``, which is a
    plain ``IN`` predicate and therefore dialect-independent — so unlike the
    ``jsonb`` containment paths, this leg needs no real PostgreSQL and runs in a
    default ``make test``. It is what makes a wedged RUNNING row block every
    later dispatch of the same task, target and payload, and what lets a row
    that reached a terminal status stop blocking them.

    The seeding helpers below are dialect-agnostic despite their names.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("finished", sorted(_FINISHED_STATUSES))
    async def test_finished_duplicate_does_not_block_dispatch(
        self, session: AsyncSession, finished: TaskHistoryStatusEnum
    ) -> None:
        """Assert a duplicate row in a finished status never conflicts.

        :param session: The async session fixture the guard queries.
        :param finished: The terminal status seeded on the duplicate row.
        """
        task = await _create_pg_task(session)
        meta = {"target": "node-1"}
        await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            status=finished,
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active", sorted(TaskHistoryStatusEnum.active_statuses()))
    async def test_active_duplicate_still_blocks_dispatch(
        self, session: AsyncSession, active: TaskHistoryStatusEnum
    ) -> None:
        """Assert a duplicate row in an active status still conflicts.

        :param session: The async session fixture the guard queries.
        :param active: The active status seeded on the duplicate row.
        """
        task = await _create_pg_task(session)
        meta = {"target": "node-1"}
        await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            status=active,
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        # Matched including the "(<id>)." suffix, unlike this module's other
        # assertions on the same message, so that one test anchors the format.
        # om_inventory's dispatcher parses that id out to adopt the named item
        # instead of failing its host, and nothing else on this side pins it;
        # a reworded suffix would pass the whole Tasks suite and silently put
        # that app back on the failure path.
        with pytest.raises(
            HTTPConflictException,
            match=r"Identical queue item already running \(\d+\)\.",
        ):
            await _raise_if_identical_task_conflict(queue_item, session)


class TestIdenticalTaskConflictEncryptedLeaves:
    """Exercise the dedup guard over the leaves that are stored encrypted.

    ``meta["args"]``, ``meta["config"]`` and ``payload`` are ciphertext at rest
    and encryption is non-deterministic, so none of them can be compared in SQL
    any more. The guard narrows in SQL on the plaintext leaves and compares these
    in Python off the loaded candidate, which is dialect-independent and
    therefore runs on the default test engine.

    Every test here expunges the session before running the guard: without it the
    identity map hands back the in-memory row that was just seeded, and the
    decrypt-on-load the comparison depends on never happens.
    """

    @pytest.mark.asyncio
    async def test_identical_encrypted_request_still_conflicts(
        self, session: AsyncSession
    ) -> None:
        """Assert an identical resubmit conflicts while every leaf is ciphertext.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        meta = {
            "target": "node-1",
            "args": "restore --password hunter2",
            CONFIG_META_KEY: "master_password: hunter2\n",
        }
        seeded = await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            payload="secret document",
        )
        stored = await stored_execution_request(session, seeded.id)
        for key in ENCRYPTED_META_KEYS:
            assert is_encrypted(stored["meta"][key])
        assert is_encrypted(stored["payload"])
        session.expunge_all()
        queue_item = _pg_queue_item(
            task, meta=meta, item_id=_UNSEEDED_ITEM_ID, payload="secret document"
        )

        with pytest.raises(
            HTTPConflictException, match="Identical queue item already running"
        ):
            await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_identical_request_without_payload_conflicts(
        self, session: AsyncSession
    ) -> None:
        """Assert two requests carrying no payload still match on the other leaves.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        meta = {"target": "node-1", "args": "--dry-run"}
        await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta=meta, payload=None
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task, meta=meta, item_id=_UNSEEDED_ITEM_ID, payload=None
        )

        with pytest.raises(
            HTTPConflictException, match="Identical queue item already running"
        ):
            await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_differing_args_does_not_conflict(
        self, session: AsyncSession
    ) -> None:
        """Assert a different command line passes even though both are ciphertext.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1", "args": "--fast"},
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task,
            meta={"target": "node-1", "args": "--slow"},
            item_id=_UNSEEDED_ITEM_ID,
        )

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_differing_config_does_not_conflict(
        self, session: AsyncSession
    ) -> None:
        """Assert two runs of one task differing only in config are not duplicates.

        ``config`` joined the encrypted leaves after ``args`` did, and its SQL
        predicate went with it. Nothing else distinguishes these two rows, so
        without the Python comparison covering the key as well, the second
        submission would be refused as a duplicate of the first.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1", CONFIG_META_KEY: "master_host: db-1\n"},
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task,
            meta={"target": "node-1", CONFIG_META_KEY: "master_host: db-2\n"},
            item_id=_UNSEEDED_ITEM_ID,
        )

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_queued_without_args_does_not_match_a_candidate_with_them(
        self, session: AsyncSession
    ) -> None:
        """Pin the narrowing: ``args`` is compared by equality, not containment.

        The SQL stage matches a ``meta`` key by containment, which ignores a key
        the incoming request does not carry, so before ``args`` moved into the
        Python stage this pair deduplicated. Equality is the more accurate
        reading, since a request with no arguments is not the same request as
        one with them, but the change is silent, so it is pinned here.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1", "args": "--fast"},
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task, meta={"target": "node-1"}, item_id=_UNSEEDED_ITEM_ID
        )

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_candidate_without_args_does_not_conflict(
        self, session: AsyncSession
    ) -> None:
        """Assert a stored row carrying no ``args`` never matches one that does.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta={"target": "node-1"}
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task,
            meta={"target": "node-1", "args": "--fast"},
            item_id=_UNSEEDED_ITEM_ID,
        )

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_args_only_meta_still_narrows_and_compares(
        self, session: AsyncSession
    ) -> None:
        """Assert a candidate whose meta is only ``args`` produces no meta predicate.

        The SQL narrows on task, target, status and ``task_id`` alone; the Python
        comparison is what still separates the two rows.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta={"args": "--fast"}
        )
        session.expunge_all()

        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"args": "--slow"}, item_id=_UNSEEDED_ITEM_ID),
            session,
        )
        with pytest.raises(
            HTTPConflictException, match="Identical queue item already running"
        ):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(
                    task, meta={"args": "--fast"}, item_id=_UNSEEDED_ITEM_ID
                ),
                session,
            )

    @pytest.mark.asyncio
    async def test_a_malformed_candidate_is_skipped_not_raised_on(
        self, session: AsyncSession
    ) -> None:
        """Assert a stored row that does not parse cannot break the dispatch.

        ``process_result_value`` hands back the raw document when it will not
        validate, so the Python comparison meets something with no ``payload``
        attribute. The row is malformed in ``tracking``, which the SQL narrowing
        never reads, so ``task``, ``target`` and the compared ``meta`` key all
        still match and the candidate reaches the comparison.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        seeded = await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta={"target": "node-1"}
        )
        await overwrite_execution_request(
            session,
            seeded.id,
            {
                "task": task.name,
                "target": "node-1",
                "meta": {"target": "node-1"},
                "tracking": "not-a-mapping",
            },
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task, meta={"target": "node-1"}, item_id=_UNSEEDED_ITEM_ID
        )

        await _raise_if_identical_task_conflict(queue_item, session)

    @pytest.mark.asyncio
    async def test_an_unreadable_candidate_is_refused_not_ignored(
        self, session: AsyncSession
    ) -> None:
        """Assert a candidate this key cannot read blocks rather than falls through.

        Its leaves stay ciphertext, so every plaintext comparison against them
        fails and the row would silently stop deduplicating. The queued side is
        always readable at this point, because dispatch is gated on that first,
        so nothing can resolve the mismatch by comparing.

        :param session: The async session fixture the guard queries.
        """
        task = await _create_pg_task(session)
        token = Fernet(Fernet.generate_key()).encrypt(b'"--fast"').decode("ascii")
        seeded = await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta={"target": "node-1"}
        )
        await overwrite_execution_request(
            session,
            seeded.id,
            {
                "task": task.name,
                "target": "node-1",
                "meta": {"target": "node-1", "args": token},
                "payload": None,
            },
        )
        session.expunge_all()
        queue_item = _pg_queue_item(
            task,
            meta={"target": "node-1", "args": "--fast"},
            item_id=_UNSEEDED_ITEM_ID,
        )

        with pytest.raises(HTTPConflictException, match="cannot be compared"):
            await _raise_if_identical_task_conflict(queue_item, session)


class TestRaiseIfIdenticalTaskConflictRealPostgres:
    """Exercise _raise_if_identical_task_conflict end-to-end on a real PostgreSQL engine.

    Each test seeds live ``TaskHistory`` rows and runs the dedup query through an
    asyncpg-backed session (``session.get_bind().name == "postgresql"``), so the
    ``jsonb`` containment (``@>``) and per-key equality paths are proven by
    outcome rather than by compiling SQL strings.
    """

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_scalar_meta_conflict_raises(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a duplicate PENDING row with scalar meta (``@>`` path) raises conflict."""
        task = await _create_pg_task(postgres_session)
        meta = {"target": "node-1", "priority": 5}
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta=meta
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        with pytest.raises(
            HTTPConflictException, match="Identical queue item already running"
        ):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_scalar_meta_only_self_passes(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert the candidate's own row is excluded by ``id != queue_item.id``."""
        task = await _create_pg_task(postgres_session)
        meta = {"target": "node-1", "priority": 5}
        row = await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta=meta
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=row.id)

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_container_meta_conflict_raises(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a duplicate with list meta (per-key jsonb equality) raises conflict."""
        task = await _create_pg_task(postgres_session)
        meta = {"_chain_task_names": ["a", "b"]}
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta=meta
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_container_meta_is_exact_match_not_subset(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert list meta uses exact equality: a differing element does not dedup."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta={"_chain_task_names": ["a", "c"]},
        )
        queue_item = _pg_queue_item(
            task, meta={"_chain_task_names": ["a", "b"]}, item_id=_UNSEEDED_ITEM_ID
        )

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_scalar_containment_is_superset_tolerant(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert ``@>`` matches when the stored meta is a superset of the candidate's."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1", "priority": 5, "extra": "x"},
        )
        queue_item = _pg_queue_item(
            task, meta={"target": "node-1", "priority": 5}, item_id=_UNSEEDED_ITEM_ID
        )

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_finished_status_is_not_deduped(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert duplicates in a finished status (not PENDING/RUNNING) never conflict."""
        task = await _create_pg_task(postgres_session)
        meta = {"target": "node-1"}
        for finished in (
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.LOST,
            TaskHistoryStatusEnum.STOPPED,
        ):
            await _seed_pg_history(
                postgres_session,
                task_id=task.id,
                task_name=task.name,
                meta=meta,
                status=finished,
            )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_running_status_is_deduped(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a duplicate in RUNNING (an active status) still conflicts."""
        task = await _create_pg_task(postgres_session)
        meta = {"target": "node-1"}
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            status=TaskHistoryStatusEnum.RUNNING,
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_different_task_id_is_not_deduped(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert dedup is per ``task_id``: an identical request under another task passes."""
        task = await _create_pg_task(postgres_session, name="task-a")
        other = await _create_pg_task(postgres_session, name="task-b")
        meta = {"target": "node-1"}
        # Same execution_request.task string, but the row belongs to `other`.
        await _seed_pg_history(
            postgres_session, task_id=other.id, task_name=task.name, meta=meta
        )
        queue_item = _pg_queue_item(task, meta=meta, item_id=_UNSEEDED_ITEM_ID)

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_different_target_is_not_deduped(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a differing execution target passes (no conflict)."""
        task = await _create_pg_task(postgres_session)
        meta = {"priority": 5}
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            target="node-2",
        )
        queue_item = _pg_queue_item(
            task, meta=meta, item_id=_UNSEEDED_ITEM_ID, target="node-1"
        )

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_different_payload_is_not_deduped(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a differing payload passes (no conflict)."""
        task = await _create_pg_task(postgres_session)
        meta = {"priority": 5}
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            payload="echo one",
        )
        queue_item = _pg_queue_item(
            task, meta=meta, item_id=_UNSEEDED_ITEM_ID, payload="echo two"
        )

        await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_bool_meta_is_type_strict(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert jsonb ``true`` dedups against ``True`` only — not ``False`` or ``"true"``."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta={"flag": True}
        )

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(task, meta={"flag": True}, item_id=_UNSEEDED_ITEM_ID),
                postgres_session,
            )
        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"flag": False}, item_id=_UNSEEDED_ITEM_ID),
            postgres_session,
        )
        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"flag": "true"}, item_id=_UNSEEDED_ITEM_ID),
            postgres_session,
        )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_int_vs_string_meta_is_type_strict(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert jsonb ``1`` dedups against int ``1`` only — not the string ``"1"``."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta={"key": 1}
        )

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(task, meta={"key": 1}, item_id=_UNSEEDED_ITEM_ID),
                postgres_session,
            )
        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"key": "1"}, item_id=_UNSEEDED_ITEM_ID),
            postgres_session,
        )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_null_meta_is_type_strict(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert jsonb ``null`` dedups against ``None`` only — not the string ``"None"``."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta={"key": None}
        )

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(task, meta={"key": None}, item_id=_UNSEEDED_ITEM_ID),
                postgres_session,
            )
        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"key": "None"}, item_id=_UNSEEDED_ITEM_ID),
            postgres_session,
        )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_empty_meta_does_not_over_constrain(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert empty candidate meta dedups on task/target/payload only."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta={"x": 1}
        )
        queue_item = _pg_queue_item(task, meta={}, item_id=_UNSEEDED_ITEM_ID)

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_none_meta_does_not_over_constrain(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert ``meta=None`` behaves like empty meta: dedup on task/target/payload."""
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session, task_id=task.id, task_name=task.name, meta={"x": 1}
        )
        queue_item = _pg_queue_item(task, meta=None, item_id=_UNSEEDED_ITEM_ID)

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_meta_value_special_characters_match_exactly(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert quote/SQL-metacharacter meta values are parameterized: exact match only."""
        malicious = "a\"b'; DROP TABLE taskhistory;--"
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta={"note": malicious},
        )
        # An exact twin still deduplicates (value round-trips through jsonb safely).
        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(
                    task, meta={"note": malicious}, item_id=_UNSEEDED_ITEM_ID
                ),
                postgres_session,
            )
        # A benign value does not match — no injection widened the comparison.
        await _raise_if_identical_task_conflict(
            _pg_queue_item(task, meta={"note": "safe"}, item_id=_UNSEEDED_ITEM_ID),
            postgres_session,
        )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_container_key_special_characters_match_exactly(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert a container meta key with quotes is safely inlined and matches exactly."""
        weird_key = "we'ird\"key"
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta={weird_key: ["x"]},
        )

        with pytest.raises(HTTPConflictException):
            await _raise_if_identical_task_conflict(
                _pg_queue_item(
                    task, meta={weird_key: ["x"]}, item_id=_UNSEEDED_ITEM_ID
                ),
                postgres_session,
            )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_identical_encrypted_request_still_conflicts(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert an identical resubmit conflicts on ``jsonb`` with every leaf encrypted.

        The dialect-independent half of this runs on the default engine; this arm
        proves the narrowed ``@>`` predicate still selects the candidate once the
        encrypted keys are excluded from it.

        :param postgres_session: The real-PostgreSQL session the guard queries.
        """
        task = await _create_pg_task(postgres_session)
        meta = {
            "target": "node-1",
            "priority": 5,
            "args": "restore --password hunter2",
            CONFIG_META_KEY: "master_password: hunter2\n",
        }
        seeded = await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta=meta,
            payload="secret document",
        )
        stored = await stored_execution_request(postgres_session, seeded.id)
        for key in ENCRYPTED_META_KEYS:
            assert is_encrypted(stored["meta"][key])
        assert is_encrypted(stored["payload"])
        assert stored["meta"]["priority"] == _PRIORITY_META_VALUE
        postgres_session.expunge_all()
        queue_item = _pg_queue_item(
            task, meta=meta, item_id=_UNSEEDED_ITEM_ID, payload="secret document"
        )

        with pytest.raises(
            HTTPConflictException, match="Identical queue item already running"
        ):
            await _raise_if_identical_task_conflict(queue_item, postgres_session)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ENCRYPTED_META_KEYS)
    async def test_a_differing_encrypted_meta_key_does_not_conflict(
        self, postgres_session: AsyncSession, key: str
    ) -> None:
        """Assert two rows differing only in one encrypted key pass on ``jsonb`` too.

        Each key is exercised because losing its SQL predicate is what makes the
        Python comparison the only thing separating the rows, and the keys come
        from the inventory so a leaf added to it is covered here as well.

        :param postgres_session: The real-PostgreSQL session the guard queries.
        :param key: The encrypted ``meta`` key the two requests disagree on.
        """
        task = await _create_pg_task(postgres_session)
        await _seed_pg_history(
            postgres_session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1", key: "--fast"},
        )
        postgres_session.expunge_all()
        queue_item = _pg_queue_item(
            task,
            meta={"target": "node-1", key: "--slow"},
            item_id=_UNSEEDED_ITEM_ID,
        )

        await _raise_if_identical_task_conflict(queue_item, postgres_session)


class TestDeleteTaskHistory:
    """Test delete_task_history."""

    @pytest.mark.asyncio
    async def test_calls_delete_where_with_queue_id(self):
        """Assert TaskHistoryManager.delete_where is called with correct ID."""
        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=mock_session_maker,
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.delete_where",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            await delete_task_history(42)

        mock_delete.assert_awaited_once_with(mock_session, id=42)


class TestPreparePeriodicTaskHistory:
    """Test prepare_periodic_task_history."""

    @pytest.mark.asyncio
    async def test_with_execution_data(self):
        """Assert execution_data is validated via PeriodicTaskExecuteRequest."""
        task = _make_task()
        expected_history = _make_history(task=task)

        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        exec_data = {"meta": {"target": "node-1"}}

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=mock_session_maker,
            ),
            patch(
                "app.tasks.celery.get_executable_task_by_name",
                new_callable=AsyncMock,
                return_value=task,
            ) as mock_get_task,
            patch(
                "app.tasks.celery.prepare_task_history",
                return_value=expected_history,
            ) as mock_prepare,
        ):
            result = await prepare_periodic_task_history("test-task", exec_data)

        mock_get_task.assert_awaited_once_with(mock_session, "test-task")
        mock_prepare.assert_called_once()
        assert result is expected_history
        call_kwargs = mock_prepare.call_args
        assert call_kwargs[0][0] is task
        assert call_kwargs[1]["executed_by"] == "SYSTEM"
        assert call_kwargs[1]["execution_data"] is not None

    @pytest.mark.asyncio
    async def test_without_execution_data(self):
        """Assert None execution_data passes None to prepare_task_history."""
        task = _make_task()
        expected_history = _make_history(task=task)

        mock_session = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=mock_session_maker,
            ),
            patch(
                "app.tasks.celery.get_executable_task_by_name",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.prepare_task_history",
                return_value=expected_history,
            ) as mock_prepare,
        ):
            result = await prepare_periodic_task_history("test-task", None)

        assert result is expected_history
        call_kwargs = mock_prepare.call_args
        assert call_kwargs[1]["execution_data"] is None


class TestSyncRunningItems:
    """Test sync_running_items."""

    @pytest.mark.asyncio
    async def test_dispatches_sync_for_running_tasks(self):
        """Assert sync tasks are dispatched in chunks for running items."""
        mock_chunks = MagicMock()
        mock_chunks.apply_async = MagicMock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.update_where",
                new_callable=AsyncMock,
                return_value=[1, 2, 3],
            ),
            patch("app.tasks.celery.sync_task_history") as mock_sync_task,
        ):
            mock_sync_task.chunks.return_value = mock_chunks
            await sync_running_items()

        mock_sync_task.chunks.assert_called_once_with([(1,), (2,), (3,)], 100)
        mock_chunks.apply_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_running_tasks_skips_dispatch(self):
        """Assert no dispatch happens when no running tasks found."""
        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.update_where",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("app.tasks.celery.sync_task_history") as mock_sync_task,
        ):
            await sync_running_items()

        mock_sync_task.chunks.assert_not_called()


@asynccontextmanager
async def _seed_purge_db(num_aged: int, *, chunks_each: int = 1):
    """Build an in-memory tasks DB with ``num_aged`` aged finished histories.

    Each history is SUCCESS, finished 100 days ago, and carries ``chunks_each``
    log rows. Yields the session maker so the helper-under-test can be patched
    onto it, then disposes the engine on exit so its aiosqlite thread cannot
    outlive the test and block interpreter shutdown.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    maker = get_async_session_maker_from_engine(engine)
    old = utc_now() - timedelta(days=100)
    async with maker() as session:
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="aged-task", backend=TaskBackendEnum.NOMAD)
            ),
        )
        for _ in range(num_aged):
            history = await TaskHistoryManager.save(
                session,
                TaskHistory(
                    task_id=task.id,
                    status=TaskHistoryStatusEnum.SUCCESS,
                    created_at=old,
                    finished_at=old,
                    execution_request={
                        "task": task.name,
                        "target": "localhost",
                        "meta": {},
                        "tracking": {"allocation_id": None, "evaluation_id": None},
                    },
                ),
            )
            for offset in range(chunks_each):
                session.add(
                    TaskHistoryLog(
                        task_history_id=history.id,
                        source="run-python",
                        stream=TaskLogType.STDOUT,
                        start_offset=offset * 10,
                        end_offset=offset * 10 + 10,
                        content="x" * 10,
                    )
                )
        await session.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


def _purge_settings(retention_days: int = 90, batch_size: int = 10):
    """Return a stand-in settings object exposing the purge knobs."""
    return SimpleNamespace(
        LOG_RETENTION_DAYS=retention_days, LOG_PURGE_BATCH_SIZE=batch_size
    )


class TestPurgeTaskHistoryLogs:
    """Test the task-history-log purge helper and Celery wrapper."""

    @pytest.mark.asyncio
    async def test_purges_all_aged_logs_across_batches(self):
        """Loop batches until every aged log row is gone; audit rows survive."""
        async with _seed_purge_db(1, chunks_each=5) as maker:
            with (
                patch(f"{MODULE}.get_async_session_maker", return_value=maker),
                patch(f"{MODULE}.tasks_settings", _purge_settings(batch_size=2)),
            ):
                await _purge_task_history_logs()

            async with maker() as session:
                logs = await session.exec(select(col(TaskHistoryLog.id)))
                histories = await session.exec(select(col(TaskHistory.id)))
            assert len(logs.all()) == 0
            assert len(histories.all()) == 1

    @pytest.mark.asyncio
    async def test_no_aged_rows_is_noop(self):
        """A clean table deletes nothing and raises no error."""
        async with _seed_purge_db(0) as maker:
            with (
                patch(f"{MODULE}.get_async_session_maker", return_value=maker),
                patch(f"{MODULE}.tasks_settings", _purge_settings()),
            ):
                await _purge_task_history_logs()  # must not raise

    @pytest.mark.asyncio
    async def test_error_triggers_alert_and_reraises(self):
        """A delete failure fires a system alert and propagates the exception."""
        async with _seed_purge_db(1) as maker:
            boom = RuntimeError("db exploded")
            with (
                patch(f"{MODULE}.get_async_session_maker", return_value=maker),
                patch(f"{MODULE}.tasks_settings", _purge_settings()),
                patch(
                    f"{MODULE}.TaskHistoryLogManager.delete_aged_batch",
                    new_callable=AsyncMock,
                    side_effect=boom,
                ),
                patch(
                    f"{MODULE}.alert_service.trigger", new_callable=AsyncMock
                ) as mock_alert,
                pytest.raises(RuntimeError),
            ):
                await _purge_task_history_logs()

            mock_alert.assert_awaited_once()
            assert mock_alert.await_args is not None
            alert = mock_alert.await_args[0][0]
            assert alert["severity"] == AlertSeverity.ERROR
            assert alert["dedup_key"] == "purge_task_history_logs"

    def test_wrapper_runs_helper_on_loop(self):
        """The Celery wrapper drives the async helper via the celery loop."""
        with patch(f"{MODULE}.celery") as mock_celery:
            mock_celery.loop.run_until_complete = MagicMock(
                side_effect=lambda coro: coro.close()
            )
            purge_task_history_logs()
        mock_celery.loop.run_until_complete.assert_called_once()


class TestTaskRevokedHandler:
    """Test task_revoked_handler."""

    def test_execute_task_queue_expired_deletes_history(self):
        """Assert expired execute_task_queue calls delete_task_history."""
        request = MagicMock()
        request.args = [42]
        request.kwargs = {}
        request.get.return_value = "app.tasks.celery.execute_task_queue"

        with patch("app.tasks.celery.celery") as mock_celery:
            mock_celery.loop.run_until_complete = MagicMock()
            task_revoked_handler(
                sender=None, request=request, expired=True, signum=None
            )

        mock_celery.loop.run_until_complete.assert_called_once()

    def test_not_execute_task_queue_is_noop(self):
        """Assert non-execute_task_queue task does not call delete."""
        request = MagicMock()
        request.args = [42]
        request.kwargs = {}
        request.get.return_value = "app.tasks.celery.some_other_task"

        with patch("app.tasks.celery.celery") as mock_celery:
            mock_celery.loop.run_until_complete = MagicMock()
            task_revoked_handler(
                sender=None, request=request, expired=True, signum=None
            )

        mock_celery.loop.run_until_complete.assert_not_called()

    def test_not_expired_is_noop(self):
        """Assert non-expired revocation does not call delete."""
        request = MagicMock()
        request.args = [42]
        request.kwargs = {}
        request.get.return_value = "app.tasks.celery.execute_task_queue"

        with patch("app.tasks.celery.celery") as mock_celery:
            mock_celery.loop.run_until_complete = MagicMock()
            task_revoked_handler(
                sender=None, request=request, expired=False, signum=None
            )

        mock_celery.loop.run_until_complete.assert_not_called()

    def test_queue_id_from_kwargs(self):
        """Assert queue_id is extracted from kwargs when args is empty."""
        request = MagicMock()
        request.args = []
        request.kwargs = {"queue_id": 99}
        request.get.return_value = "app.tasks.celery.execute_task_queue"

        with patch("app.tasks.celery.celery") as mock_celery:
            mock_celery.loop.run_until_complete = MagicMock()
            task_revoked_handler(
                sender=None, request=request, expired=True, signum=None
            )

        mock_celery.loop.run_until_complete.assert_called_once()


class TestExecuteTaskQueue:
    """Test execute_task_queue Celery task."""

    def test_executes_and_returns_encoded_result(self):
        """Assert execute_task_queue calls get_task_history and dispatch."""
        task_obj = _make_task()
        queue_item = _make_history(task=task_obj)
        dispatched = _make_history(task=task_obj, status=TaskHistoryStatusEnum.RUNNING)

        call_order = []

        async def mock_get_task_history(qid):
            call_order.append(("get_task_history", qid))
            return queue_item

        async def mock_dispatch(item, *, await_annotations=False):
            call_order.append(("dispatch_queue_item", item.id, await_annotations))
            return dispatched

        test_loop = asyncio.new_event_loop()

        with (
            patch.object(
                celery_module,
                "get_task_history",
                side_effect=mock_get_task_history,
            ),
            patch.object(
                celery_module,
                "dispatch_queue_item",
                side_effect=mock_dispatch,
            ),
            patch.object(
                celery_module.celery,
                "loop",
                test_loop,
            ),
        ):
            result = celery_module.execute_task_queue.__wrapped__(10)

        test_loop.close()

        assert isinstance(result, dict)
        assert ("get_task_history", 10) in call_order
        assert ("dispatch_queue_item", queue_item.id, True) in call_order

    def test_unresolvable_payload_fails_terminally_without_dispatch(self, mocker):
        """Assert an ad-hoc dispatch with an unresolvable payload fails FAILED, never dispatching."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="adhoc-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=True,
                    data={"task": "wrapped"},
                )
            )
            history = test_loop.run_until_complete(
                _seed_history(
                    async_session_maker,
                    task,
                    payload="file:///nonexistent/x_payload",
                )
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery._dispatch_queue_item",
                new_callable=AsyncMock,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            with patch.object(celery_module.celery, "loop", test_loop):
                result = celery_module.execute_task_queue.__wrapped__(history.id)

            mock_dispatch.assert_not_awaited()
            mock_alert.assert_awaited_once()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            assert rows[0].id == history.id
            assert rows[0].status == TaskHistoryStatusEnum.FAILED
            assert result["status"] == TaskHistoryStatusEnum.FAILED.value


class TestSyncQueueItem:
    """Test sync_queue_item."""

    @pytest.mark.asyncio
    async def test_non_running_clears_sync_lock_via_update_where(self):
        """Assert non-running sync clears the lock via update_where, not ORM save."""
        task = _make_task()
        queue_item = _make_history(task=task, status=TaskHistoryStatusEnum.PENDING)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=queue_item,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.update_where",
                new_callable=AsyncMock,
                return_value=MagicMock(rowcount=1),
            ) as mock_update_where,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await sync_queue_item(queue_item.id)

        mock_update_where.assert_awaited_once()
        call_kwargs = mock_update_where.call_args
        assert call_kwargs[0][1] == {"sync_in_progress_started_at": None}
        mock_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_running_races_to_running_proceeds_with_sync(self):
        """Assert sync proceeds if task transitions to RUNNING between read and update."""
        task = _make_task()
        pending_item = _make_history(task=task, status=TaskHistoryStatusEnum.PENDING)
        running_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)
        running_item.id = pending_item.id
        saved_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)

        mock_executor = MagicMock()
        mock_executor.sync_task_history = AsyncMock(return_value=running_item)

        get_or_404_calls = [pending_item, running_item]

        async def get_or_404_side_effect(*args, **kwargs):
            return get_or_404_calls.pop(0)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                side_effect=get_or_404_side_effect,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.update_where",
                new_callable=AsyncMock,
                return_value=MagicMock(rowcount=0),
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=saved_item,
            ) as mock_save,
        ):
            result = await sync_queue_item(pending_item.id)

        mock_executor.sync_task_history.assert_awaited_once()
        assert mock_executor.sync_task_history.await_args is not None
        called_args, called_kwargs = mock_executor.sync_task_history.await_args
        assert called_args == (running_item,)
        assert "writer_session" in called_kwargs
        mock_save.assert_awaited_once()
        assert result is saved_item

    @pytest.mark.asyncio
    async def test_running_saves_via_orm_with_flag_modified(self):
        """Assert running sync saves via ORM save with flag_modified_fields."""
        task = _make_task()
        queue_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)
        saved_item = _make_history(task=task, status=TaskHistoryStatusEnum.RUNNING)

        mock_executor = MagicMock()
        mock_executor.sync_task_history = AsyncMock(return_value=queue_item)

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_make_lock_session_maker(),
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=queue_item,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=mock_executor,
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=saved_item,
            ) as mock_save,
        ):
            result = await sync_queue_item(queue_item.id)

        mock_save.assert_awaited_once()
        call_kwargs = mock_save.call_args
        assert set(call_kwargs.kwargs.get("flag_modified_fields")) == {
            "execution_request",
            "status",
            "started_at",
            "finished_at",
            "failure_reason",
            "sync_in_progress_started_at",
        }
        assert result is saved_item


class TestDispatchChainedTask:
    """Test _dispatch_chained_task."""

    @pytest.mark.asyncio
    async def test_dispatches_on_success(self) -> None:
        """Assert _dispatch_chained_task dispatches the chained task when found."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")
        parent_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {"_chain_task_names": [chain_task.name]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskManager.first", new_callable=AsyncMock
            ) as mock_task_first,
            patch(
                "app.tasks.celery.dispatch_queue_item", new_callable=AsyncMock
            ) as mock_dispatch,
        ):
            mock_task_first.return_value = chain_task
            mock_dispatch.return_value = AsyncMock()

            await _dispatch_chained_task(
                chain_task.name, parent_history, await_annotations=True
            )

        mock_dispatch.assert_awaited_once()
        dispatched_history = mock_dispatch.call_args[0][0]
        assert dispatched_history.execution_request.target == "host1"
        assert dispatched_history.execution_request.meta.get("_chain_depth") == 1
        assert mock_dispatch.await_args.kwargs.get("await_annotations") is True

    def test_unresolvable_payload_fails_terminally(self, mocker) -> None:
        """Assert a chained task with an unresolvable payload persists FAILED, never dispatching."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            chain_task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="chain-task",
                    backend=TaskBackendEnum.PROXY,
                    data={
                        "task": "wrapped",
                        "payload": "file:///nonexistent/x_payload",
                    },
                )
            )
            parent_history = _make_chain_history(
                _make_chain_task("main-task"),
                TaskHistoryStatusEnum.SUCCESS,
                {"_chain_task_names": ["chain-task"]},
            )
            mock_internal = mocker.patch(
                "app.tasks.celery._dispatch_queue_item", new_callable=AsyncMock
            )

            test_loop.run_until_complete(
                _dispatch_chained_task("chain-task", parent_history)
            )

            mock_internal.assert_not_awaited()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, chain_task.id)
            )
            assert len(rows) == 1
            assert rows[0].status == TaskHistoryStatusEnum.FAILED

    @pytest.mark.asyncio
    async def test_unknown_task_logs_warning(self) -> None:
        """Assert _dispatch_chained_task logs a warning when task name is not found."""
        main_task = _make_chain_task("main-task")
        parent_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {"_chain_task_names": ["unknown-task"]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskManager.first", new_callable=AsyncMock
            ) as mock_task_first,
            patch(
                "app.tasks.celery.dispatch_queue_item", new_callable=AsyncMock
            ) as mock_dispatch,
            patch("app.tasks.celery.logger") as mock_logger,
        ):
            mock_task_first.return_value = None

            await _dispatch_chained_task("unknown-task", parent_history)

        mock_dispatch.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        assert "unknown-task" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_self_chain_skipped(self) -> None:
        """Assert _dispatch_chained_task skips dispatch when chain target is the parent task."""
        main_task = _make_chain_task("main-task")
        parent_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {"_chain_task_names": ["main-task"]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskManager.first", new_callable=AsyncMock
            ) as mock_task_first,
            patch(
                "app.tasks.celery.dispatch_queue_item", new_callable=AsyncMock
            ) as mock_dispatch,
            patch("app.tasks.celery.logger") as mock_logger,
        ):
            mock_task_first.return_value = main_task

            await _dispatch_chained_task("main-task", parent_history)

        mock_dispatch.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        assert "same as the parent" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_propagates_chain_on_failure_flag(self) -> None:
        """Assert _dispatch_chained_task propagates chain_on_failure in dispatched meta."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")
        parent_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {"_chain_task_names": [chain_task.name], "_chain_on_failure": True},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskManager.first", new_callable=AsyncMock
            ) as mock_task_first,
            patch(
                "app.tasks.celery.dispatch_queue_item", new_callable=AsyncMock
            ) as mock_dispatch,
        ):
            mock_task_first.return_value = chain_task
            mock_dispatch.return_value = AsyncMock()

            await _dispatch_chained_task(chain_task.name, parent_history)

        mock_dispatch.assert_awaited_once()
        dispatched_history = mock_dispatch.call_args[0][0]
        assert (
            dispatched_history.execution_request.meta.get("_chain_on_failure") is True
        )

    @pytest.mark.asyncio
    async def test_max_depth_exceeded_no_dispatch(self) -> None:
        """Assert _dispatch_chained_task does not dispatch when chain depth limit is reached."""
        main_task = _make_chain_task("main-task")
        meta = {
            "_chain_task_names": ["chain-task"],
            "_chain_depth": _MAX_CHAIN_DEPTH,
        }
        parent_history = _make_chain_history(
            main_task, TaskHistoryStatusEnum.SUCCESS, meta
        )

        with (
            patch("app.tasks.celery.get_async_session_maker") as mock_session_maker,
            patch(
                "app.tasks.celery.dispatch_queue_item", new_callable=AsyncMock
            ) as mock_dispatch,
        ):
            await _dispatch_chained_task("chain-task", parent_history)

        mock_session_maker.assert_not_called()
        mock_dispatch.assert_not_awaited()


class TestSyncQueueItemChainDispatch:
    """Test chain dispatch behavior in sync_queue_item."""

    @pytest.mark.asyncio
    async def test_save_flags_scalars_after_sync_task_history(self) -> None:
        """Assert save receives flag_modified_fields for executor-updated scalars.

        Cover the path where the load session closes before ``sync_task_history``
        mutates the in-memory ``TaskHistory``. Flag those columns on save so merge
        persists them; omitting them from ``flag_modified_fields`` skips the UPDATE,
        leaves RUNNING in the database, and triggers 409 conflicts on redispatch.
        """
        main_task = _make_chain_task("mum-task")
        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {},
        )
        running_history.sync_in_progress_started_at = datetime(
            2026, 4, 1, 10, 0, 0, tzinfo=UTC
        )

        session_maker, _ = _make_chain_session_mock()
        mock_save = AsyncMock()

        async def sync_mutates_in_place(
            queue_item: TaskHistory,
            writer_session=None,
            *,
            await_annotations: bool = False,
        ) -> TaskHistory:
            del writer_session, await_annotations
            queue_item.status = TaskHistoryStatusEnum.FAILED
            queue_item.started_at = datetime(2026, 4, 1, 10, 1, 0, tzinfo=UTC)
            queue_item.finished_at = datetime(2026, 4, 1, 10, 2, 0, tzinfo=UTC)
            return queue_item

        async def save_returns_item(session, queue_item, **kwargs):
            return queue_item

        mock_save.side_effect = save_returns_item

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_get_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new=mock_save,
            ),
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(side_effect=sync_mutates_in_place)
            mock_get_executor.return_value = executor

            result = await sync_queue_item(1)

        mock_save.assert_awaited_once()
        call_kw = mock_save.await_args.kwargs
        assert set(call_kw["flag_modified_fields"]) == {
            "execution_request",
            "status",
            "started_at",
            "finished_at",
            "failure_reason",
            "sync_in_progress_started_at",
        }
        saved_arg = mock_save.await_args.args[1]
        assert saved_arg is result
        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.started_at == datetime(2026, 4, 1, 10, 1, 0, tzinfo=UTC)
        assert result.finished_at == datetime(2026, 4, 1, 10, 2, 0, tzinfo=UTC)
        assert result.sync_in_progress_started_at is None

    @pytest.mark.asyncio
    async def test_dispatches_chain_on_terminal_status(self) -> None:
        """Assert sync_queue_item dispatches the chained task when a running task completes."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {"_chain_task_names": [chain_task.name]},
        )
        done_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {"_chain_task_names": [chain_task.name]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=done_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=done_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_awaited_once_with(
            chain_task.name, done_history, [], await_annotations=True
        )

    @pytest.mark.asyncio
    async def test_no_chain_dispatch_when_still_running(self) -> None:
        """Assert sync_queue_item does not dispatch chain when task remains running."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {"_chain_task_names": [chain_task.name]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=running_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_chain_dispatch_when_task_failed_without_flag(self) -> None:
        """Assert sync_queue_item does not dispatch chain on FAILED without chain_on_failure."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {"_chain_task_names": [chain_task.name]},
        )
        failed_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.FAILED,
            {"_chain_task_names": [chain_task.name]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=failed_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=failed_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", NON_SUCCESS_TERMINAL_STATUSES)
    async def test_dispatches_chain_on_failure_with_flag(self, status) -> None:
        """Assert sync_queue_item dispatches chain on non-success terminal status with flag."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {"_chain_task_names": [chain_task.name], "_chain_on_failure": True},
        )
        terminal_history = _make_chain_history(
            main_task,
            status,
            {"_chain_task_names": [chain_task.name], "_chain_on_failure": True},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=terminal_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=terminal_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_awaited_once_with(
            chain_task.name, terminal_history, [], await_annotations=True
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.LOST,
            TaskHistoryStatusEnum.STALE,
        ],
    )
    async def test_no_chain_on_failure_without_flag(self, status) -> None:
        """Assert sync_queue_item does not dispatch chain on non-success without flag."""
        main_task = _make_chain_task("main-task")
        chain_task = _make_chain_task("chain-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {"_chain_task_names": [chain_task.name]},
        )
        terminal_history = _make_chain_history(
            main_task,
            status,
            {"_chain_task_names": [chain_task.name]},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=terminal_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=terminal_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_chain_dispatch_when_no_chain_task_names(self) -> None:
        """Assert sync_queue_item does not dispatch chain when chain_task_names is absent."""
        main_task = _make_chain_task("main-task")

        running_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.RUNNING,
            {},
        )
        done_history = _make_chain_history(
            main_task,
            TaskHistoryStatusEnum.SUCCESS,
            {},
        )

        session_maker, _ = _make_chain_session_mock()

        with (
            patch(
                "app.tasks.celery.get_async_session_maker", return_value=session_maker
            ),
            patch(
                "app.tasks.celery.TaskHistoryManager.get_or_404",
                new_callable=AsyncMock,
                return_value=running_history,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=main_task,
            ),
            patch("app.tasks.celery.get_executor_for_task") as mock_executor,
            patch(
                "app.tasks.celery.TaskHistoryManager.save",
                new_callable=AsyncMock,
                return_value=done_history,
            ),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            executor = AsyncMock()
            executor.sync_task_history = AsyncMock(return_value=done_history)
            mock_executor.return_value = executor

            await sync_queue_item(1)

        mock_chain.assert_not_awaited()


class TestMaybeDispatchChainMetaNone:
    """Cover ``maybe_dispatch_chain`` against ``execution_request.meta = None``.

    ``TaskExecutionRequest.meta`` is typed ``dict | None``; legacy rows or
    explicit-null payloads can surface a ``None`` here. Reading ``.get(...)``
    without normalising raises ``AttributeError`` and silently kills the
    chain dispatch path.
    """

    @pytest.mark.asyncio
    async def test_meta_none_does_not_raise_or_dispatch(self) -> None:
        """Assert ``meta=None`` short-circuits cleanly without dispatching a chain."""
        main_task = _make_chain_task("main-task")
        history = _make_chain_history(main_task, TaskHistoryStatusEnum.SUCCESS, {})
        history.execution_request.meta = None

        with patch(
            "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
        ) as mock_dispatch:
            await maybe_dispatch_chain(history, was_running=True)

        mock_dispatch.assert_not_awaited()


async def _create_tables(engine):
    """Create the Tasks metadata tables on ``engine``."""
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)


@contextmanager
def _sync_db_harness(mocker):
    """Stand up an in-memory aiosqlite engine and wire ``get_async_session_maker``.

    Yields ``(test_loop, async_session_maker)``. The loop is test-owned and is
    the same loop that the Celery wrapper's ``run_until_complete`` calls must
    use — pass it via ``patch.object(celery_module.celery, "loop", test_loop)``.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    test_loop = asyncio.new_event_loop()
    try:
        test_loop.run_until_complete(_create_tables(engine))
        async_session_maker = get_async_session_maker_from_engine(engine)
        mocker.patch(
            "app.tasks.celery.get_async_session_maker",
            return_value=async_session_maker,
        )
        yield test_loop, async_session_maker
    finally:
        test_loop.run_until_complete(engine.dispose())
        test_loop.close()


async def _seed_task(
    async_session_maker,
    *,
    name: str,
    backend: TaskBackendEnum = TaskBackendEnum.NOMAD,
    alert_on_fail: bool = False,
    protected: bool = False,
    data: dict | None = None,
) -> Task:
    """Insert a Task row via ``TaskManager.create`` and return the persisted instance."""
    if data is None:
        if backend == TaskBackendEnum.CELERY:
            data = {"target": "local", "callable": "app.tasks.noop"}
        elif backend == TaskBackendEnum.PROXY:
            data = {"task": "wrapped"}
        else:
            data = {"Constraints": [{"RTarget": "node-1"}]}
    async with async_session_maker() as session:
        return await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name=name,
                    backend=backend,
                    alert_on_fail=alert_on_fail,
                    protected=protected,
                    data=data,
                )
            ),
        )


async def _list_histories(async_session_maker, task_id: int) -> list[TaskHistory]:
    """Return all TaskHistory rows for ``task_id`` with ``execution_request`` loaded."""
    async with async_session_maker() as session:
        return await TaskHistoryManager.list(
            session,
            task_id=task_id,
            query_options=[undefer(TaskHistory.execution_request)],
        )


async def _seed_history(
    async_session_maker,
    task,
    *,
    payload: str | None,
    target: str = "node-1",
) -> TaskHistory:
    """Insert a PENDING TaskHistory row for ``task`` and return it with ``id`` loaded."""
    async with async_session_maker() as session:
        history = TaskHistory(
            task_id=task.id,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target=target,
                meta={"target": target},
                payload=payload,
                tracking={"evaluation_id": ""},
            ),
            status=TaskHistoryStatusEnum.PENDING,
            executed_by="test-user",
        )
        session.add(history)
        await session.commit()
        await session.refresh(history)
        return history


async def _set_history_running(async_session_maker, task_history_id: int) -> None:
    """Set a seeded TaskHistory to RUNNING so ``sync_queue_item`` syncs it."""
    async with async_session_maker() as session:
        await TaskHistoryManager.update_where(
            session,
            {"status": TaskHistoryStatusEnum.RUNNING},
            id=task_history_id,
        )


async def _list_log_chunks(async_session_maker, task_history_id: int):
    """Return all TaskHistoryLog chunks for ``task_history_id``."""
    async with async_session_maker() as session:
        return await TaskHistoryLogManager.list(
            session, task_history_id=task_history_id
        )


async def _fake_dispatch_mark_running(
    queue_item: TaskHistory,
    *,
    await_annotations: bool = False,
    periodic_task_name: str | None = None,
) -> TaskHistory:
    """Minimal dispatch stand-in: mark the item RUNNING and return it."""
    del await_annotations, periodic_task_name
    queue_item.status = TaskHistoryStatusEnum.RUNNING
    return queue_item


def _run_skip_gate(
    test_loop: asyncio.AbstractEventLoop,
    *,
    task_name: str,
    periodic_task_name: str | None = "periodic-test-task",
    target: str = "node-1",
) -> dict:
    """Invoke ``execute_task_by_name.__wrapped__`` under ``test_loop``."""
    with patch.object(celery_module.celery, "loop", test_loop):
        return celery_module.execute_task_by_name.__wrapped__(
            task_name=task_name,
            periodic_task_name=periodic_task_name,
            execution_data={"meta": {"target": target}},
        )


class TestExecuteTaskByName:
    """Test the pre-dispatch Nomad host-health gate in ``execute_task_by_name``."""

    def test_healthy_target_dispatches(self, mocker):
        """Assert dispatch proceeds when the target is in ``get_hosts()``."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {"node-1": "10.0.0.1"}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="test-task")

            assert mock_dispatch.call_count == 1
            assert (
                mock_dispatch.call_args.kwargs["periodic_task_name"]
                == "periodic-test-task"
            )
            mock_alert.assert_not_awaited()

    def test_unhealthy_target_skips_and_alerts(self, mocker):
        """Assert the gate persists a FAILED row, writes a stderr chunk, and alerts."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            result = _run_skip_gate(test_loop, task_name="test-task")

            mock_dispatch.assert_not_called()
            mock_alert.assert_awaited_once()
            alert_payload = mock_alert.await_args.args[0]
            assert alert_payload["dedup_key"] == "task:test-task:node-1"
            assert alert_payload["class"] == "task_dispatch_failure"
            assert alert_payload["source"] == "periodic-test-task:test-task:node-1"

            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            saved = rows[0]
            assert saved.status == TaskHistoryStatusEnum.FAILED
            assert saved.finished_at is not None
            assert saved.execution_request.target == "node-1"
            assert result["status"] == TaskHistoryStatusEnum.FAILED.value
            assert result["execution_request"]["target"] == "node-1"

            chunks = test_loop.run_until_complete(
                _list_log_chunks(async_session_maker, saved.id)
            )
            stderr_chunks = [c for c in chunks if c.stream == TaskLogType.STDERR]
            assert stderr_chunks
            assert "not ready on Nomad" in stderr_chunks[0].content
            assert stderr_chunks[0].source == "execution"

    def test_unhealthy_target_persists_failure_reason_matching_stderr(self, mocker):
        """Assert the persisted failure_reason is the reason written to stderr."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=False)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )

            _run_skip_gate(test_loop, task_name="test-task")

            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            saved = rows[0]
            chunks = test_loop.run_until_complete(
                _list_log_chunks(async_session_maker, saved.id)
            )
            stderr_chunks = [c for c in chunks if c.stream == TaskLogType.STDERR]
            assert saved.failure_reason == stderr_chunks[0].content
            assert "not ready on Nomad" in saved.failure_reason

    def test_unhealthy_target_no_alert_when_alert_on_fail_false(self, mocker):
        """Assert the FAILED row + log chunk are written but no alert fires."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=False)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="test-task")

            mock_alert.assert_not_awaited()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            assert rows[0].status == TaskHistoryStatusEnum.FAILED
            chunks = test_loop.run_until_complete(
                _list_log_chunks(async_session_maker, rows[0].id)
            )
            assert any(c.stream == TaskLogType.STDERR for c in chunks)

    def test_recovered_target_dispatches_on_next_tick(self, mocker):
        """Assert a second tick with a healthy target dispatches without a new FAILED row."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.side_effect = [{}, {"node-1": "10.0.0.1"}]
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mocker.patch.object(AlertService, "trigger", new_callable=AsyncMock)

            _run_skip_gate(test_loop, task_name="test-task")
            _run_skip_gate(test_loop, task_name="test-task")

            assert mock_dispatch.call_count == 1
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            failed_rows = [r for r in rows if r.status == TaskHistoryStatusEnum.FAILED]
            assert len(failed_rows) == 1

    def test_get_hosts_raises_base_nomad_exception(self, mocker):
        """Assert a ``BaseNomadException`` from ``get_hosts`` falls into the existing handler."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.side_effect = BaseNomadException("nomad down")
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="test-task")

            mock_dispatch.assert_not_called()
            mock_alert.assert_awaited_once()
            alert_payload = mock_alert.await_args.args[0]
            assert alert_payload["class"] == "task_dispatch_failure"
            assert alert_payload["dedup_key"] == "task:test-task:node-1"
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert all(r.status != TaskHistoryStatusEnum.FAILED for r in rows)

    def test_celery_backend_skips_gate(self, mocker):
        """Assert a resolved Celery backend bypasses the host-health gate."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="celery-task",
                    backend=TaskBackendEnum.CELERY,
                    protected=True,
                    alert_on_fail=True,
                )
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {"local": "localhost"}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="celery-task", target="not-in-hosts")

            assert mock_dispatch.call_count == 1
            mock_executor.get_hosts.assert_not_called()
            mock_alert.assert_not_awaited()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert all(r.status != TaskHistoryStatusEnum.FAILED for r in rows)

    def test_log_writer_failure_still_alerts_and_returns(self, mocker):
        """Assert a log-writer failure rolls back, does not propagate, and alert still fires."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )
            mocker.patch.object(
                TaskHistoryLogWriter,
                "append",
                new_callable=AsyncMock,
                side_effect=RuntimeError("disk full"),
            )

            result = _run_skip_gate(test_loop, task_name="test-task")

            mock_alert.assert_awaited_once()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            assert rows[0].status == TaskHistoryStatusEnum.FAILED
            assert result["execution_request"]["target"] == "node-1"

    def test_proxy_wrapped_nomad_unhealthy_target_skips_and_alerts(self, mocker):
        """Assert a proxy-wrapped Nomad task's root backend drives the gate."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            inner = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="inner-nomad-task",
                    alert_on_fail=True,
                )
            )
            wrapper = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="wrapper-proxy",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=True,
                    data={"task": "inner-nomad-task"},
                )
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="wrapper-proxy")

            mock_dispatch.assert_not_called()
            mock_alert.assert_awaited_once()
            wrapper_rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, wrapper.id)
            )
            inner_rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, inner.id)
            )
            assert len(wrapper_rows) == 1
            assert wrapper_rows[0].status == TaskHistoryStatusEnum.FAILED
            assert inner_rows == []

    def test_unhealthy_target_takes_precedence_over_identical_conflict(self, mocker):
        """Assert the gate fires before ``_dispatch_queue_item``'s conflict check."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )

            async def _seed_pending():
                async with async_session_maker() as session:
                    pending = TaskHistory(
                        task_id=task.id,
                        task=task,
                        execution_request=TaskExecutionRequest(
                            task=task.name,
                            target="node-1",
                            meta={"target": "node-1"},
                            payload=None,
                            tracking={"evaluation_id": ""},
                        ),
                        status=TaskHistoryStatusEnum.PENDING,
                        executed_by="seed",
                    )
                    return await TaskHistoryManager.save(session, pending)

            existing = test_loop.run_until_complete(_seed_pending())

            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mocker.patch.object(AlertService, "trigger", new_callable=AsyncMock)

            _run_skip_gate(test_loop, task_name="test-task")

            mock_dispatch.assert_not_called()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            statuses = sorted(r.status for r in rows)
            assert statuses == sorted(
                [TaskHistoryStatusEnum.PENDING, TaskHistoryStatusEnum.FAILED]
            )
            assert any(r.id == existing.id for r in rows)

    def test_returned_payload_is_serializable_after_skip(self, mocker):
        """Assert the returned dict exposes ``execution_request`` without a lazy-load fault."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            test_loop.run_until_complete(
                _seed_task(async_session_maker, name="test-task", alert_on_fail=True)
            )
            mock_executor = MagicMock(spec=BaseExecutor)
            mock_executor.get_hosts = MagicMock()
            mock_executor.get_hosts.return_value = {}
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=mock_executor
            )
            mocker.patch(
                "app.tasks.celery.dispatch_queue_item",
                side_effect=_fake_dispatch_mark_running,
            )
            mocker.patch.object(AlertService, "trigger", new_callable=AsyncMock)

            result = _run_skip_gate(test_loop, task_name="test-task")

            assert isinstance(result, dict)
            assert result["status"] == TaskHistoryStatusEnum.FAILED.value
            assert result["execution_request"]["task"] == "test-task"
            assert result["execution_request"]["target"] == "node-1"


class TestExecuteTaskByNamePeriodicAnnotationRegression:
    """Regression suite for the periodic dispatch ``STARTED`` annotation.

    Before this fix, ``_dispatch_queue_item`` posted the ``STARTED`` annotation
    via ``schedule_annotation`` (fire-and-forget ``asyncio.create_task``). When
    called from the Celery worker (``execute_task_by_name`` →
    ``celery.loop.run_until_complete(dispatch_queue_item(...))``), the inner
    coroutine returned immediately after scheduling and the loop stopped with
    the annotation task still pending; the HTTP POST never reached PMM.

    This end-to-end test drives ``execute_task_by_name.__wrapped__`` through
    the real ``run_until_complete``, lets the production
    ``_dispatch_queue_item`` flow run against a real in-memory aiosqlite
    session, and asserts ``annotate_task_event`` is awaited with
    ``event="STARTED"`` before the wrapper returns. A regression that
    reverts to ``schedule_annotation`` here would fail this assertion
    because the abandoned background task never reaches the PMM boundary.
    """

    def test_started_annotation_reaches_pmm_for_periodic_dispatch(self, mocker):
        """Assert ``annotate_task_event`` is awaited with ``STARTED``."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            test_loop.run_until_complete(
                _seed_task(async_session_maker, name="backup_data", alert_on_fail=False)
            )

            async def fake_dispatch_task(
                passed_session: AsyncSession,
                item: TaskHistory,
                _task: Task | None = None,
            ) -> TaskHistory:
                item.status = TaskHistoryStatusEnum.RUNNING
                return await TaskHistoryManager.save(
                    passed_session, item, flag_modified_fields=["execution_request"]
                )

            fake_executor = MagicMock(spec=BaseExecutor)
            fake_executor.get_hosts = MagicMock(return_value={"node-1": "10.0.0.1"})
            fake_executor.dispatch_task = fake_dispatch_task
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=fake_executor
            )
            mock_annotate = mocker.patch(
                "app.core.pmm.annotate_task_event", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="backup_data")

        mock_annotate.assert_awaited_once()
        kwargs = mock_annotate.await_args.kwargs
        assert kwargs["event"] == "STARTED"
        assert kwargs["task_name"] == "backup_data"
        assert kwargs["target"] == "node-1"


def _noop_async_session_maker():
    """Return a session maker whose sessions are no-op context managers.

    Used to satisfy ``_dispatch_queue_item``'s lock-session helper
    without touching a real database — the dispatch lock is patched out
    separately. Keeping the session maker stub minimal avoids
    duplicating the ``_make_lock_session_maker`` helper for the single
    regression test that needs it.

    :return: A callable returning an async-context-manager session stub.
    :rtype: callable
    """
    lock_session = AsyncMock()
    lock_session_cm = AsyncMock()
    lock_session_cm.__aenter__ = AsyncMock(return_value=lock_session)
    lock_session_cm.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=lock_session_cm)


class TestInternalDispatchQueueItemRegression:
    """Regression suite for the real-session ``_dispatch_queue_item``.

    ``TaskHistoryManager.save`` re-defers the ``execution_request``
    ``column_property`` via its internal ``session.refresh(instance)``.
    Before this fix, ``schedule_annotation(result, "STARTED")`` then
    touched that deferred attribute synchronously and crashed with
    ``MissingGreenlet`` on async drivers (asyncpg, aiosqlite).

    These tests use the real ``session`` fixture — NOT
    ``AsyncMock(spec=AsyncSession)`` — so a future regression that
    drops the explicit refresh will reproduce the production failure
    mode. Mocking the subject's own session bypasses SQLAlchemy's
    lifecycle and would let the test pass even if the refresh line
    were deleted.
    """

    @pytest.mark.asyncio
    async def test_started_annotation_has_execution_request_loaded(
        self, session: AsyncSession
    ):
        """Assert ``schedule_annotation`` receives a loaded instance.

        Reproduce the production flow: a fake ``dispatch_task`` runs
        the real ``TaskHistoryManager.save`` (which re-defers the
        deferred column), then control returns to ``_dispatch_queue_item``
        which must explicitly refresh ``execution_request`` before
        calling ``schedule_annotation``. The test captures the argument
        passed to ``schedule_annotation`` and asserts the deferred
        column is loaded.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="backup_data", backend=TaskBackendEnum.NOMAD)
            ),
        )
        queue_item = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.PENDING,
            executed_by="test-user",
        )

        async def fake_dispatch_task(
            passed_session: AsyncSession,
            item: TaskHistory,
            _task: Task | None = None,
        ) -> TaskHistory:
            item.status = TaskHistoryStatusEnum.RUNNING
            return await TaskHistoryManager.save(
                passed_session, item, flag_modified_fields=["execution_request"]
            )

        captured = []

        def capture_annotation(arg: TaskHistory, _event: str) -> None:
            captured.append(arg)

        fake_executor = AsyncMock()
        fake_executor.dispatch_task = fake_dispatch_task

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=_noop_async_session_maker(),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete_where",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.create",
                new_callable=AsyncMock,
                return_value=MagicMock(spec=DispatchLock),
            ),
            patch(
                "app.tasks.celery.DispatchLockManager.delete",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery._raise_if_identical_task_conflict",
                new_callable=AsyncMock,
            ),
            patch(
                "app.tasks.celery.TaskManager.get_root_task",
                new_callable=AsyncMock,
                return_value=task,
            ),
            patch(
                "app.tasks.celery.get_executor_for_task",
                return_value=fake_executor,
            ),
            patch(
                "app.tasks.celery.schedule_annotation",
                side_effect=capture_annotation,
            ),
        ):
            result = await _dispatch_queue_item(queue_item, session)

        assert len(captured) == 1
        annotated = captured[0]
        assert "execution_request" not in sa_inspect(annotated).unloaded
        assert annotated.execution_request.task == task.name
        assert annotated.execution_request.target == "node-1"
        assert result.status == TaskHistoryStatusEnum.RUNNING


class _SharedSessionContextManager:
    """Wrap an ``AsyncSession`` as a reusable async context manager.

    ``sync_queue_item`` opens several ``async with async_session() as s:``
    blocks in sequence; to let the real save path run against the test
    DB, each block must yield the same session without closing it.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the shared session.

        :param session: The test session to yield from each ``__aenter__``.
        :type session: AsyncSession
        """
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        """Return the shared session without opening a new one.

        :return: The shared session.
        :rtype: AsyncSession
        """
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        """Do nothing — the pytest fixture owns the session lifetime.

        :param _exc: The exception info tuple passed by the runtime.
        :type _exc: object
        """


class TestSyncQueueItemRegression:
    """Regression suite for the real-session ``sync_queue_item``.

    After ``TaskHistoryManager.save`` inside the ``async with
    async_session()`` block, ``saved.execution_request`` is re-deferred
    by the save's internal ``session.refresh(instance)``. Chain-dispatch
    logic reads ``saved.execution_request.meta`` twice **after** the
    ``async with`` exits, at which point ``saved`` is also detached.
    Before this fix, that read raised ``DetachedInstanceError`` on sync
    drivers or ``MissingGreenlet`` on async drivers.
    """

    @pytest.mark.asyncio
    async def test_chain_dispatch_reads_survive_detached_session(
        self, session: AsyncSession
    ):
        """Assert chain-dispatch reads succeed after the save-block exits.

        Arrange a ``TaskHistory`` whose sync transitions it to SUCCESS
        with ``_chain_task_names`` set; exercise ``sync_queue_item``
        end-to-end and assert ``_dispatch_chained_task`` was invoked
        (which is only possible if the ``saved.execution_request.meta``
        reads at the end of ``sync_queue_item`` did not crash).
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="parent-task",
                    backend=TaskBackendEnum.NOMAD,
                    alert_on_fail=False,
                )
            ),
        )
        chain_task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(
                    name="next-task",
                    backend=TaskBackendEnum.NOMAD,
                    alert_on_fail=False,
                )
            ),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-1",
                meta={"_chain_task_names": [chain_task.name]},
                tracking={"evaluation_id": ""},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved_history = await TaskHistoryManager.save(session, history)

        async def fake_sync(
            item: TaskHistory,
            *,
            writer_session=None,
            await_annotations: bool = False,
        ) -> TaskHistory:
            del writer_session, await_annotations
            item.status = TaskHistoryStatusEnum.SUCCESS
            return item

        fake_executor = MagicMock()
        fake_executor.sync_task_history = AsyncMock(side_effect=fake_sync)

        shared_session_maker = MagicMock(
            return_value=_SharedSessionContextManager(session)
        )

        with (
            patch(
                "app.tasks.celery.get_async_session_maker",
                return_value=shared_session_maker,
            ),
            patch("app.tasks.celery.get_executor_for_task", return_value=fake_executor),
            patch(
                "app.tasks.celery._dispatch_chained_task", new_callable=AsyncMock
            ) as mock_chain,
        ):
            await sync_queue_item(saved_history.id)

        mock_chain.assert_awaited_once()
        parent_arg = mock_chain.await_args.args[1]
        assert "execution_request" not in sa_inspect(parent_arg).unloaded

    def test_failure_reason_survives_the_sync_save(self, mocker):
        """Assert a reason the executor set survives ``sync_queue_item``'s save.

        ``sync_queue_item`` loads the row in one session, hands it to the
        executor under a second, and saves it under a third, so this pins the
        round trip that the per-arm Nomad unit tests cannot see: they assert
        against the in-memory ``queue_item``, which carries the reason whether
        or not the final UPDATE writes it.
        """
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(async_session_maker, name="reason-persist-task")
            )
            history = test_loop.run_until_complete(
                _seed_history(async_session_maker, task, payload=None)
            )
            test_loop.run_until_complete(
                _set_history_running(async_session_maker, history.id)
            )

            async def fake_sync(
                item: TaskHistory,
                *,
                writer_session=None,
                await_annotations: bool = False,
            ) -> TaskHistory:
                del writer_session, await_annotations
                item.status = TaskHistoryStatusEnum.FAILED
                item.set_failure_reason("Step 'run-script' failed (exit code 1).")
                return item

            fake_executor = MagicMock(spec=BaseExecutor)
            fake_executor.sync_task_history = AsyncMock(side_effect=fake_sync)
            mocker.patch(
                "app.tasks.celery.get_executor_for_task", return_value=fake_executor
            )
            mocker.patch("app.tasks.celery.maybe_record_run", new_callable=AsyncMock)

            test_loop.run_until_complete(sync_queue_item(history.id))

            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert rows[0].status == TaskHistoryStatusEnum.FAILED
            assert rows[0].failure_reason == "Step 'run-script' failed (exit code 1)."


def _write_self_signed_pem(
    path: Path,
    *,
    not_valid_after: datetime,
    not_valid_before: datetime | None = None,
    common_name: str = "test",
) -> None:
    """Build and write a minimal self-signed PEM certificate for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
    nvb = not_valid_before or (ANCHOR - timedelta(days=1))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nvb)
        .not_valid_after(not_valid_after)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _nomad_config_for_paths(
    *,
    ca: Path | None = None,
    cert: Path | None = None,
    warn_days: int = 7,
) -> NomadExecutor:
    """Return a NomadExecutor config object for cert-expiry task tests."""
    return NomadExecutor(
        endpoint="http://127.0.0.1:4646",
        verify_ssl=False,
        ssl_cafile=ca,
        ssl_certfile=cert,
        cert_expiry_warn_days=warn_days,
    )


class TestCheckNomadCertExpiry:
    """Test _check_nomad_cert_expiry and check_nomad_cert_expiry."""

    @pytest.mark.asyncio
    async def test_healthy_certs_call_resolve_not_trigger(
        self, mocker, tmp_path: Path
    ) -> None:
        """Assert long-lived CA and client PEMs call resolve and do not trigger."""
        ca = tmp_path / "ca.pem"
        cl = tmp_path / "client.pem"
        _write_self_signed_pem(
            ca, not_valid_after=ANCHOR + timedelta(days=30), common_name="ca"
        )
        _write_self_signed_pem(
            cl, not_valid_after=ANCHOR + timedelta(days=30), common_name="cl"
        )
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=ca, cert=cl)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)

        await _check_nomad_cert_expiry()

        mock_alert.trigger.assert_not_called()
        assert mock_alert.resolve.call_count == EXPECTED_NOMAD_CERT_RESOLVE_CALLS
        called = {c.args[0] for c in mock_alert.resolve.call_args_list}
        assert called == {
            f"nomad-cert-expiry:{ca.name}",
            f"nomad-cert-expiry:{cl.name}",
        }

    @pytest.mark.asyncio
    async def test_within_window_triggers_warning(self, mocker, tmp_path: Path) -> None:
        """Assert a cert within the warning window triggers WARNING."""
        ca = tmp_path / "w.pem"
        _write_self_signed_pem(
            ca, not_valid_after=ANCHOR + timedelta(days=7), common_name="w"
        )
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=ca, cert=None)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)

        await _check_nomad_cert_expiry()

        mock_alert.trigger.assert_called_once()
        alert = mock_alert.trigger.call_args[0][0]
        assert alert["severity"] is AlertSeverity.WARNING
        assert alert["dedup_key"] == f"nomad-cert-expiry:{ca.name}"
        assert "7 day" in alert["summary"]

    @pytest.mark.asyncio
    async def test_just_beyond_window_resolves_only(
        self, mocker, tmp_path: Path
    ) -> None:
        """Assert a cert just past the warning window only resolves and does not trigger."""
        ca = tmp_path / "ok.pem"
        _write_self_signed_pem(
            ca, not_valid_after=ANCHOR + timedelta(days=8), common_name="ok"
        )
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=ca, cert=None)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)

        await _check_nomad_cert_expiry()

        mock_alert.trigger.assert_not_called()
        mock_alert.resolve.assert_called_once_with(f"nomad-cert-expiry:{ca.name}")

    @pytest.mark.asyncio
    async def test_expired_triggers_critical(self, mocker, tmp_path: Path) -> None:
        """Assert an expired cert triggers CRITICAL."""
        ca = tmp_path / "x.pem"
        _write_self_signed_pem(
            ca, not_valid_after=ANCHOR - timedelta(hours=1), common_name="x"
        )
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=ca, cert=None)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)

        await _check_nomad_cert_expiry()

        mock_alert.trigger.assert_called_once()
        mock_alert.resolve.assert_not_called()
        alert = mock_alert.trigger.call_args[0][0]
        assert alert["severity"] is AlertSeverity.CRITICAL
        assert alert["dedup_key"] == f"nomad-cert-expiry:{ca.name}"

    @pytest.mark.asyncio
    async def test_path_read_bytes_oserror_logs_and_skips(
        self, mocker, tmp_path: Path
    ) -> None:
        """Assert OSError from read_bytes is logged and does not raise."""
        ca = tmp_path / "io.pem"
        ca.write_bytes(b"unused")
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=ca, cert=None)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mocker.patch.object(
            Path, "read_bytes", side_effect=OSError("simulated read failure")
        )
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)
        log_warning = mocker.patch(f"{MODULE}.logger.warning")

        await _check_nomad_cert_expiry()

        log_warning.assert_called()
        assert "Could not read" in str(log_warning.call_args)
        mock_alert.trigger.assert_not_called()
        mock_alert.resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_pem_valueerror_logs_and_skips(
        self, mocker, tmp_path: Path
    ) -> None:
        """Assert non-PEM file bytes log and ValueError is handled (no raise)."""
        bad = tmp_path / "not-pem.pem"
        bad.write_bytes(b"not a pem")
        mocker.patch(
            "app.tasks.celery.tasks_settings",
            MagicMock(NOMAD=_nomad_config_for_paths(ca=bad, cert=None)),
        )
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        log_warning = mocker.patch(f"{MODULE}.logger.warning")

        await _check_nomad_cert_expiry()

        log_warning.assert_called()
        assert "Could not parse" in str(log_warning.call_args)

    @pytest.mark.asyncio
    async def test_missing_file_warns_and_skips(self, mocker, tmp_path: Path) -> None:
        """Assert a PEM path missing at read time logs a warning and skips alerting."""
        ca = tmp_path / "ca.pem"
        _write_self_signed_pem(
            ca, not_valid_after=ANCHOR + timedelta(days=30), common_name="ca"
        )
        nomad = _nomad_config_for_paths(ca=ca, cert=None, warn_days=7)
        # Exists at config-load (passes path validation) but gone by read time.
        ca.unlink()
        mocker.patch("app.tasks.celery.tasks_settings", MagicMock(NOMAD=nomad))
        mocker.patch("app.core.utils.utc_now", return_value=ANCHOR)
        mock_alert = MagicMock()
        mock_alert.trigger = AsyncMock()
        mock_alert.resolve = AsyncMock()
        mocker.patch("app.core.alerts.config.alert_service", mock_alert)
        log_warning = mocker.patch(f"{MODULE}.logger.warning")

        await _check_nomad_cert_expiry()

        log_warning.assert_called()
        assert "Could not read" in str(log_warning.call_args)
        mock_alert.trigger.assert_not_called()
        mock_alert.resolve.assert_not_called()

    def test_celery_entrypoint_uses_event_loop(self, mocker) -> None:
        """Assert check_nomad_cert_expiry runs the async helper via the event loop."""
        from app.celery import celery as app_celery

        coro = MagicMock()
        mock_check = MagicMock(return_value=coro)
        mocker.patch(f"{MODULE}._check_nomad_cert_expiry", mock_check)
        mocker.patch.object(
            app_celery.loop,  # ty: ignore[unresolved-attribute]
            "run_until_complete",
            autospec=True,
        )

        check_nomad_cert_expiry()
        app_celery.loop.run_until_complete.assert_called_once_with(  # ty: ignore[unresolved-attribute]
            coro
        )


class TestPreDispatchUnreadableRequest:
    """Test the pre-dispatch refusal of an execution request that cannot be read.

    A persisted row is re-dispatched by id, so a leaf this deployment's key
    cannot decrypt would otherwise reach the executor as the stored token.
    """

    @pytest.mark.asyncio
    async def test_unreadable_row_fails_terminally_without_dispatching(
        self, session: AsyncSession, mocker
    ) -> None:
        """Assert the row is persisted FAILED with a reason and never dispatched.

        :param session: The async session the row is seeded and loaded through.
        :param mocker: The patching fixture used to observe the dispatch boundary.
        """
        task = await _create_pg_task(session)
        token = Fernet(Fernet.generate_key()).encrypt(b'"secret"').decode("ascii")
        seeded = await _seed_pg_history(
            session, task_id=task.id, task_name=task.name, meta={"target": "node-1"}
        )
        await overwrite_execution_request(
            session,
            seeded.id,
            {
                "task": task.name,
                "target": "node-1",
                "meta": {"target": "node-1"},
                "payload": token,
            },
        )
        session.expunge_all()
        loaded = await TaskHistoryManager.get_or_404(
            session,
            select_related=[TaskHistory.task],
            query_options=[undefer(TaskHistory.execution_request)],
            id=seeded.id,
        )
        mock_dispatch = mocker.patch(
            f"{MODULE}._dispatch_queue_item", new_callable=AsyncMock
        )

        result = await dispatch_queue_item(loaded, session)

        mock_dispatch.assert_not_awaited()
        assert result.status == TaskHistoryStatusEnum.FAILED
        assert result.finished_at is not None
        assert result.failure_reason is not None
        assert PAYLOAD_LEAF in result.failure_reason
        assert "ENCRYPTION_KEY" in result.failure_reason

    @pytest.mark.asyncio
    async def test_readable_row_is_dispatched_normally(
        self, session: AsyncSession, mocker
    ) -> None:
        """Assert the refusal is scoped to an unreadable row, not to every row.

        :param session: The async session the row is seeded and loaded through.
        :param mocker: The patching fixture used to observe the dispatch boundary.
        """
        task = await _create_pg_task(session)
        seeded = await _seed_pg_history(
            session,
            task_id=task.id,
            task_name=task.name,
            meta={"target": "node-1"},
            payload="secret document",
        )
        session.expunge_all()
        loaded = await TaskHistoryManager.get_or_404(
            session,
            select_related=[TaskHistory.task],
            query_options=[undefer(TaskHistory.execution_request)],
            id=seeded.id,
        )
        mock_dispatch = mocker.patch(
            f"{MODULE}._dispatch_queue_item",
            new_callable=AsyncMock,
            return_value=loaded,
        )

        await dispatch_queue_item(loaded, session)

        mock_dispatch.assert_awaited_once()


class TestPreDispatchPayloadCheck:
    """Test the pre-dispatch payload-resolution gate (``_pre_dispatch_payload_check``)."""

    _BROKEN_DATA = {"task": "wrapped", "payload": "file:///nonexistent/x_payload"}

    def test_unresolvable_payload_persists_failed_logs_and_alerts(self, mocker):
        """Assert an unresolvable payload persists FAILED, writes stderr, and alerts."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=True,
                    data=self._BROKEN_DATA,
                )
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery._dispatch_queue_item",
                new_callable=AsyncMock,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            result = _run_skip_gate(test_loop, task_name="test-task")

            mock_dispatch.assert_not_awaited()
            mock_alert.assert_awaited_once()
            alert_payload = mock_alert.await_args.args[0]
            assert alert_payload["class"] == "task_dispatch_failure"
            assert alert_payload["dedup_key"] == "task:test-task:node-1"

            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            saved = rows[0]
            assert saved.status == TaskHistoryStatusEnum.FAILED
            assert saved.finished_at is not None
            assert result["status"] == TaskHistoryStatusEnum.FAILED.value

            chunks = test_loop.run_until_complete(
                _list_log_chunks(async_session_maker, saved.id)
            )
            stderr_chunks = [c for c in chunks if c.stream == TaskLogType.STDERR]
            assert stderr_chunks
            assert "file:///nonexistent/x_payload" in stderr_chunks[0].content

    def test_unresolvable_payload_persists_failure_reason(self, mocker):
        """Assert the persisted failure_reason names the task and the underlying error.

        The payload-resolution failure is the one reason permitted to interpolate
        an underlying error message, so the stored value carries it verbatim.
        """
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=False,
                    data=self._BROKEN_DATA,
                )
            )
            mocker.patch(
                "app.tasks.celery._dispatch_queue_item",
                new_callable=AsyncMock,
            )

            _run_skip_gate(test_loop, task_name="test-task")

            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            saved = rows[0]
            assert saved.failure_reason is not None
            assert "Task payload could not be resolved" in saved.failure_reason
            assert "/nonexistent/x_payload" in saved.failure_reason
            assert "\n" not in saved.failure_reason

    def test_unresolvable_payload_gates_before_health_check(self, mocker):
        """Assert the payload gate persists FAILED before the health check runs Nomad.

        The health check calls ``executor.get_hosts()``, which contacts Nomad and
        can raise when the target is unreachable; an unresolvable payload must fail
        terminally ahead of that contact rather than depend on its outcome.
        """
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=False,
                    data=self._BROKEN_DATA,
                )
            )
            mock_health = mocker.patch(
                "app.tasks.celery._pre_dispatch_health_check",
                new_callable=AsyncMock,
                side_effect=BaseNomadException("nomad down"),
            )

            _run_skip_gate(test_loop, task_name="test-task")

            mock_health.assert_not_awaited()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            assert rows[0].status == TaskHistoryStatusEnum.FAILED

    def test_unresolvable_payload_no_alert_when_alert_on_fail_false(self, mocker):
        """Assert the FAILED row and stderr chunk are written but no alert fires."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            task = test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=False,
                    data=self._BROKEN_DATA,
                )
            )
            mock_dispatch = mocker.patch(
                "app.tasks.celery._dispatch_queue_item",
                new_callable=AsyncMock,
            )
            mock_alert = mocker.patch.object(
                AlertService, "trigger", new_callable=AsyncMock
            )

            _run_skip_gate(test_loop, task_name="test-task")

            mock_dispatch.assert_not_awaited()
            mock_alert.assert_not_awaited()
            rows = test_loop.run_until_complete(
                _list_histories(async_session_maker, task.id)
            )
            assert len(rows) == 1
            assert rows[0].status == TaskHistoryStatusEnum.FAILED
            chunks = test_loop.run_until_complete(
                _list_log_chunks(async_session_maker, rows[0].id)
            )
            assert any(c.stream == TaskLogType.STDERR for c in chunks)

    def test_resolvable_payload_returns_none_to_proceed(self, mocker, tmp_path):
        """Assert the gate returns None (proceed) for a resolvable payload reference."""
        payload_file = tmp_path / "payload_script"
        payload_file.write_text("print('ok')")
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=True,
                    data={"task": "wrapped", "payload": f"file://{payload_file}"},
                )
            )
            task_history = test_loop.run_until_complete(
                prepare_periodic_task_history(
                    "test-task", {"meta": {"target": "node-1"}}
                )
            )

            result = test_loop.run_until_complete(
                celery_module._pre_dispatch_payload_check(
                    task_history, "test-task", None
                )
            )

            assert result is None

    @pytest.mark.parametrize(
        "read_error",
        [
            PermissionError("denied"),
            UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte"),
        ],
    )
    def test_unreadable_payload_persists_failed(self, mocker, read_error):
        """Assert a resolvable-but-unreadable payload (read error) also persists FAILED."""
        with _sync_db_harness(mocker) as (test_loop, async_session_maker):
            test_loop.run_until_complete(
                _seed_task(
                    async_session_maker,
                    name="test-task",
                    backend=TaskBackendEnum.PROXY,
                    alert_on_fail=False,
                    data={
                        "task": "wrapped",
                        "payload": "file://app/extensions/plugins/mysql_backups/binlog_payload",
                    },
                )
            )
            unreadable = mocker.MagicMock()
            unreadable.read_text.side_effect = read_error
            mocker.patch(
                "app.tasks.models.resolve_payload_reference", return_value=unreadable
            )
            task_history = test_loop.run_until_complete(
                prepare_periodic_task_history(
                    "test-task", {"meta": {"target": "node-1"}}
                )
            )

            result = test_loop.run_until_complete(
                celery_module._pre_dispatch_payload_check(
                    task_history, "test-task", None
                )
            )

            assert result is not None
            assert result.status == TaskHistoryStatusEnum.FAILED
