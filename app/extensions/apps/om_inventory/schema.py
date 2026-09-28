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

"""Define the ``AppSchema`` for OpenManager Inventory.

The app ships no UI of its own — its consumer is PMM's OM service — so this is
the minimum the registry needs plus a run list, which is what someone diagnosing a
sweep would want if a page is ever built for it.
"""

from app.extensions.apps.framework.schema import (
    AppSchema,
    Column,
    DetailView,
    ListView,
)

om_inventory_schema = AppSchema(
    name="om_inventory",
    display_name="OpenManager Inventory",
    item_display_name="sweep",
    description=(
        "Probes MongoDB nodes over Nomad for the facts no metric carries — the "
        "installed binary version, the command line, the config file — and serves "
        "them for PMM to merge into its topology document."
    ),
    forms=[],
    list_view=ListView(
        columns=[
            Column(key="run_id", label="Run", sortable=False),
            Column(key="status", label="Status", sortable=True),
            Column(key="started_at", label="Started", sortable=True),
            Column(key="services_answered", label="Answered", sortable=False),
        ],
        default_sort="started_at",
    ),
    detail_view=DetailView(sections=[]),
)
