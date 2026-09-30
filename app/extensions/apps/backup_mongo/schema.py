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

"""Derive the AppSchema for the backup_mongo plugin model-first.

The schema is derived from the model-first
:class:`~app.extensions.apps.backup_mongo.models.BackupForm` plus the
:data:`~app.extensions.apps.backup_mongo.views.backup_mongo_views` presentation
bundle. :data:`BACKUP_MONGO_DERIVED` carries the four ``DerivedTask`` specs (with
their two-step ``payload_substitutions``) into the ``derived`` block of
``GET /schema``; the cascade create route reuses the same specs to POST the
logical, physical, status, and incremental siblings. The Task-section description
is built from those specs so the user-facing copy cannot drift from
``name_suffix``.
"""

from dataclasses import replace

from app.extensions.apps.backup_mongo.models import BackupForm, BackupType
from app.extensions.apps.backup_mongo.views import backup_mongo_views
from app.extensions.apps.framework.form_dsl import (
    derive_app_schema,
    TASK_SECTION_LAYOUT,
)
from app.extensions.apps.framework.schema import DerivedTask, RelatedApp

BACKUP_MONGO_DERIVED = [
    DerivedTask(
        name_suffix="-logical",
        payload_substitutions={
            BackupType.PBM_CONFIG.value: BackupType.PBM_LOGICAL.value,
        },
        data_overrides={"backup_type": BackupType.PBM_LOGICAL.value},
    ),
    DerivedTask(
        name_suffix="-physical",
        payload_substitutions={
            BackupType.PBM_CONFIG.value: BackupType.PBM_LOGICAL.value,
            BackupType.PBM_LOGICAL.value: BackupType.PBM_PHYSICAL.value,
        },
        data_overrides={"backup_type": BackupType.PBM_PHYSICAL.value},
    ),
    DerivedTask(
        name_suffix="-status",
        payload_substitutions={
            BackupType.PBM_CONFIG.value: BackupType.PBM_LOGICAL.value,
            BackupType.PBM_LOGICAL.value: BackupType.PBM_STATUS.value,
        },
        data_overrides={"backup_type": BackupType.PBM_STATUS.value},
    ),
    DerivedTask(
        name_suffix="-incremental",
        payload_substitutions={
            BackupType.PBM_CONFIG.value: BackupType.PBM_LOGICAL.value,
            BackupType.PBM_LOGICAL.value: BackupType.PBM_INCREMENTAL.value,
        },
        data_overrides={"backup_type": BackupType.PBM_INCREMENTAL.value},
    ),
]

#: Task-section note lists derived ``name_suffix`` values so UI copy cannot
#: drift from :data:`BACKUP_MONGO_DERIVED` (derived-task fan-out only).
_TASK_SIBLING_DESCRIPTION = (
    "Creating this backup produces sibling tasks: "
    + ", ".join(spec.name_suffix.removeprefix("-") for spec in BACKUP_MONGO_DERIVED)
    + "."
)

#: Task section description overlays the shared layout.
_BACKUP_MONGO_LAYOUT = replace(
    backup_mongo_views.layout,
    sections=tuple(
        replace(section, description=_TASK_SIBLING_DESCRIPTION)
        if section.key == TASK_SECTION_LAYOUT.key
        else section
        for section in backup_mongo_views.layout.sections
    ),
)

backup_mongo_schema = derive_app_schema(
    BackupForm,
    _BACKUP_MONGO_LAYOUT,
    name="backup_mongo",
    display_name="MongoDB Backups",
    item_display_name="backup",
    description=(
        "Configure Percona Backup for MongoDB (PBM) and manage logical, "
        "physical, status, and incremental backup tasks."
    ),
    capabilities=backup_mongo_views.capabilities,
    list_view=backup_mongo_views.list_view,
    detail_view=backup_mongo_views.detail_view,
    derived=BACKUP_MONGO_DERIVED,
    related_apps=[
        RelatedApp(
            app_key="backup_mongo/restore",
            label="Restores",
            route_segment="restores",
        ),
    ],
)
