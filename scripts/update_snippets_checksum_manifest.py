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

"""Regenerate the built-in snippets SHA-256 checksum manifest.

Walk ``snippets/``, hash every file except the manifest itself, and write
``snippets/builtin-snippets.sha256`` in ``sha256sum`` two-space format.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPETS_DIR = REPO_ROOT / "snippets"

sys.path.insert(0, str(REPO_ROOT))


def _snippet_files(snippets_dir: Path) -> list[Path]:
    """Return every regular file under ``snippets_dir``, sorted by path.

    :param snippets_dir: The snippets directory to walk.
    :return: The files, in sorted order.
    """
    return sorted(path for path in snippets_dir.rglob("*") if path.is_file())


async def _hash_snippet_entries(
    snippets_dir: Path,
    manifest_name: str,
) -> list[tuple[str, str]]:
    """Hash every file under ``snippets_dir`` except the manifest itself.

    :param snippets_dir: The snippets directory to walk.
    :param manifest_name: The checksum-manifest filename to skip.
    :return: ``(digest, relative_path)`` pairs in sorted-path order.
    :raises OSError: If a snippet file cannot be opened or read.
    """
    from app.extensions.snippets.checksums import (  # noqa: PLC0415
        manifest_relative_path,
        sha256_file,
    )

    entries: list[tuple[str, str]] = []
    for path in await asyncio.to_thread(_snippet_files, snippets_dir):
        relative = manifest_relative_path(path, snippets_dir)
        if relative == manifest_name:
            continue
        entries.append((await sha256_file(path), relative))
    return entries


def generate_manifest() -> tuple[int, Path]:
    """Write the built-in snippets checksum manifest under ``snippets/``.

    Skip the manifest file itself. Raise ``SystemExit`` when ``snippets/`` is
    missing.

    :return: The number of snippet files hashed and the written manifest path.
    :raises SystemExit: If the snippets directory does not exist.
    :raises OSError: If a snippet file or the manifest cannot be read or written.
    """
    from app.extensions.snippets.checksums import (  # noqa: PLC0415
        BUILTIN_CHECKSUM_MANIFEST,
    )

    if not SNIPPETS_DIR.is_dir():
        raise SystemExit(f"Snippets directory not found: {SNIPPETS_DIR}")

    manifest_path = SNIPPETS_DIR / BUILTIN_CHECKSUM_MANIFEST
    entries = asyncio.run(
        _hash_snippet_entries(SNIPPETS_DIR, BUILTIN_CHECKSUM_MANIFEST)
    )

    lines = [f"{digest}  {filename}\n" for digest, filename in entries]
    manifest_path.write_text("".join(lines), encoding="utf-8")
    return len(entries), manifest_path


def main() -> None:
    """Regenerate the checksum manifest and print how many files were hashed.

    :raises SystemExit: If the snippets directory is missing.
    :raises OSError: If a snippet file or the manifest cannot be read or written.
    """
    count, manifest_path = generate_manifest()
    print(f"Wrote {count} checksums to {manifest_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
