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

"""Assert ``ENCRYPTION_FORMAT`` — not field presence — drives encryption.

The explicit selector has to reproduce, for every configuration the form can
produce, what the payload used to infer from whichever of
``XTRABACKUP_AES256_KEYFILE`` / ``POST_RUN_ENCRYPT`` happened to be set, and it
has to stop a field left behind by an earlier config from adding a pass of its
own. Three surfaces are covered: the resolver that turns the config value into
the two pass flags, ``run``'s post-backup passes, and the upload phase's
``_encrypt``, which reads the same fields from its own object.
"""

import logging
import types
from typing import cast

import pytest

from app.extensions.apps.mysql_backups.forms import (
    ENCRYPTION_FORMAT_BY_PASSES,
    EncryptionFormat,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_instance,
    payload_method,
    Recorder,
)

_FORMATS = cast("tuple[str, ...]", load_constant("ENCRYPTION_FORMATS"))
assert isinstance(_FORMATS, tuple)
_resolve_encryption = load_function("_resolve_encryption")

_KEYFILE = "/keys/aes.key"


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

    @pytest.mark.parametrize(
        ("keyfile", "post_run_encrypt", "expected"),
        [
            (None, False, ("none", False, False)),
            (None, True, ("gpg", False, True)),
            (_KEYFILE, False, ("aes256", True, False)),
            (_KEYFILE, True, ("dual", True, True)),
        ],
    )
    def test_absent_format_reproduces_the_legacy_inference(
        self,
        keyfile: str | None,
        expected: tuple[str, bool, bool],
        *,
        post_run_encrypt: bool,
    ) -> None:
        """Assert a pre-selector config resolves to the state it already ran.

        These four combinations are the whole of what the payload used to infer,
        so a stored task keeps its encryption when this payload replaces the old
        one.
        """
        assert _resolve_encryption(None, keyfile, gpg=post_run_encrypt) == expected

    @pytest.mark.parametrize("selected", ["", "aes-256", "AES256", "GPG", "yes"])
    def test_unrecognized_format_falls_back_rather_than_disabling(
        self, selected: str
    ) -> None:
        """Assert an unknown value infers instead of resolving to no encryption.

        Reading an unrecognized selector as ``none`` would ship a plaintext backup
        for a job that asked to be encrypted, so it shares the inference branch.
        """
        assert _resolve_encryption(selected, _KEYFILE, gpg=True) == (
            "dual",
            True,
            True,
        )

    def test_every_format_is_reachable_from_the_vocabulary(self) -> None:
        """Assert the resolver accepts each name the config vocabulary publishes."""
        assert [
            _resolve_encryption(name, None, gpg=False)[0] for name in _FORMATS
        ] == list(_FORMATS)


class _RunProbe:
    """Record which post-backup encryption passes ``run`` performed."""

    def __init__(self) -> None:
        self.aes_dirs: list[str] = []
        self.gpg_dirs: list[str] = []
        self.gpg_cfgs: list[dict[str, object]] = []
        self.warnings: list[str] = []
        self.saved_disk_space = 0


