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

"""Derive ``AppSchema`` form objects from a create model's fields and markers.

The functions here read each field's :class:`~pydantic.fields.FieldInfo`
(annotation, default, ``metadata``) and emit the existing
:class:`~app.extensions.apps.framework.schema.BaseField` /
:class:`~app.extensions.apps.framework.schema.FormSection` /
:class:`~app.extensions.apps.framework.schema.AppSchema` objects, so the wire
format is byte-identical to a hand-written schema.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin, Literal, TYPE_CHECKING, Union

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from app.core.utils.pydantic import bound_metadata, flatten_metadata, numeric_bounds
from app.extensions.apps.framework.form_dsl.markers import (
    Choices,
    FieldWidget,
    Forbidden,
    FormLayout,
    FormRules,
    Hidden,
    HostRef,
    RemoteChoices,
    Requires,
    SchemaRef,
    SectionRules,
    ServiceRef,
    TableRef,
    Ui,
)
from app.extensions.apps.framework.rules import FieldGate
from app.extensions.apps.framework.schema import (
    AppSchema,
    BaseField,
    BoolField,
    Choice,
    ChoiceField,
    Column,
    DateTimeField,
    FloatField,
    FormSection,
    HostField,
    IntegerField,
    ListView,
    MultiChoiceField,
    MultiHostField,
    MultiSchemaField,
    MultiServiceField,
    MultiTableField,
    OneOfBranch,
    OneOfGroup,
    RemoteChoiceField,
    SchemaField,
    ServiceField,
    StringField,
    TableField,
    TextAreaField,
    YamlField,
)

if TYPE_CHECKING:
    from app.extensions.apps.framework.form_dsl.model import AppFormModel

__all__ = [
    "build_runtime_schema",
    "derive_app_schema",
    "derive_form_sections",
    "find_ref_marker",
    "iter_service_refs",
    "resolve_base",
]

_REF_TYPES = (ServiceRef, SchemaRef, TableRef, HostRef)
_REF_BRANCH_META: dict[type, tuple[str, str]] = {
    ServiceRef: ("service", "Service"),
    SchemaRef: ("schema", "Schema"),
    TableRef: ("table", "Table"),
    HostRef: ("host", "Host"),
}
_REF_FIELD_CLASSES: dict[type, tuple[type[BaseField], type[BaseField]]] = {
    ServiceRef: (ServiceField, MultiServiceField),
    SchemaRef: (SchemaField, MultiSchemaField),
    TableRef: (TableField, MultiTableField),
    HostRef: (HostField, MultiHostField),
}
_MIN_ONE_OF_BRANCHES = 2
_NONE_TYPE = type(None)
_SIMPLE_SCALAR_FIELDS: dict[type, type[BaseField]] = {
    bool: BoolField,
    datetime: DateTimeField,
}


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    """Pair a derived field with its section placement and declaration order.

    :param base_field: The derived schema field.
    :param section: The layout-section key from :attr:`Ui.section`.
    :param order: The within-section sort key from :attr:`Ui.order`.
    :param index: The declaration index, used as a stable tiebreaker.
    """

    base_field: BaseField | OneOfGroup
    section: str
    order: int
    index: int


def resolve_base(annotation: Any) -> tuple[Any, bool]:
    """Return ``(base, is_list)`` after stripping ``Annotated`` / ``| None`` / list/set.

    ``Annotated`` wrappers, ``None``/``EmptyStrToNone`` union members, and a
    ``list[...]`` / ``set[...]`` container are peeled away so the inference sees
    the underlying scalar (and learns whether the field is a collection).

    :param annotation: The field's resolved annotation.
    :return: The base type (or ``Literal[...]`` / enum class) and whether the
        field is a list or set.
    """
    current = _strip_annotated(annotation)
    if get_origin(current) in (Union, UnionType):
        members = [
            stripped
            for arg in get_args(current)
            if (stripped := _strip_annotated(arg)) is not _NONE_TYPE
        ]
        current = members[0] if members else _NONE_TYPE
    if get_origin(current) in (list, set):
        return _strip_annotated(get_args(current)[0]), True
    return current, False


def _annotation_accepts_str(annotation: Any) -> bool:
    """Return whether ``annotation`` admits ``str`` (directly or in a union)."""
    current = _strip_annotated(annotation)
    if get_origin(current) in (Union, UnionType):
        return any(_strip_annotated(arg) is str for arg in get_args(current))
    return current is str


def _annotation_accepts_none(annotation: Any) -> bool:
    """Return whether ``annotation`` admits ``None`` (directly or in a union)."""
    current = _strip_annotated(annotation)
    if get_origin(current) in (Union, UnionType):
        return any(_strip_annotated(arg) is _NONE_TYPE for arg in get_args(current))
    return current is _NONE_TYPE


def _strip_annotated(annotation: Any) -> Any:
    """Return the underlying type of an ``Annotated[...]``, or ``annotation`` itself."""
    metadata = getattr(annotation, "__metadata__", None)
    return annotation.__args__[0] if metadata is not None else annotation


def _is_enum(annotation: Any) -> bool:
    """Return whether ``annotation`` is an :class:`enum.Enum` subclass."""
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def _is_literal(annotation: Any) -> bool:
    """Return whether ``annotation`` is a ``typing.Literal[...]``."""
    return get_origin(annotation) is Literal


def _field_default(field_info: FieldInfo, ui: Ui) -> Any:
    """Return the field's form-display default.

    Prefer the tri-state :attr:`Ui.default` when it is set (including an explicit
    ``None``); otherwise fall back to the Pydantic field default. Only the derived
    schema field's default is affected — the model's own default, which the JSON
    body validates against, is unchanged.

    :param field_info: The field's Pydantic ``FieldInfo``.
    :param ui: The field's ``Ui`` marker, source of the tri-state form default.
    :return: The form-display default, or ``None`` when the field has none.
    """
    if ui.has_default:
        return ui.default
    default = field_info.get_default(call_default_factory=False)
    return None if default is PydanticUndefined else default


def find_ref_marker(
    metadata: list[Any],
) -> ServiceRef | SchemaRef | TableRef | HostRef | None:
    """Return the single reference marker in ``metadata``, or ``None``.

    Shared with the task-payload resolver so it consumes the same marker
    introspection (``_REF_TYPES`` / :func:`_find_marker`) the schema derivation
    uses, rather than re-scanning ``FieldInfo.metadata`` independently.

    :param metadata: A field's ``FieldInfo.metadata`` list.
    :return: The field's reference marker, or ``None`` when it declares none.
    :raises ValueError: When the field declares more than one reference marker.
    """
    return _find_marker(metadata, _REF_TYPES)


def iter_service_refs(model: type["AppFormModel"]) -> Iterator[ServiceRef]:
    """Yield every ``ServiceRef`` marker declared by ``model``.

    Walk the model's top-level fields and the branch models of any
    discriminated-union (one-of) field, so a service reference nested in a one-of
    branch is surfaced alongside the top-level ones. Used by the framework's
    connectivity-primary construction guard to count and validate the markers.

    :param model: The create model to introspect.
    :yield: Each of the model's ``ServiceRef`` markers, in declaration order
        (top-level fields, then each one-of branch's leaves).
    """
    for field_info in model.model_fields.values():
        if field_info.discriminator is not None:
            for member in _union_model_members(field_info.annotation):
                for leaf_info in member.model_fields.values():
                    ref = find_ref_marker(list(leaf_info.metadata))
                    if isinstance(ref, ServiceRef):
                        yield ref
            continue
        ref = find_ref_marker(list(field_info.metadata))
        if isinstance(ref, ServiceRef):
            yield ref


def _find_marker(metadata: list[Any], marker_types: tuple[type, ...]) -> Any:
    """Return the single marker of ``marker_types`` in ``metadata``, or ``None``.

    :param metadata: A field's ``FieldInfo.metadata`` list.
    :param marker_types: The marker classes to match.
    :return: The matching marker instance, or ``None`` when absent.
    :raises ValueError: When more than one matching marker is present.
    """
    found = [item for item in metadata if isinstance(item, marker_types)]
    if len(found) > 1:
        raise ValueError(
            f"at most one {marker_types[0].__name__}-family marker is allowed per "
            f"field, found {len(found)}"
        )
    return found[0] if found else None


def _field_ui(name: str, metadata: list[Any]) -> Ui:
    """Return the field's :class:`Ui` marker.

    :param name: The field name, used in the error message.
    :param metadata: The field's ``FieldInfo.metadata`` list.
    :return: The field's ``Ui`` marker.
    :raises ValueError: When the field declares no ``Ui`` marker.
    """
    ui = _find_marker(metadata, (Ui,))
    if ui is None:
        raise ValueError(
            f"field {name!r} is missing a Ui(...) marker; every AppFormModel field "
            "must declare Ui(section=...)"
        )
    return ui


def _field_label(name: str, ui: Ui) -> str:
    """Return the field's explicit ``Ui`` label, or derive it from the name.

    A labelless marker derives the conventional shape from the field name:
    underscores become spaces and the result is title-cased. A field declares an
    explicit label only when its display text diverges from that default.

    :param name: The field name used for derivation when ``ui.label`` is unset.
    :param ui: The field's ``Ui`` marker.
    :return: The explicit label, or the derived default when none was given.
    """
    if ui.label is not None:
        return ui.label
    return name.replace("_", " ").title()


def _gates(metadata: list[Any], marker_type: type) -> list[FieldGate]:
    """Return the field gates derived from every ``marker_type`` marker."""
    return [
        FieldGate(when=marker.when, message=marker.message)
        for marker in metadata
        if isinstance(marker, marker_type)
    ]


def _derived_required(field_info: FieldInfo, ui: Ui) -> bool:
    """Return the wire ``required`` flag, honouring a ``Ui`` override.

    :param field_info: The field's Pydantic ``FieldInfo``.
    :param ui: The field's ``Ui`` marker.
    :return: Whether the field is required on the wire.
    """
    return ui.required if ui.required is not None else field_info.is_required()


def _common_field_kwargs(
    name: str, field_info: FieldInfo, ui: Ui, metadata: list[Any]
) -> dict[str, Any]:
    """Return the ``BaseField`` keyword arguments shared by every field kind.

    The model-first derivation builds the base keys only here, so a key added
    to this dict reaches the per-kind builder and the multi-reference one-of
    branch builder in one edit rather than two. Apps that construct schema
    fields directly, without a :class:`Ui` marker, bypass this and supply their
    own.

    :param name: The field name (the wire ``name``).
    :param field_info: The field's Pydantic ``FieldInfo``.
    :param ui: The field's ``Ui`` marker.
    :param metadata: The field's ``FieldInfo.metadata`` list.
    :return: The shared keyword arguments.
    """
    return {
        "name": name,
        "label": _field_label(name, ui),
        "required": _derived_required(field_info, ui),
        "description": ui.description,
        "destructive": ui.destructive,
        "default": _field_default(field_info, ui),
        "requires": _gates(metadata, Requires) or None,
        "forbidden": _gates(metadata, Forbidden) or None,
        "parent": ui.parent,
        "help_placement": ui.help_placement,
    }


def _derive_choices(name: str, base: Any, choices: Choices | None) -> list[Choice]:
    """Return the choice options for a choice field.

    :param name: The field name, used in the error message.
    :param base: The field's base type (scalar, enum class, or ``Literal[...]``).
    :param choices: The explicit :class:`Choices` marker, if any.
    :return: The ordered choice options.
    :raises ValueError: When no options can be derived and none were supplied.
    """
    if choices is not None:
        return [
            Choice(
                value=str(opt.value),
                label=opt.label,
                disabled=True if opt.disabled else None,
                disabled_reason=opt.disabled_reason,
            )
            for opt in choices.options
        ]
    if _is_enum(base):
        return [
            Choice(value=str(member.value), label=str(member.value)) for member in base
        ]
    if _is_literal(base):
        return [Choice(value=str(arg), label=str(arg)) for arg in get_args(base)]
    raise ValueError(
        f"field {name!r} is a choice field but no options could be derived; "
        "annotate it with Choices([...]) (or an Enum / Literal base type)"
    )


def _string_constraints(metadata: list[Any]) -> dict[str, Any]:
    """Return ``min_length`` / ``max_length`` / ``pattern`` from string metadata."""
    metadata = flatten_metadata(metadata)
    min_length = next(
        (
            item.min_length
            for item in metadata
            if isinstance(item, annotated_types.MinLen)
        ),
        None,
    )
    max_length = next(
        (
            item.max_length
            for item in metadata
            if isinstance(item, annotated_types.MaxLen)
        ),
        None,
    )
    pattern = next(
        (
            item.pattern
            for item in metadata
            if getattr(item, "pattern", None) is not None
        ),
        None,
    )
    return {"min_length": min_length, "max_length": max_length, "pattern": pattern}


def _build_ref_field(
    ref: Any, ui: Ui, common: dict[str, Any], *, multiple: bool = False
) -> BaseField:
    """Return the reference field selected by ``ref`` with its extras applied.

    :param ref: The reference marker driving the field class.
    :param ui: The field's ``Ui`` marker, source of a cascade ``depends_on``.
    :param common: The shared ``BaseField`` keyword arguments.
    :param multiple: When ``True``, pick the multi-value field class from the
        marker's ``(single, multi)`` pair. Defaults to ``False``.
    :return: The derived reference field.
    :raises ValueError: When a cascade reference omits ``Ui(depends_on=...)``.
    """
    allow_custom = ref.allow_custom or None
    single_class, multi_class = _REF_FIELD_CLASSES[type(ref)]
    field_class = multi_class if multiple else single_class
    if isinstance(ref, ServiceRef):
        return field_class(
            **common, service_types=list(ref.service_types), allow_custom=allow_custom
        )
    if isinstance(ref, SchemaRef | TableRef):
        if ui.depends_on is None:
            raise ValueError(
                f"field {common['name']!r} uses a cascade reference but omits "
                "Ui(depends_on=...); a SchemaRef / TableRef must name the field "
                "whose value drives its options"
            )
        return field_class(
            **common, depends_on=ui.depends_on, allow_custom=allow_custom
        )
    if isinstance(ref, HostRef):
        # Optional cascade: emit depends_on when Ui declares one; omit (None)
        # otherwise so exclude_none keeps other apps' wire schemas unchanged.
        # MultiHostField may carry the key for wire uniformity, but only the
        # single-value HostField renderer honours it today.
        return field_class(
            **common,
            allow_custom=allow_custom,
            depends_on=ui.depends_on,
            target_service=(
                ref.target_service if ref.target_service is not None else ui.depends_on
            ),
        )
    return field_class(**common, allow_custom=allow_custom)


def _union_model_members(annotation: Any) -> list[type[BaseModel]]:
    """Return the ``BaseModel`` members of a discriminated union annotation."""
    current = _strip_annotated(annotation)
    if get_origin(current) not in (Union, UnionType):
        return []
    members = []
    for member_arg in get_args(current):
        member = _strip_annotated(member_arg)
        if member is _NONE_TYPE:
            continue
        if isinstance(member, type) and issubclass(member, BaseModel):
            members.append(member)
    return members


def _literal_single_value(annotation: Any) -> str | None:
    """Return the sole ``Literal`` value when ``annotation`` is a one-value literal."""
    ann = _strip_annotated(annotation)
    if not _is_literal(ann):
        return None
    args = get_args(ann)
    return str(args[0]) if len(args) == 1 else None


def _branch_label(
    member_model: type[BaseModel], disc_key: str, branch_value: str
) -> str:
    """Return the segmented-control label for one union branch."""
    disc_info = member_model.model_fields.get(disc_key)
    if disc_info is not None:
        choices = _find_marker(list(disc_info.metadata), (Choices,))
        if choices is not None:
            for opt in choices.options:
                if str(opt.value) == branch_value:
                    return opt.label
    return branch_value.replace("_", " ").title()


def _discriminator_default_value(
    field_info: FieldInfo,
    ui: Ui,
    disc_key: str,
    members: list[type[BaseModel]],
) -> str | None:
    """Return the default branch value for a derived one-of group."""
    default = _field_default(field_info, ui)
    if default is not None:
        disc_val = getattr(default, disc_key, None)
        if disc_val is not None:
            return str(disc_val)
    for member in members:
        disc_info = member.model_fields.get(disc_key)
        if disc_info is None:
            continue
        value = _literal_single_value(disc_info.annotation)
        if value is not None:
            return value
    return None


def _derive_branch_leaves(
    member_model: type[BaseModel],
    prefix: str,
    disc_key: str,
) -> list[BaseField]:
    """Derive prefixed leaf fields for one union branch model."""
    leaves = []
    for leaf_name, leaf_info in member_model.model_fields.items():
        if leaf_name == disc_key:
            continue
        wire_name = f"{prefix}.{leaf_name}"
        leaf_metadata = list(leaf_info.metadata)
        leaf_ui = _find_marker(leaf_metadata, (Ui,))
        if leaf_ui is None:
            raise ValueError(
                f"field {wire_name!r} is missing a Ui(...) marker; every branch "
                "model field must declare Ui(section=...)"
            )
        leaves.append(_build_base_field(wire_name, leaf_info, leaf_ui, leaf_metadata))
    if not leaves:
        raise ValueError(
            f"one_of branch model {member_model.__name__!r} has no derivable leaf "
            f"fields besides discriminator {disc_key!r}"
        )
    return leaves


def _derive_one_of_from_union(
    name: str,
    field_info: FieldInfo,
    ui: Ui,
) -> OneOfGroup:
    """Derive a :class:`OneOfGroup` from a nested discriminated union field.

    :param name: The field name (the wire ``name``).
    :param field_info: The field's Pydantic ``FieldInfo``.
    :param ui: The field's ``Ui`` marker.
    :return: The derived one-of group.
    :raises ValueError: When the field declares no discriminator key, the union
        has fewer than two branch models, a branch model omits the
        discriminator or gives it no single value, a branch model field is
        missing its ``Ui(...)`` marker, a branch model has no derivable leaf
        fields besides the discriminator, or the field carries
        ``Ui(destructive=...)``.
    """
    disc_key = field_info.discriminator
    if not disc_key:
        raise ValueError(f"field {name!r} has no discriminator key")
    if ui.destructive is not None:
        raise ValueError(
            f"field {name!r} sets Ui(destructive=...) on a discriminated union, "
            "which derives a one-of group rather than a field; the group cannot "
            "carry the mark and the branch leaves take their own Ui, so mark the "
            "destructive leaf inside each branch model instead"
        )
    members = _union_model_members(field_info.annotation)
    if len(members) < _MIN_ONE_OF_BRANCHES:
        raise ValueError(
            f"field {name!r} declares a discriminated union with {len(members)} "
            "branch model(s); one_of requires at least two branches"
        )
    branches = []
    for member in members:
        disc_info = member.model_fields.get(disc_key)
        if disc_info is None:
            raise ValueError(
                f"one_of branch model {member.__name__!r} is missing discriminator "
                f"field {disc_key!r}"
            )
        branch_value = _literal_single_value(disc_info.annotation)
        if branch_value is None:
            default = disc_info.get_default(call_default_factory=False)
            branch_value = str(default) if default is not PydanticUndefined else None
        if branch_value is None:
            raise ValueError(
                f"one_of branch model {member.__name__!r} discriminator {disc_key!r} "
                "must be a single-value Literal or carry a default"
            )
        branches.append(
            OneOfBranch(
                value=branch_value,
                label=_branch_label(member, disc_key, branch_value),
                fields=_derive_branch_leaves(member, name, disc_key),
            )
        )
    return OneOfGroup(
        name=name,
        label=_field_label(name, ui),
        description=ui.description,
        discriminator=f"{name}.{disc_key}",
        default=_discriminator_default_value(field_info, ui, disc_key, members),
        branches=branches,
    )


def _derive_multi_ref_one_of(
    name: str,
    ref_markers: list[Any],
    field_info: FieldInfo,
    ui: Ui,
    metadata: list[Any],
) -> OneOfGroup:
    """Derive a :class:`OneOfGroup` when multiple reference markers share one field."""
    if any(ref.multiple for ref in ref_markers):
        raise ValueError(
            f"field {name!r} declares multiple reference markers with multiple=True; "
            "multi-value one-of reference unions are not supported — use a single "
            "reference marker per field for multi-value selection"
        )
    common = _common_field_kwargs(name, field_info, ui, metadata)
    branches = []
    for ref in ref_markers:
        ref_type = type(ref)
        branch_meta = _REF_BRANCH_META.get(ref_type)
        if branch_meta is None:
            raise ValueError(
                f"field {name!r} declares unsupported reference marker "
                f"{ref_type.__name__}"
            )
        value, branch_label = branch_meta
        if ref.allow_custom and not _annotation_accepts_str(field_info.annotation):
            raise ValueError(
                f"field {name!r} sets allow_custom=True but its annotation does not "
                "accept str; widen it to include str (e.g. int | str) so the model "
                "accepts the free-typed value the schema advertises"
            )
        branches.append(
            OneOfBranch(
                value=value,
                label=branch_label,
                fields=[_build_ref_field(ref, ui, common)],
            )
        )
    return OneOfGroup(
        name=name,
        label=_field_label(name, ui),
        description=ui.description,
        discriminator=f"{name}_mode",
        default=branches[0].value,
        branches=branches,
    )


def _derive_section_field(
    name: str, field_info: FieldInfo, ui: Ui, metadata: list[Any]
) -> BaseField | OneOfGroup:
    """Return the schema section item derived from one model field."""
    if field_info.discriminator is not None:
        return _derive_one_of_from_union(name, field_info, ui)

    ref_markers = [item for item in metadata if isinstance(item, _REF_TYPES)]
    if len(ref_markers) > 1:
        return _derive_multi_ref_one_of(name, ref_markers, field_info, ui, metadata)

    return _build_base_field(name, field_info, ui, metadata)


def _validate_remote_choice_annotation(
    name: str, annotation: Any, *, required: bool
) -> None:
    """Reject a ``RemoteChoices`` annotation that cannot hold what the selector commits.

    :param name: The field name, used in the error message.
    :param annotation: The field's resolved annotation.
    :param required: Whether the derived field is required.
    :raises ValueError: When the annotation does not accept ``str``, or the
        field is optional and the annotation does not accept ``None``.
    """
    if not _annotation_accepts_str(annotation):
        raise ValueError(
            f"field {name!r} uses RemoteChoices but its annotation does not "
            "accept str; a set value (a fetched option value or a free-typed "
            "custom value) reaches the model as a string"
        )
    if not required and not _annotation_accepts_none(annotation):
        raise ValueError(
            f"field {name!r} uses RemoteChoices and is optional, but its "
            "annotation does not accept None; the selector commits null when "
            "the field is cleared or its cascade parent changes, so widen it "
            "(e.g. str | None = None) or declare the field required"
        )


def _build_base_field(
    name: str, field_info: FieldInfo, ui: Ui, metadata: list[Any]
) -> BaseField:
    """Return the schema field derived from one model field.

    :param name: The field name (the wire ``name``).
    :param field_info: The field's Pydantic ``FieldInfo``.
    :param ui: The field's ``Ui`` marker.
    :param metadata: The field's ``FieldInfo.metadata`` list.
    :return: The derived field.
    :raises ValueError: When the base type maps to no known field kind, a
        choice field supplies no derivable options, or a ``RemoteChoices`` field
        annotation does not accept ``str`` (or, when the field is optional,
        ``None``).
    """
    required = _derived_required(field_info, ui)
    common = _common_field_kwargs(name, field_info, ui, metadata)

    ref_markers = [item for item in metadata if isinstance(item, _REF_TYPES)]
    if ref_markers:
        ref = ref_markers[0]
        base, is_list = resolve_base(field_info.annotation)
        if ref.multiple and not is_list:
            raise ValueError(
                f"field {name!r} sets multiple=True on its reference marker but its "
                "annotation is not a list/set; a multi-value reference must back a "
                "list[...] or set[...] type so the model accepts the submitted values"
            )
        if is_list and not ref.multiple:
            raise ValueError(
                f"field {name!r} has a list/set annotation but its reference marker "
                "does not set multiple=True; set multiple=True to derive a "
                "multi-value reference field, or use a scalar annotation for a "
                "single-value one"
            )
        allow_custom_target = base if ref.multiple else field_info.annotation
        if ref.allow_custom and not _annotation_accepts_str(allow_custom_target):
            raise ValueError(
                f"field {name!r} sets allow_custom=True but its annotation does not "
                "accept str; widen it to include str (e.g. int | str) so the model "
                "accepts the free-typed value the schema advertises"
            )
        return _build_ref_field(ref, ui, common, multiple=ref.multiple)

    remote = _find_marker(metadata, (RemoteChoices,))
    if remote is not None:
        _validate_remote_choice_annotation(
            name, field_info.annotation, required=required
        )
        return RemoteChoiceField(
            **common,
            endpoint_url=remote.endpoint,
            depends_on=ui.depends_on,
            allow_custom=remote.allow_custom or None,
        )

    base, is_list = resolve_base(field_info.annotation)
    choices = _find_marker(metadata, (Choices,))
    if ui.widget is FieldWidget.MULTI_CHOICE and not is_list:
        raise ValueError(
            f"field {name!r} uses widget=MULTI_CHOICE but its annotation is not a "
            "list; a multi-choice field must back a list type so the model accepts "
            "the submitted list of values"
        )
    if _wants_choice(choices, ui.widget, base):
        options = _derive_choices(name, base, choices)
        multi = is_list or ui.widget is FieldWidget.MULTI_CHOICE
        field_class = MultiChoiceField if multi else ChoiceField
        return field_class(**common, choices=options)

    if is_list:
        raise ValueError(
            f"field {name!r} is a list field but no multi-choice options could be "
            "derived; annotate it with Choices([...]) or use an Enum / Literal "
            "element type"
        )
    return _build_scalar_field(
        name, base, ui.widget, common, bound_metadata(field_info)
    )


def _wants_choice(
    choices: Choices | None, widget: FieldWidget | None, base: Any
) -> bool:
    """Return whether a field should derive a (multi-)choice from its base/markers."""
    return (
        choices is not None
        or widget in (FieldWidget.CHOICE, FieldWidget.MULTI_CHOICE)
        or _is_enum(base)
        or _is_literal(base)
    )


def _build_scalar_field(
    name: str,
    base: Any,
    widget: FieldWidget | None,
    common: dict[str, Any],
    metadata: list[Any],
) -> BaseField:
    """Return the non-reference, non-choice field for ``base`` / ``widget``.

    :param name: The field name, used in the error message.
    :param base: The unwrapped base type.
    :param widget: The optional widget override.
    :param common: The shared ``BaseField`` keyword arguments.
    :param metadata: The field's bound-carrying metadata (from
        :func:`~app.core.utils.pydantic.bound_metadata`): the outer ``FieldInfo.metadata`` plus any union
        members' metadata, for numeric bounds and string constraints.
    :return: The derived scalar field.
    :raises ValueError: When ``base`` maps to no known field kind.
    """
    if widget is FieldWidget.TEXTAREA:
        return TextAreaField(**common)
    if widget is FieldWidget.YAML:
        return YamlField(**common)
    if base is int:
        return IntegerField(**common, **numeric_bounds(metadata))
    if base is float:
        return FloatField(**common, **numeric_bounds(metadata))
    if base is str:
        return StringField(**common, **_string_constraints(metadata))
    field_class = _SIMPLE_SCALAR_FIELDS.get(base)
    if field_class is None:
        raise ValueError(
            f"field {name!r}: cannot infer a form field kind from base type "
            f"{base!r}; use a Ref marker, an Enum / Literal, Choices([...]), or a "
            "Ui(widget=...) override"
        )
    return field_class(**common)


def _derive_field_specs(model: type["AppFormModel"]) -> list[_FieldSpec]:
    """Return one :class:`_FieldSpec` per model field, in declaration order.

    Skip a field carrying a :class:`Hidden` marker before the ``Ui`` requirement
    applies: it is omitted from the derived schema (the framework renders it from a
    capability instead) and so needs no ``Ui`` presentation metadata.
    """
    consumed_mode_fields = set()
    for name, field_info in model.model_fields.items():
        metadata = list(field_info.metadata)
        ref_markers = [item for item in metadata if isinstance(item, _REF_TYPES)]
        if len(ref_markers) > 1:
            consumed_mode_fields.add(f"{name}_mode")

    specs = []
    for index, (name, field_info) in enumerate(model.model_fields.items()):
        if name in consumed_mode_fields:
            continue
        metadata = list(field_info.metadata)
        if _find_marker(metadata, (Hidden,)) is not None:
            continue
        ui = _field_ui(name, metadata)
        specs.append(
            _FieldSpec(
                base_field=_derive_section_field(name, field_info, ui, metadata),
                section=ui.section,
                order=ui.order,
                index=index,
            )
        )
    return specs


def _runtime_form_fields(model: type["AppFormModel"]) -> list[BaseField | OneOfGroup]:
    """Return runtime rule-plan fields, using one-of groups where required.

    Gate-only :class:`StringField` projections are enough for ordinary leaves.
    Discriminated unions and multi-reference fields must surface as
    :class:`OneOfGroup` containers so branch-selection rules are synthesised.
    """
    consumed_mode_fields = set()
    for name, field_info in model.model_fields.items():
        metadata = list(field_info.metadata)
        ref_markers = [item for item in metadata if isinstance(item, _REF_TYPES)]
        if len(ref_markers) > 1:
            consumed_mode_fields.add(f"{name}_mode")

    fields = []
    for name, field_info in model.model_fields.items():
        if name in consumed_mode_fields:
            continue
        metadata = list(field_info.metadata)
        if field_info.discriminator is not None:
            ui = _field_ui(name, metadata)
            fields.append(_derive_one_of_from_union(name, field_info, ui))
            continue
        ref_markers = [item for item in metadata if isinstance(item, _REF_TYPES)]
        if len(ref_markers) > 1:
            ui = _field_ui(name, metadata)
            fields.append(
                _derive_multi_ref_one_of(name, ref_markers, field_info, ui, metadata)
            )
            continue
        fields.append(
            StringField(
                name=name,
                label=name,
                requires=_gates(metadata, Requires) or None,
                forbidden=_gates(metadata, Forbidden) or None,
            )
        )
    return fields


def _validate_parent_pointers(sections: list[FormSection]) -> None:
    """Reject ``Ui(parent=...)`` pointers that the renderer could not honour.

    ``Ui`` is presentation-only, so a pointer changes nothing about what the
    server accepts: it says where the renderer draws the field and when to grey
    it out, and nothing more. What it can still get wrong is naming a target the
    renderer cannot nest under — a field in another section, one that is not a
    toggle, or one that is itself nested — and none of those is visible in a
    wire snapshot, so they are caught here, where every migrated app derives its
    schema.

    A field may still carry its own ``Forbidden`` gate on the same parent when
    the combination is genuinely invalid, but that is a validation decision made
    per field, not something the pointer implies.

    :param sections: The derived form sections.
    :raises ValueError: When a pointer names a field outside its own section, a
        field that is not a bool, or a field that is itself parented.
    """
    for section in sections:
        bools = {item.name for item in section.fields if isinstance(item, BoolField)}
        parented = {
            item.name
            for item in section.fields
            if isinstance(item, BaseField) and item.parent is not None
        }
        leaves: list[BaseField] = []
        for item in section.fields:
            if isinstance(item, BaseField):
                leaves.append(item)
                continue
            leaves.extend(leaf for branch in item.branches for leaf in branch.fields)
        for leaf in leaves:
            parent = leaf.parent
            if parent is None:
                continue
            if parent not in bools:
                raise ValueError(
                    f"field {leaf.name!r} sets Ui(parent={parent!r}), which is not "
                    f"a bool field declared directly in section {section.title!r} "
                    f"(bools there: {sorted(bools)}). A parent toggle has to be a "
                    "bool the renderer can nest this field under, declared beside "
                    "it at the top level of its own section."
                )
            if parent in parented:
                raise ValueError(
                    f"field {leaf.name!r} sets Ui(parent={parent!r}), which is "
                    "itself parented. Chained parents are not supported, and a "
                    "cycle would leave both toggles permanently inert."
                )


def derive_form_sections(
    model: type["AppFormModel"], layout: FormLayout
) -> list[FormSection]:
    """Return the form sections derived from ``model``, ordered by ``model``.

    Sections appear in the order each section's first field is declared on the
    model; the matching ``layout`` entry supplies a section's title and non-order
    metadata (``collapsible``, ``forbidden``, ...) by ``key``, but the layout's
    tuple order is not authoritative. Fields are grouped by :attr:`Ui.section` and
    ordered within a section by :attr:`Ui.order` then declaration order.
    Section-scoped rules from the model's ``__form_rules__`` attach to the matching
    section; a layout section's ``forbidden`` gates copy onto
    :attr:`~app.extensions.apps.framework.schema.FormSection.forbidden`.

    :param model: The create model carrying the field markers.
    :param layout: The section layout supplying each section's title and metadata.
    :return: The derived form sections in field-declaration order.
    :raises ValueError: When a field names a section absent from ``layout``, a
        layout section has no fields, or a ``Ui(parent=...)`` pointer names
        a target the renderer could not nest under (see
        :func:`_validate_parent_pointers`).
    """
    specs = _derive_field_specs(model)
    layout_by_key = {section.key: section for section in layout.sections}
    for spec in specs:
        if spec.section not in layout_by_key:
            raise ValueError(
                f"field {spec.base_field.name!r} names section {spec.section!r}, "
                f"which is absent from the form layout (known: {sorted(layout_by_key)})"
            )

    section_order = list(dict.fromkeys(spec.section for spec in specs))
    claimed_sections = set(section_order)
    for section_layout in layout.sections:
        if section_layout.key not in claimed_sections:
            raise ValueError(
                f"layout section {section_layout.key!r} has no fields; remove it or "
                "assign a field to it"
            )

    rules = getattr(model, "__form_rules__", FormRules())
    sections = []
    for section_key in section_order:
        section_layout = layout_by_key[section_key]
        members = sorted(
            (spec for spec in specs if spec.section == section_key),
            key=lambda spec: (spec.order, spec.index),
        )
        section_rules = rules.sections.get(section_key, SectionRules())
        sections.append(
            FormSection(
                title=section_layout.title,
                description=section_layout.description,
                fields=[spec.base_field for spec in members],
                advanced=section_layout.advanced,
                collapsible=section_layout.collapsible,
                collapsed_by_default=section_layout.collapsed_by_default,
                render_after_submit=section_layout.render_after_submit,
                forbidden=list(section_layout.forbidden)
                if section_layout.forbidden
                else None,
                fail_when=list(section_rules.fail_when) or None,
                cardinality_rules=list(section_rules.cardinality_rules) or None,
            )
        )
    _validate_parent_pointers(sections)
    return sections


def derive_app_schema(
    model: type["AppFormModel"],
    layout: FormLayout,
    *,
    name: str,
    display_name: str,
    item_display_name: str | None = None,
    item_display_name_plural: str | None = None,
    description: str | None = None,
    task_type: str | None = None,
    capabilities: Any = None,
    list_view: Any = None,
    detail_view: Any = None,
    derived: Any = None,
    predecessors: Any = None,
    related_apps: Any = None,
) -> AppSchema:
    """Assemble the full ``AppSchema`` for a model-first plugin.

    Derives the form sections from ``model`` and ``layout`` and the plugin-level
    rules from the model's ``__form_rules__``; the non-form metadata
    (``list_view``, ``capabilities``, ``detail_view``, cascade specs) is supplied
    by the caller because it is not derivable from the create model.

    :param model: The create model carrying the field markers.
    :param layout: The section layout for the create form.
    :param name: The plugin identifier.
    :param display_name: The human-readable plugin title.
    :param item_display_name: Optional name for one record the create form
        produces. Passed through as-is, so ``None`` leaves
        :class:`~app.extensions.apps.framework.schema.AppSchema` to default it from
        ``display_name``. Defaults to ``None``.
    :param item_display_name_plural: Optional name for several such records,
        defaulted by the same route. Defaults to ``None``.
    :param description: Optional plugin description. Defaults to ``None``.
    :param task_type: Optional task-type identifier. Defaults to ``None``.
    :param capabilities: Optional plugin capabilities. Defaults to ``None``.
    :param list_view: The list-view configuration (required for task-style
        plugins). Defaults to ``None``.
    :param detail_view: Optional detail-page layout. Defaults to ``None``.
    :param derived: Optional derived-task specs. Defaults to ``None``.
    :param predecessors: Optional predecessor specs. Defaults to ``None``.
    :param related_apps: Optional related-app specs for sibling UI tabs.
        Defaults to ``None``.
    :return: The fully-assembled, validated plugin schema.
    """
    rules = getattr(model, "__form_rules__", FormRules())
    return AppSchema(
        name=name,
        display_name=display_name,
        item_display_name=item_display_name,
        item_display_name_plural=item_display_name_plural,
        description=description,
        task_type=task_type,
        forms=derive_form_sections(model, layout),
        capabilities=capabilities,
        list_view=list_view,
        detail_view=detail_view,
        derived=derived,
        predecessors=predecessors,
        related_apps=related_apps,
        fail_when=list(rules.fail_when) or None,
        cardinality_rules=list(rules.cardinality_rules) or None,
    )


def build_runtime_schema(model: type["AppFormModel"]) -> AppSchema:
    """Return a single-section ``AppSchema`` for runtime rule-plan extraction.

    Section-scoped and plugin-scoped rules are hoisted to plugin scope because
    the runtime rule plan is flat — rule placement is irrelevant to evaluation,
    only to the wire layout (which :func:`derive_form_sections` handles).

    :param model: The create model carrying the field markers and rules.
    :return: A schema whose single form section holds runtime rule-plan fields.
    """
    fields = _runtime_form_fields(model)
    rules = getattr(model, "__form_rules__", FormRules())
    fail_when = list(rules.fail_when)
    cardinality = list(rules.cardinality_rules)
    for section_rules in rules.sections.values():
        fail_when.extend(section_rules.fail_when)
        cardinality.extend(section_rules.cardinality_rules)
    return AppSchema(
        name="app_form_model_runtime",
        display_name="app_form_model_runtime",
        item_display_name="app_form_model_runtime",
        item_display_name_plural="app_form_model_runtime",
        forms=[FormSection(title="rules", fields=fields)],
        list_view=ListView(columns=[Column(key="name", label="Name")]),
        fail_when=fail_when or None,
        cardinality_rules=cardinality or None,
    )
