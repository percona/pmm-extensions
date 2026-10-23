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

"""Tests for the ``scripts/check_pmm_fb_pins.py`` CLI."""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.scripts import load_script, write_file

check_pmm_fb_pins = load_script("check_pmm_fb_pins")

COMPOSE = check_pmm_fb_pins.COMPOSE_PATH

WITNESS_NAME = "NOMAD_VERSION_FB_TAG"
"""Build arg ``--print`` resolves when CI reads the committed witness."""

DRIFTED_TAG = "PR-0000-drifted"
"""Tag written into a tampered compose copy, sharing no prefix with a real one."""

WITNESS_DEFAULT = re.compile(r"(?<=NOMAD_VERSION_FB_TAG:-)[^}]*")
SERVER_TAG_DEFAULT = re.compile(r"(?<=pmm-server-fb:\$\{PMM_FB_TAG:-)[^}]*")

VALUELESS_PIN = """\
services:
  pmm-server:
    image: docker.io/perconalab/pmm-server-fb:${PMM_FB_TAG:-T}
  extensions-mysql:
    build:
      args:
        PMM_FB_TAG:
        NOMAD_VERSION: ${NOMAD_VERSION:-2.0.5}
        NOMAD_VERSION_FB_TAG: ${NOMAD_VERSION_FB_TAG:-T}
"""
"""A compose file of the right shape whose tag key carries no value.

YAML resolves that to ``None``, which the reader has to reject by name rather
than by letting the regex raise on a non-string.
"""

VALUELESS_ARGS = """\
services:
  pmm-server:
    image: docker.io/perconalab/pmm-server-fb:${PMM_FB_TAG:-T}
  extensions-mysql:
    build:
      args:
"""
"""The build-args key itself carries no mapping.

The sibling above withholds one arg's value; here the whole node is ``None``,
which reaches the reader as a well-shaped file whose args cannot be searched by
name at all.
"""

DECORATED_PIN = """\
services:
  pmm-server:
    image: docker.io/perconalab/pmm-server-fb:${PMM_FB_TAG:-T}
  extensions-mysql:
    build:
      args:
        PMM_FB_TAG: prefix-${PMM_FB_TAG:-T}
        NOMAD_VERSION: ${NOMAD_VERSION:-2.0.5}
        NOMAD_VERSION_FB_TAG: ${NOMAD_VERSION_FB_TAG:-T}
"""
"""A slot whose expansion is real but is not the whole value.

Compose builds with ``prefix-T`` while a reader searching for the expansion
anywhere in the string reports ``T``, so the tags appear to agree on a value the
build never uses.
"""


REWIRED_PIN = """\
services:
  pmm-server:
    image: docker.io/perconalab/pmm-server-fb:${PMM_FB_TAG:-T}
  extensions-mysql:
    build:
      args:
        PMM_FB_TAG: ${OTHER_TAG:-T}
        NOMAD_VERSION: ${NOMAD_VERSION:-2.0.5}
        NOMAD_VERSION_FB_TAG: ${NOMAD_VERSION_FB_TAG:-T}
"""
"""Every committed default agrees, but one slot reads a different variable.

Exporting ``PMM_FB_TAG`` would then move the server and the witness while this
slot stayed behind: the mismatch the check exists to prevent, reached without
changing a single literal.
"""


EMPTY_PIN = """\
services:
  pmm-server:
    image: docker.io/perconalab/pmm-server-fb:${PMM_FB_TAG:-}
  extensions-mysql:
    build:
      args:
        PMM_FB_TAG: ${PMM_FB_TAG:-}
        NOMAD_VERSION: ${NOMAD_VERSION:-2.0.5}
        NOMAD_VERSION_FB_TAG: ${NOMAD_VERSION_FB_TAG:-}
"""
"""Every tag slot is well-formed and defaults to nothing.

Three empty strings agree with each other, so equality alone calls this file
pinned while a fresh clone builds a tagless image reference. It is the same
vacuous comparison the witness exists to remove, one layer up.
"""


