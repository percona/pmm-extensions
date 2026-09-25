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

"""Test that a transient Tasks API refusal does not fail a host's probe outright.

A sweep dispatches up to ``MAX_CONCURRENT_PROBES`` hosts at once, each making its
own requests to the Tasks API — exactly the burst that can transiently exhaust
that process's own database connection pool (sized at 5 connections, surfaced as
a 503) or race two byte-identical dispatches against the same
dispatch-lock row (surfaced as a 409 — the lock's whole lifetime is one dispatch
call, per ``_dispatch_queue_item``'s own ``finally``, not this probe's).
``with_capacity_retry`` absorbs either queueing accident with a bounded retry
instead of letting it count as a dispatch or collection failure, and stays
bounded both in attempt count and in total added wait so a refusal that does not
clear still gives up and lets the ordinary failure path record it.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import HTTPConflictException, HTTPServiceUnavailableException
from app.extensions.apps.om_inventory.dispatch import (
    _adopted_queue_item_id,
    CAPACITY_RETRY_ATTEMPTS,
    probe_host,
    with_capacity_retry,
)
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import MappedService
from app.extensions.apps.om_inventory.models import NodeResolution
from tests.app.extensions.apps.om_inventory.conftest import HOST

HISTORY_ID = 901
#: The id the guard names in its 409, adopted rather than dispatched again.
ADOPTED_ID = 130


def entries() -> list[MappedService]:
    """Build the one resolved service a host serves.

    :return: The mapping the dispatch is built from.
    """
    return [
        MappedService(
            service=InventoryService(
                service_id=1,
                external_id="a35f6b6e-9b34-4e6a-8f0e-6a6d0f2b6c1a",
                name="svc",
                port=27017,
                node_name=HOST,
                node_address="10.0.0.1",
            ),
            executor_host=HOST,
            resolution=NodeResolution.NAME,
        )
    ]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real backoff wait, and the poll loop's own wait between them."""
    from app.extensions.apps.om_inventory import dispatch
    from app.extensions.apps.om_inventory.config import om_inventory_settings

    monkeypatch.setattr(dispatch, "_CAPACITY_RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(om_inventory_settings, "POLL_INTERVAL", 0)


class TestWithCapacityRetry:
    """Pin the retry helper directly, apart from any dispatch machinery."""

    @pytest.mark.asyncio
    async def test_succeeds_once_the_pool_frees_up(self) -> None:
        """Hide a 503 that clears within the attempt budget from the caller."""
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < CAPACITY_RETRY_ATTEMPTS:
                raise HTTPServiceUnavailableException
            return "ok"

        assert await with_capacity_retry(flaky) == "ok"
        assert calls == CAPACITY_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_gives_up_after_the_bounded_number_of_attempts(self) -> None:
        """Give up on a pool that never frees up, rather than retrying forever."""
        calls = 0

        async def always_full() -> str:
            nonlocal calls
            calls += 1
            raise HTTPServiceUnavailableException

        with pytest.raises(HTTPServiceUnavailableException):
            await with_capacity_retry(always_full)

        # Bounded in count: exactly the configured number of tries, not one more.
        assert calls == CAPACITY_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_different_error_is_not_retried(self) -> None:
        """Absorb only a recognised transient signal, and fail everything else fast."""
        calls = 0

        async def broken() -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("not a capacity problem")

        with pytest.raises(RuntimeError):
            await with_capacity_retry(broken)

        assert calls == 1

    @pytest.mark.asyncio
    async def test_a_dispatch_lock_race_clears_on_retry(self) -> None:
        """Retry the same-content dispatch-lock collision, not just the 503."""
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < CAPACITY_RETRY_ATTEMPTS:
                raise HTTPConflictException(
                    "409: DispatchLock with the same name already exists."
                )
            return "ok"

        assert await with_capacity_retry(flaky) == "ok"
        assert calls == CAPACITY_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_an_unrelated_409_is_not_retried(self) -> None:
        """Fail on the first try for a conflict that is not the dispatch lock.

        ``_dispatch_queue_item`` also raises 409 for "Queue item is not in a
        pending state" — a real conflict, not a queueing accident, and nothing
        a retry would resolve.
        """
        calls = 0

        async def rejected() -> str:
            nonlocal calls
            calls += 1
            raise HTTPConflictException("Queue item is not in a pending state.")

        with pytest.raises(HTTPConflictException):
            await with_capacity_retry(rejected)

        assert calls == 1


class TestProbeHostAbsorbsATransientRefusal:
    """Cover the end-to-end shape: a host's probe survives a queueing accident."""

    @pytest.mark.asyncio
    async def test_a_dispatch_lock_race_still_succeeds(self) -> None:
        """Treat a same-content lock collision as not this host's failure."""
        post_calls = 0

        async def post(_path: str, **_: Any) -> dict[str, Any]:
            nonlocal post_calls
            post_calls += 1
            if post_calls == 1:
                raise HTTPConflictException(
                    "409: DispatchLock with the same name already exists."
                )
            return {"id": HISTORY_ID}

        async def get(_path: str, **_: Any) -> dict[str, Any]:
            return {"id": HISTORY_ID, "status": "success"}

        api = MagicMock()
        api.post = AsyncMock(side_effect=post)
        api.get = AsyncMock(side_effect=get)

        async def stream(*_a: Any, **_kw: Any) -> Any:
            return
            yield  # pragma: no cover - makes this an async generator

        api.stream = stream

        result = await probe_host(api, HOST, entries())

        one_retry = 2
        assert post_calls == one_retry
        assert result.error is None
        assert result.task_history_id == HISTORY_ID

    @pytest.mark.asyncio
    async def test_a_dispatch_that_clears_on_retry_still_succeeds(self) -> None:
        """Treat one 503 on the initial dispatch as not this host's failure."""
        post_calls = 0

        async def post(_path: str, **_: Any) -> dict[str, Any]:
            nonlocal post_calls
            post_calls += 1
            if post_calls == 1:
                raise HTTPServiceUnavailableException
            return {"id": HISTORY_ID}

        async def get(_path: str, **_: Any) -> dict[str, Any]:
            return {"id": HISTORY_ID, "status": "success"}

        api = MagicMock()
        api.post = AsyncMock(side_effect=post)
        api.get = AsyncMock(side_effect=get)

        async def stream(*_a: Any, **_kw: Any) -> Any:
            return
            yield  # pragma: no cover - makes this an async generator

        api.stream = stream

        result = await probe_host(api, HOST, entries())

        one_retry = 2
        assert post_calls == one_retry
        assert result.error is None
        assert result.task_history_id == HISTORY_ID

    @pytest.mark.asyncio
    async def test_a_pool_that_stays_saturated_is_recorded_as_a_failure(self) -> None:
        """Record a host that exhausts the retry budget as failed, not hanging."""
        post_calls = 0

        async def always_full(_path: str, **_: Any) -> dict[str, Any]:
            nonlocal post_calls
            post_calls += 1
            raise HTTPServiceUnavailableException

        api = MagicMock()
        api.post = AsyncMock(side_effect=always_full)

        result = await probe_host(api, HOST, entries())

        assert post_calls == CAPACITY_RETRY_ATTEMPTS
        assert "HTTPServiceUnavailableException" in (result.error or "")
        # Nothing reached the queue, so there is nothing for probe_host to release.
        assert result.task_history_id is None


class TestAdoptedQueueItemId:
    """Pin which 409 names an item to adopt, apart from any dispatch machinery."""

    def test_recognises_the_identical_item_conflict(self) -> None:
        """Read the id out of the message the guard raises."""
        err = HTTPConflictException(
            f"409: Identical queue item already running ({ADOPTED_ID})."
        )

        assert _adopted_queue_item_id(err) == ADOPTED_ID

    def test_ignores_a_conflict_that_names_no_item(self) -> None:
        """Leave every other 409 to the ordinary failure path.

        Adopting is only sound for the guard that compares task, target, meta
        and payload before raising; nothing else promises an equivalent result.
        """
        assert _adopted_queue_item_id(HTTPConflictException("DispatchLock")) is None
        assert (
            _adopted_queue_item_id(
                HTTPConflictException("Queue item is not in a pending state.")
            )
            is None
        )
        assert _adopted_queue_item_id(HTTPServiceUnavailableException()) is None


class TestProbeHostAdoptsAnInFlightItem:
    """Cover the case that made a successful probe report a failed host."""

    @pytest.mark.asyncio
    async def test_an_identical_in_flight_probe_is_adopted(self) -> None:
        """Wait on the item the guard named instead of failing the host.

        The conflict says this exact probe is already running, so its result is
        this dispatch's result. Retrying cannot help -- the item stays active for
        as long as the probe takes -- and failing the host loses an answer that
        was on its way. Observed 2026-09-24: item 130 probed the host
        successfully in 5s while the run recorded it unanswered and came back
        PARTIAL.
        """
        post_calls = 0

        async def post(_path: str, **_: Any) -> dict[str, Any]:
            nonlocal post_calls
            post_calls += 1
            raise HTTPConflictException(
                f"409: Identical queue item already running ({ADOPTED_ID})."
            )

        async def get(_path: str, **_: Any) -> dict[str, Any]:
            return {"id": ADOPTED_ID, "status": "success"}

        api = MagicMock()
        api.post = AsyncMock(side_effect=post)
        api.get = AsyncMock(side_effect=get)

        async def stream(*_a: Any, **_kw: Any) -> Any:
            return
            yield  # pragma: no cover - makes this an async generator

        api.stream = stream

        result = await probe_host(api, HOST, entries())

        # Adopted, not retried: asking again cannot clear a conflict that means
        # the answer is already being computed.
        assert post_calls == 1
        assert result.error is None
        assert result.task_history_id == ADOPTED_ID
