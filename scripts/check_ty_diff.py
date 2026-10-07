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

"""Fail on the ty diagnostics a branch introduces, relative to its merge-base.

Every rule ``[tool.ty.rules]`` holds at ``warn`` is promoted to ``error``, and the
changed non-test Python files are checked twice: once in the working tree and once
in a detached worktree at the merge-base. The report is the multiset difference
over :attr:`~scripts.classify_ty_diagnostics.Diagnostic.fingerprint`.

Attribution is a baseline delta rather than a test of whether a diagnostic sits on
an added line, because an annotation change lands its consequences at call sites
the edit itself need not touch. That, the batching policy ``--per-file`` exists to
escape, and the advisory status of the CI job are recorded in
``docs/development/ty-policy.md``.

The exit status is the whole signal: non-zero when the branch adds a diagnostic.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.classify_ty_diagnostics import Diagnostic, parse_diagnostics

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

ANNOTATION_CAP = 50
ALL_CHECKS_PASSED = "All checks passed!"
TEST_ROOT = "tests/"
PROMOTED_SEVERITY = "warn"


class TyInvocationError(RuntimeError):
    """Indicate that ty failed for a reason other than reporting diagnostics."""


@dataclass(frozen=True, slots=True)
class ChangedFiles:
    """Carry the paths each pass checks.

    :param head: Repo-relative paths as they exist on the branch.
    :param base: The same files named as the merge-base knows them, which drops
        the paths the branch added, drops the ones it moved out of ``tests/``,
        and renames the rest of the ones it moved.
    :param renames: Old path to new path, for the files the branch moved within
        the checked non-test surface.
    :param tests: Changed Python files under ``tests/``, which neither pass checks.
    """

    head: tuple[str, ...]
    base: tuple[str, ...]
    renames: Mapping[str, str]
    tests: tuple[str, ...] = ()


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` with ``args`` and return its stdout.

    :param args: Arguments after the executable.
    :param cwd: Directory to run in; defaults to the current one.
    :return: The command's stdout.
    :raises subprocess.CalledProcessError: When git exits non-zero.
    :raises OSError: Propagates ``FileNotFoundError`` when git is not installed.
    """
    git_exe = shutil.which("git") or "git"
    result = subprocess.run(
        [git_exe, *args], cwd=cwd, check=True, text=True, capture_output=True
    )
    return result.stdout


