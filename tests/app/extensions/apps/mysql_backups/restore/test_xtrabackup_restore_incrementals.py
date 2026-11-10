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

"""Tests for how the XtraBackup restore finds and checks the incremental chain.

The restore payload's real ``list_incrementals``, ``_backup_chain``,
``_check_backup_chain`` and ``run`` are lifted out of the payload and driven
against real directories and stub ``aws``/``s3cmd``/``gcloud``/``ssh``
executables, so only the process boundary is faked.
"""

import ast
import configparser
import fcntl
import functools
import glob
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
    restore_payload_tree,
)

#: Shared with every lifted method, so ``pytest.raises`` matches what they raise.
_BackupError = type("BackupError", (Exception,), {})

_INCS = ("2026-01-02_00-00-00", "2026-01-03_00-00-00", "2026-01-04_00-00-00")
_FULL = "2026-01-01_00-00-00"
_SSH_HOST = "backuphost"
_SSH_USER = "percona"
_SSH_PORT = 2222
_PXB_INFO_LOCK = pathlib.Path(tempfile.gettempdir()) / "pxb_info.test-lock"


@dataclass(frozen=True, kw_only=True, slots=True)
class Stub:
    """Describe what one stub executable prints and how it exits."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class StubBin:
    """Write stub executables that record their argv and answer from a script."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.record = root / "argv.jsonl"

    def install(self, name: str, stub: Stub) -> pathlib.Path:
        """Write ``name`` into the stub bin dir and return its path.

        :param name: The executable's file name.
        :param stub: What the executable prints and how it exits.
        :return: The path of the written executable.
        """
        path = self.bin_dir / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"with open({str(self.record)!r}, 'a') as fh:\n"
            "    fh.write(json.dumps(sys.argv) + '\\n')\n"
            f"sys.stdout.write({stub.stdout!r})\n"
            f"sys.stderr.write({stub.stderr!r})\n"
            f"sys.exit({stub.returncode})\n"
        )
        path.chmod(0o755)
        return path

    def calls(self) -> list[list[str]]:
        """Return the argv of every stub run, in order."""
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


