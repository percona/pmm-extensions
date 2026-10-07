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

"""Build and deliver an incident's diagnostics bundle, recording every attempt.

The ``atw_send_log`` row *is* the job: the API creates it and enqueues the Celery
task with its id, and this module drives it to a terminal status. Because the
receiver's credentials can create attachments but not read them back, the row's
``detail`` is the only evidence a send ever landed -- so every write assigns a
fresh mapping and flags the JSON column, and a broad terminal guard makes sure no
failure family leaves a row stuck mid-flight.
"""

__all__ = [
    "AtwBundleSizeError",
    "AtwNothingToSendError",
    "AtwSendError",
    "bundle_dir",
    "fail_stale_sends",
    "purge_expired_bundles",
    "run_send",
]

import json
import logging
import time
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

from aiohttp import ClientError
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.requests import RemoteAPI
from app.core.security import get_internal_token
from app.core.utils import json_serializer
from app.core.utils.date_time import utc_now
from app.extensions.apps.atw.config import atw_settings
from app.extensions.apps.atw.crud import AtwIncidentManager, AtwSendLogManager
from app.extensions.apps.atw.models import AtwSendLog, AtwSendStatusEnum
from app.extensions.bundle_upload.factory import get_delivery_executor
from app.extensions.bundle_upload.plan import DeliveryPlan, StepRecord
from app.extensions.bundle_upload.resolver import (
    DeliveryPlanResolution,
    resolve_delivery_plan,
)
from app.extensions.bundle_upload.seam import BundleSource
from app.extensions.config import extensions_settings
from app.extensions.db import get_async_session_maker
from app.extensions.settings_override import republish_extensions_settings_snapshot
from app.tasks.config import tasks_settings
from app.tasks.models import TaskHistoryStatusEnum

logger = logging.getLogger(__name__)

_BYTES_PER_MIB = 1024 * 1024
_MANIFEST_ARCNAME = "manifest.json"
_BUNDLE_SUFFIX = ".zip"
_NOTHING_TO_SEND_ERROR = (
    "The selected executions produced no output or logs -- nothing to send."
)
#: The Nomad task name carrying a snippet's own output. Both jobs an ATW snippet
#: resolves to wrap it in a ``check-staleness`` prestart step, and
#: ``exec-python-artifact`` adds a ``prepare-env`` prestart and a ``clean-up``
#: poststop step; those steps log setup machinery, not diagnostics. See
#: ``NOMAD_EXEC_ARTIFACT`` and ``NOMAD_EXEC_PYTHON_ARTIFACT`` in
#: ``app/tasks/db/seed.py``.
_MAIN_LOG_STEP = "run-script"
#: The step carrying an unlaunchable execution's whole diagnostic. That
#: execution never starts :data:`_MAIN_LOG_STEP` and writes no output files, so
#: the general "prestart is setup machinery" rule would leave its bundle empty.
_LAUNCH_CHECK_LOG_STEP = "check-launchable"
_WORKER_LOST_ERROR = (
    "The worker running this send did not report back in time; it was most "
    "likely lost. Re-send to try again."
)


class AtwSendError(Exception):
    """Signal that a diagnostics send cannot be completed as requested."""


class AtwBundleSizeError(AtwSendError):
    """Signal that a bundle grew past the configured cap while being built."""


class AtwNothingToSendError(AtwSendError):
    """Signal that the selected executions produced no files or logs to send."""


def bundle_dir() -> Path:
    """Return the configured bundle staging directory.

    :return: Path to the staging directory.
    """
    return Path(atw_settings.bundle_dir)


