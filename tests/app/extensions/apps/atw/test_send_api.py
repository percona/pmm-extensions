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

"""Define tests for the ATW diagnostics send-job endpoints."""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import regex
from aioresponses import aioresponses
from fastapi import status
from httpx import AsyncClient
from kombu.exceptions import OperationalError
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

from app.core.utils.date_time import utc_now
from app.extensions.apps.atw import api_routes
from app.extensions.apps.atw.crud import (
    AtwIncidentExecutionManager,
    AtwIncidentManager,
    AtwSendLogManager,
)
from app.extensions.apps.atw.models import (
    AtwConfigResponse,
    AtwIncident,
    AtwIncidentExecution,
    AtwSendLog,
    AtwSendStatusEnum,
)
from app.extensions.bundle_upload import plan as delivery_plan
from app.extensions.bundle_upload.plan import DeliveryPlan, DeliveryPlanExecutor
from app.extensions.bundle_upload.resolver import DRIFTED_INPUTS_REASON
from app.extensions.config import DeliveryPlanInputs, extensions_settings
from tests.app.extensions.runaway_term_pattern import RUNAWAY_PATTERN, RUNAWAY_TERM

_BASE = "/api/apps/atw"
_CASE_SEARCH_PATH = f"{_BASE}/case-search/"
#: The transport logger whose request records carry the resolved headers.
_TRANSPORT_LOGGER = "app.core.requests.remote_api"
#: The case-search plan's declared secret, which must reach no log record.
_DELIVERY_SECRET = "real-api-key"
#: One over ``MAX_CASE_SEARCH_TERM_LENGTH``, so the route's own cap is what
#: rejects it rather than anything downstream.
_OVERLONG_TERM = "C" * 129
_EXPECTED_PAGE_TOTAL = 3
_EXPECTED_CONCURRENT_SENDS = 2


@pytest.fixture(name="configured")
def configured_fixture(mocker: MockerFixture, delivery_plan: DeliveryPlan) -> None:
    """Configure a diagnostics receiver so the send gate passes."""
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", delivery_plan)


@pytest.fixture(name="unconfigured")
def unconfigured_fixture(mocker: MockerFixture) -> None:
    """Leave diagnostics delivery unconfigured so the send gate refuses."""
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", None)


def _awaiting_secrets_plan() -> DeliveryPlan:
    """Build a baked receiver whose one declared secret carries no value.

    :return: The plan an image ships before an operator supplies credentials.
    """
    return DeliveryPlan(
        endpoint="https://intake.example.com/",
        secrets={"api_key": ""},
        upload={
            "path": "attachment/upload",
            "headers": {"x-api-key": {"source": "secret", "name": "api_key"}},
            "reference_pointer": "/result/sys_id",
        },
    )


@pytest.fixture(name="awaiting_secrets")
def awaiting_secrets_fixture(mocker: MockerFixture) -> None:
    """Bake a receiver whose declared secret an operator has not supplied yet."""
    mocker.patch.object(
        extensions_settings, "DIAGNOSTICS_DELIVERY", _awaiting_secrets_plan()
    )
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY_INPUTS", None)


@pytest.fixture(name="configured_by_inputs")
def configured_by_inputs_fixture(mocker: MockerFixture) -> None:
    """Supply the baked receiver's declared secret through the runtime inputs."""
    mocker.patch.object(
        extensions_settings, "DIAGNOSTICS_DELIVERY", _awaiting_secrets_plan()
    )
    mocker.patch.object(
        extensions_settings,
        "DIAGNOSTICS_DELIVERY_INPUTS",
        DeliveryPlanInputs(secrets={"api_key": "supplied-key"}),
    )


@pytest.fixture(name="drifted_inputs")
def drifted_inputs_fixture(mocker: MockerFixture) -> None:
    """Store inputs naming a secret the baked receiver no longer declares."""
    mocker.patch.object(
        extensions_settings, "DIAGNOSTICS_DELIVERY", _awaiting_secrets_plan()
    )
    mocker.patch.object(
        extensions_settings,
        "DIAGNOSTICS_DELIVERY_INPUTS",
        DeliveryPlanInputs(secrets={"renamed_key": "supplied-key"}),
    )


