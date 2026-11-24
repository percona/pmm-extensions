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

"""Tests for ``sidecar/dev/app/add_app.py``, a build step of a derived image.

The step runs against a copy of ``sidecar/settings.yaml``, the profile that an
image built from this repository holds.
"""

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "sidecar" / "dev" / "app" / "add_app.py"


def load_script() -> ModuleType:
    """Load the build step, which is in no package.

    :return: The script as a module.
    """
    spec = importlib.util.spec_from_file_location("dev_add_app", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


add_app = load_script()


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """Copy the sidecar's settings profile to a throwaway file.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The copy.
    """
    return Path(shutil.copy(REPO_ROOT / "sidecar" / "settings.yaml", tmp_path))


def apps(profile: Path) -> list[str]:
    """Return the module names in a profile's app list.

    :param profile: The settings profile.
    :return: The names, in the file's order.
    """
    settings = yaml.safe_load(profile.read_text(encoding="utf-8"))
    return [app["MODULE_NAME"] for app in settings["default"]["EXTENSIONS"]["APPS"]]


def test_the_app_is_added_as_the_last_entry(profile: Path) -> None:
    """Append the app to ``EXTENSIONS.APPS`` and keep the entries before it."""
    before = apps(profile)
    assert add_app.main([str(profile), "diskfree"]) == 0
    assert apps(profile) == [*before, "diskfree"]


def test_every_other_line_of_the_profile_is_unchanged(profile: Path) -> None:
    """Add one line and change no other, comments included."""
    before = profile.read_text(encoding="utf-8").splitlines()
    add_app.main([str(profile), "diskfree"])
    after = profile.read_text(encoding="utf-8").splitlines()
    added = "      - MODULE_NAME: diskfree"
    assert after.count(added) == 1
    after.remove(added)
    assert after == before


def test_a_second_call_changes_nothing(profile: Path) -> None:
    """Leave a profile that already lists the app as it is."""
    add_app.main([str(profile), "diskfree"])
    once = profile.read_bytes()
    assert add_app.main([str(profile), "diskfree"]) == 0
    assert profile.read_bytes() == once


def test_a_profile_with_no_app_list_exits_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse a file that is not a settings profile, and say what is missing."""
    profile = tmp_path / "settings.yaml"
    profile.write_text("default:\n  EXTENSIONS:\n    PORT: 9000\n", encoding="utf-8")
    assert add_app.main([str(profile), "diskfree"]) == 1
    assert "APPS" in capsys.readouterr().err
    assert profile.read_text(encoding="utf-8").count("diskfree") == 0


def test_the_image_takes_write_access_to_the_app_from_group_and_others() -> None:
    """Keep a container that joins group 0 from changing the app it runs."""
    containerfile = (SCRIPT.parent / "Containerfile").read_text(encoding="utf-8")
    modes = [line for line in containerfile.splitlines() if "chmod -R" in line]
    assert modes
    assert all("go-w" in line for line in modes)
