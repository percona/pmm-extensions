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

"""Tests for the app.extensions.apps.mysql_backups.forms module."""

import re

import pytest
from pydantic import ValidationError

from app.extensions.apps.framework import BaseTaskResponse
from app.extensions.apps.framework.form_dsl import Choices, Ui
from app.extensions.apps.mysql_backups.forms import (
    _BINARY_COMPRESSION_FAIL_RULES,
    ALLOWED_COMPRESSIONS,
    ALLOWED_XTRABACKUP_BIN_COMPRESSIONS,
    BackupConfigAll,
    BackupCreate,
    BackupTaskResponse,
    CompressionAlgorithm,
    resolve_xtrabackup_compression,
    XTRABACKUP_BIN_DEFAULT,
)
from app.extensions.apps.mysql_backups.models import BackupType, XtraBackupTool
from app.tasks.models import TaskBackendEnum
from tests.app.extensions.apps.conftest import literal_members
from tests.app.extensions.apps.mysql_backups.conftest import (
    xtrabackup_binary_default,
    XTRABACKUP_INCREMENTAL_CYCLES,
    xtrabackup_payload_tree,
)


class TestXtrabackupQuietField:
    """Tests for the ``xtrabackup_quiet`` field on ``BackupConfigAll``."""

    def test_defaults_to_false(self):
        """Field defaults to False; existing tasks are unaffected by the addition."""
        config = BackupConfigAll.model_validate({})
        assert config.xtrabackup_quiet is False

    def test_accepts_true(self):
        """Field accepts True via case-insensitive key validation."""
        config = BackupConfigAll.model_validate({"Xtrabackup_Quiet": True})
        assert config.xtrabackup_quiet is True

    def test_accepts_false_explicitly(self):
        """Explicit False round-trips correctly (not just the default)."""
        config = BackupConfigAll.model_validate({"XTRABACKUP_QUIET": False})
        assert config.xtrabackup_quiet is False

    def test_survives_config_all_round_trip(self):
        """True value persists when BackupConfigAll serialises and re-validates itself.

        Regression guard for the pattern: ``BackupConfigAll`` uses
        ``extra='ignore'``, so any field missing from the class is silently
        dropped on re-validation.  The field must be declared on
        ``BackupConfigAll`` — not only on ``BackupCreate`` — or it vanishes
        from the persisted YAML config and the Nomad payload never sees it.
        """
        config = BackupConfigAll.model_validate({"XTRABACKUP_QUIET": True})
        serialised = {k.upper(): v for k, v in config.model_dump().items()}
        round_tripped = BackupConfigAll.model_validate(serialised)
        assert round_tripped.xtrabackup_quiet is True

    def test_none_raises_validation_error(self):
        """None is not a valid boolean; must raise ValidationError, not coerce to False.

        A ``null`` YAML value or an explicit ``None`` from the form layer
        must surface as a validation failure rather than silently becoming
        ``False``, which would mask a misconfigured task.
        """
        with pytest.raises(ValidationError):
            BackupConfigAll.model_validate({"XTRABACKUP_QUIET": None})


class TestUploadQuietField:
    """Tests for the ``upload_quiet`` field on ``BackupConfigAll``."""

    def test_defaults_to_false(self):
        """Field defaults to False; existing tasks are unaffected by the addition."""
        config = BackupConfigAll.model_validate({})
        assert config.upload_quiet is False

    def test_accepts_true(self):
        """Field accepts True via case-insensitive key validation."""
        config = BackupConfigAll.model_validate({"Upload_Quiet": True})
        assert config.upload_quiet is True

    def test_accepts_false_explicitly(self):
        """Explicit False round-trips correctly (not just the default)."""
        config = BackupConfigAll.model_validate({"UPLOAD_QUIET": False})
        assert config.upload_quiet is False

    def test_survives_config_all_round_trip(self):
        """True value persists when BackupConfigAll serialises and re-validates itself.

        Regression guard for the pattern: ``BackupConfigAll`` uses
        ``extra='ignore'``, so any field missing from the class is silently
        dropped on re-validation.  The field must be declared on
        ``BackupConfigAll`` — not only on ``BackupCreate`` — or it vanishes
        from the persisted YAML config and the Nomad payload never sees it.
        """
        config = BackupConfigAll.model_validate({"UPLOAD_QUIET": True})
        serialised = {k.upper(): v for k, v in config.model_dump().items()}
        round_tripped = BackupConfigAll.model_validate(serialised)
        assert round_tripped.upload_quiet is True

    def test_none_raises_validation_error(self):
        """None is not a valid boolean; must raise ValidationError, not coerce to False.

        A ``null`` YAML value or an explicit ``None`` from the form layer
        must surface as a validation failure rather than silently becoming
        ``False``, which would mask a misconfigured task.
        """
        with pytest.raises(ValidationError):
            BackupConfigAll.model_validate({"UPLOAD_QUIET": None})


