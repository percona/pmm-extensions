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

"""Tests for binlog_payload's xbcrypt/AES-256 encryption path.

Binlog encryption runs on the upload tmpdir (mysqlbinlog has no inline
``--encrypt``), so these pin ``Upload._encrypt`` applying xbcrypt for AES-256,
skipping GPG for dual, and leaving the GPG-only path unchanged.
"""

import logging
import os
import shutil
import types
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

import pytest

from app.extensions.apps.mysql_backups.forms import (
    ENCRYPTION_FORMAT_BY_PASSES,
    EncryptionFormat,
)
from tests.app.extensions.apps.mysql_backups.conftest import BINLOG_PAYLOAD_PATH
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_instance,
    payload_method,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    XBCRYPT_BIN as _XBCRYPT_BIN,
)

_PATH = BINLOG_PAYLOAD_PATH
_FORMATS = cast(
    "tuple[str, ...]", load_constant("ENCRYPTION_FORMATS", payload_path=_PATH)
)
_resolve_encryption = load_function("_resolve_encryption", payload_path=_PATH)
_KEYFILE = "/keys/aes.key"
_ENCRYPT_METHODS = ("encrypt_files_aes256", "_run_encrypt_file_aes256", "_run_xbcrypt")


class TestIsEncryptedDirAes256:
    """Assert ``is_encrypted_dir(method='aes256')`` verifies by ``.xbcrypt`` extension."""

    def _fn(self):
        return load_function("is_encrypted_dir", payload_path=_PATH)

    def test_all_files_encrypted_returns_true(self, tmp_path: Path) -> None:
        """Assert a directory where every data file ends in ``.xbcrypt`` verifies True."""
        (tmp_path / "binlog.000001.gz.xbcrypt").write_text("x")
        assert self._fn()(str(tmp_path), method="aes256") is True

    def test_plaintext_straggler_returns_false(self, tmp_path: Path) -> None:
        """Assert a single plaintext non-excluded file fails verification."""
        (tmp_path / "binlog.000001.gz.xbcrypt").write_text("x")
        (tmp_path / "binlog.000002.gz").write_text("plain")
        assert self._fn()(str(tmp_path), method="aes256") is False

    def test_excluded_metadata_left_plaintext_still_true(self, tmp_path: Path) -> None:
        """Assert plaintext metadata PMM Extensions reads post-backup do not fail verification."""
        (tmp_path / "binlog.000001.gz.xbcrypt").write_text("x")
        for name in (
            "md5sum",
            ".uploadme",
            "xtrabackup_info",
            "xtrabackup_checkpoints",
        ):
            (tmp_path / name).write_text("meta")
        assert self._fn()(str(tmp_path), method="aes256") is True


