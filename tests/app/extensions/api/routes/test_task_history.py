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

"""Tests for the PMM Extensions merged task-history JSON API at ``/api/extensions/task-history/``."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core.pagination import (
    DEFAULT_PAGINATION_LIMIT,
    DEFAULT_PAGINATION_OFFSET,
    MAX_PAGINATION_LIMIT,
)
from app.extensions.main import extensions_app
from app.tasks.models import TaskBackendEnum
from tests.app.factories import TaskFactory

TWO_MERGED_HISTORY_ROWS = 2
TWO_UNIQUE_TASK_NAMES = 2
PROPAGATED_TEST_OFFSET = 5
PROPAGATED_TEST_LIMIT = 10
UPSTREAM_PAGES_PER_TASK = 2
EXPECTED_PAGED_UPSTREAM_CALLS = TWO_UNIQUE_TASK_NAMES * UPSTREAM_PAGES_PER_TASK


def _history_item(*, item_id: int, started_at: str, task_name: str) -> dict[str, Any]:
    task = TaskFactory.build(
        id=item_id,
        name=task_name,
        owner="BACKUP_MONGO",
        backend=TaskBackendEnum.PROXY,
    )
    task_payload = task.model_dump(mode="json")
    task_payload["data"] = {
        "task": "run-python",
        "meta": {"target": "host1"},
        "payload": "file:///plugins/backup_mongo/pbm_config_payload",
    }
    return {
        "id": item_id,
        "status": "success",
        "started_at": started_at,
        "execution_request": {"task": task_name, "target": "host1"},
        "task": {**task_payload, "deleted_at": None},
    }


def _history_page(
    *, item_id: int, started_at: str, task_name: str, total: int = 1
) -> dict:
    return {
        "items": [
            _history_item(item_id=item_id, started_at=started_at, task_name=task_name)
        ],
        "total": total,
        "offset": 0,
        "limit": 50,
    }


class TestExtensionsTaskHistoryEndpoint:
    """Cover ``GET /api/extensions/task-history/`` proxy and merge behavior."""

    def test_merges_history_for_multiple_task_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Fetch each task upstream and return rows sorted newest-first."""
        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                _history_page(
                    item_id=1,
                    started_at="2026-01-01T10:00:00+00:00",
                    task_name="parent",
                ),
                _history_page(
                    item_id=2,
                    started_at="2026-01-02T10:00:00+00:00",
                    task_name="parent-logical",
                ),
            ]
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "parent"), ("task_names", "parent-logical")],
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["id"] for item in body["items"]] == [2, 1]
        assert body["total"] == TWO_MERGED_HISTORY_ROWS
        assert body["offset"] == DEFAULT_PAGINATION_OFFSET
        assert body["limit"] == DEFAULT_PAGINATION_LIMIT

    def test_fetches_widened_upstream_window_and_applies_global_pagination(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Fetch each task from offset 0 and paginate after merge."""
        mock_task_api_dep.get = AsyncMock(
            return_value={
                "items": [],
                "total": 0,
                "offset": 0,
                "limit": PROPAGATED_TEST_OFFSET + PROPAGATED_TEST_LIMIT,
            }
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[
                ("task_names", "task-a"),
                ("task_names", "task-b"),
                ("status", "running"),
                ("offset", str(PROPAGATED_TEST_OFFSET)),
                ("limit", str(PROPAGATED_TEST_LIMIT)),
            ],
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["offset"] == PROPAGATED_TEST_OFFSET
        assert body["limit"] == PROPAGATED_TEST_LIMIT
        assert mock_task_api_dep.get.await_count == TWO_UNIQUE_TASK_NAMES
        upstream_params = {
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": PROPAGATED_TEST_OFFSET + PROPAGATED_TEST_LIMIT,
            "status": "running",
        }
        mock_task_api_dep.get.assert_any_await(
            "/task-a/history/", params=upstream_params
        )
        mock_task_api_dep.get.assert_any_await(
            "/task-b/history/", params=upstream_params
        )

    def test_returns_page_two_when_upstream_per_task_offset_would_be_empty(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Page 2 still returns rows when per-task offset would drop every task."""
        page_two_offset = 30
        page_two_limit = 30
        merged_total = 120

        def _page_items(task_index: int) -> list[dict[str, Any]]:
            return [
                _history_item(
                    item_id=(task_index * 100) + index,
                    started_at=(
                        f"2026-06-{30 - index:02d}T{task_index:02d}:00:00+00:00"
                    ),
                    task_name=f"task-{task_index}",
                )
                for index in range(30)
            ]

        mock_task_api_dep.get = AsyncMock(
            side_effect=[
                {
                    "items": _page_items(1),
                    "total": 30,
                    "offset": 0,
                    "limit": page_two_offset + page_two_limit,
                },
                {
                    "items": _page_items(2),
                    "total": 30,
                    "offset": 0,
                    "limit": page_two_offset + page_two_limit,
                },
                {
                    "items": _page_items(3),
                    "total": 30,
                    "offset": 0,
                    "limit": page_two_offset + page_two_limit,
                },
                {
                    "items": _page_items(4),
                    "total": 30,
                    "offset": 0,
                    "limit": page_two_offset + page_two_limit,
                },
            ]
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[
                ("task_names", "task-1"),
                ("task_names", "task-2"),
                ("task_names", "task-3"),
                ("task_names", "task-4"),
                ("offset", str(page_two_offset)),
                ("limit", str(page_two_limit)),
            ],
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == merged_total
        assert len(body["items"]) == page_two_limit

    def test_failure_reason_survives_the_merge_path(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Assert an upstream failure_reason survives merge and re-validation."""
        page = _history_page(
            item_id=1, started_at="2026-01-01T10:00:00+00:00", task_name="parent"
        )
        page["items"][0]["failure_reason"] = "Step 'run-script' failed (exit code 1)."
        mock_task_api_dep.get = AsyncMock(return_value=page)

        response = test_client.get(
            "/api/extensions/task-history/", params=[("task_names", "parent")]
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["failure_reason"] == (
            "Step 'run-script' failed (exit code 1)."
        )

    def test_legacy_requirements_row_stays_readable(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Assert a stored row with an invalid requirements value is not a 500."""
        page = _history_page(
            item_id=1, started_at="2026-01-01T10:00:00+00:00", task_name="parent"
        )
        page["items"][0]["execution_request"]["meta"] = {"requirements": ["a", ""]}
        mock_task_api_dep.get = AsyncMock(return_value=page)

        response = test_client.get(
            "/api/extensions/task-history/", params=[("task_names", "parent")]
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["execution_request"]["meta"] == {
            "requirements": ["a", ""]
        }

    def test_deduplicates_repeated_task_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Query each unique task name only once."""
        mock_task_api_dep.get = AsyncMock(
            return_value={"items": [], "total": 0, "offset": 0, "limit": 50}
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[
                ("task_names", "task-a"),
                ("task_names", "task-a"),
                ("task_names", "task-b"),
            ],
        )
        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.get.await_count == TWO_UNIQUE_TASK_NAMES

    def test_rejects_empty_task_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return 422 when every supplied task name is blank."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", ""), ("task_names", "   ")],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_rejects_negative_offset(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return 422 when ``offset`` is negative."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a"), ("offset", "-1")],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_rejects_negative_limit(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return 422 when ``limit`` is negative."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a"), ("limit", "-1")],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_rejects_zero_limit(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return 422 when ``limit`` is zero."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a"), ("limit", "0")],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_rejects_limit_above_cap(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Return 422 when ``limit`` exceeds the upper cap of 200."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a"), ("limit", str(MAX_PAGINATION_LIMIT + 1))],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_pages_upstream_when_merged_window_exceeds_max_limit(
        self,
        test_client: TestClient,
        mock_task_api_dep,
    ) -> None:
        """Page each task upstream when offset + limit exceeds the Tasks API cap."""
        large_offset = 151
        large_limit = 50

        async def _upstream_page(
            url: str,
            *,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            assert params is not None
            assert params["limit"] <= MAX_PAGINATION_LIMIT
            offset = params["offset"]
            limit = params["limit"]
            name = url.removeprefix("/").removesuffix("/history/")
            if offset == 0:
                items = [
                    _history_item(
                        item_id=index,
                        started_at=f"2026-06-{(index % 28) + 1:02d}T10:00:00+00:00",
                        task_name=name,
                    )
                    for index in range(limit)
                ]
            else:
                items = [
                    _history_item(
                        item_id=offset,
                        started_at="2026-01-01T10:00:00+00:00",
                        task_name=name,
                    )
                ]
            return {
                "items": items,
                "total": 500,
                "offset": offset,
                "limit": limit,
            }

        mock_task_api_dep.get = AsyncMock(side_effect=_upstream_page)

        response = test_client.get(
            "/api/extensions/task-history/",
            params=[
                ("task_names", "task-a"),
                ("task_names", "task-b"),
                ("offset", str(large_offset)),
                ("limit", str(large_limit)),
            ],
        )
        assert response.status_code == status.HTTP_200_OK
        assert mock_task_api_dep.get.await_count == EXPECTED_PAGED_UPSTREAM_CALLS
        first_page_params = {
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": MAX_PAGINATION_LIMIT,
        }
        second_page_params = {
            "offset": MAX_PAGINATION_LIMIT,
            "limit": large_offset + large_limit - MAX_PAGINATION_LIMIT,
        }
        mock_task_api_dep.get.assert_any_await(
            "/task-a/history/", params=first_page_params
        )
        mock_task_api_dep.get.assert_any_await(
            "/task-a/history/", params=second_page_params
        )
        mock_task_api_dep.get.assert_any_await(
            "/task-b/history/", params=first_page_params
        )
        mock_task_api_dep.get.assert_any_await(
            "/task-b/history/", params=second_page_params
        )

    @pytest.mark.parametrize(
        "upstream_status",
        [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND],
    )
    def test_merged_passes_through_upstream_client_error(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        upstream_status: int,
    ) -> None:
        """Return an upstream client error (4xx) from the fan-out unchanged."""
        mock_task_api_dep.get = AsyncMock(
            side_effect=HTTPException(
                status_code=upstream_status, detail="upstream rejected"
            )
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a")],
        )
        assert response.status_code == upstream_status
        assert response.json() == {"detail": "upstream rejected"}

    def test_merged_upstream_5xx_becomes_502(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Fail the merged fan-out with ``502`` on an upstream server error."""
        mock_task_api_dep.get = AsyncMock(
            side_effect=HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom"
            )
        )
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a")],
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "boom"}

    def test_merged_upstream_oserror_becomes_502(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Fail the merged fan-out with ``502`` on a connection-level ``OSError``."""
        mock_task_api_dep.get = AsyncMock(side_effect=OSError("connection refused"))
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "task-a")],
        )
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "connection refused"}


class TestExtensionsTaskHistoryAuth:
    """Cover ``/api/extensions/task-history/`` authentication enforcement."""

    @pytest.fixture
    def unauthenticated_client(self) -> Iterator[TestClient]:
        """Yield a TestClient with no auth dependency overrides applied."""
        previous = extensions_app.dependency_overrides
        extensions_app.dependency_overrides = {}
        try:
            yield TestClient(extensions_app, raise_server_exceptions=False)
        finally:
            extensions_app.dependency_overrides = previous

    def test_unauthenticated_returns_json_401(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Reject anonymous requests with a JSON 401 response."""
        response = unauthenticated_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", "foo")],
            follow_redirects=False,
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()


class TestExtensionsTaskHistoryListAll:
    """Forward ``GET /api/extensions/task-history/`` without names to the upstream list."""

    def test_passthrough_when_task_names_omitted(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Return the upstream history list directly when ``task_names`` is omitted."""
        payload = {
            "items": [
                _history_item(
                    item_id=7,
                    started_at="2026-01-01T10:00:00+00:00",
                    task_name="run-x",
                )
            ],
            "total": 1,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }
        mock_task_api_dep.get.return_value = payload
        response = test_client.get("/api/extensions/task-history/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["id"] for item in body["items"]] == [7]
        assert body["total"] == 1
        mock_task_api_dep.get.assert_awaited_once_with(
            "/history/",
            params={
                "offset": DEFAULT_PAGINATION_OFFSET,
                "limit": DEFAULT_PAGINATION_LIMIT,
            },
        )

    def test_failure_reason_survives_the_passthrough_path(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Assert an upstream failure_reason survives the no-merge passthrough.

        Covered separately from the merge path: the two diverge, so a field
        reaching one says nothing about the other.
        """
        item = _history_item(
            item_id=7, started_at="2026-01-01T10:00:00+00:00", task_name="run-x"
        )
        item["failure_reason"] = "The run failed."
        mock_task_api_dep.get.return_value = {
            "items": [item],
            "total": 1,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["failure_reason"] == "The run failed."

    def test_upstream_without_failure_reason_defaults_to_null(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Assert an older tasks service emitting no such key still validates.

        The rolling-deploy direction: migration and code land apart, so PMM Extensions must
        read a payload that predates the field.
        """
        mock_task_api_dep.get.return_value = {
            "items": [
                _history_item(
                    item_id=7,
                    started_at="2026-01-01T10:00:00+00:00",
                    task_name="run-x",
                )
            ],
            "total": 1,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["failure_reason"] is None

    def test_forwards_status_and_pagination(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Forward the ``status`` filter and client pagination to the upstream list."""
        mock_task_api_dep.get.return_value = {
            "items": [],
            "total": 0,
            "offset": PROPAGATED_TEST_OFFSET,
            "limit": PROPAGATED_TEST_LIMIT,
        }
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[
                ("status", "running"),
                ("offset", str(PROPAGATED_TEST_OFFSET)),
                ("limit", str(PROPAGATED_TEST_LIMIT)),
            ],
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["offset"] == PROPAGATED_TEST_OFFSET
        assert body["limit"] == PROPAGATED_TEST_LIMIT
        mock_task_api_dep.get.assert_awaited_once_with(
            "/history/",
            params={
                "offset": PROPAGATED_TEST_OFFSET,
                "limit": PROPAGATED_TEST_LIMIT,
                "status": "running",
            },
        )

    def test_provided_but_blank_task_names_still_rejected(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Reject provided-but-all-blank ``task_names`` with 422, never list-all."""
        response = test_client.get(
            "/api/extensions/task-history/",
            params=[("task_names", ""), ("task_names", "   ")],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        mock_task_api_dep.get.assert_not_called()

    def test_forwards_exclude_internal_when_requested(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Forward ``exclude_internal=true`` to the upstream list when requested."""
        mock_task_api_dep.get.return_value = {
            "items": [],
            "total": 0,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }
        response = test_client.get(
            "/api/extensions/task-history/",
            params={"exclude_internal": "true"},
        )
        assert response.status_code == status.HTTP_200_OK
        call_params = mock_task_api_dep.get.call_args.kwargs["params"]
        assert call_params.get("exclude_internal") == "true"

    def test_exclude_internal_not_forwarded_by_default(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Omit ``exclude_internal`` from upstream params when the option is not requested."""
        mock_task_api_dep.get.return_value = {
            "items": [],
            "total": 0,
            "offset": DEFAULT_PAGINATION_OFFSET,
            "limit": DEFAULT_PAGINATION_LIMIT,
        }
        response = test_client.get("/api/extensions/task-history/")
        assert response.status_code == status.HTTP_200_OK
        call_params = mock_task_api_dep.get.call_args.kwargs["params"]
        assert "exclude_internal" not in call_params


class TestExtensionsStopTaskHistoryEndpoint:
    """Forward ``POST /api/extensions/task-history/{id}/stop/`` to the upstream stop."""

    def test_stop_proxies_and_returns_upstream_json(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Return the upstream stop JSON verbatim (not a redirect)."""
        upstream = {"id": 42, "status": "stopped"}
        mock_task_api_dep.post.return_value = upstream
        response = test_client.post("/api/extensions/task-history/42/stop/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == upstream
        mock_task_api_dep.post.assert_awaited_once_with("/history/42/stop/")

    @pytest.mark.parametrize(
        "upstream_status",
        [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND],
    )
    def test_stop_passes_through_upstream_client_error(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        upstream_status: int,
    ) -> None:
        """Return an upstream client error (400 "not running", 404) unchanged with detail."""
        mock_task_api_dep.post.side_effect = HTTPException(
            status_code=upstream_status, detail="task is not running"
        )
        response = test_client.post("/api/extensions/task-history/42/stop/")
        assert response.status_code == upstream_status
        assert response.json() == {"detail": "task is not running"}

    def test_stop_upstream_5xx_becomes_502(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Fail the proxy with ``502`` on an upstream server error."""
        mock_task_api_dep.post.side_effect = HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom"
        )
        response = test_client.post("/api/extensions/task-history/42/stop/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "boom"}

    def test_stop_upstream_oserror_becomes_502(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Fail the proxy with ``502`` on a connection-level ``OSError``."""
        mock_task_api_dep.post.side_effect = OSError("connection refused")
        response = test_client.post("/api/extensions/task-history/42/stop/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "connection refused"}

    def test_stop_cookie_only_unauthorized(
        self,
        api_admin_client_no_bearer: TestClient,
        mock_task_api_dep: AsyncMock,
    ) -> None:
        """Reject a cookie-only stop that lacks a Bearer token with 401."""
        response = api_admin_client_no_bearer.post(
            "/api/extensions/task-history/42/stop/"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        mock_task_api_dep.post.assert_not_awaited()


ACTOR_CREATOR_ID = "11111111-1111-4111-8111-111111111111"
ACTOR_UPDATER_ID = "22222222-2222-4222-8222-222222222222"
ACTOR_EXECUTOR_ID = "33333333-3333-4333-8333-333333333333"
ACTOR_UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"
ACTOR_USERNAME_MAP = {
    ACTOR_CREATOR_ID: "alice",
    ACTOR_UPDATER_ID: "bob",
    ACTOR_EXECUTOR_ID: "carol",
}


def _actor_history_page(*, executed_by: str | None = ACTOR_EXECUTOR_ID) -> dict:
    """Build a one-row upstream page whose three actor fields carry known ids."""
    page = _history_page(
        item_id=1, started_at="2026-01-01T10:00:00+00:00", task_name="backup"
    )
    item = page["items"][0]
    item["executed_by"] = executed_by
    item["task"]["created_by"] = ACTOR_CREATOR_ID
    item["task"]["last_updated_by"] = ACTOR_UPDATER_ID
    return page


class TestExtensionsTaskHistoryActorResolution:
    """Cover actor resolution on both read paths of ``GET /api/extensions/task-history/``."""

    @pytest.fixture
    def patched_username_map(self, mocker) -> Mock:
        """Patch the mapping at the symbol the route resolves, not its definition."""
        return mocker.patch(
            "app.extensions.api.routes.task_history.get_username_mapping",
            new=AsyncMock(return_value=ACTOR_USERNAME_MAP),
        )

    def test_passthrough_resolves_all_three_actor_fields(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        patched_username_map: Mock,
    ) -> None:
        """Resolve the executor and both nested task actors on the list-all path."""
        mock_task_api_dep.get = AsyncMock(return_value=_actor_history_page())

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        row = response.json()["items"][0]
        assert row["executed_by"] == "carol"
        assert row["task"]["created_by"] == "alice"
        assert row["task"]["last_updated_by"] == "bob"

    def test_merge_path_resolves_all_three_actor_fields(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        patched_username_map: Mock,
    ) -> None:
        """Resolve the same three fields on the ``task_names`` merge path."""
        mock_task_api_dep.get = AsyncMock(return_value=_actor_history_page())

        response = test_client.get(
            "/api/extensions/task-history/", params=[("task_names", "backup")]
        )

        assert response.status_code == status.HTTP_200_OK
        row = response.json()["items"][0]
        assert row["executed_by"] == "carol"
        assert row["task"]["created_by"] == "alice"
        assert row["task"]["last_updated_by"] == "bob"

    def test_passthrough_renders_a_system_row(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        patched_username_map: Mock,
    ) -> None:
        """Render the ``SYSTEM`` sentinel as its label on the list-all path."""
        mock_task_api_dep.get = AsyncMock(
            return_value=_actor_history_page(executed_by="SYSTEM")
        )

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["executed_by"] == "System"

    def test_merge_path_renders_a_system_row(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        patched_username_map: Mock,
    ) -> None:
        """Render the ``SYSTEM`` sentinel as its label on the merge path."""
        mock_task_api_dep.get = AsyncMock(
            return_value=_actor_history_page(executed_by="SYSTEM")
        )

        response = test_client.get(
            "/api/extensions/task-history/", params=[("task_names", "backup")]
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["executed_by"] == "System"

    def test_unresolvable_actor_degrades_to_the_raw_identifier(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        patched_username_map: Mock,
    ) -> None:
        """Serve the stored identifier when the provider cannot resolve it."""
        mock_task_api_dep.get = AsyncMock(
            return_value=_actor_history_page(executed_by=ACTOR_UNKNOWN_ID)
        )

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["executed_by"] == ACTOR_UNKNOWN_ID

    def test_provider_failure_degrades_the_whole_page(
        self,
        test_client: TestClient,
        mock_task_api_dep: AsyncMock,
        mocker,
    ) -> None:
        """Serve raw identifiers with ``200`` when the provider lookup returns none."""
        mocker.patch(
            "app.extensions.api.routes.task_history.get_username_mapping",
            new=AsyncMock(return_value={}),
        )
        mock_task_api_dep.get = AsyncMock(return_value=_actor_history_page())

        response = test_client.get("/api/extensions/task-history/")

        assert response.status_code == status.HTTP_200_OK
        row = response.json()["items"][0]
        assert row["executed_by"] == ACTOR_EXECUTOR_ID
        assert row["task"]["created_by"] == ACTOR_CREATOR_ID
