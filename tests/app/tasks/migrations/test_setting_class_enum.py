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

"""Tests for the Tasks-track ``setting_class`` CHECK-drop migration."""

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.core.db.utils import check_constraint_name
from tests.app.settingoverride_rows import insert_override

# The add_setting_override_table revision on the Tasks track, before SETTINGS /
# ALERT_SETTINGS were added to the setting_class CHECK constraint.
_TASKS_PRE_ENUM_REVISION = "fafdb0445092"

# The revision immediately below drop_setting_class_check_constraint, so
# downgrading to it runs exactly that revision's ``downgrade()``.
_CHECK_DROP_PARENT_REVISION = "c8e4a2b91f70"

#: Frozen copy of the members the CHECK enumerated right before the drop.
_PRE_DROP_MEMBERS = (
    "SEP_SETTINGS",
    "TASKS_SETTINGS",
    "SNIPPETS_SETTINGS",
    "SETTINGS",
    "ALERT_SETTINGS",
    "ANONYMIZER_SETTINGS",
    "ALERTS_SETTINGS",
    "INVENTORY_SETTINGS",
    "HEALTH_REPORT_SETTINGS",
)


def test_setting_class_accepts_unregistered_token_after_upgrade(tasks_alembic_config):
    """Accept a token never listed in the CHECK once ``upgrade heads`` has run."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")

    unregistered = ("CUSTOM_PLUGIN_SETTINGS", "APP_OWNED_SETTINGS")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for member in unregistered:
                insert_override(conn, member)
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM settingoverride"
            ).scalar()
        assert count == len(unregistered)
    finally:
        engine.dispose()


def test_setting_class_check_rejects_unlisted_token_before_drop(tasks_alembic_config):
    """Reject a SETTINGS row at the pre-enum revision, whose CHECK excludes it."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, _TASKS_PRE_ENUM_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            insert_override(conn, "SETTINGS")
    finally:
        engine.dispose()


def test_setting_class_check_is_dropped_after_upgrade(tasks_alembic_config):
    """Leave ``setting_class`` an unconstrained string after ``upgrade heads``."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            unconstrained = ("UNREGISTERED_SETTINGS", "X" * 50)
            assert (
                check_constraint_name(conn, "settingoverride", "setting_class") is None
            )
            for token in unconstrained:
                insert_override(conn, token)
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM settingoverride"
            ).scalar()
        assert count == len(unconstrained)
    finally:
        engine.dispose()


def test_setting_class_check_downgrade_deletes_unknown_rows(
    tasks_alembic_config, capsys
):
    """Delete out-of-list rows on downgrade, log the count, and restore the CHECK."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            insert_override(conn, "UNREGISTERED_SETTINGS")
            for token in _PRE_DROP_MEMBERS:
                insert_override(conn, token)
    finally:
        engine.dispose()

    # alembic fileConfig routes ``app.*`` to its console handler with
    # ``propagate = 0``, so the delete notice is on stderr, not in caplog.
    capsys.readouterr()
    # Named rather than relative: ``-1`` means "one step back from head", so a
    # revision landing after the CHECK drop silently retargets it.
    command.downgrade(cfg, _CHECK_DROP_PARENT_REVISION)
    assert "Deleted 1 settingoverride row(s)" in capsys.readouterr().err

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert (
                check_constraint_name(conn, "settingoverride", "setting_class")
                == "settingclassenum"
            )
            remaining = conn.exec_driver_sql(
                "SELECT setting_class FROM settingoverride"
            ).fetchall()
            assert sorted(row[0] for row in remaining) == sorted(_PRE_DROP_MEMBERS)
            with pytest.raises(IntegrityError):
                insert_override(conn, "UNREGISTERED_SETTINGS")
    finally:
        engine.dispose()
