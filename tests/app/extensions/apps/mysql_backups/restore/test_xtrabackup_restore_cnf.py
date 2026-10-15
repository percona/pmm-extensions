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

"""Tests for where the XtraBackup restore's "Restore my.cnf" step may write.

The config files come out of the backup archive, which is untrusted input, and
each one's destination is decoded from its own filename (``_`` standing in for
``/``). These tests drive the restore payload's real ``_restore_cnf_files``,
``_cnf_destination`` and ``_edit_copy_my_cnf`` against a scratch filesystem, with
the allowlist pointed at that scratch tree.
"""

import ast
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, Protocol

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_constant,
    load_function,
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
    restore_payload_tree,
)

_METHODS = ("_restore_cnf_files", "_cnf_destination", "_edit_copy_my_cnf")
_NEW_SERVER_ID = 4242
_NEW_FILE_MODE = 0o644
_HOST_FILE_MODE = 0o640


class _Restorer(Protocol):
    """Type the synthetic object carrying the lifted restore methods."""

    BackupError: type[Exception]
    logger: Recorder

    def _restore_cnf_files(self) -> None: ...

    @staticmethod
    def _edit_copy_my_cnf(src: str, dst: str, new_server_id: int) -> None: ...


def _payload_constant(name: str) -> tuple[str, ...]:
    """Return one of the restore payload's tuple-of-paths constants."""
    return cast(
        "tuple[str, ...]", load_constant(name, payload_path=RESTORE_PAYLOAD_PATH)
    )


def _encode(path: Path | str) -> str:
    """Return the archive filename that decodes to ``path``.

    The decode turns every ``_`` into ``/``, so a path holding ``_`` cannot be
    encoded; refusing it here keeps a fixture typo from passing silently.
    """
    path = str(path)
    assert "_" not in path, f"{path} is not encodable"
    return path.replace("/", "_")


@dataclass
class Layout:
    """Describe a scratch config tree and the datadir a restore reads from."""

    root: Path
    datadir: Path
    cwd: Path
    allowed_dir: Path
    allowed_file: Path
    absent_dir: Path
    outside: Path
    server_id_calls: list[None] = field(default_factory=list)
    during_server_id: Callable[[], None] | None = None

    def plant(self, name: str, content: str = "[mysqld]\nserver-id=1\n") -> Path:
        """Write ``name`` into the datadir as a restored archive entry."""
        src = self.datadir / name
        src.write_text(content)
        return src

    def snapshot(self) -> dict[str, bytes | None]:
        """Return every path under the scratch root and cwd with its file bytes."""
        paths = sorted([*self.root.rglob("*"), *self.cwd.rglob("*")])
        return {
            str(path): None if path.is_dir() or path.is_symlink() else path.read_bytes()
            for path in paths
        }

    def restorer(self) -> _Restorer:
        """Return an object carrying the payload's real cnf-restore methods."""
        # One shared error class, so a refusal from any lifted method matches it.
        backup_error = type("BackupError", (Exception,), {})
        namespace: dict[str, object] = {
            "BackupError": backup_error,
            "MYCNF_DIRS": (str(self.allowed_dir), str(self.absent_dir)),
            "MYCNF_FILES": (str(self.allowed_file),),
            "is_lone_file": load_function(
                "is_lone_file", payload_path=RESTORE_PAYLOAD_PATH
            ),
        }
        methods = {
            name: payload_method(
                "Xtrabackup",
                name,
                extra_namespace=namespace,
                payload_path=RESTORE_PAYLOAD_PATH,
            )
            for name in _METHODS
        }

        def new_server_id(_self: object) -> int:
            self.server_id_calls.append(None)
            if self.during_server_id:
                self.during_server_id()
            return _NEW_SERVER_ID

        restorer = type(
            "Restorer",
            (),
            {
                **methods,
                "_get_new_server_id": new_server_id,
                "BackupError": backup_error,
                "datadir": str(self.datadir),
                "logger": Recorder(),
            },
        )()
        return cast("_Restorer", restorer)


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Layout]:
    """Build a scratch config tree whose absolute path holds no ``_``.

    ``tmp_path`` cannot host the config tree: its absolute path may hold ``_``,
    which the filename encoding cannot carry. The working directory moves to an
    empty scratch dir so a relative decode that slips through lands somewhere
    the tests can see, never in the repository.
    """
    root = Path("/tmp") / f"cnfroot{uuid.uuid4().hex}"
    allowed_dir = root / "etc" / "mysql"
    (allowed_dir / "conf.d").mkdir(parents=True)
    (root / "etc" / "mysql-evil").mkdir()
    outside = root / "outside"
    outside.mkdir()
    datadir = tmp_path / "datadir"
    datadir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    try:
        yield Layout(
            root=root,
            datadir=datadir,
            cwd=cwd,
            allowed_dir=allowed_dir,
            allowed_file=root / "etc" / "my.cnf",
            absent_dir=root / "etc" / "percona",
            outside=outside,
        )
    finally:
        shutil.rmtree(root)


