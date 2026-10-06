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

"""Register Operations Bootstrap as a ``BaseApp``.

Owns MongoDB provisioning execution: pre-flight checks, install/configure/
`rs.initiate`, and the persisted run/state-machine history for it — the write
side `om_inventory` deliberately stays out of.
`om_inventory` keeps owning general, read-only host facts; this app owns
everything specific to *running a bootstrap*, including checks that are
read-only in effect but bootstrap-specific in scope (disk space, path, OS
version).

``sidebar=False`` and ``custom_ui=False``: there is nothing to navigate to here.
The wizard lives in PMM's own UI. The consumer is PMM's ``om`` managed service,
driving this app's API to trigger and poll bootstraps: the same "consumer
drives/polls, no PMM Extensions native page" shape `om_inventory` established.

``artifact_base_dirs`` points Nomad's artifact download at
:func:`~app.extensions.apps.om_bootstrap.dispatch.step_scripts_dir` — this app is the
first to serve a *generated* artifact rather than a fixed, developer-authored
file (``dispatch.py``'s module docstring), but the serving mechanism itself
needs nothing app-specific to support that: it only needs a directory thunk,
the same as ``dipper``'s static one.
"""

from app.extensions.apps.framework.base import BaseApp
from app.extensions.apps.om_bootstrap.api_routes import router as api_router
from app.extensions.apps.om_bootstrap.dispatch import ARTIFACT_TYPE, step_scripts_dir
from app.extensions.apps.om_bootstrap.schema import om_bootstrap_schema

app = BaseApp(
    name="om_bootstrap",
    display_name="Operations Bootstrap",
    uri_path="/om_bootstrap",
    css_class="om_bootstrap",
    sidebar=False,
    api_router=api_router,
    schema=om_bootstrap_schema,
    custom_ui=False,
    artifact_base_dirs={ARTIFACT_TYPE: step_scripts_dir},
)
