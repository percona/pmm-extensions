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

"""Define tests for the app.tasks.config module."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy_celery_beat.models import Period

from app.core.celery.models import IntervalSchedule
from app.core.db.config import DatabaseOptions
from app.core.settings_override.registry import (
    field_reload_classification,
    is_explicit_not_overridable,
    ReloadClassification,
)
from app.extensions.sync.syncers.mysql.syncer import MySQLSyncer
from app.extensions.sync.syncers.pmm import PMMSyncer
from app.extensions.sync.syncers.system_facts.syncer import SystemFactsSyncer
from app.tasks.config import (
    MAX_SCHEDULED_SYNCER_LENGTH,
    PreExecutionCheckMode,
    tasks_settings,
    TasksSettings,
)
from tests.app.tasks.conftest import MYSQL_SYNCER, PMM_SYNCER, SYSTEM_FACTS_SYNCER

EXPECTED_UVICORN_PORT = 8002
EXPECTED_LOG_RETENTION_DAYS = 90
EXPECTED_LOG_PURGE_BATCH_SIZE = 10_000
MAX_LOG_RETENTION_DAYS = 365
EXPECTED_INVENTORY_SYNC_MINUTES = 15


class TestTasksSettings:
    """Test the TasksSettings class."""

    def test_settings_prefixes(self):
        """Assert SETTINGS_PREFIXES contains 'TASKS'."""
        assert TasksSettings.SETTINGS_PREFIXES == ["TASKS"]

    def test_default_uvicorn_port(self):
        """Assert default UVICORN_PORT is 8002."""
        assert tasks_settings.UVICORN_PORT == EXPECTED_UVICORN_PORT

    def test_default_database_name(self):
        """Assert default DATABASE.NAME is 'tasks.db'."""
        assert tasks_settings.DATABASE.NAME == "tasks.db"

    def test_default_database_is_database_options(self):
        """Assert DATABASE is a DatabaseOptions instance."""
        assert isinstance(tasks_settings.DATABASE, DatabaseOptions)

    def test_default_sync_lock_ttl(self):
        """Assert default SYNC_LOCK_TTL is 5 minutes."""
        assert timedelta(minutes=5) == tasks_settings.SYNC_LOCK_TTL

    def test_singleton_is_tasks_settings_instance(self):
        """Assert the module-level singleton is a TasksSettings instance."""
        assert isinstance(tasks_settings, TasksSettings)

    def test_default_pre_execution_connectivity_check(self):
        """Assert default PRE_EXECUTION_CONNECTIVITY_CHECK is ``warn``."""
        assert (
            tasks_settings.PRE_EXECUTION_CONNECTIVITY_CHECK
            == PreExecutionCheckMode.WARN
        )

    def test_default_log_retention_days(self):
        """Assert default LOG_RETENTION_DAYS is 90."""
        assert tasks_settings.LOG_RETENTION_DAYS == EXPECTED_LOG_RETENTION_DAYS

    def test_default_log_purge_batch_size(self):
        """Assert default LOG_PURGE_BATCH_SIZE is 10,000."""
        assert tasks_settings.LOG_PURGE_BATCH_SIZE == EXPECTED_LOG_PURGE_BATCH_SIZE

    def test_default_log_purge_interval_is_daily(self):
        """Assert default LOG_PURGE_INTERVAL runs once per day."""
        interval = tasks_settings.LOG_PURGE_INTERVAL
        assert isinstance(interval, IntervalSchedule)
        assert interval.every == 1
        assert interval.period == Period.DAYS

    def test_default_inventory_sync_interval_is_none(self):
        """Assert INVENTORY_SYNC_INTERVAL is unset by default."""
        assert TasksSettings().INVENTORY_SYNC_INTERVAL is None

    def test_default_inventory_sync_syncer_is_none(self):
        """Assert INVENTORY_SYNC_SYNCER is unset by default."""
        assert TasksSettings().INVENTORY_SYNC_SYNCER is None

    def test_inventory_sync_interval_parses_string_form(self):
        """Assert INVENTORY_SYNC_INTERVAL accepts the ``"15 minutes"`` YAML form."""
        interval = TasksSettings(
            INVENTORY_SYNC_INTERVAL="15 minutes"
        ).INVENTORY_SYNC_INTERVAL
        assert interval is not None
        assert (interval.every, interval.period) == (
            EXPECTED_INVENTORY_SYNC_MINUTES,
            Period.MINUTES,
        )

    def test_inventory_sync_syncer_rejects_blank_and_malformed_names(self):
        """Assert a blank name is refused rather than read as the sync-all default."""
        for value in ("", "   ", "not a path", "trailing."):
            with pytest.raises(ValidationError):
                TasksSettings(INVENTORY_SYNC_SYNCER=value)

    def test_inventory_sync_schedules_default_to_empty(self):
        """Assert a deployment configuring nothing keeps today's single schedule."""
        assert TasksSettings().INVENTORY_SYNC_SCHEDULES == []

    def test_inventory_sync_schedules_reject_the_scalar_syncer(self):
        """Assert an entry duplicating the pinned scalar syncer is refused."""
        with pytest.raises(ValidationError, match="already schedules"):
            TasksSettings(
                INVENTORY_SYNC_INTERVAL="15 minutes",
                INVENTORY_SYNC_SYNCER=PMM_SYNCER,
                INVENTORY_SYNC_SCHEDULES=[{"SYNCER": PMM_SYNCER, "INTERVAL": "1 days"}],
            )

    def test_inventory_sync_schedules_accept_the_scalar_syncer_when_it_seeds_nothing(
        self,
    ):
        """Assert the duplicate check is gated on the scalar pair actually seeding.

        With no interval the scalar pair seeds no schedule, so there is no second
        firing to prevent and "already schedules" would be a false rejection.
        """
        settings = TasksSettings(
            INVENTORY_SYNC_SYNCER=PMM_SYNCER,
            INVENTORY_SYNC_SCHEDULES=[{"SYNCER": PMM_SYNCER, "INTERVAL": "1 days"}],
        )
        assert [entry.syncer for entry in settings.INVENTORY_SYNC_SCHEDULES] == [
            PMM_SYNCER
        ]

    def test_inventory_sync_schedules_reject_a_repeated_syncer(self):
        """Assert one syncer at two intervals is refused rather than order-dependent.

        ``UniqueList`` compares whole entries, so these two survive it and then
        collide on a single seeded row name.
        """
        with pytest.raises(ValidationError, match="more than once"):
            TasksSettings(
                INVENTORY_SYNC_SCHEDULES=[
                    {"SYNCER": SYSTEM_FACTS_SYNCER, "INTERVAL": "1 days"},
                    {"SYNCER": SYSTEM_FACTS_SYNCER, "INTERVAL": "2 days"},
                ],
            )

    def test_inventory_sync_schedules_reject_the_sync_all_default(self):
        """Assert an entry beside the sync-all default is refused as double-firing."""
        with pytest.raises(ValidationError, match="sync-all default"):
            TasksSettings(
                INVENTORY_SYNC_INTERVAL="15 minutes",
                INVENTORY_SYNC_SCHEDULES=[
                    {"SYNCER": SYSTEM_FACTS_SYNCER, "INTERVAL": "1 days"}
                ],
            )

    def test_inventory_sync_schedules_reject_an_unschedulable_syncer_length(self):
        """Assert a path too long to name a seeded row is refused at load.

        It satisfies the dotted-path check, so without this it would be accepted
        and then overflow the beat row name mid-seed, failing startup on
        PostgreSQL. Pins the error's ``loc`` rather than its text, because that
        is what names the key and the offending entry to whoever has to edit it.
        """
        overlong = "a" * (MAX_SCHEDULED_SYNCER_LENGTH - 1) + ".B"
        with pytest.raises(ValidationError) as excinfo:
            TasksSettings(
                INVENTORY_SYNC_SCHEDULES=[{"SYNCER": overlong, "INTERVAL": "1 days"}]
            )
        (error,) = excinfo.value.errors()
        assert error["type"] == "too_long"
        assert error["loc"] == ("INVENTORY_SYNC_SCHEDULES", 0, "syncer")

    def test_inventory_sync_schedules_accept_the_longest_schedulable_syncer(self):
        """Assert the bound admits a path that exactly fills the budget."""
        longest = "a" * (MAX_SCHEDULED_SYNCER_LENGTH - 2) + ".B"
        settings = TasksSettings(
            INVENTORY_SYNC_SCHEDULES=[{"SYNCER": longest, "INTERVAL": "1 days"}]
        )
        assert settings.INVENTORY_SYNC_SCHEDULES[0].syncer == longest

    def test_inventory_sync_schedules_accept_a_distinct_syncer(self):
        """Assert an entry beside a *pinned* default is the supported combination."""
        settings = TasksSettings(
            INVENTORY_SYNC_INTERVAL="15 minutes",
            INVENTORY_SYNC_SYNCER=PMM_SYNCER,
            INVENTORY_SYNC_SCHEDULES=[
                {"SYNCER": SYSTEM_FACTS_SYNCER, "INTERVAL": "1 days"}
            ],
        )
        assert [entry.syncer for entry in settings.INVENTORY_SYNC_SCHEDULES] == [
            SYSTEM_FACTS_SYNCER
        ]
        assert settings.INVENTORY_SYNC_SCHEDULES[0].interval.period == Period.DAYS

    def test_log_retention_days_rejects_non_positive(self):
        """Assert LOG_RETENTION_DAYS rejects zero and negative values."""
        for value in (0, -1):
            with pytest.raises(ValidationError):
                TasksSettings(LOG_RETENTION_DAYS=value)

    def test_log_retention_days_rejects_above_max(self):
        """Assert LOG_RETENTION_DAYS rejects values above the 365-day ceiling."""
        with pytest.raises(ValidationError):
            TasksSettings(LOG_RETENTION_DAYS=MAX_LOG_RETENTION_DAYS + 1)

    def test_log_retention_days_accepts_bounds(self):
        """Assert LOG_RETENTION_DAYS accepts the inclusive 1..365 bounds."""
        assert TasksSettings(LOG_RETENTION_DAYS=1).LOG_RETENTION_DAYS == 1
        assert (
            TasksSettings(LOG_RETENTION_DAYS=MAX_LOG_RETENTION_DAYS).LOG_RETENTION_DAYS
            == MAX_LOG_RETENTION_DAYS
        )

    def test_default_hook_module_allowlist(self) -> None:
        """Assert the default allow-list admits the namespace holding the task apps."""
        assert tasks_settings.HOOK_MODULE_ALLOWLIST == ("app.extensions.apps",)

    def test_hook_module_allowlist_is_not_runtime_overridable(self) -> None:
        """Assert the allow-list cannot be widened through the settings API.

        Widening the namespace at runtime would itself be a privilege-escalation
        path, since the resolved callable is imported and invoked.
        """
        field_info = TasksSettings.model_fields["HOOK_MODULE_ALLOWLIST"]

        assert (
            field_reload_classification(field_info)
            is ReloadClassification.NOT_OVERRIDABLE
        )
        assert is_explicit_not_overridable(field_info)

    @pytest.mark.parametrize(
        "root",
        [
            "app/acme/apps",
            "app.acme.apps.",
            ".app.acme.apps",
            "app..acme",
            " app.acme.apps",
            "",
            "app.extensions.apps:builder",
        ],
    )
    def test_hook_module_allowlist_rejects_a_root_nothing_can_match(
        self, root: str
    ) -> None:
        """Reject an allow-list entry that is not a dotted module path.

        A root no hook path can ever match would otherwise load cleanly and then
        be reported as allow-listed in every rejection message.
        """
        with pytest.raises(ValidationError, match="dotted module path"):
            TasksSettings(HOOK_MODULE_ALLOWLIST=(root,))

    def test_hook_module_allowlist_accepts_an_extra_root(self) -> None:
        """Accept a well-formed dotted root added alongside the shipped default."""
        settings = TasksSettings(
            HOOK_MODULE_ALLOWLIST=("app.extensions.apps", "acme_plugins.hooks")
        )

        assert settings.HOOK_MODULE_ALLOWLIST == (
            "app.extensions.apps",
            "acme_plugins.hooks",
        )


class TestSyncerNameConstants:
    """Test that the suite's hand-kept syncer paths match the real classes."""

    def test_the_hand_kept_syncer_paths_match_their_classes(self) -> None:
        """Pin each shared syncer-path constant against the class it names.

        The tasks service never imports the extensions syncers, so the constants are a
        hand-kept copy — but a test can import them, and nothing else compares the
        two. Settings validate a syncer path's shape and not its existence, so a
        renamed syncer module would otherwise leave these stale and every
        assertion using them still green.
        """
        assert PMMSyncer.get_name() == PMM_SYNCER
        assert MySQLSyncer.get_name() == MYSQL_SYNCER
        assert SystemFactsSyncer.get_name() == SYSTEM_FACTS_SYNCER
