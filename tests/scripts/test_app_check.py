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

"""Tests for ``scripts/app_check.py``, the ``make app-check`` target.

The script's paths point at a throwaway tree and its suite runner is replaced,
so no test runs a nested pytest.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from tests.scripts import load_script

app_check = load_script("app_check")

PYTEST = (sys.executable, "-m", "pytest")
SHIPPED_LIST_TEST = "test_reduced_activation_mirrors_the_baked_profile"
IN_TREE_SUITES = [
    ("the app's tests", (*PYTEST, "tests/app/extensions/apps/checksums", "-q")),
    (
        "framework conformance",
        (
            *PYTEST,
            "tests/app/extensions/apps/framework/test_conformance.py",
            "tests/app/extensions/apps/framework/test_form_dsl_conformance.py",
            "-q",
        ),
    ),
    (
        "what an app must not do",
        (sys.executable, "scripts/app_must_not.py", "app/extensions/apps/checksums"),
    ),
    (
        "snapshots",
        (
            *PYTEST,
            "tests/app/extensions/test_openapi_snapshot.py",
            "tests/app/extensions/test_schema_snapshot.py",
            "-q",
        ),
    ),
    ("OpenAPI files", (*PYTEST, "tests/app/test_openapi_specs_fresh.py", "-q")),
    (
        "the list of apps that ship",
        (
            *PYTEST,
            f"tests/sidecar/test_embedded_settings.py::{SHIPPED_LIST_TEST}",
            "-q",
        ),
    ),
]

Runner = Callable[[tuple[str, ...], Path, dict[str, str]], int]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the script at a throwaway checkout that holds the app ``checksums``.

    :param tmp_path: pytest's per-test temporary directory.
    :param monkeypatch: The pytest monkeypatch fixture.
    :return: The root of the throwaway checkout.
    """
    root = tmp_path / "checkout"
    (root / "app" / "extensions" / "apps" / "checksums").mkdir(parents=True)
    (root / "tests" / "app" / "extensions" / "apps" / "checksums").mkdir(parents=True)
    settings = {"default": {"EXTENSIONS": {"APPS": [{"MODULE_NAME": "checksums"}]}}}
    (root / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setattr(app_check, "REPO_ROOT", root)
    return root


@pytest.fixture
def outside_app(tmp_path: Path) -> Path:
    """Create an app's package with a ``tests`` directory beside it.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The app's package directory, outside the throwaway checkout.
    """
    package = tmp_path / "workspace" / "diskfree"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    tests = package.parent / "tests"
    tests.mkdir()
    (tests / "test_contract.py").write_text("", encoding="utf-8")
    return package


def recorder(
    calls: list[tuple[str, ...]], status: dict[str, int] | None = None
) -> Runner:
    """Return a suite runner that records each command and runs nothing.

    :param calls: The list that receives every command.
    :param status: The exit status to answer for a command, keyed by a word of
        the command. A command that holds none of the words answers 0.
    :return: The runner.
    """

    def run(command: tuple[str, ...], log: Path, env: dict[str, str]) -> int:
        calls.append(command)
        return next(
            (
                code
                for word, code in (status or {}).items()
                if word in " ".join(command)
            ),
            0,
        )

    return run


def test_the_suites_of_an_app_in_the_tree_are_six_in_ci_order() -> None:
    """Build the six command lines that CI asks of one app."""
    assert app_check.suites("checksums", outside=False) == IN_TREE_SUITES


def test_the_suites_of_an_app_outside_the_tree_are_the_first_three() -> None:
    """Leave out the suites that describe the repository's own tree."""
    assert app_check.suites("checksums", outside=True) == IN_TREE_SUITES[:3]


def test_an_unknown_name_exits_2_and_names_the_directory(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse a name with no package, and say where the package was expected."""
    calls: list[tuple[str, ...]] = []
    assert (
        app_check.main(["--name", "nosuch"], run=recorder(calls))
        == app_check.EXIT_USAGE
    )
    assert (
        str(tree / "app" / "extensions" / "apps" / "nosuch") in capsys.readouterr().err
    )
    assert calls == []


@pytest.mark.parametrize("name", ["", "framework", "shared", "__pycache__", "../apps"])
def test_a_name_that_is_no_app_exits_2_and_runs_nothing(
    tree: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse an empty name and a directory that is no app, before any suite runs."""
    (tree / "app" / "extensions" / "apps" / "framework").mkdir()
    (tree / "app" / "extensions" / "apps" / "shared").mkdir()
    calls: list[tuple[str, ...]] = []
    assert app_check.main(["--name", name], run=recorder(calls)) == app_check.EXIT_USAGE
    assert "NAME" in capsys.readouterr().err
    assert calls == []


def test_every_suite_runs_and_prints_one_line(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Print ``<suite>: passed`` for each of the six suites and exit 0."""
    calls: list[tuple[str, ...]] = []
    assert app_check.main(["--name", "checksums"], run=recorder(calls)) == 0
    assert calls == [command for _, command in IN_TREE_SUITES]
    assert capsys.readouterr().out.splitlines() == [
        f"{label}: passed" for label, _ in IN_TREE_SUITES
    ]


def test_one_failing_suite_gives_status_1_and_the_others_still_run(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run every suite after a failure, and point at the failed suite's log."""
    calls: list[tuple[str, ...]] = []
    run = recorder(calls, {"test_conformance.py": 1})
    assert app_check.main(["--name", "checksums"], run=run) == 1
    assert len(calls) == len(IN_TREE_SUITES)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "the app's tests: passed"
    assert lines[1].startswith("framework conformance: FAILED, see ")
    assert Path(
        lines[1].removeprefix("framework conformance: FAILED, see ")
    ).parent.is_dir()


def test_an_app_outside_the_tree_is_in_place_while_the_suites_run(
    tree: Path, outside_app: Path
) -> None:
    """Copy the app and its tests in, and name a settings copy that lists the app."""
    seen: list[tuple[bool, bool, list[str]]] = []

    def run(command: tuple[str, ...], log: Path, env: dict[str, str]) -> int:
        settings = yaml.safe_load(
            Path(env["SETTINGS_FILE"]).read_text(encoding="utf-8")
        )
        seen.append(
            (
                (tree / "app/extensions/apps/diskfree/__init__.py").is_file(),
                (
                    tree / "tests/app/extensions/apps/diskfree/test_contract.py"
                ).is_file(),
                [
                    app["MODULE_NAME"]
                    for app in settings["default"]["EXTENSIONS"]["APPS"]
                ],
            )
        )
        return 0

    arguments = ["--name", "diskfree", "--app-dir", str(outside_app)]
    assert app_check.main(arguments, run=run) == 0
    assert seen == [(True, True, ["checksums", "diskfree"])] * 3


@pytest.mark.parametrize("fails", [False, True])
def test_an_app_outside_the_tree_leaves_the_checkout_as_it_was(
    tree: Path, outside_app: Path, *, fails: bool
) -> None:
    """Remove the copies after the run, and after a runner that raises."""
    before = sorted(str(path) for path in tree.rglob("*"))
    settings_before = (tree / "settings.yaml").read_bytes()

    def run(command: tuple[str, ...], log: Path, env: dict[str, str]) -> int:
        if fails:
            raise KeyboardInterrupt
        return 0

    arguments = ["--name", "diskfree", "--app-dir", str(outside_app)]
    if fails:
        with pytest.raises(KeyboardInterrupt):
            app_check.main(arguments, run=run)
    else:
        assert app_check.main(arguments, run=run) == 0
    assert sorted(str(path) for path in tree.rglob("*")) == before
    assert (tree / "settings.yaml").read_bytes() == settings_before


def test_the_directory_that_holds_the_package_is_accepted_too(
    tree: Path, outside_app: Path
) -> None:
    """Take the workspace directory as well as the package inside it."""
    calls: list[tuple[str, ...]] = []
    arguments = ["--name", "diskfree", "--app-dir", str(outside_app.parent)]
    assert app_check.main(arguments, run=recorder(calls)) == 0
    assert len(calls) == len(IN_TREE_SUITES[:3])


def test_an_app_dir_whose_name_is_taken_in_the_tree_exits_2(
    tree: Path, outside_app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse to copy over an app that the checkout already holds."""
    calls: list[tuple[str, ...]] = []
    outside_app.rename(outside_app.with_name("checksums"))
    arguments = ["--name", "checksums", "--app-dir", str(outside_app.parent)]
    assert app_check.main(arguments, run=recorder(calls)) == app_check.EXIT_USAGE
    assert "already" in capsys.readouterr().err
    assert calls == []


def test_an_app_dir_with_no_package_exits_2(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse a directory that holds no package of the given name."""
    empty = tmp_path / "empty"
    empty.mkdir()
    arguments = ["--name", "diskfree", "--app-dir", str(empty)]
    assert app_check.main(arguments, run=recorder([])) == app_check.EXIT_USAGE
    assert str(empty) in capsys.readouterr().err
