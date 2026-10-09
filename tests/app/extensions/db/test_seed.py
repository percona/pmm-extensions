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

"""Cover PMM Extensions database seeding and system periodic-task contributions."""

import json
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy_celery_beat import IntervalSchedule
from sqlalchemy_celery_beat.models import Period, PeriodicTask
from sqlmodel import SQLModel

from app.core.celery.crud import BasePeriodicTaskManager
from app.core.celery.models import IntervalSchedule as CoreIntervalSchedule
from app.core.celery.utils import SystemPeriodicTaskData, SystemPeriodicTaskSchedule
from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.extensions import periodic_tasks as periodic_tasks_module
from app.extensions.apps.framework.base import AppPeriodicTask, BaseApp
from app.extensions.apps.framework.registry import get_app_registry
from app.extensions.apps.inventory.config import inventory_app_settings
from app.extensions.apps.mysql_backups.restore.models import OWNER as RESTORES_OWNER
from app.extensions.config import App
from app.extensions.crud import AppStateManager, ExtensionsAppPeriodicTaskManager
from app.extensions.db import seed as seed_module
from app.extensions.models import AppLifecycleEnum, AppState, ExtensionsAppPeriodicTask
from app.tasks.models import INVENTORY_COLLECTION_TASK_NAME
from tests.app.db_schema import apply_schema
from tests.app.factories import TaskFactory

SNIPPETS_TASK = "extensions__sync_snippets"
ALERTS_TASK = "extensions__backup_alert_config"
COLLECTION_TASK = "extensions__inventory_collection"
ONE_DAY = CoreIntervalSchedule(every=1, period=Period.DAYS)
SIX_HOURS = CoreIntervalSchedule(every=6, period=Period.HOURS)
REPORT_PURGE_TASK = "extensions__purge_report_artifacts"
ATW_PURGE_TASK = "extensions__purge_atw_bundles"
CELERY_RESULT_EXPIRES_SECONDS = 3600


def _plugin(key: str, *, enabled: bool = True) -> App:
    """Build an ``App`` activation entry for ``key``."""
    return App(module_name=key, enabled=enabled)


def _registry_app(key: str, specs: list[AppPeriodicTask]) -> BaseApp:
    """Build a registry entry contributing ``specs``."""
    return BaseApp(key=key, name=key, uri_path=f"/{key}", periodic_task_schedules=specs)


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> Iterator[None]:
    """Rebuild the registry from each test's patched ``APPS``."""
    get_app_registry.cache_clear()
    yield
    get_app_registry.cache_clear()


