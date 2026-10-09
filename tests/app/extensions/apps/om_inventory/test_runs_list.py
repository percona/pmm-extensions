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

"""Test reading runs: date range, failing nodes, unparseable entries, unknown runs.

The window is applied before ``limit``: a week of twenty-one runs is twenty-one
runs, not the twenty newest overall with the older-than-a-week ones dropped. That
is the difference between a real history filter and a cosmetic one over a capped
page.
"""

import logging
from datetime import datetime, timedelta, UTC
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import (
    NodeResolution,
    ProbeNode,
    ProbeNodeService,
    ProbeRun,
    ProbeRunStatus,
)
from tests.app.extensions.apps.om_inventory.conftest import BASE

#: Three stamps, a day apart, so a window can include the middle one and exclude
#: the others without depending on clock-second fuzz.
DAY = timedelta(days=1)
T0 = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
T1 = T0 + DAY
T2 = T1 + DAY

ROUTES_LOGGER = "app.extensions.apps.om_inventory.api_routes"


async def record_run(session: AsyncSession, started_at: datetime) -> ProbeRun:
    """Write a finished run that started at ``started_at``.

    :param session: The database session.
    :param started_at: When the sweep started.
    :return: The saved run.
    """
    run = await ProbeRunManager.save(session, ProbeRun(status=ProbeRunStatus.SUCCESS))
    run.started_at = started_at
    return await ProbeRunManager.save(session, run)


