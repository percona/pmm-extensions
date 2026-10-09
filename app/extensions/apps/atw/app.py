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

"""Wire the ATW plugin as a declarative ``BaseApp``.

Register ``atw`` ("Support diagnostics") through the registry's definition
path, carrying the existing JSON ``api_router`` (the category listing + schema)
and ``atw_schema`` so the conformance suite reads the schema from the definition.
``atw`` owns no task and derives no execute route -- execution is delegated to the
snippets ``ScriptSource`` surface, consumed by the React ``AtwPage``. It nests
under the shared ``snippets`` nav group alongside the snippets app. The staged-
bundle purge beat schedule is contributed via ``periodic_task_schedules``.
"""

from typing import cast

from app.core.celery.models import IntervalSchedule
from app.extensions.apps.atw.api_routes import router as api_router
from app.extensions.apps.atw.config import atw_settings
from app.extensions.apps.atw.schema import atw_schema
from app.extensions.apps.framework.base import AppPeriodicTask, BaseApp
from app.extensions.apps.nav_icons import NavIcon


def atw_periodic_tasks() -> list[AppPeriodicTask]:
    """Contribute the bundle-purge and outcome-reconcile sweeps that are configured.

    Either interval may be ``None`` to unregister its own sweep, so this is kept as
    a callable: the contribution is variable-length (0 to 2) and a plain list literal
    would commit to a fixed set at ``BaseApp(...)`` construction. The two are guarded
    independently — disabling the purge must not silently stop reconciliation, which
    is the only thing keeping the incident list's failed count honest.

    :return: One contrib per configured sweep, or an empty list when both are off.
    """
    contributions: list[AppPeriodicTask] = []
    if atw_settings.cleanup_interval is not None:
        contributions.append(
            AppPeriodicTask(
                name="extensions__purge_atw_bundles",
                task="purge_atw_bundles",
                schedule=lambda: cast(
                    "IntervalSchedule", atw_settings.cleanup_interval
                ),
            )
        )
    if atw_settings.reconcile_interval is not None:
        contributions.append(
            AppPeriodicTask(
                name="extensions__reconcile_atw_executions",
                task="reconcile_atw_executions",
                schedule=lambda: cast(
                    "IntervalSchedule", atw_settings.reconcile_interval
                ),
            )
        )
    return contributions


app = BaseApp(
    name="atw",
    display_name="Support diagnostics",
    uri_path="/atw",
    css_class="atw",
    custom_ui=True,
    group="diagnostics",
    nav_order=3,
    react_route="/atw",
    nav_icon=NavIcon.SUPPORT_AGENT,
    api_router=api_router,
    schema=atw_schema,
    periodic_task_schedules=atw_periodic_tasks,
    uses_task_data=True,
)
