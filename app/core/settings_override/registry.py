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

"""Classification registry and field-introspection helpers for setting overrides."""

from __future__ import annotations

__all__ = [
    "REMOTE_API_TLS_MARKERS",
    "SECRET_STR_MASK",
    "FieldMarkerKey",
    "FieldMarkers",
    "FieldMetadata",
    "InheritedMarkers",
    "Materializer",
    "MaterializerContext",
    "MaterializerPurpose",
    "ReloadClassification",
    "annotated_type",
    "annotation_contains_credential_url",
    "annotation_contains_secret",
    "annotation_is_credential_url",
    "chain_has_advanced",
    "chain_has_explicit_not_overridable",
    "chain_is_locked",
    "coerce_field_value",
    "coerce_nested_field_value",
    "computed_field_info",
    "dump_field_value",
    "field_materializer",
    "field_reload_classification",
    "hot_field",
    "hot_field_names",
    "is_advanced_field",
    "is_explicit_not_overridable",
    "is_hot_reloadable",
    "is_nested_overridable_parent",
    "iter_class_fields",
    "iter_nested_leaf_keys",
    "materialize_override_value",
    "materialize_template",
    "materialize_via_owning_model",
    "nested_overridable_field",
    "nested_overridable_field_names",
    "not_overridable_field",
    "rendered_leaf_keys",
    "unwrap_secrets_for_storage",
]

import functools
import typing
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from string import Template
from types import UnionType
from typing import Annotated, Any, NamedTuple, TYPE_CHECKING, TypedDict, Union

from pydantic import BaseModel, SecretBytes, SecretStr, TypeAdapter, WrapSerializer
from pydantic.errors import PydanticSchemaGenerationError
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined

from app.core.settings_override.policy import (
    has_allowed_key_under,
    is_key_allowed,
    is_restriction_active,
)
from app.core.settings_override.resolution import resolve_nested_segments
from app.core.utils.fields import _credential_url_serializer
from app.core.utils.pydantic import (
    annotation_pydantic_class,
    CustomFieldMetadata,
    field_with_metadata,
)

if TYPE_CHECKING:
    # Imported only for annotations: the override substrate must not depend on
    # the concrete settings classes at runtime, which lets ``app.core.config``
    # import this module at top level without a circular import.
    from app.core.config import BaseYamlSettings


class ReloadClassification(StrEnum):
    """Declare the reload behavior of an overridable settings field.

    :cvar HOT: Field can be overridden via a DB row and the new value takes
        effect on the next snapshot refresh, without restarting the service.
        For a nested-model field, ``HOT`` permits both whole-object override
        (``PATCH {parent: {...}}``) and per-child override (``parent__leaf``).
    :vartype HOT: str
    :cvar NESTED_ONLY: Nested-model field whose children may be overridden
        (``parent__leaf``) while the parent itself rejects whole-object
        override (``PATCH {parent: {...}}`` → 422). Children default to
        HOT-inherit unless explicitly marked :func:`not_overridable_field`.
    :vartype NESTED_ONLY: str
    :cvar NOT_OVERRIDABLE: Field is not overridable from the database; YAML
        and environment variables remain the only sources of truth.
    :vartype NOT_OVERRIDABLE: str
    """

    HOT = "hot"
    NESTED_ONLY = "nested_only"
    NOT_OVERRIDABLE = "not_overridable"


class FieldMarkerKey(StrEnum):
    """Name the metadata-channel keys carrying a field's classification markers.

    A single definition for the marker vocabulary the override substrate reads
    and writes, so the keys are not repeated as bare string literals across the
    construction helpers (:func:`hot_field` and friends) and the ``.get(...)``
    read sites. As a :class:`~enum.StrEnum` each member *is* its string value,
    so it interoperates with dict keys typed as the plain literals below.

    :cvar RELOAD: The :class:`ReloadClassification` channel.
    :vartype RELOAD: str
    :cvar ADVANCED: The display-only ``advanced`` UI-grouping flag channel.
    :vartype ADVANCED: str
    :cvar MATERIALIZER: The optional snapshot :data:`Materializer` channel.
    :vartype MATERIALIZER: str
    """

    RELOAD = "reload"
    ADVANCED = "advanced"
    MATERIALIZER = "materializer"


class MaterializerPurpose(StrEnum):
    """Name why a materializer is running, so read and write may diverge.

    A materializer that cross-checks its payload against state the deployment
    holds elsewhere needs both verdicts. Rejecting a payload submitted now
    against state it does not match is a client error. A row stored earlier
    against state that has since changed is a deployment condition the operator
    has to be told about, and raising there only drops the row and erases the
    evidence.

    :cvar VALIDATE: A payload submitted through the settings API right now.
    :cvar SNAPSHOT: A row stored earlier, being read back into a snapshot.
    """

    VALIDATE = "validate"
    SNAPSHOT = "snapshot"


class MaterializerContext(NamedTuple):
    """Bundle the inputs a snapshot materializer may consult.

    A materializer receives the whole context and uses only the members it
    needs. :func:`app.core.settings_override.cache.build_snapshot` constructs
    one per overridden HOT field whose declaration attached a materializer,
    instead of calling :func:`coerce_field_value` directly.

    :param settings_cls: The Pydantic settings class that owns the field.
    :param field_name: The name of the field being materialized.
    :param field_info: The Pydantic field metadata for the field.
    :param raw: The raw, JSON-decoded value stored on the override row.
    :param purpose: Whether the value is a payload submitted now or a row
        stored earlier. Defaults to the strict :attr:`MaterializerPurpose.VALIDATE`
        so a materializer that ignores it keeps write-time semantics on both
        paths.
    """

    settings_cls: type[BaseYamlSettings]
    field_name: str
    field_info: FieldInfo
    raw: Any
    purpose: MaterializerPurpose = MaterializerPurpose.VALIDATE


Materializer = Callable[[MaterializerContext], Any]


class FieldMarkers(TypedDict, total=False):
    """Type the marker dict a single field carries or an overlay assigns to one.

    Every key is optional (``total=False``): a field or overlay entry supplies
    only the markers it wants set. Typing overlay literals against this (via
    :data:`InheritedMarkers`) gives their keys and values static checking at
    their declaration sites.

    :cvar reload: The field's reload classification.
    :vartype reload: ReloadClassification
    :cvar advanced: Whether the field is display-only ``advanced`` for UI grouping.
    :vartype advanced: bool
    :cvar materializer: The snapshot materializer to run for the field.
    :vartype materializer: Materializer
    """

    reload: ReloadClassification
    advanced: bool
    materializer: Materializer


