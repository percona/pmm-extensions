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

"""Tests for the snapshot-building cache layer."""

import logging
from datetime import timedelta
from functools import cached_property
from string import Template
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel, computed_field
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.config import AlertSettings
from app.core.alerts.models import BaseAlertProvider
from app.core.config import Settings
from app.core.encryption import encrypt, mark_ciphertext
from app.core.settings_override import cache
from app.core.settings_override.cache import (
    _build_nested_update,
    _parent_base_value,
    build_snapshot,
    load_stored_overrides,
)
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token
from app.core.settings_override.registry import MaterializerPurpose
from app.core.utils.fields import LogLevel
from app.extensions.config import CookieOptions, ExtensionsSettings
from app.tasks.config import PreExecutionCheckMode, TasksSettings
from app.tasks.execution.executors.nomad import NomadExecutor
from tests.app.core.settings_override.conftest import (
    ALERT_SETTINGS_TOKEN,
    EXTENSIONS_SETTINGS_TOKEN,
    insert_override_row,
    PMM_API_KEY,
    PMM_ENDPOINT,
    ROUTING_KEY,
    SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
    TimingSettings,
)

_NOMAD_OVERRIDE_TIMEOUT = 30


def _foreign_token(value: str = "written under another key") -> str:
    """Return ciphertext minted with a key the configured one cannot decrypt.

    :param value: The plaintext to encrypt with the foreign key.
    :return: The foreign Fernet token.
    """
    return Fernet(Fernet.generate_key()).encrypt(value.encode()).decode("ascii")


@pytest.mark.asyncio
async def test_empty_table_yields_empty_snapshot(session: AsyncSession) -> None:
    """An empty override table produces an empty snapshot."""
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert dict(snapshot) == {}


@pytest.mark.asyncio
async def test_active_hot_row_appears_in_snapshot(session: AsyncSession) -> None:
    """Active rows whose key is HOT are surfaced through the snapshot."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="CONNECTIVITY_CHECK_DEFAULT",
        value=False,
        is_active=True,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert snapshot["CONNECTIVITY_CHECK_DEFAULT"] is False


@pytest.mark.asyncio
async def test_inactive_rows_skipped(session: AsyncSession) -> None:
    """Rows with ``is_active=False`` do not enter the snapshot."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="CONNECTIVITY_CHECK_DEFAULT",
        value=False,
        is_active=False,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "CONNECTIVITY_CHECK_DEFAULT" not in snapshot


