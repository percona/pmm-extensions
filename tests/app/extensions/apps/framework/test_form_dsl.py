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

"""Test the model-first form DSL: marker extraction, wire-type inference, derivation."""

from datetime import datetime
from enum import auto, StrEnum
from typing import Annotated, ClassVar, Literal

import annotated_types
import pytest
from pydantic import BaseModel, create_model, Field, ValidationError

from app.core.utils.fields import (
    EmptyStrToNone,
    NON_WHITESPACE_PATTERN,
    NonEmptyStr,
    StrippedNonEmptyStr,
    TcpPort,
)
from app.extensions.apps.framework.form_dsl import (
    AppFormModel,
    Choices,
    derive_app_schema,
    derive_form_sections,
    FieldWidget,
    Forbidden,
    FormLayout,
    FormRules,
    HelpPlacement,
    Hidden,
    HostRef,
    Option,
    RemoteChoices,
    Requires,
    SchemaRef,
    SectionLayout,
    SectionRules,
    ServiceRef,
    TableRef,
    Ui,
)
from app.extensions.apps.framework.form_dsl.derivation import build_runtime_schema
from app.extensions.apps.framework.rules import (
    CardinalityRule,
    F,
    FailRule,
    FieldGate,
    truthy,
)
from app.extensions.apps.framework.schema import (
    BoolField,
    ChoiceField,
    Column,
    DateTimeField,
    FloatField,
    HostField,
    IntegerField,
    ListView,
    MultiChoiceField,
    MultiHostField,
    MultiSchemaField,
    MultiServiceField,
    MultiTableField,
    OneOfGroup,
    RelatedApp,
    RemoteChoiceField,
    SchemaField,
    ServiceField,
    StringField,
    TableField,
    TextAreaField,
    YamlField,
)
from app.inventory.models import ServiceTypeEnum

_SINGLE_SECTION = FormLayout(sections=[SectionLayout(key="s", title="S")])
_MINIMAL_LIST_VIEW = ListView(columns=[Column(key="name", label="Name")])
_PORT_MAX = 65535
_CODE_MIN_LEN = 2
_CODE_MAX_LEN = 8


class _Color(StrEnum):
    red = auto()
    green = auto()


def _fields_by_name(model: type[AppFormModel]) -> dict:
    """Return the single-section derived fields of ``model`` keyed by name."""
    sections = derive_form_sections(model, _SINGLE_SECTION)
    return {field.name: field for section in sections for field in section.fields}


class _ScalarModel(AppFormModel):
    an_int: Annotated[int, Ui(label="I", section="s")] = 0
    a_str: Annotated[str, Ui(label="S", section="s")] = ""
    a_bool: Annotated[bool, Ui(label="B", section="s")] = False
    a_float: Annotated[float, Ui(label="F", section="s")] = 0.0
    a_dt: Annotated[datetime | None, Ui(label="D", section="s")] = None
    opt: Annotated[int | None, Ui(label="O", section="s")] = None
    coerced: Annotated[int | EmptyStrToNone, Ui(label="E", section="s")] = None
    port: Annotated[
        int | None, Field(ge=1, le=_PORT_MAX), Ui(label="P", section="s")
    ] = None
    note: Annotated[str, Ui(label="N", section="s", widget=FieldWidget.TEXTAREA)] = ""
    cfg: Annotated[str, Ui(label="Y", section="s", widget=FieldWidget.YAML)] = ""


class _ConstrainedStrModel(AppFormModel):
    code: Annotated[
        str | None,
        Field(min_length=_CODE_MIN_LEN, max_length=_CODE_MAX_LEN, pattern="^x"),
        Ui(label="Code", section="s"),
    ] = None


class TestWireTypeInference:
    """Cover base-annotation -> field-kind inference for every scalar kind."""

    def test_string_constraints_mapped(self) -> None:
        """Map min_length/max_length/pattern onto the derived StringField."""
        code = _fields_by_name(_ConstrainedStrModel)["code"]
        assert isinstance(code, StringField)
        assert code.min_length == _CODE_MIN_LEN
        assert code.max_length == _CODE_MAX_LEN
        assert code.pattern == "^x"

    def test_scalar_kinds(self) -> None:
        """Map int/str/bool/float/datetime to their field classes."""
        fields = _fields_by_name(_ScalarModel)
        assert isinstance(fields["an_int"], IntegerField)
        assert isinstance(fields["a_str"], StringField)
        assert isinstance(fields["a_bool"], BoolField)
        assert isinstance(fields["a_float"], FloatField)
        assert isinstance(fields["a_dt"], DateTimeField)

    def test_optional_and_emptystr_unwrap_to_base(self) -> None:
        """Look through ``X | None`` and ``X | EmptyStrToNone`` to the base type."""
        fields = _fields_by_name(_ScalarModel)
        assert isinstance(fields["opt"], IntegerField)
        assert isinstance(fields["coerced"], IntegerField)

    def test_ge_le_mapped_from_constraints(self) -> None:
        """Map ``Field(ge=, le=)`` constraints onto the numeric field's bounds."""
        port = _fields_by_name(_ScalarModel)["port"]
        assert isinstance(port, IntegerField)
        assert port.ge == 1
        assert port.le == _PORT_MAX

    def test_string_widget_overrides(self) -> None:
        """Select TextArea/Yaml field classes from the ``Ui(widget=...)`` override."""
        fields = _fields_by_name(_ScalarModel)
        assert isinstance(fields["note"], TextAreaField)
        assert isinstance(fields["cfg"], YamlField)


_RANGED_INT_MIN = 1
_RANGED_INT_MAX = 10
_MULTI_GE = 3
_MULTI_LE = 99
_OUTER_GE = 100
_OUTER_LE = 200


class _GroupedBoundsModel(AppFormModel):
    ranged_int: Annotated[
        int,
        annotated_types.Interval(ge=_RANGED_INT_MIN, le=_RANGED_INT_MAX),
        Ui(label="RI", section="s"),
    ] = _RANGED_INT_MIN
    ranged_str: Annotated[
        str,
        annotated_types.Len(_CODE_MIN_LEN, _CODE_MAX_LEN),
        Ui(label="RS", section="s"),
    ] = ""
    multi_grouped: Annotated[
        int,
        annotated_types.Interval(ge=_MULTI_GE),
        annotated_types.Interval(le=_MULTI_LE),
        Ui(label="MG", section="s"),
    ] = _MULTI_GE
    stripped: Annotated[StrippedNonEmptyStr, Ui(label="SN", section="s")] = "x"


class _UnionBoundsModel(AppFormModel):
    port: Annotated[TcpPort | None, Ui(label="P", section="s")] = None
    name: Annotated[NonEmptyStr | None, Ui(label="N", section="s")] = None
    plain_opt: Annotated[int | None, Ui(label="PO", section="s")] = None
    mixed: Annotated[int | NonEmptyStr | None, Ui(label="MX", section="s")] = None


class _OuterAndMemberBoundsModel(AppFormModel):
    port: Annotated[
        TcpPort | None, Field(ge=_OUTER_GE, le=_OUTER_LE), Ui(label="P", section="s")
    ] = None


