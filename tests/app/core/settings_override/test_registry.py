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

"""Test settings-override classification, resolution, and PATCH preservation."""

import functools
from string import Template
from typing import ClassVar

import pytest
from pydantic import BaseModel, computed_field, Field, SecretBytes, SecretStr

from app.core.alerts.config import AlertSettings
from app.core.alerts.models import BaseAlertProvider
from app.core.config import BaseYamlSettings, Settings
from app.core.settings_override.registry import (
    _clear_cached_properties,
    chain_has_advanced,
    computed_field_info,
    field_materializer,
    field_reload_classification,
    hot_field,
    hot_field_names,
    is_advanced_field,
    is_explicit_not_overridable,
    is_hot_reloadable,
    iter_class_fields,
    materialize_override_value,
    materialize_template,
    materialize_via_owning_model,
    MaterializerContext,
    MaterializerPurpose,
    nested_overridable_field_names,
    ReloadClassification,
    unwrap_secrets_for_storage,
)
from app.core.settings_override.resolution import resolve_nested_field_metadata
from app.core.utils.pydantic import field_with_metadata
from app.extensions.config import ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.inventory.config import InventorySettings
from app.tasks.config import TasksSettings


class _CachedModel(BaseModel):
    """Model with a ``cached_property`` to exercise the memo-clearing helper."""

    value: int = 1

    @functools.cached_property
    def derived(self) -> int:
        """Return a value derived from ``value`` (memoised)."""
        return self.value * 10


def test_clear_cached_properties_removes_memo() -> None:
    """A populated ``cached_property`` memo is removed from ``__dict__``."""
    instance = _CachedModel(value=2)
    assert instance.derived == instance.value * 10  # populate the memo
    assert "derived" in instance.__dict__
    _clear_cached_properties(instance)
    assert "derived" not in instance.__dict__


def test_clear_cached_properties_noop_when_unpopulated() -> None:
    """Clearing an instance with no populated memo is a no-op."""
    instance = _CachedModel(value=2)
    _clear_cached_properties(instance)
    assert "derived" not in instance.__dict__


def _ctx(settings_cls: type, field_name: str, raw: object) -> MaterializerContext:
    """Build a :class:`MaterializerContext` for the given class field and raw value."""
    return MaterializerContext(
        settings_cls, field_name, settings_cls.model_fields[field_name], raw
    )


class _ProbeWithMaterializer(BaseModel):
    """A probe model whose HOT field declares a materializer."""

    value: str = hot_field("", materializer=materialize_template)


class _ProbeWithoutMaterializer(BaseModel):
    """A probe model whose HOT field declares no materializer."""

    value: str = hot_field("")


def test_hot_field_records_materializer_in_metadata() -> None:
    """Assert ``hot_field(materializer=...)`` round-trips the callable through metadata."""
    assert field_materializer(_ProbeWithMaterializer, "value") is materialize_template


def test_hot_field_without_materializer_returns_none() -> None:
    """Assert a HOT field declared without a materializer reports ``None``."""
    assert field_materializer(_ProbeWithoutMaterializer, "value") is None


def test_field_materializer_unknown_field_returns_none() -> None:
    """Assert an unknown field name reports no materializer instead of raising."""
    assert field_materializer(ExtensionsSettings, "DOES_NOT_EXIST") is None


def test_materialize_via_owning_model_runs_before_validator() -> None:
    """Assert ``materialize_via_owning_model`` runs the owning model's before-validator."""
    result = materialize_via_owning_model(
        _ctx(
            AlertSettings,
            "PROVIDERS",
            [{"PROVIDER": "pagerduty", "routing_key": "abc123"}],
        )
    )
    assert isinstance(result, set)
    assert all(isinstance(provider, BaseAlertProvider) for provider in result)
    assert len(result) == 1


def test_materialize_template_builds_template_from_string() -> None:
    """Assert ``materialize_template`` converts a raw string into a ``Template``."""
    result = materialize_template(
        _ctx(ExtensionsSettings, "FOOTER_TEMPLATE", "$summary")
    )
    assert isinstance(result, Template)
    assert result.template == "$summary"


