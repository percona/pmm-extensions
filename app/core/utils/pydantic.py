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

"""Define Pydantic-related utilities."""

__all__ = [
    "CustomFieldMetadata",
    "annotation_pydantic_class",
    "blank_str_values_to_none",
    "extract_model_from_instance",
    "loc_to_dot_sep",
    "run_pydantic_type_validator",
]

from collections.abc import Callable, Hashable
from contextlib import suppress
from functools import cache
from types import EllipsisType, UnionType
from typing import (
    Any,
    get_args,
    get_origin,
    Literal,
    NamedTuple,
    overload,
    TypeAlias,
    TypeVar,
    Union,
)

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import Field, FieldInfo
from pydantic_core import PydanticUndefined

V = TypeVar("V")
T = TypeVar("T", bound=BaseModel)

#: Anything Pydantic accepts as a type: a class, a parameterized generic
#: (``set[int]``), or an ``Annotated`` alias. Only the first is a
#: ``type`` instance, so the narrower ``type[V]`` would exclude real inputs.
_TypeExpression: TypeAlias = Any


class CustomFieldMetadata(NamedTuple):
    """Represent custom metadata for a Pydantic field.

    :param key: The key of the metadata.
    :type key: Hashable
    :param value: The value of the metadata.
    :type value: Any
    """

    key: Hashable
    value: Any

    @classmethod
    def from_dict(cls, data: dict[Any, Any]) -> list["CustomFieldMetadata"]:
        """Create a list of CustomFieldMetadata instances from a dictionary.

        :param data: A dictionary containing key-value pairs to be converted into
            CustomFieldMetadata instances.
        :type data: dict[Any, Any]
        :return: A list of CustomFieldMetadata instances.
        :rtype: list[CustomFieldMetadata]
        """
        return [cls(key=key, value=value) for key, value in data.items()]

    @classmethod
    def from_field(
        cls, field: FieldInfo, *, strict: bool = False
    ) -> list["CustomFieldMetadata"]:
        """Extract custom metadata from a Pydantic field.

        :param field: The Pydantic field from which to extract metadata.
        :type field: FieldInfo
        :param strict: Whether to perform strict type checking. If `True`, only items
            that are instances of the class will be returned. If `False`, items that can
            be coerced to the class will also be included. Defaults to `False`.
        :type strict: bool
        """
        if strict:
            return [item for item in field.metadata if isinstance(item, cls)]
        metadata = []
        for item in field.metadata:
            with suppress(ValidationError):
                metadata.append(run_pydantic_type_validator(cls, item))
        return metadata

    @classmethod
    def field_to_dict(cls, field: FieldInfo, *, strict: bool = False) -> dict[Any, Any]:
        """Convert the custom metadata of a Pydantic field to a dictionary.

        :param field: The Pydantic field from which to extract metadata.
        :type field: FieldInfo
        :param strict: Whether to perform strict type checking. If `True`, only items
            that are instances of the class will be returned. If `False`, items that can
            be coerced to the class will also be included. Defaults to `False`.
        :type strict: bool
        :return: A dictionary containing the key-value pairs from the metadata.
        :rtype: dict[Any, Any]
        """
        return cls.to_dict(*cls.from_field(field, strict=strict))

    @staticmethod
    def to_dict(*metadata: "CustomFieldMetadata") -> dict[Any, Any]:
        """Convert a list of CustomFieldMetadata instances to a dictionary.

        :param metadata: A variable number of CustomFieldMetadata instances.
        :type metadata: CustomFieldMetadata
        :return: A dictionary containing the key-value pairs from the metadata
            instances.
        :rtype: dict[Any, Any]
        """
        return {meta.key: meta.value for meta in metadata}


