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

"""Unit tests for the plugin schema DSL."""

import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.extensions.apps.framework import schema as schema_module
from app.extensions.apps.framework.rules import CardinalityRule, FailRule, present
from app.extensions.apps.framework.schema import (
    AppEntitySchema,
    AppSchema,
    BoolField,
    Capabilities,
    ChainedPredecessor,
    Choice,
    ChoiceField,
    Column,
    ColumnFormat,
    DateTimeField,
    declared_field_names_from_forms,
    default_columns,
    DerivedTask,
    DetailField,
    DetailHighlightLanguage,
    DetailSection,
    DetailView,
    EXECUTION_HOST_LABEL,
    EXECUTOR_HOST_COLUMN,
    FileField,
    FloatField,
    FormSection,
    HostField,
    IntegerField,
    ITEM_DISPLAY_NAME_KEYS,
    iter_section_fields,
    ListView,
    MultiChoiceField,
    OneOfBranch,
    OneOfGroup,
    RelatedApp,
    RemoteChoiceField,
    SchemaField,
    ScriptPreviewField,
    ServiceField,
    StringField,
    TableField,
    TaskStatusDescriptor,
    TextAreaField,
    YamlField,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum


def _minimal_detail_view() -> DetailView:
    return DetailView(sections=[])


def _minimal_list_view() -> ListView:
    return ListView(columns=[Column(key="id", label="ID")])


def _minimal_entity_schema() -> AppEntitySchema:
    return AppEntitySchema(
        name="things",
        display_name="Things",
        forms=[
            FormSection(
                title="T",
                fields=[StringField(name="title", label="Title", required=True)],
            )
        ],
        list_view=_minimal_list_view(),
    )


_CHECKSUMS_LIKE_SCHEMA = AppSchema(
    name="checksums",
    display_name="Checksums",
    description="Run pt-table-checksum to verify data consistency.",
    task_type="pt-table-checksum",
    forms=[
        FormSection(
            title="Target",
            description="Select the MySQL service to run checksums against.",
            fields=[
                ServiceField(
                    name="serviceId",
                    label="MySQL Service",
                    required=True,
                    service_types=[ServiceTypeEnum.MYSQL],
                ),
                StringField(
                    name="schema",
                    label="Schema",
                    placeholder="Leave empty for all schemas",
                    description="Optionally filter to a specific database schema.",
                ),
            ],
        ),
        FormSection(
            title="Options",
            fields=[
                ChoiceField(
                    name="chunkSize",
                    label="Chunk Size",
                    default="1000",
                    choices=[
                        Choice(label="1,000", value="1000"),
                        Choice(label="5,000", value="5000"),
                    ],
                ),
                BoolField(
                    name="replicateCheck",
                    label="Replicate check",
                    default=True,
                ),
                IntegerField(
                    name="checkInterval",
                    label="Check interval (seconds)",
                    default=1,
                    ge=1,
                    le=3600,
                ),
            ],
        ),
    ],
    capabilities=Capabilities(alert_on_fail=True, scheduling=True),
    list_view=ListView(
        columns=[
            Column(key="id", label="ID", sortable=True),
            Column(key="service", label="Service", format=ColumnFormat.CHIP),
            Column(key="schema", label="Schema", format=ColumnFormat.CODE),
            Column(
                key="status",
                label="Status",
                format=ColumnFormat.STATUS,
                sortable=True,
            ),
            Column(key="differences", label="Differences"),
            Column(
                key="lastRun",
                label="Last Run",
                format=ColumnFormat.RELATIVE,
                sortable=True,
            ),
        ],
        default_sort="-lastRun",
    ),
    detail_view=DetailView(
        sections=[
            DetailSection(
                title="Execution",
                fields=[
                    DetailField(path="data.meta.command", label="Command"),
                    DetailField(path="data.meta.args", label="Args"),
                    DetailField(path="data.meta.target", label="Target"),
                ],
            ),
        ],
    ),
)


# ── Construction ─────────────────────────────────────────────────────────


def test_plugin_schema_constructs_with_minimal_fields():
    """Construct a minimal ``AppSchema`` with no forms and one column."""
    schema = AppSchema(
        name="minimal",
        display_name="Minimal",
        forms=[],
        list_view=_minimal_list_view(),
    )

    assert schema.name == "minimal"
    assert schema.capabilities is None
    assert schema.forms == []


def test_plugin_schema_entities_mode_omits_root_list_view():
    """Construct an ``AppSchema`` with ``entities`` set and no root ``list_view``."""
    entity = _minimal_entity_schema()
    schema = AppSchema(
        name="multi",
        display_name="Multi",
        entities=[entity],
    )
    assert schema.entities is not None
    assert len(schema.entities) == 1
    assert schema.list_view is None


@pytest.mark.parametrize(
    ("conflict_kwargs", "match_pattern"),
    [
        (
            {
                "forms": [
                    FormSection(
                        title="Root",
                        fields=[StringField(name="ignored", label="Ignored")],
                    )
                ],
            },
            r"Root-level forms.*entity-style",
        ),
        (
            {
                "cardinality_rules": [
                    CardinalityRule(when=None, fields=["title"], min=1)
                ]
            },
            r"Root-level cardinality_rules.*entity-style",
        ),
        (
            {
                "fail_when": [
                    FailRule(
                        fail_when=present("title"),
                        error_fields=["title"],
                        message="title must be set",
                    ),
                ],
            },
            r"Root-level fail_when.*entity-style",
        ),
    ],
)
def test_plugin_schema_entities_mode_rejects_root_form_config_key(
    conflict_kwargs: dict[str, Any],
    match_pattern: str,
) -> None:
    """Refuse a single root form-config key on an entity-style ``AppSchema``."""
    entity = _minimal_entity_schema()
    with pytest.raises(ValidationError, match=match_pattern):
        AppSchema(
            name="multi",
            display_name="Multi",
            entities=[entity],
            **conflict_kwargs,
        )


def test_plugin_schema_entities_mode_rejects_all_root_form_keys():
    """Refuse all three root form keys at once and name each in the error."""
    entity = _minimal_entity_schema()
    with pytest.raises(
        ValidationError,
        match=(
            r"Root-level forms, cardinality_rules, fail_when must not be set "
            r"on an entity-style schema.*'things'"
        ),
    ):
        AppSchema(
            name="multi",
            display_name="Multi",
            entities=[entity],
            forms=[
                FormSection(
                    title="Root",
                    fields=[StringField(name="ignored", label="Ignored")],
                )
            ],
            cardinality_rules=[
                CardinalityRule(when=None, fields=["title"], min=1),
            ],
            fail_when=[
                FailRule(
                    fail_when=present("title"),
                    error_fields=["title"],
                    message="title must be set",
                ),
            ],
        )


def test_plugin_schema_entities_mode_duplicate_root_fields_unreachable():
    """Reject root forms before duplicate field names can be reported."""
    entity = _minimal_entity_schema()
    with pytest.raises(ValidationError, match=r"Root-level forms.*entity-style") as exc:
        AppSchema(
            name="x",
            display_name="X",
            entities=[entity],
            forms=[
                FormSection(
                    title="A",
                    fields=[StringField(name="dup", label="D")],
                ),
                FormSection(
                    title="B",
                    fields=[StringField(name="dup", label="D")],
                ),
            ],
        )
    assert "duplicate field name" not in str(exc.value)


def test_plugin_schema_entities_mode_accepts_empty_root_form_config():
    """Accept an entity-style schema whose root form config is empty or omitted."""
    entity = _minimal_entity_schema()
    schema = AppSchema(
        name="multi",
        display_name="Multi",
        entities=[entity],
        forms=[],
    )
    assert schema.forms == []
    assert schema.cardinality_rules is None
    assert schema.fail_when is None


def test_plugin_schema_task_style_root_forms_unaffected():
    """Confirm task-style root forms stay optional and duplicate-checked."""
    empty = AppSchema(
        name="minimal",
        display_name="Minimal",
        forms=[],
        list_view=_minimal_list_view(),
    )
    assert empty.forms == []
    assert empty.entities is None

    with pytest.raises(ValidationError, match="duplicate field name"):
        AppSchema(
            name="dup",
            display_name="Dup",
            forms=[
                FormSection(
                    title="A",
                    fields=[StringField(name="dup", label="D")],
                ),
                FormSection(
                    title="B",
                    fields=[StringField(name="dup", label="D")],
                ),
            ],
            list_view=_minimal_list_view(),
        )


def test_plugin_entity_schema_detail_highlights_round_trip():
    """Round-trip detail highlight hints through snake_case JSON (wire format)."""
    entity = AppEntitySchema.model_validate(
        {
            "name": "things",
            "display_name": "Things",
            "forms": [
                {
                    "title": "T",
                    "fields": [{"name": "title", "label": "Title", "type": "string"}],
                }
            ],
            "list_view": {"columns": [{"key": "title", "label": "Title"}]},
            "detail_highlights": {"ddl": "sql", "metadata": "json"},
        }
    )
    dumped = entity.model_dump(mode="json", by_alias=True)
    assert dumped["detail_highlights"] == {"ddl": "sql", "metadata": "json"}


def test_plugin_entity_schema_rejects_duplicate_field_names():
    """Reject an ``AppEntitySchema`` with duplicate field ``name`` values across sections."""
    with pytest.raises(ValueError, match="duplicate field name"):
        AppEntitySchema(
            name="dup",
            display_name="Dup",
            forms=[
                FormSection(
                    title="A",
                    fields=[StringField(name="x", label="X")],
                ),
                FormSection(
                    title="B",
                    fields=[StringField(name="x", label="X2")],
                ),
            ],
            list_view=_minimal_list_view(),
        )


def test_plugin_schema_constructs_with_all_capabilities_on():
    """Construct an ``AppSchema`` with every capability flag enabled."""
    schema = AppSchema(
        name="caps",
        display_name="Caps",
        forms=[],
        capabilities=Capabilities(
            chaining=True,
            alert_on_fail=True,
            scheduling=True,
            stats=True,
        ),
        list_view=_minimal_list_view(),
    )

    assert schema.capabilities is not None
    assert schema.capabilities.chaining is True
    assert schema.capabilities.alert_on_fail is True
    assert schema.capabilities.scheduling is True
    assert schema.capabilities.stats is True


def test_capabilities_stats_defaults_to_false():
    """Default value of ``stats`` flag must be ``False`` for backward compat."""
    caps = Capabilities()
    assert caps.stats is False


def test_capabilities_stats_accepts_true():
    """``stats=True`` is a valid construction."""
    caps = Capabilities(stats=True)
    assert caps.stats is True


def test_capabilities_serialization_round_trip_includes_stats():
    """``stats`` survives ``model_dump`` / ``model_validate`` round trip."""
    dumped = Capabilities(stats=True).model_dump()
    assert dumped["stats"] is True
    restored = Capabilities.model_validate({"stats": True})
    assert restored.stats is True


def test_capabilities_omitted_stats_in_payload_defaults_false():
    """Payload missing the ``stats`` key validates to ``False``."""
    caps = Capabilities.model_validate({})
    assert caps.stats is False


def test_dipper_schema_stats_capability_defaults_false():
    """Dipper plugin schema must not opt into the stats card."""
    from app.extensions.apps.dipper.schema import dipper_schema  # noqa: PLC0415

    assert dipper_schema.capabilities is not None
    assert dipper_schema.capabilities.stats is False


def test_capabilities_pii_anonymization_defaults_to_false():
    """Default value of ``pii_anonymization`` flag must be ``False`` for backward compat."""
    caps = Capabilities()
    assert caps.pii_anonymization is False


def test_capabilities_pii_anonymization_accepts_true():
    """``pii_anonymization=True`` is a valid construction."""
    caps = Capabilities(pii_anonymization=True)
    assert caps.pii_anonymization is True


def test_capabilities_serialization_round_trip_includes_pii_anonymization():
    """``pii_anonymization`` survives ``model_dump`` / ``model_validate`` round trip."""
    dumped = Capabilities(pii_anonymization=True).model_dump()
    assert dumped["pii_anonymization"] is True
    restored = Capabilities.model_validate({"pii_anonymization": True})
    assert restored.pii_anonymization is True


def test_capabilities_omitted_pii_anonymization_in_payload_defaults_false():
    """Payload missing the ``pii_anonymization`` key validates to ``False``."""
    caps = Capabilities.model_validate({})
    assert caps.pii_anonymization is False


@pytest.mark.parametrize(
    ("field_cls", "field_type", "extra_kwargs"),
    [
        (BoolField, "bool", {}),
        (
            ChoiceField,
            "choice",
            {"choices": [Choice(label="A", value="a")]},
        ),
        (DateTimeField, "datetime", {}),
        (FileField, "file", {}),
        (FloatField, "float", {}),
        (HostField, "host", {}),
        (IntegerField, "integer", {}),
        (
            MultiChoiceField,
            "multi_choice",
            {"choices": [Choice(label="A", value="a")]},
        ),
        (
            RemoteChoiceField,
            "remote_choice",
            {"endpoint_url": "/api/apps/x/backups"},
        ),
        (SchemaField, "schema", {"depends_on": "serviceId"}),
        (
            ScriptPreviewField,
            "script_preview",
            {"endpoint_url": "/api/apps/x/preview"},
        ),
        (
            ServiceField,
            "service",
            {"service_types": [ServiceTypeEnum.MYSQL]},
        ),
        (StringField, "string", {}),
        (TableField, "table", {"depends_on": "schemaId"}),
        (TextAreaField, "textarea", {}),
        (YamlField, "yaml", {}),
    ],
)
def test_each_field_class_constructs_with_defaults(field_cls, field_type, extra_kwargs):
    """Construct every concrete field class with its required defaults."""
    field = field_cls(name="foo", label="Foo", **extra_kwargs)

    assert field.field_type == field_type
    assert field.name == "foo"
    assert field.label == "Foo"
    assert field.required is False
    assert field.description is None
    assert field.default is None


def test_choice_field_rejects_empty_choices():
    """Reject a ``ChoiceField`` declared with an empty ``choices`` list."""
    with pytest.raises(ValidationError, match="at least 1 item"):
        ChoiceField(name="c", label="C", choices=[])


def test_multi_choice_field_rejects_empty_choices():
    """Reject a ``MultiChoiceField`` declared with an empty ``choices`` list."""
    with pytest.raises(ValidationError, match="at least 1 item"):
        MultiChoiceField(name="c", label="C", choices=[])


def test_service_field_service_types_accepts_enum_values():
    """Accept ``ServiceTypeEnum`` members in ``ServiceField.service_types``."""
    field = ServiceField(
        name="svc", label="Service", service_types=[ServiceTypeEnum.MYSQL]
    )

    assert field.service_types == [ServiceTypeEnum.MYSQL]


def test_service_field_service_types_accepts_lowercase_strings():
    """Accept lowercase string values in ``ServiceField.service_types``."""
    field = ServiceField(name="svc", label="Service", service_types=["mysql"])

    assert field.service_types == [ServiceTypeEnum.MYSQL]


@pytest.mark.parametrize(
    "invalid_name",
    [
        "-foo",
        "foo bar",
        "foo!",
        "foo-",
        "",
        "1foo",
        "9",
    ],
)
def test_base_field_name_pattern_rejects_invalid_identifiers(invalid_name):
    """Reject field ``name`` values that violate the identifier pattern."""
    with pytest.raises(ValidationError):
        StringField(name=invalid_name, label="L")


@pytest.mark.parametrize(
    "valid_name",
    [
        "foo",
        "foo_bar",
        "foo-bar",
        "foo_bar_01",
        "a",
    ],
)
def test_base_field_name_pattern_accepts_valid_identifiers(valid_name):
    """Accept field ``name`` values that match the identifier pattern."""
    field = StringField(name=valid_name, label="L")

    assert field.name == valid_name


@pytest.mark.parametrize("invalid_name", ["-foo", "foo bar", "", "1foo", "9"])
def test_plugin_schema_name_pattern_rejects_invalid_identifiers(invalid_name):
    """Reject ``AppSchema.name`` values that violate the identifier pattern."""
    with pytest.raises(ValidationError):
        AppSchema(
            name=invalid_name,
            display_name="X",
            forms=[],
            list_view=_minimal_list_view(),
        )


def test_string_field_min_length_rejects_zero():
    """Reject ``StringField.min_length`` values below one."""
    with pytest.raises(ValidationError):
        StringField(name="x", label="L", min_length=0)


def test_schema_base_model_rejects_unknown_keys():
    """Reject unknown keys in schema inputs to surface typos at validation time."""
    with pytest.raises(ValidationError):
        StringField(name="x", label="L", minLenght=5)


# ── JSON serialisation round-trip ────────────────────────────────────────


def test_plugin_schema_serialises_to_snake_case_json():
    """Serialise ``AppSchema`` to snake_case JSON matching the React contract."""
    dumped = _CHECKSUMS_LIKE_SCHEMA.model_dump(mode="json", by_alias=True)

    assert "display_name" in dumped
    assert "task_type" in dumped
    assert "list_view" in dumped
    assert "default_sort" in dumped["list_view"]
    assert "alert_on_fail" in dumped["capabilities"]
    assert "displayName" not in dumped
    assert "listView" not in dumped

    service_field = dumped["forms"][0]["fields"][0]
    assert service_field["type"] == "service"
    assert "service_types" in service_field
    assert "fieldType" not in service_field
    assert "field_type" not in service_field

    string_field = dumped["forms"][0]["fields"][1]
    assert string_field["type"] == "string"
    assert "min_length" in string_field
    assert "max_length" in string_field


@pytest.mark.parametrize(
    ("field_cls", "discriminator", "extra_kwargs"),
    [
        (BoolField, "bool", {}),
        (
            ChoiceField,
            "choice",
            {"choices": [Choice(label="A", value="a")]},
        ),
        (DateTimeField, "datetime", {}),
        (FileField, "file", {}),
        (FloatField, "float", {}),
        (HostField, "host", {}),
        (IntegerField, "integer", {}),
        (
            MultiChoiceField,
            "multi_choice",
            {"choices": [Choice(label="A", value="a")]},
        ),
        (SchemaField, "schema", {"depends_on": "svc"}),
        (
            ServiceField,
            "service",
            {"service_types": [ServiceTypeEnum.MYSQL]},
        ),
        (StringField, "string", {}),
        (TableField, "table", {"depends_on": "schema"}),
        (TextAreaField, "textarea", {}),
        (YamlField, "yaml", {}),
    ],
)
def test_each_field_type_discriminator_serialises_as_type_key(
    field_cls, discriminator, extra_kwargs
):
    """Serialise every concrete field class with ``type`` as the discriminator key."""
    field = field_cls(name="foo", label="Foo", **extra_kwargs)

    dumped = field.model_dump(mode="json", by_alias=True)

    assert dumped["type"] == discriminator
    assert "fieldType" not in dumped
    assert "field_type" not in dumped


def test_plugin_schema_round_trips_through_json():
    """Serialize ``AppSchema`` through JSON and back into concrete field subclasses."""
    dumped = _CHECKSUMS_LIKE_SCHEMA.model_dump(mode="json", by_alias=True)

    rehydrated = AppSchema.model_validate(dumped)

    assert isinstance(rehydrated.forms[0].fields[0], ServiceField)
    assert isinstance(rehydrated.forms[0].fields[1], StringField)
    assert isinstance(rehydrated.forms[1].fields[0], ChoiceField)
    assert isinstance(rehydrated.forms[1].fields[1], BoolField)
    assert isinstance(rehydrated.forms[1].fields[2], IntegerField)
    assert rehydrated.list_view.default_sort == "-lastRun"


def test_plugin_schema_accepts_snake_case_python_construction():
    """Accept snake_case Python kwargs when constructing an ``AppSchema``."""
    expected_min_length = 3
    schema = AppSchema(
        name="p",
        display_name="P",
        task_type="python",
        forms=[
            FormSection(
                title="T",
                fields=[
                    StringField(name="x", label="X", min_length=expected_min_length),
                ],
            ),
        ],
        list_view=_minimal_list_view(),
        detail_view=_minimal_detail_view(),
    )

    assert schema.display_name == "P"
    assert schema.task_type == "python"
    assert schema.forms[0].fields[0].min_length == expected_min_length


def test_plugin_schema_accepts_snake_case_json_input():
    """Accept snake_case JSON input when validating an ``AppSchema``."""
    expected_min_length = 3
    schema = AppSchema.model_validate(
        {
            "name": "p",
            "display_name": "P",
            "task_type": "python",
            "forms": [
                {
                    "title": "T",
                    "fields": [
                        {
                            "name": "x",
                            "label": "X",
                            "type": "string",
                            "min_length": expected_min_length,
                        },
                    ],
                },
            ],
            "list_view": {"columns": [{"key": "id", "label": "ID"}]},
            "detail_view": {"sections": []},
        },
    )

    assert schema.display_name == "P"
    assert schema.task_type == "python"
    assert isinstance(schema.forms[0].fields[0], StringField)
    assert schema.forms[0].fields[0].min_length == expected_min_length


def test_plugin_schema_rejects_camel_case_json_input():
    """Reject camelCase JSON input — alias generator is intentionally absent."""
    with pytest.raises(ValidationError):
        AppSchema.model_validate(
            {
                "name": "p",
                "displayName": "P",
                "forms": [],
                "listView": {"columns": [{"key": "id", "label": "ID"}]},
            },
        )


def test_column_format_serialises_to_lowercase_string():
    """Serialise ``ColumnFormat`` members to lowercase strings in JSON output."""
    column = Column(key="status", label="Status", format=ColumnFormat.STATUS)

    dumped = column.model_dump(mode="json", by_alias=True)

    assert dumped["format"] == "status"


def test_column_format_actions_serialises_to_lowercase_string():
    """Serialise ``ColumnFormat.ACTIONS`` to ``actions`` in JSON output."""
    column = Column(key="_actions", label="Actions", format=ColumnFormat.ACTIONS)
    dumped = column.model_dump(mode="json", by_alias=True)
    assert dumped["format"] == "actions"


def test_column_format_rejects_unknown_values():
    """Reject ``Column.format`` values that are not ``ColumnFormat`` members."""
    with pytest.raises(ValidationError):
        Column(key="x", label="X", format="nonsense")


def test_column_value_labels_defaults_to_none():
    """Leave ``Column.value_labels`` unset when a plugin declares no labels."""
    assert Column(key="k", label="L").value_labels is None


def test_column_value_labels_round_trips_through_json():
    """Round-trip a populated ``Column.value_labels`` map through JSON output."""
    column = Column(key="backup_type", label="Type", value_labels={"M": "Mydumper"})

    dumped = column.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert dumped["value_labels"] == {"M": "Mydumper"}


def test_column_without_value_labels_omits_the_key():
    """Drop ``value_labels`` from the payload of a column that declares none."""
    dumped = Column(key="k", label="L").model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert dumped["key"] == "k"
    assert "value_labels" not in dumped


def test_detail_field_value_labels_defaults_to_none():
    """Leave ``DetailField.value_labels`` unset when a plugin declares no labels."""
    assert DetailField(path="backup_type", label="Type").value_labels is None


def test_detail_field_value_labels_round_trips_through_json():
    """Round-trip a populated ``DetailField.value_labels`` map through JSON."""
    field = DetailField(
        path="backup_type", label="Type", value_labels={"P": "pgBackRest"}
    )

    dumped = field.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert dumped["value_labels"] == {"P": "pgBackRest"}


def test_detail_field_without_value_labels_omits_the_key():
    """Drop ``value_labels`` from the payload of a detail field declaring none."""
    dumped = DetailField(path="p", label="L").model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert dumped["path"] == "p"
    assert "value_labels" not in dumped


def test_service_field_service_types_round_trip():
    """Round-trip ``ServiceField.service_types`` through JSON back to enum members."""
    field = ServiceField(name="svc", label="S", service_types=[ServiceTypeEnum.MYSQL])

    dumped = field.model_dump(mode="json", by_alias=True)
    assert dumped["service_types"] == ["mysql"]

    rehydrated = ServiceField.model_validate(dumped)
    assert rehydrated.service_types == [ServiceTypeEnum.MYSQL]


# ── Cross-field validators ───────────────────────────────────────────────


def test_plugin_schema_rejects_duplicate_field_names_across_sections():
    """Reject an ``AppSchema`` with duplicate field ``name`` values across sections."""
    with pytest.raises(ValidationError, match="duplicate field name"):
        AppSchema(
            name="p",
            display_name="P",
            forms=[
                FormSection(
                    title="A",
                    fields=[StringField(name="foo", label="F1")],
                ),
                FormSection(
                    title="B",
                    fields=[StringField(name="foo", label="F2")],
                ),
            ],
            list_view=_minimal_list_view(),
        )


def test_plugin_schema_accepts_unique_names_across_sections():
    """Accept an ``AppSchema`` whose field ``name`` values are unique across sections."""
    schema = AppSchema(
        name="p",
        display_name="P",
        forms=[
            FormSection(
                title="A",
                fields=[StringField(name="foo", label="F1")],
            ),
            FormSection(
                title="B",
                fields=[StringField(name="bar", label="F2")],
            ),
        ],
        list_view=_minimal_list_view(),
    )

    assert schema.forms[0].fields[0].name == "foo"
    assert schema.forms[1].fields[0].name == "bar"


def test_list_view_rejects_default_sort_referencing_missing_column():
    """Reject a ``ListView`` whose ``default_sort`` does not match any column key."""
    with pytest.raises(ValidationError, match="references unknown column key"):
        ListView(
            columns=[Column(key="id", label="ID")],
            default_sort="foo",
        )


def test_list_view_accepts_default_sort_with_leading_minus():
    """Accept ``ListView.default_sort`` with a leading ``-`` descending prefix."""
    view = ListView(
        columns=[Column(key="id", label="ID")],
        default_sort="-id",
    )

    assert view.default_sort == "-id"


def test_list_view_accepts_default_sort_without_prefix():
    """Accept ``ListView.default_sort`` without any prefix."""
    view = ListView(
        columns=[Column(key="id", label="ID")],
        default_sort="id",
    )

    assert view.default_sort == "id"


def test_list_view_accepts_missing_default_sort():
    """Accept a ``ListView`` without a ``default_sort`` value."""
    view = ListView(columns=[Column(key="id", label="ID")])

    assert view.default_sort is None


def test_list_view_server_side_query_defaults_to_none():
    """Leave ``server_side_query`` unset so exclude_none keeps it off the wire."""
    view = ListView(columns=[Column(key="id", label="ID")])
    assert view.server_side_query is None
    dumped = view.model_dump(mode="json", exclude_none=True)
    assert "server_side_query" not in dumped


def test_list_view_accepts_server_side_query_true():
    """Accept an opt-in ``server_side_query=True`` and keep it on the wire."""
    view = ListView(
        columns=[Column(key="id", label="ID")],
        server_side_query=True,
    )
    assert view.server_side_query is True
    dumped = view.model_dump(mode="json", exclude_none=True)
    assert dumped["server_side_query"] is True


def test_list_view_rejects_descending_prefix_for_unknown_column():
    """Reject a ``-``-prefixed ``default_sort`` that references a missing column."""
    with pytest.raises(ValidationError, match="references unknown column key"):
        ListView(
            columns=[Column(key="id", label="ID")],
            default_sort="-missing",
        )


def test_list_view_rejects_double_dash_prefix():
    """Reject ``default_sort`` with multiple leading dashes to match the React consumer.

    The React renderer strips exactly one leading ``-`` from ``defaultSort``
    (``replace(/^-/, '')``), so a backend that strips more than one would
    silently disable the default sort at render time.
    """
    with pytest.raises(ValidationError, match="references unknown column key"):
        ListView(
            columns=[Column(key="id", label="ID")],
            default_sort="--id",
        )


class TestDefaultColumns:
    """Cover the shared ``default_columns()`` factory and ``EXECUTOR_HOST_COLUMN``."""

    def test_returns_identity_audit_bookends_in_order(self):
        """Return the fixed head/tail identity columns wrapping the middle slot."""
        columns = default_columns()

        assert [column.key for column in columns] == [
            "name",
            "status",
            "created_at",
            "last_executed_at",
            "created_by",
        ]

    def test_inserts_middle_between_head_and_tail(self):
        """Place ``*middle`` columns between the head and tail bookends, in order."""
        host = Column(key="hostname", label=EXECUTION_HOST_LABEL)
        plugin = Column(key="backup_type", label="Type", format=ColumnFormat.CHIP)

        columns = default_columns(host, plugin)

        assert [column.key for column in columns] == [
            "name",
            "status",
            "hostname",
            "backup_type",
            "created_at",
            "last_executed_at",
            "created_by",
        ]

    def test_bookends_carry_expected_attributes(self):
        """Keep labels, sortability, and formats on the head/tail columns."""
        by_key = {column.key: column for column in default_columns()}

        assert by_key["name"].label == "Name"
        assert by_key["name"].sortable is True
        assert by_key["status"].format == ColumnFormat.STATUS
        assert by_key["created_at"].format == ColumnFormat.RELATIVE
        assert by_key["last_executed_at"].label == "Last Executed"
        assert by_key["last_executed_at"].format == ColumnFormat.RELATIVE
        assert by_key["created_by"].format is None

    def test_returns_independent_instances_each_call(self):
        """Build fresh bookend instances each call so mutation never leaks across views."""
        first = default_columns()
        second = default_columns()

        assert first[0] is not second[0]

        first[0].label = "Mutated"

        assert second[0].label == "Name"

    def test_copies_middle_columns_each_call(self):
        """Copy middle columns so a shared constant never aliases across views."""
        first = default_columns(EXECUTOR_HOST_COLUMN)
        second = default_columns(EXECUTOR_HOST_COLUMN)

        assert first[2].key == "hostname"
        assert first[2] is not EXECUTOR_HOST_COLUMN
        assert first[2] is not second[2]

        first[2].label = "Mutated"

        assert second[2].label == EXECUTION_HOST_LABEL
        assert EXECUTOR_HOST_COLUMN.label == EXECUTION_HOST_LABEL

    def test_executor_host_column_key_and_label(self):
        """Carry the normalized header label on the reusable executor-host constant."""
        assert EXECUTOR_HOST_COLUMN.key == "hostname"
        assert EXECUTOR_HOST_COLUMN.label == EXECUTION_HOST_LABEL


# ── ListView.overview_hidden_fields ──────────────────────────────────────


def test_list_view_overview_hidden_fields_defaults_to_empty():
    """``overview_hidden_fields`` defaults to ``[]`` when omitted."""
    view = ListView(columns=[Column(key="id", label="ID")])

    assert view.overview_hidden_fields == []


def test_list_view_overview_hidden_fields_accepts_non_empty_strings():
    """``overview_hidden_fields`` accepts a list of non-empty strings."""
    view = ListView(
        columns=[Column(key="id", label="ID")],
        overview_hidden_fields=["foo", "bar_baz"],
    )

    assert view.overview_hidden_fields == ["foo", "bar_baz"]


def test_list_view_overview_hidden_fields_rejects_empty_string():
    """``overview_hidden_fields`` rejects entries that are empty strings."""
    with pytest.raises(ValidationError):
        ListView(
            columns=[Column(key="id", label="ID")],
            overview_hidden_fields=[""],
        )


@pytest.mark.parametrize("bad_value", ["", " ", "  ", "\t", "\n"])
def test_list_view_overview_hidden_fields_rejects_blank_entries(bad_value):
    """``overview_hidden_fields`` rejects blank entries (empty, whitespace-only)."""
    with pytest.raises(ValidationError):
        ListView(
            columns=[Column(key="id", label="ID")],
            overview_hidden_fields=[bad_value],
        )


def test_list_view_overview_hidden_fields_round_trip():
    """``overview_hidden_fields`` survives serialisation and ``model_validate``."""
    original = ListView(
        columns=[Column(key="id", label="ID")],
        overview_hidden_fields=["internal_key"],
    )
    dumped = original.model_dump()
    rehydrated = ListView.model_validate(dumped)

    assert rehydrated.overview_hidden_fields == ["internal_key"]


# ── Discriminated-union rejection ────────────────────────────────────────


def test_unknown_field_type_raises_validation_error():
    """Reject a ``FormSection`` field with an unknown ``type`` discriminator."""
    with pytest.raises(ValidationError):
        FormSection.model_validate(
            {
                "title": "T",
                "fields": [{"name": "x", "label": "X", "type": "nonsense"}],
            },
        )


def test_missing_discriminator_raises_validation_error():
    """Reject a ``FormSection`` field missing the ``type`` discriminator key."""
    with pytest.raises(ValidationError):
        FormSection.model_validate(
            {
                "title": "T",
                "fields": [{"name": "x", "label": "X"}],
            },
        )


def test_field_type_mismatch_with_concrete_class_raises():
    """Reject constructing a subclass with a ``field_type`` that does not match its literal."""
    with pytest.raises(ValidationError):
        StringField(name="x", label="X", field_type="integer")


class TestScriptPreviewField:
    """Tests covering the read-only :class:`ScriptPreviewField` field type."""

    def test_minimal_construction_defaults_depends_on_empty_and_language_none(self):
        """Construct a ScriptPreviewField with only the required fields."""
        field = ScriptPreviewField(
            name="preview",
            label="Preview",
            endpoint_url="/api/apps/snippets/x.sh/preview",
        )

        assert field.field_type == "script_preview"
        assert field.endpoint_url == "/api/apps/snippets/x.sh/preview"
        assert field.depends_on == []
        assert field.language is None

    def test_endpoint_url_is_required(self):
        """Reject construction without an endpoint_url."""
        with pytest.raises(ValidationError):
            ScriptPreviewField(name="preview", label="Preview")

    def test_endpoint_url_must_be_non_empty(self):
        """Reject construction with an empty endpoint_url."""
        with pytest.raises(ValidationError):
            ScriptPreviewField(name="preview", label="Preview", endpoint_url="")

    def test_depends_on_accepts_non_empty_strings(self):
        """Accept a depends_on list with one or more sibling field names."""
        field = ScriptPreviewField(
            name="preview",
            label="Preview",
            endpoint_url="/api/apps/x/y",
            depends_on=["snippet_filename", "executor_host"],
        )

        assert field.depends_on == ["snippet_filename", "executor_host"]

    def test_serialises_with_type_alias(self):
        """``model_dump(by_alias=True)`` exposes the discriminator as ``"type"``."""
        field = ScriptPreviewField(
            name="preview",
            label="Preview",
            endpoint_url="/api/apps/x/y",
        )

        dumped = field.model_dump(by_alias=True)
        assert dumped["type"] == "script_preview"
        assert dumped["endpoint_url"] == "/api/apps/x/y"
        assert dumped["depends_on"] == []

    def test_dispatch_via_any_field_discriminator(self):
        """A ScriptPreviewField round-trips through the AnyField union."""
        section = FormSection.model_validate(
            {
                "title": "Execution",
                "fields": [
                    {
                        "type": "script_preview",
                        "name": "preview",
                        "label": "Preview",
                        "endpoint_url": "/api/apps/x/y",
                    },
                ],
            },
        )

        assert isinstance(section.fields[0], ScriptPreviewField)
        assert section.fields[0].endpoint_url == "/api/apps/x/y"


class TestRemoteChoiceField:
    """Cover the :class:`RemoteChoiceField` field type."""

    def test_minimal_construction_omits_optional_wire_keys(self):
        """Construct with only endpoint_url; depends_on/allow_custom default None."""
        field = RemoteChoiceField(
            name="backup",
            label="Backup",
            endpoint_url="/api/apps/mysql_restore/backups",
        )

        assert field.field_type == "remote_choice"
        assert field.endpoint_url == "/api/apps/mysql_restore/backups"
        assert field.depends_on is None
        assert field.allow_custom is None

    def test_endpoint_url_is_required(self):
        """Reject construction without an endpoint_url."""
        with pytest.raises(ValidationError):
            RemoteChoiceField(name="backup", label="Backup")

    def test_endpoint_url_must_be_non_empty(self):
        """Reject construction with an empty endpoint_url."""
        with pytest.raises(ValidationError):
            RemoteChoiceField(name="backup", label="Backup", endpoint_url="")

    def test_optional_keys_absent_from_wire_by_default(self):
        """Drop depends_on/allow_custom from the wire until a plugin opts in."""
        field = RemoteChoiceField(
            name="backup",
            label="Backup",
            endpoint_url="/api/apps/x/backups",
        )

        dumped = field.model_dump(by_alias=True, exclude_none=True)
        assert dumped["type"] == "remote_choice"
        assert "depends_on" not in dumped
        assert "allow_custom" not in dumped

    def test_optional_keys_emitted_when_set(self):
        """Emit depends_on / allow_custom on the wire when opted in."""
        field = RemoteChoiceField(
            name="backup",
            label="Backup",
            endpoint_url="/api/apps/x/backups",
            depends_on="cluster",
            allow_custom=True,
        )

        dumped = field.model_dump(by_alias=True, exclude_none=True)
        assert dumped["depends_on"] == "cluster"
        assert dumped["allow_custom"] is True

    def test_dispatch_via_any_field_discriminator(self):
        """Validate a RemoteChoiceField round-trips through the union discriminator."""
        section = FormSection.model_validate(
            {
                "title": "Restore",
                "fields": [
                    {
                        "type": "remote_choice",
                        "name": "backup",
                        "label": "Backup",
                        "endpoint_url": "/api/apps/x/backups",
                    },
                ],
            },
        )

        assert isinstance(section.fields[0], RemoteChoiceField)
        assert section.fields[0].endpoint_url == "/api/apps/x/backups"


class TestChoiceDisabled:
    """Cover the opt-in ``disabled`` / ``disabled_reason`` flags on ``Choice``."""

    def test_defaults_are_absent_from_the_wire(self) -> None:
        """A plain choice keeps its pre-feature wire shape under ``exclude_none``.

        The discovery endpoint serialises with ``exclude_none=True``; typing the
        flags ``bool | None`` (default ``None``) keeps them out of the payload so
        existing schema snapshots stay byte-identical.
        """
        choice = Choice(label="Purge Only", value="0")

        assert choice.disabled is None
        assert choice.disabled_reason is None
        assert choice.model_dump(exclude_none=True) == {
            "label": "Purge Only",
            "value": "0",
        }

    def test_disabled_choice_serialises_flag_and_reason(self) -> None:
        """An opted-in disabled choice carries both flags on the wire."""
        choice = Choice(
            label="Swap & Drop",
            value="1",
            disabled=True,
            disabled_reason="Not available in the current scope.",
        )

        dumped = choice.model_dump(exclude_none=True)
        assert dumped == {
            "label": "Swap & Drop",
            "value": "1",
            "disabled": True,
            "disabled_reason": "Not available in the current scope.",
        }

    def test_disabled_reason_must_be_non_empty(self) -> None:
        """Reject an empty ``disabled_reason`` string."""
        with pytest.raises(ValidationError):
            Choice(label="A", value="a", disabled=True, disabled_reason="")

    def test_disabled_reason_requires_disabled(self) -> None:
        """Reject a ``disabled_reason`` on a still-selectable option.

        A reason without ``disabled=True`` is an inconsistent wire shape (the
        UI helpers only surface the reason for disabled options), so the model
        rejects it rather than emitting a misleading payload.
        """
        with pytest.raises(ValidationError):
            Choice(label="A", value="a", disabled_reason="Coming soon.")

        with pytest.raises(ValidationError):
            Choice(
                label="A",
                value="a",
                disabled=False,
                disabled_reason="Coming soon.",
            )

    def test_choice_field_round_trips_disabled_choice(self) -> None:
        """A disabled choice survives validation through the ChoiceField union."""
        section = FormSection.model_validate(
            {
                "title": "Options",
                "fields": [
                    {
                        "type": "choice",
                        "name": "swap_drop",
                        "label": "Archive Type",
                        "choices": [
                            {"label": "Purge Only", "value": "0"},
                            {
                                "label": "Swap & Drop",
                                "value": "1",
                                "disabled": True,
                                "disabled_reason": "Coming soon.",
                            },
                        ],
                    },
                ],
            },
        )

        field = section.fields[0]
        assert isinstance(field, ChoiceField)
        assert field.choices[0].disabled is None
        assert field.choices[1].disabled is True
        assert field.choices[1].disabled_reason == "Coming soon."


class TestBaseFieldDestructive:
    """Cover the opt-in ``destructive`` consequence text on ``BaseField``."""

    def test_default_is_absent_from_the_wire(self) -> None:
        """Keep the pre-feature wire shape for an unmarked field under ``exclude_none``.

        The discovery endpoint serialises with ``exclude_none=True``; typing the
        attribute optional (default ``None``) keeps it out of the payload so
        existing schema snapshots stay byte-identical.
        """
        field = BoolField(name="x", label="X")

        assert field.destructive is None
        assert field.model_dump(by_alias=True, exclude_none=True) == {
            "name": "x",
            "label": "X",
            "required": False,
            "type": "bool",
        }

    def test_marked_field_serialises_the_consequence_text(self) -> None:
        """Carry the consequence sentence on the wire for an opted-in field."""
        field = BoolField(
            name="overwrite_tables",
            label="Overwrite tables",
            destructive="Existing tables are dropped.",
        )

        dumped = field.model_dump(by_alias=True, exclude_none=True)

        assert dumped["destructive"] == "Existing tables are dropped."

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_consequence_text_is_rejected(self, blank: str) -> None:
        """Reject a mark carrying no consequence text, whatever the whitespace.

        The ``Ui`` marker guard only sees the model-first DSL path; the apps that
        construct schema fields directly never build a ``Ui``, so the wire model
        has to refuse the half-marked state itself.
        """
        with pytest.raises(ValidationError):
            BoolField(name="x", label="X", destructive=blank)


class TestReferenceFieldAllowCustom:
    """Cover the opt-in ``allow_custom`` flag on the inventory reference fields."""

    @pytest.mark.parametrize(
        ("field_cls", "extra"),
        [
            (SchemaField, {"depends_on": "serviceId"}),
            (TableField, {"depends_on": "schema"}),
            (ServiceField, {"service_types": [ServiceTypeEnum.MYSQL]}),
        ],
    )
    def test_allow_custom_defaults_absent_from_the_wire(self, field_cls, extra) -> None:
        """The flag defaults to ``None`` and is dropped under ``exclude_none``."""
        field = field_cls(name="ref", label="Ref", **extra)

        assert field.allow_custom is None
        assert "allow_custom" not in field.model_dump(exclude_none=True)

    @pytest.mark.parametrize(
        ("field_cls", "extra"),
        [
            (SchemaField, {"depends_on": "serviceId"}),
            (TableField, {"depends_on": "schema"}),
            (ServiceField, {"service_types": [ServiceTypeEnum.MYSQL]}),
        ],
    )
    def test_allow_custom_surfaces_when_enabled(self, field_cls, extra) -> None:
        """An opted-in field carries ``allow_custom: true`` on the wire."""
        field = field_cls(name="ref", label="Ref", allow_custom=True, **extra)

        assert field.allow_custom is True
        assert (
            field.model_dump(exclude_none=True, by_alias=True)["allow_custom"] is True
        )


# ── Conditional-rule primitives (SEP-1071) ──────────────────────────────


from app.extensions.apps.framework.rules import (  # noqa: E402 — group near tests
    F,
    FieldGate,
    truthy,
)


class TestConditionalRulePrimitivesAcceptance:
    """Verify the new declarative primitives are accepted on each scope."""

    def test_basefield_requires_accepts_field_gate_list(self) -> None:
        """Basefield requires accepts field gate list."""
        field = StringField(
            name="dsn_table",
            label="T",
            requires=[FieldGate(when=F("recursion_method") == "dsn")],
        )

        assert field.requires is not None
        assert len(field.requires) == 1

    def test_basefield_forbidden_accepts_field_gate_list(self) -> None:
        """Basefield forbidden accepts field gate list."""
        field = StringField(
            name="where",
            label="W",
            forbidden=[FieldGate(when=F("swap_drop") == "swap_drop")],
        )

        assert field.forbidden is not None
        assert len(field.forbidden) == 1

    def test_basefield_primitives_default_to_none(self) -> None:
        """Basefield primitives default to none."""
        field = StringField(name="x", label="X")

        assert field.requires is None
        assert field.forbidden is None

    def test_form_section_cardinality_rules_and_fail_when_default_to_none(
        self,
    ) -> None:
        """Form section cardinality rules and fail when default to none."""
        section = FormSection(title="S", fields=[StringField(name="x", label="X")])

        assert section.cardinality_rules is None
        assert section.fail_when is None

    def test_form_section_accepts_cardinality_rules(self) -> None:
        """Form section accepts cardinality rules."""
        section = FormSection(
            title="S",
            fields=[StringField(name="x", label="X")],
            cardinality_rules=[
                CardinalityRule(when=None, fields=["x"], min=1),
            ],
        )

        assert section.cardinality_rules is not None
        assert len(section.cardinality_rules) == 1

    def test_plugin_schema_accepts_top_level_fail_when(self) -> None:
        """Check the app schema accepts a top-level ``fail_when``."""
        schema = AppSchema(
            name="t",
            display_name="T",
            forms=[
                FormSection(
                    title="S",
                    fields=[StringField(name="x", label="X")],
                ),
            ],
            list_view=_minimal_list_view(),
            fail_when=[
                FailRule(
                    fail_when=truthy("x"),
                    error_fields=["x"],
                    message="m",
                ),
            ],
        )

        assert schema.fail_when is not None
        assert len(schema.fail_when) == 1

    def test_existing_checksums_like_schema_unchanged(self) -> None:
        """Regression: a schema without the new primitives still validates."""
        # _CHECKSUMS_LIKE_SCHEMA was constructed at module import; here we
        # assert it does NOT carry any of the new primitive keys.
        assert _CHECKSUMS_LIKE_SCHEMA.cardinality_rules is None
        assert _CHECKSUMS_LIKE_SCHEMA.fail_when is None
        for section in _CHECKSUMS_LIKE_SCHEMA.forms:
            assert section.cardinality_rules is None
            assert section.fail_when is None
            for field in section.fields:
                assert field.requires is None
                assert field.forbidden is None


class TestSchemaTier2ReferenceResolution:
    """Verify Tier-2 AppSchema-level field-reference checks fire."""

    def test_unknown_field_in_basefield_requires_rejected(self) -> None:
        """Unknown field in basefield requires rejected."""
        with pytest.raises(ValidationError, match="unknown field 'NOT_THERE'"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(
                                name="x",
                                label="X",
                                requires=[
                                    FieldGate(when=F("NOT_THERE") == "v"),
                                ],
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_basefield_predicate_typo_keeps_descriptive_suffix(self) -> None:
        """Predicate typo in a basefield gate should keep the descriptive suffix.

        Regression: previously, ``allow_implicit_self=True`` suppressed the
        ``"(the rule names a field that does not exist in any form
        section)"`` suffix for *every* unknown name in the set, not just the
        implicit-self target. A typo'd predicate field would lose the
        suffix, making the error message indistinguishable from a
        deliberate self-reference.
        """
        with pytest.raises(
            ValidationError,
            match=(
                r"unknown field 'NOT_THERE' \(the rule names a field "
                r"that does not exist"
            ),
        ):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(
                                name="x",
                                label="X",
                                requires=[
                                    FieldGate(when=F("NOT_THERE") == "v"),
                                ],
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_unknown_field_in_section_cardinality_rejected(self) -> None:
        """Unknown field in section cardinality rejected."""
        with pytest.raises(ValidationError, match="unknown field 'gone'"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[StringField(name="x", label="X")],
                        cardinality_rules=[
                            CardinalityRule(
                                when=None,
                                fields=["gone"],
                                min=1,
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_unknown_field_in_schema_fail_when_rejected(self) -> None:
        """Unknown field in schema fail when rejected."""
        with pytest.raises(ValidationError, match="unknown field 'unknown'"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[StringField(name="x", label="X")],
                    ),
                ],
                list_view=_minimal_list_view(),
                fail_when=[
                    FailRule(
                        fail_when=truthy("unknown"),
                        error_fields=["unknown"],
                    ),
                ],
            )

    def test_cross_section_reference_accepted(self) -> None:
        """Edge case #9 — cross-section references resolve at schema level.

        References span sections; the resolver covers the whole schema tree.
        """
        schema = AppSchema(
            name="t",
            display_name="T",
            forms=[
                FormSection(
                    title="A",
                    fields=[StringField(name="gate", label="G")],
                ),
                FormSection(
                    title="B",
                    fields=[StringField(name="target", label="T")],
                    cardinality_rules=[
                        CardinalityRule(
                            when=truthy("gate"),
                            fields=["target"],
                            min=1,
                        ),
                    ],
                ),
            ],
            list_view=_minimal_list_view(),
        )

        assert schema.forms[1].cardinality_rules is not None

    def test_hyphenated_field_in_rule_rejected(self) -> None:
        """Edge case for the Python-identifier requirement on rule fields."""
        with pytest.raises(ValidationError, match="no hyphens"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(name="hy-phen", label="H"),
                            StringField(
                                name="x",
                                label="X",
                                requires=[
                                    FieldGate(when=truthy("hy-phen")),
                                ],
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_hyphenated_basefield_self_target_rejected(self) -> None:
        """Edge case — implicit self target also enforces the rule."""
        with pytest.raises(ValidationError, match="no hyphens"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(name="recursion_method", label="M"),
                            StringField(
                                name="hy-phen-field",
                                label="H",
                                requires=[
                                    FieldGate(when=F("recursion_method") == "dsn"),
                                ],
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_bracket_indexed_field_in_rule_rejected(self) -> None:
        """Reject a bracket-indexed reference; no form field name carries one.

        Declaring ``items`` does not make ``items[0]`` rule-referenceable.
        """
        with pytest.raises(ValidationError, match=r"unknown field 'items\[0\]'"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            StringField(name="items", label="I"),
                            StringField(
                                name="x",
                                label="X",
                                requires=[
                                    FieldGate(when=F("items[0]") == "v"),
                                ],
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_existing_unique_field_check_still_fires(self) -> None:
        """Edge case #7 — duplicate-name check runs alongside the new resolver."""
        with pytest.raises(ValidationError, match="duplicate field name"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="A",
                        fields=[StringField(name="dup", label="A")],
                    ),
                    FormSection(
                        title="B",
                        fields=[StringField(name="dup", label="B")],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    # ── SEP-1276: FormSection-level visibility gates ────────────────────

    def test_unknown_field_in_section_forbidden_rejected(self) -> None:
        """Section ``forbidden`` referencing a missing field is rejected."""
        with pytest.raises(ValidationError, match="unknown field 'NOT_THERE'"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="Mode",
                        fields=[StringField(name="x", label="X")],
                        forbidden=[FieldGate(when=F("NOT_THERE") == "v")],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_section_forbidden_referencing_own_child_field_accepted(self) -> None:
        """Section gate referencing one of its own fields validates clean."""
        schema = AppSchema(
            name="t",
            display_name="T",
            forms=[
                FormSection(
                    title="Mode",
                    fields=[StringField(name="own", label="O")],
                    forbidden=[FieldGate(when=F("own") == "off")],
                ),
            ],
            list_view=_minimal_list_view(),
        )

        assert schema.forms[0].forbidden is not None

    def test_section_forbidden_referencing_sibling_section_field_accepted(
        self,
    ) -> None:
        """Section gate referencing a field in another section validates clean.

        Mirrors ``BaseField`` cross-section semantics — the resolver walks the
        plugin-wide field-name set, not just the section's own children.
        """
        schema = AppSchema(
            name="t",
            display_name="T",
            forms=[
                FormSection(
                    title="Shared",
                    fields=[StringField(name="backup_type", label="B")],
                ),
                FormSection(
                    title="Mode",
                    fields=[StringField(name="mode_field", label="M")],
                    forbidden=[FieldGate(when=F("backup_type") != "M")],
                ),
            ],
            list_view=_minimal_list_view(),
        )

        assert schema.forms[1].forbidden is not None

    def test_hyphenated_field_in_section_gate_rejected(self) -> None:
        """Section gate referencing a hyphenated field name is rejected.

        Conditional rules must use valid Python identifiers (no hyphens) so
        that ``getattr`` lookups at predicate-eval time succeed.
        """
        with pytest.raises(ValidationError, match="no hyphens"):
            AppSchema(
                name="t",
                display_name="T",
                forms=[
                    FormSection(
                        title="Shared",
                        fields=[StringField(name="hy-phen", label="H")],
                    ),
                    FormSection(
                        title="Mode",
                        fields=[StringField(name="x", label="X")],
                        forbidden=[FieldGate(when=truthy("hy-phen"))],
                    ),
                ],
                list_view=_minimal_list_view(),
            )


# ── DerivedTask primitive and AppSchema.derived ────────────


class TestDerivedTask:
    """Cover the ``DerivedTask`` declarative primitive."""

    def test_derived_task_minimal(self) -> None:
        """Construct a ``DerivedTask`` with only ``name_suffix`` and check defaults."""
        spec = DerivedTask(name_suffix="-dry-run")

        assert spec.name_suffix == "-dry-run"
        assert spec.arg_substitutions is None
        assert spec.payload_substitutions is None
        assert spec.data_overrides is None
        assert spec.parent_link is True

    def test_derived_task_empty_suffix_rejected(self) -> None:
        """Reject an empty ``name_suffix`` via the ``NonEmptyStr`` type."""
        with pytest.raises(ValidationError):
            DerivedTask(name_suffix="")

    def test_derived_task_extra_forbidden(self) -> None:
        """Reject an unknown field on ``DerivedTask`` (``extra="forbid"``)."""
        with pytest.raises(ValidationError):
            DerivedTask.model_validate(
                {"name_suffix": "-x", "unknown": "v"},
            )

    def test_derived_task_round_trip(self) -> None:
        """Round-trip a ``DerivedTask`` losslessly through JSON."""
        spec = DerivedTask(
            name_suffix="-dry-run",
            arg_substitutions={"--execute": "--dry-run"},
            payload_substitutions={"pbm_config": "pbm_logical"},
            data_overrides={"backup_type": "pbm_logical"},
            parent_link=False,
        )
        dumped = spec.model_dump(mode="json")

        assert dumped == {
            "name_suffix": "-dry-run",
            "arg_substitutions": {"--execute": "--dry-run"},
            "payload_substitutions": {"pbm_config": "pbm_logical"},
            "data_overrides": {"backup_type": "pbm_logical"},
            "parent_link": False,
        }
        reparsed = DerivedTask.model_validate(dumped)
        assert reparsed == spec


class TestAppSchemaDerivedField:
    """Cover the new ``derived`` field on ``AppSchema``."""

    def test_defaults_to_none(self) -> None:
        """Confirm ``derived`` is ``None`` by default (BC regression guard)."""
        schema = AppSchema(
            name="minimal",
            display_name="Minimal",
            forms=[],
            list_view=_minimal_list_view(),
        )

        assert schema.derived is None

    def test_with_derived_round_trips_through_json(self) -> None:
        """Round-trip a schema carrying ``derived`` losslessly via JSON."""
        schema = AppSchema(
            name="cascade-demo",
            display_name="Cascade Demo",
            forms=[],
            list_view=_minimal_list_view(),
            derived=[
                DerivedTask(
                    name_suffix="-dry-run",
                    arg_substitutions={"--execute": "--dry-run"},
                ),
            ],
        )
        reparsed = AppSchema.model_validate(schema.model_dump(mode="json"))

        assert reparsed == schema

    def test_existing_unique_field_check_ignores_derived(self) -> None:
        """Verify ``_validate_form_configuration`` still passes when ``derived`` mirrors a field name.

        ``derived[*].name_suffix`` lives outside the form-field namespace, so
        sharing a literal value with a form field name must not trigger the
        duplicate-name check.
        """
        schema = AppSchema(
            name="cascade-demo",
            display_name="Cascade Demo",
            forms=[
                FormSection(
                    title="T",
                    fields=[StringField(name="foo", label="Foo")],
                ),
            ],
            list_view=_minimal_list_view(),
            derived=[DerivedTask(name_suffix="-foo")],
        )

        assert schema.derived is not None

    def test_duplicate_suffixes_rejected(self) -> None:
        """Reject a schema with two ``DerivedTask`` specs sharing a ``name_suffix``."""
        with pytest.raises(
            ValidationError, match="Duplicate derived name_suffix values"
        ):
            AppSchema(
                name="cascade-demo",
                display_name="Cascade Demo",
                forms=[],
                list_view=_minimal_list_view(),
                derived=[
                    DerivedTask(name_suffix="-x"),
                    DerivedTask(name_suffix="-x"),
                ],
            )


# ── RelatedApp primitive and AppSchema.related_apps ────────


class TestRelatedApp:
    """Cover the ``RelatedApp`` declarative primitive."""

    def test_related_app_minimal(self) -> None:
        """Construct a ``RelatedApp`` with the required fields."""
        spec = RelatedApp(
            app_key="mysql_backups/restore",
            label="Restore",
            route_segment="restores",
        )

        assert spec.app_key == "mysql_backups/restore"
        assert spec.label == "Restore"
        assert spec.route_segment == "restores"

    def test_related_app_route_segment_must_be_single_segment(self) -> None:
        """Reject a ``route_segment`` containing slashes."""
        with pytest.raises(ValidationError):
            RelatedApp(
                app_key="mysql_backups/restore",
                label="Restore",
                route_segment="mysql_backups/restores",
            )

    @pytest.mark.parametrize("segment", ["new", "schedule", "task"])
    def test_related_app_route_segment_rejects_reserved_keywords(
        self, segment: str
    ) -> None:
        """Reject ``route_segment`` values that collide with shell routes."""
        with pytest.raises(ValidationError, match=r"route_segment .+ is reserved"):
            RelatedApp(
                app_key="parent/child",
                label="Child",
                route_segment=segment,
            )

    def test_related_app_extra_forbidden(self) -> None:
        """Reject an unknown field on ``RelatedApp`` (``extra="forbid"``)."""
        with pytest.raises(ValidationError):
            RelatedApp(
                app_key="mysql_backups/restore",
                label="Restore",
                route_segment="restores",
                unknown=True,
            )

    def test_serialises_to_snake_case_json(self) -> None:
        """Serialise ``RelatedApp`` to snake_case JSON matching the React contract."""
        dumped = RelatedApp(
            app_key="mysql_backups/restore",
            label="Restore",
            route_segment="restores",
        ).model_dump(mode="json", by_alias=True)

        assert dumped == {
            "app_key": "mysql_backups/restore",
            "label": "Restore",
            "route_segment": "restores",
        }


class TestAppSchemaRelatedAppsField:
    """Cover the ``related_apps`` field on ``AppSchema``."""

    def test_defaults_to_none(self) -> None:
        """Confirm ``related_apps`` is ``None`` by default (BC regression guard)."""
        schema = AppSchema(
            name="minimal",
            display_name="Minimal",
            forms=[],
            list_view=_minimal_list_view(),
        )

        assert schema.related_apps is None

    def test_with_related_apps_round_trips_through_json(self) -> None:
        """Round-trip a schema carrying ``related_apps`` losslessly via JSON."""
        schema = AppSchema(
            name="mysql_backups",
            display_name="MySQL Backups",
            forms=[],
            list_view=_minimal_list_view(),
            related_apps=[
                RelatedApp(
                    app_key="mysql_backups/restore",
                    label="Restore",
                    route_segment="restores",
                ),
            ],
        )
        dumped = schema.model_dump(mode="json", by_alias=True)

        assert dumped["related_apps"] == [
            {
                "app_key": "mysql_backups/restore",
                "label": "Restore",
                "route_segment": "restores",
            },
        ]
        reparsed = AppSchema.model_validate(dumped)

        assert reparsed == schema

    def test_duplicate_route_segments_rejected(self) -> None:
        """Reject a schema with two ``RelatedApp`` specs sharing a ``route_segment``."""
        with pytest.raises(
            ValidationError, match="Duplicate related_apps route_segment values"
        ):
            AppSchema(
                name="demo",
                display_name="Demo",
                forms=[],
                list_view=_minimal_list_view(),
                related_apps=[
                    RelatedApp(
                        app_key="parent/child-a",
                        label="A",
                        route_segment="restores",
                    ),
                    RelatedApp(
                        app_key="parent/child-b",
                        label="B",
                        route_segment="restores",
                    ),
                ],
            )


# ── ChainedPredecessor primitive and AppSchema.predecessors ──


class TestChainedPredecessor:
    """Cover the ``ChainedPredecessor`` declarative primitive."""

    def test_chained_predecessor_minimal(self) -> None:
        """Construct a ``ChainedPredecessor`` with only ``name_suffix`` and check defaults."""
        spec = ChainedPredecessor(name_suffix="-pre-checks")

        assert spec.name_suffix == "-pre-checks"
        assert spec.on_failure == "halt"
        assert spec.parent_link is True

    def test_chained_predecessor_empty_suffix_rejected(self) -> None:
        """Reject an empty ``name_suffix`` via the ``NonEmptyStr`` type."""
        with pytest.raises(ValidationError):
            ChainedPredecessor(name_suffix="")

    def test_chained_predecessor_extra_forbidden(self) -> None:
        """Reject an unknown field on ``ChainedPredecessor`` (``extra="forbid"``)."""
        with pytest.raises(ValidationError):
            ChainedPredecessor.model_validate(
                {"name_suffix": "-x", "unknown": "v"},
            )

    def test_chained_predecessor_on_failure_invalid(self) -> None:
        """Reject a non-Literal ``on_failure`` value."""
        with pytest.raises(ValidationError):
            ChainedPredecessor.model_validate(
                {"name_suffix": "-x", "on_failure": "abort"},
            )

    def test_chained_predecessor_round_trip(self) -> None:
        """Round-trip a ``ChainedPredecessor`` losslessly through JSON."""
        spec = ChainedPredecessor(
            name_suffix="-pre-checks",
            on_failure="continue",
            parent_link=False,
        )
        dumped = spec.model_dump(mode="json")

        assert dumped == {
            "name_suffix": "-pre-checks",
            "on_failure": "continue",
            "parent_link": False,
        }
        reparsed = ChainedPredecessor.model_validate(dumped)
        assert reparsed == spec


class TestAppSchemaPredecessorsField:
    """Cover the new ``predecessors`` field on ``AppSchema``."""

    def test_defaults_to_none(self) -> None:
        """Confirm ``predecessors`` is ``None`` by default (BC regression guard)."""
        schema = AppSchema(
            name="minimal",
            display_name="Minimal",
            forms=[],
            list_view=_minimal_list_view(),
        )

        assert schema.predecessors is None

    def test_empty_list_collapses_to_none(self) -> None:
        """Collapse ``predecessors=[]`` to ``None`` so the contract is single-valued.

        ``cascade_create_predecessors`` rejects an empty input as a
        programmer error; the schema validator preempts that by collapsing
        empty lists at construction time, so plugins can pass either form
        without surprising consumers.
        """
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[],
            list_view=_minimal_list_view(),
            predecessors=[],
        )

        assert schema.predecessors is None

    def test_with_predecessors_round_trips_through_json(self) -> None:
        """Round-trip a schema carrying ``predecessors`` losslessly via JSON."""
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[],
            list_view=_minimal_list_view(),
            predecessors=[
                ChainedPredecessor(name_suffix="-pre-checks", on_failure="halt"),
            ],
        )
        reparsed = AppSchema.model_validate(schema.model_dump(mode="json"))

        assert reparsed == schema

    def test_existing_unique_field_check_ignores_predecessors(self) -> None:
        """Verify ``_validate_form_configuration`` still passes when a predecessor name_suffix mirrors a field name.

        ``predecessors[*].name_suffix`` lives outside the form-field
        namespace, so sharing a literal value with a form field name must
        not trigger the duplicate-name check.
        """
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[
                FormSection(
                    title="T",
                    fields=[StringField(name="foo", label="Foo")],
                ),
            ],
            list_view=_minimal_list_view(),
            predecessors=[ChainedPredecessor(name_suffix="-foo")],
        )

        assert schema.predecessors is not None

    def test_duplicate_suffixes_rejected(self) -> None:
        """Reject a schema with two ``ChainedPredecessor`` specs sharing a ``name_suffix``."""
        with pytest.raises(
            ValidationError,
            match="Duplicate predecessors name_suffix values",
        ):
            AppSchema(
                name="chain-demo",
                display_name="Chain Demo",
                forms=[],
                list_view=_minimal_list_view(),
                predecessors=[
                    ChainedPredecessor(name_suffix="-x"),
                    ChainedPredecessor(name_suffix="-x"),
                ],
            )

    def test_mixed_on_failure_rejected(self) -> None:
        """Reject a predecessors list with mixed ``on_failure`` values."""
        with pytest.raises(ValidationError, match="Mixed on_failure policies"):
            AppSchema(
                name="chain-demo",
                display_name="Chain Demo",
                forms=[],
                list_view=_minimal_list_view(),
                predecessors=[
                    ChainedPredecessor(name_suffix="-a", on_failure="halt"),
                    ChainedPredecessor(name_suffix="-b", on_failure="continue"),
                ],
            )

    def test_consistent_on_failure_accepted(self) -> None:
        """Accept a predecessors list whose entries share an ``on_failure`` value."""
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[],
            list_view=_minimal_list_view(),
            predecessors=[
                ChainedPredecessor(name_suffix="-a", on_failure="continue"),
                ChainedPredecessor(name_suffix="-b", on_failure="continue"),
            ],
        )

        assert schema.predecessors is not None
        assert [p.on_failure for p in schema.predecessors] == ["continue", "continue"]

    def test_single_entry_does_not_trigger_duplicate_error(self) -> None:
        """Pass a single-entry predecessors list through without false-positive duplicate."""
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[],
            list_view=_minimal_list_view(),
            predecessors=[ChainedPredecessor(name_suffix="-x")],
        )

        assert schema.predecessors is not None
        assert len(schema.predecessors) == 1

    def test_shared_suffix_with_derived_is_rejected(self) -> None:
        """Reject ``name_suffix`` shared between ``derived`` and ``predecessors``."""
        with pytest.raises(
            ValidationError,
            match="name_suffix values shared between derived and predecessors",
        ):
            AppSchema(
                name="chain-demo",
                display_name="Chain Demo",
                forms=[],
                list_view=_minimal_list_view(),
                derived=[DerivedTask(name_suffix="-x")],
                predecessors=[ChainedPredecessor(name_suffix="-x")],
            )

    def test_disjoint_suffixes_with_derived_accepted(self) -> None:
        """Accept ``derived`` and ``predecessors`` when their suffixes do not overlap."""
        schema = AppSchema(
            name="chain-demo",
            display_name="Chain Demo",
            forms=[],
            list_view=_minimal_list_view(),
            derived=[DerivedTask(name_suffix="-dry-run")],
            predecessors=[ChainedPredecessor(name_suffix="-pre-checks")],
        )

        assert schema.derived is not None
        assert schema.predecessors is not None


# ── DetailView ──────────────────────────────────────────────────────────


def test_detail_view_round_trip_through_json():
    """Round-trip ``DetailView`` through snake_case JSON with a highlight hint."""
    detail_view = DetailView(
        sections=[
            DetailSection(
                title="Execution",
                fields=[
                    DetailField(
                        path="data.meta.command",
                        label="Command",
                        highlight=DetailHighlightLanguage.SQL,
                    ),
                    DetailField(path="data.meta.args", label="Args"),
                ],
            ),
        ],
    )

    dumped = detail_view.model_dump(mode="json", by_alias=True)

    assert dumped == {
        "sections": [
            {
                "title": "Execution",
                "fields": [
                    {
                        "path": "data.meta.command",
                        "label": "Command",
                        "highlight": "sql",
                        "value_labels": None,
                    },
                    {
                        "path": "data.meta.args",
                        "label": "Args",
                        "highlight": None,
                        "value_labels": None,
                    },
                ],
            },
        ],
    }
    assert DetailView.model_validate(dumped) == detail_view


@pytest.mark.parametrize(
    "valid_path",
    [
        "foo",
        "data.meta.command",
        "data.items[0].name",
        "a[0][1].b",
        "_private.field",
        "x1.y2.z3",
    ],
)
def test_detail_field_path_validator_accepts_valid_paths(valid_path):
    """Accept identifier-shape segments with optional ``[N]`` indices."""
    field = DetailField(path=valid_path, label="L")

    assert field.path == valid_path


@pytest.mark.parametrize(
    "invalid_path",
    [
        ".foo",
        "foo.",
        "..foo",
        "foo..bar",
        "1foo",
        "foo.bar baz",
        "data-meta",
        "[0].foo",
        "foo[].bar",
        "foo.bar[",
        "foo.[0]",
        "foo bar",
    ],
)
def test_detail_field_path_validator_rejects_invalid_paths(invalid_path):
    """Reject non-identifier segments, empty segments, and trailing/leading dots."""
    with pytest.raises(ValidationError):
        DetailField(path=invalid_path, label="L")


@pytest.mark.parametrize(
    "invalid_segment_path",
    [
        "data.123bad",
        "data.ok[0].9bad",
        "data.meta.[0]",
    ],
)
def test_detail_field_path_validator_rejects_invalid_identifier_segments(
    invalid_segment_path,
):
    """Reject dotted paths containing non-identifier segments."""
    with pytest.raises(ValidationError):
        DetailField(path=invalid_segment_path, label="L")


def test_detail_section_accepts_empty_fields_list():
    """Allow a section with no fields (frontend hides it at render time)."""
    section = DetailSection(title="Heading", fields=[])

    assert section.fields == []


def test_plugin_schema_task_type_requires_detail_view():
    """Refuse to construct a task-style plugin without ``detail_view``."""
    with pytest.raises(ValidationError, match="detail_view is required"):
        AppSchema(
            name="task-plugin",
            display_name="Task App",
            task_type="some-root-task",
            forms=[],
            list_view=_minimal_list_view(),
        )


def test_plugin_schema_task_type_with_empty_detail_view_sections_allowed():
    """Allow ``DetailView(sections=[])`` as the opt-out form for task-style plugins."""
    schema = AppSchema(
        name="task-plugin",
        display_name="Task App",
        task_type="some-root-task",
        forms=[],
        list_view=_minimal_list_view(),
        detail_view=DetailView(sections=[]),
    )

    assert schema.detail_view is not None
    assert schema.detail_view.sections == []


def test_plugin_schema_without_task_type_allows_missing_detail_view():
    """Legacy plugins with no ``task_type`` can still omit ``detail_view``."""
    schema = AppSchema(
        name="legacy",
        display_name="Legacy",
        forms=[],
        list_view=_minimal_list_view(),
    )

    assert schema.task_type is None
    assert schema.detail_view is None


def test_plugin_schema_detail_view_round_trips_through_json():
    """Serialize a task-style ``AppSchema`` carrying a populated ``detail_view`` through JSON and back."""
    schema = AppSchema(
        name="task-plugin",
        display_name="Task App",
        task_type="root-task",
        forms=[],
        list_view=_minimal_list_view(),
        detail_view=DetailView(
            sections=[
                DetailSection(
                    title="Execution",
                    fields=[
                        DetailField(path="data.meta.command", label="Command"),
                    ],
                ),
            ],
        ),
    )

    dumped = schema.model_dump(mode="json", by_alias=True)
    rehydrated = AppSchema.model_validate(dumped)

    assert rehydrated.detail_view == schema.detail_view


class TestDetailViewReviewFixes:
    """Cover review fixes layered on top of the initial DetailView landing.

    Groups the section-title uniqueness validator and the
    ``DetailHighlightLanguage`` ↔ frontend literal sync guard.
    """

    def test_detail_view_rejects_duplicate_section_titles(self) -> None:
        """Reject two sections with the same title within one ``DetailView``."""
        with pytest.raises(ValidationError) as exc:
            DetailView(
                sections=[
                    DetailSection(
                        title="Execution",
                        fields=[
                            DetailField(path="data.meta.command", label="Command"),
                        ],
                    ),
                    DetailSection(
                        title="Execution",
                        fields=[
                            DetailField(path="data.meta.args", label="Args"),
                        ],
                    ),
                ],
            )

        assert "Duplicate DetailSection title" in str(exc.value)

    def test_detail_highlight_language_membership_is_synced_with_frontend(
        self,
    ) -> None:
        """Guard ``DetailHighlightLanguage`` membership against silent backend drift.

        Two hand-maintained TypeScript literals mirror this enum, both in
        ``frontend/packages/api/src/types/app-schema.ts``:
        ``DetailField.highlight`` and ``AppEntitySchema.detail_highlights``.
        A third, ``DetailSyntaxLanguage`` in
        ``frontend/packages/framework/src/components/SchemaDrivenApp/detailSyntaxStyles.ts``,
        drives the highlighter itself. All three currently read
        ``'sql' | 'json' | 'bash' | 'yaml'``. If a new enum value is added here
        without updating them, this test fails and forces the author to sync
        every side.
        """
        assert {member.value for member in DetailHighlightLanguage} == {
            "sql",
            "json",
            "bash",
            "yaml",
        }


def _sample_one_of_group() -> OneOfGroup:
    """Return a minimal one-of group for schema tests."""
    return OneOfGroup(
        name="source",
        label="Source",
        description="Choose how to specify source rows.",
        discriminator="source.mode",
        default="schema",
        branches=[
            OneOfBranch(
                value="schema",
                label="Schema & Table",
                fields=[
                    StringField(
                        name="source.source_db_id",
                        label="Source Schema",
                    ),
                ],
            ),
            OneOfBranch(
                value="query",
                label="Custom Query",
                fields=[
                    StringField(
                        name="source.source_query",
                        label="Source Query",
                    ),
                ],
            ),
        ],
    )


class TestOneOfGroup:
    """Tests for the :class:`OneOfGroup` schema primitive."""

    def test_serialises_with_type_discriminator(self) -> None:
        """Serialise a one-of group with ``type`` as the wire discriminator key."""
        group = _sample_one_of_group()
        dumped = group.model_dump(mode="json", by_alias=True)

        assert dumped["type"] == "one_of"
        assert dumped["discriminator"] == "source.mode"
        assert len(dumped["branches"]) == len(group.branches)
        assert dumped["branches"][0]["fields"][0]["type"] == "string"
        assert "field_type" not in dumped

    def test_round_trips_through_form_section(self) -> None:
        """Round-trip a one-of group inside a ``FormSection``."""
        section = FormSection(
            title="Source",
            fields=[_sample_one_of_group()],
        )
        dumped = section.model_dump(mode="json", by_alias=True)
        rehydrated = FormSection.model_validate(dumped)

        field = rehydrated.fields[0]
        assert isinstance(field, OneOfGroup)
        assert field.branches[1].value == "query"

    def testiter_section_fields_expands_branches(self) -> None:
        """Expand one-of branches when iterating section leaf fields."""
        section = FormSection(title="S", fields=[_sample_one_of_group()])
        names = [field.name for field in iter_section_fields(section)]

        assert names == ["source.source_db_id", "source.source_query"]

    def test_declared_field_names_includes_discriminator(self) -> None:
        """Include the discriminator path in declared rule-reference names."""
        section = FormSection(title="S", fields=[_sample_one_of_group()])
        names = declared_field_names_from_forms([section])

        assert "source.mode" in names
        assert "source.source_db_id" in names
        assert "source.source_query" in names

    def test_allows_shared_leaf_name_across_branches(self) -> None:
        """Permit the same leaf name in every branch of one one-of group."""
        group = OneOfGroup(
            name="target",
            label="Target",
            discriminator="target.mode",
            branches=[
                OneOfBranch(
                    value="service",
                    label="Service",
                    fields=[
                        ServiceField(
                            name="target",
                            label="Target",
                            service_types=[ServiceTypeEnum.MYSQL],
                        ),
                    ],
                ),
                OneOfBranch(
                    value="schema",
                    label="Schema",
                    fields=[
                        SchemaField(
                            name="target",
                            label="Target",
                            depends_on="service_id",
                        ),
                    ],
                ),
            ],
        )
        section = FormSection(title="T", fields=[group])
        _ = AppEntitySchema(
            name="e",
            display_name="E",
            forms=[section],
            list_view=_minimal_list_view(),
        )

    def test_rejects_rule_referencing_one_of_group_name(self) -> None:
        """Reject a rule that names a one-of group, hinting at a model_validator."""
        group = _sample_one_of_group()
        with pytest.raises(ValueError, match="one-of group field 'source'"):
            AppSchema(
                name="archives",
                display_name="Archives",
                forms=[FormSection(title="Source", fields=[group])],
                list_view=_minimal_list_view(),
                fail_when=[
                    FailRule(
                        fail_when=present("source"),
                        error_fields=["source"],
                        message="Destination required.",
                    )
                ],
            )

    def test_rejects_duplicate_leaf_outside_one_of_reuse(self) -> None:
        """Reject a leaf name that collides outside one-of branch reuse."""
        group = _sample_one_of_group()
        with pytest.raises(ValueError, match="duplicate field name"):
            AppEntitySchema(
                name="e",
                display_name="E",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            group,
                            StringField(
                                name="source.source_query",
                                label="Collision",
                            ),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_rejects_shared_branch_leaf_reused_outside_one_of(self) -> None:
        """Reject a branch-shared leaf name when reused by another form field."""
        group = OneOfGroup(
            name="target",
            label="Target",
            discriminator="target.mode",
            branches=[
                OneOfBranch(
                    value="service",
                    label="Service",
                    fields=[
                        ServiceField(
                            name="target",
                            label="Target",
                            service_types=[ServiceTypeEnum.MYSQL],
                        ),
                    ],
                ),
                OneOfBranch(
                    value="schema",
                    label="Schema",
                    fields=[
                        SchemaField(
                            name="target",
                            label="Target",
                            depends_on="service_id",
                        ),
                    ],
                ),
            ],
        )
        with pytest.raises(ValueError, match="duplicate field name"):
            AppEntitySchema(
                name="e",
                display_name="E",
                forms=[
                    FormSection(
                        title="S",
                        fields=[
                            group,
                            StringField(name="target", label="Collision"),
                        ],
                    ),
                ],
                list_view=_minimal_list_view(),
            )

    def test_rejects_duplicate_branch_values(self) -> None:
        """Reject two branches that share the same ``value``."""
        with pytest.raises(ValidationError, match="duplicate one_of branch value"):
            OneOfGroup(
                name="g",
                label="G",
                discriminator="g.mode",
                branches=[
                    OneOfBranch(
                        value="a",
                        label="A",
                        fields=[StringField(name="a_field", label="A")],
                    ),
                    OneOfBranch(
                        value="a",
                        label="A again",
                        fields=[StringField(name="b_field", label="B")],
                    ),
                ],
            )

    def test_rejects_default_not_in_branches(self) -> None:
        """Reject a default branch value that does not match any branch."""
        with pytest.raises(ValidationError, match="not a declared branch value"):
            OneOfGroup(
                name="g",
                label="G",
                discriminator="g.mode",
                default="missing",
                branches=[
                    OneOfBranch(
                        value="a",
                        label="A",
                        fields=[StringField(name="a_field", label="A")],
                    ),
                    OneOfBranch(
                        value="b",
                        label="B",
                        fields=[StringField(name="b_field", label="B")],
                    ),
                ],
            )

    def test_rule_ref_validation_accepts_discriminator_path(self) -> None:
        """Accept fail_when rules that reference a one-of discriminator path."""
        group = _sample_one_of_group()
        section = FormSection(
            title="S",
            fields=[group],
            fail_when=[
                FailRule(
                    fail_when=F("source.mode") == "schema",
                    error_fields=["source.source_db_id"],
                )
            ],
        )
        schema = AppSchema(
            name="p",
            display_name="P",
            task_type="t",
            forms=[section],
            list_view=_minimal_list_view(),
            detail_view=_minimal_detail_view(),
        )
        assert schema.forms[0].fields[0].name == "source"


# ── Record display names ────────────────────────────────────────────────


class TestAppSchemaRecordDisplayNames:
    """Cover the singular/plural record names carried beside ``display_name``."""

    def test_both_record_names_default_when_omitted(self) -> None:
        """Fall back both record names to ``display_name`` when neither is supplied."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            list_view=_minimal_list_view(),
        )

        assert schema.item_display_name == "MySQL Backups"
        assert schema.item_display_name_plural == "MySQL Backups"

    def test_supplying_the_singular_derives_the_plural(self) -> None:
        """Default the plural from the resolved singular when it is omitted."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            item_display_name="backup",
            list_view=_minimal_list_view(),
        )

        assert schema.item_display_name == "backup"
        assert schema.item_display_name_plural == "backups"

    def test_supplying_the_plural_leaves_the_singular_defaulted(self) -> None:
        """Default the singular from ``display_name``, never from the plural."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            item_display_name_plural="backups",
            list_view=_minimal_list_view(),
        )

        assert schema.item_display_name == "MySQL Backups"
        assert schema.item_display_name_plural == "backups"

    def test_declared_record_names_are_kept(self) -> None:
        """Keep both record names when the author declares them."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            item_display_name="backup",
            item_display_name_plural="backups",
            list_view=_minimal_list_view(),
        )

        assert schema.item_display_name == "backup"
        assert schema.item_display_name_plural == "backups"

    @pytest.mark.parametrize("model", [AppSchema, AppEntitySchema])
    def test_record_names_stay_required_on_the_wire_schema(
        self, model: type[AppSchema | AppEntitySchema]
    ) -> None:
        """Keep both record names required and default-free in the OpenAPI schema."""
        schema = model.model_json_schema()

        assert set(ITEM_DISPLAY_NAME_KEYS).issubset(schema["required"])
        for key in ITEM_DISPLAY_NAME_KEYS:
            prop = schema["properties"][key]
            assert "default" not in prop
            assert prop.get("type") == "string"
            assert "anyOf" not in prop

    @pytest.mark.parametrize(
        "field_name", ["item_display_name", "item_display_name_plural"]
    )
    def test_empty_record_name_is_rejected(self, field_name: str) -> None:
        """Reject an empty record name; defaulting does not weaken ``NonEmptyStr``."""
        with pytest.raises(ValidationError, match="at least 1 character"):
            AppSchema(
                name="minimal",
                display_name="Minimal",
                list_view=_minimal_list_view(),
                **{field_name: ""},
            )

    def test_dumped_payload_round_trips(self) -> None:
        """Re-validate a dumped payload without the defaulting altering it."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            item_display_name="backup",
            item_display_name_plural="backups",
            list_view=_minimal_list_view(),
        )

        payload = schema.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert payload["item_display_name"] == "backup"
        assert payload["item_display_name_plural"] == "backups"
        assert AppSchema.model_validate(payload) == schema

    @pytest.mark.parametrize("payload", ["garbage", [1, 2], 7, None])
    def test_non_mapping_input_is_reported_not_raised(self, payload: Any) -> None:
        """Report non-mapping input as ``model_type`` rather than raising out of the validator.

        This is what the ``before`` validator's mapping guard buys. Without it the
        ``.get`` calls raise ``AttributeError``, which escapes as an unhandled
        exception instead of a ``ValidationError`` a caller can catch.
        """
        with pytest.raises(ValidationError) as exc_info:
            AppSchema.model_validate(payload)

        assert {error["type"] for error in exc_info.value.errors()} == {"model_type"}

    def test_non_str_singular_is_reported_not_raised(self) -> None:
        """Leave a non-str singular for Pydantic; do not crash the before validator.

        The fill helper reads the raw payload. Without a type gate it would call
        ``pluralize_item_display_name`` on an int and raise ``AttributeError``.
        """
        with pytest.raises(ValidationError) as exc_info:
            AppSchema.model_validate(
                {
                    "name": "minimal",
                    "display_name": "MySQL Backups",
                    "item_display_name": 7,
                    "list_view": _minimal_list_view(),
                }
            )

        assert any(
            error["loc"] == ("item_display_name",) for error in exc_info.value.errors()
        )

    def test_a_read_only_mapping_defaults_like_a_dict(self) -> None:
        """Default from any ``Mapping``, not only ``dict`` — the guard is not type-narrow."""
        payload = MappingProxyType(
            {
                "name": "minimal",
                "display_name": "MySQL Backups",
                "list_view": _minimal_list_view(),
            }
        )

        schema = AppSchema.model_validate(payload)

        assert schema.item_display_name == "MySQL Backups"
        assert schema.item_display_name_plural == "MySQL Backups"

    def test_entity_record_names_default_from_the_entity_display_name(self) -> None:
        """Default both entity record names from its own ``display_name`` when omitted."""
        entity = _minimal_entity_schema()

        assert entity.item_display_name == "Things"
        assert entity.item_display_name_plural == "Things"

    def test_entity_record_names_are_independent_of_the_parent(self) -> None:
        """Keep an entity's declared record names distinct from the app's."""
        entity = AppEntitySchema(
            name="nodes",
            display_name="Nodes",
            item_display_name="node",
            item_display_name_plural="nodes",
            forms=[
                FormSection(
                    title="T",
                    fields=[StringField(name="title", label="Title", required=True)],
                )
            ],
            list_view=_minimal_list_view(),
        )
        schema = AppSchema(
            name="multi",
            display_name="Inventory",
            entities=[entity],
        )

        assert schema.item_display_name == "Inventory"
        assert schema.entities is not None
        assert schema.entities[0].item_display_name == "node"

    def test_explicit_plural_overrides_the_pluraliser(self) -> None:
        """Keep an irregular plural when the author declares it."""
        schema = AppSchema(
            name="minimal",
            display_name="Children",
            item_display_name="child",
            item_display_name_plural="children",
            list_view=_minimal_list_view(),
        )

        assert schema.item_display_name_plural == "children"

    def test_entity_singular_derives_the_plural(self) -> None:
        """Derive an entity's plural from its declared singular."""
        entity = AppEntitySchema(
            name="nodes",
            display_name="Nodes",
            item_display_name="node",
            forms=[
                FormSection(
                    title="T",
                    fields=[StringField(name="title", label="Title", required=True)],
                )
            ],
            list_view=_minimal_list_view(),
        )

        assert entity.item_display_name == "node"
        assert entity.item_display_name_plural == "nodes"

    def test_entity_explicit_plural_overrides_the_pluraliser(self) -> None:
        """Keep an irregular entity plural when the author declares it."""
        entity = AppEntitySchema(
            name="children",
            display_name="Children",
            item_display_name="child",
            item_display_name_plural="children",
            forms=[
                FormSection(
                    title="T",
                    fields=[StringField(name="title", label="Title", required=True)],
                )
            ],
            list_view=_minimal_list_view(),
        )

        assert entity.item_display_name_plural == "children"


class TestPluralizeItemDisplayName:
    """Cover the Django-style pluraliser used when the plural is omitted."""

    @pytest.mark.parametrize(
        ("singular", "expected"),
        [
            ("category", "categories"),
            ("key", "keys"),
            ("box", "boxes"),
            ("buzz", "buzzes"),
            ("watch", "watches"),
            ("dish", "dishes"),
            ("backup", "backups"),
            ("schema change", "schema changes"),
        ],
    )
    def test_pluraliser_branches(self, singular: str, expected: str) -> None:
        """Apply consonant-y, vowel-y, sibilant, default, and multi-word rules."""
        assert schema_module.pluralize_item_display_name(singular) == expected


class TestAppSchemaTaskStatuses:
    """Cover the derived ``task_statuses`` vocabulary published on ``AppSchema``."""

    @staticmethod
    def _task_style_schema() -> AppSchema:
        """Return a task-style schema, the shape that publishes the vocabulary."""
        return AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            list_view=_minimal_list_view(),
        )

    def test_every_enum_member_is_published(self) -> None:
        """Publish exactly one entry per ``TaskHistoryStatusEnum`` member."""
        schema = self._task_style_schema()

        assert schema.task_statuses is not None
        assert [entry.value for entry in schema.task_statuses] == list(
            TaskHistoryStatusEnum
        )

    def test_each_status_carries_its_declared_terminality(self) -> None:
        """Pin every member's terminality independently of ``is_terminal()``.

        Deriving the expectation from the same method the implementation calls
        would pass straight through a flip of that method, and ``lost`` is the
        member such a flip is most likely to reach: it is terminal here but
        excluded by the similarly-named ``is_finished()``.
        """
        schema = self._task_style_schema()

        assert schema.task_statuses is not None
        assert {entry.value: entry.terminal for entry in schema.task_statuses} == {
            TaskHistoryStatusEnum.PENDING: False,
            TaskHistoryStatusEnum.RUNNING: False,
            TaskHistoryStatusEnum.SUCCESS: True,
            TaskHistoryStatusEnum.FAILED: True,
            TaskHistoryStatusEnum.STOPPED: True,
            TaskHistoryStatusEnum.LOST: True,
            TaskHistoryStatusEnum.STALE: True,
            TaskHistoryStatusEnum.UNLAUNCHABLE: True,
        }

    def test_entity_plugins_withhold_the_vocabulary(self) -> None:
        """Leave ``task_statuses`` unset for a plugin declaring entities."""
        schema = AppSchema(
            name="multi",
            display_name="Inventory",
            entities=[_minimal_entity_schema()],
        )

        assert schema.task_statuses is None

    def test_withheld_vocabulary_is_absent_from_the_dump(self) -> None:
        """Drop the key entirely for an entity plugin, matching the wire posture."""
        schema = AppSchema(
            name="multi",
            display_name="Inventory",
            entities=[_minimal_entity_schema()],
        )

        dumped = schema.model_dump(exclude_none=True)

        assert dumped["entities"]
        assert "task_statuses" not in dumped

    def test_schema_round_trips_through_json(self) -> None:
        """Re-validate a dumped task-style schema back into an equal instance."""
        schema = self._task_style_schema()

        assert AppSchema.model_validate(schema.model_dump(mode="json")) == schema

    def test_a_supplied_vocabulary_is_overwritten(self) -> None:
        """Re-derive the vocabulary over a well-formed value a caller supplied."""
        schema = AppSchema(
            name="minimal",
            display_name="MySQL Backups",
            list_view=_minimal_list_view(),
            task_statuses=[
                TaskStatusDescriptor(
                    value=TaskHistoryStatusEnum.PENDING,
                    terminal=True,
                    output_available=True,
                )
            ],
        )

        assert schema.task_statuses is not None
        assert len(schema.task_statuses) == len(TaskHistoryStatusEnum)
        assert {entry.value: entry.terminal for entry in schema.task_statuses} == {
            status: status.is_terminal() for status in TaskHistoryStatusEnum
        }

    def test_a_supplied_malformed_vocabulary_is_rejected(self) -> None:
        """Raise on a malformed supplied value rather than silently re-deriving it.

        The field is declared, so an incoming value is validated before the
        after-validator can overwrite it.
        """
        with pytest.raises(ValidationError):
            AppSchema.model_validate(
                {
                    "name": "minimal",
                    "display_name": "MySQL Backups",
                    "list_view": _minimal_list_view().model_dump(mode="json"),
                    "task_statuses": [{"bogus": 1}],
                }
            )


TS_MIRROR = (
    Path(__file__).resolve().parents[5]
    / "frontend"
    / "packages"
    / "api"
    / "src"
    / "types"
    / "app-schema.ts"
)

_INTERFACE_RE = re.compile(
    r"^(?:export )?interface (?P<name>\w+)(?: extends (?P<base>\w+))?\s*\{"
    r"(?P<body>.*?)^\}",
    re.MULTILINE | re.DOTALL,
)
_PROPERTY_RE = re.compile(r"^\s{2}(?P<name>\w+)\??\s*:", re.MULTILINE)

#: Models whose mirror interface carries a different name. Kept explicit rather
#: than inferred: a rule guessing at ``Column`` -> ``ListColumn`` would also
#: pair anything else that happened to look close.
MIRROR_NAMES = MappingProxyType(
    {
        "Capabilities": "AppCapabilities",
        "Choice": "ChoiceOption",
        "Column": "ListColumn",
    }
)

#: Models the wire format never carries to a browser, with what consumes them
#: instead. ``AppSchema.derived`` and ``.predecessors`` are typed by these two
#: and are absent from the mirror for the same reason.
BACKEND_ONLY_MODELS = MappingProxyType(
    {
        "DerivedTask": "cascade orchestration, consumed by framework.cascade",
        "ChainedPredecessor": "cascade orchestration, consumed by framework.cascade",
    }
)

#: Field names each side carries alone, as ``(python_only, typescript_only)``.
#: Two kinds sit here and they are not equivalent. ``AppSchema``'s pair is
#: deliberate -- both are cascade specs the renderer never sees. The other two
#: are unreconciled drift, recorded so they cannot grow: the mirror is behind on
#: ``AppEntitySchema``, and declares two constraints on ``MultiChoiceField``
#: that no backend field backs.
KNOWN_MIRROR_GAPS = MappingProxyType(
    {
        "AppSchema": (frozenset({"derived", "predecessors"}), frozenset()),
        "AppEntitySchema": (frozenset({"cardinality_rules", "fail_when"}), frozenset()),
        "MultiChoiceField": (frozenset(), frozenset({"min_items", "max_items"})),
    }
)


def _ts_interfaces() -> dict[str, frozenset[str]]:
    """Return each mirror interface's property names, with ``extends`` flattened.

    :return: Interface name to the properties it carries, inherited included.
    """
    text = TS_MIRROR.read_text(encoding="utf-8")
    declared: dict[str, tuple[str | None, set[str]]] = {}
    for match in _INTERFACE_RE.finditer(text):
        body = re.sub(r"/\*.*?\*/", "", match["body"], flags=re.DOTALL)
        body = re.sub(r"//.*", "", body)
        declared[match["name"]] = (match["base"], set(_PROPERTY_RE.findall(body)))

    resolved: dict[str, frozenset[str]] = {}
    for name, (base, properties) in declared.items():
        inherited = set(properties)
        parent = base
        while parent is not None and parent in declared:
            inherited |= declared[parent][1]
            parent = declared[parent][0]
        resolved[name] = frozenset(inherited)
    return resolved


def _wire_names(model: type[BaseModel]) -> frozenset[str]:
    """Return the field names ``model`` serializes under.

    The discriminator is declared as ``field_type`` and ships as ``type``, so
    comparing attribute names against the mirror would disagree on every field
    subclass while the wire format matches exactly.

    :param model: The schema model to read.
    :return: The names a client sees.
    """
    return frozenset(
        info.serialization_alias or info.alias or name
        for name, info in model.model_fields.items()
    )


def _mirrored_models() -> dict[str, type[BaseModel]]:
    """Return every schema model carrying fields, keyed by class name."""
    return {
        name: obj
        for name, obj in vars(schema_module).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.model_fields
    }


class TestFrontendSchemaMirror:
    """Bind ``app-schema.ts`` to the models it mirrors by hand.

    ``frontend/packages/api/specs/extensions.json`` is gated by
    ``test_committed_openapi_specs_are_fresh`` and
    ``frontend/packages/api/src/generated/extensions.ts`` by the frontend workflow's
    ``git diff --exit-code``. ``app-schema.ts`` is gated by neither: it mirrors
    ``app/extensions/apps/framework/schema.py`` by hand, so a backend field is free to
    ship with no client type and nothing says so. ``value_labels`` had to be
    hand-added to ``ListColumn`` and ``DetailField`` for exactly that reason.

    The existing ``DetailHighlightLanguage`` guard pins one enum's values; these
    pin every model's field set.
    """

    def test_every_schema_model_has_a_mirror_or_a_recorded_reason(self) -> None:
        """Refuse a model that is neither mirrored nor declared backend-only.

        This is what stops :data:`MIRROR_NAMES` and :data:`BACKEND_ONLY_MODELS`
        going stale. A new model added to the schema fails here until its author
        either mirrors it or says why it is not mirrored, which is the failure a
        hand-maintained roster otherwise defers indefinitely.
        """
        interfaces = _ts_interfaces()

        unaccounted = sorted(
            name
            for name in _mirrored_models()
            if MIRROR_NAMES.get(name, name) not in interfaces
            and name not in BACKEND_ONLY_MODELS
        )

        assert unaccounted == []

    def test_every_mirrored_model_agrees_with_its_interface(self) -> None:
        """Compare wire field sets, allowing only the recorded gaps."""
        interfaces = _ts_interfaces()
        disagreements: dict[str, tuple[list[str], list[str]]] = {}

        for name, model in sorted(_mirrored_models().items()):
            properties = interfaces.get(MIRROR_NAMES.get(name, name))
            if name in BACKEND_ONLY_MODELS or properties is None:
                continue
            allowed_python, allowed_typescript = KNOWN_MIRROR_GAPS.get(
                name, (frozenset(), frozenset())
            )
            python_only = _wire_names(model) - properties - allowed_python
            typescript_only = properties - _wire_names(model) - allowed_typescript
            if python_only or typescript_only:
                disagreements[name] = (
                    sorted(python_only),
                    sorted(typescript_only),
                )

        assert disagreements == {}

    def test_no_recorded_gap_has_silently_closed(self) -> None:
        """Fail once a recorded gap is fixed, so the allowance is deleted with it.

        A stale entry here is worse than none: it goes on excusing a field that
        agrees, and would excuse the same name if it diverged again later.
        """
        interfaces = _ts_interfaces()
        models = _mirrored_models()
        stale: dict[str, list[str]] = {}

        for name, (allowed_python, allowed_typescript) in KNOWN_MIRROR_GAPS.items():
            properties = interfaces[MIRROR_NAMES.get(name, name)]
            wire = _wire_names(models[name])
            closed = sorted(
                (allowed_python - (wire - properties))
                | (allowed_typescript - (properties - wire))
            )
            if closed:
                stale[name] = closed

        assert stale == {}
