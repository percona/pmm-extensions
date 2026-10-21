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

"""Share the AST-extraction harness for exercising a backup payload's methods.

A payload cannot be imported directly (it pulls boto3 and other heavy runtime
deps), so callers locate the relevant symbols in the source via AST and exec them
in an isolated namespace. This module holds the shared pieces so every test module
that reaches into a payload builds on one harness instead of re-exporting private
helpers from each other. ``payload_path`` selects the payload; it defaults to
xtrabackup, which most of the callers here exercise.
"""

import ast
import contextlib
import datetime
import logging
import multiprocessing.pool
import os
import pathlib
import re
import stat
import subprocess
import types
from collections.abc import Callable
from typing import Any

from tests.app.extensions.apps.mysql_backups.conftest import (
    payload_tree,
    XTRABACKUP_PAYLOAD_PATH,
    xtrabackup_payload_tree,
)

#: Home directory the stubbed ``pwd`` reports, so the constants that derive a path
#: from it resolve to the same value on every machine.
STUB_HOME = "/home/backupuser"

#: User the stubbed ``getpass`` reports when the environment names none.
STUB_USER = "backupuser"

# Module-level constants the extracted symbols read (default args / bodies).
_CONST_NAMES = frozenset(
    {
        "MD5SUM_FILE",
        "UPLOADME_FILE",
        "XTRABACKUP_INFO",
        "XTRABACKUP_CHECKPOINTS",
        "PLAINTEXT_METADATA_FILES",
        "ENCRYPTION_FORMATS",
        "BACKUP_TYPES",
        "XBCRYPT_BIN",
        "GPG_BIN",
        "XTRABACKUP_BIN",
        "XTRABACKUP_BIN_REAL",
        "XTRABACKUP_BIN_MARIADB",
        "BACKUP_DIR_REGEX",
        "MYDUMPER_BIN",
        "MYDUMPER_METADATA",
        "PARTIAL_MAX_AGE_SECONDS",
        "PARTIAL_SUFFIX",
        "REPLACED_SUFFIX",
        "CURRENT_USER",
        "CURRENT_USER_HOME_DIR",
        "BACKUP_TEXTFILE_COLLECTOR_DIR",
        "LZ4_BIN",
        "QPRESS_BIN",
        "ZSTD_BIN",
        "DEFAULT_LOGGING_DIR",
        "DEFAULT_LOGGING_STDOUT",
        "DEFAULT_MYCNF",
        "MYCNF_DIRS",
        "MYCNF_FILES",
        "DATADIR_CNF",
    }
)


def const_nodes(tree: ast.Module) -> list[ast.stmt]:
    """Return the whitelisted module-level constant assignments from the payload AST."""
    return [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in _CONST_NAMES
    ]


def base_namespace() -> dict:
    """Return an exec namespace seeded with real modules and a stub ``BackupError``.

    ``pwd`` and ``getpass`` are stubbed rather than real: the home-derived constants
    would otherwise resolve against whoever runs the suite, and ``pwd.getpwnam``
    raises for a user the passwd database does not carry — which a container can
    produce — taking every harness test with it.
    """
    namespace: dict = {
        "os": os,
        "subprocess": subprocess,
        "logging": logging,
        "re": re,
        "stat": stat,
        "datetime": datetime,
        "Path": pathlib.Path,
        "Any": object,
        "suppress": contextlib.suppress,
        "thread_pool": multiprocessing.pool,
        "pwd": types.SimpleNamespace(
            getpwnam=lambda _name: types.SimpleNamespace(pw_dir=STUB_HOME)
        ),
        "getpass": types.SimpleNamespace(getuser=lambda: STUB_USER),
        # The mydumper payload imports the function itself.
        "getuser": lambda: STUB_USER,
    }
    exec("class BackupError(Exception):\n    pass", namespace)  # noqa: S102
    return namespace


def load_constant(
    name: str, *, payload_path: pathlib.Path = XTRABACKUP_PAYLOAD_PATH
) -> object:
    """Return a whitelisted module-level constant's value from a payload's source.

    :param name: The constant to read; it has to be in ``_CONST_NAMES``.
    :param payload_path: The payload script to read the constant out of.
    :return: The constant's value.
    """
    namespace = base_namespace()
    exec(  # noqa: S102
        compile(
            ast.Module(body=const_nodes(payload_tree(payload_path)), type_ignores=[]),
            str(payload_path),
            "exec",
        ),
        namespace,
    )
    return namespace[name]


XBCRYPT_BIN = load_constant("XBCRYPT_BIN")


