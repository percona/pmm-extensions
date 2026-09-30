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

"""Wire the Checksums plugin as a declarative ``TaskExecutionApp``.

This definition replaces the hand-written JSON API router and schema: the
registry discovers the exported ``app`` and mounts its derived router, which
serves the byte-identical schema, list, detail, create, update, execute, and
delete surfaces. Create and update are derived from the model-first
:class:`~app.extensions.apps.checksums.models.ChecksumsForm` through the
``pt-table-checksum`` spec builder; the derived PUT and DELETE carry the
framework's default protected-task + running-conflict guards.
"""

from app.extensions.apps.checksums.deps import build_checksums_payload
from app.extensions.apps.checksums.models import ChecksumsForm, OWNER
from app.extensions.apps.checksums.views import checksums_views
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    TaskExecutionApp,
)
from app.extensions.apps.nav_icons import NavIcon
from app.inventory.models import ServiceTypeEnum

app = TaskExecutionApp(
    name="checksums",
    display_name="Checksums",
    item_display_name="checksum",
    uri_path="/checksums",
    css_class="checksums",
    nav_order=7,
    nav_icon=NavIcon.CHECK_CIRCLE,
    description="Run pt-table-checksum to verify MySQL replication consistency.",
    owner=OWNER,
    create_model=ChecksumsForm,
    views=checksums_views,
    payload_builder=build_checksums_payload,
    capabilities=AppCapabilities(update=True, delete=True),
    service_type=ServiceTypeEnum.MYSQL,
    list_filter=ListFilterConfig(status=True, service_type=True),
)
