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

"""Define the task history log reader with a dual-read legacy fallback."""

import base64
import binascii
import gzip
import json
import logging
import zlib
from collections import defaultdict
from collections.abc import AsyncGenerator, Generator
from itertools import product

from sqlmodel.ext.asyncio.session import AsyncSession

from app.tasks.crud import TaskHistoryLogManager
from app.tasks.logs.constants import TAIL_SCAN_MAX_CHUNKS
from app.tasks.models import (
    TaskHistory,
    TaskLog,
    TaskLogType,
)

logger = logging.getLogger(__name__)

TailOffsets = dict[str, dict[TaskLogType, int]]
TAIL_SCAN_MAX_BYTES = 64 * 1024 * 1024
NEWLINE_BYTE = ord("\n")
NEWLINE_BYTES = b"\n"


def _scan_tail_boundaries_in_bytes(
    content: bytes,
    base_offset: int,
    lines_remaining: int,
    *,
    at_stream_end: bool,
) -> tuple[int, int | None]:
    r"""Count newline boundaries in ``content`` from the end toward the start.

    Treats ``content`` as the suffix of a logical stream. Only the final byte of
    the whole stream may be a closing ``\n`` that does not start a new line (GNU
    ``tail -n`` semantics). Returns once the ``lines_remaining``\ th boundary
    from the end is found.

    :param content: UTF-8 encoded bytes to scan (newest suffix first).
    :type content: bytes
    :param base_offset: User-facing byte offset where ``content`` begins.
    :type base_offset: int
    :param lines_remaining: Trailing line boundaries still to locate.
    :type lines_remaining: int
    :param at_stream_end: Whether ``content`` ends at the logical stream end.
    :type at_stream_end: bool
    :return: Updated ``lines_remaining`` and tail offset if found.
    :rtype: tuple[int, int | None]
    """
    search_end = len(content)
    if at_stream_end and search_end > 0 and content[search_end - 1] == NEWLINE_BYTE:
        at_stream_end = False
        search_end -= 1

    while lines_remaining > 0 and search_end > 0:
        newline_index = content.rfind(NEWLINE_BYTES, 0, search_end)
        if newline_index < 0:
            break
        lines_remaining -= 1
        if lines_remaining == 0:
            return lines_remaining, base_offset + newline_index + 1
        search_end = newline_index

    return lines_remaining, None


def byte_offset_for_last_n_lines(content: bytes, line_count: int) -> int:
    r"""Return the byte offset where the last ``line_count`` lines begin.

    Lines are delimited by ``\n``. A single trailing newline closes the final
    line without starting an extra empty one (GNU ``tail -n`` semantics). When
    fewer than ``line_count`` lines exist, returns ``0`` so the full content is
    included.

    Scans backwards and stops once the target line boundary is found.

    :param content: UTF-8 encoded log bytes.
    :type content: bytes
    :param line_count: Number of trailing lines to retain.
    :type line_count: int
    :return: Byte offset at which the tail window starts.
    :rtype: int
    """
    if line_count <= 0 or not content:
        return 0

    _, tail_offset = _scan_tail_boundaries_in_bytes(
        content,
        0,
        line_count,
        at_stream_end=True,
    )
    return 0 if tail_offset is None else tail_offset


def _utf8_byte_offset_to_char_index(text: str, byte_offset: int) -> int:
    """Map a UTF-8 byte offset to a Python string index.

    Legacy ``tracking["task_logs"]`` blobs are sliced as ``str`` values; tail
    boundaries are computed on encoded bytes and must be converted before use.

    :param text: The decoded log text.
    :type text: str
    :param byte_offset: Byte offset into ``text.encode("utf-8")``.
    :type byte_offset: int
    :return: Character index equivalent to ``byte_offset``.
    :rtype: int
    """
    if byte_offset <= 0:
        return 0
    encoded = text.encode("utf-8")
    if byte_offset >= len(encoded):
        return len(text)
    return len(encoded[:byte_offset].decode("utf-8"))


