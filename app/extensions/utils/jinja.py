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

"""Define Jinja2 filters and utilities."""

import math
import time
from datetime import datetime
from typing import Any

from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer
from pygments.util import ClassNotFound

from app.core.utils import make_datetime_utc


def syntax_highlight(
    code: Any,
    language: str | None = None,
    *,
    stripall: bool = True,
    **fmt_options: Any,
) -> str:
    """Apply syntax highlighting to the provided value using Pygments.

    Coerce ``code`` to ``str`` before tokenising so that arbitrary scalar
    values (e.g. integers from ``task.meta``) do not reach Pygments' decode
    path and raise ``AttributeError``. If a specific language is provided,
    retrieve the corresponding lexer. If the lexer for the given language
    is not found, or if no language is specified, guess the lexer based on
    the coerced content or fall back to a plain text lexer.

    :param code: The value to be highlighted. Coerced to ``str`` internally.
    :type code: Any
    :param language: The programming language of the code. If not provided, the lexer is
        guessed.
    :type language: str or None
    :param stripall: Whether to strip all leading/trailing whitespace from the code
        before highlighting. When ``False``, also disables newline stripping
        (``stripnl``) so that leading/trailing blank lines are preserved for correct
        line numbering in multi-block previews.
    :type stripall: bool
    :param fmt_options: Additional keyword arguments passed to the
        :class:`pygments.formatters.HtmlFormatter`.
    :type fmt_options: Any
    :return: A string containing HTML markup with syntax-highlighted code.
    :rtype: str
    """
    code = str(code)
    lexer_options = {"stripall": stripall}
    if not stripall:
        lexer_options["stripnl"] = False
    if language is None:
        lexer = guess_lexer(code, **lexer_options)
    else:
        try:
            lexer = get_lexer_by_name(language, **lexer_options)
        except ClassNotFound:
            lexer = TextLexer(**lexer_options)
    return highlight(code, lexer, HtmlFormatter(**fmt_options))


def utc_isoformat(dt: datetime) -> str:
    """Convert a datetime to UTC and return a corresponding ISO 8601 formatted string.

    :param dt: Datetime to convert timezone.
    :type dt: datetime
    :return: ISO 8601 formatted string for aware datetime with timezone set to UTC.
    :rtype: str
    """
    return make_datetime_utc(dt).isoformat()


def humanize_bytes(num_bytes: int) -> str:
    """Return a humanized file size string from a number of bytes.

    :param num_bytes: Total number of bytes to humanize.
    :type num_bytes: int
    :return: Humanized file size.
    :rtype: str
    """
    bytes_threshold = 1024
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num_bytes) < bytes_threshold:
            return f"{num_bytes:3.1f}{unit}B"
        num_bytes /= bytes_threshold
    return f"{num_bytes:.1f}YiB"


def timestamp_format(epoch_seconds: float) -> str:
    """Format a Unix timestamp (seconds) as a human-readable UTC date string.

    :param epoch_seconds: Seconds since epoch.
    :type epoch_seconds: int | float
    :return: Formatted date string.
    :rtype: str
    """
    try:
        return time.strftime("%d %B %Y at %H:%M:%S", time.gmtime(epoch_seconds))
    except (OverflowError, OSError, ValueError):
        return str(epoch_seconds)


def backup_date(value: int | str) -> str:
    """Format a backup period start value as a readable date.

    If the value is an integer (epoch ms), it is converted to a formatted
    date string.  If it is already a string it is returned as-is.

    :param value: Epoch milliseconds or pre-formatted date string.
    :type value: int | str
    :return: Formatted date string.
    :rtype: str
    """
    if isinstance(value, int):
        try:
            return time.strftime("%d %B %Y at %H:%M:%S UTC", time.gmtime(value / 1000))
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def convert_bytes(value: str | float, unit: str = "G") -> str:
    """Convert a byte count (given as a string) to a human-readable size.

    :param value: Byte count as a string, int, or float.
    :type value: str | int | float
    :param unit: Target unit (K, M, G, T, P).
    :type unit: str
    :return: Human-readable size string.
    :rtype: str
    """
    try:
        total_bytes = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5}
    exp = units.get(unit, 3)
    result = total_bytes / math.pow(1024, exp)
    return f"{round(result)}{unit}B"


DEFAULT_FILTERS = {
    "syntax_highlight": syntax_highlight,
    "utc_isoformat": utc_isoformat,
    "humanize_bytes": humanize_bytes,
    "timestamp_format": timestamp_format,
    "backup_date": backup_date,
    "convert_bytes": convert_bytes,
}
