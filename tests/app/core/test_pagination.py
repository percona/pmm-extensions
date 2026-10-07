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

"""Define tests for app.core.pagination."""

import inspect
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.pagination import (
    DEFAULT_PAGINATION_LIMIT,
    DEFAULT_PAGINATION_OFFSET,
    fetch_all_dict_items,
    fetch_all_items,
    IncompletePaginationError,
    MAX_PAGINATION_LIMIT,
    PaginatedDictPage,
    PaginatedResponse,
    Pagination,
)
from app.core.pagination.deps import make_pagination_dep

TOTAL_ITEMS = 5
PAGE_SIZE = 2


class TestPaginationConstraints:
    """Test Pagination field validation."""

    def test_defaults(self) -> None:
        """Default offset and limit match module constants."""
        pagination = Pagination()
        assert pagination.offset == DEFAULT_PAGINATION_OFFSET
        assert pagination.limit == DEFAULT_PAGINATION_LIMIT

    def test_rejects_negative_offset(self) -> None:
        """Reject offset below zero."""
        with pytest.raises(ValidationError):
            Pagination(offset=-1)

    def test_rejects_limit_below_one(self) -> None:
        """Reject limit below one."""
        with pytest.raises(ValidationError):
            Pagination(limit=0)

    def test_rejects_limit_above_max(self) -> None:
        """Reject limit above the global cap."""
        with pytest.raises(ValidationError):
            Pagination(limit=MAX_PAGINATION_LIMIT + 1)


class TestPaginationHelpers:
    """Test Pagination helper methods."""

    def test_model_dump_for_query_params(self) -> None:
        """Serialize offset and limit for forwarding to upstream APIs."""
        pagination = Pagination(offset=10, limit=25)
        assert pagination.model_dump() == {"offset": 10, "limit": 25}

    def test_slice_empty_sequence(self) -> None:
        """Return an empty list when the sequence is empty."""
        pagination = Pagination(offset=0, limit=10)
        assert pagination.slice([]) == []

    def test_slice_partial_last_page(self) -> None:
        """Return only remaining items on the final page."""
        pagination = Pagination(offset=4, limit=10)
        assert pagination.slice(list(range(TOTAL_ITEMS))) == [4]

    def test_slice_offset_beyond_length(self) -> None:
        """Return an empty list when offset is past the end."""
        pagination = Pagination(offset=100, limit=10)
        assert pagination.slice(list(range(TOTAL_ITEMS))) == []

    def test_slice_single_item_page(self) -> None:
        """Return a one-item page when limit is one."""
        pagination = Pagination(offset=2, limit=1)
        assert pagination.slice(list(range(TOTAL_ITEMS))) == [2]


class TestPaginatedResponseHelpers:
    """Test PaginatedResponse factory and transform helpers."""

    def test_from_pagination(self) -> None:
        """Build an envelope from items, total, and pagination."""
        offset = 5
        limit = 3
        total = 20
        pagination = Pagination(offset=offset, limit=limit)
        response = PaginatedResponse.from_pagination(
            items=[1, 2],
            total=total,
            pagination=pagination,
        )
        assert response.items == [1, 2]
        assert response.total == total
        assert response.offset == offset
        assert response.limit == limit

    def test_map_items(self) -> None:
        """Transform items while preserving pagination metadata."""
        total = 10
        limit = 3
        response = PaginatedResponse[int](
            items=[1, 2, 3],
            total=total,
            offset=0,
            limit=limit,
        )
        mapped = response.map_items(lambda value: value * 2)
        assert mapped.items == [2, 4, 6]
        assert mapped.total == total
        assert mapped.offset == 0
        assert mapped.limit == limit


class TestFetchAllItems:
    """Test paginated fetch-all helper."""

    @pytest.mark.asyncio
    async def test_fetch_all_items_across_multiple_pages(self) -> None:
        """Concatenate items from every page until total is exhausted."""
        pages = {
            0: PaginatedResponse[int](
                items=[0, 1],
                total=TOTAL_ITEMS,
                offset=0,
                limit=PAGE_SIZE,
            ),
            2: PaginatedResponse[int](
                items=[2, 3],
                total=TOTAL_ITEMS,
                offset=2,
                limit=PAGE_SIZE,
            ),
            4: PaginatedResponse[int](
                items=[4],
                total=TOTAL_ITEMS,
                offset=4,
                limit=PAGE_SIZE,
            ),
        }

        async def get_page(pagination: Pagination) -> PaginatedResponse[int]:
            return pages[pagination.offset]

        assert await fetch_all_items(get_page, page_size=PAGE_SIZE) == list(
            range(TOTAL_ITEMS)
        )

    @pytest.mark.asyncio
    async def test_fetch_all_items_continues_on_short_page_when_total_is_authoritative(
        self,
    ) -> None:
        """Keep walking when upstream returns a short page but total says more remain."""
        pages = {
            0: PaginatedResponse[int](
                items=[0],
                total=3,
                offset=0,
                limit=PAGE_SIZE,
            ),
            1: PaginatedResponse[int](
                items=[1, 2],
                total=3,
                offset=1,
                limit=PAGE_SIZE,
            ),
        }

        async def get_page(pagination: Pagination) -> PaginatedResponse[int]:
            return pages[pagination.offset]

        assert await fetch_all_items(get_page, page_size=PAGE_SIZE) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_fetch_all_items_stops_on_non_full_page_without_authoritative_total(
        self,
    ) -> None:
        """Stop when a page is not full and caller opts into the no-total fallback path."""

        async def get_page(_pagination: Pagination) -> PaginatedResponse[int]:
            return PaginatedResponse[int](
                items=[0],
                total=100,
                offset=0,
                limit=PAGE_SIZE,
            )

        assert await fetch_all_items(
            get_page, page_size=PAGE_SIZE, stop_on_short_page=True
        ) == [0]

    @pytest.mark.asyncio
    async def test_fetch_all_items_empty(self) -> None:
        """Return an empty list when the first page has no items."""

        async def get_page(_pagination: Pagination) -> PaginatedResponse[int]:
            return PaginatedResponse[int](
                items=[],
                total=0,
                offset=0,
                limit=MAX_PAGINATION_LIMIT,
            )

        assert await fetch_all_items(get_page) == []

    @pytest.mark.asyncio
    async def test_fetch_all_items_rejects_invalid_page_size(self) -> None:
        """Reject non-positive page sizes."""

        async def get_page(_pagination: Pagination) -> PaginatedResponse[int]:
            raise AssertionError("get_page should not be called")

        with pytest.raises(ValidationError):
            await fetch_all_items(get_page, page_size=0)


