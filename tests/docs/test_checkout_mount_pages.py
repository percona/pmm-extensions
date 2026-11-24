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

"""Hold every page that mounts a checkout to the section that names the image.

One section of the local PMM page says which image to set when the default
image cannot run a checkout's code. A page that gives a Compose command with
the checkout mount must link that section before the command, so that a reader
whose container does not start finds the image before the first start.
"""

import pathlib

from tests.docs import DOCS_ROOT

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_MOUNT_FILE = "compose.checkout.yaml"
_SECTION_PAGE = "run-in-local-pmm.md"
_SECTION_LINK = f"{_SECTION_PAGE}#run-the-code-in-your-checkout"
_SECTION_HEADING = "## Run the code in your checkout"
_FENCE = "```"
_FENCES = (_FENCE, "~~~")


def _first_mount_command(text: str) -> int | None:
    """Find the first line of a fenced block that names the mount's Compose file.

    :param text: The text of a page.
    :return: The offset of that line, or ``None`` when no fenced block names
        the file.
    """
    offset = 0
    fenced = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(_FENCES):
            fenced = not fenced
        elif fenced and _MOUNT_FILE in line:
            return offset
        offset += len(line)
    return None


def _links_the_section_first(text: str) -> bool:
    """Say whether a page links the section before its first mount command.

    :param text: The text of a page.
    :return: ``True`` when the page has no mount command, or links the section
        above the first one.
    """
    command = _first_mount_command(text)
    return command is None or _SECTION_LINK in text[:command]


def _mount_pages() -> list[pathlib.Path]:
    """List the pages that give a mount command, without the section's own page.

    :return: The pages, sorted by path.
    """
    return sorted(
        page
        for page in DOCS_ROOT.rglob("*.md")
        if page.name != _SECTION_PAGE
        and _first_mount_command(page.read_text(encoding="utf-8")) is not None
    )


def test_every_page_with_a_mount_command_links_the_section_first() -> None:
    """Fail when a page gives the mount command before it links the section."""
    pages = _mount_pages()
    late = [
        str(page.relative_to(_REPO_ROOT))
        for page in pages
        if not _links_the_section_first(page.read_text(encoding="utf-8"))
    ]
    assert pages
    assert late == []


def test_the_section_that_the_pages_link_exists() -> None:
    """Fail when the section's heading is gone, since every link would then lead nowhere."""
    page = DOCS_ROOT / "how-to" / _SECTION_PAGE
    assert _SECTION_HEADING in page.read_text(encoding="utf-8").splitlines()


def test_a_link_below_the_command_does_not_count() -> None:
    """Refuse a page whose link to the section comes after the command."""
    command = (
        f"{_FENCE}bash\ndocker compose -f sidecar/dev/{_MOUNT_FILE} up\n{_FENCE}\n"
    )
    link = f"See [the section]({_SECTION_LINK}).\n"
    assert _links_the_section_first(link + command)
    assert not _links_the_section_first(command + link)


def test_the_file_named_in_prose_is_no_command() -> None:
    """Take a page that names the file in a sentence only as one with no command."""
    assert _first_mount_command(f"Leave out `{_MOUNT_FILE}` here.\n") is None