@pytest.mark.asyncio
async def test_non_hot_field_skipped_with_warning(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A row for a NOT_OVERRIDABLE field is skipped and a warning is logged."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="PROXY_HEADERS",
        value=True,
        is_active=True,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "PROXY_HEADERS" not in snapshot
    assert any("non-HOT" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unknown_field_skipped_with_warning(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A row for a field that does not exist on the model is skipped."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="DOES_NOT_EXIST",
        value=True,
        is_active=True,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "DOES_NOT_EXIST" not in snapshot
    assert any("unknown field" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_coerces_int_for_positive_int_field(session: AsyncSession) -> None:
    """A JSON int round-trips to ``PositiveInt`` for ``ARTIFACT_DOWNLOAD_TTL``."""
    override_ttl = 120
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="ARTIFACT_DOWNLOAD_TTL",
        value=override_ttl,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert snapshot["ARTIFACT_DOWNLOAD_TTL"] == override_ttl


@pytest.mark.asyncio
async def test_coerces_strenum_for_pre_execution_check(
    session: AsyncSession,
) -> None:
    """A JSON string is coerced into the StrEnum ``PreExecutionCheckMode``."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="PRE_EXECUTION_CONNECTIVITY_CHECK",
        value="block",
    )
    snapshot = await build_snapshot(session, TasksSettings)
    assert snapshot["PRE_EXECUTION_CONNECTIVITY_CHECK"] is PreExecutionCheckMode.BLOCK


@pytest.mark.asyncio
async def test_coercion_failure_skipped_and_logged(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A row whose value fails Pydantic coercion is dropped with a warning."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="ARTIFACT_DOWNLOAD_TTL",
        value="not-a-number",
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "ARTIFACT_DOWNLOAD_TTL" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_dict_for_scalar_field_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A JSON object override for a scalar field is dropped (nested overrides out of scope)."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="CONNECTIVITY_CHECK_DEFAULT",
        value={"nested": True},
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "CONNECTIVITY_CHECK_DEFAULT" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_positive_int_constraint_rejects_zero(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """``PositiveInt`` constraint metadata is preserved across coercion.

    ``ARTIFACT_DOWNLOAD_TTL`` is annotated ``PositiveInt`` (= ``Annotated[int,
    Gt(0)]``). The bare-annotation coercion would accept ``0`` because
    ``TypeAdapter(int)`` drops the constraint, leaving issued tokens to
    expire immediately. The cache preserves the constraint and rejects.
    """
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="ARTIFACT_DOWNLOAD_TTL",
        value=0,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "ARTIFACT_DOWNLOAD_TTL" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_positive_int_constraint_rejects_negative(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Negative integers are rejected for ``PositiveInt`` fields."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="STALENESS_THRESHOLD_SECONDS",
        value=-1,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    assert "STALENESS_THRESHOLD_SECONDS" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_other_entries_remain_after_failure(session: AsyncSession) -> None:
    """A single coercion failure does not drop other valid entries."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="ARTIFACT_DOWNLOAD_TTL",
        value="not-a-number",
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="CONNECTIVITY_CHECK_DEFAULT",
        value=False,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert snapshot == {"CONNECTIVITY_CHECK_DEFAULT": False}


@pytest.mark.asyncio
async def test_providers_materialized_via_owning_model(session: AsyncSession) -> None:
    """``PROVIDERS`` snapshots a ``set`` of providers built by the before-validator."""
    await insert_override_row(
        session,
        setting_class=ALERT_SETTINGS_TOKEN,
        key="PROVIDERS",
        value=[{"PROVIDER": "pagerduty", "routing_key": "abc123"}],
    )
    snapshot = await build_snapshot(session, AlertSettings)
    providers = snapshot["PROVIDERS"]
    assert isinstance(providers, set)
    assert all(isinstance(provider, BaseAlertProvider) for provider in providers)


@pytest.mark.asyncio
async def test_invalid_providers_value_logged_and_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A before-validator ``ValueError`` is caught, logged, and the row skipped."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=ALERT_SETTINGS_TOKEN,
        key="PROVIDERS",
        value=[{"routing_key": "no-provider-key"}],
    )
    snapshot = await build_snapshot(session, AlertSettings)
    assert "PROVIDERS" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_footer_template_materialized_to_template(session: AsyncSession) -> None:
    """``FOOTER_TEMPLATE`` snapshots a ``Template`` without crashing the build."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="FOOTER_TEMPLATE",
        value="$summary v$version",
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert isinstance(snapshot["FOOTER_TEMPLATE"], Template)
    assert snapshot["FOOTER_TEMPLATE"].template == "$summary v$version"


@pytest.mark.asyncio
async def test_snapshot_build_materializes_with_the_snapshot_purpose(
    session: AsyncSession, mocker: MockerFixture
) -> None:
    """Tell a materializer it is reading a stored row, not validating a new payload.

    A row was written against whatever the deployment declared at the time, so a
    materializer that cross-checks its payload against current state has to be
    able to treat a mismatch as drift here and as a client error on PATCH.
    """
    materialize = mocker.spy(cache, "materialize_override_value")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="FOOTER_TEMPLATE",
        value="$summary",
    )

    await build_snapshot(session, ExtensionsSettings)

    assert materialize.call_args.kwargs["purpose"] is MaterializerPurpose.SNAPSHOT


@pytest.mark.asyncio
async def test_invalid_footer_template_value_logged_and_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-string ``FOOTER_TEMPLATE`` override is caught, logged, and skipped."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="FOOTER_TEMPLATE",
        value=123,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "FOOTER_TEMPLATE" not in snapshot
    assert any("coercion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_nomad_per_leaf_override_merged_as_executor(
    session: AsyncSession,
) -> None:
    """Snapshot a per-leaf ``NOMAD`` override as a merged ``NomadExecutor``."""
    nomad = NomadExecutor(endpoint="http://nomad.example:4646")
    base = SimpleNamespace(NOMAD=nomad)
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD__TIMEOUT",
        value=_NOMAD_OVERRIDE_TIMEOUT,
    )
    first = await build_snapshot(session, TasksSettings, base_settings=base)
    second = await build_snapshot(session, TasksSettings, base_settings=base)
    assert isinstance(first["NOMAD"], NomadExecutor)
    assert first["NOMAD"].timeout == _NOMAD_OVERRIDE_TIMEOUT
    assert first["NOMAD"] == second["NOMAD"]


# --- Nested overrides -----------------------------------------------------


@pytest.mark.asyncio
async def test_nested_override_appears_as_model_copy_under_top_level_key(
    session: AsyncSession,
) -> None:
    """A nested row merges into a copy stored under the top-level key."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value=3600,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    merged = snapshot["SESSION_REFRESH"]
    assert isinstance(merged, CookieOptions)
    assert timedelta(seconds=3600) == merged.MAX_AGE
    # Untouched leaves keep the field's own declared default, which is not the
    # bare ``CookieOptions()`` default (``SESSION_REFRESH`` pins its own).
    declared_default = ExtensionsSettings.model_fields["SESSION_REFRESH"].default
    assert merged.COOKIE_NAME == declared_default.COOKIE_NAME
    assert merged.SAMESITE == declared_default.SAMESITE


@pytest.mark.asyncio
async def test_nested_override_merges_onto_base_settings_value(
    session: AsyncSession,
) -> None:
    """Leaves with no override row fall back to the YAML/env base value (AC #3)."""
    base = SimpleNamespace(SESSION_REFRESH=CookieOptions(SAMESITE="strict"))
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value=3600,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings, base_settings=base)
    merged = snapshot["SESSION_REFRESH"]
    assert timedelta(seconds=3600) == merged.MAX_AGE
    # The non-overridden SAMESITE keeps the base (YAML/env) value, not the default.
    assert merged.SAMESITE == "strict"


@pytest.mark.asyncio
async def test_mixed_case_sibling_rows_merge_into_one_parent(
    session: AsyncSession,
) -> None:
    """Sibling rows for one parent under different casings both survive the merge.

    The API persists canonical keys, but a row inserted directly into the table
    may carry a non-canonical casing. Grouping must fold on the case-insensitive
    prefix so two spellings of the same parent merge together instead of one
    group clobbering the other's ``snapshot[parent]`` write.
    """
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="session_refresh__max_age",
        value=3600,
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__SAMESITE",
        value="strict",
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    merged = snapshot["SESSION_REFRESH"]
    assert timedelta(seconds=3600) == merged.MAX_AGE
    assert merged.SAMESITE == "strict"


@pytest.mark.asyncio
async def test_duplicate_canonical_leaf_keeps_newest_row(
    session: AsyncSession,
) -> None:
    """Two raw keys resolving to one canonical leaf resolve to the newest row.

    The API persists canonical keys (the unique index then forbids duplicates),
    but a differently-cased row inserted directly into the table can coexist with
    a canonical one. The manager lists rows newest-first, so the merge must keep
    the first-seen value -- the newest row wins deterministically rather than an
    older row clobbering it.
    """
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="session_refresh__max_age",
        value=3600,
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value=7200,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert timedelta(seconds=7200) == snapshot["SESSION_REFRESH"].MAX_AGE


@pytest.mark.asyncio
async def test_nested_override_falls_back_when_parent_not_overridable(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A nested row under a non-overridable parent is skipped and warned."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="DATABASE__NAME",
        value="other.db",
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "DATABASE" not in snapshot
    assert any("non-overridable parent" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_multi_level_nested_instantiates_none_intermediate(
    session: AsyncSession,
) -> None:
    """A multi-level path through a ``None`` intermediate instantiates it from leaves."""
    max_age = 31536000
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE",
        value=max_age,
    )
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__INCLUDE_SUB_DOMAINS",
        value=True,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    sts = snapshot["SECURITY_HEADERS"].strict_transport_security
    assert sts is not None
    assert sts.max_age == max_age
    assert sts.include_sub_domains is True


@pytest.mark.asyncio
async def test_multi_level_missing_required_leaf_skips_group(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Instantiating a ``None`` intermediate without its required leaf is skipped."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    # ``max_age`` is required on StrictTransportSecurityOptions; omit it.
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__INCLUDE_SUB_DOMAINS",
        value=True,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    assert "SECURITY_HEADERS" not in snapshot
    assert any("merged model" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unknown_nested_leaf_skipped_with_warning(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown nested leaf is skipped; the parent is not stored if it was the only row."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__BOGUS_FIELD",
        value=1,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    assert "SESSION_REFRESH" not in snapshot
    assert any(
        "unknown or not-overridable field" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_nested_coercion_failure_skips_only_failing_leaf(
    session: AsyncSession,
) -> None:
    """One bad nested leaf is dropped while a sibling leaf still merges."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value="not-a-number",
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__SAMESITE",
        value="strict",
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    merged = snapshot["SESSION_REFRESH"]
    assert merged.SAMESITE == "strict"
    # The failed MAX_AGE leaf keeps the default.
    assert merged.MAX_AGE == CookieOptions().MAX_AGE


@pytest.mark.asyncio
async def test_top_level_row_targeting_nested_only_parent_is_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A whole-parent row on a NESTED_ONLY parent is dropped; nested rows still merge."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH",
        value={"MAX_AGE": 10, "SAMESITE": "none"},
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value=3600,
    )
    snapshot = await build_snapshot(session, ExtensionsSettings)
    merged = snapshot["SESSION_REFRESH"]
    assert timedelta(seconds=3600) == merged.MAX_AGE
    # The whole-object row did not take effect (SAMESITE stays default).
    assert merged.SAMESITE == CookieOptions().SAMESITE
    assert any("non-HOT" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_top_level_nomad_row_targeting_nested_only_parent_is_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Drop a whole-parent ``NOMAD`` row while still merging per-leaf rows."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    nomad = NomadExecutor(endpoint="http://nomad.example:4646")
    base = SimpleNamespace(NOMAD=nomad)
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD",
        value={
            "endpoint": "https://nomad-whole-override.example.org",
            "timeout": 99,
        },
    )
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD__TIMEOUT",
        value=_NOMAD_OVERRIDE_TIMEOUT,
    )
    snapshot = await build_snapshot(session, TasksSettings, base_settings=base)
    merged = snapshot["NOMAD"]
    assert isinstance(merged, NomadExecutor)
    assert merged.timeout == _NOMAD_OVERRIDE_TIMEOUT
    assert str(merged.endpoint).startswith("http://nomad.example")
    assert any("non-HOT" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_security_headers_uppercase_key_resolves_to_lowercase_attribute(
    session: AsyncSession,
) -> None:
    """An uppercase nested key resolves to the lowercase case-insensitive attribute."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__X_FRAME_OPTIONS_DENY",
        value=False,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    assert snapshot["SECURITY_HEADERS"].x_frame_options_deny is False


@pytest.mark.asyncio
async def test_security_headers_lowercase_key_also_resolves(
    session: AsyncSession,
) -> None:
    """A lowercase nested key resolves to the same case-insensitive attribute."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="security_headers__x_frame_options_deny",
        value=False,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    assert snapshot["SECURITY_HEADERS"].x_frame_options_deny is False


@pytest.mark.asyncio
async def test_cached_property_cleared_on_merged_copy(
    session: AsyncSession,
) -> None:
    """The merged copy drops any ``cached_property`` carried over by ``model_copy``."""
    override_timeout = 30
    nomad = NomadExecutor(endpoint="http://nomad.example:4646")
    assert nomad.backend is not None  # populate the cached_property memo
    assert "backend" in nomad.__dict__
    base = SimpleNamespace(NOMAD=nomad)
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD__TIMEOUT",
        value=override_timeout,
    )
    snapshot = await build_snapshot(session, TasksSettings, base_settings=base)
    merged = snapshot["NOMAD"]
    assert merged.timeout == override_timeout
    assert "backend" not in merged.__dict__


@pytest.mark.asyncio
async def test_snapshot_refresh_replaces_merged_copy(session: AsyncSession) -> None:
    """Deleting the only nested row drops the merged parent on the next build."""
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
        value=100,
    )
    first = await build_snapshot(session, ExtensionsSettings)
    assert "SESSION_REFRESH" in first
    await SettingsOverrideManager.delete_where(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key="SESSION_REFRESH__MAX_AGE",
    )
    second = await build_snapshot(session, ExtensionsSettings)
    assert "SESSION_REFRESH" not in second


class _Derived(BaseModel):
    """Model with a ``computed_field`` derived from a plain field."""

    base: int = 1

    @computed_field
    @property
    def doubled(self) -> int:
        """Return twice ``base``, recomputed at access time."""
        return self.base * 2


def test_model_copy_recomputes_computed_field() -> None:
    """``model_copy(update=...)`` recomputes a ``@computed_field`` (AC #6 mechanism)."""
    base, updated_base = 5, 7
    original = _Derived(base=base)
    assert original.doubled == base * 2
    copy = original.model_copy(update={"base": updated_base})
    assert copy.doubled == updated_base * 2


class _CachedChild(BaseModel):
    """Nested child carrying a ``cached_property`` to merge through."""

    v: int = 1

    @cached_property
    def memo(self) -> int:
        """Return a value derived from ``v`` (memoised on first access)."""
        return self.v * 100


class _CachedParent(BaseModel):
    """Parent holding a nested child with a ``cached_property``."""

    child: _CachedChild = _CachedChild()


def test_nested_child_cached_property_cleared_at_every_level() -> None:
    """A nested child's ``cached_property`` memo is dropped on the merged copy."""
    override_v = 5
    parent = _CachedParent()
    assert parent.child.memo == parent.child.v * 100  # populate the child memo
    merged = _build_nested_update(parent, {("child", "v"): override_v})
    assert merged.child.v == override_v
    assert "memo" not in merged.child.__dict__


@pytest.mark.asyncio
async def test_whole_child_and_leaf_override_layer_together(
    session: AsyncSession,
) -> None:
    """A whole-child override and a deeper leaf override of it both apply."""
    max_age = 100
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY",
        value={"max_age": max_age, "preload": True},
    )
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__INCLUDE_SUB_DOMAINS",
        value=True,
    )
    snapshot = await build_snapshot(session, TasksSettings)
    sts = snapshot["SECURITY_HEADERS"].strict_transport_security
    assert sts.max_age == max_age  # from the whole-child override
    assert sts.preload is True  # from the whole-child override
    assert sts.include_sub_domains is True  # layered from the leaf override


@pytest.mark.asyncio
async def test_inherited_cached_property_cleared_on_merged_copy(
    session: AsyncSession,
) -> None:
    """A ``cached_property`` inherited from a base class is cleared on the copy.

    ``NomadExecutor`` inherits ``logger`` (derived from ``logger_name``) from
    ``BaseRemoteAPI``; overriding ``NOMAD__LOGGER_NAME`` must not leave the old
    logger memoised on the merged copy.
    """
    nomad = NomadExecutor(endpoint="http://nomad.example:4646")
    assert nomad.logger is not None  # populate the inherited cached_property
    assert "logger" in nomad.__dict__
    base = SimpleNamespace(NOMAD=nomad)
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD__LOGGER_NAME",
        value="custom.logger",
    )
    snapshot = await build_snapshot(session, TasksSettings, base_settings=base)
    assert "logger" not in snapshot["NOMAD"].__dict__


def test_parent_base_value_prefers_whole_object_snapshot_entry() -> None:
    """A whole-object override already in the snapshot is the base for nested merges.

    When a HOT model parent has a whole-object override stored under its key, the
    nested-group merge must layer leaves on top of *that* override rather than the
    YAML/env value -- otherwise whole-object fields not touched by a leaf row are
    silently reverted.
    """
    whole_object = CookieOptions(MAX_AGE=timedelta(seconds=111))
    field_info = ExtensionsSettings.model_fields["SESSION_REFRESH"]
    result = _parent_base_value(
        {"SESSION_REFRESH": whole_object},
        field_info,
        "SESSION_REFRESH",
        base_settings=None,
    )
    assert result is whole_object


def test_parent_base_value_falls_back_to_field_default() -> None:
    """With no snapshot entry and no base settings, the field default seeds the merge."""
    field_info = ExtensionsSettings.model_fields["SESSION_REFRESH"]
    result = _parent_base_value({}, field_info, "SESSION_REFRESH", base_settings=None)
    assert isinstance(result, CookieOptions)


@pytest.mark.asyncio
async def test_secret_leaf_decrypted_before_materialization(
    session: AsyncSession,
) -> None:
    """An encrypted secret leaf is decrypted before the row is coerced."""
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM",
        value={"endpoint": PMM_ENDPOINT, "api_key": encrypt(PMM_API_KEY)},
    )
    snapshot = await build_snapshot(session, Settings)
    assert snapshot["PMM"].api_key.get_secret_value() == PMM_API_KEY
    assert snapshot["PMM"].endpoint == PMM_ENDPOINT


@pytest.mark.asyncio
async def test_marked_secret_leaf_decrypted_before_materialization(
    session: AsyncSession,
) -> None:
    """Decrypt a secret leaf carrying the envelope marker like any other.

    The sibling of the unmarked case above, and both have to keep passing: the
    two shapes coexist for the life of every deployment upgraded into the
    envelope, because nothing re-marks a row written before it. A snapshot that
    read only one would silently fall back to the YAML value for the other.
    """
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM",
        value={
            "endpoint": PMM_ENDPOINT,
            "api_key": mark_ciphertext(encrypt(PMM_API_KEY)),
        },
    )
    snapshot = await build_snapshot(session, Settings)
    assert snapshot["PMM"].api_key.get_secret_value() == PMM_API_KEY
    assert snapshot["PMM"].endpoint == PMM_ENDPOINT


@pytest.mark.asyncio
async def test_legacy_plaintext_secret_row_still_resolves(
    session: AsyncSession,
) -> None:
    """A row written before the re-encryption migration keeps resolving."""
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM",
        value={"endpoint": PMM_ENDPOINT, "api_key": PMM_API_KEY},
    )
    snapshot = await build_snapshot(session, Settings)
    assert snapshot["PMM"].api_key.get_secret_value() == PMM_API_KEY


@pytest.mark.asyncio
async def test_undecryptable_secret_row_logged_and_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A row encrypted under another key is dropped with a decryption warning.

    The key is absent from the snapshot rather than retained, so the proxy
    falls back to the YAML/env value instead of serving a stale credential.
    """
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM",
        value={"endpoint": PMM_ENDPOINT, "api_key": _foreign_token()},
    )
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="LOGGING",
        value="DEBUG",
    )
    snapshot = await build_snapshot(session, Settings)
    assert "PMM" not in snapshot
    assert snapshot["LOGGING"] is LogLevel.DEBUG
    assert any("decrypt" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_nested_secret_row_decrypted(session: AsyncSession) -> None:
    """A ``__``-delimited row whose whole value is the secret is decrypted."""
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM__API_KEY",
        value=encrypt(PMM_API_KEY),
    )
    snapshot = await build_snapshot(session, Settings)
    assert snapshot["PMM"].api_key.get_secret_value() == PMM_API_KEY


@pytest.mark.asyncio
async def test_undecryptable_nested_secret_row_logged_and_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """An undecryptable nested leaf is skipped without failing its siblings."""
    caplog.set_level(logging.WARNING, logger="app.core.settings_override.cache")
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM__API_KEY",
        value=_foreign_token(),
    )
    await insert_override_row(
        session,
        setting_class=SETTINGS_TOKEN,
        key="PMM__ENDPOINT",
        value=PMM_ENDPOINT,
    )
    snapshot = await build_snapshot(session, Settings)
    assert snapshot["PMM"].endpoint == PMM_ENDPOINT
    assert snapshot["PMM"].api_key is None
    assert any("decrypt" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_materializer_backed_provider_secret_decrypted(
    session: AsyncSession,
) -> None:
    """A materializer-backed ``PROVIDERS`` row resolves its routing key in plaintext."""
    await insert_override_row(
        session,
        setting_class=ALERT_SETTINGS_TOKEN,
        key="PROVIDERS",
        value=[{"PROVIDER": "pagerduty", "routing_key": encrypt(ROUTING_KEY)}],
    )
    snapshot = await build_snapshot(session, AlertSettings)
    provider = next(iter(snapshot["PROVIDERS"]))
    assert provider.routing_key.get_secret_value() == ROUTING_KEY


class TestBuildSnapshotCrossFieldRules:
    """Group the snapshot fallback for stored overrides that break a cross-field rule.

    Rows can disagree with each other without the API ever accepting the pair:
    two concurrent PATCHes each judged against the other's old value, a YAML
    change on redeploy, or a row written straight to the table.
    """

    TOKEN = setting_class_token(TimingSettings)

    async def _store(self, session: AsyncSession, **values: int) -> None:
        """Insert one active override row per ``values`` entry.

        :param session: The database session.
        :param values: Field name to stored value.
        """
        for key, value in values.items():
            await insert_override_row(
                session, setting_class=self.TOKEN, key=key, value=value
            )

    @pytest.mark.asyncio
    async def test_a_jointly_violating_pair_falls_back_to_the_base_values(
        self, session: AsyncSession
    ) -> None:
        """Ignore both stored halves so each field takes its YAML/env value.

        :param session: The database session.
        """
        await self._store(session, POLL=60, TIMEOUT=1)

        snapshot = await build_snapshot(session, TimingSettings, TimingSettings())

        assert dict(snapshot) == {}

    @pytest.mark.asyncio
    async def test_one_warning_names_the_ignored_fields_and_no_values(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Emit exactly one warning naming every ignored field.

        :param session: The database session.
        :param caplog: Fixture capturing log records.
        """
        await self._store(session, POLL=60061, TIMEOUT=1)

        with caplog.at_level(logging.WARNING, logger=cache.logger.name):
            await build_snapshot(session, TimingSettings, TimingSettings())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "POLL, TIMEOUT" in warnings[0].getMessage()
        assert "60061" not in caplog.text

    @pytest.mark.asyncio
    async def test_restoring_a_base_value_drops_the_override_it_breaks(
        self, session: AsyncSession
    ) -> None:
        """Keep dropping until every rule holds, not just the first one broken.

        :param session: The database session.
        """
        await self._store(session, POLL=90, TIMEOUT=30, STALE=100)

        snapshot = await build_snapshot(session, TimingSettings, TimingSettings())

        assert dict(snapshot) == {}

    @pytest.mark.asyncio
    async def test_the_declared_defaults_are_the_base_without_an_instance(
        self, session: AsyncSession
    ) -> None:
        """Judge an un-overridden field by its declared default when no base is given.

        :param session: The database session.
        """
        await self._store(session, POLL=181)

        snapshot = await build_snapshot(session, TimingSettings)

        assert dict(snapshot) == {}

    @pytest.mark.asyncio
    async def test_an_unrelated_override_survives(self, session: AsyncSession) -> None:
        """Keep overrides outside the violated relationship.

        :param session: The database session.
        """
        await self._store(session, POLL=60, TIMEOUT=1, BYSTANDER=7)

        snapshot = await build_snapshot(session, TimingSettings, TimingSettings())

        assert dict(snapshot) == {"BYSTANDER": 7}

    @pytest.mark.asyncio
    async def test_a_consistent_set_of_overrides_is_kept(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Publish a consistent set unchanged and without a warning.

        :param session: The database session.
        :param caplog: Fixture capturing log records.
        """
        await self._store(session, POLL=30, TIMEOUT=60, STALE=120)

        with caplog.at_level(logging.WARNING, logger=cache.logger.name):
            snapshot = await build_snapshot(session, TimingSettings, TimingSettings())

        assert dict(snapshot) == {"POLL": 30, "TIMEOUT": 60, "STALE": 120}
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_a_class_without_rules_never_warns(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Leave classes that declare no rules exactly as before.

        :param session: The database session.
        :param caplog: Fixture capturing log records.
        """
        override = not ExtensionsSettings().CONNECTIVITY_CHECK_DEFAULT
        await insert_override_row(
            session,
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="CONNECTIVITY_CHECK_DEFAULT",
            value=override,
        )

        with caplog.at_level(logging.WARNING, logger=cache.logger.name):
            snapshot = await build_snapshot(session, ExtensionsSettings)

        assert snapshot["CONNECTIVITY_CHECK_DEFAULT"] is override
        assert not caplog.records


class TestLoadStoredOverrides:
    """Group the read of stored overrides before cross-field rules apply."""

    @pytest.mark.asyncio
    async def test_a_pair_the_snapshot_ignores_is_still_returned(
        self, session: AsyncSession
    ) -> None:
        """Return what is stored, so a caller can judge a change against it.

        :param session: The database session.
        """
        for key, value in {"POLL": 60, "TIMEOUT": 1}.items():
            await insert_override_row(
                session,
                setting_class=setting_class_token(TimingSettings),
                key=key,
                value=value,
            )

        assert await load_stored_overrides(
            session, TimingSettings, TimingSettings()
        ) == {
            "POLL": 60,
            "TIMEOUT": 1,
        }
