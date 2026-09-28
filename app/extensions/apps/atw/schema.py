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

"""Define the AppSchema for the ATW plugin."""

from app.extensions.apps.atw.categories import ATWCategory, ParentCategory
from app.extensions.apps.framework.rules import (
    all_,
    any_,
    F,
    FailRule,
    falsy,
    not_,
    truthy,
)
from app.extensions.apps.framework.schema import (
    AppSchema,
    Choice,
    ChoiceField,
    Column,
    DetailView,
    FormSection,
    ListView,
)

_PARENT_CATEGORY_CHOICES = [
    Choice(label=category.value, value=category.name) for category in ParentCategory
]
_CATEGORY_CHOICES = [
    Choice(label=category.value, value=category.name) for category in ATWCategory
]


def _atw_category_browser_fail_rules() -> list[FailRule]:
    """Declare parent/category consistency for schema-driven and API consumers."""
    rules: list[FailRule] = [
        FailRule(
            fail_when=all_(truthy("category"), falsy("parent_category")),
            error_fields=["parent_category"],
            message="parent_category is required when category is set.",
        ),
    ]
    for parent in ParentCategory:
        allowed = [cat.name for cat in ATWCategory if cat.parent == parent]
        category_matches_allowed = any_(*(F("category") == name for name in allowed))
        rules.append(
            FailRule(
                fail_when=all_(
                    F("parent_category") == parent.name,
                    truthy("category"),
                    not_(category_matches_allowed),
                ),
                error_fields=["category"],
                message=f'category must belong to "{parent.value}".',
            )
        )
    return rules


atw_schema = AppSchema(
    name="atw",
    display_name="Support diagnostics",
    item_display_name="diagnostic bundle",
    description=(
        "Browse curated troubleshooting snippets by issue category and launch"
        " execution through the snippets API flow."
    ),
    forms=[
        FormSection(
            title="Category Browser",
            fields=[
                ChoiceField(
                    name="parent_category",
                    label="Subcategory 1",
                    required=False,
                    choices=_PARENT_CATEGORY_CHOICES,
                ),
                ChoiceField(
                    name="category",
                    label="Subcategory 2",
                    required=False,
                    choices=_CATEGORY_CHOICES,
                ),
            ],
            fail_when=_atw_category_browser_fail_rules(),
        ),
    ],
    # ATW currently uses a custom React page (`AtwPage`) instead of SchemaDrivenPlugin;
    # keep `list_view` for schema/non-UI consumers and future UI convergence.
    list_view=ListView(
        columns=[
            Column(key="category_root", label="Category", sortable=True),
            Column(key="parent_category_label", label="Subcategory 1", sortable=True),
            Column(key="category_label", label="Subcategory 2", sortable=True),
            Column(key="snippet_count", label="Snippets", sortable=True),
        ],
        default_sort="category_root",
    ),
    detail_view=DetailView(sections=[]),
)
