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

"""Tests for the ``scripts/changelog.py`` CLI."""

import subprocess

import pytest

from tests.scripts import load_script

changelog = load_script("changelog")


SAMPLE_CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

## [v0.11.0] - 2026-04-02

### Added

- SEP-100: Old feature

[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.11.0...HEAD
[v0.11.0]: https://github.com/percona/pmm-extensions/compare/v0.10.0...v0.11.0
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Set up a fresh repo layout in ``tmp_path`` and chdir into it.

    :param tmp_path: pytest's per-test temporary directory.
    :type tmp_path: pathlib.Path
    :param monkeypatch: pytest monkeypatch fixture.
    :type monkeypatch: pytest.MonkeyPatch
    :return: The temporary repo root.
    :rtype: pathlib.Path
    """
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE_CHANGELOG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- add subcommand --------------------------------------------------------


@pytest.mark.parametrize("ticket", ["SEP-503", "PMM-15326"])
def test_add_creates_fragment(repo, ticket):
    """Write a fragment file with the given message.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param ticket: The ticket key.
    """
    exit_code = changelog.main(
        ["add", "--ticket", ticket, "--section", "added", "--message", "New alert"],
    )
    assert exit_code == 0
    fragment = repo / "changelog.d" / f"{ticket}.added.md"
    assert fragment.exists()
    assert fragment.read_text(encoding="utf-8") == "New alert.\n"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("New alert", "New alert."),
        ("New alert.", "New alert."),
        ("Is it ready?", "Is it ready?"),
        ("Stop!", "Stop!"),
        ("Pass `--force` to overwrite", "Pass `--force` to overwrite."),
        ('He said "stop."', 'He said "stop."'),
        ("Drop the flag (deprecated)", "Drop the flag (deprecated)."),
    ],
)
def test_add_normalizes_terminal_punctuation(repo, message, expected):
    """Verify that ``add`` appends a terminal period only when the message lacks sentence punctuation.

    :param repo: Test repo fixture.
    :param message: The description passed to ``add``.
    :param expected: The description as written to the fragment.
    """
    exit_code = changelog.main(
        ["add", "--ticket", "SEP-503", "--section", "added", "--message", message],
    )
    assert exit_code == 0
    fragment = repo / "changelog.d" / "SEP-503.added.md"
    assert fragment.read_text(encoding="utf-8") == f"{expected}\n"


def test_add_rejects_duplicate_without_force(repo, capsys):
    """``add`` fails if the fragment already exists and ``--force`` is not set.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("First\n", encoding="utf-8")
    exit_code = changelog.main(
        ["add", "--ticket", "SEP-503", "--section", "added", "--message", "Second"],
    )
    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err
    assert (repo / "changelog.d" / "SEP-503.added.md").read_text(
        encoding="utf-8",
    ) == "First\n"


def test_add_force_overwrites(repo):
    """``add --force`` overwrites an existing fragment.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("First\n", encoding="utf-8")
    exit_code = changelog.main(
        [
            "add",
            "--ticket",
            "SEP-503",
            "--section",
            "added",
            "--message",
            "Second",
            "--force",
        ],
    )
    assert exit_code == 0
    assert (repo / "changelog.d" / "SEP-503.added.md").read_text(
        encoding="utf-8",
    ) == "Second.\n"


def test_add_rejects_invalid_ticket(repo, capsys):
    """Reject ticket keys with an unsupported project prefix.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    exit_code = changelog.main(
        ["add", "--ticket", "FOO-1", "--section", "added", "--message", "x"],
    )
    assert exit_code == 1
    error = capsys.readouterr().err
    assert "invalid ticket" in error
    assert "SEP-<n>" in error
    assert "PMM-<n>" in error


def test_add_rejects_multiline_message(repo, capsys):
    """``add`` refuses a message that spans multiple lines.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    exit_code = changelog.main(
        [
            "add",
            "--ticket",
            "SEP-503",
            "--section",
            "added",
            "--message",
            "line one\nline two",
        ],
    )
    assert exit_code == 1
    assert "single line" in capsys.readouterr().err


# --- check subcommand ------------------------------------------------------


def test_check_passes_on_empty_dir(repo):
    """``check`` exits 0 when ``changelog.d/`` contains no fragments.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    assert changelog.main(["check"]) == 0


@pytest.mark.parametrize("project", ["SEP", "PMM"])
def test_check_passes_on_valid_fragments(repo, project):
    """Accept well-formed fragments from supported projects.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param project: The ticket project prefix.
    """
    (repo / "changelog.d" / f"{project}-503.added.md").write_text(
        "Fix\n", encoding="utf-8"
    )
    (repo / "changelog.d" / f"{project}-816.changed.md").write_text(
        "Tweak\n", encoding="utf-8"
    )
    assert changelog.main(["check"]) == 0


@pytest.mark.parametrize("section", sorted(changelog.VALID_SECTIONS))
def test_load_pmm_fragment(repo, section):
    """Load and render a PMM fragment in each supported section.

    :param repo: Test repo fixture.
    :param section: The short section name.
    """
    fragment = repo / "changelog.d" / f"PMM-15326.{section}.md"
    fragment.write_text("New feature.\nAnother entry.\n", encoding="utf-8")

    grouped = changelog.load_fragments(repo / "changelog.d")

    assert grouped == {
        changelog.SECTION_MAP[section]: [
            ("PMM-15326", ["New feature.", "Another entry."], fragment),
        ],
    }
    assert changelog.render_section_body(grouped) == (
        f"### {changelog.SECTION_MAP[section]}\n\n"
        "- PMM-15326: New feature.\n- PMM-15326: Another entry.\n"
    )


def test_load_rejects_unknown_project(repo):
    """Reject unknown projects and name all accepted filename forms.

    :param repo: Test repo fixture.
    """
    (repo / "changelog.d" / "FOO-1.added.md").write_text("x\n", encoding="utf-8")

    with pytest.raises(changelog.FragmentError, match="FOO-1.added.md") as exc:
        changelog.load_fragments()

    assert "SEP-<n>.<section>.md" in str(exc.value)
    assert "PMM-<n>.<section>.md" in str(exc.value)


def test_check_fails_on_malformed_filename(repo, capsys):
    """``check`` rejects fragments whose filename does not match the pattern.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    (repo / "changelog.d" / "SEP-503.bogus.md").write_text("x\n", encoding="utf-8")
    assert changelog.main(["check"]) == 1
    assert "invalid filename" in capsys.readouterr().err


def test_check_fails_on_empty_fragment(repo, capsys):
    """``check`` rejects fragments whose content is empty after stripping.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("   \n\n", encoding="utf-8")
    assert changelog.main(["check"]) == 1
    assert "empty" in capsys.readouterr().err


def test_check_fails_on_bullet_prefix(repo, capsys):
    """``check`` rejects fragments that contain the leading ``- SEP-XXX:`` prefix.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text(
        "- SEP-503: Fix\n",
        encoding="utf-8",
    )
    assert changelog.main(["check"]) == 1
    assert "must not start with" in capsys.readouterr().err


def test_check_ignores_readme(repo):
    """``check`` ignores ``README.md`` inside ``changelog.d/``.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "README.md").write_text(
        "# Changelog fragments\n",
        encoding="utf-8",
    )
    (repo / "changelog.d" / "SEP-503.added.md").write_text("Fix\n", encoding="utf-8")
    assert changelog.main(["check"]) == 0


