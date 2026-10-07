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

"""Tests for the ``scripts/classify_ty_diagnostics.py`` CLI."""

import re

import pytest

from tests.scripts import load_script, write_file

classify_ty_diagnostics = load_script("classify_ty_diagnostics")

ARTIFACT_LINE = 12
ARTIFACT_COLUMN = 9
DUPLICATE_ROWS = 3
ARTIFACT_ROW = (
    "tests/app/core/test_config.py:12:9: warning[unknown-argument] "
    "Argument `_env_file` does not match any known parameter"
)
FIRST_PARTY_ROW = (
    "tests/app/extensions/test_config.py:145:9: warning[unknown-argument] "
    "Argument `PMM` does not match any known parameter"
)
NOTE_BLOCK = (
    "app/core/db/crud.py:227:24: warning[no-matching-overload] "
    "No overload of bound method `AsyncSession.exec` matches arguments\n"
    "        🚨 You probably want to use `session.exec()` instead of `session.execute()`.\n"
    "        This is the original SQLAlchemy `session.execute()` method that returns objects\n"
    "        ```Python\n"
    "        heroes = await session.exec(select(Hero)).all()\n"
    "        ```\n"
)


def _output(*rows, total=None):
    """Render ``rows`` as ty concise output with a ``Found N diagnostics`` trailer.

    :param rows: Raw output lines, each already newline-free or note-block shaped.
    :param total: The count to write into the trailer; defaults to ``len(rows)``.
    :return: The rendered output text.
    """
    body = "".join(row if row.endswith("\n") else f"{row}\n" for row in rows)
    count = len(rows) if total is None else total
    return f"{body}Found {count} diagnostics\n"


def _manifest(tmp_path, before_text):
    """Emit a baseline manifest from ``before_text`` and return its path.

    :param tmp_path: pytest's per-test temporary directory.
    :param before_text: ty concise output to capture as the baseline.
    :return: The path the manifest was written to.
    """
    source = write_file(tmp_path, "before.txt", before_text)
    manifest = tmp_path / "baseline.json"
    assert (
        classify_ty_diagnostics.main(
            ["baseline", "--from", str(source), "--out", str(manifest)]
        )
        == 0
    )
    return manifest


def _check(tmp_path, manifest, after_text):
    """Run ``check`` over ``after_text`` against ``manifest`` and return its exit code.

    :param tmp_path: pytest's per-test temporary directory.
    :param manifest: The baseline manifest path.
    :param after_text: ty concise output for the post-suppression run.
    :return: The CLI exit status.
    """
    source = write_file(tmp_path, "after.txt", after_text)
    return classify_ty_diagnostics.main(
        ["check", "--from", str(source), "--baseline", str(manifest)]
    )


def test_parse_captures_rule_and_message():
    """Capture the rule and message of a concise diagnostic row."""
    (diagnostic,) = classify_ty_diagnostics.parse_diagnostics(_output(ARTIFACT_ROW))

    assert diagnostic.path == "tests/app/core/test_config.py"
    assert diagnostic.line == ARTIFACT_LINE
    assert diagnostic.column == ARTIFACT_COLUMN
    assert diagnostic.rule == "unknown-argument"
    assert (
        diagnostic.message == "Argument `_env_file` does not match any known parameter"
    )


def test_parse_ignores_note_block_continuations():
    """Skip the indented continuations of a note block instead of counting them."""
    diagnostics = classify_ty_diagnostics.parse_diagnostics(
        _output(NOTE_BLOCK, total=1)
    )

    assert [d.rule for d in diagnostics] == ["no-matching-overload"]


def test_parse_counts_duplicate_positions_separately():
    """Count repeated ``file:line:col`` rows as distinct diagnostics."""
    diagnostics = classify_ty_diagnostics.parse_diagnostics(
        _output(ARTIFACT_ROW, ARTIFACT_ROW, ARTIFACT_ROW)
    )

    assert len(diagnostics) == DUPLICATE_ROWS


