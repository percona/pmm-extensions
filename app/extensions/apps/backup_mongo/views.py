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

"""Define the presentation bundle for the MongoDB Backups app.

Section *membership* and *order* are declared on
:class:`~app.extensions.apps.backup_mongo.models.BackupForm` (via ``Ui(section=...)``
and field-declaration order); what lives here is the part the model cannot
express: the section titles, the collapse metadata, the list columns, and the
UI capability flags. The Task-section note about derived sibling types is
applied in :mod:`app.extensions.apps.backup_mongo.schema` from
:data:`~app.extensions.apps.backup_mongo.schema.BACKUP_MONGO_DERIVED`. These feed the
derived ``GET /schema``.
"""

from app.extensions.apps.backup_mongo.models import BackupType
from app.extensions.apps.framework.apps import Views
from app.extensions.apps.framework.form_dsl import (
    FormLayout,
    SectionLayout,
    TASK_SECTION_LAYOUT,
)
from app.extensions.apps.framework.schema import (
    Capabilities,
    default_columns,
    EXECUTOR_HOST_COLUMN,
    ListView,
)
from app.extensions.apps.shared.backups.columns import backup_type_column

backup_mongo_views = Views(
    layout=FormLayout(
        sections=(
            TASK_SECTION_LAYOUT,
            # Storage and Point-in-Time Recovery are gone: they are cluster-wide PBM
            # configuration, and restating them on every backup is what let a backup
            # overwrite the cluster's config. They live on the PBM Configuration tab
            # now, together with the backup options that are also configuration
            # (priority, timeouts, oplog span, parallel collections). What is left
            # here is what one run actually decides.
            SectionLayout(
                key="BackupOptions",
                title="Options for this backup",
                collapsible=True,
                collapsed_by_default=True,
                description=(
                    "Command-line flags for this run only. They are passed to "
                    "pbm backup and override the cluster's defaults, which are set "
                    "on the PBM Configuration tab."
                ),
            ),
        )
    ),
    list_view=ListView(
        columns=default_columns(
            EXECUTOR_HOST_COLUMN,
            backup_type_column(BackupType.LABELS),
        ),
        default_sort="name",
    ),
    capabilities=Capabilities(chaining=True, alert_on_fail=True, scheduling=True),
)
