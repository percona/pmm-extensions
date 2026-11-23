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

"""Tests for ``scripts/app_must_not.py``, the suite "what an app must not do".

Each fixture app is a directory of source text. The suite reads it and never
imports it, so no fixture has to be a working app.
"""

from pathlib import Path

import pytest

from tests.scripts import load_script, PROJECT_ROOT

app_must_not = load_script("app_must_not")

APP_PY = """\
from app.extensions.apps.fixture.spec import build_fixture_spec
from app.extensions.apps.framework.apps import TaskExecutionApp

app = TaskExecutionApp(name="fixture", task_spec_builder=build_fixture_spec)
"""

GOOD_SPEC = """\
import shlex

from app.extensions.apps.framework.spec import build_command_args, RunCommandSpec


def build_fixture_spec(form, resolved):
    args = shlex.join(["--no-version-check", *build_command_args(form)])
    return RunCommandSpec(command="pt-summary", args=args)
"""

SHELL_STRING_SPEC = """\
from app.extensions.apps.framework.spec import RunCommandSpec


def build_fixture_spec(form, resolved):
    return RunCommandSpec(
        command="sh",
        args=f"-c 'pt-summary --databases={form.databases}'",
    )
"""

DICT_SPEC = """\
def build_fixture_spec(form, resolved):
    return {"task": "run-command", "meta": {"command": "pt-summary"}}
"""

OWN_SPEC_CLASS = """\
import shlex

from app.extensions.apps.framework.spec import RunCommandSpec


class ShellSpec:
    def to_envelope_data(self, *, host, service_name, connectivity):
        return {"task": "run-command", "meta": {"command": "sh"}}


def build_fixture_spec(form, resolved):
    return RunCommandSpec(command="pt-summary", args=shlex.join([]))
"""

DECLARED_ARGS_SPEC = """\
import shlex

from app.extensions.apps.framework.spec import RunCommandSpec


def build_fixture_spec(form, resolved):
    args: str
    args = shlex.join(["--no-version-check"])
    return RunCommandSpec(command="pt-summary", args=args)
"""


def make_app(tmp_path: Path, **files: str) -> Path:
    """Write a fixture app and return its directory.

    :param tmp_path: pytest's per-test temporary directory.
    :param files: File contents, keyed by path below the app's directory with
        ``__`` in place of ``/`` and without the ``.py`` suffix.
    :return: The app's directory.
    """
    app_dir = tmp_path / "fixture"
    contents = {"app": APP_PY, "spec": GOOD_SPEC, **files}
    for name, text in contents.items():
        path = app_dir / f"{name.replace('__', '/')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return app_dir


def messages(app_dir: Path) -> list[str]:
    """Return the message of every finding for ``app_dir``.

    :param app_dir: The app's directory.
    :return: The findings' messages, in the order the suite reports them.
    """
    return [finding.message for finding in app_must_not.check_app(app_dir)]


def test_an_app_that_follows_the_rules_has_no_finding(tmp_path: Path) -> None:
    """Pass an app whose builder returns a framework spec from ``shlex.join``."""
    assert app_must_not.check_app(make_app(tmp_path)) == []


def test_a_variable_that_is_declared_before_its_value_passes(tmp_path: Path) -> None:
    """Pass arguments whose variable has an annotation on a line of its own."""
    assert messages(make_app(tmp_path, spec=DECLARED_ARGS_SPEC)) == []


def test_the_checksums_app_of_the_tree_passes() -> None:
    """Pass a real app, whose spec is built behind a ``payload_builder``."""
    checksums = PROJECT_ROOT / "app" / "extensions" / "apps" / "checksums"
    assert app_must_not.check_app(checksums) == []


def test_an_import_of_subprocess_is_reported_with_its_file_and_line(
    tmp_path: Path,
) -> None:
    """Name the file and the line of an import that starts a process."""
    app_dir = make_app(tmp_path, deps='"""Doc."""\n\nimport subprocess\n')
    (finding,) = app_must_not.check_app(app_dir)
    assert (finding.path, finding.line) == (app_dir / "deps.py", 3)
    assert "subprocess" in finding.message


@pytest.mark.parametrize(
    "statement",
    [
        "import socket",
        "import requests",
        "import httpx",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import aiohttp",
        "from aiohttp import ClientSession",
        "import subprocess as sp",
    ],
)
def test_each_process_or_network_import_is_reported(
    tmp_path: Path, statement: str
) -> None:
    """Report every spelling of an import that the suite forbids."""
    assert len(messages(make_app(tmp_path, deps=f"{statement}\n"))) == 1


@pytest.mark.parametrize(
    "statement",
    ["from urllib.parse import quote", "import shlex", "import os"],
)
def test_other_imports_are_allowed(tmp_path: Path, statement: str) -> None:
    """Allow a module that neither starts a process nor opens a connection."""
    assert messages(make_app(tmp_path, deps=f"{statement}\n")) == []


@pytest.mark.parametrize(
    "call",
    ['os.system("id")', 'os.popen("id")', 'eval("1")', 'exec("pass")'],
)
def test_each_forbidden_call_is_reported(tmp_path: Path, call: str) -> None:
    """Report a call that runs a shell command or evaluates text as code."""
    assert len(messages(make_app(tmp_path, deps=f"import os\n\n{call}\n"))) == 1


def test_a_form_field_joined_into_a_shell_string_is_reported(tmp_path: Path) -> None:
    """Report an argument string that ``shlex.join`` did not build."""
    (message,) = messages(make_app(tmp_path, spec=SHELL_STRING_SPEC))
    assert "shlex.join" in message


def test_a_builder_that_returns_no_framework_spec_is_reported(tmp_path: Path) -> None:
    """Report a spec builder whose return value is not a framework spec class."""
    (message,) = messages(make_app(tmp_path, spec=DICT_SPEC))
    assert "build_fixture_spec" in message


