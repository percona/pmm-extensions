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

"""Restore the SQLite execution_request expression indexes on taskhistory

Revision ID: 5d3b7a9c1e44
Revises: 2e932d05a0d9
Create Date: 2026-10-28 23:00:00.000000

``ix_taskhistory_exec_task`` and ``ix_taskhistory_exec_target`` were created by
``9ebd648709e8`` as expression indexes. SQLAlchemy cannot reflect expression
indexes, so the ``batch_alter_table('taskhistory')`` table rebuild in
``3a4dfc2a2be8`` silently dropped them on SQLite. This revision recreates them.

SQLite only: PostgreSQL batch mode never rebuilds the table, and MySQL is a
deliberate no-op (see ``9ebd648709e8``). ``payload`` stays unindexed for the
reason recorded there.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "5d3b7a9c1e44"
down_revision: Union[str, None] = "2e932d05a0d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAMES = (
    "ix_taskhistory_exec_task",
    "ix_taskhistory_exec_target",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_taskhistory_exec_task "
        "ON taskhistory (json_extract(execution_request, '$.task'))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_taskhistory_exec_target "
        "ON taskhistory (json_extract(execution_request, '$.target'))"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for name in _INDEX_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
