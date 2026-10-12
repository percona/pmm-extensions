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

"""Test the taskhistory status CHECK migration against real PostgreSQL.

The SQLite sibling cannot fail on the step most likely to break here.
``batch_alter_table`` recreates the table on SQLite, so the CHECK arrives as
part of a fresh ``CREATE TABLE`` and the plain ``ALTER TABLE ... ADD CONSTRAINT``
this engine takes is never exercised.

Everything runs over ``asyncpg``: the tasks track's ``env.py`` builds its own
async engine, and the ``test_postgres`` CI job installs the ``postgresql`` group
only, so no sync driver is available to lean on.

Both cases stop at the CHECK revision itself rather than ``heads``, so a
failure in an unrelated later revision cannot mask or break this harness.
"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.tasks.models import TaskHistoryStatusEnum
from tests.app.inventory.migrations.postgres_support import run_on_postgres
from tests.app.tasks.migrations.conftest import (
    TASKHISTORY_STATUS_CONSTRAINT,
    TASKHISTORY_STATUS_CONSTRAINT_REVISION,
    TASKHISTORY_STATUS_OUT_OF_ENUM,
    TASKHISTORY_STATUS_PRE_CONSTRAINT_REVISION,
)

#: The ``created_at`` / ``updated_at`` pair every real-PostgreSQL seed needs,
#: spelled with the offset ``timestamptz`` requires.
_SEED_TIMESTAMPS = "'2026-01-01 00:00:00+00'"

_CONSTRAINT_PRESENT = (
    "SELECT COUNT(*) FROM pg_constraint "
    "WHERE conname = :name AND contype = 'c' "
    "AND conrelid = 'taskhistory'::regclass"
)

pytestmark = pytest.mark.postgres


async def _insert_task(conn: AsyncConnection, name: str) -> int:
    """Insert a parent ``task`` row and return its primary key."""
    result = await conn.execute(
        text(
            "INSERT INTO task (created_at, updated_at, name, data, backend, owner, "
            "is_template, protected, alert_on_fail) "
            f"VALUES ({_SEED_TIMESTAMPS}, {_SEED_TIMESTAMPS}, :name, "
            "CAST('{{}}' AS JSON), 'NOMAD', 'ANY', false, false, false) "
            "RETURNING id"
        ),
        {"name": name},
    )
    return result.scalar_one()


async def _insert_history(conn: AsyncConnection, task_id: int, status: str) -> int:
    """Insert a ``taskhistory`` row and return its primary key."""
    result = await conn.execute(
        text(
            "INSERT INTO taskhistory "
            "(created_at, task_id, execution_request, status, log_producer_epoch) "
            f"VALUES ({_SEED_TIMESTAMPS}, :task_id, CAST('{{}}' AS JSON), "
            ":status, 0) RETURNING id"
        ),
        {"task_id": task_id, "status": status},
    )
    return result.scalar_one()


async def _constraint_count(conn: AsyncConnection) -> int:
    """Return how many CHECKs of the guard's name the table carries."""
    result = await conn.execute(
        text(_CONSTRAINT_PRESENT), {"name": TASKHISTORY_STATUS_CONSTRAINT}
    )
    return result.scalar_one()


async def _history_status(conn: AsyncConnection, history_id: int) -> str:
    """Return the stored ``status`` for the named history row."""
    result = await conn.execute(
        text("SELECT status FROM taskhistory WHERE id = :id"),
        {"id": history_id},
    )
    return result.scalar_one()


def test_upgrade_rejects_out_of_enum_status(tasks_postgres_config):
    """Carry the CHECK after the plain ALTER, and reject an out-of-enum status."""
    cfg, url = tasks_postgres_config
    command.upgrade(cfg, TASKHISTORY_STATUS_CONSTRAINT_REVISION)

    assert run_on_postgres(url, _constraint_count) == 1

    async def _seed_valid(conn: AsyncConnection) -> int:
        task_id = await _insert_task(conn, "constrained")
        await _insert_history(conn, task_id, TaskHistoryStatusEnum.UNLAUNCHABLE.name)
        return task_id

    task_id = run_on_postgres(url, _seed_valid)
    with pytest.raises(IntegrityError, match=TASKHISTORY_STATUS_CONSTRAINT):
        run_on_postgres(
            url,
            lambda conn: _insert_history(conn, task_id, TASKHISTORY_STATUS_OUT_OF_ENUM),
        )


def test_downgrade_accepts_out_of_enum_status(tasks_postgres_config):
    """Accept an out-of-enum status again once the CHECK has been dropped."""
    cfg, url = tasks_postgres_config
    command.upgrade(cfg, TASKHISTORY_STATUS_CONSTRAINT_REVISION)
    command.downgrade(cfg, TASKHISTORY_STATUS_PRE_CONSTRAINT_REVISION)

    assert run_on_postgres(url, _constraint_count) == 0

    async def _seed(conn: AsyncConnection) -> int:
        task_id = await _insert_task(conn, "loose")
        return await _insert_history(conn, task_id, TASKHISTORY_STATUS_OUT_OF_ENUM)

    history_id = run_on_postgres(url, _seed)
    assert (
        run_on_postgres(url, lambda conn: _history_status(conn, history_id))
        == TASKHISTORY_STATUS_OUT_OF_ENUM
    )