def test_check_ignores_git_ignored_files(tmp_path, monkeypatch):
    """Skip a file git ignores instead of calling it a malformed fragment.

    Runs against a real repository so the ``git ls-files`` invocation itself is
    exercised, not a stand-in for it.

    :param tmp_path: pytest's per-test temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("notes.md\n", encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "notes.md").write_text("scratch\n", encoding="utf-8")
    (tmp_path / "changelog.d" / "SEP-503.added.md").write_text(
        "Fix\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert changelog.main(["check"]) == 0


def test_check_still_reports_a_typo_git_does_not_ignore(tmp_path, monkeypatch):
    """Report a misnamed fragment git does not ignore.

    The ignore skip must not degrade into "accept anything that misses
    ``FRAGMENT_RE``", which is the check this command exists for.

    :param tmp_path: pytest's per-test temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "SEP1892.added.md").write_text(
        "Typo\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert changelog.main(["check"]) == 1


def test_check_considers_every_file_without_git(repo, capsys):
    """Report an unmatched name when git cannot answer which files are ignored.

    :param repo: Test repo fixture (a plain directory, not a git checkout).
    :param capsys: pytest stdout/stderr capture fixture.
    """
    (repo / "changelog.d" / "notes.md").write_text("scratch\n", encoding="utf-8")
    assert changelog.main(["check"]) == 1
    assert "notes.md: invalid filename" in capsys.readouterr().err


