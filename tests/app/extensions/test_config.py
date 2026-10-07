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

"""Define tests for the app.extensions.config module."""

import json
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from string import Template
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from app.core.exceptions import HTTPServiceUnavailableException
from app.core.requests import RemoteAPI
from app.core.settings_override.registry import (
    chain_is_locked,
    is_advanced_field,
    is_hot_reloadable,
    is_nested_overridable_parent,
    MaterializerContext,
    MaterializerPurpose,
    SECRET_STR_MASK,
)
from app.extensions.apps.inventory.deps import get_syncers
from app.extensions.bundle_upload.plan import DeliveryPlan
from app.extensions.config import (
    App,
    AppDrainSettings,
    CookieOptions,
    DeliveryPlanInputs,
    extensions_settings,
    ExtensionsSettings,
    materialize_delivery_plan_inputs,
    prefixed_cookie_path,
    SyncerExtraKwargs,
    SyncOptions,
)
from app.extensions.sync.syncers.pmm import PMMSyncer


class TestCookieOptions:
    """Define tests for the CookieOptions model."""

    def test_default_dump_has_none_path(self):
        """Assert a bare CookieOptions dump keeps ``path=None`` (no explicit path)."""
        dumped = CookieOptions().model_dump(by_alias=True)
        assert dumped["key"] == "authToken"
        assert dumped["path"] is None

    def test_refresh_dump_carries_path(self):
        """Assert a SESSION_REFRESH-style instance exposes the configured path."""
        dumped = CookieOptions(
            COOKIE_NAME="refreshToken", PATH="/api/oauth"
        ).model_dump(by_alias=True)
        assert dumped["key"] == "refreshToken"
        assert dumped["path"] == "/api/oauth"


class TestSessionRefreshDefault:
    """Define tests for the ``ExtensionsSettings.SESSION_REFRESH`` default instance."""

    def test_session_refresh_defaults(self):
        """Assert the default SESSION_REFRESH instance targets /api/oauth."""
        settings = ExtensionsSettings()
        assert settings.SESSION_REFRESH.COOKIE_NAME == "refreshToken"
        assert settings.SESSION_REFRESH.PATH == "/api/oauth"


class TestAmbientSessionSSO:
    """Test the ambient Grafana SSO feature toggle."""

    def test_defaults_to_disabled(self):
        """Verify ambient SSO is opt-in, reading ``False`` through the proxy."""
        assert extensions_settings.AMBIENT_SESSION_SSO_ENABLED is False

    def test_is_hot_reloadable(self):
        """Verify the toggle is a hot field, so a DB override can enable it live."""
        assert is_hot_reloadable(ExtensionsSettings, "AMBIENT_SESSION_SSO_ENABLED")


class TestRootPath:
    """Cover the URL mount prefix PMM Extensions serves under."""

    def test_defaults_to_unprefixed(self):
        """Verify PMM Extensions serves from the origin root unless a deployment opts in."""
        assert ExtensionsSettings().ROOT_PATH == ""

    def test_is_not_hot_reloadable(self):
        """Verify a DB override cannot claim to move a prefix read at construction."""
        assert not is_hot_reloadable(ExtensionsSettings, "ROOT_PATH")


class TestPrefixedCookiePath:
    """Cover anchoring a configured cookie ``Path`` under the URL prefix."""

    def test_leaves_a_path_alone_without_a_prefix(self, mocker):
        """Emit today's ``Path`` unchanged, which is the regression contract."""
        mocker.patch.object(extensions_settings, "ROOT_PATH", new="")

        assert prefixed_cookie_path("/api/oauth") == "/api/oauth"

    def test_anchors_a_path_under_the_prefix(self, mocker):
        """Anchor the path so the browser sends the cookie to prefixed endpoints."""
        mocker.patch.object(extensions_settings, "ROOT_PATH", new="/extensions")

        assert prefixed_cookie_path("/api/oauth") == "/extensions/api/oauth"

    @pytest.mark.parametrize("root_path", ["", "/extensions"])
    def test_leaves_an_unset_path_unset(self, mocker, root_path):
        """Leave ``None`` alone: the browser derives a path that already carries the prefix."""
        mocker.patch.object(extensions_settings, "ROOT_PATH", new=root_path)

        assert prefixed_cookie_path(None) is None


class TestDiagnosticsDelivery:
    """Cover the ``DIAGNOSTICS_DELIVERY`` delivery-plan settings block."""

    @staticmethod
    def _plan_payload() -> dict:
        """Return a minimal valid delivery-plan mapping."""
        return {
            "endpoint": "https://snow.example.com/",
            "secrets": {"api_key": "s3cret"},
            "upload": {
                "path": "attachment/upload",
                "headers": {"x-sn-apikey": {"source": "secret", "name": "api_key"}},
            },
        }

    def test_defaults_to_not_configured(self):
        """Keep the block unset so a consumer can gate on ``None``."""
        assert (
            ExtensionsSettings(
                _env_file=None  # ty: ignore[unknown-argument]
            ).DIAGNOSTICS_DELIVERY
            is None
        )

    def test_is_not_overridable_from_the_database(self):
        """Keep the block out of the override layer, whole-object and per-leaf alike.

        A DB override would reach the plan either as a whole-object write whose
        secrets are stored masked, or as a per-leaf merge that never re-runs the
        cross-reference validator. Env and YAML are the only write paths.
        """
        assert not is_hot_reloadable(ExtensionsSettings, "DIAGNOSTICS_DELIVERY")
        assert not is_nested_overridable_parent(
            ExtensionsSettings, "DIAGNOSTICS_DELIVERY"
        )

    def test_valid_plan_parses_from_a_nested_mapping(self):
        """Build a ``DeliveryPlan`` from the nested env/YAML mapping shape."""
        settings = ExtensionsSettings(DIAGNOSTICS_DELIVERY=self._plan_payload())

        assert settings.DIAGNOSTICS_DELIVERY.upload.path == "attachment/upload"
        assert (
            settings.DIAGNOSTICS_DELIVERY.secrets["api_key"].get_secret_value()
            == "s3cret"
        )

    def test_cross_reference_invalid_plan_fails_settings_construction(self):
        """Fail fast at settings load when a plan references an undefined secret."""
        payload = self._plan_payload()
        payload["secrets"] = {}

        with pytest.raises(ValidationError, match="undefined secret 'api_key'"):
            ExtensionsSettings(DIAGNOSTICS_DELIVERY=payload)


