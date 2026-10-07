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

"""Derive the AppSchema for the Alters plugin model-first.

The schema is derived from the model-first
:class:`~app.extensions.apps.alters.models.AltersCreate` plus the
:data:`~app.extensions.apps.alters.views.alters_views` presentation bundle. The
``derived`` and ``predecessors`` blocks carry the ``-dry-run`` derived sibling and
the ``-pre-checks`` chained predecessor into ``GET /schema``; the cascade create
route reuses the same specs to POST the task group.

The schema ``display_name`` is ``"Alters"`` — distinct from the navigation label
``"Schema Change"`` carried on :data:`~app.extensions.apps.alters.app.app`.
"""

from app.extensions.apps.alters.models import AltersCreate
from app.extensions.apps.alters.views import alters_views
from app.extensions.apps.framework.form_dsl import derive_app_schema
from app.extensions.apps.framework.schema import ChainedPredecessor, DerivedTask

ALTERS_DERIVED = [
    DerivedTask(
        name_suffix="-dry-run",
        arg_substitutions={"--execute": "--dry-run"},
    ),
]

ALTERS_PREDECESSORS = [
    ChainedPredecessor(
        name_suffix="-pre-checks",
        on_failure="halt",
    ),
]

alters_schema = derive_app_schema(
    AltersCreate,
    alters_views.layout,
    name="alters",
    display_name="Alters",
    item_display_name="schema change",
    description=(
        "Run pt-online-schema-change to perform online MySQL schema modifications."
    ),
    capabilities=alters_views.capabilities,
    list_view=alters_views.list_view,
    detail_view=alters_views.detail_view,
    derived=ALTERS_DERIVED,
    predecessors=ALTERS_PREDECESSORS,
)