def hot_field(
    default: Any,
    *,
    materializer: Materializer | None = None,
    advanced: bool = False,
    **kwargs: Any,
) -> FieldInfo:
    """Declare a settings field as HOT-reloadable from a DB override.

    Thin wrapper over :func:`app.core.utils.pydantic.field_with_metadata` that
    attaches ``{"reload": ReloadClassification.HOT}`` so the field is picked up
    by :func:`is_hot_reloadable` and snapshot building. When ``materializer`` is
    supplied it rides the same metadata channel under the ``"materializer"`` key
    and :func:`app.core.settings_override.cache.build_snapshot` invokes it in
    place of the default :func:`coerce_field_value` coercion -- used for fields
    whose snapshot value cannot be produced by a plain ``TypeAdapter`` (a
    before-validator must run, or the type is not Pydantic-serialisable). When
    ``advanced`` is set it
    rides the same channel under the ``"advanced"`` key, read back by
    :func:`is_advanced_field`; it is display-only metadata and does not affect
    override eligibility.

    :param default: The field's default value, passed positionally to ``Field``.
    :type default: Any
    :param materializer: An optional callable that converts the raw override
        value into the snapshot value. Receives a :class:`MaterializerContext`.
    :type materializer: Materializer | None
    :param advanced: Whether to flag the field as ``advanced`` for UI grouping.
    :param kwargs: Additional keyword arguments forwarded to ``Field``.
    :type kwargs: Any
    :return: A Pydantic field marked with the HOT reload classification.
    :rtype: FieldInfo
    """
    metadata = {
        FieldMarkerKey.RELOAD: ReloadClassification.HOT,
        **(
            {FieldMarkerKey.MATERIALIZER: materializer}
            if materializer is not None
            else {}
        ),
        **({FieldMarkerKey.ADVANCED: True} if advanced else {}),
    }
    return field_with_metadata(default, metadata=metadata, **kwargs)


#: Opt-in class attribute mapping an inherited field name to a marker dict, so a
#: subclass can mark inherited fields without redeclaring them. Keys match the
#: field metadata channel (``"reload"``, ``"advanced"``, ``"materializer"``).
INHERITED_MARKERS_ATTR = "INHERITED_MARKERS"

#: Type of an :data:`INHERITED_MARKERS_ATTR` overlay: field name -> marker dict.
#: The outer mapping is read-only (covariant) -- the right shape for a
#: class-level overlay that is only ever read -- while each value is a
#: :class:`FieldMarkers` so overlay literals get key/value checking.
InheritedMarkers = Mapping[str, FieldMarkers]

#: Shared overlay marking the inherited ``BaseRemoteAPI`` TLS fields HOT and
#: ``advanced``, so every remote-api settings model reuses one definition
#: instead of restating it. Lives in the settings-override layer (not on
#: ``BaseRemoteAPI``) to keep ``app.core.requests`` free of any dependency on
#: ``settings_override``.
REMOTE_API_TLS_MARKERS: InheritedMarkers = {
    "verify_ssl": {"reload": ReloadClassification.HOT, "advanced": True},
    "ssl_cafile": {"reload": ReloadClassification.HOT, "advanced": True},
    "ssl_keyfile": {"reload": ReloadClassification.HOT, "advanced": True},
    "ssl_certfile": {"reload": ReloadClassification.HOT, "advanced": True},
}


def _effective_field_markers(
    field_info: FieldInfo,
    owner_cls: type[BaseModel] | None = None,
    field_name: str | None = None,
) -> dict[str, Any]:
    """Return a field's effective markers: its own metadata plus the owner's overlay.

    The field's own metadata takes precedence -- the ``INHERITED_MARKERS_ATTR``
    overlay only fills keys the field does not already carry, so it can *add*
    markers to an inherited field but never un-mark an explicit declaration.

    :param field_info: The Pydantic field metadata to read.
    :param owner_cls: The class that owns ``field_info``, if known.
    :param field_name: The field's attribute name on ``owner_cls``, if known.
    :return: The field's effective markers keyed by marker name.
    """
    markers = CustomFieldMetadata.field_to_dict(field_info)
    if owner_cls is None or field_name is None:
        return markers
    overlay = getattr(owner_cls, INHERITED_MARKERS_ATTR, None)
    if not isinstance(overlay, Mapping):
        return markers
    entry = overlay.get(field_name)
    if not isinstance(entry, Mapping):
        return markers
    return {**entry, **markers}


def _policy_locked(settings_cls: type[BaseModel], canonical_key: str) -> bool:
    """Return whether ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withholds one canonical key.

    Keys the allowlist on ``settings_cls.__name__``, the same token
    ``ALLOWED_KEYS`` entries use. A class that is not a
    :class:`~app.core.settings_override.models.SettingClassEnum` member is
    therefore still reachable when the allowlist names it, and still withheld
    when it does not.

    :param settings_cls: The top-level Pydantic settings class owning the key.
    :param canonical_key: The canonical override key: a top-level field name or
        a ``__``-delimited nested path in its case-corrected spelling.
    :return: ``True`` when a restriction is active and does not allow this pair.
    """
    if not is_restriction_active():
        return False
    return not is_key_allowed(settings_cls.__name__, canonical_key)


def is_hot_reloadable(
    settings_cls: type[BaseModel],
    field_name: str,
    *,
    include_policy_gate: bool = True,
) -> bool:
    """Return whether the given field is marked HOT on the given settings class.

    Accepts any Pydantic ``BaseModel`` subclass, not just ``BaseYamlSettings``:
    the nested-override resolver consults this predicate against nested
    submodels (e.g. ``CookieOptions``) when classifying leaf fields.

    The policy gate, however, only answers for a top-level settings class: it
    keys the allowlist on the class ``__name__`` and the key as spelled, and a
    submodel is named by no entry, so the gate withholds it unconditionally.
    Pass ``include_policy_gate=False`` when inspecting a submodel, or ask
    :func:`chain_is_locked` with the top-level class and the full
    ``__``-delimited key instead.

    :param settings_cls: The Pydantic model class to inspect.
    :param field_name: The name of the field to check.
    :param include_policy_gate: Whether to also require
        ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` to permit the key. Pass ``False`` to
        read the static declaration alone, which is what distinguishes a field
        the allowlist withholds from one the code declares not overridable, and
        which is required when ``settings_cls`` is a submodel.
    :return: ``True`` when ``field_name`` exists on ``settings_cls``, is
        marked with ``{"reload": ReloadClassification.HOT}`` via
        :func:`app.core.utils.pydantic.field_with_metadata` or the class overlay,
        and (unless ``include_policy_gate`` is ``False``) the allowlist permits
        overriding it.
    """
    field = settings_cls.model_fields.get(field_name)
    if field is None:
        return False
    markers = _effective_field_markers(
        field, owner_cls=settings_cls, field_name=field_name
    )
    if markers.get(FieldMarkerKey.RELOAD) != ReloadClassification.HOT:
        return False
    return not (include_policy_gate and _policy_locked(settings_cls, field_name))


def field_reload_classification(
    field_info: FieldInfo,
    *,
    owner_cls: type[BaseModel] | None = None,
    field_name: str | None = None,
) -> ReloadClassification:
    """Return the reload classification attached to a single field.

    Reads the ``{"reload": ...}`` metadata set by :func:`hot_field`,
    :func:`nested_overridable_field`, or :func:`not_overridable_field`. Any
    field with no recognised marker is reported ``NOT_OVERRIDABLE``.

    Unlike :func:`is_hot_reloadable` (which takes a settings class plus a field
    name), this operates on a :class:`FieldInfo` directly so callers can
    classify a nested leaf resolved out of a submodel.

    When both ``owner_cls`` and ``field_name`` are supplied
    ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` is consulted too, so a field it withholds
    reports ``NOT_OVERRIDABLE`` and the settings API describes what it will
    actually accept. That lookup only answers for a top-level settings class,
    and this function exposes no way to skip it alone: omitting ``owner_cls`` /
    ``field_name`` drops the class overlay along with it. Classify a submodel
    leaf through :func:`chain_is_locked` instead, passing the top-level class
    and the full ``__``-delimited key.

    :param field_info: The Pydantic field metadata to classify.
    :param owner_cls: The class owning ``field_info``, for overlay lookup.
    :param field_name: The field's name on ``owner_cls``, for overlay lookup.
    :return: The field's reload classification.
    """
    value = _effective_field_markers(field_info, owner_cls, field_name).get(
        FieldMarkerKey.RELOAD
    )
    if value not in {ReloadClassification.HOT, ReloadClassification.NESTED_ONLY}:
        return ReloadClassification.NOT_OVERRIDABLE
    if (
        owner_cls is not None
        and field_name is not None
        and _policy_locked(owner_cls, field_name)
    ):
        return ReloadClassification.NOT_OVERRIDABLE
    return value


