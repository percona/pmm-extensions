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
"""Resolve shared side-car runtime inputs and write diagnostics."""

import os
import sys
from pathlib import Path

DEFAULT_STATE_DIR = Path("/home/extensions/state")
RETRY_INTERVAL_SECONDS = 3.0


def state_dir() -> Path:
    """Return the directory PMM Extensions persists its minted credentials in.

    :return: The configured directory, or the image's own.
    """
    configured = os.environ.get("EXTENSIONS_STATE_DIR") or ""
    return Path(configured) if configured.strip() else DEFAULT_STATE_DIR


def warn(prefix: str, message: str) -> None:
    """Write one diagnostic line, leaving stdout as the credential channel alone.

    :param prefix: The script's diagnostic label, without brackets.
    :param message: The line to write.
    """
    sys.stderr.write(f"[{prefix}] {message}\n")
