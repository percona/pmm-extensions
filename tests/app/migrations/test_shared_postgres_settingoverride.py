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

"""Test the shared-database guards on the ``settingoverride`` migrations.

The ``settingoverride`` table is created by the Extensions, Tasks and Inventory
Alembic tracks alike, and every track also drops the ``setting_class`` CHECK
and adds the ``updated_by`` actor column. On a shared PostgreSQL database the
tracks run ``upgrade heads`` against one physical schema, so the guarded
migrations must apply the DDL exactly once regardless of which track wins the
race. The real-PostgreSQL cases exercise that cross-track scenario; the SQLite
cases pin the cross-dialect helpers the guards rely on, plus the ``updated_by``
add-and-drop round trip on the default engine, where ``batch_alter_table``
recreates the table instead of altering it in place.
"""

from typing import Any
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import (
    CheckConstraint,
    Column,
    create_engine,
    inspect,
    Integer,
    MetaData,
    String,
    Table,
    text,
)
from sqlalchemy import (
    Enum as EnumField,
)
from sqlalchemy.engine import URL
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.exc import OperationalError
from sqlmodel import select, Session

from app.core.db.utils import (
    acquire_pg_advisory_xact_lock,
    check_constraint_lists_members,
    check_constraint_name,
    column_exists,
)
from app.core.encryption import marked_ciphertext
from app.core.settings_override.constants import (
    SETTINGOVERRIDE_MIGRATION_LOCK_KEY,
    SETTINGOVERRIDE_UPDATED_BY_COLUMN,
)
from app.core.settings_override.models import SettingOverride
from app.core.utils import json_serializer
from app.core.utils.fields import AsyncDatabaseEngine
from app.extensions.config import extensions_settings
from app.inventory.config import inventory_settings
from app.tasks.config import tasks_settings
from tests.app.alembic_paths import ALEMBIC_INI
from tests.app.core.settings_override.conftest import (
    ALERT_SETTINGS_TOKEN,
    EXTENSIONS_SETTINGS_TOKEN,
    LEGACY_SEP_SETTINGS_TOKEN,
    LONG_USERNAME_LENGTH,
    PMM_API_KEY,
    ROUTING_KEY,
    SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)
from tests.app.encryption_fixtures import stored_plaintext

_SETTING_CLASS_VARCHAR_LENGTH = 255
#: The ``SYNC_REFRESH_TIME`` value seeded before a downgrade, read back through
#: the JSONB column to prove the drop left the row's data intact.
_SEEDED_OVERRIDE_VALUE = 5
# The PMM Extensions revision immediately below the secret re-encryption one, so a test can
# seed plaintext rows into the state a deployment carrying overrides upgrades from.
_EXTENSIONS_PRE_ENCRYPTION_REVISION = "c9880f0ac1bd"


#: A credential-bearing endpoint, whose userinfo password the credential-URL
#: revision encrypts while leaving the rest of the URL legible.
_CREDENTIAL_URL = "https://inv-user:inv-secret@inventory.internal:8080/api"
_CREDENTIAL_PASSWORD = "inv-secret"

#: A ``PMM`` endpoint carrying a password, so the whole-object row holds both
#: leaf kinds at once: the ``SecretStr`` ``api_key`` one revision encrypts and
#: the credential URL the next one does. That row is what proves the second
#: revision's downgrade is its own inverse rather than the broad helper's.
_PMM_CREDENTIAL_ENDPOINT = "https://pmm-user:pmm-secret@pmm.example.com:8443/"

_SEED_ROWS = [
    (
        SETTINGS_TOKEN,
        "PMM",
        {"endpoint": _PMM_CREDENTIAL_ENDPOINT, "api_key": PMM_API_KEY},
    ),
    (SETTINGS_TOKEN, "PMM__api_key", PMM_API_KEY),
    (SETTINGS_TOKEN, "LOGGING", "DEBUG"),
    (
        ALERT_SETTINGS_TOKEN,
        "PROVIDERS",
        [{"PROVIDER": "pagerduty", "routing_key": ROUTING_KEY}],
    ),
    (TASKS_SETTINGS_TOKEN, "STALENESS_THRESHOLD_SECONDS", 7200),
    (LEGACY_SEP_SETTINGS_TOKEN, "INVENTORY_ENDPOINT", _CREDENTIAL_URL),
]

