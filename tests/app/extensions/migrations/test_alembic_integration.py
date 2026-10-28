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

"""Define end-to-end tests for PMM Extensions' multi-head Alembic configuration.

Exercise ``alembic.command.upgrade``/``downgrade``/``check`` against a
temporary SQLite database using the real ``env.py``, ``_discovery.py``,
``_orphan_heads.py``, and ``alembic.ini`` — the combination that breaks on
any misconfigured ``version_locations`` or plugin-discovery regression.
"""

import io
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from logging.config import dictConfig, fileConfig
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from cryptography.fernet import Fernet
from rich.logging import RichHandler
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.core.celery.bootstrap import bootstrap_beat_schema
from app.core.celery.migrations import BEAT_TABLE_NAMES
from app.core.config import LOGGING_CONFIG, settings
from app.core.db.utils import check_constraint_name
from app.extensions.apps.alerts.models import AlertBackup
from app.extensions.migrations._version_table import (
    PRE_RENAME_VERSION_TABLE,
    VERSION_TABLE,
)
from tests.app.alembic_paths import ALEMBIC_INI, REPO_ROOT
from tests.app.beat_autogenerate import (
    autogenerate_diffs,
    create_beat_tables,
    tables_mentioned,
)
from tests.app.settingoverride_rows import insert_override

from .conftest import ALERTS_HEAD, UNKNOWN_REVISION

# The add_setting_override_table revision on the PMM Extensions track, before SETTINGS /
# ALERT_SETTINGS were added to the setting_class CHECK constraint.
_EXTENSIONS_PRE_ENUM_REVISION = "ed97b99eef38"

# The add_seppluginperiodictask revision: appstate still has the boolean
# ``enabled`` column, before ``lifecycle_state`` replaces it.
_EXTENSIONS_PRE_LIFECYCLE_REVISION = "64f10ead74f6"
# The add_lifecycle_state_to_app_state revision under test.
_EXTENSIONS_LIFECYCLE_REVISION = "a7c4e9f1b2d3"

#: The revision preceding drop_setting_class_check_constraint: downgrading to it
#: is what runs that migration's downgrade.
_SETTING_CLASS_CHECK_PARENT = "d1e2f3a4b5c6"

#: The revision preceding add_sync_run_state_and_entity_absence: syncinstance
#: still lacks the run-level ``status`` and ``snapshot_complete`` columns.
_EXTENSIONS_PRE_SYNC_RUN_STATE_REVISION = "74720aeda25b"
#: The add_sync_run_state_and_entity_absence revision under test.
_EXTENSIONS_SYNC_RUN_STATE_REVISION = "867df844fe17"

#: The revision preceding rename_sep_settings_override_token: override rows
#: for the service settings class are still stored under ``SEP_SETTINGS``.
_EXTENSIONS_PRE_RETOKEN_REVISION = "cbc3026013de"
#: The rename_sep_settings_override_token revision under test.
_EXTENSIONS_RETOKEN_REVISION = "ee2b220c8c73"
#: The rename_app_periodic_task_table revision under test.
_EXTENSIONS_TABLE_RENAME_REVISION = "93cf1ec26fcd"

PRE_RENAME_APP_PERIODIC_TASK_TABLE = "seppluginperiodictask"
"""The ownership table's name before ``93cf1ec26fcd``. A frozen literal."""

PRE_RENAME_SCHEDULE_NAME = "sep__purge_atw_bundles"
"""A schedule name under the prefix used before ``93cf1ec26fcd``. A frozen literal."""

_ORPHAN_HEADS_LOGGER = "app.extensions.migrations._orphan_heads"

#: A table owned by neither SQLModel.metadata nor the beat library, so the
#: narrowness case has something autogenerate is still expected to report.
_ORPHAN_PROBE_TABLE = "extensions_orphan_probe"

# Tokens only — these fixtures check rendering/grepability, not production prose.
_SKIP_NOTICE = (
    "a1b2c3d4e5f6 9f8e7d6c5b4a "
    "app/extensions/apps/alerts/migrations/versions "
    "app/extensions/apps/dipper/migrations/versions"
)
_SKEW_NOTICE = f"{UNKNOWN_REVISION} present on disk"


@contextmanager
def _app_then_alembic_logging(stream: io.StringIO) -> Iterator[logging.Logger]:
    """Install app Rich logging, then alembic fileConfig, redirect console to ``stream``.

    Reproduces the real order: settings-driven ``dictConfig`` runs during env
    imports, then ``fileConfig(..., disable_existing_loggers=False)`` runs in
    ``env.py``. Redirect the alembic console handler stream so assertions see
    rendered bytes, not ``caplog`` records. Restore ``LOGGING_CONFIG`` on exit so
    handlers/levels do not leak into later tests.
    """
    try:
        dictConfig(LOGGING_CONFIG)
        fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, RichHandler
            ):
                handler.setStream(stream)
        app_logger = logging.getLogger("app")
        for handler in app_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(stream)
        yield logging.getLogger(_ORPHAN_HEADS_LOGGER)
    finally:
        dictConfig(LOGGING_CONFIG)


