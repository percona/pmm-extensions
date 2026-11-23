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

"""Reference-integrity test for the files that an AI assistant reads.

The files under ``assistant/`` and the tracked ``AGENTS.md`` files tell an
assistant which files of this repository to read and which commands to run.
No test runs that prose, so a renamed file would leave it pointing nowhere.
This module enforces two contracts on every Markdown file among them:

* **Every path exists.** A string in backticks, or the target of a link, that
  starts with a directory or a file of this repository names something that is
  there. An anchor is cut off, and ``<app>`` and ``<name>`` stand for
  ``checksums``.
* **The words that the files must not hold are absent.** The product is
  "PMM Extensions" and no shorter form, and the files name no team.
"""

import pathlib
import re
import subprocess

import pytest

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_ASSISTANT_ROOT = _REPO_ROOT / "assistant"
_PATH_STARTS = (
    "app/",
    "docs/",
    "sidecar/",
    "tests/",
    "scripts/",
    "assistant/",
    ".github/",
    "Makefile",
    "CONTRIBUTING.md",
    "README.md",
)
_BACKTICKED_RE = re.compile(r"`([^`\n]+)`")
_LINK_TARGET_RE = re.compile(r"\]\(([^)\s]+)\)")
_PLACEHOLDER_RE = re.compile(r"<(?:app|name)>")
_SHORT_NAME_RE = re.compile(r"pmm-?x", re.IGNORECASE)
_TEAM_NAME_RE = re.compile(r"\bServices\b")
_SENTENCE_STARTS = (".", "!", "?", ":", "|", "#", "*", "-", ">", "(")


def _tracked_agents_files() -> list[pathlib.Path]:
    """Return the ``AGENTS.md`` files that Git tracks, in any directory.

    :return: The files. Empty when Git is absent or this is no repository.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--", "AGENTS.md", "**/AGENTS.md"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [_REPO_ROOT / line for line in listing.splitlines() if line]


def _assistant_files() -> list[pathlib.Path]:
    """Return every Markdown file that an assistant reads.

    :return: The Markdown files under ``assistant/``, then the tracked
        ``AGENTS.md`` files.
    """
    return [*sorted(_ASSISTANT_ROOT.rglob("*.md")), *_tracked_agents_files()]


def _named_paths(text: str) -> list[str]:
    """Return the repository paths that a Markdown text names.

    :param text: The text of one file.
    :return: Each path, with its anchor cut off and its placeholder filled in.
    """
    candidates = [
        match.split()[0]
        for match in _BACKTICKED_RE.findall(text) + _LINK_TARGET_RE.findall(text)
        if match.strip()
    ]
    return [
        _PLACEHOLDER_RE.sub("checksums", candidate.split("#")[0]).rstrip(".,;:")
        for candidate in candidates
        if candidate.startswith(_PATH_STARTS)
    ]


def _label(file: pathlib.Path) -> str:
    """Return a file's path from the repository's root, or its full path.

    :param file: The file.
    :return: The path to print in a failure.
    """
    if file.is_relative_to(_REPO_ROOT):
        return str(file.relative_to(_REPO_ROOT))
    return str(file)


def _missing_paths(files: list[pathlib.Path]) -> list[str]:
    """Return a line for each named path that does not exist.

    :param files: The files to read.
    :return: ``<file>: <path>`` for each missing path.
    """
    return [
        f"{_label(file)}: {path}"
        for file in files
        for path in _named_paths(file.read_text(encoding="utf-8"))
        if not (_REPO_ROOT / path).exists()
    ]


def _forbidden_words(text: str) -> list[str]:
    """Return the words of a text that an assistant's file must not hold.

    :param text: The text of one file.
    :return: Each short form of the product's name, and each "Services" that
        does not start a sentence, a heading, a list item or a table cell.
    """
    found = _SHORT_NAME_RE.findall(text)
    for line in text.splitlines():
        for match in _TEAM_NAME_RE.finditer(line):
            before = line[: match.start()].rstrip()
            if before and not before.endswith(_SENTENCE_STARTS):
                found.append(match.group())
    return found


def test_every_named_path_exists() -> None:
    """Fail when an assistant's file names a path that is not in the repository."""
    if not (_ASSISTANT_ROOT / "skills").is_dir():
        pytest.skip("no skill under assistant/skills/ yet")
    files = _assistant_files()
    assert files, "assistant/skills/ exists and no Markdown file was collected"
    assert _missing_paths(files) == []


def test_no_file_holds_a_forbidden_word() -> None:
    """Fail on a short form of the product's name, and on a team's name."""
    found = {
        _label(file): words
        for file in _assistant_files()
        if (words := _forbidden_words(file.read_text(encoding="utf-8")))
    }
    assert found == {}


def test_a_missing_path_is_reported_with_its_file(tmp_path: pathlib.Path) -> None:
    """Name the file and the path, for a backticked string and for a link."""
    scratch = tmp_path / "SKILL.md"
    absent = "app/tasks/models.py"
    scratch.write_text(
        f"Read `{absent}` and [the page](docs/how-to/no-such-page.md#top).\n"
        "Run `make app-check NAME=<app>` and read `docs/how-to/test-an-app.md`.\n"
        "The tests are in `tests/app/extensions/apps/<app>/`.\n",
        encoding="utf-8",
    )
    assert _missing_paths([scratch]) == [
        f"{scratch}: {absent}",
        f"{scratch}: docs/how-to/no-such-page.md",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Use the PMMX skill.", ["PMMX"]),
        ("the pmm-x app", ["pmm-x"]),
        ("A person from Services builds an app.", ["Services"]),
        ("Services that PMM knows are in the form.", []),
        ("| Services | The database services of PMM |", []),
        ("Copy the services from PMM's inventory.", []),
    ],
)
def test_forbidden_words_are_found(text: str, expected: list[str]) -> None:
    """Find a short product name and a team's name, and no ordinary word."""
    assert _forbidden_words(text) == expected
