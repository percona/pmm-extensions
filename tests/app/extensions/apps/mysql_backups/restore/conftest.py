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

"""Define test fixtures for mysql_backups restore payload tests."""

import ast
import pathlib
import shutil
import types
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, Protocol

import pytest

from app.extensions.apps.mysql_backups.restore.models import RestoreConfigAll
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_function,
    payload_method,
    Recorder,
)

_RESTORE_DIR = (
    pathlib.Path(__file__).parents[6] / "app/extensions/apps/mysql_backups/restore"
)
RESTORE_PAYLOAD_PATH = _RESTORE_DIR / "xtrabackup_payload"
RESTORE_MYDUMPER_PAYLOAD_PATH = _RESTORE_DIR / "mydumper_payload"
RESTORE_BINLOG_PAYLOAD_PATH = _RESTORE_DIR / "binlog_payload"

_CNF_METHODS = ("_restore_cnf_files", "_cnf_destination", "_edit_copy_my_cnf")
NEW_SERVER_ID = 4242


def legacy_default(field_name: str) -> Any:
    """Return a gated field's pre-declaration default, read from the config model.

    The config models still declare ``percona`` / ``22`` / ``s3cmd``, so reading
    them here keeps the tests from carrying a second copy of the table the
    normalizer itself derives.
    """
    default = RestoreConfigAll.model_fields[field_name].default
    return getattr(default, "value", default)


def restore_payload_tree() -> ast.Module:
    """Parse and return the restore payload's AST, fresh on every call.

    Centralizes the payload-path lookup so the per-file AST-extraction helpers
    in this directory's test modules do not each re-derive it independently.
    """
    return ast.parse(RESTORE_PAYLOAD_PATH.read_text())


def restore_mydumper_payload_tree() -> ast.Module:
    """Parse and return the mydumper restore payload's AST, fresh on every call."""
    return ast.parse(RESTORE_MYDUMPER_PAYLOAD_PATH.read_text())


def restore_binlog_payload_tree() -> ast.Module:
    """Parse and return the binlog restore payload's AST, fresh on every call."""
    return ast.parse(RESTORE_BINLOG_PAYLOAD_PATH.read_text())


class _Restorer(Protocol):
    """Type the synthetic object carrying the lifted restore methods."""

    BackupError: type[Exception]
    logger: Recorder

    def _restore_cnf_files(self) -> None: ...

    @staticmethod
    def _edit_copy_my_cnf(src: str, dst: str, new_server_id: int) -> None: ...


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
    unreachable_master: bool = False

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
            "unquote": urllib.parse.unquote,
        }
        methods = {
            name: payload_method(
                "Xtrabackup",
                name,
                extra_namespace=namespace,
                payload_path=RESTORE_PAYLOAD_PATH,
            )
            for name in _CNF_METHODS
        }

        def new_server_id(_self: object) -> int:
            self.server_id_calls.append(None)
            if self.during_server_id:
                self.during_server_id()
            return NEW_SERVER_ID

        server_id_attrs: dict[str, object] = {"_get_new_server_id": new_server_id}
        if self.unreachable_master:

            def refuse(*_args: object, **_kwargs: object) -> None:
                raise backup_error("master unreachable")

            server_id_attrs = {
                "_get_new_server_id": payload_method(
                    "Xtrabackup",
                    "_get_new_server_id",
                    extra_namespace={
                        **namespace,
                        "ProgrammingError": type("ProgrammingError", (Exception,), {}),
                        "random": types.SimpleNamespace(
                            randint=lambda _a, _b: NEW_SERVER_ID
                        ),
                    },
                    payload_path=RESTORE_PAYLOAD_PATH,
                ),
                "_query_mysql": refuse,
                "master_ip": "10.0.0.5",
                "master_port": 3306,
            }

        restorer = type(
            "Restorer",
            (),
            {
                **methods,
                **server_id_attrs,
                "BackupError": backup_error,
                "datadir": str(self.datadir),
                "logger": Recorder(),
            },
        )()
        return cast("_Restorer", restorer)


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Layout]:
    """Build a scratch config tree and the datadir a restore reads from.

    The working directory moves to an empty scratch dir so a relative decode that
    slips through lands somewhere the tests can see, never in the repository.
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