class _CappedWriter:
    """Wrap a binary handle, refusing writes once the file outgrows a cap.

    The cap is enforced *during* the build rather than on the finished file, so a
    runaway bundle stops mid-stream instead of after the whole thing is staged.
    """

    def __init__(self, handle: BinaryIO, cap_mb: int) -> None:
        """Bind the wrapper to the handle it guards.

        :param handle: The open binary handle to write through.
        :param cap_mb: The largest the file may grow, in mebibytes.
        """
        self._handle = handle
        self._cap_mb = cap_mb
        self._cap_bytes = cap_mb * _BYTES_PER_MIB

    def write(self, data: bytes) -> int:
        """Write ``data`` through, then refuse if the file outgrew its cap.

        :param data: The bytes to write.
        :return: The number of bytes written.
        :raises AtwBundleSizeError: When the file has grown past the cap.
        """
        written = self._handle.write(data)
        if self._handle.tell() > self._cap_bytes:
            raise AtwBundleSizeError(
                f"Bundle exceeded the configured {self._cap_mb} MiB limit while "
                f"being built."
            )
        return written

    def tell(self) -> int:
        """Return the handle's current offset.

        :return: The offset in bytes.
        """
        return self._handle.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek the underlying handle.

        :param offset: The target offset.
        :param whence: How to interpret ``offset``.
        :return: The new absolute offset.
        """
        return self._handle.seek(offset, whence)

    def seekable(self) -> bool:
        """Report the handle as seekable so :mod:`zipfile` patches its headers.

        :return: Whether the underlying handle supports seeking.
        """
        return self._handle.seekable()

    def flush(self) -> None:
        """Flush the underlying handle."""
        self._handle.flush()


async def get_tasks_api() -> RemoteAPI:
    """Return the pooled Tasks API client the worker streams files through.

    :return: The Tasks API client, unauthenticated.
    """
    return await settings.get_remote_api(
        endpoint=extensions_settings.TASKS_ENDPOINT,
        ssl_cafile=settings.SSL_CAFILE,
        ssl_keyfile=tasks_settings.SSL_KEYFILE,
        ssl_certfile=tasks_settings.SSL_CERTFILE,
        logger_name="tasks_api",
    )


def _execution_prefix(execution: dict[str, Any]) -> str:
    """Return the zip directory namespacing one execution's files.

    Files are listed per execution, so two executions can each produce a
    ``stdout.log``; prefixing every entry keeps both and lets the manifest map a
    file back to the execution that produced it.

    :param execution: The selected execution descriptor.
    :return: The per-execution arcname prefix, without a trailing separator.
    """
    return f"{execution['task_history_id']}-{execution['snippet_filename']}"


def _execution_label(execution: dict[str, Any]) -> str:
    """Name one execution the way every upstream-failure message names it.

    :param execution: The selected execution descriptor.
    :return: The execution's id and snippet filename, as the messages render them.
    """
    return f"execution {execution['task_history_id']} ({execution['snippet_filename']})"


def _entry_arcname(prefix: str, path: str, *, is_dir: bool) -> str:
    """Return the archive entry name one upstream file is written under.

    A directory streams from the Tasks API as an on-the-fly tar.gz, so its entry
    is named for what it actually holds rather than for the directory. Upstream
    path components are filtered because :meth:`zipfile.ZipFile.open` writes
    whatever name it is handed, traversal and all -- unlike
    :meth:`zipfile.ZipFile.write`, which sanitizes.

    :param prefix: The per-execution namespace.
    :param path: The file's path within that execution's output.
    :param is_dir: Whether the entry arrives as a tar.gz stream.
    :return: The entry name to write the member under.
    """
    parts = [part for part in PurePosixPath(path).parts if part not in ("/", "..")]
    arcname = f"{prefix}/{'/'.join(parts)}"
    return f"{arcname}.tar.gz" if is_dir else arcname


def _log_arcname(prefix: str, step: str, stream: str) -> str:
    """Return the archive entry name one execution's log group is written under.

    ``step`` is an upstream Nomad task name, so its components are filtered for
    the same reason :func:`_entry_arcname` filters an upstream file path.

    :param prefix: The per-execution namespace.
    :param step: The step whose logs the member holds.
    :param stream: The stream the member holds, ``stdout`` or ``stderr``.
    :return: The entry name to write the member under.
    """
    parts = [
        part
        for part in PurePosixPath(f"{step}.{stream}.log").parts
        if part not in ("/", "..")
    ]
    return f"{prefix}/logs/{'/'.join(parts)}"


async def _execution_status(
    tasks_api: RemoteAPI, execution: dict[str, Any]
) -> TaskHistoryStatusEnum | None:
    """Return one execution's upstream status.

    The send log snapshots only an execution's identity, so whether its logs are
    safe to fetch has to be read back from the Tasks API.

    :param tasks_api: The authenticated Tasks API client.
    :param execution: The selected execution descriptor.
    :return: The upstream status, or ``None`` when it is unrecognized.
    :raises AtwSendError: When the execution's status cannot be read.
    """
    task_history_id = execution["task_history_id"]
    with _reraise_upstream_errors(
        f"Could not read the status of {_execution_label(execution)}"
    ):
        payload = await tasks_api.get(f"/history/{task_history_id}") or {}
    status = payload.get("status")
    try:
        return TaskHistoryStatusEnum(status)
    except ValueError:
        logger.warning(
            "Skipping the logs of execution %s: unrecognized status %r.",
            task_history_id,
            status,
        )
        return None


async def _add_execution_files(
    archive: zipfile.ZipFile, tasks_api: RemoteAPI, execution: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stream one execution's output files into the archive.

    Entries are written strictly one at a time: :mod:`zipfile` rejects a second
    open writable member while one is still open.

    :param archive: The open archive to write into.
    :param tasks_api: The authenticated Tasks API client.
    :param execution: The selected execution descriptor.
    :return: One manifest entry per file written.
    :raises AtwSendError: When the execution's files cannot be listed or streamed
        -- most often because it has not finished, or its output has aged out of
        the executor node.
    :raises AtwBundleSizeError: Propagated from the archive's capped writer when
        this execution's files push the bundle past the plan's cap.
    """
    task_history_id = execution["task_history_id"]
    with _reraise_upstream_errors(
        f"Could not list output files for {_execution_label(execution)}"
    ):
        listing = await tasks_api.get(f"/history/{task_history_id}/files/") or {}

    prefix = _execution_prefix(execution)
    written: list[dict[str, Any]] = []
    for path, metadata in listing.items():
        is_dir = bool(metadata.get("is_dir"))
        arcname = _entry_arcname(prefix, path, is_dir=is_dir)
        with _reraise_upstream_errors(
            f"Could not read {path!r} from {_execution_label(execution)}"
        ):
            size = await _write_entry(
                archive, tasks_api, task_history_id, path, arcname
            )
        written.append(
            {"path": path, "arcname": arcname, "size": size, "is_dir": is_dir}
        )
    return written