def _ty_stdout(argv: Sequence[str], cwd: Path) -> str:
    """Run one ty invocation and return its stdout.

    Exit code 1 means ty reported diagnostics, the expected outcome of a promoted
    run; any other non-zero status is a failure of the run itself.

    :param argv: The full command line, executable included.
    :param cwd: Tree to check in.
    :return: The command's stdout.
    :raises TyInvocationError: When ty exits anything but 0 or 1.
    :raises OSError: Propagates ``FileNotFoundError`` when the pinned binary is
        absent.
    """
    result = subprocess.run(argv, cwd=cwd, check=False, text=True, capture_output=True)
    if result.returncode not in (0, 1):
        raise TyInvocationError(
            f"ty exited {result.returncode} in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def resolve_ty() -> Path:
    """Return the ty binary installed beside the running interpreter.

    Resolving through ``PATH`` would accept whichever ty the runner happens to
    find, rather than the pinned version the baseline is measured against.

    :return: Path to the pinned executable.
    """
    return Path(sys.executable).parent / "ty"


def promoted_rules(pyproject: Path) -> tuple[str, ...]:
    """Return the rules the branch's own severity table holds at ``warn``.

    :param pyproject: The ``pyproject.toml`` carrying ``[tool.ty.rules]``.
    :return: Rule names, sorted.
    :raises OSError: When the file cannot be read.
    :raises tomllib.TOMLDecodeError: When it does not parse.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    rules = data.get("tool", {}).get("ty", {}).get("rules", {})
    return tuple(
        sorted(
            name for name, severity in rules.items() if severity == PROMOTED_SEVERITY
        )
    )


def ty_argv(executable: Path, rules: Sequence[str], paths: Sequence[str]) -> list[str]:
    """Build one ty command line over ``paths``.

    ``--force-exclude`` re-establishes both halves of ``[tool.ty.src]`` for paths
    named on the command line; without it a changed migration is checked against a
    surface that deliberately drops it.

    :param executable: The pinned ty binary.
    :param rules: Rule names to promote to ``error``.
    :param paths: Files to check, relative to the tree being checked.
    :return: The command line, executable first.
    """
    argv = [
        str(executable),
        "check",
        "--force-exclude",
        "--python",
        sys.prefix,
        "--output-format",
        "concise",
    ]
    for rule in rules:
        argv += ["--error", rule]
    argv.extend(paths)
    return argv


def parse_ty_output(text: str) -> list[Diagnostic]:
    """Parse one ty run's stdout, treating its clean-run sentinel as empty.

    ty prints ``All checks passed!`` and no ``Found N diagnostics`` trailer when
    the count is zero. Everything else goes through the reconciling parser, so a
    truncated run — neither sentinel nor trailer — raises instead of reading clean.

    :param text: Raw ``ty check --output-format concise`` stdout.
    :return: Every parsed diagnostic, in output order.
    :raises ReconciliationError: When the run cannot be trusted to be complete.
    """
    if any(line.strip() == ALL_CHECKS_PASSED for line in text.splitlines()):
        return []
    return parse_diagnostics(text)


def parse_name_status(text: str) -> ChangedFiles:
    """Split ``git diff --name-status`` output into the two passes' file lists.

    A path moved out of ``tests/`` is absent from the base list rather than rebased
    onto its new name: ``tests/`` is inside ``[tool.ty.src]``, so its diagnostics
    would otherwise cancel against the surface they have just entered.

    :param text: Raw ``--name-status`` output, rename detection enabled.
    :return: The head and base file lists.
    """
    head: list[str] = []
    base: list[str] = []
    tests: list[str] = []
    renames: dict[str, str] = {}
    for raw in text.splitlines():
        status, _, operands = raw.partition("\t")
        paths = operands.split("\t")
        if not status or not paths[0]:
            continue
        new = paths[-1] if status.startswith(("C", "R")) else paths[0]
        old = "" if status.startswith(("A", "C")) else paths[0]
        if not new.endswith(".py"):
            continue
        if new.startswith(TEST_ROOT):
            tests.append(new)
            continue
        head.append(new)
        if old and not old.startswith(TEST_ROOT):
            base.append(old)
            if old != new:
                renames[old] = new
    return ChangedFiles(
        head=tuple(head), base=tuple(base), renames=renames, tests=tuple(tests)
    )


def changed_files(merge_base: str) -> ChangedFiles:
    """Return the Python files the branch changed since ``merge_base``.

    ``--diff-filter=ACMR`` drops deletions, because handing ty a path that does
    not exist is an ``error[io]`` rather than a diagnostic.

    :param merge_base: The revision to diff from.
    :return: The head and base file lists.
    :raises subprocess.CalledProcessError: Propagated from git, when the
        revision is unknown — a shallow clone is the reachable cause.
    """
    text = _git(
        "diff",
        "--name-status",
        "-M",
        "--diff-filter=ACMR",
        merge_base,
        "HEAD",
        "--",
        "*.py",
    )
    return parse_name_status(text)


def uncommitted_files(repo_root: Path) -> tuple[str, ...]:
    """Return the Python files with uncommitted changes.

    These are the files a commit would add to the change set, so deletions are
    left out, as they are from the committed one.

    :param repo_root: The repository root, which the pathspec is resolved against.
    :return: Repo-relative paths, in ``git status`` order.
    :raises subprocess.CalledProcessError: Propagated from git.
    """
    rows = _git(
        "status", "--porcelain", "--untracked-files=all", "--", "*.py", cwd=repo_root
    ).splitlines()
    paths: list[str] = []
    for row in rows:
        state, path = row[:2], row[3:].rpartition(" -> ")[2].strip('"')
        deleted = state[0] == "D" or state == " D"
        if deleted or not path.endswith(".py"):
            continue
        paths.append(path)
    return tuple(paths)


def unexamined_notice(changed: ChangedFiles, repo_root: Path) -> list[str]:
    """Describe the Python changes this run did not check.

    The run checks the working-tree copies of the non-test files the commits
    changed, so uncommitted edits to any other file, and every change under
    ``tests/``, are outside it whatever the verdict on the files it did check,
    and a zero exit says nothing about them.

    :param changed: The change set the committed diff produced.
    :param repo_root: The repository root.
    :return: One line per kind of unexamined change; empty when there is none.
    :raises subprocess.CalledProcessError: Propagated from git.
    """
    lines: list[str] = []
    uncommitted = [
        path for path in uncommitted_files(repo_root) if path not in changed.head
    ]
    outside = [path for path in uncommitted if not path.startswith(TEST_ROOT)]
    tests = {
        *changed.tests,
        *(path for path in uncommitted if path.startswith(TEST_ROOT)),
    }
    if outside:
        lines.append(
            f"{len(outside)} uncommitted non-test Python file(s) were NOT examined: "
            "this check reads only the files the commits changed. "
            "This is not a pass for them."
        )
    if tests:
        lines.append(
            f"{len(tests)} changed test file(s) were NOT examined: "
            "tests/ is outside this check; `make typecheck` covers it."
        )
    return lines


@contextmanager
def base_tree(merge_base: str) -> Iterator[Path]:
    """Yield a detached worktree checked out at ``merge_base``.

    :param merge_base: The revision to check out.
    :return: The worktree path, removed again when the block exits.
    :raises subprocess.CalledProcessError: Propagated from git, when the
        worktree cannot be created or removed.
    """
    with tempfile.TemporaryDirectory(prefix="ty-diff-base-") as parent:
        tree = Path(parent) / "tree"
        _git("worktree", "add", "--detach", str(tree), merge_base)
        try:
            yield tree
        finally:
            _git("worktree", "remove", "--force", str(tree))


def run_pass(
    executable: Path,
    rules: Sequence[str],
    paths: Sequence[str],
    cwd: Path,
    *,
    per_file: bool,
) -> list[Diagnostic]:
    """Check ``paths`` in ``cwd`` and return every diagnostic reported.

    :param executable: The pinned ty binary.
    :param rules: Rule names to promote to ``error``.
    :param paths: Files to check, relative to ``cwd``.
    :param cwd: Tree to check in.
    :param per_file: Run one invocation per path instead of one for the batch.
    :return: Every parsed diagnostic, in output order.
    :raises TyInvocationError: When ty fails for a reason other than diagnostics.
    :raises ReconciliationError: When a run cannot be trusted to be complete.
    :raises OSError: Propagated from the subprocess when the binary is absent.
    """
    batches = [(path,) for path in paths] if per_file else [tuple(paths)]
    return [
        diagnostic
        for batch in batches
        for diagnostic in parse_ty_output(
            _ty_stdout(ty_argv(executable, rules, batch), cwd)
        )
    ]


def rebase_paths(
    diagnostics: Sequence[Diagnostic], renames: Mapping[str, str]
) -> list[Diagnostic]:
    """Rewrite base-side diagnostic paths to the names the branch gives them.

    ``Diagnostic.fingerprint`` carries the path, so without this a file the branch
    merely moved has every pre-existing diagnostic counted as surplus.

    :param diagnostics: Diagnostics from the base pass.
    :param renames: Old path to new path, for the files the branch moved.
    :return: The same diagnostics, addressed as the branch addresses them.
    """
    return [
        replace(item, path=renames[item.path]) if item.path in renames else item
        for item in diagnostics
    ]


def surplus_diagnostics(
    head: Sequence[Diagnostic], base: Sequence[Diagnostic]
) -> list[Diagnostic]:
    """Return the head diagnostics the merge-base did not already carry.

    The difference is taken over fingerprints as a multiset, so duplicating a
    defect that already exists still reports. Each surplus fingerprint is mapped
    back to a head diagnostic to recover the line the fingerprint drops.

    :param head: Diagnostics reported on the branch.
    :param base: Diagnostics reported at the merge-base.
    :return: The surplus, in head output order.
    """
    remaining = Counter(item.fingerprint for item in head) - Counter(
        item.fingerprint for item in base
    )
    extra: list[Diagnostic] = []
    for diagnostic in head:
        if remaining[diagnostic.fingerprint] > 0:
            remaining[diagnostic.fingerprint] -= 1
            extra.append(diagnostic)
    return extra


def emit_annotations(diagnostics: Sequence[Diagnostic]) -> None:
    """Print one workflow annotation per surplus diagnostic, up to the cap.

    GitHub renders these inline only for lines inside the diff, and the
    characteristic finding here sits on an unchanged line. The step summary always
    carries the full list.

    :param diagnostics: The surplus, in head output order.
    """
    for diagnostic in diagnostics[:ANNOTATION_CAP]:
        print(
            f"::warning file={diagnostic.path},line={diagnostic.line},"
            f"title={diagnostic.rule}::{diagnostic.message}"
        )
    hidden = len(diagnostics) - ANNOTATION_CAP
    if hidden > 0:
        print(f"::warning::+{hidden} more surplus diagnostics; see the job summary.")


def _table_cell(text: str) -> str:
    """Return a value escaped so it cannot split its Markdown table row.

    ty spells union types with ``|`` inside the messages the promoted rules
    report, so an unescaped message would split its row into extra columns.

    :param text: Raw cell content.
    :return: The content with column separators escaped.
    """
    return text.replace("|", "\\|")


def write_summary(diagnostics: Sequence[Diagnostic], path: Path) -> None:
    """Append the full surplus to the workflow's step summary.

    :param diagnostics: The surplus, in head output order.
    :param path: The file ``GITHUB_STEP_SUMMARY`` names.
    :raises OSError: When the summary file cannot be written.
    """
    rows = [
        f"| `{item.path}` | {item.line} | `{item.rule}` | {_table_cell(item.message)} |"
        for item in diagnostics
    ]
    table = [
        "### New ty diagnostics",
        "",
        "| File | Line | Rule | Message |",
        "| --- | --- | --- | --- |",
        *rows,
        "",
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(table))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    :param argv: Arguments to parse, or ``None`` to read ``sys.argv``.
    :return: The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Fail on the ty diagnostics a branch introduces."
    )
    parser.add_argument(
        "--base-sha",
        default=None,
        help="Revision the branch is merging into; defaults to $BASE_SHA.",
    )
    parser.add_argument(
        "--per-file",
        action="store_true",
        help="Run one ty invocation per path instead of one per pass.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Compare the branch against its merge-base and report what it added.

    :param argv: Arguments to parse, or ``None`` to read ``sys.argv``.
    :return: ``1`` when the branch adds a diagnostic, ``2`` on a usage error,
        ``0`` otherwise.
    :raises TyInvocationError: When ty fails for a reason other than diagnostics.
    :raises ReconciliationError: When a run cannot be trusted to be complete.
    :raises subprocess.CalledProcessError: Propagated from git, when a revision
        is unresolvable or the base worktree cannot be created.
    :raises OSError: Propagated from reading ``pyproject.toml``, from the ty and
        git subprocesses, and from writing the step summary.
    :raises tomllib.TOMLDecodeError: When ``pyproject.toml`` does not parse.
    """
    args = _parse_args(argv)
    base_sha = args.base_sha or os.environ.get("BASE_SHA", "")
    if not base_sha:
        print("Pass --base-sha or set BASE_SHA.", file=sys.stderr)
        return 2

    repo_root = Path(_git("rev-parse", "--show-toplevel").strip())
    merge_base = _git("merge-base", base_sha, "HEAD").strip()
    changed = changed_files(merge_base)
    notice = unexamined_notice(changed, repo_root)
    if not changed.head:
        print("\n".join(["No non-test Python files changed.", *notice]))
        return 0

    executable = resolve_ty()
    rules = promoted_rules(repo_root / "pyproject.toml")
    head = run_pass(executable, rules, changed.head, repo_root, per_file=args.per_file)
    base: list[Diagnostic] = []
    if changed.base:
        with base_tree(merge_base) as tree:
            base = run_pass(
                executable, rules, changed.base, tree, per_file=args.per_file
            )
        base = rebase_paths(base, changed.renames)

    extra = surplus_diagnostics(head, base)
    if not extra:
        print(f"No new ty diagnostics across {len(changed.head)} changed file(s).")
        for line in notice:
            print(line)
        return 0

    print(f"{len(extra)} ty diagnostic(s) absent at {merge_base}:")
    for diagnostic in extra:
        print(f"  {diagnostic}")
    for line in notice:
        print(line)
    emit_annotations(extra)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        write_summary(extra, Path(summary))
    return 1


if __name__ == "__main__":
    sys.exit(main())
