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

"""Test ``run_probe``'s own ``ENABLED`` check, and the one way past it.

``trigger_probe`` refuses a manual trigger while ``ENABLED`` is off, but beat calls
the task directly, and the worker reads ``ENABLED`` from a snapshot that can lag the
API process that accepted a trigger. In both cases the sweep must be recorded as
refused. A run left ``RUNNING`` would hold every host until ``STALE_RUN_AFTER``.

That lag cuts the other way for the sweep PMM fires the moment it turns the switch
on, so the endpoint hands its own fresher read down as ``enabled_confirmed`` and the
worker honours it rather than refusing a sweep the switch allows.
"""

from contextlib import nullcontext
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory import (
    celery as celery_module,
)
from app.extensions.apps.om_inventory import (
    service as service_module,
)
from app.extensions.apps.om_inventory.celery import run_om_probe
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from app.extensions.apps.om_inventory.service import run_probe, SWITCHED_OFF_DETAIL
from tests.app.extensions.apps.om_inventory.conftest import CLEAN_OUTCOME


@pytest.fixture
def _switched_off(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """Turn ``ENABLED`` off and point ``run_probe`` at the test session.

    :param mocker: Patches the session maker and the Nomad-bound sweep.
    :param monkeypatch: Restores the real ``ENABLED`` after the test.
    :param session: The session every ``run_probe`` block should reuse.
    """
    monkeypatch.setattr(om_inventory_settings, "ENABLED", False)
    mocker.patch.object(
        service_module,
        "get_async_session_maker",
        return_value=lambda: nullcontext(session),
    )
    mocker.patch.object(service_module, "sweep", AsyncMock(return_value=CLEAN_OUTCOME))


@pytest.mark.usefixtures("_switched_off")
class TestRunProbeWhileSwitchedOff:
    """Record a refused sweep, rather than running it or leaving it in flight."""

    @pytest.mark.asyncio
    async def test_a_triggered_run_is_closed_as_skipped(
        self, session: AsyncSession
    ) -> None:
        """Close the trigger's row as ``SKIPPED``, naming the switch.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun(scope=None))

        returned_id = await run_probe(execution_id=run.id, node_ids=None)

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.finished_at is not None
        assert stored.error == SWITCHED_OFF_DETAIL

    @pytest.mark.asyncio
    async def test_a_scheduled_run_is_recorded_as_skipped(
        self, session: AsyncSession
    ) -> None:
        """Record a beat-driven sweep as ``SKIPPED`` instead of a silent gap.

        :param session: The database session.
        """
        returned_id = await run_probe(execution_id=None, node_ids=None)

        stored = await ProbeRunManager.get(session, id=returned_id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.error == SWITCHED_OFF_DETAIL

    @pytest.mark.asyncio
    async def test_a_trigger_that_already_read_the_switch_is_run(
        self, session: AsyncSession
    ) -> None:
        """Run the sweep the endpoint confirmed, despite the stale snapshot.

        This is the enable-then-trigger case: ``ENABLED`` here stands for the
        worker's snapshot, which still says off, while the endpoint read the
        applied override and said on.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun(scope=None))

        returned_id = await run_probe(
            execution_id=run.id, node_ids=None, enabled_confirmed=True
        )

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status is not ProbeRunStatus.SKIPPED
        assert stored.error != SWITCHED_OFF_DETAIL

    @pytest.mark.asyncio
    async def test_a_scheduled_run_cannot_claim_the_switch_was_read(
        self, session: AsyncSession
    ) -> None:
        """Keep beat on the snapshot: it has no fresher read to offer.

        Beat calls the task with no arguments, so the default is what decides
        whether the periodic sweep can outlive the switch being turned off.

        :param session: The database session.
        """
        returned_id = await run_probe()

        stored = await ProbeRunManager.get(session, id=returned_id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.error == SWITCHED_OFF_DETAIL


class TestRunOmProbeCarriesTheFlag:
    """Keep the Celery wrapper wired to the sweep it stands in front of.

    Nothing else exercises this hop. The endpoint names the keyword and the gate
    reads it, so a wrapper that dropped it on the floor would put every triggered
    sweep back on the worker's snapshot with every test still green.
    """

    def test_a_confirmed_trigger_reaches_the_sweep(self, mocker: MockerFixture) -> None:
        """Forward the endpoint's read, and the ids it came with.

        :param mocker: Patches the sweep and the loop the task drives it on.
        """
        run_id = uuid4()
        probe = mocker.patch.object(celery_module, "run_probe")
        mocker.patch.object(
            celery_module.celery.loop, "run_until_complete", return_value=run_id
        )

        returned = run_om_probe(str(run_id), ["node-1"], enabled_confirmed=True)

        assert returned == str(run_id)
        probe.assert_called_once_with(run_id, ["node-1"], enabled_confirmed=True)

    def test_a_scheduled_call_confirms_nothing(self, mocker: MockerFixture) -> None:
        """Leave beat's zero-argument call reading the switch itself.

        :param mocker: Patches the sweep and the loop the task drives it on.
        """
        probe = mocker.patch.object(celery_module, "run_probe")
        mocker.patch.object(
            celery_module.celery.loop, "run_until_complete", return_value=uuid4()
        )

        run_om_probe()

        probe.assert_called_once_with(None, None, enabled_confirmed=False)
