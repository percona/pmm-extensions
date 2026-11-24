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

"""Find the Python files PMM Extensions ships to executor hosts and check them against 3.9.

Executor hosts run payloads under their own ``python3``, which can be as old as
:data:`MINIMUM_HOST_PYTHON`, while the suite runs under 3.14. The guard in
:mod:`tests.app.test_host_payloads` closes that gap with two branches, because
each catches what the other misses: :func:`load_under` loads each file under a
real 3.9 interpreter, which catches syntax, module-level APIs and annotations
evaluated at definition time; :func:`static_violations` runs ``vermin`` over the
same files, which catches standard-library APIs used only inside function bodies,
which loading never executes. Neither sees a ``X | Y`` type union evaluated
inside a function body, so :func:`runtime_union_violations` looks for those. All
three read the unminified source; what dispatch sends once minified has its own
tests in :mod:`tests.app.test_host_payloads`.

The file set is derived rather than listed. :func:`discover_host_payloads` takes
the payload naming conventions, every literal ``payload_uri(__file__, "...")``
target, and the artifact directories, and :func:`drift_violations` then checks
the packages that reference payloads for a file the derivation missed. What the
tree cannot reveal is a ``.py`` payload beside ordinary package modules that is
referenced other than through ``payload_uri`` or a ``file://`` f-string, or
chosen by a computed rather than literal name: give such a payload a
``payloads/`` directory, as topology and Dipper do, or name it with a literal
``payload_uri`` argument.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Collection, Iterator, Mapping, Sequence
from functools import cache
from pathlib import Path

from tests.app.import_ast import absolute_base, package_of

#: The oldest Python an executor host is documented to run payloads under.
MINIMUM_HOST_PYTHON = (3, 9)
#: :data:`MINIMUM_HOST_PYTHON` in dotted form, as messages and ``vermin`` spell it.
MINIMUM_HOST_PYTHON_VERSION = ".".join(map(str, MINIMUM_HOST_PYTHON))

#: The environment variable naming the interpreter the load branch runs under.
CHECK_PYTHON_ENV = "EXTENSIONS_PAYLOAD_CHECK_PYTHON"

#: The repo-relative globs naming host payloads by convention.
PAYLOAD_GLOBS = (
    "app/extensions/apps/**/payload",
    "app/extensions/apps/**/*_payload",
    "app/extensions/apps/**/payloads/*.py",
    "app/extensions/sync/syncers/**/payload.py",
    "app/tasks/**/payload.py",
)

#: The argv a payload that parses its arguments at import needs in order to load.
LOAD_ARGV: Mapping[str, tuple[str, ...]] = {
    "app/extensions/apps/dipper/payloads/pcs-collect-pmm-mysql.py": (
        "--list",
        "https://user:pass@payload-check.invalid/",
    ),
    "app/extensions/apps/dipper/payloads/pcs-collect-pmm-valkey.py": (
        "--list",
        "https://user:pass@payload-check.invalid/",
    ),
}

_LOADER = Path(__file__).with_name("host_payload_loader.py")
_PAYLOAD_URI = "payload_uri"
_PATH_HELPERS = Path("app/core/utils/path.py")
_LOAD_TIMEOUT_SECONDS = 60


class InterpreterMismatchError(RuntimeError):
    """Raise when the configured check interpreter is not the minimum host Python."""


def _runs_under_another_interpreter(path: Path) -> bool:
    """Return whether ``path``'s shebang names an interpreter other than Python.

    :param path: A regular file.
    :return: Whether the file is, say, a shell script rather than a Python payload.
    """
    with path.open("rb") as source:
        first_line = source.readline()
    return first_line.startswith(b"#!") and b"python" not in first_line


@cache
def _module_trees(root: Path) -> dict[Path, ast.Module]:
    """Return the parsed tree of every non-migration module under ``root/app``.

    :param root: The repository root.
    :return: Each module's tree, keyed by its path.
    """
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted((root / "app").rglob("*.py"))
        if "migrations" not in path.relative_to(root).parts
    }


def _payload_uri_names(tree: ast.Module) -> set[str]:
    """Return the local names ``tree`` binds to ``payload_uri``.

    :param tree: A parsed module.
    :return: ``payload_uri`` itself plus every alias it is imported under.
    """
    names = {_PAYLOAD_URI}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == _PAYLOAD_URI and alias.asname
            )
    return names


def _payload_uri_calls(tree: ast.Module) -> Iterator[ast.Call]:
    """Yield every call ``tree`` makes to ``payload_uri``, under any spelling.

    :param tree: A parsed module.
    :return: The calls, whether by the imported name, an alias, or an attribute.
    """
    names = _payload_uri_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in names) or (
            isinstance(func, ast.Attribute) and func.attr == _PAYLOAD_URI
        ):
            yield node


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    """Return the argument ``call`` passes at ``position`` or as ``keyword``.

    :param call: A call node.
    :param position: The parameter's positional index.
    :param keyword: The parameter's name.
    :return: The argument expression, or ``None`` when it is not passed.
    """
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def _literal_target(module: Path, call: ast.Call) -> Path | None:
    """Return the file a ``payload_uri`` call names, when it names one literally.

    :param module: The module making the call.
    :param call: The ``payload_uri`` call.
    :return: The path beside ``module`` the literal resolves to, or ``None`` when
        the anchor is not ``__file__`` or the name is computed.
    """
    anchor = _argument(call, 0, "anchor_file")
    name = _argument(call, 1, "name")
    if not (isinstance(anchor, ast.Name) and anchor.id == "__file__"):
        return None
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    return module.parent / name.value


def literal_payload_references(root: Path) -> list[tuple[Path, Path]]:
    """Return every literal ``payload_uri(__file__, "<name>")`` reference in ``app/``.

    :param root: The repository root.
    :return: ``(module, target)`` pairs, whether or not the target exists.
    """
    return [
        (module, target)
        for module, tree in _module_trees(root).items()
        for call in _payload_uri_calls(tree)
        if (target := _literal_target(module, call)) is not None
    ]


def discover_host_payloads(
    root: Path, artifact_dirs: Sequence[Path] = ()
) -> list[Path]:
    """Return every Python file PMM Extensions ships to executor hosts.

    The union of the conventional payload globs, the existing targets of literal
    ``payload_uri`` references, and the ``*.py`` files of each artifact directory,
    less any file whose shebang names another interpreter. Extensionless payloads
    are included by construction.

    :param root: The repository root.
    :param artifact_dirs: Directories whose ``*.py`` files are shipped as artifacts.
    :return: The payload paths, sorted.
    """
    found = {path for pattern in PAYLOAD_GLOBS for path in root.glob(pattern)}
    found.update(target for _, target in literal_payload_references(root))
    found.update(path for directory in artifact_dirs for path in directory.glob("*.py"))
    return sorted(
        path
        for path in found
        if path.is_file() and not _runs_under_another_interpreter(path)
    )


def _references_a_payload(tree: ast.Module) -> bool:
    """Return whether ``tree`` calls ``payload_uri`` or builds a ``file://`` f-string.

    :param tree: A parsed module.
    :return: Whether the module is a payload reference site.
    """
    if next(_payload_uri_calls(tree), None) is not None:
        return True
    return any(
        isinstance(node, ast.JoinedStr)
        and node.values
        and isinstance(first := node.values[0], ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith("file://")
        for node in ast.walk(tree)
    )


def reference_site_packages(root: Path) -> set[Path]:
    """Return the directories of the ``app/`` modules that reference a payload.

    :param root: The repository root.
    :return: Each directory holding a module that calls ``payload_uri`` or builds a
        ``file://`` f-string, excluding the helper that builds such references.
    """
    return {
        module.parent
        for module, tree in _module_trees(root).items()
        if module.relative_to(root) != _PATH_HELPERS and _references_a_payload(tree)
    }


def _module_file(root: Path, dotted: str) -> Path | None:
    """Return the file a dotted module name resolves to under ``root``.

    :param root: The repository root.
    :param dotted: An absolute dotted module name.
    :return: Its ``.py`` file or package ``__init__.py``, or ``None`` when neither
        exists.
    """
    base = root.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_module_files(root: Path) -> set[Path]:
    """Return every ``app/`` module file some ``app/`` module imports.

    :param root: The repository root.
    :return: The files reached by an absolute or relative import.
    """
    dotted: set[str] = set()
    for module, tree in _module_trees(root).items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dotted.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = absolute_base(node, package_of(module, root))
                if base is not None:
                    dotted.add(base)
                    dotted.update(f"{base}.{alias.name}" for alias in node.names)
    return {path for name in dotted if (path := _module_file(root, name)) is not None}


def _string_literals(tree: ast.Module) -> set[str]:
    """Return every string constant in ``tree``, docstrings included.

    :param tree: A parsed module.
    :return: The distinct string values.
    """
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _may_be_python(path: Path) -> bool:
    """Return whether ``path`` could be a Python payload by name and shebang.

    :param path: A regular file.
    :return: Whether it is ``.py`` or extensionless, and not another
        interpreter's script.
    """
    return path.suffix in {"", ".py"} and not _runs_under_another_interpreter(path)


def drift_violations(root: Path, discovered: Collection[Path]) -> list[str]:
    """Return every file a payload reference site needs that ``discovered`` lacks.

    Three rules, each applied to the packages :func:`reference_site_packages`
    returns:

    - R1: a literal ``payload_uri`` target must exist and be discovered, unless
      it is another interpreter's script.
    - R2: an extensionless file in such a package, or in its ``payloads/``
      directory, must be discovered unless it is another interpreter's script,
      since extensionless files there are payloads by convention.
    - R3: a sibling file one of the package's modules names by a string literal
      must be discovered, unless ``app/`` imports it as a module. This catches a
      payload chosen through a mapping or a branch.

    A literal ``payload_uri`` target is judged by R1 alone, so one missing file is
    reported once.

    :param root: The repository root.
    :param discovered: The file set to check, normally
        :func:`discover_host_payloads`'s result.
    :return: One line per violation, naming the file and the rule.
    """
    covered = set(discovered)
    violations: list[str] = []
    references = literal_payload_references(root)
    referenced = {target for _, target in references}
    for module, target in references:
        where = module.relative_to(root)
        if not target.is_file():
            violations.append(
                f"R1: {where} names {target.name!r}, which does not exist beside it"
            )
        elif target not in covered and not _runs_under_another_interpreter(target):
            violations.append(
                f"R1: {target.relative_to(root)} is named by {where} but not discovered"
            )
    packages = reference_site_packages(root)
    known = covered | referenced
    violations.extend(
        f"R2: {path.relative_to(root)} is extensionless beside a payload reference"
        " but not discovered"
        for package in sorted(packages)
        for directory in (package, package / "payloads")
        if directory.is_dir()
        for path in sorted(directory.iterdir())
        if path.is_file()
        and not path.suffix
        and not path.name.startswith(".")
        and path not in known
        and not _runs_under_another_interpreter(path)
    )
    exempt = _imported_module_files(root) | referenced
    trees = _module_trees(root)
    for package in sorted(packages):
        siblings = {
            path.name: path
            for path in package.iterdir()
            if path.is_file() and _may_be_python(path)
        }
        named: set[Path] = set()
        for module in sorted(package.glob("*.py")):
            named.update(
                siblings[literal]
                for literal in _string_literals(trees[module])
                if literal in siblings
            )
        violations.extend(
            f"R3: {path.relative_to(root)} is named by a literal in its package"
            " but not discovered"
            for path in sorted(named - covered - exempt)
        )
    return violations


def _interpreter_version(python: str) -> tuple[int, int]:
    """Return the ``(major, minor)`` version ``python`` reports.

    :param python: The interpreter to ask.
    :return: Its major and minor version.
    :raises subprocess.CalledProcessError: When the interpreter fails to run.
    """
    result = subprocess.run(  # noqa: S603
        [python, "-c", "import sys; print(*sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
    )
    major, minor = result.stdout.split()
    return int(major), int(minor)


def resolve_py39_interpreter() -> str | None:
    """Return the interpreter the load branch runs under, or ``None`` if absent.

    ``$EXTENSIONS_PAYLOAD_CHECK_PYTHON`` wins when set, and otherwise ``python3.9`` on
    ``PATH`` is used. Either way the interpreter must report
    :data:`MINIMUM_HOST_PYTHON`: a check silently run under a newer Python would
    pass the very files it exists to fail.

    :return: The interpreter path, or ``None`` when neither source names one.
    :raises InterpreterMismatchError: When the named interpreter does not exist or
        reports another version.
    :raises subprocess.CalledProcessError: When the named interpreter fails to run.
    """
    configured = os.environ.get(CHECK_PYTHON_ENV)
    python = configured or shutil.which(f"python{MINIMUM_HOST_PYTHON_VERSION}")
    if python is None:
        return None
    if not Path(python).is_file():
        raise InterpreterMismatchError(f"{CHECK_PYTHON_ENV}={python} does not exist")
    if (version := _interpreter_version(python)) != MINIMUM_HOST_PYTHON:
        raise InterpreterMismatchError(
            f"{python} is Python {'.'.join(map(str, version))}, "
            f"not {MINIMUM_HOST_PYTHON_VERSION}"
        )
    return python


def missing_interpreter_is_fatal() -> bool:
    """Return whether a missing check interpreter fails the guard instead of skipping.

    CI provisions the interpreter, so there a missing one means the provisioning
    step was removed, which must turn the build red rather than quietly skip the
    load branch. A developer checkout may simply not have 3.9.

    :return: Whether ``$CI`` is set to a truthy value.
    """
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def load_under(
    python: str, payload: Path, workdir: Path, argv: Sequence[str] = ()
) -> str | None:
    """Load ``payload`` under ``python`` and return why it failed, if it did.

    The payload runs in a fresh subprocess, in isolated mode, with ``workdir`` as
    its working directory, home and Nomad task and alloc directories, so a file it
    reads or writes relative to any of those at import lands in the scratch
    directory rather than the checkout or the developer's home.

    :param python: The interpreter to load under.
    :param payload: The payload file.
    :param workdir: A scratch directory the payload may write to.
    :param argv: The argv the payload needs in order to load.
    :return: The failure output, or ``None`` when the payload loaded.
    :raises subprocess.TimeoutExpired: When loading does not finish in time.
    """
    result = subprocess.run(  # noqa: S603
        [python, "-I", str(_LOADER), str(payload), *argv],
        input=json.dumps(sorted(sys.stdlib_module_names)),
        capture_output=True,
        text=True,
        cwd=workdir,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(workdir),
            "NOMAD_TASK_DIR": str(workdir),
            "NOMAD_ALLOC_DIR": str(workdir),
        },
        timeout=_LOAD_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"


def static_violations(paths: Sequence[Path]) -> str | None:
    """Run ``vermin`` over ``paths`` and return its report if any needs a newer Python.

    :param paths: The files to analyse.
    :return: The violations report, or ``None`` when every file supports
        :data:`MINIMUM_HOST_PYTHON`.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys, vermin; sys.exit(vermin.main())",
            "--no-config-file",
            f"--target={MINIMUM_HOST_PYTHON_VERSION}-",
            "--violations",
            "--no-tips",
            *map(str, paths),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stdout + result.stderr).strip()


