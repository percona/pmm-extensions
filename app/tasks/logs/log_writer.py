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

"""Define the append-only task history log writer and legacy backfill helper."""

import logging
from datetime import datetime, UTC

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import utc_now
from app.tasks.crud import (
    TaskHistoryLogManager,
    TaskHistoryLogStateManager,
    TaskHistoryManager,
)
from app.tasks.models import (
    LogCaptureStatusEnum,
    TaskHistoryLogState,
    TaskLogType,
)

logger = logging.getLogger(__name__)

CHUNK_BYTES = 128 * 1024
MIN_FLUSH = 16 * 1024
MAX_AGE_SEC = 120
_MAX_VERSION_RETRIES = 5
_UTF8_MAX_LOOKBACK = 3
_UTF8_ASCII_CEILING = 0x80
_UTF8_LEADING_BYTE_FLOOR = 0xC0


class LogWriterConflictError(RuntimeError):
    """Raise when the writer cannot converge within ``_MAX_VERSION_RETRIES``."""


class TaskHistoryLogWriter:
    """Append-only writer that stages bytes and flushes size-bounded chunks.

    The writer owns a single ``TaskHistoryLogState`` row per
    ``(task_history_id, source, stream)`` triple. Callers pass the new bytes
    produced since the previous cycle; the writer appends them to the staging
    buffer, splits the buffer into ``CHUNK_BYTES`` chunks, and flushes any
    remainder that either crosses ``MIN_FLUSH``, hits ``MAX_AGE_SEC``, or is
    force-flushed by the caller. Updates to the state row are serialised with
    an optimistic ``version`` column.
    """

    @classmethod
    async def append(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        source: str,
        stream: TaskLogType,
        new_bytes: bytes,
        force_flush: bool = False,
        producer_offset_after: int | None = None,
        producer_fetch_offset_after: int | None = None,
        producer_epoch: int | None = None,
    ) -> None:
        """Persist ``new_bytes`` for the given ``(task_history_id, source, stream)``.

        :param session: The SQLAlchemy asynchronous session. The writer commits
            on success so callers are responsible only for rollback on failure.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name (Nomad task name or
            ``"execution"`` for Celery callables).
        :param stream: The log stream (stdout or stderr) being written.
        :param new_bytes: The new bytes to append since the last successful call.
        :param force_flush: Force-flush the remainder buffer even when below
            ``MIN_FLUSH``. Terminal syncs pass ``True`` to drain staging.
        :param producer_offset_after: The producer-relative byte offset at the
            end of ``new_bytes``. When provided, the state row's
            ``producer_offset`` is advanced atomically with the flush.
        :param producer_fetch_offset_after: The raw producer-space fetch offset
            for the next read. When provided, the row's ``producer_fetch_offset``
            is advanced atomically with the flush; when ``None`` the existing
            value is preserved so callers that do not track a fetch cursor do
            not disturb it.
        :param producer_epoch: The producer-generation stamp the bytes belong
            to (for example a Nomad allocation ``CreateIndex``). When it is
            *older* than the current producer epoch the write is discarded —
            the bytes come from a producer the frontier has already moved past
            (a sync that overlapped a reschedule) and appending them would
            corrupt the stream. For an existing row the comparison is against
            that row's per-stream epoch; on the first-insert path (no row yet)
            it is against the task-level high-water mark stamped at the last
            frontier reset. ``None`` leaves the row's epoch untouched.
        :raises LogWriterConflictError: If the optimistic-locking retries are
            exhausted without converging on a successful update.
        """
        for _ in range(_MAX_VERSION_RETRIES):
            state = await TaskHistoryLogStateManager.get_for_stream(
                session, task_history_id, source, stream
            )
            is_new = state is None
            if state is None:
                state = TaskHistoryLogStateManager.build_default(
                    task_history_id, source, stream
                )

            if producer_epoch is not None:
                # First insert has no per-stream row yet; guard against the
                # task-level high-water mark, not the transient row's ``0``.
                guard_epoch = state.producer_epoch
                if is_new:
                    # Take the row lock so a concurrent frontier reset serialises
                    # instead of racing (first-insert TOCTOU).
                    guard_epoch = await TaskHistoryManager.get_log_producer_epoch(
                        session, task_history_id, for_update=True
                    )
                if producer_epoch < guard_epoch:
                    # Stale write from a superseded producer; drop it, releasing
                    # any first-insert lock so it doesn't pin the row.
                    await cls._release_first_insert_lock(session, is_new=is_new)
                    return

            effective_bytes = cls._effective_new_bytes(
                state, new_bytes, producer_offset_after
            )
            if (
                not effective_bytes
                and not force_flush
                and not state.staging
                and producer_offset_after is not None
                and state.producer_offset >= producer_offset_after
            ):
                # No-op early return; same first-insert lock-release invariant as
                # the stale-discard path above.
                await cls._release_first_insert_lock(session, is_new=is_new)
                return

            now = utc_now()
            staging = state.staging + effective_bytes
            persisted_offset = state.persisted_offset
            previous_persisted_offset = persisted_offset

            staging, persisted_offset = await cls._flush_full_chunks(
                session=session,
                task_history_id=task_history_id,
                source=source,
                stream=stream,
                staging=staging,
                persisted_offset=persisted_offset,
                now=now,
            )

            if cls._should_flush_remainder(
                staging=staging,
                force_flush=force_flush,
                staging_updated_at=state.staging_updated_at if not is_new else None,
                now=now,
            ):
                staging, persisted_offset = await cls._flush_remainder(
                    session=session,
                    task_history_id=task_history_id,
                    source=source,
                    stream=stream,
                    staging=staging,
                    persisted_offset=persisted_offset,
                    now=now,
                )

            new_producer = (
                producer_offset_after
                if producer_offset_after is not None
                else state.producer_offset
            )
            new_fetch = (
                producer_fetch_offset_after
                if producer_fetch_offset_after is not None
                else state.producer_fetch_offset
            )
            new_producer_epoch = (
                producer_epoch if producer_epoch is not None else state.producer_epoch
            )
            new_version = state.version + 1

            applied = await cls._persist_state(
                session=session,
                task_history_id=task_history_id,
                source=source,
                stream=stream,
                is_new=is_new,
                new_version=new_version,
                old_version=state.version,
                persisted_offset=persisted_offset,
                producer_offset=new_producer,
                producer_fetch_offset=new_fetch,
                producer_epoch=new_producer_epoch,
                staging=staging,
                now=now,
            )
            if applied:
                await cls._evict_over_cap(
                    session,
                    task_history_id=task_history_id,
                    source=source,
                    stream=stream,
                    persisted_offset=persisted_offset,
                    previous_persisted_offset=previous_persisted_offset,
                )
                await session.commit()
                return
            await session.rollback()

        raise LogWriterConflictError(
            f"TaskHistoryLogWriter: exhausted {_MAX_VERSION_RETRIES} retries for "
            f"task_history_id={task_history_id} source={source!r} stream={stream}"
        )

    @classmethod
    async def record_capture_status(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        source: str,
        stream: TaskLogType,
        capture_status: LogCaptureStatusEnum,
        producer_epoch: int | None = None,
    ) -> None:
        """Record how completely ``(source, stream)`` was captured.

        Creates the state row when none exists, which is what lets a reader
        tell a stream that produced no bytes from one whose bytes were lost:
        :meth:`append` returns early for a fresh empty stream, so without this
        the most common case leaves no row at all. The offsets and the staging
        bytes are re-written unchanged; the row's audit columns and ``version``
        advance as they do on any state write, so this is not a free-standing
        column update.

        :param session: The SQLAlchemy asynchronous session. The writer commits
            on success so callers are responsible only for rollback on failure.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name (Nomad task name or
            ``"execution"`` for Celery callables).
        :param stream: The log stream (stdout or stderr) being classified.
        :param capture_status: The verdict to record for this stream.
        :param producer_epoch: The producer-generation stamp the verdict
            describes (for example a Nomad allocation ``CreateIndex``). When it
            is *older* than the epoch the row (or, on the first-insert path, the
            task-level high-water mark) already carries, the write is discarded:
            it reports on a producer the frontier has moved past, and applying
            it would overwrite a newer producer's verdict. ``None`` leaves the
            row's epoch untouched.
        :raises LogWriterConflictError: If the optimistic-locking retries are
            exhausted without converging on a successful update.
        """
        for _ in range(_MAX_VERSION_RETRIES):
            state = await TaskHistoryLogStateManager.get_for_stream(
                session, task_history_id, source, stream
            )
            is_new = state is None
            if state is None:
                state = TaskHistoryLogStateManager.build_default(
                    task_history_id, source, stream
                )

            if producer_epoch is not None:
                guard_epoch = state.producer_epoch
                if is_new:
                    guard_epoch = await TaskHistoryManager.get_log_producer_epoch(
                        session, task_history_id, for_update=True
                    )
                if producer_epoch < guard_epoch:
                    await cls._release_first_insert_lock(session, is_new=is_new)
                    return

            new_producer_epoch = (
                producer_epoch if producer_epoch is not None else state.producer_epoch
            )
            applied = await cls._persist_state(
                session=session,
                task_history_id=task_history_id,
                source=source,
                stream=stream,
                is_new=is_new,
                new_version=state.version + 1,
                old_version=state.version,
                persisted_offset=state.persisted_offset,
                producer_offset=state.producer_offset,
                producer_fetch_offset=state.producer_fetch_offset,
                producer_epoch=new_producer_epoch,
                staging=state.staging,
                now=utc_now(),
                capture_status=capture_status,
            )

            if applied:
                await session.commit()
                return
            await session.rollback()

        raise LogWriterConflictError(
            f"TaskHistoryLogWriter: exhausted {_MAX_VERSION_RETRIES} capture-status "
            f"retries for task_history_id={task_history_id} source={source!r} "
            f"stream={stream}"
        )

    @classmethod
    async def drain_and_reset_producer_frontier(
        cls,
        session: AsyncSession,
        task_history_id: int,
        *,
        new_producer_epoch: int,
    ) -> None:
        """Flush every stream's staging buffer and reset the fetch frontier.

        Called when the executor reschedules onto a follow-up producer (for
        example a Nomad allocation): the new producer's log file starts at byte
        0, so the producer-relative cursors (``producer_offset`` and
        ``producer_fetch_offset``) from the previous producer must be cleared
        and ``producer_epoch`` advanced, and the leftover staging bytes from
        the previous producer must be emitted as their own chunk instead of
        being concatenated with the new producer's bytes.

        :param session: The SQLAlchemy asynchronous session.
        :param task_history_id: The ``TaskHistory`` identifier whose state
            rows should be drained and reset.
        :param new_producer_epoch: The producer epoch the frontier is being
            reset onto (for example a Nomad allocation ``CreateIndex``).
        """
        # Lock the TaskHistory row before touching any log/state rows so this
        # reset and a concurrent first-insert append serialise in the same order
        # (TaskHistory first), closing the first-insert TOCTOU.
        await TaskHistoryManager.get_log_producer_epoch(
            session, task_history_id, for_update=True
        )
        rows = await TaskHistoryLogStateManager.list_for_task(session, task_history_id)
        for row in rows:
            if row.staging:
                now = utc_now()
                _, new_persisted = await cls._flush_remainder(
                    session=session,
                    task_history_id=task_history_id,
                    source=row.source,
                    stream=row.stream,
                    staging=row.staging,
                    persisted_offset=row.persisted_offset,
                    now=now,
                )
                drained_bytes = len(row.staging)
                applied = await cls._persist_state(
                    session=session,
                    task_history_id=task_history_id,
                    source=row.source,
                    stream=row.stream,
                    is_new=False,
                    new_version=row.version + 1,
                    old_version=row.version,
                    persisted_offset=new_persisted,
                    producer_offset=row.producer_offset,
                    producer_fetch_offset=row.producer_fetch_offset,
                    producer_epoch=row.producer_epoch,
                    staging=b"",
                    now=now,
                )
                if not applied:
                    await session.rollback()
                    raise LogWriterConflictError(
                        "TaskHistoryLogWriter: lost version race while draining "
                        f"task_history_id={task_history_id} source={row.source!r} "
                        f"stream={row.stream}"
                    )
                logger.info(
                    "taskhistory_log producer switch drained",
                    extra={
                        "event": "taskhistory_log_producer_switch_drained",
                        "task_history_id": task_history_id,
                        "source": row.source,
                        "stream": row.stream.value,
                        "drained_bytes": drained_bytes,
                    },
                )
        await TaskHistoryLogStateManager.reset_producer_frontier(
            session, task_history_id, new_producer_epoch=new_producer_epoch
        )
        # Stamp the task-level high-water mark in the same transaction as the
        # per-stream reset so a first-insert guard (no per-stream row yet) has a
        # current epoch to check against.
        await TaskHistoryManager.bump_log_producer_epoch(
            session, task_history_id, new_producer_epoch=new_producer_epoch
        )
        await session.commit()

    @staticmethod
    async def _release_first_insert_lock(
        session: AsyncSession, *, is_new: bool
    ) -> None:
        """Release the first-insert ``FOR UPDATE`` lock before an early return.

        The first-insert path locks the ``TaskHistory`` row (``for_update``) to
        serialise against a concurrent frontier reset. Any ``append`` exit that
        returns without ending the transaction must roll back first, else a
        long-lived ``writer_session`` pins the row and stalls resets/other
        writers. A no-op when ``is_new`` is false — the existing-row paths take no
        such lock.

        :param session: The writer session that may hold the first-insert lock.
        :param is_new: Whether this call took the first-insert ``FOR UPDATE`` lock.
        """
        if is_new:
            await session.rollback()

    @staticmethod
    def _effective_new_bytes(
        state: TaskHistoryLogState,
        new_bytes: bytes,
        producer_offset_after: int | None,
    ) -> bytes:
        """Return the slice of ``new_bytes`` not yet covered by the state row.

        When ``producer_offset_after`` is supplied, the caller guarantees that
        ``new_bytes`` maps to the range
        ``[producer_offset_after - len(new_bytes), producer_offset_after]``;
        any bytes already covered by ``state.producer_offset`` are dropped so
        retries after a concurrent update do not double-write.

        :param state: The current state row for this stream.
        :type state: TaskHistoryLogState
        :param new_bytes: The bytes the caller wants to append.
        :type new_bytes: bytes
        :param producer_offset_after: The producer-relative offset at the end
            of ``new_bytes``, or ``None`` when the producer has no concept of
            offsets (Celery callables).
        :type producer_offset_after: int | None
        :return: The bytes that still need to be persisted.
        :rtype: bytes
        """
        if producer_offset_after is None:
            return new_bytes
        producer_offset_before = producer_offset_after - len(new_bytes)
        skip = max(0, state.producer_offset - producer_offset_before)
        if skip >= len(new_bytes):
            return b""
        return new_bytes[skip:]

    @classmethod
    async def _flush_full_chunks(
        cls,
        *,
        session: AsyncSession,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        staging: bytes,
        persisted_offset: int,
        now: datetime,
    ) -> tuple[bytes, int]:
        """Drain ``staging`` into ``CHUNK_BYTES`` chunk rows.

        :param session: The SQLAlchemy asynchronous session.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: The execution step name.
        :type source: str
        :param stream: The log stream.
        :type stream: TaskLogType
        :param staging: The current staging bytes buffer.
        :type staging: bytes
        :param persisted_offset: The current user-facing persisted offset.
        :type persisted_offset: int
        :param now: The flush timestamp used for ``created_at``.
        :type now: datetime
        :return: The remaining staging bytes and the advanced persisted offset.
        :rtype: tuple[bytes, int]
        """
        while len(staging) >= CHUNK_BYTES:
            chunk_end = cls._utf8_safe_split(staging, CHUNK_BYTES)
            chunk = staging[:chunk_end]
            await TaskHistoryLogManager.insert_chunk_idempotent(
                session,
                task_history_id=task_history_id,
                source=source,
                stream=stream,
                start_offset=persisted_offset,
                chunk=chunk,
                now=now,
            )
            persisted_offset += len(chunk)
            staging = staging[chunk_end:]
        return staging, persisted_offset

    @staticmethod
    def _utf8_safe_split(staging: bytes, target: int) -> int:
        """Return a split index ``<= target`` that aligns with a UTF-8 boundary.

        Walk back at most :data:`_UTF8_MAX_LOOKBACK` bytes from ``target`` to
        find the first byte that starts a UTF-8 codepoint — either an ASCII
        byte (``< 0x80``) or a leading byte (``>= 0xC0``). When no aligned
        split is found inside the lookback window — which can only happen if
        ``staging[target - 3 : target]`` contains nothing but continuation
        bytes, i.e. the boundary falls inside a single codepoint whose start
        is further back than the lookback window — return ``target`` so the
        writer still makes progress. ``errors="replace"`` in the decode path
        handles the resulting sliver without raising.

        :param staging: The staging buffer being flushed.
        :type staging: bytes
        :param target: The preferred split index, normally ``CHUNK_BYTES``.
        :type target: int
        :return: The index at which to split ``staging`` into a chunk.
        :rtype: int
        """
        if target >= len(staging):
            return target
        for offset in range(_UTF8_MAX_LOOKBACK + 1):
            candidate = target - offset
            if candidate <= 0:
                break
            byte = staging[candidate]
            if byte < _UTF8_ASCII_CEILING or byte >= _UTF8_LEADING_BYTE_FLOOR:
                return candidate
        return target

    @classmethod
    async def _flush_remainder(
        cls,
        *,
        session: AsyncSession,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        staging: bytes,
        persisted_offset: int,
        now: datetime,
    ) -> tuple[bytes, int]:
        """Flush the remaining ``staging`` bytes as a single chunk row.

        :param session: The SQLAlchemy asynchronous session.
        :type session: AsyncSession
        :param task_history_id: The ``TaskHistory`` identifier.
        :type task_history_id: int
        :param source: The execution step name.
        :type source: str
        :param stream: The log stream.
        :type stream: TaskLogType
        :param staging: The remaining staging bytes to flush.
        :type staging: bytes
        :param persisted_offset: The current user-facing persisted offset.
        :type persisted_offset: int
        :param now: The flush timestamp used for ``created_at``.
        :type now: datetime
        :return: An empty staging buffer and the advanced persisted offset.
        :rtype: tuple[bytes, int]
        """
        await TaskHistoryLogManager.insert_chunk_idempotent(
            session,
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            start_offset=persisted_offset,
            chunk=staging,
            now=now,
        )
        return b"", persisted_offset + len(staging)

    @staticmethod
    def _should_flush_remainder(
        *,
        staging: bytes,
        force_flush: bool,
        staging_updated_at: datetime | None,
        now: datetime,
    ) -> bool:
        """Return whether the remaining ``staging`` should be flushed.

        :param staging: The current staging buffer.
        :type staging: bytes
        :param force_flush: Whether the caller requested a forced flush.
        :type force_flush: bool
        :param staging_updated_at: When the staging buffer was last modified,
            or ``None`` for a brand-new state row.
        :type staging_updated_at: datetime | None
        :param now: The current flush timestamp.
        :type now: datetime
        :return: Whether the remainder should be flushed on this call.
        :rtype: bool
        """
        if not staging:
            return False
        if force_flush or len(staging) >= MIN_FLUSH:
            return True
        if staging_updated_at is not None:
            aware_staging_updated_at = (
                staging_updated_at.replace(tzinfo=UTC)
                if staging_updated_at.tzinfo is None
                else staging_updated_at
            )
            age = (now - aware_staging_updated_at).total_seconds()
            if age > MAX_AGE_SEC:
                return True
        return False

    @staticmethod
    async def _persist_state(
        *,
        session: AsyncSession,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        is_new: bool,
        new_version: int,
        old_version: int,
        persisted_offset: int,
        producer_offset: int,
        producer_fetch_offset: int,
        producer_epoch: int,
        staging: bytes,
        now: datetime,
        capture_status: LogCaptureStatusEnum | None = None,
    ) -> bool:
        """Insert or update the state row and return whether we won the race.

        :param session: The SQLAlchemy asynchronous session.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name.
        :param stream: The log stream.
        :param is_new: Whether the state row was freshly built (``True``) or
            loaded from the DB (``False``).
        :param new_version: The bumped version counter to write.
        :param old_version: The version observed on the loaded state row, or
            ``0`` for a new row.
        :param persisted_offset: The advanced user-facing persisted offset.
        :param producer_offset: The producer-relative offset to persist.
        :param producer_fetch_offset: The raw producer-space fetch offset to
            persist.
        :param producer_epoch: The producer-generation stamp to persist.
        :param staging: The remaining staging buffer to persist.
        :param now: The update timestamp.
        :param capture_status: The capture verdict to write, or ``None`` to
            leave the stored verdict untouched on an update and take the
            column's own default on an insert. Omitting it is what keeps a
            byte-appending write from downgrading a stream already recorded
            ``COMPLETE``.
        :return: Whether the insert or optimistic-locked update succeeded.
        """
        if is_new:
            insert_verdict = (
                {"capture_status": capture_status} if capture_status is not None else {}
            )
            return await TaskHistoryLogStateManager.insert_row_idempotent(
                session,
                task_history_id=task_history_id,
                source=source,
                stream=stream,
                persisted_offset=persisted_offset,
                producer_offset=producer_offset,
                producer_fetch_offset=producer_fetch_offset,
                producer_epoch=producer_epoch,
                staging=staging,
                version=new_version,
                now=now,
                **insert_verdict,
            )
        return await TaskHistoryLogStateManager.update_row_if_version(
            session,
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            old_version=old_version,
            new_version=new_version,
            persisted_offset=persisted_offset,
            producer_offset=producer_offset,
            producer_fetch_offset=producer_fetch_offset,
            producer_epoch=producer_epoch,
            staging=staging,
            now=now,
            capture_status=capture_status,
        )

    @classmethod
    async def _evict_over_cap(
        cls,
        session: AsyncSession,
        *,
        task_history_id: int,
        source: str,
        stream: TaskLogType,
        persisted_offset: int,
        previous_persisted_offset: int,
    ) -> None:
        """Drop oldest chunks beyond ``LOG_STREAM_CAP_BYTES`` for this stream.

        No-op unless this flush advanced ``persisted_offset`` and the stream
        exceeds the cap (``low_water > 0``). The delete is staged on ``session``
        without committing so it commits atomically with the flush and the
        version-CAS in :meth:`append`; a delete error therefore aborts the whole
        append rather than leaving a best-effort cap.

        :param session: The SQLAlchemy asynchronous session.
        :param task_history_id: The ``TaskHistory`` identifier.
        :param source: The execution step name.
        :param stream: The log stream (stdout or stderr).
        :param persisted_offset: The advanced post-flush persisted offset.
        :param previous_persisted_offset: The persisted offset before this flush.
        """
        if persisted_offset <= previous_persisted_offset:
            return
        from app.tasks import (  # noqa: PLC0415 - tasks.config imports nomad, whose models import this (cycle)
            config as tasks_config,
        )

        cap = tasks_config.tasks_settings.LOG_STREAM_CAP_BYTES
        low_water = persisted_offset - cap
        if low_water <= 0:
            return
        deleted = await TaskHistoryLogManager.delete_chunks_below_offset(
            session,
            task_history_id=task_history_id,
            source=source,
            stream=stream,
            max_end_offset=low_water,
            max_rows=tasks_config.tasks_settings.LOG_STREAM_EVICTION_MAX_ROWS,
        )
        if deleted:
            logger.debug(
                "taskhistory_log stream capped",
                extra={
                    "event": "taskhistory_log_stream_capped",
                    "task_history_id": task_history_id,
                    "source": source,
                    "stream": stream.value,
                    "evicted_rows": deleted,
                    "low_water": low_water,
                },
            )


