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

"""Define the static plugin schema and per-snippet schema synthesiser.

Snippets are entity-centric rather than task-centric: the plugin-level
schema describes the snippet list view but declares no forms (snippets are
discovered by ``update_snippets()`` rather than created via this API). The
per-snippet schema is synthesised at request time from the snippet's YAML
frontmatter and served at
``GET /api/apps/snippets/snippet/schema?snippet_filename=...``.
"""

__all__ = [
    "SNIPPETS_PLUGIN_SCHEMA",
    "build_snippet_schema",
    "evaluate_snippet_gates",
]

from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

from app.extensions.apps.field_names import (
    EXECUTOR_HOST_FIELD_NAME,
    EXTRA_ARGS_FIELD_NAME,
    SCRIPT_PREVIEW_FIELD_NAME,
    SUDO_FIELD_NAME,
)
from app.extensions.apps.framework.rules import (
    evaluate_conditional_rules,
    extract_forbidden_field_gate_plan,
    extract_required_field_gate_plan,
    F,
    FieldGate,
    Not,
    Predicate,
    truthy,
)
from app.extensions.apps.framework.schema import (
    AnyField,
    AppSchema,
    BoolField,
    Capabilities,
    Choice,
    ChoiceField,
    Column,
    ColumnFormat,
    DateTimeField,
    EXECUTION_HOST_LABEL,
    FloatField,
    FormSection,
    HostField,
    IntegerField,
    ListView,
    ScriptPreviewField,
    StringField,
)
from app.extensions.snippets.config import SnippetSudoOption
from app.extensions.snippets.models.meta import (
    SnippetMetaParameter,
    SnippetMetaParameterType,
    SnippetVisibilityCondition,
)
from app.extensions.snippets.models.snippet import BaseSnippet, BaseSnippetArgs

SNIPPETS_PLUGIN_SCHEMA = AppSchema(
    name="snippets",
    display_name="Snippet Manager",
    item_display_name="script",
    description=(
        "Discover and execute pre-approved support snippets against "
        "registered executor hosts."
    ),
    forms=[],
    capabilities=Capabilities(scheduling=False, alert_on_fail=False, chaining=False),
    list_view=ListView(
        columns=[
            Column(key="filename", label="Filename", sortable=True),
            Column(key="title", label="Title", sortable=True),
            Column(key="description", label="Description"),
            Column(key="isApproved", label="Approved", format=ColumnFormat.STATUS),
            Column(key="reason", label="Reason"),
            Column(key="createdAt", label="Added", format=ColumnFormat.RELATIVE),
        ],
        default_sort="filename",
    ),
)


def _choice_field_for(parameter: SnippetMetaParameter) -> ChoiceField:
    """Build a :class:`ChoiceField` from a parameter's normalised choices."""
    choices = []
    for entry in parameter.choices or []:
        value = entry["value"]
        label = entry.get("label") or value
        choices.append(Choice(value=value, label=label))
    default = None if parameter.default is None else str(parameter.default)
    return ChoiceField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=default,
        choices=choices,
    )


def _string_field_for(parameter: SnippetMetaParameter) -> StringField:
    """Build a :class:`StringField` from a STR-typed parameter."""
    return StringField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=parameter.default,
        min_length=parameter.min_length if parameter.min_length > 1 else None,
        max_length=parameter.max_length,
        pattern=parameter.pattern,
        placeholder=parameter.placeholder,
    )


def _int_field_for(parameter: SnippetMetaParameter) -> IntegerField:
    """Build an :class:`IntegerField` from an INT-typed parameter."""
    ge = parameter.ge
    if ge is None and parameter.gt is not None:
        ge = int(parameter.gt) + 1
    le = parameter.le
    if le is None and parameter.lt is not None:
        le = int(parameter.lt) - 1
    return IntegerField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=parameter.default,
        ge=int(ge) if ge is not None else None,
        le=int(le) if le is not None else None,
        step=int(parameter.step) if parameter.step is not None else None,
    )


def _float_field_for(parameter: SnippetMetaParameter) -> FloatField:
    """Build a :class:`FloatField` from a FLOAT-typed parameter."""
    ge = parameter.ge if parameter.ge is not None else parameter.gt
    le = parameter.le if parameter.le is not None else parameter.lt
    return FloatField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=parameter.default,
        ge=float(ge) if ge is not None else None,
        le=float(le) if le is not None else None,
        step=parameter.step,
    )


def _bool_field_for(parameter: SnippetMetaParameter) -> BoolField:
    """Build a :class:`BoolField` from a BOOL-typed parameter."""
    return BoolField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=parameter.default,
    )


def _datetime_field_for(parameter: SnippetMetaParameter) -> DateTimeField:
    """Build a :class:`DateTimeField` from a DATETIME-typed parameter."""
    return DateTimeField(
        name=parameter.name,
        label=parameter.label or parameter.name,
        required=parameter.required,
        description=parameter.description,
        default=parameter.default,
    )