# The PMM Extensions and Tasks revisions immediately below ``add_setting_override_table``
# on each track — downgrading to them drops the shared table and runs the other
# track's enum-narrowing downgrades against the now-missing table.
_EXTENSIONS_PRE_SETTINGOVERRIDE_REVISION = "810c31754b54"
_TASKS_PRE_SETTINGOVERRIDE_REVISION = "e42ce8324da7"

# The PMM Extensions revision immediately below ``add_settingoverride_updated_by``, so a
# downgrade to it runs exactly the column drop.
_EXTENSIONS_PRE_UPDATED_BY_REVISION = "867df844fe17"


def _sqlite_engine_with_setting_class_check(members):
    """Build an in-memory SQLite engine whose ``settingoverride`` CHECK lists ``members``.

    :param members: The enum member names the ``setting_class`` CHECK constraint
        should enumerate.
    :type members: tuple[str, ...]
    :return: A connected in-memory SQLite engine with the table materialized.
    :rtype: Engine
    """
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "settingoverride",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "setting_class",
            EnumField(
                *members,
                name="settingclassenum",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )
    metadata.create_all(engine)
    return engine


class TestCheckConstraintListsMembers:
    """Cover the cross-dialect ``check_constraint_lists_members`` helper on SQLite."""

    def test_returns_true_for_a_present_member(self):
        """Confirm one listed member is reported as present."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn, "settingoverride", "setting_class", ("SEP_SETTINGS",)
                    )
                    is True
                )
        finally:
            engine.dispose()

    def test_returns_true_when_all_requested_members_present(self):
        """Confirm ``True`` only when every requested member is listed."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS", "SETTINGS", "ALERT_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn,
                        "settingoverride",
                        "setting_class",
                        ("SETTINGS", "ALERT_SETTINGS"),
                    )
                    is True
                )
        finally:
            engine.dispose()

    def test_returns_false_when_a_member_is_absent(self):
        """Confirm ``False`` when the requested member is not listed."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS", "SETTINGS", "ALERT_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn,
                        "settingoverride",
                        "setting_class",
                        ("ANONYMIZER_SETTINGS",),
                    )
                    is False
                )
        finally:
            engine.dispose()

    def test_returns_false_when_only_some_members_present(self):
        """Confirm ``False`` when only a subset of the requested members is listed."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS", "SETTINGS", "ALERT_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn,
                        "settingoverride",
                        "setting_class",
                        ("SETTINGS", "ANONYMIZER_SETTINGS"),
                    )
                    is False
                )
        finally:
            engine.dispose()

    def test_does_not_match_quoted_substring(self):
        """Match whole quoted tokens so ``SETTINGS`` does not match ``SEP_SETTINGS``."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn, "settingoverride", "setting_class", ("SETTINGS",)
                    )
                    is False
                )
        finally:
            engine.dispose()

    def test_returns_false_for_missing_table(self):
        """Confirm ``False`` for a missing table instead of raising ``NoSuchTableError``."""
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_lists_members(
                        conn, "settingoverride", "setting_class", ("SEP_SETTINGS",)
                    )
                    is False
                )
        finally:
            engine.dispose()


class TestCheckConstraintName:
    """Cover :func:`check_constraint_name` on SQLite."""

    def test_returns_sqlalchemy_enum_constraint_name(self):
        """Return the name SQLAlchemy assigns a non-native enum CHECK."""
        engine = _sqlite_engine_with_setting_class_check(
            ("SEP_SETTINGS", "TASKS_SETTINGS")
        )
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_name(conn, "settingoverride", "setting_class")
                    == "settingclassenum"
                )
        finally:
            engine.dispose()

    def test_returns_none_for_missing_table(self):
        """Return ``None`` for a missing table instead of raising."""
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_name(conn, "settingoverride", "setting_class")
                    is None
                )
        finally:
            engine.dispose()

    def test_returns_none_when_column_has_no_check(self):
        """Return ``None`` when the table exists but the column is unconstrained."""
        engine = create_engine("sqlite://")
        metadata = MetaData()
        Table(
            "settingoverride",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("setting_class", Integer, nullable=False),
        )
        metadata.create_all(engine)
        try:
            with engine.connect() as conn:
                assert (
                    check_constraint_name(conn, "settingoverride", "setting_class")
                    is None
                )
        finally:
            engine.dispose()

    def test_raises_when_multiple_checks_mention_column(self):
        """Fail fast when more than one CHECK mentions the column."""
        engine = create_engine("sqlite://")
        metadata = MetaData()
        Table(
            "settingoverride",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("setting_class", String(64), nullable=False),
            CheckConstraint(
                "setting_class IN ('A', 'B')",
                name="setting_class_enum_check",
            ),
            CheckConstraint(
                "length(setting_class) > 0",
                name="setting_class_nonempty_check",
            ),
        )
        metadata.create_all(engine)
        try:
            with (
                engine.connect() as conn,
                pytest.raises(RuntimeError, match="at most one CHECK"),
            ):
                check_constraint_name(conn, "settingoverride", "setting_class")
        finally:
            engine.dispose()


def test_advisory_lock_is_noop_off_postgres():
    """Issue no SQL and raise nothing when the bind is not PostgreSQL."""
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            assert (
                acquire_pg_advisory_xact_lock(conn, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)
                is None
            )
    finally:
        engine.dispose()


@pytest.fixture
def shared_postgres_db(postgres_sync_url, monkeypatch):
    """Configure the Extensions, Tasks and Inventory tracks to share one real-PostgreSQL database.

    ``command.upgrade`` builds its own engine from ``<svc>_settings.DATABASE.URL``
    via each track's ``env.py`` and writes to the ``public`` schema — it does not
    inherit the ``postgres_engine`` fixture's per-worker schema — so every service
    settings must point at the same host and database for the cross-track race to
    occur. Yield the sync URL for verification and drop every table the upgrade
    created on teardown so the shared schema is left clean for sibling tests.
    """
    for settings in (extensions_settings, tasks_settings, inventory_settings):
        monkeypatch.setattr(settings.DATABASE, "ENGINE", AsyncDatabaseEngine.POSTGRESQL)
        monkeypatch.setattr(settings.DATABASE, "USER", postgres_sync_url.username)
        monkeypatch.setattr(
            settings.DATABASE,
            "PASSWORD",
            SecretStr(postgres_sync_url.password)
            if postgres_sync_url.password
            else None,
        )
        monkeypatch.setattr(settings.DATABASE, "HOST", postgres_sync_url.host)
        monkeypatch.setattr(settings.DATABASE, "PORT", postgres_sync_url.port)
        monkeypatch.setattr(settings.DATABASE, "NAME", postgres_sync_url.database)
    try:
        yield postgres_sync_url
    finally:
        engine = create_engine(postgres_sync_url)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("DROP SCHEMA public CASCADE")
                conn.exec_driver_sql("CREATE SCHEMA public")
        finally:
            engine.dispose()


def _setting_class_check_haystack(sync_url) -> str:
    """Return the joined ``setting_class`` CHECK constraint text for ``settingoverride``."""
    engine = create_engine(sync_url)
    try:
        constraints = inspect(engine).get_check_constraints("settingoverride")
    finally:
        engine.dispose()
    return " ".join(
        constraint["sqltext"] or ""
        for constraint in constraints
        if "setting_class" in (constraint["sqltext"] or "")
    )


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_extensions_then_tasks_upgrade_is_clean(shared_postgres_db):
    """Apply PMM Extensions, then Tasks, on one shared database cleanly.

    Neither upgrade may raise a duplicate-table error.
    """
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    tasks_cfg = Config(str(ALEMBIC_INI), ini_section="tasks")

    command.upgrade(extensions_cfg, "heads")
    command.upgrade(tasks_cfg, "heads")

    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("settingoverride")
        assert inspector.has_table("alembic_version_extensions")
        assert inspector.has_table("alembic_version_tasks")
        setting_class_type = next(
            column["type"]
            for column in inspector.get_columns("settingoverride")
            if column["name"] == "setting_class"
        )
        assert setting_class_type.length == _SETTING_CLASS_VARCHAR_LENGTH
    finally:
        engine.dispose()

    haystack = _setting_class_check_haystack(sync_url)
    assert haystack == ""


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_tasks_then_extensions_upgrade_is_clean(shared_postgres_db):
    """Apply Tasks, then PMM Extensions — the reverse order must be equally clean."""
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    tasks_cfg = Config(str(ALEMBIC_INI), ini_section="tasks")

    command.upgrade(tasks_cfg, "heads")
    command.upgrade(extensions_cfg, "heads")

    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("settingoverride")
        assert inspector.has_table("alembic_version_extensions")
        assert inspector.has_table("alembic_version_tasks")
        setting_class_type = next(
            column["type"]
            for column in inspector.get_columns("settingoverride")
            if column["name"] == "setting_class"
        )
        assert setting_class_type.length == _SETTING_CLASS_VARCHAR_LENGTH
    finally:
        engine.dispose()

    haystack = _setting_class_check_haystack(sync_url)
    assert haystack == ""


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_downgrade_either_order_is_clean(shared_postgres_db):
    """Drop the shared table via PMM Extensions, then downgrade Tasks over the missing table.

    The Tasks enum-narrowing downgrades run after the PMM Extensions track already dropped
    ``settingoverride``; their guards must no-op instead of raising
    ``NoSuchTableError``.
    """
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    tasks_cfg = Config(str(ALEMBIC_INI), ini_section="tasks")

    command.upgrade(extensions_cfg, "heads")
    command.upgrade(tasks_cfg, "heads")

    command.downgrade(extensions_cfg, _EXTENSIONS_PRE_SETTINGOVERRIDE_REVISION)
    command.downgrade(tasks_cfg, _TASKS_PRE_SETTINGOVERRIDE_REVISION)

    engine = create_engine(sync_url)
    try:
        assert not inspect(engine).has_table("settingoverride")
    finally:
        engine.dispose()


def _seed_override_rows(sync_url: URL, rows: list[tuple[str, str, Any]]) -> None:
    """Insert one ``settingoverride`` row per entry through a sync engine.

    :param sync_url: The sync URL of the shared database.
    :param rows: ``(setting_class, key, value)`` triples to persist.
    """
    engine = create_engine(sync_url, json_serializer=json_serializer)
    try:
        with Session(engine) as session:
            for setting_class, key, value in rows:
                session.add(
                    SettingOverride(
                        setting_class=setting_class,
                        key=key,
                        value=value,
                        is_active=True,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def _stored_override_values(sync_url: URL) -> dict[tuple[str, str], Any]:
    """Return every stored override value keyed by ``(setting_class, key)``.

    :param sync_url: The sync URL of the shared database.
    :return: The values as PostgreSQL's ``jsonb`` column returns them.
    """
    engine = create_engine(sync_url, json_serializer=json_serializer)
    try:
        with Session(engine) as session:
            rows = session.exec(select(SettingOverride)).all()
        return {(row.setting_class, row.key): row.value for row in rows}
    finally:
        engine.dispose()


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_secret_rows_are_encrypted_by_the_extensions_track(
    shared_postgres_db,
):
    """Encrypt the secret leaves of pre-existing rows when the PMM Extensions chain reaches them.

    Seeds at the revision below the re-encryption one so the rows already exist
    when it runs, which is the upgrade path a deployment carrying overrides
    takes. The values come back through ``jsonb`` rather than SQLite's ``json``,
    which is what makes this the dialect arm of the walker's coverage.

    Both leaf kinds are seeded: a whole-value ``SecretStr`` leaf and a
    credential-bearing URL, whose userinfo password alone is rewritten while the
    surrounding endpoint stays legible.

    The Tasks chain then runs over the same physical table and must neither
    re-encrypt what the PMM Extensions chain rewrote nor touch the rows whose
    ``setting_class`` it cannot resolve.
    """
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    tasks_cfg = Config(str(ALEMBIC_INI), ini_section="tasks")

    command.upgrade(extensions_cfg, _EXTENSIONS_PRE_ENCRYPTION_REVISION)
    _seed_override_rows(sync_url, _SEED_ROWS)
    before = _stored_override_values(sync_url)

    command.upgrade(extensions_cfg, "heads")

    stored = _stored_override_values(sync_url)
    assert stored_plaintext(stored[(SETTINGS_TOKEN, "PMM")]["api_key"]) == PMM_API_KEY
    assert marked_ciphertext(stored[(SETTINGS_TOKEN, "PMM")]["api_key"]) is not None
    pmm_endpoint = urlparse(stored[(SETTINGS_TOKEN, "PMM")]["endpoint"])
    assert stored_plaintext(pmm_endpoint.password) == "pmm-secret"
    assert marked_ciphertext(pmm_endpoint.password) is not None
    assert pmm_endpoint.hostname == "pmm.example.com"
    assert stored_plaintext(stored[(SETTINGS_TOKEN, "PMM__api_key")]) == PMM_API_KEY
    provider = stored[(ALERT_SETTINGS_TOKEN, "PROVIDERS")][0]
    assert stored_plaintext(provider["routing_key"]) == ROUTING_KEY
    assert provider["PROVIDER"] == "pagerduty"
    endpoint = urlparse(stored[(EXTENSIONS_SETTINGS_TOKEN, "INVENTORY_ENDPOINT")])
    assert stored_plaintext(endpoint.password) == _CREDENTIAL_PASSWORD
    assert endpoint.username == "inv-user"
    assert endpoint.hostname == "inventory.internal"
    assert endpoint.path == "/api"
    assert stored[(SETTINGS_TOKEN, "LOGGING")] == before[(SETTINGS_TOKEN, "LOGGING")]
    assert (
        stored[(TASKS_SETTINGS_TOKEN, "STALENESS_THRESHOLD_SECONDS")]
        == (before[(TASKS_SETTINGS_TOKEN, "STALENESS_THRESHOLD_SECONDS")])
    )

    after_extensions = _stored_override_values(sync_url)
    command.upgrade(tasks_cfg, "heads")
    assert _stored_override_values(sync_url) == after_extensions


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_downgrade_restores_the_original_plaintext(shared_postgres_db):
    """Return every secret leaf to the plaintext the previous release reads."""
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")

    command.upgrade(extensions_cfg, _EXTENSIONS_PRE_ENCRYPTION_REVISION)
    _seed_override_rows(sync_url, _SEED_ROWS)
    before = _stored_override_values(sync_url)
    command.upgrade(extensions_cfg, "heads")
    assert _stored_override_values(sync_url) != before

    command.downgrade(extensions_cfg, _EXTENSIONS_PRE_ENCRYPTION_REVISION)

    assert _stored_override_values(sync_url) == before


@pytest.mark.postgres
def test_advisory_lock_serializes(postgres_sync_url):
    """Verify two connections cannot hold the migration advisory lock at once."""
    engine = create_engine(postgres_sync_url)
    try:
        with engine.connect() as conn_a, engine.connect() as conn_b:
            conn_b.exec_driver_sql("SET lock_timeout = '750ms'")
            conn_b.commit()

            trans_a = conn_a.begin()
            acquire_pg_advisory_xact_lock(conn_a, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)

            trans_b = conn_b.begin()
            with pytest.raises(OperationalError):
                acquire_pg_advisory_xact_lock(
                    conn_b, SETTINGOVERRIDE_MIGRATION_LOCK_KEY
                )
            trans_b.rollback()

            trans_a.commit()

            trans_b_retry = conn_b.begin()
            acquire_pg_advisory_xact_lock(conn_b, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)
            trans_b_retry.commit()
    finally:
        engine.dispose()


class TestColumnExists:
    """Cover the cross-dialect :func:`column_exists` helper on SQLite."""

    def test_returns_true_for_a_declared_column(self):
        """Report a column the table declares as present."""
        engine = _sqlite_engine_with_setting_class_check(("SEP_SETTINGS",))
        try:
            with engine.connect() as conn:
                assert column_exists(conn, "settingoverride", "setting_class") is True
        finally:
            engine.dispose()

    def test_returns_false_for_an_absent_column(self):
        """Report a column the table does not declare as absent."""
        engine = _sqlite_engine_with_setting_class_check(("SEP_SETTINGS",))
        try:
            with engine.connect() as conn:
                assert (
                    column_exists(
                        conn, "settingoverride", SETTINGOVERRIDE_UPDATED_BY_COLUMN
                    )
                    is False
                )
        finally:
            engine.dispose()

    def test_returns_false_for_missing_table(self):
        """Report ``False`` for a missing table instead of raising ``NoSuchTableError``."""
        engine = create_engine("sqlite://")
        try:
            with engine.connect() as conn:
                assert (
                    column_exists(
                        conn, "settingoverride", SETTINGOVERRIDE_UPDATED_BY_COLUMN
                    )
                    is False
                )
        finally:
            engine.dispose()


@pytest.mark.postgres
def test_column_exists_on_real_postgres(postgres_sync_url):
    """Distinguish a declared from an undeclared column on the deployment dialect."""
    engine = create_engine(postgres_sync_url)
    metadata = MetaData()
    Table(
        "settingoverride_column_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("setting_class", String(64), nullable=False),
    )
    try:
        metadata.create_all(engine)
        try:
            with engine.connect() as conn:
                assert (
                    column_exists(conn, "settingoverride_column_probe", "setting_class")
                    is True
                )
                assert (
                    column_exists(
                        conn,
                        "settingoverride_column_probe",
                        SETTINGOVERRIDE_UPDATED_BY_COLUMN,
                    )
                    is False
                )
        finally:
            metadata.drop_all(engine)
    finally:
        engine.dispose()


def _updated_by_columns(sync_url: str) -> list[ReflectedColumn]:
    """Return every reflected ``settingoverride`` column named ``updated_by``.

    :param sync_url: The sync URL of the shared database to inspect.
    :return: The matching inspector column dicts.
    """
    engine = create_engine(sync_url)
    try:
        columns = inspect(engine).get_columns("settingoverride")
    finally:
        engine.dispose()
    return [
        column
        for column in columns
        if column["name"] == SETTINGOVERRIDE_UPDATED_BY_COLUMN
    ]


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
@pytest.mark.parametrize(
    "order",
    [
        ("extensions", "tasks", "inventory"),
        ("tasks", "extensions", "inventory"),
        ("inventory", "extensions", "tasks"),
    ],
    ids=["extensions-first", "tasks-first", "inventory-first"],
)
def test_shared_db_three_track_upgrade_adds_updated_by_once(shared_postgres_db, order):
    """Add ``updated_by`` exactly once whichever track reaches the column first."""
    sync_url = shared_postgres_db
    for section in order:
        command.upgrade(Config(str(ALEMBIC_INI), ini_section=section), "heads")

    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("alembic_version_extensions")
        assert inspector.has_table("alembic_version_tasks")
        assert inspector.has_table("alembic_version_inventory")
    finally:
        engine.dispose()

    matching = _updated_by_columns(sync_url)
    assert len(matching) == 1
    assert matching[0]["nullable"] is True


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_rerunning_a_track_upgrade_is_a_noop(shared_postgres_db):
    """Re-run one track's ``upgrade heads`` without a duplicate-column error."""
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    command.upgrade(extensions_cfg, "heads")
    command.upgrade(Config(str(ALEMBIC_INI), ini_section="tasks"), "heads")

    command.upgrade(extensions_cfg, "heads")

    assert len(_updated_by_columns(sync_url)) == 1


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_long_actor_round_trips(shared_postgres_db):
    """Store an unusually long username intact on the deployment dialect.

    The column is unbounded, so there is no width for a long username to exceed;
    a bounded one would raise ``StringDataRightTruncation`` here while passing on
    SQLite, which ignores ``VARCHAR`` lengths.
    """
    sync_url = shared_postgres_db
    command.upgrade(Config(str(ALEMBIC_INI), ini_section="extensions"), "heads")
    actor = "a" * LONG_USERNAME_LENGTH

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settingoverride "
                    "(setting_class, key, value, is_active, created_at, updated_by) "
                    "VALUES ('EXTENSIONS_SETTINGS', 'SYNC_REFRESH_TIME', '5', true, now(), "
                    ":actor)"
                ),
                {"actor": actor},
            )
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT updated_by FROM settingoverride"))
            assert stored.scalar_one() == actor
    finally:
        engine.dispose()