def test_materialize_template_passes_through_existing_template() -> None:
    """Assert ``materialize_template`` returns an already-``Template`` value unchanged."""
    tmpl = Template("$version")
    result = materialize_template(_ctx(ExtensionsSettings, "FOOTER_TEMPLATE", tmpl))
    assert result is tmpl


def test_materialize_template_rejects_non_string() -> None:
    """Reject a non-string, non-``Template`` override instead of passing it through."""
    with pytest.raises(ValueError, match="must be a string"):
        materialize_template(_ctx(ExtensionsSettings, "FOOTER_TEMPLATE", 1))


def _purpose_probe(seen: list[MaterializerPurpose]) -> type[BaseModel]:
    """Build a probe class whose materializer records the purpose it ran for.

    :param seen: The list each invocation appends its purpose to.
    :return: A model class with one HOT, materializer-backed field.
    """

    def _record(ctx: MaterializerContext) -> object:
        """Record ``ctx.purpose`` and echo the raw value back unchanged."""
        seen.append(ctx.purpose)
        return ctx.raw

    class _Probe(BaseModel):
        """Represent a model whose HOT field records how it was materialized."""

        value: str = hot_field("", materializer=_record)

    return _Probe


class TestMaterializerPurpose:
    """Cover the channel telling a materializer a stored row from a new payload."""

    def test_a_context_defaults_to_validating_a_submitted_payload(self) -> None:
        """Default to the strict purpose, so an unaware caller keeps write semantics."""
        context = _ctx(ExtensionsSettings, "FOOTER_TEMPLATE", "$summary")

        assert context.purpose is MaterializerPurpose.VALIDATE

    def test_materialize_override_value_defaults_to_validating(self) -> None:
        """Hand the strict purpose down when the caller names none."""
        seen: list[MaterializerPurpose] = []
        probe = _purpose_probe(seen)

        materialize_override_value(probe, "value", probe.model_fields["value"], "x")

        assert seen == [MaterializerPurpose.VALIDATE]

    def test_materialize_override_value_forwards_an_explicit_purpose(self) -> None:
        """Let the snapshot builder announce that the value is a stored row."""
        seen: list[MaterializerPurpose] = []
        probe = _purpose_probe(seen)

        materialize_override_value(
            probe,
            "value",
            probe.model_fields["value"],
            "x",
            purpose=MaterializerPurpose.SNAPSHOT,
        )

        assert seen == [MaterializerPurpose.SNAPSHOT]


class _SecretLeafModel(BaseModel):
    """Nested model with a scalar SecretStr leaf (PMM-shaped)."""

    api_key: SecretStr
    label: str = "ok"


def test_providers_field_reports_is_secret() -> None:
    """Assert ``PROVIDERS`` is flagged secret via subclass ``SecretStr`` fields."""
    meta = next(m for m in iter_class_fields(AlertSettings) if m.key == "PROVIDERS")
    assert meta.is_secret is True


def test_unwrap_secrets_for_storage_scalar_secret() -> None:
    """Assert a SecretStr is persisted as plaintext, not the JSON mask."""
    assert (
        unwrap_secrets_for_storage(SecretStr("stored-top-secret"))
        == "stored-top-secret"
    )


def test_unwrap_secrets_for_storage_nested_model() -> None:
    """Assert SecretStr leaves inside a model dump to plaintext for JSON storage."""
    current = _SecretLeafModel(api_key=SecretStr("stored-nested-secret"), label="ok")
    assert unwrap_secrets_for_storage(current) == {
        "api_key": "stored-nested-secret",
        "label": "ok",
    }


def test_unwrap_secrets_for_storage_dict_of_secrets() -> None:
    """Assert secret-valued dict entries unwrap per key."""
    current = {
        "api_key": SecretStr("stored-dict-secret"),
        "token": SecretStr("keep-me"),
    }
    assert unwrap_secrets_for_storage(current) == {
        "api_key": "stored-dict-secret",
        "token": "keep-me",
    }


