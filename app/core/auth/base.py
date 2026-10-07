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

"""Define the base authentication-provider interface."""

from abc import ABC
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import ClassVar

from app.core.auth.models import (
    BaseUser,
    OAuthToken,
    SessionExchangeTokenResponse,
)


class BaseAuthProvider(ABC):
    """Compose the user model and lifecycle of an authentication provider.

    A concrete provider subclasses its configuration-bearing model (typically a
    ``RemoteAPI`` SDK) first and this mixin second, so the provider *is* its SDK
    and its config maps flat onto the SDK fields. It declares the user model it
    serves via ``user_model``, which the auth seam (``get_user_model``) reads off
    the active provider.

    :cvar user_model: The provider's concrete :class:`BaseUser` subclass.
    :cvar supports_ambient_session: Whether the provider can sign a caller in
        from an ambient session cookie already carried on the request. Defaults
        to ``False``; a provider that validates such a cookie overrides it.
    """

    user_model: ClassVar[type[BaseUser]]
    supports_ambient_session: ClassVar[bool] = False

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None, None]:
        """Yield control while the provider's resources are active.

        Default to a no-op for providers whose SDK needs no async setup or
        teardown; a provider whose SDK is itself an async context manager
        overrides this to enter it.

        :yield: ``None`` once the provider's resources are ready.
        """
        yield

    async def resolve_ambient_session(
        self, cookies: Mapping[str, str]
    ) -> OAuthToken | None:
        """Mint a PMM Extensions token from an ambient session cookie carried on the request.

        A provider that sets ``supports_ambient_session`` overrides this to read
        its session cookie from ``cookies`` and validate it upstream. The base
        provider carries no ambient session, so this is never reached under the
        ``supports_ambient_session`` gate the callers apply.

        :param cookies: The request cookies, keyed by name.
        :return: A minted :class:`OAuthToken` on a valid ambient session, else
            ``None``.
        :raises NotImplementedError: If a provider opts into ambient sessions
            without overriding this method.
        """
        raise NotImplementedError

    async def exchange_ambient_session(
        self,
        cookies: Mapping[str, str],  # noqa: ARG002
    ) -> SessionExchangeTokenResponse | None:
        """Mint a short-lived bearer from an ambient session cookie on the request.

        Unlike :meth:`resolve_ambient_session`, default to denying rather than
        raising: a provider written before this seam existed then refuses the
        exchange instead of surfacing a 500, which is the safe outcome for a
        credential-minting endpoint.

        :param cookies: The request cookies, keyed by name.
        :return: The minted bearer on a valid ambient session, else ``None``.
        """
        return None
