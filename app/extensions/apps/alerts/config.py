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

"""Define the alerts plugin settings section."""

__all__ = ["AlertsSettings", "alerts_settings"]

from typing import ClassVar

from pydantic import PositiveInt

from app.core.celery.models import IntervalSchedule, Period
from app.core.config import BaseYamlSettings
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field


class AlertsSettings(BaseYamlSettings):
    """Define configuration options for the alerts plugin.

    :cvar SETTINGS_PREFIXES: The prefixes for alerts-plugin settings in the
        configuration file. Set to ``["EXTENSIONS", "ALERTS"]`` so the section lives
        under ``EXTENSIONS.ALERTS`` and never collides with the core ``AlertSettings``
        section (prefix ``ALERTING``).
    :param BACKUP_INTERVAL: Interval between alert configuration backups.
    :param BACKUP_RETENTION: Maximum number of alert backups to retain.
    :param ALERT_FOLDER_NAME: Display name of the PMM folder used for
        PMM Extensions managed alert rules.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "ALERTS"]
    BACKUP_INTERVAL: IntervalSchedule = hot_field(  # ty: ignore[invalid-assignment]
        IntervalSchedule(every=24, period=Period.HOURS)
    )
    BACKUP_RETENTION: PositiveInt = hot_field(10)  # ty: ignore[invalid-assignment]
    ALERT_FOLDER_NAME: str = hot_field(
        "PMM Extensions Alerts"
    )  # ty: ignore[invalid-assignment]


alerts_settings: AlertsSettings = OverridableSettingsProxy(AlertsSettings)
