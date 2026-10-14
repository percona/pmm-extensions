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

"""Tests for the snippets plugin schema synthesiser."""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import pytest

from app.extensions.apps.dipper.models import DipperScript
from app.extensions.apps.field_names import (
    EXECUTOR_HOST_FIELD_NAME,
    EXTRA_ARGS_FIELD_NAME,
    RESERVED_EXECUTION_FIELD_NAMES,
    SCRIPT_PREVIEW_FIELD_NAME,
    SUDO_FIELD_NAME,
)
from app.extensions.apps.framework.schema import (
    BoolField,
    ChoiceField,
    DateTimeField,
    HostField,
    IntegerField,
    ScriptPreviewField,
    StringField,
)
from app.extensions.snippets.config import snippets_settings, SnippetSudoOption
from app.extensions.snippets.masking import is_credential_parameter
from app.extensions.snippets.models.meta import (
    SnippetMetaParameter,
    SnippetMetaParameterType,
)
from app.extensions.snippets.models.snippet import (
    EXECUTOR_HOSTS_INPUT_NAME,
    Snippet,
)
from app.extensions.snippets.schema import (
    build_snippet_schema,
    evaluate_snippet_gates,
    field_for,
    SNIPPETS_PLUGIN_SCHEMA,
)
from tests.app.extensions.form_schema_utils import (
    assert_only_synthesized_fields,
    form_field_names,
    form_field_types,
)
from tests.app.extensions.snippets.snippet_kit import drop_cached_reads


def test_static_schema_has_no_forms_and_keyed_columns():
    """The static plugin schema declares no forms and lists snippet columns."""
    assert SNIPPETS_PLUGIN_SCHEMA.name == "snippets"
    assert SNIPPETS_PLUGIN_SCHEMA.forms == []
    column_keys = [column.key for column in SNIPPETS_PLUGIN_SCHEMA.list_view.columns]
    assert "filename" in column_keys
    assert "isApproved" in column_keys


def test_static_schema_declares_every_capability_unsupported():
    """Declare every capability unsupported, including the ones left at default.

    Chaining, scheduling and alerting were never offered by the legacy Jinja UI
    either, so the declaration records their absence rather than a gap. Derive
    the field list from the model so a capability added later has to be decided
    here rather than defaulting in unnoticed.
    """
    capabilities = SNIPPETS_PLUGIN_SCHEMA.capabilities
    assert capabilities is not None
    assert {
        name: getattr(capabilities, name) for name in type(capabilities).model_fields
    } == dict.fromkeys(type(capabilities).model_fields, False)


def _execution_section(schema):
    return next(section for section in schema.forms if section.title == "Execution")


def _script_preview_section(schema):
    return next(
        section for section in schema.forms if section.title == "Script preview"
    )


@pytest.mark.asyncio
async def test_per_snippet_schema_includes_host_and_preview(create_snippet):
    """Per-snippet schema keeps host in Execution; preview is a separate collapsible section using the query API."""
    snippet = await create_snippet("hello.sh", approved=True)

    schema = build_snippet_schema(snippet)

    execution = _execution_section(schema)
    field_types = {type(field) for field in execution.fields}
    assert HostField in field_types
    assert ScriptPreviewField not in field_types
    preview_section = _script_preview_section(schema)
    assert preview_section.collapsible is True
    assert preview_section.collapsed_by_default is True
    assert preview_section.render_after_submit is True
    preview = next(
        f for f in preview_section.fields if isinstance(f, ScriptPreviewField)
    )
    assert preview.label == "Snippet file"
    assert preview.endpoint_url == (
        "/apps/snippets/snippet/preview?"
        + urlencode({"snippet_filename": snippet.filename})
    )


@pytest.mark.asyncio
async def test_per_snippet_schema_url_encodes_nested_filenames(create_snippet):
    """Nested relative keys encode ``/`` as ``%2F`` in ``snippet_filename`` only — the preview path segment stays constant."""
    snippet = await create_snippet("diag/slow-query.sh", approved=True)

    schema = build_snippet_schema(snippet)

    preview_section = _script_preview_section(schema)
    preview = next(
        f for f in preview_section.fields if isinstance(f, ScriptPreviewField)
    )
    assert preview.endpoint_url == (
        "/apps/snippets/snippet/preview?"
        + urlencode({"snippet_filename": snippet.filename})
    )
    path, _, _query = preview.endpoint_url.partition("?")
    assert path == "/apps/snippets/snippet/preview"
    assert "%2F" not in path
    assert "diag" not in path
    assert "slow-query" not in path


