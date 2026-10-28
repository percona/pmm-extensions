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

"""Provide the row type and specs the in-memory list-query suites drive.

Shared rather than copied per module because every suite exercising the applier, its
dependency, or an adapter over it must assert against the same declaration; a spec
that drifts in one copy leaves the others asserting a contract the subject no longer
has.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import column
from sqlmodel import col

from app.core.db import BaseSQLModel
from app.core.db.crud import BaseSQLModelManager
from app.core.db.list_query import ListQuerySpec

__all__ = [
    "LIST_QUERY_SPEC",
    "NOSEARCH_SPEC",
    "NO_SEARCH_LIST_QUERY_SPEC",
    "SEED_NAMES",
    "LQItem",
    "LQItemManager",
    "ListQueryRow",
    "list_query_rows",
    "make_lq_spec",
]

SEED_NAMES = ("alpha", "bravo", "charlie")


@dataclass(frozen=True, slots=True)
class ListQueryRow:
    """Stand in for a materialized row an in-memory list-query spec describes.

    :param filename: The sortable, searchable, tie-breaking identifier.
    :param title: A nullable sortable and searchable attribute, for NULLS-LAST cover.
    :param created_at: A non-string sortable attribute.
    """

    filename: str
    title: str | None
    created_at: int


LIST_QUERY_SPEC = ListQuerySpec(
    sortable={
        "filename": column("filename"),
        "title": column("title"),
        "created_at": column("created_at"),
    },
    default_sort="-created_at",
    tie_breaker=column("filename"),
    searchable=(column("filename"), column("title")),
)

NO_SEARCH_LIST_QUERY_SPEC = ListQuerySpec(
    sortable={"filename": column("filename")},
    default_sort="filename",
    tie_breaker=column("filename"),
)


def list_query_rows(*specs: tuple[str, str | None, int]) -> list[ListQueryRow]:
    """Build materialized rows from ``(filename, title, created_at)`` triples.

    :param specs: One triple per row, in the order the source materialized them.
    :return: The rows to hand to the applier.
    """
    return [ListQueryRow(filename=f, title=t, created_at=c) for f, t, c in specs]


class LQItem(BaseSQLModel, table=True):
    """Represent a test row backing the list-query dependency and applier scenarios."""

    __tablename__ = "test_list_query_item"

    name: str
    category: str


class LQItemManager(BaseSQLModelManager):
    """Manage ``LQItem`` rows through a search-enabled list-query spec."""

    Model = LQItem
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(LQItem.name),
            "created_at": col(LQItem.created_at),
        },
        default_sort="name",
        tie_breaker=col(LQItem.id),
        searchable=[col(LQItem.name)],
    )


NOSEARCH_SPEC = ListQuerySpec(
    sortable={"name": col(LQItem.name)},
    default_sort="name",
    tie_breaker=col(LQItem.id),
)


def make_lq_spec(**overrides: object) -> ListQuerySpec:
    """Build a valid ``ListQuerySpec`` for ``LQItem``, applying field overrides."""
    kwargs = {
        "sortable": {"name": col(LQItem.name)},
        "default_sort": "name",
        "tie_breaker": col(LQItem.id),
    }
    kwargs.update(overrides)
    return ListQuerySpec(**kwargs)
