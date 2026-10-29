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

"""Declare the list-query dependencies a route injects at the request boundary.

:func:`make_query_param_dep` owns the parameters, the published allowlist ``enum`` and
the HTTP 422 for an out-of-allowlist sort key. :func:`make_list_query_dep` and
:func:`make_in_memory_list_query_dep` add the resolved value object: SQL expressions
for a manager-backed source, row attributes for one that materializes its whole set
rather than pushing the query down to a table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from fastapi import params, Query

from app.core.db.list_query import (
    build_search_predicate,
    ListQuery,
    ListQuerySpec,
    UnknownSortKeyError,
)
from app.core.exceptions import HTTPUnprocessableEntityException

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.db.crud import BaseManager
    from app.core.db.in_memory_list_query import (
        InMemoryListQuery,
        InMemoryListQueryApplier,
    )

__all__ = [
    "make_in_memory_list_query_dep",
    "make_list_query_dep",
    "make_query_param_dep",
    "search_query_param",
    "sort_query_param",
]

Q = TypeVar("Q")

SORT_PARAM_DESCRIPTION = "Sort key; prefix with '-' for descending order."
SEARCH_PARAM_DESCRIPTION = "Case-insensitive search across the searchable columns."


def make_in_memory_list_query_dep(
    applier: InMemoryListQueryApplier,
) -> Callable[..., InMemoryListQuery]:
    """Create a FastAPI dependency yielding a validated ``InMemoryListQuery``.

    The request boundary is Core's, through
    :func:`~app.core.db.deps.make_query_param_dep`, so this path and the SQL one
    expose the same parameters, publish the same allowlist ``enum`` and descriptions,
    and reject an out-of-allowlist sort key with the same HTTP 422. Only the resolved
    value object differs.

    Takes an applier rather than a spec because the caller already holds one: the
    misdeclaration check and the attribute resolution both happened when it was built,
    so neither is repeated here. Call this at wiring time and hand the result to
    ``Depends``; a fresh dependency is built per call rather than cached, because
    FastAPI binds each reflected parameter's ``Query`` declaration to the route it was
    found on.

    :param applier: The spec-bound applier whose allowlist bounds the request.
    :return: A dependency callable resolving the request into an ``InMemoryListQuery``.
    """

    # Core re-passes the spec it was given; the applier already binds it.
    def resolve(
        _spec: ListQuerySpec, sort: str, search: str | None
    ) -> InMemoryListQuery:
        return applier.resolve_query(sort, search)

    return make_query_param_dep(applier.spec, resolve)


def sort_query_param(spec: ListQuerySpec) -> params.Query:
    """Return the ``sort`` query-parameter declaration a spec's allowlist backs.

    The allowlist is emitted into the schema as an ``enum`` covering both directions of
    every public key, so a generated client carries the accepted values as a union
    instead of a bare string and a typed caller fails to compile rather than getting a
    runtime 422. The enum documents; :meth:`ListQuerySpec.resolve_sort` still validates,
    so the 422 boundary is unchanged.

    A fresh declaration is built per call because FastAPI binds one to each parameter it
    reflects.

    :param spec: The spec whose sortable allowlist and default seed the declaration.
    :return: The ``sort`` parameter declaration.
    """
    return Query(
        default=spec.default_sort,
        description=SORT_PARAM_DESCRIPTION,
        json_schema_extra={
            "enum": [
                value for key in sorted(spec.sortable) for value in (key, f"-{key}")
            ]
        },
    )


def search_query_param() -> params.Query:
    """Return the ``search`` query-parameter declaration.

    Carries no spec-derived detail — the searchable set is a server-side implementation
    choice, not a client input — so only the description is declared.

    :return: The ``search`` parameter declaration.
    """
    return Query(default=None, description=SEARCH_PARAM_DESCRIPTION)


def make_query_param_dep(
    spec: ListQuerySpec,
    build: Callable[[ListQuerySpec, str, str | None], Q],
) -> Callable[..., Q]:
    """Create a FastAPI dependency declaring a spec's list-query request boundary.

    Owns the boundary both list-query paths share: the enabled query parameters —
    ``sort`` always, ``search`` only when the spec's searchable set is non-empty — and
    the rejection of an out-of-allowlist sort key as an HTTP 422. The value object is
    the caller's, built by ``build``, so a SQL-backed source and an in-memory one
    present one contract while yielding different resolved types.

    The two variants are statically defined rather than a dynamically built signature:
    FastAPI reflects the parameters off the returned function, and synthesizing that
    signature has silently dropped them from the generated OpenAPI.

    :param spec: The spec whose allowlist and searchable set bound the request.
    :param build: Resolves ``(spec, sort, search)`` into the value object the
        dependency yields; may raise :class:`UnknownSortKeyError` to reject the sort.
    :return: A dependency callable resolving the request through ``build``.
    """

    def resolve(sort: str, search: str | None) -> Q:
        try:
            return build(spec, sort, search)
        except UnknownSortKeyError as exc:
            raise HTTPUnprocessableEntityException(
                detail=f"Invalid sort key: {exc.key!r}"
            ) from exc

    if spec.search_enabled:

        def _list_query_dep(
            sort: str = sort_query_param(spec),
            search: str | None = search_query_param(),
        ) -> Q:
            return resolve(sort, search)

        return _list_query_dep

    def _list_query_dep_no_search(
        sort: str = sort_query_param(spec),
    ) -> Q:
        return resolve(sort, None)

    return _list_query_dep_no_search


def make_list_query_dep(
    source: type[BaseManager] | ListQuerySpec,
) -> Callable[..., ListQuery]:
    """Create a FastAPI dependency yielding a validated :class:`ListQuery`.

    The request boundary is :func:`make_query_param_dep`'s; this adds only the
    SQL-expression value object. Callers wrap the result in a module-scope
    ``Annotated[ListQuery, Depends(...)]`` alias, mirroring
    :func:`app.core.pagination.deps.make_pagination_dep`.

    :param source: A :class:`ListQuerySpec` instance, or a manager class from which
        the ``list_query_spec`` class attribute is read (duck-typed so no runtime
        import of the manager type is needed).
    :return: A dependency callable resolving the request into a :class:`ListQuery`.
    :raises ValueError: When ``source`` is a manager class declaring no
        ``list_query_spec``.
    """
    spec = source if isinstance(source, ListQuerySpec) else source.list_query_spec
    if spec is None:
        raise ValueError(f"{source!r} declares no list_query_spec")
    return make_query_param_dep(spec, _build_list_query)


def _build_list_query(
    spec: ListQuerySpec,
    sort: str,
    search: str | None,
) -> ListQuery:
    """Resolve a request's sort and search into a :class:`ListQuery`.

    :param spec: The spec whose allowlist and searchable columns bound the request.
    :param sort: The requested public sort key (possibly ``-`` prefixed).
    :param search: The raw search term, or ``None`` when search is disabled or unset.
    :return: The resolved list query.
    :raises UnknownSortKeyError: When ``sort`` is not in the allowlist; the dependency
        maps it to a 422.
    """
    return ListQuery(
        order_by=tuple(spec.resolve_sort(sort)),
        search_predicate=build_search_predicate(search, spec.searchable),
    )