class TestGroupedAndUnionBounds:
    """Cover bounds carried in GroupedMetadata containers and union members.

    The bounds scan must flatten ``annotated_types.GroupedMetadata`` (``Interval`` /
    ``Len``) and descend into union members so those bounds reach the derived form
    schema instead of being silently dropped.
    """

    def test_numeric_grouped_metadata_flattened(self) -> None:
        """Surface ``Interval`` ge/le onto the derived IntegerField."""
        field = _fields_by_name(_GroupedBoundsModel)["ranged_int"]
        assert isinstance(field, IntegerField)
        assert field.ge == _RANGED_INT_MIN
        assert field.le == _RANGED_INT_MAX

    def test_string_grouped_metadata_flattened(self) -> None:
        """Surface ``Len`` min/max onto the derived StringField."""
        field = _fields_by_name(_GroupedBoundsModel)["ranged_str"]
        assert isinstance(field, StringField)
        assert field.min_length == _CODE_MIN_LEN
        assert field.max_length == _CODE_MAX_LEN

    def test_string_constraints_pattern_flattened(self) -> None:
        """Surface a ``StringConstraints`` pattern onto the derived StringField.

        ``StringConstraints`` carries its pattern in Pydantic's own metadata marker,
        not in an ``annotated_types`` one, so it reaches the scan by a different route
        than a ``Field(pattern=...)`` declaration.
        """
        field = _fields_by_name(_GroupedBoundsModel)["stripped"]
        assert isinstance(field, StringField)
        assert field.pattern == NON_WHITESPACE_PATTERN
        assert field.min_length == 1

    def test_multiple_grouped_metadata_merged(self) -> None:
        """Merge bounds spread across two GroupedMetadata containers on one field."""
        field = _fields_by_name(_GroupedBoundsModel)["multi_grouped"]
        assert isinstance(field, IntegerField)
        assert field.ge == _MULTI_GE
        assert field.le == _MULTI_LE

    def test_numeric_bound_in_union_member(self) -> None:
        """Descend into ``TcpPort | None`` to expose the port bounds."""
        field = _fields_by_name(_UnionBoundsModel)["port"]
        assert isinstance(field, IntegerField)
        assert field.ge == 1
        assert field.le == _PORT_MAX

    def test_string_bound_in_union_member(self) -> None:
        """Descend into ``NonEmptyStr | None`` to expose min_length."""
        field = _fields_by_name(_UnionBoundsModel)["name"]
        assert isinstance(field, StringField)
        assert field.min_length == 1

    def test_plain_optional_without_bounds_is_unbounded(self) -> None:
        """Leave a bare ``int | None`` unbounded without erroring on the None member."""
        field = _fields_by_name(_UnionBoundsModel)["plain_opt"]
        assert isinstance(field, IntegerField)
        assert field.ge is None
        assert field.le is None

    def test_mismatched_member_bound_does_not_leak(self) -> None:
        """Keep a string member's min_length off an int-based field's bounds."""
        field = _fields_by_name(_UnionBoundsModel)["mixed"]
        assert isinstance(field, IntegerField)
        assert field.ge is None
        assert field.le is None

    def test_outer_bound_wins_over_union_member(self) -> None:
        """Prefer the outer-level bound when both outer and a member carry one."""
        field = _fields_by_name(_OuterAndMemberBoundsModel)["port"]
        assert isinstance(field, IntegerField)
        assert field.ge == _OUTER_GE
        assert field.le == _OUTER_LE


class _LabelModel(AppFormModel):
    oplog_span: Annotated[int, Ui(section="s")] = 0
    custom_span: Annotated[int, Ui(label="Custom", section="s")] = 0
    hostname: Annotated[str, Ui(label="Executor Host", section="s")] = ""


class TestFieldLabelDerivation:
    """Cover derived vs explicit ``Ui`` field labels."""

    def test_derives_label_from_field_name(self) -> None:
        """Title-case the field name when ``Ui.label`` is omitted."""
        assert _fields_by_name(_LabelModel)["oplog_span"].label == "Oplog Span"

    def test_explicit_label_overrides_derivation(self) -> None:
        """Honor an explicit ``Ui.label`` over the derived default."""
        assert _fields_by_name(_LabelModel)["custom_span"].label == "Custom"

    def test_divergent_explicit_label_preserved(self) -> None:
        """Keep an explicit label that diverges from the title-cased field name."""
        assert _fields_by_name(_LabelModel)["hostname"].label == "Executor Host"


class _ChoiceModel(AppFormModel):
    color: Annotated[_Color, Ui(label="C", section="s")] = _Color.red
    mode: Annotated[Literal["a", "b"], Ui(label="M", section="s")] = "a"
    tags: Annotated[list[_Color], Ui(label="T", section="s")] = Field(
        default_factory=list
    )
    swap_drop: Annotated[
        int,
        Choices(((0, "Purge Only"), (1, "Swap & Drop"))),
        Ui(label="Archive Type", section="s"),
    ] = 0


class _ChoiceModelWithOptions(AppFormModel):
    swap_drop: Annotated[
        int,
        Choices(
            (
                Option(
                    value=0,
                    label="Purge Only",
                    disabled=True,
                    disabled_reason="Not available",
                ),
                (1, "Swap & Drop"),
            )
        ),
        Ui(label="Archive Type", section="s"),
    ] = 0


class _BadChoiceModel(AppFormModel):
    kind: Annotated[int, Ui(label="K", section="s", widget=FieldWidget.CHOICE)] = 0


class _BadListModel(AppFormModel):
    tags: Annotated[list[str], Ui(label="T", section="s")] = Field(default_factory=list)


class _BadMultiWidgetModel(AppFormModel):
    pick: Annotated[
        str, Ui(label="P", section="s", widget=FieldWidget.MULTI_CHOICE)
    ] = ""


