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
import sys
import tempfile
import time
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_function,
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

    def install(self, name: str, *answers: Stub) -> pathlib.Path:
        """Write ``name`` into the stub bin dir and return its path.

        :param name: The executable's file name.
        :param answers: What each successive run prints and how it exits; runs past
            the last answer repeat it.
        :return: The path of the written executable.
        """
        path = self.bin_dir / name
        script = [(a.stdout, a.stderr, a.returncode) for a in answers]
        counter = self.root / f"{name}.count"
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(self.record)!r}, 'a') as fh:\n"
            "    fh.write(json.dumps(sys.argv) + '\\n')\n"
            f"counter = {str(counter)!r}\n"
            "n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
            "open(counter, 'w').write(str(n + 1))\n"
            f"script = {script!r}\n"
            "out, err, code = script[min(n, len(script) - 1)]\n"
            "sys.stdout.write(out)\n"
            "sys.stderr.write(err)\n"
            "sys.exit(code)\n"
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


def _rsync_rsh() -> Callable[..., str]:
    """Return the payload's module-level ``rsync_rsh``, with ``shlex`` supplied."""
    rsync_rsh = load_function("rsync_rsh", payload_path=RESTORE_PAYLOAD_PATH)
    rsync_rsh.__globals__["shlex"] = shlex
    return cast("Callable[..., str]", rsync_rsh)


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
            "rsync_rsh": _rsync_rsh(),
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


def _rsync_line(name: str, *, is_dir: bool = True) -> str:
    """Return one ``rsync --list-only`` line for ``name``."""
    mode = "drwxr-xr-x" if is_dir else "-rw-r--r--"
    return f"{mode}          4,096 2026/01/05 10:00:00 {name}\n"


# Each scheme's listing, rendered the way its tool prints it, with noise the
# parser has to drop: a gcloud folder placeholder, a stray file, a non-timestamp dir.
# SSH answers twice: first the full's parent dir, then the chain dir.
_UNORDERED = (_INCS[2], _INCS[0], _INCS[1])
_INC_DIR_NAME = f"{_FULL}_incrementals"
_SSH_PARENT_WITH_CHAIN = Stub(
    stdout=_rsync_line(".") + _rsync_line(_FULL) + _rsync_line(_INC_DIR_NAME)
)
_PRESENT = {
    "s3/awscli": (
        Stub(
            stdout="".join(f"                           PRE {i}/\n" for i in _UNORDERED)
            + "2026-01-05 10:00:00        12 notes.txt\n"
            + "                           PRE scratch/\n"
        ),
    ),
    "s3/s3cmd": (
        Stub(
            stdout="".join(
                f"                          DIR  s3://b/p/{_INC_DIR_NAME}/{i}/\n"
                for i in _UNORDERED
            )
            + f"2026-01-05 10:00        12  s3://b/p/{_INC_DIR_NAME}/notes.txt\n"
        ),
    ),
    "gs": (
        Stub(
            stdout="".join(f"gs://b/p/{_INC_DIR_NAME}/{i}/\n" for i in _UNORDERED)
            + f"gs://b/p/{_INC_DIR_NAME}/{_INCS[0]}_$folder$\n"
            + f"gs://b/p/{_INC_DIR_NAME}/scratch/\n"
        ),
    ),
    "ssh": (
        _SSH_PARENT_WITH_CHAIN,
        Stub(
            stdout=_rsync_line(".")
            + "".join(_rsync_line(i) for i in _UNORDERED)
            + _rsync_line("notes.txt", is_dir=False)
            + _rsync_line("scratch")
        ),
    ),
}
_ABSENT = {
    "s3/awscli": (Stub(returncode=1),),
    "s3/s3cmd": (Stub(),),
    "gs": (
        Stub(
            stderr="ERROR: (gcloud.storage.ls) One or more URLs matched no objects.\n",
            returncode=1,
        ),
    ),
    "ssh": (Stub(stdout=_rsync_line(".") + _rsync_line(_FULL)),),
}
_FAILED = {
    "s3/awscli": (
        Stub(
            stderr="An error occurred (AccessDenied) when calling the "
            "ListObjectsV2 operation: Access Denied\n",
            returncode=255,
        ),
    ),
    "s3/s3cmd": (
        Stub(stderr="ERROR: Access to bucket 'b' was denied\n", returncode=77),
    ),
    "gs": (
        Stub(
            stderr="ERROR: (gcloud.storage.ls) gs://b not found: 404.\n", returncode=1
        ),
    ),
    "ssh": (
        Stub(
            stderr="ssh: connect to host backuphost port 2222: Connection refused\n",
            returncode=255,
        ),
    ),
}
_TOOL = {"s3/awscli": "aws", "s3/s3cmd": "s3cmd", "gs": "gcloud", "ssh": "rsync"}
_SOURCE = {
    "s3/awscli": f"s3://b/p/{_FULL}",
    "s3/s3cmd": f"s3://b/p/{_FULL}",
    "gs": f"gs://b/p/{_FULL}",
    "ssh": f"{_SSH_HOST}:/backups/{_FULL}",
}
_REMOTE_SCHEMES = tuple(_TOOL)


