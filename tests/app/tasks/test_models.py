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

"""Define test cases for the tasks data models and validation."""

import json
from collections import defaultdict
from datetime import datetime, UTC
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from cryptography.fernet import Fernet
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import ValidationError

from app.core.alerts.models import AlertService, AlertSeverity
from app.core.encryption import decrypt, is_encrypted
from app.core.utils.path import PayloadReferenceError
from app.extensions.apps.archives.alerts import (
    ALERT_DETAIL_BUILDER,
    ARCHIVER_TRACE_PLACEHOLDER,
)
from app.extensions.apps.framework.spec import build_run_python_task
from app.extensions.apps.mysql_backups.recorder import RUN_RESULT_RECORDER
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.crud import TaskManager
from app.tasks.execution_request_secrets import (
    ARGS_LEAF,
    ENCRYPTED_META_KEYS,
    PAYLOAD_LEAF,
)
from app.tasks.models import (
    _encode_anonymize_mask,
    ANY_OWNER,
    DispatchLock,
    FileMetadata,
    LogCaptureStatusEnum,
    MAX_FAILURE_REASON_LENGTH,
    Task,
    TaskBackendEnum,
    TaskBase,
    TaskExecuteRequest,
    TaskExecutionRequest,
    TaskExecutionRequestJSON,
    TaskHistory,
    TaskHistoryBase,
    TaskHistoryLogState,
    TaskHistoryResponse,
    TaskHistoryStatusEnum,
    TaskLogType,
    TaskResponse,
    TaskStats,
    TaskWrite,
    TransformPayloadRequest,
)
from tests.app.factories import TaskFactory, TaskResponseFactory
from tests.app.tasks.conftest import HOOK_PATH_FIELDS, REJECTED_HOOK_PATHS

ENCODE_MASK_INT_INPUT = 42
FILE_METADATA_TEST_SIZE = 100
DURATION_SECONDS = 90.0
STATS_EXPECTED_TOTAL = 2
STATS_AVERAGE_SECONDS = 15.0
STATS_LAST_SECONDS = 20.0
STATS_TOTAL_SECONDS = 10.0


class TestTaskBackendEnum:
    """Test TaskBackendEnum values."""

    def test_nomad_value(self) -> None:
        """Assert NOMAD has the expected auto-generated value."""
        assert TaskBackendEnum.NOMAD == "nomad"

    def test_proxy_value(self) -> None:
        """Assert PROXY has the expected auto-generated value."""
        assert TaskBackendEnum.PROXY == "proxy"

    def test_celery_value(self) -> None:
        """Assert CELERY has the expected auto-generated value."""
        assert TaskBackendEnum.CELERY == "celery"

    def test_is_str_enum(self) -> None:
        """Assert values are strings."""
        assert isinstance(TaskBackendEnum.NOMAD, str)


class TestTaskHistoryStatusEnum:
    """Test TaskHistoryStatusEnum values and output-availability predicates."""

    def test_all_values_exist(self) -> None:
        """Assert all eight status values exist."""
        expected = {
            "FAILED",
            "PENDING",
            "RUNNING",
            "SUCCESS",
            "STOPPED",
            "LOST",
            "STALE",
            "UNLAUNCHABLE",
        }
        assert {s.name for s in TaskHistoryStatusEnum} == expected

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.STALE,
            TaskHistoryStatusEnum.UNLAUNCHABLE,
        ],
    )
    def test_is_finished_true(self, status: TaskHistoryStatusEnum) -> None:
        """Assert is_finished returns True for statuses with observed outcomes."""
        assert status.is_finished() is True

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.PENDING,
            TaskHistoryStatusEnum.RUNNING,
            TaskHistoryStatusEnum.LOST,
        ],
    )
    def test_is_finished_false(self, status: TaskHistoryStatusEnum) -> None:
        """Assert is_finished returns False when output retrieval is not meaningful."""
        assert status.is_finished() is False

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.LOST,
            TaskHistoryStatusEnum.STALE,
            TaskHistoryStatusEnum.UNLAUNCHABLE,
        ],
    )
    def test_is_terminal_true(self, status: TaskHistoryStatusEnum) -> None:
        """Assert is_terminal returns True for terminal statuses."""
        assert status.is_terminal() is True

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.PENDING,
            TaskHistoryStatusEnum.RUNNING,
        ],
    )
    def test_is_terminal_false(self, status: TaskHistoryStatusEnum) -> None:
        """Assert is_terminal returns False for active statuses."""
        assert status.is_terminal() is False

    @pytest.mark.parametrize("status", list(TaskHistoryStatusEnum))
    def test_every_member_is_classified(self, status: TaskHistoryStatusEnum) -> None:
        """Refuse a member that is neither terminal nor active.

        The two parametrized lists above are hand-enumerated, so a member added
        to the enum joins neither and is silently uncovered by them. Driving
        this off the enum itself names the offender instead, which is what
        keeps the published ``task_statuses`` vocabulary exhaustive.
        """
        assert status.is_terminal() != status.is_active(), (
            f"{status.value}: is_terminal={status.is_terminal()} "
            f"is_active={status.is_active()} -- expected exactly one"
        )

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (TaskHistoryStatusEnum.FAILED, "failed"),
            (TaskHistoryStatusEnum.LOST, "execution tracking lost"),
            (
                TaskHistoryStatusEnum.STALE,
                "skipped as stale (executor placement delayed past threshold)",
            ),
            (
                TaskHistoryStatusEnum.UNLAUNCHABLE,
                "could not be launched (the executor node cannot run the "
                "requested command)",
            ),
        ],
    )
    def test_operator_summary_prose(
        self, status: TaskHistoryStatusEnum, expected: str
    ) -> None:
        """Assert operator_summary returns the operator-facing prose per status."""
        assert status.operator_summary() == expected

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.SUCCESS,
            TaskHistoryStatusEnum.PENDING,
            TaskHistoryStatusEnum.RUNNING,
            TaskHistoryStatusEnum.STOPPED,
        ],
    )
    def test_operator_summary_none(self, status: TaskHistoryStatusEnum) -> None:
        """Assert operator_summary returns None for statuses carrying no prose."""
        assert status.operator_summary() is None


class TestTaskLogType:
    """Test TaskLogType enum values."""

    def test_stdout_value(self) -> None:
        """Assert STDOUT has value 'stdout'."""
        assert TaskLogType.STDOUT == "stdout"

    def test_stderr_value(self) -> None:
        """Assert STDERR has value 'stderr'."""
        assert TaskLogType.STDERR == "stderr"


class TestLogCaptureStatusEnum:
    """Cover LogCaptureStatusEnum values."""

    def test_values_are_spelled_explicitly(self) -> None:
        """Assert each member carries its literal wire value."""
        assert LogCaptureStatusEnum.COMPLETE == "complete"
        assert LogCaptureStatusEnum.INCOMPLETE == "incomplete"
        assert LogCaptureStatusEnum.UNKNOWN == "unknown"

    def test_membership_is_exact(self) -> None:
        """Assert the enum admits exactly the three capture verdicts."""
        assert {member.value for member in LogCaptureStatusEnum} == {
            "complete",
            "incomplete",
            "unknown",
        }


