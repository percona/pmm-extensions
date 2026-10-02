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

"""Unit tests for the field-introspection and override-provenance helpers in ``registry.py`` and the response builder ``_settings_response_from_field`` in ``api.routes``."""

from datetime import datetime, timedelta
from typing import Annotated, ClassVar

import pytest
from annotated_types import Gt, Le
from pydantic import (
    BaseModel,
    Field,
    PositiveInt,
    SecretStr,
    Strict,
    TypeAdapter,
    ValidationError,
)

from app.core.celery.config import CeleryOptions
from app.core.config import BaseYamlSettings, PMMSettings, Settings
from app.core.settings_override.api.routes import (
    _remote_wiring,
    _settings_response_from_field,
)
from app.core.settings_override.constants import TASKS_SETTINGS
from app.core.settings_override.models import (
    setting_class_token,
    SettingOverride,
)
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    annotated_type,
    annotation_contains_credential_url,
    annotation_is_credential_url,
    chain_has_advanced,
    coerce_field_value,
    dump_field_value,
    hot_field,
    is_advanced_field,
    is_credential_url_field,
    iter_class_fields,
    iter_nested_leaf_keys,
    nested_overridable_field,
    ReloadClassification,
)
from app.core.settings_override.resolution import (
    override_provenance_for_rows,
    resolve_nested_field_metadata,
    SettingProvenance,
)
from app.core.utils.date_time import utc_now
from app.core.utils.fields import CredentialHttpUrl, StrHttpUrl
from app.extensions.config import DeliveryPlanInputs, ExtensionsSettings
from app.tasks.config import TasksSettings
from app.tasks.execution.executors.nomad.models import NomadExecutor


class _NestedWithSecret(BaseModel):
    """Submodel exposing a top-level ``SecretStr`` for nesting tests."""

    name: str
    token: SecretStr


