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

"""Define tests for the app.core.config module."""

import base64
import hashlib
import hmac
import secrets
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import AliasChoices, Field, SecretStr, ValidationError
from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.exceptions import SettingsError
from pydantic_settings.sources import (
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
)

from app import BASE_DIR
from app.core.alerts.config import AlertSettings
from app.core.auth.config import AuthSettings
from app.core.config import (
    _ExtensionsDatabaseSettings,
    _sanitize_client_kwargs,
    BaseYamlAppSettings,
    create_app,
    default_lifespan,
    detect_removed_settings_override_keys,
    PMMSettings,
    PreEnvSettings,
    Settings,
    settings,
    YamlPrefixConfigSettingsSource,
)
from app.core.settings_override.registry import (
    field_reload_classification,
    ReloadClassification,
)
from app.extensions.apps.alerts.config import AlertsSettings
from app.extensions.apps.atw.config import AtwSettings
from app.extensions.apps.report.config import HealthReportSettings
from app.extensions.config import ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.inventory.config import InventorySettings
from app.tasks.anonymizer.config import AnonymizerSettings
from app.tasks.config import TasksSettings

DEFAULT_ANNOTATIONS_TIMEOUT = 5
CUSTOM_ANNOTATIONS_TIMEOUT = 7


def test_sanitize_client_kwargs_redacts_url_password():
    """``_sanitize_client_kwargs`` masks a password embedded in an endpoint URL."""
    safe = _sanitize_client_kwargs(
        {
            "endpoint": "http://user:hunter2@localhost:8000/",
            "verify_ssl": False,
        }
    )
    assert "hunter2" not in safe["endpoint"]
    assert "****" in safe["endpoint"]
    assert safe["verify_ssl"] is False


def test_sanitize_client_kwargs_preserves_credential_free_values():
    """``_sanitize_client_kwargs`` leaves non-credential values untouched."""
    api_key = SecretStr("token")
    safe = _sanitize_client_kwargs(
        {"endpoint": "http://localhost:8000/", "api_key": api_key}
    )
    assert safe["endpoint"] == "http://localhost:8000/"
    assert safe["api_key"] is api_key


class DummySettings(BaseSettings):
    """Define dummy settings class for testing purposes."""

    model_config = SettingsConfigDict(env_prefix="DUMMY_")


@pytest.fixture
def mock_yaml_data():
    """Provide mock YAML data for testing."""
    return {
        "default": {
            "key1": "default_value1",
            "key2": "default_value2",
            "items": ["a", "b"],
        },
        "env1": {"key1": "env1_value1", "nested": {"key2": "env1_nested_value2"}},
        "env2": {
            "key3": "env2_value3",
            "nested": {"key2": "env2_nested_value2", "key4": "env2_nested_value4"},
        },
    }


@pytest.fixture
def mock_yaml_file(tmp_path, mock_yaml_data):
    """Create a temporary mock YAML file for testing."""
    yaml_file = tmp_path / "settings.yaml"
    import yaml

    with yaml_file.open("w") as f:
        yaml.dump(mock_yaml_data, f)
    return yaml_file


def test_yaml_prefix_config_settings_source_multiple_prefixes(mock_yaml_file):
    """Test YamlPrefixConfigSettingsSource with multiple prefixes."""
    prefixes = ("env2", "nested")
    base_prefix = "default"

    settings_source = YamlPrefixConfigSettingsSource(
        DummySettings,
        yaml_file=mock_yaml_file,
        prefixes=prefixes,
        base_prefix=base_prefix,
    )

    expected_data = {
        "key2": "env2_nested_value2",
        "key4": "env2_nested_value4",
    }

    assert settings_source.yaml_data == expected_data, (
        f"Expected {expected_data} but got {settings_source.yaml_data}"
    )


def test_yaml_prefix_config_settings_source_base_profile(
    mock_yaml_file, mock_yaml_data
):
    """Select the base profile and return its data without doubling any list."""
    prefixes = ("default",)
    base_prefix = "default"

    settings_source = YamlPrefixConfigSettingsSource(
        DummySettings,
        yaml_file=mock_yaml_file,
        prefixes=prefixes,
        base_prefix=base_prefix,
    )

    assert settings_source.yaml_data == mock_yaml_data["default"]
    assert settings_source.yaml_data["items"] == ["a", "b"]


class MockSettings(BaseSettings):
    """Define mock settings class for testing."""


@pytest.fixture
def mock_env_settings():
    """Mock for environment settings."""
    env_settings = MagicMock()
    env_settings.env_vars = {
        "prefix__UVICORN_HOST": "0.0.0.0",
        "prefix__UVICORN_PORT": "8000",
    }
    return env_settings


@pytest.fixture
def mock_dotenv_settings():
    """Mock for dotenv settings."""
    dotenv_settings = MagicMock()
    dotenv_settings.env_vars = {
        "prefix__SSL_KEYFILE": "/path/to/keyfile",
        "prefix__SSL_CERTFILE": "/path/to/certfile",
    }
    return dotenv_settings


@pytest.fixture
def mock_yaml_prefix_source():
    """Mock for YAML settings source."""
    yaml_prefix_source = MagicMock()
    yaml_prefix_source.return_value = {
        "UVICORN_HOST": "127.0.0.1",
        "UVICORN_PORT": 8080,
    }
    return yaml_prefix_source


@pytest.fixture
def settings_class():
    """Mock settings class."""

    class TestSettings(BaseYamlAppSettings):
        SETTINGS_PREFIXES = ["prefix"]

    return TestSettings


@pytest.fixture
def file_secret_settings(settings_class, tmp_path):
    """Provide a real file-secret source over a directory holding one prefixed secret.

    A ``MagicMock`` cannot stand in here: ``NestedSecretsSettingsSource`` reads
    ``settings_cls.model_config`` and rejects the ``MagicMock`` that every
    ``conf.get(...)`` returns with an opaque ``SettingsError``.
    """
    (tmp_path / "prefix__SOME_SECRET").write_text("supersecret", encoding="utf-8")
    return SecretsSettingsSource(settings_class, secrets_dir=tmp_path)


def test_settings_customise_sources(
    settings_class,
    mock_env_settings,
    mock_dotenv_settings,
    file_secret_settings,
    mock_yaml_prefix_source,
):
    """Test the settings_customise_sources method."""
    settings_cls = settings_class

    customised_sources = settings_cls.settings_customise_sources(
        MockSettings,
        mock_yaml_prefix_source,
        mock_env_settings,
        mock_dotenv_settings,
        file_secret_settings,
    )

    expected_length = 5
    assert isinstance(customised_sources, tuple)
    assert len(customised_sources) == expected_length

    processed_env_vars = customised_sources[1].env_vars
    assert "UVICORN_HOST" in processed_env_vars
    assert "UVICORN_PORT" in processed_env_vars
    assert processed_env_vars["UVICORN_HOST"] == "0.0.0.0"
    assert processed_env_vars["UVICORN_PORT"] == "8000"

    processed_dotenv_vars = customised_sources[2].env_vars
    assert "SSL_KEYFILE" in processed_dotenv_vars
    assert "SSL_CERTFILE" in processed_dotenv_vars
    assert processed_dotenv_vars["SSL_KEYFILE"] == "/path/to/keyfile"
    assert processed_dotenv_vars["SSL_CERTFILE"] == "/path/to/certfile"

    secret_source = customised_sources[3]
    assert isinstance(secret_source, NestedSecretsSettingsSource)
    assert secret_source.env_vars == {"some_secret": "supersecret"}