class TestChoices:
    """Cover explicit and derived choice options."""

    def test_enum_and_literal_become_choice(self) -> None:
        """Derive a ChoiceField from an enum or a ``Literal`` base type."""
        fields = _fields_by_name(_ChoiceModel)
        assert isinstance(fields["color"], ChoiceField)
        assert isinstance(fields["mode"], ChoiceField)

    def test_list_becomes_multichoice(self) -> None:
        """Derive a MultiChoiceField from a ``list`` base type."""
        assert isinstance(_fields_by_name(_ChoiceModel)["tags"], MultiChoiceField)

    def test_explicit_choices_win_and_stringify_values(self) -> None:
        """Use explicit ``Choices`` options and stringify their values."""
        field = _fields_by_name(_ChoiceModel)["swap_drop"]
        assert isinstance(field, ChoiceField)
        assert [(c.value, c.label) for c in field.choices] == [
            ("0", "Purge Only"),
            ("1", "Swap & Drop"),
        ]

    def test_enum_choices_use_identity_labels(self) -> None:
        """Derive identity (value == label) options from a bare enum."""
        field = _fields_by_name(_ChoiceModel)["color"]
        assert [(c.value, c.label) for c in field.choices] == [
            ("red", "red"),
            ("green", "green"),
        ]

    def test_non_enum_scalar_without_choices_errors(self) -> None:
        """Reject a choice widget on a scalar that supplies no derivable options."""
        with pytest.raises(ValueError, match="Choices"):
            derive_form_sections(_BadChoiceModel, _SINGLE_SECTION)

    def test_list_without_derivable_choices_errors(self) -> None:
        """Reject a ``list`` field that resolves to no multi-choice options."""
        with pytest.raises(ValueError, match="multi-choice"):
            derive_form_sections(_BadListModel, _SINGLE_SECTION)

    def test_multi_choice_widget_on_non_list_errors(self) -> None:
        """Reject ``widget=MULTI_CHOICE`` on a scalar (non-list) field."""
        with pytest.raises(ValueError, match="not a list"):
            derive_form_sections(_BadMultiWidgetModel, _SINGLE_SECTION)

    def test_disabled_option_derives_choice_with_disabled_fields(self) -> None:
        """Derive disabled=True and disabled_reason on the Choice for a disabled Option."""
        field = _fields_by_name(_ChoiceModelWithOptions)["swap_drop"]
        assert isinstance(field, ChoiceField)
        choices = field.choices
        assert choices[0].disabled is True
        assert choices[0].disabled_reason == "Not available"

    def test_tuple_option_in_choices_remains_selectable(self) -> None:
        """Derive a selectable Choice with no disabled fields from a bare tuple in Choices."""
        field = _fields_by_name(_ChoiceModelWithOptions)["swap_drop"]
        assert isinstance(field, ChoiceField)
        choices = field.choices
        assert choices[1].disabled is None
        assert choices[1].disabled_reason is None

    @pytest.mark.parametrize("invalid_option", [("single",), ("one", "two", "three")])
    def test_choices_marker_rejects_invalid_tuple_option(
        self, invalid_option: tuple[str, ...]
    ) -> None:
        """Raise on tuple options that do not contain exactly value and label."""
        with pytest.raises(ValueError, match="Choices options"):
            Choices((invalid_option,))

    def test_reason_without_disabled_raises_at_marker_construction(self) -> None:
        """Raise when Option is constructed with disabled_reason but disabled=False."""
        with pytest.raises((ValueError, TypeError), match="disabled"):
            Option(value=0, label="Label", disabled_reason="reason")


class _RefModel(AppFormModel):
    service_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Service", section="s"),
    ]
    schema_id: Annotated[
        int | None,
        SchemaRef(),
        Ui(label="Schema", section="s", depends_on="service_id"),
    ] = None
    table_id: Annotated[
        int | None,
        TableRef(),
        Ui(label="Table", section="s", depends_on="schema_id"),
    ] = None
    hostname: Annotated[str, HostRef(), Ui(label="Host", section="s")]
    plain: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Plain", section="s"),
    ]
    free: Annotated[
        int | str,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], allow_custom=True),
        Ui(label="Free", section="s"),
    ]


class _MultiRefModel(AppFormModel):
    target_mode: Annotated[
        Literal["service", "schema"],
        Ui(label="Target mode", section="s"),
    ] = "service"
    target: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        SchemaRef(),
        Ui(
            label="Target",
            section="s",
            depends_on="other",
            destructive="The selected target is dropped and rebuilt.",
        ),
    ]


class _BadAllowCustomModel(AppFormModel):
    svc: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], allow_custom=True),
        Ui(label="Svc", section="s"),
    ]


class _MultiValueRefModel(AppFormModel):
    services: Annotated[
        list[int],
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], multiple=True),
        Ui(label="Services", section="s"),
    ] = Field(default_factory=list)
    schemas: Annotated[
        list[int],
        SchemaRef(multiple=True),
        Ui(label="Schemas", section="s", depends_on="services"),
    ] = Field(default_factory=list)
    tables: Annotated[
        list[int],
        TableRef(multiple=True),
        Ui(label="Tables", section="s", depends_on="schemas"),
    ] = Field(default_factory=list)
    hosts: Annotated[
        list[str],
        HostRef(multiple=True),
        Ui(label="Hosts", section="s"),
    ] = Field(default_factory=list)


class _MultiSetRefModel(AppFormModel):
    services: Annotated[
        set[int],
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], multiple=True),
        Ui(label="Services", section="s"),
    ] = Field(default_factory=set)


class _MultipleScalarRefModel(AppFormModel):
    service: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], multiple=True),
        Ui(label="Service", section="s"),
    ]


class _ListRefWithoutMultipleModel(AppFormModel):
    services: Annotated[
        list[int],
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Services", section="s"),
    ] = Field(default_factory=list)


class _MultiAllowCustomBadModel(AppFormModel):
    schemas: Annotated[
        list[int],
        SchemaRef(multiple=True, allow_custom=True),
        Ui(label="Schemas", section="s", depends_on="services"),
    ] = Field(default_factory=list)


class _MultiAllowCustomGoodModel(AppFormModel):
    schemas: Annotated[
        list[int | str],
        SchemaRef(multiple=True, allow_custom=True),
        Ui(label="Schemas", section="s", depends_on="services"),
    ] = Field(default_factory=list)


class _MultiRefValueModel(AppFormModel):
    services: Annotated[
        list[int | str],
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL], multiple=True),
        Ui(label="Services", section="s"),
    ] = Field(default_factory=list)


class _HostCascadeModel(AppFormModel):
    service_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Service", section="s"),
    ]
    hostname: Annotated[
        str,
        HostRef(),
        Ui(label="Host", section="s", depends_on="service_id"),
    ]
    plain_host: Annotated[str, HostRef(), Ui(label="Plain Host", section="s")]


class _MultiHostCascadeModel(AppFormModel):
    service_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Service", section="s"),
    ]
    hosts: Annotated[
        list[str],
        HostRef(multiple=True),
        Ui(label="Hosts", section="s", depends_on="service_id"),
    ] = Field(default_factory=list)


class _HostTargetServiceModel(AppFormModel):
    """Cover ``HostRef(target_service=...)`` precedence and omit-when-unset."""

    service_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Service", section="s"),
    ]
    other_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Other", section="s"),
    ]
    explicit_only: Annotated[
        str,
        HostRef(target_service="service_id"),
        Ui(label="Explicit", section="s"),
    ]
    precedence: Annotated[
        str,
        HostRef(target_service="other_id"),
        Ui(label="Precedence", section="s", depends_on="service_id"),
    ]
    plain_host: Annotated[str, HostRef(), Ui(label="Plain Host", section="s")]