def drift(pattern: re.Pattern[str], text: str, what: str) -> tuple[str, str]:
    """Rewrite one tag default in ``text`` to :data:`DRIFTED_TAG`.

    :param pattern: A zero-width-prefixed pattern matching just the tag.
    :param text: The compose file's text.
    :param what: What the pattern looks for, for the failure message.
    :return: The tampered text and the tag it replaced.
    :raises AssertionError: When the pattern no longer matches, so a restructured
        compose file fails loudly rather than tampering with nothing.
    """
    match = pattern.search(text)
    assert match is not None, f"compose.yaml no longer carries {what}"
    return f"{text[: match.start()]}{DRIFTED_TAG}{text[match.end() :]}", match.group(0)


@pytest.fixture
def tampered_compose(
    tmp_path: Path,
) -> Callable[[re.Pattern[str], str], tuple[Path, str]]:
    """Return a writer that drops a tampered copy of the compose file in ``tmp_path``.

    :param tmp_path: The per-test temporary directory.
    :return: A callable taking a pattern and a description, returning the copy's
        path and the tag it replaced. It propagates :func:`drift`'s
        ``AssertionError`` when the pattern no longer matches.
    """

    def write(pattern: re.Pattern[str], what: str) -> tuple[Path, str]:
        tampered, original = drift(pattern, COMPOSE.read_text(encoding="utf-8"), what)
        return write_file(tmp_path, "compose.yaml", tampered), original

    return write


def test_committed_pins_agree(capsys: pytest.CaptureFixture[str]) -> None:
    """Keep the committed state buildable: every arm64 build reads these defaults."""
    code = check_pmm_fb_pins.main([])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err


def test_a_witness_left_behind_is_caught(
    tampered_compose: Callable[[re.Pattern[str], str], tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a compose file whose witness no longer names the pinned feature build."""
    path, original = tampered_compose(WITNESS_DEFAULT, "a NOMAD_VERSION_FB_TAG default")

    assert check_pmm_fb_pins.main([str(path)]) != 0
    captured = capsys.readouterr()
    assert original in captured.err
    assert DRIFTED_TAG in captured.err


def test_a_drifting_server_tag_is_caught(
    tampered_compose: Callable[[re.Pattern[str], str], tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject the pre-existing drift the two spellings of the tag always allowed."""
    path, original = tampered_compose(SERVER_TAG_DEFAULT, "a pmm-server-fb image tag")

    assert check_pmm_fb_pins.main([str(path)]) != 0
    captured = capsys.readouterr()
    assert original in captured.err
    assert DRIFTED_TAG in captured.err


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("services: {}\n", id="no-extensions-mysql-service"),
        pytest.param("just a string\n", id="not-a-mapping"),
        pytest.param(VALUELESS_PIN, id="a-pin-with-no-value"),
        pytest.param(VALUELESS_ARGS, id="build-args-with-no-mapping"),
        pytest.param(REWIRED_PIN, id="a-pin-on-the-wrong-variable"),
        pytest.param(DECORATED_PIN, id="a-pin-with-a-literal-beside-it"),
    ],
)
def test_an_unreadable_compose_file_is_refused(tmp_path: Path, content: str) -> None:
    """Refuse a file this check cannot read, rather than resolving it to nothing.

    A reader that returned empty pins here would compare "" against "", call the
    tags agreed, and report success on a file it never understood.
    """
    path = write_file(tmp_path, "compose.yaml", content)

    with pytest.raises(SystemExit) as exc_info:
        check_pmm_fb_pins.main([str(path)])

    assert "ERROR" in str(exc_info.value)


def test_pins_that_name_no_build_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse tags that agree only because every one of them defaults to nothing.

    Equality is satisfied by three empty strings, so the check has to ask what
    the tags resolve to as well as whether they match.
    """
    path = write_file(tmp_path, "compose.yaml", EMPTY_PIN)

    assert check_pmm_fb_pins.main([str(path)]) == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.out + captured.err


def test_print_emits_the_committed_witness(capsys: pytest.CaptureFixture[str]) -> None:
    """Check that the value the CI gate reads is the one the agreement check validated."""
    match = WITNESS_DEFAULT.search(COMPOSE.read_text(encoding="utf-8"))
    assert match is not None, (
        "compose.yaml no longer carries a NOMAD_VERSION_FB_TAG default"
    )

    assert check_pmm_fb_pins.main(["--print", WITNESS_NAME]) == 0
    assert capsys.readouterr().out.strip() == match.group(0)
