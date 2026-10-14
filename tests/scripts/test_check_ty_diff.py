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

"""Tests for the ``scripts/check_ty_diff.py`` CLI."""

import ast
import shutil
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from scripts.classify_ty_diagnostics import ReconciliationError
from tests.scripts import load_script, PROJECT_ROOT

if TYPE_CHECKING:
    from pathlib import Path

check_ty_diff = load_script("check_ty_diff")

# The summary table has four columns, so a well-formed row carries five bars.
SUMMARY_ROW_SEPARATORS = 5
MERGE_BASE = "0f1e2d3c4b5a"
BASE_SHA = "9a8b7c6d5e4f"

PYPROJECT = """
[tool.ty.rules]
all = "error"
unresolved-attribute = "warn"
invalid-argument-type = "warn"
invalid-return-type = "error"
missing-type-argument = "ignore"
"""

CRUD_ROW = (
    "app/inventory/crud.py:327:38: error[unresolved-attribute] "
    "Object of type `SyncHealthBase` has no attribute `id`"
)
CRUD_ROW_SECOND = (
    "app/inventory/crud.py:333:38: error[unresolved-attribute] "
    "Object of type `SyncHealthBase` has no attribute `id`"
)
DEPS_ROW = (
    "app/api/deps.py:296:24: error[invalid-argument-type] "
    "Argument to bound method `get_user` is incorrect"
)


def _output(*rows, total=None):
    """Render ``rows`` as ty concise output with a ``Found N diagnostics`` trailer.

    :param rows: Raw output lines, each newline-free.
    :param total: The count to write into the trailer; defaults to ``len(rows)``.
    :return: The rendered output text.
    """
    body = "".join(f"{row}\n" for row in rows)
    count = len(rows) if total is None else total
    return f"{body}Found {count} diagnostics\n"


class _TySpy:
    """Stand in for the ty subprocess, recording each pass's argv and cwd."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, cwd):
        """Record one invocation and return the next canned stdout."""
        self.calls.append((list(argv), cwd))
        return self.outputs.pop(0) if self.outputs else _output()

    @property
    def paths(self):
        """Return the file-path operands of each recorded invocation."""
        return [[arg for arg in argv if arg.endswith(".py")] for argv, _ in self.calls]


def _fake_git(root, name_status):
    """Return a ``_git`` stand-in answering each git query the runner issues.

    :param root: Repository root the runner should resolve.
    :param name_status: Raw ``git diff --name-status`` output to serve.
    :return: A callable with ``_git``'s signature.
    """

    def run(*args, cwd=None):
        if args[0] == "rev-parse":
            return f"{root}\n"
        if args[0] == "merge-base":
            return f"{MERGE_BASE}\n"
        if args[0] == "diff":
            return name_status
        return ""

    return run


def _run_main(monkeypatch, tmp_path, name_status, *outputs, argv=None):
    """Drive ``main`` with both subprocess boundaries stubbed.

    :param monkeypatch: pytest's monkeypatch fixture.
    :param tmp_path: Directory standing in for the repository root.
    :param name_status: Raw ``git diff --name-status`` output to serve.
    :param outputs: Canned ty stdout, head pass first.
    :param argv: CLI arguments; defaults to ``--base-sha <BASE_SHA>``.
    :return: A ``(exit_code, spy)`` pair.
    """
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    spy = _TySpy(*outputs)
    monkeypatch.setattr(check_ty_diff, "_git", _fake_git(tmp_path, name_status))
    monkeypatch.setattr(check_ty_diff, "_ty_stdout", spy)
    code = check_ty_diff.main(argv if argv is not None else ["--base-sha", BASE_SHA])
    return code, spy


RULE = "invalid-argument-type"
OTHER_RULE = "unresolved-attribute"


def _row(path, line, column, message, rule=RULE):
    """Render one ty concise output row.

    :param path: The repo-relative path the row points into.
    :param line: 1-indexed line.
    :param column: 1-indexed character column.
    :param message: The diagnostic's message.
    :param rule: The ty rule that fired.
    :return: The row, as ty prints it.
    """
    return f"{path}:{line}:{column}: error[{rule}] {message}"


def _diagnostic(line, column=1, *, path="app/x.py", rule=RULE, message="m"):
    """Build a diagnostic with only the fields the delta key reads varying.

    :param line: 1-indexed line.
    :param column: 1-indexed character column.
    :param path: The repo-relative path the diagnostic points into.
    :param rule: The ty rule that fired.
    :param message: The diagnostic's message.
    :return: The diagnostic.
    """
    return check_ty_diff.Diagnostic(
        path=path,
        line=line,
        column=column,
        severity="error",
        rule=rule,
        message=message,
    )


def _source(text):
    """Parse dedented ``text`` the way the runner parses a checked file.

    :param text: Python source, possibly indented as a test literal.
    :return: The parsed source.
    """
    return check_ty_diff.Source.parse(textwrap.dedent(text))


def _key(text, line, column=1, **fields):
    """Return the delta key of a diagnostic at ``line``/``column`` in ``text``.

    :param text: Python source, possibly indented as a test literal.
    :param line: 1-indexed line.
    :param column: 1-indexed character column.
    :param fields: Overrides for :func:`_diagnostic`'s keyword fields.
    :return: The diagnostic's key.
    """
    return check_ty_diff.delta_key(_diagnostic(line, column, **fields), _source(text))


def _write_tree(root, files):
    """Write ``files`` under ``root``, dedenting each one.

    :param root: The directory the paths are relative to.
    :param files: Repo-relative path to Python source.
    """
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")


def _run_main_with_sources(
    monkeypatch, tmp_path, name_status, head_files, base_files, *outputs, argv=None
):
    """Drive ``main`` with real source files on both sides of the comparison.

    The base tree is removed when its block exits, as the real worktree is, so a
    runner that read base sources after the block would see none of them.

    :param monkeypatch: pytest's monkeypatch fixture.
    :param tmp_path: The repository root, holding the head sources.
    :param name_status: Canned ``git diff --name-status`` output.
    :param head_files: Repo-relative path to head-side source.
    :param base_files: Repo-relative path to base-side source.
    :param outputs: Canned ty stdout, head pass first.
    :param argv: CLI arguments; defaults to ``--base-sha <BASE_SHA>``.
    :return: A ``(exit_code, spy, base_root)`` triple.
    """
    _write_tree(tmp_path, head_files)
    base_root = tmp_path / "base-tree"

    @contextmanager
    def fake_base_tree(merge_base):
        _write_tree(base_root, base_files)
        try:
            yield base_root
        finally:
            shutil.rmtree(base_root)

    monkeypatch.setattr(check_ty_diff, "base_tree", fake_base_tree)
    code, spy = _run_main(monkeypatch, tmp_path, name_status, *outputs, argv=argv)
    return code, spy, base_root


CALL_SITE = """\
from app.core import create_app


