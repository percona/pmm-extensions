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
"""Tests for the pcs-collect-pmm-mysql.py payload script."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.extensions.snippets.schema import field_for

SCRIPT = (
    Path(__file__).parents[5]
    / "app/extensions/apps/dipper/payloads/pcs-collect-pmm-mysql.py"
)
ARGPARSE_ERROR_EXIT_CODE = 2


class TestPcsCollectPmmMysqlInsecureFlag:
    """Tests for the --insecure flag of pcs-collect-pmm-mysql.py."""

    @pytest.fixture(scope="class")
    def help_output(self):
        """Run the script with --help and capture the output.

        Pin ``COLUMNS`` so argparse wraps the help text at a fixed width: under
        ``pytest -n`` (no tty) the inherited terminal width can be narrow enough
        to split ``self-signed`` across lines and break a substring assertion.
        """
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "COLUMNS": "80"},
        )

    def test_insecure_flag_in_help(self, help_output):
        """--insecure must appear in --help output."""
        assert help_output.returncode == 0
        assert "--insecure" in help_output.stdout

    def test_insecure_flag_description_in_help(self, help_output):
        """--insecure help text must mention self-signed certs."""
        assert "self-signed" in help_output.stdout

    def test_yaml_frontmatter_contains_insecure_parameter(self):
        """YAML frontmatter must expose insecure so it surfaces in the dipper UI."""
        content = SCRIPT.read_text()
        frontmatter_end = content.index("# ---", content.index("# ---") + 1)
        frontmatter = content[:frontmatter_end]
        assert "name: insecure" in frontmatter
        insecure_index = frontmatter.index("name: insecure")
        next_parameter_index = frontmatter.find(
            "name: ", insecure_index + len("name: insecure")
        )
        if next_parameter_index == -1:
            insecure_block = frontmatter[insecure_index:]
        else:
            insecure_block = frontmatter[insecure_index:next_parameter_index]
        assert "type: bool" in insecure_block

    def test_no_hardcoded_verify_false(self):
        """Regression: no requests call site must use a literal verify=False."""
        assert "verify=False" not in SCRIPT.read_text()


def _frontmatter() -> str:
    """Return the YAML frontmatter block from the collector script."""
    content = SCRIPT.read_text()
    frontmatter_end = content.index("# ---", content.index("# ---") + 1)
    return content[:frontmatter_end]


def _parameter_block(frontmatter: str, parameter_name: str) -> str:
    """Return the frontmatter slice for a single parameter declaration."""
    marker = f"name: {parameter_name}"
    start = frontmatter.index(marker)
    next_parameter_index = frontmatter.find("name: ", start + len(marker))
    if next_parameter_index == -1:
        return frontmatter[start:]
    return frontmatter[start:next_parameter_index]


class TestPcsCollectPmmMysqlHaDbaasConsolidation:
    """Tests for consolidated HA and DBaaS flags in pcs-collect-pmm-mysql.py."""

    @pytest.fixture(scope="class")
    def help_output(self):
        """Run the script with --help and capture the output."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_shows_consolidated_flags(self, help_output):
        """--ha, --ha-name, and --dbaas must appear in --help output."""
        assert help_output.returncode == 0
        stdout = help_output.stdout
        assert "--ha" in stdout
        assert "--ha-name" in stdout
        assert "--dbaas" in stdout
        assert "--pxc" not in stdout
        assert "--gr" not in stdout
        assert "--async" not in stdout
        assert "--rds" not in stdout
        assert "--aurora" not in stdout

    def test_yaml_frontmatter_ha_dbaas_parameters(self):
        """YAML frontmatter must expose ha, ha_name, and dbaas so they surface in the dipper UI."""
        frontmatter = _frontmatter()
        for legacy_name in ("pxc", "gr", "async", "rds", "aurora"):
            assert f"name: {legacy_name}" not in frontmatter

        ha_block = _parameter_block(frontmatter, "ha")
        assert "group: High Availability" in ha_block
        assert "choices:" in ha_block
        assert "value: pxc" in ha_block
        assert "value: gr" in ha_block
        assert "value: async" in ha_block
        assert 'arg_format: "--ha ${value}"' in ha_block

        ha_name_block = _parameter_block(frontmatter, "ha_name")
        assert "group: High Availability" in ha_name_block
        assert 'arg_format: "--ha-name ${value}"' in ha_name_block

        dbaas_block = _parameter_block(frontmatter, "dbaas")
        assert "group: DBaaS Options" in dbaas_block
        assert "value: rds" in dbaas_block
        assert "value: aurora" in dbaas_block
        assert 'arg_format: "--dbaas ${value}"' in dbaas_block

    @pytest.mark.parametrize("ha_mode", ["pxc", "gr"])
    def test_ha_mode_requires_name(self, ha_mode):
        """--ha-name must be required when --ha is pxc or gr."""
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "https://u:p@localhost",
                "--list",
                "--ha",
                ha_mode,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == ARGPARSE_ERROR_EXIT_CODE
        assert "ha-name" in result.stderr