@pytest.mark.asyncio
async def test_default_lifespan():
    """Verify default_lifespan enters the active provider's lifespan and closes sessions."""
    mock_lifespan_cm = AsyncMock()
    mock_provider = MagicMock()
    mock_provider.lifespan.return_value = mock_lifespan_cm
    mock_close_registry = AsyncMock()

    with (
        patch.object(Settings, "close_client_registry", mock_close_registry),
        patch(
            "app.core.auth.config.get_active_auth_provider", return_value=mock_provider
        ),
    ):
        app = FastAPI()

        async with default_lifespan(app):
            pass

    mock_provider.lifespan.assert_called_once()
    mock_lifespan_cm.__aenter__.assert_called_once()
    mock_lifespan_cm.__aexit__.assert_called_once()
    mock_close_registry.assert_called_once()


def test_uvicorn_reload_defaults_to_false():
    """Assert ``UVICORN_RELOAD`` defaults to ``False`` on ``BaseYamlAppSettings``."""
    assert BaseYamlAppSettings.model_fields["UVICORN_RELOAD"].default is False


@pytest.mark.parametrize(
    "field_name",
    [
        "UVICORN_EXTRA_RELOAD_DIRS",
        "UVICORN_EXTRA_RELOAD_INCLUDES",
        "UVICORN_EXTRA_RELOAD_EXCLUDES",
    ],
)
def test_uvicorn_extra_reload_fields_default_to_empty_list(field_name: str):
    """Assert extra reload fields default to empty lists on ``BaseYamlAppSettings``."""
    field_info = BaseYamlAppSettings.model_fields[field_name]
    assert field_info.default == []


@pytest.mark.parametrize(
    ("field_name", "extra_values"),
    [
        ("UVICORN_EXTRA_RELOAD_DIRS", ["/extra/dir1", "/extra/dir2"]),
        ("UVICORN_EXTRA_RELOAD_INCLUDES", ["*.toml", "*.yaml"]),
        ("UVICORN_EXTRA_RELOAD_EXCLUDES", ["*.log", "*.tmp"]),
    ],
)
def test_uvicorn_extra_reload_fields_accept_nonempty_lists(field_name, extra_values):
    """Assert extra reload fields store non-empty values on ``BaseYamlAppSettings``."""
    instance = BaseYamlAppSettings.model_construct(**{field_name: extra_values})
    assert getattr(instance, field_name) == extra_values


class TestPMMSettings:
    """Test the core ``PMMSettings`` model."""

    def test_defaults_all_none_or_true(self):
        """Assert all fields default to ``None`` or ``True``."""
        pmm = PMMSettings()
        assert pmm.endpoint is None
        assert pmm.frontend is None
        assert pmm.api_key is None
        assert pmm.verify_ssl is True
        assert pmm.execution_target is None
        assert pmm.annotations_timeout == DEFAULT_ANNOTATIONS_TIMEOUT

    def test_hostname_extracted_from_endpoint(self):
        """Assert ``hostname`` extracts the host from the endpoint URL."""
        pmm = PMMSettings(endpoint="https://pmm.example.com:8443/path")
        assert pmm.hostname == "pmm.example.com"

    def test_hostname_none_when_no_endpoint(self):
        """Assert ``hostname`` is ``None`` when endpoint is not set."""
        pmm = PMMSettings()
        assert pmm.hostname is None

    def test_api_key_masked_in_repr(self):
        """Assert ``api_key`` is masked in repr output."""
        pmm = PMMSettings(api_key="my-secret-key")
        assert "my-secret-key" not in repr(pmm)

    def test_api_key_accepts_secretstr(self):
        """Assert ``PMMSettings`` accepts a string for ``api_key``."""
        pmm = PMMSettings(api_key="test-key")
        assert pmm.api_key.get_secret_value() == "test-key"

    def test_lowercases_yaml_keys(self):
        """Assert uppercase YAML keys are normalized to lowercase."""
        pmm = PMMSettings.model_validate(
            {
                "ENDPOINT": "https://pmm.example.com",
                "VERIFY_SSL": False,
                "ANNOTATIONS_TIMEOUT": CUSTOM_ANNOTATIONS_TIMEOUT,
            }
        )
        assert pmm.endpoint == "https://pmm.example.com"
        assert pmm.verify_ssl is False
        assert pmm.annotations_timeout == CUSTOM_ANNOTATIONS_TIMEOUT

    def test_ignores_extra_fields(self):
        """Assert extra fields are silently ignored."""
        pmm = PMMSettings.model_validate(
            {"endpoint": "https://pmm.example.com", "unknown_field": "value"}
        )
        assert pmm.endpoint == "https://pmm.example.com"
        assert not hasattr(pmm, "unknown_field")

    def test_frontend_defaults_to_endpoint(self):
        """Assert ``frontend`` defaults to ``endpoint`` when not explicitly set."""
        pmm = PMMSettings(endpoint="https://pmm.example.com")
        assert pmm.frontend == "https://pmm.example.com"

    def test_frontend_not_overridden_when_set(self):
        """Assert ``frontend`` is not overridden when explicitly set."""
        pmm = PMMSettings(
            endpoint="https://pmm.example.com",
            frontend="https://pmm-frontend.example.com",
        )
        assert pmm.frontend == "https://pmm-frontend.example.com"

    def test_frontend_stays_none_when_no_endpoint(self):
        """Assert ``frontend`` stays ``None`` when endpoint is also ``None``."""
        pmm = PMMSettings()
        assert pmm.frontend is None

    def test_settings_has_pmm_field(self):
        """Assert the global ``Settings`` class has a ``PMM`` field."""
        assert "PMM" in Settings.model_fields
        assert Settings.model_fields["PMM"].default == PMMSettings()

    def test_endpoint_rejects_redacted_mask_on_yaml_path(self) -> None:
        """Fail when a masked export is re-fed as the PMM endpoint."""
        with pytest.raises(ValidationError, match="endpoint"):
            PMMSettings(endpoint="https://pmm-user:****@pmm.example.com:8443")

    def test_endpoint_rejects_redacted_mask_on_env_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail Settings load when a masked PMM endpoint arrives via the environment."""
        monkeypatch.setenv(
            "PMM__ENDPOINT", "https://pmm-user:****@pmm.example.com:8443"
        )
        with pytest.raises(ValidationError, match="endpoint"):
            Settings(_env_file=None)


def test_settings_secret_key_is_secretstr():
    """Test that SECRET_KEY is a SecretStr instance and masked in repr."""
    assert isinstance(settings.SECRET_KEY, SecretStr)
    secret_value = settings.SECRET_KEY.get_secret_value()
    if secret_value:
        assert secret_value not in repr(settings.SECRET_KEY)


def test_create_app_default_docs_urls():
    """``create_app`` without docs kwargs preserves FastAPI's default docs URLs."""
    app = create_app()
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"