class TestReferenceFields:
    """Cover ref markers driving the schema field class and their extras."""

    def test_ref_markers_select_field_classes(self) -> None:
        """Select Service/Schema/Table/Host field classes from the ref markers."""
        fields = _fields_by_name(_RefModel)
        assert isinstance(fields["service_id"], ServiceField)
        assert fields["service_id"].service_types == [ServiceTypeEnum.MYSQL]
        assert isinstance(fields["schema_id"], SchemaField)
        assert fields["schema_id"].depends_on == "service_id"
        assert isinstance(fields["table_id"], TableField)
        assert fields["table_id"].depends_on == "schema_id"
        assert isinstance(fields["hostname"], HostField)
        assert fields["hostname"].depends_on is None

    def test_host_ref_depends_on_emitted_when_set(self) -> None:
        """Emit ``depends_on`` on a HostField when ``Ui(depends_on=...)`` is set."""
        fields = _fields_by_name(_HostCascadeModel)
        assert isinstance(fields["hostname"], HostField)
        assert fields["hostname"].depends_on == "service_id"
        assert "depends_on" not in fields["plain_host"].model_dump(exclude_none=True)
        assert fields["hostname"].model_dump(exclude_none=True)["depends_on"] == (
            "service_id"
        )
        assert fields["hostname"].target_service == "service_id"
        assert "target_service" not in fields["plain_host"].model_dump(
            exclude_none=True
        )

    def test_multi_host_ref_depends_on_emitted_when_set(self) -> None:
        """Emit ``depends_on`` on MultiHostField for wire uniformity (no cascade).

        Cascade auto-select is single-host only; this pins derivation still
        forwards ``Ui(depends_on=...)`` onto the multi-value field.
        """
        fields = _fields_by_name(_MultiHostCascadeModel)
        assert isinstance(fields["hosts"], MultiHostField)
        assert fields["hosts"].depends_on == "service_id"
        assert fields["hosts"].target_service == "service_id"

    def test_host_ref_target_service_explicit_and_omitted(self) -> None:
        """Emit ``target_service`` from the marker; omit it when neither source is set."""
        fields = _fields_by_name(_HostTargetServiceModel)
        assert isinstance(fields["explicit_only"], HostField)
        assert fields["explicit_only"].target_service == "service_id"
        assert fields["explicit_only"].depends_on is None
        assert "depends_on" not in fields["explicit_only"].model_dump(exclude_none=True)
        assert (
            fields["explicit_only"].model_dump(exclude_none=True)["target_service"]
            == "service_id"
        )
        assert "target_service" not in fields["plain_host"].model_dump(
            exclude_none=True
        )

    def test_host_ref_target_service_marker_precedes_depends_on(self) -> None:
        """Prefer ``HostRef(target_service=...)`` over ``Ui(depends_on=...)``."""
        fields = _fields_by_name(_HostTargetServiceModel)
        assert isinstance(fields["precedence"], HostField)
        assert fields["precedence"].depends_on == "service_id"
        assert fields["precedence"].target_service == "other_id"

    def test_host_ref_empty_target_service_does_not_fall_back(self) -> None:
        """Treat only ``None`` as unset — do not fall back on empty string via ``or``.

        An explicit ``target_service=""`` must not silently inherit
        ``Ui(depends_on=...)``. The wire type rejects empty strings
        (``NonEmptyStr``), so derivation fails instead of emitting the
        cascade field name.
        """

        class _EmptyTarget(AppFormModel):
            service_id: Annotated[
                int,
                ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
                Ui(label="Service", section="s"),
            ]
            hostname: Annotated[
                str,
                HostRef(target_service=""),
                Ui(label="Host", section="s", depends_on="service_id"),
            ]

        with pytest.raises(ValidationError):
            derive_form_sections(_EmptyTarget, _SINGLE_SECTION)

    def test_allow_custom_emitted_only_when_true(self) -> None:
        """Emit ``allow_custom`` only when the ref opts in."""
        fields = _fields_by_name(_RefModel)
        assert fields["plain"].allow_custom is None
        assert fields["free"].allow_custom is True

    def test_multiple_ref_markers_derive_one_of_group(self) -> None:
        """Derive a one-of group when a field declares multiple reference markers."""
        sections = derive_form_sections(_MultiRefModel, _SINGLE_SECTION)
        groups = [
            field for field in sections[0].fields if isinstance(field, OneOfGroup)
        ]
        assert len(groups) == 1
        group = groups[0]
        assert group.name == "target"
        assert group.discriminator == "target_mode"
        branches = {branch.value: branch for branch in group.branches}
        assert set(branches) == {"service", "schema"}
        assert isinstance(branches["service"].fields[0], ServiceField)
        assert isinstance(branches["schema"].fields[0], SchemaField)
        assert branches["service"].fields[0].name == "target"
        assert branches["schema"].fields[0].name == "target"
        assert branches["schema"].fields[0].depends_on == "other"
        assert [field.name for field in sections[0].fields] == ["target"]

    def test_multiple_ref_marker_branches_carry_destructive(self) -> None:
        """Publish ``Ui(destructive=...)`` on every branch of a multi-reference one-of."""
        sections = derive_form_sections(_MultiRefModel, _SINGLE_SECTION)
        group = next(
            field for field in sections[0].fields if isinstance(field, OneOfGroup)
        )
        assert {
            branch.value: branch.fields[0].destructive for branch in group.branches
        } == {
            "service": "The selected target is dropped and rebuilt.",
            "schema": "The selected target is dropped and rebuilt.",
        }

    def test_allow_custom_requires_str_in_annotation(self) -> None:
        """Reject allow_custom on a field whose annotation cannot accept str."""
        with pytest.raises(ValueError, match="allow_custom"):
            derive_form_sections(_BadAllowCustomModel, _SINGLE_SECTION)

    def test_multiple_list_derives_multi_ref_field_classes(self) -> None:
        """Select the four multi-value ref field classes from ``multiple=True``."""
        fields = _fields_by_name(_MultiValueRefModel)
        assert isinstance(fields["services"], MultiServiceField)
        assert fields["services"].service_types == [ServiceTypeEnum.MYSQL]
        assert isinstance(fields["schemas"], MultiSchemaField)
        assert fields["schemas"].depends_on == "services"
        assert isinstance(fields["tables"], MultiTableField)
        assert fields["tables"].depends_on == "schemas"
        assert isinstance(fields["hosts"], MultiHostField)

    def test_multiple_set_annotation_derives_multi_ref_field(self) -> None:
        """Derive the same multi-value field from a ``set[...]`` annotation."""
        field = _fields_by_name(_MultiSetRefModel)["services"]
        assert isinstance(field, MultiServiceField)

    def test_multiple_on_scalar_annotation_errors(self) -> None:
        """Reject ``multiple=True`` on a scalar (non-list/set) annotation."""
        with pytest.raises(ValueError, match="multiple=True"):
            derive_form_sections(_MultipleScalarRefModel, _SINGLE_SECTION)

    def test_list_ref_without_multiple_errors(self) -> None:
        """Reject a list/set ref annotation whose marker omits ``multiple=True``."""
        with pytest.raises(ValueError, match="multiple=True"):
            derive_form_sections(_ListRefWithoutMultipleModel, _SINGLE_SECTION)

    def test_multi_allow_custom_requires_str_in_element(self) -> None:
        """Reject multi allow_custom when the element type cannot accept str."""
        with pytest.raises(ValueError, match="allow_custom"):
            derive_form_sections(_MultiAllowCustomBadModel, _SINGLE_SECTION)

    def test_multi_allow_custom_derives_when_element_accepts_str(self) -> None:
        """Derive a multi ref with ``allow_custom`` when the element admits str."""
        field = _fields_by_name(_MultiAllowCustomGoodModel)["schemas"]
        assert isinstance(field, MultiSchemaField)
        assert field.allow_custom is True

    def test_multi_value_one_of_union_errors(self) -> None:
        """Reject ``multiple=True`` inside a multi-marker one-of reference union.

        The runtime rule-synthesis path derives multi-marker one-of groups at
        class-definition time, so the guard fires as the model class is built.
        """
        target = (
            Annotated[
                list[int],
                ServiceRef(service_types=[ServiceTypeEnum.MYSQL], multiple=True),
                SchemaRef(multiple=True),
                Ui(label="Target", section="s", depends_on="other"),
            ],
            Field(default_factory=list),
        )
        with pytest.raises(ValueError, match="multi-value one-of"):
            create_model("_MultiValueOneOfModel", __base__=AppFormModel, target=target)

    def test_multi_ref_model_validates_mixed_list_rejects_scalar(self) -> None:
        """Validate a mixed ``[id, custom]`` list and reject a non-list body value."""
        model = _MultiRefValueModel(services=[1, "custom"])
        assert model.services == [1, "custom"]
        with pytest.raises(ValidationError):
            _MultiRefValueModel(services=5)


