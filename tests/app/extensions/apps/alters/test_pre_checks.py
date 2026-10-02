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

import pymysql
import pytest
from pytest_mock import MockerFixture

from app.extensions.apps.alters.pre_checks import MySQLPreChecks

_MODULE = "app.extensions.apps.alters.pre_checks"
_DATADIR = "/var/lib/mysql"
_TABLE_SIZE_MB = 100.0
_BYTES_PER_MB = 1024 * 1024


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


def _patch_disk_usage(mocker: MockerFixture, *, free_mb: float) -> MagicMock:
    """Patch ``shutil.disk_usage`` so the datadir reports ``free_mb`` free."""
    usage = mocker.patch(f"{_MODULE}.shutil.disk_usage")
    usage.return_value = (_BYTES_PER_MB * 1000, 0, int(free_mb * _BYTES_PER_MB))
    return usage


def _disk_queries() -> list[tuple[object, ...]]:
    """Return the table-size and datadir rows ``check_disk_space`` reads."""
    return [(_TABLE_SIZE_MB,), ("datadir", _DATADIR)]


class TestCheckDiskSpace:
    """Test ``check_disk_space``."""

    def test_passes_when_free_space_exceeds_table_size(
        self, mocker: MockerFixture
    ) -> None:
        """Free space above the table size lets the alter proceed."""
        checks = _make_checks()
        _connect_with_cursor(mocker, checks, fetchone=_disk_queries())
        usage = _patch_disk_usage(mocker, free_mb=200)

        assert checks.check_disk_space() is True
        usage.assert_called_once_with(_DATADIR)

    @pytest.mark.parametrize("free_mb", [100, 50], ids=["equal", "below"])
    def test_fails_when_free_space_does_not_exceed_table_size(
        self,
        mocker: MockerFixture,
        free_mb: float,
    ) -> None:
        """Free space at or below the table size blocks the alter."""
        checks = _make_checks()
        _connect_with_cursor(mocker, checks, fetchone=_disk_queries())
        usage = _patch_disk_usage(mocker, free_mb=free_mb)

        assert checks.check_disk_space() is False
        usage.assert_called_once_with(_DATADIR)

    def test_fails_when_table_size_is_unknown(self, mocker: MockerFixture) -> None:
        """A missing table size stops the check before the filesystem is touched."""
        checks = _make_checks()
        _connect_with_cursor(mocker, checks, fetchone=[None])
        usage = _patch_disk_usage(mocker, free_mb=200)

        assert checks.check_disk_space() is False
        usage.assert_not_called()

    def test_fails_when_datadir_is_missing(self, mocker: MockerFixture) -> None:
        """A missing datadir stops the check before the filesystem is touched."""
        checks = _make_checks()
        _connect_with_cursor(mocker, checks, fetchone=[(_TABLE_SIZE_MB,), None])
        usage = _patch_disk_usage(mocker, free_mb=200)

        assert checks.check_disk_space() is False
        usage.assert_not_called()

    def test_fails_when_disk_usage_raises(self, mocker: MockerFixture) -> None:
        """An unreadable datadir partition fails the disk check."""
        checks = _make_checks()
        _connect_with_cursor(mocker, checks, fetchone=_disk_queries())
        usage = mocker.patch(
            f"{_MODULE}.shutil.disk_usage",
            side_effect=OSError("offline"),
        )

        assert checks.check_disk_space() is False
        usage.assert_called_once_with(_DATADIR)


def _assert_query_targets(
    cursor: MagicMock,
    checks: MySQLPreChecks,
    *predicates: str,
) -> None:
    """Assert the execute call binds ``checks`` schema and table to ``predicates``."""
    query, params = cursor.execute.call_args.args
    assert params == (checks.schema, checks.table)
    for predicate in predicates:
        assert f"{predicate} = %s" in query


_FOREIGN_KEY_ROW = (
    "otherdb",
    "children",
    "fk_orders",
    "order_id",
    "appdb",
    "orders",
    "id",
)
_TRIGGER_ROW = ("appdb", "orders", "appdb", "orders_bi", "INSERT")


