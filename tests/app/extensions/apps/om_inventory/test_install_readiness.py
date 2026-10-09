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

"""Test the install-readiness facts collected for every host.

Collected for a machine with nothing installed on it yet, not only ones already
running a database — that bare-machine case is exactly what an install decision is
about, so both facts have to survive a host answering neither.
"""

from unittest.mock import patch

import pytest

from app.extensions.apps.om_bootstrap.api_routes import TriggerRunRequest
from app.extensions.apps.om_inventory.payload.probe import (
    collect_install_readiness,
    DEFAULT_DATA_PATH,
)
from tests.app.extensions.apps.om_inventory.conftest import FREE_BYTES


class TestPackageManagerDetection:
    """Identify which package manager, if any, this host installs through."""

    def test_the_first_match_wins(self) -> None:
        """Describe a host reporting more than one tool by the one tried first."""
        with patch(
            "shutil.which", side_effect=lambda binary: binary in {"apt-get", "yum"}
        ):
            facts = collect_install_readiness()

        assert facts["package_manager"] == "apt"

    def test_dnf_is_preferred_over_the_yum_symlink(self) -> None:
        """Report the tool actually present when RHEL8+ symlinks ``yum`` to ``dnf``."""
        with patch("shutil.which", side_effect=lambda binary: binary in {"dnf", "yum"}):
            facts = collect_install_readiness()

        assert facts["package_manager"] == "dnf"

    def test_zypper_is_recognised(self) -> None:
        """Recognise zypper rather than leaving SUSE hosts unclassified."""
        with patch("shutil.which", side_effect=lambda binary: binary == "zypper"):
            facts = collect_install_readiness()

        assert facts["package_manager"] == "zypper"

    def test_none_of_the_four_is_reported_as_none(self) -> None:
        """Report an unrecognised host as absent, not with a wrong guess."""
        with patch("shutil.which", return_value=None):
            facts = collect_install_readiness()

        assert facts["package_manager"] is None


class TestDataDirFreeBytes:
    """Report free space on the filesystem an install would land on."""

    @pytest.mark.parametrize(
        ("directories", "measured"),
        [
            ({"/var/lib/mongo", "/var/lib", "/var", "/"}, "/var/lib/mongo"),
            ({"/var/lib", "/var", "/"}, "/var/lib"),
        ],
    )
    def test_the_free_byte_count_is_reported_where_an_install_would_put_data(
        self, directories: set[str], measured: str
    ) -> None:
        """Measure the data directory, or its nearest ancestor before it exists.

        :param directories: The directories that exist on the host.
        :param measured: Where the free space should be measured.
        """
        usage = type("Usage", (), {"free": FREE_BYTES})()
        with (
            patch("shutil.which", return_value=None),
            patch("os.path.isdir", side_effect=directories.__contains__),
            patch("shutil.disk_usage", return_value=usage) as disk_usage,
        ):
            facts = collect_install_readiness()

        assert facts["data_dir_free_bytes"] == FREE_BYTES
        assert disk_usage.call_args.args == (measured,)

    def test_the_measured_path_is_the_install_s_default(self) -> None:
        """Measure where om_bootstrap installs when the request names no path."""
        assert TriggerRunRequest.model_fields["data_path"].default == DEFAULT_DATA_PATH

    def test_an_unreadable_filesystem_is_none_not_an_exception(self) -> None:
        """Keep the rest of the host record when a permission or mount failure hits."""
        with (
            patch("shutil.which", return_value=None),
            patch("shutil.disk_usage", side_effect=OSError("permission denied")),
        ):
            facts = collect_install_readiness()

        assert facts["data_dir_free_bytes"] is None
