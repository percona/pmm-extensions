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

"""Cover the allowlist gate folded into the classification and snapshot layers.

Every case pins ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` explicitly; the unrestricted
counterparts assert that the default leaves each helper's answer unchanged.
"""

import logging
from collections.abc import Callable

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.config import AlertSettings
from app.core.config import Settings
from app.core.settings_override.cache import build_snapshot
from app.core.settings_override.registry import (
    chain_is_locked,
    coerce_nested_field_value,
    is_hot_reloadable,
    is_nested_overridable_parent,
    iter_class_fields,
    ReloadClassification,
)
from app.core.settings_override.resolution import resolve_nested_field_metadata
from app.extensions.apps.alerts.config import AlertsSettings
from app.extensions.config import ExtensionsSettings
from app.tasks.config import TasksSettings
from tests.app.core.settings_override.conftest import (
    EXTENSIONS_SETTINGS_TOKEN,
    insert_override_row,
    SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)

ANNOTATIONS_KEY = "Settings.PMM__annotations_enabled"
_SYNC_REFRESH_OVERRIDE = 11
_BACKUP_RETENTION_OVERRIDE = 7


def _reload_of(settings_cls: type, key: str) -> ReloadClassification:
    """Return the reload classification LIST/DETAIL report for a top-level key."""
    for field_meta in iter_class_fields(settings_cls):
        if field_meta.key == key:
            return field_meta.reload
    pytest.fail(f"{settings_cls.__name__}.{key} is not a declared field")


