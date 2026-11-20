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

"""Define ``AppFormModel`` and ``TaskFormModel``, the single-declaration bases for model-first plugins."""

from typing import Annotated, Any, ClassVar

from pydantic import ConfigDict

from app.core.utils.fields import NonEmptyStr
from app.extensions.apps.framework.form_dsl.derivation import build_runtime_schema
from app.extensions.apps.framework.form_dsl.markers import (
    FormRules,
    Hidden,
    HostRef,
    Ui,
)
from app.extensions.apps.framework.rules import (
    apply_conditional_rules,
    ConditionalRulesModel,
)
from app.extensions.apps.labels import EXECUTION_HOST_LABEL

__all__ = ["AppFormModel", "TaskFormModel"]


class AppFormModel(ConditionalRulesModel):
    """Serve as the single field declaration for a model-first plugin create form.

    A subclass declares each form field once, as a Pydantic field whose
    ``Annotated[...]`` carries the DSL markers (``Ui`` plus optional ``Ref`` /
    ``Choices`` / ``Requires`` / ``Forbidden``). That one declaration drives both
    runtime validation and schema export: at class definition the conditional
    rules (field gates plus the model's ``__form_rules__``) are extracted into the
    inherited :attr:`~app.extensions.apps.framework.rules.ConditionalRulesModel.\
__conditional_rules_plan__` so the inherited validator enforces them, while
    :func:`~app.extensions.apps.framework.form_dsl.derivation.derive_form_sections`
    derives the wire schema from the same fields.

    A body key the model does not declare is rejected (``extra="forbid"``), so a
    create or update that names a setting the form does not have answers ``422``
    naming the key instead of saving the task without it. Code that validates a
    *stored* form rather than a request passes ``extra="ignore"`` to
    ``model_validate``, so a key a form has since retired does not make the
    stored task unreadable.

    :param alert_on_fail: Whether to alert on task failure and auto-resolve on a
        later success. Excluded from the derived schema — the framework renders it
        from the ``alert_on_fail`` capability — yet validated in the JSON body so
        every model-first form accepts it. Defaults to ``False``.
    :cvar __form_rules__: Section-scoped and plugin-scoped conditional rules that
        cannot attach to a single field (field-level gates live on the fields via
        ``Requires`` / ``Forbidden``). Defaults to an empty :class:`FormRules`.
    """

    model_config = ConfigDict(extra="forbid")

    __form_rules__: ClassVar[FormRules] = FormRules()

    alert_on_fail: Annotated[bool, Hidden()] = False

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Extract the conditional-rule plan once ``model_fields`` is populated.

        Pydantic calls this after the subclass's fields are built, so the rule
        plan can be derived from them and wired onto the class via the existing
        :func:`~app.extensions.apps.framework.rules.apply_conditional_rules` machinery.

        :param kwargs: Forwarded to the superclass hook.
        """
        super().__pydantic_init_subclass__(**kwargs)
        if not cls.model_fields:
            return
        apply_conditional_rules(build_runtime_schema(cls))(cls)


class TaskFormModel(AppFormModel):
    """Provide the Task-section identity fields every task-based plugin form shares.

    Task plugins all open their form with the same two fields — the task's name
    and the executor host it runs on — declared with identical DSL markers. This
    base centralises that single declaration the same way :class:`AppFormModel`
    centralises ``alert_on_fail``, so a task plugin's create model inherits
    ``task_name`` / ``hostname`` and declares only its plugin-specific fields.

    Unlike ``alert_on_fail`` (a hidden, defaulted capability control), these are
    required, schema-visible fields, so a subclass's form layout must include a
    ``Task`` section. A non-task form model that needs no Task section should
    subclass :class:`AppFormModel` directly instead.

    :param task_name: The human-readable task name; required and non-empty.
    :param hostname: The executor host the task runs on; required and non-empty.
        The default ``HostRef(target_service="service_id")`` ties the
        co-location warning to a service field named ``service_id``; subclasses
        that name their service field differently must override with an explicit
        ``HostRef(target_service=...)``.
    """

    task_name: Annotated[NonEmptyStr, Ui(section="Task")]
    hostname: Annotated[
        NonEmptyStr,
        HostRef(target_service="service_id"),
        Ui(label=EXECUTION_HOST_LABEL, section="Task"),
    ]
