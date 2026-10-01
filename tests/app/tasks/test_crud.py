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

"""Define tests for the Tasks CRUD managers."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.auth.exceptions import HTTPForbiddenException
from app.core.db import ListQuery
from app.core.db.list_query import build_search_predicate
from app.core.db.utils import get_async_session_maker_from_engine, NullsLastOrdering
from app.core.encryption import is_encrypted
from app.core.exceptions import HTTPConflictException, HTTPNotFoundException
from app.core.pagination import (
    DEFAULT_PAGINATION_LIMIT,
    DEFAULT_PAGINATION_OFFSET,
    Pagination,
)
from app.core.utils.date_time import utc_now
from app.extensions.apps.meta_keys import SERVICE_ID_META_KEY
from app.tasks.crud import (
    DispatchLockManager,
    TaskHistoryLogManager,
    TaskHistoryLogStateManager,
    TaskHistoryManager,
    TaskManager,
)
from app.tasks.execution.executors.nomad.steps import NomadStep
from app.tasks.execution_request_secrets import ENCRYPTED_META_KEYS
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    DispatchLock,
    LogCaptureStatusEnum,
    SYSTEM_USER,
    Task,
    TaskBackendEnum,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryLog,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskWrite,
)
from tests.app.factories import TaskFactory
from tests.app.tasks.conftest import stored_execution_request

HISTORY_FIXTURE_COUNT = 3
PAGINATED_TASK_COUNT = 2
PAGINATED_CUSTOM_TASK_COUNT = 3
PBM_CONFIG_BACKUP_TYPE = "pbm_config"
PARENT_FILTER_TASK_COUNT = 3
SEARCH_MATCH_TOTAL = 2
TIE_BREAK_TOTAL = 3
CHUNKS_AT_OR_BELOW_OFFSET = 2


def _default_list_query(
    manager: type[TaskManager] | type[TaskHistoryManager],
) -> ListQuery:
    """Build a default ListQuery from a manager's list_query_spec.

    :param manager: The manager whose ``list_query_spec`` supplies the default sort.
    :return: A ListQuery with the spec default ordering and no search predicate.
    """
    return ListQuery(
        order_by=tuple(manager.list_query_spec.resolve_sort(None)),
        search_predicate=None,
    )


def _list_query(
    manager: type[TaskManager] | type[TaskHistoryManager],
    *,
    sort: str | None = None,
    search: str | None = None,
) -> ListQuery:
    """Build a ListQuery with an optional sort key and search term.

    :param manager: The manager whose ``list_query_spec`` bounds sort and search.
    :param sort: Public sort key (optionally ``-`` prefixed), or ``None`` to use the
        spec default sort.
    :param search: Raw search term, or ``None`` / empty for no search predicate.
    :return: A resolved ListQuery for the manager's list-query applier.
    """
    spec = manager.list_query_spec
    return ListQuery(
        order_by=tuple(spec.resolve_sort(sort)),
        search_predicate=build_search_predicate(search, spec.searchable),
    )


async def _create_task(
    session: AsyncSession,
    *,
    name: str = "test-task",
    owner: str = "ANY",
    is_template: bool = False,
    protected: bool = False,
    backend: TaskBackendEnum = TaskBackendEnum.NOMAD,
    data: dict | None = None,
) -> Task:
    """Create and persist a task with the given attributes.

    :param session: The async database session.
    :type session: AsyncSession
    :param name: The task name.
    :type name: str
    :param owner: The task owner.
    :type owner: str
    :param is_template: Whether the task is a template.
    :type is_template: bool
    :param protected: Whether the task is protected.
    :type protected: bool
    :param backend: The task backend.
    :type backend: TaskBackendEnum
    :param data: The task data payload.
    :type data: dict | None
    :return: The persisted task.
    :rtype: Task
    """
    task_data = TaskFactory.build(
        name=name,
        owner=owner,
        is_template=is_template,
        protected=protected,
        backend=backend,
        data=data if data is not None else {"job": "test"},
    )
    return await TaskManager.create(session, TaskWrite.model_validate(task_data))


async def _create_task_history(
    session: AsyncSession,
    task: Task,
    *,
    status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.SUCCESS,
    snippet_filename: str | None = None,
    finished_at: datetime | None = None,
    executed_by: str | None = None,
    created_at: datetime | None = None,
) -> TaskHistory:
    """Create and persist a task history record.

    :param session: The async database session.
    :type session: AsyncSession
    :param task: The parent task.
    :type task: Task
    :param status: The history status.
    :type status: TaskHistoryStatusEnum
    :param snippet_filename: Optional snippet filename in meta.
    :type snippet_filename: str | None
    :param finished_at: Optional completion timestamp for the history row.
    :type finished_at: datetime | None
    :param executed_by: Optional executor marker (e.g. ``SYSTEM_USER``) stamped
        on the row; left at the model default when ``None``.
    :type executed_by: str | None
    :param created_at: Optional ``created_at`` to force on the row; left at the
        model default (``utc_now``) when ``None``.
    :type created_at: datetime | None
    :return: The persisted task history.
    :rtype: TaskHistory
    """
    meta = {}
    if snippet_filename:
        meta["_snippet_filename"] = snippet_filename
    history = TaskHistory(
        task_id=task.id,
        status=status,
        finished_at=finished_at,
        executed_by=executed_by,
        execution_request={
            "task": task.name,
            "target": "localhost",
            "meta": meta,
            "tracking": {"allocation_id": None, "evaluation_id": None},
        },
        **({"created_at": created_at} if created_at is not None else {}),
    )
    return await TaskHistoryManager.save(session, history)


# ---------------------------------------------------------------------------
# TaskManager.list_active
# ---------------------------------------------------------------------------


class TestTaskManagerListActive:
    """Test TaskManager.list_active."""

    @pytest.mark.asyncio
    async def test_returns_only_non_deleted_tasks(self, session: AsyncSession) -> None:
        """Assert only active (non-deleted) tasks are returned."""
        task = await _create_task(session, name="active-task")
        await _create_task(session, name="to-delete")
        await TaskManager.delete_by_name(session, "to-delete")

        result = await TaskManager.list_active(session)

        assert len(result) == 1
        assert result[0].id == task.id

    @pytest.mark.asyncio
    async def test_with_owner_filter(self, session: AsyncSession) -> None:
        """Assert owner filter restricts returned tasks."""
        await _create_task(session, name="backup-task", owner="BACKUPS")
        await _create_task(session, name="alter-task", owner="ALTERS")

        result = await TaskManager.list_active(session, owner="BACKUPS")

        assert len(result) == 1
        assert result[0].name == "backup-task"

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self, session: AsyncSession) -> None:
        """Assert empty database returns an empty list."""
        result = await TaskManager.list_active(session)

        assert result == []

    @pytest.mark.asyncio
    async def test_with_target_filter(self, session: AsyncSession) -> None:
        """Assert target filter restricts returned tasks by data.meta.target."""
        await _create_task(
            session,
            name="host1-task",
            data={"meta": {"target": "host1"}},
        )
        await _create_task(
            session,
            name="host2-task",
            data={"meta": {"target": "host2"}},
        )

        result = await TaskManager.list_active(session, target="host1")

        assert len(result) == 1
        assert result[0].name == "host1-task"


# ---------------------------------------------------------------------------
# TaskManager.iter_active_batches
# ---------------------------------------------------------------------------


class TestTaskManagerIterActiveBatches:
    """Test TaskManager.iter_active_batches."""

    @staticmethod
    async def _collect(
        session: AsyncSession, *, owner: str | None = None, batch_size: int
    ) -> list[list[str]]:
        """Return the yielded batches as lists of task names.

        :param session: The async database session.
        :param owner: The owner filter to pass through, or ``None`` for all.
        :param batch_size: Rows per batch.
        :return: One list of task names per yielded batch, in yield order.
        """
        return [
            [task.name for task in batch]
            async for batch in TaskManager.iter_active_batches(
                session, owner=owner, batch_size=batch_size
            )
        ]

    @pytest.mark.asyncio
    async def test_yields_every_active_task_in_id_order(
        self, session: AsyncSession
    ) -> None:
        """Assert the batches partition the active population by ascending id."""
        for index in range(5):
            await _create_task(session, name=f"task-{index}")

        batches = await self._collect(session, batch_size=2)

        assert batches == [
            ["task-0", "task-1"],
            ["task-2", "task-3"],
            ["task-4"],
        ]

    @pytest.mark.asyncio
    async def test_skips_deleted_and_other_owners(self, session: AsyncSession) -> None:
        """Assert the same active-and-owner scope as ``list_active``."""
        await _create_task(session, name="kept", owner="BACKUPS")
        await _create_task(session, name="other-owner", owner="ALTERS")
        await _create_task(session, name="deleted", owner="BACKUPS")
        await TaskManager.delete_by_name(session, "deleted")

        batches = await self._collect(session, owner="BACKUPS", batch_size=10)

        assert batches == [["kept"]]

    @pytest.mark.asyncio
    async def test_empty_db_yields_nothing(self, session: AsyncSession) -> None:
        """Assert an empty population yields no batch at all."""
        assert await self._collect(session, batch_size=10) == []

    @pytest.mark.asyncio
    async def test_a_full_final_batch_ends_the_pass(
        self, session: AsyncSession
    ) -> None:
        """Assert a population that divides evenly does not repeat its last batch.

        The keyset cursor advances past the highest id seen, so a re-queried
        final page would surface as a duplicate batch rather than as an error.
        """
        for index in range(4):
            await _create_task(session, name=f"task-{index}")

        batches = await self._collect(session, batch_size=2)

        assert batches == [["task-0", "task-1"], ["task-2", "task-3"]]

    @pytest.mark.asyncio
    async def test_a_gap_in_the_ids_skips_no_row(self, session: AsyncSession) -> None:
        """Assert the cursor tracks the last id seen, not the count of rows seen.

        Ids are only contiguous until something is hard-deleted, and the pass an
        exhaustive count rests on must not step over the rows either side of the
        hole.
        """
        for index in range(5):
            await _create_task(session, name=f"task-{index}")
        await TaskManager.delete_where(session, col(Task.name) == "task-1")

        batches = await self._collect(session, batch_size=2)

        assert batches == [["task-0", "task-2"], ["task-3", "task-4"]]


# ---------------------------------------------------------------------------
# TaskManager.retrieve_by_name
# ---------------------------------------------------------------------------


class TestTaskManagerRetrieveByName:
    """Test TaskManager.retrieve_by_name."""

    @pytest.mark.asyncio
    async def test_existing_active_task(self, session: AsyncSession) -> None:
        """Assert an existing active task is returned."""
        task = await _create_task(session, name="my-task")

        result = await TaskManager.retrieve_by_name(session, "my-task")

        assert result.id == task.id

    @pytest.mark.asyncio
    async def test_non_existing_name_raises_404(self, session: AsyncSession) -> None:
        """Assert HTTPNotFoundException is raised for a missing task."""
        with pytest.raises(HTTPNotFoundException):
            await TaskManager.retrieve_by_name(session, "no-such-task")

    @pytest.mark.asyncio
    async def test_is_template_filter(self, session: AsyncSession) -> None:
        """Assert is_template filter returns only templates."""
        await _create_task(session, name="regular-task", is_template=False)
        template = await _create_task(session, name="template-task", is_template=True)

        result = await TaskManager.retrieve_by_name(
            session, "template-task", is_template=True
        )
        assert result.id == template.id

        with pytest.raises(HTTPNotFoundException):
            await TaskManager.retrieve_by_name(
                session, "regular-task", is_template=True
            )

    @pytest.mark.asyncio
    async def test_is_active_true_returns_only_active(
        self, session: AsyncSession
    ) -> None:
        """Assert is_active=True returns only non-deleted tasks."""
        task = await _create_task(session, name="alive-task")

        result = await TaskManager.retrieve_by_name(
            session, "alive-task", is_active=True
        )
        assert result.id == task.id

    @pytest.mark.asyncio
    async def test_is_active_false_returns_only_deleted(
        self, session: AsyncSession
    ) -> None:
        """Assert is_active=False returns only deleted tasks."""
        task = await _create_task(session, name="doomed-task")
        deleted = await TaskManager.delete_by_name(session, "doomed-task")

        result = await TaskManager.retrieve_by_name(
            session, deleted.name, is_active=False
        )
        assert result.id == task.id

    @pytest.mark.asyncio
    async def test_is_active_none_returns_both(self, session: AsyncSession) -> None:
        """Assert is_active=None returns both active and deleted tasks."""
        task = await _create_task(session, name="any-task")

        result = await TaskManager.retrieve_by_name(session, "any-task", is_active=None)
        assert result.id == task.id


# ---------------------------------------------------------------------------
# TaskManager.delete_by_name
# ---------------------------------------------------------------------------


class TestTaskManagerDeleteByName:
    """Test TaskManager.delete_by_name."""

    @pytest.mark.asyncio
    async def test_soft_deletes_unprotected_task(self, session: AsyncSession) -> None:
        """Assert an active, unprotected task is soft-deleted."""
        await _create_task(session, name="deletable-task")

        result = await TaskManager.delete_by_name(session, "deletable-task")

        assert result.deleted_at is not None
        assert result.name.startswith("deletable-task-")

    @pytest.mark.asyncio
    async def test_protected_task_raises_403(self, session: AsyncSession) -> None:
        """Assert deleting a protected task raises HTTPForbiddenException."""
        await _create_task(session, name="protected-task", protected=True)

        with pytest.raises(HTTPForbiddenException):
            await TaskManager.delete_by_name(session, "protected-task")

    @pytest.mark.asyncio
    async def test_running_task_raises_409(self, session: AsyncSession) -> None:
        """Assert deleting a task with running history raises HTTPConflictException."""
        task = await _create_task(session, name="running-task")
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.RUNNING)

        with pytest.raises(HTTPConflictException):
            await TaskManager.delete_by_name(session, "running-task")

    @pytest.mark.asyncio
    async def test_pending_task_raises_409(self, session: AsyncSession) -> None:
        """Assert deleting a task with pending history raises HTTPConflictException."""
        task = await _create_task(session, name="pending-task")
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.PENDING)

        with pytest.raises(HTTPConflictException):
            await TaskManager.delete_by_name(session, "pending-task")

    @pytest.mark.asyncio
    async def test_non_existing_task_raises_404(self, session: AsyncSession) -> None:
        """Assert deleting a non-existing task raises HTTPNotFoundException."""
        with pytest.raises(HTTPNotFoundException):
            await TaskManager.delete_by_name(session, "ghost-task")


# ---------------------------------------------------------------------------
# TaskManager.delete_unattached_system_tasks
# ---------------------------------------------------------------------------


class TestTaskManagerDeleteUnattachedSystemTasks:
    """Test TaskManager.delete_unattached_system_tasks."""

    @pytest.mark.asyncio
    async def test_deletes_unattached_protected_tasks(
        self, session: AsyncSession
    ) -> None:
        """Assert unattached protected tasks without history or proxy refs are deleted."""
        unattached = await _create_task(session, name="orphan-system", protected=True)

        await TaskManager.delete_unattached_system_tasks(session, [])

        remaining = await TaskManager.list(session)
        remaining_ids = [t.id for t in remaining]
        assert unattached.id not in remaining_ids

    @pytest.mark.asyncio
    async def test_keeps_tasks_with_history(self, session: AsyncSession) -> None:
        """Assert tasks with history are not deleted."""
        task = await _create_task(session, name="has-history", protected=True)
        await _create_task_history(session, task)

        await TaskManager.delete_unattached_system_tasks(session, [])

        remaining = await TaskManager.list(session)
        assert any(t.id == task.id for t in remaining)

    @pytest.mark.asyncio
    async def test_keeps_tasks_referenced_by_proxy(self, session: AsyncSession) -> None:
        """Assert tasks referenced by a proxy task are not deleted."""
        target = await _create_task(session, name="target-task", protected=True)
        await _create_task(
            session,
            name="proxy-task",
            backend=TaskBackendEnum.PROXY,
            data={"task": "target-task"},
            protected=False,
        )

        await TaskManager.delete_unattached_system_tasks(session, [])

        remaining = await TaskManager.list(session)
        assert any(t.id == target.id for t in remaining)

    @pytest.mark.asyncio
    async def test_keeps_excluded_tasks(self, session: AsyncSession) -> None:
        """Assert tasks in the exclude list are not deleted."""
        task = await _create_task(session, name="excluded-task", protected=True)

        await TaskManager.delete_unattached_system_tasks(session, ["excluded-task"])

        remaining = await TaskManager.list(session)
        assert any(t.id == task.id for t in remaining)

    @pytest.mark.asyncio
    async def test_skips_unprotected_tasks(self, session: AsyncSession) -> None:
        """Assert unprotected tasks are not deleted by this method."""
        task = await _create_task(session, name="unprotected-task", protected=False)

        await TaskManager.delete_unattached_system_tasks(session, [])

        remaining = await TaskManager.list(session)
        assert any(t.id == task.id for t in remaining)


# ---------------------------------------------------------------------------
# TaskManager.get_root_task
# ---------------------------------------------------------------------------


class TestTaskManagerGetRootTask:
    """Test TaskManager.get_root_task."""

    @pytest.mark.asyncio
    async def test_proxy_backend_returns_target_task(
        self, session: AsyncSession
    ) -> None:
        """Assert proxy task resolves to its target task."""
        target = await _create_task(session, name="real-task")
        proxy = await _create_task(
            session,
            name="proxy-ref",
            backend=TaskBackendEnum.PROXY,
            data={"task": "real-task"},
        )

        result = await TaskManager.get_root_task(session, proxy)

        assert result.id == target.id

    @pytest.mark.asyncio
    async def test_nomad_backend_returns_same_task(self, session: AsyncSession) -> None:
        """Assert a NOMAD task returns itself as the root task."""
        task = await _create_task(session, name="nomad-task")

        result = await TaskManager.get_root_task(session, task)

        assert result.id == task.id


# ---------------------------------------------------------------------------
# TaskHistoryManager.list_by_task_name
# ---------------------------------------------------------------------------


class TestTaskHistoryManagerListByTaskName:
    """Test TaskHistoryManager.list_by_task_name."""

    @pytest_asyncio.fixture
    async def task_with_histories(self, session: AsyncSession) -> Task:
        """Create a task with multiple history records.

        :param session: The async database session.
        :type session: AsyncSession
        :return: The created task.
        :rtype: Task
        """
        task = await _create_task(session, name="history-task")
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.SUCCESS)
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.FAILED)
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.RUNNING)
        return task

    @pytest.mark.asyncio
    async def test_returns_histories_for_task(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert all histories for a task are returned."""
        result = await TaskHistoryManager.list_by_task_name(
            session=session, task_name="history-task"
        )

        assert len(result) == HISTORY_FIXTURE_COUNT

    @pytest.mark.asyncio
    async def test_with_status_filter(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert status filter returns only matching histories."""
        result = await TaskHistoryManager.list_by_task_name(
            session=session,
            task_name="history-task",
            status=TaskHistoryStatusEnum.RUNNING,
        )

        assert len(result) == 1
        assert result[0].status == TaskHistoryStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_with_select_related_task(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert select_related_task=True loads the task relationship."""
        result = await TaskHistoryManager.list_by_task_name(
            session=session,
            task_name="history-task",
            select_related_task=True,
        )

        assert len(result) > 0
        assert result[0].task is not None
        assert result[0].task.name == "history-task"

    @pytest.mark.asyncio
    async def test_with_snippet_filename(self, session: AsyncSession) -> None:
        """Assert snippet_filename filter restricts results."""
        task = await _create_task(session, name="snippet-task")
        await _create_task_history(session, task, snippet_filename="config.yaml")
        await _create_task_history(session, task, snippet_filename="other.yaml")

        result = await TaskHistoryManager.list_by_task_name(
            session=session,
            task_name="snippet-task",
            snippet_filename="config.yaml",
        )

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_empty_result(self, session: AsyncSession) -> None:
        """Assert empty list is returned for a task with no history."""
        await _create_task(session, name="no-history-task")

        result = await TaskHistoryManager.list_by_task_name(
            session=session, task_name="no-history-task"
        )

        assert result == []


class TestTaskAndHistoryManagerGetOrdering:
    """Cover spec-derived default ordering used by non-HTTP list callers."""

    def test_task_manager_derives_default_sort_from_spec(self) -> None:
        """Return the Task ``list_query_spec`` default sort via ``_get_ordering``."""
        ordering = list(TaskManager._get_ordering())
        expected = TaskManager.list_query_spec.resolve_sort(None)

        assert len(ordering) == len(expected)
        assert isinstance(ordering[0], NullsLastOrdering)
        assert isinstance(expected[0], NullsLastOrdering)
        assert ordering[0].descending is expected[0].descending
        assert ordering[0].column.compare(expected[0].column)
        assert ordering[1].compare(expected[1])

    def test_task_history_manager_derives_default_sort_from_spec(self) -> None:
        """Return the history ``list_query_spec`` default sort via ``_get_ordering``."""
        ordering = list(TaskHistoryManager._get_ordering())
        expected = TaskHistoryManager.list_query_spec.resolve_sort(None)

        assert len(ordering) == len(expected)
        assert isinstance(ordering[0], NullsLastOrdering)
        assert isinstance(expected[0], NullsLastOrdering)
        assert ordering[0].descending is expected[0].descending
        assert ordering[0].column.compare(expected[0].column)
        assert ordering[1].compare(expected[1])


class TestTaskHistoryManagerListByTaskNameOrdering:
    """Cover ``list_by_task_name`` ordering driven by the shared history spec."""

    @pytest.mark.asyncio
    async def test_orders_by_created_at_descending(self, session: AsyncSession) -> None:
        """Return histories newest-first when ``created_at`` values differ."""
        task = await _create_task(session, name="order-created-at")
        base = datetime(2026, 1, 1, tzinfo=UTC)
        older = await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, created_at=base
        )
        newer = await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.FAILED,
            created_at=base + timedelta(minutes=1),
        )

        result = await TaskHistoryManager.list_by_task_name(
            session=session, task_name="order-created-at"
        )

        assert [row.id for row in result] == [newer.id, older.id]

    @pytest.mark.asyncio
    async def test_tie_breaks_equal_created_at_by_id_ascending(
        self, session: AsyncSession
    ) -> None:
        """Resolve ties on equal ``created_at`` values with ascending unique ``id``."""
        task = await _create_task(session, name="order-tie-break")
        tied_at = datetime(2026, 1, 1, tzinfo=UTC)
        first = await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, created_at=tied_at
        )
        second = await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.FAILED, created_at=tied_at
        )
        third = await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.RUNNING, created_at=tied_at
        )

        result = await TaskHistoryManager.list_by_task_name(
            session=session, task_name="order-tie-break"
        )

        assert [row.id for row in result] == [first.id, second.id, third.id]
        assert first.id is not None
        assert second.id is not None
        assert third.id is not None
        assert first.id < second.id < third.id


