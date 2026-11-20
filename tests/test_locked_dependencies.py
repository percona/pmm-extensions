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

"""Check the locked dependencies that a development virtualenv needs on every platform."""

import tomllib
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parent.parent / "poetry.lock"


def test_greenlet_is_locked_for_every_platform() -> None:
    """Lock ``greenlet`` with no environment marker, so Apple Silicon Macs install it.

    SQLAlchemy's own requirement on ``greenlet`` names ``aarch64`` but not ``arm64``,
    which is what macOS reports, and every async database call needs ``greenlet``.
    """
    lock = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    (greenlet,) = (p for p in lock["package"] if p["name"] == "greenlet")

    assert "markers" not in greenlet