def test_unwrap_secrets_for_storage_passes_through_non_secrets() -> None:
    """Assert non-secret scalars and mappings are unchanged."""
    assert unwrap_secrets_for_storage("plain") == "plain"
    assert unwrap_secrets_for_storage({"a": 1}) == {"a": 1}


def test_is_hot_reloadable_true_for_marked_field() -> None:
    """Assert a field marked HOT via ``field_with_metadata`` is detected."""
    assert is_hot_reloadable(ExtensionsSettings, "CONNECTIVITY_CHECK_DEFAULT") is True


def test_is_hot_reloadable_true_for_promoted_endpoint() -> None:
    """Assert ``INVENTORY_ENDPOINT`` is promoted to HOT for live endpoint rebind."""
    assert is_hot_reloadable(ExtensionsSettings, "INVENTORY_ENDPOINT") is True


def test_is_hot_reloadable_false_for_structural_field() -> None:
    """Assert structural fields are never overridable."""
    assert is_hot_reloadable(ExtensionsSettings, "APPS") is False


def test_is_hot_reloadable_false_for_missing_field() -> None:
    """Assert an unknown field returns False instead of raising."""
    assert is_hot_reloadable(ExtensionsSettings, "DOES_NOT_EXIST") is False


def test_hot_field_names_extensions_settings() -> None:
    """Check that ``ExtensionsSettings`` ships the promoted HOT fields and toggles.

    Includes the endpoint and footer promotions and the ambient-SSO toggle.
    """
    assert hot_field_names(ExtensionsSettings) == frozenset(
        {
            "CONNECTIVITY_CHECK_DEFAULT",
            "AMBIENT_SESSION_SSO_ENABLED",
            "ARTIFACT_DOWNLOAD_TTL",
            "SYNC_REFRESH_TIME",
            "INVENTORY_ENDPOINT",
            "TASKS_ENDPOINT",
            "FOOTER_TEMPLATE",
            "DIAGNOSTICS_DELIVERY_INPUTS",
        }
    )


def test_hot_field_names_tasks_settings() -> None:
    """Assert ``TasksSettings`` HOT fields exclude ``NOMAD`` but include ``SYNC_LOCK_TTL``."""
    assert hot_field_names(TasksSettings) == frozenset(
        {
            "LOG_STREAM_CAP_BYTES",
            "LOG_STREAM_EVICTION_MAX_ROWS",
            "PRE_EXECUTION_CONNECTIVITY_CHECK",
            "STALENESS_THRESHOLD_SECONDS",
            "PENDING_ALLOCATION_TIMEOUT_SECONDS",
            "SYNC_LOCK_TTL",
            "LOG_RETENTION_DAYS",
            "LOG_PURGE_BATCH_SIZE",
        }
    )


def test_nested_overridable_field_names_extensions_settings() -> None:
    """Assert ``ExtensionsSettings`` exposes the refresh-session parent plus ``APP_DRAIN``."""
    assert nested_overridable_field_names(ExtensionsSettings) == frozenset(
        {"SESSION_REFRESH", "APP_DRAIN"}
    )


def test_nested_overridable_field_names_tasks_settings() -> None:
    """Assert ``NOMAD`` and ``SECURITY_HEADERS`` are NESTED_ONLY parents on ``TasksSettings``."""
    assert nested_overridable_field_names(TasksSettings) == frozenset(
        {"NOMAD", "SECURITY_HEADERS"}
    )


def test_hot_field_names_snippets_settings() -> None:
    """Expose the HOT preview, sync, and auto-approval fields on ``SnippetsSettings``."""
    assert hot_field_names(SnippetsSettings) == frozenset(
        {
            "AUTO_APPROVE_BUILTIN_SNIPPETS",
            "ENABLE_MANUAL_SYNC",
            "PREVIEW_MAX_CHARS",
            "PREVIEW_MAX_LINES",
            "SYNC_INTERVAL",
            "SNIPPETS_BASE_URL",
            "SYNC_FILTER",
        }
    )


