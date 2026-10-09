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

"""Define the Inventory app settings section."""

__all__ = ["InventoryAppSettings", "inventory_app_settings"]

from datetime import timedelta
from typing import Annotated, ClassVar

from annotated_types import Gt
from pydantic import PositiveInt

from app.core.celery.models import ManageableInterval
from app.core.config import BaseYamlSettings
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field


class InventoryAppSettings(BaseYamlSettings):
    """Define the tombstone-collection job's schedule and runtime knobs.

    The whole feature configures from this one section: every field below is
    read either by ``app.extensions.apps.inventory.collection`` or by the app's own
    beat-schedule declaration, and all of them are hot-reloadable.

    :cvar SETTINGS_PREFIXES: The prefixes for Inventory-app settings in the
        configuration file, placing the section under ``EXTENSIONS.INVENTORY``.
    :param COLLECTION_INTERVAL: The schedule on which the collection job runs.
        ``None`` seeds no beat entry, which is the shipped default: collection
        deletes rows irreversibly, so a deployment carrying that default does
        not start doing so on upgrade. The PMM sidecar profile overrides it to
        ``1 days``, so a PMM-embedded deployment does. Sub-minute periods are
        rejected here rather than at first use: the periodic-task write path
        enforces a one-minute floor, so a shorter cadence would seed a running
        schedule the operator could then neither toggle nor edit.
    :param COLLECTION_RETENTION: How long a tombstone is kept before it becomes
        eligible for deletion. The positive lower bound is load-bearing: a
        non-positive retention would put the cutoff at or after the present and
        collect every tombstone in a single pass.
    :param IDENTITY_LINK_PIN_RETENTION: How long a standing identity link keeps
        its successor's tombstone out of collection, measured from the
        successor's own ``retired_at``. Past it the successor is collected like
        any other aged tombstone and the link can no longer be reversed. The
        positive lower bound is load-bearing for the reason
        ``COLLECTION_RETENTION``'s is: a non-positive bound would release every
        pinned tombstone in a single pass. A value shorter than
        ``COLLECTION_RETENTION`` is accepted but leaves the pin protecting
        nothing, since a tombstone that young is not yet eligible anyway.
    :param COLLECTION_BATCH_SIZE: The most entities of each type one call to the
        Inventory API may collect.
    :param COLLECTION_MAX_BATCHES: The most batches one scheduled run issues.
        Reaching the cap ends the run normally and leaves the rest to the next
        tick, so a first run against years of accumulated tombstones cannot hold
        the worker for an unbounded time.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "INVENTORY"]
    COLLECTION_INTERVAL: ManageableInterval | None = hot_field(None)
    COLLECTION_RETENTION: Annotated[timedelta, Gt(timedelta(0))] = hot_field(
        timedelta(days=30)
    )
    IDENTITY_LINK_PIN_RETENTION: Annotated[timedelta, Gt(timedelta(0))] = hot_field(
        timedelta(days=180)
    )
    COLLECTION_BATCH_SIZE: PositiveInt = hot_field(500, advanced=True)
    COLLECTION_MAX_BATCHES: PositiveInt = hot_field(20, advanced=True)


inventory_app_settings: InventoryAppSettings = OverridableSettingsProxy(
    InventoryAppSettings
)
