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

"""Tests for the ``scripts/check_sidecar_purge.py`` CLI."""

import pytest

from tests.scripts import load_script

check_sidecar_purge = load_script("check_sidecar_purge")


PURGED_PACKAGES = ["perl-base", "gzip", "libncursesw6", "ncurses-base", "ncurses-bin"]

SHIPPED_PURGED_PACKAGES = [
    "perl-base",
    "gzip",
    "libncursesw6",
    "ncurses-base",
    "ncurses-bin",
    "mount",
    "util-linux",
    "login",
    "bsdutils",
    "libuuid1",
    "libblkid1",
    "libmount1",
    "libsmartcols1",
    "liblastlog2-2",
]

PURGE_LAYER = """\
RUN dpkg --purge --force-remove-essential \\
        perl-base \\
        gzip \\
        libncursesw6 \\
        ncurses-base \\
        ncurses-bin
"""

RECIPE_PREFIX = """\
FROM docker.io/library/python:3.14.10-slim

# An apt-get comment before the purge is ordinary prose.
RUN apt-get update && \\
    apt-get install -y --no-install-recommends netcat-openbsd

"""


def _write_recipe(tmp_path, body):
    """Write ``body`` as a Containerfile under ``tmp_path`` and return its path.

    :param tmp_path: pytest's per-test temporary directory.
    :param body: Containerfile contents.
    :return: The newly-written path.
    """
    path = tmp_path / "Containerfile.sidecar"
    path.write_text(body, encoding="utf-8")
    return path


def _instructions(tmp_path, body):
    """Parse ``body`` as a Containerfile written under ``tmp_path``.

    :param tmp_path: pytest's per-test temporary directory.
    :param body: Containerfile contents.
    :return: Parsed ``(line_number, body)`` instruction pairs.
    """
    return check_sidecar_purge.parse_instructions(_write_recipe(tmp_path, body))


def test_parses_the_real_containerfile():
    """Report exactly the packages the shipped side-car recipe purges, in order."""
    instructions = check_sidecar_purge.parse_instructions(
        check_sidecar_purge.CONTAINERFILE
    )
    assert check_sidecar_purge.purged_packages(instructions) == SHIPPED_PURGED_PACKAGES


def test_ordering_passes_on_the_real_containerfile():
    """Find no package-manager instruction after the purge in the shipped recipe."""
    instructions = check_sidecar_purge.parse_instructions(
        check_sidecar_purge.CONTAINERFILE
    )
    assert check_sidecar_purge.check_ordering(instructions) == []


def test_comment_naming_apt_is_not_an_instruction(tmp_path):
    """Ignore a post-purge comment naming apt-get, since comments are not instructions."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\n# apt-get install is impossible from here on.\nUSER extensions\n"
    assert check_sidecar_purge.check_ordering(_instructions(tmp_path, body)) == []


def test_purge_instruction_itself_is_not_an_offender(tmp_path):
    """Exclude the purge instruction's own dpkg invocation from the ordering scan."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}"
    assert check_sidecar_purge.check_ordering(_instructions(tmp_path, body)) == []


def test_apt_instruction_after_purge_is_reported(tmp_path):
    """Report an apt-get instruction placed after the purge, at its own line."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN apt-get install -y foo\n"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert len(offenders) == 1
    lineno, offender_body = offenders[0]
    assert lineno == body.splitlines().index("RUN apt-get install -y foo") + 1
    assert offender_body == "RUN apt-get install -y foo"


def test_dpkg_instruction_after_purge_is_reported(tmp_path):
    """Report a bare dpkg instruction placed after the purge."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN dpkg -i /tmp/foo.deb\n"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert [b for _, b in offenders] == ["RUN dpkg -i /tmp/foo.deb"]


def test_continuation_lines_are_folded(tmp_path):
    """Report a multi-line instruction whose continuation carries apt-get, at its start line."""
    trailing = "RUN echo one && \\\n    apt-get install -y foo\n"
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\n{trailing}"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert len(offenders) == 1
    lineno, offender_body = offenders[0]
    assert lineno == body.splitlines().index("RUN echo one && \\") + 1
    assert offender_body == "RUN echo one && apt-get install -y foo"


def test_missing_purge_instruction_is_a_hard_failure(tmp_path):
    """Raise ``SystemExit`` rather than passing silently when no purge layer exists."""
    instructions = _instructions(tmp_path, RECIPE_PREFIX)
    with pytest.raises(SystemExit, match="No purge instruction found"):
        check_sidecar_purge.check_ordering(instructions)


