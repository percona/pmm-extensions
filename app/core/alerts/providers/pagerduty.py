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

"""Provide a PagerDuty alert provider."""

from enum import StrEnum
from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    validate_call,
)

from app.core.alerts.models import Alert, BaseAlertProvider
from app.core.config import settings
from app.core.requests import RemoteAPI
from app.core.utils.fields import EnumFieldMixin, NonEmptyStr


class PagerDutyAlertSeverity(EnumFieldMixin, StrEnum):
    """Define alert severity levels."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class PagerDutyAlert(Alert):
    """Define a PagerDuty alert model.

    This model extends the base Alert model to include PagerDuty-specific fields.

    :param severity: The severity level of the alert, indicating its importance or
        urgency. Must be one of the PagerDutyAlertSeverity values.
    :type severity: PagerDutyAlertSeverity
    :param dedup_key: A unique key to deduplicate alerts. If provided, PagerDuty will
        not trigger a new incident if an alert with the same dedup_key is already
        active. Defaults to None.
    :type dedup_key: NonEmptyStr | None
    :param component: The component affected by the alert, such as a service or
        application. Defaults to None.
    :type component: NonEmptyStr | None
    :param group: The group associated with the alert, such as a team or department.
        Defaults to None.
    :type group: NonEmptyStr | None
    :param class_: The class of the alert, which can be used to categorize it.
        Defaults to None.
    :type class_: NonEmptyStr | None
    :param custom_details: Additional custom details to include in the alert.
        Defaults to None.
    :type custom_details: dict[str, Any] | None
    :param images: A list of images to include in the alert, each represented as a
        dictionary with 'src', 'href', and 'alt' keys. Defaults to None.
    :type images: list[dict[str, str]] | None
    :param links: A list of links to include in the alert, each represented as a
        dictionary with 'href' and 'text' keys. Defaults to None.
    :type links: list[dict[str, str]] | None
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    severity: PagerDutyAlertSeverity
    dedup_key: NonEmptyStr | None = None
    component: NonEmptyStr | None = None
    group: NonEmptyStr | None = None
    class_: NonEmptyStr | None = Field(None, alias="class")
    custom_details: dict[str, Any] | None = None
    images: list[dict[str, str]] | None = None
    links: list[dict[str, str]] | None = None


class PagerDutyEventsAlertProvider(BaseAlertProvider):
    """Define a PagerDuty alert provider.

    This provider sends alerts to PagerDuty using the Events API v2.

    :param api_endpoint: The API endpoint for Events v2. Override to point at a
        local capture server for end-to-end testing.
    :type api_endpoint: str
    :param routing_key: The routing key used for API requests.
    :type routing_key: SecretStr
    """

    api_endpoint: str = "https://events.pagerduty.com/v2/"
    routing_key: SecretStr

    def __hash__(self) -> int:
        return hash((self.__class__.__name__, self.routing_key.get_secret_value()))

    async def get_api(self) -> RemoteAPI:
        """Get the PagerDuty API client.

        :return: The RemoteAPI client for PagerDuty.
        :rtype: RemoteAPI
        """
        return await settings.get_remote_api(endpoint=self.api_endpoint)

    async def send_alert(self, alert: Alert) -> None:
        """Send an alert to PagerDuty using the Events API v2.

        The dispatcher hands every provider the base :class:`Alert`, so the
        PagerDuty-specific fields are resolved here rather than by widening what
        the base contract promises. Carries no ``validate_call``: that would
        coerce the argument to :class:`Alert` first, and ``AlertSeverity`` has
        neither the lowercase values nor the name-or-value lookup that
        ``PagerDutyAlertSeverity`` accepts, so a mapping naming ``"critical"``
        would be rejected before reaching the conversion below.

        :param alert: The alert to be sent, as an :class:`Alert`, a
            :class:`PagerDutyAlert`, or a mapping of either's fields.
        :raises ValidationError: If ``alert`` carries no PagerDuty severity.
        """
        if not isinstance(alert, PagerDutyAlert):
            alert = PagerDutyAlert.model_validate(alert, from_attributes=True)
        pagerduty_api = await self.get_api()
        with pagerduty_api.redact_body_fields({"routing_key"}):
            await pagerduty_api.post(
                "enqueue",
                json={
                    "routing_key": self.routing_key.get_secret_value(),
                    "event_action": "trigger",
                    **alert.model_dump(
                        include={"dedup_key", "images", "links"}, exclude_none=True
                    ),
                    "payload": alert.model_dump(
                        by_alias=True,
                        exclude={"dedup_key", "images", "links"},
                        exclude_none=True,
                    ),
                },
            )

    @validate_call
    async def resolve_alert(self, dedup_key: NonEmptyStr) -> None:
        """Resolve a PagerDuty alert by dedup key.

        Send an ``event_action: "resolve"`` event to the PagerDuty Events API
        v2. Resolving a non-existent incident is a harmless no-op (PD returns
        202).

        :param dedup_key: The deduplication key of the alert to resolve.
        :type dedup_key: NonEmptyStr
        """
        pagerduty_api = await self.get_api()
        with pagerduty_api.redact_body_fields({"routing_key"}):
            await pagerduty_api.post(
                "enqueue",
                json={
                    "routing_key": self.routing_key.get_secret_value(),
                    "event_action": "resolve",
                    "dedup_key": dedup_key,
                },
            )
