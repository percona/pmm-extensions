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

"""add worker_boot_id to om_inventory_run

Revision ID: 63a9e7daf7dc
Revises: 77f3037c84cf
Create Date: 2026-10-30 12:00:00.000000

Which worker boot claimed a run, so a run whose worker was restarted can be failed as
soon as a new worker starts.

Nullable with no default: a row already there was claimed by no boot, which is what
``NULL`` says.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "63a9e7daf7dc"
down_revision: Union[str, None] = "77f3037c84cf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "om_inventory_run",
        sa.Column("worker_boot_id", sa.Uuid(), nullable=True),
        schema=om_schema(sep_settings.DATABASE),
    )


def downgrade() -> None:
    op.drop_column(
        "om_inventory_run", "worker_boot_id", schema=om_schema(sep_settings.DATABASE)
    )