class TestDiagnosticsDeliveryInputs:
    """Cover the ``DIAGNOSTICS_DELIVERY_INPUTS`` runtime-inputs settings block."""

    @staticmethod
    def _context(
        raw: Any, purpose: MaterializerPurpose = MaterializerPurpose.VALIDATE
    ) -> MaterializerContext:
        """Return the materialization context the override layer would build.

        :param raw: The raw, JSON-decoded value of the candidate override.
        :param purpose: Whether the value stands for a payload submitted now or
            a row read back out of the database.
        :return: The context for the inputs field.
        """
        return MaterializerContext(
            settings_cls=ExtensionsSettings,
            field_name="DIAGNOSTICS_DELIVERY_INPUTS",
            field_info=ExtensionsSettings.model_fields["DIAGNOSTICS_DELIVERY_INPUTS"],
            raw=raw,
            purpose=purpose,
        )

    @staticmethod
    def _skeleton() -> DeliveryPlan:
        """Return a baked plan declaring two secrets, both left empty."""
        return DeliveryPlan(
            endpoint="https://snow.example.com/",
            secrets={"sn_api_key": "", "client_token": ""},
            upload={
                "path": "attachment/upload",
                "headers": {"x-sn-apikey": {"source": "secret", "name": "sn_api_key"}},
                "fields": {
                    "client_token": {"source": "secret", "name": "client_token"}
                },
            },
        )

    def test_defaults_to_not_configured(self):
        """Leave the inputs unset so a standalone deployment behaves unchanged."""
        assert (
            ExtensionsSettings(
                _env_file=None  # ty: ignore[unknown-argument]
            ).DIAGNOSTICS_DELIVERY_INPUTS
            is None
        )

    def test_is_hot_reloadable_and_advanced(self):
        """Expose the whole object to the settings API, grouped as advanced."""
        assert is_hot_reloadable(ExtensionsSettings, "DIAGNOSTICS_DELIVERY_INPUTS")
        assert (
            is_advanced_field(
                ExtensionsSettings.model_fields["DIAGNOSTICS_DELIVERY_INPUTS"]
            )
            is True
        )

    @pytest.mark.parametrize("leaf", ["secrets", "endpoint"])
    def test_leaves_are_sealed(self, leaf: str):
        """Refuse every per-leaf write, so the materializer sees each payload whole."""
        assert chain_is_locked(
            ExtensionsSettings, f"DIAGNOSTICS_DELIVERY_INPUTS__{leaf}"
        )

    def test_materializer_accepts_exactly_the_declared_secret_names(
        self, mocker: MockerFixture
    ):
        """Bind a payload naming every secret the baked plan declares."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        inputs = materialize_delivery_plan_inputs(
            self._context({"secrets": {"sn_api_key": "a", "client_token": "b"}})
        )

        assert inputs.secrets["sn_api_key"].get_secret_value() == "a"

    def test_materializer_passes_none_through(self, mocker: MockerFixture):
        """Accept clearing the inputs back to the field default."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        assert materialize_delivery_plan_inputs(self._context(None)) is None

    def test_materializer_rejects_an_unknown_secret_name(self, mocker: MockerFixture):
        """Refuse a name the plan never cites, which would persist and be ignored."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        with pytest.raises(ValueError, match="extra_key"):
            materialize_delivery_plan_inputs(
                self._context(
                    {
                        "secrets": {
                            "sn_api_key": "a",
                            "client_token": "b",
                            "extra_key": "c",
                        }
                    }
                )
            )

    def test_materializer_rejects_a_missing_secret_name(self, mocker: MockerFixture):
        """Refuse a payload that leaves a declared secret unsupplied."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        with pytest.raises(ValueError, match="client_token"):
            materialize_delivery_plan_inputs(
                self._context({"secrets": {"sn_api_key": "a"}})
            )

    def test_materializer_rejects_secrets_without_a_baked_plan(
        self, mocker: MockerFixture
    ):
        """Refuse inputs for a receiver the image never baked."""
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", None)

        with pytest.raises(ValueError, match="sn_api_key"):
            materialize_delivery_plan_inputs(
                self._context({"secrets": {"sn_api_key": "a"}})
            )

    def test_a_stored_row_that_stopped_matching_survives_the_read(
        self, mocker: MockerFixture
    ):
        """Keep a drifted row readable so the resolver can report it as drift.

        Raising here would only get the row dropped from the snapshot, leaving
        the resolver unable to tell an upgrade-renamed secret from a deployment
        that never configured delivery.
        """
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        inputs = materialize_delivery_plan_inputs(
            self._context(
                {"secrets": {"sn_api_key": "a", "renamed_token": "b"}},
                MaterializerPurpose.SNAPSHOT,
            )
        )

        assert set(inputs.secrets) == {"sn_api_key", "renamed_token"}

    def test_a_malformed_stored_row_is_still_refused_on_the_read(
        self, mocker: MockerFixture
    ):
        """Keep shape coercion strict on both paths, so a junk row is still dropped."""
        mocker.patch.object(
            extensions_settings, "DIAGNOSTICS_DELIVERY", self._skeleton()
        )

        with pytest.raises(ValidationError):
            materialize_delivery_plan_inputs(
                self._context(
                    {"secrets": "not-a-mapping"}, MaterializerPurpose.SNAPSHOT
                )
            )

    def test_model_rejects_a_secret_carrying_the_mask(self):
        """Refuse the redaction mask as a stored credential value."""
        with pytest.raises(ValidationError, match="sn_api_key"):
            DeliveryPlanInputs(secrets={"sn_api_key": SECRET_STR_MASK})

    def test_mask_is_rejected_on_the_yaml_path(self):
        """Fail settings construction when a masked export is re-fed as configuration.

        No materializer runs on this path, so the model's own validator is what
        stops the mask from reaching the receiver as a literal credential.
        """
        with pytest.raises(ValidationError, match="sn_api_key"):
            ExtensionsSettings(
                DIAGNOSTICS_DELIVERY_INPUTS={"secrets": {"sn_api_key": SECRET_STR_MASK}}
            )


