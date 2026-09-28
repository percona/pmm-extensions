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

"""Tests for the backup_pg create form and JSON API Pydantic models."""

import re

import pytest
from pydantic import ValidationError

from app.extensions.apps.backup_pg.models import (
    BackupConfigAll,
    BackupPgForm,
    BackupTaskDetailResponse,
    BackupTaskResponse,
    BackupType,
    PgBackRestBackupType,
)
from app.extensions.apps.framework import BaseTaskResponse
from app.extensions.apps.framework.form_dsl import Choices
from app.tasks.models import TaskBackendEnum
from tests.app.extensions.apps.backup_pg.conftest import PGBACKREST_INCREMENTAL_CYCLES
from tests.app.extensions.apps.conftest import literal_members

DEFAULT_PG_PORT = 5432
SAMPLE_BACKUP_DIR = "/var/lib/pgbackrest"


def test_backup_pg_form_accepts_minimal_payload() -> None:
    """Accept the minimal required fields."""
    body = BackupPgForm(
        task_name="pg-task",
        hostname="pg-host",
        service_id=1,
        backup_dir=SAMPLE_BACKUP_DIR,
        stanza="extensions-test",
    )

    assert body.task_name == "pg-task"
    assert body.alert_on_fail is False


def test_backup_pg_form_rejects_missing_backup_dir() -> None:
    """``backup_dir`` is required: omitting it fails Pydantic validation."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            stanza="extensions-test",
        )


def test_backup_pg_form_rejects_empty_backup_dir() -> None:
    """``backup_dir`` is ``NonEmptyStr``; an empty string fails validation."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            stanza="extensions-test",
            backup_dir="",
        )


def test_backup_pg_form_rejects_extra_host_port_fields() -> None:
    """Reject ``host``/``port`` so silent drops surface as 422.

    The payload pins ``host="localhost"`` and uses ``service.port`` from the
    inventory lookup; accepting these fields would let a stale FE caller
    submit values that vanish.
    """
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza="extensions-test",
            host="x",
        )
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza="extensions-test",
            port=DEFAULT_PG_PORT,
        )


def test_backup_pg_form_rejects_empty_task_name() -> None:
    """Empty task_name fails Pydantic validation."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza="extensions-test",
        )


def test_backup_pg_form_accepts_pgbackrest_backup_type() -> None:
    """pgbackrest_backup_type accepts INCR or DIFF enum values."""
    body = BackupPgForm(
        task_name="pg-task",
        hostname="pg-host",
        service_id=1,
        backup_dir=SAMPLE_BACKUP_DIR,
        stanza="extensions-test",
        pgbackrest_backup_type=PgBackRestBackupType.DIFF,
    )

    assert body.pgbackrest_backup_type is PgBackRestBackupType.DIFF


def test_backup_pg_form_coerces_blank_optionals_to_none() -> None:
    """Coerce empty-string optional fields to None (the HTML form path)."""
    body = BackupPgForm(
        task_name="pg-task",
        hostname="pg-host",
        service_id=1,
        backup_dir=SAMPLE_BACKUP_DIR,
        stanza="extensions-test",
        pgbackrest_bin="",
        pgbackrest_backup_type="",
        pgbackrest_retention_full="",
        pgbackrest_config_file="",
        pgbackrest_datadir="",
        pgbackrest_incremental_cycle="",
        logging_dir="",
    )

    assert body.pgbackrest_bin is None
    assert body.pgbackrest_backup_type is None
    assert body.pgbackrest_retention_full is None
    assert body.pgbackrest_config_file is None
    assert body.pgbackrest_datadir is None
    assert body.pgbackrest_incremental_cycle is None
    assert body.logging_dir is None


def test_backup_pg_form_strips_stanza_whitespace() -> None:
    """Stanza trims surrounding whitespace."""
    body = BackupPgForm(
        task_name="pg-task",
        hostname="pg-host",
        service_id=1,
        backup_dir=SAMPLE_BACKUP_DIR,
        stanza="  extensions-test  ",
    )

    assert body.stanza == "extensions-test"


@pytest.mark.parametrize(
    "invalid_stanza",
    ["../extensions", "extensions/test", "extensions.test", "_extensions"],
)
def test_backup_pg_form_rejects_unsafe_stanza(invalid_stanza: str) -> None:
    """Stanza only allows [A-Za-z0-9][A-Za-z0-9_-]*."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza=invalid_stanza,
        )


