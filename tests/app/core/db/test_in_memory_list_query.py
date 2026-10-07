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


"""Test Core's in-memory list-query applier."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import cast, column, String

from app.core.db import in_memory_list_query as in_memory_list_query_module
from app.core.db.in_memory_list_query import (
    InMemoryListQuery,
    InMemoryListQueryApplier,
)
from app.core.db.list_query import (
    build_search_predicate,
    ListQuerySpec,
    UnknownSortKeyError,
)
from app.core.pagination import Pagination
from tests.app.list_query_data import (
    list_query_rows,
    LIST_QUERY_SPEC,
    NO_SEARCH_LIST_QUERY_SPEC,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
_CHILD_TIMEOUT_SEC = 300

#: Every activatable app package. Core sits below all three, so an import of any of
#: them here would put the applier out of reach of the other two.
_APP_PACKAGE_PREFIXES = ("app.inventory", "app.extensions", "app.tasks")

#: The applier under test, bound to the shared spec once for the whole suite —
#: which is how a caller holds one.
APPLIER = InMemoryListQueryApplier(LIST_QUERY_SPEC)


class TestApplierConstruction:
    """Pin that a misdeclared spec is rejected up front, not once per request."""

    def test_binds_the_spec_and_its_resolved_attributes(self) -> None:
        """Bind the spec given and resolve its expressions once, at construction."""
        applier = InMemoryListQueryApplier(LIST_QUERY_SPEC)

        assert applier.spec is LIST_QUERY_SPEC
        assert applier._attrs.sort_attrs == {
            "filename": "filename",
            "title": "title",
            "created_at": "created_at",
        }
        assert applier._attrs.tie_attr == "filename"
        assert applier._attrs.search_attrs == ("filename", "title")

    def test_resolved_mapping_cannot_be_mutated(self) -> None:
        """Expose the resolved sort mapping immutably, so no caller can rewrite it.

        Appliers are built once and shared across requests, so a mutable mapping would
        let one request repoint a public sort key at another attribute for every later
        one.
        """
        applier = InMemoryListQueryApplier(LIST_QUERY_SPEC)

        with pytest.raises(TypeError):
            applier._attrs.sort_attrs["filename"] = "title"  # type: ignore[index]

    def test_attributes_are_never_resolved_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolve the spec at construction only, never again on a later call.

        Re-resolving per call would satisfy every behavioural assertion here while
        undoing the reason the spec is bound at construction, so this makes a second
        resolution fatal rather than merely unasserted.
        """
        applier = InMemoryListQueryApplier(LIST_QUERY_SPEC)

        def _boom(spec: ListQuerySpec) -> None:
            raise AssertionError(f"spec resolved again for {spec!r}")

        monkeypatch.setattr(in_memory_list_query_module, "_spec_attrs", _boom)

        query = applier.resolve_query("-filename", "a")
        page, total = applier.apply(
            list_query_rows(("a.sh", "Alpha", 1)), query, Pagination()
        )

        assert applier.default_query().sort_key == "created_at"
        assert [row.filename for row in page] == ["a.sh"]
        assert total == 1

    def test_unnamed_sortable_column_rejected(self) -> None:
        """Reject an unnamed sort expression."""
        spec = ListQuerySpec(
            sortable={"size": cast(column("size"), String)},
            default_sort="size",
            tie_breaker=column("filename"),
        )

        with pytest.raises(ValueError, match="exposes no name"):
            InMemoryListQueryApplier(spec)

    @pytest.mark.parametrize("role", ["tie_breaker", "searchable"])
    def test_unnamed_non_sortable_expression_also_rejected(self, role: str) -> None:
        """Guard every role, not just the sortable allowlist.

        :param role: The spec role carrying the unnamed expression.
        """
        unnamed = cast(column("filename"), String)
        spec = ListQuerySpec(
            sortable={"filename": column("filename")},
            default_sort="filename",
            tie_breaker=unnamed if role == "tie_breaker" else column("filename"),
            searchable=[unnamed] if role == "searchable" else [],
        )

        with pytest.raises(ValueError, match=f"spec {role}"):
            InMemoryListQueryApplier(spec)


