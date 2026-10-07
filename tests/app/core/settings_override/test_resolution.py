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

"""Test how ``__``-delimited override keys resolve against nested models."""

from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import cast, ClassVar

import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.middleware.security_headers import SecurityHeadersOptions
from app.core.settings_override.constants import NESTED_VALUE_MISSING
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import (
    chain_has_advanced,
    chain_has_explicit_not_overridable,
    coerce_nested_field_value,
    is_hot_reloadable,
    is_nested_overridable_parent,
    iter_nested_leaf_keys,
    nested_overridable_field,
    nested_overridable_field_names,
    not_overridable_field,
    ReloadClassification,
    rendered_leaf_keys,
)
from app.core.settings_override.resolution import (
    canonical_override_key,
    override_provenance_for_rows,
    override_rows_for_key,
    resolve_field_in_model,
    resolve_nested_field,
    resolve_nested_field_metadata,
    resolve_nested_value,
)
from app.extensions.config import CookieOptions, ExtensionsSettings
from app.tasks.config import TasksSettings
from tests.app.core.settings_override.conftest import (
    EXTENSIONS_SETTINGS_TOKEN,
    insert_override_row,
    TASKS_SETTINGS_TOKEN,
)


class _Leaf(BaseModel):
    """Leaf model nested under an explicitly not-overridable intermediate."""

    VALUE: int = 1


class _Inner(BaseModel):
    """Inner model used to exercise nested classification helpers."""

    BARE: int = 1
    LOCKED: int = not_overridable_field(2)
    LOCKED_SUB: _Leaf = not_overridable_field(_Leaf())


class _Outer(BaseModel):
    """Outer model declaring a nested-overridable parent."""

    NESTED: _Inner = nested_overridable_field(_Inner())
    PLAIN: int = 5


def test_resolve_field_in_model_exact_match() -> None:
    """An exact attribute-name match returns the canonical name and field."""
    resolved = resolve_field_in_model(CookieOptions, "MAX_AGE")
    assert resolved is not None
    canonical, _ = resolved
    assert canonical == "MAX_AGE"


def test_resolve_field_in_model_uppercase_alias_match() -> None:
    """An uppercase alias resolves to the lowercase canonical attribute name.

    ``SecurityHeadersOptions`` is a ``BaseCaseInsensitiveModel`` whose
    attribute names are lowercase but whose aliases are uppercase.
    """
    resolved = resolve_field_in_model(SecurityHeadersOptions, "X_FRAME_OPTIONS_DENY")
    assert resolved is not None
    canonical, _ = resolved
    assert canonical == "x_frame_options_deny"


def test_resolve_field_in_model_lowercase_fallback() -> None:
    """A lowercase segment resolves to the lowercase canonical attribute name."""
    resolved = resolve_field_in_model(SecurityHeadersOptions, "x_frame_options_deny")
    assert resolved is not None
    canonical, _ = resolved
    assert canonical == "x_frame_options_deny"


def test_resolve_field_in_model_missing_segment() -> None:
    """An unknown segment returns ``None``."""
    assert resolve_field_in_model(CookieOptions, "NOPE") is None


@pytest.mark.parametrize("segment", ["External", "Incoming", "Outgoing"])
def test_resolve_field_in_model_alias_only_match(segment: str) -> None:
    """Resolve each alias kind independently of the canonical attribute name."""

    class _AliasedModel(BaseModel):
        value: int = Field(
            default=1,
            alias="external",
            validation_alias="incoming",
            serialization_alias="outgoing",
        )

    resolved = resolve_field_in_model(_AliasedModel, segment)

    assert resolved is not None
    canonical, field = resolved
    assert canonical == "value"
    assert field is _AliasedModel.model_fields["value"]


def test_resolve_nested_field_single_level() -> None:
    """A single-level key resolves to a one-segment chain and its leaf field."""
    resolved = resolve_nested_field(ExtensionsSettings, "SESSION_REFRESH__MAX_AGE")
    assert resolved is not None
    chain, _ = resolved
    assert chain == ("SESSION_REFRESH", "MAX_AGE")


