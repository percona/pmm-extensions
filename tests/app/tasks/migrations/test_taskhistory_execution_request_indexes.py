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

"""Tests for the SQLite execution_request expression indexes on taskhistory."""

from alembic import command
from sqlalchemy import create_engine

_RESTORING_REVISION = "5d3b7a9c1e44"
_PRE_RESTORING_REVISION = "2e932d05a0d9"
_EXPECTED_INDEXES = {"ix_taskhistory_exec_task", "ix_taskhistory_exec_target"}


def _taskhistory_indexes(sync_url: str) -> set[str]:
    """Return the names of the indexes on ``taskhistory`` from ``sqlite_master``."""
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'taskhistory'"
            ).all()
    finally:
        engine.dispose()
    return {row[0] for row in rows}


def test_head_has_execution_request_indexes(tasks_alembic_config):
    """Assert a fully upgraded database has both expression indexes.

    This guards the final head, so a future ``batch_alter_table('taskhistory')``
    that drops the indexes fails here.
    """
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, "heads")
    assert _taskhistory_indexes(sync_url) >= _EXPECTED_INDEXES


def test_upgrade_from_previous_head_restores_indexes(tasks_alembic_config):
    """Assert upgrading from the old head adds the indexes and the planner uses them."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, _PRE_RESTORING_REVISION)
    assert not _EXPECTED_INDEXES & _taskhistory_indexes(sync_url)

    command.upgrade(cfg, _RESTORING_REVISION)
    assert _taskhistory_indexes(sync_url) >= _EXPECTED_INDEXES

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            plan = conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM taskhistory "
                "WHERE json_extract(execution_request, '$.task') = 'x'"
            ).all()
    finally:
        engine.dispose()
    assert "ix_taskhistory_exec_task" in " ".join(str(row[-1]) for row in plan)


def test_downgrade_removes_indexes_and_upgrade_is_repeatable(tasks_alembic_config):
    """Assert the downgrade restores the prior schema and re-upgrading succeeds."""
    cfg, sync_url = tasks_alembic_config
    command.upgrade(cfg, _RESTORING_REVISION)
    command.downgrade(cfg, _PRE_RESTORING_REVISION)
    assert not _EXPECTED_INDEXES & _taskhistory_indexes(sync_url)

    command.upgrade(cfg, _RESTORING_REVISION)
    assert _taskhistory_indexes(sync_url) >= _EXPECTED_INDEXES