def _pages_of(*pages: tuple[list[int], int]) -> Any:
    """Return a ``get_page`` answering successive calls with the given pages.

    :param pages: Each page's items and reported total, in call order.
    :return: An async page getter for :func:`fetch_all_items`.
    """
    remaining = list(pages)

    async def get_page(pagination: Pagination) -> PaginatedResponse[int]:
        items, total = remaining.pop(0)
        return PaginatedResponse[int](
            items=items, total=total, offset=pagination.offset, limit=PAGE_SIZE
        )

    return get_page


class TestFetchAllItemsRequireComplete:
    """Test the opt-in refusal of a walk that ends short of its reported total."""

    @pytest.mark.asyncio
    async def test_a_short_walk_raises(self) -> None:
        """Refuse a walk whose next page came back empty before the total."""
        get_page = _pages_of(([0, 1], 3), ([], 3))

        with pytest.raises(IncompletePaginationError) as excinfo:
            await fetch_all_items(get_page, page_size=PAGE_SIZE, require_complete=True)

        assert (excinfo.value.collected, excinfo.value.total) == (2, 3)

    @pytest.mark.asyncio
    async def test_a_complete_walk_returns_every_item(self) -> None:
        """Return the items when the walk collected the whole reported total."""
        get_page = _pages_of(([0, 1], 3), ([2], 3))

        assert await fetch_all_items(
            get_page, page_size=PAGE_SIZE, require_complete=True
        ) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_a_later_smaller_total_is_checked_against_the_greatest(
        self,
    ) -> None:
        """Hold the walk to the greatest total any page reported, not the last one."""
        get_page = _pages_of(([0, 1], 5), ([2], 3))

        with pytest.raises(IncompletePaginationError) as excinfo:
            await fetch_all_items(get_page, page_size=PAGE_SIZE, require_complete=True)

        assert (excinfo.value.collected, excinfo.value.total) == (3, 5)

    @pytest.mark.asyncio
    async def test_a_short_walk_is_returned_by_default(self) -> None:
        """Keep returning what was collected when the caller did not opt in."""
        get_page = _pages_of(([0, 1], 3), ([], 3))

        assert await fetch_all_items(get_page, page_size=PAGE_SIZE) == [0, 1]


class TestMakePaginationDep:
    """Test custom pagination dependency factory."""

    def test_rejects_max_limit_above_global_cap(self) -> None:
        """Fail fast when max_limit exceeds MAX_PAGINATION_LIMIT."""
        with pytest.raises(ValueError, match="MAX_PAGINATION_LIMIT"):
            make_pagination_dep(max_limit=MAX_PAGINATION_LIMIT + 1)

    def test_clamps_default_limit_to_max_limit(self) -> None:
        """Default limit must not exceed the factory cap."""
        custom_cap = 10
        dep = make_pagination_dep(max_limit=custom_cap)
        limit_default = inspect.signature(dep).parameters["limit"].default.default
        assert limit_default == custom_cap


class TestFetchAllDictItems:
    """Test dict-page fetch-all helper."""

    @pytest.mark.asyncio
    async def test_fetch_all_dict_items(self) -> None:
        """Concatenate items from validated paginated dict responses."""

        async def fetch_page(pagination: Pagination) -> PaginatedDictPage:
            if pagination.offset == 0:
                return {
                    "items": [0, 1],
                    "total": TOTAL_ITEMS,
                    "offset": 0,
                    "limit": PAGE_SIZE,
                }
            return {
                "items": [2, 3, 4],
                "total": TOTAL_ITEMS,
                "offset": 2,
                "limit": PAGE_SIZE,
            }

        assert await fetch_all_dict_items(fetch_page, page_size=PAGE_SIZE) == list(
            range(TOTAL_ITEMS)
        )

    @pytest.mark.asyncio
    async def test_fetch_all_dict_items_without_total(self) -> None:
        """Walk every page when upstream omits total."""

        async def fetch_page(pagination: Pagination) -> PaginatedDictPage:
            if pagination.offset == 0:
                return {
                    "items": [0, 1],
                    "offset": 0,
                    "limit": PAGE_SIZE,
                }
            return {
                "items": [2, 3, 4],
                "offset": 2,
                "limit": PAGE_SIZE,
            }

        assert await fetch_all_dict_items(fetch_page, page_size=PAGE_SIZE) == list(
            range(TOTAL_ITEMS)
        )

    @pytest.mark.asyncio
    async def test_fetch_all_dict_items_rejects_invalid_items(self) -> None:
        """Reject upstream pages whose ``items`` field is not a list."""

        async def fetch_page(_pagination: Pagination) -> dict[str, Any]:
            return {"items": "not-a-list"}

        with pytest.raises(ValidationError):
            await fetch_all_dict_items(fetch_page)