def _remote_restorer(
    stub_bin: StubBin, scheme: str, answers: tuple[Stub, ...]
) -> types.SimpleNamespace:
    """Install ``scheme``'s listing tool giving ``answers`` and return a restorer."""
    stub_bin.install(_TOOL[scheme], *answers)
    return _restorer(stub_bin, s3_tool="s3cmd" if scheme == "s3/s3cmd" else "awscli")


class TestListIncrementalsRemote:
    """Cover ``list_incrementals`` over every scheme that shells out."""

    @pytest.mark.parametrize("scheme", _REMOTE_SCHEMES)
    def test_returns_the_sorted_chain_without_noise(
        self, stubs: StubBin, scheme: str
    ) -> None:
        """Return the timestamp dirs as sorted ``str`` names, dropping the rest."""
        restorer = _remote_restorer(stubs, scheme, _PRESENT[scheme])

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
        answers = _FAILED[scheme]
        restorer = _remote_restorer(stubs, scheme, answers)

        with pytest.raises(_BackupError) as exc_info:
            restorer.list_incrementals(_SOURCE[scheme])

        message = str(exc_info.value)
        assert _INC_DIR_NAME in message
        assert answers[0].stderr.strip() in message

    def test_raises_on_an_awscli_exit_1_with_an_error_message(
        self, stubs: StubBin
    ) -> None:
        """Read awscli's exit 1 as an absent chain only when stderr is empty."""
        restorer = _remote_restorer(
            stubs,
            "s3/awscli",
            (Stub(stderr="Could not connect to the endpoint URL\n", returncode=1),),
        )

        with pytest.raises(_BackupError, match=_INC_DIR_NAME):
            restorer.list_incrementals(_SOURCE["s3/awscli"])

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
        stubs.install("rsync", *_PRESENT["ssh"])
        restorer = _remote_restorer(stubs, "gs", _ABSENT["gs"])

        restorer.list_incrementals(_SOURCE["gs"])

        assert [pathlib.Path(argv[0]).name for argv in stubs.calls()] == ["gcloud"]

    def test_raises_backup_error_for_a_missing_binary(self, stubs: StubBin) -> None:
        """Turn a listing binary that does not exist into a ``BackupError``."""
        restorer = _restorer(stubs)

        with pytest.raises(_BackupError, match=_INC_DIR_NAME):
            restorer.list_incrementals(_SOURCE["s3/awscli"])


class TestListIncrementalsSsh:
    """Cover the SSH listing, which goes through ``sudo rsync`` like the copy."""

    def test_lists_the_parent_then_the_chain_through_sudo_rsync(
        self, stubs: StubBin
    ) -> None:
        """Use the copy's privilege path, listing the parent dir before the chain."""
        restorer = _remote_restorer(stubs, "ssh", _PRESENT["ssh"])

        restorer.list_incrementals(_SOURCE["ssh"])

        rsync_args = [
            "--list-only",
            "--rsync-path=sudo rsync",
            "-e",
            f"ssh -t -p {_SSH_PORT} -i /home/{_SSH_USER}/.ssh/id_rsa",
        ]
        remote = f"{_SSH_USER}@{_SSH_HOST}:/backups/"
        assert [argv[1:] for argv in stubs.calls()] == [
            [*rsync_args, remote],
            [*rsync_args, f"{remote}{_INC_DIR_NAME}/"],
        ]

    def test_finds_the_chain_of_a_home_relative_source(self, stubs: StubBin) -> None:
        """Match the parent listing against the remote name, not ``host:name``."""
        restorer = _remote_restorer(stubs, "ssh", _PRESENT["ssh"])

        chain = restorer.list_incrementals(f"{_SSH_HOST}:{_FULL}")

        remote = f"{_SSH_USER}@{_SSH_HOST}:"
        assert chain == list(_INCS)
        assert [argv[-1] for argv in stubs.calls()] == [
            remote,
            f"{remote}{_INC_DIR_NAME}/",
        ]

    def test_does_not_list_a_chain_the_parent_lacks(self, stubs: StubBin) -> None:
        """Stop after the parent listing when it has no ``_incrementals`` entry."""
        restorer = _remote_restorer(stubs, "ssh", _ABSENT["ssh"])

        restorer.list_incrementals(_SOURCE["ssh"])

        assert len(stubs.calls()) == 1

    def test_raises_when_a_listed_chain_cannot_be_read(self, stubs: StubBin) -> None:
        """Fail, not report an empty chain, when the chain dir exists but is unreadable."""
        denied = Stub(
            stderr=f'rsync: [sender] change_dir "/backups/{_INC_DIR_NAME}" failed: '
            "Permission denied (13)\n",
            returncode=23,
        )
        restorer = _remote_restorer(stubs, "ssh", (_SSH_PARENT_WITH_CHAIN, denied))

        with pytest.raises(_BackupError, match="Permission denied"):
            restorer.list_incrementals(_SOURCE["ssh"])


