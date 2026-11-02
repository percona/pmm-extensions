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

"""Test that ``run_probe`` marks a run failed for the whole width of its own work.

Only ``sweep`` used to sit inside the ``try``/``except`` that calls ``_fail_run``.
A raise while writing what the sweep found — ``persist_estate`` — or while closing
the run out — ``finalise`` — propagated past both without anything marking the row
failed, leaving it ``RUNNING`` forever: the one status a caller polling ``GET
/runs/{run_id}`` can never treat as "done, try again".

Pruning (``prune_runs``) is deliberately excluded from that widened path: a run that
persisted and finalised successfully is already done, and a pruning failure afterwards
must not rewrite its terminal status.

The run's heartbeat covers the sweep and the estate write, a run that is no longer
running when its message arrives is not swept at all, and a sweep whose run was
failed under it stops without writing over that failure.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory import service as service_module
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import INTERRUPTED_DETAIL, ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from app.extensions.apps.om_inventory.service import run_probe, RunLostError
from tests.app.extensions.apps.om_inventory.conftest import CLEAN_OUTCOME


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the sweep on, since ``run_probe`` refuses to run while it is off.

    :param monkeypatch: Restores the real value after the test.
    """
    monkeypatch.setattr(om_inventory_settings, "ENABLED", True)


def _session_maker(session: AsyncSession):
    """Build the ``get_async_session_maker`` stand-in the sweep functions expect.

    Every caller in ``service.py`` does ``async with session_maker() as session:``,
    so the replacement has to be a zero-argument callable returning something
    ``async with``-able. :class:`contextlib.nullcontext` supports the async
    protocol as well as the sync one, and handing back the same test session on
    every call is exactly what lets assertions see what ``run_probe`` wrote.

    :param session: The test session every call should reuse.
    :return: The stand-in callable.
    """
    return lambda: nullcontext(session)


class TestPersistOrFinaliseFailureMarksTheRunFailed:
    """Route a raise after ``sweep`` returns to ``_fail_run``."""

    @pytest.mark.asyncio
    async def test_a_persist_estate_failure_fails_the_run(
        self, session: AsyncSession
    ) -> None:
        """Mark the run ``FAILED``, not ``RUNNING``, when ``persist_estate`` raises.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(
                service_module, "sweep", AsyncMock(return_value=CLEAN_OUTCOME)
            ),
            patch.object(
                service_module,
                "persist_estate",
                AsyncMock(side_effect=RuntimeError("disk full")),
            ),
        ):
            returned_id = await run_probe(execution_id=run.id, node_ids=[])

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.finished_at is not None
        assert stored.error == "disk full"

    @pytest.mark.asyncio
    async def test_a_finalise_failure_fails_the_run(
        self, session: AsyncSession
    ) -> None:
        """Mark the run ``FAILED``, not ``RUNNING``, when ``finalise`` raises.

        ``persist_estate`` runs for real here (with nothing for it to write, since
        ``CLEAN_OUTCOME`` carries no hosts or services) so this pins the *second*
        half of the widened window, not just the first.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(
                service_module, "sweep", AsyncMock(return_value=CLEAN_OUTCOME)
            ),
            patch.object(
                service_module,
                "finalise",
                AsyncMock(side_effect=RuntimeError("constraint violation")),
            ),
        ):
            returned_id = await run_probe(execution_id=run.id, node_ids=[])

        assert returned_id == run.id
        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.finished_at is not None
        assert stored.error == "constraint violation"


