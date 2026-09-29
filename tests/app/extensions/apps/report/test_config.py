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

"""Define tests for the report plugin configuration."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.celery.models import CrontabSchedule, IntervalSchedule, Period
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import is_hot_reloadable
from app.extensions.apps.report.config import (
    health_report_settings,
    HealthReportSettings,
    ReportScheduleEntry,
)

HEALTH_REPORT_YAML_BLOCK = """\
  EXTENSIONS:
    HEALTH_REPORT:
      UPLOAD: true
      ENDPOINT: https://snow.example.com/v1/upload/
      CLIENT_ID: test-client
      SCHEDULES:
        - SCHEDULE: {EVERY: 7, PERIOD: days}
          SINCE: "now-10d"
          UPLOAD: true
"""


def _use_profile(tmp_path, monkeypatch, body: str) -> None:
    """Write a settings profile into ``tmp_path`` and make it the process profile."""
    (tmp_path / "settings.yaml").write_text(f"default:\n{body}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


DEFAULT_ARTIFACT_TTL = 3600


class TestHealthReportSettings:
    """Test the report plugin-owned ``HealthReportSettings`` section."""

    def test_settings_prefixes(self) -> None:
        """Assert the section is scoped under ``EXTENSIONS.HEALTH_REPORT``."""
        assert HealthReportSettings.SETTINGS_PREFIXES == ["EXTENSIONS", "HEALTH_REPORT"]

    def test_defaults(self) -> None:
        """Assert default values for the health report settings section."""
        config = HealthReportSettings(_env_file=None)
        assert config.schedules == []
        assert config.upload is False
        assert config.endpoint is None
        assert config.api_key is None
        assert config.client_id is None
        assert Path(config.artifact_dir) == Path("data/health-reports").resolve()
        assert config.artifact_ttl == DEFAULT_ARTIFACT_TTL
        assert config.cleanup_interval == IntervalSchedule(
            every=15, period=Period.MINUTES
        )

    def test_yaml_health_report_block_resolves(self, tmp_path, monkeypatch) -> None:
        """Resolve ``EXTENSIONS.HEALTH_REPORT`` from a deployed ``settings.yaml`` block."""
        _use_profile(tmp_path, monkeypatch, HEALTH_REPORT_YAML_BLOCK)

        config = HealthReportSettings(_env_file=None)

        assert config.upload is True
        assert config.endpoint == "https://snow.example.com/v1/upload/"
        assert config.client_id == "test-client"
        assert len(config.schedules) == 1
        assert config.schedules[0].since == "now-10d"
        assert config.schedules[0].upload is True
        assert config.schedules[0].schedule == IntervalSchedule(
            every=7, period=Period.DAYS
        )

    def test_env_var_overrides_health_report_field(self, monkeypatch) -> None:
        """Resolve ``EXTENSIONS__HEALTH_REPORT__*`` environment variables."""
        monkeypatch.setenv("EXTENSIONS__HEALTH_REPORT__CLIENT_ID", "env-client")

        config = HealthReportSettings(_env_file=None)

        assert config.client_id == "env-client"

    def test_secret_file_resolves_api_key(self, tmp_path) -> None:
        """Resolve ``EXTENSIONS__HEALTH_REPORT__API_KEY`` from a mounted secret file."""
        (tmp_path / "EXTENSIONS__HEALTH_REPORT__API_KEY").write_text(
            "secret-from-file\n", encoding="utf-8"
        )

        config = HealthReportSettings(_secrets_dir=tmp_path, _env_file=None)

        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "secret-from-file"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (
                "https://intake.example.com/v1/upload/",
                "https://intake.example.com/v1/upload/",
            ),
            (
                "https://intake.example.com/v1/upload",
                "https://intake.example.com/v1/upload",
            ),
            ("https://intake.example.com/", "https://intake.example.com"),
            ("https://intake.example.com", "https://intake.example.com"),
        ],
    )
    def test_preserves_a_path_trailing_slash(self, configured, expected) -> None:
        """Keep a path's trailing slash, trimming only a bare origin's."""
        assert HealthReportSettings(endpoint=configured).endpoint == expected

    def test_empty_endpoint_becomes_none(self) -> None:
        """Leave a blank endpoint unset rather than normalizing it."""
        assert HealthReportSettings(endpoint="   ").endpoint is None

    def test_upload_disabled_when_toggle_off(self) -> None:
        """Return a reason when upload is disabled."""
        config = HealthReportSettings(upload=False)
        assert config.upload_disabled_reasons == ["Upload is disabled"]
        assert config.is_upload_configured is False

    def test_upload_configured_when_fully_set(self) -> None:
        """Return no reasons when upload is enabled and credentials are set."""
        config = HealthReportSettings(
            upload=True,
            endpoint="https://snow.example.com",
            api_key=SecretStr("key"),
            client_id="client-1",
        )
        assert config.upload_disabled_reasons == []
        assert config.is_upload_configured is True


class TestReportScheduleEntry:
    """Test nested schedule entries keep uppercase YAML key parsing."""

    def test_schedules_parse_uppercase_yaml_keys(self, tmp_path, monkeypatch) -> None:
        """Parse uppercase schedule keys from a real YAML profile block."""
        _use_profile(tmp_path, monkeypatch, HEALTH_REPORT_YAML_BLOCK)

        config = HealthReportSettings(_env_file=None)
        entry = config.schedules[0]

        assert isinstance(entry, ReportScheduleEntry)
        assert entry.schedule == IntervalSchedule(every=7, period=Period.DAYS)
        assert entry.since == "now-10d"
        assert entry.until == "now"
        assert entry.full is True
        assert entry.refresh is False
        assert entry.upload is True

    def test_a_runnable_crontab_schedule_still_loads(self) -> None:
        """Assert a cron the scheduler can run is accepted from settings."""
        entry = ReportScheduleEntry.model_validate(
            {"schedule": {"minute": "0", "hour": "2"}}
        )
        assert isinstance(entry.schedule, CrontabSchedule)
        assert entry.schedule.minute == "0"

    def test_an_unrunnable_crontab_schedule_fails_at_load(self) -> None:
        """Assert a malformed cron fails at settings load, naming the field.

        It previously reached the seed path and raised a bare ``ValueError``
        there instead.
        """
        with pytest.raises(ValidationError) as excinfo:
            ReportScheduleEntry.model_validate(
                {"schedule": {"minute": "not-a-cron", "hour": "2"}}
            )
        assert "schedule" in str(excinfo.value)


class TestHealthReportSettingsProxy:
    """Expose an overridable proxy bound to the section's enum member."""

    def test_health_report_settings_is_overridable_proxy(self) -> None:
        """``health_report_settings`` is an ``OverridableSettingsProxy``."""
        assert isinstance(health_report_settings, OverridableSettingsProxy)

    def test_proxy_uses_class_name_identifier(self) -> None:
        """Bind the proxy to the Pydantic class ``__name__``, not the storage token."""
        assert (
            health_report_settings.get_setting_class() == HealthReportSettings.__name__
        )


class TestHealthReportSettingsOverridePosture:
    """Keep every field ``NOT_OVERRIDABLE`` after the rehome."""

    @pytest.mark.parametrize(
        "field",
        [
            "schedules",
            "upload",
            "endpoint",
            "api_key",
            "client_id",
            "artifact_dir",
            "artifact_ttl",
            "cleanup_interval",
        ],
    )
    def test_no_field_is_hot_reloadable(self, field: str) -> None:
        """Assert each health report field is left unmarked."""
        assert is_hot_reloadable(HealthReportSettings, field) is False
