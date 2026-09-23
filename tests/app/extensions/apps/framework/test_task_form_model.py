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

"""Test ``TaskFormModel`` — the shared base providing the Task identity fields.

Lift the ``task_name`` / ``hostname`` Task-section fields off every task-based
plugin form onto a single ``TaskFormModel`` base, mirroring how
``alert_on_fail`` is centralised on ``AppFormModel``.
"""

from typing import Annotated

import pytest
from pydantic import ValidationError

from app.extensions.apps.alters.models import AltersCreate
from app.extensions.apps.archives.models import ArchivesCreate
from app.extensions.apps.backup_mongo.config.models import BackupConfigForm
from app.extensions.apps.backup_mongo.models import _BackupMongoTaskForm, BackupForm
from app.extensions.apps.backup_mongo.restore.models import RestoreForm
from app.extensions.apps.backup_pg.models import BackupPgForm
from app.extensions.apps.checksums.models import ChecksumsForm
from app.extensions.apps.framework.form_dsl import (
    derive_form_sections,
    FormLayout,
    HostRef,
    SectionLayout,
    TaskFormModel,
    Ui,
)
from app.extensions.apps.framework.schema import (
    EXECUTION_HOST_LABEL,
    HostField,
    StringField,
)
from app.extensions.apps.mysql_backups.forms import BackupCreate
from app.extensions.apps.mysql_backups.restore.models import RestoreCreate

_TASK_LAYOUT = FormLayout(sections=[SectionLayout(key="Task", title="Task")])

# The task-based plugin create forms that should inherit the identity fields.
_PLUGIN_FORMS = [
    ArchivesCreate,
    BackupPgForm,
    ChecksumsForm,
    BackupCreate,
    RestoreCreate,
    BackupForm,
    BackupConfigForm,
    RestoreForm,
]

# Forms that inherit ``TaskFormModel`` but redeclare ``task_name`` only so the
# derived schema can carry a ``Ui(default=...)`` presentation default. ``hostname``
# must still come from the base.
_PLUGIN_FORMS_WITH_TASK_NAME_DEFAULT = (RestoreForm,)

# Forms that inherit ``TaskFormModel`` but redeclare both identity fields: a
# ``task_name`` presentation default plus a ``hostname`` ``HostRef`` cascade
# (``Ui(depends_on=...)``) and Task-section field order.
#
# ``_BackupMongoTaskForm`` rather than ``BackupForm``: MongoDB Backups and PBM
# Configuration are two forms now, disjoint below the Task section, so the
# redeclared trio moved to the base they share. Both remain plain inheritors in
# ``_PLUGIN_FORMS`` above, which is what the redeclaration was extracted to make
# true again.
_PLUGIN_FORMS_WITH_IDENTITY_REDECLARE = (_BackupMongoTaskForm,)

# ``AltersCreate`` is a deliberate carve-out, NOT an inheritor. It is also a
# model-first task form, but it redeclares its identity fields locally
# (``task_name``, ``hostname``) alongside its own Task-section fields
# (``service_id``, ``pre_checks_mysql_config_file``) rather than inheriting the
# identity pair from ``TaskFormModel``. Its Task section uses the same
# ``section="Task"`` key as every inheritor and the shared ``TASK_SECTION_LAYOUT``;
# reparenting it onto ``TaskFormModel`` is a separate, out-of-scope change.


def _marker(field_info, marker_type):
    """Return the first metadata marker of ``marker_type`` on ``field_info``."""
    return next((m for m in field_info.metadata if isinstance(m, marker_type)), None)


class TestTaskFormModelFields:
    """Cover the canonical declaration on the base itself."""

    def test_exposes_task_identity_fields(self) -> None:
        """Declare ``task_name`` and ``hostname`` directly on ``TaskFormModel``."""
        assert "task_name" in TaskFormModel.model_fields
        assert "hostname" in TaskFormModel.model_fields

    def test_task_name_carries_canonical_ui(self) -> None:
        """Carry a labelless ``Ui(section="Task")`` marker; label derives from the name."""
        ui = _marker(TaskFormModel.model_fields["task_name"], Ui)
        assert ui is not None
        assert ui.label is None
        assert ui.section == "Task"

    def test_hostname_carries_hostref_and_ui(self) -> None:
        """Mark ``hostname`` as a ``HostRef`` executor selector in the Task section."""
        field = TaskFormModel.model_fields["hostname"]
        host_ref = _marker(field, HostRef)
        assert host_ref is not None
        assert host_ref.target_service == "service_id"
        ui = _marker(field, Ui)
        assert ui is not None
        assert ui.label == EXECUTION_HOST_LABEL
        assert ui.section == "Task"

    def test_identity_fields_are_required_non_empty(self) -> None:
        """Reject empty ``task_name`` / ``hostname`` (inherited ``NonEmptyStr``)."""

        class _Form(TaskFormModel):
            pass

        with pytest.raises(ValidationError):
            _Form(task_name="", hostname="host")
        with pytest.raises(ValidationError):
            _Form(task_name="t", hostname="")
        # A fully-populated instance validates.
        assert _Form(task_name="t", hostname="host").task_name == "t"


