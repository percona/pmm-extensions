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

"""Define the report plugin settings section.

The section is read straight off YAML/env rather than mounted as a field on
``ExtensionsSettings``: importing this module runs the report package ``__init__``, which
pulls in the app definition and, transitively, ``extensions_settings`` — so a field
default typed with :class:`HealthReportSettings` would cycle while
``ExtensionsSettings`` is still being constructed. Consumers import
:data:`health_report_settings` at call time (periodic task schedules, Celery
tasks, API routes), matching how alerts reads its section.
"""

__all__ = [
    "HealthReportSettings",
    "ReportScheduleEntry",
    "health_report_settings",
]

from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import field_validator, PositiveInt, SecretStr

from app.core.celery.models import CrontabSchedule, IntervalSchedule, Period
from app.core.config import BaseYamlSettings
from app.core.models import BaseLowercaseModel, BaseTransformFieldsModel
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.utils.fields import StrRelativePath
from app.core.utils.strings import lower_if_string


class ReportScheduleEntry(BaseLowercaseModel):
    """Define a single scheduled report generation with its own cadence and parameters.

    :param schedule: When to run (interval or crontab).
    :param since: Prometheus-style start offset for the report window.
    :param until: Prometheus-style end offset for the report window.
    :param full: Whether to generate a full report.
    :param refresh: Re-run advisor checks before collecting results.
    :param sections: Optional list of report sections to include.
    :param upload: Upload the generated report to ServiceNow after generation.
        Requires global upload credentials to be configured.
    """

    schedule: IntervalSchedule | CrontabSchedule
    since: str = "now-7d"
    until: str = "now"
    full: bool = True
    refresh: bool = False
    sections: list[str] | None = None
    upload: bool = False


class HealthReportSettings(BaseYamlSettings, BaseTransformFieldsModel):
    """Define configuration options for the Health & Security Report plugin.

    :cvar SETTINGS_PREFIXES: The prefixes for health-report settings in the
        configuration file. Set to ``["EXTENSIONS", "HEALTH_REPORT"]`` so the section
        lives under ``EXTENSIONS.HEALTH_REPORT``.
    :param schedules: List of report generation schedules, each with its own
        cadence and parameters.  Empty by default (no periodic generation).
    :param upload: Master toggle for ServiceNow upload.  When ``False``
        (the default) uploading is disabled regardless of other fields.
    :param endpoint: The ServiceNow upload API URL.
    :param api_key: API key for authenticating with the upload endpoint.
    :param client_id: Customer identifier sent with each upload.
    :param artifact_dir: Directory where rendered PDF artifacts are staged for
        download. Shared between the Celery worker (writer) and web (reader), so
        only lightweight job metadata transits the Celery result backend.
    :param artifact_ttl: Maximum age (seconds) of a staged PDF artifact before the
        cleanup task removes it. Should mirror ``CELERY.RESULT_EXPIRES`` so a
        job's metadata and its artifact expire together.
    :param cleanup_interval: Cadence of the ``purge_report_artifacts`` sweep that
        deletes staged PDFs older than ``artifact_ttl``.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "HEALTH_REPORT"]
    TRANSFORM_CALLABLE: ClassVar[Callable[[Any], Any]] = lower_if_string
    TRANSFORM_DEEP: ClassVar[bool] = True
    schedules: list[ReportScheduleEntry] = []
    upload: bool = False
    endpoint: str | None = None
    api_key: SecretStr | None = None
    client_id: str | None = None
    artifact_dir: StrRelativePath = "data/health-reports"
    artifact_ttl: PositiveInt = 3600
    cleanup_interval: IntervalSchedule = IntervalSchedule(
        every=15, period=Period.MINUTES
    )

    @field_validator("endpoint", "client_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("endpoint", mode="after")
    @classmethod
    def _normalize_endpoint(cls, v: str | None) -> str | None:
        """Trim a bare origin's trailing slash while preserving a path's.

        An intake may route ``/v1/upload/`` and ``/v1/upload`` differently,
        answering the slashless spelling with a redirect that replays the
        request body — credentials included — to the redirect target. The
        configured path is therefore sent exactly as written.

        :param v: The configured endpoint, or ``None`` when unset.
        :return: The endpoint with only a bare origin's trailing slash removed.
        """
        if v is None:
            return None
        return v if urlparse(v).path.strip("/") else v.rstrip("/")

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if isinstance(raw, str) and not raw.strip():
            return None
        return v

    @property
    def upload_disabled_reasons(self) -> list[str]:
        """Return a list of reasons why uploading is not possible.

        An empty list means upload is fully configured and ready.
        """
        if not self.upload:
            return ["Upload is disabled"]

        reasons = []
        if self.endpoint is None:
            reasons.append("Endpoint is not configured")
        else:
            parsed = urlparse(self.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                reasons.append("Endpoint is not a valid HTTP/HTTPS address")
        if self.api_key is None:
            reasons.append("API key is not configured")
        if self.client_id is None:
            reasons.append("Client ID is not configured")
        return reasons

    @property
    def is_upload_configured(self) -> bool:
        """Return ``True`` when upload is enabled and all credentials are set."""
        return not self.upload_disabled_reasons


health_report_settings: HealthReportSettings = OverridableSettingsProxy(
    HealthReportSettings
)
