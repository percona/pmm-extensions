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

"""Tests for the archives payload script — specifically pt_archive_runner."""

import types
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def payload():
    """Import the archives payload script as a module (no .py extension)."""
    payload_path = Path(__file__).parents[5] / "app/extensions/apps/archives/payload"
    loader = SourceFileLoader("archives_payload", str(payload_path))
    mod = types.ModuleType("archives_payload")
    loader.exec_module(mod)
    return mod


def _base_conf(overrides: dict | None = None) -> dict:
    """Return a minimal conf dict for pt_archive_runner."""
    conf = {
        "prg_alias": "test_task",
        "src_host": "source_host",
        "src_port": 3306,
        "src_db": "mydb",
        "src_tbl": "mytable",
        "dst_host": "dest_host",
        "dst_port": 3306,
        "dst_db": "destdb",
        "dst_tbl": "desttable",
        "dst_file": None,
        "prg_where": "id > 100",
        "prg_limit": 1000,
        "prg_sleep": 1,
        "prg_purge": 0,
        "prg_extra_args": None,
        "prg_disable_binlog": 0,
        "prg_disable_bulk_insert": 0,
        "prg_use_index": None,
    }
    if overrides:
        conf.update(overrides)
    return conf


class TestPtArchiveRunnerBulkInsert:
    """Test --bulk-insert flag behaviour in pt_archive_runner."""

    def test_bulk_insert_included_by_default(self, payload):
        """--bulk-insert and --bulk-delete are both added to the dest-table branch by default."""
        conf = _base_conf()
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--bulk-insert" in args
        assert "--bulk-delete" in args

    def test_bulk_insert_omitted_when_disabled(self, payload):
        """--bulk-insert is not added when prg_disable_bulk_insert=1; --bulk-delete stays."""
        conf = _base_conf({"prg_disable_bulk_insert": 1})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--bulk-insert" not in args
        assert "--bulk-delete" in args

    def test_dest_arg_still_present_when_bulk_insert_disabled(self, payload):
        """--dest=... is always included regardless of disable_bulk_insert."""
        conf = _base_conf({"prg_disable_bulk_insert": 1})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert any(a.startswith("--dest=") for a in args)

    def test_bulk_insert_not_in_purge_branch(self, payload):
        """--bulk-insert is never added in the purge-only (prg_purge=1) branch."""
        conf = _base_conf({"prg_purge": 1, "prg_disable_bulk_insert": 0})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--bulk-insert" not in args
        assert "--bulk-delete" in args
        assert "--purge" in args

    def test_purge_branch_writes_nowhere(self, payload):
        """The purge-only branch archives to no destination at all."""
        conf = _base_conf({"prg_purge": 1})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert not any(a.startswith("--dest=") for a in args)
        assert not any(a.startswith("--file=") for a in args)

    def test_bulk_insert_not_in_dest_file_branch(self, payload):
        """--bulk-insert is never added in the dest-file branch; --bulk-delete and --buffer are."""
        conf = _base_conf(
            {"dst_file": "/tmp/archive.csv", "prg_disable_bulk_insert": 0}
        )
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--bulk-insert" not in args
        assert "--bulk-delete" in args
        assert "--buffer" in args
        assert any(a.startswith("--file=") for a in args)

    def test_use_index_does_not_affect_bulk_insert_behavior(self, payload):
        """disable_bulk_insert=1 omits --bulk-insert even when use_index is set."""
        conf = _base_conf({"prg_disable_bulk_insert": 1, "prg_use_index": "PRIMARY"})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--bulk-insert" not in args
        assert any(a.startswith("--dest=") for a in args)