def _run_backup(
    tmp_path,
    *,
    enc_aes: bool,
    enc_gpg: bool,
    post_run_encrypt: bool,
    dir_encrypt_config: dict[str, object] | None = None,
    incremental: bool = True,
    fail_metadata_copy: bool = False,
) -> _RunProbe:
    """Run the real ``run`` past its post-backup encryption block.

    Every pass is replaced by a recorder, so the assertions are about which
    branches the real method took rather than about its shape. The
    ``encrypt_dir`` stand-in accepts ``**cfg`` so a dropped
    ``dir_encrypt_config`` cannot hide behind a two-argument stub.

    :param tmp_path: Pytest temp directory used for the backup paths.
    :param enc_aes: Whether the AES-256 post-backup pass is enabled.
    :param enc_gpg: Whether GPG encryption is enabled on the instance.
    :param post_run_encrypt: Whether GPG runs after the backup finishes.
    :param dir_encrypt_config: Mapping assigned to ``inst.dir_encrypt_config`` and
        spread into ``encrypt_dir`` as kwargs. ``None`` leaves ``{}`` (no kwargs).
    :param incremental: When ``True`` (the default), skip the local mycnf/certs copy
        block so encryption-format cases stay focused on the post-backup passes.
    :param fail_metadata_copy: When ``True``, force a localhost non-incremental run
        whose ``backup_mycnf`` raises the payload ``BackupError``, exercising the
        soft-fail warning before post-run encryption.
    :return: Probe of which encryption passes ran, which GPG kwargs were seen,
        warnings logged during ``run``, and whether disk space was saved.
    """
    probe = _RunProbe()
    last_backup_dir = tmp_path / "backups" / "host1"
    last_backup_dir.mkdir(parents=True)

    def _record_gpg(dir_path: str, _logger: object, **cfg: object) -> None:
        probe.gpg_dirs.append(dir_path)
        probe.gpg_cfgs.append(cfg)

    inst, backup_error, _ = payload_instance(
        ("run",),
        extra_namespace={
            "time": types.SimpleNamespace(time=lambda: 0.0),
            "is_encrypted_dir": lambda *_a, **_k: True,
            "encrypt_dir": _record_gpg,
            "format_seconds_to_hhmmss": lambda _s: "00:00:00",
            "get_dir_size": lambda *_a, **_k: "1 MB",
            "_write_run_result": lambda *_a, **_k: None,
            "STATUS_OK": 0,
            "STATUS_WARNING": 1,
            # The upload map is built before the (empty) provider loop, so the
            # names have to resolve even though nothing uploads here.
            "RsyncUploadProvider": object(),
            "S3UploadProvider": object(),
            "GSUploadProvider": object(),
        },
    )
    recorder = Recorder()
    inst.logger = recorder
    probe.warnings = recorder.warnings
    inst.enc_aes = enc_aes
    inst.enc_gpg = enc_gpg
    inst.post_run_encrypt = post_run_encrypt
    inst.hardlink = True
    inst.max_copies = 2
    inst.only_if_running_replica = False
    inst.only_if_read_only = False
    inst.check_disk_space = False
    inst.is_pxc = False
    if fail_metadata_copy:
        incremental = False

        def _fail_mycnf(_backup_dir: str) -> None:
            raise backup_error(
                "Access denied for user 'root'@'localhost' (using password: YES)"
            )

        inst.backup_mycnf = _fail_mycnf
        inst.backup_certs = lambda *_a, **_k: None
    inst.incremental = incremental
    inst.host = "localhost"
    inst.upload_type = []
    inst.backup_dir = str(tmp_path / "xtrabackup_tmpdir")
    inst.last_backup_dir = str(last_backup_dir)
    inst.dir_encrypt_config = {} if dir_encrypt_config is None else dir_encrypt_config
    inst.encrypt_files_aes256 = probe.aes_dirs.append
    inst._decrypt_metadata_file = lambda *_a, **_k: None
    inst._run_backup_cmd = lambda: None
    inst._is_track_changed_pages = lambda: False
    inst._purge_old_backups = lambda: None
    inst._save_disk_space = lambda: setattr(
        probe, "saved_disk_space", probe.saved_disk_space + 1
    )
    inst.notify = lambda *_a, **_k: None

    inst.run()
    return probe