async def _compute_stream_tail_offset(
    session: AsyncSession,
    task_history_id: int,
    stream_source: str,
    stream: TaskLogType,
    tail_lines: int,
) -> int:
    """Scan one stream's chunks newest-first and return its tail byte offset.

    Chunks are treated as one logical byte stream. Newline boundaries are
    located by scanning backwards across chunk rows; GNU ``tail -n``'s
    trailing-newline rule applies only to the final byte of the stream.

    :param session: The SQLAlchemy asynchronous session.
    :type session: AsyncSession
    :param task_history_id: The ``TaskHistory`` identifier.
    :type task_history_id: int
    :param stream_source: The execution step name.
    :type stream_source: str
    :param stream: The log stream being scanned.
    :type stream: TaskLogType
    :param tail_lines: Number of trailing lines to retain.
    :type tail_lines: int
    :return: The tail byte offset for the stream.
    :rtype: int
    """
    lines_remaining = tail_lines
    tail_byte_offset: int | None = None
    chunks_scanned = 0
    bytes_scanned = 0
    oldest_scanned_start: int | None = None
    at_stream_end = True

    async for chunk in TaskHistoryLogManager.iter_chunks_reverse(
        session,
        task_history_id,
        source=stream_source,
        stream=stream,
    ):
        if (
            chunks_scanned >= TAIL_SCAN_MAX_CHUNKS
            or bytes_scanned >= TAIL_SCAN_MAX_BYTES
        ):
            logger.debug(
                "taskhistory_log tail scan budget exhausted",
                extra={
                    "event": "taskhistory_log_tail_scan_budget",
                    "task_history_id": task_history_id,
                    "source": stream_source,
                    "stream": stream.value,
                    "chunks_scanned": chunks_scanned,
                    "bytes_scanned": bytes_scanned,
                },
            )
            if tail_byte_offset is None and oldest_scanned_start is not None:
                tail_byte_offset = oldest_scanned_start
            break

        chunks_scanned += 1
        content_bytes = chunk.content.encode("utf-8")
        bytes_scanned += len(content_bytes)
        oldest_scanned_start = (
            chunk.start_offset
            if oldest_scanned_start is None
            else min(oldest_scanned_start, chunk.start_offset)
        )

        lines_remaining, found_offset = _scan_tail_boundaries_in_bytes(
            content_bytes,
            chunk.start_offset,
            lines_remaining,
            at_stream_end=at_stream_end,
        )
        at_stream_end = False
        if found_offset is not None:
            tail_byte_offset = found_offset
            break

    return 0 if tail_byte_offset is None else tail_byte_offset


async def compute_tail_offsets_from_chunks(
    session: AsyncSession,
    task_history_id: int,
    tail_lines: int,
    *,
    source: str | None,
) -> TailOffsets:
    """Derive per-stream start offsets that retain only the last ``tail_lines``.

    Scans chunk rows newest-first per ``(source, stream)`` pair. A scan budget
    caps work on pathological logs; when exhausted, the oldest scanned chunk
    boundary is used as a best-effort tail.

    :param session: The SQLAlchemy asynchronous session.
    :type session: AsyncSession
    :param task_history_id: The ``TaskHistory`` identifier.
    :type task_history_id: int
    :param tail_lines: Number of trailing lines to retain per stream.
    :type tail_lines: int
    :param source: Optional step filter.
    :type source: str | None
    :return: Tail-derived byte offsets keyed by step and stream.
    :rtype: dict[str, dict[TaskLogType, int]]
    """
    tail_offsets: TailOffsets = {}
    stream_keys = await TaskHistoryLogManager.list_stream_keys(
        session, task_history_id, source=source
    )
    for stream_source, stream in stream_keys:
        tail_byte_offset = await _compute_stream_tail_offset(
            session,
            task_history_id,
            stream_source,
            stream,
            tail_lines,
        )
        if tail_byte_offset > 0:
            tail_offsets.setdefault(stream_source, {})[stream] = tail_byte_offset

    return tail_offsets