class TestResolveQuery:
    """Cover the raw resolver, whose rejection the caller is expected to translate."""

    def test_resolves_sort_and_search(self) -> None:
        """Carry a vetted sort key and search term onto the resolved query."""
        query = APPLIER.resolve_query("-filename", "needle")

        assert query == InMemoryListQuery(
            sort_key="filename", descending=True, search="needle"
        )

    def test_unknown_sort_key_raises_untranslated(self) -> None:
        """Leave the rejection as ``UnknownSortKeyError`` for the caller to map."""
        with pytest.raises(UnknownSortKeyError):
            APPLIER.resolve_query("bogus", None)


class TestApplySort:
    """Verify ordering matches the SQL path: direction, NULLS-LAST, tie-breaker."""

    def test_descending_primary(self) -> None:
        """Order rows by the primary key descending."""
        rows = list_query_rows(("a", "A", 1), ("b", "B", 3), ("c", "C", 2))
        query = InMemoryListQuery(sort_key="created_at", descending=True, search=None)
        page, total = APPLIER.apply(rows, query, Pagination())
        assert [r.filename for r in page] == ["b", "c", "a"]
        assert total == len(rows)

    def test_ascending_primary(self) -> None:
        """Order rows by the primary key ascending."""
        rows = list_query_rows(("a", "A", 1), ("b", "B", 3), ("c", "C", 2))
        query = InMemoryListQuery(sort_key="created_at", descending=False, search=None)
        page, _ = APPLIER.apply(rows, query, Pagination())
        assert [r.filename for r in page] == ["a", "c", "b"]

    def test_nulls_sort_last_regardless_of_direction(self) -> None:
        """Trail NULL sort values last in both ascending and descending order."""
        rows = list_query_rows(("a", None, 1), ("b", "B", 2), ("c", None, 3))
        asc = InMemoryListQuery(sort_key="title", descending=False, search=None)
        desc = InMemoryListQuery(sort_key="title", descending=True, search=None)
        asc_page, _ = APPLIER.apply(rows, asc, Pagination())
        desc_page, _ = APPLIER.apply(rows, desc, Pagination())
        # Non-null "B" leads both directions; NULL rows trail, ordered by tie-breaker.
        assert [r.filename for r in asc_page] == ["b", "a", "c"]
        assert [r.filename for r in desc_page] == ["b", "a", "c"]

    def test_tie_breaker_makes_equal_primary_deterministic(self) -> None:
        """Break equal-primary ties on the tie-breaker ascending, both directions."""
        rows = list_query_rows(("c", "X", 5), ("a", "X", 5), ("b", "X", 5))
        asc = InMemoryListQuery(sort_key="created_at", descending=False, search=None)
        desc = InMemoryListQuery(sort_key="created_at", descending=True, search=None)
        asc_page, _ = APPLIER.apply(rows, asc, Pagination())
        desc_page, _ = APPLIER.apply(rows, desc, Pagination())
        assert [r.filename for r in asc_page] == ["a", "b", "c"]
        assert [r.filename for r in desc_page] == ["a", "b", "c"]