class TestAllowedDestination:
    """Cover entries whose decoded destination is on the allowlist."""

    @pytest.mark.parametrize(
        "destination",
        [
            pytest.param(lambda lay: lay.allowed_dir / "my.cnf", id="allowed-dir"),
            pytest.param(
                lambda lay: lay.allowed_dir / "conf.d" / "custom.cnf",
                id="nested-allowed-subdir",
            ),
            pytest.param(lambda lay: lay.allowed_file, id="exact-allowed-file"),
        ],
    )
    def test_restores_with_a_new_server_id(
        self, layout: Layout, destination: Callable[[Layout], Path]
    ) -> None:
        """Restore an entry decoding to an allowed path, with a new server-id."""
        dst = destination(layout)
        layout.plant(_encode(dst))

        layout.restorer()._restore_cnf_files()

        assert dst.read_text() == f"[mysqld]\nserver-id= {_NEW_SERVER_ID}\n"

    def test_rewrites_server_id_and_keeps_other_lines(self, layout: Layout) -> None:
        """Replace only an active server-id line and copy the rest verbatim."""
        dst = layout.allowed_dir / "my.cnf"
        layout.plant(
            _encode(dst),
            "# server-id=9 stays commented\n[mysqld]\nserver-id=1\nport = 3307\n",
        )

        layout.restorer()._restore_cnf_files()

        assert dst.read_text() == (
            "# server-id=9 stays commented\n"
            f"[mysqld]\nserver-id= {_NEW_SERVER_ID}\nport = 3307\n"
        )

    def test_truncates_previous_content(self, layout: Layout) -> None:
        """Replace a longer existing file instead of leaving its tail behind."""
        dst = layout.allowed_dir / "my.cnf"
        dst.write_text("[mysqld]\nold=1\n" * 50)
        layout.plant(_encode(dst), "[mysqld]\n")

        layout.restorer()._restore_cnf_files()

        assert dst.read_text() == "[mysqld]\n"

    def test_symlink_within_allowed_dir_writes_its_target(self, layout: Layout) -> None:
        """Write through a symlink that stays inside the allowlist, keeping the link."""
        target = layout.allowed_dir / "real.cnf"
        target.write_text("old\n")
        link = layout.allowed_dir / "alias.cnf"
        link.symlink_to(target)
        layout.plant(_encode(link), "[mysqld]\n")

        layout.restorer()._restore_cnf_files()

        assert link.is_symlink()
        assert target.read_text() == "[mysqld]\n"

    def test_symlink_chain_through_an_outside_dir_writes_its_final_target(
        self, layout: Layout
    ) -> None:
        """Write to where a link chain ends, like Debian's alternatives layout."""
        target = layout.allowed_dir / "mysql.cnf"
        target.write_text("old\n")
        alternative = layout.outside / "my.cnf"
        alternative.symlink_to(target)
        link = layout.allowed_dir / "my.cnf"
        link.symlink_to(alternative)
        layout.plant(_encode(link), "[mysqld]\n")

        layout.restorer()._restore_cnf_files()

        assert link.readlink() == alternative
        assert alternative.readlink() == target
        assert target.read_text() == "[mysqld]\n"

    def test_logs_each_restored_destination(self, layout: Layout) -> None:
        """Log the resolved path every restored file is written to."""
        dst = layout.allowed_dir / "my.cnf"
        layout.plant(_encode(dst))
        restorer = layout.restorer()

        restorer._restore_cnf_files()

        assert any(str(dst.resolve()) in msg for msg in restorer.logger.infos)


_Planter = Callable[[Layout], str]


def _plant_outside(layout: Layout) -> str:
    return layout.plant(_encode(layout.outside / "probe.cnf")).name


def _plant_traversal(layout: Layout) -> str:
    return layout.plant(_encode(layout.allowed_dir) + "_.._.._outside_probe.cnf").name


def _plant_prefix_lookalike(layout: Layout) -> str:
    return layout.plant(_encode(layout.root / "etc" / "mysql-evil" / "probe.cnf")).name


def _plant_sibling_of_allowed_dir(layout: Layout) -> str:
    return layout.plant(_encode(layout.root / "etc" / "mysql.cnf")).name