class _FixtureSettings(BaseYamlSettings):
    """Synthetic settings class exercising every helper branch."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["FIXTURE"]

    HOT_INT: PositiveInt = hot_field(10)
    HOT_STRICT_INT: Annotated[int, Strict(), Gt(0), Le(365)] = hot_field(90)
    HOT_BOOL: bool = hot_field(default=True)
    COLD_STRING: str = "default"
    COLD_TIMEDELTA: timedelta = timedelta(minutes=5)
    COLD_NESTED: _NestedWithSecret = _NestedWithSecret(
        name="alice", token=SecretStr("s3cr3t")
    )
    COLD_TOP_SECRET: SecretStr = SecretStr("top-secret")
    COLD_OPTIONAL: int | None = None


def test_coerce_field_value_preserves_positive_int_constraint() -> None:
    """A ``PositiveInt`` field rejects ``-1`` via the preserved ``Gt(0)`` metadata."""
    field = _FixtureSettings.model_fields["HOT_INT"]
    with pytest.raises(ValidationError):
        coerce_field_value(field, -1)


def test_coerce_field_value_accepts_valid_int() -> None:
    """A ``PositiveInt`` field accepts a valid positive integer."""
    valid_int = 42
    field = _FixtureSettings.model_fields["HOT_INT"]
    assert coerce_field_value(field, valid_int) == valid_int


def test_coerce_field_value_rejects_wrong_type() -> None:
    """A ``bool`` field rejects a string that is not bool-parseable."""
    field = _FixtureSettings.model_fields["HOT_BOOL"]
    with pytest.raises(ValidationError):
        coerce_field_value(field, "not-a-bool")


_STRICT_INT_LOWER_BOUND = 1
_STRICT_INT_UPPER_BOUND = 365
_STRICT_INT_DEFAULT = 90


@pytest.mark.parametrize(
    "bad_value",
    [True, 1.5, 0, _STRICT_INT_UPPER_BOUND + 1],
)
def test_coerce_field_value_strict_int_rejects(bad_value: object) -> None:
    """A ``Strict()``-int field rejects bool/float and out-of-range integers.

    ``Strict()`` blocks the lax ``bool``/``float -> int`` coercion that a plain
    ``int`` annotation would silently accept; ``Gt(0)``/``Le(365)`` are preserved
    through ``annotated_type`` reassembly so the bounds still reject 0 and 366.
    """
    field = _FixtureSettings.model_fields["HOT_STRICT_INT"]
    with pytest.raises(ValidationError):
        coerce_field_value(field, bad_value)


@pytest.mark.parametrize(
    "valid_value",
    [_STRICT_INT_LOWER_BOUND, _STRICT_INT_DEFAULT, _STRICT_INT_UPPER_BOUND],
)
def test_coerce_field_value_strict_int_accepts_in_range(valid_value: int) -> None:
    """A ``Strict()``-int field accepts genuine integers within the bounds."""
    field = _FixtureSettings.model_fields["HOT_STRICT_INT"]
    assert coerce_field_value(field, valid_value) == valid_value


def test_iter_class_fields_yields_every_field() -> None:
    """Every declared field on the synthetic class is surfaced."""
    fields = {meta.key for meta in iter_class_fields(_FixtureSettings)}
    expected = {
        "FASTAPI_ENV",
        "HOT_INT",
        "HOT_BOOL",
        "COLD_STRING",
        "COLD_TIMEDELTA",
        "COLD_NESTED",
        "COLD_TOP_SECRET",
        "COLD_OPTIONAL",
    }
    assert expected.issubset(fields)


def test_iter_class_fields_classifies_hot_and_cold() -> None:
    """HOT fields are marked HOT; everything else is NOT_OVERRIDABLE."""
    classification = {
        meta.key: meta.reload for meta in iter_class_fields(_FixtureSettings)
    }
    assert classification["HOT_INT"] is ReloadClassification.HOT
    assert classification["HOT_BOOL"] is ReloadClassification.HOT
    assert classification["COLD_STRING"] is ReloadClassification.NOT_OVERRIDABLE
    assert classification["COLD_NESTED"] is ReloadClassification.NOT_OVERRIDABLE


def test_iter_class_fields_marks_top_level_secret() -> None:
    """A top-level ``SecretStr`` field is flagged ``is_secret=True``."""
    metas = {meta.key: meta for meta in iter_class_fields(_FixtureSettings)}
    assert metas["COLD_TOP_SECRET"].is_secret is True


def test_iter_class_fields_marks_nested_secret() -> None:
    """A nested ``SecretStr`` inside a submodel is flagged ``is_secret=True``."""
    metas = {meta.key: meta for meta in iter_class_fields(_FixtureSettings)}
    assert metas["COLD_NESTED"].is_secret is True


def test_iter_class_fields_marks_complex_submodel() -> None:
    """A field whose annotation is a Pydantic ``BaseModel`` is flagged ``is_complex``."""
    metas = {meta.key: meta for meta in iter_class_fields(_FixtureSettings)}
    assert metas["COLD_NESTED"].is_complex is True
    assert metas["HOT_INT"].is_complex is False


def test_dump_field_value_serialises_timedelta_to_iso8601() -> None:
    """A ``timedelta`` value is dumped to its ISO 8601 duration string."""
    field = _FixtureSettings.model_fields["COLD_TIMEDELTA"]
    dumped = dump_field_value(field, timedelta(minutes=5))
    assert dumped == "PT5M"


def test_dump_field_value_redacts_secret_str() -> None:
    """A ``SecretStr`` value is redacted by Pydantic's JSON dump."""
    field = _FixtureSettings.model_fields["COLD_TOP_SECRET"]
    dumped = dump_field_value(field, SecretStr("hunter2"))
    assert dumped == "**********"


def test_dump_field_value_redacts_credential_http_url() -> None:
    """A credential-bearing URL is redacted by its field metadata serializer."""
    field = ExtensionsSettings.model_fields["INVENTORY_ENDPOINT"]
    url = TypeAdapter(CredentialHttpUrl).validate_python(
        "http://inv-user:inv-secret@inventory.internal:8080"
    )
    dumped = dump_field_value(field, url)
    assert "inv-secret" not in dumped
    assert "****" in dumped
    assert "inv-user" in dumped


