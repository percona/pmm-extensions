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

"""Assert localhost my.cnf/certs copy soft-fails into a warning outcome.

A ``BackupError`` from ``_query_mysql`` (via ``get_default_mycnf`` /
``get_datadir``) must not abort ``run``, must log a prefixed warning, and must
finish as ``STATUS_WARNING`` with path/size still reported. Per-file copy
failures are recorded the same way. Zero-size warning text stays unchanged when
the copy succeeds.
"""

from __future__ import annotations

import shutil
import types
import urllib.parse
from typing import Any, TYPE_CHECKING

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_instance,
    Recorder,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

STATUS_OK = 0
STATUS_WARNING = 1

#: Query error ``backup_mycnf`` surfaces when ``_query_mysql`` fails for basedir.
_METADATA_COPY_QUERY_ERROR = (
    'Error running query "SELECT @@basedir, @@datadir": '
    "(1045, \"Access denied for user 'root'@'localhost' (using password: YES)\")"
)

_SKIPPED_WARNING = f"Skipped my.cnf/cert copy: {_METADATA_COPY_QUERY_ERROR}"

_COPY_METHODS = (
    "run",
    "backup_mycnf",
    "backup_certs",
    "get_default_mycnf",
    "get_datadir",
    "get_included_config",
    "notify",
)


