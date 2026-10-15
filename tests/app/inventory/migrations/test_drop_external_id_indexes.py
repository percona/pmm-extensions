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

"""Test the Inventory-track migration dropping redundant ``external_id`` indexes."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

_PRE_REVISION = "13204e7e1043"
_REVISION = "a7d3f1c95b26"

_INSERT_NODE = (
    "INSERT INTO node "
    "(created_at, updated_at, address, name, external_id, source, type, "
    "retirement_key) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', '10.0.0.1', 'n', 'n', "
    "'PMM', 'generic', -1)"
)


def _index_names(sync_url: str, table_name: str) -> set[str]:
    """Return the names of the indexes on one table."""
    engine = create_engine(sync_url)
    try:
        return {i["name"] for i in inspect(engine).get_indexes(table_name)}
    finally:
        engine.dispose()


def test_upgrade_drops_indexes_and_keeps_source(
    inventory_alembic_config: tuple[Config, str],
) -> None:
    """Drop the redundant indexes, keep the composite ones and the source value."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, _PRE_REVISION)
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(_INSERT_NODE)
    engine.dispose()

    command.upgrade(cfg, _REVISION)

    node_indexes = _index_names(sync_url, "node")
    service_indexes = _index_names(sync_url, "service")
    assert "ix_node_external_id" not in node_indexes
    assert "ix_service_external_id" not in service_indexes
    assert "ix_node_external_id_source" in node_indexes
    assert "ix_service_external_id_node_id" in service_indexes
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT source FROM node").scalar_one() == "PMM"
    engine.dispose()


def test_upgrade_rejects_a_source_outside_the_enum(
    inventory_alembic_config: tuple[Config, str],
) -> None:
    """Refuse a ``source`` label the enum does not define once it is non-native."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, _REVISION)

    engine = create_engine(sync_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.exec_driver_sql(_INSERT_NODE.replace("'PMM'", "'XYZ'"))
    finally:
        engine.dispose()


def test_downgrade_restores_indexes(
    inventory_alembic_config: tuple[Config, str],
) -> None:
    """Restore the standalone indexes on downgrade and keep the source value."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, _REVISION)
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(_INSERT_NODE)
    engine.dispose()

    command.downgrade(cfg, _PRE_REVISION)

    assert "ix_node_external_id" in _index_names(sync_url, "node")
    assert "ix_service_external_id" in _index_names(sync_url, "service")
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT source FROM node").scalar_one() == "PMM"
    engine.dispose()
