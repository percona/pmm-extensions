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

"""Report per-entity sync outcomes to the inventory service."""

import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from itertools import batched
from typing import Any, Final

from fastapi import HTTPException

from app.core.requests import RemoteAPI
from app.extensions.inventory import CreatedEntity
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.extensions.sync.constants import INVENTORY_PATH_SEGMENTS
from app.extensions.sync.exceptions import (
    SyncFailError,
    SyncInstanceAlreadyInProgressError,
    SyncItemAlreadyInProgressError,
)
from app.inventory.constants import SYNC_HEALTH_BULK_MAX_ENTRIES
from app.inventory.models import SyncOutcomeEnum

logger = logging.getLogger(__name__)


#: The exceptions whose full message may be stored on ``last_sync_error``.
#:
#: An allowlist rather than a denylist, and matched by exact type rather than by
#: ``isinstance``: a ``SyncError`` subclass added later must be opted in
#: deliberately instead of inheriting persistence from its base — a subclass is
#: free to interpolate remote context its base never did. Every member here
#: interpolates only sync bookkeeping — the ``SyncItem`` an attempt was for.
#: ``ExecutorHostNotFoundError`` is deliberately absent: its message carries the
#: whole executor-host map from the Tasks API, which the inventory read routes
#: would then expose to any authenticated caller.
_MESSAGE_SAFE_ERRORS: Final = frozenset(
    {
        SyncFailError,
        SyncInstanceAlreadyInProgressError,
        SyncItemAlreadyInProgressError,
    }
)


def _describe_sync_error(error: Exception) -> str:
    """Summarize an exception for storage on an entity's ``last_sync_error``.

    The field is durable and readable through the inventory read routes, so the
    summary carries a message only for the exceptions whose *exact* type is
    listed in :data:`_MESSAGE_SAFE_ERRORS`; a subclass of one is not a member.
    ``HTTPException`` is matched by ``isinstance`` instead because that branch
    reduces rather than admits, and a subclass of it must reduce too.
    Everything else is reduced to its type name: a
    third-party exception's message is arbitrary text this code never inspected
    — a ``ValidationError`` echoes the offending input, a driver error can carry
    a DSN — and no redaction pass can be trusted over an open set. Nothing is
    lost by the reduction: only the description is persisted, and the exception
    itself is re-raised out of :meth:`SyncHealthReporter.record` to the boundary
    that logs it with its traceback.

    The result is non-empty but not length-bounded; the inventory service
    truncates it to the column's contract when it stores it.

    :param error: The exception that ended the sync attempt.
    :return: A non-empty description carrying no caller-supplied text.
    """
    if isinstance(error, HTTPException):
        return f"{type(error).__name__}: HTTP {error.status_code}"
    if type(error) in _MESSAGE_SAFE_ERRORS:
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


class SyncHealthAttempt:
    """Track whether a sync attempt reached a real comparison against source.

    Defaults to not-compared so a path that forgets to mark it records nothing,
    which is the safe direction: an unwritten column reads as stale, never as
    freshly confirmed.

    :ivar compared: Whether the block held real source data and synced from it.
    """

    def __init__(self) -> None:
        self.compared = False

    def mark_compared(self) -> None:
        """Record that source data was in hand and the entity was synced from it."""
        self.compared = True


def _outcome_payload(attempted_at: datetime, error: Exception | None) -> dict[str, Any]:
    """Build the body the single-entity ``sync-health`` route takes.

    :param attempted_at: When the attempt began.
    :param error: The exception that ended the attempt, or None on success.
    :return: The JSON-ready body.
    """
    outcome = (
        {"outcome": SyncOutcomeEnum.SUCCESS}
        if error is None
        else {
            "outcome": SyncOutcomeEnum.FAILURE,
            "error": _describe_sync_error(error),
        }
    )
    return {"attempted_at": attempted_at.isoformat(), **outcome}


#: The success outcomes one open ``record`` block holds for the blocks nested in
#: it, per entity level, each entry already shaped as a bulk-route item.
_SuccessBuffer = defaultdict[SyncInventoryEntityTypeEnum, list[dict[str, Any]]]


