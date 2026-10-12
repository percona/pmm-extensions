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

"""Shared fixtures for the Tasks-track migration tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import URL
from sqlalchemy.engine import make_url

from app.core.db.config import DatabaseOptions
from app.core.utils.fields import AsyncDatabaseEngine
from app.tasks.config import tasks_settings
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.conftest import (
    POSTGRES_DSN_ENV,
    postgres_dsn_or_skip,
    postgres_worker_schema,
)
from tests.app.inventory.migrations.postgres_support import recreate_database

#: The revision that attaches the ``taskhistory.status`` CHECK. The SQLite and
#: real-PostgreSQL halves of that revision's coverage must land on the same
#: revision so their assertions mean the same thing.
TASKHISTORY_STATUS_CONSTRAINT_REVISION = "9c1d4f7a6b32"

#: The head immediately before the CHECK constraint is added.
TASKHISTORY_STATUS_PRE_CONSTRAINT_REVISION = "b5e17f6b3bc7"

#: Name SQLAlchemy derives for the enum's CHECK from the model side.
TASKHISTORY_STATUS_CONSTRAINT = "taskhistorystatusenum"

#: A status name no member of the enum carries, so only the CHECK rejects it.
TASKHISTORY_STATUS_OUT_OF_ENUM = "ABANDONED"

#: PostgreSQL ``NAMEDATALEN - 1`` — identifiers longer than this are truncated
#: by the server, which would make CREATE/DROP target a different name than
#: the URL the tests connect with.
_PG_IDENT_MAX = 63


class _DatabaseUrlOverride:
    """Expose a fixed ``URL`` while proxying every other attribute.

    ``DatabaseOptions.URL`` rebuilds from HOST/USER/PASSWORD/NAME and drops
    query parameters (``sslmode``, …). Alembic's ``env.py`` connects through
    that property, while the test engines use the full ``asyncpg`` URL, so the
    two must be forced onto the same string.
    """

    __slots__ = ("_database", "_url")

    def __init__(self, database: DatabaseOptions, url: str) -> None:
        object.__setattr__(self, "_database", database)
        object.__setattr__(self, "_url", url)

    def __getattr__(self, name: str) -> object:
        # ``URL`` must match ``DatabaseOptions.URL`` (uppercase); served via
        # ``__getattr__`` so N802 does not fire on a property definition.
        if name == "URL":
            return object.__getattribute__(self, "_url")
        return getattr(object.__getattribute__(self, "_database"), name)


def _tasks_worker_database_name(maintenance_url: URL) -> str:
    """Return a per-worker database name that fits PostgreSQL's identifier limit.

    :param maintenance_url: The DSN URL used for ``CREATE``/``DROP DATABASE``.
    :return: A database name unique to this xdist worker.
    """
    if not maintenance_url.database:
        pytest.fail(
            f"{POSTGRES_DSN_ENV} must include a database name "
            "(maintenance connection for CREATE/DROP DATABASE)"
        )
    # Short fixed prefix + worker token: independent of the (possibly long) DSN
    # database name, and unique across xdist workers.
    name = f"tasks_{postgres_worker_schema()}"
    if len(name) > _PG_IDENT_MAX:
        name = name[:_PG_IDENT_MAX]
    return name


@pytest.fixture
def tasks_alembic_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, str]:
    """Return an Alembic ``Config`` and sync URL pointing at a temp SQLite file."""
    db_path = tmp_path / "test_tasks.sqlite"
    sync_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(tasks_settings.DATABASE, "HOST", "")
    monkeypatch.setattr(tasks_settings.DATABASE, "NAME", str(db_path))

    cfg = Config(str(ALEMBIC_INI), ini_section="tasks")
    return cfg, sync_url


@pytest.fixture
def postgres_async_url() -> URL:
    """Return an ``asyncpg`` URL to the real-PostgreSQL test database.

    Skip when ``$EXTENSIONS_TEST_POSTGRES_DSN`` is unset (local runs without
    PostgreSQL); the dedicated ``test_postgres`` CI job supplies it.
    """
    return make_url(postgres_dsn_or_skip()).set(drivername="postgresql+asyncpg")


@pytest.fixture
def tasks_postgres_config(
    postgres_async_url: URL, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Config, URL]]:
    """Point the tasks track at a per-worker PostgreSQL database.

    ``command.upgrade`` builds its own engine inside the track's ``env.py`` from
    ``tasks_settings.DATABASE`` rather than accepting one, so the settings are
    what must be redirected. That engine takes no ``schema_translate_map``, so
    the per-worker *schema* the other PostgreSQL fixtures use cannot isolate it;
    a database per xdist worker does, and is dropped on teardown.

    The yielded ``asyncpg`` URL is also installed as ``DATABASE.URL``, so Alembic
    and the test-side engines share one connection string — including query
    parameters the field-wise ``DatabaseOptions.URL`` rebuild would drop.

    :return: The Alembic config and the ``asyncpg`` URL of the worker database.
    """
    worker_name = _tasks_worker_database_name(postgres_async_url)
    worker_url = postgres_async_url.set(database=worker_name)
    recreate_database(postgres_async_url, worker_name)
    database = tasks_settings.DATABASE
    monkeypatch.setattr(database, "ENGINE", AsyncDatabaseEngine.POSTGRESQL)
    monkeypatch.setattr(database, "USER", postgres_async_url.username)
    monkeypatch.setattr(
        database,
        "PASSWORD",
        SecretStr(postgres_async_url.password) if postgres_async_url.password else None,
    )
    monkeypatch.setattr(database, "HOST", postgres_async_url.host)
    monkeypatch.setattr(database, "PORT", postgres_async_url.port)
    monkeypatch.setattr(database, "NAME", worker_name)
    monkeypatch.setattr(
        tasks_settings,
        "DATABASE",
        _DatabaseUrlOverride(
            database, worker_url.render_as_string(hide_password=False)
        ),
    )

    cfg = Config(str(ALEMBIC_INI), ini_section="tasks")
    try:
        yield cfg, worker_url
    finally:
        recreate_database(postgres_async_url, worker_name, create=False)
