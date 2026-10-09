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

"""Unit tests for the conditional-rule primitive DSL, evaluator, and wiring."""

import re
from collections.abc import Callable
from enum import IntEnum
from typing import Annotated, Literal, Self

import pytest
from pydantic import BaseModel, create_model, Field, model_validator, ValidationError

from app.extensions.apps.framework.rules import (
    _extract_rule_plan,
    _PreparedRule,
    _resolve_field,
    _RuleKind,
    _validate_plan_against_model_fields,
    absent,
    all_,
    all_equal,
    all_falsy,
    all_present,
    all_truthy,
    AllEqual,
    AllFalsy,
    AllPresent,
    AllTruthy,
    And,
    any_,
    any_falsy,
    any_present,
    any_truthy,
    AnyFalsy,
    AnyPresent,
    AnyTruthy,
    apply_conditional_rules,
    CardinalityRule,
    ConditionalRulesModel,
    Contains,
    contains,
    Equals,
    evaluate_conditional_rules,
    extract_forbidden_field_gate_plan,
    extract_required_field_gate_plan,
    F,
    FailRule,
    Falsy,
    falsy,
    FieldExpr,
    FieldGate,
    Gte,
    Lt,
    Lte,
    none_present,
    NonePresent,
    Not,
    not_,
    NotEquals,
    Or,
    Predicate,
    present,
    RulePlan,
    Truthy,
    truthy,
    value_is_present,
    Xor,
    xor_,
)
from app.extensions.apps.framework.schema import (
    AppEntitySchema,
    AppSchema,
    Column,
    DetailView,
    FormSection,
    ListView,
    OneOfBranch,
    OneOfGroup,
    StringField,
)


def _build_schema(
    field: StringField,
    *,
    section_cardinality: list[CardinalityRule] | None = None,
    section_fail: list[FailRule] | None = None,
    schema_cardinality: list[CardinalityRule] | None = None,
    schema_fail: list[FailRule] | None = None,
    extra_fields: list[StringField] | None = None,
) -> AppSchema:
    """Build a one-section schema around ``field`` with optional rule sets."""
    section_fields = [field]
    if extra_fields:
        section_fields.extend(extra_fields)
    return AppSchema(
        name="test",
        display_name="Test",
        forms=[
            FormSection(
                title="S",
                fields=section_fields,
                cardinality_rules=section_cardinality,
                fail_when=section_fail,
            ),
        ],
        list_view=ListView(columns=[Column(key="id", label="ID")]),
        cardinality_rules=schema_cardinality,
        fail_when=schema_fail,
    )


# ── Layer A: FieldExpr operator overloads ────────────────────────────────


class TestFieldExpr:
    """Verify operator overloads on :class:`FieldExpr`."""

    def test_eq_with_literal_returns_equals_predicate(self) -> None:
        """Eq with literal returns equals predicate."""
        predicate = F("a") == "v"

        assert isinstance(predicate, Equals)
        assert predicate.to_dict() == {"equals": {"a": "v"}}
        assert predicate.referenced_fields() == {"a"}

    def test_ne_with_literal_returns_not_equals_predicate(self) -> None:
        """Ne with literal returns not equals predicate."""
        predicate = F("a") != "v"

        assert isinstance(predicate, NotEquals)
        assert predicate.to_dict() == {"not_equals": {"a": "v"}}
        assert predicate.referenced_fields() == {"a"}

    def test_eq_between_field_exprs_is_binary_all_equal(self) -> None:
        """Eq between field exprs is binary all equal."""
        predicate = F("a") == F("b")

        assert isinstance(predicate, AllEqual)
        assert predicate.to_dict() == {"all_equal": ["a", "b"]}
        assert predicate.referenced_fields() == {"a", "b"}

    def test_ne_between_field_exprs_wraps_all_equal_in_not(self) -> None:
        """Ne between field exprs wraps all equal in not."""
        predicate = F("a") != F("b")

        assert predicate.to_dict() == {"not": {"all_equal": ["a", "b"]}}
        assert predicate.referenced_fields() == {"a", "b"}

    @pytest.mark.parametrize(
        ("op", "wire_op"),
        [
            (lambda lhs, rhs: lhs > rhs, "gt"),
            (lambda lhs, rhs: lhs >= rhs, "gte"),
            (lambda lhs, rhs: lhs < rhs, "lt"),
            (lambda lhs, rhs: lhs <= rhs, "lte"),
        ],
    )
    def test_ordered_comparison_with_literal_emits_wire_op(
        self, op, wire_op: str
    ) -> None:
        """Ordered comparison with literal emits wire op."""
        rhs = 5
        predicate = op(F("a"), rhs)

        assert predicate.to_dict() == {wire_op: {"a": rhs}}
        assert predicate.referenced_fields() == {"a"}

    @pytest.mark.parametrize(
        ("op", "wire_op"),
        [
            (lambda lhs, rhs: lhs > rhs, "gt"),
            (lambda lhs, rhs: lhs >= rhs, "gte"),
            (lambda lhs, rhs: lhs < rhs, "lt"),
            (lambda lhs, rhs: lhs <= rhs, "lte"),
        ],
    )
    def test_field_to_field_ordered_comparison_uses_field_ref_wrapper(
        self, op, wire_op: str
    ) -> None:
        """Field to field ordered comparison uses field ref wrapper."""
        predicate = op(F("a"), F("b"))

        assert predicate.to_dict() == {wire_op: {"a": {"$field": "b"}}}
        assert predicate.referenced_fields() == {"a", "b"}

    @pytest.mark.parametrize(
        "op_fn",
        [
            lambda lhs, rhs: lhs & rhs,
            lambda lhs, rhs: lhs | rhs,
            lambda lhs, rhs: lhs ^ rhs,
        ],
    )
    def test_boolean_combination_of_field_exprs_raises(self, op_fn) -> None:
        """Boolean combination of field exprs raises."""
        with pytest.raises(TypeError, match="FieldExpr cannot be combined"):
            op_fn(F("a"), F("b"))

    def test_invert_of_field_expr_raises(self) -> None:
        """Invert of field expr raises."""
        with pytest.raises(TypeError, match="FieldExpr cannot be combined"):
            ~F("a")

    def test_field_expr_rejects_empty_name(self) -> None:
        """Field expr rejects empty name."""
        with pytest.raises(TypeError, match="non-empty string"):
            FieldExpr("")

    def test_field_expr_repr_is_readable(self) -> None:
        """Field expr repr is readable."""
        assert repr(F("foo")) == "F('foo')"


# ── Layer A: Predicate construction guards ──────────────────────────────


class TestPredicateGuards:
    """Verify :class:`Predicate` defensive guards trigger loudly."""

    def test_bool_on_predicate_raises(self) -> None:
        """Bool on predicate raises."""
        predicate = F("a") == "v"

        with pytest.raises(TypeError, match="cannot be used with 'and', 'or'"):
            bool(predicate)

    def test_implicit_truthiness_in_if_statement_raises(self) -> None:
        """Implicit truthiness in if statement raises."""
        predicate = F("a") == "v"

        with pytest.raises(TypeError, match="cannot be used"):
            bool(predicate)

    def test_python_and_keyword_raises(self) -> None:
        """Python and keyword raises."""
        p1 = truthy("a")
        p2 = truthy("b")

        with pytest.raises(TypeError, match="cannot be used"):
            _ = p1 and p2

    def test_python_or_keyword_raises(self) -> None:
        """Python or keyword raises."""
        p1 = truthy("a")
        p2 = truthy("b")

        with pytest.raises(TypeError, match="cannot be used"):
            _ = p1 or p2

    def test_python_not_keyword_raises(self) -> None:
        """Python not keyword raises."""
        with pytest.raises(TypeError, match="cannot be used"):
            _ = not truthy("a")