def test_resolve_nested_field_multi_level() -> None:
    """A multi-level key descends through nested models to the leaf."""
    resolved = resolve_nested_field(
        TasksSettings, "SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE"
    )
    assert resolved is not None
    chain, _ = resolved
    assert chain == ("SECURITY_HEADERS", "strict_transport_security", "max_age")


def test_resolve_nested_field_unknown_top_level() -> None:
    """An unknown top-level segment resolves to ``None``."""
    assert resolve_nested_field(ExtensionsSettings, "BOGUS__X") is None


def test_resolve_nested_field_unknown_nested_leaf() -> None:
    """An unknown nested leaf resolves to ``None``."""
    assert resolve_nested_field(ExtensionsSettings, "SESSION_REFRESH__BOGUS") is None


def test_resolve_nested_field_non_pydantic_intermediate() -> None:
    """A path whose intermediate is a collection (not a model) resolves to ``None``."""
    assert resolve_nested_field(ExtensionsSettings, "APPS__0__NAME") is None


def test_resolve_nested_field_primitive_past_leaf() -> None:
    """A path that descends past a primitive leaf resolves to ``None``."""
    assert (
        resolve_nested_field(ExtensionsSettings, "SESSION_REFRESH__MAX_AGE__SUB")
        is None
    )


def test_resolve_nested_field_empty_key() -> None:
    """An empty key resolves to ``None``."""
    assert resolve_nested_field(ExtensionsSettings, "") is None


def test_coerce_nested_field_value_success() -> None:
    """A leaf value is coerced to the leaf's declared type (int → timedelta)."""
    chain, value = coerce_nested_field_value(
        ExtensionsSettings, "SESSION_REFRESH__MAX_AGE", 3600
    )
    assert chain == ("SESSION_REFRESH", "MAX_AGE")
    assert value == timedelta(seconds=3600)


def test_coerce_nested_field_value_preserves_constraint() -> None:
    """The leaf's constraint metadata (``Gt(0)``) is enforced during coercion."""
    with pytest.raises(ValidationError):
        coerce_nested_field_value(
            TasksSettings,
            "SECURITY_HEADERS__STRICT_TRANSPORT_SECURITY__MAX_AGE",
            0,
        )


def test_coerce_nested_field_value_unresolvable_raises_keyerror() -> None:
    """An unresolvable path raises ``KeyError``."""
    with pytest.raises(KeyError):
        coerce_nested_field_value(ExtensionsSettings, "SESSION_REFRESH__BOGUS", 1)


def test_coerce_nested_field_value_rejects_not_overridable_leaf() -> None:
    """A leaf explicitly marked ``not_overridable_field`` raises ``KeyError``."""
    with pytest.raises(KeyError):
        coerce_nested_field_value(_Outer, "NESTED__LOCKED", 9)


def test_coerce_nested_field_value_rejects_not_overridable_intermediate() -> None:
    """A leaf under an explicitly not-overridable *intermediate* raises ``KeyError``.

    ``NESTED.LOCKED_SUB`` is marked ``not_overridable_field``; even though its
    own ``VALUE`` leaf is unmarked, the intermediate marker must block the
    override of any descendant.
    """
    with pytest.raises(KeyError):
        coerce_nested_field_value(_Outer, "NESTED__LOCKED_SUB__VALUE", 9)


def test_chain_has_explicit_not_overridable_flags_intermediate() -> None:
    """The chain check reports a not-overridable intermediate, not only the leaf."""
    assert chain_has_explicit_not_overridable(_Outer, "NESTED__LOCKED_SUB__VALUE")


def test_chain_has_explicit_not_overridable_flags_leaf() -> None:
    """The chain check reports an explicitly not-overridable leaf."""
    assert chain_has_explicit_not_overridable(_Outer, "NESTED__LOCKED")


def test_chain_has_explicit_not_overridable_false_for_open_path() -> None:
    """A fully overridable path reports no explicit not-overridable segment."""
    assert not chain_has_explicit_not_overridable(_Outer, "NESTED__BARE")