@pytest.fixture
def stubs(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> StubBin:
    """Return a stub bin dir that is first on ``PATH``."""
    stub_bin = StubBin(tmp_path / "stubs")
    monkeypatch.setenv("PATH", f"{stub_bin.bin_dir}{os.pathsep}{os.environ['PATH']}")
    return stub_bin


def _lift(
    name: str,
    extra: Mapping[str, object] | None = None,
    *,
    class_name: str = "Xtrabackup",
) -> Callable[..., object]:
    """Return the restore payload's ``<class_name>.<name>`` lifted to a function.

    :param name: The method to lift.
    :param extra: Globals the method reads beyond the whitelisted constants.
    :param class_name: The payload class that defines the method.
    :return: The lifted method, called as ``fn(self, ...)``.
    """
    return payload_method(
        class_name,
        name,
        extra_namespace={"BackupError": _BackupError, **(extra or {})},
        payload_path=RESTORE_PAYLOAD_PATH,
    )


def _restorer(stub_bin: StubBin, *, s3_tool: str = "awscli") -> types.SimpleNamespace:
    """Return a restorer whose ``list_incrementals`` is the real lifted method."""
    restorer = types.SimpleNamespace(
        backup_code="X",
        s3_tool=s3_tool,
        ssh_port=_SSH_PORT,
        ssh_user=_SSH_USER,
        skip_incrementals=False,
        logger=Recorder(),
    )
    list_incrementals = _lift(
        "list_incrementals",
        {
            "AWS_BIN": str(stub_bin.bin_dir / "aws"),
            "GCLOUD_BIN": str(stub_bin.bin_dir / "gcloud"),
            "INSTALL_DIR": str(stub_bin.root),
            "shlex": shlex,
        },
    )
    restorer.list_incrementals = functools.partial(list_incrementals, restorer)
    return restorer


def _make_local_chain(root: pathlib.Path, *incs: str) -> pathlib.Path:
    """Create a full backup dir and its incremental dirs; return the full's path."""
    full = root / _FULL
    full.mkdir(parents=True)
    for inc in incs:
        (root / f"{_FULL}_incrementals" / inc).mkdir(parents=True)
    return full


# Each scheme's listing, rendered the way its tool prints it, with noise the
# parser has to drop: a gcloud folder placeholder, a stray file, a non-timestamp dir.
_UNORDERED = (_INCS[2], _INCS[0], _INCS[1])
_PRESENT_OUTPUT = {
    "s3/awscli": "".join(f"                           PRE {i}/\n" for i in _UNORDERED)
    + "2026-01-05 10:00:00        12 notes.txt\n"
    + "                           PRE scratch/\n",
    "s3/s3cmd": "".join(
        f"                          DIR  s3://b/p/{_FULL}_incrementals/{i}/\n"
        for i in _UNORDERED
    )
    + f"2026-01-05 10:00        12  s3://b/p/{_FULL}_incrementals/notes.txt\n",
    "gs": "".join(f"gs://b/p/{_FULL}_incrementals/{i}/\n" for i in _UNORDERED)
    + f"gs://b/p/{_FULL}_incrementals/{_INCS[0]}_$folder$\n"
    + f"gs://b/p/{_FULL}_incrementals/scratch/\n",
    "ssh": "".join(f"{i}\n" for i in _UNORDERED) + "notes.txt\nscratch\n",
}
_ABSENT = {
    "s3/awscli": Stub(returncode=1),
    "s3/s3cmd": Stub(),
    "gs": Stub(
        stderr="ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\n",
        returncode=1,
    ),
    "ssh": Stub(),
}
_FAILED = {
    "s3/awscli": Stub(
        stderr="An error occurred (AccessDenied) when calling the "
        "ListObjectsV2 operation: Access Denied\n",
        returncode=255,
    ),
    "s3/s3cmd": Stub(stderr="ERROR: Access to bucket 'b' was denied\n", returncode=77),
    "gs": Stub(
        stderr="ERROR: (gcloud.storage.ls) gs://b not found: 404.\n", returncode=1
    ),
    "ssh": Stub(
        stderr="ssh: connect to host backuphost: Connection refused\n", returncode=255
    ),
}
_TOOL = {"s3/awscli": "aws", "s3/s3cmd": "s3cmd", "gs": "gcloud", "ssh": "ssh"}
_SOURCE = {
    "s3/awscli": f"s3://b/p/{_FULL}",
    "s3/s3cmd": f"s3://b/p/{_FULL}",
    "gs": f"gs://b/p/{_FULL}",
    "ssh": f"{_SSH_HOST}:/backups/{_FULL}",
}
_REMOTE_SCHEMES = tuple(_TOOL)


def _remote_restorer(
    stub_bin: StubBin, scheme: str, stub: Stub
) -> types.SimpleNamespace:
    """Install ``scheme``'s listing tool answering ``stub`` and return a restorer."""
    stub_bin.install(_TOOL[scheme], stub)
    return _restorer(stub_bin, s3_tool="s3cmd" if scheme == "s3/s3cmd" else "awscli")


class TestListIncrementalsRemote:
    """Cover ``list_incrementals`` over every scheme that shells out."""

    @pytest.mark.parametrize("scheme", _REMOTE_SCHEMES)
    def test_returns_the_sorted_chain_without_noise(
        self, stubs: StubBin, scheme: str
    ) -> None:
        """Return the timestamp dirs as sorted ``str`` names, dropping the rest."""
        restorer = _remote_restorer(stubs, scheme, Stub(stdout=_PRESENT_OUTPUT[scheme]))

        assert restorer.list_incrementals(_SOURCE[scheme]) == list(_INCS)

    @pytest.mark.parametrize("scheme", _REMOTE_SCHEMES)
    def test_returns_nothing_for_an_absent_chain(
        self, stubs: StubBin, scheme: str
    ) -> None:
        """Read the tool's own "nothing there" answer as an empty chain."""
        restorer = _remote_restorer(stubs, scheme, _ABSENT[scheme])

        assert restorer.list_incrementals(_SOURCE[scheme]) == []

    @pytest.mark.parametrize("scheme", _REMOTE_SCHEMES)
    def test_raises_on_a_failed_listing(self, stubs: StubBin, scheme: str) -> None:
        """Fail loudly, naming the listed location and the tool's error."""
        stub = _FAILED[scheme]
        restorer = _remote_restorer(stubs, scheme, stub)

        with pytest.raises(_BackupError) as exc_info:
            restorer.list_incrementals(_SOURCE[scheme])

        message = str(exc_info.value)
        assert f"{_FULL}_incrementals" in message
        assert stub.stderr.strip() in message

    @pytest.mark.parametrize("scheme", ["s3/awscli", "s3/s3cmd", "gs"])
    def test_lists_the_prefix_children_with_a_trailing_slash(
        self, stubs: StubBin, scheme: str
    ) -> None:
        """Pass ``<source>_incrementals/`` as the listed URL, slash included."""
        restorer = _remote_restorer(stubs, scheme, _ABSENT[scheme])

        restorer.list_incrementals(_SOURCE[scheme])

        assert stubs.calls()[0][-1] == f"{_SOURCE[scheme]}_incrementals/"

    def test_lists_a_gs_source_with_gcloud_not_ssh(self, stubs: StubBin) -> None:
        """Send a ``gs://`` source to gcloud, never down the ``host:path`` branch."""
        stubs.install("ssh", Stub())
        restorer = _remote_restorer(stubs, "gs", _ABSENT["gs"])

        restorer.list_incrementals(_SOURCE["gs"])

        assert [pathlib.Path(argv[0]).name for argv in stubs.calls()] == ["gcloud"]

    def test_raises_backup_error_for_a_missing_binary(self, stubs: StubBin) -> None:
        """Turn a listing binary that does not exist into a ``BackupError``."""
        restorer = _restorer(stubs)

        with pytest.raises(_BackupError, match=f"{_FULL}_incrementals"):
            restorer.list_incrementals(_SOURCE["s3/awscli"])


class TestListIncrementalsSsh:
    """Cover the SSH listing's remote command."""

    def _remote_command(self, stubs: StubBin, path: str) -> str:
        """List ``host:path`` over the ssh stub and return the remote command."""
        restorer = _remote_restorer(stubs, "ssh", Stub())
        restorer.list_incrementals(f"{_SSH_HOST}:{path}")
        argv = stubs.calls()[0]
        assert argv[1:-1] == [
            "-p",
            str(_SSH_PORT),
            "-l",
            _SSH_USER,
            "-i",
            f"/home/{_SSH_USER}/.ssh/id_rsa",
            _SSH_HOST,
        ]
        return argv[-1]

    def test_quotes_a_path_containing_a_space(self, stubs: StubBin) -> None:
        """Quote the remote path so a space cannot split it into two arguments."""
        command = self._remote_command(stubs, "/backups/my dir/F")

        assert shlex.quote("/backups/my dir/F_incrementals") in command

    def test_a_refused_sudo_cannot_read_as_an_absent_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Exit non-zero when ``sudo -n`` is refused, even for a missing dir."""
        command = self._remote_command(stubs, str(tmp_path / "missing" / "F"))
        stubs.install(
            "sudo", Stub(stderr="sudo: a password is required\n", returncode=1)
        )

        result = subprocess.run(
            ["/bin/sh", "-c", command], capture_output=True, check=False
        )

        assert "sudo -n test ! -e" in command
        assert "sudo -n ls -1" in command
        assert result.returncode != 0


class TestListIncrementalsLocal:
    """Cover ``list_incrementals`` on a local source."""

    def test_returns_the_sorted_chain_without_noise(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """List real dirs, dropping files and non-timestamp names."""
        full = _make_local_chain(tmp_path, *_UNORDERED, "scratch")
        (tmp_path / f"{_FULL}_incrementals" / "notes.txt").write_text("x")

        assert _restorer(stubs).list_incrementals(str(full)) == list(_INCS)

    def test_returns_nothing_for_an_absent_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Read a missing ``_incrementals`` dir as an empty chain."""
        full = _make_local_chain(tmp_path)

        assert _restorer(stubs).list_incrementals(str(full)) == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 dir")
    def test_raises_on_an_unreadable_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Fail loudly on a chain dir that exists but cannot be listed."""
        full = _make_local_chain(tmp_path, *_INCS)
        inc_dir = tmp_path / f"{_FULL}_incrementals"
        inc_dir.chmod(0o000)
        try:
            with pytest.raises(_BackupError, match=f"{_FULL}_incrementals"):
                _restorer(stubs).list_incrementals(str(full))
        finally:
            inc_dir.chmod(0o755)


def _chain_restorer(
    stubs: StubBin, source: str, *, skip_incrementals: bool = False
) -> types.SimpleNamespace:
    """Return a restorer with the real ``_backup_chain`` bound for ``source``."""
    restorer = _restorer(stubs)
    restorer.backup_source = source
    restorer.skip_incrementals = skip_incrementals
    restorer.backup_chain = functools.partial(_lift("_backup_chain"), restorer)
    return restorer


class TestBackupChain:
    """Cover how ``_backup_chain`` turns the picked source into a restore list."""

    def test_full_source_restores_the_whole_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Return the full backup followed by every incremental in order."""
        full = str(_make_local_chain(tmp_path, *_INCS))

        assert _chain_restorer(stubs, full).backup_chain() == [
            full,
            *(f"{full}_incrementals/{inc}" for inc in _INCS),
        ]

    def test_full_source_with_a_trailing_slash_lists_beside_the_full(
        self, stubs: StubBin
    ) -> None:
        """Strip the S3 catalog's trailing ``/`` before deriving the chain prefix."""
        stubs.install("aws", Stub(stdout=_PRESENT_OUTPUT["s3/awscli"]))
        full = _SOURCE["s3/awscli"]

        chain = _chain_restorer(stubs, full + "/").backup_chain()

        assert chain == [full, *(f"{full}_incrementals/{inc}" for inc in _INCS)]
        assert stubs.calls()[0][-1] == f"{full}_incrementals/"

    def test_incremental_source_restores_up_to_the_picked_run(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Restore the full and the chain up to and including the picked run."""
        full = str(_make_local_chain(tmp_path, *_INCS))

        chain = _chain_restorer(stubs, f"{full}_incrementals/{_INCS[1]}").backup_chain()

        assert chain == [full, *(f"{full}_incrementals/{inc}" for inc in _INCS[:2])]

    def test_incremental_source_with_a_trailing_slash_is_truncated(
        self, stubs: StubBin
    ) -> None:
        """Detect an S3 catalog incremental source despite its trailing ``/``."""
        stubs.install("aws", Stub(stdout=_PRESENT_OUTPUT["s3/awscli"]))
        full = _SOURCE["s3/awscli"]

        chain = _chain_restorer(
            stubs, f"{full}_incrementals/{_INCS[0]}/"
        ).backup_chain()

        assert chain == [full, f"{full}_incrementals/{_INCS[0]}"]

    def test_incremental_source_missing_from_the_listing_raises(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Fail, naming the picked run, when the chain does not contain it."""
        full = str(_make_local_chain(tmp_path, *_INCS[:2]))
        restorer = _chain_restorer(stubs, f"{full}_incrementals/{_INCS[2]}")

        with pytest.raises(_BackupError, match=_INCS[2]):
            restorer.backup_chain()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 dir")
    def test_skip_incrementals_restores_the_full_without_listing(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Restore the full alone and never list, as an unreadable chain proves."""
        full = str(_make_local_chain(tmp_path, *_INCS))
        inc_dir = tmp_path / f"{_FULL}_incrementals"
        inc_dir.chmod(0o000)
        try:
            chain = _chain_restorer(stubs, full, skip_incrementals=True).backup_chain()
        finally:
            inc_dir.chmod(0o755)

        assert chain == [full]

    def test_skip_incrementals_refuses_an_incremental_source(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Refuse to restore an incremental run while skipping incrementals."""
        full = str(_make_local_chain(tmp_path, *_INCS))
        restorer = _chain_restorer(
            stubs, f"{full}_incrementals/{_INCS[0]}", skip_incrementals=True
        )

        with pytest.raises(_BackupError, match="Skip incrementals"):
            restorer.backup_chain()

    def test_full_source_without_a_chain_restores_the_full(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Restore the full alone when no ``_incrementals`` dir exists."""
        full = str(_make_local_chain(tmp_path))

        assert _chain_restorer(stubs, full).backup_chain() == [full]

    def test_logs_the_incremental_count(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Say how many incrementals the restore applies and where it stops."""
        full = str(_make_local_chain(tmp_path, *_INCS))
        restorer = _chain_restorer(stubs, f"{full}_incrementals/{_INCS[1]}")

        restorer.backup_chain()

        assert f"Restoring {full} with 2 incremental(s) up to {_INCS[1]}" in (
            restorer.logger.infos
        )


def _my_config_parser() -> type:
    """Return the payload's own ``MyConfigParser`` class."""
    class_node = next(
        node
        for node in restore_payload_tree().body
        if isinstance(node, ast.ClassDef) and node.name == "MyConfigParser"
    )
    namespace = {"configparser": configparser, "io": io}
    exec(
        compile(
            ast.Module(body=[class_node], type_ignores=[]),
            str(RESTORE_PAYLOAD_PATH),
            "exec",
        ),
        namespace,
    )
    return cast("type", namespace["MyConfigParser"])


@pytest.fixture
def _pxb_info_lock() -> Iterator[None]:
    """Serialize tests that run ``backup_info``, which uses a fixed ``/tmp`` dir."""
    with _PXB_INFO_LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_info(path: pathlib.Path, *, incremental: bool | None) -> None:
    """Write an ``xtrabackup_info`` into ``path``, or none when ``incremental`` is ``None``."""
    path.mkdir(parents=True, exist_ok=True)
    if incremental is not None:
        (path / "xtrabackup_info").write_text(
            "tool_version = 8.0.35-30\n"
            "server_version = 8.0.35-27\n"
            f"incremental = {'Y' if incremental else 'N'}\n"
        )


def _info_restorer(
    tmp_path: pathlib.Path, chain: list[bool | None]
) -> tuple[types.SimpleNamespace, list[str]]:
    """Build a local chain whose elements report ``chain`` and a restorer over it.

    :param tmp_path: Where the chain is created.
    :param chain: Per element, its ``incremental`` flag, or ``None`` for no info file.
    :return: The restorer and the chain's paths, full first.
    """
    full = tmp_path / _FULL
    paths = [full] + [tmp_path / f"{_FULL}_incrementals" / i for i in _INCS]
    paths = paths[: len(chain)]
    for path, incremental in zip(paths, chain, strict=True):
        _write_info(path, incremental=incremental)
    restorer = types.SimpleNamespace(
        backup_code="X",
        backup_source=str(full),
        xb_log_file=str(tmp_path / "xb.log"),
        decrypt_decompress=lambda _path: None,
        logger=Recorder(),
    )
    extra = {"shutil": shutil, "glob": glob, "MyConfigParser": _my_config_parser()}
    restorer.local_copy = functools.partial(
        _lift("local_copy", extra, class_name="Restore"), restorer
    )
    restorer.backup_info = functools.partial(_lift("backup_info", extra), restorer)
    restorer.check_backup_chain = functools.partial(
        _lift("_check_backup_chain", {"configparser": configparser}), restorer
    )
    return restorer, [str(path) for path in paths]


@pytest.mark.usefixtures("_pxb_info_lock")
class TestCheckBackupChain:
    """Cover the chain pre-flight that runs before anything destructive."""

    def test_accepts_a_valid_chain_and_restores_the_source(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Pass a full plus incrementals, leaving the source on the full."""
        restorer, paths = _info_restorer(tmp_path, [False, True, True, True])

        restorer.check_backup_chain(paths)

        assert restorer.backup_source == paths[0]

    def test_rejects_an_element_without_xtrabackup_info(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Name the element whose ``xtrabackup_info`` is missing."""
        restorer, paths = _info_restorer(tmp_path, [False, True, None])

        with pytest.raises(_BackupError, match=paths[2]):
            restorer.check_backup_chain(paths)

    def test_rejects_a_later_element_that_is_not_incremental(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Refuse a chain member that reports itself as a full backup."""
        restorer, paths = _info_restorer(tmp_path, [False, True, False])

        with pytest.raises(_BackupError, match=paths[2]):
            restorer.check_backup_chain(paths)

    def test_rejects_an_incremental_base(self, tmp_path: pathlib.Path) -> None:
        """Refuse a chain whose first element is itself incremental."""
        restorer, paths = _info_restorer(tmp_path, [True, True])

        with pytest.raises(_BackupError, match=paths[0]):
            restorer.check_backup_chain(paths)


@pytest.mark.usefixtures("_pxb_info_lock")
class TestRunChecksTheChainFirst:
    """Cover ``run`` refusing a bad chain before it stops mysqld."""

    def test_does_not_stop_mysqld_for_a_broken_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Raise before ``signal_mysqld`` runs when a later element is unreadable."""
        restorer, paths = _info_restorer(tmp_path, [False, True, None])
        signals: list[str] = []
        restorer.signal_mysqld = signals.append
        restorer.logging_dir = str(tmp_path)
        restorer.pre_script = None
        restorer.continue_replication = False
        restorer.is_stream = False
        restorer.skip_incrementals = False
        restorer.list_incrementals = _restorer(stubs).list_incrementals
        restorer._backup_chain = functools.partial(_lift("_backup_chain"), restorer)
        restorer._check_backup_chain = restorer.check_backup_chain
        run = _lift("run", {"time": time})

        with pytest.raises(_BackupError, match=paths[2]):
            run(restorer)

        assert signals == []
