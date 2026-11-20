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

"""Pin how a restore payload reports a restore that failed for its host.

The executor reads a run's outcome from the script's exit status alone, so a
restore script that logs a failure and still exits 0 leaves the task history at
``success``. The XtraBackup script also has to give up when MySQL keeps
answering after the stop command: carrying on restores beside a running server
and then runs replication statements on it.
"""

import pathlib
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_BINLOG_PAYLOAD_PATH,
    RESTORE_MYDUMPER_PAYLOAD_PATH,
    RESTORE_PAYLOAD_PATH,
)

#: The exit status every restore script reserves for a failed restore.
FAILED_EXIT_STATUS = 2


class _BackupError(Exception):
    """Mirror the payloads' ``BackupError``, which carries its text as ``message``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _OperationalError(Exception):
    """Stand in for the driver error a refused connection raises."""


@dataclass(frozen=True, slots=True)
class _Script:
    """Name one restore script and the per-host class its ``Restorer`` drives."""

    path: pathlib.Path
    restore_class: str
    backup_type: str


_SCRIPTS = (
    pytest.param(_Script(RESTORE_PAYLOAD_PATH, "Xtrabackup", "X"), id="xtrabackup"),
    pytest.param(_Script(RESTORE_BINLOG_PAYLOAD_PATH, "Binlog", "B"), id="binlog"),
    pytest.param(
        _Script(RESTORE_MYDUMPER_PAYLOAD_PATH, "Myloader", "M"), id="mydumper"
    ),
)


class _LockFile:
    """Stand in for the script's lock file, which always grants the lock."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_lock_or_exit(self) -> None:
        """Grant the lock."""

    def free_lock(self) -> None:
        """Release the lock."""


def _exit_status(script: _Script, restore: Callable[[], None]) -> tuple[int, Recorder]:
    """Run a restore script's ``main`` over one configured host.

    ``main`` and ``Restorer.run`` are the script's own. The per-host restore
    class, the configuration, the lock file and the logger are stand-ins.

    :param script: The restore script to run.
    :param restore: What the per-host restore does: return, or raise.
    :return: The status the script exited with, and what it logged.
    """
    logger = Recorder()

    class _HostRestore:
        def __init__(self, _server_data: dict[str, str]) -> None:
            pass

        def run(self) -> None:
            restore()

        def notify(self, *_args: object, **_kwargs: object) -> None:
            pass

        def textfile_collector_write_status(
            self, *_args: object, **_kwargs: object
        ) -> None:
            pass

    config = types.SimpleNamespace(
        raw_cfg_data={"ALL_SERVERS": {}},
        server_data_list=[{"BACKUP_TYPE": script.backup_type}],
    )
    stubs: dict[str, object] = {
        "BackupError": _BackupError,
        "LocalConfig": lambda _path: config,
        "LockFile": _LockFile,
        "logging_setup": lambda *_args, **_kwargs: logger,
        "format_seconds_to_hhmmss": str,
        "time": time,
        "sys": sys,
        script.restore_class: _HostRestore,
    }
    run = payload_method(
        "Restorer", "run", extra_namespace=stubs, payload_path=script.path
    )

    class _Restorer:
        def __init__(self, options: types.SimpleNamespace) -> None:
            self.config_path = options.config_path
            self.backup_type = None
            self.backup_path = None
            self.logger = None

        def run(self) -> object:
            return run(self)

    main = load_function(
        "main",
        extra_namespace={
            **stubs,
            "preflight_check": lambda: None,
            "parse_args": lambda: types.SimpleNamespace(config_path="restore.yml"),
            "Restorer": _Restorer,
        },
        payload_path=script.path,
    )
    try:
        main()
    except SystemExit as exc:
        if exc.code is None:
            return 0, logger
        return cast("int", exc.code), logger
    return 0, logger


@pytest.mark.parametrize("script", _SCRIPTS)
class TestExitStatus:
    """Pin the exit status of each restore script to the restore's outcome."""

    def test_a_restore_that_failed_for_its_host_exits_non_zero(
        self, script: _Script
    ) -> None:
        """Pin the exit status after the failure the script logs and survives."""

        def restore() -> None:
            raise _BackupError("stop mysqld failed")

        status, logger = _exit_status(script, restore)

        assert status == FAILED_EXIT_STATUS
        assert "stop mysqld failed" in logger.errors

    def test_an_unexpected_error_exits_non_zero(self, script: _Script) -> None:
        """Pin the exit status after an error the script did not anticipate."""

        def restore() -> None:
            raise RuntimeError("boom")

        status, logger = _exit_status(script, restore)

        assert status == FAILED_EXIT_STATUS
        assert logger.errors

    def test_a_restore_that_succeeded_exits_zero(self, script: _Script) -> None:
        """Pin a successful restore to exit status 0."""
        status, logger = _exit_status(script, lambda: None)

        assert status == 0
        assert not logger.errors