class _SourceBySchema(BaseModel):
    mode: Literal["schema"] = "schema"
    source_db_id: Annotated[str, Ui(label="Source Schema", section="s")] = ""


class _SourceByQuery(BaseModel):
    mode: Literal["query"] = "query"
    source_query: Annotated[str, Ui(label="Source Query", section="s")] = ""


class _SourceUnionModel(AppFormModel):
    source: Annotated[
        _SourceBySchema | _SourceByQuery,
        Field(discriminator="mode"),
        Ui(
            label="Source",
            section="s",
            description="Choose how to specify source rows.",
        ),
    ] = Field(default_factory=_SourceBySchema)


class TestOneOfDerivation:
    """Cover discriminated-union and multi-ref one-of derivation."""

    def test_discriminated_union_derives_one_of_group(self) -> None:
        """Map a nested discriminated union to a dotted-path one-of group."""
        sections = derive_form_sections(_SourceUnionModel, _SINGLE_SECTION)
        group = sections[0].fields[0]
        assert isinstance(group, OneOfGroup)
        assert group.name == "source"
        assert group.label == "Source"
        assert group.description == "Choose how to specify source rows."
        assert group.discriminator == "source.mode"
        assert group.default == "schema"
        branches = {branch.value: branch for branch in group.branches}
        assert branches["schema"].fields[0].name == "source.source_db_id"
        assert branches["query"].fields[0].name == "source.source_query"

    def test_runtime_schema_surfaces_one_of_for_union(self) -> None:
        """Include a one-of group in the runtime schema for branch rule synthesis."""
        schema = build_runtime_schema(_SourceUnionModel)
        groups = [f for f in schema.forms[0].fields if isinstance(f, OneOfGroup)]
        assert len(groups) == 1
        assert groups[0].discriminator == "source.mode"

    def test_nested_union_runtime_validation_enforces_branch_exclusivity(self) -> None:
        """Reject inactive-branch values on a nested union write model."""
        stale = _SourceUnionModel.model_construct(
            source=_SourceBySchema(source_db_id="inventory")
        )
        object.__setattr__(stale.source, "source_query", "SELECT 1")
        with pytest.raises(ValidationError):
            _SourceUnionModel.model_validate(stale)

        _SourceUnionModel(source=_SourceBySchema(source_db_id="inventory"))
        _SourceUnionModel(source=_SourceByQuery(source_query="SELECT 1"))


class _ReqDefModel(AppFormModel):
    need: Annotated[str, Ui(label="N", section="s")]
    opt: Annotated[str, Ui(label="O", section="s")] = "x"
    method: Annotated[
        str,
        Choices((("dsn", "DSN"), ("none", "None"))),
        Ui(label="M", section="s", required=True),
    ] = "none"
    flag: Annotated[bool, Ui(label="F", section="s")] = False


class TestRequiredAndDefault:
    """Cover required derivation and default propagation."""

    def test_required_from_missing_default(self) -> None:
        """Derive ``required`` from whether the model field has a default."""
        fields = _fields_by_name(_ReqDefModel)
        assert fields["need"].required is True
        assert fields["opt"].required is False
        assert fields["opt"].default == "x"

    def test_ui_required_override(self) -> None:
        """Honor a ``Ui(required=True)`` override on a field that has a default."""
        field = _fields_by_name(_ReqDefModel)["method"]
        assert field.required is True
        assert field.default == "none"

    def test_bool_default_false_is_kept(self) -> None:
        """Keep a ``False`` default on a bool field (it is not ``None``)."""
        assert _fields_by_name(_ReqDefModel)["flag"].default is False


class _DefaultModel(AppFormModel):
    task_name: Annotated[str, Ui(label="N", section="s")] = ""
    plain: Annotated[str, Ui(label="P", section="s")] = "body-default"
    display: Annotated[str, Ui(label="D", section="s", default="display-default")] = (
        "body-default"
    )
    none_display: Annotated[str, Ui(label="ND", section="s", default=None)] = (
        "body-default"
    )
    excluded: Annotated[bool, Hidden()] = False
    excluded_with_ui: Annotated[bool, Hidden(), Ui(label="X", section="s")] = False


class TestUiDefaultTriState:
    """Cover the tri-state ``Ui(default=...)`` form-display default."""

    def test_unset_default_uses_pydantic_default(self) -> None:
        """Derive the schema default from the model default when ``Ui.default`` is unset."""
        assert _fields_by_name(_DefaultModel)["plain"].default == "body-default"

    def test_value_default_overrides_model_default(self) -> None:
        """Prefer ``Ui.default`` over the model default, leaving the model default intact."""
        assert _fields_by_name(_DefaultModel)["display"].default == "display-default"
        assert _DefaultModel.model_fields["display"].default == "body-default"

    def test_explicit_none_default_sets_schema_default_to_none(self) -> None:
        """Honor ``Ui(default=None)`` as a form default of ``None`` over a value default."""
        assert _fields_by_name(_DefaultModel)["none_display"].default is None
        assert _DefaultModel.model_fields["none_display"].default == "body-default"


class _DestructiveModel(AppFormModel):
    wipe: Annotated[
        bool,
        Ui(label="Wipe", section="s", destructive="Existing rows are dropped."),
    ] = False
    strategy: Annotated[
        str,
        Ui(
            label="Strategy",
            section="s",
            destructive="Choosing this recreates the table from scratch.",
        ),
    ] = ""
    plain: Annotated[bool, Ui(label="Plain", section="s")] = False


class _DestructiveBranchA(BaseModel):
    mode: Literal["a"] = "a"
    a_value: Annotated[str, Ui(label="A", section="s")] = ""


class _DestructiveBranchB(BaseModel):
    mode: Literal["b"] = "b"
    b_value: Annotated[str, Ui(label="B", section="s")] = ""


