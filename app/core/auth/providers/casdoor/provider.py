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

"""Define the Casdoor authentication-provider bundle."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar

from app.core.auth.base import BaseAuthProvider
from app.core.auth.models import BaseUser
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.auth.providers.casdoor.sdk import CasdoorSDK


class CasdoorAuthProvider(CasdoorSDK, BaseAuthProvider):
    """Compose the Casdoor SDK and user model into an auth provider.

    Inherit :class:`CasdoorSDK` first so the provider *is* its SDK -- its config
    maps flat onto the SDK fields (e.g. ``AUTH__PROVIDER__CASDOOR__ENDPOINT``) --
    and the :class:`BaseAuthProvider` mixin second for the user model and the
    lifecycle hook.

    :cvar user_model: The Casdoor user model.
    """

    user_model: ClassVar[type[BaseUser]] = CasdoorUser

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None, None]:
        """Open the Casdoor SDK's async context for the application's lifespan.

        :yield: ``None`` while the SDK's client session is open.
        """
        async with self:
            yield
