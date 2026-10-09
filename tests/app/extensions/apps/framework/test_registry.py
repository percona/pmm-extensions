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

"""Tests for the ``AppRegistry`` and its builders in ``registry.py``."""

import importlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import APIRouter
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.celery.config import STATIC_CELERY_INCLUDE
from app.core.config import settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.settings_override.api.routes import AppOwnedClassEntry
from app.core.utils import json_serializer
from app.extensions.apps.alerts.config import alerts_settings, AlertsSettings
from app.extensions.apps.atw.schema import atw_schema
from app.extensions.apps.backup_mongo.restore.models import OWNER as RESTORE_MONGO_OWNER
from app.extensions.apps.framework.apps import TaskExecutionApp
from app.extensions.apps.framework.base import BaseApp
from app.extensions.apps.framework.registry import (
    app_celery_module_for,
    app_celery_module_paths,
    AppRegistry,
    build_app_registry,
    build_celery_include,
    collect_app_owned_settings_classes,
    collect_inventory_reference_providers,
    get_app_registry,
    resolve_app_settings_metadata,
)
from app.extensions.apps.framework.schema import Capabilities
from app.extensions.apps.mysql_backups.forms import OWNER as BACKUPS_OWNER
from app.extensions.apps.mysql_backups.inventory_references import (
    referenced_inventory_entities,
)
from app.extensions.apps.mysql_backups.restore.models import OWNER as RESTORES_OWNER
from app.extensions.apps.report.config import (
    health_report_settings,
    HealthReportSettings,
)
from app.extensions.apps.tasks.schema import TASKS_PLUGIN_SCHEMA
from app.extensions.config import App, extensions_settings
from app.extensions.models import AppLifecycleEnum, AppState
from app.tasks.models import ANY_OWNER
from tests.app.db_schema import apply_schema
from tests.app.extensions.apps.framework.kit import synth_app, synth_app_kwargs
from tests.app.extensions.conftest import REDUCED_ACTIVATION


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> Iterator[None]:
    """Reset the cached registry so each test rebuilds from its own input."""
    get_app_registry.cache_clear()
    yield
    get_app_registry.cache_clear()


def _force_legacy_synthesis(mocker: MockerFixture) -> None:
    """Drive the legacy-synthesis path with a stubbed module and jinja router.

    Every shipped plugin now exports an ``app`` definition, so synthesis is
    exercised against a module that exports no ``app`` and a stand-in router for
    the convention import paths.
    """
    mocker.patch(
        "app.extensions.apps.framework.registry.import_module",
        return_value=SimpleNamespace(),
    )
    mocker.patch(
        "app.extensions.apps.framework.registry.import_var",
        return_value=APIRouter(),
    )


class TestLegacyWrapping:
    """Tests for synthesizing a ``BaseApp`` from a legacy ``App``."""

    def test_legacy_plugin_wrapped_with_metadata(self) -> None:
        """A plugin with no ``app`` export is wrapped from its settings entry."""
        registry = build_app_registry([App(name="Snippets", module_name="snippets")])
        app = registry.get("snippets")
        assert app is not None
        assert app.name == "Snippets"
        assert app.uri_path == "/snippets"
        assert app.css_class == "snippets"
        assert app.sidebar is True
        assert app.custom_ui is False
        assert app.app_schema is None

    def test_legacy_plugin_resolves_api_router(self) -> None:
        """Carry the resolved API router on the synthesized app."""
        registry = build_app_registry([App(name="Snippets", module_name="snippets")])
        app = registry.get("snippets")
        assert isinstance(app.api_router, APIRouter)

    def test_module_name_only_derives_metadata(self, mocker: MockerFixture) -> None:
        """A MODULE_NAME-only entry derives name/uri/css/display from the key."""
        _force_legacy_synthesis(mocker)
        registry = build_app_registry([App(module_name="alters")])
        app = registry.get("alters")
        assert app.name == "alters"
        assert app.uri_path == "/alters"
        assert app.css_class == "alters"
        assert app.display_name == "alters"

    def test_api_router_is_none_when_opted_out(self, mocker: MockerFixture) -> None:
        """A plugin opting out of the API mount carries ``api_router is None``."""
        _force_legacy_synthesis(mocker)
        registry = build_app_registry(
            [App(name="Alters", module_name="alters", api_router_path=None)]
        )
        assert registry.get("alters").api_router is None

    def test_legacy_plugin_carries_group_and_nav_order(self) -> None:
        """Carry ``group``/``nav_order`` onto the synthesized app from the plugin entry."""
        nav_order = 5
        registry = build_app_registry(
            [
                App(
                    name="Snippets",
                    module_name="snippets",
                    group="alerts",
                    nav_order=nav_order,
                )
            ]
        )
        app = registry.get("snippets")
        assert app.group == "alerts"
        assert app.nav_order == nav_order

    def test_legacy_plugin_without_grouping_is_ungrouped(
        self, mocker: MockerFixture
    ) -> None:
        """Carry ``None`` for ``group``/``nav_order`` when the plugin omits them."""
        _force_legacy_synthesis(mocker)
        registry = build_app_registry([App(name="Alters", module_name="alters")])
        app = registry.get("alters")
        assert app.group is None
        assert app.nav_order is None


class TestFailFast:
    """Tests for the fail-fast carried over from ``build_apps_router``."""

    def test_non_router_attribute_raises_type_error(
        self, mocker: MockerFixture
    ) -> None:
        """An ``api_router_path`` resolving to a non-``APIRouter`` raises ``TypeError``."""
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(),
        )
        plugin = App(
            name="Alters",
            module_name="alters",
            api_router_path="app.extensions.config.App",
        )
        with pytest.raises(TypeError, match="alters"):
            build_app_registry([plugin])

    def test_empty_string_api_router_path_is_no_mount(
        self, mocker: MockerFixture
    ) -> None:
        """An empty-string ``api_router_path`` yields ``api_router is None``."""
        _force_legacy_synthesis(mocker)
        plugin = App.model_construct(
            name="Ghost",
            module_name="app.extensions.apps.alters",
            api_router_path="",
        )
        registry = build_app_registry([plugin])
        assert registry.get("alters").api_router is None