# --- list subcommand -------------------------------------------------------


def test_list_groups_and_sorts_by_ticket(repo, capsys):
    """``list`` groups entries by section and sorts by numeric ticket ID.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    (repo / "changelog.d" / "SEP-818.changed.md").write_text("A\n", encoding="utf-8")
    (repo / "changelog.d" / "SEP-503.added.md").write_text("B\n", encoding="utf-8")
    (repo / "changelog.d" / "SEP-100.added.md").write_text("C\n", encoding="utf-8")
    exit_code = changelog.main(["list"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.index("- SEP-100: C") < output.index("- SEP-503: B")
    assert output.index("### Added") < output.index("### Changed")


def test_list_sorts_by_project_then_numeric_ticket(repo, capsys):
    """Group tickets alphabetically by project and numerically within each.

    :param repo: Test repo fixture.
    :param capsys: pytest output capture fixture.
    """
    for ticket in ("SEP-2083", "PMM-15326", "SEP-2", "PMM-2", "SEP-10", "PMM-10"):
        (repo / "changelog.d" / f"{ticket}.added.md").write_text(
            "Entry.\n", encoding="utf-8"
        )

    assert changelog.main(["list"]) == 0
    assert capsys.readouterr().out == (
        "### Added\n\n"
        "- PMM-2: Entry.\n"
        "- PMM-10: Entry.\n"
        "- PMM-15326: Entry.\n"
        "- SEP-2: Entry.\n"
        "- SEP-10: Entry.\n"
        "- SEP-2083: Entry.\n"
    )


# --- assemble subcommand ---------------------------------------------------


def test_assemble_inserts_new_section(repo):
    """``assemble`` inserts a new ``[vX.Y.Z]`` section after ``[Unreleased]``.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text(
        "New alert\n",
        encoding="utf-8",
    )
    exit_code = changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
        ],
    )
    assert exit_code == 0
    content = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [v0.12.0] - 2026-04-30" in content
    assert "- SEP-503: New alert" in content
    unreleased_idx = content.index("## [Unreleased]")
    new_idx = content.index("## [v0.12.0]")
    old_idx = content.index("## [v0.11.0]")
    assert unreleased_idx < new_idx < old_idx


def test_assemble_updates_compare_links(repo):
    """``assemble`` rewrites the ``[Unreleased]`` compare link and adds a new one.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("Fix\n", encoding="utf-8")
    changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
        ],
    )
    content = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert (
        "[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.12.0...HEAD"
        in content
    )
    assert (
        "[v0.12.0]: https://github.com/percona/pmm-extensions/compare/v0.11.0...v0.12.0"
        in content
    )
    # old unreleased link should be gone
    assert "compare/v0.11.0...HEAD" not in content


def test_assemble_deletes_consumed_fragments(repo):
    """``assemble`` removes fragments belonging to the target fix version.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    fragment = repo / "changelog.d" / "SEP-503.added.md"
    fragment.write_text("Fix\n", encoding="utf-8")
    changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
        ],
    )
    assert not fragment.exists()


def test_assemble_preserves_post_scope_lock_fragments(repo):
    """``assemble`` leaves fragments whose ticket is not in ``--tickets`` untouched.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    consumed = repo / "changelog.d" / "SEP-503.added.md"
    consumed.write_text("Scope\n", encoding="utf-8")
    kept = repo / "changelog.d" / "SEP-999.added.md"
    kept.write_text("PostScopeLock\n", encoding="utf-8")
    changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
        ],
    )
    assert not consumed.exists()
    assert kept.exists()
    content = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- SEP-503: Scope" in content
    assert "SEP-999" not in content


def test_assemble_dry_run_touches_nothing(repo):
    """``assemble --dry-run`` prints the rendered block but does not modify files.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("Fix\n", encoding="utf-8")
    original_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    exit_code = changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
            "--dry-run",
        ],
    )
    assert exit_code == 0
    assert (repo / "changelog.d" / "SEP-503.added.md").exists()
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original_changelog


