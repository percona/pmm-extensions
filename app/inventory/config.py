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

"""Define settings for the Inventory API."""

from typing import ClassVar

from app.core.config import BaseYamlAppSettings
from app.core.db.config import DatabaseOptions
from app.core.middleware.security_headers import SecurityHeadersOptions
from app.core.settings_override.constants import INVENTORY_SETTINGS
from app.core.settings_override.proxy import OverridableSettingsProxy


class InventorySettings(BaseYamlAppSettings):
    """Settings for the Inventory API.

    :cvar SETTINGS_PREFIXES: The prefixes for task-related settings in the
        configuration file. Set to ["INVENTORY"].
    :vartype SETTINGS_PREFIXES: ClassVar[list[str]]
    :param UVICORN_PORT: The port to be used by Uvicorn for running the server.
        Defaults to 8001.
    :type UVICORN_PORT: int
    :param DATABASE: The database configuration options. Defaults to an SQLite database
        with the name "inventory.db".
    :type DATABASE: DatabaseOptions
    :param SECURITY_HEADERS: Specific options for the SecurityHeadersMiddleware.
        Use `False` to disable the middleware completely.
    :type SECURITY_HEADERS: SecurityHeadersOptions | None
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["INVENTORY"]
    UVICORN_PORT: int = 8001
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="inventory.db")
    SECURITY_HEADERS: SecurityHeadersOptions | None = SecurityHeadersOptions(
        content_security_policy_strict=False
    )


inventory_settings: InventorySettings = OverridableSettingsProxy(
    InventorySettings, setting_class=INVENTORY_SETTINGS
)