def test_dump_field_value_redacts_a_computed_credential_url() -> None:
    """Redact the derived base URL of a client setting alongside its endpoint.

    The settings LIST, DETAIL, and export responses all serialize through this
    helper, and a computed field carries no annotation for the credential-URL
    detection to read — the redaction has to travel on the field's own return
    annotation to reach them.
    """
    field = TasksSettings.model_fields["NOMAD"]
    executor = NomadExecutor.model_validate(
        {"endpoint": "http://nomad-user:nomad-secret@nomad.internal:4646"}
    )
    dumped = dump_field_value(field, executor)
    assert "nomad-secret" not in str(dumped)
    assert dumped["base_url"] == "http://nomad-user:****@nomad.internal:4646"
    assert dumped["endpoint"] == "http://nomad-user:****@nomad.internal:4646/"


def test_is_credential_url_field_recognises_all_aliases() -> None:
    """Detect every credential-URL annotated type by serializer identity.

    The shared mask-rejecting validator adds metadata beside the serializer; this
    pins that detection still keys off serializer-function identity alone.
    ``NomadExecutor.endpoint`` is the inherited non-``Optional`` case, whose
    ``Annotated`` Pydantic hoists onto ``FieldInfo`` — detection has to resolve
    it through :func:`annotated_type` rather than reading ``.annotation``.
    """
    for field in (
        ExtensionsSettings.model_fields["INVENTORY_ENDPOINT"],
        PMMSettings.model_fields["endpoint"],
        CeleryOptions.model_fields["broker_url"],
        NomadExecutor.model_fields["endpoint"],
    ):
        assert is_credential_url_field(field)


class TestCredentialUrlPredicatePair:
    """Cover the subtree/position split between the two credential-URL predicates.

    ``annotation_contains_credential_url`` answers "does this subtree reach
    one", the question the read-surface redaction asks; ``annotation_is_credential_url``
    answers "is the value at this position one", which is what the at-rest leaf
    transform needs. A model-typed parent separates them.
    """

    @pytest.mark.parametrize(
        ("settings_cls", "field_name"),
        [
            (ExtensionsSettings, "INVENTORY_ENDPOINT"),
            (ExtensionsSettings, "TASKS_ENDPOINT"),
            (PMMSettings, "endpoint"),
            (NomadExecutor, "endpoint"),
            (DeliveryPlanInputs, "endpoint"),
        ],
    )
    def test_a_live_leaf_answers_both_predicates(
        self, settings_cls: type[BaseModel], field_name: str
    ) -> None:
        """Report a scalar credential-URL leaf as both reachable and positional."""
        annotation = annotated_type(settings_cls.model_fields[field_name])

        assert annotation_is_credential_url(annotation)
        assert annotation_contains_credential_url(annotation)

    @pytest.mark.parametrize(
        ("settings_cls", "field_name"),
        [
            (Settings, "PMM"),
            (Settings, "CELERY"),
            (ExtensionsSettings, "DIAGNOSTICS_DELIVERY_INPUTS"),
        ],
    )
    def test_a_model_typed_parent_reaches_one_without_being_one(
        self, settings_cls: type[BaseModel], field_name: str
    ) -> None:
        """Separate a parent whose *child* is the credential URL from the child itself.

        A leaf transform that used the subtree predicate here would try to
        rewrite the whole stored object as if it were a URL string.
        """
        annotation = annotated_type(settings_cls.model_fields[field_name])

        assert annotation_contains_credential_url(annotation)
        assert not annotation_is_credential_url(annotation)

    def test_neither_predicate_fires_on_a_plain_url(self) -> None:
        """Report a URL type carrying no credential-URL marker as neither."""
        assert not annotation_contains_credential_url(StrHttpUrl)
        assert not annotation_is_credential_url(StrHttpUrl)