@pytest.mark.xdist_group("shared_postgres_db")
@pytest.mark.postgres
def test_shared_db_downgrade_drops_updated_by_and_keeps_the_rows(shared_postgres_db):
    """Drop ``updated_by`` on downgrade while the override rows and values survive."""
    sync_url = shared_postgres_db
    extensions_cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    command.upgrade(extensions_cfg, "heads")
    command.upgrade(Config(str(ALEMBIC_INI), ini_section="tasks"), "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settingoverride "
                    "(setting_class, key, value, is_active, created_at, updated_by) "
                    "VALUES ('EXTENSIONS_SETTINGS', 'SYNC_REFRESH_TIME', '5', true, now(), "
                    "'alice')"
                )
            )
    finally:
        engine.dispose()

    command.downgrade(extensions_cfg, _EXTENSIONS_PRE_UPDATED_BY_REVISION)

    assert _updated_by_columns(sync_url) == []
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            surviving = conn.execute(
                text("SELECT value FROM settingoverride WHERE key = :key"),
                {"key": "SYNC_REFRESH_TIME"},
            )
            # ``value`` is JSONB, so the driver decodes the stored ``'5'`` back
            # to a Python int rather than the literal that was inserted.
            assert surviving.scalar_one() == _SEEDED_OVERRIDE_VALUE
    finally:
        engine.dispose()


