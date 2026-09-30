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

"""Test the Inventory-track migration adding ``newest_attempt_at``."""

from collections.abc import Iterator
from datetime import datetime, UTC

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, Engine, inspect, text, URL
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.app.inventory.migrations.postgres_support import run_on_postgres

_PRE_REVISION = "6ee7bfe9c9d3"
_REVISION = "bed760f9fb35"

_SYNCABLE_TABLES = ("node", "service", "schema", "table")

_LAST_SYNCED_AT = datetime(2026, 9, 1, 10, tzinfo=UTC)
_FAILING_SINCE = datetime(2026, 9, 1, 11, tzinfo=UTC)

_INSERT_NODE = (
    "INSERT INTO node "
    "(created_at, updated_at, address, name, external_id, source, type, "
    "retirement_key, last_synced_at, sync_failing_since) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', '10.0.0.1', ?, ?, "
    "'PMM', 'generic', -1, ?, ?)"
)
_INSERT_SERVICE = (
    "INSERT INTO service "
    "(created_at, updated_at, external_id, name, type, port, node_id, "
    "retirement_key) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'svc', 'svc', "
    "'MYSQL', 3306, ?, -1)"
)
_INSERT_SCHEMA = (
    "INSERT INTO schema "
    "(created_at, updated_at, name, service_id, retirement_key) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'db', ?, -1)"
)
_INSERT_TABLE = (
    'INSERT INTO "table" '
    '(created_at, updated_at, name, "create", keys, schema_id, retirement_key, '
    "last_synced_at, sync_failing_since) "
    "VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 't', "
    "'CREATE TABLE t (id INT)', '{}', ?, -1, ?, ?)"
)

_PG_SEED_TIMESTAMPS = "'2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00'"
_PG_HEALTH = {"last_synced_at": _LAST_SYNCED_AT, "sync_failing_since": _FAILING_SINCE}


def _sqlite_text(value: datetime | None) -> str | None:
    """Render a timestamp the way SQLite stores ``DateTime`` columns.

    :param value: The timestamp to render.
    :return: Its stored text, or ``None`` for ``None``.
    """
    return value.strftime("%Y-%m-%d %H:%M:%S.%f") if value else None


def _last_id(conn: Connection) -> int:
    """Return the primary key of the row just inserted.

    :param conn: The SQLite connection that ran the insert.
    :return: The new row's id.
    """
    return conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()


def _seed_node(
    conn: Connection,
    last_synced_at: datetime | None,
    sync_failing_since: datetime | None,
) -> int:
    """Insert a ``node`` row carrying the given sync-health state.

    :param conn: The SQLite connection to insert with.
    :param last_synced_at: The row's last success, or ``None``.
    :param sync_failing_since: The row's failing-run start, or ``None``.
    :return: The new row's id.
    """
    conn.exec_driver_sql(
        _INSERT_NODE,
        ("n", "n", _sqlite_text(last_synced_at), _sqlite_text(sync_failing_since)),
    )
    return _last_id(conn)


def _newest_attempt_at(conn: Connection, table_name: str, entity_id: int) -> str | None:
    """Return the stored ``newest_attempt_at`` of one row.

    :param conn: The SQLite connection to read with.
    :param table_name: The table holding the row.
    :param entity_id: The row's id.
    :return: The stored text, or ``None`` when unset.
    """
    return conn.exec_driver_sql(
        f'SELECT newest_attempt_at FROM "{table_name}" WHERE id = ?', (entity_id,)
    ).scalar_one()


