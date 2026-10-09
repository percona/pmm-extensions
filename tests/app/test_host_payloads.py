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

"""Guard that every Python file shipped to executor hosts runs on Python 3.9.

The load branch needs a real 3.9 interpreter, which CI provides through
``$EXTENSIONS_PAYLOAD_CHECK_PYTHON``; see :mod:`tests.app.host_payloads` for how the
file set is derived and why both branches are needed.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import BASE_DIR
from app.extensions.routes.artifacts import collect_base_dirs
from tests.app.host_payloads import (
    CHECK_PYTHON_ENV,
    discover_host_payloads,
    drift_violations,
    InterpreterMismatchError,
    LOAD_ARGV,
    load_under,
    MINIMUM_HOST_PYTHON,
    MINIMUM_HOST_PYTHON_VERSION,
    missing_interpreter_is_fatal,
    resolve_py39_interpreter,
    runtime_union_violations,
    static_violations,
)

ARTIFACT_DIRS = [thunk() for thunk in collect_base_dirs().values()]
DISCOVERED = discover_host_payloads(BASE_DIR, ARTIFACT_DIRS)


def _relative(path: Path) -> str:
    """Return ``path`` relative to the repository root, as a test id."""
    return path.relative_to(BASE_DIR).as_posix()


@pytest.fixture(scope="module")
def py39() -> str:
    """Return the minimum-version interpreter, failing in CI when it is missing."""
    if (python := resolve_py39_interpreter()) is not None:
        return python
    if missing_interpreter_is_fatal():
        pytest.fail(
            f"CI must provide Python {MINIMUM_HOST_PYTHON_VERSION} for the "
            f"host-payload load check: set {CHECK_PYTHON_ENV} to its path"
        )
    pytest.skip(
        f"no Python {MINIMUM_HOST_PYTHON_VERSION} on PATH and {CHECK_PYTHON_ENV} unset"
    )


@pytest.mark.parametrize("payload", DISCOVERED, ids=_relative)
def test_every_host_payload_loads_under_the_minimum_python(
    py39: str, payload: Path, tmp_path: Path
) -> None:
    """Load each shipped file, unminified, under the oldest supported host Python."""
    error = load_under(py39, payload, tmp_path, LOAD_ARGV.get(_relative(payload), ()))

    assert error is None, f"{_relative(payload)} does not load:\n{error}"


def test_no_host_payload_uses_a_newer_standard_library() -> None:
    """Find no API newer than the minimum host Python anywhere in a shipped file."""
    assert static_violations(DISCOVERED) is None


def test_no_host_payload_evaluates_a_type_union_at_runtime() -> None:
    """Find no ``X | Y`` type union a shipped file evaluates inside a function."""
    assert runtime_union_violations(DISCOVERED) == []


@pytest.mark.parametrize(
    ("source", "count"),
    [
        pytest.param(
            "def f(v):\n    return isinstance(v, int | str)\n", 1, id="isinstance"
        ),
        pytest.param(
            "def f(v):\n    return issubclass(v, int | str | None)\n",
            1,
            id="nested-union-counts-once",
        ),
        pytest.param(
            "from typing import cast\n\n\ndef f(v):\n    return cast(int | None, v)\n",
            1,
            id="none-operand",
        ),
        pytest.param(
            "def f(v: int | None) -> str | None:\n    return None\n",
            0,
            id="annotations-left-to-the-load-branch",
        ),
        pytest.param(
            "def f(a, b):\n    return a | b, 4 | 1\n", 0, id="set-and-integer-or"
        ),
        pytest.param(
            "def f(v):\n    return isinstance(v, (int, str))\n", 0, id="tuple-form"
        ),
        pytest.param(
            "from typing import cast\n\n\ndef f(v):\n    return cast(int | str, v)\n",
            1,
            id="cast-type-argument",
        ),
        pytest.param(
            "import typing\n\n\ndef f(v):\n    return typing.cast(int | str, v)\n",
            1,
            id="qualified-cast",
        ),
        pytest.param(
            "from typing import cast as as_type\n\n\n"
            "def f(v):\n    return as_type(int | str, v)\n",
            1,
            id="aliased-cast",
        ),
        pytest.param(
            "def f():\n    alias = int | str\n    return alias\n",
            1,
            id="builtin-type-operand",
        ),
        pytest.param(
            "def f():\n    return list[int] | str\n", 1, id="builtin-generic-operand"
        ),
        pytest.param(
            "import re\n\n\ndef f():\n    return re.I | re.M\n", 0, id="flag-or"
        ),
    ],
)
def test_the_union_check_flags_only_runtime_type_unions(
    fixture_dir: Path, source: str, count: int
) -> None:
    """Flag a type union in a function body, and leave other ``|`` alone.

    Covers ``isinstance``/``issubclass``, ``typing.cast`` under a bare, aliased,
    or qualified name, and a builtin type name (bare or subscripted) as an
    operand — Python 3.9 raises ``TypeError`` on these only when the function
    runs, which loading never does and ``vermin`` does not report.
    """
    payload = fixture_dir / "unions_payload"
    payload.write_text(source, encoding="utf-8")

    assert len(runtime_union_violations([payload])) == count


def test_every_payload_reference_site_is_covered_by_discovery() -> None:
    """Find nothing a payload reference site names that discovery missed."""
    assert drift_violations(BASE_DIR, DISCOVERED) == []


@pytest.mark.parametrize(
    "landmark",
    [
        "app/extensions/sync/syncers/system_facts/payload.py",
        "app/tasks/connectivity/payload.py",
        "app/extensions/apps/alters/pre_checks.py",
        "app/extensions/apps/backup_mongo/restore/pbm_list_payload",
        "app/extensions/apps/topology/payloads/topology.py",
        "app/extensions/apps/dipper/payloads/pcs-collect-pmm-mysql.py",
    ],
)
def test_discovery_reaches_each_payload_convention(landmark: str) -> None:
    """Include a payload of every shipping convention, named for readability."""
    assert BASE_DIR / landmark in DISCOVERED


def test_discovery_covers_every_extensionless_and_python_script_file() -> None:
    """Include every file under ``app/`` shaped like a host payload.

    Expressed over the whole tree rather than through the payload globs, so a
    narrowing of those globs cannot shrink both sides of the comparison at once.
    ``migrations/README`` files are the only extensionless files that are not
    payloads.
    """
    shaped_like_payloads = {
        path
        for path in (BASE_DIR / "app").rglob("*")
        if path.is_file()
        and "migrations" not in path.parts
        and "__pycache__" not in path.parts
        and (not path.suffix or path.read_bytes().startswith(b"#!/usr/bin/env python"))
    }

    assert shaped_like_payloads
    assert shaped_like_payloads <= set(DISCOVERED)


def test_discovery_includes_no_shell_script() -> None:
    """Leave every shell script out of the Python check."""
    assert [path for path in DISCOVERED if path.suffix == ".sh"] == []


def test_every_load_argv_entry_names_a_discovered_payload() -> None:
    """Keep ``LOAD_ARGV`` from naming a payload the guard no longer loads."""
    assert LOAD_ARGV
    assert set(LOAD_ARGV) <= {_relative(path) for path in DISCOVERED}


def _write(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and return ``root``."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


_PACKAGE = "app/extensions/apps/example"
_VALID_PAYLOAD = f"{_PACKAGE}/valid_payload"
_IMPORT_PAYLOAD_URI = "from app.core.utils.path import payload_uri\n\n"


@pytest.mark.parametrize(
    ("files", "rule", "named"),
    [
        pytest.param(
            {
                f"{_PACKAGE}/spec.py": _IMPORT_PAYLOAD_URI
                + 'PAYLOAD = payload_uri(__file__, "helper.py")\n',
                f"{_PACKAGE}/helper.py": "VALUE = 1\n",
            },
            "R1",
            "helper.py",
            id="R1-literal-target-not-discovered",
        ),
        pytest.param(
            {
                f"{_PACKAGE}/spec.py": _IMPORT_PAYLOAD_URI
                + 'PAYLOAD = payload_uri(__file__, "renamed_payload")\n',
            },
            "R1",
            "renamed_payload",
            id="R1-literal-target-missing",
        ),
        pytest.param(
            {
                f"{_PACKAGE}/spec.py": _IMPORT_PAYLOAD_URI
                + 'PAYLOAD = payload_uri(__file__, "valid_payload")\n',
                f"{_PACKAGE}/restore_tool": "VALUE = 1\n",
            },
            "R2",
            "restore_tool",
            id="R2-extensionless-sibling",
        ),
        pytest.param(
            {
                f"{_PACKAGE}/spec.py": _IMPORT_PAYLOAD_URI
                + 'PAYLOAD = payload_uri(__file__, "valid_payload")\n'
                + 'RUNNERS = {"a": "runner.py"}\n',
                f"{_PACKAGE}/runner.py": "VALUE = 1\n",
            },
            "R3",
            "runner.py",
            id="R3-sibling-chosen-by-a-mapping",
        ),
    ],
)
def test_drift_names_each_undiscovered_payload_and_its_rule(
    tmp_path: Path, files: dict[str, str], rule: str, named: str
) -> None:
    """Flag the one file each rule exists for, beside a discovered payload.

    The discovered set is non-empty in every case, so a check that only asked
    whether the package had any discovered payload would pass them all.
    """
    root = _write(tmp_path, {_VALID_PAYLOAD: "VALUE = 1\n", **files})

    (violation,) = drift_violations(root, [root / _VALID_PAYLOAD])

    assert violation.startswith(f"{rule}: ")
    assert named in violation


@pytest.mark.parametrize(
    "importer",
    [
        "from .models import Thing\n",
        "from app.extensions.apps.example.models import Thing\n",
        "from app.extensions.apps.example import models\n",
    ],
    ids=["relative", "absolute-symbol", "absolute-module"],
)
def test_drift_exempts_a_sibling_the_app_imports_as_a_module(
    tmp_path: Path, importer: str
) -> None:
    """Accept a literal naming a module ``app/`` imports, under any import spelling."""
    root = _write(
        tmp_path,
        {
            _VALID_PAYLOAD: "VALUE = 1\n",
            f"{_PACKAGE}/spec.py": _IMPORT_PAYLOAD_URI
            + 'PAYLOAD = payload_uri(__file__, "valid_payload")\n'
            + 'SOURCE = "models.py"\n',
            f"{_PACKAGE}/models.py": "Thing = object\n",
            f"{_PACKAGE}/views.py": importer,
        },
    )

    assert drift_violations(root, [root / _VALID_PAYLOAD]) == []


@pytest.mark.parametrize(
    "call",
    [
        'payload_uri(__file__, "checks.py")',
        'payload_uri(anchor_file=__file__, name="checks.py")',
        'path.payload_uri(__file__, "checks.py")',
    ],
    ids=["positional", "keyword", "attribute"],
)
def test_discovery_follows_every_spelling_of_a_literal_reference(
    tmp_path: Path, call: str
) -> None:
    """Follow a literal reference to a ``.py`` payload outside the globs."""
    root = _write(
        tmp_path,
        {
            f"{_PACKAGE}/deps.py": "from app.core.utils import path\n"
            + _IMPORT_PAYLOAD_URI
            + f"PAYLOAD = {call}\n",
            f"{_PACKAGE}/checks.py": "VALUE = 1\n",
        },
    )

    assert discover_host_payloads(root) == [root / f"{_PACKAGE}/checks.py"]


def test_discovery_follows_an_aliased_payload_uri(tmp_path: Path) -> None:
    """Follow a literal reference made through an alias of ``payload_uri``."""
    root = _write(
        tmp_path,
        {
            f"{_PACKAGE}/deps.py": (
                "from app.core.utils.path import payload_uri as uri\n"
                'PAYLOAD = uri(__file__, "checks.py")\n'
            ),
            f"{_PACKAGE}/checks.py": "VALUE = 1\n",
        },
    )

    assert discover_host_payloads(root) == [root / f"{_PACKAGE}/checks.py"]


def test_discovery_reads_artifact_dirs_and_skips_other_interpreters(
    tmp_path: Path,
) -> None:
    """Take an artifact directory's ``*.py``, and drop another interpreter's script."""
    root = _write(
        tmp_path,
        {
            f"{_PACKAGE}/python_payload": "#!/usr/bin/env python3\nVALUE = 1\n",
            f"{_PACKAGE}/shell_payload": "#!/bin/bash\necho hi\n",
            "artifacts/collector.py": "VALUE = 1\n",
        },
    )

    assert discover_host_payloads(root, [root / "artifacts"]) == [
        root / "app/extensions/apps/example/python_payload",
        root / "artifacts/collector.py",
    ]


def test_a_configured_interpreter_of_another_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse a configured interpreter that is not the minimum host Python."""
    monkeypatch.setenv(CHECK_PYTHON_ENV, sys.executable)

    with pytest.raises(InterpreterMismatchError, match=re.escape("not 3.9")):
        resolve_py39_interpreter()


