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

"""Define declarative conditional-rule primitives for the plugin schema DSL.

This module exposes three layers:

* **DSL** — :class:`FieldExpr` and the :class:`Predicate` hierarchy, plus
  helper functions (:func:`F`, :func:`truthy`, :func:`all_`, :func:`xor_`, …)
  for authoring predicates as typed Python expressions.
* **Rule envelopes** — :class:`FieldGate`, :class:`CardinalityRule`, and
  :class:`FailRule` Pydantic models attached to ``BaseField`` /
  ``FormSection`` / ``AppSchema`` scopes.
* **Runtime/wiring** — :class:`ConditionalRulesModel` and
  :func:`apply_conditional_rules` opt a plugin ``Write`` model into runtime
  enforcement of the declarative rules. Multi-entity plugins (non-empty
  ``AppSchema.entities``) must pass ``entity_name=...`` to
  :func:`apply_conditional_rules` so the plan is scoped to one entity's
  ``forms`` / section rules / entity-level rules; root ``forms`` and
  plugin-level ``cardinality_rules`` / ``fail_when`` are not used in that
  mode.

The JSON wire format produced by :meth:`Predicate.to_dict` is the contract
consumed verbatim by the frontend renderer; predicate authoring
in production uses this DSL only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, ClassVar, Self, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import core_schema

from app.core.utils.fields import value_is_present

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from app.extensions.apps.framework.schema import AppSchema

__all__ = [
    "AllEqual",
    "AllFalsy",
    "AllPresent",
    "AllTruthy",
    "And",
    "AnyFalsy",
    "AnyPresent",
    "AnyTruthy",
    "CardinalityRule",
    "ConditionalRulesModel",
    "Contains",
    "Equals",
    "F",
    "FailRule",
    "Falsy",
    "FieldExpr",
    "FieldGate",
    "Gt",
    "Gte",
    "Lt",
    "Lte",
    "NonePresent",
    "Not",
    "NotEquals",
    "Or",
    "Predicate",
    "RulePlan",
    "Truthy",
    "Xor",
    "absent",
    "all_",
    "all_equal",
    "all_falsy",
    "all_present",
    "all_truthy",
    "any_",
    "any_falsy",
    "any_present",
    "any_truthy",
    "apply_conditional_rules",
    "contains",
    "evaluate_conditional_rules",
    "extract_forbidden_field_gate_plan",
    "extract_required_field_gate_plan",
    "falsy",
    "none_present",
    "not_",
    "present",
    "truthy",
    "value_is_present",
    "xor_",
]


_BOOL_GUARD_MESSAGE = (
    "Rule objects cannot be used with 'and', 'or', or 'not'. "
    "Use '&', '|', '^', and '~', or all_(), any_(), xor_(), and not_()."
)
_FIELDEXPR_COMBINE_MESSAGE = (
    "FieldExpr cannot be combined with {op}; use truthy(...) "
    "or compare to a value first."
)


# ── Field references ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _FieldRef:
    """Mark a value as a reference to another field rather than a literal.

    Used as the right-hand side of ordered comparisons (``F("a") > F("b")``)
    so :meth:`Gt.to_dict` can emit ``{"$field": "<name>"}``. ``Equals`` and
    ``NotEquals`` never carry a :class:`_FieldRef` — field-to-field equality
    is encoded as the binary form of :class:`AllEqual` instead, keeping a
    single wire shape per semantic.

    :ivar name: The referenced field's Python attribute name.
    :vartype name: str
    """

    name: str


class FieldExpr:
    """Reference a field by name in the typed predicate DSL.

    A :class:`FieldExpr` is **not** a :class:`Predicate`. Comparison
    operators (``==``, ``!=``, ``>``, ``>=``, ``<``, ``<=``) return
    Predicates; boolean operators (``&``, ``|``, ``^``, ``~``) raise
    ``TypeError`` to surface common authoring mistakes loudly at construction.

    :ivar name: The referenced field's Python attribute name.
    :vartype name: str
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name:
            raise TypeError(
                f"F() requires a non-empty string field name, got {name!r}."
            )
        self.name = name

    def __eq__(self, other: object) -> Predicate:  # ty: ignore[invalid-method-override]
        if isinstance(other, FieldExpr):
            return AllEqual([self.name, other.name])
        return Equals(self.name, other)

    def __ne__(self, other: object) -> Predicate:  # ty: ignore[invalid-method-override]
        if isinstance(other, FieldExpr):
            return Not(AllEqual([self.name, other.name]))
        return NotEquals(self.name, other)

    def __gt__(self, other: object) -> Predicate:
        return Gt(self.name, _wrap_rhs(other))

    def __ge__(self, other: object) -> Predicate:
        return Gte(self.name, _wrap_rhs(other))

    def __lt__(self, other: object) -> Predicate:
        return Lt(self.name, _wrap_rhs(other))

    def __le__(self, other: object) -> Predicate:
        return Lte(self.name, _wrap_rhs(other))

    def __and__(self, other: object) -> None:
        raise TypeError(_FIELDEXPR_COMBINE_MESSAGE.format(op="&"))

    def __or__(self, other: object) -> None:
        raise TypeError(_FIELDEXPR_COMBINE_MESSAGE.format(op="|"))

    def __xor__(self, other: object) -> None:
        raise TypeError(_FIELDEXPR_COMBINE_MESSAGE.format(op="^"))

    def __invert__(self) -> None:
        raise TypeError(_FIELDEXPR_COMBINE_MESSAGE.format(op="~"))

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"F({self.name!r})"


def F(name: str) -> FieldExpr:  # noqa: N802 — DSL entry point
    """Return a :class:`FieldExpr` referring to a field by name.

    :param name: The Python attribute name of the field to reference.
    :type name: str
    :return: A field expression usable in predicate construction.
    :rtype: FieldExpr
    """
    return FieldExpr(name)


def _wrap_rhs(value: object) -> object:
    """Wrap a :class:`FieldExpr` RHS as :class:`_FieldRef`; pass others through.

    :param value: The right-hand side of an ordered comparison operator.
    :type value: object
    :return: The original literal, or a :class:`_FieldRef` for a
        :class:`FieldExpr`.
    :rtype: object
    """
    if isinstance(value, FieldExpr):
        return _FieldRef(value.name)
    return value


# ── Field-presence / truthiness helpers ──────────────────────────────────


def _resolve_field(instance: Any, path: str) -> Any:
    """Return the value at ``path``, walking dotted nested attribute paths.

    Each segment is resolved with ``getattr(segment, default=None)``. A
    missing intermediate value short-circuits to ``None``.

    :param instance: The model instance being evaluated.
    :param path: A top-level field name or dotted path (for example,
        ``"source.mode"``).
    :return: The resolved value, or ``None`` when any segment is absent.
    """
    current: Any = instance
    for segment in path.split("."):
        if current is None:
            return None
        current = getattr(current, segment, None)
    return current


