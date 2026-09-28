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

"""Define PMM Extensions settings."""

import logging
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import Annotated, Any, ClassVar, Literal, Self
from urllib.parse import urlparse

from annotated_types import Gt
from pydantic import (
    AliasChoices,
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    HttpUrl,
    model_validator,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    SecretStr,
    ValidationError,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app import BASE_DIR
from app.core.celery.models import IntervalSchedule, Period
from app.core.config import (
    BaseYamlAppSettings,
)
from app.core.db.config import DatabaseOptions
from app.core.health import (
    API_READINESS_POLL_INTERVAL as DEFAULT_API_READINESS_POLL_INTERVAL,
)
from app.core.health import (
    API_READINESS_TIMEOUT as DEFAULT_API_READINESS_TIMEOUT,
)
from app.core.models import BaseCaseInsensitiveModel, BaseLowercaseModel
from app.core.settings_override.models import SettingClassEnum
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    coerce_field_value,
    hot_field,
    materialize_template,
    MaterializerContext,
    MaterializerPurpose,
    nested_overridable_field,
    not_overridable_field,
    SECRET_STR_MASK,
)
from app.core.utils import (
    deep_dict_update,
    slugify,
)
from app.core.utils.fields import (
    CredentialHttpUrl,
    RelativeDirectoryPathField,
    StrImportableAttribute,
    TimedeltaSeconds,
    UniqueList,
    URIPath,
    URIPathPrefix,
    URL,
)
from app.extensions.apps.nav_icons import NavIcon
from app.extensions.bundle_upload.plan import DeliveryPlan
from app.extensions.sync.fields import CONSTRAINED_SYNCER_FIELDS

logger = logging.getLogger(__name__)

_LEGACY_BACKUP_MODULE_NAMES = frozenset({"backup", "backups"})


def _module_or_package_exists(target: Path) -> bool:
    """Return whether ``target`` names a module file or package directory.

    Probe the filesystem instead of importing, so settings-construction validators
    can confirm a dotted path resolves without triggering circular imports through
    plugin ``__init__`` modules.

    :param target: The import path rendered as a filesystem path, extension omitted.
    :return: ``True`` when a ``target/__init__.py`` package or ``target.py`` module
        file exists on disk.
    """
    return (target / "__init__.py").is_file() or target.with_suffix(".py").is_file()