class TestTaskManagerListQueryPaginated:
    """Cover TaskManager list-query applier sort, search, and filtered totals."""

    @pytest.mark.asyncio
    async def test_sort_by_name_ascending(self, session: AsyncSession) -> None:
        """Map the public ``name`` sort key to ascending name order."""
        await _create_task(session, name="zeta-task")
        await _create_task(session, name="alpha-task")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(),
            list_query=_list_query(TaskManager, sort="name"),
        )

        assert [task.name for task in result.items] == ["alpha-task", "zeta-task"]

    @pytest.mark.asyncio
    async def test_search_filters_and_reports_filtered_total(
        self, session: AsyncSession
    ) -> None:
        """Filter by name search and report the filtered total, not page size."""
        await _create_task(session, name="match-one", owner="BACKUPS")
        await _create_task(session, name="match-two", owner="BACKUPS")
        await _create_task(session, name="other-task", owner="ALTERS")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(offset=0, limit=1),
            list_query=_list_query(TaskManager, search="match"),
        )

        assert result.total == SEARCH_MATCH_TOTAL
        assert len(result.items) == 1
        assert "match" in result.items[0].name

    @pytest.mark.asyncio
    async def test_search_composes_with_owner_base_restriction(
        self, session: AsyncSession
    ) -> None:
        """Keep owner predicates separate from search and share them for the total."""
        await _create_task(session, name="keep-match", owner="BACKUPS")
        await _create_task(session, name="drop-match", owner="ALTERS")
        await _create_task(session, name="keep-other", owner="BACKUPS")

        result = await TaskManager.list_active_paginated(
            session,
            owner="BACKUPS",
            pagination=Pagination(),
            list_query=_list_query(TaskManager, search="match"),
        )

        assert result.total == 1
        assert result.items[0].name == "keep-match"

    @pytest.mark.asyncio
    async def test_tie_broken_ordering_across_pages(
        self, session: AsyncSession
    ) -> None:
        """Keep page order deterministic when the mapped sort key ties."""
        tied_at = datetime(2026, 1, 1, tzinfo=UTC)
        first = await _create_task(session, name="tie-a")
        second = await _create_task(session, name="tie-b")
        third = await _create_task(session, name="tie-c")
        for task in (first, second, third):
            task.created_at = tied_at
            await TaskManager.save(session, task)

        page_1 = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(offset=0, limit=2),
            list_query=_list_query(TaskManager, sort="-created_at"),
        )
        page_2 = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(offset=2, limit=2),
            list_query=_list_query(TaskManager, sort="-created_at"),
        )

        assert [task.id for task in page_1.items] == [first.id, second.id]
        assert [task.id for task in page_2.items] == [third.id]
        assert page_1.total == page_2.total == TIE_BREAK_TOTAL