async def _write_entry(
    archive: zipfile.ZipFile,
    tasks_api: RemoteAPI,
    task_history_id: int,
    path: str,
    arcname: str,
) -> int:
    """Stream one upstream file into one archive member.

    :param archive: The open archive to write into.
    :param tasks_api: The authenticated Tasks API client.
    :param task_history_id: The execution the file belongs to.
    :param path: The file's path within that execution's output.
    :param arcname: The entry name to write it under.
    :return: The number of bytes streamed.
    :raises AtwBundleSizeError: Propagated from the archive's capped writer when
        this file pushes the bundle past the plan's cap.
    :raises HTTPException: Propagates the Tasks API's error status for the stream.
    :raises OSError: Propagates a connection or timeout failure mid-stream.
    :raises ClientError: Propagates a truncated or malformed response mid-stream.
    """
    size = 0
    with archive.open(arcname, "w", force_zip64=True) as entry:
        async for chunk in tasks_api.stream_chunks(
            f"/history/{task_history_id}/file/", params={"path": path}
        ):
            size += len(chunk)
            entry.write(chunk)
    return size


def _decode_log_line(line: bytes, task_history_id: int) -> dict[str, Any] | None:
    """Decode one NDJSON line of an execution's log stream.

    :param line: The raw line, carrying the trailing newline
        :meth:`RemoteAPI.stream` preserves.
    :param task_history_id: The execution the line came from.
    :return: The decoded record, or ``None`` when the line is blank, unparseable,
        or not a JSON object.
    """
    if not (text := line.strip()):
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "Skipping an unparseable log line from execution %s.", task_history_id
        )
        return None
    if not isinstance(record, dict):
        logger.warning(
            "Skipping a non-object log line from execution %s.", task_history_id
        )
        return None
    return record


