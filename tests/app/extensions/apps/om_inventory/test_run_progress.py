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

``RunProgress`` writes the hosts in scope onto the run row as soon as they are known,
and counts each host as its executor's scan comes back, so the row says more than
``running`` while a sweep is in flight. ``finalise`` settles the same count, and both
count a host, not an executor, so the finished count reaches ``hosts_probeable``; the
answered count is in hosts as well.
"""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.dispatch import HostProbeResult
from app.extensions.apps.om_inventory.enumeration import InventoryHost
from app.extensions.apps.om_inventory.models import ProbeRun
from app.extensions.apps.om_inventory.service import (
    finalise,
    RunProgress,
    SweepOutcome,
    SweepProgress,
)
from tests.app.extensions.apps.om_inventory.conftest import host, RunSweep, SERVICE

#: Hosts in scope, the executors whose scans came back, and the
#: ``(hosts_total, hosts_probeable, hosts_finished)`` that should be stored for them.
COUNTS = [
    pytest.param(
        [host("node00"), host("node01"), host("node02", orphaned=True)],
        ["node00", "node01"],
        (3, 2, 2),
        id="one-host-per-executor",
    ),
    pytest.param(
        [host("node00", executor="db00"), host("node01", executor="db00")],
        ["db00"],
        (2, 2, 2),
        id="two-hosts-on-one-executor",
    ),
    pytest.param(
        [host("node00")],
        ["serves-no-host-in-scope"],
        (1, 1, 0),
        id="an-executor-serving-no-host",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("hosts", "came_back", "counts"), COUNTS)
async def test_writes_the_hosts_in_scope_and_counts_each_one_back(
    session: AsyncSession,
    hosts: list[InventoryHost],
    came_back: list[str],
    counts: tuple[int, int, int],
) -> None:
    """Store the totals at once, and count every host an executor serves as it lands."""
    run = await ProbeRunManager.save(session, ProbeRun())
    progress = RunProgress(run.id)

    with patch(
        f"{SERVICE}.get_async_session_maker", return_value=lambda: nullcontext(session)
    ):
        await progress.started(hosts)
        for executor in came_back:
            await progress.host_done(HostProbeResult(executor_host=executor))

    stored = await ProbeRunManager.get(session, id=run.id)
    assert (stored.hosts_total, stored.hosts_probeable, stored.hosts_finished) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize(("hosts", "came_back", "counts"), COUNTS)
async def test_a_finished_sweep_settles_the_same_counts(
    session: AsyncSession,
    hosts: list[InventoryHost],
    came_back: list[str],
    counts: tuple[int, int, int],
) -> None:
    """Settle the counts on the hosts whose executor was dispatched to."""
    run = await ProbeRunManager.save(session, ProbeRun())

    stored = await finalise(
        session, run.id, SweepOutcome(hosts=hosts, dispatched=set(came_back))
    )

    assert (stored.hosts_total, stored.hosts_probeable, stored.hosts_finished) == counts


@pytest.mark.asyncio
async def test_answered_hosts_are_counted_as_hosts_too(session: AsyncSession) -> None:
    """Count every host an answering executor serves, and none of a silent one's."""
    run = await ProbeRunManager.save(session, ProbeRun())
    hosts = [
        host("node00", executor="db00"),
        host("node01", executor="db00"),
        host("node02"),
    ]

    stored = await finalise(
        session,
        run.id,
        SweepOutcome(
            hosts=hosts,
            dispatched={"db00", "node02"},
            host_documents={"db00": {"collected_at": "2026-10-09T12:00:00+00:00"}},
        ),
    )

    assert (stored.hosts_probeable, stored.hosts_answered) == (3, 2)


@pytest.mark.asyncio
async def test_a_failed_write_does_not_fail_the_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log and carry on: progress is for a reader, the outcome is the record."""
    broken = MagicMock(side_effect=RuntimeError("database gone"))

    with patch(f"{SERVICE}.get_async_session_maker", return_value=broken):
        await RunProgress(ProbeRun().id).started([host("node00")])

    assert "could not record progress" in caplog.text


@pytest.mark.asyncio
async def test_the_sweep_reports_its_hosts_and_each_one_back(
    run_sweep: RunSweep,
) -> None:
    """Report the hosts once enumerated, and each executor's result as it lands."""
    progress = MagicMock(spec=SweepProgress)
    hosts = [host("node00"), host("node01"), host("node02", orphaned=True)]
    results = {
        name: HostProbeResult(executor_host=name) for name in ("node00", "node01")
    }

    await run_sweep([], results, hosts=hosts, progress=progress)

    progress.started.assert_awaited_once_with(hosts)
    landed = [call.args[0] for call in progress.host_done.await_args_list]
    assert landed == list(results.values())