class TestTopLevelGate:
    """Cover the HOT predicate that gates PATCH and the snapshot's top-level rows."""

    def test_unlisted_key_stops_being_hot(self, restrict: Callable[..., None]) -> None:
        """Assert an unlisted HOT field reports non-HOT under an active restriction."""
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        assert is_hot_reloadable(ExtensionsSettings, "INVENTORY_ENDPOINT") is False
        assert is_hot_reloadable(ExtensionsSettings, "SYNC_REFRESH_TIME") is True

    def test_gate_can_be_bypassed(self, restrict: Callable[..., None]) -> None:
        """Assert the ungated view still reports the static classification."""
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        assert (
            is_hot_reloadable(
                ExtensionsSettings, "INVENTORY_ENDPOINT", include_policy_gate=False
            )
            is True
        )

    def test_unrestricted_keeps_every_hot_field(self) -> None:
        """Assert the default leaves the HOT predicate byte-identical."""
        assert is_hot_reloadable(ExtensionsSettings, "INVENTORY_ENDPOINT") is True
        assert is_hot_reloadable(ExtensionsSettings, "SYNC_REFRESH_TIME") is True

    def test_app_owned_class_defaults_to_locked(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert an app-owned class only exposes the fields its entries name."""
        restrict("AlertSettings.PROVIDERS")
        assert is_hot_reloadable(AlertSettings, "PROVIDERS") is True
        assert is_hot_reloadable(AlertSettings, "SOURCE_PREFIX") is False

    def test_fail_closed_for_unknown_settings_class(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a class with no allowlist entries is locked, not allowed."""

        class ProbeSettings(Settings):
            """Stand in for a settings class the allowlist does not name."""

        restrict("Settings.LOGGING")
        assert is_hot_reloadable(Settings, "LOGGING") is True
        assert is_hot_reloadable(ProbeSettings, "LOGGING") is False

    def test_unregistered_class_honours_allowlist_by_name(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a class with no core identifier still matches ``ALLOWED_KEYS`` by ``__name__``.

        App-owned classes have no core identifier, so a gate that mapped
        ``__name__`` through a closed core set would withhold every key,
        including ones the allowlist already names.
        """

        class ProbeSettings(Settings):
            """Stand in for an app-owned class with no core identifier."""

        restrict("ProbeSettings.LOGGING")
        assert is_hot_reloadable(ProbeSettings, "LOGGING") is True
        assert is_hot_reloadable(Settings, "LOGGING") is False
        assert is_nested_overridable_parent(ProbeSettings, "PMM") is False

        restrict("ProbeSettings.PMM__annotations_enabled")
        assert is_nested_overridable_parent(ProbeSettings, "PMM") is True
        assert is_nested_overridable_parent(Settings, "PMM") is False

    def test_listing_reports_the_gated_classification(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert LIST/DETAIL report a locked field as not overridable."""
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        assert _reload_of(ExtensionsSettings, "INVENTORY_ENDPOINT") is (
            ReloadClassification.NOT_OVERRIDABLE
        )
        assert (
            _reload_of(ExtensionsSettings, "SYNC_REFRESH_TIME")
            is ReloadClassification.HOT
        )


class TestRestrictOnlyInvariant:
    """Cover the invariant that the allowlist restricts but never grants."""

    def test_listing_a_static_lock_changes_nothing(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a statically locked field stays locked even when listed."""
        restrict("ExtensionsSettings.DIAGNOSTICS_DELIVERY")
        assert is_hot_reloadable(ExtensionsSettings, "DIAGNOSTICS_DELIVERY") is False
        assert _reload_of(ExtensionsSettings, "DIAGNOSTICS_DELIVERY") is (
            ReloadClassification.NOT_OVERRIDABLE
        )

    def test_listing_a_static_lock_leaves_its_children_locked(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a statically locked parent stays closed to nested writes when listed."""
        restrict("ExtensionsSettings.DIAGNOSTICS_DELIVERY")
        assert is_nested_overridable_parent(
            ExtensionsSettings, "DIAGNOSTICS_DELIVERY"
        ) is (False)
        assert (
            is_nested_overridable_parent(
                ExtensionsSettings, "DIAGNOSTICS_DELIVERY", include_policy_gate=False
            )
            is False
        )


class TestNestedParentGate:
    """Cover the parent-level predicate that gates nested PATCH and LIST expansion."""

    def test_fully_locked_parent_closes(self, restrict: Callable[..., None]) -> None:
        """Assert a parent with no allowed leaf stops accepting nested overrides."""
        restrict(ANNOTATIONS_KEY)
        assert is_nested_overridable_parent(TasksSettings, "NOMAD") is False

    def test_fully_locked_parent_stays_addressable_ungated(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert the ungated view keeps a fully locked parent addressable."""
        restrict(ANNOTATIONS_KEY)
        assert (
            is_nested_overridable_parent(
                TasksSettings, "NOMAD", include_policy_gate=False
            )
            is True
        )

    def test_partially_allowed_parent_stays_open(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert one allowed leaf keeps its parent addressable."""
        restrict(ANNOTATIONS_KEY)
        assert is_nested_overridable_parent(Settings, "PMM") is True

    def test_unrestricted_keeps_every_parent_open(self) -> None:
        """Assert the default leaves the parent predicate byte-identical."""
        assert is_nested_overridable_parent(TasksSettings, "NOMAD") is True
        assert is_nested_overridable_parent(Settings, "PMM") is True


class TestChainGate:
    """Cover the chain predicate that gates nested PATCH and leaf metadata."""

    def test_locked_leaf_reports_locked(self, restrict: Callable[..., None]) -> None:
        """Assert an unlisted leaf under an open parent is locked."""
        restrict(ANNOTATIONS_KEY)
        assert chain_is_locked(Settings, "PMM__endpoint") is True
        assert chain_is_locked(Settings, "PMM__annotations_enabled") is False

    def test_lookup_uses_the_canonical_chain(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert a non-canonically cased key resolves to the same allow decision."""
        restrict(ANNOTATIONS_KEY)
        assert chain_is_locked(Settings, "pmm__ANNOTATIONS_ENABLED") is False

    def test_unrestricted_leaves_every_leaf_open(self) -> None:
        """Assert the default only locks explicitly marked chains."""
        assert chain_is_locked(Settings, "PMM__endpoint") is False

    def test_unresolvable_key_is_not_locked(self) -> None:
        """Assert an unresolvable key is reported open, not locked.

        The caller surfaces the resolution failure itself; reporting it as
        locked here would turn a 404 into a 422.
        """
        assert chain_is_locked(Settings, "GONE__missing_leaf") is False

    def test_leaf_metadata_reports_the_gated_classification(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert per-leaf metadata reports locked leaves as not overridable."""
        restrict(ANNOTATIONS_KEY)
        locked = resolve_nested_field_metadata(Settings, "PMM__endpoint")
        allowed = resolve_nested_field_metadata(Settings, "PMM__annotations_enabled")
        assert locked is not None
        assert allowed is not None
        assert locked.reload is ReloadClassification.NOT_OVERRIDABLE
        assert allowed.reload is ReloadClassification.HOT

    def test_coercion_rejects_a_locked_leaf(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert the snapshot coercion path refuses a locked nested key."""
        restrict(ANNOTATIONS_KEY)
        with pytest.raises(KeyError):
            coerce_nested_field_value(TasksSettings, "NOMAD__timeout", 30)

    def test_coercion_accepts_an_allowed_leaf(
        self, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed nested key still coerces normally."""
        restrict(ANNOTATIONS_KEY)
        raw_value = True
        chain, value = coerce_nested_field_value(
            Settings, "PMM__annotations_enabled", raw_value
        )
        assert chain == ("PMM", "annotations_enabled")
        assert value is True


class TestSnapshotFiltering:
    """Cover the second line of defence: rows already in the table."""

    @pytest.mark.asyncio
    async def test_locked_top_level_row_is_skipped(
        self,
        session: AsyncSession,
        restrict: Callable[..., None],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Assert a pre-lockdown row for a now-locked field never reaches readers."""
        caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        await insert_override_row(
            session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="CONNECTIVITY_CHECK_DEFAULT",
            value=False,
            is_active=True,
        )
        snapshot = await build_snapshot(session, ExtensionsSettings)
        assert "CONNECTIVITY_CHECK_DEFAULT" not in snapshot
        assert any("non-HOT" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_allowed_top_level_row_is_applied(
        self, session: AsyncSession, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed field's row still lands in the snapshot."""
        restrict("ExtensionsSettings.SYNC_REFRESH_TIME")
        await insert_override_row(
            session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=_SYNC_REFRESH_OVERRIDE,
            is_active=True,
        )
        snapshot = await build_snapshot(session, ExtensionsSettings)
        assert snapshot["SYNC_REFRESH_TIME"] == _SYNC_REFRESH_OVERRIDE

    @pytest.mark.asyncio
    async def test_app_owned_class_honours_allowlist_like_core(
        self, session: AsyncSession, restrict: Callable[..., None]
    ) -> None:
        """Apply an allowed ``AlertsSettings`` row and skip a sibling the same way as core."""
        restrict("AlertsSettings.BACKUP_RETENTION")
        await insert_override_row(
            session,
            setting_class="ALERTS_SETTINGS",
            key="BACKUP_RETENTION",
            value=_BACKUP_RETENTION_OVERRIDE,
            is_active=True,
        )
        await insert_override_row(
            session,
            setting_class="ALERTS_SETTINGS",
            key="ALERT_FOLDER_NAME",
            value="Locked Folder",
            is_active=True,
        )
        snapshot = await build_snapshot(session, AlertsSettings)
        assert snapshot["BACKUP_RETENTION"] == _BACKUP_RETENTION_OVERRIDE
        assert "ALERT_FOLDER_NAME" not in snapshot

    @pytest.mark.asyncio
    async def test_locked_nested_row_is_skipped(
        self,
        session: AsyncSession,
        restrict: Callable[..., None],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Assert a row under a fully locked parent never reaches readers."""
        caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
        restrict(ANNOTATIONS_KEY)
        await insert_override_row(
            session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__timeout",
            value=30,
            is_active=True,
        )
        snapshot = await build_snapshot(session, TasksSettings)
        assert "NOMAD" not in snapshot
        assert any(
            "non-overridable parent" in record.getMessage() for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_locked_leaf_under_open_parent_is_skipped(
        self,
        session: AsyncSession,
        restrict: Callable[..., None],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Assert the per-leaf coercion gate skips a locked sibling of an open leaf."""
        caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
        restrict(ANNOTATIONS_KEY)
        await insert_override_row(
            session,
            setting_class=SETTINGS_TOKEN,
            key="PMM__endpoint",
            value="https://stale.example.com",
            is_active=True,
        )
        snapshot = await build_snapshot(session, Settings)
        assert "PMM" not in snapshot
        assert any(
            "not-overridable field" in record.getMessage() for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_allowed_nested_row_is_applied(
        self, session: AsyncSession, restrict: Callable[..., None]
    ) -> None:
        """Assert an allowed leaf's row still merges into its parent."""
        restrict(ANNOTATIONS_KEY)
        await insert_override_row(
            session,
            setting_class=SETTINGS_TOKEN,
            key="PMM__annotations_enabled",
            value=True,
            is_active=True,
        )
        snapshot = await build_snapshot(session, Settings)
        assert snapshot["PMM"].annotations_enabled is True