class TestUiDestructive:
    """Cover the opt-in ``Ui(destructive=...)`` consequence notice."""

    def test_marker_stores_the_consequence_text(self) -> None:
        """Keep the consequence sentence verbatim on the marker."""
        assert Ui(section="s", destructive="Drops tables.").destructive == (
            "Drops tables."
        )

    def test_marker_is_unmarked_by_default(self) -> None:
        """Leave ``destructive`` unset so an ordinary field carries no notice."""
        assert Ui(section="s").destructive is None

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_marker_rejects_a_blank_consequence(self, blank: str) -> None:
        """Reject a marker whose consequence text is empty or whitespace-only."""
        with pytest.raises(ValueError, match="destructive"):
            Ui(section="s", destructive=blank)

    def test_bool_field_derives_the_consequence_text(self) -> None:
        """Publish the notice on a derived ``bool`` field."""
        assert (
            _fields_by_name(_DestructiveModel)["wipe"].destructive
            == "Existing rows are dropped."
        )

    def test_non_bool_field_derives_the_consequence_text(self) -> None:
        """Publish the notice on a derived non-``bool`` field."""
        assert _fields_by_name(_DestructiveModel)["strategy"].destructive == (
            "Choosing this recreates the table from scratch."
        )

    def test_unmarked_field_derives_no_consequence_text(self) -> None:
        """Leave an unmarked field's ``destructive`` at ``None``."""
        assert _fields_by_name(_DestructiveModel)["plain"].destructive is None

    def test_destructive_on_a_discriminated_union_is_rejected(self) -> None:
        """Reject a mark on a discriminated union, which derives a group that cannot carry it.

        A ``OneOfGroup`` is not a ``BaseField``, so it has no ``destructive``
        and the branch leaves carry their own ``Ui``. Deriving silently would
        drop the mark and leave the field looking safe, so the model is refused
        as it is declared — ``AppFormModel`` builds the runtime schema in
        ``__pydantic_init_subclass__``, so the failure lands at class creation.
        """
        with pytest.raises(ValueError, match="destructive"):

            class _DestructiveOneOfModel(AppFormModel):
                source: Annotated[
                    _DestructiveBranchA | _DestructiveBranchB,
                    Field(discriminator="mode"),
                    Ui(
                        label="Source",
                        section="s",
                        destructive="Everything is dropped.",
                    ),
                ] = Field(default_factory=_DestructiveBranchA)


class TestHiddenExclusion:
    """Cover the ``Hidden()`` field-exclusion marker."""

    def test_hidden_field_absent_from_derived_schema(self) -> None:
        """Drop a ``Hidden`` field from the derived form sections."""
        assert "excluded" not in _fields_by_name(_DefaultModel)

    def test_hidden_field_stays_on_create_model(self) -> None:
        """Keep a ``Hidden`` field on the create model for JSON-body validation."""
        assert "excluded" in _DefaultModel.model_fields

    def test_hidden_field_skipped_before_ui_requirement(self) -> None:
        """Skip a ``Hidden`` field before the ``Ui`` requirement, ignoring a paired ``Ui``."""
        assert "excluded_with_ui" not in _fields_by_name(_DefaultModel)

    def test_alert_on_fail_inherited_as_hidden_control(self) -> None:
        """Inherit ``alert_on_fail`` from ``AppFormModel`` as an off-schema Hidden field."""
        assert "alert_on_fail" in _DefaultModel.model_fields
        assert _DefaultModel.model_fields["alert_on_fail"].default is False
        assert "alert_on_fail" not in _fields_by_name(_DefaultModel)


class _GateModel(AppFormModel):
    method: Annotated[
        str, Choices((("dsn", "DSN"), ("none", "None"))), Ui(label="M", section="s")
    ] = "none"
    dsn_table: Annotated[
        str,
        Requires(when=F("method") == "dsn"),
        Forbidden(when=F("method") != "dsn"),
        Ui(label="DSN", section="s"),
    ] = ""
    plain: Annotated[str, Ui(label="P", section="s")] = ""


class TestFieldGates:
    """Cover Requires/Forbidden markers mapping to BaseField gates."""

    def test_requires_and_forbidden_become_gates(self) -> None:
        """Map ``Requires`` / ``Forbidden`` markers onto the field's gate lists."""
        dsn = _fields_by_name(_GateModel)["dsn_table"]
        assert dsn.requires is not None
        assert len(dsn.requires) == 1
        assert dsn.forbidden is not None
        assert len(dsn.forbidden) == 1

    def test_no_gates_leaves_none(self) -> None:
        """Leave gate lists ``None`` for a field with no gate markers."""
        plain = _fields_by_name(_GateModel)["plain"]
        assert plain.requires is None
        assert plain.forbidden is None


class _OrderModel(AppFormModel):
    b: Annotated[str, Ui(label="B", section="two", order=1)] = ""
    a: Annotated[str, Ui(label="A", section="one", order=2)] = ""
    c: Annotated[str, Ui(label="C", section="one", order=1)] = ""


class _SectionForbiddenModel(AppFormModel):
    x: Annotated[str, Ui(label="X", section="gated")] = ""


class _UnknownSectionModel(AppFormModel):
    x: Annotated[str, Ui(label="X", section="missing")] = ""


class _EmptyLayoutModel(AppFormModel):
    x: Annotated[str, Ui(label="X", section="used")] = ""


class TestSectionLayout:
    """Cover grouping, ordering, section forbidden, and layout errors."""

    def test_fields_grouped_and_ordered(self) -> None:
        """Order sections by field first-appearance and fields by ``Ui(order=...)``.

        ``_OrderModel`` declares its first field in section ``two`` though the
        layout lists ``one`` first, so the derived section order follows the model
        (``Two`` then ``One``), not the layout tuple order.
        """
        layout = FormLayout(
            sections=[
                SectionLayout(key="one", title="One"),
                SectionLayout(key="two", title="Two"),
            ]
        )
        sections = derive_form_sections(_OrderModel, layout)
        assert [s.title for s in sections] == ["Two", "One"]
        assert [f.name for f in sections[0].fields] == ["b"]
        assert [f.name for f in sections[1].fields] == ["c", "a"]

    def test_section_forbidden_from_layout(self) -> None:
        """Copy a layout section's ``forbidden`` hide gate onto the FormSection."""
        layout = FormLayout(
            sections=[
                SectionLayout(
                    key="gated",
                    title="Gated",
                    collapsible=True,
                    forbidden=[FieldGate(when=F("x") != "go")],
                ),
            ]
        )
        section = derive_form_sections(_SectionForbiddenModel, layout)[0]
        assert section.collapsible is True
        assert section.forbidden is not None
        assert len(section.forbidden) == 1

    def test_unknown_section_key_errors(self) -> None:
        """Reject a field naming a section absent from the layout."""
        with pytest.raises(ValueError, match="missing"):
            derive_form_sections(
                _UnknownSectionModel,
                FormLayout(sections=[SectionLayout(key="present", title="P")]),
            )

    def test_empty_layout_section_errors(self) -> None:
        """Reject a layout section that no field is assigned to."""
        layout = FormLayout(
            sections=[
                SectionLayout(key="used", title="Used"),
                SectionLayout(key="empty", title="Empty"),
            ]
        )
        with pytest.raises(ValueError, match="empty"):
            derive_form_sections(_EmptyLayoutModel, layout)


class _ScopeModel(AppFormModel):
    a: Annotated[str, Ui(label="A", section="s")] = ""
    b: Annotated[str, Ui(label="B", section="s")] = ""

    __form_rules__: ClassVar[FormRules] = FormRules(
        sections={
            "s": SectionRules(
                fail_when=(FailRule(fail_when=truthy("a"), error_fields=["a"]),),
            ),
        },
        cardinality_rules=(CardinalityRule(fields=["a", "b"], max=1),),
    )


