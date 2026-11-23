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

"""Load one host-side payload under the interpreter running this script.

:mod:`tests.app.host_payloads` runs this file as a subprocess of the oldest Python
an executor host may carry: ``python3.9 -I host_payload_loader.py <payload>
[argv...]``, with the parent interpreter's ``sys.stdlib_module_names`` as a JSON
list on stdin, since 3.9 has no such attribute. The payload runs under a module
name other than ``__main__``, so its module-level code and definitions execute
while its entry point does not. Exit status 0 means it loaded.

A top-level import the parent does not know as standard library is stubbed, which
is how an executor host's driver packages are stood in for. A standard-library
module that 3.9 lacks, such as ``tomllib``, is known to the parent and so is left
to fail for real.

It is its own module rather than part of that one because it runs under the
payload's interpreter: it imports only the standard library, and it must stay
valid Python 3.9, which is why ``[tool.ruff.per-file-target-version]`` lints it
for 3.9 rather than for the project's target.
"""

import importlib.abc
import importlib.machinery
import io
import json
import runpy
import sys
import types
from contextlib import redirect_stderr, redirect_stdout

CHECK_RUN_NAME = "__host_payload_check__"


def _is_dunder(name: str) -> bool:
    """Return whether ``name`` is a special name a stub must not answer.

    :param name: The attribute name being looked up.
    :return: Whether the name is reserved for the interpreter's protocols.
    """
    return name.startswith("__") and name.endswith("__")


class _StubMeta(type):
    """Answer any attribute read on a stub class with another stub class."""

    def __getattr__(cls, name: str) -> type:
        """Return a stub for ``name``, leaving special names unanswered.

        :param name: The attribute read on the class.
        :return: A stub class named ``name``.
        :raises AttributeError: For a special name.
        """
        if _is_dunder(name):
            raise AttributeError(name)
        return _stub_class(name)


class _Stub(metaclass=_StubMeta):
    """Stand in for any object a stubbed package provides.

    A stub can be subclassed, called and read from, so a payload can declare
    classes on a driver and build objects from it at module level. Any other use
    at module level fails the load, which surfaces the dependency rather than
    hiding it; uses inside a function never run while loading.
    """

    def __getattr__(self, name: str) -> type:
        """Return a stub for ``name``, leaving special names unanswered.

        :param name: The attribute read on the instance.
        :return: A stub class named ``name``.
        :raises AttributeError: For a special name.
        """
        if _is_dunder(name):
            raise AttributeError(name)
        return _stub_class(name)

    def __call__(self, *args: object, **kwargs: object) -> "_Stub":
        """Return this stub, so calling a stubbed object yields another.

        :param args: Ignored positional arguments.
        :param kwargs: Ignored keyword arguments.
        :return: This stub.
        """
        del args, kwargs
        return self


def _stub_class(name: str) -> type:
    """Return a new stub class named ``name``.

    :param name: The name the payload read.
    :return: A subclassable, callable stub class.
    """
    return _StubMeta(name, (_Stub,), {})


class _StubModule(types.ModuleType):
    """Stand in for a module of a package the executor host would install."""

    def __getattr__(self, name: str) -> type:
        """Return a stub for ``name``, leaving special names unanswered.

        :param name: The attribute imported from or read on the module.
        :return: A stub class named ``name``.
        :raises AttributeError: For a special name.
        """
        if _is_dunder(name):
            raise AttributeError(name)
        return _stub_class(name)


class _StubLoader(importlib.abc.Loader):
    """Create stub modules, executing nothing."""

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        """Return an empty stub module for ``spec``.

        :param spec: The spec the stub finder issued.
        :return: The stub module.
        """
        return _StubModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        """Leave the stub module empty.

        :param module: The stub module being initialised.
        """


class _StubFinder(importlib.abc.MetaPathFinder):
    """Resolve every import outside the standard library to a stub module."""

    def __init__(self, stdlib: frozenset[str]) -> None:
        """Record which top-level names are standard library.

        :param stdlib: The parent interpreter's standard-library module names.
        """
        self._stdlib = stdlib

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> "importlib.machinery.ModuleSpec | None":
        """Return a stub spec for a non-standard-library module.

        :param fullname: The dotted module name being imported.
        :param path: Unused; the parent package's search path.
        :param target: Unused; the module being reloaded, if any.
        :return: A stub package spec, or ``None`` to leave the import to the
            interpreter's own finders.
        """
        del path, target
        if fullname.partition(".")[0] in self._stdlib:
            return None
        return importlib.machinery.ModuleSpec(fullname, _StubLoader(), is_package=True)


def main(argv: list[str]) -> int:
    """Load the payload named in ``argv`` and report whether it loaded.

    :param argv: This script's argv: the payload path, then the argv it needs.
    :return: ``0`` when the payload loaded, ``1`` when it called ``sys.exit``.
        Any other failure propagates, which exits non-zero with its traceback
        after the output the payload printed.
    """
    payload = argv[1]
    sys.meta_path.insert(0, _StubFinder(frozenset(json.load(sys.stdin))))
    sys.argv = [payload, *argv[2:]]
    output = io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            runpy.run_path(payload, run_name=CHECK_RUN_NAME)
    except SystemExit as exc:
        sys.stderr.write(
            f"{payload} called sys.exit({exc.code!r}) while loading\n"
            f"{output.getvalue()}"
        )
        return 1
    except BaseException:
        sys.stderr.write(output.getvalue())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
