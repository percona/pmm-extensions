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

"""Define test cases for periodic task routes."""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import status
from sqlalchemy import select
from sqlalchemy_celery_beat import IntervalSchedule
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.celery.schedules import INTERVAL_TIMEZONE, NEXT_RUNS_PREVIEW_COUNT
from app.core.pagination import DEFAULT_PAGINATION_LIMIT, DEFAULT_PAGINATION_OFFSET
from app.core.utils.date_time import utc_now
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.models import (
    SYSTEM_USER,
    Task,
    TaskHistory,
    TaskHistoryStatusEnum,
    TaskWrite,
)
from tests.app.factories import TaskFactory

CELERY_TASK_NAME = "app.tasks.celery.execute_task_by_name"
PAGED_PERIODIC_TOTAL = 5
UNFILTERED_PAGE_OFFSET = 1
UNFILTERED_PAGE_LIMIT = 2
OWNER_FILTER_MATCH_TOTAL = 3
OWNER_FILTER_PAGE_LIMIT = 2
OWNER_NAME = "BACKUPS"

#: A day count well inside ``timedelta``'s range, so the cadence builds, whose
#: upcoming runs still land past ``datetime.max``. Guarding only the cadence
#: would let this one through.
OVERFLOWS_RUN_DATETIME = 500_000_000

#: A day count past ``timedelta``'s own range, so the cadence cannot be built.
OVERFLOWS_CADENCE_TIMEDELTA = 2_000_000_000

#: A ``start_time`` close enough to ``datetime.max`` that the runs after the
#: first overflow, whatever the cadence.
OVERFLOWS_FROM_START_TIME = "9999-12-31T23:00:00Z"


async def _add_periodic_task(
    celery_beat_session: AsyncSession,
    *,
    name: str,
    task_name: str,
    enabled: bool = True,
    last_run_at: datetime | None = None,
) -> PeriodicTask:
    """Create and persist a periodic task bound to ``task_name``."""
    schedule = IntervalSchedule(every=10, period=Period.MINUTES)
    celery_beat_session.add(schedule)
    await celery_beat_session.flush()

    task = PeriodicTask(
        name=name,
        task=CELERY_TASK_NAME,
        kwargs=json.dumps({"task_name": task_name, "execution_data": None}),
        enabled=enabled,
        last_run_at=last_run_at,
        schedule_model=schedule,
    )
    celery_beat_session.add(task)
    await celery_beat_session.commit()
    await celery_beat_session.refresh(task)
    return task


async def _add_history(
    tasks_session: AsyncSession,
    *,
    task_name: str,
    task_status: TaskHistoryStatusEnum,
    executed_by: str | None = SYSTEM_USER,
) -> None:
    """Persist a task plus one history row for ``task_name`` in the tasks DB."""
    task = await TaskManager.create(
        tasks_session, TaskWrite.model_validate(TaskFactory.build(name=task_name))
    )
    history = TaskHistory(
        task_id=task.id,
        status=task_status,
        executed_by=executed_by,
        execution_request={
            "task": task_name,
            "target": "localhost",
            "meta": {},
            "tracking": {"allocation_id": None, "evaluation_id": None},
        },
    )
    await TaskHistoryManager.save(tasks_session, history)


async def _add_history_row(
    tasks_session: AsyncSession,
    task: Task,
    *,
    task_status: TaskHistoryStatusEnum,
    created_at: datetime,
    executed_by: str | None = SYSTEM_USER,
) -> None:
    """Persist one history row for an existing task at an explicit ``created_at``."""
    history = TaskHistory(
        task_id=task.id,
        status=task_status,
        created_at=created_at,
        executed_by=executed_by,
        execution_request={
            "task": task.name,
            "target": "localhost",
            "meta": {},
            "tracking": {"allocation_id": None, "evaluation_id": None},
        },
    )
    await TaskHistoryManager.save(tasks_session, history)


@pytest_asyncio.fixture
async def second_periodic_task(celery_beat_session: AsyncSession) -> PeriodicTask:
    """Create a second periodic task for list tests."""
    schedule = IntervalSchedule(every=1, period=Period.HOURS)
    celery_beat_session.add(schedule)
    await celery_beat_session.flush()

    task = PeriodicTask(
        name="second-periodic",
        task=CELERY_TASK_NAME,
        kwargs=json.dumps({"task_name": "other-task", "execution_data": None}),
        enabled=False,
        description="Second periodic task",
        schedule_model=schedule,
    )
    celery_beat_session.add(task)
    await celery_beat_session.commit()
    await celery_beat_session.refresh(task)
    return task


