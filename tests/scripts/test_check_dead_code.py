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

"""Tests for the ``scripts/check_dead_code.py`` CLI.

Every test runs the real vulture over a throwaway tree under ``tmp_path``; none
scans the repository, which the CI hook does.
"""

import importlib.metadata
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.scripts import ci_filter_globs, CI_WORKFLOW_PATH, load_script, PROJECT_ROOT

check_dead_code = load_script("check_dead_code")

BASELINE = "scripts/dead_code_baseline.toml"
HOOK_RUN = "pre-commit run check-dead-code --hook-stage manual --all-files"
COMMITTED_ENTRY_COUNT = 5


def write(root: Path, relpath: str, text: str) -> Path:
    """Write ``text`` to ``root/relpath``, creating parent directories.

    :param root: The throwaway repository root.
    :param relpath: POSIX path of the file, relative to ``root``.
    :param text: UTF-8 contents to write.
    :return: The written path.
    """
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def lock_text(version: str) -> str:
    """Return a minimal ``poetry.lock`` that locks vulture at ``version``.

    :param version: The vulture version to lock.
    :return: The lockfile text.
    """
    return f'[[package]]\nname = "vulture"\nversion = "{version}"\n'


def entry_text(
    path: str,
    kind: str,
    name: str,
    category: str = "dynamic",
    reason: str = "Reached by name.",
) -> str:
    """Return one baseline ``[[entry]]`` table.

    :param path: The entry's module path.
    :param kind: The vulture kind.
    :param name: The symbol name.
    :param category: The entry category.
    :param reason: Why the finding is kept.
    :return: The TOML text of the table.
    """
    return (
        "[[entry]]\n"
        f'path = "{path}"\n'
        f'kind = "{kind}"\n'
        f'name = "{name}"\n'
        f'category = "{category}"\n'
        f'reason = "{reason}"\n'
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Build the smallest repository the check can run against.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The tree's root.
    """
    for name in ("app", "tests", "scripts", "sidecar"):
        (tmp_path / name).mkdir()
    write(tmp_path, BASELINE, "")
    write(tmp_path, "poetry.lock", lock_text(importlib.metadata.version("vulture")))
    return tmp_path


def run_check(
    root: Path, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str]:
    """Run the check's ``main`` against ``root`` and capture what it says.

    :param root: The tree to check.
    :param capsys: pytest's output capture.
    :param args: Extra command-line arguments.
    :return: The exit status and the combined output, including a ``SystemExit`` message.
    """
    message = ""
    try:
        status = check_dead_code.main(["--root", str(root), *args])
    except SystemExit as exc:
        if isinstance(exc.code, str):
            status, message = 1, exc.code
        else:
            status = exc.code or 0
    captured = capsys.readouterr()
    return status, captured.out + captured.err + message


def test_clean_tree_passes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Pass a tree whose app code is all referenced, unreachable code included."""
    write(tree, "app/m.py", "def used():\n    return 1\n")
    write(
        tree,
        "tests/test_m.py",
        "from app.m import used\n\n\ndef test_used():\n    assert used()\n    return\n    yield\n",
    )

    status, output = run_check(tree, capsys)

    assert status == 0, output
    assert "0 entries" in output


def test_new_unused_function_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail an unreferenced module-level function under app/ with a ready entry block."""
    write(tree, "app/m.py", "def orphan():\n    return 1\n")

    status, output = run_check(tree, capsys)

    assert status == 1
    assert "app/m.py: unused function 'orphan' is new. Remove it." in output
    assert 'category = "dynamic"' in output
    assert 'name = "orphan"' in output


def test_stale_baseline_entry_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail a baseline entry vulture no longer reports, pointing at --update-baseline."""
    write(tree, BASELINE, entry_text("app/m.py", "function", "gone"))

    status, output = run_check(tree, capsys)

    assert status == 1
    assert (
        "app/m.py: function 'gone' is in the baseline but vulture no longer reports it"
        in output
    )
    assert "--update-baseline" in output


@pytest.mark.parametrize("relpath", ["app/bad.py", "tests/bad.py"])
def test_unparseable_file_fails(
    tree: Path, capsys: pytest.CaptureFixture[str], relpath: str
) -> None:
    """Fail on a file vulture cannot parse, in the reported tree or a reference tree."""
    write(tree, relpath, "def (:\n")

    status, output = run_check(tree, capsys)

    assert status == 1
    assert relpath in output
    assert "could not parse" in output


def test_dynamic_entry_keeps_finding(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pass a finding that a matching dynamic entry keeps."""
    write(tree, "app/m.py", "def hook():\n    return 1\n")
    write(tree, BASELINE, entry_text("app/m.py", "function", "hook"))

    status, output = run_check(tree, capsys)

    assert status == 0, output
    assert "1 entries" in output


def test_only_module_scope_variables_reported(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report module variables, but not class-body variables, arguments or locals."""
    write(
        tree,
        "app/m.py",
        "X = 1\nY = (\n    1\n)\n\n\nclass C:\n    y = 1\n\n\ndef f(a):\n    z = 1\n",
    )
    write(tree, "tests/test_m.py", "from app.m import C, f\n\nC()\nf(1)\n")

    status, output = run_check(tree, capsys)

    assert status == 1
    assert "unused variable 'X'" in output
    assert "unused variable 'Y'" in output
    for name in ("y", "a", "z"):
        assert f"'{name}'" not in output


def test_module_scope_walrus_bindings_reported(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report walrus targets evaluated in module scope, but not comprehension or lambda names."""
    write(
        tree,
        "app/m.py",
        "[(w_comp := n) for n in range(3)]\n"
        "\n\ndef f(arg=(w_default := 1)):\n    return arg\n"
        "\n\nclass C(dict, metaclass=(w_meta := type)):\n    pass\n"
        "\n\nlam = lambda p=(w_lambda := 2): p\n"
        "rows = [m for m in range(2)]\n",
    )
    write(
        tree,
        "tests/test_m.py",
        "from app.m import C, f, lam, rows\n\nprint(C, f, lam, rows)\n",
    )

    status, output = run_check(tree, capsys)

    assert status == 1
    for name in ("w_comp", "w_default", "w_meta", "w_lambda"):
        assert f"unused variable '{name}'" in output
    for name in ("n", "m", "p"):
        assert f"'{name}'" not in output


def test_migrations_are_references_not_findings(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Count migrations at any depth under app/ as references, never as findings."""
    write(tree, "app/core/h.py", "def helper():\n    return 1\n")
    migration = "from app.core.h import helper\n\n\ndef upgrade():\n    helper()\n"
    write(tree, "app/svc/migrations/v1.py", migration)
    write(tree, "app/extensions/apps/a/migrations/v1.py", migration)

    status, output = run_check(tree, capsys)

    assert status == 0, output


def test_sidecar_counts_as_reference(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Treat a helper called only from sidecar/ as live."""
    write(tree, "app/h.py", "def only_sidecar():\n    return 1\n")
    write(tree, "sidecar/s.py", "from app.h import only_sidecar\n\nonly_sidecar()\n")

    status, output = run_check(tree, capsys)

    assert status == 0, output


def test_decorator_registered_handler_ignored(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ignore a route handler registered by decorator."""
    write(tree, "app/r.py", '@router.get("/")\ndef handler():\n    return 1\n')

    status, output = run_check(tree, capsys)

    assert status == 0, output


def test_dipper_payloads_excluded(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exclude a dipper payload vulture cannot parse from the scan."""
    write(tree, "app/extensions/apps/dipper/payloads/p.py", "def (:\n")

    status, output = run_check(tree, capsys)

    assert status == 0, output


def test_vulture_noqa_under_app_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail a vulture noqa code under app/, which vulture itself would honour."""
    write(tree, "app/m.py", "def orphan():  # noqa: V103\n    return 1\n")

    status, output = run_check(tree, capsys)

    assert status == 1
    assert "app/m.py:1: a '# noqa: V…' comment hides a finding" in output


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        pytest.param(
            entry_text("app/m.py", "function", "hook", category="unused"),
            "entry 0",
            id="unknown-category",
        ),
        pytest.param(
            entry_text("app/m.py", "function", "hook", reason=""),
            "entry 0",
            id="empty-reason",
        ),
        pytest.param(
            entry_text("app/m.py", "function", "hook") + 'line = "3"\n',
            "entry 0",
            id="extra-key",
        ),
        pytest.param(
            '[[entry]]\npath = "app/m.py"\nkind = "function"\nname = "hook"\ncategory = "dynamic"\n',
            "entry 0",
            id="missing-key",
        ),
        pytest.param(
            entry_text("app/m.py", "function", "hook", category="catalog"),
            "entry 0",
            id="catalog-outside-catalog-module",
        ),
        pytest.param(
            entry_text("app/m.py", "import", "hook"),
            "entry 0",
            id="unreported-kind",
        ),
        pytest.param('extra = "x"\n', BASELINE, id="unknown-top-level-key"),
        pytest.param("entry = [1]\n", "entry 0", id="entry-not-a-table"),
        pytest.param('entry = "x"\n', BASELINE, id="entry-not-a-list"),
        pytest.param("[[entry]\n", BASELINE, id="invalid-toml"),
    ],
)
def test_malformed_baseline_rejected(
    tree: Path, capsys: pytest.CaptureFixture[str], baseline: str, expected: str
) -> None:
    """Reject a baseline breaking each shape rule, naming where it broke."""
    write(tree, "app/m.py", "def hook():\n    return 1\n")
    write(tree, BASELINE, baseline)

    status, output = run_check(tree, capsys)

    assert status == 1
    assert expected in output


def test_missing_baseline_rejected(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail on a missing baseline file, naming the path."""
    (tree / BASELINE).unlink()

    status, output = run_check(tree, capsys)

    assert status == 1
    assert BASELINE in output


def test_duplicate_findings_compared_as_multiset(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compare same-keyed findings as a multiset: one entry leaves one new finding."""
    write(
        tree,
        "app/m.py",
        "class A:\n    def go(self):\n        return 1\n"
        "\n\nclass B:\n    def go(self):\n        return 2\n",
    )
    write(tree, "tests/test_m.py", "from app.m import A, B\n\nprint(A, B)\n")
    write(tree, BASELINE, entry_text("app/m.py", "method", "go"))

    status, output = run_check(tree, capsys)

    assert status == 1
    assert output.count("unused method 'go' is new") == 1


def test_update_baseline_prunes_and_never_adds(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--update-baseline drops stale entries, keeps live ones, adds none, and is idempotent."""
    write(
        tree, "app/m.py", "def hook():\n    return 1\n\n\ndef orphan():\n    return 2\n"
    )
    write(
        tree,
        BASELINE,
        entry_text("app/m.py", "function", "hook")
        + "\n"
        + entry_text("app/m.py", "function", "gone"),
    )

    status, output = run_check(tree, capsys, "--update-baseline")
    first = (tree / BASELINE).read_text(encoding="utf-8")
    second_status, _ = run_check(tree, capsys, "--update-baseline")

    assert status == 0, output
    assert second_status == 0
    assert (tree / BASELINE).read_text(encoding="utf-8") == first
    entries = tomllib.loads(first)["entry"]
    assert [entry["name"] for entry in entries] == ["hook"]
    assert "orphan" in output


def test_rendered_baseline_round_trips() -> None:
    """Round-trip quotes, backslashes and non-ASCII characters through render and reload."""
    entry = check_dead_code.Entry(
        check_dead_code.Finding("app/m.py", "function", "hook"),
        "dynamic",
        'Called as "hook" via \\registry, é and \U0001f642.',
    )

    loaded = tomllib.loads(check_dead_code.render_baseline([entry]))

    assert loaded["entry"] == [
        {
            "path": "app/m.py",
            "kind": "function",
            "name": "hook",
            "category": "dynamic",
            "reason": entry.reason,
        }
    ]


def test_pin_mismatch_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Fail when the running vulture differs from the locked one, naming both pins."""
    write(tree, "poetry.lock", lock_text("0.0.1"))

    status, output = run_check(tree, capsys)

    assert status == 1
    assert importlib.metadata.version("vulture") in output
    assert "0.0.1" in output
    assert ".pre-commit-config.yaml" in output


def test_lockfile_without_vulture_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail on a lockfile that locks no vulture, naming poetry.lock."""
    write(tree, "poetry.lock", '[[package]]\nname = "ruff"\nversion = "1.0"\n')

    status, output = run_check(tree, capsys)

    assert status == 1
    assert "poetry.lock" in output


def test_ci_runs_hook_on_both_pins_and_baseline() -> None:
    """Run the manual-stage hook in CI when either pin or the baseline changes."""
    precommit = ci_filter_globs("precommit")
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["precommit-python"]["steps"]
    hook_steps = [step for step in steps if step.get("run") == HOOK_RUN]

    assert BASELINE in precommit
    assert ".pre-commit-config.yaml" in precommit
    assert "poetry.lock" in ci_filter_globs("python")
    assert len(hook_steps) == 1
    assert "outputs.python" in hook_steps[0]["if"]
    assert "outputs.precommit" in hook_steps[0]["if"]


def test_committed_baseline_is_well_formed() -> None:
    """Load the committed baseline and confirm it keeps only categorised entries."""
    entries = check_dead_code.load_baseline(PROJECT_ROOT / BASELINE)

    assert len(entries) == COMMITTED_ENTRY_COUNT
    assert {entry.category for entry in entries} <= {"catalog", "dynamic"}