def test_assemble_multiline_fragment_produces_multiple_entries(repo):
    """A fragment containing multiple non-empty lines is rendered as one entry per line.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-491.config.md").write_text(
        "First setting\nSecond setting\n",
        encoding="utf-8",
    )
    changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-491",
        ],
    )
    content = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- SEP-491: First setting" in content
    assert "- SEP-491: Second setting" in content


def test_assemble_empty_ticket_list_is_error(repo):
    """Passing an empty ticket list is treated as an error and leaves files unchanged.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-503.added.md").write_text("Fix\n", encoding="utf-8")
    original = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    exit_code = changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "",
        ],
    )
    assert exit_code == 1
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == original
    assert (repo / "changelog.d" / "SEP-503.added.md").exists()


def test_assemble_refuses_duplicate_version(repo, capsys):
    """``assemble`` refuses to run if the version section already exists.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    :param capsys: pytest output capture fixture.
    :type capsys: pytest.CaptureFixture
    """
    existing = """\
# Changelog

Intro text.

## [Unreleased]

## [v0.12.0] - 2026-04-15

### Added

- SEP-503: Already assembled

## [v0.11.0] - 2026-04-02

### Added

- SEP-100: Old feature

[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.12.0...HEAD
[v0.12.0]: https://github.com/percona/pmm-extensions/compare/v0.11.0...v0.12.0
[v0.11.0]: https://github.com/percona/pmm-extensions/compare/v0.10.0...v0.11.0
"""
    (repo / "CHANGELOG.md").write_text(existing, encoding="utf-8")
    fragment = repo / "changelog.d" / "SEP-503.added.md"
    fragment.write_text("New alert\n", encoding="utf-8")
    exit_code = changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-503",
        ],
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "already contains" in err
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == existing
    assert fragment.exists()


def test_assemble_preserves_section_order(repo):
    """``assemble`` renders sections in the canonical order regardless of fragment order.

    :param repo: Test repo fixture.
    :type repo: pathlib.Path
    """
    (repo / "changelog.d" / "SEP-200.security.md").write_text("CVE\n", encoding="utf-8")
    (repo / "changelog.d" / "SEP-100.added.md").write_text(
        "Feature\n", encoding="utf-8"
    )
    (repo / "changelog.d" / "SEP-150.breaking.md").write_text(
        "Break\n", encoding="utf-8"
    )
    (repo / "changelog.d" / "SEP-175.fixed.md").write_text("Bug\n", encoding="utf-8")
    changelog.main(
        [
            "assemble",
            "--version",
            "0.12.0",
            "--date",
            "2026-04-30",
            "--tickets",
            "SEP-100,SEP-150,SEP-175,SEP-200",
        ],
    )
    content = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    added_idx = content.index("### Added")
    breaking_idx = content.index("### Breaking Changes")
    fixed_idx = content.index("### Fixed")
    security_idx = content.index("### Security")
    assert added_idx < breaking_idx < fixed_idx < security_idx


SAMPLE_CHANGELOG_NO_UNRELEASED_FOOTER = """\
# Changelog

Intro text.

## [Unreleased]

## [v0.12.1] - 2026-05-05

### Fixed

- SEP-1093: Restore chained task dispatch

[v0.12.1]: https://github.com/percona/pmm-extensions/compare/v0.12.0...v0.12.1
"""