class TestTaskHistoryLogStateCaptureStatus:
    """Cover the capture_status column's two distinct defaults."""

    def test_new_rows_default_to_incomplete(self) -> None:
        """Assert a freshly constructed row starts pessimistic.

        A row exists before its stream is known to be drained, so the honest
        starting verdict is ``INCOMPLETE`` — it is upgraded once the drain
        converges.
        """
        state = TaskHistoryLogState(
            task_history_id=1,
            source="run-script",
            stream=TaskLogType.STDOUT,
        )

        assert state.capture_status == LogCaptureStatusEnum.INCOMPLETE

    def test_server_default_back_classifies_pre_existing_rows_as_unknown(self) -> None:
        """Assert the column's server default differs from the model default.

        SQLAlchemy always sends the column on INSERT, so the server default
        governs only the migration's backfill of rows written before the column
        existed. Those rows cannot distinguish "emitted nothing" from "emitted
        bytes that were never captured", so they are ``UNKNOWN`` — setting both
        defaults alike would silently misreport one of the two populations.
        """
        column = TaskHistoryLogState.__table__.columns["capture_status"]

        assert column.server_default.arg == LogCaptureStatusEnum.UNKNOWN.name
        assert column.nullable is False
        assert column.server_default.arg != LogCaptureStatusEnum.INCOMPLETE.name

    def test_server_default_is_readable_back_through_the_mapped_type(self) -> None:
        """Assert the server default is a string the column can actually read.

        ``EnumField`` persists member names, so a default spelled as a member
        *value* is written to every backfilled row and then raises
        ``LookupError`` on the first read — breaking the log writer and every
        history endpoint on a deployment that had rows before the upgrade.
        """
        column = TaskHistoryLogState.__table__.columns["capture_status"]

        assert column.server_default.arg in column.type.enums


class TestEncodeAnonymizeMask:
    """Test the _encode_anonymize_mask function."""

    def test_encode_set_of_pii_entities(self) -> None:
        """Assert encoding a set of PIIEntity produces correct bitmask."""
        entities = {PIIEntity.CREDIT_CARD, PIIEntity.EMAIL_ADDRESS}
        result = _encode_anonymize_mask(entities)
        assert result == PIIEntity.CREDIT_CARD | PIIEntity.EMAIL_ADDRESS

    def test_pass_through_int(self) -> None:
        """Assert an integer value passes through unchanged."""
        assert _encode_anonymize_mask(ENCODE_MASK_INT_INPUT) == ENCODE_MASK_INT_INPUT

    def test_type_error_returns_as_is(self) -> None:
        """Assert non-encodable input is returned as-is on TypeError."""
        result = _encode_anonymize_mask("not_encodable")
        assert result == "not_encodable"


class TestFileMetadata:
    """Test FileMetadata model."""

    def test_default_construction(self) -> None:
        """Assert default values are size=0 and is_dir=False."""
        meta = FileMetadata()
        assert meta.size == 0
        assert meta.is_dir is False

    def test_extra_fields_ignored(self) -> None:
        """Assert extra fields are silently ignored."""
        meta = FileMetadata(
            size=FILE_METADATA_TEST_SIZE, is_dir=True, unknown_field="ignored"
        )
        assert meta.size == FILE_METADATA_TEST_SIZE
        assert meta.is_dir is True
        assert not hasattr(meta, "unknown_field")


class TestTaskBaseValidation:
    """Test TaskBase.validate_data_for_backend validator."""

    def test_proxy_backend_without_task_raises(self) -> None:
        """Assert ValidationError when PROXY backend has no 'task' in data."""
        with pytest.raises(ValidationError, match="data must contain 'task'"):
            TaskBase(
                name="test",
                data={"something": "else"},
                backend=TaskBackendEnum.PROXY,
            )

    def test_proxy_backend_with_task_passes(self) -> None:
        """Assert PROXY backend with 'task' in data is valid."""
        task = TaskBase(
            name="test",
            data={"task": "inner-task"},
            backend=TaskBackendEnum.PROXY,
        )
        assert task.backend == TaskBackendEnum.PROXY

    def test_nomad_backend_without_task_passes(self) -> None:
        """Assert NOMAD backend does not require 'task' in data."""
        task = TaskBase(
            name="test",
            data={"something": "else"},
            backend=TaskBackendEnum.NOMAD,
        )
        assert task.backend == TaskBackendEnum.NOMAD

    def test_celery_backend_protected_passes(self) -> None:
        """Assert CELERY backend passes validation when protected."""
        task = TaskBase(
            name="test",
            data={"callable": "some.module.func", "target": "local"},
            backend=TaskBackendEnum.CELERY,
            protected=True,
        )
        assert task.backend == TaskBackendEnum.CELERY

    def test_celery_backend_unprotected_raises(self) -> None:
        """Assert CELERY backend raises when not protected."""
        with pytest.raises(ValidationError, match="protected"):
            TaskBase(
                name="test",
                data={"callable": "some.module.func", "target": "local"},
                backend=TaskBackendEnum.CELERY,
                protected=False,
            )


class TestTaskWriteHookPathValidation:
    """Cover the hook-path allow-list enforced on ``TaskWrite``."""

    @staticmethod
    def _write(**overrides: object) -> TaskWrite:
        """Build a minimal valid ``TaskWrite``, overriding the given fields.

        :param overrides: Field values to set on the model.
        :return: The constructed model.
        """
        return TaskWrite(name="hook-task", data={"task": "run-python"}, **overrides)

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    @pytest.mark.parametrize("path", REJECTED_HOOK_PATHS)
    def test_rejects_path_outside_allow_list(self, field: str, path: str) -> None:
        """Assert a hook path the allow-list denies is rejected at the write boundary."""
        with pytest.raises(ValidationError, match="app.extensions.apps"):
            self._write(**{field: path})

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    def test_rejection_names_the_field(self, field: str) -> None:
        """Assert the rejection message names the offending field."""
        with pytest.raises(ValidationError, match=field):
            self._write(**{field: "os:system"})

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    def test_accepts_allow_listed_path(self, field: str) -> None:
        """Assert an allow-listed hook path is accepted unchanged."""
        write = self._write(**{field: ALERT_DETAIL_BUILDER})

        assert getattr(write, field) == ALERT_DETAIL_BUILDER

    @pytest.mark.parametrize("field", HOOK_PATH_FIELDS)
    def test_accepts_none(self, field: str) -> None:
        """Assert an unset hook field stays None."""
        assert getattr(self._write(**{field: None}), field) is None

    @pytest.mark.parametrize("factory", [TaskFactory, TaskResponseFactory])
    def test_reading_back_a_stored_non_conforming_path_succeeds(
        self, factory: type[ModelFactory[Any]]
    ) -> None:
        """Assert the allow-list constrains writes only, never read-back.

        A row whose hook path predates the allow-list must still serialise.
        """
        legacy_path = "app.extensions.plugins.archives.alerts:build"

        instance = factory.build(alert_detail_builder=legacy_path)

        assert instance.alert_detail_builder == legacy_path


class TestTask:
    """Test Task model construction and properties."""

    def test_construction_with_required_fields(self) -> None:
        """Assert Task can be constructed with required fields."""
        task = TaskFactory.build(
            name="my-task",
            data={"key": "value"},
        )
        assert task.name == "my-task"
        assert task.data == {"key": "value"}

    def test_anonymized_entities_with_explicit_mask(self) -> None:
        """Assert anonymized_entities decodes an explicit bitmask."""
        mask = PIIEntity.CREDIT_CARD | PIIEntity.IP_ADDRESS
        task = TaskFactory.build(anonymize_mask=mask)
        assert task.anonymized_entities == {PIIEntity.CREDIT_CARD, PIIEntity.IP_ADDRESS}

    def test_anonymized_entities_with_none_mask(self) -> None:
        """Assert anonymized_entities falls back to anonymizer_settings defaults."""
        default_entities = {PIIEntity.EMAIL_ADDRESS, PIIEntity.PHONE_NUMBER}
        mock_defaults = defaultdict(lambda: default_entities)
        task = TaskFactory.build(anonymize_mask=None, owner="BACKUPS")
        with patch("app.tasks.models.anonymizer_settings") as mock_settings:
            mock_settings.DEFAULT_ENTITIES = mock_defaults
            result = task.anonymized_entities
        assert result == default_entities

    @pytest.mark.asyncio
    async def test_run_result_recorder_propagates_to_task_row(self, session) -> None:
        """Persist ``run_result_recorder`` from a ``TaskWrite`` onto the ``Task`` row."""
        recorder = RUN_RESULT_RECORDER
        write = TaskWrite.model_validate(
            TaskFactory.build(name="recorder-task", run_result_recorder=recorder)
        )
        task = await TaskManager.create(session, write)
        assert task.run_result_recorder == recorder