@pytest.mark.asyncio
async def test_per_snippet_schema_omits_sudo_field_when_sudo_never(create_snippet):
    """A NEVER-sudo snippet's execution section excludes the sudo toggle."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("sudo", None)
    snippet.meta = {**snippet.meta, "sudo": "never"}
    snippet.__dict__.pop("sudo", None)

    schema = build_snippet_schema(snippet)

    section = _execution_section(schema)
    sudo_fields = [f for f in section.fields if f.name == "sudo"]
    assert sudo_fields == []


@pytest.mark.asyncio
async def test_per_snippet_schema_includes_optional_sudo_toggle(create_snippet):
    """A snippet with optional sudo includes a default-False sudo BoolField."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("sudo", None)
    snippet.meta = {**snippet.meta, "sudo": "optional"}
    snippet.__dict__.pop("sudo", None)

    schema = build_snippet_schema(snippet)

    section = _execution_section(schema)
    sudo_field = next(f for f in section.fields if f.name == "sudo")
    assert isinstance(sudo_field, BoolField)
    assert sudo_field.default is False


@pytest.mark.asyncio
async def test_per_snippet_schema_includes_extra_args_field_when_allowed(
    create_snippet,
):
    """Add the Extra Args field to the Execution section when the snippet allows it."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.meta = {**snippet.meta, "allow_extra_args": True}
    snippet.__dict__.pop("allow_extra_args", None)

    schema = build_snippet_schema(snippet)

    section = _execution_section(schema)
    extra_args_field = next(f for f in section.fields if f.name == "extra_args")
    assert isinstance(extra_args_field, StringField)
    assert extra_args_field.label == "Extra Args"
    assert extra_args_field.placeholder == "e.g. --verbose"
    assert (
        extra_args_field.description
        == "Any extra args to pass to the snippet execution command"
    )


@pytest.mark.asyncio
async def test_per_snippet_schema_omits_extra_args_field_by_default(create_snippet):
    """Omit the Extra Args field for a snippet that never opts into ``allow_extra_args``."""
    snippet = await create_snippet("hello.sh", approved=True)

    schema = build_snippet_schema(snippet)

    all_fields = [field for section in schema.forms for field in section.fields]
    assert not any(field.name == "extra_args" for field in all_fields)


@pytest.mark.parametrize(
    ("parameter", "expected"),
    [
        ({"name": "password", "type": "str"}, True),
        ({"name": "auth-blob", "type": "str", "sensitive": True}, True),
        ({"name": "dest", "type": "str"}, False),
    ],
    ids=["credential-name", "sensitive-flag", "neither"],
)
def test_string_parameter_is_sensitive_exactly_when_the_masker_masks_it(
    parameter, expected
):
    """Mark a string field sensitive by the classification history masking uses.

    :param parameter: The frontmatter parameter definition.
    :param expected: Whether the field must be marked sensitive.
    """
    declared = SnippetMetaParameter.model_validate(parameter)

    field = field_for(declared)

    assert isinstance(field, StringField)
    assert field.sensitive is expected
    assert field.sensitive is is_credential_parameter(
        declared.name, sensitive=declared.sensitive
    )


@pytest.mark.asyncio
async def test_extra_args_field_is_not_sensitive(create_snippet):
    """Leave the free-form Extra Args field readable as typed."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.meta = {**snippet.meta, "allow_extra_args": True}
    snippet.__dict__.pop("allow_extra_args", None)

    schema = build_snippet_schema(snippet)

    extra_args_field = next(
        f for f in _execution_section(schema).fields if f.name == "extra_args"
    )
    assert extra_args_field.sensitive is False


#: Every execution field this builder can synthesize, and the widget rendering it.
#: Snippets is the maximal builder, so these are the whole reserved set.
_SYNTHESIZED_FIELD_TYPES = {
    EXECUTOR_HOST_FIELD_NAME: HostField,
    SUDO_FIELD_NAME: BoolField,
    SCRIPT_PREVIEW_FIELD_NAME: ScriptPreviewField,
    EXTRA_ARGS_FIELD_NAME: StringField,
}


def _reset_cached_meta(snippet: Snippet, **meta: Any) -> None:
    """Re-apply snippet meta and drop every cached property derived from it.

    :param snippet: The already-created snippet whose meta is being replaced.
    :param meta: Frontmatter keys merged over the snippet's existing meta.
    """
    snippet.meta = {**snippet.meta, **meta}
    drop_cached_reads(snippet)


