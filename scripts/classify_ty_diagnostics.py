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

"""Split ty's diagnostics into dependency-typing artifacts and first-party defects.

``make typecheck`` reports thousands of diagnostics and nothing in the output says
which of them describe a defect in this repository. Each group in :data:`GROUPS`
is a shape ty reports because of how a *dependency* is typed — a pydantic-settings
private constructor kwarg, a ``Field(...)`` default that stays a ``FieldInfo``
until the model is built, a Celery attribute installed at runtime. Those are the
ones neutralized by ``[[tool.ty.overrides]]`` entries and per-site
``# ty: ignore[rule]`` comments; everything else is first-party and stays
reportable. ``docs/development/ty-policy.md`` carries the narrative.

Each group records the **discriminant** it classifies on, because several groups
share a rule with first-party diagnostics: under ``unknown-argument`` the
pydantic-settings ``_secrets_dir`` kwarg and a first-party ``PMM`` kwarg differ
only in the symbol the message names.

Three modes::

    python3 scripts/classify_ty_diagnostics.py report
    python3 scripts/classify_ty_diagnostics.py baseline --out /tmp/ty-baseline.json
    python3 scripts/classify_ty_diagnostics.py check --baseline /tmp/ty-baseline.json

``check`` is the gate. It takes the multiset difference between the baseline
manifest and the current run over ``(path, rule, message)`` fingerprints and
asserts that every diagnostic which stopped reporting is one the classification
marks as an artifact. Counts alone cannot prove that: once a suppression hides a
first-party warning its row is simply absent, so a change that removes one
artifact *and* one first-party diagnostic while an unrelated new one appears
reconciles to the same total.

The manifest is a build artifact, not a committed file. A reviewer reproduces it
by running ``baseline`` on the base branch and ``check --baseline`` on this one.
"""

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>[^\s].*?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<severity>error|warning)\[(?P<rule>[a-z0-9-]+)\] (?P<message>.*)$"
)
TRAILER_RE = re.compile(r"^Found (?P<total>\d+) diagnostics?$")
SUMMARY = (
    "Split ty's diagnostics into dependency-typing artifacts and first-party defects."
)

Fingerprint = tuple[str, str, str]

#: The classifier's own test module, read as a corpus of first-party
#: fingerprints by :func:`corpus_fingerprints`. A test that leaves a diagnostic
#: first-party is a standing claim that some group's pattern discriminates
#: within that rule.
CORPUS_TEST = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "scripts"
    / "test_classify_ty_diagnostics.py"
)

#: Elements in a ``(path, rule, message)`` tuple. Arity discriminates almost
#: nothing on its own — ``("a", "b", "c")`` is three strings too — so a corpus
#: read also holds the first two elements against the patterns below.
FINGERPRINT_ARITY = 3

#: A repo-relative Python path, the first element of a fingerprint tuple.
PATH_RE = re.compile(r"[\w./-]+\.py")

#: A ty rule name, the second element of a fingerprint tuple.
RULE_RE = re.compile(r"[a-z0-9-]+")

#: A backtick-quoted span in a group's message pattern.
SYMBOL_SPAN_RE = re.compile(r"`([^`]*)`")

#: The literal identifier run a quoted span must contain to name a symbol. A
#: span holding only wildcards (``.+``, ``\w+``, ``[\w.]+``) quotes nothing a
#: reader could grep and narrows its rule no better than the bare rule does.
SYMBOL_LITERAL_RE = re.compile(r"[A-Za-z_]{2,}")


class ReconciliationError(Exception):
    """Indicate that a ty run cannot be trusted to be complete."""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Represent one row of ``ty check --output-format concise`` output.

    :param path: The repo-relative file the diagnostic points at.
    :param line: The 1-indexed line ty reported, which is where a
        ``# ty: ignore[rule]`` comment has to sit to suppress it.
    :param column: The 1-indexed column ty reported.
    :param severity: ``error`` or ``warning``, as resolved by ``[tool.ty.rules]``.
    :param rule: The lint rule name inside the brackets.
    :param message: The rendered message after the rule name.
    """

    path: str
    line: int
    column: int
    severity: str
    rule: str
    message: str

    @property
    def fingerprint(self) -> Fingerprint:
        """Return the identity a baseline manifest is reconciled on.

        This tuple is exactly :func:`classify`'s input, so two diagnostics sharing
        it always share a verdict and folding away the line and column costs the
        reconciliation no precision — while making it survive the reformatting
        that appending a suppression comment provokes. Keying on position instead
        would report every diagnostic below an edited line as newly suppressed.
        """
        return (self.path, self.rule, self.message)

    def __str__(self) -> str:
        """Return the diagnostic rendered back in ty's concise format."""
        return (
            f"{self.path}:{self.line}:{self.column}: "
            f"{self.severity}[{self.rule}] {self.message}"
        )