async def _seed_failing_chain(conn: AsyncConnection) -> dict[str, int]:
    """Insert one failing row per syncable table on PostgreSQL.

    :param conn: The PostgreSQL connection to insert with.
    :return: The new rows' ids by table.
    """
    node_id = (
        await conn.execute(
            text(
                "INSERT INTO node (created_at, updated_at, address, name, "
                "external_id, source, type, retirement_key, last_synced_at, "
                f"sync_failing_since) VALUES ({_PG_SEED_TIMESTAMPS}, '10.0.0.1', "
                "'n', 'n', CAST('PMM' AS sourceenum), 'generic', -1, "
                ":last_synced_at, :sync_failing_since) RETURNING id"
            ),
            _PG_HEALTH,
        )
    ).scalar_one()
    service_id = (
        await conn.execute(
            text(
                "INSERT INTO service (created_at, updated_at, external_id, name, "
                "type, port, node_id, retirement_key, last_synced_at, "
                f"sync_failing_since) VALUES ({_PG_SEED_TIMESTAMPS}, 's', 's', "
                "'MYSQL', 3306, :node_id, -1, :last_synced_at, :sync_failing_since) "
                "RETURNING id"
            ),
            {"node_id": node_id, **_PG_HEALTH},
        )
    ).scalar_one()
    schema_id = (
        await conn.execute(
            text(
                "INSERT INTO schema (created_at, updated_at, name, service_id, "
                "retirement_key, last_synced_at, sync_failing_since) "
                f"VALUES ({_PG_SEED_TIMESTAMPS}, 'db', :service_id, -1, "
                ":last_synced_at, :sync_failing_since) RETURNING id"
            ),
            {"service_id": service_id, **_PG_HEALTH},
        )
    ).scalar_one()
    table_id = (
        await conn.execute(
            text(
                'INSERT INTO "table" (created_at, updated_at, name, "create", keys, '
                "schema_id, retirement_key, last_synced_at, sync_failing_since) "
                f"VALUES ({_PG_SEED_TIMESTAMPS}, 't', 'CREATE TABLE t (id INT)', "
                "'{}', :schema_id, -1, :last_synced_at, :sync_failing_since) "
                "RETURNING id"
            ),
            {"schema_id": schema_id, **_PG_HEALTH},
        )
    ).scalar_one()
    return {
        "node": node_id,
        "service": service_id,
        "schema": schema_id,
        "table": table_id,
    }


async def _newest_attempts(
    conn: AsyncConnection, ids: dict[str, int]
) -> dict[str, datetime | None]:
    """Return each seeded PostgreSQL row's ``newest_attempt_at``.

    :param conn: The PostgreSQL connection to read with.
    :param ids: The rows' ids by table.
    :return: The stored values by table.
    """
    return {
        table_name: (
            await conn.execute(
                text(f'SELECT newest_attempt_at FROM "{table_name}" WHERE id = :id'),
                {"id": entity_id},
            )
        ).scalar_one()
        for table_name, entity_id in ids.items()
    }


async def _tables_with_newest_attempt_at(conn: AsyncConnection) -> set[str]:
    """Return the PostgreSQL tables that carry ``newest_attempt_at``.

    :param conn: The PostgreSQL connection to inspect with.
    :return: The table names.
    """
    result = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'newest_attempt_at'"
        )
    )
    return set(result.scalars())


@pytest.fixture
def pre_revision_engine(
    inventory_alembic_config: tuple[Config, str],
) -> Iterator[Engine]:
    """Return an engine on a database migrated up to the preceding revision."""
    cfg, sync_url = inventory_alembic_config
    command.upgrade(cfg, _PRE_REVISION)
    engine = create_engine(sync_url)
    yield engine
    engine.dispose()


