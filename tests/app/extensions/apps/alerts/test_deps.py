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

"""Define tests for the app.extensions.apps.alerts.deps module."""

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ServerDisconnectedError
from fastapi.exceptions import HTTPException
from polyfactory.factories.pydantic_factory import ModelFactory

import app.extensions.deps as extensions_deps
from app.extensions.apps.alerts import deps as alerts_deps
from app.extensions.apps.alerts.deps import (
    ensure_pagerduty_notification_route,
    get_or_create_alert_folder,
    get_pagerduty_status,
    get_pmm_present_names,
    PAGERDUTY_CONTACT_POINT_NAME,
)
from app.extensions.apps.alerts.models import (
    AlertTemplate,
)
from app.extensions.clients.pmm import AlertTemplate as PMMAlertTemplate
from app.extensions.clients.pmm import (
    ContactPoint,
    Folder,
    NotificationPolicy,
    PMMRemoteAPI,
)

_TRANSPORT_ERRORS = pytest.mark.parametrize(
    "error",
    [OSError("unreachable"), ServerDisconnectedError()],
    ids=["oserror", "client_error"],
)
"""Parametrize a test over an ``OSError`` and a non-``OSError`` aiohttp failure."""


class AlertTemplateFactory(ModelFactory[AlertTemplate]):
    """Define factory for AlertTemplate instances."""


class TestGetPmmPresentNames:
    """Test the ``get_pmm_present_names`` dependency."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pmm_api(self):
        """Assert ``None`` is returned when the PMM API is not available."""
        result = await get_pmm_present_names(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_set_of_names_on_success(self):
        """Assert a set of template names is returned on success."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_templates.return_value = [
            PMMAlertTemplate(name="template-a", summary="A", template="yaml"),
            PMMAlertTemplate(name="template-b", summary="B", template="yaml"),
        ]
        result = await get_pmm_present_names(mock_api)
        assert result == {"template-a", "template-b"}

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_no_templates(self):
        """Assert an empty set is returned when PMM has no templates."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_templates.return_value = []
        result = await get_pmm_present_names(mock_api)
        assert result == set()

    @pytest.mark.asyncio
    @_TRANSPORT_ERRORS
    async def test_returns_none_on_connection_error(self, error):
        """Assert ``None`` is returned when PMM is unreachable."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_templates.side_effect = error
        result = await get_pmm_present_names(mock_api)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_exception(self):
        """Assert ``None`` is returned on HTTP error from PMM."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_templates.side_effect = HTTPException(
            status_code=502, detail="Bad Gateway"
        )
        result = await get_pmm_present_names(mock_api)
        assert result is None


class TestGetPagerdutyStatus:
    """Test the ``get_pagerduty_status`` dependency."""

    @pytest.mark.asyncio
    async def test_returns_none_when_pmm_unavailable(self):
        """Assert ``None`` is returned when PMM API is not available."""
        result = await get_pagerduty_status(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_configured_when_pd_contact_point_exists(self):
        """Assert configured status when PD contact point exists."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.return_value = [
            ContactPoint(
                uid="cp-1",
                name="PMM Extensions PagerDuty",
                type="pagerduty",
                settings={"integrationKey": "abcdefghij1234"},
            ),
        ]
        result = await get_pagerduty_status(mock_api)
        assert result == {
            "configured": True,
            "uid": "cp-1",
        }

    @pytest.mark.asyncio
    async def test_returns_not_configured_when_no_pd_contact_point(self):
        """Assert not-configured status when no PD contact point exists."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.return_value = [
            ContactPoint(
                uid="cp-slack",
                name="Slack",
                type="slack",
                settings={},
            ),
        ]
        result = await get_pagerduty_status(mock_api)
        assert result == {"configured": False}

    @pytest.mark.asyncio
    async def test_returns_not_configured_when_empty_contact_points(self):
        """Assert not-configured status when contact points list is empty."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.return_value = []
        result = await get_pagerduty_status(mock_api)
        assert result == {"configured": False}

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        """Assert ``None`` is returned on HTTP error from PMM."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.side_effect = HTTPException(
            status_code=502, detail="Bad Gateway"
        )
        result = await get_pagerduty_status(mock_api)
        assert result is None

    @pytest.mark.asyncio
    @_TRANSPORT_ERRORS
    async def test_returns_none_on_connection_error(self, error):
        """Assert ``None`` is returned when PMM is unreachable."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.side_effect = error
        result = await get_pagerduty_status(mock_api)
        assert result is None

    @pytest.mark.asyncio
    async def test_ignores_pagerduty_contact_point_with_different_name(self):
        """Assert not-configured status when PD contact point has a different name."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_contact_points.return_value = [
            ContactPoint(
                uid="cp-other",
                name="Other PagerDuty",
                type="pagerduty",
                settings={"integrationKey": "some-key-1234"},
            ),
        ]
        result = await get_pagerduty_status(mock_api)
        assert result == {"configured": False}


class TestEnsurePagerdutyNotificationRoute:
    """Test the ``ensure_pagerduty_notification_route`` helper."""

    @pytest.mark.asyncio
    async def test_creates_route_when_not_present(self):
        """Assert a notification route is appended when none exists."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.get_notification_policy.return_value = NotificationPolicy(
            receiver="default",
            routes=[],
        )
        await ensure_pagerduty_notification_route(
            mock_api, PAGERDUTY_CONTACT_POINT_NAME
        )
        mock_api.update_notification_policy.assert_awaited_once()
        updated_policy = mock_api.update_notification_policy.call_args[0][0]
        assert len(updated_policy.routes) == 1
        assert updated_policy.routes[0]["receiver"] == PAGERDUTY_CONTACT_POINT_NAME

    @pytest.mark.asyncio
    async def test_does_not_duplicate_existing_route(self):
        """Assert no route is added when one already exists."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.get_notification_policy.return_value = NotificationPolicy(
            receiver="default",
            routes=[{"receiver": PAGERDUTY_CONTACT_POINT_NAME}],
        )
        await ensure_pagerduty_notification_route(
            mock_api, PAGERDUTY_CONTACT_POINT_NAME
        )
        mock_api.update_notification_policy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preserves_existing_routes(self):
        """Assert existing routes are preserved when appending a new one."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        existing_route = {"receiver": "slack-channel"}
        mock_api.get_notification_policy.return_value = NotificationPolicy(
            receiver="default",
            routes=[existing_route],
        )
        await ensure_pagerduty_notification_route(
            mock_api, PAGERDUTY_CONTACT_POINT_NAME
        )
        updated_policy = mock_api.update_notification_policy.call_args[0][0]
        expected_route_count = 2
        assert len(updated_policy.routes) == expected_route_count
        assert updated_policy.routes[0] == existing_route