def test_app_logger_after_alembic_fileconfig_uses_console_not_rich():
    """Assert alembic fileConfig replaces RichHandler with the console StreamHandler."""
    stream = io.StringIO()
    with _app_then_alembic_logging(stream):
        app_logger = logging.getLogger("app")
        assert not app_logger.propagate
        assert app_logger.handlers, "app logger must keep an explicit handler"
        assert not any(isinstance(h, RichHandler) for h in app_logger.handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in app_logger.handlers)


def test_orphan_skip_warning_renders_as_single_greppable_line():
    """Skip notice stays one line with every revision id and path greppable."""
    stream = io.StringIO()
    with _app_then_alembic_logging(stream) as logger:
        logger.warning(_SKIP_NOTICE)
    output = stream.getvalue()
    matching = [line for line in output.splitlines() if "a1b2c3d4e5f6" in line]
    assert len(matching) == 1, output
    line = matching[0]
    assert "\n" not in line
    for token in (
        "a1b2c3d4e5f6",
        "9f8e7d6c5b4a",
        "app/extensions/apps/alerts/migrations/versions",
        "app/extensions/apps/dipper/migrations/versions",
        "app.extensions.migrations._orphan_heads",
    ):
        assert token in line, (token, line)
    # generic formatter: levelname truncated to 5 chars
    assert "WARNI" in line


def test_version_skew_error_renders_as_single_greppable_line():
    """Render the version-skew ERROR as one greppable console line."""
    stream = io.StringIO()
    with _app_then_alembic_logging(stream) as logger:
        logger.error(_SKEW_NOTICE)
    output = stream.getvalue()
    matching = [line for line in output.splitlines() if UNKNOWN_REVISION in line]
    assert len(matching) == 1, output
    line = matching[0]
    assert UNKNOWN_REVISION in line
    assert "present on disk" in line
    assert "app.extensions.migrations._orphan_heads" in line
    assert "ERROR" in line


#: Frozen copy of the members the CHECK enumerated right before the drop.
_PRE_DROP_MEMBERS = (
    "SEP_SETTINGS",
    "TASKS_SETTINGS",
    "SNIPPETS_SETTINGS",
    "SETTINGS",
    "ALERT_SETTINGS",
    "ANONYMIZER_SETTINGS",
    "ALERTS_SETTINGS",
    "INVENTORY_SETTINGS",
    "HEALTH_REPORT_SETTINGS",
)


def _insert_appstate_enabled(conn, app_key: str, enabled: int) -> None:
    """Insert an ``appstate`` row using the pre-lifecycle ``enabled`` column."""
    conn.exec_driver_sql(
        "INSERT INTO appstate (created_at, app_key, enabled) "
        "VALUES ('2026-01-01 00:00:00', ?, ?)",
        (app_key, enabled),
    )


def _get_stamped_revisions(sync_url: str) -> set[str]:
    """Return the set of revisions stamped in ``alembic_version_extensions``.

    :param sync_url: Sync SQLAlchemy URL to the test database.
    :type sync_url: str
    :return: All ``version_num`` values currently in the version table.
    :rtype: set[str]
    """
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            if "alembic_version_extensions" not in inspect(conn).get_table_names():
                return set()
            rows = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version_extensions"
            ).fetchall()
            return {row[0] for row in rows}
    finally:
        engine.dispose()


@pytest.fixture
def extensions_alembic_config_stripped_alerts(extensions_alembic_config, tmp_path):
    """Return an Alembic ``Config`` whose alerts version location is gone from disk.

    Reproduce a stripped image. ``alembic.ini`` is generated at commit time and
    ships an entry for every app that owns migrations, so removing an app removes
    its ``versions/`` directory, not the ini line — the entry stays configured
    while pointing nowhere. Dropping the entry instead would leave every
    configured location present, which is the fail-closed case rather than a
    stripped app.

    The returned config addresses the same temp SQLite database as
    ``extensions_alembic_config``.

    :param extensions_alembic_config: The full-config fixture whose database is shared.
    :param tmp_path: Pytest's per-test temporary directory.
    :return: A tuple of (Config, sync sqlite URL, the absent alerts path).
    """
    _, sync_url = extensions_alembic_config
    absent = tmp_path / "stripped" / "alerts" / "migrations" / "versions"
    cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    locations = ScriptDirectory.from_config(cfg).version_locations
    cfg.set_main_option(
        "version_locations",
        ":".join(
            str(absent) if "alerts" in location else location for location in locations
        ),
    )
    return cfg, sync_url, str(absent)


@pytest.fixture
def extensions_alembic_config_empty_alerts_versions(
    extensions_alembic_config, tmp_path
):
    """Return a Config whose alerts location exists on disk but has no revisions.

    The configured ``versions/`` directory is present but contributes no scripts
    to the revision map — the shared evidence for both a package that lost its
    ``__init__.py`` while leaving ``versions/``, and a package left intact with
    an empty ``versions/``.

    :param extensions_alembic_config: The full-config fixture whose database is shared.
    :param tmp_path: Pytest's per-test temporary directory.
    :return: A tuple of (Config, sync sqlite URL, the empty alerts path).
    """
    _, sync_url = extensions_alembic_config
    empty = tmp_path / "empty" / "alerts" / "migrations" / "versions"
    empty.mkdir(parents=True)
    cfg = Config(str(ALEMBIC_INI), ini_section="extensions")
    locations = ScriptDirectory.from_config(cfg).version_locations
    cfg.set_main_option(
        "version_locations",
        ":".join(
            str(empty) if "alerts" in location else location for location in locations
        ),
    )
    return cfg, sync_url, str(empty)


