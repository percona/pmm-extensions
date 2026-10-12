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

from app.core.utils.fields import AsyncDatabaseEngine
from app.tasks.config import tasks_settings
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.conftest import postgres_dsn_or_skip, postgres_worker_schema
from tests.app.inventory.migrations.postgres_support import recreate_database


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

    :return: The Alembic config and the ``asyncpg`` URL of the worker database.
    """
    worker_url = postgres_async_url.set(
        database=f"{postgres_async_url.database}_{postgres_worker_schema()}"
    )
    recreate_database(postgres_async_url, worker_url.database)
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
    monkeypatch.setattr(database, "NAME", worker_url.database)

    cfg = Config(str(ALEMBIC_INI), ini_section="tasks")
    try:
        yield cfg, worker_url
    finally:
        recreate_database(postgres_async_url, worker_url.database, create=False)