class TestApplySearch:
    """Verify case-insensitive substring search and the filtered total."""

    def test_case_insensitive_substring_over_searchable_attrs(self) -> None:
        """Match a term case-insensitively as a substring of a searchable attr."""
        rows = list_query_rows(("alpha.sh", "First", 1), ("beta.sh", "Second", 2))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="FIR")
        page, total = APPLIER.apply(rows, query, Pagination())
        assert [r.filename for r in page] == ["alpha.sh"]
        assert total == 1

    def test_search_matches_filename_column(self) -> None:
        """Match the term against the filename attribute."""
        rows = list_query_rows(("alpha.sh", None, 1), ("beta.sh", None, 2))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="beta")
        page, total = APPLIER.apply(rows, query, Pagination())
        assert [r.filename for r in page] == ["beta.sh"]
        assert total == 1

    def test_none_searchable_value_skipped(self) -> None:
        """Skip a row whose only match candidate is a ``None`` attribute."""
        rows = list_query_rows(("alpha.sh", None, 1))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="x")
        page, total = APPLIER.apply(rows, query, Pagination())
        assert page == []
        assert total == 0

    def test_whitespace_only_search_ignored(self) -> None:
        """Treat a whitespace-only term as no search."""
        rows = list_query_rows(("a", "A", 1), ("b", "B", 2))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="   ")
        _, total = APPLIER.apply(rows, query, Pagination())
        assert total == len(rows)

    def test_padded_term_is_not_stripped(self) -> None:
        """Match a padded term as submitted, the way the escaped SQL predicate does.

        ``build_search_predicate`` escapes the term without stripping it, so a term
        padded with spaces matches nothing on a SQL-backed source. Stripping here would
        make the same request answer differently depending on which source served it.
        """
        rows = list_query_rows(("alpha.sh", "Alpha", 1))
        query = InMemoryListQuery(
            sort_key="filename", descending=False, search="  alpha  "
        )

        page, total = APPLIER.apply(rows, query, Pagination())
        predicate = build_search_predicate("  alpha  ", LIST_QUERY_SPEC.searchable)

        assert page == []
        assert total == 0
        assert predicate is not None
        assert set(predicate.compile().params.values()) == {"%  alpha  %"}

    def test_non_searchable_spec_keeps_every_row(self) -> None:
        """Keep every row when the spec declares nothing searchable.

        ``build_search_predicate`` builds no predicate for an empty searchable set, so
        a SQL-backed source answers such a request with the unfiltered set. Matching
        nothing here instead would drop every row for the same query.
        """
        rows = list_query_rows(("a", "A", 1), ("b", "B", 2))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="a")

        page, total = InMemoryListQueryApplier(NO_SEARCH_LIST_QUERY_SPEC).apply(
            rows, query, Pagination()
        )

        assert [r.filename for r in page] == ["a", "b"]
        assert total == len(rows)
        assert build_search_predicate("a", NO_SEARCH_LIST_QUERY_SPEC.searchable) is None

    def test_none_search_returns_all(self) -> None:
        """Return every row when no search term is supplied."""
        rows = list_query_rows(("a", "A", 1), ("b", "B", 2))
        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)
        _, total = APPLIER.apply(rows, query, Pagination())
        assert total == len(rows)

    def test_non_string_searchable_value_matched_as_text(self) -> None:
        """Match a non-string searchable attribute through its text form.

        A spec is free to make a non-string attribute searchable, and the applier reads
        whatever the row exposes — so it compares the value's text form rather than
        skipping the attribute and silently narrowing the searchable set.
        """
        spec = ListQuerySpec(
            sortable={"filename": column("filename")},
            default_sort="filename",
            tie_breaker=column("filename"),
            searchable=(column("created_at"),),
        )
        rows = list_query_rows(("a", "A", 17), ("b", "B", 42))
        query = InMemoryListQuery(sort_key="filename", descending=False, search="17")

        page, total = InMemoryListQueryApplier(spec).apply(rows, query, Pagination())

        assert [row.filename for row in page] == ["a"]
        assert total == 1


class TestApplyPagination:
    """Verify slicing and that the total reflects the filtered set, not the page."""

    def test_total_is_filtered_count_before_slice(self) -> None:
        """Report the filtered total across all pages, not the sliced page size."""
        row_count, page_limit = 10, 3
        rows = list_query_rows(*[(f"{i:02d}.sh", "T", i) for i in range(row_count)])
        query = InMemoryListQuery(sort_key="filename", descending=False, search="T")
        page, total = APPLIER.apply(rows, query, Pagination(offset=0, limit=page_limit))
        assert len(page) == page_limit
        assert total == row_count

    def test_offset_and_limit_window(self) -> None:
        """Return exactly the offset/limit window of the ordered set."""
        rows = list_query_rows(*[(f"{i:02d}.sh", None, i) for i in range(5)])
        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)
        page, _ = APPLIER.apply(rows, query, Pagination(offset=2, limit=2))
        assert [r.filename for r in page] == ["02.sh", "03.sh"]

    def test_empty_items(self) -> None:
        """Return an empty page and zero total for an empty input."""
        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)
        page, total = APPLIER.apply([], query, Pagination())
        assert page == []
        assert total == 0