def compute_tail_offsets_legacy(
    task_history: TaskHistory,
    tail_lines: int,
    *,
    source: str | None,
) -> TailOffsets:
    """Derive tail offsets from the legacy ``tracking["task_logs"]`` blob.

    Offsets are character indices into each step's ``str`` log payload so
    :func:`iter_legacy_blob` can slice ``msg[start:]`` correctly.

    :param task_history: The task history whose legacy logs should be inspected.
    :type task_history: TaskHistory
    :param tail_lines: Number of trailing lines to retain per stream.
    :type tail_lines: int
    :param source: Optional step filter.
    :type source: str | None
    :return: Tail-derived byte offsets keyed by step and stream.
    :rtype: dict[str, dict[TaskLogType, int]]
    """
    tail_offsets: TailOffsets = {}
    task_logs = decompress_legacy_logs(task_history)
    if source is not None:
        task_logs = {source: task_logs.get(source, {})}
    for cur_step, log in task_logs.items():
        for log_type in TaskLogType:
            msg = log.get(log_type) or log.get(log_type.value) or ""
            if not msg:
                continue
            tail_byte_offset = byte_offset_for_last_n_lines(
                msg.encode("utf-8"), tail_lines
            )
            tail_char_offset = _utf8_byte_offset_to_char_index(msg, tail_byte_offset)
            if tail_char_offset > 0:
                tail_offsets.setdefault(cur_step, {})[log_type] = tail_char_offset
    return tail_offsets


def merge_tail_offsets(
    offsets: defaultdict[str, dict[str | TaskLogType, int]],
    tail_offsets: TailOffsets,
) -> None:
    """Merge tail offsets into ``offsets``, preferring the larger byte position.

    When both a resume ``start_offsets`` entry and a tail-derived offset exist
    for the same stream, the larger offset wins so already-delivered content is
    never re-streamed.

    :param offsets: Mutable per-step/per-stream offset map.
    :type offsets: defaultdict[str, dict[str | TaskLogType, int]]
    :param tail_offsets: Tail-derived offsets keyed by step and stream.
    :type tail_offsets: dict[str, dict[TaskLogType, int]]
    """
    for step, streams in tail_offsets.items():
        for stream, tail_offset in streams.items():
            offsets[step][stream] = max(offsets[step].get(stream, 0), tail_offset)


def has_legacy_logs(task_history: TaskHistory) -> bool:
    """Return ``True`` when ``tracking["task_logs"]`` has any non-empty content.

    Cheap predicate variant of :func:`decompress_legacy_logs` for callers
    that only need a boolean -- skips the base64/gzip/JSON decode so
    list endpoints stay O(N) in row count instead of
    O(total_legacy_log_bytes). Treats a corrupted blob as "has logs"
    because the string is non-empty; the download endpoint will surface
    the decode error to the user.

    :param task_history: The task history to check.
    :type task_history: TaskHistory
    :return: Whether any legacy log content exists in ``tracking``.
    :rtype: bool
    """
    tracking = task_history.execution_request.tracking or {}
    return bool(tracking.get("task_logs"))


def decompress_legacy_logs(task_history: TaskHistory) -> dict:
    """Return the decompressed legacy ``tracking["task_logs"]`` payload.

    Handles both historical encodings: the base64+gzip string produced by old
    Nomad syncs and the plain dict stored by some early Celery paths. Returns
    an empty dict when no legacy content exists, and also returns an empty
    dict (with a structured warning) when the blob is corrupted — a garbled
    pre-migration record must not crash the sync loop.

    :param task_history: The task history whose legacy logs should be
        decompressed.
    :type task_history: TaskHistory
    :return: The legacy ``{step: {log_type: text, f"{log_type}_last_offset": int}}``
        dict, or an empty dict when no legacy content exists or the blob
        cannot be decoded.
    :rtype: dict
    """
    tracking = task_history.execution_request.tracking or {}
    logs = tracking.get("task_logs") or {}
    if isinstance(logs, str):
        try:
            return json.loads(gzip.decompress(base64.b64decode(logs)))
        except (
            binascii.Error,
            zlib.error,
            json.JSONDecodeError,
            UnicodeDecodeError,
            OSError,
        ):
            logger.warning(
                "taskhistory_log legacy decode failed",
                extra={
                    "event": "taskhistory_log_legacy_decode_failed",
                    "task_history_id": task_history.id,
                },
            )
            return {}
    return logs