class _LogMember:
    """Hold the one open archive member a single log group is streamed into.

    :mod:`zipfile` rejects a second open writable member while one is still open,
    so exactly one of these exists at a time and the caller replaces it when the
    group changes.
    """

    def __init__(
        self, archive: zipfile.ZipFile, prefix: str, group: tuple[str, str]
    ) -> None:
        """Open the member this group's messages are written into.

        :param archive: The open archive to write into.
        :param prefix: The per-execution namespace.
        :param group: The group's ``(step, stream)`` pair.
        """
        self.group = group
        self._arcname = _log_arcname(prefix, *group)
        self._size = 0
        self._handle = archive.open(self._arcname, "w", force_zip64=True)

    def write(self, msg: str) -> int:
        """Append one log message to the member.

        :param msg: The message to append.
        :return: The number of uncompressed bytes written.
        :raises AtwBundleSizeError: Propagated from the archive's capped writer
            when this message pushes the bundle past the plan's cap.
        """
        written = self._handle.write(msg.encode())
        self._size += written
        return written

    def close(self) -> dict[str, Any]:
        """Finalize the member and describe it for the manifest.

        Safe to call more than once, so a caller that closes on its normal path
        can still close unconditionally on the way out.

        :return: The manifest entry describing the finished member.
        :raises AtwBundleSizeError: Propagated from the archive's capped writer
            when flushing the compressor's tail outgrows the plan's cap.
        """
        self._handle.close()
        step, stream = self.group
        return {
            "step": step,
            "stream": stream,
            "arcname": self._arcname,
            "size": self._size,
        }


def _log_step_for(status: TaskHistoryStatusEnum | None) -> str:
    """Return the step whose logs carry this execution's diagnostic.

    :param status: The execution's terminal status.
    :return: :data:`_LAUNCH_CHECK_LOG_STEP` for an execution the node could not
        launch, :data:`_MAIN_LOG_STEP` otherwise.
    """
    if status == TaskHistoryStatusEnum.UNLAUNCHABLE:
        return _LAUNCH_CHECK_LOG_STEP
    return _MAIN_LOG_STEP


async def _add_execution_logs(
    archive: zipfile.ZipFile,
    tasks_api: RemoteAPI,
    execution: dict[str, Any],
    step: str,
) -> tuple[list[dict[str, Any]], int]:
    """Stream one execution's captured logs for ``step`` into the archive.

    A single step is fetched: for a run that happened, the prestart and poststop
    steps surrounding :data:`_MAIN_LOG_STEP` log setup machinery, which is noise
    on a support case. The one exception is a run that never started, whose only
    diagnostic lives in a prestart step — see :func:`_log_step_for`.

    Members are keyed by a record's ``(step, stream)`` group and replaced when it
    changes -- the two upstream read paths both deliver contiguous runs per group
    but in opposite stream order, so keying on the group covers either without
    assuming a sequence. A record carrying no message is skipped before its group
    is considered, so a stream with nothing to say opens no member at all.

    :param archive: The open archive to write into.
    :param tasks_api: The authenticated Tasks API client.
    :param execution: The selected execution descriptor.
    :param step: The Nomad step whose logs to stream.
    :return: One manifest entry per log group written, and the total number of
        uncompressed bytes they carry.
    :raises AtwSendError: When the execution's logs cannot be streamed.
    :raises AtwBundleSizeError: Propagated from the archive's capped writer when
        this execution's logs push the bundle past the plan's cap.
    :raises ValueError: Propagated from :meth:`RemoteAPI.stream` when one log line
        outgrows its line cap.
    """
    task_history_id = execution["task_history_id"]
    prefix = _execution_prefix(execution)
    entries: list[dict[str, Any]] = []
    total = 0
    member: _LogMember | None = None
    try:
        with _reraise_upstream_errors(
            f"Could not read logs for {_execution_label(execution)}"
        ):
            async for line in tasks_api.stream(
                f"/history/{task_history_id}/logs/", params={"step": step}
            ):
                record = _decode_log_line(line, task_history_id)
                if record is None:
                    continue
                if not isinstance(msg := record.get("msg"), str) or not msg:
                    continue
                group = (str(record.get("step", "")), str(record.get("type", "")))
                if member is None or member.group != group:
                    if member is not None:
                        entries.append(member.close())
                    member = _LogMember(archive, prefix, group)
                total += member.write(msg)
            if member is not None:
                entries.append(member.close())
    finally:
        if member is not None:
            member.close()
    return entries, total