class TestPtArchiveRunnerArgs:
    """Test argument construction in pt_archive_runner beyond bulk-insert gating."""

    def test_use_index_adds_i_flag_to_source_arg(self, payload):
        """--source= includes i=<index> when prg_use_index is set."""
        conf = _base_conf({"prg_use_index": "PRIMARY"})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        source_arg = next(a for a in args if a.startswith("--source="))
        assert "i=PRIMARY" in source_arg

    def test_no_use_index_omits_i_flag_from_source_arg(self, payload):
        """--source= has no i= when prg_use_index is None."""
        conf = _base_conf()
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        source_arg = next(a for a in args if a.startswith("--source="))
        assert ",i=" not in source_arg

    def test_disable_binlog_reflected_in_source_arg(self, payload):
        """--source= b= value matches prg_disable_binlog."""
        conf = _base_conf({"prg_disable_binlog": 1})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        source_arg = next(a for a in args if a.startswith("--source="))
        assert "b=1" in source_arg

    def test_disable_binlog_reflected_in_dest_arg(self, payload):
        """--dest= b= value matches prg_disable_binlog."""
        conf = _base_conf({"prg_disable_binlog": 1})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        dest_arg = next(a for a in args if a.startswith("--dest="))
        assert "b=1" in dest_arg

    def test_extra_args_appended_to_command(self, payload):
        """prg_extra_args tokens are appended to the pt-archiver command."""
        conf = _base_conf({"prg_extra_args": "--no-check-columns --dry-run"})
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        args = mock_run.call_args[0]
        assert "--no-check-columns" in args
        assert "--dry-run" in args

    def test_no_extra_args_when_none(self, payload):
        """No spurious extra tokens are added when prg_extra_args is None."""
        conf = _base_conf()
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.pt_archive_runner("pt-archiver", "mydb", "mytable", conf)

        # call_args[0] = (cmd, *pt_archiver_flags); skip the cmd at index 0
        pt_archiver_flags = mock_run.call_args[0][1:]
        assert all(isinstance(a, str) and a.startswith("--") for a in pt_archiver_flags)


def _ddl_conf() -> dict:
    """Return a minimal conf dict for DDL helpers."""
    return {
        "src_host": "source_host",
        "src_port": 3306,
        "dst_host": "dest_host",
        "dst_port": 3306,
        "dst_db": "destdb",
    }


def _extract_sql(call_args) -> str:
    """Extract the SQL command from a run_cmd("mysql", *cmd) call."""
    args = call_args[0]
    sql_arg = next(a for a in args if isinstance(a, str) and a.startswith("-e"))
    return sql_arg[len("-e") :]


class TestSwapCreateTableQuoting:
    """SQL emitted by swap_create_table backtick-quotes every identifier."""

    def test_plain_identifiers_are_backtick_quoted(self, payload):
        """Standard identifiers are wrapped in backticks in CREATE/RENAME TABLE."""
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.swap_create_table("mydb", "mytable", "mytable_old", _ddl_conf())

        sql = _extract_sql(mock_run.call_args_list[0])
        assert "CREATE TABLE `mydb`.`mytable_tmp` LIKE `mydb`.`mytable`;" in sql
        assert (
            "RENAME TABLE `mydb`.`mytable` TO `mydb`.`mytable_old`,"
            " `mydb`.`mytable_tmp` TO `mydb`.`mytable`;"
        ) in sql

    def test_hyphenated_swap_suffix_does_not_break_sql(self, payload):
        """A swap-name like ``my_table_2026-04-29`` survives MySQL parsing via backticks.

        Without backticks MySQL would parse the hyphens as subtraction
        operators in the RENAME TABLE clause and reject the statement.
        """
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.swap_create_table(
                "mydb", "my_table", "my_table_2026-04-29", _ddl_conf()
            )

        sql = _extract_sql(mock_run.call_args_list[0])
        assert "`mydb`.`my_table_2026-04-29`" in sql
        assert "mydb.my_table_2026-04-29" not in sql


class TestDropTableQuoting:
    """SQL emitted by drop_table backtick-quotes every identifier."""

    def test_plain_identifiers_are_backtick_quoted(self, payload):
        """Standard identifiers are wrapped in backticks in DROP TABLE."""
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.drop_table("mydb", "mytable", _ddl_conf())

        sql = _extract_sql(mock_run.call_args)
        assert sql == "DROP TABLE `mydb`.`mytable`;"

    def test_hyphenated_table_name_does_not_break_sql(self, payload):
        """A table name with hyphens (date suffix) survives MySQL parsing via backticks."""
        with patch.object(payload, "run_cmd", return_value=0) as mock_run:
            payload.drop_table("mydb", "my_table_2026-04-29", _ddl_conf())

        sql = _extract_sql(mock_run.call_args)
        assert sql == "DROP TABLE `mydb`.`my_table_2026-04-29`;"
