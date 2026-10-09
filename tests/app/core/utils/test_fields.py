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

"""Define tests for the app.core.utils.fields module."""

import re

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.utils.fields import (
    AuthSchemeStr,
    bounded_int_from_empty_str_factory,
    dsn_safe,
    NON_WHITESPACE_PATTERN,
    StrippedNonEmptyStr,
    TCP_PORT_MAX,
    TCP_PORT_MIN,
    TcpPort,
    URIPathPrefix,
    URL,
)
from app.core.utils.imports import (
    validate_attribute_is_importable,
    validate_module_is_importable,
)
from app.core.utils.path import resolve_relative_path


def test_validate_attribute_is_importable_invalid_format():
    """Test that an invalid format raises a ValueError."""
    input_str = "invalidformat"
    with pytest.raises(
        ValueError, match=re.escape("Must follow the format module.attribute")
    ):
        validate_attribute_is_importable(input_str)


def test_validate_module_is_importable_invalid():
    """Test that an invalid module path raises a ValueError."""
    invalid_module = "nonexistent_module"
    with pytest.raises(ValueError, match=f"No module named {invalid_module}"):
        validate_module_is_importable(invalid_module)


def test_resolve_relative_path_invalid_type():
    """Raise a ValueError when an invalid type is provided for the path."""
    invalid_path = None
    with pytest.raises(ValueError, match="Unable to resolve path: None"):
        resolve_relative_path(invalid_path)


class MockHandler:
    """Mock handler to simulate Pydantic core schema handler."""

    def __call__(self, core_schema):
        """Return a mock Pydantic core schema for testing."""
        return {"type": "string"}


def test_get_pydantic_json_schema():
    """Test that the __get_pydantic_json_schema__ method returns the correct schema."""
    mock_core_schema = {"type": "string"}
    mock_handler = MockHandler()

    expected_schema = {
        "type": "string",
        "format": "uri",
    }

    result = URL.__get_pydantic_json_schema__(mock_core_schema, mock_handler)

    assert result == expected_schema


class TestTcpPort:
    """Cover the ``TcpPort`` constrained TCP-port field type."""

    @pytest.mark.parametrize("port", [TCP_PORT_MIN, 443, TCP_PORT_MAX])
    def test_accepts_ports_in_range(self, port: int) -> None:
        """Accept ports within the inclusive 1-65535 range."""
        assert TypeAdapter(TcpPort).validate_python(port) == port

    @pytest.mark.parametrize("port", [0, TCP_PORT_MAX + 1, -1])
    def test_rejects_ports_out_of_range(self, port: int) -> None:
        """Reject ports outside the 1-65535 range."""
        with pytest.raises(ValidationError):
            TypeAdapter(TcpPort).validate_python(port)


class TestAuthSchemeStr:
    """Cover the ``AuthSchemeStr`` RFC 7230 ``token`` field type."""

    @pytest.mark.parametrize(
        "scheme", ["Bearer", "Basic", "Token", "X-Custom.v1", "a!#$%&'*+^_`|~-"]
    )
    def test_accepts_token_schemes(self, scheme: str) -> None:
        """Accept every scheme shape the ``token`` production allows."""
        assert TypeAdapter(AuthSchemeStr).validate_python(scheme) == scheme

    @pytest.mark.parametrize(
        "scheme",
        ["", " ", "Bea rer", "Bearer\r\nX-Injected: yes", "Bearer\n", "Bearer\x00"],
    )
    def test_rejects_values_no_header_can_carry(self, scheme: str) -> None:
        """Reject whitespace and control bytes, which no header value can carry."""
        with pytest.raises(ValidationError):
            TypeAdapter(AuthSchemeStr).validate_python(scheme)


class TestDsnSafe:
    """Cover the shared ``dsn_safe`` delimiter guard for free-typed names."""

    @pytest.mark.parametrize("value", ["mydb", "host.example.com", "schema.table"])
    def test_accepts_safe_names(self, value: str) -> None:
        """Return names that contain no DSN delimiters unchanged."""
        assert dsn_safe(value) == value

    @pytest.mark.parametrize("value", ["bad,name", "key=value", "a,b=c"])
    def test_rejects_delimiters(self, value: str) -> None:
        """Reject free-typed names containing ``,`` or ``=``."""
        with pytest.raises(ValueError, match="DSN delimiters"):
            dsn_safe(value)


