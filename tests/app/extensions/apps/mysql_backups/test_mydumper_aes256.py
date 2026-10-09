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

"""Tests for mydumper_payload's xbcrypt/AES-256 encryption path.

Mirrors ``test_xtrabackup_aes256_encrypt`` / ``test_xtrabackup_encryption_format``
against the mydumper payload: post-run encrypt, verification by ``.xbcrypt``
suffix, format resolution, and upload skipping a second AES pass.
"""

import logging
import os
import types
from pathlib import Path
from typing import cast

import pytest

from app.extensions.apps.mysql_backups.forms import (
    ENCRYPTION_FORMAT_BY_PASSES,
    EncryptionFormat,
)
from tests.app.extensions.apps.mysql_backups.conftest import MYDUMPER_PAYLOAD_PATH
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_instance,
    payload_method,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    XBCRYPT_BIN as _XBCRYPT_BIN,
)

_PATH = MYDUMPER_PAYLOAD_PATH
_FORMATS = cast(
    "tuple[str, ...]", load_constant("ENCRYPTION_FORMATS", payload_path=_PATH)
)
_resolve_encryption = load_function("_resolve_encryption", payload_path=_PATH)
_KEYFILE = "/keys/aes.key"
_ENCRYPT_METHODS = ("encrypt_files_aes256", "_run_encrypt_file_aes256", "_run_xbcrypt")
_null_logger = logging.getLogger("mydumper-aes-test")
_null_logger.addHandler(logging.NullHandler())


def _is_encrypted_dir():
    return load_function("is_encrypted_dir", payload_path=_PATH)


class TestIsEncryptedDirAes256:
    """Assert ``is_encrypted_dir(method='aes256')`` verifies by ``.xbcrypt`` extension."""

    def test_all_files_encrypted_returns_true(self, tmp_path: Path) -> None:
        """Assert a directory where every data file ends in ``.xbcrypt`` verifies True."""
        (tmp_path / "table.sql.xbcrypt").write_text("x")
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "rows.sql.xbcrypt").write_text("x")
        assert _is_encrypted_dir()(tmp_path, _null_logger, method="aes256") is True

    def test_plaintext_straggler_returns_false(self, tmp_path: Path) -> None:
        """Assert a single plaintext non-excluded file fails verification."""
        (tmp_path / "table.sql.xbcrypt").write_text("x")
        (tmp_path / "rows.sql").write_text("plaintext")
        assert _is_encrypted_dir()(tmp_path, _null_logger, method="aes256") is False

    def test_unknown_method_raises(self, tmp_path: Path) -> None:
        """Assert an unrecognized method fails fast instead of silently using gpg."""
        with pytest.raises(Exception, match="Unknown encryption method"):
            _is_encrypted_dir()(tmp_path, _null_logger, method="AES256")

    def test_excluded_metadata_left_plaintext_still_true(self, tmp_path: Path) -> None:
        """Assert plaintext metadata PMM Extensions reads post-backup do not fail verification."""
        (tmp_path / "table.sql.xbcrypt").write_text("x")
        for name in ("md5sum", ".uploadme", "metadata"):
            (tmp_path / name).write_text("meta")
        assert _is_encrypted_dir()(tmp_path, _null_logger, method="aes256") is True


class TestEncryptFilesAes256:
    """Assert the recursive xbcrypt encrypt pass targets the right files/commands."""

    def test_builds_expected_xbcrypt_command(self, tmp_path: Path) -> None:
        """Assert each file is encrypted with the AES256 flags and ``.xbcrypt`` output."""
        target = tmp_path / "table.sql"
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

    def test_skips_already_encrypted_and_excluded(self, tmp_path: Path) -> None:
        """Assert ``.xbcrypt`` files and excluded metadata are not (re-)encrypted."""
        (tmp_path / "done.sql.xbcrypt").write_text("enc")
        (tmp_path / "md5sum").write_text("meta")
        (tmp_path / ".uploadme").write_text("meta")
        (tmp_path / "metadata").write_text("meta")
        (tmp_path / "rows.sql").write_text("plain")
        inst, _, calls = payload_instance(_ENCRYPT_METHODS, payload_path=_PATH)
        inst.encrypt_files_aes256(str(tmp_path))
        inputs = [a for cmd in calls for a in cmd if a.startswith("--input=")]
        assert inputs == [f"--input={tmp_path / 'rows.sql'}"]

    def test_removes_plaintext_on_success(self, tmp_path: Path) -> None:
        """Assert the plaintext source is unlinked after a successful encrypt."""
        target = tmp_path / "table.sql"
        target.write_text("data")
        inst, _, _ = payload_instance(
            _ENCRYPT_METHODS, returncode=0, payload_path=_PATH
        )
        inst.encrypt_files_aes256(str(tmp_path))
        assert not target.exists()

    def test_raises_backuperror_on_nonzero(self, tmp_path: Path) -> None:
        """Assert a failed xbcrypt run raises ``BackupError``."""
        (tmp_path / "table.sql").write_text("data")
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

    def map(self, func, iterable):
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
        (tmp_path / "table.sql").write_text("data")
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