def test_create_app_disables_docs_when_none():
    """``create_app`` forwards ``docs_url=None`` and ``redoc_url=None`` to FastAPI."""
    app = create_app(docs_url=None, redoc_url=None)
    assert app.docs_url is None
    assert app.redoc_url is None


def test_create_app_custom_docs_url():
    """``create_app`` forwards a custom ``docs_url`` to FastAPI."""
    app = create_app(docs_url="/custom-docs", redoc_url="/custom-redoc")
    assert app.docs_url == "/custom-docs"
    assert app.redoc_url == "/custom-redoc"


def test_create_app_defaults_to_no_root_path():
    """``create_app`` without a prefix leaves ``root_path`` empty."""
    assert create_app().root_path == ""


def test_create_app_forwards_root_path():
    """``create_app`` forwards ``root_path`` to FastAPI."""
    assert create_app(root_path="/extensions").root_path == "/extensions"


class TestDeriveInternalToken:
    """Cover EXTENSIONS_INTERNAL_TOKEN derivation from SECRET_KEY."""

    @staticmethod
    def _expected_token(secret_key: str) -> str:
        return hmac.new(
            secret_key.encode(), b"extensions-internal-token", hashlib.sha256
        ).hexdigest()

    def test_explicit_token_takes_precedence(self):
        """An explicitly configured token is used verbatim, never derived."""
        instance = Settings(
            SECRET_KEY=SecretStr("any-secret"),
            EXTENSIONS_INTERNAL_TOKEN=SecretStr("explicit-token"),
        )
        assert instance.EXTENSIONS_INTERNAL_TOKEN.get_secret_value() == "explicit-token"

    def test_derives_from_secret_key_when_unset(self):
        """An unset token is derived as HMAC-SHA256 over the secret key."""
        instance = Settings(
            SECRET_KEY=SecretStr("derivation-secret"),
            EXTENSIONS_INTERNAL_TOKEN=None,
        )
        assert (
            instance.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
            == self._expected_token("derivation-secret")
        )

    def test_empty_token_is_derived(self):
        """An empty-string token is treated as unset and derived."""
        instance = Settings(
            SECRET_KEY=SecretStr("derivation-secret"),
            EXTENSIONS_INTERNAL_TOKEN=SecretStr(""),
        )
        assert (
            instance.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
            == self._expected_token("derivation-secret")
        )

    def test_derivation_is_identical_across_instances(self):
        """Two settings sharing a secret key derive the identical token."""
        first = Settings(
            SECRET_KEY=SecretStr("shared-secret"), EXTENSIONS_INTERNAL_TOKEN=None
        )
        second = Settings(
            SECRET_KEY=SecretStr("shared-secret"), EXTENSIONS_INTERNAL_TOKEN=None
        )
        assert (
            first.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
            == second.EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
        )

    def test_empty_secret_key_without_token_raises(self):
        """An empty secret key with no explicit token fails fast at construction."""
        with pytest.raises(ValidationError, match="SECRET_KEY must be set"):
            Settings(SECRET_KEY=SecretStr(""), EXTENSIONS_INTERNAL_TOKEN=None)

    def test_token_resolves_from_environment(self, monkeypatch):
        """Resolve an explicit token from the canonical environment variable.

        :param monkeypatch: The environment patcher.
        """
        monkeypatch.setenv("EXTENSIONS_INTERNAL_TOKEN", "environment-token")

        assert (
            Settings().EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
            == "environment-token"
        )

    def test_token_resolves_from_dotenv(self, tmp_path, monkeypatch):
        """Resolve an explicit token from a dotenv entry.

        :param tmp_path: The directory holding the dotenv file.
        :param monkeypatch: The environment patcher.
        """
        monkeypatch.delenv("EXTENSIONS_INTERNAL_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "EXTENSIONS_INTERNAL_TOKEN=dotenv-token\n", encoding="utf-8"
        )

        instance = Settings(_env_file=env_file)

        assert instance.EXTENSIONS_INTERNAL_TOKEN.get_secret_value() == "dotenv-token"

    def test_token_resolves_from_secret_file(self, tmp_path, monkeypatch):
        """Resolve an explicit token from a file mounted under ``SECRETS_DIR``.

        :param tmp_path: The directory mounted as ``SECRETS_DIR``.
        :param monkeypatch: The environment patcher.
        """
        monkeypatch.delenv("EXTENSIONS_INTERNAL_TOKEN", raising=False)
        (tmp_path / "EXTENSIONS_INTERNAL_TOKEN").write_text(
            "file-token\n", encoding="utf-8"
        )

        instance = Settings(_secrets_dir=tmp_path)

        assert instance.EXTENSIONS_INTERNAL_TOKEN.get_secret_value() == "file-token"

    def test_token_resolves_from_yaml_profile(self, tmp_path, monkeypatch):
        """Resolve an explicit token from the YAML profile block.

        :param tmp_path: The directory holding the profile.
        :param monkeypatch: The working-directory and environment patcher.
        """
        monkeypatch.delenv("EXTENSIONS_INTERNAL_TOKEN", raising=False)
        _use_profile(
            tmp_path,
            monkeypatch,
            CELERY_PROFILE_BLOCK + "  EXTENSIONS_INTERNAL_TOKEN: yaml-token\n",
        )

        assert Settings().EXTENSIONS_INTERNAL_TOKEN.get_secret_value() == "yaml-token"

    def test_token_is_masked(self):
        """Mask the token everywhere a settings dump could carry it."""
        instance = Settings(EXTENSIONS_INTERNAL_TOKEN=SecretStr("masked-token"))
        dumps = [
            repr(instance),
            str(instance.model_dump()),
            instance.model_dump_json(),
        ]

        assert (
            instance.model_dump()["EXTENSIONS_INTERNAL_TOKEN"].get_secret_value()
            == "masked-token"
        )
        assert not [dump for dump in dumps if "masked-token" in dump]


