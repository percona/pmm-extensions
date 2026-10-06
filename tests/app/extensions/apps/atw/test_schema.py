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

"""Tests for the ATW schema's category browser consistency rules."""

from types import SimpleNamespace

import pytest

from app.extensions.apps.atw.schema import _atw_category_browser_fail_rules
from app.extensions.apps.framework.rules import FailRule


def _fired_rules(parent_category: str | None, category: str | None) -> list[FailRule]:
    """Return the category-browser fail rules a form submission would trip.

    :param parent_category: The submitted parent category member name.
    :param category: The submitted leaf category member name.
    :return: The rules whose ``fail_when`` predicate evaluates true.
    """
    form = SimpleNamespace(parent_category=parent_category, category=category)
    return [
        rule
        for rule in _atw_category_browser_fail_rules()
        if rule.fail_when.evaluate(form)
    ]


class TestCategoryBrowserFailRules:
    """Verify the parent/category consistency rules of the category browser."""

    @pytest.mark.parametrize(
        ("parent_category", "category"),
        [
            ("BACKUP_RECOVERY", "BACKUP_PBM"),
            ("REPLICATION_HA", "REPLICA_SET_REPLICATION"),
        ],
    )
    def test_matching_parent_and_leaf_pass(
        self, parent_category: str, category: str
    ) -> None:
        """Verify a leaf submitted with its own parent trips no rule."""
        assert _fired_rules(parent_category, category) == []

    @pytest.mark.parametrize(
        ("parent_category", "category", "parent_label"),
        [
            ("BACKUP_RECOVERY", "GALERA", "Backup and Recovery"),
            ("REPLICATION_HA", "BACKUP_PBM", "Replication High / Availability"),
            ("CRASHES", "REPLICA_SET_REPLICATION", "Crashes"),
            ("PERFORMANCE_ISSUES", "BACKUP_PBM", "Performance Issues"),
        ],
    )
    def test_mismatched_parent_and_leaf_fail_on_category(
        self, parent_category: str, category: str, parent_label: str
    ) -> None:
        """Verify a leaf submitted under a foreign parent is rejected on ``category``."""
        fired = _fired_rules(parent_category, category)

        assert len(fired) == 1
        assert fired[0].error_fields == ["category"]
        assert fired[0].message == f'category must belong to "{parent_label}".'

    def test_leaf_without_parent_requires_parent(self) -> None:
        """Verify a new leaf submitted with no parent asks for the parent."""
        fired = _fired_rules(None, "BACKUP_PBM")

        assert len(fired) == 1
        assert fired[0].error_fields == ["parent_category"]
