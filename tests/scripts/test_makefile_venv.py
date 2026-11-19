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

"""Tests for the ``venv`` target of the ``Makefile``.

The recipe is driven through ``make`` itself, against throwaway paths under
``tmp_path``, so no test touches a real virtualenv.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests.scripts import PROJECT_ROOT


def make_environment() -> dict[str, str]:
    """Return the process environment without the variables that select a virtualenv.

    ``VIRTUAL_ENV`` and ``POETRY`` each switch the ``Makefile`` to an existing
    environment, which is the path these tests must not take.

    :return: A copy of ``os.environ`` without ``VIRTUAL_ENV`` and ``POETRY``.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name not in {"VIRTUAL_ENV", "POETRY"}
    }


def test_make_venv_keeps_poetry_out_of_the_project_virtualenv(tmp_path: Path) -> None:
    """Install Poetry with no ``pip`` call that targets the project virtualenv."""
    venv = tmp_path / "venv"

    result = subprocess.run(
        [
            "make",
            "-n",
            "venv",
            f"VENV={venv}",
            f"POETRY_VENV={tmp_path / 'poetry-venv'}",
        ],
        cwd=PROJECT_ROOT,
        env=make_environment(),
        capture_output=True,
        text=True,
        check=True,
    )

    recipe_lines = [line.replace('"', "") for line in result.stdout.splitlines()]
    project_pip_lines = [
        line.replace(str(tmp_path), "<tmp>")
        for line in recipe_lines
        if f"{venv}/bin/pip" in line
    ]
    assert project_pip_lines
    assert [line for line in project_pip_lines if "poetry" in line] == []


def test_make_venv_removes_a_virtualenv_it_failed_to_build(tmp_path: Path) -> None:
    """Leave no half-built virtualenv behind for the next run to reuse."""
    venv = tmp_path / "venv"
    interpreter = tmp_path / "python-stub"
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then\n'
        '    mkdir -p "$3"\n'
        "    exit 1\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)

    result = subprocess.run(
        [
            "make",
            "venv",
            f"VENV={venv}",
            f"POETRY_VENV={tmp_path / 'poetry-venv'}",
            f"PYTHON={interpreter}",
        ],
        cwd=PROJECT_ROOT,
        env=make_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not venv.exists()


def test_make_venv_refuses_a_venv_path_that_is_not_a_directory(tmp_path: Path) -> None:
    """Refuse a ``VENV`` that names a file, and leave that file in place."""
    venv = tmp_path / "venv"
    venv.write_text("not a virtualenv\n", encoding="utf-8")

    result = subprocess.run(
        [
            "make",
            "venv",
            f"VENV={venv}",
            f"POETRY_VENV={tmp_path / 'poetry-venv'}",
            f"PYTHON={sys.executable}",
        ],
        cwd=PROJECT_ROOT,
        env=make_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert venv.read_text(encoding="utf-8") == "not a virtualenv\n"