class TestRunPostBackupPasses:
    """Assert ``run``'s post-backup passes follow the resolved format."""

    def test_none_runs_no_pass_and_saves_disk_space(self, tmp_path) -> None:
        """Assert an unencrypted run encrypts nothing and hardlinks instead."""
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=False, post_run_encrypt=False
        )
        assert probe.aes_dirs == []
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 1

    def test_gpg_runs_the_post_run_pass_only(self, tmp_path) -> None:
        """Assert a GPG format with post-run timing encrypts the finished directory."""
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=True, post_run_encrypt=True
        )
        assert probe.aes_dirs == []
        assert len(probe.gpg_dirs) == 1
        assert probe.saved_disk_space == 0

    def test_gpg_with_in_place_timing_defers_to_the_upload_phase(
        self, tmp_path
    ) -> None:
        """Assert in-place GPG encrypts nothing here and still saves disk space.

        In-place GPG is applied by the upload phase, so the backup phase must
        behave exactly as an unencrypted run does.
        """
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=True, post_run_encrypt=False
        )
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 1

    def test_aes256_runs_the_aes_pass_only(self, tmp_path) -> None:
        """Assert an AES-256 format encrypts with xbcrypt and skips GPG."""
        probe = _run_backup(
            tmp_path, enc_aes=True, enc_gpg=False, post_run_encrypt=False
        )
        assert len(probe.aes_dirs) == 1
        assert probe.gpg_dirs == []

    def test_dual_runs_aes_and_skips_gpg(self, tmp_path) -> None:
        """Assert ``dual`` applies AES-256 only, as the backend always has.

        XtraBackup's built-in AES-256 already encrypted the directory and the
        backend cannot layer GPG on top, so ``dual`` selects AES-256 and reports
        the GPG pass as skipped. Pinned here because the name suggests otherwise.
        """
        probe = _run_backup(tmp_path, enc_aes=True, enc_gpg=True, post_run_encrypt=True)
        assert len(probe.aes_dirs) == 1
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 0

    def test_stale_post_run_flag_runs_no_gpg_pass(self, tmp_path) -> None:
        """Assert a post-run flag the format excludes cannot encrypt on its own.

        This is the configuration the selector exists for: a stored task whose
        ``POST_RUN_ENCRYPT`` survives from an earlier config while the operator
        selected no encryption.
        """
        probe = _run_backup(
            tmp_path, enc_aes=False, enc_gpg=False, post_run_encrypt=True
        )
        assert probe.gpg_dirs == []
        assert probe.saved_disk_space == 1

    def test_post_run_gpg_passes_the_configured_recipient(self, tmp_path) -> None:
        """Assert post-run GPG forwards ``DIR_ENCRYPT_CONFIG`` to ``encrypt_dir``.

        Without the kwargs, ``DirectoryEncryptor`` falls back to the host
        ``dir_encrypt.yml`` and the form recipient never applies.
        """
        recipient = {"encryption recipient": "ops@example.com"}
        probe = _run_backup(
            tmp_path,
            enc_aes=False,
            enc_gpg=True,
            post_run_encrypt=True,
            dir_encrypt_config=recipient,
        )
        assert len(probe.gpg_dirs) == 1
        assert probe.gpg_cfgs == [recipient]

    def test_post_run_gpg_with_empty_config_adds_no_kwargs(self, tmp_path) -> None:
        """Assert an empty ``DIR_ENCRYPT_CONFIG`` still reaches ``encrypt_dir``.

        Spreading ``{}`` adds nothing, so the encryptor keeps today's host-file
        then default fallback.
        """
        probe = _run_backup(
            tmp_path,
            enc_aes=False,
            enc_gpg=True,
            post_run_encrypt=True,
            dir_encrypt_config={},
        )
        assert len(probe.gpg_dirs) == 1
        assert probe.gpg_cfgs == [{}]

    def test_mycnf_copy_failure_warns_and_still_runs_post_run_gpg(
        self, tmp_path
    ) -> None:
        """Assert a local metadata-copy ``BackupError`` cannot skip post-run GPG.

        ``backup_mycnf`` / ``backup_certs`` used to abort ``run`` before
        ``encrypt_dir``. The soft-fail path must warn and still forward the form
        recipient.
        """
        recipient = {"encryption recipient": "ops@example.com"}
        probe = _run_backup(
            tmp_path,
            enc_aes=False,
            enc_gpg=True,
            post_run_encrypt=True,
            dir_encrypt_config=recipient,
            fail_metadata_copy=True,
        )
        assert any("Access denied" in warning for warning in probe.warnings)
        assert len(probe.gpg_dirs) == 1
        assert probe.gpg_cfgs == [recipient]


