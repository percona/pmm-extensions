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

"""Define shared offset/limit pagination models and fetch helpers."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 — validate_call resolves these at runtime
    Awaitable,
    Callable,
)
from typing import Any, Generic, NotRequired, TYPE_CHECKING, TypeVar

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field, PositiveInt, validate_call

from app.core.utils import run_pydantic_type_validator

T = TypeVar("T")
U = TypeVar("U")

MAX_PAGINATION_LIMIT = 200
DEFAULT_PAGINATION_OFFSET = 0
DEFAULT_PAGINATION_LIMIT = 50


class IncompletePaginationError(RuntimeError):
    """Raise when a paginated walk ends short of the total its pages reported.

    :param collected: The number of items the walk collected.
    :param total: The greatest total any page reported.
    """

    def __init__(self, collected: int, total: int) -> None:
        self.collected = collected
        self.total = total
        super().__init__(f"Collected {collected} items of the {total} reported")


class PaginatedDictPage(TypedDict):
    """Raw paginated upstream API page.

    Upstream may omit ``total``, ``offset``, or ``limit``; fetch helpers fill
    those before walking pages (``total`` uses a high placeholder, never
    ``len(items)`` alone).

    :param items: Rows returned for the current page.
    :type items: list[Any]
    :param total: Total matching rows across all pages.
    :type total: NotRequired[int]
    :param offset: Zero-based offset for this page.
    :type offset: NotRequired[int]
    :param limit: Page size for this request.
    :type limit: NotRequired[int]
    """

    items: list[Any]
    total: NotRequired[int]
    offset: NotRequired[int]
    limit: NotRequired[int]


def _coerce_dict_page(
    raw: Any,
    pagination: Pagination,
    page_size: int,
) -> tuple[dict[str, Any], bool]:
    """Normalize a raw upstream payload into a paginated dict envelope.

    :return: Envelope plus whether ``total`` came from upstream (not synthesized).
    :rtype: tuple[dict[str, Any], bool]
    """
    if isinstance(raw, list):
        envelope: dict[str, Any] = {"items": raw}
    elif isinstance(raw, dict):
        envelope = dict(raw)
    else:
        envelope = {}
    total_authoritative = "total" in envelope
    if "items" not in envelope:
        envelope["items"] = []
    if "offset" not in envelope:
        envelope["offset"] = pagination.offset
    if "limit" not in envelope:
        envelope["limit"] = pagination.limit
    if not total_authoritative:
        envelope["total"] = pagination.offset + len(envelope["items"]) + page_size
    return envelope, total_authoritative


class Pagination(BaseModel):
    """Validated offset/limit page window for list endpoints and helpers.

    :param offset: Zero-based index of the first item in the page.
    :type offset: int
    :param limit: Maximum number of items in the page.
    :type limit: int
    """

    offset: int = Field(default=DEFAULT_PAGINATION_OFFSET, ge=0)
    limit: int = Field(
        default=DEFAULT_PAGINATION_LIMIT,
        ge=1,
        le=MAX_PAGINATION_LIMIT,
    )

    def slice(self, seq: Sequence[T]) -> list[T]:
        """Return the page slice of ``seq`` for this offset/limit window.

        :param seq: The sequence to slice.
        :type seq: Sequence[T]
        :return: Items in ``seq`` within this pagination window.
        :rtype: list[T]
        """
        return list(seq[self.offset : self.offset + self.limit])


class PaginatedResponse(BaseModel, Generic[T]):
    """Represent a paginated response envelope.

    :param items: The items returned for the current page.
    :type items: list[T]
    :param total: The total number of matching records across all pages.
    :type total: int
    :param offset: The zero-based starting offset used for this page.
    :type offset: int
    :param limit: The maximum number of items requested for this page.
    :type limit: int
    """

    items: list[T]
    total: int
    offset: int
    limit: int

    @classmethod
    def from_pagination(
        cls,
        items: list[T],
        total: int,
        pagination: Pagination,
    ) -> PaginatedResponse[T]:
        """Build a paginated response echoing the request pagination window.

        :param items: The items returned for the current page.
        :type items: list[T]
        :param total: The total number of matching records across all pages.
        :type total: int
        :param pagination: The pagination window used for this request.
        :type pagination: Pagination
        :return: A paginated response envelope with echoed offset and limit.
        :rtype: PaginatedResponse[T]
        """
        return cls(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )

    def map_items(self, func: Callable[[T], U]) -> PaginatedResponse[U]:
        """Transform page items while preserving pagination metadata.

        :param func: Callable applied to each item on the current page.
        :type func: Callable[[T], U]
        :return: A paginated response with transformed items and unchanged metadata.
        :rtype: PaginatedResponse[U]
        """
        return PaginatedResponse(
            items=[func(item) for item in self.items],
            total=self.total,
            offset=self.offset,
            limit=self.limit,
        )


def build_proxied_page(
    items: list[T],
    upstream: Mapping[str, Any],
    pagination: Pagination,
    *,
    client_side_filtered: bool,
) -> PaginatedResponse[T]:
    """Build a paginated envelope from a proxied upstream list response.

    Pass through the upstream ``total`` when the proxy returned the upstream
    rows unchanged; substitute ``len(items)`` when the rows were filtered in
    this process, since the current-page filtered count is the only honest
    total available without server-side filter support.

    :param items: The rows for the current page, after any client-side filter.
    :param upstream: The raw upstream envelope, read for its ``total``.
    :param pagination: The request pagination window echoed into the envelope.
    :param client_side_filtered: Whether ``items`` was narrowed in this process.
    :return: A paginated envelope with the corrected ``total``.
    """
    total = len(items) if client_side_filtered else upstream.get("total", len(items))
    return PaginatedResponse.from_pagination(items, total, pagination)


@validate_call
async def fetch_all_items(
    get_page: Callable[[Pagination], Awaitable[PaginatedResponse[T]]],
    *,
    page_size: PositiveInt = MAX_PAGINATION_LIMIT,
    stop_on_short_page: bool | Callable[[], bool] = False,
    require_complete: bool = False,
) -> list[T]:
    """Fetch every item by walking paginated upstream responses.

    :param get_page: Async callable returning one page for the given window.
    :param page_size: ``limit`` used for each upstream request.
    :param stop_on_short_page: When ``False`` (default), rely on ``page.total``
        and stop once ``offset >= page.total``. When ``True``, or a callable
        returning ``True`` (no upstream ``total`` fallback), also stop when a
        page returns fewer or more items than ``page_size``.
    :param require_complete: When ``True``, refuse a walk that collects fewer items
        than the greatest ``page.total`` any page reported, so a listing that shrank
        or stopped answering mid-walk is not mistaken for the whole of it.
    :return: All items across every page, in upstream order.
    :raises IncompletePaginationError: If ``require_complete`` is set and the walk
        ended short of the greatest reported total.
    """
    all_items: list[T] = []
    offset = 0
    reported_total = 0
    while True:
        page = await get_page(Pagination(offset=offset, limit=page_size))
        reported_total = max(reported_total, page.total)
        if not page.items:
            break
        all_items.extend(page.items)
        page_item_count = len(page.items)
        offset += page_item_count
        use_short_page_stop = (
            stop_on_short_page() if callable(stop_on_short_page) else stop_on_short_page
        )
        if use_short_page_stop and page_item_count != page_size:
            break
        if offset >= page.total:
            break
    if require_complete and len(all_items) < reported_total:
        raise IncompletePaginationError(len(all_items), reported_total)
    return all_items


@validate_call
async def fetch_all_dict_items(
    fetch_page: Callable[[Pagination], Awaitable[dict[str, Any] | list[Any] | None]],
    *,
    page_size: PositiveInt = MAX_PAGINATION_LIMIT,
) -> list[Any]:
    """Fetch every item from paginated dict responses (e.g. RemoteAPI payloads).

    ``fetch_page`` returns the upstream payload untouched — an envelope, a bare
    list of rows, or ``None`` — because :func:`_coerce_dict_page` normalizes all
    three before the envelope is validated here.

    :param fetch_page: Async callable returning one raw upstream page per window.
    :param page_size: ``limit`` used for each upstream request.
    :return: All ``items`` across every page, in upstream order.
    """
    total_authoritative: bool | None = None

    async def get_page(pagination: Pagination) -> PaginatedResponse[Any]:
        nonlocal total_authoritative
        raw = await fetch_page(pagination)
        envelope, page_total_authoritative = _coerce_dict_page(
            raw, pagination, page_size
        )
        if total_authoritative is None:
            total_authoritative = page_total_authoritative
        page = run_pydantic_type_validator(PaginatedDictPage, envelope)
        return PaginatedResponse.model_validate(page)

    return await fetch_all_items(
        get_page,
        page_size=page_size,
        stop_on_short_page=lambda: total_authoritative is False,
    )
