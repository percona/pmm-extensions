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
"""Cover who may change what the side-car image ships.

Every process in the image runs as ``extensions``. The owner of a directory can
rename and delete any entry in it, whoever owns the entry, so the directories
holding the shipped tree must not belong to that user. And the bundle arrives as
a ``git archive`` whose entries are group-writable, which a side-car started
with the supplementary group 0 could write through, so the recipe has to take
that write away.
"""

import re
import stat

import pytest

from tests.sidecar.conftest import CONTAINERFILE

RUNTIME_UID = "1001"
ROOT_UID = "0"

ARCHIVE_DIRECTORY_MODE = 0o775
ARCHIVE_FILE_MODE = 0o664
"""The modes ``git archive`` gives its entries under its default ``tar.umask`` of ``0002``."""

INSTALL_DIRECTORY = re.compile(
    r"\binstall -d -o (?P<owner>\S+) -g \S+ -m (?P<mode>\d+) (?P<target>\S+)"
)
RECURSIVE_CHMOD = re.compile(r"^RUN chmod -R (?P<mode>\S+) \$APP_HOME$")
CLAUSE = re.compile(r"^(?P<who>[ugoa]*)(?P<op>[+-=])(?P<perms>[rwxX]*)$")

WHO_BITS = {"u": 0o700, "g": 0o070, "o": 0o007}
PERM_BITS = {"r": 0o444, "w": 0o222, "x": 0o111}


def recipe_lines() -> list[str]:
    """Return the recipe with every backslash continuation joined.

    :return: One string per logical instruction line.
    """
    return CONTAINERFILE.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()


def installed_directories() -> dict[str, tuple[str, str]]:
    """Return each directory the recipe creates with ``install -d``.

    :return: The owner and the mode of each, keyed by its target.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in recipe_lines():
        for match in INSTALL_DIRECTORY.finditer(line):
            found[match["target"]] = (match["owner"], match["mode"])
    return found


def apply_symbolic_mode(mode: int, symbolic: str, *, is_directory: bool) -> int:
    """Return ``mode`` after ``chmod`` applies a symbolic mode to it.

    Covers the clause forms a recipe line uses: who letters, one operator, and
    ``rwxX`` permissions, where ``X`` applies to a directory, or to a file some
    class can already execute.

    :param mode: The permission bits before the change.
    :param symbolic: The comma-separated clauses, as given to ``chmod``.
    :param is_directory: Whether the entry is a directory.
    :return: The permission bits after the change.
    """
    for clause in symbolic.split(","):
        match = CLAUSE.match(clause)
        assert match, f"unsupported chmod clause {clause!r}"
        who = sum(
            WHO_BITS[letter] for letter in (match["who"] or "a").replace("a", "ugo")
        )
        perms = 0
        for letter in match["perms"]:
            if letter == "X":
                if is_directory or mode & 0o111:
                    perms |= PERM_BITS["x"]
            else:
                perms |= PERM_BITS[letter]
        bits = who & perms
        if match["op"] == "+":
            mode |= bits
        elif match["op"] == "-":
            mode &= ~bits
        else:
            mode = (mode & ~who) | bits
    return mode


def test_the_home_and_app_directories_belong_to_root():
    """Keep the runtime user from renaming what the two directories hold.

    ``$HOME`` holds ``app`` itself, and ``$APP_HOME`` the start scripts, the
    settings profile and the ``app/`` package. Either one owned by
    ``extensions`` lets that user move a shipped entry aside and put its own in
    its place.
    """
    directories = installed_directories()

    assert directories.get("$HOME") == (ROOT_UID, "0750")
    assert directories.get("$APP_HOME") == (ROOT_UID, "0750")


def test_useradd_creates_no_home_of_its_own():
    """Leave ``$HOME`` to the ``install -d`` that makes it root's.

    ``useradd --create-home`` creates the directory owned by the new user.
    """
    useradd = [line for line in recipe_lines() if "useradd" in line]

    assert len(useradd) == 1
    assert "--create-home" not in useradd[0]
    assert "--no-create-home" in useradd[0]


def test_the_state_directory_stays_the_runtime_users():
    """Keep the one place the side-car writes its minted credentials writable."""
    assert installed_directories().get("$HOME/state") == (RUNTIME_UID, "0700")


def test_the_shipped_tree_loses_the_archives_group_write():
    """Take group and other write off the bundle's entries after it is added.

    ``ADD`` applies neither ``--chown`` nor ``--chmod`` to the entries of an
    archive it extracts, so they keep the ``git archive`` modes. The last
    recursive ``chmod`` over ``$APP_HOME`` runs after the bundle is added, and
    applying it to those modes must leave nothing a group or other can write,
    while ``extensions`` can still enter every directory.
    """
    lines = recipe_lines()
    bundle = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("ADD") and "bundle.tgz" in line
    )
    chmods = [
        (i, m["mode"])
        for i, line in enumerate(lines)
        if (m := RECURSIVE_CHMOD.match(line))
    ]
    assert chmods
    position, symbolic = chmods[-1]
    assert position > bundle

    directory = apply_symbolic_mode(ARCHIVE_DIRECTORY_MODE, symbolic, is_directory=True)
    file = apply_symbolic_mode(ARCHIVE_FILE_MODE, symbolic, is_directory=False)

    assert directory & (stat.S_IWGRP | stat.S_IWOTH) == 0
    assert file & (stat.S_IWGRP | stat.S_IWOTH) == 0
    assert directory & stat.S_IXGRP


@pytest.mark.parametrize(
    ("before", "symbolic", "entry", "after"),
    [
        (0o640, "u+X,g+X", "directory", 0o750),
        (0o640, "u+X,g+X", "file", 0o640),
        (0o775, "go-w", "directory", 0o755),
        (0o664, "a=r", "file", 0o444),
    ],
)
def test_the_symbolic_mode_helper_follows_chmod(
    before: int, symbolic: str, entry: str, after: int
):
    """Pin the helper the case above relies on against modes ``chmod`` gives."""
    result = apply_symbolic_mode(before, symbolic, is_directory=entry == "directory")

    assert result == after