_FIELD_BUILDERS = {
    SnippetMetaParameterType.STR: _string_field_for,
    SnippetMetaParameterType.INT: _int_field_for,
    SnippetMetaParameterType.FLOAT: _float_field_for,
    SnippetMetaParameterType.BOOL: _bool_field_for,
    SnippetMetaParameterType.DATETIME: _datetime_field_for,
}


def _visibility_forbidden(parameter: SnippetMetaParameter) -> list[FieldGate] | None:
    """Lower a parameter's visibility condition onto a ``forbidden`` gate.

    ``visible_when_not(cond)`` hides the field when ``cond`` matches, so it
    forbids the field when the predicate fires. ``visible_when(cond)`` hides the
    field when ``cond`` does **not** match, so it forbids on the negated
    predicate. A condition's ``equals`` value yields an equality predicate;
    otherwise a truthiness predicate on the referenced parameter is used.

    .. note::
        The React renderer hides the field and drops its value from the payload.
        The resulting gate is *also* enforced server-side on the execute paths
        via :func:`evaluate_snippet_gates`, which rejects a value submitted
        directly for a gated-hidden field (matching ``@apply_conditional_rules``
        for hand-coded plugins).

    :param parameter: The validated snippet meta parameter.
    :type parameter: SnippetMetaParameter
    :return: A single-element list with the ``forbidden`` gate, or ``None`` when
        the parameter declares no visibility condition.
    :rtype: list[FieldGate] | None
    """
    condition = parameter.visible_when or parameter.visible_when_not
    if condition is None:
        return None
    predicate = _gate_predicate(condition, negated=parameter.visible_when is not None)
    return [FieldGate(when=predicate)]


def _gate_predicate(
    condition: SnippetVisibilityCondition, *, negated: bool
) -> Predicate:
    """Build the predicate for a bounded gate/visibility condition.

    A condition matches its referenced sibling by truthiness (when ``equals`` is
    ``None``) or by equality (otherwise); ``negated`` wraps the predicate in
    ``Not`` so the condition fires on the inverse. The wire shapes produced
    (``{"truthy": ...}``, ``{"equals": {...}}``, ``{"not": {...}}``) match the
    marker DSL, keeping the format shared.

    :param condition: The bounded sibling condition to lower.
    :param negated: Whether to negate the predicate (the ``_when_not`` variants
        and ``visible_when``).
    :return: The framework predicate encoding the condition.
    """
    base = (
        truthy(condition.parameter)
        if condition.equals is None
        else F(condition.parameter) == condition.equals
    )
    return Not(base) if negated else base


def _requires_gates(parameter: SnippetMetaParameter) -> list[FieldGate] | None:
    """Lower a parameter's ``requires_when`` / ``requires_when_not`` onto a gate.

    :param parameter: The validated snippet meta parameter.
    :return: A single-element list with the ``requires`` gate, or ``None`` when
        the parameter declares no requires gate.
    """
    for condition, negated in (
        (parameter.requires_when, False),
        (parameter.requires_when_not, True),
    ):
        if condition is not None:
            return [FieldGate(when=_gate_predicate(condition, negated=negated))]
    return None


def _forbidden_gates(parameter: SnippetMetaParameter) -> list[FieldGate] | None:
    """Lower a parameter's forbidden gate and visibility condition onto gates.

    Both the visibility-derived forbidden gate (see :func:`_visibility_forbidden`)
    and an explicit ``forbidden_when`` / ``forbidden_when_not`` gate land on the
    field's ``forbidden`` list. The two sources are mutually exclusive at parse
    time, but the merge is defensive.

    :param parameter: The validated snippet meta parameter.
    :return: The combined ``forbidden`` gate list, or ``None`` when the parameter
        declares neither a visibility condition nor a forbidden gate.
    """
    gates = list(_visibility_forbidden(parameter) or [])
    for condition, negated in (
        (parameter.forbidden_when, False),
        (parameter.forbidden_when_not, True),
    ):
        if condition is not None:
            gates.append(FieldGate(when=_gate_predicate(condition, negated=negated)))
    return gates or None


def field_for(parameter: SnippetMetaParameter) -> AnyField:
    """Map a snippet meta parameter to its framework field counterpart.

    A parameter declaring ``choices`` always maps to :class:`ChoiceField`
    regardless of ``py_type``. A parameter declaring a visibility condition
    (``visible_when`` / ``visible_when_not``) or a bounded gate (``requires_when``
    / ``requires_when_not`` / ``forbidden_when`` / ``forbidden_when_not``)
    additionally carries the corresponding ``requires`` / ``forbidden`` gate lists
    (see :func:`_requires_gates` / :func:`_forbidden_gates`).

    :param parameter: The validated snippet meta parameter.
    :type parameter: SnippetMetaParameter
    :return: The corresponding framework field instance.
    :rtype: AnyField
    """
    if parameter.choices:
        field = cast(AnyField, _choice_field_for(parameter))
    else:
        field = cast(AnyField, _FIELD_BUILDERS[parameter.py_type](parameter))
    update = {}
    requires = _requires_gates(parameter)
    if requires is not None:
        update["requires"] = requires
    forbidden = _forbidden_gates(parameter)
    if forbidden is not None:
        update["forbidden"] = forbidden
    if update:
        field = field.model_copy(update=update)
    return field