# ── Layer A: Helper functions and composition ───────────────────────────


class TestSingleFieldHelpers:
    """Verify single-field predicate helper functions."""

    def test_truthy_emits_truthy_wire_shape(self) -> None:
        """Truthy emits truthy wire shape."""
        predicate = truthy("a")

        assert isinstance(predicate, Truthy)
        assert predicate.to_dict() == {"truthy": "a"}
        assert predicate.referenced_fields() == {"a"}

    def test_falsy_emits_falsy_wire_shape(self) -> None:
        """Falsy emits falsy wire shape."""
        predicate = falsy("a")

        assert isinstance(predicate, Falsy)
        assert predicate.to_dict() == {"falsy": "a"}

    def test_present_is_any_present_of_single_field(self) -> None:
        """Present is any present of single field."""
        predicate = present("a")

        assert isinstance(predicate, AnyPresent)
        assert predicate.to_dict() == {"any_present": ["a"]}

    def test_absent_is_none_present_of_single_field(self) -> None:
        """Absent is none present of single field."""
        predicate = absent("a")

        assert isinstance(predicate, NonePresent)
        assert predicate.to_dict() == {"none_present": ["a"]}


class TestMultiFieldHelpers:
    """Verify multi-field predicate helpers."""

    @pytest.mark.parametrize(
        ("helper", "klass", "wire_op"),
        [
            (all_truthy, AllTruthy, "all_truthy"),
            (all_falsy, AllFalsy, "all_falsy"),
            (any_truthy, AnyTruthy, "any_truthy"),
            (any_falsy, AnyFalsy, "any_falsy"),
            (any_present, AnyPresent, "any_present"),
            (all_present, AllPresent, "all_present"),
            (none_present, NonePresent, "none_present"),
        ],
    )
    def test_helper_returns_expected_class_and_wire_shape(
        self, helper, klass, wire_op: str
    ) -> None:
        """Helper returns expected class and wire shape."""
        predicate = helper("a", "b", "c")

        assert isinstance(predicate, klass)
        assert predicate.to_dict() == {wire_op: ["a", "b", "c"]}
        assert predicate.referenced_fields() == {"a", "b", "c"}

    def test_all_equal_requires_at_least_two_names(self) -> None:
        """All equal requires at least two names."""
        with pytest.raises(ValueError, match="at least 2 field"):
            all_equal("a")

    def test_all_equal_with_two_names_emits_n_ary_shape(self) -> None:
        """All equal with two names emits n ary shape."""
        predicate = all_equal("a", "b")

        assert isinstance(predicate, AllEqual)
        assert predicate.to_dict() == {"all_equal": ["a", "b"]}


class TestCompositionHelpers:
    """Verify boolean composition: AND, OR, XOR, NOT."""

    def test_and_via_operator(self) -> None:
        """And via operator."""
        predicate = truthy("a") & truthy("b")

        assert isinstance(predicate, And)
        assert predicate.to_dict() == {"all": [{"truthy": "a"}, {"truthy": "b"}]}

    def test_all_helper_collects_all_args_in_one_and(self) -> None:
        """All helper collects all args in one and."""
        predicate = all_(truthy("a"), truthy("b"), truthy("c"))

        assert isinstance(predicate, And)
        assert predicate.to_dict() == {
            "all": [{"truthy": "a"}, {"truthy": "b"}, {"truthy": "c"}]
        }

    def test_or_via_operator(self) -> None:
        """Or via operator."""
        predicate = truthy("a") | truthy("b")

        assert isinstance(predicate, Or)
        assert predicate.to_dict() == {"any": [{"truthy": "a"}, {"truthy": "b"}]}

    def test_any_helper_collects_all_args_in_one_or(self) -> None:
        """Any helper collects all args in one or."""
        predicate = any_(truthy("a"), truthy("b"), truthy("c"))

        assert predicate.to_dict() == {
            "any": [{"truthy": "a"}, {"truthy": "b"}, {"truthy": "c"}]
        }

    def test_xor_via_operator(self) -> None:
        """Xor via operator."""
        predicate = truthy("a") ^ truthy("b")

        assert isinstance(predicate, Xor)
        assert predicate.to_dict() == {"xor": [{"truthy": "a"}, {"truthy": "b"}]}

    def test_xor_helper_with_three_args_raises(self) -> None:
        """Xor helper with three args raises."""
        with pytest.raises(ValueError, match="exactly 2 arguments"):
            xor_(truthy("a"), truthy("b"), truthy("c"))

    def test_not_via_operator(self) -> None:
        """Not via operator."""
        predicate = ~truthy("a")

        assert isinstance(predicate, Not)
        assert predicate.to_dict() == {"not": {"truthy": "a"}}

    def test_not_helper_equivalent_to_invert(self) -> None:
        """Not helper equivalent to invert."""
        assert not_(truthy("a")).to_dict() == (~truthy("a")).to_dict()

    def test_all_helper_rejects_no_args(self) -> None:
        """All helper rejects no args."""
        with pytest.raises(ValueError, match="at least one"):
            all_()

    def test_any_helper_rejects_no_args(self) -> None:
        """Any helper rejects no args."""
        with pytest.raises(ValueError, match="at least one"):
            any_()

    def test_referenced_fields_is_union_over_children(self) -> None:
        """Referenced fields is union over children."""
        predicate = all_(truthy("a"), any_(truthy("b"), truthy("c")))

        assert predicate.referenced_fields() == {"a", "b", "c"}


# ── Layer A: evaluate() per Predicate ───────────────────────────────────


class _Instance:
    """Tiny attribute-bearing object used to drive predicate evaluation."""

    def __init__(self, **attrs: object) -> None:
        for key, value in attrs.items():
            setattr(self, key, value)


