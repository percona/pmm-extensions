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

"""Declare the om_inventory app's own settings class.

Owning the class here rather than mounting it on ``ExtensionsSettings`` is what keeps
``OmInventorySettings`` out of a deployment that never activates the app: the
registry collects only activated apps, so a PMM Extensions without OM has no such section
to be confused by.
"""

from app.core.settings_override.api.routes import AppOwnedClassEntry
from app.extensions.apps.om_inventory.config import (
    om_inventory_settings,
    OmInventorySettings,
)

SERVICE_ONLY_KEYS = frozenset({"ENABLED"})
"""Set by pmm-managed alone, through ``PUT /enabled``, from PMM's own switch."""

APP_OWNED_SETTINGS_CLASSES: list[AppOwnedClassEntry] = [
    AppOwnedClassEntry(
        setting_class=OmInventorySettings.__name__,
        settings_cls=OmInventorySettings,
        proxy=om_inventory_settings,  # ty: ignore[invalid-argument-type]
        app_key="om_inventory",
        reseed_keys=frozenset({"ENABLED", "AUTO_SCAN", "SCHEDULE"}),
        service_only_keys=SERVICE_ONLY_KEYS,
    ),
]
