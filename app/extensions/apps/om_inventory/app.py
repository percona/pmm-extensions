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

"""Register Operations Inventory as a ``BaseApp``.

The first of the PMM Extensions apps that exist to do work on Nomad clients. This one only
reads — it runs a payload on each database host and collects facts no metric
carries — but it establishes the shape the rest will follow: a periodic sweep, a
run history, and an API a consumer polls rather than drives.

``sidebar=False`` and no ``react_route``: there is nothing to navigate to. The
consumer is PMM's OM service polling ``GET /hosts`` and ``GET /services``, and the run
history is for diagnosis through the API. Registering it as an app is still what earns
the Celery module inclusion, the app-drain ownership tag, and the switch on the Apps
page.
"""

from typing import cast

from app.core.celery.models import IntervalSchedule
from app.extensions.apps.framework.base import AppPeriodicTask, BaseApp
from app.extensions.apps.nav_icons import NavIcon
from app.extensions.apps.om_inventory.api_routes import router as api_router
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.schema import om_inventory_schema


def _om_inventory_periodic_tasks() -> list[AppPeriodicTask]:
    """Contribute the periodic sweep while it is enabled and configured.

    A callable rather than a list literal because ``ENABLED``/``SCHEDULE`` may make
    the sweep unregistered, so the contribution is variable-length and a literal
    would commit to a fixed set at ``BaseApp(...)`` construction.

    That the checks live *inside* the thunk is what makes both settings genuinely hot
    in both directions: turning the sweep back on over ``PATCH /config`` re-registers
    the task on the next registry rebuild, where a literal guarded by an import-time
    ``if`` would have decided once and stayed decided.

    The sweep is what keeps the estate current at all: no endpoint probes, so with no
    schedule a row only changes when someone posts to ``/runs``.

    :return: The sweep contribution, or an empty list when disabled or unconfigured.
    """
    if not om_inventory_settings.ENABLED or om_inventory_settings.SCHEDULE is None:
        return []
    return [
        AppPeriodicTask(
            name="extensions__run_om_probe",
            task="run_om_probe",
            schedule=lambda: cast("IntervalSchedule", om_inventory_settings.SCHEDULE),
        ),
    ]


app = BaseApp(
    name="om_inventory",
    display_name="Operations Inventory",
    uri_path="/om_inventory",
    css_class="om_inventory",
    group="diagnostics",
    sidebar=False,
    nav_icon=NavIcon.MONGO,
    api_router=api_router,
    schema=om_inventory_schema,
    custom_ui=False,
    periodic_task_schedules=_om_inventory_periodic_tasks,
)