def test_a_spec_class_of_the_apps_own_is_reported(tmp_path: Path) -> None:
    """Report a class that stands in for a framework spec class."""
    (message,) = messages(make_app(tmp_path, spec=OWN_SPEC_CLASS))
    assert "ShellSpec" in message


@pytest.mark.parametrize("directory", ["payload", "payloads"])
def test_a_payload_file_is_not_checked(tmp_path: Path, directory: str) -> None:
    """Skip a payload, which runs on the database host and is the app's purpose."""
    files = {f"{directory}__collect": "import subprocess\n"}
    assert messages(make_app(tmp_path, **files)) == []


@pytest.mark.parametrize(
    "statement",
    [
        "import http.client",
        "from http import client",
        "import urllib3",
        "import asyncio.subprocess",
        "from asyncio import create_subprocess_exec",
        "from asyncio.subprocess import create_subprocess_shell",
        "from os import execv",
        "from os import system as run",
    ],
)
def test_each_further_process_or_network_import_is_reported(
    tmp_path: Path, statement: str
) -> None:
    """Report an import of the standard library that starts a process or connects."""
    assert len(messages(make_app(tmp_path, deps=f"{statement}\n"))) == 1


@pytest.mark.parametrize(
    "source",
    [
        'import asyncio\n\n\nasync def go():\n    await asyncio.create_subprocess_exec("id")\n',
        'import asyncio\n\n\nasync def go():\n    await asyncio.create_subprocess_shell("id")\n',
        'import asyncio\n\n\nasync def go():\n    await asyncio.open_connection("host", 1)\n',
        'import os\n\nos.execv("/bin/id", ["id"])\n',
        'import os\n\nos.spawnlp(os.P_WAIT, "id", "id")\n',
        'import os\n\nos.posix_spawn("/bin/id", ["id"], {})\n',
        'import os as o\n\no.system("id")\n',
        'import importlib\n\nimportlib.import_module("subprocess")\n',
        '__import__("subprocess")\n',
    ],
)
def test_each_further_call_that_starts_a_process_or_connects_is_reported(
    tmp_path: Path, source: str
) -> None:
    """Report a call that starts a process, opens a connection or imports by name."""
    assert len(messages(make_app(tmp_path, deps=source))) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "from app.extensions.apps.fixture.payload import helpers",
        "from app.extensions.apps.fixture.payload.helpers import collect",
        "import app.extensions.apps.fixture.payloads.helpers",
        "from .payload import helpers",
    ],
)
def test_an_import_of_a_payload_module_outside_the_payload_is_reported(
    tmp_path: Path, statement: str
) -> None:
    """Report code of a payload that the app would run inside the sidecar."""
    files = {"payload__helpers": "import subprocess\n", "deps": f"{statement}\n"}
    assert len(messages(make_app(tmp_path, **files))) == 1


@pytest.mark.parametrize(
    "statement",
    ["from . import payload", "from app.extensions.apps.fixture import payload"],
)
def test_an_import_of_the_payload_package_is_allowed(
    tmp_path: Path, statement: str
) -> None:
    """Allow the package itself, which an app imports to find its payload's files."""
    files = {"payload__helpers": "import subprocess\n", "deps": f"{statement}\n"}
    assert messages(make_app(tmp_path, **files)) == []


def test_the_init_file_of_a_payload_is_checked(tmp_path: Path) -> None:
    """Read the one file of a payload that an import of the package runs."""
    app_dir = make_app(tmp_path, payload__helpers="import subprocess\n")
    (app_dir / "payload" / "__init__.py").write_text(
        "import subprocess\n", encoding="utf-8"
    )
    assert len(messages(app_dir)) == 1


def test_main_prints_each_finding_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Print ``file:line: message`` for each finding and fail."""
    app_dir = make_app(tmp_path, deps="import socket\n")
    assert app_must_not.main([str(app_dir)]) == 1
    assert f"{app_dir / 'deps.py'}:1: " in capsys.readouterr().out


def test_main_exits_0_for_an_app_with_no_finding(tmp_path: Path) -> None:
    """Pass an app that breaks no rule."""
    assert app_must_not.main([str(make_app(tmp_path))]) == 0


def test_main_exits_2_for_a_directory_that_is_not_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse a path that holds no app, and name it."""
    missing = tmp_path / "nothing"
    assert app_must_not.main([str(missing)]) == app_must_not.EXIT_NO_APP
    assert str(missing) in capsys.readouterr().err


@pytest.mark.parametrize(
    "source",
    [
        'import asyncio\n\nasyncio.subprocess.create_subprocess_shell("id")\n',
        'import asyncio as aio\n\naio.subprocess.create_subprocess_exec("id")\n',
        'import asyncio\n\nasyncio.get_running_loop().subprocess_shell(None, "id")\n',
        "def start(loop):\n    return loop.subprocess_exec(None, 'id')\n",
    ],
)
def test_a_process_started_through_a_longer_name_is_reported(
    tmp_path: Path, source: str
) -> None:
    """Report a process that is started through a module's attribute or an event loop."""
    assert len(messages(make_app(tmp_path, deps=source))) == 1


@pytest.mark.parametrize(
    ("statement", "call"),
    [
        ("from shlex import join", "join"),
        ("import shlex as words", "words.join"),
    ],
)
def test_shlex_join_under_another_name_passes(
    tmp_path: Path, statement: str, call: str
) -> None:
    """Pass arguments that ``shlex.join`` built, whatever name the file gives it."""
    spec = GOOD_SPEC.replace("import shlex", statement).replace("shlex.join", call)
    assert messages(make_app(tmp_path, spec=spec)) == []
