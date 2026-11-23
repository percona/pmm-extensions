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

"""Cross-check a not-yet-migrated plugin's create model against its hand-written schema.

This is a transitional, warning-level drift detector for plugins that still
declare their form fields twice (a Pydantic create model and a hand-written
:class:`~app.extensions.apps.framework.schema.AppSchema`). It compares the
cheaply-derivable surface — field presence, scalar field kind, required-ness, and
default — and never blocks. A silent (empty) result means "no presence, kind, or
required disagreement", **not** "wire-identical": choice/reference/widget kinds,
constraints, gates, and section metadata are out of its scope and remain the
golden byte-compare's responsibility.
"""

from datetime import datetime
from enum import Enum
from typing import Any, get_origin, Literal, TYPE_CHECKING

from pydantic_core import PydanticUndefined

from app.extensions.apps.framework.form_dsl.derivation import resolve_base
from app.extensions.apps.framework.schema import AnyField, AppSchema

if TYPE_CHECKING:
    from app.extensions.apps.framework.form_dsl.model import AppFormModel

__all__ = ["check_form_conformance"]

_COMPARABLE_KINDS = frozenset({"bool", "integer", "float", "string", "datetime"})
_SCALAR_KINDS: dict[type, str] = {
    bool: "bool",
    int: "integer",
    float: "float",
    str: "string",
    datetime: "datetime",
}


def _natural_kind(annotation: Any) -> str | None:
    """Return the field kind a raw model annotation maps to, or ``None``.

    Only the unambiguous scalar kinds are returned; reference and choice kinds
    are not inferable from a bare model annotation (any scalar can be rendered as
    a choice or a reference selector), so those map to ``None`` and are skipped by
    the kind comparison.

    :param annotation: A model field's annotation.
    :return: The natural field-kind string, or ``None`` when not inferable.
    """
    base, is_list = resolve_base(annotation)
    if is_list:
        return "multi_choice"
    if (isinstance(base, type) and issubclass(base, Enum)) or get_origin(
        base
    ) is Literal:
        return "choice"
    return _SCALAR_KINDS.get(base)


def _schema_form_fields(
    schema: AppSchema, entity_name: str | None
) -> dict[str, AnyField]:
    """Return the schema's create-form fields keyed by name.

    :param schema: The hand-written plugin schema.
    :param entity_name: The entity segment when ``schema`` is multi-entity; must
        be ``None`` for task-style schemas.
    :return: The form fields keyed by field name.
    :raises ValueError: When ``entity_name`` is incompatible with ``schema``.
    """
    if schema.entities:
        if entity_name is None:
            raise ValueError(
                "entity_name is required when checking a multi-entity schema"
            )
        entity = next((e for e in schema.entities if e.name == entity_name), None)
        if entity is None:
            known = ", ".join(sorted(e.name for e in schema.entities))
            raise ValueError(f"unknown entity {entity_name!r}; known: {known}")
        forms = entity.forms
    else:
        forms = schema.forms
    return {field.name: field for section in forms for field in section.fields}


def check_form_conformance(
    model: type[AppFormModel], schema: AppSchema, *, entity_name: str | None = None
) -> list[str]:
    """Return human-readable disagreements between ``model`` and ``schema``.

    Compares field presence, scalar field kind, required-ness, and default
    between a create ``model`` and its hand-written ``schema`` form. The result
    is a non-authoritative drift report: an empty list means no presence/kind/
    required disagreement was found, not that the two are wire-identical.

    :param model: The Pydantic create model to cross-check.
    :param schema: The hand-written plugin schema.
    :param entity_name: The entity segment for multi-entity schemas; ``None`` for
        task-style schemas.
    :return: One message per detected disagreement; empty when none are found.
    :raises ValueError: When ``entity_name`` is incompatible with ``schema``.
    """
    schema_fields = _schema_form_fields(schema, entity_name)
    model_fields = model.model_fields
    model_names = set(model_fields)
    schema_names = set(schema_fields)

    warnings = [
        f"field {name!r} is on the model but absent from the schema form"
        for name in sorted(model_names - schema_names)
    ]
    warnings.extend(
        f"field {name!r} is in the schema form but absent from the model"
        for name in sorted(schema_names - model_names)
    )

    for name in sorted(model_names & schema_names):
        field_info = model_fields[name]
        schema_field = schema_fields[name]

        model_kind = _natural_kind(field_info.annotation)
        if (
            model_kind in _COMPARABLE_KINDS
            and schema_field.field_type in _COMPARABLE_KINDS
            and model_kind != schema_field.field_type
        ):
            warnings.append(
                f"field {name!r} kind disagrees: model {model_kind!r} vs "
                f"schema {schema_field.field_type!r}"
            )

        if field_info.is_required() != schema_field.required:
            warnings.append(
                f"field {name!r} required disagrees: model "
                f"{field_info.is_required()} vs schema {schema_field.required}"
            )

        model_default = field_info.get_default(call_default_factory=True)
        if (
            model_default is not PydanticUndefined
            and model_default != schema_field.default
        ):
            warnings.append(
                f"field {name!r} default disagrees: model {model_default!r} vs "
                f"schema {schema_field.default!r}"
            )

    return warnings