def _upload_instance(
    *, encrypt: bool, post_run_encrypt: bool, aes256: bool, tmpdir: bool = False
):
    """Build an ``Upload`` with its resolved state fixed, to drive ``_encrypt`` alone.

    The flags are set directly rather than resolved: what ``__init__`` resolves
    them to is covered by :class:`TestUploadInitResolvesEncryption`.
    """
    encrypted: list[str] = []
    warnings: list[str] = []
    inst, _, _ = payload_instance(
        ("_encrypt",),
        extra_namespace={
            "encrypt_dir": lambda dir_path, _logger, **_cfg: encrypted.append(dir_path)
        },
    )
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        warning=lambda msg, *_a, **_k: warnings.append(msg),
    )
    inst.encrypt = encrypt
    inst.post_run_encrypt = post_run_encrypt
    inst.xtrabackup_aes256 = _KEYFILE if aes256 else False
    inst.encrypt_using_tmpdir = tmpdir
    inst.dir_encrypt_config = {}
    inst.paths = [{"source": "/backups/host1", "tmpdir": "/tmp/enc/host1"}]
    return inst, encrypted, warnings


class TestUploadEncrypt:
    """Assert the upload phase's GPG pass follows the resolved format.

    ``Upload`` reads the encryption fields from its own object, independently of
    the backup phase, so the format has to reach it too or a selection would hold
    on the way down and not on the way out.
    """

    def test_gpg_encrypts_the_source_directory(self) -> None:
        """Assert a GPG selection encrypts before upload."""
        inst, encrypted, _ = _upload_instance(
            encrypt=True, post_run_encrypt=False, aes256=False
        )
        inst._encrypt()
        assert encrypted == ["/backups/host1"]

    def test_gpg_with_tmpdir_encrypts_the_copy(self) -> None:
        """Assert the tmpdir copy is what gets encrypted when that timing is on."""
        inst, encrypted, _ = _upload_instance(
            encrypt=True, post_run_encrypt=False, aes256=False, tmpdir=True
        )
        inst._encrypt()
        assert encrypted == ["/tmp/enc/host1"]

    def test_aes256_skips_the_gpg_pass(self) -> None:
        """Assert an AES-256 selection is not GPG-encrypted again on the way out."""
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
        """Accept and ignore whatever the real base would consume."""


def _upload_init(tmp_path, **settings: object):
    """Run the real ``Upload.__init__`` and return the object it configured.

    :param tmp_path: Directory standing in for the backup and logging roots.
    :param settings: Server settings merged over the required keys.
    :return: The initialised stand-in for an ``Upload``.
    """
    init = payload_method(
        "Upload",
        "__init__",
        extra_namespace={
            "super": _StubBase,
            "upload_logging_setup": lambda *_a, **_k: logging.getLogger("upload-test"),
            "DEFAULT_LOGGING_DIR": str(tmp_path),
            "_resolve_encryption": _resolve_encryption,
        },
    )
    inst = types.SimpleNamespace(
        alias="db1", backup_code="X", report_options={"encryption": "none"}
    )
    init(
        inst,
        "S3",
        lambda *_a: object(),
        {
            "BACKUP_TYPE": "X",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "LOGGING_DIR": str(tmp_path),
            **settings,
        },
        logging.getLogger("test"),
    )
    inst.log_stream.close()
    return inst