def test_chain_has_explicit_not_overridable_false_for_unresolvable() -> None:
    """An unresolvable path reports ``False`` (resolution failure surfaces elsewhere)."""
    assert not chain_has_explicit_not_overridable(_Outer, "NESTED__BOGUS")


def test_not_overridable_field_detected_as_not_hot() -> None:
    """A ``not_overridable_field`` leaf is not HOT."""
    assert is_hot_reloadable(_Inner, "LOCKED") is False


def test_is_nested_overridable_parent_true_for_nested_field() -> None:
    """A ``nested_overridable_field`` parent is nested-overridable."""
    assert is_nested_overridable_parent(_Outer, "NESTED") is True


def test_is_nested_overridable_parent_false_for_plain_field() -> None:
    """An unmarked field is not nested-overridable."""
    assert is_nested_overridable_parent(_Outer, "PLAIN") is False


def test_is_nested_overridable_parent_false_for_missing_field() -> None:
    """An unknown field is not nested-overridable."""
    assert is_nested_overridable_parent(_Outer, "DOES_NOT_EXIST") is False


def test_iter_nested_leaf_keys_session_yields_all_leaves() -> None:
    """``SESSION_REFRESH`` enumerates its five leaves with canonical uppercase chains."""
    leaves = dict(iter_nested_leaf_keys(ExtensionsSettings, "SESSION_REFRESH"))
    assert set(leaves) == {
        "SESSION_REFRESH__COOKIE_NAME",
        "SESSION_REFRESH__MAX_AGE",
        "SESSION_REFRESH__SAMESITE",
        "SESSION_REFRESH__SECURE",
        "SESSION_REFRESH__PATH",
    }
    for key, chain in leaves.items():
        assert "__".join(chain) == key
        assert canonical_override_key(ExtensionsSettings, key) == key


def test_iter_nested_leaf_keys_security_headers_descends_two_levels() -> None:
    """``SECURITY_HEADERS`` enumerates lowercase-child leaves, descending two levels."""
    leaves = dict(iter_nested_leaf_keys(TasksSettings, "SECURITY_HEADERS"))
    assert set(leaves) == {
        "SECURITY_HEADERS__x_frame_options_deny",
        "SECURITY_HEADERS__x_content_type_options_nosniff",
        "SECURITY_HEADERS__referrer_policy_same_origin",
        "SECURITY_HEADERS__content_security_policy_strict",
        "SECURITY_HEADERS__content_security_policy_exclude_paths",
        "SECURITY_HEADERS__strict_transport_security__max_age",
        "SECURITY_HEADERS__strict_transport_security__include_sub_domains",
        "SECURITY_HEADERS__strict_transport_security__preload",
        "SECURITY_HEADERS__permissions_policy__allow_self",
        "SECURITY_HEADERS__permissions_policy__allow_all",
    }
    for key, chain in leaves.items():
        assert "__".join(chain) == key
        assert canonical_override_key(TasksSettings, key) == key


def test_iter_nested_leaf_keys_scalar_field_yields_nothing() -> None:
    """A scalar (non-submodel) field enumerates to no leaves."""
    assert list(iter_nested_leaf_keys(_Outer, "PLAIN")) == []


def test_iter_nested_leaf_keys_unknown_parent_yields_nothing() -> None:
    """An unknown parent field name enumerates to no leaves."""
    assert list(iter_nested_leaf_keys(_Outer, "DOES_NOT_EXIST")) == []


def test_iter_nested_leaf_keys_descends_synthetic_submodel() -> None:
    """Recursion descends through a nested submodel to its grandchild leaf."""
    leaves = dict(iter_nested_leaf_keys(_Outer, "NESTED"))
    assert set(leaves) == {
        "NESTED__BARE",
        "NESTED__LOCKED",
        "NESTED__LOCKED_SUB__VALUE",
    }
    assert leaves["NESTED__LOCKED_SUB__VALUE"] == ("NESTED", "LOCKED_SUB", "VALUE")