class TestPredicateEvaluation:
    """Verify ``Predicate.evaluate`` returns the expected boolean."""

    @pytest.mark.parametrize(
        ("predicate", "instance_kwargs", "expected"),
        [
            (Equals("x", "v"), {"x": "v"}, True),
            (Equals("x", "v"), {"x": "w"}, False),
            (Equals("x", "v"), {"x": None}, False),
            (NotEquals("x", "v"), {"x": "w"}, True),
            (NotEquals("x", "v"), {"x": "v"}, False),
            (NotEquals("x", "v"), {"x": None}, True),
            (Truthy("x"), {"x": "yes"}, True),
            (Truthy("x"), {"x": ""}, False),
            (Truthy("x"), {"x": 0}, False),
            (Truthy("x"), {"x": None}, False),
            (Falsy("x"), {"x": ""}, True),
            (Falsy("x"), {"x": "yes"}, False),
            (Falsy("x"), {"x": None}, True),
        ],
    )
    def test_simple_predicate_evaluation(
        self,
        predicate: Predicate,
        instance_kwargs: dict,
        *,
        expected: bool,
    ) -> None:
        """Simple predicate evaluation."""
        assert predicate.evaluate(_Instance(**instance_kwargs)) is expected

    def test_gt_with_two_literals_evaluates(self) -> None:
        """Gt with two literals evaluates."""
        threshold = 5
        predicate = F("x") > threshold

        assert predicate.evaluate(_Instance(x=threshold + 1)) is True
        assert predicate.evaluate(_Instance(x=threshold)) is False
        assert predicate.evaluate(_Instance(x=threshold - 1)) is False

    def test_gt_with_field_ref_evaluates(self) -> None:
        """Gt with field ref evaluates."""
        higher, lower = 10, 5
        predicate = F("x") > F("y")

        assert predicate.evaluate(_Instance(x=higher, y=lower)) is True
        assert predicate.evaluate(_Instance(x=lower, y=higher)) is False

    def test_gt_with_none_operand_returns_false(self) -> None:
        """``None > anything`` would raise; the evaluator returns False instead."""
        threshold = 5
        predicate = F("x") > threshold

        assert predicate.evaluate(_Instance(x=None)) is False

    @pytest.mark.parametrize(
        ("operator", "literal", "value", "expected"),
        [
            (Gte, 5, 5, True),
            (Gte, 5, 4, False),
            (Lt, 5, 4, True),
            (Lt, 5, 5, False),
            (Lte, 5, 5, True),
            (Lte, 5, 6, False),
        ],
    )
    def test_other_ordered_comparisons(
        self,
        operator: type,
        literal: int,
        value: int,
        *,
        expected: bool,
    ) -> None:
        """Other ordered comparisons."""
        predicate = operator("x", literal)

        assert predicate.evaluate(_Instance(x=value)) is expected

    def test_any_present_with_one_present_field(self) -> None:
        """Any present with one present field."""
        predicate = any_present("a", "b")

        assert predicate.evaluate(_Instance(a=None, b="x")) is True
        assert predicate.evaluate(_Instance(a=None, b="")) is False

    def test_all_present_requires_all_fields_present(self) -> None:
        """All present requires all fields present."""
        predicate = all_present("a", "b")

        assert predicate.evaluate(_Instance(a="x", b="y")) is True
        assert predicate.evaluate(_Instance(a="x", b="")) is False

    def test_none_present_means_no_listed_field_is_present(self) -> None:
        """None present means no listed field is present."""
        predicate = none_present("a", "b")

        assert predicate.evaluate(_Instance(a=None, b="")) is True
        assert predicate.evaluate(_Instance(a="x", b="")) is False

    def test_bool_false_is_treated_as_absent(self) -> None:
        """``False`` does not count as present.

        Bool defaults do not trip ``forbidden=`` :class:`FieldGate` entries on
        :class:`BoolField`. Only an explicit ``True`` toggle is considered set,
        matching the ``FailRule(fail_when=truthy(name))`` convention used for
        mode bools.
        """
        assert any_present("a").evaluate(_Instance(a=False)) is False
        assert any_present("a").evaluate(_Instance(a=True)) is True
        assert none_present("a").evaluate(_Instance(a=False)) is True
        assert none_present("a").evaluate(_Instance(a=True)) is False

    def test_int_zero_remains_present(self) -> None:
        """Numeric ``0`` stays present even though it is falsy.

        Int fields with a meaningful zero value (e.g.
        ``rsync_compression_level=0``) still satisfy presence-based gates.
        """
        assert any_present("a").evaluate(_Instance(a=0)) is True

    def test_all_truthy_evaluates_python_truthiness(self) -> None:
        """All truthy evaluates python truthiness."""
        predicate = all_truthy("a", "b")

        assert predicate.evaluate(_Instance(a=1, b="y")) is True
        assert predicate.evaluate(_Instance(a=1, b=0)) is False

    def test_all_falsy_evaluates_python_truthiness(self) -> None:
        """All falsy evaluates python truthiness."""
        predicate = all_falsy("a", "b")

        assert predicate.evaluate(_Instance(a=0, b="")) is True
        assert predicate.evaluate(_Instance(a=0, b=1)) is False

    def test_any_truthy_at_least_one(self) -> None:
        """Any truthy at least one."""
        predicate = any_truthy("a", "b")

        assert predicate.evaluate(_Instance(a=0, b=1)) is True
        assert predicate.evaluate(_Instance(a=0, b=0)) is False

    def test_any_falsy_at_least_one(self) -> None:
        """Any falsy at least one."""
        predicate = any_falsy("a", "b")

        assert predicate.evaluate(_Instance(a=1, b=0)) is True
        assert predicate.evaluate(_Instance(a=1, b=1)) is False

    def test_all_equal_compares_listed_fields_pairwise(self) -> None:
        """All equal compares listed fields pairwise."""
        predicate = all_equal("a", "b", "c")

        assert predicate.evaluate(_Instance(a=1, b=1, c=1)) is True
        assert predicate.evaluate(_Instance(a=1, b=1, c=2)) is False

    def test_xor_returns_true_iff_exactly_one_child_matches(self) -> None:
        """Xor returns true iff exactly one child matches."""
        predicate = xor_(truthy("a"), truthy("b"))

        assert predicate.evaluate(_Instance(a=1, b=0)) is True
        assert predicate.evaluate(_Instance(a=0, b=1)) is True
        assert predicate.evaluate(_Instance(a=1, b=1)) is False
        assert predicate.evaluate(_Instance(a=0, b=0)) is False

    def test_nested_composition_evaluates_correctly(self) -> None:
        """Nested composition evaluates correctly."""
        predicate = all_(truthy("a"), any_(truthy("b"), truthy("c")))

        assert predicate.evaluate(_Instance(a=1, b=0, c=1)) is True
        assert predicate.evaluate(_Instance(a=1, b=0, c=0)) is False
        assert predicate.evaluate(_Instance(a=0, b=1, c=1)) is False

    def test_contains_matches_list_member(self) -> None:
        """Contains matches when value is in the list."""
        predicate = contains("upload", "s3")

        assert predicate.evaluate(_Instance(upload=["s3", "rsync"])) is True
        assert predicate.evaluate(_Instance(upload=["rsync"])) is False

    def test_contains_with_empty_or_none_container(self) -> None:
        """Empty list and ``None`` container both evaluate False."""
        predicate = contains("upload", "s3")

        assert predicate.evaluate(_Instance(upload=[])) is False
        assert predicate.evaluate(_Instance(upload=None)) is False

    def test_contains_normalizes_enum_member(self) -> None:
        """An enum literal matches its underlying value inside the list."""

        class _Provider(IntEnum):
            S3 = 1
            RSYNC = 2

        predicate = Contains("upload", _Provider.S3)

        assert predicate.evaluate(_Instance(upload=[_Provider.S3])) is True
        assert predicate.evaluate(_Instance(upload=[1])) is True
        assert predicate.evaluate(_Instance(upload=[_Provider.RSYNC])) is False

    def test_contains_handles_non_iterable_container(self) -> None:
        """A non-iterable / scalar value at the field returns False rather than raising."""
        predicate = contains("upload", "s3")

        assert predicate.evaluate(_Instance(upload=42)) is False

    def test_contains_rejects_str_bytes_and_mapping(self) -> None:
        """str/bytes/mapping containers return False — list-membership only, matching the frontend."""
        predicate = contains("upload", "s3")

        assert predicate.evaluate(_Instance(upload="s3")) is False
        assert predicate.evaluate(_Instance(upload="s3rsync")) is False
        assert predicate.evaluate(_Instance(upload=b"s3")) is False
        assert predicate.evaluate(_Instance(upload={"s3": True})) is False

    def test_contains_to_dict_and_referenced_fields(self) -> None:
        """Wire shape is ``{contains: {field: value}}`` and references the field."""
        predicate = contains("upload", "s3")

        assert predicate.to_dict() == {"contains": {"upload": "s3"}}
        assert predicate.referenced_fields() == {"upload"}


