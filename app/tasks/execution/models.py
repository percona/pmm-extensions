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

"""Define base executor models for the Tasks API."""

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.models import BaseCaseInsensitiveModel
from app.core.pmm import await_annotation, schedule_annotation
from app.core.utils import utc_now
from app.tasks.crud import TaskHistoryManager
from app.tasks.execution.utils import parse_payload
from app.tasks.models import (
    ExecutionEvent,
    ExecutorHostState,
    FileMetadata,
    Task,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskLog,
)

logger = logging.getLogger(__name__)
_ONE_MEBIBYTE = 1024 * 1024
_TERMINAL_STATUS_EVENT_MAP = {
    TaskHistoryStatusEnum.SUCCESS: "COMPLETED",
    TaskHistoryStatusEnum.FAILED: "FAILED",
    TaskHistoryStatusEnum.STOPPED: "STOPPED",
    TaskHistoryStatusEnum.LOST: "LOST",
    TaskHistoryStatusEnum.STALE: "STALE",
    TaskHistoryStatusEnum.UNLAUNCHABLE: "UNLAUNCHABLE",
}


class BaseExecutor(BaseCaseInsensitiveModel, ABC):
    """Define the blueprint of a task executor.

    :param wait_interval: The interval in seconds between status checks.
        Defaults to 5 seconds.
    :type wait_interval: int
    """

    wait_interval: int = 5

    async def transform_payload(
        self,
        payload: str | bytes,
        payload_format: str,
    ) -> dict[str, Any]:
        """Parse and validate a job spec payload based on its format.

        This function parses the payload according to the specified format
        (HCL, JSON, or YAML) and validates it using the backend.

        :param payload: The job specification payload to be parsed.
        :type payload: str | bytes
        :param payload_format: The format of the payload, which can be "hcl", "json",
            or "yaml".
        :type payload_format: str
        :return: The parsed and validated job specification.
        :rtype: dict[str, Any]
        :raises ValueError: If the provided payload format is unsupported.
        :raises HTTPException: If validation of the job specification fails.
        """
        parsed = await self.parse_payload(payload, payload_format)
        logger.debug("Parsed payload: %s", parsed)
        return await self.validate_job(parsed)

    async def parse_payload(
        self, payload: str | bytes, payload_format: str
    ) -> dict[str, Any]:
        """Parse a job spec payload based on its format.

        This function parses the payload according to the specified format
        (JSON, or YAML).

        :param payload: The job specification payload to be parsed.
        :type payload: str | bytes
        :param payload_format: The format of the payload, which can be "json" or "yaml".
        :type payload_format: str
        :return: The parsed job specification.
        :rtype: dict[str, Any]
        :raises ValueError: If the provided payload format is unsupported.
        """
        return parse_payload(payload, payload_format)

    @abstractmethod
    async def dispatch_task(
        self,
        session: AsyncSession,
        queue_item: TaskHistory,
        task: Task | None = None,
    ) -> TaskHistory:
        """Dispatch a task and update the related task history.

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
        """

    async def stop_task(
        self, session: AsyncSession, queue_item: TaskHistory
    ) -> TaskHistory:
        """Stop a task execution and record its outcome.

        Persist the outcome the sync resolved: a terminal status and the finish
        time that came with it stand as they are, and only a run the sync left
        non-terminal is stamped STOPPED. Whether a stop request beats the run's
        own result is the executor's decision, already applied by the time the
        sync returns.

        Send PMM exactly one terminal annotation per stop, naming the status
        that was persisted.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param queue_item: The task history record for tracking this execution.
        :return: The updated task history with execution details.
        """
        await self._stop_task(queue_item)
        was_running = queue_item.status == TaskHistoryStatusEnum.RUNNING
        # TODO(yan): Remove sync_task_history from here as it can keep the db session open for too long
        # SEP-554
        queue_item = await self.sync_task_history(queue_item)
        sync_resolved_it = queue_item.status.is_terminal()
        if not sync_resolved_it:
            queue_item.status = TaskHistoryStatusEnum.STOPPED
            queue_item.set_failure_reason(None)
        if queue_item.finished_at is None:
            queue_item.finished_at = utc_now()
        event = _TERMINAL_STATUS_EVENT_MAP[queue_item.status]
        saved = await TaskHistoryManager.save(session, queue_item)
        # A run the sync found running and resolved is already annotated by it.
        if not (was_running and sync_resolved_it):
            await session.refresh(saved, attribute_names=["execution_request"])
            schedule_annotation(saved, event)
        return saved

    @abstractmethod
    async def _stop_task(self, queue_item: TaskHistory) -> None:
        """Stop a task execution in the backend.

        :param queue_item: The task history record for tracking this execution.
        :type queue_item: TaskHistory
        """

    # TODO: Use pydantic models instead of dict for job validation  # noqa: TD002, TD003
    @abstractmethod
    async def validate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Validate a job specification.

        :param job: The job specification to validate.
        :type job: dict[str, Any]
        :return: The original job specification if validation is successful.
        :rtype: dict[str, Any]
        """

    @abstractmethod
    def get_hosts(self) -> dict[str, str]:
        """Get the list of valid executor hosts.

        :return: A dictionary with node names as key and the respective addresses
            as values.
        :rtype: list[str]
        """

    def get_host_states(self) -> list[ExecutorHostState]:
        """Describe every host the backend knows about, usable or not.

        Deliberately concrete rather than abstract: a backend with nothing to add
        should not have to say so, and for most of them there is nothing to add.
        The default reports exactly what :meth:`get_hosts` returns as reachable and
        healthy, which is true by construction - that method returns the usable
        hosts - and reports nothing about hosts it cannot see, because a backend
        with no notion of an unusable host has none to report.

        Override it where the backend can distinguish "not registered" from
        "registered and broken"; :class:`NomadExecutor` does.

        :return: One entry per host the backend knows about.
        """
        return [
            ExecutorHostState(
                name=name, address=address, reachable=True, driver_healthy=True
            )
            for name, address in self.get_hosts().items()
        ]

    @abstractmethod
    async def stream_logs(
        self,
        queue_item: TaskHistory,
        start_offsets: dict[str, dict[str, int]] | None = None,
    ) -> AsyncGenerator[TaskLog | None, None]:
        """Stream logs from a task history record.

        Retrieves the allocation details and concurrently streams stdout and stderr logs
        for each task step. Yields ``TaskLog`` instances as log lines are received.

        A ``None`` marks a stream that carries no lines, which the route renders as
        an empty frame to keep the response open.

        :param queue_item: The task history record for tracking the logs.
        :param start_offsets: A dictionary containing the starting offsets for each
            step and log type. If None, defaults to starting from the beginning.
        :return: An async generator yielding ``TaskLog`` instances containing
            log messages.
        """
        raise NotImplementedError
        # An `async def` with no `yield` in its body is a coroutine function, not
        # an async generator, so overrides would not match this signature.
        yield  # pragma: no cover

    def preflight_stream_logs(self, queue_item: TaskHistory) -> None:
        """Validate executor state before :meth:`stream_logs` sends response headers.

        Streaming responses commit status and headers before the body iterator runs.
        Executors that resolve Nomad allocations (or similar) only inside
        :meth:`stream_logs` must override this to run that resolution here, so
        :class:`~app.tasks.execution.exceptions.TaskDataNotFoundInExecutorError`
        (data gone) and
        :class:`~app.tasks.execution.exceptions.TaskNotStartedInExecutorError`
        (nothing to stream yet, answered with a retryable 409) can be handled as
        HTTP error responses.

        The route calls it in a worker thread, so blocking I/O is fine here and
        an override must not touch event-loop-bound state.

        :param queue_item: The task history record that will be streamed.
        """

    def get_events(
        self,
        queue_item: TaskHistory,  # noqa: ARG002
    ) -> list[ExecutionEvent]:
        """Return structured execution events from stored tracking data.

        Executors that persist backend-specific state under
        :attr:`TaskHistory.execution_request` should override this to map that
        data into :class:`~app.tasks.models.ExecutionEvent`.

        :param queue_item: The task history record to read tracking from.
        :type queue_item: TaskHistory
        :return: Events sorted oldest-first by the executor implementation.
        :rtype: list[ExecutionEvent]
        """
        return []

    @abstractmethod
    async def stream_file(
        self,
        queue_item: TaskHistory,
        path: str,
        chunk_size: int = _ONE_MEBIBYTE,
        *,
        anonymize: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Stream a file from a task history record.

        :param queue_item: The task history record for tracking the file.
        :param path: The path to the file to be streamed.
        :param chunk_size: The size of each chunk to be read from the file, in bytes.
            Defaults to 1 MiB.
        :param anonymize: Whether to redact the task's configured entities from the
            streamed content. Defaults to ``True``, as every read served to a user
            must be redacted; internal reads of content PMM Extensions itself produced may opt
            out to get the bytes back verbatim.
        :return: An async generator yielding chunks of the file as bytes.
        """
        raise NotImplementedError
        # An `async def` with no `yield` in its body is a coroutine function, not
        # an async generator, so overrides would not match this signature.
        yield  # pragma: no cover

    @abstractmethod
    async def list_files(
        self, queue_item: TaskHistory, path: str
    ) -> dict[str, FileMetadata]:
        """List files in a directory from a task history record.

        :param queue_item: The task history record for tracking the logs.
        :type queue_item: TaskHistory
        :param path: The path to the directory to list files from.
        :type path: str
        :return: A dictionary with filenames as keys and their metadata as values.
            The metadata includes the file size and whether it is a directory.
        :rtype: dict[str, FileMetadata]
        """

    async def sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
        *,
        await_annotations: bool = False,
    ) -> TaskHistory:
        """Sync the task history with the backend and trigger the configured alerts.

        :param queue_item: The task history record for tracking this execution.
        :param writer_session: Optional dedicated session the executor may use
            for side-effect writes such as append-only log persistence. When
            ``None``, the executor falls back to whatever session management it
            has available.
        :param await_annotations: When True, await the terminal PMM annotation
            inline instead of scheduling it as a fire-and-forget background
            task. Required from Celery contexts that drive the event loop via
            discrete ``celery.loop.run_until_complete(...)`` calls; the FastAPI
            default (``False``) keeps user-facing request paths (manual sync
            route, connectivity polling, stop-task) non-blocking.
        :return: The updated task history with execution details.
        """
        was_running = queue_item.status == TaskHistoryStatusEnum.RUNNING
        queue_item = await self._sync_task_history(
            queue_item, writer_session=writer_session
        )
        if queue_item.task.alert_on_fail:
            await queue_item.alert_for_status()
        if was_running:
            event = _TERMINAL_STATUS_EVENT_MAP.get(queue_item.status)
            if event:
                if await_annotations:
                    await await_annotation(queue_item, event)
                else:
                    schedule_annotation(queue_item, event)
        return queue_item

    @abstractmethod
    async def _sync_task_history(
        self,
        queue_item: TaskHistory,
        writer_session: AsyncSession | None = None,
    ) -> TaskHistory:
        """Sync the task history with the backend.

        :param queue_item: The task history record for tracking this execution.
        :type queue_item: TaskHistory
        :param writer_session: Optional dedicated session the executor may use
            for side-effect writes such as log persistence.
        :type writer_session: AsyncSession | None
        :return: The updated task history with execution details.
        :rtype: TaskHistory
        """
