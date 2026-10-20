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

"""Pin that the autouse snapshot reset covers every wired override proxy."""

import pytest

from app.core.alerts.config import alert_settings
from app.core.config import settings
from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.inventory.config import inventory_app_settings
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.report.config import health_report_settings
from app.extensions.config import extensions_settings
from app.extensions.settings_override import build_extensions_override_proxies
from app.extensions.snippets.config import snippets_settings
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.celery import build_tasks_override_proxies
from app.tasks.config import tasks_settings
from tests.app.conftest import wired_override_proxies

_PROBE = {"_sep2161_probe": True}


def test_wired_override_proxies_covers_both_registries() -> None:
    """Fail when a registry proxy is missing from the autouse reset set.

    A newly activated app that declares ``APP_OWNED_SETTINGS_CLASSES`` must
    appear here the moment it is activated; otherwise snapshots leak across
    tests in the same xdist worker again.
    """
    expected = {
        id(entry.proxy)
        for registry in (
            build_extensions_override_proxies(),
            build_tasks_override_proxies(),
        )
        for entry in registry.values()
    }
    wired_ids = {id(proxy) for proxy in wired_override_proxies()}
    assert wired_ids == expected
    # The six proxies the hand-written reset covered before SEP-2161.
    for proxy in (
        settings,
        extensions_settings,
        tasks_settings,
        snippets_settings,
        alert_settings,
        anonymizer_settings,
    ):
        assert id(proxy) in wired_ids


@pytest.mark.xdist_group("snapshot_reset_alerts")
class TestAlertsSettingsSnapshotDoesNotLeak:
    """A published ``alerts_settings`` snapshot must not reach the next test."""

    def test_publish_leaves_a_snapshot(self) -> None:
        """Publish a probe snapshot and leave it for the following test."""
        alerts_settings._set_snapshot(_PROBE)  # ty: ignore[unresolved-attribute]
        assert dict(alerts_settings.get_snapshot()) == _PROBE  # ty: ignore[unresolved-attribute]

    def test_next_test_finds_snapshot_empty(self) -> None:
        """See an empty snapshot after the autouse reset ran."""
        assert dict(alerts_settings.get_snapshot()) == {}  # ty: ignore[unresolved-attribute]


@pytest.mark.xdist_group("snapshot_reset_inventory_app")
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


@pytest.mark.xdist_group("snapshot_reset_om_inventory")
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


@pytest.mark.xdist_group("snapshot_reset_health_report")
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
