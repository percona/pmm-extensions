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

"""Define database operations for the Tasks API."""

import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ChunkedIteratorResult, CursorResult, delete, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnExpressionArgument
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import and_, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import SERVICE_PRINCIPAL_ID
from app.core.auth.exceptions import HTTPForbiddenException
from app.core.db import ListQuery, ListQuerySpec
from app.core.db.crud import BaseManager, BaseSQLModelManager
from app.core.db.utils import func_json_extract, idempotent_insert
from app.core.exceptions import HTTPConflictException
from app.core.pagination import PaginatedResponse, Pagination
from app.core.utils.date_time import utc_now
from app.core.utils.fields import DatabaseDialect
from app.tasks.execution.executors.nomad.steps import NON_PERSISTABLE_STEPS
from app.tasks.execution_request_secrets import ENCRYPTED_META_KEYS
from app.tasks.logs.constants import TAIL_SCAN_MAX_CHUNKS
from app.tasks.models import (
    CAPTURE_STATUS_PRECEDENCE,
    DispatchLock,
    GENERIC_EXECUTOR_TASK_NAMES,
    INTERNAL_TASK_NAMES,
    LogCaptureStatusEnum,
    SYSTEM_USER,
    Task,
    TaskBackendEnum,
    TaskHistory,
    TaskHistoryLatestStatus,
    TaskHistoryLog,
    TaskHistoryLogState,
    TaskHistoryStatusEnum,
    TaskHistoryStatusPoint,
    TaskLogType,
)

logger = logging.getLogger(__name__)

SYSTEM_EXECUTOR_IDS = frozenset({SYSTEM_USER, str(SERVICE_PRINCIPAL_ID)})

#: Rows per batch for whole-population passes over the active tasks.
ACTIVE_TASK_BATCH_SIZE = 500


