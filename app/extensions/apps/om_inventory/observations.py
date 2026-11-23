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

"""Ingest a scan's facts into an observation, dropping secrets on the way in.

The scan produces raw facts and their declared classes; this classifies them with
:func:`ingestible_facts` so every secret and unclassified value is dropped before
anything is stored, then records the surviving non-secret facts through
``om_observation_put``. That function is PL/pgSQL and keeps the newer of two
results for a subject, so it is reached by name on a PostgreSQL bind through the
resolved OM schema, as the dispatch receipt is.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, Text
from sqlalchemy.dialects import postgresql
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory.models import (
    ObservationSubjectKind,
    ScanProfile,
)
from app.extensions.apps.shared.om.classification import FactClass, ingestible_facts
from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings

__all__ = ["ingest_observation"]


async def ingest_observation(
    session: AsyncSession,
    *,
    subject_kind: ObservationSubjectKind,
    subject_key: str,
    profile: ScanProfile,
    scan_id: UUID,
    collected_at: datetime,
    facts: Mapping[str, Any],
    classes: Mapping[str, FactClass],
) -> bool:
    """Classify a scan's facts and store the non-secret subset as an observation.

    Drop every secret and unclassified fact with :func:`ingestible_facts`, then
    record the rest through ``om_observation_put``, which keeps the newer of two
    results for the subject. The caller commits the session. PL/pgSQL only, so this
    runs on a PostgreSQL bind.

    :param session: The session to record through; the caller commits it.
    :param subject_kind: What ``subject_key`` names.
    :param subject_key: The opaque key pmm-managed sent with the scan request.
    :param profile: The scan profile the facts were collected under; never
        :attr:`ScanProfile.MONITORED`.
    :param scan_id: The run that produced the facts; its profile must match.
    :param collected_at: When the host collected the facts.
    :param facts: The raw facts the scan collected, keyed by fact name.
    :param classes: The declared class of each fact name; an unlisted fact is
        dropped as secret.
    :return: Whether the observation was applied, which is ``False`` when a newer
        result for the subject already exists.
    :raises sqlalchemy.exc.DBAPIError: When the composite foreign key or a
        constraint refuses the row.
    """
    ingestible = ingestible_facts(facts, classes)
    schema = om_schema(extensions_settings.DATABASE)
    generator = func if schema is None else getattr(func, schema)
    call = generator.om_observation_put(
        subject_kind.value,
        subject_key,
        profile.value,
        scan_id,
        collected_at,
        literal(ingestible, postgresql.JSONB(astext_type=Text())),
    )
    return (await session.exec(select(call))).one()