class TestEncryptFilesAes256:
    """Assert the recursive xbcrypt encrypt pass targets the right files/commands."""

    def test_builds_expected_xbcrypt_command(self, tmp_path: Path) -> None:
        """Assert each file is encrypted with the AES256 flags and ``.xbcrypt`` output."""
        target = tmp_path / "binlog.000001.gz"
        target.write_text("data")
        inst, _, calls = payload_instance(_ENCRYPT_METHODS, payload_path=_PATH)
        inst.encrypt_files_aes256(str(tmp_path))
        assert calls == [
            [
                _XBCRYPT_BIN,
                "--encrypt-algo=AES256",
                "--encrypt-key-file=/keys/aes.key",
                f"--input={target}",
                f"--output={target}.xbcrypt",
            ]
        ]

    def test_uses_xtrabackup_aes256_attribute(self, tmp_path: Path) -> None:
        """Assert binlog encrypt reads the historical ``xtrabackup_aes256`` key path."""
        target = tmp_path / "binlog.000001.gz"
        target.write_text("data")
        inst, _, calls = payload_instance(_ENCRYPT_METHODS, payload_path=_PATH)
        inst.xtrabackup_aes256 = "/keys/binlog.key"
        inst.encrypt_files_aes256(str(tmp_path))
        assert "--encrypt-key-file=/keys/binlog.key" in calls[0]

    def test_skips_already_encrypted_and_excluded(self, tmp_path: Path) -> None:
        """Assert ``.xbcrypt`` files and excluded metadata are not (re-)encrypted."""
        (tmp_path / "done.gz.xbcrypt").write_text("enc")
        (tmp_path / "md5sum").write_text("meta")
        (tmp_path / "xtrabackup_checkpoints").write_text("meta")
        (tmp_path / "binlog.000001.gz").write_text("plain")
        inst, _, calls = payload_instance(_ENCRYPT_METHODS, payload_path=_PATH)
        inst.encrypt_files_aes256(str(tmp_path))
        inputs = [a for cmd in calls for a in cmd if a.startswith("--input=")]
        assert inputs == [f"--input={tmp_path / 'binlog.000001.gz'}"]

    def test_raises_backuperror_on_nonzero(self, tmp_path: Path) -> None:
        """Assert a failed xbcrypt run raises ``BackupError``."""
        (tmp_path / "binlog.000001.gz").write_text("data")
        inst, backup_error, _ = payload_instance(
            _ENCRYPT_METHODS, returncode=1, payload_path=_PATH
        )
        with pytest.raises(backup_error):
            inst.encrypt_files_aes256(str(tmp_path))


class _RecordingThreadPool:
    """Stand in for ``multiprocessing.pool.ThreadPool`` that runs synchronously."""

    def __init__(self, processes: int) -> None:
        self.processes = processes

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def map(
        self, func: Callable[[object], object], iterable: Iterable[object]
    ) -> list[object]:
        """Apply ``func`` to each item synchronously."""
        return [func(item) for item in iterable]


class TestEncryptFilesAes256WorkerCount:
    """Assert the encrypt pass bounds its ``ThreadPool`` size by CPU cores."""

    @pytest.mark.parametrize(
        ("cpu_count", "expected"),
        [(8, 5), (2, 2), (None, 5)],
    )
    def test_pool_size_bounded_by_cpu_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cpu_count: int | None,
        expected: int,
    ) -> None:
        """Assert the pool never exceeds available cores, capped at 5 by default."""
        monkeypatch.setattr(os, "cpu_count", lambda: cpu_count)
        (tmp_path / "binlog.000001.gz").write_text("data")
        pool_sizes: list[int] = []

        class _CapturingThreadPool(_RecordingThreadPool):
            def __init__(self, processes: int) -> None:
                pool_sizes.append(processes)
                super().__init__(processes)

        inst, _, _ = payload_instance(
            _ENCRYPT_METHODS,
            payload_path=_PATH,
            extra_namespace={
                "thread_pool": types.SimpleNamespace(ThreadPool=_CapturingThreadPool)
            },
        )
        inst.encrypt_files_aes256(str(tmp_path))
        assert pool_sizes == [expected]


class TestResolveEncryption:
    """Assert the resolver maps a config to the passes that must run."""

    @pytest.mark.parametrize(
        ("selected", "expected"),
        [
            ("none", ("none", False, False)),
            ("gpg", ("gpg", False, True)),
            ("aes256", ("aes256", True, False)),
            ("dual", ("dual", True, True)),
        ],
    )
    def test_explicit_format_wins_over_the_fields(
        self, selected: str, expected: tuple[str, bool, bool]
    ) -> None:
        """Assert an explicit format decides, with both legacy fields still set."""
        assert _resolve_encryption(selected, _KEYFILE, gpg=True) == expected

    def test_vocabulary_matches_the_form(self) -> None:
        """Assert the payload tuple equals the form's, value for value."""
        assert tuple(fmt.value for fmt in ENCRYPTION_FORMAT_BY_PASSES) == _FORMATS
        assert {fmt.value for fmt in EncryptionFormat} == set(_FORMATS)