def build():
    return create_app(settings=None)
"""
# The same call site two lines lower, below an unrelated import.
CALL_SITE_SHIFTED = """\
from app.core import create_app
from app.core import settings_for


def build():

    return create_app(settings=None)
"""
CALL_SITE_EDITED = """\
from app.core import create_app


def build():
    return create_app(settings=None, debug=True)
"""
DECORATED = """\
@route("/a")
def build():
    return 1
"""
TRY_EXCEPT = """\
try:
    run()
except Missing as exc:
    pass
"""
BASE_MESSAGE = "Expected `int | str`, found `None`"
HEAD_MESSAGE = "Expected `str | int`, found `None`"


def test_surplus_at_head_fails_and_prints_the_diagnostic(monkeypatch, tmp_path, capsys):
    """Report a diagnostic the base pass did not carry, and exit non-zero."""
    code, _ = _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/inventory/crud.py\n",
        _output(CRUD_ROW),
        _output(),
    )

    assert code == 1
    assert "app/inventory/crud.py:327" in capsys.readouterr().out


def test_pre_existing_diagnostic_is_not_surplus(monkeypatch, tmp_path):
    """Stay green when the same fingerprint is present at the merge-base."""
    code, _ = _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/api/deps.py\n",
        _output(DEPS_ROW),
        _output(DEPS_ROW),
    )

    assert code == 0


def test_duplicating_an_existing_diagnostic_is_surplus(monkeypatch, tmp_path):
    """Count fingerprints as a multiset, so a second copy still reports."""
    code, _ = _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/inventory/crud.py\n",
        _output(CRUD_ROW, CRUD_ROW_SECOND),
        _output(CRUD_ROW),
    )

    assert code == 1


def test_base_pass_omits_only_the_file_absent_at_base(monkeypatch, tmp_path):
    """Pass an identical batch to both ty passes, minus paths the branch added."""
    _, spy = _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/inventory/crud.py\nA\tapp/inventory/new.py\n",
        _output(),
        _output(),
    )

    head_paths, base_paths = spy.paths
    assert head_paths == ["app/inventory/crud.py", "app/inventory/new.py"]
    assert base_paths == ["app/inventory/crud.py"]


def test_a_failed_ty_run_is_not_read_as_output(tmp_path):
    """Reject any ty status but 0 and 1, so a broken run cannot read as clean."""
    with pytest.raises(check_ty_diff.TyInvocationError):
        check_ty_diff._ty_stdout(
            [sys.executable, "-c", "raise SystemExit(2)"], tmp_path
        )


def test_a_diagnostic_exit_is_ordinary_output(tmp_path):
    """Return stdout on exit 1, which is what a promoted run normally exits."""
    argv = [sys.executable, "-c", "print('hi'); raise SystemExit(1)"]

    assert check_ty_diff._ty_stdout(argv, tmp_path).strip() == "hi"


def test_summary_escapes_the_pipes_in_a_union_type(tmp_path):
    """Keep a union-typed message inside its own Markdown table column."""
    diagnostic = check_ty_diff.Diagnostic(
        path="app/x.py",
        line=7,
        column=1,
        severity="error",
        rule="invalid-argument-type",
        message="Expected `int | None`, found `int | float`",
    )
    summary = tmp_path / "summary.md"

    check_ty_diff.write_summary([diagnostic], summary)

    row = next(
        line
        for line in summary.read_text(encoding="utf-8").splitlines()
        if "app/x.py" in line
    )
    assert row.count("|") - row.count("\\|") == SUMMARY_ROW_SEPARATORS
    assert "`int \\| None`" in row


def test_all_checks_passed_is_a_clean_run():
    """Read ty's zero-diagnostic sentinel as empty rather than as truncation."""
    assert check_ty_diff.parse_ty_output("All checks passed!\n") == []