def build_snippet_schema(snippet: BaseSnippet) -> AppSchema:
    """Synthesise the per-snippet form schema for a single snippet.

    The schema includes one form section per parameter group declared in
    the snippet metadata (or a single ``"Parameters"`` section if no
    groups are declared), plus a trailing ``"Execution"`` section for
    dispatch controls and a separate collapsible ``"Script preview"``
    section rendered after submit. Parameters marked ``hidden`` are excluded
    from the parameter sections.

    ``display_name`` is the snippet's own title, but the record this form
    creates is a *run* of that snippet rather than the snippet itself, so the
    record names are fixed here instead of derived from the title.

    :param snippet: The snippet whose schema to synthesise.
    :return: The fully-validated plugin schema for this single snippet.
    """
    parameter_sections = {}
    for parameter in snippet.validated_parameters.visible_parameters:
        section_title = parameter.group or "Parameters"
        parameter_sections.setdefault(section_title, []).append(field_for(parameter))

    forms = [
        FormSection(title=title, fields=fields)
        for title, fields in parameter_sections.items()
    ]

    execution_fields = [
        cast(
            AnyField,
            HostField(
                name=EXECUTOR_HOST_FIELD_NAME,
                label=EXECUTION_HOST_LABEL,
                required=True,
            ),
        ),
    ]
    if snippet.sudo.is_optional:
        execution_fields.append(
            cast(
                AnyField,
                BoolField(
                    name=SUDO_FIELD_NAME,
                    label="Run with sudo",
                    default=snippet.sudo.sudo_default,
                    description="Prepend sudo to the interpreter when the snippet is executed.",
                ),
            ),
        )
    elif snippet.sudo == SnippetSudoOption.ALWAYS:
        execution_fields.append(
            cast(
                AnyField,
                BoolField(
                    name=SUDO_FIELD_NAME,
                    label="Run with sudo",
                    default=True,
                    description=("This snippet is configured to always run with sudo."),
                ),
            ),
        )
    if snippet.allow_extra_args:
        execution_fields.append(
            cast(
                AnyField,
                StringField(
                    name=EXTRA_ARGS_FIELD_NAME,
                    label="Extra Args",
                    placeholder="e.g. --verbose",
                    description="Any extra args to pass to the snippet execution command",
                ),
            ),
        )
    forms.append(FormSection(title="Execution", fields=execution_fields))
    forms.append(
        FormSection(
            title="Script preview",
            fields=[
                cast(
                    AnyField,
                    ScriptPreviewField(
                        name=SCRIPT_PREVIEW_FIELD_NAME,
                        label="Snippet file",
                        endpoint_url=(
                            "/apps/snippets/snippet/preview?"
                            + urlencode({"snippet_filename": snippet.filename})
                        ),
                    ),
                )
            ],
            collapsible=True,
            collapsed_by_default=True,
            render_after_submit=True,
        )
    )

    return AppSchema(
        name="snippets",
        display_name=snippet.title,
        item_display_name="run",
        description=snippet.description or None,
        forms=forms,
        list_view=SNIPPETS_PLUGIN_SCHEMA.list_view,
    )


def evaluate_snippet_gates(
    snippet: BaseSnippet, execution_args: BaseSnippetArgs
) -> list[str]:
    """Return failure messages for any snippet field gate that fires.

    Snippet ``visible_when`` / ``visible_when_not`` conditions and bounded
    ``forbidden_when`` / ``forbidden_when_not`` gates are lowered onto
    ``forbidden=[FieldGate(...)]``, and ``requires_when`` / ``requires_when_not``
    gates onto ``requires=[FieldGate(...)]``, by :func:`field_for`. This reuses
    the framework ``field_gate_forbidden`` / ``field_gate_requires`` engines to
    enforce them server-side on the execute paths, matching how
    ``@apply_conditional_rules`` enforces hand-coded plugins: a value submitted
    for a parameter whose ``forbidden`` gate fires is rejected, and a parameter
    omitted while its ``requires`` gate fires is rejected (given the rest of the
    submission).

    Gates reference parameters by their wire (alias) name, so evaluation runs
    against the alias-shaped view of the validated args
    (``model_dump(by_alias=True)``), not the model's generated python attribute
    names — otherwise the predicates would silently never resolve.

    :param snippet: The snippet whose field gates to enforce.
    :param execution_args: The already type/presence-validated execution args.
    :return: One message per fired gate; empty when every gate passes (including
        gateless snippets, which take the identical path to before).
    """
    schema = build_snippet_schema(snippet)
    forbidden_plan = extract_forbidden_field_gate_plan(schema)
    required_plan = extract_required_field_gate_plan(schema)
    if not forbidden_plan.rules and not required_plan.rules:
        return []
    alias_view = SimpleNamespace()
    alias_view.__dict__.update(execution_args.model_dump(by_alias=True))
    return [
        *evaluate_conditional_rules(alias_view, forbidden_plan),
        *evaluate_conditional_rules(alias_view, required_plan),
    ]
