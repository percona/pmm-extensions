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

"""Security utilities module."""

from fastapi import Request
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer

from app.core.config import settings

crypto_serializer = URLSafeSerializer(settings.SECRET_KEY.get_secret_value())
crypto_timestamp_serializer = URLSafeTimedSerializer(
    settings.SECRET_KEY.get_secret_value()
)

SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_bearer_authenticated(request: Request) -> bool:
    """Return whether the request carries an ``Authorization: Bearer`` header.

    Inspects only the ``Authorization`` header prefix — the token itself is not
    validated. Used to reject a credential-less request before ``oauth2_scheme``
    can raise a bare Starlette error, and to gate mutating methods.

    :param request: The incoming HTTP request.
    :return: ``True`` when the header starts with ``Bearer ``, ``False`` otherwise.
    """
    return request.headers.get("authorization", "").lower().startswith("bearer ")


def get_internal_token() -> str:
    """Return the configured or derived internal service token.

    ``Settings.derive_internal_token`` populates ``EXTENSIONS_INTERNAL_TOKEN`` on every
    constructed instance, from an explicit value or derived from ``SECRET_KEY``,
    so the token is always present here.

    :return: The internal token's secret value.
    """
    return settings.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