def test_truncated_output_raises():
    """Refuse output that carries rows but no trailer and no sentinel."""
    with pytest.raises(ReconciliationError):
        check_ty_diff.parse_ty_output(f"{CRUD_ROW}\n")


def test_trailer_disagreement_raises():
    """Refuse output whose trailer disagrees with the rows parsed."""
    with pytest.raises(ReconciliationError):
        check_ty_diff.parse_ty_output(_output(CRUD_ROW, total=7))


def test_promoted_rules_are_derived_from_pyproject(tmp_path):
    """Promote every rule held at ``warn``, including ones with no current hits."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")

    assert check_ty_diff.promoted_rules(pyproject) == (
        "invalid-argument-type",
        "unresolved-attribute",
    )


def test_argv_forces_exclusions_pins_the_interpreter_and_promotes_each_rule():
    """Re-establish ``[tool.ty.src]``, pin the interpreter, and promote each rule.

    ``--python`` is what decides which environment ty resolves imports against;
    without it ty reads whichever interpreter is first on ``PATH`` and reports
    every third-party import as unresolved.
    """
    argv = check_ty_diff.ty_argv(
        check_ty_diff.resolve_ty(), ("unresolved-attribute",), ("app/x.py",)
    )

    assert "--force-exclude" in argv
    assert argv[argv.index("--python") + 1] == sys.prefix
    assert argv[argv.index("--error") + 1] == "unresolved-attribute"
    assert argv[-1] == "app/x.py"


def test_ty_is_resolved_beside_the_interpreter_not_on_path():
    """Resolve the pinned binary from the running interpreter's own directory."""
    resolved = check_ty_diff.resolve_ty()

    assert resolved.parent == check_ty_diff.Path(sys.executable).parent
    assert str(resolved) != "ty"


def test_test_paths_are_dropped():
    """Leave ``tests/`` out of both passes, per the measured block rate."""
    changed = check_ty_diff.parse_name_status(
        "M\tapp/core/db.py\nM\ttests/app/core/test_db.py\n"
    )

    assert changed.head == ("app/core/db.py",)
    assert changed.base == ("app/core/db.py",)


def test_deleted_paths_are_excluded_by_the_diff_filter(monkeypatch, tmp_path):
    """Ask git for ``ACMR`` only, so no removed path is handed to ty."""
    recorded: list[tuple[str, ...]] = []

    def spy_git(*args, cwd=None):
        recorded.append(args)
        return f"{tmp_path}\n" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(check_ty_diff, "_git", spy_git)
    check_ty_diff.changed_files(MERGE_BASE)

    diff_args = next(args for args in recorded if args[0] == "diff")
    assert "--diff-filter=ACMR" in diff_args


def test_renames_map_the_base_pass_back_to_the_old_path():
    """Read a renamed file at its old path in the base tree."""
    changed = check_ty_diff.parse_name_status(
        "R088\tapp/extensions/apps/snippets/models.py\tapp/extensions/snippets/models.py\n"
    )

    assert changed.head == ("app/extensions/snippets/models.py",)
    assert changed.base == ("app/extensions/apps/snippets/models.py",)


def test_a_move_out_of_tests_leaves_the_base_pass_empty():
    """Give a file promoted out of ``tests/`` no baseline to cancel against."""
    changed = check_ty_diff.parse_name_status(
        "R091\ttests/app/core/helper.py\tapp/core/helper.py\n"
    )

    assert changed.head == ("app/core/helper.py",)
    assert changed.base == ()
    assert changed.renames == {}


def test_a_move_out_of_tests_reports_the_diagnostics_it_carries(monkeypatch, tmp_path):
    """Count a file's diagnostics as new when the move puts them in production."""
    moved_row = CRUD_ROW.replace("app/inventory/crud.py", "app/inventory/moved.py")
    code, spy = _run_main(
        monkeypatch,
        tmp_path,
        "R091\ttests/app/inventory/helper.py\tapp/inventory/moved.py\n",
        _output(moved_row),
    )

    assert code == 1
    assert spy.paths == [["app/inventory/moved.py"]]


def test_a_rename_does_not_make_its_existing_diagnostics_surplus(monkeypatch, tmp_path):
    """Compare a moved file's diagnostics under the name the branch gives it."""
    moved_row = CRUD_ROW.replace("app/inventory/crud.py", "app/inventory/moved.py")
    code, _ = _run_main(
        monkeypatch,
        tmp_path,
        "R091\tapp/inventory/crud.py\tapp/inventory/moved.py\n",
        _output(moved_row),
        _output(CRUD_ROW),
    )

    assert code == 0


def test_empty_change_set_never_invokes_ty(monkeypatch, tmp_path):
    """Exit clean without running ty when no non-test Python file changed."""
    code, spy = _run_main(
        monkeypatch, tmp_path, "M\ttests/app/core/test_db.py\nM\tREADME.md\n"
    )

    assert code == 0
    assert spy.calls == []