def test_update_compare_links_synthesizes_when_unreleased_footer_missing(
    repo,
    monkeypatch,
):
    """`_update_compare_links` writes both links when [Unreleased]: is absent.

    This is the post-transition state: the broken `[Unreleased]: v0.12.1...HEAD`
    line was removed by hand for v0.12.x; the next release (v0.13.0) must still
    produce a complete footer.
    """
    repo.joinpath("CHANGELOG.md").write_text(
        SAMPLE_CHANGELOG_NO_UNRELEASED_FOOTER,
        encoding="utf-8",
    )
    # Set up one consumed fragment so assemble has something to do.
    repo.joinpath("changelog.d", "SEP-200.added.md").write_text(
        "New thing\n",
        encoding="utf-8",
    )
    exit_code = changelog.main(
        [
            "assemble",
            "--version",
            "0.13.0",
            "--date",
            "2026-06-01",
            "--tickets",
            "SEP-200",
        ],
    )
    assert exit_code == 0
    text = repo.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    unreleased_line = (
        "[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.13.0...HEAD"
    )
    new_link_line = (
        "[v0.13.0]: https://github.com/percona/pmm-extensions/compare/v0.12.1...v0.13.0"
    )
    old_link_line = (
        "[v0.12.1]: https://github.com/percona/pmm-extensions/compare/v0.12.0...v0.12.1"
    )
    assert unreleased_line in text
    assert new_link_line in text
    # Newest-first ordering: synthesized lines slot in at the start of the
    # footer block, above the pre-existing ``[v0.12.1]:`` line.
    unreleased_idx = text.index(unreleased_line)
    new_link_idx = text.index(new_link_line)
    old_link_idx = text.index(old_link_line)
    assert unreleased_idx < new_link_idx < old_link_idx


# --- resolve-backmerge subcommand ------------------------------------------

MAIN_CHANGELOG_FIXTURE = """\
# Changelog

Intro text.

## [Unreleased]

<!-- comment -->

## [v0.12.1] - 2026-05-05

### Fixed

- SEP-1093: Old fix

[v0.12.1]: https://github.com/percona/pmm-extensions/compare/v0.12.0...v0.12.1
"""

RELEASE_CHANGELOG_FIXTURE = """\
# Changelog

Intro text.

## [Unreleased]

## [v0.13.0] - 2026-06-01

### Added

- SEP-200: New thing
- SEP-201: Another new thing

### Fixed

- SEP-205: A fix

## [v0.12.1] - 2026-05-05

### Fixed

- SEP-1093: Old fix

[v0.13.0]: https://github.com/percona/pmm-extensions/compare/v0.12.1...v0.13.0
[v0.12.1]: https://github.com/percona/pmm-extensions/compare/v0.12.0...v0.12.1
"""

# Fake merge-base SHA used by resolve-backmerge tests to monkeypatch
# ``_git_merge_base`` — represents the scope-lock commit (common ancestor of
# main and the release branch at the point the release was cut).
FAKE_MERGE_BASE = "abcdef1234567890"


def test_resolve_backmerge_merges_changelog_and_prunes_fragments(
    repo,
    monkeypatch,
):
    """Happy path: release consumed all 5 pre-scope-lock fragments.

    Simulates ``git merge --no-ff release/v0.13.0`` having produced a
    conflict on CHANGELOG.md and accumulated fragments under changelog.d/.
    All 5 fragments existed at the merge-base and were consumed by the
    release branch; none were added to main after scope-lock.  The
    'post-lock preservation' case is covered separately by
    ``test_resolve_backmerge_preserves_post_scope_lock_fragments``.
    """
    for ticket, section, body in [
        ("SEP-200", "added", "New thing\n"),
        ("SEP-201", "added", "Another new thing\n"),
        ("SEP-205", "fixed", "A fix\n"),
        ("SEP-300", "added", "Pre-scope-lock work\n"),
        ("SEP-301", "fixed", "Another pre-scope-lock\n"),
    ]:
        repo.joinpath("changelog.d", f"{ticket}.{section}.md").write_text(
            body,
            encoding="utf-8",
        )
    repo.joinpath("changelog.d", "README.md").write_text(
        "guide\n",
        encoding="utf-8",
    )
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if path != "CHANGELOG.md":
            raise AssertionError(f"unexpected path {path}")
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return RELEASE_CHANGELOG_FIXTURE
        raise AssertionError(f"unexpected ref {ref}")

    def fake_merge_base(a: str, b: str) -> str:
        assert {a, b} == {"HEAD", "MERGE_HEAD"}
        return FAKE_MERGE_BASE

    def fake_ls_tree(ref: str, path: str) -> set[str]:
        if ref == FAKE_MERGE_BASE:
            return {
                "SEP-200.added.md",
                "SEP-201.added.md",
                "SEP-205.fixed.md",
                "SEP-300.added.md",
                "SEP-301.fixed.md",
                "README.md",
            }
        if ref == "MERGE_HEAD":
            return {"README.md"}
        raise AssertionError(f"unexpected ref {ref}")

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    monkeypatch.setattr(changelog, "_git_merge_base", fake_merge_base)
    monkeypatch.setattr(changelog, "_git_ls_tree", fake_ls_tree)

    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 0

    merged = repo.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [v0.13.0] - 2026-06-01" in merged
    assert "## [Unreleased]" in merged
    assert "<!-- comment -->" in merged
    assert (
        "[Unreleased]: https://github.com/percona/pmm-extensions/compare/v0.13.0...HEAD"
        in merged
    )
    assert (
        "[v0.13.0]: https://github.com/percona/pmm-extensions/compare/v0.12.1...v0.13.0"
        in merged
    )
    assert (
        "[v0.12.1]: https://github.com/percona/pmm-extensions/compare/v0.12.0...v0.12.1"
        in merged
    )

    assert not repo.joinpath("changelog.d", "SEP-200.added.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-201.added.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-205.fixed.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-300.added.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-301.fixed.md").exists()
    assert repo.joinpath("changelog.d", "README.md").exists()