@pytest.fixture
def unreadable_chain(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    """Build a local chain whose ``_incrementals`` dir cannot be listed.

    :param tmp_path: Where the chain is created.
    :yield: The full backup's path.
    """
    full = _make_local_chain(tmp_path, *_INCS)
    inc_dir = tmp_path / f"{_FULL}_incrementals"
    inc_dir.chmod(0o000)
    try:
        yield full
    finally:
        inc_dir.chmod(0o755)


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
        self, stubs: StubBin, unreadable_chain: pathlib.Path
    ) -> None:
        """Fail loudly on a chain dir that exists but cannot be listed."""
        with pytest.raises(_BackupError, match=f"{_FULL}_incrementals"):
            _restorer(stubs).list_incrementals(str(unreadable_chain))


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
        stubs.install("aws", *_PRESENT["s3/awscli"])
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
        stubs.install("aws", *_PRESENT["s3/awscli"])
        full = _SOURCE["s3/awscli"]

        chain = _chain_restorer(
            stubs, f"{full}_incrementals/{_INCS[0]}/"
        ).backup_chain()

        assert chain == [full, f"{full}_incrementals/{_INCS[0]}"]

    def test_home_relative_ssh_incremental_source_keeps_the_host(
        self, stubs: StubBin
    ) -> None:
        """Truncate a ``host:path`` incremental source and keep the host in each path."""
        stubs.install("rsync", *_PRESENT["ssh"])
        full = f"{_SSH_HOST}:{_FULL}"

        chain = _chain_restorer(stubs, f"{full}_incrementals/{_INCS[1]}").backup_chain()

        assert chain == [full, *(f"{full}_incrementals/{inc}" for inc in _INCS[:2])]

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
        self, stubs: StubBin, unreadable_chain: pathlib.Path
    ) -> None:
        """Restore the full alone and never list, as an unreadable chain proves."""
        full = str(unreadable_chain)

        chain = _chain_restorer(stubs, full, skip_incrementals=True).backup_chain()

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


class _NoRlimit:
    """Stand in for ``resource`` so ``run`` does not change the test process's limits."""

    RLIMIT_NOFILE = 7

    @staticmethod
    def setrlimit(_which: int, _limits: list[int]) -> None:
        """Accept the limit without applying it."""


def _copy_back_reached() -> None:
    """Fail the restore the moment ``run`` gets as far as copying data back."""
    raise _BackupError("copy_back reached")


def _run_restorer(
    stubs: StubBin, tmp_path: pathlib.Path, chain: list[bool | None]
) -> tuple[types.SimpleNamespace, list[str], list[str]]:
    """Build a restorer carrying everything ``run`` needs to reach ``signal_mysqld``.

    :param stubs: The stub bin dir the lifted listing runs against.
    :param tmp_path: Where the chain is created.
    :param chain: Per element, its ``incremental`` flag, or ``None`` for no info file.
    :return: The restorer, the chain's paths and the recorded ``signal_mysqld`` calls.
    """
    restorer, paths = _info_restorer(tmp_path, chain)
    signals: list[str] = []
    restorer.signal_mysqld = signals.append
    restorer.datadir = str(tmp_path / "datadir")
    restorer.from_commandline = False
    restorer.copy_back = _copy_back_reached
    restorer.logging_dir = str(tmp_path)
    restorer.pre_script = None
    restorer.continue_replication = False
    restorer.is_stream = False
    restorer.skip_incrementals = False
    restorer.list_incrementals = _restorer(stubs).list_incrementals
    restorer._backup_chain = functools.partial(_lift("_backup_chain"), restorer)
    restorer._check_backup_chain = restorer.check_backup_chain
    return restorer, paths, signals


@pytest.mark.usefixtures("_pxb_info_lock")
class TestRunChecksTheChainFirst:
    """Cover ``run`` refusing a bad chain before it stops mysqld."""

    def test_does_not_stop_mysqld_for_a_broken_chain(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Raise before ``signal_mysqld`` runs when a later element is unreadable.

        The restorer carries everything ``run`` needs to reach ``signal_mysqld`` for
        the full backup, so only the pre-flight stands between the two.
        ``test_stops_mysqld_once_the_chain_is_valid`` is the positive control.
        """
        restorer, paths, signals = _run_restorer(stubs, tmp_path, [False, True, None])
        run = _lift("run", {"time": time, "resource": _NoRlimit})

        with pytest.raises(_BackupError) as exc_info:
            run(restorer)

        assert signals == []
        assert paths[2] in str(exc_info.value)

    def test_stops_mysqld_once_the_chain_is_valid(
        self, stubs: StubBin, tmp_path: pathlib.Path
    ) -> None:
        """Get past the pre-flight and stop mysqld when every element checks out."""
        restorer, _, signals = _run_restorer(stubs, tmp_path, [False, True, True])
        run = _lift("run", {"time": time, "resource": _NoRlimit})

        with pytest.raises(_BackupError, match="copy_back reached"):
            run(restorer)

        assert signals == ["stop"]
