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

"""Exercise observation ingestion and its upsert against real PostgreSQL.

``om_observation_put`` is PL/pgSQL, so the upsert and the classify-then-store path
run on a real engine: the migration is applied to the test database, then facts are
ingested through it. The cases pin that secrets never reach the stored document or
its digest, that an older result loses to a newer one, and that the composite
foreign key refuses an observation citing a run of another profile.
"""

import asyncio
import json
from datetime import timedelta

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import utc_now
from app.core.utils.fields import AsyncDatabaseEngine
from app.extensions.apps.om_inventory.models import (
    ObservationSubjectKind,
    OM_SCHEMA,
    OmObservation,
    ProbeRun,
    ScanProfile,
)
from app.extensions.apps.om_inventory.observations import ingest_observation
from app.extensions.apps.shared.om.classification import FactClass
from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings
from tests.app.alembic_paths import ALEMBIC_INI

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

#: A value stood in for a secret, searched for across the stored facts and digest.
_SECRET = "SENTINEL-de1e7e-secret"  # nosec B105


@pytest_asyncio.fixture
async def observation_session(postgres_async_url, monkeypatch):
    """Apply the migration to the test database and yield a session on it.

    Point the Extensions settings at the test PostgreSQL, upgrade the extensions
    track so ``om_observation_put`` and the tables exist, and yield a session whose
    engine translates the symbolic OM schema the way the function resolution does.
    Drop and recreate ``public`` on teardown so the next test starts clean.

    :param postgres_async_url: The test database's async URL.
    :param monkeypatch: The fixture used to redirect the Extensions database.
    :return: A session bound to the upgraded test database.
    """
    database = extensions_settings.DATABASE
    monkeypatch.setattr(database, "ENGINE", AsyncDatabaseEngine.POSTGRESQL)
    monkeypatch.setattr(database, "USER", postgres_async_url.username)
    monkeypatch.setattr(
        database,
        "PASSWORD",
        SecretStr(postgres_async_url.password) if postgres_async_url.password else None,
    )
    monkeypatch.setattr(database, "HOST", postgres_async_url.host)
    monkeypatch.setattr(database, "PORT", postgres_async_url.port)
    monkeypatch.setattr(database, "NAME", postgres_async_url.database)

    await asyncio.to_thread(
        command.upgrade, Config(str(ALEMBIC_INI), ini_section="extensions"), "heads"
    )

    engine = create_async_engine(postgres_async_url).execution_options(
        schema_translate_map={OM_SCHEMA: om_schema(database)}
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
        sync = create_engine(postgres_async_url.set(drivername="postgresql+psycopg2"))
        try:
            with sync.begin() as conn:
                conn.exec_driver_sql("DROP SCHEMA public CASCADE")
                conn.exec_driver_sql("CREATE SCHEMA public")
        finally:
            sync.dispose()


async def _seed_run(session: AsyncSession, profile: ScanProfile) -> ProbeRun:
    """Create a scan run of ``profile`` and return it.

    :param session: The session to create through.
    :param profile: The run's scan profile.
    :return: The created run.
    """
    run = ProbeRun(profile=profile)
    session.add(run)
    await session.commit()
    return run


async def _stored(session: AsyncSession, subject_key: str) -> OmObservation:
    """Return the observation stored for a subject key.

    :param session: The session to read through.
    :param subject_key: The subject key to read.
    :return: The stored observation.
    """
    return (
        await session.exec(
            select(OmObservation).where(OmObservation.subject_key == subject_key)
        )
    ).one()


async def test_ingest_stores_only_non_secret_facts(
    observation_session: AsyncSession,
) -> None:
    """Store public facts and drop the secret and the unclassified ones."""
    run = await _seed_run(observation_session, ScanProfile.MANAGED)
    applied = await ingest_observation(
        observation_session,
        subject_kind=ObservationSubjectKind.MEMBER,
        subject_key="m1",
        profile=ScanProfile.MANAGED,
        scan_id=run.id,
        collected_at=utc_now(),
        facts={"host": "node-a", "password": _SECRET, "unlisted": _SECRET},
        classes={"host": FactClass.PUBLIC, "password": FactClass.SECRET},
    )
    await observation_session.commit()
    assert applied is True

    stored = await _stored(observation_session, "m1")
    assert stored.facts == {"host": "node-a"}
    assert _SECRET not in json.dumps(stored.facts)
    assert _SECRET not in stored.digest


async def test_an_older_result_loses_to_a_newer_one(
    observation_session: AsyncSession,
) -> None:
    """Keep the newer result and refuse to overwrite it with an older one."""
    run = await _seed_run(observation_session, ScanProfile.MANAGED)
    newer = utc_now()
    older = newer - timedelta(minutes=5)
    await ingest_observation(
        observation_session,
        subject_kind=ObservationSubjectKind.MEMBER,
        subject_key="m1",
        profile=ScanProfile.MANAGED,
        scan_id=run.id,
        collected_at=newer,
        facts={"host": "new"},
        classes={"host": FactClass.PUBLIC},
    )
    await observation_session.commit()
    applied = await ingest_observation(
        observation_session,
        subject_kind=ObservationSubjectKind.MEMBER,
        subject_key="m1",
        profile=ScanProfile.MANAGED,
        scan_id=run.id,
        collected_at=older,
        facts={"host": "old"},
        classes={"host": FactClass.PUBLIC},
    )
    await observation_session.commit()
    assert applied is False
    stored = await _stored(observation_session, "m1")
    assert stored.facts == {"host": "new"}


async def test_the_composite_fk_refuses_a_cross_profile_run(
    observation_session: AsyncSession,
) -> None:
    """Refuse an observation citing a run collected under another profile."""
    run = await _seed_run(observation_session, ScanProfile.IMPORT_CHECK)
    run_id = run.id
    with pytest.raises(DBAPIError):
        await ingest_observation(
            observation_session,
            subject_kind=ObservationSubjectKind.MEMBER,
            subject_key="m1",
            profile=ScanProfile.MANAGED,
            scan_id=run_id,
            collected_at=utc_now(),
            facts={"host": "node-a"},
            classes={"host": FactClass.PUBLIC},
        )
    await observation_session.rollback()