class SyncHealthReporter:
    """Report per-entity sync outcomes to the inventory service.

    Mechanism only: which entity levels a syncer mirrors is policy, declared on
    the syncer class (``BaseSyncer.mirrors_entity_levels``) and handed in here.

    Successes are buffered rather than posted one by one: every open
    :meth:`record` block holds the successes of the blocks nested in it, and
    flushes them as one bulk call per entity level when it exits — so a
    schema's tables go in one call when the schema's block closes. A success
    with no enclosing block has no parent to wait for and posts at once.
    Failures are never buffered. The buffers form a stack that follows the
    syncers' strictly nested, sequential walk, so one reporter must not be
    entered from concurrent tasks.

    :param inventory_api: The inventory service client the outcomes post to.
    :param mirrored_levels: The entity levels whose attempts are recorded;
        every other level passes through unrecorded.
    """

    def __init__(
        self,
        inventory_api: RemoteAPI,
        mirrored_levels: frozenset[SyncInventoryEntityTypeEnum],
    ) -> None:
        self._inventory_api = inventory_api
        self._mirrored_levels = mirrored_levels
        self._open_buffers: list[_SuccessBuffer] = []

    @asynccontextmanager
    async def record(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity,
    ) -> AsyncGenerator[SyncHealthAttempt, None]:
        """Write the entity's sync-health columns from this attempt's outcome.

        Nests inside ``manage_sync_item`` so a failure is recorded before that
        boundary marks the SyncItem failed and raises under ``break_on_error``.
        A failure is recorded unconditionally and immediately — entering this
        block at a mirrored level *is* the genuine attempt, and a ``fetch_*``
        that raises is exactly the failure worth reporting, so a run that dies
        later has already written it. A success is recorded only once the block
        marked itself compared, which is what excludes the filtered-out early
        return that leaves ``manage_sync_item``'s clean-exit path
        indistinguishable from a real sync; it is handed to the enclosing block
        to flush with its siblings.

        Every block, mirrored or not, opens a buffer for the successes of the
        blocks nested in it, and flushes it on exit — a clean exit, an early
        return or an ``Exception`` alike, since each buffered success is a
        completed attempt whatever happens to its parent afterwards. Only an
        exit that is not an ``Exception`` (cancellation, interpreter shutdown)
        drops the buffer instead of awaiting a write, leaving those entities
        looking stale rather than falsely fresh.

        A ``SyncFailError`` is the exception: it can only come from a *nested*
        level, because this level's own boundary raises it after this block has
        already exited. The syncers walk to children from inside the parent's
        ``perform_*_sync``, so a child's failure passes through here — and
        whether it does at all depends on ``break_on_error``, since otherwise
        the child's own boundary swallows it. Attributing it to this entity
        would make the columns describe an identical outcome differently in the
        two modes, and would report a parent as failing whose own mirrored
        fields were confirmed. The child records its own failure on its own row.

        :param entity_type: The level being synced.
        :param created_entity: The entity being synced.
        :return: The marker the block flags once it holds real source data.
        """
        attempt = SyncHealthAttempt()
        mirrored = entity_type in self._mirrored_levels
        attempted_at = datetime.now(UTC)
        children: _SuccessBuffer = defaultdict(list)
        self._open_buffers.append(children)
        try:
            yield attempt
        except Exception as exc:
            await self._flush(self._close(children))
            if mirrored:
                if isinstance(exc, SyncFailError) and attempt.compared:
                    await self._record_success(
                        entity_type, created_entity, attempted_at
                    )
                else:
                    await self._post(entity_type, created_entity, attempted_at, exc)
            raise
        except BaseException:
            self._discard(self._close(children))
            raise
        await self._flush(self._close(children))
        if mirrored and attempt.compared:
            await self._record_success(entity_type, created_entity, attempted_at)

    def _close(self, buffer: _SuccessBuffer) -> _SuccessBuffer:
        """Remove a block's buffer from the stack of open ones.

        Matched by identity rather than ``list.remove``'s equality, under which
        every empty buffer is equal to every other.

        :param buffer: The buffer the exiting block opened.
        :return: The same buffer, for the caller to flush or discard.
        """
        for index in range(len(self._open_buffers) - 1, -1, -1):
            if self._open_buffers[index] is buffer:
                del self._open_buffers[index]
                break
        return buffer

    async def _record_success(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity,
        attempted_at: datetime,
    ) -> None:
        """Hand a success to the enclosing block, or post it with none open.

        :param entity_type: The level being reported.
        :param created_entity: The entity being reported.
        :param attempted_at: When this attempt began.
        """
        if not self._open_buffers:
            await self._post(entity_type, created_entity, attempted_at, None)
            return
        self._open_buffers[-1][entity_type].append(
            {"id": created_entity.id, **_outcome_payload(attempted_at, None)}
        )

    async def _flush(self, buffer: _SuccessBuffer) -> None:
        """Post a block's buffered successes, one bulk call per entity level.

        Best-effort as :meth:`_post` is: a failed call is logged and swallowed,
        leaving its entities looking stale rather than falsely fresh. A level
        holding more than one request may carry is split across calls.

        :param buffer: The successes to post.
        """
        for entity_type, outcomes in buffer.items():
            for chunk in batched(outcomes, SYNC_HEALTH_BULK_MAX_ENTRIES):
                try:
                    await self._inventory_api.post(
                        f"/{INVENTORY_PATH_SEGMENTS[entity_type]}/sync-health",
                        json=list(chunk),
                    )
                except Exception:
                    logger.exception(
                        "Failed to record sync health for %d %s entities",
                        len(chunk),
                        entity_type.name,
                    )

    @staticmethod
    def _discard(buffer: _SuccessBuffer) -> None:
        """Drop a block's buffered successes without writing them.

        :param buffer: The successes to drop.
        """
        for entity_type, outcomes in buffer.items():
            logger.warning(
                "Dropped sync health for %d %s entities: the sync was interrupted",
                len(outcomes),
                entity_type.name,
            )

    async def _post(
        self,
        entity_type: SyncInventoryEntityTypeEnum,
        created_entity: CreatedEntity,
        attempted_at: datetime,
        error: Exception | None,
    ) -> None:
        """Report one entity's sync outcome to the inventory service.

        Best-effort: a bookkeeping write must not fail an otherwise-healthy
        sync item, and losing one leaves the entity looking stale rather than
        falsely fresh.

        :param entity_type: The level being reported.
        :param created_entity: The entity being reported.
        :param attempted_at: When this attempt began, captured before the block
            ran so the inventory service orders by attempt, not by arrival. Read
            from the clock directly rather than through ``utc_now``, whose
            second truncation would hand two attempts within one second the same
            ordering key — and the service's guards admit an equal one, so the
            later arrival would win regardless of which attempt was newer.
        :param error: The exception that ended the attempt, or None on success.
        """
        try:
            await self._inventory_api.post(
                f"/{INVENTORY_PATH_SEGMENTS[entity_type]}/{created_entity.id}"
                "/sync-health",
                json=_outcome_payload(attempted_at, error),
            )
        except Exception:
            logger.exception(
                "Failed to record sync health for %s %s",
                entity_type.name,
                created_entity.id,
            )