def _upload_encrypt(
    *,
    encrypt: bool,
    post_run_encrypt: bool,
    aes256: bool,
    tmpdir: bool = True,
):
    """Drive ``Upload._encrypt`` with recorders for GPG and AES passes."""
    gpg_dirs: list[str] = []
    aes_dirs: list[str] = []
    warnings: list[str] = []
    inst, _, _ = payload_instance(
        ("_encrypt",),
        payload_path=_PATH,
        extra_namespace={
            "encrypt_dir": lambda dir_path, _logger, **_cfg: gpg_dirs.append(
                str(dir_path)
            ),
            "is_encrypted_dir": lambda *_a, **_k: True,
            "Path": Path,
        },
    )
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda msg, *_a, **_k: warnings.append(msg),
    )
    inst.encrypt = encrypt
    inst.post_run_encrypt = post_run_encrypt
    inst.xtrabackup_aes256 = _KEYFILE if aes256 else False
    inst.encrypt_using_tmpdir = tmpdir
    inst.dir_encrypt_config = {}
    inst.paths = [{"source": "/backups/host1", "tmpdir": "/tmp/enc/host1"}]
    inst.encrypt_files_aes256 = aes_dirs.append
    return inst, gpg_dirs, aes_dirs, warnings


class TestUploadEncrypt:
    """Assert upload-time encrypt follows the resolved format for binlogs."""

    def test_gpg_encrypts_the_tmpdir_copy(self) -> None:
        """Assert a GPG selection encrypts the upload tmpdir."""
        inst, gpg_dirs, aes_dirs, _ = _upload_encrypt(
            encrypt=True, post_run_encrypt=False, aes256=False
        )
        inst._encrypt()
        assert gpg_dirs == ["/tmp/enc/host1"]
        assert aes_dirs == []

    def test_aes256_runs_xbcrypt_on_the_tmpdir(self) -> None:
        """Assert AES-256 encrypts the upload copy with xbcrypt (not an early return)."""
        inst, gpg_dirs, aes_dirs, warnings = _upload_encrypt(
            encrypt=False, post_run_encrypt=False, aes256=True
        )
        inst._encrypt()
        assert aes_dirs == ["/tmp/enc/host1"]
        assert gpg_dirs == []
        assert warnings == []

    def test_dual_runs_aes_and_skips_gpg(self) -> None:
        """Assert ``dual`` applies AES-256 only on the upload path."""
        inst, gpg_dirs, aes_dirs, _ = _upload_encrypt(
            encrypt=True, post_run_encrypt=True, aes256=True
        )
        # AES branch returns before GPG; dual still carries xtrabackup_aes256.
        inst._encrypt()
        assert aes_dirs == ["/tmp/enc/host1"]
        assert gpg_dirs == []

    def test_no_encryption_warns_loudly(self) -> None:
        """Assert an unencrypted upload is announced rather than passing silently."""
        inst, gpg_dirs, aes_dirs, warnings = _upload_encrypt(
            encrypt=False, post_run_encrypt=False, aes256=False
        )
        inst._encrypt()
        assert gpg_dirs == []
        assert aes_dirs == []
        assert warnings == ["UPLOADING UNENCRYPTED BACKUP!!!"]


