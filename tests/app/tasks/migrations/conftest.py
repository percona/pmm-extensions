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
from sqlalchemy import URL

from app.tasks.config import tasks_settings
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.conftest import postgres_worker_schema
from tests.app.inventory.migrations.postgres_support import (
    point_database_at,
    recreate_database,
)

#: The head immediately before the CHECK constraint is added.
TASKHISTORY_STATUS_PRE_CONSTRAINT_REVISION = "b5e17f6b3bc7"

#: Name SQLAlchemy derives for the enum's CHECK from the model side.
TASKHISTORY_STATUS_CONSTRAINT = "taskhistorystatusenum"

#: A status name no member of the enum carries, so only the CHECK rejects it.
TASKHISTORY_STATUS_OUT_OF_ENUM = "ABANDONED"


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
    worker_name = f"tasks_{postgres_worker_schema()}"
    worker_url = postgres_async_url.set(database=worker_name)
    recreate_database(postgres_async_url, worker_name)
    point_database_at(
        monkeypatch, tasks_settings.DATABASE, postgres_async_url, worker_name
    )

    cfg = Config(str(ALEMBIC_INI), ini_section="tasks")
    try:
        yield cfg, worker_url
    finally:
        recreate_database(postgres_async_url, worker_name, create=False)
