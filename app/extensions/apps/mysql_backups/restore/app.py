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

"""Wire the MySQL Restores subpackage as a parent-bound ``TaskExecutionApp``.

The registry discovers this app through ``mysql_backups``'s ``child_apps`` rather
than a ``settings.yaml`` entry, so it is mounted and toggled exactly with its
parent under the explicit scoped key ``mysql_backups/restore``, serving its
derived JSON router at ``/api/apps/mysql_backups/restore/``. Because a restore
legitimately has no destination service for
XtraBackup / Binlog and tolerates a 404 fallback, the create payload is built by
the ``payload_builder`` escape hatch
(:func:`~app.extensions.apps.mysql_backups.restore.deps.build_restore_payload`)
rather than the framework's auto-resolve three-phase path; the model-first
:class:`~app.extensions.apps.mysql_backups.restore.models.RestoreCreate` still drives
``GET /schema`` and the create form. ``service_id`` lives in the Task section
so the restore ``backup_source`` ``RemoteChoices`` cascade is available for every
backup type (not only Mydumper).
"""

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.core.pagination.deps import make_pagination_dep
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    TaskExecutionApp,
)
from app.extensions.apps.mysql_backups.restore.deps import (
    build_restore_api_task_response,
    build_restore_payload,
)
from app.extensions.apps.mysql_backups.restore.models import (
    OWNER,
    RestoreCreate,
    RestoresResponse,
)
from app.extensions.apps.mysql_backups.restore.views import restore_views

app = TaskExecutionApp(
    key="mysql_backups/restore",
    name="mysql_backups_restores",
    display_name="MySQL Restores",
    item_display_name="restore",
    uri_path="/mysql_backups/restores",
    css_class="mysql_backups",
    group="backups",
    nav_order=8,
    sidebar=False,
    parent_key="mysql_backups",
    description="Restore MySQL hosts from XtraBackup, Mydumper, and Binlog backups.",
    owner=OWNER,
    create_model=RestoreCreate,
    response_model=RestoresResponse,
    views=restore_views,
    payload_builder=build_restore_payload,
    response_builder=build_restore_api_task_response,
    pagination=make_pagination_dep(max_limit=DEFAULT_PAGINATION_LIMIT),
    capabilities=AppCapabilities(update=True, delete=True),
    list_filter=ListFilterConfig(status=True),
)
