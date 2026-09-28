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

"""Shared fixtures for the migration tests that need a real PostgreSQL server."""

from collections.abc import Iterator
from contextlib import ExitStack
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, Engine
from sqlalchemy.engine import make_url, URL

from tests.app.conftest import postgres_dsn_or_skip


@pytest.fixture
def postgres_sync_url() -> URL:
    """Return a sync (``psycopg2``) URL to the real-PostgreSQL test database.

    Skip when ``$EXTENSIONS_TEST_POSTGRES_DSN`` is unset (local runs without
    PostgreSQL); the dedicated ``test_postgres`` CI job supplies it.
    """
    return make_url(postgres_dsn_or_skip()).set(drivername="postgresql+psycopg2")


@pytest.fixture
def postgres_migration_stores(postgres_sync_url: URL) -> Iterator[dict[str, Engine]]:
    """Provision empty databases for each migration track and the beat store.

    :param postgres_sync_url: The test server URL; its role needs CREATEDB.
    :return: Synchronous engines for the isolated stores, dropped after the test.
    """
    admin = create_engine(postgres_sync_url, isolation_level="AUTOCOMMIT")
    stores: dict[str, Engine] = {}
    try:
        with admin.connect() as connection, ExitStack() as cleanup:
            for app in ("tasks", "inventory", "extensions", "beat"):
                name = f"extensions_migrate_{uuid4().hex}_{app}"
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
                cleanup.callback(
                    connection.exec_driver_sql,
                    f'DROP DATABASE "{name}" WITH (FORCE)',
                )
                engine = create_engine(postgres_sync_url.set(database=name))
                cleanup.callback(engine.dispose)
                stores[app] = engine
            yield stores
    finally:
        admin.dispose()
