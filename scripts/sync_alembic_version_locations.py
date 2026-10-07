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

"""Sync the ``[extensions] version_locations`` value in ``alembic.ini`` from disk.

Keeps a static ``version_locations`` line so bare ``alembic --name extensions ...``
keeps working (``ScriptDirectory.from_config`` reads it before ``env.py``),
but regenerates the value from the migrations-first filesystem walk so new
apps under ``app/extensions/apps/<name>/migrations/versions`` need no hand edit.

Run without arguments to rewrite in place; run with ``--check`` to fail
without writing when the committed value has drifted.

A regeneration that would drop an entry already listed in the ini is
refused unless ``--allow-removals`` is passed — see
``app/extensions/migrations/_orphan_heads.py`` for why a configured location
that contributes no revisions has to survive.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INI = REPO_ROOT / "alembic.ini"
DEFAULT_APPS_ROOT = REPO_ROOT / "app" / "extensions" / "apps"
MAIN_VERSIONS_ENTRY = "%(here)s/app/extensions/migrations/versions"
ENTRY_SEPARATOR = ":"

GENERATED_COMMENT = """\
# GENERATED — do not hand-edit. Rewritten by
# ``scripts/sync_alembic_version_locations.py`` from a deterministic
# filesystem walk of ``app/extensions/apps/*/migrations/versions`` (main chain
# first, then plugin dirs sorted). Entries are joined with ``:``
# (matching ``version_path_separator = :`` above). Bare ``alembic``
# reads this before ``env.py``, so the list must stay here; regenerate
# via the sync script, pre-commit, or Make migration targets.
# Regenerating never drops an entry listed here — that needs an explicit
# ``--allow-removals`` run — because a configured location that is absent
# from disk or contributes no migration scripts is how the orphan-head
# filter recognises a stripped app.
"""

_SECTION_HEADER = re.compile(r"^\[(?P<name>[^]]+)]\s*$")
_VERSION_LOCATIONS = re.compile(r"^version_locations\s*=")


class VersionLocationsRemovalError(ValueError):
    """Signal that regenerating would drop configured ``version_locations``."""

    def __init__(self, removed: tuple[str, ...]) -> None:
        """Record the entries the write would have removed.

        :param removed: Configured entries absent from the filesystem walk,
            in configuration order.
        """
        self.removed = removed
        super().__init__(
            "regenerating [extensions] version_locations would remove "
            f"{len(removed)} entry(ies): {', '.join(removed)}"
        )


def compute_version_locations(apps_root: Path) -> str:
    """Build the ``version_locations`` value for the ``[extensions]`` section.

    :param apps_root: Directory of plugin packages to scan.
    :return: Colon-joined ``%(here)s/...`` entries, main chain first.
    """
    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from app.extensions.migrations._discovery import discover_plugin_version_dirs

    entries = [MAIN_VERSIONS_ENTRY]
    for versions_dir in discover_plugin_version_dirs(apps_root):
        plugin_name = Path(versions_dir).parent.parent.name
        entries.append(
            f"%(here)s/app/extensions/apps/{plugin_name}/migrations/versions"
        )
    return ENTRY_SEPARATOR.join(entries)


def _extensions_section_bounds(lines: list[str]) -> tuple[int, int]:
    """Return ``(start, end)`` line indices for the ``[extensions]`` section body.

    ``start`` is the first line after ``[extensions]``; ``end`` is the index of the
    next section header (or ``len(lines)``).

    :param lines: ``alembic.ini`` lines (``keepends`` optional).
    :return: Inclusive-exclusive body span after the ``[extensions]`` header.
    :raises ValueError: If no ``[extensions]`` section header is present.
    """
    extensions_header: int | None = None
    for index, line in enumerate(lines):
        match = _SECTION_HEADER.match(line)
        if match is None:
            continue
        if match.group("name") == "extensions":
            extensions_header = index
            continue
        if extensions_header is not None:
            return extensions_header + 1, index
    if extensions_header is None:
        msg = "alembic.ini has no [extensions] section"
        raise ValueError(msg)
    return extensions_header + 1, len(lines)


def _reject_multiline_version_locations(
    lines: list[str], assignment_idx: int, body_end: int
) -> None:
    """Require a single-line ``version_locations`` assignment.

    Continuations are not rewritten, so leaving them would let a later
    sync falsely report the file as in sync.

    :param lines: ``alembic.ini`` lines (``keepends`` optional).
    :param assignment_idx: Index of the ``version_locations =`` line.
    :param body_end: Exclusive end of the ``[extensions]`` section body.
    :raises ValueError: If an indented ConfigParser continuation follows
        the assignment inside the ``[extensions]`` section.
    """
    for index in range(assignment_idx + 1, body_end):
        content = lines[index].rstrip("\r\n")
        # Mirror ConfigParser: blank lines and full-line comments are skipped
        # without ending the current option, so an indented line after them
        # is still a continuation.
        if content.strip() == "" or content[0] in "#;":
            continue
        if content[0].isspace():
            msg = (
                "[extensions] version_locations must be a single line; "
                "indented continuation lines are not supported"
            )
            raise ValueError(msg)
        return


def _locate_version_locations(lines: list[str]) -> tuple[int, int]:
    """Return the ``[extensions]`` body start and its ``version_locations`` line index.

    :param lines: ``alembic.ini`` lines (``keepends`` optional).
    :return: The first line index after ``[extensions]`` and the index of the
        ``version_locations =`` assignment inside it.
    :raises ValueError: If ``[extensions]`` is missing, the assignment is missing
        inside it, or the value uses indented continuations.
    """
    body_start, body_end = _extensions_section_bounds(lines)
    for index in range(body_start, body_end):
        if _VERSION_LOCATIONS.match(lines[index]):
            _reject_multiline_version_locations(lines, index, body_end)
            return body_start, index
    msg = "[extensions] section has no version_locations assignment"
    raise ValueError(msg)


def _normalize_entry(entry: str) -> str:
    """Return ``entry`` in the spelling used to compare it with another.

    ``%(here)s`` is left uninterpolated — it behaves as an ordinary leading
    path component — so the result is only ever a comparison key, never a
    value written back to the ini.

    :param entry: A single ``version_locations`` entry.
    :return: The entry with redundant separators and ``.`` segments folded
        away.
    """
    return posixpath.normpath(entry)


def _current_version_locations(text: str) -> tuple[str, ...]:
    """Return the entries currently listed in ``[extensions] version_locations``.

    The raw ``%(here)s`` entries are returned uninterpolated, matching what
    :func:`compute_version_locations` produces, so the two are comparable.

    :param text: Full ``alembic.ini`` contents.
    :return: Non-empty entries in configuration order.
    :raises ValueError: If ``[extensions]`` is missing, the assignment is missing
        inside it, or the value uses indented continuations.
    """
    lines = text.splitlines(keepends=True)
    _, assignment_idx = _locate_version_locations(lines)
    _, _, raw_value = lines[assignment_idx].partition("=")
    return tuple(
        entry
        for entry in (part.strip() for part in raw_value.split(ENTRY_SEPARATOR))
        if entry
    )


def _removed_version_locations(text: str, value: str) -> tuple[str, ...]:
    """Return configured entries the computed ``value`` would drop.

    A set difference, not a subset test: a tree that adds one app's
    migration directory while removing another's still prunes. Entries are
    matched on their normalised spelling, so a hand-written trailing slash
    is a rewrite rather than a removal.

    :param text: Full ``alembic.ini`` contents.
    :param value: The computed ``version_locations`` value.
    :return: Configured entries absent from ``value``, in configuration order
        and in their original spelling.
    :raises ValueError: If the ini cannot be parsed for its current entries.
    """
    discovered = {
        _normalize_entry(entry) for entry in value.split(ENTRY_SEPARATOR) if entry
    }
    return tuple(
        entry
        for entry in _current_version_locations(text)
        if _normalize_entry(entry) not in discovered
    )


def render_extensions_version_locations(text: str, value: str) -> str:
    """Return ``text`` with the ``[extensions] version_locations`` block rewritten.

    Replaces the contiguous comment lines immediately above
    ``version_locations =`` (and that assignment line) inside ``[extensions]``.
    Leaves every other section and line untouched. Re-emits the same
    line ending style present in ``text`` (``LF`` or ``CRLF``).

    :param text: Full ``alembic.ini`` contents with original line endings
        preserved (not universal-newline-normalized).
    :param value: The computed ``version_locations`` value.
    :return: Updated file contents.
    :raises ValueError: If ``[extensions]`` is missing, ``version_locations`` is
        missing inside it, or the existing value uses indented continuations.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    body_start, assignment_idx = _locate_version_locations(lines)

    comment_start = assignment_idx
    while comment_start > body_start and lines[comment_start - 1].lstrip().startswith(
        "#"
    ):
        comment_start -= 1

    comment_block = GENERATED_COMMENT.replace("\n", newline)
    if not comment_block.endswith(newline):
        comment_block += newline
    assignment = f"version_locations = {value}{newline}"
    new_lines = (
        lines[:comment_start]
        + [comment_block, assignment]
        + lines[assignment_idx + 1 :]
    )
    return "".join(new_lines)


