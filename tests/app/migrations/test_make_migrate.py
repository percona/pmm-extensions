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

"""Exercise the real ``make migrate`` recipe against isolated PostgreSQL stores."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Engine, inspect, MetaData, String, Table

from tests.app.alembic_paths import ALEMBIC_INI, REPO_ROOT
from tests.app.beat_autogenerate import BEAT_TABLES

pytestmark = [pytest.mark.postgres, pytest.mark.xdist_group("make_migrate")]


@pytest.fixture
def migrate_env(postgres_migration_stores: dict[str, Engine]) -> dict[str, str]:
    """Route every subprocess database connection to an isolated test store.

    :param postgres_migration_stores: Engines for the freshly provisioned databases.
    :return: Subprocess environment with inherited store overrides removed.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(
            (
                "DATABASE",
                "TASKS__DATABASE",
                "INVENTORY__DATABASE",
                "EXTENSIONS__DATABASE",
                "CELERY",
            )
        )
        and key
        not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "GNUMAKEFLAGS", "MAKEFILES"}
    }
    env.update(
        ENV_FILE=str(REPO_ROOT / "tests/pytest.env"),
        SETTINGS_FILE=str(REPO_ROOT / "settings.yaml"),
        FASTAPI_ENV="development",
        SECRETS_DIR="",
        CELERY__BEAT_DBURI=postgres_migration_stores["beat"].url.render_as_string(
            hide_password=False
        ),
        CELERY__BEAT_SCHEMA="public",
    )
    for app in ("tasks", "inventory", "extensions"):
        url = postgres_migration_stores[app].url
        env[f"{app.upper()}__DATABASE"] = json.dumps(
            {
                "ENGINE": "postgresql+asyncpg",
                "HOST": url.host,
                "PORT": url.port,
                "USER": url.username,
                "PASSWORD": url.password,
                "NAME": url.database,
            }
        )
    return env


def _run_migrate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the migration recipe using the already-installed pytest environment.

    Skip only the ``venv`` prerequisite, not any step of the migration recipe.

    :param env: Environment pointing all stores at the isolated databases.
    :return: The target's exit status and captured output.
    """
    return subprocess.run(
        [
            "make",
            "--old-file=venv",
            "migrate",
            f"VENV_BIN={Path(sys.executable).parent}",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def _assert_at_heads(app: str, engine: Engine) -> None:
    """Compare the stored revisions with all heads in the app's migration tree.

    :param app: The Alembic track name.
    :param engine: Engine connected to that track's isolated database.
    """
    scripts = ScriptDirectory.from_config(Config(ALEMBIC_INI, ini_section=app))
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"version_table": f"alembic_version_{app}"}
        )
        assert set(context.get_current_heads()) == set(scripts.get_heads()), app


def test_migrate_upgrades_all_apps_and_bootstraps_beat(
    postgres_migration_stores: dict[str, Engine], migrate_env: dict[str, str]
) -> None:
    """Apply every app head and create the beat tables through ``make``."""
    result = _run_migrate(migrate_env)

    assert result.returncode == 0, result.stdout + result.stderr
    for app in ("tasks", "inventory", "extensions"):
        _assert_at_heads(app, postgres_migration_stores[app])
    assert (
        set(inspect(postgres_migration_stores["beat"]).get_table_names()) >= BEAT_TABLES
    )


def test_migrate_reports_non_final_failure_without_bootstrapping_beat(
    postgres_migration_stores: dict[str, Engine], migrate_env: dict[str, str]
) -> None:
    """Reject a tasks failure even when the final PMM Extensions upgrade succeeds."""
    version = Table(
        "alembic_version_tasks",
        MetaData(),
        Column("version_num", String(32), primary_key=True),
    )
    bad_revision = "missing_test_revision"
    with postgres_migration_stores["tasks"].begin() as connection:
        version.create(connection)
        connection.execute(version.insert().values(version_num=bad_revision))

    result = _run_migrate(migrate_env)

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        f"Can't locate revision identified by '{bad_revision}'"
        in result.stdout + result.stderr
    )
    for app in ("inventory", "extensions"):
        _assert_at_heads(app, postgres_migration_stores[app])
    assert inspect(postgres_migration_stores["beat"]).get_table_names() == []
