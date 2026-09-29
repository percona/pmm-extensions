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

"""Provide task execution management for Celery-based tasks."""

import asyncio
import importlib
import io
import logging
import traceback
from collections.abc import AsyncGenerator
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils import utc_now
from app.tasks.crud import TaskHistoryManager
from app.tasks.execution.models import BaseExecutor
from app.tasks.logs.log_writer import TaskHistoryLogWriter
from app.tasks.models import (
    FileMetadata,
    LogCaptureStatusEnum,
    Task,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
    TaskLogType,
)

logger = logging.getLogger(__name__)

CELERY_CALLABLE_ALLOWED_PREFIX = "app."

# `meta["target"]` carries executor-routing data (the host/cluster slug) and is
# injected by the chained-dispatch and Nomad pre-dispatch paths. It is not a
# callable argument, so the Celery executor must filter it out alongside the
# underscore-prefixed control keys before forwarding `meta` as `**kwargs`.
_RESERVED_META_KEYS = frozenset({"target"})


def _check_allowed_callable(callable_path: str) -> None:
    """Raise when a callable path is outside the allowed namespace.

    :param callable_path: The dotted path to validate.
    :raises ValueError: If the path is outside the allowed namespace.
    """
    if not callable_path.startswith(CELERY_CALLABLE_ALLOWED_PREFIX):
        raise ValueError(
            f"Callable '{callable_path}' is not in the allowed namespace "
            f"'{CELERY_CALLABLE_ALLOWED_PREFIX}'"
        )


def _callable_failure_reason(task: Task, exc: Exception) -> str:
    """Compose the stored failure reason for a callable that raised.

    ``Task.data`` is a raw JSON column with no validator behind it, so the
    resolved callable path is echoed only when it is a string inside the
    allowed namespace; any other stored value composes the generic reason
    rather than being copied into an unmasked API field. The exception's own
    message and traceback stay in the run's stderr log.

    :param task: The task whose callable was invoked.
    :param exc: The exception the invocation raised.
    :return: The reason to store on the task history.
    """
    callable_path = task.data.get("callable") if isinstance(task.data, dict) else None
    if isinstance(callable_path, str) and callable_path.startswith(
        CELERY_CALLABLE_ALLOWED_PREFIX
    ):
        return f"Task callable {callable_path!r} raised {type(exc).__name__}."
    return f"Task execution raised {type(exc).__name__}."