def is_explicit_not_overridable(
    field_info: FieldInfo,
    *,
    owner_cls: type[BaseModel] | None = None,
    field_name: str | None = None,
) -> bool:
    """Return whether a field carries an *explicit* ``NOT_OVERRIDABLE`` marker.

    Distinct from ``field_reload_classification(...) == NOT_OVERRIDABLE``: an
    unmarked field reports ``NOT_OVERRIDABLE`` from
    :func:`field_reload_classification` (the default top-level classification),
    but a *nested leaf* under a nested-overridable parent inherits HOT unless it
    is explicitly :func:`not_overridable_field`-marked. This predicate is
    ``True`` only for the explicit marker, so unmarked nested leaves stay
    overridable.

    :param field_info: The Pydantic field metadata to inspect.
    :param owner_cls: The class owning ``field_info``, for overlay lookup.
    :param field_name: The field's name on ``owner_cls``, for overlay lookup.
    :return: ``True`` iff the field has an explicit ``NOT_OVERRIDABLE`` marker.
    :rtype: bool
    """
    return (
        _effective_field_markers(field_info, owner_cls, field_name).get(
            FieldMarkerKey.RELOAD
        )
        == ReloadClassification.NOT_OVERRIDABLE
    )


def hot_field_names(settings_cls: type[BaseYamlSettings]) -> frozenset[str]:
    """Return the set of field names on ``settings_cls`` marked HOT.

    :param settings_cls: The Pydantic settings class to inspect.
    :type settings_cls: type[BaseYamlSettings]
    :return: A frozenset of field names declared HOT via
        :func:`app.core.utils.pydantic.field_with_metadata`.
    :rtype: frozenset[str]
    """
    return frozenset(
        name
        for name in settings_cls.model_fields
        if is_hot_reloadable(settings_cls, name)
    )


def annotated_type(field_info: FieldInfo) -> Any:
    """Reassemble the constraint-preserving annotated type for a field.

    Constraint metadata attached to the field's annotation (e.g. ``Gt(0)`` from
    ``PositiveInt``) is preserved by re-assembling an ``Annotated`` type from
    ``field_info.annotation`` plus every non-:class:`CustomFieldMetadata` item
    in ``field_info.metadata``. Without this, ``TypeAdapter(field_info.annotation)``
    would accept values the original settings model rejects — e.g. a negative
    integer override for a ``PositiveInt`` field would silently load.

    Public for a second reason, and it is the load-bearing one for the at-rest
    walker in :mod:`app.core.settings_override.secret_storage`: Pydantic hoists
    a non-``Optional`` field's ``Annotated`` metadata onto ``FieldInfo``, so
    ``field_info.annotation`` alone is a bare type carrying none of the markers
    that decide how a leaf is stored. A field typed
    :data:`~app.core.utils.fields.CredentialHttpUrl` presents as a bare
    :class:`~pydantic.HttpUrl`, and a classifier reading ``.annotation``
    silently misses it. A second re-assembly there would drift from this one.

    :param field_info: The Pydantic field metadata for the target attribute.
    :return: The field's annotation, wrapped in ``Annotated`` together with its
        preserved constraint metadata when any constraints are present.
    """
    constraints = tuple(
        item
        for item in field_info.metadata
        if not isinstance(item, CustomFieldMetadata)
    )
    if constraints:
        return Annotated[
            (field_info.annotation, *constraints)  # ty: ignore[invalid-type-form]
        ]
    return field_info.annotation


def _coerce_value(field_info: FieldInfo, raw: Any) -> Any:
    """Coerce a raw JSON-decoded value to the field's declared Python type.

    :param field_info: The Pydantic field metadata for the target attribute.
    :type field_info: FieldInfo
    :param raw: The JSON-decoded value as stored on the override row.
    :type raw: Any
    :return: The validated Python value matching ``field_info.annotation``
        plus its preserved constraint metadata.
    :rtype: Any
    :raises ValidationError: If ``raw`` cannot be coerced to the declared
        type or violates a preserved constraint. Callers handle and log.
    """
    return TypeAdapter(annotated_type(field_info)).validate_python(raw)


def coerce_field_value(field_info: FieldInfo, raw: Any) -> Any:
    """Validate and coerce a raw value to the field's declared annotated type.

    Mirrors the validation that :func:`app.core.settings_override.cache.build_snapshot`
    performs when materialising a DB-override row into a typed Python value,
    including preservation of constraint metadata (``PositiveInt`` etc.) via
    the ``annotated_type`` reassembly.

    :param field_info: The Pydantic field metadata for the target attribute.
    :param raw: The raw, JSON-decoded value to validate and coerce.
    :return: The validated Python value matching the field's annotation plus
        its preserved constraint metadata.
    :raises ValidationError: If ``raw`` cannot be coerced or violates a
        preserved constraint. Callers in the API layer map this to HTTP 422.
    """
    return _coerce_value(field_info, raw)


def field_materializer(
    settings_cls: type[BaseYamlSettings], field_name: str
) -> Materializer | None:
    """Return the materializer declared on a field, or ``None`` if none.

    Reads the ``"materializer"`` entry attached by :func:`hot_field` through the
    same custom-metadata channel :func:`is_hot_reloadable` reads ``"reload"`` from.

    :param settings_cls: The Pydantic settings class to inspect.
    :type settings_cls: type[BaseYamlSettings]
    :param field_name: The name of the field to check.
    :type field_name: str
    :return: The declared :data:`Materializer`, or ``None`` when the field is
        unknown or declares no materializer.
    :rtype: Materializer | None
    """
    field = settings_cls.model_fields.get(field_name)
    if field is None:
        return None
    markers = _effective_field_markers(
        field, owner_cls=settings_cls, field_name=field_name
    )
    return markers.get(FieldMarkerKey.MATERIALIZER)


def is_advanced_field(
    field_info: FieldInfo,
    *,
    owner_cls: type[BaseModel] | None = None,
    field_name: str | None = None,
) -> bool:
    """Return whether a field is flagged ``advanced`` via field metadata.

    Reads the ``"advanced"`` entry attached by :func:`hot_field`,
    :func:`nested_overridable_field`, or :func:`not_overridable_field` through
    the same custom-metadata channel :func:`field_reload_classification` reads
    ``"reload"`` from. ``advanced`` is display-only metadata used by the settings
    UI to group rarely-changed, easy-to-misconfigure settings separately; it does
    not affect override, PATCH, or DELETE eligibility. Operates on a
    :class:`FieldInfo` directly so callers can classify a nested leaf resolved
    out of a submodel.

    :param field_info: The Pydantic field metadata to inspect.
    :param owner_cls: The class owning ``field_info``, for overlay lookup.
    :param field_name: The field's name on ``owner_cls``, for overlay lookup.
    :return: ``True`` iff the field carries an explicit ``advanced`` marker.
    """
    return (
        _effective_field_markers(field_info, owner_cls, field_name).get(
            FieldMarkerKey.ADVANCED, False
        )
        is True
    )