def _field_is_present(instance: Any, name: str) -> bool:
    """Return ``True`` iff ``instance.<name>`` is set and non-empty.

    Delegates the value-level classification to :func:`value_is_present`.

    :param instance: The model instance being evaluated.
    :param name: The field name to check.
    :return: Whether the field is considered present.
    """
    return value_is_present(_resolve_field(instance, name))


def _field_is_truthy(instance: Any, name: str) -> bool:
    """Return ``bool(instance.<name>)``.

    :param instance: The model instance being evaluated.
    :type instance: Any
    :param name: The field name to check.
    :type name: str
    :return: The Python truthiness of the field's value.
    :rtype: bool
    """
    return bool(_resolve_field(instance, name))


# ── Predicate hierarchy ──────────────────────────────────────────────────


class Predicate(ABC):
    """Abstract base for boolean predicates over a model instance.

    Composable via ``&`` (AND), ``|`` (OR), ``^`` (XOR; 2-arg only) and
    ``~`` (NOT). ``bool(predicate)`` raises ``TypeError`` so misuse with
    Python's ``and`` / ``or`` / ``not`` keywords fails loudly at
    construction.

    Subclasses implement :meth:`evaluate`, :meth:`to_dict`, and
    :meth:`referenced_fields`.
    """

    @abstractmethod
    def evaluate(self, instance: Any) -> bool:
        """Return the predicate's truth value against ``instance``.

        :param instance: The model instance to evaluate against.
        :type instance: Any
        :return: ``True`` if the predicate matches.
        :rtype: bool
        """

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape consumed by the FE renderer.

        :return: A single-key dict whose key is the predicate's wire
            operator (``equals``, ``truthy``, ``all``, ``not``, …).
        :rtype: dict[str, Any]
        """

    @abstractmethod
    def referenced_fields(self) -> set[str]:
        """Return the set of field names this predicate reads from.

        :return: Every field name appearing in the predicate (recursively
            for boolean composers).
        :rtype: set[str]
        """

    def __bool__(self) -> bool:
        raise TypeError(_BOOL_GUARD_MESSAGE)

    def __and__(self, other: object) -> Predicate:
        if not isinstance(other, Predicate):
            return NotImplemented
        return And([self, other])

    def __or__(self, other: object) -> Predicate:
        if not isinstance(other, Predicate):
            return NotImplemented
        return Or([self, other])

    def __xor__(self, other: object) -> Predicate:
        if not isinstance(other, Predicate):
            return NotImplemented
        return Xor(self, other)

    def __invert__(self) -> Predicate:
        return Not(self)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: Any
    ) -> core_schema.CoreSchema:
        """Tell Pydantic how to validate / serialise :class:`Predicate` fields.

        Predicates are constructed via the typed DSL only; deserialising a
        dict back into a :class:`Predicate` is unsupported by design. The
        validator therefore accepts only :class:`Predicate` instances; the
        serializer dispatches to :meth:`Predicate.to_dict`.
        """
        return core_schema.no_info_plain_validator_function(
            cls._validate_instance,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda p: p.to_dict(),
                when_used="always",
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, _schema: core_schema.CoreSchema, _handler: Any
    ) -> dict[str, Any]:
        """Return an open object schema for OpenAPI / JSON-schema generation.

        Predicate JSON is a discriminated union over operator keys (each
        producing a single-key object). Modelling that precisely as a
        JSON-schema oneOf is overkill for the FE contract, since the frontend
        consumes the wire format directly rather than auto-generating a
        TypeScript model from it. An open ``object`` shape lets FastAPI
        emit a valid OpenAPI document while keeping the wire contract
        documented in the plan.

        :return: An open JSON-schema object shape.
        :rtype: dict[str, Any]
        """
        return {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Predicate wire shape — a single-key object whose key is "
                "the operator name (equals, truthy, all, any, xor, not, "
                "etc.). See the plan for the full operator catalogue."
            ),
        }

    @classmethod
    def _validate_instance(cls, value: Any) -> Predicate:
        if isinstance(value, Predicate):
            return value
        raise TypeError(
            f"Expected a Predicate instance, got {type(value).__name__}. "
            "Predicates are constructed via the typed DSL "
            "(F('name') == ..., truthy(...), all_(...), etc.); they cannot "
            "be deserialised from JSON or dict literals."
        )


def _wire_value(value: Any) -> Any:
    """Convert a literal value to its JSON-wire representation.

    Enums are coerced to their ``.value`` so the wire JSON contains the
    underlying scalar (``IntEnum.X`` → ``int(X)``), keeping the FE contract
    independent of plugin-side enum definitions.

    :param value: A literal predicate operand.
    :type value: Any
    :return: The wire-safe representation of ``value``.
    :rtype: Any
    """
    if isinstance(value, Enum):
        return value.value
    return value


# ── Comparison predicates ────────────────────────────────────────────────


class Equals(Predicate):
    """Match when ``instance.<field>`` equals the literal ``value``.

    :ivar field: The referenced field name.
    :vartype field: str
    :ivar value: The literal compared against the field.
    :vartype value: Any
    """

    __slots__ = ("field", "value")

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return _resolve_field(instance, self.field) == self.value

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"equals": {self.field: _wire_value(self.value)}}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return {self.field}


class NotEquals(Predicate):
    """Match when ``instance.<field>`` is not equal to the literal ``value``.

    :ivar field: The referenced field name.
    :vartype field: str
    :ivar value: The literal compared against the field.
    :vartype value: Any
    """

    __slots__ = ("field", "value")

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return _resolve_field(instance, self.field) != self.value

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"not_equals": {self.field: _wire_value(self.value)}}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return {self.field}


class Contains(Predicate):
    """Match when ``value`` is a member of the container at ``instance.<field>``.

    The container must be a list/tuple/set/frozenset (typically the value of
    a MultiChoice field). Any other type — including ``str``, ``bytes``, and
    mappings — evaluates to ``False``. This matches the frontend predicate
    evaluator, which treats non-arrays as ``False``. For substring tests use
    :class:`Equals` plus :func:`truthy` instead.

    Comparison is tolerant of enum members: when either side is an
    :class:`Enum`, both the underlying ``.value`` and the member ``.name``
    are checked. This lets a single :class:`Contains` declaration work
    against both the post-Pydantic instance (where the container holds
    enum members) and the frontend wire form (where it holds the choice's
    ``value`` string).

    :ivar field: The referenced field name.
    :vartype field: str
    :ivar value: The literal member checked for inclusion.
    :vartype value: Any
    """

    __slots__ = ("field", "value")

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    @staticmethod
    def _keys(value: Any) -> set[Any]:
        """Return the comparison keys for an enum or scalar value."""
        if isinstance(value, Enum):
            return {value, value.value, value.name}
        return {value}

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        container = _resolve_field(instance, self.field)
        if not isinstance(container, list | tuple | set | frozenset):
            return False
        target_keys = self._keys(self.value)
        return any(target_keys & self._keys(item) for item in container)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"contains": {self.field: _wire_value(self.value)}}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return {self.field}


class _OrderedComparison(Predicate):
    """Common machinery for the four ordered-comparison predicates.

    Concrete subclasses set the wire-operator key and the Python comparator.

    :ivar field: The left-hand field name.
    :vartype field: str
    :ivar value: A literal or :class:`_FieldRef` operand for the right-hand side.
    :vartype value: Any | _FieldRef
    """

    __slots__ = ("field", "value")
    _WIRE_OP: ClassVar[str]

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def _compare(self, lhs: Any, rhs: Any) -> bool:
        """Apply the subclass's ordered-comparison operator."""
        raise NotImplementedError

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        lhs = _resolve_field(instance, self.field)
        if isinstance(self.value, _FieldRef):
            rhs = _resolve_field(instance, self.value.name)
        else:
            rhs = self.value
        if lhs is None or rhs is None:
            return False
        return self._compare(lhs, rhs)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        if isinstance(self.value, _FieldRef):
            return {self._WIRE_OP: {self.field: {"$field": self.value.name}}}
        return {self._WIRE_OP: {self.field: _wire_value(self.value)}}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        refs = {self.field}
        if isinstance(self.value, _FieldRef):
            refs.add(self.value.name)
        return refs


