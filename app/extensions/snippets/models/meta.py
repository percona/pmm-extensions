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

"""Define models for snippet metadata in the PMM Extensions app."""

__all__ = [
    "META_KEY_ALERTS",
    "META_KEY_ALLOW_EXTRA_ARGS",
    "META_KEY_ATW",
    "META_KEY_DESCRIPTION",
    "META_KEY_DIAGNOSTIC_CATEGORIES",
    "META_KEY_PARAMETERS",
    "META_KEY_REQUIRES_PACKAGES",
    "META_KEY_SERVICE_TYPE",
    "META_KEY_SUDO",
    "META_KEY_TITLE",
    "SUPPORTED_META_KEYS",
    "SnippetMetaParameter",
    "SnippetMetaParameterChoice",
    "SnippetMetaParameterType",
    "SnippetMetaParametersValidationResult",
    "SnippetVisibilityCondition",
    "serialize_cli_value",
]

import logging
from collections.abc import Callable
from datetime import datetime
from enum import Enum, StrEnum
from functools import cached_property
from typing import Annotated, Any, NamedTuple, NotRequired, Self

from annotated_types import (
    GroupedMetadata,
    Interval,
)
from pydantic import (
    AliasChoices,
    BaseModel,
    computed_field,
    Field,
    field_validator,
    model_validator,
    PositiveInt,
    StringConstraints,
    ValidationError,
)
from pydantic.fields import FieldInfo
from pydantic_core.core_schema import ValidationInfo, ValidatorFunctionWrapHandler
from typing_extensions import TypedDict

from app.core.utils import run_pydantic_type_validator, shorten_text
from app.core.utils.cli_args import is_value_arg_template
from app.core.utils.date_time import make_datetime_utc
from app.core.utils.fields import (
    EmptyStrToNone,
    EnumFieldMixin,
    NonEmptyStr,
    UTCDatetime,
    value_is_present,
)
from app.core.utils.pydantic import (
    field_with_metadata,
    loc_to_dot_sep,
)
from app.extensions.apps.field_names import (
    RESERVED_EXECUTION_FIELD_NAMES,
)
from app.extensions.snippets.models.constants import (
    TextInputHTMLElement,
)

ParameterType = str | int | float | bool | datetime | None

logger = logging.getLogger(__name__)

META_KEY_ATW = "atw"
"""Legacy ``meta`` JSON key holding diagnostics-browser categories during migration."""

META_KEY_TITLE = "title"
"""``meta`` JSON key holding a snippet's human title."""

META_KEY_DESCRIPTION = "description"
"""``meta`` JSON key holding a snippet's description."""

META_KEY_SERVICE_TYPE = "service_type"
"""``meta`` JSON key holding a snippet's free-form service type."""

META_KEY_PARAMETERS = "parameters"
"""``meta`` JSON key holding a snippet's parameter declarations."""

META_KEY_ALLOW_EXTRA_ARGS = "allow_extra_args"
"""``meta`` JSON key controlling whether free-form extra arguments are accepted."""

META_KEY_SUDO = "sudo"
"""``meta`` JSON key holding the snippet's sudo-execution policy."""

META_KEY_REQUIRES_PACKAGES = "requires_packages"
"""``meta`` JSON key holding any prerequisite packages for the snippet."""

META_KEY_ALERTS = "alerts"
"""``meta`` JSON key tagging snippets with alert-troubleshooting metadata."""

META_KEY_DIAGNOSTIC_CATEGORIES = "diagnostic_categories"
"""``meta`` JSON key naming the diagnostics-browser categories a script sits under."""

SUPPORTED_META_KEYS = frozenset(
    {
        META_KEY_ALERTS,
        META_KEY_ALLOW_EXTRA_ARGS,
        META_KEY_DESCRIPTION,
        META_KEY_DIAGNOSTIC_CATEGORIES,
        META_KEY_PARAMETERS,
        META_KEY_REQUIRES_PACKAGES,
        META_KEY_SERVICE_TYPE,
        META_KEY_SUDO,
        META_KEY_TITLE,
    }
)
"""All supported top-level snippet front-matter keys."""


def _param_choices_enum(choices: list[dict[str, Any]]) -> type[StrEnum]:
    """Build the choice enum a parameter validates its value against.

    Kept as a function so the enum's own name stays ``ParamChoices`` rather than
    having to match the variable it is assigned to at the call site.

    :param choices: The parameter's declared choices.
    :return: A ``StrEnum`` over the choice values.
    """
    return StrEnum("ParamChoices", [choice["value"] for choice in choices])