class TestEncryptionKey:
    """Cover ENCRYPTION_KEY validation and the absence of any committed key."""

    def test_unset_key_fails(self, monkeypatch):
        """Reject an absent key with the actionable remediation message.

        The suite-wide key is cleared so no channel supplies one, which is the
        state a fresh checkout starts in and the case the message exists for.

        :param monkeypatch: The environment patcher.
        """
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

        with pytest.raises(ValidationError, match="ENCRYPTION_KEY must be set"):
            Settings()

    def test_empty_key_fails(self):
        """Reject an empty key, which reads as configured but cannot encrypt."""
        with pytest.raises(ValidationError, match="ENCRYPTION_KEY must be set"):
            Settings(ENCRYPTION_KEY=SecretStr(""))

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-fernet-key",
            secrets.token_hex(32),
            base64.urlsafe_b64encode(b"x" * 16).decode("ascii"),
        ],
        ids=["garbage", "openssl-rand-hex-32", "sixteen-byte-key"],
    )
    def test_malformed_key_fails(self, value):
        """Reject a value Fernet cannot build a cipher from.

        ``openssl rand -hex 32`` is covered explicitly: it is the remediation
        ``SECRET_KEY`` documents and it yields 64 characters Fernet refuses, so
        this case pins the remediation text against regressing to ``-hex``.
        """
        with pytest.raises(ValidationError, match="ENCRYPTION_KEY must be set"):
            Settings(ENCRYPTION_KEY=SecretStr(value))

    def test_valid_key_accepted(self):
        """Accept a freshly generated Fernet key and keep it wrapped."""
        key = Fernet.generate_key().decode("ascii")

        instance = Settings(ENCRYPTION_KEY=SecretStr(key))

        assert isinstance(instance.ENCRYPTION_KEY, SecretStr)
        assert instance.ENCRYPTION_KEY.get_secret_value() == key

    def test_no_key_is_committed(self):
        """Reject a key resolved from the committed profile, whatever the environment.

        The values this key protects are real credentials, and the repository is
        public, so a committed key would be readable by anyone. Every
        environment supplies its own; nothing ships one.
        """
        profile = yaml.safe_load(
            (BASE_DIR / "settings.yaml").read_text(encoding="utf-8")
        )

        assert not [
            name for name, block in profile.items() if "ENCRYPTION_KEY" in block
        ]

    def test_key_is_masked(self):
        """Mask the key everywhere a settings dump could carry it."""
        key = Fernet.generate_key().decode("ascii")

        instance = Settings(ENCRYPTION_KEY=SecretStr(key))
        dumps = [
            repr(instance.ENCRYPTION_KEY),
            repr(instance),
            str(instance.model_dump()),
            instance.model_dump_json(),
        ]

        assert all(dumps)
        assert not [dump for dump in dumps if key in dump]

    def test_key_resolves_from_secret_file(self, tmp_path, monkeypatch):
        """Resolve the key from a mounted file named after the canonical variable.

        The suite-wide key is cleared first because an environment variable
        outranks a secret file, so leaving it set would test the environment
        channel a second time rather than the file one.

        :param tmp_path: The directory mounted as ``SECRETS_DIR``.
        :param monkeypatch: The environment patcher.
        """
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        key = Fernet.generate_key().decode("ascii")
        (tmp_path / "ENCRYPTION_KEY").write_text(f"{key}\n", encoding="utf-8")

        instance = Settings(_secrets_dir=tmp_path)

        assert instance.ENCRYPTION_KEY.get_secret_value() == key

    def test_key_is_not_overridable(self):
        """Classify the key as not overridable, so the DB layer cannot rewrite it.

        The settings-override layer is itself a consumer of this key, so a key
        the override API could rewrite would orphan every row it had written.
        """
        classification = field_reload_classification(
            Settings.model_fields["ENCRYPTION_KEY"],
            owner_cls=Settings,
            field_name="ENCRYPTION_KEY",
        )

        assert classification is ReloadClassification.NOT_OVERRIDABLE


SECRET_FILE_MATRIX = [
    pytest.param(
        Settings,
        "SECRET_KEY",
        "matrix-secret-key",
        "matrix-secret-key",
        lambda s: s.SECRET_KEY.get_secret_value(),
        id="Settings",
    ),
    pytest.param(
        AlertSettings,
        "ALERTING__SOURCE_PREFIX",
        "matrix-prefix",
        "matrix-prefix",
        lambda s: s.SOURCE_PREFIX,
        id="AlertSettings",
    ),
    pytest.param(
        AuthSettings,
        "AUTH__PROVIDER__CASDOOR__CLIENT_SECRET",
        "matrix-client-secret",
        "matrix-client-secret",
        lambda s: s.PROVIDER["casdoor"].client_secret.get_secret_value(),
        id="AuthSettings",
    ),
    pytest.param(
        InventorySettings,
        "INVENTORY__DATABASE__PASSWORD",
        "matrix-inventory-pw",
        "matrix-inventory-pw",
        lambda s: s.DATABASE.PASSWORD.get_secret_value(),
        id="InventorySettings",
    ),
    pytest.param(
        ExtensionsSettings,
        "EXTENSIONS__DATABASE__PASSWORD",
        "matrix-extensions-pw",
        "matrix-extensions-pw",
        lambda s: s.DATABASE.PASSWORD.get_secret_value(),
        id="ExtensionsSettings",
    ),
    pytest.param(
        AlertsSettings,
        "EXTENSIONS__ALERTS__ALERT_FOLDER_NAME",
        "Matrix Folder",
        "Matrix Folder",
        lambda s: s.ALERT_FOLDER_NAME,
        id="AlertsSettings",
    ),
    pytest.param(
        HealthReportSettings,
        "EXTENSIONS__HEALTH_REPORT__API_KEY",
        "matrix-health-report-key",
        "matrix-health-report-key",
        lambda s: s.api_key.get_secret_value(),
        id="HealthReportSettings",
    ),
    pytest.param(
        AtwSettings,
        "EXTENSIONS__ATW__BUNDLE_TTL",
        "77",
        77,
        lambda s: s.bundle_ttl,
        id="AtwSettings",
    ),
    pytest.param(
        SnippetsSettings,
        "EXTENSIONS__SNIPPETS__PREVIEW_MAX_CHARS",
        "123",
        123,
        lambda s: s.PREVIEW_MAX_CHARS,
        id="SnippetsSettings",
    ),
    pytest.param(
        TasksSettings,
        "TASKS__DATABASE__PASSWORD",
        "matrix-tasks-pw",
        "matrix-tasks-pw",
        lambda s: s.DATABASE.PASSWORD.get_secret_value(),
        id="TasksSettings",
    ),
    pytest.param(
        AnonymizerSettings,
        "TASKS__ANONYMIZER__NLP_MODELS",
        '{"en": "matrix_model"}',
        "matrix_model",
        lambda s: s.NLP_MODELS["en"],
        id="AnonymizerSettings",
    ),
]


@pytest.fixture
def aliased_settings_class():
    """Build a prefixed settings class whose field carries a validation alias.

    ``ExtensionsSettings.APPS`` declares ``AliasChoices("APPS", "PLUGINS")``. An aliased
    field is looked up without ``env_prefix`` unless ``env_prefix_target`` opts in,
    so the source's native ``secrets_prefix`` would silently drop its secret file;
    the key-rewriting loop this class exercises is what makes it resolve.
    """

    class AliasedSettings(BaseYamlAppSettings):
        SETTINGS_PREFIXES = ["EXTENSIONS"]
        APPS: list[str] = Field(
            default_factory=list,
            validation_alias=AliasChoices("APPS", "PLUGINS"),
        )

    return AliasedSettings


def test_unprefixed_class_reads_secret_file(tmp_path):
    """Resolve an unprefixed setting from a file named after its canonical variable."""
    (tmp_path / "SECRET_KEY").write_text("from-file\n", encoding="utf-8")

    instance = Settings(_secrets_dir=tmp_path)

    assert instance.SECRET_KEY.get_secret_value() == "from-file"


def test_prefixed_class_reads_nested_secret_file(tmp_path):
    """Resolve a nested setting on a prefixed class from its canonical file name."""
    (tmp_path / "INVENTORY__DATABASE__PASSWORD").write_text(
        "inventory-pw", encoding="utf-8"
    )

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.DATABASE.PASSWORD.get_secret_value() == "inventory-pw"