def test_a_configured_interpreter_that_does_not_exist_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Refuse a configured interpreter path with nothing behind it."""
    monkeypatch.setenv(CHECK_PYTHON_ENV, str(tmp_path / "python3.9"))

    with pytest.raises(InterpreterMismatchError, match="does not exist"):
        resolve_py39_interpreter()


def test_the_check_interpreter_is_the_minimum_host_python(py39: str) -> None:
    """Load under the oldest supported host Python, never a newer one."""
    reported = subprocess.run(
        [py39, "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert reported == str(MINIMUM_HOST_PYTHON)


@pytest.mark.parametrize(
    ("ci", "fatal"),
    [("true", True), ("1", True), ("", False), (None, False)],
    ids=["github", "numeric", "empty", "unset"],
)
def test_a_missing_interpreter_is_fatal_only_in_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, *, fatal: bool
) -> None:
    """Fail rather than skip in CI, where the interpreter is provisioned."""
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)

    assert missing_interpreter_is_fatal() is fatal


_UTC_IMPORT = "from datetime import UTC\n"
_PEP604 = "def describe(value: str | None) -> None:\n    return None\n"
_TOMLLIB_IN_BODY = (
    "def read(text):\n    import tomllib\n    return tomllib.loads(text)\n"
)
_CLEAN = "import json\n\n\ndef dump(value):\n    return json.dumps(value)\n"


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """Return a directory to write fixture payloads into, apart from the workdir."""
    directory = tmp_path / "fixtures"
    directory.mkdir()
    return directory


@pytest.mark.parametrize(
    ("source", "name", "loads"),
    [
        pytest.param(_UTC_IMPORT, "utc.py", False, id="module-level-datetime-UTC"),
        pytest.param(_PEP604, "pep604.py", False, id="pep604-without-future-import"),
        pytest.param(
            "from __future__ import annotations\n\n" + _PEP604,
            "pep604_future.py",
            True,
            id="pep604-with-future-import",
        ),
        pytest.param(_TOMLLIB_IN_BODY, "tomllib_payload", True, id="tomllib-in-body"),
        pytest.param(_CLEAN, "clean.py", True, id="clean-control"),
        pytest.param("import pymysql\n", "driver.py", True, id="driver-is-stubbed"),
        pytest.param(
            "import tomllib\n", "toml.py", False, id="newer-stdlib-is-not-stubbed"
        ),
        pytest.param(
            _UTC_IMPORT, "utc_payload", False, id="extensionless-file-is-executed"
        ),
        pytest.param(
            'if __name__ == "__main__":\n    raise SystemExit("entry point ran")\n',
            "entry_point.py",
            True,
            id="entry-point-does-not-run",
        ),
    ],
)
def test_the_load_branch_fails_exactly_what_3_9_cannot_load(
    py39: str,
    fixture_dir: Path,
    tmp_path: Path,
    source: str,
    name: str,
    *,
    loads: bool,
) -> None:
    """Fail a fixture that 3.9 cannot load, and pass one it can.

    The PEP 604 pair is what proves the worker compiles without inheriting its own
    ``__future__`` flags, the extensionless case that a file without ``.py`` is
    executed rather than skipped, and the entry-point case that the payload runs
    under a name other than ``__main__``.
    """
    payload = fixture_dir / name
    payload.write_text(source, encoding="utf-8")

    assert (load_under(py39, payload, tmp_path) is None) is loads


@pytest.mark.parametrize("code", [0, 1])
def test_the_load_branch_fails_a_payload_that_exits_at_import(
    py39: str, fixture_dir: Path, tmp_path: Path, code: int
) -> None:
    """Fail a payload that exits while loading, even with a success status."""
    payload = fixture_dir / "exits.py"
    payload.write_text(f"import sys\nsys.exit({code})\n", encoding="utf-8")

    error = load_under(py39, payload, tmp_path)

    assert error is not None
    assert f"sys.exit({code})" in error


@pytest.mark.parametrize(
    ("source", "name", "clean"),
    [
        pytest.param(_UTC_IMPORT, "utc.py", False, id="module-level-datetime-UTC"),
        pytest.param(_PEP604, "pep604.py", True, id="pep604-without-future-import"),
        pytest.param(_TOMLLIB_IN_BODY, "tomllib_payload", False, id="tomllib-in-body"),
        pytest.param(_CLEAN, "clean.py", True, id="clean-control"),
    ],
)
def test_the_static_branch_fails_exactly_what_needs_a_newer_python(
    fixture_dir: Path, source: str, name: str, *, clean: bool
) -> None:
    """Flag a newer standard-library API, even one only a function body uses.

    It passes the PEP 604 fixture the load branch fails, and fails the in-body
    ``tomllib`` fixture the load branch passes, which is why both run.
    """
    payload = fixture_dir / name
    payload.write_text(source, encoding="utf-8")

    assert (static_violations([payload]) is None) is clean


def test_the_load_branch_runs_the_payload_in_its_workdir(
    py39: str, fixture_dir: Path, tmp_path: Path
) -> None:
    """Point the payload's home and Nomad directories at the scratch workdir."""
    payload = fixture_dir / "where.py"
    payload.write_text(
        "import os\n"
        "assert os.getcwd() == os.environ['HOME'] == os.environ['NOMAD_TASK_DIR']\n"
        f"assert os.getcwd() == {os.fspath(tmp_path)!r}\n",
        encoding="utf-8",
    )

    assert load_under(py39, payload, tmp_path) is None
