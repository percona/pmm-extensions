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

"""Define regression tests for PMM annotation across session boundaries.

PMM annotation must not touch ORM attributes after the originating
session has closed, and ``schedule_annotation`` must reject callers
that pass an instance whose deferred ``execution_request`` column is
still unloaded.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import undefer
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import PMMSettings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.pmm import _background_tasks, await_annotation, schedule_annotation
from app.core.requests.remote_api import RemoteAPI
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.models import (
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskWrite,
)
from tests.app.factories import TaskFactory
from tests.app.tasks.execution.test_models import ConcreteExecutor


class TestScheduleAnnotationDetachedInstance:
    """Cover ORM access after the originating session has closed."""

    @pytest.mark.asyncio
    async def test_safe_after_session_close(self, session: AsyncSession):
        """Assert the background task does not touch the ORM after close.

        Reproduce the production caller pattern: fetch a real
        ``TaskHistory`` with ``undefer(TaskHistory.execution_request)``
        so the deferred column is loaded, schedule an annotation, close
        the session, then drain the background tasks.

        With the fix, the primitives are snapshotted synchronously
        before ``asyncio.create_task`` and the background coroutine
        never touches the ORM. Without the fix, the background task
        accesses ``queue_item.execution_request.meta`` after the
        session has closed and expired the attribute, raising
        ``DetachedInstanceError`` (sync driver) or
        ``sqlalchemy.exc.MissingGreenlet`` (asyncpg) — the production
        failure mode captured in the production extensions.log traceback.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)

        fetched = await TaskHistoryManager.get_or_404(
            session,
            query_options=[undefer(TaskHistory.execution_request)],
            id=saved.id,
        )

        _background_tasks.clear()
        with patch(
            "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
        ) as mock_create:
            schedule_annotation(fetched, "STARTED")
            session.sync_session.expire(fetched)
            await session.close()

            for bg_task in list(_background_tasks):
                await bg_task

        mock_create.assert_awaited_once_with(
            text="PMM Extensions: backup_data - STARTED",
            node_name="node-1",
            tags=["pmm-extensions", "backup_data", "started"],
            service_names=["svc1"],
        )

    @pytest.mark.asyncio
    async def test_pmm_failure_is_contained_to_background_task(
        self, session: AsyncSession
    ):
        """Assert a PMM-client failure does not escape ``schedule_annotation``.

        ``schedule_annotation`` is fire-and-forget *and*
        :func:`create_pmm_annotation` is best-effort: it catches every
        exception raised by the PMM client and logs it, never re-raising. So a
        real upstream failure — here the annotation ``POST`` raising — must
        leave the scheduled background task returning normally, with the error
        confined to the log. Patch the real boundary (the PMM client returned by
        ``settings.get_remote_api``) rather than ``create_pmm_annotation``
        itself, so the genuine swallow-and-log branch is exercised instead of a
        leak that production cannot produce.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.refresh(saved, attribute_names=["execution_request"])

        pmm_settings = MagicMock(spec=PMMSettings)
        pmm_settings.annotations_enabled = True
        pmm_settings.endpoint = "https://pmm.example.com"
        pmm_settings.api_key = SecretStr("test-api-key")
        pmm_settings.verify_ssl = True
        pmm_settings.annotations_timeout = 5

        failing_api = MagicMock(spec=RemoteAPI)
        failing_api.post = AsyncMock(side_effect=RuntimeError("PMM unavailable"))
        failing_api.auth.return_value.__enter__ = MagicMock(return_value=failing_api)
        failing_api.auth.return_value.__exit__ = MagicMock(return_value=False)

        _background_tasks.clear()
        with patch("app.core.pmm.settings") as mock_settings:
            mock_settings.PMM = pmm_settings
            mock_settings.get_remote_api = AsyncMock(return_value=failing_api)

            # Fire-and-forget: scheduling itself must not raise.
            schedule_annotation(saved, "STARTED")

            bg_tasks = list(_background_tasks)
            assert len(bg_tasks) == 1

            # The background task swallows the PMM failure: awaiting it must
            # complete normally rather than re-raising the upstream error.
            with patch("app.core.pmm.logger.exception") as mock_log_exc:
                await bg_tasks[0]

        failing_api.post.assert_awaited_once()
        mock_log_exc.assert_called_once()
        assert "Failed to create PMM annotation" in mock_log_exc.call_args.args[0]


class TestScheduleAnnotationPrecondition:
    """Guard against an unloaded ``execution_request`` at schedule time."""

    @pytest.mark.asyncio
    async def test_raises_when_execution_request_unloaded(self, session: AsyncSession):
        """Assert ``schedule_annotation`` rejects a deferred instance.

        Reproduce the caller pattern: save a ``TaskHistory``
        via ``TaskHistoryManager.save`` (which re-defers
        ``execution_request`` via its internal plain ``session.refresh``),
        then call ``schedule_annotation`` without first refreshing the
        deferred column. The guard must raise ``RuntimeError`` with a
        message that names the required fix.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)

        with pytest.raises(
            RuntimeError,
            match="execution_request to be loaded",
        ):
            schedule_annotation(saved, "STARTED")

    @pytest.mark.asyncio
    async def test_passes_after_explicit_refresh(self, session: AsyncSession):
        """Assert the guard does not fire when callers refresh the column.

        The fix pattern is
        ``await session.refresh(obj, attribute_names=["execution_request"])``
        immediately before ``schedule_annotation``. With that call, the
        deferred column is loaded and the guard passes.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.refresh(saved, attribute_names=["execution_request"])

        _background_tasks.clear()
        with patch(
            "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
        ) as mock_create:
            schedule_annotation(saved, "STARTED")
            for bg_task in list(_background_tasks):
                await bg_task

        mock_create.assert_awaited_once_with(
            text="PMM Extensions: backup_data - STARTED",
            node_name="node-1",
            tags=["pmm-extensions", "backup_data", "started"],
            service_names=["svc1"],
        )


class _SuccessAfterRunExecutor(ConcreteExecutor):
    """Minimal executor that marks the task history as SUCCESS in _sync_task_history."""

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        queue_item.status = TaskHistoryStatusEnum.SUCCESS
        return queue_item


class _StillRunningExecutor(ConcreteExecutor):
    """Minimal executor that leaves the task history in RUNNING (non-terminal)."""

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        return queue_item


class TestSyncTaskHistoryPmmRegressionSep1021:
    """Test terminal PMM on a detached TaskHistory with a separate writer session."""

    @pytest.mark.asyncio
    async def test_detached_item_terminal_pmm_with_writer_session(
        self, session: AsyncSession
    ) -> None:
        """Assert the terminal PMM annotation survives the load→writer session boundary.

        Reproduce the production flow: ``sync_queue_item`` loads the
        ``TaskHistory`` with ``undefer(TaskHistory.execution_request)`` in a
        reader session, closes that session, then calls
        ``executor.sync_task_history(queue_item, writer_session=...)`` inside
        a fresh writer session. With ``await_annotations=True`` the sync
        returns a pending event and the caller awaits ``await_annotation``,
        which must snapshot the request primitives via
        ``execution_request_for_pmm_snapshot`` rather than touching the
        deferred column getter across the closed reader session — that would
        raise ``MissingGreenlet`` on async drivers and abort before chain
        dispatch.

        Patch ``create_pmm_annotation`` (the HTTP boundary) so the real
        ``await_annotation`` — and therefore ``execution_request_for_pmm_snapshot``
        — runs, and assert the annotation fires with the primitives
        captured from the detached instance.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="sep1021-pmm-chain")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-z",
                meta={"_service_names": ["svc1"]},
                tracking={},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.commit()

        engine = session.bind
        if engine is None:
            raise RuntimeError("expected AsyncSession.bind to be set")
        maker = get_async_session_maker_from_engine(engine)
        async with maker() as load_session:
            loaded = await TaskHistoryManager.get_or_404(
                load_session,
                select_related=[TaskHistory.task],
                query_options=[undefer(TaskHistory.execution_request)],
                id=saved.id,
            )

        async with maker() as writer_session:
            executor = _SuccessAfterRunExecutor()
            with patch(
                "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
            ) as mock_create:
                loaded, pending_event = await executor.sync_task_history(
                    loaded, writer_session=writer_session, await_annotations=True
                )
                assert pending_event == "COMPLETED"
                await await_annotation(loaded, pending_event)

        assert loaded.status == TaskHistoryStatusEnum.SUCCESS
        mock_create.assert_awaited_once_with(
            text=f"PMM Extensions: {task.name} - COMPLETED",
            node_name="node-z",
            tags=["pmm-extensions", task.name, "completed"],
            service_names=["svc1"],
        )

    @pytest.mark.asyncio
    async def test_terminal_pmm_with_empty_service_names(
        self, session: AsyncSession
    ) -> None:
        """Assert the terminal annotation fires with an empty service-name batch.

        When ``meta`` carries no ``_service_names``, the annotation must still
        fire on a terminal status, with ``service_names`` defaulting to ``[]``.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="sep1021-empty-batch")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-z",
                meta={},
                tracking={},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.commit()

        engine = session.bind
        if engine is None:
            raise RuntimeError("expected AsyncSession.bind to be set")
        maker = get_async_session_maker_from_engine(engine)
        async with maker() as load_session:
            loaded = await TaskHistoryManager.get_or_404(
                load_session,
                select_related=[TaskHistory.task],
                query_options=[undefer(TaskHistory.execution_request)],
                id=saved.id,
            )

        async with maker() as writer_session:
            executor = _SuccessAfterRunExecutor()
            with patch(
                "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
            ) as mock_create:
                loaded, pending_event = await executor.sync_task_history(
                    loaded, writer_session=writer_session, await_annotations=True
                )
                assert pending_event == "COMPLETED"
                await await_annotation(loaded, pending_event)

        mock_create.assert_awaited_once_with(
            text=f"PMM Extensions: {task.name} - COMPLETED",
            node_name="node-z",
            tags=["pmm-extensions", task.name, "completed"],
            service_names=[],
        )

    @pytest.mark.asyncio
    async def test_non_terminal_status_skips_annotation(
        self, session: AsyncSession
    ) -> None:
        """Assert a non-terminal sync result yields no pending PMM annotation."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(
                TaskFactory.build(name="sep1021-running", alert_on_fail=False)
            ),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="node-z",
                meta={"_service_names": ["svc1"]},
                tracking={},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)

        executor = _StillRunningExecutor()
        with patch(
            "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
        ) as mock_create:
            result, pending_event = await executor.sync_task_history(
                saved, writer_session=session, await_annotations=True
            )

        assert result.status == TaskHistoryStatusEnum.RUNNING
        assert pending_event is None
        mock_create.assert_not_awaited()