class TestScopeAttribution:
    """Cover that section-tagged vs plugin-level rules serialize to the right scope."""

    def test_rules_attach_to_correct_scope(self) -> None:
        """Attach section rules to the section and untagged rules to the plugin."""
        schema = derive_app_schema(
            _ScopeModel,
            _SINGLE_SECTION,
            name="m",
            display_name="M",
            list_view=_MINIMAL_LIST_VIEW,
        )
        assert schema.forms[0].fail_when is not None
        assert len(schema.forms[0].fail_when) == 1
        assert schema.cardinality_rules is not None
        assert len(schema.cardinality_rules) == 1
        assert schema.fail_when is None


class TestDeriveAppSchemaRelatedApps:
    """Cover ``related_apps`` threading through :func:`derive_app_schema`."""

    def test_related_apps_passed_through(self) -> None:
        """Stamp ``related_apps`` onto the derived schema unchanged."""
        related = [
            RelatedApp(
                app_key="mysql_backups/restore",
                label="Restore",
                route_segment="restores",
            ),
        ]
        schema = derive_app_schema(
            _ScopeModel,
            _SINGLE_SECTION,
            name="mysql_backups",
            display_name="MySQL Backups",
            list_view=_MINIMAL_LIST_VIEW,
            related_apps=related,
        )

        assert schema.related_apps == related


class _RuntimeParityModel(AppFormModel):
    method: Annotated[str, Ui(label="M", section="s")] = "none"
    dsn_table: Annotated[
        str,
        Forbidden(when=F("method") != "dsn"),
        Ui(label="DSN", section="s"),
    ] = ""


class TestRuntimeValidationParity:
    """Cover that the RulePlan built at class definition enforces rules at runtime."""

    def test_forbidden_gate_enforced(self) -> None:
        """Enforce a forbidden gate as model validation at runtime."""
        with pytest.raises(ValidationError):
            _RuntimeParityModel(method="none", dsn_table="present")
        assert _RuntimeParityModel(method="none").dsn_table == ""
        assert _RuntimeParityModel(method="dsn", dsn_table="t").dsn_table == "t"


class _RemoteChoicesModel(AppFormModel):
    cluster: Annotated[str, Ui(label="Cluster", section="s")] = ""
    backup: Annotated[
        str | None,
        RemoteChoices(endpoint="/apps/mysql_restore/backups"),
        Ui(label="Backup", section="s"),
    ] = None
    cascaded: Annotated[
        str | None,
        RemoteChoices(endpoint="/apps/mysql_restore/backups"),
        Ui(label="Cascaded", section="s", depends_on="cluster"),
    ] = None
    custom: Annotated[
        str | None,
        RemoteChoices(endpoint="/apps/mysql_restore/backups", allow_custom=True),
        Ui(label="Custom", section="s"),
    ] = None


class _RemoteChoicesNonStrModel(AppFormModel):
    backup: Annotated[
        int,
        RemoteChoices(endpoint="/apps/x/backups"),
        Ui(label="Backup", section="s"),
    ] = 0


class _RemoteChoicesNonNullableModel(AppFormModel):
    backup: Annotated[
        str,
        RemoteChoices(endpoint="/apps/x/backups"),
        Ui(label="Backup", section="s"),
    ] = ""


class _RemoteChoicesRequiredModel(AppFormModel):
    backup: Annotated[
        str,
        RemoteChoices(endpoint="/apps/x/backups"),
        Ui(label="Backup", section="s"),
    ]


class TestRemoteChoices:
    """Cover the standalone RemoteChoices marker deriving a RemoteChoiceField."""

    def test_plain_remote_choices_derives_remote_choice_field(self) -> None:
        """Derive a RemoteChoiceField with endpoint_url and no optional wire keys."""
        field = _fields_by_name(_RemoteChoicesModel)["backup"]
        assert isinstance(field, RemoteChoiceField)
        assert field.endpoint_url == "/apps/mysql_restore/backups"
        dumped = field.model_dump(exclude_none=True)
        assert "depends_on" not in dumped
        assert "allow_custom" not in dumped

    def test_depends_on_emitted_when_ui_declares_cascade(self) -> None:
        """Emit depends_on when ``Ui(depends_on=...)`` names the parent field."""
        field = _fields_by_name(_RemoteChoicesModel)["cascaded"]
        assert isinstance(field, RemoteChoiceField)
        assert field.depends_on == "cluster"

    def test_allow_custom_emitted_only_when_true(self) -> None:
        """Emit allow_custom only when the marker opts in; else drop it."""
        fields = _fields_by_name(_RemoteChoicesModel)
        assert fields["custom"].allow_custom is True
        assert fields["backup"].allow_custom is None

    def test_non_str_annotation_errors(self) -> None:
        """Reject RemoteChoices on an annotation that cannot accept str."""
        with pytest.raises(ValueError, match="RemoteChoices"):
            derive_form_sections(_RemoteChoicesNonStrModel, _SINGLE_SECTION)

    def test_optional_non_nullable_annotation_errors(self) -> None:
        """Reject an optional RemoteChoices field whose annotation excludes None."""
        with pytest.raises(ValueError, match="does not accept None"):
            derive_form_sections(_RemoteChoicesNonNullableModel, _SINGLE_SECTION)

    def test_required_field_may_keep_a_non_nullable_annotation(self) -> None:
        """Leave a required RemoteChoices field free of the nullability demand."""
        field = _fields_by_name(_RemoteChoicesRequiredModel)["backup"]
        assert isinstance(field, RemoteChoiceField)
        assert field.required is True

    def test_optional_field_accepts_the_null_a_cleared_selector_commits(self) -> None:
        """Validate the None the selector commits on a clear or a cascade reset."""
        model = _RemoteChoicesModel(backup=None, cascaded=None, custom=None)
        assert (model.backup, model.cascaded, model.custom) == (None, None, None)

    def test_empty_endpoint_rejected(self) -> None:
        """Reject an empty (whitespace-only) endpoint at marker construction."""
        with pytest.raises(ValueError, match="endpoint"):
            RemoteChoices(endpoint="   ")


class TestDeriveAppSchemaItemDisplayNames:
    """Cover the record names threading through :func:`derive_app_schema`."""

    def test_record_names_passed_through(self) -> None:
        """Stamp both record names onto the derived schema unchanged."""
        schema = derive_app_schema(
            _ScopeModel,
            _SINGLE_SECTION,
            name="mysql_backups",
            display_name="MySQL Backups",
            list_view=_MINIMAL_LIST_VIEW,
            item_display_name="backup",
            item_display_name_plural="backups",
        )

        assert schema.item_display_name == "backup"
        assert schema.item_display_name_plural == "backups"

    def test_omitted_record_names_default_via_model_fill(self) -> None:
        """Leave the model's fill: both keys fall back to ``display_name`` when omitted."""
        schema = derive_app_schema(
            _ScopeModel,
            _SINGLE_SECTION,
            name="mysql_backups",
            display_name="MySQL Backups",
            list_view=_MINIMAL_LIST_VIEW,
        )

        assert schema.item_display_name == "MySQL Backups"
        assert schema.item_display_name_plural == "MySQL Backups"


