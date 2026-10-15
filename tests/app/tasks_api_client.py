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

"""Drive the Tasks API in-loop, against caller-supplied databases.

The synchronous ``TestClient`` runs the app on a loop of its own, so a session
opened by an async test cannot cross into it, and a test cannot hold a lock in
its own loop while a request waits on that lock in another. This client runs
the app in the test's loop. Each request gets a session of its own from the
tasks maker, as the production dependency hands out, so two in-flight requests
never share one.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.celery.deps import get_session as get_celery_beat_session
from app.tasks.deps import get_session as get_tasks_session
from app.tasks.main import tasks_app

__all__ = ["tasks_api_client"]


@asynccontextmanager
async def tasks_api_client(
    user: CasdoorUser,
    tasks_session_maker: Callable[[], Any],
    beat_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """Yield an authenticated client for the Tasks API.

    :param user: The user every request is made as.
    :param tasks_session_maker: The maker each request draws its tasks-database
        session from.
    :param beat_session: The celery-beat session every request receives.
    :return: The client, with the overrides removed on exit.
    """

    async def _tasks_session() -> AsyncIterator[AsyncSession]:
        async with tasks_session_maker() as session:
            yield session

    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = lambda: (
        None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: user
    tasks_app.dependency_overrides[get_tasks_session] = _tasks_session
    tasks_app.dependency_overrides[get_celery_beat_session] = lambda: beat_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=tasks_app), base_url="http://test"
        ) as client:
            yield client
    finally:
        tasks_app.dependency_overrides = {}