class App(BaseCaseInsensitiveModel):
    """Represent a PMM Extensions plugin.

    This model defines the structure for a plugin, including its name, module,
    URI path, and CSS class. It includes custom validators to resolve the module
    path and set default values based on the plugin's name.

    :param name: The name of the plugin. Optional: a MODULE_NAME-only entry
        omits it and the :class:`app.extensions.apps.framework.registry.AppRegistry`
        derives descriptive metadata from the module basename instead.
    :param module_name: The name of the module associated with the plugin. This field is
        automatically prefixed with ``app.extensions.apps.`` during validation.
    :param uri_path: The URI path where the plugin is accessible. Defaults to an empty
        string, but is automatically set to a slugified version of the plugin name if
        not provided.
    :param css_class: The CSS class associated with the plugin. Defaults to an empty
        string, but is automatically set to a slugified version of the plugin name if
        not provided.
    :param sidebar: Whether to add this plugin to the sidebar. Defaults to True.
    :param group: The nav group key this plugin nests under (read from YAML as
        ``GROUP``); ``None`` renders it as a top-level sidebar entry.
    :param nav_order: The plugin's sort position within the sidebar (read from
        YAML as ``NAV_ORDER``); ``None`` sorts last.
    :param react_route: The canonical React route (read from YAML as
        ``REACT_ROUTE``); ``None`` resolves to ``/apps/<key>`` in the listing.
    :param nav_icon: The sidebar icon key (read from YAML as ``NAV_ICON``),
        validated against the :class:`~app.extensions.apps.nav_icons.NavIcon` vocabulary;
        an unknown value fails settings validation at load.
    :param enabled: Whether the plugin ships enabled. Read only at first-startup
        seed time to set the initial :class:`app.extensions.models.AppState` row;
        defaults to ``True`` so every plugin already in ``settings.yaml`` keeps
        shipping enabled. Set ``ENABLED: false`` to seed a plugin disabled.
    :param api_router_path: Optional dot-separated import path to the plugin's
        JSON ``APIRouter`` instance (e.g. ``"app.extensions.apps.checksums.api_routes.router"``).
        When set, the router is mounted under ``/api/apps/{key}`` by the
        shared API router loop. Three input states:

        * **Field omitted** — auto-derive from ``module_name`` when the
          plugin ships ``api_routes.router`` (convention).
        * **Explicit string** — use as-is; fail-fast if the path cannot be
          imported.
        * **Explicit ``null``** — opt the plugin out of the JSON API mount,
          even if a conventional module exists.
    :param celery_module_path: Optional dot-separated import path to the plugin's
        Celery task module (e.g. ``"app.extensions.apps.alerts.celery"``). The path is a
        *module*, not an attribute, so it feeds the worker ``include`` list rather
        than being resolved to an object. Three input states, mirroring
        ``api_router_path``:

        * **Field omitted** — auto-derive ``<module_name>.celery`` when the plugin
          ships a ``celery.py`` (convention).
        * **Explicit string** — use as-is, filesystem-probed for existence during
          settings construction (import-free, like ``module_name``) so a typo fails
          at load rather than only when the Celery worker imports it. The path is a
          trusted operator setting at the same trust level as
          ``module_name``/``api_router_path`` and is not confined to the plugin's
          own package.
        * **Explicit ``null``** — opt the plugin out of the Celery include list,
          even if a conventional ``celery.py`` exists.
    """

    name: str | None = None
    module_name: str
    uri_path: HttpUrl | URIPath = ""
    css_class: str = ""
    sidebar: bool = True
    group: str | None = None
    nav_order: int | None = None
    react_route: URIPath | None = None
    nav_icon: NavIcon | None = None
    enabled: bool = True
    api_router_path: StrImportableAttribute | None = None
    celery_module_path: str | None = None

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, App):
            return self.module_name == other.module_name
        raise NotImplementedError

    @field_validator("module_name", mode="before")
    @classmethod
    def resolve_module_path(cls, v: str) -> str:
        """Resolve the full module path for the plugin.

        This method takes the module name provided and prefixes it with
        ``app.extensions.apps.`` to resolve the full import path. Legacy MySQL
        backups plugin names (``backup``, ``backups``) are remapped to
        ``mysql_backups`` with a deprecation warning before prefixing; the
        legacy aliases will not be supported in the next version.

        :param v: The module name to resolve.
        :type v: str
        :return: The full module path with the ``app.extensions.apps.`` prefix.
        :rtype: str
        """
        if v in _LEGACY_BACKUP_MODULE_NAMES:
            logger.warning(
                "App MODULE_NAME %r is deprecated; remapping to "
                "'mysql_backups'. The legacy value will not be supported "
                "in the next version — update settings.yaml to use "
                "'mysql_backups'.",
                v,
            )
            v = "mysql_backups"
        return f"app.extensions.apps.{v}"

    @field_validator("module_name")
    @classmethod
    def validate_module_exists(cls, v: str) -> str:
        """Confirm the plugin module exists on disk without importing its parent.

        ``importlib`` resolution executes the parent package's ``__init__``, which
        imports the plugin route/model graph. For a nested module
        (``mysql_backups.restore``) that both cycles back through ``app.extensions.deps``
        and registers cross-track models on the shared metadata while settings are
        still constructing. A filesystem probe keeps construction import-free; the
        real import happens when the registry is built, after settings are ready.

        :param v: The resolved ``app.extensions.apps.``-prefixed module path.
        :return: The validated module path.
        :raises ValueError: When no module file or package exists at the path.
        """
        relative = v.removeprefix("app.extensions.apps.")
        target = Path(__file__).parent / "apps" / Path(*relative.split("."))
        if _module_or_package_exists(target):
            return v
        raise ValueError(f"No module named {v}")

    @model_validator(mode="before")
    @classmethod
    def _set_default_from_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and (name := data.get("name")):
            slug = slugify(name)
            data["uri_path"] = data.get("uri_path") or f"/{slug}"
            data["css_class"] = data.get("css_class") or slug
        return data

    @model_validator(mode="after")
    def _default_api_router_from_convention(self) -> Self:
        """Auto-derive ``api_router_path`` from ``module_name`` when not set.

        Run only when the field was not present in the input data.
        ``None`` in the input is a deliberate opt-out and is preserved.

        Probe by filesystem, not by import, so that this validator can run
        during settings construction without triggering circular imports
        through plugin ``__init__`` modules. Fail-fast on a missing
        ``router`` attribute is still enforced later in
        ``build_apps_router`` via ``import_var``.

        :return: ``self`` with ``api_router_path`` populated when the
            plugin module ships an ``api_routes.py`` file.
        :rtype: Self
        """
        if "api_router_path" in self.model_fields_set:
            return self
        relative = self.module_name.removeprefix("app.extensions.apps.")
        candidate_file = (
            Path(__file__).parent
            / "apps"
            / Path(*relative.split("."))
            / "api_routes.py"
        )
        if candidate_file.is_file():
            self.api_router_path = f"{self.module_name}.api_routes.router"
        return self

    @model_validator(mode="after")
    def _default_celery_module_from_convention(self) -> Self:
        """Auto-derive ``celery_module_path`` from ``module_name`` when not set.

        Run only when the field was not present in the input data. ``None`` in the
        input is a deliberate opt-out and is preserved.

        Probe by filesystem, not by import, so this validator can run during
        settings construction without triggering circular imports through plugin
        ``__init__`` modules -- exactly as ``_default_api_router_from_convention``
        does for ``api_routes.py``.

        :return: ``self`` with ``celery_module_path`` populated when the plugin
            module ships a ``celery.py`` file.
        """
        if "celery_module_path" in self.model_fields_set:
            return self
        relative = self.module_name.removeprefix("app.extensions.apps.")
        candidate_file = (
            Path(__file__).parent / "apps" / Path(*relative.split(".")) / "celery.py"
        )
        if candidate_file.is_file():
            self.celery_module_path = f"{self.module_name}.celery"
        return self

    @model_validator(mode="after")
    def _validate_celery_module_exists(self) -> Self:
        """Confirm ``celery_module_path`` resolves to a module file on disk.

        The convention-derived path is set only after its own filesystem probe, but
        an explicit override skips that check, so a typo would surface only when the
        Celery worker tries to import the module. Probe by filesystem (import-free,
        consistent with ``validate_module_exists``) so a bad override fails during
        settings construction instead. The path is repo-root-relative, so an
        override is not confined to the plugin's own package.

        :return: ``self`` unchanged when the path exists or is unset.
        :raises ValueError: When the path names no module file or package on disk.
        """
        if self.celery_module_path is None:
            return self
        target = BASE_DIR / Path(*self.celery_module_path.split("."))
        if _module_or_package_exists(target):
            return self
        raise ValueError(f"No module named {self.celery_module_path}")


