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

"""Derive the AppSchema for the PBM Configuration child app.

Shares ``BackupForm`` and the Storage / Point-in-Time Recovery / Backup Options
layout with its parent rather than redeclaring either. ``derive_form_sections``
is strict in both directions -- a field naming a section absent from the layout
raises, and so does a layout section no field claims -- so a config-only layout
over the parent's form is not expressible while that form still carries its Task
fields. Reusing both is the honest way to say "the same configuration, applied on
its own" until the backups form is stripped of the config sections and this
becomes their only home.

The difference from the parent is what is *not* here: no ``derived`` block. A
config task applies configuration and produces no logical / physical / status /
incremental siblings, which is the whole reason this app exists -- applying
cluster-wide PBM config stops being a side effect of creating a backup and
becomes something an operator asks for.
"""

from app.sep.apps.backup_mongo.models import BackupForm
from app.sep.apps.backup_mongo.views import backup_mongo_views
from app.sep.apps.framework.form_dsl import derive_app_schema

backup_mongo_config_schema = derive_app_schema(
    BackupForm,
    backup_mongo_views.layout,
    name="backup_mongo_config",
    display_name="PBM Configuration",
    description=(
        "Read and apply the Percona Backup for MongoDB (PBM) configuration for a "
        "cluster: storage, point-in-time recovery, and backup options. S3 "
        "credentials are shown as PBM reports them and are not written from here."
    ),
    capabilities=backup_mongo_views.capabilities,
    list_view=backup_mongo_views.list_view,
    detail_view=backup_mongo_views.detail_view,
)