class _RunProbe:
    """Record which post-backup encryption passes ``run`` performed."""

    def __init__(self) -> None:
        self.aes_dirs: list[object] = []
        self.gpg_dirs: list[object] = []
        self.saved_disk_space = 0


def _run_backup(
    tmp_path: Path, *, enc_aes: bool, enc_gpg: bool, post_run_encrypt: bool
):
    """Run the real ``run`` past its post-backup encryption block."""
    probe = _RunProbe()
    backup_server_dir = tmp_path
    backup_dir = tmp_path / "20260101"
    work_dir = tmp_path / ".20260101.030000.1.partial"
    latest_link = tmp_path / "latest"

    inst, _, _ = payload_instance(
        ("run",),
        payload_path=_PATH,
        extra_namespace={
            "time": types.SimpleNamespace(time=lambda: 0.0),
            "is_encrypted_dir": lambda *_a, **_k: True,
            "encrypt_dir": lambda dir_path, _logger, **_cfg: probe.gpg_dirs.append(
                dir_path
            ),
            "format_seconds_to_hhmmss": lambda _s: "00:00:00",
            "get_dir_size": lambda *_a, **_k: 1,
            "humanize_bytes": lambda _n: "1 MB",
            "_write_run_result": lambda *_a, **_k: None,
            "RsyncUploadProvider": object(),
            "S3UploadProvider": object(),
            "GSUploadProvider": object(),
            "MyDumperUpload": object(),
            "BackupNotifyStatus": types.SimpleNamespace(OK=0),
        },
    )
    inst.enc_aes = enc_aes
    inst.enc_gpg = enc_gpg
    inst.post_run_encrypt = post_run_encrypt
    inst.only_if_running_replica = False
    inst.only_if_read_only = False
    inst.check_disk_space = False
    inst.mydumper_for_schemas = True
    inst.upload_type = []
    inst.backup_server_dir = backup_server_dir
    inst.backup_dir = backup_dir
    inst.work_dir = work_dir
    inst.latest_link = latest_link
    inst.today_str = "20260101"
    inst.last_backup_dir = str(backup_dir)
    inst.dir_encrypt_config = {}
    inst.report_options = {}
    inst.encrypt_files_aes256 = probe.aes_dirs.append
    inst._get_version = lambda: "0.16"
    inst._run_backup_cmd = lambda: None
    inst._reclaim_interrupted_publish = lambda: None
    inst._publish_backup = lambda: backup_dir.mkdir(exist_ok=True)
    inst._purge_old_backups = lambda: None
    inst._save_disk_space = lambda: setattr(
        probe, "saved_disk_space", probe.saved_disk_space + 1
    )
    inst.notify = lambda *_a, **_k: None
    inst.enable_upload = lambda *_a, **_k: None

    inst.run()
    return probe


