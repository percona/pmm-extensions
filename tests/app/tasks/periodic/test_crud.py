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

"""Define test cases for periodic task CRUD operations."""

import json
import logging

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy_celery_beat import IntervalSchedule
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.exceptions import HTTPBadRequestException
from app.tasks.periodic.crud import PeriodicTaskManager

CELERY_TASK_NAME = "app.tasks.celery.execute_task_by_name"

PAGED_TASK_TOTAL = 7
PAGE_SIZE = 2


async def _seed_periodic_tasks(
    session: AsyncSession, *, other_task: str
) -> list[PeriodicTask]:
    """Seed two PMM Extensions managed periodic tasks plus one unmanaged row; return the managed rows.

    The managed rows carry ``task_name`` values ``backup-daily`` / ``restore-weekly``;
    the unmanaged row carries ``task_name`` ``unrelated``. ``other_task`` sets the
    unmanaged row's ``task`` column: the filter-query tests need it distinct from
    ``CELERY_TASK_NAME`` so the task filter excludes it, while the real-PG
    where-clause test needs it equal so only the ``kwargs`` predicate excludes it.
    """
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    session.add(schedule)
    await session.flush()

    managed = []
    for name in ("backup-daily", "restore-weekly"):
        task = PeriodicTask(
            name=f"periodic-{name}",
            task=CELERY_TASK_NAME,
            kwargs=json.dumps({"task_name": name, "execution_data": None}),
            enabled=True,
            description=f"Test {name}",
            schedule_model=schedule,
        )
        session.add(task)
        managed.append(task)

    session.add(
        PeriodicTask(
            name="other-celery-task",
            task=other_task,
            kwargs=json.dumps({"task_name": "unrelated"}),
            enabled=True,
            description="Not managed by PMM Extensions",
            schedule_model=schedule,
        )
    )
    await session.commit()
    return managed


@pytest_asyncio.fixture
async def periodic_tasks(celery_beat_session: AsyncSession) -> list[PeriodicTask]:
    """Create multiple periodic tasks for testing."""
    tasks = await _seed_periodic_tasks(
        celery_beat_session, other_task="some.other.celery.task"
    )
    for task in tasks:
        await celery_beat_session.refresh(task)
    return tasks


async def _seed_numbered_periodic_tasks(session: AsyncSession, count: int) -> list[str]:
    """Seed ``count`` PMM Extensions managed periodic tasks; return their names in creation order.

    The names are deliberately not in alphabetical order relative to creation, so a
    test walking pages cannot pass by accident on a name-sorted result set.
    """
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    session.add(schedule)
    await session.flush()

    names = [f"periodic-{(index * 3) % count}" for index in range(count)]
    for name in names:
        session.add(
            PeriodicTask(
                name=name,
                task=CELERY_TASK_NAME,
                kwargs=json.dumps({"task_name": "backup-daily"}),
                enabled=True,
                description="",
                schedule_model=schedule,
            )
        )
    await session.commit()
    return names


