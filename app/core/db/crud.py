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

"""Define database operations."""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Generic, Literal, NamedTuple, overload, TypeVar

from pydantic import BaseModel
from sqlalchemy import (
    ChunkedIteratorResult,
    CursorResult,
    delete,
    func,
    Insert,
    inspect,
    ScalarResult,
    Select,
)
from sqlalchemy.engine import TupleResult
from sqlalchemy.exc import DatabaseError, IntegrityError, NoResultFound
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import ColumnExpressionArgument
from sqlalchemy.sql.dml import Delete, Update
from sqlmodel import col, select, SQLModel, update
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import Select as SQLModelSelect
from sqlmodel.sql.expression import SelectOfScalar

from app.core.db import BaseSQLModel
from app.core.db.list_query import ListQuery, ListQuerySpec
from app.core.db.utils import idempotent_insert, unique_violation_columns
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPNotFoundException,
)
from app.core.pagination import (
    PaginatedResponse,
    Pagination,
)

logger = logging.getLogger(__name__)

Whereable = Select | Update | Delete
Executable = Select | Insert | Update | Delete
ColumnExpressionOrStrLabelArgument = str | ColumnExpressionArgument[Any]
W = TypeVar("W", bound=Whereable)
E = TypeVar("E", bound=Executable)
T = TypeVar("T")
S = TypeVar("S", bound=SQLModel)
BS = TypeVar("BS", bound=BaseSQLModel)
B = TypeVar("B", bound=BaseModel)
M = TypeVar("M", bound="BaseSQLModelManager")


class _QueryBuilder(NamedTuple, Generic[W]):
    """Hold a query builder function and the arguments to build it with.

    :param function: The function to build the query.
    :param args: The arguments to pass to the function. Defaults to an empty tuple.
    """

    function: Callable[..., W]
    args: tuple[Any, ...] = ()


def _select_builder(*args: Any) -> _QueryBuilder[Select[Any]]:
    """Create a query builder for SELECT statements.

    :param args: The table the statement selects from, or empty for the
        manager's own.
    :return: A builder that produces the SELECT statement.
    """
    # call-shape-dup-ok: the three builders are already the thin wrappers this
    # rule asks for -- one per statement kind, each naming its own intent.
    return _QueryBuilder(select, args)


def _update_builder(*args: Any, values: Mapping[str, Any]) -> _QueryBuilder[Update]:
    """Create a query builder for UPDATE statements.

    :param args: The table the statement updates, or empty for the manager's own.
    :param values: The column values the statement sets.
    :return: A builder that produces the UPDATE statement.
    """

    def _update(table: T) -> Update:
        return update(table).values(**values)

    return _QueryBuilder(_update, args)


def _delete_builder(*args: Any) -> _QueryBuilder[Delete]:
    """Create a query builder for DELETE statements.

    :param args: The table the statement deletes from, or empty for the
        manager's own.
    :return: A builder that produces the DELETE statement.
    """
    return _QueryBuilder(delete, args)


_DEFAULT_SELECT_QUERY_BUILDER = _select_builder()