class TestFooterTemplate:
    """Define tests for the FOOTER_TEMPLATE setting."""

    def test_footer_template_default(self):
        """Assert FOOTER_TEMPLATE defaults to ``$summary $version``.

        Load without the dotenv file so a local ``.env.local`` override (a
        worktree hook may set ``EXTENSIONS__FOOTER_TEMPLATE`` to the branch name) does
        not mask the built-in default.
        """
        settings = ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]
        assert settings.FOOTER_TEMPLATE.template == "$summary $version"

    def test_footer_template_coerced_from_string(self):
        """Assert a plain string is coerced to a Template object."""
        settings = ExtensionsSettings(FOOTER_TEMPLATE="$version only")
        assert isinstance(settings.FOOTER_TEMPLATE, Template)
        assert settings.FOOTER_TEMPLATE.template == "$version only"

    def test_footer_template_accepts_template_object(self):
        """Assert a Template object is accepted as-is."""
        tmpl = Template("custom $summary")
        settings = ExtensionsSettings(FOOTER_TEMPLATE=tmpl)
        assert settings.FOOTER_TEMPLATE is tmpl


class TestHealthReportFieldRemoved:
    """Check that ``ExtensionsSettings`` no longer mounts the ``HEALTH_REPORT`` section."""

    def test_extensions_settings_has_no_health_report_field(self):
        """Assert ``ExtensionsSettings`` no longer declares a ``HEALTH_REPORT`` field."""
        assert "HEALTH_REPORT" not in ExtensionsSettings.model_fields


class TestDeprecatedPMMRemoved:
    """Confirm the removed ``EXTENSIONS.PMM`` section stays gone; PMM is top-level only."""

    def test_extensions_settings_has_no_pmm_field(self):
        """Check that ``ExtensionsSettings`` no longer declares a ``PMM`` field."""
        assert "PMM" not in ExtensionsSettings.model_fields

    def test_stray_extensions_pmm_mapping_is_rejected(self):
        """Reject a leftover ``EXTENSIONS.PMM`` mapping at construction.

        Connection config must now come from the top-level ``PMM`` section. The
        stale ``EXTENSIONS.PMM`` block (including the ``EXTENSIONS__PMM__*`` env-var path) is
        rejected with a ``ValidationError`` so upgraded deployments fail fast at
        startup instead of silently carrying dead config.
        """
        with pytest.raises(ValidationError, match="EXTENSIONS.PMM"):
            ExtensionsSettings(PMM={"ENDPOINT": "https://pmm.example.com"})

    def test_clean_build_without_stray_pmm(self):
        """Construct a clean ``ExtensionsSettings`` (no ``PMM`` key) without error."""
        assert ExtensionsSettings() is not None


class TestPerSyncerPMMRemoved:
    """The per-syncer ``pmm:`` override is gone; PMM syncers read top-level ``PMM``."""

    def test_stray_pmm_on_syncer_is_rejected(self):
        """A leftover ``pmm`` key on a ``SYNCERS[]`` entry is rejected."""
        with pytest.raises(ValidationError, match="pmm"):
            SyncOptions(syncer="PMMSyncer", pmm={"endpoint": "https://pmm.example.com"})

    def test_stray_pmm_on_extra_kwargs_is_rejected(self):
        """A leftover ``pmm`` key in ``SYNCER_EXTRA_KWARGS`` is rejected."""
        with pytest.raises(ValidationError, match="pmm"):
            SyncerExtraKwargs(pmm={"api_key": "secret"})


class TestPluginModuleNameResolution:
    """``App.MODULE_NAME`` resolution after the legacy backup shim removal."""

    @pytest.mark.parametrize("legacy_value", ["backup", "backups"])
    def test_legacy_value_is_remapped_to_mysql_backups(self, legacy_value: str):
        """Assert legacy aliases resolve to ``mysql_backups`` and log a deprecation warning."""
        with patch("app.extensions.config.logger") as mock_logger:
            plugin = App(name="MySQL Backups", module_name=legacy_value)
        assert plugin.module_name == "app.extensions.apps.mysql_backups"
        mock_logger.warning.assert_called_once()
        rendered = mock_logger.warning.call_args.args[0] % tuple(
            mock_logger.warning.call_args.args[1:]
        )
        assert repr(legacy_value) in rendered
        assert "mysql_backups" in rendered
        assert "next version" in rendered

    def test_modern_value_resolves_without_warning(self):
        """Assert the modern ``mysql_backups`` value resolves normally with no warning."""
        with patch("app.extensions.config.logger") as mock_logger:
            plugin = App(name="MySQL Backups", module_name="mysql_backups")
        assert plugin.module_name == "app.extensions.apps.mysql_backups"
        mock_logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        ("sibling_value", "expected_module"),
        [
            ("backup_mongo", "app.extensions.apps.backup_mongo"),
            ("backup_pg", "app.extensions.apps.backup_pg"),
        ],
    )
    def test_sibling_backup_module_resolves(
        self, sibling_value: str, expected_module: str
    ):
        """Resolve a sibling ``backup``-prefixed plugin without remapping it."""
        plugin = App(name="Backups", module_name=sibling_value)
        assert plugin.module_name == expected_module


