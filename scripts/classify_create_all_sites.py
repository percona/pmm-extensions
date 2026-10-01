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

"""Classify every ``create_all`` call under ``tests/`` by the engine it runs on.

:func:`tests.app.db_schema.apply_schema` is a drop-in for ``create_all`` on an
**async in-memory SQLite** connection only: it replays captured DDL through
``executescript``, which asyncpg lacks and which ``apply_schema`` reaches only
through an ``AsyncConnection`` (stdlib ``sqlite3`` has it, so the sync bucket is
unconverted, not unconvertible). The metadata expression cannot tell the buckets apart — nearly every
site reads ``SQLModel.metadata.create_all`` whichever engine it runs on — so this
walks back to the enclosing function and classifies by the engine it binds.

Run it after adding a ``create_all`` site to see which bucket it lands in::

    python3 scripts/classify_create_all_sites.py
    python3 scripts/classify_create_all_sites.py --bucket real-db
"""

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REAL_DB_ENGINES = ("postgres_engine",)


class Bucket(StrEnum):
    """Enumerate the engine kinds a ``create_all`` site can bind."""

    CONVERTIBLE = "async-sqlite"
    REAL_DB = "real-db"
    SYNC = "sync-sqlite"


@dataclass(frozen=True, slots=True)
class Site:
    """Represent one ``create_all`` call site and the bucket it falls in.

    :param path: The file the call sits in.
    :param line: The 1-indexed line of the call.
    :param func: The enclosing function's name, or ``<module>``.
    :param bucket: The engine kind the enclosing function binds.
    :param why: The signal the classification was drawn from.
    """

    path: Path
    line: int
    func: str
    bucket: Bucket
    why: str

    def __str__(self) -> str:
        """Return the one-line report row for this site."""
        return f"{self.path}:{self.line}  [{self.bucket}]  {self.func} — {self.why}"


def _enclosing(
    tree: ast.Module, line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the innermost function containing ``line``.

    :param tree: The parsed module.
    :param line: The 1-indexed line the call sits on.
    :return: The innermost enclosing function node, or ``None`` at module level.
    """
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno <= line <= (node.end_lineno or node.lineno) and (
            best is None or node.lineno > best.lineno
        ):
            best = node
    return best


def _classify(source: str | None) -> tuple[Bucket, str]:
    """Return the bucket and reason for a call whose enclosing source is ``source``.

    :param source: The enclosing function's source text, or ``None`` when the
        call sits at module level.
    :return: A ``(bucket, reason)`` pair.
    """
    if source is None:
        return Bucket.SYNC, "module level, no engine binding"
    for name in REAL_DB_ENGINES:
        if name in source:
            return Bucket.REAL_DB, f"binds {name}"
    if "create_engine(" in source and "create_async_engine(" not in source:
        return Bucket.SYNC, "synchronous create_engine"
    return Bucket.CONVERTIBLE, "async in-memory SQLite engine"


def classify_sites(root: Path) -> Iterator[Site]:
    """Yield every classified ``create_all`` site under ``root``.

    :param root: The ``tests/`` directory to walk.
    :return: An iterator over the classified sites, in path order.
    """
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "create_all" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr != "create_all":
                continue
            enclosing = _enclosing(tree, node.lineno)
            segment = ast.get_source_segment(text, enclosing) if enclosing else None
            bucket, why = _classify(segment)
            yield Site(
                path=path,
                line=node.lineno,
                func=enclosing.name if enclosing else "<module>",
                bucket=bucket,
                why=why,
            )


def main() -> int:
    """Print the classification report.

    :return: ``0`` always — the script reports, it does not gate.
    """
    parser = argparse.ArgumentParser(description="Classify tests/ create_all sites.")
    parser.add_argument(
        "--bucket", type=Bucket, choices=list(Bucket), help="show only this bucket"
    )
    parser.add_argument("--root", type=Path, default=Path("tests"))
    args = parser.parse_args()

    sites = [s for s in classify_sites(args.root) if args.bucket in (None, s.bucket)]
    counts = {b: sum(s.bucket == b for s in sites) for b in Bucket}
    tally = " ".join(f"{bucket}={counts[bucket]}" for bucket in Bucket)
    print("\n".join(str(site) for site in sites))
    print(f"\ntotal={len(sites)} {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