class BaseManager:
    """Manage database operations for a SQLAlchemy model.

    :cvar Model: The SQLAlchemy class for which this manager handles operations.
    :cvar list_query_spec: The list-query spec declaring this entity's sortable
        allowlist, searchable columns, and default sort; ``None`` uses the built-in
        ``created_at``-descending fallback for ``BaseSQLModel`` managers.
    """

    # Subclasses map both SQLModel tables and the sqlalchemy-celery-beat
    # declarative models, which share no declared base, so no type narrower than
    # ``type[Any]`` covers them until the manager hierarchy is made generic.
    Model: type[Any]
    list_query_spec: ListQuerySpec | None = None

    @classmethod
    def _construct_instance(cls, instance_create: B, **extra_fields: Any) -> T:
        instance_data = {
            **instance_create.model_dump(include=cls.Model.__table__.columns.keys()),
            **extra_fields,
        }
        return cls.Model(**instance_data)

    @classmethod
    def _get_column(cls, name: str) -> ColumnExpressionArgument:
        """Get the column expression for a field in the model.

        :param name: The name of the field.
        :type name: str
        :return: The column expression for the field.
        :rtype: ColumnExpressionArgument
        """
        return col(getattr(cls.Model, name))

    @classmethod
    def _get_columns(cls, *names: str) -> list[ColumnExpressionArgument]:
        """Get the column expressions for multiple fields in the model.

        :param names: The names of the fields.
        :type names: str
        :return: A list of column expressions for the fields.
        :rtype: list[ColumnExpressionArgument]
        """
        return [cls._get_column(field_name) for field_name in names]

    @classmethod
    def _filter_query(
        cls,
        query: W,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        **equal_filters: Any,
    ) -> W:
        for clause in whereclause:
            query = query.where(clause)
        for field_name, value in equal_filters.items():
            if value is None:
                logger.debug(
                    "Field %s has no value and will be ignored. Use %s.%s.is_(None) to check for null values.",
                    field_name,
                    cls.Model.__name__,
                    field_name,
                )
                continue
            query = query.where(cls._get_column(field_name) == value)
        if select_related:
            query = query.options(*[joinedload(attr) for attr in select_related])
        if query_options:
            query = query.options(*query_options)
        return query

    @overload
    @classmethod
    def _build_query(
        cls,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> Select[Any]: ...

    @overload
    @classmethod
    def _build_query(
        cls,
        *whereclause: ColumnExpressionArgument[bool],
        builder: _QueryBuilder[W],
        select_related: Sequence = (),
        query_options: Sequence = (),
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> W: ...

    @classmethod
    def _build_query(
        cls,
        *whereclause: ColumnExpressionArgument[bool],
        builder: _QueryBuilder[Any] = _DEFAULT_SELECT_QUERY_BUILDER,
        select_related: Sequence = (),
        query_options: Sequence = (),
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> Any:
        """Build the statement ``builder`` produces, filtered and RETURNING-decorated.

        The overloads keep the omitted-``builder`` case concrete: the default
        produces a SELECT, and a bare ``W`` would have no argument to solve from.

        :param whereclause: SQL expressions for the ``where`` clause.
        :param builder: The query builder producing the statement. Defaults to the
            manager's own SELECT.
        :param select_related: Relationship attributes to eagerly load.
        :param query_options: Additional loader options to apply.
        :param returning: If True, return the affected rows as objects of
            ``cls.Model``. If a list of column names is provided, return only those
            columns. Defaults to False.
        :param equal_filters: Column names and their respective filter values.
        :return: The built statement.
        """
        query = cls._filter_query(
            builder.function(*(builder.args or (cls.Model,))),
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            **equal_filters,
        )
        if returning is True:
            query = query.returning(cls.Model)
        elif returning:
            query = query.returning(*cls._get_columns(*returning))
        return query

    @classmethod
    def _get_ordering(
        cls,
    ) -> Iterable[ColumnExpressionOrStrLabelArgument] | None:
        """Return the ordering for SELECT queries.

        :return: The spec-derived default ordering (NULLS-LAST, tie-broken) when
            ``list_query_spec`` is set; otherwise the default ``created_at``-descending
            fallback (tie-broken by primary key) for ``BaseSQLModel`` models, or
            ``None``.
        """
        if cls.list_query_spec is not None:
            return cls.list_query_spec.resolve_sort(None)
        if issubclass(cls.Model, BaseSQLModel):
            # Keep fallback ordering deterministic when created_at ties occur.
            return [cls._get_column("created_at").desc(), cls._get_column("id").desc()]
        return None

    @overload
    @classmethod
    async def _exec(
        cls,
        session: AsyncSession,
        query: SelectOfScalar[T],
    ) -> ScalarResult[T]: ...

    @overload
    @classmethod
    async def _exec(
        cls,
        session: AsyncSession,
        query: SQLModelSelect[T],
    ) -> TupleResult[T]: ...

    @overload
    @classmethod
    async def _exec(
        cls,
        session: AsyncSession,
        query: Update | Delete | Insert,
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any]: ...

    @classmethod
    async def _exec(
        cls,
        session: AsyncSession,
        query: E,
    ) -> (
        ScalarResult[Any]
        | TupleResult[Any]
        | CursorResult[Any]
        | ChunkedIteratorResult[Any]
    ):
        """Run ``query`` and return its result, keyed on the statement's shape.

        The overloads mirror what ``AsyncSession.exec`` declares for the two
        select forms, so a caller's element type survives into ``.all()`` instead
        of collapsing to a union. ``sqlmodel`` declares no DML overload, so the
        third arm states what the DML path returns at runtime.

        :param session: The asynchronous database session to execute on.
        :param query: The statement to execute.
        :return: The result, keyed on the statement's shape.
        """
        logger.debug("Executing query: %s", query)
        return await session.exec(query)

    @classmethod
    async def _select(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        offset: int | None = None,
        limit: int | None = None,
        order_by: Sequence[ColumnExpressionOrStrLabelArgument] | None = None,
        **equal_filters: Any,
    ) -> TupleResult | ScalarResult:
        ordering = order_by if order_by is not None else cls._get_ordering()
        pagination_requested = offset is not None or limit is not None

        if select_related and pagination_requested:
            pk_query = cls._filter_query(
                select(cls._get_column("id")),
                *whereclause,
                **equal_filters,
            )
            if ordering:
                pk_query = pk_query.order_by(*ordering)
            if offset is not None:
                pk_query = pk_query.offset(offset)
            if limit:
                pk_query = pk_query.limit(limit)
            pk_result = await cls._exec(session, pk_query)
            page_ids = list(pk_result.all())
            query = cls._build_query(
                cls._get_column("id").in_(page_ids),
                select_related=select_related,
                query_options=query_options,
            )
            if ordering:
                query = query.order_by(*ordering)
        else:
            query = cls._build_query(
                *whereclause,
                select_related=select_related,
                query_options=query_options,
                **equal_filters,
            )
            if ordering:
                query = query.order_by(*ordering)
            if offset is not None:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

        result = await cls._exec(session, query)
        return result.unique()

    @classmethod
    async def _mutate_where(
        cls,
        session: AsyncSession,
        builder: _QueryBuilder[Any],
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> CursorResult | ChunkedIteratorResult:
        """Execute a DML statement (UPDATE or DELETE) with the specified filters."""
        query = cls._build_query(
            *whereclause, builder=builder, returning=returning, **equal_filters
        )
        result = await cls._exec(session, query)
        await session.commit()
        return result

    @classmethod
    async def _dml_where(
        cls,
        session: AsyncSession,
        builder: _QueryBuilder[Any],
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> CursorResult | ChunkedIteratorResult | list:
        """Execute a DML statement (UPDATE or DELETE) with the specified filters.

        This method ensures that at least one filter is provided to avoid unintentional
        mass updates or deletions.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param builder: The builder producing the UPDATE or DELETE statement.
        :param whereclause: The filter expressions applied to the statement.
        :param returning: The column names to return, ``True`` for whole rows, or
            ``False`` for none. A non-``bool`` argument is materialized before it is
            inspected, so a one-shot iterable survives the emptiness check and the row
            read below.
        :param equal_filters: Additional equality filters applied to the statement.
        :return: The raw cursor result when nothing is returned, a list of rows when
            more than one column is requested, or a list of scalars for a single one.
        :raises ValueError: If neither ``whereclause`` nor ``equal_filters`` is given,
            or if ``returning`` is an empty iterable — the overloads promise a list of
            rows for any non-``bool`` ``returning``, and an empty one names no column
            to return.
        """
        if not whereclause and not equal_filters:
            raise ValueError(
                "You must specify at least one filter in *whereclause or **equal_filters"
            )
        if not isinstance(returning, bool):
            returning = tuple(returning)
            if not returning:
                raise ValueError(
                    "returning must name at least one column, or be True or False"
                )

        result = await cls._mutate_where(
            session,
            builder,
            *whereclause,
            returning=returning,
            **equal_filters,
        )

        if returning is True or (returning and len(returning) > 1):
            return list(result.all())
        if returning:
            return list(result.scalars().all())
        return result

    @overload
    @classmethod
    async def update_where(
        cls,
        session: AsyncSession,
        values: Mapping[str, Any],
        *whereclause: ColumnExpressionArgument[bool],
        returning: Literal[False] = False,
        **equal_filters: Any,
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any]: ...

    @overload
    @classmethod
    async def update_where(
        cls,
        session: AsyncSession,
        values: Mapping[str, Any],
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | Literal[True],
        **equal_filters: Any,
    ) -> list[Any]: ...

    @classmethod
    async def update_where(
        cls,
        session: AsyncSession,
        values: Mapping[str, Any],
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any] | list[Any]:
        """Execute an UPDATE statement.

        A truthy ``returning`` always yields a list, so the overloads let a caller
        state which of the two shapes it asked for.

        This method executes an UPDATE statement to update specific values for rows
        matching the specified filters.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param values: A mapping with column names as keys and values as values.
        :param whereclause: SQL expressions for the `where` clause of the query.
        :param returning: If True, return the updated rows as objects of `cls.Model`. If
            a list of column names is provided, return only those columns. Defaults to
            False, meaning no rows are returned from the statement.
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :return: The result of the UPDATE statement execution.
        """
        return await cls._dml_where(
            session,
            _update_builder(values=values),
            *whereclause,
            returning=returning,
            **equal_filters,
        )

    @overload
    @classmethod
    async def delete_where(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        returning: Literal[False] = False,
        **equal_filters: Any,
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any]: ...

    @overload
    @classmethod
    async def delete_where(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | Literal[True],
        **equal_filters: Any,
    ) -> list[Any]: ...

    @classmethod
    async def delete_where(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        returning: Iterable[str] | bool = False,
        **equal_filters: Any,
    ) -> CursorResult[Any] | ChunkedIteratorResult[Any] | list[Any]:
        """Execute a DELETE statement.

        This method executes a DELETE statement to delete specific rows matching the
        specified filters.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param whereclause: SQL expressions for the `where` clause of the query.
        :param returning: If True, return the updated rows as objects of `cls.Model`. If
            a list of column names is provided, return only those columns. Defaults to
            False, meaning no rows are returned from the statement.
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :return: The result of the DELETE statement execution.
        """
        return await cls._dml_where(
            session,
            _delete_builder(),
            *whereclause,
            returning=returning,
            **equal_filters,
        )

    @classmethod
    async def values_list(
        cls,
        session: AsyncSession,
        fields: Sequence[str],
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        **equal_filters: Any,
    ) -> list[Any]:
        """Return a list of values for the specified fields.

        This method retrieves values for the specified fields from the database. If no
        fields are provided, it retrieves all values for the model in alphabetical
        order.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param fields: The fields to retrieve values for.
        :type fields: Sequence[str]
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :type select_related: Sequence
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: A list of tuples containing the values for the specified fields, or
            a flat list of values if only one field is specified.
        :rtype: list[Any]
        """
        if not fields:
            items = await cls.list(
                session, *whereclause, select_related=select_related, **equal_filters
            )
            return [
                tuple(field[1] for field in sorted(item, key=lambda field: field[0]))
                for item in items
            ]
        query = cls._filter_query(
            select(*cls._get_columns(*fields)),
            *whereclause,
            select_related=select_related,
            **equal_filters,
        )
        result = await cls._exec(session, query)
        return list(result.all())

    @classmethod
    async def list(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        offset: int | None = None,
        limit: int | None = None,
        order_by: Sequence[ColumnExpressionOrStrLabelArgument] | None = None,
        **equal_filters: Any,
    ) -> list[T]:
        """Return a list of matching records, optionally paginated.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param whereclause: SQL expressions for the `where` clause of the query.
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :param query_options: Additional SQLAlchemy query options to apply.
        :param offset: The zero-based starting offset for the query results, or
            ``None`` (default) to return all matching records from the beginning.
        :param limit: The maximum number of records to return, or ``None``
            (default) to return all matching records.
        :param order_by: Column expressions overriding the manager's default
            ordering for this call, or ``None`` (default) to use it.
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :return: A list of matching records.
        """
        result = await cls._select(
            session,
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            order_by=order_by,
            offset=offset,
            limit=limit,
            **equal_filters,
        )
        return list(result.all())

    @classmethod
    async def list_paginated(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        order_by: Sequence[ColumnExpressionOrStrLabelArgument] | None = None,
        pagination: Pagination,
        **equal_filters: Any,
    ) -> PaginatedResponse[T]:
        """Return a paginated response for matching records.

        The filtered ``total`` and the page ``items`` are computed from identical
        predicates (``whereclause`` / ``equal_filters``), so the count always
        matches the visible result set.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :type select_related: Sequence
        :param query_options: Additional SQLAlchemy query options to apply.
        :type query_options: Sequence
        :param order_by: Column expressions overriding the manager's default
            ordering for this page, or ``None`` (default) to use it.
        :param pagination: Validated offset/limit window for this page.
        :type pagination: Pagination
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: A paginated response containing matching records and metadata.
        :rtype: PaginatedResponse[T]
        """
        total = await cls.count(session, *whereclause, **equal_filters)
        items = await cls.list(
            session,
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            order_by=order_by,
            offset=pagination.offset,
            limit=pagination.limit,
            **equal_filters,
        )
        return PaginatedResponse.from_pagination(items, total, pagination)

    @classmethod
    async def list_query_paginated(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        list_query: ListQuery,
        select_related: Sequence = (),
        query_options: Sequence = (),
        pagination: Pagination,
        **equal_filters: Any,
    ) -> PaginatedResponse[T]:
        """Return a paginated response applying a resolved list query.

        The search predicate is folded into the single whereclause set feeding both
        the count and the data query, so the reported total is the filtered total by
        construction; the list query's ordering overrides the manager default.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :param whereclause: Base SQL expressions for the ``where`` clause of the query.
        :param list_query: The resolved sort/search produced at the request boundary.
        :param select_related: Fields to be loaded using ``joinedload`` for related
            objects.
        :param query_options: Additional SQLAlchemy query options to apply.
        :param pagination: Validated offset/limit window for this page.
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :return: A paginated response containing matching records and metadata.
        """
        clauses = whereclause
        if list_query.search_predicate is not None:
            clauses = (*whereclause, list_query.search_predicate)
        total = await cls.count(session, *clauses, **equal_filters)
        items = await cls.list(
            session,
            *clauses,
            select_related=select_related,
            query_options=query_options,
            order_by=list_query.order_by,
            offset=pagination.offset,
            limit=pagination.limit,
            **equal_filters,
        )
        return PaginatedResponse.from_pagination(items, total, pagination)

    @classmethod
    async def first(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        **equal_filters: Any,
    ) -> T | None:
        """Return the first record that matches the query.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :type select_related: Sequence
        :param query_options: Additional SQLAlchemy query options to apply.
        :type query_options: Sequence
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: The first matching record, or None if no match is found.
        :rtype: T | None
        """
        result = await cls._select(
            session,
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            **equal_filters,
        )
        return result.first()

    @classmethod
    async def get(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        **equal_filters: Any,
    ) -> T:
        """Return the single record that matches the query.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :type select_related: Sequence
        :param query_options: Additional SQLAlchemy query options to apply.
        :type query_options: Sequence
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: The matching record.
        :rtype: T
        :raises NoResultFound: If no record is found that matches the query.
        """
        result = await cls._select(
            session,
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            **equal_filters,
        )
        return result.one()

    @classmethod
    async def get_or_404(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        **equal_filters: Any,
    ) -> T:
        """Return the single record that matches the query, or raise a 404 error.

        :param session: The SQLAlchemy asynchronous session to use for query execution.
        :type session: AsyncSession
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param select_related: Fields to be loaded using `joinedload` for related
            objects.
        :type select_related: Sequence
        :param query_options: Additional SQLAlchemy query options to apply.
        :type query_options: Sequence
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: The matching record.
        :rtype: T
        :raises HTTPNotFoundException: If no record is found that matches the query.
        """
        try:
            return await cls.get(
                session,
                *whereclause,
                select_related=select_related,
                query_options=query_options,
                **equal_filters,
            )
        except NoResultFound:
            raise HTTPNotFoundException from None

    @classmethod
    async def save_batch(
        cls,
        session: AsyncSession,
        *instances: T,
        flag_modified_fields: Sequence[str] = (),
    ) -> tuple[T, ...]:
        """Save multiple instances of a model to the database.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param instances: The model instances to be saved.
        :type instances: T
        :param flag_modified_fields: Fields to be flagged as modified before saving.
        :type flag_modified_fields: Sequence[str]
        :return: The saved instances.
        :rtype: tuple[T, ...]
        """
        for instance in instances:
            for field in flag_modified_fields:
                flag_modified(instance, field)
            session.add(instance)
        await session.commit()
        return instances

    @classmethod
    async def save(
        cls,
        session: AsyncSession,
        instance: T,
        *,
        flag_modified_fields: Sequence[str] = (),
    ) -> T:
        """Save a model instance to the database.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param instance: The model instance to be saved.
        :param flag_modified_fields: Fields to be flagged as modified before saving.
        :return: The saved instance.
        :raises HTTPConflictException: If the write violates one of the Model's
            unique keys, whether declared as a unique index or a
            ``UniqueConstraint``.
        :raises HTTPBadRequestException: If any other DatabaseError occurs during
            commit.
        """
        for field in flag_modified_fields:
            flag_modified(instance, field)
        session.add(instance)
        try:
            await session.commit()
        except IntegrityError as error:
            # The failed statement leaves the transaction unusable, so the session
            # has to be released before the caller can act on the conflict.
            await session.rollback()
            duplicate_columns = unique_violation_columns(cls.Model.__table__, error)
            if duplicate_columns is None:
                logger.exception("DatabaseError saving instance %s", instance)
                raise HTTPBadRequestException from None
            # Drop columns marked exclude=True (e.g. retirement_key) so the
            # conflict message only names caller-visible fields.
            #
            # Caller audit: the only SQLModel unique keys that include an
            # exclude=True column today are RetirableSQLModel's indexes on
            # Node, Service, Schema, and Table (retirement_key). Every other
            # BaseSQLModelManager unique key has no excluded members, so its
            # 409 wording is unchanged. Plain SQLAlchemy managers
            # (celery-beat IntervalSchedule / CrontabSchedule / PeriodicTask)
            # have no model_fields — treat every column as visible so their
            # conflict messages stay unchanged too.
            fields = getattr(cls.Model, "model_fields", {})
            visible_columns = [
                name
                for name in duplicate_columns
                if not (field := fields.get(name)) or not field.exclude
            ]
            if visible_columns:
                detail = (
                    f"{cls.Model.__name__} with the same "
                    f"{', '.join(visible_columns)} already exists."
                )
            else:
                detail = f"{cls.Model.__name__} already exists."
            raise HTTPConflictException(detail) from None
        except DatabaseError:
            logger.exception("DatabaseError saving instance %s", instance)
            raise HTTPBadRequestException from None
        else:
            logger.debug(
                "Saved instance of %s with id %s", cls.Model.__name__, instance.id
            )
        await session.refresh(instance)
        return instance

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        instance_create: B,
        **extra_fields: Any,
    ) -> T:
        """Create and save a new model instance in the database.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param instance_create: The data used to create the new model instance.
        :type instance_create: B
        :param extra_fields: Additional fields to be set on the model instance.
        :type extra_fields: Any
        :return: The newly created and saved instance.
        :rtype: T
        """
        instance = cls._construct_instance(instance_create, **extra_fields)
        logger.debug("Creating new instance of %s: %s", cls.Model.__name__, instance)
        return await cls.save(session, instance)

    @classmethod
    async def get_or_create(
        cls,
        session: AsyncSession,
        instance_create: B,
        filter_include: set[str] | None = None,
        **extra_fields: Any,
    ) -> tuple[T, bool]:
        """Retrieve an existing model instance or create a new one if none exists.

        This method attempts to find an instance of `cls.Model` with the fields defined
        in `instance_create` and (optionally) specified in `filter_include`. If such an
        instance exists, it returns it. Otherwise, it creates and saves a new one.

        The creation step is conflict-tolerant: it uses a dialect-aware idempotent
        insert (``INSERT ... ON CONFLICT DO NOTHING`` on PostgreSQL and SQLite) so
        that two calls racing to create the same row do not surface a duplicate-key
        error. The losing call no-ops on the insert and refetches the winning row
        with ``created=False``. ``created`` is ``True`` only for the call whose insert
        actually landed.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param instance_create: The data used to filter and possibly create the
            instance.
        :param filter_include: The set of fields of `instance_create` to be included in
            the search filter. Use None (default) for all fields.
        :param extra_fields: Additional fields to be set on the created instance.
        :return: The existing or newly created instance of `cls.Model`, and a bool
            specifying whether a new instance was created.
        :raises HTTPBadRequestException: If a ``DatabaseError`` occurs during the
            insert commit.
        :raises RuntimeError: If the post-conflict refetch matches no row, meaning
            ``filter_include`` reaches outside a unique constraint.
        """
        existent_instance = await cls.first(
            session, **instance_create.model_dump(include=filter_include)
        )
        if existent_instance:
            return existent_instance, False

        # Managers that override create() carry domain guards/side-effects (e.g.
        # SyncItemManager raises if a matching item is already in progress). The
        # conflict-tolerant fast path below bypasses create() entirely, so it must
        # only apply when create() is the inherited base implementation.
        if cls.create.__func__ is not BaseManager.create.__func__:
            return await cls.create(session, instance_create, **extra_fields), True

        instance = cls._construct_instance(instance_create, **extra_fields)
        pk_names = {column.name for column in inspect(cls.Model).primary_key}
        values = {}
        for column in cls.Model.__table__.columns:
            value = getattr(instance, column.name)
            carries_default = (
                column.default is not None or column.server_default is not None
            )
            # Skip a None whose value the database supplies: any PK column (e.g.
            # autoincrement) and any column with a SQLAlchemy/server default. Passing
            # it explicitly would override that default with NULL.
            if value is None and (column.name in pk_names or carries_default):
                continue
            values[column.name] = value
        statement = idempotent_insert(session.get_bind().name, cls.Model).values(
            **values
        )
        try:
            result = await cls._exec(session, statement)
            await session.commit()
        except DatabaseError:
            logger.exception(
                "DatabaseError in get_or_create for %s", cls.Model.__name__
            )
            raise HTTPBadRequestException from None
        created = result.rowcount == 1
        # A core insert does not populate the constructed instance's PK, so refetch.
        row = await cls.first(
            session, **instance_create.model_dump(include=filter_include)
        )
        if row is None:
            # Fail loud rather than return (None, False) and defer a confusing crash
            # to the caller.
            raise RuntimeError(
                f"{cls.Model.__name__}.get_or_create resolved a unique conflict but "
                f"no row matched filter_include={filter_include!r}; filter_include "
                f"must be a subset of a unique constraint."
            )
        return row, created

    @classmethod
    async def update(
        cls,
        session: AsyncSession,
        existing_instance: T,
        updated_instance: B,
        *,
        flag_modified_fields: Sequence[str] = (),
        **extra_fields: Any,
    ) -> T:
        """Update an existing model instance with new data and save it.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param existing_instance: The existing model instance to be updated.
        :type existing_instance: T
        :param updated_instance: The new data to update the model instance with.
        :type updated_instance: B
        :param flag_modified_fields: Fields to be flagged as modified before saving.
        :type flag_modified_fields: Sequence[str]
        :param extra_fields: Additional fields to be set on the model instance.
        :type extra_fields: Any
        :return: The updated and saved instance.
        :rtype: T
        """
        logger.debug(
            "Updating existing instance of %s (%s): %s",
            cls.Model.__name__,
            existing_instance.id,
            updated_instance,
        )
        updated_data = (
            updated_instance.model_dump(include=cls.Model.__table__.columns.keys())
            | extra_fields
        )
        for key, value in updated_data.items():
            setattr(existing_instance, key, value)
        return await cls.save(
            session,
            existing_instance,
            flag_modified_fields=flag_modified_fields,
        )

    @classmethod
    async def delete(cls, session: AsyncSession, instance: T) -> T:
        """Delete a model instance from the database.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param instance: The model instance to be deleted.
        :type instance: T
        :return: The deleted instance.
        :rtype: T
        """
        await session.delete(instance)
        await session.commit()
        return instance

    @classmethod
    async def count(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        **equal_filters: Any,
    ) -> int:
        """Return the count of records that match the query.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param whereclause: SQL expressions for the `where` clause of the query.
        :type whereclause: ColumnExpressionArgument[bool]
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :type equal_filters: Any
        :return: The count of matching records.
        :rtype: int
        """
        query = cls._filter_query(
            select(func.count()).select_from(cls.Model), *whereclause, **equal_filters
        )
        result = await session.scalar(query)
        return result or 0

    @classmethod
    async def exists(
        cls,
        session: AsyncSession,
        *whereclause: ColumnExpressionArgument[bool],
        **equal_filters: Any,
    ) -> bool:
        """Return whether any record matches the query.

        Emit a short-circuiting ``SELECT EXISTS (...)`` so the database can
        stop at the first matching row. Filter arguments match :meth:`count`
        and are applied through :meth:`_filter_query` (``None`` equal-filter
        values are skipped — same behaviour as ``count``).

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :param whereclause: SQL expressions for the ``where`` clause of the query.
        :param equal_filters: Keyword arguments representing column names and their
            respective filter values.
        :return: ``True`` when at least one matching row exists.
        """
        inner = cls._filter_query(select(cls.Model), *whereclause, **equal_filters)
        result = await session.scalar(select(inner.exists()))
        return bool(result)


class BaseSQLModelManager(BaseManager):
    """Manage database operations for a BaseSQLModel-based model.

    :param Model: The BaseSQLModel class for which this manager handles operations.
    """

    Model: type[BaseSQLModel]

    @classmethod
    def _construct_instance(
        cls, instance_create: S, **extra_fields: Any
    ) -> BaseSQLModel:
        pk_column = inspect(cls.Model).primary_key[0]
        if pk_column.autoincrement and isinstance(
            None,
            cls.Model.model_fields[pk_column.name].annotation,
        ):
            extra_fields[pk_column.name] = None
        return cls.Model.model_validate(instance_create, update=extra_fields)

    @classmethod
    async def update(
        cls,
        session: AsyncSession,
        existing_instance: BS,
        updated_instance: S,
        *,
        flag_modified_fields: Sequence[str] = (),
        **extra_fields: Any,
    ) -> BS:
        """Update an existing model instance with new data and save it.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param existing_instance: The existing model instance to be updated.
        :type existing_instance: BS
        :param updated_instance: The new data to update the model instance with.
        :type updated_instance: S
        :param flag_modified_fields: Fields to be flagged as modified before saving.
        :type flag_modified_fields: Sequence[str]
        :param extra_fields: Additional fields to be set on the model instance.
        :type extra_fields: Any
        :return: The updated and saved instance.
        :rtype: BS
        """
        logger.debug(
            "Updating existing instance of %s (%s): %s",
            cls.Model.__name__,
            existing_instance.id,
            updated_instance,
        )
        updated_instance_data = (
            updated_instance.model_dump(exclude_unset=True) | extra_fields
        )
        existing_instance.sqlmodel_update(updated_instance_data)
        return await cls.save(
            session,
            existing_instance,
            flag_modified_fields=flag_modified_fields,
        )


class BaseSQLModelChildManager(BaseSQLModelManager):
    """Manage database operations for child models with a parent association.

    :param ParentManager: The manager class responsible for handling the parent model.
    :param connected_by: The field name that connects the child model to the parent
        model.
    """

    ParentManager: type[BaseSQLModelManager]
    connected_by: str

    @classmethod
    async def update(
        cls,
        session: AsyncSession,
        existing_instance: T,
        updated_instance: B,
        *,
        flag_modified_fields: Sequence[str] = (),
        **extra_fields: Any,
    ) -> T:
        """Update an existing child model instance, ensuring parent association.

        :param session: The SQLAlchemy asynchronous session to use for database
            operations.
        :type session: AsyncSession
        :param existing_instance: The existing child model instance to be updated.
        :type existing_instance: T
        :param updated_instance: The new data to update the child model instance with.
        :type updated_instance: B
        :param flag_modified_fields: Fields to be flagged as modified before saving.
        :type flag_modified_fields: Sequence[str]
        :param extra_fields: Additional fields to be set on the model instance.
        :type extra_fields: Any
        :return: The updated and saved child instance.
        :rtype: T
        :raises HTTPBadRequestException: If the parent foreign key is supplied but the
            referenced parent instance does not exist. The check is skipped when the
            foreign key is omitted, preserving the base manager's partial-update
            (``exclude_unset``) semantics.
        """
        supplied_fields = updated_instance.model_dump(exclude_unset=True)
        if cls.connected_by in supplied_fields or cls.connected_by in extra_fields:
            parent_id = extra_fields.get(
                cls.connected_by, getattr(updated_instance, cls.connected_by, None)
            )
            # An explicitly-supplied null FK cannot persist on the non-nullable parent
            # column; reject it deterministically rather than delegating to ``get``,
            # whose ``id=None`` filter is ignored (it would match every parent row).
            if parent_id is None:
                raise HTTPBadRequestException(
                    f"Invalid {cls.connected_by}: {parent_id}"
                )
            try:
                await cls.ParentManager.get(session, id=parent_id)
            except NoResultFound:
                raise HTTPBadRequestException(
                    f"Invalid {cls.connected_by}: {parent_id}",
                ) from None
        return await super().update(
            session,
            existing_instance,
            updated_instance,
            flag_modified_fields=flag_modified_fields,
            **extra_fields,
        )
