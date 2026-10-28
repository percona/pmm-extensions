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

"""Provide connectivity check helpers and an async LRU cache for task creation.

These are the shared primitives — the memoized probe. The request-facing
wrapper that turns a probe into a ``ConnectivityWarning | None`` lives in
:mod:`app.extensions.apps.framework.connectivity`.
"""

import logging
from typing import Any, cast, NamedTuple

from aiohttp import ClientError
from async_lru import alru_cache
from fastapi import HTTPException

from app.core.requests import RemoteAPI

logger = logging.getLogger(__name__)

CACHE_TTL = 600
#: Connect budget (seconds) sent to the Tasks API as ``request.timeout``. The
#: poll loop charges only post-provisioning connect time against it; the inner
#: DB ``connect_timeout`` (the payload's ``CONNECT_TIMEOUT``) must stay strictly
#: below it.
CHECK_TIMEOUT = 20
CACHE_MAXSIZE = 128

CONNECTIVITY_META_HOST_KEY = "_connectivity_host"
CONNECTIVITY_META_PORT_KEY = "_connectivity_port"
CONNECTIVITY_META_SERVICE_TYPE_KEY = "_connectivity_service_type"


class ConnectivityResult(NamedTuple):
    """Hold the outcome of a Tasks API connectivity check.

    :param success: Whether the database connect succeeded.
    :param error: A failure description, or ``None`` on success.
    :param task_history_id: The run-script task-history id whose log explains
        the result, or ``None`` when no task was created (e.g. the Tasks API
        was unreachable).
    """

    success: bool
    error: str | None
    task_history_id: int | None


def clear_connectivity_caches() -> None:
    """Reset the connectivity ``alru_cache``.

    Provide a single public entry point for tests to wipe the ``alru_cache`` on
    :func:`_fetch_connectivity_result`. Production code does not need this —
    ``alru_cache`` honors its own TTL.

    :return: ``None``.
    """
    _fetch_connectivity_result.cache_clear()


@alru_cache(maxsize=CACHE_MAXSIZE, ttl=CACHE_TTL)
async def _fetch_connectivity_result(
    tasks_api: RemoteAPI,
    target: str,
    host: str,
    port: int,
    service_type: str,
) -> ConnectivityResult:
    """Run the Tasks API connectivity check and return a ``ConnectivityResult``.

    Memoize results by ``(tasks_api, target, host, port, service_type)`` so
    repeated checks within ``CACHE_TTL`` seconds skip the API roundtrip.
    ``RemoteAPI`` is hashable, so it participates in the cache key directly
    without any contextvar indirection.

    ``task_history_id`` carries the underlying run-script task id so callers can
    link its log; it is ``None`` on transport errors where no task was created.
    Because the result is memoized, a repeat check within ``CACHE_TTL`` reuses
    the first call's ``task_history_id`` — its "View log" link points at that
    earlier run rather than a fresh one. This is acceptable: the cache key pins
    identical check parameters, so the earlier log stays representative.

    :param tasks_api: Authenticated Tasks API client.
    :param target: The Nomad node name.
    :param host: The database host address.
    :param port: The database port.
    :param service_type: The lowercase service type (e.g. ``mysql``).
    :return: A :class:`ConnectivityResult`; ``error`` is ``None`` on success and
        ``task_history_id`` is ``None`` when no task was created.
    """
    try:
        result = cast(
            "dict[str, Any]",
            await tasks_api.post(
                "/connectivity-check/",
                json={
                    "target": target,
                    "host": host,
                    "port": port,
                    "service_type": service_type,
                    "timeout": CHECK_TIMEOUT,
                },
            ),
        )
        return ConnectivityResult(
            result.get("success", False),
            result.get("error"),
            result.get("task_history_id"),
        )
    except HTTPException as exc:
        logger.debug(
            "Tasks API connectivity check returned error response", exc_info=True
        )
        error = (
            exc.detail if isinstance(exc.detail, str) else "Connectivity check failed"
        )
        return ConnectivityResult(success=False, error=error, task_history_id=None)
    except (OSError, TimeoutError, ClientError):
        logger.debug("Could not reach Tasks API for connectivity check", exc_info=True)
        return ConnectivityResult(
            success=False, error="Could not reach the Tasks API", task_history_id=None
        )
