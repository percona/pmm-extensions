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

"""Test the scan request and observation read routes om_inventory serves pmm-managed.

``POST /scans`` accepts a scan request, fulfils once per idempotency key and queues
the run; ``GET /observations/{subject_key}`` returns a subject's stored facts or 404.
The observations are seeded directly here because ``om_observation_put`` is PL/pgSQL
and the unit suite runs on SQLite; its ingest path is covered against real PostgreSQL
in ``test_om_observation_postgres.py``.
"""

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from kombu.exceptions import OperationalError as KombuOperationalError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import utc_now
from app.extensions.apps.om_inventory import celery as celery_module
from app.extensions.apps.om_inventory.api_routes import QUEUE_UNAVAILABLE_DETAIL
from app.extensions.apps.om_inventory.config import om_inventory_settings
from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import (
    ObservationSubjectKind,
    OmObservation,
    ProbeRunStatus,
    ScanProfile,
)
from tests.app.extensions.apps.om_inventory.conftest import BASE


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the app on for this module, since a disabled app refuses a scan.

    :param monkeypatch: Restores the real value after the test.
    """
    monkeypatch.setattr(om_inventory_settings, "ENABLED", True)


async def _seed_observation(
    session: AsyncSession,
    *,
    subject_key: str,
    profile: ScanProfile = ScanProfile.MANAGED,
    facts: dict[str, Any] | None = None,
) -> OmObservation:
    """Store one observation row directly, without the PL/pgSQL upsert.

    :param session: The database session.
    :param subject_key: The subject the facts are keyed to.
    :param profile: The scan profile the facts belong to.
    :param facts: The stored facts, or a single public fact when omitted.
    :return: The stored observation.
    """
    observation = OmObservation(
        subject_kind=ObservationSubjectKind.MEMBER,
        subject_key=subject_key,
        profile=profile,
        scan_id=uuid4(),
        collected_at=utc_now(),
        facts=facts or {"pbm.storage_type": "s3"},
        digest="seed-digest",
    )
    session.add(observation)
    await session.commit()
    await session.refresh(observation)
    return observation


class TestRequestScan:
    """Assert the scan request contract."""

    @pytest.mark.asyncio
    async def test_queues_a_managed_cluster_scan(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Record the run under the key and profile, and queue it.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        with patch.object(celery_module.run_om_scan, "delay") as delay:
            response = await api.post(
                f"{BASE}/scans",
                json={
                    "profile": "managed",
                    "cluster_id": "cl-1",
                    "idempotency_key": "cl-1:managed:42",
                },
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        (run,) = await ProbeRunManager.list(session)
        assert run.profile is ScanProfile.MANAGED
        assert run.idempotency_key == "cl-1:managed:42"
        assert response.json()["run_id"] == str(run.id)
        delay.assert_called_once_with(str(run.id))

    @pytest.mark.asyncio
    async def test_a_repeated_key_fulfils_once(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Return the same run for a repeated key, queuing it only the first time.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        body = {
            "profile": "managed",
            "cluster_id": "cl-1",
            "idempotency_key": "cl-1:managed:42",
        }
        with patch.object(celery_module.run_om_scan, "delay") as delay:
            first = await api.post(f"{BASE}/scans", json=body)
            second = await api.post(f"{BASE}/scans", json=body)

        assert first.json()["run_id"] == second.json()["run_id"]
        assert len(await ProbeRunManager.list(session)) == 1
        delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_operation_id_is_recorded_as_a_uuid(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Store a UUID operation id as the run's trace link.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        operation_id = str(uuid4())
        with patch.object(celery_module.run_om_scan, "delay"):
            await api.post(
                f"{BASE}/scans",
                json={
                    "profile": "install_check",
                    "operation_id": operation_id,
                    "idempotency_key": f"{operation_id}:1",
                },
            )

        (run,) = await ProbeRunManager.list(session)
        assert str(run.requested_by_operation) == operation_id

    @pytest.mark.asyncio
    async def test_refused_while_disabled(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answer 503 while the app is switched off.

        :param api: The authenticated client.
        :param monkeypatch: Restores the real value after the test.
        """
        monkeypatch.setattr(om_inventory_settings, "ENABLED", False)

        response = await api.post(
            f"{BASE}/scans",
            json={"profile": "managed", "cluster_id": "cl-1", "idempotency_key": "k"},
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_a_scan_that_cannot_be_queued_is_failed(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer 503 and fail the run, rather than leave it running, without a broker.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        with patch.object(
            celery_module.run_om_scan,
            "delay",
            side_effect=KombuOperationalError("Connection refused"),
        ):
            response = await api.post(
                f"{BASE}/scans",
                json={
                    "profile": "managed",
                    "cluster_id": "cl-1",
                    "idempotency_key": "k",
                },
            )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        (run,) = await ProbeRunManager.list(session)
        assert (run.status, run.error) == (
            ProbeRunStatus.FAILED,
            QUEUE_UNAVAILABLE_DETAIL,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(
                {"profile": "monitored", "cluster_id": "c", "idempotency_key": "k"},
                id="monitored-is-not-a-scan",
            ),
            pytest.param(
                {
                    "profile": "managed",
                    "operation_id": "o",
                    "cluster_id": "c",
                    "idempotency_key": "k",
                },
                id="both-subjects",
            ),
            pytest.param(
                {"profile": "managed", "idempotency_key": "k"},
                id="no-subject",
            ),
            pytest.param(
                {"profile": "managed", "cluster_id": "c", "idempotency_key": ""},
                id="empty-key",
            ),
        ],
    )
    async def test_rejects_a_malformed_request(
        self, api: AsyncClient, body: dict
    ) -> None:
        """Reject a request that names no scan, no subject, both subjects or no key.

        :param api: The authenticated client.
        :param body: The malformed request body.
        """
        response = await api.post(f"{BASE}/scans", json=body)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestReadObservation:
    """Assert the observation read contract."""

    @pytest.mark.asyncio
    async def test_returns_facts_and_collected_at(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Return the subject's stored facts and their collection time.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        stored = await _seed_observation(
            session, subject_key="m-1", facts={"pbm.configured": True}
        )

        response = await api.get(
            f"{BASE}/observations/m-1",
            params={"subject_kind": "member", "profile": "managed"},
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["facts"] == {"pbm.configured": True}
        assert payload["collected_at"].startswith(stored.collected_at.isoformat()[:19])

    @pytest.mark.asyncio
    async def test_an_unreported_subject_is_404(self, api: AsyncClient) -> None:
        """Answer 404 for a subject no scan has reported, which reads as absent.

        :param api: The authenticated client.
        """
        response = await api.get(
            f"{BASE}/observations/nobody",
            params={"subject_kind": "member", "profile": "managed"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_a_subject_is_read_per_profile(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Keep one subject's facts apart by profile, so a profile miss is 404.

        :param api: The authenticated client.
        :param session: The database session the route shares.
        """
        await _seed_observation(session, subject_key="m-1", profile=ScanProfile.MANAGED)

        response = await api.get(
            f"{BASE}/observations/m-1",
            params={"subject_kind": "member", "profile": "import_check"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