def _stamp_extra_revision(sync_url: str, revision: str) -> None:
    """Insert an extra ``alembic_version_extensions`` row for the given revision.

    :param sync_url: Sync SQLAlchemy URL to the test database.
    :param revision: The revision id to stamp.
    """
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO alembic_version_extensions (version_num) VALUES (?)",
                (revision,),
            )
    finally:
        engine.dispose()


def _get_table_names(sync_url: str) -> set[str]:
    """Return the table names present in the test database.

    :param sync_url: Sync SQLAlchemy URL to the test database.
    :return: Every table name the database currently holds.
    """
    engine = create_engine(sync_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_succeeds_when_a_recorded_head_has_no_migration_script(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts
):
    """Upgrade a database whose alerts head no longer resolves."""
    full_cfg, _ = extensions_alembic_config
    stripped_cfg, _, _ = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")

    command.upgrade(stripped_cfg, "heads")


def test_upgrade_preserves_the_unresolvable_row(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts
):
    """Leave the orphaned alerts row in place so a returning app resumes from it."""
    full_cfg, sync_url = extensions_alembic_config
    stripped_cfg, _, _ = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")

    command.upgrade(stripped_cfg, "heads")

    assert ALERTS_HEAD in _get_stamped_revisions(sync_url)


def test_upgrade_logs_every_skipped_revision_id(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts, capsys
):
    """Name each skipped revision, and the absent location, in one warning."""
    full_cfg, _ = extensions_alembic_config
    stripped_cfg, _, absent_path = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")
    capsys.readouterr()

    command.upgrade(stripped_cfg, "heads")
    err = capsys.readouterr().err
    matching = [
        line
        for line in err.splitlines()
        if _ORPHAN_HEADS_LOGGER in line and "Skipping" in line
    ]
    assert len(matching) == 1, err
    assert ALERTS_HEAD in matching[0]
    assert absent_path in matching[0]


def test_upgrade_applies_another_branch_while_preserving_the_orphan_row(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts
):
    """Advance the extensions_main branch without disturbing the orphaned alerts row."""
    full_cfg, sync_url = extensions_alembic_config
    stripped_cfg, _, _ = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")
    command.downgrade(full_cfg, _EXTENSIONS_PRE_LIFECYCLE_REVISION)

    command.upgrade(stripped_cfg, "heads")

    engine = create_engine(sync_url)
    try:
        columns = {col["name"] for col in inspect(engine).get_columns("appstate")}
    finally:
        engine.dispose()
    assert "lifecycle_state" in columns
    assert ALERTS_HEAD in _get_stamped_revisions(sync_url)


def test_returning_app_resumes_from_the_preserved_row(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts
):
    """Resume the alerts branch from its preserved row once the app comes back.

    The returning upgrade finds the branch already at its head, so the rows
    written while the app was stripped survive untouched.
    """
    full_cfg, sync_url = extensions_alembic_config
    stripped_cfg, _, _ = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")
    command.upgrade(stripped_cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                'INSERT INTO alert_backup (created_at, data, "metadata") '
                "VALUES ('2026-01-01 00:00:00', '{\"kept\": true}', '{}')"
            )
    finally:
        engine.dispose()

    command.upgrade(full_cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            rows = [
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT data FROM alert_backup"
                ).fetchall()
            ]
    finally:
        engine.dispose()
    assert rows == ['{"kept": true}']
    assert ALERTS_HEAD in _get_stamped_revisions(sync_url)


def test_fresh_db_with_every_branch_present_logs_nothing(
    extensions_alembic_config, capsys
):
    """Leave a full-image upgrade on its existing code path, silently."""
    cfg, sync_url = extensions_alembic_config

    command.upgrade(cfg, "heads")
    err = capsys.readouterr().err

    table_names = _get_table_names(sync_url)
    assert "alert_backup" in table_names
    assert "snippet" in table_names
    assert not any(_ORPHAN_HEADS_LOGGER in line for line in err.splitlines())


def test_fresh_db_with_a_stripped_config_materializes_present_branches_only(
    extensions_alembic_config_stripped_alerts,
):
    """Create the present branches' tables and none of the stripped app's."""
    stripped_cfg, sync_url, _ = extensions_alembic_config_stripped_alerts

    command.upgrade(stripped_cfg, "heads")

    table_names = _get_table_names(sync_url)
    assert "snippet" in table_names
    assert "alert_backup" not in table_names


def test_unknown_revision_is_not_skipped_when_every_location_is_present(
    extensions_alembic_config,
):
    """Refuse to filter version skew, so Alembic rejects it exactly as today."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")
    _stamp_extra_revision(sync_url, UNKNOWN_REVISION)

    with pytest.raises(CommandError, match=UNKNOWN_REVISION):
        command.upgrade(cfg, "heads")


def test_refusal_to_skip_is_explained_before_alembic_raises(
    extensions_alembic_config, capsys
):
    """Log an error naming the unresolved id and why it was left in place."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")
    _stamp_extra_revision(sync_url, UNKNOWN_REVISION)
    capsys.readouterr()

    with pytest.raises(CommandError):
        command.upgrade(cfg, "heads")

    err = capsys.readouterr().err
    matching = [
        line
        for line in err.splitlines()
        if _ORPHAN_HEADS_LOGGER in line and UNKNOWN_REVISION in line
    ]
    assert len(matching) == 1, err
    assert "present on disk" in matching[0]
    assert "ERROR" in matching[0]