def _build_manifest(
    row: AtwSendLog, incident_name: str, executions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe the bundle's contents for the receiver and the send log.

    :param row: The send log this bundle belongs to.
    :param incident_name: The incident's human-readable label.
    :param executions: One entry per execution, carrying its written files.
    :return: The manifest mapping.
    """
    return {
        "incident_id": str(row.incident_id),
        "incident_name": incident_name,
        "case_ref": row.case_ref,
        "requested_by": row.requested_by,
        "generated_at": utc_now().isoformat(),
        "executions": executions,
    }


async def _stage_bundle(
    path: Path,
    row: AtwSendLog,
    incident_name: str,
    tasks_api: RemoteAPI,
    cap_mb: int,
) -> tuple[int, dict[str, Any]]:
    """Stage the incident's diagnostics as one zip on local disk.

    :param path: Where to write the archive.
    :param row: The send log naming the selected executions.
    :param incident_name: The incident's human-readable label.
    :param tasks_api: The authenticated Tasks API client.
    :param cap_mb: The largest the bundle may grow, in mebibytes.
    :return: The number of collected output files and the bundle manifest.
    :raises AtwBundleSizeError: When the archive outgrows the plan's cap.
    :raises AtwNothingToSendError: When no execution produced a file or a log.
    :raises AtwSendError: When an execution's status, files, or logs cannot be
        collected.
    :raises ValueError: Propagated from :meth:`RemoteAPI.stream` when one log line
        outgrows its line cap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_executions: list[dict[str, Any]] = []
    file_count = 0
    log_bytes = 0
    with (
        path.open("wb") as handle,
        zipfile.ZipFile(
            _CappedWriter(handle, cap_mb), "w", zipfile.ZIP_DEFLATED
        ) as archive,
    ):
        for execution in row.detail.get("executions", []):
            status = await _execution_status(tasks_api, execution)
            files = await _add_execution_files(archive, tasks_api, execution)
            file_count += len(files)
            logs: list[dict[str, Any]] = []
            if status is not None and status.is_finished():
                logs, written = await _add_execution_logs(
                    archive, tasks_api, execution, _log_step_for(status)
                )
                log_bytes += written
            manifest_executions.append({**execution, "files": files, "logs": logs})
        if not file_count and not log_bytes:
            raise AtwNothingToSendError(_NOTHING_TO_SEND_ERROR)
        manifest = _build_manifest(row, incident_name, manifest_executions)
        archive.writestr(_MANIFEST_ARCNAME, json_serializer(manifest))
    return file_count, manifest


async def _persist(
    session: AsyncSession, row: AtwSendLog, detail: dict[str, Any], **fields: Any
) -> None:
    """Write a fresh ``detail`` mapping and the given columns to the row.

    A JSON column is not change-tracked through in-place mutation, so ``detail``
    is always replaced wholesale and flagged -- a silently-dropped write here
    would lose the only record that a bundle reached the receiver.

    :param session: The database session.
    :param row: The send log to update.
    :param detail: The evidence mapping to store.
    :param fields: Column values to set alongside it.
    :raises HTTPBadRequestException: Propagated from the manager when the row
        cannot be written.
    """
    for name, value in fields.items():
        setattr(row, name, value)
    row.detail = detail
    await AtwSendLogManager.save(session, row, flag_modified_fields=["detail"])


async def run_send(send_log_id: UUID) -> None:
    """Build and deliver the bundle one send log describes.

    Every failure family lands as a ``FAILED`` row carrying its reason: the row is
    the only place a support engineer can learn what happened, so the terminal
    write is guaranteed rather than best-effort — short of the database itself
    being unreachable, where the row is left to the stale sweep instead.

    :param send_log_id: The send log driving this attempt.
    :raises HTTPNotFoundException: Propagated when the incident was deleted
        between the enqueue and this attempt.
    :raises HTTPBadRequestException: Propagated when a terminal row cannot be
        written.
    :raises Exception: Propagated when rolling back or refreshing after a failed
        settings re-read itself fails. Either the database is unreachable, so no
        terminal write was going to land either way and the row is left to the
        stale sweep, or the row was deleted under the send, so there is nothing
        left to write.
    """
    async_session_maker = get_async_session_maker()
    async with async_session_maker() as session:
        row = await AtwSendLogManager.first(session, id=send_log_id)
        if row is None:
            logger.info(
                "Send log %s is gone; its incident was most likely deleted.",
                send_log_id,
            )
            return
        await _run_send_for_row(session, row)


async def _resolve_plan_after_refresh(
    session: AsyncSession,
    row: AtwSendLog,
) -> DeliveryPlanResolution:
    """Republish the PMM Extensions settings snapshot and resolve the delivery plan.

    Every send resolves against a snapshot no older than itself, so a rotated
    secret, a repointed endpoint, or a first-time enabling write takes effect
    even when this worker child's task-boundary refresher has not yet run
    (boundary refresh does not advance mid-task or while a child is idle).

    A failed republish must not escape and must not invent a fixed reason: this
    runs ahead of the broad terminal guard, so an escaping error would leave the
    row non-terminal until the stale sweep mislabels it as a lost worker, and a
    fixed unconfigured reason would turn a deliverable send — one whose in-hand
    snapshot already resolves a usable plan — into a terminal failure on a
    transient database error. Log, roll the session back so an aborted
    transaction does not also fail the terminal write that may still follow,
    refresh ``row`` so the rollback's expire does not break later attribute
    reads on the delivery path, and resolve against the snapshot this child
    already holds.

    :param session: The database session.
    :param row: The send log being driven; refreshed after a failed republish
        so delivery can continue against the held snapshot.
    :return: The resolution taken against the fresh snapshot, or against the
        snapshot this child already holds when the republish failed.
    :raises Exception: Propagates a ``session.rollback()`` or
        ``session.refresh()`` that itself fails. Either the database is
        unreachable, so no terminal write was going to land either way and the
        row is left to the stale sweep, or ``row`` was deleted under the send —
        an incident cascades to its send logs — so there is nothing left to
        write.
    """
    try:
        await republish_extensions_settings_snapshot(session)
    except Exception:
        logger.exception(
            "Could not re-read the diagnostics delivery settings; falling back "
            "to the snapshot this worker already holds."
        )
        await session.rollback()
        await session.refresh(row)
    return resolve_delivery_plan()


async def _run_send_for_row(session: AsyncSession, row: AtwSendLog) -> None:
    """Drive one send log from pending to a terminal status.

    A row the stale sweep already failed is left alone: a task delivered late
    enough for that to happen would otherwise resurrect a terminal row and
    deliver a bundle the UI has already reported as failed — and, if the
    engineer re-sent in the meantime, attach it to the case twice.

    The delivery settings are re-read once — a fresh snapshot is published —
    before the plan is resolved, so a send enqueued after an enabling write, a
    rotated secret, or a changed endpoint is not delivered against a snapshot
    older than it. A re-read that fails is the one exception: delivery then
    proceeds against the snapshot this child already holds. The failed row
    carries whichever reason the resolver gave, so an operator can tell inputs
    that stopped matching the plan from delivery nobody configured.

    :param session: The database session.
    :param row: The send log to drive.
    :raises HTTPNotFoundException: Propagated from the manager when the incident
        was deleted between the enqueue and this attempt.
    :raises HTTPBadRequestException: Propagated from the manager when a terminal
        row cannot be written.
    :raises Exception: Propagated from ``_resolve_plan_after_refresh`` when
        rolling back or refreshing after a failed settings re-read itself
        fails. That call sits ahead of the broad terminal guard, so nothing
        here catches it.
    """
    if row.status.is_terminal():
        logger.info(
            "Send log %s is already %s; not delivering it again.", row.id, row.status
        )
        return

    detail = dict(row.detail)
    resolution = await _resolve_plan_after_refresh(session, row)
    if (reason := resolution.unavailable_reason) is not None:
        await _fail(session, row, detail, [], reason)
        return
    # A resolution refuses to hold neither member, so an empty reason is a plan.
    plan = cast(DeliveryPlan, resolution.plan)

    incident = await AtwIncidentManager.get_or_404(session, id=row.incident_id)
    incident_name = incident.name
    await _persist(
        session, row, detail, status=AtwSendStatusEnum.RUNNING, started_at=utc_now()
    )

    records: list[StepRecord] = []
    path = bundle_dir() / f"{row.id}-{uuid4().hex}{_BUNDLE_SUFFIX}"
    try:
        client = await get_tasks_api()
        with client.auth(get_internal_token()) as tasks_api:
            file_count, manifest = await _stage_bundle(
                path, row, incident_name, tasks_api, plan.max_bundle_size_mb
            )
        size = path.stat().st_size
        async with get_delivery_executor(
            plan, step_observer=records.append
        ) as executor:
            with path.open("rb") as handle:
                result = await executor.upload_bundle(
                    source_ref=f"atw-incident/{row.incident_id}",
                    bundle=BundleSource(filename=path.name, content=handle, size=size),
                    case_ref=row.case_ref,
                    manifest=manifest,
                )
        logger.info(
            "Diagnostics send %s delivered to case %s as %s: %s",
            row.id,
            row.case_ref,
            result.reference,
            result.detail,
        )
    except Exception as exc:  # noqa: BLE001 -- every family must land terminally
        logger.warning("Diagnostics send %s failed.", row.id, exc_info=True)
        await _fail(session, row, detail, records, _error_message(exc, records))
        return
    finally:
        path.unlink(missing_ok=True)

    await _persist(
        session,
        row,
        {
            **detail,
            "steps": [_step_detail(record) for record in records],
            "upload_response": None if result.detail is None else dict(result.detail),
            "upload_reference": result.reference,
            "bundle_size": size,
            "file_count": file_count,
        },
        status=AtwSendStatusEnum.SUCCESS,
        finished_at=utc_now(),
    )


async def _fail(
    session: AsyncSession,
    row: AtwSendLog,
    detail: dict[str, Any],
    records: Sequence[StepRecord],
    error: str,
) -> None:
    """Write the terminal failed row carrying the reason the send ended.

    :param session: The database session.
    :param row: The send log to finalize.
    :param detail: The evidence gathered before the failure.
    :param records: The step transitions observed so far.
    :param error: The reason to record.
    """
    await _persist(
        session,
        row,
        {
            **detail,
            "steps": [_step_detail(record) for record in records],
            "error": error,
        },
        status=AtwSendStatusEnum.FAILED,
        finished_at=utc_now(),
    )


def _step_detail(record: StepRecord) -> dict[str, Any]:
    """Render one observed plan step for the send log.

    :param record: The step transition the executor reported.
    :return: The JSON-serializable step entry.
    """
    return {
        "name": record.name,
        "kind": record.kind,
        "status": record.status,
        "outputs": None if record.outputs is None else dict(record.outputs),
    }


def _upstream_detail(exc: Exception) -> str:
    """Return the message ``exc`` carries, preferring a project exception's detail.

    :param exc: The exception to describe.
    :return: The exception's ``detail`` when it carries a non-empty one, else
        its string form.
    """
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc)


