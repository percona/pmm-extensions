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

"""Define database utilities."""

import hashlib
import logging
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from itertools import chain
from typing import Any, Final

from alembic.runtime.migration import MigrationContext
from sqlalchemy import (
    cast,
    Column,
    ColumnClause,
    ColumnElement,
    ForeignKeyConstraint,
    func,
    inspect,
    JSON,
    literal,
    MetaData,
    Table,
    Text,
    text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ReflectedCheckConstraint
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import coercions, ColumnExpressionArgument, roles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.dml import Insert as GenericInsert
from sqlalchemy.sql.schema import BLANK_SCHEMA, RETAIN_SCHEMA, SchemaConst
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy.sql.visitors import InternalTraversal
from sqlmodel import AutoString, col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.config import DatabaseOptions
from app.core.db.sql_types import AutoJSON
from app.core.utils.fields import DatabaseDialect
from app.core.utils.serialization import json_serializer

logger = logging.getLogger(__name__)

SQLAlchemyColumn = ColumnClause | Column | InstrumentedAttribute

# SQLite carries no structured diagnostics, so the violated key is only recoverable
# from the message text.
SQLITE_UNIQUE_VIOLATION_RE = re.compile(r"UNIQUE constraint failed:\s*(?P<columns>.+)")

#: The SQLSTATE PostgreSQL raises when ``lock_timeout`` ends a lock wait.
LOCK_NOT_AVAILABLE_SQLSTATE: Final = "55P03"


def get_async_session_maker_from_engine(engine: AsyncEngine) -> async_sessionmaker:
    """Return a new asynchronous session maker for database operations.

    This function creates a new SQLAlchemy asynchronous session maker using the
    predefined engine configuration.

    :param engine: The SQLAlchemy asynchronous engine to bind the session maker to.
    :type engine: AsyncEngine
    :return: A new asynchronous session maker.
    :rtype: async_sessionmaker
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def create_app_async_engine(database: DatabaseOptions) -> AsyncEngine:
    """Build a service API async engine with pool, connect and schema options.

    ``pool_pre_ping`` is always forwarded. The pool sizing fields carry bounded
    defaults and are forwarded only for a dialect :class:`DatabaseOptions`
    sizes, so a SQLite engine of either backing gets none of them and a
    PostgreSQL one gets whatever :attr:`DatabaseOptions.pool_engine_kwargs`
    resolved. An unset or SQLite-inapplicable ``CONNECT_TIMEOUT`` likewise
    omits ``connect_args`` entirely. A non-empty
    ``database.SCHEMA_TRANSLATE_MAP`` is applied via ``execution_options``
    here, once, so every caller that shares a ``DatabaseOptions`` gets the same
    translation without composing its own.

    :param database: The service database options carrying the URL and any
        configured pool sizing.
    :return: A configured asynchronous engine.
    """
    engine = create_async_engine(
        database.URL,
        echo=False,
        json_serializer=json_serializer,
        **database.connect_engine_kwargs,
        **database.pool_engine_kwargs,
    )
    if database.SCHEMA_TRANSLATE_MAP:
        engine = engine.execution_options(
            schema_translate_map=database.SCHEMA_TRANSLATE_MAP
        )
    return engine


def translate_metadata_schemas(
    metadata: MetaData, translate_map: Mapping[str, str | None]
) -> MetaData:
    """Return ``metadata`` with every symbolic schema token resolved through the map.

    Alembic's autogenerate compares ``Table`` objects against the reflected
    database before any statement executes, so a connection's
    ``schema_translate_map`` never reaches the comparison. Applying the same
    map to a copy lets ``check`` and ``--autogenerate`` see the schema each
    table actually lands in: the default schema for a token mapped to ``None``,
    the mapped name otherwise. A schema absent from the map is kept as
    declared, so an unconfigured token still fails the check loudly.
    Foreign-key targets are resolved by the same rule.

    Two declared tables can resolve to the same physical table on a bind — a
    token mapped to the bind's default schema beside an untokened table of the
    same name, for instance. The first one reached in dependency order is
    copied; the rest are skipped rather than raising, since that is the table
    the bind actually has. This cannot hide a real conflict from ``check``:
    the physical table then carries only one of the two definitions, so the
    comparison reports a mismatch against whichever declared table it does
    not match, exactly where the diff would show it.

    :param metadata: The metadata whose tables may declare symbolic schemas.
    :param translate_map: The bind's ``schema_translate_map``.
    :return: ``metadata`` itself when the map is empty, otherwise a copy.
    """
    if not translate_map:
        return metadata

    def resolve(schema: str | None) -> str | SchemaConst:
        if schema not in translate_map:
            return RETAIN_SCHEMA
        mapped = translate_map[schema]
        return BLANK_SCHEMA if mapped is None else mapped

    def referred_schema(
        _table: Table,
        _to_schema: str | None,
        _constraint: ForeignKeyConstraint,
        referred: str | None,
    ) -> str | SchemaConst:
        return resolve(referred)

    translated = MetaData()
    for table in metadata.sorted_tables:
        resolved = resolve(table.schema)
        schema_arg = None if resolved is BLANK_SCHEMA else resolved
        key_schema = table.schema if resolved is RETAIN_SCHEMA else schema_arg
        key = f"{key_schema}.{table.name}" if key_schema is not None else table.name
        if key in translated.tables:
            continue
        # SQLAlchemy annotates ``schema`` as ``str | Literal[RETAIN_SCHEMA]`` and
        # ``referred_schema_fn`` as returning ``str | None``, but its own docstring
        # says ``None`` selects the target metadata's schema and ``BLANK_SCHEMA``
        # resets a referred schema: the annotations are narrower than the
        # documented runtime contract.
        table.to_metadata(
            translated,
            schema=schema_arg,  # ty: ignore[invalid-argument-type]
            referred_schema_fn=referred_schema,  # ty: ignore[invalid-argument-type]
        )
    return translated


def json_join_path_elems(*path_elems: str) -> str:
    """Join JSON path elements into a single string.

    :param path_elems: The JSON path elements to join.
    :type path_elems: str
    :return: The joined JSON path string.
    :rtype: str
    """
    json_path = "$"
    for elem in path_elems:
        if elem.isdigit():
            json_path += f"[{elem}]"
        else:
            json_path += f".{elem}"
    return json_path


def _column_resolves_to_json(column: ColumnElement) -> bool:
    """Return True when the column's declared SQLAlchemy type is JSON-semantic.

    Unwrap ``TypeDecorator`` chains so columns typed with ``AutoJSON`` (or
    subclasses like ``TaskExecutionRequestJSON``) are recognised as JSON via
    their ``impl`` type and do not receive a redundant ``CAST(... AS JSON)``
    wrapper that would break expression-index matches on ``jsonb`` columns.

    :param column: The SQLAlchemy column element to inspect.
    :type column: ColumnElement
    :return: ``True`` if the column resolves to ``JSON`` or ``JSONB`` (directly
        or through a ``TypeDecorator`` chain), ``False`` otherwise.
    :rtype: bool
    """
    type_obj = column.type
    while isinstance(type_obj, TypeDecorator):
        type_obj = type_obj.impl_instance
    return isinstance(type_obj, JSON)


def func_json_extract(
    db_engine: str, json_column: SQLAlchemyColumn, *path_elems: str
) -> ColumnElement:
    """Render a dialect-specific JSON scalar extraction expression.

    Emit SQL whose shape matches the expression indexes created for
    ``taskhistory.execution_request`` so the planner can use them:

    - PostgreSQL: ``col->'a'->>'b'``. The final ``->>`` returns the value as
      text. The expression is valid on both ``json`` and ``jsonb`` columns.
      Path elements are inlined as SQL literals (via SQLAlchemy's
      ``literal_execute``) instead of bound parameters so the planner can
      syntactically match the expression against the functional indexes.
      Columns whose declared type is not JSON-semantic (e.g.
      ``celery_periodictask.kwargs`` which the third-party
      ``sqlalchemy-celery-beat`` library defines as ``sa.Text()``) are wrapped
      in ``CAST(... AS JSON)`` first, because PostgreSQL does not define
      ``->>`` on ``text``. JSON, JSONB, and ``TypeDecorator`` chains whose
      underlying impl is JSON (e.g. ``AutoJSON``) are left unwrapped so their
      expression indexes keep matching.
    - SQLite: ``json_extract(col, '$.a.b')``. SQLite auto-unquotes scalars, so
      the result is directly comparable to a string.

    :param db_engine: The database engine type (e.g., ``"postgresql"``).
    :type db_engine: str
    :param json_column: The JSON column to extract the value from.
    :type json_column: SQLAlchemyColumn
    :param path_elems: The JSON path elements to extract.
    :type path_elems: str
    :return: A SQL expression whose value is comparable to a string.
    :rtype: ColumnElement
    """
    column = col(json_column)
    if db_engine.startswith(DatabaseDialect.POSTGRESQL):
        expression = column if _column_resolves_to_json(column) else cast(column, JSON)
        for elem in path_elems[:-1]:
            expression = expression.op("->")(literal(elem, Text, literal_execute=True))
        return expression.op("->>", return_type=Text)(
            literal(path_elems[-1], Text, literal_execute=True)
        )
    return func.json_extract(
        column,
        literal(json_join_path_elems(*path_elems), Text, literal_execute=True),
    )


def idempotent_insert(engine_name: str, table: Any) -> GenericInsert:
    """Return a dialect-specific INSERT that ignores duplicate-key conflicts.

    PostgreSQL and SQLite use ``INSERT ... ON CONFLICT DO NOTHING``. The caller
    chains ``.values(...)`` and passes the result to ``session.execute``.

    :param engine_name: SQLAlchemy engine ``name`` (``"postgresql"`` or
        ``"sqlite"``).
    :param table: The target table or ORM model class.
    :return: A dialect-specific insert construct.
    :raises NotImplementedError: If the dialect is not supported.
    """
    if engine_name == DatabaseDialect.POSTGRESQL:
        return postgresql.insert(table).on_conflict_do_nothing()
    if engine_name == DatabaseDialect.SQLITE:
        return sqlite.insert(table).on_conflict_do_nothing()
    raise NotImplementedError(f"idempotent_insert: unsupported dialect {engine_name!r}")


def _columns_of_named_unique_key(table: Table, name: str) -> list[str] | None:
    """Resolve a unique index or constraint name to the column names it spans.

    :param table: The table whose declared unique keys are searched.
    :param name: The index or constraint name the database reported.
    :return: The key's column names, or ``None`` if no declared unique key bears
        that name.
    """
    declared_keys = chain(
        (index for index in table.indexes if index.unique),
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ),
    )
    for key in declared_keys:
        if key.name == name:
            return [column.name for column in key.columns]
    return None


def _reported_constraint_name(error: IntegrityError) -> str | None:
    """Return the constraint name the database driver attached to an error.

    The two drivers expose it in different places, and asyncpg's is reachable only
    through the wrapper SQLAlchemy raises in its place: psycopg carries a ``diag``
    record, while asyncpg sets the attribute on the original exception it chains to.

    :param error: The integrity error raised while flushing or committing.
    :return: The reported constraint name, or ``None`` if neither shape carries one.
    """
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        name = getattr(
            getattr(candidate, "diag", None), "constraint_name", None
        ) or getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


def unique_violation_columns(table: Table, error: IntegrityError) -> list[str] | None:
    """Name the columns of the unique key an integrity error violated.

    The two supported dialects report a violation differently: PostgreSQL names the
    offending index or constraint, which resolves against the table's declared unique
    keys, while SQLite spells the columns into the message itself. Any other integrity
    violation (a foreign key, a ``NOT NULL``, a primary key) resolves to no declared
    unique key, so the caller can tell a duplicate apart from a row the database
    rejected for another reason.

    :param table: The table the failing statement targeted.
    :param error: The integrity error raised while flushing or committing.
    :return: The violated key's column names, or ``None`` when the error is not a
        unique-key violation.
    """
    reported_name = _reported_constraint_name(error)
    if reported_name is not None:
        return _columns_of_named_unique_key(table, reported_name)
    match = SQLITE_UNIQUE_VIOLATION_RE.search(str(error.orig))
    if match is None:
        return None
    return [
        qualified_column.strip().rpartition(".")[2]
        for qualified_column in match["columns"].split(",")
    ]


class NullsLastOrdering(ColumnElement):
    """Render an ``ORDER BY`` term that places NULLs last on every supported dialect.

    PostgreSQL and SQLite render the standard ``NULLS LAST`` clause.

    Takes the direction as a flag rather than a pre-directed expression: wrapping an
    already-``desc()``-ed expression would render ``<expr> DESC ASC NULLS LAST``.

    Participates in SQLAlchemy's compiled-statement cache, with a key that
    discriminates both column and direction.

    :param column: The direction-free column expression to order by.
    :param descending: Whether the primary ordering term is descending.
    """

    _traverse_internals: list[tuple[str, InternalTraversal]] = [
        ("column", InternalTraversal.dp_clauseelement),
        ("descending", InternalTraversal.dp_boolean),
    ]

    def __init__(
        self,
        column: ColumnExpressionArgument,
        *,
        descending: bool = False,
    ) -> None:
        self.column = coercions.expect(roles.ExpressionElementRole, column)
        self.descending = descending


@compiles(NullsLastOrdering)
def _compile_nulls_last_ordering(
    element: NullsLastOrdering, compiler: SQLCompiler, **kw: Any
) -> str:
    """Render the standard ``<expr> <direction> NULLS LAST`` ordering term.

    :param element: The ordering construct being compiled.
    :param compiler: The active SQL compiler.
    :return: The rendered ``ORDER BY`` term.
    """
    direction = "DESC" if element.descending else "ASC"
    return f"{compiler.process(element.column, **kw)} {direction} NULLS LAST"


def prepare_unsafe_value_for_json_comparison(db_engine: str, value: Any) -> Any:
    """Prepare a value for JSON comparison based on the database engine.

    On PostgreSQL the text operator ``->>`` returns JSON scalars as text, so we
    convert the value to a string for comparison. For other databases, we return
    the value as is.

    :param db_engine: The database engine type (e.g., "postgresql").
    :type db_engine: str
    :param value: The value to prepare for comparison.
    :type value: Any
    :return: The prepared value for JSON comparison.
    :rtype: Any
    """
    if db_engine.startswith(DatabaseDialect.POSTGRESQL):
        return str(value)
    return value


def compare_type(
    context: MigrationContext,  # noqa: ARG001
    inspected_column: Column,  # noqa: ARG001
    metadata_column: Column,  # noqa: ARG001
    inspected_type: TypeEngine,
    metadata_type: TypeEngine,
) -> bool | None:
    """Suppress spurious Alembic type diffs for known equivalent type pairs.

    :param context: The Alembic migration context.
    :type context: MigrationContext
    :param inspected_column: The column object as inspected from the database.
    :type inspected_column: Column
    :param metadata_column: The column object as defined in the model's metadata.
    :type metadata_column: Column
    :param inspected_type: The type of the column as determined by the database
        inspector.
    :type inspected_type: TypeEngine
    :param metadata_type: The type of the column as defined in the model's metadata.
    :type metadata_type: TypeEngine
    :return: False if the types are equivalent and no migration is needed;
        None to fall through to default comparison.
    :rtype: bool | None
    """
    if isinstance(inspected_type, Text) and isinstance(metadata_type, AutoString):
        return False
    if isinstance(metadata_type, AutoJSON) and isinstance(inspected_type, JSONB | JSON):
        return False
    return None


def acquire_pg_advisory_xact_lock(bind: Connection, lock_key: int) -> None:
    """Serialize concurrent shared-database migrations on PostgreSQL.

    Take a transaction-scoped advisory lock so two service tracks running
    ``upgrade heads`` against one physical database cannot both pass an
    idempotency preflight and execute the same DDL simultaneously. The lock
    releases automatically at transaction end. No-op on other dialects: SQLite
    and per-service-database deployments give each service its own database, so
    there is no cross-track race to serialize.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param lock_key: The advisory-lock key; all callers racing on the same
        object must pass the same key.
    """
    if bind.dialect.name == DatabaseDialect.POSTGRESQL:
        bind.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


def advisory_lock_key(name: str) -> int:
    """Derive a stable advisory-lock key from the name of the locked object.

    Digest rather than :func:`hash`: the builtin is salted per process, so every
    worker would derive its own key, put itself in its own lock, and fence
    nothing while still reporting success.

    :param name: The name of the locked object; every caller racing on it must
        pass the same name.
    :return: A key inside the signed 32-bit range PostgreSQL's two-argument
        advisory-lock form accepts.
    """
    digest = hashlib.blake2b(name.encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


class AdvisoryLockTimeoutError(Exception):
    """Report that an advisory lock was not granted within its wait bound."""


class AdvisoryLockLostError(Exception):
    """Report that a held advisory lock's connection or transaction is gone."""


class AdvisoryLockHandle:
    """Let the holder of an advisory lock check it still holds it.

    Being inside the lock's block is not the same as holding the lock: the
    server can end the lock's backend while the block runs, which releases a
    transaction-scoped lock without anything in the holder's own coroutine
    failing.

    :ivar connection: The connection whose transaction holds the lock, or
        ``None`` where no lock was taken because the dialect has none.
    """

    def __init__(self, connection: AsyncConnection | None) -> None:
        """Wrap the lock's connection.

        :param connection: The connection whose transaction holds the lock, or
            ``None`` where no lock was taken.
        """
        self.connection = connection

    async def verify(self) -> None:
        """Confirm the lock's transaction is still alive, and so still holds it.

        A transaction-scoped advisory lock is released only when its
        transaction ends, so a statement that succeeds inside that transaction
        proves the lock is still held. A no-op where no lock was taken.

        :raises AdvisoryLockLostError: If the lock's backend is gone or its
            transaction has aborted.
        """
        if self.connection is None:
            return
        try:
            await self.connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise AdvisoryLockLostError(
                "The advisory lock's connection or transaction is gone, so the "
                "lock is no longer held."
            ) from exc


@asynccontextmanager
async def _advisory_lock_connection(
    session: AsyncSession,
    namespace: int,
    key: int,
) -> AsyncIterator[AsyncConnection | None]:
    """Open the connection an advisory lock on ``session``'s database is taken on.

    See :func:`try_pg_advisory_xact_lock` for why the connection is the helper's
    own, on a throwaway ``NullPool`` engine.

    :param session: The session whose engine the lock connection is drawn from.
    :param namespace: The lock-space classifier, named in the warning only.
    :param key: The key within ``namespace``, named in the warning only.
    :return: A connection with a begun transaction, or ``None`` on a dialect
        without advisory locks or a bind that cannot be introspected.
    :raises SQLAlchemyError: If the lock connection cannot be opened.
    """
    bind = session.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    if not isinstance(engine, AsyncEngine):
        logger.warning(
            "Advisory lock %s/%s not taken: bind %s cannot be introspected as an "
            "asynchronous engine, so concurrent callers are not fenced.",
            namespace,
            key,
            type(bind).__name__,
        )
        yield None
        return
    if engine.dialect.name != DatabaseDialect.POSTGRESQL:
        yield None
        return
    lock_engine = create_async_engine(engine.url, poolclass=NullPool)
    try:
        async with lock_engine.connect() as connection:
            await connection.begin()
            yield connection
    finally:
        await lock_engine.dispose()


@asynccontextmanager
async def try_pg_advisory_xact_lock(
    session: AsyncSession,
    namespace: int,
    key: int,
) -> AsyncIterator[bool]:
    """Fence a check-then-write sequence against concurrent callers on PostgreSQL.

    Take the lock on a connection of the helper's own rather than the caller's, so
    it outlives the commits the guarded sequence issues along the way — a lock on
    the caller's connection would be released by the first of them and fence only
    its own final statement. The lock is transaction-scoped on that connection, so
    it is released by exiting the block, by an exception inside it, and by the
    death of the worker holding it, without an unlock call to leak.

    That connection is opened outside the caller's pool, on a throwaway
    ``NullPool`` engine, rather than borrowed from it: the guarded block issues its
    own statements on the caller's session, so a lock holding a slot of that same
    pool would need a second one for as long as the block runs, and a deployment
    configured with ``POOL_SIZE=1``/``MAX_OVERFLOW=0`` would then time out on every
    guarded sequence — the lock owning the only slot while the work it fences waits
    for one. The price is a connect per acquisition, against a sequence that runs
    periodically, and one connection above the configured ceiling while the block
    runs. The engine is built from the caller's URL alone, so the lock connection
    also connects on the driver's own defaults rather than the service's configured
    ``connect_args``: a configured ``CONNECT_TIMEOUT`` does not bound it, and this
    is the guarded sequence's first contact with the database.

    Acquisition never waits: contention yields ``False`` for the caller to refuse
    on, because a caller blocking here would hold its connection while a peer
    completes the very sequence that would make the caller refuse anyway.

    No-op yielding ``True`` on a dialect that has no advisory lock, silently:
    SQLite backs single-writer test runs, not the concurrent workers this fences,
    so there is nothing there to serialize. A bind that cannot be introspected as
    an asynchronous engine also grants — refusing would turn an unrecognised bind
    into a total refusal of the guarded work — but it leaves the caller unfenced
    rather than not needing a fence, so that case is logged.

    :param session: The session whose engine the lock connection is drawn from.
    :param namespace: The lock-space classifier, shared by every caller racing on
        the same class of object. Pairs live in a lock space disjoint from
        single-``bigint`` locks, so a namespace cannot collide with one of those.
    :param key: The key identifying the locked object within ``namespace``.
    :return: ``True`` while the lock is held for the duration of the block.
    :raises SQLAlchemyError: If the lock connection cannot be opened or the lock
        statement fails — pool-independent, but not failure-free: the server can
        refuse the connection or drop it. Raised before the block is entered, so
        the guarded sequence does not run under a lock it does not hold.
    """
    async with _advisory_lock_connection(session, namespace, key) as connection:
        if connection is None:
            yield True
            return
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
            {"namespace": namespace, "key": key},
        )
        yield bool(acquired)