def test_a_stripped_app_and_version_skew_are_skipped_together(
    extensions_alembic_config, extensions_alembic_config_stripped_alerts, capsys
):
    """Skip both orphan kinds once a configured location is missing, naming each."""
    full_cfg, sync_url = extensions_alembic_config
    stripped_cfg, _, _ = extensions_alembic_config_stripped_alerts
    command.upgrade(full_cfg, "heads")
    _stamp_extra_revision(sync_url, UNKNOWN_REVISION)
    capsys.readouterr()

    command.upgrade(stripped_cfg, "heads")
    err = capsys.readouterr().err
    matching = [
        line
        for line in err.splitlines()
        if _ORPHAN_HEADS_LOGGER in line and "Skipping" in line
    ]
    assert len(matching) == 1, err
    assert ALERTS_HEAD in matching[0]
    assert UNKNOWN_REVISION in matching[0]
    stamped = _get_stamped_revisions(sync_url)
    assert {ALERTS_HEAD, UNKNOWN_REVISION} <= stamped


def test_upgrade_succeeds_when_versions_dir_exists_but_is_empty(
    extensions_alembic_config, extensions_alembic_config_empty_alerts_versions
):
    """Upgrade when alerts' versions/ is present on disk but contributes nothing."""
    full_cfg, _ = extensions_alembic_config
    empty_cfg, _, _ = extensions_alembic_config_empty_alerts_versions
    command.upgrade(full_cfg, "heads")

    command.upgrade(empty_cfg, "heads")


def test_upgrade_preserves_the_unresolvable_row_when_versions_dir_is_empty(
    extensions_alembic_config, extensions_alembic_config_empty_alerts_versions
):
    """Keep the orphaned alerts row when the filter arms on an empty versions/."""
    full_cfg, sync_url = extensions_alembic_config
    empty_cfg, _, _ = extensions_alembic_config_empty_alerts_versions
    command.upgrade(full_cfg, "heads")

    command.upgrade(empty_cfg, "heads")

    assert ALERTS_HEAD in _get_stamped_revisions(sync_url)


def test_upgrade_logs_empty_location_that_armed_the_filter(
    extensions_alembic_config, extensions_alembic_config_empty_alerts_versions, capsys
):
    """Name the empty versions/ location in the skip WARNING."""
    full_cfg, _ = extensions_alembic_config
    empty_cfg, _, empty_path = extensions_alembic_config_empty_alerts_versions
    command.upgrade(full_cfg, "heads")
    capsys.readouterr()

    command.upgrade(empty_cfg, "heads")
    err = capsys.readouterr().err
    matching = [
        line
        for line in err.splitlines()
        if _ORPHAN_HEADS_LOGGER in line and "Skipping" in line
    ]
    assert len(matching) == 1, err
    assert ALERTS_HEAD in matching[0]
    assert empty_path in matching[0]
    assert "contributed no revisions" in matching[0]