def test_parse_rejects_total_disagreeing_with_trailer():
    """Fail when the parsed row count disagrees with ty's own total."""
    with pytest.raises(
        classify_ty_diagnostics.ReconciliationError, match="ty reported 4"
    ):
        classify_ty_diagnostics.parse_diagnostics(
            _output(ARTIFACT_ROW, FIRST_PARTY_ROW, total=4)
        )


def test_parse_rejects_output_without_trailer():
    """Fail loudly when ty printed no trailer, rather than trusting a truncated run."""
    with pytest.raises(classify_ty_diagnostics.ReconciliationError, match="trailer"):
        classify_ty_diagnostics.parse_diagnostics(f"{ARTIFACT_ROW}\n")


def test_parse_accepts_a_run_with_no_diagnostics():
    """Return an empty list for a clean run rather than raising."""
    assert classify_ty_diagnostics.parse_diagnostics("Found 0 diagnostics\n") == []


def test_classify_splits_a_mixed_unknown_argument_shape():
    """Separate the pydantic-settings kwarg from the first-party one under one rule."""
    artifact, first_party = classify_ty_diagnostics.parse_diagnostics(
        _output(ARTIFACT_ROW, FIRST_PARTY_ROW)
    )

    assert classify_ty_diagnostics.classify(artifact.fingerprint) is not None
    assert classify_ty_diagnostics.classify(first_party.fingerprint) is None


def test_classify_confines_absent_modules_to_the_scaffolded_paths():
    """Treat an unresolvable import outside the known paths as a first-party defect."""
    scaffolded, mistyped = classify_ty_diagnostics.parse_diagnostics(
        _output(
            "tests/app/extensions/apps/framework/golden/task/app.py:9:20: "
            "warning[unresolved-import] Cannot resolve imported module "
            "`app.extensions.apps.golden_task.models`",
            "app/extensions/routes/reports.py:9:20: warning[unresolved-import] "
            "Cannot resolve imported module `app.extensions.reprots`",
        )
    )

    assert classify_ty_diagnostics.classify(scaffolded.fingerprint) is not None
    assert classify_ty_diagnostics.classify(mistyped.fingerprint) is None


def test_classify_names_the_proxy_installed_settings_helpers():
    """Treat a misspelled attribute on a ``*Settings`` receiver as first-party.

    The receiver alone cannot carry the verdict: the proxy installs three helpers
    and every other absent attribute on the same object is an ordinary typo.
    """
    helper, typo, retired = classify_ty_diagnostics.parse_diagnostics(
        _output(
            "tests/app/extensions/routes/test_stream_logs.py:325:5: "
            "warning[unresolved-attribute] Object of type `ExtensionsSettings` has no "
            "attribute `_set_snapshot`",
            "tests/app/extensions/routes/test_stream_logs.py:326:5: "
            "warning[unresolved-attribute] Object of type `ExtensionsSettings` has no "
            "attribute `PMM_typo`",
            "tests/app/extensions/routes/test_stream_logs.py:327:5: "
            "warning[unresolved-attribute] Object of type `ExtensionsSettings` has no "
            "attribute `_setting_class`",
        )
    )

    assert classify_ty_diagnostics.classify(helper.fingerprint) is not None
    assert classify_ty_diagnostics.classify(typo.fingerprint) is None
    assert classify_ty_diagnostics.classify(retired.fingerprint) is None


def test_classify_names_the_runtime_installed_celery_attribute():
    """Treat a misspelled attribute on a ``Celery`` receiver as first-party."""
    installed, typo = classify_ty_diagnostics.parse_diagnostics(
        _output(
            "app/extensions/apps/alerts/celery.py:58:5: warning[unresolved-attribute] "
            "Object of type `Celery` has no attribute `loop`",
            "app/extensions/apps/alerts/celery.py:59:5: warning[unresolved-attribute] "
            "Object of type `Celery` has no attribute `brokr_url`",
        )
    )

    assert classify_ty_diagnostics.classify(installed.fingerprint) is not None
    assert classify_ty_diagnostics.classify(typo.fingerprint) is None


def test_check_passes_when_only_artifacts_were_removed(tmp_path, capsys):
    """Accept a run whose every dropped fingerprint is classified as an artifact."""
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))

    assert _check(tmp_path, manifest, _output(FIRST_PARTY_ROW)) == 0
    assert "unknown-argument" in capsys.readouterr().out