async def iter_task_history_logs(
    session: AsyncSession,
    task_history: TaskHistory,
    start_offsets: dict[str, dict[str, int]] | None = None,
    *,
    source: str | None = None,
    tail_lines: int | None = None,
) -> AsyncGenerator[TaskLog]:
    """Yield ``TaskLog`` records for a finished task history.

    When the chunk store has any row for ``task_history``, chunks are streamed
    from it in ``(source, stream, start_offset)`` order. Otherwise, a
    structured warning is logged and the legacy ``tracking["task_logs"]`` blob
    is decoded and yielded — this is the dual-read fallback used while
    pre-migration records are still in the database.

    :param session: The SQLAlchemy asynchronous session. Must have
        ``execution_request`` undeferred when the fallback path might run.
    :type session: AsyncSession
    :param task_history: The task history to read logs for. Must already have
        ``execution_request`` loaded.
    :type task_history: TaskHistory
    :param start_offsets: Optional per-step/per-stream start offsets. Chunks or
        bytes below the offset are skipped; the first partial chunk is trimmed
        so callers resume exactly where they left off.
    :type start_offsets: dict[str, dict[str, int]] | None
    :param source: Optional step filter. When set, only logs for the matching
        source are yielded.
    :type source: str | None
    :param tail_lines: Optional cap on trailing lines per stream. When set with
        ``start_offsets``, the larger byte offset per stream wins so resume
        never re-streams already-delivered content.
    :type tail_lines: int | None
    :return: An async generator yielding ``TaskLog`` records in
        ``(source, stream, offset)`` order.
    :rtype: AsyncGenerator[TaskLog, None]
    """
    offsets: defaultdict[str, dict[str | TaskLogType, int]] = defaultdict(
        dict, start_offsets or {}
    )
    has_chunks = await TaskHistoryLogManager.exists(
        session, task_history_id=task_history.id
    )
    if tail_lines is not None and tail_lines > 0:
        if has_chunks:
            tail_offsets = await compute_tail_offsets_from_chunks(
                session, task_history.id, tail_lines, source=source
            )
        else:
            tail_offsets = compute_tail_offsets_legacy(
                task_history, tail_lines, source=source
            )
        merge_tail_offsets(offsets, tail_offsets)
    if has_chunks:
        async for chunk in TaskHistoryLogManager.iter_chunks(
            session, task_history.id, source=source
        ):
            start_offset = offsets[chunk.source].get(chunk.stream, 0)
            if chunk.end_offset <= start_offset:
                continue
            if chunk.start_offset >= start_offset:
                yield TaskLog(
                    step=chunk.source,
                    type=chunk.stream,
                    msg=chunk.content,
                    offset=chunk.end_offset,
                )
                continue
            trim = start_offset - chunk.start_offset
            trimmed = chunk.content.encode("utf-8")[trim:].decode(
                "utf-8", errors="replace"
            )
            yield TaskLog(
                step=chunk.source,
                type=chunk.stream,
                msg=trimmed,
                offset=chunk.end_offset,
            )
        return

    logger.warning(
        "taskhistory_log dual-read fallback used",
        extra={
            "event": "taskhistory_log_fallback",
            "task_history_id": task_history.id,
        },
    )
    for log in iter_legacy_blob(task_history, offsets, source=source):
        yield log


def iter_legacy_blob(
    task_history: TaskHistory,
    offsets: defaultdict[str, dict[str, int]],
    *,
    source: str | None,
    chunk_size: int = 65536,
) -> Generator[TaskLog]:
    """Yield ``TaskLog`` records from the legacy ``tracking["task_logs"]`` blob.

    Mirrors the behavior of the removed ``TaskHistory.iter_logs`` method so the
    fallback path keeps bit-for-bit compatibility with pre-migration records.
    """
    task_logs = decompress_legacy_logs(task_history)
    if source is not None:
        task_logs = {source: task_logs.get(source, {})}
    for (cur_step, log), log_type in product(task_logs.items(), TaskLogType):
        msg = log.get(log_type) or log.get(log_type.value) or ""
        start = offsets[cur_step].get(log_type, 0)
        for chunk_start in range(start, len(msg), chunk_size):
            chunk_end = chunk_start + chunk_size
            yield TaskLog(
                step=cur_step,
                type=log_type,
                msg=msg[chunk_start:chunk_end],
                offset=chunk_end,
            )
