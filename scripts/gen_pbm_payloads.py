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

"""Sync the canonical PBM generated regions into each backup_mongo payload.

Payloads are shipped by ``file://`` and can't import shared code, so this
rewrites the block between each payload's ``# --- BEGIN/END GENERATED ... ---``
markers with the canonical region from
``app/extensions/apps/backup_mongo/pbm_creds_common.py``. Each region is independent: a
payload opts into one by carrying that region's BEGIN marker as a full line, and a
file with no markers is left untouched. Regions carried by only a subset of
payloads (config-apply, restore ``--yes``, textfile-collector) sync to exactly
that subset.

Run without arguments to rewrite in place; run with ``--check`` (the CI guard) to
fail without writing when a payload has drifted.
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEARCH_ROOT = REPO_ROOT / "app" / "extensions" / "apps" / "backup_mongo"
CANONICAL_SOURCE = (
    REPO_ROOT / "app" / "extensions" / "apps" / "backup_mongo" / "pbm_creds_common.py"
)


def find_payloads(search_root: Path, begin_marker: str) -> list[Path]:
    """Return the payload files that opt into the shared preamble via the marker.

    A file opts in by carrying ``begin_marker`` as a full line (not merely as a
    substring, so the marker-defining constants in the canonical source do not
    match). The canonical source itself is skipped.

    :param search_root: The directory tree scanned for opted-in payloads.
    :param begin_marker: The BEGIN marker line a payload must contain.
    :return: The opted-in payload paths, sorted for deterministic output.
    """
    found = []
    for path in sorted(search_root.rglob("*")):
        if not path.is_file() or path.resolve() == CANONICAL_SOURCE.resolve():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        if begin_marker in lines:
            found.append(path)
    return found


def render(text: str, region: str, begin_marker: str, end_marker: str) -> str:
    """Return ``text`` with its marked region replaced by ``region``.

    Splitting on the newline character (rather than :meth:`str.splitlines`)
    preserves the file's trailing newline through the round-trip.

    :param text: The payload's current source text.
    :param region: The canonical preamble body to place between the markers.
    :param begin_marker: The BEGIN marker line.
    :param end_marker: The END marker line.
    :return: The rewritten payload source.
    :raises ValueError: When either marker line is absent or out of order.
    """
    lines = text.split("\n")
    try:
        begin = lines.index(begin_marker)
        end = lines.index(end_marker, begin + 1)
    except ValueError as exc:
        raise ValueError("payload is missing a PBM CREDS PREAMBLE marker line") from exc
    rebuilt = [
        *lines[:begin],
        begin_marker,
        *region.split("\n"),
        end_marker,
        *lines[end + 1 :],
    ]
    return "\n".join(rebuilt)


def _sync_region(
    root: Path,
    label: str,
    begin_marker: str,
    end_marker: str,
    region: str,
    *,
    check: bool,
) -> tuple[list[Path], list[Path], list[Path]]:
    """Sync one marked region across every payload that opts into it.

    A payload opts into a region by carrying ``begin_marker``; markerless payloads
    are skipped, so a region carried by only a subset of payloads (e.g. the
    config-apply block) syncs to exactly that subset.

    :param root: The directory tree scanned for opted-in payloads.
    :param label: Human-readable region name used in log/error output.
    :param begin_marker: The BEGIN marker line delimiting the region.
    :param end_marker: The END marker line delimiting the region.
    :param region: The canonical region body to materialize between the markers.
    :param check: When ``True``, report drift without writing.
    :return: The ``(payloads, drift, rewritten)`` paths for this region.
    """
    payloads = find_payloads(root, begin_marker)
    if not payloads:
        print(f"No payloads carry the {label} marker under {root}", file=sys.stderr)
        return [], [], []

    drift = []
    rewritten = []
    for path in payloads:
        rel = path.relative_to(REPO_ROOT)
        current = path.read_text(encoding="utf-8")
        updated = render(current, region, begin_marker, end_marker)
        if updated == current:
            print(f"unchanged  [{label}] {rel}")
            continue
        if check:
            drift.append(path)
            print(f"drifted    [{label}] {rel}")
        else:
            path.write_text(updated, encoding="utf-8")
            rewritten.append(path)
            print(f"rewrote    [{label}] {rel}")
    return payloads, drift, rewritten


def _load_canonical() -> ModuleType:
    """Return the canonical source module, loaded by path.

    Loaded by path rather than imported: ``app.extensions.apps.backup_mongo.__init__``
    imports the whole app, which a pre-commit hook must not have to stand up.

    :return: The ``pbm_creds_common`` module.
    :raises RuntimeError: When no import spec can be built for the canonical source.
    """
    spec = importlib.util.spec_from_file_location("pbm_creds_common", CANONICAL_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the canonical regions from {CANONICAL_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    """Rewrite or check the payload generated regions against their canonical sources.

    :param argv: The CLI arguments (defaults to ``sys.argv``).
    :return: ``0`` when every payload is in sync (or was rewritten); ``1`` when
        ``--check`` finds drift or a region has no opted-in payload.
    """
    canonical = _load_canonical()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when any payload has drifted from the canonical region",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_SEARCH_ROOT,
        help="directory tree scanned for opted-in payloads (default: the backup_mongo app)",
    )
    args = parser.parse_args(argv)

    regions = (
        (
            "creds preamble",
            canonical.PREAMBLE_BEGIN,
            canonical.PREAMBLE_END,
            canonical.preamble_source(),
        ),
        (
            "config apply",
            canonical.CONFIG_APPLY_BEGIN,
            canonical.CONFIG_APPLY_END,
            canonical.config_apply_source(),
        ),
        (
            "restore yes",
            canonical.RESTORE_YES_BEGIN,
            canonical.RESTORE_YES_END,
            canonical.restore_yes_source(),
        ),
        (
            "textfile collector",
            canonical.TEXTFILE_BEGIN,
            canonical.TEXTFILE_END,
            canonical.textfile_source(),
        ),
    )

    total_payloads = 0
    total_drift: list[Path] = []
    total_rewritten: list[Path] = []
    missing = False
    for label, begin, end, region in regions:
        payloads, drift, rewritten = _sync_region(
            args.root, label, begin, end, region, check=args.check
        )
        if not payloads:
            missing = True
            continue
        total_payloads += len(payloads)
        total_drift += drift
        total_rewritten += rewritten

    if missing:
        return 1

    if args.check:
        if total_drift:
            rels = [str(path.relative_to(REPO_ROOT)) for path in total_drift]
            print(
                f"PBM payload region drift: {rels}; regenerate with "
                "`python scripts/gen_pbm_payloads.py`",
                file=sys.stderr,
            )
            return 1
        print(
            f"All {total_payloads} PBM payload regions are in sync with their "
            "canonical sources."
        )
        return 0

    print(
        f"Synced {total_payloads} PBM payload regions: "
        f"{len(total_rewritten)} rewritten, "
        f"{total_payloads - len(total_rewritten)} already in sync."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
