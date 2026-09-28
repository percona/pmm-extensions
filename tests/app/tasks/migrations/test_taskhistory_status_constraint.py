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

"""Tests for the Tasks-track taskhistory status CHECK-constraint migration."""

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.tasks.models import TaskHistoryStatusEnum

#: The head immediately before the CHECK constraint is added.
_PRE_CONSTRAINT_REVISION = "b5e17f6b3bc7"

_INSERT_HISTORY_ROW = (
    "INSERT INTO taskhistory "
    "(created_at, task_id, execution_request, status, log_producer_epoch) "
    "VALUES ('2026-01-01 00:00:00', ?, '{}', ?, 0)"
)

#: A status name no member of the enum carries, so only the CHECK rejects it.
_OUT_OF_ENUM_STATUS = "ABANDONED"


def test_upgrade_constrains_status_to_the_enum_domain(tasks_alembic_config):
    """Assert the migrated column rejects a status outside the enum's members."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                _INSERT_HISTORY_ROW, (1, TaskHistoryStatusEnum.UNLAUNCHABLE.name)
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.exec_driver_sql(_INSERT_HISTORY_ROW, (2, _OUT_OF_ENUM_STATUS))
    finally:
        engine.dispose()


def test_upgrade_aborts_on_a_pre_existing_out_of_enum_row(tasks_alembic_config):
    """Assert a stored value outside the domain stops the upgrade, unremapped.

    The migration deliberately carries no cleanup branch, so the only thing
    standing between a bad row and a silent coercion is that the CHECK-adding
    ALTER refuses it.
    """
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, _PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_INSERT_HISTORY_ROW, (1, _OUT_OF_ENUM_STATUS))
    finally:
        engine.dispose()

    with pytest.raises(IntegrityError):
        command.upgrade(cfg, "heads")


def test_downgrade_drops_the_constraint(tasks_alembic_config):
    """Assert the downgrade returns the column to its unconstrained state."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")
    command.downgrade(cfg, _PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_INSERT_HISTORY_ROW, (1, _OUT_OF_ENUM_STATUS))
    finally:
        engine.dispose()
