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

"""Test the acyclic layering of the settings-override package's modules."""

import ast
import functools
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
from types import ModuleType
from typing import NamedTuple

import pytest

import app.core.settings_override as package
from app.core.settings_override import (
    cache,
    constants,
    registry,
    resolution,
    secret_preservation,
)

#: The definitions the classification registry pulls from the resolution module
#: below it. Anything else means the layering has started to rot.
REGISTRY_BACK_IMPORTS = frozenset({"resolve_nested_field", "resolve_nested_segments"})

#: Imports the resolution module first, then calls the nested-metadata resolver
#: through the registry, so a cycle reintroduced between the pair would surface
#: on the entry point production reaches first.
RESOLUTION_FIRST_PROBE = """
import app.core.settings_override.resolution
from pydantic import BaseModel

from app.core.settings_override.registry import (
    hot_field,
    nested_overridable_field,
    resolve_nested_field_metadata,
)


class Leaf(BaseModel):
    depth: int = hot_field(1)


class Root(BaseModel):
    leaf: Leaf = nested_overridable_field(Leaf())


metadata = resolve_nested_field_metadata(Root, "leaf__depth")
assert metadata is not None, "nested key did not resolve"
assert metadata.key == "leaf__depth", metadata.key
"""

NEW_MODULES = (resolution, secret_preservation)

#: Registry helpers consumed by sibling modules, each published under one name.
SHARED_HELPERS = (
    "clear_cached_properties",
    "field_contains_secret",
    "stable_collection_sort_key",
    "unwrap_secret_value",
)


def _package_module_names() -> list[str]:
    """Return every importable module in the settings-override package.

    :return: Dotted module names, the package itself included.
    """
    return [
        package.__name__,
        *sorted(
            info.name
            for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
        ),
    ]


def _is_package_module(name: str) -> bool:
    """Return whether a dotted module name lies inside the package.

    :param name: The dotted module name an import statement targets.
    :return: ``True`` for the package itself or any of its submodules.
    """
    return name == package.__name__ or name.startswith(f"{package.__name__}.")


class PackageImport(NamedTuple):
    """Describe one import statement that reads from the package."""

    source: str
    statement: ast.Import | ast.ImportFrom
    nested: bool


def _import_sources(statement: ast.Import | ast.ImportFrom, anchor: str) -> list[str]:
    """Return the absolute module names an import statement reads from.

    :param statement: The import statement to inspect.
    :param anchor: The importing module's package, against which a relative
        ``from`` resolves.
    :return: One absolute module name per source the statement names.
    """
    if isinstance(statement, ast.Import):
        return [alias.name for alias in statement.names]
    if not statement.level:
        return [statement.module] if statement.module else []
    relative = "." * statement.level + (statement.module or "")
    return [importlib.util.resolve_name(relative, anchor)]


def _scan_package_imports(source: str, *, anchor: str) -> list[PackageImport]:
    """Return every import in ``source`` that reads from the package.

    Walks the whole tree rather than the top level, so imports inside function
    bodies and ``TYPE_CHECKING`` blocks are reported too. Relative imports are
    resolved first, since their raw ``module`` never names the package.

    :param source: Python source text to scan.
    :param anchor: The importing module's package.
    :return: One entry per package source an import statement names.
    """
    found: list[PackageImport] = []

    def visit(node: ast.AST, *, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom | ast.Import):
                found.extend(
                    PackageImport(target, child, nested)
                    for target in _import_sources(child, anchor)
                    if _is_package_module(target)
                )
            visit(
                child,
                nested=nested
                or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef),
            )

    visit(ast.parse(source), nested=False)
    return found


def _package_imports(module_name: str) -> list[PackageImport]:
    """Return every import in a package module that reads from the package.

    :param module_name: The dotted name of the module to parse.
    :return: The imports :func:`_scan_package_imports` reports for its source.
    """
    module = importlib.import_module(module_name)
    return _scan_package_imports(
        inspect.getsource(module), anchor=module.__package__ or module_name
    )


def _imported_names(module_name: str) -> list[tuple[str, str]]:
    """Return the ``(source module, name)`` pairs a module pulls from the package.

    :param module_name: The dotted name of the importing module.
    :return: One pair per name bound by a ``from ... import`` of a package module.
    """
    return [
        (found.source, alias.name)
        for found in _package_imports(module_name)
        if isinstance(found.statement, ast.ImportFrom)
        for alias in found.statement.names
    ]


