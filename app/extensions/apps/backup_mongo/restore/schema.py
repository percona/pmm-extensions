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

"""Derive the AppSchema for the backup_mongo restores plugin model-first.

The schema is derived from the model-first
:class:`~app.extensions.apps.backup_mongo.restore.models.RestoreForm` plus the
:data:`~app.extensions.apps.backup_mongo.restore.views.restore_views` presentation
bundle. A restore's children are independent, fully-built payloads (not
``DerivedTask`` substitution specs), so there is no ``derived`` block.
"""

from app.extensions.apps.backup_mongo.restore.models import RestoreForm
from app.extensions.apps.backup_mongo.restore.views import restore_views
from app.extensions.apps.framework.form_dsl import derive_app_schema

restore_mongo_schema = derive_app_schema(
    RestoreForm,
    restore_views.layout,
    name="backup_mongo_restores",
    display_name="MongoDB Restores",
    item_display_name="restore",
    description=(
        "Configure and run Percona Backup for MongoDB (PBM) logical or "
        "physical restore operations."
    ),
    capabilities=restore_views.capabilities,
    list_view=restore_views.list_view,
    detail_view=restore_views.detail_view,
)