def sync_alembic_ini(
    ini_path: Path,
    apps_root: Path,
    *,
    check: bool = False,
    allow_removals: bool = False,
) -> bool:
    """Rewrite or check ``ini_path`` against discovery.

    Preserves the file's existing line endings (``LF`` or ``CRLF``).

    Refuses — under ``check`` as well — when the walk would drop an entry
    the ini already lists, unless ``allow_removals`` says the deletion is
    deliberate.

    :param ini_path: Path to ``alembic.ini``.
    :param apps_root: Plugin packages directory to scan.
    :param check: When true, report drift without writing.
    :param allow_removals: When true, write even if entries are dropped.
    :return: ``True`` when the file already matched (or was rewritten);
        ``False`` when ``check`` found drift.
    :raises VersionLocationsRemovalError: When the write would drop
        configured entries and ``allow_removals`` is false.
    :raises ValueError: When the ini is missing ``[extensions]``, missing
        ``version_locations``, or uses a multi-line value.
    """
    value = compute_version_locations(apps_root)
    # newline="" disables universal-newline translation so a CRLF file still
    # contains "\r\n" for render_extensions_version_locations to detect and re-emit.
    with ini_path.open(encoding="utf-8", newline="") as handle:
        original = handle.read()
    if not allow_removals:
        removed = _removed_version_locations(original, value)
        if removed:
            raise VersionLocationsRemovalError(removed)
    updated = render_extensions_version_locations(original, value)
    if original == updated:
        return True
    if check:
        return False
    _atomic_write(ini_path, updated)
    return True


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` via a same-directory temp swap.

    Truncating the target before writing lets a concurrent ``alembic`` run read a
    torn file. Writing a sibling temp file and :func:`os.replace`-ing it in means
    readers see either the whole old file or the whole new one, and a failed
    write leaves the original intact. The target's mode is copied onto the temp
    file (``mkstemp`` creates it ``0600``).

    :param path: The file to overwrite.
    :param text: The new contents.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        shutil.copymode(path, tmp)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Sync or check ``[extensions] version_locations`` in ``alembic.ini``.

    :param argv: CLI arguments (defaults to ``sys.argv[1:]``).
    :return: ``0`` on success / in sync; ``1`` when ``--check`` finds drift,
        the write would drop configured entries, or the ini is malformed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when version_locations has drifted",
    )
    parser.add_argument(
        "--allow-removals",
        action="store_true",
        help=(
            "write even when entries currently listed in version_locations "
            "would be dropped (deliberate deletion of an app's migrations)"
        ),
    )
    parser.add_argument(
        "--ini",
        type=Path,
        default=DEFAULT_INI,
        help="path to alembic.ini (default: repo-root alembic.ini)",
    )
    parser.add_argument(
        "--apps-root",
        type=Path,
        default=DEFAULT_APPS_ROOT,
        help="plugin packages directory to scan",
    )
    args = parser.parse_args(argv)

    try:
        matched = sync_alembic_ini(
            args.ini,
            args.apps_root,
            check=args.check,
            allow_removals=args.allow_removals,
        )
    except VersionLocationsRemovalError as exc:
        action = "regenerating would remove" if args.check else "refusing to remove"
        opt_in = (
            "Regenerating with `--allow-removals` and without `--check` performs "
            "the removal, and only belongs on a tree whose migration chain is "
            "being deleted for good."
            if args.check
            else "Re-run with `--allow-removals` only when the migration chain "
            "is being deleted for good."
        )
        print(
            f"{args.ini}: {action} {len(exc.removed)} "
            f"[extensions] version_locations entry(ies): {', '.join(exc.removed)}. "
            "A configured location that is absent from disk or contributes "
            "no migration scripts is how the orphan-head filter recognises a "
            "stripped app, so removing it silently would disarm that check. "
            "Restore the migration directory; or, on a tree with an app "
            "deliberately stripped, skip this script and run "
            "`alembic --name extensions upgrade heads` directly — leaving the "
            f"entry in place is what arms the filter. {opt_in}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"{args.ini}: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if not matched:
            print(
                f"{args.ini}: [extensions] version_locations is out of date; "
                "regenerate with `python scripts/sync_alembic_version_locations.py`",
                file=sys.stderr,
            )
            return 1
        print(f"{args.ini}: [extensions] version_locations is in sync.")
        return 0

    print(f"Synced [extensions] version_locations in {args.ini}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
