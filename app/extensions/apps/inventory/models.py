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

"""Define Pydantic models for the Inventory plugin's schedule UI and API responses."""

from pydantic import BaseModel

from app.core.utils.fields import NonEmptyStr, UTCDatetime
from app.extensions.models import SyncStatusEnum


class SyncRunSummary(BaseModel):
    """Summarize one recorded synchronization run for the sync-status endpoint.

    :param syncer: The fully qualified name of the synchronizer that ran.
    :param started_at: When the run was recorded.
    :param finished_at: When the run last changed, or ``None`` while it is open.
    :param status: The run-level outcome.
    :param snapshot_complete: Whether the run observed a complete generation of the
        remote inventory, or ``None`` when the syncer does not produce one.
    """

    syncer: NonEmptyStr
    started_at: UTCDatetime
    finished_at: UTCDatetime | None
    status: SyncStatusEnum
    snapshot_complete: bool | None


class PluginTaskResponse(BaseModel):
    """Represent a single plugin task entry returned by ``GET /api/apps/inventory/``.

    :param name: Machine-readable task identifier (e.g. ``"inventory-sync"``).
    :param display_name: Human-readable label for the schedule UI.
    """

    name: str
    display_name: str