def _dependencies_of(imports: list[PackageImport]) -> set[str]:
    """Return the package modules a set of imports depends on.

    ``from package import registry`` reads its source as the package, so a name
    that is itself a submodule counts as a dependency on that submodule too.

    :param imports: The imports to attribute.
    :return: Every package module the imports name, as a source or a submodule.
    """
    submodules = set(_package_module_names())
    dependencies = {found.source for found in imports}
    dependencies.update(
        f"{found.source}.{alias.name}"
        for found in imports
        if isinstance(found.statement, ast.ImportFrom)
        for alias in found.statement.names
        if f"{found.source}.{alias.name}" in submodules
    )
    return dependencies


def _module_dependencies(module_name: str) -> set[str]:
    """Return the package modules a package module imports, in any form.

    :param module_name: The dotted name of the importing module.
    :return: The dependencies :func:`_dependencies_of` finds in its imports.
    """
    return _dependencies_of(_package_imports(module_name))


def _module_bindings(imports: list[PackageImport]) -> dict[str, str]:
    """Return the local names a set of imports binds to whole package modules.

    A plain ``import package.registry`` binds no short name, so its full dotted
    name is the key that attribute chains are matched against.

    :param imports: The imports to inspect.
    :return: A mapping of local (possibly dotted) name to absolute module name.
    """
    modules = {package.__name__, *_package_module_names()}
    bindings: dict[str, str] = {}
    for found in imports:
        for alias in found.statement.names:
            if isinstance(found.statement, ast.Import):
                target = alias.name
            else:
                target = f"{found.source}.{alias.name}"
            if target in modules:
                bindings[alias.asname or alias.name] = target
    return bindings


