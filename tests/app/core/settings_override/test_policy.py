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

"""Cover the ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` restriction and its predicates.

Exercise the setting itself (env parsing through the real ``Settings()`` source
chain, entry-format validation), the ``policy`` predicates that read it, the
fail-closed and restrict-only invariants, and the value the side-car image
bakes into its embedded settings profile.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta

import pytest
import yaml
from pydantic import ValidationError

from app import BASE_DIR
from app.core.alerts.config import AlertSettings
from app.core.config import Settings, settings, SettingsOverrideOptions
from app.core.settings_override.policy import (
    has_allowed_key_under,
    is_key_allowed,
    is_restriction_active,
)
from app.core.settings_override.registry import (
    chain_has_explicit_not_overridable,
    field_reload_classification,
    is_nested_overridable_parent,
    iter_class_fields,
    ReloadClassification,
    rendered_leaf_keys,
)
from app.core.settings_override.resolution import (
    resolve_nested_field,
    resolve_nested_field_metadata,
)
from app.extensions.apps.alerts.config import AlertsSettings
from app.extensions.apps.om_inventory.config import OmInventorySettings
from app.extensions.apps.report.config import HealthReportSettings
from app.extensions.config import ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.inventory.config import InventorySettings
from app.tasks.anonymizer.config import AnonymizerSettings
from app.tasks.config import TasksSettings
from tests.sidecar.conftest import ALLOWLIST_KEY, EMBEDDED_PROFILE, read_allowlist

#: Every settings class reachable from the settings router, keyed by the
#: Pydantic class ``__name__`` (the REST / allowlist spelling).
SETTINGS_CLASSES: dict[str, type] = {
    Settings.__name__: Settings,
    ExtensionsSettings.__name__: ExtensionsSettings,
    TasksSettings.__name__: TasksSettings,
    SnippetsSettings.__name__: SnippetsSettings,
    AlertSettings.__name__: AlertSettings,
    AlertsSettings.__name__: AlertsSettings,
    HealthReportSettings.__name__: HealthReportSettings,
    AnonymizerSettings.__name__: AnonymizerSettings,
    InventorySettings.__name__: InventorySettings,
    OmInventorySettings.__name__: OmInventorySettings,
}

#: Keys the ticket names as provisioned topology that the embedded image must
#: never expose, plus the profile-pinned PMM connection leaves.
TOPOLOGY_KEYS = [
    "ExtensionsSettings.INVENTORY_ENDPOINT",
    "ExtensionsSettings.TASKS_ENDPOINT",
    "ExtensionsSettings.AMBIENT_SESSION_SSO_ENABLED",
    "Settings.PMM__endpoint",
    "Settings.PMM__api_key",
    "TasksSettings.NOMAD__endpoint",
    "SnippetsSettings.SNIPPETS_BASE_URL",
]

#: The one product default the embedded image deliberately leaves tunable.
CARVE_OUT_KEY = "Settings.PMM__annotations_enabled"


def shipped_allowed_keys() -> list[str]:
    """Return the allowlist the side-car profile bakes into the image.

    Fail the run outright when the profile does not carry the key: the
    restriction's negative assertions are all satisfied by an empty list, so
    degrading to one would leave them green while guarding nothing.

    :return: The entries the embedded profile declares.
    """
    profile = yaml.safe_load(EMBEDDED_PROFILE.read_text(encoding="utf-8"))
    entries = read_allowlist(profile)
    if not isinstance(entries, list):
        pytest.fail(
            f"{'.'.join(ALLOWLIST_KEY)} is not a list in {EMBEDDED_PROFILE}: {entries!r}"
        )
    return entries


def listed_hot_keys(settings_cls: type) -> set[str]:
    """Return the keys the settings list renders HOT for one settings class.

    Shares ``_field_responses``' row selection through ``rendered_leaf_keys``, so
    an entry naming a parent the listing replaces with its leaves addresses no
    rendered row and unlocks none of them.

    :param settings_cls: The Pydantic settings class to render.
    :return: The rendered keys that are currently overridable.
    """
    hot: set[str] = set()
    for field_meta in iter_class_fields(settings_cls):
        leaves = rendered_leaf_keys(settings_cls, field_meta.key)
        if not leaves:
            if field_meta.reload is ReloadClassification.HOT:
                hot.add(field_meta.key)
            continue
        for leaf_key, _chain in leaves:
            leaf_meta = resolve_nested_field_metadata(settings_cls, leaf_key)
            if leaf_meta is not None and leaf_meta.reload is ReloadClassification.HOT:
                hot.add(leaf_key)
    return hot


class TestSettingDeclaration:
    """Cover the setting's own declaration, parsing and validation."""

    def test_default_is_none(self) -> None:
        """Assert the shipped default leaves every deployment unrestricted."""
        assert settings.SETTINGS_OVERRIDE.ALLOWED_KEYS is None

    def test_field_is_not_overridable(self) -> None:
        """Assert the restriction cannot be unlocked through the override API.

        Positively locks both halves of the self-lockdown: the unmarked
        ``SETTINGS_OVERRIDE`` parent classifies ``NOT_OVERRIDABLE`` (so nested
        leaves are unreachable), and ``ALLOWED_KEYS`` keeps its explicit
        ``not_overridable_field`` marker (so a later parent-marker change still
        refuses that leaf via the chain check).
        """
        assert (
            field_reload_classification(
                Settings.model_fields["SETTINGS_OVERRIDE"],
                owner_cls=Settings,
                field_name="SETTINGS_OVERRIDE",
            )
            is ReloadClassification.NOT_OVERRIDABLE
        )
        assert (
            is_nested_overridable_parent(
                Settings, "SETTINGS_OVERRIDE", include_policy_gate=False
            )
            is False
        )
        assert (
            field_reload_classification(
                SettingsOverrideOptions.model_fields["ALLOWED_KEYS"],
                owner_cls=SettingsOverrideOptions,
                field_name="ALLOWED_KEYS",
            )
            is ReloadClassification.NOT_OVERRIDABLE
        )
        assert chain_has_explicit_not_overridable(
            Settings, "SETTINGS_OVERRIDE__ALLOWED_KEYS"
        )

    @pytest.mark.parametrize("raw", ["PT0S", "-PT5S"])
    def test_rejects_non_positive_refresh_interval_from_env(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Assert a zero or negative interval fails the settings load.

        ``start_refresh_task`` sleeps for the configured interval between
        cycles, so a non-positive value would spin against the database.
        """
        monkeypatch.setenv("SETTINGS_OVERRIDE__REFRESH_INTERVAL", raw)
        with pytest.raises(ValidationError, match="greater than 0"):
            Settings()

    @pytest.mark.parametrize("raw", [0, -5])
    def test_rejects_non_positive_refresh_interval_as_seconds(self, raw: int) -> None:
        """Assert the bound also holds for the integer-seconds form YAML yields."""
        with pytest.raises(ValidationError, match="greater than 0"):
            SettingsOverrideOptions(REFRESH_INTERVAL=raw)

    def test_accepts_positive_refresh_interval_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert a positive interval loads unchanged."""
        monkeypatch.setenv("SETTINGS_OVERRIDE__REFRESH_INTERVAL", "PT45S")
        assert timedelta(seconds=45) == Settings().SETTINGS_OVERRIDE.REFRESH_INTERVAL

    def test_parses_json_array_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assert a bare env var carrying a JSON array reaches the field as a set."""
        monkeypatch.setenv(
            "SETTINGS_OVERRIDE__ALLOWED_KEYS",
            '["Settings.LOGGING", "ExtensionsSettings.SYNC_REFRESH_TIME"]',
        )
        assert {
            "Settings.LOGGING",
            "ExtensionsSettings.SYNC_REFRESH_TIME",
        } == Settings().SETTINGS_OVERRIDE.ALLOWED_KEYS

    @pytest.mark.parametrize(
        "entry",
        [
            "no-dot",
            ".KEY",
            "Class.",
            "Too.Many.Dots",
            "",
            "Settings.LOGGING ",
            " Settings.LOGGING",
            "Settings. LOGGING",
            "Settings.LOG GING",
            "Settings.LOGGING\t",
        ],
    )
    def test_rejects_malformed_entry(
        self, monkeypatch: pytest.MonkeyPatch, entry: str
    ) -> None:
        """Assert a malformed entry fails the settings load rather than going inert."""
        monkeypatch.setenv("SETTINGS_OVERRIDE__ALLOWED_KEYS", json.dumps([entry]))
        with pytest.raises(ValueError, match="ALLOWED_KEYS"):
            Settings()

    def test_accepts_nested_key_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assert a ``__``-delimited key token passes the format validator."""
        monkeypatch.setenv(
            "SETTINGS_OVERRIDE__ALLOWED_KEYS",
            '["Settings.PMM__annotations_enabled"]',
        )
        assert {
            "Settings.PMM__annotations_enabled"
        } == Settings().SETTINGS_OVERRIDE.ALLOWED_KEYS


class TestPredicates:
    """Cover the policy predicates the classification gates call."""

    def test_inactive_by_default(self) -> None:
        """Assert an unset restriction reports inactive."""
        assert is_restriction_active() is False

    def test_inactive_allows_every_key(self) -> None:
        """Assert every key stays allowed while the restriction is unset."""
        assert is_key_allowed("ExtensionsSettings", "INVENTORY_ENDPOINT")
        assert has_allowed_key_under("TasksSettings", "NOMAD")

    def test_active_locks_unlisted_key(self, restrict: Callable[..., None]) -> None:
        """Assert an entry set locks every key it does not name."""
        restrict("Settings.LOGGING")
        assert is_restriction_active() is True
        assert is_key_allowed("Settings", "LOGGING") is True
        assert is_key_allowed("ExtensionsSettings", "LOGGING") is False
        assert is_key_allowed("ExtensionsSettings", "INVENTORY_ENDPOINT") is False

    def test_unknown_entry_allows_nothing(self, restrict: Callable[..., None]) -> None:
        """Assert an entry naming no real class or field grants no access."""
        restrict("BogusSettings.WHATEVER")
        assert is_key_allowed("Settings", "WHATEVER") is False
        assert is_key_allowed("Settings", "LOGGING") is False

    def test_unregistered_class_matches_allowlist_by_name(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a class with no core identifier still matches ``ALLOWED_KEYS``.

        App-owned classes have no core identifier, so a lookup that withheld
        every unknown ``__name__`` would lock keys the allowlist already names.
        """
        restrict("UnregisteredSettings.FOO")
        assert is_key_allowed("UnregisteredSettings", "FOO") is True
        assert is_key_allowed("UnregisteredSettings", "BAR") is False
        assert is_key_allowed("ALERTS_SETTINGS", "FOO") is False

    def test_service_and_app_owned_classes_share_allowlist_spelling(
        self, restrict: Callable[..., None]
    ) -> None:
        """Match a core class and an app-owned class by ``__name__``, not the token.

        ``AlertsSettings`` is app-owned and has no core identifier; the allowlist
        must still address it the same way it addresses ``ExtensionsSettings``.
        The storage tokens (``EXTENSIONS_SETTINGS``, ``ALERTS_SETTINGS``) grant nothing.
        """
        restrict(
            "ExtensionsSettings.CONNECTIVITY_CHECK_DEFAULT",
            "AlertsSettings.BACKUP_RETENTION",
        )
        assert (
            is_key_allowed("ExtensionsSettings", "CONNECTIVITY_CHECK_DEFAULT") is True
        )
        assert is_key_allowed("ExtensionsSettings", "INVENTORY_ENDPOINT") is False
        assert is_key_allowed("AlertsSettings", "BACKUP_RETENTION") is True
        assert is_key_allowed("AlertsSettings", "ALERT_FOLDER_NAME") is False
        assert (
            is_key_allowed("EXTENSIONS_SETTINGS", "CONNECTIVITY_CHECK_DEFAULT") is False
        )
        assert is_key_allowed("ALERTS_SETTINGS", "BACKUP_RETENTION") is False

    def test_parent_addressable_via_allowed_leaf(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed leaf keeps its parent addressable for nested writes."""
        restrict(CARVE_OUT_KEY)
        assert has_allowed_key_under("Settings", "PMM") is True
        assert has_allowed_key_under("Settings", "SECURITY_HEADERS") is False

    def test_parent_addressable_via_exact_entry(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert an entry naming the parent itself keeps the parent addressable."""
        restrict("Settings.PMM")
        assert has_allowed_key_under("Settings", "PMM") is True

    def test_prefix_match_requires_a_segment_boundary(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a shared textual prefix does not make an unrelated parent open."""
        restrict("ExtensionsSettings.SESSION_REFRESH__ENABLED")
        assert has_allowed_key_under("ExtensionsSettings", "SESSION") is False


class TestShippedValue:
    """Cover the allowlist the side-car image bakes in."""

    def test_every_entry_resolves(self) -> None:
        """Assert each shipped entry names a real settings class and field."""
        for entry in shipped_allowed_keys():
            class_token, key = entry.split(".", 1)
            settings_cls = SETTINGS_CLASSES[class_token]
            if "__" in key:
                resolved = resolve_nested_field(settings_cls, key)
                assert resolved is not None, f"{entry} does not resolve"
                assert "__".join(resolved[0]) == key, f"{entry} is not canonical"
            else:
                assert key in settings_cls.model_fields, f"{entry} does not resolve"

    def test_every_entry_unlocks_a_listed_key(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert each shipped entry leaves a key the settings list renders HOT."""
        for entry in shipped_allowed_keys():
            class_token, _, _key = entry.partition(".")
            settings_cls = SETTINGS_CLASSES[class_token]
            restrict(entry)
            assert listed_hot_keys(settings_cls), f"{entry} unlocks no listed key"

    def test_entries_are_unique(self) -> None:
        """Assert the shipped list carries no duplicate entry."""
        entries = shipped_allowed_keys()
        assert len(entries) == len(set(entries))

    @pytest.mark.parametrize("entry", TOPOLOGY_KEYS)
    def test_topology_keys_are_locked(self, entry: str) -> None:
        """Assert no provisioned-topology key is tunable in the embedded image."""
        assert entry not in shipped_allowed_keys()

    def test_annotations_carve_out_is_allowed(self) -> None:
        """Assert the one documented product-default carve-out stays tunable."""
        assert CARVE_OUT_KEY in shipped_allowed_keys()

    def test_no_nomad_key_is_allowed(self) -> None:
        """Assert the whole Nomad subtree is locked, not only its endpoint."""
        assert not [
            entry
            for entry in shipped_allowed_keys()
            if entry.startswith("TasksSettings.NOMAD")
        ]


class TestImportOrder:
    """Cover the module-import cycle the lazy settings read exists to avoid."""

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("app.core.settings_override.policy", "app.core.config"),
            ("app.core.config", "app.core.settings_override.policy"),
        ],
    )
    def test_both_import_orders_succeed(self, first: str, second: str) -> None:
        """Assert neither import order trips a circular import at startup."""
        result = subprocess.run(
            [sys.executable, "-c", f"import {first}; import {second}"],
            capture_output=True,
            check=False,
            cwd=BASE_DIR,
            text=True,
        )
        assert result.returncode == 0, result.stderr
