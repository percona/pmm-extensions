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

__all__ = ["make_datetime_utc", "utc_now"]


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
