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

"""Fail when vulture reports dead code under ``app/`` that the baseline does not keep.

Vulture scans ``app/``, ``tests/``, ``scripts/`` and ``sidecar/``, migrations
included, so a name used anywhere in those trees counts as a reference. Findings
are reported only under ``app/`` outside any ``migrations`` directory, and only
for these kinds: function, method, class, property, and module-level variable.
Class-body variables are left out because nearly all of them are model fields
that frameworks read by name. Vulture matches by name, so a same-named attribute
anywhere counts as a use: the check is a floor, not a full audit. A module
variable assigned only through ``global`` inside a function is not reported.

Dead code is removed, never baselined. A finding stays only when its entry in
``scripts/dead_code_baseline.toml`` names one of two categories:

* ``catalog``: a reusable primitive in a catalog module. The only catalog module
  is ``app/core/utils/fields.py``.
* ``dynamic``: something vulture cannot see reaches it, such as ``getattr``, a
  string import, a by-name registry, deserialization, or a framework or language
  hook that calls it by name. The reason names that mechanism.

Run the check, as CI does::

    pre-commit run check-dead-code --hook-stage manual --all-files

Drop the entries vulture no longer reports::

    python3 scripts/check_dead_code.py --update-baseline

Keep a finding by adding an entry by hand::

    [[entry]]
    path = "app/core/config.py"
    kind = "method"
    name = "get_field_value"
    category = "dynamic"
    reason = "Abstract on PydanticBaseSettingsSource; instantiation needs it."

Vulture runs as if no ``# noqa`` comment existed, so a ``# noqa`` cannot hide a
finding from this check: a finding kept on purpose needs a baseline entry.
"""

import argparse
import ast
import contextlib
import importlib.metadata
import io
import json
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import NamedTuple

import vulture
from vulture.utils import ExitCode

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_RELPATH = Path("scripts") / "dead_code_baseline.toml"
LOCKFILE_NAME = "poetry.lock"
SCAN_DIRS = ("app", "tests", "scripts", "sidecar")
EXCLUDE = ("*/dipper/payloads/*",)
MIN_CONFIDENCE = 60
IGNORE_DECORATORS = (
    "@*.get",
    "@*.post",
    "@*.put",
    "@*.patch",
    "@*.delete",
    "@*.api_route",
    "@*.websocket",
    "@*validator",
    "@*serializer",
    "@computed_field",
    "@*.task",
    "@*.connect",
    "@*.exception_handler",
    "@*.listens_for",
    "@compiles",
    "@abstractmethod",
    "@*.fixture",
)
REPORTED_KINDS = frozenset({"function", "method", "class", "property", "variable"})
CATALOG_MODULES = frozenset({"app/core/utils/fields.py"})
CATEGORIES = frozenset({"catalog", "dynamic"})
ENTRY_KEYS = frozenset({"path", "kind", "name", "category", "reason"})
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
BASELINE_HEADER = (
    "# Dead code that check_dead_code.py keeps on purpose. Every entry is a\n"
    "# catalog primitive or a dynamically reached symbol; dead code is removed.\n"
    "# See scripts/check_dead_code.py for the policy and how to edit this file."
)


class Finding(NamedTuple):
    """Identify one reported symbol, keyed without a line number."""

    path: str
    kind: str
    name: str


class Entry(NamedTuple):
    """Record one baseline entry: a finding kept on purpose, and why."""

    finding: Finding
    category: str
    reason: str


class _NoqaBlindVulture(vulture.Vulture):
    """Run vulture as if the scanned modules carried no ``# noqa`` comments."""

    @property
    def noqa_lines(self) -> defaultdict[str, set[int]]:
        """Report no ``noqa`` lines for any code.

        :return: An empty mapping that answers every code with no lines.
        """
        return defaultdict(set)

    @noqa_lines.setter
    def noqa_lines(self, _parsed: object) -> None:
        """Discard the ``noqa`` lines vulture parses from each module.

        :param _parsed: The parsed lines, which are dropped.
        """


