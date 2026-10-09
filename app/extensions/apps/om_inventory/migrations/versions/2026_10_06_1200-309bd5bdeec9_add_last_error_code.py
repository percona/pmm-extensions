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

"""add last_error_code to om_host and om_service

Revision ID: 309bd5bdeec9
Revises: a3f1c8d24b71
Create Date: 2026-10-06 12:00:00.000000

What kind of failure ``last_error`` is, as a ``ScanFailure``, so a resolution hint
can be keyed off it rather than off the message's wording.

A revision of its own rather than a column folded into ``a3f1c8d24b71``, although
that file is still rewritten in place until OM ships: its ``upgrade()`` creates each
table only when absent, so a column added there would never reach a database that
already has the tables - every developer stack and feature build that has run it.

A non-native enum with a CHECK constraint, like ``om_inventory_run.status``, so a new
failure kind needs a revision that widens the constraint. Nullable, with no default:
``None`` is what a healthy row holds, and what a failing row from before this
revision honestly is.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "309bd5bdeec9"
down_revision: Union[str, None] = "a3f1c8d24b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("om_host", "om_service")
_COLUMN = "last_error_code"
#: The name SQLAlchemy gives the model's CHECK constraint, so a database built by
#: this revision and one built from the metadata agree.
_CONSTRAINT = "scanfailure"
#: ``ScanFailure``'s member *names*, which are what the column stores.
_NAMES = (
    "DISPATCH_REJECTED",
    "NOT_STARTED",
    "TIMED_OUT",
    "BLOCKED",
    "ENVIRONMENT_SETUP_FAILED",
    "SCAN_CRASHED",
    "SCAN_LOST",
    "NO_OUTPUT",
    "DATABASE_UNREACHABLE",
    "DATABASE_AUTH_FAILED",
    "DATABASE_ERROR",
    "UNKNOWN",
)


def _column() -> sa.Column:
    """Build the column, fresh for each table it is added to.

    ``create_constraint`` is off and :func:`upgrade` creates the CHECK explicitly
    instead, so each engine gets it once: left on, the type would emit it as well -
    from ``ADD COLUMN`` on PostgreSQL, from the table rebuild on SQLite.

    :return: The column.
    """
    return sa.Column(
        _COLUMN,
        sa.Enum(*_NAMES, name=_CONSTRAINT, native_enum=False, create_constraint=False),
        nullable=True,
    )


def upgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)
    for table in _TABLES:
        # Batch mode, so SQLite gets the CHECK as well: it cannot add a constraint
        # to an existing table, and batch mode rebuilds the table to do it.
        with op.batch_alter_table(table, schema=schema) as batch_op:
            batch_op.add_column(_column())
            batch_op.create_check_constraint(
                _CONSTRAINT, sa.column(_COLUMN).in_(_NAMES)
            )


def downgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)
    for table in reversed(_TABLES):
        with op.batch_alter_table(table, schema=schema) as batch_op:
            batch_op.drop_constraint(_CONSTRAINT, type_="check")
            batch_op.drop_column(_COLUMN)
