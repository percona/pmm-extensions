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

"""Test the migration that adds ``last_error_code`` to OM's host and service rows.

The revision creates its CHECK constraint itself rather than through the column's
type, so that each engine gets exactly one, and its downgrade drops the constraint
before the column. Both halves differ by dialect: SQLite rebuilds the table to add
a constraint, PostgreSQL adds it in place. SQLite is checked on every run, and
PostgreSQL where the test job supplies one.
"""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url, URL
from sqlalchemy.exc import IntegrityError

from app.core.utils.fields import AsyncDatabaseEngine
from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.conftest import postgres_dsn_or_skip

REVISION = "309bd5bdeec9"
PREVIOUS = "a3f1c8d24b71"
TABLES = ("om_host", "om_service")
COLUMN = "last_error_code"


def _table(table: str) -> str:
    """Name a table as raw SQL has to, with the schema OM's tables resolve to.

    :param table: The table.
    :return: Its name, schema-qualified where OM has a schema of its own.
    """
    schema = om_schema(extensions_settings.DATABASE)
    return f"{schema}.{table}" if schema else table


def _shape(url: str | URL) -> dict[str, tuple[bool, int]]:
    """Report, per table, whether it has the column and how many CHECKs name it.

    :param url: The database.
    :return: ``(has the column, CHECKs on it)`` by table.
    """
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        schema = om_schema(extensions_settings.DATABASE)
        return {
            table: (
                COLUMN
                in {
                    column["name"]
                    for column in inspector.get_columns(table, schema=schema)
                },
                sum(
                    COLUMN in check["sqltext"]
                    for check in inspector.get_check_constraints(table, schema=schema)
                ),
            )
            for table in TABLES
        }
    finally:
        engine.dispose()


def _insert_host(url: str | URL, node_id: str, code: str) -> None:
    """Insert a host row carrying ``code``.

    :param url: The database.
    :param node_id: The row's key.
    :param code: The ``last_error_code`` to store.
    """
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {_table('om_host')} "
                    f"(node_id, name, first_seen_at, updated_at, {COLUMN}) "
                    "VALUES (:node_id, 'db00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                    ":code)"
                ),
                {"node_id": node_id, "code": code},
            )
    finally:
        engine.dispose()


def _round_trip(config: Config, url: str | URL) -> None:
    """Upgrade, downgrade and upgrade again, checking the shape at each step.

    :param config: The extensions Alembic track.
    :param url: The database it migrates.
    """
    one_check = dict.fromkeys(TABLES, (True, 1))

    command.upgrade(config, REVISION)
    assert _shape(url) == one_check
    _insert_host(url, "n-1", "scan_crashed")
    with pytest.raises(IntegrityError):
        _insert_host(url, "n-2", "not_a_failure_kind")

    command.downgrade(config, PREVIOUS)
    assert _shape(url) == dict.fromkeys(TABLES, (False, 0))

    command.upgrade(config, REVISION)
    assert _shape(url) == one_check


def test_the_revision_round_trips_on_sqlite(
    extensions_alembic_config: tuple[Config, str],
) -> None:
    """Add, drop and re-add the column and its one CHECK on SQLite.

    :param extensions_alembic_config: The track, on a temporary SQLite file.
    """
    config, url = extensions_alembic_config

    _round_trip(config, url)


@pytest.fixture
def extensions_postgres_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    """Point the extensions Alembic track at the real test PostgreSQL database.

    ``command.upgrade`` builds its own engine from ``extensions_settings.DATABASE``
    through the track's ``env.py``, so redirecting the settings is what moves the
    migration off SQLite. The schema is dropped on teardown so the tables do not
    leak into sibling tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :return: The sync URL of the database migrated.
    """
    url = make_url(postgres_dsn_or_skip()).set(drivername="postgresql+psycopg2")
    database = extensions_settings.DATABASE
    monkeypatch.setattr(database, "ENGINE", AsyncDatabaseEngine.POSTGRESQL)
    monkeypatch.setattr(database, "USER", url.username)
    monkeypatch.setattr(
        database, "PASSWORD", SecretStr(url.password) if url.password else None
    )
    monkeypatch.setattr(database, "HOST", url.host)
    monkeypatch.setattr(database, "PORT", url.port)
    monkeypatch.setattr(database, "NAME", url.database)
    try:
        yield url
    finally:
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA public CASCADE")
                connection.exec_driver_sql("CREATE SCHEMA public")
        finally:
            engine.dispose()


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_the_revision_round_trips_on_postgresql(extensions_postgres_db: URL) -> None:
    """Add, drop and re-add the column and its one CHECK on PostgreSQL.

    PostgreSQL is where the type would have emitted a second CHECK from ``ADD
    COLUMN`` had the revision left ``create_constraint`` on.

    :param extensions_postgres_db: The real test database.
    """
    _round_trip(
        Config(str(ALEMBIC_INI), ini_section="extensions"), extensions_postgres_db
    )
