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

"""Define the application settings."""

import hashlib
import hmac
import logging.config
import re
import secrets
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from functools import cached_property
from pathlib import Path
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    NoReturn,
    Protocol,
    runtime_checkable,
    Self,
    TypeVar,
)
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastapi import APIRouter, FastAPI, params
from fastapi.applications import AppType
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from pydantic import (
    computed_field,
    DirectoryPath,
    Field,
    field_validator,
    model_validator,
    PositiveInt,
    SecretStr,
    StringConstraints,
    validate_call,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from pydantic_settings.sources import DotEnvSettingsSource, PathType
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Lifespan

from app import BASE_DIR
from app.core.celery.config import CeleryOptions
from app.core.db.config import DatabaseOptions
from app.core.db.exception_handlers import register_db_capacity_handlers
from app.core.middleware.security_headers import (
    SecurityHeadersMiddleware,
    SecurityHeadersOptions,
)
from app.core.models import BaseCaseInsensitiveModel, BaseLowercaseModel
from app.core.requests import BaseRemoteAPI, ClientRegistry, RemoteAPI
from app.core.settings_override.models import SettingClassEnum
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field, not_overridable_field
from app.core.utils import deep_dict_update
from app.core.utils.fields import (
    EmptyStrToNone,
    LogLevel,
    NonEmptyStr,
    redact_credential_url,
    RelativeFilePathField,
    StrCredentialHttpUrl,
    StrHttpUrl,
    TimedeltaSeconds,
    URL,
)
from app.core.utils.openapi import (
    generate_tag_prefixed_unique_id,
    install_namespaced_openapi,
)


def _sanitize_client_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of remote-API client kwargs safe to log.

    Any value carrying an embedded URL password (e.g. ``endpoint``) is
    redacted so credentials never reach the debug log. ``SecretStr`` values
    already mask themselves on ``repr`` and are left untouched.

    :param kwargs: The client construction kwargs about to be logged.
    :type kwargs: dict[str, Any]
    :return: A copy with credential URLs redacted.
    :rtype: dict[str, Any]
    """
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        raw = str(value)
        redacted = redact_credential_url(raw)
        safe[key] = redacted if redacted != raw else value
    return safe


@runtime_checkable
class _EnvVarsSource(Protocol):
    """Expose the environment variables a settings source collected.

    ``settings_customise_sources`` must declare its parameters as the widest
    source type its base does, and this is the only capability it needs from the
    environment and dotenv ones.
    """

    env_vars: Mapping[str, str | None]


LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "context": {
            "()": "app.core.log.ContextFilter",
        },
    },
    "formatters": {
        "default": {
            "()": "app.core.log.ContextFormatter",
            "fmt": "%(name)s: [%(correlation_id)s] %(message)s <%(process)d>",
            "skip_keys": ["correlation_id"],
        },
        "uvicorn": {
            "()": "app.core.log.ContextFormatter",
            "fmt": "uvicorn: [%(correlation_id)s] %(message)s <%(process)d>",
            "skip_keys": ["correlation_id"],
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "rich.logging.RichHandler",
            "filters": ["context"],
            "omit_repeated_times": False,
            "show_path": False,
        },
        "app": {
            "formatter": "default",
            "class": "rich.logging.RichHandler",
            "filters": ["context"],
            "omit_repeated_times": False,
            "show_path": True,
        },
        "uvicorn": {
            "formatter": "uvicorn",
            "class": "rich.logging.RichHandler",
            "filters": ["context"],
            "omit_repeated_times": False,
            "show_path": False,
        },
        "celery": {
            "formatter": "default",
            "class": "rich.logging.RichHandler",
            "filters": ["context"],
            "omit_repeated_times": False,
            "show_path": False,
        },
    },
    "loggers": {
        "": {"handlers": ["default"], "level": "INFO"},
        "app": {"handlers": ["app"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["celery"], "level": "INFO", "propagate": False},
        "celery.beat": {"handlers": ["celery"], "level": "INFO", "propagate": False},
        "sqlalchemy_celery_beat": {
            "handlers": ["celery"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn": {"handlers": ["uvicorn"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["uvicorn"], "level": "INFO", "propagate": False},
        "uvicorn.access": {
            "handlers": ["uvicorn"],
            "level": "INFO",
            "propagate": False,
        },
        "watchfiles.main": {"handlers": ["default"], "level": "WARNING"},
    },
}


class PreEnvSettings(BaseSettings):
    """Define meta environment settings read that need to be read before the others.

    :param FASTAPI_ENV: The environment used (e.g. development, production).
        Defaults to "development".
    :param ENV_FILE: The dot env file used to populate the applications settings.
        Defaults to ".env" in the current directory.
    :param SETTINGS_FILE: The YAML file used to populate the applications settings.
        Defaults to "settings.yaml" in the current directory.
    :param SECRETS_DIR: The directory holding mounted secret files, each named after
        the canonical ``__``-nested variable it supplies. Defaults to unset, in which
        case no secret files are read.
    """

    model_config = SettingsConfigDict(extra="ignore")
    FASTAPI_ENV: str = "development"
    ENV_FILE: Path = Path(".env")
    SETTINGS_FILE: Path = Path("settings.yaml")
    SECRETS_DIR: Path | EmptyStrToNone = None


pre_env_settings = PreEnvSettings()


class YamlPrefixConfigSettingsSource(YamlConfigSettingsSource):
    """Define a YAML configuration settings source with prefix handling.

    This class extends `YamlConfigSettingsSource` to allow loading settings based on
    specified prefixes. It processes the YAML data by merging data from multiple
    prefixes into a single configuration.

    :param settings_cls: The settings class associated with this source.
    :type settings_cls: type[BaseSettings]
    :param yaml_file: The path to the YAML settings file. Defaults to "settings.yaml".
    :type yaml_file: PathType | None
    :param yaml_file_encoding: The encoding of the YAML file. Defaults to `None`.
    :type yaml_file_encoding: str | None
    :param prefixes: A sequence of prefixes to navigate through the YAML data.
    :type prefixes: Sequence[NonEmptyStr]
    :param base_prefix: The base prefix to start the configuration. Defaults to
        "default".
    :type base_prefix: NonEmptyStr | None
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        yaml_file: PathType | None = pre_env_settings.SETTINGS_FILE,
        yaml_file_encoding: str | None = None,
        prefixes: Sequence[NonEmptyStr] = (),
        base_prefix: NonEmptyStr | None = "default",
    ) -> None:
        super().__init__(settings_cls, yaml_file, yaml_file_encoding)
        if prefixes:
            prefix_data = self.yaml_data.get(prefixes[0], {})
            base_prefix_data = (
                self.yaml_data.get(base_prefix, {}) if base_prefix else {}
            )
            for prefix in prefixes[1:]:
                prefix_data = prefix_data.get(prefix, {})
                base_prefix_data = (
                    base_prefix_data.get(prefix, {}) if base_prefix else {}
                )
            self.yaml_data = base_prefix_data
            deep_dict_update(self.yaml_data, prefix_data)
            self.init_kwargs = self.yaml_data