class TestCheckForeignKeyReferences:
    """Test ``check_foreign_key_references``."""

    def test_passes_when_nothing_references_the_table(
        self, mocker: MockerFixture
    ) -> None:
        """No referencing foreign keys lets the alter proceed."""
        checks = _make_checks()
        cursor = _connect_with_cursor(mocker, checks, fetchall=[[]])

        assert checks.check_foreign_key_references() is True
        _assert_query_targets(
            cursor,
            checks,
            "REFERENCED_TABLE_SCHEMA",
            "REFERENCED_TABLE_NAME",
        )

    def test_fails_when_a_foreign_key_references_the_table(
        self, mocker: MockerFixture
    ) -> None:
        """A referencing foreign key blocks the alter."""
        checks = _make_checks()
        cursor = _connect_with_cursor(mocker, checks, fetchall=[[_FOREIGN_KEY_ROW]])

        assert checks.check_foreign_key_references() is False
        _assert_query_targets(
            cursor,
            checks,
            "REFERENCED_TABLE_SCHEMA",
            "REFERENCED_TABLE_NAME",
        )

    def test_fails_when_the_cursor_raises(self, mocker: MockerFixture) -> None:
        """A query error blocks the alter."""
        checks = _make_checks()
        cursor = _connect_with_cursor(
            mocker,
            checks,
            execute_error=pymysql.Error("foreign key lookup failed"),
        )

        assert checks.check_foreign_key_references() is False
        _assert_query_targets(
            cursor,
            checks,
            "REFERENCED_TABLE_SCHEMA",
            "REFERENCED_TABLE_NAME",
        )


class TestCheckTableTriggers:
    """Test ``check_table_triggers``."""

    def test_passes_when_the_table_has_no_triggers(self, mocker: MockerFixture) -> None:
        """No triggers lets the alter proceed."""
        checks = _make_checks()
        cursor = _connect_with_cursor(mocker, checks, fetchall=[[]])

        assert checks.check_table_triggers() is True
        _assert_query_targets(
            cursor,
            checks,
            "EVENT_OBJECT_SCHEMA",
            "EVENT_OBJECT_TABLE",
        )

    def test_fails_when_the_table_has_a_trigger(self, mocker: MockerFixture) -> None:
        """A trigger on the table blocks the alter."""
        checks = _make_checks()
        cursor = _connect_with_cursor(mocker, checks, fetchall=[[_TRIGGER_ROW]])

        assert checks.check_table_triggers() is False
        _assert_query_targets(
            cursor,
            checks,
            "EVENT_OBJECT_SCHEMA",
            "EVENT_OBJECT_TABLE",
        )

    def test_fails_when_the_cursor_raises(self, mocker: MockerFixture) -> None:
        """A query error blocks the alter."""
        checks = _make_checks()
        cursor = _connect_with_cursor(
            mocker,
            checks,
            execute_error=pymysql.Error("trigger lookup failed"),
        )

        assert checks.check_table_triggers() is False
        _assert_query_targets(
            cursor,
            checks,
            "EVENT_OBJECT_SCHEMA",
            "EVENT_OBJECT_TABLE",
        )


def _executed_sql(cursor: MagicMock) -> list[str]:
    """Return the SQL strings passed to ``cursor.execute``, in order."""
    return [call.args[0] for call in cursor.execute.call_args_list]


class TestRunAllChecks:
    """Test ``run_all_checks``."""

    def test_passes_when_every_check_passes(self, mocker: MockerFixture) -> None:
        """Disk, foreign-key, and trigger checks all passing lets the alter proceed."""
        checks = _make_checks()
        _connect_with_cursor(
            mocker,
            checks,
            fetchone=_disk_queries(),
            fetchall=[[], []],
        )
        _patch_disk_usage(mocker, free_mb=200)

        assert checks.run_all_checks() is True

    def test_skips_disk_space_when_filesystem_checks_are_disabled(
        self, mocker: MockerFixture
    ) -> None:
        """The skip flag leaves the real disk check uncalled."""
        checks = _make_checks(skip_filesystem_checks=True)
        cursor = _connect_with_cursor(mocker, checks, fetchall=[[], []])
        disk_space = mocker.spy(checks, "check_disk_space")

        assert checks.run_all_checks() is True
        assert disk_space.call_count == 0
        sql = _executed_sql(cursor)
        assert any("REFERENCED_TABLE_SCHEMA = %s" in query for query in sql)
        assert any("EVENT_OBJECT_SCHEMA = %s" in query for query in sql)

    def test_fails_when_one_check_fails_and_later_checks_still_run(
        self, mocker: MockerFixture
    ) -> None:
        """A foreign-key failure fails the aggregate, and the trigger check still runs."""
        checks = _make_checks()
        cursor = _connect_with_cursor(
            mocker,
            checks,
            fetchone=_disk_queries(),
            fetchall=[[_FOREIGN_KEY_ROW], []],
        )
        _patch_disk_usage(mocker, free_mb=200)

        assert checks.run_all_checks() is False
        sql = _executed_sql(cursor)
        assert any("EVENT_OBJECT_SCHEMA = %s" in query for query in sql)
