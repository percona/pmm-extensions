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

"""Assert the supported-release table."""

import pytest

from app.extensions.apps.om_bootstrap.releases import (
    major_minor,
    supported_operating_system,
)
from app.extensions.apps.om_bootstrap.strategy import OperatingSystem

UBUNTU = OperatingSystem.UBUNTU
ROCKY = OperatingSystem.ROCKY


class TestSupportedReleases:
    """Assert each row of Percona's release lifecycle table for the two distributions."""

    @pytest.mark.parametrize(
        ("os_", "mongodb_version", "expected"),
        [
            (UBUNTU, "7.0", ["22.04", "24.04"]),
            (UBUNTU, "8.0.4", ["22.04", "24.04"]),
            (ROCKY, "7.0", ["8", "9"]),
            (ROCKY, "8.0", ["8", "9", "10"]),
            (ROCKY, "9.0", []),
            (UBUNTU, "6.0", []),
        ],
    )
    def test_lists_the_releases_a_version_installs_onto(
        self, os_: OperatingSystem, mongodb_version: str, expected: list[str]
    ) -> None:
        """Name only the releases supported for the version, patch ignored."""
        assert supported_operating_system(os_).releases_for(mongodb_version) == expected

    @pytest.mark.parametrize("os_", list(OperatingSystem))
    def test_every_operating_system_has_an_entry(self, os_: OperatingSystem) -> None:
        """Keep the table and the OperatingSystem enum in step."""
        assert supported_operating_system(os_).os is os_

    @pytest.mark.parametrize("os_", list(OperatingSystem))
    def test_lists_each_release_s_versions_oldest_first(
        self, os_: OperatingSystem
    ) -> None:
        """Keep the order PMM's install form relies on to pick the newest version."""
        for release in supported_operating_system(os_).releases:
            versions = [tuple(map(int, v.split("."))) for v in release.mongodb_versions]
            assert versions == sorted(versions), release

    def test_offers_every_version_some_release_gets(self) -> None:
        """Offer each distribution's versions once, oldest first."""
        assert supported_operating_system(ROCKY).mongodb_versions() == ["7.0", "8.0"]


@pytest.mark.parametrize(
    ("version", "expected"), [("8.0", "8.0"), ("7.0.14", "7.0"), ("10.1.2", "10.1")]
)
def test_major_minor_drops_the_patch(version: str, expected: str) -> None:
    """Cut a version to the part that selects its builds."""
    assert major_minor(version) == expected