class TestCredentialUrlFieldsAreNotSecretBearing:
    """Pin that the at-rest change never leaks into the API's ``is_secret`` flag.

    ``is_secret`` is published on every settings LIST/DETAIL response. Widening
    ``annotation_contains_secret`` to cover credential URLs would flip these four
    to ``True`` while encrypting nothing, because the leaf gate is the Pydantic
    secret type and neither ``HttpUrl`` nor ``str`` subclasses it.
    """

    @pytest.mark.parametrize(
        ("settings_cls", "key"),
        [
            (ExtensionsSettings, "INVENTORY_ENDPOINT"),
            (ExtensionsSettings, "TASKS_ENDPOINT"),
        ],
    )
    def test_a_top_level_credential_url_field_is_not_secret(
        self, settings_cls: type[BaseYamlSettings], key: str
    ) -> None:
        """Report ``is_secret`` as ``False`` for a top-level credential-URL field."""
        metadata = next(
            entry for entry in iter_class_fields(settings_cls) if entry.key == key
        )

        assert metadata.is_secret is False

    @pytest.mark.parametrize(
        ("settings_cls", "key"),
        [
            (Settings, "PMM__endpoint"),
            (TasksSettings, "NOMAD__endpoint"),
        ],
    )
    def test_a_nested_credential_url_leaf_is_not_secret(
        self, settings_cls: type[BaseYamlSettings], key: str
    ) -> None:
        """Report ``is_secret`` as ``False`` for a nested credential-URL leaf."""
        metadata = resolve_nested_field_metadata(settings_cls, key)

        assert metadata is not None
        assert metadata.is_secret is False


def test_dump_field_value_redacts_nested_secret() -> None:
    """A nested ``SecretStr`` inside a submodel is redacted in the dumped dict."""
    field = _FixtureSettings.model_fields["COLD_NESTED"]
    dumped = dump_field_value(
        field, _NestedWithSecret(name="bob", token=SecretStr("hunter2"))
    )
    assert dumped == {"name": "bob", "token": "**********"}


class _NoDefault(BaseYamlSettings):
    """Declare a required HOT field carrying no default."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["NODEF"]
    BARE: int = hot_field(...)


def test_dump_field_value_returns_none_for_undefined_default() -> None:
    """A field with no declared default dumps to ``None`` instead of raising."""
    field = _NoDefault.model_fields["BARE"]
    assert dump_field_value(field, field.default) is None


class _WithFactory(BaseYamlSettings):
    """Synthetic settings class with a field declared via ``default_factory``."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["FACTORY"]
    ITEMS: list[str] = Field(default_factory=lambda: ["alpha", "beta"])


def test_iter_class_fields_invokes_default_factory() -> None:
    """``default_factory`` is invoked so the metadata holds the actual default value."""
    metas = {meta.key: meta for meta in iter_class_fields(_WithFactory)}
    assert metas["ITEMS"].default == ["alpha", "beta"]


class _Unschemable:
    """A non-Pydantic type with no overridden ``__str__`` (default object repr)."""


