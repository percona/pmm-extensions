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

"""Test a mydumper run landing on a day that already holds a backup.

A day-granular output directory is reused by every run of the same calendar day,
and mydumper refuses a non-empty target, so the run has to stage its dump beside
the day directory and publish it once the dump is complete. These tests drive the
payload's own methods over a real directory tree under ``tmp_path``; no mydumper
process is ever spawned.
"""

import datetime
import os
import shutil
import subprocess
import sys
import time as real_time
import types
from pathlib import Path
from typing import Any, cast, TYPE_CHECKING

import pytest

from tests.app.extensions.apps.mysql_backups.conftest import MYDUMPER_PAYLOAD_PATH
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_instance,
    Recorder,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _payload_constant(name: str) -> object:
    """Return one of the mydumper payload's own module-level constants.

    Reading the value out of the payload keeps the tests from re-stating a
    contract the payload owns.

    :param name: The constant's name.
    :return: Its value.
    """
    return load_constant(name, payload_path=MYDUMPER_PAYLOAD_PATH)


TODAY = datetime.date(2026, 1, 5)
TODAY_STR = "20260105"
YESTERDAY_STR = "20260104"
PARTIAL_SUFFIX = cast("str", _payload_constant("PARTIAL_SUFFIX"))
REPLACED_SUFFIX = cast("str", _payload_constant("REPLACED_SUFFIX"))
PARTIAL_MAX_AGE_SECONDS = cast("int", _payload_constant("PARTIAL_MAX_AGE_SECONDS"))
OLDER_THAN_ANY_GRACE_PERIOD = PARTIAL_MAX_AGE_SECONDS * 2
UPDATED_SINCE_DAYS = 3
_IS_BACKUP_DAY_NAME = cast(
    "Callable[[str], bool]",
    load_function("_is_backup_day_name", payload_path=MYDUMPER_PAYLOAD_PATH),
)
_RECLAIM_METHODS = (
    "_reclaim_interrupted_publish",
    "_reclaim_scratch_dir",
    "_scratch_name_parts",
    "_staging_owner_is_alive",
    "_process_started_at",
    "_run_started_at",
)
_FIRST_DUMP = {
    "metadata": "Finished dump at: first\n",
    "sakila.film.sql": "dump first\n",
}
_SECOND_DUMP = {
    "metadata": "Finished dump at: second\n",
    "sakila.film.sql": "dump second\n",
}


class _StoppedAtDispatchError(Exception):
    """Abort a lifted method at the point it would spawn mydumper."""


def _write_dump(target: Path, marker: str) -> None:
    """Write the files a completed dump leaves behind.

    :param target: The directory the dump lands in.
    :param marker: The token identifying which run produced the dump.
    """
    (target / "metadata").write_text(f"Finished dump at: {marker}\n")
    (target / "sakila.film.sql").write_text(f"dump {marker}\n")


def _scratch_name(suffix: str, *parts: object) -> str:
    """Build the name a run gives one of its scratch directories.

    :param suffix: The scratch suffix, read off the payload.
    :param parts: The dot-separated segments between the day and the suffix,
        a run's start time and the pid owning it, where the case under test has
        them.
    :return: The directory name, dot-prefixed so retention ignores it.
    """
    return ".".join((f".{TODAY_STR}", *(str(part) for part in parts))) + suffix


_DEFAULT_WORK_DIR_NAME = _scratch_name(PARTIAL_SUFFIX, "030000", 4242)


def _this_runs_scratch_name(suffix: str, *, started_at: float | None = None) -> str:
    """Build the name this test process would give a scratch directory right now.

    Stamped off the real clock rather than the frozen ``TODAY``, so the start time
    the name carries is one this process genuinely postdates: the payload reads the
    owner's liveness off that field against the pid's real start time.

    :param suffix: The scratch suffix, read off the payload.
    :param started_at: The instant to stamp, or the current time when omitted.
    :return: The directory name.
    """
    if started_at is None:
        stamp = real_time.strftime("%Y%m%d.%H%M%S")
    else:
        stamp = real_time.strftime("%Y%m%d.%H%M%S", real_time.localtime(started_at))
    return f".{stamp}.{os.getpid()}{suffix}"


