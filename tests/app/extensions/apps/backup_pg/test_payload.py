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
"""Tests for the pgBackRest payload script."""

import datetime
import importlib.util
import logging
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

PAYLOAD_PATH = str(
    Path(__file__).resolve().parents[5]
    / "app"
    / "extensions"
    / "apps"
    / "backup_pg"
    / "payload"
)

EXPECTED_RETENTION_FULL = 3
EXPECTED_RETENTION_ARCHIVE = 5


def _load_payload_module() -> ModuleType:
    """Load the pgBackRest payload script as an importable module."""
    loader = SourceFileLoader("backup_pg_payload_module", PAYLOAD_PATH)
    spec = importlib.util.spec_from_loader("backup_pg_payload_module", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def payload_module() -> ModuleType:
    """Return the loaded pgBackRest payload module."""
    return _load_payload_module()


def _make_pgbackrest(
    payload_module: ModuleType, tmp_path: Path, **overrides: Any
) -> Any:
    """Build a PgBackRest instance backed by a temporary backup directory."""
    (tmp_path / "backup").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    server_data = {
        "ALIAS": "test-pg",
        "BACKUP_DIR": str(tmp_path / "backup"),
        "LOGGING_DIR": str(tmp_path / "logs"),
    }
    server_data.update(overrides)
    return payload_module.PgBackRest(server_data, logger=MagicMock(spec=logging.Logger))


def _captured_backup_cmd(pg: Any) -> list[str]:
    """Run _run_backup_command() and return the command list passed to Popen."""
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value.returncode = 0
        pg._run_backup_command()
        return mock_popen.call_args[0][0]


class TestPgBackRestConfigKeys:
    """Cover PGBACKREST_-prefixed config key reads."""

    def test_reads_pgbackrest_prefixed_keys(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Read PGBACKREST_-prefixed keys from the server config."""
        pg = _make_pgbackrest(
            payload_module,
            tmp_path,
            PGBACKREST_DATADIR="/custom/datadir",
            PGBACKREST_RETENTION_FULL=EXPECTED_RETENTION_FULL,
            PGBACKREST_RETENTION_ARCHIVE=EXPECTED_RETENTION_ARCHIVE,
            PGBACKREST_INCREMENTAL_CYCLE="4",
        )
        assert pg.pg_datadir == "/custom/datadir"
        assert pg.pg_retention_full == EXPECTED_RETENTION_FULL
        assert pg.pg_retention_archive == EXPECTED_RETENTION_ARCHIVE
        assert pg.pg_incremental_cycle == "4"


class TestIncrementalCycleValidation:
    """Cover incremental-cycle normalization and validation."""

    def test_weekly_normalizes_to_1(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Normalize a ``weekly`` cycle to ``1`` (Monday)."""
        pg = _make_pgbackrest(
            payload_module, tmp_path, PGBACKREST_INCREMENTAL_CYCLE="weekly"
        )
        assert pg.pg_incremental_cycle == "1"

    def test_invalid_cycle_raises_backup_error(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Raise a BackupError for an invalid cycle value."""
        pg = _make_pgbackrest(
            payload_module, tmp_path, PGBACKREST_INCREMENTAL_CYCLE="bogus"
        )
        with pytest.raises(payload_module.BackupError):
            pg._run_backup_command()


class TestFullBackupDecision:
    """Cover the FULL-backup weekday decision in _run_backup_command()."""

    def test_matching_weekday_triggers_full(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Trigger a FULL backup when the cycle matches today's ISO weekday."""
        today_iso = datetime.datetime.now(datetime.UTC).astimezone().isoweekday()
        pg = _make_pgbackrest(
            payload_module, tmp_path, PGBACKREST_INCREMENTAL_CYCLE=str(today_iso)
        )
        cmd = _captured_backup_cmd(pg)
        assert "--type=full" in cmd

    def test_non_matching_weekday_does_not_trigger_full(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Skip the FULL backup when the cycle does not match today's ISO weekday."""
        today_iso = datetime.datetime.now(datetime.UTC).astimezone().isoweekday()
        other_day = "1" if today_iso != 1 else "2"
        pg = _make_pgbackrest(
            payload_module, tmp_path, PGBACKREST_INCREMENTAL_CYCLE=other_day
        )
        cmd = _captured_backup_cmd(pg)
        assert "--type=full" not in cmd

    def test_daily_always_triggers_full(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Trigger a FULL backup for every ``daily`` cycle run."""
        pg = _make_pgbackrest(
            payload_module, tmp_path, PGBACKREST_INCREMENTAL_CYCLE="daily"
        )
        cmd = _captured_backup_cmd(pg)
        assert "--type=full" in cmd


class TestCreateStanzaRetentionCoercion:
    """Cover str() coercion of retention values in _create_stanza()."""

    def test_int_retention_values_do_not_raise(
        self, payload_module: ModuleType, tmp_path: Path
    ) -> None:
        """Accept integer retention values without raising a TypeError."""
        cfg_file = tmp_path / "pgbackrest.conf"
        pg = _make_pgbackrest(
            payload_module,
            tmp_path,
            PGBACKREST_CONFIG_FILE=str(cfg_file),
            PGBACKREST_RETENTION_FULL=EXPECTED_RETENTION_FULL,
            PGBACKREST_RETENTION_ARCHIVE=EXPECTED_RETENTION_ARCHIVE,
        )
        with patch("subprocess.check_output") as mock_check_output:
            mock_check_output.return_value = b"OK"
            pg._create_stanza()
        content = cfg_file.read_text()
        assert f"repo1-retention-full = {EXPECTED_RETENTION_FULL}" in content
        assert f"repo1-retention-archive = {EXPECTED_RETENTION_ARCHIVE}" in content


class TestDirectoriesOnAPmmClientHost:
    """Cover the default directories when PMM Client runs the payload as ``root``.

    The payload then re-runs itself as ``postgres`` through ``sudo``, which sets
    ``SUDO_USER`` to ``root``.
    """

    def test_default_directories_follow_the_effective_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Place the default directories under the home of the user the process runs as."""
        homes = {"root": "/root", "postgres": "/var/lib/postgresql"}
        monkeypatch.setenv("SUDO_USER", "root")
        for variable in (
            "PERCONA_BACKUP_USER",
            "PERCONA_BACKUP_DEFAULT_LOGGING_DIR",
            "PERCONA_BACKUP_DEFAULT_CFG_DIR",
            "PERCONA_BACKUP_TEXTFILE_COLLECTOR_DIR",
        ):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setattr("pwd.getpwuid", lambda _uid: MagicMock(pw_name="postgres"))
        monkeypatch.setattr(
            "pwd.getpwnam", lambda name: MagicMock(pw_name=name, pw_dir=homes[name])
        )

        module = _load_payload_module()

        assert (
            Path("/var/lib/postgresql/.local/percona/logs/backup")
            == module.DEFAULT_LOGGING_DIR
        )

    def test_unreadable_collector_directory_counts_as_absent(
        self,
        payload_module: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Skip the collector report when its directory cannot be inspected."""
        pg = _make_pgbackrest(payload_module, tmp_path)

        def refuse(_path: Path) -> bool:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "is_dir", refuse)

        assert pg.check_textcollector_dir() is False
        pg.logger.warning.assert_called_once_with(
            "Textfile Collector directory %s cannot be inspected: permission denied.",
            pg.backup_textfile_collector_dir.parent,
        )
