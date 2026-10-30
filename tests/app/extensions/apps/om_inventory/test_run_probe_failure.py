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

Only ``sweep`` used to sit inside the ``try``/``except`` that calls ``fail_run``.
A raise while writing what the sweep found — ``persist_estate`` — or while closing
the run out — ``finalise`` — propagated past both without anything marking the row
failed, leaving it ``RUNNING`` forever: the one status a caller polling ``GET
/runs/{run_id}`` can never treat as "done, try again".

Pruning (``prune_runs``) is deliberately excluded from that widened path: a run that
persisted and finalised successfully is already done, and a pruning failure afterwards
must not rewrite its terminal status.
"""

import pickle
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from celery.utils.serialization import get_pickleable_exception
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory import service as service_module
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from app.extensions.apps.om_inventory.service import (
    FailureNotRecordedError,
    run_probe,
)
from tests.app.extensions.apps.om_inventory.conftest import CLEAN_OUTCOME

pytestmark = pytest.mark.usefixtures("enabled")


class TestPersistOrFinaliseFailureMarksTheRunFailed:
    """Route a raise after ``sweep`` returns to ``fail_run``."""

    @pytest.mark.asyncio
    async def test_a_persist_estate_failure_fails_the_run(
        self, service_session: AsyncSession
    ) -> None:
        """Mark the run ``FAILED``, not ``RUNNING``, when ``persist_estate`` raises.

        :param service_session: The session every sweep function opens.
        """
        run = await ProbeRunManager.save(service_session, ProbeRun())

        with (
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
        stored = await ProbeRunManager.get(service_session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.finished_at is not None
        assert stored.error == "disk full"

    @pytest.mark.asyncio
    async def test_a_finalise_failure_fails_the_run(
        self, service_session: AsyncSession
    ) -> None:
        """Mark the run ``FAILED``, not ``RUNNING``, when ``finalise`` raises.

        ``persist_estate`` runs for real here (with nothing for it to write, since
        ``CLEAN_OUTCOME`` carries no hosts or services) so this pins the *second*
        half of the widened window, not just the first.

        :param service_session: The session every sweep function opens.
        """
        run = await ProbeRunManager.save(service_session, ProbeRun())

        with (
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
        stored = await ProbeRunManager.get(service_session, id=run.id)
        assert stored.status == ProbeRunStatus.FAILED
        assert stored.finished_at is not None
        assert stored.error == "constraint violation"


class TestPruneFailureStaysOutsideTheFailurePath:
    """Keep a raise from pruning out of an already-finalised run."""

    @pytest.mark.asyncio
    async def test_a_prune_failure_does_not_touch_the_finished_run(
        self, service_session: AsyncSession
    ) -> None:
        """Leave pruning outside the ``try`` that covers persist and finalise.

        The run is written to ``SUCCESS`` by the real ``finalise`` before pruning
        ever runs. If pruning's failure were folded into the failure path, this run
        would come back ``FAILED`` with pruning's exception as its error; instead it
        must keep exactly what ``finalise`` wrote, and ``run_probe`` propagates the
        pruning exception rather than swallowing it.

        :param service_session: The session every sweep function opens.
        """
        run = await ProbeRunManager.save(service_session, ProbeRun())

        with (
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

        stored = await ProbeRunManager.get(service_session, id=run.id)
        assert stored.status == ProbeRunStatus.SUCCESS
        assert stored.finished_at is not None
        assert stored.error is None


class TestAFailureThatCannotBeWritten:
    """Hand back a failure the database refused, rather than drop it."""

    @pytest.mark.asyncio
    async def test_it_is_raised_for_the_caller_to_retry(
        self, service_session: AsyncSession
    ) -> None:
        """Raise with the run and the detail when the failure write cannot connect.

        :param service_session: The session every sweep function opens.
        """
        run = await ProbeRunManager.save(service_session, ProbeRun())

        with (
            patch.object(
                service_module,
                "sweep",
                AsyncMock(side_effect=RuntimeError("tasks API answered 500")),
            ),
            patch.object(
                service_module,
                "fail_run",
                AsyncMock(side_effect=ConnectionRefusedError(111, "refused")),
            ),
            pytest.raises(FailureNotRecordedError) as raised,
        ):
            await run_probe(execution_id=run.id)

        assert (raised.value.run_id, raised.value.error) == (
            run.id,
            "tasks API answered 500",
        )

    def test_it_survives_celery_pickling_it(self) -> None:
        """Keep the class, run, detail and message through Celery's pickling."""
        unrecorded = FailureNotRecordedError(uuid4(), "tasks API answered 500")

        pickled = get_pickleable_exception(unrecorded)

        assert pickled is unrecorded
        restored = pickle.loads(pickle.dumps(unrecorded))
        assert type(restored) is FailureNotRecordedError
        assert (restored.run_id, restored.error, str(restored)) == (
            unrecorded.run_id,
            unrecorded.error,
            str(unrecorded),
        )
