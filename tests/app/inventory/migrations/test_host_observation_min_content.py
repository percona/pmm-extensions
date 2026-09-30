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

"""Test the Inventory-track minimum-content guard on host system observations.

Rows are inserted through the driver rather than through
``HostSystemObservationWrite``, since the Pydantic validator is exactly the
layer this revision stops relying on.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from tests.app.inventory.migrations.conftest import (
    HOST_OBSERVATION_PRE_CONSTRAINT_REVISION,
)

_INSERT_NODE = (
    "INSERT INTO node "
    "(created_at, updated_at, address, name, external_id, source, type, "
    "retired_at, retirement_key) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', ?, ?, ?, 'PMM', "
    "'generic', NULL, -1)"
)
_INSERT_OBSERVATION = (
    "INSERT INTO hostsystemobservation "
    "(created_at, updated_at, node_id, os_version, installed_packages, config, "
    "can_elevate, observed_at) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', ?, ?, NULL, NULL, "
    "NULL, '2026-01-01 00:00:00')"
)
#: The literals are the JSON text ``null``, not SQL ``NULL``. That is what
#: SQLAlchemy's ``JSON`` wrote for an unset value before the model declared
#: ``none_as_null=True``, so it is the shape a pre-existing row actually has —
#: and it is non-NULL, which is what lets such a row satisfy the CHECK.
_INSERT_JSON_NULL_OBSERVATION = (
    "INSERT INTO hostsystemobservation "
    "(created_at, updated_at, node_id, os_version, installed_packages, config, "
    "can_elevate, observed_at) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', ?, ?, 'null', 'null', "
    "NULL, '2026-01-01 00:00:00')"
)


def _seed_node(conn, address, name, external_id):
    """Insert a ``node`` row and return its primary key."""
    conn.exec_driver_sql(_INSERT_NODE, (address, name, external_id))
    return conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()


def _seed_observation(conn, node_id, os_version):
    """Insert a host observation carrying only ``os_version``, if anything."""
    conn.exec_driver_sql(_INSERT_OBSERVATION, (node_id, os_version))
    return conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()


def _seed_json_null_observation(conn, node_id, os_version):
    """Insert an observation whose two JSON facts hold the JSON text ``null``."""
    conn.exec_driver_sql(_INSERT_JSON_NULL_OBSERVATION, (node_id, os_version))
    return conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()


def _json_fact_types(conn, node_id):
    """Return SQLite's storage class for the row's two JSON fact columns."""
    return conn.exec_driver_sql(
        "SELECT typeof(installed_packages), typeof(config) "
        "FROM hostsystemobservation WHERE node_id = ?",
        (node_id,),
    ).one()


def _observation_count(conn, node_id):
    """Return how many observation rows the named node still has."""
    return conn.exec_driver_sql(
        "SELECT COUNT(*) FROM hostsystemobservation WHERE node_id = ?",
        (node_id,),
    ).scalar_one()


def test_fact_less_observation_is_deleted(inventory_alembic_config, capfd):
    """Delete a pre-existing observation whose every fact column is NULL.

    The warning is read off the captured stream rather than through ``caplog``:
    ``env.py`` runs ``fileConfig`` on ``alembic.ini``, which replaces the root
    handlers ``caplog`` installs with the console handler the ini declares.
    """
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.1", "empty", "/node_id/empty")
            _seed_observation(conn, node_id, None)
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")
    logged = capfd.readouterr().err

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _observation_count(conn, node_id) == 0
    finally:
        engine.dispose()

    assert f"node_id={node_id}" in logged
    assert "observed_at=" in logged


def test_observation_with_a_fact_survives(inventory_alembic_config):
    """Leave an observation carrying at least one fact exactly as it was."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.2", "observed", "/node_id/observed")
            _seed_observation(conn, node_id, "Ubuntu 24.04")
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _observation_count(conn, node_id) == 1
            assert (
                conn.exec_driver_sql(
                    "SELECT os_version FROM hostsystemobservation WHERE node_id = ?",
                    (node_id,),
                ).scalar_one()
                == "Ubuntu 24.04"
            )
    finally:
        engine.dispose()


def test_fact_less_insert_is_rejected_after_upgrade(inventory_alembic_config):
    """Reject a fact-less insert at the database once the CHECK is in place."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.3", "fresh", "/node_id/fresh")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _seed_observation(conn, node_id, None)
    finally:
        engine.dispose()


def test_downgrade_drops_the_constraint(inventory_alembic_config):
    """Accept a fact-less insert again once the CHECK has been dropped."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, "heads")
    command.downgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.4", "loose", "/node_id/loose")
            _seed_observation(conn, node_id, None)
            assert _observation_count(conn, node_id) == 1
    finally:
        engine.dispose()


def test_json_null_fact_less_observation_is_deleted(inventory_alembic_config, capfd):
    """Delete a fact-less observation whose JSON columns hold JSON ``null``.

    This is the production shape: ``installed_packages`` and ``config`` are
    non-NULL text, so the row escapes an ``IS NULL`` scan and would satisfy the
    CHECK, while carrying no observed fact whatsoever.
    """
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.5", "jsonnull", "/node_id/jsonnull")
            _seed_json_null_observation(conn, node_id, None)
            assert _json_fact_types(conn, node_id) == ("text", "text")
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")
    logged = capfd.readouterr().err

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _observation_count(conn, node_id) == 0
    finally:
        engine.dispose()

    assert f"node_id={node_id}" in logged


def test_json_null_facts_beside_a_real_fact_become_sql_null(inventory_alembic_config):
    """Keep an observation carrying a real fact, normalizing its JSON nulls."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, HOST_OBSERVATION_PRE_CONSTRAINT_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            node_id = _seed_node(conn, "10.0.0.6", "mixed", "/node_id/mixed")
            _seed_json_null_observation(conn, node_id, "Ubuntu 24.04")
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _observation_count(conn, node_id) == 1
            assert (
                conn.exec_driver_sql(
                    "SELECT os_version FROM hostsystemobservation WHERE node_id = ?",
                    (node_id,),
                ).scalar_one()
                == "Ubuntu 24.04"
            )
            assert _json_fact_types(conn, node_id) == ("null", "null")
    finally:
        engine.dispose()