def _annotation_node_ids(tree: ast.Module) -> set[int]:
    """Return the ids of every node inside an annotation in ``tree``.

    :param tree: A parsed module.
    :return: The ``id()`` of each node under a parameter, return or variable
        annotation.
    """
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            parameters.extend(arg for arg in (args.vararg, args.kwarg) if arg)
            annotations.extend(p.annotation for p in parameters if p.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return {id(sub) for annotation in annotations for sub in ast.walk(annotation)}


#: Builtin type names that mark a ``|`` operand as a type rather than a value.
_BUILTIN_TYPE_NAMES = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "set",
        "str",
        "tuple",
        "type",
    }
)


def _union_operands(node: ast.expr) -> Iterator[ast.BinOp]:
    """Yield every ``|`` expression within ``node``, ``node`` included.

    :param node: The expression to search.
    :return: The ``BinOp`` nodes whose operator is ``|``.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            yield sub


def _cast_names(tree: ast.Module) -> set[str]:
    """Return the local names ``tree`` binds to ``typing.cast``.

    :param tree: A parsed module.
    :return: ``cast`` itself plus every alias it is imported under.
    """
    names = {"cast"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == "cast" and alias.asname
            )
    return names


def _is_cast_call(node: ast.Call, cast_names: Collection[str]) -> bool:
    """Return whether ``node`` calls ``typing.cast`` under any name it is known by.

    :param node: A call expression.
    :param cast_names: Local names bound to ``cast`` (see :func:`_cast_names`).
    :return: Whether ``node.func`` is a bare name in ``cast_names`` or any
        attribute access named ``cast`` (``typing.cast``, ``t.cast``).
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in cast_names
    return isinstance(func, ast.Attribute) and func.attr == "cast"