class TestRunsListDateRangeFilter:
    """Pin ``GET /runs``'s date-range filtering."""

    @pytest.mark.asyncio
    async def test_list_runs_filters_started_at_before_limit(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Keep the in-window runs, then apply limit, so an older in-window run survives.

        Three runs a day apart, ``since`` on the oldest, ``limit=2``: without the
        window the newest two would win and the oldest would drop. With it, the two
        oldest are the ones in range once the newest is excluded by ``until``.
        """
        oldest = await record_run(session, T0)
        middle = await record_run(session, T1)
        await record_run(session, T2)

        response = await api.get(
            f"{BASE}/runs",
            params={
                "since": T0.isoformat(),
                "until": T1.isoformat(),
                "limit": 2,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        ids = [row["run_id"] for row in response.json()]
        assert ids == [str(middle.id), str(oldest.id)]

    @pytest.mark.asyncio
    async def test_list_runs_since_excludes_older(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Drop runs that started before a lower bound."""
        await record_run(session, T0)
        kept = await record_run(session, T2)

        response = await api.get(f"{BASE}/runs", params={"since": T1.isoformat()})

        assert response.status_code == status.HTTP_200_OK
        assert [row["run_id"] for row in response.json()] == [str(kept.id)]

    @pytest.mark.asyncio
    async def test_list_runs_until_excludes_newer(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Drop runs that started after the upper bound."""
        kept = await record_run(session, T0)
        await record_run(session, T2)

        response = await api.get(f"{BASE}/runs", params={"until": T1.isoformat()})

        assert response.status_code == status.HTTP_200_OK
        assert [row["run_id"] for row in response.json()] == [str(kept.id)]

    @pytest.mark.asyncio
    async def test_list_runs_rejects_until_before_since(self, api: AsyncClient) -> None:
        """Reject an inverted window as a validation failure, not an empty page."""
        response = await api.get(
            f"{BASE}/runs",
            params={"since": T2.isoformat(), "until": T0.isoformat()},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["detail"] == "until must not be before since"

    @pytest.mark.asyncio
    async def test_list_runs_omits_window_when_unset(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Return newest first with no date params, including the oldest row."""
        oldest = await record_run(session, T0)
        newest = await record_run(session, T2)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert [row["run_id"] for row in response.json()] == [
            str(newest.id),
            str(oldest.id),
        ]


def receipt_node(
    node_id: str,
    name: str,
    *,
    error: str | None = None,
    service_error: str | None = None,
    has_agent: bool = True,
) -> dict[str, Any]:
    """Build one host's receipt entry, dumped to JSON as a run row stores it.

    :param node_id: PMM's node id.
    :param name: The node's name.
    :param error: The host's own failure, if any.
    :param service_error: A failure on its one service, if any.
    :param has_agent: Whether an automation agent serves the host. Without one
        nothing is dispatched, so neither the host nor its service answers.
    :return: The receipt entry.
    """
    return ProbeNode(
        node_id=node_id,
        host_name=name,
        executor_host=name if has_agent else None,
        resolution=NodeResolution.NAME if has_agent else NodeResolution.ORPHANED,
        answered=has_agent and error is None,
        error=error,
        services=[
            ProbeNodeService(
                service_id=f"{node_id}-svc",
                service_name=f"{name}-mongodb",
                answered=has_agent and service_error is None,
                error=service_error,
            )
        ],
    ).model_dump(mode="json")


class TestRunsListFailingNodes:
    """Pin the failing nodes each run in the list names."""

    @pytest.mark.asyncio
    async def test_names_the_nodes_that_failed_their_own_or_a_service_s_scan(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Name a node whose dispatch failed and one whose service did, by name."""
        run = await record_run(session, T0)
        run.nodes = [
            receipt_node("n-3", "node03", service_error="could not query the database"),
            receipt_node("n-1", "node01"),
            receipt_node("n-2", "node02", error="the scan did not finish within 180s"),
        ]
        await ProbeRunManager.save(session, run)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["failing_nodes"] == [
            {"node_id": "n-2", "name": "node02"},
            {"node_id": "n-3", "name": "node03"},
        ]

    @pytest.mark.asyncio
    async def test_a_node_with_no_automation_agent_is_not_named(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Leave out a node nothing was dispatched to, beside one that did fail."""
        run = await record_run(session, T0)
        run.nodes = [
            receipt_node("n-1", "node01", has_agent=False),
            receipt_node("n-2", "node02", error="the scan did not finish within 180s"),
        ]
        await ProbeRunManager.save(session, run)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["failing_nodes"] == [
            {"node_id": "n-2", "name": "node02"}
        ]

    @pytest.mark.asyncio
    async def test_a_failing_node_with_no_recorded_name_is_named_by_its_node_id(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Name a failing node by its node id where its entry recorded no name."""
        run = await record_run(session, T0)
        run.nodes = [
            {
                **receipt_node("n-2", "node02", error="the scan did not finish"),
                "host_name": None,
            }
        ]
        await ProbeRunManager.save(session, run)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["failing_nodes"] == [
            {"node_id": "n-2", "name": "n-2"}
        ]

    @pytest.mark.asyncio
    async def test_a_run_with_an_empty_receipt_names_none(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Answer an empty list for a run whose receipt records no host."""
        await record_run(session, T0)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["failing_nodes"] == []


class TestRunReadsSkipAnEntryThatDoesNotParse:
    """Pin both run reads skipping, and logging, a receipt entry that is not a host."""

    @pytest_asyncio.fixture
    async def run(self, session: AsyncSession) -> ProbeRun:
        """Write a run whose receipt holds a bare service entry and a failing host.

        :param session: The database session.
        :return: The saved run.
        """
        run = await record_run(session, T0)
        run.nodes = [
            ProbeNodeService(
                service_id="s-1",
                service_name="node01-mongodb",
                error="could not query the database",
            ).model_dump(mode="json"),
            receipt_node("n-2", "node02", error="the scan did not finish within 180s"),
        ]
        return await ProbeRunManager.save(session, run)

    @pytest.mark.asyncio
    async def test_the_list_names_the_host_beside_it(
        self, api: AsyncClient, run: ProbeRun, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Answer the list, naming the failing host and logging the skipped entry."""
        caplog.set_level(logging.WARNING, logger=ROUTES_LOGGER)

        response = await api.get(f"{BASE}/runs")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["failing_nodes"] == [
            {"node_id": "n-2", "name": "node02"}
        ]
        assert f"skipping entry 0 of run {run.id}'s receipt" in caplog.text

    @pytest.mark.asyncio
    async def test_the_detail_answers_with_the_host_beside_it(
        self, api: AsyncClient, run: ProbeRun, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Answer the run's detail with only the host, logging the skipped entry."""
        caplog.set_level(logging.WARNING, logger=ROUTES_LOGGER)

        response = await api.get(f"{BASE}/runs/{run.id}")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [node["node_id"] for node in body["nodes"]] == ["n-2"]
        assert body["failing_nodes"] == [{"node_id": "n-2", "name": "node02"}]
        assert f"skipping entry 0 of run {run.id}'s receipt" in caplog.text


class TestGetRun:
    """Pin what ``GET /runs/{run_id}`` says about a run that does not exist."""

    @pytest.mark.asyncio
    async def test_an_unknown_run_is_a_404_in_the_ui_s_words(
        self, api: AsyncClient
    ) -> None:
        """Call it a scan, as PMM's UI does, naming the id asked for."""
        run_id = uuid4()

        response = await api.get(f"{BASE}/runs/{run_id}")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["detail"] == f"Scan {run_id} not found"


class TestRunsListLimitBounds:
    """Pin ``GET /runs``'s ``limit`` bounds, declared through ``Annotated``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, 101])
    async def test_out_of_range_limit_is_rejected(
        self, api: AsyncClient, limit: int
    ) -> None:
        """Reject a ``limit`` outside ``1..100`` before the query runs."""
        response = await api.get(f"{BASE}/runs", params={"limit": limit})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.asyncio
    async def test_in_range_limit_caps_the_page(
        self, api: AsyncClient, session: AsyncSession
    ) -> None:
        """Return at most ``limit`` runs, newest first."""
        await record_run(session, T0)
        newest = await record_run(session, T1)

        response = await api.get(f"{BASE}/runs", params={"limit": 1})

        assert response.status_code == status.HTTP_200_OK
        assert [row["run_id"] for row in response.json()] == [str(newest.id)]
