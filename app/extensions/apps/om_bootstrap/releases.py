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

"""Name the OS releases a run installs each MongoDB version onto.

The table follows Percona's release lifecycle for MongoDB, not what Percona's
repositories serve: they keep old builds for releases Percona no longer
supports, so ``percona-release`` succeeds there and installs an unsupported
build. PMM reads the table from :func:`~app.extensions.apps.om_bootstrap.api_routes.list_supported_releases`
instead of keeping its own.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.extensions.apps.om_bootstrap.strategy import OperatingSystem

__all__ = [
    "SUPPORTED_OPERATING_SYSTEMS",
    "ReleaseMatch",
    "SupportedOperatingSystem",
    "SupportedRelease",
    "major_minor",
    "supported_operating_system",
]


class ReleaseMatch(StrEnum):
    """Say which part of ``/etc/os-release``'s ``VERSION_ID`` names a release."""

    #: The whole value, as Ubuntu's ``22.04``.
    EXACT = "exact"
    #: The part before the first dot, so Rocky Linux ``9.4`` is release ``9``.
    MAJOR = "major"


class SupportedRelease(BaseModel):
    """Hold one OS release and the MongoDB versions a run installs onto it.

    :param version: The release, as :class:`ReleaseMatch` reads it from ``VERSION_ID``.
    :param mongodb_versions: ``major.minor`` MongoDB versions, oldest first.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    mongodb_versions: tuple[str, ...]


class SupportedOperatingSystem(BaseModel):
    """Hold one distribution a run installs onto, with its supported releases.

    :param os: The distribution, as ``/etc/os-release``'s ``ID``.
    :param name: The distribution's name for people, e.g. ``Rocky Linux``.
    :param release_match: How a node's ``VERSION_ID`` names one of ``releases``.
    :param releases: The supported releases, oldest first.
    """

    model_config = ConfigDict(frozen=True)

    os: OperatingSystem
    name: str
    release_match: ReleaseMatch
    releases: tuple[SupportedRelease, ...]

    def releases_for(self, mongodb_version: str) -> list[str]:
        """Return the releases a run installs ``mongodb_version`` onto.

        :param mongodb_version: ``major.minor`` or ``major.minor.patch``.
        :return: The releases, oldest first; empty when none is supported.
        """
        version = major_minor(mongodb_version)
        return [
            supported.version
            for supported in self.releases
            if version in supported.mongodb_versions
        ]

    def mongodb_versions(self) -> list[str]:
        """Return every MongoDB version some release of this distribution gets.

        :return: ``major.minor`` versions, oldest first.
        """
        versions = {
            version
            for supported in self.releases
            for version in supported.mongodb_versions
        }
        return sorted(versions, key=lambda version: tuple(map(int, version.split("."))))


SUPPORTED_OPERATING_SYSTEMS: tuple[SupportedOperatingSystem, ...] = (
    SupportedOperatingSystem(
        os=OperatingSystem.UBUNTU,
        name="Ubuntu",
        release_match=ReleaseMatch.EXACT,
        releases=(
            SupportedRelease(version="22.04", mongodb_versions=("7.0", "8.0")),
            SupportedRelease(version="24.04", mongodb_versions=("7.0", "8.0")),
        ),
    ),
    SupportedOperatingSystem(
        os=OperatingSystem.ROCKY,
        name="Rocky Linux",
        release_match=ReleaseMatch.MAJOR,
        releases=(
            SupportedRelease(version="8", mongodb_versions=("7.0", "8.0")),
            SupportedRelease(version="9", mongodb_versions=("7.0", "8.0")),
            SupportedRelease(version="10", mongodb_versions=("8.0",)),
        ),
    ),
)


def supported_operating_system(os_: OperatingSystem) -> SupportedOperatingSystem:
    """Return ``os_``'s entry in :data:`SUPPORTED_OPERATING_SYSTEMS`.

    :param os_: A distribution.
    :raises ValueError: If ``os_`` has no entry.
    :return: Its supported releases.
    """
    for supported in SUPPORTED_OPERATING_SYSTEMS:
        if supported.os is os_:
            return supported
    raise ValueError(f"unsupported OperatingSystem: {os_!r}")


def major_minor(mongodb_version: str) -> str:
    """Cut a MongoDB version to ``major.minor``, which is what selects its builds.

    :param mongodb_version: ``"8.0"`` or ``"8.0.4"``.
    :return: ``"8.0"``.
    """
    return ".".join(mongodb_version.split(".")[:2])