class TestOrderAndLookup:
    """Tests for activation-order preservation and key lookup."""

    def test_keys_preserve_activation_order(self) -> None:
        """``keys()`` mirrors the activation-list order (OpenAPI-critical)."""
        registry = build_app_registry(
            [
                App(name="Inventory", module_name="inventory"),
                App(name="Snippet Manager", module_name="snippets"),
                App(name="Checksums", module_name="checksums"),
            ]
        )
        assert registry.keys() == ["inventory", "snippets", "checksums"]

    def test_iteration_yields_entries_in_order(self) -> None:
        """Iterating the registry yields ``BaseApp`` entries in order."""
        registry = build_app_registry(
            [
                App(name="Inventory", module_name="inventory"),
                App(name="Snippet Manager", module_name="snippets"),
            ]
        )
        assert [app.key for app in registry] == ["inventory", "snippets"]

    def test_protected_app_surfaces_like_any_other(self) -> None:
        """The protected ``inventory`` app is a normal registry entry."""
        registry = build_app_registry([App(name="Inventory", module_name="inventory")])
        assert registry.get("inventory") is not None

    def test_get_returns_none_for_unknown_key(self) -> None:
        """An unconfigured key resolves to ``None``."""
        registry = build_app_registry([App(name="Inventory", module_name="inventory")])
        assert registry.get("nonexistent") is None


class TestCeleryDerivation:
    """Cover the registry-derived Celery include + seed-prefix accessors."""

    def _apps(self) -> list[App]:
        """Return a mixed activation list: two celery apps, one without, one opt-out.

        ``celery_module_path`` is auto-derived by a filesystem probe, so the
        celery-bearing exemplars must be apps that really ship a ``celery.py`` --
        a synthetic module name would derive ``None``.
        """
        return [
            App(name="Support diagnostics", module_name="atw"),
            App(name="Checksums", module_name="checksums"),
            App(name="Alerts", module_name="alerts"),
            App(name="Report", module_name="report", celery_module_path=None),
        ]

    def test_module_paths_ordered_and_filtered(self) -> None:
        """Return only celery-bearing apps, in activation order, opt-out excluded."""
        assert app_celery_module_paths(self._apps()) == [
            "app.extensions.apps.atw.celery",
            "app.extensions.apps.alerts.celery",
        ]

    def test_explicit_override_is_used(self) -> None:
        """Surface an explicit ``celery_module_path`` string verbatim."""
        apps = [App(module_name="checksums", celery_module_path="app.tasks.celery")]
        assert app_celery_module_paths(apps) == ["app.tasks.celery"]

    def test_build_include_prepends_static_base(self) -> None:
        """Compose the static base first, then the app modules."""
        assert build_celery_include(self._apps()) == [
            *STATIC_CELERY_INCLUDE,
            "app.extensions.apps.atw.celery",
            "app.extensions.apps.alerts.celery",
        ]

    def test_build_include_empty_apps_is_static_base_only(self) -> None:
        """Return exactly the static base when no apps are configured."""
        assert build_celery_include([]) == list(STATIC_CELERY_INCLUDE)

    def test_static_base_carries_the_app_independent_modules(self) -> None:
        """Pin the base: tasks service, snippet sync, drain, PMM Extensions override refresher.

        The last three must register in an image shipping no app with a
        ``celery.py``, so they cannot be registry-derived.
        """
        assert STATIC_CELERY_INCLUDE == (
            "app.tasks.celery",
            "app.extensions.snippets.celery",
            "app.extensions.app_drain",
            "app.extensions.settings_override",
        )

    def test_build_include_dedupes_static_base_collision(self) -> None:
        """Drop an app module that duplicates the static base, keeping first-seen order."""
        apps = [App(module_name="checksums", celery_module_path="app.tasks.celery")]
        assert build_celery_include(apps) == list(STATIC_CELERY_INCLUDE)

    def test_module_for_key(self) -> None:
        """Map an app key to its celery module."""
        assert (
            app_celery_module_for("atw", self._apps())
            == "app.extensions.apps.atw.celery"
        )

    def test_module_for_key_none_when_absent_or_opted_out(self) -> None:
        """Resolve unknown keys and opt-out/no-celery apps to ``None``."""
        apps = self._apps()
        assert app_celery_module_for("checksums", apps) is None
        assert app_celery_module_for("report", apps) is None
        assert app_celery_module_for("nonexistent", apps) is None