class TestTaskHistoryManagerListQueryPaginated:
    """Cover TaskHistoryManager list-query applier search and filtered totals."""

    @pytest.mark.asyncio
    async def test_search_filters_executed_by_and_reports_filtered_total(
        self, session: AsyncSession
    ) -> None:
        """Filter histories by executed_by ILIKE and report the filtered total."""
        task = await _create_task(session, name="search-history-task")
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, executed_by="alice"
        )
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.FAILED, executed_by="alice-ops"
        )
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.RUNNING, executed_by="bob"
        )

        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="search-history-task",
            pagination=Pagination(offset=0, limit=1),
            list_query=_list_query(TaskHistoryManager, search="alice"),
        )

        assert result.total == SEARCH_MATCH_TOTAL
        assert len(result.items) == 1
        assert "alice" in (result.items[0].executed_by or "")

    @pytest.mark.asyncio
    async def test_search_composes_with_status_base_restriction(
        self, session: AsyncSession
    ) -> None:
        """Keep status predicates separate from search on the shared history surface."""
        task = await _create_task(session, name="status-search-task")
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, executed_by="alice"
        )
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.FAILED, executed_by="alice"
        )

        result = await TaskHistoryManager.list_all_history_paginated(
            session,
            pagination=Pagination(),
            list_query=_list_query(TaskHistoryManager, search="alice"),
            status=TaskHistoryStatusEnum.SUCCESS,
        )

        assert result.total == 1
        assert result.items[0].status == TaskHistoryStatusEnum.SUCCESS


# ---------------------------------------------------------------------------
# TaskManager.list_active_paginated
# ---------------------------------------------------------------------------