def test_aliased_field_resolves_from_secret_file(tmp_path, aliased_settings_class):
    """Resolve a prefixed secret file for a field declaring a validation alias."""
    (tmp_path / "EXTENSIONS__APPS").write_text('["alerts"]', encoding="utf-8")

    instance = aliased_settings_class(_secrets_dir=tmp_path)

    assert instance.APPS == ["alerts"]


def test_extensions_settings_reads_aliased_secret_file(tmp_path):
    """Resolve ``EXTENSIONS__APPS`` from a file through the production override of the hook."""
    (tmp_path / "EXTENSIONS__APPS").write_text(
        '[{"module_name": "tasks", "uri_path": "/tasks"}]', encoding="utf-8"
    )

    instance = ExtensionsSettings(_secrets_dir=tmp_path)

    assert [app.module_name for app in instance.APPS] == ["app.extensions.apps.tasks"]


def test_file_backed_legacy_plugins_key_does_not_warn(tmp_path):
    """Populate the app list from an ``EXTENSIONS__PLUGINS`` file without deprecating it.

    ``ExtensionsSettings.settings_customise_sources`` inspects only the environment and
    dotenv sources, so the file-backed spelling is outside the deprecation
    contract even though the alias still resolves it.
    """
    (tmp_path / "EXTENSIONS__PLUGINS").write_text(
        '[{"module_name": "tasks", "uri_path": "/tasks"}]', encoding="utf-8"
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        instance = ExtensionsSettings(_secrets_dir=tmp_path)

    assert [app.module_name for app in instance.APPS] == ["app.extensions.apps.tasks"]
    assert not [w for w in caught if "PLUGINS" in str(w.message)]


def test_env_var_beats_secret_file(tmp_path, monkeypatch):
    """Prefer an environment variable over a same-named secret file."""
    (tmp_path / "INVENTORY__DATABASE__PASSWORD").write_text(
        "from-file", encoding="utf-8"
    )
    monkeypatch.setenv("INVENTORY__DATABASE__PASSWORD", "from-env")

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.DATABASE.PASSWORD.get_secret_value() == "from-env"


def test_dotenv_entry_beats_secret_file(tmp_path):
    """Prefer a dotenv entry over a same-named secret file."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "INVENTORY__DATABASE__PASSWORD").write_text(
        "from-file", encoding="utf-8"
    )
    env_file = tmp_path / "dotenv"
    env_file.write_text("INVENTORY__DATABASE__PASSWORD=from-dotenv\n", encoding="utf-8")

    instance = InventorySettings(_secrets_dir=secrets_dir, _env_file=env_file)

    assert instance.DATABASE.PASSWORD.get_secret_value() == "from-dotenv"


def test_secret_file_beats_yaml_profile(tmp_path):
    """Prefer a secret file over the value the baked YAML profile supplies."""
    baseline = Settings()
    (tmp_path / "SECRET_KEY").write_text("from-file", encoding="utf-8")

    instance = Settings(_secrets_dir=tmp_path)

    assert baseline.SECRET_KEY.get_secret_value() != "from-file"
    assert instance.SECRET_KEY.get_secret_value() == "from-file"


def test_full_precedence_chain(tmp_path, monkeypatch):
    """Resolve one key at every level of the chain, each beating the ones after it."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "INVENTORY__DATABASE__PASSWORD").write_text(
        "from-file", encoding="utf-8"
    )
    env_file = tmp_path / "dotenv"
    env_file.write_text("INVENTORY__DATABASE__PASSWORD=from-dotenv\n", encoding="utf-8")

    def build(**kwargs):
        return InventorySettings(
            _secrets_dir=secrets_dir, _env_file=env_file, **kwargs
        ).DATABASE.PASSWORD.get_secret_value()

    monkeypatch.setenv("INVENTORY__DATABASE__PASSWORD", "from-env")
    assert build(DATABASE={"PASSWORD": "from-init"}) == "from-init"
    assert build() == "from-env"

    monkeypatch.delenv("INVENTORY__DATABASE__PASSWORD")
    assert build() == "from-dotenv"

    env_file.write_text("", encoding="utf-8")
    assert build() == "from-file"


def test_unset_secrets_dir_resolves_as_before():
    """Resolve every setting unchanged, and warn about nothing, with the knob unset."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        instance = Settings()

    assert (
        instance.SECRET_KEY.get_secret_value()
        == Settings().SECRET_KEY.get_secret_value()
    )
    assert not caught


def test_empty_secrets_dir_env_value_coerces_to_none(monkeypatch):
    """Coerce an empty ``SECRETS_DIR`` to ``None`` so the source never walks the CWD."""
    monkeypatch.setenv("SECRETS_DIR", "")

    assert PreEnvSettings().SECRETS_DIR is None


def test_missing_secrets_dir_warns_and_degrades(tmp_path):
    """Degrade to the profile value with one warning when the directory is absent."""
    absent = tmp_path / "absent"

    with pytest.warns(UserWarning, match="does not exist"):
        instance = Settings(_secrets_dir=absent)

    assert (
        instance.SECRET_KEY.get_secret_value()
        == Settings().SECRET_KEY.get_secret_value()
    )


def test_secrets_dir_without_matching_file_resolves_defaults(tmp_path):
    """Fall through to the profile when the directory holds no matching file."""
    (tmp_path / "UNRELATED_KEY").write_text("ignored", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        instance = Settings(_secrets_dir=tmp_path)

    assert (
        instance.SECRET_KEY.get_secret_value()
        == Settings().SECRET_KEY.get_secret_value()
    )
    assert not caught


def test_empty_secret_file_overrides_profile_value(tmp_path):
    """Resolve an empty secret file to ``""`` rather than falling back to the profile."""
    (tmp_path / "INVENTORY__UVICORN_HOST").write_text("", encoding="utf-8")

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.UVICORN_HOST == ""


def test_secret_file_content_is_stripped(tmp_path):
    """Strip surrounding whitespace so ``openssl rand -hex 32 > FILE`` works as written."""
    (tmp_path / "INVENTORY__UVICORN_HOST").write_text("  padded  \n", encoding="utf-8")

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.UVICORN_HOST == "padded"


def test_secrets_dir_pointing_at_a_file_fails_fast(tmp_path):
    """Reject a ``SECRETS_DIR`` that names a file instead of a directory."""
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("content", encoding="utf-8")

    with pytest.raises(SettingsError, match="must reference a directory"):
        Settings(_secrets_dir=not_a_dir)


def test_oversized_secrets_dir_fails_fast(tmp_path):
    """Reject a secrets directory whose contents exceed the size ceiling."""
    (tmp_path / "OVERSIZED").write_bytes(b"x" * (16 * 2**20 + 1))

    with pytest.raises(SettingsError, match="size is above"):
        Settings(_secrets_dir=tmp_path)


def test_secret_in_subdirectory_is_ignored(tmp_path):
    """Ignore a secret nested in a subdirectory, whose key can match no field."""
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "SECRET_KEY").write_text("nested-value", encoding="utf-8")

    instance = Settings(_secrets_dir=tmp_path)

    assert instance.SECRET_KEY.get_secret_value() != "nested-value"


def test_symlink_escaping_secrets_dir_is_ignored(tmp_path):
    """Ignore a symlink whose target resolves outside the secrets directory."""
    outside = tmp_path / "outside"
    outside.write_text("escaped-value", encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "SECRET_KEY").symlink_to(outside)

    instance = Settings(_secrets_dir=secrets_dir)

    assert instance.SECRET_KEY.get_secret_value() != "escaped-value"


def test_kubernetes_projected_secret_resolves(tmp_path):
    """Resolve a secret through the ``..data`` symlink layout Kubernetes projects."""
    data_dir = tmp_path / "..data"
    data_dir.mkdir()
    (data_dir / "SECRET_KEY").write_text("projected-value", encoding="utf-8")
    (tmp_path / "SECRET_KEY").symlink_to(data_dir / "SECRET_KEY")

    instance = Settings(_secrets_dir=tmp_path)

    assert instance.SECRET_KEY.get_secret_value() == "projected-value"


def test_unprefixed_file_resolves_for_prefixed_class(tmp_path):
    """Resolve an unprefixed secret file for a prefixed class, as its env twin does."""
    (tmp_path / "DATABASE__PASSWORD").write_text("bare-pw", encoding="utf-8")

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.DATABASE.PASSWORD.get_secret_value() == "bare-pw"


def test_foreign_prefix_file_does_not_resolve(tmp_path):
    """Leave a class untouched by a secret file spelled with another class's prefix."""
    (tmp_path / "EXTENSIONS__DATABASE__PASSWORD").write_text(
        "extensions-pw", encoding="utf-8"
    )

    instance = InventorySettings(_secrets_dir=tmp_path)

    assert instance.DATABASE.PASSWORD is None


