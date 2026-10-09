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

"""Tests for SnippetMetaParameter model validators and computed properties."""

from datetime import datetime, timedelta, timezone, UTC
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.utils.fields import UTCDatetime
from app.core.utils.pydantic import CustomFieldMetadata
from app.extensions.apps.field_names import (
    RESERVED_EXECUTION_FIELD_NAMES,
)
from app.extensions.snippets.models.meta import (
    serialize_cli_value,
    SnippetMetaParameter,
    SnippetMetaParametersValidationResult,
    SnippetMetaParameterType,
    SnippetVisibilityCondition,
)

FLOAT_DEFAULT_STEP = 0.1
INT_DEFAULT_STEP = 1.0
CUSTOM_STEP = 5.0
EXPECTED_INT_DEFAULT = 5
EXPECTED_INT_LT = 100
MIN_LENGTH_TWO = 2
MAX_LENGTH_FIFTY = 50
EXPECTED_PARAM_COUNT = 2
EXPECTED_CONSTRAINT_COUNT = 2
MD5_DIGEST_LENGTH = 32


class TestSetDefaultStep:
    """Test the set_default_step model validator."""

    def test_int_type_gets_step_one(self):
        """Verify INT type sets step to 1.0 when not explicitly provided."""
        param = SnippetMetaParameter(name="count", type=SnippetMetaParameterType.INT)
        assert param.step == 1.0

    def test_float_type_gets_step_point_one(self):
        """Verify FLOAT type sets step to 0.1 when not explicitly provided."""
        param = SnippetMetaParameter(name="ratio", type=SnippetMetaParameterType.FLOAT)
        assert param.step == FLOAT_DEFAULT_STEP

    def test_explicit_step_is_preserved(self):
        """Verify explicitly provided step value is not overridden."""
        param = SnippetMetaParameter(
            name="count", type=SnippetMetaParameterType.INT, step=CUSTOM_STEP
        )
        assert param.step == CUSTOM_STEP

    def test_str_type_has_no_step(self):
        """Verify STR type does not get a default step value."""
        param = SnippetMetaParameter(name="label", type=SnippetMetaParameterType.STR)
        assert param.step is None

    def test_bool_type_has_no_step(self):
        """Verify BOOL type does not get a default step value."""
        param = SnippetMetaParameter(name="flag", type=SnippetMetaParameterType.BOOL)
        assert param.step is None

    def test_datetime_type_has_no_step(self):
        """Verify DATETIME type does not get a default step value."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME
        )
        assert param.step is None


class TestReservedParameterName:
    """Test rejection of parameter names reserved for synthesized fields."""

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    def test_reserved_name_raises(self, reserved_name: str) -> None:
        """Raise when a parameter is named after a synthesized execution field."""
        with pytest.raises(ValidationError) as exc_info:
            SnippetMetaParameter(name=reserved_name, type=SnippetMetaParameterType.STR)
        message = str(exc_info.value)
        assert "reserved" in message
        assert reserved_name in message

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    def test_reserved_name_is_rejected_when_hidden(self, reserved_name: str) -> None:
        """Raise for a reserved name even when the parameter renders in no form.

        ``hidden`` suppresses form rendering only. The parameter still reaches
        the generated execution model, where its validation alias is its
        declared name, so a hidden reserved name would collide there instead.
        """
        with pytest.raises(ValidationError, match="reserved"):
            SnippetMetaParameter(
                name=reserved_name, type=SnippetMetaParameterType.STR, hidden=True
            )

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    def test_reserved_name_is_rejected_when_positional(
        self, reserved_name: str
    ) -> None:
        """Raise for a reserved name even when the parameter is positional."""
        with pytest.raises(ValidationError, match="reserved"):
            SnippetMetaParameter(
                name=reserved_name, type=SnippetMetaParameterType.STR, positional=True
            )

    @pytest.mark.parametrize(
        "name",
        [
            "extra_args_suffix",
            "sudo_mode",
            "executor_hosts",
            "script_preview_url",
            "preview",
            "host",
        ],
    )
    def test_near_miss_names_are_unaffected(self, name: str) -> None:
        """Accept a name that merely resembles a reserved one."""
        param = SnippetMetaParameter(name=name, type=SnippetMetaParameterType.STR)
        assert param.name == name

    @pytest.mark.parametrize("name", ["Sudo", "SUDO", "Extra_Args"])
    def test_reservation_is_case_sensitive(self, name: str) -> None:
        """Accept a re-cased reserved name, which cannot collide.

        Duplicate form field names are detected by exact comparison, and every
        downstream binding (HTML input names, generated model field keys,
        validation aliases) is case-sensitive too, so ``Sudo`` is provably
        distinct from the synthesized ``sudo`` field.
        """
        param = SnippetMetaParameter(name=name, type=SnippetMetaParameterType.STR)
        assert param.name == name


class TestDatetimeTypeResolution:
    """Test DATETIME snippet parameter type resolution."""

    def test_yaml_datetime_string_resolves_to_datetime_enum(self):
        """Verify type: datetime in YAML resolves via name-based enum lookup."""
        param = SnippetMetaParameter(name="start", type="datetime")
        assert param.py_type == SnippetMetaParameterType.DATETIME

    def test_datetime_enum_member_value_is_utc_datetime_type(self):
        """Verify DATETIME enum member maps to UTCDatetime for validation."""
        assert SnippetMetaParameterType.DATETIME.value is UTCDatetime


class TestSerializeCliValue:
    """Test per-type CLI value serialization."""

    def test_datetime_serializes_with_t_separator_no_microseconds(self):
        """Verify datetime values render as YYYY-MM-DDTHH:MM:SS without microseconds."""
        value = datetime(2024, 6, 10, 14, 30, 45, 123456, tzinfo=UTC)
        assert serialize_cli_value(value) == "2024-06-10T14:30:45"

    def test_tz_aware_datetime_serializes_as_utc_wall_clock(self):
        """Verify non-UTC offsets convert to UTC before CLI formatting."""
        value = datetime(2024, 6, 10, 14, 30, 0, tzinfo=timezone(timedelta(hours=5)))
        assert serialize_cli_value(value) == "2024-06-10T09:30:00"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("hello", "hello"),
            (42, "42"),
            (True, "True"),
        ],
    )
    def test_non_datetime_values_use_str(self, value, expected):
        """Verify non-datetime values still use str() unchanged."""
        assert serialize_cli_value(value) == expected


class TestDatetimeValidation:
    """Test DATETIME parameter validation and error reporting."""

    def test_optional_empty_string_coerces_to_none(self):
        """Verify optional datetime params accept empty form input as None."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        adapter = TypeAdapter(param.validation_type)
        assert adapter.validate_python("") is None

    def test_valid_iso_datetime_string_accepted(self):
        """Verify ISO-8601 datetime strings validate for DATETIME params."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        adapter = TypeAdapter(param.validation_type)
        result = adapter.validate_python("2024-06-10T14:30:00")
        assert isinstance(result, datetime)
        assert result.tzinfo == UTC

    def test_tz_aware_iso_input_validates_and_serializes_as_utc(self):
        """Verify offset ISO input normalizes to UTC at validation and serialization."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        adapter = TypeAdapter(param.validation_type)
        result = adapter.validate_python("2024-06-10T14:30:00+05:00")
        assert result.tzinfo == UTC
        assert serialize_cli_value(result) == "2024-06-10T09:30:00"

    def test_datetime_without_seconds_accepted(self):
        """Verify datetime-local input without seconds parses successfully."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        adapter = TypeAdapter(param.validation_type)
        result = adapter.validate_python("2024-06-10T14:30")
        assert (result.year, result.month, result.day, result.hour, result.minute) == (
            2024,
            6,
            10,
            14,
            30,
        )

    def test_malformed_datetime_produces_parameter_error_message(self):
        """Verify malformed datetime input yields a readable parameter error."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        adapter = TypeAdapter(param.validation_type)
        with pytest.raises(ValidationError) as exc_info:
            adapter.validate_python("not-a-datetime")
        errors = SnippetMetaParameter.convert_validation_errors(
            exc_info.value, {"name": "start"}
        )
        assert any("Parameter error" in e for e in errors)
        assert any("start" in e for e in errors)