class Gt(_OrderedComparison):
    """Match when ``instance.<field>`` is strictly greater than the operand."""

    _WIRE_OP = "gt"

    def _compare(self, lhs: Any, rhs: Any) -> bool:
        """Apply the subclass's ordered-comparison operator."""
        return lhs > rhs


class Gte(_OrderedComparison):
    """Match when ``instance.<field>`` is greater than or equal to the operand."""

    _WIRE_OP = "gte"

    def _compare(self, lhs: Any, rhs: Any) -> bool:
        """Apply the subclass's ordered-comparison operator."""
        return lhs >= rhs


class Lt(_OrderedComparison):
    """Match when ``instance.<field>`` is strictly less than the operand."""

    _WIRE_OP = "lt"

    def _compare(self, lhs: Any, rhs: Any) -> bool:
        """Apply the subclass's ordered-comparison operator."""
        return lhs < rhs


class Lte(_OrderedComparison):
    """Match when ``instance.<field>`` is less than or equal to the operand."""

    _WIRE_OP = "lte"

    def _compare(self, lhs: Any, rhs: Any) -> bool:
        """Apply the subclass's ordered-comparison operator."""
        return lhs <= rhs


# ── Single-field truthiness predicates ───────────────────────────────────


class Truthy(Predicate):
    """Match when ``bool(instance.<field>)`` is ``True``.

    :ivar field: The referenced field name.
    :vartype field: str
    """

    __slots__ = ("field",)

    def __init__(self, field: str) -> None:
        self.field = field

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return _field_is_truthy(instance, self.field)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"truthy": self.field}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return {self.field}


class Falsy(Predicate):
    """Match when ``bool(instance.<field>)`` is ``False``.

    :ivar field: The referenced field name.
    :vartype field: str
    """

    __slots__ = ("field",)

    def __init__(self, field: str) -> None:
        self.field = field

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return not _field_is_truthy(instance, self.field)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"falsy": self.field}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return {self.field}


# ── Multi-field predicates ───────────────────────────────────────────────


class _MultiFieldPredicate(Predicate):
    """Common machinery for predicates that take a list of field names.

    :ivar fields: The non-empty list of referenced field names.
    :vartype fields: list[str]
    """

    __slots__ = ("fields",)
    _WIRE_OP: ClassVar[str]
    _MIN_FIELDS: ClassVar[int] = 1

    def __init__(self, fields: Iterable[str]) -> None:
        items = list(fields)
        if len(items) < self._MIN_FIELDS:
            raise ValueError(
                f"{type(self).__name__} requires at least "
                f"{self._MIN_FIELDS} field name(s)."
            )
        self.fields = items

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {self._WIRE_OP: list(self.fields)}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return set(self.fields)


class AllTruthy(_MultiFieldPredicate):
    """Match when every listed field is truthy."""

    _WIRE_OP = "all_truthy"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return all(_field_is_truthy(instance, name) for name in self.fields)


class AllFalsy(_MultiFieldPredicate):
    """Match when every listed field is falsy."""

    _WIRE_OP = "all_falsy"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return all(not _field_is_truthy(instance, name) for name in self.fields)


class AnyTruthy(_MultiFieldPredicate):
    """Match when at least one listed field is truthy."""

    _WIRE_OP = "any_truthy"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return any(_field_is_truthy(instance, name) for name in self.fields)


class AnyFalsy(_MultiFieldPredicate):
    """Match when at least one listed field is falsy."""

    _WIRE_OP = "any_falsy"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return any(not _field_is_truthy(instance, name) for name in self.fields)


class AnyPresent(_MultiFieldPredicate):
    """Match when at least one listed field is present (non-``None``, non-empty)."""

    _WIRE_OP = "any_present"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return any(_field_is_present(instance, name) for name in self.fields)


class AllPresent(_MultiFieldPredicate):
    """Match when every listed field is present (non-``None``, non-empty)."""

    _WIRE_OP = "all_present"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return all(_field_is_present(instance, name) for name in self.fields)


class NonePresent(_MultiFieldPredicate):
    """Match when none of the listed fields is present."""

    _WIRE_OP = "none_present"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return all(not _field_is_present(instance, name) for name in self.fields)


class AllEqual(_MultiFieldPredicate):
    """Match when every listed field is mutually equal.

    Requires at least two field names.
    """

    _WIRE_OP = "all_equal"
    _MIN_FIELDS = 2

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        first = _resolve_field(instance, self.fields[0])
        return all(_resolve_field(instance, name) == first for name in self.fields[1:])


# ── Boolean composition ──────────────────────────────────────────────────


class _BoolListPredicate(Predicate):
    """Common machinery for predicates that AND/OR a list of children.

    :ivar predicates: The non-empty list of child predicates.
    :vartype predicates: list[Predicate]
    """

    __slots__ = ("predicates",)
    _WIRE_OP: ClassVar[str]

    def __init__(self, predicates: Iterable[Predicate]) -> None:
        items = list(predicates)
        if not items:
            raise ValueError(f"{type(self).__name__} requires at least one predicate")
        self.predicates = items

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {self._WIRE_OP: [p.to_dict() for p in self.predicates]}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        refs = set()
        for p in self.predicates:
            refs |= p.referenced_fields()
        return refs


