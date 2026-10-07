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

"""Test that a running sweep says how far it has got.

The run row used to hold nothing but ``running`` until the sweep ended, so the page
could show only a spinner for tens of seconds. ``RunProgress`` writes the hosts in
scope as soon as they are known and counts each host's scan as it comes back.
"""

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun
from app.extensions.apps.om_inventory.service import (
    finalise,
    RunProgress,
    sweep,
    SweepOutcome,
    SweepProgress,
)
from tests.app.extensions.apps.om_inventory.test_sweep_nodes import host

BASE = "app.extensions.apps.om_inventory.service"


@pytest.mark.asyncio
async def test_writes_the_hosts_in_scope_and_counts_each_one_back(
    session: AsyncSession,
) -> None:
    """Store the totals at once, and the finished count as hosts come back."""
    run = await ProbeRunManager.save(session, ProbeRun())
    progress = RunProgress(run.id)

    with patch(
        f"{BASE}.get_async_session_maker", return_value=lambda: nullcontext(session)
    ):
        await progress.started(hosts_total=5, hosts_probeable=4)
        await progress.host_done(MagicMock())
        await progress.host_done(MagicMock())

    stored = await ProbeRunManager.get(session, id=run.id)
    assert (stored.hosts_total, stored.hosts_probeable, stored.hosts_finished) == (
        5,
        4,
        2,
    )


@pytest.mark.asyncio
async def test_a_failed_write_does_not_fail_the_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log and carry on: progress is for a reader, the outcome is the record."""
    broken = MagicMock(side_effect=RuntimeError("database gone"))

    with patch(f"{BASE}.get_async_session_maker", return_value=broken):
        await RunProgress(ProbeRun().id).host_done(MagicMock())

    assert "could not record progress" in caplog.text


@pytest.mark.asyncio
async def test_the_sweep_reports_its_hosts_and_each_one_back() -> None:
    """Report the totals once enumerated, and hand probe_all the per-host callback."""
    progress = MagicMock(spec=SweepProgress)
    progress.started = AsyncMock()
    hosts = [host("node00"), host("node01"), host("node02", orphaned=True)]
    clients = (MagicMock(), MagicMock())
    for client in clients:
        client.auth.return_value = nullcontext()
    probe_all = AsyncMock(return_value={})

    with (
        patch(f"{BASE}._build_clients", AsyncMock(return_value=clients)),
        patch(f"{BASE}.get_internal_token", return_value="token"),
        patch(f"{BASE}.list_mongodb_services", AsyncMock(return_value=[])),
        patch(f"{BASE}.list_inventory_nodes", AsyncMock(return_value=[])),
        patch(f"{BASE}.build_hosts", return_value=hosts),
        patch(f"{BASE}.get_executor_states", AsyncMock(return_value={})),
        patch(f"{BASE}.map_services", return_value=[]),
        patch(f"{BASE}.probe_all", probe_all),
    ):
        await sweep("2026-10-07T12:00:00+00:00", progress=progress)

    progress.started.assert_awaited_once_with(hosts_total=3, hosts_probeable=2)
    assert probe_all.await_args is not None
    assert probe_all.await_args.kwargs["on_host_done"] is progress.host_done


@pytest.mark.asyncio
async def test_a_finished_sweep_counts_every_host_it_dispatched_to(
    session: AsyncSession,
) -> None:
    """Settle the count on the hosts dispatched to, whatever progress recorded."""
    run = await ProbeRunManager.save(session, ProbeRun())
    dispatched = {"node00", "node01"}

    stored = await finalise(session, run.id, SweepOutcome(dispatched=dispatched))

    assert stored.hosts_finished == len(dispatched)