class TestPeriodicTaskManagerOrdering:
    """Cover the deterministic ordering that offset pagination depends on."""

    def test_manager_ordering_is_wired_into_the_emitted_sql(self):
        """Assert the paginated SELECT actually carries an ORDER BY clause.

        The row-order assertions below cannot fail on the in-memory SQLite beat
        store used by these tests: an unmodified-table scan happens to return
        insertion order even with no ``ORDER BY`` at all, so they stay green
        against an unordered query and only a real PostgreSQL run would expose
        it. Asserting on the compiled SQL is backend-independent.
        """
        ordering = PeriodicTaskManager._get_ordering()
        assert ordering, "manager must supply a default ordering"

        query = PeriodicTaskManager._build_query().order_by(*ordering).limit(PAGE_SIZE)
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))

        assert "ORDER BY" in sql.upper()
        assert "celery_periodictask.id" in sql[sql.upper().index("ORDER BY") :]

    @pytest.mark.asyncio
    async def test_paging_returns_every_row_exactly_once(self, celery_beat_session):
        """Assert walking every window yields each row once, none repeated or skipped."""
        await _seed_numbered_periodic_tasks(celery_beat_session, PAGED_TASK_TOTAL)

        paged_ids = []
        for offset in range(0, PAGED_TASK_TOTAL, PAGE_SIZE):
            page = await PeriodicTaskManager.list(
                celery_beat_session, offset=offset, limit=PAGE_SIZE
            )
            paged_ids.extend(task.id for task in page)

        all_ids = [
            task.id for task in await PeriodicTaskManager.list(celery_beat_session)
        ]
        assert len(paged_ids) == PAGED_TASK_TOTAL
        assert paged_ids == sorted(all_ids)

    @pytest.mark.asyncio
    async def test_repeating_a_window_returns_the_same_rows(self, celery_beat_session):
        """Assert the same offset/limit window is stable across requests."""
        await _seed_numbered_periodic_tasks(celery_beat_session, PAGED_TASK_TOTAL)

        first = await PeriodicTaskManager.list(
            celery_beat_session, offset=PAGE_SIZE, limit=PAGE_SIZE
        )
        second = await PeriodicTaskManager.list(
            celery_beat_session, offset=PAGE_SIZE, limit=PAGE_SIZE
        )

        assert [task.id for task in first] == [task.id for task in second]
        assert len(first) == PAGE_SIZE

    @pytest.mark.asyncio
    async def test_paging_by_task_names_returns_every_row_exactly_once(
        self, celery_beat_session
    ):
        """Assert the task-names branch pages deterministically too."""
        await _seed_numbered_periodic_tasks(celery_beat_session, PAGED_TASK_TOTAL)
        where_clause = PeriodicTaskManager.build_where_clause_by_task_names(
            "backup-daily"
        )

        paged_ids = []
        for offset in range(0, PAGED_TASK_TOTAL, PAGE_SIZE):
            page = await PeriodicTaskManager.list(
                celery_beat_session, where_clause, offset=offset, limit=PAGE_SIZE
            )
            paged_ids.extend(task.id for task in page)

        assert len(paged_ids) == PAGED_TASK_TOTAL
        assert paged_ids == sorted(paged_ids)
        assert len(set(paged_ids)) == PAGED_TASK_TOTAL


class TestPeriodicTaskManagerFilterQuery:
    """Test the _filter_query method of PeriodicTaskManager."""

    @pytest.mark.asyncio
    async def test_filter_query_only_returns_execute_task_by_name(
        self, celery_beat_session, periodic_tasks
    ):
        """Assert _filter_query always filters by the execute_task_by_name task."""
        results = await PeriodicTaskManager.list(celery_beat_session)
        task_names = {t.task for t in results}
        assert task_names == {CELERY_TASK_NAME}
        assert len(results) == len(periodic_tasks)


class TestPeriodicTaskManagerSave:
    """Test the save method of PeriodicTaskManager."""

    @pytest.mark.asyncio
    async def test_save_sets_task_to_celery_name(self, celery_beat_session):
        """Assert save forces the task field to execute_task_by_name."""
        schedule = IntervalSchedule(every=5, period=Period.HOURS)
        celery_beat_session.add(schedule)
        await celery_beat_session.flush()

        instance = PeriodicTask(
            name="save-test",
            task="wrong.task.name",
            kwargs=json.dumps({"task_name": "test"}),
            enabled=True,
            description="",
            schedule_model=schedule,
        )
        saved = await PeriodicTaskManager.save(celery_beat_session, instance)
        assert saved.task == CELERY_TASK_NAME