def test_backup_pg_form_rejects_unknown_pgbackrest_backup_type() -> None:
    """Non-enum pgbackrest_backup_type values are rejected."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza="extensions-test",
            pgbackrest_backup_type="full",
        )


def test_backup_pg_form_rejects_negative_retention() -> None:
    """Reject a negative retention count (the fields are ``ge=0``)."""
    with pytest.raises(ValidationError):
        BackupPgForm(
            task_name="pg-task",
            hostname="pg-host",
            service_id=1,
            backup_dir=SAMPLE_BACKUP_DIR,
            stanza="extensions-test",
            pgbackrest_retention_full=-1,
        )


def test_backup_task_response_roundtrips_backup_type() -> None:
    """Serialize backup_type and omit the internal owner fields."""
    response = BackupTaskResponse(
        name="pg-task",
        owner="BACKUP_PG",
        hostname="pg-host",
        backend=TaskBackendEnum.PROXY,
        backup_type="P",
        data={"meta": {"target": "pg-host"}},
        protected=False,
        alert_on_fail=False,
    )

    dumped = response.model_dump(mode="json")

    assert isinstance(response, BaseTaskResponse)
    assert response.owner == "BACKUP_PG"
    assert dumped["backup_type"] == "P"
    assert "owner" not in dumped
    assert "service_type" not in dumped
    assert "anonymize_mask" in dumped
    assert "anonymized_entities" in dumped
    assert "connectivity_warning" in dumped


def test_backup_task_detail_response_inherits_response_fields() -> None:
    """Carry host/port on the detail response beyond the list response."""
    detail = BackupTaskDetailResponse(
        name="pg-task",
        owner="BACKUP_PG",
        backend=TaskBackendEnum.PROXY,
        backup_type="P",
        data={},
        protected=False,
        alert_on_fail=False,
    )

    assert detail.status is None
    assert detail.created_at is None
    assert detail.host is None
    assert detail.port is None


class TestPgbackrestIncrementalCycleField:
    """Cover for the ``pgbackrest_incremental_cycle`` field on both form models."""

    _FIELD = "pgbackrest_incremental_cycle"

    @staticmethod
    def _create_body(**overrides: object) -> dict[str, object]:
        """Return a valid PostgreSQL Backups create body."""
        return {
            "task_name": "cycle-form",
            "hostname": "host-1",
            "service_id": 1,
            "stanza": "extensions-test",
            "backup_dir": SAMPLE_BACKUP_DIR,
            **overrides,
        }

    @classmethod
    def _choices(cls) -> dict[str, str]:
        """Return the create form's cycle options as ``{value: label}``."""
        marker = next(
            entry
            for entry in BackupPgForm.model_fields[cls._FIELD].metadata
            if isinstance(entry, Choices)
        )
        return {str(option.value): option.label for option in marker.options}

    @pytest.mark.parametrize("cycle", PGBACKREST_INCREMENTAL_CYCLES)
    def test_config_model_accepts_every_cycle(self, cycle):
        """Accept the whole vocabulary on the configuration-file model."""
        config = BackupConfigAll.model_validate({"PGBACKREST_INCREMENTAL_CYCLE": cycle})
        assert config.pgbackrest_incremental_cycle == cycle

    @pytest.mark.parametrize("cycle", PGBACKREST_INCREMENTAL_CYCLES)
    def test_create_model_accepts_every_cycle(self, cycle):
        """Accept the whole vocabulary on the create form."""
        form = BackupPgForm.model_validate(
            self._create_body(pgbackrest_incremental_cycle=cycle)
        )
        assert form.pgbackrest_incremental_cycle == cycle

    @pytest.mark.parametrize("cycle", ["0", "8", "monday", "01", " 1"])
    def test_both_models_reject_values_outside_the_vocabulary(self, cycle):
        """Reject anything the payload would reject, at the request boundary."""
        with pytest.raises(ValidationError):
            BackupConfigAll.model_validate({"PGBACKREST_INCREMENTAL_CYCLE": cycle})
        with pytest.raises(ValidationError):
            BackupPgForm.model_validate(
                self._create_body(pgbackrest_incremental_cycle=cycle)
            )

    def test_empty_string_becomes_none(self):
        """Read an empty submission as unset rather than as a cycle."""
        config = BackupConfigAll.model_validate({"PGBACKREST_INCREMENTAL_CYCLE": ""})
        assert config.pgbackrest_incremental_cycle is None
        form = BackupPgForm.model_validate(
            self._create_body(pgbackrest_incremental_cycle="")
        )
        assert form.pgbackrest_incremental_cycle is None

    def test_stays_unset_by_default(self):
        """Introduce no default: an unset field reaches the payload absent."""
        assert BackupConfigAll.model_validate({}).pgbackrest_incremental_cycle is None
        assert (
            BackupPgForm.model_validate(
                self._create_body()
            ).pgbackrest_incremental_cycle
            is None
        )

    def test_survives_config_all_round_trip(self):
        """Persist a Monday cycle through serialise / re-validate."""
        config = BackupConfigAll.model_validate({"PGBACKREST_INCREMENTAL_CYCLE": "1"})
        serialised = {k.upper(): v for k, v in config.model_dump().items()}
        assert (
            BackupConfigAll.model_validate(serialised).pgbackrest_incremental_cycle
            == "1"
        )

    def test_both_models_declare_the_same_vocabulary(self):
        """Keep the two models' vocabularies identical."""
        assert literal_members(BackupPgForm, self._FIELD) == literal_members(
            BackupConfigAll, self._FIELD
        )

    def test_every_accepted_value_is_offered_once(self):
        """Offer exactly the accepted vocabulary as dropdown options."""
        assert tuple(self._choices()) == literal_members(BackupPgForm, self._FIELD)

    def test_numeric_options_are_labelled_as_weekdays(self):
        """Name each numeric option by the weekday it selects."""
        choices = self._choices()
        assert [choices[str(day)] for day in range(1, 8)] == [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        assert not [
            label for label in choices.values() if re.fullmatch(r"\d+ days?", label)
        ]

    def test_weekly_label_shows_its_monday_equivalence(self):
        """Explain the duplicate: ``weekly`` is Monday under another name."""
        assert "Monday" in self._choices()["weekly"]


def test_backup_type_labels_cover_every_member():
    """Label every declared ``BackupType`` member, keyed by its stored value."""
    assert set(BackupType.LABELS) == {member.value for member in BackupType}


def test_backup_type_labels_are_the_declared_display_strings():
    """Pin the display text each stored backup-type value resolves to."""
    assert BackupType.LABELS == {"P": "pgBackRest"}


def test_backup_type_labels_is_not_an_enum_member():
    """Keep ``LABELS`` off the enum's member list via ``enum.nonmember``."""
    assert "LABELS" not in {member.name for member in BackupType}