class TestReservedParameterNames:
    """Cover frontmatter parameters colliding with synthesized execution fields."""

    @pytest.mark.parametrize("reserved_name", sorted(RESERVED_EXECUTION_FIELD_NAMES))
    @pytest.mark.asyncio
    async def test_per_snippet_schema_drops_reserved_named_parameter(
        self,
        create_snippet: Callable[..., Awaitable[Snippet]],
        reserved_name: str,
    ) -> None:
        """Drop a frontmatter parameter reserved for a synthesized execution field.

        Regression test: previously this reached ``build_snippet_schema`` and
        raised an opaque ``duplicate field name(s)`` error whenever the snippet
        configuration made the builder synthesize the colliding field, or silently
        double-bound submitted values in the execution model when it did not. The
        reserved-name parameter is now caught earlier, at parameter parse time (see
        ``validated_parameters`` on the snippet), so it never reaches either path --
        ``build_snippet_schema`` just omits it like any other invalid parameter.

        Both ``allow_extra_args`` and ``sudo`` are enabled so that every one of the
        four reserved names is actually synthesized, making the collision reachable.
        """
        snippet = await create_snippet("hello.sh", approved=True)
        _reset_cached_meta(
            snippet,
            allow_extra_args=True,
            sudo=SnippetSudoOption.OPTIONAL.value,
            parameters=[{"name": reserved_name, "type": "str"}],
        )

        assert any("reserved" in error for error in snippet.validated_parameters.errors)

        schema = build_snippet_schema(snippet)

        assert_only_synthesized_fields(schema, _SYNTHESIZED_FIELD_TYPES)

    @pytest.mark.asyncio
    async def test_per_snippet_schema_builds_when_invalid_parameters_are_ignored(
        self,
        create_snippet: Callable[..., Awaitable[Snippet]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Build a collision-free form even when invalid parameters are ignored.

        ``IGNORE_INVALID_PARAMETERS`` relaxes ``can_execute`` only; the reserved
        parameter is still dropped at parse time, so the form the operator opted
        into still carries exactly one ``executor_host`` field, still the
        synthesized host widget and not the author's string, rather than the
        duplicate-field error this setting would otherwise expose them to.
        """
        monkeypatch.setattr(snippets_settings.META, "IGNORE_INVALID_PARAMETERS", True)
        snippet = await create_snippet("hello.sh", approved=True)
        _reset_cached_meta(
            snippet, parameters=[{"name": "executor_host", "type": "str"}]
        )

        assert snippet.can_execute is True

        schema = build_snippet_schema(snippet)

        assert form_field_names(schema).count(EXECUTOR_HOST_FIELD_NAME) == 1
        assert form_field_types(schema)[EXECUTOR_HOST_FIELD_NAME] is HostField

    @pytest.mark.asyncio
    async def test_every_synthesized_field_name_is_reserved(
        self, create_snippet: Callable[..., Awaitable[Snippet]]
    ) -> None:
        """Keep every field this builder synthesizes covered by the reserved set.

        A parameterless snippet yields a schema whose fields are all synthesized,
        so a synthesized field added later without being reserved fails here rather
        than only when an author happens to pick its name.
        """
        snippet = await create_snippet("hello.sh", approved=True)
        _reset_cached_meta(
            snippet, allow_extra_args=True, sudo=SnippetSudoOption.OPTIONAL.value
        )

        schema = build_snippet_schema(snippet)

        assert set(form_field_names(schema)) == RESERVED_EXECUTION_FIELD_NAMES
        assert form_field_types(schema) == _SYNTHESIZED_FIELD_TYPES


@pytest.mark.asyncio
async def test_per_snippet_schema_maps_str_parameter_to_string_field(create_snippet):
    """STR parameters surface as StringField with their declared constraints."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("validated_parameters", None)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {
                "name": "username",
                "type": "str",
                "label": "Username",
                "min_length": 3,
                "max_length": 32,
                "pattern": "^[a-z]+$",
            },
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    parameters_section = next(s for s in schema.forms if s.title == "Parameters")
    field = parameters_section.fields[0]
    assert isinstance(field, StringField)
    assert field.name == "username"
    assert field.label == "Username"
    expected_min_length = 3
    expected_max_length = 32
    assert field.min_length == expected_min_length
    assert field.max_length == expected_max_length
    assert field.pattern == "^[a-z]+$"


@pytest.mark.asyncio
async def test_per_snippet_schema_maps_int_parameter_with_gt_to_ge_plus_one(
    create_snippet,
):
    """INT parameters with gt/lt translate to ge/le inclusive bounds."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("validated_parameters", None)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {
                "name": "count",
                "type": "int",
                "label": "Count",
                "gt": 0,
                "lt": 101,
                "step": 5,
            },
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    parameters_section = next(s for s in schema.forms if s.title == "Parameters")
    field = parameters_section.fields[0]
    assert isinstance(field, IntegerField)
    expected_le = 100
    expected_step = 5
    assert field.ge == 1
    assert field.le == expected_le
    assert field.step == expected_step


@pytest.mark.asyncio
async def test_per_snippet_schema_maps_choices_to_choice_field(create_snippet):
    """A parameter with declared choices becomes a ChoiceField regardless of py_type."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("validated_parameters", None)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {
                "name": "level",
                "type": "str",
                "label": "Level",
                "choices": [
                    {"value": "info", "label": "Info"},
                    {"value": "debug"},
                ],
            },
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    parameters_section = next(s for s in schema.forms if s.title == "Parameters")
    field = parameters_section.fields[0]
    assert isinstance(field, ChoiceField)
    values = [choice.value for choice in field.choices]
    assert values == ["info", "debug"]
    labels = [choice.label for choice in field.choices]
    assert labels == ["Info", "debug"]