# ── Layer A: edge cases for field presence semantics ────────────────────


class TestEdgeCases:
    """Verify the failure-prone semantics around None / empty / zero values."""

    def test_none_gating_field(self) -> None:
        """None gating field."""
        instance = _Instance(x=None)

        assert truthy("x").evaluate(instance) is False
        assert falsy("x").evaluate(instance) is True
        assert any_present("x").evaluate(instance) is False
        assert all_present("x").evaluate(instance) is False
        assert (F("x") == "v").evaluate(instance) is False
        assert (F("x") != "v").evaluate(instance) is True

    def test_empty_string_gating_field(self) -> None:
        """Empty string gating field."""
        instance = _Instance(x="")

        assert truthy("x").evaluate(instance) is False
        assert any_present("x").evaluate(instance) is False
        assert (F("x") == "").evaluate(instance) is True

    def test_zero_int_distinguished_from_unset(self) -> None:
        """Zero int distinguished from unset."""
        zero = _Instance(x=0)
        unset = _Instance(x=None)

        assert truthy("x").evaluate(zero) is False
        assert any_present("x").evaluate(zero) is True
        assert any_present("x").evaluate(unset) is False
        assert (F("x") == 0).evaluate(zero) is True
        assert (F("x") == 0).evaluate(unset) is False

    def test_false_bool_treated_as_absent_like_unset(self) -> None:
        """``False`` is the unset bool default and so registers as absent.

        Pins the convention used by ``forbidden=`` :class:`FieldGate` entries
        on :class:`BoolField`: the gate fires only on an explicit ``True``
        toggle, never on the legitimate default. ``truthy`` already returned
        ``False`` here; presence-based predicates now agree.
        """
        false = _Instance(x=False)
        unset = _Instance(x=None)

        assert truthy("x").evaluate(false) is False
        assert any_present("x").evaluate(false) is False
        assert any_present("x").evaluate(unset) is False

    def test_empty_collection_treated_as_absent(self) -> None:
        """Empty collection treated as absent."""
        for empty_value in ([], {}, set(), ""):
            instance = _Instance(x=empty_value)
            assert any_present("x").evaluate(instance) is False
            assert truthy("x").evaluate(instance) is False

    def test_intenum_equals_plain_int(self) -> None:
        """Intenum equals plain int."""

        class Mode(IntEnum):
            DSN = 1
            NONE = 2

        instance = _Instance(x=1)

        assert (F("x") == Mode.DSN).evaluate(instance) is True
        assert (F("x") == Mode.NONE).evaluate(instance) is False

    def test_intenum_equals_serializes_to_int(self) -> None:
        """Intenum equals serializes to int."""

        class Mode(IntEnum):
            DSN = 1

        predicate = F("x") == Mode.DSN

        assert predicate.to_dict() == {"equals": {"x": 1}}

    def test_field_to_field_eq_is_binary_all_equal(self) -> None:
        """Field to field eq is binary all equal."""
        binary_eq = F("a") == F("b")
        n_ary_helper = all_equal("a", "b")

        assert binary_eq.to_dict() == n_ary_helper.to_dict()


# ── Layer B: rule envelope shape validation ──────────────────────────────


class TestCardinalityRuleShape:
    """Verify Tier-1 cardinality bound checks fire at the rule envelope."""

    def test_empty_fields_rejected(self) -> None:
        """Empty fields rejected."""
        with pytest.raises(ValidationError, match="non-empty `fields`"):
            CardinalityRule(when=truthy("a"), fields=[], min=1)

    def test_no_bounds_rejected(self) -> None:
        """No bounds rejected."""
        with pytest.raises(ValidationError, match="no constraint specified"):
            CardinalityRule(when=truthy("a"), fields=["x"])

    def test_negative_min_rejected(self) -> None:
        """Negative min rejected."""
        with pytest.raises(ValidationError, match="non-negative"):
            CardinalityRule(when=truthy("a"), fields=["x"], min=-1)

    def test_negative_max_rejected(self) -> None:
        """Negative max rejected."""
        with pytest.raises(ValidationError, match="non-negative"):
            CardinalityRule(when=truthy("a"), fields=["x"], max=-1)

    def test_min_greater_than_max_rejected(self) -> None:
        """Min greater than max rejected."""
        with pytest.raises(ValidationError, match="must be <="):
            CardinalityRule(when=truthy("a"), fields=["x"], min=2, max=1)

    def test_min_greater_than_field_count_rejected(self) -> None:
        """Min greater than field count rejected."""
        with pytest.raises(ValidationError, match="exceeds number"):
            CardinalityRule(when=truthy("a"), fields=["x", "y"], min=3)


class TestRuleEnvelopeSerialization:
    """Verify rule envelopes serialize to snake_case via Pydantic."""

    def test_field_gate_dump(self) -> None:
        """Field gate dump."""
        gate = FieldGate(when=F("x") == "v", message="must be v")

        assert gate.model_dump(mode="json") == {
            "when": {"equals": {"x": "v"}},
            "message": "must be v",
        }

    def test_cardinality_rule_dump(self) -> None:
        """Cardinality rule dump."""
        rule = CardinalityRule(
            when=truthy("a"),
            fields=["x", "y"],
            min=1,
            max=2,
            message="bounds",
        )

        assert rule.model_dump(mode="json") == {
            "when": {"truthy": "a"},
            "fields": ["x", "y"],
            "min": 1,
            "max": 2,
            "message": "bounds",
        }

    def test_cardinality_rule_unconditional_when_is_null(self) -> None:
        """Cardinality rule unconditional when is null."""
        rule = CardinalityRule(when=None, fields=["x", "y"], max=1)

        assert rule.model_dump(mode="json")["when"] is None

    def test_fail_rule_dump(self) -> None:
        """Fail rule dump."""
        rule = FailRule(
            fail_when=truthy("x"),
            error_fields=["x"],
            message="fails",
        )

        assert rule.model_dump(mode="json") == {
            "fail_when": {"truthy": "x"},
            "error_fields": ["x"],
            "message": "fails",
        }

    def test_dict_input_for_when_field_rejected(self) -> None:
        """Dict input for when field rejected."""
        with pytest.raises(TypeError, match="Predicate instance"):
            FieldGate.model_validate(
                {"when": {"truthy": "x"}, "message": None},
            )


# ── Layer C: ConditionalRulesModel + decorator ──────────────────────────