class CookieOptions(BaseModel):
    """Define the options for an authentication cookie PMM Extensions sets.

    :param COOKIE_NAME: The key of the authentication cookie. Defaults to "authToken".
    :param MAX_AGE: Maximum age of the cookie. Defaults to 7 days.
    :param SAMESITE: SameSite policy for the cookie. Defaults to 'lax'.
    :param SECURE: Whether the cookie should be accessible only via HTTPS.
        Defaults to True.
    :param PATH: Cookie ``Path`` attribute, stated relative to the application
        root and anchored under ``ROOT_PATH`` when the cookie is emitted. When
        ``None`` (the default), the cookie is not scoped to a specific path and
        the browser applies its default. When set, the value must start with
        ``/``.
    """

    model_config = ConfigDict(
        alias_generator=AliasGenerator(
            serialization_alias=lambda field_name: field_name.lower(),
        ),
    )
    COOKIE_NAME: str = Field(default="authToken", serialization_alias="key")
    MAX_AGE: TimedeltaSeconds = timedelta(days=7)
    SAMESITE: Literal["lax", "strict", "none"] = "lax"
    SECURE: bool = True
    PATH: URIPath | None = None


def _reject_removed_syncer_pmm(data: Any) -> Any:
    """Reject a removed per-syncer ``pmm`` override key.

    The per-syncer ``pmm:`` override was removed; PMM synchronizers now
    read the top-level ``PMM`` section directly. A leftover ``pmm`` key (any case) is
    rejected with a ``ValueError`` -- which pydantic wraps into a ``ValidationError``
    -- so upgraded deployments fail fast at startup instead of silently honoring dead
    config. ``ExtensionsSettings.add_syncer_extra_kwargs`` re-validates each merged
    ``SyncOptions``, so a ``pmm`` carried via ``SYNCER_EXTRA_KWARGS`` is rejected there
    too.

    :param data: The raw input mapping passed to the model.
    :return: The input unchanged when no ``pmm`` key is present.
    :raises ValueError: When the input carries a ``pmm`` key (any case).
    """
    if isinstance(data, dict) and any(
        isinstance(k, str) and k.lower() == "pmm" for k in data
    ):
        raise ValueError(
            "Per-syncer 'pmm:' override is no longer honored (removed in SEP-1477); "
            "PMM synchronizers read the top-level 'PMM' section. Remove the stale "
            "'pmm' key from EXTENSIONS.SYNCERS / EXTENSIONS.SYNCER_EXTRA_KWARGS."
        )
    return data


