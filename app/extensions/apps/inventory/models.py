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

from pydantic import BaseModel, ConfigDict

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


class InventorySyncTriggerWrite(BaseModel):
    """Carry the optional JSON body for the ad-hoc inventory sync trigger.

    A ``None`` or empty ``syncer`` selects the sync-all path; a non-empty
    string targets a single configured syncer by its fully qualified
    ``"module.ClassName"`` identifier. Unknown fields are rejected with
    HTTP 422 so a typo on the client never silently degrades to sync-all.

    :param syncer: Fully qualified syncer name, or ``None`` for sync-all.
    :type syncer: str | None
    """

    model_config = ConfigDict(extra="forbid")

    syncer: str | None = None


class InventorySyncStatusResponse(BaseModel):
    """Represent the inventory sync status response.

    :param is_running: ``True`` when an inventory-wide sync is currently
        in progress; ``False`` otherwise.
    :param last_runs: The most recently recorded synchronization runs, newest first.
    """

    is_running: bool
    last_runs: list[SyncRunSummary] = []


class AvailableSyncer(BaseModel):
    """Provide template-facing metadata for an available syncer.

    :param name: The fully qualified ``"module.ClassName"`` identifier matching
        ``BaseSyncer.get_name()`` and the value persisted in
        ``SyncInstance.syncer``. Used as the wire identifier in form payloads.
    :type name: str
    :param display_name: The human-readable label rendered in the dropdown
        (the syncer class's short name with any trailing ``Syncer`` suffix
        stripped).
    :type display_name: str
    """

    name: str
    display_name: str


class PluginTaskResponse(BaseModel):
    """Represent a single plugin task entry returned by ``GET /api/apps/inventory/``.

    :param name: Machine-readable task identifier (e.g. ``"inventory-sync"``).
    :param display_name: Human-readable label for the schedule UI.
    """

    name: str
    display_name: str
