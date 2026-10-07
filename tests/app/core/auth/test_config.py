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

"""Define tests for the authentication-provider configuration.

Resolution is exercised through ``IsolatedAuthSettings`` -- an ``AuthSettings``
subclass whose settings sources are restricted to init kwargs -- so provider
resolution is tested on explicit input, isolated from the ambient
environment/YAML settings sources.
"""

from types import SimpleNamespace
from typing import ClassVar

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.core.auth.base import BaseAuthProvider
from app.core.auth.config import (
    _LegacyAuthUserModelSettings,
    _LegacyCasdoorSettings,
    AuthSettings,
    detect_removed_auth_user_model,
)
from app.core.auth.models import BaseTokenPayload, BaseUser
from app.core.auth.providers.casdoor.models import CasdoorTokenPayload, CasdoorUser
from app.core.auth.providers.casdoor.provider import CasdoorAuthProvider
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider

_CASDOOR_CONFIG = {
    "endpoint": "http://localhost:9999",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
}

_GRAFANA_CONFIG = {
    "endpoint": "https://grafana.example.com",
    "service_account_token": "test-service-account-token",
}


class _StubSDK(BaseModel):
    """Represent a minimal config-bearing model for a stub provider."""

    token: str = ""


class StubAuthProvider(_StubSDK, BaseAuthProvider):
    """Bundle a stub SDK to exercise the CUSTOM provider class-path branch."""

    user_model: ClassVar[type[BaseUser]] = CasdoorUser
    token_payload_model: ClassVar[type[BaseTokenPayload]] = CasdoorTokenPayload


_STUB_PATH = f"{__name__}.StubAuthProvider"

# ``IsolatedAuthSettings`` drops the YAML source, so pydantic-settings warns that
# the inherited ``yaml_file`` config key is unused. That is expected here.
pytestmark = pytest.mark.filterwarnings("ignore:Config key .yaml_file.")


class IsolatedAuthSettings(AuthSettings):
    """Restrict ``AuthSettings`` sources to init kwargs for hermetic tests."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],  # noqa: ARG003
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return only the init-kwargs source, ignoring env and YAML."""
        return (init_settings,)


@pytest.fixture(autouse=True)
def no_legacy_casdoor(mocker):
    """Disable the legacy CASDOOR reader so ambient env/yaml cannot leak in."""
    return mocker.patch("app.core.auth.config._read_legacy_casdoor", return_value=None)