@asynccontextmanager
async def pg_advisory_xact_lock(
    session: AsyncSession,
    namespace: int,
    key: int,
    *,
    shared: bool = False,
    max_wait: timedelta,
) -> AsyncIterator[AdvisoryLockHandle]:
    """Hold a shared or exclusive advisory lock, waiting at most ``max_wait``.

    The waiting sibling of :func:`try_pg_advisory_xact_lock`, on the same kind
    of connection for the same reasons. That one refuses on contention because
    its caller would refuse anyway; this one waits, because here the wait *is*
    the point — a caller queued behind a conflicting holder runs only once that
    holder's whole block has finished. Shared holders never wait for each other,
    only for an exclusive request, held or queued — PostgreSQL queues a new
    request behind a waiting one it conflicts with, so a stream of shared
    holders cannot starve an exclusive one. An exclusive request waits for
    every holder.

    The lock connection sits idle in its transaction while the guarded work runs
    elsewhere, so the server's ``idle_in_transaction_session_timeout`` is turned
    off on it: that timeout would end the transaction and release the lock with
    nothing in the holder failing. Other causes remain — an operator terminating
    the backend, a lost network — which is what the yielded handle's
    :meth:`AdvisoryLockHandle.verify` detects.

    No-op on a dialect without advisory locks and on a bind that cannot be
    introspected, exactly as :func:`try_pg_advisory_xact_lock` grants there: the
    block runs at once, and the handle's ``verify`` passes.

    The lock function's name is interpolated into the statement because the
    shared and exclusive forms are distinct functions; it is one of two
    literals chosen by ``shared``, never input.

    :param session: The session whose engine the lock connection is drawn from.
    :param namespace: The lock-space classifier, shared by every caller racing on
        the same class of object.
    :param key: The key identifying the locked object within ``namespace``.
    :param shared: Whether to take the lock shared rather than exclusive.
    :param max_wait: The longest to wait for the lock to be granted.
    :return: A handle for checking the lock is still held.
    :raises AdvisoryLockTimeoutError: If the lock is not granted within
        ``max_wait``. Raised before the block is entered.
    :raises SQLAlchemyError: If the lock connection cannot be opened or a lock
        statement fails for any other reason. Raised before the block is entered.
    """
    async with _advisory_lock_connection(session, namespace, key) as connection:
        if connection is None:
            yield AdvisoryLockHandle(None)
            return
        await connection.execute(
            text("SELECT set_config('idle_in_transaction_session_timeout', '0', true)")
        )
        await connection.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{int(max_wait.total_seconds() * 1000)}ms"},
        )
        function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        try:
            await connection.execute(
                text(f"SELECT {function}(:namespace, :key)"),
                {"namespace": namespace, "key": key},
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE_SQLSTATE:
                raise AdvisoryLockTimeoutError(
                    f"Advisory lock {namespace}/{key} not granted within {max_wait}."
                ) from exc
            raise
        yield AdvisoryLockHandle(connection)


def table_exists(bind: Connection, table_name: str) -> bool:
    """Return whether ``table_name`` is present on the bound database.

    Enum-widening downgrades need this as a separate preflight from
    :func:`check_constraint_lists_members`. That helper collapses "the table is
    gone" and "the member is not listed" into a single ``False``, which reads
    correctly for a narrowing guard (``if not lists_members: return``) but
    inverts for a widening one (``if lists_members: return``) — there, a missing
    table falls through into DDL against a table another track already dropped.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param table_name: The table to test for.
    :return: ``True`` when the table exists.
    """
    return inspect(bind).has_table(table_name)


def column_exists(bind: Connection, table_name: str, column_name: str) -> bool:
    """Return whether ``table_name.column_name`` is present on the bound database.

    The idempotent add-column guards need this the way the enum-widening guards
    need :func:`table_exists`: on a shared PostgreSQL schema the second track
    must see the column the first track already added. A missing table reports
    ``False`` so a guard reading ``if column_exists(...): return`` still falls
    through to its own :func:`table_exists` preflight.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param table_name: The table owning the column.
    :param column_name: The column to test for.
    :return: ``True`` when the table exists and declares the column.
    """
    inspector = inspect(bind)
    if not inspector.has_table(table_name):
        return False
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _check_constraints_for_column(
    bind: Connection,
    table_name: str,
    column_name: str,
) -> list[ReflectedCheckConstraint]:
    """Return CHECK constraints whose SQL text mentions ``column_name``.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param table_name: The table whose CHECK constraints are inspected.
    :param column_name: The constrained column, used to select the relevant
        constraint and avoid matching unrelated CHECKs.
    :return: Matching inspector constraint dicts, or an empty list when the
        table does not exist.
    """
    inspector = inspect(bind)
    if not inspector.has_table(table_name):
        return []
    return [
        constraint
        for constraint in inspector.get_check_constraints(table_name)
        if column_name in (constraint["sqltext"] or "")
    ]


def check_constraint_name(
    bind: Connection,
    table_name: str,
    column_name: str,
) -> str | None:
    """Return the name of the CHECK constraint on ``column_name``, if any.

    Lets a migration that adds or drops a column's CHECK constraint detect
    whether one is already in place and no-op, which is what makes the
    operation replayable on a database another track has already migrated.

    Raises when more than one CHECK mentions ``column_name`` so a schema
    drift fails fast instead of returning an arbitrary inspector-ordered
    name that a drop migration might apply to the wrong constraint.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param table_name: The table whose CHECK constraints are inspected.
    :param column_name: The constrained column.
    :return: The constraint name, or ``None`` when the table or constraint is
        absent.
    :raises RuntimeError: If more than one CHECK constraint's SQL text
        mentions ``column_name``.
    """
    constraints = _check_constraints_for_column(bind, table_name, column_name)
    if not constraints:
        return None
    if len(constraints) > 1:
        names = [constraint.get("name") for constraint in constraints]
        raise RuntimeError(
            f"Expected at most one CHECK constraint mentioning "
            f"{column_name!r} on {table_name!r}, found {len(constraints)}: "
            f"{names}"
        )
    return constraints[0].get("name")


def check_constraint_lists_members(
    bind: Connection,
    table_name: str,
    column_name: str,
    members: Iterable[str],
) -> bool:
    """Return ``True`` when the CHECK constraint on ``column_name`` lists every member.

    The ``setting_class`` column's allowed values lived in a ``CHECK``
    constraint rather than a PostgreSQL ``TYPE``, because the column used
    ``native_enum=False``. This reflects the constraint text cross-dialect via
    ``sqlalchemy.inspect`` and tests membership by matching each value as a
    single-quoted SQL string literal, so ``"SETTINGS"`` does not spuriously
    match ``"SEP_SETTINGS"``. The historical enum-widening and enum-narrowing
    revisions still consult this helper to decide whether their own DDL has
    already been applied.

    Returns ``False`` when the table does not exist. ``get_check_constraints``
    raises ``NoSuchTableError`` for a missing table, and a missing table means
    there is no constraint to list anything.

    That single ``False`` carries two meanings, so it only short-circuits DDL
    for a guard written ``if not lists_members: return`` (enum widening on
    upgrade, narrowing on downgrade). A guard with the opposite polarity —
    ``if lists_members: return``, as enum *narrowing* needs on upgrade and its
    widening downgrade needs in reverse — falls through on a missing table and
    runs DDL against it. Those call sites must precede this check with
    :func:`table_exists`.

    :param bind: The migration's bound connection (``op.get_bind()``).
    :param table_name: The table whose CHECK constraints are inspected.
    :param column_name: The constrained column, used to select the relevant
        constraint and avoid matching unrelated CHECKs.
    :param members: The enum member names to test for.
    :return: ``True`` only if the table exists and every member appears as a
        quoted literal in a CHECK constraint referencing ``column_name``.
    """
    haystack = " ".join(
        constraint["sqltext"] or ""
        for constraint in _check_constraints_for_column(bind, table_name, column_name)
    )
    return bool(haystack) and all(
        re.search(rf"'{re.escape(member)}'", haystack) for member in members
    )
