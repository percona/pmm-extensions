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

"""Define tests for the alerting methods of app.extensions.clients.pmm."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponse, ClientResponseError
from fastapi import HTTPException
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from app.core.exceptions import HTTPNotFoundException
from app.extensions.clients.pmm import (
    AlertRule,
    AlertTemplate,
    ContactPoint,
    Folder,
    NotificationPolicy,
    PMMRemoteAPI,
)

ALERTING_HEADERS = {"X-Disable-Provenance": "true"}


def _client_response_error(status_code: int) -> ClientResponseError:
    """Build a ``ClientResponseError`` simulating a failed PMM HTTP response."""
    return ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=status_code,
        message="PMM error",
    )


@pytest.fixture
def pmm_remote_api() -> Iterator[PMMRemoteAPI]:
    """Yield a PMMRemoteAPI instance, clearing its version cache on teardown."""
    pmm_remote_api = PMMRemoteAPI(endpoint="http://localhost", api_key="test-key")
    yield pmm_remote_api
    pmm_remote_api.is_older_than_v3.cache_clear()


@pytest.fixture
def mock_request(mocker) -> AsyncMock:
    """Mock the request method on PMMRemoteAPI."""
    return mocker.patch.object(PMMRemoteAPI, "request", new_callable=AsyncMock)


@pytest.fixture
def mock_get_version(mocker) -> AsyncMock:
    """Mock get_version on PMMRemoteAPI."""
    return mocker.patch(
        "app.extensions.clients.pmm.PMMRemoteAPI.get_version", new_callable=AsyncMock
    )


@pytest.fixture
def captured_requests(mocker) -> tuple[list[dict], MagicMock]:
    """Patch ``PMMRemoteAPI._request`` to capture calls at the HTTP boundary.

    :param mocker: Pytest-mock fixture used to patch ``PMMRemoteAPI._request``.
    :return: A ``(captured, response)`` tuple where ``captured`` accumulates one
        ``{"method", "path", "kwargs"}`` record per ``_request`` call and ``response``
        is the shared :class:`ClientResponse` mock each call yields.
    """
    captured: list[dict] = []
    mock_response = MagicMock(spec=ClientResponse)
    mock_response.raise_for_status = MagicMock()

    @asynccontextmanager
    async def fake_request(self_arg, method: str, path: str, **kwargs):
        captured.append({"method": method, "path": path, "kwargs": kwargs})
        yield mock_response

    mocker.patch.object(PMMRemoteAPI, "_request", fake_request)
    return captured, mock_response


class TestAlertingHeaders:
    """Test the alerting_headers property."""

    def test_alerting_headers_returns_x_disable_provenance(
        self, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test alerting_headers returns the X-Disable-Provenance header."""
        assert pmm_remote_api.alerting_headers == {"X-Disable-Provenance": "true"}


