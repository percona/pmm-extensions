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

"""Define utilities for describing exceptions."""

__all__ = ["exception_detail"]


def exception_detail(exc: BaseException) -> str:
    """Return the message ``exc`` carries, preferring a non-empty ``detail``.

    An upstream exception's ``detail`` is built from the upstream JSON error
    body, so it may be a dict, list or number; it is always coerced to text.
    A falsy ``detail`` (``""``, ``{}``, ``[]``, ``0``) falls back to
    ``str(exc)``, which for an ``HTTPException`` is ``"<status>: <detail>"``,
    so the value still surfaces beside its status.
    A request timeout raises a bare ``TimeoutError`` whose string form is
    empty, so the type name stands in for a missing message.

    :param exc: The exception to describe.
    :return: ``str(exc.detail)`` when the exception carries a truthy ``detail``,
        else ``str(exc)``, else the exception's type name.
    """
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc) or type(exc).__name__
