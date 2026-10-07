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

"""constrain taskhistory status enum

Revision ID: 9c1d4f7a6b32
Revises: b5e17f6b3bc7
Create Date: 2026-09-21 21:10:00.000000

``taskhistory.status`` pre-dates the convention newer non-native enum columns
follow — ``native_enum=False`` paired with ``create_constraint=True`` — so the
column has always accepted any string at the database level, with only
application-side enum validation keeping it to the real status names. This
revision brings it in line by attaching the CHECK the model now declares.
``Task.backend``, ``TaskHistoryLog.stream``, and ``TaskHistoryLogState.stream``
remain exceptions to that convention and are unaffected by this revision.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9c1d4f7a6b32"
down_revision: Union[str, Sequence[str], None] = "b5e17f6b3bc7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "taskhistory"
#: Name SQLAlchemy derives for the enum's CHECK from the model side, so a
#: database built from migrations and one built from the metadata agree and
#: autogenerate reports no drift between them.
_STATUS_CONSTRAINT = "taskhistorystatusenum"
#: The enum's member *names*, which are what the column stores.
_STATUS_NAMES = (
    "FAILED",
    "PENDING",
    "RUNNING",
    "SUCCESS",
    "STOPPED",
    "LOST",
    "STALE",
    "UNLAUNCHABLE",
)


def upgrade() -> None:
    """Attach the CHECK constraining ``taskhistory.status`` to its member names.

    Batch mode, not a bare ``ALTER``: SQLite cannot attach a CHECK constraint to
    an existing table in place. A row holding a value outside the member set
    aborts this migration rather than being remapped — no code path writes such
    a value, so surfacing the bad data beats guessing a replacement for it.
    """
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.create_check_constraint(
            _STATUS_CONSTRAINT, sa.column("status").in_(_STATUS_NAMES)
        )


def downgrade() -> None:
    """Drop the CHECK, returning the column to an unconstrained ``VARCHAR``."""
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_STATUS_CONSTRAINT, type_="check")