def test_resolve_nested_field_metadata_reflects_chain_not_overridable() -> None:
    """A leaf under a ``not_overridable_field`` intermediate reports NOT_OVERRIDABLE.

    The enumerated-leaf reload classification must match the chain check that
    gates PATCH/DELETE, so a leaf is never advertised as editable when an
    intermediate in its chain is locked.
    """
    open_leaf = resolve_nested_field_metadata(_Outer, "NESTED__BARE")
    locked_leaf = resolve_nested_field_metadata(_Outer, "NESTED__LOCKED_SUB__VALUE")
    assert open_leaf is not None
    assert locked_leaf is not None
    assert open_leaf.reload is ReloadClassification.HOT
    assert locked_leaf.reload is ReloadClassification.NOT_OVERRIDABLE


def test_resolve_nested_field_metadata_unknown_leaf_returns_none() -> None:
    """Return no metadata for an unresolvable nested key."""
    assert (
        resolve_nested_field_metadata(ExtensionsSettings, "SESSION_REFRESH__BOGUS")
        is None
    )


@pytest.mark.asyncio
async def test_override_provenance_for_nested_row_includes_all_prefixes(
    session: AsyncSession,
) -> None:
    """Report the stored key and every canonical ancestor of a nested override."""
    row = await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="security_headers__STRICT_TRANSPORT_SECURITY__MAX_AGE",
        value=3600,
    )

    provenance = override_provenance_for_rows(TasksSettings, [row])

    assert set(provenance) == {
        row.key,
        "SECURITY_HEADERS",
        "SECURITY_HEADERS__strict_transport_security",
        "SECURITY_HEADERS__strict_transport_security__max_age",
    }


class _SecretLeafModel(BaseModel):
    """Represent a submodel with a required ``SecretStr`` leaf for resolver tests."""

    TOKEN: SecretStr = SecretStr("s3cr3t")
    LABEL: str = "public"


class _SecretLeafParent(BaseModel):
    """Represent a parent declaring a nested-overridable group over a secret submodel."""

    GROUP: _SecretLeafModel = nested_overridable_field(_SecretLeafModel())


class _OptionalInner(BaseModel):
    """Represent an inner model reached through an optional intermediate."""

    DEEP: int = 1


class _OptionalIntermediate(BaseModel):
    """Represent a submodel whose intermediate child defaults to ``None``."""

    INNER: _OptionalInner | None = None


class _OptionalIntermediateParent(BaseModel):
    """Represent the top-level parent for optional-intermediate resolver tests."""

    NESTED: _OptionalIntermediate = nested_overridable_field(_OptionalIntermediate())


def test_resolve_nested_value_unknown_leaf_raises_keyerror() -> None:
    """Reject a key that cannot resolve to a nested field."""
    proxy = OverridableSettingsProxy(_OptionalIntermediateParent)

    with pytest.raises(KeyError, match="NESTED__BOGUS"):
        resolve_nested_value(
            settings_cls=_OptionalIntermediateParent,
            proxy=proxy,
            key="NESTED__BOGUS",
        )


@pytest.mark.parametrize(
    ("inner_key", "leaf_key"),
    [("INNER", "DEEP"), ("inner", "deep")],
    ids=["exact-mapping-keys", "case-insensitive-mapping-keys"],
)
def test_resolve_nested_value_continues_through_mapping(
    inner_key: str, leaf_key: str
) -> None:
    """Traverse a mapping intermediate and read its child using canonical names."""
    proxy = OverridableSettingsProxy(_OptionalIntermediateParent)
    expected_value = 42
    proxy._set_snapshot({"NESTED": {inner_key: {leaf_key: expected_value}}})

    field, value = resolve_nested_value(
        settings_cls=_OptionalIntermediateParent,
        proxy=proxy,
        key="NESTED__INNER__DEEP",
    )

    assert value == expected_value
    assert field is _OptionalInner.model_fields["DEEP"]


