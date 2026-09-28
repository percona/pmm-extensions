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

"""Define AppSchema objects for the Dipper plugin."""

from typing import cast
from urllib.parse import urlencode

from app.extensions.apps.field_names import (
    EXECUTOR_HOST_FIELD_NAME,
    SCRIPT_PREVIEW_FIELD_NAME,
    SUDO_FIELD_NAME,
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
    DetailField,
    DetailSection,
    DetailView,
    EXECUTION_HOST_LABEL,
    FormSection,
    HostField,
    ListView,
    ScriptPreviewField,
    ServiceField,
)
from app.extensions.snippets.config import SnippetSudoOption
from app.extensions.snippets.models.snippet import BaseSnippet
from app.extensions.snippets.schema import field_for
from app.inventory.models import ServiceTypeEnum

_SERVICE_FIELD_NAME = "service_id"
_COLLECTOR_FIELD_NAME = "collector_type"

dipper_schema = AppSchema(
    name="dipper",
    display_name="Collect Diagnostic Data",
    item_display_name="data collection",
    description="Run diagnostic data collection scripts on managed database hosts.",
    forms=[
        FormSection(
            title="Execution",
            fields=[
                ServiceField(
                    name="service_id",
                    label="Database Service",
                    required=True,
                    service_types=[
                        ServiceTypeEnum.MYSQL,
                        ServiceTypeEnum.MONGODB,
                        ServiceTypeEnum.POSTGRESQL,
                        ServiceTypeEnum.VALKEY,
                    ],
                ),
                ChoiceField(
                    name="collector_type",
                    label="Collector Type",
                    required=True,
                    default="environment",
                    choices=[
                        Choice(value="environment", label="Environment"),
                        Choice(value="pmm", label="PMM"),
                    ],
                ),
                HostField(
                    name=EXECUTOR_HOST_FIELD_NAME,
                    label=EXECUTION_HOST_LABEL,
                    required=True,
                ),
            ],
        ),
    ],
    capabilities=Capabilities(scheduling=False, alert_on_fail=False, chaining=False),
    list_view=ListView(
        columns=[
            Column(key="snippet_filename", label="Script", sortable=True),
            Column(key="collector_type", label="Collector", sortable=True),
            Column(key="task_name", label="Task"),
            Column(key="task_id", label="Task ID"),
            Column(key="status", label="Status", format=ColumnFormat.STATUS),
        ],
    ),
    detail_view=DetailView(
        sections=[
            DetailSection(
                title="Execution",
                fields=[
                    DetailField(path="data.meta.command", label="Command"),
                    DetailField(path="data.meta.args", label="Args"),
                    DetailField(path="data.meta.target", label=EXECUTION_HOST_LABEL),
                ],
            ),
        ],
    ),
)


def _choice_field_from_options(
    name: str,
    label: str,
    options: list[str],
    *,
    required: bool,
    description: str | None,
    default: str | None,
) -> AnyField:
    """Build a single-select ``ChoiceField`` from a list of option values.

    The ``default`` is applied only when it is present among ``options`` — a
    ``ChoiceField`` default that is not a valid option renders as no selection.

    :param name: The form-state key for the field.
    :type name: str
    :param label: The human-readable field label.
    :type label: str
    :param options: The option values (also used as labels).
    :type options: list[str]
    :param required: Whether the field is required.
    :type required: bool
    :param description: Optional helper text.
    :type description: str | None
    :param default: The candidate default value, applied only if in ``options``.
    :type default: str | None
    :return: The constructed choice field.
    :rtype: AnyField
    """
    return cast(
        AnyField,
        ChoiceField(
            name=name,
            label=label,
            required=required,
            description=description,
            default=default if default in options else None,
            choices=[Choice(value=option, label=option) for option in options],
        ),
    )


def build_dipper_form_schema(
    script: BaseSnippet,
    service_id: int,
    collector_type: str,
    defaults: dict[str, str] | None = None,
    node_options: list[str] | None = None,
    service_options: list[str] | None = None,
) -> AppSchema:
    """Build the Dipper execution form schema for a selected service/script.

    Parameters marked ``hidden`` are omitted from the schema.

    When ``node_options`` / ``service_options`` are non-empty, the corresponding
    free-text ``node`` / ``service`` fields are rendered as single-select
    :class:`~app.extensions.apps.framework.schema.ChoiceField` dropdowns sourced from
    the configured PMM inventory. Empty option lists keep the original
    ``StringField`` (a ``ChoiceField`` requires at least one option), so the form
    stays usable when PMM is unconfigured or unreachable.

    :param script: The selected snippet whose parameters drive the form fields.
    :type script: BaseSnippet
    :param service_id: The inventory service the form targets.
    :type service_id: int
    :param collector_type: The Dipper collector type for the selected script.
    :type collector_type: str
    :param defaults: Optional per-field default values keyed by field name.
    :type defaults: dict[str, str] | None
    :param node_options: Optional PMM node names rendered as a ``node`` dropdown.
    :type node_options: list[str] | None
    :param service_options: Optional PMM service names rendered as a ``service`` dropdown.
    :type service_options: list[str] | None
    :return: The assembled plugin form schema.
    """
    options_by_field = {"node": node_options, "service": service_options}
    parameter_sections: dict[str, list[AnyField]] = {}
    for parameter in script.validated_parameters.visible_parameters:
        section_title = parameter.group or "Parameters"
        field = field_for(parameter)
        resolved_default = defaults.get(field.name) if defaults else None
        options = options_by_field.get(field.name)
        if options:
            field = _choice_field_from_options(
                field.name,
                field.label,
                options,
                required=field.required,
                description=field.description,
                default=resolved_default,
            )
        elif resolved_default is not None and field.default in (None, ""):
            field.default = resolved_default
        parameter_sections.setdefault(section_title, []).append(field)

    forms = [
        FormSection(title=title, fields=fields)
        for title, fields in parameter_sections.items()
    ]

    execution_fields: list[AnyField] = [
        cast(
            AnyField,
            HostField(
                name=EXECUTOR_HOST_FIELD_NAME,
                label=EXECUTION_HOST_LABEL,
                required=True,
            ),
        ),
    ]
    if script.sudo.is_optional:
        execution_fields.append(
            cast(
                AnyField,
                BoolField(
                    name=SUDO_FIELD_NAME,
                    label="Run with sudo",
                    default=script.sudo.sudo_default,
                    description="Prepend sudo to the interpreter when the script is executed.",
                ),
            )
        )
    elif script.sudo == SnippetSudoOption.ALWAYS:
        execution_fields.append(
            cast(
                AnyField,
                BoolField(
                    name=SUDO_FIELD_NAME,
                    label="Run with sudo",
                    default=True,
                    description="This script is configured to always run with sudo.",
                ),
            )
        )
    preview_query = urlencode(
        {"service_id": service_id, "collector_type": collector_type}
    )
    execution_fields.append(
        cast(
            AnyField,
            ScriptPreviewField(
                name=SCRIPT_PREVIEW_FIELD_NAME,
                label="Script preview",
                endpoint_url=f"/apps/dipper/script-preview?{preview_query}",
            ),
        )
    )
    forms.append(FormSection(title="Execution", fields=execution_fields))

    return AppSchema(
        name="dipper",
        display_name=script.title,
        item_display_name=dipper_schema.item_display_name,
        item_display_name_plural=dipper_schema.item_display_name_plural,
        description=script.description or dipper_schema.description,
        forms=forms,
        capabilities=dipper_schema.capabilities,
        list_view=dipper_schema.list_view,
        detail_view=dipper_schema.detail_view,
    )