class TestVisibilityGates:
    """Test that field_for lowers visibility conditions onto forbidden gates.

    The React renderer hides the field and drops its value; the gates are also
    enforced server-side on the execute paths (see ``evaluate_snippet_gates``
    and the execute-path tests), which reject a directly-submitted hidden value.
    """

    def test_no_condition_leaves_forbidden_none(self):
        """A parameter without a visibility condition carries no forbidden gate."""
        field = field_for(SnippetMetaParameter(name="start", type="str"))
        assert field.forbidden is None

    def test_visible_when_not_truthy_lowers_to_truthy_gate(self):
        """visible_when_not (truthiness) forbids the field when the ref is truthy."""
        field = field_for(
            SnippetMetaParameter(name="start", type="str", visible_when_not="list")
        )
        assert field.forbidden is not None
        assert len(field.forbidden) == 1
        assert field.forbidden[0].when.to_dict() == {"truthy": "list"}

    def test_visible_when_truthy_lowers_to_negated_gate(self):
        """visible_when (truthiness) forbids the field when the ref is NOT truthy."""
        field = field_for(
            SnippetMetaParameter(name="start", type="str", visible_when="list")
        )
        assert field.forbidden is not None
        assert len(field.forbidden) == 1
        assert field.forbidden[0].when.to_dict() == {"not": {"truthy": "list"}}

    def test_visible_when_not_equals_lowers_to_equals_gate(self):
        """visible_when_not with equals forbids the field on an equality match."""
        field = field_for(
            SnippetMetaParameter(
                name="region",
                type="str",
                visible_when_not={"parameter": "mode", "equals": "advanced"},
            )
        )
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {"equals": {"mode": "advanced"}}

    def test_condition_on_choice_field(self):
        """A choice-typed parameter still receives the forbidden gate."""
        field = field_for(
            SnippetMetaParameter(
                name="region",
                type="str",
                choices=["us", "eu"],
                visible_when_not="list",
            )
        )
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {"truthy": "list"}