class TestPcsCollectPmmMysqlApikeyHidden:
    """Tests for hiding the apikey field from the PMM form."""

    def test_frontmatter_marks_apikey_hidden(self):
        """The apikey parameter declares ``hidden: true`` in the YAML frontmatter."""
        block = _parameter_block(_frontmatter(), "apikey")
        assert "hidden: true" in block

    def test_other_parameters_are_not_hidden(self):
        """Only apikey is hidden; sibling params do not declare ``hidden``."""
        frontmatter = _frontmatter()
        for name in ("pmmserver", "node", "service", "list"):
            assert "hidden: true" not in _parameter_block(frontmatter, name)

    @pytest.mark.asyncio
    async def test_validated_apikey_parameter_is_hidden(self):
        """The parsed snippet parameter for apikey carries ``hidden=True``."""
        from app.extensions.snippets.models.snippet import BaseSnippet  # noqa: PLC0415

        meta = await BaseSnippet.get_meta_by_path(SCRIPT)
        snippet = BaseSnippet(
            filename="pcs-collect-pmm-mysql.py",
            size=1,
            md5_digest="a" * 32,
            meta=meta,
        )
        validated = snippet.validated_parameters
        assert validated.errors == []
        by_name = {p.name: p for p in validated.parameters}
        assert by_name["apikey"].hidden is True
        assert by_name["pmmserver"].hidden is False


class TestPcsCollectPmmMysqlListVisibility:
    """Hide date/HA/DBaaS fields in list-services mode."""

    _GATED_PARAMETERS = ("start", "end", "ha", "ha_name", "dbaas")

    def test_frontmatter_marks_fields_hidden_when_list(self):
        """start, end, ha, ha_name, and dbaas declare visible_when_not: list."""
        frontmatter = _frontmatter()
        for name in self._GATED_PARAMETERS:
            block = _parameter_block(frontmatter, name)
            assert "visible_when_not: list" in block

    @pytest.mark.asyncio
    async def test_schema_forbids_gated_fields_when_list(self):
        """The synthesised schema hides the gated fields when ``list`` is truthy."""
        from app.extensions.snippets.models.snippet import BaseSnippet  # noqa: PLC0415

        meta = await BaseSnippet.get_meta_by_path(SCRIPT)
        snippet = BaseSnippet(
            filename="pcs-collect-pmm-mysql.py",
            size=1,
            md5_digest="a" * 32,
            meta=meta,
        )
        validated = snippet.validated_parameters
        assert validated.errors == []
        by_name = {p.name: field_for(p) for p in validated.parameters}
        for name in self._GATED_PARAMETERS:
            gate = by_name[name].forbidden
            assert gate is not None, name
            assert gate[0].when.to_dict() == {"truthy": "list"}