def _exited_pid() -> int:
    """Return the pid of a process that has already exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class _DumpWriter:
    """Stand in for mydumper: write dump files into ``--outputdir`` and exit."""

    def __init__(self, returncode: int, marker: str, calls: list[list[str]]) -> None:
        self.returncode_to_report = returncode
        self.marker = marker
        self.calls = calls

    def __call__(self, cmd: list[str], **_kwargs: object) -> Any:
        """Write this run's dump files and report the configured exit status.

        :param cmd: The argv the payload dispatched.
        :param _kwargs: The stream options the payload passes, all ignored.
        :return: A stand-in for the process the payload would have spawned.
        """
        self.calls.append(list(cmd))
        outputdir = next(
            Path(arg.split("=", 1)[1])
            for arg in cmd
            if str(arg).startswith("--outputdir=")
        )
        if self.returncode_to_report == 0:
            _write_dump(outputdir, self.marker)
        return types.SimpleNamespace(
            stdout=None,
            returncode=self.returncode_to_report,
            wait=lambda: self.returncode_to_report,
            poll=lambda: self.returncode_to_report,
        )


def _mydumper_instance(
    method_names: tuple[str, ...],
    *,
    popen: Any = None,
    extra: dict[str, object] | None = None,
) -> tuple[Any, type[Exception], list[list[str]]]:
    """Lift the named ``MyDumper`` methods onto an instance with a faked clock.

    :param method_names: The payload methods to lift.
    :param popen: A ``subprocess.Popen`` stand-in, or ``None`` to keep the
        harness's recording fake.
    :param extra: Stand-ins for the module-level functions the lifted methods call.
    :return: The instance, the payload's ``BackupError``, and the recorded argv lists.
    """
    calls: list[list[str]] = []
    namespace: dict[str, object] = {
        "shutil": shutil,
        "sys": sys,
        "datetime": datetime,
        "time": types.SimpleNamespace(
            strftime=lambda fmt, *_args: TODAY.strftime(fmt),
            strptime=real_time.strptime,
            mktime=real_time.mktime,
            time=real_time.time,
            clock_gettime=real_time.clock_gettime,
            CLOCK_BOOTTIME=getattr(
                real_time, "CLOCK_BOOTTIME", real_time.CLOCK_MONOTONIC
            ),
            sleep=lambda _seconds: None,
        ),
        "_is_backup_day_name": _IS_BACKUP_DAY_NAME,
    }
    if popen is not None:
        namespace["subprocess"] = types.SimpleNamespace(
            Popen=popen(calls), PIPE=-1, STDOUT=-2
        )
    namespace.update(extra or {})
    instance, backup_error, harness_calls = payload_instance(
        method_names,
        extra_namespace=namespace,
        payload_path=MYDUMPER_PAYLOAD_PATH,
    )
    instance.logger = Recorder()
    return instance, backup_error, calls if popen is not None else harness_calls


def _tripwire(calls: list[list[str]]) -> Any:
    """Return a ``Popen`` stand-in that records the argv and aborts the run.

    :param calls: The list each dispatched argv is appended to.
    :return: The ``Popen`` stand-in.
    """

    def popen(cmd: list[str], **_kwargs: object) -> None:
        calls.append(list(cmd))
        raise _StoppedAtDispatchError

    return popen


def _dump_writer(*, returncode: int = 0, marker: str = "first") -> Any:
    """Return a ``_DumpWriter`` factory for ``_mydumper_instance``.

    :param returncode: The exit status the faked mydumper reports.
    :param marker: The token identifying the dump it writes.
    :return: A factory taking the recording list and returning the stand-in.
    """
    return lambda calls: _DumpWriter(returncode, marker, calls)


def _existing_dump(server_dir: Path, day: str = TODAY_STR) -> Path:
    """Lay down a completed dump for ``day`` the way an earlier run leaves it.

    :param server_dir: The server directory the day directory goes in.
    :param day: The day the dump is published under.
    :return: The day directory holding the dump.
    """
    day_dir = server_dir / day
    day_dir.mkdir(parents=True)
    _write_dump(day_dir, "first")
    return day_dir


def _dumper(
    tmp_path: Path,
    method_names: tuple[str, ...],
    *,
    popen: Any = None,
    extra: dict[str, object] | None = None,
    work_dir_name: str = _DEFAULT_WORK_DIR_NAME,
) -> tuple[Any, type[Exception], list[list[str]]]:
    """Build an instance whose paths point into a real server directory.

    :param tmp_path: The server directory the run works in.
    :param method_names: The payload methods to lift.
    :param popen: A ``subprocess.Popen`` stand-in, or ``None`` to keep the
        harness's recording fake.
    :param extra: Stand-ins for the module-level functions the lifted methods call.
    :param work_dir_name: The name of the staging directory this run owns.
    :return: The instance, the payload's ``BackupError``, and the recorded argv lists.
    """
    instance, backup_error, calls = _mydumper_instance(
        method_names, popen=popen, extra=extra
    )
    instance.backup_server_dir = tmp_path
    instance.backup_dir = tmp_path / TODAY_STR
    instance.last_backup_dir = instance.backup_dir
    instance.work_dir = tmp_path / work_dir_name
    instance.latest_link = tmp_path / "latest"
    instance.today_str = TODAY_STR
    instance.logging_dir = tmp_path / "logs"
    instance.logging_dir.mkdir(exist_ok=True)
    instance.defaults_cnf_file = "/root/.my.cnf"
    instance.alias = "primary"
    instance.host = "127.0.0.1"
    instance.port = 3306
    instance.mydumper_verbose = 3
    instance.mydumper_for_schemas = True
    instance.mydumper_dump_triggers = True
    instance.mydumper_extra_args = None
    instance.mydumper_use_numa = False
    instance.compression_algorithm = "gzip"
    instance.updated_since = 0
    instance.less_locking = False
    instance.desync_pxc = False
    instance.use_ftwrl_guardian = False
    instance.post_run_encrypt = False
    instance.encrypt_using_tmpdir = False
    instance.daily_purge = 7
    instance.weekly_purge = 4
    instance.report_options = {}
    instance.dir_encrypt_config = {}
    instance.upload_type = []
    instance.check_disk_space = False
    instance.only_if_running_replica = False
    instance.only_if_read_only = False
    instance.hardlink = False
    instance.prev_backup_dir = tmp_path / YESTERDAY_STR
    instance.prev_encrypted = False
    instance.valid_prev_backup_dir = None
    instance.cache_md5 = False
    instance.compress = False
    instance.s3_bucket = None
    instance.s3_daily = False
    instance.s3_encrypt = True
    return instance, backup_error, calls


def _dir_snapshot(path: Path) -> dict[str, str]:
    """Return every file under ``path`` keyed by its relative name.

    :param path: The directory to read.
    :return: Each file's contents, keyed by its path relative to ``path``.
    """
    return {
        str(item.relative_to(path)): item.read_text()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _scratch_names(path: Path) -> list[str]:
    """Return the names of the scratch directories left in a server directory.

    :param path: The server directory to read.
    :return: The scratch directory names, sorted.
    """
    return sorted(
        item.name
        for item in path.iterdir()
        if item.name.endswith((PARTIAL_SUFFIX, REPLACED_SUFFIX))
    )


class _StubRecord:
    """Collect what the stubbed collaborators of a lifted method were handed.

    Attributes rather than dictionary keys, so a test that asserts a stub was
    never reached fails on a misspelled name instead of passing vacuously.
    """

    def __init__(self) -> None:
        self.encrypted: Path | None = None
        self.saved: Path | None = None
        self.purged: bool = False
        self.checksummed: Path | None = None
        self.hardlinked: tuple[Path, Path] | None = None


def _runner(
    tmp_path: Path,
    *,
    free_space: bool = True,
    post_run_encrypt: bool = False,
    encrypt_error: Exception | None = None,
    popen: Any = None,
    work_dir_name: str = _DEFAULT_WORK_DIR_NAME,
) -> tuple[Any, type[Exception], _StubRecord]:
    """Build an instance carrying ``run`` with its module-level calls stubbed.

    :param tmp_path: The server directory the run works in.
    :param free_space: What the stubbed free-space check reports.
    :param post_run_encrypt: Whether the run takes the GPG branch.
    :param encrypt_error: An error the stubbed encryption raises, if any.
    :param popen: A faked mydumper, which makes the run dispatch the dump command
        for real rather than take a stub in its place.
    :param work_dir_name: The name of the staging directory this run owns.
    :return: The instance, the payload's ``BackupError``, and a record of what the
        stubbed collaborators were handed.
    """
    record = _StubRecord()

    def _encrypt(path: Path, *_args: object, **_kwargs: object) -> None:
        record.encrypted = path
        if encrypt_error is not None:
            raise encrypt_error

    extra: dict[str, object] = {
        "is_free_space": lambda *_a, **_k: (free_space, "Not enough free space"),
        "encrypt_dir": _encrypt,
        "get_dir_size": lambda _path: 0,
        "humanize_bytes": lambda _size: "0 B",
        "format_seconds_to_hhmmss": lambda _seconds: "00:00:00",
        "_write_run_result": lambda *_a, **_k: None,
        "BackupNotifyStatus": types.SimpleNamespace(OK="OK"),
        # Named in the provider table the run builds before it checks whether any
        # upload is configured.
        "RsyncUploadProvider": object,
        "S3UploadProvider": object,
        "GSUploadProvider": object,
    }
    methods = ("run", "_publish_backup", "_needs_double_space", *_RECLAIM_METHODS)
    instance, backup_error, _ = _dumper(
        tmp_path,
        methods + (("_run_backup_cmd",) if popen is not None else ()),
        popen=popen,
        extra=extra,
        work_dir_name=work_dir_name,
    )

    def _save_disk_space() -> None:
        record.saved = instance.work_dir

    def _purge_old_backups() -> None:
        record.purged = True

    instance.post_run_encrypt = post_run_encrypt
    instance.enc_gpg = post_run_encrypt
    instance.enc_aes = False
    instance.notify = lambda *_a, **_k: None
    instance._get_version = lambda: "0.19.3"
    if popen is None:
        instance._run_backup_cmd = lambda: _write_dump(instance.work_dir, "second")
    instance._save_disk_space = _save_disk_space
    instance._purge_old_backups = _purge_old_backups
    return instance, backup_error, record


def _saver(
    tmp_path: Path, *, daily_purge: int = 7, weekly_purge: int = 4
) -> tuple[Any, _StubRecord]:
    """Build an instance carrying the space-saving pass with its helpers stubbed.

    :param tmp_path: The server directory the run works in.
    :param daily_purge: How many daily backups retention keeps.
    :param weekly_purge: How many weekly backups retention keeps.
    :return: The instance and a record of the directories the helpers were handed.
    """
    record = _StubRecord()

    def _get_dir_dict(path: Path, **_kwargs: object) -> dict[str, object]:
        record.checksummed = path
        return {}

    def _hardlink_dirs(src: Path, dest: Path, **_kwargs: object) -> dict[str, object]:
        record.hardlinked = (src, dest)
        return {}

    extra: dict[str, object] = {
        "get_dir_dict": _get_dir_dict,
        "hardlink_dirs": _hardlink_dirs,
        "hardlink_listed_files": lambda *_a, **_k: (0, "Ok"),
        "is_encrypted_dir": lambda *_a, **_k: False,
    }
    instance, _, _ = _dumper(
        tmp_path,
        ("_save_disk_space", "_day_is_retained", "_retained_days", "_list_backups"),
        extra=extra,
    )
    instance.hardlink = True
    instance.daily_purge = daily_purge
    instance.weekly_purge = weekly_purge
    return instance, record


class TestStagedOutputDir:
    """Cover the directory mydumper is pointed at."""

    def test_dumps_into_the_staging_directory(self, tmp_path: Path) -> None:
        """Assert the dump target is the staging directory, not the day directory."""
        instance, _, calls = _dumper(tmp_path, ("_run_backup_cmd",), popen=_tripwire)
        instance.work_dir.mkdir()

        with pytest.raises(_StoppedAtDispatchError):
            instance._run_backup_cmd()

        assert f"--outputdir={instance.work_dir}" in calls[0]
        assert f"--outputdir={instance.backup_dir}" not in calls[0]

    def test_never_asks_mydumper_to_reuse_a_populated_directory(
        self, tmp_path: Path
    ) -> None:
        """Assert no flag is passed that would clear or merge into an existing dump."""
        instance, _, calls = _dumper(tmp_path, ("_run_backup_cmd",), popen=_tripwire)
        instance.work_dir.mkdir()

        with pytest.raises(_StoppedAtDispatchError):
            instance._run_backup_cmd()

        assert not {"--clear", "--dirty", "--merge"}.intersection(calls[0])

    def test_second_run_leaves_the_first_dump_in_place_while_dumping(
        self, tmp_path: Path
    ) -> None:
        """Assert dispatch happens with the day's dump untouched on disk."""
        day_dir = _existing_dump(tmp_path)
        before = _dir_snapshot(day_dir)
        instance, _, calls = _dumper(tmp_path, ("_run_backup_cmd",), popen=_tripwire)
        instance.work_dir.mkdir()

        with pytest.raises(_StoppedAtDispatchError):
            instance._run_backup_cmd()

        assert calls, "the run has to reach mydumper instead of refusing the day"
        assert _dir_snapshot(day_dir) == before