class TestAppsModuleExistenceAtLoad:
    """Cover the ``APPS`` module-existence probe at full-settings construction."""

    def test_missing_module_rejects_full_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a missing app package even when its registration is disabled."""
        monkeypatch.setenv(
            "EXTENSIONS__APPS",
            '[{"MODULE_NAME": "_scaffold_missing_package", "ENABLED": false}]',
        )
        with pytest.raises(
            ValidationError,
            match=r"No module named app\.extensions\.apps\._scaffold_missing_package",
        ):
            ExtensionsSettings()


class TestPluginNameOptional:
    """Test the MODULE_NAME-only ``App`` shrink (``name`` optional)."""

    def test_plugin_constructs_without_name(self) -> None:
        """A MODULE_NAME-only entry validates with ``name`` absent."""
        plugin = App(module_name="checksums")
        assert plugin.name is None

    def test_name_absent_leaves_derived_metadata_empty(self) -> None:
        """Without a name, ``uri_path``/``css_class`` stay empty for the registry."""
        plugin = App(module_name="checksums")
        assert plugin.uri_path == ""
        assert plugin.css_class == ""

    def test_name_still_seeds_derived_metadata(self) -> None:
        """A supplied name keeps driving the slugified defaults."""
        plugin = App(name="Snippet Manager", module_name="snippets")
        assert plugin.uri_path == "/snippet-manager"
        assert plugin.css_class == "snippet-manager"


class TestPluginNavIcon:
    """``App.NAV_ICON`` is validated against the closed ``NavIcon`` vocabulary."""

    def test_valid_nav_icon_is_accepted(self) -> None:
        """Accept a known icon key and round-trip it as its string value."""
        plugin = App(module_name="backup_pg", nav_icon="postgresql")
        assert plugin.nav_icon == "postgresql"

    def test_invalid_nav_icon_is_rejected(self) -> None:
        """Reject an unknown icon key at settings-load validation."""
        with pytest.raises(ValidationError, match="NAV_ICON"):
            App(module_name="backup_pg", nav_icon="not-a-real-icon")


class TestPluginReactRoute:
    """``App.REACT_ROUTE`` must be an absolute React route path."""

    def test_absolute_react_route_is_accepted(self) -> None:
        """Accept a leading-slash route path."""
        plugin = App(module_name="backup_pg", react_route="/backups/postgresql")
        assert plugin.react_route == "/backups/postgresql"

    def test_relative_react_route_is_rejected(self) -> None:
        """Reject a route path that is not absolute."""
        with pytest.raises(ValidationError, match="REACT_ROUTE"):
            App(module_name="backup_pg", react_route="backups/postgresql")


class TestAppCeleryModulePath:
    """Cover the ``App.celery_module_path`` three-state convention (like ``api_router_path``)."""

    def test_auto_derives_when_app_ships_celery_module(self) -> None:
        """Derive ``<module_name>.celery`` when the app ships a ``celery.py``.

        The derivation is a filesystem probe, so the exemplar must be an app that
        really ships one -- a synthetic module name derives ``None``.
        """
        plugin = App(module_name="alerts")
        assert plugin.celery_module_path == "app.extensions.apps.alerts.celery"

    def test_none_when_app_has_no_celery_module(self) -> None:
        """Leave ``celery_module_path`` unset when the app ships no ``celery.py``."""
        plugin = App(module_name="checksums")
        assert plugin.celery_module_path is None

    def test_explicit_string_override_is_used_verbatim(self) -> None:
        """Use an explicit, existing string as-is, bypassing the convention probe.

        The override points at a real module outside the plugin's own package
        (``app.tasks.celery``), proving the path is used verbatim and is not
        confined to ``app/extensions/apps/<module>/``.
        """
        plugin = App(module_name="checksums", celery_module_path="app.tasks.celery")
        assert plugin.celery_module_path == "app.tasks.celery"

    def test_explicit_missing_override_is_rejected(self) -> None:
        """Reject an explicit override that names no module file on disk.

        Validation is a filesystem probe during settings construction, so a typo
        fails at load rather than only when the Celery worker imports the module.
        """
        with pytest.raises(ValidationError, match="No module named"):
            App(
                module_name="checksums",
                celery_module_path="app.extensions.apps.nope.celery",
            )

    def test_explicit_null_opts_out_even_with_celery_module(self) -> None:
        """Opt out on an explicit ``None`` even when a conventional ``celery.py`` exists."""
        plugin = App(module_name="snippets", celery_module_path=None)
        assert plugin.celery_module_path is None

    def test_legacy_alias_probes_remapped_module(self) -> None:
        """Probe the ``mysql_backups`` remap target, which ships no ``celery.py``."""
        with patch("app.extensions.config.logger"):
            plugin = App(name="MySQL Backups", module_name="backup")
        assert plugin.module_name == "app.extensions.apps.mysql_backups"
        assert plugin.celery_module_path is None


class TestAppDrainSettings:
    """The drain reconciler settings reject a non-positive stale-task TTL."""

    def test_default_ttl_is_one_hour(self) -> None:
        """The default TTL is a positive duration."""
        assert AppDrainSettings().stale_task_ttl == timedelta(hours=1)

    @pytest.mark.parametrize("seconds", [0, -1, -3600])
    def test_non_positive_ttl_rejected(self, seconds: int) -> None:
        """A zero or negative TTL fails validation rather than pruning live rows.

        Positivity is enforced by the ``Gt`` annotation constraint so it also
        holds for runtime overrides, hence the standard "greater than" message.
        """
        with pytest.raises(ValidationError, match="greater than"):
            AppDrainSettings(stale_task_ttl=timedelta(seconds=seconds))


def _logged_legacy_apps_warning(mock_logger: object) -> bool:
    """Return whether any ``logger.warning`` call names the deprecated PLUGINS key."""
    return any(
        "PLUGINS" in str(call.args[0]) for call in mock_logger.warning.call_args_list
    )


class TestAppsKeyBackCompat:
    """Cover the ``EXTENSIONS.PLUGINS`` -> ``EXTENSIONS.APPS`` config-key back-compat shim."""

    def test_legacy_plugins_key_populates_apps(self):
        """Load a legacy ``PLUGINS`` key into ``APPS`` via the validation alias."""
        settings = ExtensionsSettings.model_validate(
            {"PLUGINS": [{"MODULE_NAME": "backup_pg"}]}
        )
        assert [app.module_name for app in settings.APPS] == [
            "app.extensions.apps.backup_pg"
        ]

    def test_modern_apps_key_populates_apps(self):
        """Load the app list from the modern ``APPS`` key."""
        settings = ExtensionsSettings.model_validate(
            {"APPS": [{"MODULE_NAME": "backup_pg"}]}
        )
        assert [app.module_name for app in settings.APPS] == [
            "app.extensions.apps.backup_pg"
        ]

    def test_apps_takes_precedence_over_legacy(self):
        """Prefer ``APPS`` over the legacy ``PLUGINS`` when both keys are set."""
        settings = ExtensionsSettings.model_validate(
            {
                "APPS": [{"MODULE_NAME": "backup_pg"}],
                "PLUGINS": [{"MODULE_NAME": "backup_mongo"}],
            }
        )
        assert [app.module_name for app in settings.APPS] == [
            "app.extensions.apps.backup_pg"
        ]

    def test_deprecation_warning_for_legacy_key(self):
        """Assert a deprecation warning is logged when the legacy ``PLUGINS`` key is set."""
        with patch("app.extensions.config.logger") as mock_logger:
            ExtensionsSettings.model_validate(
                {"PLUGINS": [{"MODULE_NAME": "backup_pg"}]}
            )
        assert _logged_legacy_apps_warning(mock_logger)

    def test_no_warning_for_modern_key(self):
        """Assert no deprecation warning is logged for the modern ``APPS`` key."""
        with patch("app.extensions.config.logger") as mock_logger:
            ExtensionsSettings.model_validate({"APPS": [{"MODULE_NAME": "backup_pg"}]})
        assert not _logged_legacy_apps_warning(mock_logger)

    def test_legacy_plugins_env_var_populates_apps_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Load a legacy ``EXTENSIONS__PLUGINS`` env var into ``APPS`` and warn."""
        monkeypatch.setenv("EXTENSIONS__PLUGINS", '[{"MODULE_NAME": "backup_pg"}]')
        with patch("app.extensions.config.logger") as mock_logger:
            settings = ExtensionsSettings()
        assert any(
            app.module_name == "app.extensions.apps.backup_pg" for app in settings.APPS
        )
        assert _logged_legacy_apps_warning(mock_logger)

    def test_modern_apps_env_var_does_not_warn(self, monkeypatch: pytest.MonkeyPatch):
        """Load a modern ``EXTENSIONS__APPS`` env var into ``APPS`` without warning."""
        monkeypatch.setenv("EXTENSIONS__APPS", '[{"MODULE_NAME": "backup_pg"}]')
        with patch("app.extensions.config.logger") as mock_logger:
            settings = ExtensionsSettings()
        assert any(
            app.module_name == "app.extensions.apps.backup_pg" for app in settings.APPS
        )
        assert not _logged_legacy_apps_warning(mock_logger)


