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
over :func:`delta_key`: the path, the rule, and the enclosing statement's AST with
locations stripped. The message is left out because an edit elsewhere can
rewrite it (a signature change reprints the types in every call site's
diagnostic) without adding anything.

Attribution is a baseline delta rather than a test of whether a diagnostic sits on
an added line, because an annotation change lands its consequences at call sites
the edit itself need not touch. That, the batching policy ``--per-file`` exists to
escape, and the advisory status of the CI job are recorded in
``docs/development/ty-policy.md``.

The exit status is the whole signal: non-zero when the branch adds a diagnostic.
"""

from __future__ import annotations

import argparse
import ast
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, TYPE_CHECKING

from scripts.classify_ty_diagnostics import Diagnostic, parse_diagnostics

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

ANNOTATION_CAP = 50
ALL_CHECKS_PASSED = "All checks passed!"
TEST_ROOT = "tests/"
PROMOTED_SEVERITY = "warn"
_NESTED_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")

Block = ast.stmt | ast.ExceptHandler | ast.match_case


class TyInvocationError(RuntimeError):
    """Indicate that ty failed for a reason other than reporting diagnostics."""


class AnchorKind(StrEnum):
    """Name what a delta key anchors a diagnostic on."""

    STATEMENT = "statement"
    MESSAGE = "message"


class DeltaKey(NamedTuple):
    """Identify one diagnostic across the two passes.

    :param path: The repo-relative path, as the branch names it.
    :param rule: The ty rule that fired.
    :param kind: What ``anchor`` holds, so the two kinds can never collide.
    :param anchor: The statement's location-free AST dump, or the message.
    """

    path: str
    rule: str
    kind: AnchorKind
    anchor: str


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


@dataclass(frozen=True, slots=True)
class Source:
    """Carry one checked file as one side of the comparison read it.

    :param lines: The file's lines, numbered as the parser numbers them.
    :param tree: The parsed module.
    """

    lines: tuple[str, ...]
    tree: ast.Module

    @classmethod
    def parse(cls, text: str) -> Source:
        """Return ``text`` split into lines and parsed.

        :param text: The file's contents, line endings already normalised.
        :return: The parsed source.
        :raises SyntaxError: When ``text`` does not parse.
        :raises ValueError: When ``text`` holds a null byte, on Python 3.11.
        :raises RecursionError: When ``text`` nests too deeply to parse.
        :raises MemoryError: When ``text`` nests too deeply to parse.
        """
        # str.splitlines() also breaks on \x0c and \x1c-\x1e, which the parser
        # treats as ordinary characters, so its line numbers would drift.
        return cls(lines=tuple(text.split("\n")), tree=ast.parse(text))


@dataclass(frozen=True, slots=True)
class CheckedPass:
    """Carry what one pass reported, with the files it reported on.

    :param diagnostics: The pass's diagnostics, in output order.
    :param sources: Each checked file that parsed, by repo-relative path.
    """

    diagnostics: Sequence[Diagnostic] = ()
    sources: Mapping[str, Source] = field(default_factory=dict)

    def rebased(self, renames: Mapping[str, str]) -> CheckedPass:
        """Return this pass with its paths renamed to the branch's names.

        :func:`delta_key` carries the path, so without this a file the branch
        merely moved has every pre-existing diagnostic counted as surplus.

        :param renames: Old path to new path, for the files the branch moved.
        :return: The same pass, addressed as the branch addresses it.
        """
        return CheckedPass(
            diagnostics=[
                replace(item, path=renames.get(item.path, item.path))
                for item in self.diagnostics
            ],
            sources={
                renames.get(path, path): source for path, source in self.sources.items()
            },
        )


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


def parse_sources(root: Path, paths: Iterable[str]) -> dict[str, Source]:
    """Read and parse each of ``paths`` under ``root``.

    A path that resolves outside ``root``, or a file that cannot be read or
    parsed, is left out rather than raised on, so its diagnostics fall back to
    being compared by message.

    :param root: The tree the paths are relative to.
    :param paths: Repo-relative paths, as one pass checked them.
    :return: Each readable path's parsed source.
    """
    resolved_root = root.resolve()
    sources: dict[str, Source] = {}
    for path in paths:
        file = (root / path).resolve()
        if not file.is_relative_to(resolved_root):
            continue
        try:
            # The parser rejects a byte order mark left in a str, and ty does
            # not count one as a column.
            sources[path] = Source.parse(file.read_text(encoding="utf-8-sig"))
        # ValueError covers undecodable bytes and, on 3.11, a null byte; the
        # parser raises RecursionError or MemoryError on pathological nesting.
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            continue
    return sources


def _children(node: ast.Module | Block) -> Iterator[Block]:
    """Yield the statement-like nodes nested directly inside ``node``.

    :param node: A module, statement, exception handler, or match case.
    :return: Statements, exception handlers, and match cases, in source order.
    """
    for name in _NESTED_BLOCKS:
        for child in getattr(node, name, ()):
            if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                yield child


def _start(node: Block) -> tuple[int, int]:
    """Return the ``(line, byte column)`` a statement-like node starts at.

    Since Python 3.8 a definition's own position is its ``def`` or ``class``
    line, so a diagnostic on a decorator would otherwise fall outside it.

    :param node: A statement, exception handler, or match case.
    :return: Its 1-indexed start line and 0-indexed UTF-8 byte offset.
    """
    # A match case carries no location of its own, and its line holds only
    # the case, so it starts at its pattern's line, before any column.
    if isinstance(node, ast.match_case):
        return (node.pattern.lineno, 0)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
        node.decorator_list
    ):
        return (min(item.lineno for item in node.decorator_list), 0)
    return (node.lineno, node.col_offset)


def _end_line(node: Block) -> int:
    """Return the line a statement-like node ends on.

    :param node: A statement, exception handler, or match case.
    :return: Its 1-indexed last line.
    """
    if isinstance(node, ast.match_case):
        return _end_line(node.body[-1])
    return node.end_lineno or node.lineno


def _enclosing_statement(tree: ast.Module, line: int, byte_column: int) -> Block | None:
    """Return the innermost statement-like node spanning a position.

    Among siblings sharing the line, the last one starting at or before the
    column wins, so a diagnostic past a statement's end — on its trailing
    comment, say — still lands on the statement of its line.

    :param tree: The parsed module.
    :param line: 1-indexed line.
    :param byte_column: 0-indexed UTF-8 byte offset into the line.
    :return: The statement, exception handler, or match case, or ``None`` at
        module level.
    """
    node: ast.Module | Block = tree
    while True:
        spanning = [
            child
            for child in _children(node)
            if _start(child)[0] <= line <= _end_line(child)
        ]
        if not spanning:
            return None if isinstance(node, ast.Module) else node
        started = [child for child in spanning if _start(child) <= (line, byte_column)]
        # Every child starts past the column only on a one-line block such as
        # ``if bad: use()``, where the diagnostic sits on the header.
        if not started and not isinstance(node, ast.Module):
            return node
        node = started[-1] if started else spanning[0]


def _header_only(node: Block) -> Block:
    """Return a copy of ``node`` with its nested blocks emptied.

    Without this, a diagnostic on a ``def`` or ``if`` line would compare as new
    whenever any line of the block below it changed.

    :param node: A statement, exception handler, or match case.
    :return: A shallow copy whose nested blocks are empty.
    """
    header = copy.copy(node)
    for name in _NESTED_BLOCKS:
        if hasattr(header, name):
            setattr(header, name, [])
    return header


def delta_key(diagnostic: Diagnostic, source: Source | None) -> DeltaKey:
    """Return the identity ``diagnostic`` is compared across the two passes on.

    The anchor is the location-free AST of the enclosing statement, so a
    diagnostic whose statement and rule are unchanged matches whatever its
    message now says and however far the statement moved. Without a statement
    to anchor on, the message stands in, which still reports anything new.

    :param diagnostic: One diagnostic from either pass.
    :param source: The file it points into, as the same pass read it.
    :return: The diagnostic's key.
    """
    fallback = DeltaKey(
        diagnostic.path, diagnostic.rule, AnchorKind.MESSAGE, diagnostic.message
    )
    if source is None or not 1 <= diagnostic.line <= len(source.lines):
        return fallback
    text = source.lines[diagnostic.line - 1]
    if not text.strip() or text.lstrip().startswith("#"):
        return fallback
    # ty counts columns in characters; the AST counts UTF-8 bytes. A column
    # inside the indentation still means the line's first statement, not the
    # header of the block around it.
    indent = len(text) - len(text.lstrip())
    byte_column = len(text[: max(diagnostic.column - 1, indent)].encode())
    node = _enclosing_statement(source.tree, diagnostic.line, byte_column)
    if node is None:
        return fallback
    return DeltaKey(
        diagnostic.path,
        diagnostic.rule,
        AnchorKind.STATEMENT,
        ast.dump(_header_only(node)),
    )


def surplus_diagnostics(head: CheckedPass, base: CheckedPass) -> list[Diagnostic]:
    """Return the head diagnostics the merge-base did not already carry.

    The difference is taken over :func:`delta_key` as a multiset, so duplicating
    a defect that already exists still reports. Each surplus key is mapped back
    to a head diagnostic to recover the message and line the key drops.

    :param head: The branch's pass.
    :param base: The merge-base's pass, under head paths.
    :return: The surplus, in head output order.
    """
    # A file keyed by statement on one side and by message on the other would
    # match nothing, so it is compared by statement only when both sides parse.
    shared = head.sources.keys() & base.sources.keys()

    def key_of(item: Diagnostic, sources: Mapping[str, Source]) -> DeltaKey:
        return delta_key(item, sources[item.path] if item.path in shared else None)

    head_keyed = [(item, key_of(item, head.sources)) for item in head.diagnostics]
    remaining = Counter(key for _, key in head_keyed) - Counter(
        key_of(item, base.sources) for item in base.diagnostics
    )
    extra: list[Diagnostic] = []
    for diagnostic, key in head_keyed:
        if remaining[key] > 0:
            remaining[key] -= 1
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
    head = CheckedPass(
        run_pass(executable, rules, changed.head, repo_root, per_file=args.per_file),
        parse_sources(repo_root, changed.head),
    )
    base = CheckedPass()
    if changed.base:
        # The worktree is removed when the block exits, so its sources must be
        # read inside it.
        with base_tree(merge_base) as tree:
            base = CheckedPass(
                run_pass(executable, rules, changed.base, tree, per_file=args.per_file),
                parse_sources(tree, changed.base),
            )
        base = base.rebased(changed.renames)

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