def materialize_via_owning_model(ctx: MaterializerContext) -> Any:
    """Materialize a value by validating it through its owning settings class.

    Runs the owning class's ``mode="before"`` validators (which a bare
    ``TypeAdapter`` on the field annotation would not invoke) by validating a
    single-key payload and reading the resulting attribute back. Only valid for
    settings classes whose every other field is defaulted, so a one-key
    ``model_validate`` succeeds.

    :param ctx: The materialization context.
    :type ctx: MaterializerContext
    :return: The materialized value as the owning model produces it.
    :rtype: Any
    :raises ValidationError: If the owning model rejects the one-key payload.
    :raises ValueError: If a ``mode="before"`` validator rejects ``raw``.
    """
    validated = ctx.settings_cls.model_validate({ctx.field_name: ctx.raw})
    return getattr(validated, ctx.field_name)


def materialize_template(ctx: MaterializerContext) -> Any:
    """Materialize a :class:`string.Template` from a raw string override.

    ``TypeAdapter(Template)`` raises :class:`PydanticSchemaGenerationError`, so a
    ``Template`` field cannot use the default coercion path. A raw string is
    wrapped in a ``Template``; an already-``Template`` value passes through. Any
    other type is rejected -- otherwise a non-string override (e.g. ``1``) would
    be published into the snapshot and crash the next ``safe_substitute`` read
    with ``AttributeError``.

    :param ctx: The materialization context.
    :type ctx: MaterializerContext
    :return: A :class:`string.Template` for the override.
    :rtype: Any
    :raises ValueError: If ``raw`` is neither a string nor a ``Template``.
    """
    if isinstance(ctx.raw, Template):
        return ctx.raw
    if isinstance(ctx.raw, str):
        return Template(ctx.raw)
    raise ValueError(
        f"{ctx.field_name} override must be a string, got {type(ctx.raw).__name__}"
    )


def materialize_override_value(
    settings_cls: type[BaseYamlSettings],
    field_name: str,
    field_info: FieldInfo,
    raw: Any,
    *,
    purpose: MaterializerPurpose = MaterializerPurpose.VALIDATE,
) -> Any:
    """Turn a raw override value into its typed snapshot value.

    Routes through the field's declared materializer when present, otherwise the
    default :func:`coerce_field_value` coercion. Shared by snapshot building
    (:func:`app.core.settings_override.cache.build_snapshot`) and the settings
    API PATCH validation so both accept exactly the same override payloads -- a
    materializer-backed field (``PROVIDERS``, ``FOOTER_TEMPLATE``)
    would otherwise be accepted on snapshot load but rejected by the API.

    ``purpose`` is the one channel through which the two paths may diverge, and
    the snapshot builder is the only caller that sets it. Every materializer
    that ignores it is therefore unaffected.

    :param settings_cls: The Pydantic settings class that owns the field.
    :param field_name: The name of the field being materialized.
    :param field_info: The Pydantic field metadata for the field.
    :param raw: The raw, JSON-decoded override value.
    :param purpose: Whether ``raw`` is a payload submitted now or a row stored
        earlier.
    :return: The materialized (or coerced) typed value.
    :raises ValidationError: If coercion or the materializer's validation fails.
    :raises ValueError: If a ``mode="before"`` validator rejects ``raw``.
    """
    materializer = field_materializer(settings_cls, field_name)
    if materializer is not None:
        return materializer(
            MaterializerContext(settings_cls, field_name, field_info, raw, purpose)
        )
    return coerce_field_value(field_info, raw)


def nested_overridable_field(
    default: Any, *, advanced: bool = False, **kwargs: Any
) -> FieldInfo:
    """Declare a nested-model field whose children may be overridden by DB rows.

    The parent field itself rejects whole-object override
    (``PATCH {parent: {...}}`` → 422). Nested children (``parent__leaf``) are
    accepted, defaulting to HOT-inherit unless the leaf is explicitly
    :func:`not_overridable_field`-marked.

    Mirrors :func:`hot_field`'s call signature; attaches
    ``{"reload": ReloadClassification.NESTED_ONLY}``. When ``advanced`` is set,
    the parent carries an ``advanced`` marker that every emitted leaf inherits
    via :func:`chain_has_advanced` -- the parent's per-leaf expansion would
    otherwise never surface the flag for the session/security-header fields.

    :param default: The field's default value, passed positionally to ``Field``.
    :type default: Any
    :param advanced: Whether to flag the field (and its leaves) as ``advanced``.
    :param kwargs: Additional keyword arguments forwarded to ``Field``.
    :type kwargs: Any
    :return: A Pydantic field marked NESTED_ONLY.
    :rtype: FieldInfo
    """
    metadata = {
        FieldMarkerKey.RELOAD: ReloadClassification.NESTED_ONLY,
        **({FieldMarkerKey.ADVANCED: True} if advanced else {}),
    }
    return field_with_metadata(default, metadata=metadata, **kwargs)


def not_overridable_field(
    default: Any, *, advanced: bool = False, **kwargs: Any
) -> FieldInfo:
    """Declare a settings field as explicitly NOT overridable from a DB row.

    Mirrors :func:`hot_field` but attaches
    ``{"reload": ReloadClassification.NOT_OVERRIDABLE}``. Use under a HOT or
    NESTED_ONLY parent when a specific nested leaf must NOT inherit the
    parent's HOT-by-default child semantics, or on a top-level field that must
    stay environment- and YAML-only -- a whole-object PATCH and every
    ``__``-delimited leaf PATCH are both rejected, so a block whose validity
    depends on cross-field validation is never assembled one leaf at a time.
    ``advanced`` rides the same channel as on the other helpers and is
    independent of the reload classification.

    :param default: The field's default value, passed positionally to ``Field``.
    :param advanced: Whether to flag the field as ``advanced``.
    :param kwargs: Additional keyword arguments forwarded to ``Field``.
    :return: A Pydantic field marked NOT_OVERRIDABLE.
    """
    metadata = {
        FieldMarkerKey.RELOAD: ReloadClassification.NOT_OVERRIDABLE,
        **({FieldMarkerKey.ADVANCED: True} if advanced else {}),
    }
    return field_with_metadata(default, metadata=metadata, **kwargs)


def is_nested_overridable_parent(
    settings_cls: type[BaseModel],
    field_name: str,
    *,
    include_policy_gate: bool = True,
) -> bool:
    """Return whether ``field_name`` accepts nested-child overrides.

    ``True`` iff the field's classification is :attr:`ReloadClassification.HOT`
    OR :attr:`ReloadClassification.NESTED_ONLY`. ``False`` for unknown fields
    and for fields classified ``NOT_OVERRIDABLE``.

    Under an active allowlist the parent additionally has to lead somewhere: one
    allowed descendant keeps it addressable, while a parent whose every leaf is
    withheld stops accepting nested overrides entirely.

    Used by :func:`app.core.settings_override.api.routes._validate_patch_body`
    and :func:`app.core.settings_override.cache.build_snapshot` to gate
    ``__``-delimited keys at the parent level before walking into the nested
    resolver.

    :param settings_cls: The Pydantic settings class declaring the field.
    :param field_name: The top-level field name.
    :param include_policy_gate: Whether to also require
        ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` to leave something reachable under the
        parent. Pass ``False`` to ask only whether the key is addressable at all,
        which is what keeps a stale row under a fully withheld parent deletable.
    :return: ``True`` iff nested-child overrides may target this field.
    """
    info = settings_cls.model_fields.get(field_name)
    if info is None:
        return False
    markers = _effective_field_markers(
        info, owner_cls=settings_cls, field_name=field_name
    )
    if markers.get(FieldMarkerKey.RELOAD) not in {
        ReloadClassification.HOT,
        ReloadClassification.NESTED_ONLY,
    }:
        return False
    if not include_policy_gate or not is_restriction_active():
        return True
    return has_allowed_key_under(settings_cls.__name__, field_name)