def test_hot_field_names_inventory_settings_empty() -> None:
    """Assert ``InventorySettings`` has no HOT fields in this iteration."""
    assert hot_field_names(InventorySettings) == frozenset()


def test_field_without_metadata_returns_false() -> None:
    """Assert a field without any ``CustomFieldMetadata`` returns False without raising."""

    class _Plain(BaseModel):
        value: int = 1

    assert is_hot_reloadable(_Plain, "value") is False


def test_metadata_with_other_keys_does_not_trigger_hot() -> None:
    """Assert custom metadata other than ``reload`` does not flip the classification."""

    class _Other(BaseModel):
        value: int = field_with_metadata(1, metadata={"unrelated": "yes"})

    assert is_hot_reloadable(_Other, "value") is False


def test_reload_classification_values() -> None:
    """Assert ``ReloadClassification`` exposes ``HOT`` and ``NOT_OVERRIDABLE`` values."""
    assert ReloadClassification.HOT.value == "hot"
    assert ReloadClassification.NOT_OVERRIDABLE.value == "not_overridable"


@pytest.mark.parametrize(
    "field_name",
    [
        "SESSION_REFRESH",
        "INVENTORY_ENDPOINT",
        "TASKS_ENDPOINT",
        "FOOTER_TEMPLATE",
        "DIAGNOSTICS_DELIVERY_INPUTS",
    ],
)
def test_extensions_settings_marked_advanced(field_name: str) -> None:
    """Assert the promoted PMM Extensions settings carry the advanced flag."""
    assert is_advanced_field(ExtensionsSettings.model_fields[field_name]) is True


@pytest.mark.parametrize("field_name", ["SYNC_REFRESH_TIME", "APPS", "DATABASE"])
def test_extensions_settings_not_marked_advanced(field_name: str) -> None:
    """Assert PMM Extensions settings left basic do not carry the advanced flag (no over-marking)."""
    assert is_advanced_field(ExtensionsSettings.model_fields[field_name]) is False


def test_security_headers_marked_advanced() -> None:
    """Assert ``Tasks.SECURITY_HEADERS`` is the only Tasks setting promoted to advanced."""
    assert is_advanced_field(TasksSettings.model_fields["SECURITY_HEADERS"]) is True


@pytest.mark.parametrize("field_name", ["NOMAD", "PRE_EXECUTION_CONNECTIVITY_CHECK"])
def test_tasks_settings_not_marked_advanced(field_name: str) -> None:
    """Assert the ``NOMAD`` parent and other unpromoted Tasks settings stay basic.

    Only the NOMAD *leaves* are advanced, never the parent itself.
    """
    assert is_advanced_field(TasksSettings.model_fields[field_name]) is False


@pytest.mark.parametrize(
    "field_name",
    [
        "SYNC_LOCK_TTL",
        "STALENESS_THRESHOLD_SECONDS",
        "PENDING_ALLOCATION_TIMEOUT_SECONDS",
        "LOG_RETENTION_DAYS",
        "LOG_PURGE_BATCH_SIZE",
        "LOG_STREAM_CAP_BYTES",
        "LOG_STREAM_EVICTION_MAX_ROWS",
    ],
)
def test_tasks_settings_marked_advanced(field_name: str) -> None:
    """Promote the log-retention cluster and lock/staleness TTLs to advanced."""
    assert is_advanced_field(TasksSettings.model_fields[field_name]) is True


def test_session_leaf_inherits_advanced() -> None:
    """Assert every ``SESSION_REFRESH`` leaf inherits the parent's advanced flag."""
    leaf = resolve_nested_field_metadata(
        ExtensionsSettings, "SESSION_REFRESH__COOKIE_NAME"
    )
    assert leaf is not None
    assert leaf.is_advanced is True


