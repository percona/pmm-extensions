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

"""Tests for the AES-256 encrypt -> verify -> restore round trip.

The backup and restore payloads are independent Nomad scripts with no shared
import, so this exercises both sides for real: the backup payload's
``encrypt_files_aes256`` and the restore payload's ``is_encrypted``/
``decrypt_aes`` are lifted via AST (same technique as
``test_xtrabackup_aes256_encrypt.py``) and run with a real subprocess against
a stand-in ``xbcrypt`` executable, rather than a faked ``Popen`` -- proving
actual file *content* survives the boundary between the two payloads, not
just that the right argv was built.
"""

import ast
import multiprocessing.pool as thread_pool
import os
import stat
import subprocess
import types
from pathlib import Path

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import (
    load_function as _load_backup_function,
)
from tests.app.extensions.apps.mysql_backups.payload_harness import (
    payload_instance as _payload_instance,
)
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
    restore_payload_tree,
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


def _write_fake_xbcrypt(tmp_path) -> str:
    """Write a reversing stand-in ``xbcrypt`` executable and return its path.

    Byte-reversal is its own inverse, so encrypting then decrypting through
    this double is a genuine round trip, not a no-op -- proving content
    actually survives the real subprocess boundary between the two payloads.
    A file containing ``CORRUPT_MARKER`` makes the double fail, standing in
    for a corrupted/garbage ``.xbcrypt`` input.
    """
    script = tmp_path / "fake_xbcrypt.sh"
    script.write_text(_FAKE_XBCRYPT_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _restore_method_nodes(method_names: tuple[str, ...]) -> list[ast.FunctionDef]:
    """Return the named restore-payload ``FunctionDef`` nodes, raising if missing."""
    tree = restore_payload_tree()
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in method_names
    ]
    missing = set(method_names) - {node.name for node in nodes}
    if missing:
        raise RuntimeError(f"{sorted(missing)} not found in {RESTORE_PAYLOAD_PATH}.")
    return nodes


class _FakeRestoreProc:
    """Stand in for a ``Popen`` result with a fixed return code."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def communicate(self):
        """Return ``(stdout, stderr)``. stderr is included in ``BackupError``."""
        return b"", b""


_DECRYPT_METHODS = ("decrypt_aes", "_run_decrypt_file_aes256")


class _RecordingThreadPool:
    """Stand in for ``multiprocessing.pool.ThreadPool`` that runs synchronously.

    Records nothing itself; subclasses capture the requested ``processes`` count.
    ``decrypt_aes`` enters the pool with ``with``, so the context manager is required.
    """

    def __init__(self, processes: int) -> None:
        self.processes = processes

    def __enter__(self) -> "_RecordingThreadPool":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def map(self, func, iterable):
        """Apply ``func`` to each item synchronously, mirroring ``ThreadPool.map``."""
        return [func(item) for item in iterable]


def _write_keyfile(path: Path) -> Path:
    """Create an AES key file at ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("key")
    return path


def _restore_instance(
    method_names: tuple[str, ...],
    *,
    real_subprocess: bool,
    returncode: int = 0,
    extra_namespace: dict[str, object] | None = None,
):
    """Build a restore-payload instance carrying the named methods.

    Mirrors ``test_xtrabackup_aes256_encrypt._payload_instance`` for the
    restore payload: methods are lifted verbatim via AST into a synthetic
    class over a controlled namespace. ``real_subprocess=True`` keeps the
    real ``subprocess`` module so a stand-in ``xbcrypt`` executable actually
    runs; ``real_subprocess=False`` fakes ``Popen`` and records each argv list
    and the keyword arguments it was given (``calls``).

    :param method_names: The restore payload method names to lift.
    :param real_subprocess: Whether to keep the real ``subprocess`` module.
    :param returncode: Return code the faked ``Popen`` reports (ignored when
        ``real_subprocess`` is True).
    :param extra_namespace: Extra globals (e.g. ``XBCRYPT_BIN``) merged into
        the exec namespace before the class is compiled.
    :return: A ``(instance, BackupError, calls)`` tuple. ``calls`` is
        ``(argv, kwargs)`` pairs from the faked ``Popen`` (always ``[]`` when
        ``real_subprocess`` is True).
    """
    calls: list[tuple[object, dict[str, object]]] = []

    class _FakeSubprocess:
        PIPE = -1

        @staticmethod
        def Popen(cmd, **kwargs):  # noqa: N802
            calls.append((cmd, kwargs))
            return _FakeRestoreProc(returncode)

    namespace: dict[str, object] = {
        "os": os,
        "Path": Path,
        "thread_pool": thread_pool,
        "subprocess": subprocess if real_subprocess else _FakeSubprocess,
    }
    exec("class BackupError(Exception):\n    pass", namespace)
    namespace["XBCRYPT_BIN"] = "/usr/bin/xbcrypt"
    namespace.update(extra_namespace or {})

    cls = ast.ClassDef(
        name="_RestorePayload",
        bases=[],
        keywords=[],
        body=_restore_method_nodes(method_names),
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(RESTORE_PAYLOAD_PATH), "exec"), namespace)

    inst = namespace["_RestorePayload"]()
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    inst.xtrabackup_aes256_keyfile = "/keys/aes.key"
    inst.xb_parallel = 2
    return inst, namespace["BackupError"], calls