class BaseYamlSettings(BaseSettings):
    """Base settings class for YAML config.

    :cvar SETTINGS_PREFIXES: Tuple of settings prefixes.
    :vartype SETTINGS_PREFIXES: list[str]
    :param FASTAPI_ENV: The environment used (e.g. development, production).
        Defaults to "development".
    :type FASTAPI_ENV: str
    """

    model_config = SettingsConfigDict(
        env_file=pre_env_settings.ENV_FILE,
        env_nested_delimiter="__",
        yaml_file=pre_env_settings.SETTINGS_FILE,
        secrets_dir=pre_env_settings.SECRETS_DIR,
        extra="ignore",
    )
    SETTINGS_PREFIXES: ClassVar[list[str]] = []
    FASTAPI_ENV: str = pre_env_settings.FASTAPI_ENV

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return the settings sources, highest priority first.

        The order is init kwarg, environment variable, dotenv entry, secret file,
        then YAML profile; each overrides the ones after it. Secret files are read
        from the directory ``SECRETS_DIR`` names, keyed by the same canonical
        ``__``-nested variable names their environment twins use.

        Within each of the environment, dotenv and secret-file sources, a name
        spelled with this class's prefix outranks the unprefixed global spelling
        of the same destination; the ranking between sources is unaffected.

        ``FASTAPI_ENV`` selects the YAML profile block, and is read from the same
        three sources in the same order, so the block loaded always matches the
        value the resolved settings report.

        :param settings_cls: The settings class being configured.
        :param init_settings: The init-arguments source.
        :param env_settings: The environment-variable source.
        :param dotenv_settings: The dotenv-file source.
        :param file_secret_settings: The file-secret source the secret-file source
            derives its directory and settings class from.
        :return: The settings sources, ordered highest-priority first.
        :raises SettingsError: When ``SECRETS_DIR`` names a path that is not a
            directory, or one whose contents exceed the source's size ceiling.
        :raises TypeError: If pydantic-settings supplies an environment or dotenv
            source that carries no ``env_vars`` mapping.
        """
        # The base declares the widest source type for every parameter, so the
        # two this reads ``env_vars`` off are narrowed here rather than in the
        # signature, which would not be a valid override.
        if not isinstance(env_settings, _EnvVarsSource) or not isinstance(
            dotenv_settings, _EnvVarsSource
        ):
            raise TypeError(
                "settings_customise_sources expects env and dotenv sources that "
                "expose env_vars"
            )
        secret_settings = NestedSecretsSettingsSource(file_secret_settings)
        env_key = "fastapi_env"
        yaml_prefix = (
            env_settings.env_vars.get(env_key)
            or dotenv_settings.env_vars.get(env_key)
            or secret_settings.env_vars.get(env_key, pre_env_settings.FASTAPI_ENV)
        )
        if cls.SETTINGS_PREFIXES:
            prefix_pattern = re.compile(
                f"^{'__'.join(cls.SETTINGS_PREFIXES).lower()}__([a-zA-Z0-9_-]+)$"
            )
            for env_source in [env_settings, dotenv_settings, secret_settings]:
                unprefixed: dict[str, Any] = {}
                prefixed: dict[str, Any] = {}
                for key, value in env_source.env_vars.items():
                    stripped = prefix_pattern.sub(r"\1", key)
                    (unprefixed if stripped == key else prefixed)[stripped] = value
                env_source.env_vars = {**unprefixed, **prefixed}
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            secret_settings,
            YamlPrefixConfigSettingsSource(
                settings_cls,
                prefixes=(yaml_prefix, *cls.SETTINGS_PREFIXES),
            ),
        )


T = TypeVar("T", bound=BaseRemoteAPI)


class PMMSettings(BaseLowercaseModel):
    """Define core PMM connection and authentication configuration.

    :param endpoint: The PMM server URL.
    :type endpoint: StrCredentialHttpUrl | None
    :param frontend: The PMM frontend URL.
    :type frontend: StrHttpUrl | None
    :param api_key: API key for PMM authentication.
    :type api_key: SecretStr | None
    :param verify_ssl: Whether to verify SSL certificates.
    :type verify_ssl: bool
    :param execution_target: Explicit execution target name or address for PMM tasks.
    :type execution_target: str | None
    :param annotations_enabled: Whether to create PMM annotations for task lifecycle
        events.
    :type annotations_enabled: bool
    :param annotations_timeout: Timeout in seconds for PMM annotation API calls.
    :type annotations_timeout: PositiveInt
    """

    endpoint: StrCredentialHttpUrl | None = None
    frontend: StrHttpUrl | None = hot_field(  # ty: ignore[invalid-assignment]
        None, advanced=True
    )
    api_key: SecretStr | None = None
    verify_ssl: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=True, advanced=True
    )
    execution_target: str | None = hot_field(  # ty: ignore[invalid-assignment]
        None, advanced=True
    )
    annotations_enabled: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=False, advanced=True
    )
    annotations_timeout: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        5, advanced=True
    )

    @model_validator(mode="after")
    def _default_frontend_to_endpoint(self) -> Self:
        """Set ``frontend`` to ``endpoint`` when not explicitly provided.

        :return: The updated settings instance.
        :rtype: Self
        """
        if "frontend" not in self.model_fields_set and self.endpoint is not None:
            self.frontend = self.endpoint
        return self

    @cached_property
    def hostname(self) -> str | None:
        """Extract and return the hostname from the PMM endpoint.

        :return: The hostname of the PMM endpoint, or ``None`` if not set.
        :rtype: str | None
        """
        if self.endpoint:
            return urlparse(self.endpoint).hostname
        return None


_INTERNAL_TOKEN_LABEL = b"extensions-internal-token"


def _encryption_key_error() -> str:
    """Return the remediation text for a missing or malformed key.

    Names the absolute dotenv path rather than the default spelling.
    ``ENV_FILE`` is a supported indirection and this repository's own checkouts
    use it, so prose that hardcodes the default sends the reader to a file the
    loader never reads: the append succeeds, the next start fails identically,
    and the natural reading is a malformed key rather than an unread file. It
    is resolved because the setting is a relative path by default, and a
    process whose working directory is not the one the reader is standing in
    would otherwise be told to edit ``.env`` without being told which.
    ``openssl rand -hex 32``, which ``SECRET_KEY``'s own message offers,
    produces 64 characters Fernet rejects, so the two remediations are
    deliberately different.

    :return: The remediation sentence, naming the dotenv file in use.
    """
    return (
        "ENCRYPTION_KEY must be set to a valid Fernet key (32 url-safe "
        "base64-encoded bytes). Generate one with `make encryption-key` or "
        "`openssl rand -base64 32`, then add it as ENCRYPTION_KEY=<key> to "
        f"{pre_env_settings.ENV_FILE.resolve()} (the file ENV_FILE names), "
        "export it, or mount it as a file named ENCRYPTION_KEY under "
        "SECRETS_DIR. It has no default and is never derived from SECRET_KEY."
    )


SettingsOverrideKey = Annotated[str, StringConstraints(pattern=r"^[^\s.]+\.[^\s.]+$")]


class SettingsOverrideOptions(BaseCaseInsensitiveModel):
    """Configure the DB-backed settings-override layer.

    Left unmarked on :class:`Settings` so the parent classifies
    ``NOT_OVERRIDABLE`` (matching ``SECURITY_HEADERS``). Marking it
    ``NESTED_ONLY`` would expose ``REFRESH_INTERVAL`` and ``REFRESHER_ENABLED``
    to the override API; ``ALLOWED_KEYS`` would stay refused by its own
    ``not_overridable_field`` marker, which is why that marker is kept.

    :param REFRESH_INTERVAL: How often each service refreshes its DB-backed
        setting overrides. Defaults to 30 seconds, and must be strictly
        positive. In a web process this is the wall-clock delay between
        periodic refresh cycles (``start_refresh_task`` hands
        ``interval.total_seconds()`` to ``asyncio.sleep``). In a prefork
        worker child it is checked at task boundaries — at most one refresh
        per interval per child per refresher — rather than a free-running
        timer; the same value is also the hang budget passed to
        ``bounded_refresh``, so lowering it for fresher overrides also
        tightens how long a due task may stall. A child running both the
        PMM Extensions side and Tasks-side refreshers can pay that budget twice when
        both are due at the same boundary. A non-positive value is rejected
        so neither path can hammer the database every iteration.
    :param REFRESHER_ENABLED: Master kill-switch for the DB-override
        background refresher. Tests set this to ``False`` to keep
        ``TestClient`` lifespans hermetic; production leaves it ``True``.
    :param ALLOWED_KEYS: The exhaustive set of settings keys the override API
        may write, each spelled ``"<SettingsClassName>.<KEY>"`` (the key being
        a top-level field name or a ``__``-delimited nested path). ``None``,
        the default, places no restriction. A set activates a default-locked
        allowlist: any pair it does not name is refused. The allowlist only
        ever *restricts*: a field that is already not overridable stays that
        way even when listed. Whether the named class and field exist is not
        checked at load time, so a typo'd entry allows nothing rather than
        raising; whitespace anywhere in an entry is rejected outright, since
        such an entry reads correct but matches nothing.
    """

    REFRESH_INTERVAL: Annotated[TimedeltaSeconds, Field(gt=timedelta(0))] = timedelta(
        seconds=30
    )
    REFRESHER_ENABLED: bool = True
    ALLOWED_KEYS: set[SettingsOverrideKey] | None = (  # ty: ignore[invalid-assignment]
        not_overridable_field(None)
    )


_REMOVED_SETTINGS_OVERRIDE_KEYS = {
    "SETTINGS_OVERRIDE_REFRESH_INTERVAL": "SETTINGS_OVERRIDE__REFRESH_INTERVAL",
    "SETTINGS_OVERRIDE_REFRESHER_ENABLED": "SETTINGS_OVERRIDE__REFRESHER_ENABLED",
    "SETTINGS_OVERRIDE_ALLOWED_KEYS": "SETTINGS_OVERRIDE__ALLOWED_KEYS",
}
"""Map each removed flat settings-override key to its nested replacement."""


class _LegacySettingsOverrideSettings(BaseYamlSettings):
    """Define the reader for the removed flat settings-override keys.

    Each field is typed :data:`~typing.Any` because only presence is inspected;
    a stale value is reported, never parsed.

    :param SETTINGS_OVERRIDE_REFRESH_INTERVAL: The removed key's value, or
        ``None`` when unset.
    :param SETTINGS_OVERRIDE_REFRESHER_ENABLED: The removed key's value, or
        ``None`` when unset.
    :param SETTINGS_OVERRIDE_ALLOWED_KEYS: The removed key's value, or ``None``
        when unset.
    """

    SETTINGS_OVERRIDE_REFRESH_INTERVAL: Any = None
    SETTINGS_OVERRIDE_REFRESHER_ENABLED: Any = None
    SETTINGS_OVERRIDE_ALLOWED_KEYS: Any = None


def detect_removed_settings_override_keys() -> None:
    """Reject a deployment still configuring the flat settings-override keys.

    ``Settings`` sets ``extra="ignore"``, so a flat key left in the environment
    or in a YAML profile is dropped without a word and the nested field falls
    back to its default. For ``ALLOWED_KEYS`` that default is ``None``, which
    lifts the override-API restriction entirely, so a deployment that migrates
    its image without re-spelling the key would widen what the settings API
    accepts. Fail fast on any of the three instead.

    :raises ValueError: When any removed flat key is still configured.
    """
    legacy = _LegacySettingsOverrideSettings()
    stale = sorted(
        (old, new)
        for old, new in _REMOVED_SETTINGS_OVERRIDE_KEYS.items()
        if getattr(legacy, old) is not None
    )
    if not stale:
        return
    spellings = ", ".join(f"{old} is now {new}" for old, new in stale)
    raise ValueError(
        f"Settings-override keys moved into the nested SETTINGS_OVERRIDE block "
        f"and are no longer read: {spellings}. Re-spell them in the "
        f"environment, or nest them under a SETTINGS_OVERRIDE: block in the "
        f"YAML profile. Leaving SETTINGS_OVERRIDE_ALLOWED_KEYS unmigrated lifts "
        f"the override-API restriction entirely."
    )


class _ExtensionsDatabaseSettings(BaseYamlSettings):
    """Resolve the PMM Extensions service's database options in isolation.

    ``Settings`` cannot read ``extensions_settings`` while it is being constructed:
    ``BaseYamlAppSettings.BACKEND_CORS_ORIGINS`` defaults off ``settings``, so
    forcing the PMM Extensions proxy re-enters the global proxy that is still resolving. This
    reads the same ``EXTENSIONS__DATABASE__*`` sources without either proxy.

    :cvar SETTINGS_PREFIXES: The prefix the probe resolves its environment and YAML
        sources under. Set to ["EXTENSIONS"].
    :param DATABASE: The PMM Extensions service's database connection options.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS"]
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="extensions.db")


