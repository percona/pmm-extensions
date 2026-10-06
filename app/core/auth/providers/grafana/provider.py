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

"""Define the Grafana authentication-provider bundle."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import ClassVar

from app.core.auth.base import BaseAuthProvider
from app.core.auth.models import (
    BaseUser,
    OAuthToken,
    SessionExchangeTokenResponse,
)
from app.core.auth.providers.grafana.models import GrafanaUser
from app.core.auth.providers.grafana.sdk import GrafanaSDK


class GrafanaAuthProvider(GrafanaSDK, BaseAuthProvider):
    """Compose the Grafana SDK, user model, and token model into an auth provider.

    Inherit :class:`GrafanaSDK` first so the provider *is* its SDK -- its config
    maps flat onto the SDK fields (e.g. ``AUTH__PROVIDER__GRAFANA__ENDPOINT``) --
    and the :class:`BaseAuthProvider` mixin second for the model bundle and the
    lifecycle hook.

    :cvar user_model: The Grafana user model.
    :cvar supports_ambient_session: ``True`` -- Grafana validates an ambient
        session cookie, so ambient SSO applies under this provider.
    """

    user_model: ClassVar[type[BaseUser]] = GrafanaUser
    supports_ambient_session: ClassVar[bool] = True

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None, None]:
        """Open the Grafana SDK's async context for the application's lifespan.

        :yield: ``None`` while the SDK's client session is open.
        """
        async with self:
            yield

    async def resolve_ambient_session(
        self, cookies: Mapping[str, str]
    ) -> OAuthToken | None:
        """Mint a PMM Extensions token from an ambient Grafana session cookie.

        Read the Grafana session cookie (named per :attr:`session_cookie_name`)
        off ``cookies`` and validate it against Grafana; an absent cookie yields
        ``None``.

        :param cookies: The request cookies, keyed by name.
        :return: A minted :class:`OAuthToken` on a valid ambient session, else
            ``None``.
        """
        session = cookies.get(self.session_cookie_name)
        if not session:
            return None
        return await GrafanaUser.oauth_token_from_session(session)

    async def exchange_ambient_session(
        self, cookies: Mapping[str, str]
    ) -> SessionExchangeTokenResponse | None:
        """Mint a short-lived PMM Extensions bearer from an ambient Grafana session cookie.

        Read the Grafana session cookie (named per :attr:`session_cookie_name`)
        off ``cookies`` and validate it against Grafana; an absent cookie yields
        ``None``.

        :param cookies: The request cookies, keyed by name.
        :return: The minted bearer on a valid ambient session, else ``None``.
        :raises HTTPException: For a non-401 upstream error, including the
            ``GrafanaException`` raised when Grafana is unreachable. The caller
            swallows it so the exchange denies rather than surfacing a 502.
        """
        session = cookies.get(self.session_cookie_name)
        if not session:
            return None
        return await GrafanaUser.exchange_token_from_session(session)