class TestGetOrCreateAlertFolder:
    """Test the ``get_or_create_alert_folder`` dependency."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pmm_api(self):
        """Assert ``None`` is returned when the PMM API is not available."""
        result = await get_or_create_alert_folder(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_existing_folder(self):
        """Assert the existing folder is returned when it matches."""
        existing = Folder(uid="f-1", title="PMM Extensions Alerts", id=1)
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_folders.return_value = [existing]

        with patch("app.extensions.apps.alerts.deps.alerts_settings") as mock_config:
            mock_config.ALERT_FOLDER_NAME = "PMM Extensions Alerts"
            result = await get_or_create_alert_folder(mock_api)

        assert result is existing
        mock_api.create_folder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_folder_when_missing(self):
        """Assert a new folder is created when none matches."""
        created = Folder(uid="f-new", title="PMM Extensions Alerts", id=42)
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_folders.return_value = []
        mock_api.create_folder.return_value = created

        with patch("app.extensions.apps.alerts.deps.alerts_settings") as mock_config:
            mock_config.ALERT_FOLDER_NAME = "PMM Extensions Alerts"
            result = await get_or_create_alert_folder(mock_api)

        assert result is created
        mock_api.create_folder.assert_awaited_once_with("PMM Extensions Alerts")

    @pytest.mark.asyncio
    async def test_ignores_non_matching_folders(self):
        """Assert folders with different titles are ignored."""
        other = Folder(uid="f-other", title="Other Folder", id=2)
        created = Folder(uid="f-new", title="PMM Extensions Alerts", id=42)
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_folders.return_value = [other]
        mock_api.create_folder.return_value = created

        with patch("app.extensions.apps.alerts.deps.alerts_settings") as mock_config:
            mock_config.ALERT_FOLDER_NAME = "PMM Extensions Alerts"
            result = await get_or_create_alert_folder(mock_api)

        assert result is created

    @pytest.mark.asyncio
    @_TRANSPORT_ERRORS
    async def test_returns_none_on_connection_error(self, error):
        """Assert ``None`` is returned when PMM is unreachable."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_folders.side_effect = error

        with patch("app.extensions.apps.alerts.deps.alerts_settings") as mock_config:
            mock_config.ALERT_FOLDER_NAME = "PMM Extensions Alerts"
            result = await get_or_create_alert_folder(mock_api)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_exception(self):
        """Assert ``None`` is returned on HTTP error from PMM."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_folders.side_effect = HTTPException(
            status_code=502, detail="Bad Gateway"
        )

        with patch("app.extensions.apps.alerts.deps.alerts_settings") as mock_config:
            mock_config.ALERT_FOLDER_NAME = "PMM Extensions Alerts"
            result = await get_or_create_alert_folder(mock_api)

        assert result is None


class TestPmmDepReExports:
    """Assert the alerts PMM deps are re-exports of the ``app.extensions.deps`` originals."""

    @pytest.mark.parametrize(
        "name",
        ["get_pmm_api", "require_pmm_api", "PMMAPIDep", "RequiredPMMAPIDep"],
    )
    def test_symbol_is_same_object_as_extensions_deps(self, name):
        """Assert each re-exported symbol is identical to its ``app.extensions.deps`` original.

        Identity is load-bearing: ``dependency_overrides`` and ``mocker.patch`` bind by
        object identity, so a local re-definition would silently break production overrides
        while leaving the suite green.

        :param name: The re-exported symbol name to compare.
        """
        assert getattr(alerts_deps, name) is getattr(extensions_deps, name)