def _is_static_type_operand(node: ast.expr) -> bool:
    """Return whether ``node`` names a type rather than a value.

    :param node: One operand of a ``|`` expression.
    :return: Whether ``node`` is ``None``, a builtin type name, or a
        subscripted builtin type name (``list[int]``).
    """
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Name):
        return node.id in _BUILTIN_TYPE_NAMES
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Name) and node.value.id in _BUILTIN_TYPE_NAMES
    return False


def runtime_union_violations(paths: Sequence[Path]) -> list[str]:
    """Return every ``X | Y`` type union ``paths`` evaluate outside an annotation.

    Python 3.9 has no ``|`` on types, so ``isinstance(value, int | str)`` raises
    ``TypeError`` when the function runs. Loading never runs a function body and
    ``vermin`` does not flag the expression. An ``|`` counts as a type union when
    either: it sits in a type-expression argument, namely the second argument of
    ``isinstance``/``issubclass`` or the first argument of ``typing.cast`` (under
    a bare, aliased, or qualified name); or one of its operands is statically a
    type, namely ``None``, a builtin type name (``int``, ``list``, ...), or a
    subscripted builtin type name (``list[int]``). Integer and set ``|`` fit
    neither. Annotations are left to the load branch, which evaluates the ones
    Python evaluates.

    :param paths: The files to analyse.
    :return: One line per union, naming the file and line.
    """
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        in_annotation = _annotation_node_ids(tree)
        cast_names = _cast_names(tree)
        unions: dict[int, ast.BinOp] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"isinstance", "issubclass"}
                and len(node.args) > 1
            ):
                unions.update((id(u), u) for u in _union_operands(node.args[1]))
            elif (
                isinstance(node, ast.Call)
                and _is_cast_call(node, cast_names)
                and node.args
            ):
                unions.update((id(u), u) for u in _union_operands(node.args[0]))
            elif (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.BitOr)
                and any(
                    _is_static_type_operand(side) for side in (node.left, node.right)
                )
            ):
                unions[id(node)] = node
        nested = {id(side) for u in unions.values() for side in (u.left, u.right)}
        violations.extend(
            f"{path}:{union.lineno}: {ast.unparse(union)} is a type union Python "
            f"{MINIMUM_HOST_PYTHON_VERSION} cannot evaluate"
            for key, union in sorted(unions.items(), key=lambda item: item[1].lineno)
            if key not in in_annotation and key not in nested
        )
    return violations