class TestConditionalRulesModel:
    """Verify the base class + decorator wiring."""

    def test_no_decorator_is_noop(self) -> None:
        """No decorator is noop."""

        class Plain(ConditionalRulesModel):
            x: str = ""

        Plain(x="anything")  # no rule plan; validates without enforcement

    def test_decorator_requires_conditional_rules_model_base(self) -> None:
        """Decorator requires conditional rules model base."""
        schema = _build_schema(StringField(name="x", label="X"))

        with pytest.raises(TypeError, match="ConditionalRulesModel"):

            @apply_conditional_rules(schema)
            class _Bad(BaseModel):
                x: str = ""

    def test_decorator_preserves_class_identity(self) -> None:
        """Decorator preserves class identity."""
        schema = _build_schema(StringField(name="x", label="X"))

        class Body(ConditionalRulesModel):
            x: str = ""

        decorated = apply_conditional_rules(schema)(Body)

        assert decorated is Body
        assert Body.__conditional_rules_plan__ is not None

    def test_empty_rule_plan_is_noop(self) -> None:
        """Empty rule plan is noop."""
        schema = _build_schema(StringField(name="x", label="X"))

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            x: str = ""

        instance = Body(x="ok")

        assert instance.x == "ok"

    def test_unknown_attribute_on_write_model_raises_at_import(self) -> None:
        """Unknown attribute on write model raises at import."""
        schema = _build_schema(
            StringField(
                name="x",
                label="X",
                requires=[FieldGate(when=F("missing_attr") == "v")],
            ),
            extra_fields=[StringField(name="missing_attr", label="M")],
        )

        with pytest.raises(TypeError, match="missing_attr"):

            @apply_conditional_rules(schema)
            class _Body(ConditionalRulesModel):
                x: str = ""
                # `missing_attr` is referenced by the rule but not declared
                # on the Write model

    def test_camelcase_alias_in_rule_rejected(self) -> None:
        """Rule must reference the Python attribute name, not its alias."""
        schema = _build_schema(
            StringField(
                name="recursionMethod",
                label="M",
                requires=[FieldGate(when=F("recursionMethod") == "dsn")],
            ),
        )

        with pytest.raises(TypeError, match="recursionMethod"):

            @apply_conditional_rules(schema)
            class _Body(ConditionalRulesModel):
                recursion_method: str = ""

    def test_inherited_rule_validator_runs_before_subclass_validator(
        self,
    ) -> None:
        """Pydantic v2 collects ``mode='after'`` validators in MRO order."""
        schema = _build_schema(
            StringField(
                name="x",
                label="X",
                requires=[FieldGate(when=truthy("y"))],
            ),
            extra_fields=[StringField(name="y", label="Y")],
        )

        observed = []

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            x: str = ""
            y: str = ""

            @model_validator(mode="after")
            def _track_subclass_run(self) -> Self:
                observed.append("subclass")
                return self

        # Rule passes (y is empty so the requires gate doesn't fire); the
        # plugin-defined validator runs after the inherited rule check.
        Body(x="", y="")

        assert observed == ["subclass"]

        # When the rule fails, the plugin-defined validator does NOT run
        # because the inherited validator raises first.
        observed.clear()
        with pytest.raises(ValidationError):
            Body(x="", y="something")
        assert observed == []


class _IndexedRefBody(ConditionalRulesModel):
    """Declare ``items`` without any bracket-indexed attribute."""

    x: str = ""
    items: list[str] = Field(default_factory=list)


def _predicate_plan(name: str) -> RulePlan:
    """Return a one-rule plan whose gating predicate references ``name``."""
    return RulePlan(
        rules=(
            _PreparedRule(
                kind=_RuleKind.FAIL,
                scope_path="AppSchema 'test'",
                predicate=F(name) == "v",
                fields=(),
                min=None,
                max=None,
                message=None,
            ),
        )
    )


def _cardinality_plan(name: str) -> RulePlan:
    """Return a one-rule plan whose ``fields`` target is ``name``."""
    return RulePlan(
        rules=(
            _PreparedRule(
                kind=_RuleKind.CARDINALITY,
                scope_path="AppSchema 'test'",
                predicate=None,
                fields=(name,),
                min=1,
                max=None,
                message=None,
            ),
        )
    )


class TestDeclaredOnModelIndexHandling:
    """Pin the decoration-time gate's rejection of bracket-indexed references.

    A rule reference reaches this gate only after ``AppSchema`` has matched it
    against the form tree's declared field names, and those names admit no
    brackets, so an indexed reference cannot arrive through a schema that
    validates — ``test_bracket_indexed_field_in_rule_rejected`` pins that outer
    half. These cases therefore drive ``_validate_plan_against_model_fields``
    directly. The gate rejects an indexed reference anyway, and must keep doing
    so: ``_resolve_field`` walks paths with plain dotted ``getattr``, so one
    allowed through here would silently evaluate to ``None`` on every request
    rather than failing at import.

    ``test_unknown_attribute_on_write_model_raises_at_import`` covers the
    decoration route that feeds this gate its reference strings.
    """

    @pytest.mark.parametrize(
        ("build_plan", "reference"),
        [
            (_predicate_plan, "items[0]"),
            (_predicate_plan, "items[0].sub_field"),
            (_cardinality_plan, "items[0]"),
        ],
        ids=["predicate_bare", "predicate_dotted", "cardinality_fields"],
    )
    def test_indexed_reference_rejected(
        self, build_plan: Callable[[str], RulePlan], reference: str
    ) -> None:
        """Reject a bracket-indexed reference wherever a rule can carry one."""
        with pytest.raises(TypeError, match=re.escape(reference)):
            _validate_plan_against_model_fields(build_plan(reference), _IndexedRefBody)

    def test_indexed_reference_that_is_a_literal_field_accepted(self) -> None:
        """Accept ``items[0]`` when it is itself a declared field name.

        Only ``create_model`` can declare that name — it is not an identifier,
        so no hand-written model reaches this branch. It stays covered because
        it is the sole exemption from the rejection above.
        """
        body = create_model(
            "_LiteralIndexBody",
            __base__=ConditionalRulesModel,
            **{"items[0]": (str, "")},
        )

        # No raise: the reference is itself a declared field name.
        _validate_plan_against_model_fields(_predicate_plan("items[0]"), body)

    def test_plain_dotted_reference_with_declared_root_accepted(self) -> None:
        """Accept a dotted path whose leading segment is a declared field."""

        class Source(BaseModel):
            mode: str = ""

        class Body(ConditionalRulesModel):
            source: Source = Source()

        # No raise: the leading segment resolves to a declared field.
        _validate_plan_against_model_fields(_predicate_plan("source.mode"), Body)


# ── Layer C: cardinality semantics per pattern ──────────────────────────


class TestCardinalityPatterns:
    """Verify every documented cardinality encoding fires correctly."""

    def _make_body(self, rule: CardinalityRule, *fields: str) -> type:
        """Make body."""
        schema_fields = [StringField(name=name, label=name) for name in fields]
        schema = AppSchema(
            name="test",
            display_name="T",
            forms=[FormSection(title="S", fields=schema_fields)],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
            cardinality_rules=[rule],
        )

        annotations = dict.fromkeys(fields, str)
        defaults = dict.fromkeys(fields, "")

        body = type(
            "Body",
            (ConditionalRulesModel,),
            {"__annotations__": annotations, **defaults},
        )
        return apply_conditional_rules(schema)(body)

    def test_min_one_at_least_one_of(self) -> None:
        """Min one at least one of."""
        body = self._make_body(
            CardinalityRule(when=None, fields=["a", "b"], min=1),
            "a",
            "b",
        )

        body(a="set", b="")
        body(a="", b="set")
        body(a="set", b="set")
        with pytest.raises(ValidationError):
            body(a="", b="")

    def test_min_n_all_of(self) -> None:
        """Min n all of."""
        body = self._make_body(
            CardinalityRule(when=None, fields=["a", "b"], min=2),
            "a",
            "b",
        )

        body(a="set", b="set")
        with pytest.raises(ValidationError):
            body(a="set", b="")
        with pytest.raises(ValidationError):
            body(a="", b="")

    def test_min_one_max_one_exactly_one(self) -> None:
        """Min one max one exactly one."""
        body = self._make_body(
            CardinalityRule(when=None, fields=["a", "b"], min=1, max=1),
            "a",
            "b",
        )

        body(a="set", b="")
        body(a="", b="set")
        with pytest.raises(ValidationError):
            body(a="", b="")
        with pytest.raises(ValidationError):
            body(a="set", b="set")

    def test_max_zero_none_of(self) -> None:
        """Max zero none of."""
        body = self._make_body(
            CardinalityRule(when=None, fields=["a", "b"], max=0),
            "a",
            "b",
        )

        body(a="", b="")
        with pytest.raises(ValidationError):
            body(a="set", b="")

    def test_max_one_at_most_one(self) -> None:
        """Max one at most one."""
        body = self._make_body(
            CardinalityRule(when=None, fields=["a", "b"], max=1),
            "a",
            "b",
        )

        body(a="", b="")
        body(a="set", b="")
        body(a="", b="set")
        with pytest.raises(ValidationError):
            body(a="set", b="set")

    def test_predicate_gates_cardinality_rule(self) -> None:
        """Predicate gates cardinality rule."""
        body = self._make_body(
            CardinalityRule(
                when=truthy("gate"),
                fields=["a", "b"],
                min=1,
            ),
            "gate",
            "a",
            "b",
        )

        # gate is falsy → rule does not fire even if a/b unset
        body(gate="", a="", b="")
        # gate is truthy → rule fires; a or b must be set
        body(gate="on", a="set", b="")
        with pytest.raises(ValidationError):
            body(gate="on", a="", b="")


