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

"""Define tests for the app.core.utils.date_time module."""

from datetime import datetime, timedelta, timezone, UTC

import pytest

from app.core.utils import make_datetime_utc
from app.core.utils.date_time import parse_aware_datetime


def test_make_datetime_utc_with_naive_datetime():
    """Test that a naive datetime is converted to an aware UTC datetime."""
    naive_dt = datetime(2023, 1, 1, 12, 0, 0)  # noqa: DTZ001

    utc_dt = make_datetime_utc(naive_dt)

    assert utc_dt.tzinfo == UTC
    assert utc_dt == datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_make_datetime_utc_with_aware_datetime():
    """Test that an aware datetime in a different timezone is converted to UTC correctly."""
    est = timezone(timedelta(hours=-5))

    aware_dt = datetime(2023, 1, 1, 12, 0, 0, tzinfo=est)

    utc_dt = make_datetime_utc(aware_dt)

    expected_utc_dt = datetime(2023, 1, 1, 17, 0, 0, tzinfo=UTC)

    assert utc_dt.tzinfo == UTC, "The timezone should be set to UTC"
    assert utc_dt == expected_utc_dt, f"The datetime should be {expected_utc_dt} in UTC"


class TestParseAwareDatetime:
    """Test reading a timezone-aware timestamp out of untrusted data."""

    def test_reads_an_offset_timestamp_as_utc(self):
        """Assert an aware ISO-8601 string comes back as the same instant in UTC."""
        parsed = parse_aware_datetime("2026-09-01T02:00:00+02:00")

        assert parsed == datetime(2026, 9, 1, tzinfo=UTC)
        assert parsed is not None
        assert parsed.tzinfo == UTC

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("2026-09-01T00:00:00", id="naive"),
            pytest.param("", id="empty"),
            pytest.param("yesterday", id="garbage"),
            pytest.param(1_756_684_800, id="epoch-number"),
            pytest.param(None, id="none"),
            pytest.param(["2026-09-01T00:00:00+00:00"], id="list"),
            pytest.param("0001-01-01T00:00:00+01:00", id="before-the-utc-range"),
        ],
    )
    def test_returns_none_for_anything_else(self, value: object):
        """Assert a value that is not an aware ISO-8601 string reads as missing.

        A naive timestamp is refused rather than assumed UTC, since nothing says
        which zone it was written in.
        """
        assert parse_aware_datetime(value) is None
