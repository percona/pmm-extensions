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

"""Tests for the ATW category taxonomy."""

import pytest

from app.extensions.apps.atw.categories import ATWCategory, ParentCategory

_ORIGINAL_TAXONOMY = {
    "SERVER_CRASHED_RESTART_SUCCESSFUL": (
        "Server crashed - Restart Successful",
        ParentCategory.CRASHES,
    ),
    "SERVER_CRASHED_RESTART_NOT_SUCCESSFUL": (
        "Server crashed - Restart not Successful",
        ParentCategory.CRASHES,
    ),
    "OVERALL_SLOWNESS": ("Overall Slowness", ParentCategory.PERFORMANCE_ISSUES),
    "QUERY_TUNING_OPTIMIZATION": (
        "Query Tuning / Optimization",
        ParentCategory.PERFORMANCE_ISSUES,
    ),
    "NOT_RESPONDING": ("Not Responding", ParentCategory.PERFORMANCE_ISSUES),
    "WRITES_ARE_BLOCKED": ("Writes are Blocked", ParentCategory.PERFORMANCE_ISSUES),
    "PERFORMANCE_OTHER": ("Other", ParentCategory.PERFORMANCE_ISSUES),
    "TEMPORARY_STALLS": ("Temporary Stalls", ParentCategory.PERFORMANCE_ISSUES),
    "NATIVE_ASYNC_REPLICATION": (
        "Native Asynchronous Replication",
        ParentCategory.REPLICATION_HA,
    ),
    "MULTI_SOURCE_REPLICATION": (
        "Multi-Source replication",
        ParentCategory.REPLICATION_HA,
    ),
    "GALERA": ("Galera", ParentCategory.REPLICATION_HA),
    "GROUP_REPLICATION": ("Group Replication", ParentCategory.REPLICATION_HA),
}


class TestATWCategoryTaxonomy:
    """Verify the ``ATWCategory`` and ``ParentCategory`` members."""

    @pytest.mark.parametrize(
        ("name", "label", "parent", "parent_label"),
        [
            (
                "REPLICA_SET_REPLICATION",
                "Replica Set Replication",
                ParentCategory.REPLICATION_HA,
                "Replication High / Availability",
            ),
            (
                "BACKUP_PBM",
                "Backup / PBM",
                ParentCategory.BACKUP_RECOVERY,
                "Backup and Recovery",
            ),
        ],
    )
    def test_new_leaf_label_and_parent(
        self, name: str, label: str, parent: ParentCategory, parent_label: str
    ) -> None:
        """Verify each new leaf carries its label and hangs from its parent."""
        leaf = ATWCategory[name]

        assert leaf.value == label
        assert leaf.parent is parent
        assert parent.value == parent_label

    def test_backup_parent_holds_only_backup_leaves(self) -> None:
        """Verify no unrelated leaf is grouped under the backup parent."""
        members = [
            category.name
            for category in ATWCategory
            if category.parent is ParentCategory.BACKUP_RECOVERY
        ]

        assert members == ["BACKUP_PBM"]

    @pytest.mark.parametrize(("name", "expected"), _ORIGINAL_TAXONOMY.items())
    def test_existing_members_are_unchanged(
        self, name: str, expected: tuple[str, ParentCategory]
    ) -> None:
        """Verify pre-existing members keep their label and parent."""
        category = ATWCategory[name]

        assert (category.value, category.parent) == expected

    def test_labels_are_unique(self) -> None:
        """Verify no two members share a display label."""
        # A repeated label makes the later member an alias of the earlier one,
        # which drops it from iteration and so from the listing and schema.
        assert len(ATWCategory.__members__) == len(list(ATWCategory))

    @pytest.mark.parametrize("parent", list(ParentCategory))
    def test_every_parent_has_a_leaf(self, parent: ParentCategory) -> None:
        """Verify every parent offered in the schema has at least one leaf."""
        assert any(category.parent is parent for category in ATWCategory)
