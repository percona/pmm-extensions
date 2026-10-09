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

"""Test the built-in task-envelope scan backing inventory collection.

These scanners are the safety gate the whole job rests on: the route tests pass
``keep`` in directly and the job tests stub the providers, so nothing else
exercises them.
"""

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pytest_mock import MockerFixture
from sqlalchemy_celery_beat.models import IntervalSchedule, Period, PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.inventory.collection import collect_task_envelope_service_ids
from app.extensions.apps.meta_keys import SERVICE_ID_META_KEY
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.models import (
    Task,
    TaskExecutionRequest,
    TaskHistory,
    TaskHistoryStatusEnum,
)
from tests.app.factories import TaskFactory

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
def scan_databases(
    mocker: MockerFixture, session: AsyncSession, celery_beat_session: AsyncSession
) -> None:
    """Point the scan's three reads at the in-memory test databases."""
    mocker.patch(
        "app.extensions.apps.inventory.collection.get_tasks_session_maker",
        return_value=_session_maker(session),
    )
    mocker.patch(
        "app.extensions.apps.inventory.collection.get_beat_session_maker",
        return_value=_session_maker(celery_beat_session),
    )


async def _create_task(session: AsyncSession, name: str, meta: Any) -> Task:
    """Save a task whose envelope carries the given ``meta``.

    :param session: The tasks-database session.
    :param name: The task name, unique per row.
    :param meta: The value to place under ``data["meta"]``.
    :return: The saved task.
    """
    task = TaskFactory.build(
        name=name, data={"task": "run-command", "meta": meta}, deleted_at=None
    )
    return await TaskManager.save(session, task)


async def _create_history(
    session: AsyncSession,
    task: Task,
    status: TaskHistoryStatusEnum,
    service_id: int,
) -> TaskHistory:
    """Save an execution whose frozen request names a service id.

    :param session: The tasks-database session.
    :param task: The task the execution belongs to.
    :param status: The execution status to record.
    :param service_id: The inventory service id to stamp into the request meta.
    :return: The saved execution.
    """
    return await TaskHistoryManager.save(
        session,
        TaskHistory(
            task_id=task.id,
            status=status,
            execution_request=TaskExecutionRequest(
                task=task.name,
                target="local",
                meta={SERVICE_ID_META_KEY: service_id},
            ),
        ),
    )


async def _create_beat_row(session: AsyncSession, name: str, kwargs: str) -> None:
    """Save a beat schedule row carrying the given raw ``kwargs`` string.

    :param session: The celery-beat database session.
    :param name: The schedule name, unique per row.
    :param kwargs: The raw JSON string to store in the text column.
    """
    schedule = IntervalSchedule(every=1, period=Period.DAYS)
    session.add(schedule)
    await session.flush()
    session.add(
        PeriodicTask(
            name=name,
            task=EXECUTE_BY_NAME,
            kwargs=kwargs,
            schedule_model=schedule,
        )
    )
    await session.commit()


def _beat_kwargs(meta: Any) -> str:
    """Render the beat ``kwargs`` payload wrapping an envelope ``meta``.

    :param meta: The value to nest under ``execution_data.meta``.
    :return: The JSON string the text column stores.
    """
    return json.dumps({"task_name": "backup", "execution_data": {"meta": meta}})


@pytest.mark.asyncio
async def test_task_envelope_meta_is_retained(
    scan_databases: None, session: AsyncSession
) -> None:
    """Keep the service a live task envelope still names."""
    await _create_task(session, "backup-a", {SERVICE_ID_META_KEY: 7})

    assert await collect_task_envelope_service_ids() == {7}


@pytest.mark.asyncio
async def test_soft_deleted_task_is_still_scanned(
    scan_databases: None, session: AsyncSession
) -> None:
    """Keep a soft-deleted task's service — its recorder can still fire.

    The scan-to-delete window is closed by the fact that a run-result recorder
    resolves off the live ``Task`` row, and a soft-deleted row still satisfies
    that relationship. Filtering on ``deleted_at`` here would break the argument
    while passing every test that does not build a deleted task.
    """
    task = await _create_task(session, "backup-a", {SERVICE_ID_META_KEY: 7})
    await TaskManager.delete_by_name(session, task.name)

    assert await collect_task_envelope_service_ids() == {7}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({}, id="no-key"),
        pytest.param({"other": 1}, id="other-keys-only"),
        pytest.param("not-a-mapping", id="meta-not-a-mapping"),
        pytest.param({SERVICE_ID_META_KEY: "abc"}, id="non-integer"),
        pytest.param({SERVICE_ID_META_KEY: 0}, id="zero"),
        pytest.param({SERVICE_ID_META_KEY: -3}, id="negative"),
        pytest.param({SERVICE_ID_META_KEY: None}, id="null"),
    ],
)
async def test_malformed_task_meta_is_skipped(
    scan_databases: None, session: AsyncSession, meta: Any
) -> None:
    """Skip an envelope that names no usable service id, without raising."""
    await _create_task(session, "backup-a", meta)

    assert await collect_task_envelope_service_ids() == set()