class TestAuthSettingsResolution:
    """Test provider resolution on ``AuthSettings``."""

    def test_builtin_casdoor_resolves_via_enum(self):
        """Verify a built-in name resolves to its class via the enum."""
        settings = IsolatedAuthSettings(PROVIDER={"casdoor": _CASDOOR_CONFIG})
        assert isinstance(settings.active_provider, CasdoorAuthProvider)
        assert settings.active_provider.client_id.get_secret_value() == "test-client-id"

    @pytest.mark.parametrize("name", ["casdoor", "CASDOOR", "CasDoor"])
    def test_mixed_case_builtin_name_resolves(self, name):
        """Verify built-in names resolve case-insensitively (enum ``.upper()``)."""
        settings = IsolatedAuthSettings(PROVIDER={name: _CASDOOR_CONFIG})
        assert isinstance(settings.active_provider, CasdoorAuthProvider)

    def test_builtin_grafana_resolves_via_enum(self):
        """Verify the Grafana name resolves to its provider class via the enum."""
        settings = IsolatedAuthSettings(PROVIDER={"grafana": _GRAFANA_CONFIG})
        assert isinstance(settings.active_provider, GrafanaAuthProvider)
        assert (
            settings.active_provider.service_account_token.get_secret_value()
            == "test-service-account-token"
        )

    def test_unknown_provider_errors_clearly(self):
        """Verify an unknown provider name errors and lists the available names."""
        with pytest.raises(
            ValidationError, match="available: casdoor, custom, grafana"
        ):
            IsolatedAuthSettings(PROVIDER={"okta": _CASDOOR_CONFIG})

    @pytest.mark.parametrize("class_key", ["PROVIDER_CLASS", "provider_class"])
    def test_custom_provider_resolves_via_class_path(self, class_key):
        """Verify CUSTOM resolves an out-of-tree class from ``PROVIDER_CLASS``.

        The lowercase key mirrors how env vars arrive
        (``AUTH__PROVIDER__CUSTOM__PROVIDER_CLASS`` is lowercased by
        pydantic-settings), so the lookup must be case-insensitive.
        """
        settings = IsolatedAuthSettings(
            PROVIDER={"custom": {class_key: _STUB_PATH, "token": "abc"}}
        )
        provider = settings.active_provider
        assert isinstance(provider, StubAuthProvider)
        assert provider.token == "abc"
        assert "PROVIDER_CLASS" not in type(provider).model_fields

    def test_custom_provider_without_class_path_errors(self):
        """Verify a CUSTOM entry missing ``PROVIDER_CLASS`` errors at load."""
        with pytest.raises(ValidationError, match="PROVIDER_CLASS"):
            IsolatedAuthSettings(PROVIDER={"custom": {"token": "abc"}})

    def test_custom_provider_needs_no_casdoor_config(self):
        """Verify selecting a non-casdoor provider needs no Casdoor config."""
        settings = IsolatedAuthSettings(
            PROVIDER={"custom": {"PROVIDER_CLASS": _STUB_PATH}}
        )
        assert isinstance(settings.active_provider, StubAuthProvider)

    def test_zero_providers_fails_fast(self):
        """Verify configuring no provider fails fast."""
        with pytest.raises(ValidationError, match="Exactly one auth provider"):
            IsolatedAuthSettings(PROVIDER={})

    def test_two_providers_fails_fast(self):
        """Verify configuring more than one provider fails fast."""
        with pytest.raises(ValidationError, match="Exactly one auth provider"):
            IsolatedAuthSettings(
                PROVIDER={
                    "casdoor": _CASDOOR_CONFIG,
                    "custom": {"PROVIDER_CLASS": _STUB_PATH},
                }
            )

    def test_null_entry_dropped_sibling_survives(self):
        """Drop a ``None``-valued entry and resolve the surviving sibling."""
        settings = IsolatedAuthSettings(
            PROVIDER={"casdoor": None, "grafana": _GRAFANA_CONFIG}
        )
        assert list(settings.PROVIDER) == ["grafana"]
        assert isinstance(settings.active_provider, GrafanaAuthProvider)

    def test_null_entry_emptying_map_fails_fast(self):
        """Reject a ``None`` entry that empties the map via the count error."""
        with pytest.raises(ValidationError, match="Exactly one auth provider"):
            IsolatedAuthSettings(PROVIDER={"casdoor": None})


class TestDeprecationShim:
    """Test the legacy top-level ``CASDOOR`` deprecation shim."""

    def test_legacy_only_folds_into_casdoor(self, mocker):
        """Verify legacy-only config folds into ``PROVIDER.casdoor`` with one warning."""
        mocker.patch(
            "app.core.auth.config._read_legacy_casdoor",
            return_value=dict(_CASDOOR_CONFIG),
        )
        mock_logger = mocker.patch("app.core.auth.config.logger")
        settings = IsolatedAuthSettings(PROVIDER={})
        assert isinstance(settings.active_provider, CasdoorAuthProvider)
        mock_logger.warning.assert_called_once()
        assert "is deprecated" in mock_logger.warning.call_args.args[0]

    def test_legacy_fills_gaps_canonical_wins_on_conflict(self, mocker):
        """Verify legacy fills missing casdoor fields but canonical wins conflicts."""
        mocker.patch(
            "app.core.auth.config._read_legacy_casdoor",
            return_value={
                "endpoint": "http://localhost:9999",
                "client_id": "legacy-id",
                "client_secret": "legacy-secret",
            },
        )
        mock_logger = mocker.patch("app.core.auth.config.logger")
        settings = IsolatedAuthSettings(
            PROVIDER={
                "casdoor": {
                    "endpoint": "http://localhost:9999",
                    "client_id": "canonical-id",
                }
            }
        )
        provider = settings.active_provider
        assert provider.client_id.get_secret_value() == "canonical-id"
        assert provider.client_secret.get_secret_value() == "legacy-secret"
        mock_logger.warning.assert_called_once()
        assert "is deprecated" in mock_logger.warning.call_args.args[0]

    def test_legacy_ignored_when_non_casdoor_provider_configured(self, mocker):
        """Verify legacy CASDOOR is ignored when a non-casdoor provider is set."""
        mocker.patch(
            "app.core.auth.config._read_legacy_casdoor",
            return_value=dict(_CASDOOR_CONFIG),
        )
        mock_logger = mocker.patch("app.core.auth.config.logger")
        settings = IsolatedAuthSettings(
            PROVIDER={"custom": {"PROVIDER_CLASS": _STUB_PATH}}
        )
        assert isinstance(settings.active_provider, StubAuthProvider)
        mock_logger.warning.assert_called_once()
        assert "Ignoring the deprecated" in mock_logger.warning.call_args.args[0]

    def test_canonical_only_emits_no_deprecation_warning(self, mocker):
        """Verify canonical-only config emits no deprecation warning."""
        mocker.patch("app.core.auth.config._read_legacy_casdoor", return_value=None)
        mock_logger = mocker.patch("app.core.auth.config.logger")
        IsolatedAuthSettings(PROVIDER={"casdoor": _CASDOOR_CONFIG})
        mock_logger.warning.assert_not_called()

    def test_null_casdoor_beats_legacy_resurrection(self, mocker):
        """Verify an explicit ``casdoor: null`` beats a legacy CASDOOR resurrection.

        The null-drop runs after the legacy fold, so a legacy top-level
        ``CASDOOR`` cannot revive the sole provider an overlay nulled out; the
        map empties and the count invariant reports it. Reordering the drop
        before the fold would resurrect casdoor and this would stop raising.
        """
        mocker.patch(
            "app.core.auth.config._read_legacy_casdoor",
            return_value=dict(_CASDOOR_CONFIG),
        )
        with pytest.raises(ValidationError, match="Exactly one auth provider"):
            IsolatedAuthSettings(PROVIDER={"casdoor": None})

    def test_null_casdoor_keeps_sibling_despite_legacy(self, mocker):
        """Verify a nulled ``casdoor`` leaves the sibling active despite legacy config."""
        mocker.patch(
            "app.core.auth.config._read_legacy_casdoor",
            return_value=dict(_CASDOOR_CONFIG),
        )
        settings = IsolatedAuthSettings(
            PROVIDER={"casdoor": None, "grafana": _GRAFANA_CONFIG}
        )
        assert list(settings.PROVIDER) == ["grafana"]
        assert isinstance(settings.active_provider, GrafanaAuthProvider)


