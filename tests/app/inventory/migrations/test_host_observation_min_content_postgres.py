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

"""Test the host-observation minimum-content migration against real PostgreSQL.

The SQLite sibling cannot fail on the two steps most likely to break here.
``batch_alter_table`` recreates the table on SQLite, so the CHECK arrives as
part of a fresh ``CREATE TABLE`` and the plain ``ALTER TABLE ... ADD CONSTRAINT``
this engine takes is never exercised; and SQLite stores an unset JSON fact as
the text ``'null'`` while PostgreSQL stores it in a real ``json`` column, where
``CAST(col AS TEXT) = 'null'`` is the only portable way to recognize it — the
direct comparison the normalization avoids has no operator at all on this
engine.

Everything runs over ``asyncpg``: the inventory track's ``env.py`` builds its own
async engine, and the ``test_postgres`` CI job installs the ``postgresql`` group
only, so no sync driver is available to lean on.
"""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.inventory.models import HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT
from tests.app.inventory.migrations.conftest import (
    HOST_OBSERVATION_PRE_CONSTRAINT_REVISION,
    SEED_TIMESTAMPS,
)
from tests.app.inventory.migrations.postgres_support import run_on_postgres

_OBSERVED_AT = "'2026-01-01 00:00:00+00'"

#: The JSON-typed spelling of an unset fact as SQLAlchemy's ``JSON`` wrote it
#: before the model declared ``none_as_null=True``: a ``json`` value holding the
#: document ``null``, which ``IS NOT NULL`` reports as present.
_JSON_NULL = "CAST('null' AS JSON)"

_CONSTRAINT_PRESENT = (
    "SELECT COUNT(*) FROM pg_constraint "
    "WHERE conname = :name AND contype = 'c' "
    "AND conrelid = 'hostsystemobservation'::regclass"
)

pytestmark = pytest.mark.postgres


async def _insert_node(conn, address, name, external_id):
    """Insert a PMM-sourced ``node`` row and return its primary key."""
    result = await conn.execute(
        text(
            "INSERT INTO node (created_at, updated_at, address, name, external_id, "
            f"source, type, retirement_key) VALUES ({SEED_TIMESTAMPS}, :address, "
            ":name, :external_id, CAST('PMM' AS sourceenum), 'generic', -1) "
            "RETURNING id"
        ),
        {"address": address, "name": name, "external_id": external_id},
    )
    return result.scalar_one()


async def _insert_observation(conn, node_id, os_version, json_facts):
    """Insert a host observation and return its primary key.

    :param conn: The open connection to write through.
    :param node_id: The node the observation belongs to.
    :param os_version: The only fact this row may carry, or ``None``.
    :param json_facts: SQL producing the value for both JSON fact columns —
        ``NULL`` for a genuinely absent fact, ``_JSON_NULL`` for the legacy
        spelling.
    :return: The new row's primary key.
    """
    result = await conn.execute(
        text(
            "INSERT INTO hostsystemobservation (created_at, updated_at, node_id, "
            "os_version, installed_packages, config, can_elevate, observed_at) "
            f"VALUES ({SEED_TIMESTAMPS}, :node_id, :os_version, {json_facts}, "
            f"{json_facts}, NULL, {_OBSERVED_AT}) RETURNING id"
        ),
        {"node_id": node_id, "os_version": os_version},
    )
    return result.scalar_one()


async def _observation_count(conn, node_id):
    """Return how many observation rows the named node still has."""
    result = await conn.execute(
        text("SELECT COUNT(*) FROM hostsystemobservation WHERE node_id = :node_id"),
        {"node_id": node_id},
    )
    return result.scalar_one()


async def _json_facts_are_null(conn, node_id):
    """Report whether both JSON fact columns hold SQL NULL for the node's row."""
    result = await conn.execute(
        text(
            "SELECT installed_packages IS NULL AND config IS NULL "
            "FROM hostsystemobservation WHERE node_id = :node_id"
        ),
        {"node_id": node_id},
    )
    return result.scalar_one()


async def _constraint_count(conn):
    """Return how many CHECKs of the guard's name the table carries."""
    result = await conn.execute(
        text(_CONSTRAINT_PRESENT), {"name": HOST_OBSERVATION_MIN_CONTENT_CONSTRAINT}
    )
    return result.scalar_one()


def test_json_null_fact_less_observation_is_deleted(inventory_postgres_config):
    """Delete a fact-less row whose JSON columns hold the ``json`` value ``null``.

    ``IS NOT NULL`` is true of both columns here, so nothing but the
    normalization exposes the row to the deletion.
    """
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    async def _seed(conn):
        node_id = await _insert_node(conn, "10.0.0.1", "jsonnull", "/node_id/jsonnull")
        await _insert_observation(conn, node_id, None, _JSON_NULL)
        return node_id

    node_id = run_on_postgres(url, _seed)

    command.upgrade(cfg, "heads")

    assert run_on_postgres(url, lambda conn: _observation_count(conn, node_id)) == 0


def test_json_null_facts_beside_a_real_fact_become_sql_null(inventory_postgres_config):
    """Keep a row carrying a real fact and rewrite its JSON nulls to SQL NULL."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    async def _seed(conn):
        node_id = await _insert_node(conn, "10.0.0.2", "mixed", "/node_id/mixed")
        await _insert_observation(conn, node_id, "Ubuntu 24.04", _JSON_NULL)
        return node_id

    node_id = run_on_postgres(url, _seed)

    command.upgrade(cfg, "heads")

    assert run_on_postgres(url, lambda conn: _observation_count(conn, node_id)) == 1
    assert (
        run_on_postgres(url, lambda conn: _json_facts_are_null(conn, node_id)) is True
    )


def test_check_lands_on_the_native_alter_path(inventory_postgres_config):
    """Carry the CHECK after the plain ALTER, and reject a fact-less insert."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, "heads")

    assert run_on_postgres(url, _constraint_count) == 1

    node_id = run_on_postgres(
        url, lambda conn: _insert_node(conn, "10.0.0.3", "fresh", "/node_id/fresh")
    )
    with pytest.raises(IntegrityError):
        run_on_postgres(
            url, lambda conn: _insert_observation(conn, node_id, None, "NULL")
        )


def test_downgrade_drops_the_constraint(inventory_postgres_config):
    """Accept a fact-less insert again once the CHECK has been dropped."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, "heads")
    command.downgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    assert run_on_postgres(url, _constraint_count) == 0

    async def _seed(conn):
        node_id = await _insert_node(conn, "10.0.0.4", "loose", "/node_id/loose")
        await _insert_observation(conn, node_id, None, "NULL")
        return node_id

    node_id = run_on_postgres(url, _seed)
    assert run_on_postgres(url, lambda conn: _observation_count(conn, node_id)) == 1
