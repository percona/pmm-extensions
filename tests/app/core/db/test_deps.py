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


"""Test the list-query dependency factories Core exposes to routes."""

from __future__ import annotations

import inspect
from typing import Annotated, Any, TYPE_CHECKING

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, params, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import cast, column, String
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import col, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: TC002
from sqlmodel.pool import StaticPool

from app.core import db as core_db_package
from app.core.db.crud import BaseSQLModelManager
from app.core.db.deps import (
    make_in_memory_list_query_dep,
    make_list_query_dep,
    make_query_param_dep,
    SEARCH_PARAM_DESCRIPTION,
    SORT_PARAM_DESCRIPTION,
)
from app.core.db.in_memory_list_query import (
    InMemoryListQuery,
    InMemoryListQueryApplier,
)
from app.core.db.list_query import ListQuery, ListQuerySpec, UnknownSortKeyError
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.exceptions import HTTPUnprocessableEntityException
from app.core.pagination import PaginatedResponse, Pagination
from app.core.pagination.deps import pagination_dep
from app.core.utils import json_serializer
from tests.app.db_schema import apply_schema
from tests.app.list_query_data import (
    list_query_rows,
    LIST_QUERY_SPEC,
    LQ_ITEM_NO_SEARCH_SPEC,
    LQItem,
    LQItemManager,
    make_lq_spec,
    NO_SEARCH_LIST_QUERY_SPEC,
    SEED_NAMES,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

APPLIER = InMemoryListQueryApplier(LIST_QUERY_SPEC)
NO_SEARCH_APPLIER = InMemoryListQueryApplier(NO_SEARCH_LIST_QUERY_SPEC)


@pytest.fixture(name="dep")
def dep_fixture() -> Callable[..., InMemoryListQuery]:
    """Return the dependency the searchable spec wires.

    :return: A dependency callable built from the searchable spec's applier.
    """
    return make_in_memory_list_query_dep(APPLIER)


class TestMakeInMemoryListQueryDep:
    """Exercise the FastAPI dependency the paginated list route injects."""

    def test_exposed_from_the_package(self) -> None:
        """Re-export the factory beside its SQL sibling on the package surface.

        Both consumers reach ``make_list_query_dep`` through ``app.core.db``, so the
        in-memory factory answering only from the submodule would leave one kind of
        construct with two import homes.
        """
        assert (
            core_db_package.make_in_memory_list_query_dep
            is make_in_memory_list_query_dep
        )

    def test_exposes_sort_and_search_params_when_searchable(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Expose ``sort`` and ``search`` when the spec has searchable columns.

        :param dep: The dependency built from the searchable spec.
        """
        assert set(inspect.signature(dep).parameters) == {"sort", "search"}

    def test_exposes_only_sort_when_no_searchable(self) -> None:
        """Expose only ``sort`` when the spec has no searchable columns."""
        dep = make_in_memory_list_query_dep(NO_SEARCH_APPLIER)
        params = inspect.signature(dep).parameters
        assert set(params) == {"sort"}

    def test_default_sort_resolves_to_spec_default(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Resolve the spec's default sort, honoring its descending prefix.

        :param dep: The dependency built from the searchable spec.
        """
        query = dep(sort=LIST_QUERY_SPEC.default_sort, search=None)
        assert query == InMemoryListQuery(
            sort_key="created_at", descending=True, search=None
        )

    def test_ascending_sort_key_parsed(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Parse a bare (unprefixed) sort key as ascending.

        :param dep: The dependency built from the searchable spec.
        """
        assert dep(sort="filename", search=None).descending is False

    def test_search_term_passed_through(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Carry the raw search term onto the resolved query.

        :param dep: The dependency built from the searchable spec.
        """
        assert dep(sort="filename", search="needle").search == "needle"

    def test_unknown_sort_key_raises_422(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422.

        :param dep: The dependency built from the searchable spec.
        """
        with pytest.raises(HTTPUnprocessableEntityException):
            dep(sort="bogus", search=None)

    def test_unknown_sort_key_with_descending_prefix_raises_422(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Reject an out-of-allowlist descending sort key with HTTP 422.

        :param dep: The dependency built from the searchable spec.
        """
        with pytest.raises(HTTPUnprocessableEntityException):
            dep(sort="-bogus", search=None)

    def test_misdeclared_spec_rejected_before_a_dependency_exists(self) -> None:
        """Reject a misdeclaration where the applier is built, never at a request.

        The factory takes an applier, so a spec that cannot be read off a row fails one
        step earlier than the wiring — there is no dependency to reach a request with.
        """
        spec = ListQuerySpec(
            sortable={"size": cast(column("size"), String)},
            default_sort="size",
            tie_breaker=column("filename"),
        )

        with pytest.raises(ValueError, match="exposes no name"):
            make_in_memory_list_query_dep(InMemoryListQueryApplier(spec))

    def test_each_call_declares_its_own_query_parameters(self) -> None:
        """Hand every route its own parameter declarations, never a shared one.

        FastAPI binds a ``Query`` declaration to each parameter it reflects, so two
        routes built from the same applier must not share one object — the dependency
        is rebuilt per call rather than memoized on the instance.
        """
        first = make_in_memory_list_query_dep(APPLIER)
        second = make_in_memory_list_query_dep(APPLIER)

        assert first is not second
        first_sort = inspect.signature(first).parameters["sort"].default
        second_sort = inspect.signature(second).parameters["sort"].default
        assert first_sort is not second_sort
        assert first_sort.json_schema_extra == second_sort.json_schema_extra

    def test_params_carry_the_allowlist_enum_and_descriptions(
        self, dep: Callable[..., InMemoryListQuery]
    ) -> None:
        """Declare the params through Core, so both paths document one contract.

        :param dep: The dependency built from the searchable spec.
        """
        declarations = {
            name: param.default
            for name, param in inspect.signature(dep).parameters.items()
        }

        assert declarations["sort"].json_schema_extra == {
            "enum": [
                "created_at",
                "-created_at",
                "filename",
                "-filename",
                "title",
                "-title",
            ]
        }
        assert declarations["sort"].description == SORT_PARAM_DESCRIPTION
        assert declarations["search"].description == SEARCH_PARAM_DESCRIPTION


_ROUTE_APP = FastAPI()
_ROUTE_ROWS = list_query_rows(
    ("a.sh", "Alpha", 1), ("b.sh", "Beta", 2), ("c.sh", "Gamma", 3)
)

_search_dep = make_in_memory_list_query_dep(APPLIER)
_no_search_dep = make_in_memory_list_query_dep(NO_SEARCH_APPLIER)


@_ROUTE_APP.get("/scripts")
async def _list_scripts(
    list_query: Annotated[InMemoryListQuery, Depends(_search_dep)],
    pagination: Annotated[Pagination, Depends(pagination_dep)],
) -> dict[str, Any]:
    page, total = APPLIER.apply(_ROUTE_ROWS, list_query, pagination)
    return {"items": [row.filename for row in page], "total": total}


@_ROUTE_APP.get("/scripts-nosearch")
async def _list_scripts_no_search(
    list_query: Annotated[InMemoryListQuery, Depends(_no_search_dep)],
    pagination: Annotated[Pagination, Depends(pagination_dep)],
) -> dict[str, Any]:
    page, total = NO_SEARCH_APPLIER.apply(_ROUTE_ROWS, list_query, pagination)
    return {"items": [row.filename for row in page], "total": total}


@pytest_asyncio.fixture(name="route_client")
async def route_client_fixture() -> AsyncIterator[AsyncClient]:
    """Yield an async client bound to the throwaway in-memory list-query app.

    :return: A client whose lifetime is scoped to the requesting test.
    """
    client = AsyncClient(
        transport=ASGITransport(app=_ROUTE_APP), base_url="http://test"
    )
    try:
        yield client
    finally:
        await client.aclose()


def _route_params(path: str) -> dict[str, dict[str, Any]]:
    """Return the generated OpenAPI query parameters of a route, keyed by name.

    :param path: The route path to read the generated parameters of.
    :return: Each declared query parameter's schema entry, keyed by parameter name.
    """
    return {
        param["name"]: param
        for param in _ROUTE_APP.openapi()["paths"][path]["get"]["parameters"]
    }


class TestInMemoryListQueryDepAtTheRequestBoundary:
    """Pin the in-memory dependency's boundary against the SQL dependency's.

    The direct-call tests above cover resolution; these cover what a client sees —
    the reflected OpenAPI parameters and the rejection body — which is the half a
    request contract can drift on without any unit test noticing.
    """

    def test_search_enabled_route_exposes_both_params(self) -> None:
        """Reflect ``sort`` and ``search`` into the generated OpenAPI."""
        assert {"sort", "search"} <= set(_route_params("/scripts"))

    def test_search_disabled_route_omits_search(self) -> None:
        """Reflect only ``sort`` when the spec declares nothing searchable."""
        names = set(_route_params("/scripts-nosearch"))
        assert "sort" in names
        assert "search" not in names

    def test_params_document_the_allowlist_and_descriptions(self) -> None:
        """Publish the allowlist enum and both descriptions a generated client reads."""
        params = _route_params("/scripts")

        assert params["sort"]["schema"]["enum"] == [
            "created_at",
            "-created_at",
            "filename",
            "-filename",
            "title",
            "-title",
        ]
        assert params["sort"]["description"] == SORT_PARAM_DESCRIPTION
        assert params["search"]["description"] == SEARCH_PARAM_DESCRIPTION

    @pytest.mark.asyncio
    async def test_out_of_allowlist_sort_returns_a_flat_422(
        self, route_client: AsyncClient
    ) -> None:
        """Reject an unvetted sort key with the SQL path's exact 422 body."""
        response = await route_client.get("/scripts", params={"sort": "evil"})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json() == {"detail": "Invalid sort key: 'evil'"}

    @pytest.mark.asyncio
    async def test_dunder_sort_key_rejected_at_the_boundary(
        self, route_client: AsyncClient
    ) -> None:
        """Stop an attribute-shaped sort key at the allowlist, before any row read."""
        response = await route_client.get("/scripts", params={"sort": "__class__"})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json() == {"detail": "Invalid sort key: '__class__'"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("term", ["%", "_", "\\", "a%a"])
    async def test_search_metacharacters_match_literally(
        self, route_client: AsyncClient, term: str
    ) -> None:
        """Treat LIKE metacharacters as text, matching the escaped SQL predicate.

        :param term: The metacharacter-bearing term no row contains literally.
        """
        response = await route_client.get("/scripts", params={"search": term})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_padded_search_term_matches_nothing(
        self, route_client: AsyncClient
    ) -> None:
        """Forward a padded term unstripped, so the reply matches a SQL-backed route."""
        response = await route_client.get("/scripts", params={"search": "  beta  "})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_default_sort_applied_when_omitted(
        self, route_client: AsyncClient
    ) -> None:
        """Apply the spec's descending default when the request omits ``sort``."""
        response = await route_client.get("/scripts")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"] == ["c.sh", "b.sh", "a.sh"]

    @pytest.mark.asyncio
    async def test_search_filters_and_reports_filtered_total(
        self, route_client: AsyncClient
    ) -> None:
        """Filter by the search term and report the filtered total."""
        response = await route_client.get(
            "/scripts", params={"sort": "filename", "search": "beta"}
        )

        assert response.json() == {"items": ["b.sh"], "total": 1}

    @pytest.mark.asyncio
    async def test_search_param_ignored_where_the_spec_disables_it(
        self, route_client: AsyncClient
    ) -> None:
        """Ignore a ``search`` the route never declared instead of filtering on it."""
        response = await route_client.get(
            "/scripts-nosearch", params={"search": "beta"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == len(_ROUTE_ROWS)


class TestMakeListQueryDep:
    """Cover the dependency factory dispatch and construction guards."""

    def test_bare_spec_source_builds_dep(self) -> None:
        """Accept a bare ``ListQuerySpec`` as the dependency source."""
        assert callable(make_list_query_dep(LQ_ITEM_NO_SEARCH_SPEC))

    def test_manager_source_builds_dep(self) -> None:
        """Accept a manager class and read its ``list_query_spec``."""
        assert callable(make_list_query_dep(LQItemManager))

    def test_spec_less_manager_raises(self) -> None:
        """Reject a manager class that declares no ``list_query_spec``."""

        class SpecLessManager(BaseSQLModelManager):
            Model = LQItem

        with pytest.raises(ValueError, match="list_query_spec"):
            make_list_query_dep(SpecLessManager)


def _record_build(
    calls: list[tuple[ListQuerySpec, str, str | None]],
) -> Callable[[ListQuerySpec, str, str | None], None]:
    """Return a builder recording the arguments the dependency shape hands it.

    :param calls: The list each invocation appends its ``(spec, sort, search)`` to.
    :return: A builder usable as the dependency's value-object factory.
    """

    def build(spec: ListQuerySpec, sort: str, search: str | None) -> None:
        calls.append((spec, sort, search))

    return build


class TestMakeQueryParamDep:
    """Cover the dependency shape both list-query factories are built from."""

    def test_builder_receives_the_spec_and_both_request_values(self) -> None:
        """Hand the bound spec and the request's sort and search to the builder."""
        spec = make_lq_spec(searchable=[col(LQItem.name)])
        calls: list[tuple[ListQuerySpec, str, str | None]] = []

        make_query_param_dep(spec, _record_build(calls))(sort="name", search="needle")

        assert calls == [(spec, "name", "needle")]

    def test_search_disabled_builder_receives_no_term(self) -> None:
        """Pass ``None`` as the term when the spec declares nothing searchable."""
        spec = make_lq_spec()
        calls: list[tuple[ListQuerySpec, str, str | None]] = []

        make_query_param_dep(spec, _record_build(calls))(sort="name")

        assert calls == [(spec, "name", None)]

    def test_unknown_sort_key_maps_to_422(self) -> None:
        """Translate the builder's ``UnknownSortKeyError`` into a flat 422 detail."""

        def build(spec: ListQuerySpec, sort: str, search: str | None) -> None:
            raise UnknownSortKeyError(sort)

        dep = make_query_param_dep(make_lq_spec(), build)

        with pytest.raises(HTTPUnprocessableEntityException) as exc_info:
            dep(sort="evil")

        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert exc_info.value.detail == "Invalid sort key: 'evil'"

    def test_builder_value_object_is_returned_unchanged(self) -> None:
        """Return exactly what the builder produced, so each path keeps its own type."""
        sentinel = object()

        def build(spec: ListQuerySpec, sort: str, search: str | None) -> object:
            return sentinel

        dep = make_query_param_dep(make_lq_spec(), build)

        assert dep(sort="name") is sentinel

    def test_detail_reports_the_rejected_key_not_the_raw_request_value(self) -> None:
        """Report the key the error carries, which is the request value minus any ``-``."""

        def build(spec: ListQuerySpec, sort: str, search: str | None) -> None:
            raise UnknownSortKeyError(sort.removeprefix("-"))

        dep = make_query_param_dep(make_lq_spec(), build)

        with pytest.raises(HTTPUnprocessableEntityException) as exc_info:
            dep(sort="-evil")

        assert exc_info.value.detail == "Invalid sort key: 'evil'"

    def test_rejection_chains_the_original_error(self) -> None:
        """Keep the ``UnknownSortKeyError`` as the 422's cause for the traceback."""

        def build(spec: ListQuerySpec, sort: str, search: str | None) -> None:
            raise UnknownSortKeyError(sort)

        dep = make_query_param_dep(make_lq_spec(), build)

        with pytest.raises(HTTPUnprocessableEntityException) as exc_info:
            dep(sort="evil")

        assert isinstance(exc_info.value.__cause__, UnknownSortKeyError)

    def test_unrelated_builder_failure_propagates(self) -> None:
        """Leave a non-sort failure alone rather than reporting it as a bad sort key."""

        def build(spec: ListQuerySpec, sort: str, search: str | None) -> None:
            raise ValueError("row mismatch")

        dep = make_query_param_dep(make_lq_spec(), build)

        with pytest.raises(ValueError, match="row mismatch"):
            dep(sort="name")

    @pytest.mark.parametrize(
        ("searchable", "expected"),
        [([col(LQItem.name)], {"sort", "search"}), ([], {"sort"})],
        ids=["search-enabled", "search-disabled"],
    )
    def test_declares_only_the_enabled_params(
        self, searchable: list[Any], expected: set[str]
    ) -> None:
        """Declare ``search`` only for a spec whose searchable set is non-empty."""
        dep = make_query_param_dep(
            make_lq_spec(searchable=searchable), _record_build([])
        )

        assert set(inspect.signature(dep).parameters) == expected

    def test_each_dep_gets_its_own_param_declarations(self) -> None:
        """Build a fresh declaration per dependency, as FastAPI binds one per param."""
        spec = make_lq_spec(searchable=[col(LQItem.name)])
        deps = [make_query_param_dep(spec, _record_build([])) for _ in range(2)]

        defaults = [inspect.signature(dep).parameters["sort"].default for dep in deps]

        assert all(isinstance(default, params.Query) for default in defaults)
        assert defaults[0] is not defaults[1]

    def test_signature_is_the_functions_own(self) -> None:
        """Reflect a statically-defined signature, never a synthesized one.

        A dynamically built signature has silently broken OpenAPI reflection in this
        repo before, which is why the shape is two hand-written inner functions.
        """
        dep = make_query_param_dep(make_lq_spec(), _record_build([]))

        assert "__signature__" not in vars(dep)
        assert not hasattr(dep, "__wrapped__")


_SESSION_SENTINEL_APP = FastAPI()

_search_dep = make_list_query_dep(LQItemManager)
_no_search_dep = make_list_query_dep(LQ_ITEM_NO_SEARCH_SPEC)


async def _get_lq_session() -> AsyncSession:
    """Raise to force a per-test session-dependency override."""
    raise NotImplementedError


@_SESSION_SENTINEL_APP.get("/lq")
async def _lq_route(
    list_query: Annotated[ListQuery, Depends(_search_dep)],
    session: Annotated[AsyncSession, Depends(_get_lq_session)],
    pagination: Annotated[Pagination, Depends(pagination_dep)],
) -> PaginatedResponse[LQItem]:
    return await LQItemManager.list_query_paginated(
        session, list_query=list_query, pagination=pagination
    )


@_SESSION_SENTINEL_APP.get("/lq-nosearch")
async def _lq_nosearch_route(
    list_query: Annotated[ListQuery, Depends(_no_search_dep)],
    session: Annotated[AsyncSession, Depends(_get_lq_session)],
    pagination: Annotated[Pagination, Depends(pagination_dep)],
) -> PaginatedResponse[LQItem]:
    return await LQItemManager.list_query_paginated(
        session, list_query=list_query, pagination=pagination
    )


@pytest_asyncio.fixture(name="lq_session")
async def lq_session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an isolated SQLite session seeded with ordered, searchable rows."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            for name in SEED_NAMES:
                await LQItemManager.save(session, LQItem(name=name, category="default"))
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(name="lq_client")
async def lq_client_fixture(
    lq_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Yield an async client bound to the throwaway list-query app."""

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield lq_session

    _SESSION_SENTINEL_APP.dependency_overrides[_get_lq_session] = _override_session
    transport = ASGITransport(app=_SESSION_SENTINEL_APP)
    client = AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client
    finally:
        await client.aclose()
        _SESSION_SENTINEL_APP.dependency_overrides = {}


class TestListQueryDependencyOpenAPI:
    """Cover conditional query-parameter exposure in the generated OpenAPI."""

    def test_search_enabled_route_exposes_search_param(self) -> None:
        """Expose ``sort`` and ``search`` on the search-enabled route."""
        params = _SESSION_SENTINEL_APP.openapi()["paths"]["/lq"]["get"]["parameters"]
        names = {param["name"] for param in params}
        assert "sort" in names
        assert "search" in names

    def test_search_disabled_route_omits_search_param(self) -> None:
        """Expose only ``sort`` (not ``search``) on the search-disabled route."""
        params = _SESSION_SENTINEL_APP.openapi()["paths"]["/lq-nosearch"]["get"][
            "parameters"
        ]
        names = {param["name"] for param in params}
        assert "sort" in names
        assert "search" not in names

    def test_sort_param_publishes_the_allowlist_as_an_enum(self) -> None:
        """Publish both directions of every sortable key, so a client can discover them."""
        params = _SESSION_SENTINEL_APP.openapi()["paths"]["/lq"]["get"]["parameters"]
        sort = next(param for param in params if param["name"] == "sort")

        assert sort["schema"]["enum"] == [
            "created_at",
            "-created_at",
            "name",
            "-name",
        ]

    def test_query_params_carry_descriptions(self) -> None:
        """Document both parameters, so the generated client is not bare strings."""
        params = _SESSION_SENTINEL_APP.openapi()["paths"]["/lq"]["get"]["parameters"]
        described = {
            param["name"]: param.get("description")
            for param in params
            if param["name"] in {"sort", "search"}
        }

        assert described == {
            "sort": SORT_PARAM_DESCRIPTION,
            "search": SEARCH_PARAM_DESCRIPTION,
        }


class TestListQueryDependencyRequests:
    """Cover request-boundary behavior of the list-query dependency and applier."""

    @pytest.mark.asyncio
    async def test_default_sort_applied(self, lq_client: AsyncClient) -> None:
        """Apply the default sort key when ``sort`` is omitted."""
        response = await lq_client.get("/lq")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["name"] for item in body["items"]] == list(SEED_NAMES)
        assert body["total"] == len(SEED_NAMES)

    @pytest.mark.asyncio
    async def test_descending_sort_applied(self, lq_client: AsyncClient) -> None:
        """Apply descending order when a ``-`` prefixed key is requested."""
        response = await lq_client.get("/lq", params={"sort": "-name"})
        assert response.status_code == status.HTTP_200_OK
        names = [item["name"] for item in response.json()["items"]]
        assert names == list(reversed(SEED_NAMES))

    @pytest.mark.asyncio
    async def test_out_of_allowlist_sort_returns_422(
        self, lq_client: AsyncClient
    ) -> None:
        """Reject an out-of-allowlist sort key with HTTP 422."""
        response = await lq_client.get("/lq", params={"sort": "evil"})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.asyncio
    async def test_rejected_sort_names_the_key_in_a_flat_detail(
        self, lq_client: AsyncClient
    ) -> None:
        """Report the rejected key in a flat ``detail`` body, not a validation list."""
        response = await lq_client.get("/lq", params={"sort": "evil"})

        assert response.json() == {"detail": "Invalid sort key: 'evil'"}

    @pytest.mark.asyncio
    async def test_rejected_descending_key_is_reported_without_its_prefix(
        self, lq_client: AsyncClient
    ) -> None:
        """Strip the direction marker from the reported key, as the allowlist holds it."""
        response = await lq_client.get("/lq", params={"sort": "-evil"})

        assert response.json() == {"detail": "Invalid sort key: 'evil'"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sort",
        ["", "-", "name ", " name", "NAME", "-evil"],
        ids=[
            "empty",
            "bare-dash",
            "trailing-space",
            "leading-space",
            "case",
            "unknown",
        ],
    )
    async def test_sort_keys_are_matched_exactly(
        self, lq_client: AsyncClient, sort: str
    ) -> None:
        """Match allowlist keys byte-for-byte: nothing is trimmed, folded, or defaulted."""
        response = await lq_client.get("/lq", params={"sort": sort})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.asyncio
    async def test_sort_injection_attempt_never_reaches_the_query(
        self, lq_client: AsyncClient
    ) -> None:
        """Reject a SQL-shaped sort value and leave the table listable afterwards."""
        rejected = await lq_client.get(
            "/lq", params={"sort": f"name); DROP TABLE {LQItem.__tablename__}; --"}
        )
        assert rejected.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        surviving = await lq_client.get("/lq")
        assert surviving.json()["total"] == len(SEED_NAMES)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "term", ["%", "_", "\\", "a%a"], ids=["pct", "us", "esc", "mixed"]
    )
    async def test_search_wildcards_match_literally(
        self, lq_client: AsyncClient, term: str
    ) -> None:
        """Escape LIKE metacharacters, so a wildcard cannot widen the filtered total."""
        response = await lq_client.get("/lq", params={"search": term})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_repeated_sort_param_takes_the_last_value(
        self, lq_client: AsyncClient
    ) -> None:
        """Resolve a repeated ``sort`` to its last value rather than erroring."""
        response = await lq_client.get(
            "/lq", params=[("sort", "name"), ("sort", "-name")]
        )

        assert response.status_code == status.HTTP_200_OK
        names = [item["name"] for item in response.json()["items"]]
        assert names == list(reversed(SEED_NAMES))

    @pytest.mark.asyncio
    async def test_search_filters_and_reports_filtered_total(
        self, lq_client: AsyncClient
    ) -> None:
        """Filter rows by search and report the filtered total, not the page size."""
        response = await lq_client.get("/lq", params={"search": "alp"})
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["name"] for item in body["items"]] == ["alpha"]
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_empty_search_returns_all_rows(self, lq_client: AsyncClient) -> None:
        """Apply no predicate for an empty search term."""
        response = await lq_client.get("/lq", params={"search": ""})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == len(SEED_NAMES)

    @pytest.mark.asyncio
    async def test_search_param_ignored_on_search_disabled_route(
        self, lq_client: AsyncClient
    ) -> None:
        """Ignore a ``search`` param on a route whose spec disables search."""
        response = await lq_client.get("/lq-nosearch", params={"search": "alp"})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == len(SEED_NAMES)