def test_alembic_upgrade_heads_fresh_db_creates_alert_backup(extensions_alembic_config):
    """Run ``upgrade heads`` on a fresh DB to materialize both branches."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alert_backup" in table_names
    assert "snippet" in table_names


def test_alembic_upgrade_idempotent_on_existing_table(extensions_alembic_config):
    """Tolerate ``alert_backup`` left over from pre-migration runs."""
    cfg, sync_url = extensions_alembic_config

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            AlertBackup.__table__.create(conn)
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        assert "alert_backup" in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_alembic_downgrade_alerts_to_base_drops_table(extensions_alembic_config):
    """Run ``downgrade alerts@base`` to drop the alerts branch only."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")

    command.downgrade(cfg, "alerts@base")

    engine = create_engine(sync_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alert_backup" not in table_names
    assert "snippet" in table_names
    stamped = _get_stamped_revisions(sync_url)
    script = ScriptDirectory.from_config(cfg)
    extensions_main_heads = {
        rev.revision for rev in script.get_revisions("extensions_main@heads")
    }
    alerts_heads = {rev.revision for rev in script.get_revisions("alerts@heads")}
    assert not (alerts_heads & stamped)
    assert extensions_main_heads & stamped


def test_setting_class_accepts_unregistered_token_after_upgrade(
    extensions_alembic_config,
):
    """Accept a token never listed in the CHECK once ``upgrade heads`` has run."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")

    unregistered = ("CUSTOM_PLUGIN_SETTINGS", "APP_OWNED_SETTINGS")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for member in unregistered:
                insert_override(conn, member)
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM settingoverride"
            ).scalar()
        assert count == len(unregistered)
    finally:
        engine.dispose()


def test_setting_class_check_rejects_unlisted_token_before_drop(
    extensions_alembic_config,
):
    """Reject a SETTINGS row at the pre-enum revision, whose CHECK excludes it."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_PRE_ENUM_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            insert_override(conn, "SETTINGS")
    finally:
        engine.dispose()


def test_setting_class_check_is_dropped_after_upgrade(extensions_alembic_config):
    """Leave ``setting_class`` an unconstrained string after ``upgrade heads``."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            unconstrained = ("UNREGISTERED_SETTINGS", "X" * 50)
            assert (
                check_constraint_name(conn, "settingoverride", "setting_class") is None
            )
            for token in unconstrained:
                insert_override(conn, token)
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM settingoverride"
            ).scalar()
        assert count == len(unconstrained)
    finally:
        engine.dispose()


def test_setting_class_check_downgrade_deletes_unknown_rows(
    extensions_alembic_config, capsys
):
    """Delete out-of-list rows on downgrade, log the count, and restore the CHECK."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            insert_override(conn, "UNREGISTERED_SETTINGS")
            insert_override(conn, "EXTENSIONS_SETTINGS")
            for token in _PRE_DROP_MEMBERS:
                if token != "SEP_SETTINGS":
                    insert_override(conn, token)
    finally:
        engine.dispose()

    # alembic fileConfig routes ``app.*`` to its console handler with
    # ``propagate = 0``, so the delete notice is on stderr, not in caplog.
    capsys.readouterr()
    # The revision under test is named outright: a head-relative target silently
    # retargets this assertion at whatever migration lands on extensions_main next.
    command.downgrade(cfg, _SETTING_CLASS_CHECK_PARENT)
    assert "Deleted 1 settingoverride row(s)" in capsys.readouterr().err

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert (
                check_constraint_name(conn, "settingoverride", "setting_class")
                == "settingclassenum"
            )
            remaining = conn.exec_driver_sql(
                "SELECT setting_class FROM settingoverride"
            ).fetchall()
            assert sorted(row[0] for row in remaining) == sorted(_PRE_DROP_MEMBERS)
            with pytest.raises(IntegrityError):
                insert_override(conn, "UNREGISTERED_SETTINGS")
    finally:
        engine.dispose()


def _setting_classes(sync_url: str) -> list[str]:
    """Return every stored ``settingoverride.setting_class``, sorted."""
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            rows = conn.exec_driver_sql(
                "SELECT setting_class FROM settingoverride"
            ).fetchall()
    finally:
        engine.dispose()
    return sorted(row[0] for row in rows)


def test_retoken_revision_moves_the_service_settings_rows_and_back(
    extensions_alembic_config,
):
    """Rewrite ``SEP_SETTINGS`` rows to ``EXTENSIONS_SETTINGS`` and restore them.

    A row of another class is seeded alongside to prove the rewrite is scoped to
    the renamed token.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_PRE_RETOKEN_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            insert_override(conn, "SEP_SETTINGS")
            insert_override(conn, "TASKS_SETTINGS")
    finally:
        engine.dispose()

    command.upgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)
    assert _setting_classes(sync_url) == ["EXTENSIONS_SETTINGS", "TASKS_SETTINGS"]

    command.downgrade(cfg, _EXTENSIONS_PRE_RETOKEN_REVISION)
    assert _setting_classes(sync_url) == ["SEP_SETTINGS", "TASKS_SETTINGS"]


def _stored_value(sync_url: str) -> dict[str, str]:
    """Return the decoded value of the single stored ``settingoverride`` row."""
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            raw = conn.exec_driver_sql("SELECT value FROM settingoverride").scalar_one()
    finally:
        engine.dispose()
    return json.loads(raw)


def test_retoken_revision_moves_the_ciphertext_marker_forward_only(
    extensions_alembic_config,
):
    """Move ``sep.enc.v1.`` envelopes to ``extensions.enc.v1.`` and keep them there.

    The credential URL carries its marked token inside the userinfo segment, so
    the rewrite has to reach an envelope that is not the whole leaf. Plaintext
    carrying the old marker rides along in the same row, as a whole leaf over a
    non-token payload, as a URL password over one, and as a prefix in the middle
    of prose, to prove only a well-formed envelope moves. The downgrade keeps
    the new marker because the earlier revisions' downgrades run this release's
    code, which reads the new marker alone.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_PRE_RETOKEN_REVISION)
    token = Fernet(Fernet.generate_key()).encrypt(b"secret").decode()
    plaintext = {
        "plain_leaf": "sep.enc.v1.operator-secret",
        "plain_endpoint": "https://user:sep.enc.v1.operator-secret@pmm.example.com/",
        "note": f"rotate sep.enc.v1.{token} next week",
        "name": "plain",
    }
    legacy = {
        "password": f"sep.enc.v1.{token}",
        "endpoint": f"https://user:sep.enc.v1.{token}@pmm.example.com/",
        **plaintext,
    }

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO settingoverride "
                "(created_at, setting_class, key, value, is_active) "
                "VALUES ('2026-01-01 00:00:00', 'SEP_SETTINGS', 'X', ?, 1)",
                (json.dumps(legacy),),
            )
    finally:
        engine.dispose()

    migrated = {
        "password": f"extensions.enc.v1.{token}",
        "endpoint": f"https://user:extensions.enc.v1.{token}@pmm.example.com/",
        **plaintext,
    }
    command.upgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)
    assert _stored_value(sync_url) == migrated

    command.downgrade(cfg, _EXTENSIONS_PRE_RETOKEN_REVISION)
    assert _stored_value(sync_url) == migrated


