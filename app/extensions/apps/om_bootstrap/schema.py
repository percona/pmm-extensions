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

"""Define the ``AppSchema`` for Operations Bootstrap.

The app ships no UI of its own — its consumer is PMM's ``om`` service — so this
is the minimum the registry needs plus a run list, which is what someone
diagnosing a bootstrap would want if a page is ever built for it. Placeholder
columns until the real run/state-machine model exists.
"""

from app.extensions.apps.framework.schema import (
    AppSchema,
    Column,
    DetailView,
    ListView,
)

om_bootstrap_schema = AppSchema(
    name="om_bootstrap",
    display_name="Operations Bootstrap",
    item_display_name="bootstrap run",
    description=(
        "Provisions MongoDB replica sets on inventory hosts over Nomad -- "
        "pre-flight checks, install, configuration, and rs.initiate -- and "
        "tracks each run's state so PMM can show live progress and recover "
        "after a failure."
    ),
    forms=[],
    list_view=ListView(
        columns=[Column(key="run_id", label="Run", sortable=False)],
        default_sort="run_id",
    ),
    detail_view=DetailView(sections=[]),
)