def load_function(
    name: str, *, payload_path: pathlib.Path = XTRABACKUP_PAYLOAD_PATH
) -> Callable[..., Any]:
    """Extract a single module-level payload function with its constants seeded.

    :param name: The function to extract; it has to be defined at module level.
    :param payload_path: The payload script to extract the function out of.
    :raises TypeError: If the extracted name is not callable.
    """
    tree = payload_tree(payload_path)
    namespace = base_namespace()
    body = const_nodes(tree)
    fn_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if not fn_nodes:
        raise RuntimeError(f"{name} not found in {payload_path}. Renamed or removed?")
    body = body + fn_nodes
    exec(  # noqa: S102
        compile(ast.Module(body=body, type_ignores=[]), str(payload_path), "exec"),
        namespace,
    )
    extracted = namespace[name]
    if not callable(extracted):
        raise TypeError(f"{name} in {payload_path} is not callable.")
    return extracted


def payload_method(
    class_name: str,
    method_name: str,
    *,
    extra_namespace: dict[str, object] | None = None,
    payload_path: pathlib.Path = XTRABACKUP_PAYLOAD_PATH,
) -> Callable[..., object]:
    """Return one payload method lifted out of its class as a plain function.

    ``payload_instance`` cannot run an ``__init__``: it builds a bases-less class,
    so ``super().__init__(...)`` has nothing to reach. Lifting the method to module
    level drops the ``__class__`` cell the compiler adds inside a class body, which
    leaves zero-arg ``super()`` an ordinary global lookup — so a caller can stub it
    and drive the rest of the method for real, passing its own object as ``self``.

    :param class_name: The payload class owning the method.
    :param method_name: The method to lift.
    :param extra_namespace: Globals the method body reads that are not whitelisted
        constants — module-level payload functions, or stand-ins for them.
    :param payload_path: The payload script to lift the method out of.
    :return: The lifted method, called as ``fn(self, ...)``.
    """
    tree = payload_tree(payload_path)
    class_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if not class_nodes:
        raise RuntimeError(
            f"{class_name} not found in {payload_path}. Renamed or removed?"
        )
    method_nodes = [
        node
        for node in class_nodes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    if not method_nodes:
        raise RuntimeError(
            f"{class_name}.{method_name} not found in {payload_path}. "
            "Renamed or removed?"
        )
    namespace = base_namespace()
    module = ast.fix_missing_locations(
        ast.Module(body=const_nodes(tree) + method_nodes, type_ignores=[])
    )
    exec(compile(module, str(payload_path), "exec"), namespace)  # noqa: S102
    namespace.update(extra_namespace or {})
    return namespace[method_name]


def gpg_probe(*, returncode: int = 0) -> tuple[Callable[..., bool], list[list[str]]]:
    """Return the payload's ``is_encrypted_dir`` wired to a faked ``gpg`` binary.

    :param returncode: Exit status every faked ``gpg`` run reports.
    :return: The lifted function and the list its ``Popen`` calls append to.
    """
    tree = xtrabackup_payload_tree()
    namespace = base_namespace()
    calls: list[list[str]] = []

    class _Popen:
        def __init__(self, cmd: list[str], **_kwargs: object) -> None:
            calls.append(list(cmd))
            self.returncode = returncode

        def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"err"

    namespace["subprocess"] = types.SimpleNamespace(Popen=_Popen, PIPE=-1)
    fn_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "is_encrypted_dir"
    ]
    exec(  # noqa: S102
        compile(
            ast.Module(body=const_nodes(tree) + fn_nodes, type_ignores=[]),
            str(XTRABACKUP_PAYLOAD_PATH),
            "exec",
        ),
        namespace,
    )
    return namespace["is_encrypted_dir"], calls


class Recorder:
    """Collect the messages a payload method logs, in order."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def info(self, msg: str, *args: object) -> None:
        """Record an informational message.

        :param msg: The message, or the ``%``-style format string for one.
        :param args: The values that format string interpolates.
        """
        self.infos.append(msg % args if args else msg)

    def warning(self, msg: str, *args: object) -> None:
        """Record a warning.

        :param msg: The message, or the ``%``-style format string for one.
        :param args: The values that format string interpolates.
        """
        self.warnings.append(msg % args if args else msg)

    warn = warning

    def debug(self, msg: str, *args: object) -> None:
        """Discard a debug message.

        :param msg: The message, or the ``%``-style format string for one.
        :param args: The values that format string interpolates.
        """

    def error(self, msg: str, *args: object) -> None:
        """Record an error.

        :param msg: The message, or the ``%``-style format string for one.
        :param args: The values that format string interpolates.
        """
        self.errors.append(msg % args if args else msg)

    exception = error

    @property
    def messages(self) -> list[str]:
        """Return every recorded message regardless of level.

        :return: The recorded messages, informational ones first.
        """
        return [*self.infos, *self.warnings, *self.errors]


class FakeProc:
    """Stand in for a ``Popen`` result with a fixed return code and canned stderr."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def communicate(self) -> tuple[bytes, bytes]:
        """Return ``(stdout, stderr)``, stderr is non-empty so error paths format it."""
        return b"", b"boom"