class TestRemovedAuthUserModelDetector:
    """Test the removed ``AUTH_USER_MODEL`` startup detector."""

    def test_unset_is_noop(self, mocker):
        """Verify an unset ``AUTH_USER_MODEL`` neither warns nor raises."""
        mocker.patch(
            "app.core.auth.config._LegacyAuthUserModelSettings",
            return_value=SimpleNamespace(AUTH_USER_MODEL=None),
        )
        mock_logger = mocker.patch("app.core.auth.config.logger")
        detect_removed_auth_user_model()
        mock_logger.warning.assert_not_called()

    def test_default_value_warns(self, mocker):
        """Verify the removed default value warns without failing."""
        mocker.patch(
            "app.core.auth.config._LegacyAuthUserModelSettings",
            return_value=SimpleNamespace(AUTH_USER_MODEL="app.models.CasdoorUser"),
        )
        mock_logger = mocker.patch("app.core.auth.config.logger")
        detect_removed_auth_user_model()
        mock_logger.warning.assert_called_once()
        assert "AUTH_USER_MODEL is removed" in mock_logger.warning.call_args.args[0]

    def test_non_default_override_fails_fast(self, mocker):
        """Verify a non-default override fails fast with a migration error."""
        mocker.patch(
            "app.core.auth.config._LegacyAuthUserModelSettings",
            return_value=SimpleNamespace(AUTH_USER_MODEL="my.custom.UserModel"),
        )
        with pytest.raises(ValueError, match="AUTH_USER_MODEL is removed"):
            detect_removed_auth_user_model()


def test_legacy_casdoor_settings_reads_secret_file(tmp_path):
    """Resolve the legacy top-level ``CASDOOR`` block from a mounted secret file."""
    (tmp_path / "CASDOOR").write_text(
        '{"endpoint": "http://from-file:9999"}\n', encoding="utf-8"
    )

    instance = _LegacyCasdoorSettings(_secrets_dir=tmp_path)

    assert instance.CASDOOR is not None
    assert instance.CASDOOR["endpoint"] == "http://from-file:9999"


def test_legacy_auth_user_model_settings_reads_secret_file(tmp_path):
    """Resolve the legacy ``AUTH_USER_MODEL`` key from a mounted secret file."""
    (tmp_path / "AUTH_USER_MODEL").write_text("app.from.File\n", encoding="utf-8")

    instance = _LegacyAuthUserModelSettings(_secrets_dir=tmp_path)

    assert instance.AUTH_USER_MODEL == "app.from.File"