class TestSaveDiskSpacePrevEncrypted:
    """Assert hardlink gating detects AES-256 prior backups, not only GPG."""

    def _instance(self, tmp_path: Path, *, is_encrypted_dir):
        """Build a ``_save_disk_space`` instance with the encrypt probe stubbed."""
        prev = tmp_path / "20260101"
        prev.mkdir()
        today = tmp_path / "20260102"
        today.mkdir()
        inst, _, _ = payload_instance(
            ("_save_disk_space",),
            payload_path=_PATH,
            extra_namespace={
                "is_encrypted_dir": is_encrypted_dir,
                "datetime": __import__("datetime"),
                "get_dir_dict": lambda *_a, **_k: None,
                "hardlink_dirs": lambda *_a, **_k: {},
            },
        )
        inst.s3_bucket = None
        inst.s3_encrypt = False
        inst.hardlink = True
        inst.encrypt_using_tmpdir = False
        inst.prev_backup_dir = prev
        inst.backup_dir = today
        inst.work_dir = today
        inst.backup_server_dir = tmp_path
        inst.updated_since = 0
        inst.valid_prev_backup_dir = None
        inst.cache_md5 = False
        inst.prev_encrypted = False
        inst.report_options = {}
        return inst, prev

    def test_aes256_previous_backup_disables_hardlinking(self, tmp_path: Path) -> None:
        """Assert a prior ``.xbcrypt`` backup is treated as encrypted."""
        methods: list[str] = []
        real = load_function("is_encrypted_dir", payload_path=_PATH)

        def _recording_is_encrypted_dir(path, logger, method="gpg"):
            methods.append(method)
            return real(path, logger, method=method)

        inst, prev = self._instance(
            tmp_path, is_encrypted_dir=_recording_is_encrypted_dir
        )
        (prev / "table.sql.xbcrypt").write_text("enc")
        inst._save_disk_space()
        assert "aes256" in methods
        assert inst.prev_encrypted is True

    def test_gpg_previous_backup_still_disables_hardlinking(
        self, tmp_path: Path
    ) -> None:
        """Assert a prior GPG backup still trips the gate after the AES probe."""

        def _fake_is_encrypted_dir(_path, _logger, method="gpg"):
            return method == "gpg"

        inst, prev = self._instance(tmp_path, is_encrypted_dir=_fake_is_encrypted_dir)
        (prev / "table.sql.gpg").write_text("enc")
        inst._save_disk_space()
        assert inst.prev_encrypted is True


class TestRunPostBackupPasses:
    """Assert ``run``'s post-backup passes follow the resolved format."""

    def test_gpg_runs_the_post_run_pass_only(self, tmp_path: Path) -> None:
        """Assert a GPG format with post-run timing encrypts the finished directory."""
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=True, post_run_encrypt=True
        )
        assert probe.aes_dirs == []
        assert len(probe.gpg_dirs) == 1
        assert probe.saved_disk_space == 0

    def test_aes256_runs_the_aes_pass_only(self, tmp_path: Path) -> None:
        """Assert an AES-256 format encrypts with xbcrypt and skips GPG."""
        probe = _run_backup(
            tmp_path, enc_aes=True, enc_gpg=False, post_run_encrypt=False
        )
        assert len(probe.aes_dirs) == 1
        assert probe.gpg_dirs == []

    def test_dual_runs_aes_and_skips_gpg(self, tmp_path: Path) -> None:
        """Assert ``dual`` applies AES-256 only, matching XtraBackup."""
        probe = _run_backup(tmp_path, enc_aes=True, enc_gpg=True, post_run_encrypt=True)
        assert len(probe.aes_dirs) == 1
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 0

    def test_none_saves_disk_space(self, tmp_path: Path) -> None:
        """Assert an unencrypted run encrypts nothing and hardlinks instead."""
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=False, post_run_encrypt=False
        )
        assert probe.aes_dirs == []
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 1


def _upload_instance(*, encrypt: bool, post_run_encrypt: bool, aes256: bool):
    """Build an ``Upload`` with resolved state fixed, to drive ``_encrypt`` alone."""
    encrypted: list[str] = []
    warnings: list[str] = []
    inst, _, _ = payload_instance(
        ("_encrypt",),
        payload_path=_PATH,
        extra_namespace={
            "encrypt_dir": lambda dir_path, _logger, **_cfg: encrypted.append(
                str(dir_path)
            )
        },
    )
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda msg, *_a, **_k: warnings.append(msg),
    )
    inst.encrypt = encrypt
    inst.post_run_encrypt = post_run_encrypt
    inst.aes_keyfile = _KEYFILE if aes256 else False
    inst.xtrabackup_aes256 = _KEYFILE if aes256 else False
    inst.encrypt_using_tmpdir = False
    inst.dir_encrypt_config = {}
    inst.paths = [{"source": "/backups/host1", "tmpdir": "/tmp/enc/host1"}]
    return inst, encrypted, warnings