class TestValidateArgFormat:
    """Test the validate_arg_format model validator."""

    def test_missing_value_placeholder_raises(self):
        """Verify arg_format without ${value} raises ValueError for non-flag params."""
        with pytest.raises(ValidationError, match="arg_format must include"):
            SnippetMetaParameter(
                name="host",
                type=SnippetMetaParameterType.STR,
                arg_format="--host",
            )

    def test_flag_type_allows_arg_format_without_value(self):
        """Verify BOOL type allows arg_format without ${value} placeholder."""
        param = SnippetMetaParameter(
            name="verbose",
            type=SnippetMetaParameterType.BOOL,
            arg_format="--verbose",
        )
        assert param.arg_format == "--verbose"

    def test_valid_arg_format_with_value_placeholder(self):
        """Verify arg_format with ${value} is accepted for non-flag params."""
        param = SnippetMetaParameter(
            name="host",
            type=SnippetMetaParameterType.STR,
            arg_format="--host=${value}",
        )
        assert param.arg_format == "--host=${value}"

    def test_none_arg_format_is_accepted(self):
        """Verify None arg_format passes validation."""
        param = SnippetMetaParameter(name="host", type=SnippetMetaParameterType.STR)
        assert param.arg_format is None


class TestNormalizeChoices:
    """Test the normalize_choices field validator."""

    def test_string_choices_converted_to_dicts(self):
        """Verify string choices are normalized to dicts with 'value' key."""
        param = SnippetMetaParameter(name="color", choices=["red", "green", "blue"])
        assert param.choices == [
            {"value": "red"},
            {"value": "green"},
            {"value": "blue"},
        ]

    def test_dict_choices_preserved(self):
        """Verify dict choices are passed through unchanged."""
        choices = [{"value": "a", "label": "Option A"}, {"value": "b"}]
        param = SnippetMetaParameter(name="opt", choices=choices)
        assert param.choices == choices

    def test_mixed_choices_normalized(self):
        """Verify mixed string and dict choices are handled correctly."""
        param = SnippetMetaParameter(
            name="opt", choices=["x", {"value": "y", "label": "Y Label"}]
        )
        assert param.choices == [
            {"value": "x"},
            {"value": "y", "label": "Y Label"},
        ]