def _plant_dir_symlink_escape(layout: Layout) -> str:
    (layout.allowed_dir / "escape").symlink_to(layout.outside)
    return layout.plant(_encode(layout.allowed_dir / "escape" / "probe.cnf")).name


def _plant_file_symlink_escape(layout: Layout) -> str:
    target = layout.outside / "target.cnf"
    target.write_text("keep\n")
    (layout.allowed_dir / "evil.cnf").symlink_to(target)
    return layout.plant(_encode(layout.allowed_dir / "evil.cnf")).name


def _plant_relative(layout: Layout) -> str:
    return layout.plant("conf_evil.cnf").name


def _plant_missing_parent(layout: Layout) -> str:
    return layout.plant(_encode(layout.allowed_dir / "newdir" / "probe.cnf")).name


def _plant_absent_allowed_dir(layout: Layout) -> str:
    return layout.plant(_encode(layout.absent_dir / "probe.cnf")).name


def _plant_directory_destination(layout: Layout) -> str:
    (layout.allowed_dir / "sub.cnf").mkdir()
    return layout.plant(_encode(layout.allowed_dir / "sub.cnf")).name


def _plant_symlinked_source(layout: Layout) -> str:
    secret = layout.outside / "secret"
    secret.write_text("password=hunter2\n")
    name = _encode(layout.allowed_dir / "leak.cnf")
    (layout.datadir / name).symlink_to(secret)
    return name


def _plant_hardlinked_source(layout: Layout) -> str:
    secret = layout.datadir.parent / "secret"
    secret.write_text("password=hunter2\n")
    name = _encode(layout.allowed_dir / "leak.cnf")
    (layout.datadir / name).hardlink_to(secret)
    return name


def _plant_directory_source(layout: Layout) -> str:
    name = _encode(layout.allowed_dir / "dir.cnf")
    (layout.datadir / name).mkdir()
    return name


_OUTSIDE = "outside the allowed"
_MISSING = "directory missing"
_NOT_FILE = "not a regular file"


class TestRefusedDestination:
    """Cover entries that must be refused before anything is written."""

    @pytest.mark.parametrize(
        ("plant", "reason"),
        [
            pytest.param(_plant_outside, _OUTSIDE, id="absolute-outside"),
            pytest.param(_plant_traversal, _OUTSIDE, id="dotdot-traversal"),
            pytest.param(_plant_prefix_lookalike, _OUTSIDE, id="prefix-lookalike"),
            pytest.param(_plant_sibling_of_allowed_dir, _OUTSIDE, id="allowed-sibling"),
            pytest.param(_plant_dir_symlink_escape, _OUTSIDE, id="dir-symlink-escape"),
            pytest.param(
                _plant_file_symlink_escape, _OUTSIDE, id="file-symlink-escape"
            ),
            pytest.param(_plant_relative, "no destination", id="relative-decode"),
            pytest.param(_plant_missing_parent, _MISSING, id="missing-parent"),
            pytest.param(_plant_absent_allowed_dir, _MISSING, id="absent-allowed-dir"),
            pytest.param(_plant_directory_destination, _NOT_FILE, id="dst-is-dir"),
            pytest.param(_plant_symlinked_source, _NOT_FILE, id="src-is-symlink"),
            pytest.param(_plant_hardlinked_source, _NOT_FILE, id="src-is-hardlink"),
            pytest.param(_plant_directory_source, _NOT_FILE, id="src-is-dir"),
        ],
    )
    def test_refuses_without_touching_the_filesystem(
        self, layout: Layout, plant: _Planter, reason: str
    ) -> None:
        """Raise naming the entry and reason, leaving every file and dir as it was."""
        name = plant(layout)
        before = layout.snapshot()
        restorer = layout.restorer()

        with pytest.raises(restorer.BackupError, match=reason) as exc_info:
            restorer._restore_cnf_files()

        assert name in str(exc_info.value)
        assert layout.snapshot() == before
        assert layout.server_id_calls == []


