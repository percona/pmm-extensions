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

"""Tests for the ``image-preflight`` target of the ``Makefile``.

A stub ``podman`` and a stub ``buildah`` stand first on ``PATH``, so no test
needs either tool or builds an image.
"""

import os
import shutil
import subprocess
from pathlib import Path

from tests.scripts import PROJECT_ROOT


def run_preflight(
    tmp_path: Path, *, podman_exit: int
) -> subprocess.CompletedProcess[str]:
    """Run ``make image-preflight`` with stub container tools.

    :param tmp_path: pytest's per-test temporary directory, which receives the stubs.
    :param podman_exit: The exit status the ``podman`` stub returns, which is
        the answer to ``podman image exists``.
    :return: The finished ``make`` process.
    """
    for tool, status in (("podman", podman_exit), ("buildah", 0)):
        stub = tmp_path / tool
        stub.write_text(f"#!/usr/bin/env bash\nexit {status}\n", encoding="utf-8")
        stub.chmod(0o755)
    return subprocess.run(
        ["make", "image-preflight"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_image_preflight_names_make_builder(tmp_path: Path) -> None:
    """Point at ``make builder`` when the builder image is absent."""
    result = run_preflight(tmp_path, podman_exit=1)

    assert result.returncode != 0
    assert "make builder" in result.stdout + result.stderr


def test_image_preflight_passes_with_a_builder_image(tmp_path: Path) -> None:
    """Let the build go on when the builder image exists."""
    result = run_preflight(tmp_path, podman_exit=0)

    assert result.returncode == 0


def test_image_preflight_names_the_missing_tools(tmp_path: Path) -> None:
    """Say which tools the build needs on a machine that has neither."""
    for tool in ("env", "bash"):
        (tmp_path / tool).symlink_to(shutil.which(tool))

    result = subprocess.run(
        [shutil.which("make"), "image-preflight"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "podman and buildah are required" in result.stdout + result.stderr