def test_resolve_nested_value_missing_mapping_segment_returns_sentinel() -> None:
    """Return :data:`NESTED_VALUE_MISSING` for a dict snapshot missing a segment."""
    proxy = OverridableSettingsProxy(_SecretLeafParent)
    proxy._set_snapshot({"GROUP": {"LABEL": "visible"}})
    _, value = resolve_nested_value(
        settings_cls=_SecretLeafParent, proxy=proxy, key="GROUP__TOKEN"
    )
    assert value is NESTED_VALUE_MISSING


def test_resolve_nested_value_optional_none_intermediate_returns_none() -> None:
    """Collapse the leaf to ``None`` for a present-``None`` optional intermediate."""
    proxy = OverridableSettingsProxy(_OptionalIntermediateParent)
    _, value = resolve_nested_value(
        settings_cls=_OptionalIntermediateParent,
        proxy=proxy,
        key="NESTED__INNER__DEEP",
    )
    assert value is None
    assert value is not NESTED_VALUE_MISSING


def test_resolve_nested_value_present_none_secret_leaf_returns_none() -> None:
    """Distinguish a present-``None`` secret leaf from a missing segment."""
    proxy = OverridableSettingsProxy(_SecretLeafParent)
    proxy._set_snapshot(
        {"GROUP": _SecretLeafModel.model_construct(TOKEN=None, LABEL="public")}
    )
    _, value = resolve_nested_value(
        settings_cls=_SecretLeafParent, proxy=proxy, key="GROUP__TOKEN"
    )
    assert value is None
    assert value is not NESTED_VALUE_MISSING


class _OverlayLeafOwner(BaseModel):
    """Define a submodel whose overlay promotes one bare leaf; a sibling stays unmarked.

    Mirrors the ``NomadExecutor`` use case: the overlay lives on the class that
    *owns* the resolved leaf, not on the top-level settings class.
    """

    INHERITED_MARKERS: ClassVar[dict[str, dict[str, object]]] = {
        "MARKED": {"reload": ReloadClassification.HOT, "advanced": True},
    }
    MARKED: int = 1
    PLAIN: int = 2


class _OverlayParent(BaseModel):
    """Define a top-level model nesting an overlay-bearing submodel; carries no overlay itself."""

    CHILD: _OverlayLeafOwner = nested_overridable_field(_OverlayLeafOwner())


def test_nested_leaf_uses_owning_class_overlay() -> None:
    """Assert a nested leaf is classified against its owning submodel's overlay."""
    assert chain_has_advanced(_OverlayParent, "CHILD__MARKED") is True
    meta = resolve_nested_field_metadata(_OverlayParent, "CHILD__MARKED")
    assert meta is not None
    assert meta.is_advanced is True
    assert meta.reload is ReloadClassification.HOT


def test_nested_sibling_without_overlay_entry_stays_unmarked() -> None:
    """Assert a sibling leaf with no overlay entry is not promoted (opt-in per field)."""
    assert chain_has_advanced(_OverlayParent, "CHILD__PLAIN") is False
    meta = resolve_nested_field_metadata(_OverlayParent, "CHILD__PLAIN")
    assert meta is not None
    assert meta.is_advanced is False


class _OverlayNestedParent(BaseModel):
    """Define a model whose overlay promotes a bare submodel field to nested-overridable.

    Exercises the overlay path in :func:`is_nested_overridable_parent` and
    :func:`nested_overridable_field_names`: a subclass can mark an inherited
    submodel field ``NESTED_ONLY`` without redeclaring it, and both parent-level
    classifiers must agree with the overlay-aware reload classification.
    """

    INHERITED_MARKERS: ClassVar[dict[str, dict[str, object]]] = {
        "PROMOTED": {"reload": ReloadClassification.NESTED_ONLY},
    }
    PROMOTED: _OverlayLeafOwner = _OverlayLeafOwner()
    UNMARKED: _OverlayLeafOwner = _OverlayLeafOwner()


