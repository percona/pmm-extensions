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

"""Provide helpers that pause :meth:`~app.core.requests.remote_api.BaseRemoteAPI.close_when_idle`."""

from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

from app.core.requests.remote_api import PendingCloses

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def patch_paused_close_when_idle(
    mocker: MockerFixture,
    cls: type,
) -> tuple[asyncio.Event, asyncio.Event]:
    """Patch ``cls.close_when_idle`` to pause until the caller resumes.

    :param mocker: The pytest-mock fixture.
    :param cls: The class whose ``close_when_idle`` to patch (e.g.
        :class:`~app.core.requests.remote_api.RemoteAPI` or
        :class:`~app.tasks.execution.executors.nomad.NomadExecutor`).
    :return: ``(entered, resume)`` — ``entered`` is set when a call reaches the
        pause; set ``resume`` to let that call continue into the original method.
    """
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = cls.close_when_idle

    async def paused_close_when_idle(
        self: Any, pending: PendingCloses | None = None
    ) -> None:
        entered.set()
        await resume.wait()
        await original(self, pending=pending)

    mocker.patch.object(cls, "close_when_idle", paused_close_when_idle)
    return entered, resume
