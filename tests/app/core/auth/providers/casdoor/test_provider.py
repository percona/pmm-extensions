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

"""Define tests for the Casdoor authentication-provider bundle."""

from uuid import UUID

import pytest

from app.core.auth.base import BaseAuthProvider
from app.core.auth.models import UserRole
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.auth.providers.casdoor.provider import CasdoorAuthProvider

_SERVICE_ID = UUID("00000000-0000-4000-8000-000000000000")


def _provider() -> CasdoorAuthProvider:
    """Build a ``CasdoorAuthProvider`` with minimal config."""
    return CasdoorAuthProvider(
        endpoint="http://localhost:9999",
        client_id="id",
        client_secret="secret",
    )


class TestCasdoorAuthProviderBundle:
    """Test the Casdoor provider bundle wiring and lifecycle."""

    def test_user_model_wired(self):
        """Verify the provider exposes the Casdoor user model."""
        assert CasdoorAuthProvider.user_model is CasdoorUser

    def test_does_not_support_ambient_session(self):
        """Verify Casdoor and the base default do not advertise ambient SSO."""
        assert CasdoorAuthProvider.supports_ambient_session is False
        assert BaseAuthProvider.supports_ambient_session is False

    def test_provider_is_a_casdoor_sdk(self):
        """Verify the provider carries the flat Casdoor SDK config."""
        provider = _provider()
        assert provider.client_id.get_secret_value() == "id"
        assert callable(provider.get_users)

    @pytest.mark.asyncio
    async def test_lifespan_enters_and_exits_the_sdk(self, mocker):
        """Verify ``lifespan`` enters and exits the provider's SDK context."""
        provider = _provider()
        enter = mocker.patch.object(
            CasdoorAuthProvider,
            "__aenter__",
            mocker.AsyncMock(return_value=provider),
        )
        exit_ = mocker.patch.object(
            CasdoorAuthProvider,
            "__aexit__",
            mocker.AsyncMock(return_value=False),
        )
        async with provider.lifespan():
            pass
        enter.assert_awaited_once()
        exit_.assert_awaited_once()


class TestBuildServicePrincipal:
    """Test ``CasdoorUser.build_service_principal``."""

    def test_sets_required_owner(self):
        """Verify the Casdoor service principal fills the required ``owner``."""
        user = CasdoorUser.build_service_principal(
            user_id=_SERVICE_ID, username="extensions-service", role=UserRole.VIEWER
        )
        assert isinstance(user, CasdoorUser)
        assert user.owner == "built-in"
        assert user.username == "extensions-service"
        assert user.role is UserRole.VIEWER
        assert user.is_admin is False

    def test_access_token_survives_model_copy_via_setter(self):
        """Verify ``access_token`` set via the setter survives a ``model_copy``."""
        user = CasdoorUser.build_service_principal(
            user_id=_SERVICE_ID, username="extensions-service", role=UserRole.VIEWER
        )
        copy = user.model_copy()
        copy.access_token = "secret-token"
        assert copy.access_token == "secret-token"
        assert user.access_token == ""