# ── Section grouping and parent toggles ──────────────────────────────────────


class _AdvancedLayoutModel(AppFormModel):
    lead: Annotated[str, Ui(label="Lead", section="Task")] = ""
    general: Annotated[str, Ui(label="General", section="General")] = ""
    upload: Annotated[str, Ui(label="Upload", section="Upload")] = ""


_ADVANCED_LAYOUT = FormLayout(
    sections=(
        SectionLayout(key="Task", title="Task"),
        SectionLayout(key="General", title="General", advanced=True),
        SectionLayout(key="Upload", title="Upload", advanced=True),
    )
)


class _ParentedModel(AppFormModel):
    kill: Annotated[bool, Ui(label="Kill", section="s")] = False
    timeout: Annotated[
        int | EmptyStrToNone,
        Ui(label="Timeout", section="s", parent="kill"),
    ] = None


_PARENTED_LAYOUT = FormLayout(sections=(SectionLayout(key="s", title="S"),))

#: An arbitrary in-range value for the parented model's timeout field.
_PARENTED_TIMEOUT = 30


def _one_section_layout() -> FormLayout:
    return FormLayout(sections=(SectionLayout(key="s", title="S"),))


class TestAdvancedSection:
    """Cover SectionLayout.advanced reaching the wire."""

    def test_advanced_copied_onto_the_derived_section(self) -> None:
        """Mark the sections the layout declares advanced, and no others."""
        sections = derive_form_sections(_AdvancedLayoutModel, _ADVANCED_LAYOUT)
        assert [(s.title, s.advanced) for s in sections] == [
            ("Task", False),
            ("General", True),
            ("Upload", True),
        ]

    def test_ordinary_section_defaults_to_not_advanced(self) -> None:
        """Leave the flag false so an unmarked section renders as it always did."""
        sections = derive_form_sections(_ParentedModel, _PARENTED_LAYOUT)
        assert sections[0].advanced is False

    def test_advanced_sections_need_not_be_adjacent(self) -> None:
        """Accept advanced sections split by an ordinary one.

        The renderer collects them wherever they appear rather than requiring a
        run, so nothing here depends on field declaration order — which is what
        a heading-based grouping did require.
        """

        class _Model(AppFormModel):
            first: Annotated[str, Ui(label="First", section="a")] = ""
            middle: Annotated[str, Ui(label="Middle", section="b")] = ""
            last: Annotated[str, Ui(label="Last", section="c")] = ""

        layout = FormLayout(
            sections=(
                SectionLayout(key="a", title="A", advanced=True),
                SectionLayout(key="b", title="B"),
                SectionLayout(key="c", title="C", advanced=True),
            )
        )
        assert [s.advanced for s in derive_form_sections(_Model, layout)] == [
            True,
            False,
            True,
        ]


class TestHelpPlacement:
    """Cover Ui(help_placement=...) reaching the wire."""

    def test_placement_copied_onto_the_derived_field(self) -> None:
        """Carry an explicit placement through to the field."""

        class _Model(AppFormModel):
            a: Annotated[
                str,
                Ui(label="A", section="s", description="x", help_placement="inline"),
            ] = ""
            b: Annotated[
                str,
                Ui(
                    label="B",
                    section="s",
                    description="y",
                    help_placement=HelpPlacement.TOOLTIP,
                ),
            ] = ""

        fields = derive_form_sections(_Model, _one_section_layout())[0].fields
        assert [f.help_placement for f in fields] == [
            HelpPlacement.INLINE,
            HelpPlacement.TOOLTIP,
        ]

    def test_unset_leaves_the_renderer_to_decide(self) -> None:
        """Leave it unset so a route excluding nulls keeps it off the wire."""
        sections = derive_form_sections(_ParentedModel, _PARENTED_LAYOUT)
        assert all(f.help_placement is None for f in sections[0].fields)

    def test_unknown_placement_rejected(self) -> None:
        """Reject a value the renderer has no rule for."""
        with pytest.raises(ValueError, match="is not one of"):
            Ui(label="x", section="s", help_placement="popover")


class TestParentToggle:
    """Cover Ui(parent=...) reaching the wire and its conformance rules."""

    def test_parent_copied_onto_the_derived_field(self) -> None:
        """Copy the pointer onto the field and leave unparented fields unset."""
        sections = derive_form_sections(_ParentedModel, _PARENTED_LAYOUT)
        by_name = {f.name: f for f in sections[0].fields}
        assert by_name["timeout"].parent == "kill"
        assert by_name["kill"].parent is None

    def test_blank_parent_rejected(self) -> None:
        """Reject a pointer that names nothing."""
        with pytest.raises(ValueError, match="must name the sibling bool field"):
            Ui(label="x", section="s", parent="  ")

    def test_non_bool_parent_rejected(self) -> None:
        """Reject a pointer at a field the renderer could not render as a toggle."""

        class _Model(AppFormModel):
            kill: Annotated[str, Ui(label="Kill", section="s")] = ""
            timeout: Annotated[
                int | EmptyStrToNone,
                Ui(label="Timeout", section="s", parent="kill"),
            ] = None

        with pytest.raises(ValueError, match="is not a bool field declared directly"):
            derive_form_sections(_Model, _one_section_layout())

    def test_cross_section_parent_rejected(self) -> None:
        """Reject a pointer the renderer could not nest under, being elsewhere."""

        class _Model(AppFormModel):
            kill: Annotated[bool, Ui(label="Kill", section="a")] = False
            timeout: Annotated[
                int | EmptyStrToNone,
                Ui(label="Timeout", section="b", parent="kill"),
            ] = None

        layout = FormLayout(
            sections=(
                SectionLayout(key="a", title="A"),
                SectionLayout(key="b", title="B"),
            )
        )
        with pytest.raises(ValueError, match="is not a bool field declared directly"):
            derive_form_sections(_Model, layout)

    def test_chained_parent_rejected(self) -> None:
        """Reject a chain, whose cyclic form leaves both toggles inert."""

        class _Model(AppFormModel):
            a: Annotated[bool, Ui(label="A", section="s")] = False
            b: Annotated[
                bool,
                Ui(label="B", section="s", parent="a"),
            ] = False
            c: Annotated[
                int | EmptyStrToNone,
                Ui(label="C", section="s", parent="b"),
            ] = None

        with pytest.raises(ValueError, match="is itself parented"):
            derive_form_sections(_Model, _one_section_layout())

    def test_parent_does_not_change_what_the_server_accepts(self) -> None:
        """Accept a parented field whose toggle is off.

        The pointer is presentation: it tells the renderer where to draw the
        field and when to grey it out, and nothing more. A field that should
        also be rejected in that state says so with its own ``Forbidden``,
        which is a per-field validation decision rather than something the
        pointer implies.
        """
        assert (
            _ParentedModel(kill=False, timeout=_PARENTED_TIMEOUT).timeout
            == _PARENTED_TIMEOUT
        )
        assert (
            _ParentedModel(kill=True, timeout=_PARENTED_TIMEOUT).timeout
            == _PARENTED_TIMEOUT
        )
        assert _ParentedModel(kill=False).timeout is None
