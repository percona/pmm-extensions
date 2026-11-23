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

"""Test the capability probe: its script, its report parser, and the derived facts."""

import pytest

from app.extensions.apps.shared.om.capability import (
    build_capability_script,
    capability_facts,
    parse_capability_report,
)
from app.extensions.apps.shared.om.step_contract import AgentElevation


class TestBuildCapabilityScript:
    """Build the probe script for a member's paths."""

    def test_reads_no_new_privileges_before_sudo(self) -> None:
        """Read ``/proc/self/status`` so a ``NoNewPrivileges`` unit is not misread."""
        script = build_capability_script(
            config_path="/etc/mongod.conf", keyfile_path="/etc/mongo/keyfile"
        )
        assert "/proc/self/status" in script
        assert script.index("NoNewPrivs") < script.index("sudo")

    def test_quotes_the_paths_against_injection(self) -> None:
        """Quote a path so a crafted path cannot break out of the test command."""
        script = build_capability_script(
            config_path="/etc/m.conf; rm -rf /", keyfile_path="/k f"
        )
        assert "'/etc/m.conf; rm -rf /'" in script
        assert "'/k f'" in script


class TestParseCapabilityReport:
    """Read the probe's key-value output."""

    def test_keeps_pairs_and_ignores_noise(self) -> None:
        """Keep ``key=value`` lines, drop a line with no ``=``, and take the last."""
        report = parse_capability_report("euid=0\ndiagnostic line\neuid=1000\n")
        assert report == {"euid": "1000"}


class TestElevation:
    """Derive ``agent.elevation`` from the probe report."""

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("nnp=1\neuid=0\nsudo_all=1\n", AgentElevation.BLOCKED_NO_NEW_PRIVILEGES),
            ("nnp=0\neuid=0\n", AgentElevation.ROOT),
            ("nnp=0\neuid=1000\nsudo_all=1\nsudo_any=1\n", AgentElevation.SUDO_ALL),
            ("nnp=0\neuid=1000\nsudo_all=0\nsudo_any=1\n", AgentElevation.SUDO_LIMITED),
            ("nnp=0\neuid=1000\nsudo_all=0\nsudo_any=0\n", AgentElevation.NONE),
        ],
        ids=["nnp-beats-root", "root", "sudo-all", "sudo-limited", "none"],
    )
    def test_elevation_states(self, output: str, expected: AgentElevation) -> None:
        """Map the probe signals to the measured elevation, ``NoNewPrivileges`` first.

        :param output: The probe stdout.
        :param expected: The elevation it measures.
        """
        assert capability_facts(output)["agent.elevation"] == expected.value


class TestDerivedCapabilities:
    """Derive the host and member capability facts from elevation and file tests."""

    def test_root_drives_units_and_packages_directly(self) -> None:
        """Report unit control and package install direct for root."""
        facts = capability_facts("nnp=0\neuid=0\n")
        assert facts["host.unit_control"] == "direct"
        assert facts["host.package_install"] == "direct"

    def test_sudo_limited_elevates_only_the_granted_command(self) -> None:
        """Elevate unit control via a ``systemctl`` grant, leave package install out."""
        facts = capability_facts(
            "nnp=0\neuid=1000\nsudo_any=1\nsudo_systemctl=1\nsudo_package=0\n"
        )
        assert facts["host.unit_control"] == "via_elevation"
        assert facts["host.package_install"] == "no"

    def test_blocked_no_new_privileges_grants_nothing_elevated(self) -> None:
        """Refuse elevation under ``NoNewPrivileges`` even if a grant is reported."""
        facts = capability_facts("nnp=1\neuid=1000\nsudo_systemctl=1\n")
        assert facts["host.unit_control"] == "no"

    def test_file_tests_drive_direct_read_and_write(self) -> None:
        """Report config write and keyFile read direct from the agent's own test."""
        facts = capability_facts(
            "nnp=0\neuid=1000\ndirect_config_writable=1\ndirect_keyfile_readable=0\n"
        )
        assert facts["host.config_writable"] == "direct"
        assert facts["member.keyfile_readable"] == "no"