def nested_overridable_field_names(
    settings_cls: type[BaseModel],
) -> frozenset[str]:
    """Return the set of field names on ``settings_cls`` marked ``NESTED_ONLY``.

    Parallels :func:`hot_field_names`. Used by tests and by future tooling that
    needs to surface which parents accept nested overrides.

    :param settings_cls: The Pydantic settings class to introspect.
    :type settings_cls: type[BaseModel]
    :return: A frozenset of field names declared NESTED_ONLY.
    :rtype: frozenset[str]
    """
    return frozenset(
        name
        for name, info in settings_cls.model_fields.items()
        if _effective_field_markers(info, owner_cls=settings_cls, field_name=name).get(
            FieldMarkerKey.RELOAD
        )
        == ReloadClassification.NESTED_ONLY
    )


def chain_has_explicit_not_overridable(settings_cls: type[BaseModel], key: str) -> bool:
    """Return whether any segment of a nested key is explicitly not-overridable.

    Unlike checking only the resolved leaf, this walks every segment from the
    top-level parent to the leaf and reports ``True`` if *any* of them carries an
    explicit :func:`not_overridable_field` marker. An intermediate model marked
    not-overridable therefore blocks overrides of its descendants, matching the
    contract a reader would expect from the marker. Returns ``False`` for an
    unresolvable key (the caller surfaces the resolution failure separately).

    :param settings_cls: The top-level Pydantic settings class.
    :type settings_cls: type[BaseModel]
    :param key: The ``__``-delimited override key.
    :type key: str
    :return: ``True`` iff some segment is explicitly ``NOT_OVERRIDABLE``.
    :rtype: bool
    """
    resolved = resolve_nested_segments(settings_cls, key)
    if resolved is None:
        return False
    return any(
        is_explicit_not_overridable(info, owner_cls=owner, field_name=name)
        for owner, name, info in resolved
    )


def chain_has_advanced(settings_cls: type[BaseModel], key: str) -> bool:
    """Return whether any segment of a nested key is flagged ``advanced``.

    Walks every segment from the top-level parent down to the leaf -- mirroring
    :func:`chain_has_explicit_not_overridable` -- and reports ``True`` if *any* of
    them carries an ``advanced`` marker. A parent marked advanced therefore
    propagates the flag to every leaf it expands into, which is
    the only way the dashboard sees ``advanced`` for the session and
    security-header fields whose parent, not leaves, is marked. Returns ``False``
    for an unresolvable key.

    :param settings_cls: The top-level Pydantic settings class.
    :param key: The ``__``-delimited override key.
    :return: ``True`` iff some segment in the chain is flagged ``advanced``.
    """
    resolved = resolve_nested_segments(settings_cls, key)
    if resolved is None:
        return False
    return any(
        is_advanced_field(info, owner_cls=owner, field_name=name)
        for owner, name, info in resolved
    )


def chain_is_locked(settings_cls: type[BaseModel], key: str) -> bool:
    """Return whether a nested key is closed to overrides, statically or by policy.

    Composes the two independent reasons a nested path may be refused: an
    explicit :func:`not_overridable_field` marker anywhere along the chain
    (:func:`chain_has_explicit_not_overridable`), or
    ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` withholding the leaf. The policy lookup
    uses the canonical chain rather than ``key`` as spelled, so a
    case-insensitive spelling reaches the same verdict as the row it would
    resolve to. Returns ``False`` for an unresolvable key (the caller surfaces
    the resolution failure separately).

    Kept distinct from :func:`chain_has_explicit_not_overridable`, which stays
    purely static: telling "locked by the code" from "locked by the allowlist"
    is what lets DELETE clear a stale row for a key the allowlist withheld.

    :param settings_cls: The top-level Pydantic settings class.
    :param key: The ``__``-delimited override key.
    :return: ``True`` iff an override of ``key`` would be refused.
    """
    resolved = resolve_nested_segments(settings_cls, key)
    if resolved is None:
        return False
    if any(
        is_explicit_not_overridable(info, owner_cls=owner, field_name=name)
        for owner, name, info in resolved
    ):
        return True
    canonical_key = "__".join(name for _owner, name, _info in resolved)
    return _policy_locked(settings_cls, canonical_key)


def coerce_nested_field_value(
    settings_cls: type[BaseModel],
    key: str,
    raw: Any,
) -> tuple[tuple[str, ...], Any]:
    """Resolve ``key`` to a nested attribute chain and coerce ``raw`` to the leaf type.

    Combines :func:`app.core.settings_override.resolution.resolve_nested_field`
    and :func:`coerce_field_value` so the
    cache and API layers have one entry point for the full nested-row coercion
    contract. A path whose leaf *or any intermediate* is explicitly classified
    ``NOT_OVERRIDABLE``, or whose leaf ``SETTINGS_OVERRIDE.ALLOWED_KEYS``
    withholds, is rejected by raising :class:`KeyError`, matching the
    unresolvable-path contract so the caller's existing ``except KeyError``
    branch logs and skips uniformly.

    :param settings_cls: The top-level Pydantic settings class.
    :param key: The override row's ``__``-delimited key.
    :param raw: The raw JSON-decoded value to coerce.
    :return: ``((canonical_segment, ...), coerced_value)``.
    :raises KeyError: If the path is unresolvable on ``settings_cls``, any
        segment along it is explicitly classified ``NOT_OVERRIDABLE``, or
        ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` does not allow overriding the leaf.
    :raises ValidationError: If ``raw`` cannot be coerced to the leaf's type.
    """
    resolved = resolve_nested_segments(settings_cls, key)
    if resolved is None:
        raise KeyError(key)
    if chain_is_locked(settings_cls, key):
        raise KeyError(key)
    chain = tuple(name for _owner, name, _info in resolved)
    leaf_info = resolved[-1][2]
    return chain, coerce_field_value(leaf_info, raw)


def _clear_cached_properties(instance: BaseModel) -> None:
    """Remove every ``@cached_property`` memo from ``instance.__dict__``.

    Pydantic ``model_copy()`` is shallow and carries over any
    :class:`functools.cached_property` values that were already evaluated on
    the source instance. Drop them so the copy recomputes lazily against its
    new (possibly overridden) field values.

    :param instance: The freshly-copied Pydantic instance to clean.
    :type instance: BaseModel
    """
    cls = type(instance)
    for name in list(instance.__dict__):
        # ``getattr`` walks the full MRO, so a ``cached_property`` declared on a
        # base class (e.g. ``BaseRemoteAPI.logger``) is cleared too -- scanning
        # only ``cls.__dict__`` would leave those inherited memos stale.
        if isinstance(getattr(cls, name, None), functools.cached_property):
            del instance.__dict__[name]