class TestCredentialUrlMaskRejection:
    """Reject a redacted credential URL copied into PMM Extensions endpoint configuration."""

    _MASKED_ENDPOINT = "http://inv-user:****@inventory.internal:8080"

    @pytest.mark.parametrize("field", ["INVENTORY_ENDPOINT", "TASKS_ENDPOINT"])
    def test_mask_is_rejected_on_the_yaml_path(self, field: str) -> None:
        """Fail settings construction when a masked export is re-fed as configuration."""
        with pytest.raises(ValidationError, match=field):
            ExtensionsSettings(
                **{field: self._MASKED_ENDPOINT},
                _env_file=None,  # ty: ignore[unknown-argument]
            )

    @pytest.mark.parametrize(
        ("field", "env_name"),
        [
            ("INVENTORY_ENDPOINT", "EXTENSIONS__INVENTORY_ENDPOINT"),
            ("TASKS_ENDPOINT", "EXTENSIONS__TASKS_ENDPOINT"),
        ],
    )
    def test_mask_is_rejected_on_the_env_path(
        self, field: str, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail settings construction when a masked endpoint arrives via the environment."""
        monkeypatch.setenv(env_name, self._MASKED_ENDPOINT)
        with pytest.raises(ValidationError, match=field):
            ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]


class TestSyncerRetirementThresholdsOverConfig:
    """Hold the retirement floors that ``EXTENSIONS.SYNCERS[]`` forwards unvalidated.

    Load-time rejection of these values lives in
    :class:`TestSyncerExtrasValidatedAtLoad`; what this class owns is the floor a
    syncer enforces for itself, which is what still stands when a settings object
    reaches construction without having passed the load-time check.
    """

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("missing_grace_generations", 0),
            ("missing_grace_generations", 1),
            ("stale_run_after", 0),
        ],
    )
    def test_unsafe_threshold_is_rejected_at_construction(
        self, field, value, mocker: MockerFixture
    ) -> None:
        """Keep the floors enforced when a settings object skipped the load check."""
        option = SyncOptions(
            syncer="app.extensions.sync.syncers.pmm.PMMSyncer", **{field: value}
        )
        assert getattr(option, field) == value
        mocker.patch.object(extensions_settings, "SYNCERS", [option])

        # ``BaseCaseInsensitiveModel`` reports the field uppercased.
        with pytest.raises(HTTPServiceUnavailableException, match=f"(?i){field}"):
            get_syncers(
                inventory_api=MagicMock(spec=RemoteAPI),
                tasks_api=MagicMock(spec=RemoteAPI),
            )


_MYSQL_SYNCER = "app.extensions.sync.syncers.mysql.syncer.MySQLSyncer"
_PMM_SYNCER = "app.extensions.sync.syncers.pmm.PMMSyncer"


def _build_via_model_validate(data: dict[str, Any]) -> ExtensionsSettings:
    """Return settings built through ``ExtensionsSettings.model_validate``.

    :param data: The settings payload to validate.
    :return: The constructed settings object.
    """
    return ExtensionsSettings.model_validate(deepcopy(data))


def _build_via_constructor(data: dict[str, Any]) -> ExtensionsSettings:
    """Return settings built through the ordinary ``ExtensionsSettings`` constructor.

    :param data: The settings payload to pass as keyword arguments.
    :return: The constructed settings object.
    """
    return ExtensionsSettings(
        **deepcopy(data),
        _env_file=None,  # ty: ignore[unknown-argument]
    )


_BUILDERS = pytest.mark.parametrize(
    "build",
    [_build_via_model_validate, _build_via_constructor],
    ids=["model_validate", "constructor"],
)


class TestSyncerExtrasValidatedAtLoad:
    """Reject an unusable syncer threshold at settings load, not per request.

    The ``EXTENSIONS__SYNCER_EXTRA_KWARGS__<KEY>`` form hands its leaf to
    ``SyncerExtraKwargs`` as a raw string, and both that model and ``SyncOptions``
    allow untyped extras — so without a load-time check the string only meets a
    typed field once a syncer is constructed, which for the request-scoped
    ``get_syncers`` dependency means a fresh error on every sync trigger.
    """

    @staticmethod
    def _syncers_env(*syncers: str) -> str:
        """Return a JSON ``EXTENSIONS__SYNCERS`` value naming each syncer.

        :param syncers: Dotted syncer paths to configure.
        :return: The JSON list pydantic-settings parses for ``SYNCERS``.
        """
        return json.dumps([{"SYNCER": syncer} for syncer in syncers])

    def test_dunder_leaf_numeric_string_is_rejected_at_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a bare-integer-seconds ``STALE_RUN_AFTER`` from the env leaf form."""
        monkeypatch.setenv("EXTENSIONS__SYNCER_EXTRA_KWARGS__STALE_RUN_AFTER", "60")
        monkeypatch.setenv("EXTENSIONS__SYNCERS", self._syncers_env(_PMM_SYNCER))

        with pytest.raises(ValidationError, match="STALE_RUN_AFTER") as excinfo:
            ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]

        message = str(excinfo.value)
        assert "ISO-8601" in message
        assert "HH:MM:SS" in message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("MISSING_GRACE_GENERATIONS", "0"),
            ("TASK_EXECUTION_TIMEOUT", "0"),
            ("TASK_EXECUTION_TIMEOUT", "-1"),
            ("TASKS_EXECUTION_WAIT_INTERVAL", "0"),
            ("TASKS_EXECUTION_WAIT_INTERVAL", "-1"),
        ],
    )
    def test_dunder_leaf_threshold_below_floor_is_rejected_at_load(
        self, field: str, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a threshold below its floor from the environment leaf form."""
        monkeypatch.setenv("EXTENSIONS__SYNCERS", self._syncers_env(_PMM_SYNCER))
        monkeypatch.setenv(f"EXTENSIONS__SYNCER_EXTRA_KWARGS__{field}", value)

        with pytest.raises(ValidationError, match=field):
            ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("MISSING_GRACE_GENERATIONS", 0),
            ("MISSING_GRACE_GENERATIONS", 1),
            ("STALE_RUN_AFTER", 0),
            ("TASK_EXECUTION_TIMEOUT", 0),
            ("TASK_EXECUTION_TIMEOUT", -1),
            ("TASKS_EXECUTION_WAIT_INTERVAL", 0),
            ("TASKS_EXECUTION_WAIT_INTERVAL", -1),
        ],
    )
    def test_per_entry_threshold_below_the_floor_is_rejected(
        self, field: str, value: int
    ) -> None:
        """Refuse to load a threshold below its safe floor from a syncer entry.

        ``SyncOptions`` carries ``extra="allow"`` and does not validate these keys
        itself, so the merge validator is what stands between a deployment's settings
        and a syncer carrying an unsafe value.
        """
        with pytest.raises(ValidationError, match=field):
            ExtensionsSettings.model_validate(
                {"SYNCERS": [{"SYNCER": _PMM_SYNCER, field: value}]}
            )

    @pytest.mark.parametrize(
        "field", ["TASK_EXECUTION_TIMEOUT", "TASKS_EXECUTION_WAIT_INTERVAL"]
    )
    def test_json_env_non_positive_task_timing_is_rejected_at_load(
        self, field: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a non-positive task timing value from the JSON environment form."""
        monkeypatch.setenv(
            "EXTENSIONS__SYNCERS",
            json.dumps([{"SYNCER": _PMM_SYNCER, field: 0}]),
        )

        with pytest.raises(ValidationError, match=field):
            ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]

    def test_env_hint_is_offered_but_not_asserted_for_an_extras_string(self) -> None:
        """Explain the env leaf's quoting without claiming it is what happened.

        ``EXTENSIONS.SYNCER_EXTRA_KWARGS`` is a YAML surface too, so a string arriving here
        may have been quoted deliberately in the profile.
        """
        with pytest.raises(ValidationError) as excinfo:
            ExtensionsSettings.model_validate(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"STALE_RUN_AFTER": "60"},
                }
            )

        message = str(excinfo.value)
        assert "EXTENSIONS__SYNCER_EXTRA_KWARGS__STALE_RUN_AFTER" in message
        assert "If this came from" in message

    def test_env_hint_is_withheld_for_a_numeric_value(self) -> None:
        """Blame the env form's quoting only when a string is what arrived."""
        with pytest.raises(ValidationError) as excinfo:
            ExtensionsSettings.model_validate(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"STALE_RUN_AFTER": 0},
                }
            )

        assert "If this came from" not in str(excinfo.value)

    def test_env_hint_is_withheld_for_a_string_on_a_syncer_entry(self) -> None:
        """Do not send an operator back to the surface they already configured.

        A string reaching a ``SYNCERS[]`` entry was spelled there deliberately, so
        the advice that fits the env leaf form would only mislead here.
        """
        with pytest.raises(ValidationError) as excinfo:
            ExtensionsSettings.model_validate(
                {"SYNCERS": [{"SYNCER": _PMM_SYNCER, "STALE_RUN_AFTER": "abc"}]}
            )

        assert "If this came from" not in str(excinfo.value)

    @_BUILDERS
    @pytest.mark.parametrize(
        "value", ["60", "60.5", "-60", "abc", "", 0, -1, [60], {"seconds": 60}]
    )
    def test_unusable_stale_run_after_is_rejected_at_load(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings], value: Any
    ) -> None:
        """Reject every shape ``stale_run_after`` cannot be built from."""
        with pytest.raises(ValidationError, match="(?i)stale_run_after"):
            build(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"STALE_RUN_AFTER": value},
                }
            )

    @_BUILDERS
    @pytest.mark.parametrize("value", ["0", "1", 0, 1, "abc", 2.5, ""])
    def test_unusable_grace_generations_is_rejected_at_load(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings], value: Any
    ) -> None:
        """Reject every shape ``missing_grace_generations`` cannot be built from."""
        with pytest.raises(ValidationError, match="(?i)missing_grace_generations"):
            build(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"MISSING_GRACE_GENERATIONS": value},
                }
            )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (3600, timedelta(hours=1)),
            (60.5, timedelta(seconds=60, milliseconds=500)),
            ("PT1H", timedelta(hours=1)),
            ("01:00:00", timedelta(hours=1)),
            (timedelta(minutes=90), timedelta(minutes=90)),
        ],
    )
    def test_documented_forms_reach_the_syncer(
        self, value: Any, expected: timedelta, mocker: MockerFixture
    ) -> None:
        """Keep every form pydantic reads as a duration working end to end."""
        settings = ExtensionsSettings.model_validate(
            {"SYNCERS": [{"SYNCER": _PMM_SYNCER, "STALE_RUN_AFTER": value}]}
        )
        mocker.patch.object(extensions_settings, "SYNCERS", settings.SYNCERS)

        syncers = get_syncers(
            inventory_api=MagicMock(spec=RemoteAPI),
            tasks_api=MagicMock(spec=RemoteAPI),
        )

        assert syncers[0].stale_run_after == expected

    def test_json_env_form_reaches_the_syncer(
        self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Keep the JSON ``EXTENSIONS__SYNCERS`` override working, numbers read as numbers."""
        grace = 3
        monkeypatch.setenv(
            "EXTENSIONS__SYNCERS",
            json.dumps(
                [
                    {
                        "SYNCER": _PMM_SYNCER,
                        "STALE_RUN_AFTER": 60,
                        "MISSING_GRACE_GENERATIONS": grace,
                    }
                ]
            ),
        )
        settings = ExtensionsSettings(_env_file=None)  # ty: ignore[unknown-argument]
        mocker.patch.object(extensions_settings, "SYNCERS", settings.SYNCERS)

        syncer = get_syncers(
            inventory_api=MagicMock(spec=RemoteAPI),
            tasks_api=MagicMock(spec=RemoteAPI),
        )[0]

        assert syncer.stale_run_after == timedelta(seconds=60)
        assert isinstance(syncer, PMMSyncer)
        assert syncer.missing_grace_generations == grace

    def test_error_names_the_offending_syncer(self) -> None:
        """Point the operator at the entry to fix when several are configured."""
        with pytest.raises(ValidationError, match=re.escape(_MYSQL_SYNCER)):
            ExtensionsSettings.model_validate(
                {
                    "SYNCERS": [
                        {"SYNCER": _PMM_SYNCER, "STALE_RUN_AFTER": 3600},
                        {"SYNCER": _MYSQL_SYNCER, "STALE_RUN_AFTER": "60"},
                    ]
                }
            )

    def test_unconstrained_extras_are_forwarded_untouched(self) -> None:
        """Keep the check a floor on known thresholds, not an allowlist of keys."""
        settings = ExtensionsSettings.model_validate(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {
                    "IGNORE_SCHEMAS": ["sys"],
                    "DEFAULT_EXECUTOR_HOST": "node-1",
                },
            }
        )

        dumped = settings.SYNCERS[0].model_dump()
        assert dumped["ignore_schemas"] == ["sys"]
        assert dumped["default_executor_host"] == "node-1"

    @pytest.mark.parametrize("key", ["STALE_RUN_AFTER", "stale_run_after"])
    def test_key_is_matched_whatever_case_it_arrives_in(self, key: str) -> None:
        """Refuse the value however the operator spelled the key."""
        with pytest.raises(ValidationError, match="(?i)stale_run_after"):
            ExtensionsSettings.model_validate(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {key: "60"},
                }
            )

    def test_threshold_is_checked_even_when_the_syncer_lacks_the_field(self) -> None:
        """Refuse a global extra no configured syncer would ever apply.

        ``MISSING_GRACE_GENERATIONS`` is a ``PMMSyncer`` field, so on a MySQL-only
        deployment an unusable value is refused at load rather than merged into an
        entry that would silently drop it. A usable one still loads, and is still
        ignored.
        """
        with pytest.raises(ValidationError, match="(?i)missing_grace_generations"):
            ExtensionsSettings.model_validate(
                {
                    "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"MISSING_GRACE_GENERATIONS": 0},
                }
            )

    @_BUILDERS
    def test_extra_kwargs_win_the_merge_and_are_still_checked(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Check the merged threshold, not the per-entry one the extras displace."""
        with pytest.raises(ValidationError, match="(?i)stale_run_after"):
            build(
                {
                    "SYNCERS": [{"SYNCER": _PMM_SYNCER, "STALE_RUN_AFTER": 3600}],
                    "SYNCER_EXTRA_KWARGS": {"STALE_RUN_AFTER": "60"},
                }
            )

    @_BUILDERS
    def test_removed_pmm_key_on_the_extras_is_still_rejected(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Refuse a dead per-syncer ``pmm`` override carried by the extras."""
        with pytest.raises(ValidationError, match="(?i)pmm"):
            build(
                {
                    "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}],
                    "SYNCER_EXTRA_KWARGS": {"PMM": {"URL": "http://pmm"}},
                }
            )

    @_BUILDERS
    def test_unusable_extra_loads_when_no_syncer_is_configured(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Leave a deployment that runs no syncer alone.

        The check walks configured syncers, so with none there is nothing an extra
        can reach and nothing to refuse.
        """
        settings = build(
            {"SYNCERS": [], "SYNCER_EXTRA_KWARGS": {"STALE_RUN_AFTER": "60"}}
        )

        assert settings.SYNCERS == []

    @_BUILDERS
    def test_null_threshold_falls_back_to_the_field_default(
        self,
        build: Callable[[dict[str, Any]], ExtensionsSettings],
        mocker: MockerFixture,
    ) -> None:
        """Read an explicit ``null`` as "unset" rather than refusing to load."""
        settings = build(
            {"SYNCERS": [{"SYNCER": _PMM_SYNCER, "STALE_RUN_AFTER": None}]}
        )
        mocker.patch.object(extensions_settings, "SYNCERS", settings.SYNCERS)

        syncers = get_syncers(
            inventory_api=MagicMock(spec=RemoteAPI),
            tasks_api=MagicMock(spec=RemoteAPI),
        )

        assert syncers[0].stale_run_after == timedelta(hours=1)

    @pytest.mark.parametrize("field", ["SYNCERS", "SYNCER_EXTRA_KWARGS"])
    def test_syncer_settings_are_not_database_overridable(self, field: str) -> None:
        """Keep the load-time check unbypassable by a DB-backed override."""
        assert not is_hot_reloadable(ExtensionsSettings, field)
        assert not is_nested_overridable_parent(ExtensionsSettings, field)


class TestSyncerExtrasMergeParity:
    """Merge ``SYNCER_EXTRA_KWARGS`` into each syncer exactly once, however built.

    ``model_validate`` and the ordinary constructor have to agree: a list-valued
    extra reaches a syncer once on either path, never concatenated with itself.
    """

    @_BUILDERS
    def test_list_extra_is_forwarded_once(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Forward a list-valued extra without duplicating its entries."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["sys"]},
            }
        )

        assert settings.SYNCERS[0].model_dump()["ignore_schemas"] == ["sys"]

    @_BUILDERS
    def test_list_extra_precedes_the_syncers_own_entries(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Keep the overlay ahead of the entry's own list, prepended only once."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER, "IGNORE_SCHEMAS": ["mine"]}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["sys"]},
            }
        )

        assert settings.SYNCERS[0].model_dump()["ignore_schemas"] == ["sys", "mine"]

    def test_both_construction_paths_agree(self) -> None:
        """Produce identical syncers whichever construction path was taken."""
        data = {
            "SYNCERS": [
                {"SYNCER": _MYSQL_SYNCER, "IGNORE_SCHEMAS": ["mine"]},
                {"SYNCER": _PMM_SYNCER},
            ],
            "SYNCER_EXTRA_KWARGS": {
                "IGNORE_SCHEMAS": ["sys"],
                "DEFAULT_EXECUTOR_HOST": "node-1",
            },
        }

        validated = _build_via_model_validate(data)
        constructed = _build_via_constructor(data)

        assert [syncer.model_dump() for syncer in validated.SYNCERS] == [
            syncer.model_dump() for syncer in constructed.SYNCERS
        ]

    @_BUILDERS
    def test_scalar_extra_reaches_every_syncer(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Merge a scalar extra into each configured syncer."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}, {"SYNCER": _PMM_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {"DEFAULT_EXECUTOR_HOST": "node-1"},
            }
        )

        assert [s.model_dump()["default_executor_host"] for s in settings.SYNCERS] == [
            "node-1",
            "node-1",
        ]

    @_BUILDERS
    def test_nested_list_extra_is_merged_once(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Merge a list nested inside a mapping extra exactly once."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER, "OPTS": {"schemas": ["mine"]}}],
                "SYNCER_EXTRA_KWARGS": {"OPTS": {"schemas": ["sys"]}},
            }
        )

        assert settings.SYNCERS[0].model_dump()["opts"] == {"schemas": ["sys", "mine"]}

    @_BUILDERS
    def test_empty_overlay_clears_the_syncers_list(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Read an empty list overlay as "drop the entry's own value"."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER, "IGNORE_SCHEMAS": ["mine"]}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": []},
            }
        )

        assert settings.SYNCERS[0].model_dump()["ignore_schemas"] == []

    @_BUILDERS
    def test_repeated_syncer_entry_collapses_to_one(
        self, build: Callable[[dict[str, Any]], ExtensionsSettings]
    ) -> None:
        """Keep one entry per syncer path, still carrying the extras once."""
        settings = build(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}, {"SYNCER": _MYSQL_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["sys"]},
            }
        )

        assert len(settings.SYNCERS) == 1
        assert settings.SYNCERS[0].model_dump()["ignore_schemas"] == ["sys"]

    def test_re_merging_reads_the_syncers_currently_configured(self) -> None:
        """Merge over whatever ``SYNCERS`` holds now, and still only once."""
        settings = _build_via_constructor(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER, "IGNORE_SCHEMAS": ["mine"]}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["sys"]},
            }
        )
        merged_once = [syncer.model_dump() for syncer in settings.SYNCERS]

        settings.add_syncer_extra_kwargs()  # ty: ignore[call-non-callable]

        assert [syncer.model_dump() for syncer in settings.SYNCERS] == merged_once

        settings.SYNCERS = [SyncOptions(syncer=_PMM_SYNCER)]
        settings.add_syncer_extra_kwargs()  # ty: ignore[call-non-callable]

        assert settings.SYNCERS[0].syncer == _PMM_SYNCER
        assert settings.SYNCERS[0].model_dump()["ignore_schemas"] == ["sys"]

    def test_each_settings_object_keeps_its_own_syncers(self) -> None:
        """Keep one object's merge out of another's, whatever is cached per instance."""
        first = _build_via_model_validate(
            {
                "SYNCERS": [{"SYNCER": _MYSQL_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["sys"]},
            }
        )
        second = _build_via_model_validate(
            {
                "SYNCERS": [{"SYNCER": _PMM_SYNCER}],
                "SYNCER_EXTRA_KWARGS": {"IGNORE_SCHEMAS": ["other"]},
            }
        )

        assert first.SYNCERS[0].model_dump()["ignore_schemas"] == ["sys"]
        assert second.SYNCERS[0].model_dump()["ignore_schemas"] == ["other"]
        assert first.SYNCERS[0].syncer == _MYSQL_SYNCER