@dataclass(frozen=True, slots=True)
class Group:
    """Represent one dependency-typing shape and the discriminant that isolates it.

    :param name: The group's identifier, as used in ``ty-policy.md``.
    :param rules: The ty rules the group's diagnostics report under.
    :param pattern: The message pattern that separates the group from first-party
        diagnostics sharing those rules.
    :param paths: Path prefixes the group is confined to, empty for a group whose
        message alone establishes the verdict. Needed where it does not:
        ``Cannot resolve imported module`` reads identically for a golden-app
        module scaffolded at test time and for a first-party import someone
        mistyped, so without a prefix that typo would classify as an artifact and
        ``check`` would demand its suppression.
    :param discriminant: A prose statement of what ``pattern`` keys on, for the
        report and the policy document.
    """

    name: str
    rules: frozenset[str]
    pattern: re.Pattern[str]
    paths: tuple[str, ...]
    discriminant: str

    def claims(self, fingerprint: Fingerprint) -> bool:
        """Return whether this group claims ``fingerprint``.

        :param fingerprint: The ``(path, rule, message)`` identity to test.
        :return: ``True`` when rule, message and path all match.
        """
        path, rule, message = fingerprint
        return (
            rule in self.rules
            and bool(self.pattern.search(message))
            and (not self.paths or path.startswith(self.paths))
        )


@dataclass(frozen=True, slots=True)
class Retained:
    """Represent an artifact deliberately left reporting, with its reason.

    An artifact sharing a line with a first-party diagnostic of the same rule
    cannot be suppressed by a comment without suppressing both, and splitting the
    expression is the first resort. An entry here records the case where the split
    is not clean, so ``check`` can stay green without the artifact disappearing
    from the report.

    :param fingerprint: The ``(path, rule, message)`` identity that stays
        unsuppressed.
    :param reason: Why the artifact could not be neutralized.
    """

    fingerprint: Fingerprint
    reason: str


def _group(
    name: str, rules: str, pattern: str, discriminant: str, paths: str = ""
) -> Group:
    """Build a :class:`Group` from its space-separated rule list and raw pattern.

    :param name: The group's identifier.
    :param rules: Space-separated ty rule names.
    :param pattern: The message regex, matched with :meth:`re.Pattern.search`.
    :param discriminant: Prose statement of what the pattern keys on.
    :param paths: Space-separated path prefixes to confine the group to.
    :return: The assembled group.
    """
    return Group(
        name=name,
        rules=frozenset(rules.split()),
        pattern=re.compile(pattern),
        paths=tuple(paths.split()),
        discriminant=discriminant,
    )