class TestSetDefaultTypeIfUnknown:
    """Test the set_default_type_if_unknown field validator."""

    def test_unknown_type_defaults_to_str(self):
        """Verify unknown type value falls back to STR."""
        param = SnippetMetaParameter(name="field", type="unknown_type")
        assert param.py_type == SnippetMetaParameterType.STR

    def test_valid_type_preserved(self):
        """Verify valid type values are preserved."""
        param = SnippetMetaParameter(name="field", type=SnippetMetaParameterType.INT)
        assert param.py_type == SnippetMetaParameterType.INT

    def test_datetime_type_preserved(self):
        """Verify DATETIME type value is preserved."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME
        )
        assert param.py_type == SnippetMetaParameterType.DATETIME


class TestCoerceToType:
    """Test the coerce_to_type field validator."""

    def test_string_default_coerced_to_int(self):
        """Verify string default '5' is coerced to int 5 for INT type."""
        param = SnippetMetaParameter(
            name="count", type=SnippetMetaParameterType.INT, default="5"
        )
        assert param.default == EXPECTED_INT_DEFAULT
        assert isinstance(param.default, int)

    def test_string_default_coerced_to_float(self):
        """Verify string default '3.14' is coerced to float for FLOAT type."""
        param = SnippetMetaParameter(
            name="ratio", type=SnippetMetaParameterType.FLOAT, default="3.14"
        )
        assert param.default == pytest.approx(3.14)

    def test_none_default_preserved(self):
        """Verify None default is not coerced."""
        param = SnippetMetaParameter(
            name="count", type=SnippetMetaParameterType.INT, default=None
        )
        assert param.default is None

    def test_gt_lt_coerced(self):
        """Verify gt and lt values are coerced to the declared type."""
        param = SnippetMetaParameter(
            name="val",
            type=SnippetMetaParameterType.INT,
            gt="0",
            lt="100",
        )
        assert param.gt == 0
        assert isinstance(param.gt, int)
        assert param.lt == EXPECTED_INT_LT
        assert isinstance(param.lt, int)

    def test_string_default_coerced_to_datetime(self):
        """Verify string default is coerced to datetime for DATETIME type."""
        param = SnippetMetaParameter(
            name="start",
            type=SnippetMetaParameterType.DATETIME,
            default="2024-06-10T14:30:00",
        )
        assert isinstance(param.default, datetime)
        assert param.default.tzinfo == UTC


class TestIsFlag:
    """Test the is_flag computed field."""

    def test_bool_type_is_flag(self):
        """Verify BOOL type returns is_flag=True."""
        param = SnippetMetaParameter(name="flag", type=SnippetMetaParameterType.BOOL)
        assert param.is_flag is True

    def test_str_type_is_not_flag(self):
        """Verify STR type returns is_flag=False."""
        param = SnippetMetaParameter(name="name", type=SnippetMetaParameterType.STR)
        assert param.is_flag is False

    def test_int_type_is_not_flag(self):
        """Verify INT type returns is_flag=False."""
        param = SnippetMetaParameter(name="count", type=SnippetMetaParameterType.INT)
        assert param.is_flag is False


class TestConstraints:
    """Test the constraints cached property."""

    def test_str_type_with_lengths(self):
        """Verify STR type returns StringConstraints with min/max_length."""
        param = SnippetMetaParameter(
            name="label",
            type=SnippetMetaParameterType.STR,
            min_length=2,
            max_length=50,
        )
        constraints = param.constraints
        assert len(constraints) == 1
        assert constraints[0].min_length == MIN_LENGTH_TWO
        assert constraints[0].max_length == MAX_LENGTH_FIFTY

    def test_int_type_with_gt_lt(self):
        """Verify INT type with gt/lt returns Interval constraint."""
        param = SnippetMetaParameter(
            name="count",
            type=SnippetMetaParameterType.INT,
            gt=0,
            lt=100,
        )
        constraints = param.constraints
        assert len(constraints) == 1
        assert constraints[0].gt == 0
        assert constraints[0].lt == EXPECTED_INT_LT

    def test_str_with_interval_returns_both(self):
        """Verify STR type with ge returns both StringConstraints and Interval."""
        param = SnippetMetaParameter(
            name="code",
            type=SnippetMetaParameterType.STR,
            min_length=1,
            ge="0",
        )
        assert len(param.constraints) == EXPECTED_CONSTRAINT_COUNT

    def test_no_interval_when_no_bounds(self):
        """Verify no Interval constraint when no gt/lt/ge/le specified."""
        param = SnippetMetaParameter(name="name", type=SnippetMetaParameterType.STR)
        assert len(param.constraints) == 1


class TestValidationType:
    """Test the validation_type cached property."""

    def test_with_choices_returns_str_enum(self):
        """Verify required parameter with choices returns a StrEnum type."""
        param = SnippetMetaParameter(name="opt", choices=["a", "b", "c"], required=True)
        vtype = param.validation_type
        assert hasattr(vtype, "__members__")
        assert set(vtype.__members__) == {"a", "b", "c"}

    def test_required_str_returns_raw_type(self):
        """Verify required STR param returns annotated str type."""
        param = SnippetMetaParameter(
            name="host", type=SnippetMetaParameterType.STR, required=True
        )
        assert param.validation_type is not None

    def test_optional_returns_union_with_empty_str_to_none(self):
        """Verify optional param with no default returns union with EmptyStrToNone."""
        param = SnippetMetaParameter(
            name="host", type=SnippetMetaParameterType.STR, required=False
        )
        vtype = param.validation_type
        assert hasattr(vtype, "__args__")

    def test_optional_datetime_returns_union_with_empty_str_to_none(self):
        """Verify optional DATETIME param returns UTCDatetime | EmptyStrToNone."""
        param = SnippetMetaParameter(
            name="start", type=SnippetMetaParameterType.DATETIME, required=False
        )
        vtype = param.validation_type
        assert UTCDatetime in get_args(vtype)


class TestToValidationField:
    """Test the to_validation_field method."""

    def test_returns_field_info(self):
        """Verify to_validation_field returns a FieldInfo instance."""
        param = SnippetMetaParameter(
            name="host",
            type=SnippetMetaParameterType.STR,
            description="Target host",
            default="localhost",
        )
        field = param.to_validation_field()
        assert field.default == "localhost"
        assert field.description == "Target host"
        assert field.alias == "host"

    def test_optional_without_default_sets_none(self):
        """Verify optional param without default gets default=None."""
        param = SnippetMetaParameter(
            name="host", type=SnippetMetaParameterType.STR, required=False
        )
        field = param.to_validation_field()
        assert field.default is None


class TestGroupField:
    """Test the group field on SnippetMetaParameter."""

    def test_accepts_valid_string(self):
        """Verify group field accepts a valid non-empty string."""
        param = SnippetMetaParameter(name="host", group="Network")
        assert param.group == "Network"

    def test_defaults_to_none(self):
        """Verify group field defaults to None when not provided."""
        param = SnippetMetaParameter(name="host")
        assert param.group is None

    def test_rejects_empty_string(self):
        """Verify group field rejects an empty string."""
        with pytest.raises(ValidationError):
            SnippetMetaParameter(name="host", group="")

    def test_whitespace_only_accepted_as_nonempty(self):
        """Verify group field accepts whitespace-only string (length > 0)."""
        param = SnippetMetaParameter(name="host", group="   ")
        assert param.group == "   "


class TestConvertValidationErrors:
    """Test the convert_validation_errors static method."""

    def test_converts_to_error_strings(self):
        """Verify ValidationError is converted to a list of readable error strings."""
        with pytest.raises(ValidationError) as exc_info:
            SnippetMetaParameter(name="")
        errors = SnippetMetaParameter.convert_validation_errors(
            exc_info.value, {"name": ""}
        )
        assert len(errors) > 0
        assert all(isinstance(e, str) for e in errors)
        assert any("Parameter error" in e for e in errors)

    def test_dict_input_shows_name(self):
        """Verify dict input with 'name' key shows parameter name in error."""
        with pytest.raises(ValidationError) as exc_info:
            SnippetMetaParameter(name="")
        errors = SnippetMetaParameter.convert_validation_errors(
            exc_info.value, {"name": "my_param"}
        )
        assert any("my_param" in e for e in errors)

    def test_non_dict_input_shows_repr(self):
        """Verify non-dict input shows repr in error message."""
        with pytest.raises(ValidationError) as exc_info:
            SnippetMetaParameter(name="")
        errors = SnippetMetaParameter.convert_validation_errors(
            exc_info.value, "bad_input"
        )
        assert any("bad_input" in e for e in errors)


class TestVisibilityCondition:
    """Test the visible_when / visible_when_not conditional-visibility DSL.

    The React renderer hides the field and drops its value from the payload; the
    gates are also enforced server-side on the execute paths, which reject a
    directly-submitted hidden value (see ``evaluate_snippet_gates``).
    """

    def test_string_shorthand_normalized_to_condition(self):
        """A bare string is shorthand for a truthiness condition on that param."""
        param = SnippetMetaParameter(name="start", visible_when_not="list")
        assert isinstance(param.visible_when_not, SnippetVisibilityCondition)
        assert param.visible_when_not.parameter == "list"
        assert param.visible_when_not.equals is None

    def test_visible_when_string_shorthand(self):
        """visible_when accepts the same bare-string shorthand."""
        param = SnippetMetaParameter(name="start", visible_when="list")
        assert param.visible_when.parameter == "list"
        assert param.visible_when.equals is None

    def test_mapping_with_equals(self):
        """A mapping with equals yields an equality match condition."""
        param = SnippetMetaParameter(
            name="region",
            visible_when_not={"parameter": "mode", "equals": "advanced"},
        )
        assert param.visible_when_not.parameter == "mode"
        assert param.visible_when_not.equals == "advanced"

    def test_no_condition_leaves_fields_none(self):
        """A parameter without conditions keeps both visibility fields as None."""
        param = SnippetMetaParameter(name="start")
        assert param.visible_when is None
        assert param.visible_when_not is None

    def test_both_conditions_set_raises(self):
        """Declaring both visible_when and visible_when_not is rejected."""
        with pytest.raises(ValidationError, match="visible_when"):
            SnippetMetaParameter(name="start", visible_when="a", visible_when_not="b")

    def test_self_reference_raises(self):
        """A condition that references the parameter itself is rejected."""
        with pytest.raises(ValidationError, match=r"itself|self"):
            SnippetMetaParameter(name="start", visible_when_not="start")

    def test_required_with_condition_raises(self):
        """Combining required=True with a visibility condition is rejected."""
        with pytest.raises(ValidationError, match="required"):
            SnippetMetaParameter(name="start", required=True, visible_when_not="list")

    def test_nonempty_default_with_condition_raises(self):
        """A non-empty default + visibility condition is rejected.

        A hidden field is dropped client-side; server-side validation would then
        backfill the default, which the forbidden gate sees as present and
        rejects — an unsatisfiable trap. Reject the combination at meta time.
        """
        with pytest.raises(ValidationError, match="default"):
            SnippetMetaParameter(name="start", default="now", visible_when_not="list")

    def test_empty_default_with_condition_allowed(self):
        """A falsy/empty default (treated as absent) is fine with a condition."""
        param = SnippetMetaParameter(
            name="flag", type="bool", default=False, visible_when_not="list"
        )
        assert param.visible_when_not.parameter == "list"

    def test_zero_default_with_condition_raises(self):
        """A ``0`` default + visibility condition is rejected.

        ``0`` is falsy but ``value_is_present`` classifies it as *present*
        (numeric), so it would hit the same unsatisfiable backfill trap as any
        other non-empty default and must be rejected — unlike ``False``/``""``.
        """
        with pytest.raises(ValidationError, match="default"):
            SnippetMetaParameter(
                name="count", type="int", default=0, visible_when_not="list"
            )

    def test_empty_string_default_with_condition_allowed(self):
        """An empty-string default (treated as absent) is fine with a condition."""
        param = SnippetMetaParameter(name="note", default="", visible_when_not="list")
        assert param.visible_when_not.parameter == "list"

    def test_blank_parameter_name_raises(self):
        """An empty referenced parameter name is rejected."""
        with pytest.raises(ValidationError):
            SnippetMetaParameter(name="start", visible_when_not={"parameter": ""})

    def test_hyphenated_gated_name_raises(self):
        """A gated parameter whose own name has a hyphen is rejected.

        The framework conditional-rules engine folds the field's own name into
        the gate's reference set and rejects hyphenated names, so reject early.
        """
        with pytest.raises(ValidationError, match="valid Python identifier"):
            SnippetMetaParameter(name="ha-name", visible_when_not="list")

    def test_hyphenated_referenced_parameter_raises(self):
        """A condition referencing a hyphenated sibling name is rejected."""
        with pytest.raises(ValidationError, match="valid Python identifier"):
            SnippetMetaParameter(name="start", visible_when_not="list-mode")


class TestGateCondition:
    """Test the bounded requires/forbidden field-gate grammar.

    Four fields (``requires_when`` / ``requires_when_not`` / ``forbidden_when`` /
    ``forbidden_when_not``) reuse the visibility-condition shape and lower onto
    framework ``requires`` / ``forbidden`` :class:`FieldGate` lists. Negation is
    expressed by field choice (the ``_when_not`` variants), mirroring visibility.
    """

    def test_string_shorthand_normalized_on_all_fields(self):
        """A bare string is truthiness shorthand on every gate field."""
        for attr in (
            "requires_when",
            "requires_when_not",
            "forbidden_when",
            "forbidden_when_not",
        ):
            param = SnippetMetaParameter(name="field", **{attr: "trigger"})
            condition = getattr(param, attr)
            assert isinstance(condition, SnippetVisibilityCondition)
            assert condition.parameter == "trigger"
            assert condition.equals is None

    def test_mapping_with_equals(self):
        """A mapping with equals yields an equality-match gate condition."""
        param = SnippetMetaParameter(
            name="reason",
            requires_when={"parameter": "mode", "equals": "write"},
        )
        assert param.requires_when.parameter == "mode"
        assert param.requires_when.equals == "write"

    def test_no_gate_leaves_fields_none(self):
        """A parameter without gates keeps all four gate fields as None."""
        param = SnippetMetaParameter(name="field")
        assert param.requires_when is None
        assert param.requires_when_not is None
        assert param.forbidden_when is None
        assert param.forbidden_when_not is None

    def test_both_requires_variants_set_raises(self):
        """Declaring both requires_when and requires_when_not is rejected."""
        with pytest.raises(ValidationError, match="requires_when"):
            SnippetMetaParameter(
                name="reason", requires_when="a", requires_when_not="b"
            )

    def test_both_forbidden_variants_set_raises(self):
        """Declaring both forbidden_when and forbidden_when_not is rejected."""
        with pytest.raises(ValidationError, match="forbidden_when"):
            SnippetMetaParameter(
                name="reason", forbidden_when="a", forbidden_when_not="b"
            )

    def test_requires_and_forbidden_together_allowed(self):
        """A requires gate and a forbidden gate on one parameter is legitimate."""
        param = SnippetMetaParameter(
            name="reason",
            requires_when="write_mode",
            forbidden_when="readonly_mode",
        )
        assert param.requires_when.parameter == "write_mode"
        assert param.forbidden_when.parameter == "readonly_mode"

    def test_self_reference_raises(self):
        """A gate that references the parameter itself is rejected."""
        with pytest.raises(ValidationError, match=r"itself|self"):
            SnippetMetaParameter(name="reason", requires_when="reason")

    def test_required_with_requires_gate_raises(self):
        """Combining required=True with a requires gate is rejected."""
        with pytest.raises(ValidationError, match="required"):
            SnippetMetaParameter(name="reason", required=True, requires_when="mode")

    def test_required_with_forbidden_gate_raises(self):
        """Combining required=True with a forbidden gate is rejected."""
        with pytest.raises(ValidationError, match="required"):
            SnippetMetaParameter(name="reason", required=True, forbidden_when="mode")

    def test_nonempty_default_with_forbidden_gate_raises(self):
        """A non-empty default + forbidden gate is an unsatisfiable trap."""
        with pytest.raises(ValidationError, match="default"):
            SnippetMetaParameter(name="reason", default="x", forbidden_when="mode")

    def test_nonempty_default_with_requires_gate_raises(self):
        """A non-empty default + requires gate is a dead rule; rejected."""
        with pytest.raises(ValidationError, match="default"):
            SnippetMetaParameter(name="reason", default="x", requires_when="mode")

    def test_empty_default_with_gate_allowed(self):
        """A falsy/empty default (treated as absent) is fine with a gate."""
        param = SnippetMetaParameter(
            name="flag", type="bool", default=False, requires_when="mode"
        )
        assert param.requires_when.parameter == "mode"

    def test_gate_combined_with_visibility_raises(self):
        """A gate cannot be combined with a visibility condition on one field."""
        with pytest.raises(ValidationError, match=r"visibilit|combine"):
            SnippetMetaParameter(
                name="reason", visible_when="mode", requires_when="other"
            )

    def test_hyphenated_gated_name_raises(self):
        """A gated parameter whose own name has a hyphen is rejected."""
        with pytest.raises(ValidationError, match="valid Python identifier"):
            SnippetMetaParameter(name="ha-name", requires_when="mode")

    def test_hyphenated_referenced_parameter_raises(self):
        """A gate referencing a hyphenated sibling name is rejected."""
        with pytest.raises(ValidationError, match="valid Python identifier"):
            SnippetMetaParameter(name="reason", forbidden_when="list-mode")

    @pytest.mark.parametrize(
        "gate_field",
        [
            "requires_when",
            "requires_when_not",
            "forbidden_when",
            "forbidden_when_not",
        ],
    )
    def test_hidden_with_gate_raises(self, gate_field):
        """A hidden parameter cannot declare a gate.

        Hidden parameters are excluded from the form schema, so their gates are
        never lowered and never enforced server-side -- accepting them would be a
        silent bypass of the ``requires`` / ``forbidden`` enforcement.
        """
        with pytest.raises(ValidationError, match="hidden"):
            SnippetMetaParameter(name="reason", hidden=True, **{gate_field: "mode"})


class TestHiddenParameter:
    """Test the generic, unconditional ``hidden`` flag.

    A hidden parameter is omitted from every rendered form but is still
    validated normally, so a value the server injects (e.g. the PMM
    ``apikey``) continues to validate without a visible field.
    """

    def test_hidden_defaults_to_false(self):
        """A parameter is not hidden unless explicitly marked."""
        param = SnippetMetaParameter(name="pmmserver")
        assert param.hidden is False

    def test_hidden_can_be_set_true(self):
        """``hidden: true`` is accepted and round-trips."""
        param = SnippetMetaParameter(name="apikey", hidden=True)
        assert param.hidden is True

    def test_hidden_param_still_produces_validation_field(self):
        """A hidden param still yields a validation field so it validates."""
        param = SnippetMetaParameter(name="apikey", description="API key", hidden=True)
        field = param.to_validation_field()
        assert field.alias == "apikey"
        # validation_type is unchanged by hiding (optional str without default)
        assert param.validation_type is not None

    def test_hidden_may_combine_with_required(self):
        """A hidden parameter may also be required.

        A hidden field whose value is injected server-side may legitimately be
        required; hiding only suppresses rendering, never validation.
        """
        param = SnippetMetaParameter(name="apikey", required=True, hidden=True)
        assert param.hidden is True
        assert param.required is True

    def test_hidden_serialized_in_model_dump(self):
        """``hidden`` participates in serialization (so it joins the form cache key)."""
        param = SnippetMetaParameter(name="apikey", hidden=True)
        assert param.model_dump()["hidden"] is True

    def test_visible_parameters_excludes_hidden_and_keeps_order(self):
        """``visible_parameters`` drops hidden params while preserving order."""
        first = SnippetMetaParameter(name="pmmserver")
        hidden = SnippetMetaParameter(name="apikey", hidden=True)
        last = SnippetMetaParameter(name="node")
        result = SnippetMetaParametersValidationResult(
            parameters=[first, hidden, last], errors=[]
        )
        assert result.visible_parameters == [first, last]
        assert result.parameters == [first, hidden, last]


class TestSensitiveParameter:
    """Cover the ``sensitive`` flag that opts a parameter into value masking."""

    def test_sensitive_defaults_to_false(self):
        """Leave a parameter unmarked unless the frontmatter opts in."""
        param = SnippetMetaParameter(name="dest")
        assert param.sensitive is False

    def test_sensitive_parses_from_frontmatter(self):
        """Accept ``sensitive: true`` and round-trip it."""
        param = SnippetMetaParameter(name="auth-blob", sensitive=True)
        assert param.sensitive is True

    def test_frontmatter_without_the_key_still_parses(self):
        """Keep existing frontmatter valid now that the field exists."""
        param = SnippetMetaParameter.model_validate(
            {"name": "dest", "type": "str", "required": True}
        )
        assert param.sensitive is False

    def test_absent_from_validation_metadata_when_false(self):
        """Omit the default from the lowered metadata so readers see ``None``."""
        param = SnippetMetaParameter(name="dest")
        metadata = CustomFieldMetadata.field_to_dict(
            param.to_validation_field(), strict=True
        )
        assert metadata.get("sensitive") is None

    def test_present_in_validation_metadata_when_true(self):
        """Lower an opted-in parameter's flag onto the dynamic field's metadata."""
        param = SnippetMetaParameter(name="auth-blob", sensitive=True)
        metadata = CustomFieldMetadata.field_to_dict(
            param.to_validation_field(), strict=True
        )
        assert metadata["sensitive"] is True