class TestBackupTaskResponseModel:
    """Verify ``BackupTaskResponse`` is rebased onto ``BaseTaskResponse``."""

    def test_exposes_inherited_task_response_surface(self) -> None:
        """Carry the shared anonymization and connectivity surface from the base."""
        response = BackupTaskResponse(
            name="mysql-backup",
            owner="BACKUPS",
            backend=TaskBackendEnum.PROXY,
            backup_type=BackupType.MYDUMPER,
            hostname="db-host",
            data={"meta": {"target": "db-host"}},
            protected=False,
            alert_on_fail=False,
        )

        dumped = response.model_dump(mode="json")

        assert isinstance(response, BaseTaskResponse)
        assert dumped["backup_type"] == BackupType.MYDUMPER.value
        assert dumped["hostname"] == "db-host"
        assert "service_type" not in dumped
        assert "owner" not in dumped
        assert "anonymize_mask" in dumped
        assert "anonymized_entities" in dumped
        assert "connectivity_warning" in dumped


class TestXtrabackupIncrementalCycleField:
    """Cover for the ``xtrabackup_incremental_cycle`` field on both form models."""

    _FIELD = "xtrabackup_incremental_cycle"

    @staticmethod
    def _create_body(**overrides: object) -> dict[str, object]:
        """Return a gated XtraBackup create body the per-mode rules accept."""
        return {
            "task_name": "cycle-form",
            "hostname": "host-1",
            "service_id": 1,
            "backup_type": BackupType.XTRABACKUP.value,
            "backup_dir": "/backups",
            "upload": ["RSYNC"],
            "rsync_path": "/data/rsync",
            **overrides,
        }

    @classmethod
    def _choices(cls) -> dict[str, str]:
        """Return the create form's cycle options as ``{value: label}``."""
        marker = next(
            entry
            for entry in BackupCreate.model_fields[cls._FIELD].metadata
            if isinstance(entry, Choices)
        )
        return {str(option.value): option.label for option in marker.options}

    @classmethod
    def _description(cls) -> str:
        """Return the create form's cycle help text."""
        marker = next(
            entry
            for entry in BackupCreate.model_fields[cls._FIELD].metadata
            if isinstance(entry, Ui)
        )
        return marker.description or ""

    @pytest.mark.parametrize("cycle", XTRABACKUP_INCREMENTAL_CYCLES)
    def test_config_model_accepts_every_cycle(self, cycle):
        """Accept the whole vocabulary on the configuration-file model."""
        config = BackupConfigAll.model_validate({"XTRABACKUP_INCREMENTAL_CYCLE": cycle})
        assert config.xtrabackup_incremental_cycle == cycle

    @pytest.mark.parametrize("cycle", XTRABACKUP_INCREMENTAL_CYCLES)
    def test_create_model_accepts_every_cycle(self, cycle):
        """Accept the whole vocabulary on the create form."""
        form = BackupCreate.model_validate(
            self._create_body(xtrabackup_incremental_cycle=cycle)
        )
        assert form.xtrabackup_incremental_cycle == cycle

    @pytest.mark.parametrize("cycle", ["0", "8", "monday", "01", " 1"])
    def test_both_models_reject_values_outside_the_vocabulary(self, cycle):
        """Reject anything the payload would reject, at the request boundary."""
        with pytest.raises(ValidationError):
            BackupConfigAll.model_validate({"XTRABACKUP_INCREMENTAL_CYCLE": cycle})
        with pytest.raises(ValidationError):
            BackupCreate.model_validate(
                self._create_body(xtrabackup_incremental_cycle=cycle)
            )

    def test_empty_string_becomes_none(self):
        """Read an empty submission as unset rather than as a cycle."""
        config = BackupConfigAll.model_validate({"XTRABACKUP_INCREMENTAL_CYCLE": ""})
        assert config.xtrabackup_incremental_cycle is None
        form = BackupCreate.model_validate(
            self._create_body(xtrabackup_incremental_cycle="")
        )
        assert form.xtrabackup_incremental_cycle is None

    def test_stays_unset_by_default(self):
        """Introduce no default: an unset field reaches the payload absent.

        The payload supplies ``weekly`` when the configuration carries no
        ``XTRABACKUP_INCREMENTAL_CYCLE`` key, so a form-side default would silently
        pin the cadence for every task that never touched the field.
        """
        assert BackupConfigAll.model_validate({}).xtrabackup_incremental_cycle is None
        assert (
            BackupCreate.model_validate(
                self._create_body()
            ).xtrabackup_incremental_cycle
            is None
        )

    def test_survives_config_all_round_trip(self):
        """Persist a Monday cycle through serialise / re-validate.

        ``BackupConfigAll`` uses ``extra='ignore'``, so a value the model cannot
        express is dropped from the stored YAML and the payload never sees it.
        """
        config = BackupConfigAll.model_validate({"XTRABACKUP_INCREMENTAL_CYCLE": "1"})
        serialised = {k.upper(): v for k, v in config.model_dump().items()}
        assert (
            BackupConfigAll.model_validate(serialised).xtrabackup_incremental_cycle
            == "1"
        )

    def test_both_models_declare_the_same_vocabulary(self):
        """Keep the two models' vocabularies identical.

        A value one model accepts must be accepted by the other, or a
        configuration written through the create form fails to load back.
        """
        assert literal_members(BackupCreate, self._FIELD) == literal_members(
            BackupConfigAll, self._FIELD
        )

    def test_every_accepted_value_is_offered_once(self):
        """Offer exactly the accepted vocabulary as dropdown options.

        Choice derivation returns the ``Choices`` marker verbatim and never
        cross-checks it against the annotation, so an option missing here is a
        value the form accepts but the user cannot pick.
        """
        assert tuple(self._choices()) == literal_members(BackupCreate, self._FIELD)

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

    def test_help_text_states_the_vocabulary_and_its_reach(self):
        """State the vocabulary, the ISO weekday reading, and the governed method."""
        description = self._description()
        assert "ISO weekday" in description
        assert "1-7" in description
        assert "less_space" in description