class TestTaskManagerListActivePaginated:
    """Test TaskManager.list_active_paginated."""

    @pytest.mark.asyncio
    async def test_returns_paginated_response(self, session: AsyncSession) -> None:
        """Assert paginated response contains items, total, offset, and limit."""
        await _create_task(session, name="pag-task-1")
        await _create_task(session, name="pag-task-2")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == PAGINATED_TASK_COUNT
        assert result.offset == DEFAULT_PAGINATION_OFFSET
        assert result.limit == DEFAULT_PAGINATION_LIMIT
        assert len(result.items) == PAGINATED_TASK_COUNT

    @pytest.mark.asyncio
    async def test_with_custom_offset_and_limit(self, session: AsyncSession) -> None:
        """Assert custom offset and limit restrict returned items."""
        for i in range(3):
            await _create_task(session, name=f"pag-custom-{i}")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(offset=0, limit=1),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == PAGINATED_CUSTOM_TASK_COUNT
        assert len(result.items) == 1
        assert result.limit == 1

    @pytest.mark.asyncio
    async def test_with_owner_filter(self, session: AsyncSession) -> None:
        """Assert owner filter works with pagination."""
        await _create_task(session, name="pag-backup", owner="BACKUPS")
        await _create_task(session, name="pag-alter", owner="ALTERS")

        result = await TaskManager.list_active_paginated(
            session,
            owner="BACKUPS",
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 1
        assert result.items[0].name == "pag-backup"

    @pytest.mark.asyncio
    async def test_excludes_deleted_tasks(self, session: AsyncSession) -> None:
        """Assert deleted tasks are excluded from paginated results."""
        await _create_task(session, name="pag-active")
        await _create_task(session, name="pag-deleted")
        await TaskManager.delete_by_name(session, "pag-deleted")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 1
        assert result.items[0].name == "pag-active"

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero_total(self, session: AsyncSession) -> None:
        """Assert empty database returns total of zero."""
        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 0
        assert result.items == []

    @pytest.mark.asyncio
    async def test_offset_beyond_total(self, session: AsyncSession) -> None:
        """Assert offset beyond total returns empty items with correct total."""
        await _create_task(session, name="pag-beyond")

        result = await TaskManager.list_active_paginated(
            session,
            pagination=Pagination(offset=999),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 1
        assert result.items == []

    @pytest.mark.asyncio
    async def test_with_parent_is_null_filter(self, session: AsyncSession) -> None:
        """Assert parent_is_null=True excludes tasks with a parent reference."""
        await _create_task(
            session,
            name="pag-parent",
            data={"backup_type": PBM_CONFIG_BACKUP_TYPE},
        )
        await _create_task(
            session,
            name="pag-child",
            data={
                "backup_type": "pbm_logical",
                "parent": "pag-parent",
            },
        )

        null_parents = await TaskManager.list_active_paginated(
            session,
            parent_is_null=True,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )
        non_null_parents = await TaskManager.list_active_paginated(
            session,
            parent_is_null=False,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert null_parents.total == 1
        assert null_parents.items[0].name == "pag-parent"
        assert non_null_parents.total == 1
        assert non_null_parents.items[0].name == "pag-child"

    @pytest.mark.asyncio
    async def test_with_backup_type_filter(self, session: AsyncSession) -> None:
        """Assert backup_type filters on data.backup_type as string."""
        await _create_task(
            session,
            name="pag-pbm-config",
            data={"backup_type": PBM_CONFIG_BACKUP_TYPE},
        )
        await _create_task(
            session,
            name="pag-pbm-logical",
            data={"backup_type": "pbm_logical", "parent": "pag-pbm-config"},
        )

        result = await TaskManager.list_active_paginated(
            session,
            backup_type=PBM_CONFIG_BACKUP_TYPE,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 1
        assert result.items[0].name == "pag-pbm-config"

    @pytest.mark.asyncio
    async def test_with_self_parent_filter(self, session: AsyncSession) -> None:
        """Assert self_parent=True keeps only rows where data.parent == task.name."""
        await _create_task(
            session,
            name="pag-self-parent",
            data={"backup_type": "pbm_logical", "parent": "pag-self-parent"},
        )
        await _create_task(
            session,
            name="pag-config-parent",
            data={"backup_type": PBM_CONFIG_BACKUP_TYPE},
        )
        await _create_task(
            session,
            name="pag-child",
            data={"backup_type": "pbm_logical", "parent": "pag-config-parent"},
        )

        result = await TaskManager.list_active_paginated(
            session,
            self_parent=True,
            pagination=Pagination(),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == 1
        assert result.items[0].name == "pag-self-parent"

    @pytest.mark.asyncio
    async def test_parent_and_backup_type_filters_with_pagination(
        self, session: AsyncSession
    ) -> None:
        """Assert combined JSON filters paginate over the filtered set."""
        for index in range(PARENT_FILTER_TASK_COUNT):
            await _create_task(
                session,
                name=f"pag-filter-parent-{index}",
                data={"backup_type": PBM_CONFIG_BACKUP_TYPE},
            )
        await _create_task(
            session,
            name="pag-filter-child",
            data={
                "backup_type": "pbm_logical",
                "parent": "pag-filter-parent-0",
            },
        )

        result = await TaskManager.list_active_paginated(
            session,
            parent_is_null=True,
            backup_type=PBM_CONFIG_BACKUP_TYPE,
            pagination=Pagination(offset=1, limit=1),
            list_query=_default_list_query(TaskManager),
        )

        assert result.total == PARENT_FILTER_TASK_COUNT
        assert result.offset == 1
        assert result.limit == 1
        assert len(result.items) == 1
        assert result.items[0].name in {
            "pag-filter-parent-0",
            "pag-filter-parent-1",
            "pag-filter-parent-2",
        }


# ---------------------------------------------------------------------------
# TaskHistoryManager.latest_status_by_task_names
# ---------------------------------------------------------------------------


class TestTaskHistoryManagerLatestStatusByTaskNames:
    """Test TaskHistoryManager.latest_status_by_task_names."""

    @pytest.mark.asyncio
    async def test_empty_names_returns_empty_mapping(
        self, session: AsyncSession
    ) -> None:
        """Assert an empty request yields an empty mapping."""
        result = await TaskHistoryManager.latest_status_by_task_names(session, [])

        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_latest_status_per_task(self, session: AsyncSession) -> None:
        """Assert newest non-null history status is returned for each task."""
        suffix = uuid4().hex[:8]
        task_a_name = f"latest-status-a-{suffix}"
        task_b_name = f"latest-status-b-{suffix}"
        missing_name = f"latest-status-missing-{suffix}"

        task_a = await _create_task(session, name=task_a_name)
        task_b = await _create_task(session, name=task_b_name)
        await _create_task_history(
            session, task_a, status=TaskHistoryStatusEnum.SUCCESS
        )
        await _create_task_history(
            session, task_a, status=TaskHistoryStatusEnum.RUNNING
        )
        await _create_task_history(session, task_b, status=TaskHistoryStatusEnum.FAILED)

        result = await TaskHistoryManager.latest_status_by_task_names(
            session,
            [task_a_name, task_b_name, missing_name],
        )

        assert result[task_a_name].status == TaskHistoryStatusEnum.RUNNING
        assert result[task_b_name].status == TaskHistoryStatusEnum.FAILED
        assert result[missing_name] is None

    @pytest.mark.asyncio
    async def test_deduplicates_duplicate_names(self, session: AsyncSession) -> None:
        """Assert duplicate names are resolved once while preserving order."""
        task = await _create_task(session, name="latest-status-dedupe")
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.SUCCESS)

        result = await TaskHistoryManager.latest_status_by_task_names(
            session,
            ["latest-status-dedupe", "latest-status-dedupe"],
        )

        assert list(result.keys()) == ["latest-status-dedupe"]
        assert result["latest-status-dedupe"].status == TaskHistoryStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_latest_status_from_history_statuses_skips_nulls(self) -> None:
        """Assert null statuses are skipped when scanning newest-to-oldest."""
        result = TaskHistoryManager._latest_status_from_history_statuses(
            [None, TaskHistoryStatusEnum.SUCCESS, TaskHistoryStatusEnum.FAILED]
        )

        assert result == TaskHistoryStatusEnum.SUCCESS

    @pytest.mark.asyncio
    async def test_finished_at_is_max_across_rows(self, session: AsyncSession) -> None:
        """Assert finished_at is the max across rows while status is the newest.

        Models the in-progress re-run case: an earlier SUCCESS/FAILED pair has
        real finish times and the newest row is RUNNING with no finish time, so
        the projection reports RUNNING status but still the prior (max) finish.
        """
        task = await _create_task(session, name="latest-mixed")
        early = utc_now() - timedelta(hours=2)
        later = utc_now() - timedelta(hours=1)
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, finished_at=early
        )
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.FAILED, finished_at=later
        )
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.RUNNING, finished_at=None
        )

        result = await TaskHistoryManager.latest_status_by_task_names(
            session, [task.name]
        )

        latest = result[task.name]
        assert latest is not None
        assert latest.status == TaskHistoryStatusEnum.RUNNING
        assert latest.finished_at == later

    @pytest.mark.asyncio
    async def test_only_running_never_finished_has_no_finish(
        self, session: AsyncSession
    ) -> None:
        """Assert a single unfinished RUNNING row yields status but no finish."""
        task = await _create_task(session, name="latest-running")
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.RUNNING, finished_at=None
        )

        result = await TaskHistoryManager.latest_status_by_task_names(
            session, [task.name]
        )

        latest = result[task.name]
        assert latest is not None
        assert latest.status == TaskHistoryStatusEnum.RUNNING
        assert latest.finished_at is None

    @pytest.mark.asyncio
    async def test_failed_run_reports_finish(self, session: AsyncSession) -> None:
        """Assert a FAILED run still reports its finish time (it did run)."""
        task = await _create_task(session, name="latest-failed")
        finished = utc_now() - timedelta(minutes=30)
        await _create_task_history(
            session, task, status=TaskHistoryStatusEnum.FAILED, finished_at=finished
        )

        result = await TaskHistoryManager.latest_status_by_task_names(
            session, [task.name]
        )

        latest = result[task.name]
        assert latest is not None
        assert latest.status == TaskHistoryStatusEnum.FAILED
        assert latest.finished_at == finished

    @pytest.mark.asyncio
    async def test_no_executor_filter_returns_newest_regardless(
        self, session: AsyncSession
    ) -> None:
        """Assert the latest-status lookup still returns the newest row's status.

        Guards the existing ``POST /history/latest`` consumer, which reports the
        newest run irrespective of executor.
        """
        task = await _create_task(session, name="latest-executor-default")
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
        )
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.RUNNING,
            executed_by="test-user",
        )

        result = await TaskHistoryManager.latest_status_by_task_names(
            session, [task.name]
        )

        latest = result[task.name]
        assert latest is not None
        assert latest.status == TaskHistoryStatusEnum.RUNNING


