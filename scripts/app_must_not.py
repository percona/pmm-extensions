#!/usr/bin/env python3
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

"""Check what an app must not do, from its source and without importing it.

An app's Python runs inside the sidecar with the sidecar's reach. This suite
reports what would make that reach larger than the app's form:

* an import of a module that starts a process or opens a network connection;
* a call that runs a shell command or evaluates text as code;
* a spec builder that returns something other than a framework spec class, a
  class that stands in for one, and a ``RunCommandSpec`` whose argument string
  ``shlex.join`` did not build.

A file under ``payload/`` or ``payloads/`` is not read: a payload runs on the
database host, and what it does there is the app's purpose. Two things keep
that true. The suite reports an import of a payload's module from the app's
other code, because that import runs the module inside the sidecar. It reads
each ``__init__.py`` of a payload, because an import of the package runs it.

Usage: ``python scripts/app_must_not.py <app directory>``. Exit status 0 when
the app has no finding, 1 when it has one, 2 when the directory is not there.
"""

import ast
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_MODULES = (
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "urllib.request",
    "urllib3",
    "http.client",
    "aiohttp",
    "asyncio.subprocess",
)
FORBIDDEN_CALLS = (
    "os.system",
    "os.popen",
    "eval",
    "exec",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "asyncio.open_connection",
    "asyncio.open_unix_connection",
    "importlib.import_module",
    "__import__",
)
FORBIDDEN_CALL_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
SPEC_CLASSES = ("RunCommandSpec", "RunPythonSpec")
PAYLOAD_DIRECTORIES = ("payload", "payloads")
EXIT_FINDINGS = 1
EXIT_NO_APP = 2

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class Finding:
    """Hold one thing an app does that it must not.

    :param path: The file that holds it.
    :param line: The line, counted from 1.
    :param message: What was found, and what the suite expects in its place.
    """

    path: Path
    line: int
    message: str


def _is_forbidden_module(name: str) -> bool:
    """Tell whether ``name`` is a forbidden module or a module inside one.

    :param name: A dotted module name.
    :return: ``True`` when importing it is a finding.
    """
    return any(
        name == module or name.startswith(f"{module}.") for module in FORBIDDEN_MODULES
    )


def _is_forbidden_call(name: str) -> bool:
    """Tell whether ``name`` starts a process, connects, evaluates text or imports by name.

    :param name: The dotted name of a function.
    :return: ``True`` when calling it, or importing it, is a finding.
    """
    return name in FORBIDDEN_CALLS or name.startswith(FORBIDDEN_CALL_PREFIXES)


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Return the other names that a file gives to modules.

    :param tree: The file's syntax tree.
    :return: The module's name for each alias of ``import module as alias``.
    """
    return {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.asname
    }


def _imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return every dotted name that an import statement can bind.

    :param node: The import statement.
    :return: The module names, and for ``from a import b`` also ``a.b``.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def _call_name(node: ast.Call) -> str:
    """Return the dotted name that a call names, or an empty string.

    :param node: The call.
    :return: ``name`` for ``name(...)``, ``owner.name`` for ``owner.name(...)``.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    return ""


def _is_spec_call(node: ast.AST | None) -> bool:
    """Tell whether ``node`` constructs one of the framework's spec classes.

    :param node: An expression, or ``None`` for a bare ``return``.
    :return: ``True`` for ``RunCommandSpec(...)`` and ``RunPythonSpec(...)``.
    """
    return (
        isinstance(node, ast.Call) and _call_name(node).split(".")[-1] in SPEC_CLASSES
    )


