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

"""Define tests for the app.extensions.utils.jinja module."""

import pytest
from faker import Faker
from pygments.lexers import TextLexer
from pygments.util import ClassNotFound

from app.extensions.utils.jinja import (
    backup_date,
    convert_bytes,
    syntax_highlight,
    timestamp_format,
)


@pytest.fixture
def random_json(faker: Faker) -> str:
    """Generate a random JSON string."""
    return faker.json()


def test_syntax_highlight_guess_lexer(mocker, random_json):
    """Test that syntax_highlight calls guess_lexer when no language is provided."""
    guess_lexer_mock = mocker.patch(
        "app.extensions.utils.jinja.guess_lexer", return_value=TextLexer()
    )
    syntax_highlight(random_json)
    guess_lexer_mock.assert_called_once_with(random_json, stripall=True)


def test_syntax_highlight_with_valid_language(mocker, random_json):
    """Test that syntax_highlight calls get_lexer_by_name for a valid language."""
    get_lexer_mock = mocker.patch(
        "app.extensions.utils.jinja.get_lexer_by_name",
    )
    highlighted = syntax_highlight(random_json, language="python")
    get_lexer_mock.assert_called_once_with("python", stripall=True)
    assert "<div" in highlighted


def test_syntax_highlight_with_unknown_language(mocker, random_json):
    """Test that syntax_highlight falls back to TextLexer when an unknown language is used."""
    get_lexer_mock = mocker.patch(
        "app.extensions.utils.jinja.get_lexer_by_name",
        side_effect=ClassNotFound("Unknown language"),
    )
    highlight_mock = mocker.patch("app.extensions.utils.jinja.highlight")
    syntax_highlight(random_json, language="unknown_lang")
    get_lexer_mock.assert_called_once()
    highlight_mock.assert_called_once()
    assert isinstance(highlight_mock.call_args.args[1], TextLexer)


@pytest.mark.parametrize(
    "value",
    [30, 30.5, True, False, None],
    ids=["int", "float", "bool_true", "bool_false", "none"],
)
def test_syntax_highlight_coerces_non_string_input(value):
    """Test that syntax_highlight coerces non-string scalar input to ``str``."""
    result = syntax_highlight(value)
    assert isinstance(result, str)
    assert str(value) in result


def test_timestamp_format_zero():
    """Test that epoch 0 formats to the Unix epoch date."""
    assert timestamp_format(0) == "01 January 1970 at 00:00:00"


def test_timestamp_format_known_date():
    """Test a known epoch value produces the expected human-readable date."""
    assert timestamp_format(1_700_000_000) == "14 November 2023 at 22:13:20"


def test_timestamp_format_float():
    """Test that a float epoch value is handled correctly."""
    result = timestamp_format(1_700_000_000.5)
    assert "14 November 2023" in result


def test_timestamp_format_overflow_returns_str():
    """Test that an overflowing value falls back to str() instead of raising."""
    result = timestamp_format(10**18)
    assert isinstance(result, str)


def test_backup_date_epoch_ms():
    """Test that an integer epoch-ms value is converted to a formatted date."""
    result = backup_date(1_700_000_000_000)
    assert result == "14 November 2023 at 22:13:20 UTC"


def test_backup_date_zero():
    """Test that epoch-ms 0 formats to the Unix epoch date."""
    assert backup_date(0) == "01 January 1970 at 00:00:00 UTC"


def test_backup_date_string_passthrough():
    """Test that a pre-formatted string is returned as-is."""
    assert (
        backup_date("14 November 2023 at 22:13:20 UTC")
        == "14 November 2023 at 22:13:20 UTC"
    )


def test_backup_date_arbitrary_string():
    """Test that an arbitrary string is returned unchanged."""
    assert backup_date("N/A") == "N/A"


def test_backup_date_overflow_returns_str():
    """Test that an overflowing epoch-ms falls back to str()."""
    result = backup_date(10**18)
    assert isinstance(result, str)


def test_convert_bytes_zero():
    """Test that 0 bytes converts to 0GB."""
    assert convert_bytes("0") == "0GB"


def test_convert_bytes_one_gib():
    """Test that exactly 1 GiB in bytes converts to 1GB."""
    assert convert_bytes(str(1024**3)) == "1GB"


def test_convert_bytes_integer_input():
    """Test that an integer input is accepted."""
    assert convert_bytes(1024**3) == "1GB"


def test_convert_bytes_float_input():
    """Test that a float input is accepted."""
    assert convert_bytes(float(1024**3)) == "1GB"


def test_convert_bytes_unit_m():
    """Test conversion to megabytes."""
    assert convert_bytes(str(1024**2), unit="M") == "1MB"


def test_convert_bytes_unit_k():
    """Test conversion to kilobytes."""
    assert convert_bytes(str(1024), unit="K") == "1KB"


def test_convert_bytes_unit_t():
    """Test conversion to terabytes."""
    assert convert_bytes(str(1024**4), unit="T") == "1TB"


def test_convert_bytes_non_numeric_returns_str():
    """Test that a non-numeric string is returned unchanged."""
    assert convert_bytes("not-a-number") == "not-a-number"


def test_convert_bytes_none_returns_str():
    """Test that None input is returned as its str() representation."""
    assert convert_bytes(None) == "None"


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("0", "G", "0GB"),
        (str(500 * 1024**2), "G", "0GB"),
        (str(int(1.5 * 1024**3)), "G", "2GB"),
        (str(10 * 1024**3), "G", "10GB"),
    ],
)
def test_convert_bytes_rounding(value, unit, expected):
    """Test that convert_bytes rounds to the nearest integer."""
    assert convert_bytes(value, unit=unit) == expected