class _WithUnschemableAnnotation(BaseYamlSettings):
    """Synthetic settings class whose field annotation Pydantic can't schemafy."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["UNSCHEMABLE"]
    model_config = {"arbitrary_types_allowed": True}
    OPAQUE: _Unschemable = _Unschemable()


def test_dump_field_value_returns_none_for_unschemable_annotation() -> None:
    """Unschemable annotations dump to ``None`` rather than an unstable object repr."""
    field = _WithUnschemableAnnotation.model_fields["OPAQUE"]
    assert dump_field_value(field, _Unschemable()) is None


class _SecretLeafModel(BaseModel):
    """Submodel carrying a secret leaf under a nested-overridable parent."""

    TOKEN: SecretStr = SecretStr("s3cr3t")
    LABEL: str = "public"


class _SecretLeafParent(BaseModel):
    """Model declaring a nested-overridable parent over a secret submodel."""

    GROUP: _SecretLeafModel = nested_overridable_field(_SecretLeafModel())


def test_iter_nested_leaf_keys_enumerates_secret_leaf() -> None:
    """A nested-overridable parent enumerates its secret leaf alongside siblings."""
    leaves = dict(iter_nested_leaf_keys(_SecretLeafParent, "GROUP"))
    assert set(leaves) == {"GROUP__TOKEN", "GROUP__LABEL"}


def test_settings_response_redacts_secret_leaf_with_key_path() -> None:
    """A secret leaf response redacts the value and carries the canonical key_path."""
    proxy = OverridableSettingsProxy(
        _SecretLeafParent, setting_class=ExtensionsSettings.__name__
    )
    leaf_meta = resolve_nested_field_metadata(_SecretLeafParent, "GROUP__TOKEN")
    assert leaf_meta is not None
    response = _settings_response_from_field(
        setting_class=ExtensionsSettings.__name__,
        settings_cls=_SecretLeafParent,
        proxy=proxy,
        field_meta=leaf_meta,
        provenance=None,
    )
    assert response.value == "**********"
    assert response.is_secret is True
    assert response.key_path == ["GROUP", "TOKEN"]


def test_settings_response_serializes_missing_mapping_segment_as_null() -> None:
    """LIST projection maps a missing nested segment to JSON ``null``."""
    proxy = OverridableSettingsProxy(
        _SecretLeafParent, setting_class=ExtensionsSettings.__name__
    )
    proxy._set_snapshot({"GROUP": {"LABEL": "visible"}})
    leaf_meta = resolve_nested_field_metadata(_SecretLeafParent, "GROUP__TOKEN")
    assert leaf_meta is not None
    response = _settings_response_from_field(
        setting_class=ExtensionsSettings.__name__,
        settings_cls=_SecretLeafParent,
        proxy=proxy,
        field_meta=leaf_meta,
        provenance=None,
    )
    assert response.value is None
    assert response.is_secret is True


def test_settings_response_serializes_present_none_secret_leaf_as_null() -> None:
    """LIST projection renders an unresolved secret leaf as JSON ``null``."""
    proxy = OverridableSettingsProxy(
        _SecretLeafParent, setting_class=ExtensionsSettings.__name__
    )
    proxy._set_snapshot(
        {"GROUP": _SecretLeafModel.model_construct(TOKEN=None, LABEL="public")}
    )
    leaf_meta = resolve_nested_field_metadata(_SecretLeafParent, "GROUP__TOKEN")
    assert leaf_meta is not None
    response = _settings_response_from_field(
        setting_class=ExtensionsSettings.__name__,
        settings_cls=_SecretLeafParent,
        proxy=proxy,
        field_meta=leaf_meta,
        provenance=None,
    )
    assert response.value is None
    assert response.is_secret is True


def test_settings_response_applicable_defaults_true() -> None:
    """Mark a field response applicable when no applicability predicate is given."""
    proxy = OverridableSettingsProxy(
        _FixtureSettings, setting_class=ExtensionsSettings.__name__
    )
    meta = next(m for m in iter_class_fields(_FixtureSettings) if m.key == "HOT_BOOL")
    response = _settings_response_from_field(
        setting_class=ExtensionsSettings.__name__,
        settings_cls=_FixtureSettings,
        proxy=proxy,
        field_meta=meta,
        provenance=None,
    )
    assert response.is_applicable is True


def test_settings_response_honors_applicability_predicate() -> None:
    """Mark the field response not applicable when the predicate returns ``False``."""
    proxy = OverridableSettingsProxy(
        _FixtureSettings, setting_class=ExtensionsSettings.__name__
    )
    meta = next(m for m in iter_class_fields(_FixtureSettings) if m.key == "HOT_BOOL")
    response = _settings_response_from_field(
        setting_class=ExtensionsSettings.__name__,
        settings_cls=_FixtureSettings,
        proxy=proxy,
        field_meta=meta,
        provenance=None,
        applicability=lambda _cls, field: field.key != "HOT_BOOL",
    )
    assert response.is_applicable is False


class _DeepLeafModel(BaseModel):
    """Innermost submodel, two levels under a nested-overridable parent."""

    DEEP: int = 1


class _AdvancedLeafModel(BaseModel):
    """Submodel exposing both a deep submodel and a flat leaf."""

    INNER: _DeepLeafModel = _DeepLeafModel()
    FLAT: str = "flat"


class _AdvancedFixtureSettings(BaseYamlSettings):
    """Synthetic settings class exercising the ``advanced`` flag end-to-end."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["ADV"]

    PLAIN: int = hot_field(1)
    MARKED: int = hot_field(2, advanced=True)
    ADV_PARENT: _AdvancedLeafModel = nested_overridable_field(
        _AdvancedLeafModel(), advanced=True
    )
    BASIC_PARENT: _AdvancedLeafModel = nested_overridable_field(_AdvancedLeafModel())