def test_check_fails_when_an_artifact_still_reports(tmp_path, capsys):
    """Reject a run still emitting an artifact that is not listed in ``RETAINED``."""
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))

    assert _check(tmp_path, manifest, _output(ARTIFACT_ROW, FIRST_PARTY_ROW)) == 1
    out = capsys.readouterr().out
    assert "unknown-argument" in out
    assert "tests/app/core/test_config.py" in out


def test_check_fails_when_a_first_party_diagnostic_was_suppressed(tmp_path, capsys):
    """Name the first-party fingerprint a suppression removed, even when totals reconcile."""
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))
    unrelated = (
        "app/extensions/config.py:474:5: warning[invalid-assignment] "
        "Object of type `FieldInfo` is not assignable to `int`"
    )

    assert _check(tmp_path, manifest, _output(unrelated)) == 1
    out = capsys.readouterr().out
    assert "tests/app/extensions/test_config.py" in out
    assert "`PMM`" in out


def test_check_names_the_rule_and_delta_when_the_drop_is_wrong(tmp_path, capsys):
    """Report the rule and the delta when more rows dropped than the classification predicts."""
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))

    assert _check(tmp_path, manifest, _output()) == 1
    assert re.search(
        r"unknown-argument.*dropped 2.*expected 1", capsys.readouterr().out
    )


def test_check_honours_and_reports_a_retained_entry(tmp_path, capsys, monkeypatch):
    """Accept a still-reporting artifact that ``RETAINED`` names, and print the entry."""
    retained = classify_ty_diagnostics.Retained(
        fingerprint=(
            "tests/app/core/test_config.py",
            "unknown-argument",
            "Argument `_env_file` does not match any known parameter",
        ),
        reason="collides on one line with a first-party hit that cannot be split",
    )
    monkeypatch.setattr(classify_ty_diagnostics, "RETAINED", (retained,))
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))

    assert _check(tmp_path, manifest, _output(ARTIFACT_ROW, FIRST_PARTY_ROW)) == 0
    assert retained.reason in capsys.readouterr().out


def test_check_accepts_a_partial_drop_of_a_retained_fingerprint(
    tmp_path, capsys, monkeypatch
):
    """Accept a run that resolves one of a retained fingerprint's several sites.

    A ``RETAINED`` entry says the artifact stays reportable, not that every
    occurrence survives, so fixing one colliding site while the other still
    reports breaches nothing the reconciliation asserts.
    """
    duplicate = (
        "tests/app/core/test_config.py:30:9: warning[unknown-argument] "
        "Argument `_env_file` does not match any known parameter"
    )
    (artifact,) = classify_ty_diagnostics.parse_diagnostics(_output(ARTIFACT_ROW))
    fingerprint = artifact.fingerprint
    monkeypatch.setattr(
        classify_ty_diagnostics,
        "RETAINED",
        (classify_ty_diagnostics.Retained(fingerprint=fingerprint, reason="collides"),),
    )
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, duplicate))

    assert _check(tmp_path, manifest, _output(ARTIFACT_ROW)) == 0
    assert "reconciled" in capsys.readouterr().out


def test_check_fails_when_a_retained_entry_no_longer_matches(
    tmp_path, capsys, monkeypatch
):
    """Reject a stale ``RETAINED`` entry whose fingerprint the run no longer emits."""
    stale = classify_ty_diagnostics.Retained(
        fingerprint=("app/gone.py", "unknown-argument", "Argument `_env_file` x"),
        reason="stale",
    )
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))
    monkeypatch.setattr(classify_ty_diagnostics, "RETAINED", (stale,))

    assert _check(tmp_path, manifest, _output(FIRST_PARTY_ROW)) == 1
    assert "app/gone.py" in capsys.readouterr().out


def test_report_prints_a_zero_row_for_a_group_with_no_hits(tmp_path, capsys):
    """Print a zero row, not a stale warning, for a group the run does not match.

    Every group reaches zero on a neutralized tree, so a zero in ``report`` is the
    expected end state rather than a signal.
    """
    source = write_file(tmp_path, "run.txt", _output(FIRST_PARTY_ROW))

    assert classify_ty_diagnostics.main(["report", "--from", str(source)]) == 0
    out = capsys.readouterr().out
    assert "    0  pydantic-settings-private-kwargs" in out
    assert "STALE" not in out


