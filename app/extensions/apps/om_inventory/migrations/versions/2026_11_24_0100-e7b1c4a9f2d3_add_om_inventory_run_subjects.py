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

"""add subjects to om_inventory_run

Revision ID: e7b1c4a9f2d3
Revises: c5e2a1f09b34
Create Date: 2026-11-24 01:00:00.000000

The members a scan collects on, resolved by pmm-managed and sent with the scan
request, so the run's worker reads them back from the row rather than the request.
``server_default`` "[]" fills the rows already there; the monitored sweep and an
operation scan that resolves to no member both carry an empty list honestly.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "e7b1c4a9f2d3"
down_revision: Union[str, None] = "c5e2a1f09b34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "om_inventory_run",
        sa.Column(
            "subjects",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
            server_default=sa.text("('[]')"),
        ),
        schema=om_schema(sep_settings.DATABASE),
    )


def downgrade() -> None:
    op.drop_column(
        "om_inventory_run", "subjects", schema=om_schema(sep_settings.DATABASE)
    )