def test_duplicate_purge_instructions_are_a_hard_failure(tmp_path):
    """Raise ``SystemExit`` when the recipe carries more than one purge layer."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\n{PURGE_LAYER}"
    instructions = _instructions(tmp_path, body)
    with pytest.raises(SystemExit, match="found 2"):
        check_sidecar_purge.purged_packages(instructions)


def test_print_packages_output(tmp_path, monkeypatch, capsys):
    """Print one purged package name per line, in recipe order, and exit 0."""
    path = _write_recipe(tmp_path, f"{RECIPE_PREFIX}{PURGE_LAYER}")
    monkeypatch.setattr(check_sidecar_purge, "CONTAINERFILE", path)
    monkeypatch.setattr(check_sidecar_purge, "REPO_ROOT", tmp_path)
    assert check_sidecar_purge.main(["--print-packages"]) == 0
    assert capsys.readouterr().out.splitlines() == PURGED_PACKAGES


def test_main_reports_offenders_and_returns_one(tmp_path, monkeypatch, capsys):
    """Exit 1 from ``main`` and print ``file:line`` for each post-purge apt instruction."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN apt-get install -y foo\n"
    path = _write_recipe(tmp_path, body)
    monkeypatch.setattr(check_sidecar_purge, "CONTAINERFILE", path)
    monkeypatch.setattr(check_sidecar_purge, "REPO_ROOT", tmp_path)
    assert check_sidecar_purge.main(["--check-ordering"]) == 1
    out = capsys.readouterr().out
    assert "Containerfile.sidecar:" in out
    assert "RUN apt-get install -y foo" in out


def test_main_defaults_to_the_ordering_check(tmp_path, monkeypatch):
    """Run the ordering check when neither flag is passed."""
    path = _write_recipe(tmp_path, f"{RECIPE_PREFIX}{PURGE_LAYER}")
    monkeypatch.setattr(check_sidecar_purge, "CONTAINERFILE", path)
    monkeypatch.setattr(check_sidecar_purge, "REPO_ROOT", tmp_path)
    assert check_sidecar_purge.main([]) == 0


def test_comment_inside_a_continuation_is_dropped_not_treated_as_a_break(tmp_path):
    """Fold an instruction whose continuation is interrupted by a comment line."""
    trailing = (
        "RUN echo one && \\\n# an interrupting comment\n    apt-get install -y foo\n"
    )
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\n{trailing}"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert len(offenders) == 1
    lineno, offender_body = offenders[0]
    assert lineno == body.splitlines().index("RUN echo one && \\") + 1
    assert offender_body == "RUN echo one && apt-get install -y foo"


def test_absolute_path_invocation_is_reported(tmp_path):
    """Report a package manager invoked by absolute path, matching on its basename."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN /usr/bin/apt-get install -y foo\n"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert [b for _, b in offenders] == ["RUN /usr/bin/apt-get install -y foo"]


def test_hyphenated_package_manager_programs_are_reported(tmp_path):
    """Report dpkg-reconfigure, which needs the debconf the purge breaks."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN dpkg-reconfigure tzdata\n"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert [b for _, b in offenders] == ["RUN dpkg-reconfigure tzdata"]


def test_path_segment_named_apt_is_not_an_invocation(tmp_path):
    """Leave a post-purge path containing an apt segment alone; it invokes nothing."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nCOPY foo /etc/apt/apt.conf.d/99local\n"
    assert check_sidecar_purge.check_ordering(_instructions(tmp_path, body)) == []


def test_exec_form_instruction_after_purge_is_reported(tmp_path):
    """Report an exec-form RUN, whose JSON-array punctuation must not mask the program."""
    body = f'{RECIPE_PREFIX}{PURGE_LAYER}\nRUN ["apt-get", "install", "-y", "foo"]\n'
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert [b for _, b in offenders] == ['RUN ["apt-get", "install", "-y", "foo"]']


def test_blank_line_inside_a_continuation_does_not_truncate_the_package_list(tmp_path):
    """Fold a purge layer whose continuation is interrupted by a blank line."""
    interrupted = PURGE_LAYER.replace("        gzip \\\n", "\n        gzip \\\n")
    body = f"{RECIPE_PREFIX}{interrupted}"
    instructions = _instructions(tmp_path, body)
    assert check_sidecar_purge.purged_packages(instructions) == PURGED_PACKAGES


def test_chained_purge_instruction_is_a_hard_failure(tmp_path):
    """Raise ``SystemExit`` when the purge tail carries shell words, not packages."""
    chained = (
        "RUN dpkg --purge --force-remove-essential perl-base && rm -rf /var/cache\n"
    )
    instructions = _instructions(tmp_path, f"{RECIPE_PREFIX}{chained}")
    with pytest.raises(SystemExit, match="non-package tokens"):
        check_sidecar_purge.purged_packages(instructions)


def test_data_path_ending_in_apt_is_not_an_invocation(tmp_path):
    """Leave a post-purge path whose final segment is apt alone; it invokes nothing."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN rm -rf /var/lib/apt\n"
    assert check_sidecar_purge.check_ordering(_instructions(tmp_path, body)) == []


def test_backtick_substitution_after_purge_is_reported(tmp_path):
    """Report a package manager invoked inside backticks, which glue to the name."""
    body = f"{RECIPE_PREFIX}{PURGE_LAYER}\nRUN echo `apt-get -v`\n"
    offenders = check_sidecar_purge.check_ordering(_instructions(tmp_path, body))
    assert [b for _, b in offenders] == ["RUN echo `apt-get -v`"]


def test_absent_containerfile_is_a_hard_failure(tmp_path):
    """Raise ``SystemExit`` rather than reporting a clean parse for a missing file."""
    with pytest.raises(SystemExit, match="No Containerfile at"):
        check_sidecar_purge.parse_instructions(tmp_path / "does-not-exist")