def test_overlay_promotes_field_to_nested_overridable_parent() -> None:
    """Assert both parent-level classifiers honor an overlay ``NESTED_ONLY`` marker."""
    assert is_nested_overridable_parent(_OverlayNestedParent, "PROMOTED") is True
    assert "PROMOTED" in nested_overridable_field_names(_OverlayNestedParent)


def test_overlay_nested_parent_leaves_unmarked_field_untouched() -> None:
    """Assert a submodel field with no overlay entry is not nested-overridable."""
    assert is_nested_overridable_parent(_OverlayNestedParent, "UNMARKED") is False
    assert "UNMARKED" not in nested_overridable_field_names(_OverlayNestedParent)


def test_rendered_leaf_keys_yields_leaves_for_a_mixed_parent() -> None:
    """Assert a parent with one unsealed leaf still renders as its leaves."""
    assert dict(rendered_leaf_keys(_Outer, "NESTED")) == dict(
        iter_nested_leaf_keys(_Outer, "NESTED")
    )


def test_rendered_leaf_keys_empty_for_a_locked_parent() -> None:
    """Assert a field that takes no nested override at all renders as one row."""
    assert rendered_leaf_keys(ExtensionsSettings, "DIAGNOSTICS_DELIVERY") == []


def test_rendered_leaf_keys_empty_for_a_scalar_hot_field() -> None:
    """Assert a scalar HOT field renders as one row, enumerating no leaves."""
    assert rendered_leaf_keys(ExtensionsSettings, "FOOTER_TEMPLATE") == []


def test_rendered_leaf_keys_empty_when_every_leaf_is_sealed() -> None:
    """Assert an all-sealed parent renders whole, not as leaves no PATCH can target."""
    assert list(
        iter_nested_leaf_keys(ExtensionsSettings, "DIAGNOSTICS_DELIVERY_INPUTS")
    )
    assert rendered_leaf_keys(ExtensionsSettings, "DIAGNOSTICS_DELIVERY_INPUTS") == []


def test_rendered_leaf_keys_keeps_allowlist_withheld_leaves(
    restrict: Callable[..., None],
) -> None:
    """Assert an allowlist withholding every leaf still enumerates them.

    A withheld leaf is not the sealed case: it stays listed so an admin can see
    what the allowlist is holding back, which is why the selection asks whether
    the key is addressable at all rather than whether the policy permits it.
    """
    restrict("Settings.LOGGING")
    assert dict(rendered_leaf_keys(ExtensionsSettings, "SESSION_REFRESH")) == dict(
        iter_nested_leaf_keys(ExtensionsSettings, "SESSION_REFRESH")
    )


class TestNestedValueTraversalGaps:
    """Cover chains the snapshot cannot walk to the end."""

    def test_missing_attribute_segment_returns_sentinel(self) -> None:
        """Return the sentinel when an intermediate object lacks the next segment."""
        proxy = cast(
            "OverridableSettingsProxy",
            SimpleNamespace(NESTED=SimpleNamespace()),
        )

        _, value = resolve_nested_value(
            settings_cls=_OptionalIntermediateParent,
            proxy=proxy,
            key="NESTED__INNER__DEEP",
        )

        assert value is NESTED_VALUE_MISSING


class TestProvenanceKeys:
    """Cover which keys one override row reports a provenance stamp for."""

    @pytest.mark.asyncio
    async def test_unresolvable_nested_row_reports_only_its_stored_key(
        self, session: AsyncSession
    ) -> None:
        """Report no ancestor keys for a nested row that no longer resolves.

        A row whose field was renamed or removed still has to report itself, so
        an admin can see and delete it, without inventing parent keys.

        :param session: The async DB session the row is written through.
        :return: ``None``.
        """
        row = await insert_override_row(
            session,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="GONE__missing_leaf",
            value=1,
        )

        provenance = override_provenance_for_rows(TasksSettings, [row])

        assert set(provenance) == {row.key}


_CANONICAL_NESTED = "NOMAD__timeout"
_LEGACY_NESTED = "nomad__TIMEOUT"
_TOP_LEVEL = "INVENTORY_ENDPOINT"