def test_security_headers_deep_leaf_inherits_advanced() -> None:
    """Assert a two-level ``SECURITY_HEADERS`` leaf inherits advanced from the top parent.

    Only ``SECURITY_HEADERS`` is marked; ``strict_transport_security`` and
    ``max_age`` are not, so the chain walk is what propagates the flag down.
    """
    leaf = resolve_nested_field_metadata(
        TasksSettings, "SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"
    )
    assert leaf is not None
    assert leaf.is_advanced is True
    assert chain_has_advanced(
        TasksSettings, "SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"
    )


def test_non_advanced_nested_leaf_stays_false() -> None:
    """Assert a leaf under a non-advanced parent reports ``is_advanced=False``."""
    leaf = resolve_nested_field_metadata(ExtensionsSettings, "DATABASE__NAME")
    assert leaf is not None
    assert leaf.is_advanced is False


def test_advanced_does_not_change_reload_classification() -> None:
    """Assert marking a HOT field advanced leaves its reload classification untouched.

    ``advanced`` is display-only: a marked-advanced HOT endpoint stays HOT (and
    thus still patchable), proving the flag does not gate override eligibility.
    """
    assert (
        is_advanced_field(ExtensionsSettings.model_fields["INVENTORY_ENDPOINT"]) is True
    )
    assert is_hot_reloadable(ExtensionsSettings, "INVENTORY_ENDPOINT") is True


def test_is_advanced_field_false_without_metadata() -> None:
    """Assert a field without any ``CustomFieldMetadata`` reads ``False`` without raising."""

    class _Plain(BaseModel):
        value: int = 1

    assert is_advanced_field(_Plain.model_fields["value"]) is False


class _OverlayProbe(BaseModel):
    """Define a probe carrying an ``INHERITED_MARKERS`` overlay over otherwise-bare fields.

    ``inherited_leaf`` is a plain (unmarked) field promoted purely by the
    overlay, mirroring the inherited-field use case. ``plain_leaf`` has no
    overlay entry and must classify exactly as it would without the mechanism.
    ``conflicting`` carries an explicit ``NOT_OVERRIDABLE`` marker that the
    overlay must not override. ``ghost_field`` is an overlay entry naming a field
    that does not exist on the model, to prove such entries are safely ignored.
    """

    INHERITED_MARKERS: ClassVar[dict[str, dict[str, object]]] = {
        "inherited_leaf": {"reload": ReloadClassification.HOT, "advanced": True},
        "conflicting": {"reload": ReloadClassification.HOT, "advanced": True},
        "ghost_field": {"advanced": True},
    }
    inherited_leaf: int = 1
    plain_leaf: int = 2
    conflicting: int = field_with_metadata(
        3, metadata={"reload": ReloadClassification.NOT_OVERRIDABLE}
    )


def test_overlay_promotes_bare_inherited_field() -> None:
    """Assert an overlay entry marks a bare field ``advanced`` + HOT via owner context."""
    info = _OverlayProbe.model_fields["inherited_leaf"]
    assert is_advanced_field(info, owner_cls=_OverlayProbe, field_name="inherited_leaf")
    assert (
        field_reload_classification(
            info, owner_cls=_OverlayProbe, field_name="inherited_leaf"
        )
        is ReloadClassification.HOT
    )
    assert is_hot_reloadable(_OverlayProbe, "inherited_leaf") is True


def test_overlay_bare_call_is_unchanged_fast_path() -> None:
    """Assert calling classifiers with only a bare ``FieldInfo`` ignores the overlay."""
    info = _OverlayProbe.model_fields["inherited_leaf"]
    assert is_advanced_field(info) is False
    assert field_reload_classification(info) is ReloadClassification.NOT_OVERRIDABLE


def test_overlay_is_opt_in_per_field() -> None:
    """Assert a field with no overlay entry classifies exactly as today."""
    info = _OverlayProbe.model_fields["plain_leaf"]
    assert (
        is_advanced_field(info, owner_cls=_OverlayProbe, field_name="plain_leaf")
        is False
    )
    assert (
        field_reload_classification(
            info, owner_cls=_OverlayProbe, field_name="plain_leaf"
        )
        is ReloadClassification.NOT_OVERRIDABLE
    )