def test_app_lifecycle_backfill_maps_enabled_to_state(extensions_alembic_config):
    """The lifecycle migration backfills ``enabled`` into ``lifecycle_state``."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_PRE_LIFECYCLE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            _insert_appstate_enabled(conn, "snippets", 1)
            _insert_appstate_enabled(conn, "checksums", 0)
    finally:
        engine.dispose()

    command.upgrade(cfg, _EXTENSIONS_LIFECYCLE_REVISION)

    engine = create_engine(sync_url)
    try:
        columns = {col["name"] for col in inspect(engine).get_columns("appstate")}
        with engine.begin() as conn:
            rows = dict(
                conn.exec_driver_sql(
                    "SELECT app_key, lifecycle_state FROM appstate"
                ).fetchall()
            )
    finally:
        engine.dispose()

    assert "enabled" not in columns
    assert "lifecycle_state" in columns
    assert rows == {"snippets": "ENABLED", "checksums": "DISABLED"}


def test_app_lifecycle_check_rejects_unknown_state(extensions_alembic_config):
    """After upgrade, a bogus ``lifecycle_state`` violates the CHECK constraint."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_LIFECYCLE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.exec_driver_sql(
                "INSERT INTO appstate (created_at, app_key, lifecycle_state) "
                "VALUES ('2026-01-01 00:00:00', 'snippets', 'BOGUS')"
            )
    finally:
        engine.dispose()