@pytest.mark.asyncio
async def test_override_rows_for_key_resolves_legacy_nested_casing(
    session: AsyncSession,
) -> None:
    """Assert a mixed-case nested row is found under its canonical key."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_LEGACY_NESTED,
        value=30,
        is_active=True,
    )
    rows = await override_rows_for_key(
        session,
        settings_cls=TasksSettings,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_CANONICAL_NESTED,
    )
    assert [row.key for row in rows] == [_LEGACY_NESTED]


@pytest.mark.asyncio
async def test_override_rows_for_key_returns_legacy_and_canonical_duplicates(
    session: AsyncSession,
) -> None:
    """Assert every row that canonicalizes to the requested key is returned."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_LEGACY_NESTED,
        value=30,
        is_active=True,
    )
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_CANONICAL_NESTED,
        value=45,
        is_active=True,
    )
    rows = await override_rows_for_key(
        session,
        settings_cls=TasksSettings,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_CANONICAL_NESTED,
    )
    assert {row.key for row in rows} == {_LEGACY_NESTED, _CANONICAL_NESTED}


@pytest.mark.asyncio
async def test_override_rows_for_key_excludes_other_setting_class(
    session: AsyncSession,
) -> None:
    """Assert a matching stored key on another class is not returned."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_LEGACY_NESTED,
        value=30,
        is_active=True,
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key=_LEGACY_NESTED,
        value=99,
        is_active=True,
    )
    rows = await override_rows_for_key(
        session,
        settings_cls=TasksSettings,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_CANONICAL_NESTED,
    )
    assert [row.key for row in rows] == [_LEGACY_NESTED]
    assert rows[0].setting_class == TASKS_SETTINGS_TOKEN


@pytest.mark.asyncio
async def test_override_rows_for_key_includes_inactive_row(
    session: AsyncSession,
) -> None:
    """Assert an inactive row is still resolved (write paths match on key alone)."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_LEGACY_NESTED,
        value=30,
        is_active=False,
    )
    rows = await override_rows_for_key(
        session,
        settings_cls=TasksSettings,
        setting_class=TASKS_SETTINGS_TOKEN,
        key=_CANONICAL_NESTED,
    )
    assert [row.key for row in rows] == [_LEGACY_NESTED]
    assert rows[0].is_active is False


@pytest.mark.asyncio
async def test_override_rows_for_key_returns_empty_for_no_match(
    session: AsyncSession,
) -> None:
    """Assert a missing key or an unresolvable stored key yields no rows."""
    await insert_override_row(
        session,
        setting_class=TASKS_SETTINGS_TOKEN,
        key="NOMAD__does_not_exist",
        value=1,
        is_active=True,
    )
    assert (
        await override_rows_for_key(
            session,
            settings_cls=TasksSettings,
            setting_class=TASKS_SETTINGS_TOKEN,
            key=_CANONICAL_NESTED,
        )
        == []
    )
    assert (
        await override_rows_for_key(
            session,
            settings_cls=TasksSettings,
            setting_class=TASKS_SETTINGS_TOKEN,
            key="NOMAD__unknown_leaf",
        )
        == []
    )


@pytest.mark.asyncio
async def test_override_rows_for_key_matches_top_level_case_insensitively(
    session: AsyncSession,
) -> None:
    """Assert a top-level key also matches a mixed-case stored spelling.

    Keeps mixed-case stored keys reachable by DELETE/PATCH now that the ``key``
    match moved from SQL into Python.
    """
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key=_TOP_LEVEL,
        value="https://canonical.example.com",
        is_active=True,
    )
    await insert_override_row(
        session,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key=_TOP_LEVEL.lower(),
        value="https://legacy.example.com",
        is_active=True,
    )
    rows = await override_rows_for_key(
        session,
        settings_cls=ExtensionsSettings,
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
        key=_TOP_LEVEL,
    )
    assert {row.key for row in rows} == {_TOP_LEVEL, _TOP_LEVEL.lower()}