class TestDefaultQuery:
    """Cover the query a caller with no request-derived selections falls back to."""

    def test_descending_default_strips_the_prefix(self) -> None:
        """Split a ``-`` prefixed default into a bare key plus a descending flag."""
        query = APPLIER.default_query()

        assert (query.sort_key, query.descending, query.search) == (
            "created_at",
            True,
            None,
        )

    def test_ascending_default_is_not_descending(self) -> None:
        """Keep an unprefixed default ascending."""
        query = InMemoryListQueryApplier(NO_SEARCH_LIST_QUERY_SPEC).default_query()

        assert (query.sort_key, query.descending) == ("filename", False)


class TestApplyWithoutPagination:
    """Cover the whole-collection call shape the derived non-paginated route makes."""

    def test_returns_every_row_unsliced(self) -> None:
        """Return the full ordered set, past a default page, when pagination is None."""
        row_count = 60
        rows = list_query_rows(*[(f"{i:02d}.sh", "T", i) for i in range(row_count)])
        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)

        page, total = APPLIER.apply(rows, query, None)

        assert len(page) == row_count
        assert total == row_count
        assert page[0].filename == "00.sh"

    def test_still_filters_and_orders(self) -> None:
        """Apply search and ordering even with no page window."""
        rows = list_query_rows(
            ("b.sh", "Beta", 2), ("a.sh", "Alpha", 1), ("c.sh", "Gamma", 3)
        )
        query = InMemoryListQuery(sort_key="filename", descending=True, search="a")

        page, total = APPLIER.apply(rows, query, None)

        assert [row.filename for row in page] == ["c.sh", "b.sh", "a.sh"]
        assert total == len(rows)


class TestApplierRejectsUnvettedInput:
    """Pin the gate that keeps an unvetted sort key away from attribute access."""

    def test_row_missing_spec_attribute_names_both_sides(self) -> None:
        """Name the row type and the attribute instead of raising ``AttributeError``."""

        class _Sparse(BaseModel):
            filename: str

        query = InMemoryListQuery(sort_key="created_at", descending=False, search=None)

        with pytest.raises(ValueError, match="_Sparse has no attribute 'created_at'"):
            APPLIER.apply([_Sparse(filename="a.sh")], query, Pagination())

    def test_out_of_allowlist_sort_key_rejected(self) -> None:
        """Reject a hand-built query whose sort key was never vetted by the spec."""
        query = InMemoryListQuery(sort_key="secret", descending=False, search=None)

        with pytest.raises(UnknownSortKeyError):
            APPLIER.apply(list_query_rows(("a.sh", None, 1)), query, Pagination())

    @pytest.mark.parametrize("sort_key", ["__class__", "__dict__"])
    def test_dunder_sort_key_rejected(self, sort_key: str) -> None:
        """Reject a dunder sort key, which the allowlist gate keeps out of ``getattr``.

        The ordering reads the sort key off each row as an attribute, so an unvetted key
        would otherwise sort on object internals rather than on data.

        :param sort_key: The attribute-shaped sort key to reject.
        """
        query = InMemoryListQuery(sort_key=sort_key, descending=False, search=None)

        with pytest.raises(UnknownSortKeyError):
            APPLIER.apply(list_query_rows(("a.sh", None, 1)), query, Pagination())

    def test_row_missing_searchable_attribute_names_both_sides(self) -> None:
        """Reject a searchable-only mismatch on the first list call, not the first search.

        The sortable and tie-breaker attributes can line up while a searchable one does
        not, and the search attributes are read only for a non-blank term — so checking
        them lazily would defer a wiring error to whichever request first searched.
        """

        class _Untitled(BaseModel):
            filename: str
            created_at: int = 0

        query = InMemoryListQuery(sort_key="filename", descending=False, search=None)

        with pytest.raises(ValueError, match="_Untitled has no attribute 'title'"):
            APPLIER.apply([_Untitled(filename="a.sh")], query, Pagination())


