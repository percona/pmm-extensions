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

"""Define the presentation bundle for the Archives app.

Section *membership* and *order* are declared on
:class:`~app.extensions.apps.archives.models.ArchivesCreate` (via ``Ui(section=...)``
and field-declaration order); what lives here is the part the model cannot
express: the section titles, the list columns, the detail layout, and the UI
capability flags. These feed the derived ``GET /schema`` and are carried over
from the previous hand-written ``AppSchema``.
"""

from app.extensions.apps.framework.apps import Views
from app.extensions.apps.framework.form_dsl import (
    FormLayout,
    SectionLayout,
    TASK_SECTION_LAYOUT,
)
from app.extensions.apps.framework.rules import FieldGate, truthy
from app.extensions.apps.framework.schema import (
    Capabilities,
    default_columns,
    DetailField,
    DetailSection,
    DetailView,
    EXECUTION_HOST_LABEL,
    ListView,
)

#: A delete-only run is reachable only with both destination groups gated out: the
#: create model rejects a destination alongside ``delete_data``, and hiding a section
#: unregisters its fields, so the payload omits them rather than the segmented
#: control forcing a branch choice the model then refuses.
_HIDDEN_WHEN_DELETE_ONLY = (FieldGate(when=truthy("delete_data")),)

archives_views = Views(
    layout=FormLayout(
        sections=(
            TASK_SECTION_LAYOUT,
            SectionLayout(key="Archive Type", title="Archive Type"),
            SectionLayout(key="Source", title="Source"),
            SectionLayout(
                key="Destination",
                title="Destination",
                forbidden=_HIDDEN_WHEN_DELETE_ONLY,
            ),
            SectionLayout(
                key="Destination Host",
                title="Destination Host",
                forbidden=_HIDDEN_WHEN_DELETE_ONLY,
            ),
            SectionLayout(key="Options", title="Options"),
            SectionLayout(
                key="Advanced",
                title="Advanced",
                collapsible=True,
                collapsed_by_default=True,
            ),
        )
    ),
    list_view=ListView(
        columns=default_columns(),
    ),
    detail_view=DetailView(
        sections=[
            DetailSection(
                title="Archive Configuration",
                fields=[
                    DetailField(path="data.meta.target", label=EXECUTION_HOST_LABEL),
                    DetailField(path="data.meta.config", label="Config (YAML)"),
                ],
            ),
        ],
    ),
    capabilities=Capabilities(
        chaining=True,
        alert_on_fail=True,
        scheduling=True,
        stats=True,
    ),
)