class TestAes256RoundTrip:
    """Assert an AES-256 backup survives a real encrypt -> verify -> restore pass."""

    def test_mariadb_backup_encrypt_verify_restore_round_trip(self, tmp_path) -> None:
        """Assert file content -- not just command shape -- survives the round trip."""
        fake_bin = _write_fake_xbcrypt(tmp_path)
        keyfile = _write_keyfile(tmp_path / "aes.key")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        original = "top-secret-backup-content\n"
        (backup_dir / "ibdata1").write_text(original)

        backup_inst, _, _ = _payload_instance(
            ("encrypt_files_aes256", "_run_encrypt_file_aes256", "_run_xbcrypt"),
            extra_namespace={"XBCRYPT_BIN": fake_bin},
            real_subprocess=True,
        )
        backup_inst.encrypt_files_aes256(str(backup_dir))

        is_encrypted_dir = _load_backup_function("is_encrypted_dir")
        assert is_encrypted_dir(str(backup_dir), method="aes256") is True

        restore_inst, _, _ = _restore_instance(
            ("is_encrypted", *_DECRYPT_METHODS),
            real_subprocess=True,
            extra_namespace={"XBCRYPT_BIN": fake_bin},
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        assert restore_inst.is_encrypted(str(backup_dir)) == "aes"

        restore_inst.decrypt_aes(str(backup_dir))

        restored = backup_dir / "ibdata1"
        assert restored.exists()
        assert not (backup_dir / "ibdata1.xbcrypt").exists()
        assert restored.read_text() == original

    def test_corrupt_xbcrypt_input_raises_backuperror(self, tmp_path) -> None:
        """Assert a corrupted ``.xbcrypt`` file fails restore, not silently drops data."""
        fake_bin = _write_fake_xbcrypt(tmp_path)
        keyfile = _write_keyfile(tmp_path / "aes.key")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        ciphertext = backup_dir / "ibdata1.xbcrypt"
        ciphertext.write_text("CORRUPT_MARKER\n")
        partial = backup_dir / "ibdata1"
        partial.write_text("partial plaintext")

        restore_inst, backup_error, _ = _restore_instance(
            _DECRYPT_METHODS,
            real_subprocess=True,
            extra_namespace={"XBCRYPT_BIN": fake_bin},
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        with pytest.raises(backup_error, match="ibdata1.xbcrypt"):
            restore_inst.decrypt_aes(str(backup_dir))
        assert ciphertext.exists()
        assert not partial.exists()

    def test_no_xbcrypt_files_raises(self, tmp_path: Path) -> None:
        """Assert an empty directory fails instead of succeeding with nothing done."""
        keyfile = _write_keyfile(tmp_path / "aes.key")
        empty = tmp_path / "empty"
        empty.mkdir()
        restore_inst, backup_error, calls = _restore_instance(
            _DECRYPT_METHODS,
            real_subprocess=False,
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        with pytest.raises(backup_error, match="No .xbcrypt files"):
            restore_inst.decrypt_aes(str(empty))
        assert calls == []

    def test_spaces_and_metacharacters_decrypt_without_shell(
        self, tmp_path: Path
    ) -> None:
        """Assert a spaced path and a metacharacter filename decrypt and execute nothing."""
        fake_bin = _write_fake_xbcrypt(tmp_path)
        # A slash cannot appear in one filename, so the touch target is a single
        # path component. A shell would create it in the process working directory.
        marker = "sep2115-pwned"
        touched = Path.cwd() / marker
        keyfile = _write_keyfile(tmp_path / "key dir" / f"k$(touch {marker});'q'.key")
        backup_dir = tmp_path / "daily backup"
        backup_dir.mkdir()
        filename = f"x$(touch {marker});'quote'.xbcrypt"
        original = "secret backup\n"
        ciphertext = backup_dir / filename
        ciphertext.write_text(original[:-1][::-1] + "\n")

        restore_inst, _, _ = _restore_instance(
            _DECRYPT_METHODS,
            real_subprocess=True,
            extra_namespace={"XBCRYPT_BIN": fake_bin},
        )
        restore_inst.xtrabackup_aes256_keyfile = str(keyfile)
        try:
            restore_inst.decrypt_aes(str(backup_dir))

            restored = backup_dir / filename.removesuffix(".xbcrypt")
            assert restored.read_text() == original
            assert not ciphertext.exists()
            assert not touched.exists()
            assert not (backup_dir / marker).exists()
        finally:
            touched.unlink(missing_ok=True)


class TestDecryptAesParallelism:
    """Assert restore decrypts via a bounded pool of argv lists, never a shell pipeline."""

    def _decrypt(self, tmp_path: Path, xb_parallel: int | str | None):
        """Run decrypt against two files and return ``(pool_sizes, calls)``."""
        keyfile = _write_keyfile(tmp_path / "aes.key")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        targets = [backup_dir / "a.xbcrypt", backup_dir / "b.xbcrypt"]
        for path in targets:
            path.write_text("enc")
        pool_sizes: list[int] = []

        class _CapturingThreadPool(_RecordingThreadPool):
            def __init__(self, processes: int) -> None:
                pool_sizes.append(processes)
                super().__init__(processes)

        inst, _, calls = _restore_instance(
            _DECRYPT_METHODS,
            real_subprocess=False,
            extra_namespace={
                "thread_pool": types.SimpleNamespace(ThreadPool=_CapturingThreadPool)
            },
        )
        inst.xtrabackup_aes256_keyfile = str(keyfile)
        inst.xb_parallel = xb_parallel
        inst.decrypt_aes(str(backup_dir))
        return pool_sizes, calls, targets

    def test_legacy_missing_xb_parallel_is_bounded_not_unlimited(
        self, tmp_path: Path
    ) -> None:
        """Assert ``xb_parallel=0`` (legacy task default) requests 4 workers.

        Legacy task data with no ``XB_PARALLEL`` key resolves ``xb_parallel``
        to ``0``. Falling back to 4 keeps that from becoming unlimited parallelism.
        """
        pool_sizes, calls, targets = self._decrypt(tmp_path, xb_parallel=0)
        assert pool_sizes == [4]
        self._assert_argv_lists(calls, targets)

    def test_configured_value_passes_through(self, tmp_path: Path) -> None:
        """Assert an explicit operator-configured value is the pool size."""
        pool_sizes, calls, targets = self._decrypt(tmp_path, xb_parallel=2)
        assert pool_sizes == [2]
        self._assert_argv_lists(calls, targets)

    @pytest.mark.parametrize(
        ("xb_parallel", "expected"),
        [("0", 4), ("6", 6), ("nope", 4), (None, 4)],
    )
    def test_non_int_xb_parallel_is_coerced(
        self, tmp_path: Path, xb_parallel: str | None, expected: int
    ) -> None:
        """Assert a string or missing ``XB_PARALLEL`` becomes an int pool size."""
        pool_sizes, _, _ = self._decrypt(tmp_path, xb_parallel=xb_parallel)
        assert pool_sizes == [expected]

    @staticmethod
    def _assert_argv_lists(calls, targets: list[Path]) -> None:
        """Assert each decrypt is an argv list and ``shell`` is not passed."""
        assert len(calls) == len(targets)
        inputs = set()
        for cmd, kwargs in calls:
            assert isinstance(cmd, list)
            assert cmd[1] == "-d"
            assert "--encrypt-algo=AES256" in cmd
            assert "shell" not in kwargs
            inputs.update(arg for arg in cmd if str(arg).startswith("--input="))
        assert inputs == {f"--input={path}" for path in targets}