class TestUploadInitResolvesEncryption:
    """Assert ``Upload.__init__`` derives its encryption state from the format.

    The upload phase reads the encryption fields from its own object, so it
    resolves the format a second time. Every flag ``_encrypt`` and the tmpdir copy
    later branch on is decided here, which is also where a field left behind by an
    earlier config has to stop mattering.
    """

    def test_gpg_ignores_a_stale_key_file(self, tmp_path) -> None:
        """Assert a GPG format keeps its timings and drops a leftover key file.

        This is the regression the second resolution exists for: ``_encrypt``
        returns early when it sees an AES-256 key file, so a ``gpg`` task still
        carrying one used to upload without ever being GPG-encrypted.
        """
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

    def test_aes256_ignores_stale_gpg_timings(self, tmp_path) -> None:
        """Assert an AES-256 format keeps its key file and drops both GPG timings."""
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

    def test_none_clears_every_stale_field(self, tmp_path) -> None:
        """Assert an unencrypted format leaves nothing for the upload to act on."""
        inst = _upload_init(
            tmp_path,
            ENCRYPTION_FORMAT="none",
            ENCRYPT=True,
            POST_RUN_ENCRYPT=True,
            XTRABACKUP_AES256_KEYFILE=_KEYFILE,
        )
        assert inst.xtrabackup_aes256 is False
        assert inst.encrypt is False
        assert inst.post_run_encrypt is False
        assert inst.encrypt_using_tmpdir is False
        assert inst.report_options["encryption"] == "none"

    def test_absent_format_and_flag_still_encrypts(self, tmp_path) -> None:
        """Assert the fail-safe survives the format: an absent ``ENCRYPT`` encrypts.

        A hand-authored config naming neither key must not upload plaintext, so an
        absent ``ENCRYPT`` still reads as enabled and the inference it feeds
        resolves to a GPG format rather than to no encryption.
        """
        inst = _upload_init(tmp_path)
        assert inst.encrypt is True
        assert inst.report_options["encryption"] == "gpg"

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            ({"ENCRYPTION_FORMAT": "gpg", "ENCRYPT": True}, True),
            (
                {
                    "ENCRYPTION_FORMAT": "gpg",
                    "ENCRYPT": True,
                    "POST_RUN_ENCRYPT": True,
                },
                False,
            ),
            (
                {
                    "ENCRYPTION_FORMAT": "dual",
                    "ENCRYPT": True,
                    "XTRABACKUP_AES256_KEYFILE": _KEYFILE,
                },
                False,
            ),
            (
                {
                    "ENCRYPTION_FORMAT": "aes256",
                    "ENCRYPT": True,
                    "XTRABACKUP_AES256_KEYFILE": _KEYFILE,
                },
                False,
            ),
            ({"ENCRYPTION_FORMAT": "none", "ENCRYPT": True}, False),
        ],
    )
    def test_tmpdir_is_claimed_only_by_in_place_gpg(
        self, tmp_path, settings: dict[str, object], *, expected: bool
    ) -> None:
        """Assert only an in-place-GPG-and-nothing-else run copies to a tmpdir.

        Post-run GPG and AES-256 each encrypt the backup where it already sits, so
        neither has a claim on the copy.
        """
        assert _upload_init(tmp_path, **settings).encrypt_using_tmpdir is expected


class TestFormatVocabularyMatchesTheForm:
    """Pin the payload's format vocabulary to the form's, ordering included.

    Both sides encode a format's passes in its index — bit 1 is AES-256, bit 0 is
    GPG — and each infers a pre-selector task's format from that index alone. They
    are separate literals because the payload is standalone, so nothing but this
    test stops a reorder on one side from making the edit form and the backup
    script disagree about what an existing task already runs.
    """

    def test_the_ordered_tuples_are_identical(self) -> None:
        """Assert the payload tuple equals the form's, value for value."""
        assert tuple(fmt.value for fmt in ENCRYPTION_FORMAT_BY_PASSES) == _FORMATS

    def test_the_enum_publishes_exactly_the_payload_vocabulary(self) -> None:
        """Assert neither side carries a format the other cannot name."""
        assert {fmt.value for fmt in EncryptionFormat} == set(_FORMATS)

    @pytest.mark.parametrize("passes", range(len(ENCRYPTION_FORMAT_BY_PASSES)))
    def test_each_index_resolves_to_the_form_format_for_those_passes(
        self, passes: int
    ) -> None:
        """Assert the resolver's inference agrees with the form's index ordering.

        Drives the real resolver rather than re-reading its tuple, so the
        agreement covers the arithmetic each side performs on the index too.
        """
        resolved, _, _ = _resolve_encryption(
            None, _KEYFILE if passes & 2 else None, gpg=bool(passes & 1)
        )
        assert resolved == ENCRYPTION_FORMAT_BY_PASSES[passes].value