class TestGateLowering:
    """field_for lowers bounded requires/forbidden gates onto the gate lists.

    The wire shapes (``{"truthy": ...}`` / ``{"equals": {...}}`` / ``{"not":
    {...}}``) match those the marker DSL and visibility path emit — parity is the
    point.
    """

    def test_requires_when_truthy_lowers_to_requires_gate(self):
        """requires_when (truthiness) requires the field when the ref is truthy."""
        field = field_for(
            SnippetMetaParameter(name="reason", type="str", requires_when="mode")
        )
        assert field.requires is not None
        assert len(field.requires) == 1
        assert field.requires[0].when.to_dict() == {"truthy": "mode"}
        assert field.forbidden is None

    def test_requires_when_not_lowers_to_negated_gate(self):
        """requires_when_not requires the field when the ref is NOT truthy."""
        field = field_for(
            SnippetMetaParameter(name="reason", type="str", requires_when_not="mode")
        )
        assert field.requires is not None
        assert field.requires[0].when.to_dict() == {"not": {"truthy": "mode"}}

    def test_requires_when_equals_lowers_to_equals_gate(self):
        """requires_when with equals requires the field on an equality match."""
        field = field_for(
            SnippetMetaParameter(
                name="reason",
                type="str",
                requires_when={"parameter": "mode", "equals": "write"},
            )
        )
        assert field.requires is not None
        assert field.requires[0].when.to_dict() == {"equals": {"mode": "write"}}

    def test_forbidden_when_lowers_to_forbidden_gate(self):
        """forbidden_when (truthiness) forbids the field when the ref is truthy."""
        field = field_for(
            SnippetMetaParameter(name="reason", type="str", forbidden_when="mode")
        )
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {"truthy": "mode"}
        assert field.requires is None

    def test_forbidden_when_not_lowers_to_negated_gate(self):
        """forbidden_when_not forbids the field when the ref is NOT truthy."""
        field = field_for(
            SnippetMetaParameter(name="reason", type="str", forbidden_when_not="mode")
        )
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {"not": {"truthy": "mode"}}

    def test_requires_and_forbidden_both_lowered(self):
        """A parameter can carry both a requires and a forbidden gate."""
        field = field_for(
            SnippetMetaParameter(
                name="reason",
                type="str",
                requires_when="write_mode",
                forbidden_when="read_mode",
            )
        )
        assert field.requires is not None
        assert field.requires[0].when.to_dict() == {"truthy": "write_mode"}
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {"truthy": "read_mode"}

    def test_requires_when_not_equals_lowers_to_negated_equals_gate(self):
        """The negated-equals branch wraps an equals predicate in ``not``."""
        field = field_for(
            SnippetMetaParameter(
                name="reason",
                type="str",
                requires_when_not={"parameter": "mode", "equals": "write"},
            )
        )
        assert field.requires is not None
        assert field.requires[0].when.to_dict() == {
            "not": {"equals": {"mode": "write"}}
        }

    def test_forbidden_when_not_equals_lowers_to_negated_equals_gate(self):
        """The forbidden negated-equals branch mirrors the requires one."""
        field = field_for(
            SnippetMetaParameter(
                name="note",
                type="str",
                forbidden_when_not={"parameter": "mode", "equals": "read"},
            )
        )
        assert field.forbidden is not None
        assert field.forbidden[0].when.to_dict() == {
            "not": {"equals": {"mode": "read"}}
        }


@pytest.mark.asyncio
async def test_build_snippet_schema_with_gated_field_validates(create_snippet):
    """Build a valid AppSchema from a gated, identifier-safe parameter.

    Regression guard: ``build_snippet_schema`` constructs an ``AppSchema``,
    whose validator folds each gated field's own name into the gate's reference
    set and rejects hyphenated names. This exercises that full construction so a
    gate that produces an invalid schema fails here rather than as a 500 at
    request time.
    """
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {"name": "list", "type": "bool", "label": "List services"},
            {
                "name": "start",
                "type": "str",
                "label": "Start",
                "visible_when_not": "list",
            },
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    parameters_section = next(s for s in schema.forms if s.title == "Parameters")
    start_field = next(f for f in parameters_section.fields if f.name == "start")
    assert start_field.forbidden is not None
    assert start_field.forbidden[0].when.to_dict() == {"truthy": "list"}


@pytest.mark.asyncio
async def test_per_snippet_schema_omits_hidden_parameter(create_snippet):
    """A ``hidden`` parameter is excluded from the generic snippet schema.

    ``hidden`` is a generic snippet primitive, so the schema-driven snippets form
    must omit it just as ``_to_form`` and the Dipper schema builder do — while a
    sibling non-hidden parameter still renders.
    """
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("validated_parameters", None)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {"name": "pmmserver", "type": "str", "label": "PMM server"},
            {"name": "apikey", "type": "str", "label": "API key", "hidden": True},
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    field_names = {field.name for section in schema.forms for field in section.fields}
    assert "pmmserver" in field_names
    assert "apikey" not in field_names


def _snippet_with_params(params: list[dict]) -> Snippet:
    """Return an unpersisted snippet carrying ``params`` as its meta parameters."""
    snippet = Snippet(filename="gated.sh", size=20, md5_digest="a" * 32)
    snippet.meta = {**snippet.meta, "parameters": params}
    return snippet