def test_app_lifecycle_downgrade_restores_enabled(extensions_alembic_config):
    """Downgrading the lifecycle migration restores the boolean ``enabled`` column."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_LIFECYCLE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO appstate (created_at, app_key, lifecycle_state) "
                "VALUES ('2026-01-01 00:00:00', 'snippets', 'ENABLED')"
            )
            conn.exec_driver_sql(
                "INSERT INTO appstate (created_at, app_key, lifecycle_state) "
                "VALUES ('2026-01-01 00:00:00', 'checksums', 'DISABLING')"
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, _EXTENSIONS_PRE_LIFECYCLE_REVISION)

    engine = create_engine(sync_url)
    try:
        columns = {col["name"] for col in inspect(engine).get_columns("appstate")}
        with engine.begin() as conn:
            rows = dict(
                conn.exec_driver_sql("SELECT app_key, enabled FROM appstate").fetchall()
            )
    finally:
        engine.dispose()

    assert "lifecycle_state" not in columns
    assert "enabled" in columns
    assert rows == {"snippets": 1, "checksums": 0}


def _insert_sync_instance(conn, instance_id: str) -> None:
    """Insert a ``syncinstance`` row using the pre-run-state column set."""
    conn.exec_driver_sql(
        "INSERT INTO syncinstance (id, created_at, syncer) "
        "VALUES (?, '2026-01-01 00:00:00', 'PMMSyncer')",
        (instance_id,),
    )


def _insert_sync_item(conn, item_id: str, instance_id: str, status: str) -> None:
    """Insert a ``syncitem`` row belonging to the given instance."""
    conn.exec_driver_sql(
        "INSERT INTO syncitem "
        "(id, created_at, entity_type, status, sync_instance_id) "
        "VALUES (?, '2026-01-01 00:00:00', 'INVENTORY', ?, ?)",
        (item_id, status, instance_id),
    )


def test_sync_run_state_backfill_derives_a_verdict_per_historical_run(
    extensions_alembic_config,
):
    """Derive ``status`` for historical runs by the rule finalize_run applies."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_PRE_SYNC_RUN_STATE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            _insert_sync_instance(conn, "all-success")
            _insert_sync_item(conn, "s1", "all-success", "SUCCESS")
            _insert_sync_item(conn, "s2", "all-success", "SUCCESS")
            _insert_sync_instance(conn, "one-failed")
            _insert_sync_item(conn, "f1", "one-failed", "SUCCESS")
            _insert_sync_item(conn, "f2", "one-failed", "FAILED")
            _insert_sync_instance(conn, "still-running")
            _insert_sync_item(conn, "r1", "still-running", "RUNNING")
            _insert_sync_instance(conn, "no-items")
    finally:
        engine.dispose()

    command.upgrade(cfg, _EXTENSIONS_SYNC_RUN_STATE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            rows = dict(
                conn.exec_driver_sql("SELECT id, status FROM syncinstance").fetchall()
            )
            complete = dict(
                conn.exec_driver_sql(
                    "SELECT id, snapshot_complete FROM syncinstance"
                ).fetchall()
            )
    finally:
        engine.dispose()

    assert rows == {
        "all-success": "SUCCESS",
        "one-failed": "FAILED",
        "still-running": "PENDING",
        "no-items": "PENDING",
    }
    assert set(complete.values()) == {None}


def test_sync_run_state_status_check_rejects_unknown_value(extensions_alembic_config):
    """Reject a bogus ``syncinstance.status`` through the CHECK constraint."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_SYNC_RUN_STATE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.exec_driver_sql(
                "INSERT INTO syncinstance (id, created_at, syncer, status) "
                "VALUES ('bogus', '2026-01-01 00:00:00', 'PMMSyncer', 'NOPE')"
            )
    finally:
        engine.dispose()


def test_sync_run_state_status_server_default_survives_the_migration(
    extensions_alembic_config,
):
    """Accept an insert omitting ``status``, as a pre-rollout release issues."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_SYNC_RUN_STATE_REVISION)

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            _insert_sync_instance(conn, "old-code")
            status = conn.exec_driver_sql(
                "SELECT status FROM syncinstance WHERE id = 'old-code'"
            ).scalar_one()
    finally:
        engine.dispose()

    assert status == "PENDING"


def test_sync_run_state_downgrade_drops_the_added_state(extensions_alembic_config):
    """Remove both run-state columns and the absence ledger on downgrade."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_SYNC_RUN_STATE_REVISION)
    command.downgrade(cfg, _EXTENSIONS_PRE_SYNC_RUN_STATE_REVISION)

    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("syncinstance")}
        tables = set(inspector.get_table_names())
    finally:
        engine.dispose()

    assert "status" not in columns
    assert "snapshot_complete" not in columns
    assert "syncentityabsence" not in tables
    assert "syncitem" in tables


def test_autogenerate_ignores_the_beat_tables(extensions_alembic_config):
    """Keep the schedule tables out of the PMM Extensions track's proposed operations."""
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")
    create_beat_tables(sync_url)
    assert _get_table_names(sync_url) >= BEAT_TABLE_NAMES

    diffs = autogenerate_diffs(cfg)

    assert tables_mentioned(diffs, BEAT_TABLE_NAMES) == []


def test_autogenerate_still_reports_a_table_no_track_owns(extensions_alembic_config):
    """Report an orphaned PMM Extensions table while the beat tables stay excluded.

    The rejected blanket recipe — drop every reflected object with no metadata
    counterpart — would silence the probe too, so both halves are asserted. The
    probe is in no metadata at all, so ``remove_table`` is the only operation
    that can name it.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")
    create_beat_tables(sync_url)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"CREATE TABLE {_ORPHAN_PROBE_TABLE} (id INTEGER)")
    finally:
        engine.dispose()

    diffs = autogenerate_diffs(cfg)

    assert tables_mentioned(diffs, [_ORPHAN_PROBE_TABLE]) == [_ORPHAN_PROBE_TABLE]
    assert tables_mentioned(diffs, BEAT_TABLE_NAMES) == []


def test_bootstrap_after_upgrade_leaves_the_beat_tables_unproposed(
    extensions_alembic_config, monkeypatch
):
    """Run the ``migrate`` order — upgrade, then bootstrap — against one store.

    This is the sequence ``make migrate`` now performs when the beat store
    resolves to a track's own database, so the tables reach the sweep from the
    real bootstrap rather than from the test.
    """
    cfg, sync_url = extensions_alembic_config
    monkeypatch.setattr(settings.CELERY, "beat_dburi", sync_url)
    monkeypatch.setattr(settings.CELERY, "beat_schema", None)
    command.upgrade(cfg, "heads")

    bootstrap_beat_schema()

    assert _get_table_names(sync_url) >= BEAT_TABLE_NAMES
    assert tables_mentioned(autogenerate_diffs(cfg), BEAT_TABLE_NAMES) == []


def test_check_is_clean_after_upgrade_to_heads(tmp_path: Path) -> None:
    """Report models and migrations in sync once every branch is applied.

    Run the CLI in a subprocess rather than ``command.check`` in-process: this
    process imports every service's models into the one ``SQLModel.metadata``,
    so an in-process check on the extensions track would report the tasks and inventory
    tables as missing. The CLI process imports only what ``env.py`` imports,
    which is exactly what ``make checkmigrations`` runs. OM's tables declare a
    symbolic schema, so this is also where the translated comparison is proven.
    """
    env = {
        **os.environ,
        "EXTENSIONS__DATABASE__HOST": "",
        "EXTENSIONS__DATABASE__NAME": str(tmp_path / "extensions.sqlite"),
    }
    for verb in (("upgrade", "heads"), ("check",)):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "--name", "extensions", *verb],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def _app_periodic_task_rows(sync_url: str, table: str) -> list[tuple[str, int]]:
    """Return every ``(periodic_task_name, user_enabled)`` row of ``table``, sorted."""
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            rows = conn.exec_driver_sql(
                f"SELECT periodic_task_name, user_enabled FROM {table}"
            ).fetchall()
    finally:
        engine.dispose()
    return sorted((name, enabled) for name, enabled in rows)


def test_table_rename_revision_moves_the_rows_and_their_names_and_back(
    extensions_alembic_config,
):
    """Rename the ownership table and its old-prefixed names, keeping each override.

    A name outside the old prefix rides along to prove the rewrite is scoped to
    it, and the disabled row proves the operator's choice survives the move.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for name, enabled in ((PRE_RENAME_SCHEDULE_NAME, 0), ("custom", 1)):
                conn.exec_driver_sql(
                    f"INSERT INTO {PRE_RENAME_APP_PERIODIC_TASK_TABLE} "
                    "(created_at, periodic_task_name, app_key, user_enabled) "
                    "VALUES ('2026-01-01 00:00:00', ?, 'atw', ?)",
                    (name, enabled),
                )
    finally:
        engine.dispose()

    command.upgrade(cfg, _EXTENSIONS_TABLE_RENAME_REVISION)

    tables = _get_table_names(sync_url)
    assert "extensionsappperiodictask" in tables
    assert PRE_RENAME_APP_PERIODIC_TASK_TABLE not in tables
    assert _app_periodic_task_rows(sync_url, "extensionsappperiodictask") == [
        ("custom", 1),
        ("extensions__purge_atw_bundles", 0),
    ]

    command.downgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)

    tables = _get_table_names(sync_url)
    assert PRE_RENAME_APP_PERIODIC_TASK_TABLE in tables
    assert "extensionsappperiodictask" not in tables
    assert _app_periodic_task_rows(sync_url, PRE_RENAME_APP_PERIODIC_TASK_TABLE) == [
        ("custom", 1),
        (PRE_RENAME_SCHEDULE_NAME, 0),
    ]


