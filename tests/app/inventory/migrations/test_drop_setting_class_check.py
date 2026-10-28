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

"""Test the Inventory-track ``drop_setting_class_check_constraint`` downgrade."""

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.core.db.utils import check_constraint_name

# The revision immediately below the drop, so downgrading to it runs exactly
# that revision's ``downgrade()``.
_CHECK_DROP_PARENT_REVISION = "f3a4b5c6d7e8"

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


def _insert_override(conn, setting_class: str) -> None:
    """Insert a minimal ``settingoverride`` row with the given setting_class."""
    conn.exec_driver_sql(
        "INSERT INTO settingoverride "
        "(created_at, setting_class, key, value, is_active) "
        "VALUES ('2026-01-01 00:00:00', ?, 'X', 'true', 1)",
        (setting_class,),
    )


def test_downgrade_keeps_pre_drop_tokens_and_restores_check(
    inventory_alembic_config, capsys
):
    """Keep one row per pre-drop token, delete the rest, and restore the CHECK."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            _insert_override(conn, "UNREGISTERED_SETTINGS")
            for token in _PRE_DROP_MEMBERS:
                _insert_override(conn, token)
    finally:
        engine.dispose()

    # alembic fileConfig routes ``app.*`` to its console handler with
    # ``propagate = 0``, so the delete notice is on stderr, not in caplog.
    capsys.readouterr()
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
                _insert_override(conn, "UNREGISTERED_SETTINGS")
    finally:
        engine.dispose()