def test_check_names_a_group_that_matched_nothing_in_the_baseline(tmp_path, capsys):
    """Name a stale group against the baseline without failing an otherwise clean run.

    The baseline is the only run where a zero means drift rather than a group
    doing its job, and naming it is the whole effect: a retired artifact class
    loses no first-party diagnostic, so there is nothing for the gate to reject.

    The expected count is derived rather than written down: exactly one group
    matches this fixture, so every other registered one is stale, and a literal
    would fail the next time a group is added for a reason unrelated to this.
    """
    manifest = _manifest(tmp_path, _output(ARTIFACT_ROW, FIRST_PARTY_ROW))

    assert _check(tmp_path, manifest, _output(FIRST_PARTY_ROW)) == 0
    out = capsys.readouterr().out
    stale_count = len(classify_ty_diagnostics.GROUPS) - 1
    assert (
        f"STALE groups (matched nothing in the baseline, advisory): {stale_count}"
        in out
    )
    assert "pydantic-settings-private-kwargs" not in out.split("STALE groups")[1]


def test_report_flags_a_line_holding_an_artifact_and_a_first_party_hit(
    tmp_path, capsys
):
    """Flag a ``file:line:rule`` a per-site comment cannot discriminate."""
    collision = (
        "tests/app/extensions/test_config.py:145:40: warning[unknown-argument] "
        "Argument `_env_file` does not match any known parameter"
    )
    source = write_file(tmp_path, "run.txt", _output(FIRST_PARTY_ROW, collision))

    assert classify_ty_diagnostics.main(["report", "--from", str(source)]) == 0
    out = capsys.readouterr().out
    assert "tests/app/extensions/test_config.py:145" in out
    assert "Unsuppressable-by-comment sites: 1" in out


def _claiming_groups(fingerprint):
    """Return the names of every registered group claiming ``fingerprint``.

    :param fingerprint: The ``(path, rule, message)`` identity to test.
    :return: The claiming groups' names, in registration order.
    """
    return [
        group.name
        for group in classify_ty_diagnostics.GROUPS
        if group.claims(fingerprint)
    ]


CALL_IN_TYPE_EXPRESSION = "Function calls are not allowed in type expressions"
RULE_CLAIMED_BY_NO_GROUP = "synthetic-rule-a"
SIBLING_RULE_CLAIMED_BY_NO_GROUP = "synthetic-rule-b"


def test_factory_built_alias_group_claims_only_the_module_it_is_confined_to():
    """Confine the call-in-type-expression group to the module holding that site.

    The message names no discriminant: it reads the same for the field-type
    factory the form DSL calls and for an ordinary call someone wrote into a type
    position by mistake. Only the path separates them, so a hit anywhere else has
    to stay first-party — otherwise ``check`` would accept a suppression over a
    genuine defect.
    """
    confined = (
        "app/extensions/apps/mysql_backups/forms.py",
        "invalid-type-form",
        CALL_IN_TYPE_EXPRESSION,
    )
    elsewhere = (
        "app/extensions/routes/users.py",
        "invalid-type-form",
        CALL_IN_TYPE_EXPRESSION,
    )

    assert _claiming_groups(confined) == ["factory-built-annotated-alias"]
    assert _claiming_groups(elsewhere) == []


def test_runtime_computed_model_group_does_not_claim_a_bare_call():
    """Keep the two ``invalid-type-form`` groups from overlapping.

    The runtime-computed-model group matches on its own message, so it is not
    path-confined; that only stays safe while its pattern refuses the call form,
    which carries no evidence of a runtime-computed class.
    """
    fingerprint = (
        "app/extensions/apps/mysql_backups/forms.py",
        "invalid-type-form",
        CALL_IN_TYPE_EXPRESSION,
    )
    groups = {group.name: group for group in classify_ty_diagnostics.GROUPS}

    assert not groups["runtime-computed-model-in-type-position"].claims(fingerprint)
    assert groups["runtime-computed-model-in-type-position"].claims(
        (
            "app/api/routes/users.py",
            "invalid-type-form",
            "Variable of type `type[BaseUser]` is not allowed in a type expression",
        )
    )


