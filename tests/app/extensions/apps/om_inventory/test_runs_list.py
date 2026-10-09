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

"""Test ``GET /runs`` date-range filtering, and ``GET /runs/{run_id}``'s 404.

The window is applied before ``limit``: a week of twenty-one runs is twenty-one
runs, not the twenty newest overall with the older-than-a-week ones dropped. That
is the difference between a real history filter and a cosmetic one over a capped
page.
"""

from datetime import datetime, timedelta, UTC
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.om_inventory.crud import ProbeRunManager
from app.extensions.apps.om_inventory.models import ProbeRun, ProbeRunStatus
from tests.app.extensions.apps.om_inventory.conftest import BASE

#: Three stamps, a day apart, so a window can include the middle one and exclude
#: the others without depending on clock-second fuzz.
DAY = timedelta(days=1)
T0 = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
T1 = T0 + DAY
T2 = T1 + DAY


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
