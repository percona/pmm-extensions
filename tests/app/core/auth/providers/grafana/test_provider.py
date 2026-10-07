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

"""Define tests for the Grafana authentication-provider bundle."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.auth.providers.casdoor.provider import CasdoorAuthProvider
from app.core.auth.providers.grafana.models import GrafanaUser
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider


def _provider() -> GrafanaAuthProvider:
    """Build a ``GrafanaAuthProvider`` with minimal config."""
    return GrafanaAuthProvider(
        endpoint="https://grafana.example.com",
        service_account_token="svc-token",
    )


class TestGrafanaAuthProviderBundle:
    """Test the Grafana provider bundle wiring and lifecycle."""

    def test_user_model_wired(self):
        """Verify the provider exposes the Grafana user model."""
        assert GrafanaAuthProvider.user_model is GrafanaUser

    def test_supports_ambient_session(self):
        """Verify the Grafana provider advertises ambient-session support."""
        assert GrafanaAuthProvider.supports_ambient_session is True

    def test_provider_is_a_grafana_sdk(self):
        """Verify the provider carries the flat Grafana SDK config."""
        provider = _provider()
        assert provider.service_account_token.get_secret_value() == "svc-token"
        assert provider.access_token_max_age == timedelta(hours=1)
        assert provider.refresh_token_max_age == timedelta(days=7)
        assert callable(provider.get_org_users)

    @pytest.mark.asyncio
    async def test_lifespan_enters_and_exits_the_sdk(self, mocker):
        """Verify ``lifespan`` enters and exits the provider's SDK context."""
        provider = _provider()
        enter = mocker.patch.object(
            GrafanaAuthProvider,
            "__aenter__",
            mocker.AsyncMock(return_value=provider),
        )
        exit_ = mocker.patch.object(
            GrafanaAuthProvider,
            "__aexit__",
            mocker.AsyncMock(return_value=False),
        )
        async with provider.lifespan():
            pass
        enter.assert_awaited_once()
        exit_.assert_awaited_once()


class TestResolveAmbientSession:
    """Test the provider's ambient-session seam (``resolve_ambient_session``)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_session_cookie_absent(self, mocker):
        """Verify a missing session cookie yields ``None`` without an upstream call."""
        provider = _provider()
        mint = mocker.patch.object(
            GrafanaUser, "oauth_token_from_session", mocker.AsyncMock()
        )
        assert await provider.resolve_ambient_session({"unrelated": "x"}) is None
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mints_token_from_session_cookie(self, mocker):
        """Verify the provider delegates its session cookie to the user model."""
        provider = _provider()
        token = mocker.Mock()
        mint = mocker.patch.object(
            GrafanaUser,
            "oauth_token_from_session",
            mocker.AsyncMock(return_value=token),
        )
        result = await provider.resolve_ambient_session(
            {provider.session_cookie_name: "ambient"}
        )
        assert result is token
        mint.assert_awaited_once_with("ambient")


class TestExchangeAmbientSession:
    """Verify the provider's session-exchange seam (``exchange_ambient_session``)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_session_cookie_absent(self, mocker):
        """Verify a missing session cookie yields ``None`` without an upstream call."""
        provider = _provider()
        mint = mocker.patch.object(
            GrafanaUser, "exchange_token_from_session", mocker.AsyncMock()
        )
        assert await provider.exchange_ambient_session({"unrelated": "x"}) is None
        mint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mints_exchange_token_from_session_cookie(self, mocker):
        """Verify the provider delegates its session cookie to the user model."""
        provider = _provider()
        token = mocker.Mock()
        mint = mocker.patch.object(
            GrafanaUser,
            "exchange_token_from_session",
            mocker.AsyncMock(return_value=token),
        )
        result = await provider.exchange_ambient_session(
            {provider.session_cookie_name: "ambient"}
        )
        assert result is token
        mint.assert_awaited_once_with("ambient")

    @pytest.mark.asyncio
    async def test_base_provider_default_denies(self):
        """Verify a provider that does not override the seam denies rather than raising.

        Casdoor never opts into ambient sessions, so the inherited no-op is what
        an out-of-tree provider unaware of this feature would run.
        """
        provider = CasdoorAuthProvider(
            endpoint="https://casdoor.example.com",
            client_id="client",
            client_secret="secret",
        )

        assert await provider.exchange_ambient_session({"grafana_session": "s"}) is None


class TestExchangeTokenMaxAge:
    """Verify the configured lifetime of a minted exchange assertion."""

    def test_defaults_to_five_minutes(self):
        """Verify the default keeps the embedded bearer short-lived."""
        assert _provider().exchange_token_max_age == timedelta(minutes=5)

    @pytest.mark.parametrize("value", [timedelta(0), timedelta(seconds=-1)])
    def test_rejects_a_non_positive_value_at_config_load(self, value):
        """Verify a non-positive lifetime is a startup error, not a silent auth outage.

        Without the bound, ``0`` expires every assertion at mint time and
        disables embedded-UI authentication across the deployment.
        """
        with pytest.raises(ValidationError):
            GrafanaAuthProvider(
                endpoint="https://grafana.example.com",
                service_account_token="svc-token",
                exchange_token_max_age=value,
            )

    def test_does_not_fall_back_to_the_access_lifetime(self):
        """Verify the exchange lifetime is independent of the access lifetime."""
        provider = GrafanaAuthProvider(
            endpoint="https://grafana.example.com",
            service_account_token="svc-token",
            access_token_max_age=timedelta(hours=12),
        )

        assert provider.exchange_token_max_age == timedelta(minutes=5)
