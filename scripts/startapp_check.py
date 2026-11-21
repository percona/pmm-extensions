#!/usr/bin/env python3
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

"""Scaffold each app flavor via the real CLI and assert its generated tests pass.

The ``make startapp-check`` CI gate exercises the Makefile/CLI/``settings.yaml``
path end-to-end — which the in-process ``test_scaffold.py`` deliberately bypasses
— by scaffolding a throwaway app per flavor through
``python app/extensions/apps/framework/scaffold.py`` and running the contract test it
generates. With all three registered, it then runs framework conformance once: a
scaffold can pass its own contract test and still break a rule that holds across
the registry.
The throwaway packages and the ``settings.yaml`` edit are reverted in a
``finally`` even when a flavor fails, so a failed run never dirties the worktree
or breaks the next run's clobber guard.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = REPO_ROOT / "settings.yaml"
PLUGINS_DIR = REPO_ROOT / "app" / "extensions" / "apps"
TESTS_DIR = REPO_ROOT / "tests" / "app" / "extensions" / "apps"
SCAFFOLDER = REPO_ROOT / "app" / "extensions" / "apps" / "framework" / "scaffold.py"
FLAVORS = ("task", "script", "base")
CONFORMANCE_TESTS = (
    "tests/app/extensions/apps/framework/test_conformance.py",
    "tests/app/extensions/apps/framework/test_form_dsl_conformance.py",
)


def _run(*args: str) -> None:
    """Run a subprocess from the repo root, raising on a non-zero exit."""
    subprocess.run(args, cwd=REPO_ROOT, check=True)


def main() -> int:
    """Run the scaffold-and-test loop over every flavor, restoring the worktree.

    :return: ``0`` when every flavor scaffolds, its generated tests pass and
        framework conformance passes over all three, ``1`` otherwise.
    """
    settings_backup = SETTINGS_FILE.read_text()
    names = [f"_scaffold_ci_{flavor}" for flavor in FLAVORS]
    exit_code = 0
    try:
        for flavor, name in zip(FLAVORS, names, strict=True):
            print(f"== scaffolding {flavor!r} app {name!r} ==", flush=True)
            _run(
                sys.executable,
                str(SCAFFOLDER),
                "--name",
                name,
                "--type",
                flavor,
                "--no-input",
            )
            _run(
                sys.executable,
                "-m",
                "pytest",
                f"tests/app/extensions/apps/{name}/",
                "-q",
            )
        print("== framework conformance over the scaffolded apps ==", flush=True)
        _run(sys.executable, "-m", "pytest", *CONFORMANCE_TESTS, "-q")
    except subprocess.CalledProcessError as error:
        print(f"startapp-check failed: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        SETTINGS_FILE.write_text(settings_backup)
        for name in names:
            shutil.rmtree(PLUGINS_DIR / name, ignore_errors=True)
            shutil.rmtree(TESTS_DIR / name, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