class TestFailClosed:
    """Cover a batch where one entry is bad and the rest are fine."""

    def test_one_bad_entry_blocks_every_write(self, layout: Layout) -> None:
        """Leave a valid entry's destination untouched when another is refused."""
        good = layout.allowed_dir / "my.cnf"
        good.write_text("original\n")
        layout.plant(_encode(good))
        layout.plant(_encode(layout.outside / "probe.cnf"))
        before = layout.snapshot()
        restorer = layout.restorer()

        with pytest.raises(restorer.BackupError):
            restorer._restore_cnf_files()

        assert layout.snapshot() == before
        assert good.read_text() == "original\n"

    def test_server_id_lookup_failure_writes_nothing(self, layout: Layout) -> None:
        """Leave every destination untouched when the new server-id cannot be had."""
        good = layout.allowed_dir / "my.cnf"
        good.write_text("original\n")
        layout.plant(_encode(good))
        before = layout.snapshot()
        restorer = layout.restorer()

        def fail() -> None:
            raise restorer.BackupError("no master")

        layout.during_server_id = fail

        with pytest.raises(restorer.BackupError, match="no master"):
            restorer._restore_cnf_files()

        assert layout.snapshot() == before

    def test_write_failure_keeps_the_files_already_written(
        self, layout: Layout
    ) -> None:
        """Pin that a failed write does not roll back the entries written before it."""
        first = layout.allowed_dir / "a.cnf"
        second = layout.allowed_dir / "b.cnf"
        layout.plant(_encode(first), "[mysqld]\n")
        late = layout.plant(_encode(second), "[mysqld]\n")
        secret = layout.outside / "secret"
        secret.write_text("password=hunter2\n")

        def swap_late_source() -> None:
            late.unlink()
            late.symlink_to(secret)

        layout.during_server_id = swap_late_source

        with pytest.raises(OSError, match="symbolic link"):
            layout.restorer()._restore_cnf_files()

        assert first.read_text() == "[mysqld]\n"
        assert not second.exists()


class TestSkippedEntries:
    """Cover datadir entries the restore leaves alone."""

    @pytest.mark.parametrize("name", ["backup-my.cnf", "auto.cnf", "mysqld-auto.cnf"])
    def test_datadir_owned_file_stays_in_place(self, layout: Layout, name: str) -> None:
        """Leave a file MySQL keeps in the datadir where copy-back put it."""
        src = layout.plant(name, "[mysqld]\nserver-id=1\n")
        before = layout.snapshot()

        layout.restorer()._restore_cnf_files()

        assert src.read_text() == "[mysqld]\nserver-id=1\n"
        assert layout.snapshot() == before

    def test_non_cnf_entries_are_ignored(self, layout: Layout) -> None:
        """Ignore entries without a ``.cnf`` suffix, whatever they decode to."""
        layout.plant(_encode(layout.outside / "probe.txt"))
        layout.plant("ibdata1")
        before = layout.snapshot()

        layout.restorer()._restore_cnf_files()

        assert layout.snapshot() == before

    def test_skipped_entry_does_not_block_a_valid_one(self, layout: Layout) -> None:
        """Restore a valid entry alongside a datadir-owned one."""
        layout.plant("mysqld-auto.cnf")
        dst = layout.allowed_dir / "my.cnf"
        layout.plant(_encode(dst))

        layout.restorer()._restore_cnf_files()

        assert dst.is_file()
        assert layout.server_id_calls == [None]