class TaskManager(BaseSQLModelManager):
    """Manage task operations, including retrieval, listing, and deletion.

    This class provides methods to interact with ``Task`` models in the database,
    such as listing active tasks, retrieving tasks by name, and deleting tasks.

    :ivar Model: The SQLModel class this manager is responsible for (``Task``).
    :cvar list_query_spec: Sort allowlist, searchable columns, default sort, and
        unique ``id`` tie-breaker for Task list endpoints.
    """

    Model = Task
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(Task.name),
            "backend": col(Task.backend),
            "owner": col(Task.owner),
            "created_at": col(Task.created_at),
            "updated_at": col(Task.updated_at),
        },
        default_sort="-created_at",
        tie_breaker=col(Task.id),
        searchable=[col(Task.name), col(Task.owner)],
    )

    @classmethod
    def _append_list_active_data_filters(
        cls,
        where: list[ColumnElement[bool]],
        session: AsyncSession,
        *,
        target: str | None = None,
        parent_is_null: bool | None = None,
        backup_type: str | None = None,
        self_parent: bool | None = None,
    ) -> None:
        """Append JSON ``Task.data`` predicates used by active-task list queries."""
        if target is not None:
            where.append(Task.data["meta"]["target"].as_string() == target)
        # Bound unconditionally: it only builds a SQL expression, and the guard
        # it used to sit behind was the disjunction of the two guards below, so
        # no path ever reached an unbound read.
        parent_value = func_json_extract(
            session.get_bind().name, col(Task.data), "parent"
        )
        if parent_is_null is not None:
            if parent_is_null:
                where.append(parent_value.is_(None))
            else:
                where.append(parent_value.isnot(None))
        if backup_type is not None:
            where.append(Task.data["backup_type"].as_string() == backup_type)
        if self_parent:
            where.append(parent_value == col(Task.name))

    @classmethod
    async def list_active(
        cls,
        session: AsyncSession,
        owner: str | None = None,
        target: str | None = None,
    ) -> list[Task]:
        """List all active (non-deleted) tasks.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param owner: The owner of the tasks. If provided, only tasks for this owner
            will be listed.
        :param target: The execution target hostname. If provided, only tasks whose
            ``data["meta"]["target"]`` matches will be listed.
        :return: A list of active tasks.
        """
        where = [col(Task.deleted_at).is_(None)]
        kwargs = {}
        if owner is not None:
            kwargs["owner"] = owner
        cls._append_list_active_data_filters(where, session, target=target)
        return await cls.list(session, *where, **kwargs)

    @classmethod
    async def iter_active_batches(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        owner: str | None = None,
        batch_size: int = ACTIVE_TASK_BATCH_SIZE,
        query_options: Sequence = (),
    ) -> AsyncGenerator[list[Task], None]:
        """Yield every active task in ascending-id batches.

        For whole-population passes, where :meth:`list_active` would materialize
        every matching row — each with its full JSON ``data`` — before the caller
        sees the first one. Keyset paging on the primary key rather than
        ``offset`` so the batches stay disjoint and exhaustive: a concurrent
        insert or delete shifts no row across a page boundary.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param whereclause: Extra SQL predicates ANDed onto the active-task
            filter.
        :param owner: The owner of the tasks. If provided, only tasks for this
            owner are yielded.
        :param batch_size: The maximum number of tasks per batch.
        :param query_options: SQLAlchemy loader options applied to every batch
            query, e.g. ``load_only`` to keep unread columns out of it.
        :return: Batches of active tasks, ordered by ascending id.
        """
        kwargs: dict[str, Any] = {}
        if owner is not None:
            kwargs["owner"] = owner
        last_id = 0
        while True:
            batch = await cls.list(
                session,
                col(Task.deleted_at).is_(None),
                col(Task.id) > last_id,
                *whereclause,
                order_by=[col(Task.id)],
                limit=batch_size,
                query_options=query_options,
                **kwargs,
            )
            if not batch:
                return
            yield batch
            if len(batch) < batch_size:
                return
            # ``id`` is typed optional for unpersisted instances; a queried row
            # always carries one, and the ordering puts the highest last.
            last_id = batch[-1].id
            if last_id is None:
                return

    @classmethod
    async def list_active_paginated(
        cls,
        session: AsyncSession,
        pagination: Pagination,
        *,
        list_query: ListQuery,
        owner: str | None = None,
        target: str | None = None,
        parent_is_null: bool | None = None,
        backup_type: str | None = None,
        self_parent: bool | None = None,
    ) -> PaginatedResponse[Task]:
        """Return a paginated response of active (non-deleted) tasks.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param pagination: Validated offset/limit window for this page.
        :param list_query: The resolved sort/search produced at the request boundary.
        :param owner: The owner of the tasks. If provided, only tasks for this owner
            will be listed.
        :param target: The execution target hostname. If provided, only tasks whose
            ``data["meta"]["target"]`` matches will be listed.
        :param parent_is_null: When ``True``, only tasks with a null ``data["parent"]``
            key; when ``False``, only tasks with a non-null parent. When ``None``,
            do not filter on parent.
        :param backup_type: When provided, only tasks whose ``data["backup_type"]``
            matches this string.
        :param self_parent: When ``True``, only tasks whose ``data["parent"]`` equals
            ``Task.name`` are returned.
        :return: A paginated response containing active tasks and metadata.
        """
        where = [col(Task.deleted_at).is_(None)]
        kwargs = {}
        if owner is not None:
            kwargs["owner"] = owner
        cls._append_list_active_data_filters(
            where,
            session,
            target=target,
            parent_is_null=parent_is_null,
            backup_type=backup_type,
            self_parent=self_parent,
        )
        return await cls.list_query_paginated(
            session,
            *where,
            list_query=list_query,
            pagination=pagination,
            **kwargs,
        )

    @classmethod
    async def retrieve_by_name(
        cls,
        session: AsyncSession,
        name: str,
        *,
        is_template: bool | None = None,
        is_active: bool | None = None,
    ) -> Task:
        """Retrieve a task by its name, raising a 404 error if not found.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param name: The name of the task to retrieve.
        :type name: str
        :param is_template: Whether the task should be a template or not.
            Use None to not use the filter. Defaults to None.
        :type is_template: bool | None
        :param is_active: Whether to retrieve only active tasks (not deleted).
            If None (default), both active and deleted tasks will be considered.
        :type is_active: bool | None
        :return: The task with the given name.
        :rtype: Task
        :raises HTTPNotFoundException: If no task with the given name is found.
        """
        if is_active is None:
            return await cls.get_or_404(session, name=name, is_template=is_template)
        where = (
            col(Task.deleted_at).is_(None)
            if is_active
            else col(Task.deleted_at).isnot(None)
        )
        return await cls.get_or_404(session, where, name=name, is_template=is_template)

    @classmethod
    async def delete_by_name(cls, session: AsyncSession, name: str) -> Task:
        """Delete a task by its name, marking it as deleted.

        If the task is protected, a forbidden exception will be raised.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param name: The name of the task to delete.
        :type name: str
        :return: The deleted task.
        :rtype: Task
        :raises HTTPForbiddenException: If the task is protected and cannot be deleted.
        :raises HTTPConflictException: If the task is currently running or pending.
        """
        task = await cls.retrieve_by_name(session=session, name=name, is_active=True)
        if task.protected:
            raise HTTPForbiddenException(
                f"Task {name} is protected and cannot be deleted.",
            )
        # TODO(yan): Implement proper locking mechanism for deletion
        # SEP-393
        if await TaskHistoryManager.list_by_task_name(
            session=session, task_name=name, status=TaskHistoryStatusEnum.RUNNING
        ):
            raise HTTPConflictException(
                f"Task {name} is currently running and cannot be deleted."
            )
        if await TaskHistoryManager.list_by_task_name(
            session=session, task_name=name, status=TaskHistoryStatusEnum.PENDING
        ):
            raise HTTPConflictException(
                f"Task {name} is currently pending and cannot be deleted."
            )
        task.deleted_at = utc_now()
        task.name = f"{task.name}-{task.deleted_at.strftime('%Y%m%d%H%M%S')}"
        return await cls.save(session, task)

    @classmethod
    async def delete_unattached_system_tasks(
        cls, session: AsyncSession, exclude_task_names: Sequence[str]
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any]:
        """Delete unattached system tasks that are not in the provided sequence.

        This method identifies system tasks that are not attached to any task history
        and are not in the provided sequence of task names. It then deletes these tasks.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param exclude_task_names: A sequence of task names to exclude from deletion.
        :type exclude_task_names: Sequence[str]
        :return: The result of the delete operation.
        :rtype: CursorResult
        """
        proxy_task = aliased(Task)
        query = (
            select(Task)
            .join(TaskHistory, isouter=True)
            .join(
                proxy_task,
                and_(
                    col(proxy_task.backend) == TaskBackendEnum.PROXY,
                    func_json_extract(session.get_bind().name, proxy_task.data, "task")
                    == col(Task.name),
                    col(proxy_task.id) != col(Task.id),
                ),
                isouter=True,
            )
        )
        query = TaskManager._filter_query(
            query,
            col(Task.name).not_in(exclude_task_names),
            col(Task.protected).is_(True),
            col(TaskHistory.task_id).is_(None),
            col(proxy_task.id).is_(None),
        )
        result = await TaskManager._exec(session, query)
        tasks_ids_to_delete = [task.id for task in result.unique().all()]
        return await TaskManager.delete_where(
            session, col(Task.id).in_(tasks_ids_to_delete)
        )

    @classmethod
    async def get_root_task(cls, session: AsyncSession, task: Task) -> Task:
        """Get the root task for a given task.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param task: The task for which to find the root task.
        :type task: Task
        :return: The root task.
        :rtype: Task
        """
        if task.backend == TaskBackendEnum.PROXY:
            return await cls.retrieve_by_name(session=session, name=task.data["task"])
        return task

    @classmethod
    async def envelope_meta_values(
        cls, session: AsyncSession, meta_key: str
    ) -> list[str]:  # pagination-ok: an exhaustive read is the contract
        """Return every value tasks carry under ``data["meta"][meta_key]``.

        Deliberately unfiltered by ``deleted_at``: a soft-deleted task still
        satisfies ``TaskHistory.task``, so its envelope can still be read back by
        a run-result recorder, and a caller asking what the envelopes still name
        needs those rows too.

        Unpaginated for the same reason. A caller asks this to learn what it must
        *not* act on, so a truncated answer is not a shorter list but a wrong one.
        The result is distinct values of one meta key, bounded by how many
        distinct ids the fleet ever stamped.

        :param session: The SQLAlchemy asynchronous session to use.
        :param meta_key: The envelope ``meta`` key to read.
        :return: The distinct values present under that key.
        """
        extracted = func_json_extract(
            session.get_bind().name, Task.data, "meta", meta_key
        )
        query = cls._filter_query(select(extracted).distinct(), extracted.is_not(None))
        result = await cls._exec(session, query)
        return list(result.all())


