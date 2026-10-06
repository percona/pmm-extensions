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

"""Test ``run_probe``'s own ``ENABLED`` check against the override as stored.

``trigger_probe`` refuses a manual trigger while ``ENABLED`` is off, but beat calls
the task directly, so the gate has to live here too. A sweep it refuses must be
recorded ``SKIPPED``; a run left ``RUNNING`` would hold every host until
``STALE_RUN_AFTER``.

A worker's own snapshot is not the thing to read. It advances only at task
boundaries and at most once per ``SETTINGS_OVERRIDE.REFRESH_INTERVAL``, so it can
disagree with the stored override in **either** direction, and each direction is a
bug: a sweep triggered in the same breath as the PATCH that turned OM on gets
refused on a stale no, and one enqueued shortly before the switch went off runs on a
stale yes. ``run_probe`` republishes the snapshot from the row and reads that, so the
tests below pin the stored value against a deliberately stale proxy.
"""

from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token, SettingOverride
from app.extensions.apps.om_inventory import (
    service as service_module,
)
from app.extensions.apps.om_inventory.config import (
    om_inventory_settings,
    OmInventorySettings,
)
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from app.extensions.apps.om_inventory.service import run_probe, SWITCHED_OFF_DETAIL
from tests.app.extensions.apps.om_inventory.conftest import CLEAN_OUTCOME


async def _store_enabled(session: AsyncSession, *, enabled: bool) -> None:
    """Write the ``ENABLED`` override row ``run_probe`` republishes from.

    The row, not the proxy, is what the gate must obey, so every test below sets
    this and leaves the in-process snapshot saying the opposite.

    :param session: The session the override is written on.
    :param enabled: The stored value of the switch.
    """
    await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=setting_class_token(OmInventorySettings),
            key="ENABLED",
            value=enabled,
        ),
    )


@pytest.fixture
def _sweep_on_the_test_session(mocker: MockerFixture, session: AsyncSession) -> None:
    """Point ``run_probe`` at the test session and stub the Nomad-bound sweep.

    ``ENABLED`` is deliberately not touched here: each test stores the override it
    means to assert on, and sets the stale snapshot it means to be ignored.

    :param mocker: Patches the session maker and the sweep.
    :param session: The session every ``run_probe`` block should reuse.
    """
    mocker.patch.object(
        service_module,
        "get_async_session_maker",
        return_value=lambda: nullcontext(session),
    )
    mocker.patch.object(service_module, "sweep", AsyncMock(return_value=CLEAN_OUTCOME))


@pytest.mark.usefixtures("_sweep_on_the_test_session")
class TestRunProbeReadsTheStoredSwitch:
    """Obey the override row, whichever way this worker's snapshot is stale."""

    @pytest.mark.asyncio
    async def test_a_triggered_run_is_closed_as_skipped(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Close the trigger's row as ``SKIPPED``, naming the switch.

        :param session: The database session.
        :param monkeypatch: Leaves the snapshot stale at on.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", True)
        await _store_enabled(session, enabled=False)
        run = await ProbeRunManager.save(session, ProbeRun(scope=None))

        returned_id = await run_probe(execution_id=run.id, node_ids=None)

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.finished_at is not None
        assert stored.error == SWITCHED_OFF_DETAIL

    @pytest.mark.asyncio
    async def test_a_scheduled_run_is_recorded_as_skipped(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record a beat-driven sweep as ``SKIPPED`` instead of a silent gap.

        :param session: The database session.
        :param monkeypatch: Leaves the snapshot stale at on.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", True)
        await _store_enabled(session, enabled=False)

        returned_id = await run_probe(execution_id=None, node_ids=None)

        stored = await ProbeRunManager.get(session, id=returned_id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.error == SWITCHED_OFF_DETAIL

    @pytest.mark.asyncio
    async def test_a_stale_no_does_not_refuse_a_sweep_the_row_allows(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the sweep PMM triggers in the same breath as turning OM on.

        This is the regression: the snapshot still says off because this worker
        has not refreshed since the PATCH, and refusing here is what put a red
        error on the first page a new user opens.

        The assertion is the exact terminal state a clean sweep reaches, not
        merely "not ``SKIPPED``" - a run left ``RUNNING`` would satisfy that and
        is the other failure this gate exists to prevent.

        :param session: The database session.
        :param monkeypatch: Leaves the snapshot stale at off.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", False)
        await _store_enabled(session, enabled=True)
        run = await ProbeRunManager.save(session, ProbeRun(scope=None))

        returned_id = await run_probe(execution_id=run.id, node_ids=None)

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status is ProbeRunStatus.SUCCESS
        assert stored.error is None
        assert stored.finished_at is not None

    @pytest.mark.asyncio
    async def test_a_stale_yes_does_not_run_a_sweep_the_row_forbids(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse a sweep enqueued before the switch went off.

        The other direction of the same lag, and the one a flag carried on the
        message cannot fix: the snapshot says on, the row says off, and the row
        is the authority.

        :param session: The database session.
        :param monkeypatch: Leaves the snapshot stale at on.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", True)
        await _store_enabled(session, enabled=False)

        returned_id = await run_probe()

        stored = await ProbeRunManager.get(session, id=returned_id)
        assert stored.status is ProbeRunStatus.SKIPPED
        assert stored.error == SWITCHED_OFF_DETAIL