# ── Worked declarative-DSL equivalents from the plan ────────────────────


class TestWorkedExamples:
    """Verify the 6 worked declarative-DSL equivalents fire correctly.

    These mirror the imperative validators in archives / alters that are
    now expressible declaratively.
    """

    def _build_dsn_table_body(self) -> type:
        """Build dsn table body."""
        schema = AppSchema(
            name="alters",
            display_name="Alters",
            forms=[
                FormSection(
                    title="Recursion",
                    fields=[
                        StringField(name="recursion_method", label="M"),
                        StringField(
                            name="dsn_table",
                            label="T",
                            requires=[
                                FieldGate(when=F("recursion_method") == "dsn"),
                            ],
                        ),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
        )

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            recursion_method: str = ""
            dsn_table: str = ""

        return Body

    def test_alters_dsn_table_required_when_recursion_method_dsn(self) -> None:
        """Alters dsn table required when recursion method dsn."""
        body = self._build_dsn_table_body()

        # Non-dsn methods: dsn_table not required
        body(recursion_method="processlist", dsn_table="")
        # dsn method: dsn_table required
        body(recursion_method="dsn", dsn_table="D=percona,t=dsns")
        with pytest.raises(ValidationError, match="dsn_table"):
            body(recursion_method="dsn", dsn_table="")

    def test_archives_where_required_unless_swap_drop(self) -> None:
        """Worked example #3 — `where` requires + forbidden mirror."""
        schema = AppSchema(
            name="archives",
            display_name="Archives",
            forms=[
                FormSection(
                    title="Source",
                    fields=[
                        StringField(name="swap_drop", label="SD"),
                        StringField(
                            name="where",
                            label="W",
                            requires=[
                                FieldGate(when=F("swap_drop") != "swap_drop"),
                            ],
                            forbidden=[
                                FieldGate(when=F("swap_drop") == "swap_drop"),
                            ],
                        ),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
        )

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            swap_drop: str = ""
            where: str = ""

        # swap_drop != "swap_drop" → where required
        with pytest.raises(ValidationError, match="where"):
            Body(swap_drop="archive", where="")
        Body(swap_drop="archive", where="WHERE 1=1")

        # swap_drop == "swap_drop" → where forbidden
        with pytest.raises(ValidationError, match="where"):
            Body(swap_drop="swap_drop", where="WHERE 1=1")
        Body(swap_drop="swap_drop", where="")

    def test_archives_tables_are_different_fail_rule(self) -> None:
        """Worked example #4 — predicate-only invariant via FailRule."""
        schema = AppSchema(
            name="archives",
            display_name="Archives",
            forms=[
                FormSection(
                    title="Source",
                    fields=[
                        StringField(name="source_table_id", label="STID"),
                        StringField(name="dest_table_id", label="DTID"),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
            fail_when=[
                FailRule(
                    fail_when=all_(
                        F("source_table_id") == F("dest_table_id"),
                        all_truthy("source_table_id", "dest_table_id"),
                    ),
                    error_fields=["source_table_id", "dest_table_id"],
                    message="Source and Destination tables cannot be the same.",
                ),
            ],
        )

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            source_table_id: str = ""
            dest_table_id: str = ""

        # Different ids: passes.
        Body(source_table_id="1", dest_table_id="2")
        # Both empty: rule does not fire because all_truthy guard fails.
        Body(source_table_id="", dest_table_id="")
        # Same ids: rule fires.
        with pytest.raises(ValidationError, match="cannot be the same"):
            Body(source_table_id="1", dest_table_id="1")

    def test_archives_dest_file_co_located_cardinality_rules(self) -> None:
        """Worked example #5 — three co-located CardinalityRules."""
        schema = AppSchema(
            name="archives",
            display_name="Archives",
            forms=[
                FormSection(
                    title="Destination",
                    fields=[
                        StringField(name="swap_drop", label="SD"),
                        StringField(name="delete_data", label="DD"),
                        StringField(name="dest_file", label="DF"),
                        StringField(name="dest_table_id", label="DTID"),
                        StringField(name="dest_table_name", label="DTN"),
                    ],
                    cardinality_rules=[
                        CardinalityRule(
                            when=all_(
                                falsy("delete_data"),
                                F("swap_drop") != "swap_drop",
                            ),
                            fields=[
                                "dest_file",
                                "dest_table_id",
                                "dest_table_name",
                            ],
                            min=1,
                            message="At least one destination must be set.",
                        ),
                        CardinalityRule(
                            when=any_(
                                F("swap_drop") == "swap_drop",
                                truthy("delete_data"),
                            ),
                            fields=[
                                "dest_file",
                                "dest_table_id",
                                "dest_table_name",
                            ],
                            max=0,
                            message="Destination must be unset.",
                        ),
                        CardinalityRule(
                            when=None,
                            fields=["dest_table_id", "dest_table_name"],
                            max=1,
                            message="Specify dest_table_id or dest_table_name, not both.",
                        ),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
        )

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            swap_drop: str = ""
            delete_data: str = ""
            dest_file: str = ""
            dest_table_id: str = ""
            dest_table_name: str = ""

        # When swap_drop is "swap_drop", destination MUST be unset.
        Body(swap_drop="swap_drop")
        with pytest.raises(ValidationError, match="Destination must be unset"):
            Body(swap_drop="swap_drop", dest_file="x.sql")

        # When neither swap_drop nor delete_data, at least one destination required.
        Body(dest_file="x.sql")
        with pytest.raises(ValidationError, match="At least one"):
            Body()

        # at-most-one of dest_table_id / dest_table_name fires unconditionally.
        with pytest.raises(ValidationError, match="not both"):
            Body(dest_table_id="1", dest_table_name="t")


# ── Multiple-rules-same-scope behaviour ─────────────────────────────────


class TestMultipleRulesSameScope:
    """Verify multiple rules at the same scope all fire and are joined."""

    def test_two_rules_both_fire(self) -> None:
        """Two rules both fire."""
        schema = AppSchema(
            name="t",
            display_name="T",
            forms=[
                FormSection(
                    title="S",
                    fields=[
                        StringField(name="a", label="A"),
                        StringField(name="b", label="B"),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
            fail_when=[
                FailRule(
                    fail_when=truthy("a"),
                    error_fields=["a"],
                    message="rule one fired",
                ),
                FailRule(
                    fail_when=truthy("b"),
                    error_fields=["b"],
                    message="rule two fired",
                ),
            ],
        )

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            a: str = ""
            b: str = ""

        with pytest.raises(ValidationError) as exc_info:
            Body(a="set", b="set")

        joined = str(exc_info.value)
        assert "rule one fired" in joined
        assert "rule two fired" in joined


# ── Multi-entity AppSchema + rule plan extraction ─────────────────────


def _multi_entity_two_alpha_beta_schema() -> AppSchema:
    """Build a minimal two-entity schema; ``alpha`` carries a requires gate on ``x``."""
    return AppSchema(
        name="p",
        display_name="P",
        entities=[
            AppEntitySchema(
                name="alpha",
                display_name="Alpha",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(
                                name="x",
                                label="X",
                                requires=[FieldGate(when=truthy("y"))],
                            ),
                            StringField(name="y", label="Y"),
                        ],
                    ),
                ],
                list_view=ListView(columns=[Column(key="x", label="X")]),
            ),
            AppEntitySchema(
                name="beta",
                display_name="Beta",
                forms=[
                    FormSection(
                        title="T",
                        fields=[StringField(name="z", label="Z")],
                    ),
                ],
                list_view=ListView(columns=[Column(key="z", label="Z")]),
            ),
        ],
    )


class TestMultiEntityRulePlan:
    """``_extract_rule_plan`` / ``apply_conditional_rules`` for ``entities`` schemas."""

    def test_extract_requires_entity_name(self) -> None:
        """Multi-entity schemas refuse a plan without ``entity_name``."""
        schema = _multi_entity_two_alpha_beta_schema()
        with pytest.raises(ValueError, match="entity_name"):
            _extract_rule_plan(schema)

    def test_extract_unknown_entity_name(self) -> None:
        """Unknown ``entity_name`` raises with known entity list."""
        schema = _multi_entity_two_alpha_beta_schema()
        with pytest.raises(ValueError, match=re.escape("not an AppEntitySchema.name")):
            _extract_rule_plan(schema, entity_name="gamma")

    def test_extract_scopes_rules_to_named_entity(self) -> None:
        """Plans for ``alpha`` and ``beta`` differ; ``beta`` has no declarative rules."""
        schema = _multi_entity_two_alpha_beta_schema()
        plan_alpha = _extract_rule_plan(schema, entity_name="alpha")
        assert len(plan_alpha.rules) >= 1
        assert any(
            r.kind == "field_gate_requires" and "alpha" in r.scope_path
            for r in plan_alpha.rules
        )

        plan_beta = _extract_rule_plan(schema, entity_name="beta")
        assert plan_beta.rules == ()

    def test_single_entity_schema_rejects_entity_name(self) -> None:
        """Task-style schema rejects a stray ``entity_name``."""
        schema = _build_schema(StringField(name="x", label="X"))
        with pytest.raises(ValueError, match="no `entities`"):
            _extract_rule_plan(schema, entity_name="alpha")

    def test_apply_conditional_rules_with_entity_name(self) -> None:
        """Decorator accepts ``entity_name`` for multi-entity schemas."""
        schema = _multi_entity_two_alpha_beta_schema()

        @apply_conditional_rules(schema, entity_name="alpha")
        class Body(ConditionalRulesModel):
            x: str = ""
            y: str = ""

        Body(x="", y="")
        Body(x="ok", y="y")

        with pytest.raises(ValidationError, match="x"):
            Body(x="", y="y")


class TestValueIsPresent:
    """Direct coverage of the shared presence predicate.

    ``value_is_present`` is the single source of truth used by both runtime
    gate evaluation and authoring-time guards, so its boundaries are pinned
    here — a regression silently changes whether forbidden gates fire.
    """

    @pytest.mark.parametrize(
        "value",
        [None, False, "", b"", [], (), set(), frozenset(), {}],
        ids=[
            "none",
            "false",
            "empty_str",
            "empty_bytes",
            "empty_list",
            "empty_tuple",
            "empty_set",
            "empty_frozenset",
            "empty_dict",
        ],
    )
    def test_absent_values(self, value: object) -> None:
        """``None``, ``False`` and empty containers count as absent."""
        assert value_is_present(value) is False

    @pytest.mark.parametrize(
        "value",
        [0, 0.0, "0", " ", "x", b"x", [0], (0,), {0}, {"k": "v"}, True],
        ids=[
            "zero_int",
            "zero_float",
            "str_zero",
            "whitespace",
            "str",
            "bytes",
            "list",
            "tuple",
            "set",
            "dict",
            "true",
        ],
    )
    def test_present_values(self, value: object) -> None:
        """Non-empty values — including falsy ``0`` and whitespace — are present."""
        assert value_is_present(value) is True

    def test_zero_present_but_false_absent(self) -> None:
        """Pin the ``0`` (present) vs ``False`` (absent) asymmetry explicitly."""
        false_value = False
        assert value_is_present(0) is True
        assert value_is_present(false_value) is False


class TestExtractForbiddenFieldGatePlan:
    """``extract_forbidden_field_gate_plan`` isolates only forbidden gates."""

    def test_returns_only_forbidden_gates(self) -> None:
        """Requires gates, cardinality and fail rules are filtered out."""
        schema = _build_schema(
            StringField(
                name="x",
                label="X",
                requires=[FieldGate(when=truthy("y"))],
                forbidden=[FieldGate(when=truthy("z"))],
            ),
            extra_fields=[
                StringField(name="y", label="Y"),
                StringField(name="z", label="Z"),
            ],
            section_cardinality=[
                CardinalityRule(when=truthy("y"), fields=["x"], min=1)
            ],
            schema_fail=[FailRule(fail_when=truthy("z"), error_fields=["z"])],
        )

        plan = extract_forbidden_field_gate_plan(schema)

        assert len(plan.rules) == 1
        assert all(r.kind == "field_gate_forbidden" for r in plan.rules)

    def test_gateless_schema_yields_empty_plan(self) -> None:
        """A schema with no forbidden gates produces an empty plan."""
        schema = _build_schema(StringField(name="x", label="X"))

        assert extract_forbidden_field_gate_plan(schema).rules == ()

    def test_scopes_forbidden_gates_to_named_entity(self) -> None:
        """The ``entity_name`` argument is honoured for multi-entity schemas."""
        schema = _multi_entity_two_alpha_beta_schema()

        # alpha declares only a requires gate, so its forbidden plan is empty.
        assert (
            extract_forbidden_field_gate_plan(schema, entity_name="alpha").rules == ()
        )
        assert extract_forbidden_field_gate_plan(schema, entity_name="beta").rules == ()


class TestExtractRequiredFieldGatePlan:
    """``extract_required_field_gate_plan`` isolates only requires gates."""

    def test_returns_only_requires_gates(self) -> None:
        """Forbidden gates, cardinality and fail rules are filtered out."""
        schema = _build_schema(
            StringField(
                name="x",
                label="X",
                requires=[FieldGate(when=truthy("y"))],
                forbidden=[FieldGate(when=truthy("z"))],
            ),
            extra_fields=[
                StringField(name="y", label="Y"),
                StringField(name="z", label="Z"),
            ],
            section_cardinality=[
                CardinalityRule(when=truthy("y"), fields=["x"], min=1)
            ],
            schema_fail=[FailRule(fail_when=truthy("z"), error_fields=["z"])],
        )

        plan = extract_required_field_gate_plan(schema)

        assert len(plan.rules) == 1
        assert all(r.kind == "field_gate_requires" for r in plan.rules)

    def test_gateless_schema_yields_empty_plan(self) -> None:
        """A schema with no requires gates produces an empty plan."""
        schema = _build_schema(StringField(name="x", label="X"))

        assert extract_required_field_gate_plan(schema).rules == ()

    def test_scopes_requires_gates_to_named_entity(self) -> None:
        """The ``entity_name`` argument is honoured for multi-entity schemas."""
        schema = _multi_entity_two_alpha_beta_schema()

        # alpha declares a requires gate; beta declares none.
        assert (
            len(extract_required_field_gate_plan(schema, entity_name="alpha").rules)
            == 1
        )
        assert extract_required_field_gate_plan(schema, entity_name="beta").rules == ()


class TestResolveField:
    """Tests for dotted-path field resolution in the rules evaluator."""

    def test_resolve_top_level_attribute(self) -> None:
        """Resolve a plain top-level field name."""
        assert _resolve_field(_Instance(a="x"), "a") == "x"

    def test_resolve_dotted_nested_attribute(self) -> None:
        """Resolve a nested attribute via a dotted path."""

        class Source(BaseModel):
            mode: str = "schema"
            source_db_id: str = ""

        class Root(BaseModel):
            source: Source = Source()

        root = Root(source=Source(mode="query", source_db_id="42"))
        assert _resolve_field(root, "source.mode") == "query"
        assert _resolve_field(root, "source.source_db_id") == "42"

    def test_resolve_missing_intermediate_returns_none(self) -> None:
        """Return ``None`` when an intermediate segment is absent."""

        class Root(BaseModel):
            source: None = None

        assert _resolve_field(Root(), "source.mode") is None

    def test_resolve_indexed_segment_returns_none(self) -> None:
        """Return ``None`` for a bracket-indexed segment; traversal is getattr-only."""

        class Root(BaseModel):
            items: list[str] = Field(default_factory=list)

        assert _resolve_field(Root(items=["a"]), "items[0]") is None


class TestOneOfGroupRules:
    """Branch-selection enforcement synthesised from :class:`OneOfGroup`."""

    @staticmethod
    def _source_one_of_schema() -> AppSchema:
        return AppSchema(
            name="p",
            display_name="P",
            task_type="t",
            forms=[
                FormSection(
                    title="Source",
                    fields=[
                        OneOfGroup(
                            name="source",
                            label="Source",
                            discriminator="source.mode",
                            default="schema",
                            branches=[
                                OneOfBranch(
                                    value="schema",
                                    label="Schema",
                                    fields=[
                                        StringField(
                                            name="source.source_db_id",
                                            label="Schema",
                                        ),
                                    ],
                                ),
                                OneOfBranch(
                                    value="query",
                                    label="Query",
                                    fields=[
                                        StringField(
                                            name="source.source_query",
                                            label="Query",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
            detail_view=DetailView(sections=[]),
        )

    @staticmethod
    def _shared_leaf_one_of_schema() -> AppSchema:
        return AppSchema(
            name="p",
            display_name="P",
            task_type="t",
            forms=[
                FormSection(
                    title="Target",
                    fields=[
                        OneOfGroup(
                            name="target",
                            label="Target",
                            discriminator="target_mode",
                            default="service",
                            branches=[
                                OneOfBranch(
                                    value="service",
                                    label="Service",
                                    fields=[StringField(name="target", label="Target")],
                                ),
                                OneOfBranch(
                                    value="schema",
                                    label="Schema",
                                    fields=[StringField(name="target", label="Target")],
                                ),
                            ],
                        ),
                    ],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
            detail_view=DetailView(sections=[]),
        )

    def test_rule_plan_includes_synthesised_branch_forbidden_gates(self) -> None:
        """Emit one ``field_gate_forbidden`` rule per branch leaf."""
        plan = _extract_rule_plan(self._source_one_of_schema())
        forbidden = [
            rule
            for rule in plan.rules
            if isinstance(rule.predicate, NotEquals)
            and rule.predicate.field == "source.mode"
        ]
        targets = {rule.fields[0] for rule in forbidden}
        assert targets == {"source.source_db_id", "source.source_query"}

    def test_active_branch_leaf_allowed_inactive_branch_leaf_forbidden(self) -> None:
        """Forbid leaves that belong to an unselected one-of branch."""
        plan = _extract_rule_plan(self._source_one_of_schema())

        class _Source:
            def __init__(self, mode: str, **attrs: str) -> None:
                self.mode = mode
                for key, value in attrs.items():
                    setattr(self, key, value)

        schema_body = type("Body", (), {"source": _Source("schema", source_db_id="x")})
        assert evaluate_conditional_rules(schema_body(), plan) == []

        stale_query = type(
            "Body",
            (),
            {"source": _Source("schema", source_db_id="x", source_query="SELECT 1")},
        )
        failures = evaluate_conditional_rules(stale_query(), plan)
        assert any("source.source_query" in message for message in failures)

        query_body = type(
            "Body", (), {"source": _Source("query", source_query="SELECT 1")}
        )
        assert evaluate_conditional_rules(query_body(), plan) == []

        stale_schema = type(
            "Body",
            (),
            {"source": _Source("query", source_query="SELECT 1", source_db_id="x")},
        )
        failures = evaluate_conditional_rules(stale_schema(), plan)
        assert any("source.source_db_id" in message for message in failures)

    def test_apply_conditional_rules_accepts_nested_union_write_model(self) -> None:
        """Decorate a write model whose nested union backs a one-of schema."""
        schema = self._source_one_of_schema()

        class SourceBySchema(BaseModel):
            mode: Literal["schema"] = "schema"
            source_db_id: str = ""

        class SourceByQuery(BaseModel):
            mode: Literal["query"] = "query"
            source_query: str = ""

        @apply_conditional_rules(schema)
        class Body(ConditionalRulesModel):
            source: Annotated[
                SourceBySchema | SourceByQuery, Field(discriminator="mode")
            ]

        Body(source=SourceBySchema(source_db_id="inventory"))
        Body(source=SourceByQuery(source_query="SELECT 1"))

    def test_shared_leaf_across_branches_does_not_emit_contradictory_gates(
        self,
    ) -> None:
        """Avoid per-branch forbidden gates for a leaf shared by all branches."""
        plan = _extract_rule_plan(self._shared_leaf_one_of_schema())
        shared_leaf_gates = [
            rule
            for rule in plan.rules
            if isinstance(rule.predicate, NotEquals)
            and rule.predicate.field == "target_mode"
            and rule.fields == ("target",)
        ]
        assert shared_leaf_gates == []

    def test_shared_leaf_allows_values_for_each_mode(self) -> None:
        """Allow a shared one-of leaf when either mode is selected."""
        plan = _extract_rule_plan(self._shared_leaf_one_of_schema())
        service_body = type("Body", (), {"target_mode": "service", "target": "42"})
        assert evaluate_conditional_rules(service_body(), plan) == []
        schema_body = type("Body", (), {"target_mode": "schema", "target": "42"})
        assert evaluate_conditional_rules(schema_body(), plan) == []
