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

"""Ensure Nomad dispatch payloads stay under the 16 KiB limit after minify+gzip."""

import argparse
import sys
from pathlib import Path

NOMAD_PAYLOAD_SIZE_LIMIT = 16384
REPO_ROOT = Path(__file__).resolve().parent.parent


def payload_size(path: str) -> int:
    """Return the size in bytes of the Python file at ``path`` as dispatch sends it.

    The file goes through dispatch's own minify and gzip helpers, so a source
    dispatch leaves unminified is measured unminified here too.

    :param path: Payload file path.
    :return: The gzip-compressed size in bytes.
    :raises SystemExit: When ``python_minifier`` is not importable.
    """
    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from app.tasks.execution.utils import (  # noqa: PLC0415
            gzip_compress,
            minify_file_content,
        )
    except ModuleNotFoundError as err:
        if err.name != "python_minifier":
            raise
        raise SystemExit(
            "python_minifier is not installed for this Python interpreter.\n"
            "Use the project venv interpreter, or let make resolve it:\n"
            "  make check-nomad-payload-size ARGS='--report <path>...'"
        ) from err

    with open(path, encoding="utf-8") as f:  # noqa: PTH123
        return len(gzip_compress(minify_file_content(f.read())))


def check_payload(path: str) -> tuple[str, int] | None:
    """Minify and gzip a Python file, returning ``(path, size)`` if it exceeds the Nomad limit.

    :param path: Payload file path.
    :return: ``(path, size)`` when the size exceeds the limit, otherwise ``None``.
    """
    sz = payload_size(path)
    if sz > NOMAD_PAYLOAD_SIZE_LIMIT:
        return (path, sz)
    return None


def format_report_line(path: str, size: int) -> str:
    """Format one ``--report`` output line for ``path`` at minify+gzip ``size``.

    :param path: Payload file path.
    :param size: Minify+gzip size in bytes.
    :return: Human-readable report line.
    """
    limit = NOMAD_PAYLOAD_SIZE_LIMIT
    if size <= limit:
        margin = limit - size
        return f"{path}: {size:,} / {limit:,} bytes ({margin:,} bytes headroom)"
    over = size - limit
    return f"{path}: {size:,} / {limit:,} bytes ({over:,} bytes OVER LIMIT)"


def main(argv: list[str] | None = None) -> int:
    """Check every file path passed as a CLI argument against the Nomad payload limit.

    Returns 0 when all payloads are within the limit, or 1 after printing
    a summary of the offending files.  With ``--report``, print one line per
    path giving its size against the limit and its remaining headroom (or the
    number of bytes over), and return 0.

    :param argv: CLI arguments (defaults to ``sys.argv[1:]``).
    :return: Process exit code.
    :raises SystemExit: With usage status 2 when no payload paths are provided.
    """
    parser = argparse.ArgumentParser(
        description="Check Nomad dispatch payload sizes after minify+gzip.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print size and headroom for each path (always exits 0).",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="path",
        help="Payload file path(s) to check.",
    )
    args = parser.parse_args(argv)

    if args.report:
        for path in args.paths:
            print(format_report_line(path, payload_size(path)))
        return 0

    failed = []
    for path in args.paths:
        result = check_payload(path)
        if result is not None:
            failed.append(result)
    if failed:
        print("ERROR: Payload(s) exceed Nomad 16 KiB dispatch limit:")
        for p, sz in failed:
            print(f"  {p}: {sz:,} bytes (limit: {NOMAD_PAYLOAD_SIZE_LIMIT:,})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
