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

"""Define the request-boundary list-query framework: spec, dependency, applier inputs.

The capability lives in ``app.core.db`` so all three services can consume it. Import
direction is one-way: this module never imports :mod:`app.core.db.crud` at runtime
(the manager type is referenced under ``TYPE_CHECKING`` and resolved by duck-typed
dispatch), so :mod:`app.core.db.crud` may import it directly without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from sqlalchemy import or_

from app.core.db.utils import NullsLastOrdering

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.sql import ColumnElement, ColumnExpressionArgument

_ILIKE_ESCAPE = "\\"


class UnknownSortKeyError(Exception):
    """Raise when a requested sort key is absent from a spec's allowlist.

    :func:`app.core.db.deps.make_query_param_dep` maps it to an HTTP 422, so the dependency owns the
    request-boundary error contract and every path that resolves a sort key against a
    spec reports the same rejection.

    :param key: The rejected public sort key (with any ``-`` prefix stripped).
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Unknown sort key: {key!r}")


@dataclass(frozen=True, slots=True)
class ListQuerySpec:
    """Describe one entity's sortable allowlist, searchable columns, and defaults.

    Members are SQLAlchemy column expressions rather than Pydantic-serializable
    values, so this is a frozen dataclass, not a model. The mapping and sequence are
    coerced to immutable containers in ``__post_init__`` so a shared class-attribute
    instance cannot be mutated in place.

    :param sortable: Public sort key mapped to a direction-free column expression.
        Keys must not begin with ``-`` (reserved as the descending-direction
        marker). Client input is resolved against this allowlist, so no raw column
        name reaches query construction.
    :param default_sort: Public sort key applied when the request omits ``sort``; a
        leading ``-`` selects descending order, and the key (minus any ``-``) must be
        present in ``sortable``.
    :param tie_breaker: A unique column expression appended to every ordering so
        pagination stays deterministic across pages.
    :param searchable: Column expressions the search term matches against. An empty
        sequence disables search (there is no separate flag).
    """

    sortable: Mapping[str, ColumnExpressionArgument]
    default_sort: str
    tie_breaker: ColumnExpressionArgument
    searchable: Sequence[ColumnExpressionArgument] = ()

    def __post_init__(self) -> None:
        """Validate the invariants and freeze the mutable members.

        :raises ValueError: When the spec is invalid — an empty or ``-``-prefixed
            public sort key, a non-column sortable value or searchable entry, a
            default sort whose key is outside the allowlist, or a missing or
            non-column tie-breaker.
        """
        for key, column in self.sortable.items():
            if not key.strip():
                raise ValueError("sortable keys must be non-empty")
            if key.startswith("-"):
                raise ValueError(
                    "sortable keys must not start with '-' (reserved for descending order)"
                )
            if not hasattr(column, "asc"):
                raise ValueError("sortable values must be column expressions")
        if self.default_sort.removeprefix("-") not in self.sortable:
            raise ValueError(
                f"default_sort {self.default_sort!r} is not in the sortable allowlist"
            )
        if self.tie_breaker is None:
            raise ValueError("tie_breaker is required")
        if not hasattr(self.tie_breaker, "asc"):
            raise ValueError("tie_breaker must be a column expression")
        if any(not hasattr(entry, "ilike") for entry in self.searchable):
            raise ValueError("searchable entries must be column expressions")
        object.__setattr__(self, "sortable", MappingProxyType(dict(self.sortable)))
        object.__setattr__(self, "searchable", tuple(self.searchable))

    @property
    def search_enabled(self) -> bool:
        """Return whether search is enabled (the searchable set is non-empty)."""
        return bool(self.searchable)

    def resolve_sort(self, raw_sort: str | None) -> list[ColumnElement]:
        """Resolve a raw sort value into a vetted, NULLs-last, tie-broken ordering.

        A leading ``-`` selects descending order; the remaining key is looked up in
        the allowlist. ``None`` resolves the default sort key.

        :param raw_sort: The client ``sort`` value, or ``None`` for the default.
        :return: An ordering list ``[dialect-aware NULLs-last sort expression,
            tie-breaker asc]``.
        :raises UnknownSortKeyError: When the key is absent from the allowlist.
        """
        key = self.default_sort if raw_sort is None else raw_sort
        descending = key.startswith("-")
        if descending:
            key = key[1:]
        try:
            column = self.sortable[key]
        except KeyError as exc:
            raise UnknownSortKeyError(key) from exc
        return [
            NullsLastOrdering(column, descending=descending),
            self.tie_breaker.asc(),
        ]


@dataclass(frozen=True, slots=True)
class ListQuery:
    """Carry a request's resolved, vetted list-query expressions.

    Holds only already-mapped SQL expressions, never raw client strings, so the
    applier can fold them straight into query construction.

    :param order_by: The resolved ordering (sort expression plus tie-breaker).
    :param search_predicate: The combined ILIKE search predicate, or ``None`` when
        no search term was supplied.
    """

    order_by: tuple[ColumnElement, ...]
    search_predicate: ColumnExpressionArgument[bool] | None


def build_search_predicate(
    term: str | None,
    searchable: Sequence[ColumnExpressionArgument],
) -> ColumnExpressionArgument[bool] | None:
    """Build an escaped case-insensitive ILIKE predicate across searchable columns.

    Wildcards (``%``, ``_``) and the escape character in ``term`` are escaped so the
    term matches literally rather than acting as LIKE wildcards.

    :param term: The raw search term; empty or whitespace-only yields no predicate.
    :param searchable: Column expressions to match the term against; an empty
        sequence yields no predicate.
    :return: An ``OR`` of per-column ILIKE predicates, or ``None`` when the term is
        empty or no searchable columns are supplied.
    """
    if not searchable or term is None or not term.strip():
        return None
    escaped = (
        term.replace(_ILIKE_ESCAPE, _ILIKE_ESCAPE * 2)
        .replace("%", f"{_ILIKE_ESCAPE}%")
        .replace("_", f"{_ILIKE_ESCAPE}_")
    )
    pattern = f"%{escaped}%"
    return or_(*(column.ilike(pattern, escape=_ILIKE_ESCAPE) for column in searchable))