@pytest_asyncio.fixture(name="seed_maker")
async def seed_maker_fixture() -> AsyncIterator:
    """Provide a session maker bound to an in-memory SQLite DB with all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    try:
        yield get_async_session_maker_from_engine(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def patched_seed(mocker, seed_maker):
    """Patch the seed module's session maker and stub the periodic-task work.

    The celery-beat task seeding (``init_periodic_tasks_db``), the cross-database
    gating (``sync_app_periodic_task_gating``) and the unschedulable-owner sweep
    (``disable_unschedulable_task_schedules``) are stubbed so the AppState-only
    tests never reach a real scheduler or tasks database.
    """
    mocker.patch.object(seed_module, "get_async_session_maker", return_value=seed_maker)
    mocker.patch.object(
        seed_module, "sync_app_periodic_task_gating", new_callable=mocker.AsyncMock
    )
    mocker.patch.object(
        seed_module,
        "disable_unschedulable_task_schedules",
        new_callable=mocker.AsyncMock,
    )
    return mocker.patch.object(
        seed_module, "init_periodic_tasks_db", new_callable=mocker.AsyncMock
    )


@pytest.mark.asyncio
class TestInitExtensionsDbAppStateSeeding:
    """Tests for the AppState portion of ``init_extensions_db``."""

    async def test_first_run_inserts_rows_with_yaml_enabled(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """Each non-protected plugin yields a row with its YAML ``enabled`` value."""
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [
                _plugin("snippets", enabled=True),
                _plugin("checksums", enabled=False),
                _plugin("inventory", enabled=True),
            ],
        )

        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            lifecycle_states = await AppStateManager.all_lifecycle_states(session)
        assert lifecycle_states == {
            "snippets": AppLifecycleEnum.ENABLED,
            "checksums": AppLifecycleEnum.DISABLED,
        }

    async def test_inventory_is_never_seeded(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """The protected ``inventory`` app gets no row even when configured."""
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("inventory")]
        )

        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            states = await AppStateManager.all_lifecycle_states(session)
        assert "inventory" not in states

    async def test_idempotent_second_run(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """A second seed with the same configured set inserts no extra rows."""
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("snippets", enabled=True)]
        )

        await seed_module.init_extensions_db()
        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            states = await AppStateManager.all_lifecycle_states(session)
        assert states == {"snippets": AppLifecycleEnum.ENABLED}

    async def test_existing_row_not_overwritten(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """An existing row keeps its value even when the YAML flips ``enabled``."""
        async with seed_maker() as session:
            session.add(
                AppState(app_key="snippets", lifecycle_state=AppLifecycleEnum.DISABLED)
            )
            await session.commit()

        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("snippets", enabled=True)]
        )
        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            assert await AppStateManager.is_enabled(session, "snippets") is False

    async def test_orphan_rows_deleted(self, mocker, patched_seed, seed_maker) -> None:
        """Rows for apps no longer configured (incl. now-protected) are removed."""
        async with seed_maker() as session:
            session.add(
                AppState(app_key="ghost", lifecycle_state=AppLifecycleEnum.ENABLED)
            )
            session.add(
                AppState(app_key="inventory", lifecycle_state=AppLifecycleEnum.ENABLED)
            )
            await session.commit()

        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("snippets", enabled=True)]
        )
        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            states = await AppStateManager.all_lifecycle_states(session)
        assert states == {"snippets": AppLifecycleEnum.ENABLED}

    async def test_child_app_not_seeded_and_stale_child_row_orphaned(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """Seed no row for a child app and orphan-delete a pre-existing child row."""
        async with seed_maker() as session:
            session.add(
                AppState(
                    app_key="mysql_backups/restore",
                    lifecycle_state=AppLifecycleEnum.ENABLED,
                )
            )
            await session.commit()

        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("mysql_backups")]
        )
        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            states = await AppStateManager.all_lifecycle_states(session)
        assert states == {"mysql_backups": AppLifecycleEnum.ENABLED}

    async def test_periodic_task_seeding_still_runs(
        self, mocker, patched_seed, seed_maker
    ) -> None:
        """Periodic-task seeding still fires after AppState seeding (no regression)."""
        mocker.patch.object(seed_module.extensions_settings, "APPS", [])

        await seed_module.init_extensions_db()

        patched_seed.assert_awaited_once()


def test_reconciler_seeded_as_ungated_system_task() -> None:
    """The drain reconciler is seeded with no owner, so it is never gated off."""
    reconcilers = [
        task
        for schedule in seed_module.get_system_periodic_tasks()
        for task in schedule.tasks
        if task.task_name == "app.extensions.app_drain.reconcile_disabling_apps"
    ]
    assert len(reconcilers) == 1
    assert reconcilers[0].owner_app_key is None


def _snippets_schedule(
    tasks: list[SystemPeriodicTaskSchedule],
) -> SystemPeriodicTaskSchedule:
    """Return the schedule carrying the ``extensions__sync_snippets`` task."""
    return next(
        schedule
        for schedule in tasks
        if any(task.name == SNIPPETS_TASK for task in schedule.tasks)
    )


def test_builder_reads_sync_interval_at_call_time() -> None:
    """``get_system_periodic_tasks`` reflects the live ``SYNC_INTERVAL`` override.

    Built per call, so a DB-backed override published to the proxy snapshot is
    honored without a restart.
    """
    from app.core.celery.models import (  # noqa: PLC0415
        IntervalSchedule as CoreIntervalSchedule,
    )
    from app.extensions.snippets.config import snippets_settings  # noqa: PLC0415

    snippets_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"SYNC_INTERVAL": CoreIntervalSchedule(every=30, period=Period.MINUTES)}
    )
    try:
        schedule = _snippets_schedule(seed_module.get_system_periodic_tasks())
        assert schedule.schedule == CoreIntervalSchedule(
            every=30, period=Period.MINUTES
        )
    finally:
        snippets_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    # A different override on the next call is reflected (no import-time freeze).
    snippets_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"SYNC_INTERVAL": CoreIntervalSchedule(every=5, period=Period.MINUTES)}
    )
    try:
        schedule = _snippets_schedule(seed_module.get_system_periodic_tasks())
        assert schedule.schedule == CoreIntervalSchedule(every=5, period=Period.MINUTES)
    finally:
        snippets_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


class TestInventoryCollectionSchedule:
    """Cover the tombstone-collection beat entry the Inventory app owns."""

    @staticmethod
    def _entries(
        tasks: list[SystemPeriodicTaskSchedule],
    ) -> list[SystemPeriodicTaskSchedule]:
        """Return the schedules carrying the collection task."""
        return [
            schedule
            for schedule in tasks
            for task in schedule.tasks
            if task.name == COLLECTION_TASK
        ]

    def test_no_entry_while_the_interval_is_unset(self) -> None:
        """Seed no schedule on the shipped default, so nothing is ever deleted."""
        inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"COLLECTION_INTERVAL": None}
        )
        try:
            assert self._entries(seed_module.get_system_periodic_tasks()) == []
        finally:
            inventory_app_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    def test_builds_the_execute_by_name_entry(self) -> None:
        """Point the entry at the PMM Extensions task through ``execute_task_by_name``.

        ``inventory-collection`` is a ``Task`` row rather than a Celery
        function, so only this indirection puts it in the plugin task list. The
        task path must arrive verbatim: the Inventory app ships no ``celery.py``
        to prefix it with, and prefixing would point beat at nothing.
        """
        inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"COLLECTION_INTERVAL": ONE_DAY}
        )
        try:
            (schedule,) = self._entries(seed_module.get_system_periodic_tasks())
        finally:
            inventory_app_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

        assert schedule.schedule == ONE_DAY
        (entry,) = schedule.tasks
        assert entry.task_name == "app.tasks.celery.execute_task_by_name"
        assert entry.extra_kwargs is not None
        kwargs = json.loads(entry.extra_kwargs["kwargs"])
        assert kwargs["task_name"] == INVENTORY_COLLECTION_TASK_NAME
        assert kwargs["periodic_task_name"] == COLLECTION_TASK
        assert "execution_data" not in kwargs

    def test_the_entry_is_owned_so_app_state_gates_it(self) -> None:
        """Stamp ``owner_app_key`` so disabling the app stops collection.

        The callable lives in the app package the embedded image strips, so a
        schedule that outlived its app would fire and fail on every tick.
        """
        inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"COLLECTION_INTERVAL": ONE_DAY}
        )
        try:
            (schedule,) = self._entries(seed_module.get_system_periodic_tasks())
        finally:
            inventory_app_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

        (entry,) = schedule.tasks
        assert entry.owner_app_key == "inventory"

    def test_the_interval_is_read_at_call_time(self) -> None:
        """Reflect a live override without a restart, and drop the entry when cleared.

        The whole reason the schedule is built here rather than by the tasks
        seeder: ``get_system_periodic_tasks`` re-reads the proxy snapshot on
        every call, so the override refresh callback re-seeds beat in place.
        """
        inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"COLLECTION_INTERVAL": ONE_DAY}
        )
        try:
            (schedule,) = self._entries(seed_module.get_system_periodic_tasks())
            assert schedule.schedule == ONE_DAY

            inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
                {"COLLECTION_INTERVAL": SIX_HOURS}
            )
            (schedule,) = self._entries(seed_module.get_system_periodic_tasks())
            assert schedule.schedule == SIX_HOURS

            inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
                {"COLLECTION_INTERVAL": None}
            )
            assert self._entries(seed_module.get_system_periodic_tasks()) == []
        finally:
            inventory_app_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    def test_repeated_calls_do_not_accumulate_the_entry(self) -> None:
        """Build a fresh set per call, so repeated boots do not duplicate it."""
        inventory_app_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"COLLECTION_INTERVAL": ONE_DAY}
        )
        try:
            first = seed_module.get_system_periodic_tasks()
            second = seed_module.get_system_periodic_tasks()
        finally:
            inventory_app_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

        assert len(self._entries(first)) == 1
        assert len(self._entries(second)) == 1
        assert len(first) == len(second)


class TestAppOwnedScheduleGating:
    """Cover omission of an app-owned schedule when its Celery module is absent."""

    @staticmethod
    def _task_names(tasks: list[SystemPeriodicTaskSchedule]) -> set[str]:
        """Return the ``name`` of every task across ``tasks``."""
        return {task.name for schedule in tasks for task in schedule.tasks}

    def test_absent_app_yields_no_schedule(self, mocker) -> None:
        """Omit an app's schedule when it contributes no Celery module.

        Interpolating an absent module path once produced a beat ``task_name``
        like ``None.backup_alert_config`` pointing at nothing the worker
        registers. The guard still covers alerts, report and atw; snippet sync is
        exempt because its task is statically included, not app-derived.
        """
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("inventory")]
        )

        tasks = seed_module.get_system_periodic_tasks()

        prefixes = [task.task_name for schedule in tasks for task in schedule.tasks]
        assert not any(name.startswith("None.") for name in prefixes)
        assert ALERTS_TASK not in self._task_names(tasks)

    def test_celery_opt_out_yields_no_schedule(self, mocker) -> None:
        """Omit the alerts schedule when alerts opts out of the Celery include."""
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [App(module_name="alerts", celery_module_path=None)],
        )

        tasks = seed_module.get_system_periodic_tasks()

        assert ALERTS_TASK not in self._task_names(tasks)

    def test_snippet_sync_is_seeded_without_the_app(self, mocker) -> None:
        """Seed the snippet-sync schedule against the static path, unowned.

        Ingestion is library-owned, so it must survive an activation list that
        never mounts the snippets app, and must carry no ``owner_app_key`` -- an
        owner would let an operator disable gate it off.
        """
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("inventory")]
        )

        tasks = seed_module.get_system_periodic_tasks()

        snippet_task = next(
            task
            for schedule in tasks
            for task in schedule.tasks
            if task.name == SNIPPETS_TASK
        )
        assert snippet_task.task_name == "app.extensions.snippets.celery.sync_snippets"
        assert snippet_task.owner_app_key is None


class TestAppScheduleContribution:
    """Cover per-app ``periodic_task_schedules`` specs folded by seed."""

    @staticmethod
    def _tasks_by_name(
        schedules: list[SystemPeriodicTaskSchedule],
    ) -> dict[str, SystemPeriodicTaskData]:
        """Index every task in ``schedules`` by ``name``."""
        return {task.name: task for schedule in schedules for task in schedule.tasks}

    @staticmethod
    def _schedule_for(
        schedules: list[SystemPeriodicTaskSchedule], task_name: str
    ) -> SystemPeriodicTaskSchedule:
        """Return the schedule carrying ``task_name``."""
        return next(
            schedule
            for schedule in schedules
            if any(task.name == task_name for task in schedule.tasks)
        )

    def test_a_qualified_spec_keeps_its_task_path(self, mocker) -> None:
        """Emit a ``qualified`` spec verbatim, even with no Celery module to prefix.

        The unqualified guard skips a whole app that ships no ``celery.py``,
        which is right for a spec naming a bare attribute on that module and
        wrong for one already naming a complete path.
        """
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("inventory")]
        )
        spec = AppPeriodicTask(
            name="extensions__qualified_probe",
            task="app.tasks.celery.execute_task_by_name",
            schedule=lambda: ONE_DAY,
            qualified=True,
        )
        mocker.patch.object(
            seed_module,
            "get_app_registry",
            return_value=[_registry_app("inventory", [spec])],
        )

        tasks = self._tasks_by_name(seed_module.get_system_periodic_tasks())

        assert tasks["extensions__qualified_probe"].task_name == (
            "app.tasks.celery.execute_task_by_name"
        )
        assert tasks["extensions__qualified_probe"].owner_app_key == "inventory"

    def test_a_none_schedule_contributes_nothing(self, mocker) -> None:
        """Drop a spec whose thunk returns ``None`` rather than seeding it.

        A nullable interval setting spells "do not run" that way, and the
        per-spec skip must not take the app's other specs down with it.
        """
        off = AppPeriodicTask(
            name="extensions__off_probe",
            task="app.tasks.celery.execute_task_by_name",
            schedule=lambda: None,
            qualified=True,
        )
        on = AppPeriodicTask(
            name="extensions__on_probe",
            task="app.tasks.celery.execute_task_by_name",
            schedule=lambda: ONE_DAY,
            qualified=True,
        )
        mocker.patch.object(
            seed_module,
            "get_app_registry",
            return_value=[_registry_app("inventory", [off, on])],
        )

        tasks = self._tasks_by_name(seed_module.get_system_periodic_tasks())

        assert "extensions__off_probe" not in tasks
        assert "extensions__on_probe" in tasks

    def test_contributing_apps_set_owner_and_celery_prefix(self, mocker) -> None:
        """Assert owner keys and Celery task-name prefixes on contributed schedules."""
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [_plugin("alerts"), _plugin("report"), _plugin("atw")],
        )

        tasks = self._tasks_by_name(seed_module.get_system_periodic_tasks())

        assert tasks[ALERTS_TASK].owner_app_key == "alerts"
        assert tasks[ALERTS_TASK].task_name == (
            "app.extensions.apps.alerts.celery.backup_alert_config"
        )
        assert tasks[REPORT_PURGE_TASK].owner_app_key == "report"
        assert tasks[REPORT_PURGE_TASK].task_name == (
            "app.extensions.apps.report.celery.purge_report_artifacts"
        )
        assert tasks[ATW_PURGE_TASK].owner_app_key == "atw"
        assert tasks[ATW_PURGE_TASK].task_name == (
            "app.extensions.apps.atw.celery.purge_atw_bundles"
        )

    def test_non_contributing_app_adds_no_owned_schedule(self, mocker) -> None:
        """Contribute only the unowned schedules when no app declares a factory."""
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("inventory")]
        )

        schedules = seed_module.get_system_periodic_tasks()
        tasks = [task for schedule in schedules for task in schedule.tasks]

        assert {task.name for task in tasks} == {
            "extensions__reconcile_disabling_apps",
            SNIPPETS_TASK,
        }
        assert all(task.owner_app_key is None for task in tasks)

    def test_builder_reads_backup_interval_at_call_time(self, mocker) -> None:
        """Reflect a live ``BACKUP_INTERVAL`` override on each builder call."""
        from app.core.celery.models import (  # noqa: PLC0415
            IntervalSchedule as CoreIntervalSchedule,
        )
        from app.extensions.apps.alerts.config import alerts_settings  # noqa: PLC0415

        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("alerts")]
        )

        alerts_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"BACKUP_INTERVAL": CoreIntervalSchedule(every=6, period=Period.HOURS)}
        )
        try:
            schedule = self._schedule_for(
                seed_module.get_system_periodic_tasks(), ALERTS_TASK
            )
            assert schedule.schedule == CoreIntervalSchedule(
                every=6, period=Period.HOURS
            )
        finally:
            alerts_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

        alerts_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {"BACKUP_INTERVAL": CoreIntervalSchedule(every=12, period=Period.HOURS)}
        )
        try:
            schedule = self._schedule_for(
                seed_module.get_system_periodic_tasks(), ALERTS_TASK
            )
            assert schedule.schedule == CoreIntervalSchedule(
                every=12, period=Period.HOURS
            )
        finally:
            alerts_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    def test_report_kwargs_assemble_from_non_default_entry(self, mocker) -> None:
        """Carry kwargs only for non-default report schedule-entry fields."""
        from app.core.celery.models import (  # noqa: PLC0415
            IntervalSchedule as CoreIntervalSchedule,
        )
        from app.extensions.apps.report.config import (  # noqa: PLC0415
            health_report_settings,
            ReportScheduleEntry,
        )

        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("report")]
        )
        mocker.patch.object(
            health_report_settings,
            "schedules",
            [
                ReportScheduleEntry(
                    schedule=CoreIntervalSchedule(every=7, period=Period.DAYS),
                    since="now-10d",
                    full=False,
                    upload=True,
                )
            ],
        )

        tasks = self._tasks_by_name(seed_module.get_system_periodic_tasks())
        generation = tasks["extensions__generate_health_report"]

        assert generation.owner_app_key == "report"
        assert generation.task_name == (
            "app.extensions.apps.report.celery.generate_health_report"
        )
        assert generation.extra_kwargs == {
            "kwargs": '{"since": "now-10d", "full": false, "upload": true}'
        }
        assert REPORT_PURGE_TASK in tasks

    def test_registry_entry_exposes_schedule_specs(self, mocker) -> None:
        """Carry app-owned schedule specs on a contributing registry entry."""
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("alerts")]
        )

        app = get_app_registry().get("alerts")
        assert app is not None
        assert app.periodic_task_schedules is not None
        schedules = app.periodic_task_schedules
        contributed = schedules if isinstance(schedules, list) else schedules()
        assert ALERTS_TASK in {spec.name for spec in contributed}


def test_celery_result_expires_configured() -> None:
    """Celery results have a TTL so result backends do not grow forever."""
    assert settings.CELERY.result_expires == CELERY_RESULT_EXPIRES_SECONDS


@pytest.mark.asyncio
class TestInitExtensionsDbPeriodicTaskGating:
    """Tests for the periodic-task gating wired into ``init_extensions_db``."""

    @staticmethod
    async def _seed_beat_row(
        beat_maker, name: str, task_name: str, *, enabled: bool = True
    ) -> None:
        """Insert a beat row so the gate has something to write through."""
        async with beat_maker() as session:
            schedule = IntervalSchedule(every=10, period=Period.MINUTES)
            session.add(schedule)
            await session.flush()
            session.add(
                PeriodicTask(
                    name=name,
                    task=task_name,
                    enabled=enabled,
                    schedule_model=schedule,
                )
            )
            await session.commit()

    @staticmethod
    def _patch_gate_session_makers(mocker, seed_maker, beat_maker) -> None:
        """Point the seed and gating modules at the in-memory session makers."""
        mocker.patch.object(
            seed_module, "get_async_session_maker", return_value=seed_maker
        )
        mocker.patch.object(
            seed_module, "init_periodic_tasks_db", new_callable=mocker.AsyncMock
        )
        mocker.patch.object(
            periodic_tasks_module,
            "get_extensions_session_maker",
            return_value=seed_maker,
        )
        mocker.patch.object(
            periodic_tasks_module,
            "get_celery_beat_session_maker",
            return_value=beat_maker,
        )
        mocker.patch.object(
            periodic_tasks_module, "get_tasks_session_maker", return_value=seed_maker
        )

    async def test_startup_survives_an_unreadable_tasks_database(
        self, mocker, seed_maker, beat_maker, caplog
    ) -> None:
        """Boot, and say so, when the sweep cannot reach the tasks database.

        The sweep is the only step here that reads the tasks database, and a
        deployment whose ``extensions`` track has migrated ahead of its ``tasks`` track
        reaches it before the table exists. Asserting the log line as well as the
        survival is deliberate: a test that only checks ``init_extensions_db`` returned
        passes equally against a silent ``except``.
        """
        self._patch_gate_session_makers(mocker, seed_maker, beat_maker)
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("mysql_backups")]
        )
        mocker.patch.object(
            seed_module,
            "disable_unschedulable_task_schedules",
            new_callable=mocker.AsyncMock,
            side_effect=SQLAlchemyError("no such table: periodictask"),
        )

        with caplog.at_level(logging.ERROR, logger=seed_module.logger.name):
            await seed_module.init_extensions_db()

        async with seed_maker() as session:
            states = await AppStateManager.all_lifecycle_states(session)
        assert "mysql_backups" in states
        assert [
            record
            for record in caplog.records
            if "unschedulable task schedules" in record.getMessage()
        ]

    async def test_restore_schedules_are_switched_off(
        self, mocker, seed_maker, beat_maker
    ) -> None:
        """Disable a stored restore schedule on the first startup after upgrade."""
        async with beat_maker() as session:
            schedule = IntervalSchedule(every=10, period=Period.MINUTES)
            session.add(schedule)
            await session.flush()
            session.add(
                PeriodicTask(
                    name="nightly-restore",
                    task="app.tasks.celery.execute_task_by_name",
                    enabled=True,
                    kwargs=json.dumps({"task_name": "r1"}),
                    schedule_model=schedule,
                )
            )
            await session.commit()
        async with seed_maker() as session:
            session.add(
                TaskFactory.build(name="r1", owner=RESTORES_OWNER, deleted_at=None)
            )
            await session.commit()
        self._patch_gate_session_makers(mocker, seed_maker, beat_maker)
        mocker.patch.object(
            seed_module.extensions_settings, "APPS", [_plugin("mysql_backups")]
        )

        await seed_module.init_extensions_db()

        async with beat_maker() as session:
            task = await BasePeriodicTaskManager.first(session, name="nightly-restore")
        assert task is not None, "no schedule named 'nightly-restore'"
        assert task.enabled is False

    @pytest.mark.parametrize("app_enabled", [True, False])
    async def test_gate_reflects_app_state(
        self, mocker, seed_maker, beat_maker, app_enabled
    ) -> None:
        """``init_extensions_db`` seeds wrapper rows and writes ``effective_enabled`` through."""
        task_name = "app.extensions.apps.alerts.celery.backup_alert_config"
        await self._seed_beat_row(beat_maker, ALERTS_TASK, task_name)
        self._patch_gate_session_makers(mocker, seed_maker, beat_maker)
        mocker.patch.object(
            seed_module,
            "get_system_periodic_tasks",
            return_value=[
                SystemPeriodicTaskSchedule(
                    schedule=IntervalSchedule(every=10, period=Period.MINUTES),
                    tasks=[
                        SystemPeriodicTaskData(
                            name=ALERTS_TASK,
                            task_name=task_name,
                            owner_app_key="alerts",
                        ),
                    ],
                ),
            ],
        )
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [_plugin("alerts", enabled=app_enabled)],
        )

        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            rows = await ExtensionsAppPeriodicTaskManager.list(session)
        assert {r.periodic_task_name for r in rows} == {ALERTS_TASK}

        async with beat_maker() as session:
            task = await BasePeriodicTaskManager.first(session, name=ALERTS_TASK)
        assert task.enabled is app_enabled

    async def test_unowned_snippet_sync_is_never_gated(
        self, mocker, seed_maker, beat_maker
    ) -> None:
        """Leave the snippet-sync beat row enabled with the snippets app disabled.

        The real schedule carries no ``owner_app_key``, so ``init_extensions_db`` writes
        no ``ExtensionsAppPeriodicTask`` wrapper row for it and ``apply_effective_enabled``
        has nothing to gate on -- ingestion keeps running through an operator
        disable.
        """
        await self._seed_beat_row(
            beat_maker, SNIPPETS_TASK, "app.extensions.snippets.celery.sync_snippets"
        )
        self._patch_gate_session_makers(mocker, seed_maker, beat_maker)
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [_plugin("snippets", enabled=False)],
        )

        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            rows = await ExtensionsAppPeriodicTaskManager.list(session)
        assert SNIPPETS_TASK not in {r.periodic_task_name for r in rows}

        async with beat_maker() as session:
            task = await BasePeriodicTaskManager.first(session, name=SNIPPETS_TASK)
        assert task.enabled is True

    async def test_upgrade_releases_a_gate_left_by_the_previous_owner(
        self, mocker, seed_maker, beat_maker
    ) -> None:
        """Enable a snippet-sync beat row an earlier owned regime left disabled.

        An instance upgrading with the snippets app already disabled carries a
        ``PeriodicTask.enabled = False`` written by the gate back when the schedule
        was owned, plus its now-orphan wrapper row. Seeding drops the wrapper, which
        leaves ``apply_effective_enabled`` nothing to iterate -- so without an
        explicit release the stale ``False`` is permanent and ingestion never
        restarts.
        """
        await self._seed_beat_row(
            beat_maker,
            SNIPPETS_TASK,
            "app.extensions.snippets.celery.sync_snippets",
            enabled=False,
        )
        async with seed_maker() as session:
            session.add(
                ExtensionsAppPeriodicTask(
                    periodic_task_name=SNIPPETS_TASK,
                    app_key="snippets",
                    user_enabled=True,
                )
            )
            await session.commit()
        self._patch_gate_session_makers(mocker, seed_maker, beat_maker)
        mocker.patch.object(
            seed_module.extensions_settings,
            "APPS",
            [_plugin("snippets", enabled=False)],
        )

        await seed_module.init_extensions_db()

        async with seed_maker() as session:
            rows = await ExtensionsAppPeriodicTaskManager.list(session)
        assert SNIPPETS_TASK not in {r.periodic_task_name for r in rows}

        async with beat_maker() as session:
            task = await BasePeriodicTaskManager.first(session, name=SNIPPETS_TASK)
        assert task.enabled is True