async def _save_nameless_periodic_task(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Clear a stored schedule's name and save it, expecting the bad-request answer.

    The schedule's text form reads its related interval row, which the rollback
    after the failed commit has expired, so a log call that formats the
    instance would load it outside the async context.

    :param session: A session bound to the celery-beat tables.
    :param caplog: The log capture fixture, recording the failure's log record.
    """
    schedule = IntervalSchedule(every=5, period=Period.HOURS)
    session.add(schedule)
    await session.flush()
    instance = await PeriodicTaskManager.save(
        session,
        PeriodicTask(
            name="nameless",
            task=CELERY_TASK_NAME,
            kwargs=json.dumps({"task_name": "test"}),
            enabled=True,
            description="",
            schedule_model=schedule,
        ),
    )
    instance.name = None

    with (
        caplog.at_level(logging.ERROR, logger="app.core.db.crud"),
        pytest.raises(HTTPBadRequestException),
    ):
        await PeriodicTaskManager.save(session, instance)

    [record] = [r for r in caplog.records if r.name == "app.core.db.crud"]
    assert "PeriodicTask" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], IntegrityError)


class TestPeriodicTaskManagerSaveRejectedWrite:
    """Test that a write the database rejects answers bad request."""

    @pytest.mark.asyncio
    async def test_null_name_raises_bad_request(
        self, celery_beat_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ):
        """Assert a schedule saved without a name raises the bad-request answer."""
        await _save_nameless_periodic_task(celery_beat_session, caplog)


@pytest.mark.postgres
class TestPeriodicTaskManagerSaveRejectedWriteOnPostgres:
    """Test the rejected write on a real PostgreSQL bind."""

    @pytest.mark.asyncio
    async def test_null_name_raises_bad_request(
        self,
        postgres_celery_beat_session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ):
        """Assert a schedule saved without a name raises the bad-request answer."""
        await _save_nameless_periodic_task(postgres_celery_beat_session, caplog)


class TestPeriodicTaskManagerListByTaskNames:
    """Test the list_by_task_names method of PeriodicTaskManager."""

    @pytest.mark.asyncio
    async def test_list_by_single_task_name(self, celery_beat_session, periodic_tasks):
        """Assert filtering by a single task name returns matching tasks."""
        results = await PeriodicTaskManager.list_by_task_names(
            celery_beat_session, "backup-daily"
        )
        assert len(results) == 1
        assert results[0].name == "periodic-backup-daily"

    @pytest.mark.asyncio
    async def test_list_by_multiple_task_names(
        self, celery_beat_session, periodic_tasks
    ):
        """Assert filtering by multiple task names returns all matches."""
        results = await PeriodicTaskManager.list_by_task_names(
            celery_beat_session, "backup-daily", "restore-weekly"
        )
        assert len(results) == len(periodic_tasks)
        result_names = {t.name for t in results}
        assert result_names == {"periodic-backup-daily", "periodic-restore-weekly"}

    @pytest.mark.asyncio
    async def test_list_by_nonexistent_task_name(
        self, celery_beat_session, periodic_tasks
    ):
        """Assert filtering by a non-existent task name returns empty list."""
        results = await PeriodicTaskManager.list_by_task_names(
            celery_beat_session, "nonexistent-task"
        )
        assert results == []


class TestPeriodicTaskManagerBuildWhereClause:
    """Test the build_where_clause_by_task_names static method."""

    def test_returns_clause(self):
        """Assert build_where_clause_by_task_names returns a valid SQLAlchemy clause."""
        clause = PeriodicTaskManager.build_where_clause_by_task_names(
            "task-a", "task-b"
        )
        assert clause is not None
        compiled = str(clause.compile())
        assert "IN" in compiled

    def test_renders_cast_on_postgres(self, mocker):
        """Render ``CAST(kwargs AS JSON) ->> 'task_name'`` on PostgreSQL.

        ``sqlalchemy-celery-beat`` defines ``PeriodicTask.kwargs`` as a plain
        ``sa.Text()`` column. PostgreSQL's ``->>`` operator is not defined
        on ``text``, so the helper must cast the column to ``JSON`` before
        extracting. Without the cast, every call that reaches this WHERE
        clause fails with ``operator does not exist: text ->> unknown`` on
        PG deployments.
        """
        mocker.patch.object(
            settings.CELERY, "beat_dburi", "postgresql://user:pass@localhost/db"
        )

        clause = PeriodicTaskManager.build_where_clause_by_task_names(
            "backup-daily", "restore-weekly"
        )

        rendered = str(
            clause.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "CAST(" in rendered.upper()
        assert "AS JSON" in rendered.upper()
        assert "->>" in rendered
        assert "'task_name'" in rendered
        assert "IN (" in rendered.upper()

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_build_where_clause_executes_on_real_postgres(
        self, postgres_celery_beat_session, mocker
    ):
        """Execute ``CAST(kwargs AS JSON) ->> 'task_name' IN (...)`` on real PostgreSQL.

        Real-engine sibling of ``test_renders_cast_on_postgres``. Two things must
        both hold: the helper dispatches on ``settings.CELERY.beat_dburi`` (not the
        session bind), so ``beat_dburi`` is patched to a ``postgresql://`` URL; and
        the clause must execute against the real PG engine. ``PeriodicTask.kwargs``
        is a ``sa.Text()`` column, so the ``text``-to-``CAST``-to-``->>`` path runs
        — the text-column regression surface — and only the matching rows come back.
        """
        mocker.patch.object(
            settings.CELERY, "beat_dburi", "postgresql://user:pass@localhost/db"
        )
        await _seed_periodic_tasks(
            postgres_celery_beat_session, other_task=CELERY_TASK_NAME
        )

        clause = PeriodicTaskManager.build_where_clause_by_task_names(
            "backup-daily", "restore-weekly"
        )
        result = await postgres_celery_beat_session.exec(
            select(PeriodicTask).where(clause)
        )

        names = {task.name for task in result.scalars().all()}
        assert names == {"periodic-backup-daily", "periodic-restore-weekly"}
