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
from unittest.mock import patch

from app.tasks.execution.utils import gzip_compress, minify_file_content


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