async def backfill_legacy_logs(
    session: AsyncSession,
    task_history_id: int,
    legacy_logs: dict,
) -> None:
    """Migrate a legacy ``tracking["task_logs"]`` dict into the chunk store.

    Walk every ``(step, log_type)`` entry in ``legacy_logs`` and forward any
    content that does not yet have a ``TaskHistoryLogState`` row to
    :meth:`TaskHistoryLogWriter.append`. The per-stream existence check makes
    the helper idempotent under partial-crash recovery: a second call only
    picks up the streams that the first call did not finish migrating.

    :param session: The SQLAlchemy asynchronous session.
    :type session: AsyncSession
    :param task_history_id: The ``TaskHistory`` identifier.
    :type task_history_id: int
    :param legacy_logs: The decompressed legacy ``tracking["task_logs"]`` dict
        with the shape ``{step: {log_type: text, f"{log_type}_last_offset": int}}``.
    :type legacy_logs: dict
    """
    if not legacy_logs:
        return
    existing_states = await TaskHistoryLogStateManager.list_for_task(
        session, task_history_id
    )
    existing_keys = {(row.source, row.stream) for row in existing_states}
    for step, payload in legacy_logs.items():
        if not isinstance(payload, dict):
            continue
        for log_type in TaskLogType:
            if (step, log_type) in existing_keys:
                continue
            content = payload.get(log_type) or payload.get(log_type.value) or ""
            if not content:
                continue
            last_offset_key = f"{log_type.value}_last_offset"
            legacy_offset = int(
                payload.get(last_offset_key)
                or payload.get(f"{log_type}_last_offset")
                or len(content.encode("utf-8"))
            )
            await TaskHistoryLogWriter.append(
                session,
                task_history_id,
                source=step,
                stream=log_type,
                new_bytes=content.encode("utf-8"),
                force_flush=True,
                producer_offset_after=legacy_offset,
            )
