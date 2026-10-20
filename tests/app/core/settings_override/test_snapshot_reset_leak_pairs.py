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

"""Publisher/checker pairs for the autouse snapshot-reset leak guard.

``collect_ignore`` in this directory's conftest keeps these out of xdist
``worksteal`` directory collection, where a checker on a fresh worker would
see an empty snapshot and pass even when cleanup is broken.
``test_snapshot_reset.py`` runs this file in an isolated ``-n0`` child so
each publisher is immediately followed by its checker in one process,
exercising the real fixture lifecycle.
"""

from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.inventory.config import inventory_app_settings
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.report.config import health_report_settings

_PROBE = {"_sep2161_probe": True}


class TestAlertsSettingsSnapshotDoesNotLeak:
    """A published ``alerts_settings`` snapshot must not reach the next test."""

    def test_publish_leaves_a_snapshot(self) -> None:
        """Publish a probe snapshot and leave it for the following test."""
        alerts_settings._set_snapshot(_PROBE)  # ty: ignore[unresolved-attribute]
        assert (
            dict(alerts_settings.get_snapshot()) == _PROBE  # ty: ignore[unresolved-attribute]
        )

    def test_next_test_finds_snapshot_empty(self) -> None:
        """See an empty snapshot after the autouse reset ran."""
        assert (
            dict(alerts_settings.get_snapshot()) == {}  # ty: ignore[unresolved-attribute]
        )


class TestInventoryAppSettingsSnapshotDoesNotLeak:
    """A published ``inventory_app_settings`` snapshot must not reach the next test."""

    def test_publish_leaves_a_snapshot(self) -> None:
        """Publish a probe snapshot and leave it for the following test."""
        inventory_app_settings._set_snapshot(_PROBE)  # ty: ignore[unresolved-attribute]
        assert (
            dict(inventory_app_settings.get_snapshot()) == _PROBE  # ty: ignore[unresolved-attribute]
        )

    def test_next_test_finds_snapshot_empty(self) -> None:
        """See an empty snapshot after the autouse reset ran."""
        assert (
            dict(inventory_app_settings.get_snapshot()) == {}  # ty: ignore[unresolved-attribute]
        )


class TestOmInventorySettingsSnapshotDoesNotLeak:
    """A published ``om_inventory_settings`` snapshot must not reach the next test."""

    def test_publish_leaves_a_snapshot(self) -> None:
        """Publish a probe snapshot and leave it for the following test."""
        om_inventory_settings._set_snapshot(_PROBE)  # ty: ignore[unresolved-attribute]
        assert (
            dict(om_inventory_settings.get_snapshot()) == _PROBE  # ty: ignore[unresolved-attribute]
        )

    def test_next_test_finds_snapshot_empty(self) -> None:
        """See an empty snapshot after the autouse reset ran."""
        assert (
            dict(om_inventory_settings.get_snapshot()) == {}  # ty: ignore[unresolved-attribute]
        )


class TestHealthReportSettingsSnapshotDoesNotLeak:
    """A published ``health_report_settings`` snapshot must not reach the next test."""

    def test_publish_leaves_a_snapshot(self) -> None:
        """Publish a probe snapshot and leave it for the following test."""
        health_report_settings._set_snapshot(_PROBE)  # ty: ignore[unresolved-attribute]
        assert (
            dict(health_report_settings.get_snapshot()) == _PROBE  # ty: ignore[unresolved-attribute]
        )

    def test_next_test_finds_snapshot_empty(self) -> None:
        """See an empty snapshot after the autouse reset ran."""
        assert (
            dict(health_report_settings.get_snapshot()) == {}  # ty: ignore[unresolved-attribute]
        )
