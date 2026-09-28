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

"""Wire the MySQL Backups plugin as a declarative ``TaskExecutionApp``.

This definition replaces the hand-written JSON API router and schema: the
registry discovers the exported ``app`` and mounts its derived router, which
serves the byte-identical schema, list, detail, create, update, execute, and
delete surfaces. Create and update are derived from the model-first
:class:`~app.extensions.apps.mysql_backups.forms.BackupCreate` through the
``run-python`` spec builder, with the ``backup_type``-aware
:func:`~app.extensions.apps.mysql_backups.deps.build_mysql_backups_api_task_response`
stamping ``backup_type`` / ``hostname`` on list, detail, and create; delete is
the framework's plain default. The display fields (``display_name`` / ``uri_path``
/ ``css_class``) are supplied here because ``settings.yaml`` no longer carries
them. The restore subpackage is a structurally-bound child app
declared via ``child_apps`` (key ``mysql_backups/restore``), so it is mounted and
toggled with this parent rather than as an independent ``settings.yaml`` entry or
a sub-router here.
"""

from app.core.pagination import DEFAULT_PAGINATION_LIMIT
from app.core.pagination.deps import make_pagination_dep
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    TaskExecutionApp,
)
from app.extensions.apps.framework.schema import RelatedApp
from app.extensions.apps.mysql_backups.api_routes import router as catalog_router
from app.extensions.apps.mysql_backups.deps import (
    build_mysql_backups_api_task_response,
    get_mysql_backups_task,
)
from app.extensions.apps.mysql_backups.forms import (
    BackupCreate,
    BackupTaskResponse,
    OWNER,
)
from app.extensions.apps.mysql_backups.recorder import RUN_RESULT_RECORDER
from app.extensions.apps.mysql_backups.restore.app import app as restore_app
from app.extensions.apps.mysql_backups.spec import build_backup_spec
from app.extensions.apps.mysql_backups.views import mysql_backups_views
from app.extensions.apps.nav_icons import NavIcon

app = TaskExecutionApp(
    name="mysql_backups",
    display_name="MySQL Backups",
    item_display_name="backup",
    uri_path="/mysql_backups",
    css_class="mysql_backups",
    group="backups",
    nav_order=8,
    nav_icon=NavIcon.MYSQL,
    description="Run XtraBackup, Mydumper, and Binlog backups against MySQL hosts.",
    owner=OWNER,
    get_task=get_mysql_backups_task,
    create_model=BackupCreate,
    response_model=BackupTaskResponse,
    views=mysql_backups_views,
    task_spec_builder=build_backup_spec,
    run_result_recorder=RUN_RESULT_RECORDER,
    response_builder=build_mysql_backups_api_task_response,
    pagination=make_pagination_dep(max_limit=DEFAULT_PAGINATION_LIMIT),
    capabilities=AppCapabilities(update=True, delete=True),
    list_filter=ListFilterConfig(status=True),
    related_apps=(
        RelatedApp(
            app_key="mysql_backups/restore",
            label="Restores",
            route_segment="restores",
        ),
    ),
    extra_routes=(catalog_router,),
    child_apps=(restore_app,),
)
