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

"""Define tests for the app.core.utils.exceptions module."""

from typing import Any

import pytest
from aiohttp import ClientError
from fastapi import HTTPException, status

from app.core.exceptions import HTTPConflictException
from app.core.requests.remote_api import exception_for_status
from app.core.utils.exceptions import exception_detail


class _DetailNoneError(Exception):
    """Carry a ``detail`` attribute explicitly set to ``None``."""

    detail = None


class TestExceptionDetail:
    """Cover :func:`exception_detail`."""

    def test_returns_string_detail_unchanged(self) -> None:
        """Return a truthy string detail as-is."""
        assert (
            exception_detail(
                HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="boom")
            )
            == "boom"
        )

    def test_preserves_non_ascii_detail(self) -> None:
        """Pass a non-ASCII string detail through untouched."""
        exc = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="falha: conexão"
        )
        assert exception_detail(exc) == "falha: conexão"

    @pytest.mark.parametrize(
        "detail",
        [
            {"message": "upstream failed"},
            ["first", "second"],
            500,
            1.5,
            {"errors": [{"code": 7, "message": "nested"}]},
        ],
        ids=["dict", "list", "int", "float", "nested"],
    )
    def test_coerces_non_string_detail(self, detail: Any) -> None:
        """Return a non-string upstream detail as its string form."""
        result = exception_detail(
            HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
        )
        assert isinstance(result, str)
        assert result == str(detail)

    @pytest.mark.parametrize(
        "detail", ["", {}, [], 0], ids=["empty-str", "empty-dict", "empty-list", "zero"]
    )
    def test_falls_back_to_str_exc_on_falsy_detail(self, detail: Any) -> None:
        """Describe the exception itself when its detail is falsy."""
        exc = HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
        assert exception_detail(exc) == str(exc)
        assert str(status.HTTP_502_BAD_GATEWAY) in exception_detail(exc)

    def test_falls_back_when_detail_is_none(self) -> None:
        """Describe the exception itself when its detail is ``None``."""
        assert exception_detail(_DetailNoneError("no detail")) == "no detail"

    def test_falls_back_when_attribute_missing(self) -> None:
        """Describe an exception without a ``detail`` attribute by its string form."""
        assert exception_detail(OSError("connection reset")) == "connection reset"

    def test_describes_aiohttp_client_error(self) -> None:
        """Describe an ``aiohttp.ClientError`` by its string form."""
        assert exception_detail(ClientError("boom")) == "boom"

    def test_returns_empty_string_for_bare_exception(self) -> None:
        """Return an empty string, not ``None``, for an exception with no message."""
        assert exception_detail(OSError()) == ""

    def test_describes_mapped_project_exception(self) -> None:
        """Coerce the structured detail of an exception ``RemoteAPI`` would raise."""
        exc = exception_for_status(
            status.HTTP_409_CONFLICT, detail={"message": "rule exists"}
        )
        assert isinstance(exc, HTTPConflictException)
        result = exception_detail(exc)
        assert isinstance(result, str)
        assert "rule exists" in result
