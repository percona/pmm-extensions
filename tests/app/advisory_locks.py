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

"""Observe PostgreSQL advisory-lock waiters from a test.

An ordering test has to know that a contender is *blocked on the lock*, not
merely that it has not finished yet. A sleep cannot tell the two apart: a
contender that never reached the lock looks exactly like one queued behind it.
``pg_locks`` can, because a queued request is listed there as not granted.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from pytest_mock import MockerFixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db.utils import advisory_lock_key
from app.tasks import reference_fence

__all__ = ["SHORT_LOCK_WAIT", "isolate_reference_fence", "wait_for_advisory_waiters"]

#: Short enough to keep a lock-timeout test fast, long enough that a slow CI
#: connect cannot be mistaken for the lock wait.
SHORT_LOCK_WAIT = timedelta(milliseconds=200)

_WAITERS = text(
    "SELECT count(*) FROM pg_locks "
    "WHERE locktype = 'advisory' AND NOT granted AND objsubid = 2 "
    "AND classid::bigint = :classid AND objid::bigint = :objid"
)

_OID_MASK = 0xFFFFFFFF


def isolate_reference_fence(mocker: MockerFixture) -> int:
    """Move the reference fence onto a key no other test can be holding.

    Advisory locks are database-wide while the test fixtures isolate parallel
    workers by schema only, so two workers on the production key would queue
    behind each other's batches and read the other's hold as their own.

    :param mocker: The pytest-mock fixture.
    :return: The key the fence now uses.
    """
    key = advisory_lock_key(uuid4().hex)
    mocker.patch.object(reference_fence, "REFERENCE_FENCE_KEY", key)
    return key


async def wait_for_advisory_waiters(
    engine: AsyncEngine,
    namespace: int,
    key: int,
    *,
    count: int = 1,
    within: float = 5.0,
) -> None:
    """Wait until ``count`` requests are queued on a two-key advisory lock.

    ``pg_locks`` lists the two keys as unsigned ``oid`` columns, so a negative
    key is compared through its 32-bit two's-complement image.

    :param engine: An engine on the database the lock lives in.
    :param namespace: The lock's first key.
    :param key: The lock's second key.
    :param count: How many queued requests to wait for.
    :param within: The seconds to wait before failing the test.
    :raises AssertionError: If fewer than ``count`` requests are queued in time.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    params = {"classid": namespace & _OID_MASK, "objid": key & _OID_MASK}
    async with engine.connect() as connection:
        while True:
            waiting = await connection.scalar(_WAITERS, params)
            if waiting >= count:
                return
            if loop.time() >= deadline:
                raise AssertionError(
                    f"{waiting} of {count} advisory-lock waiters queued on "
                    f"{namespace}/{key} after {within}s"
                )
            await asyncio.sleep(0.02)
