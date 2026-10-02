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

"""Wire the MongoDB Backups plugin as a declarative ``TaskExecutionApp``.

Only the ``GET /schema`` and paginated list routes are derived: the list sets
``list_filter=ListFilterConfig(roots_only=True, extra_params={"backup_type":
"pbm_config"})`` so the server-side ``parent_is_null=true`` + ``backup_type``
filters keep only the parent ``pbm_config`` tasks and preserve an accurate
paginated ``total``; its rows are built by
:func:`~app.extensions.apps.backup_mongo.deps.build_backup_mongo_api_task_response`. Every
mutation is kept per-app: all :class:`~app.extensions.apps.framework.apps.AppCapabilities`
are off and the sibling-aggregating detail (with its PBM-status tail), the cascade
create, cascade update (a custom ``PUT`` leg — the group cannot ride the derived
single-task update), delete, and execute routes ride the ``extra_routes`` router.
``capabilities.detail=False`` suppresses the greedy derived detail so the custom
``GET /{task_name}`` wins. The schema is the model-first
:data:`~app.extensions.apps.backup_mongo.schema.backup_mongo_schema` passed through
verbatim, and it carries the ``RelatedApp`` sibling-tab metadata for the restore
app. The restore subpackage is a structurally-bound child app declared via
``child_apps`` (mounted at ``/api/apps/backup_mongo/restore/`` with its own
derived router), so it is no longer mounted as a ``/restores`` sub-router here.
"""

from app.extensions.apps.backup_mongo.api_routes import (
    router as backup_mongo_custom_router,
)
from app.extensions.apps.backup_mongo.config.app import app as config_app
from app.extensions.apps.backup_mongo.deps import (
    build_backup_mongo_api_task_response,
    get_backups_task,
)
from app.extensions.apps.backup_mongo.models import (
    BackupTaskResponse,
    BackupType,
    OWNER,
)
from app.extensions.apps.backup_mongo.restore.app import app as restore_app
from app.extensions.apps.backup_mongo.schema import backup_mongo_schema
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    TaskExecutionApp,
)
from app.extensions.apps.nav_icons import NavIcon

app = TaskExecutionApp(
    name="backup_mongo",
    display_name="MongoDB Backups",
    uri_path="/backup_mongo",
    css_class="backup_mongo",
    group="backups",
    nav_order=9,
    react_route="/backups/mongodb",
    nav_icon=NavIcon.MONGO,
    owner=OWNER,
    schema=backup_mongo_schema,
    response_model=BackupTaskResponse,
    response_builder=build_backup_mongo_api_task_response,
    get_task=get_backups_task,
    list_filter=ListFilterConfig(
        status=True,
        roots_only=True,
        extra_params={"backup_type": BackupType.PBM_CONFIG.value},
    ),
    capabilities=AppCapabilities(
        create=False, detail=False, execute=False, update=False, delete=False
    ),
    extra_routes=(backup_mongo_custom_router,),
    child_apps=(config_app, restore_app),
)