@dataclass(slots=True, frozen=True, kw_only=True)
class FieldMetadata:
    """Represent introspected metadata for a single settings field.

    :param key: The field name on the owning settings class.
    :type key: str
    :param annotation: The field's declared Python annotation (without any
        constraint metadata reassembly).
    :type annotation: Any
    :param default: The field's declared default value, or
        :data:`pydantic_core.PydanticUndefined` when no default exists.
    :type default: Any
    :param description: The field's free-text description, or ``None`` when
        not declared.
    :type description: str | None
    :param reload: The reload classification for this field
        (``HOT`` or ``NOT_OVERRIDABLE``).
    :type reload: ReloadClassification
    :param is_secret: Whether the field's annotation contains a
        :class:`pydantic.SecretStr` / :class:`pydantic.SecretBytes`, either at
        the top level or nested inside a Pydantic submodel.
    :type is_secret: bool
    :param is_complex: Whether the field's annotation is or contains a Pydantic
        :class:`pydantic.BaseModel` subclass (true for nested submodels and
        unions containing them).
    :type is_complex: bool
    :param is_advanced: Whether the field is flagged ``advanced`` for UI grouping.
        For a nested leaf this is chain-resolved: ``True`` when the leaf or any
        ancestor is marked. Display-only; does not affect override eligibility.
    """

    key: str
    annotation: Any
    default: Any
    description: str | None
    reload: ReloadClassification
    is_secret: bool
    is_complex: bool
    is_advanced: bool = False


def _iter_type_arguments(annotation: Any) -> Iterator[Any]:
    """Yield every type argument referenced by ``annotation`` recursively.

    Walks unions, generic containers (``list[X]``, ``dict[K, V]``, etc.) and
    :class:`pydantic.BaseModel` subclasses, descending into the latter's
    fields so nested model attributes are inspected too. Also queues each
    model's ``__subclasses__()`` so secrets declared only on concrete
    subclasses of a polymorphic base remain reachable (limited to subclasses
    already imported when this runs).

    A model's fields are queued through :func:`annotated_type` rather than as
    bare ``.annotation`` values, because Pydantic hoists a non-``Optional``
    field's ``Annotated`` metadata onto ``FieldInfo``. Descending on the bare
    annotation drops every marker one level down, which is invisible to a
    predicate keyed on a type and fatal to one keyed on metadata.

    Queueing those aliases is also why ``keep_alive`` exists. The cycle guard
    keys on :func:`id`, which only identifies an object for as long as that
    object lives, and :func:`annotated_type` returns a value ``Annotated``
    built on demand rather than an attribute of anything. ``typing`` memoises
    that construction in a 128-entry LRU whose overflow is evicted, and skips
    it altogether for metadata that does not hash, so a walk wide enough to
    pass either limit frees an alias whose address a later one can reuse — and
    a recycled address already in ``seen`` would silently prune a subtree the
    walk never looked at. Holding every visited object for the duration keeps
    the addresses distinct. The leaves this guards reach the at-rest
    encryption predicates, where a pruned subtree means a credential stored in
    the clear, so the walk must not depend on when a cache evicts.

    :param annotation: The type annotation to walk.
    :return: An iterator over the referenced type arguments.
    """
    seen = set()
    keep_alive: list[Any] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if current is None or current is type(None):
            continue
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        keep_alive.append(current)
        yield current
        origin = typing.get_origin(current)
        if origin is not None:
            stack.extend(typing.get_args(current))
            continue
        if isinstance(current, type) and issubclass(current, BaseModel):
            stack.extend(
                annotated_type(nested) for nested in current.model_fields.values()
            )
            stack.extend(current.__subclasses__())


#: Pydantic's default JSON dump mask for :class:`~pydantic.SecretStr` /
#: :class:`~pydantic.SecretBytes`. Distinct from
#: :data:`~app.core.utils.fields.CREDENTIAL_URL_MASK` (``"****"``).
SECRET_STR_MASK = "**********"  # noqa: S105 # nosec B105


def annotation_contains_secret(annotation: Any) -> bool:
    """Return whether a Pydantic secret type is reachable from ``annotation``.

    Walks the annotation recursively (nested models and imported concrete
    subclasses of polymorphic bases), looking for
    :class:`pydantic.SecretStr` or :class:`pydantic.SecretBytes`.

    Public because the at-rest walker in
    :mod:`app.core.settings_override.secret_storage` decides which leaves to
    encrypt with this exact rule, and a second copy of it there would let the
    two drift into disagreeing about which fields are credentials.

    :param annotation: The type annotation to inspect.
    :return: ``True`` when a secret type is reachable from the annotation.
    """
    return any(
        isinstance(arg, type) and issubclass(arg, SecretStr | SecretBytes)
        for arg in _iter_type_arguments(annotation)
    )


def _field_contains_secret(field_info: FieldInfo) -> bool:
    """Return whether ``field_info`` exposes a Pydantic secret anywhere in its annotation.

    :param field_info: The Pydantic field metadata for the target attribute.
    :return: ``True`` when a secret type is reachable from the annotation.
    """
    return annotation_contains_secret(field_info.annotation)


def _unwrap_secret_value(current: Any) -> str | bytes | None:
    """Return the plain secret from a ``SecretStr``/``SecretBytes``, else ``None``.

    :param current: A live stored value that may be a Pydantic secret wrapper.
    :return: ``get_secret_value()`` when ``current`` is a secret instance;
        ``None`` otherwise.
    """
    if isinstance(current, SecretStr | SecretBytes):
        return current.get_secret_value()
    return None


