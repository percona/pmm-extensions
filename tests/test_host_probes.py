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

"""Cover the probes that skip a case its host cannot run, and fail it in CI."""

import errno
from pathlib import Path

import pytest

from tests.conftest import require_bash_4, require_file_name, require_gnu


def _tool(directory: Path, name: str, body: str) -> Path:
    """Write an executable stand-in for ``name`` into ``directory``."""
    tool = directory / name
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)
    return tool


@pytest.fixture
def not_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a developer checkout, where a missing tool skips."""
    monkeypatch.delenv("CI", raising=False)


@pytest.mark.usefixtures("not_ci")
class TestDeveloperCheckout:
    """Skip, naming what is missing, when the host cannot run the case."""

    def test_bash_3_skips_with_the_way_to_get_bash_4(self, tmp_path: Path) -> None:
        """Skip under a bash older than 4, and say how to install a newer one."""
        _tool(tmp_path, "bash", "echo 3")

        with pytest.raises(pytest.skip.Exception, match=r"bash 4.*brew install bash"):
            require_bash_4(path=str(tmp_path))

    def test_bash_5_runs(self, tmp_path: Path) -> None:
        """Let the case run under bash 5."""
        _tool(tmp_path, "bash", "echo 5")

        require_bash_4(path=str(tmp_path))

    def test_a_tool_that_is_not_gnu_skips(self, tmp_path: Path) -> None:
        """Skip when the tool on the case's ``PATH`` does not report GNU."""
        _tool(tmp_path, "xargs", "exit 1")

        with pytest.raises(pytest.skip.Exception, match="needs GNU xargs"):
            require_gnu("xargs", path=str(tmp_path))

    def test_a_bash_that_cannot_start_skips(self, tmp_path: Path) -> None:
        """Skip, not error, when ``bash`` is found but its interpreter is missing."""
        _tool(tmp_path, "bash", "").write_text("#!/nonexistent/interpreter\n")

        with pytest.raises(pytest.skip.Exception, match="bash 4"):
            require_bash_4(path=str(tmp_path))

    def test_a_tool_that_cannot_start_skips(self, tmp_path: Path) -> None:
        """Skip, not error, when the tool is found but its interpreter is missing."""
        _tool(tmp_path, "xargs", "").write_text("#!/nonexistent/interpreter\n")

        with pytest.raises(pytest.skip.Exception, match="needs GNU xargs"):
            require_gnu("xargs", path=str(tmp_path))

    def test_a_gnu_tool_runs(self, tmp_path: Path) -> None:
        """Let the case run when the tool reports GNU."""
        _tool(tmp_path, "xargs", "echo 'xargs (GNU findutils) 4.10.0'")

        require_gnu("xargs", path=str(tmp_path))

    def test_a_refused_file_name_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skip when the file system refuses the name, as APFS refuses one not in UTF-8."""

        def refuse(_path: Path) -> None:
            raise OSError(errno.EILSEQ, "Illegal byte sequence")

        monkeypatch.setattr(Path, "touch", refuse)

        with pytest.raises(pytest.skip.Exception, match="refuses"):
            require_file_name(tmp_path, "\udcff.cnf")

    def test_an_accepted_file_name_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """Let the case run, and remove the probe's file, when the name is accepted."""
        require_file_name(tmp_path, "my.cnf")

        assert list(tmp_path.iterdir()) == []


def test_ci_fails_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail in CI, which provides every tool, so a lost one cannot hide as a skip."""
    monkeypatch.setenv("CI", "true")
    _tool(tmp_path, "xargs", "exit 1")

    with pytest.raises(pytest.fail.Exception, match="CI must provide"):
        require_gnu("xargs", path=str(tmp_path))
