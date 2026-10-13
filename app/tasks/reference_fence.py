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

"""Fence task writes that can name an inventory id against inventory collection.

Inventory collection reads every task and beat schedule naming a service, then
deletes the retired services none of them names. A task or schedule written
between that read and that delete could name a service the delete removes. The
fence closes that gap with one advisory lock in the tasks database: each Tasks
API write that can persist such a reference holds it *shared* across the write,
and each collection batch holds it *exclusive* from its read through its delete.
Writers never wait for each other's locks, only for a batch that holds the
fence or is queued for it; a batch waits for the writes already under way, so it
reads them once they have committed. Each holder's lock is on a connection of
its own, outside the pool, so the writers one process lets hold the fence at once
are capped at :data:`REFERENCE_FENCE_MAX_WRITERS`, keeping those connections
inside the side-car's connection budget.

The tasks service owns the fence because it owns the tables the references live
in. On a database without advisory locks both holders run unfenced, as
:func:`app.core.db.utils.pg_advisory_xact_lock` documents.
"""

import asyncio
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, AsyncExitStack
from datetime import timedelta
from typing import Final

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.utils import (
    advisory_lock_key,
    AdvisoryLockHandle,
    AdvisoryLockTimeoutError,
    pg_advisory_xact_lock,
)
from app.core.exceptions import HTTPServiceUnavailableException

#: Advisory-lock space of the reference fence. Distinct from
#: ``SYNC_RUN_LOCK_NAMESPACE``, the only other two-key namespace.
REFERENCE_FENCE_LOCK_NAMESPACE: Final = 0x52464E43

#: The one key every writer and every batch contend on within the namespace.
REFERENCE_FENCE_KEY: Final = advisory_lock_key("inventory-reference-holders")

#: The longest a writer or a batch waits for the other side of the fence.
REFERENCE_FENCE_TIMEOUT: Final = timedelta(seconds=30)

#: How long a batch keeps the fence after its delete fails, so a COMMIT the
#: server received before the failure can land before any writer is let in.
REFERENCE_FENCE_COMMIT_GRACE: Final = timedelta(seconds=5)

#: The most writers one process lets hold the fence at once. Each opens a lock
#: connection outside the pool before its write checks one out of it, so the cap
#: matches the tasks engine's POOL_SIZE + MAX_OVERFLOW ceiling in the side-car: a
#: writer past it would be waiting for a pooled connection anyway.
REFERENCE_FENCE_MAX_WRITERS: Final = 5

#: Lock-wait floor for a writer whose slot wait used up the whole bound, so an
#: uncontended lock is still granted rather than the write refused after all.
_LEAST_LOCK_WAIT: Final = timedelta(milliseconds=1)

_writer_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _writer_slots_of_running_loop() -> asyncio.Semaphore:
    """Return the running event loop's semaphore of write-fence slots.

    One per loop rather than one per module, because a semaphore binds to the
    loop it first waits on and refuses every other: a process serves on one loop,
    but a test run starts a fresh one per test.

    :return: The loop's semaphore, sized by :data:`REFERENCE_FENCE_MAX_WRITERS`
        when the loop first asks for it.
    """
    loop = asyncio.get_running_loop()
    slots = _writer_slots.get(loop)
    if slots is None:
        slots = _writer_slots[loop] = asyncio.Semaphore(REFERENCE_FENCE_MAX_WRITERS)
    return slots


def _fence_busy() -> HTTPServiceUnavailableException:
    """Build the retryable refusal of a write that could not take the fence.

    :return: The 503 to raise.
    """
    return HTTPServiceUnavailableException(
        detail="Inventory collection is running; retry the request shortly."
    )


@asynccontextmanager
async def hold_reference_write_fence(
    session: AsyncSession, *, max_wait: timedelta | None = None
) -> AsyncIterator[None]:
    """Hold the fence shared across a write that can name an inventory id.

    The write first takes one of the process's
    :data:`REFERENCE_FENCE_MAX_WRITERS` slots, and keeps it until the block
    exits, so writers queued behind a batch cannot open a lock connection each
    without limit. The slot wait and the lock wait share one bound, which
    defaults to :data:`REFERENCE_FENCE_TIMEOUT`, read when the fence is taken
    rather than when this function was defined.

    :param session: A tasks-database session; the lock is taken on a connection
        of its own, so the write's commits do not release it.
    :param max_wait: The longest to wait for a slot and for a running batch to
        finish, together.
    :return: Nothing; the write runs inside the block.
    :raises HTTPServiceUnavailableException: If no slot frees up, or a
        collection batch holds the fence, beyond the bound. Raised before the
        block is entered, so nothing is written.
    :raises SQLAlchemyError: If the fence's connection cannot be opened or a
        lock statement fails. Raised before the block is entered.
    """
    bound = REFERENCE_FENCE_TIMEOUT if max_wait is None else max_wait
    slots = _writer_slots_of_running_loop()
    started = time.monotonic()
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(bound.total_seconds()):
                await slots.acquire()
        except TimeoutError as exc:
            raise _fence_busy() from exc
        stack.callback(slots.release)
        remaining = bound - timedelta(seconds=time.monotonic() - started)
        try:
            await stack.enter_async_context(
                pg_advisory_xact_lock(
                    session,
                    REFERENCE_FENCE_LOCK_NAMESPACE,
                    REFERENCE_FENCE_KEY,
                    shared=True,
                    max_wait=max(remaining, _LEAST_LOCK_WAIT),
                )
            )
        except AdvisoryLockTimeoutError as exc:
            raise _fence_busy() from exc
        yield


@asynccontextmanager
async def hold_reference_collection_fence(
    session: AsyncSession, *, max_wait: timedelta | None = None
) -> AsyncIterator[AdvisoryLockHandle]:
    """Hold the fence exclusive across one collection batch.

    The bound defaults to :data:`REFERENCE_FENCE_TIMEOUT`, read when the fence
    is taken rather than when this function was defined.

    :param session: A tasks-database session; the lock is taken on a connection
        of its own.
    :param max_wait: The longest to wait for in-flight writes to finish.
    :return: The handle to verify the fence is still held before deleting.
    :raises AdvisoryLockTimeoutError: If writes hold the fence beyond the bound.
        Raised before the block is entered, so the batch deletes nothing.
    :raises SQLAlchemyError: If the fence's connection cannot be opened or a
        lock statement fails. Raised before the block is entered.
    """
    async with pg_advisory_xact_lock(
        session,
        REFERENCE_FENCE_LOCK_NAMESPACE,
        REFERENCE_FENCE_KEY,
        max_wait=REFERENCE_FENCE_TIMEOUT if max_wait is None else max_wait,
    ) as handle:
        yield handle
