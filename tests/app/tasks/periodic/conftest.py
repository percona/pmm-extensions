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

"""Define test fixtures for periodic task tests."""

import json
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy_celery_beat import IntervalSchedule
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool
from starlette.testclient import TestClient

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.celery.deps import get_session as get_celery_beat_session
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.tasks.deps import get_session as get_tasks_session
from app.tasks.main import tasks_app
from tests.app.conftest import postgres_worker_schema
from tests.app.db_schema import apply_schema

CELERY_TASK_NAME = "app.tasks.celery.execute_task_by_name"


@pytest_asyncio.fixture
async def postgres_celery_beat_session(
    postgres_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Create a real-PostgreSQL session for celery-beat tables.

    Real-PG sibling of ``celery_beat_session``. The celery-beat tables declare a
    ``celery_schema``; route both it and the default schema into the per-worker
    schema. ``execution_options`` replaces the engine's translate map wholesale,
    so the ``None`` key is re-applied alongside ``celery_schema``.
    """
    schema = postgres_worker_schema()
    engine = postgres_engine.execution_options(
        schema_translate_map={"celery_schema": schema, None: schema}
    )
    metadata = PeriodicTask.__table__.metadata
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)


@pytest_asyncio.fixture(name="tasks_session")
async def tasks_session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async db session for tasks tables."""
    # scaffolding-dup-ok: this duplication predates the change that
    # re-annotated the fixture's return type; promoting it against
    # its sibling bootstrap is a cross-tree refactor of its own.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def periodic_test_client(
    regular_user: CasdoorUser,
    celery_beat_session: AsyncSession,
    tasks_session: AsyncSession,
) -> Iterator[TestClient]:
    """Create an authenticated test client with both celery beat and tasks sessions."""
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = lambda: (
        None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_celery_beat_session] = lambda: (
        celery_beat_session
    )
    tasks_app.dependency_overrides[get_tasks_session] = lambda: tasks_session
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def created_periodic_task(celery_beat_session: AsyncSession) -> PeriodicTask:
    """Create and return a periodic task in the celery beat database."""
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    celery_beat_session.add(schedule)
    await celery_beat_session.flush()

    task = PeriodicTask(
        name="test-periodic-task",
        task=CELERY_TASK_NAME,
        kwargs=json.dumps(
            {"task_name": "my-backup-task", "execution_data": {"meta": {}}}
        ),
        enabled=True,
        description="A test periodic task",
        schedule_model=schedule,
    )
    celery_beat_session.add(task)
    await celery_beat_session.commit()
    await celery_beat_session.refresh(task)
    return task