@pytest.mark.parametrize(
    "env_order",
    [
        ("DATABASE__PASSWORD", "EXTENSIONS__DATABASE__PASSWORD"),
        ("EXTENSIONS__DATABASE__PASSWORD", "DATABASE__PASSWORD"),
    ],
    ids=["global-first", "per-service-first"],
)
def test_per_service_env_beats_global_env(monkeypatch, env_order):
    """Prefer a per-service environment variable over the global spelling."""
    values = {
        "DATABASE__PASSWORD": "globalpw",
        "EXTENSIONS__DATABASE__PASSWORD": "seppw",
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)
    for name in env_order:
        monkeypatch.setenv(name, values[name])

    assert ExtensionsSettings().DATABASE.PASSWORD.get_secret_value() == "seppw"


def test_shipped_profile_resolves_the_bounded_pool_defaults():
    """Assert the shipped profile leaves the bounded pool sizing in force.

    The profile's own ``DATABASE`` blocks set no sizing, so each service
    takes this class's defaults; its SQLite engine still emits none of them.
    """
    for settings_cls in (ExtensionsSettings, InventorySettings, TasksSettings):
        database = settings_cls().DATABASE
        assert (database.POOL_SIZE, database.MAX_OVERFLOW, database.POOL_TIMEOUT) == (
            3,
            2,
            10.0,
        )
        assert database.pool_engine_kwargs == {"pool_pre_ping": True}


@pytest.mark.parametrize(
    "dotenv_lines",
    [
        "DATABASE__PASSWORD=globalpw\nEXTENSIONS__DATABASE__PASSWORD=seppw\n",
        "EXTENSIONS__DATABASE__PASSWORD=seppw\nDATABASE__PASSWORD=globalpw\n",
    ],
    ids=["global-first", "per-service-first"],
)
def test_per_service_dotenv_beats_global_dotenv(tmp_path, dotenv_lines):
    """Prefer a per-service dotenv entry over the global spelling."""
    env_file = tmp_path / "dotenv"
    env_file.write_text(dotenv_lines, encoding="utf-8")

    assert (
        ExtensionsSettings(_env_file=env_file).DATABASE.PASSWORD.get_secret_value()
        == "seppw"
    )


def test_per_service_secret_file_beats_global_secret_file(tmp_path):
    """Prefer a per-service secret file over the global spelling."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "DATABASE__PASSWORD").write_text("globalpw", encoding="utf-8")
    (secrets_dir / "EXTENSIONS__DATABASE__PASSWORD").write_text(
        "seppw", encoding="utf-8"
    )

    assert (
        ExtensionsSettings(
            _secrets_dir=secrets_dir
        ).DATABASE.PASSWORD.get_secret_value()
        == "seppw"
    )


@pytest.mark.parametrize(
    "settings_cls",
    [ExtensionsSettings, InventorySettings, TasksSettings],
)
def test_global_database_password_resolves_for_every_service(tmp_path, settings_cls):
    """Resolve one global password file for every service when none overrides it."""
    (tmp_path / "DATABASE__PASSWORD").write_text("shared-pw", encoding="utf-8")

    assert (
        settings_cls(_secrets_dir=tmp_path).DATABASE.PASSWORD.get_secret_value()
        == "shared-pw"
    )


@pytest.mark.parametrize(
    ("settings_cls", "filename", "content", "expected", "read"), SECRET_FILE_MATRIX
)
def test_every_settings_class_reads_its_own_secret_file(
    tmp_path, monkeypatch, settings_cls, filename, content, expected, read
):
    """Resolve one secret file per concrete settings class, spelled with its own prefix."""
    monkeypatch.delenv(filename, raising=False)
    (tmp_path / filename).write_text(f"{content}\n", encoding="utf-8")

    assert read(settings_cls(_secrets_dir=tmp_path)) == expected


def test_grafana_token_file_maps_to_its_nested_field_path(tmp_path):
    """Rewrite the Grafana service-account-token file onto its nested field path.

    ``AuthSettings`` accepts exactly one provider and the shipped profile supplies
    Casdoor, so a Grafana-only instance cannot be built here — that invariant is
    identical for the variable's environment twin. What this ticket owns is the
    key the source hands the model, which is what this pins.
    """
    filename = "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN"
    (tmp_path / filename).write_text("glsa_from_file\n", encoding="utf-8")

    sources = AuthSettings.settings_customise_sources(
        AuthSettings,
        InitSettingsSource(AuthSettings, init_kwargs={}),
        EnvSettingsSource(AuthSettings),
        DotEnvSettingsSource(AuthSettings, env_file=None),
        SecretsSettingsSource(AuthSettings, secrets_dir=tmp_path),
    )
    secret_source = next(
        source for source in sources if isinstance(source, NestedSecretsSettingsSource)
    )

    assert secret_source.env_vars == {
        "provider__grafana__service_account_token": "glsa_from_file"
    }


def _yaml_source_for(settings_cls, secrets_dir):
    """Return the YAML source ``settings_customise_sources`` builds for a secrets dir."""
    sources = settings_cls.settings_customise_sources(
        settings_cls,
        InitSettingsSource(settings_cls, init_kwargs={}),
        EnvSettingsSource(settings_cls),
        DotEnvSettingsSource(settings_cls, env_file=None),
        SecretsSettingsSource(settings_cls, secrets_dir=secrets_dir),
    )
    return next(
        source
        for source in sources
        if isinstance(source, YamlPrefixConfigSettingsSource)
    )


def test_secret_file_selects_the_yaml_profile(tmp_path, monkeypatch):
    """Select the YAML profile block a file-supplied ``FASTAPI_ENV`` names.

    ``FASTAPI_ENV`` both populates a field and picks the profile block. Reading it
    from only the environment and dotenv would let the two disagree — the settings
    reporting one environment while the values came from another's block.
    """
    (tmp_path / "FASTAPI_ENV").write_text("production_docker\n", encoding="utf-8")

    from_file = _yaml_source_for(Settings, tmp_path)

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("FASTAPI_ENV", "production_docker")
    from_env = _yaml_source_for(Settings, empty)

    assert from_file.yaml_data == from_env.yaml_data


def test_env_var_beats_secret_file_for_the_yaml_profile(tmp_path, monkeypatch):
    """Prefer the environment over a secret file when selecting the profile block."""
    (tmp_path / "FASTAPI_ENV").write_text("production_docker\n", encoding="utf-8")
    monkeypatch.setenv("FASTAPI_ENV", "development")

    empty = tmp_path / "empty"
    empty.mkdir()
    from_env = _yaml_source_for(Settings, tmp_path)
    baseline = _yaml_source_for(Settings, empty)

    assert from_env.yaml_data == baseline.yaml_data


CELERY_PROFILE_BLOCK = "  CELERY:\n    BROKER_URL: redis://127.0.0.1:6379/0\n"
"""A minimal ``CELERY`` block, since ``Settings.CELERY`` carries no default."""

EXTENSIONS_POSTGRES_PROFILE_BLOCK = """\
  EXTENSIONS:
    DATABASE:
      ENGINE: postgresql
      HOST: pmm-server
      NAME: pmm_extensions
      PORT: 5432
      USER: pmm_extensions