def _without_paths(name: str) -> tuple:
    """Return :data:`GROUPS` with one group's path confinement dropped.

    :param name: The group to strip.
    :return: The table, with that group rebuilt carrying no paths.
    """
    return tuple(
        classify_ty_diagnostics._group(
            group.name,
            " ".join(sorted(group.rules)),
            group.pattern.pattern,
            group.discriminant,
        )
        if group.name == name
        else group
        for group in classify_ty_diagnostics.GROUPS
    )


def test_group_constraint_audit_accepts_the_shipped_table():
    """Report nothing for the table as committed.

    This is the pin the rest of the audit's tests are read against: a new group
    that carries no discriminant fails here, so the table cannot grow an
    unconfined entry without a red test.
    """
    assert classify_ty_diagnostics.group_constraint_failures() == []


def test_group_constraint_audit_rejects_a_pattern_quoting_only_a_wildcard():
    """Report a group whose backticks quote a wildcard rather than a symbol.

    Backticks alone are not the discriminant. This pattern claims every
    ``unresolved-attribute`` diagnostic in the tree — including the two typos
    the receiver-predicate tests above pin as first-party — so accepting it
    would let the table launder a genuine defect into a suppressible artifact,
    which is the whole failure the audit exists to catch.
    """
    wildcard = classify_ty_diagnostics._group(
        "wildcard-attributes",
        "unresolved-attribute",
        r"^Object of type `.+` has no attribute `.+`$",
        "nothing the reader could grep",
    )

    (failure,) = classify_ty_diagnostics.group_constraint_failures(groups=(wildcard,))
    assert "wildcard-attributes" in failure
    assert wildcard.claims(
        (
            "app/extensions/apps/alerts/celery.py",
            "unresolved-attribute",
            "Object of type `Celery` has no attribute `brokr_url`",
        )
    )


def test_group_constraint_audit_names_a_group_whose_confinement_is_dropped():
    """Report the call-in-type-expression group once its path is taken away.

    The group's message names no symbol, so the path is its only discriminant —
    removing it must turn the table red, or the audit is asserting nothing about
    the one group it was written for.
    """
    (failure,) = classify_ty_diagnostics.group_constraint_failures(
        groups=_without_paths("factory-built-annotated-alias")
    )

    assert "factory-built-annotated-alias" in failure
    assert "invalid-type-form" in failure


def test_group_constraint_audit_requires_evidence_under_every_claimed_rule():
    """Report a two-rule group the corpus only declines under one of its rules.

    A declined fingerprint shows the pattern discriminating within *that* rule
    and says nothing about the other, where the group still claims every
    matching diagnostic in the tree.

    The rules are synthetic because this module is itself the corpus
    :func:`corpus_fingerprints` reads: a fixture naming a real ty rule would pin
    that rule as first-party for the whole table and relax the audit these tests
    exercise. Naming a rule no group claims keeps the fixture inert.
    """
    two_rules = classify_ty_diagnostics._group(
        "two-rule-no-symbol",
        f"{RULE_CLAIMED_BY_NO_GROUP} {SIBLING_RULE_CLAIMED_BY_NO_GROUP}",
        f"^{CALL_IN_TYPE_EXPRESSION}$",
        "nothing",
    )
    one = [("app/extensions/config.py", RULE_CLAIMED_BY_NO_GROUP, "Some other message")]
    both = [
        *one,
        ("app/extensions/routes/users.py", SIBLING_RULE_CLAIMED_BY_NO_GROUP, "Another"),
    ]

    assert classify_ty_diagnostics.group_constraint_failures(
        groups=(two_rules,), corpus=one
    )
    assert (
        classify_ty_diagnostics.group_constraint_failures(
            groups=(two_rules,), corpus=both
        )
        == []
    )


