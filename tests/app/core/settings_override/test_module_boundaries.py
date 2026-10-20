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
import inspect
import pkgutil
import subprocess
import sys
from types import ModuleType

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


def _import_targets(statement: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the dotted module names an import statement reads from.

    :param statement: The import statement to inspect.
    :return: The module names; empty for a relative ``from . import``.
    """
    if isinstance(statement, ast.ImportFrom):
        return [statement.module] if statement.module else []
    return [alias.name for alias in statement.names]


def _package_imports(
    module_name: str,
) -> list[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Return every import statement in a module that targets the package.

    Walks the whole tree rather than the top level, so imports inside function
    bodies and ``TYPE_CHECKING`` blocks are reported too.

    :param module_name: The dotted name of the module to parse.
    :return: ``(statement, nested)`` pairs, where ``nested`` is ``True`` when
        the statement sits inside a function body.
    """
    tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))
    found: list[tuple[ast.Import | ast.ImportFrom, bool]] = []

    def visit(node: ast.AST, *, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom | ast.Import) and any(
                _is_package_module(target) for target in _import_targets(child)
            ):
                found.append((child, nested))
            visit(
                child,
                nested=nested
                or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef),
            )

    visit(tree, nested=False)
    return found


def _imported_names(module_name: str) -> list[tuple[str, str]]:
    """Return the ``(source module, name)`` pairs a module pulls from the package.

    :param module_name: The dotted name of the importing module.
    :return: One pair per name bound by a ``from ... import`` of a package module.
    """
    return [
        (statement.module, alias.name)
        for statement, _nested in _package_imports(module_name)
        if isinstance(statement, ast.ImportFrom) and statement.module
        for alias in statement.names
    ]


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
        assert not [
            name
            for source, name in _imported_names(resolution.__name__)
            if source == registry.__name__
        ]

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
    def test_no_deferred_import_between_package_modules(self, module_name: str) -> None:
        """Assert no function body imports from the package.

        An in-function import is how a cycle gets hidden rather than removed.
        Deferred imports of modules outside the package are not this test's
        concern, which is why it filters on the import target.

        :param module_name: The importing module.
        :return: ``None``.
        """
        deferred = [
            f"line {statement.lineno}"
            for statement, nested in _package_imports(module_name)
            if nested
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
        unexported = []
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
