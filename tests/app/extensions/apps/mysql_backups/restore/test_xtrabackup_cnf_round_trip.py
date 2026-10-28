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

"""Round-trip config files through the real XtraBackup backup and restore code.

The backup payload's ``backup_mycnf`` names each copy so the restore payload's
``_restore_cnf_files`` can decode where it came from. These tests run both real
halves against one scratch tree, with the backup directory standing in for the
datadir copy-back fills.
"""

import shutil
import urllib.parse
from pathlib import Path
from typing import cast, Protocol

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_method,
    Recorder,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    Layout,
    NEW_SERVER_ID,
)

_BACKUP_METHODS = ("backup_mycnf", "get_included_config", "get_default_mycnf")
_NAME_MAX = 255


class _Backup(Protocol):
    """Type the synthetic object carrying the lifted backup methods."""

    mycnf_copy_skipped: bool
    logger: Recorder

    def backup_mycnf(self, backup_dir: str) -> None: ...

    def get_default_mycnf(self) -> str: ...


def _backup(
    mycnf_path: str, query_result: dict[str, str | None] | None = None
) -> _Backup:
    """Return an object carrying the backup payload's real my.cnf copy methods."""
    methods = {
        name: payload_method(
            "Backup",
            name,
            extra_namespace={"shutil": shutil, "quote": urllib.parse.quote},
        )
        for name in _BACKUP_METHODS
    }
    backup = type(
        "Backup",
        (),
        {
            **methods,
            "mycnf_path": mycnf_path,
            "all_cnf_files": [],
            "mycnf_copy_skipped": False,
            "logger": Recorder(),
            "_query_mysql": lambda _self, _query: query_result,
        },
    )()
    return cast("_Backup", backup)


def _archived(path: Path) -> str:
    """Return the name a copy of ``path`` is expected to carry in the backup."""
    return urllib.parse.quote(str(path), safe="")


def _server_cnf(layout: Layout) -> tuple[Path, Path, Path, Path]:
    """Write a server's config files under the scratch tree and return them.

    ``/etc/my.cnf`` includes ``conf.d``, whose file holds a ``_``, and
    ``/etc/mysql/my.cnf`` shares the first file's basename.
    """
    conf_d = layout.allowed_dir / "conf.d"
    etc_my = layout.allowed_file
    etc_my.write_text(f"[mysqld]\nserver-id=1\n!includedir {conf_d}\n")
    custom = conf_d / "my_custom.cnf"
    custom.write_text("[mysqld]\nserver-id=2\nport = 3307\n")
    mysql_my = layout.allowed_dir / "my.cnf"
    mysql_my.write_text("[mysqld]\nserver-id=3\nlog_bin = on\n")
    server_datadir = layout.root / "var" / "lib" / "mysql"
    server_datadir.mkdir(parents=True)
    auto = server_datadir / "mysqld-auto.cnf"
    auto.write_text('{"Version": 1}\n')
    return etc_my, custom, mysql_my, auto


