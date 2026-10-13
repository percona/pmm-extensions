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

"""Test the reference fence between Tasks API writers and inventory collection."""

import pytest
from fastapi import status
from pytest_mock import MockerFixture

from app.core.db.utils import AdvisoryLockTimeoutError
from app.core.exceptions import HTTPServiceUnavailableException
from app.tasks import reference_fence
from app.tasks.reference_fence import (
    hold_reference_collection_fence,
    hold_reference_write_fence,
)
from tests.app.advisory_locks import isolate_reference_fence, SHORT_LOCK_WAIT


@pytest.fixture(autouse=True)
def _isolated_fence(mocker: MockerFixture) -> None:
    """Keep these tests off the fence key other workers may be holding."""
    isolate_reference_fence(mocker)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_write_fence_answers_503_while_a_collection_batch_holds_it(
    contending_sessions, mocker: MockerFixture
):
    """Turn a writer's expired wait into a retryable 503.

    The bound is patched on the module rather than passed, because that is how
    the routes reach it: a bound captured when the function was defined would
    ignore the patch and wait the full production timeout.
    """
    collector, writer = contending_sessions
    mocker.patch.object(reference_fence, "REFERENCE_FENCE_TIMEOUT", SHORT_LOCK_WAIT)

    async with hold_reference_collection_fence(collector):
        with pytest.raises(HTTPServiceUnavailableException) as raised:
            async with hold_reference_write_fence(writer):
                pytest.fail("a writer entered while a batch held the fence")

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "collection" in raised.value.detail


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_collection_fence_propagates_the_timeout_while_a_writer_holds_it(
    contending_sessions,
):
    """Leave a collector's expired wait as the lock error, for the run to fail on."""
    collector, writer = contending_sessions

    async with hold_reference_write_fence(writer):
        with pytest.raises(AdvisoryLockTimeoutError):
            async with hold_reference_collection_fence(
                collector, max_wait=SHORT_LOCK_WAIT
            ):
                pytest.fail("a batch entered while a writer held the fence")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_writers_hold_the_fence_together(contending_sessions):
    """Let concurrent writers proceed without waiting for each other."""
    first, second = contending_sessions

    async with (
        hold_reference_write_fence(first, max_wait=SHORT_LOCK_WAIT),
        hold_reference_write_fence(second, max_wait=SHORT_LOCK_WAIT),
    ):
        pass
