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

"""Report a run's structured result from its output files to a per-task recorder.

A payload writes a machine-readable JSON result to a reserved filename in its
working directory when it produces one; at terminal status the tasks service
reads that file back through the executor and hands the decoded payload to a
per-task recorder the owning plugin declared. The recorder path is resolved
lazily via :func:`app.tasks.hook_resolver.resolve_hook`, mirroring
:mod:`app.tasks.alert_hooks`, so this channel stays free of any static
``app.extensions`` import. The result is passed through as a plain ``dict`` — the tasks
service never learns a plugin's field names; the recorder validates the payload
into its own model.
"""

import json
import logging
import posixpath
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import aiohttp
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.requests.remote_api import BaseRemoteAPI
from app.tasks.crud import TaskHistoryManager
from app.tasks.db import get_async_session_maker
from app.tasks.execution.exceptions import TaskDataNotFoundInExecutorError
from app.tasks.execution.models import BaseExecutor
from app.tasks.hook_resolver import resolve_hook
from app.tasks.models import TaskHistory

logger = logging.getLogger(__name__)

#: Fixed name of the file a payload writes its result to, relative to the run's
#: working directory — which the job spec pins to the task's output-files
#: directory, so this resolves under :attr:`Task.output_files_path` on read. The
#: leading dot keeps it out of the output-files listing, which skips dot-prefixed
#: names: this is PMM Extensions' own protocol, not an artifact for a user to download.
RUN_RESULT_FILENAME = ".pmm-extensions-run-result.json"

#: Upper bound on a result file's size. A run result is a handful of scalar
#: fields; anything larger is a payload bug and is discarded unread.
RUN_RESULT_MAX_BYTES = 16_384

#: An ``async`` callable a plugin declares on ``Task.run_result_recorder`` to
#: record a terminal run's result (``None`` when the run produced no result).
RunResultRecorder = Callable[
    [AsyncSession, TaskHistory, dict[str, Any] | None], Awaitable[None]
]


async def read_run_result(
    executor: BaseExecutor, history: TaskHistory
) -> dict[str, Any] | None:
    """Read a terminal run's result file through the executor.

    The read opts out of anonymization: this content is PMM Extensions' own protocol, not
    output served to a user, and redaction would corrupt the very fields the
    recorder consumes. The read is best-effort: every shape of "there is no
    usable result" maps to ``None`` — no file, an empty one, an executor without
    file support, an allocation already gone, and content that is oversized,
    undecodable, or not a JSON object. Only an unanticipated failure propagates
    to the caller's failure logging.

    The read owns the executor's aiohttp session only when it arrives without
    one: the Celery seam builds its executor through
    :func:`app.tasks.deps.get_executor`, which never enters the async context, so
    the session has to be opened here — and closed again, being bound to whatever
    loop the worker reused for that task. A session the caller already opened is
    left untouched, since the route seam is handed the one ``NomadLifecycle``
    owns and every later log and file stream keeps streaming through it.

    :param executor: The executor that ran the task, used to read its files.
    :param history: The terminal history to read the result of; must have both
        ``task`` and ``execution_request`` loaded.
    :return: The decoded result object, or ``None`` when there is none to read.
    """
    task = history.task
    if task is None or not task.output_files_path:
        return None
    path = posixpath.join(task.output_files_path, RUN_RESULT_FILENAME)
    buffer = bytearray()
    try:
        async with AsyncExitStack() as stack:
            if isinstance(executor, BaseRemoteAPI) and executor.session is None:
                await stack.enter_async_context(executor)
            async for chunk in executor.stream_file(history, path, anonymize=False):
                buffer.extend(chunk)
                if len(buffer) > RUN_RESULT_MAX_BYTES:
                    logger.warning(
                        "Run-result file for task history %s exceeds %d bytes;"
                        " ignoring.",
                        history.id,
                        RUN_RESULT_MAX_BYTES,
                    )
                    return None
        result = json.loads(buffer.decode())
    except (
        NotImplementedError,
        TaskDataNotFoundInExecutorError,
        aiohttp.ClientError,
        ValueError,
        UnicodeDecodeError,
    ):
        logger.debug(
            "No readable run result for task history %s.", history.id, exc_info=True
        )
        return None
    if not isinstance(result, dict):
        logger.warning("Ignoring non-object run result: %r", result)
        return None
    return result


