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

"""AES-256 encrypt -> verify -> restore round trips for Mydumper and Binlog.

The backup and restore payloads are independent Nomad scripts with no shared
import, so this exercises both sides for real with a stand-in ``xbcrypt``
executable — the same technique as ``test_xtrabackup_aes256_restore.py``.
"""

import ast
import os
import pathlib
import re
import stat
import subprocess
import types

import pytest

from tests.app.extensions.apps.mysql_backups.conftest import (
    BINLOG_PAYLOAD_PATH,
    MYDUMPER_PAYLOAD_PATH,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_function as _load_backup_function,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_instance as _payload_instance,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_BINLOG_PAYLOAD_PATH,
    RESTORE_MYDUMPER_PAYLOAD_PATH,
)

_FAKE_XBCRYPT_SCRIPT = """\
#!/bin/sh
input=""
output=""
for arg in "$@"; do
    case "$arg" in
        --input=*) input="${arg#--input=}" ;;
        --output=*) output="${arg#--output=}" ;;
    esac
done
if grep -q CORRUPT_MARKER "$input" 2>/dev/null; then
    echo "corrupt xbcrypt input" >&2
    exit 1
fi
rev "$input" > "$output"
"""


def _write_fake_xbcrypt(tmp_path: pathlib.Path) -> str:
    """Write a reversing stand-in ``xbcrypt`` executable and return its path."""
    script = tmp_path / "fake_xbcrypt.sh"
    script.write_text(_FAKE_XBCRYPT_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


class _FakeRestoreProc:
    """Stand in for a ``Popen`` result with a fixed return code."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def communicate(self):
        """Return ``(stdout, stderr)`` — unused by ``decrypt_aes``."""
        return b"", b""


def _restore_method_nodes(
    payload_path: pathlib.Path, method_names: tuple[str, ...]
) -> list[ast.FunctionDef]:
    """Return the named restore-payload ``FunctionDef`` nodes, raising if missing."""
    tree = ast.parse(payload_path.read_text())
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in method_names
    ]
    missing = set(method_names) - {node.name for node in nodes}
    if missing:
        raise RuntimeError(f"{sorted(missing)} not found in {payload_path}.")
    return nodes


def _restore_instance(
    payload_path: pathlib.Path,
    method_names: tuple[str, ...],
    *,
    real_subprocess: bool,
    returncode: int = 0,
    extra_namespace: dict[str, object] | None = None,
):
    """Build a restore-payload instance carrying the named methods."""
    calls: list[list[str]] = []

    class _FakeSubprocess:
        PIPE = -1

        @staticmethod
        def Popen(cmd: list[str], **_kwargs: object) -> _FakeRestoreProc:  # noqa: N802
            calls.append(cmd)
            return _FakeRestoreProc(returncode)

    namespace = {
        "os": os,
        "Path": pathlib.Path,
        "thread_pool": __import__("multiprocessing.pool", fromlist=["pool"]),
        "subprocess": subprocess if real_subprocess else _FakeSubprocess,
    }
    exec("class BackupError(Exception):\n    pass", namespace)
    namespace["XBCRYPT_BIN"] = "/usr/bin/xbcrypt"
    namespace.update(extra_namespace or {})

    cls = ast.ClassDef(
        name="_RestorePayload",
        bases=[],
        keywords=[],
        body=_restore_method_nodes(payload_path, method_names),
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(payload_path), "exec"), namespace)

    inst = namespace["_RestorePayload"]()
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    inst.xtrabackup_aes256_keyfile = "/keys/aes.key"
    return inst, namespace["BackupError"], calls


_ENGINES = (
    pytest.param(
        MYDUMPER_PAYLOAD_PATH,
        RESTORE_MYDUMPER_PAYLOAD_PATH,
        "table.sql",
        id="mydumper",
    ),
    pytest.param(
        BINLOG_PAYLOAD_PATH,
        RESTORE_BINLOG_PAYLOAD_PATH,
        "binlog.000001.gz",
        id="binlog",
    ),
)


class TestAes256RoundTrip:
    """Assert an AES-256 backup survives a real encrypt -> verify -> restore pass."""

    @pytest.mark.parametrize(("backup_path", "restore_path", "filename"), _ENGINES)
    def test_encrypt_verify_restore_round_trip(
        self,
        tmp_path: pathlib.Path,
        backup_path: pathlib.Path,
        restore_path: pathlib.Path,
        filename: str,
    ) -> None:
        """Assert file content — not just command shape — survives the round trip."""
        fake_bin = _write_fake_xbcrypt(tmp_path)
        keyfile = tmp_path / "aes.key"
        keyfile.write_text("key")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        original = "top-secret-backup-content\n"
        (backup_dir / filename).write_text(original)

        backup_inst, _, _ = _payload_instance(
            ("encrypt_files_aes256", "_run_encrypt_file_aes256", "_run_xbcrypt"),
            extra_namespace={"XBCRYPT_BIN": fake_bin},
            real_subprocess=True,
            payload_path=backup_path,
        )
        backup_inst.encrypt_files_aes256(str(backup_dir))

        is_encrypted_dir = _load_backup_function(
            "is_encrypted_dir", payload_path=backup_path
        )
        if backup_path == MYDUMPER_PAYLOAD_PATH:
            assert (
                is_encrypted_dir(backup_dir, backup_inst.logger, method="aes256")
                is True
            )
        else:
            assert is_encrypted_dir(str(backup_dir), method="aes256") is True

        restore_inst, _, _ = _restore_instance(
            restore_path,
            ("is_encrypted", "decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=True,
            extra_namespace={"XBCRYPT_BIN": fake_bin},
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        assert restore_inst.is_encrypted(str(backup_dir)) == "aes"

        restore_inst.decrypt_aes(str(backup_dir))

        restored = backup_dir / filename
        assert restored.exists()
        assert not (backup_dir / f"{filename}.xbcrypt").exists()
        assert restored.read_text() == original

    @pytest.mark.parametrize(
        "restore_path",
        [RESTORE_MYDUMPER_PAYLOAD_PATH, RESTORE_BINLOG_PAYLOAD_PATH],
    )
    def test_corrupt_xbcrypt_input_raises_backuperror(
        self, tmp_path: pathlib.Path, restore_path: pathlib.Path
    ) -> None:
        """Assert a corrupted ``.xbcrypt`` file fails restore, not silently drops data."""
        fake_bin = _write_fake_xbcrypt(tmp_path)
        keyfile = tmp_path / "aes.key"
        keyfile.write_text("key")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        (backup_dir / "data.xbcrypt").write_text("CORRUPT_MARKER\n")

        restore_inst, backup_error, _ = _restore_instance(
            restore_path,
            ("decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=True,
            extra_namespace={"XBCRYPT_BIN": fake_bin},
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        with pytest.raises(backup_error):
            restore_inst.decrypt_aes(str(backup_dir))


class TestDecryptAesMissingKeyfile:
    """Assert a missing/unusable AES key fails with a clear named error."""

    @pytest.mark.parametrize(
        "restore_path",
        [RESTORE_MYDUMPER_PAYLOAD_PATH, RESTORE_BINLOG_PAYLOAD_PATH],
    )
    def test_empty_keyfile_raises_not_configured(
        self, restore_path: pathlib.Path
    ) -> None:
        """Assert an empty key path fails before invoking xbcrypt."""
        restore_inst, backup_error, calls = _restore_instance(
            restore_path,
            ("decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=False,
        )
        restore_inst.xtrabackup_aes256_keyfile = ""
        with pytest.raises(backup_error, match="not configured"):
            restore_inst.decrypt_aes("/backups/host1")
        assert calls == []

    @pytest.mark.parametrize(
        "restore_path",
        [RESTORE_MYDUMPER_PAYLOAD_PATH, RESTORE_BINLOG_PAYLOAD_PATH],
    )
    def test_absent_keyfile_raises_not_found(
        self, tmp_path: pathlib.Path, restore_path: pathlib.Path
    ) -> None:
        """Assert a missing key file path fails with a clear named error."""
        restore_inst, backup_error, calls = _restore_instance(
            restore_path,
            ("decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=False,
        )
        restore_inst.xtrabackup_aes256_keyfile = str(tmp_path / "missing.key")
        with pytest.raises(backup_error, match="not found"):
            restore_inst.decrypt_aes("/backups/host1")
        assert calls == []

    @pytest.mark.parametrize(
        "restore_path",
        [RESTORE_MYDUMPER_PAYLOAD_PATH, RESTORE_BINLOG_PAYLOAD_PATH],
    )
    def test_no_xbcrypt_files_raises(
        self, tmp_path: pathlib.Path, restore_path: pathlib.Path
    ) -> None:
        """Assert an empty directory fails instead of succeeding with nothing done."""
        keyfile = tmp_path / "aes.key"
        keyfile.write_text("k")
        empty = tmp_path / "empty"
        empty.mkdir()
        restore_inst, backup_error, calls = _restore_instance(
            restore_path,
            ("decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=False,
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        with pytest.raises(backup_error, match=re.escape("No .xbcrypt files")):
            restore_inst.decrypt_aes(str(empty))
        assert calls == []


class TestDecryptAesParallelism:
    """Assert restore decrypts via argv lists, never a shell pipeline."""

    @pytest.mark.parametrize(
        "restore_path",
        [RESTORE_MYDUMPER_PAYLOAD_PATH, RESTORE_BINLOG_PAYLOAD_PATH],
    )
    def test_decrypts_each_file_without_shell(
        self, tmp_path: pathlib.Path, restore_path: pathlib.Path
    ) -> None:
        """Assert each ``.xbcrypt`` becomes an argv list with ``-d``, not ``shell=True``."""
        keyfile = tmp_path / "aes.key"
        keyfile.write_text("k")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        targets = [backup_dir / "a.sql.xbcrypt", backup_dir / "b.sql.xbcrypt"]
        for path in targets:
            path.write_text("enc")
        inst, _, calls = _restore_instance(
            restore_path,
            ("decrypt_aes", "_run_decrypt_file_aes256"),
            real_subprocess=False,
        )
        inst.xtrabackup_aes256_keyfile = str(keyfile)
        inst.decrypt_aes(str(backup_dir))
        assert len(calls) == len(targets)
        for cmd in calls:
            assert isinstance(cmd, list)
            assert cmd[1] == "-d"
            assert "--encrypt-algo=AES256" in cmd
        inputs = {a for cmd in calls for a in cmd if a.startswith("--input=")}
        assert inputs == {f"--input={path}" for path in targets}


class TestRunAesBranch:
    """Assert ``run`` calls ``decrypt_aes`` for ``aes`` instead of raising."""

    @pytest.mark.parametrize(
        ("restore_path", "class_name"),
        [
            (RESTORE_MYDUMPER_PAYLOAD_PATH, "Myloader"),
            (RESTORE_BINLOG_PAYLOAD_PATH, "Binlog"),
        ],
    )
    def test_aes_calls_decrypt_aes(
        self, restore_path: pathlib.Path, class_name: str
    ) -> None:
        """Assert the ``aes`` branch decrypts rather than raising 'not supported'."""
        tree = ast.parse(restore_path.read_text())
        class_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        run_nodes = [
            node
            for node in class_nodes[0].body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        ]
        namespace = {
            "os": os,
            "time": __import__("time"),
        }
        exec("class BackupError(Exception):\n    pass", namespace)
        module = ast.fix_missing_locations(ast.Module(body=run_nodes, type_ignores=[]))
        exec(compile(module, str(restore_path), "exec"), namespace)

        class _StopError(Exception):
            """Abort ``run`` after the decrypt branch under test."""

        decrypted: list[str] = []
        inst = types.SimpleNamespace(
            logging_dir="/tmp",
            local_path="/tmp/restore",
            pre_script=None,
            post_script=None,
            copy_back=lambda: None,
            is_encrypted=lambda _p: "aes",
            decrypt_aes=decrypted.append,
            decrypt_gpg=lambda _p: (_ for _ in ()).throw(
                AssertionError("gpg path must not run for aes")
            ),
            logger=types.SimpleNamespace(info=lambda *_a, **_k: None),
            dest_host="localhost",
            dest_port=3306,
        )
        if class_name == "Myloader":
            inst._run_myloader = lambda: (_ for _ in ()).throw(_StopError())
            inst._run_script = lambda *_a, **_k: None
        else:
            inst.is_binlog_compressed = lambda _p: (_ for _ in ()).throw(_StopError())

        with pytest.raises(_StopError):
            namespace["run"](inst)

        assert decrypted == ["/tmp/restore"]