def unwrap_secrets_for_storage(value: Any) -> Any:
    """Return a JSON-column-safe form of ``value`` with secrets as plaintext.

    Override rows persist through a JSON column. Assigning a
    :class:`~pydantic.SecretStr` / :class:`~pydantic.SecretBytes` (or a
    model/mapping that contains one) is unsafe: Pydantic's secret-aware
    serialisation rewrites the credential to :data:`SECRET_STR_MASK`, and that
    mask is what ends up stored. Snapshot load expects plaintext JSON and
    re-wraps via :func:`coerce_field_value` / :func:`materialize_override_value`.

    :param value: A coerced/materialized PATCH value, possibly containing
        secret wrappers.
    :return: ``value`` with every secret wrapper replaced by its plain
        ``get_secret_value()`` (``SecretBytes`` decoded as UTF-8 with
        surrogateescape so the result stays JSON-serialisable).
    """
    unwrapped = _unwrap_secret_value(value)
    if unwrapped is not None:
        if isinstance(unwrapped, bytes):
            return unwrapped.decode("utf-8", errors="surrogateescape")
        return unwrapped
    if isinstance(value, BaseModel):
        return {
            name: unwrap_secrets_for_storage(getattr(value, name))
            for name in value.__class__.model_fields
        }
    if isinstance(value, Mapping):
        return {key: unwrap_secrets_for_storage(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [unwrap_secrets_for_storage(item) for item in value]
    return value


def _metadata_has_credential_url_serializer(metadata: tuple[Any, ...]) -> bool:
    """Return whether ``metadata`` carries the credential URL JSON serializer.

    :param metadata: The ``__metadata__`` tuple of an ``Annotated`` type.
    :return: ``True`` when the credential-URL serializer is among the markers.
    """
    return any(
        isinstance(item, WrapSerializer) and item.func is _credential_url_serializer
        for item in metadata
    )


def annotation_contains_credential_url(annotation: Any) -> bool:
    """Return whether a credential-bearing URL is reachable from ``annotation``.

    The subtree question, and the counterpart of
    :func:`annotation_contains_secret`: it descends nested models and imported
    subclasses looking for the credential-URL marker anywhere below. Use
    :func:`annotation_is_credential_url` for "is the value *at this position*
    one", which is what a leaf transform needs.

    :param annotation: The type annotation to inspect.
    :return: ``True`` when the marker is reachable from the annotation.
    """
    return any(
        _metadata_has_credential_url_serializer(getattr(arg, "__metadata__", ()))
        for arg in _iter_type_arguments(annotation)
    )


def annotation_is_credential_url(annotation: Any) -> bool:
    """Return whether the value at this JSON position is itself a credential URL.

    Flattens unions and optionals but deliberately **not** ``Annotated``: the
    marker lives in ``__metadata__``, so stripping the wrapper first — which
    ``secret_storage._positional_args`` does — discards the very thing being
    tested.

    :param annotation: The type annotation to inspect.
    :return: ``True`` when a value at this position is a credential-bearing URL.
    """
    stack = [annotation]
    while stack:
        current = stack.pop()
        if current is None or current is type(None):
            continue
        if _metadata_has_credential_url_serializer(
            getattr(current, "__metadata__", ())
        ):
            return True
        if hasattr(current, "__metadata__"):
            stack.append(typing.get_args(current)[0])
            continue
        if typing.get_origin(current) in {Union, UnionType}:
            stack.extend(typing.get_args(current))
    return False


def is_credential_url_field(field_info: FieldInfo) -> bool:
    """Return whether ``field_info``'s subtree carries a credential-bearing URL.

    :param field_info: The Pydantic field metadata for the target attribute.
    :return: ``True`` when the marker is reachable from the field's annotation.
    """
    return annotation_contains_credential_url(annotated_type(field_info))


def _stable_collection_sort_key(item: Any) -> tuple[Any, ...]:
    """Return a deterministic sort key for collection pairing and JSON dumps.

    Unordered collections (``set``/``frozenset``) must serialize and match in
    the same order across workers; plaintext secret values are included so two
    otherwise identical models remain distinguishable before masking.

    :param item: A stored collection element (model, secret wrapper, or scalar).
    :return: A comparable tuple suitable for :func:`sorted`.
    """
    if isinstance(item, BaseModel):
        field_parts: list[tuple[str, str]] = []
        for name in sorted(type(item).model_fields):
            value = getattr(item, name, None)
            unwrapped = _unwrap_secret_value(value)
            if unwrapped is not None:
                rendered = unwrapped if isinstance(unwrapped, str) else repr(unwrapped)
            else:
                rendered = repr(value)
            field_parts.append((name, rendered))
        return (item.__class__.__qualname__, tuple(field_parts))
    unwrapped = _unwrap_secret_value(item)
    if unwrapped is not None:
        return (
            type(item).__qualname__,
            unwrapped if isinstance(unwrapped, str | bytes) else repr(unwrapped),
        )
    if isinstance(item, Mapping):
        return (
            "mapping",
            tuple(
                (
                    str(key),
                    repr(
                        stored
                        if (stored := _unwrap_secret_value(value)) is not None
                        else value
                    ),
                )
                for key, value in sorted(item.items(), key=lambda kv: str(kv[0]))
            ),
        )
    return (type(item).__qualname__, repr(item))


def _field_is_complex(annotation: Any) -> bool:
    """Return whether ``annotation`` is or contains a Pydantic ``BaseModel`` subclass.

    :param annotation: The annotation to inspect.
    :type annotation: Any
    :return: ``True`` when a ``BaseModel`` subclass is reachable from the
        annotation (top-level or inside a union / generic container).
    :rtype: bool
    """
    for arg in _iter_type_arguments(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return True
    return False


def _docstring_summary(description: str | None) -> str | None:
    """Return the first line of a description Pydantic took from a docstring.

    A computed field declared without an explicit ``description`` inherits the
    property's whole docstring, reST field list included. Only the summary line
    is prose an operator reading the settings page can use.

    :param description: The description Pydantic recorded, if any.
    :return: The summary line, or ``None`` when there is no prose.
    """
    if description is None:
        return None
    return description.strip().split("\n", 1)[0].strip() or None


def _computed_field_info(info: ComputedFieldInfo) -> FieldInfo:
    """Return a :class:`FieldInfo` standing in for a computed field.

    A :class:`ComputedFieldInfo` is not a :class:`FieldInfo`, so none of the
    classification helpers below accept one. The stand-in carries the
    property's return type and summary description and nothing else: no
    default, and no marker metadata, which is what makes a computed field
    classify :attr:`ReloadClassification.NOT_OVERRIDABLE` through the same code
    path as an unmarked declared field.

    :param info: The computed-field metadata Pydantic recorded for the property.
    :return: Field metadata describing the same field.
    """
    return FieldInfo(
        annotation=info.return_type, description=_docstring_summary(info.description)
    )


def computed_field_info(settings_cls: type[BaseModel], key: str) -> FieldInfo | None:
    """Return stand-in field metadata for a computed field, or ``None``.

    The companion lookup to ``settings_cls.model_fields.get(key)`` for the call
    sites that must also serve the computed keys :func:`iter_class_fields`
    enumerates.

    :param settings_cls: The Pydantic model class to look the key up on.
    :param key: The top-level key, which may name a computed field.
    :return: Stand-in metadata when ``key`` names a computed field on
        ``settings_cls``, ``None`` otherwise.
    """
    info = settings_cls.model_computed_fields.get(key)
    return None if info is None else _computed_field_info(info)


def iter_class_fields(
    settings_cls: type[BaseYamlSettings],
) -> Iterator[FieldMetadata]:
    """Yield introspected metadata for every field a settings class exposes.

    The key set matches the one ``model_dump()`` produces: every declared field
    except those marked ``exclude=True``, followed by every computed field. A
    field excluded from the dump is deliberately not part of the model's public
    surface, and a computed field is, so the settings API advertises the keys
    the model itself does rather than diverging whenever a class hides a
    settable input behind a computed accessor. A computed field carries no
    marker metadata and no default, so it always classifies
    :attr:`ReloadClassification.NOT_OVERRIDABLE`.

    Each entry exposes the public attributes the settings API needs without
    leaking :class:`FieldInfo` into the response layer: ``key``, ``annotation``,
    ``default``, ``description``, ``reload`` (HOT or NOT_OVERRIDABLE),
    ``is_secret`` (whether the field's type contains a SecretStr anywhere),
    ``is_complex`` (whether the type is a nested Pydantic model) and
    ``is_advanced`` (whether the field, or an advanced ancestor, is marked
    display-only advanced).

    :param settings_cls: The Pydantic settings class to introspect.
    :return: An iterator yielding one :class:`FieldMetadata` per exposed field.
    """
    for name, field in settings_cls.model_fields.items():
        if field.exclude:
            continue
        yield _field_metadata(settings_cls, name, field)
    for name, computed in settings_cls.model_computed_fields.items():
        yield _field_metadata(settings_cls, name, _computed_field_info(computed))


def _field_metadata(
    settings_cls: type[BaseYamlSettings], name: str, field: FieldInfo
) -> FieldMetadata:
    """Return the introspected metadata for one field of a settings class.

    :param settings_cls: The Pydantic settings class owning the field.
    :param name: The field's key on the settings class.
    :param field: The field metadata, declared or synthesised for a computed
        field by :func:`_computed_field_info`.
    :return: The metadata entry the settings API renders.
    """
    return FieldMetadata(
        key=name,
        annotation=field.annotation,
        default=_resolve_default(field),
        description=field.description,
        reload=field_reload_classification(
            field, owner_cls=settings_cls, field_name=name
        ),
        is_secret=_field_contains_secret(field),
        is_complex=_field_is_complex(field.annotation),
        is_advanced=is_advanced_field(field, owner_cls=settings_cls, field_name=name),
    )


def iter_nested_leaf_keys(
    settings_cls: type[BaseModel], parent_field_name: str
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield ``(canonical_key, segment_chain)`` for each nested leaf under a parent.

    Walk the parent submodel's ``model_fields`` recursively, descending into
    Pydantic submodels via :func:`annotation_pydantic_class` (which unwraps
    ``X | None``). A field whose annotation is not a Pydantic model -- a scalar,
    ``list[...]`` or ``set[...]`` -- is a leaf, so collection-typed fields stay a
    single leaf (their items are not expanded). Segments are the canonical
    attribute names from ``model_fields``, so each yielded key matches the form
    :func:`app.core.settings_override.resolution.resolve_nested_field` and
    :func:`app.core.settings_override.resolution.override_provenance_for_rows`
    produce, and ``"__".join(chain) == key`` holds by construction.

    Yield nothing when ``parent_field_name`` is unknown or is not a Pydantic
    submodel (e.g. a scalar HOT field), letting the caller fall back to a single
    top-level entry.

    :param settings_cls: The settings class declaring ``parent_field_name``.
    :param parent_field_name: The top-level field whose leaves to enumerate.
    :return: A ``(canonical_key, segment_chain)`` pair for one nested leaf.
    """
    parent_info = settings_cls.model_fields.get(parent_field_name)
    if parent_info is None:
        return
    submodel = annotation_pydantic_class(parent_info.annotation)
    if submodel is None:
        return
    yield from _iter_leaf_chains(submodel, (parent_field_name,))


def rendered_leaf_keys(
    settings_cls: type[BaseModel], parent_field_name: str
) -> list[tuple[str, tuple[str, ...]]]:
    """Return the nested leaves the settings listing renders under a parent.

    Empty when the field renders as one whole-object row instead: either it
    accepts no nested overrides at all, or it enumerates no leaves to begin with
    (a scalar HOT field), or every leaf it enumerates carries an explicit
    :func:`not_overridable_field` marker. That last case makes the whole object
    the field's only write unit, so expanding it would advertise leaves no PATCH
    can target while hiding the key that one can. Leaves withheld by
    ``SETTINGS_OVERRIDE.ALLOWED_KEYS`` are not that case; they stay
    enumerated, so an admin can see what the allowlist is holding back.

    :param settings_cls: The settings class declaring ``parent_field_name``.
    :param parent_field_name: The top-level field whose leaves to render.
    :return: The leaves to render, empty to render the parent as one row.
    """
    if not is_nested_overridable_parent(
        settings_cls, parent_field_name, include_policy_gate=False
    ):
        return []
    leaves = list(iter_nested_leaf_keys(settings_cls, parent_field_name))
    if all(
        chain_has_explicit_not_overridable(settings_cls, leaf_key)
        for leaf_key, _chain in leaves
    ):
        return []
    return leaves


def _iter_leaf_chains(
    model_cls: type[BaseModel], prefix: tuple[str, ...]
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield ``(key, chain)`` for every leaf reachable from ``model_cls``, recursing into submodels.

    :param model_cls: The Pydantic model whose fields to walk.
    :type model_cls: type[BaseModel]
    :param prefix: The canonical segment chain accumulated from the parent down
        to (but excluding) ``model_cls``'s own fields.
    :type prefix: tuple[str, ...]
    :yield: A ``(key, chain)`` pair for one leaf reachable from ``model_cls``.
    :rtype: Iterator[tuple[str, tuple[str, ...]]]
    """
    for name, info in model_cls.model_fields.items():
        chain = (*prefix, name)
        child = annotation_pydantic_class(info.annotation)
        if child is None:
            yield "__".join(chain), chain
        else:
            yield from _iter_leaf_chains(child, chain)


def _resolve_default(field_info: FieldInfo) -> Any:
    """Return the field's declared default, invoking ``default_factory`` if any.

    Pydantic sets ``field_info.default`` to :data:`PydanticUndefined` when a
    field is declared with ``Field(default_factory=...)``. Returning that
    sentinel through the metadata layer makes ``dump_field_value`` emit
    ``None`` — misrepresenting fields like ``BACKEND_CORS_ORIGINS`` whose
    real default is the factory's return value (e.g. ``[]``). Invoke the
    factory eagerly so the API surfaces the actual default.

    :param field_info: The Pydantic field metadata for the target attribute.
    :return: The resolved default value, or :data:`PydanticUndefined` when
        neither ``default`` nor ``default_factory`` is declared.
    """
    if field_info.default is not PydanticUndefined:
        return field_info.default
    factory = field_info.default_factory
    if factory is None:
        return PydanticUndefined
    return factory()


def dump_field_value(field_info: FieldInfo, value: Any) -> Any:
    """Return a JSON-safe representation of ``value`` for the response model.

    Delegates to ``TypeAdapter(annotated_type(field_info)).dump_python(value, mode='json')``
    so nested Pydantic models, enums, timedeltas, URLs and paths all serialise
    to their canonical JSON shape, including field metadata such as credential-URL
    serializers and constraint annotations. :class:`pydantic.SecretStr` /
    :class:`pydantic.SecretBytes` instances inside the value are automatically
    redacted to ``"**********"`` by Pydantic's secret-aware JSON dump.

    Unordered collections (``set``/``frozenset``) are dumped as a list sorted by
    :func:`_stable_collection_sort_key` so GET order matches the PATCH restore
    path in :func:`app.core.settings_override.secret_preservation._stable_collection_items`
    across workers.

    When ``field_info.annotation`` is a non-Pydantic-compatible type (e.g.
    ``string.Template``) for which Pydantic cannot build a TypeAdapter, the
    helper returns ``None`` rather than ``str(value)``: a default object
    ``repr`` like ``<string.Template object at 0x7f...>`` is unstable
    (memory-address dependent) and useless for a diffing UI. Operators see
    the field's ``key``, ``description``, ``is_complex`` and ``type`` flags
    and know it cannot be edited via the API.

    :param field_info: The Pydantic field metadata for the target attribute.
    :param value: The Python value to serialise.
    :return: A JSON-serialisable representation of ``value``, or ``None`` when
        ``value`` is :data:`pydantic_core.PydanticUndefined` (the field has no
        declared default).
    """
    if value is PydanticUndefined:
        return None
    try:
        if isinstance(value, set | frozenset):
            # Dump each element with its concrete runtime type so polymorphic
            # set members (e.g. PagerDuty under BaseAlertProvider) keep their
            # fields; a list[Base...] adapter would strip subclass attributes.
            return [
                TypeAdapter(type(item)).dump_python(item, mode="json")
                for item in sorted(value, key=_stable_collection_sort_key)
            ]
        return TypeAdapter(annotated_type(field_info)).dump_python(value, mode="json")
    except PydanticSchemaGenerationError:
        return None