def test_is_advanced_field_false_for_unmarked() -> None:
    """A field without an ``advanced`` marker reads ``False``."""
    field = _AdvancedFixtureSettings.model_fields["PLAIN"]
    assert is_advanced_field(field) is False


def test_is_advanced_field_true_for_marked() -> None:
    """A field declared ``advanced=True`` reads ``True``."""
    field = _AdvancedFixtureSettings.model_fields["MARKED"]
    assert is_advanced_field(field) is True


def test_iter_class_fields_unmarked_field_not_advanced() -> None:
    """An unmarked field surfaces ``is_advanced=False`` (backward-compatible default)."""
    metas = {meta.key: meta for meta in iter_class_fields(_AdvancedFixtureSettings)}
    assert metas["PLAIN"].is_advanced is False


def test_iter_class_fields_marks_top_level_advanced() -> None:
    """A top-level ``advanced=True`` field surfaces ``is_advanced=True``."""
    metas = {meta.key: meta for meta in iter_class_fields(_AdvancedFixtureSettings)}
    assert metas["MARKED"].is_advanced is True


def test_nested_leaf_inherits_advanced_from_parent() -> None:
    """A leaf two levels under an advanced parent inherits ``is_advanced=True``.

    Neither ``INNER`` nor ``DEEP`` is marked; only the top-level ``ADV_PARENT``
    carries the flag, so the chain walk is what propagates it to the leaf.
    """
    leaf_meta = resolve_nested_field_metadata(
        _AdvancedFixtureSettings, "ADV_PARENT__INNER__DEEP"
    )
    assert leaf_meta is not None
    assert leaf_meta.is_advanced is True


def test_nested_leaf_under_basic_parent_not_advanced() -> None:
    """A sibling leaf under an unmarked parent stays ``is_advanced=False``."""
    leaf_meta = resolve_nested_field_metadata(
        _AdvancedFixtureSettings, "BASIC_PARENT__FLAT"
    )
    assert leaf_meta is not None
    assert leaf_meta.is_advanced is False


def test_chain_has_advanced_true_for_advanced_ancestor() -> None:
    """``chain_has_advanced`` reports ``True`` when an ancestor is marked advanced."""
    assert (
        chain_has_advanced(_AdvancedFixtureSettings, "ADV_PARENT__INNER__DEEP") is True
    )


def test_chain_has_advanced_false_for_unmarked_chain() -> None:
    """``chain_has_advanced`` reports ``False`` when no segment is advanced."""
    assert chain_has_advanced(_AdvancedFixtureSettings, "BASIC_PARENT__FLAT") is False


def test_chain_has_advanced_false_for_unresolvable_key() -> None:
    """An unresolvable key reports ``False`` rather than raising."""
    assert chain_has_advanced(_AdvancedFixtureSettings, "NOPE__NOPE") is False


def test_remote_wiring_requires_dep_when_remote_classes_present() -> None:
    """Configuring ``remote_classes`` without ``remote_api_dep`` fails fast."""
    remote_classes = [(TASKS_SETTINGS, "/admin/settings")]
    with pytest.raises(ValueError, match="remote_api_dep is required"):
        _remote_wiring(remote_classes, None)


