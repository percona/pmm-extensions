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

import subprocess
import sys
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
