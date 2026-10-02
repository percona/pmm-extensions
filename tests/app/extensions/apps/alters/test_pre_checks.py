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

"""Tests for pt-online-schema-change pre-checks."""

from collections.abc import Sequence
from unittest.mock import MagicMock

from pytest_mock import MockerFixture

from app.extensions.apps.alters.pre_checks import MySQLPreChecks

_MODULE = "app.extensions.apps.alters.pre_checks"


def _make_checks(
    *,
    schema: str = "appdb",
    table: str = "orders",
    skip_filesystem_checks: bool = False,
) -> MySQLPreChecks:
    """Build a pre-checks instance for ``schema.table``."""
    return MySQLPreChecks(
        schema=schema,
        table=table,
        skip_filesystem_checks=skip_filesystem_checks,
    )


def _connect_with_cursor(
    mocker: MockerFixture,
    checks: MySQLPreChecks,
    *,
    fetchone: Sequence[object] | None = None,
    fetchall: Sequence[object] | None = None,
    execute_error: Exception | None = None,
) -> MagicMock:
    """Patch ``pymysql.connect``, open it, and return the cursor.

    Each entry in ``fetchone`` or ``fetchall`` is one call's return value, in
    order. ``execute_error`` makes ``cursor.execute`` raise that exception.
    """
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    if execute_error is not None:
        cursor.execute.side_effect = execute_error
    if fetchone is not None:
        cursor.fetchone.side_effect = list(fetchone)
    if fetchall is not None:
        cursor.fetchall.side_effect = list(fetchall)

    connection = MagicMock()
    connection.cursor.return_value = cursor
    mocker.patch(f"{_MODULE}.pymysql.connect", return_value=connection)
    assert checks.connect_to_mysql() is True
    return cursor