"""
"""The embedded profile's Extensions service database block, which the beat store derives from."""

EXTENSIONS_SQLITE_PROFILE_BLOCK = (
    "  EXTENSIONS:\n    DATABASE:\n      NAME: extensions.db\n"
)
"""A SQLite Extensions service database block, whose derived store nulls ``beat_schema``."""

DERIVED_BEAT_DBURI = (
    "postgresql+psycopg2://pmm_extensions@pmm-server:5432/pmm_extensions"
)
"""The store :data:`EXTENSIONS_POSTGRES_PROFILE_BLOCK` derives with no password supplied."""


def _use_profile(tmp_path, monkeypatch, body):
    """Write a settings profile into ``tmp_path`` and make it the process's profile.

    ``PreEnvSettings.SETTINGS_FILE`` is the relative ``settings.yaml``, reopened per
    instantiation, so the copy in the working directory is what a freshly
    constructed class reads.

    :param tmp_path: The directory to write the profile into.
    :param monkeypatch: The working-directory patcher.
    :param body: The ``default:`` block's body, already indented.
    """
    (tmp_path / "settings.yaml").write_text(f"default:\n{body}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def _mounted_secrets(tmp_path, **files):
    """Write the named secret files into a subdirectory and return it.

    The subdirectory keeps the profile out of the mounted set, since both live in
    the same temporary directory.

    :param tmp_path: The per-test temporary directory.
    :param files: Each canonical name a file supplies, mapped to its contents.
    :return: The directory to pass as ``_secrets_dir``.
    """
    directory = tmp_path / "secrets"
    directory.mkdir(exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return directory


class TestDerivedBeatStoreDefault:
    """Cover the beat-store URI ``Settings`` derives from the PMM Extensions database."""

    @pytest.fixture
    def _postgres_profile(self, tmp_path, monkeypatch):
        """Install the PostgreSQL PMM Extensions profile the derivation cases start from.

        The cases that vary the profile call ``_use_profile`` themselves, which
        overwrites this one, so requesting the fixture is what marks a case as
        using the default arrangement.
        """
        _use_profile(
            tmp_path,
            monkeypatch,
            CELERY_PROFILE_BLOCK + EXTENSIONS_POSTGRES_PROFILE_BLOCK,
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_derives_the_store_from_the_extensions_database(self):
        """Resolve the beat store from the PMM Extensions database when nothing configures it."""
        assert Settings().CELERY.beat_dburi == DERIVED_BEAT_DBURI

    @pytest.mark.usefixtures("_postgres_profile")
    def test_environment_variable_outranks_the_derived_store(self, monkeypatch):
        """Prefer an explicit ``CELERY__BEAT_DBURI`` set in the environment."""
        monkeypatch.setenv("CELERY__BEAT_DBURI", "postgresql://envwins@host:5432/beat")

        assert (
            Settings().CELERY.beat_dburi
            == "postgresql+psycopg2://envwins@host:5432/beat"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_an_init_keyword_outranks_the_environment(self, monkeypatch):
        """Prefer a ``CELERY`` init keyword, the highest-priority settings source.

        The keyword and the environment name the store in different key cases, so
        a derived value contributed unconditionally would seed the keyword's case
        ahead of the environment's and hand the fold to the lower-ranked source.
        """
        monkeypatch.setenv("CELERY__BEAT_DBURI", "postgresql://envloses@host:5432/beat")

        resolved = Settings(
            CELERY={
                "BROKER_URL": "redis://127.0.0.1:6379/0",
                "BEAT_DBURI": "postgresql://initwins@host:5432/beat",
            }
        )

        assert (
            resolved.CELERY.beat_dburi
            == "postgresql+psycopg2://initwins@host:5432/beat"
        )

    def test_yaml_profile_outranks_the_derived_store(self, tmp_path, monkeypatch):
        """Prefer a profile's own ``BEAT_DBURI``, which is a configured value."""
        _use_profile(
            tmp_path,
            monkeypatch,
            CELERY_PROFILE_BLOCK
            + "    BEAT_DBURI: postgresql://profile@elsewhere:5432/beat\n"
            + EXTENSIONS_POSTGRES_PROFILE_BLOCK,
        )

        assert (
            Settings().CELERY.beat_dburi
            == "postgresql+psycopg2://profile@elsewhere:5432/beat"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_secret_file_outranks_the_derived_store(self, tmp_path):
        """Prefer a mounted ``CELERY__BEAT_DBURI``, which targets a separate store."""
        secrets_dir = _mounted_secrets(
            tmp_path, CELERY__BEAT_DBURI="postgresql://mounted:y@beatstore:5432/beat"
        )

        assert (
            Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi
            == "postgresql+psycopg2://mounted:y@beatstore:5432/beat"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_mounted_password_reaches_the_derived_store(self, tmp_path):
        """Carry a mounted password into the store without exporting it anywhere."""
        secrets_dir = _mounted_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="pw")

        assert (
            Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi
            == "postgresql+psycopg2://pmm_extensions:pw@pmm-server:5432/pmm_extensions"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_global_password_reaches_the_derived_store(self, tmp_path):
        """Carry a global mounted password into the derived beat store."""
        secrets_dir = _mounted_secrets(tmp_path, DATABASE__PASSWORD="pw")

        assert (
            Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi
            == "postgresql+psycopg2://pmm_extensions:pw@pmm-server:5432/pmm_extensions"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_reserved_password_characters_are_percent_encoded(self, tmp_path):
        """Encode a password carrying URI syntax, which would corrupt the authority."""
        secrets_dir = _mounted_secrets(
            tmp_path, EXTENSIONS__DATABASE__PASSWORD="p@ss:w/rd"
        )

        assert (
            Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi
            == "postgresql+psycopg2://pmm_extensions:p%40ss%3Aw%2Frd@pmm-server:5432/pmm_extensions"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_an_empty_password_file_yields_a_passwordless_store(self, tmp_path):
        """Omit the password entirely for a blank mount, as an empty secret is falsy."""
        secrets_dir = _mounted_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="")

        assert Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi == (
            DERIVED_BEAT_DBURI
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_dotenv_password_reaches_the_derived_store(self, tmp_path):
        """Read the password from the caller's ``_env_file``, which the probe inherits."""
        env_file = tmp_path / "dotenv"
        env_file.write_text(
            "EXTENSIONS__DATABASE__PASSWORD=from-dotenv\n", encoding="utf-8"
        )

        assert (
            Settings(_env_file=env_file).CELERY.beat_dburi
            == "postgresql+psycopg2://pmm_extensions:from-dotenv@pmm-server:5432/pmm_extensions"
        )

    @pytest.mark.usefixtures("_postgres_profile")
    def test_a_suppressed_dotenv_still_resolves_the_derived_store(self):
        """Propagate ``_env_file=None`` to the probe, which then reads no dotenv."""
        assert Settings(_env_file=None).CELERY.beat_dburi == DERIVED_BEAT_DBURI

    def test_an_unconfigured_extensions_database_falls_back_to_the_field_default(
        self, tmp_path, monkeypatch
    ):
        """Resolve the probe's own default when the profile configures no PMM Extensions database."""
        _use_profile(tmp_path, monkeypatch, CELERY_PROFILE_BLOCK)

        assert Settings().CELERY.beat_dburi == "sqlite:///extensions.db"

    def test_a_sqlite_store_still_nulls_the_beat_schema(self, tmp_path, monkeypatch):
        """Clear ``beat_schema`` for a SQLite-derived store, as the validator does.

        The derived value enters as source data rather than as a post-construction
        assignment, so ``CeleryOptions``' own validators still see it.
        """
        _use_profile(
            tmp_path,
            monkeypatch,
            CELERY_PROFILE_BLOCK
            + "    BEAT_SCHEMA: extensions\n"
            + EXTENSIONS_SQLITE_PROFILE_BLOCK,
        )

        resolved = Settings().CELERY

        assert resolved.beat_dburi == "sqlite:///extensions.db"
        assert resolved.beat_schema is None

    @pytest.mark.usefixtures("_postgres_profile")
    def test_an_invalid_extensions_database_fails_settings_construction(
        self, monkeypatch
    ):
        """Reject an unusable PMM Extensions database rather than derive a malformed store URI."""
        monkeypatch.setenv("EXTENSIONS__DATABASE__PORT", "notanumber")

        with pytest.raises(ValidationError, match="DATABASE.PORT"):
            Settings()

    @pytest.mark.usefixtures("_postgres_profile")
    def test_a_configured_store_is_not_held_to_the_extensions_database(
        self, monkeypatch
    ):
        """Accept an unusable PMM Extensions database when the store is configured outright.

        ``InventorySettings`` and ``TasksSettings`` both resolve the global proxy
        through their ``BACKEND_CORS_ORIGINS`` default, so probing unconditionally
        would fail an Inventory-only or Tasks-only start-up over a PMM Extensions namespaced
        value nothing in that process reads.
        """
        monkeypatch.setenv("EXTENSIONS__DATABASE__PORT", "notanumber")
        monkeypatch.setenv(
            "CELERY__BEAT_DBURI", "postgresql://elsewhere@host:5432/beat"
        )

        assert (
            Settings().CELERY.beat_dburi
            == "postgresql+psycopg2://elsewhere@host:5432/beat"
        )

    def test_the_probe_default_matches_the_extensions_settings_field(self):
        """Pin the probe's database default to ``ExtensionsSettings``', which it cannot import.

        ``app/extensions/config.py`` imports ``app/core/config.py``, so the probe duplicates
        the default rather than reading it; this fails if the two drift apart.
        """
        assert (
            _ExtensionsDatabaseSettings.model_fields["DATABASE"].default
            == ExtensionsSettings.model_fields["DATABASE"].default
        )


class TestRemovedSettingsOverrideKeysDetector:
    """Cover the startup detector for the removed flat settings-override keys."""

    def test_unset_is_noop(self, monkeypatch, tmp_path):
        """Verify a profile naming none of the removed keys raises nothing."""
        _use_profile(tmp_path, monkeypatch, "  LOGGING: WARNING\n")
        detect_removed_settings_override_keys()

    @pytest.mark.parametrize(
        ("legacy_var", "value"),
        [
            ("SETTINGS_OVERRIDE_REFRESH_INTERVAL", "PT60S"),
            ("SETTINGS_OVERRIDE_REFRESHER_ENABLED", "false"),
            ("SETTINGS_OVERRIDE_ALLOWED_KEYS", '["Settings.LOGGING"]'),
        ],
    )
    def test_flat_env_var_fails_fast(self, monkeypatch, tmp_path, legacy_var, value):
        """Verify each removed key still set in the environment stops start-up."""
        _use_profile(tmp_path, monkeypatch, "  LOGGING: WARNING\n")
        monkeypatch.setenv(legacy_var, value)
        with pytest.raises(ValueError, match=legacy_var):
            detect_removed_settings_override_keys()

    @pytest.mark.parametrize(
        ("nested_var", "value"),
        [
            ("SETTINGS_OVERRIDE__REFRESH_INTERVAL", "PT60S"),
            ("SETTINGS_OVERRIDE__REFRESHER_ENABLED", "false"),
            ("SETTINGS_OVERRIDE__ALLOWED_KEYS", '["Settings.LOGGING"]'),
        ],
    )
    def test_nested_env_var_is_accepted(self, monkeypatch, tmp_path, nested_var, value):
        """Verify the nested spelling is not mistaken for the removed flat one."""
        _use_profile(tmp_path, monkeypatch, "  LOGGING: WARNING\n")
        monkeypatch.setenv(nested_var, value)
        detect_removed_settings_override_keys()

    def test_flat_yaml_key_fails_fast(self, monkeypatch, tmp_path):
        """Verify a profile still nesting nothing stops start-up.

        This is the deployment shape that matters most: a mounted profile
        written against the old spelling reads as an absent allowlist, which
        leaves the override API unrestricted.
        """
        _use_profile(
            tmp_path,
            monkeypatch,
            "  SETTINGS_OVERRIDE_ALLOWED_KEYS:\n    - Settings.LOGGING\n",
        )
        with pytest.raises(ValueError, match="SETTINGS_OVERRIDE_ALLOWED_KEYS"):
            detect_removed_settings_override_keys()

    def test_nested_yaml_block_is_accepted(self, monkeypatch, tmp_path):
        """Verify a migrated profile passes the detector."""
        _use_profile(
            tmp_path,
            monkeypatch,
            "  SETTINGS_OVERRIDE:\n    ALLOWED_KEYS:\n      - Settings.LOGGING\n",
        )
        detect_removed_settings_override_keys()