def test_resolve_backmerge_when_no_fragments_consumed(repo, monkeypatch):
    """A release whose bullets all come from release-branch-only fixes.

    No changelog.d/ fragments are pruned (the release branch's changelog.d/
    contains the same fragments as main's — nothing was consumed). The
    CHANGELOG merge still completes successfully.
    """
    repo.joinpath("changelog.d", "SEP-300.added.md").write_text(
        "Unrelated\n",
        encoding="utf-8",
    )
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return RELEASE_CHANGELOG_FIXTURE
        raise AssertionError(ref)

    def fake_merge_base(a: str, b: str) -> str:
        assert {a, b} == {"HEAD", "MERGE_HEAD"}
        return FAKE_MERGE_BASE

    def fake_ls_tree(ref: str, path: str) -> set[str]:
        if ref == FAKE_MERGE_BASE:
            return {"SEP-300.added.md"}
        if ref == "MERGE_HEAD":
            return {"SEP-300.added.md"}
        raise AssertionError(ref)

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    monkeypatch.setattr(changelog, "_git_merge_base", fake_merge_base)
    monkeypatch.setattr(changelog, "_git_ls_tree", fake_ls_tree)
    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 0
    assert repo.joinpath("changelog.d", "SEP-300.added.md").exists()


def test_resolve_backmerge_errors_when_release_section_missing(repo, monkeypatch):
    """When theirs (release) doesn't have a ``## [vX.Y.Z]`` heading, error.

    Surfaces a release-branch state mismatch (the assembler didn't run, or
    the wrong --release version was passed) rather than silently writing a
    half-merged file.
    """
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return MAIN_CHANGELOG_FIXTURE
        raise AssertionError(ref)

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    # _git_ls_tree is not reached because _extract_version_section errors first.
    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 1


def test_resolve_backmerge_handles_unconflicted_changelog(repo, monkeypatch):
    """Works even when CHANGELOG.md was auto-merged without conflict.

    The script reads ours from HEAD and theirs from MERGE_HEAD, so it
    doesn't depend on index stages 2/3 being populated.
    """
    repo.joinpath("changelog.d", "SEP-200.added.md").write_text(
        "New thing\n",
        encoding="utf-8",
    )
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if path != "CHANGELOG.md":
            raise AssertionError(path)
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return RELEASE_CHANGELOG_FIXTURE
        raise AssertionError(ref)

    def fake_merge_base(a: str, b: str) -> str:
        assert {a, b} == {"HEAD", "MERGE_HEAD"}
        return FAKE_MERGE_BASE

    def fake_ls_tree(ref: str, path: str) -> set[str]:
        if ref == FAKE_MERGE_BASE:
            return {"SEP-200.added.md"}
        if ref == "MERGE_HEAD":
            return set()
        raise AssertionError(ref)

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    monkeypatch.setattr(changelog, "_git_merge_base", fake_merge_base)
    monkeypatch.setattr(changelog, "_git_ls_tree", fake_ls_tree)

    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 0
    assert not repo.joinpath("changelog.d", "SEP-200.added.md").exists()


