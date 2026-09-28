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

"""Define the plugin schema DSL for schema-driven plugin form and list views."""

__all__ = [
    "AnyField",
    "AppDeploymentCapabilities",
    "AppEntitySchema",
    "AppSchema",
    "BaseField",
    "BoolField",
    "Capabilities",
    "ChainedPredecessor",
    "Choice",
    "ChoiceField",
    "Column",
    "ColumnFormat",
    "DateTimeField",
    "DerivedTask",
    "DetailField",
    "DetailHighlightLanguage",
    "DetailSection",
    "DetailView",
    "FileField",
    "FloatField",
    "FormSection",
    "HostField",
    "IntegerField",
    "ListView",
    "MultiChoiceField",
    "MultiHostField",
    "MultiSchemaField",
    "MultiServiceField",
    "MultiTableField",
    "OneOfBranch",
    "OneOfGroup",
    "RelatedApp",
    "RemoteChoiceField",
    "SchemaBaseModel",
    "SchemaField",
    "ScriptPreviewField",
    "ServiceField",
    "StringField",
    "TableField",
    "TaskStatusDescriptor",
    "TextAreaField",
    "YamlField",
    "declared_field_names_from_forms",
    "iter_section_fields",
    "pluralize_item_display_name",
]

from collections import Counter
from collections.abc import Iterator, Mapping
from enum import auto, StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    StringConstraints,
)

from app.core.utils.fields import (
    ArbitraryMapping,
    EnumFieldMixin,
    NonEmptyStr,
    StrippedNonEmptyStr,
)
from app.extensions.apps.framework.rules import (
    CardinalityRule,
    FailRule,
    FieldGate,
)
from app.extensions.apps.labels import EXECUTION_HOST_LABEL
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum

# Dots are permitted so nested one-of branch fields can use paths such as
# ``source.source_db_id`` (see :class:`OneOfGroup`).
_FIELD_NAME_PATTERN = r"^[A-Za-z_](?:[\w.-]*\w)?$"


class SchemaBaseModel(BaseModel):
    """Define the base model for every model in the plugin schema DSL.

    :cvar model_config: Serialises to snake_case JSON (no alias translation),
        forbids unknown keys, and accepts arbitrary types so rule envelopes
        carrying :class:`Predicate` instances validate.
    :vartype model_config: ConfigDict
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )


class Choice(SchemaBaseModel):
    """Represent one option in a choice or multi-choice field.

    :param label: The human-readable label displayed for the choice.
    :type label: NonEmptyStr
    :param value: The value submitted when the choice is selected.
    :type value: NonEmptyStr
    :param disabled: Whether the option is rendered non-selectable. Optional,
        defaulting to ``None`` (selectable). Typed ``bool | None`` so the
        discovery endpoint's ``exclude_none`` posture drops it from the wire
        until a plugin opts in, keeping the addition byte-compatible with
        existing schemas. UI hint only; enforcing rejection of a disabled
        value is the consuming app's responsibility.
    :type disabled: bool | None
    :param disabled_reason: Optional explanatory text surfaced (for example,
        in a tooltip) when the option is disabled. Defaults to ``None`` and
        may only be set when ``disabled`` is ``True``.
    :type disabled_reason: NonEmptyStr | None
    """

    label: NonEmptyStr
    value: NonEmptyStr
    disabled: bool | None = None
    disabled_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_disabled_reason_implies_disabled(self) -> Self:
        """Ensure ``disabled_reason`` is only set on a disabled option.

        ``disabled_reason`` is tooltip text shown when the option is
        non-selectable, and the UI helpers ignore it unless ``disabled`` is
        true. Allowing a reason on a still-selectable option would emit an
        inconsistent wire shape, so reject it here.

        :return: The validated choice instance.
        :rtype: Choice
        :raises ValueError: If ``disabled_reason`` is set while ``disabled``
            is not ``True``.
        """
        if self.disabled_reason is not None and not self.disabled:
            raise ValueError("disabled_reason requires disabled=True")
        return self


class ColumnFormat(EnumFieldMixin, StrEnum):
    """Enumerate the supported list-view column formatting hints.

    :cvar TEXT: Render the column value as plain text.
    :vartype TEXT: str
    :cvar CHIP: Render the column value as a Material UI chip.
    :vartype CHIP: str
    :cvar STATUS: Render the column value as a colour-coded status badge.
    :vartype STATUS: str
    :cvar DATE: Render the column value as an absolute date.
    :vartype DATE: str
    :cvar RELATIVE: Render the column value as a relative time (for example,
        "2 hours ago").
    :vartype RELATIVE: str
    :cvar CODE: Render the column value in a monospaced code font.
    :vartype CODE: str
    :cvar ACTIONS: Row actions (for example delete); not bound to row data.
        Use with a synthetic column key such as ``_actions``.
    :vartype ACTIONS: str
    :cvar SCHEDULE: Render a generic schedule cell for the row's task. The
        frontend joins the row to its periodic task by name and shows the next
        run (relative, with the absolute timestamp on hover) plus a periodicity
        popover, or a muted "Not scheduled" chip when the task has no schedule.
        Not bound to the column's own cell value.
    :vartype SCHEDULE: str
    """

    TEXT = auto()
    CHIP = auto()
    STATUS = auto()
    DATE = auto()
    RELATIVE = auto()
    CODE = auto()
    ACTIONS = auto()
    SCHEDULE = auto()


NON_ROW_BOUND_FORMATS: frozenset[ColumnFormat] = frozenset(
    {ColumnFormat.ACTIONS, ColumnFormat.SCHEDULE}
)
"""Column formats whose cells are not derived from the column's own row value.