class TestDefinitionCollection:
    """Tests for the future declarative-definition collection path."""

    @staticmethod
    def _patch_definition(mocker: MockerFixture, definition: BaseApp) -> None:
        """Make ``import_module`` return a stub module exporting ``definition``."""
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(app=definition),
        )

    def test_exported_definition_is_used_and_stamped(
        self, mocker: MockerFixture
    ) -> None:
        """A module exporting ``app`` is used, with key/enabled stamped."""
        api_router = APIRouter()
        definition = BaseApp(
            name="internal",
            display_name="Internal Display",
            uri_path="/def-path",
            css_class="def-css",
            sidebar=False,
            custom_ui=True,
            api_router=api_router,
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry([App(module_name="checksums")])
        app = registry.get("checksums")
        assert app.key == "checksums"
        assert app.enabled is True
        assert app.name == "internal"
        assert app.uri_path == "/def-path"
        assert app.sidebar is False
        assert app.custom_ui is True
        assert app.api_router is api_router
        assert app.display_name == "Internal Display"

    def test_explicit_yaml_keys_override_definition(
        self, mocker: MockerFixture
    ) -> None:
        """Explicit legacy YAML keys win over the definition's values."""
        definition = BaseApp(
            name="internal", uri_path="/def-path", css_class="def-css", custom_ui=True
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry(
            [App(name="Yaml Override", module_name="checksums")]
        )
        app = registry.get("checksums")
        assert app.name == "Yaml Override"
        assert app.uri_path == "/yaml-override"
        assert app.css_class == "yaml-override"
        assert app.custom_ui is True
        assert app.display_name == "Yaml Override"

    def test_explicit_display_name_survives_yaml_name_override(
        self, mocker: MockerFixture
    ) -> None:
        """A definition's distinct ``display_name`` is kept when YAML overrides name."""
        definition = BaseApp(
            name="internal", display_name="Internal Display", uri_path="/def-path"
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry(
            [App(name="Yaml Override", module_name="checksums")]
        )
        app = registry.get("checksums")
        assert app.name == "Yaml Override"
        assert app.display_name == "Internal Display"

    def test_enabled_stamped_from_activation_entry(self, mocker: MockerFixture) -> None:
        """A disabled activation entry stamps ``enabled=False`` on the definition."""
        definition = BaseApp(name="internal", uri_path="/def-path")
        self._patch_definition(mocker, definition)

        registry = build_app_registry([App(module_name="checksums", enabled=False)])
        assert registry.get("checksums").enabled is False

    def test_definition_grouping_used_when_yaml_silent(
        self, mocker: MockerFixture
    ) -> None:
        """Keep the definition's ``group``/``nav_order`` when YAML omits them."""
        nav_order = 8
        definition = BaseApp(
            name="internal", uri_path="/def-path", group="backups", nav_order=nav_order
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry([App(module_name="checksums")])
        app = registry.get("checksums")
        assert app.group == "backups"
        assert app.nav_order == nav_order

    def test_explicit_yaml_grouping_binds_onto_absent_definition_grouping(
        self, mocker: MockerFixture
    ) -> None:
        """Bind explicit YAML ``GROUP``/``NAV_ORDER`` when the definition omits them."""
        nav_order = 6
        definition = BaseApp(name="internal", uri_path="/def-path")
        self._patch_definition(mocker, definition)

        registry = build_app_registry(
            [App(module_name="checksums", group="schema_change", nav_order=nav_order)]
        )
        app = registry.get("checksums")
        assert app.group == "schema_change"
        assert app.nav_order == nav_order

    def test_omitted_yaml_grouping_does_not_override_definition(
        self, mocker: MockerFixture
    ) -> None:
        """Keep the definition's values when the YAML entry omits ``GROUP``/``NAV_ORDER``."""
        nav_order = 8
        definition = BaseApp(
            name="internal", uri_path="/def-path", group="backups", nav_order=nav_order
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry(
            [App(name="Yaml Override", module_name="checksums")]
        )
        app = registry.get("checksums")
        assert app.group == "backups"
        assert app.nav_order == nav_order

    def test_explicit_null_yaml_grouping_forces_ungrouped(
        self, mocker: MockerFixture
    ) -> None:
        """Bind ``group``/``nav_order`` to ``None`` on an explicit YAML ``GROUP: null``."""
        definition = BaseApp(
            name="internal", uri_path="/def-path", group="backups", nav_order=8
        )
        self._patch_definition(mocker, definition)

        registry = build_app_registry(
            [App(module_name="checksums", group=None, nav_order=None)]
        )
        app = registry.get("checksums")
        assert app.group is None
        assert app.nav_order is None


class TestScopedKeyDerivation:
    """Tests for the module-path-derived scoped app key and its override."""

    @staticmethod
    def _patch_definition(mocker: MockerFixture, definition: BaseApp) -> None:
        """Make ``import_module`` return a stub module exporting ``definition``."""
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(app=definition),
        )

    def test_nested_module_derives_scoped_key(self, mocker: MockerFixture) -> None:
        """Derive a ``/``-joined scoped key from a nested ``MODULE_NAME``."""
        self._patch_definition(
            mocker, BaseApp(name="internal", uri_path="/mysql_backups/restores")
        )
        registry = build_app_registry([App(module_name="mysql_backups.restore")])
        assert registry.keys() == ["mysql_backups/restore"]

    def test_top_level_module_keeps_single_segment_key(
        self, mocker: MockerFixture
    ) -> None:
        """Keep a top-level ``MODULE_NAME`` single-segment key unchanged."""
        self._patch_definition(mocker, BaseApp(name="internal", uri_path="/checksums"))
        registry = build_app_registry([App(module_name="checksums")])
        assert registry.keys() == ["checksums"]

    def test_explicit_definition_key_overrides_derivation(
        self, mocker: MockerFixture
    ) -> None:
        """Honor an explicit ``key`` on the definition over the derived key."""
        self._patch_definition(
            mocker,
            BaseApp(
                key="mysql_backups/restores",
                name="internal",
                uri_path="/mysql_backups/restores",
            ),
        )
        registry = build_app_registry([App(module_name="mysql_backups.restore")])
        assert registry.get("mysql_backups/restores") is not None
        assert registry.get("mysql_backups/restore") is None


class TestGetAppRegistry:
    """Tests for the lazy cached accessor over ``extensions_settings.APPS``."""

    def test_returns_registry_over_configured_plugins(self) -> None:
        """Cover every configured plugin in the registry, each followed by its children."""
        registry = get_app_registry()
        assert isinstance(registry, AppRegistry)
        expected = []
        for p in extensions_settings.APPS:
            expected.append(
                p.module_name.removeprefix("app.extensions.apps.").replace(".", "/")
            )
            definition = getattr(importlib.import_module(p.module_name), "app", None)
            if isinstance(definition, BaseApp):
                expected.extend(child.key for child in definition.child_apps)
        assert registry.keys() == expected

    def test_result_is_cached(self) -> None:
        """Repeated calls return the same cached instance."""
        assert get_app_registry() is get_app_registry()

    def test_alters_binds_as_task_execution_app_with_nav_metadata(self) -> None:
        """Resolve alters to a derived ``TaskExecutionApp`` carrying nav metadata.

        Only the list/schema is derived; the schema is the ``alters_schema``
        passthrough and every mutation stays custom on the ``extra_routes`` router,
        so the derived ``api_router`` is not the hand-written router itself.
        """
        app = get_app_registry().get("alters")
        definition = importlib.import_module("app.extensions.apps.alters").app
        assert isinstance(app, TaskExecutionApp)
        assert app.display_name == definition.display_name
        assert app.uri_path == definition.uri_path
        assert app.css_class == definition.css_class
        assert app.group == definition.group
        assert app.nav_order == definition.nav_order

        api_routes = importlib.import_module("app.extensions.apps.alters.api_routes")
        schema = importlib.import_module("app.extensions.apps.alters.schema")
        assert app.app_schema is schema.alters_schema
        assert api_routes.router in app.extra_routes
        assert app.api_router is not api_routes.router


BESPOKE_BASE_APP_PLUGINS = [
    "alert_troubleshooting",
    "alerts",
    "dipper",
    "inventory",
    "report",
    "tasks",
]


class TestBespokeBaseAppDefinitions:
    """Cover the bespoke ``BaseApp`` definition wiring for every plugin in ``BESPOKE_BASE_APP_PLUGINS``."""

    @pytest.mark.parametrize("plugin", BESPOKE_BASE_APP_PLUGINS)
    def test_module_exports_bare_base_app(self, plugin: str) -> None:
        """Assert each plugin exports a bare ``BaseApp``, not a ``TaskExecutionApp``."""
        app = importlib.import_module(f"app.extensions.apps.{plugin}").app
        assert isinstance(app, BaseApp)
        assert not isinstance(app, TaskExecutionApp)

    @pytest.mark.parametrize("plugin", BESPOKE_BASE_APP_PLUGINS)
    def test_registry_binds_definition_routers(self, plugin: str) -> None:
        """Bind each plugin's API router by identity through the registry."""
        api_routes = importlib.import_module(f"app.extensions.apps.{plugin}.api_routes")
        app = get_app_registry().get(plugin)
        assert app.api_router is api_routes.router

    def test_tasks_definition_carries_schema(self) -> None:
        """Carry ``TASKS_PLUGIN_SCHEMA`` on the tasks definition's ``app_schema``."""
        assert get_app_registry().get("tasks").app_schema is TASKS_PLUGIN_SCHEMA

    @pytest.mark.parametrize(
        "plugin", ["alert_troubleshooting", "alerts", "dipper", "inventory", "report"]
    )
    def test_schemaless_plugins_have_no_app_schema(self, plugin: str) -> None:
        """Register the schemaless bespoke definitions without an ``app_schema``."""
        assert get_app_registry().get(plugin).app_schema is None

    @pytest.mark.parametrize("plugin", ["alert_troubleshooting", "atw"])
    def test_snippet_consumers_declare_no_required_apps(self, plugin: str) -> None:
        """Consume the snippets library without gating on the snippets app.

        Both read snippet *scripts* from ``app/extensions/snippets/``, which ships
        independently of the app package, so neither may re-acquire a
        ``requires_apps`` edge that would hide it when the snippets UI is off.
        """
        assert get_app_registry().get(plugin).requires_apps == ()

    @pytest.mark.parametrize("plugin", BESPOKE_BASE_APP_PLUGINS)
    def test_legacy_router_reexport_removed(self, plugin: str) -> None:
        """Assert no package still re-exports a ``router`` from the deleted Jinja layer."""
        package = importlib.import_module(f"app.extensions.apps.{plugin}")
        assert not hasattr(package, "router")


class TestAtwDefinition:
    """Cover the atw ``BaseApp`` definition."""

    def test_module_exports_bare_base_app(self) -> None:
        """Assert atw exports a bare ``BaseApp``, not a ``TaskExecutionApp``."""
        app = importlib.import_module("app.extensions.apps.atw").app
        assert isinstance(app, BaseApp)
        assert not isinstance(app, TaskExecutionApp)

    def test_registry_binds_api_router(self) -> None:
        """Bind atw's API router by identity."""
        api_routes = importlib.import_module("app.extensions.apps.atw.api_routes")
        app = get_app_registry().get("atw")
        assert app.api_router is api_routes.router

    def test_definition_carries_schema_and_nav_metadata(self) -> None:
        """Carry ``atw_schema``, ``custom_ui``, and nav metadata from the definition."""
        app = get_app_registry().get("atw")
        definition = importlib.import_module("app.extensions.apps.atw").app
        assert app.app_schema is atw_schema
        assert app.custom_ui is True
        assert app.group == definition.group
        assert app.nav_order == definition.nav_order


@pytest_asyncio.fixture(name="override_session")
async def override_session_fixture() -> AsyncIterator[AsyncSession]:
    """Provide an in-memory SQLite PMM Extensions session for app-state lookups."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()


def _checksums_declaring(mocker: MockerFixture, declared: Any) -> None:
    """Make the ``checksums`` app declare ``declared`` on its second import.

    ``collect_app_owned_settings_classes`` imports each activated module twice:
    once through ``build_app_registry`` and once to read the declaration. The
    first import must return the real package so the app resolves and registers
    its key; only the second is swapped for a stub carrying ``declared``.

    :param mocker: The ``pytest-mock`` fixture used to patch the import.
    :param declared: The value the stub exposes as
        ``APP_OWNED_SETTINGS_CLASSES``: a list of entries, or a malformed
        value when the test drives a rejection path.
    """
    fake_module = mocker.MagicMock()
    fake_module.APP_OWNED_SETTINGS_CLASSES = declared
    real_checksums = importlib.import_module("app.extensions.apps.checksums")
    import_calls = {"count": 0}

    def import_side_effect(name: str):
        if name == "app.extensions.apps.checksums":
            import_calls["count"] += 1
            if import_calls["count"] == 1:
                return real_checksums
            return fake_module
        return importlib.import_module(name)

    mocker.patch(
        "app.extensions.apps.framework.registry.import_module",
        side_effect=import_side_effect,
    )


class TestCollectAppOwnedSettingsClasses:
    """Tests for ``collect_app_owned_settings_classes``."""

    def test_collects_alerts_declaration(self) -> None:
        """Return the alerts app's own ``AlertsSettings`` entry."""
        entries = collect_app_owned_settings_classes([App(module_name="alerts")])
        assert len(entries) == 1
        entry = entries[0]
        assert entry.setting_class == AlertsSettings.__name__
        assert entry.app_key == "alerts"
        assert entry.settings_cls is AlertsSettings
        assert entry.proxy is alerts_settings
        assert entry.reseed_keys == frozenset({"BACKUP_INTERVAL"})

    def test_collects_report_declaration(self) -> None:
        """Return the report app's own ``HealthReportSettings`` entry."""
        entries = collect_app_owned_settings_classes([App(module_name="report")])
        assert len(entries) == 1
        entry = entries[0]
        assert entry.setting_class == HealthReportSettings.__name__
        assert entry.app_key == "report"
        assert entry.settings_cls is HealthReportSettings
        assert entry.proxy is health_report_settings
        assert entry.reseed_keys == frozenset()

    def test_reduced_activation_declares_no_alerts_entry(self) -> None:
        """Return no alerts entry under the PMM-embedded activation list."""
        entries = collect_app_owned_settings_classes(REDUCED_ACTIVATION)
        assert AlertsSettings.__name__ not in {entry.setting_class for entry in entries}
        assert HealthReportSettings.__name__ not in {
            entry.setting_class for entry in entries
        }
        assert "alerts" not in {entry.app_key for entry in entries}
        assert "report" not in {entry.app_key for entry in entries}

    def test_skips_plugins_without_declaration(self) -> None:
        """Ignore activation entries that export no ``APP_OWNED_SETTINGS_CLASSES``."""
        entries = collect_app_owned_settings_classes([App(module_name="checksums")])
        assert entries == []

    def test_rejects_duplicate_setting_class(self, mocker: MockerFixture) -> None:
        """Fail when the same settings class is declared by two distinct apps.

        Two entries share ``AlertsSettings`` across two distinctly-keyed apps
        (``alerts`` and ``checksums``), so the duplicate-setting-class guard --
        not the duplicate-app-key guard -- is what trips.
        """
        dup_entry = AppOwnedClassEntry(
            setting_class=AlertsSettings.__name__,
            settings_cls=AlertsSettings,
            proxy=alerts_settings,
            app_key="checksums",
        )
        _checksums_declaring(mocker, [dup_entry])
        with pytest.raises(ValueError, match="more than one app-owned"):
            collect_app_owned_settings_classes(
                [App(module_name="alerts"), App(module_name="checksums")],
            )

    def test_rejects_unknown_app_key(self, mocker: MockerFixture) -> None:
        """Fail when an entry references an app key absent from the registry."""
        fake_entry = AppOwnedClassEntry(
            setting_class=AlertsSettings.__name__,
            settings_cls=AlertsSettings,
            proxy=alerts_settings,
            app_key="ghost",
        )
        _checksums_declaring(mocker, [fake_entry])
        with pytest.raises(ValueError, match="unknown app key 'ghost'"):
            collect_app_owned_settings_classes([App(module_name="checksums")])

    def test_rejects_non_list_declaration(self, mocker: MockerFixture) -> None:
        """Fail when ``APP_OWNED_SETTINGS_CLASSES`` is not a list."""
        _checksums_declaring(mocker, "not-a-list")
        with pytest.raises(TypeError, match="must be a list"):
            collect_app_owned_settings_classes([App(module_name="checksums")])

    def test_rejects_non_entry_list_items(self, mocker: MockerFixture) -> None:
        """Fail when list items are not ``AppOwnedClassEntry`` instances."""
        _checksums_declaring(mocker, ["not-an-entry"])
        with pytest.raises(TypeError, match="AppOwnedClassEntry"):
            collect_app_owned_settings_classes([App(module_name="checksums")])

    def test_rejects_unknown_reseed_key(self, mocker: MockerFixture) -> None:
        """Fail when ``reseed_keys`` names a field absent from ``settings_cls``.

        A typo'd or renamed field must fail collection rather than silently
        registering a beat-reseed callback that can never fire.
        """
        fake_entry = AppOwnedClassEntry(
            setting_class=AlertsSettings.__name__,
            settings_cls=AlertsSettings,
            proxy=alerts_settings,
            app_key="checksums",
            reseed_keys=frozenset({"BACKUP_INTERVL"}),
        )
        _checksums_declaring(mocker, [fake_entry])
        with pytest.raises(ValueError, match="not a hot-reloadable field"):
            collect_app_owned_settings_classes([App(module_name="checksums")])

    def test_rejects_non_hot_reseed_key(self, mocker: MockerFixture) -> None:
        """Fail when ``reseed_keys`` names a real field that is not marked HOT.

        ``cleanup_interval`` exists on ``HealthReportSettings`` but carries no
        HOT marker, pinning the HOT half of the check separately from the
        field-existence half covered by ``test_rejects_unknown_reseed_key``.
        """
        fake_entry = AppOwnedClassEntry(
            setting_class=HealthReportSettings.__name__,
            settings_cls=HealthReportSettings,
            proxy=health_report_settings,
            app_key="checksums",
            reseed_keys=frozenset({"cleanup_interval"}),
        )
        _checksums_declaring(mocker, [fake_entry])
        with pytest.raises(ValueError, match="not a hot-reloadable field"):
            collect_app_owned_settings_classes([App(module_name="checksums")])

    def test_accepts_reseed_key_withheld_by_the_allowlist(
        self, mocker: MockerFixture
    ) -> None:
        """Accept a ``reseed_keys`` entry the runtime allowlist would withhold.

        Collection-time validation reads the static declaration only
        (``include_policy_gate=False``): a deployment that narrows
        ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` so it no longer names
        ``AlertsSettings.BACKUP_INTERVAL`` must not fail startup.
        """
        mocker.patch.object(
            settings.SETTINGS_OVERRIDE, "ALLOWED_KEYS", {"ExtensionsSettings.APP_DRAIN"}
        )
        entries = collect_app_owned_settings_classes([App(module_name="alerts")])
        assert entries[0].reseed_keys == frozenset({"BACKUP_INTERVAL"})


@pytest.mark.asyncio
class TestResolveAppSettingsMetadata:
    """Test ``resolve_app_settings_metadata``."""

    async def test_returns_alerts_identity(
        self, override_session: AsyncSession
    ) -> None:
        """Resolve display name and enabled state for the alerts app."""
        metadata = await resolve_app_settings_metadata(override_session, "alerts")
        assert metadata.is_app_owned is True
        assert metadata.app_id == "alerts"
        assert metadata.app_display_name == "Alert Templates"
        assert metadata.app_enabled is True

    async def test_reports_disabled_app(
        self,
        override_session: AsyncSession,
    ) -> None:
        """Report ``app_enabled=False`` when the owning app is disabled in the DB."""
        override_session.add(
            AppState(app_key="alerts", lifecycle_state=AppLifecycleEnum.DISABLED),
        )
        await override_session.commit()

        metadata = await resolve_app_settings_metadata(override_session, "alerts")
        assert metadata.app_id == "alerts"
        assert metadata.app_enabled is False

    async def test_unknown_app_key_raises(self, override_session: AsyncSession) -> None:
        """Reject metadata resolution for an unregistered app key."""
        with pytest.raises(ValueError, match="Unknown app key 'ghost'"):
            await resolve_app_settings_metadata(override_session, "ghost")


TASK_EXECUTION_PLUGINS_WITH_CSS_CLASS = ["backup_pg", "checksums"]


class TestTaskExecutionAppCssClass:
    """Verify ``css_class`` resolves from the ``AppDefinition`` for plugins that previously relied on ``settings.yaml``."""

    @pytest.mark.parametrize("plugin", TASK_EXECUTION_PLUGINS_WITH_CSS_CLASS)
    def test_css_class_comes_from_definition(self, plugin: str) -> None:
        """Assert the registry-bound ``css_class`` equals the definition's ``css_class``."""
        definition = importlib.import_module(f"app.extensions.apps.{plugin}.app").app
        bound = get_app_registry().get(plugin)
        assert bound.css_class == definition.css_class

    @pytest.mark.parametrize("plugin", TASK_EXECUTION_PLUGINS_WITH_CSS_CLASS)
    def test_definition_declares_css_class(self, plugin: str) -> None:
        """Assert the definition itself sets a non-empty ``css_class``."""
        definition = importlib.import_module(f"app.extensions.apps.{plugin}.app").app
        assert definition.css_class, f"{plugin} definition must declare css_class"


def _child_app(key: str, *, parent_key: str) -> BaseApp:
    """Build a minimal parent-bound child ``BaseApp`` for registry tests."""
    return BaseApp(
        key=key,
        name=key.replace("/", "_"),
        display_name=key,
        uri_path=f"/{key}",
        parent_key=parent_key,
    )


def _parent_app(*children: BaseApp) -> BaseApp:
    """Build a minimal parent ``BaseApp`` carrying ``children`` as ``child_apps``."""
    return BaseApp(
        key="parent_app",
        name="parent_app",
        display_name="Parent App",
        uri_path="/parent_app",
        child_apps=children,
    )


def _parent_plugin(*, enabled: bool = True) -> App:
    """Build an activation entry for the synthetic parent module.

    Bypass ``App``'s on-disk module-existence validation via ``model_construct``
    — ``import_module`` is mocked to yield the synthetic parent definition, so
    the module need not exist on disk.
    """
    return App.model_construct(name="Parent", module_name="parent_app", enabled=enabled)


def _dep_app(key: str, *, requires_apps: tuple[str, ...] = ()) -> BaseApp:
    """Build a minimal top-level ``BaseApp`` carrying ``requires_apps``."""
    return BaseApp(
        key=key,
        name=key,
        display_name=key,
        uri_path=f"/{key}",
        requires_apps=requires_apps,
    )


class TestRequiresAppsValidation:
    """Cover build-time validation of ``requires_apps`` and app keys."""

    def test_duplicate_app_key_raises(self) -> None:
        """Reject two apps sharing a key rather than silently collapsing them."""
        with pytest.raises(ValueError, match=r"duplicate|Duplicate"):
            AppRegistry([_dep_app("dup"), _dep_app("dup")])

    def test_dangling_dependency_raises(self) -> None:
        """Reject a ``requires_apps`` key that resolves to no registered app."""
        with pytest.raises(ValueError, match="ghost"):
            AppRegistry([_dep_app("a", requires_apps=("ghost",))])

    def test_self_dependency_raises(self) -> None:
        """Reject an app that depends on itself."""
        with pytest.raises(ValueError, match=r"itself|self"):
            AppRegistry([_dep_app("a", requires_apps=("a",))])

    def test_dependency_cycle_raises(self) -> None:
        """Reject a dependency cycle across apps."""
        with pytest.raises(ValueError, match=r"cycle|Cycle"):
            AppRegistry(
                [
                    _dep_app("a", requires_apps=("b",)),
                    _dep_app("b", requires_apps=("a",)),
                ]
            )

    def test_valid_dependency_graph_builds(self) -> None:
        """Build a well-formed dependency graph without error."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.keys() == ["a", "b"]

    def test_declared_dependency_survives_onto_the_built_app(self) -> None:
        """Read a declared ``requires_apps`` tuple back off the registered app.

        Synthetic stand-in for the retired live-registry assertion: no shipped app
        declares ``requires_apps`` any more, so the declaration-to-registry
        round-trip needs its own fixture to stay covered.
        """
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.get("a").requires_apps == ("b",)
        assert registry.get("b").requires_apps == ()

    def test_child_apps_with_requires_apps_raises(self) -> None:
        """Reject combining ``child_apps`` with ``requires_apps``.

        A child resolves its own state through the parent's ``state_key`` but does
        not inherit the parent's ``requires_apps``, so the combination would leave
        a child reachable while its parent is gated -- reject it until supported.
        """
        child = BaseApp(
            key="parent_app/sub",
            name="sub",
            display_name="Sub",
            uri_path="/parent_app/sub",
            parent_key="parent_app",
        )
        parent = BaseApp(
            key="parent_app",
            name="parent_app",
            display_name="Parent App",
            uri_path="/parent_app",
            child_apps=(child,),
            requires_apps=("dep",),
        )
        with pytest.raises(ValueError, match="child_apps"):
            AppRegistry([parent, _dep_app("dep")])


class TestEffectiveEnabled:
    """Cover the centralized effective-enabled resolver."""

    def test_enabled_when_own_and_dep_enabled(self) -> None:
        """Resolve effective-enabled when an app and its dependency are enabled."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_effective_enabled("a", {}) is True

    def test_disabled_when_own_disabled(self) -> None:
        """Gate an app when its own state is disabled."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        states = {"a": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("a", states) is False

    def test_disabled_when_dependency_disabled(self) -> None:
        """Gate an app when a dependency is disabled."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        states = {"b": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("a", states) is False

    @pytest.mark.parametrize(
        "state",
        [
            AppLifecycleEnum.DISABLING,
            AppLifecycleEnum.ENABLING,
        ],
    )
    def test_disabled_when_dependency_not_fully_enabled(
        self, state: AppLifecycleEnum
    ) -> None:
        """Gate the dependent app for any dependency state other than ENABLED."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_effective_enabled("a", {"b": state}) is False

    def test_missing_row_treated_as_enabled(self) -> None:
        """Treat a missing ``AppState`` row as ENABLED for every node."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_effective_enabled("a", {}) is True

    def test_unknown_key_resolves_disabled(self) -> None:
        """Resolve an unregistered key to ``False`` rather than raising."""
        registry = AppRegistry([_dep_app("a")])
        assert registry.resolve_effective_enabled("ghost", {}) is False

    @pytest.mark.parametrize(
        "state",
        [
            AppLifecycleEnum.DISABLING,
            AppLifecycleEnum.ENABLING,
        ],
    )
    def test_disabled_when_own_state_not_fully_enabled(
        self, state: AppLifecycleEnum
    ) -> None:
        """Gate an app for any of its own states other than ENABLED."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_effective_enabled("a", {"a": state}) is False

    def test_disabled_when_one_of_several_deps_disabled(self) -> None:
        """Gate an app when any one of its multiple dependencies is disabled."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b", "c")),
                _dep_app("b"),
                _dep_app("c"),
            ]
        )
        assert registry.resolve_effective_enabled("a", {}) is True
        states = {"c": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("a", states) is False

    def test_child_app_gated_through_parent_state(self) -> None:
        """Gate a child app via its parent's ``AppState`` (child owns no row).

        A child resolves its own enabled state through ``state_key`` (its
        ``parent_key``), so disabling the parent gates the child.
        """
        registry = AppRegistry(
            [
                _dep_app("parent_app"),
                _child_app("parent_app/restore", parent_key="parent_app"),
            ]
        )
        assert registry.resolve_effective_enabled("parent_app/restore", {}) is True
        states = {"parent_app": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("parent_app/restore", states) is False

    def test_protected_dependency_is_always_enabled(self) -> None:
        """Never gate on a protected dependency, even when its row says disabled."""
        registry = AppRegistry(
            [_dep_app("a", requires_apps=("inventory",)), _dep_app("inventory")]
        )
        states = {"inventory": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("a", states) is True

    def test_protected_app_ignores_its_own_disabled_dependency(self) -> None:
        """Keep a dependent enabled when a protected middle node has a disabled dep.

        ``inventory`` is protected, so ``consumer -> inventory -> snippets`` must
        not gate ``consumer`` when ``snippets`` is disabled: a protected node is
        unconditionally enabled and its own ``requires_apps`` are never walked.
        """
        registry = AppRegistry(
            [
                _dep_app("consumer", requires_apps=("inventory",)),
                _dep_app("inventory", requires_apps=("snippets",)),
                _dep_app("snippets"),
            ]
        )
        states = {"snippets": AppLifecycleEnum.DISABLED}
        assert registry.resolve_effective_enabled("consumer", states) is True

    def test_transitive_dependency_gates(self) -> None:
        """Gate the whole chain on a disabled transitive dependency."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b",)),
                _dep_app("b", requires_apps=("c",)),
                _dep_app("c"),
            ]
        )
        assert (
            registry.resolve_effective_enabled("a", {"c": AppLifecycleEnum.DISABLED})
            is False
        )
        assert registry.resolve_effective_enabled("a", {}) is True

    def test_cycle_at_resolve_time_fails_closed(self) -> None:
        """Gate an app off if a dependency cycle is present at resolve time.

        The build rejects cycles, so this guards the defensive branch against
        post-build mutation or an unexpected graph shape: a detected cycle must
        fail closed (gate the app off), never fall through to enabled.
        """
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        # Inject an ``a -> b -> a`` cycle past the build-time validation.
        registry._by_key["b"] = registry._by_key["b"].model_copy(
            update={"requires_apps": ("a",)}
        )
        assert registry.resolve_effective_enabled("a", {}) is False

    def test_memo_caches_shared_subtree_across_calls(self) -> None:
        """Reuse a memoized dependency result across a full-registry projection."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("shared",)),
                _dep_app("b", requires_apps=("shared",)),
                _dep_app("shared"),
            ]
        )
        memo: dict[str, bool] = {}
        assert registry.resolve_effective_enabled("a", {}, memo) is True
        assert registry.resolve_effective_enabled("b", {}, memo) is True
        # The shared subtree is resolved once and cached for later reuse.
        assert memo["shared"] is True

    def test_memo_matches_unmemoized_result_when_dep_disabled(self) -> None:
        """Produce identical gating with and without a memo for a disabled dep."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("shared",)),
                _dep_app("b", requires_apps=("shared",)),
                _dep_app("shared"),
            ]
        )
        states = {"shared": AppLifecycleEnum.DISABLED}
        memo: dict[str, bool] = {}
        assert registry.resolve_effective_enabled("a", states, memo) is False
        assert registry.resolve_effective_enabled("b", states, memo) is False


class TestResolveBlockingDependencies:
    """Cover reporting of the immediate disabled ``requires_apps`` blocker(s)."""

    def test_empty_when_effective_enabled(self) -> None:
        """Report no blocker when the app and its dependency are enabled."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_blocking_dependencies("a", {}) == ()

    def test_empty_when_self_disabled(self) -> None:
        """Report no blocker when the app is disabled by its own state.

        A self-disabled app keeps the generic splash, so the blocker list is empty
        even when a dependency also happens to be off -- own-state disablement
        takes precedence over dependency-driven disablement.
        """
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        states = {
            "a": AppLifecycleEnum.DISABLED,
            "b": AppLifecycleEnum.DISABLED,
        }
        assert registry.resolve_blocking_dependencies("a", states) == ()

    def test_reports_disabled_dependency(self) -> None:
        """Name the disabled dependency when own state is enabled but a dep is off."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        states = {"b": AppLifecycleEnum.DISABLED}
        assert registry.resolve_blocking_dependencies("a", states) == ("b",)

    def test_reports_only_disabled_of_several_deps(self) -> None:
        """List only the disabled dependencies, preserving declaration order."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b", "c", "d")),
                _dep_app("b"),
                _dep_app("c"),
                _dep_app("d"),
            ]
        )
        states = {
            "d": AppLifecycleEnum.DISABLED,
            "b": AppLifecycleEnum.DISABLED,
        }
        assert registry.resolve_blocking_dependencies("a", states) == ("b", "d")

    def test_reports_immediate_dependency_for_transitive_disable(self) -> None:
        """Name the immediate dependency, not the deep transitive cause.

        ``a -> b -> c`` with ``c`` off: ``a``'s blocker is ``b`` (its immediate
        effective-disabled dependency), while ``b``'s blocker is ``c``.
        """
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b",)),
                _dep_app("b", requires_apps=("c",)),
                _dep_app("c"),
            ]
        )
        states = {"c": AppLifecycleEnum.DISABLED}
        assert registry.resolve_blocking_dependencies("a", states) == ("b",)
        assert registry.resolve_blocking_dependencies("b", states) == ("c",)

    def test_protected_dependency_is_never_a_blocker(self) -> None:
        """Never list a protected dependency, even when its row says disabled."""
        registry = AppRegistry(
            [_dep_app("a", requires_apps=("inventory",)), _dep_app("inventory")]
        )
        states = {"inventory": AppLifecycleEnum.DISABLED}
        assert registry.resolve_blocking_dependencies("a", states) == ()

    def test_protected_app_reports_no_blocker(self) -> None:
        """Report no blocker for a protected app itself (never disabled)."""
        registry = AppRegistry(
            [_dep_app("inventory", requires_apps=("b",)), _dep_app("b")]
        )
        states = {"b": AppLifecycleEnum.DISABLED}
        assert registry.resolve_blocking_dependencies("inventory", states) == ()

    def test_unknown_key_reports_no_blocker(self) -> None:
        """Report no blocker for an unregistered key rather than raising."""
        registry = AppRegistry([_dep_app("a")])
        assert registry.resolve_blocking_dependencies("ghost", {}) == ()

    def test_shares_memo_with_effective_resolver(self) -> None:
        """Reuse a shared memo across effective-enabled and blocker resolution."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("shared",)),
                _dep_app("shared"),
            ]
        )
        states = {"shared": AppLifecycleEnum.DISABLED}
        memo: dict[str, bool] = {}
        assert registry.resolve_effective_enabled("a", states, memo) is False
        assert registry.resolve_blocking_dependencies("a", states, memo) == ("shared",)
        assert memo["shared"] is False

    def test_reports_every_dependency_when_all_disabled(self) -> None:
        """List the full dependency tuple, in declaration order, when all are off."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b", "c", "d")),
                _dep_app("b"),
                _dep_app("c"),
                _dep_app("d"),
            ]
        )
        states = {
            "b": AppLifecycleEnum.DISABLED,
            "c": AppLifecycleEnum.DISABLED,
            "d": AppLifecycleEnum.DISABLED,
        }
        assert registry.resolve_blocking_dependencies("a", states) == ("b", "c", "d")

    def test_empty_when_own_enabled_without_dependencies(self) -> None:
        """Report no blocker for an enabled app that declares no dependencies."""
        registry = AppRegistry([_dep_app("a")])
        assert registry.resolve_blocking_dependencies("a", {}) == ()

    @pytest.mark.parametrize(
        "state",
        [AppLifecycleEnum.DISABLING, AppLifecycleEnum.ENABLING],
    )
    def test_reports_dependency_in_transitional_state(
        self, state: AppLifecycleEnum
    ) -> None:
        """Name a dependency stuck mid-transition, since only ENABLED counts as on."""
        registry = AppRegistry([_dep_app("a", requires_apps=("b",)), _dep_app("b")])
        assert registry.resolve_blocking_dependencies("a", {"b": state}) == ("b",)

    def test_shares_memo_across_sibling_dependencies_in_one_call(self) -> None:
        """Resolve a shared sub-dependency once when two blockers both require it.

        ``a`` requires ``b`` and ``c``, both of which require a disabled ``shared``.
        Within a single call the generator resolves ``b`` and ``c`` in turn; the
        threaded memo caches ``shared`` so it is walked once, and both siblings are
        reported as immediate blockers.
        """
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("b", "c")),
                _dep_app("b", requires_apps=("shared",)),
                _dep_app("c", requires_apps=("shared",)),
                _dep_app("shared"),
            ]
        )
        states = {"shared": AppLifecycleEnum.DISABLED}
        memo: dict[str, bool] = {}
        assert registry.resolve_blocking_dependencies("a", states, memo) == ("b", "c")
        assert memo["shared"] is False

    def test_follows_declaration_order_not_registration_order(self) -> None:
        """Order blockers by ``requires_apps`` declaration, not registration order."""
        registry = AppRegistry(
            [
                _dep_app("a", requires_apps=("c", "b")),
                _dep_app("b"),
                _dep_app("c"),
            ]
        )
        states = {
            "b": AppLifecycleEnum.DISABLED,
            "c": AppLifecycleEnum.DISABLED,
        }
        assert registry.resolve_blocking_dependencies("a", states) == ("c", "b")