class BeatStoreDefaultSource(PydanticBaseSettingsSource):
    """Supply the celery-beat store URI derived from the PMM Extensions database.

    Ranked last, and skipped entirely once a real source — init kwarg, environment,
    dotenv, secret file, or YAML profile — supplies ``CELERY__BEAT_DBURI``. The
    value is contributed as source data rather than assigned after construction, so
    ``CeleryOptions``' own validators still normalize the driver and null
    ``beat_schema`` for a SQLite store.

    :param settings_cls: The settings class being configured.
    :param dotenv_settings: The dotenv source, for the ``_env_file`` the caller
        passed to ``Settings``.
    :param file_secret_settings: The secret-file source, for the caller's
        ``_secrets_dir``. Typed concretely because that attribute is declared by
        ``SecretsSettingsSource`` rather than by the source base class.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        dotenv_settings: DotEnvSettingsSource,
        file_secret_settings: SecretsSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._env_file = dotenv_settings.env_file
        self._secrets_dir = file_secret_settings.secrets_dir

    def get_field_value(self, field: FieldInfo, field_name: str) -> NoReturn:
        """Raise, since this source builds its whole payload in ``__call__``.

        :param field: The field being resolved.
        :param field_name: The field's name.
        :raises NotImplementedError: Always.
        """
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        """Return the derived beat-store URI, or nothing when a source supplied one.

        Rank alone would not settle this. ``deep_update`` merges the source payloads
        case-sensitively and ``CeleryOptions`` folds the key cases afterwards, so
        contributing ``BEAT_DBURI`` unconditionally would pin a same-cased
        higher-priority value at this source's position and let a lower-priority one
        written in the other case win the fold. ``current_state`` carries the
        sources above this one already merged, so withholding the key whenever one
        of them supplies it keeps the collision from arising at all.

        :return: The ``CELERY.BEAT_DBURI`` default derived from PMM Extensions' database, or an
            empty payload when a configured source already supplies the store.
        :raises ValidationError: When the resolved ``EXTENSIONS__DATABASE__*`` values do
            not validate, so an unusable PMM Extensions database fails ``Settings``
            construction instead of yielding a malformed store URI. Only a
            deployment that leaves the beat store to be derived is held to this.
        """
        configured = self.current_state.get("CELERY")
        if isinstance(configured, dict) and any(
            key.lower() == "beat_dburi" for key in configured
        ):
            return {}
        database = _ExtensionsDatabaseSettings(
            _env_file=self._env_file, _secrets_dir=self._secrets_dir
        ).DATABASE
        return {"CELERY": {"BEAT_DBURI": database.URL}}


class Settings(BaseYamlSettings):
    """Define the main application settings.

    :param CELERY: Celery configuration options. ``BEAT_DBURI`` defaults to the
        resolved PMM Extensions database connection, so the beat store follows
        ``EXTENSIONS__DATABASE__*`` unless a source configures it explicitly.
    :param ALLOW_CONCURRENT_SESSIONS: Whether to allow concurrent sessions for the same
        user. Defaults to False, meaning all previous sessions will be invalidated once
        a new one is created.
    :param SECRET_KEY: The secret key used for signing tokens. Defaults to
        ``secrets.token_urlsafe(32)``.
    :param EXTENSIONS_INTERNAL_TOKEN_INPUT: The explicitly configured value of
        ``EXTENSIONS_INTERNAL_TOKEN``, read from every source under that canonical
        name. Optional: when it is unset or empty, ``derive_internal_token``
        derives the token from ``SECRET_KEY`` instead.
    :param EXTENSIONS_INTERNAL_TOKEN: A long random secret used for PMM Extensions internal
        service-to-service authentication (e.g. scheduled inventory sync). Never
        unset on a constructed instance: ``derive_internal_token`` populates it
        from ``EXTENSIONS_INTERNAL_TOKEN_INPUT``, or — when no explicit value is
        supplied — derives it from ``SECRET_KEY`` so every process sharing
        ``SECRET_KEY`` resolves the identical token. Generate an explicit value
        with ``openssl rand -hex 32`` to rotate it independently of
        ``SECRET_KEY``.
    :param ENCRYPTION_KEY: The Fernet key :mod:`app.core.encryption` uses to
        encrypt values PMM Extensions stores in its own databases. It has no default and is
        never derived from ``SECRET_KEY``: ciphertext outlives the process that
        wrote it, so a key that changed on restart would orphan every encrypted
        row. Every environment supplies its own, as an environment variable or
        as a file named ``ENCRYPTION_KEY`` under ``SECRETS_DIR``. Nothing is
        committed: a key in the repository would be readable by anyone who can
        read the repository, and the values it protects are real credentials.
    :param LOGGING: The logging level for the application. Defaults to LogLevel.WARNING.
    :param LOGGING_CONFIG: dictConfig logging configuration.
    :param SSL_CAFILE: The SSL CA file to use for remote API requests.
    :param BASE_URL: The application's base URL. Its path is preserved, with composed
        URLs appended to it rather than replacing it, so it must already include
        ``EXTENSIONS.ROOT_PATH`` when a URL prefix is configured.
    :param BACKEND_CORS_ORIGINS: A global list of allowed CORS origins, to be used as
        the default BACKEND_CORS_ORIGINS setting across all apps.
    :param ALLOWED_HOSTS: A global list of trusted domain names or wildcards, to be used
        as the default ALLOWED_HOSTS setting across all apps.
    :param SECURITY_HEADERS: Global options for the SecurityHeadersMiddleware, to be
        used as the default SECURITY_HEADERS setting across all apps.
    :param PMM: PMM connection and authentication configuration.
    :param SETTINGS_OVERRIDE: Options for the DB-backed settings-override layer
        (refresh interval, refresher kill-switch, and the override-API allowlist).
        See :class:`SettingsOverrideOptions`.
    """

    CELERY: CeleryOptions
    ALLOW_CONCURRENT_SESSIONS: bool = False
    SECRET_KEY: SecretStr = SecretStr(secrets.token_urlsafe(32))
    EXTENSIONS_INTERNAL_TOKEN_INPUT: SecretStr | None = Field(
        default=None, validation_alias="EXTENSIONS_INTERNAL_TOKEN", exclude=True
    )
    ENCRYPTION_KEY: SecretStr
    LOGGING: LogLevel = hot_field(LogLevel.WARNING)  # ty: ignore[invalid-assignment]
    LOGGING_CONFIG: dict[str, Any] = {}
    SSL_CAFILE: RelativeFilePathField | None = None
    BASE_URL: URL | None = None
    BACKEND_CORS_ORIGINS: list[StrHttpUrl] | None = None
    ALLOWED_HOSTS: list[str] = []
    SECURITY_HEADERS: SecurityHeadersOptions | None = SecurityHeadersOptions()
    PMM: PMMSettings = hot_field(PMMSettings())  # ty: ignore[invalid-assignment]
    SETTINGS_OVERRIDE: SettingsOverrideOptions = SettingsOverrideOptions()
    _CLIENT_REGISTRY: ClientRegistry = ClientRegistry()
    _EXTENSIONS_INTERNAL_TOKEN: SecretStr = SecretStr("")

    @computed_field(
        description=(
            "The internal service-to-service token. Derived from SECRET_KEY "
            "when no explicit value is configured."
        )
    )
    @property
    def EXTENSIONS_INTERNAL_TOKEN(self) -> SecretStr:
        """Return the internal service-to-service token, always populated.

        ``derive_internal_token`` fills it at the end of every construction, so
        nothing downstream has an unset case to handle.

        :return: The configured or derived internal token.
        """
        return self._EXTENSIONS_INTERNAL_TOKEN

    @EXTENSIONS_INTERNAL_TOKEN.setter
    def EXTENSIONS_INTERNAL_TOKEN(self, value: SecretStr) -> None:
        """Replace the resolved token, leaving the configured input untouched.

        :param value: The token to resolve to from here on.
        """
        self._EXTENSIONS_INTERNAL_TOKEN = value

    @EXTENSIONS_INTERNAL_TOKEN.deleter
    def EXTENSIONS_INTERNAL_TOKEN(self) -> None:
        """Resolve the token back to its configured or derived value.

        An override set through the setter is discarded by re-running the one
        derivation there is, rather than by restoring a remembered copy.
        """
        # The validator stays an ordinary bound method at runtime; only the
        # decorator's static type says otherwise.
        self.derive_internal_token()  # ty: ignore[call-non-callable]

    @computed_field
    @property
    def BASE_DIR(self) -> DirectoryPath:
        """The base directory for the application.

        :return: The base directory.
        :rtype: DirectoryPath
        """
        return BASE_DIR

    @field_validator("LOGGING_CONFIG")
    @classmethod
    def _set_default_logging_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Return the default configuration updated with the provided data.

        :param v: The new logging configuration.
        :type v: dict[str, Any]
        :return: The updated configuration.
        :rtype: dict[str, Any]
        """
        logging_config = deepcopy(LOGGING_CONFIG)
        deep_dict_update(logging_config, v)
        return logging_config

    @model_validator(mode="after")
    def set_log_level(self) -> Self:
        """Set the logging level for the application.

        Set the default log level in `self.LOGGING_CONFIG` according to the `LOGGING`
        setting.

        :return: Validated settings with default logging level set.
        :rtype: Settings
        """
        self.LOGGING_CONFIG["loggers"][""]["level"] = self.LOGGING
        self.LOGGING_CONFIG["loggers"]["app"]["level"] = self.LOGGING
        return self

    @model_validator(mode="after")
    def derive_internal_token(self) -> Self:
        """Populate ``EXTENSIONS_INTERNAL_TOKEN``, deriving it from ``SECRET_KEY``.

        Every process sharing ``SECRET_KEY`` derives the identical token via
        HMAC-SHA256, so PMM Extensions internal service-to-service authentication works
        across the web apps and the lifespan-less Celery worker without
        persisting or distributing a separate secret. An explicitly configured
        ``EXTENSIONS_INTERNAL_TOKEN`` takes precedence so it can be rotated
        independently; an unset or empty one is derived. This is the only place
        the token is derived, and it runs on every constructed instance, which
        is what lets ``EXTENSIONS_INTERNAL_TOKEN`` be typed as always set.

        :return: Validated settings with ``EXTENSIONS_INTERNAL_TOKEN`` guaranteed set.
        :raises ValueError: If ``EXTENSIONS_INTERNAL_TOKEN`` is unset and ``SECRET_KEY``
            is empty, so no token can be derived.
        """
        if (
            self.EXTENSIONS_INTERNAL_TOKEN_INPUT is not None
            and self.EXTENSIONS_INTERNAL_TOKEN_INPUT.get_secret_value()
        ):
            self._EXTENSIONS_INTERNAL_TOKEN = self.EXTENSIONS_INTERNAL_TOKEN_INPUT
            return self
        secret_key = self.SECRET_KEY.get_secret_value()
        if not secret_key:
            raise ValueError(
                "SECRET_KEY must be set to a non-empty value so EXTENSIONS_INTERNAL_TOKEN "
                "can be derived for service-to-service authentication "
                "(e.g. `openssl rand -hex 32`)."
            )
        derived = hmac.new(
            secret_key.encode(), _INTERNAL_TOKEN_LABEL, hashlib.sha256
        ).hexdigest()
        self._EXTENSIONS_INTERNAL_TOKEN = SecretStr(derived)
        return self

    @model_validator(mode="before")
    @classmethod
    def validate_encryption_key(cls, data: Any) -> Any:
        """Reject an unset, empty, or malformed ``ENCRYPTION_KEY``.

        The check runs ``before`` field validation so that an absent key reports
        the remediation below rather than a generic ``Field required``: pydantic
        resolves required-field presence ahead of any ``after`` model validator,
        so an ``after`` check never sees the absent case at all. Running here
        instead lets the field stay non-optional, which is what it is — nothing
        downstream ever reads a ``None`` key.

        :param data: The assembled settings sources, before field validation.
        :return: ``data`` unchanged, once the key is known usable.
        :raises ValueError: If the key is unset, empty, or not a valid Fernet key.
        """
        if not isinstance(data, dict):
            return data
        supplied = data.get("ENCRYPTION_KEY")
        key = (
            supplied.get_secret_value() if isinstance(supplied, SecretStr) else supplied
        )
        if not key:
            raise ValueError(_encryption_key_error())
        try:
            Fernet(key.encode() if isinstance(key, str) else key)
        except (TypeError, ValueError) as exc:
            raise ValueError(_encryption_key_error()) from exc
        return data

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Append the beat-store default below every configured source.

        :param settings_cls: The settings class being configured.
        :param init_settings: The init-arguments source.
        :param env_settings: The environment-variable source.
        :param dotenv_settings: The dotenv-file source.
        :param file_secret_settings: The file-secret source.
        :return: The settings sources, ordered highest-priority first.
        :raises SettingsError: When ``SECRETS_DIR`` names a path that is not a
            directory, or one whose contents exceed the source's size ceiling.
        """
        return (
            *super().settings_customise_sources(
                settings_cls,
                init_settings,
                env_settings,
                dotenv_settings,
                file_secret_settings,
            ),
            BeatStoreDefaultSource(settings_cls, dotenv_settings, file_secret_settings),
        )

    @validate_call
    async def get_remote_api(
        self,
        cls: type[T] = RemoteAPI,
        **kwargs: Any,
    ) -> T:
        """Get or create a RemoteAPI client instance.

        :param cls: The class of the RemoteAPI client. Defaults to :class:`RemoteAPI`.
        :type cls: type[T]
        :param kwargs: Additional keyword arguments to configure the RemoteAPI client.
        :type kwargs: Any
        :return: An instance of the requested RemoteAPI client.
        :rtype: T
        """
        logger.debug(
            "Getting remote API client from registry for %s with kwargs %s",
            cls.__name__,
            _sanitize_client_kwargs(kwargs),
        )
        return await self._CLIENT_REGISTRY.get(cls, **kwargs)

    async def invalidate_client(self, endpoint: str) -> None:
        """Evict every cached remote-API client served from ``endpoint``.

        Thin wrapper over :meth:`ClientRegistry.invalidate` used by override
        rebind callbacks to drop clients (e.g. PMM) whose connection settings
        changed at runtime, so the next request reconstructs a fresh client.

        :param endpoint: The endpoint URL whose cached clients to evict.
        :type endpoint: str
        """
        await self._CLIENT_REGISTRY.invalidate(endpoint)

    async def close_client_registry(self) -> None:
        """Close the client registry and all its managed clients."""
        await self._CLIENT_REGISTRY.close_all()


def _create_settings() -> Settings:
    """Create a :class:`Settings` instance and apply its logging configuration."""
    s = Settings()
    logging.config.dictConfig(s.LOGGING_CONFIG)
    return s


settings: Settings = OverridableSettingsProxy(
    _create_settings, setting_class=SettingClassEnum.SETTINGS
)
logger = logging.getLogger(__name__)


class BaseYamlAppSettings(BaseYamlSettings):
    """Define base settings for a FastAPI app.

    :cvar SETTINGS_PREFIXES: Tuple of settings prefixes.
    :vartype SETTINGS_PREFIXES: list[str]
    :param UVICORN_HOST: The host for Uvicorn. Defaults to "127.0.0.1".
    :type UVICORN_HOST: str
    :param UVICORN_PORT: The port for Uvicorn. Defaults to 0.
    :type UVICORN_PORT: int
    :param UVICORN_RELOAD: Enable auto-reload on file changes. Defaults to ``False``.
        Set to ``True`` in development for hot-reloading.
    :type UVICORN_RELOAD: bool
    :param UVICORN_EXTRA_RELOAD_DIRS: Additional directories for uvicorn to watch when
        ``UVICORN_RELOAD`` is enabled. Appended to the hardcoded base directories.
        Defaults to ``[]``.
    :type UVICORN_EXTRA_RELOAD_DIRS: list[str]
    :param UVICORN_EXTRA_RELOAD_INCLUDES: Additional glob patterns for uvicorn to include
        when watching for changes. Appended to the hardcoded base includes.
        Defaults to ``[]``.
    :type UVICORN_EXTRA_RELOAD_INCLUDES: list[str]
    :param UVICORN_EXTRA_RELOAD_EXCLUDES: Additional glob patterns for uvicorn to exclude
        when watching for changes. Appended to the hardcoded base excludes.
        Defaults to ``[]``.
    :type UVICORN_EXTRA_RELOAD_EXCLUDES: list[str]
    :param SSL_KEYFILE: Path to the SSL key file. Defaults to None.
    :type SSL_KEYFILE: RelativeFilePathField | None
    :param SSL_CERTFILE: Path to the SSL certificate file. Defaults to None.
    :type SSL_CERTFILE: RelativeFilePathField | None
    :param BACKEND_CORS_ORIGINS: A list of allowed CORS origins. Use None to disable the
        CORSMiddleware.
    :type BACKEND_CORS_ORIGINS: list[StrHttpUrl] | None
    :param ALLOWED_HOSTS: A list of trusted domain names or wildcards, to which all
        incoming requests have the Host header validated against. Use `["*"]` to allow
        any hostname. Defaults to `settings.ALLOWED_HOSTS`.
    :type ALLOWED_HOSTS: list[str]
    :param SECURITY_HEADERS: Specific options for the SecurityHeadersMiddleware.
        Use `False` to disable the middleware completely.
    :type SECURITY_HEADERS: SecurityHeadersOptions | None
    """

    UVICORN_HOST: str = "127.0.0.1"
    UVICORN_PORT: int = 0
    UVICORN_RELOAD: bool = False
    UVICORN_EXTRA_RELOAD_DIRS: list[str] = []
    UVICORN_EXTRA_RELOAD_INCLUDES: list[str] = []
    UVICORN_EXTRA_RELOAD_EXCLUDES: list[str] = []
    SSL_KEYFILE: RelativeFilePathField | None = None
    SSL_CERTFILE: RelativeFilePathField | None = None
    BACKEND_CORS_ORIGINS: list[StrHttpUrl] | None = Field(
        default_factory=lambda: settings.BACKEND_CORS_ORIGINS
    )
    ALLOWED_HOSTS: list[str] = Field(
        default_factory=lambda: settings.ALLOWED_HOSTS, min_length=1
    )
    SECURITY_HEADERS: SecurityHeadersOptions | None = SecurityHeadersOptions()

    @field_validator("ALLOWED_HOSTS")
    @classmethod
    def _warn_allowed_hosts_match_any(cls, v: list[str]) -> list[str]:
        if "*" in v:
            logger.warning(
                "The value '*' in %s.ALLOWED_HOSTS matches any hostname "
                "- use it carefully",
                cls.__name__,
            )
        return v

    @field_validator("SECURITY_HEADERS")
    @classmethod
    def _warn_security_headers_middleware_enabled_without_headers(
        cls, v: SecurityHeadersOptions | Literal[False]
    ) -> SecurityHeadersOptions | Literal[False]:
        if v and not any(v.model_dump().values()):
            logger.warning(
                "SecurityHeadersMiddleware is enabled in %s but all options are "
                "disabled. Set `SECURITY_HEADERS` to False to disable the middleware.",
                cls.__name__,
            )
        return v


@asynccontextmanager
async def default_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Define the default manager for the application's lifespan.

    Ensures that the active auth provider's SDK and any extra client sessions are
    properly managed during the application's startup and shutdown phases.

    :param app: The FastAPI application instance.
    :yield: None
    """
    # lazy import: auth/config.py imports BaseYamlSettings from this module, so a
    # module-level import here would cycle
    from app.core.auth.config import get_active_auth_provider

    async with get_active_auth_provider().lifespan():
        yield
    await settings.close_client_registry()


