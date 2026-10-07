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

"""Test that an abandoned probe dispatch is not left in the queue.

The tasks API refuses a queue item identical to one already in flight, and every
sweep dispatches the same ``run-python`` to the same host with the same config. So a
run this app gives up waiting for does not merely cost one probe: it makes that host
answer ``409`` on every later sweep until someone clears the row by hand. These tests
pin the release, and pin the two ways it must not misfire — releasing a run that
already finished, and failing a sweep because its own cleanup failed.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.extensions.apps.om_inventory.dispatch import probe_host
from app.extensions.apps.om_inventory.inventory import InventoryService
from app.extensions.apps.om_inventory.mapping import MappedService
from app.extensions.apps.om_inventory.models import NodeResolution, ScanFailure
from tests.app.extensions.apps.om_inventory.conftest import HOST

HISTORY_ID = 779


def entries() -> list[MappedService]:
    """Build the one resolved service a host serves.

    :return: The mapping the dispatch is built from.
    """
    return [
        MappedService(
            service=InventoryService(
                service_id=1,
                external_id="ff0275b6-3633-474a-8068-3c39d3c7a4da",
                name="svc",
                port=27017,
                node_name=HOST,
                node_address="10.0.0.1",
            ),
            executor_host=HOST,
            resolution=NodeResolution.NAME,
        )
    ]


def make_api(
    statuses: list[str],
    *,
    stop_raises: Exception | None = None,
    get_raises: Exception | None = None,
) -> MagicMock:
    """Build a tasks API stub that answers a dispatch and then a poll sequence.

    :param statuses: The statuses ``GET /history/{id}`` returns, in order. The last is
        repeated once exhausted, which is what makes a never-terminal run expressible.
    :param stop_raises: Raised by ``POST /history/{id}/stop/`` when given.
    :param get_raises: Raised by every ``GET`` when given.
    :return: The stub.
    """
    api = MagicMock()
    remaining = list(statuses)

    async def get(path: str, **_: Any) -> dict[str, Any]:
        if get_raises is not None:
            raise get_raises
        status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return {"id": HISTORY_ID, "status": status}

    async def post(path: str, **_: Any) -> dict[str, Any]:
        if path.endswith("/stop/"):
            if stop_raises is not None:
                raise stop_raises
            return {"id": HISTORY_ID, "status": "stopped"}
        return {"id": HISTORY_ID}

    api.get = AsyncMock(side_effect=get)
    api.post = AsyncMock(side_effect=post)
    return api


def stop_calls(api: MagicMock) -> list[str]:
    """Return the stop paths the dispatch posted to.

    :param api: The tasks API stub.
    :return: The paths.
    """
    return [
        call.args[0]
        for call in api.post.call_args_list
        if call.args and str(call.args[0]).endswith("/stop/")
    ]


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the wait so a timeout case takes a test's worth of time, not minutes."""
    from app.extensions.apps.om_inventory.config import om_inventory_settings

    monkeypatch.setattr(om_inventory_settings, "TASK_TIMEOUT", 1)
    monkeypatch.setattr(om_inventory_settings, "POLL_INTERVAL", 1)


class TestReleaseOnAbandonedDispatch:
    """Pin the release, and the two ways it must not misfire."""

    @pytest.mark.asyncio
    async def test_a_run_we_stop_waiting_for_is_released(self) -> None:
        """Stop a dispatch that never reached a terminal status."""
        api = make_api(["running"])

        result = await probe_host(api, HOST, entries())

        assert stop_calls(api) == [f"/history/{HISTORY_ID}/stop/"]
        assert result.error == (
            f"the scan did not finish within 1s and was cancelled (task history "
            f"{HISTORY_ID})"
        )
        assert result.error_code == ScanFailure.TIMED_OUT
        # The sweep still reports the probe as failed — releasing the queue item is
        # cleanup, not a rescue of the data this host owed.
        assert result.records == {}

    @pytest.mark.asyncio
    async def test_a_release_failure_is_reported_and_not_raised(self) -> None:
        """Say the queue item was left behind, rather than failing the sweep over it."""
        api = make_api(
            ["running"], stop_raises=RuntimeError("500: KeyError TaskStates")
        )

        result = await probe_host(api, HOST, entries())

        assert stop_calls(api) == [f"/history/{HISTORY_ID}/stop/"]
        assert "the scan did not finish" in (result.error or "")
        # The operator needs to know which id is now blocking this host: the stop
        # route raises exactly where the allocation is gone, which is the case most
        # likely to have caused the abandonment.
        assert f"task history {HISTORY_ID} could not be released" in (
            result.error or ""
        )
        assert "block this node's next scan" in (result.error or "")
        assert "cancelled" not in (result.error or "")
        # The stuck run outranks the timeout: it is the part that will not clear.
        assert result.error_code == ScanFailure.BLOCKED

    @pytest.mark.asyncio
    async def test_a_finished_run_is_not_stopped(self) -> None:
        """Leave a terminal queue item alone when collection failed after it finished."""
        api = make_api(["success"])
        # The run finished; reading its logs is what failed.
        api.stream = MagicMock(side_effect=RuntimeError("log stream closed"))

        result = await probe_host(api, HOST, entries())

        assert stop_calls(api) == []
        assert "log stream closed" in (result.error or "")
        assert "could not be released" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_a_dispatch_that_never_queued_releases_nothing(self) -> None:
        """Skip the release when no queue item was ever created."""
        api = MagicMock()
        api.post = AsyncMock(side_effect=RuntimeError("connection refused"))

        result = await probe_host(api, HOST, entries())

        assert stop_calls(api) == []
        assert "connection refused" in (result.error or "")

    @pytest.mark.asyncio
    async def test_an_unreadable_status_still_releases(self) -> None:
        """Stop the run anyway when its status cannot be read.

        Not knowing whether an item is in flight is not a reason to leave it there;
        an unnecessary stop costs one refused request, a missed one costs the host.
        """
        api = make_api(["running"], get_raises=RuntimeError("gateway timeout"))

        await probe_host(api, HOST, entries())

        assert stop_calls(api) == [f"/history/{HISTORY_ID}/stop/"]