def test_overlay_does_not_override_explicit_field_metadata() -> None:
    """Assert the field's own marker wins; the overlay only fills absent keys.

    ``conflicting`` explicitly declares ``NOT_OVERRIDABLE`` -- the overlay's
    ``HOT`` must not win -- while ``advanced`` (absent on the field) is filled.
    """
    info = _OverlayProbe.model_fields["conflicting"]
    assert (
        field_reload_classification(
            info, owner_cls=_OverlayProbe, field_name="conflicting"
        )
        is ReloadClassification.NOT_OVERRIDABLE
    )
    assert is_explicit_not_overridable(
        info, owner_cls=_OverlayProbe, field_name="conflicting"
    )
    assert is_advanced_field(info, owner_cls=_OverlayProbe, field_name="conflicting")


def test_overlay_entry_for_absent_field_is_ignored() -> None:
    """Assert an overlay entry naming a field absent from the model is harmless.

    ``ghost_field`` has an overlay entry but no matching model field, so no
    classifier ever resolves it and its presence must not affect the real fields.
    """
    assert "ghost_field" not in _OverlayProbe.model_fields
    assert (
        is_advanced_field(
            _OverlayProbe.model_fields["plain_leaf"],
            owner_cls=_OverlayProbe,
            field_name="plain_leaf",
        )
        is False
    )


class _OverlayMaterializerProbe(BaseModel):
    """Define a probe whose overlay supplies a materializer for an otherwise-bare field.

    ``inherited_leaf`` declares no materializer of its own; the overlay attaches
    one. ``own`` declares its own materializer that the overlay must not shadow.
    """

    INHERITED_MARKERS: ClassVar[dict[str, dict[str, object]]] = {
        "inherited_leaf": {
            "reload": ReloadClassification.HOT,
            "materializer": materialize_template,
        },
    }
    inherited_leaf: str = ""
    own: str = hot_field("", materializer=materialize_template)


def test_overlay_supplies_materializer_for_bare_field() -> None:
    """Assert ``field_materializer`` honors an overlay-supplied materializer."""
    assert (
        field_materializer(_OverlayMaterializerProbe, "inherited_leaf")
        is materialize_template
    )


def test_overlay_materializer_does_not_shadow_own_field() -> None:
    """Assert a field's own materializer wins over an (absent) overlay entry."""
    assert field_materializer(_OverlayMaterializerProbe, "own") is materialize_template


@pytest.mark.parametrize(
    "overlay",
    [None, {}, "not-a-dict", {"value": "not-a-dict-entry"}],
)
def test_overlay_malformed_or_absent_is_harmless(overlay: object) -> None:
    """Assert an absent, empty, or malformed overlay never affects classification or raises."""

    class _Probe(BaseModel):
        value: int = 1

    if overlay is not None:
        _Probe.INHERITED_MARKERS = overlay  # type: ignore[attr-defined]

    info = _Probe.model_fields["value"]
    assert is_advanced_field(info, owner_cls=_Probe, field_name="value") is False
    assert (
        field_reload_classification(info, owner_cls=_Probe, field_name="value")
        is ReloadClassification.NOT_OVERRIDABLE
    )


class TestUnwrapSecretsForStorage:
    """Cover the storage shapes the JSON override column has to accept."""

    def test_secret_bytes_are_decoded_for_json_storage(self) -> None:
        """Decode ``SecretBytes`` so the JSON column can hold the plaintext."""
        assert unwrap_secrets_for_storage(SecretBytes(b"raw-bytes")) == "raw-bytes"

    def test_collection_members_are_unwrapped_elementwise(self) -> None:
        """Unwrap every secret inside a list or tuple, keeping plain members."""
        assert unwrap_secrets_for_storage(
            [SecretStr("first"), "plain", (SecretBytes(b"second"),)]
        ) == ["first", "plain", ["second"]]