@overload
def field_with_metadata(
    default: EllipsisType,
    *,
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> Any: ...
@overload
def field_with_metadata(
    default: Any,
    *,
    validate_default: Literal[True],
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> Any: ...
@overload
def field_with_metadata(
    default: V,
    *,
    validate_default: Literal[False] = ...,
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> V: ...
@overload
def field_with_metadata(
    *,
    default_factory: Callable[[], Any] | Callable[[dict[str, Any]], Any],
    validate_default: Literal[True],
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> Any: ...
@overload
def field_with_metadata(
    *,
    default_factory: Callable[[], V] | Callable[[dict[str, Any]], V],
    validate_default: Literal[False] | None = ...,
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> V: ...
@overload
def field_with_metadata(
    *,
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> Any: ...
def field_with_metadata(
    default: Any = PydanticUndefined,
    *,
    metadata: dict[Any, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Create a Pydantic Field with custom metadata.

    The overloads mirror pydantic's own ``Field``: an explicit default (or a
    factory's product) is returned as its own type, so a type checker compares it
    with the field's annotation, while ``...``, ``validate_default=True`` and an
    omitted default return ``Any``. At runtime the result is always a
    :class:`~pydantic.fields.FieldInfo`.

    :param default: The field's default value, forwarded to ``Field``. Omitted, the
        field is required.
    :type default: Any
    :param metadata: A dictionary containing key-value pairs to be added as custom
        metadata to the field. Defaults to ``None``.
    :type metadata: dict[Any, Any] | None
    :param kwargs: Keyword arguments to pass to the Field constructor.
    :type kwargs: Any
    :return: A Pydantic Field with the specified metadata.
    :rtype: FieldInfo
    """
    field = Field(default, **kwargs)
    field.metadata.extend(CustomFieldMetadata.from_dict(metadata or {}))
    return field


def annotation_pydantic_class(annotation: Any) -> type[BaseModel] | None:
    """Return the Pydantic ``BaseModel`` subclass referenced by ``annotation``, if any.

    Unwraps ``X | None`` / ``Optional[X]`` and returns the first ``BaseModel``
    subclass found in the type arguments. Returns ``None`` when the annotation
    is a primitive, a generic container (``list[X]``, ``dict[K, V]``), or any
    other non-BaseModel type -- those are not traversable by the nested-override
    mechanism.

    :param annotation: The Pydantic field annotation to inspect.
    :type annotation: Any
    :return: The referenced ``BaseModel`` subclass, or ``None``.
    :rtype: type[BaseModel] | None
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


@cache
def _type_adapter(validate_class: _TypeExpression) -> TypeAdapter[Any]:
    """Return a memoized ``TypeAdapter`` for ``validate_class``.

    Building the adapter, not validating with it, dominates the cost on the
    paths that validate once per item, so each distinct type builds its adapter
    once and every later call reuses it. ``validate_class`` is the cache key, so
    every type reaching this function must stay hashable.

    ``cache`` holds no lock across the build, so threads missing on the same
    cold key each build an adapter and one wins the entry; the duplicates are
    equivalent and discarded, costing a spare build at warm-up and nothing
    after.

    :param validate_class: The type expression to build the adapter for.
    :return: The memoized adapter for ``validate_class``.
    """
    return TypeAdapter(validate_class)


def _adapter_for(validate_class: _TypeExpression) -> TypeAdapter[Any]:
    """Return the memoized adapter for ``validate_class``, or a fresh one.

    An unhashable type expression cannot key the cache, so it bypasses it and
    pays a rebuild per call rather than failing. Hashability is probed directly
    instead of catching ``TypeError`` from the cached call, which would also
    swallow the ``TypeError`` an unsupported type expression raises while its
    schema is built.

    :param validate_class: The type expression to adapt.
    :return: An adapter for ``validate_class``, memoized when it can be.
    """
    try:
        hash(validate_class)
    except TypeError:
        return TypeAdapter(validate_class)
    return _type_adapter(validate_class)


def run_pydantic_type_validator(validate_class: type[V], obj: Any) -> V:
    """Perform Pydantic validation for the specified type with the specified object.

    :param validate_class: The class to use for validation. Any Pydantic type
        expression works; ``type[V]`` is declared so callers passing a class get
        the validated type back.
    :param obj: The Python object to validate.
    :return: The validated object.
    :raises ValidationError: If ``obj`` does not satisfy ``validate_class``.
    """
    return _adapter_for(validate_class).validate_python(obj)


def extract_model_from_instance(instance: BaseModel, model_cls: type[T]) -> T:
    """Extract and validate only matching fields from one Pydantic model to another.

    This function filters the source model's data to match only the fields defined
    in the target model class, then performs validation using Pydantic.

    :param instance: The source Pydantic model instance.
    :param model_cls: The target model class to validate against.
    :return: An instance of the target model with validated and filtered data.
    """
    data = instance.model_dump()
    allowed_keys = model_cls.model_fields.keys()
    filtered_data = {k: data[k] for k in allowed_keys if k in data}
    return TypeAdapter(model_cls).validate_python(filtered_data)


def loc_to_dot_sep(loc: tuple[str | int, ...]) -> str:
    """Convert a Pydantic error location tuple to a dot-separated string.

    This function transforms a tuple representing the location of an error in a
    Pydantic model into a human-readable dot-separated string format.

    :param loc: A tuple representing the location of an error in a Pydantic model. Each
        element can be either a string (for field names) or an integer (for list
        indices).
    :type loc: tuple[str | int, ...]
    :return: A dot-separated string representing the error location.
    :rtype: str
    :raises TypeError: If an element in the `loc` tuple is neither a string nor an
        integer.
    """
    path = ""
    for loc_part_index, loc_part in enumerate(loc):
        if isinstance(loc_part, str):
            if loc_part_index > 0:
                path += "."
            path += loc_part
        elif isinstance(loc_part, int):
            path += f"[{loc_part}]"
        else:
            raise TypeError("Unexpected type")
    return path


def blank_str_values_to_none(data: Any) -> Any:
    """Coerce every empty-string value in a submission ``dict`` to ``None``.

    Intended as the body of a ``@model_validator(mode="before")`` on form models whose
    HTML path submits ``""`` for unset fields where the JSON path sends ``null``;
    normalising ``""`` to ``None`` lets one model validate both paths identically (a
    blank required field still fails as ``None``). A non-``dict`` input is returned
    unchanged so it flows through to the model's own validation.

    :param data: The raw pre-validation input; only ``dict`` values are transformed.
    :return: The ``dict`` with empty-string values replaced by ``None``, or ``data``
        unchanged when it is not a ``dict``.
    """
    if isinstance(data, dict):
        return {key: (None if value == "" else value) for key, value in data.items()}
    return data