class TestXtrabackupBinaryCompressionMatrix:
    """Hold the per-binary compression matrix to the surfaces around it."""

    _FIELD = "xtrabackup_bin_cmd"

    def test_matrix_covers_every_declared_binary(self):
        """Hold a row for each binary the vocabulary declares.

        A binary added to the enum with no row would fall through the gate
        entirely and reach a host that cannot run the chosen algorithm. Asserted
        against the enum rather than each model's annotation now that both models
        and the restore form share it.
        """
        assert set(ALLOWED_XTRABACKUP_BIN_COMPRESSIONS) == set(XtraBackupTool)

    @pytest.mark.parametrize("binary", list(XtraBackupTool))
    def test_config_model_accepts_every_binary(self, binary: XtraBackupTool):
        """Accept the whole vocabulary on the configuration-file model.

        The matrix parity check above compares the matrix to the enum, so it
        stays green if a model's field drifts off the enum; this is what notices.
        ``BackupCreate``'s side of that is covered by the gating suite, which
        builds a form per matrix row.
        """
        config = BackupConfigAll.model_validate({"XTRABACKUP_BIN_CMD": binary.value})
        assert config.xtrabackup_bin_cmd is binary

    @pytest.mark.parametrize("model", [BackupCreate, BackupConfigAll])
    def test_both_models_reject_a_binary_outside_the_enum(
        self, model: type[BackupCreate | BackupConfigAll]
    ):
        """Refuse a binary the vocabulary does not declare.

        The ``Literal`` these fields carried gave this for free; the enum has to
        be shown to keep it, or a typo reaches a host as an unrunnable command.
        """
        with pytest.raises(ValidationError) as excinfo:
            model.model_validate({self._FIELD: "not-a-backup-binary"})

        # Case-folded: the config model reports the uppercase YAML alias, the form
        # model the field name, and the claim is about the field either way.
        reported = {str(error["loc"][0]).lower() for error in excinfo.value.errors()}
        assert self._FIELD in reported

    def test_every_row_narrows_the_backup_type_list(self):
        """Offer only algorithms the XtraBackup type already publishes."""
        offered = set(ALLOWED_COMPRESSIONS[BackupType.XTRABACKUP])
        for allowed in ALLOWED_XTRABACKUP_BIN_COMPRESSIONS.values():
            # vacuous-ok: test_every_row_leaves_a_usable_algorithm rejects an empty
            # row, and test_matrix_covers_every_declared_binary an empty matrix.
            assert set(allowed) <= offered

    def test_every_row_leaves_a_usable_algorithm(self):
        """Keep at least one algorithm per binary.

        An empty row rejects every algorithm for that binary, which reads as
        "compression is unavailable" without saying so anywhere.
        """
        assert all(ALLOWED_XTRABACKUP_BIN_COMPRESSIONS.values())

    def test_default_binary_has_a_row(self):
        """Give the defaulted binary a row: blank forms are gated against it."""
        assert XTRABACKUP_BIN_DEFAULT in ALLOWED_XTRABACKUP_BIN_COMPRESSIONS

    @pytest.mark.parametrize(
        ("binary", "expected"),
        [
            (None, CompressionAlgorithm.ZSTD),
            (XtraBackupTool.XTRABACKUP, CompressionAlgorithm.ZSTD),
            (XtraBackupTool.INNOBACKUPEX, CompressionAlgorithm.QUICKLZ),
            (XtraBackupTool.MARIADB_BACKUP, CompressionAlgorithm.QUICKLZ),
        ],
    )
    def test_blank_algorithm_resolves_to_a_runnable_one(
        self, binary: XtraBackupTool | None, expected: CompressionAlgorithm
    ) -> None:
        """Resolve a blank algorithm to one the binary can run, zstd where it can.

        The only place the algorithm names are typed out: the payload and dispatch
        tests derive their expectations from this resolver, so this table is what
        stops both sides from agreeing on a wrong answer.
        """
        assert resolve_xtrabackup_compression(binary) == expected

    def test_default_binary_matches_the_payload(self):
        """Pin the form's blank-field resolution to the payload's own default."""
        assert (
            xtrabackup_binary_default(xtrabackup_payload_tree())
            == XTRABACKUP_BIN_DEFAULT
        )

    @staticmethod
    def _compression_description() -> str:
        """Return the create form's compression-algorithm help text."""
        marker = next(
            entry
            for entry in BackupCreate.model_fields["compression_algorithm"].metadata
            if isinstance(entry, Ui)
        )
        return marker.description or ""

    def test_field_description_names_every_row(self):
        """Keep the field's help text and the gate telling one story.

        The description is the operator's only sight of the matrix before they
        submit; a row it omits or misstates contradicts the message they then get.
        """
        description = self._compression_description()

        for binary, allowed in ALLOWED_XTRABACKUP_BIN_COMPRESSIONS.items():
            assert binary in description
            for algorithm in allowed:
                assert algorithm.value in description

    def test_every_row_carries_a_rule_or_needs_none(self):
        """Emit one gate per binary that rejects something, and none otherwise.

        A row accepting the whole XtraBackup list has nothing to reject, and the
        predicate DSL refuses an empty disjunction — so the builder has to skip it
        rather than raise at import and take the app down with it.
        """
        gating = {
            binary
            for binary, allowed in ALLOWED_XTRABACKUP_BIN_COMPRESSIONS.items()
            if set(allowed) != set(ALLOWED_COMPRESSIONS[BackupType.XTRABACKUP])
        }

        assert len(_BINARY_COMPRESSION_FAIL_RULES) == len(gating)
        for binary in gating:
            assert any(
                rule.message and f"is {binary.value!r}" in rule.message
                for rule in _BINARY_COMPRESSION_FAIL_RULES
            )

    def test_the_create_model_carries_the_rules_on_the_field_s_section(self):
        """Attach the built rules to the section that declares the field.

        Section scope is what gets the rules evaluated before submit, so rules
        built here but left off the section would gate the POST and nothing else.
        Asserted in this module because it is the one that owns the rule tuple;
        the leniency the backfill model derives from the same split is pinned by
        ``TestBinaryCompressionBackfillLeniency`` in ``test_form_backfill.py``.
        """
        rules = BackupCreate.__form_rules__

        assert set(rules.sections) == {"General"}
        assert rules.sections["General"].fail_when == _BINARY_COMPRESSION_FAIL_RULES