def _exec_args(snippet: Snippet, wire: dict):
    """Validate ``wire`` (keyed by parameter wire/alias names) into exec args."""
    return snippet.get_execution_model().model_validate(
        {EXECUTOR_HOSTS_INPUT_NAME: "host1", **wire}
    )


class TestEvaluateSnippetGates:
    """Direct coverage of the server-side gate evaluator.

    ``evaluate_snippet_gates`` is the security backstop: it rejects a value
    submitted directly for a parameter the client would have hidden. Generated
    execution models name fields with opaque identifiers and expose each
    parameter only through its wire alias, so evaluation MUST run against the
    ``by_alias`` view — these tests pin that and the present/absent boundaries.
    """

    _LIST_START = [
        {"name": "list", "type": "bool", "label": "List"},
        {"name": "start", "type": "str", "label": "Start", "visible_when_not": "list"},
    ]

    def test_gateless_snippet_returns_empty(self) -> None:
        """A snippet with no visibility gate never reports a failure."""
        snippet = _snippet_with_params(
            [{"name": "name", "type": "str", "label": "Name"}]
        )
        assert evaluate_snippet_gates(snippet, _exec_args(snippet, {"name": "x"})) == []

    def test_evaluation_runs_against_alias_view(self) -> None:
        """The gate resolves the wire name even though the python attr differs.

        Regression guard for the ``model_dump(by_alias=True)`` requirement: the
        plain dump is keyed by generated identifiers, so a non-alias evaluation
        would silently never fire — a gate bypass.
        """
        snippet = _snippet_with_params(self._LIST_START)
        args = _exec_args(snippet, {"list": True, "start": "2020"})

        assert "start" not in args.model_dump()  # python attr is opaque
        assert "start" in args.model_dump(by_alias=True)
        assert evaluate_snippet_gates(snippet, args)  # gate fires

    def test_truthy_gate_not_fired_when_trigger_absent(self) -> None:
        """With the trigger falsy, the gated value is allowed."""
        snippet = _snippet_with_params(self._LIST_START)
        args = _exec_args(snippet, {"start": "2020"})
        assert evaluate_snippet_gates(snippet, args) == []

    def test_visible_when_negated_gate_fires(self) -> None:
        """``visible_when`` forbids the field while the trigger is NOT truthy."""
        snippet = _snippet_with_params(
            [
                {"name": "list", "type": "bool", "label": "List"},
                {
                    "name": "start",
                    "type": "str",
                    "label": "Start",
                    "visible_when": "list",
                },
            ]
        )
        # ``list`` falsy -> field is hidden -> a submitted ``start`` is rejected.
        args = _exec_args(snippet, {"start": "2020"})
        assert evaluate_snippet_gates(snippet, args)

    def test_equals_gate_fires(self) -> None:
        """An equals condition rejects the gated value on an equality match."""
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "str", "label": "Mode"},
                {
                    "name": "region",
                    "type": "str",
                    "label": "Region",
                    "visible_when_not": {"parameter": "mode", "equals": "advanced"},
                },
            ]
        )
        args = _exec_args(snippet, {"mode": "advanced", "region": "eu"})
        assert evaluate_snippet_gates(snippet, args)

    def test_multiple_gates_fire_yield_multiple_messages(self) -> None:
        """Every fired gate contributes a message."""
        snippet = _snippet_with_params(
            [
                {"name": "list", "type": "bool", "label": "List"},
                {
                    "name": "start",
                    "type": "str",
                    "label": "Start",
                    "visible_when_not": "list",
                },
                {
                    "name": "end",
                    "type": "str",
                    "label": "End",
                    "visible_when_not": "list",
                },
            ]
        )
        args = _exec_args(snippet, {"list": True, "start": "a", "end": "b"})
        expected_failures = 2
        assert len(evaluate_snippet_gates(snippet, args)) == expected_failures

    def test_gated_bool_false_is_allowed(self) -> None:
        """A gated bool submitted ``False`` is absent, so the gate does not reject.

        ``True`` for the same field IS rejected — locking the ``value_is_present``
        ``False``-is-absent convention end-to-end through the evaluator.
        """
        snippet = _snippet_with_params(
            [
                {"name": "list", "type": "bool", "label": "List"},
                {
                    "name": "flag",
                    "type": "bool",
                    "label": "Flag",
                    "visible_when_not": "list",
                },
            ]
        )
        allowed = _exec_args(snippet, {"list": True, "flag": False})
        rejected = _exec_args(snippet, {"list": True, "flag": True})

        assert evaluate_snippet_gates(snippet, allowed) == []
        assert evaluate_snippet_gates(snippet, rejected)