class TestTaskHistoryManagerRecentSystemStatusPoints:
    """Test TaskHistoryManager.recent_system_status_points_by_task_names."""

    @pytest.mark.asyncio
    async def test_returns_system_points_oldest_first(
        self, session: AsyncSession
    ) -> None:
        """Assert system-executed points are grouped by name, oldest first."""
        task = await _create_task(session, name="points-order")
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
        )
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.FAILED,
            executed_by=SYSTEM_USER,
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session, {task.name: utc_now() - timedelta(days=1)}
        )

        assert [point.status for point in points[task.name]] == [
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.FAILED,
        ]

    @pytest.mark.asyncio
    async def test_excludes_non_system_rows(self, session: AsyncSession) -> None:
        """Assert manual (non-system) runs are excluded from the points."""
        task = await _create_task(session, name="points-non-system")
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
        )
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.RUNNING,
            executed_by="test-user",
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session, {task.name: utc_now() - timedelta(days=1)}
        )

        assert [point.status for point in points[task.name]] == [
            TaskHistoryStatusEnum.SUCCESS
        ]

    @pytest.mark.asyncio
    async def test_name_absent_when_only_non_system(
        self, session: AsyncSession
    ) -> None:
        """Assert a name with only manual runs is absent from the mapping."""
        task = await _create_task(session, name="points-only-manual")
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by="test-user",
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session, {task.name: utc_now() - timedelta(days=1)}
        )

        assert task.name not in points

    @pytest.mark.asyncio
    async def test_empty_thresholds_returns_empty(self, session: AsyncSession) -> None:
        """Assert an empty threshold map short-circuits to an empty mapping."""
        assert (
            await TaskHistoryManager.recent_system_status_points_by_task_names(
                session, {}
            )
            == {}
        )

    @pytest.mark.asyncio
    async def test_groups_points_by_name(self, session: AsyncSession) -> None:
        """Assert points are partitioned per task name, not mixed across names."""
        first = await _create_task(session, name="points-first")
        second = await _create_task(session, name="points-second")
        await _create_task_history(
            session,
            first,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
        )
        await _create_task_history(
            session,
            second,
            status=TaskHistoryStatusEnum.FAILED,
            executed_by=SYSTEM_USER,
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session,
            {
                first.name: utc_now() - timedelta(days=1),
                second.name: utc_now() - timedelta(days=1),
            },
        )

        assert [point.status for point in points[first.name]] == [
            TaskHistoryStatusEnum.SUCCESS
        ]
        assert [point.status for point in points[second.name]] == [
            TaskHistoryStatusEnum.FAILED
        ]

    @pytest.mark.asyncio
    async def test_excludes_points_before_cutoff(self, session: AsyncSession) -> None:
        """Assert rows older than a name's cutoff are excluded."""
        task = await _create_task(session, name="points-cutoff")
        await _create_task_history(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session, {task.name: utc_now() + timedelta(days=1)}
        )

        assert task.name not in points

    @pytest.mark.asyncio
    async def test_returns_all_points_at_or_after_cutoff(
        self, session: AsyncSession
    ) -> None:
        """Assert the query is time-bound, not capped at a fixed row count."""
        task = await _create_task(session, name="points-uncapped")
        row_count = 55
        for _ in range(row_count):
            await _create_task_history(
                session,
                task,
                status=TaskHistoryStatusEnum.SUCCESS,
                executed_by=SYSTEM_USER,
            )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session, {task.name: utc_now() - timedelta(days=1)}
        )

        assert len(points[task.name]) == row_count

    @pytest.mark.asyncio
    async def test_cutoff_is_per_name(self, session: AsyncSession) -> None:
        """Assert each name is filtered by its own cutoff, not a shared bound."""
        included = await _create_task(session, name="points-cutoff-included")
        excluded = await _create_task(session, name="points-cutoff-excluded")
        base = utc_now()
        await _create_task_history(
            session,
            included,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
            created_at=base,
        )
        await _create_task_history(
            session,
            excluded,
            status=TaskHistoryStatusEnum.SUCCESS,
            executed_by=SYSTEM_USER,
            created_at=base,
        )

        points = await TaskHistoryManager.recent_system_status_points_by_task_names(
            session,
            {
                included.name: base - timedelta(minutes=5),
                excluded.name: base + timedelta(minutes=5),
            },
        )

        assert included.name in points
        assert excluded.name not in points


# ---------------------------------------------------------------------------
# TaskHistoryManager.list_by_task_name_paginated
# ---------------------------------------------------------------------------