class TestBoundedIntFromEmptyStrFactory:
    """Cover the bounded optional-int-from-empty-string field factory."""

    def test_blank_coerces_to_none(self) -> None:
        """Coerce an empty string to ``None``."""
        adapter = TypeAdapter(bounded_int_from_empty_str_factory(0, 3))
        assert adapter.validate_python("") is None

    def test_numeric_string_coerces_to_int(self) -> None:
        """Coerce a numeric form string to the equivalent int."""
        adapter = TypeAdapter(bounded_int_from_empty_str_factory(0, 3))
        level = 2
        assert adapter.validate_python(str(level)) == level

    @pytest.mark.parametrize("value", [0, 3])
    def test_boundary_values_accepted(self, value: int) -> None:
        """Accept the inclusive ends of the bounded range."""
        adapter = TypeAdapter(bounded_int_from_empty_str_factory(0, 3))
        assert adapter.validate_python(value) == value

    @pytest.mark.parametrize("value", [4, -1, "abc"])
    def test_out_of_range_or_garbage_rejected(self, value: int | str) -> None:
        """Reject values outside the range or non-numeric input."""
        adapter = TypeAdapter(bounded_int_from_empty_str_factory(0, 3))
        with pytest.raises(ValidationError):
            adapter.validate_python(value)

    def test_lower_bound_only_factory(self) -> None:
        """Coerce blanks and enforce only the lower bound when no upper bound is set."""
        adapter = TypeAdapter(bounded_int_from_empty_str_factory(0))
        large = 1_000_000
        assert adapter.validate_python("") is None
        assert adapter.validate_python(0) == 0
        assert adapter.validate_python(large) == large
        with pytest.raises(ValidationError):
            adapter.validate_python(-1)


class TestUriPathPrefix:
    """Cover the ``URIPathPrefix`` URL mount-prefix field type."""

    @pytest.mark.parametrize("value", ["", "/extensions", "/a/b"])
    def test_accepts_the_unprefixed_default_and_mount_prefixes(
        self, value: str
    ) -> None:
        """Accept the empty default and one or more ``/``-prefixed segments."""
        assert TypeAdapter(URIPathPrefix).validate_python(value) == value

    @pytest.mark.parametrize(
        "value",
        ["/extensions/", "extensions", "/a b", "/extensions?x", "/extensions#x"],
    )
    def test_rejects_values_that_would_not_concatenate_cleanly(
        self, value: str
    ) -> None:
        """Reject a trailing slash, a relative value, whitespace, and query or fragment."""
        with pytest.raises(ValidationError):
            TypeAdapter(URIPathPrefix).validate_python(value)


class TestStrippedNonEmptyStr:
    """Cover the ``StrippedNonEmptyStr`` trimmed non-blank string field type."""

    @pytest.mark.parametrize("value", ["", " ", "\t", "\n", "   \t\n "])
    def test_rejects_whitespace_only_values(self, value: str) -> None:
        """Reject a blank or whitespace-only value as a length violation.

        The published pattern rejects such a value independently, so pin the error
        type too: a client reading the error code has to keep seeing the length
        violation it always saw, not a pattern mismatch.
        """
        with pytest.raises(ValidationError) as excinfo:
            TypeAdapter(StrippedNonEmptyStr).validate_python(value)

        assert [error["type"] for error in excinfo.value.errors()] == [
            "string_too_short"
        ]

    @pytest.mark.parametrize(
        "value", ["/var/my backups", "a b", "schema name with spaces"]
    )
    def test_accepts_interior_whitespace(self, value: str) -> None:
        """Accept interior whitespace when the edges carry non-whitespace."""
        assert TypeAdapter(StrippedNonEmptyStr).validate_python(value) == value

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("  /var/backups  ", "/var/backups"),
            ("\t/var/my backups\n", "/var/my backups"),
        ],
    )
    def test_strips_surrounding_whitespace(self, value: str, expected: str) -> None:
        """Strip leading and trailing whitespace before the constraints apply."""
        assert TypeAdapter(StrippedNonEmptyStr).validate_python(value) == expected

    def test_publishes_the_non_whitespace_pattern(self) -> None:
        """Publish the non-whitespace constraint the validator already enforces."""
        schema = TypeAdapter(StrippedNonEmptyStr).json_schema()
        assert schema["pattern"] == NON_WHITESPACE_PATTERN
        assert schema["minLength"] == 1