def _column_exists_in(engine, column_name) -> bool:
    """Report whether ``settingoverride`` declares ``column_name`` on ``engine``.

    :param engine: A connected engine for the database to inspect.
    :param column_name: The column to test for.
    :return: ``True`` when the table declares the column.
    """
    return any(
        column["name"] == column_name
        for column in inspect(engine).get_columns("settingoverride")
    )


@pytest.fixture
def extensions_sqlite_alembic_config(tmp_path, monkeypatch):
    """Return an Alembic ``Config`` and sync URL for the extensions track on temp SQLite.

    SQLite is the default engine, and ``batch_alter_table`` recreates the table
    rather than altering it in place, so the add and the drop take a different
    code path there than the PostgreSQL cases above exercise.

    :param tmp_path: pytest's per-test temporary directory.
    :param monkeypatch: pytest's attribute patcher, pointing the extensions settings at
        the temp database file.
    :return: The extensions-track ``Config`` and the sync URL of the database it targets.
    """
    db_path = tmp_path / "test_extensions.sqlite"
    monkeypatch.setattr(
        extensions_settings.DATABASE, "ENGINE", AsyncDatabaseEngine.SQLITE
    )
    monkeypatch.setattr(extensions_settings.DATABASE, "HOST", "")
    monkeypatch.setattr(extensions_settings.DATABASE, "NAME", str(db_path))
    return Config(str(ALEMBIC_INI), ini_section="extensions"), f"sqlite:///{db_path}"