class TestAuthorizationHeaders:
    """Test the Authorization header on PMM requests."""

    def test_headers_carry_bearer_authorization(
        self, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Assert the complete headers including Bearer Authorization."""
        assert pmm_remote_api.headers == {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer test-key",
        }


class TestCreateTemplate:
    """Test the create_template method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_path"),
        [
            ("2.0.0", "/v1/management/alerting/Templates/Create"),
            ("3.0.0", "/v1/alerting/templates"),
        ],
    )
    async def test_create_template_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_path: str,
    ) -> None:
        """Test create_template uses the correct PMM v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "name": "cpu-high",
            "summary": "CPU usage is high",
            "template": "name: cpu-high\n",
        }

        result = await pmm_remote_api.create_template("name: cpu-high\n")

        assert isinstance(result, AlertTemplate)
        assert result.name == "cpu-high"
        assert result.summary == "CPU usage is high"
        mock_request.assert_awaited_once_with(
            "POST",
            expected_path,
            json={"yaml": "name: cpu-high\n"},
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_create_template_returns_none_on_empty_response(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test create_template returns ``None`` when PMM v3 returns an empty dict."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {}

        result = await pmm_remote_api.create_template("name: cpu-high\n")

        assert result is None


class TestListTemplates:
    """Test the list_templates method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_method", "expected_path", "expected_kwargs"),
        [
            (
                "2.0.0",
                "POST",
                "/v1/management/alerting/Templates/List",
                {"json": {}, "headers": ALERTING_HEADERS},
            ),
            (
                "3.0.0",
                "GET",
                "/v1/alerting/templates",
                {
                    "params": {},
                    "headers": ALERTING_HEADERS,
                },
            ),
        ],
    )
    async def test_list_templates_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_method: str,
        expected_path: str,
        expected_kwargs: dict,
    ) -> None:
        """Test list_templates uses the correct PMM v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "templates": [
                {"name": "tmpl-1", "summary": "Summary 1", "template": "yaml1"},
                {"name": "tmpl-2", "summary": "Summary 2", "template": "yaml2"},
            ]
        }

        expected_template_count = 2

        result = await pmm_remote_api.list_templates()

        assert len(result) == expected_template_count
        assert all(isinstance(t, AlertTemplate) for t in result)
        assert result[0].name == "tmpl-1"
        assert result[1].name == "tmpl-2"
        mock_request.assert_awaited_once_with(
            expected_method, expected_path, **expected_kwargs
        )

    @pytest.mark.asyncio
    async def test_list_templates_returns_empty_list_when_no_templates(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test list_templates returns empty list when response has no templates."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {"templates": []}

        result = await pmm_remote_api.list_templates()

        assert result == []


class TestTemplateExists:
    """Test the template_exists method."""

    @pytest.mark.asyncio
    async def test_template_exists_returns_true_when_found(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test template_exists returns True when a matching template exists."""
        mocker.patch.object(
            PMMRemoteAPI,
            "list_templates",
            new=AsyncMock(
                return_value=[
                    AlertTemplate(name="target-template", summary="", template=""),
                    AlertTemplate(name="other-template", summary="", template=""),
                ]
            ),
        )

        result = await pmm_remote_api.template_exists("target-template")

        assert result is True

    @pytest.mark.asyncio
    async def test_template_exists_returns_false_when_not_found(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test template_exists returns False when no matching template exists."""
        mocker.patch.object(
            PMMRemoteAPI,
            "list_templates",
            new=AsyncMock(
                return_value=[
                    AlertTemplate(name="other-template", summary="", template=""),
                ]
            ),
        )

        result = await pmm_remote_api.template_exists("missing-template")

        assert result is False

    @pytest.mark.asyncio
    async def test_template_exists_returns_false_when_list_is_empty(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test template_exists returns False when template list is empty."""
        mocker.patch.object(
            PMMRemoteAPI,
            "list_templates",
            new=AsyncMock(return_value=[]),
        )

        result = await pmm_remote_api.template_exists("any-template")

        assert result is False


class TestCreateRule:
    """Test the create_rule method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_path"),
        [
            ("2.0.0", "/v1/management/alerting/Rules/Create"),
            ("3.0.0", "/v1/alerting/rules"),
        ],
    )
    async def test_create_rule_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_path: str,
    ) -> None:
        """Test create_rule uses the correct PMM v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "uid": "rule-abc",
            "title": "High CPU",
            "labels": {"severity": "critical"},
            "annotations": {},
            "data": [],
        }

        result = await pmm_remote_api.create_rule(
            name="High CPU",
            template_name="cpu-high",
            folder_uid="folder-1",
            for_duration="300s",
            group="infra-alerts",
            labels={"severity": "critical"},
        )

        assert isinstance(result, AlertRule)
        assert result.uid == "rule-abc"
        assert result.title == "High CPU"
        mock_request.assert_awaited_once_with(
            "POST",
            expected_path,
            json={
                "name": "High CPU",
                "template_name": "cpu-high",
                "folder_uid": "folder-1",
                "for": "300s",
                "group": "infra-alerts",
                "labels": {"severity": "critical"},
            },
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_create_rule_raises_not_found_on_coded_404(
        self, mocker, mock_get_version: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Map PMM's gRPC-gateway coded 404 to HTTPNotFoundException, not a bare one.

        PMM returns ``{"code": 5, "message": ...}`` for a missing template; the
        ``code`` field must no longer force the bare-``HTTPException`` fallback, so
        callers can narrow to ``except HTTPNotFoundException``.
        """
        mock_get_version.return_value = "3.0.0"
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.status = HTTP_404_NOT_FOUND
        mock_response.json = AsyncMock(
            return_value={"code": 5, "message": "template not found"}
        )
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(HTTP_404_NOT_FOUND)
        )

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        with pytest.raises(HTTPNotFoundException) as exc_info:
            await pmm_remote_api.create_rule(
                name="High CPU",
                template_name="missing",
                folder_uid="folder-1",
                for_duration="300s",
                group="infra-alerts",
            )
        assert type(exc_info.value) is HTTPNotFoundException
        assert exc_info.value.detail == "template not found"
        assert exc_info.value.headers == {"X-Error-Code": "5"}

    @pytest.mark.asyncio
    async def test_create_rule_with_params(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test create_rule includes params in the request body when provided."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {
            "uid": "rule-xyz",
            "title": "Disk Full",
            "labels": {},
            "annotations": {},
            "data": [],
        }
        params = [{"name": "threshold", "type": "FLOAT", "float": "0.9"}]

        await pmm_remote_api.create_rule(
            name="Disk Full",
            template_name="disk-full",
            folder_uid="folder-1",
            for_duration="10m",
            group="disk-alerts",
            params=params,
        )

        call_json = mock_request.call_args.kwargs["json"]
        assert call_json["params"] == params

    @pytest.mark.asyncio
    async def test_create_rule_omits_none_labels_and_params(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test create_rule omits labels and params from body when they are None."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {
            "uid": "rule-min",
            "title": "Minimal Rule",
            "labels": {},
            "annotations": {},
            "data": [],
        }

        await pmm_remote_api.create_rule(
            name="Minimal Rule",
            template_name="tmpl",
            folder_uid="folder-1",
            for_duration="1m",
            group="group-1",
        )

        call_json = mock_request.call_args.kwargs["json"]
        assert "labels" not in call_json
        assert "params" not in call_json

    @pytest.mark.asyncio
    async def test_create_rule_returns_none_on_empty_response(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test create_rule returns ``None`` when PMM v3 returns an empty dict."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {}

        result = await pmm_remote_api.create_rule(
            name="High CPU",
            template_name="cpu-high",
            folder_uid="folder-1",
            for_duration="300s",
            group="infra-alerts",
        )

        assert result is None


class TestListRules:
    """Test the list_rules method."""

    @pytest.mark.asyncio
    async def test_list_rules_flattens_nested_response(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_rules flattens the nested folder/group/rules structure."""
        mock_request.return_value = {
            "folder-1": [
                {
                    "name": "group-1",
                    "rules": [
                        {
                            "grafana_alert": {
                                "uid": "rule-1",
                                "title": "Rule 1",
                                "labels": {"env": "prod"},
                                "annotations": {"summary": "Alert summary"},
                                "data": [{"refId": "A"}],
                            }
                        }
                    ],
                }
            ],
            "folder-2": [
                {
                    "name": "group-2",
                    "rules": [
                        {
                            "grafana_alert": {
                                "uid": "rule-2",
                                "title": "Rule 2",
                                "labels": {},
                                "annotations": {},
                                "data": [],
                            }
                        }
                    ],
                }
            ],
        }

        expected_rule_count = 2

        result = await pmm_remote_api.list_rules()

        assert len(result) == expected_rule_count
        assert all(isinstance(r, AlertRule) for r in result)
        uids = {r.uid for r in result}
        assert uids == {"rule-1", "rule-2"}
        rule1 = next(r for r in result if r.uid == "rule-1")
        assert rule1.title == "Rule 1"
        assert rule1.labels == {"env": "prod"}
        mock_request.assert_awaited_once_with(
            "GET",
            "/graph/api/ruler/grafana/api/v1/rules/",
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_list_rules_returns_empty_list_when_no_rules(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_rules returns an empty list when the response is empty."""
        mock_request.return_value = {}

        result = await pmm_remote_api.list_rules()

        assert result == []


class TestDeleteRule:
    """Test the delete_rule method."""

    @pytest.mark.asyncio
    async def test_delete_rule_sends_delete_with_correct_uid_and_headers(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_rule sends a DELETE request with the uid and alerting headers."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.raise_for_status = MagicMock()
        captured = []

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            captured.append({"method": method, "path": path, "kwargs": kwargs})
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        await pmm_remote_api.delete_rule("rule-uid-123")

        assert len(captured) == 1
        assert captured[0]["method"] == "DELETE"
        assert (
            captured[0]["path"] == "/graph/api/v1/provisioning/alert-rules/rule-uid-123"
        )
        assert captured[0]["kwargs"].get("headers") == ALERTING_HEADERS
        mock_response.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_rule_returns_none(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_rule returns None on success."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.raise_for_status = MagicMock()

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        result = await pmm_remote_api.delete_rule("rule-uid-123")

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_rule_raises_not_found_on_error_response(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_rule maps a JSON upstream 404 to ``HTTPNotFoundException``."""
        error_status = HTTP_404_NOT_FOUND
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.content_type = "application/json"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(error_status)
        )

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        with pytest.raises(HTTPNotFoundException) as exc_info:
            await pmm_remote_api.delete_rule("missing-uid")

        assert type(exc_info.value) is HTTPNotFoundException
        assert exc_info.value.status_code == error_status

    @pytest.mark.asyncio
    async def test_delete_rule_non_json_404_stays_bare(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test a non-JSON (proxy) 404 stays a bare ``HTTPException``, not not-found."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.content_type = "text/html"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(HTTP_404_NOT_FOUND)
        )

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.delete_rule("missing-uid")

        assert type(exc_info.value) is HTTPException
        assert not isinstance(exc_info.value, HTTPNotFoundException)
        assert exc_info.value.status_code == HTTP_404_NOT_FOUND


class TestUpdateRule:
    """Test the update_rule method."""

    @pytest.mark.asyncio
    async def test_update_rule_calls_delete_then_create_in_order(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test update_rule deletes the old rule then creates the new one, in order."""
        captured, mock_response = captured_requests
        mock_get_version.return_value = "3.0.0"
        mock_response.json = AsyncMock(
            return_value={
                "uid": "new-uid",
                "title": "Updated Rule",
                "labels": {},
                "annotations": {},
                "data": [],
            }
        )

        result = await pmm_remote_api.update_rule(
            uid="old-uid",
            name="Updated Rule",
            template_name="tmpl",
            folder_uid="folder-1",
            for_duration="10m",
            group="group-1",
        )

        assert isinstance(result, AlertRule)
        assert result.uid == "new-uid"

        # The delete-then-create ordering invariant is derived from the ordered
        # sequence of real ``_request`` calls, not from patched subject methods.
        expected_request_count = 2
        assert len(captured) == expected_request_count

        assert captured[0]["method"] == "DELETE"
        assert captured[0]["path"] == "/graph/api/v1/provisioning/alert-rules/old-uid"
        assert captured[0]["kwargs"].get("headers") == ALERTING_HEADERS

        assert captured[1]["method"] == "POST"
        assert captured[1]["path"] == "/v1/alerting/rules"
        assert captured[1]["kwargs"].get("headers") == ALERTING_HEADERS
        assert captured[1]["kwargs"].get("json") == {
            "name": "Updated Rule",
            "template_name": "tmpl",
            "folder_uid": "folder-1",
            "for": "10m",
            "group": "group-1",
        }

    @pytest.mark.asyncio
    async def test_update_rule_returns_none_when_recreate_yields_empty(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test update_rule returns ``None`` when the recreate step gets no data."""
        captured, mock_response = captured_requests
        mock_get_version.return_value = "3.0.0"
        mock_response.json = AsyncMock(return_value={})

        result = await pmm_remote_api.update_rule(
            uid="old-uid",
            name="Updated Rule",
            template_name="tmpl",
            folder_uid="folder-1",
            for_duration="10m",
            group="group-1",
        )

        assert result is None


class TestListFolders:
    """Test the list_folders method."""

    @pytest.mark.asyncio
    async def test_list_folders_returns_folder_list(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_folders fetches and returns a list of Folder objects."""
        mock_request.return_value = [
            {"uid": "folder-1", "title": "Folder One", "id": 1},
            {"uid": "folder-2", "title": "Folder Two", "id": 2},
        ]

        expected_folder_count = 2

        result = await pmm_remote_api.list_folders()

        assert len(result) == expected_folder_count
        assert all(isinstance(f, Folder) for f in result)
        assert result[0].uid == "folder-1"
        assert result[0].title == "Folder One"
        assert result[0].id == 1
        mock_request.assert_awaited_once_with(
            "GET", "/graph/api/folders/", headers=ALERTING_HEADERS
        )

    @pytest.mark.asyncio
    async def test_list_folders_returns_empty_list_when_no_folders(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_folders returns an empty list when the API returns an empty list."""
        mock_request.return_value = []

        result = await pmm_remote_api.list_folders()

        assert result == []


class TestCreateFolder:
    """Test the create_folder method."""

    @pytest.mark.asyncio
    async def test_create_folder_posts_and_returns_folder(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test create_folder sends POST and returns a Folder object."""
        mock_request.return_value = {
            "uid": "new-folder-uid",
            "title": "PMM Extensions Alerts",
            "id": 42,
        }

        expected_id = 42

        result = await pmm_remote_api.create_folder("PMM Extensions Alerts")

        assert isinstance(result, Folder)
        assert result.uid == "new-folder-uid"
        assert result.title == "PMM Extensions Alerts"
        assert result.id == expected_id
        mock_request.assert_awaited_once_with(
            "POST",
            "/graph/api/folders/",
            json={"title": "PMM Extensions Alerts"},
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_create_folder_propagates_http_exception_on_error(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test create_folder propagates an ``HTTPException`` from the API."""
        error_status = HTTP_500_INTERNAL_SERVER_ERROR
        mock_request.side_effect = HTTPException(status_code=error_status)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.create_folder("PMM Extensions Alerts")

        assert exc_info.value.status_code == error_status


class TestListContactPoints:
    """Test the list_contact_points method."""

    @pytest.mark.asyncio
    async def test_list_contact_points_returns_contact_point_list(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_contact_points fetches and returns a list of ContactPoint objects."""
        mock_request.return_value = [
            {
                "uid": "cp-1",
                "name": "Slack",
                "type": "slack",
                "settings": {"url": "https://hooks.slack.com/test"},
            },
        ]

        result = await pmm_remote_api.list_contact_points()

        assert len(result) == 1
        assert isinstance(result[0], ContactPoint)
        assert result[0].uid == "cp-1"
        assert result[0].name == "Slack"
        assert result[0].type == "slack"
        mock_request.assert_awaited_once_with(
            "GET",
            "/graph/api/v1/provisioning/contact-points/",
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_list_contact_points_returns_empty_list_when_none(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test list_contact_points returns an empty list when the API returns an empty list."""
        mock_request.return_value = []

        result = await pmm_remote_api.list_contact_points()

        assert result == []


class TestCreateContactPoint:
    """Test the create_contact_point method."""

    @pytest.mark.asyncio
    async def test_create_contact_point_posts_to_provisioning_endpoint(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test create_contact_point posts to the Grafana provisioning endpoint."""
        mock_request.return_value = {
            "uid": "cp-new",
            "name": "Email",
            "type": "email",
            "settings": {"addresses": "admin@example.com"},
        }

        result = await pmm_remote_api.create_contact_point(
            name="Email",
            type_="email",
            settings={"addresses": "admin@example.com"},
        )

        assert isinstance(result, ContactPoint)
        assert result.uid == "cp-new"
        assert result.type == "email"
        mock_request.assert_awaited_once_with(
            "POST",
            "/graph/api/v1/provisioning/contact-points/",
            json={
                "name": "Email",
                "type": "email",
                "settings": {"addresses": "admin@example.com"},
            },
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_create_contact_point_propagates_http_exception_on_error(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test create_contact_point propagates an ``HTTPException`` from the API."""
        error_status = HTTP_500_INTERNAL_SERVER_ERROR
        mock_request.side_effect = HTTPException(status_code=error_status)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.create_contact_point(
                name="Email",
                type_="email",
                settings={"addresses": "admin@example.com"},
            )

        assert exc_info.value.status_code == error_status


class TestUpdateContactPoint:
    """Test the update_contact_point method."""

    @pytest.mark.asyncio
    async def test_update_contact_point_puts_to_provisioning_endpoint(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test update_contact_point sends a PUT with uid and alerting headers."""
        captured, mock_response = captured_requests

        await pmm_remote_api.update_contact_point(
            uid="cp-123",
            name="Updated Email",
            type_="email",
            settings={"addresses": "new@example.com"},
        )

        assert len(captured) == 1
        assert captured[0]["method"] == "PUT"
        assert captured[0]["path"] == "/graph/api/v1/provisioning/contact-points/cp-123"
        assert captured[0]["kwargs"].get("json") == {
            "uid": "cp-123",
            "name": "Updated Email",
            "type": "email",
            "settings": {"addresses": "new@example.com"},
        }
        assert captured[0]["kwargs"].get("headers") == ALERTING_HEADERS
        mock_response.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_contact_point_raises_http_exception_on_error(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test update_contact_point maps an upstream error to ``HTTPException``."""
        error_status = HTTP_409_CONFLICT
        _captured, mock_response = captured_requests
        mock_response.content_type = "application/json"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(error_status)
        )

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.update_contact_point(
                uid="cp-123",
                name="Updated Email",
                type_="email",
                settings={"addresses": "new@example.com"},
            )

        assert exc_info.value.status_code == error_status

    @pytest.mark.asyncio
    async def test_update_contact_point_raises_not_found_on_404(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test update_contact_point maps a JSON 404 to ``HTTPNotFoundException``."""
        _captured, mock_response = captured_requests
        mock_response.content_type = "application/json"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(HTTP_404_NOT_FOUND)
        )

        with pytest.raises(HTTPNotFoundException) as exc_info:
            await pmm_remote_api.update_contact_point(
                uid="cp-123",
                name="Updated Email",
                type_="email",
                settings={"addresses": "new@example.com"},
            )

        assert type(exc_info.value) is HTTPNotFoundException
        assert exc_info.value.status_code == HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_update_contact_point_non_json_404_stays_bare(
        self,
        captured_requests: tuple[list[dict], MagicMock],
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test a non-JSON (proxy) 404 stays a bare ``HTTPException``, not not-found."""
        _captured, mock_response = captured_requests
        mock_response.content_type = "text/html"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(HTTP_404_NOT_FOUND)
        )

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.update_contact_point(
                uid="cp-123",
                name="Updated Email",
                type_="email",
                settings={"addresses": "new@example.com"},
            )

        assert type(exc_info.value) is HTTPException
        assert not isinstance(exc_info.value, HTTPNotFoundException)
        assert exc_info.value.status_code == HTTP_404_NOT_FOUND


class TestDeleteContactPoint:
    """Test the delete_contact_point method."""

    @pytest.mark.asyncio
    async def test_delete_contact_point_sends_delete_request(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_contact_point sends a DELETE with uid and alerting headers."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.raise_for_status = MagicMock()
        captured = []

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            captured.append({"method": method, "path": path, "kwargs": kwargs})
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        await pmm_remote_api.delete_contact_point("cp-uid-456")

        assert len(captured) == 1
        assert captured[0]["method"] == "DELETE"
        assert (
            captured[0]["path"]
            == "/graph/api/v1/provisioning/contact-points/cp-uid-456"
        )
        assert captured[0]["kwargs"].get("headers") == ALERTING_HEADERS
        mock_response.raise_for_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_contact_point_returns_none(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_contact_point returns None on success."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.raise_for_status = MagicMock()

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        result = await pmm_remote_api.delete_contact_point("cp-uid-456")

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_contact_point_raises_not_found_on_error(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test delete_contact_point maps a JSON 404 to ``HTTPNotFoundException``."""
        error_status = HTTP_404_NOT_FOUND
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.content_type = "application/json"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(error_status)
        )

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        with pytest.raises(HTTPNotFoundException) as exc_info:
            await pmm_remote_api.delete_contact_point("missing-cp")

        assert type(exc_info.value) is HTTPNotFoundException
        assert exc_info.value.status_code == error_status

    @pytest.mark.asyncio
    async def test_delete_contact_point_non_json_404_stays_bare(
        self, mocker, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test a non-JSON (proxy) 404 stays a bare ``HTTPException``, not not-found."""
        mock_response = MagicMock(spec=ClientResponse)
        mock_response.content_type = "text/html"
        mock_response.raise_for_status = MagicMock(
            side_effect=_client_response_error(HTTP_404_NOT_FOUND)
        )

        @asynccontextmanager
        async def fake_request(self_arg, method: str, path: str, **kwargs):
            yield mock_response

        mocker.patch.object(PMMRemoteAPI, "_request", fake_request)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.delete_contact_point("missing-cp")

        assert type(exc_info.value) is HTTPException
        assert not isinstance(exc_info.value, HTTPNotFoundException)
        assert exc_info.value.status_code == HTTP_404_NOT_FOUND


class TestGetNotificationPolicy:
    """Test the get_notification_policy method."""

    @pytest.mark.asyncio
    async def test_get_notification_policy_returns_policy(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test get_notification_policy fetches and returns the policy tree."""
        mock_request.return_value = {
            "receiver": "grafana-default-email",
            "group_by": ["alertname"],
            "routes": [{"receiver": "slack", "match": {"severity": "critical"}}],
        }

        result = await pmm_remote_api.get_notification_policy()

        assert isinstance(result, NotificationPolicy)
        assert result.receiver == "grafana-default-email"
        assert result.group_by == ["alertname"]
        assert len(result.routes) == 1
        mock_request.assert_awaited_once_with(
            "GET", "/graph/api/v1/provisioning/policies", headers=ALERTING_HEADERS
        )

    @pytest.mark.asyncio
    async def test_get_notification_policy_propagates_http_exception_on_error(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test get_notification_policy propagates an ``HTTPException`` from the API."""
        error_status = HTTP_502_BAD_GATEWAY
        mock_request.side_effect = HTTPException(status_code=error_status)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.get_notification_policy()

        assert exc_info.value.status_code == error_status


class TestUpdateNotificationPolicy:
    """Test the update_notification_policy method."""

    @pytest.mark.asyncio
    async def test_update_notification_policy_puts_serialized_policy(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test update_notification_policy sends a PUT with the serialized policy."""
        policy = NotificationPolicy(
            receiver="slack",
            group_by=["alertname", "cluster"],
            routes=[{"receiver": "pagerduty", "match": {"severity": "critical"}}],
        )
        await pmm_remote_api.update_notification_policy(policy)

        mock_request.assert_awaited_once_with(
            "PUT",
            "/graph/api/v1/provisioning/policies",
            json=policy.model_dump(exclude_none=True),
            headers=ALERTING_HEADERS,
        )

    @pytest.mark.asyncio
    async def test_update_notification_policy_propagates_http_exception_on_error(
        self, mock_request: AsyncMock, pmm_remote_api: PMMRemoteAPI
    ) -> None:
        """Test update_notification_policy propagates an ``HTTPException`` from the API."""
        error_status = HTTP_500_INTERNAL_SERVER_ERROR
        mock_request.side_effect = HTTPException(status_code=error_status)
        policy = NotificationPolicy(receiver="slack")

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.update_notification_policy(policy)

        assert exc_info.value.status_code == error_status


class TestGetAdvisorChecks:
    """Test the get_advisor_checks method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_method", "expected_path", "expected_kwargs"),
        [
            (
                "2.44.1",
                "POST",
                "/v1/management/SecurityChecks/List",
                {"json": {}},
            ),
            (
                "3.6.0",
                "GET",
                "/v1/advisors/checks",
                {},
            ),
        ],
    )
    async def test_get_advisor_checks_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_method: str,
        expected_path: str,
        expected_kwargs: dict,
    ) -> None:
        """Test get_advisor_checks uses the correct v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "checks": [
                {
                    "name": "mysql_version",
                    "summary": "MySQL version",
                    "disabled": False,
                },
            ]
        }

        result = await pmm_remote_api.get_advisor_checks()

        assert len(result) == 1
        assert result[0]["name"] == "mysql_version"
        mock_request.assert_awaited_once_with(
            expected_method, expected_path, **expected_kwargs
        )

    @pytest.mark.asyncio
    async def test_get_advisor_checks_returns_empty_list_when_no_checks(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_advisor_checks returns empty list when response has no checks key."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {}

        result = await pmm_remote_api.get_advisor_checks()

        assert result == []


class TestGetFailedAdvisorChecks:
    """Test the get_failed_advisor_checks method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_method", "expected_path", "expected_kwargs"),
        [
            (
                "2.44.1",
                "POST",
                "/v1/management/SecurityChecks/FailedChecks",
                {"json": {}},
            ),
            (
                "3.6.0",
                "GET",
                "/v1/advisors/checks/failed",
                {},
            ),
        ],
    )
    async def test_get_failed_advisor_checks_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_method: str,
        expected_path: str,
        expected_kwargs: dict,
    ) -> None:
        """Test get_failed_advisor_checks uses the correct v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "results": [
                {
                    "check_name": "mysql_version",
                    "summary": "Outdated",
                    "severity": "SEVERITY_WARNING",
                    "labels": {"node_id": "n1", "service_id": "s1"},
                },
            ]
        }

        result = await pmm_remote_api.get_failed_advisor_checks()

        assert len(result) == 1
        assert result[0]["check_name"] == "mysql_version"
        mock_request.assert_awaited_once_with(
            expected_method, expected_path, **expected_kwargs
        )

    @pytest.mark.asyncio
    async def test_get_failed_advisor_checks_returns_empty_list_when_no_results(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_failed_advisor_checks returns empty list when no results key."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {}

        result = await pmm_remote_api.get_failed_advisor_checks()

        assert result == []


class TestStartAdvisorChecks:
    """Test the start_advisor_checks method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_path"),
        [
            ("2.44.1", "/v1/management/SecurityChecks/Start"),
            ("3.6.0", "/v1/advisors/checks:start"),
        ],
    )
    async def test_start_advisor_checks_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_path: str,
    ) -> None:
        """Test start_advisor_checks uses the correct v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {}

        await pmm_remote_api.start_advisor_checks()

        mock_request.assert_awaited_once_with("POST", expected_path, json={})

    @pytest.mark.asyncio
    async def test_start_advisor_checks_passes_names_when_provided(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test start_advisor_checks includes names in the payload."""
        mock_get_version.return_value = "3.0.0"
        mock_request.return_value = {}

        await pmm_remote_api.start_advisor_checks(names=["mysql_version"])

        mock_request.assert_awaited_once_with(
            "POST",
            "/v1/advisors/checks:start",
            json={"names": ["mysql_version"]},
        )

    @pytest.mark.asyncio
    async def test_start_advisor_checks_sends_empty_payload_when_names_is_none(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test start_advisor_checks sends empty payload when names is None."""
        mock_get_version.return_value = "3.6.0"
        mock_request.return_value = {}

        await pmm_remote_api.start_advisor_checks(names=None)

        call_json = mock_request.call_args.kwargs["json"]
        assert call_json == {}

    @pytest.mark.asyncio
    async def test_start_advisor_checks_propagates_http_exception_on_error(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test start_advisor_checks propagates an ``HTTPException`` from the API."""
        error_status = HTTP_503_SERVICE_UNAVAILABLE
        mock_get_version.return_value = "3.6.0"
        mock_request.side_effect = HTTPException(status_code=error_status)

        with pytest.raises(HTTPException) as exc_info:
            await pmm_remote_api.start_advisor_checks()

        assert exc_info.value.status_code == error_status


class TestGetGrafanaAnnotations:
    """Test the get_grafana_annotations method."""

    @pytest.mark.asyncio
    async def test_get_grafana_annotations_returns_all_annotations(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_grafana_annotations returns annotations from a single page."""
        annotations = [
            {"id": 1, "text": "alert1", "newState": "Alerting"},
            {"id": 2, "text": "alert2", "newState": "Alerting"},
        ]
        mock_request.return_value = annotations

        result = await pmm_remote_api.get_grafana_annotations(from_ts=1000, to_ts=2000)

        assert result == annotations
        mock_request.assert_awaited_once_with(
            "GET",
            "/graph/api/annotations",
            params={"type": "alert", "limit": 100, "from": 1000, "to": 2000},
        )

    @pytest.mark.asyncio
    async def test_get_grafana_annotations_returns_empty_list_on_no_data(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_grafana_annotations returns empty list when API returns nothing."""
        mock_request.return_value = []

        result = await pmm_remote_api.get_grafana_annotations()

        assert result == []

    @pytest.mark.asyncio
    async def test_get_grafana_annotations_paginates_until_no_new_items(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_grafana_annotations increases the limit when more data exists."""
        page1 = [{"id": i} for i in range(100)]
        page2 = [{"id": i} for i in range(150)]

        mock_request.side_effect = [page1, page2]

        result = await pmm_remote_api.get_grafana_annotations(limit=100)

        expected_page_count = 2
        expected_first_limit = 100
        expected_second_limit = 200

        assert result == page2
        assert mock_request.await_count == expected_page_count
        first_call_params = mock_request.call_args_list[0].kwargs["params"]
        second_call_params = mock_request.call_args_list[1].kwargs["params"]
        assert first_call_params["limit"] == expected_first_limit
        assert second_call_params["limit"] == expected_second_limit

    @pytest.mark.asyncio
    async def test_get_grafana_annotations_stops_when_last_item_unchanged(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test pagination stops when the last item is the same as the previous page."""
        same_page = [{"id": i} for i in range(100)]
        mock_request.side_effect = [same_page, same_page]

        result = await pmm_remote_api.get_grafana_annotations(limit=100)

        expected_call_count = 2

        assert result == same_page
        assert mock_request.await_count == expected_call_count

    @pytest.mark.asyncio
    async def test_get_grafana_annotations_omits_none_timestamps(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test that None from_ts/to_ts are not included in the query params."""
        mock_request.return_value = []

        await pmm_remote_api.get_grafana_annotations(from_ts=None, to_ts=None)

        params = mock_request.call_args.kwargs["params"]
        assert "from" not in params
        assert "to" not in params


class TestQueryGrafanaDatasource:
    """Test the query_grafana_datasource method."""

    @pytest.mark.asyncio
    async def test_query_grafana_datasource_builds_correct_payload(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test query_grafana_datasource constructs the expected POST payload."""
        mock_request.return_value = {
            "results": {"A": {"frames": []}, "B": {"frames": []}}
        }

        ds_id = 42
        max_dp = 500
        expected_query_count = 2

        result = await pmm_remote_api.query_grafana_datasource(
            expressions=["expr_a", "expr_b"],
            datasource_uid="ds-uid",
            datasource_id=ds_id,
            from_ts=1000,
            to_ts=2000,
            max_data_points=max_dp,
            instant=True,
            range_=False,
        )

        assert result == {"A": {"frames": []}, "B": {"frames": []}}
        mock_request.assert_awaited_once()
        call_kwargs = mock_request.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["from"] == "1000"
        assert payload["to"] == "2000"
        assert len(payload["queries"]) == expected_query_count
        assert payload["queries"][0]["refId"] == "A"
        assert payload["queries"][0]["expr"] == "expr_a"
        assert payload["queries"][0]["datasource"] == {"uid": "ds-uid"}
        assert payload["queries"][0]["datasourceId"] == ds_id
        assert payload["queries"][0]["maxDataPoints"] == max_dp
        assert payload["queries"][0]["instant"] is True
        assert payload["queries"][0]["range"] is False
        assert payload["queries"][1]["refId"] == "B"
        assert payload["queries"][1]["expr"] == "expr_b"

    @pytest.mark.asyncio
    async def test_query_grafana_datasource_returns_empty_dict_when_no_results(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test query_grafana_datasource returns empty dict when results key is absent."""
        mock_request.return_value = {}

        result = await pmm_remote_api.query_grafana_datasource(
            expressions=["expr"],
            datasource_uid="uid",
            datasource_id=1,
            from_ts="now-7d",
            to_ts="now",
        )

        assert result == {}

    @pytest.mark.asyncio
    async def test_query_grafana_datasource_single_expression(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test query_grafana_datasource with a single expression uses refId A."""
        mock_request.return_value = {"results": {"A": {"frames": [{"data": {}}]}}}

        result = await pmm_remote_api.query_grafana_datasource(
            expressions=["up"],
            datasource_uid="uid",
            datasource_id=1,
            from_ts=0,
            to_ts=1000,
        )

        payload = mock_request.call_args.kwargs["json"]
        assert len(payload["queries"]) == 1
        assert payload["queries"][0]["refId"] == "A"
        assert "A" in result


class TestGetGrafanaDatasources:
    """Test the get_grafana_datasources method."""

    @pytest.mark.asyncio
    async def test_get_grafana_datasources_returns_datasource_list(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_grafana_datasources fetches and returns all datasources."""
        datasources = [
            {"id": 1, "uid": "ds-1", "name": "Metrics", "type": "prometheus"},
            {"id": 2, "uid": "ds-2", "name": "Logs", "type": "loki"},
        ]
        mock_request.return_value = datasources

        result = await pmm_remote_api.get_grafana_datasources()

        assert result == datasources
        mock_request.assert_awaited_once_with("GET", "/graph/api/datasources")

    @pytest.mark.asyncio
    async def test_get_grafana_datasources_returns_empty_list(
        self,
        mock_request: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_grafana_datasources returns empty list when no datasources exist."""
        mock_request.return_value = []

        result = await pmm_remote_api.get_grafana_datasources()

        assert result == []


class TestGetInventoryServicesWithAgents:
    """Test the get_inventory_services_with_agents method."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pmm_version", "expected_method", "expected_path", "expected_kwargs"),
        [
            (
                "2.44.1",
                "POST",
                "/v1/management/Service/List",
                {"json": {}},
            ),
            (
                "3.6.0",
                "GET",
                "/v1/management/services",
                {},
            ),
        ],
    )
    async def test_get_inventory_services_with_agents_calls_correct_endpoint(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
        pmm_version: str,
        expected_method: str,
        expected_path: str,
        expected_kwargs: dict,
    ) -> None:
        """Test get_inventory_services_with_agents uses the correct v2/v3 endpoint."""
        mock_get_version.return_value = pmm_version
        mock_request.return_value = {
            "services": [
                {
                    "service_name": "mysql-1",
                    "service_type": "mysql",
                    "node_name": "node-1",
                    "agents": [{"agent_type": "mysqld_exporter", "status": "RUNNING"}],
                },
            ]
        }

        result = await pmm_remote_api.get_inventory_services_with_agents()

        assert len(result) == 1
        assert result[0]["service_name"] == "mysql-1"
        assert result[0]["agents"][0]["status"] == "RUNNING"
        mock_request.assert_awaited_once_with(
            expected_method, expected_path, **expected_kwargs
        )

    @pytest.mark.asyncio
    async def test_get_inventory_services_with_agents_returns_empty_list(
        self,
        mock_request: AsyncMock,
        mock_get_version: AsyncMock,
        pmm_remote_api: PMMRemoteAPI,
    ) -> None:
        """Test get_inventory_services_with_agents returns empty list when no services."""
        mock_get_version.return_value = "3.6.0"
        mock_request.return_value = {}

        result = await pmm_remote_api.get_inventory_services_with_agents()

        assert result == []
