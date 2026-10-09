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

"""Test one full retained-set computation per persisted reference holder.

The scanner tests cover each read in isolation and the job tests stub the
retained set out entirely. These run the real composition — every declared
provider plus the built-in task-envelope scan — across all four databases, so
one holder silently dropping out of the union is caught here.
"""

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pytest_mock import MockerFixture
from sqlalchemy_celery_beat.models import IntervalSchedule, Period, PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.inventory import collection
from app.extensions.apps.inventory.collection import collect_referenced_entities
from app.extensions.apps.meta_keys import SERVICE_ID_META_KEY
from app.extensions.apps.mysql_backups.crud import MysqlBackupRunManager
from app.extensions.apps.mysql_backups.inventory_references import (
    referenced_inventory_entities,
)
from app.extensions.apps.mysql_backups.models import MysqlBackupRun
from app.inventory.constants import RetirableEntityName
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.models import (
    Task,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
)
from tests.app.factories import TaskFactory

SERVICE_ID = 5
EXECUTE_BY_NAME = "app.tasks.celery.execute_task_by_name"


def _session_maker(session: AsyncSession) -> Callable[[], Any]:
    """Return a session-maker factory yielding one already-built session.

    :param session: The session every ``async with`` block should receive.
    :return: A zero-argument callable standing in for a session maker.
    """

    @asynccontextmanager
    async def maker():
        yield session

    return maker


@pytest.fixture
def all_databases(
    mocker: MockerFixture,
    session: AsyncSession,
    celery_beat_session: AsyncSession,
) -> None:
    """Point every database the retained-set computation reads at the test ones.

    The tasks and PMM Extensions tables share one in-memory ``SQLModel`` metadata, so a
    single session serves both reads.
    """
    for maker in ("get_tasks_session_maker", "get_extensions_session_maker"):
        mocker.patch.object(collection, maker, return_value=_session_maker(session))
    mocker.patch.object(
        collection,
        "get_beat_session_maker",
        return_value=_session_maker(celery_beat_session),
    )
    mocker.patch.object(
        collection,
        "collect_inventory_reference_providers",
        return_value=[referenced_inventory_entities],
    )


async def _plain_task(session: AsyncSession) -> Task:
    """Save a task whose envelope names no inventory service.

    :param session: The tasks-database session.
    :return: The saved task.
    """
    return await TaskManager.save(
        session,
        TaskFactory.build(
            name="unrelated", data={"task": "run-command", "meta": {}}, deleted_at=None
        ),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("all_databases")
class TestCollectReferencedEntities:
    """Cover each blocking holder end to end, and the collectible case."""

    async def test_a_recorded_backup_run_retains_its_service(
        self, session: AsyncSession
    ) -> None:
        """Keep the service a catalog row still resolves by id."""
        await MysqlBackupRunManager.save(
            session,
            MysqlBackupRun(
                task_history_id=1,
                service_name="svc-a",
                service_id=SERVICE_ID,
                backup_type="M",
            ),
        )

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == {SERVICE_ID}

    async def test_a_task_envelope_retains_its_service(
        self, session: AsyncSession
    ) -> None:
        """Keep the service a live task can re-emit on its next run."""
        await TaskManager.save(
            session,
            TaskFactory.build(
                name="backup-a",
                data={"task": "run-command", "meta": {SERVICE_ID_META_KEY: SERVICE_ID}},
                deleted_at=None,
            ),
        )

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == {SERVICE_ID}

    async def test_an_in_flight_execution_retains_its_service(
        self, session: AsyncSession
    ) -> None:
        """Keep the service an execution that has not finished names."""
        task = await _plain_task(session)
        await TaskHistoryManager.save(
            session,
            TaskHistory(
                task_id=task.id,
                status=TaskHistoryStatusEnum.RUNNING,
                execution_request=TaskExecutionRequest(
                    task=task.name,
                    target="local",
                    meta={SERVICE_ID_META_KEY: SERVICE_ID},
                ),
            ),
        )

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == {SERVICE_ID}

    async def test_a_beat_schedule_retains_its_service(
        self, celery_beat_session: AsyncSession
    ) -> None:
        """Keep the service a scheduled-but-unfired envelope names."""
        schedule = IntervalSchedule(every=1, period=Period.DAYS)
        celery_beat_session.add(schedule)
        await celery_beat_session.flush()
        celery_beat_session.add(
            PeriodicTask(
                name="nightly",
                task=EXECUTE_BY_NAME,
                kwargs=json.dumps(
                    {
                        "task_name": "backup",
                        "execution_data": {"meta": {SERVICE_ID_META_KEY: SERVICE_ID}},
                    }
                ),
                schedule_model=schedule,
            )
        )
        await celery_beat_session.commit()

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == {SERVICE_ID}

    async def test_a_terminal_execution_alone_retains_nothing(
        self, session: AsyncSession
    ) -> None:
        """Let a service only finished history names be collected.

        A terminal execution cannot re-emit its id and already carries
        ``_service_name``, so treating it as blocking is what would make the
        mechanism degenerate into retaining everything.
        """
        task = await _plain_task(session)
        await TaskHistoryManager.save(
            session,
            TaskHistory(
                task_id=task.id,
                status=TaskHistoryStatusEnum.SUCCESS,
                execution_request=TaskExecutionRequest(
                    task=task.name,
                    target="local",
                    meta={SERVICE_ID_META_KEY: SERVICE_ID},
                ),
            ),
        )

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == set()

    async def test_nothing_referencing_retains_nothing(
        self, session: AsyncSession
    ) -> None:
        """Keep no service when no holder names one."""
        await _plain_task(session)

        referenced = await collect_referenced_entities()

        assert referenced[RetirableEntityName.SERVICE] == set()