class TaskHistoryManager(BaseSQLModelManager):
    """Manage task history operations, including listing task histories by task name.

    :ivar Model: The SQLModel class this manager is responsible for (``TaskHistory``).
    :cvar list_query_spec: Shared sort allowlist, searchable columns, default sort,
        and unique ``id`` tie-breaker for both TaskHistory list endpoints.
    """

    Model = TaskHistory
    list_query_spec = ListQuerySpec(
        sortable={
            "created_at": col(TaskHistory.created_at),
            "started_at": col(TaskHistory.started_at),
            "finished_at": col(TaskHistory.finished_at),
            "status": col(TaskHistory.status),
            "executed_by": col(TaskHistory.executed_by),
        },
        default_sort="-created_at",
        tie_breaker=col(TaskHistory.id),
        searchable=[col(TaskHistory.executed_by)],
    )

    @classmethod
    async def in_flight_meta_values(
        cls, session: AsyncSession, meta_key: str
    ) -> list[str]:  # pagination-ok: an exhaustive read is the contract
        """Return the values in-flight executions carry under that ``meta`` key.

        Scoped to :meth:`TaskHistoryStatusEnum.active_statuses` rather than a
        literal status set, so a future non-terminal status is picked up by
        adding it there. A terminal execution is history and is deliberately
        excluded: it can no longer produce a write.

        Unpaginated: a caller asks this to learn what it must *not* act on, so a
        truncated answer is wrong rather than short. In-flight executions are
        bounded by concurrency, not by history.

        An encrypted key is refused rather than served. The extraction reads the
        stored JSON directly, so pointing it at one of those keys returns a list
        of ciphertext, which is a plausible-looking answer to a question whose
        caller asked it precisely to avoid acting wrongly.

        :param session: The SQLAlchemy asynchronous session to use.
        :param meta_key: The execution-request ``meta`` key to read.
        :raises ValueError: If ``meta_key`` names a key stored encrypted.
        :return: The distinct values present under that key.
        """
        if meta_key in ENCRYPTED_META_KEYS:
            raise ValueError(
                f"{meta_key!r} is stored encrypted and cannot be read out of the "
                f"execution request in SQL; load the rows instead."
            )
        extracted = func_json_extract(
            session.get_bind().name,
            col(TaskHistory.execution_request),
            "meta",
            meta_key,
        )
        query = cls._filter_query(
            select(extracted).distinct(),
            extracted.is_not(None),
            col(TaskHistory.status).in_(TaskHistoryStatusEnum.active_statuses()),
        )
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def get_log_producer_epoch(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        for_update: bool = False,
    ) -> int:
        """Return the task-level log producer-epoch high-water mark.

        The log writer consults this on the first-insert path — before any
        per-stream ``TaskHistoryLogState`` row exists — to discard writes from a
        superseded producer. A missing row yields the ``0`` sentinel so the
        caller trusts the write.

        With ``for_update`` the ``TaskHistory`` row is locked (``SELECT ... FOR
        UPDATE``) and the lock is held until the caller's transaction ends. This
        serialises the first-insert discard decision against
        :meth:`bump_log_producer_epoch` (stamped during a frontier reset) so a
        reset cannot commit a newer epoch in the window between the guard read
        and the row insert. Both the first-insert writer and the frontier reset
        acquire this same row first, giving a consistent lock order. On SQLite
        the clause is a no-op (writes already serialise at the database level).

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param for_update: Whether to lock the row for the duration of the
            transaction.
        :return: The stored ``log_producer_epoch``, or ``0`` when the row is
            absent.
        """
        query = select(TaskHistory.log_producer_epoch).where(
            col(TaskHistory.id) == task_history_id
        )
        if for_update:
            query = query.with_for_update()
        result = await cls._exec(session, query)
        epoch = result.first()
        return epoch if epoch is not None else 0

    @classmethod
    async def bump_log_producer_epoch(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        new_producer_epoch: int,
    ) -> None:
        """Advance the task-level producer-epoch high-water mark monotonically.

        Stamped wherever the log frontier is reset. The ``< new`` guard makes the
        update monotonic: an out-of-order or stale reset carrying a smaller epoch
        is a no-op, so the task-level mark never regresses below a per-stream
        epoch. Does not commit — the caller owns the transaction so the mark and
        the per-stream frontier reset land (or roll back) together.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param new_producer_epoch: The producer epoch the frontier is being
            reset onto (for example a Nomad allocation ``CreateIndex``).
        """
        stmt = (
            update(TaskHistory)
            .where(
                col(TaskHistory.id) == task_history_id,
                col(TaskHistory.log_producer_epoch) < new_producer_epoch,
            )
            .values(log_producer_epoch=new_producer_epoch)
        )
        await cls._exec(session, stmt)

    @classmethod
    async def list_by_task_name(
        cls,
        *,
        session: AsyncSession,
        task_name: str,
        status: TaskHistoryStatusEnum | None = None,
        snippet_filename: str | None = None,
        select_related_task: bool = False,
        offset: int | None = None,
        limit: int | None = None,
        query_options: Sequence = (),
    ) -> list[TaskHistory]:
        """List task histories by the task's name.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param task_name: The name of the task to list histories for.
        :type task_name: str
        :param status: The status of the task history. If provided, only histories
            with this status will be listed.
        :type status: TaskHistoryStatusEnum | None
        :param select_related_task: Whether to include the related task data in the
            result. Defaults to False.
        :type select_related_task: bool
        :param snippet_filename: If provided, filter task histories by the specified
            snippet filename in the task's metadata.
        :type snippet_filename: str | None
        :param offset: The zero-based starting offset, or ``None`` to skip offset.
        :type offset: int | None
        :param limit: The maximum number of records, or ``None`` for no limit.
        :type limit: int | None
        :param query_options: Additional SQLAlchemy query options to apply.
        :type query_options: Sequence
        :return: A list of task histories for the specified task.
        :rtype: list[TaskHistory]
        """
        query = select(TaskHistory).join(Task)
        clauses = [col(Task.name) == task_name]
        if snippet_filename:
            clauses.append(
                func_json_extract(
                    session.get_bind().name,
                    col(TaskHistory.execution_request),
                    "meta",
                    "_snippet_filename",
                )
                == snippet_filename
            )
        select_related = (TaskHistory.task,) if select_related_task else ()
        query = cls._filter_query(
            query,
            *clauses,
            select_related=select_related,
            query_options=query_options,
            status=status,
        )
        ordering = cls._get_ordering()
        if ordering:
            query = query.order_by(*ordering)
        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def list_by_task_name_paginated(
        cls,
        *,
        session: AsyncSession,
        task_name: str,
        pagination: Pagination,
        list_query: ListQuery,
        status: TaskHistoryStatusEnum | None = None,
        snippet_filename: str | None = None,
        select_related_task: bool = False,
        query_options: Sequence = (),
    ) -> PaginatedResponse[TaskHistory]:
        """Return a paginated response of task histories by the task's name.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param task_name: The name of the task to list histories for.
        :param pagination: Validated offset/limit window for this page.
        :param list_query: The resolved sort/search produced at the request boundary.
        :param status: The status of the task history. If provided, only histories
            with this status will be listed.
        :param snippet_filename: If provided, filter task histories by the specified
            snippet filename in the task's metadata.
        :param select_related_task: Whether to include the related task data in the
            result. Defaults to False.
        :param query_options: Additional SQLAlchemy query options to apply.
        :return: A paginated response containing task histories and metadata.
        """
        clauses = [
            col(TaskHistory.task_id).in_(
                select(Task.id).where(col(Task.name) == task_name)
            )
        ]
        if snippet_filename:
            clauses.append(
                func_json_extract(
                    session.get_bind().name,
                    col(TaskHistory.execution_request),
                    "meta",
                    "_snippet_filename",
                )
                == snippet_filename
            )
        select_related = (TaskHistory.task,) if select_related_task else ()
        return await cls.list_query_paginated(
            session,
            *clauses,
            list_query=list_query,
            select_related=select_related,
            query_options=query_options,
            pagination=pagination,
            status=status,
        )

    @classmethod
    async def list_all_history_paginated(
        cls,
        session: AsyncSession,
        *,
        pagination: Pagination,
        list_query: ListQuery,
        status: TaskHistoryStatusEnum | None = None,
        exclude_internal: bool = False,
        query_options: Sequence = (),
    ) -> PaginatedResponse[TaskHistory]:
        """Return paginated all-history, optionally excluding internal maintenance tasks.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param pagination: Validated offset/limit window for this page.
        :param list_query: The resolved sort/search produced at the request boundary.
        :param status: Optional exact status filter.
        :param exclude_internal: When ``True``, omit system-internal rows before
            counting and pagination, so ``limit=5`` always returns five user-facing
            rows. Two kinds are dropped: rows whose task name belongs to
            :data:`~app.tasks.models.INTERNAL_TASK_NAMES`, and system-initiated
            generic-executor runs -- a generic-executor task (e.g. ``run-python``)
            executed by a non-human identity in :data:`SYSTEM_EXECUTOR_IDS`, such as
            connectivity checks and scheduler syncs. Defaults to ``False``.
        :param query_options: Additional SQLAlchemy query options to apply.
        :return: Paginated task-history response.
        """
        extra = ()
        if exclude_internal:
            internal_ids = (
                select(Task.id)
                .where(col(Task.name).in_(INTERNAL_TASK_NAMES))
                .scalar_subquery()
            )
            generic_executor_ids = (
                select(Task.id)
                .where(col(Task.name).in_(GENERIC_EXECUTOR_TASK_NAMES))
                .scalar_subquery()
            )
            system_generic_history_ids = (
                select(TaskHistory.id)
                .where(col(TaskHistory.task_id).in_(generic_executor_ids))
                .where(col(TaskHistory.executed_by).in_(SYSTEM_EXECUTOR_IDS))
                .scalar_subquery()
            )
            extra = (
                col(TaskHistory.task_id).not_in(internal_ids),
                col(TaskHistory.id).not_in(system_generic_history_ids),
            )
        return await cls.list_query_paginated(
            session,
            *extra,
            list_query=list_query,
            select_related=(TaskHistory.task,),
            query_options=query_options,
            pagination=pagination,
            status=status,
        )

    @staticmethod
    def _latest_status_from_history_statuses(
        statuses: Sequence[TaskHistoryStatusEnum | None],
    ) -> TaskHistoryStatusEnum | None:
        """Return the first non-null status in newest-to-oldest order."""
        for status in statuses:
            if status is not None:
                return status
        return None

    @classmethod
    async def latest_status_by_task_names(
        cls,
        session: AsyncSession,
        names: Sequence[str],
    ) -> dict[str, TaskHistoryLatestStatus | None]:
        """Return the latest known history projection for each task name.

        Status resolution matches PMM Extensions list helpers: histories are considered in
        ``created_at`` descending order and the newest non-null status wins.
        ``finished_at`` is resolved independently as the ``max`` across all of a
        task's history rows, so an in-progress re-run (whose newest row has a
        null ``finished_at``) still reports the prior completion time.

        :param session: The asynchronous session used for query execution.
        :param names: Task names to resolve. Duplicates are ignored; order is
            preserved in the returned mapping.
        :return: A mapping of task name to its latest-history projection (newest
            status plus the ``max`` ``finished_at``), or ``None`` when no history
            exists or every history row has a null status.
        """
        unique_names = list(dict.fromkeys(names))
        if not unique_names:
            return {}

        latest_status_subquery = (
            select(
                col(Task.name).label("task_name"),
                col(TaskHistory.status).label("status"),
                func.max(col(TaskHistory.finished_at))
                .over(partition_by=col(Task.name))
                .label("last_finished_at"),
                func.row_number()
                .over(
                    partition_by=col(Task.name),
                    order_by=(
                        col(TaskHistory.created_at).desc(),
                        col(TaskHistory.id).desc(),
                    ),
                )
                .label("row_number"),
            )
            .select_from(TaskHistory)
            .join(Task)
            .where(
                col(Task.name).in_(unique_names),
                col(TaskHistory.status).isnot(None),
            )
            .subquery()
        )
        query = select(
            latest_status_subquery.c.task_name,
            latest_status_subquery.c.status,
            latest_status_subquery.c.last_finished_at,
        ).where(
            latest_status_subquery.c.row_number == 1,
        )
        result = await cls._exec(session, query)
        latest_by_name = {
            row.task_name: TaskHistoryLatestStatus(
                status=row.status, finished_at=row.last_finished_at
            )
            for row in result.all()
        }

        return {name: latest_by_name.get(name) for name in unique_names}

    @classmethod
    async def recent_system_status_points_by_task_names(
        cls,
        session: AsyncSession,
        thresholds: Mapping[str, datetime],
    ) -> dict[str, list[TaskHistoryStatusPoint]]:
        """Return system-triggered status observations per task name from a cutoff.

        For each ``name -> cutoff`` entry, collect the ``created_at``/``status``
        of every history row for that name executed by a system identity
        (:data:`SYSTEM_EXECUTOR_IDS`) whose ``created_at`` is at or after
        ``cutoff``, so a caller can attribute a specific periodic schedule's run
        by taking the earliest point at or after that schedule's own
        ``last_run_at``. Points are grouped by name and returned oldest-first.

        Bounding by time rather than a fixed row count guarantees a schedule's own
        run is never evicted by later same-name runs (chain children, other
        schedules, a frequently-firing name); the caller passes each name's cutoff
        as the earliest ``last_run_at`` among the schedules being resolved for it.

        :param session: The asynchronous session used for query execution.
        :param thresholds: Map of task name to the earliest ``created_at`` to
            include. Empty short-circuits to an empty mapping.
        :return: A mapping of task name to its system-run status points, ascending
            by ``created_at`` then ``id``; names with no such history are absent.
        """
        if not thresholds:
            return {}

        query = (
            select(
                col(Task.name).label("task_name"),
                col(TaskHistory.created_at).label("created_at"),
                col(TaskHistory.status).label("status"),
            )
            .select_from(TaskHistory)
            .join(Task)
            .where(
                col(TaskHistory.status).isnot(None),
                col(TaskHistory.executed_by).in_(SYSTEM_EXECUTOR_IDS),
                or_(
                    *(
                        and_(
                            col(Task.name) == name,
                            col(TaskHistory.created_at) >= cutoff,
                        )
                        for name, cutoff in thresholds.items()
                    )
                ),
            )
            .order_by(
                col(Task.name),
                col(TaskHistory.created_at),
                col(TaskHistory.id),
            )
        )
        result = await cls._exec(session, query)
        points: dict[str, list[TaskHistoryStatusPoint]] = {}
        for row in result.all():
            points.setdefault(row.task_name, []).append(
                TaskHistoryStatusPoint(created_at=row.created_at, status=row.status)
            )
        return points