class TestTaskFormModelDerivation:
    """Cover schema derivation of an inheriting subclass."""

    def test_identity_fields_lead_the_task_section(self) -> None:
        """Derive ``task_name`` then ``hostname`` as the first visible fields."""

        class _Form(TaskFormModel):
            pass

        sections = derive_form_sections(_Form, _TASK_LAYOUT)
        fields = [f for section in sections for f in section.fields]
        assert [f.name for f in fields[:2]] == ["task_name", "hostname"]
        assert fields[0].label == "Task Name"
        assert isinstance(fields[0], StringField)
        assert isinstance(fields[1], HostField)
        assert fields[1].target_service == "service_id"

    def test_subclass_field_follows_inherited_identity_fields(self) -> None:
        """Place a subclass's own Task field after the inherited identity fields."""

        class _Form(TaskFormModel):
            note: Annotated[str, Ui(label="Note", section="Task")] = ""

        fields = [
            f
            for section in derive_form_sections(_Form, _TASK_LAYOUT)
            for f in section.fields
        ]
        assert [f.name for f in fields] == ["task_name", "hostname", "note"]


class TestPluginFormInheritance:
    """Cover that the 7 plugin forms inherit rather than redeclare."""

    @pytest.mark.parametrize("form", _PLUGIN_FORMS, ids=lambda f: f.__name__)
    def test_inherits_task_form_model(self, form: type) -> None:
        """Reparent every task plugin form onto ``TaskFormModel``."""
        assert issubclass(form, TaskFormModel)

    @pytest.mark.parametrize(
        "form",
        [
            form
            for form in _PLUGIN_FORMS
            if form not in _PLUGIN_FORMS_WITH_TASK_NAME_DEFAULT
            and form not in _PLUGIN_FORMS_WITH_IDENTITY_REDECLARE
        ],
        ids=lambda f: f.__name__,
    )
    def test_does_not_redeclare_identity_fields(self, form: type) -> None:
        """Stop redeclaring the identity fields locally — inherit them instead."""
        own = set(getattr(form, "__annotations__", {}))
        assert "task_name" not in own
        assert "hostname" not in own

    @pytest.mark.parametrize(
        "form", _PLUGIN_FORMS_WITH_TASK_NAME_DEFAULT, ids=lambda f: f.__name__
    )
    def test_task_name_default_redeclares_only_task_name(self, form: type) -> None:
        """Allow redeclaring ``task_name`` solely for a ``Ui(default=...)`` value."""
        own = set(getattr(form, "__annotations__", {}))
        assert "task_name" in own
        assert "hostname" not in own
        ui = _marker(form.model_fields["task_name"], Ui)
        assert ui is not None
        assert ui.has_default
        assert ui.default

    @pytest.mark.parametrize(
        "form", _PLUGIN_FORMS_WITH_IDENTITY_REDECLARE, ids=lambda f: f.__name__
    )
    def test_identity_redeclare_keeps_default_and_host_cascade(
        self, form: type
    ) -> None:
        """Allow redeclaring both fields for a task-name default and host cascade."""
        own = set(getattr(form, "__annotations__", {}))
        assert {"task_name", "hostname"} <= own
        task_ui = _marker(form.model_fields["task_name"], Ui)
        assert task_ui is not None
        assert task_ui.has_default
        assert task_ui.default
        host_ui = _marker(form.model_fields["hostname"], Ui)
        assert host_ui is not None
        assert host_ui.depends_on
        assert _marker(form.model_fields["hostname"], HostRef) is not None

    @pytest.mark.parametrize("form", _PLUGIN_FORMS, ids=lambda f: f.__name__)
    def test_still_has_identity_fields(self, form: type) -> None:
        """Keep both identity fields available on every form via inheritance."""
        assert "task_name" in form.model_fields
        assert "hostname" in form.model_fields


class TestAltersCarveOut:
    """Lock ``AltersCreate`` as the documented non-inheriting carve-out."""

    def test_alters_does_not_inherit_task_form_model(self) -> None:
        """Keep alters off ``TaskFormModel`` — it redeclares its own identity fields."""
        assert not issubclass(AltersCreate, TaskFormModel)

    def test_alters_uses_titlecase_task_section(self) -> None:
        """Pin the carve-out: alters redeclares identity fields ``section="Task"``."""
        own = set(AltersCreate.__annotations__)
        assert {"task_name", "hostname"} <= own
        for name in ("task_name", "hostname"):
            ui = _marker(AltersCreate.model_fields[name], Ui)
            assert ui is not None
            assert ui.section == "Task"
