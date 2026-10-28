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

"""drop setting_class CHECK constraint

Revision ID: 74720aeda25b
Revises: d1e2f3a4b5c6
Create Date: 2026-08-17 22:10:00.000000

Widen ``settingoverride.setting_class`` from a CHECK-constrained enum to a
plain string so an app can declare a settings class without a migration.
The registry, not the database, is now the authority for which classes
exist.

Downgrade re-adds the CHECK with the pre-drop member list and deletes rows
naming a class outside that list.
"""

from app.core.settings_override.alembic_ops import (
    downgrade_restore_setting_class_check,
    upgrade_drop_setting_class_check,
)

_OLD_MEMBERS = (
    "SEP_SETTINGS",
    "TASKS_SETTINGS",
    "SNIPPETS_SETTINGS",
    "SETTINGS",
    "ALERT_SETTINGS",
    "ANONYMIZER_SETTINGS",
    "ALERTS_SETTINGS",
    "INVENTORY_SETTINGS",
    "HEALTH_REPORT_SETTINGS",
)

# revision identifiers, used by Alembic.
revision = "74720aeda25b"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the ``setting_class`` CHECK constraint."""
    upgrade_drop_setting_class_check(_OLD_MEMBERS)


def downgrade() -> None:
    """Re-add the CHECK, deleting rows whose ``setting_class`` is outside it."""
    downgrade_restore_setting_class_check(_OLD_MEMBERS)
