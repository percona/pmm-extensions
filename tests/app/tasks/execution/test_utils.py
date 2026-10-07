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

"""Define tests for the app.tasks.execution.utils module."""

import gzip
import json
from unittest.mock import patch

import pytest

from app.tasks.execution.utils import gzip_compress, minify_file_content, parse_payload


def test_minify_valid_python_code():
    """Test minification of valid Python code."""
    python_code = """
def foo():
    x = 1
    y = 2
    return x + y
"""
    minified_code = minify_file_content(python_code, file_ext="py")
    assert minified_code.strip() != ""
    assert "\n" not in minified_code or len(minified_code.split("\n")) == 1
    assert "def" in minified_code
    assert "return" in minified_code


def test_minify_unrelated_file_extension():
    """Test minification with a non-Python file extension."""
    text_content = "This is some plain text."
    result = minify_file_content(text_content, file_ext="txt")
    assert result == text_content


def test_minify_with_syntax_error():
    """Return the content unchanged when the source has a syntax error."""
    broken_python_code = "def baz()\n    print('missing colon')"
    result = minify_file_content(broken_python_code, file_ext="py")
    assert result == broken_python_code


class TestParsePayload:
    """Test the parse_payload function."""

    def test_parse_valid_json(self):
        """Assert valid JSON string is parsed to a dict."""
        payload = '{"key": "value", "number": 42}'
        result = parse_payload(payload, "json")
        assert result == {"key": "value", "number": 42}

    def test_parse_valid_yaml(self):
        """Assert valid YAML string is parsed to a dict."""
        payload = "key: value\nnumber: 42"
        result = parse_payload(payload, "yaml")
        assert result == {"key": "value", "number": 42}

    def test_unsupported_format_raises_value_error(self):
        """Assert unsupported format raises ValueError."""
        with pytest.raises(ValueError, match="unsupported format: hcl"):
            parse_payload("{}", "hcl")

    def test_invalid_json_raises_json_decode_error(self):
        """Assert invalid JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            parse_payload("{invalid json", "json")

    def test_yaml_with_invalid_content_returns_string(self):
        """Assert YAML parser returns a string for non-mapping content."""
        result = parse_payload("just a plain string", "yaml")
        assert result == "just a plain string"

    def test_parse_json_nested_structure(self):
        """Assert nested JSON structures are parsed correctly."""
        payload = '{"outer": {"inner": [1, 2, 3]}}'
        result = parse_payload(payload, "json")
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_parse_yaml_nested_structure(self):
        """Assert nested YAML structures are parsed correctly."""
        payload = "outer:\n  inner:\n    - 1\n    - 2\n    - 3"
        result = parse_payload(payload, "yaml")
        assert result == {"outer": {"inner": [1, 2, 3]}}


class TestGzipCompress:
    """Test the gzip_compress function."""

    def test_compress_and_decompress(self):
        """Assert compressed data can be decompressed back to the original string."""
        data = "hello world"
        compressed = gzip_compress(data)
        decompressed = gzip.decompress(compressed).decode("utf-8")
        assert decompressed == data

    def test_compress_returns_bytes(self):
        """Assert gzip_compress returns bytes."""
        result = gzip_compress("test")
        assert isinstance(result, bytes)

    def test_compress_empty_string(self):
        """Assert empty string produces valid gzip bytes."""
        compressed = gzip_compress("")
        decompressed = gzip.decompress(compressed).decode("utf-8")
        assert decompressed == ""

    def test_compress_with_custom_encoding(self):
        """Assert custom encoding is respected during compression."""
        data = "hello"
        compressed = gzip_compress(data, encoding="ascii")
        decompressed = gzip.decompress(compressed).decode("ascii")
        assert decompressed == data

    def test_compress_is_independent_of_wall_clock(self):
        """Assert the output is byte-identical across a change in wall-clock time."""
        with patch("gzip.time.time", side_effect=[1_000_000.0, 1_000_001.0]):
            first = gzip_compress("SELECT 1;")
            second = gzip_compress("SELECT 1;")
        assert first == second

    def test_compress_unicode_content(self):
        """Assert unicode content is compressed and decompressed correctly."""
        data = "hola mundo"
        compressed = gzip_compress(data)
        decompressed = gzip.decompress(compressed).decode("utf-8")
        assert decompressed == data
