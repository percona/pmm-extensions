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

"""add newest_attempt_at to inventory entities

Revision ID: bed760f9fb35
Revises: 6ee7bfe9c9d3
Create Date: 2026-09-23 16:15:00.000000

Add ``newest_attempt_at`` to every syncable inventory table, recording the
newest sync attempt accepted for a row whatever its outcome, so a late report
from an older attempt cannot clear a failing run or replace a newer error
message.

Existing rows are backfilled rather than left NULL, since NULL disables the
guard until a row's next report. A failing row takes the upgrade time: its
stored run start names only the run's first failure, so a later failure of the
same run left no trace, and seeding from the run start would let a late success
attempted between the two clear it. The cost is that a success attempted before
the upgrade but reported after it is refused, leaving the row failing until its
next sync. A clean row takes ``last_synced_at``, which no unrecorded failure can
postdate without having opened a run.

The guard holds only if every writer maintains the column, so no Inventory API
instance running the previous release may record sync health once this
revision is applied: stop them before upgrading rather than rolling the release
out behind the migration. A previous-release writer would record a failure
without advancing ``newest_attempt_at``, and a late success older than that
failure would then pass the guard and clear the run.
"""

from datetime import datetime, UTC

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "bed760f9fb35"
down_revision = "6ee7bfe9c9d3"
branch_labels = None
depends_on = None

#: Every table carrying the per-entity sync-health columns.
_SYNCABLE_TABLES = ("node", "service", "schema", "table")


def upgrade() -> None:
    """Add ``newest_attempt_at`` to every syncable table and backfill it."""
    # Bound as a typed Python value rather than ``now()`` so SQLite stores the
    # same text format the application writes, keeping string comparison sound.
    upgraded_at = sa.literal(datetime.now(UTC), sa.DateTime(timezone=True))
    for table_name in _SYNCABLE_TABLES:
        op.add_column(
            table_name,
            sa.Column("newest_attempt_at", sa.DateTime(timezone=True), nullable=True),
        )
        # A lightweight table construct rather than raw SQL: ``schema`` and
        # ``table`` are reserved words that need dialect-specific quoting.
        table = sa.table(
            table_name,
            sa.column("newest_attempt_at"),
            sa.column("sync_failing_since"),
            sa.column("last_synced_at"),
        )
        op.execute(
            table.update().values(
                newest_attempt_at=sa.case(
                    (table.c.sync_failing_since.is_not(None), upgraded_at),
                    else_=table.c.last_synced_at,
                )
            )
        )


def downgrade() -> None:
    """Drop ``newest_attempt_at``, reversing the order it was added in."""
    for table_name in reversed(_SYNCABLE_TABLES):
        op.drop_column(table_name, "newest_attempt_at")
