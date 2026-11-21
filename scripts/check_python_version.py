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

"""Refuse an interpreter that the project does not support.

``make venv`` runs this file with the interpreter it is about to build a
virtualenv from, so it uses the standard library only and must stay importable
on interpreters older and newer than the supported range: Python 3.7 and later.
"""

from __future__ import annotations

import sys

MINIMUM = (3, 11, 9)
EXCLUDED = ((3, 12, 0), (3, 12, 1), (3, 12, 2))
UPPER_BOUND = (3, 14, 0)


def is_supported(version: tuple[int, int, int]) -> bool:
    """Tell whether ``version`` satisfies ``requires-python`` in ``pyproject.toml``.

    :param version: The interpreter's major, minor and micro numbers.
    :return: ``True`` when the project can be installed on that interpreter.
    """
    return MINIMUM <= version < UPPER_BOUND and version not in EXCLUDED


def main() -> int:
    """Check the running interpreter and report an unsupported one.

    :return: ``0`` for a supported interpreter, ``1`` otherwise.
    """
    version = (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    if is_supported(version):
        return 0
    print(
        f"venv: Python {'.'.join(str(part) for part in version)} is not supported. "
        "Use 3.11.9 to 3.13, except 3.12.0 to 3.12.2. Example: make venv PYTHON=python3.12",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
