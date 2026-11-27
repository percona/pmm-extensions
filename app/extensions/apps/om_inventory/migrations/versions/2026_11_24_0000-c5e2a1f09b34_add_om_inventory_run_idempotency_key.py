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

"""add idempotency_key to om_inventory_run

Revision ID: c5e2a1f09b34
Revises: b4a80b222f57
Create Date: 2026-11-24 00:00:00.000000

``om_inventory_run`` gains ``idempotency_key``, the key a scan request carries so a
retried request fulfils once: ``POST /scans`` finds the run already recorded under
the key rather than starting a second. The column is nullable because the scheduled
estate sweep carries no key, and a unique index enforces the fulfil-once rule; NULLs
are distinct, so the keyless sweeps never collide. The schema is translated by the
connection throughout.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "c5e2a1f09b34"
down_revision: Union[str, None] = "b4a80b222f57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)
    op.add_column(
        "om_inventory_run",
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        schema=schema,
    )
    op.create_index(
        "ix_om_inventory_run_idempotency_key",
        "om_inventory_run",
        ["idempotency_key"],
        unique=True,
        schema=schema,
    )


def downgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)
    op.drop_index(
        "ix_om_inventory_run_idempotency_key", "om_inventory_run", schema=schema
    )
    op.drop_column("om_inventory_run", "idempotency_key", schema=schema)
