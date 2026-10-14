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

"""add hosts_finished to om_inventory_run

Revision ID: 77f3037c84cf
Revises: 309bd5bdeec9
Create Date: 2026-10-07 22:00:00.000000

How many hosts' scans have come back while a sweep runs, so the page can say how far
it has got instead of showing a spinner for tens of seconds.

Its own revision, after ``309bd5bdeec9``, for the reason that one gives:
``a3f1c8d24b71`` creates each table only when absent, so a column added there would
never reach a database that already has the table. ``server_default`` "0" fills the
rows already there; a finished sweep's count is set when it ends.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "77f3037c84cf"
down_revision: Union[str, None] = "309bd5bdeec9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "om_inventory_run",
        sa.Column("hosts_finished", sa.Integer(), nullable=False, server_default="0"),
        schema=om_schema(sep_settings.DATABASE),
    )


def downgrade() -> None:
    op.drop_column(
        "om_inventory_run", "hosts_finished", schema=om_schema(sep_settings.DATABASE)
    )
