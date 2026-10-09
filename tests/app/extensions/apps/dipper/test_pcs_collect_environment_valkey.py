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
"""Test the pcs-collect-environment-valkey.sh payload script."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

SCRIPT = (
    Path(__file__).parents[5]
    / "app/extensions/apps/dipper/payloads/pcs-collect-environment-valkey.sh"
)


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


class TestPcsCollectEnvironmentValkeyFrontmatter:
    """Verify the frontmatter declares the expected Valkey environment parameters."""

    def test_frontmatter_declares_expected_parameters(self):
        """Verify each getopts-derived form parameter appears in the frontmatter."""
        frontmatter = _frontmatter()
        for name in ("o", "d", "i", "t", "p", "l", "c", "s", "P"):
            assert f"name: {name}" in frontmatter

    @pytest.mark.parametrize("name", ["l", "c", "s"])
    def test_mode_flags_are_bool(self, name):
        """Verify the Valkey mode flags (-l legacy, -c cluster, -s sentinel) are booleans."""
        assert "type: bool" in _parameter_block(_frontmatter(), name)

    def test_sentinel_port_is_int(self):
        """Verify the Sentinel port (-P) is an int form field."""
        assert "type: int" in _parameter_block(_frontmatter(), "P")

    def test_sentinel_port_declares_tcp_range(self):
        """Verify the Sentinel port (-P) constrains values to the valid TCP range."""
        block = _parameter_block(_frontmatter(), "P")
        assert "ge: 1" in block
        assert "le: 65535" in block


class TestPcsCollectEnvironmentValkeySentinelPortValidation:
    """Verify the Sentinel port (-P) rejects values outside the valid TCP range."""

    async def _port_adapter(self) -> TypeAdapter:
        """Build a validator for the Sentinel port from the parsed metadata."""
        from app.extensions.snippets.models.snippet import BaseSnippet  # noqa: PLC0415

        meta = await BaseSnippet.get_meta_by_path(SCRIPT)
        snippet = BaseSnippet(
            filename="pcs-collect-environment-valkey.sh",
            size=1,
            md5_digest="a" * 32,
            meta=meta,
        )
        port = next(p for p in snippet.validated_parameters.parameters if p.name == "P")
        return TypeAdapter(port.validation_type)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [1, 26379, 65535])
    async def test_valid_ports_accepted(self, value):
        """Verify a port inside the TCP range validates."""
        adapter = await self._port_adapter()
        assert adapter.validate_python(value) == value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0, -1, 65536, 70000])
    async def test_out_of_range_ports_rejected(self, value):
        """Verify a port outside the TCP range fails validation before dispatch."""
        adapter = await self._port_adapter()
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


class TestPcsCollectEnvironmentValkeyParses:
    """Verify the hand-authored metadata header parses without validation errors."""

    @pytest.mark.asyncio
    async def test_metadata_header_parses_without_errors(self):
        """Assert the parsed metadata header yields no validation errors."""
        from app.extensions.snippets.models.snippet import BaseSnippet  # noqa: PLC0415

        meta = await BaseSnippet.get_meta_by_path(SCRIPT)
        snippet = BaseSnippet(
            filename="pcs-collect-environment-valkey.sh",
            size=1,
            md5_digest="a" * 32,
            meta=meta,
        )
        assert snippet.validated_parameters.errors == []


def _capable_bash() -> str | None:
    """Return the path to a bash that supports ``readarray`` (>= 4.0), or None.

    The collector relies on ``readarray``; macOS ships bash 3.2, so the behavior
    test is skipped when no capable interpreter is available.
    """
    for candidate in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if not path or not os.access(path, os.X_OK):
            continue
        probe = subprocess.run(
            [path, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            major, minor = (int(part) for part in probe.stdout.strip().split("."))
        except ValueError:
            continue
        if (major, minor) >= (4, 0):
            return path
    return None


def _write_exec(path: Path, body: str) -> None:
    """Write ``body`` to ``path`` and mark it executable."""
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestPcsCollectEnvironmentValkeySentinelRouting:
    """Verify every Sentinel query connects through the configured Sentinel port."""

    SENTINEL_PORT = "12345"
    DATA_PORT = "6379"

    def _run(
        self, tmp_path: Path, mode_args: tuple[str, ...] | None = None
    ) -> list[str]:
        """Drive the collector and return the logged CLI argv.

        Fake ``valkey-cli``/``jq``/``sleep`` binaries are placed ahead of the real
        ones on ``PATH``; the fake CLI appends each invocation's arguments to a log
        file. RDS mode (``-i``) skips OS collection and the multi-PID check, and a
        pre-seeded ``pt-summary`` avoids the toolkit download.

        :param tmp_path: pytest temp directory used as the working directory.
        :param mode_args: collection-mode flags to append (defaults to Sentinel
            mode on the non-default ``-P`` port).
        :return: every fake-CLI invocation's argument string, one per line.
        """
        if mode_args is None:
            mode_args = ("-s", "-P", self.SENTINEL_PORT)
        bash = _capable_bash()
        if bash is None:
            pytest.skip("no bash >= 4.0 available for readarray support")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        cli_log = tmp_path / "cli.log"
        _write_exec(
            bin_dir / "valkey-cli",
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$CLI_LOG"\n'
            'case "$*" in\n'
            '  *"SENTINEL MASTERS"*) printf "name mymaster\\n" ;;\n'
            '  *ping*) printf "PONG\\n" ;;\n'
            '  *) printf "null\\n" ;;\n'
            "esac\n",
        )
        _write_exec(bin_dir / "jq", '#!/usr/bin/env bash\nprintf "null\\n"\n')
        _write_exec(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

        toolkit = tmp_path / ".percona-toolkit"
        toolkit.mkdir()
        _write_exec(toolkit / "pt-summary", "#!/usr/bin/env bash\nexit 0\n")

        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "CLI_LOG": str(cli_log),
        }
        result = subprocess.run(
            [
                bash,
                str(SCRIPT),
                "-o",
                f"-h 127.0.0.1 -p {self.DATA_PORT}",
                "-i",
                "rdshost",
                "-t",
                *mode_args,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return cli_log.read_text().splitlines()

    def test_sentinel_queries_use_the_sentinel_port(self, tmp_path):
        """Verify every SENTINEL command (incl. master discovery) targets the -P port."""
        calls = self._run(tmp_path)
        sentinel_calls = [line for line in calls if "SENTINEL" in line]
        # config, masters, primaries, discovery readarray, per-master replicas + sentinels
        expected_sentinel_calls = 6
        assert len(sentinel_calls) >= expected_sentinel_calls
        assert all(f"-p {self.SENTINEL_PORT}" in line for line in sentinel_calls)

    def test_non_sentinel_queries_stay_on_the_data_port(self, tmp_path):
        """Verify data-node queries never pick up the Sentinel port override."""
        calls = self._run(tmp_path)
        data_calls = [line for line in calls if "SENTINEL" not in line]
        assert data_calls
        assert all(f"-p {self.SENTINEL_PORT}" not in line for line in data_calls)

    def test_no_hardcoded_default_sentinel_port(self, tmp_path):
        """Assert no reintroduced literal 26379 survives in any CLI call."""
        assert all("26379" not in line for line in self._run(tmp_path))

    def test_default_sentinel_port_when_flag_omitted(self, tmp_path):
        """Verify Sentinel queries fall back to 26379 when -P is not given."""
        calls = self._run(tmp_path, mode_args=("-s",))
        sentinel_calls = [line for line in calls if "SENTINEL" in line]
        assert sentinel_calls
        assert all("-p 26379" in line for line in sentinel_calls)

    def test_single_mode_issues_no_sentinel_queries(self, tmp_path):
        """Verify default (single-instance) mode runs no SENTINEL commands."""
        calls = self._run(tmp_path, mode_args=())
        assert not any("SENTINEL" in line for line in calls)
        assert any("INFO" in line for line in calls)
        assert all(f"-p {self.SENTINEL_PORT}" not in line for line in calls)
