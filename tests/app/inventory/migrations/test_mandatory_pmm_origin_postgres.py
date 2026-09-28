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

"""Test the mandatory-PMM-origin migration against real PostgreSQL.

The SQLite sibling cannot fail on the two steps most likely to break. SQLite
stores the ``source`` enum as free text, so a wrong label passes there and only
``sourceenum`` rejects it here; and ``batch_alter_table`` recreates the table on
SQLite while emitting a plain ``ALTER`` here. These cases exercise the backfill
and the DDL on the native path.

Everything runs over ``asyncpg``: the inventory track's ``env.py`` builds its own
async engine, and the ``test_postgres`` CI job installs the ``postgresql`` group
only, so no sync driver is available to lean on.
"""

from functools import partial

import pytest
from alembic import command
from sqlalchemy import text

from tests.app.inventory.legacy_origin import PRE_RENAME_LEGACY_PREFIX
from tests.app.inventory.migrations.conftest import SEED_TIMESTAMPS
from tests.app.inventory.migrations.postgres_support import run_on_postgres

# The head immediately before the PMM origin becomes mandatory.
_PRE_ORIGIN_REVISION = "c7d1e94ab3f2"

_MANDATORY_COLUMNS = (
    ("node", "external_id"),
    ("node", "source"),
    ("service", "external_id"),
)

_NULLABILITY = (
    "SELECT is_nullable FROM information_schema.columns "
    "WHERE table_name = :table_name AND column_name = :column_name"
)

pytestmark = pytest.mark.postgres


async def _insert_node(conn, address, name, external_id, source):
    """Insert a ``node`` row and return its primary key."""
    result = await conn.execute(
        text(
            "INSERT INTO node (created_at, updated_at, address, name, external_id, "
            f"source, type, retirement_key) VALUES ({SEED_TIMESTAMPS}, :address, "
            ":name, :external_id, CAST(:source AS sourceenum), 'generic', -1) "
            "RETURNING id"
        ),
        {
            "address": address,
            "name": name,
            "external_id": external_id,
            "source": source,
        },
    )
    return result.scalar_one()


async def _insert_service(conn, external_id, name, port, node_id):
    """Insert a ``service`` row and return its primary key."""
    result = await conn.execute(
        text(
            "INSERT INTO service (created_at, updated_at, external_id, name, type, "
            f"port, node_id, retirement_key) VALUES ({SEED_TIMESTAMPS}, "
            ":external_id, :name, 'MYSQL', :port, :node_id, -1) RETURNING id"
        ),
        {
            "external_id": external_id,
            "name": name,
            "port": port,
            "node_id": node_id,
        },
    )
    return result.scalar_one()


async def _row(conn, table_name, entity_id):
    """Return the named table's row for ``entity_id`` as a dict."""
    quoted = f'"{table_name}"' if table_name == "table" else table_name
    result = await conn.execute(
        text(f"SELECT * FROM {quoted} WHERE id = :id"),
        {"id": entity_id},
    )
    return dict(result.mappings().one())


async def _projected_row(conn, table_name, entity_id, column_names):
    """Return the row restricted to ``column_names``.

    A revision above ``_PRE_ORIGIN_REVISION`` may add columns, and downgrading
    to it drops them on the way past. Comparing whole rows across that boundary
    would then fail on the schema difference rather than on the data the test is
    about. The caller reads ``column_names`` off the pre-origin row itself, so no
    list here has to be kept in step with later revisions, and the projected
    expectation still pins the column set: it came from a different source than
    the post-downgrade row it is compared against.

    :param conn: The open connection to read through.
    :param table_name: The table holding the row.
    :param entity_id: The row's primary key.
    :param column_names: The columns to keep.
    :return: The kept columns and their values.
    """
    row = await _row(conn, table_name, entity_id)
    return {name: value for name, value in row.items() if name in column_names}