class TestUploadEncrypt:
    """Assert upload skips a second AES pass and keeps the GPG path intact."""

    def test_gpg_encrypts_the_source_directory(self) -> None:
        """Assert a GPG selection still encrypts before upload."""
        inst, encrypted, _ = _upload_instance(
            encrypt=True, post_run_encrypt=False, aes256=False
        )
        inst._encrypt()
        assert encrypted == ["/backups/host1"]

    def test_aes256_skips_the_gpg_pass(self) -> None:
        """Assert AES already applied at backup is not GPG-encrypted on the way out."""
        inst, encrypted, warnings = _upload_instance(
            encrypt=False, post_run_encrypt=False, aes256=True
        )
        inst._encrypt()
        assert encrypted == []
        assert warnings == []

    def test_no_encryption_warns_loudly(self) -> None:
        """Assert an unencrypted upload is announced rather than passing silently."""
        inst, encrypted, warnings = _upload_instance(
            encrypt=False, post_run_encrypt=False, aes256=False
        )
        inst._encrypt()
        assert encrypted == []
        assert warnings == ["UPLOADING UNENCRYPTED BACKUP!!!"]


class _StubBase:
    """Stand in for ``Backup`` so ``Upload.__init__`` can run without one."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.logging_dir = Path("/tmp")
        self.alias = "db1"
        self.report_options = {"encryption": "none"}


def _upload_init(tmp_path: Path, **settings: object):
    """Run the real ``Upload.__init__`` and return the object it configured."""
    init = payload_method(
        "Upload",
        "__init__",
        payload_path=_PATH,
        extra_namespace={
            "super": _StubBase,
            "upload_logging_setup": lambda *_a, **_k: logging.getLogger("upload-test"),
            "BACKUP_TYPE": "M",
            "_resolve_encryption": _resolve_encryption,
            "Path": Path,
        },
    )
    logging_dir = tmp_path / "logs"
    logging_dir.mkdir()
    inst = types.SimpleNamespace(
        alias="db1",
        logging_dir=logging_dir,
        report_options={"encryption": "none"},
    )
    init(
        inst,
        "S3",
        lambda *_a: object(),
        {
            "BACKUP_TYPE": "M",
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
            POST_RUN_ENCRYPT=True,
            XTRABACKUP_AES256_KEYFILE=_KEYFILE,
        )
        assert inst.xtrabackup_aes256 is False
        assert inst.encrypt is True
        assert inst.post_run_encrypt is True
        assert inst.report_options["encryption"] == "gpg"

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            ({"ENCRYPTION_FORMAT": "gpg", "ENCRYPT": True}, False),
            (
                {
                    "ENCRYPTION_FORMAT": "gpg",
                    "ENCRYPT": True,
                    "ENCRYPT_USING_TMPDIR": True,
                },
                True,
            ),
            (
                {
                    "ENCRYPTION_FORMAT": "gpg",
                    "ENCRYPT": True,
                    "POST_RUN_ENCRYPT": True,
                    "ENCRYPT_USING_TMPDIR": True,
                },
                False,
            ),
            (
                {
                    "ENCRYPTION_FORMAT": "aes256",
                    "ENCRYPT": True,
                    "ENCRYPT_USING_TMPDIR": True,
                    "XTRABACKUP_AES256_KEYFILE": _KEYFILE,
                },
                False,
            ),
        ],
    )
    def test_tmpdir_follows_config_for_in_place_gpg(
        self, tmp_path: Path, settings: dict[str, object], *, expected: bool
    ) -> None:
        """Assert ENCRYPT_USING_TMPDIR is honoured only for in-place GPG.

        Unlike XtraBackup, Mydumper defaults to encrypting the on-host backup
        directory; a missing or false ENCRYPT_USING_TMPDIR must not force a
        tmpdir copy that leaves the real backup plaintext.
        """
        assert _upload_init(tmp_path, **settings).encrypt_using_tmpdir is expected
