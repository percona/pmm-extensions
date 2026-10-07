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

"""Keep the diagnostic script authoring guide's taxonomy in step with the code.

Script authors copy member names from the guide's permitted-values table, and a
name the taxonomy does not define is discarded silently, so a stale table files
scripts nowhere without any failure to show for it.
"""

import pathlib
import re

from app.extensions.apps.atw.categories import ATWCategory, ParentCategory

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_GUIDE_PATH = _REPO_ROOT / "docs/development/diagnostic-script-authoring.md"

_ROW_RE = re.compile(
    r"^\| `(?P<name>[A-Z_]+)` \| (?P<label>[^|]+?) \| (?P<parent>[^|]+?) \|$"
)
_COUNT_RE = re.compile(r"(?P<members>\w+)\s+members under (?P<parents>\w+) parents")
_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve"
    " thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
).split()


def _guide_text() -> str:
    """Return the authoring guide's text."""
    return _GUIDE_PATH.read_text(encoding="utf-8")


class TestAuthoringGuideTaxonomy:
    """Verify the guide's permitted-values table matches ``ATWCategory``."""

    def test_table_rows_match_the_taxonomy(self) -> None:
        """Verify each member appears once, in order, with its label and parent."""
        rows = [
            (match["name"], match["label"], match["parent"])
            for line in _guide_text().splitlines()
            if (match := _ROW_RE.match(line))
        ]

        assert rows == [
            (category.name, category.value, category.parent.value)
            for category in ATWCategory
        ]

    def test_member_and_parent_counts_match_the_taxonomy(self) -> None:
        """Verify the prose count of members and parents is current."""
        match = _COUNT_RE.search(" ".join(_guide_text().split()))

        assert match is not None
        assert match["members"] == _NUMBER_WORDS[len(ATWCategory)]
        assert match["parents"] == _NUMBER_WORDS[len(ParentCategory)]