class TestApplierIsBackingAgnostic:
    """Pin the applier's independence from any one caller's row type.

    A hand-written route materializes whatever rows it has and applies the result over
    them, so the applier must not assume a script type — nor drag an app package in as
    an import, which is what would put it back out of reach of the others.
    """

    def test_applies_over_pydantic_rows(self) -> None:
        """Sort, search, and total plain Pydantic rows, not just script dataclasses."""

        class _Node(BaseModel):
            filename: str
            title: str | None = None
            created_at: int = 0

        matching = 2
        rows = [
            _Node(filename="b-node", title="Beta"),
            _Node(filename="a-node", title="Alpha"),
            _Node(filename="c-node", title="Gamma alpha"),
        ]
        query = InMemoryListQuery(sort_key="filename", descending=False, search="alpha")

        page, total = APPLIER.apply(rows, query, Pagination())

        assert [row.filename for row in page] == ["a-node", "c-node"]
        assert total == matching

    def test_module_imports_no_app_package(self) -> None:
        """Keep the applier importable by every app package, not by one of them.

        Walks the module's own import statements rather than scanning its source text,
        so prose naming a consumer cannot fail the check and a ``TYPE_CHECKING``-only
        import cannot pass it.
        """
        tree = ast.parse(inspect.getsource(in_memory_list_query_module))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        assert "app.core.db.list_query" in imported
        assert not [name for name in imported if name.startswith(_APP_PACKAGE_PREFIXES)]


class TestApplierReachesEveryAppPackage:
    """Pin the reachability the applier's placement in Core exists to deliver.

    The AST guard above reads one module's own statements, so it sees neither what the
    applier drags in transitively nor whether ``app.core.db`` still imports cleanly with
    ``app.core.pagination`` loaded. Each probe gets a fresh interpreter so a module some
    other test already imported cannot satisfy it by accident.
    """

    @staticmethod
    def _probe(code: str) -> None:
        """Run ``code`` in a clean interpreter rooted at the repo.

        :param code: The probe body to execute.
        :raises AssertionError: When the probe exits non-zero, quoting both streams.
        :raises subprocess.TimeoutExpired: When the child outlives the timeout, which
            an import probe only does if something in the chain blocks on start-up.
        """
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SEC,
            check=False,
        )
        assert result.returncode == 0, (
            f"probe failed in a clean interpreter:\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    @pytest.mark.parametrize(
        "module",
        ["app.core.db.in_memory_list_query", "app.core.db.deps"],
    )
    def test_importing_the_applier_loads_no_app_package(self, module: str) -> None:
        """Reach the applier without loading an app, which is the point of the placement.

        Each app package imports nothing from the others, so a transitive edge into any
        one of them would make the applier unusable in the rest however clean the
        module's own import list looks.

        :param module: The dotted path imported in the clean interpreter.
        """
        self._probe(
            f"import importlib\nimportlib.import_module({module!r})\n"
            "import sys\n"
            f"prefixes = {_APP_PACKAGE_PREFIXES!r}\n"
            "leaked = sorted(m for m in sys.modules if m.startswith(prefixes))\n"
            "assert not leaked, leaked\n"
        )

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("app.core.pagination", "app.core.db"),
            ("app.core.db", "app.core.pagination"),
        ],
    )
    def test_core_db_and_pagination_import_in_either_order(
        self, first: str, second: str
    ) -> None:
        """Import the two packages both ways round, pinning the absence of a cycle.

        The applier references :class:`~app.core.pagination.Pagination`, so its landing
        in ``app.core.db`` is only safe while that direction stays acyclic.

        :param first: The package imported first.
        :param second: The package imported second.
        """
        self._probe(
            "import importlib\n"
            f"importlib.import_module({first!r})\n"
            f"importlib.import_module({second!r})\n"
            "from app.core.db.in_memory_list_query import InMemoryListQueryApplier\n"
            "assert InMemoryListQueryApplier.__name__ == 'InMemoryListQueryApplier'\n"
        )
