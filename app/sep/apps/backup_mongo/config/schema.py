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

Extends rather than copies: the form is
:class:`~app.sep.apps.backup_mongo.config.models.BackupConfigForm`, a subclass of
its parent's ``BackupForm``, and the layout is the parent's sections plus one.
``derive_form_sections`` is strict in both directions -- a field naming a section
absent from the layout raises, and so does a layout section no field claims -- so
the two must be grown together, which is exactly what subclassing buys: a section
added here cannot exist without a field, and a field added to the parent cannot
go missing from this form.

``Advanced`` is what the parent gives up. ``credentials_path`` describes the
execution host rather than any one backup, so it belongs with the configuration an
operator applies once per cluster, not beside the database service on every backup
form. It is marked ``advanced`` rather than merely collapsed: the
``$HOME/.mongodb_uri`` fallback is right nearly always, so the field should be out
of the way behind "Show advanced options" and surface itself only when it holds a
value or an error points into it.

The other difference from the parent is what is *not* here: no ``derived`` block.
A config task applies configuration and produces no logical / physical / status /
incremental siblings, which is the whole reason this app exists -- applying
cluster-wide PBM config stops being a side effect of creating a backup and
becomes something an operator asks for.
"""

from app.sep.apps.backup_mongo.config.models import (
    ADVANCED_SECTION,
    BackupConfigForm,
    RESTORE_SECTION,
    STORAGE_TUNING_SECTION,
)
from app.sep.apps.backup_mongo.views import backup_mongo_views
from app.sep.apps.framework.form_dsl import (
    derive_app_schema,
    FormLayout,
    SectionLayout,
    TASK_SECTION_LAYOUT,
)

backup_mongo_config_layout = FormLayout(
    sections=(
        TASK_SECTION_LAYOUT,
        SectionLayout(
            key="Storage",
            title="Storage",
            collapsible=True,
        ),
        SectionLayout(
            key="PITR",
            title="Point-in-Time Recovery",
            collapsible=True,
            collapsed_by_default=True,
        ),
        SectionLayout(
            key=STORAGE_TUNING_SECTION,
            title="Storage Tuning",
            advanced=True,
            description=(
                "Retry, encryption and chunking. PBM's defaults are right for most "
                "deployments."
            ),
        ),
        SectionLayout(
            key="BackupOptions",
            title="Backup Options",
            collapsible=True,
            collapsed_by_default=True,
        ),
        SectionLayout(
            key=RESTORE_SECTION,
            title="Restore Tuning",
            advanced=True,
            description=(
                "PBM's restore section: how a restore runs, cluster-wide. Restores "
                "themselves are created on the Restores tab."
            ),
        ),
        SectionLayout(
            key=ADVANCED_SECTION,
            title="Advanced",
            advanced=True,
        ),
    )
)

backup_mongo_config_schema = derive_app_schema(
    BackupConfigForm,
    backup_mongo_config_layout,
    name="backup_mongo_config",
    display_name="PBM Configuration",
    # The record this form creates one of, mid-sentence. Without the pair an app
    # serves its ``display_name`` under both keys, so every place the framework
    # composes a sentence reads "Create PBM Configuration" / "No PBM Configuration
    # yet". The siblings name theirs the same way: backup/backups, restore/restores.
    item_display_name="configuration",
    item_display_name_plural="configurations",
    description=(
        "Read and apply the Percona Backup for MongoDB (PBM) configuration for a "
        "cluster: storage, point-in-time recovery, and backup options. S3 "
        "credentials are shown as PBM reports them and are not written from here."
    ),
    capabilities=backup_mongo_views.capabilities,
    list_view=backup_mongo_views.list_view,
    detail_view=backup_mongo_views.detail_view,
)