class TestEvaluateGateEnforcement:
    """Server-side enforcement of author-declared requires/forbidden gates.

    The ``requires`` direction is the security-critical new behaviour: a
    parameter omitted while its gate fires must be rejected server-side, not just
    hidden client-side. These pin both directions and the present/absent
    convention, mirroring :class:`TestEvaluateSnippetGates`.
    """

    _MODE_REASON_REQUIRES = [
        {"name": "mode", "type": "str", "label": "Mode"},
        {
            "name": "reason",
            "type": "str",
            "label": "Reason",
            "requires_when": {"parameter": "mode", "equals": "write"},
        },
    ]

    def test_requires_gate_fires_when_field_absent(self) -> None:
        """A requires gate rejects a submission omitting the required field."""
        snippet = _snippet_with_params(self._MODE_REASON_REQUIRES)
        args = _exec_args(snippet, {"mode": "write"})  # reason omitted
        assert evaluate_snippet_gates(snippet, args)

    def test_requires_gate_passes_when_field_present(self) -> None:
        """A requires gate is satisfied when the required field is supplied."""
        snippet = _snippet_with_params(self._MODE_REASON_REQUIRES)
        args = _exec_args(snippet, {"mode": "write", "reason": "cleanup"})
        assert evaluate_snippet_gates(snippet, args) == []

    def test_requires_gate_not_fired_when_predicate_false(self) -> None:
        """With the trigger not matching, the field is not required."""
        snippet = _snippet_with_params(self._MODE_REASON_REQUIRES)
        args = _exec_args(snippet, {"mode": "read"})  # reason not required
        assert evaluate_snippet_gates(snippet, args) == []

    def test_requires_when_not_fires_when_trigger_falsy(self) -> None:
        """requires_when_not requires the field while the trigger is NOT truthy."""
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "bool", "label": "Mode"},
                {
                    "name": "reason",
                    "type": "str",
                    "label": "Reason",
                    "requires_when_not": "mode",
                },
            ]
        )
        args = _exec_args(snippet, {})  # mode falsy -> reason required -> absent
        assert evaluate_snippet_gates(snippet, args)

    def test_forbidden_when_gate_fires(self) -> None:
        """A forbidden_when gate rejects a value on an equality match."""
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "str", "label": "Mode"},
                {
                    "name": "note",
                    "type": "str",
                    "label": "Note",
                    "forbidden_when": {"parameter": "mode", "equals": "read"},
                },
            ]
        )
        args = _exec_args(snippet, {"mode": "read", "note": "x"})
        assert evaluate_snippet_gates(snippet, args)

    def test_forbidden_gate_passes_when_field_absent(self) -> None:
        """A forbidden gate whose predicate fires still passes if no value given.

        Forbidden rejects only a *present* value; omitting the field is always
        allowed. Pins the pass direction the requires triad has but forbidden
        lacked.
        """
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "str", "label": "Mode"},
                {
                    "name": "note",
                    "type": "str",
                    "label": "Note",
                    "forbidden_when": {"parameter": "mode", "equals": "read"},
                },
            ]
        )
        args = _exec_args(snippet, {"mode": "read"})  # note omitted
        assert evaluate_snippet_gates(snippet, args) == []

    def test_forbidden_when_not_fires_when_trigger_truthy(self) -> None:
        """forbidden_when_not forbids the value while the trigger IS truthy."""
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "bool", "label": "Mode"},
                {
                    "name": "note",
                    "type": "str",
                    "label": "Note",
                    "forbidden_when_not": "mode",
                },
            ]
        )
        # ``mode`` truthy -> NOT(truthy) is false -> forbidden does NOT fire.
        allowed = _exec_args(snippet, {"mode": True, "note": "x"})
        # ``mode`` falsy -> NOT(truthy) is true -> a submitted ``note`` is rejected.
        rejected = _exec_args(snippet, {"note": "x"})

        assert evaluate_snippet_gates(snippet, allowed) == []
        assert evaluate_snippet_gates(snippet, rejected)

    def test_requires_gate_bool_false_is_absent(self) -> None:
        """A gated bool submitted ``False`` counts as absent for a requires gate.

        ``True`` satisfies presence — locking the ``value_is_present``
        convention through the requires evaluator too.
        """
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "bool", "label": "Mode"},
                {
                    "name": "flag",
                    "type": "bool",
                    "label": "Flag",
                    "requires_when": "mode",
                },
            ]
        )
        fires = _exec_args(snippet, {"mode": True, "flag": False})
        passes = _exec_args(snippet, {"mode": True, "flag": True})

        assert evaluate_snippet_gates(snippet, fires)
        assert evaluate_snippet_gates(snippet, passes) == []

    def test_requires_and_forbidden_yield_multiple_messages(self) -> None:
        """A fired requires gate and a fired forbidden gate each report."""
        snippet = _snippet_with_params(
            [
                {"name": "mode", "type": "str", "label": "Mode"},
                {
                    "name": "reason",
                    "type": "str",
                    "label": "Reason",
                    "requires_when": {"parameter": "mode", "equals": "write"},
                },
                {
                    "name": "note",
                    "type": "str",
                    "label": "Note",
                    "forbidden_when": {"parameter": "mode", "equals": "write"},
                },
            ]
        )
        # mode=write -> reason required (absent) + note forbidden (present).
        args = _exec_args(snippet, {"mode": "write", "note": "x"})
        expected_failures = 2
        assert len(evaluate_snippet_gates(snippet, args)) == expected_failures