def test_group_constraint_audit_tightens_when_the_corpus_yields_nothing():
    """Fail an otherwise-cleared group when no corpus evidence is available.

    The unreadable-corpus branch resolves toward strictness, and a caller that
    treated the empty set as "no objection" would turn every unreadable corpus
    into a silent pass. Asserting the direction is what keeps that branch from
    becoming a hole with a test-shaped cover over it.

    The rule is synthetic for the reason given in the test above.
    """
    unconfined = classify_ty_diagnostics._group(
        "no-discriminant",
        RULE_CLAIMED_BY_NO_GROUP,
        f"^{CALL_IN_TYPE_EXPRESSION}$",
        "nothing",
    )
    declined = [
        ("app/extensions/routes/users.py", RULE_CLAIMED_BY_NO_GROUP, "Something else")
    ]

    assert (
        classify_ty_diagnostics.group_constraint_failures(
            groups=(unconfined,), corpus=declined
        )
        == []
    )
    assert classify_ty_diagnostics.group_constraint_failures(
        groups=(unconfined,), corpus=[]
    )


def test_corpus_read_resolves_a_message_bound_to_a_module_constant(tmp_path):
    """Read a fingerprint whose message arrives through a name, not a literal.

    Both spellings are asserted because this suite uses both, and a literal-only
    read would drop whichever one the author of a future test preferred. The
    annotated form is included for the same reason: adding a ``: str`` to a
    constant must not silently shrink the corpus.

    The rule is synthetic because the expected-value tuples below are read back
    out of this module by the very function under test, so a real ty rule here
    would pin itself as first-party for the whole table.
    """
    rule = RULE_CLAIMED_BY_NO_GROUP
    source = write_file(
        tmp_path,
        "corpus.py",
        f'INLINE = ("app/extensions/routes/users.py", "{rule}", "inline message")\n'
        f'BARE = "{CALL_IN_TYPE_EXPRESSION}"\n'
        f'VIA_NAME = ("app/extensions/config.py", "{rule}", BARE)\n'
        f'ANNOTATED: str = "{CALL_IN_TYPE_EXPRESSION} twice"\n'
        f'VIA_ANNOTATED = ("app/extensions/deps.py", "{rule}", ANNOTATED)\n',
    )

    assert classify_ty_diagnostics.corpus_fingerprints(source) == frozenset(
        {
            ("app/extensions/routes/users.py", rule, "inline message"),
            ("app/extensions/config.py", rule, CALL_IN_TYPE_EXPRESSION),
            ("app/extensions/deps.py", rule, f"{CALL_IN_TYPE_EXPRESSION} twice"),
        }
    )


def test_corpus_read_ignores_a_triple_that_is_not_a_fingerprint(tmp_path):
    """Skip three-string tuples whose elements are not a path, rule and message.

    A false positive is not inert: a spurious fingerprint enters the pinned
    corpus, and one landing on an unconfined group's rule would clear that group
    instead of failing it.

    The rule is synthetic for the reason given two tests above.
    """
    rule = RULE_CLAIMED_BY_NO_GROUP
    source = write_file(
        tmp_path,
        "corpus.py",
        'BOGUS = ("a", "b", "c")\n'
        f'NO_PATH = ("not-a-path", "{rule}", "a message")\n'
        f'PAIR = ("app/extensions/config.py", "{rule}")\n'
        f'REAL = ("app/extensions/config.py", "{rule}", "a message")\n',
    )

    assert classify_ty_diagnostics.corpus_fingerprints(source) == frozenset(
        {("app/extensions/config.py", rule, "a message")}
    )


def test_corpus_read_treats_an_undecodable_module_as_absent(tmp_path):
    """Return the empty set for a corpus that is missing, unparseable or not UTF-8.

    All three are "unreadable", and the decode case is the one that reaches the
    reader as a ``ValueError`` rather than an ``OSError`` — left uncaught it
    would propagate out of ``check`` instead of tightening the audit.
    """
    undecodable = tmp_path / "undecodable.py"
    undecodable.write_bytes(b'JUNK = "\xff\xfe"\n')

    assert (
        classify_ty_diagnostics.corpus_fingerprints(tmp_path / "gone.py") == frozenset()
    )
    assert classify_ty_diagnostics.corpus_fingerprints(undecodable) == frozenset()
    assert (
        classify_ty_diagnostics.corpus_fingerprints(
            write_file(tmp_path, "broken.py", "def (:\n")
        )
        == frozenset()
    )