class TestPruneFailureStaysOutsideTheFailurePath:
    """Keep a raise from pruning out of an already-finalised run."""

    @pytest.mark.asyncio
    async def test_a_prune_failure_does_not_touch_the_finished_run(
        self, session: AsyncSession
    ) -> None:
        """Leave pruning outside the ``try`` that covers persist and finalise.

        The run is written to ``SUCCESS`` by the real ``finalise`` before pruning
        ever runs. If pruning's failure were folded into the failure path, this run
        would come back ``FAILED`` with pruning's exception as its error; instead it
        must keep exactly what ``finalise`` wrote, and ``run_probe`` propagates the
        pruning exception rather than swallowing it.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(
                service_module, "sweep", AsyncMock(return_value=CLEAN_OUTCOME)
            ),
            patch.object(
                service_module,
                "prune_runs",
                AsyncMock(side_effect=RuntimeError("prune boom")),
            ),
            pytest.raises(RuntimeError, match="prune boom"),
        ):
            await run_probe(execution_id=run.id, node_ids=[])

        stored = await ProbeRunManager.get(session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS
        assert stored.finished_at is not None
        assert stored.error is None


class TestTheRunIsHeldForItsWholeWidth:
    """Sweep under the run's heartbeat, and never for a run no longer waiting."""

    @pytest.mark.asyncio
    async def test_the_sweep_runs_inside_the_heartbeat(
        self, session: AsyncSession
    ) -> None:
        """Keep the run alive across the sweep and the estate write, then finalise.

        ``finalise`` comes after the heartbeat stops, because its next write would
        find the finished run and take it for lost.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())
        beating = [False]
        seen: list[bool] = []

        @asynccontextmanager
        async def heartbeat(_run_id: object) -> AsyncIterator[None]:
            beating[0] = True
            yield
            beating[0] = False

        async def record(*_args: object, **_kwargs: object) -> object:
            seen.append(beating[0])
            return CLEAN_OUTCOME

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(service_module, "_heartbeat", heartbeat),
            patch.object(service_module, "sweep", AsyncMock(side_effect=record)),
            patch.object(
                service_module, "persist_estate", AsyncMock(side_effect=record)
            ),
            patch.object(service_module, "finalise", AsyncMock(side_effect=record)),
        ):
            await run_probe(execution_id=run.id, node_ids=[])

        assert seen == [True, True, False]

    @pytest.mark.asyncio
    async def test_a_lost_run_is_left_as_the_lease_failed_it(
        self, session: AsyncSession
    ) -> None:
        """Stop without a failure write when the heartbeat finds the run gone.

        :param session: The database session.
        """
        run = await ProbeRunManager.save(session, ProbeRun())
        fail_run = AsyncMock()

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(
                service_module, "sweep", AsyncMock(side_effect=RunLostError(run.id))
            ),
            patch.object(service_module, "_fail_run", fail_run),
        ):
            assert await run_probe(execution_id=run.id, node_ids=[]) == run.id

        fail_run.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("run_status", "error"),
        [
            pytest.param(ProbeRunStatus.FAILED, INTERRUPTED_DETAIL, id="reaped"),
            pytest.param(ProbeRunStatus.SUCCESS, None, id="finished"),
        ],
    )
    async def test_a_run_no_longer_running_is_not_swept(
        self, session: AsyncSession, run_status: ProbeRunStatus, error: str | None
    ) -> None:
        """Leave a run its message reached too late as it is, and sweep nothing.

        The lease fails a run whose message waited longer than it, as one does while
        the worker is down. Sweeping it then would overwrite that failure, after a
        newer scan was already let through on the strength of it.

        :param session: The database session.
        :param run_status: The run's status when its message arrives.
        :param error: Its failure detail.
        """
        run = await ProbeRunManager.save(
            session, ProbeRun(status=run_status, error=error)
        )
        sweep = AsyncMock(return_value=CLEAN_OUTCOME)

        with (
            patch.object(
                service_module,
                "get_async_session_maker",
                return_value=_session_maker(session),
            ),
            patch.object(service_module, "sweep", sweep),
        ):
            assert await run_probe(execution_id=run.id, node_ids=[]) == run.id

        sweep.assert_not_awaited()
        stored = await ProbeRunManager.get(session, id=run.id)
        assert (stored.status, stored.error) == (run_status, error)
