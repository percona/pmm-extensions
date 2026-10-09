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

"""Define exception handlers mapping database capacity failures to HTTP 503."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.core.exceptions import HTTPServiceUnavailableException

#: asyncpg's capacity-error base class, or ``None`` where asyncpg is absent. It
#: ships in the ``postgresql`` Poetry group, which is optional, so a SQLite-only
#: install has nothing to import here and must still start: an unconditional
#: import would fail every such deployment at application import.
_ASYNCPG_CAPACITY_ERROR: type[Exception] | None
try:
    from asyncpg.exceptions import InsufficientResourcesError
except ImportError:
    _ASYNCPG_CAPACITY_ERROR = None
else:
    _ASYNCPG_CAPACITY_ERROR = InsufficientResourcesError

logger = logging.getLogger(__name__)

#: The response a capacity failure renders. Pre-instantiated like
#: :data:`app.core.auth.exceptions.InactiveUserException` because the payload is
#: fixed. The wording names no resource on purpose: the classes below span
#: connections, memory and disk, so anything more specific would be wrong for
#: some of them, and which one ran out is the operator's business, not the
#: client's.
_CAPACITY_UNAVAILABLE = HTTPServiceUnavailableException(
    detail="The database is temporarily unavailable. Please retry.",
)


async def db_capacity_exception_handler(
    request: Request,  # noqa: ARG001
    exc: Exception,
) -> JSONResponse:
    """Return a 503 for a database that cannot serve the request right now.

    :param request: The incoming request, which the fixed payload does not
        consult.
    :param exc: The capacity failure to report.
    :return: A JSON response carrying the service-unavailable detail.
    """
    logger.error("Database capacity exhausted:", exc_info=exc)
    return JSONResponse(
        {"detail": _CAPACITY_UNAVAILABLE.detail},
        status_code=_CAPACITY_UNAVAILABLE.status_code,
    )


def _wraps_capacity_failure(exc: BaseException | None) -> bool:
    """Report whether a capacity failure sits in ``exc``'s cause chain.

    The dialect raises its own DBAPI error *from* the asyncpg one, so what
    SQLAlchemy hands over as ``orig`` is the translated shim and the driver
    exception is a link further down ``__cause__``. Walking the chain also
    covers the shape where ``orig`` is the asyncpg exception itself.

    :param exc: The exception to walk, or ``None``.
    :return: Whether any link is an asyncpg capacity failure.
    """
    if _ASYNCPG_CAPACITY_ERROR is None:
        return False
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, _ASYNCPG_CAPACITY_ERROR):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


async def db_wrapped_capacity_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return a 503 when SQLAlchemy wrapped a capacity failure, else re-raise.

    A capacity failure the server raises while *executing* a statement — out of
    memory, disk full, a configuration limit — does not reach the app raw. The
    asyncpg dialect's ``_handle_exception`` walks the exception's MRO against
    ``_asyncpg_error_translate``, matches ``asyncpg.exceptions.PostgresError``,
    and re-raises its own DBAPI error, which SQLAlchemy then wraps in
    :class:`sqlalchemy.exc.DBAPIError` with the driver exception on ``orig``.
    Only a *connect*-time refusal escapes that path unwrapped, which is why the
    raw class alone does not cover the resource classes this maps.

    Re-raising for anything else is what keeps this narrow: an ordinary
    ``DBAPIError`` stays on the 500 path it takes today.

    :param request: The incoming request, forwarded to the shared handler.
    :param exc: The wrapper exception to inspect.
    :return: A JSON response carrying the service-unavailable detail.
    :raises Exception: ``exc`` itself, when it does not wrap a capacity failure.
    """
    if not _wraps_capacity_failure(getattr(exc, "orig", None)):
        raise exc
    return await db_capacity_exception_handler(request, exc)


def register_db_capacity_handlers(app: FastAPI) -> None:
    """Register the capacity-to-503 mapping on ``app``.

    The handlers are keyed on the exception class rather than on status ``500``
    for two reasons. Starlette resolves status-code handlers only for a
    :class:`starlette.exceptions.HTTPException`, and neither class below is
    one: SQLAlchemy leaves a server-side refusal unwrapped, so the raw driver
    exception reaches the app, and its own pool timeout is a plain
    ``SQLAlchemyError``. Class handlers also live in Starlette's inner
    ``ExceptionMiddleware``, which sees an exception before the outer
    ``ServerErrorMiddleware`` a sub-application's own ``500`` handler is
    lifted into, so this mapping wins over one of those.

    Three registrations cover the three routes one capacity failure can arrive
    on: ``InsufficientResourcesError`` raw, :class:`sqlalchemy.exc.DBAPIError`
    wrapping it, and :class:`sqlalchemy.exc.TimeoutError` for this process's
    own saturated pool. Each handler documents the shape it takes.

    :param app: The FastAPI application to register the handlers on.
    """
    app.add_exception_handler(SQLAlchemyTimeoutError, db_capacity_exception_handler)
    if _ASYNCPG_CAPACITY_ERROR is not None:
        app.add_exception_handler(
            _ASYNCPG_CAPACITY_ERROR, db_capacity_exception_handler
        )
        app.add_exception_handler(DBAPIError, db_wrapped_capacity_exception_handler)