class And(_BoolListPredicate):
    """Match when every child predicate matches."""

    _WIRE_OP = "all"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return all(p.evaluate(instance) for p in self.predicates)


class Or(_BoolListPredicate):
    """Match when at least one child predicate matches."""

    _WIRE_OP = "any"

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return any(p.evaluate(instance) for p in self.predicates)


class Xor(Predicate):
    """Match when exactly one of two child predicates matches.

    Two-argument only — chained ``p1 ^ p2 ^ p3`` is parity rather than
    "exactly one of N", which is rarely the intended meaning.

    :ivar left: The first predicate.
    :vartype left: Predicate
    :ivar right: The second predicate.
    :vartype right: Predicate
    """

    __slots__ = ("left", "right")

    def __init__(self, left: Predicate, right: Predicate) -> None:
        self.left = left
        self.right = right

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return self.left.evaluate(instance) != self.right.evaluate(instance)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"xor": [self.left.to_dict(), self.right.to_dict()]}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return self.left.referenced_fields() | self.right.referenced_fields()


class Not(Predicate):
    """Match when the child predicate does not match.

    :ivar predicate: The negated child predicate.
    :vartype predicate: Predicate
    """

    __slots__ = ("predicate",)

    def __init__(self, predicate: Predicate) -> None:
        self.predicate = predicate

    def evaluate(self, instance: Any) -> bool:
        """Evaluate this predicate against ``instance``."""
        return not self.predicate.evaluate(instance)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON wire shape for this predicate."""
        return {"not": self.predicate.to_dict()}

    def referenced_fields(self) -> set[str]:
        """Return the field names this predicate reads from."""
        return self.predicate.referenced_fields()


# ── DSL helper functions ─────────────────────────────────────────────────


def truthy(name: str) -> Predicate:
    """Return a predicate matching when ``instance.<name>`` is truthy.

    :param name: The field name to check.
    :type name: str
    :return: A :class:`Truthy` predicate.
    :rtype: Predicate
    """
    return Truthy(name)


def falsy(name: str) -> Predicate:
    """Return a predicate matching when ``instance.<name>`` is falsy.

    :param name: The field name to check.
    :type name: str
    :return: A :class:`Falsy` predicate.
    :rtype: Predicate
    """
    return Falsy(name)


def present(name: str) -> Predicate:
    """Return a predicate matching when ``instance.<name>`` is present.

    Equivalent to :func:`any_present` of a single field.

    :param name: The field name to check.
    :type name: str
    :return: An :class:`AnyPresent` predicate over the single field.
    :rtype: Predicate
    """
    return AnyPresent([name])


def absent(name: str) -> Predicate:
    """Return a predicate matching when ``instance.<name>`` is absent.

    Equivalent to :func:`none_present` of a single field.

    :param name: The field name to check.
    :type name: str
    :return: A :class:`NonePresent` predicate over the single field.
    :rtype: Predicate
    """
    return NonePresent([name])


def contains(name: str, value: Any) -> Predicate:
    """Return a predicate matching when ``value`` is a member of ``instance.<name>``.

    :param name: The field name whose value is the container.
    :type name: str
    :param value: The literal member checked for inclusion.
    :type value: Any
    :return: A :class:`Contains` predicate.
    :rtype: Predicate
    """
    return Contains(name, value)


def all_truthy(*names: str) -> Predicate:
    """Return a predicate matching when every listed field is truthy.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AllTruthy` predicate.
    :rtype: Predicate
    """
    return AllTruthy(names)


def all_falsy(*names: str) -> Predicate:
    """Return a predicate matching when every listed field is falsy.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AllFalsy` predicate.
    :rtype: Predicate
    """
    return AllFalsy(names)


def any_truthy(*names: str) -> Predicate:
    """Return a predicate matching when at least one listed field is truthy.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AnyTruthy` predicate.
    :rtype: Predicate
    """
    return AnyTruthy(names)


def any_falsy(*names: str) -> Predicate:
    """Return a predicate matching when at least one listed field is falsy.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AnyFalsy` predicate.
    :rtype: Predicate
    """
    return AnyFalsy(names)


def any_present(*names: str) -> Predicate:
    """Return a predicate matching when at least one listed field is present.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AnyPresent` predicate.
    :rtype: Predicate
    """
    return AnyPresent(names)


def all_present(*names: str) -> Predicate:
    """Return a predicate matching when every listed field is present.

    :param names: The field names to check.
    :type names: str
    :return: An :class:`AllPresent` predicate.
    :rtype: Predicate
    """
    return AllPresent(names)


def none_present(*names: str) -> Predicate:
    """Return a predicate matching when none of the listed fields is present.

    :param names: The field names to check.
    :type names: str
    :return: A :class:`NonePresent` predicate.
    :rtype: Predicate
    """
    return NonePresent(names)


def all_equal(*names: str) -> Predicate:
    """Return a predicate matching when every listed field is mutually equal.

    Requires at least two field names; ``all_equal("a")`` is meaningless.

    :param names: The field names to compare for mutual equality.
    :type names: str
    :return: An :class:`AllEqual` predicate.
    :rtype: Predicate
    :raises ValueError: If fewer than two names are supplied.
    """
    return AllEqual(names)


def all_(*predicates: Predicate) -> Predicate:
    """Return the conjunction of every supplied predicate.

    :param predicates: The non-empty predicates to AND together.
    :type predicates: Predicate
    :return: An :class:`And` predicate over ``predicates``.
    :rtype: Predicate
    :raises ValueError: If no predicates are supplied.
    """
    return And(predicates)


def any_(*predicates: Predicate) -> Predicate:
    """Return the disjunction of every supplied predicate.

    :param predicates: The non-empty predicates to OR together.
    :type predicates: Predicate
    :return: An :class:`Or` predicate over ``predicates``.
    :rtype: Predicate
    :raises ValueError: If no predicates are supplied.
    """
    return Or(predicates)


def xor_(*predicates: Predicate) -> Predicate:
    """Return the exclusive-or of two predicates.

    Three-or-more-argument calls raise rather than silently desugaring to
    parity: chained ``p1 ^ p2 ^ p3`` is odd-number-true, which is almost
    never what the author intended for "exactly one of N". For that
    semantic, use a :class:`CardinalityRule` with ``min=1, max=1`` over
    the relevant fields.

    :param predicates: Exactly two predicates.
    :type predicates: Predicate
    :return: An :class:`Xor` predicate.
    :rtype: Predicate
    :raises ValueError: If the call site supplies a number of arguments
        other than two.
    """
    expected_arity = 2
    if len(predicates) != expected_arity:
        raise ValueError(
            "xor_ takes exactly 2 arguments. For 'exactly one of N', use a "
            "CardinalityRule with `min=1, max=1` over the relevant fields, "
            "or compose `xor_` calls explicitly with the semantics you "
            "intend (note: chained `p1 ^ p2 ^ p3` is parity — odd-number-"
            "true — NOT exactly-one-of-N)."
        )
    return Xor(predicates[0], predicates[1])


def not_(predicate: Predicate) -> Predicate:
    """Return the negation of ``predicate``.

    :param predicate: The predicate to negate.
    :type predicate: Predicate
    :return: A :class:`Not` predicate wrapping ``predicate``.
    :rtype: Predicate
    """
    return Not(predicate)


# ── Rule envelopes (Layer B) ─────────────────────────────────────────────


class _RuleBase(BaseModel):
    """Common Pydantic config for the three rule envelope types."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )


class FieldGate(_RuleBase):
    """Express a binary self-cardinality gate at :class:`BaseField` scope.

    Used by :attr:`BaseField.requires` and :attr:`BaseField.forbidden`. The
    target field is the field carrying the rule (implicit self).

    :param when: The predicate that gates the rule.
    :type when: Predicate
    :param message: Optional failure message surfaced when the rule fires.
        Defaults to ``None``.
    :type message: str | None
    """

    when: Predicate
    message: str | None = None


class CardinalityRule(_RuleBase):
    """Express a cardinality constraint over a list of fields.

    When ``when`` matches (or unconditionally if ``when`` is ``None``), the
    count of present fields in ``fields`` must lie in ``[min, max]`` with
    ``None`` bounds treated as unbounded.

    :param when: The predicate that gates the rule. ``None`` means
        unconditional.
    :type when: Predicate | None
    :param fields: The non-empty list of target field names.
    :type fields: list[str]
    :param min: Optional lower bound on the count of present fields.
        Defaults to ``None``.
    :type min: int | None
    :param max: Optional upper bound on the count of present fields.
        Defaults to ``None``.
    :type max: int | None
    :param message: Optional failure message. Defaults to ``None``.
    :type message: str | None
    """

    when: Predicate | None = None
    fields: list[str]
    min: int | None = None
    max: int | None = None
    message: str | None = None

    @field_validator("fields")
    @classmethod
    def _fields_non_empty(cls, value: list[str]) -> list[str]:
        """Reject an empty ``fields`` list at field-validator scope.

        :param value: The candidate ``fields`` list.
        :type value: list[str]
        :return: The validated list.
        :rtype: list[str]
        :raises ValueError: If ``value`` is empty.
        """
        if not value:
            raise ValueError(
                "CardinalityRule requires non-empty `fields`. Use `fail_when` "
                "(FailRule) for predicate-only invariants."
            )
        return value

    @model_validator(mode="after")
    def _validate_cardinality_shape(self) -> Self:
        """Reject malformed cardinality bounds.

        :return: The validated rule instance.
        :rtype: CardinalityRule
        :raises ValueError: If both bounds are unset, either bound is
            negative, ``min > max``, or ``min > len(fields)``.
        """
        if self.min is None and self.max is None:
            raise ValueError(
                "CardinalityRule requires at least one of `min` or `max` "
                "(no constraint specified)."
            )
        if self.min is not None and self.min < 0:
            raise ValueError(
                f"CardinalityRule.min must be non-negative, got {self.min}."
            )
        if self.max is not None and self.max < 0:
            raise ValueError(
                f"CardinalityRule.max must be non-negative, got {self.max}."
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"CardinalityRule.min ({self.min}) must be <= max ({self.max})."
            )
        if self.min is not None and self.min > len(self.fields):
            raise ValueError(
                f"CardinalityRule.min ({self.min}) exceeds number of "
                f"fields ({len(self.fields)})."
            )
        return self


class FailRule(_RuleBase):
    """Express a predicate-only invariant: rule fails iff predicate matches.

    :param fail_when: The predicate whose match triggers a failure.
    :type fail_when: Predicate
    :param error_fields: The field names the FE renderer should attach the
        error to (informational hint; the runtime evaluator ignores this
        list). May be empty when no field is logically responsible.
    :type error_fields: list[str]
    :param message: Optional failure message. Defaults to ``None``.
    :type message: str | None
    """

    fail_when: Predicate
    error_fields: list[str]
    message: str | None = None


# ── Runtime / wiring (Layer C) ───────────────────────────────────────────


class _RuleKind(StrEnum):
    """Tag distinguishing the four runtime rule shapes."""

    FIELD_GATE_REQUIRES = "field_gate_requires"
    FIELD_GATE_FORBIDDEN = "field_gate_forbidden"
    CARDINALITY = "cardinality"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class _PreparedRule:
    """Runtime-friendly snapshot of a single rule extracted from a schema.

    :ivar kind: The rule's category (drives evaluation logic).
    :vartype kind: _RuleKind
    :ivar scope_path: A human-readable path identifying the rule's
        origin within the schema, used in failure messages and Tier-2
        / Tier-3 errors.
    :vartype scope_path: str
    :ivar predicate: The gating predicate; ``None`` only for unconditional
        :class:`CardinalityRule` (``when=None``).
    :vartype predicate: Predicate | None
    :ivar fields: The target field names. For ``field_gate_*`` rules this
        contains exactly one entry — the implicit self target.
    :vartype fields: tuple[str, ...]
    :ivar min: Lower bound for cardinality rules; ``None`` otherwise.
    :vartype min: int | None
    :ivar max: Upper bound for cardinality rules; ``None`` otherwise.
    :vartype max: int | None
    :ivar message: Optional failure message.
    :vartype message: str | None
    """

    kind: _RuleKind
    scope_path: str
    predicate: Predicate | None
    fields: tuple[str, ...]
    min: int | None
    max: int | None
    message: str | None


@dataclass(frozen=True, slots=True)
class RulePlan:
    """Hold a flat list of every conditional rule extracted from a plugin schema.

    :ivar rules: One :class:`_PreparedRule` per declarative rule across the
        schema's BaseField, FormSection, and AppSchema scopes.
    :vartype rules: tuple[_PreparedRule, ...]
    """

    rules: tuple[_PreparedRule, ...]


def _append_leaf_field_gates(
    prepared: list[_PreparedRule],
    field: Any,
    *,
    field_scope_prefix: str,
    scope_path: str | None = None,
) -> None:
    """Append ``requires`` / ``forbidden`` gates declared on one leaf field.

    :param prepared: Mutable list to extend.
    :param field: The leaf :class:`~app.extensions.apps.framework.schema.BaseField`.
    :param field_scope_prefix: Prefix for default ``scope_path`` values.
    :param scope_path: Optional explicit ``scope_path`` label for this field.
    """
    self_name = field.name
    base_scope = scope_path or (
        f"{field_scope_prefix}BaseField {self_name!r}"
        if field_scope_prefix
        else f"BaseField {self_name!r}"
    )
    for rule_index, gate in enumerate(field.requires or []):
        prepared.append(
            _PreparedRule(
                kind=_RuleKind.FIELD_GATE_REQUIRES,
                scope_path=f"{base_scope} requires[{rule_index}]",
                predicate=gate.when,
                fields=(self_name,),
                min=None,
                max=None,
                message=gate.message,
            )
        )
    for rule_index, gate in enumerate(field.forbidden or []):
        prepared.append(
            _PreparedRule(
                kind=_RuleKind.FIELD_GATE_FORBIDDEN,
                scope_path=f"{base_scope} forbidden[{rule_index}]",
                predicate=gate.when,
                fields=(self_name,),
                min=None,
                max=None,
                message=gate.message,
            )
        )


def _append_one_of_group_rules(
    prepared: list[_PreparedRule],
    group: Any,
    *,
    field_scope_prefix: str,
) -> None:
    """Append branch-selection and leaf gates for one :class:`OneOfGroup`."""
    from app.extensions.apps.framework.schema import OneOfGroup  # noqa: PLC0415

    if not isinstance(group, OneOfGroup):
        return
    group_scope = (
        f"{field_scope_prefix}OneOfGroup {group.name!r}"
        if field_scope_prefix
        else f"OneOfGroup {group.name!r}"
    )
    all_branch_values = {branch.value for branch in group.branches}
    leaves_by_name: dict[str, Any] = {}
    allowed_branch_values: dict[str, set[str]] = {}
    for branch in group.branches:
        for leaf in branch.fields:
            leaves_by_name.setdefault(leaf.name, leaf)
            allowed_branch_values.setdefault(leaf.name, set()).add(branch.value)
    for leaf_name, leaf in leaves_by_name.items():
        allowed = allowed_branch_values.get(leaf_name, set())
        if allowed and allowed != all_branch_values:
            predicate: Predicate
            if len(allowed) == 1:
                predicate = NotEquals(group.discriminator, next(iter(allowed)))
            else:
                predicate = all_(
                    *[
                        NotEquals(group.discriminator, value)
                        for value in sorted(allowed)
                    ]
                )
            prepared.append(
                _PreparedRule(
                    kind=_RuleKind.FIELD_GATE_FORBIDDEN,
                    scope_path=(f"{group_scope} forbidden[{leaf_name!r}]"),
                    predicate=predicate,
                    fields=(leaf_name,),
                    min=None,
                    max=None,
                    message=None,
                )
            )
        _append_leaf_field_gates(
            prepared,
            leaf,
            field_scope_prefix=field_scope_prefix,
            scope_path=f"{group_scope} BaseField {leaf_name!r}",
        )


def _append_rules_for_form_sections(
    prepared: list[_PreparedRule],
    forms: list,
    *,
    field_scope_prefix: str,
    section_label_for_index: Callable[[int, Any], str],
) -> None:
    """Append prepared rules for every section in ``forms``.

    :param prepared: Mutable list to extend.
    :param forms: ``FormSection`` list from root ``AppSchema.forms`` or one
        :class:`AppEntitySchema.forms`.
    :param field_scope_prefix: Prefix for field-gate ``scope_path`` values
        (empty string for single-entity schemas; entity label for multi-entity).
    :param section_label_for_index: Returns the scope label for a section's
        cardinality / fail rules.
    """
    from app.extensions.apps.framework.schema import OneOfGroup  # noqa: PLC0415

    for section_index, section in enumerate(forms):
        section_scope = section_label_for_index(section_index, section)
        for field in section.fields:
            if isinstance(field, OneOfGroup):
                _append_one_of_group_rules(
                    prepared, field, field_scope_prefix=field_scope_prefix
                )
            else:
                _append_leaf_field_gates(
                    prepared, field, field_scope_prefix=field_scope_prefix
                )
        prepared.extend(
            _prepare_cardinality_rules(section.cardinality_rules, section_scope)
        )
        prepared.extend(_prepare_fail_rules(section.fail_when, section_scope))


def _extract_rule_plan(
    schema: AppSchema, *, entity_name: str | None = None
) -> RulePlan:
    """Walk a :class:`AppSchema` and emit one :class:`_PreparedRule` per rule.

    For task-style (single-entity) schemas, walks root ``forms`` plus
    ``AppSchema``-level ``cardinality_rules`` / ``fail_when``.

    For multi-entity schemas (non-empty ``entities``), ``entity_name`` must
    name one ``AppEntitySchema.name``; only that entity's ``forms`` and
    entity-level ``cardinality_rules`` / ``fail_when`` are included. Root
    ``forms`` and plugin-level rule lists are omitted in that mode, matching
    tier-1 schema validation.

    :param schema: The plugin schema to extract rules from.
    :param entity_name: When ``schema.entities`` is set, the entity segment
        whose rules to extract. Must be ``None`` when ``schema.entities`` is
        unset.
    :type entity_name: str | None
    :return: A frozen :class:`RulePlan` over every declarative rule in the
        selected scope.
    :rtype: RulePlan
    :raises ValueError: When ``entities`` is set but ``entity_name`` is
        missing or unknown, or when ``entity_name`` is set for a non-entity
        schema.
    """
    prepared: list[_PreparedRule] = []

    if schema.entities:
        if entity_name is None:
            raise ValueError(
                "AppSchema defines `entities`; pass entity_name=<segment> "
                "to apply_conditional_rules / _extract_rule_plan so the rule plan "
                "is scoped to one AppEntitySchema (e.g. entity_name='nodes')."
            )
        entity_index = None
        entity = None
        for idx, ent in enumerate(schema.entities):
            if ent.name == entity_name:
                entity_index = idx
                entity = ent
                break
        if entity is None:
            known = ", ".join(sorted(e.name for e in schema.entities))
            raise ValueError(
                f"entity_name {entity_name!r} is not an AppEntitySchema.name "
                f"on this schema; known entities: {known}"
            )
        entity_label = f"AppEntitySchema[{entity_index}] {entity.name!r}"
        field_prefix = f"{entity_label} "
        _append_rules_for_form_sections(
            prepared,
            entity.forms,
            field_scope_prefix=field_prefix,
            section_label_for_index=lambda i, s: (
                f"{entity_label} FormSection[{i}] {s.title!r}"
            ),
        )
        prepared.extend(
            _prepare_cardinality_rules(
                entity.cardinality_rules,
                entity_label,
            )
        )
        prepared.extend(_prepare_fail_rules(entity.fail_when, entity_label))
    else:
        if entity_name is not None:
            raise ValueError(
                "entity_name was given but this AppSchema has no `entities`; "
                "omit entity_name for task-style (single-entity) plugins."
            )
        _append_rules_for_form_sections(
            prepared,
            schema.forms,
            field_scope_prefix="",
            section_label_for_index=lambda i, s: f"FormSection[{i}] {s.title!r}",
        )
        prepared.extend(
            _prepare_cardinality_rules(
                schema.cardinality_rules, f"AppSchema {schema.name!r}"
            )
        )
        prepared.extend(
            _prepare_fail_rules(schema.fail_when, f"AppSchema {schema.name!r}")
        )

    return RulePlan(rules=tuple(prepared))


def extract_forbidden_field_gate_plan(
    schema: AppSchema, *, entity_name: str | None = None
) -> RulePlan:
    """Return a :class:`RulePlan` of only the schema's forbidden field gates.

    A public, supported entry point for callers (such as the snippets plugin's
    server-side visibility enforcement) that need just the
    ``forbidden=[FieldGate(...)]`` rules lowered onto a schema, without
    depending on the private extraction internals. The returned plan is
    evaluable with :func:`evaluate_conditional_rules`.

    :param schema: The plugin schema to extract forbidden field gates from.
    :param entity_name: For multi-entity schemas, the entity segment whose
        rules to extract; must be ``None`` for task-style schemas. See
        :func:`_extract_rule_plan`.
    :return: A frozen plan holding only the ``field_gate_forbidden`` rules.
    """
    plan = _extract_rule_plan(schema, entity_name=entity_name)
    return RulePlan(
        rules=tuple(
            rule for rule in plan.rules if rule.kind is _RuleKind.FIELD_GATE_FORBIDDEN
        )
    )


def extract_required_field_gate_plan(
    schema: AppSchema, *, entity_name: str | None = None
) -> RulePlan:
    """Return a :class:`RulePlan` of only the schema's required field gates.

    The ``requires`` counterpart of :func:`extract_forbidden_field_gate_plan`: a
    public, supported entry point for callers (such as the snippets plugin's
    server-side gate enforcement) that need just the ``requires=[FieldGate(...)]``
    rules lowered onto a schema, without depending on the private extraction
    internals. The returned plan is evaluable with
    :func:`evaluate_conditional_rules`.

    :param schema: The plugin schema to extract required field gates from.
    :param entity_name: For multi-entity schemas, the entity segment whose
        rules to extract; must be ``None`` for task-style schemas. See
        :func:`_extract_rule_plan`.
    :return: A frozen plan holding only the ``field_gate_requires`` rules.
    """
    plan = _extract_rule_plan(schema, entity_name=entity_name)
    return RulePlan(
        rules=tuple(
            rule for rule in plan.rules if rule.kind is _RuleKind.FIELD_GATE_REQUIRES
        )
    )


def _prepare_cardinality_rules(
    rules: list[CardinalityRule] | None, scope_label: str
) -> list[_PreparedRule]:
    """Convert :class:`CardinalityRule` instances into :class:`_PreparedRule`.

    :param rules: The rule list (or ``None``).
    :type rules: list[CardinalityRule] | None
    :param scope_label: The human-readable scope label used in error paths.
    :type scope_label: str
    :return: The prepared runtime rules.
    :rtype: list[_PreparedRule]
    """
    if not rules:
        return []
    return [
        _PreparedRule(
            kind=_RuleKind.CARDINALITY,
            scope_path=f"{scope_label} cardinality_rules[{index}]",
            predicate=rule.when,
            fields=tuple(rule.fields),
            min=rule.min,
            max=rule.max,
            message=rule.message,
        )
        for index, rule in enumerate(rules)
    ]


def _prepare_fail_rules(
    rules: list[FailRule] | None, scope_label: str
) -> list[_PreparedRule]:
    """Convert :class:`FailRule` instances into :class:`_PreparedRule`.

    :param rules: The rule list (or ``None``).
    :type rules: list[FailRule] | None
    :param scope_label: The human-readable scope label used in error paths.
    :type scope_label: str
    :return: The prepared runtime rules.
    :rtype: list[_PreparedRule]
    """
    if not rules:
        return []
    return [
        _PreparedRule(
            kind=_RuleKind.FAIL,
            scope_path=f"{scope_label} fail_when[{index}]",
            predicate=rule.fail_when,
            fields=tuple(rule.error_fields),
            min=None,
            max=None,
            message=rule.message,
        )
        for index, rule in enumerate(rules)
    ]


def _count_present_fields(instance: Any, names: tuple[str, ...]) -> int:
    """Return how many of the named fields are present on ``instance``.

    :param instance: The model instance being evaluated.
    :type instance: Any
    :param names: The field names to count over.
    :type names: tuple[str, ...]
    :return: The count of present fields.
    :rtype: int
    """
    return sum(1 for name in names if _field_is_present(instance, name))


def _format_failure(rule: _PreparedRule, default_template: str) -> str:
    """Return the message string surfaced when ``rule`` fires.

    :param rule: The rule that fired.
    :type rule: _PreparedRule
    :param default_template: The fallback message used when ``rule.message``
        is ``None``.
    :type default_template: str
    :return: The user-visible failure message.
    :rtype: str
    """
    if rule.message:
        return rule.message
    return default_template.format(scope=rule.scope_path)


def _evaluate_field_gate_requires(rule: _PreparedRule, instance: Any) -> str | None:
    """Return the failure message for a ``field_gate_requires`` rule, or ``None``."""
    if rule.predicate is None or not rule.predicate.evaluate(instance):
        return None
    self_name = rule.fields[0]
    if _field_is_present(instance, self_name):
        return None
    return _format_failure(rule, f"{self_name!r} is required (rule {{scope}})")


def _evaluate_field_gate_forbidden(rule: _PreparedRule, instance: Any) -> str | None:
    """Return the failure message for a ``field_gate_forbidden`` rule, or ``None``."""
    if rule.predicate is None or not rule.predicate.evaluate(instance):
        return None
    self_name = rule.fields[0]
    if not _field_is_present(instance, self_name):
        return None
    return _format_failure(rule, f"{self_name!r} must not be set (rule {{scope}})")


def _evaluate_cardinality(rule: _PreparedRule, instance: Any) -> str | None:
    """Return the failure message for a ``cardinality`` rule, or ``None``."""
    if rule.predicate is not None and not rule.predicate.evaluate(instance):
        return None
    count = _count_present_fields(instance, rule.fields)
    if rule.min is not None and count < rule.min:
        return _format_failure(
            rule,
            f"at least {rule.min} of {list(rule.fields)} must be set (rule {{scope}})",
        )
    if rule.max is not None and count > rule.max:
        return _format_failure(
            rule,
            f"at most {rule.max} of {list(rule.fields)} may be set (rule {{scope}})",
        )
    return None


def _evaluate_fail(rule: _PreparedRule, instance: Any) -> str | None:
    """Return the failure message for a ``fail`` rule, or ``None``."""
    if rule.predicate is None or not rule.predicate.evaluate(instance):
        return None
    return _format_failure(rule, "invariant violated (rule {scope})")


_RULE_EVALUATORS = {
    _RuleKind.FIELD_GATE_REQUIRES: _evaluate_field_gate_requires,
    _RuleKind.FIELD_GATE_FORBIDDEN: _evaluate_field_gate_forbidden,
    _RuleKind.CARDINALITY: _evaluate_cardinality,
    _RuleKind.FAIL: _evaluate_fail,
}


def evaluate_conditional_rules(instance: Any, plan: RulePlan) -> list[str]:
    """Evaluate every rule in ``plan`` against ``instance`` and collect failures.

    :param instance: The Pydantic model instance to evaluate.
    :type instance: Any
    :param plan: The pre-extracted :class:`RulePlan`.
    :type plan: RulePlan
    :return: Failure messages, one per rule that fired. An empty list means
        every rule passed.
    :rtype: list[str]
    """
    failures = []
    for rule in plan.rules:
        evaluator = _RULE_EVALUATORS[rule.kind]
        message = evaluator(rule, instance)
        if message is not None:
            failures.append(message)
    return failures


def _validate_plan_against_model_fields(
    plan: RulePlan, cls: type[ConditionalRulesModel]
) -> None:
    """Verify every rule's referenced fields exist on ``cls.model_fields``.

    Performs Tier-3 validation at decoration time so a schema/model
    name mismatch fails fast on module import rather than first form
    submission.

    :param plan: The extracted :class:`RulePlan`.
    :type plan: RulePlan
    :param cls: The :class:`ConditionalRulesModel` subclass being decorated.
    :type cls: type[ConditionalRulesModel]
    :raises TypeError: If any referenced name is missing from
        ``cls.model_fields``.
    """
    model_fields = set(cls.model_fields)

    def _declared_on_model(name: str) -> bool:
        """Return whether ``name``'s leading segment is declared on the model.

        Deliberately not :func:`~app.extensions.apps.framework.responses.root_segment`,
        which strips a ``[N]`` index: that helper serves callers resolving paths
        against serialized rows, where an index is a real segment. Rule
        references are instead walked by :func:`_resolve_field`'s dotted-only
        ``getattr`` traversal. An indexed segment therefore resolves only
        when the model declares that exact name as an attribute — the case the
        first branch below accepts. Every other indexed reference resolves to
        ``None``, so rejecting it here keeps the mistake an import-time error
        rather than a silent no-op on every request.

        :param name: A rule's field reference — a plain name or dotted path.
        :return: Whether the reference's leading segment is a model field.
        """
        if name in model_fields:
            return True
        if "." not in name:
            return False
        return name.split(".", 1)[0] in model_fields

    for rule in plan.rules:
        referenced = set(rule.fields)
        if rule.predicate is not None:
            referenced |= rule.predicate.referenced_fields()
        missing = {name for name in referenced if not _declared_on_model(name)}
        if missing:
            raise TypeError(
                f"@apply_conditional_rules on {cls.__name__}: rule "
                f"{rule.scope_path} references attribute(s) "
                f"{sorted(missing)} that are not declared on "
                f"{cls.__name__}.model_fields. Rule field names must match "
                "the Write-model attribute names, not their aliases."
            )


class ConditionalRulesModel(BaseModel):
    """Provide the base class for plugin ``Write`` models that opt into runtime rule enforcement.

    Subclasses combine inheritance from this base with the
    :func:`apply_conditional_rules` decorator. The decorator extracts a
    :class:`RulePlan` from the supplied :class:`AppSchema` and stores it
    on the class; the inherited ``model_validator`` evaluates the plan on
    every instance.

    App authors who define their own ``model_validator(mode="after")``
    methods inherit this validator first via Pydantic's MRO-based
    collection — conditional rules run before the plugin's own validators
    when both are present.
    """

    __conditional_rules_plan__: ClassVar[RulePlan | None] = None

    @model_validator(mode="after")
    def _apply_conditional_rules(self) -> Self:
        """Evaluate the class's :class:`RulePlan` against the instance.

        :return: The validated instance.
        :rtype: Self
        :raises ValueError: If any conditional rule fires; the message
            joins every failure with ``"; "``.
        """
        plan = type(self).__conditional_rules_plan__
        if plan is None:
            return self
        failures = evaluate_conditional_rules(self, plan)
        if failures:
            raise ValueError("; ".join(failures))
        return self


def apply_conditional_rules(
    schema: AppSchema,
    *,
    entity_name: str | None = None,
) -> Callable[[type[ConditionalRulesModel]], type[ConditionalRulesModel]]:
    """Return a class decorator that wires ``schema``'s rule plan onto the model.

    Performs Tier-3 validation against the decorated class's
    ``model_fields`` at decoration time so a schema/model name mismatch
    fails on module import rather than first form submission. The
    decorator only sets ``__conditional_rules_plan__`` on the class — it
    does not subclass via :func:`type`, so class identity is preserved.

    When ``schema.entities`` is non-empty, pass ``entity_name`` matching one
    entity's ``name`` so the plan includes only that entity's declarative
    rules (see :func:`_extract_rule_plan`).

    :param schema: The plugin schema whose declarative rules drive runtime
        enforcement.
    :param entity_name: Required for multi-entity schemas; must be ``None``
        for legacy task-style schemas.
    :type entity_name: str | None
    :return: A class decorator.
    :rtype: Callable[[type], type]
    :raises TypeError: When the decorated class does not inherit from
        :class:`ConditionalRulesModel`, or any rule references an attribute
        that is not declared on the class.
    :raises ValueError: When ``entity_name`` is incompatible with
        ``schema.entities`` (see :func:`_extract_rule_plan`).
    """
    plan = _extract_rule_plan(schema, entity_name=entity_name)

    def decorator(
        cls: type[ConditionalRulesModel],
    ) -> type[ConditionalRulesModel]:
        if not issubclass(cls, ConditionalRulesModel):
            raise TypeError(
                f"@apply_conditional_rules on {cls.__name__}: class must "
                "inherit from ConditionalRulesModel."
            )
        _validate_plan_against_model_fields(plan, cls)
        cls.__conditional_rules_plan__ = plan
        return cls

    return decorator
