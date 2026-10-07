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

"""Wire the PostgreSQL Backups plugin as a declarative ``TaskExecutionApp``.

This definition replaces the hand-written JSON API router and schema: the
registry discovers the exported ``app`` and mounts its derived router, which
serves the schema, list, detail, create, execute, and delete surfaces, plus a
net-new ``PUT /{task_name}`` update route. The derived ``GET /schema`` matches
the legacy one except that it now surfaces the stanza name validation pattern,
which the legacy hand-written schema enforced server-side only. Create and update are
derived from the model-first
:class:`~app.extensions.apps.backup_pg.models.BackupPgForm` through the ``run-python``
pgBackRest spec builder, with the YAML-config-derived
:func:`~app.extensions.apps.backup_pg.deps.build_backup_pg_api_task_response` /
:func:`~app.extensions.apps.backup_pg.deps.build_backup_pg_api_detail_response`
stamping ``hostname`` / ``backup_type`` (and ``host`` / ``port`` on detail) so
the list, detail, create, and update bodies stay byte-identical. The derived PUT
and DELETE carry the framework's default protected-task + running-conflict
guards; only the body-reading create running-conflict guard rides explicitly via
``create_extra_deps``.
"""

from app.extensions.apps.backup_pg.deps import (
    build_backup_pg_api_detail_response,
    build_backup_pg_api_task_response,
    HasNoConflictedRunningTasksOnCreate,
)
from app.extensions.apps.backup_pg.models import (
    BackupPgForm,
    BackupTaskDetailResponse,
    BackupTaskResponse,
    OWNER,
)
from app.extensions.apps.backup_pg.spec import build_backup_pg_spec
from app.extensions.apps.backup_pg.views import backup_pg_views
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    TaskExecutionApp,
)
from app.extensions.apps.nav_icons import NavIcon
from app.inventory.models import ServiceTypeEnum

app = TaskExecutionApp(
    name="backup_pg",
    display_name="PostgreSQL Backups",
    item_display_name="backup",
    uri_path="/backup_pg",
    css_class="backup_pg",
    group="backups",
    nav_order=10,
    react_route="/backups/postgresql",
    nav_icon=NavIcon.POSTGRESQL,
    description=(
        "Configure pgBackRest-based PostgreSQL backups and run incremental "
        "or differential backup tasks against a Percona-managed Postgres host."
    ),
    owner=OWNER,
    create_model=BackupPgForm,
    response_model=BackupTaskResponse,
    views=backup_pg_views,
    task_spec_builder=build_backup_pg_spec,
    capabilities=AppCapabilities(update=True, delete=True),
    service_type=ServiceTypeEnum.POSTGRESQL,
    list_filter=ListFilterConfig(status=True),
    response_builder=build_backup_pg_api_task_response,
    detail_response_builder=build_backup_pg_api_detail_response,
    detail_response_model=BackupTaskDetailResponse,
    create_extra_deps=(HasNoConflictedRunningTasksOnCreate,),
)