def test_resolve_backmerge_prunes_multiple_fragments_for_one_ticket(
    repo,
    monkeypatch,
):
    """Prune every fragment belonging to one ticket together.

    Both of the ticket's fragments disappear from the release branch's
    ``changelog.d/`` after ``cmd_assemble``, so the directory diff catches
    both.
    """
    repo.joinpath("changelog.d", "SEP-200.added.md").write_text(
        "Feature\n",
        encoding="utf-8",
    )
    repo.joinpath("changelog.d", "SEP-200.fixed.md").write_text(
        "Bug fix\n",
        encoding="utf-8",
    )
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return RELEASE_CHANGELOG_FIXTURE
        raise AssertionError(ref)

    def fake_merge_base(a: str, b: str) -> str:
        assert {a, b} == {"HEAD", "MERGE_HEAD"}
        return FAKE_MERGE_BASE

    def fake_ls_tree(ref: str, path: str) -> set[str]:
        if ref == FAKE_MERGE_BASE:
            return {"SEP-200.added.md", "SEP-200.fixed.md"}
        if ref == "MERGE_HEAD":
            return set()
        raise AssertionError(ref)

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    monkeypatch.setattr(changelog, "_git_merge_base", fake_merge_base)
    monkeypatch.setattr(changelog, "_git_ls_tree", fake_ls_tree)

    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 0
    assert not repo.joinpath("changelog.d", "SEP-200.added.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-200.fixed.md").exists()


def test_resolve_backmerge_preserves_post_scope_lock_fragments(repo, monkeypatch):
    """Preserve the fragments main added after scope-lock.

    Scenario:
    - The merge-base (scope-lock) carried two fragments.
    - The release branch consumed both via ``cmd_assemble``; ``MERGE_HEAD``
      has only the README.
    - Main added two further fragments after scope-lock.
    - The resolver must delete only the consumed pair, preserving the
      post-scope-lock additions.
    """
    for ticket, section in [
        ("SEP-200", "added"),
        ("SEP-201", "added"),
        ("SEP-400", "added"),
        ("SEP-401", "fixed"),
    ]:
        repo.joinpath("changelog.d", f"{ticket}.{section}.md").write_text(
            "body\n",
            encoding="utf-8",
        )
    repo.joinpath("changelog.d", "README.md").write_text("\n", encoding="utf-8")
    repo.joinpath("CHANGELOG.md").write_text(
        MAIN_CHANGELOG_FIXTURE,
        encoding="utf-8",
    )

    def fake_show_ref(ref: str, path: str) -> str:
        if ref == "HEAD":
            return MAIN_CHANGELOG_FIXTURE
        if ref == "MERGE_HEAD":
            return RELEASE_CHANGELOG_FIXTURE
        raise AssertionError(ref)

    def fake_merge_base(a: str, b: str) -> str:
        assert {a, b} == {"HEAD", "MERGE_HEAD"}
        return FAKE_MERGE_BASE

    def fake_ls_tree(ref: str, path: str) -> set[str]:
        if ref == FAKE_MERGE_BASE:
            return {"SEP-200.added.md", "SEP-201.added.md", "README.md"}
        if ref == "MERGE_HEAD":
            return {"README.md"}
        raise AssertionError(ref)

    monkeypatch.setattr(changelog, "_git_show_ref", fake_show_ref)
    monkeypatch.setattr(changelog, "_git_merge_base", fake_merge_base)
    monkeypatch.setattr(changelog, "_git_ls_tree", fake_ls_tree)

    exit_code = changelog.main(["resolve-backmerge", "--release", "0.13.0"])
    assert exit_code == 0
    # Consumed fragments deleted.
    assert not repo.joinpath("changelog.d", "SEP-200.added.md").exists()
    assert not repo.joinpath("changelog.d", "SEP-201.added.md").exists()
    # Post-scope-lock fragments preserved.
    assert repo.joinpath("changelog.d", "SEP-400.added.md").exists()
    assert repo.joinpath("changelog.d", "SEP-401.fixed.md").exists()
    # README preserved.
    assert repo.joinpath("changelog.d", "README.md").exists()
