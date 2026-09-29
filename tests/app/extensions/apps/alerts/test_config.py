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

"""Define tests for the alerts plugin configuration."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.celery.models import IntervalSchedule, Period
from app.core.settings_override.constants import ALERT_SETTINGS
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    is_hot_reloadable,
    materialize_override_value,
)
from app.extensions.apps.alerts.config import alerts_settings, AlertsSettings

DEFAULT_BACKUP_RETENTION = 10
CUSTOM_BACKUP_RETENTION = 5


class TestAlertsSettings:
    """Test the ``AlertsSettings`` plugin-owned settings section."""

    def test_defaults(self) -> None:
        """Assert default values for the alerts settings section.

        Load without the dotenv file so a local ``.env.local`` override does not
        mask the built-in defaults.
        """
        config = AlertsSettings(_env_file=None)
        assert IntervalSchedule(every=24, period=Period.HOURS) == config.BACKUP_INTERVAL
        assert config.BACKUP_RETENTION == DEFAULT_BACKUP_RETENTION
        assert config.ALERT_FOLDER_NAME == "PMM Extensions Alerts"

    def test_custom_values(self) -> None:
        """Assert custom values are accepted."""
        config = AlertsSettings(
            BACKUP_RETENTION=CUSTOM_BACKUP_RETENTION,
            ALERT_FOLDER_NAME="Custom Alerts",
        )
        assert config.BACKUP_RETENTION == CUSTOM_BACKUP_RETENTION
        assert config.ALERT_FOLDER_NAME == "Custom Alerts"

    def test_settings_prefixes(self) -> None:
        """Assert the section is scoped under ``EXTENSIONS.ALERTS``."""
        assert AlertsSettings.SETTINGS_PREFIXES == ["EXTENSIONS", "ALERTS"]

    @pytest.mark.parametrize("bad", [0, -1, -10])
    def test_backup_retention_rejects_non_positive(self, bad: int) -> None:
        """``BACKUP_RETENTION`` is a ``PositiveInt`` and rejects 0 / negatives."""
        with pytest.raises(ValidationError):
            AlertsSettings(BACKUP_RETENTION=bad)

    @pytest.mark.parametrize(
        "bad",
        [{"every": 0, "period": "hours"}, {"every": -1, "period": "hours"}, "junk"],
    )
    def test_backup_interval_rejects_invalid(self, bad: Any) -> None:
        """An unparseable / non-positive ``BACKUP_INTERVAL`` is rejected."""
        with pytest.raises((ValidationError, ValueError)):
            AlertsSettings(BACKUP_INTERVAL=bad)


class TestAlertsSettingsProxy:
    """Expose an overridable proxy bound to the class ``__name__``."""

    def test_alerts_settings_is_overridable_proxy(self) -> None:
        """``alerts_settings`` is an ``OverridableSettingsProxy``."""
        assert isinstance(alerts_settings, OverridableSettingsProxy)

    def test_proxy_reads_default_fields(self) -> None:
        """Reads through the proxy resolve to the section's defaults."""
        assert alerts_settings.ALERT_FOLDER_NAME == "PMM Extensions Alerts"
        assert alerts_settings.BACKUP_RETENTION == DEFAULT_BACKUP_RETENTION

    def test_proxy_uses_class_name_identifier(self) -> None:
        """Bind the proxy to its own ``__name__``, distinct from the core ``AlertSettings``."""
        assert alerts_settings._setting_class == AlertsSettings.__name__
        assert AlertsSettings.__name__ != ALERT_SETTINGS


class TestAlertsSettingsHotFields:
    """All three fields are HOT-reloadable so DB overrides take effect live."""

    @pytest.mark.parametrize(
        "field", ["BACKUP_INTERVAL", "BACKUP_RETENTION", "ALERT_FOLDER_NAME"]
    )
    def test_field_is_hot_reloadable(self, field: str) -> None:
        """Each alerts field is declared HOT via ``hot_field``."""
        assert is_hot_reloadable(AlertsSettings, field) is True

    def test_materializes_interval_override(self) -> None:
        """A dict override for ``BACKUP_INTERVAL`` coerces to an ``IntervalSchedule``."""
        field_info = AlertsSettings.model_fields["BACKUP_INTERVAL"]
        value = materialize_override_value(
            AlertsSettings,
            "BACKUP_INTERVAL",
            field_info,
            {"every": 6, "period": "hours"},
        )
        assert value == IntervalSchedule(every=6, period=Period.HOURS)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_retention_override_rejected(self, bad: int) -> None:
        """Non-positive ``BACKUP_RETENTION`` overrides fail coercion."""
        field_info = AlertsSettings.model_fields["BACKUP_RETENTION"]
        with pytest.raises((ValidationError, ValueError)):
            materialize_override_value(
                AlertsSettings, "BACKUP_RETENTION", field_info, bad
            )