@contextmanager
def _reraise_upstream_errors(message: str) -> Iterator[None]:
    """Report an upstream Tasks-API failure raised in the block as a send failure.

    A size-cap or decoding failure raised inside the block propagates untouched,
    so a local fault is not relabelled as an upstream one. ``OSError`` is the one
    blurred edge: a disk failure during a zip write inside the block is caught
    here too and reported against the Tasks API.

    :param message: What the block was trying to do, named for a support engineer.
    :return: Control to the wrapped block.
    :raises AtwSendError: When the block raises an upstream failure, carrying
        ``message`` and the upstream's own detail.
    """
    try:
        yield
    except (HTTPException, OSError, ClientError) as exc:
        raise AtwSendError(f"{message}: {_upstream_detail(exc)}") from exc


def _error_message(exc: Exception, records: Sequence[StepRecord]) -> str:
    """Return the reason to record against a failed send.

    A failure inside the delivery plan is attributed to the step that ended it
    and to the send inputs that step reads, ahead of the receiver's own message.
    A failure before any step reported one, such as bundle staging or the size
    cap, has no step to name and keeps the upstream detail alone.

    :param exc: The exception that ended the attempt.
    :param records: The step transitions the executor observed.
    :return: A message a support engineer can act on.
    """
    upstream = _upstream_detail(exc)
    failed = next(
        (record for record in reversed(records) if record.status == "failed"), None
    )
    if failed is None:
        return upstream
    where = (
        "the bundle upload"
        if failed.kind == "upload"
        else f"delivery step {failed.name!r}"
    )
    reads = (
        f", which reads {', '.join(failed.cited_inputs)}" if failed.cited_inputs else ""
    )
    return f"The send failed in {where}{reads}: {upstream}"


