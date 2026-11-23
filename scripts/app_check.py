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

"""Run every check that CI asks of one app, behind ``make app-check``.

For an app in the tree, ``make app-check NAME=<app>`` runs six suites: the
app's own tests, framework conformance, "what an app must not do", the
snapshot modules, the test that the OpenAPI files are fresh, and the test that
pins the list of apps that ship.

With ``APP_DIR=<directory>`` the app is outside the tree. The directory is the
app's package, with a ``tests`` directory beside it, or the directory that
holds both. The script copies the package and its tests into the checkout for
the run, names a temporary copy of ``settings.yaml`` that lists the app, runs
the first three suites, and removes the copies. The other three suites describe
the repository's own tree and do not apply.

It prints one line for each suite, runs every suite even after a failure, and
regenerates nothing. Exit status 0 when every suite passed, 1 when one failed,
2 when the app is not where the arguments say.
"""

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
APPS_PATH = Path("app/extensions/apps")
TESTS_PATH = Path("tests/app/extensions/apps")
SHIPPED_LIST_TEST = "test_reduced_activation_mirrors_the_baked_profile"
EXIT_FAILED = 1
EXIT_USAGE = 2
SIGTERM_EXIT = 143

Suite = tuple[str, tuple[str, ...]]
Runner = Callable[[tuple[str, ...], Path, dict[str, str]], int]


APP_NAME = re.compile(r"[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*")
NOT_APPS = ("framework", "shared")


class UsageError(Exception):
    """Report that the app is not where the arguments say it is."""


def suites(name: str, *, outside: bool) -> list[Suite]:
    """Return the suites for one app, each with its command line.

    :param name: The app's module name.
    :param outside: ``True`` for an app that is not in the tree.
    :return: Pairs of the suite's label and its command, in the order they run.
    """
    pytest = (sys.executable, "-m", "pytest")
    framework = f"{TESTS_PATH}/framework"
    every_app: list[Suite] = [
        ("the app's tests", (*pytest, f"{TESTS_PATH}/{name}", "-q")),
        (
            "framework conformance",
            (
                *pytest,
                f"{framework}/test_conformance.py",
                f"{framework}/test_form_dsl_conformance.py",
                "-q",
            ),
        ),
        (
            "what an app must not do",
            (sys.executable, "scripts/app_must_not.py", f"{APPS_PATH}/{name}"),
        ),
    ]
    tree_only: list[Suite] = [
        (
            "snapshots",
            (
                *pytest,
                "tests/app/extensions/test_openapi_snapshot.py",
                "tests/app/extensions/test_schema_snapshot.py",
                "-q",
            ),
        ),
        ("OpenAPI files", (*pytest, "tests/app/test_openapi_specs_fresh.py", "-q")),
        (
            "the list of apps that ship",
            (
                *pytest,
                f"tests/sidecar/test_embedded_settings.py::{SHIPPED_LIST_TEST}",
                "-q",
            ),
        ),
    ]
    return every_app if outside else every_app + tree_only


def run_suite(command: tuple[str, ...], log: Path, env: dict[str, str]) -> int:
    """Run one suite from the root of the checkout, with its output in ``log``.

    :param command: The suite's command line.
    :param log: The file that receives the suite's output.
    :param env: The environment for the suite.
    :return: The suite's exit status.
    """
    with log.open("w", encoding="utf-8") as output:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode


def _package_and_tests(name: str, app_dir: Path) -> tuple[Path, Path]:
    """Find an outside app's package and its tests.

    :param name: The app's module name.
    :param app_dir: The package, or the directory that holds it.
    :return: The package directory and the tests directory.
    :raises UsageError: When ``app_dir`` holds no package of that name.
    """
    for package in (app_dir, app_dir / name):
        if package.name == name and (package / "__init__.py").is_file():
            return package, package.parent / "tests"
    message = f"no package {name} at {app_dir} or at {app_dir / name}"
    raise UsageError(message)


@contextlib.contextmanager
def placed(name: str, app_dir: Path, work_dir: Path) -> Iterator[dict[str, str]]:
    """Put an outside app into the checkout for the length of a run.

    The checkout's own ``settings.yaml`` is not written: the suites read a copy
    that lists the app, named by ``SETTINGS_FILE``.

    :param name: The app's module name.
    :param app_dir: The package, or the directory that holds it.
    :param work_dir: A directory outside the checkout for the settings copy.
    :return: The environment variables that the suites need.
    :raises UsageError: When the package is missing, or the checkout already
        holds an app or a test package of that name.
    """
    package, tests = _package_and_tests(name, app_dir.resolve())
    copies = [(package, REPO_ROOT / APPS_PATH / name)]
    if tests.is_dir():
        copies.append((tests, REPO_ROOT / TESTS_PATH / name))
    for _, target in copies:
        if target.exists():
            message = f"{target} already exists: remove it, or leave out APP_DIR"
            raise UsageError(message)
    settings = yaml.safe_load((REPO_ROOT / "settings.yaml").read_text(encoding="utf-8"))
    apps = settings["default"]["EXTENSIONS"]["APPS"]
    if name not in [app["MODULE_NAME"] for app in apps]:
        apps.append({"MODULE_NAME": name})
    settings_copy = work_dir / "settings.yaml"
    settings_copy.write_text(yaml.safe_dump(settings), encoding="utf-8")
    try:
        for source, target in copies:
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__")
            )
        yield {"SETTINGS_FILE": str(settings_copy)}
    finally:
        for _, target in copies:
            shutil.rmtree(target, ignore_errors=True)


def check(name: str, app_dir: Path | None, run: Runner) -> int:
    """Run the suites of one app and print one line for each.

    :param name: The app's module name.
    :param app_dir: The app's directory outside the tree, or ``None``.
    :param run: The function that runs one suite.
    :return: ``0`` when every suite passed, ``1`` otherwise.
    :raises UsageError: When the app is not where the arguments say.
    """
    if not APP_NAME.fullmatch(name) or name in NOT_APPS:
        message = (
            f"NAME must be the module name of an app, such as checksums: got {name!r}"
        )
        raise UsageError(message)
    work_dir = Path(tempfile.mkdtemp(prefix=f"app-check-{name.replace('/', '-')}-"))
    with contextlib.ExitStack() as stack:
        env = dict(os.environ)
        if app_dir is not None:
            env.update(stack.enter_context(placed(name, app_dir, work_dir)))
        elif not (REPO_ROOT / APPS_PATH / name).is_dir():
            message = f"no app at {REPO_ROOT / APPS_PATH / name}"
            raise UsageError(message)
        failed = False
        for number, (label, command) in enumerate(
            suites(name, outside=app_dir is not None), start=1
        ):
            log = work_dir / f"{number}.log"
            if run(command, log, env) == 0:
                print(f"{label}: passed", flush=True)
            else:
                failed = True
                print(f"{label}: FAILED, see {log}", flush=True)
    return EXIT_FAILED if failed else 0


def main(argv: list[str], run: Runner = run_suite) -> int:
    """Parse the arguments and check one app.

    :param argv: The command line's arguments.
    :param run: The function that runs one suite.
    :return: The exit status.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--name", required=True, help="the app's module name")
    parser.add_argument("--app-dir", type=Path, help="an app outside the tree")
    arguments = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(SIGTERM_EXIT))
    try:
        return check(arguments.name, arguments.app_dir, run)
    except UsageError as error:
        print(f"app-check: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
