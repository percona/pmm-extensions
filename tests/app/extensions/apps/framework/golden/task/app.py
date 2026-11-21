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

"""Wire the Golden Task plugin as a declarative ``TaskExecutionApp``.

The registry discovers the exported ``app`` and mounts its derived router, which
serves the schema, list, detail, create, update, execute, and delete surfaces.
Set ``group`` / ``nav_order`` to place the app in the sidebar, or
``service_type`` plus ``list_filter=ListFilterConfig(...)`` to expose list filters.
"""

from app.extensions.apps.golden_task.models import GoldenTaskForm, OWNER
from app.extensions.apps.golden_task.spec import build_golden_task_spec
from app.extensions.apps.golden_task.views import golden_task_views
from app.extensions.apps.framework.apps import AppCapabilities, TaskExecutionApp

app = TaskExecutionApp(
    name="golden_task",
    display_name="Golden Task",
    item_display_name="Golden Task task",
    item_display_name_plural="Golden Task tasks",
    uri_path="/golden_task",
    description="TODO: describe what the Golden Task task does.",
    owner=OWNER,
    create_model=GoldenTaskForm,
    views=golden_task_views,
    task_spec_builder=build_golden_task_spec,
    capabilities=AppCapabilities(update=True, delete=True),
)