def test_field_for_maps_datetime_parameter_to_datetime_field():
    """Verify DATETIME parameters map directly to DateTimeField via field_for."""
    param = SnippetMetaParameter(
        name="start",
        type="datetime",
        label="Start time (UTC)",
        description="Starting timestamp for graph data.",
    )
    field = field_for(param)
    assert isinstance(field, DateTimeField)
    assert field.name == "start"
    assert field.label == "Start time (UTC)"
    assert field.description == "Starting timestamp for graph data."


@pytest.mark.asyncio
async def test_per_snippet_schema_maps_datetime_parameter_to_datetime_field(
    create_snippet,
):
    """Verify DATETIME parameters surface as DateTimeField in the per-snippet schema."""
    snippet = await create_snippet("hello.sh", approved=True)
    snippet.__dict__.pop("validated_parameters", None)
    snippet.meta = {
        **snippet.meta,
        "parameters": [
            {
                "name": "start",
                "type": "datetime",
                "label": "Start time (UTC)",
                "description": "Starting timestamp for graph data.",
            },
        ],
    }
    snippet.__dict__.pop("validated_parameters", None)

    schema = build_snippet_schema(snippet)

    parameters_section = next(s for s in schema.forms if s.title == "Parameters")
    field = parameters_section.fields[0]
    assert isinstance(field, DateTimeField)
    assert field.name == "start"
    assert field.label == "Start time (UTC)"


@pytest.mark.asyncio
async def test_pmm_mysql_payload_start_end_map_to_datetime_field():
    """Verify PMM MySQL collector start/end params declare datetime and map to DateTimeField."""
    script = await DipperScript.from_path("pcs-collect-pmm-mysql.py", update_meta=True)

    assert script.validated_parameters.errors == []

    for name in ("start", "end"):
        param = next(
            p for p in script.validated_parameters.parameters if p.name == name
        )
        assert param.py_type is SnippetMetaParameterType.DATETIME
        assert isinstance(field_for(param), DateTimeField)


@pytest.mark.asyncio
async def test_widened_helpers_accept_non_snippet_basesnippet():
    """Accept a disk-backed ``BaseSnippet`` subclass, not only the DB ``Snippet``."""
    script = await DipperScript.from_path("pcs-collect-pmm-mysql.py", update_meta=True)

    schema = build_snippet_schema(script)
    assert any(section.title == "Execution" for section in schema.forms)

    execution_args = script.get_execution_model().model_construct(executor_host="host1")
    assert isinstance(evaluate_snippet_gates(script, execution_args), list)


@pytest.mark.asyncio
async def test_per_snippet_schema_names_the_record_a_run(create_snippet):
    """Name the record a run of the script, not the script the title names."""
    snippet = await create_snippet("hello.sh", approved=True)

    schema = build_snippet_schema(snippet)

    assert schema.display_name == snippet.title
    assert schema.item_display_name == "run"
    assert schema.item_display_name_plural == "runs"


def test_snippets_plugin_schema_names_the_record_a_script():
    """Name the app-level record a script, distinct from the app's own title."""
    assert SNIPPETS_PLUGIN_SCHEMA.item_display_name == "script"
    assert SNIPPETS_PLUGIN_SCHEMA.item_display_name_plural == "scripts"
