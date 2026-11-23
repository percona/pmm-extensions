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

"""Tests for the ``scripts/check_python_version.py`` interpreter gate."""

import tomllib

import pytest
from packaging.specifiers import SpecifierSet

from tests.scripts import load_script, PROJECT_ROOT

check_python_version = load_script("check_python_version")


def requires_python() -> SpecifierSet:
    """Return the interpreter range ``pyproject.toml`` declares.

    :return: The parsed ``project.requires-python`` specifier.
    """
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return SpecifierSet(pyproject["project"]["requires-python"])


@pytest.mark.parametrize(
    "version",
    [
        (3, 10, 14),
        (3, 11, 8),
        (3, 11, 9),
        (3, 12, 0),
        (3, 12, 2),
        (3, 12, 3),
        (3, 13, 9),
        (3, 14, 0),
        (3, 14, 10),
        (3, 15, 0),
    ],
)
def test_is_supported_mirrors_requires_python(version: tuple[int, int, int]) -> None:
    """Agree with ``requires-python`` on each side of every boundary it draws."""
    expected = requires_python().contains(".".join(str(part) for part in version))

    assert check_python_version.is_supported(version) is expected


def test_check_loads_on_an_interpreter_older_than_3_9() -> None:
    """Leave the annotations unevaluated, so ``tuple[int, int, int]`` cannot fail on 3.8."""
    assert isinstance(check_python_version.is_supported.__annotations__["version"], str)