class TestAwaitAnnotation:
    """Cover the ``await_annotation`` precondition and primitive snapshot.

    Mirrors :class:`TestScheduleAnnotationPrecondition` and
    :class:`TestScheduleAnnotationDetachedInstance` for the awaited helper used
    from Celery worker contexts where ``asyncio.create_task`` is abandoned when
    the outer coroutine returns.
    """

    @pytest.mark.asyncio
    async def test_raises_when_execution_request_unloaded(self, session: AsyncSession):
        """Assert ``await_annotation`` rejects a deferred instance."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)

        with pytest.raises(
            RuntimeError,
            match="execution_request to be loaded",
        ):
            await await_annotation(saved, "STARTED")

    @pytest.mark.asyncio
    async def test_awaits_annotate_task_event_after_explicit_refresh(
        self, session: AsyncSession
    ):
        """Assert ``await_annotation`` awaits ``annotate_task_event`` with snapshotted kwargs."""
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.refresh(saved, attribute_names=["execution_request"])

        with patch(
            "app.core.pmm.annotate_task_event", new_callable=AsyncMock
        ) as mock_annotate:
            await await_annotation(saved, "STARTED")

        mock_annotate.assert_awaited_once_with(
            task_name="backup_data",
            target="node-1",
            meta={"_service_names": ["svc1"]},
            event="STARTED",
        )

    @pytest.mark.asyncio
    async def test_safe_across_load_writer_session_boundary(
        self, session: AsyncSession
    ):
        """Assert ``await_annotation`` snapshots primitives from a detached instance.

        Reproduce the load→writer-session pattern: fetch the
        ``TaskHistory`` (with ``undefer(TaskHistory.execution_request)``) in
        a reader session, close the reader, then await the helper from a
        fresh writer session. The snapshot must come from the loaded
        :attr:`loaded_value` (per :func:`execution_request_for_pmm_snapshot`)
        — touching the deferred attribute getter across the closed reader
        would raise ``MissingGreenlet`` on async drivers.
        """
        task = await TaskManager.create(
            session,
            TaskWrite.model_validate(TaskFactory.build(name="backup_data")),
        )
        history = TaskHistory(
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(
                task="backup_data",
                target="node-1",
                meta={"_service_names": ["svc1"]},
            ),
            status=TaskHistoryStatusEnum.RUNNING,
        )
        saved = await TaskHistoryManager.save(session, history)
        await session.commit()

        engine = session.bind
        if engine is None:
            raise RuntimeError("expected AsyncSession.bind to be set")
        maker = get_async_session_maker_from_engine(engine)
        async with maker() as load_session:
            loaded = await TaskHistoryManager.get_or_404(
                load_session,
                query_options=[undefer(TaskHistory.execution_request)],
                id=saved.id,
            )

        with patch(
            "app.core.pmm.create_pmm_annotation", new_callable=AsyncMock
        ) as mock_create:
            await await_annotation(loaded, "STARTED")

        mock_create.assert_awaited_once_with(
            text="PMM Extensions: backup_data - STARTED",
            node_name="node-1",
            tags=["pmm-extensions", "backup_data", "started"],
            service_names=["svc1"],
        )