class _UnsetType:
    """Sentinel type for unset ``create_app`` parameters.

    Distinguishes "caller did not pass this argument" from "caller passed ``None``"
    so we can preserve FastAPI's own defaults when the parameter is omitted while
    still letting callers explicitly set the value to ``None``.
    """

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_UNSET"


_UNSET = _UnsetType()


def create_app(
    *routers: APIRouter,
    dependencies: Sequence[params.Depends] | None = None,
    lifespan: Lifespan[AppType] | None = None,
    backend_cors_origins: list[StrHttpUrl] | None = None,
    allowed_hosts: list[str] | None = None,
    security_headers: SecurityHeadersOptions | None = None,
    title: str | None = None,
    version: str | None = None,
    description: str | None = None,
    generate_unique_id_function: Callable[[APIRoute], str] | None = None,
    docs_url: str | None | _UnsetType = _UNSET,
    redoc_url: str | None | _UnsetType = _UNSET,
    root_path: str = "",
) -> FastAPI:
    """Create and configure the FastAPI app.

    :param routers: Routers to include in the created app.
    :param dependencies: Dependencies applied to every route the app includes,
        inherited by routers added after creation. Defaults to None.
    :param lifespan: Lifespan context manager for the FastAPI app, if any. Defaults to
        None.
    :param backend_cors_origins: A list of allowed origins for the CORSMiddleware.
        Defaults to None, meaning the middleware won't be added to the app.
    :param allowed_hosts: List of allowed hosts for the TrustedHostMiddleware. Defaults
        to None, meaning the middleware won't be added to the app.
    :param security_headers: Options for the SecurityHeadersMiddleware. Defaults to
        None, meaning the middleware won't be added to the app.
    :param title: Optional OpenAPI title for the generated spec.
    :param version: Optional OpenAPI version string.
    :param description: Optional OpenAPI description text.
    :param generate_unique_id_function: Optional callback for stable ``operationId``
        values. When omitted, :func:`app.core.utils.openapi.generate_tag_prefixed_unique_id`
        is used so similarly named handlers across routers do not collide.
    :param docs_url: Override for FastAPI's ``docs_url`` parameter. Pass ``None`` to
        disable the auto-generated Swagger UI. When omitted, FastAPI's default of
        ``"/docs"`` is preserved.
    :param redoc_url: Override for FastAPI's ``redoc_url`` parameter. Pass ``None`` to
        disable the auto-generated ReDoc UI. When omitted, FastAPI's default of
        ``"/redoc"`` is preserved.
    :param root_path: The URL prefix an intermediary proxy mounts the app under.
        Starlette strips it before matching routes and ``request.url_for`` re-adds
        it. Defaults to ``""``, which is inert: FastAPI writes the ASGI scope key
        only for a non-empty value, so the unprefixed app is untouched.
    :return: An instance of the FastAPI application, carrying the database
        capacity handlers every sub-application inherits from here.
    """
    openapi_kwargs = {}
    if title is not None:
        openapi_kwargs["title"] = title
    if version is not None:
        openapi_kwargs["version"] = version
    if description is not None:
        openapi_kwargs["description"] = description
    openapi_kwargs["generate_unique_id_function"] = (
        generate_unique_id_function or generate_tag_prefixed_unique_id
    )
    if docs_url is not _UNSET:
        openapi_kwargs["docs_url"] = docs_url
    if redoc_url is not _UNSET:
        openapi_kwargs["redoc_url"] = redoc_url
    app = FastAPI(
        lifespan=lifespan,
        root_path=root_path,
        dependencies=dependencies,
        **openapi_kwargs,
    )
    register_db_capacity_handlers(app)
    if backend_cors_origins is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=backend_cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if allowed_hosts is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    if security_headers is not None:
        app.add_middleware(SecurityHeadersMiddleware, options=security_headers)
    for router in routers:
        app.include_router(router)
    install_namespaced_openapi(app)
    return app
