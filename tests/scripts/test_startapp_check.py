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

"""Tests for the ``scripts/startapp_check.py`` scaffold gate.

The gate's subprocess runner and its paths are replaced, so no test scaffolds an
app or runs a nested pytest.
"""

from pathlib import Path

import pytest

from tests.scripts import load_script

startapp_check = load_script("startapp_check")

CONFORMANCE_FILES = (
    "tests/app/extensions/apps/framework/test_conformance.py",
    "tests/app/extensions/apps/framework/test_form_dsl_conformance.py",
)


@pytest.fixture
def recorded_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, ...]]:
    """Point the gate at throwaway paths and record every command it runs.

    :param tmp_path: pytest's per-test temporary directory.
    :param monkeypatch: The pytest monkeypatch fixture.
    :return: The commands the gate ran, in order, filled in as it runs.
    """
    settings = tmp_path / "settings.yaml"
    settings.write_text("default: {}\n", encoding="utf-8")
    monkeypatch.setattr(startapp_check, "SETTINGS_FILE", settings)
    monkeypatch.setattr(startapp_check, "PLUGINS_DIR", tmp_path / "apps")
    monkeypatch.setattr(startapp_check, "TESTS_DIR", tmp_path / "tests")
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(startapp_check, "_run", lambda *args: runs.append(args))
    return runs


def test_runs_framework_conformance_once_after_every_flavor(
    recorded_runs: list[tuple[str, ...]],
) -> None:
    """Run both conformance files while all three scaffolded apps are registered."""
    assert startapp_check.main() == 0

    conformance_runs = [
        run for run in recorded_runs if set(CONFORMANCE_FILES) <= set(run)
    ]
    assert len(conformance_runs) == 1
    assert recorded_runs[-1] == conformance_runs[0]
    scaffold_runs = [run for run in recorded_runs if "--name" in run]
    assert len(scaffold_runs) == len(startapp_check.FLAVORS)