@pytest.fixture(name="enqueue")
def enqueue_fixture(mocker: MockerFixture) -> Any:
    """Intercept the Celery enqueue so no broker is needed."""
    return mocker.patch("app.extensions.apps.atw.api_routes.send_incident_diagnostics")


async def _seed_incident(
    session: AsyncSession, *, executions: int = 2
) -> tuple[AtwIncident, list[AtwIncidentExecution]]:
    """Persist an incident with ``executions`` recorded snippet executions.

    :param session: The database session.
    :param executions: How many execution rows to record.
    :return: The incident and its execution rows.
    """
    incident = await AtwIncidentManager.save(
        session, AtwIncident(created_by="alice", case_ref="CS0001")
    )
    rows = [
        await AtwIncidentExecutionManager.save(
            session,
            AtwIncidentExecution(
                incident_id=incident.id,
                task_history_id=index + 1,
                snippet_filename=f"diag-{index}.sh",
            ),
        )
        for index in range(executions)
    ]
    return incident, rows


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
class TestStartSendJob:
    """Cover POST of a new diagnostics send job."""

    async def test_creates_a_pending_row_and_enqueues_it(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Accept the send, record it as pending, and hand its id to the worker."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(execution.id) for execution in executions],
            },
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        body = response.json()
        assert body["status"] == AtwSendStatusEnum.PENDING.value
        assert body["case_ref"] == "CS0042"
        assert body["requested_by"]

        row = await AtwSendLogManager.get_or_404(session, id=UUID(body["id"]))
        assert [entry["task_history_id"] for entry in row.detail["executions"]] == [
            1,
            2,
        ]
        enqueue.delay.assert_called_once_with(str(row.id))

    async def test_send_still_works_when_incident_is_closed(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Accept a send for a closed incident so delivery is never stranded."""
        incident, executions = await _seed_incident(session)
        incident.closed_at = utc_now()
        await AtwIncidentManager.save(session, incident)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(execution.id) for execution in executions],
            },
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["status"] == AtwSendStatusEnum.PENDING.value
        enqueue.delay.assert_called_once()

    async def test_rejects_an_execution_from_another_incident(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Refuse a send naming an execution the incident does not own."""
        incident, _executions = await _seed_incident(session)
        other, other_executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(other_executions[0].id)],
            },
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert str(other_executions[0].id) in str(response.json()["detail"])
        assert await AtwSendLogManager.count(session, incident_id=incident.id) == 0
        assert await AtwSendLogManager.count(session, incident_id=other.id) == 0
        enqueue.delay.assert_not_called()

    async def test_rejects_an_empty_execution_selection(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a send that selects nothing."""
        incident, _executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={"case_ref": "CS0042", "execution_ids": []},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_rejects_a_blank_case_ref(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse a send with no support-case reference to attach to."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={"case_ref": "", "execution_ids": [str(executions[0].id)]},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    async def test_a_broker_failure_leaves_a_failed_row(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Record the attempt as failed when it could not even be queued."""
        enqueue.delay.side_effect = OperationalError("broker unreachable")
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={"case_ref": "CS0042", "execution_ids": [str(executions[0].id)]},
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        rows = await AtwSendLogManager.list(session, incident_id=incident.id)
        assert [row.status for row in rows] == [AtwSendStatusEnum.FAILED]
        assert "broker unreachable" in rows[0].detail["error"]

    async def test_concurrent_sends_for_one_incident_are_allowed(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Let a second send start while the first is still in flight."""
        incident, executions = await _seed_incident(session)
        payload = {
            "case_ref": "CS0042",
            "execution_ids": [str(executions[0].id)],
        }

        first = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/", json=payload
        )
        second = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/", json=payload
        )

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_202_ACCEPTED
        assert (
            await AtwSendLogManager.count(session, incident_id=incident.id)
            == _EXPECTED_CONCURRENT_SENDS
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("unconfigured")
class TestStartSendJobUnconfigured:
    """Cover the gate that refuses a send with no configured receiver."""

    async def test_refuses_with_the_reason(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer 503 naming why the send cannot run."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={"case_ref": "CS0042", "execution_ids": [str(executions[0].id)]},
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "Diagnostics delivery is not configured" in response.json()["detail"]
        assert await AtwSendLogManager.count(session, incident_id=incident.id) == 0


@pytest.mark.asyncio
class TestReadSendJobs:
    """Cover the polling and history endpoints."""

    async def test_get_returns_the_full_send_log(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Return every stored field so the dialog can render any state."""
        incident, _executions = await _seed_incident(session)
        row = await AtwSendLogManager.save(
            session,
            AtwSendLog(
                incident_id=incident.id,
                case_ref="CS0042",
                requested_by="alice",
                status=AtwSendStatusEnum.SUCCESS,
                finished_at=utc_now(),
                detail={"upload_reference": "att-9"},
            ),
        )

        response = await async_api_client.get(
            f"{_BASE}/incidents/{incident.id}/send-jobs/{row.id}"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == AtwSendStatusEnum.SUCCESS.value
        assert body["detail"]["upload_reference"] == "att-9"
        assert body["finished_at"] is not None

    async def test_get_scoped_to_the_owning_incident(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Refuse to read another incident's send log through this incident."""
        incident, _executions = await _seed_incident(session)
        other, _other_executions = await _seed_incident(session)
        row = await AtwSendLogManager.save(
            session,
            AtwSendLog(
                incident_id=other.id, case_ref="CS0042", requested_by="alice", detail={}
            ),
        )

        response = await async_api_client.get(
            f"{_BASE}/incidents/{incident.id}/send-jobs/{row.id}"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_get_unknown_id_is_not_found(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer 404 for a send job that never existed."""
        incident, _executions = await _seed_incident(session)

        response = await async_api_client.get(
            f"{_BASE}/incidents/{incident.id}/send-jobs/{uuid4()}"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_list_is_paginated_newest_first(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Return a paginated page of attempts with the newest first."""
        incident, _executions = await _seed_incident(session)
        for index in range(_EXPECTED_PAGE_TOTAL):
            await AtwSendLogManager.save(
                session,
                AtwSendLog(
                    incident_id=incident.id,
                    case_ref=f"CS000{index}",
                    requested_by="alice",
                    created_at=utc_now() - timedelta(minutes=index),
                    detail={},
                ),
            )

        response = await async_api_client.get(
            f"{_BASE}/incidents/{incident.id}/send-jobs/"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == _EXPECTED_PAGE_TOTAL
        assert [item["case_ref"] for item in body["items"]] == [
            "CS0000",
            "CS0001",
            "CS0002",
        ]

    async def test_list_excludes_other_incidents(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Scope the history strip to the incident being viewed."""
        incident, _executions = await _seed_incident(session)
        other, _other_executions = await _seed_incident(session)
        await AtwSendLogManager.save(
            session,
            AtwSendLog(
                incident_id=other.id, case_ref="CS9999", requested_by="bob", detail={}
            ),
        )

        response = await async_api_client.get(
            f"{_BASE}/incidents/{incident.id}/send-jobs/"
        )

        assert response.json()["total"] == 0


@pytest.mark.asyncio
class TestAtwConfig:
    """Cover the config-status endpoint the Send button reads."""

    @pytest.mark.usefixtures("unconfigured")
    async def test_reports_the_reason_when_unconfigured(
        self, async_api_client: AsyncClient
    ) -> None:
        """Tell the UI why the Send button is disabled."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["send_disabled_reasons"] == [
            "Diagnostics delivery is not configured"
        ]

    @pytest.mark.usefixtures("configured")
    async def test_reports_no_reasons_when_configured(
        self, async_api_client: AsyncClient
    ) -> None:
        """Offer the Send button once a receiver is configured."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.json()["send_disabled_reasons"] == []

    @pytest.mark.usefixtures("awaiting_secrets")
    async def test_reports_a_reason_while_a_declared_secret_is_empty(
        self, async_api_client: AsyncClient
    ) -> None:
        """Withhold the Send button until an operator supplies the credentials."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["send_disabled_reasons"] == [
            "Diagnostics delivery is not configured"
        ]

    @pytest.mark.usefixtures("configured_by_inputs")
    async def test_reports_no_reasons_once_the_inputs_supply_the_secrets(
        self, async_api_client: AsyncClient
    ) -> None:
        """Offer the Send button once the runtime inputs complete the plan."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.json()["send_disabled_reasons"] == []

    @pytest.mark.usefixtures("drifted_inputs")
    async def test_reports_the_drift_reason_when_the_inputs_stopped_matching(
        self, async_api_client: AsyncClient
    ) -> None:
        """Send the UI a reason distinct from the never-configured one."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["send_disabled_reasons"] == [DRIFTED_INPUTS_REASON]

    @pytest.mark.usefixtures("case_search_configured")
    async def test_reports_case_search_available_when_the_plan_declares_one(
        self, async_api_client: AsyncClient
    ) -> None:
        """Let the dialog issue searches only where a search is actually declared."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.json()["case_search_available"] is True

    @pytest.mark.usefixtures("configured")
    async def test_reports_no_case_search_for_a_send_only_receiver(
        self, async_api_client: AsyncClient
    ) -> None:
        """Keep a deployment that declares no search from issuing one per keystroke."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.json()["case_search_available"] is False

    @pytest.mark.usefixtures("unconfigured")
    async def test_reports_no_case_search_when_delivery_is_unconfigured(
        self, async_api_client: AsyncClient
    ) -> None:
        """Report no search where there is no receiver to search."""
        response = await async_api_client.get(f"{_BASE}/config/")

        assert response.json()["case_search_available"] is False


@pytest.mark.asyncio
class TestStartSendJobRuntimeInputs:
    """Cover the send gate when the receiver's credentials arrive as runtime inputs."""

    @pytest.mark.usefixtures("configured_by_inputs")
    async def test_accepts_the_send_once_the_inputs_complete_the_plan(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        enqueue: Any,
    ) -> None:
        """Let the send through on a plan completed by the runtime inputs."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(execution.id) for execution in executions],
            },
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        row = await AtwSendLogManager.get_or_404(
            session, id=UUID(response.json()["id"])
        )
        enqueue.delay.assert_called_once_with(str(row.id))

    @pytest.mark.usefixtures("awaiting_secrets")
    async def test_refuses_the_send_while_a_declared_secret_is_empty(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """Refuse before staging a bundle, where the plan previously failed mid-send."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(execution.id) for execution in executions],
            },
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "Diagnostics delivery is not configured" in response.json()["detail"]

    @pytest.mark.usefixtures("drifted_inputs")
    async def test_refuses_the_send_naming_the_drift(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """Refuse with the reason that tells the operator to re-supply the inputs."""
        incident, executions = await _seed_incident(session)

        response = await async_api_client.post(
            f"{_BASE}/incidents/{incident.id}/send-jobs/",
            json={
                "case_ref": "CS0042",
                "execution_ids": [str(execution.id) for execution in executions],
            },
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["detail"] == DRIFTED_INPUTS_REASON


def _case_search_plan(term_pattern: str = r"[A-Za-z0-9 ._-]+") -> DeliveryPlan:
    """Build a configured receiver that also declares a case search.

    :param term_pattern: The pattern the whole typed term must match.
    :return: The plan a deployment ships once case search is configured.
    """
    return DeliveryPlan(
        endpoint="https://intake.example.com/",
        secrets={"api_key": "real-api-key"},
        case_search={
            "path": "api/now/table/case",
            "headers": {"x-sn-apikey": {"source": "secret", "name": "api_key"}},
            "query": {"sysparm_query": {"source": "term", "prefix": "123TEXTQUERY321"}},
            "term_pattern": term_pattern,
            "results_pointer": "/result",
            "reference_pointer": "/number",
            "title_pointer": "/short_description",
        },
        upload={
            "path": "attachment/upload",
            "reference_pointer": "/result/sys_id",
        },
    )


@pytest.fixture(name="case_search_configured")
def case_search_configured_fixture(mocker: MockerFixture) -> None:
    """Configure a receiver whose plan declares a case-search section."""
    mocker.patch.object(
        extensions_settings, "DIAGNOSTICS_DELIVERY", _case_search_plan()
    )
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY_INPUTS", None)


@pytest.fixture(name="runaway_case_search_configured")
def runaway_case_search_configured_fixture(mocker: MockerFixture) -> None:
    """Configure a receiver whose case search declares a backtracking pattern."""
    mocker.patch.object(
        extensions_settings,
        "DIAGNOSTICS_DELIVERY",
        _case_search_plan(term_pattern=RUNAWAY_PATTERN),
    )
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY_INPUTS", None)


@pytest.mark.asyncio
class TestAtwCaseSearch:
    """Cover the case-search endpoint the send dialog's field queries."""

    @pytest.mark.usefixtures("case_search_configured")
    async def test_returns_the_matches_the_plans_pointers_address(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Answer with the reference and title the configured pointers name."""
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_200_OK,
                payload={
                    "result": [
                        {"number": "CS0001", "short_description": "Slow queries"},
                        {"number": "CS0002", "short_description": "Replica lag"},
                    ]
                },
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00"}
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "available": True,
            "matches": [
                {"reference": "CS0001", "title": "Slow queries"},
                {"reference": "CS0002", "title": "Replica lag"},
            ],
        }

    @pytest.mark.usefixtures("case_search_configured")
    async def test_matches_beyond_the_cap_are_dropped_keeping_the_receivers_order(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Offer at most the capped number of matches, in the receiver's order."""
        row_count = api_routes.MAX_CASE_SEARCH_MATCHES + 5
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_200_OK,
                payload={
                    "result": [
                        {"number": f"CS{index:04d}", "short_description": "Case"}
                        for index in range(row_count)
                    ]
                },
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00"}
            )

        body = response.json()
        assert response.status_code == status.HTTP_200_OK
        assert body["available"] is True
        assert len(body["matches"]) == api_routes.MAX_CASE_SEARCH_MATCHES
        assert body["matches"] == [
            {"reference": f"CS{index:04d}", "title": "Case"}
            for index in range(api_routes.MAX_CASE_SEARCH_MATCHES)
        ]

    @pytest.mark.usefixtures("configured")
    async def test_reports_unavailable_when_the_plan_declares_no_case_search(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Degrade to the plain field on a receiver configured for sends only."""
        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("unconfigured")
    async def test_reports_unavailable_when_delivery_is_unconfigured(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Answer for an unconfigured deployment rather than refusing it."""
        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("drifted_inputs")
    async def test_reports_unavailable_when_the_stored_inputs_drifted(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Withhold the search while the stored inputs no longer fit the plan."""
        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("case_search_configured")
    async def test_reports_unavailable_when_the_receiver_errors(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Keep a failing receiver from reaching the dialog as a 5xx."""
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                payload={"error": "boom"},
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00"}
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("case_search_configured")
    async def test_a_failed_search_leaves_no_secret_in_the_logs(
        self, admin_api_client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Mask the plan's credential in everything a failed search records.

        Two records are in play and only one is the executor's: the transport
        logs the outgoing request, and the route logs the failure's type after
        the executor's redaction context has already closed.
        """
        caplog.set_level("DEBUG", logger=_TRANSPORT_LOGGER)
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                payload={"error": "boom"},
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00"}
            )

        assert response.json() == {"available": False, "matches": []}
        transport = [
            record.getMessage()
            for record in caplog.records
            if record.name == _TRANSPORT_LOGGER
        ]
        # Without this the secret assertions below hold vacuously, since a run
        # that logged nothing carries nothing to leak.
        assert any("****" in message for message in transport)
        assert any(
            "case search failed" in record.getMessage() for record in caplog.records
        )
        assert all(
            _DELIVERY_SECRET not in record.getMessage() for record in caplog.records
        )
        # A traceback is what would carry the credential past the assertion
        # above, since that one reads the format string alone.
        assert all(record.exc_info is None for record in caplog.records)

    @pytest.mark.usefixtures("case_search_configured")
    async def test_no_upstream_error_body_reaches_a_log_record(
        self, admin_api_client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Keep an error body out of the log line the degraded search writes.

        ``RemoteAPI`` maps an upstream error body's ``detail`` onto the
        exception it raises, so a body-carried credential reaches the log
        through the exception rather than through the request line the
        transport redacts.
        """
        # No level is raised, unlike in the neighbours: the transport logs every
        # response body at DEBUG, a separate leak at this call site that a DEBUG
        # capture would see however this route behaves.
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_401_UNAUTHORIZED,
                payload={"detail": "encrypted-token-blob"},
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00"}
            )

        assert response.json() == {"available": False, "matches": []}
        assert "encrypted-token-blob" not in response.text
        # Without this the sentinel assertion below holds vacuously, since a
        # run that logged nothing carries nothing to leak. The open paren keeps
        # the control on the route's own wording rather than on which exception
        # the transport happens to map an unauthorised status to.
        assert "Diagnostics case search failed (" in caplog.text
        # ``caplog.text``, not ``record.getMessage()``: the latter renders the
        # format string alone, so it cannot see a value carried in a traceback.
        assert "encrypted-token-blob" not in caplog.text

    @pytest.mark.usefixtures("case_search_configured")
    async def test_reports_unavailable_when_the_search_outruns_its_bound(
        self, admin_api_client: AsyncClient, mocker: MockerFixture
    ) -> None:
        """Bound a search issued while someone is still typing."""

        async def _slow_search(_self: Any, _term: str, **_kwargs: Any) -> list[Any]:
            await asyncio.sleep(1)
            return []

        mocker.patch.object(api_routes, "CASE_SEARCH_TIMEOUT_SECONDS", 0.01)
        mocker.patch.object(DeliveryPlanExecutor, "search_cases", _slow_search)

        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("case_search_configured")
    async def test_the_bound_also_covers_opening_the_transport(
        self, admin_api_client: AsyncClient, mocker: MockerFixture
    ) -> None:
        """Bound the connect phase too, which opens inside the executor's context.

        A timeout placed only around ``search_cases`` would let a receiver that
        never completes the connection hang the request indefinitely.
        """

        @asynccontextmanager
        async def _hanging_executor(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(1)
            yield None

        mocker.patch.object(api_routes, "CASE_SEARCH_TIMEOUT_SECONDS", 0.01)
        mocker.patch.object(api_routes, "get_delivery_executor", _hanging_executor)

        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    # The signal method interrupts a match ``re`` would never return from, so a
    # regression fails this test alone instead of tearing down the whole run.
    @pytest.mark.timeout(10, method="signal")
    @pytest.mark.usefixtures("runaway_case_search_configured")
    async def test_a_runaway_term_pattern_reports_unavailable_promptly(
        self, admin_api_client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Answer a backtracking match with unavailability rather than a hang."""
        started = time.monotonic()
        with aioresponses() as mock:
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": RUNAWAY_TERM}
            )

            assert not mock.requests
        elapsed = time.monotonic() - started

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}
        assert elapsed < api_routes.CASE_SEARCH_TIMEOUT_SECONDS
        assert "Diagnostics case search failed (DeliveryPlanError)" in caplog.text
        assert RUNAWAY_TERM not in caplog.text
        assert RUNAWAY_PATTERN not in caplog.text

    @pytest.mark.timeout(10, method="signal")
    @pytest.mark.usefixtures("runaway_case_search_configured")
    async def test_time_spent_opening_the_executor_comes_out_of_the_match_budget(
        self, admin_api_client: AsyncClient, mocker: MockerFixture
    ) -> None:
        """Answer a runaway match after a slow open within the route's bound.

        The match's own cap is raised past the bound, so only the time left of
        the route's deadline can stop the match once the executor has opened.
        """
        bound_seconds = 2.0
        open_seconds = 1.0
        open_executor = api_routes.get_delivery_executor

        @asynccontextmanager
        async def _slow_to_open(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(open_seconds)
            async with open_executor(*args, **kwargs) as executor:
                yield executor

        mocker.patch.object(api_routes, "CASE_SEARCH_TIMEOUT_SECONDS", bound_seconds)
        mocker.patch.object(delivery_plan, "TERM_MATCH_TIMEOUT_SECONDS", 5.0)
        mocker.patch.object(api_routes, "get_delivery_executor", _slow_to_open)
        spy = mocker.spy(regex, "fullmatch")

        started = time.monotonic()
        with aioresponses() as mock:
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": RUNAWAY_TERM}
            )

            assert not mock.requests
        elapsed = time.monotonic() - started

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}
        assert elapsed < bound_seconds + 0.5
        spy.assert_called_once()
        assert 0 <= spy.call_args.kwargs["timeout"] <= bound_seconds - open_seconds

    @pytest.mark.usefixtures("case_search_configured")
    async def test_a_term_carrying_receiver_query_syntax_reaches_no_receiver(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Answer a query-widening term with unavailability and no request.

        The receiver's encoded query separates clauses with a character it gives
        no escape for, so a term carrying one would return rows the plan never
        selected. ``aioresponses`` registers no route here, so any request at
        all would fail the mock rather than pass silently.
        """
        with aioresponses() as mock:
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "CS00^ORsys_idISNOTEMPTY"}
            )

            assert not mock.requests

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"available": False, "matches": []}

    @pytest.mark.usefixtures("case_search_configured")
    async def test_the_term_is_required(self, admin_api_client: AsyncClient) -> None:
        """Refuse a search with nothing to match on."""
        response = await admin_api_client.get(_CASE_SEARCH_PATH)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.usefixtures("case_search_configured")
    async def test_an_empty_term_is_refused(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Refuse an empty term rather than issuing an unbounded search."""
        response = await admin_api_client.get(_CASE_SEARCH_PATH, params={"term": ""})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.usefixtures("case_search_configured")
    async def test_a_whitespace_only_term_is_refused(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Refuse a term that strips to nothing rather than matching everything."""
        response = await admin_api_client.get(_CASE_SEARCH_PATH, params={"term": "   "})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.usefixtures("case_search_configured")
    async def test_a_padded_term_is_trimmed_before_it_reaches_the_receiver(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Strip surrounding whitespace rather than forwarding it to the query."""
        with aioresponses() as mock:
            mock.get(
                re.compile(r"https://intake\.example\.com/api/now/table/case.*"),
                status=status.HTTP_200_OK,
                payload={"result": []},
            )
            response = await admin_api_client.get(
                _CASE_SEARCH_PATH, params={"term": "  CS00  "}
            )

            requests = [req for reqs in mock.requests.values() for req in reqs]

        assert response.status_code == status.HTTP_200_OK
        assert requests[0].kwargs["params"]["sysparm_query"] == "123TEXTQUERY321CS00"

    @pytest.mark.usefixtures("case_search_configured")
    async def test_a_term_over_the_cap_is_refused(
        self, admin_api_client: AsyncClient
    ) -> None:
        """Reject a term longer than the route forwards to the receiver."""
        response = await admin_api_client.get(
            _CASE_SEARCH_PATH, params={"term": _OVERLONG_TERM}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestAtwCaseSearchContract:
    """Cover the case-search surfaces that need no event loop to check.

    Kept apart from the asyncio-marked classes above: a sync test inheriting a
    class-level ``asyncio`` mark is never run as a coroutine, and pytest warns
    on every collection rather than failing, so the mark would go unnoticed.
    """

    def test_case_search_available_is_optional_in_the_published_schema(self) -> None:
        """Keep a client validating the previous contract passing.

        A new *required* field on an existing response is a breaking change; the
        default is what keeps this one additive.
        """
        schema = AtwConfigResponse.model_json_schema()

        assert "case_search_available" in schema["properties"]
        assert "case_search_available" not in schema.get("required", [])

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("case_search_configured")
    async def test_case_search_refuses_a_non_administrator(
        self, async_api_client: AsyncClient
    ) -> None:
        """Match the search's authorization to the action it serves.

        The router resolves a minimum role for unsafe methods only, so this safe
        method would otherwise answer any authenticated caller, while the dialog
        that issues it is offered to administrators alone.
        """
        response = await async_api_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_case_search_requires_authentication(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Ensure unauthenticated callers receive JSON 401, as every ATW GET does."""
        response = unauthenticated_client.get(
            _CASE_SEARCH_PATH, params={"term": "CS00"}, follow_redirects=False
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