@dataclass
class _Host:
    """Stand in for a host whose MySQL answers a number of probes after a stop.

    :param answers: How many probes MySQL answers before it refuses
        connections; ``None`` when it never stops answering.
    """

    answers: int | None
    probes: int = 0
    sleeps: list[int] = field(default_factory=list)

    def query(self, _sql: str) -> None:
        """Answer one probe, or refuse the connection once MySQL has stopped.

        :raises _OperationalError: When MySQL has stopped.
        """
        self.probes += 1
        if self.answers is not None and self.probes > self.answers:
            raise _OperationalError


def _restore_on(host: _Host) -> types.SimpleNamespace:
    """Return an object carrying the XtraBackup script's real ``signal_mysqld``.

    The init command exits 0 and the waits between probes are recorded on
    ``host`` rather than slept.

    :param host: The host whose MySQL the stop command is sent to.
    :return: The object to call ``signal_mysqld`` on.
    """
    proc = types.SimpleNamespace(returncode=0, stderr="", wait=lambda: 0)
    signal_mysqld = payload_method(
        "Xtrabackup",
        "signal_mysqld",
        extra_namespace={
            "BackupError": _BackupError,
            "OperationalError": _OperationalError,
            "subprocess": types.SimpleNamespace(
                Popen=lambda *_args, **_kwargs: proc, PIPE=-1
            ),
            "time": types.SimpleNamespace(sleep=host.sleeps.append),
        },
        payload_path=RESTORE_PAYLOAD_PATH,
    )
    restore = types.SimpleNamespace(
        custom_mysql_init_command="/usr/local/bin/mysql-init",
        logger=Recorder(),
        _query_mysql=host.query,
    )
    restore.signal_mysqld = lambda action: signal_mysqld(restore, action)
    return restore


class TestStopWait:
    """Pin what the XtraBackup restore concludes from its probes after a stop."""

    def test_a_server_that_keeps_answering_fails_the_restore(self) -> None:
        """Pin the failure once the whole wait has passed with MySQL answering."""
        host = _Host(answers=None)
        max_attempts = cast(
            "int", load_constant("MAX_ATTEMPTS", payload_path=RESTORE_PAYLOAD_PATH)
        )
        interval = load_constant("SLEEP_INTERVAL", payload_path=RESTORE_PAYLOAD_PATH)

        with pytest.raises(_BackupError, match="Failed to stop MySQL"):
            _restore_on(host).signal_mysqld("stop")

        assert host.sleeps == [interval] * max_attempts

    def test_a_refused_probe_means_mysql_has_stopped(self) -> None:
        """Pin a refused connection as the sign that the stop worked."""
        host = _Host(answers=0)

        assert _restore_on(host).signal_mysqld("stop") == 0
        assert host.sleeps == []

    def test_a_server_that_stops_during_the_wait_is_not_a_failure(self) -> None:
        """Pin that a slow stop inside the wait still counts as stopped."""
        answered = 3
        host = _Host(answers=answered)

        assert _restore_on(host).signal_mysqld("stop") == 0
        assert len(host.sleeps) == answered

    def test_no_restore_step_runs_after_a_failed_stop(self) -> None:
        """Pin that nothing is copied into the data directory after a failed stop."""
        steps: list[str] = []
        restore = _restore_on(_Host(answers=None))
        run = payload_method(
            "Xtrabackup",
            "run",
            extra_namespace={
                "BackupError": _BackupError,
                "time": time,
                "resource": types.SimpleNamespace(
                    RLIMIT_NOFILE=0, setrlimit=lambda *_args: None
                ),
            },
            payload_path=RESTORE_PAYLOAD_PATH,
        )
        restore.continue_replication = False
        restore.logging_dir = "/var/log/restore"
        restore.pre_script = None
        restore.backup_source = "/backups/full"
        restore.is_stream = False
        restore.datadir = "/var/lib/mysql"
        restore.from_commandline = False
        restore._backup_chain = lambda: ["/backups/full"]
        restore._check_backup_chain = lambda _chain: None
        restore.backup_info = lambda: setattr(restore, "backup_is_incremental", False)
        for step in ("copy_back", "decrypt_decompress", "prepare_backup"):
            setattr(restore, step, lambda *_args, name=step: steps.append(name))

        with pytest.raises(_BackupError, match="Failed to stop MySQL"):
            run(restore)

        assert steps == []
