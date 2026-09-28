#!/usr/bin/env python3
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

"""Manage changelog fragments for the PMM Extensions project.

Each PR with a user-facing change drops one or more small fragment files under
``changelog.d/`` instead of appending entries to ``CHANGELOG.md`` directly. This
avoids per-PR merge conflicts on the ``[Unreleased]`` section.

Subcommands:

- ``add``: create a new fragment file under ``changelog.d/``.
- ``check``: validate all fragments (filename, section, content).
- ``list``: render a CHANGELOG-style preview of all fragments.
- ``assemble``: move fragments for a Jira fix version into a new
  ``[vX.Y.Z]`` section of ``CHANGELOG.md`` and delete the consumed files.

Fragment filename: ``<TICKET>.<section>.md`` where ``section`` is one of
``added``, ``changed``, ``breaking``, ``config``, ``fixed``, ``security``.
Fragment content: one or more single-line entries; the assembler prepends
``- <TICKET>: `` to each line at render time.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SECTION_MAP: dict[str, str] = {
    "added": "Added",
    "changed": "Changed",
    "breaking": "Breaking Changes",
    "config": "Configuration Changes",
    "fixed": "Fixed",
    "security": "Security",
}
SECTION_ORDER: list[str] = [
    "Added",
    "Changed",
    "Breaking Changes",
    "Configuration Changes",
    "Fixed",
    "Security",
]
VALID_SECTIONS: frozenset[str] = frozenset(SECTION_MAP)
TICKET_PROJECTS: tuple[str, ...] = ("SEP", "PMM")
PROJECT_PATTERN: str = "|".join(re.escape(project) for project in TICKET_PROJECTS)
TICKET_FORMATS: str = " or ".join(f"``{project}-<n>``" for project in TICKET_PROJECTS)
FRAGMENT_FORMATS: str = " or ".join(
    f"``{project}-<n>.<section>.md``" for project in TICKET_PROJECTS
)
FRAGMENT_RE: re.Pattern[str] = re.compile(
    rf"^((?:{PROJECT_PATTERN})-\d+)\.(added|changed|breaking|config|fixed|security)\.md$",
)
TICKET_RE: re.Pattern[str] = re.compile(rf"^({PROJECT_PATTERN})-(\d+)$")
UNRELEASED_COMPARE_RE: re.Pattern[str] = re.compile(
    r"^\[Unreleased\]: (?P<url>https://github\.com/percona/pmm-extensions/compare/"
    r"v(?P<previous>[\w.\-]+)\.\.\.HEAD)$",
)
VERSION_FOOTER_LINE_RE: re.Pattern[str] = re.compile(
    r"^\[v(?P<version>[\w.\-]+)\]: ",
)
#: Tracked names in ``changelog.d/`` that are documentation or scaffolding
#: rather than fragments.
RESERVED_FILENAMES: frozenset[str] = frozenset({"README.md", ".gitkeep"})
CHANGELOG_D: Path = Path("changelog.d")
CHANGELOG_MD: Path = Path("CHANGELOG.md")
REPO_COMPARE_URL: str = "https://github.com/percona/pmm-extensions/compare"


class FragmentError(Exception):
    """Indicate an invalid changelog fragment or CHANGELOG.md state."""


def _ticket_sort_key(ticket: str) -> tuple[str, int]:
    """Return the project and numeric sort key for a ticket.

    :param ticket: The ticket key.
    :return: The project prefix and integer portion of the ticket key.
    :raises FragmentError: If the ticket key is invalid.
    """
    match = TICKET_RE.match(ticket)
    if match is None:
        raise FragmentError(f"invalid ticket key: {ticket}")
    return match.group(1), int(match.group(2))


def load_fragments(
    changelog_d: Path = CHANGELOG_D,
) -> dict[str, list[tuple[str, list[str], Path]]]:
    """Load and validate all fragments under ``changelog_d``.

    :data:`RESERVED_FILENAMES` and anything git ignores
    (:func:`_git_ignored_names`) are skipped; every other file is held to
    :data:`FRAGMENT_RE`, so a typo like ``SEP1892.added.md`` is still reported.

    :param changelog_d: The fragments directory.
    :return: A mapping of display section name to ``(ticket, lines, path)``
        triples sorted by project prefix and then numeric ticket ID.
    :raises FragmentError: If any fragment has an invalid filename, an unknown
        section, or empty/malformed content.
    """
    grouped = defaultdict(list)
    errors = []
    if not changelog_d.exists():
        return {}
    ignored = _git_ignored_names(str(changelog_d).rstrip("/"))
    for path in sorted(changelog_d.iterdir()):
        if path.name in RESERVED_FILENAMES or path.name in ignored:
            continue
        if not path.is_file():
            continue
        match = FRAGMENT_RE.match(path.name)
        if match is None:
            errors.append(
                f"{path.name}: invalid filename (expected "
                f"{FRAGMENT_FORMATS} where section is one of "
                f"{', '.join(sorted(VALID_SECTIONS))})",
            )
            continue
        ticket = match.group(1)
        section_display = SECTION_MAP[match.group(2)]
        raw = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            errors.append(f"{path.name}: file is empty")
            continue
        bad_prefix = next((line for line in lines if line.startswith("- ")), None)
        if bad_prefix is not None:
            errors.append(
                f"{path.name}: content must not start with ``- `` (the bullet "
                "and ``<TICKET>:`` prefix are added at assembly time)",
            )
            continue
        grouped[section_display].append((ticket, lines, path))

    if errors:
        raise FragmentError("\n".join(errors))

    for section_display in grouped:
        grouped[section_display].sort(key=lambda item: _ticket_sort_key(item[0]))

    return dict(grouped)


def render_section_body(
    grouped: dict[str, list[tuple[str, list[str], Path]]],
) -> str:
    """Render the grouped fragments as a CHANGELOG ``[vX.Y.Z]``-style block body.

    :param grouped: The output of :func:`load_fragments`, optionally filtered.
    :type grouped: dict[str, list[tuple[str, list[str], Path]]]
    :return: The rendered body with ``### Section`` subheaders and
        ``- <TICKET>: ...`` bullets, separated by blank lines, with a trailing
        newline.
    :rtype: str
    """
    blocks = []
    for section_display in SECTION_ORDER:
        entries = grouped.get(section_display)
        if not entries:
            continue
        lines = [f"### {section_display}", ""]
        lines.extend(
            f"- {ticket}: {line}"
            for ticket, entry_lines, _ in entries
            for line in entry_lines
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _parse_tickets(tickets_csv: str) -> set[str]:
    """Parse a comma-separated list of ticket keys into a set.

    :param tickets_csv: The raw CSV string from ``--tickets``.
    :type tickets_csv: str
    :return: The set of trimmed, non-empty ticket keys.
    :rtype: set[str]
    """
    return {t.strip() for t in tickets_csv.split(",") if t.strip()}


def _filter_by_tickets(
    grouped: dict[str, list[tuple[str, list[str], Path]]],
    tickets: set[str],
) -> dict[str, list[tuple[str, list[str], Path]]]:
    """Return a new grouping limited to entries whose ticket is in ``tickets``.

    :param grouped: The full grouped fragments.
    :type grouped: dict[str, list[tuple[str, list[str], Path]]]
    :param tickets: The set of ticket keys to keep.
    :type tickets: set[str]
    :return: The filtered grouping.
    :rtype: dict[str, list[tuple[str, list[str], Path]]]
    """
    filtered = {}
    for section_display, entries in grouped.items():
        kept = [entry for entry in entries if entry[0] in tickets]
        if kept:
            filtered[section_display] = kept
    return filtered


def _splice_version_section(
    changelog_text: str,
    version: str,
    date: str,
    body: str,
) -> str:
    """Insert a new ``## [vX.Y.Z] - YYYY-MM-DD`` block after ``## [Unreleased]``.

    :param changelog_text: The current ``CHANGELOG.md`` contents.
    :type changelog_text: str
    :param version: The new version (without the ``v`` prefix).
    :type version: str
    :param date: The release date in ``YYYY-MM-DD``.
    :type date: str
    :param body: The rendered section body from :func:`render_section_body`.
    :type body: str
    :return: The updated ``CHANGELOG.md`` contents.
    :rtype: str
    :raises FragmentError: If the ``[Unreleased]`` heading or a following
        ``[vX.Y.Z]`` heading cannot be located.
    """
    lines = changelog_text.splitlines()
    try:
        unreleased_idx = lines.index("## [Unreleased]")
    except ValueError as exc:
        raise FragmentError(
            "CHANGELOG.md: missing ``## [Unreleased]`` heading",
        ) from exc
    next_version_idx = None
    for idx in range(unreleased_idx + 1, len(lines)):
        if lines[idx].startswith("## [v"):
            next_version_idx = idx
            break
    if next_version_idx is None:
        raise FragmentError(
            "CHANGELOG.md: no existing ``## [v...]`` section found after [Unreleased]",
        )
    new_block = [f"## [v{version}] - {date}", ""]
    new_block.extend(body.rstrip().splitlines())
    new_block.append("")
    new_lines = lines[:next_version_idx] + new_block + lines[next_version_idx:]
    # preserve the file's original trailing newline, if any
    suffix = "\n" if changelog_text.endswith("\n") else ""
    return "\n".join(new_lines) + suffix


def _infer_previous_version(lines: list[str], version: str) -> str | None:
    """Return the most recent prior version from ``## [vA.B.C]`` headings.

    :param lines: The ``CHANGELOG.md`` contents split into lines.
    :type lines: list[str]
    :param version: The version being added (excluded from the search).
    :type version: str
    :return: The previous version string (no ``v`` prefix), or ``None`` if
        no usable section heading exists.
    :rtype: str | None
    """
    new_section_heading = f"## [v{version}]"
    _prefix = "## ["
    for line in lines:
        if not line.startswith("## [v") or line.startswith(new_section_heading):
            continue
        # Strip ``## [`` prefix and everything from ``]`` onwards.
        bracket_end = line.find("]")
        if bracket_end <= len(_prefix):
            continue
        candidate = line[len(_prefix) : bracket_end]  # e.g. ``v0.12.1``
        if candidate.startswith("v"):
            return candidate[1:]
    return None


def _insert_synthesized_footer(
    lines: list[str],
    new_unreleased: str,
    new_version_link: str,
) -> None:
    """Insert synthesized footer compare links in-place, newest-first.

    Inserts before the first existing ``[vA.B.C]:`` compare-link line so the
    synthesized lines stay in descending-version order, matching the rest of
    the file. Falls back to appending at the end of ``lines`` (after trimming
    trailing blanks) when no existing footer line is found.

    :param lines: The ``CHANGELOG.md`` contents split into lines; mutated.
    :type lines: list[str]
    :param new_unreleased: The new ``[Unreleased]:`` line to insert.
    :type new_unreleased: str
    :param new_version_link: The new ``[vX.Y.Z]:`` line to insert below it.
    :type new_version_link: str
    """
    insert_idx = None
    for footer_idx, footer_line in enumerate(lines):
        if footer_line.startswith("[v") and "]: " in footer_line:
            insert_idx = footer_idx
            break
    if insert_idx is None:
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines.append(new_unreleased)
        lines.append(new_version_link)
    else:
        lines.insert(insert_idx, new_unreleased)
        lines.insert(insert_idx + 1, new_version_link)


def _update_compare_links(changelog_text: str, version: str) -> str:
    """Rewrite the ``[Unreleased]`` compare link and insert a new ``[vX.Y.Z]`` link.

    When the ``[Unreleased]:`` footer is absent (the state immediately after
    the v0.12.x transition cleanup that removed the broken compare link),
    synthesize both lines from scratch by inferring the previous-version tag
    from the most recent ``## [vA.B.C]`` heading.

    :param changelog_text: The current ``CHANGELOG.md`` contents.
    :type changelog_text: str
    :param version: The new version (without the ``v`` prefix).
    :type version: str
    :return: The updated ``CHANGELOG.md`` contents.
    :rtype: str
    :raises FragmentError: If neither an ``[Unreleased]:`` footer nor any
        ``## [v...]`` section heading can be found.
    """
    lines = changelog_text.splitlines()
    suffix = "\n" if changelog_text.endswith("\n") else ""
    for idx, line in enumerate(lines):
        match = UNRELEASED_COMPARE_RE.match(line)
        if match is None:
            continue
        previous = match.group("previous")
        lines[idx] = f"[Unreleased]: {REPO_COMPARE_URL}/v{version}...HEAD"
        new_link = f"[v{version}]: {REPO_COMPARE_URL}/v{previous}...v{version}"
        lines.insert(idx + 1, new_link)
        return "\n".join(lines) + suffix

    # Footer absent — synthesize. Find the previous version from the most
    # recent ``## [vA.B.C]`` heading (excluding the version we're about to add,
    # which by this point in cmd_assemble already lives near the top of the file).
    previous_version = _infer_previous_version(lines, version)
    if previous_version is None:
        raise FragmentError(
            "CHANGELOG.md: missing ``[Unreleased]`` compare link and no "
            "``## [vA.B.C]`` section heading to infer previous version from",
        )
    new_unreleased = f"[Unreleased]: {REPO_COMPARE_URL}/v{version}...HEAD"
    new_version_link = (
        f"[v{version}]: {REPO_COMPARE_URL}/v{previous_version}...v{version}"
    )
    _insert_synthesized_footer(lines, new_unreleased, new_version_link)
    return "\n".join(lines) + suffix


def _git_show_ref(ref: str, path: str) -> str:
    """Return ``git show <ref>:<path>`` as a string.

    Used by :func:`cmd_resolve_backmerge` to read the ours/theirs sides of
    a back-merge directly from refs (HEAD for ours, MERGE_HEAD for
    theirs), so the script works whether or not the path was conflicted.
    Monkeypatched in tests.

    :param ref: A git ref name (e.g. ``HEAD``, ``MERGE_HEAD``).
    :type ref: str
    :param path: The repository-relative path.
    :type path: str
    :return: The file contents at that ref.
    :rtype: str
    """
    git_exe = shutil.which("git") or "git"
    result = subprocess.run(
        [git_exe, "show", f"{ref}:{path}"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


def _git_ls_tree(ref: str, path: str) -> set[str]:
    """Return the set of file basenames under ``path`` at ``ref``.

    Used to compute the set difference between main's and release branch's
    ``changelog.d/`` contents during a back-merge — files present in main's
    tree but absent in the release branch's tree are the ones the release
    consumed via ``cmd_assemble``.

    :param ref: A git ref name (e.g. ``HEAD``, ``MERGE_HEAD``).
    :type ref: str
    :param path: The repository-relative directory path (no trailing slash).
    :type path: str
    :return: The set of basenames; empty if the directory doesn't exist at
        the ref.
    :rtype: set[str]
    """
    git_exe = shutil.which("git") or "git"
    result = subprocess.run(
        [git_exe, "ls-tree", "--name-only", ref, f"{path}/"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return set()
    names = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        # ls-tree returns full paths; strip the directory prefix.
        names.add(name.rsplit("/", 1)[-1])
    return names


def _git_ignored_names(path: str) -> frozenset[str]:
    """Return the basenames git ignores under ``path``.

    ``changelog.d/`` accumulates untracked files a contributor's own ignore
    rules cover: editor swap files, OS metadata, personal notes. None of them is
    a fragment. Skipping whatever git ignores leaves those alone while
    :func:`load_fragments` still reports a genuinely misnamed fragment, which an
    allowed-pattern filter could not do. Guarded like :func:`_git_ls_tree`, plus
    the missing-executable case, so a checkout without git falls back to
    considering every file.

    :param path: The repository-relative directory path (no trailing slash).
    :return: The ignored basenames; empty when git cannot answer.
    """
    git_exe = shutil.which("git") or "git"
    try:
        result = subprocess.run(
            [
                git_exe,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                f"{path}/",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        stripped.rsplit("/", 1)[-1]
        for line in result.stdout.splitlines()
        if (stripped := line.strip())
    )


def _git_merge_base(ref_a: str, ref_b: str) -> str:
    """Return the merge-base commit SHA of two refs.

    Used by :func:`_prune_consumed_fragments` to find the scope-lock commit
    — the common ancestor of main (``HEAD``) and the release branch
    (``MERGE_HEAD``). The merge-base's ``changelog.d/`` listing is the
    correct baseline for "what existed when the release branch was cut".
    Monkeypatched in tests.

    :param ref_a: First ref name.
    :type ref_a: str
    :param ref_b: Second ref name.
    :type ref_b: str
    :return: The merge-base commit SHA.
    :rtype: str
    :raises subprocess.CalledProcessError: If ``git merge-base`` fails (no
        common ancestor, etc.).
    """
    git_exe = shutil.which("git") or "git"
    result = subprocess.run(
        [git_exe, "merge-base", ref_a, ref_b],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _extract_version_section(
    changelog_text: str,
    version: str,
) -> list[str]:
    """Return the lines of the ``## [vX.Y.Z]`` section, heading inclusive.

    Trailing blank lines are trimmed. The caller is responsible for
    re-inserting one blank line as a separator when splicing into another
    document.

    :param changelog_text: Source CHANGELOG body.
    :type changelog_text: str
    :param version: Version to extract (no ``v`` prefix).
    :type version: str
    :return: The section's lines.
    :rtype: list[str]
    :raises FragmentError: If the section heading is not found.
    """
    lines = changelog_text.splitlines()
    target_heading_prefix = f"## [v{version}]"
    start = None
    for idx, line in enumerate(lines):
        if line.startswith(target_heading_prefix):
            start = idx
            break
    if start is None:
        raise FragmentError(
            f"release-side CHANGELOG.md: missing ``## [v{version}]`` section",
        )
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if line.startswith(("## [", "[Unreleased]:")) or VERSION_FOOTER_LINE_RE.match(
            line,
        ):
            end = idx
            break
    section = lines[start:end]
    while section and section[-1].strip() == "":
        section.pop()
    return section


def _split_body_and_footer(changelog_text: str) -> tuple[list[str], list[str]]:
    """Split a CHANGELOG into the body lines and the footer-link lines.

    Footer = the contiguous trailing block of ``[Unreleased]: ...`` /
    ``[vA.B.C]: ...`` lines (possibly preceded by blank lines that are
    treated as part of the body).

    :param changelog_text: The CHANGELOG body.
    :type changelog_text: str
    :return: ``(body_lines, footer_lines)``.
    :rtype: tuple[list[str], list[str]]
    """
    lines = changelog_text.splitlines()
    footer_start = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if line == "":
            continue
        if line.startswith("[Unreleased]:") or VERSION_FOOTER_LINE_RE.match(line):
            footer_start = idx
            continue
        break
    return lines[:footer_start], lines[footer_start:]


def _splice_release_into_body(
    ours_body: list[str],
    section_lines: list[str],
) -> list[str] | None:
    """Splice the release section into the ours-side body lines.

    Locates ``## [Unreleased]`` then the first ``## [v...]`` after it and
    inserts ``section_lines`` (plus a trailing blank line separator) between
    them.

    :param ours_body: Body lines from the ours-side CHANGELOG (no footer).
    :type ours_body: list[str]
    :param section_lines: The release ``## [vX.Y.Z]`` section lines to splice in.
    :type section_lines: list[str]
    :return: The merged body, or ``None`` if the required headings are absent.
    :rtype: list[str] | None
    """
    try:
        unreleased_idx = ours_body.index("## [Unreleased]")
    except ValueError:
        return None
    next_section_idx = None
    for idx in range(unreleased_idx + 1, len(ours_body)):
        if ours_body[idx].startswith("## [v"):
            next_section_idx = idx
            break
    if next_section_idx is None:
        return None
    new_block = [*section_lines, ""]
    return [*ours_body[:next_section_idx], *new_block, *ours_body[next_section_idx:]]


def _build_rebuilt_footer(
    version: str,
    ours_footer: list[str],
    theirs_footer: list[str],
) -> list[str] | None:
    """Assemble the new footer: [Unreleased] link, new [vX.Y.Z] link, old links.

    :param version: The release version (no ``v`` prefix).
    :type version: str
    :param ours_footer: Footer lines from the ours-side CHANGELOG.
    :type ours_footer: list[str]
    :param theirs_footer: Footer lines from the theirs-side CHANGELOG.
    :type theirs_footer: list[str]
    :return: The rebuilt footer lines, or ``None`` if the release link is absent.
    :rtype: list[str] | None
    """
    new_version_footer_line = None
    for line in theirs_footer:
        match = VERSION_FOOTER_LINE_RE.match(line)
        if match is not None and match.group("version") == version:
            new_version_footer_line = line
            break
    if new_version_footer_line is None:
        return None
    unreleased_link = f"[Unreleased]: {REPO_COMPARE_URL}/v{version}...HEAD"
    rebuilt = [unreleased_link, new_version_footer_line]
    for line in ours_footer:
        if not line or line.startswith("[Unreleased]:"):
            continue
        match = VERSION_FOOTER_LINE_RE.match(line)
        if match is not None and match.group("version") == version:
            continue
        rebuilt.append(line)
    return rebuilt


def _prune_consumed_fragments() -> int:
    """Delete ``changelog.d/`` fragments that the release branch consumed.

    Consumed = present in the merge-base's ``changelog.d/`` but absent in
    the release branch's ``changelog.d/`` (the release branch's
    ``cmd_assemble`` step deleted them). Fragments newly added to main
    after scope-lock did not exist at the merge-base, so they are not in
    the consumed set and are preserved.

    ``RESERVED_FILENAMES`` are excluded from consideration even if they
    happen to be missing on one side. Files matching :data:`FRAGMENT_RE`
    are deleted from the working tree; all other files are left alone.

    :return: The number of fragment files attempted to be deleted (count
        includes files that were already removed by git's auto-merge
        before this function ran).
    :rtype: int
    """
    try:
        merge_base = _git_merge_base("HEAD", "MERGE_HEAD")
    except subprocess.CalledProcessError as exc:
        print(
            f"error: failed to compute merge-base of HEAD and MERGE_HEAD: {exc}",
            file=sys.stderr,
        )
        return 0
    changelog_d_path = str(CHANGELOG_D).rstrip("/")
    baseline_fragments = _git_ls_tree(merge_base, changelog_d_path)
    theirs_fragments = _git_ls_tree("MERGE_HEAD", changelog_d_path)
    consumed_names = baseline_fragments - theirs_fragments
    pruned = 0
    for name in sorted(consumed_names):
        if name in RESERVED_FILENAMES:
            continue
        if FRAGMENT_RE.match(name) is None:
            continue
        fragment_path = CHANGELOG_D / name
        if fragment_path.exists():
            fragment_path.unlink()
        pruned += 1
    return pruned


def cmd_resolve_backmerge(version: str) -> int:
    """Resolve the CHANGELOG.md + changelog.d/ conflict produced by a back-merge.

    Reads ``CHANGELOG.md`` from refs (ours = HEAD, theirs = MERGE_HEAD) via
    :func:`_git_show_ref`. This works whether or not CHANGELOG.md was
    conflicted, because MERGE_HEAD always exists during a mid-merge state.
    Writes the merged result to ``CHANGELOG.md`` in the working tree and
    deletes ``changelog.d/`` fragments that the release branch consumed
    (determined by directory diff between the scope-lock merge-base and
    MERGE_HEAD, so post-scope-lock additions to main are preserved).

    :param version: The release version (no ``v`` prefix).
    :type version: str
    :return: ``0`` on success, ``1`` on error.
    :rtype: int
    """
    try:
        ours = _git_show_ref("HEAD", "CHANGELOG.md")
        theirs = _git_show_ref("MERGE_HEAD", "CHANGELOG.md")
    except subprocess.CalledProcessError as exc:
        print(
            f"error: failed to read CHANGELOG.md from refs: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        section_lines = _extract_version_section(theirs, version)
    except FragmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ours_body, ours_footer = _split_body_and_footer(ours)
    _, theirs_footer = _split_body_and_footer(theirs)

    merged_body = _splice_release_into_body(ours_body, section_lines)
    if merged_body is None:
        if "## [Unreleased]" not in ours_body:
            print(
                "error: ours-side CHANGELOG.md: missing ``## [Unreleased]`` heading",
                file=sys.stderr,
            )
        else:
            print(
                "error: ours-side CHANGELOG.md: no existing ``## [v...]`` heading "
                "to splice the new release before",
                file=sys.stderr,
            )
        return 1

    rebuilt_footer = _build_rebuilt_footer(version, ours_footer, theirs_footer)
    if rebuilt_footer is None:
        print(
            f"error: release-side CHANGELOG.md: missing ``[v{version}]:`` "
            "compare-link footer line",
            file=sys.stderr,
        )
        return 1

    while merged_body and merged_body[-1].strip() == "":
        merged_body.pop()
    CHANGELOG_MD.write_text(
        "\n".join([*merged_body, "", *rebuilt_footer]) + "\n",
        encoding="utf-8",
    )

    pruned = _prune_consumed_fragments()
    print(
        f"Resolved back-merge for v{version}: "
        f"merged CHANGELOG.md, pruned {pruned} consumed fragment(s).",
    )
    return 0


SENTENCE_TERMINATORS = ".!?"
TRAILING_CLOSERS = ")]}\"'`"


def ensure_terminal_punctuation(message: str) -> str:
    """Return ``message`` with terminal sentence punctuation, appending a period when needed.

    A fragment is rendered verbatim as a release-note bullet, so it has to read as
    a complete sentence. Closing delimiters are ignored when locating the final
    character, so ``Drop the flag (deprecated)`` gains a period while ``He said "stop."``
    does not.

    :param message: The single-line fragment description.
    :return: The description, guaranteed to end in sentence punctuation.
    """
    trimmed = message.rstrip(TRAILING_CLOSERS)
    if trimmed and trimmed[-1] in SENTENCE_TERMINATORS:
        return message
    return message + "."


def cmd_add(ticket: str, section: str, message: str, *, force: bool) -> int:
    """Handle the ``add`` subcommand.

    :param ticket: A ticket key from :data:`TICKET_PROJECTS`.
    :param section: The short section name, e.g. ``added``.
    :param message: The single-line description for the fragment.
    :param force: Overwrite an existing fragment when ``True``.
    :return: Process exit code (``0`` on success, ``1`` on error).
    """
    ticket = ticket.strip()
    section = section.strip().lower()
    message = message.strip()

    if TICKET_RE.match(ticket) is None:
        print(
            f"error: invalid ticket key {ticket!r} (expected {TICKET_FORMATS})",
            file=sys.stderr,
        )
        return 1
    if section not in VALID_SECTIONS:
        print(
            f"error: invalid section {section!r} (expected one of "
            f"{', '.join(sorted(VALID_SECTIONS))})",
            file=sys.stderr,
        )
        return 1
    if not message:
        print("error: message is empty", file=sys.stderr)
        return 1
    if "\n" in message:
        print("error: message must be a single line", file=sys.stderr)
        return 1

    CHANGELOG_D.mkdir(exist_ok=True)
    fragment_path = CHANGELOG_D / f"{ticket}.{section}.md"
    if fragment_path.exists() and not force:
        print(
            f"error: {fragment_path} already exists (use --force to overwrite "
            "or edit the file directly to add more entries)",
            file=sys.stderr,
        )
        return 1
    normalized = ensure_terminal_punctuation(message)
    fragment_path.write_text(normalized + "\n", encoding="utf-8")
    print(f"Created {fragment_path}")
    if normalized != message:
        print("note: appended a terminal period so the entry reads as a sentence")
    return 0


def cmd_check() -> int:
    """Handle the ``check`` subcommand.

    :return: ``0`` if all fragments are valid, ``1`` otherwise.
    """
    try:
        load_fragments()
    except FragmentError as exc:
        print("error: invalid changelog fragments:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OK: {CHANGELOG_D}/ fragments pass all checks")
    return 0


def cmd_list() -> int:
    """Handle the ``list`` subcommand.

    :return: ``0`` on success, ``1`` if fragments are invalid.
    :rtype: int
    """
    try:
        grouped = load_fragments()
    except FragmentError as exc:
        print("error: invalid changelog fragments:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    body = render_section_body(grouped)
    if not body:
        print(f"(no fragments under {CHANGELOG_D}/)")
        return 0
    sys.stdout.write(body)
    return 0


def _prepare_assemble(
    version: str,
    tickets_csv: str,
) -> tuple[
    dict[str, list[tuple[str, list[str], Path]]],
    dict[str, list[tuple[str, list[str], Path]]],
    str,
]:
    """Validate inputs and return the data needed by :func:`cmd_assemble`.

    :param version: The release version (without the ``v`` prefix).
    :type version: str
    :param tickets_csv: Comma-separated ticket keys that belong to this release.
    :type tickets_csv: str
    :return: A tuple of ``(grouped, filtered, original_changelog_text)``.
    :rtype: tuple[dict[str, list[tuple[str, list[str], Path]]], dict[str, list[tuple[str, list[str], Path]]], str]
    :raises FragmentError: If ``--tickets`` is empty, no fragments match,
        ``CHANGELOG.md`` is missing, or ``CHANGELOG.md`` already contains a
        ``## [v<version>]`` section or a ``[v<version>]:`` compare link.
    """
    tickets = _parse_tickets(tickets_csv)
    if not tickets:
        msg = "--tickets is empty"
        raise FragmentError(msg)
    grouped = load_fragments()
    filtered = _filter_by_tickets(grouped, tickets)
    if not filtered:
        msg = f"none of the given tickets have fragments under {CHANGELOG_D}/"
        raise FragmentError(msg)
    if not CHANGELOG_MD.exists():
        msg = f"{CHANGELOG_MD} does not exist"
        raise FragmentError(msg)
    changelog_text = CHANGELOG_MD.read_text(encoding="utf-8")
    version_heading = f"## [v{version}]"
    version_link_prefix = f"[v{version}]:"
    for line in changelog_text.splitlines():
        if line.startswith(version_heading):
            msg = f"{CHANGELOG_MD} already contains a ``## [v{version}]`` section"
            raise FragmentError(msg)
        if line.startswith(version_link_prefix):
            msg = f"{CHANGELOG_MD} already contains a ``[v{version}]:`` compare link"
            raise FragmentError(msg)
    return grouped, filtered, changelog_text


def cmd_assemble(
    version: str,
    date: str,
    tickets_csv: str,
    *,
    dry_run: bool,
) -> int:
    """Handle the ``assemble`` subcommand.

    :param version: The release version (without the ``v`` prefix).
    :type version: str
    :param date: The release date in ``YYYY-MM-DD``.
    :type date: str
    :param tickets_csv: Comma-separated ticket keys that belong to this release.
    :type tickets_csv: str
    :param dry_run: If ``True``, print the rendered block but do not modify files.
    :type dry_run: bool
    :return: ``0`` on success, ``1`` on error.
    :rtype: int
    """
    try:
        grouped, filtered, original = _prepare_assemble(version, tickets_csv)
        body = render_section_body(filtered)
        with_section = _splice_version_section(original, version, date, body)
        with_links = _update_compare_links(with_section, version)
    except FragmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    consumed_paths = [entry[2] for entries in filtered.values() for entry in entries]

    if dry_run:
        print(f"=== would insert section [v{version}] - {date} ===")
        sys.stdout.write(body)
        print("=== would delete fragment files ===")
        for path in consumed_paths:
            print(f"  - {path}")
        return 0

    CHANGELOG_MD.write_text(with_links, encoding="utf-8")
    for path in consumed_paths:
        path.unlink()
    kept = sum(len(entries) for entries in grouped.values()) - len(consumed_paths)
    print(
        f"Inserted [v{version}] section with "
        f"{len(consumed_paths)} fragment file(s), {kept} fragment(s) remaining.",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI.

    :return: The top-level parser with all subcommands registered.
    :rtype: argparse.ArgumentParser
    """
    parser = argparse.ArgumentParser(
        prog="changelog",
        description="Manage PMM Extensions changelog fragments under changelog.d/.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser(
        "add",
        help="Create a new changelog fragment.",
    )
    add_parser.add_argument(
        "--ticket", required=True, help=f"Ticket key: {TICKET_FORMATS}."
    )
    add_parser.add_argument(
        "--section",
        required=True,
        choices=sorted(VALID_SECTIONS),
        help="Section: added, changed, breaking, config, fixed, security.",
    )
    add_parser.add_argument(
        "--message",
        required=True,
        help="Single-line description (no leading ``- <TICKET>:`` prefix).",
    )
    add_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing fragment.",
    )

    subparsers.add_parser("check", help="Validate all fragments.")
    subparsers.add_parser("list", help="Print a CHANGELOG-style preview of fragments.")

    assemble_parser = subparsers.add_parser(
        "assemble",
        help="Assemble fragments into a new CHANGELOG.md version section.",
    )
    assemble_parser.add_argument("--version", required=True, help="X.Y.Z")
    assemble_parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    assemble_parser.add_argument(
        "--tickets",
        required=True,
        help="Comma-separated ticket keys in the Jira fix version.",
    )
    assemble_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered section and list consumed fragments without writing.",
    )

    backmerge_parser = subparsers.add_parser(
        "resolve-backmerge",
        help="Resolve CHANGELOG.md + changelog.d/ conflict after a release back-merge.",
    )
    backmerge_parser.add_argument(
        "--release",
        required=True,
        help="The release version being back-merged (no ``v`` prefix), e.g. 0.13.0.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the CLI.

    :param argv: Optional argv override for testing.
    :type argv: list[str] | None
    :return: Process exit code.
    :rtype: int
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "add":
        return cmd_add(args.ticket, args.section, args.message, force=args.force)
    if args.command == "check":
        return cmd_check()
    if args.command == "list":
        return cmd_list()
    if args.command == "resolve-backmerge":
        return cmd_resolve_backmerge(args.release)
    return cmd_assemble(
        args.version,
        args.date,
        args.tickets,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