class _RunProbe:
    """Record GPG passes, notify calls, and logger output from ``run``."""

    def __init__(self) -> None:
        self.gpg_dirs: list[str] = []
        self.gpg_cfgs: list[Mapping[str, object]] = []
        self.notify_calls: list[tuple[int, str]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.report_options: dict[str, Any] = {}
        self.last_backup_dir: str = ""


def _archived_cnf(tmp_path: Path) -> str:
    """Return the name ``backup_mycnf`` gives the my.cnf ``_stage_copy_sources`` writes."""
    return urllib.parse.quote(str(tmp_path / "my.cnf"), safe="")


def _shutil_namespace(*, fail_mycnf: bool = False, fail_certs: bool = False) -> object:
    """Return ``shutil``, or a copy stub that fails my.cnf and/or ``.pem`` copies."""
    if not fail_mycnf and not fail_certs:
        return shutil

    def _copy(src: object, dst: object) -> object:
        path = str(src)
        if fail_certs and path.endswith(".pem"):
            raise OSError("Permission denied")
        if fail_mycnf and path.endswith(".cnf"):
            raise OSError("Permission denied")
        return shutil.copy(src, dst)

    return types.SimpleNamespace(copy=_copy, Error=shutil.Error)


def _extra_namespace(
    probe: _RunProbe,
    *,
    backup_size: str,
    size_mb: float,
    shutil_ns: object,
) -> dict[str, object]:
    """Build the exec namespace extras ``run`` / ``notify`` need."""

    def _record_gpg(dir_path: str, _logger: object, **cfg: object) -> None:
        probe.gpg_dirs.append(dir_path)
        probe.gpg_cfgs.append(cfg)

    def _dir_size(_path: object, unit: str = "text") -> object:
        return size_mb if unit == "mb" else backup_size

    return {
        "time": types.SimpleNamespace(
            time=lambda: 0.0,
            strftime=lambda *_a, **_k: "2026-01-01 00:00:00",
            gmtime=lambda: None,
        ),
        "is_encrypted_dir": lambda *_a, **_k: True,
        "encrypt_dir": _record_gpg,
        "format_seconds_to_hhmmss": lambda _s: "00:00:00",
        "get_dir_size": _dir_size,
        "_write_run_result": lambda *_a, **_k: None,
        "STATUS_OK": STATUS_OK,
        "STATUS_WARNING": STATUS_WARNING,
        "DATETIME_FORMAT": "%Y-%m-%d %H:%M:%S",
        # Synthetic harness class is not the payload ``Backup``; ``object``
        # keeps ``isinstance(self, Backup)`` true so ``notify`` still fills
        # path/size on ``STATUS_WARNING``.
        "Backup": object,
        "shutil": shutil_ns,
        "quote": urllib.parse.quote,
        "RsyncUploadProvider": object(),
        "S3UploadProvider": object(),
        "GSUploadProvider": object(),
    }


def _seed_instance(
    inst: object,
    probe: _RunProbe,
    tmp_path: Path,
    *,
    enc_gpg: bool,
    post_run_encrypt: bool,
    dir_encrypt_config: Mapping[str, object] | None,
) -> None:
    """Attach logger, gates, and stubs so ``run`` reaches the copy/notify path."""
    recorder = Recorder()
    inst.logger = recorder
    probe.warnings = recorder.warnings
    probe.errors = recorder.errors
    last_backup_dir = tmp_path / "backups" / "host1"
    last_backup_dir.mkdir(parents=True)
    probe.last_backup_dir = str(last_backup_dir)
    inst.enc_aes = False
    inst.enc_gpg = enc_gpg
    inst.post_run_encrypt = post_run_encrypt
    inst.hardlink = False
    inst.max_copies = 0
    inst.only_if_running_replica = False
    inst.only_if_read_only = False
    inst.check_disk_space = False
    inst.is_pxc = False
    inst.host = "localhost"
    inst.upload_type = []
    inst.backup_dir = str(tmp_path / "xtrabackup_tmpdir")
    inst.last_backup_dir = str(last_backup_dir)
    inst.dir_encrypt_config = {} if dir_encrypt_config is None else dir_encrypt_config
    inst.backup_code = "X"
    inst.is_replica = False
    inst.report_options = {"options": {}, "status": "error", "size_mb": 0}
    inst.encrypt_files_aes256 = lambda *_a, **_k: None
    inst._decrypt_metadata_file = lambda *_a, **_k: None
    inst._run_backup_cmd = lambda: None
    inst._is_track_changed_pages = lambda: False
    inst._purge_old_backups = lambda: None
    inst._save_disk_space = lambda: None
    inst.textfile_collector_notify = lambda *_a, **_k: None


def _stage_copy_sources(tmp_path: Path) -> tuple[str, str]:
    """Create a readable my.cnf and a datadir ``.pem``; return their paths."""
    cnf = tmp_path / "my.cnf"
    cnf.write_text("[mysqld]\n")
    datadir = tmp_path / "datadir"
    datadir.mkdir()
    pem = datadir / "server-cert.pem"
    pem.write_text("cert\n")
    return str(cnf), str(datadir)


def _configure_copy(
    inst: object,
    backup_error: type[Exception],
    tmp_path: Path,
    *,
    fail_metadata_copy: bool,
) -> None:
    """Arm the localhost my.cnf/certs block for success or a query soft-fail.

    Per-file copy failures are armed via ``_shutil_namespace`` in ``_run_backup``.
    """
    inst.incremental = False
    inst.all_cnf_files = []
    if fail_metadata_copy:
        inst.mycnf_path = None

        def _fail_query(sql: str, *_args: object, **_kwargs: object) -> object:
            raise backup_error(
                f'Error running query "{sql}": '
                "(1045, \"Access denied for user 'root'@'localhost' "
                '(using password: YES)")'
            )

        inst._query_mysql = _fail_query
        return

    cnf, datadir = _stage_copy_sources(tmp_path)
    inst.mycnf_path = cnf
    inst._query_mysql = lambda *_a, **_k: {
        "@@basedir": str(tmp_path),
        "@@datadir": datadir,
    }


def _wrap_notify(inst: object, probe: _RunProbe) -> None:
    """Record ``notify`` status/message while still running the real method."""
    real_notify = inst.notify

    def _notify(status: int, msg: str, *args: object, **kwargs: object) -> None:
        probe.notify_calls.append((status, msg))
        real_notify(status, msg, *args, **kwargs)

    inst.notify = _notify


def _run_backup(
    tmp_path: Path,
    *,
    fail_metadata_copy: bool = False,
    fail_mycnf: bool = False,
    fail_certs: bool = False,
    backup_size: str = "1 MB",
    size_mb: float = 1.0,
    enc_gpg: bool = True,
    post_run_encrypt: bool = True,
    dir_encrypt_config: Mapping[str, object] | None = None,
) -> _RunProbe:
    """Drive ``run`` through the my.cnf/certs block and end-of-run notify.

    :param tmp_path: Pytest temp directory used for the backup paths.
    :param fail_metadata_copy: When ``True``, ``_query_mysql`` raises so the failure
        boundary is ``get_default_mycnf``.
    :param fail_mycnf: When ``True``, ``shutil.copy`` raises for ``.cnf`` sources.
    :param fail_certs: When ``True``, ``shutil.copy`` raises for ``.pem`` sources.
    :param backup_size: Text size ``get_dir_size`` returns for the notify branch.
    :param size_mb: Numeric size ``notify`` stores into ``report_options``.
    :param enc_gpg: Whether GPG encryption is enabled on the instance.
    :param post_run_encrypt: Whether GPG runs after the backup finishes.
    :param dir_encrypt_config: Mapping spread into ``encrypt_dir`` as kwargs.
    :return: Probe of warnings, notify calls, GPG activity, and report_options.
    """
    probe = _RunProbe()
    inst, backup_error, _ = payload_instance(
        _COPY_METHODS,
        extra_namespace=_extra_namespace(
            probe,
            backup_size=backup_size,
            size_mb=size_mb,
            shutil_ns=_shutil_namespace(fail_mycnf=fail_mycnf, fail_certs=fail_certs),
        ),
    )
    _seed_instance(
        inst,
        probe,
        tmp_path,
        enc_gpg=enc_gpg,
        post_run_encrypt=post_run_encrypt,
        dir_encrypt_config=dir_encrypt_config,
    )
    _configure_copy(
        inst,
        backup_error,
        tmp_path,
        fail_metadata_copy=fail_metadata_copy,
    )
    _wrap_notify(inst, probe)
    inst.run()
    probe.report_options = dict(inst.report_options)
    return probe


def test_query_failure_warns_notifies_warning_and_still_runs_gpg(
    tmp_path: Path,
) -> None:
    """Assert a ``_query_mysql`` failure soft-fails into STATUS_WARNING with GPG."""
    recipient = {"encryption recipient": "ops@example.com"}
    probe = _run_backup(
        tmp_path,
        fail_metadata_copy=True,
        dir_encrypt_config=recipient,
    )
    assert probe.warnings == [_SKIPPED_WARNING]
    assert probe.notify_calls == [
        (
            STATUS_WARNING,
            "WARN my.cnf/cert copy skipped. Duration: 00:00:00. Size: 1 MB.",
        )
    ]
    assert probe.report_options["options"]["local_path"] == str(
        tmp_path / "backups" / "host1"
    )
    assert probe.report_options["size_mb"] == 1.0
    # Only STATUS_OK maps report_options["status"] to "success"; WARNING keeps
    # the initial "error" so consumers still treat the run as non-success.
    assert probe.report_options["status"] == "error"
    assert len(probe.gpg_dirs) == 1
    assert probe.gpg_cfgs == [recipient]


def test_per_file_mycnf_copy_failure_notifies_warning(tmp_path: Path) -> None:
    """Assert an OSError copying my.cnf records the skip and warns at notify."""
    probe = _run_backup(
        tmp_path, fail_mycnf=True, enc_gpg=False, post_run_encrypt=False
    )
    assert probe.errors == ["Copy of my.cnf failed: Permission denied"]
    assert probe.notify_calls == [
        (
            STATUS_WARNING,
            "WARN my.cnf/cert copy skipped. Duration: 00:00:00. Size: 1 MB.",
        )
    ]
    assert probe.report_options["options"]["local_path"] == str(
        tmp_path / "backups" / "host1"
    )


def test_per_file_cert_copy_failure_warns_and_still_runs_gpg(tmp_path: Path) -> None:
    """Assert a ``.pem`` copy OSError alone still warns and keeps post-run GPG."""
    recipient = {"encryption recipient": "ops@example.com"}
    probe = _run_backup(
        tmp_path,
        fail_certs=True,
        dir_encrypt_config=recipient,
    )
    assert probe.warnings == ["Copy of cert files failed: Permission denied"]
    assert probe.notify_calls == [
        (
            STATUS_WARNING,
            "WARN my.cnf/cert copy skipped. Duration: 00:00:00. Size: 1 MB.",
        )
    ]
    assert (tmp_path / "backups" / "host1" / _archived_cnf(tmp_path)).is_file()
    assert not (tmp_path / "backups" / "host1" / "server-cert.pem").exists()
    assert len(probe.gpg_dirs) == 1
    assert probe.gpg_cfgs == [recipient]


def test_zero_size_warning_unchanged_when_copy_succeeds(tmp_path: Path) -> None:
    """Assert the zero-size WARN text is unchanged after a successful copy."""
    probe = _run_backup(
        tmp_path,
        backup_size="0 MB",
        size_mb=0,
        enc_gpg=False,
        post_run_encrypt=False,
    )
    backup = tmp_path / "backups" / "host1"
    assert (backup / _archived_cnf(tmp_path)).is_file()
    assert (backup / "server-cert.pem").is_file()
    assert probe.warnings == []
    assert probe.notify_calls == [
        (STATUS_WARNING, "WARN Backup completed without data. Duration: 00:00:00.")
    ]
    # Any WARNING fills path/size, zero-size included; status stays "error".
    assert probe.report_options["options"]["local_path"] == str(backup)
    assert probe.report_options["size_mb"] == 0
    assert probe.report_options["status"] == "error"


def test_skip_takes_precedence_over_zero_size(tmp_path: Path) -> None:
    """Assert a skipped copy wins the notify message when size is also zero."""
    probe = _run_backup(
        tmp_path,
        fail_metadata_copy=True,
        backup_size="0 MB",
        size_mb=0,
        enc_gpg=False,
        post_run_encrypt=False,
    )
    assert probe.warnings == [_SKIPPED_WARNING]
    assert probe.notify_calls == [
        (
            STATUS_WARNING,
            "WARN my.cnf/cert copy skipped. Duration: 00:00:00. Size: 0 MB.",
        )
    ]
    assert probe.report_options["options"]["local_path"] == str(
        tmp_path / "backups" / "host1"
    )
    assert probe.report_options["size_mb"] == 0
    assert probe.report_options["status"] == "error"


def test_successful_copy_path_notifies_ok(tmp_path: Path) -> None:
    """Assert a full my.cnf/certs copy finishes STATUS_OK with the artifacts."""
    probe = _run_backup(tmp_path, enc_gpg=False, post_run_encrypt=False)
    backup = tmp_path / "backups" / "host1"
    assert (backup / _archived_cnf(tmp_path)).is_file()
    assert (backup / "server-cert.pem").is_file()
    assert probe.warnings == []
    assert probe.notify_calls == [
        (STATUS_OK, "OK Backup complete. Duration: 00:00:00. Size: 1 MB.")
    ]