def test_sqlite_updated_by_round_trips_through_batch_alter(
    extensions_sqlite_alembic_config,
):
    """Add, re-add and drop ``updated_by`` on SQLite, leaving the seeded row intact.

    The drop goes through a batch table rebuild, so this pins that the rebuild
    carries the surviving rows and their values across.
    """
    cfg, sync_url = extensions_sqlite_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        assert _column_exists_in(engine, SETTINGOVERRIDE_UPDATED_BY_COLUMN)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settingoverride "
                    "(setting_class, key, value, is_active, created_at, updated_by) "
                    "VALUES ('EXTENSIONS_SETTINGS', 'SYNC_REFRESH_TIME', '5', 1, "
                    "'2026-01-01 00:00:00', 'alice')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")
    command.downgrade(cfg, _EXTENSIONS_PRE_UPDATED_BY_REVISION)

    engine = create_engine(sync_url)
    try:
        assert not _column_exists_in(engine, SETTINGOVERRIDE_UPDATED_BY_COLUMN)
        with engine.connect() as conn:
            surviving = conn.execute(
                text("SELECT value FROM settingoverride WHERE key = :key"),
                {"key": "SYNC_REFRESH_TIME"},
            )
            assert surviving.scalar_one() == _SEEDED_OVERRIDE_VALUE
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")
    engine = create_engine(sync_url)
    try:
        assert _column_exists_in(engine, SETTINGOVERRIDE_UPDATED_BY_COLUMN)
    finally:
        engine.dispose()