def _validate_constrained_syncer_extras(
    merged: dict[str, Any], extra_kwargs: dict[str, Any]
) -> None:
    """Reject a configured syncer threshold its field would refuse at construction.

    :param merged: One syncer's settings, with ``SYNCER_EXTRA_KWARGS`` merged in.
    :param extra_kwargs: The global extras merged in. ``EXTENSIONS.SYNCER_EXTRA_KWARGS`` is
        a YAML surface as well as an env one, so a string reaching a syncer through it
        gets the env leaf's quoting offered as a possibility, never as the diagnosis.
    :raises ValueError: When a constrained field carries an unusable value.
    """
    for key, constrained in CONSTRAINED_SYNCER_FIELDS.items():
        raw = merged.get(key)
        if raw is None:
            continue
        try:
            constrained.adapter.validate_python(raw)
        except ValidationError as exc:
            quoting_note = (
                f" If this came from an EXTENSIONS__SYNCER_EXTRA_KWARGS__{key.upper()} env "
                f"override, note that such a leaf always reaches settings as a "
                f"string; spell the value under EXTENSIONS.SYNCERS in the YAML profile, or "
                f"as JSON in EXTENSIONS__SYNCERS, to have it read as a number."
                if isinstance(raw, str) and key in extra_kwargs
                else ""
            )
            raise ValueError(
                f"{key.upper()} is set to {raw!r} for syncer "
                f"{merged.get('syncer')!r}, which is not a usable value: give "
                f"{constrained.accepted}.{quoting_note}"
            ) from exc