def test_check_fails_on_an_over_claiming_table(tmp_path, capsys, monkeypatch):
    """Exit non-zero from ``check`` when a group claims more than it proves.

    The run reconciles — the same output is both baseline and current, so no
    fingerprint went missing — which is what makes this assert the new verdict
    is independent of the reconciliation rather than riding on it.
    """
    run = _output(FIRST_PARTY_ROW)
    manifest = _manifest(tmp_path, run)
    assert _check(tmp_path, manifest, run) == 0
    capsys.readouterr()

    monkeypatch.setattr(
        classify_ty_diagnostics,
        "GROUPS",
        _without_paths("factory-built-annotated-alias"),
    )

    assert _check(tmp_path, manifest, run) == 1
    assert "factory-built-annotated-alias" in capsys.readouterr().out


#: One row per group whose only discriminant is the symbol its message names:
#: the artifact it is meant to claim, and a first-party diagnostic of the same
#: rule it must decline. The six are the groups carrying neither a path
#: constraint nor a negative pin, so nothing but these tests demonstrates that
#: their patterns separate anything.
SYMBOL_ONLY_GROUPS = [
    (
        "pydantic-fieldinfo",
        (
            "app/extensions/apps/backup/models.py",
            "invalid-assignment",
            "Object of type `FieldInfo` is not assignable to `str`",
        ),
        (
            "app/extensions/apps/backup/models.py",
            "invalid-assignment",
            "Object of type `str` is not assignable to `int`",
        ),
    ),
    (
        "env-populated-required-params",
        (
            "app/core/celery/app.py",
            "missing-argument",
            "No argument provided for required parameter `CELERY`",
        ),
        (
            "app/core/celery/app.py",
            "missing-argument",
            "No argument provided for required parameter `task_id`",
        ),
    ),
    (
        "third-party-overload-sets",
        (
            "app/core/db/crud.py",
            "no-matching-overload",
            "No overload of bound method `AsyncSession.exec` matches arguments",
        ),
        (
            "app/core/db/crud.py",
            "no-matching-overload",
            "No overload of bound method `TaskHistoryManager.save` matches arguments",
        ),
    ),
    (
        "sa-type-typedecorator",
        (
            "app/core/db/types.py",
            "invalid-argument-type",
            "Expected `type[Any] | PydanticUndefinedType`, found `EncryptedString`",
        ),
        (
            "app/core/db/types.py",
            "invalid-argument-type",
            "Expected `str`, found `int`",
        ),
    ),
    (
        "subscripted-generics-called",
        (
            "app/extensions/apps/framework/schema.py",
            "call-non-callable",
            "Object of type `GenericAlias` is not callable",
        ),
        (
            "app/extensions/apps/framework/schema.py",
            "call-non-callable",
            "Object of type `None` is not callable",
        ),
    ),
    (
        "fastapi-query-default",
        (
            "app/extensions/api/routes/delivery_connection.py",
            "invalid-parameter-default",
            "Default value of type `Query` is not assignable to `int`",
        ),
        (
            "app/extensions/api/routes/delivery_connection.py",
            "invalid-parameter-default",
            "Default value of type `None` is not assignable to `str`",
        ),
    ),
]


@pytest.mark.parametrize(("name", "artifact", "first_party"), SYMBOL_ONLY_GROUPS)
def test_a_symbol_only_group_claims_its_artifact_and_declines_its_rule(
    name, artifact, first_party
):
    """Pin what the symbol in each unconfined group's message actually separates.

    These six groups carry no path constraint, so the symbol their pattern
    quotes is the whole of their evidence, and ``group_constraint_failures``
    accepts that symbol without checking it separates anything. A rule that
    reads its own regex cannot: the pattern is the claim under test. Only a
    same-rule diagnostic the group declines shows the discriminant working,
    which is why the audit counts one of these as evidence in its own right.
    """
    assert name in _claiming_groups(artifact)
    assert _claiming_groups(first_party) == []