class CeleryExecutor(BaseExecutor):
    """Execute tasks as Python callables directly in the Celery worker process.

    Unlike the NomadExecutor which dispatches jobs to a remote Nomad cluster,
    the CeleryExecutor imports and calls a Python callable synchronously within
    ``dispatch_task``, updating the TaskHistory to SUCCESS or FAILED before
    returning.

    :param wait_interval: The interval in seconds between status checks.
        Defaults to 5 seconds.
    :type wait_interval: int
    """

    async def dispatch_task(
        self,
        session: AsyncSession,
        queue_item: TaskHistory,
        task: Task | None = None,
    ) -> TaskHistory:
        """Import and execute the callable specified in the task data.

        Captured ``stdout`` and ``stderr`` are persisted into the
        ``taskhistory_log`` chunk store via
        :class:`~app.tasks.logs.log_writer.TaskHistoryLogWriter` instead of
        being stuffed into ``execution_request.tracking``.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param queue_item: The task history record for tracking this execution.
        :type queue_item: TaskHistory
        :param task: The task to be executed. If ``None``, the queue_item's
            task will be used.
        :type task: Task | None
        :return: The updated task history with execution details.
        :rtype: TaskHistory
        """
        task = task or queue_item.task
        queue_item.started_at = utc_now()
        queue_item.status = TaskHistoryStatusEnum.RUNNING

        meta = (
            queue_item.execution_request.meta
            if queue_item.execution_request and queue_item.execution_request.meta
            else {}
        )
        kwargs = {
            key: value
            for key, value in meta.items()
            if not key.startswith("_") and key not in _RESERVED_META_KEYS
        }

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            result = await self._run_callable(
                task,
                stdout_buffer,
                stderr_buffer,
                kwargs=kwargs,
            )
            stdout_buffer.write(f"\nResult: {result}\n")
            queue_item.status = TaskHistoryStatusEnum.SUCCESS
            queue_item.set_failure_reason(None)
        except Exception as exc:
            logger.exception("Celery task %s failed", task.name)
            stderr_buffer.write(f"\nError:\n{traceback.format_exc()}")
            queue_item.status = TaskHistoryStatusEnum.FAILED
            queue_item.set_failure_reason(_callable_failure_reason(task, exc))
        finally:
            queue_item.finished_at = utc_now()

        saved = await TaskHistoryManager.save(session, queue_item)
        for stream, value in (
            (TaskLogType.STDOUT, stdout_buffer.getvalue()),
            (TaskLogType.STDERR, stderr_buffer.getvalue()),
        ):
            if value:
                await TaskHistoryLogWriter.append(
                    session,
                    saved.id,
                    source="execution",
                    stream=stream,
                    new_bytes=value.encode("utf-8"),
                    force_flush=True,
                )
            # Recorded for both streams whether or not either produced bytes:
            # this executor captures each buffer in full and synchronously, so
            # a silent stream is known-empty rather than possibly-lost.
            await TaskHistoryLogWriter.record_capture_status(
                session,
                saved.id,
                source="execution",
                stream=stream,
                capture_status=LogCaptureStatusEnum.COMPLETE,
            )
        return saved

    async def _run_callable(
        self,
        task: Task,
        stdout_buffer: io.StringIO,
        stderr_buffer: io.StringIO,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Import and invoke the callable specified in the task data.

        Redirect ``sys.stdout`` and ``sys.stderr`` into the provided buffers
        so that output produced by the callable is captured in task logs.
        Support both async and sync callables: async callables are awaited
        directly while sync callables are executed via :func:`asyncio.to_thread`.

        :param task: The task containing the callable path in its data dict.
        :type task: Task
        :param stdout_buffer: A StringIO buffer for capturing standard output.
        :type stdout_buffer: io.StringIO
        :param stderr_buffer: A StringIO buffer for capturing standard error.
        :type stderr_buffer: io.StringIO
        :param kwargs: Keyword arguments forwarded to the callable. Forwarded
            from ``execution_request.meta`` by ``dispatch_task``; defaults to
            an empty mapping (the callable is invoked with no arguments).
        :type kwargs: dict[str, Any] | None
        :return: The return value of the callable.
        :rtype: Any
        :raises ValueError: If the callable is outside the allowed namespace.
        """
        callable_path = task.data["callable"]
        _check_allowed_callable(callable_path)
        module_path, func_name = callable_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        if not callable(func):
            raise TypeError(f"'{callable_path}' is not callable")
        stdout_buffer.write(f"Executing {callable_path}\n")
        call_kwargs = kwargs or {}
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            if asyncio.iscoroutinefunction(func):
                return await func(**call_kwargs)
            return await asyncio.to_thread(func, **call_kwargs)

    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,  # noqa: ARG002
    ) -> TaskHistory:
        """Return the queue_item unchanged.

        Celery tasks run synchronously within ``dispatch_task``, so no
        additional synchronization is needed. The ``writer_session`` parameter
        is accepted to satisfy the base class contract and intentionally
        ignored.

        :param queue_item: The task history record.
        :type queue_item: TaskHistory
        :param writer_session: Ignored. Present only to match the
            ``BaseExecutor`` signature.
        :type writer_session: AsyncSession | None
        :return: The unchanged task history.
        :rtype: TaskHistory
        """
        return queue_item

    async def _stop_task(self, queue_item: TaskHistory) -> None:
        """No-op for Celery tasks.

        Celery tasks run synchronously and cannot be stopped mid-execution.

        :param queue_item: The task history record.
        :type queue_item: TaskHistory
        """

    async def validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Validate that the job contains a safe, importable callable path.

        Enforce that the callable path starts with the allowed namespace prefix
        to prevent arbitrary code execution.

        :param job: The job specification containing a ``callable`` key.
        :type job: dict[str, Any]
        :return: The original job specification if validation is successful.
        :rtype: dict[str, Any]
        :raises ValueError: If ``callable`` is missing, outside the allowed
            namespace, or not importable.
        """
        callable_path = job.get("callable")
        if not callable_path:
            raise ValueError("Job must contain a 'callable' key")
        _check_allowed_callable(callable_path)
        try:
            module_path, func_name = callable_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise ValueError(f"Cannot import callable '{callable_path}'") from exc
        if not callable(func):
            raise TypeError(f"'{callable_path}' is not callable")
        return job

    async def get_hosts(self) -> dict[str, str]:
        """Return the local host as the only available executor host.

        :return: A dictionary with a single ``local`` entry.
        :rtype: dict[str, str]
        """
        return {"local": "localhost"}

    async def stream_logs(
        self,
        queue_item: TaskHistory,  # noqa: ARG002
        start_offsets: dict[str, dict[str, int]] | None = None,  # noqa: ARG002
    ) -> AsyncGenerator[TaskLog, None]:
        """Return an empty live-log stream for Celery tasks.

        Celery tasks finish synchronously inside ``dispatch_task``, so the
        route's live-stream branch (``status == RUNNING``) is unreachable for
        them in practice. Finished Celery log retrieval flows through the
        route's non-``RUNNING`` branch, which now calls
        :func:`iter_task_history_logs` against the chunk store.

        :param queue_item: The task history record. Unused.
        :type queue_item: TaskHistory
        :param start_offsets: Starting offsets. Unused.
        :type start_offsets: dict[str, dict[str, int]] | None
        :return: An empty async generator — Celery live-streaming is
            unreachable.
        :rtype: AsyncGenerator[TaskLog, None]
        """
        return
        yield  # pragma: no cover

    async def stream_file(
        self,
        queue_item: TaskHistory,  # noqa: ARG002
        path: str,  # noqa: ARG002
        chunk_size: int = 1024 * 1024,  # noqa: ARG002
        *,
        anonymize: bool = True,  # noqa: ARG002
    ) -> AsyncGenerator[bytes, None]:
        """Raise NotImplementedError.

        Celery tasks do not produce downloadable files.

        :param queue_item: The task history record.
        :param path: The path to the file.
        :param chunk_size: The chunk size in bytes.
        :param anonymize: Whether to redact the task's configured entities.
        :raises NotImplementedError: Always.
        """
        raise NotImplementedError("File streaming is not supported for Celery tasks")
        yield  # pragma: no cover

    async def list_files(
        self,
        queue_item: TaskHistory,
        path: str,
    ) -> dict[str, FileMetadata]:
        """Raise NotImplementedError.

        Celery tasks do not produce downloadable files.

        :param queue_item: The task history record.
        :type queue_item: TaskHistory
        :param path: The path to list files from.
        :type path: str
        :raises NotImplementedError: Always.
        """
        raise NotImplementedError("File listing is not supported for Celery tasks")
