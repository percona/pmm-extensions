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

"""Define datetime utilities."""

from datetime import datetime, UTC

__all__ = ["make_datetime_utc", "parse_aware_datetime", "utc_now"]


def utc_now() -> datetime:
    """Get current UTC datetime with microsecond set to 0.

    :return: Current aware datetime with timezone set to UTC.
    :rtype: datetime
    """
    return datetime.now(UTC).replace(microsecond=0)


def make_datetime_utc(dt: datetime) -> datetime:
    """Convert a datetime to UTC.

    This method converts an aware datetime to UTC, or just adds UTC tzinfo
    to a naive datetime.

    :param dt: Datetime to convert timezone.
    :type dt: datetime
    :return: Aware datetime with timezone set to UTC.
    :rtype: datetime
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_aware_datetime(value: object) -> datetime | None:
    """Return ``value`` as a UTC datetime when it is an aware ISO-8601 string.

    Meant for timestamps read back from untrusted or operator-editable data, so
    any other value reads as missing instead of raising. A naive timestamp is
    refused rather than assumed UTC, since nothing says which zone wrote it.

    :param value: The candidate timestamp.
    :return: The instant in UTC, or ``None`` when ``value`` is not one.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None