def _git_with_status(root, name_status, status):
    """Return a ``_git`` stand-in that also serves ``git status`` output.

    :param root: Repository root the runner should resolve.
    :param name_status: Raw ``git diff --name-status`` output to serve.
    :param status: Raw ``git status --porcelain`` output to serve.
    :return: A callable with ``_git``'s signature.
    """
    fallback = _fake_git(root, name_status)

    def run(*args, cwd=None):
        if args[0] == "status":
            return status
        return fallback(*args, cwd=cwd)

    return run


def test_empty_change_set_names_the_test_files_it_did_not_check(
    monkeypatch, tmp_path, capsys
):
    """Say that changed tests were skipped, since a zero exit reads as a pass."""
    code, _ = _run_main(
        monkeypatch, tmp_path, "M\ttests/app/core/test_db.py\nM\tREADME.md\n"
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "1 changed test file(s) were NOT examined" in out
    assert "uncommitted" not in out


def test_empty_change_set_names_uncommitted_python_files(monkeypatch, tmp_path, capsys):
    """Say that uncommitted edits were skipped when the committed diff is empty."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    status = " M app/core/db.py\n?? app/core/new_module.py\n"
    monkeypatch.setattr(check_ty_diff, "_git", _git_with_status(tmp_path, "", status))
    code = check_ty_diff.main(["--base-sha", BASE_SHA])

    out = capsys.readouterr().out
    assert code == 0
    assert "2 uncommitted non-test Python file(s) were NOT examined" in out
    assert "This is not a pass for them." in out


def test_a_clean_empty_change_set_prints_only_the_plain_line(
    monkeypatch, tmp_path, capsys
):
    """Add nothing when there is genuinely no Python change to report on."""
    code, _ = _run_main(monkeypatch, tmp_path, "M\tREADME.md\n")

    assert code == 0
    assert capsys.readouterr().out == "No non-test Python files changed.\n"


def test_a_passing_run_still_names_what_it_did_not_check(monkeypatch, tmp_path, capsys):
    """Report skipped changes beside a clean verdict on the files that were checked."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    name_status = "M\tapp/inventory/crud.py\nM\ttests/app/inventory/test_crud.py\n"
    monkeypatch.setattr(
        check_ty_diff,
        "_git",
        _git_with_status(tmp_path, name_status, " M app/core/db.py\n"),
    )
    monkeypatch.setattr(check_ty_diff, "_ty_stdout", _TySpy(_output(), _output()))
    code = check_ty_diff.main(["--base-sha", BASE_SHA])

    out = capsys.readouterr().out
    assert code == 0
    assert "No new ty diagnostics across 1 changed file(s)." in out
    assert "1 uncommitted non-test Python file(s) were NOT examined" in out
    assert "1 changed test file(s) were NOT examined" in out


def test_an_uncommitted_edit_to_a_checked_file_is_not_called_unexamined(
    monkeypatch, tmp_path, capsys
):
    """Count a dirty file the head pass read from disk as examined."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(
        check_ty_diff,
        "_git",
        _git_with_status(
            tmp_path, "M\tapp/inventory/crud.py\n", " M app/inventory/crud.py\n"
        ),
    )
    monkeypatch.setattr(check_ty_diff, "_ty_stdout", _TySpy(_output(), _output()))
    code = check_ty_diff.main(["--base-sha", BASE_SHA])

    out = capsys.readouterr().out
    assert code == 0
    assert out == "No new ty diagnostics across 1 changed file(s).\n"


def test_an_uncommitted_test_edit_is_named(monkeypatch, tmp_path, capsys):
    """Name a dirty test file, counting it once when it is also committed."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    status = " M tests/app/core/test_db.py\n?? tests/app/core/test_new.py\n"
    monkeypatch.setattr(
        check_ty_diff,
        "_git",
        _git_with_status(tmp_path, "M\ttests/app/core/test_db.py\n", status),
    )
    code = check_ty_diff.main(["--base-sha", BASE_SHA])

    out = capsys.readouterr().out
    assert code == 0
    assert "2 changed test file(s) were NOT examined" in out
    assert "uncommitted non-test" not in out


def test_uncommitted_files_keeps_only_what_a_commit_would_bring_in(
    monkeypatch, tmp_path
):
    """Leave out deletions, keep tests, and read a rename at its new path."""
    status = (
        " M app/core/db.py\n"
        " D app/core/old.py\n"
        "D  app/core/gone.py\n"
        "MD app/core/staged.py\n"
        "AD app/core/added.py\n"
        " M tests/app/core/test_db.py\n"
        "?? app/core/new_module.py\n"
        "R  app/core/a.py -> app/core/b.py\n"
    )
    monkeypatch.setattr(check_ty_diff, "_git", _git_with_status(tmp_path, "", status))

    assert check_ty_diff.uncommitted_files(tmp_path) == (
        "app/core/db.py",
        "app/core/staged.py",
        "app/core/added.py",
        "tests/app/core/test_db.py",
        "app/core/new_module.py",
        "app/core/b.py",
    )


def test_uncommitted_files_asks_git_from_the_repository_root(monkeypatch, tmp_path):
    """Resolve the pathspec against the root, and list untracked files whatever the config."""
    recorded: list[tuple[tuple[str, ...], Path | None]] = []

    def spy_git(*args, cwd=None):
        recorded.append((args, cwd))
        return ""

    monkeypatch.setattr(check_ty_diff, "_git", spy_git)
    check_ty_diff.uncommitted_files(tmp_path)

    args, cwd = recorded[0]
    assert cwd == tmp_path
    assert "--untracked-files=all" in args


def test_changed_test_files_are_recorded_but_not_checked():
    """Keep ``tests/`` out of both passes while remembering what was skipped."""
    changed = check_ty_diff.parse_name_status(
        "M\tapp/core/db.py\nM\ttests/app/core/test_db.py\n"
    )

    assert changed.head == ("app/core/db.py",)
    assert changed.tests == ("tests/app/core/test_db.py",)


def test_per_file_matches_the_batched_verdict(monkeypatch, tmp_path):
    """Reach the same verdict one path at a time as in a single batch."""
    name_status = "M\tapp/inventory/crud.py\nM\tapp/api/deps.py\n"
    code, spy = _run_main(
        monkeypatch,
        tmp_path,
        name_status,
        _output(CRUD_ROW),
        _output(DEPS_ROW),
        _output(),
        _output(DEPS_ROW),
        argv=["--base-sha", BASE_SHA, "--per-file"],
    )

    assert code == 1
    assert spy.paths == [
        ["app/inventory/crud.py"],
        ["app/api/deps.py"],
        ["app/inventory/crud.py"],
        ["app/api/deps.py"],
    ]


def test_module_invocation_resolves_the_sibling_import():
    """Run the Make target's own ``-m`` form, which pytest's path cannot mask."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_ty_diff", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_annotations_carry_the_head_side_line(monkeypatch, tmp_path, capsys):
    """Emit one workflow annotation per surplus diagnostic, at its head line."""
    _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/inventory/crud.py\n",
        _output(CRUD_ROW),
        _output(),
    )

    out = capsys.readouterr().out
    assert (
        "::warning file=app/inventory/crud.py,line=327,"
        "title=unresolved-attribute::Object of type `SyncHealthBase` "
        "has no attribute `id`" in out
    )


def test_annotation_cap_holds_while_the_summary_stays_complete(
    monkeypatch, tmp_path, capsys
):
    """Cap the annotations but write every surplus row to the step summary."""
    surplus = check_ty_diff.ANNOTATION_CAP + 3
    rows = [
        f"app/inventory/crud.py:{line}:1: error[unresolved-attribute] "
        f"Object of type `SyncHealthBase` has no attribute `id{line}`"
        for line in range(1, surplus + 1)
    ]
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    _run_main(
        monkeypatch,
        tmp_path,
        "M\tapp/inventory/crud.py\n",
        _output(*rows),
        _output(),
    )

    out = capsys.readouterr().out
    assert out.count("::warning file=") == check_ty_diff.ANNOTATION_CAP
    assert "+3 more" in out
    assert summary.read_text(encoding="utf-8").count("app/inventory/crud.py") == surplus


class TestDeltaKey:
    """Key a diagnostic on its rule and enclosing statement, never its message."""

    def test_message_text_is_not_part_of_the_key(self):
        """Match two diagnostics on one statement whatever their messages say."""
        assert _key(CALL_SITE, 5, message=BASE_MESSAGE) == _key(
            CALL_SITE, 5, message=HEAD_MESSAGE
        )

    def test_rule_is_part_of_the_key(self):
        """Keep diagnostics of different rules on one statement apart."""
        assert _key(CALL_SITE, 5) != _key(CALL_SITE, 5, rule=OTHER_RULE)

    def test_path_is_part_of_the_key(self):
        """Keep identical statements in different files apart."""
        assert _key(CALL_SITE, 5) != _key(CALL_SITE, 5, path="app/y.py")

    def test_lines_inserted_above_do_not_change_the_key(self):
        """Match a statement that only moved down the file."""
        assert _key(CALL_SITE, 5) == _key(CALL_SITE_SHIFTED, 7)

    def test_editing_the_statement_changes_the_key(self):
        """Separate a statement from its edited form."""
        assert _key(CALL_SITE, 5) != _key(CALL_SITE_EDITED, 5)

    def test_comments_and_formatting_do_not_change_the_key(self):
        """Match a statement that was only reformatted or commented."""
        reformatted = """\
        from app.core import create_app


        def build():
            return create_app(  # the factory
                settings=None,
            )
        """

        assert _key(CALL_SITE, 5) == _key(reformatted, 6)

    def test_a_continuation_line_keys_on_the_whole_statement(self):
        """Anchor a diagnostic on a continuation line to its whole statement."""
        multi_line = """\
        value = compute(
            first,
            second,
        )
        """

        assert _key(multi_line, 3) == _key("value = compute(first, second)\n", 1)

    def test_a_body_statement_ignores_its_siblings(self):
        """Anchor a body diagnostic to its own statement, not the function."""
        before = """\
        def build():
            prepare()
            return create_app(settings=None)
        """
        after = """\
        def build():
            prepare(verbose=True)
            return create_app(settings=None)
        """

        assert _key(before, 3) == _key(after, 3)

    def test_a_header_diagnostic_ignores_the_body(self):
        """Match a ``def``-line diagnostic across edits to the body below it."""
        before = """\
        def build(settings: int = None):
            return 1
        """
        after = """\
        def build(settings: int = None):
            log()
            return 2
        """

        assert _key(before, 1) == _key(after, 1)

    def test_a_header_diagnostic_follows_the_signature(self):
        """Separate a ``def``-line diagnostic once the signature changes."""
        before = "def build(settings: int = None):\n    return 1\n"
        after = "def build(settings: str = None):\n    return 1\n"

        assert _key(before, 1) != _key(after, 1)

    def test_a_decorator_line_belongs_to_the_decorated_definition(self):
        """Anchor a decorator-line diagnostic to the decorated definition."""
        assert _key(DECORATED, 1) == _key(DECORATED, 2)

    def test_changing_a_decorator_changes_the_key(self):
        """Separate a decorator-line diagnostic once the decorator changes."""
        redecorated = DECORATED.replace('"/a"', '"/b"')

        assert _key(DECORATED, 1) != _key(redecorated, 1)

    @pytest.mark.parametrize(
        ("column", "statement"),
        [(1, "first = f(a)\n"), (15, "second = g(b)\n")],
        ids=["first", "second"],
    )
    def test_the_column_selects_between_statements_on_one_line(self, column, statement):
        """Pick the statement the column points at when a line holds two."""
        line = "first = f(a); second = g(b)\n"

        assert _key(line, 1, column=column) == _key(statement, 1)

    def test_the_column_counts_characters_not_bytes(self):
        """Convert ty's character column to the parser's byte offset."""
        # "é" is one character but two UTF-8 bytes: ty puts `t` at column 10,
        # the AST at byte offset 10, so an uncorrected count lands on `s = "é"`.
        line = 's = "é"; t = f(s)\n'

        assert _key(line, 1, column=10) == _key("t = f(s)\n", 1)

    def test_a_diagnostic_past_the_end_of_a_statement_stays_on_its_line(self):
        """Anchor a diagnostic on a trailing comment to its line's statement."""
        line = "value = f(a)  # ty: ignore[invalid-argument-type]\n"

        assert _key(line, 1, column=20) == _key("value = f(a)\n", 1)

    def test_an_except_clause_keys_on_the_handler_not_the_try(self):
        """Match an ``except``-line diagnostic across edits to the ``try`` body."""
        after = TRY_EXCEPT.replace("run()", "run(retries=3)")

        assert _key(TRY_EXCEPT, 3) == _key(after, 3)

    def test_an_except_clause_follows_its_exception_type(self):
        """Separate an ``except``-line diagnostic once the caught type changes."""
        after = TRY_EXCEPT.replace("Missing", "Other")

        assert _key(TRY_EXCEPT, 3) != _key(after, 3)

    @pytest.mark.parametrize(
        ("edit", "same"),
        [
            (("Point(x)", "Point(y)"), False),
            (("if x > 0", "if x > 1"), False),
            (("handle(x)", "handle(x, now=True)"), True),
            (("drop()", "drop(all=True)"), True),
        ],
        ids=["pattern", "guard", "own-body", "other-case"],
    )
    def test_a_case_line_keys_on_its_own_pattern_and_guard(self, edit, same):
        """Anchor a ``case``-line diagnostic to that case's header alone."""
        before = """\
        match value:
            case Point(x) if x > 0:
                handle(x)
            case _:
                drop()
        """

        assert (
            _key(before, 2, column=10) == _key(before.replace(*edit), 2, column=10)
        ) is same

    def test_a_case_body_statement_ignores_other_cases(self):
        """Anchor a diagnostic in a case body to its own statement."""
        before = "match v:\n    case 1:\n        go(v)\n    case _:\n        stop()\n"
        after = before.replace("stop()", "stop(now=True)")

        assert _key(before, 3, column=9) == _key(after, 3, column=9)

    @pytest.mark.parametrize(
        ("before", "after", "column"),
        [
            ("if bad: use()\n", "if worse: use()\n", 4),
            ("while bad: use()\n", "while worse: use()\n", 7),
            (
                "try: run()\nexcept Missing: pass\n",
                "try: run()\nexcept Other: pass\n",
                8,
            ),
        ],
        ids=["if", "while", "except"],
    )
    def test_a_one_line_header_keys_on_the_header_not_its_body(
        self, before, after, column
    ):
        """Separate a header diagnostic on a one-line block once the header changes."""
        line = before.count("\n")

        assert _key(before, line, column=column) != _key(after, line, column=column)

    def test_a_column_inside_the_indentation_keys_on_the_line_statement(self):
        """Anchor a column before an indented statement to that statement."""
        body = "if ok:\n    use(bad)\n"

        assert _key(body, 2, column=1) == _key(body, 2, column=5)

    def test_a_one_line_body_diagnostic_keys_on_the_body(self):
        """Anchor a diagnostic after a one-line header's colon to the body."""
        assert _key("if ok: use(bad)\n", 1, column=8) == _key("use(bad)\n", 1)

    def test_an_elif_condition_keys_on_its_own_branch(self):
        """Anchor an ``elif``-line diagnostic to that branch's condition."""
        before = """\
        if ready:
            go()
        elif check(x):
            wait()
        """
        after = before.replace("go()", "go(now=True)")

        assert _key(before, 3) == _key(after, 3)

    def test_a_form_feed_does_not_shift_the_line_count(self):
        """Number lines as the parser does, not as ``str.splitlines`` does."""
        # str.splitlines() treats \x0c as a line break; the parser does not.
        text = "x = 1  # \x0c\ny = f(x)\n"

        assert _key(text, 2) == _key("y = f(x)\n", 1)

    def test_a_missing_source_falls_back_to_the_message(self):
        """Key on the message when the file could not be parsed."""
        key = check_ty_diff.delta_key(_diagnostic(5, message=BASE_MESSAGE), None)

        assert key == check_ty_diff.DeltaKey(
            "app/x.py", RULE, check_ty_diff.AnchorKind.MESSAGE, BASE_MESSAGE
        )

    @pytest.mark.parametrize("line", [0, -1, 99])
    def test_a_line_outside_the_file_falls_back_to_the_message(self, line):
        """Key on the message when the line is not in the file."""
        assert _key(CALL_SITE, line).kind == check_ty_diff.AnchorKind.MESSAGE

    @pytest.mark.parametrize("line", [2, 3, 5, 6])
    def test_a_blank_or_comment_line_falls_back_to_the_message(self, line):
        """Key on the message when the line holds no code."""
        # Lines 5 and 6 sit inside a function, which would otherwise claim them.
        text = "x = 1\n\n# a comment\ndef build():\n\n    # inner\n    return 1\n"

        assert _key(text, line).kind == check_ty_diff.AnchorKind.MESSAGE

    def test_a_line_outside_every_statement_falls_back_to_the_message(self):
        """Key on the message when no statement spans a line that holds code."""
        # A lone continuation backslash joins the next line, but the statement
        # it joins starts on that next line, so nothing spans this one.
        text = "x = 1\n\\\ny = 2\n"

        assert _key(text, 2).kind == check_ty_diff.AnchorKind.MESSAGE

    def test_a_fallback_key_never_equals_a_statement_key(self):
        """Tag the anchor kind so a message can never pose as a statement."""
        statement = _key("m\n", 1, message="m")
        fallback = check_ty_diff.delta_key(_diagnostic(1, message="m"), None)

        assert statement.kind == check_ty_diff.AnchorKind.STATEMENT
        assert statement != fallback


class TestParseSources:
    """Read the checked files, leaving out any that cannot be keyed."""

    def test_a_valid_file_is_parsed(self, tmp_path):
        """Return the parsed tree and lines of a readable file."""
        _write_tree(tmp_path, {"app/x.py": "x = 1\n"})

        sources = check_ty_diff.parse_sources(tmp_path, ["app/x.py"])

        assert isinstance(sources["app/x.py"].tree, ast.Module)
        assert sources["app/x.py"].lines[0] == "x = 1"

    def test_a_missing_file_is_left_out(self, tmp_path):
        """Leave out a path that does not exist on that side."""
        assert check_ty_diff.parse_sources(tmp_path, ["app/gone.py"]) == {}

    @pytest.mark.parametrize(
        "content",
        [
            b"def broken(:\n",
            b"x = '\xff'\n",
            b"x = 1\x00\n",
        ],
        ids=["syntax-error", "not-utf8", "null-byte"],
    )
    def test_an_unparseable_file_is_left_out(self, tmp_path, content):
        """Leave out a file that cannot be decoded or parsed."""
        (tmp_path / "x.py").write_bytes(content)

        assert check_ty_diff.parse_sources(tmp_path, ["x.py"]) == {}

    def test_a_parser_recursion_failure_is_left_out(self, tmp_path, monkeypatch):
        """Leave out a file nested too deeply for the parser."""
        (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")

        def explode(*args, **kwargs):
            raise RecursionError

        monkeypatch.setattr(check_ty_diff.Source, "parse", explode)

        assert check_ty_diff.parse_sources(tmp_path, ["x.py"]) == {}

    def test_a_path_outside_the_root_is_not_read(self, tmp_path):
        """Refuse a path that resolves outside the tree being read."""
        root = tmp_path / "repo"
        root.mkdir()
        (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")

        assert check_ty_diff.parse_sources(root, ["../outside.py"]) == {}


class TestMessageRewriteIsNotSurplus:
    """Stay green when the branch only changes what a pre-existing diagnostic says."""

    @pytest.mark.parametrize(
        ("head_rows", "base_rows", "extra_argv"),
        [
            pytest.param(
                [_row("app/x.py", 7, 12, HEAD_MESSAGE)],
                [_row("app/x.py", 5, 12, BASE_MESSAGE)],
                [],
                id="rewritten-message",
            ),
            pytest.param(
                [_row("app/x.py", 7, 12, HEAD_MESSAGE)],
                [_row("app/x.py", 5, 12, BASE_MESSAGE)],
                ["--per-file"],
                id="rewritten-message-per-file",
            ),
            # Accepted limit: a same-rule swap at unchanged multiplicity cancels.
            pytest.param(
                [_row("app/x.py", 7, 12, "c"), _row("app/x.py", 7, 30, "d")],
                [_row("app/x.py", 5, 12, "a"), _row("app/x.py", 5, 30, "b")],
                [],
                id="same-statement-swap",
            ),
        ],
    )
    def test_a_rewritten_message_on_an_unchanged_statement_passes(
        self, monkeypatch, tmp_path, head_rows, base_rows, extra_argv
    ):
        """Exit clean when only the messages on an unchanged statement changed."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE_SHIFTED},
            {"app/x.py": CALL_SITE},
            _output(*head_rows),
            _output(*base_rows),
            argv=["--base-sha", BASE_SHA, *extra_argv],
        )

        assert code == 0

    def test_a_renamed_file_with_rewritten_messages_passes(self, monkeypatch, tmp_path):
        """Match a moved file's statements under the name the branch gives it."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "R091\tapp/old.py\tapp/new.py\n",
            {"app/new.py": CALL_SITE_SHIFTED},
            {"app/old.py": CALL_SITE},
            _output(_row("app/new.py", 7, 12, HEAD_MESSAGE)),
            _output(_row("app/old.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 0

    def test_base_sources_are_read_before_the_worktree_is_removed(
        self, monkeypatch, tmp_path
    ):
        """Parse the base files while the merge-base worktree still exists."""
        code, _, base_root = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE},
            {"app/x.py": CALL_SITE},
            _output(_row("app/x.py", 5, 12, HEAD_MESSAGE)),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert not base_root.exists()
        assert code == 0


class TestChangedStatementIsSurplus:
    """Compare a diagnostic on a statement the branch edited as new."""

    def test_an_edited_statement_reports_even_with_an_identical_message(
        self, monkeypatch, tmp_path, capsys
    ):
        """Report an edited statement's diagnostic even with an unchanged message."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE_EDITED},
            {"app/x.py": CALL_SITE},
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 1
        assert "app/x.py:5:12" in capsys.readouterr().out


class TestAddedDiagnosticsStillReport:
    """Report what the branch adds, rendered as the branch reports it."""

    def test_a_diagnostic_on_a_new_statement_reports(self, monkeypatch, tmp_path):
        """Report a diagnostic on a statement the branch added."""
        head = CALL_SITE + "\n\nextra = create_app(None)\n"
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": head},
            {"app/x.py": CALL_SITE},
            _output(
                _row("app/x.py", 5, 12, HEAD_MESSAGE),
                _row("app/x.py", 8, 20, HEAD_MESSAGE),
            ),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 1

    def test_a_second_copy_on_an_unchanged_statement_reports_once(
        self, monkeypatch, tmp_path, capsys
    ):
        """Report exactly the extra copy, since keys form a multiset."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE},
            {"app/x.py": CALL_SITE},
            _output(
                _row("app/x.py", 5, 12, BASE_MESSAGE),
                _row("app/x.py", 5, 30, BASE_MESSAGE),
            ),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 1
        assert "1 ty diagnostic(s) absent" in capsys.readouterr().out

    def test_a_diagnostic_in_an_added_file_reports(self, monkeypatch, tmp_path):
        """Report every diagnostic in a file absent at the merge-base."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "A\tapp/x.py\n",
            {"app/x.py": CALL_SITE},
            {},
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 1

    def test_the_report_carries_the_head_message_and_line(
        self, monkeypatch, tmp_path, capsys
    ):
        """Render surplus with the branch's message and line, not the base's."""
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        head = CALL_SITE_SHIFTED.replace("settings=None", "settings=None, debug=True")

        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": head},
            {"app/x.py": CALL_SITE},
            _output(_row("app/x.py", 7, 12, HEAD_MESSAGE)),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        out = capsys.readouterr().out
        assert code == 1
        assert f"::warning file=app/x.py,line=7,title={RULE}::{HEAD_MESSAGE}" in out
        assert BASE_MESSAGE not in out
        assert "| `app/x.py` | 7 |" in summary.read_text(encoding="utf-8")


class TestUnparseableSourceDegrades:
    """Compare a file by message when either side cannot be keyed by statement."""

    @pytest.mark.parametrize(
        ("head_message", "expected"),
        [(BASE_MESSAGE, 0), (HEAD_MESSAGE, 1)],
        ids=["same-message-matches", "changed-message-reports"],
    )
    def test_an_unparseable_base_compares_by_message(
        self, monkeypatch, tmp_path, head_message, expected
    ):
        """Fall back to message identity, reporting what it cannot match."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE},
            {"app/x.py": "def build(:\n"},
            _output(_row("app/x.py", 5, 12, head_message)),
            _output(_row("app/x.py", 1, 1, BASE_MESSAGE)),
        )

        assert code == expected

    def test_a_diagnostic_past_the_end_of_the_file_still_reports(
        self, monkeypatch, tmp_path
    ):
        """Report a diagnostic whose line the parsed file does not have."""
        code, _, _ = _run_main_with_sources(
            monkeypatch,
            tmp_path,
            "M\tapp/x.py\n",
            {"app/x.py": CALL_SITE},
            {"app/x.py": CALL_SITE},
            _output(_row("app/x.py", 99, 1, BASE_MESSAGE)),
            _output(_row("app/x.py", 5, 12, BASE_MESSAGE)),
        )

        assert code == 1
