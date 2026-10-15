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

"""Test the ``node.source`` enum conversion against real PostgreSQL.

SQLite has no native enum, so only PostgreSQL can show the ``sourceenum`` type
going away on upgrade and coming back on downgrade, with the CHECK taking over
its job in between.
"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.app.inventory.migrations.conftest import (
    EXTERNAL_ID_INDEXES_PRE_REVISION,
    EXTERNAL_ID_INDEXES_REVISION,
    SEED_TIMESTAMPS,
)
from tests.app.inventory.migrations.postgres_support import run_on_postgres

pytestmark = pytest.mark.postgres


async def _insert_node(conn, source):
    """Insert a ``node`` row carrying ``source`` as a SQL literal."""
    await conn.execute(
        text(
            "INSERT INTO node (created_at, updated_at, address, name, external_id, "
            f"source, type, retirement_key) VALUES ({SEED_TIMESTAMPS}, '10.0.0.1', "
            f"'n', 'n', '{source}', 'generic', -1)"
        )
    )


async def _type_exists(conn):
    """Return whether the ``sourceenum`` type exists."""
    result = await conn.execute(
        text("SELECT count(*) FROM pg_type WHERE typname = 'sourceenum'")
    )
    return result.scalar_one() == 1


async def _source_column_type(conn):
    """Return ``information_schema``'s data type for ``node.source``."""
    result = await conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'node' AND column_name = 'source'"
        )
    )
    return result.scalar_one()


async def _stored_source(conn):
    """Return the one stored ``node.source`` value as text."""
    result = await conn.execute(text("SELECT source::text FROM node"))
    return result.scalar_one()


def test_upgrade_replaces_the_type_with_a_check(inventory_postgres_config):
    """Drop ``sourceenum``, keep the stored label, and reject a foreign one."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, EXTERNAL_ID_INDEXES_PRE_REVISION)
    run_on_postgres(url, lambda conn: _insert_node(conn, "PMM"))

    command.upgrade(cfg, EXTERNAL_ID_INDEXES_REVISION)

    assert run_on_postgres(url, _type_exists) is False
    assert run_on_postgres(url, _source_column_type) == "character varying"
    assert run_on_postgres(url, _stored_source) == "PMM"
    with pytest.raises(IntegrityError):
        run_on_postgres(url, lambda conn: _insert_node(conn, "XYZ"))


def test_downgrade_restores_the_native_type(inventory_postgres_config):
    """Recreate ``sourceenum`` and cast the stored label back into it."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, EXTERNAL_ID_INDEXES_REVISION)
    run_on_postgres(url, lambda conn: _insert_node(conn, "PMM"))

    command.downgrade(cfg, EXTERNAL_ID_INDEXES_PRE_REVISION)

    assert run_on_postgres(url, _type_exists) is True
    assert run_on_postgres(url, _source_column_type) == "USER-DEFINED"
    assert run_on_postgres(url, _stored_source) == "PMM"