GROUPS: tuple[Group, ...] = (
    _group(
        "settings-subclass-attributes",
        "unresolved-attribute",
        r"^Object of type `\w*Settings` has no attribute "
        r"`(_set_snapshot|get_snapshot|_resolve)`$",
        "the attribute is one of the three helpers the settings-override proxy "
        "installs on a `*Settings` subclass; naming them is what keeps an "
        "ordinary misspelled attribute on the same receiver first-party",
    ),
    _group(
        "pydantic-fieldinfo",
        "invalid-argument-type invalid-assignment",
        r"^Object of type `FieldInfo` is not assignable to ",
        "the assigned type is `FieldInfo`, the pre-build type of a `Field(...)` default",
    ),
    _group(
        "env-populated-required-params",
        "missing-argument",
        r"^No argument provided for required parameter `(CELERY|_session)`",
        "the required parameter is `CELERY` or `_session`, filled from the "
        "environment or by dependency injection",
    ),
    _group(
        "pydantic-settings-private-kwargs",
        "unknown-argument",
        r"^Argument `_(env_file|env_file_encoding|secrets_dir|case_sensitive"
        r"|env_prefix|env_nested_delimiter|cli_parse_args)` "
        r"does not match any known parameter",
        "the argument is a pydantic-settings private kwarg, absent from the "
        "generated `__init__`",
    ),
    _group(
        "celery-app-attributes",
        "unresolved-attribute",
        r"^Object of type `Celery` has no attribute `loop`$",
        "the attribute is `loop`, which this project installs on the `Celery` "
        "instance at runtime; naming it is what keeps an ordinary misspelled "
        "attribute on the same receiver first-party",
    ),
    _group(
        "third-party-overload-sets",
        "no-matching-overload",
        r"^No overload of (bound method `AsyncSession\.exec`"
        r"|function `(select|create_model)`) matches arguments",
        "the callee is a third-party overload set ty cannot resolve",
    ),
    _group(
        "sa-type-typedecorator",
        "invalid-argument-type",
        r"Expected `type\[Any\] \| PydanticUndefinedType`, found ",
        "`sa_type=` is given a `TypeDecorator` instance where the stub wants a type",
    ),
    _group(
        "absent-modules",
        "unresolved-import",
        r"^Cannot resolve imported module `",
        "the module does not exist at check time: golden apps are scaffolded by "
        "the test run, and the payloads run on the host they are dispatched to",
        paths="tests/app/extensions/apps/framework/golden/ "
        "app/extensions/apps/om_inventory/payload/probe.py "
        "app/extensions/sync/syncers/system_facts/payload.py",
    ),
    _group(
        "subscripted-generics-called",
        "call-non-callable",
        r"^Object of type `GenericAlias` is not callable",
        "the receiver is a subscripted generic, which is callable at runtime",
    ),
    _group(
        "fastapi-query-default",
        "invalid-parameter-default",
        r"^Default value of type `Query` is not assignable to ",
        "the default is a FastAPI `Query(...)` marker, replaced during request "
        "handling",
    ),
    _group(
        "pygments-textlexer",
        "unresolved-import",
        r"^Module `pygments\.lexers` has no member `TextLexer`",
        "`pygments.lexers` re-exports lazily, so its stub lists no member",
    ),
    _group(
        "predicate-dsl-comparison-operators",
        "invalid-method-override",
        r"^Invalid override of method `__(eq|ne)__`: Definition is incompatible "
        r"with `object\.__(eq|ne)__`$",
        "the override is `FieldExpr.__eq__`/`__ne__`, which return a `Predicate` "
        'node so `F("field") == value` builds a rule the way SQLAlchemy builds '
        "one for a column; `object.__eq__` is declared `-> bool` in typeshed and "
        "cannot move, and the two are confined to the rules DSL module",
        "app/extensions/apps/framework/rules.py",
    ),
    _group(
        "runtime-computed-model-in-type-position",
        "invalid-type-form",
        r"^Variable of type `type\[[\w.]+\]` is not allowed in a "
        r"(return type annotation|parameter annotation|type expression)$",
        "the annotation names a class chosen at runtime -- a provider-selected "
        "user model, a `create_model` response model, a form model read back by "
        "`get_type_hints` -- so no static form can name it; Python has no "
        "spelling for `the class in this variable`, and the framework reads "
        "these annotations back at request time",
    ),
    _group(
        "factory-built-annotated-alias",
        "invalid-type-form",
        r"^Function calls are not allowed in type expressions$",
        "the annotation is a call to a field-type factory returning an "
        "`Annotated` alias, which pydantic resolves at model-build time; unlike "
        "the group above, the message names no discriminant of its own -- it "
        "reads identically for an ordinary call mistakenly written in a type "
        "position -- so the group is confined to the module holding the only "
        "such site",
        "app/extensions/apps/mysql_backups/forms.py",
    ),
)

RETAINED: tuple[Retained, ...] = ()


def parse_diagnostics(text: str) -> list[Diagnostic]:
    """Parse ty's concise output, reconciling the row count against ty's own total.

    Note-block continuations are indented and so never match ``DIAGNOSTIC_RE``;
    duplicate ``file:line:column`` rows are kept, because ty genuinely emits them
    and de-duplicating would drop real hits.

    :param text: Raw ``ty check --output-format concise`` output.
    :return: Every parsed diagnostic, in output order.
    :raises ReconciliationError: When the trailer is missing (a truncated or
        crashed run) or disagrees with the number of rows parsed.
    """
    diagnostics: list[Diagnostic] = []
    reported: int | None = None
    for raw in text.splitlines():
        trailer = TRAILER_RE.match(raw)
        if trailer is not None:
            reported = int(trailer["total"])
            continue
        match = DIAGNOSTIC_RE.match(raw)
        if match is None:
            continue
        diagnostics.append(
            Diagnostic(
                path=match["path"],
                line=int(match["line"]),
                column=int(match["column"]),
                severity=match["severity"],
                rule=match["rule"],
                message=match["message"],
            )
        )
    if reported is None:
        raise ReconciliationError(
            "ty printed no `Found N diagnostics` trailer; the run did not complete"
        )
    if reported != len(diagnostics):
        raise ReconciliationError(
            f"parsed {len(diagnostics)} rows but ty reported {reported} diagnostics"
        )
    return diagnostics