Their keys are therefore exempt from the list-view column validation gate in
:meth:`~app.extensions.apps.framework.apps.TaskExecutionApp._validate_view_columns`, so such
a column may use a synthetic key absent from the serialized row.
"""


class DetailHighlightLanguage(EnumFieldMixin, StrEnum):
    """Enumerate supported syntax highlighters for detail fields."""

    SQL = auto()
    JSON = auto()
    BASH = auto()
    YAML = auto()


class HelpPlacement(StrEnum):
    """Say where a field's ``description`` is shown, overriding the default.

    The renderer otherwise places help by length — a description that fits
    roughly one line sits under the input, a longer one goes behind a help icon
    beside the label. Setting this is for the cases where that reads wrong: a
    terse note that is still secondary, or a long one someone needs in front of
    them while they type.
    """

    TOOLTIP = "tooltip"
    INLINE = "inline"


class BaseField(SchemaBaseModel):
    """Define the abstract base for every concrete field in the plugin schema DSL.

    Concrete subclasses add a ``field_type`` discriminator and any
    type-specific constraints. Cross-field validation (for example,
    ``min_length <= max_length`` or ``depends_on`` target existence) is
    deliberately not enforced here — the DSL is declarative and plugins
    validate their own schema contents at the plugin level.

    :param name: The form-state key for the field; must match Python
        identifier rules, optionally with internal hyphens.
    :param label: The human-readable label displayed next to the field.
    :param required: Whether the field must be provided when the form is
        submitted. Defaults to ``False``.
    :param description: Optional helper text rendered beneath the field.
        Defaults to ``None``.
    :param destructive: Optional consequence text marking the field as one
        whose enabled or set state irreversibly destroys something the operator
        cannot get back — user data, or operator-managed state such as a
        hand-tuned configuration file; presence is the mark and the value is
        what a confirmation displays. Typed
        optional so a route serialising with ``exclude_none`` drops it from the
        wire until a field opts in, which is what keeps the discovery schemas
        byte-identical; a route without that posture publishes it as an
        explicit null alongside the other unset keys. Carries no validation
        semantics — the API accepts exactly the same bodies either way.
    :param default: Optional default value pre-filled when the form is
        rendered. Typed permissively because consumer defaults may be
        scalars, lists, or dicts depending on the field type. Defaults to
        ``None``.
    :param requires: Optional list of binary self-cardinality gates: when
        any gate's ``when`` predicate matches, the field must be present.
        Defaults to ``None``.
    :param forbidden: Optional list of binary self-cardinality gates: when
        any gate's ``when`` predicate matches, the field must be absent.
        Defaults to ``None``.
    :param help_placement: Optional override for where the field's
        ``description`` is shown — ``"inline"`` under the input, ``"tooltip"``
        behind a help icon beside the label. ``None`` (the default) lets the
        renderer decide by length: roughly one line renders inline, longer
        prose goes behind the icon. Reference and selector fields ignore it and
        are always inline, having a plain-string label with no node to hang an
        icon from. Typed optional so a route serialising with ``exclude_none``
        keeps it off the wire until a field opts in.
    :param parent: Optional name of a sibling ``bool`` field, in the same
        section, that this field parameterises. The schema-driven React
        renderer draws the field indented beneath that toggle and inert until
        it is on, rather than hiding it. Presentation only — it does not change
        what the server accepts, and the disable state comes from the named
        field's truthiness alone. A field that additionally declares a
        ``forbidden`` gate on the parent being falsy is still nested rather
        than hidden: the renderer recognises that shape and consumes it as the
        disable condition, while every other gate keeps hiding the field.
        Typed optional so a route serialising with ``exclude_none`` drops it
        from the wire until a field opts in, the same posture as
        ``destructive``. Defaults to ``None``.
    """

    name: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)]
    label: NonEmptyStr
    required: bool = False
    description: NonEmptyStr | None = None
    destructive: StrippedNonEmptyStr | None = None
    default: Any | None = None
    requires: list[FieldGate] | None = None
    forbidden: list[FieldGate] | None = None
    parent: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)] | None = None
    help_placement: HelpPlacement | None = None


class BoolField(BaseField):
    """Represent a boolean toggle field.

    :param field_type: The discriminator literal; always ``"bool"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["bool"]
    """

    field_type: Literal["bool"] = Field(
        "bool", alias="type", serialization_alias="type"
    )


class ChoiceField(BaseField):
    """Represent a single-select dropdown field.

    :param field_type: The discriminator literal; always ``"choice"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["choice"]
    :param choices: The non-empty list of options the user can choose from.
    :type choices: list[Choice]
    """

    field_type: Literal["choice"] = Field(
        "choice", alias="type", serialization_alias="type"
    )
    choices: list[Choice] = Field(..., min_length=1)


class FloatField(BaseField):
    """Represent a float numeric input field.

    :param field_type: The discriminator literal; always ``"float"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["float"]
    :param ge: Optional lower bound (inclusive) for accepted values. Defaults
        to ``None``.
    :type ge: float | None
    :param le: Optional upper bound (inclusive) for accepted values. Defaults
        to ``None``.
    :type le: float | None
    :param step: Optional step size for the input control. Defaults to
        ``None``.
    :type step: float | None
    """

    field_type: Literal["float"] = Field(
        "float", alias="type", serialization_alias="type"
    )
    ge: float | None = None
    le: float | None = None
    step: float | None = None


class IntegerField(BaseField):
    """Represent an integer numeric input field.

    :param field_type: The discriminator literal; always ``"integer"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["integer"]
    :param ge: Optional lower bound (inclusive) for accepted values. Defaults
        to ``None``.
    :type ge: int | None
    :param le: Optional upper bound (inclusive) for accepted values. Defaults
        to ``None``.
    :type le: int | None
    :param step: Optional step size for the input control. Defaults to
        ``None``.
    :type step: int | None
    """

    field_type: Literal["integer"] = Field(
        "integer", alias="type", serialization_alias="type"
    )
    ge: int | None = None
    le: int | None = None
    step: int | None = None


class MultiChoiceField(BaseField):
    """Represent a multi-select dropdown field.

    :param field_type: The discriminator literal; always ``"multi_choice"``
        for this class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["multi_choice"]
    :param choices: The non-empty list of options the user can choose from.
    :type choices: list[Choice]
    """

    field_type: Literal["multi_choice"] = Field(
        "multi_choice", alias="type", serialization_alias="type"
    )
    choices: list[Choice] = Field(..., min_length=1)


class StringField(BaseField):
    """Represent a single-line string input field.

    :param field_type: The discriminator literal; always ``"string"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["string"]
    :param min_length: Optional minimum character length. Defaults to
        ``None``.
    :type min_length: int | None
    :param max_length: Optional maximum character length. Defaults to
        ``None``.
    :type max_length: int | None
    :param pattern: Optional regular expression the value must match.
        Defaults to ``None``.
    :type pattern: NonEmptyStr | None
    :param placeholder: Optional placeholder text displayed when the field is
        empty. Defaults to ``None``.
    :type placeholder: NonEmptyStr | None
    """

    field_type: Literal["string"] = Field(
        "string", alias="type", serialization_alias="type"
    )
    min_length: int | None = Field(default=None, ge=1)
    max_length: int | None = Field(default=None, ge=1)
    pattern: NonEmptyStr | None = None
    placeholder: NonEmptyStr | None = None


class TextAreaField(BaseField):
    """Represent a multi-line string input field.

    :param field_type: The discriminator literal; always ``"textarea"`` for
        this class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["textarea"]
    :param rows: Optional number of visible text rows. Defaults to ``None``.
    :type rows: int | None
    :param placeholder: Optional placeholder text displayed when the field is
        empty. Defaults to ``None``.
    :type placeholder: NonEmptyStr | None
    """

    field_type: Literal["textarea"] = Field(
        "textarea", alias="type", serialization_alias="type"
    )
    rows: int | None = Field(default=None, ge=1)
    placeholder: NonEmptyStr | None = None


class DateTimeField(BaseField):
    """Represent a datetime picker field.

    :param field_type: The discriminator literal; always ``"datetime"`` for
        this class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["datetime"]
    """

    field_type: Literal["datetime"] = Field(
        "datetime", alias="type", serialization_alias="type"
    )


class FileField(BaseField):
    """Represent a file upload field.

    :param field_type: The discriminator literal; always ``"file"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["file"]
    :param accept: Optional list of accepted file types, using the same
        syntax as the HTML ``accept`` attribute (for example, ``[".sql",
        ".csv"]`` or ``["image/png"]``). Defaults to ``None``.
    :type accept: list[str] | None
    """

    field_type: Literal["file"] = Field(
        "file", alias="type", serialization_alias="type"
    )
    accept: list[str] | None = None


class YamlField(BaseField):
    """Represent a YAML-syntax multi-line text input field.

    :param field_type: The discriminator literal; always ``"yaml"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["yaml"]
    :param rows: Optional number of visible text rows. Defaults to ``None``.
    :type rows: int | None
    :param placeholder: Optional placeholder text displayed when the field is
        empty. Defaults to ``None``.
    :type placeholder: NonEmptyStr | None
    """

    field_type: Literal["yaml"] = Field(
        "yaml", alias="type", serialization_alias="type"
    )
    rows: int | None = Field(default=None, ge=1)
    placeholder: NonEmptyStr | None = None


class HostField(BaseField):
    """Represent an executor-target (Nomad / Celery) selector field.

    The React renderer loads options from ``GET /api/extensions/hosts/`` (a PMM Extensions
    proxy endpoint that internally calls Tasks ``/hosts/`` and merges
    Inventory display names server-side). When ``depends_on`` is set (typically
    a ``ServiceField``), the renderer may auto-select an executor from the
    upstream service; when omitted every available executor is listed and no
    cascade runs.

    :param field_type: The discriminator literal; always ``"host"`` for this
        class. Serialised as the JSON key ``"type"``.
    :param depends_on: Optional name of the field whose value drives the
        default executor selection. ``None`` (the default) omits the key from
        the wire so plugins that do not opt in stay byte-identical.
    :param target_service: Optional service field for a non-blocking
        co-location warning. Independent of ``depends_on`` (see
        :class:`~app.extensions.apps.framework.form_dsl.markers.HostRef`).
        ``None`` (the default) omits the key from the wire.
    :param allow_custom: When ``True``, the selector also accepts a free-typed
        value alongside the inventory options. ``None`` (the default) omits the
        key from the wire so plugins that do not opt in stay byte-identical.
    """

    field_type: Literal["host"] = Field(
        "host", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr | None = None
    target_service: NonEmptyStr | None = None
    allow_custom: bool | None = None


class MultiHostField(BaseField):
    """Represent a multi-value executor-target selector field.

    The multi-value counterpart of :class:`HostField`: the renderer commits a
    list of executor targets instead of a single one. Derived from a
    ``HostRef(multiple=True)`` marker on a ``list[...]`` / ``set[...]`` field.

    Cascade auto-select is single-host only (:class:`HostField`). ``depends_on``
    may still be emitted when ``Ui(depends_on=...)`` is set so derivation stays
    uniform, but the multi-host renderer does not honour it today.
    ``target_service`` is mirrored the same way; the multi-host renderer
    ignores it (no co-location warning).

    :param field_type: The discriminator literal; always ``"multi_host"`` for
        this class. Serialised as the JSON key ``"type"``.
    :param depends_on: Optional upstream field name mirrored from
        ``Ui(depends_on=...)``. Emitted for wire uniformity with
        :class:`HostField`; the current multi-host renderer ignores it (no
        cascade). ``None`` (the default) omits the key from the wire.
    :param target_service: Optional service field name mirrored for wire
        uniformity with :class:`HostField`; ignored by the multi-host
        renderer. ``None`` (the default) omits the key from the wire.
    :param allow_custom: When ``True``, the selector also accepts free-typed
        values alongside the inventory options. ``None`` (the default) omits the
        key from the wire so plugins that do not opt in stay byte-identical.
    """

    field_type: Literal["multi_host"] = Field(
        "multi_host", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr | None = None
    target_service: NonEmptyStr | None = None
    allow_custom: bool | None = None


class SchemaField(BaseField):
    """Represent an inventory database-schema selector field.

    ``depends_on`` is a forward reference to another field's ``name`` in the
    same plugin schema (typically a ``ServiceField``); the React renderer
    cascades schema options from the selected service. The referenced field
    is not validated at the schema level.

    :param field_type: The discriminator literal; always ``"schema"`` for
        this class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["schema"]
    :param depends_on: The name of the field whose value drives the list of
        available schemas.
    :type depends_on: NonEmptyStr
    :param allow_custom: Opt-in capability flag declaring that a consuming
        renderer may offer free-text (free-solo) entry in addition to the
        cascaded inventory options. Optional, defaulting to ``None`` (options
        only). Typed ``bool | None`` so the discovery endpoint's
        ``exclude_none`` posture drops it from the wire until a plugin opts
        in, keeping the addition byte-compatible with existing schemas. The
        built-in SchemaFormRenderer does not yet consume the flag; the
        free-solo widget that reads it ships with the consuming plugin.
    :type allow_custom: bool | None
    """

    field_type: Literal["schema"] = Field(
        "schema", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr
    allow_custom: bool | None = None


class MultiSchemaField(BaseField):
    """Represent a multi-value inventory database-schema selector field.

    The multi-value counterpart of :class:`SchemaField`: the renderer commits a
    list of schemas instead of a single one. Derived from a
    ``SchemaRef(multiple=True)`` marker on a ``list[...]`` / ``set[...]`` field.

    :param field_type: The discriminator literal; always ``"multi_schema"`` for
        this class. Serialised as the JSON key ``"type"``.
    :param depends_on: The name of the field whose value drives the list of
        available schemas.
    :param allow_custom: Opt-in flag for free-text (free-solo) entry alongside
        the cascaded options. ``None`` (the default) omits the key from the wire
        so plugins that do not opt in stay byte-identical.
    """

    field_type: Literal["multi_schema"] = Field(
        "multi_schema", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr
    allow_custom: bool | None = None


class ServiceField(BaseField):
    """Represent an inventory service selector field.

    :param field_type: The discriminator literal; always ``"service"`` for
        this class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["service"]
    :param service_types: The list of service types the selector should offer
        (for example, ``[ServiceTypeEnum.MYSQL]``).
    :type service_types: list[ServiceTypeEnum]
    :param allow_custom: Opt-in capability flag declaring that a consuming
        renderer may offer free-text (free-solo) entry in addition to the
        inventory options. Optional, defaulting to ``None`` (options only).
        Typed ``bool | None`` so the discovery endpoint's ``exclude_none``
        posture drops it from the wire until a plugin opts in, keeping the
        addition byte-compatible with existing schemas. The built-in
        SchemaFormRenderer does not yet consume the flag; the free-solo
        widget that reads it ships with the consuming plugin.
    :type allow_custom: bool | None
    """

    field_type: Literal["service"] = Field(
        "service", alias="type", serialization_alias="type"
    )
    service_types: list[ServiceTypeEnum]
    allow_custom: bool | None = None


class MultiServiceField(BaseField):
    """Represent a multi-value inventory service selector field.

    The multi-value counterpart of :class:`ServiceField`: the renderer commits a
    list of services instead of a single one. Derived from a
    ``ServiceRef(multiple=True)`` marker on a ``list[...]`` / ``set[...]`` field.

    :param field_type: The discriminator literal; always ``"multi_service"`` for
        this class. Serialised as the JSON key ``"type"``.
    :param service_types: The list of service types the selector should offer
        (for example, ``[ServiceTypeEnum.MYSQL]``).
    :param allow_custom: Opt-in flag for free-text (free-solo) entry alongside
        the inventory options. ``None`` (the default) omits the key from the wire
        so plugins that do not opt in stay byte-identical.
    """

    field_type: Literal["multi_service"] = Field(
        "multi_service", alias="type", serialization_alias="type"
    )
    service_types: list[ServiceTypeEnum]
    allow_custom: bool | None = None


class TableField(BaseField):
    """Represent an inventory table selector field.

    ``depends_on`` is a forward reference to another field's ``name`` in the
    same plugin schema (typically a ``SchemaField``); the React renderer
    cascades table options from the selected schema. The referenced field
    is not validated at the schema level.

    :param field_type: The discriminator literal; always ``"table"`` for this
        class. Serialised as the JSON key ``"type"``.
    :type field_type: Literal["table"]
    :param depends_on: The name of the field whose value drives the list of
        available tables.
    :type depends_on: NonEmptyStr
    :param allow_custom: Opt-in capability flag declaring that a consuming
        renderer may offer free-text (free-solo) entry in addition to the
        cascaded inventory options. Optional, defaulting to ``None`` (options
        only). Typed ``bool | None`` so the discovery endpoint's
        ``exclude_none`` posture drops it from the wire until a plugin opts
        in, keeping the addition byte-compatible with existing schemas. The
        built-in SchemaFormRenderer does not yet consume the flag; the
        free-solo widget that reads it ships with the consuming plugin.
    :type allow_custom: bool | None
    """

    field_type: Literal["table"] = Field(
        "table", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr
    allow_custom: bool | None = None


class MultiTableField(BaseField):
    """Represent a multi-value inventory table selector field.

    The multi-value counterpart of :class:`TableField`: the renderer commits a
    list of tables instead of a single one. Derived from a
    ``TableRef(multiple=True)`` marker on a ``list[...]`` / ``set[...]`` field.

    :param field_type: The discriminator literal; always ``"multi_table"`` for
        this class. Serialised as the JSON key ``"type"``.
    :param depends_on: The name of the field whose value drives the list of
        available tables.
    :param allow_custom: Opt-in flag for free-text (free-solo) entry alongside
        the cascaded options. ``None`` (the default) omits the key from the wire
        so plugins that do not opt in stay byte-identical.
    """

    field_type: Literal["multi_table"] = Field(
        "multi_table", alias="type", serialization_alias="type"
    )
    depends_on: NonEmptyStr
    allow_custom: bool | None = None


class ScriptPreviewField(BaseField):
    """Represent a read-only field that renders a backend-fetched script preview.

    The renderer fetches ``endpoint_url`` on mount, and re-fetches whenever
    any sibling field listed in ``depends_on`` changes value (debounced and
    cancellation-safe). The response shape is
    ``{content: str, language: str, is_truncated: bool}``.

    :param field_type: The discriminator literal; always
        ``"script_preview"`` for this class. Serialised as the JSON key
        ``"type"``.
    :type field_type: Literal["script_preview"]
    :param endpoint_url: The fully-resolved URL the renderer fetches preview
        content from, relative to the FE ``apiClient`` base (``/api``). Schema
        synthesisers should bake any plugin-specific path segments (for
        example, ``/apps/snippets/{filename}/preview``) here at
        schema build time rather than templating client-side.
    :type endpoint_url: NonEmptyStr
    :param depends_on: Names of sibling fields whose values trigger a
        re-fetch when changed. Empty (the default) means fetch once on
        mount.
    :type depends_on: list[NonEmptyStr]
    :param language: Optional default highlighter language hint used when
        the backend response omits ``language``. Defaults to ``None``.
    :type language: NonEmptyStr | None
    """

    field_type: Literal["script_preview"] = Field(
        "script_preview", alias="type", serialization_alias="type"
    )
    endpoint_url: NonEmptyStr
    depends_on: list[NonEmptyStr] = Field(default_factory=list)
    language: NonEmptyStr | None = None


class RemoteChoiceField(BaseField):
    """Represent a field whose options are fetched at render from an app endpoint.

    The renderer fetches ``endpoint_url`` and renders the returned
    ``Choice``-compatible options (``value`` / ``label`` / optional ``disabled``
    / ``disabled_reason``). When ``depends_on`` is set, the fetch is
    parameterised by the dependency's value (appended as a query parameter named
    after ``depends_on``) and the field offers no options until the dependency
    has a value, staying disabled while it waits unless ``allow_custom`` keeps
    free-text entry open. When ``allow_custom`` is set, the renderer also
    accepts a free-typed value. The endpoint response contract is a JSON array
    of objects shaped like :class:`Choice`: ``{"value": str, "label": str,
    "disabled"?: bool, "disabled_reason"?: str}``.

    :param field_type: The discriminator literal; always ``"remote_choice"``.
        Serialised as the JSON key ``"type"``.
    :param endpoint_url: The fully-resolved URL the renderer fetches options
        from, relative to the frontend ``apiClient`` base (``/api``).
    :param depends_on: Optional name of the sibling field whose value drives
        (and parameterises) the option fetch. ``None`` (the default) omits the
        key from the wire so plugins that do not cascade stay byte-identical.
    :param allow_custom: When ``True``, the selector also accepts a free-typed
        value. ``None`` (the default) omits the key from the wire so plugins
        that do not opt in stay byte-identical.
    """

    field_type: Literal["remote_choice"] = Field(
        "remote_choice", alias="type", serialization_alias="type"
    )
    endpoint_url: NonEmptyStr
    depends_on: NonEmptyStr | None = None
    allow_custom: bool | None = None


LeafField = Annotated[
    BoolField
    | ChoiceField
    | DateTimeField
    | FileField
    | FloatField
    | HostField
    | IntegerField
    | MultiChoiceField
    | MultiHostField
    | MultiSchemaField
    | MultiServiceField
    | MultiTableField
    | RemoteChoiceField
    | SchemaField
    | ScriptPreviewField
    | ServiceField
    | StringField
    | TableField
    | TextAreaField
    | YamlField,
    Field(discriminator="field_type"),
]
"""Discriminated union of every leaf field class (excludes :class:`OneOfGroup`)."""


class OneOfBranch(SchemaBaseModel):
    """Represent one mutually-exclusive branch inside a :class:`OneOfGroup`.

    :param value: The discriminator value that selects this branch.
    :param label: The human-readable label for the segmented-control option.
    :param fields: The leaf fields revealed when this branch is active.
    """

    value: NonEmptyStr
    label: NonEmptyStr
    fields: list[LeafField] = Field(..., min_length=1)


class OneOfGroup(SchemaBaseModel):
    """Represent a labelled either/or field group with a segmented mode switch.

    The React renderer binds :attr:`discriminator` to a ``ToggleButtonGroup``,
    shows :attr:`description` as helper text, and renders only the active
    branch's :attr:`~OneOfBranch.fields`. Inactive-branch leaves are forbidden
    at validation time via rules synthesised from the group's branch contract.

    :param field_type: The discriminator literal; always ``"one_of"`` for this
        class. Serialised as the JSON key ``"type"``.
    :param name: Stable group identifier used as the React list key. Not a
        separate form value — :attr:`discriminator` names the mode field.
    :param label: The human-readable group heading above the segmented control.
    :param description: Optional helper text rendered beneath the group label.
        Defaults to ``None``.
    :param discriminator: Dotted path to the mode field (for example,
        ``"source.mode"``) whose value selects the active branch.
    :param default: Optional default branch :attr:`~OneOfBranch.value`. Must
        match one of the declared branches when set. Defaults to ``None``.
    :param branches: Two or more named branches, each owning its own field list.
    """

    field_type: Literal["one_of"] = Field(
        "one_of", alias="type", serialization_alias="type"
    )
    name: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)]
    label: NonEmptyStr
    description: NonEmptyStr | None = None
    discriminator: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)]
    default: NonEmptyStr | None = None
    branches: list[OneOfBranch] = Field(..., min_length=2)

    @model_validator(mode="after")
    def _validate_branch_values(self) -> Self:
        """Ensure branch values are unique and ``default`` matches one branch."""
        values = [branch.value for branch in self.branches]
        duplicates = sorted(
            value for value, count in Counter(values).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate one_of branch value(s): {duplicates}")
        if self.default is not None and self.default not in values:
            raise ValueError(
                f"one_of default {self.default!r} is not a declared branch value; "
                f"known: {sorted(values)}"
            )
        return self


AnyField = Annotated[
    LeafField | OneOfGroup,
    Field(discriminator="field_type"),
]
"""Discriminated union of every concrete field class in the plugin schema DSL."""


class FormSection(SchemaBaseModel):
    """Represent a labelled group of related fields rendered as one fieldset.

    :param title: The section heading displayed above the grouped fields.
    :param description: Optional helper text for the section. PMM Extensions'
        renderer shows it beneath the section heading, but a renderer may
        omit it, so do not put guidance here that a user must see. Defaults
        to ``None``.
    :param fields: The list of fields belonging to this section. May include
        :class:`OneOfGroup` containers alongside leaf fields.
    :param cardinality_rules: Optional cross-field cardinality constraints
        scoped to the fields in this section. Defaults to ``None``.
    :param fail_when: Optional predicate-only invariants scoped to this
        section. Defaults to ``None``.
    :param advanced: Whether the section holds expert options rather than the
        common case. The renderer withholds advanced sections behind a single
        "Show advanced options" control placed after the ordinary ones and
        reveals them as ordinary top-level sections, so several expert sections
        cost one row at rest instead of one each. Membership needs no
        adjacency. Defaults to ``False``.
    :param collapsible: Whether the renderer may collapse this section behind
        a toggle. Defaults to ``False``.
    :param collapsed_by_default: Whether a collapsible section should start
        collapsed. Ignored when ``collapsible`` is ``False``. Defaults to
        ``False``.
    :param render_after_submit: Whether this section should render after the
        submit button instead of before it. Defaults to ``False``.
    :param forbidden: Optional gates that hide the entire section when any
        of them fires. The schema-driven React renderer skips the section
        and unregisters every child field from the form so stale values
        do not ship in the submission payload. Gates may reference any
        field declared in the plugin schema (including fields in other
        sections). Defaults to ``None`` — sections render unconditionally.
        Backend ``fail_when`` and conditional-rule validation on hidden
        sections still applies: hidden-section children arrive in the
        submitted payload as **absent** (not zeroed or defaulted), so
        ``truthy``/``present`` predicates silently pass while
        ``falsy``/``absent`` predicates see the children as missing.
        Author ``fail_when`` rules accordingly.
    """

    title: NonEmptyStr
    description: NonEmptyStr | None = None
    fields: list[AnyField]
    cardinality_rules: list[CardinalityRule] | None = None
    fail_when: list[FailRule] | None = None
    advanced: bool = False
    collapsible: bool = False
    collapsed_by_default: bool = False
    render_after_submit: bool = False
    forbidden: list[FieldGate] | None = None


class Column(SchemaBaseModel):
    """Represent one column in a plugin list view.

    :param key: The task attribute path this column displays (for example,
        ``"status"`` or ``"target.service"``). Must be non-empty.
    :param label: The human-readable column header. Must be non-empty.
    :param sortable: Whether the column can be used to sort the list.
        Defaults to ``False``.
    :param format: Optional formatting hint applied when rendering the
        column values. Defaults to ``None``.
    :param value_labels: Optional map from a raw cell value to the text a
        renderer displays in its place. Defaults to ``None``, which the schema
        route's ``exclude_none`` posture drops from the payload, so a column
        declaring no labels stays byte-identical on the wire. A value absent
        from the map is the consuming app's decision to render as-is.
    """

    key: NonEmptyStr
    label: NonEmptyStr
    sortable: bool = False
    format: ColumnFormat | None = None
    value_labels: dict[NonEmptyStr, NonEmptyStr] | None = None


class ListView(SchemaBaseModel):
    """Represent the list-view configuration for a plugin.

    :param columns: The ordered list of columns displayed in the list view.
    :param default_sort: Optional key of the column to sort by on first
        render. Prefix with ``-`` for descending order (for example,
        ``"-lastRun"``). The unprefixed key must match one of the declared
        column keys. Defaults to ``None``.
    :param server_side_query: Opt-in capability flag declaring that the list
        endpoint honors whole-result-set sort and search via server query
        params. When ``True`` and the list is also server-paginated, the React
        list view enables manual sorting and filtering and drives them through
        those params instead of sorting the loaded page; a capability-on list
        rendered without pagination stays client-side.
        Typed ``bool | None`` so the discovery endpoint's
        ``exclude_none`` posture drops it from the wire until a plugin opts
        in, keeping the addition byte-compatible with existing schemas.
        Defaults to ``None``.
    :param overview_hidden_fields: Additional task-level keys to suppress
        from the auto-rendered "extras" loop on the plugin detail Overview
        tab. The framework always hides a baseline set of internal fields
        (``id``, ``backend``, ``protected``, ``data``, ``updated_at``,
        ``last_updated_by``, ``connectivity_warning``); any keys listed here
        are merged with that baseline. Defaults to ``[]``.
    """

    columns: list[Column]
    default_sort: NonEmptyStr | None = None
    server_side_query: bool | None = None
    overview_hidden_fields: list[StrippedNonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_default_sort_references_column(self) -> Self:
        """Ensure ``default_sort`` (stripped of one leading ``-``) matches a column.

        Exactly one leading ``-`` is stripped to mirror the React consumer,
        which strips a single leading ``-`` via ``replace(/^-/, '')`` when
        deriving the initial sort column; a schema with multiple leading
        dashes (for example, ``"--lastRun"``) would silently fail to match
        a column at render time.

        :return: The validated list view instance.
        :rtype: ListView
        :raises ValueError: If ``default_sort`` does not reference a declared
            column key.
        """
        if self.default_sort is None:
            return self
        target = self.default_sort.removeprefix("-")
        valid_keys = {column.key for column in self.columns}
        if target not in valid_keys:
            raise ValueError(
                f"default_sort {self.default_sort!r} references unknown column key; "
                f"valid keys: {sorted(valid_keys)}"
            )
        return self


#: Read-only execution-host column shared by every host-bearing list view.
#: Never mutate it; pass through ``default_columns()``, which copies per call.
EXECUTOR_HOST_COLUMN = Column(key="hostname", label=EXECUTION_HOST_LABEL)


def default_columns(*middle: Column) -> list[Column]:
    """Return the standard task-plugin list-view columns wrapping ``middle``.

    Build the ordered column list ``[name, status, *middle, created_at,
    last_executed_at, created_by]`` shared by every task-plugin ``ListView``.
    The identity/audit bookends are the part that is duplicated across all eight
    task plugins;
    each plugin passes only its plugin-specific columns (and, where it has one,
    ``EXECUTOR_HOST_COLUMN``) through the ``middle`` slot, in the order they
    should appear. Fresh ``Column`` instances are built on every call — the
    column models are not frozen, so returning shared instances would risk
    aliasing across app declarations. The ``middle`` columns are copied for
    the same reason: callers pass the shared ``EXECUTOR_HOST_COLUMN`` constant,
    so spreading it verbatim would alias every host-bearing view.

    :param middle: The plugin-specific columns to place between ``status`` and
        ``created_at``, in display order.
    :return: The ordered list of columns for the plugin's list view.
    """
    return [
        Column(key="name", label="Name", sortable=True),
        Column(key="status", label="Status", format=ColumnFormat.STATUS),
        *(column.model_copy() for column in middle),
        Column(key="created_at", label="Created", format=ColumnFormat.RELATIVE),
        Column(
            key="last_executed_at",
            label="Last Executed",
            format=ColumnFormat.RELATIVE,
        ),
        Column(key="created_by", label="Created By"),
    ]


DetailPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=(
            r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*"
            r"(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*)*$"
        ),
    ),
]


class DetailField(SchemaBaseModel):
    """Declare one labelled field rendered inside a :class:`DetailSection`.

    :param path: Dotted path into the task record (for example
        ``"data.meta.command"``). Each segment must be a Python identifier,
        optionally followed by one or more ``[N]`` array indices.
    :param label: Human-readable label rendered alongside the resolved value.
        Must be non-empty.
    :param highlight: Optional syntax-highlighter hint. Defaults to ``None``.
    :param value_labels: Optional map from a raw resolved value to the text a
        renderer displays in its place. Defaults to ``None``, which the schema
        route's ``exclude_none`` posture drops from the payload, so a field
        declaring no labels stays byte-identical on the wire. A value absent
        from the map is the consuming app's decision to render as-is.
    """

    path: DetailPath
    label: NonEmptyStr
    highlight: DetailHighlightLanguage | None = None
    value_labels: dict[NonEmptyStr, NonEmptyStr] | None = None


class DetailSection(SchemaBaseModel):
    """Declare one titled section inside a :class:`DetailView`.

    :param title: Heading rendered above the section's fields.
    :param fields: Ordered list of fields rendered inside the section. An
        empty list is valid; the frontend hides the section when every
        field resolves to an empty value.
    """

    title: NonEmptyStr
    fields: list[DetailField]


class DetailView(SchemaBaseModel):
    """Declare the per-section detail-page layout for a task-style plugin.

    Mirrors the role of :attr:`AppSchema.list_view` for the list table:
    the React framework reads ``detail_view`` to render the task detail
    page's section cards instead of inferring structure from the runtime
    ``task.data`` shape.

    :param sections: Ordered list of sections rendered on the detail page.
        An empty list is valid; the frontend renders no section cards.
        Section titles must be unique within a view so the React key can
        be derived from the title without positional disambiguation.
    :type sections: list[DetailSection]
    """

    sections: list[DetailSection]

    @field_validator("sections", mode="after")
    @classmethod
    def _validate_unique_section_titles(
        cls, value: list[DetailSection]
    ) -> list[DetailSection]:
        """Reject duplicate ``DetailSection.title`` values within a view.

        :param value: The validated ``sections`` list.
        :type value: list[DetailSection]
        :return: The input ``value`` unchanged when all titles are unique.
        :rtype: list[DetailSection]
        :raises ValueError: When two or more sections share a title.
        """
        counts = Counter(section.title for section in value)
        duplicates = sorted(title for title, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"Duplicate DetailSection title values: {duplicates}")
        return value


class DerivedTask(SchemaBaseModel):
    """Represent a sibling task derived from a parent during cascade operations.

    The cascade module (:mod:`app.extensions.apps.framework.cascade`) consumes this
    spec when POSTing, PUTting, or DELETEing a plugin's tasks: the parent task
    is created first, then for each ``DerivedTask`` the parent payload is
    deep-copied, ``name`` is suffixed with ``name_suffix``, ``arg_substitutions``
    are applied to ``data["meta"]["args"]`` as literal :meth:`str.replace`
    calls in dict insertion order, ``payload_substitutions`` are applied to
    ``data["payload"]`` as literal :meth:`str.replace` calls in dict insertion
    order, ``data_overrides`` entries are assigned directly onto ``data`` as
    literal key/value pairs in iteration order, and ``data["parent"]`` is set
    to the parent's name when ``parent_link`` is true.

    :param name_suffix: String appended to the parent's ``name`` to form the
        derived task's name (for example ``"-dry-run"``).
    :type name_suffix: NonEmptyStr
    :param arg_substitutions: Optional ordered mapping of literal substring
        replacements applied to ``data["meta"]["args"]``. Each ``(old, new)``
        pair is applied once via :meth:`str.replace` in dict insertion order.
        Defaults to ``None`` (no substitutions).
    :type arg_substitutions: dict[str, str] | None
    :param payload_substitutions: Optional ordered mapping of literal substring
        replacements applied to ``data["payload"]``. Each ``(old, new)``
        pair is applied once via :meth:`str.replace` in dict insertion order.
        Defaults to ``None`` (no substitutions).
    :type payload_substitutions: dict[str, str] | None
    :param data_overrides: Optional mapping of literal key→value pairs
        assigned directly onto ``data`` after substitutions run. Each
        pair becomes ``data[key] = value`` in iteration order. Use this
        for plugin-specific identity fields (e.g. ``{"backup_type":
        "pbm_logical"}``) that the framework should not name itself.
        Defaults to ``None``.
    :param parent_link: When true, set ``data["parent"]`` on the derived
        payload to the parent's ``name``. Defaults to ``True``.
    :type parent_link: bool
    """

    name_suffix: NonEmptyStr
    arg_substitutions: dict[str, str] | None = None
    payload_substitutions: dict[str, str] | None = None
    data_overrides: ArbitraryMapping | None = None
    parent_link: bool = True


class ChainedPredecessor(SchemaBaseModel):
    """Represent a chained predecessor task that runs before the parent.

    The cascade module (:mod:`app.extensions.apps.framework.cascade`) consumes
    this spec when POSTing, PUTting, or DELETEing a plugin's tasks: each
    predecessor is created with ``data["parent"]`` linked to the parent's
    name (when ``parent_link`` is true) and named
    ``f"{parent_name}{name_suffix}"``. Create persists task records only;
    when the user starts the chain, the consuming plugin fires
    ``POST /execute/{first_predecessor_name}`` using
    :func:`build_predecessor_chain_execute_body` for ``chain_task_names``
    (remaining predecessors then parent) and ``chain_on_failure`` derived
    from ``on_failure`` (``"halt"`` maps to ``False``, ``"continue"`` maps
    to ``True``).

    :param name_suffix: String appended to the parent's ``name`` to form
        the predecessor's name (for example ``"-pre-checks"``).
    :type name_suffix: NonEmptyStr
    :param on_failure: Chain semantics when the predecessor terminates
        non-successfully. ``"halt"`` (default) stops the chain;
        ``"continue"`` lets the chain continue regardless. Translates to
        the boolean ``chain_on_failure`` flag at execute time.
    :type on_failure: Literal["halt", "continue"]
    :param parent_link: When true, set ``data["parent"]`` on the
        predecessor payload to the parent's ``name``. Defaults to ``True``.
    :type parent_link: bool
    """

    name_suffix: NonEmptyStr
    on_failure: Literal["halt", "continue"] = "halt"
    parent_link: bool = True


_ROUTE_SEGMENT_PATTERN = r"^[a-z][a-z0-9_-]*$"
_RELATED_APP_RESERVED_ROUTE_SEGMENTS = frozenset({"new", "schedule", "task"})


class RelatedApp(SchemaBaseModel):
    """Declare a separately registered app surfaced as a sibling tab in the UI.

    Consumed by the React ``SchemaDrivenPlugin`` shell to mount a nested
    schema-driven flow for the related registry entry (for example
    ``mysql_backups/restore``) under ``{route_base}/{route_segment}`` without
    re-merging the child app's API router into the parent.

    :param app_key: The scoped registry key of the related app (for example
        ``mysql_backups/restore``).
    :type app_key: NonEmptyStr
    :param label: The human-readable tab label (for example ``Restore``).
    :type label: NonEmptyStr
    :param route_segment: The React sub-path segment under the parent's
        ``route_base`` (for example ``restores``). Must be a single URL path
        segment — no slashes — and must not be a reserved single-entity route
        keyword (``new``, ``schedule``, ``task``).
    :type route_segment: NonEmptyStr
    """

    app_key: NonEmptyStr
    label: NonEmptyStr
    route_segment: Annotated[NonEmptyStr, Field(pattern=_ROUTE_SEGMENT_PATTERN)]

    @field_validator("route_segment", mode="after")
    @classmethod
    def _validate_route_segment_not_reserved(cls, value: str) -> str:
        """Reject segments that collide with single-entity plugin shell routes.

        ``SchemaDrivenPlugin`` registers ``new``, ``schedule``, and
        ``task/:id/*`` alongside ``{route_segment}/*`` for related apps; a
        reserved segment would make React Router match the wrong branch.

        :param value: The validated ``route_segment``.
        :return: The input ``value`` when it is not reserved.
        :raises ValueError: When ``value`` is a reserved route keyword.
        """
        if value in _RELATED_APP_RESERVED_ROUTE_SEGMENTS:
            raise ValueError(
                f"route_segment {value!r} is reserved; "
                f"reserved segments: {sorted(_RELATED_APP_RESERVED_ROUTE_SEGMENTS)}"
            )
        return value


class Capabilities(SchemaBaseModel):
    """Represent plugin-level feature flags.

    :param chaining: Whether the plugin supports chaining tasks together.
        Defaults to ``False``.
    :type chaining: bool
    :param alert_on_fail: Whether the plugin supports configuring a PMM
        alert when a task fails. Defaults to ``False``.
    :type alert_on_fail: bool
    :param scheduling: Whether the plugin supports scheduling tasks on a
        periodic interval. Defaults to ``False``.
    :type scheduling: bool
    :param stats: Whether the plugin supports rendering the aggregated
        execution statistics card on its detail page. Defaults to
        ``False``.
    :type stats: bool
    :param pii_anonymization: Whether the plugin wires ``anonymize_mask``
        into task execution and the React detail page should surface which
        PII entities are anonymized. This is a UI-rendering gate — the
        anonymization always happens when configured; this flag controls
        whether the detail view renders the "PII Anonymization" section.
        Defaults to ``False``.
    :type pii_anonymization: bool
    """

    chaining: bool = False
    alert_on_fail: bool = False
    scheduling: bool = False
    stats: bool = False
    pii_anonymization: bool = False


def _iter_form_item_leaves(field: AnyField) -> Iterator[BaseField]:
    """Yield leaf fields for one section item (a leaf field or a one-of group)."""
    if isinstance(field, OneOfGroup):
        for branch in field.branches:
            yield from branch.fields
        return
    yield field


def iter_section_fields(section: FormSection) -> Iterator[BaseField]:
    """Yield every leaf :class:`BaseField` in ``section``, expanding one-of groups."""
    for field in section.fields:
        yield from _iter_form_item_leaves(field)


def declared_field_names_from_forms(forms: list[FormSection]) -> set[str]:
    """Return every field name conditional rules may reference across ``forms``."""
    names: set[str] = set()
    for section in forms:
        for field in section.fields:
            if isinstance(field, OneOfGroup):
                names.add(field.discriminator)
        for leaf in iter_section_fields(section):
            names.add(leaf.name)
    return names


def _one_of_group_names_from_forms(forms: list[FormSection]) -> frozenset[str]:
    """Return the name of every one-of group declared across ``forms``.

    A one-of group's name is reserved (it must be unique) but is intentionally
    not a rule-referenceable field — only its discriminator and branch leaves are
    (see :func:`declared_field_names_from_forms`). Surfacing the group names lets
    the reference check explain *why* a group name is rejected instead of falling
    back to the generic unknown-field error.

    :param forms: The form sections to scan for one-of group declarations.
    :return: The names of all one-of groups declared across ``forms``.
    """
    return frozenset(
        field.name
        for section in forms
        for field in section.fields
        if isinstance(field, OneOfGroup)
    )


def _register_field_name(
    name: str, global_seen: set[str], duplicates: list[str]
) -> None:
    """Record ``name`` in ``global_seen`` or append to ``duplicates``."""
    if name in global_seen:
        duplicates.append(name)
    else:
        global_seen.add(name)


def _validate_one_of_group_names(
    group: OneOfGroup, global_seen: set[str], duplicates: list[str]
) -> None:
    """Apply uniqueness rules for one :class:`OneOfGroup` and its branch leaves."""
    _register_field_name(group.name, global_seen, duplicates)
    _register_field_name(group.discriminator, global_seen, duplicates)

    branch_counts: Counter[str] = Counter(
        leaf.name for branch in group.branches for leaf in branch.fields
    )
    structural_names = {group.name, group.discriminator}
    branch_shared_registered: set[str] = set()
    for branch in group.branches:
        for leaf in branch.fields:
            if branch_counts[leaf.name] > 1:
                if leaf.name in branch_shared_registered:
                    continue
                branch_shared_registered.add(leaf.name)
            if leaf.name in global_seen:
                if leaf.name not in structural_names:
                    duplicates.append(leaf.name)
            else:
                global_seen.add(leaf.name)


def _validate_unique_field_names_in_forms(forms: list[FormSection]) -> None:
    """Raise when form field names collide outside allowed one-of branch reuse."""
    global_seen: set[str] = set()
    duplicates: list[str] = []

    for section in forms:
        for field in section.fields:
            if isinstance(field, OneOfGroup):
                _validate_one_of_group_names(field, global_seen, duplicates)
            else:
                _register_field_name(field.name, global_seen, duplicates)

    if duplicates:
        raise ValueError(
            f"duplicate field name(s) across form sections: {sorted(set(duplicates))}"
        )


def _collect_reference_errors(
    scope_path: str,
    names: set[str],
    declared_field_names: set[str],
    errors: list[str],
    *,
    implicit_self_name: str | None = None,
    group_field_names: frozenset[str] = frozenset(),
) -> None:
    """Append messages to ``errors`` for any unknown or hyphenated rule names.

    :param scope_path: A human-readable path to the rule emitting the names,
        used in failure messages.
    :type scope_path: str
    :param names: The set of field names referenced by the rule.
    :type names: set[str]
    :param declared_field_names: The set of names declared anywhere in the
        plugin schema's form tree.
    :type declared_field_names: set[str]
    :param errors: A mutable list to which failure messages are appended.
    :type errors: list[str]
    :param implicit_self_name: When set, suppress the descriptive suffix on
        the error for that one name (used for ``BaseField`` gates whose
        implicit-self target is always present in ``names``). Predicate-
        referenced fields with the same value as the implicit self also
        keep the suffix-less form, since the underlying mistake is the
        same: the field that should exist is missing.
    :type implicit_self_name: str | None
    :param group_field_names: The names of one-of groups in the form tree. A
        rule that names one is reported with a targeted hint, since a group is
        not a rule-referenceable field — a group-presence invariant belongs in
        a ``@model_validator`` rather than a field rule.
    """
    for name in names:
        if "-" in name:
            errors.append(
                f"{scope_path} references field {name!r}, but field names that "
                "participate in conditional rules must be valid Python "
                "identifiers (no hyphens)."
            )
            continue
        if name not in declared_field_names:
            if name in group_field_names:
                errors.append(
                    f"{scope_path} references one-of group field {name!r}; a "
                    "one-of group is not a rule-referenceable field (only its "
                    "discriminator and branch fields are). Enforce a "
                    "group-presence invariant with a @model_validator instead."
                )
                continue
            suffix = (
                ""
                if name == implicit_self_name
                else " (the rule names a field that does not exist in any form section)"
            )
            errors.append(f"{scope_path} references unknown field {name!r}{suffix}.")


def _collect_basefield_gate_errors(
    field: "AnyField",
    declared_field_names: set[str],
    errors: list[str],
    *,
    group_field_names: frozenset[str] = frozenset(),
) -> None:
    """Walk a leaf field's ``requires`` / ``forbidden`` gates (expands one-of branches)."""
    if isinstance(field, OneOfGroup):
        for branch in field.branches:
            for leaf in branch.fields:
                _collect_basefield_gate_errors(
                    leaf,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
        return
    for primitive in ("requires", "forbidden"):
        for rule_index, gate in enumerate(getattr(field, primitive) or []):
            references = gate.when.referenced_fields() | {field.name}
            _collect_reference_errors(
                f"BaseField {field.name!r} {primitive}[{rule_index}]",
                references,
                declared_field_names,
                errors,
                implicit_self_name=field.name,
                group_field_names=group_field_names,
            )


def _collect_section_gate_errors(
    section: "FormSection",
    section_label: str,
    declared_field_names: set[str],
    errors: list[str],
    *,
    group_field_names: frozenset[str] = frozenset(),
) -> None:
    """Walk a ``FormSection``'s ``forbidden`` gates."""
    for rule_index, gate in enumerate(section.forbidden or []):
        _collect_reference_errors(
            f"{section_label} forbidden[{rule_index}]",
            gate.when.referenced_fields(),
            declared_field_names,
            errors,
            group_field_names=group_field_names,
        )


def _collect_cardinality_rule_errors(
    rules: list[CardinalityRule] | None,
    scope_label: str,
    declared_field_names: set[str],
    errors: list[str],
    *,
    group_field_names: frozenset[str] = frozenset(),
) -> None:
    """Walk a list of :class:`CardinalityRule` instances."""
    for rule_index, rule in enumerate(rules or []):
        references = set(rule.fields)
        if rule.when is not None:
            references |= rule.when.referenced_fields()
        _collect_reference_errors(
            f"{scope_label} cardinality_rules[{rule_index}]",
            references,
            declared_field_names,
            errors,
            group_field_names=group_field_names,
        )


def _collect_fail_rule_errors(
    rules: list[FailRule] | None,
    scope_label: str,
    declared_field_names: set[str],
    errors: list[str],
    *,
    group_field_names: frozenset[str] = frozenset(),
) -> None:
    """Walk a list of :class:`FailRule` instances."""
    for rule_index, rule in enumerate(rules or []):
        references = rule.fail_when.referenced_fields() | set(rule.error_fields)
        _collect_reference_errors(
            f"{scope_label} fail_when[{rule_index}]",
            references,
            declared_field_names,
            errors,
            group_field_names=group_field_names,
        )


ITEM_DISPLAY_NAME_KEYS = ("item_display_name", "item_display_name_plural")
"""The two record-name keys, shared with the conformance detector that checks them."""

_VOWELS = frozenset("aeiou")


def _require_item_display_names_in_json_schema(
    json_schema: dict[str, Any],
) -> None:
    """Keep record-name fields required on the wire despite constructor defaults.

    Both fields default to ``None`` at construction so authors (and ty) may omit
    them; :func:`_fill_item_display_names` always writes a string before the
    instance exists. Without this patch, Pydantic would drop them from
    ``required`` and advertise a null default in OpenAPI, breaking the
    generated client's non-nullable ``string`` contract.

    Bound as ``json_schema_extra`` on :class:`AppSchema` and
    :class:`AppEntitySchema` (mutating callable form).

    :param json_schema: The JSON schema Pydantic built for the model; mutated
        in place.
    """
    required: set[str] = set(json_schema.get("required") or ())
    required.update(ITEM_DISPLAY_NAME_KEYS)
    json_schema["required"] = sorted(required)
    properties = json_schema.get("properties") or {}
    for key in ITEM_DISPLAY_NAME_KEYS:
        prop = properties.get(key)
        if isinstance(prop, dict):
            prop.pop("default", None)


def pluralize_item_display_name(singular: str) -> str:
    """Return a Django-style English plural for a mid-sentence record noun.

    Pluralises only the last whitespace-separated token so multi-word nouns
    like ``schema change`` become ``schema changes``. Irregular plurals and
    forms outside this heuristic stay author-declared via
    ``item_display_name_plural``. Shared by schema defaulting and the scaffold
    plural prompt default.

    :param singular: The resolved singular record noun (explicit or
        ``display_name`` fallback).
    :return: The pluralised noun under the consonant-``y`` / sibilant / ``+s``
        rules (including vowel-``y`` → ``+s``, e.g. ``key`` → ``keys``).
    """
    head, sep, word = singular.rpartition(" ")
    if not word:
        return singular
    lower = word.lower()
    penult = lower[-2:-1]
    if lower.endswith("y") and penult and penult not in _VOWELS:
        plural_word = f"{word[:-1]}ies"
    elif lower.endswith(("s", "x", "z", "ch", "sh")):
        plural_word = f"{word}es"
    else:
        plural_word = f"{word}s"
    return f"{head}{sep}{plural_word}"


def _fill_item_display_names(data: Any) -> Any:
    """Fill unset record names from ``display_name`` or a declared singular.

    Backs the ``mode="before"`` validator on :class:`AppEntitySchema` and
    :class:`AppSchema` so both fields can be declared bare and required while
    staying optional for the author. The singular falls back to
    ``display_name`` when omitted. The plural is derived from a
    **declared** singular via a Django-style pluraliser; when the singular
    was also defaulted, the plural falls back to ``display_name`` (so an
    already-plural title is not pluralised again). An explicit
    ``item_display_name_plural`` always wins (irregular override).

    :param data: The raw input passed to the model, which Pydantic hands over
        before field validation and therefore does not constrain — anything the
        caller passed to ``model_validate`` arrives here. Non-mapping input is
        returned untouched so Pydantic reports it as a ``model_type`` error
        rather than this function raising ``AttributeError`` out of the
        validator; input whose ``display_name`` is absent or not a string is
        returned untouched for the same reason, leaving Pydantic to report the
        ``display_name`` error alone. A non-string
        ``item_display_name`` is likewise left untouched so the pluraliser is
        never called on a non-str and Pydantic surfaces the field type error.
    :return: A mapping with either record name filled, or the input unchanged
        when both were supplied or nothing could be filled.
    """
    if not isinstance(data, Mapping):
        return data
    display_name = data.get("display_name")
    if not isinstance(display_name, str):
        return data
    declared_singular = data.get("item_display_name")
    if declared_singular is not None and not isinstance(declared_singular, str):
        return data
    singular = display_name if declared_singular is None else declared_singular
    filled: dict[str, str] = {}
    if declared_singular is None:
        filled["item_display_name"] = singular
    if data.get("item_display_name_plural") is None:
        filled["item_display_name_plural"] = (
            pluralize_item_display_name(singular)
            if declared_singular is not None
            else display_name
        )
    return {**data, **filled} if filled else data


class AppEntitySchema(SchemaBaseModel):
    """Describe one CRUD entity for a multi-entity schema-driven plugin.

    Used when a plugin exposes several independent resources (for example
    inventory nodes, services, schemas, and tables), each with its own list
    view and create/edit forms. Task-style plugins omit ``entities`` and use
    the root ``forms`` / ``list_view`` instead.

    :param name: URL segment and API key for the entity (for example ``nodes``).
    :param display_name: Human-readable title for this entity's screens.
    :param item_display_name: What **one** record of this entity is called (for
        example ``node``), as opposed to ``display_name``, which names the
        entity's screens. Stored in mid-sentence form so a consumer composing a
        label capitalises the first character itself. Defaults to this entity's
        own ``display_name`` — not the parent app's, and never inferred from
        ``item_display_name_plural``. Optional at construction (``None``
        default); the before-validator always fills a string, and the OpenAPI
        schema keeps the field required and non-nullable.
    :param item_display_name_plural: What **several** records of this entity are
        called (for example ``nodes``). Same mid-sentence convention. When the
        singular is declared, defaults by pluralising it; when both are
        omitted, defaults to this entity's own ``display_name``. Declare
        explicitly for irregulars or forms the heuristic misses. Optional at
        construction under the same wire-required contract as the singular.
    :param description: Optional helper text for this entity. Defaults to
        ``None``.
    :param forms: Form sections for create (and edit when the UI supports it).
    :param list_view: Column configuration for this entity's list table.
    :param detail_highlights: Optional per-field syntax highlighter hints for
        detail pages. Keys are field names; values are highlighting languages.
        Defaults to an empty mapping.
    :param cardinality_rules: Optional entity-wide cross-field cardinality
        constraints. Defaults to ``None``.
    :param fail_when: Optional entity-wide predicate-only invariants.
        Defaults to ``None``.
    """

    name: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)]
    display_name: NonEmptyStr
    item_display_name: NonEmptyStr = Field(default=None)
    item_display_name_plural: NonEmptyStr = Field(default=None)
    description: NonEmptyStr | None = None
    forms: list[FormSection]
    list_view: ListView
    detail_highlights: dict[NonEmptyStr, DetailHighlightLanguage] = Field(
        default_factory=dict
    )
    cardinality_rules: list[CardinalityRule] | None = None
    fail_when: list[FailRule] | None = None

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        json_schema_extra=_require_item_display_names_in_json_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def _default_item_display_names(cls, data: Any) -> Any:
        """Fill unset record names from ``display_name`` or a declared singular.

        :param data: The raw input Pydantic passes before field validation.
        :return: The input with either record name filled, or unchanged when both
            were supplied or nothing could be filled.
        """
        return _fill_item_display_names(data)

    @model_validator(mode="after")
    def _validate_unique_field_names(self) -> Self:
        """Ensure field ``name`` values are unique within this entity's forms."""
        _validate_unique_field_names_in_forms(self.forms)
        return self


class TaskStatusDescriptor(SchemaBaseModel):
    """Declare one task-status value and its run terminality/output predicates.

    :param value: The status as it appears on a task-history payload.
    :param terminal: Whether a run in this status will not transition again, so
        a client polling for completion can stop re-reading on it.
    :param output_available: Whether the run reached an observed outcome, so its
        output may be requested and may legitimately be empty, as for ``stale``
        and ``unlaunchable``. ``lost`` is excluded because its outcome was never
        observed.
    """

    value: TaskHistoryStatusEnum
    terminal: bool
    output_available: bool


def _task_status_descriptors() -> list[TaskStatusDescriptor]:
    """Return the task-status vocabulary in enum declaration order.

    :return: One descriptor per :class:`TaskHistoryStatusEnum` member, each
        classified by :meth:`TaskHistoryStatusEnum.is_terminal` and
        :meth:`TaskHistoryStatusEnum.is_finished` (the observed-outcome
        predicate backing ``output_available``).
    """
    return [
        TaskStatusDescriptor(
            value=status,
            terminal=status.is_terminal(),
            output_available=status.is_finished(),
        )
        for status in TaskHistoryStatusEnum
    ]


class AppSchema(SchemaBaseModel):
    """Represent a plugin's complete schema: form sections, list view, capabilities.

    :param name: The plugin identifier; must match Python identifier rules,
        optionally with internal hyphens.
    :param display_name: The human-readable plugin title displayed in the UI.
    :param item_display_name: What **one** record this plugin's create form
        produces is called (for example ``backup``), as opposed to
        ``display_name``, which names the plugin. Stored in mid-sentence form —
        lowercase unless it opens with a proper noun — so a consumer composing a
        label capitalises the first character itself. Defaults to
        ``display_name``, and is never inferred from
        ``item_display_name_plural``. Optional at construction (``None``
        default); the before-validator always fills a string. Unlike the
        optional UI hints on this model, both record names stay required and
        non-nullable on the wire so the generated client types them as
        ``string`` and no consumer needs a fallback.
    :param item_display_name_plural: What **several** of those records are
        called (for example ``backups``). Same mid-sentence convention. When
        the singular is declared, defaults by pluralising it; when both are
        omitted, defaults to ``display_name``. Declare explicitly for
        irregulars or forms the heuristic misses. Optional at construction
        under the same wire-required contract as the singular.
    :param description: Optional helper text describing the plugin's
        purpose. Defaults to ``None``.
    :param task_type: Optional task-type identifier used when creating tasks
        via the shared task API. Defaults to ``None``.
    :param forms: Form sections for single-entity / task plugins. When
        ``entities`` is non-empty, root ``forms`` must be empty (declare
        forms on each entity instead); non-empty root ``forms`` are rejected
        at construction. Defaults to an empty list.
    :param capabilities: Optional plugin-level feature flags. Defaults to
        ``None``.
    :param list_view: List-view configuration when ``entities`` is unset
        (single-entity / task plugins). Ignored when ``entities`` is set.
    :param detail_view: Optional declarative layout for the task detail page's
        section cards (task-style plugins only; ignored when ``entities`` is
        set). Optional at the model layer for backwards compatibility. A
        forward-looking guard refuses to load a plugin that sets
        ``task_type`` without declaring ``detail_view``. Defaults to ``None``.
    :param entities: Optional list of CRUD entities for multi-resource plugins.
        When non-empty, the React shell renders one list/create/detail flow
        per entity. Defaults to ``None`` (legacy single-entity mode).
    :param cardinality_rules: Optional plugin-wide cross-field cardinality
        constraints (task-style plugins only). Rejected at construction when
        ``entities`` is non-empty — declare rules on each entity instead.
        Defaults to ``None``.
    :param fail_when: Optional plugin-wide predicate-only invariants (task-style
        plugins only). Rejected at construction when ``entities`` is non-empty —
        declare rules on each entity instead. Defaults to ``None``.
    :param derived: Optional declarative specs for sibling tasks derived from
        the parent task on cascade. Consumed by
        :mod:`app.extensions.apps.framework.cascade` to drive POST/PUT/DELETE
        across the parent and N derived siblings. Defaults to ``None``.
    :param predecessors: Optional declarative specs for tasks that must run
        before the parent. Consumed by
        :mod:`app.extensions.apps.framework.cascade` to drive POST/PUT/DELETE
        across the predecessors and the parent, including the chain wiring
        applied at execute time. Defaults to ``None``.
    :param related_apps: Optional separately registered apps the React shell
        surfaces as sibling tabs (for example a restore app nested under a
        backups parent). Defaults to ``None``.
    :param task_statuses: The task-status vocabulary a client polls against,
        declaring per status value both run terminality and whether output
        retrieval is meaningful. Server-authored, so a supplied value is
        replaced rather than honoured. Withheld (``None``) for a plugin
        declaring ``entities``, whose records are not task runs.
    """

    name: Annotated[NonEmptyStr, Field(pattern=_FIELD_NAME_PATTERN)]
    display_name: NonEmptyStr
    item_display_name: NonEmptyStr = Field(default=None)
    item_display_name_plural: NonEmptyStr = Field(default=None)
    description: NonEmptyStr | None = None
    task_type: NonEmptyStr | None = None
    forms: list[FormSection] = Field(default_factory=list)
    capabilities: Capabilities | None = None
    list_view: ListView | None = None
    detail_view: DetailView | None = None
    entities: list[AppEntitySchema] | None = None
    cardinality_rules: list[CardinalityRule] | None = None
    fail_when: list[FailRule] | None = None
    derived: list[DerivedTask] | None = None
    predecessors: list[ChainedPredecessor] | None = None
    related_apps: list[RelatedApp] | None = None
    task_statuses: list[TaskStatusDescriptor] | None = None

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        json_schema_extra=_require_item_display_names_in_json_schema,
    )

    @model_validator(mode="before")
    @classmethod
    def _default_item_display_names(cls, data: Any) -> Any:
        """Fill unset record names from ``display_name`` or a declared singular.

        :param data: The raw input Pydantic passes before field validation.
        :return: The input with either record name filled, or unchanged when both
            were supplied or nothing could be filled.
        """
        return _fill_item_display_names(data)

    @model_validator(mode="after")
    def _populate_task_statuses(self) -> Self:
        """Publish the status vocabulary, or withhold it for entity plugins.

        The list is derived from :class:`~app.tasks.models.TaskHistoryStatusEnum`
        and overwrites whatever a caller supplied, though a supplied value still
        has to parse as ``list[TaskStatusDescriptor]`` first, since the field is
        declared rather than computed.

        Deriving it here rather than at the construction sites covers every path
        that *validates* an ``AppSchema`` — ``__init__`` and ``model_validate``
        — including the ``schema=`` passthrough that never reaches
        ``derive_app_schema``. ``model_construct`` and ``model_copy`` bypass
        validation and so bypass this.

        A ``computed_field`` is the more idiomatic derivation and is ruled out
        here: :class:`SchemaBaseModel` sets ``extra="forbid"``, and a computed
        field serialises into the dump without being an accepted input, so every
        ``AppSchema`` round-trip back through ``model_validate`` would fail.

        :return: The validated plugin schema instance.
        """
        self.task_statuses = None if self.entities else _task_status_descriptors()
        return self

    @model_validator(mode="after")
    def _validate_detail_view_required_for_task_type(self) -> Self:
        """Refuse to load a task-style plugin that omits ``detail_view``.

        The field is optional at the model layer for backwards compatibility
        with legacy plugin schemas that do not yet declare a ``task_type``.
        Once a plugin opts into the shared task API by setting ``task_type``,
        the React framework can no longer infer the detail-page section
        layout from the runtime ``task.data`` shape, so the schema must
        declare it. Use ``DetailView(sections=[])`` to opt out of rendering
        any section cards.

        :return: The validated plugin schema instance.
        :raises ValueError: When ``task_type`` is set and ``detail_view``
            is unset.
        """
        if self.task_type is not None and self.detail_view is None:
            raise ValueError(
                "AppSchema.detail_view is required when task_type is set "
                "(declare DetailView(sections=[]) to render no section cards)"
            )
        return self

    @field_validator("derived", mode="after")
    @classmethod
    def _validate_unique_derived_name_suffixes(
        cls, value: list[DerivedTask] | None
    ) -> list[DerivedTask] | None:
        """Ensure no two ``DerivedTask`` specs share the same ``name_suffix``.

        Duplicate ``name_suffix`` values would cause deterministic task-name
        collisions during cascade (both derived specs would attempt to POST,
        PUT, or DELETE a task with the same generated name). Reject them at
        schema-construction time so the failure surfaces at plugin load
        rather than on the first task creation.

        :param value: The validated ``derived`` list, or ``None`` when unset.
        :type value: list[DerivedTask] | None
        :return: The input ``value`` unchanged when all suffixes are unique.
        :rtype: list[DerivedTask] | None
        :raises ValueError: When two or more entries share a ``name_suffix``.
        """
        if not value:
            return value
        counts = Counter(spec.name_suffix for spec in value)
        duplicates = sorted(suffix for suffix, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"Duplicate derived name_suffix values: {duplicates}")
        return value

    @field_validator("predecessors", mode="after")
    @classmethod
    def _validate_predecessors(
        cls, value: list[ChainedPredecessor] | None
    ) -> list[ChainedPredecessor] | None:
        """Reject duplicate ``name_suffix`` and mixed ``on_failure`` values.

        Duplicate suffixes would cause deterministic task-name collisions on
        cascade. Mixed ``on_failure`` policies cannot be expressed by the
        underlying chain machinery: celery's
        :func:`_dispatch_chained_task` inherits ``_chain_on_failure`` from
        the parent's execution request and applies it to every chained
        step. Reject both at schema construction time so the failure
        surfaces at plugin load, not on first task creation.

        Also collapses ``[]`` to ``None`` so the field's contract is
        single-valued: either the plugin declares predecessors (non-empty
        list) or omits them entirely.

        :param value: The validated ``predecessors`` list, or ``None``.
        :type value: list[ChainedPredecessor] | None
        :return: The input ``value`` when non-empty, ``None`` when the
            input was ``None`` or an empty list.
        :rtype: list[ChainedPredecessor] | None
        :raises ValueError: When two entries share a ``name_suffix`` or when
            the entries have differing ``on_failure`` values.
        """
        if not value:
            return None
        counts = Counter(spec.name_suffix for spec in value)
        duplicates = sorted(suffix for suffix, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"Duplicate predecessors name_suffix values: {duplicates}")
        on_failure_values = {spec.on_failure for spec in value}
        if len(on_failure_values) > 1:
            raise ValueError(
                "Mixed on_failure policies in predecessors are not supported; "
                "all entries must share the same on_failure value because "
                "celery's chain machinery inherits _chain_on_failure chain-wide. "
                f"Found: {sorted(on_failure_values)}"
            )
        return value

    @field_validator("related_apps", mode="after")
    @classmethod
    def _validate_unique_related_app_route_segments(
        cls, value: list[RelatedApp] | None
    ) -> list[RelatedApp] | None:
        """Ensure no two ``RelatedApp`` specs share the same ``route_segment``.

        Duplicate segments would mount two nested plugin shells on the same
        React sub-path. Reject at schema-construction time so the failure
        surfaces at plugin load rather than at runtime routing.

        :param value: The validated ``related_apps`` list, or ``None`` when unset.
        :return: The input ``value`` unchanged when all segments are unique.
        :raises ValueError: When two or more entries share a ``route_segment``.
        """
        if not value:
            return value
        counts = Counter(spec.route_segment for spec in value)
        duplicates = sorted(segment for segment, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                f"Duplicate related_apps route_segment values: {duplicates}"
            )
        return value

    @model_validator(mode="after")
    def _validate_cascade_suffixes_disjoint(self) -> Self:
        """Reject ``name_suffix`` values shared between ``derived`` and ``predecessors``.

        Both cascade families produce task names of the form
        ``f"{parent_name}{name_suffix}"`` on cascade. A suffix declared on
        both lists would target the same task name on the tasks API, so
        the create/update/delete flow that manages both task sets would
        collide deterministically. Reject at schema-construction time so
        the failure surfaces at plugin load rather than on first cascade.

        :return: The validated plugin schema instance.
        :raises ValueError: When the two lists share at least one
            ``name_suffix`` value.
        """
        if not self.derived or not self.predecessors:
            return self
        derived_suffixes = {spec.name_suffix for spec in self.derived}
        predecessor_suffixes = {spec.name_suffix for spec in self.predecessors}
        shared = sorted(derived_suffixes & predecessor_suffixes)
        if shared:
            raise ValueError(
                f"name_suffix values shared between derived and predecessors "
                f"would collide on cascade: {shared}"
            )
        return self

    @model_validator(mode="after")
    def _validate_form_configuration(self) -> Self:
        """Validate root-level form configuration for plugin schemas.

        When ``entities`` is non-empty, root ``forms``, ``cardinality_rules``,
        and ``fail_when`` must be empty — those keys belong on the entity that
        owns them. If any are set alongside non-empty ``entities``, construction
        fails with a message naming the offending keys.

        For task-style schemas (``entities`` unset or empty), requires
        ``list_view`` and validates field-name uniqueness across root ``forms``.

        :return: The validated plugin schema instance.
        :raises ValueError: If root form config is set on an entity-style
            schema, if duplicate field names appear, or if neither
            ``entities`` nor ``list_view`` is usable.
        """
        if self.entities:
            conflicting: list[str] = []
            if self.forms:
                conflicting.append("forms")
            if self.cardinality_rules:
                conflicting.append("cardinality_rules")
            if self.fail_when:
                conflicting.append("fail_when")
            if conflicting:
                entity_names = ", ".join(repr(e.name) for e in self.entities)
                raise ValueError(
                    f"Root-level {', '.join(conflicting)} must not be set on an "
                    f"entity-style schema — move the declaration onto the "
                    f"relevant entity ({entity_names})."
                )
            return self
        if self.list_view is None:
            raise ValueError(
                "list_view is required when entities is not set (task-style plugins)"
            )
        _validate_unique_field_names_in_forms(self.forms)
        return self

    @model_validator(mode="after")
    def _validate_rule_field_references(self) -> Self:
        """Resolve every conditional-rule field reference against the schema tree.

        Performs Tier-2 validation: walks every BaseField ``requires`` /
        ``forbidden`` rule, every FormSection ``cardinality_rules`` /
        ``fail_when`` rule, and every AppSchema- or AppEntitySchema-scope
        rule. For each, it collects the predicate's referenced fields plus the
        rule's target ``fields`` / ``error_fields`` / implicit-self target (for
        BaseField gates) and verifies each name resolves to a known field
        declared in the schema. Hyphenated field names are also rejected here
        since they cannot resolve to Python attributes via ``getattr`` at
        runtime.

        :return: The validated plugin schema instance.
        :raises ValueError: If any rule references a field that does not
            exist in the schema, or whose name contains a hyphen.
        """
        errors = []
        if self.entities:
            for entity_index, entity in enumerate(self.entities):
                declared_field_names = declared_field_names_from_forms(entity.forms)
                group_field_names = _one_of_group_names_from_forms(entity.forms)
                entity_label = f"AppEntitySchema[{entity_index}] {entity.name!r}"
                for section_index, section in enumerate(entity.forms):
                    section_label = (
                        f"{entity_label} FormSection[{section_index}] {section.title!r}"
                    )
                    for field in section.fields:
                        _collect_basefield_gate_errors(
                            field,
                            declared_field_names,
                            errors,
                            group_field_names=group_field_names,
                        )
                    _collect_section_gate_errors(
                        section,
                        section_label,
                        declared_field_names,
                        errors,
                        group_field_names=group_field_names,
                    )
                    _collect_cardinality_rule_errors(
                        section.cardinality_rules,
                        section_label,
                        declared_field_names,
                        errors,
                        group_field_names=group_field_names,
                    )
                    _collect_fail_rule_errors(
                        section.fail_when,
                        section_label,
                        declared_field_names,
                        errors,
                        group_field_names=group_field_names,
                    )
                _collect_cardinality_rule_errors(
                    entity.cardinality_rules,
                    entity_label,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
                _collect_fail_rule_errors(
                    entity.fail_when,
                    entity_label,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
        else:
            declared_field_names = declared_field_names_from_forms(self.forms)
            group_field_names = _one_of_group_names_from_forms(self.forms)
            for section_index, section in enumerate(self.forms):
                section_label = f"FormSection[{section_index}] {section.title!r}"
                for field in section.fields:
                    _collect_basefield_gate_errors(
                        field,
                        declared_field_names,
                        errors,
                        group_field_names=group_field_names,
                    )
                _collect_section_gate_errors(
                    section,
                    section_label,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
                _collect_cardinality_rule_errors(
                    section.cardinality_rules,
                    section_label,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
                _collect_fail_rule_errors(
                    section.fail_when,
                    section_label,
                    declared_field_names,
                    errors,
                    group_field_names=group_field_names,
                )
            schema_label = f"AppSchema {self.name!r}"
            _collect_cardinality_rule_errors(
                self.cardinality_rules,
                schema_label,
                declared_field_names,
                errors,
                group_field_names=group_field_names,
            )
            _collect_fail_rule_errors(
                self.fail_when,
                schema_label,
                declared_field_names,
                errors,
                group_field_names=group_field_names,
            )
        if errors:
            raise ValueError("; ".join(errors))
        return self


class AppDeploymentCapabilities(BaseModel):
    """Serve as the base class for plugin ``GET /capabilities`` response models.

    Inherit from this class when defining the response model for a plugin's
    ``capabilities_endpoint()`` provider.  The marker lets consumers
    statically identify deployment-capabilities models and distinguishes them
    from :class:`Capabilities`, which describes static UI feature flags on
    :attr:`AppSchema.capabilities` (chaining, scheduling, alert_on_fail).
    """