class TaskHistoryLogManager(BaseSQLModelManager):
    """Manage append-only task history log chunk operations.

    :ivar Model: The SQLModel class this manager is responsible for
        (``TaskHistoryLog``).
    :vartype Model: type[TaskHistoryLog]
    """

    Model = TaskHistoryLog

    @classmethod
    async def delete_aged_batch(
        cls,
        session: AsyncSession,
        *,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        """Delete one bounded batch of aged, non-active task-execution log rows.

        Select up to ``batch_size`` ``taskhistory_log`` rows whose parent
        ``TaskHistory`` is no longer active (any status except ``PENDING`` /
        ``RUNNING``) and whose effective completion time —
        ``COALESCE(finished_at, started_at, created_at)`` — is strictly older
        than ``cutoff``, then delete them in a single committed statement. The
        parent ``taskhistory`` audit row is never touched.

        On PostgreSQL the inner selection takes ``FOR UPDATE ... SKIP LOCKED``
        on the log rows so concurrent workers never contend on or double-delete
        the same batch; SQLite omits the clause.

        :param session: The async session bound to the Tasks database.
        :param cutoff: The age boundary; rows with an effective completion time
            strictly less than this are eligible for deletion.
        :param batch_size: The maximum number of rows to delete in this call.
        :return: The number of ``taskhistory_log`` rows deleted.
        """
        effective_completion = func.coalesce(
            col(TaskHistory.finished_at),
            col(TaskHistory.started_at),
            col(TaskHistory.created_at),
        )
        doomed = (
            select(col(TaskHistoryLog.id))
            .join(
                TaskHistory,
                col(TaskHistoryLog.task_history_id) == col(TaskHistory.id),
            )
            .where(
                col(TaskHistory.status).not_in(TaskHistoryStatusEnum.active_statuses()),
                effective_completion < cutoff,
            )
            .limit(batch_size)
        )
        dialect = session.get_bind().name
        if dialect == DatabaseDialect.POSTGRESQL:
            doomed = doomed.with_for_update(skip_locked=True, of=TaskHistoryLog)

        result = await cls.delete_where(session, col(TaskHistoryLog.id).in_(doomed))
        return result.rowcount

    @classmethod
    async def ids_with_chunks(
        cls,
        session: AsyncSession,
        task_history_ids: Sequence[int],
    ) -> set[int]:
        """Return the subset of ``task_history_ids`` that have at least one chunk.

        Emit a single ``SELECT DISTINCT task_history_id FROM taskhistory_log
        WHERE task_history_id IN (:ids)`` so list endpoints avoid an N+1
        :meth:`exists` call per paginated row. Return an empty set
        for empty input without emitting any SQL -- an empty ``IN ()``
        predicate triggers a SQLAlchemy warning and is a no-op anyway.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_ids: The ``TaskHistory`` identifiers to check.
        :type task_history_ids: Sequence[int]
        :return: The subset of ``task_history_ids`` with at least one chunk row.
        :rtype: set[int]
        """
        if not task_history_ids:
            return set()
        query = (
            select(col(TaskHistoryLog.task_history_id))
            .where(col(TaskHistoryLog.task_history_id).in_(task_history_ids))
            .distinct()
        )
        result = await cls._exec(session, query)
        return set(result.all())

    @classmethod
    async def list_chunks_for_task(
        cls,
        session: AsyncSession,
        task_history_id: int,
    ) -> list[TaskHistoryLog]:
        """Return every chunk row for a task history eagerly, ordered for readback.

        Eager helper intended for tests and one-shot admin tooling; the
        streaming :meth:`iter_chunks` remains the API for production readers.
        Rows are returned in ``(source, stream, start_offset)`` order.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :return: A list of chunk rows for the task history, oldest first.
        :rtype: list[TaskHistoryLog]
        """
        query = (
            select(TaskHistoryLog)
            .where(col(TaskHistoryLog.task_history_id) == task_history_id)
            .order_by(
                col(TaskHistoryLog.source),
                col(TaskHistoryLog.stream),
                col(TaskHistoryLog.start_offset),
            )
        )
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def iter_chunks(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        source: str | None = None,
    ) -> AsyncGenerator[TaskHistoryLog, None]:
        """Yield chunk rows in ``(source, stream, start_offset)`` order.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: Optional step filter; when set, only chunks for the given
            source are yielded.
        :type source: str | None
        :yield: Matching ``TaskHistoryLog`` chunk rows, oldest first.
        """
        query = select(TaskHistoryLog).where(
            col(TaskHistoryLog.task_history_id) == task_history_id,
        )
        if source is not None:
            query = query.where(col(TaskHistoryLog.source) == source)
        query = query.order_by(
            col(TaskHistoryLog.source),
            col(TaskHistoryLog.stream),
            col(TaskHistoryLog.start_offset),
        ).execution_options(yield_per=50)
        result = await session.stream(query)
        async for row in result.scalars():
            yield row

    @classmethod
    async def list_stream_keys(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        source: str | None = None,
    ) -> list[tuple[str, TaskLogType]]:
        """Return distinct ``(source, stream)`` pairs that have chunk rows.

        Pairs are ordered by ``(source, stream)`` for deterministic tail scans.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: Optional step filter; when set, only pairs for the given
            source are returned.
        :type source: str | None
        :return: Distinct ``(source, stream)`` tuples present in the chunk store.
        :rtype: list[tuple[str, TaskLogType]]
        """
        query = (
            select(col(TaskHistoryLog.source), col(TaskHistoryLog.stream))
            .where(col(TaskHistoryLog.task_history_id) == task_history_id)
            .distinct()
            .order_by(col(TaskHistoryLog.source), col(TaskHistoryLog.stream))
        )
        if source is not None:
            query = query.where(col(TaskHistoryLog.source) == source)
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def iter_chunks_reverse(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        source: str | None = None,
        stream: TaskLogType | None = None,
    ) -> AsyncGenerator[TaskHistoryLog, None]:
        """Yield chunk rows newest-first within each ``(source, stream)`` group.

        Ordering is ``(source, stream, start_offset DESC)`` so callers can scan
        backward from the log tail without loading the full history forward.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: Optional step filter; when set, only chunks for the given
            source are yielded.
        :type source: str | None
        :param stream: Optional stream filter; when set, only chunks for the
            given stream are yielded.
        :type stream: TaskLogType | None
        :yield: Matching ``TaskHistoryLog`` chunk rows, newest first per stream.
        """
        query = select(TaskHistoryLog).where(
            col(TaskHistoryLog.task_history_id) == task_history_id,
        )
        if source is not None:
            query = query.where(col(TaskHistoryLog.source) == source)
        if stream is not None:
            query = query.where(col(TaskHistoryLog.stream) == stream)
        query = query.order_by(
            col(TaskHistoryLog.source),
            col(TaskHistoryLog.stream),
            col(TaskHistoryLog.start_offset).desc(),
        ).execution_options(yield_per=50)
        result = await session.stream(query)
        async for row in result.scalars():
            yield row

    @classmethod
    async def get_stderr_tail_chunks(
        cls,
        session: AsyncSession,
        task_history_id: int,
        limit: int = TAIL_SCAN_MAX_CHUNKS,
    ) -> list[str]:
        """Return a task history's newest ``STDERR`` chunk contents, newest-first.

        Generic tail accessor: returns up to ``limit`` STDERR chunk contents
        ordered newest-first by insertion id (``TaskHistoryLog.id``, i.e. DB
        row-insertion order -- a stable proxy for arrival order, not a guarantee
        about the chronology of the underlying log content across sources or
        streams). Callers reconstruct insertion order by reversing and joining.
        STDOUT chunks are ignored.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param limit: The maximum number of newest STDERR chunks to return.
        :return: The newest STDERR chunk contents, newest-first; empty when no
            STDERR chunk exists.
        """
        query = (
            select(TaskHistoryLog)
            .where(col(TaskHistoryLog.task_history_id) == task_history_id)
            .where(col(TaskHistoryLog.stream) == TaskLogType.STDERR)
            .order_by(col(TaskHistoryLog.id).desc())
            .limit(limit)
        )
        result = await cls._exec(session, query)
        return [chunk.content for chunk in result.all()]

    @classmethod
    async def insert_chunk_idempotent(
        cls,
        session: AsyncSession,
        *,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        start_offset: int,
        chunk: bytes,
        now: datetime,
    ) -> None:
        """Insert a log chunk row, dropping duplicates on conflict.

        Uses the dialect-specific ``INSERT ... ON CONFLICT DO NOTHING`` helper
        so the call is safe to retry — duplicate
        ``(task_history_id, source, stream, start_offset)`` keys are dropped by
        the database without raising.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: The execution step name.
        :type source: str
        :param stream: The log stream (stdout or stderr).
        :type stream: TaskLogType
        :param start_offset: The user-facing byte offset at which the chunk
            starts.
        :type start_offset: int
        :param chunk: The raw bytes to persist.
        :type chunk: bytes
        :param now: The insertion timestamp used for ``created_at``.
        :type now: datetime
        """
        stmt = idempotent_insert(session.get_bind().name, TaskHistoryLog).values(
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            start_offset=start_offset,
            end_offset=start_offset + len(chunk),
            content=chunk.decode("utf-8", errors="replace"),
            created_at=now,
        )
        await cls._exec(session, stmt)

    @classmethod
    async def delete_chunks_below_offset(
        cls,
        session: AsyncSession,
        *,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        max_end_offset: int,
        max_rows: int,
    ) -> int:
        """Delete up to ``max_rows`` oldest chunks for a stream at/under an offset.

        Removes chunk rows whose ``end_offset <= max_end_offset`` for the given
        ``(task_history_id, source, stream)``, oldest first, bounded to
        ``max_rows`` per call. Does NOT commit: the caller (the writer's
        ``append``) owns the transaction so eviction stays atomic with the chunk
        insert and the version-CAS. ``DELETE`` has no ``LIMIT`` clause, so the
        bound is applied via an ``id IN (SELECT ... ORDER BY end_offset LIMIT)``
        subquery served by the ``(task_history_id, source, stream, end_offset)``
        index. That id-select is nested one extra level (a derived table) because
        MySQL rejects referencing the delete target in an uncorrelated subquery
        (error 1093); the wrapper forces materialization and is transparent on
        PostgreSQL and SQLite.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name.
        :param stream: The log stream (stdout or stderr).
        :param max_end_offset: The inclusive upper bound on ``end_offset`` of the
            chunks eligible for deletion (the stream's low-water mark).
        :param max_rows: The maximum number of chunk rows to delete in this call.
        :return: The number of chunk rows deleted.
        """
        limited_ids = (
            select(col(TaskHistoryLog.id))
            .where(
                col(TaskHistoryLog.task_history_id) == task_history_id,
                col(TaskHistoryLog.source) == source,
                col(TaskHistoryLog.stream) == stream,
                col(TaskHistoryLog.end_offset) <= max_end_offset,
            )
            .order_by(col(TaskHistoryLog.end_offset))
            .limit(max_rows)
            .subquery()
        )
        stmt = (
            delete(TaskHistoryLog)
            .where(col(TaskHistoryLog.id).in_(select(limited_ids.c.id)))
            .execution_options(synchronize_session=False)
        )
        result = await cls._exec(session, stmt)
        return result.rowcount or 0


class TaskHistoryLogStateManager(BaseManager):
    """Manage per-stream log staging state rows.

    :ivar Model: The SQLModel class this manager is responsible for
        (``TaskHistoryLogState``).
    :vartype Model: type[TaskHistoryLogState]
    """

    Model = TaskHistoryLogState

    @classmethod
    async def get_for_stream(
        cls,
        session: AsyncSession,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
    ) -> TaskHistoryLogState | None:
        """Return the state row for a single ``(task_history_id, source, stream)`` tuple.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: The execution step name.
        :type source: str
        :param stream: The log stream (stdout or stderr).
        :type stream: TaskLogType
        :return: The existing state row, or ``None`` when it does not exist yet.
        :rtype: TaskHistoryLogState | None
        """
        query = select(TaskHistoryLogState).where(
            col(TaskHistoryLogState.task_history_id) == task_history_id,
            col(TaskHistoryLogState.source) == source,
            col(TaskHistoryLogState.stream) == stream,
        )
        result = await cls._exec(session, query)
        return result.first()

    @classmethod
    def build_default(
        cls,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
    ) -> TaskHistoryLogState:
        """Return a transient zero-offset state row with an empty staging buffer.

        The returned instance is detached and has ``version=0``; callers use it
        as the starting point for an INSERT when no row exists yet.

        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: The execution step name.
        :type source: str
        :param stream: The log stream (stdout or stderr).
        :type stream: TaskLogType
        :return: A transient ``TaskHistoryLogState`` instance with default values.
        :rtype: TaskHistoryLogState
        """
        return TaskHistoryLogState(
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            persisted_offset=0,
            producer_offset=0,
            producer_fetch_offset=0,
            producer_epoch=0,
            staging=b"",
            staging_updated_at=utc_now(),
            capture_status=LogCaptureStatusEnum.INCOMPLETE,
            version=0,
        )

    @classmethod
    async def capture_status_by_task(
        cls,
        session: AsyncSession,
        task_history_ids: Sequence[int],
    ) -> dict[int, LogCaptureStatusEnum]:
        """Return the aggregate capture verdict per task history.

        Emit a single grouped ``SELECT`` over the distinct
        ``(task_history_id, capture_status)`` pairs so list endpoints avoid an
        N+1 read per paginated row, then reduce each history's pairs by
        severity: any ``INCOMPLETE`` stream wins, else any ``UNKNOWN``, else
        ``COMPLETE``. Return an empty mapping for empty input without emitting
        any SQL — an empty ``IN ()`` predicate triggers a SQLAlchemy warning
        and is a no-op anyway.

        Histories with no state rows are absent from the result rather than
        defaulting to a verdict here; the caller renders a missing key as
        ``UNKNOWN``.

        Rows are filtered by *excluding* :data:`NON_PERSISTABLE_STEPS` — the same
        set :meth:`NomadStep.is_persistable` tests — rather than by selecting the
        Nomad steps PMM Extensions drains: ``source`` also carries non-Nomad producers
        (the Celery executor writes ``"execution"``, legacy rows carry
        ``"step1"``), and selecting by Nomad step name would drop every one of
        them and report ``UNKNOWN`` for tasks whose capture is known-complete.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_ids: The ``TaskHistory`` identifiers to aggregate.
        :return: The aggregate verdict keyed by task history identifier, for
            those identifiers carrying at least one state row.
        """
        if not task_history_ids:
            return {}
        query = (
            select(
                col(TaskHistoryLogState.task_history_id),
                col(TaskHistoryLogState.capture_status),
            )
            .where(
                col(TaskHistoryLogState.task_history_id).in_(task_history_ids),
                col(TaskHistoryLogState.source).notin_(NON_PERSISTABLE_STEPS),
            )
            .distinct()
        )
        result = await cls._exec(session, query)

        statuses: defaultdict[int, set[LogCaptureStatusEnum]] = defaultdict(set)
        for task_history_id, capture_status in result.all():
            statuses[task_history_id].add(capture_status)
        return {
            task_history_id: next(
                candidate
                for candidate in CAPTURE_STATUS_PRECEDENCE
                if candidate in seen
            )
            for task_history_id, seen in statuses.items()
        }

    @classmethod
    async def list_for_task(
        cls, session: AsyncSession, task_history_id: int
    ) -> list[TaskHistoryLogState]:
        """Return every state row attached to the given task history.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :return: A list of state rows for the task history.
        :rtype: list[TaskHistoryLogState]
        """
        query = select(TaskHistoryLogState).where(
            col(TaskHistoryLogState.task_history_id) == task_history_id,
        )
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def reset_producer_frontier(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        new_producer_epoch: int,
    ) -> None:
        """Reset both cursors to zero and stamp the new epoch for every stream.

        Called when the executor reschedules onto a follow-up producer (for
        example a Nomad allocation): the new producer's log file starts at byte
        0, so both producer-relative cursors (``producer_offset`` and
        ``producer_fetch_offset``) must be cleared in the database before the
        writer dedups or fetches against them, and ``producer_epoch`` must be
        advanced so stale-producer writes are discarded by the write guard.
        Bumps ``version`` so concurrent writers re-read the row.

        :param session: The SQLAlchemy asynchronous session.
        :param task_history_id: The ``TaskHistory`` identifier whose state
            rows should be reset.
        :param new_producer_epoch: The producer epoch the frontier is being
            reset onto (for example a Nomad allocation ``CreateIndex``).
        """
        stmt = (
            update(TaskHistoryLogState)
            .where(col(TaskHistoryLogState.task_history_id) == task_history_id)
            .values(
                producer_offset=0,
                producer_fetch_offset=0,
                producer_epoch=new_producer_epoch,
                version=col(TaskHistoryLogState.version) + 1,
                updated_at=utc_now(),
            )
        )
        await cls._exec(session, stmt)

    @classmethod
    async def insert_row_idempotent(
        cls,
        session: AsyncSession,
        *,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        persisted_offset: int,
        producer_offset: int,
        producer_fetch_offset: int,
        producer_epoch: int,
        staging: bytes,
        version: int,
        now: datetime,
        capture_status: LogCaptureStatusEnum = LogCaptureStatusEnum.INCOMPLETE,
    ) -> bool:
        """Insert a new state row, returning ``True`` on a successful insert.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` so a concurrent writer that
        already created the row causes this call to return ``False`` instead of
        raising.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name.
        :param stream: The log stream (stdout or stderr).
        :param persisted_offset: The user-facing byte offset already persisted.
        :param producer_offset: The producer-relative byte offset already
            consumed from the current producer epoch.
        :param producer_fetch_offset: The raw producer-space fetch offset for
            the next read.
        :param producer_epoch: The producer-generation stamp the cursors belong
            to (for example a Nomad allocation ``CreateIndex``).
        :param staging: Bytes pending flush to the chunk store.
        :param version: The initial optimistic-locking version counter.
        :param now: The insertion timestamp used for audit columns.
        :param capture_status: The capture verdict to stamp on the new row.
            Defaults to ``INCOMPLETE`` — a stream is not known to be drained
            at the moment its row first appears.
        :return: ``True`` when a row was inserted; ``False`` when the row
            already existed.
        """
        stmt = idempotent_insert(session.get_bind().name, TaskHistoryLogState).values(
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            persisted_offset=persisted_offset,
            producer_offset=producer_offset,
            producer_fetch_offset=producer_fetch_offset,
            producer_epoch=producer_epoch,
            staging=staging,
            staging_updated_at=now,
            capture_status=capture_status,
            version=version,
            created_at=now,
        )
        result = await cls._exec(session, stmt)
        return bool(result.rowcount == 1)

    @classmethod
    async def update_row_if_version(
        cls,
        session: AsyncSession,
        *,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        old_version: int,
        new_version: int,
        persisted_offset: int,
        producer_offset: int,
        producer_fetch_offset: int,
        producer_epoch: int,
        staging: bytes,
        now: datetime,
        capture_status: LogCaptureStatusEnum | None = None,
    ) -> bool:
        """Update a state row conditioned on the old version and return whether we won.

        Implements optimistic locking: the update matches only when the row's
        ``version`` column still equals ``old_version``. A concurrent writer
        that already bumped the version causes ``rowcount`` to be ``0`` and this
        method returns ``False`` so the caller can retry.

        :param session: The SQLAlchemy asynchronous session to use for query
            execution.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name.
        :param stream: The log stream (stdout or stderr).
        :param old_version: The version the caller read from the state row.
        :param new_version: The bumped version to write.
        :param persisted_offset: The updated user-facing persisted offset.
        :param producer_offset: The updated producer-relative offset.
        :param producer_fetch_offset: The updated raw producer-space fetch
            offset.
        :param producer_epoch: The updated producer-generation stamp the
            cursors belong to (for example a Nomad allocation ``CreateIndex``).
        :param staging: The updated staging bytes buffer.
        :param now: The update timestamp used for the audit columns.
        :param capture_status: The capture verdict to write, or ``None`` to
            leave the stored verdict untouched. Omitting the column is what
            keeps an ordinary byte-appending update from downgrading a stream
            already recorded ``COMPLETE``.
        :return: ``True`` when exactly one row was updated; ``False`` when the
            optimistic-locking guard prevented the write.
        """
        values = {
            "persisted_offset": persisted_offset,
            "producer_offset": producer_offset,
            "producer_fetch_offset": producer_fetch_offset,
            "producer_epoch": producer_epoch,
            "staging": staging,
            "staging_updated_at": now,
            "version": new_version,
            "updated_at": now,
        }
        if capture_status is not None:
            values["capture_status"] = capture_status
        stmt = (
            update(TaskHistoryLogState)
            .where(
                col(TaskHistoryLogState.task_history_id) == task_history_id,
                col(TaskHistoryLogState.source) == source,
                col(TaskHistoryLogState.stream) == stream,
                col(TaskHistoryLogState.version) == old_version,
            )
            .values(**values)
        )
        result = await cls._exec(session, stmt)
        return bool(result.rowcount == 1)


class DispatchLockManager(BaseSQLModelManager):
    """Manage dispatch lock operations.

    :ivar Model: The SQLModel class this manager is responsible for (``DispatchLock``).
    """

    Model = DispatchLock

    @classmethod
    async def claim(cls, session: AsyncSession, name: str) -> datetime:
        """Insert the lock row for ``name``, committing without reading it back.

        Deliberately not :meth:`~app.core.db.crud.CRUDBase.save`, which follows
        its commit with ``session.refresh``. That refresh is a second checkout
        from the pool, taken at the one moment when failing is most expensive:
        the row is committed by then, so a refresh that cannot get a connection
        raises after the lock exists and before the caller's ``try``/``finally``
        has been entered. The lock is orphaned, and nothing clears it until the
        caller's stale-row sweep, which refuses every identical dispatch until it
        runs. No caller reads anything back off a lock row, so the refresh buys
        nothing to pay that with.

        The ``IntegrityError`` is left to the caller rather than translated here:
        on this table a unique violation means "an identical dispatch already
        holds the lock", which is the caller's conflict to name.

        :param session: A session dedicated to this lock, separate from the one
            the dispatch itself runs on so the commit does not end that
            transaction.
        :param name: The lock's content hash.
        :return: The row's ``created_at``, which :meth:`release` needs to tell
            this claim's row from a later dispatch's. Read off the instance
            before the commit, so it costs nothing: ``default_factory`` fills it
            client-side at construction, not from the returned row.
        :raises IntegrityError: If the lock is already held.
        """
        # ty reads SQLModel's `id` as required because it cannot see the
        # primary-key default. Per-site rather than a pyproject override, which
        # would also cover any genuine hit of this rule in the file.
        lock = cls.Model(name=name)  # ty: ignore[missing-argument]
        created_at = lock.created_at
        session.add(lock)
        try:
            await session.commit()
        except IntegrityError:
            # The failed statement leaves the transaction unusable, so it has to
            # be released before the caller can act on the conflict.
            await session.rollback()
            raise
        return created_at

    @classmethod
    async def release(
        cls, session: AsyncSession, name: str, created_at: datetime
    ) -> None:
        """Delete the row this claim inserted, and only that row.

        ``name`` alone would not be safe. A dispatch that outlives the caller's
        stale window has already had its row swept and replaced by a later
        dispatch's, and a delete keyed only by name would take *that* row -
        freeing the lock while its owner is still dispatching, which is the
        double dispatch this table exists to prevent.

        ``created_at`` identifies the claim without a read-back. It is sound as a
        token because a sweep is the only way one row can give way to another
        under the same name, so the two are at least the caller's stale interval
        apart - far enough apart to survive the one-second resolution of
        :func:`~app.core.utils.date_time.utc_now`. The primary key would not do
        on its own: SQLite reissues a freed ``rowid``, so a swept row's key can
        come back on the row that replaced it.

        Deleting nothing is the expected outcome once a sweep has been through,
        not an error. The caller releases from a ``finally`` where raising would
        fail a dispatch that has already succeeded.

        :param session: A session dedicated to this lock.
        :param name: The lock's content hash.
        :param created_at: The value :meth:`claim` returned for this dispatch.
        """
        await cls.delete_where(session, name=name, created_at=created_at)