class TestChildApps:
    """Cover ``child_apps`` structural registration in ``build_app_registry``."""

    def test_child_is_registered_right_after_parent(
        self, mocker: MockerFixture
    ) -> None:
        """Register a parent's ``child_apps`` entry immediately after the parent."""
        child = _child_app("parent_app/restore", parent_key="parent_app")
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(app=_parent_app(child)),
        )
        registry = build_app_registry([_parent_plugin()])

        assert registry.keys() == ["parent_app", "parent_app/restore"]

    def test_child_has_no_settings_entry_yet_resolves(
        self, mocker: MockerFixture
    ) -> None:
        """Resolve the child by its scoped key though no ``App`` entry declares it."""
        child = _child_app("parent_app/restore", parent_key="parent_app")
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(app=_parent_app(child)),
        )
        registry = build_app_registry([_parent_plugin()])

        assert registry.get("parent_app/restore").parent_key == "parent_app"

    def test_child_enabled_is_stamped_from_parent(self, mocker: MockerFixture) -> None:
        """Derive the child's ``enabled`` from the bound parent."""
        child = _child_app("parent_app/restore", parent_key="parent_app")
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=SimpleNamespace(app=_parent_app(child)),
        )
        registry = build_app_registry([_parent_plugin(enabled=False)])

        assert registry.get("parent_app/restore").enabled is False