def _is_safe_args(node: ast.AST) -> bool:
    """Tell whether ``node`` is an argument string that cannot carry a shell word.

    :param node: The expression given as a ``RunCommandSpec``'s ``args``.
    :return: ``True`` for a ``shlex.join(...)`` call and for a string literal.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.Call) and _call_name(node) == "shlex.join"


def _own_nodes(function: FunctionNode) -> Iterator[ast.AST]:
    """Yield the nodes of a function's body, without those of a nested function.

    :param function: The function to walk.
    :return: Every node that belongs to ``function`` itself.
    """
    pending = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, FunctionNode | ast.Lambda | ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))


def _values_bound_to(function: FunctionNode, name: str) -> list[ast.AST]:
    """Return every expression that ``function`` assigns to the variable ``name``.

    :param function: The function to search.
    :param name: The variable's name.
    :return: The assigned expressions. Empty when the function never assigns it.
    """
    values: list[ast.AST] = []
    for node in _own_nodes(function):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = [node.target]
        else:
            continue
        if node.value is not None and any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            values.append(node.value)
    return values


def _holds(
    function: FunctionNode | None,
    node: ast.AST | None,
    accept: Callable[[ast.AST], bool],
) -> bool:
    """Tell whether an expression, or the variable it names, passes ``accept``.

    :param function: The function that holds the expression, if any.
    :param node: The expression.
    :param accept: The test for one expression.
    :return: ``True`` when ``node`` passes, or names a variable of ``function``
        whose every assigned value passes.
    """
    if node is not None and accept(node):
        return True
    if function is None or not isinstance(node, ast.Name):
        return False
    values = _values_bound_to(function, node.id)
    return bool(values) and all(accept(value) for value in values)


def _spec_args(call: ast.Call) -> ast.AST | None:
    """Return the expression given as the ``args`` of a ``RunCommandSpec`` call.

    :param call: The call that constructs the spec.
    :return: The keyword's value, the second positional argument, or ``None``.
    """
    for keyword in call.keywords:
        if keyword.arg == "args":
            return keyword.value
    return call.args[1] if len(call.args) > 1 else None


def _nodes_with_function(
    tree: ast.Module,
) -> Iterator[tuple[FunctionNode | None, ast.AST]]:
    """Yield every node of a module with the function that holds it.

    :param tree: The parsed module.
    :return: Pairs of the innermost enclosing function, or ``None`` at module
        level, and the node.
    """
    pending: list[tuple[FunctionNode | None, ast.AST]] = [(None, tree)]
    while pending:
        function, node = pending.pop()
        yield function, node
        inner = node if isinstance(node, FunctionNode) else function
        pending.extend((inner, child) for child in ast.iter_child_nodes(node))


def _check_import(node: ast.Import | ast.ImportFrom) -> str | None:
    """Return the finding for an import statement, if it is one.

    :param node: The import statement.
    :return: The finding's message, or ``None``.
    """
    names = _imported_names(node)
    if any(_is_forbidden_module(name) for name in names):
        return (
            f"imports {names[0]}: an app starts no process and opens no "
            "network connection of its own"
        )
    if any(_is_forbidden_call(name) for name in names):
        return (
            "imports a function that starts a process or opens a connection: "
            "an app does neither of its own"
        )
    if any(set(name.split(".")[:-1]) & set(PAYLOAD_DIRECTORIES) for name in names):
        return (
            "imports a module of the payload: a payload runs on the database "
            "host, and its code must not run inside the sidecar"
        )
    return None


def _check_call(
    function: FunctionNode | None, node: ast.Call, aliases: dict[str, str]
) -> str | None:
    """Return the finding for a call, if it is one.

    :param function: The function that holds the call, if any.
    :param node: The call.
    :param aliases: The module's name for each alias that the file gives one.
    :return: The finding's message, or ``None``.
    """
    name = _call_name(node)
    owner, dot, attribute = name.rpartition(".")
    name = f"{aliases[owner]}{dot}{attribute}" if owner in aliases else name
    if _is_forbidden_call(name):
        return (
            f"calls {name}(): an app starts no process, opens no connection, "
            "evaluates no text and imports no module by name"
        )
    if name.split(".")[-1] == "RunCommandSpec" and not _holds(
        function, _spec_args(node), _is_safe_args
    ):
        return (
            "builds the args of a RunCommandSpec without shlex.join: a form "
            "value joined into the string can carry a shell word"
        )
    return None


def _check_class(node: ast.ClassDef) -> str | None:
    """Return the finding for a class that stands in for a framework spec class.

    :param node: The class definition.
    :return: The finding's message, or ``None``.
    """
    if any(
        isinstance(item, FunctionNode) and item.name == "to_envelope_data"
        for item in node.body
    ):
        return (
            f"defines {node.name}, a spec class of its own: use RunCommandSpec "
            "or RunPythonSpec"
        )
    return None


def _check_builder(function: FunctionNode) -> str | None:
    """Return the finding for a spec builder that returns no framework spec.

    :param function: The spec builder.
    :return: The finding's message, or ``None``.
    """
    returns = [node for node in _own_nodes(function) if isinstance(node, ast.Return)]
    if returns and all(_holds(function, node.value, _is_spec_call) for node in returns):
        return None
    return (
        f"{function.name} is a spec builder that returns something other than "
        "RunCommandSpec or RunPythonSpec"
    )


def _builder_names(tree: ast.Module) -> set[str]:
    """Return the names of the functions a module gives as ``task_spec_builder``.

    :param tree: The parsed module.
    :return: The function names.
    """
    return {
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "task_spec_builder" and isinstance(keyword.value, ast.Name)
    }


def _check_node(
    function: FunctionNode | None,
    node: ast.AST,
    builders: set[str],
    aliases: dict[str, str],
) -> str | None:
    """Return the finding for one node, if it is one.

    :param function: The function that holds the node, if any.
    :param node: The node.
    :param builders: The names of the app's spec builders.
    :param aliases: The module's name for each alias that the file gives one.
    :return: The finding's message, or ``None``.
    """
    if isinstance(node, ast.Import | ast.ImportFrom):
        return _check_import(node)
    if isinstance(node, ast.Call):
        return _check_call(function, node, aliases)
    if isinstance(node, ast.ClassDef):
        return _check_class(node)
    if isinstance(node, FunctionNode) and function is None and node.name in builders:
        return _check_builder(node)
    return None


def check_app(app_dir: Path) -> list[Finding]:
    """Return everything the app in ``app_dir`` does that it must not.

    :param app_dir: The app's package directory.
    :return: The findings, ordered by file and line.
    """
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(app_dir.rglob("*.py"))
        if path.name == "__init__.py"
        or not set(path.relative_to(app_dir).parts[:-1]) & set(PAYLOAD_DIRECTORIES)
    }
    builders = set().union(*(_builder_names(tree) for tree in trees.values()))
    aliases = {path: _module_aliases(tree) for path, tree in trees.items()}
    findings = [
        Finding(path, getattr(node, "lineno", 1), message)
        for path, tree in trees.items()
        for function, node in _nodes_with_function(tree)
        if (message := _check_node(function, node, builders, aliases[path])) is not None
    ]
    return sorted(findings, key=lambda finding: (finding.path, finding.line))


def main(argv: list[str]) -> int:
    """Check one app and print its findings.

    :param argv: The command line's arguments: the app's directory.
    :return: The exit status.
    """
    app_dir = Path(argv[0]) if argv else None
    if app_dir is None or not app_dir.is_dir():
        print(f"no app directory at {app_dir}", file=sys.stderr)
        return EXIT_NO_APP
    findings = check_app(app_dir)
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.message}")
    return EXIT_FINDINGS if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
