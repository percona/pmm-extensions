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

"""Shared error mapping for PMM Extensions gateway proxies to the Tasks sub-app."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn

from fastapi import HTTPException, status

from app.core.exceptions import HTTPBadGatewayException
from app.core.utils.exceptions import exception_detail


def reraise_upstream_tasks_error(exc: HTTPException | OSError) -> NoReturn:
    """Map an upstream Tasks-API failure onto the PMM Extensions gateway error contract.

    Re-raise upstream **client** errors (HTTP < 500 -- ``400`` / ``404`` /
    ``409`` / ``422``) unchanged so their status and ``detail`` survive the proxy
    and the React SPA can render inline validation / conflict messages. Map
    upstream **availability** failures (HTTP >= 500, or an ``OSError`` connection
    failure) onto :class:`~app.core.exceptions.HTTPBadGatewayException` (``502``).

    :param exc: The upstream failure raised by a :class:`RemoteAPI` call -- an
        ``HTTPException`` (an upstream non-2xx response) or an ``OSError`` (a
        connection-level failure).
    :raises HTTPException: Re-raised unchanged for an upstream client error
        (status < 500).
    :raises HTTPBadGatewayException: For an upstream server error (status >= 500)
        or a connection-level ``OSError``.
    """
    if (
        isinstance(exc, HTTPException)
        and exc.status_code < status.HTTP_500_INTERNAL_SERVER_ERROR
    ):
        raise exc.with_traceback(exc.__traceback__)
    raise HTTPBadGatewayException(detail=exception_detail(exc)) from exc


@contextmanager
def reraise_upstream_tasks_errors() -> Iterator[None]:
    """Route upstream Tasks-API failures raised in the block onto the gateway contract.

    Wrap an upstream ``tasks_api`` call so an ``HTTPException`` or ``OSError`` it
    raises is mapped through :func:`reraise_upstream_tasks_error`; any other
    exception propagates unchanged.

    :yield: Control to the wrapped block; the block runs inside the try, and any
        ``HTTPException`` / ``OSError`` it raises is routed through the mapper.
    :raises HTTPException: Re-raised unchanged for an upstream client error
        (status < 500).
    :raises HTTPBadGatewayException: For an upstream server error (status >= 500)
        or a connection-level ``OSError``.
    """
    try:
        yield
    except (HTTPException, OSError) as exc:
        reraise_upstream_tasks_error(exc)