class SnippetMetaParameterType(EnumFieldMixin, Enum):
    """Enumerate the possible types for snippet parameters."""

    STR = str
    INT = int
    FLOAT = float
    BOOL = bool
    DATETIME = UTCDatetime


_CLI_VALUE_SERIALIZERS: dict[type, Callable[[Any], str]] = {
    datetime: lambda value: make_datetime_utc(value).strftime("%Y-%m-%dT%H:%M:%S"),
}


def serialize_cli_value(value: Any) -> str:
    """Serialize a validated parameter value for command-line argument substitution.

    Datetime values are normalized to UTC before formatting as an ISO-8601
    ``T``-separated form without microseconds.
    All other types fall back to ``str(value)``.

    :param value: The validated parameter value to serialize.
    :type value: Any
    :return: The command-line string representation of ``value``.
    :rtype: str
    """
    serializer = _CLI_VALUE_SERIALIZERS.get(type(value))
    return serializer(value) if serializer else str(value)


class SnippetMetaParametersValidationResult(NamedTuple):
    """A collection of validated snippet parameters and any validation errors.

    :param parameters: A list of validated snippet parameters.
    :type parameters: list[app.extensions.snippets.meta.SnippetMetaParameter]
    :param errors: A list of validation error messages.
    :type errors: list[str]
    """

    parameters: list["SnippetMetaParameter"]
    errors: list[str]

    @property
    def visible_parameters(self) -> list["SnippetMetaParameter"]:
        """Return the parameters that are rendered in execution forms.

        Parameters marked ``hidden`` are excluded. They remain in
        :attr:`parameters` and are still validated normally; only their form
        rendering is suppressed.

        :return: The non-hidden parameters, in declaration order.
        :rtype: list[SnippetMetaParameter]
        """
        return [param for param in self.parameters if not param.hidden]


class SnippetVisibilityCondition(BaseModel):
    """Reference a sibling parameter for a conditional-visibility rule.

    A condition names one sibling parameter and matches either its truthiness
    (when ``equals`` is ``None``) or its equality against a literal value.

    .. note::
        Visibility conditions lower onto the framework's ``forbidden``
        :class:`~app.extensions.apps.framework.rules.FieldGate`. The React renderer
        evaluates them to hide the field and drop its value from the submitted
        payload, and both snippet execute paths additionally enforce them
        server-side: a value submitted directly for a field whose gate fires is
        rejected (HTTP 422 on the JSON API; flash + redirect on the legacy form),
        matching ``field_gate_forbidden`` "must be absent" semantics. See
        :func:`app.extensions.snippets.schema.evaluate_snippet_gates`.

    :param parameter: The name of the sibling parameter the rule references.
    :param equals: The literal the referenced parameter must equal for the
        condition to match. Defaults to ``None``, meaning a truthiness match.
    """

    parameter: NonEmptyStr
    equals: ParameterType = None


class SnippetMetaParameterChoice(TypedDict):
    """Represent a choice for a snippet parameter.

    :param value: The value of the choice.
    :type value: str
    :param label: The label for the choice. If not provided, the value will be used as
        the label.
    :type label: NotRequired[str]
    """

    value: str
    label: NotRequired[str]