class TestCollectInventoryReferenceProviders:
    """Cover the inventory-reference providers activated apps declare."""

    def test_collects_the_mysql_backups_declaration(self) -> None:
        """Return the backup catalog's declared provider."""
        providers = collect_inventory_reference_providers(
            [App(module_name="mysql_backups")]
        )
        assert providers == [referenced_inventory_entities]

    def test_skips_plugins_without_declaration(self) -> None:
        """Ignore activation entries exporting no ``INVENTORY_REFERENCE_PROVIDERS``."""
        assert (
            collect_inventory_reference_providers([App(module_name="checksums")]) == []
        )

    def test_preserves_activation_order(self) -> None:
        """Return providers in the order the activation list declares them."""
        providers = collect_inventory_reference_providers(
            [App(module_name="checksums"), App(module_name="mysql_backups")]
        )
        assert providers == [referenced_inventory_entities]

    def test_rejects_a_non_list_declaration(self, mocker: MockerFixture) -> None:
        """Fail fast when an app exports the wrong container type."""
        fake_module = mocker.MagicMock()
        fake_module.INVENTORY_REFERENCE_PROVIDERS = referenced_inventory_entities
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=fake_module,
        )

        with pytest.raises(TypeError, match="must be a list"):
            collect_inventory_reference_providers([App(module_name="mysql_backups")])

    def test_rejects_a_non_callable_entry(self, mocker: MockerFixture) -> None:
        """Fail fast when a declared entry cannot be invoked as a provider."""
        fake_module = mocker.MagicMock()
        fake_module.INVENTORY_REFERENCE_PROVIDERS = ["not-a-callable"]
        mocker.patch(
            "app.extensions.apps.framework.registry.import_module",
            return_value=fake_module,
        )

        with pytest.raises(TypeError, match="must be callable"):
            collect_inventory_reference_providers([App(module_name="mysql_backups")])