class TestTaskHistoryManagerListByTaskNamePaginated:
    """Test TaskHistoryManager.list_by_task_name_paginated."""

    @pytest_asyncio.fixture
    async def task_with_histories(self, session: AsyncSession) -> Task:
        """Create a task with multiple history records.

        :param session: The async database session.
        :type session: AsyncSession
        :return: The created task.
        :rtype: Task
        """
        task = await _create_task(session, name="pag-history-task")
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.SUCCESS)
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.FAILED)
        await _create_task_history(session, task, status=TaskHistoryStatusEnum.RUNNING)
        return task

    @pytest.mark.asyncio
    async def test_returns_paginated_response(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert paginated response has correct envelope fields."""
        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-history-task",
            pagination=Pagination(),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == HISTORY_FIXTURE_COUNT
        assert result.offset == DEFAULT_PAGINATION_OFFSET
        assert result.limit == DEFAULT_PAGINATION_LIMIT
        assert len(result.items) == HISTORY_FIXTURE_COUNT

    @pytest.mark.asyncio
    async def test_with_custom_limit(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert custom limit restricts returned items."""
        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-history-task",
            pagination=Pagination(limit=1),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == HISTORY_FIXTURE_COUNT
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_with_status_filter(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert status filter restricts paginated results."""
        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-history-task",
            status=TaskHistoryStatusEnum.RUNNING,
            pagination=Pagination(),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].status == TaskHistoryStatusEnum.RUNNING

    @pytest.mark.asyncio
    async def test_empty_result(self, session: AsyncSession) -> None:
        """Assert empty paginated response for task with no history."""
        await _create_task(session, name="pag-empty-task")

        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-empty-task",
            pagination=Pagination(),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == 0
        assert result.items == []

    @pytest.mark.asyncio
    async def test_offset_beyond_total(
        self, session: AsyncSession, task_with_histories: Task
    ) -> None:
        """Assert offset beyond total returns empty items with correct total."""
        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-history-task",
            pagination=Pagination(offset=999),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == HISTORY_FIXTURE_COUNT
        assert result.items == []

    @pytest.mark.asyncio
    async def test_with_snippet_filename(self, session: AsyncSession) -> None:
        """Assert snippet_filename filter works with pagination."""
        task = await _create_task(session, name="pag-snippet-task")
        await _create_task_history(session, task, snippet_filename="config.yaml")
        await _create_task_history(session, task, snippet_filename="other.yaml")

        result = await TaskHistoryManager.list_by_task_name_paginated(
            session=session,
            task_name="pag-snippet-task",
            snippet_filename="config.yaml",
            pagination=Pagination(),
            list_query=_default_list_query(TaskHistoryManager),
        )

        assert result.total == 1
        assert len(result.items) == 1


# ---------------------------------------------------------------------------
# DispatchLockManager
# ---------------------------------------------------------------------------


class TestDispatchLockManager:
    """Test DispatchLockManager."""

    @pytest.mark.asyncio
    async def test_create_lock(self, session: AsyncSession) -> None:
        """Assert a dispatch lock can be created."""
        lock = DispatchLock(name="my-lock")
        saved = await DispatchLockManager.save(session, lock)

        assert saved.id is not None
        assert saved.name == "my-lock"

    @pytest.mark.asyncio
    async def test_duplicate_lock_raises_conflict(self, session: AsyncSession) -> None:
        """Assert creating a duplicate lock name raises HTTPConflictException."""
        lock1 = DispatchLock(name="unique-lock")
        await DispatchLockManager.save(session, lock1)

        lock2 = DispatchLock(name="unique-lock")
        with pytest.raises(HTTPConflictException):
            await DispatchLockManager.save(session, lock2)

    @pytest.mark.asyncio
    async def test_claim_commits_the_row_without_reading_it_back(
        self, session: AsyncSession
    ) -> None:
        """Assert ``claim`` persists the lock and takes no post-commit refresh.

        ``save``'s ``session.refresh`` is a second pool checkout taken once the
        row is committed, so under exhaustion it raises after the lock exists and
        before its owner can release it. ``claim`` exists to avoid that, and the
        row still has to be durably there afterwards or it locks nothing.
        """
        with patch.object(
            session, "refresh", new=AsyncMock(wraps=session.refresh)
        ) as refresh:
            await DispatchLockManager.claim(session, "claimed-lock")

        refresh.assert_not_awaited()
        assert [lock.name for lock in await DispatchLockManager.list(session)] == [
            "claimed-lock"
        ]

    @pytest.mark.asyncio
    async def test_claim_raises_integrity_error_on_a_held_lock(
        self, session: AsyncSession
    ) -> None:
        """Assert a second claim surfaces the raw IntegrityError, usable after.

        ``_dispatch_queue_item`` translates it into its own 409, so ``claim`` must
        not translate it first. The session has to be usable afterwards too: the
        failed statement leaves the transaction unusable until it is rolled back.
        """
        await DispatchLockManager.claim(session, "contended-lock")

        with pytest.raises(IntegrityError):
            await DispatchLockManager.claim(session, "contended-lock")

        assert len(await DispatchLockManager.list(session)) == 1

    @pytest.mark.asyncio
    async def test_release_deletes_the_claimed_row(self, session: AsyncSession) -> None:
        """Assert ``release`` clears the lock without a persisted instance."""
        token = await DispatchLockManager.claim(session, "released-lock")

        await DispatchLockManager.release(session, "released-lock", token)

        assert await DispatchLockManager.list(session) == []

    @pytest.mark.asyncio
    async def test_release_is_quiet_about_a_lock_that_is_already_gone(
        self, session: AsyncSession
    ) -> None:
        """Assert releasing a swept lock is not an error.

        The stale-row sweep may have reclaimed it already, and the caller runs
        this in a ``finally`` where raising would fail a completed dispatch.
        """
        await DispatchLockManager.release(session, "never-existed", utc_now())

        assert await DispatchLockManager.list(session) == []

    @pytest.mark.asyncio
    async def test_release_leaves_a_later_dispatchs_row_alone(
        self, session: AsyncSession
    ) -> None:
        """Assert an overrunning dispatch's release cannot free a live lock.

        Keyed by name alone this is a double-dispatch window, which is what the
        table exists to close. Walking it: A claims and overruns the stale
        window, B's sweep reclaims A's row and B claims the same name, then A
        finishes and releases. A's release must not take B's row, or C could
        claim while B is still dispatching.
        """
        stale_after = timedelta(seconds=30)
        token_a = utc_now() - 2 * stale_after
        session.add(DispatchLock(name="overrun", created_at=token_a))
        await session.commit()

        await DispatchLockManager.delete_where(
            session,
            col(DispatchLock.created_at) < (utc_now() - stale_after),
            name="overrun",
        )
        token_b = await DispatchLockManager.claim(session, "overrun")

        await DispatchLockManager.release(session, "overrun", token_a)

        # B holds the lock still, so a third dispatch of the same name cannot
        # take it. ``claim`` rolls the failed statement back, leaving the session
        # usable for the release below.
        with pytest.raises(IntegrityError):
            await DispatchLockManager.claim(session, "overrun")
        # And the row that survived is B's, not some residue of A's: B's own
        # token is what clears it.
        await DispatchLockManager.release(session, "overrun", token_b)
        assert await DispatchLockManager.list(session) == []

    @pytest.mark.asyncio
    async def test_delete_lock(self, session: AsyncSession) -> None:
        """Assert a dispatch lock can be deleted."""
        lock = DispatchLock(name="temp-lock")
        saved = await DispatchLockManager.save(session, lock)

        await DispatchLockManager.delete(session, saved)

        remaining = await DispatchLockManager.list(session)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# TaskHistoryLogManager.ids_with_chunks
# ---------------------------------------------------------------------------


async def _seed_task_history(
    session: AsyncSession, task: Task, name: str
) -> TaskHistory:
    """Persist and return a SUCCESS task history for ``task``.

    Helper for ``ids_with_chunks`` tests: each chunk-store row requires a
    real ``TaskHistory`` FK.
    """
    history = TaskHistory(
        task_id=task.id,
        status=TaskHistoryStatusEnum.SUCCESS,
        execution_request={
            "task": task.name,
            "target": name,
            "meta": {},
            "tracking": {"allocation_id": None, "evaluation_id": None},
        },
    )
    return await TaskHistoryManager.save(session, history)


async def _seed_chunk(
    session: AsyncSession, task_history_id: int, payload: bytes = b"hello"
) -> None:
    """Write a single chunk-store row for ``task_history_id`` via the writer."""
    await TaskHistoryLogWriter.append(
        session,
        task_history_id,
        source="run-script",
        stream=TaskLogType.STDOUT,
        new_bytes=payload,
        force_flush=True,
        producer_offset_after=len(payload),
    )


class TestTaskHistoryLogManagerIdsWithChunks:
    """Test ``TaskHistoryLogManager.ids_with_chunks``."""

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_set(self, session: AsyncSession) -> None:
        """Assert an empty input returns ``set()`` without querying the DB."""
        result = await TaskHistoryLogManager.ids_with_chunks(session, [])

        assert result == set()

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty_set(self, session: AsyncSession) -> None:
        """Assert IDs with no chunks in the store come back as an empty set."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")

        result = await TaskHistoryLogManager.ids_with_chunks(session, [history.id])

        assert result == set()

    @pytest.mark.asyncio
    async def test_returns_only_ids_with_chunks(self, session: AsyncSession) -> None:
        """Assert the subset with chunks is returned for a mixed input."""
        task = await _create_task(session)
        with_chunks_a = await _seed_task_history(session, task, "node-a")
        with_chunks_b = await _seed_task_history(session, task, "node-b")
        without_chunks = await _seed_task_history(session, task, "node-c")
        await _seed_chunk(session, with_chunks_a.id)
        await _seed_chunk(session, with_chunks_b.id)

        result = await TaskHistoryLogManager.ids_with_chunks(
            session,
            [with_chunks_a.id, with_chunks_b.id, without_chunks.id],
        )

        assert result == {with_chunks_a.id, with_chunks_b.id}

    @pytest.mark.asyncio
    async def test_deduplicates_multiple_chunk_rows_per_history(
        self, session: AsyncSession
    ) -> None:
        """Assert a history with multiple chunks is reported once (DISTINCT)."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_chunk(session, history.id, payload=b"first")
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDERR,
            new_bytes=b"second",
            force_flush=True,
            producer_offset_after=6,
        )

        result = await TaskHistoryLogManager.ids_with_chunks(session, [history.id])

        assert result == {history.id}


async def _seed_state_row(
    session: AsyncSession,
    task_history_id: int,
    *,
    source: str,
    capture_status: LogCaptureStatusEnum,
    stream: TaskLogType = TaskLogType.STDOUT,
) -> None:
    """Persist one state row carrying an explicit capture verdict."""
    state = TaskHistoryLogStateManager.build_default(task_history_id, source, stream)
    state.capture_status = capture_status
    session.add(state)
    await session.commit()


class TestTaskHistoryLogStateManagerCaptureStatusByTask:
    """Cover ``TaskHistoryLogStateManager.capture_status_by_task``."""

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_mapping(
        self, session: AsyncSession, mocker
    ) -> None:
        """Assert an empty input returns ``{}`` without emitting any SQL.

        An empty ``IN ()`` predicate triggers a SQLAlchemy warning and matches
        nothing anyway, so the early return is what keeps a page of zero rows
        from touching the database.
        """
        spy = mocker.spy(TaskHistoryLogStateManager, "_exec")

        result = await TaskHistoryLogStateManager.capture_status_by_task(session, [])

        assert result == {}
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_without_state_rows_is_absent(
        self, session: AsyncSession
    ) -> None:
        """Assert a history with no state rows yields no entry.

        The caller reads a missing key as ``unknown``; materialising a verdict
        here would assert evidence that does not exist.
        """
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_all_complete_streams_aggregate_to_complete(
        self, session: AsyncSession
    ) -> None:
        """Assert a history whose every stream completed reports ``complete``."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_state_row(
            session,
            history.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )
        await _seed_state_row(
            session,
            history.id,
            source="clean-up",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {history.id: LogCaptureStatusEnum.COMPLETE}

    @pytest.mark.asyncio
    async def test_any_incomplete_stream_dominates(self, session: AsyncSession) -> None:
        """Assert one incomplete stream outranks complete and unknown siblings."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_state_row(
            session,
            history.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )
        await _seed_state_row(
            session,
            history.id,
            source="prepare-env",
            capture_status=LogCaptureStatusEnum.UNKNOWN,
        )
        await _seed_state_row(
            session,
            history.id,
            source="clean-up",
            capture_status=LogCaptureStatusEnum.INCOMPLETE,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {history.id: LogCaptureStatusEnum.INCOMPLETE}

    @pytest.mark.asyncio
    async def test_unknown_outranks_complete(self, session: AsyncSession) -> None:
        """Assert an unknown stream beats a complete one absent any incomplete."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_state_row(
            session,
            history.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )
        await _seed_state_row(
            session,
            history.id,
            source="clean-up",
            capture_status=LogCaptureStatusEnum.UNKNOWN,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {history.id: LogCaptureStatusEnum.UNKNOWN}

    @pytest.mark.asyncio
    async def test_hold_step_rows_are_excluded_from_the_aggregate(
        self, session: AsyncSession
    ) -> None:
        """Assert the hold step's own row cannot drag the verdict down.

        The hold produces no task output and is never drained, so its row stays
        ``INCOMPLETE`` for the whole allocation; counting it would report every
        held task incomplete.
        """
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_state_row(
            session,
            history.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )
        await _seed_state_row(
            session,
            history.id,
            source=NomadStep.LOG_CAPTURE_HOLD,
            capture_status=LogCaptureStatusEnum.INCOMPLETE,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {history.id: LogCaptureStatusEnum.COMPLETE}

    @pytest.mark.asyncio
    async def test_celery_execution_source_is_counted(
        self, session: AsyncSession
    ) -> None:
        """Assert a non-Nomad ``source`` still contributes to the aggregate.

        The Celery executor writes ``source="execution"``, which is not a
        ``NomadStep`` at all — filtering by the Nomad step names rather than by
        excluding the hold would drop every Celery-backed task to ``unknown``.
        """
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_state_row(
            session,
            history.id,
            source="execution",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [history.id]
        )

        assert result == {history.id: LogCaptureStatusEnum.COMPLETE}

    @pytest.mark.asyncio
    async def test_aggregates_each_history_independently(
        self, session: AsyncSession
    ) -> None:
        """Assert a batched call keeps per-history verdicts separate."""
        task = await _create_task(session)
        complete = await _seed_task_history(session, task, "node-a")
        incomplete = await _seed_task_history(session, task, "node-b")
        bare = await _seed_task_history(session, task, "node-c")
        await _seed_state_row(
            session,
            complete.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.COMPLETE,
        )
        await _seed_state_row(
            session,
            incomplete.id,
            source="run-script",
            capture_status=LogCaptureStatusEnum.INCOMPLETE,
        )

        result = await TaskHistoryLogStateManager.capture_status_by_task(
            session, [complete.id, incomplete.id, bare.id]
        )

        assert result == {
            complete.id: LogCaptureStatusEnum.COMPLETE,
            incomplete.id: LogCaptureStatusEnum.INCOMPLETE,
        }


# ---------------------------------------------------------------------------
# TaskHistoryLogManager.list_stream_keys / iter_chunks_reverse
# ---------------------------------------------------------------------------


class TestTaskHistoryLogManagerStreamKeysAndReverse:
    """Test ``list_stream_keys`` and ``iter_chunks_reverse``."""

    @pytest.mark.asyncio
    async def test_list_stream_keys_returns_distinct_pairs(
        self, session: AsyncSession
    ) -> None:
        """Assert distinct ``(source, stream)`` pairs are returned in order."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_chunk(session, history.id, payload=b"stdout-a")
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDERR,
            new_bytes=b"stderr-a",
            force_flush=True,
            producer_offset_after=8,
        )
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="prepare",
            stream=TaskLogType.STDOUT,
            new_bytes=b"stdout-b",
            force_flush=True,
            producer_offset_after=8,
        )

        keys = await TaskHistoryLogManager.list_stream_keys(session, history.id)

        assert keys == [
            ("prepare", TaskLogType.STDOUT),
            ("run-script", TaskLogType.STDERR),
            ("run-script", TaskLogType.STDOUT),
        ]

    @pytest.mark.asyncio
    async def test_list_stream_keys_source_filter(self, session: AsyncSession) -> None:
        """Assert ``source`` limits pairs to the matching step."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_chunk(session, history.id, payload=b"stdout-a")
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="prepare",
            stream=TaskLogType.STDOUT,
            new_bytes=b"stdout-b",
            force_flush=True,
            producer_offset_after=8,
        )

        keys = await TaskHistoryLogManager.list_stream_keys(
            session, history.id, source="run-script"
        )

        assert keys == [("run-script", TaskLogType.STDOUT)]

    @pytest.mark.asyncio
    async def test_list_stream_keys_empty_when_no_chunks(
        self, session: AsyncSession
    ) -> None:
        """Assert an empty list is returned when no chunk rows exist."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")

        keys = await TaskHistoryLogManager.list_stream_keys(session, history.id)

        assert keys == []

    @pytest.mark.asyncio
    async def test_iter_chunks_reverse_yields_newest_first_per_stream(
        self, session: AsyncSession
    ) -> None:
        """Assert chunks are yielded in descending ``start_offset`` order."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"first",
            force_flush=True,
            producer_offset_after=5,
        )
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            new_bytes=b"second",
            force_flush=True,
            producer_offset_after=11,
        )
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDERR,
            new_bytes=b"err",
            force_flush=True,
            producer_offset_after=3,
        )

        chunks = [
            chunk
            async for chunk in TaskHistoryLogManager.iter_chunks_reverse(
                session,
                history.id,
                source="run-script",
                stream=TaskLogType.STDOUT,
            )
        ]

        assert [chunk.content for chunk in chunks] == ["second", "first"]
        assert [chunk.start_offset for chunk in chunks] == [5, 0]

    @pytest.mark.asyncio
    async def test_iter_chunks_reverse_orders_streams_before_offsets(
        self, session: AsyncSession
    ) -> None:
        """Assert global ordering is ``(source, stream, start_offset DESC)``."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_chunk(session, history.id, payload=b"stdout")
        await TaskHistoryLogWriter.append(
            session,
            history.id,
            source="run-script",
            stream=TaskLogType.STDERR,
            new_bytes=b"stderr",
            force_flush=True,
            producer_offset_after=6,
        )

        chunks = [
            chunk
            async for chunk in TaskHistoryLogManager.iter_chunks_reverse(
                session, history.id
            )
        ]

        assert [(chunk.source, chunk.stream, chunk.content) for chunk in chunks] == [
            ("run-script", TaskLogType.STDERR, "stderr"),
            ("run-script", TaskLogType.STDOUT, "stdout"),
        ]


class TestTaskHistoryLogManagerStderrTailChunks:
    """Test ``TaskHistoryLogManager.get_stderr_tail_chunks``."""

    async def _append_stderr(
        self,
        session: AsyncSession,
        task_history_id: int,
        payload: bytes,
        producer_offset_after: int,
    ) -> None:
        """Append a STDERR chunk via the writer."""
        await TaskHistoryLogWriter.append(
            session,
            task_history_id,
            source="run-script",
            stream=TaskLogType.STDERR,
            new_bytes=payload,
            force_flush=True,
            producer_offset_after=producer_offset_after,
        )

    @pytest.mark.asyncio
    async def test_returns_chunk_contents_newest_first(
        self, session: AsyncSession
    ) -> None:
        """Return STDERR chunk contents ordered newest-first by insertion id.

        Callers reconstruct chronological order by reversing; the manager only
        guarantees newest-first ordering of the raw chunk contents.
        """
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await self._append_stderr(session, history.id, b"first error", 11)
        await self._append_stderr(session, history.id, b"last error", 21)

        chunks = await TaskHistoryLogManager.get_stderr_tail_chunks(session, history.id)
        assert chunks == ["last error", "first error"]

    @pytest.mark.asyncio
    async def test_respects_limit(self, session: AsyncSession) -> None:
        """Return only the newest ``limit`` STDERR chunks."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await self._append_stderr(session, history.id, b"c1", 2)
        await self._append_stderr(session, history.id, b"c2", 4)
        await self._append_stderr(session, history.id, b"c3", 6)

        chunks = await TaskHistoryLogManager.get_stderr_tail_chunks(
            session, history.id, limit=2
        )
        assert chunks == ["c3", "c2"]

    @pytest.mark.asyncio
    async def test_ignores_stdout_chunks(self, session: AsyncSession) -> None:
        """Consider only STDERR chunks; ignore STDOUT."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _seed_chunk(session, history.id, payload=b"stdout only")

        chunks = await TaskHistoryLogManager.get_stderr_tail_chunks(session, history.id)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_chunks(self, session: AsyncSession) -> None:
        """Return an empty list when the task history has no chunks at all."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")

        chunks = await TaskHistoryLogManager.get_stderr_tail_chunks(session, history.id)
        assert chunks == []