class SnippetMetaParameter(BaseModel):
    """Represent a parameter for a support snippet.

    :param name: The name of the parameter.
    :param py_type: The type of the parameter (``str``, ``int``, ``float``, ``bool``).
        Defaults to ``str``. This parameter is validated as "type" in input data.
    :param required: Whether the parameter is required. Defaults to False.
    :param positional: Whether the parameter is positional. Defaults to False.
    :param arg_format: The format string for the parameter when used as a command-line
        argument. Use ``${value}`` as a placeholder (required for non-flag arguments).
        Defaults to None, which uses the default format from the snippets settings.
    :param description: A description of the parameter. Defaults to None, meaning it
        won't be used for validation.
    :param label: A label for the parameter. Defaults to None, meaning it won't be used
        for validation.
    :param placeholder: A placeholder for the parameter. Defaults to None, meaning it
        won't be used for validation.
    :param group: An optional group name for organizing parameters into separate
        fieldsets in the execution form. Parameters sharing the same group are rendered
        together. Defaults to None, meaning the parameter belongs to the default
        ungrouped fieldset.
    :param default: The default value for the parameter. Defaults to None, meaning no
        default.
    :param choices: A list of choices for the parameter. Each choice can be a string or
        a dictionary with "label" and "value" keys. Defaults to None, meaning it won't
        be used for validation. This parameter is validated as "options" or "choices"
        in input data.
    :param min_length: The minimum length for string parameters. Defaults to 1.
    :param max_length: The maximum length for string parameters. Defaults to 1,
        meaning it won't be used for validation.
    :param pattern: A regex pattern that the parameter value must match. Defaults to
        None, meaning it won't be used for validation.
    :param gt: The value must be greater than this for numeric parameters. Defaults to
        None, meaning it won't be used for validation.
    :param lt: The value must be less than this for numeric parameters. Defaults to
        None, meaning it won't be used for validation.
    :param ge: The value must be greater than or equal to this for numeric parameters.
        Defaults to None, meaning it won't be used for validation.
    :param le: The value must be less than or equal to this for numeric parameters.
        Defaults to None, meaning it won't be used for validation.
    :param step: The step value for numeric parameters. Defaults to None, which sets
        step to 1 for int and 0.1 for float types.
    :param html_elem: The HTML element to use for text input parameters. Can be
        TextInputHTMLElement.TEXT or TextInputHTMLElement.TEXTAREA. Defaults to None,
        which uses TextInputHTMLElement.TEXT.
    :param visible_when: Hide this parameter unless the referenced sibling
        condition matches. Accepts a bare parameter name (truthiness match) or a
        mapping with ``parameter`` and optional ``equals``. Mutually exclusive
        with ``visible_when_not``. Client-enforced only (see
        :class:`SnippetVisibilityCondition`). Defaults to None.
    :param visible_when_not: Hide this parameter when the referenced sibling
        condition matches. Same grammar as ``visible_when``. Mutually exclusive
        with ``visible_when``. Client-enforced only. Defaults to None.
    :param requires_when: Require a value for this parameter when the referenced
        sibling condition matches. Same grammar as ``visible_when``. Mutually
        exclusive with ``requires_when_not``. Lowered onto a ``requires``
        :class:`~app.extensions.apps.framework.rules.FieldGate` and enforced server-side
        on the execute paths (see
        :func:`app.extensions.snippets.schema.evaluate_snippet_gates`). Defaults to
        None.
    :param requires_when_not: Require a value for this parameter when the
        referenced sibling condition does not match. Same grammar as
        ``requires_when``. Mutually exclusive with ``requires_when``. Enforced
        server-side. Defaults to None.
    :param forbidden_when: Forbid a value for this parameter when the referenced
        sibling condition matches. Same grammar as ``visible_when``. Mutually
        exclusive with ``forbidden_when_not``. Lowered onto a ``forbidden``
        :class:`~app.extensions.apps.framework.rules.FieldGate` and enforced server-side.
        Defaults to None.
    :param forbidden_when_not: Forbid a value for this parameter when the
        referenced sibling condition does not match. Same grammar as
        ``forbidden_when``. Mutually exclusive with ``forbidden_when``. Enforced
        server-side. Defaults to None.
    :param hidden: Unconditionally omit this parameter from every rendered
        execution form -- it is never emitted into the form HTML or form schema
        at all. It is still validated normally (it stays in
        ``to_validation_field``), so a value injected server-side -- e.g. the PMM
        ``apikey`` from ``settings.PMM.api_key`` -- continues to validate without
        a visible field. Defaults to False.
    :param sensitive: Treat this parameter's value as a credential, so the run
        form hides it as it is typed and surfaces that replay a recorded command
        line replace it with a fixed-width mask
        (see :func:`app.extensions.snippets.masking.is_credential_parameter`).
        Parameters whose name carries a credential word are treated the same
        without opting in; this flag covers the ones the name pattern cannot
        recognise. Defaults to ``False``.
    """

    name: NonEmptyStr = Field(
        ..., pattern=r"^\w(?:[\w-]*\w)?$", serialization_alias="title"
    )

    py_type: SnippetMetaParameterType = Field(
        SnippetMetaParameterType.STR, validation_alias="type"
    )
    required: bool = False
    positional: bool = False
    arg_format: NonEmptyStr | None = None
    description: NonEmptyStr | None = None
    label: NonEmptyStr | None = None
    placeholder: NonEmptyStr | None = None
    group: NonEmptyStr | None = None
    default: ParameterType = None
    choices: list[SnippetMetaParameterChoice] | None = Field(
        None, validation_alias=AliasChoices("choices", "options")
    )
    min_length: PositiveInt = 1
    max_length: PositiveInt | None = None
    pattern: NonEmptyStr | None = None
    gt: ParameterType = None
    lt: ParameterType = None
    ge: ParameterType = None
    le: ParameterType = None
    step: float | None = None
    html_elem: TextInputHTMLElement | None = None
    visible_when: SnippetVisibilityCondition | None = None
    visible_when_not: SnippetVisibilityCondition | None = None
    requires_when: SnippetVisibilityCondition | None = None
    requires_when_not: SnippetVisibilityCondition | None = None
    forbidden_when: SnippetVisibilityCondition | None = None
    forbidden_when_not: SnippetVisibilityCondition | None = None
    hidden: bool = False
    sensitive: bool = False

    @field_validator("name")
    @classmethod
    def _reject_reserved_name(cls, value: str) -> str:
        """Reject a parameter name reserved for a synthesized execution field.

        Reservation is unconditional and case-sensitive. A name in
        :data:`~app.extensions.apps.field_names.RESERVED_EXECUTION_FIELD_NAMES` is
        rejected even when the app rendering this script never synthesizes that
        particular field, so an author can apply the rule to the frontmatter in
        front of them without knowing which app will render it.

        :param value: The candidate parameter name.
        :return: ``value`` unchanged, when it is not reserved.
        :raises ValueError: When the name matches a reserved synthesized field name.
        """
        if value in RESERVED_EXECUTION_FIELD_NAMES:
            reserved = ", ".join(sorted(RESERVED_EXECUTION_FIELD_NAMES))
            raise ValueError(
                f"parameter name {value!r} is reserved for a synthesized "
                f"execution field; rename it (reserved names: {reserved})"
            )
        return value

    @field_validator(
        "visible_when",
        "visible_when_not",
        "requires_when",
        "requires_when_not",
        "forbidden_when",
        "forbidden_when_not",
        mode="before",
    )
    @classmethod
    def normalize_visibility_condition(cls, value: Any) -> Any:
        """Normalize a bare string into a truthiness visibility condition.

        :param value: The input value to normalize.
        :type value: Any
        :return: A condition mapping when given a bare string, else the input.
        :rtype: Any
        """
        if isinstance(value, str):
            return {"parameter": value}
        return value

    @model_validator(mode="after")
    def validate_visibility_conditions(self) -> Self:
        """Validate the visible_when / visible_when_not conditions.

        :return: The validated :class:`SnippetMetaParameter` instance.
        :rtype: SnippetMetaParameter
        :raises ValueError: If both conditions are declared, a condition
            references the parameter itself, a condition is combined with
            ``required=True`` (a hidden field is dropped client-side and would
            then fail server-side required validation), a condition is combined
            with a non-empty ``default`` (the dropped field is backfilled with
            the default, which the server-side forbidden gate then sees as
            present and rejects — an unsatisfiable trap), or a gated
            parameter's name or referenced sibling is not a valid Python
            identifier (the framework conditional-rules engine rejects
            hyphenated names).
        """
        if self.visible_when is not None and self.visible_when_not is not None:
            raise ValueError("declare only one of 'visible_when' or 'visible_when_not'")
        condition = self.visible_when or self.visible_when_not
        if condition is None:
            return self
        if condition.parameter == self.name:
            raise ValueError(
                "a visibility condition cannot reference the parameter itself"
            )
        if self.required:
            raise ValueError(
                "a required parameter cannot declare a visibility condition"
            )
        if value_is_present(self.default):
            raise ValueError(
                "a parameter with a non-empty default cannot declare a "
                "visibility condition"
            )
        if not self.name.isidentifier():
            raise ValueError(
                f"a parameter declaring a visibility condition must have a name "
                f"that is a valid Python identifier (no hyphens); got {self.name!r}"
            )
        if not condition.parameter.isidentifier():
            raise ValueError(
                f"a visibility condition must reference a parameter whose name is "
                f"a valid Python identifier (no hyphens); got "
                f"{condition.parameter!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_gate_conditions(self) -> Self:
        """Validate the requires/forbidden field-gate conditions.

        The four gate fields (``requires_when`` / ``requires_when_not`` /
        ``forbidden_when`` / ``forbidden_when_not``) reuse the visibility-condition
        shape and lower onto the framework's ``requires`` / ``forbidden``
        :class:`FieldGate` lists, enforced server-side on the execute paths (see
        :func:`app.extensions.snippets.schema.evaluate_snippet_gates`).

        :return: The validated :class:`SnippetMetaParameter` instance.
        :raises ValueError: If both variants of a kind are declared together; if a
            gate is declared on a ``hidden`` parameter (hidden parameters are
            excluded from the form schema, so the gate would never be lowered or
            enforced — a silent bypass); if a gate is combined with a visibility
            condition (visibility already emits a forbidden gate — the combination
            is ambiguous); if a gate is combined with ``required=True`` (``requires``
            is redundant, ``forbidden`` is unsatisfiable); if a gate is combined
            with a non-empty ``default`` (the backfilled default makes a
            ``forbidden`` gate an unsatisfiable trap and a ``requires`` gate a dead
            rule); or if a gated parameter's name or referenced sibling is not a
            valid Python identifier.
        """
        if self.requires_when is not None and self.requires_when_not is not None:
            raise ValueError(
                "declare only one of 'requires_when' or 'requires_when_not'"
            )
        if self.forbidden_when is not None and self.forbidden_when_not is not None:
            raise ValueError(
                "declare only one of 'forbidden_when' or 'forbidden_when_not'"
            )
        requires = (self.requires_when, self.requires_when_not)
        forbidden = (self.forbidden_when, self.forbidden_when_not)
        gate_conditions = [c for c in (*requires, *forbidden) if c is not None]
        if not gate_conditions:
            return self
        if self.hidden:
            raise ValueError(
                "a hidden parameter cannot declare a requires/forbidden gate "
                "(hidden parameters are excluded from the form schema, so the "
                "gate would never be enforced server-side)"
            )
        if self.visible_when is not None or self.visible_when_not is not None:
            raise ValueError(
                "a parameter cannot combine a requires/forbidden gate with a "
                "visibility condition"
            )
        if self.required and any(c is not None for c in requires):
            raise ValueError("a required parameter cannot declare a 'requires' gate")
        if self.required and any(c is not None for c in forbidden):
            raise ValueError("a required parameter cannot declare a 'forbidden' gate")
        if value_is_present(self.default):
            raise ValueError(
                "a parameter with a non-empty default cannot declare a "
                "requires/forbidden gate"
            )
        if not self.name.isidentifier():
            raise ValueError(
                f"a parameter declaring a requires/forbidden gate must have a name "
                f"that is a valid Python identifier (no hyphens); got {self.name!r}"
            )
        self._validate_gate_references(gate_conditions)
        return self

    def _validate_gate_references(
        self, conditions: list["SnippetVisibilityCondition"]
    ) -> None:
        """Reject gate conditions that self-reference or name a non-identifier.

        :param conditions: The gate conditions declared on this parameter.
        :raises ValueError: If a condition references the parameter itself or a
            sibling whose name is not a valid Python identifier.
        """
        for condition in conditions:
            if condition.parameter == self.name:
                raise ValueError(
                    "a requires/forbidden gate cannot reference the parameter itself"
                )
            if not condition.parameter.isidentifier():
                raise ValueError(
                    f"a requires/forbidden gate must reference a parameter whose "
                    f"name is a valid Python identifier (no hyphens); got "
                    f"{condition.parameter!r}"
                )

    @model_validator(mode="after")
    def set_default_step(self) -> Self:
        """Set default step for numeric parameters if not provided.

        :return: The instance of SnippetMetaParameter with the step set if it is a
            numeric type.
        :rtype: SnippetMetaParameter
        """
        if self.step is None:
            if self.py_type == SnippetMetaParameterType.FLOAT:
                self.step = 0.1
            elif self.py_type == SnippetMetaParameterType.INT:
                self.step = 1.0
        return self

    @model_validator(mode="after")
    def validate_arg_format(self) -> Self:
        """Validate the arg_format for non-flag parameters contains '${value}'.

        :return: The validated ``SnippetMetaParameter`` instance.
        :rtype: SnippetMetaParameter
        :raises ValueError: If arg_format is provided for non-flag parameters but
            does not include '${value}'.
        """
        if (
            not self.is_flag
            and self.arg_format is not None
            and not is_value_arg_template(self.arg_format)
        ):
            raise ValueError(
                "arg_format must include '${value}' for non-flag parameters"
            )
        return self

    @field_validator("choices", mode="before")
    @classmethod
    def normalize_choices(cls, value: Any) -> Any:
        """Normalize choices to a list of dictionaries with 'value'.

        :param value: The input value to normalize.
        :type value: Any
        :return: The normalized list of choices or the original input value.
        :rtype: Any
        """
        if isinstance(value, list):
            return [
                {"value": choice} if isinstance(choice, str) else choice
                for choice in value
            ]
        return value

    @field_validator("py_type", mode="wrap")
    @classmethod
    def set_default_type_if_unknown(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> SnippetMetaParameterType:
        """Set default type to str if the provided type is unknown.

        :param value: The input value to validate.
        :type value: Any
        :param handler: The validation handler function.
        :type handler: ValidatorFunctionWrapHandler
        :return: The validated type, defaulting to str if unknown.
        :rtype: SnippetMetaParameterType
        """
        try:
            return handler(value)
        except ValidationError:
            logger.debug(
                "Unknown type %s for snippet parameter, defaulting to str", value
            )
            return SnippetMetaParameterType.STR

    @field_validator("default", "gt", "lt", "ge", "le")
    @classmethod
    def coerce_to_type(
        cls, value: ParameterType, info: ValidationInfo
    ) -> ParameterType:
        """Validate and coerce the value to the specified parameter type.

        :param value: The value to validate.
        :type value: ParameterType
        :param info: The validation information.
        :type info: ValidationInfo
        :return: The validated default value.
        :rtype: ParameterType
        """
        if value is None:
            return value
        param_type = info.data["py_type"]
        return run_pydantic_type_validator(param_type.value, value)

    @computed_field
    @cached_property
    def is_flag(self) -> bool:
        """Determine if the parameter is a boolean flag.

        :return: True if the parameter is of type BOOL, else False.
        :rtype: bool
        """
        return self.py_type == SnippetMetaParameterType.BOOL

    @cached_property
    def constraints(self) -> list[GroupedMetadata]:
        """Get the constraints for the parameter based on its type and attributes.

        :return: A list of constraint metadata instances applicable to the parameter.
        :rtype: list[GroupedMetadata]
        """
        constraints = []
        if self.py_type == SnippetMetaParameterType.STR:
            constraints.append(
                StringConstraints(
                    **self.model_dump(
                        include={"min_length", "max_length", "pattern"},
                        exclude_none=True,
                    )
                )
            )
        interval_constraints = self.model_dump(
            include={"gt", "lt", "ge", "le"}, exclude_none=True
        )
        if interval_constraints:
            constraints.append(Interval(**interval_constraints))
        return constraints

    @cached_property
    def validation_type(
        self,
    ) -> Any:
        """Get the validation type for the parameter.

        Annotated ``Any`` because the value is a type *expression* assembled at
        runtime — an ``Annotated[...]`` form or a ``X | None`` union — neither
        of which is a ``type``.

        :return: The type to use for validating the parameter value.
        """
        if self.choices:
            raw_type = _param_choices_enum(self.choices)
        else:
            raw_type = self.py_type.value
            if self.constraints:
                raw_type = Annotated[raw_type, *self.constraints]
        if not self.required and self.default is None:
            return raw_type | EmptyStrToNone
        return raw_type

    def to_validation_field(self) -> FieldInfo:
        """Convert the SnippetMetaParameter to a Pydantic Field.

        :return: A Pydantic Field instance with the attributes of the parameter.
        :rtype: FieldInfo
        """
        attrs = self.model_dump(
            include={
                "name",
                "description",
                "default",
            },
            by_alias=True,
            exclude_none=True,
        )
        if not self.required and self.default is None:
            attrs["default"] = None
        return field_with_metadata(
            **attrs,
            alias=self.name,
            metadata=self.model_dump(
                include={"positional", "is_flag", "arg_format", "sensitive"},
                exclude_none=True,
                exclude_defaults=True,
            ),
        )

    @staticmethod
    def convert_validation_errors(exc: ValidationError, input_param: Any) -> list[str]:
        """Convert Pydantic ValidationError to a list of readable error messages.

        :param exc: The ValidationError instance to convert.
        :type exc: ValidationError
        :param input_param: The input parameter that caused the validation error.
        :type input_param: Any
        :return: A list of formatted error messages.
        :rtype: list[str]
        """
        errors = []
        base_error_msg = "Parameter error"
        if isinstance(input_param, dict) and "name" in input_param:
            param_preview = repr(input_param["name"])
        else:
            param_preview = repr(input_param)
        param_preview = shorten_text(param_preview, keep_last_chars=1)
        for param_error in exc.errors():
            param_error_loc = loc_to_dot_sep(param_error.get("loc", ()))
            param_error_msg = param_error.get("msg")
            error_msg = f"{base_error_msg} ({param_preview})"
            if param_error_loc:
                error_msg += f" at {param_error_loc!r}"
            if param_error_msg:
                error_msg += f": {param_error_msg}"
            errors.append(error_msg)
        return errors