class TestTaskResponseAnonymizedEntities:
    """Test the anonymized_entities computed field on TaskResponse."""

    BASE_FIELDS: dict = {
        "id": 1,
        "name": "test-task",
        "data": {},
        "deleted_at": None,
        "created_by": None,
        "last_updated_by": None,
    }

    def test_explicit_mask_returns_sorted_entity_names(self) -> None:
        """Explicit anonymize_mask decodes to a sorted list of PIIEntity name strings."""
        mask = int(PIIEntity.CREDIT_CARD | PIIEntity.EMAIL_ADDRESS)
        response = TaskResponse.model_validate(
            {**self.BASE_FIELDS, "anonymize_mask": mask}
        )
        assert response.anonymized_entities == ["CREDIT_CARD", "EMAIL_ADDRESS"]

    def test_zero_mask_returns_empty_list(self) -> None:
        """anonymize_mask=0 decodes to an empty list (no entities set)."""
        response = TaskResponse.model_validate(
            {**self.BASE_FIELDS, "anonymize_mask": 0}
        )
        assert response.anonymized_entities == []

    def test_none_mask_falls_back_to_owner_defaults(self) -> None:
        """anonymize_mask=None falls back to anonymizer_settings.DEFAULT_ENTITIES[owner]."""
        default_entities = {PIIEntity.EMAIL_ADDRESS, PIIEntity.PHONE_NUMBER}
        mock_defaults = defaultdict(lambda: default_entities)
        fields = {
            **self.BASE_FIELDS,
            "owner": "CHECKSUMS",
            "anonymize_mask": None,
        }
        with patch("app.tasks.models.anonymizer_settings") as mock_settings:
            mock_settings.DEFAULT_ENTITIES = mock_defaults
            response = TaskResponse.model_validate(fields)
            result = response.anonymized_entities
        assert result == sorted(e.name for e in default_entities)


class TestTaskExecutionRequest:
    """Test TaskExecutionRequest model."""

    def test_default_values(self) -> None:
        """Assert default values for meta and tracking fields."""
        req = TaskExecutionRequest(task="backup", target="node-1")
        assert req.meta == {}
        assert req.tracking == {"allocation_id": None, "evaluation_id": None}

    def test_payload_content_plain_string(self) -> None:
        """Assert payload_content returns payload string when not a file path."""
        req = TaskExecutionRequest(task="t", target="n", payload="some content")
        assert req.payload_content == "some content"

    def test_payload_content_file_path_existing(self, tmp_path) -> None:
        """Assert payload_content reads file content for file:// paths."""
        payload_file = tmp_path / "payload.json"
        payload_file.write_text('{"key": "value"}')
        req = TaskExecutionRequest(
            task="t", target="n", payload=f"file://{payload_file}"
        )
        assert req.payload_content == '{"key": "value"}'

    def test_payload_content_unresolvable_file_raises(self) -> None:
        """Assert payload_content raises for an unresolvable file:// reference."""
        req = TaskExecutionRequest(
            task="t", target="n", payload="file:///nonexistent/path.json"
        )
        with pytest.raises(PayloadReferenceError):
            _ = req.payload_content

    def test_payload_content_none(self) -> None:
        """Assert payload_content returns None when payload is None."""
        req = TaskExecutionRequest(task="t", target="n")
        assert req.payload_content is None


class TestTaskExecuteRequest:
    """Test TaskExecuteRequest model and validators."""

    def test_default_values(self) -> None:
        """Assert default values for meta, payload, and eta."""
        req = TaskExecuteRequest()
        assert req.meta == {}
        assert req.payload is None
        assert req.eta is None

    def test_populate_meta_extracts_meta_prefixed_keys(self) -> None:
        """Assert keys with 'meta_' prefix are extracted into meta dict."""
        req = TaskExecuteRequest.model_validate({"meta_foo": "bar", "meta_baz": "qux"})
        assert req.meta == {"foo": "bar", "baz": "qux"}

    def test_populate_meta_merges_with_existing(self) -> None:
        """Assert meta_ keys merge with explicitly provided meta."""
        req = TaskExecuteRequest.model_validate(
            {"meta": {"existing": "val"}, "meta_new": "val2"}
        )
        assert req.meta == {"existing": "val", "new": "val2"}

    @pytest.mark.parametrize(
        "meta_value",
        ["oops", ["a"], None],
        ids=["string", "list", "null"],
    )
    def test_populate_meta_rejects_non_mapping(self, meta_value: Any) -> None:
        """Assert non-mapping meta alongside a meta_ key raises ValidationError."""
        with pytest.raises(ValidationError):
            TaskExecuteRequest.model_validate({"meta": meta_value, "meta_x": 1})

    def test_populate_meta_rejects_non_mapping_without_meta_key(self) -> None:
        """Assert bare non-mapping meta keeps Pydantic's field-scoped error."""
        with pytest.raises(ValidationError) as exc_info:
            TaskExecuteRequest.model_validate({"meta": "oops"})
        errors = exc_info.value.errors()
        assert errors[0]["loc"] == ("meta",)
        assert errors[0]["type"] == "dict_type"

    def test_empty_str_to_none_for_eta(self) -> None:
        """Assert empty string for eta is converted to None."""
        req = TaskExecuteRequest.model_validate({"eta": ""})
        assert req.eta is None

    def test_chain_task_names_valid_json_list(self) -> None:
        """Assert valid JSON list is parsed correctly."""
        req = TaskExecuteRequest(chain_task_names='["a","b"]')
        assert req.chain_task_names == ["a", "b"]

    def test_chain_task_names_invalid_json_falls_back(self) -> None:
        """Assert invalid JSON string is treated as a single task name."""
        req = TaskExecuteRequest(chain_task_names="[broken")
        assert req.chain_task_names == ["[broken"]

    def test_chain_task_names_empty_string_returns_none(self) -> None:
        """Assert empty string normalizes to None."""
        req = TaskExecuteRequest(chain_task_names="")
        assert req.chain_task_names is None

    def test_chain_task_names_empty_list_returns_none(self) -> None:
        """Assert empty list normalizes to None."""
        req = TaskExecuteRequest(chain_task_names=[])
        assert req.chain_task_names is None

    def test_chain_task_names_single_string(self) -> None:
        """Assert single string is wrapped in a list."""
        req = TaskExecuteRequest(chain_task_names="task-a")
        assert req.chain_task_names == ["task-a"]

    def test_chain_task_names_list_with_empties_filtered(self) -> None:
        """Assert empty strings are filtered from a list."""
        req = TaskExecuteRequest(chain_task_names=["a", "", "b"])
        assert req.chain_task_names == ["a", "b"]

    def test_chain_on_failure_defaults_to_false(self) -> None:
        """Assert chain_on_failure defaults to False."""
        req = TaskExecuteRequest()
        assert req.chain_on_failure is False

    @pytest.mark.parametrize("value", ["true", "on", "1", True])
    def test_chain_on_failure_truthy_values(self, value) -> None:
        """Assert truthy form values normalize to True."""
        req = TaskExecuteRequest(chain_on_failure=value)
        assert req.chain_on_failure is True

    @pytest.mark.parametrize("value", ["false", "off", "0", "", False])
    def test_chain_on_failure_falsy_values(self, value) -> None:
        """Assert falsy form values normalize to False."""
        req = TaskExecuteRequest(chain_on_failure=value)
        assert req.chain_on_failure is False