class TestRoundTrip:
    """Cover a backup's config copies restored by the real decode."""

    def test_each_file_returns_to_its_source_with_a_new_server_id(
        self, layout: Layout
    ) -> None:
        """Restore every captured file to the path it came from, server-id renewed."""
        etc_my, custom, mysql_my, auto = _server_cnf(layout)
        sources = {path: path.read_text() for path in (etc_my, custom, mysql_my)}
        backup = _backup(f"{etc_my},{mysql_my},{auto}")

        backup.backup_mycnf(str(layout.datadir))

        assert sorted(p.name for p in layout.datadir.iterdir()) == sorted(
            [*map(_archived, sources), "mysqld-auto.cnf"]
        )
        for path in sources:
            path.write_text("stale\n")
        auto_copy = (layout.datadir / "mysqld-auto.cnf").read_bytes()

        layout.restorer()._restore_cnf_files()

        renewed = f"server-id= {NEW_SERVER_ID}"
        assert {path: path.read_text() for path in sources} == {
            etc_my: f"[mysqld]\n{renewed}\n!includedir {custom.parent}\n",
            custom: f"[mysqld]\n{renewed}\nport = 3307\n",
            mysql_my: f"[mysqld]\n{renewed}\nlog_bin = on\n",
        }
        assert (layout.datadir / "mysqld-auto.cnf").read_bytes() == auto_copy
        assert auto.read_text() == '{"Version": 1}\n'

    def test_restore_writes_nothing_but_the_captured_files(
        self, layout: Layout
    ) -> None:
        """Leave every other path as it was, ``mysqld-auto.cnf`` included."""
        etc_my, custom, mysql_my, auto = _server_cnf(layout)
        _backup(f"{etc_my},{mysql_my},{auto}").backup_mycnf(str(layout.datadir))
        before = layout.snapshot()

        layout.restorer()._restore_cnf_files()

        after = layout.snapshot()
        changed = {path for path in after if after[path] != before.get(path)}
        assert changed == {str(etc_my), str(custom), str(mysql_my)}

    def test_included_file_outside_the_allowlist_blocks_the_restore(
        self, layout: Layout
    ) -> None:
        """Refuse before any write when a captured include lands outside the allowlist."""
        stray = layout.outside / "x.cnf"
        stray.write_text("[mysqld]\nserver-id=9\n")
        etc_my = layout.allowed_file
        etc_my.write_text(f"[mysqld]\nserver-id=1\n!include {stray}\n")
        _backup(str(etc_my)).backup_mycnf(str(layout.datadir))
        before = layout.snapshot()
        restorer = layout.restorer()

        with pytest.raises(restorer.BackupError, match="outside the allowed config"):
            restorer._restore_cnf_files()

        assert layout.snapshot() == before

    def test_too_long_encoded_name_is_skipped_with_a_warning_flag(
        self, layout: Layout
    ) -> None:
        """Flag the copy as skipped and keep the others when a name exceeds 255 bytes."""
        etc_my = layout.allowed_file
        conf_d = layout.allowed_dir / "conf.d"
        etc_my.write_text(f"[mysqld]\n!includedir {conf_d}\n")
        long_cnf = conf_d / f"{'x' * 240}.cnf"
        long_cnf.write_text("[mysqld]\n")
        assert len(_archived(long_cnf)) > _NAME_MAX
        backup = _backup(str(etc_my))

        backup.backup_mycnf(str(layout.datadir))

        assert backup.mycnf_copy_skipped is True
        assert [p.name for p in layout.datadir.iterdir()] == [_archived(etc_my)]


class TestDefaultMycnf:
    """Cover the config files a backup captures when none were configured."""

    def test_adds_existing_basedir_and_datadir_files(self, layout: Layout) -> None:
        """Capture the basedir my.cnf and datadir mysqld-auto.cnf that exist."""
        basedir = layout.root / "usr"
        basedir.mkdir()
        (basedir / "my.cnf").write_text("[mysqld]\n")
        datadir = layout.root / "var"
        datadir.mkdir()
        backup = _backup("", {"@@basedir": str(basedir), "@@datadir": str(datadir)})

        found = backup.get_default_mycnf().split(",")

        assert f"{basedir}/my.cnf" in found
        assert f"{datadir}/mysqld-auto.cnf" not in found

    @pytest.mark.parametrize(
        "has_basedir", [True, False], ids=["with-basedir", "without-basedir"]
    )
    def test_datadir_file_is_captured_only_with_a_basedir(
        self, layout: Layout, *, has_basedir: bool
    ) -> None:
        """Capture the datadir ``mysqld-auto.cnf`` only when the server reports a basedir."""
        datadir = layout.root / "var"
        datadir.mkdir()
        (datadir / "mysqld-auto.cnf").write_text("{}\n")
        basedir = str(layout.root / "usr") if has_basedir else None
        backup = _backup("", {"@@basedir": basedir, "@@datadir": str(datadir)})

        found = backup.get_default_mycnf().split(",")

        assert (f"{datadir}/mysqld-auto.cnf" in found) is has_basedir