def payload_instance(
    method_names: tuple[str, ...],
    *,
    returncode: int = 0,
    extra_namespace: dict[str, object] | None = None,
    real_subprocess: bool = False,
    payload_path: pathlib.Path = XTRABACKUP_PAYLOAD_PATH,
) -> tuple[object, type[Exception], list[list[str]]]:
    """Build an instance of a synthetic class carrying the named payload methods.

    The methods are lifted verbatim from the payload and exec'd into a class over
    a namespace whose ``subprocess`` is faked (recording every command and
    returning ``returncode``). Returns ``(instance, BackupError, calls)`` where
    ``calls`` is the list of xbcrypt argv lists the code tried to run.

    :param method_names: The payload method names to lift into the synthetic class.
    :param returncode: The return code the faked ``subprocess.Popen`` reports.
    :param extra_namespace: Extra globals (e.g. a fake module-level function, or a
        payload constant override such as ``XBCRYPT_BIN``) merged into the exec
        namespace after the payload's own constants are loaded (so this wins),
        before the class is compiled.
    :param real_subprocess: When True, keep the real ``subprocess`` module instead
        of faking ``Popen``, for integration tests that need a real process (e.g.
        a stand-in ``xbcrypt`` executable) to actually run. ``calls`` is unused
        (always ``[]``) in this mode.
    :param payload_path: The payload script to lift the methods out of.
    """
    tree = payload_tree(payload_path)
    method_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in method_names
    ]
    missing = set(method_names) - {node.name for node in method_nodes}
    if missing:
        raise RuntimeError(f"{sorted(missing)} not found in {payload_path}.")

    calls: list[list[str]] = []

    class _FakeSubprocess:
        PIPE = -1

        # N802: signature mirrors ``subprocess.Popen`` verbatim so the payload's
        # real call site binds unchanged. ``list.append`` is GIL-atomic, so this
        # stays safe when ``encrypt_files_aes256`` calls it from pool threads.
        @staticmethod
        def Popen(cmd: list[str], **_kwargs: object) -> FakeProc:  # noqa: N802
            calls.append(list(cmd))
            return FakeProc(returncode)

    namespace = base_namespace()
    if not real_subprocess:
        namespace["subprocess"] = _FakeSubprocess
    exec(  # noqa: S102
        compile(
            ast.Module(body=const_nodes(tree), type_ignores=[]),
            str(payload_path),
            "exec",
        ),
        namespace,
    )
    namespace.update(extra_namespace or {})
    cls = ast.ClassDef(
        name="_Payload",
        bases=[],
        keywords=[],
        body=list(method_nodes),
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(payload_path), "exec"), namespace)  # noqa: S102

    inst = namespace["_Payload"]()
    inst.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    # Stand-ins for the collaborators a lifted method calls, skipped for any method
    # the caller lifted itself: an instance attribute would shadow the real one.
    stubs = {
        "_clean_after_error": lambda: None,
        "_check_config": lambda: None,
        "get_compression_ext": lambda: "",
    }
    for name, stub in stubs.items():
        if name not in method_names:
            setattr(inst, name, stub)
    inst.aes_keyfile = "/keys/aes.key"
    # Binlog/XtraBackup Upload encrypt paths read this historical attribute name.
    inst.xtrabackup_aes256 = "/keys/aes.key"
    inst.enc_aes = True
    inst.enc_gpg = False
    inst.compress = False
    # XtraBackup ``run`` / the runner read these after notify; synthetic
    # instances never call ``Backup.__init__``, so seed them here.
    inst.mycnf_copy_skipped = False
    inst.notify_status = 0  # STATUS_OK
    return inst, namespace["BackupError"], calls


def _config_reader_state() -> dict[str, object]:
    """Return the state every config-reading payload method expects.

    No ``server_data``, no option files, and compression on, since the checks that
    read a config are only reachable with it on. One layer over what
    ``payload_instance`` seeds for the encryption callers, so a caller reads one
    table rather than two. Rebuilt per call so a test that mutates ``server_data``
    cannot reach the next one.

    :return: The attribute names and values to seed.
    """
    return {
        "server_data": {},
        "defaults_cnf_file": None,
        "defaults_file": None,
        "compress": True,
        "xtrabackup_bin_cmd": "xtrabackup",
        "compression_algorithm": "zstd",
    }


def seeded_instance(
    method_names: tuple[str, ...], **attributes: object
) -> tuple[object, type[Exception]]:
    """Build a payload instance carrying the named methods and the given state.

    Seeds ``_config_reader_state`` so a caller names only what its own assertion
    turns on. Typed ``object`` rather than a narrower union because the seeded values
    are genuinely heterogeneous (a dict, paths, a bool, algorithm names) and the
    instance they land on is a synthetic class with no annotations to match.

    :param method_names: The payload method names to lift into the instance.
    :param attributes: Instance attributes overriding the seeded defaults.
    :return: The instance and the payload's own ``BackupError``.
    """
    inst, backup_error, _ = payload_instance(
        method_names,
        extra_namespace={
            "supported_compression": load_function("supported_compression")
        },
    )
    for name, value in (_config_reader_state() | attributes).items():
        setattr(inst, name, value)
    return inst, backup_error