class RunResultCapture:
    """Read a run's result before the executor releases the run's files.

    A sync that ends a run lets go of the run's working directory before it
    returns, and a host short of disk space deletes the directory at once. The
    recorder can only run later, once the terminal status is saved. Pass an
    instance to :meth:`~app.tasks.execution.models.BaseExecutor.sync_task_history`
    as ``on_terminal`` to read the result in between, and hand :attr:`result`
    to :func:`maybe_record_run`.

    :param executor: The executor that runs the sync, used to read the file.
    :ivar result: The result read, or ``None`` when there was none to read.
    """

    def __init__(self, executor: BaseExecutor) -> None:
        self._executor = executor
        self.result: dict[str, Any] | None = None

    async def __call__(self, history: TaskHistory) -> None:
        """Read the result of ``history`` when its task declares a recorder.

        Nothing is read for a task without a recorder, so such a run costs no
        request to its host. A failure is logged and swallowed: it must not
        fail the sync, and :func:`maybe_record_run` reads again.

        :param history: The run that has just reached a terminal status; must
            have both ``task`` and ``execution_request`` loaded.
        """
        task = history.task
        if task is None or not task.run_result_recorder:
            return
        try:
            self.result = await read_run_result(self._executor, history)
        except Exception:
            logger.exception(
                "Could not read the run result of task history %s before its"
                " files were released.",
                history.id,
            )


async def maybe_record_run(
    task_history_id: int,
    executor: BaseExecutor,
    early_result: dict[str, Any] | None = None,
) -> None:
    """Resolve and invoke the task's run-result recorder at terminal status.

    Best-effort: no-op when the history is missing, not terminal, or declares no
    recorder; and any failure (unresolvable path, unexpected executor error,
    recorder raising, DB error) is logged and swallowed so recording can never
    fail the caller.

    Observation contract — coverage follows the entry point driving the
    transition, not the status reached. Observed:
    :func:`app.tasks.celery.sync_queue_item`, ``POST /history/{id}/sync/``, and
    :func:`app.tasks.celery.dispatch_queue_item` for a backend whose dispatch
    returns already-terminal (the Celery backend runs its callable inline). Not
    observed, each pinned by a test: a run stopped via the stop route, one
    failed before dispatch, and one the connectivity probe drives to terminal in
    its own poll loop — the first two have no result to read by construction,
    and the probe parses its own verdict.

    :param task_history_id: The id of the just-terminal ``TaskHistory``.
    :param executor: The executor that ran the task, used to read its result.
    :param early_result: The result a :class:`RunResultCapture` read during the
        sync. When there is none, the file is read here, which is the only read
        for a backend that keeps a finished run's files.
    """
    recorder_path: str | None = None
    try:
        session_maker = get_async_session_maker()
        async with session_maker() as session:
            history = await TaskHistoryManager.first(
                session,
                select_related=[TaskHistory.task],
                id=task_history_id,
            )
            if history is None or not history.status.is_terminal():
                return
            recorder_path = history.task.run_result_recorder
            if not recorder_path:
                return
            recorder: RunResultRecorder = resolve_hook(recorder_path)
            await session.refresh(history, ["execution_request"])
            result = early_result
            if result is None:
                result = await read_run_result(executor, history)
            await recorder(session, history, result)
    except Exception:
        logger.exception(
            "Run-result recorder %r failed for task history %s.",
            recorder_path,
            task_history_id,
        )
