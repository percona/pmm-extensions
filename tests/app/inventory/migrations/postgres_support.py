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

"""Share the real-PostgreSQL plumbing of the Inventory-track migration tests.

Everything runs over ``asyncpg``: the inventory track's ``env.py`` builds its own
async engine, and the ``test_postgres`` CI job installs the ``postgresql`` group
only, so no sync driver is available to lean on.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from pydantic import SecretStr
from sqlalchemy import text, URL
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.db.config import DatabaseOptions
from app.core.utils.fields import AsyncDatabaseEngine

T = TypeVar("T")


def run_on_postgres(
    url: URL, coroutine_factory: Callable[[AsyncConnection], Awaitable[T]]
) -> T:
    """Run ``coroutine_factory`` in one transaction on a fresh async engine.

    :param url: The ``asyncpg`` URL of the database to connect to.
    :param coroutine_factory: The work to run against the open connection.
    :return: Whatever ``coroutine_factory`` returned.
    """

    async def _run() -> T:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await coroutine_factory(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def point_database_at(
    monkeypatch: pytest.MonkeyPatch, database: DatabaseOptions, url: URL, name: str
) -> None:
    """Redirect a track's ``DATABASE`` settings to database ``name`` on ``url``'s server.

    A track's ``env.py`` builds its engine from these settings rather than
    accepting one, so patching them is how a migration test reaches PostgreSQL.

    :param monkeypatch: The fixture that undoes the patches on teardown.
    :param database: The track's ``DATABASE`` settings object.
    :param url: The ``asyncpg`` URL whose credentials, host and port to use.
    :param name: The database to point at.
    """
    monkeypatch.setattr(database, "ENGINE", AsyncDatabaseEngine.POSTGRESQL)
    monkeypatch.setattr(database, "USER", url.username)
    monkeypatch.setattr(
        database, "PASSWORD", SecretStr(url.password) if url.password else None
    )
    monkeypatch.setattr(database, "HOST", url.host)
    monkeypatch.setattr(database, "PORT", url.port)
    monkeypatch.setattr(database, "NAME", name)


def recreate_database(url: URL, name: str, *, create: bool = True) -> None:
    """Drop the database ``name`` if it exists, then create it afresh.

    ``CREATE DATABASE`` refuses to run inside a transaction, so this connects
    in autocommit rather than through :func:`run_on_postgres`.

    :param url: The ``asyncpg`` URL of any database on the same server.
    :param name: The database to recreate.
    :param create: Whether to create it again; ``False`` only drops it.
    """

    async def _run() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                )
                if create:
                    await conn.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()

    asyncio.run(_run())