class _ComputedFixtureSettings(BaseYamlSettings):
    """Expose an excluded input beside a computed field, as ``Settings`` does."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["COMPUTED_FIXTURE"]

    PLAIN: str = "visible"
    HIDDEN_INPUT: SecretStr | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def derived(self) -> SecretStr:
        """Return the resolved value callers read.

        The body below the summary line exists to pin that only the summary
        reaches the API description.

        :return: The resolved value.
        """
        return self.HIDDEN_INPUT or SecretStr("derived")


class TestIterClassFieldsKeySet:
    """Pin the key set to the one ``model_dump()`` produces."""

    def test_excluded_field_is_not_enumerated(self) -> None:
        """Omit a field declared ``exclude=True``; it is not public surface."""
        keys = {meta.key for meta in iter_class_fields(_ComputedFixtureSettings)}
        assert "HIDDEN_INPUT" not in keys
        assert "PLAIN" in keys

    def test_computed_field_is_enumerated(self) -> None:
        """Surface a computed field alongside the declared ones."""
        metas = {meta.key: meta for meta in iter_class_fields(_ComputedFixtureSettings)}
        assert metas["derived"].annotation is SecretStr
        assert metas["derived"].is_secret is True
        assert metas["derived"].is_complex is False

    def test_computed_field_is_not_overridable(self) -> None:
        """Classify a computed field NOT_OVERRIDABLE; it carries no reload marker."""
        meta = next(
            m for m in iter_class_fields(_ComputedFixtureSettings) if m.key == "derived"
        )
        assert meta.reload is ReloadClassification.NOT_OVERRIDABLE

    def test_computed_field_description_is_the_summary_line(self) -> None:
        """Trim the docstring Pydantic backfills down to its summary line."""
        meta = next(
            m for m in iter_class_fields(_ComputedFixtureSettings) if m.key == "derived"
        )
        assert meta.description == "Return the resolved value callers read."

    def test_computed_field_info_is_none_for_a_declared_field(self) -> None:
        """Answer ``None`` for a key ``model_fields`` already owns."""
        assert computed_field_info(_ComputedFixtureSettings, "PLAIN") is None
        assert computed_field_info(_ComputedFixtureSettings, "derived") is not None


class TestSettingsInternalTokenKeys:
    """Pin the ``Settings`` keys the admin settings API advertises."""

    def test_resolved_token_replaces_the_settable_input(self) -> None:
        """Assert the computed ``EXTENSIONS_INTERNAL_TOKEN`` is listed, never its excluded input."""
        keys = {meta.key for meta in iter_class_fields(Settings)}
        assert "EXTENSIONS_INTERNAL_TOKEN" in keys
        assert "EXTENSIONS_INTERNAL_TOKEN_INPUT" not in keys

    def test_internal_token_stays_secret_and_not_overridable(self) -> None:
        """Keep the token masked and closed to overrides through the computed field."""
        meta = next(
            m
            for m in iter_class_fields(Settings)
            if m.key == "EXTENSIONS_INTERNAL_TOKEN"
        )
        assert meta.is_secret is True
        assert meta.reload is ReloadClassification.NOT_OVERRIDABLE

    def test_internal_token_description_is_operator_facing(self) -> None:
        """Describe the token for an operator rather than echoing its docstring.

        Docstrings here open in imperative mood, which reads as an instruction
        once the settings page renders it, so the computed field declares its
        own description instead of inheriting the summary line.
        """
        meta = next(
            m
            for m in iter_class_fields(Settings)
            if m.key == "EXTENSIONS_INTERNAL_TOKEN"
        )
        assert meta.description == (
            "The internal service-to-service token. Derived from SECRET_KEY "
            "when no explicit value is configured."
        )

    def test_base_dir_is_advertised(self) -> None:
        """Assert ``BASE_DIR`` is listed: matching ``model_dump()`` adds every computed key."""
        keys = {meta.key for meta in iter_class_fields(Settings)}
        assert "BASE_DIR" in keys