def test_remote_wiring_allows_no_dep_when_no_remote_classes() -> None:
    """An empty/absent ``remote_classes`` keeps the no-op dependency, no raise."""
    remote_lookup, remote_dep = _remote_wiring(None, None)
    assert remote_lookup == {}
    assert remote_dep is not None


class _OverlayFixtureSettings(BaseYamlSettings):
    """Define a settings class promoting a bare field via an ``INHERITED_MARKERS`` overlay."""

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["OVERLAYFIX"]
    INHERITED_MARKERS: ClassVar[dict[str, dict[str, object]]] = {
        "PROMOTED": {"reload": ReloadClassification.HOT, "advanced": True},
    }

    PROMOTED: int = 5
    PLAIN: int = hot_field(6)


def test_iter_class_fields_surfaces_overlay_advanced_and_reload() -> None:
    """Assert a bare field promoted by the overlay surfaces ``advanced`` + HOT via the API."""
    metas = {meta.key: meta for meta in iter_class_fields(_OverlayFixtureSettings)}
    assert metas["PROMOTED"].is_advanced is True
    assert metas["PROMOTED"].reload is ReloadClassification.HOT


def test_iter_class_fields_leaves_non_overlay_field_untouched() -> None:
    """Assert a field with no overlay entry is unaffected by the overlay mechanism."""
    metas = {meta.key: meta for meta in iter_class_fields(_OverlayFixtureSettings)}
    assert metas["PLAIN"].is_advanced is False


def test_overlay_promoted_field_bare_call_ignores_overlay() -> None:
    """Assert the bare ``FieldInfo`` fast path ignores the overlay (backward-compatible)."""
    field = _OverlayFixtureSettings.model_fields["PROMOTED"]
    assert is_advanced_field(field) is False


def _override_row(
    key: str,
    *,
    row_id: int,
    created_at: datetime,
    updated_at: datetime | None = None,
    updated_by: str | None = None,
) -> SettingOverride:
    """Build an unpersisted ``SettingOverride`` for the provenance aggregation.

    ``SettingOverride`` is a ``table=True`` SQLModel, so ``__init__`` skips
    validation and every timestamp is stored exactly as passed, which is what
    lets a naive ``updated_at`` be constructed here at all.

    :param key: The stored override key.
    :param row_id: The primary key, which the aggregation uses to break ties.
    :param created_at: The row's creation stamp.
    :param updated_at: The row's last-write stamp, or ``None`` for a legacy row.
    :param updated_by: The recorded actor, or ``None`` for a legacy row.
    :return: The constructed row.
    """
    return SettingOverride(
        id=row_id,
        setting_class=setting_class_token(ExtensionsSettings),
        key=key,
        value=1,
        is_active=True,
        created_at=created_at,
        updated_at=updated_at,
        updated_by=updated_by,
    )


def test_override_provenance_reports_the_rows_own_stamp() -> None:
    """Report a top-level row's ``updated_at`` and ``updated_by`` under its own key."""
    written_at = utc_now()
    rows = [
        _override_row(
            "SYNC_REFRESH_TIME",
            row_id=1,
            created_at=written_at - timedelta(days=1),
            updated_at=written_at,
            updated_by="alice",
        )
    ]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    assert provenance["SYNC_REFRESH_TIME"] == SettingProvenance(
        updated_at=written_at, updated_by="alice"
    )


def test_override_provenance_falls_back_to_created_at_for_a_legacy_row() -> None:
    """Fall back to ``created_at`` for a row written before explicit stamping."""
    created_at = utc_now()
    rows = [_override_row("SYNC_REFRESH_TIME", row_id=1, created_at=created_at)]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    assert provenance["SYNC_REFRESH_TIME"] == SettingProvenance(
        updated_at=created_at, updated_by=None
    )


def test_override_provenance_promotes_a_nested_leaf_to_its_parent() -> None:
    """Report a nested leaf's provenance under every canonical prefix of its chain."""
    written_at = utc_now()
    rows = [
        _override_row(
            "APP_DRAIN__stale_task_ttl",
            row_id=1,
            created_at=written_at,
            updated_at=written_at,
            updated_by="alice",
        )
    ]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    expected = SettingProvenance(updated_at=written_at, updated_by="alice")
    assert provenance["APP_DRAIN"] == expected
    assert provenance["APP_DRAIN__stale_task_ttl"] == expected