class TestTaskHistoryBase:
    """Test TaskHistoryBase computed fields."""

    def test_duration_with_both_timestamps(self) -> None:
        """Assert duration returns seconds when both timestamps are set."""
        started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        finished = datetime(2026, 1, 1, 12, 1, 30, tzinfo=UTC)
        req = TaskExecutionRequest(task="t", target="n")
        history = TaskHistoryBase(
            execution_request=req,
            started_at=started,
            finished_at=finished,
        )
        assert history.duration == DURATION_SECONDS

    def test_duration_missing_started_at(self) -> None:
        """Assert duration returns None when started_at is missing."""
        req = TaskExecutionRequest(task="t", target="n")
        history = TaskHistoryBase(
            execution_request=req,
            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert history.duration is None

    def test_duration_missing_finished_at(self) -> None:
        """Assert duration returns None when finished_at is missing."""
        req = TaskExecutionRequest(task="t", target="n")
        history = TaskHistoryBase(
            execution_request=req,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert history.duration is None

    def test_failure_reason_defaults_to_none(self) -> None:
        """Assert failure_reason defaults to None on a fresh history."""
        req = TaskExecutionRequest(task="t", target="n")
        assert TaskHistoryBase(execution_request=req).failure_reason is None


class TestSetFailureReason:
    """Test TaskHistory.set_failure_reason normalization and bounding."""

    @staticmethod
    def _history() -> TaskHistory:
        """Return a TaskHistory with no failure reason set."""
        return TaskHistory(
            id=1,
            task_id=1,
            execution_request=TaskExecutionRequest(task="t", target="n"),
        )

    def test_collapses_whitespace_to_one_line(self) -> None:
        """Assert a multi-line reason is collapsed to a single space-joined line."""
        history = self._history()
        history.set_failure_reason("Step failed\n  with   detail\nand more")
        assert history.failure_reason == "Step failed with detail and more"

    def test_truncates_past_the_bound(self) -> None:
        """Assert an over-long reason is truncated to the stored maximum."""
        history = self._history()
        history.set_failure_reason("x" * (MAX_FAILURE_REASON_LENGTH * 2))
        reason = history.failure_reason
        assert reason is not None
        assert len(reason) == MAX_FAILURE_REASON_LENGTH

    @pytest.mark.parametrize("reason", [None, "", "   ", "\n\t "])
    def test_blank_stores_none(self, reason: str | None) -> None:
        """Assert a None or blank reason is stored as None, never as an empty string."""
        history = self._history()
        history.set_failure_reason(reason)
        assert history.failure_reason is None

    def test_clears_a_previously_set_reason(self) -> None:
        """Assert passing None clears a reason recorded earlier."""
        history = self._history()
        history.set_failure_reason("The run failed.")
        history.set_failure_reason(None)
        assert history.failure_reason is None


class TestTaskHistory:
    """Test TaskHistory model properties and methods."""

    @pytest.fixture
    def task_instance(self) -> Task:
        """Return a Task instance for TaskHistory construction."""
        return TaskFactory.build(id=1, name="test-task", data={"key": "val"})

    @pytest.fixture
    def execution_request(self) -> TaskExecutionRequest:
        """Return a TaskExecutionRequest for TaskHistory construction."""
        return TaskExecutionRequest(
            task="test-task",
            target="node-1",
            tracking={"allocation_id": None, "evaluation_id": None},
        )

    def test_is_running_true(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert is_running returns True when status is RUNNING."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.RUNNING,
        )
        assert history.is_running is True

    def test_is_running_false(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert is_running returns False for non-RUNNING statuses."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        assert history.is_running is False

    def test_anonymized_entities(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert anonymized_entities decodes the bitmask correctly."""
        mask = PIIEntity.CREDIT_CARD | PIIEntity.PERSON
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            anonymize_mask=mask,
        )
        assert history.anonymized_entities == {PIIEntity.CREDIT_CARD, PIIEntity.PERSON}

    def test_anonymized_entities_none_mask_falls_back_to_task(
        self, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert a ``None`` history mask returns the associated task's entities."""
        task_mask = PIIEntity.EMAIL_ADDRESS | PIIEntity.IP_ADDRESS
        task = TaskFactory.build(
            id=1, name="test-task", data={"key": "val"}, anonymize_mask=task_mask
        )
        history = TaskHistory(
            id=1,
            task_id=task.id,
            task=task,
            execution_request=execution_request,
            anonymize_mask=None,
        )
        assert history.anonymized_entities == {
            PIIEntity.EMAIL_ADDRESS,
            PIIEntity.IP_ADDRESS,
        }
        assert history.anonymized_entities == task.anonymized_entities

    def test_anonymized_entities_none_mask_follows_task_owner_defaults(
        self, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert ``None`` on both history and task falls through to owner defaults."""
        default_entities = {PIIEntity.EMAIL_ADDRESS, PIIEntity.PHONE_NUMBER}
        mock_defaults = defaultdict(lambda: default_entities)
        task = TaskFactory.build(
            id=1,
            name="test-task",
            data={"key": "val"},
            anonymize_mask=None,
            owner="BACKUPS",
        )
        history = TaskHistory(
            id=1,
            task_id=task.id,
            task=task,
            execution_request=execution_request,
            anonymize_mask=None,
        )
        with patch("app.tasks.models.anonymizer_settings") as mock_settings:
            mock_settings.DEFAULT_ENTITIES = mock_defaults
            result = history.anonymized_entities
            assert result == default_entities
            assert result == task.anonymized_entities

    @pytest.mark.asyncio
    async def test_alert_for_status_failed(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert alert_for_status triggers ERROR alert with dedup key for FAILED."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            mock_trigger.assert_called_once()
            alert_data = mock_trigger.call_args[0][0]
            assert alert_data["severity"] == AlertSeverity.ERROR
            assert alert_data["class"] == "task_failure"
            assert "failed" in alert_data["summary"]
            assert alert_data["dedup_key"] == "task:test-task:node-1"

    @pytest.mark.asyncio
    async def test_alert_for_status_lost(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert alert_for_status triggers WARNING alert with dedup key for LOST."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.LOST,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            mock_trigger.assert_called_once()
            alert_data = mock_trigger.call_args[0][0]
            assert alert_data["severity"] == AlertSeverity.WARNING
            assert alert_data["class"] == "task_lost"
            assert alert_data["dedup_key"] == "task:test-task:node-1"

    @pytest.mark.asyncio
    async def test_alert_for_status_success_resolves(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert alert_for_status resolves both the base and stale alerts on SUCCESS."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        mock_trigger = AsyncMock()
        mock_resolve = AsyncMock()
        with (
            patch.object(AlertService, "trigger", mock_trigger),
            patch.object(AlertService, "resolve", mock_resolve),
        ):
            await history.alert_for_status()
            mock_trigger.assert_not_called()
            resolved_keys = [call.args[0] for call in mock_resolve.call_args_list]
            assert resolved_keys == [
                "task:test-task:node-1",
                "task:test-task:node-1:stale",
                "task:test-task:node-1:unlaunchable",
            ]

    @pytest.mark.asyncio
    async def test_alert_for_status_unlaunchable(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert UNLAUNCHABLE triggers its own WARNING alert under a suffixed key.

        A suffixed key keeps the incident distinct from a plain task failure
        while still scoping it to the same task/target pair — and the paired
        ``SUCCESS`` resolve above is what lets it ever clear.
        """
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.UNLAUNCHABLE,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            mock_trigger.assert_called_once()
            alert_data = mock_trigger.call_args[0][0]
            assert alert_data["severity"] == AlertSeverity.WARNING
            assert alert_data["class"] == "task_unlaunchable"
            assert alert_data["dedup_key"] == "task:test-task:node-1:unlaunchable"
            assert (
                "could not be launched (the executor node cannot run the "
                "requested command)" in alert_data["summary"]
            )
            assert "node-1" in alert_data["summary"]

    @pytest.mark.asyncio
    async def test_alert_for_status_stale(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert alert_for_status triggers WARNING task_stale alert with :stale suffix."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.STALE,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            mock_trigger.assert_called_once()
            alert_data = mock_trigger.call_args[0][0]
            assert alert_data["severity"] == AlertSeverity.WARNING
            assert alert_data["class"] == "task_stale"
            assert alert_data["dedup_key"] == "task:test-task:node-1:stale"
            assert (
                "skipped as stale (executor placement delayed past threshold)"
                in alert_data["summary"]
            )

    @pytest.mark.asyncio
    async def test_alert_for_status_stopped_no_action(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert alert_for_status takes no action for STOPPED status."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.STOPPED,
        )
        mock_trigger = AsyncMock()
        mock_resolve = AsyncMock()
        with (
            patch.object(AlertService, "trigger", mock_trigger),
            patch.object(AlertService, "resolve", mock_resolve),
        ):
            await history.alert_for_status()
            mock_trigger.assert_not_called()
            mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_for_status_dedup_key_consistent(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert dedup key is identical for FAILED and SUCCESS of same task."""
        failed_history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        success_history = TaskHistory(
            id=2,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        mock_trigger = AsyncMock()
        mock_resolve = AsyncMock()
        with (
            patch.object(AlertService, "trigger", mock_trigger),
            patch.object(AlertService, "resolve", mock_resolve),
        ):
            await failed_history.alert_for_status()
            await success_history.alert_for_status()
            trigger_dedup = mock_trigger.call_args[0][0]["dedup_key"]
            resolved_keys = [call.args[0] for call in mock_resolve.call_args_list]
            assert trigger_dedup in resolved_keys

    @staticmethod
    def _archiver_task(*, with_node: bool = True) -> Task:
        """Return an ARCHIVER task with archiver config in its payload meta."""
        meta = {
            "config": yaml.dump(
                {
                    "PURGE_LIST": [
                        {
                            "SOURCE_DB": "sbtest",
                            "SOURCE_TABLE": "sbtest2",
                            "WHERE": "k <= 2000",
                            "DEST_DB": "sbtest_archived",
                            "DEST_TABLE": "sbtest2",
                            "SWAP_DROP": 0,
                        }
                    ],
                }
            ),
            "target": "executor-host",
        }
        if with_node:
            meta["_pmm_node_name"] = "mvc-lab2-db1"
        return TaskFactory.build(
            id=1,
            name="test-task",
            owner="ARCHIVER",
            alert_detail_builder=ALERT_DETAIL_BUILDER,
            anonymize_mask=None,
            data={"task": "run-python", "meta": meta},
        )

    @pytest.mark.asyncio
    async def test_alert_for_status_failed_archiver(
        self, execution_request: TaskExecutionRequest, mocker
    ) -> None:
        """Use the source node in the summary and attach custom_details on failure."""
        history = TaskHistory(
            id=1075,
            task_id=1,
            task=self._archiver_task(),
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value="2026 ERROR: pt-archiver Purge Failed"),
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            alert_data = mock_trigger.call_args[0][0]
            # Source node in the summary, not the executor target.
            assert "mvc-lab2-db1" in alert_data["summary"]
            assert "failed" in alert_data["summary"]
            # dedup_key and source stay keyed on the executor target.
            assert alert_data["dedup_key"] == "task:test-task:node-1"
            assert alert_data["source"] == "test-task:1075:node-1"
            # Combined detail block carried in custom_details.
            desc = alert_data["custom_details"]["description"]
            assert "=== ERROR DETAILS ===" in desc
            assert "Purge Failed" in desc
            assert "Source: sbtest.sbtest2" in desc
            assert "Condition: k <= 2000" in desc
            assert "Target: sbtest_archived.sbtest2" in desc

    @pytest.mark.asyncio
    async def test_alert_for_status_failed_archiver_pmm_node_name_fallback(
        self, execution_request: TaskExecutionRequest, mocker
    ) -> None:
        """Fall back to the target in the summary without ``_pmm_node_name``."""
        history = TaskHistory(
            id=1075,
            task_id=1,
            task=self._archiver_task(with_node=False),
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value="ERROR: boom"),
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            alert_data = mock_trigger.call_args[0][0]
            assert "node-1" in alert_data["summary"]
            assert "custom_details" in alert_data

    @pytest.mark.asyncio
    async def test_alert_for_status_failed_archiver_empty_trace_placeholder(
        self, execution_request: TaskExecutionRequest, mocker
    ) -> None:
        """Render the placeholder for a missing STDERR trace, never an empty block."""
        history = TaskHistory(
            id=1075,
            task_id=1,
            task=self._archiver_task(),
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value=None),
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            desc = mock_trigger.call_args[0][0]["custom_details"]["description"]
            assert ARCHIVER_TRACE_PLACEHOLDER in desc

    @pytest.mark.asyncio
    async def test_alert_for_status_failed_non_archiver_unchanged(
        self, task_instance: Task, execution_request: TaskExecutionRequest, mocker
    ) -> None:
        """Give non-archiver failures no custom_details and keep the target summary."""
        read_spy = mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value="x"),
        )
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            alert_data = mock_trigger.call_args[0][0]
            assert "custom_details" not in alert_data
            assert "node-1" in alert_data["summary"]
        read_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_for_status_lost_archiver_unchanged(
        self, execution_request: TaskExecutionRequest, mocker
    ) -> None:
        """Leave Archiver LOST unchanged: no custom_details, no source-node summary."""
        read_spy = mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value="x"),
        )
        history = TaskHistory(
            id=1,
            task_id=1,
            task=self._archiver_task(),
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.LOST,
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            alert_data = mock_trigger.call_args[0][0]
            assert "custom_details" not in alert_data
            assert alert_data["class"] == "task_lost"
            assert "node-1" in alert_data["summary"]
            assert "mvc-lab2-db1" not in alert_data["summary"]
        read_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_for_status_archiver_uses_execution_snapshot_meta(
        self, mocker
    ) -> None:
        """Describe the failed execution's config, not a later-edited task.

        ``task.data["meta"]`` is mutable after dispatch, while
        ``execution_request.meta`` is the snapshot captured at dispatch. The
        failure alert must reflect the snapshot (source node + config).
        """
        snapshot_request = TaskExecutionRequest(
            task="test-task",
            target="node-1",
            meta={
                "config": yaml.dump(
                    {
                        "PURGE_LIST": [
                            {
                                "SOURCE_DB": "snap_db",
                                "SOURCE_TABLE": "snap_table",
                                "WHERE": "id < 10",
                                "DEST_DB": "snap_archived",
                                "DEST_TABLE": "snap_table",
                                "SWAP_DROP": 0,
                            }
                        ],
                    }
                ),
                "_pmm_node_name": "snapshot-node",
            },
            tracking={"allocation_id": None, "evaluation_id": None},
        )
        # ``_archiver_task`` carries the (now edited) live config/node.
        history = TaskHistory(
            id=1075,
            task_id=1,
            task=self._archiver_task(),
            execution_request=snapshot_request,
            status=TaskHistoryStatusEnum.FAILED,
        )
        mocker.patch(
            "app.extensions.apps.archives.alerts._read_last_stderr",
            new=AsyncMock(return_value="ERROR: boom"),
        )
        mock_trigger = AsyncMock()
        with patch.object(AlertService, "trigger", mock_trigger):
            await history.alert_for_status()
            alert_data = mock_trigger.call_args[0][0]
            desc = alert_data["custom_details"]["description"]
            # Snapshot config wins.
            assert "snapshot-node" in alert_data["summary"]
            assert "Source: snap_db.snap_table" in desc
            assert "Condition: id < 10" in desc
            assert "Target: snap_archived.snap_table" in desc
            # Edited live task config must not leak in.
            assert "mvc-lab2-db1" not in alert_data["summary"]
            assert "sbtest" not in desc


class TestTaskHistoryResponse:
    """Test ``TaskHistoryResponse`` serialization of the ``has_logs`` attribute."""

    @pytest.fixture
    def task_instance(self) -> Task:
        """Return a Task instance for TaskHistory construction."""
        return TaskFactory.build(id=1, name="test-task", data={"key": "val"})

    @pytest.fixture
    def execution_request(self) -> TaskExecutionRequest:
        """Return a TaskExecutionRequest for TaskHistory construction."""
        return TaskExecutionRequest(
            task="test-task",
            target="node-1",
            tracking={"allocation_id": None, "evaluation_id": None},
        )

    def test_has_logs_defaults_to_false(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert ``has_logs`` defaults to ``False`` when never populated."""
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )

        response = TaskHistoryResponse.model_validate(history)

        assert response.has_logs is False

    def test_log_capture_defaults_to_unknown(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert ``log_capture`` defaults to ``unknown`` when never populated.

        A history with no state rows has no evidence either way, so the reader
        is told the capture outcome is unknown rather than complete.
        """
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )

        response = TaskHistoryResponse.model_validate(history)

        assert response.log_capture == LogCaptureStatusEnum.UNKNOWN

    def test_has_logs_true_propagates_via_object_setattr(
        self, task_instance: Task, execution_request: TaskExecutionRequest
    ) -> None:
        """Assert ``object.__setattr__`` on the ORM instance propagates via ``model_validate``.

        ``TaskHistory`` is a strict Pydantic model so ``history.has_logs = True``
        raises ``ValueError``; routes use ``object.__setattr__`` to write the
        value directly into ``__dict__``. ``TaskHistoryResponse`` then reads it
        through the ``from_attributes=True`` path that SQLModel enables by
        default.
        """
        history = TaskHistory(
            id=1,
            task_id=task_instance.id,
            task=task_instance,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        object.__setattr__(history, "has_logs", True)

        response = TaskHistoryResponse.model_validate(history)

        assert response.has_logs is True


class TestTaskHistoryResponseDisplayName:
    """Test ``TaskHistoryResponse.display_name`` derivation."""

    @pytest.fixture
    def normal_task(self) -> Task:
        """Return a Task with a plain user-defined name."""
        return TaskFactory.build(id=1, name="backup-task", data={"key": "val"})

    @pytest.fixture
    def run_python_task(self) -> Task:
        """Return a run-python generic executor Task."""
        return TaskFactory.build(id=2, name="run-python", data={"key": "val"})

    @pytest.fixture
    def exec_artifact_task(self) -> Task:
        """Return an exec-artifact generic executor Task."""
        return TaskFactory.build(id=3, name="exec-artifact", data={"key": "val"})

    def _history(
        self, task: Task, execution_request: TaskExecutionRequest
    ) -> TaskHistoryResponse:
        history = TaskHistory(
            id=1,
            task_id=task.id,
            task=task,
            execution_request=execution_request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        return TaskHistoryResponse.model_validate(history)

    def test_normal_task_returns_task_name(self, normal_task: Task) -> None:
        """Assert a regular task returns its ``task.name`` as the display label."""
        req = TaskExecutionRequest(task="backup-task", target="node-1")
        assert self._history(normal_task, req).display_name == "backup-task"

    def test_proxy_over_a_generic_executor_is_classified_by_its_root(self) -> None:
        """Assert a proxy wrapping a generic executor still derives a per-run label.

        History binds to the dispatched task, so an app that wraps ``exec-artifact``
        to attach its own hooks would otherwise collapse every one of its runs onto
        the wrapper's single name.
        """
        proxy = TaskFactory.build(
            id=4,
            name="atw__exec-artifact",
            backend=TaskBackendEnum.PROXY,
            data={"task": "exec-artifact"},
        )
        req = TaskExecutionRequest(
            task="atw__exec-artifact",
            target="node-1",
            meta={"_snippet_filename": "snippets/collect.sh"},
        )

        assert self._history(proxy, req).display_name == "snippets/collect.sh on node-1"

    def test_framework_proxy_carrying_its_own_payload_keeps_its_name(self) -> None:
        """Assert a proxy that fixes its own payload is labelled by its own name.

        The framework's task apps wrap ``run-python``, a generic executor, but carry
        the payload themselves, so every run of one is the same configured job and
        its per-service name is the meaningful label. Classifying it by its root
        would relabel every backup or restore run as ``<dir>/<script> on <target>``.
        """
        write = build_run_python_task(
            name="backup_mongo__nightly-rs0",
            owner=ANY_OWNER,
            target="node-1",
            config="{}",
            requirements="",
            payload="file:///usr/src/extensions/scripts/backup_mongo/mongo_backup.py",
        )
        proxy = TaskFactory.build(id=6, **write.model_dump())
        req = TaskExecutionRequest(
            task=proxy.name,
            target="node-1",
            meta=dict(proxy.data["meta"]),
            payload=proxy.data["payload"],
        )

        assert self._history(proxy, req).display_name == "backup_mongo__nightly-rs0"

    def test_proxy_over_a_named_task_still_reports_its_own_name(self) -> None:
        """Assert a proxy over a non-generic root keeps its own name.

        Only a generic executor's runs need a derived label; a proxy wrapping any
        other task already carries the meaningful name.
        """
        proxy = TaskFactory.build(
            id=5,
            name="mysql-backup-svc-a",
            backend=TaskBackendEnum.PROXY,
            data={"task": "mysql-backup"},
        )
        req = TaskExecutionRequest(task="mysql-backup-svc-a", target="node-1")

        assert self._history(proxy, req).display_name == "mysql-backup-svc-a"

    def test_generic_executor_uses_underscore_snippet_filename_from_meta(
        self, run_python_task: Task
    ) -> None:
        """Assert ``_snippet_filename`` drives the label as ``<dir>/<file> on <target>``."""
        req = TaskExecutionRequest(
            task="run-python",
            target="node-1",
            meta={"_snippet_filename": "diag/slow-query.sh"},
        )
        assert (
            self._history(run_python_task, req).display_name
            == "diag/slow-query.sh on node-1"
        )

    def test_generic_executor_uses_legacy_snippet_filename_key(
        self, run_python_task: Task
    ) -> None:
        """Assert bare ``snippet_filename`` key is accepted when underscore variant absent."""
        req = TaskExecutionRequest(
            task="run-python", target="node-1", meta={"snippet_filename": "legacy.sh"}
        )
        assert self._history(run_python_task, req).display_name == "legacy.sh on node-1"

    def test_generic_executor_fallback_to_task_on_target(
        self, run_python_task: Task
    ) -> None:
        """Assert the fallback label is ``<task> on <target>`` when no snippet filename."""
        req = TaskExecutionRequest(task="run-python", target="node-1")
        assert (
            self._history(run_python_task, req).display_name == "run-python on node-1"
        )

    def test_generic_executor_file_payload_uses_source_dir_and_basename(
        self, exec_artifact_task: Task
    ) -> None:
        """Assert a ``file://`` payload yields ``<dir>/<file> on <target>``."""
        req = TaskExecutionRequest(
            task="exec-artifact",
            target="node-1",
            payload="file:///plugins/backup/script.sh",
        )
        assert (
            self._history(exec_artifact_task, req).display_name
            == "backup/script.sh on node-1"
        )

    def test_generic_executor_system_payload_without_snippet_filename(
        self, run_python_task: Task
    ) -> None:
        """Assert a system ``file://`` payload surfaces its owning directory."""
        req = TaskExecutionRequest(
            task="run-python",
            target="db-1",
            payload="file://app/tasks/connectivity/payload.py",
        )
        assert (
            self._history(run_python_task, req).display_name
            == "connectivity/payload.py on db-1"
        )

    def test_generic_executor_bare_snippet_borrows_source_dir_from_payload(
        self, run_python_task: Task
    ) -> None:
        """Assert a bare snippet filename borrows its directory from the payload path."""
        req = TaskExecutionRequest(
            task="run-python",
            target="db-2",
            meta={"_snippet_filename": "payload.py"},
            payload="file://app/extensions/sync/syncers/system_facts/payload.py",
        )
        assert (
            self._history(run_python_task, req).display_name
            == "system_facts/payload.py on db-2"
        )

    def test_generic_executor_non_file_payload_falls_back_to_task_on_target(
        self, exec_artifact_task: Task
    ) -> None:
        """Assert a non-file:// payload is ignored for basename derivation."""
        req = TaskExecutionRequest(
            task="exec-artifact", target="node-1", payload="inline-value"
        )
        assert (
            self._history(exec_artifact_task, req).display_name
            == "exec-artifact on node-1"
        )

    def test_empty_meta_does_not_crash(self, run_python_task: Task) -> None:
        """Assert an empty meta dict still yields a non-empty display name."""
        req = TaskExecutionRequest(task="run-python", target="node-1", meta={})
        result = self._history(run_python_task, req).display_name
        assert isinstance(result, str)
        assert len(result) > 0

    def test_none_meta_does_not_crash(self, run_python_task: Task) -> None:
        """Assert a None meta still yields a string display name."""
        req = TaskExecutionRequest(task="run-python", target="node-1", meta=None)
        result = self._history(run_python_task, req).display_name
        assert isinstance(result, str)


class TestTaskStats:
    """Test TaskStats computed fields."""

    @pytest.fixture
    def task_instance(self) -> Task:
        """Return a Task instance for TaskStats construction."""
        return TaskFactory.build(id=1, name="stats-task", data={"key": "val"})

    def _make_history(
        self,
        task: Task,
        status: TaskHistoryStatusEnum,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        history_id: int = 1,
    ) -> TaskHistory:
        """Return a TaskHistory instance with given parameters.

        :param task: The parent task.
        :type task: Task
        :param status: The execution status.
        :type status: TaskHistoryStatusEnum
        :param started_at: Start timestamp.
        :type started_at: datetime | None
        :param finished_at: Finish timestamp.
        :type finished_at: datetime | None
        :param history_id: The history record ID.
        :type history_id: int
        :return: A configured TaskHistory.
        :rtype: TaskHistory
        """
        return TaskHistory(
            id=history_id,
            task_id=task.id,
            task=task,
            execution_request=TaskExecutionRequest(task=task.name, target="n"),
            status=status,
            started_at=started_at,
            finished_at=finished_at,
        )

    def test_total_count(self, task_instance: Task) -> None:
        """Assert total returns the count of the tasks list."""
        h1 = self._make_history(
            task_instance, TaskHistoryStatusEnum.SUCCESS, history_id=1
        )
        h2 = self._make_history(
            task_instance, TaskHistoryStatusEnum.FAILED, history_id=2
        )
        stats = TaskStats(tasks=[h1, h2])
        assert stats.total == STATS_EXPECTED_TOTAL

    def test_status_counts(self, task_instance: Task) -> None:
        """Assert status counts pass/fail correctly."""
        h1 = self._make_history(
            task_instance, TaskHistoryStatusEnum.SUCCESS, history_id=1
        )
        h2 = self._make_history(
            task_instance, TaskHistoryStatusEnum.FAILED, history_id=2
        )
        h3 = self._make_history(
            task_instance, TaskHistoryStatusEnum.RUNNING, history_id=3
        )
        stats = TaskStats(tasks=[h1, h2, h3])
        assert stats.status == {"pass": 1, "fail": 1}

    def test_duration_values(self, task_instance: Task) -> None:
        """Assert duration computes average, last, and total seconds."""
        t1_start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t1_end = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
        t2_start = datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)
        t2_end = datetime(2026, 1, 1, 13, 0, 20, tzinfo=UTC)
        h1 = self._make_history(
            task_instance,
            TaskHistoryStatusEnum.SUCCESS,
            started_at=t1_start,
            finished_at=t1_end,
            history_id=1,
        )
        h2 = self._make_history(
            task_instance,
            TaskHistoryStatusEnum.SUCCESS,
            started_at=t2_start,
            finished_at=t2_end,
            history_id=2,
        )
        stats = TaskStats(tasks=[h1, h2])
        duration = stats.duration
        assert duration["average_seconds"] == STATS_AVERAGE_SECONDS
        assert duration["last_seconds"] == STATS_LAST_SECONDS
        assert duration["total_seconds"] == STATS_TOTAL_SECONDS

    def test_last_finished_at(self, task_instance: Task) -> None:
        """Assert last_finished_at returns the max finished_at timestamp."""
        t1_end = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
        t2_end = datetime(2026, 1, 1, 13, 0, 20, tzinfo=UTC)
        h1 = self._make_history(
            task_instance,
            TaskHistoryStatusEnum.SUCCESS,
            started_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            finished_at=t1_end,
            history_id=1,
        )
        h2 = self._make_history(
            task_instance,
            TaskHistoryStatusEnum.SUCCESS,
            started_at=datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC),
            finished_at=t2_end,
            history_id=2,
        )
        stats = TaskStats(tasks=[h1, h2])
        assert stats.last_finished_at == t2_end

    def test_empty_tasks_defaults(self) -> None:
        """Assert empty tasks list produces zero total and None durations."""
        stats = TaskStats(tasks=[])
        assert stats.total == 0
        assert stats.duration["average_seconds"] is None
        assert stats.duration["last_seconds"] is None
        assert stats.duration["total_seconds"] is None
        assert stats.last_finished_at is None


class TestTransformPayloadRequest:
    """Test TransformPayloadRequest validation."""

    def test_valid_construction(self) -> None:
        """Assert valid payload and fmt are accepted."""
        req = TransformPayloadRequest(payload="content", fmt="json")
        assert req.payload == "content"
        assert req.fmt == "json"

    @pytest.mark.parametrize("fmt", ["hcl", "json", "yaml"])
    def test_valid_formats(self, fmt: str) -> None:
        """Assert all allowed format values are accepted."""
        req = TransformPayloadRequest(payload="data", fmt=fmt)
        assert req.fmt == fmt

    def test_invalid_format_raises(self) -> None:
        """Assert invalid fmt raises ValidationError."""
        with pytest.raises(ValidationError):
            TransformPayloadRequest(payload="data", fmt="xml")


class TestDispatchLock:
    """Test DispatchLock model construction."""

    def test_construction(self) -> None:
        """Assert DispatchLock can be constructed with a name."""
        lock = DispatchLock(name="my-lock")
        assert lock.name == "my-lock"


class TestTaskExecutionRequestJSON:
    """Cover the column type's encrypt-on-write and decrypt-on-read hooks."""

    @staticmethod
    def _column() -> TaskExecutionRequestJSON:
        """Return the column type under test."""
        return TaskExecutionRequestJSON()

    @staticmethod
    def _request(**overrides: Any) -> TaskExecutionRequest:
        """Return an execution request carrying every protected leaf.

        :param overrides: Field values replacing the defaults.
        :return: The execution request.
        """
        fields = {
            "task": "run-python",
            "target": "node-1",
            "meta": {
                "args": "restore --password hunter2",
                "config": "master_password: hunter2\n",
                "_service_name": "mysql-1",
            },
            "payload": "secret document",
        }
        fields.update(overrides)
        return TaskExecutionRequest(**fields)

    def test_bind_encrypts_the_protected_leaves(self) -> None:
        """Assert the dumped document reaches storage with each leaf as ciphertext."""
        secret = "hunter2"
        assert secret in json.dumps(self._request().model_dump(mode="json")), (
            "the request must carry the secret before binding, or its absence "
            "below proves nothing"
        )

        stored = self._column().process_bind_param(self._request(), None)

        for key in ENCRYPTED_META_KEYS:
            assert is_encrypted(stored["meta"][key])
        assert is_encrypted(stored["payload"])
        assert stored["meta"]["_service_name"] == "mysql-1"
        assert secret not in json.dumps(stored)

    def test_bind_does_not_mutate_the_request(self) -> None:
        """Assert the live request the dedup comparison reads stays in plaintext."""
        request = self._request()

        self._column().process_bind_param(request, None)

        assert (request.meta or {})["args"] == "restore --password hunter2"
        assert request.payload == "secret document"

    def test_bind_does_not_mutate_a_plain_dict(self) -> None:
        """Assert the non-request branch copies before encrypting."""
        document = {
            "task": "run-python",
            "target": "node-1",
            "meta": {"args": "restore --password hunter2"},
            "payload": "secret document",
        }

        stored = self._column().process_bind_param(document, None)

        assert is_encrypted(stored["payload"])
        assert document["meta"]["args"] == "restore --password hunter2"
        assert document["payload"] == "secret document"

    def test_bind_leaves_an_already_encrypted_dict_byte_identical(self) -> None:
        """Assert re-saving the raw fallback document does not encrypt it twice.

        ``process_result_value`` hands back the stored document unchanged when it
        will not validate, so the dict branch can receive ciphertext. A second
        encryption there would leave a leaf no key can read, and the read path
        would report it as plaintext rather than as unreadable, so nothing
        downstream could tell.
        """
        column = self._column()
        stored = column.process_bind_param(self._request(), None)
        assert is_encrypted(stored["payload"]), (
            "the fixture must already be ciphertext, or the identity asserted "
            "below proves nothing"
        )

        resaved = column.process_bind_param(stored, None)

        assert resaved == stored
        assert json.loads(decrypt(resaved["payload"])) == "secret document"

    def test_bind_passes_none_through(self) -> None:
        """Assert a ``None`` column value is stored unchanged."""
        assert self._column().process_bind_param(None, None) is None

    def test_result_returns_plaintext_leaves(self) -> None:
        """Assert a stored row deserialises into a request carrying its plaintext."""
        column = self._column()
        stored = column.process_bind_param(self._request(), None)

        request = column.process_result_value(stored, None)

        assert isinstance(request, TaskExecutionRequest)
        assert (request.meta or {})["args"] == "restore --password hunter2"
        assert request.payload == "secret document"
        assert request.unreadable_leaves == ()

    def test_result_passes_a_legacy_plaintext_row_through(self) -> None:
        """Assert a row written before the migration ran still resolves."""
        request = self._column().process_result_value(
            {
                "task": "run-python",
                "target": "node-1",
                "meta": {"args": "restore --password hunter2"},
                "payload": "secret document",
            },
            None,
        )

        assert request.payload == "secret document"
        assert request.unreadable_leaves == ()

    def test_result_marks_the_leaf_it_cannot_decrypt(self) -> None:
        """Assert a foreign token is reported on the object rather than raised."""
        column = self._column()
        stored = column.process_bind_param(self._request(), None)
        token = Fernet(Fernet.generate_key()).encrypt(b"elsewhere").decode("ascii")
        stored["payload"] = token

        request = column.process_result_value(stored, None)

        assert request.unreadable_leaves == (PAYLOAD_LEAF,)
        assert request.payload == token
        assert (request.meta or {})["args"] == "restore --password hunter2"

    def test_result_returns_a_non_document_value_unchanged(self) -> None:
        """Assert a stored value that is not an execution request is handed back raw."""
        column = self._column()

        assert column.process_result_value("not-a-document", None) == "not-a-document"
        assert column.process_result_value(None, None) is None

    def test_bind_preserves_a_marked_leaf_byte_identically(self) -> None:
        """Assert re-saving an unreadable row keeps the only copy of its plaintext."""
        column = self._column()
        stored = column.process_bind_param(self._request(), None)
        token = Fernet(Fernet.generate_key()).encrypt(b"elsewhere").decode("ascii")
        stored["payload"] = token
        request = column.process_result_value(stored, None)

        resaved = column.process_bind_param(request, None)

        assert resaved["payload"] == token
        assert is_encrypted(resaved["meta"]["args"])
        assert decrypt(resaved["meta"]["args"]) == '"restore --password hunter2"'

    def test_payload_content_resolves_a_round_tripped_file_reference(
        self, tmp_path
    ) -> None:
        """Assert a ``file://`` payload still resolves after a storage round trip.

        :param tmp_path: The directory holding the referenced payload file.
        """
        payload_file = tmp_path / "payload.json"
        payload_file.write_text('{"key": "value"}')
        column = self._column()
        stored = column.process_bind_param(
            self._request(payload=f"file://{payload_file}"), None
        )

        request = column.process_result_value(stored, None)

        assert request.payload_content == '{"key": "value"}'


class TestTaskHistoryResponseUnreadableLeaves:
    """Cover how the response reports an execution request it could not decrypt."""

    @staticmethod
    def _response(
        request: TaskExecutionRequest, *, task_name: str = "backup-task"
    ) -> TaskHistoryResponse:
        """Return the response built from a history row carrying ``request``.

        :param request: The execution request the row carries.
        :param task_name: The parent task's name.
        :return: The validated response.
        """
        task = TaskFactory.build(id=1, name=task_name, data={"key": "val"})
        history = TaskHistory(
            id=1,
            task_id=task.id,
            task=task,
            execution_request=request,
            status=TaskHistoryStatusEnum.SUCCESS,
        )
        return TaskHistoryResponse.model_validate(history)

    def test_readable_row_reports_no_leaves(self) -> None:
        """Assert an ordinary row serialises its execution request unchanged."""
        request = TaskExecutionRequest(
            task="backup-task", target="node-1", meta={"args": "--fast"}
        )

        dumped = self._response(request).model_dump(mode="json")

        assert dumped["unreadable_request_leaves"] == []
        assert dumped["execution_request"]["meta"]["args"] == "--fast"

    def test_unreadable_leaf_serialises_as_null_and_is_listed(self) -> None:
        """Assert the token is replaced by ``None`` and the leaf is named."""
        request = TaskExecutionRequest(
            task="backup-task", target="node-1", meta={"args": "gAAAAAtoken"}
        )
        request.mark_unreadable_leaves((ARGS_LEAF,))

        dumped = self._response(request).model_dump(mode="json")

        assert dumped["unreadable_request_leaves"] == [ARGS_LEAF]
        assert dumped["execution_request"]["meta"]["args"] is None

    def test_a_readable_response_shares_the_row_s_request_object(self) -> None:
        """Pin why redaction must replace the field rather than blank it in place.

        Validating from an ORM row hands the response the row's own request
        object, so an in-place edit would destroy the ciphertext the write path
        has to preserve.
        """
        request = TaskExecutionRequest(task="backup-task", target="node-1")

        assert self._response(request).execution_request is request

    def test_redaction_leaves_the_source_request_untouched(self) -> None:
        """Assert redaction cannot corrupt the object a later write would persist."""
        request = TaskExecutionRequest(
            task="backup-task", target="node-1", payload="gAAAAAtoken"
        )
        request.mark_unreadable_leaves((PAYLOAD_LEAF,))

        response = self._response(request)

        assert request.payload == "gAAAAAtoken"
        assert response.execution_request is not request
        assert response.execution_request.payload is None

    def test_execution_request_keeps_its_declared_schema(self) -> None:
        """Assert redaction does not collapse the field's published type.

        Deriving the schema from a serializer's return type would publish a bare
        object here, and every generated client would lose the field's type.
        """
        schema = TaskHistoryResponse.model_json_schema()

        assert schema["properties"]["execution_request"] == {
            "$ref": "#/$defs/TaskExecutionRequest"
        }

    def test_inbound_indicator_survives_revalidation(self) -> None:
        """Assert a gateway re-validating a redacted body keeps the indicator.

        The leaf is already ``null`` by then, so nothing downstream can re-derive
        the indicator from the execution request.
        """
        request = TaskExecutionRequest(
            task="backup-task", target="node-1", meta={"args": "gAAAAAtoken"}
        )
        request.mark_unreadable_leaves((ARGS_LEAF,))
        upstream = self._response(request).model_dump(mode="json")

        revalidated = TaskHistoryResponse.model_validate(upstream)

        assert revalidated.unreadable_request_leaves == [ARGS_LEAF]

    def test_display_name_degrades_without_leaking_the_token(self) -> None:
        """Assert an unreadable ``file://`` payload falls back to the task label.

        A Fernet token matches no ``file://`` prefix, so the label loses the
        filename it used to carry rather than publishing the token.
        """
        token = Fernet(Fernet.generate_key()).encrypt(b"file://x.py").decode("ascii")
        request = TaskExecutionRequest(
            task="run-python", target="node-1", payload=token
        )
        request.mark_unreadable_leaves((PAYLOAD_LEAF,))

        display_name = self._response(request, task_name="run-python").display_name

        assert display_name == "run-python on node-1"
        assert token not in display_name