def _dotted_name(node: ast.expr) -> str | None:
    """Return the dotted name an expression spells, or ``None`` if it is not one.

    :param node: A ``Name`` or a chain of ``Attribute`` nodes.
    :return: The dotted name, such as ``registry`` or ``package.registry``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _scan_private_attribute_reads(source: str, *, anchor: str) -> list[str]:
    """Return every private attribute ``source`` reads off a package module.

    An import scan alone misses ``registry._helper`` after ``from . import
    registry``: the import names a public module, and the private access is
    an attribute read further down.

    :param source: Python source text to scan.
    :param anchor: The importing module's package.
    :return: One ``module.attribute`` entry per private read.
    """
    bindings = _module_bindings(_scan_package_imports(source, anchor=anchor))
    return [
        f"{bindings[base]}.{node.attr}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
        and (base := _dotted_name(node.value)) in bindings
    ]


def _private_attribute_reads(module_name: str) -> list[str]:
    """Return every private attribute a package module reads off a sibling.

    :param module_name: The dotted name of the module to parse.
    :return: The reads :func:`_scan_private_attribute_reads` reports.
    """
    module = importlib.import_module(module_name)
    return _scan_private_attribute_reads(
        inspect.getsource(module), anchor=module.__package__ or module_name
    )


@functools.cache
def _declared_names(module: ModuleType) -> frozenset[str]:
    """Return the names ``module`` binds at its own top level.

    Attribution cannot go through ``__module__``: only functions and classes
    carry it, so a borrowed constant would read as owned by whichever module
    imported it, and a module's own constant would read as borrowed. Parsing
    the source answers the question the tests actually ask, for every kind of
    definition.

    :param module: The module whose source to parse.
    :return: Every name bound by a top-level statement other than an import.
    """
    names: set[str] = set()
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return frozenset(names)


def _borrowed_from(module: ModuleType, sources: tuple[ModuleType, ...]) -> set[str]:
    """Return the names ``module`` imported from ``sources``.

    A name counts as borrowed when a source owns it — declares it, or exports
    it — and ``module`` is bound to that same object, so a relayed constant is
    caught alongside a relayed function. Names the module declares itself are
    excluded, and so are the third-party imports both modules happen to share.

    :param module: The importing module.
    :param sources: The modules whose definitions to look for.
    :return: Every name in ``module`` bound to a definition one of ``sources`` owns.
    """
    unbound = object()
    own = _declared_names(module)
    owned_by = {
        source: _declared_names(source) | set(getattr(source, "__all__", ()))
        for source in sources
    }
    return {
        name
        for name, value in vars(module).items()
        if name not in own
        and any(
            name in names and value is getattr(source, name, unbound)
            for source, names in owned_by.items()
        )
    }


class TestSymbolOwnership:
    """Cover which module each shared symbol belongs to."""

    @pytest.mark.parametrize(
        "module", NEW_MODULES, ids=["resolution", "secret-preservation"]
    )
    def test_module_defines_everything_it_exports(self, module: ModuleType) -> None:
        """Assert a module owns its exports instead of re-exporting them.

        :param module: One of the two modules carved out of the registry.
        :return: ``None``.
        """
        owned = _declared_names(module)
        borrowed = sorted(name for name in module.__all__ if name not in owned)
        assert not borrowed, borrowed

    def test_registry_borrows_only_the_resolution_walkers(self) -> None:
        """Assert the registry borrows only the key walkers from the new modules.

        Asserted dynamically rather than against a symbol list so a later
        rename stays free while a new cross-module import does not.
        """
        assert _borrowed_from(registry, NEW_MODULES) == REGISTRY_BACK_IMPORTS

    def test_resolution_borrows_nothing_from_registry(self) -> None:
        """Assert resolution sits strictly below the registry.

        The AST check covers what the runtime check cannot: an import confined
        to a ``TYPE_CHECKING`` block or a function body binds nothing at module
        scope, yet still points the dependency the wrong way.
        """
        assert _borrowed_from(resolution, (registry,)) == set()
        assert registry.__name__ not in _module_dependencies(resolution.__name__)

    def test_nested_metadata_resolver_lives_with_the_classification_registry(
        self,
    ) -> None:
        """Assert the resolver is owned by the module whose predicates it calls."""
        assert "resolve_nested_field_metadata" in _declared_names(registry)
        assert "resolve_nested_field_metadata" in registry.__all__
        assert "resolve_nested_field_metadata" not in vars(resolution)
        assert "resolve_nested_field_metadata" not in resolution.__all__

    def test_field_resolution_helper_is_public_everywhere(self) -> None:
        """Assert the cross-module helper carries its public name only.

        A half-applied rename would leave callers importing a private alias
        that still exists, so the old name has to be gone from every namespace
        that used to expose it.
        """
        assert callable(resolution.resolve_field_in_model)
        for module in (registry, resolution):
            assert "_resolve_field_in_model" not in vars(module)

    @pytest.mark.parametrize("public_name", SHARED_HELPERS)
    def test_shared_helper_is_public_only(self, public_name: str) -> None:
        """Assert a helper consumed across modules has no private alias left.

        No alias is kept on purpose: a caller still importing the old name must
        fail at import time rather than keep working against a name nobody
        maintains as part of the module's contract.

        :param public_name: The helper's public name on the registry.
        :return: ``None``.
        """
        assert callable(getattr(registry, public_name))
        assert public_name in registry.__all__
        for module in (registry, secret_preservation, cache):
            assert f"_{public_name}" not in vars(module), module.__name__


class TestInternalImports:
    """Cover how the package's modules import one another."""

    @pytest.mark.parametrize("module_name", _package_module_names())
    def test_no_private_name_crosses_a_module_boundary(self, module_name: str) -> None:
        """Assert a module imports only public names from its siblings.

        :param module_name: The importing module.
        :return: ``None``.
        """
        private = [
            f"{source}.{name}"
            for source, name in _imported_names(module_name)
            if name.startswith("_")
        ]
        assert not private, private

    @pytest.mark.parametrize("module_name", _package_module_names())
    def test_no_private_attribute_read_through_a_module(self, module_name: str) -> None:
        """Assert a module reads no private name off a sibling module object.

        :param module_name: The importing module.
        :return: ``None``.
        """
        private = _private_attribute_reads(module_name)
        assert not private, private

    @pytest.mark.parametrize("module_name", _package_module_names())
    def test_no_deferred_import_between_package_modules(self, module_name: str) -> None:
        """Assert no function body imports from the package.

        An in-function import is how a cycle gets hidden rather than removed.
        Deferred imports of modules outside the package are not this test's
        concern, which is why it filters on the import target.

        :param module_name: The importing module.
        :return: ``None``.
        """
        deferred = [
            f"line {found.statement.lineno}"
            for found in _package_imports(module_name)
            if found.nested
        ]
        assert not deferred, deferred

    @pytest.mark.parametrize("module_name", _package_module_names())
    def test_cross_module_helpers_are_exported(self, module_name: str) -> None:
        """Assert every name imported from a sibling is in the sibling's ``__all__``.

        Sources without an ``__all__`` are skipped, since they declare no
        export contract to check against.
        Submodules imported by name are skipped too: they are not exports.

        :param module_name: The importing module.
        :return: ``None``.
        """
        unexported: list[str] = []
        for source_name, name in _imported_names(module_name):
            source = importlib.import_module(source_name)
            exports = getattr(source, "__all__", None)
            if exports is None or isinstance(getattr(source, name, None), ModuleType):
                continue
            if name not in exports:
                unexported.append(f"{source_name}.{name}")
        assert not unexported, unexported


