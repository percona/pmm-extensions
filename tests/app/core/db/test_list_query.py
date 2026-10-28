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

"""Define tests for the core list-query framework (spec, dependency, predicate)."""

import pytest
from sqlalchemy.dialects import postgresql
from sqlmodel import col, select

from app.core.db.list_query import build_search_predicate, UnknownSortKeyError
from tests.app.list_query_data import LQItem, make_lq_spec

RESOLVED_ORDER_BY_LENGTH = 2


class TestListQuerySpecValidation:
    """Cover ``ListQuerySpec`` construction-time validation and immutability."""

    def test_valid_spec_constructs(self) -> None:
        """Accept a spec whose default sort is in the sortable allowlist."""
        spec = make_lq_spec()
        assert spec.default_sort == "name"

    def test_default_sort_not_in_allowlist_raises(self) -> None:
        """Reject a default sort key absent from the sortable allowlist."""
        with pytest.raises(ValueError, match="default_sort"):
            make_lq_spec(default_sort="missing")

    def test_empty_sortable_key_raises(self) -> None:
        """Reject an empty public sort key."""
        with pytest.raises(ValueError, match="non-empty"):
            make_lq_spec(sortable={"": col(LQItem.name)}, default_sort="")

    def test_dash_prefixed_sortable_key_raises(self) -> None:
        """Reject a sortable key beginning with ``-`` (reserved for descending)."""
        with pytest.raises(ValueError, match="must not start with '-'"):
            make_lq_spec(
                sortable={"name": col(LQItem.name), "-bad": col(LQItem.category)}
            )

    def test_missing_tie_breaker_raises(self) -> None:
        """Reject a spec without a tie-breaker column."""
        with pytest.raises(ValueError, match="tie_breaker"):
            make_lq_spec(tie_breaker=None)

    def test_non_column_tie_breaker_raises(self) -> None:
        """Reject a tie-breaker that is not a column expression."""
        with pytest.raises(ValueError, match="tie_breaker must be a column"):
            make_lq_spec(tie_breaker="id")

    def test_non_column_searchable_entry_raises(self) -> None:
        """Reject a searchable entry that is not a column expression."""
        with pytest.raises(ValueError, match="searchable"):
            make_lq_spec(searchable=["name"])

    def test_non_column_sortable_value_raises(self) -> None:
        """Reject a sortable value that is not a column expression."""
        with pytest.raises(ValueError, match="sortable values"):
            make_lq_spec(sortable={"name": "name"})

    def test_sortable_coerced_to_immutable_mapping(self) -> None:
        """Coerce a mutable ``sortable`` mapping into a read-only mapping."""
        spec = make_lq_spec(sortable={"name": col(LQItem.name)})
        with pytest.raises(TypeError):
            spec.sortable["name"] = col(LQItem.category)  # type: ignore[index]

    def test_searchable_coerced_to_tuple(self) -> None:
        """Coerce a mutable ``searchable`` sequence into an immutable tuple."""
        spec = make_lq_spec(searchable=[col(LQItem.name)])
        assert isinstance(spec.searchable, tuple)

    def test_search_enabled_reflects_searchable(self) -> None:
        """Derive ``search_enabled`` from whether ``searchable`` is non-empty."""
        assert make_lq_spec(searchable=[col(LQItem.name)]).search_enabled is True
        assert make_lq_spec().search_enabled is False


class TestResolveSort:
    """Cover ``ListQuerySpec.resolve_sort`` direction and tie-breaker handling."""

    def test_default_sort_resolves_with_tie_breaker(self) -> None:
        """Resolve the default key into an ordering plus the tie-breaker."""
        order_by = make_lq_spec().resolve_sort(None)
        assert len(order_by) == RESOLVED_ORDER_BY_LENGTH
        assert "ASC" in str(order_by[0])
        assert "NULLS LAST" in str(order_by[0])

    def test_descending_prefix_resolves_descending(self) -> None:
        """Resolve a ``-`` prefixed key into a descending ordering."""
        order_by = make_lq_spec().resolve_sort("-name")
        assert "DESC" in str(order_by[0])
        assert "NULLS LAST" in str(order_by[0])

    def test_descending_default_sort_resolves_descending(self) -> None:
        """Accept a ``-``-prefixed default sort and resolve it descending."""
        order_by = make_lq_spec(default_sort="-name").resolve_sort(None)
        assert "DESC" in str(order_by[0])
        assert "NULLS LAST" in str(order_by[0])

    def test_unknown_key_raises_unknown_sort_key(self) -> None:
        """Raise ``UnknownSortKeyError`` for a key absent from the allowlist."""
        with pytest.raises(UnknownSortKeyError):
            make_lq_spec().resolve_sort("evil")

    def test_unknown_descending_key_raises(self) -> None:
        """Raise ``UnknownSortKeyError`` after stripping ``-`` from an unknown key."""
        with pytest.raises(UnknownSortKeyError):
            make_lq_spec().resolve_sort("-evil")

    @pytest.mark.parametrize("raw_sort", [None, "-name"], ids=["asc", "desc"])
    def test_resolved_ordering_renders_nulls_last_with_tie_breaker_last(
        self, raw_sort
    ) -> None:
        """Render ``NULLS LAST`` on the sort column and keep the tie-breaker final."""
        order_by = make_lq_spec().resolve_sort(raw_sort)

        rendered = str(
            select(col(LQItem.id))
            .order_by(*order_by)
            .compile(dialect=postgresql.dialect())
        )

        assert "NULLS LAST" in rendered
        assert rendered.rstrip().endswith("id ASC")


class TestBuildSearchPredicate:
    """Cover ``build_search_predicate`` empty-term handling."""

    def test_none_term_returns_none(self) -> None:
        """Return ``None`` for a missing search term."""
        assert build_search_predicate(None, [col(LQItem.name)]) is None

    def test_empty_term_returns_none(self) -> None:
        """Return ``None`` for an empty search term."""
        assert build_search_predicate("", [col(LQItem.name)]) is None

    def test_whitespace_term_returns_none(self) -> None:
        """Return ``None`` for a whitespace-only search term."""
        assert build_search_predicate("   ", [col(LQItem.name)]) is None

    def test_non_empty_term_returns_predicate(self) -> None:
        """Return a predicate for a non-empty search term."""
        assert build_search_predicate("foo", [col(LQItem.name)]) is not None

    def test_no_searchable_columns_returns_none(self) -> None:
        """Return ``None`` when no searchable columns are supplied."""
        assert build_search_predicate("foo", []) is None