async def _create_history_with_log(
    session: AsyncSession,
    task: Task,
    *,
    status: TaskHistoryStatusEnum,
    created_at: datetime,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    chunks: int = 1,
) -> TaskHistory:
    """Create a ``TaskHistory`` with explicit timestamps and ``chunks`` log rows.

    :param session: The async database session.
    :param task: The parent task.
    :param status: The execution status to assign.
    :param created_at: The ``created_at`` timestamp to force on the history row.
    :param started_at: The optional ``started_at`` timestamp.
    :param finished_at: The optional ``finished_at`` timestamp.
    :param chunks: The number of ``TaskHistoryLog`` rows to attach.
    :return: The persisted task history.
    """
    history = TaskHistory(
        task_id=task.id,
        status=status,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        execution_request={
            "task": task.name,
            "target": "localhost",
            "meta": {},
            "tracking": {"allocation_id": None, "evaluation_id": None},
        },
    )
    history = await TaskHistoryManager.save(session, history)
    for offset in range(chunks):
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
    return history


async def _count_logs(session: AsyncSession) -> int:
    """Return the total number of ``taskhistory_log`` rows."""
    result = await session.exec(select(col(TaskHistoryLog.id)))
    return len(result.all())


async def _count_histories(session: AsyncSession) -> int:
    """Return the total number of ``taskhistory`` rows."""
    result = await session.exec(select(col(TaskHistory.id)))
    return len(result.all())


AGED_CHUNKS = 2
BATCH_TOTAL_CHUNKS = 5
BATCH_LIMIT = 3
BATCH_REMAINDER = BATCH_TOTAL_CHUNKS - BATCH_LIMIT
PG_CHUNKS = 3
PG_LOCK_CHUNKS = 2