class TestExportLists:
    """Cover the ``__all__`` contracts of the registry and its split-out modules."""

    @pytest.mark.parametrize(
        "module",
        [package, registry, *NEW_MODULES],
        ids=["package", "registry", "resolution", "secret-preservation"],
    )
    def test_every_export_resolves(self, module: ModuleType) -> None:
        """Assert each exported name exists on its module.

        The package is checked alongside the modules because its
        ``__init__`` re-export list is what consumers outside the package
        import from, and a symbol that moves between modules can be dropped
        from it without any module-level import failing.

        :param module: The module whose ``__all__`` to check.
        :return: ``None``.
        """
        unresolved = sorted(
            name for name in module.__all__ if not hasattr(module, name)
        )
        assert not unresolved, unresolved

    def test_registry_exports_nothing_it_no_longer_owns(self) -> None:
        """Assert the registry stops advertising the moved public API."""
        moved = set(resolution.__all__) | set(secret_preservation.__all__)
        assert not set(registry.__all__) & moved


class TestImportCycles:
    """Cover importing each module first in a fresh interpreter."""

    @pytest.mark.parametrize("module_name", _package_module_names())
    def test_module_imports_standalone(self, module_name: str) -> None:
        """Assert the module imports with nothing else imported first.

        A cycle between two modules leaves whichever one the interpreter
        reaches second partially initialized, and which one that is depends on
        the entry point. Only a fresh interpreter per entry point catches that.
        The list is discovered rather than written down so a module added later
        cannot opt out.

        :param module_name: The module to import as the interpreter's first act.
        :return: ``None``.
        """
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr

    def test_nested_metadata_resolves_with_resolution_imported_first(self) -> None:
        """Assert the registry's resolver works when resolution is the entry point.

        Importing alone only proves module scope is sound; calling the resolver
        also exercises every name it reaches across the registry/resolution
        boundary.
        """
        result = subprocess.run(
            [sys.executable, "-c", RESOLUTION_FIRST_PROBE],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr


class TestSharedSentinel:
    """Cover the identity of the missing-value sentinel after the split."""

    def test_every_module_sees_one_sentinel_object(self) -> None:
        """Assert the sentinel stays a single object across its re-exports.

        Callers compare it with ``is``, so a duplicated ``object()`` would make
        a missing nested segment read as a present value. The package re-export
        is included because that is the path most consumers take.
        """
        assert (
            constants.NESTED_VALUE_MISSING
            is resolution.NESTED_VALUE_MISSING
            is package.NESTED_VALUE_MISSING
        )

    def test_the_registry_no_longer_relays_the_sentinel(self) -> None:
        """Assert readers reach the sentinel through the module that owns it.

        The sentinel was moved to a leaf module precisely so no reader has to
        import it from a module that may still be initializing; a relay left on
        the registry would hand that hazard back to every consumer that keeps
        using it.
        """
        assert "NESTED_VALUE_MISSING" not in vars(registry)
        assert "NESTED_VALUE_MISSING" not in registry.__all__


class TestImportScanner:
    """Cover the AST scan the layering tests rely on.

    A form the scan cannot see would let the boundary it guards rot silently,
    so each one is pinned against an inline source rather than a real module.
    """

    def _scan(self, source: str) -> list[PackageImport]:
        """Return the package imports ``source`` makes from a top-level package module.

        :param source: Python source text to scan.
        :return: The package imports the scan reports.
        """
        return _scan_package_imports(source, anchor=package.__name__)

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("from .registry import x", f"{package.__name__}.registry"),
            ("from . import registry", package.__name__),
            ("from .api.routes import x", f"{package.__name__}.api.routes"),
            ("from .. import config", None),
        ],
        ids=["sibling", "package", "subpackage", "outside-package"],
    )
    def test_relative_import_resolves_against_the_importer_package(
        self, source: str, expected: str | None
    ) -> None:
        """Report a relative import under its absolute source module.

        :param source: A one-line relative import.
        :param expected: The absolute source, or ``None`` when it leaves the
            package.
        :return: ``None``.
        """
        assert [found.source for found in self._scan(source)] == (
            [expected] if expected else []
        )

    def test_relative_private_import_is_reported_by_name(self) -> None:
        """Attribute a relative import's names to the resolved sibling."""
        found = self._scan("from .registry import _resolve_default")

        assert [
            (item.source, alias.name)
            for item in found
            for alias in item.statement.names
        ] == [(f"{package.__name__}.registry", "_resolve_default")]

    def test_star_import_is_reported_as_its_wildcard(self) -> None:
        """Report ``*`` as the imported name, which no ``__all__`` lists."""
        found = self._scan("from .registry import *")

        assert [alias.name for item in found for alias in item.statement.names] == ["*"]

    @pytest.mark.parametrize(
        ("source", "nested"),
        [
            ("def f():\n    from .registry import x\n", True),
            ("async def f():\n    from .registry import x\n", True),
            ("class C:\n    def f(self):\n        from .registry import x\n", True),
            ("class C:\n    from .registry import x\n", False),
            (
                "try:\n    from .registry import x\nexcept ImportError:\n    pass\n",
                False,
            ),
        ],
        ids=["function", "async-function", "method", "class-body", "try-except"],
    )
    def test_only_a_function_body_marks_an_import_nested(
        self, source: str, *, nested: bool
    ) -> None:
        """Flag an import as deferred exactly when it runs inside a function.

        :param source: Source text holding one package import.
        :param nested: Whether that import runs only when a function is called.
        :return: ``None``.
        """
        assert [item.nested for item in self._scan(source)] == [nested]

    @pytest.mark.parametrize(
        "source",
        [
            "from . import registry\nregistry._x\n",
            "from . import registry as reg\ndef f():\n    return reg._x\n",
            f"from {package.__name__} import registry\nregistry._x()\n",
            f"import {package.__name__}.registry as reg\nreg._x\n",
            f"import {package.__name__}.registry\n{package.__name__}.registry._x\n",
        ],
        ids=[
            "relative-package",
            "aliased-in-function",
            "package-attribute",
            "module-alias",
            "dotted-module",
        ],
    )
    def test_private_attribute_read_off_a_module_is_reported(self, source: str) -> None:
        """Report a private name reached through a module object.

        :param source: Source text binding the registry and reading ``_x`` off it.
        :return: ``None``.
        """
        assert _scan_private_attribute_reads(source, anchor=package.__name__) == [
            f"{package.__name__}.registry._x"
        ]

    @pytest.mark.parametrize(
        "source",
        [
            "from . import registry\nregistry.FieldMetadata\n",
            "from . import registry\nregistry.__name__\n",
            "from .registry import FieldMetadata\nFieldMetadata._x\n",
            "registry = object()\nregistry._x\n",
        ],
        ids=["public-name", "dunder", "symbol-attribute", "unbound-name"],
    )
    def test_non_module_private_reads_are_not_reported(self, source: str) -> None:
        """Ignore reads that are public, dunder, or not off a package module.

        :param source: Source text whose attribute read crosses no boundary.
        :return: ``None``.
        """
        assert _scan_private_attribute_reads(source, anchor=package.__name__) == []

    @pytest.mark.parametrize(
        "source",
        [
            f"import {package.__name__}.registry as registry",
            f"from {package.__name__} import registry",
            "from . import registry",
            "from .registry import FieldMetadata",
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                f"    import {package.__name__}.registry\n"
            ),
            (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    pass\n"
                "else:\n"
                "    from .registry import FieldMetadata\n"
            ),
            "try:\n    from .registry import x\nexcept ImportError:\n    x = None\n",
            "from .registry import *",
        ],
        ids=[
            "module-alias",
            "package-attribute",
            "relative-package",
            "symbol",
            "type-checking",
            "type-checking-else",
            "try-except",
            "star",
        ],
    )
    def test_every_import_form_counts_as_a_module_dependency(self, source: str) -> None:
        """Record a dependency on the registry whichever import form names it.

        :param source: Source text importing the registry in one form.
        :return: ``None``.
        """
        assert f"{package.__name__}.registry" in _dependencies_of(self._scan(source))