async def _is_nullable(conn, table_name, column):
    """Return ``information_schema``'s nullability verdict for one column."""
    result = await conn.execute(
        text(_NULLABILITY), {"table_name": table_name, "column_name": column}
    )
    return result.scalar_one()


def test_backfill_writes_a_valid_enum_label(inventory_postgres_config):
    """Stamp a label ``sourceenum`` accepts, which SQLite could not have proven."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, _PRE_ORIGIN_REVISION)
    node_id = run_on_postgres(
        url, lambda conn: _insert_node(conn, "10.0.0.1", "legacy", None, None)
    )

    command.upgrade(cfg, "heads")

    node = run_on_postgres(url, lambda conn: _row(conn, "node", node_id))
    assert node["source"] == "PMM"
    assert node["external_id"] == f"{PRE_RENAME_LEGACY_PREFIX}{node_id}"


def test_set_not_null_lands_on_the_native_path(inventory_postgres_config):
    """Report all three columns as NOT NULL after the plain ALTER."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, "heads")

    for table_name, column in _MANDATORY_COLUMNS:
        verdict = run_on_postgres(
            url, partial(_is_nullable, table_name=table_name, column=column)
        )
        assert verdict == "NO", f"{table_name}.{column}"


def test_cascade_under_real_foreign_keys(inventory_postgres_config):
    """Retire the whole subtree without tripping a foreign-key constraint."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, _PRE_ORIGIN_REVISION)

    async def _seed(conn):
        node_id = await _insert_node(conn, "10.0.0.2", "deep", None, None)
        service_id = await _insert_service(
            conn, "/service_id/deep", "svc", 3306, node_id
        )
        schema_id = (
            await conn.execute(
                text(
                    "INSERT INTO schema (created_at, updated_at, name, service_id, "
                    f"retirement_key) VALUES ({SEED_TIMESTAMPS}, 'sch', "
                    ":service_id, -1) RETURNING id"
                ),
                {"service_id": service_id},
            )
        ).scalar_one()
        table_id = (
            await conn.execute(
                text(
                    'INSERT INTO "table" (created_at, updated_at, name, "create", '
                    f"keys, schema_id, retirement_key) VALUES ({SEED_TIMESTAMPS}, "
                    "'tbl', 'CREATE TABLE t (id INT)', '{}', :schema_id, -1) "
                    "RETURNING id"
                ),
                {"schema_id": schema_id},
            )
        ).scalar_one()
        return node_id, service_id, schema_id, table_id

    node_id, service_id, schema_id, table_id = run_on_postgres(url, _seed)

    command.upgrade(cfg, "heads")

    for table_name, entity_id in (
        ("node", node_id),
        ("service", service_id),
        ("schema", schema_id),
        ("table", table_id),
    ):
        row = run_on_postgres(
            url, partial(_row, table_name=table_name, entity_id=entity_id)
        )
        assert row["retired_at"] is not None, table_name
        assert row["retirement_key"] == entity_id, table_name


def test_downgrade_restores_nullability_and_leaves_data_alone(
    inventory_postgres_config,
):
    """Restore the three columns to nullable without rewriting a stamped row."""
    cfg, url = inventory_postgres_config
    command.upgrade(cfg, _PRE_ORIGIN_REVISION)
    node_id = run_on_postgres(
        url, lambda conn: _insert_node(conn, "10.0.0.3", "legacy", None, None)
    )
    node_columns = set(run_on_postgres(url, lambda conn: _row(conn, "node", node_id)))

    command.upgrade(cfg, "heads")
    stamped = run_on_postgres(
        url, lambda conn: _projected_row(conn, "node", node_id, node_columns)
    )

    command.downgrade(cfg, _PRE_ORIGIN_REVISION)

    for table_name, column in _MANDATORY_COLUMNS:
        verdict = run_on_postgres(
            url, partial(_is_nullable, table_name=table_name, column=column)
        )
        assert verdict == "YES", f"{table_name}.{column}"

    assert run_on_postgres(url, lambda conn: _row(conn, "node", node_id)) == stamped