PRE_RENAME_SYNCER = "app.sep.sync.syncers.pmm.PMMSyncer"
"""A syncer's stored name before ``93cf1ec26fcd``. A frozen literal."""


def _stored_syncers(sync_url: str) -> dict[str, list[str]]:
    """Return the sorted ``syncer`` values of each table that stores one.

    :param sync_url: The synchronous database URL.
    :return: ``{table: syncers}`` for the run and entity-absence tables.
    """
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            return {
                table: sorted(
                    syncer
                    for (syncer,) in conn.exec_driver_sql(f"SELECT syncer FROM {table}")
                )
                for table in ("syncinstance", "syncentityabsence")
            }
    finally:
        engine.dispose()


def test_table_rename_revision_moves_the_stored_syncers_and_back(
    extensions_alembic_config,
):
    """Move a syncer stored under the old package to the new one, and back.

    A syncer from another package rides along to prove the rewrite is scoped to
    the old one.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)
    other = "custom.syncers.Syncer"
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for index, syncer in enumerate((PRE_RENAME_SYNCER, other)):
                conn.exec_driver_sql(
                    "INSERT INTO syncinstance (id, created_at, syncer, status) "
                    "VALUES (?, '2026-01-01 00:00:00', ?, 'SUCCESS')",
                    (f"{index:032x}", syncer),
                )
                conn.exec_driver_sql(
                    "INSERT INTO syncentityabsence "
                    "(created_at, syncer, entity_type, entity_id, missing_generations) "
                    "VALUES ('2026-01-01 00:00:00', ?, 'NODE', 1, 2)",
                    (syncer,),
                )
    finally:
        engine.dispose()

    command.upgrade(cfg, _EXTENSIONS_TABLE_RENAME_REVISION)

    renamed = sorted(["app.extensions.sync.syncers.pmm.PMMSyncer", other])
    assert _stored_syncers(sync_url) == {
        "syncinstance": renamed,
        "syncentityabsence": renamed,
    }

    command.downgrade(cfg, _EXTENSIONS_RETOKEN_REVISION)

    restored = sorted([PRE_RENAME_SYNCER, other])
    assert _stored_syncers(sync_url) == {
        "syncinstance": restored,
        "syncentityabsence": restored,
    }


def test_the_main_branch_is_addressed_by_its_current_label(extensions_alembic_config):
    """Resolve ``extensions_main@head`` to the head of the track's main branch."""
    cfg, _ = extensions_alembic_config
    script = ScriptDirectory.from_config(cfg)

    heads = [rev.revision for rev in script.get_revisions("extensions_main@heads")]
    ancestry = {
        rev.revision
        for rev in script.walk_revisions(base="base", head="extensions_main@head")
    }

    assert len(heads) == 1
    assert _EXTENSIONS_TABLE_RENAME_REVISION in ancestry


def test_upgrade_adopts_the_pre_rename_version_table(extensions_alembic_config):
    """Resume a database that recorded its heads under the pre-rename table name.

    Read through the new name alone, the database would look empty and have its
    first revision replayed over tables it already holds, which fails.
    """
    cfg, sync_url = extensions_alembic_config
    command.upgrade(cfg, "heads")
    stamped = _get_stamped_revisions(sync_url)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"ALTER TABLE {VERSION_TABLE} RENAME TO {PRE_RENAME_VERSION_TABLE}"
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "heads")

    tables = _get_table_names(sync_url)
    assert VERSION_TABLE in tables
    assert PRE_RENAME_VERSION_TABLE not in tables
    assert _get_stamped_revisions(sync_url) == stamped
