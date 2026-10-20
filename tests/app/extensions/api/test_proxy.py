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

"""Tests for the shared PMM Extensions→Tasks gateway proxy helpers in ``app/extensions/api/proxy.py``."""

import pytest
from fastapi import HTTPException, status

from app.core.exceptions import HTTPBadGatewayException
from app.core.requests.remote_api import exception_for_status
from app.extensions.api.proxy import reraise_upstream_tasks_errors


class TestReraiseUpstreamTasksErrors:
    """Exercise the ``reraise_upstream_tasks_errors`` context manager wrapper."""

    def test_clean_block_does_not_raise(self) -> None:
        """Leave a block that completes normally untouched."""
        with reraise_upstream_tasks_errors():
            result = "ok"
        assert result == "ok"

    @pytest.mark.parametrize(
        "upstream_status",
        [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ],
    )
    def test_client_error_reraised_unchanged(self, upstream_status: int) -> None:
        """Re-raise an upstream client error (< 500) with its status and detail intact."""
        with (
            pytest.raises(HTTPException) as exc_info,
            reraise_upstream_tasks_errors(),
        ):
            raise HTTPException(status_code=upstream_status, detail="upstream detail")
        assert exc_info.value.status_code == upstream_status
        assert exc_info.value.detail == "upstream detail"
        assert not isinstance(exc_info.value, HTTPBadGatewayException)

    @pytest.mark.parametrize(
        "upstream_status",
        [status.HTTP_500_INTERNAL_SERVER_ERROR, status.HTTP_503_SERVICE_UNAVAILABLE],
    )
    def test_server_error_becomes_502(self, upstream_status: int) -> None:
        """Map an upstream server error (>= 500) onto a 502 gateway failure."""
        with (
            pytest.raises(HTTPBadGatewayException) as exc_info,
            reraise_upstream_tasks_errors(),
        ):
            raise HTTPException(status_code=upstream_status, detail="tasks down")
        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
        assert exc_info.value.detail == "tasks down"

    @pytest.mark.parametrize(
        "detail",
        [{"message": "tasks down"}, ["tasks", "down"], 503],
        ids=["dict", "list", "number"],
    )
    def test_server_error_structured_detail_reaches_gateway_as_text(
        self, detail: object
    ) -> None:
        """Carry a structured upstream detail onto the 502 as its string form."""
        with (
            pytest.raises(HTTPBadGatewayException) as exc_info,
            reraise_upstream_tasks_errors(),
        ):
            raise exception_for_status(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
            )
        assert exc_info.value.detail == str(detail)

    def test_server_error_empty_detail_falls_back_to_exception_text(self) -> None:
        """Describe the upstream exception itself when its detail is empty."""
        upstream = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=""
        )
        with (
            pytest.raises(HTTPBadGatewayException) as exc_info,
            reraise_upstream_tasks_errors(),
        ):
            raise upstream
        assert exc_info.value.detail == str(upstream)

    def test_oserror_becomes_502(self) -> None:
        """Map a connection-level ``OSError`` onto a 502 gateway failure."""
        with (
            pytest.raises(HTTPBadGatewayException) as exc_info,
            reraise_upstream_tasks_errors(),
        ):
            raise OSError("connection refused")
        assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
        assert exc_info.value.detail == "connection refused"

    def test_unrelated_exception_propagates(self) -> None:
        """Let a non-target exception propagate unchanged, never swallowing it."""
        with (
            pytest.raises(ValueError, match="boom"),
            reraise_upstream_tasks_errors(),
        ):
            raise ValueError("boom")
