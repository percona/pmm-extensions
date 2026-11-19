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

"""Keep the type-check policy page's rule table in step with ``pyproject.toml``.

A reader takes a rule's level from the page to learn whether a diagnostic fails
the check. A level changed in ``[tool.ty.rules]`` and not on the page gives the
wrong answer without any failure to show for it.
"""

import pathlib
import re
import tomllib

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_PAGE_PATH = _REPO_ROOT / "docs/reference/type-check-policy.md"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

_BASELINE_KEY = "all"
_ROW_RE = re.compile(
    r"^\| `(?P<rule>[a-z][a-z0-9-]*)` \| `(?P<level>[a-z]+)` \| .+ \|$"
)


def _configured_levels() -> dict[str, str]:
    """Return each rule that ``[tool.ty.rules]`` names, with its level.

    :return: Rule name to level, without the ``all`` baseline.
    """
    with _PYPROJECT_PATH.open("rb") as handle:
        rules: dict[str, str] = tomllib.load(handle)["tool"]["ty"]["rules"]
    return {rule: level for rule, level in rules.items() if rule != _BASELINE_KEY}


def _page_levels() -> dict[str, str]:
    """Return each rule in the page's "Rule severities" table, with its level.

    :return: Rule name to level, one entry per table row.
    """
    text = _PAGE_PATH.read_text(encoding="utf-8")
    section = text.split("## Rule severities\n", 1)[1].split("\n## ", 1)[0]
    matches = (_ROW_RE.match(line) for line in section.splitlines())
    return {match["rule"]: match["level"] for match in matches if match}


def test_page_has_one_row_per_configured_rule_at_its_level() -> None:
    """Test that the page's table and ``[tool.ty.rules]`` name the same rules and levels."""
    assert _page_levels() == _configured_levels()


def test_page_states_the_baseline_level() -> None:
    """Test that the page gives the level of a rule the table does not list."""
    with _PYPROJECT_PATH.open("rb") as handle:
        baseline = tomllib.load(handle)["tool"]["ty"]["rules"][_BASELINE_KEY]

    assert f'`all = "{baseline}"`' in _PAGE_PATH.read_text(encoding="utf-8")