class TestUploadRunCleansTmpdirOnEncryptFailure:
    """Assert ``Upload.run`` removes the upload tmpdir when encrypt aborts."""

    def test_partial_aes_failure_still_removes_tmpdir(self, tmp_path: Path) -> None:
        """Assert a raised encrypt leaves no plaintext sibling in the upload tmpdir.

        ``pool.map`` aborts the rest of a chunk on the first raise; without a
        ``finally`` the tmpdir would keep whatever plaintext (and partial
        ``.xbcrypt``) files were already written.
        """
        tmpdir = tmp_path / "enc"
        tmpdir.mkdir()
        (tmpdir / "binlog.000001").write_text("plain")
        (tmpdir / "binlog.000002.xbcrypt").write_text("partial")

        inst, backup_error, _ = payload_instance(
            ("run", "_cleanup"),
            payload_path=_PATH,
            extra_namespace={
                "shutil": shutil,
                "time": types.SimpleNamespace(time=lambda: 0.0),
                "format_seconds_to_hhmmss": lambda _s: "00:00:00",
                "MSPAction": types.SimpleNamespace(UPLOAD="upload"),
            },
        )
        inst.full_backup_path = tmp_path / "missing"
        inst.paths = [{"source": str(tmp_path / "src"), "tmpdir": str(tmpdir)}]
        inst.encrypt_using_tmpdir = True
        inst.upload_type = "rsync"
        inst.backup_type = "binlog"
        inst._copy_to_tmpdir = lambda: None

        def _raise_encrypt() -> None:
            raise backup_error("AES-256 verify failed")

        inst._encrypt = _raise_encrypt
        inst._upload = lambda: None
        inst.textfile_collector_write_status = lambda *_a, **_k: None

        with pytest.raises(backup_error, match="AES-256 verify failed"):
            inst.run()

        assert not tmpdir.exists()


class _StubBase:
    """Stand in for ``Backup`` so ``Upload.__init__`` can run without one."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.logging_dir = Path("/tmp")
        self.alias = "db1"
        self.report_options = {"encryption": "none"}
        self.cdba_report = {}


def _upload_init(tmp_path: Path, **settings: object):
    """Run the real ``Upload.__init__`` and return the object it configured."""
    logging_dir = tmp_path / "logs"
    logging_dir.mkdir()
    init = payload_method(
        "Upload",
        "__init__",
        payload_path=_PATH,
        extra_namespace={
            "super": _StubBase,
            "upload_logging_setup": lambda *_a, **_k: logging.getLogger("upload-test"),
            "_resolve_encryption": _resolve_encryption,
            "Path": Path,
            "BACKUP_TYPES": {"B": "binlog", "M": "mydumper", "X": "xtrabackup"},
            "DEFAULT_LOGGING_DIR": str(logging_dir),
        },
    )
    inst = types.SimpleNamespace(
        alias="db1",
        backup_code="B",
        logging_dir=logging_dir,
        report_options={"encryption": "none"},
        cdba_report={},
    )
    init(
        inst,
        "S3",
        lambda *_a: object(),
        {
            "BACKUP_TYPE": "B",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "LOGGING_DIR": str(logging_dir),
            **settings,
        },
        logging.getLogger("test"),
    )
    inst.log_stream.close()
    return inst


class TestUploadInitResolvesEncryption:
    """Assert ``Upload.__init__`` derives encryption state from the format."""

    def test_aes256_stamps_report_and_drops_gpg(self, tmp_path: Path) -> None:
        """Assert AES-256 keeps the key file, drops GPG timings, and reports aes256."""
        inst = _upload_init(
            tmp_path,
            ENCRYPTION_FORMAT="aes256",
            ENCRYPT=True,
            POST_RUN_ENCRYPT=True,
            XTRABACKUP_AES256_KEYFILE=_KEYFILE,
        )
        assert inst.xtrabackup_aes256 == _KEYFILE
        assert inst.encrypt is False
        assert inst.post_run_encrypt is False
        assert inst.report_options["encryption"] == "aes256"

    def test_gpg_ignores_a_stale_key_file(self, tmp_path: Path) -> None:
        """Assert a GPG format keeps its timings and drops a leftover key file."""
        inst = _upload_init(
            tmp_path,
            ENCRYPTION_FORMAT="gpg",
            ENCRYPT=True,
            POST_RUN_ENCRYPT=False,
            XTRABACKUP_AES256_KEYFILE=_KEYFILE,
        )
        assert inst.xtrabackup_aes256 is False
        assert inst.encrypt is True
        assert inst.report_options["encryption"] == "gpg"