class TestTaskHistoryLogManagerDeleteAgedBatch:
    """Test ``TaskHistoryLogManager.delete_aged_batch``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.LOST,
            TaskHistoryStatusEnum.STALE,
        ],
    )
    async def test_deletes_aged_non_active_logs(
        self, session: AsyncSession, status: TaskHistoryStatusEnum
    ) -> None:
        """Delete logs of aged, non-active executions and preserve the audit row."""
        task = await _create_task(session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            session,
            task,
            status=status,
            created_at=old,
            finished_at=old,
            chunks=AGED_CHUNKS,
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == AGED_CHUNKS
        assert await _count_logs(session) == 0
        assert await _count_histories(session) == 1  # audit row preserved

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [TaskHistoryStatusEnum.PENDING, TaskHistoryStatusEnum.RUNNING],
    )
    async def test_skips_active_logs(
        self, session: AsyncSession, status: TaskHistoryStatusEnum
    ) -> None:
        """Never delete logs of PENDING/RUNNING executions, however old."""
        task = await _create_task(session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            session, task, status=status, created_at=old, chunks=AGED_CHUNKS
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == 0
        assert await _count_logs(session) == AGED_CHUNKS

    @pytest.mark.asyncio
    async def test_skips_recent_logs(self, session: AsyncSession) -> None:
        """Keep logs whose effective completion is newer than the cutoff."""
        task = await _create_task(session)
        recent = utc_now() - timedelta(days=10)
        await _create_history_with_log(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            created_at=recent,
            finished_at=recent,
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == 0
        assert await _count_logs(session) == 1

    @pytest.mark.asyncio
    async def test_cutoff_boundary_is_strict(self, session: AsyncSession) -> None:
        """A row exactly at the cutoff is kept (strict ``<`` comparison)."""
        task = await _create_task(session)
        cutoff = utc_now() - timedelta(days=90)
        await _create_history_with_log(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            created_at=cutoff,
            finished_at=cutoff,
        )

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == 0
        assert await _count_logs(session) == 1

    @pytest.mark.asyncio
    async def test_coalesce_falls_back_to_started_at(
        self, session: AsyncSession
    ) -> None:
        """With finished_at NULL, age is taken from started_at."""
        task = await _create_task(session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            created_at=utc_now(),  # recent created_at must NOT save the row
            started_at=old,
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == 1

    @pytest.mark.asyncio
    async def test_coalesce_falls_back_to_created_at(
        self, session: AsyncSession
    ) -> None:
        """With finished_at and started_at NULL, age is taken from created_at."""
        task = await _create_task(session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            session, task, status=TaskHistoryStatusEnum.SUCCESS, created_at=old
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )

        assert deleted == 1

    @pytest.mark.asyncio
    async def test_batch_size_caps_rows_per_call(self, session: AsyncSession) -> None:
        """A single call deletes at most ``batch_size`` rows."""
        task = await _create_task(session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            created_at=old,
            finished_at=old,
            chunks=BATCH_TOTAL_CHUNKS,
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=BATCH_LIMIT
        )

        assert deleted == BATCH_LIMIT
        assert await _count_logs(session) == BATCH_REMAINDER

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self, session: AsyncSession) -> None:
        """Return 0 when there is nothing to delete."""
        cutoff = utc_now() - timedelta(days=90)
        deleted = await TaskHistoryLogManager.delete_aged_batch(
            session, cutoff=cutoff, batch_size=100
        )
        assert deleted == 0

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_deletes_on_real_postgres(
        self, postgres_session: AsyncSession
    ) -> None:
        """The FOR UPDATE SKIP LOCKED batched delete runs and deletes on real PG."""
        task = await _create_task(postgres_session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            postgres_session,
            task,
            status=TaskHistoryStatusEnum.LOST,
            created_at=old,
            finished_at=old,
            chunks=PG_CHUNKS,
        )
        cutoff = utc_now() - timedelta(days=90)

        deleted = await TaskHistoryLogManager.delete_aged_batch(
            postgres_session, cutoff=cutoff, batch_size=100
        )

        assert deleted == PG_CHUNKS
        assert await _count_logs(postgres_session) == 0
        assert await _count_histories(postgres_session) == 1

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_skip_locked_skips_rows_locked_by_another_session(
        self, postgres_engine: AsyncEngine, postgres_session: AsyncSession
    ) -> None:
        """Rows locked by a concurrent transaction are skipped, not blocked on."""
        task = await _create_task(postgres_session)
        old = utc_now() - timedelta(days=100)
        await _create_history_with_log(
            postgres_session,
            task,
            status=TaskHistoryStatusEnum.SUCCESS,
            created_at=old,
            finished_at=old,
            chunks=PG_LOCK_CHUNKS,
        )
        cutoff = utc_now() - timedelta(days=90)

        # Session A locks every log row and holds the transaction open.
        locker_maker = get_async_session_maker_from_engine(postgres_engine)
        async with locker_maker() as locker:
            await locker.exec(
                select(col(TaskHistoryLog.id)).with_for_update(skip_locked=True)
            )
            # Session B must skip the locked rows rather than block/deadlock.
            deleted = await TaskHistoryLogManager.delete_aged_batch(
                postgres_session, cutoff=cutoff, batch_size=100
            )
            assert deleted == 0
            await locker.rollback()

        # With the lock released, the rows are now deletable.
        deleted = await TaskHistoryLogManager.delete_aged_batch(
            postgres_session, cutoff=cutoff, batch_size=100
        )
        assert deleted == PG_LOCK_CHUNKS


# ---------------------------------------------------------------------------
# TaskHistoryLogManager.delete_chunks_below_offset
# ---------------------------------------------------------------------------


async def _insert_log_chunk(
    session: AsyncSession,
    task_history_id: int,
    *,
    start: int,
    end: int,
    source: str = "run-script",
    stream: TaskLogType = TaskLogType.STDOUT,
) -> None:
    """Insert a chunk row spanning ``[start, end)`` for the given stream."""
    await TaskHistoryLogManager.insert_chunk_idempotent(
        session,
        task_history_id=task_history_id,
        source=source,
        stream=stream,
        start_offset=start,
        chunk=b"x" * (end - start),
        now=utc_now(),
    )


async def _stream_end_offsets(
    session: AsyncSession,
    task_history_id: int,
    *,
    source: str = "run-script",
    stream: TaskLogType = TaskLogType.STDOUT,
) -> list[int]:
    """Return the ``end_offset`` of each chunk for a stream, ascending."""
    result = await session.exec(
        select(TaskHistoryLog)
        .where(
            col(TaskHistoryLog.task_history_id) == task_history_id,
            col(TaskHistoryLog.source) == source,
            col(TaskHistoryLog.stream) == stream,
        )
        .order_by(col(TaskHistoryLog.end_offset))
    )
    return [chunk.end_offset for chunk in result.all()]


class TestTaskHistoryLogManagerDeleteChunksBelowOffset:
    """Test ``TaskHistoryLogManager.delete_chunks_below_offset``."""

    @pytest.mark.asyncio
    async def test_deletes_chunks_at_or_below_offset_inclusive(
        self, session: AsyncSession
    ) -> None:
        """Assert ``end_offset <= max_end_offset`` rows are deleted (boundary)."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        for start in (0, 100, 200, 300):
            await _insert_log_chunk(session, history.id, start=start, end=start + 100)
        await session.commit()

        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=200,
            max_rows=100,
        )
        await session.commit()

        assert deleted == CHUNKS_AT_OR_BELOW_OFFSET
        assert await _stream_end_offsets(session, history.id) == [300, 400]

    @pytest.mark.asyncio
    async def test_respects_max_rows_oldest_first(self, session: AsyncSession) -> None:
        """Assert at most ``max_rows`` chunks are deleted, oldest first."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        for start in (0, 100, 200, 300):
            await _insert_log_chunk(session, history.id, start=start, end=start + 100)
        await session.commit()

        max_rows = 2
        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=1000,
            max_rows=max_rows,
        )
        await session.commit()

        assert deleted == max_rows
        assert await _stream_end_offsets(session, history.id) == [300, 400]

    @pytest.mark.asyncio
    async def test_no_match_returns_zero(self, session: AsyncSession) -> None:
        """Assert a delete that matches nothing returns ``0`` and removes nothing."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        for start in (0, 100, 200):
            await _insert_log_chunk(session, history.id, start=start, end=start + 100)
        await session.commit()

        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=50,
            max_rows=10,
        )
        await session.commit()

        assert deleted == 0
        assert await _stream_end_offsets(session, history.id) == [100, 200, 300]

    @pytest.mark.asyncio
    async def test_only_targets_matching_stream(self, session: AsyncSession) -> None:
        """Assert chunks of other ``(source, stream)`` tuples are left untouched."""
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        await _insert_log_chunk(session, history.id, start=0, end=100)
        await _insert_log_chunk(
            session, history.id, start=0, end=100, stream=TaskLogType.STDERR
        )
        await _insert_log_chunk(session, history.id, start=0, end=100, source="prepare")
        await session.commit()

        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=100,
            max_rows=10,
        )
        await session.commit()

        assert deleted == 1
        assert await _stream_end_offsets(session, history.id) == []
        assert await _stream_end_offsets(
            session, history.id, stream=TaskLogType.STDERR
        ) == [100]
        assert await _stream_end_offsets(session, history.id, source="prepare") == [100]

    @pytest.mark.asyncio
    async def test_does_not_commit(self, session: AsyncSession) -> None:
        """Assert the helper stages the delete without committing it.

        The caller owns the transaction, so a ``rollback`` after the call must
        restore every staged-for-deletion row.
        """
        task = await _create_task(session)
        history = await _seed_task_history(session, task, "node-a")
        history_id = history.id
        for start in (0, 100, 200, 300):
            await _insert_log_chunk(session, history_id, start=start, end=start + 100)
        await session.commit()

        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=history_id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=200,
            max_rows=100,
        )
        assert deleted == CHUNKS_AT_OR_BELOW_OFFSET
        await session.rollback()

        assert await _stream_end_offsets(session, history_id) == [100, 200, 300, 400]

    @pytest.mark.asyncio
    async def test_bounded_delete_on_postgres(
        self, postgres_session: AsyncSession
    ) -> None:
        """Assert the PK-IN-subquery delete behaves identically on real PostgreSQL."""
        task = await _create_task(postgres_session)
        history = await _seed_task_history(postgres_session, task, "node-a")
        for start in (0, 100, 200, 300):
            await _insert_log_chunk(
                postgres_session, history.id, start=start, end=start + 100
            )
        await postgres_session.commit()

        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            postgres_session,
            task_history_id=history.id,
            source="run-script",
            stream=TaskLogType.STDOUT,
            max_end_offset=200,
            max_rows=1,
        )
        await postgres_session.commit()

        assert deleted == 1
        assert await _stream_end_offsets(postgres_session, history.id) == [
            200,
            300,
            400,
        ]


class TestTaskHistoryManagerInFlightMetaValues:
    """Cover the SQL-level ``meta`` reader over rows whose protected leaves are encrypted.

    This is the one production reader that extracts a ``meta`` key straight out
    of the stored JSON, so it is where "every other key stays plaintext and
    queryable" either holds or does not.
    """

    @staticmethod
    async def _seed(
        session: AsyncSession,
        *,
        service_id: str,
        status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.RUNNING,
    ) -> TaskHistory:
        """Persist one in-flight history row carrying every leaf kind.

        :param session: The session to persist through.
        :param service_id: The plaintext ``_service_id`` the row records.
        :param status: The status the row is seeded in.
        :return: The persisted history row.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name=f"meta-{service_id}")),
        )
        return await TaskHistoryManager.save(
            session,
            TaskHistory(
                task_id=task.id,
                status=status,
                execution_request=TaskExecutionRequest(
                    task=task.name,
                    target="node-1",
                    meta={
                        SERVICE_ID_META_KEY: service_id,
                        "args": "restore --password hunter2",
                        "config": "master_password: hunter2\n",
                    },
                    payload="secret document",
                ),
                executed_by="test-user",
            ),
        )

    @pytest.mark.asyncio
    async def test_reads_a_plaintext_meta_key_off_an_encrypted_row(
        self, session: AsyncSession
    ) -> None:
        """Assert the extraction still resolves once the protected leaves are ciphertext.

        :param session: The async session the manager queries.
        """
        history = await self._seed(session, service_id="svc-1")
        await self._seed(session, service_id="svc-2")
        stored = await stored_execution_request(session, history.id)
        for key in ENCRYPTED_META_KEYS:
            assert is_encrypted(stored["meta"][key])
        assert is_encrypted(stored["payload"])

        values = await TaskHistoryManager.in_flight_meta_values(
            session, SERVICE_ID_META_KEY
        )

        assert sorted(values) == ["svc-1", "svc-2"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("meta_key", ENCRYPTED_META_KEYS)
    async def test_refuses_an_encrypted_meta_key(
        self, session: AsyncSession, meta_key: str
    ) -> None:
        """Assert asking for an encrypted key raises rather than yielding ciphertext.

        The extraction reads the stored JSON, so an encrypted key would come back
        as a list of tokens. A caller reads this to learn what it must not act
        on, so a plausible-looking wrong answer is worse than none.

        :param session: The async session the manager queries.
        :param meta_key: The encrypted key the caller asks for.
        """
        await self._seed(session, service_id="svc-1")

        with pytest.raises(ValueError, match=meta_key):
            await TaskHistoryManager.in_flight_meta_values(session, meta_key)

    @pytest.mark.asyncio
    async def test_ignores_a_terminal_row(self, session: AsyncSession) -> None:
        """Assert the active-status scoping is unaffected by the encryption.

        :param session: The async session the manager queries.
        """
        await self._seed(
            session, service_id="finished", status=TaskHistoryStatusEnum.SUCCESS
        )

        assert (
            await TaskHistoryManager.in_flight_meta_values(session, SERVICE_ID_META_KEY)
            == []
        )