def purge_expired_bundles(ttl_seconds: int) -> int:
    """Delete staged bundles whose mtime is older than ``ttl_seconds``.

    :param ttl_seconds: Maximum bundle age in seconds.
    :return: The number of bundles removed.
    """
    staging = bundle_dir()
    if not staging.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for path in staging.glob(f"*{_BUNDLE_SUFFIX}"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            logger.exception("Failed to purge diagnostics bundle %s", path)
    return removed


async def fail_stale_sends(stale_after: timedelta) -> int:
    """Drive sends abandoned by a lost worker to a terminal failed status.

    Both non-terminal statuses need a driver: a worker killed between the enqueue
    and the terminal write leaves a row nothing else would ever advance, and the
    UI would poll it forever. Rows are aged from when they were last known to
    progress -- ``started_at`` once a worker picked one up, ``created_at`` while
    still queued.

    :param stale_after: How long a send may stay non-terminal before it is failed.
    :return: The number of sends failed.
    :raises HTTPBadRequestException: Propagated from the manager when a swept row
        cannot be written.
    """
    cutoff = utc_now() - stale_after
    async_session_maker = get_async_session_maker()
    async with async_session_maker() as session:
        rows = await AtwSendLogManager.list(
            session,
            col(AtwSendLog.status).in_(AtwSendStatusEnum.active_statuses()),
            func.coalesce(col(AtwSendLog.started_at), col(AtwSendLog.created_at))
            < cutoff,
        )
        for row in rows:
            await _persist(
                session,
                row,
                {**row.detail, "error": _WORKER_LOST_ERROR},
                status=AtwSendStatusEnum.FAILED,
                finished_at=utc_now(),
            )
    if rows:
        logger.info("Failed %d stale diagnostics send(s).", len(rows))
    return len(rows)
