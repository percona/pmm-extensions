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

"""Define tests for the app.core.security module."""

from pydantic import SecretStr

from app.core.config import settings
from app.core.security import (
    crypto_serializer,
    crypto_timestamp_serializer,
    get_internal_token,
    is_bearer_authenticated,
)
from tests.app.conftest import make_request


def test_crypto_serializer_basic():
    """Test basic serialization and deserialization."""
    payload = {"user_id": 123, "email": "test@example.com"}
    token = crypto_serializer.dumps(payload)
    decoded_payload = crypto_serializer.loads(token)
    assert decoded_payload == payload


def test_crypto_timestamp_serializer_basic():
    """Test basic serialization and deserialization with timestamp."""
    payload = {"user_id": 456, "email": "time@example.com"}
    token = crypto_timestamp_serializer.dumps(payload)
    decoded_payload = crypto_timestamp_serializer.loads(token)
    assert decoded_payload == payload


def test_get_internal_token_returns_secret(mocker):
    """``get_internal_token`` returns the configured token's secret value."""
    mocker.patch.object(
        settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr("internal-secret")
    )
    assert get_internal_token() == "internal-secret"


class TestBearerHeaderEdgeCases:
    """Cover header-parsing edges for ``is_bearer_authenticated``.

    These tests pin the *current* permissive-prefix contract: the predicate is a
    routing signal, not a credential check. Any future tightening (token shape,
    DoS bounds) should fail these tests visibly so it can't slip in silently.
    """

    def test_tab_separator_does_not_match(self) -> None:
        r"""``Bearer\ttoken`` does not match; the prefix requires a literal space."""
        request = make_request(authorization="Bearer\ttoken")
        assert is_bearer_authenticated(request) is False

    def test_double_space_after_bearer_matches(self) -> None:
        """``Bearer  token`` (two spaces) still satisfies the prefix check.

        Documents the lenient prefix contract: anything after ``Bearer `` is the
        token payload — downstream validators (``oauth2_scheme``,
        ``get_current_user_api``) are responsible for token shape.
        """
        request = make_request(authorization="Bearer  token")
        assert is_bearer_authenticated(request) is True

    def test_leading_whitespace_in_header_does_not_match(self) -> None:
        """`` Bearer token`` (leading space) is not a Bearer credential.

        Starlette does not strip leading whitespace from header values; the
        predicate's ``startswith`` check is byte-faithful, so a leading space
        rejects.
        """
        request = make_request(authorization=" Bearer token")
        assert is_bearer_authenticated(request) is False

    def test_mixed_case_scheme_matches(self) -> None:
        """Match the scheme case-insensitively (``BeArEr token`` is valid)."""
        request = make_request(authorization="BeArEr token")
        assert is_bearer_authenticated(request) is True

    def test_very_long_header_does_not_crash(self) -> None:
        """Parse a 64 KiB Authorization header without raising or hanging.

        DoS sanity check: the predicate is a single ``str.startswith`` — adding
        token length validation later would need a different shape, so a
        regression introducing a quadratic scan would fail here.
        """
        long_token = "a" * (64 * 1024)
        request = make_request(authorization=f"Bearer {long_token}")
        assert is_bearer_authenticated(request) is True

    def test_null_byte_in_token_passes_predicate(self) -> None:
        r"""``Bearer \x00abc`` passes; null-byte filtering is a downstream concern.

        Pinning permissive behaviour: the routing-signal predicate is
        intentionally thin, so any future "block control characters" change is
        visible here rather than a silent behaviour shift.
        """
        request = make_request(authorization="Bearer \x00abc")
        assert is_bearer_authenticated(request) is True

    def test_unicode_nbsp_separator_does_not_match(self) -> None:
        r"""``Bearer<NBSP>token`` does not match the ASCII prefix.

        Prevents a unicode-confusable bypass: a client crafting
        ``Bearer<NBSP>...`` cannot trick the predicate into accepting a request
        that Starlette will then route to the safe codepath.
        """
        request = make_request(authorization="Bearer\u00a0token")
        assert is_bearer_authenticated(request) is False

    def test_only_scheme_no_separator_does_not_match(self) -> None:
        """``Bearer`` alone (no trailing space) is not a Bearer credential."""
        request = make_request(authorization="Bearer")
        assert is_bearer_authenticated(request) is False