class TestPublish:
    """Cover promoting a staged dump into the day directory."""

    def test_publishes_dump_files_directly_into_the_day_directory(
        self, tmp_path: Path
    ) -> None:
        """Assert the published directory holds dump files, not a per-run subdirectory."""
        instance, _, _ = _dumper(tmp_path, ("_publish_backup",))
        instance.work_dir.mkdir()
        (instance.work_dir / "metadata").write_text("Finished dump at: second\n")

        instance._publish_backup()

        assert (instance.backup_dir / "metadata").is_file()
        assert not any(item.is_dir() for item in instance.backup_dir.iterdir())
        assert not instance.work_dir.exists()

    def test_replaces_an_earlier_same_day_dump(self, tmp_path: Path) -> None:
        """Assert the staged dump wins and no scratch directory is left behind."""
        _existing_dump(tmp_path)
        instance, _, _ = _dumper(tmp_path, ("_publish_backup",))
        instance.work_dir.mkdir()
        (instance.work_dir / "metadata").write_text("Finished dump at: second\n")

        instance._publish_backup()

        assert _dir_snapshot(instance.backup_dir) == {
            "metadata": "Finished dump at: second\n"
        }
        assert [item.name for item in tmp_path.iterdir() if item.name != "logs"] == [
            TODAY_STR
        ]

    def test_latest_symlink_still_resolves_to_dump_files(self, tmp_path: Path) -> None:
        """Assert the day-granular ``latest`` target survives the promotion."""
        _existing_dump(tmp_path)
        instance, _, _ = _dumper(tmp_path, ("_publish_backup",))
        instance.latest_link.symlink_to(TODAY_STR)
        instance.work_dir.mkdir()
        (instance.work_dir / "metadata").write_text("Finished dump at: second\n")

        instance._publish_backup()

        assert (instance.latest_link / "metadata").read_text() == (
            "Finished dump at: second\n"
        )

    def test_refuses_a_symlinked_day_path(self, tmp_path: Path) -> None:
        """Assert a day path swapped for a symlink is rejected rather than followed."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        instance, backup_error, _ = _dumper(tmp_path, ("_publish_backup",))
        instance.backup_dir.symlink_to(elsewhere, target_is_directory=True)
        instance.work_dir.mkdir()

        with pytest.raises(backup_error):
            instance._publish_backup()

        assert instance.backup_dir.is_symlink()

    def test_puts_the_earlier_dump_back_when_the_promotion_fails(
        self, tmp_path: Path
    ) -> None:
        """Assert a failed promotion restores the day directory there and then."""
        day_dir = _existing_dump(tmp_path)
        instance, backup_error, _ = _dumper(tmp_path, ("_publish_backup",))

        with pytest.raises(backup_error):
            instance._publish_backup()

        assert _dir_snapshot(day_dir) == _FIRST_DUMP
        assert not _scratch_names(tmp_path)

    def test_leaves_another_runs_moved_aside_dump_alone(self, tmp_path: Path) -> None:
        """Assert promotion never touches scratch directories it does not own."""
        _existing_dump(tmp_path)
        sibling = tmp_path / _scratch_name(REPLACED_SUFFIX, "235959", 999999)
        sibling.mkdir()
        _write_dump(sibling, "sibling")
        instance, _, _ = _dumper(tmp_path, ("_publish_backup",))
        instance.work_dir.mkdir()
        _write_dump(instance.work_dir, "second")

        instance._publish_backup()

        assert _dir_snapshot(instance.backup_dir) == _SECOND_DUMP
        assert _dir_snapshot(sibling) == {
            "metadata": "Finished dump at: sibling\n",
            "sakila.film.sql": "dump sibling\n",
        }

    def test_reports_a_failed_rename_as_a_backup_error(self, tmp_path: Path) -> None:
        """Assert an OS-level rename failure surfaces with both paths named."""
        instance, backup_error, _ = _dumper(tmp_path, ("_publish_backup",))

        with pytest.raises(backup_error) as excinfo:
            instance._publish_backup()

        assert str(instance.work_dir) in str(excinfo.value)
        assert str(instance.backup_dir) in str(excinfo.value)


class TestFailedRerunKeepsFirstDump:
    """Cover a second run that fails after the day already holds a dump."""

    def test_failed_dump_leaves_the_earlier_dump_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """Assert a non-zero mydumper touches nothing outside its staging directory."""
        day_dir = _existing_dump(tmp_path)
        instance, backup_error, _ = _dumper(
            tmp_path, ("_run_backup_cmd",), popen=_dump_writer(returncode=1)
        )
        instance.work_dir.mkdir()

        with pytest.raises(backup_error):
            instance._run_backup_cmd()

        assert _dir_snapshot(day_dir) == _FIRST_DUMP

    def test_mydumper_error_never_mentions_a_non_empty_directory(
        self, tmp_path: Path
    ) -> None:
        """Assert the raw non-empty-directory refusal cannot be produced at all."""
        _existing_dump(tmp_path)
        instance, backup_error, _ = _dumper(
            tmp_path, ("_run_backup_cmd",), popen=_dump_writer(returncode=1)
        )
        instance.work_dir.mkdir()

        with pytest.raises(backup_error) as excinfo:
            instance._run_backup_cmd()

        assert "Mydumper failed (1)" in str(excinfo.value)
        assert any(
            str(instance.work_dir) in message for message in instance.logger.messages
        ), "the dispatch has to be logged for the next assertion to mean anything"
        assert "Directory is not empty" not in str(excinfo.value)
        assert not any(
            "Directory is not empty" in message for message in instance.logger.messages
        )


class TestReclaimInterruptedPublish:
    """Cover the leftovers an interrupted run can strand in the server directory."""

    def test_restores_a_dump_left_aside_by_an_interrupted_promotion(
        self, tmp_path: Path
    ) -> None:
        """Assert a moved-aside dump is put back when the day directory is missing."""
        aside = tmp_path / _scratch_name(REPLACED_SUFFIX)
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: first\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert (instance.backup_dir / "metadata").read_text() == (
            "Finished dump at: first\n"
        )
        assert not aside.exists()

    def test_discards_a_dump_left_aside_by_a_completed_promotion(
        self, tmp_path: Path
    ) -> None:
        """Assert the superseded copy is dropped when the day directory is present."""
        day_dir = _existing_dump(tmp_path)
        aside = tmp_path / _scratch_name(REPLACED_SUFFIX)
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: older\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert not aside.exists()
        assert _dir_snapshot(day_dir) == _FIRST_DUMP

    def test_discards_staging_directories_from_a_killed_run(
        self, tmp_path: Path
    ) -> None:
        """Assert abandoned staging directories are removed and the cleanup is logged."""
        stale = tmp_path / _scratch_name(PARTIAL_SUFFIX, "010000", _exited_pid())
        stale.mkdir()
        (stale / "sakila.film.sql").write_text("half a dump\n")
        aged = real_time.time() - 7200
        os.utime(stale, (aged, aged))
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert not stale.exists()
        assert any(stale.name in message for message in instance.logger.messages)

    def test_leaves_a_fresh_staging_directory_whose_name_carries_no_pid(
        self, tmp_path: Path
    ) -> None:
        """Assert the age grace covers a name no owning process can be read out of."""
        live = tmp_path / _scratch_name(PARTIAL_SUFFIX, "030000", "unknown")
        live.mkdir()
        (live / "sakila.film.sql").write_text("dump in flight\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert (live / "sakila.film.sql").read_text() == "dump in flight\n"

    def test_discards_a_fresh_staging_directory_whose_process_is_gone(
        self, tmp_path: Path
    ) -> None:
        """Assert a killed run's leftovers go at once rather than after the grace period.

        The grace period exists for names that carry no readable pid; a name whose
        pid is gone already answers the question.
        """
        stale = tmp_path / _scratch_name(PARTIAL_SUFFIX, "030000", _exited_pid())
        stale.mkdir()
        (stale / "sakila.film.sql").write_text("half a dump\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert not stale.exists()

    def test_restores_the_newest_moved_aside_dump_and_drops_the_rest(
        self, tmp_path: Path
    ) -> None:
        """Assert the copy published last is the one put back.

        Two runs can each have moved a dump aside for one day. Restoring by name
        would restore by run start time, which is not the order they published in,
        and the copies left over are then deleted against the restored day.
        """
        stale = tmp_path / _scratch_name(REPLACED_SUFFIX, "010000", _exited_pid())
        newest = tmp_path / _scratch_name(REPLACED_SUFFIX, "020000", _exited_pid())
        for aside, marker in ((stale, "first"), (newest, "second")):
            aside.mkdir()
            _write_dump(aside, marker)
        aged = real_time.time() - OLDER_THAN_ANY_GRACE_PERIOD
        os.utime(stale, (aged, aged))
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert _dir_snapshot(instance.backup_dir) == _SECOND_DUMP
        assert not _scratch_names(tmp_path)

    def test_keeps_a_staging_directory_its_process_is_still_writing_to(
        self, tmp_path: Path
    ) -> None:
        """Assert a live run's staging directory survives however old it looks.

        A directory's mtime does not advance while mydumper appends to a chunk file
        it already created, so age alone cannot tell a slow dump from a dead one.
        ``_process_started_at`` is pinned to the backdated mtime so the fake pid's
        actual start time (this test process, started long after ``aged``) does not
        make a genuine owner look recycled.
        """
        live = tmp_path / _scratch_name(PARTIAL_SUFFIX, "010000", os.getpid())
        live.mkdir()
        (live / "sakila.film.sql").write_text("dump in flight\n")
        aged = real_time.time() - OLDER_THAN_ANY_GRACE_PERIOD
        os.utime(live, (aged, aged))
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: aged

        instance._reclaim_interrupted_publish()

        assert (live / "sakila.film.sql").read_text() == "dump in flight\n"

    def test_discards_a_staging_directory_whose_pid_was_reused(
        self, tmp_path: Path
    ) -> None:
        """Assert a live pid that started after the directory is a different process.

        Pids get recycled; once one is, the next process to get it did not create
        the staging directory carrying it, however alive that pid looks.
        """
        stale = tmp_path / _scratch_name(PARTIAL_SUFFIX, "010000", os.getpid())
        stale.mkdir()
        (stale / "sakila.film.sql").write_text("half a dump\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: (
            real_time.time() + OLDER_THAN_ANY_GRACE_PERIOD
        )

        instance._reclaim_interrupted_publish()

        assert not stale.exists()

    def test_leaves_a_moved_aside_dump_alone_while_its_owner_is_still_publishing(
        self, tmp_path: Path
    ) -> None:
        """Assert a live owner's moved-aside dump is untouched, not restored early.

        A second run's reclaim pass can start inside the window ``_publish_backup``
        leaves open between its two renames. Restoring or dropping the first run's
        moved-aside copy while that run is still live races its own recovery path.

        The directory is shaped as ``_publish_backup`` leaves it: the name is the
        live run's, while the mtime is the previous run's last write, which predates
        the live run's start. Reading the owner's liveness off the mtime would call
        the live owner recycled here.
        """
        aside = tmp_path / _this_runs_scratch_name(REPLACED_SUFFIX)
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: previous\n")
        aged = real_time.time() - OLDER_THAN_ANY_GRACE_PERIOD
        os.utime(aside, (aged, aged))
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert aside.is_dir()
        assert not (tmp_path / real_time.strftime("%Y%m%d")).exists()

    def test_reads_a_moved_aside_owners_liveness_off_its_name_not_its_mtime(
        self, tmp_path: Path
    ) -> None:
        """Assert the owner's start is compared against the name's time, not the mtime.

        Pinned rather than read off ``/proc`` so the case holds on a host without
        it: the owner started after the previous run's last write and within the
        second its name records, which is the shape a live publication has.
        """
        owner_started = real_time.time()
        aside = tmp_path / _this_runs_scratch_name(
            REPLACED_SUFFIX, started_at=owner_started
        )
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: previous\n")
        aged = owner_started - OLDER_THAN_ANY_GRACE_PERIOD
        os.utime(aside, (aged, aged))
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: owner_started

        instance._reclaim_interrupted_publish()

        assert aside.is_dir()
        assert not (tmp_path / real_time.strftime("%Y%m%d")).exists()

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs procfs")
    def test_a_process_start_time_is_read_off_the_kernels_record(self) -> None:
        """Assert field 22 of ``/proc/<pid>/stat`` is turned into a Unix timestamp.

        The field index, the ``SC_CLK_TCK`` conversion and the ``CLOCK_BOOTTIME``
        epoch are the parts that can go wrong silently, so a reading for a child of
        known age has to land at its spawn. Any host this runs on stamps
        ``/proc/<pid>`` at about the spawn too, so the case cannot tell the two
        sources apart; why the field is read instead of the entry's own timestamps
        is documented on ``_process_started_at``.
        """
        instance, _, _ = _mydumper_instance(("_process_started_at",))
        spawned_at = real_time.time()
        child = subprocess.Popen(["sleep", "30"])
        try:
            real_time.sleep(2)
            started = instance._process_started_at(child.pid)
        finally:
            child.kill()
            child.wait()

        assert started is not None
        assert abs(started - spawned_at) < 1

    def test_restores_a_moved_aside_dump_whose_pid_was_reused(
        self, tmp_path: Path
    ) -> None:
        """Assert a moved-aside dump is put back once its owner's pid belongs to another.

        The process that took the pid started after the run named in the directory,
        so however alive the pid looks, the run that moved the dump aside is gone.
        """
        aside = tmp_path / _scratch_name(REPLACED_SUFFIX, "010000", os.getpid())
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: first\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: (
            real_time.time() + OLDER_THAN_ANY_GRACE_PERIOD
        )

        instance._reclaim_interrupted_publish()

        assert (instance.backup_dir / "metadata").read_text() == (
            "Finished dump at: first\n"
        )
        assert not aside.exists()

    def test_an_owner_that_started_mid_second_is_not_taken_for_a_reused_pid(
        self, tmp_path: Path
    ) -> None:
        """Assert the name's whole-second start time does not undercut a real owner.

        The name truncates the run's start time to the second while a process's
        start time does not, so an owner that started mid-second postdates its own
        name by a fraction.
        """
        aside = tmp_path / _scratch_name(REPLACED_SUFFIX, "010000", os.getpid())
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: live\n")
        named = real_time.mktime(
            real_time.strptime(f"{TODAY_STR}010000", "%Y%m%d%H%M%S")
        )
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: named + 0.5

        instance._reclaim_interrupted_publish()

        assert aside.is_dir()
        assert not instance.backup_dir.exists()

    def test_a_pid_it_cannot_signal_is_still_checked_for_reuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert a pid held by another user's process is not taken for a live owner.

        ``os.kill`` refuses to signal it, but its start time is readable all the same,
        and a start after the run named in the directory means the run is gone. Left
        unread, a crashed publication whose pid landed on a root-owned process would
        never be restored.
        """

        def refuse(_pid: int, _sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "kill", refuse)
        aside = tmp_path / _scratch_name(REPLACED_SUFFIX, "010000", os.getpid())
        aside.mkdir()
        (aside / "metadata").write_text("Finished dump at: first\n")
        stale = tmp_path / _scratch_name(PARTIAL_SUFFIX, "010000", os.getpid())
        stale.mkdir()
        (stale / "sakila.film.sql").write_text("half a dump\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)
        instance._process_started_at = lambda _pid: (
            real_time.time() + OLDER_THAN_ANY_GRACE_PERIOD
        )

        instance._reclaim_interrupted_publish()

        assert (instance.backup_dir / "metadata").read_text() == (
            "Finished dump at: first\n"
        )
        assert not _scratch_names(tmp_path)

    def test_never_restores_a_moved_aside_name_that_is_not_a_day(
        self, tmp_path: Path
    ) -> None:
        """Assert only a day-stamped scratch name can become a backup directory."""
        junk = tmp_path / f".sakila{REPLACED_SUFFIX}"
        junk.mkdir()
        (junk / "metadata").write_text("not a backup\n")
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert not (tmp_path / "sakila").exists()
        assert not junk.exists()

    def test_a_leftover_it_cannot_remove_does_not_fail_the_backup(
        self, tmp_path: Path
    ) -> None:
        """Assert an unremovable leftover is reported and the run carries on."""
        server_dir = tmp_path / "server"
        server_dir.mkdir()
        stale = server_dir / _scratch_name(PARTIAL_SUFFIX, "010000", _exited_pid())
        stale.mkdir()
        aged = real_time.time() - OLDER_THAN_ANY_GRACE_PERIOD
        os.utime(stale, (aged, aged))
        instance, _, _ = _dumper(server_dir, _RECLAIM_METHODS)
        server_dir.chmod(0o500)
        try:
            instance._reclaim_interrupted_publish()
        finally:
            server_dir.chmod(0o700)

        assert stale.is_dir()
        assert any(stale.name in message for message in instance.logger.warnings)

    def test_leaves_a_symlinked_leftover_alone(self, tmp_path: Path) -> None:
        """Assert a symlink planted in place of a leftover is neither followed nor moved."""
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep").write_text("untouched\n")
        link = tmp_path / _scratch_name(PARTIAL_SUFFIX, "020000", 1)
        link.symlink_to(target, target_is_directory=True)
        instance, _, _ = _dumper(tmp_path, _RECLAIM_METHODS)

        instance._reclaim_interrupted_publish()

        assert (target / "keep").read_text() == "untouched\n"


class TestScratchDirsAreInvisibleToRetention:
    """Cover retention's view of the staging and moved-aside directories."""

    def test_listing_ignores_scratch_directories(self, tmp_path: Path) -> None:
        """Assert only day-stamped directories are listed as backups."""
        _existing_dump(tmp_path)
        (tmp_path / _scratch_name(PARTIAL_SUFFIX, "030000", 1)).mkdir()
        (tmp_path / _scratch_name(REPLACED_SUFFIX)).mkdir()
        instance, _, _ = _dumper(tmp_path, ("_list_backups",))

        assert instance._list_backups(tmp_path) == [TODAY_STR]

    def test_purge_keeps_today_and_ignores_scratch_directories(
        self, tmp_path: Path
    ) -> None:
        """Assert purging by day count neither counts nor deletes scratch directories."""
        _existing_dump(tmp_path)
        _existing_dump(tmp_path, day="20260101")
        staging = tmp_path / _scratch_name(PARTIAL_SUFFIX, "030000", 1)
        staging.mkdir()
        instance, _, _ = _dumper(
            tmp_path, ("_list_backups", "_purge_old_backups", "_retained_days")
        )
        instance.daily_purge = 1
        instance.weekly_purge = 0

        purged = instance._purge_old_backups()

        assert purged == 1
        assert (tmp_path / TODAY_STR).is_dir()
        assert not (tmp_path / "20260101").exists()
        assert staging.is_dir()

    def test_purge_keeps_only_the_newest_weeklies(self, tmp_path: Path) -> None:
        """Assert the purge drops a Monday the weekly allowance no longer reaches.

        This is the rule the hardlink and ``--updated-since`` gates read, so the
        two have to be asserted against one another.
        """
        for day in ("20251222", "20251229", TODAY_STR):
            _existing_dump(tmp_path, day=day)
        instance, _, _ = _dumper(
            tmp_path, ("_list_backups", "_purge_old_backups", "_retained_days")
        )
        instance.daily_purge = 1
        instance.weekly_purge = 2

        purged = instance._purge_old_backups()

        assert purged == 1
        assert not (tmp_path / "20251222").exists()
        assert (tmp_path / "20251229").is_dir()
        assert (tmp_path / TODAY_STR).is_dir()


class TestScratchDirsAreInvisibleToUpload:
    """Cover the upload collector's view of the scratch directories."""

    def test_collector_skips_scratch_directories(self, tmp_path: Path) -> None:
        """Assert a staged or moved-aside dump is never queued for upload."""
        day_dir = _existing_dump(tmp_path)
        (day_dir / ".uploadme").touch()
        for name in (
            _scratch_name(PARTIAL_SUFFIX, "030000", 1),
            _scratch_name(REPLACED_SUFFIX),
        ):
            scratch_dir = tmp_path / name
            scratch_dir.mkdir()
            (scratch_dir / ".uploadme").touch()
        instance, _, _ = _mydumper_instance(("_collect_paths",))
        instance.full_backup_path = tmp_path
        instance.relative_backup_path = Path("mydumper") / "primary"
        instance.tmpdir_for_enc_upload = tmp_path / "_tmpdir"
        instance.paths = []

        instance._collect_paths()

        assert [Path(entry["source"]).name for entry in instance.paths] == [TODAY_STR]


class TestSameDayRerunEndToEnd:
    """Cover two dumps of one server inside a single calendar day."""

    def test_both_runs_complete_and_the_day_holds_the_newer_dump(
        self, tmp_path: Path
    ) -> None:
        """Assert a second same-day execution of one task publishes over the first.

        Each iteration is a whole execution: ``run`` reclaims, stages, dispatches
        the faked mydumper, publishes and repoints ``latest`` itself, so the
        ordering under test is the payload's rather than this test's.
        """
        for run_index, marker in ((1, "first"), (2, "second")):
            instance, _, record = _runner(
                tmp_path,
                popen=_dump_writer(marker=marker),
                work_dir_name=_scratch_name(
                    PARTIAL_SUFFIX, f"03000{run_index}", run_index
                ),
            )

            instance.run()

            assert record.purged is True
            assert not instance.logger.errors
            assert not any(
                "Directory is not empty" in message
                for message in instance.logger.messages
            )

        assert _dir_snapshot(tmp_path / TODAY_STR) == _SECOND_DUMP
        assert (tmp_path / "latest" / "metadata").is_file()
        assert not _scratch_names(tmp_path)


class TestRunIsolatesFailuresFromThePublishedDump:
    """Cover the produce-then-publish sequence the run drives."""

    def test_a_completed_run_publishes_and_leaves_no_scratch(
        self, tmp_path: Path
    ) -> None:
        """Assert the staged dump is promoted and the run's own scratch is gone."""
        instance, _, record = _runner(tmp_path)

        instance.run()

        assert _dir_snapshot(instance.backup_dir) == _SECOND_DUMP
        assert record.saved == instance.work_dir
        assert record.purged is True
        assert not _scratch_names(tmp_path)
        assert instance.latest_link.resolve() == instance.backup_dir

    def test_a_failed_dump_keeps_the_published_dump(self, tmp_path: Path) -> None:
        """Assert a dump that raises costs only the staged copy."""
        day_dir = _existing_dump(tmp_path)
        instance, backup_error, record = _runner(tmp_path)

        def _raise() -> None:
            raise backup_error("Mydumper failed (1)")

        instance._run_backup_cmd = _raise

        with pytest.raises(backup_error):
            instance.run()

        assert _dir_snapshot(day_dir) == _FIRST_DUMP
        assert not _scratch_names(tmp_path)
        assert record.saved is None

    def test_a_failure_after_the_dump_keeps_the_published_dump(
        self, tmp_path: Path
    ) -> None:
        """Assert a raising space-saving pass costs only the staged copy."""
        day_dir = _existing_dump(tmp_path)
        instance, _, record = _runner(tmp_path)

        def _raise() -> None:
            raise RuntimeError("hardlinking failed")

        instance._save_disk_space = _raise

        with pytest.raises(RuntimeError):
            instance.run()

        assert _dir_snapshot(day_dir) == _FIRST_DUMP
        assert not _scratch_names(tmp_path)
        assert record.purged is False

    def test_a_failed_encryption_keeps_the_published_dump(self, tmp_path: Path) -> None:
        """Assert the GPG branch also fails without touching the published dump."""
        day_dir = _existing_dump(tmp_path)
        instance, _, record = _runner(
            tmp_path, post_run_encrypt=True, encrypt_error=RuntimeError("gpg failed")
        )

        with pytest.raises(RuntimeError):
            instance.run()

        assert record.encrypted == instance.work_dir
        assert _dir_snapshot(day_dir) == _FIRST_DUMP
        assert not _scratch_names(tmp_path)

    def test_the_encryption_branch_runs_against_the_staging_directory(
        self, tmp_path: Path
    ) -> None:
        """Assert encryption is applied before the dump is published, not after."""
        instance, _, record = _runner(tmp_path, post_run_encrypt=True)
        staged = instance.work_dir

        instance.run()

        assert record.encrypted == staged
        assert instance.report_options["encryption"] == "gpg"

    def test_a_rejected_free_space_check_stages_nothing(self, tmp_path: Path) -> None:
        """Assert a guard that aborts the run leaves no staging directory behind."""
        instance, backup_error, _ = _runner(tmp_path, free_space=False)
        instance.check_disk_space = True

        with pytest.raises(backup_error):
            instance.run()

        assert not _scratch_names(tmp_path)


class TestSpaceSavingTargetsTheStagedDump:
    """Cover the checksumming and hardlinking that precede publication."""

    def test_hardlinks_the_previous_day_into_the_staging_directory(
        self, tmp_path: Path
    ) -> None:
        """Assert both passes are pointed at the staged dump, not the day directory."""
        _existing_dump(tmp_path, day=YESTERDAY_STR)
        instance, record = _saver(tmp_path)

        instance._save_disk_space()

        assert record.checksummed == instance.work_dir
        assert record.hardlinked == (instance.prev_backup_dir, instance.work_dir)

    def test_skips_a_previous_day_this_run_purges(self, tmp_path: Path) -> None:
        """Assert no dedup is attempted against a day the purge is about to remove.

        The purge runs after publication now, so hardlinking into a doomed day would
        leave its removal freeing nothing.
        """
        _existing_dump(tmp_path, day=YESTERDAY_STR)
        instance, record = _saver(tmp_path, daily_purge=1, weekly_purge=0)

        instance._save_disk_space()

        assert record.checksummed == instance.work_dir
        assert record.hardlinked is None

    def test_keeps_a_previous_day_the_weeklies_retain(self, tmp_path: Path) -> None:
        """Assert a Monday the weeklies keep is still worth hardlinking against.

        Today is a Monday too and takes the newest weekly slot, so the run has to
        keep two of them for the previous one to survive.
        """
        monday = "20251229"
        _existing_dump(tmp_path, day=monday)
        instance, record = _saver(tmp_path, daily_purge=1, weekly_purge=2)
        instance.prev_backup_dir = tmp_path / monday

        instance._save_disk_space()

        assert record.hardlinked == (instance.prev_backup_dir, instance.work_dir)

    def test_skips_a_monday_the_weeklies_no_longer_reach(self, tmp_path: Path) -> None:
        """Assert only the Mondays the purge actually keeps count as retained.

        Retention keeps the newest few Mondays, not every Monday, so an older one
        is removed like any other day and dedup against it would be lost.
        """
        older_monday = "20251222"
        _existing_dump(tmp_path, day=older_monday)
        _existing_dump(tmp_path, day="20251229")
        instance, record = _saver(tmp_path, daily_purge=1, weekly_purge=1)
        instance.prev_backup_dir = tmp_path / older_monday

        instance._save_disk_space()

        assert record.hardlinked is None


class TestUpdatedSinceBaseIsRetained:
    """Cover the incremental base the ``--updated-since`` validation settles on."""

    @staticmethod
    def _validator(tmp_path: Path, *, daily_purge: int) -> tuple[Any, str]:
        """Build an instance carrying the validation pass over real calendar days.

        The pass walks back from the current date, so the frozen clock the rest of
        this module uses cannot drive it.

        :param tmp_path: The server directory the run works in.
        :param daily_purge: How many daily backups retention keeps.
        :return: The instance and the day name of the dump laid down for yesterday.
        """
        today = datetime.date(*real_time.localtime()[:3])
        yesterday = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")
        _existing_dump(tmp_path, day=yesterday)
        instance, _, _ = _dumper(
            tmp_path,
            (
                "_validate_updated_since",
                "_day_is_retained",
                "_retained_days",
                "_list_backups",
            ),
        )
        instance.today_str = today.strftime("%Y%m%d")
        instance.updated_since = UPDATED_SINCE_DAYS
        instance.daily_purge = daily_purge
        instance.weekly_purge = 0
        return instance, yesterday

    def test_selects_a_previous_day_retention_keeps(self, tmp_path: Path) -> None:
        """Assert a base that survives the purge is still selected."""
        instance, yesterday = self._validator(tmp_path, daily_purge=7)

        instance._validate_updated_since()

        assert instance.valid_prev_backup_dir == str(tmp_path / yesterday)
        assert instance.updated_since == UPDATED_SINCE_DAYS

    def test_rejects_a_previous_day_this_run_purges(self, tmp_path: Path) -> None:
        """Assert a doomed base is passed over and the option switched off.

        Hardlinking listed files out of a day this run removes leaves the removal
        freeing nothing, because the staged dump holds those inodes.
        """
        instance, _ = self._validator(tmp_path, daily_purge=1)

        instance._validate_updated_since()

        assert instance.valid_prev_backup_dir is None
        assert instance.updated_since == 0
        assert any(
            "no valid previous backup" in message
            for message in instance.logger.warnings
        )


class TestDoubleSpaceDecision:
    """Cover the free-space requirement a replacing run imposes."""

    def test_requires_double_space_when_the_day_already_holds_a_dump(
        self, tmp_path: Path
    ) -> None:
        """Assert a replacing run asks for room for both copies."""
        _existing_dump(tmp_path)
        instance, _, _ = _dumper(tmp_path, ("_needs_double_space",))

        assert instance._needs_double_space() is True

    def test_requires_double_space_for_the_tmpdir_encryption_path(
        self, tmp_path: Path
    ) -> None:
        """Assert the pre-existing tmpdir-encryption requirement still applies."""
        instance, _, _ = _dumper(tmp_path, ("_needs_double_space",))
        instance.encrypt_using_tmpdir = True

        assert instance._needs_double_space() is True

    def test_first_run_of_the_day_needs_single_space(self, tmp_path: Path) -> None:
        """Assert a first run keeps the original single-copy requirement."""
        instance, _, _ = _dumper(tmp_path, ("_needs_double_space",))

        assert instance._needs_double_space() is False