def _task_app_offering(owner: str, key: str, *, scheduling: bool) -> TaskExecutionApp:
    """Build a synthetic task app under ``owner`` with the given scheduling flag.

    :param owner: The ``Task.owner`` the app claims.
    :param key: The registry key, unique within the registry under test.
    :param scheduling: Whether the app's served schema declares ``scheduling``.
    :return: The synthetic definition.
    """
    return synth_app(
        key=key,
        owner=owner,
        views=replace(
            synth_app_kwargs()["views"],
            capabilities=Capabilities(scheduling=scheduling),
        ),
    )


class TestSchedulingOwners:
    """Cover the owner-level scheduling predicate and the sweep's owner set."""

    def test_an_owner_whose_app_offers_scheduling(self) -> None:
        """Allow an owner every registered task app carrying it offers scheduling for."""
        assert get_app_registry().owner_offers_scheduling(BACKUPS_OWNER) is True

    @pytest.mark.parametrize(
        "owner",
        [RESTORES_OWNER, RESTORE_MONGO_OWNER, ANY_OWNER, "NOT_A_REGISTERED_OWNER"],
    )
    def test_refused_owners(self, owner: str) -> None:
        """Refuse a restore owner, ``ANY_OWNER``, and an owner no app carries."""
        assert get_app_registry().owner_offers_scheduling(owner) is False

    def test_unschedulable_task_owners(self) -> None:
        """Report exactly the restore owners as the set the startup sweep covers."""
        assert get_app_registry().unschedulable_task_owners() == frozenset(
            {RESTORES_OWNER, RESTORE_MONGO_OWNER}
        )

    def test_a_shared_owner_needs_every_app_to_offer_scheduling(self) -> None:
        """Refuse an owner two apps share when one of them withholds scheduling."""
        registry = AppRegistry(
            [
                _task_app_offering("SHARED", "shared-yes", scheduling=True),
                _task_app_offering("SHARED", "shared-no", scheduling=False),
            ]
        )

        assert registry.owner_offers_scheduling("SHARED") is False
        assert "SHARED" in registry.unschedulable_task_owners()

    def test_any_owner_is_refused_and_never_swept(self) -> None:
        """Refuse ``ANY_OWNER`` even when its app advertises scheduling, and skip it.

        An app declaring ``ANY_OWNER`` does not speak for the unclaimed tasks that
        default to it, so its schedules are neither permitted nor switched off.
        """
        registry = AppRegistry(
            [_task_app_offering(ANY_OWNER, "any-owner-app", scheduling=True)]
        )

        assert registry.owner_offers_scheduling(ANY_OWNER) is False
        assert registry.unschedulable_task_owners() == frozenset()