def test_override_provenance_breaks_an_equal_stamp_tie_on_the_higher_id() -> None:
    """Prefer the higher ``id`` when two contributing rows share one timestamp.

    One PATCH batch stamps every key it writes with a single ``utc_now()``, and
    that helper zeroes microseconds, so equal stamps are the common case rather
    than a corner.
    """
    written_at = utc_now()
    rows = [
        _override_row(
            "APP_DRAIN__stale_task_ttl",
            row_id=1,
            created_at=written_at,
            updated_at=written_at,
            updated_by="alice",
        ),
        _override_row(
            "APP_DRAIN__reconcile_interval",
            row_id=2,
            created_at=written_at,
            updated_at=written_at,
            updated_by="bob",
        ),
    ]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    assert provenance["APP_DRAIN"].updated_by == "bob"


def test_override_provenance_tie_break_follows_creation_not_write_order() -> None:
    """Pin the documented limit of the ``id`` tie-break at second granularity.

    ``id`` orders rows by creation, so when two separate writes land in the same
    second the winner is whichever contributing row was created later, even when
    the other row was the one written later. Second-granularity stamps make the
    real order unrecoverable, so this asserts the deterministic behaviour rather
    than an accurate one.
    """
    written_at = utc_now()
    later_written_but_created_first = _override_row(
        "APP_DRAIN__stale_task_ttl",
        row_id=1,
        created_at=written_at - timedelta(days=1),
        updated_at=written_at,
        updated_by="wrote-last",
    )
    earlier_written_but_created_last = _override_row(
        "APP_DRAIN__reconcile_interval",
        row_id=2,
        created_at=written_at,
        updated_at=written_at,
        updated_by="created-last",
    )

    provenance = override_provenance_for_rows(
        ExtensionsSettings,
        [later_written_but_created_first, earlier_written_but_created_last],
    )

    assert provenance["APP_DRAIN"].updated_by == "created-last"


def test_override_provenance_prefers_the_later_stamp_over_the_higher_id() -> None:
    """Prefer the later timestamp even when the lower ``id`` carries it."""
    written_at = utc_now()
    rows = [
        _override_row(
            "APP_DRAIN__stale_task_ttl",
            row_id=1,
            created_at=written_at,
            updated_at=written_at,
            updated_by="alice",
        ),
        _override_row(
            "APP_DRAIN__reconcile_interval",
            row_id=2,
            created_at=written_at,
            updated_at=written_at - timedelta(hours=1),
            updated_by="bob",
        ),
    ]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    assert provenance["APP_DRAIN"].updated_by == "alice"


def test_override_provenance_normalizes_a_naive_stamp() -> None:
    """Compare a naive ``updated_at`` against an aware ``created_at`` without raising.

    SQLModel skips validation on a ``table=True`` model, so a value loaded from
    the database bypasses the ``UTCDatetime`` normalizer and can reach the
    aggregation naive.
    """
    aware = utc_now()
    naive = aware.replace(tzinfo=None) - timedelta(hours=1)
    rows = [
        _override_row(
            "APP_DRAIN__stale_task_ttl",
            row_id=1,
            created_at=aware,
            updated_at=naive,
            updated_by="alice",
        ),
        _override_row(
            "APP_DRAIN__reconcile_interval",
            row_id=2,
            created_at=aware,
            updated_at=aware,
            updated_by="bob",
        ),
    ]

    provenance = override_provenance_for_rows(ExtensionsSettings, rows)

    assert provenance["APP_DRAIN"] == SettingProvenance(
        updated_at=aware, updated_by="bob"
    )


def test_override_provenance_is_empty_without_rows() -> None:
    """Return an empty mapping when the class has no active override rows."""
    assert override_provenance_for_rows(ExtensionsSettings, []) == {}
