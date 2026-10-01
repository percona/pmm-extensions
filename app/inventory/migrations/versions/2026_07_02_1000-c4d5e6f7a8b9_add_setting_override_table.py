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

"""add setting_override table

The Inventory database gains its first ``settingoverride`` table so
``InventorySettings`` can be wired into the runtime override framework. The
``setting_class`` CHECK constraint lists every shared ``SettingClassEnum``
member (including ``INVENTORY_SETTINGS``) so all three tracks converge on the
same allowed set. The table is created under the shared advisory lock the other
tracks' settingoverride migrations hold.

Revision ID: c4d5e6f7a8b9
Revises: b73c0110ad55
Create Date: 2026-07-02 10:00:00.000000

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

import app.core.db.sql_types
from app.core.db.utils import acquire_pg_advisory_xact_lock
from app.core.settings_override.constants import SETTINGOVERRIDE_MIGRATION_LOCK_KEY

# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "b73c0110ad55"
branch_labels = None
depends_on = None

_MEMBERS = (
    "SEP_SETTINGS",
    "TASKS_SETTINGS",
    "SNIPPETS_SETTINGS",
    "MESSAGES_SETTINGS",
    "SETTINGS",
    "ALERT_SETTINGS",
    "ANONYMIZER_SETTINGS",
    "ALERTS_SETTINGS",
    "INVENTORY_SETTINGS",
)


def upgrade() -> None:
    """Create the ``settingoverride`` table on the Inventory database."""
    bind = op.get_bind()
    acquire_pg_advisory_xact_lock(bind, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)
    if sa.inspect(bind).has_table("settingoverride"):
        return
    op.create_table(
        "settingoverride",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "setting_class",
            sa.Enum(
                *_MEMBERS,
                name="settingclassenum",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("value", app.core.db.sql_types.AutoJSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_settingoverride_is_active"),
        "settingoverride",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        op.f("ix_settingoverride_key"), "settingoverride", ["key"], unique=False
    )
    op.create_index(
        "ix_settingoverride_setting_class_key",
        "settingoverride",
        ["setting_class", "key"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``settingoverride`` table from the Inventory database."""
    bind = op.get_bind()
    acquire_pg_advisory_xact_lock(bind, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)
    if not sa.inspect(bind).has_table("settingoverride"):
        return
    op.drop_index(
        "ix_settingoverride_setting_class_key", table_name="settingoverride"
    )
    op.drop_index(op.f("ix_settingoverride_key"), table_name="settingoverride")
    op.drop_index(op.f("ix_settingoverride_is_active"), table_name="settingoverride")
    op.drop_table("settingoverride")