class TestEdgeNames:
    """Cover entry names that decode to unusual but harmless paths."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param(
                lambda lay: " " + _encode(lay.allowed_dir / "my.cnf"),
                id="leading-whitespace",
            ),
            pytest.param(
                lambda lay: _encode(lay.allowed_dir) + "__my.cnf",
                id="doubled-separator",
            ),
        ],
    )
    def test_name_is_normalised_before_the_check(
        self, layout: Layout, name: Callable[[Layout], str]
    ) -> None:
        """Restore an entry whose decoded path only normalises into an allowed dir."""
        layout.plant(name(layout))

        layout.restorer()._restore_cnf_files()

        assert (layout.allowed_dir / "my.cnf").is_file()


class TestEditCopyMyCnf:
    """Cover the write itself, after the destination has passed the check."""

    def test_does_not_follow_a_symlink_at_the_destination(self, layout: Layout) -> None:
        """Fail rather than write through a symlink swapped in after the check."""
        src = layout.plant("source.cnf")
        target = layout.outside / "target.cnf"
        target.write_text("keep\n")
        link = layout.allowed_dir / "swapped.cnf"
        link.symlink_to(target)
        edit = layout.restorer()._edit_copy_my_cnf

        with pytest.raises(OSError, match="symbolic link"):
            edit(str(src), str(link), _NEW_SERVER_ID)

        assert target.read_text() == "keep\n"

    def test_does_not_follow_a_symlink_at_the_source(self, layout: Layout) -> None:
        """Fail rather than read through a source swapped for a symlink after the check."""
        secret = layout.outside / "secret"
        secret.write_text("password=hunter2\n")
        dst = layout.allowed_file
        dst.write_text("keep\n")
        src = layout.plant(_encode(dst))

        def swap_source() -> None:
            src.unlink()
            src.symlink_to(secret)

        layout.during_server_id = swap_source
        restorer = layout.restorer()

        with pytest.raises(OSError, match="symbolic link"):
            restorer._restore_cnf_files()

        assert dst.read_text() == "keep\n"

    def test_does_not_read_a_source_hardlinked_after_the_check(
        self, layout: Layout
    ) -> None:
        """Fail rather than copy out a file hard-linked in after the check."""
        secret = layout.datadir.parent / "secret"
        secret.write_text("password=hunter2\n")
        dst = layout.allowed_file
        dst.write_text("keep\n")
        src = layout.plant(_encode(dst))

        def swap_source() -> None:
            src.unlink()
            src.hardlink_to(secret)

        layout.during_server_id = swap_source
        restorer = layout.restorer()

        with pytest.raises(restorer.BackupError, match=_NOT_FILE):
            restorer._restore_cnf_files()

        assert dst.read_text() == "keep\n"

    @pytest.mark.parametrize("umask", [0, 0o007, 0o077], ids=["000", "007", "077"])
    def test_new_file_is_world_readable_whatever_the_umask(
        self, layout: Layout, umask: int
    ) -> None:
        """Create a new config file ``0644`` so mysqld can read it under any umask."""
        src = layout.plant("source.cnf")
        dst = layout.allowed_dir / "fresh.cnf"
        old_umask = os.umask(umask)
        try:
            layout.restorer()._edit_copy_my_cnf(str(src), str(dst), 1)
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(dst.stat().st_mode) == _NEW_FILE_MODE

    def test_existing_file_keeps_its_mode(self, layout: Layout) -> None:
        """Leave the mode of a replaced config file as the host had it."""
        src = layout.plant("source.cnf")
        dst = layout.allowed_dir / "existing.cnf"
        dst.write_text("old\n")
        dst.chmod(_HOST_FILE_MODE)

        layout.restorer()._edit_copy_my_cnf(str(src), str(dst), 1)

        assert stat.S_IMODE(dst.stat().st_mode) == _HOST_FILE_MODE


class TestBackupEncoderCompatibility:
    """Pin how the entries a real backup carries are handled.

    A Percona Server 8.0.35 / XtraBackup 8.0.35 backup carries these names at its
    root: ``backup-my.cnf`` from XtraBackup, and the basenames ``backup_mycnf``
    copies (``my.cnf``, an ``!includedir`` file, ``mysqld-auto.cnf``).
    """

    @pytest.mark.parametrize("name", ["my.cnf", "docker.cnf"])
    def test_basename_entry_is_refused_as_undecodable(
        self, layout: Layout, name: str
    ) -> None:
        """Refuse a basename-only entry, since it encodes no destination."""
        layout.plant(name)
        restorer = layout.restorer()

        with pytest.raises(restorer.BackupError, match="no destination") as exc_info:
            restorer._restore_cnf_files()

        assert name in str(exc_info.value)

    def test_archive_with_only_datadir_files_restores_cleanly(
        self, layout: Layout
    ) -> None:
        """Complete without writing when only datadir-owned entries are present."""
        for name in ("backup-my.cnf", "mysqld-auto.cnf"):
            layout.plant(name)

        layout.restorer()._restore_cnf_files()

        assert layout.server_id_calls == []


class TestProductionAllowlist:
    """Pin the allowlist and skip list the payload ships with."""

    def test_covers_the_standard_mysql_config_locations(self) -> None:
        """Allow the packaged MySQL, Percona Server and PXC config locations."""
        assert set(_payload_constant("MYCNF_DIRS")) == {
            "/etc/mysql",
            "/etc/my.cnf.d",
            "/etc/percona-server.conf.d",
            "/etc/percona-xtradb-cluster.conf.d",
        }
        assert set(_payload_constant("MYCNF_FILES")) == {
            "/etc/my.cnf",
            "/usr/local/etc/my.cnf",
        }

    def test_skips_only_files_mysql_keeps_in_the_datadir(self) -> None:
        """Skip exactly the cnf files MySQL and XtraBackup keep in the datadir."""
        assert set(_payload_constant("DATADIR_CNF")) == {
            "backup-my.cnf",
            "auto.cnf",
            "mysqld-auto.cnf",
        }


class TestToggleOff:
    """Cover the "Restore my.cnf" toggle gating the whole step."""

    def test_cnf_restore_runs_only_under_restore_mycnf(self) -> None:
        """Call ``_restore_cnf_files`` once, directly under ``self.restore_mycnf``."""
        tree = restore_payload_tree()
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_restore_cnf_files"
        ]

        assert len(calls) == 1
        guard = parents[parents[calls[0]]]
        assert isinstance(guard, ast.If)
        assert ast.unparse(guard.test) == "self.restore_mycnf"