def classify(fingerprint: Fingerprint) -> Group | None:
    """Return the artifact group a diagnostic falls in, or ``None`` if first-party.

    :param fingerprint: The diagnostic's ``(path, rule, message)`` identity.
    :return: The matching group, or ``None`` when no group claims the diagnostic.
    """
    for group in GROUPS:
        if group.claims(fingerprint):
            return group
    return None


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Return the module-level ``NAME = "..."`` bindings of a parsed test module.

    The corpus writes a fingerprint's message either inline or through a constant
    (``CALL_IN_TYPE_EXPRESSION``), so a literal-only read of the tuples below
    would silently miss whichever spelling the author preferred. Annotated
    bindings count for the same reason: adding a ``: str`` to a constant the
    corpus already has must not drop the fingerprint it feeds.

    :param tree: The parsed corpus module.
    :return: Every module-level name bound directly to a string.
    """
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value.value
    return bindings


def _tuple_fingerprint(node: ast.Tuple, bindings: dict[str, str]) -> Fingerprint | None:
    """Return the fingerprint a three-string tuple literal spells, if it is one.

    A false positive here is not inert: a spurious fingerprint enters the pinned
    corpus, and one whose rule matches an unconfined group's would *clear* that
    group in :func:`group_constraint_failures`. So the shape of all three
    elements is checked, not just their count.

    :param node: The tuple expression under test.
    :param bindings: Module-level string constants from :func:`_module_strings`.
    :return: The ``(path, rule, message)`` identity, or ``None``.
    """
    if len(node.elts) != FINGERPRINT_ARITY:
        return None
    parts: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            parts.append(element.value)
        elif isinstance(element, ast.Name) and element.id in bindings:
            parts.append(bindings[element.id])
        else:
            return None
    path, rule, message = parts
    if not PATH_RE.fullmatch(path) or not RULE_RE.fullmatch(rule) or not message:
        return None
    return (path, rule, message)


def corpus_fingerprints(source: Path | None = None) -> frozenset[Fingerprint]:
    """Return every ``(path, rule, message)`` identity the classifier's tests name.

    The suite is read as a corpus rather than executed: a test that leaves a
    diagnostic first-party is a standing claim that some group's pattern
    discriminates within that rule, which is what
    :func:`group_constraint_failures` accepts in place of a path constraint. Both
    spellings the suite uses are resolved — a whole ty row, and a bare
    three-string tuple whose message may arrive through a module constant.

    An unreadable or absent corpus yields the empty set, which can only make the
    audit stricter: every group then has to carry its own discriminant. Three
    things count as unreadable — the file is missing, it does not decode as
    UTF-8, or it does not parse.

    :param source: The corpus module, defaulting to the classifier's own tests.
    :return: The fingerprints the suite names, in no particular order.
    """
    path = CORPUS_TEST if source is None else source
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return frozenset()

    bindings = _module_strings(tree)
    found: set[Fingerprint] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple):
            fingerprint = _tuple_fingerprint(node, bindings)
            if fingerprint is not None:
                found.add(fingerprint)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            row = DIAGNOSTIC_RE.match(node.value)
            if row is not None:
                found.add((row["path"], row["rule"], row["message"]))
    return frozenset(found)


def _names_symbol(pattern: str) -> bool:
    """Return whether ``pattern`` quotes an identifier rather than a wildcard.

    Naming a symbol is what lets a group tell its artifact from a first-party
    diagnostic of the same rule without a path constraint — under
    ``unknown-argument``, the pydantic-settings ``_secrets_dir`` kwarg and a
    first-party ``PMM`` kwarg differ only in the symbol the message names. The
    backticks alone do not establish that: ``^Object of type `.+` has no
    attribute `.+`$`` quotes two wildcards and so claims every diagnostic of
    its rule, which is the shape :func:`group_constraint_failures` exists to
    reject.

    Spans are matched pairwise rather than by searching the whole pattern,
    because the prose *between* two quoted spans is itself letters and would
    satisfy the run on its own.

    :param pattern: The group's message pattern, as written.
    :return: ``True`` when some quoted span holds a literal identifier run.
    """
    return any(
        SYMBOL_LITERAL_RE.search(span) for span in SYMBOL_SPAN_RE.findall(pattern)
    )


def group_constraint_failures(
    groups: Iterable[Group] | None = None,
    corpus: Iterable[Fingerprint] | None = None,
) -> list[str]:
    """Return one message per group claiming more than its discriminant proves.

    A group suppresses first-party diagnostics if its predicate is wider than the
    evidence separating an artifact from a defect sharing the rule. Three things
    count as that evidence, and a group needs one:

    * a **path** constraint, which settles the question the message cannot;
    * a **symbol** the message names, per :func:`_names_symbol` — a
      backtick-quoted identifier in the pattern, which is how most of the
      shipped groups tell an artifact from a first-party hit under the same
      rule;
    * a **negative corpus fingerprint** for *every* rule the group claims — a
      diagnostic of that rule which the whole table leaves first-party,
      demonstrating the pattern discriminating rather than asserting it. Two
      conditions make it evidence, and dropping either one lets a group through
      unconfined. The test must leave it claimed by *nothing*: a fingerprint
      some sibling group claims is that sibling's evidence, and reading it as
      this group's would let two groups pin each other while neither is
      confined. And it must cover the group's whole rule set: a group claiming
      two rules with a declined fingerprint under only one of them is still
      unconfined under the other.

    Without one, the group claims every diagnostic of its rules whose message
    happens to match, anywhere in the tree. That is the shape this exists to
    catch: ``factory-built-annotated-alias`` matches ``Function calls are not
    allowed in type expressions``, which reads identically for the field-type
    factory it means and for an ordinary call written into a type position by
    mistake, so before the path constraint it would have classified a genuine
    defect as suppressible.

    :param groups: The table to audit, defaulting to :data:`GROUPS`.
    :param corpus: Fingerprints tests pin as first-party, defaulting to
        :func:`corpus_fingerprints`. Pass an empty iterable to audit the table
        on its own constraints alone.
    :return: A message per unconfined group, in table order.
    """
    table = GROUPS if groups is None else tuple(groups)
    pinned = corpus_fingerprints() if corpus is None else tuple(corpus)

    declined_rules = {
        rule
        for path, rule, message in pinned
        if not any(group.claims((path, rule, message)) for group in table)
    }

    failures: list[str] = []
    for group in table:
        if group.paths or _names_symbol(group.pattern.pattern):
            continue
        if group.rules <= declined_rules:
            continue
        failures.append(
            f"{group.name} claims every {'/'.join(sorted(group.rules))} diagnostic "
            f"matching {group.pattern.pattern!r}, but names no symbol, is confined "
            "to no path, and no test pins a declined diagnostic under every rule "
            "it claims: its discriminant proves less than it claims"
        )
    return failures


def _is_artifact(diagnostic: Diagnostic) -> bool:
    """Return whether ``diagnostic`` matches one of the artifact groups.

    :param diagnostic: The diagnostic to classify.
    :return: ``True`` when a group claims it.
    """
    return classify(diagnostic.fingerprint) is not None


def _render(fingerprint: Fingerprint) -> str:
    """Return a fingerprint rendered in ty's concise format, minus the severity.

    :param fingerprint: The ``(path, rule, message)`` tuple.
    :return: The one-line rendering.
    """
    path, rule, message = fingerprint
    return f"{path}: [{rule}] {message}"


def load_output(source: Path | None) -> str:
    """Return ty's concise output, read from ``source`` or produced by running ty.

    :param source: A file holding captured output, or ``None`` to run ty now.
    :return: The raw output text.
    :raises ReconciliationError: When ty produced no output but reported a cause
        on stderr, which is the shape of a configuration or startup failure.
    """
    if source is not None:
        return source.read_text(encoding="utf-8")
    executable = shutil.which("ty") or "ty"
    completed = subprocess.run(
        [executable, "check", "--output-format", "concise"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not completed.stdout.strip() and completed.stderr.strip():
        raise ReconciliationError(
            f"ty wrote no diagnostics: {completed.stderr.strip()}"
        )
    return completed.stdout


def _pairs(diagnostics: Sequence[Diagnostic]) -> list[tuple[str, str, int, int]]:
    """Return ``(path, rule, artifact_hits, total_hits)`` for each pair with artifacts.

    The pair is the unit the neutralization mechanism is chosen on: a pair whose
    every hit is an artifact takes a ``[[tool.ty.overrides]]`` entry, a pair that
    mixes takes per-site comments.

    :param diagnostics: The run to summarize.
    :return: One row per ``(path, rule)`` pair holding at least one artifact,
        sorted by path then rule.
    """
    total: Counter[tuple[str, str]] = Counter()
    artifact: Counter[tuple[str, str]] = Counter()
    for diagnostic in diagnostics:
        key = (diagnostic.path, diagnostic.rule)
        total[key] += 1
        if _is_artifact(diagnostic):
            artifact[key] += 1
    return [
        (path, rule, artifact[path, rule], total[path, rule])
        for path, rule in sorted(artifact)
    ]


def _collisions(diagnostics: Sequence[Diagnostic]) -> list[tuple[str, int, str]]:
    """Return every ``(path, line, rule)`` holding both an artifact and a first-party hit.

    A comment on such a line suppresses both, so the site has to be split or
    listed in :data:`RETAINED`.

    :param diagnostics: The run to scan.
    :return: The colliding sites, sorted.
    """
    verdicts: dict[tuple[str, int, str], set[bool]] = {}
    for diagnostic in diagnostics:
        key = (diagnostic.path, diagnostic.line, diagnostic.rule)
        verdicts.setdefault(key, set()).add(_is_artifact(diagnostic))
    return sorted(key for key, seen in verdicts.items() if seen == {True, False})


def group_hits(diagnostics: Sequence[Diagnostic]) -> Counter[str]:
    """Return each group's hit count over ``diagnostics``, zeros included.

    :param diagnostics: The run to tally.
    :return: A counter carrying a key for every group in :data:`GROUPS`.
    """
    hits: Counter[str] = Counter({group.name: 0 for group in GROUPS})
    for diagnostic in diagnostics:
        group = classify(diagnostic.fingerprint)
        if group is not None:
            hits[group.name] += 1
    return hits


def _print_groups(diagnostics: Sequence[Diagnostic]) -> None:
    """Print the per-group hit counts, zero rows included.

    A zero is not a staleness signal here: on a neutralized tree every group
    reaches zero by design. ``check`` names the stale groups instead, against the
    baseline, which is the only run where a group matching nothing means its
    predicate has gone stale.

    :param diagnostics: The run to summarize.
    """
    hits = group_hits(diagnostics)
    print("\nArtifact groups")
    for group in GROUPS:
        print(f"  {hits[group.name]:5d}  {group.name}")
        print(f"         {' | '.join(sorted(group.rules))} — {group.discriminant}")
        if group.paths:
            print(f"         confined to: {', '.join(group.paths)}")
    print(f"  total artifacts: {sum(hits.values())}")


def _print_rule_split(diagnostics: Sequence[Diagnostic]) -> None:
    """Print each rule's total, artifact and residual counts.

    :param diagnostics: The run to summarize.
    """
    total: Counter[str] = Counter()
    artifact: Counter[str] = Counter()
    for diagnostic in diagnostics:
        total[diagnostic.rule] += 1
        if _is_artifact(diagnostic):
            artifact[diagnostic.rule] += 1
    print("\nPer-rule split (residual is SEP-1908's baseline)")
    print(f"  {'rule':32s} {'total':>6s} {'artifact':>9s} {'residual':>9s}")
    for rule in sorted(total, key=lambda name: (-total[name], name)):
        residual = total[rule] - artifact[rule]
        print(f"  {rule:32s} {total[rule]:6d} {artifact[rule]:9d} {residual:9d}")


def _print_mechanisms(diagnostics: Sequence[Diagnostic]) -> None:
    """Print which pairs take an override and which take per-site comments.

    :param diagnostics: The run to summarize.
    """
    pairs = _pairs(diagnostics)
    pure = [row for row in pairs if row[2] == row[3]]
    mixed = [row for row in pairs if row[2] < row[3]]

    print(
        f"\nOverride pairs (every hit is an artifact): "
        f"{len(pure)} pairs, {sum(row[2] for row in pure)} hits"
    )
    by_rule: dict[str, list[tuple[str, int]]] = {}
    for path, rule, artifact, _ in pure:
        by_rule.setdefault(rule, []).append((path, artifact))
    for rule in sorted(by_rule):
        files = by_rule[rule]
        print(f"  {rule} — {len(files)} files, {sum(n for _, n in files)} hits")
        for path, artifact in files:
            print(f"    {artifact:4d}  {path}")

    lines: dict[tuple[str, str], list[int]] = {}
    for diagnostic in diagnostics:
        key = (diagnostic.path, diagnostic.rule)
        if _is_artifact(diagnostic):
            lines.setdefault(key, []).append(diagnostic.line)
    print(
        f"\nComment pairs (artifact and first-party hits share the rule): "
        f"{len(mixed)} pairs, {sum(row[2] for row in mixed)} artifact hits"
    )
    for path, rule, artifact, total in mixed:
        sites = sorted(set(lines[(path, rule)]))
        print(f"  {artifact:4d}/{total:<4d} {rule:28s} {path}")
        print(f"        lines: {', '.join(str(line) for line in sites)}")


def _print_collisions(diagnostics: Sequence[Diagnostic]) -> None:
    """Print the sites a per-site comment cannot discriminate.

    :param diagnostics: The run to summarize.
    """
    collisions = _collisions(diagnostics)
    print(f"\nUnsuppressable-by-comment sites: {len(collisions)}")
    for path, line, rule in collisions:
        print(f"  {path}:{line} [{rule}] — split the expression or add to RETAINED")


def _print_retained() -> None:
    """Print the retained artifacts, so the list cannot grow unnoticed."""
    print(f"\nRETAINED artifacts: {len(RETAINED)}")
    for entry in RETAINED:
        print(f"  {_render(entry.fingerprint)}\n        {entry.reason}")


def _print_overclaims(overclaims: Sequence[str]) -> None:
    """Print the groups whose discriminant proves less than they claim.

    :param overclaims: What :func:`group_constraint_failures` returned.
    """
    if not overclaims:
        return
    print(f"\n{len(overclaims)} groups claim more than their discriminant proves:")
    for overclaim in overclaims:
        print(f"  {overclaim}")


def _drop_failures(
    before: Counter[Fingerprint],
    removed: Counter[Fingerprint],
    retained: Sequence[Retained],
) -> list[str]:
    """Return a failure line per rule whose drop disagrees with its artifact count.

    A retained fingerprint is excluded from **both** sides rather than cancelled
    out of ``expected`` alone. Its drop is not governed by the suppression
    invariant this reconciles: the entry says the artifact stays reportable, not
    that every one of its occurrences survives, so a run that legitimately
    resolves one of two colliding sites still leaves the other reporting. Netting
    the entry to zero on the ``expected`` side while its partial drop still
    reached ``actual`` failed exactly that run.

    :param before: Fingerprint multiset from the baseline manifest.
    :param removed: Fingerprints present in the baseline and absent afterwards.
    :param retained: Artifacts deliberately left reporting.
    :return: One message per mismatched rule, naming the rule and the delta.
    """
    kept = {entry.fingerprint for entry in retained}
    expected: Counter[str] = Counter()
    for fingerprint, count in before.items():
        if fingerprint not in kept and classify(fingerprint) is not None:
            expected[fingerprint[1]] += count
    actual: Counter[str] = Counter()
    for fingerprint, count in removed.items():
        if fingerprint not in kept:
            actual[fingerprint[1]] += count
    return [
        f"{rule}: dropped {actual[rule]}, expected {expected[rule]} "
        f"(delta {actual[rule] - expected[rule]})"
        for rule in sorted(set(expected) | set(actual))
        if expected[rule] != actual[rule]
    ]


def check_manifest(
    baseline: Sequence[Diagnostic],
    current: Sequence[Diagnostic],
    retained: Sequence[Retained],
) -> list[str]:
    """Return every reconciliation failure between the baseline and the current run.

    :param baseline: Diagnostics captured before any suppression landed.
    :param current: Diagnostics from the tree under test.
    :param retained: Artifacts deliberately left reporting.
    :return: A failure message per breach, empty when the run reconciles.
    """
    before: Counter[Fingerprint] = Counter(d.fingerprint for d in baseline)
    after: Counter[Fingerprint] = Counter(d.fingerprint for d in current)
    removed = before - after
    kept = {entry.fingerprint for entry in retained}

    failures = [
        f"suppressed a first-party diagnostic (x{count}): {_render(fingerprint)}"
        for fingerprint, count in sorted(removed.items())
        if classify(fingerprint) is None
    ]
    failures.extend(
        f"artifact still reports: {diagnostic}"
        for diagnostic in current
        if _is_artifact(diagnostic) and diagnostic.fingerprint not in kept
    )
    failures.extend(
        f"RETAINED entry no longer matches: {_render(entry.fingerprint)}"
        for entry in retained
        if entry.fingerprint not in after
    )
    failures.extend(_drop_failures(before, removed, retained))
    return failures


def _read_manifest(path: Path) -> list[Diagnostic]:
    """Load the diagnostics a previous ``baseline`` run recorded.

    :param path: The manifest file.
    :return: The recorded diagnostics.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Diagnostic(*row) for row in payload["diagnostics"]]