class SyncerExtraKwargs(BaseLowercaseModel):
    """Global keyword arguments merged into every configured synchronizer."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def reject_removed_pmm(cls, data: Any) -> Any:
        """Reject a removed per-syncer ``pmm`` override key.

        :param data: The raw input mapping passed to the model.
        :return: The input unchanged when no ``pmm`` key is present.
        :raises ValueError: When the input carries a ``pmm`` key.
        """
        return _reject_removed_syncer_pmm(data)


class SyncOptions(BaseLowercaseModel):
    """Represent a synchronizer for the PMM Extensions app.

    This model represents a synchronizer component within the PMM Extensions application,
    including its importable attribute and any additional keyword arguments required for
    its operation.

    :param syncer: The importable attribute name for the synchronizer. This field is
        automatically prefixed with "app.extensions.sync.syncers." during validation.
    :type syncer: StrImportableAttribute
    """

    model_config = ConfigDict(extra="allow")
    syncer: StrImportableAttribute

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SyncOptions):
            return self.syncer == other.syncer
        raise NotImplementedError

    @model_validator(mode="before")
    @classmethod
    def reject_removed_pmm(cls, data: Any) -> Any:
        """Reject a removed per-syncer ``pmm`` override key.

        :param data: The raw input mapping passed to the model.
        :return: The input unchanged when no ``pmm`` key is present.
        :raises ValueError: When the input carries a ``pmm`` key.
        """
        return _reject_removed_syncer_pmm(data)

    @field_validator("syncer", mode="before")
    @classmethod
    def resolve_syncer_path(cls, v: str) -> str:
        """Resolve the full path for the syncer.

        Prefix the provided synchronizer name with "app.extensions.sync.syncers." to form the
        complete import path.

        :param v: The base syncer name provided.
        :type v: str
        :return: The fully qualified path for the synchronizer.
        :rtype: str
        """
        root = "app.extensions.sync.syncers."
        if not v.startswith(root):
            v = root + v
        return v


class AppDrainSettings(BaseLowercaseModel):
    """Configure the cooperative app-drain reconciler.

    :param reconcile_interval: Cadence of the ``reconcile_disabling_apps`` safety
        net that prunes orphaned running-task rows and finalizes idle
        ``DISABLING`` apps. Defaults to every 5 minutes.
    :param stale_task_ttl: Maximum age of an
        :class:`app.extensions.models.AppRunningTask` row before the reconciler treats it
        as orphaned (its task was force-killed or its worker crashed, so
        ``task_postrun`` never deleted it) and prunes it. Must exceed the longest
        expected runtime of a drainable task. Defaults to 1 hour.
    """

    reconcile_interval: IntervalSchedule = IntervalSchedule(
        every=5, period=Period.MINUTES
    )
    # Positivity uses the ``Gt`` annotation constraint rather than a
    # ``field_validator`` because runtime-override coercion re-checks
    # annotated-type constraints but does not re-run field validators; a
    # non-positive TTL would trigger the premature-pruning failure mode noted in
    # the ``stale_task_ttl`` docstring param above.
    stale_task_ttl: Annotated[TimedeltaSeconds, Gt(timedelta(0))] = timedelta(hours=1)


class DeliveryPlanInputs(BaseModel):
    """Carry the per-deployment inputs a baked delivery plan leaves open.

    Both leaves are sealed with ``not_overridable_field`` so the settings API
    accepts only an atomic whole-object PATCH. Were they overridable, a
    ``__``-delimited leaf write would be coerced by ``coerce_nested_field_value``
    and never reach the owning field's materializer, which is the only place a
    payload is checked against the baked skeleton.

    :param endpoint: The receiver base URL, or ``None`` to keep the baked plan's.
    :param secrets: Values for the secret names the baked plan declares.
    """

    endpoint: CredentialHttpUrl | None = (  # ty: ignore[invalid-assignment]
        not_overridable_field(None)
    )
    secrets: dict[str, SecretStr] = (  # ty: ignore[invalid-assignment]
        not_overridable_field({})
    )

    @field_validator("secrets")
    @classmethod
    def _reject_unresolved_mask(
        cls, value: dict[str, SecretStr]
    ) -> dict[str, SecretStr]:
        """Reject a secret carrying the redaction mask as its value.

        The mask is what every read surface renders -- the settings API and the
        YAML export alike -- so it round-trips back as a plausible-looking value.
        On the override path ``preserve_patch_secret_value`` swaps a resubmitted
        mask for the stored secret before this runs, so a mask arriving here
        means there was nothing to restore; on the YAML and environment paths
        nothing swaps it at all. A field validator covers every acquisition path,
        which a materializer cannot: materializers run on the override path only.

        :param value: The submitted secret values.
        :return: The validated secret values.
        :raises ValueError: When any value is the redaction mask.
        """
        if masked := sorted(
            name
            for name, secret in value.items()
            if secret.get_secret_value() == SECRET_STR_MASK
        ):
            raise ValueError(
                f"Secrets carry the redaction mask and cannot be stored: "
                f"{', '.join(masked)}"
            )
        return value


def materialize_delivery_plan_inputs(ctx: MaterializerContext) -> Any:
    """Bind submitted delivery-plan inputs against the baked plan skeleton.

    ``DeliveryPlan``'s cross-reference validator only checks that every cited
    secret name is declared, never the converse, so an extra name would persist
    in the stored row and come back to whoever submitted it as drift.

    The name check applies to a payload submitted now
    (:attr:`~app.core.settings_override.MaterializerPurpose.VALIDATE`) and not to
    a row stored earlier. A row written against a skeleton an image upgrade has
    since changed is a condition the operator has to be told about, and raising
    here would only drop the row from the snapshot, leaving
    :func:`~app.extensions.bundle_upload.resolver.resolve_delivery_plan` unable to tell
    drift from a deployment that never configured delivery. Shape coercion stays
    strict on both paths, so a malformed row is still dropped.

    Reading the skeleton off ``extensions_settings`` is safe because
    ``DIAGNOSTICS_DELIVERY`` is ``not_overridable_field``, so it never comes from
    the snapshot being rebuilt.

    :param ctx: The materialization context.
    :return: The validated inputs, or ``None`` when the override clears them.
    :raises ValidationError: When the payload does not match the field's shape.
    :raises ValueError: When a submitted payload's secret names are not exactly
        the ones the skeleton declares.
    """
    inputs = coerce_field_value(ctx.field_info, ctx.raw)
    if inputs is None or ctx.purpose is MaterializerPurpose.SNAPSHOT:
        return inputs
    skeleton = extensions_settings.DIAGNOSTICS_DELIVERY
    declared = frozenset(skeleton.secrets) if skeleton is not None else frozenset()
    submitted = frozenset(inputs.secrets)
    if submitted != declared:
        problems: list[str] = []
        if undeclared := sorted(submitted - declared):
            problems.append(f"undeclared secret names {', '.join(undeclared)}")
        if missing := sorted(declared - submitted):
            problems.append(f"missing secret names {', '.join(missing)}")
        raise ValueError(
            f"Delivery-plan inputs do not match the configured plan: "
            f"{'; '.join(problems)}."
        )
    return inputs


def _warn_legacy_apps_key() -> None:
    """Emit a deprecation warning for the legacy ``EXTENSIONS.PLUGINS`` config key."""
    logger.warning(
        "The EXTENSIONS.PLUGINS / EXTENSIONS__PLUGINS config key is deprecated and will be "
        "removed in a future release; use EXTENSIONS.APPS / EXTENSIONS__APPS instead.",
    )


class ExtensionsSettings(BaseYamlAppSettings):
    """Define settings for PMM Extensions.

    :cvar SETTINGS_PREFIXES: The prefixes for PMM Extensions related settings in the configuration
        file. Set to ["EXTENSIONS"].
    :param UVICORN_PORT: The port number used by the Uvicorn server. Defaults to 8000.
    :param ROOT_PATH: The URL prefix an intermediary proxy serves PMM Extensions under, such as
        ``/extensions``. Defaults to ``""``, the origin root. Read once when the application
        is constructed, so it is set through env or YAML only. A configured cookie
        ``Path`` is anchored under it, so ``SESSION_REFRESH.PATH`` is stated
        prefix-free and reaches the browser prefixed.
    :param SESSION_REFRESH: Cookie configuration options for the SPA
        ``refreshToken`` cookie. The cookie is ``HttpOnly`` and scoped to
        ``/api/oauth`` by default. When overriding ``PATH`` via YAML or env
        vars, the value must start with ``/``.
    :param ALERT_DEFINITIONS_DIR: Path to the directory containing YAML alert
        definition files. When ``None``, the bundled ``alert_definitions/`` directory
        inside the alerts plugin is used.
    :param INVENTORY_ENDPOINT: The endpoint URL for the Inventory API.
    :param TASKS_ENDPOINT: The endpoint URL for the Tasks API.
    :param APPS: A list of apps used by PMM Extensions. Defaults to an empty list with
        duplicates removed.
    :param PROXY_HEADERS: Whether to use proxy headers (like ``X-Forwarded-For``).
        Defaults to ``False``.
    :param DATABASE: The database configuration options.
        Defaults to an SQLite database with the name 'extensions.db'.
    :param SYNCERS: A list of synchronizers used by PMM Extensions. Defaults to an empty list with
        duplicates removed.
    :param SYNCER_EXTRA_KWARGS: Additional keyword arguments for synchronizers. Defaults
        to an empty mapping.
    :param SYNC_REFRESH_TIME: The time interval (in seconds) for browser refresh during
        synchronization. Defaults to 5 seconds.
    :param DIAGNOSTICS_DELIVERY: The delivery plan used to send diagnostic
        bundles to a receiver: named secrets, an ordered list of HTTP resolution
        steps, and one terminal multipart upload step. ``None`` (the default)
        means no receiver is configured. This skeleton is set through env or YAML
        only, so every write runs the plan's cross-reference validation as a
        whole; DB overrides are rejected because a per-leaf override would merge
        without re-validating the plan. ``DIAGNOSTICS_DELIVERY_INPUTS`` is the
        runtime surface for the values that differ per deployment.
    :param DIAGNOSTICS_DELIVERY_INPUTS: The per-deployment values
        ``DIAGNOSTICS_DELIVERY`` leaves open -- the receiver endpoint and the
        values of the secret names its steps cite. ``None`` (the default) leaves
        the baked plan to stand on its own. Overridable as one atomic
        whole-object write, whose materializer refuses a payload naming any
        secret the baked plan does not declare, or omitting one it does.
    :param APP_DRAIN: Operator-tunable settings for the cooperative app-drain
        reconciler (reconcile cadence and stale running-task TTL).
    :param FOOTER_TEMPLATE: Template string for the sidebar footer text, supporting
        ``$summary`` and ``$version`` placeholders. Defaults to ``"$summary $version"``.
    :param ARTIFACT_DOWNLOAD_TTL: Maximum age (in seconds) of signed artifact download
        tokens. Defaults to 600.
    :param CONNECTIVITY_CHECK_DEFAULT: Initial state of the "Check connectivity"
        checkbox on task creation forms. When ``True``, the checkbox is pre-checked;
        when ``False`` (default), it is unchecked. Because unchecked HTML
        checkboxes submit no field, the route parameter defaults to ``False`` —
        automated clients that omit ``check_connectivity`` will skip the check
        regardless of this setting.
    :param API_READINESS_TIMEOUT: Total seconds Celery beat waits for the HTTP API
        to answer its health probe before starting ungated. Defaults to 60. A
        deployment whose application startup runs long needs to raise this;
        exhausting the budget means beat dispatches overdue periodic tasks against
        an API that is not yet accepting connections.
    :param API_READINESS_POLL_INTERVAL: Seconds between readiness probe attempts.
        Defaults to 0.5.
    :param AMBIENT_SESSION_SSO_ENABLED: Whether to sign an unauthenticated caller
        in automatically from an existing PMM/Grafana session cookie (ambient
        SSO), skipping PMM Extensions' login form. Defaults to ``False`` (opt-in). Takes
        effect only under the Grafana auth provider and requires PMM Extensions and Grafana
        to be same-site so the browser sends the session cookie to PMM Extensions.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS"]
    UVICORN_PORT: int = 8000
    ROOT_PATH: URIPathPrefix = ""
    SESSION_REFRESH: CookieOptions = (  # ty: ignore[invalid-assignment]
        nested_overridable_field(
            CookieOptions(
                COOKIE_NAME="refreshToken",
                PATH="/api/oauth",
            ),
            advanced=True,
        )
    )
    ALERT_DEFINITIONS_DIR: RelativeDirectoryPathField | None = None
    INVENTORY_ENDPOINT: CredentialHttpUrl = hot_field(  # ty: ignore[invalid-assignment]
        ..., advanced=True
    )
    TASKS_ENDPOINT: CredentialHttpUrl = hot_field(  # ty: ignore[invalid-assignment]
        ..., advanced=True
    )
    APPS: UniqueList[App] = Field(
        default_factory=UniqueList,
        validation_alias=AliasChoices("APPS", "PLUGINS"),
    )
    PROXY_HEADERS: bool = False
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="extensions.db")
    SYNCERS: UniqueList[SyncOptions] = UniqueList()
    SYNCER_EXTRA_KWARGS: SyncerExtraKwargs = SyncerExtraKwargs()
    SYNC_REFRESH_TIME: int = hot_field(5)  # ty: ignore[invalid-assignment]
    DIAGNOSTICS_DELIVERY: DeliveryPlan | None = (  # ty: ignore[invalid-assignment]
        not_overridable_field(None, advanced=True)
    )
    DIAGNOSTICS_DELIVERY_INPUTS: (
        DeliveryPlanInputs | None
    ) = (  # ty: ignore[invalid-assignment]
        hot_field(None, materializer=materialize_delivery_plan_inputs, advanced=True)
    )
    APP_DRAIN: AppDrainSettings = (  # ty: ignore[invalid-assignment]
        nested_overridable_field(AppDrainSettings())
    )
    ARTIFACT_DOWNLOAD_TTL: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        600, advanced=True
    )
    # Plain fields rather than ``hot_field``: the readiness gate runs in a
    # pre-fork beat child, before the DB override refresher exists to serve one.
    API_READINESS_TIMEOUT: PositiveFloat = DEFAULT_API_READINESS_TIMEOUT
    API_READINESS_POLL_INTERVAL: PositiveFloat = DEFAULT_API_READINESS_POLL_INTERVAL
    CONNECTIVITY_CHECK_DEFAULT: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=False
    )
    AMBIENT_SESSION_SSO_ENABLED: bool = hot_field(  # ty: ignore[invalid-assignment]
        default=False,
        description=(
            "Enable ambient Grafana-session SSO: sign an unauthenticated caller "
            "in automatically from an existing PMM/Grafana session cookie, "
            "skipping the PMM Extensions login form. Off by default; effective "
            "only under the Grafana auth provider and only when PMM Extensions and "
            "Grafana are same-site so the browser sends the session cookie to it."
        ),
    )
    FOOTER_TEMPLATE: Template = hot_field(  # ty: ignore[invalid-assignment]
        Template("$summary $version"),
        materializer=materialize_template,
        advanced=True,
    )

    _pre_merge_syncers: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _merged_syncers: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _warn_removed_pmm_frontend(cls, data: Any) -> Any:
        """Warn if the removed ``PMM_FRONTEND`` field is still set.

        :param data: The raw input data.
        :type data: Any
        :return: The input data unchanged.
        :rtype: Any
        """
        if isinstance(data, dict) and data.get("PMM_FRONTEND") is not None:
            logger.warning(
                "EXTENSIONS__PMM_FRONTEND has been removed. "
                "Use PMM__FRONTEND (top-level) or EXTENSIONS__PMM__FRONTEND instead.",
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _warn_legacy_plugins_key(cls, data: Any) -> Any:
        """Emit a deprecation warning when the legacy ``PLUGINS`` key is set via YAML or init.

        :param data: The raw input data.
        :return: The input data unchanged.
        """
        if isinstance(data, dict) and "PLUGINS" in data:
            _warn_legacy_apps_key()
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
        """Emit a deprecation warning when the legacy ``EXTENSIONS__PLUGINS`` env key supplies the app list.

        The before-validator covers the YAML / init path but never sees an
        env-only legacy key: the environment source keys the value by the field
        name ``APPS``, not the matched alias. Detect the stripped legacy key
        here so the deprecation warning is airtight for the env source too.

        :param settings_cls: The settings class being configured.
        :param init_settings: The init-arguments source.
        :param env_settings: The environment-variable source.
        :param dotenv_settings: The dotenv-file source.
        :param file_secret_settings: The file-secret source.
        :return: The source tuple from the base implementation, unchanged.
        """
        sources = super().settings_customise_sources(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        if any(
            "plugins" in getattr(source, "env_vars", {})
            for source in (env_settings, dotenv_settings)
        ):
            _warn_legacy_apps_key()
        return sources

    @field_validator("FOOTER_TEMPLATE", mode="before")
    @classmethod
    def coerce_footer_template(cls, v: Any) -> Any:
        """Coerce the footer template to a Template object.

        If the provided value is a string, convert it to a ``Template`` object. Else,
        return it as is.

        :param v: The footer template value to coerce.
        :type v: Any
        :return: The coerced ``Template`` object or the original input value.
        :rtype: Any
        """
        if isinstance(v, str):
            return Template(v)
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_removed_extensions_pmm(cls, data: Any) -> Any:
        """Reject a removed ``EXTENSIONS.PMM`` section.

        ``EXTENSIONS.PMM`` was removed; PMM connection/auth config now lives
        only under the top-level ``PMM`` section, and the alerts fields it used to
        carry moved to the alerts-owned ``EXTENSIONS.ALERTS`` section. A leftover ``PMM``
        key under ``EXTENSIONS`` (any case, including the ``EXTENSIONS__PMM__*`` env-var path) is
        rejected with a ``ValueError`` -- which pydantic wraps into a
        ``ValidationError`` -- so upgraded deployments fail fast at startup instead
        of silently carrying dead config.

        :param data: The raw input mapping passed to the model.
        :return: The input unchanged when no top-level ``PMM`` key is present.
        :raises ValueError: When the input carries a ``PMM`` key.
        """
        if isinstance(data, dict) and any(
            isinstance(k, str) and k.lower() == "pmm" for k in data
        ):
            raise ValueError(
                "The 'EXTENSIONS.PMM' section was removed (SEP-1477); PMM connection config "
                "now lives only under the top-level 'PMM' section, and its alerts "
                "fields moved to the 'EXTENSIONS.ALERTS' section. Remove the stale 'EXTENSIONS.PMM' "
                "block from settings.yaml."
            )
        return data

    @model_validator(mode="after")
    def add_syncer_extra_kwargs(self) -> Self:
        """Integrate extra keyword arguments into synchronizers.

        Merge additional keyword arguments from ``SYNCER_EXTRA_KWARGS`` into each
        synchronizer in ``SYNCERS`` and update the list accordingly. Every override
        surface lands in this merge, so it is also where a constrained threshold is
        checked against the type its syncer field declares. Merging again with the
        same ``SYNCER_EXTRA_KWARGS`` leaves ``SYNCERS`` as it was.

        :return: The updated ``ExtensionsSettings`` instance with modified ``SYNCERS``.
        :raises ValueError: When a merged threshold carries an unusable value.
        """
        # ``model_validate`` runs an after-validator twice, and ``deep_dict_update``
        # prepends for a list, so re-merging a merged value concatenates it with
        # itself. Each run re-derives from the pre-merge payloads instead, re-taking
        # them only when SYNCERS holds something this merge did not produce.
        current = [syncer.model_dump() for syncer in self.SYNCERS]
        if current != self._merged_syncers:
            self._pre_merge_syncers = current
        syncers = UniqueList()
        extra_kwargs = self.SYNCER_EXTRA_KWARGS.model_dump(exclude_none=True)
        for syncer_data in self._pre_merge_syncers:
            # deep_dict_update mutates nested dicts in place; keep the snapshot intact.
            merged = deepcopy(syncer_data)
            deep_dict_update(merged, extra_kwargs)
            _validate_constrained_syncer_extras(merged, extra_kwargs)
            syncers.append(SyncOptions.model_validate(merged))
        self.SYNCERS = syncers
        self._merged_syncers = [syncer.model_dump() for syncer in syncers]
        return self


extensions_settings: ExtensionsSettings = OverridableSettingsProxy(
    ExtensionsSettings, setting_class=SettingClassEnum.EXTENSIONS_SETTINGS
)


def prefixed_cookie_path(path: str | None) -> str | None:
    """Return a cookie ``Path`` anchored under the configured URL prefix.

    A browser matches ``Path`` against the URL it requests, which carries the
    prefix, so a root-anchored value would be withheld from every prefixed
    endpoint, and a cookie the browser never sends is indistinguishable from one
    that was never set. ``None`` is returned unchanged: with no ``Path``
    attribute the browser derives one from the request URI, which already
    carries the prefix.

    :param path: The configured cookie path, or ``None`` to leave the attribute
        unset.
    :return: The path to emit, prefixed when both a prefix and a path are
        configured.
    """
    if path is None:
        return None
    return f"{extensions_settings.ROOT_PATH}{path}"


def warn_if_base_url_lacks_root_path(base_url: URL | None, setting_name: str) -> None:
    """Warn when an externally configured base URL omits the configured prefix.

    URLs are composed by joining onto the base's path, so a base that resolves
    outside ``ROOT_PATH`` still yields a well-formed string and the mismatch
    surfaces only as a download that no longer routes through the prefix.
    Advisory only: the caller still emits the URL.

    :param base_url: The configured external base URL, or ``None`` when unset.
    :param setting_name: The setting the base was read from, named in the warning
        so an operator knows which value to correct.
    """
    prefix = extensions_settings.ROOT_PATH
    if not prefix or base_url is None:
        return
    if not urlparse(str(base_url)).path.rstrip("/").endswith(prefix):
        logger.warning(
            "%s is set to %s, which omits the configured ROOT_PATH %s; URLs built "
            "on it will not reach PMM Extensions through the prefix it is served under.",
            setting_name,
            base_url,
            prefix,
        )