@pytest.mark.asyncio
async def test_task_without_meta_is_skipped(
    scan_databases: None, session: AsyncSession
) -> None:
    """Skip a task whose envelope has no ``meta`` at all."""
    await TaskManager.save(
        session, TaskFactory.build(name="plain", data={"task": "run-command"})
    )

    assert await collect_task_envelope_service_ids() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [TaskHistoryStatusEnum.PENDING, TaskHistoryStatusEnum.RUNNING],
)
async def test_in_flight_history_is_retained(
    scan_databases: None, session: AsyncSession, status: TaskHistoryStatusEnum
) -> None:
    """Keep the service an execution that can still write names."""
    task = await _create_task(session, "backup-a", {})
    await _create_history(session, task, status, 11)

    assert await collect_task_envelope_service_ids() == {11}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        status
        for status in TaskHistoryStatusEnum
        if status not in TaskHistoryStatusEnum.active_statuses()
    ],
)
async def test_terminal_history_alone_does_not_retain(
    scan_databases: None, session: AsyncSession, status: TaskHistoryStatusEnum
) -> None:
    """Let a finished execution's service be collected — history cannot re-emit."""
    task = await _create_task(session, "backup-a", {})
    await _create_history(session, task, status, 11)

    assert await collect_task_envelope_service_ids() == set()


@pytest.mark.asyncio
async def test_beat_schedule_kwargs_is_retained(
    scan_databases: None, celery_beat_session: AsyncSession
) -> None:
    """Keep the service a scheduled-but-unfired envelope names."""
    await _create_beat_row(
        celery_beat_session, "nightly", _beat_kwargs({SERVICE_ID_META_KEY: 21})
    )

    assert await collect_task_envelope_service_ids() == {21}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param("", id="empty"),
        pytest.param("{}", id="empty-object"),
        pytest.param("not json", id="malformed"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"execution_data": "nope"}', id="execution-data-not-a-mapping"),
        pytest.param(_beat_kwargs({SERVICE_ID_META_KEY: []}), id="unhashable-list-id"),
        pytest.param(
            _beat_kwargs({SERVICE_ID_META_KEY: {"nested": 1}}),
            id="unhashable-object-id",
        ),
    ],
)
async def test_malformed_beat_kwargs_is_skipped(
    scan_databases: None, celery_beat_session: AsyncSession, kwargs: str
) -> None:
    """Skip an unreadable schedule payload rather than raising on it."""
    await _create_beat_row(celery_beat_session, "nightly", kwargs)

    assert await collect_task_envelope_service_ids() == set()


@pytest.mark.asyncio
async def test_an_unreachable_beat_database_raises(
    scan_databases: None, mocker: MockerFixture
) -> None:
    """Fail the run rather than read a missing holder as an unreferenced one."""
    mocker.patch(
        "app.extensions.apps.inventory.collection.get_beat_session_maker",
        side_effect=OSError("beat database unreachable"),
    )

    with pytest.raises(OSError, match="beat database unreachable"):
        await collect_task_envelope_service_ids()


@pytest.mark.postgres
@pytest.mark.asyncio
class TestEnvelopeExtractionOnPostgres:
    """Exercise the envelope extraction against a real PostgreSQL.

    The JSON extraction is dialect-specific: SQLite's ``json_extract`` is
    type-permissive while PostgreSQL's ``->>`` is defined only on JSON-typed
    columns and hands back text rather than the native scalar. That divergence
    passes silently on SQLite and fails only here, so the predicate needs its own
    lane. The ``postgres`` marker is what puts these in CI's PostgreSQL lane.
    """

    @pytest.fixture(autouse=True)
    def _postgres_scan_databases(
        self,
        mocker: MockerFixture,
        postgres_session: AsyncSession,
        celery_beat_session: AsyncSession,
    ) -> None:
        """Point the tasks half of the scan at the real PostgreSQL."""
        mocker.patch(
            "app.extensions.apps.inventory.collection.get_tasks_session_maker",
            return_value=_session_maker(postgres_session),
        )
        mocker.patch(
            "app.extensions.apps.inventory.collection.get_beat_session_maker",
            return_value=_session_maker(celery_beat_session),
        )

    async def test_task_envelope_ids_match_the_sqlite_run(
        self, postgres_session: AsyncSession
    ) -> None:
        """Return the same service ids the SQLite-backed scan returns."""
        await _create_task(postgres_session, "backup-a", {SERVICE_ID_META_KEY: 7})
        await _create_task(postgres_session, "backup-b", {SERVICE_ID_META_KEY: "abc"})
        await _create_task(postgres_session, "backup-c", {"other": 1})
        await _create_task(postgres_session, "backup-d", "not-a-mapping")

        assert await collect_task_envelope_service_ids() == {7}

    async def test_in_flight_history_ids_match_the_sqlite_run(
        self, postgres_session: AsyncSession
    ) -> None:
        """Return the in-flight execution's service id and no terminal one."""
        task = await _create_task(postgres_session, "backup-a", {})
        await _create_history(postgres_session, task, TaskHistoryStatusEnum.RUNNING, 11)
        await _create_history(postgres_session, task, TaskHistoryStatusEnum.SUCCESS, 12)

        assert await collect_task_envelope_service_ids() == {11}