def cmd_report(args: argparse.Namespace) -> int:
    """Print the classification report for one ty run.

    :param args: Parsed arguments carrying ``source``.
    :return: ``0`` — the report describes, it does not gate.
    :raises ReconciliationError: When the ty run cannot be trusted.
    """
    diagnostics = parse_diagnostics(load_output(args.source))
    severities = Counter(d.severity for d in diagnostics)
    print(
        f"{len(diagnostics)} diagnostics — "
        f"{severities['error']} error, {severities['warning']} warning"
    )
    _print_groups(diagnostics)
    _print_rule_split(diagnostics)
    _print_mechanisms(diagnostics)
    _print_collisions(diagnostics)
    _print_retained()
    _print_overclaims(group_constraint_failures())
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """Write the pre-suppression fingerprint manifest ``check`` reconciles against.

    :param args: Parsed arguments carrying ``source`` and ``out``.
    :return: ``0`` on success.
    :raises ReconciliationError: When the ty run cannot be trusted.
    """
    diagnostics = parse_diagnostics(load_output(args.source))
    payload = {
        "total": len(diagnostics),
        "diagnostics": [
            [d.path, d.line, d.column, d.severity, d.rule, d.message]
            for d in diagnostics
        ],
    }
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"wrote {len(diagnostics)} diagnostics to {args.out}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Report two verdicts: the run against its baseline, and the group table.

    Both gate. The reconciliation is per-run; the table audit
    (:func:`group_constraint_failures`) is a static property of :data:`GROUPS`,
    so it can fail a run whose fingerprints reconcile perfectly — an
    over-claiming group is what makes a suppression untrustworthy in the first
    place, and there is no run against which it is benign.

    The STALE list is the one thing here that does *not* move the exit status. A
    group that matches nothing in the baseline is either drift — in which case
    the diagnostics it used to claim are unclassified, and suppressing them
    already fails the reconciliation below — or an artifact class a dependency
    upgrade retired, which leaves a run that lost no first-party diagnostic and
    so has nothing to fail. Naming the group is what points at the mechanism to
    remove; failing on it would red a clean run.

    :param args: Parsed arguments carrying ``source`` and ``baseline``.
    :return: ``0`` when the run reconciles and no group over-claims, ``1``
        otherwise.
    :raises ReconciliationError: When the ty run cannot be trusted.
    """
    baseline = _read_manifest(args.baseline)
    current = parse_diagnostics(load_output(args.source))
    overclaims = group_constraint_failures()
    failures = check_manifest(baseline, current, RETAINED)

    before: Counter[str] = Counter(d.rule for d in baseline if _is_artifact(d))
    after: Counter[str] = Counter(d.rule for d in current)
    print(f"baseline {len(baseline)} diagnostics → current {len(current)}")
    print(f"  {'rule':32s} {'artifacts':>9s} {'remaining':>9s}")
    for rule in sorted(before, key=lambda name: (-before[name], name)):
        print(f"  {rule:32s} {before[rule]:9d} {after[rule]:9d}")

    stale = [name for name, count in sorted(group_hits(baseline).items()) if not count]
    print(f"\nSTALE groups (matched nothing in the baseline, advisory): {len(stale)}")
    for name in stale:
        print(f"  {name}")
    _print_retained()

    _print_overclaims(overclaims)

    if failures:
        print(f"\n{len(failures)} reconciliation failures:")
        for failure in failures:
            print(f"  {failure}")
    if failures or overclaims:
        return 1
    print("\nreconciled: every dropped fingerprint is a classified artifact")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    :param argv: Arguments to parse, or ``None`` to read ``sys.argv``.
    :return: The process exit status.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--from",
        dest="source",
        type=Path,
        help="read captured `ty check --output-format concise` output instead of "
        "running ty",
    )
    parser = argparse.ArgumentParser(description=SUMMARY)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("report", parents=[common]).set_defaults(run=cmd_report)
    emit = modes.add_parser("baseline", parents=[common])
    emit.add_argument("--out", type=Path, required=True)
    emit.set_defaults(run=cmd_baseline)
    verify = modes.add_parser("check", parents=[common])
    verify.add_argument("--baseline", type=Path, required=True)
    verify.set_defaults(run=cmd_check)

    args = parser.parse_args(argv)
    try:
        return int(args.run(args))
    except ReconciliationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