class TestNewestAttemptAtMigration:
    """Test the ``newest_attempt_at`` column's upgrade, backfill and downgrade."""

    def test_upgrade_adds_a_nullable_column_to_every_syncable_table(
        self, inventory_alembic_config: tuple[Config, str], pre_revision_engine: Engine
    ) -> None:
        """Add ``newest_attempt_at`` as a nullable column on each syncable table."""
        cfg, _ = inventory_alembic_config

        command.upgrade(cfg, _REVISION)

        inspector = inspect(pre_revision_engine)
        for table_name in _SYNCABLE_TABLES:
            columns = {c["name"]: c for c in inspector.get_columns(table_name)}
            assert columns["newest_attempt_at"]["nullable"] is True

    @pytest.mark.parametrize(
        ("last_synced_at", "expected"),
        [(None, None), (_LAST_SYNCED_AT, _LAST_SYNCED_AT)],
        ids=["never_synced", "clean"],
    )
    def test_upgrade_backfills_a_clean_row_from_its_last_success(
        self,
        inventory_alembic_config: tuple[Config, str],
        pre_revision_engine: Engine,
        last_synced_at: datetime | None,
        expected: datetime | None,
    ) -> None:
        """Seed a row outside a failing run from its last success, else NULL."""
        cfg, _ = inventory_alembic_config
        with pre_revision_engine.begin() as conn:
            node_id = _seed_node(conn, last_synced_at, None)

        command.upgrade(cfg, _REVISION)

        with pre_revision_engine.connect() as conn:
            assert _newest_attempt_at(conn, "node", node_id) == _sqlite_text(expected)

    @pytest.mark.parametrize(
        "last_synced_at",
        [_LAST_SYNCED_AT, None],
        ids=["failing_after_success", "failing_never_synced"],
    )
    def test_upgrade_backfills_a_failing_row_with_the_upgrade_time(
        self,
        inventory_alembic_config: tuple[Config, str],
        pre_revision_engine: Engine,
        last_synced_at: datetime | None,
    ) -> None:
        """Seed a failing row past every pre-upgrade attempt, not from its run start.

        The run start names only the first failure, so a success attempted
        after it may still predate a later, unrecorded failure of the run.
        """
        cfg, _ = inventory_alembic_config
        with pre_revision_engine.begin() as conn:
            node_id = _seed_node(conn, last_synced_at, _FAILING_SINCE)

        before = datetime.now(UTC)
        command.upgrade(cfg, _REVISION)
        after = datetime.now(UTC)

        with pre_revision_engine.connect() as conn:
            stored = _newest_attempt_at(conn, "node", node_id)
        assert _sqlite_text(before) <= stored <= _sqlite_text(after)

    def test_upgrade_backfills_the_reserved_word_table(
        self, inventory_alembic_config: tuple[Config, str], pre_revision_engine: Engine
    ) -> None:
        """Backfill ``table`` too, whose name needs quoting in the UPDATE."""
        cfg, _ = inventory_alembic_config
        with pre_revision_engine.begin() as conn:
            node_id = _seed_node(conn, None, None)
            conn.exec_driver_sql(_INSERT_SERVICE, (node_id,))
            conn.exec_driver_sql(_INSERT_SCHEMA, (_last_id(conn),))
            conn.exec_driver_sql(
                _INSERT_TABLE,
                (
                    _last_id(conn),
                    _sqlite_text(_LAST_SYNCED_AT),
                    _sqlite_text(_FAILING_SINCE),
                ),
            )
            table_id = _last_id(conn)

        before = datetime.now(UTC)
        command.upgrade(cfg, _REVISION)

        with pre_revision_engine.connect() as conn:
            stored = _newest_attempt_at(conn, "table", table_id)
        assert stored >= _sqlite_text(before)

    def test_downgrade_drops_only_the_new_column(
        self, inventory_alembic_config: tuple[Config, str], pre_revision_engine: Engine
    ) -> None:
        """Remove ``newest_attempt_at`` and keep the other sync-health columns."""
        cfg, _ = inventory_alembic_config
        with pre_revision_engine.begin() as conn:
            node_id = _seed_node(conn, _LAST_SYNCED_AT, _FAILING_SINCE)
        command.upgrade(cfg, _REVISION)

        command.downgrade(cfg, _PRE_REVISION)

        inspector = inspect(pre_revision_engine)
        for table_name in _SYNCABLE_TABLES:
            names = {c["name"] for c in inspector.get_columns(table_name)}
            assert "newest_attempt_at" not in names
            assert {"last_synced_at", "sync_failing_since"} <= names
        with pre_revision_engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT last_synced_at, sync_failing_since FROM node WHERE id = ?",
                (node_id,),
            ).one()
        assert tuple(row) == (
            _sqlite_text(_LAST_SYNCED_AT),
            _sqlite_text(_FAILING_SINCE),
        )


@pytest.mark.postgres
class TestNewestAttemptAtMigrationOnPostgreSQL:
    """Test the ``newest_attempt_at`` migration against real PostgreSQL.

    The backfill ``UPDATE`` targets ``schema`` and ``table``, both reserved
    words, and binds a ``timestamptz`` value; SQLite quotes and types neither
    the way PostgreSQL does, so only this lane proves the statement is accepted.
    """

    def test_upgrade_backfills_every_syncable_table(
        self, inventory_postgres_config: tuple[Config, URL]
    ) -> None:
        """Seed each failing row with the upgrade time, reserved-word tables included."""
        cfg, url = inventory_postgres_config
        command.upgrade(cfg, _PRE_REVISION)
        ids = run_on_postgres(url, _seed_failing_chain)

        before = datetime.now(UTC)
        command.upgrade(cfg, _REVISION)
        after = datetime.now(UTC)

        newest = run_on_postgres(url, lambda conn: _newest_attempts(conn, ids))
        assert newest.keys() == ids.keys()
        assert all(before <= value <= after for value in newest.values())

    def test_downgrade_round_trips(
        self, inventory_postgres_config: tuple[Config, URL]
    ) -> None:
        """Drop the column from every syncable table, then let it be re-added."""
        cfg, url = inventory_postgres_config
        command.upgrade(cfg, _REVISION)

        command.downgrade(cfg, _PRE_REVISION)
        after_downgrade = run_on_postgres(url, _tables_with_newest_attempt_at)
        command.upgrade(cfg, _REVISION)

        assert after_downgrade == set()
        assert run_on_postgres(url, _tables_with_newest_attempt_at) == set(
            _SYNCABLE_TABLES
        )