def _definition_time_parts(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
) -> list[ast.expr]:
    """Return the parts of a definition that run in the enclosing scope.

    :param node: A function, class or lambda definition.
    :return: Its decorators, defaults, annotations, bases and keyword values.
    """
    if isinstance(node, ast.ClassDef):
        return [
            *node.decorator_list,
            *node.bases,
            *(keyword.value for keyword in node.keywords),
        ]
    arguments = node.args
    defaults = [
        *arguments.defaults,
        *(default for default in arguments.kw_defaults if default is not None),
    ]
    if isinstance(node, ast.Lambda):
        return defaults
    every_arg = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        arguments.vararg,
        arguments.kwarg,
    ]
    annotations = [
        arg.annotation
        for arg in every_arg
        if arg is not None and arg.annotation is not None
    ]
    returns = [node.returns] if node.returns is not None else []
    return [*node.decorator_list, *defaults, *annotations, *returns]


def _comprehension_walrus_targets(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the walrus targets inside a comprehension.

    A nested definition is entered only through its definition-time parts
    (decorators, defaults, annotations, bases), which the comprehension evaluates.

    :param node: A comprehension, or a node inside one.
    :return: The ``:=`` targets, which bind in the comprehension's enclosing scope.
    """
    children = (
        _definition_time_parts(node)
        if isinstance(node, DEFINITIONS)
        else ast.iter_child_nodes(node)
    )
    for child in children:
        if isinstance(child, ast.NamedExpr):
            yield child.target
        yield from _comprehension_walrus_targets(child)


def _module_scope_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the nodes under ``node`` that the enclosing scope evaluates.

    A definition contributes only what runs when it is defined. A comprehension
    contributes its first iterable and its walrus targets. Any other node
    contributes itself and its children, under the same rules.

    :param node: A node evaluated in the enclosing scope.
    :return: The nodes whose bindings belong to that scope.
    """
    if isinstance(node, COMPREHENSIONS):
        yield from _module_scope_nodes(node.generators[0].iter)
        yield from _comprehension_walrus_targets(node)
    elif isinstance(node, DEFINITIONS):
        for part in _definition_time_parts(node):
            yield from _module_scope_nodes(part)
    else:
        yield node
        for child in ast.iter_child_nodes(node):
            yield from _module_scope_nodes(child)


def module_scope_bindings(path: Path) -> set[tuple[int, str]]:
    """Return the ``(line, name)`` pairs a module binds at module scope.

    :param path: The Python module to read.
    :return: Every name the module binds in its own scope, by Python's scoping rules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        (node.lineno, node.id)
        for node in _module_scope_nodes(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def scan(root: Path) -> list[Finding]:
    """Run vulture over the scan directories and return the reported findings.

    :param root: Resolved repository root the scan directories live under.
    :return: Findings under ``app/`` outside migrations, in vulture's order.
    :raises SystemExit: If vulture could not parse a scanned file.
    """
    scavenger = _NoqaBlindVulture(ignore_decorators=list(IGNORE_DECORATORS))
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        scavenger.scavenge([root / name for name in SCAN_DIRS], exclude=list(EXCLUDE))
    if scavenger.exit_code == ExitCode.InvalidInput:
        raise SystemExit(
            "vulture could not parse these files, so their references are missing:\n"
            f"{errors.getvalue()}"
            "Fix the file, or add a path pattern to EXCLUDE in scripts/check_dead_code.py "
            "if it is deliberately not Python that vulture can read."
        )
    bindings: dict[Path, set[tuple[int, str]]] = {}
    findings: list[Finding] = []
    for item in scavenger.get_unused_code(min_confidence=MIN_CONFIDENCE):
        relpath = Path(item.filename).relative_to(root)
        if item.typ not in REPORTED_KINDS or relpath.parts[0] != "app":
            continue
        if "migrations" in relpath.parts:
            continue
        if item.typ == "variable":
            if relpath not in bindings:
                bindings[relpath] = module_scope_bindings(root / relpath)
            if (item.first_lineno, item.name) not in bindings[relpath]:
                continue
        findings.append(Finding(relpath.as_posix(), item.typ, item.name))
    return findings


def _shape_problem(raw: Mapping[str, object]) -> str | None:
    """Return why a baseline table does not hold exactly the entry keys as non-empty strings.

    :param raw: One table of the baseline's ``entry`` array.
    :return: The broken rule, or ``None``.
    """
    keys = set(raw)
    if keys != ENTRY_KEYS:
        return f"must have exactly the keys {sorted(ENTRY_KEYS)}, has {sorted(keys)}"
    blank = sorted(
        key
        for key, value in raw.items()
        if not isinstance(value, str) or not value.strip()
    )
    if blank:
        return f"has blank or non-string values for {blank}"
    multi_line = sorted(key for key, value in raw.items() if "\n" in str(value))
    if multi_line:
        return f"has multi-line values for {multi_line}; each value is one line"
    return None


def _value_problem(raw: Mapping[str, object]) -> str | None:
    """Return why a well-shaped baseline entry holds a disallowed value, if it does.

    :param raw: One baseline entry that passed :func:`_shape_problem`.
    :return: The broken rule, or ``None``.
    """
    if raw["kind"] not in REPORTED_KINDS:
        return f"has kind {raw['kind']!r}, which is not one of {sorted(REPORTED_KINDS)}"
    if raw["category"] not in CATEGORIES:
        return f"has category {raw['category']!r}, which is not one of {sorted(CATEGORIES)}"
    if raw["category"] == "catalog" and raw["path"] not in CATALOG_MODULES:
        return (
            f"is a catalog entry outside the catalog modules {sorted(CATALOG_MODULES)}"
        )
    return None


def load_baseline(path: Path) -> list[Entry]:
    """Read and validate the baseline file.

    :param path: The baseline TOML file.
    :return: Its entries, in file order.
    :raises SystemExit: If the file is unreadable, not TOML, or breaks a shape rule.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{path}: cannot read the dead-code baseline: {exc}") from exc
    unknown = sorted(set(document) - {"entry"})
    if unknown:
        raise SystemExit(
            f"{path}: unknown top-level keys {unknown}; only [[entry]] tables belong here"
        )
    raw_entries = document.get("entry", [])
    if not isinstance(raw_entries, list):
        raise SystemExit(f"{path}: 'entry' must be an array of [[entry]] tables")
    entries: list[Entry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise SystemExit(f"{path}: entry {index} is not a table")
        table = {str(key): value for key, value in raw.items()}
        problem = _shape_problem(table) or _value_problem(table)
        if problem is not None:
            raise SystemExit(f"{path}: entry {index} {problem}")
        values = {key: str(value) for key, value in table.items()}
        finding = Finding(values["path"], values["kind"], values["name"])
        entries.append(Entry(finding, values["category"], values["reason"]))
    return entries


def compare(
    findings: Iterable[Finding], entries: Iterable[Entry]
) -> tuple[list[Finding], list[Finding]]:
    """Compare findings with baseline entries as multisets.

    :param findings: What vulture reports.
    :param entries: What the baseline keeps.
    :return: The findings the baseline does not cover, and the entries nothing matches.
    """
    reported = Counter(findings)
    kept = Counter(entry.finding for entry in entries)
    return sorted((reported - kept).elements()), sorted((kept - reported).elements())


def prune(entries: Iterable[Entry], findings: Iterable[Finding]) -> list[Entry]:
    """Keep the entries a finding still matches, one finding per entry.

    :param entries: The current baseline entries.
    :param findings: What vulture reports.
    :return: The matched entries, in their original order.
    """
    available = Counter(findings)
    kept: list[Entry] = []
    for entry in entries:
        if available[entry.finding] > 0:
            available[entry.finding] -= 1
            kept.append(entry)
    return kept


def render_baseline(entries: Iterable[Entry]) -> str:
    """Render entries as the baseline file, sorted by finding.

    :param entries: The entries to write.
    :return: The TOML text, header included.
    """
    blocks = [BASELINE_HEADER]
    for entry in sorted(entries, key=lambda entry: entry.finding):
        fields = {
            **entry.finding._asdict(),
            "category": entry.category,
            "reason": entry.reason,
        }
        lines = [
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in fields.items()
        ]
        blocks.append("\n".join(["[[entry]]", *lines]))
    return "\n\n".join(blocks) + "\n"


def locked_vulture_version(root: Path) -> str:
    """Return the vulture version ``poetry.lock`` locks.

    :param root: The repository root.
    :return: The locked version string.
    :raises SystemExit: If the lockfile is unreadable or locks no vulture.
    """
    lockfile = root / LOCKFILE_NAME
    try:
        document = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{lockfile}: cannot read the lockfile: {exc}") from exc
    packages = document.get("package", [])
    if isinstance(packages, list):
        for package in packages:
            if isinstance(package, dict) and package.get("name") == "vulture":
                version = package.get("version")
                if isinstance(version, str):
                    return version
    raise SystemExit(
        f"{lockfile}: {LOCKFILE_NAME} locks no vulture: add it with poetry add --group dev vulture==<version>"
    )


def _new_finding_message(finding: Finding) -> str:
    """Describe a finding the baseline does not keep, with a ready-to-edit entry.

    :param finding: The new finding.
    :return: The message text.
    """
    block = (
        render_baseline([Entry(finding, "dynamic", "")])
        .removeprefix(BASELINE_HEADER)
        .strip()
    )
    return (
        f"{finding.path}: unused {finding.kind} '{finding.name}' is new. Remove it. If "
        "something vulture cannot see reaches it, add a dynamic entry naming that mechanism:\n"
        f"{block}"
    )


def _stale_entry_message(finding: Finding) -> str:
    """Describe a baseline entry vulture no longer reports.

    :param finding: The stale entry's finding.
    :return: The message text.
    """
    return (
        f"{finding.path}: {finding.kind} '{finding.name}' is in the baseline but vulture no "
        "longer reports it. Delete the entry, or run python3 scripts/check_dead_code.py "
        "--update-baseline."
    )


def _update_baseline(
    baseline: Path, entries: list[Entry], findings: list[Finding]
) -> int:
    """Rewrite the baseline without its stale entries, adding nothing.

    :param baseline: The baseline file.
    :param entries: Its current entries.
    :param findings: What vulture reports.
    :return: Exit status 0.
    """
    kept = prune(entries, findings)
    baseline.write_text(render_baseline(kept), encoding="utf-8")
    print(f"{baseline}: kept {len(kept)} entries, removed {len(entries) - len(kept)}.")
    new, _ = compare(findings, kept)
    if new:
        print("These findings were not added; dead code is removed, never baselined:")
        for finding in new:
            print(f"  {finding.path}: {finding.kind} '{finding.name}'")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the dead-code check, or prune the baseline.

    :param argv: Command-line arguments; ``None`` reads ``sys.argv``.
    :return: Exit status. A check returns 0 when the tree is clean and 1 otherwise;
        ``--update-baseline`` returns 0 once the baseline is rewritten, whatever it
        reports.
    :raises SystemExit: If the baseline or lockfile is unreadable or malformed, the
        lockfile locks no vulture, the running vulture differs from the locked one, or
        vulture could not parse a scanned file.
    """
    parser = argparse.ArgumentParser(
        description="Fail when vulture reports dead code under app/ that the baseline does not keep."
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root to check"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="drop baseline entries vulture no longer reports; never adds any",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    baseline = root / BASELINE_RELPATH
    entries = load_baseline(baseline)
    locked = locked_vulture_version(root)
    running = importlib.metadata.version("vulture")
    if running != locked:
        raise SystemExit(
            f"vulture {running} runs this check but {LOCKFILE_NAME} locks {locked}. Move the "
            "additional_dependencies pin in .pre-commit-config.yaml and the dev-group pin together."
        )
    findings = scan(root)
    if args.update_baseline:
        return _update_baseline(baseline, entries, findings)

    new, stale = compare(findings, entries)
    failures = [_new_finding_message(finding) for finding in new]
    failures.extend(_stale_entry_message(finding) for finding in stale)
    if failures:
        print("\n\n".join(failures))
        return 1
    print(f"No new dead code under app/; the baseline keeps {len(entries)} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