class TestListPeriodicTasks:
    """Test the GET /periodic/ endpoint."""

    def test_list_all_periodic_tasks(self, periodic_test_client, created_periodic_task):
        """Assert listing all periodic tasks returns a paginated envelope."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 1
        assert data["offset"] == DEFAULT_PAGINATION_OFFSET
        assert data["limit"] == DEFAULT_PAGINATION_LIMIT
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "test-periodic-task"
        assert data["items"][0]["task"] == "my-backup-task"

    def test_list_periodic_tasks_empty(self, periodic_test_client):
        """Assert listing when none exist returns an empty paginated envelope."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "items": [],
            "total": 0,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }

    def test_list_periodic_tasks_filter_enabled(
        self,
        periodic_test_client,
        created_periodic_task,
        second_periodic_task,
    ):
        """Assert filtering by enabled returns only enabled tasks and their total."""
        response = periodic_test_client.get("/periodic/", params={"enabled": True})
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_paginates_unfiltered_window(
        self, periodic_test_client, celery_beat_session
    ):
        """Assert the unfiltered branch returns at most limit rows with full total."""
        for index in range(PAGED_PERIODIC_TOTAL):
            await _add_periodic_task(
                celery_beat_session,
                name=f"unfiltered-{index}",
                task_name=f"task-{index}",
            )

        response = periodic_test_client.get(
            "/periodic/",
            params={"offset": UNFILTERED_PAGE_OFFSET, "limit": UNFILTERED_PAGE_LIMIT},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == PAGED_PERIODIC_TOTAL
        assert data["offset"] == UNFILTERED_PAGE_OFFSET
        assert data["limit"] == UNFILTERED_PAGE_LIMIT
        assert len(data["items"]) == UNFILTERED_PAGE_LIMIT

        full = periodic_test_client.get(
            "/periodic/", params={"offset": 0, "limit": PAGED_PERIODIC_TOTAL}
        ).json()
        window_end = UNFILTERED_PAGE_OFFSET + UNFILTERED_PAGE_LIMIT
        assert [row["id"] for row in data["items"]] == [
            row["id"] for row in full["items"][UNFILTERED_PAGE_OFFSET:window_end]
        ]

    @pytest.mark.asyncio
    async def test_list_pages_are_deterministic_across_requests(
        self, periodic_test_client, celery_beat_session
    ):
        """Assert re-requesting a window and walking every window loses no rows."""
        for index in range(PAGED_PERIODIC_TOTAL):
            await _add_periodic_task(
                celery_beat_session,
                name=f"stable-{index}",
                task_name=f"stable-task-{index}",
            )

        first_ids = [
            row["id"]
            for row in periodic_test_client.get(
                "/periodic/",
                params={
                    "offset": UNFILTERED_PAGE_LIMIT,
                    "limit": UNFILTERED_PAGE_LIMIT,
                },
            ).json()["items"]
        ]
        second_ids = [
            row["id"]
            for row in periodic_test_client.get(
                "/periodic/",
                params={
                    "offset": UNFILTERED_PAGE_LIMIT,
                    "limit": UNFILTERED_PAGE_LIMIT,
                },
            ).json()["items"]
        ]
        assert first_ids == second_ids
        assert len(first_ids) == UNFILTERED_PAGE_LIMIT

        paged_ids = []
        for offset in range(0, PAGED_PERIODIC_TOTAL, UNFILTERED_PAGE_LIMIT):
            page = periodic_test_client.get(
                "/periodic/",
                params={"offset": offset, "limit": UNFILTERED_PAGE_LIMIT},
            ).json()
            paged_ids.extend(row["id"] for row in page["items"])
        assert paged_ids == sorted(paged_ids)
        assert len(paged_ids) == PAGED_PERIODIC_TOTAL
        assert len(set(paged_ids)) == PAGED_PERIODIC_TOTAL

    @pytest.mark.asyncio
    async def test_list_paginates_owner_filtered_window(
        self, periodic_test_client, celery_beat_session, tasks_session
    ):
        """Assert the owner branch pages only schedules whose tasks match the owner."""
        for index in range(OWNER_FILTER_MATCH_TOTAL):
            await TaskManager.create(
                tasks_session,
                TaskWrite.model_validate(
                    TaskFactory.build(name=f"owned-task-{index}", owner=OWNER_NAME)
                ),
            )
            await _add_periodic_task(
                celery_beat_session,
                name=f"owned-schedule-{index}",
                task_name=f"owned-task-{index}",
            )
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(
                TaskFactory.build(name="other-owner-task", owner="OTHER")
            ),
        )
        await _add_periodic_task(
            celery_beat_session,
            name="other-owner-schedule",
            task_name="other-owner-task",
        )

        response = periodic_test_client.get(
            "/periodic/",
            params={
                "owner": OWNER_NAME,
                "offset": 0,
                "limit": OWNER_FILTER_PAGE_LIMIT,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["total"] == OWNER_FILTER_MATCH_TOTAL
        assert data["offset"] == 0
        assert data["limit"] == OWNER_FILTER_PAGE_LIMIT
        assert len(data["items"]) == OWNER_FILTER_PAGE_LIMIT
        assert all(row["name"].startswith("owned-schedule-") for row in data["items"])

        remainder = periodic_test_client.get(
            "/periodic/",
            params={
                "owner": OWNER_NAME,
                "offset": OWNER_FILTER_PAGE_LIMIT,
                "limit": OWNER_FILTER_PAGE_LIMIT,
            },
        ).json()
        assert remainder["total"] == OWNER_FILTER_MATCH_TOTAL
        assert len(remainder["items"]) == 1
        walked = [row["id"] for row in data["items"] + remainder["items"]]
        assert walked == sorted(walked)
        assert len(set(walked)) == OWNER_FILTER_MATCH_TOTAL


class TestRetrievePeriodicTask:
    """Test the GET /periodic/{periodic_task_id} endpoint."""

    def test_retrieve_existing(self, periodic_test_client, created_periodic_task):
        """Assert retrieving an existing periodic task returns it."""
        response = periodic_test_client.get(f"/periodic/{created_periodic_task.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == created_periodic_task.id
        assert data["task"] == "my-backup-task"

    def test_retrieve_nonexistent_returns_404(self, periodic_test_client):
        """Assert retrieving a non-existent periodic task returns 404."""
        response = periodic_test_client.get("/periodic/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestPeriodicTaskLastRunStatus:
    """Test last-run-status population on the periodic-task list/retrieve routes."""

    @pytest_asyncio.fixture
    async def seeded_matrix(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed one periodic task per acceptance-criteria scenario."""
        ran_at = utc_now()

        # Never run: a system history row exists, but last_run_at is None.
        await _add_periodic_task(
            celery_beat_session,
            name="never-run",
            task_name="never-run-task",
            last_run_at=None,
        )
        await _add_history(
            tasks_session,
            task_name="never-run-task",
            task_status=TaskHistoryStatusEnum.SUCCESS,
        )

        await _add_periodic_task(
            celery_beat_session,
            name="succeeded",
            task_name="succeeded-task",
            last_run_at=ran_at,
        )
        await _add_history(
            tasks_session,
            task_name="succeeded-task",
            task_status=TaskHistoryStatusEnum.SUCCESS,
        )

        await _add_periodic_task(
            celery_beat_session,
            name="failed",
            task_name="failed-task",
            last_run_at=ran_at,
        )
        await _add_history(
            tasks_session,
            task_name="failed-task",
            task_status=TaskHistoryStatusEnum.FAILED,
        )

        await _add_periodic_task(
            celery_beat_session,
            name="running",
            task_name="running-task",
            last_run_at=ran_at,
        )
        await _add_history(
            tasks_session,
            task_name="running-task",
            task_status=TaskHistoryStatusEnum.RUNNING,
        )

        # Manual-only: history exists but was not system-executed.
        await _add_periodic_task(
            celery_beat_session,
            name="manual-only",
            task_name="manual-task",
            last_run_at=ran_at,
        )
        await _add_history(
            tasks_session,
            task_name="manual-task",
            task_status=TaskHistoryStatusEnum.SUCCESS,
            executed_by="test-user",
        )

        # No history at all, despite a recorded last_run_at.
        await _add_periodic_task(
            celery_beat_session,
            name="no-history",
            task_name="no-history-task",
            last_run_at=ran_at,
        )

    def test_list_reports_last_run_status(self, periodic_test_client, seeded_matrix):
        """Assert each scenario resolves to the expected last_run_status."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        statuses = {
            row["name"]: row["last_run_status"] for row in response.json()["items"]
        }

        assert statuses["never-run"] is None
        assert statuses["succeeded"] == "success"
        assert statuses["failed"] == "failed"
        assert statuses["running"] == "running"
        assert statuses["manual-only"] is None
        assert statuses["no-history"] is None

    @pytest_asyncio.fixture
    async def seeded_two_schedules(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed two schedules bound to the same task name with one system run."""
        ran_at = utc_now()
        await _add_periodic_task(
            celery_beat_session,
            name="schedule-one",
            task_name="shared-task",
            last_run_at=ran_at,
        )
        await _add_periodic_task(
            celery_beat_session,
            name="schedule-two",
            task_name="shared-task",
            last_run_at=ran_at,
        )
        await _add_history(
            tasks_session,
            task_name="shared-task",
            task_status=TaskHistoryStatusEnum.SUCCESS,
        )

    def test_two_schedules_same_task_agree(
        self, periodic_test_client, seeded_two_schedules
    ):
        """Assert two schedules on one task name report the same last_run_status."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        statuses = {
            row["name"]: row["last_run_status"] for row in response.json()["items"]
        }

        assert statuses["schedule-one"] == "success"
        assert statuses["schedule-two"] == "success"

    @pytest_asyncio.fixture
    async def seeded_staggered_schedules(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed two schedules on one task name dispatched minutes apart.

        Each schedule must resolve to the system run that followed its own
        dispatch, not the other schedule's run -- the shared name's query cutoff
        is the earlier dispatch, so both runs are in play for both schedules.
        """
        early = utc_now() - timedelta(minutes=10)
        late = utc_now()
        await _add_periodic_task(
            celery_beat_session,
            name="early-schedule",
            task_name="staggered-task",
            last_run_at=early,
        )
        await _add_periodic_task(
            celery_beat_session,
            name="late-schedule",
            task_name="staggered-task",
            last_run_at=late,
        )
        task = await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="staggered-task")),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.SUCCESS,
            created_at=early + timedelta(seconds=1),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.FAILED,
            created_at=late + timedelta(seconds=1),
        )

    def test_staggered_schedules_resolve_own_runs(
        self, periodic_test_client, seeded_staggered_schedules
    ):
        """Assert each schedule on a shared name resolves to its own dispatch."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        statuses = {
            row["name"]: row["last_run_status"] for row in response.json()["items"]
        }

        assert statuses["early-schedule"] == "success"
        assert statuses["late-schedule"] == "failed"

    @pytest_asyncio.fixture
    async def seeded_retrieve(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> PeriodicTask:
        """Seed a single run periodic task for the retrieve route."""
        task = await _add_periodic_task(
            celery_beat_session,
            name="retrieve-me",
            task_name="retrieve-task",
            last_run_at=utc_now(),
        )
        await _add_history(
            tasks_session,
            task_name="retrieve-task",
            task_status=TaskHistoryStatusEnum.SUCCESS,
        )
        return task

    def test_retrieve_reports_last_run_status(
        self, periodic_test_client, seeded_retrieve
    ):
        """Assert the retrieve route also carries last_run_status."""
        response = periodic_test_client.get(f"/periodic/{seeded_retrieve.id}")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["last_run_status"] == "success"

    def test_update_preserves_last_run_status(
        self, periodic_test_client, seeded_retrieve
    ):
        """Assert editing a previously-run schedule still reports its last result.

        Regression: the update route defaulted ``last_run_status`` to ``None``
        because it returned the raw ORM row without enrichment, making an edited
        schedule look never-run.
        """
        update_data = {
            "name": "retrieve-me",
            "task": "retrieve-task",
            "start_time": None,
            "enabled": False,
            "description": "edited",
            "interval": {"every": 30, "period": "minutes"},
        }
        response = periodic_test_client.put(
            f"/periodic/{seeded_retrieve.id}",
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["last_run_status"] == "success"

    @pytest_asyncio.fixture
    async def seeded_false_attribution(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed a schedule whose own run is older than an unrelated later run.

        The schedule dispatched at ``ran_at`` and succeeded; a separate later
        system run of the same task name (e.g. a chain child) then failed. The
        schedule must report its own success, not the later failure.
        """
        ran_at = utc_now()
        await _add_periodic_task(
            celery_beat_session,
            name="own-run",
            task_name="shared-run",
            last_run_at=ran_at,
        )
        task = await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="shared-run")),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.SUCCESS,
            created_at=ran_at + timedelta(seconds=1),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.FAILED,
            created_at=ran_at + timedelta(minutes=5),
        )

    def test_later_unrelated_run_not_attributed(
        self, periodic_test_client, seeded_false_attribution
    ):
        """Assert a later same-name system run does not clobber the schedule's result."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        row = next(row for row in response.json()["items"] if row["name"] == "own-run")
        assert row["last_run_status"] == "success"

    @pytest_asyncio.fixture
    async def seeded_prior_run(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed a schedule preceded by an older, unrelated same-name system run.

        A separate system run of the same task name failed before the schedule
        dispatched at ``ran_at``. The schedule must report its own later success,
        not the earlier failure that predates ``last_run_at``.
        """
        ran_at = utc_now()
        await _add_periodic_task(
            celery_beat_session,
            name="later-run",
            task_name="prior-run",
            last_run_at=ran_at,
        )
        task = await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="prior-run")),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.FAILED,
            created_at=ran_at - timedelta(minutes=5),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.SUCCESS,
            created_at=ran_at + timedelta(seconds=1),
        )

    def test_earlier_unrelated_run_not_attributed(
        self, periodic_test_client, seeded_prior_run
    ):
        """Assert a same-name system run before last_run_at is not attributed."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        row = next(
            row for row in response.json()["items"] if row["name"] == "later-run"
        )
        assert row["last_run_status"] == "success"

    @pytest_asyncio.fixture
    async def seeded_subsecond_dispatch(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed a schedule whose ``last_run_at`` carries sub-second precision.

        ``last_run_at`` keeps microseconds while ``TaskHistory.created_at`` is
        floored to whole seconds, so comparing at the finer granularity would
        place the schedule's own history row just before the dispatch and miss it.
        """
        base = utc_now()
        await _add_periodic_task(
            celery_beat_session,
            name="subsecond",
            task_name="subsecond-task",
            last_run_at=base + timedelta(microseconds=412000),
        )
        task = await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="subsecond-task")),
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.SUCCESS,
            created_at=base,
        )

    def test_subsecond_dispatch_reports_own_run(
        self, periodic_test_client, seeded_subsecond_dispatch
    ):
        """Assert a sub-second last_run_at still matches its whole-second row."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        row = next(
            row for row in response.json()["items"] if row["name"] == "subsecond"
        )
        assert row["last_run_status"] == "success"


class TestListPeriodicTasksByTaskName:
    """Test last-run-status population on GET /{task_name}/periodic/."""

    @pytest_asyncio.fixture
    async def seeded_by_task_name(
        self,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ) -> None:
        """Seed an executable task, a run schedule for it, and a system history row."""
        ran_at = utc_now()
        task = await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="by-name-task")),
        )
        await _add_periodic_task(
            celery_beat_session,
            name="by-name-schedule",
            task_name="by-name-task",
            last_run_at=ran_at,
        )
        await _add_history_row(
            tasks_session,
            task,
            task_status=TaskHistoryStatusEnum.SUCCESS,
            created_at=ran_at,
        )

    def test_list_by_task_name_reports_last_run_status(
        self, periodic_test_client, seeded_by_task_name
    ):
        """Assert the by-task-name list route also carries last_run_status."""
        response = periodic_test_client.get("/by-name-task/periodic/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["last_run_status"] == "success"


class TestUpdatePeriodicTask:
    """Test the PUT /periodic/{periodic_task_id} endpoint."""

    def test_update_periodic_task(self, periodic_test_client, created_periodic_task):
        """Assert updating a periodic task returns the updated task."""
        update_data = {
            "name": "updated-name",
            "task": "my-backup-task",
            "start_time": None,
            "enabled": False,
            "description": "Updated description",
            "interval": {"every": 30, "period": "minutes"},
        }
        response = periodic_test_client.put(
            f"/periodic/{created_periodic_task.id}",
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "updated-name"
        assert data["enabled"] is False
        assert data["description"] == "Updated description"

    @pytest.mark.asyncio
    async def test_update_with_changed_task_name_validates(
        self,
        periodic_test_client,
        created_periodic_task,
        tasks_session,
    ):
        """Assert updating task_name validates via get_executable_task_by_name."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="new-task-name")),
        )
        update_data = {
            "name": "updated-name",
            "task": "new-task-name",
            "start_time": None,
            "enabled": True,
            "description": "",
            "interval": {"every": 10, "period": "minutes"},
        }
        response = periodic_test_client.put(
            f"/periodic/{created_periodic_task.id}",
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["task"] == "new-task-name"

    @pytest.mark.asyncio
    async def test_update_periodic_task_with_chain_unchanged(
        self,
        periodic_test_client,
        celery_beat_session,
        tasks_session,
    ):
        """Assert updating only cron preserves existing chain and succeeds.

        This is a regression test for the bug where editing a periodic task
        with existing chained tasks would fail validation even if the chain
        was not being modified. The PUT request should include the execute_request
        with chain_task_names, and validation should not fail for unchanged chains.
        """
        shared = {"owner": "BACKUPS", "data": {"Constraints": [{"RTarget": "n"}]}}
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-a", **shared)),
        )
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-b", **shared)),
        )

        schedule = IntervalSchedule(every=10, period=Period.MINUTES)
        celery_beat_session.add(schedule)
        await celery_beat_session.flush()

        periodic_task = PeriodicTask(
            name="chained-periodic",
            task=CELERY_TASK_NAME,
            kwargs=json.dumps(
                {
                    "task_name": "task-a",
                    "execution_data": {
                        "chain_task_names": ["task-b"],
                        "meta": {},
                    },
                }
            ),
            enabled=True,
            description="A periodic task with chain",
            schedule_model=schedule,
        )
        celery_beat_session.add(periodic_task)
        await celery_beat_session.commit()
        await celery_beat_session.refresh(periodic_task)

        update_data = {
            "name": "chained-periodic",
            "task": "task-a",
            "start_time": None,
            "enabled": True,
            "description": "A periodic task with chain",
            "execute_request": {
                "chain_task_names": ["task-b"],
                "meta": {},
            },
            "crontab": {
                "minute": "0",
                "hour": "*/2",
                "day_of_month": "*",
                "month_of_year": "*",
                "day_of_week": "*",
                "timezone": "UTC",
            },
        }
        response = periodic_test_client.put(
            f"/periodic/{periodic_task.id}",
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Chain should be preserved
        assert data["execute_request"]["chain_task_names"] == ["task-b"]

    @pytest.mark.asyncio
    async def test_update_periodic_task_without_execute_request_preserves_chain(
        self,
        periodic_test_client,
        celery_beat_session,
        tasks_session,
    ):
        """Assert updating without execute_request in body preserves existing chain.

        This is a regression test for the bug where kwargs reconstruction
        did not update the kwargs field, causing execution_data to be dropped
        from the persisted row when execute_request was omitted from the PUT body.
        """
        shared = {"owner": "BACKUPS", "data": {"Constraints": [{"RTarget": "n"}]}}
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-a", **shared)),
        )
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-b", **shared)),
        )

        schedule = IntervalSchedule(every=10, period=Period.MINUTES)
        celery_beat_session.add(schedule)
        await celery_beat_session.flush()

        periodic_task = PeriodicTask(
            name="chained-periodic",
            task=CELERY_TASK_NAME,
            kwargs=json.dumps(
                {
                    "task_name": "task-a",
                    "execution_data": {
                        "chain_task_names": ["task-b"],
                        "meta": {},
                    },
                }
            ),
            enabled=True,
            description="A periodic task with chain",
            schedule_model=schedule,
        )
        celery_beat_session.add(periodic_task)
        await celery_beat_session.commit()
        await celery_beat_session.refresh(periodic_task)

        update_data = {
            "name": "chained-periodic",
            "task": "task-a",
            "start_time": None,
            "enabled": True,
            "description": "Updated description",
            "crontab": {
                "minute": "0",
                "hour": "*/4",
                "day_of_month": "*",
                "month_of_year": "*",
                "day_of_week": "*",
                "timezone": "UTC",
            },
        }
        response = periodic_test_client.put(
            f"/periodic/{periodic_task.id}",
            json=update_data,
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["execute_request"]["chain_task_names"] == ["task-b"]
        assert data["description"] == "Updated description"


class TestCreatePeriodicTaskChainValidation:
    """Test chain_task_names validation on POST /{task_name}/periodic/."""

    @pytest.mark.asyncio
    async def test_self_chain_returns_400(self, periodic_test_client, tasks_session):
        """Assert creating a periodic task that chains to itself returns 400."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="my-task")),
        )
        payload = {
            "interval": {"every": 10, "period": "minutes"},
            "execute_request": {"chain_task_names": ["my-task"]},
        }
        response = periodic_test_client.post("/my-task/periodic/", json=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Cycle detected" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_nonexistent_chain_task_returns_404(
        self, periodic_test_client, tasks_session
    ):
        """Assert creating a periodic task with a nonexistent chain task returns 404."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="my-task")),
        )
        payload = {
            "interval": {"every": 10, "period": "minutes"},
            "execute_request": {"chain_task_names": ["does-not-exist"]},
        }
        response = periodic_test_client.post("/my-task/periodic/", json=payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_valid_chain_task_succeeds(self, periodic_test_client, tasks_session):
        """Assert creating a periodic task with a valid chain task succeeds."""
        shared = {"owner": "BACKUPS", "data": {"Constraints": [{"RTarget": "n"}]}}
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-a", **shared)),
        )
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="task-b", **shared)),
        )
        payload = {
            "interval": {"every": 10, "period": "minutes"},
            "execute_request": {"chain_task_names": ["task-b"]},
        }
        response = periodic_test_client.post("/task-a/periodic/", json=payload)
        assert response.status_code == status.HTTP_201_CREATED


class TestDuplicateUnnamedPeriodicTaskAttach:
    """Test that a repeat unnamed create request is rejected, not duplicated."""

    @pytest.mark.asyncio
    async def test_second_unnamed_request_after_restart_gets_409(
        self,
        periodic_test_client,
        celery_beat_session: AsyncSession,
        tasks_session: AsyncSession,
    ):
        """Assert a second process's identical unnamed request collides.

        A row seeded with the literal name this payload generates stands in for
        a prior process (a worker restart, a deploy, or a second operator)
        having already attached the same schedule.
        """
        await TaskManager.create(
            tasks_session, TaskWrite.model_validate(TaskFactory.build(name="my-task"))
        )
        payload = {"interval": {"every": 10, "period": "minutes"}}
        expected_name = "run_my-task_every_10_minutes_01263f4315fc8f0f"
        await _add_periodic_task(
            celery_beat_session, name=expected_name, task_name="my-task"
        )

        response = periodic_test_client.post("/my-task/periodic/", json=payload)

        assert response.status_code == status.HTTP_409_CONFLICT
        rows = await celery_beat_session.exec(
            select(PeriodicTask).where(PeriodicTask.name == expected_name)
        )
        assert len(rows.all()) == 1


class TestDeletePeriodicTask:
    """Test the DELETE /periodic/{periodic_task_id} endpoint."""

    def test_delete_existing(self, periodic_test_client, created_periodic_task):
        """Assert deleting an existing periodic task returns 204."""
        response = periodic_test_client.delete(f"/periodic/{created_periodic_task.id}")
        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_nonexistent_returns_404(self, periodic_test_client):
        """Assert deleting a non-existent periodic task returns 404."""
        response = periodic_test_client.delete("/periodic/99999")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestScheduleFieldsOnReadRoutes:
    """Cover timezone and next_runs on every route returning a schedule."""

    def test_retrieve_carries_the_schedule_fields(
        self, periodic_test_client, created_periodic_task
    ):
        """Assert the detail route reports the zone and the upcoming runs."""
        response = periodic_test_client.get(f"/periodic/{created_periodic_task.id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["timezone"] == INTERVAL_TIMEZONE
        assert len(data["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT
        assert data["next_run_at"] == data["next_runs"][0]

    def test_list_carries_the_schedule_fields(
        self, periodic_test_client, created_periodic_task
    ):
        """Assert every row of the paginated list reports both fields."""
        response = periodic_test_client.get("/periodic/")
        assert response.status_code == status.HTTP_200_OK
        items = response.json()["items"]
        assert items
        for item in items:
            assert item["timezone"] == INTERVAL_TIMEZONE
            assert len(item["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT

    @pytest.mark.asyncio
    async def test_list_by_task_name_carries_the_schedule_fields(
        self, periodic_test_client, celery_beat_session, tasks_session
    ):
        """Assert the by-task-name route reports both fields."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="scheduled-task")),
        )
        await _add_periodic_task(
            celery_beat_session, name="nightly", task_name="scheduled-task"
        )
        response = periodic_test_client.get("/scheduled-task/periodic/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data
        assert data[0]["timezone"] == INTERVAL_TIMEZONE
        assert len(data[0]["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT


class TestCronValidationAtTheRequestBoundary:
    """Cover the 422 a cron the scheduler cannot run earns on every write path."""

    @pytest.mark.asyncio
    async def test_create_rejects_an_unrunnable_cron(
        self, periodic_test_client, tasks_session
    ):
        """Assert create answers 422 naming the offending field."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="my-task")),
        )
        payload = {"crontab": {"minute": "not-a-cron", "hour": "2"}}
        response = periodic_test_client.post("/my-task/periodic/", json=payload)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert any(
            error["loc"][-2:] == ["crontab", "minute"]
            for error in response.json()["detail"]
        )

    @pytest.mark.asyncio
    async def test_create_accepts_a_runnable_cron(
        self, periodic_test_client, tasks_session
    ):
        """Assert a cron the scheduler can run is still created."""
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="my-task")),
        )
        payload = {"crontab": {"minute": "0", "hour": "2"}}
        response = periodic_test_client.post("/my-task/periodic/", json=payload)
        assert response.status_code == status.HTTP_201_CREATED

    def test_update_rejects_an_unrunnable_cron(
        self, periodic_test_client, created_periodic_task
    ):
        """Assert update answers 422 rather than failing at flush time."""
        payload = {
            "name": "updated-name",
            "task": "my-backup-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "crontab": {"minute": "not-a-cron", "hour": "2"},
        }
        response = periodic_test_client.put(
            f"/periodic/{created_periodic_task.id}", json=payload
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_update_accepts_a_runnable_cron(
        self, periodic_test_client, created_periodic_task
    ):
        """Assert a cron the scheduler can run is still accepted on update."""
        payload = {
            "name": "updated-name",
            "task": "my-backup-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "crontab": {"minute": "0", "hour": "2"},
        }
        response = periodic_test_client.put(
            f"/periodic/{created_periodic_task.id}", json=payload
        )
        assert response.status_code == status.HTTP_200_OK

    def test_update_rejects_a_parseable_but_unsatisfiable_cron(
        self, periodic_test_client, created_periodic_task
    ):
        """Assert 30 February is refused at the boundary, not at serialisation.

        The expression parses; only ``remaining_estimate`` discovers it can never
        fire, and it signals that with ``RuntimeError`` rather than ``ValueError``.
        """
        payload = {
            "name": "updated-name",
            "task": "my-backup-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "crontab": {
                "minute": "0",
                "hour": "2",
                "day_of_month": "30",
                "month_of_year": "2",
            },
        }
        response = periodic_test_client.put(
            f"/periodic/{created_periodic_task.id}", json=payload
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestPreviewSchedule:
    """Test the POST /periodic/schedule/preview/ endpoint."""

    def test_preview_interval_reports_zone_and_runs(self, periodic_test_client):
        """Assert an interval body previews three UTC runs in UTC."""
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"interval": {"every": 30, "period": "minutes"}},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["timezone"] == INTERVAL_TIMEZONE
        assert len(data["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT
        assert data["next_run_at"] == data["next_runs"][0]

    def test_preview_crontab_reports_its_own_zone(self, periodic_test_client):
        """Assert a crontab body previews in the zone it declares."""
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"crontab": {"minute": "0", "hour": "2", "timezone": "Europe/Lisbon"}},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["timezone"] == "Europe/Lisbon"
        assert len(data["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT

    def test_preview_honours_a_future_start_time(self, periodic_test_client):
        """Assert the preview reports no run before a future start_time."""
        start = utc_now() + timedelta(days=7)
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={
                "interval": {"every": 30, "period": "minutes"},
                "start_time": start.isoformat(),
            },
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["next_run_at"] == start.isoformat().replace(
            "+00:00", "Z"
        )

    def test_preview_rejects_an_unrunnable_cron(self, periodic_test_client):
        """Assert the preview route answers 422 like the write routes do."""
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"crontab": {"minute": "not-a-cron", "hour": "2"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_preview_rejects_both_schedule_kinds(self, periodic_test_client):
        """Assert naming both an interval and a crontab is refused."""
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={
                "interval": {"every": 30, "period": "minutes"},
                "crontab": {"minute": "0", "hour": "2"},
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_preview_rejects_neither_schedule_kind(self, periodic_test_client):
        """Assert naming no schedule at all is refused."""
        response = periodic_test_client.post("/periodic/schedule/preview/", json={})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_preview_rejects_a_period_create_would_refuse(self, periodic_test_client):
        """Assert an interval previewable here is one create would also accept."""
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"interval": {"every": 5, "period": "seconds"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize(
        "every",
        [OVERFLOWS_RUN_DATETIME, OVERFLOWS_CADENCE_TIMEDELTA],
        ids=["overflows-the-run-datetime", "overflows-the-cadence-timedelta"],
    )
    def test_preview_rejects_an_unschedulable_interval(
        self, periodic_test_client, every
    ):
        """Assert an interval that cannot produce runs earns a 422, not a 500.

        ``every`` is otherwise unbounded, and the overflow would surface from a
        computed field during serialisation. The two values fail at different
        points: one builds a cadence but lands a run past
        ``datetime.max``, the other cannot build the cadence at all.
        """
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"interval": {"every": every, "period": "days"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.parametrize(
        "schedule",
        [
            {"interval": {"every": 1, "period": "hours"}},
            {"crontab": {"minute": "0", "hour": "*"}},
        ],
        ids=["interval", "crontab"],
    )
    def test_preview_rejects_a_start_time_that_overflows_later_runs(
        self, periodic_test_client, schedule
    ):
        """Assert a near-``datetime.max`` start_time earns a 422, not a 500.

        The cadence is representable and the first run is fine; the runs after it
        fall off the end of the calendar, so the anchor has to be checked
        together with the schedule rather than either alone.
        """
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={**schedule, "start_time": OVERFLOWS_FROM_START_TIME},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_create_rejects_an_unschedulable_interval(
        self, periodic_test_client, tasks_session
    ):
        """Assert the write path refuses what a read could not then serialise.

        A stored row of this shape would 500 the whole list endpoint, not just
        its own detail response.
        """
        await TaskManager.create(
            tasks_session,
            TaskWrite.model_validate(TaskFactory.build(name="my-task")),
        )
        response = periodic_test_client.post(
            "/my-task/periodic/",
            json={"interval": {"every": OVERFLOWS_RUN_DATETIME, "period": "days"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_preview_persists_nothing(
        self, periodic_test_client, celery_beat_session
    ):
        """Assert previewing leaves the beat store untouched."""
        before = len(
            (await celery_beat_session.execute(select(PeriodicTask))).scalars().all()
        )
        response = periodic_test_client.post(
            "/periodic/schedule/preview/",
            json={"interval": {"every": 30, "period": "minutes"}},
        )
        assert response.status_code == status.HTTP_200_OK
        after = len(
            (await celery_beat_session.execute(select(PeriodicTask))).scalars().all()
        )
        assert after == before
