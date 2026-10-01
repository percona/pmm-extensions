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

"""Define models for the Task API."""

import json
from datetime import datetime
from enum import auto, StrEnum
from functools import cached_property
from pathlib import Path
from statistics import mean
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    computed_field,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
)
from sqlalchemy import (
    BigInteger,
    Column,
    func,
    Index,
    JSON,
    LargeBinary,
    String,
    text,
    UniqueConstraint,
)
from sqlalchemy import Enum as EnumField
from sqlalchemy.orm import column_property
from sqlmodel import Field as SQLField
from sqlmodel import Relationship, SQLModel

from app.core.alerts.config import alert_service
from app.core.alerts.models import AlertSeverity
from app.core.db import BaseSQLModel
from app.core.db.models import DateTimeWithTimezone
from app.core.db.sql_types import AutoJSON, MaybeCompressedText
from app.core.utils.fields import (
    ARBITRARY_ARGS_SCHEMA,
    ArbitraryMapping,
    EmptyStrToNone,
    UTCDatetime,
)
from app.core.utils.path import resolve_payload_reference
from app.core.utils.strings import shorten_text
from app.tasks.alert_hooks import build_owner_alert_details
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.execution_request_secrets import (
    decrypt_request_leaves,
    encrypt_request_leaves,
    redact_request_leaves,
    reencrypt_request_leaves,
)
from app.tasks.hook_resolver import validate_hook_path
from app.tasks.task_status import TaskHistoryStatusEnum

TASK_ALIAS_LENGTH = 100
SYSTEM_USER = "SYSTEM"
ANY_OWNER = "ANY"


def _encode_anonymize_mask(v: Any) -> Any:
    """Encode the anonymize mask from a set of PII entities.

    If the input is a set of PIIEntity, it encodes it into an integer bitmask.
    Otherwise, it returns it as is.

    :param v: The input value to encode.
    :type v: Any
    :return: The encoded anonymize mask as an integer or the original input in case of
        a TypeError.
    :rtype: Any
    """
    try:
        return PIIEntity.encode_selection(v)
    except TypeError:
        return v


AnonymizeMask = Annotated[int, BeforeValidator(_encode_anonymize_mask)]


class FileMetadata(BaseModel):
    """Represent file metadata for task artifacts."""

    size: int = 0
    is_dir: bool = False

    model_config = ConfigDict(extra="ignore")


class ExecutorHostState(BaseModel):
    """Describe one executor host in more detail than "usable or absent".

    :meth:`app.tasks.execution.models.BaseExecutor.get_hosts` answers a yes/no
    question - can a job be placed here - by collapsing several conditions into
    presence in a mapping. That is the right answer for *dispatching*, and the wrong
    one for *reporting*: a host that is missing from it may never have been onboarded,
    or may be onboarded and down, or up with a broken driver, and those are three
    different jobs for whoever has to fix it.

    :param name: The host's name as the backend knows it.
    :param address: Its network address.
    :param reachable: Whether the backend currently considers it ready. ``False``
        covers several causes the backend does not distinguish here - down, stopped,
        never registered, or still initializing; see ``status`` for the precise one.
    :param driver_healthy: Whether it can actually run this executor's job type. A
        reachable host with an unhealthy driver is onboarded but broken - a different
        problem from one that was never onboarded, and the distinction this type
        exists for. When ``reachable`` is ``False`` this is the last-known value from
        before the host dropped, not a current fact.
    :param status: The backend's own word for its state, passed through unmapped so a
        reader can look it up in the backend's documentation.
    :param detail: Why the driver is unhealthy, or why the host is unreachable, when
        the backend says.
    """

    name: str
    address: str
    reachable: bool
    driver_healthy: bool
    status: str | None = None
    detail: str | None = None


class ExecutionEvent(BaseModel):
    """Represent a single lifecycle event from a task executor (executor-agnostic shape).

    :param timestamp: When the event occurred (UTC).
    :param event_type: Executor-specific event category (for example a Nomad
        task event type).
    :param description: Human-readable message for the event (no step prefix).
    :param step: Optional executor task/step name (for example a Nomad task
        within the group).
    """

    timestamp: UTCDatetime
    event_type: str = Field(
        serialization_alias="type",
        validation_alias="type",
    )
    description: str
    step: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class TaskBackendEnum(StrEnum):
    """Control the choice of backends.

    :cvar NOMAD: Enum value for Nomad backend.
    :vartype NOMAD: str
    :cvar PROXY: Enum value for Proxy backend.
    :vartype PROXY: str
    :cvar CELERY: Enum value for Celery backend.
    :vartype CELERY: str
    """

    NOMAD = auto()
    PROXY = auto()
    CELERY = auto()


class TaskLogType(StrEnum):
    """Define the type of task log.

    :cvar STDOUT: Enum value for standard output logs.
    :vartype STDOUT: str
    :cvar STDERR: Enum value for standard error logs.
    :vartype STDERR: str
    """

    STDOUT = "stdout"
    STDERR = "stderr"


class LogCaptureStatusEnum(StrEnum):
    """Describe how completely PMM Extensions captured a task's log stream.

    Distinguishes a stream that genuinely produced nothing from one whose bytes
    were lost before PMM Extensions could read them — the stored offsets alone cannot tell
    those apart, since both leave the cursors at zero.

    ``UNKNOWN`` is the honest verdict where no evidence survives: rows written
    before the column existed, and histories carrying no state rows at all.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


#: Severity order for reducing a task's per-stream verdicts to one. A single
#: lost stream makes the whole execution's logs untrustworthy, so ``INCOMPLETE``
#: outranks the rest; ``UNKNOWN`` outranks ``COMPLETE`` so an unclassified
#: stream is never rounded up into a clean bill of health.
CAPTURE_STATUS_PRECEDENCE: tuple[LogCaptureStatusEnum, ...] = (
    LogCaptureStatusEnum.INCOMPLETE,
    LogCaptureStatusEnum.UNKNOWN,
    LogCaptureStatusEnum.COMPLETE,
)


class TaskLog(BaseModel):
    """Define a task log line.

    :param step: The task step name.
    :type step: str
    :param type: The type of log to stream ('stdout' or 'stderr').
    :type type: TaskLogType
    :param msg: The log message. If None, represents the end of the log for that step.
    :type msg: str | None
    """

    step: str
    type: TaskLogType
    msg: str | None
    offset: int = 0


class TaskExecutionRequest(BaseModel):
    """Represent an execution request.

    :param task: The task name.
    :type task: str
    :param target: The target system or environment.
    :type target: str
    :param meta: Additional metadata for the task. Defaults to an empty dictionary.
    :param payload: Optional payload or file path for parameterizing the task.
        Defaults to None.
    :type payload: str | None
    :param tracking: Tracking information for task execution. Defaults to a dictionary
        with keys for allocation and evaluation IDs.
    """

    model_config = ConfigDict(extra="allow")
    task: str
    target: str
    meta: ArbitraryMapping | None = {}
    payload: str | None = None
    tracking: ArbitraryMapping | None = {"allocation_id": None, "evaluation_id": None}
    eta: datetime | None = None

    #: Dotted paths of the protected leaves the stored document could not be
    #: read into. Private so it never reaches ``model_dump``, and therefore
    #: neither the stored document nor an API response.
    _unreadable_leaves: tuple[str, ...] = PrivateAttr(default=())

    @cached_property
    def payload_content(self) -> str | None:
        """Return the payload content, resolving a ``file://`` reference to file text.

        If the payload is a ``file://`` reference, resolve it to an existing file
        via :func:`resolve_payload_reference` and return the file's contents.
        Otherwise return the payload string (possibly ``None``) unchanged.

        :return: The referenced file's contents, or the payload string as-is.
        :raises PayloadReferenceError: If a ``file://`` reference cannot be
            resolved to an existing file.
        """
        if self.payload and self.payload.strip().startswith("file://"):
            return resolve_payload_reference(self.payload).read_text()
        return self.payload

    @property
    def unreadable_leaves(self) -> tuple[str, ...]:
        """Return the protected leaves the stored document could not be read into.

        Empty on a request the route layer built, and on a stored row whose every
        protected leaf resolved. A non-empty value means those leaves still hold
        the stored ciphertext rather than a value: a re-save has to write them
        back unchanged, and a dispatch has to refuse.

        :return: The dotted paths of the leaves that could not be read.
        """
        return self._unreadable_leaves

    def mark_unreadable_leaves(self, leaves: tuple[str, ...]) -> None:
        """Record the protected leaves the stored document could not be read into.

        Set by the storage layer, which is the only place that knows a leaf
        arrived from the database rather than from a request body, which is the
        distinction the write path needs and cannot recover from the value.

        The marker describes the values the leaves held when the row was loaded,
        and nothing re-derives it on assignment. Replacing a marked leaf with a
        new plaintext without clearing the marker would have the write path spare
        that plaintext and store it in the clear; no caller does this today, and
        one that wants to must clear the marker in the same breath.

        :param leaves: The dotted paths of the leaves that could not be read.
        """
        self._unreadable_leaves = leaves


class TaskExecutionRequestJSON(AutoJSON):
    """Define JSON column type that deserializes values into ``TaskExecutionRequest``.

    Resolve to ``JSONB`` on PostgreSQL and ``JSON`` on other dialects via the
    inherited ``AutoJSON.load_dialect_impl``, and additionally wrap deserialised
    values in a ``TaskExecutionRequest`` instance when possible so ORM consumers
    see the typed object instead of a plain dict.

    :cvar cache_ok: Allow SQLAlchemy to cache compiled statements using this type.
    :vartype cache_ok: bool
    """

    cache_ok = True

    def process_result_value(self, value: Any, dialect: Any) -> Any:  # noqa: ARG002
        """Deserialize a JSON value into a ``TaskExecutionRequest``.

        Decrypt the credential-bearing leaves before construction, so every
        consumer above the storage layer reads plaintext and nothing else in the
        service changes. A leaf this deployment's ``ENCRYPTION_KEY`` cannot read
        is left as the stored ciphertext and named on the returned request
        instead of raising: this runs inside row loading for a column the
        task-history list routes undefer, so raising would fail a whole page over
        one row.

        :param value: The raw value from the database.
        :param dialect: The SQLAlchemy dialect in use.
        :return: A ``TaskExecutionRequest`` if valid, otherwise the raw value.
        """
        if value is None:
            return value
        unreadable: tuple[str, ...] = ()
        document = value
        if isinstance(value, dict):
            document, unreadable = decrypt_request_leaves(value)
        try:
            request = TaskExecutionRequest(**document)
        except (ValidationError, TypeError):
            return value
        request.mark_unreadable_leaves(unreadable)
        return request

    def process_bind_param(self, value: Any, dialect: Any) -> Any:  # noqa: ARG002
        """Serialize a ``TaskExecutionRequest`` into a dict for storage.

        Encrypt the credential-bearing leaves of the dumped document, except
        those a read already reported unreadable on this object: encrypting such
        a token a second time destroys the only copy of its plaintext, and a
        loaded row does get saved back. Which leaves to spare comes from the
        object's provenance, never from testing whether a value looks like
        ciphertext. A freshly submitted payload can legitimately be a
        well-formed token, and sparing that one would store a credential in the
        clear.

        A bare dict carries no provenance to read, so it takes the structural
        variant instead. That is the weaker rule the paragraph above rejects,
        and it is the right one here only because the dict branch cannot
        receive a fresh submission: every caller assigns a
        :class:`TaskExecutionRequest`, and the one value that arrives as a dict
        is the document :meth:`process_result_value` hands back when a stored
        row will not validate, which is already ciphertext. Encrypting it
        unconditionally would leave a leaf no key can read, reported by nothing.

        Both branches copy before rewriting, so the caller's live request (which
        the dispatch dedup comparison reads in the same request) and any plain
        dict handed to the column keep their plaintext.

        :param value: The value to store in the database.
        :param dialect: The SQLAlchemy dialect in use.
        :return: A dict representation suitable for JSON storage.
        """
        if value is None:
            return value
        if isinstance(value, TaskExecutionRequest):
            return encrypt_request_leaves(
                value.model_dump(mode="json"), preserve=value.unreadable_leaves
            )
        if isinstance(value, dict):
            return reencrypt_request_leaves(value)
        return value


class TaskBase(SQLModel):
    """Define the base structure for task-related operations.

    :param name: The name of the task.
    :param data: The task data stored in JSON format.
    :param backend: The backend used for task execution. Defaults to Nomad.
    :param owner: The owner of the task. Defaults to ``"ANY"``.
    :param is_template: Whether the task is a template. Defaults to False.
    :param protected: Whether the task is protected from deletion. Defaults to False.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the task. Defaults to None, meaning the entities
        defined in
        :attr:`app.tasks.anonymizer.config.AnonymizerSettings.DEFAULT_ENTITIES` will be
        used.
    :param alert_on_fail: Whether to trigger an alert on task failure and
        auto-resolve it on subsequent success. Defaults to False.
    :param alert_detail_builder: The ``"module:function"`` path of a plugin
        callable that enriches this task's failure alert, or None. Set by the
        owning plugin at task creation and resolved lazily by
        :func:`app.tasks.alert_hooks.build_owner_alert_details`; keeps plugin
        alert knowledge out of the tasks service. Defaults to None.
    :param run_result_recorder: The ``"module:function"`` path of a plugin
        callable that records this task's structured run result when it reaches a
        terminal status, or None. Set by the owning plugin at task creation and
        resolved lazily by :func:`app.tasks.run_result.maybe_record_run`; keeps
        plugin result-recording knowledge out of the tasks service. Defaults to
        None.
    :param output_files_path: The path, relative to the executor's working
        directory, where output files generated by the task are expected. Only
        files in this path are offered for user pulling, and it is where a run's
        structured result is read back from. Defaults to None, meaning no files
        will be available for pulling.
    """

    name: str = SQLField(min_length=1, max_length=255, unique=True, index=True)
    data: dict = SQLField(
        sa_column=Column(JSON, nullable=False),
        schema_extra={"json_schema_extra": {"additionalProperties": True}},
    )
    backend: TaskBackendEnum = SQLField(
        default=TaskBackendEnum.NOMAD,
        sa_column=Column(EnumField(TaskBackendEnum, native_enum=False), nullable=False),
    )
    owner: str = SQLField(
        default=ANY_OWNER,
        index=True,
    )
    is_template: bool = SQLField(default=False, index=True)
    protected: bool = False
    alert_on_fail: bool = False
    alert_detail_builder: str | None = None
    run_result_recorder: str | None = None
    output_files_path: str | None = None
    anonymize_mask: AnonymizeMask | None = None

    @model_validator(mode="after")
    def validate_data_for_backend(self) -> Self:
        """Validate the data dictionary against the selected backend.

        Enforce that Proxy tasks include a ``task`` key and that Celery tasks
        are marked as protected to prevent arbitrary code execution.

        :return: The validated instance.
        :rtype: Self
        :raises ValueError: If the backend is Proxy and ``task`` is not set in data,
            or if the backend is Celery and the task is not protected.
        """
        if self.backend == TaskBackendEnum.PROXY and not self.data.get("task"):
            raise ValueError("data must contain 'task' for Proxy backend")
        if self.backend == TaskBackendEnum.CELERY and not self.protected:
            raise ValueError("Celery tasks must be protected (system tasks only)")
        return self


class Task(TaskBase, BaseSQLModel, table=True):
    """Represent a task stored in the database.

    :param name: The name of the task.
    :param data: The task data stored in JSON format.
    :param backend: The backend used for task execution. Defaults to Nomad.
    :param owner: The owner of the task. Defaults to ``"ANY"``.
    :param is_template: Whether the task is a template. Defaults to False.
    :param protected: Whether the task is protected from deletion. Defaults to False.
    :param alert_on_fail: Whether to trigger an alert on task failure and
        auto-resolve it on subsequent success. Defaults to False.
    :param alert_detail_builder: The ``"module:function"`` path of a plugin
        callable that enriches this task's failure alert, or None.
    :param run_result_recorder: The ``"module:function"`` path of a plugin
        callable that records this task's structured run result at terminal
        status, or None.
    :param output_files_path: The path, relative to the executor's working
        directory, where output files generated by the task are expected, or
        None.
    :param history: The history of task executions.
    :param deleted_at: The deletion timestamp, if applicable.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the task. Defaults to 0 (no anonymization).
    :param created_by: The user ID of the user who created the task.
    :param last_updated_by: The user ID of the user who last modified the task.
    """

    __table_args__ = (
        Index("ix_task_deleted_at_owner", "deleted_at", "owner"),
        Index("ix_task_deleted_at_name", "deleted_at", "name"),
        Index(
            "ix_task_deleted_at_name_is_template",
            "deleted_at",
            "name",
            "is_template",
        ),
    )
    history: list["TaskHistory"] = Relationship(back_populates="task")
    deleted_at: UTCDatetime | None = SQLField(
        sa_type=DateTimeWithTimezone,  # ty: ignore[invalid-argument-type]
        default=None,
        index=True,
    )
    created_by: str | None = None
    last_updated_by: str | None = None

    @property
    def anonymized_entities(self) -> set[PIIEntity]:
        """Return the set of PII entities to be anonymized.

        :return: A set of PIIEntity to be anonymized.
        :rtype: set[PIIEntity]
        """
        return (
            PIIEntity.decode_selection(self.anonymize_mask)
            if self.anonymize_mask is not None
            else anonymizer_settings.DEFAULT_ENTITIES[self.owner]
        )


class TaskResponse(TaskBase, BaseSQLModel):
    """Represent a task API response.

    :param name: The name of the task.
    :param data: The task data stored in JSON format.
    :param backend: The backend used for task execution. Defaults to Nomad.
    :param owner: The owner of the task. Defaults to ``"ANY"``.
    :param is_template: Whether the task is a template. Defaults to False.
    :param protected: Whether the task is protected from deletion. Defaults to False.
    :param alert_on_fail: Whether to trigger an alert on task failure and
        auto-resolve it on subsequent success. Defaults to False.
    :param alert_detail_builder: The ``"module:function"`` path of a plugin
        callable that enriches this task's failure alert, or None.
    :param run_result_recorder: The ``"module:function"`` path of a plugin
        callable that records this task's structured run result at terminal
        status, or None.
    :param output_files_path: The path, relative to the executor's working
        directory, where output files generated by the task are expected, or
        None.
    :param deleted_at: The deletion timestamp, if applicable.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the task. Defaults to 0 (no anonymization).
    :param created_by: The user ID of the user who created the task.
    :param last_updated_by: The user ID of the user who last modified the task.
    :param anonymized_entities: Sorted list of PII entity names derived from
        ``anonymize_mask`` (or from the owner's configured defaults when the
        mask is ``None``). Each name is the raw ``PIIEntity`` member name
        (e.g. ``"EMAIL_ADDRESS"``). Read-only; computed on serialisation.
    """

    deleted_at: UTCDatetime | None
    created_by: str | None
    last_updated_by: str | None

    @computed_field
    @property
    def anonymized_entities(self) -> list[str]:
        """Return sorted PII entity names decoded from ``anonymize_mask``."""
        entities = (
            PIIEntity.decode_selection(self.anonymize_mask)
            if self.anonymize_mask is not None
            else anonymizer_settings.DEFAULT_ENTITIES[self.owner]
        )
        return sorted(entity.name for entity in entities)


class TaskWrite(TaskBase):
    """Define the model for creating new tasks.

    :param name: The name of the task.
    :param data: The task data stored in JSON format.
    :param backend: The backend used for task execution. Defaults to Nomad.
    :param owner: The owner of the task. Defaults to ``"ANY"``.
    :param is_template: Whether the task is a template. Defaults to False.
    :param protected: Whether the task is protected from deletion. Defaults to False.
    :param alert_on_fail: Whether to trigger an alert on task failure and
        auto-resolve it on subsequent success. Defaults to False.
    :param alert_detail_builder: The ``"module:function"`` path of a plugin
        callable that enriches this task's failure alert, or None. Must name a
        public callable in a module under one of the roots
        :attr:`app.tasks.config.TasksSettings.HOOK_MODULE_ALLOWLIST` lists;
        anything else is rejected.
    :param run_result_recorder: The ``"module:function"`` path of a plugin
        callable that records this task's structured run result at terminal
        status, or None. Constrained to the same allow-listed namespace as
        ``alert_detail_builder``.
    :param output_files_path: The path, relative to the executor's working
        directory, where output files generated by the task are expected, or
        None.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the task. Defaults to 0 (no anonymization).
    """

    @field_validator("alert_detail_builder", "run_result_recorder")
    @classmethod
    def validate_hook_path_allow_listed(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        """Reject a hook path outside the allow-listed module namespace.

        A hook path is imported and invoked by the tasks service, so an
        unconstrained value would let any caller who can write a task name an
        arbitrary callable. The check lives here rather than on
        :class:`TaskBase` so reading back a row whose stored path predates the
        allow-list keeps working.

        :param value: The candidate ``"module:function"`` path, or None.
        :param info: The validation context, carrying the field name.
        :return: The validated path, or None when the field is unset.
        :raises HookPathNotAllowedError: When the path is malformed or names a
            module outside the allow-listed namespace.
        """
        if value is None:
            return None
        return validate_hook_path(value, field=info.field_name or "hook path")


class TaskExecuteRequest(BaseModel):
    """Represent a request to execute a task with additional metadata and payload.

    :param meta: A dictionary of meta variables for the task execution.
        Defaults to an empty dictionary.
    :type meta: dict[str, Any]
    :param payload: Optional payload data or file path for the task execution.
        Defaults to None.
    :type payload: str | None
    :param eta: The earliest time the task can be executed. Defaults to None, meaning
        it will be executed as soon as possible.
    :type eta: datetime | None
    :param anonymize_mask: Bitmask of PII entities to anonymize. Defaults to None.
    :type anonymize_mask: int | None
    :param chain_task_names: Ordered list of task names to execute sequentially after
        this one completes. Defaults to None.
    :type chain_task_names: list[str] | None
    :param chain_on_failure: Whether the chain should continue when a task fails,
        stops, or is lost. Defaults to False (chain only on success).
    :type chain_on_failure: bool
    """

    meta: dict[str, Any] = Field(default={}, json_schema_extra=ARBITRARY_ARGS_SCHEMA)
    payload: str | None = None
    eta: datetime | EmptyStrToNone = None
    anonymize_mask: int | None = None
    chain_task_names: list[str] | None = None
    chain_on_failure: bool = False

    @field_validator("chain_on_failure", mode="before")
    @classmethod
    def normalize_chain_on_failure(cls, v: Any) -> bool:
        """Normalize form string values to a boolean.

        :param v: The raw chain_on_failure value.
        :type v: Any
        :return: The normalized boolean value.
        :rtype: bool
        """
        if isinstance(v, str):
            return v.lower() in ("true", "on", "1")
        return bool(v)

    @field_validator("chain_task_names", mode="before")
    @classmethod
    def normalize_chain_task_names(cls, v: Any) -> list[str] | None:
        """Normalize chain task names to a list or None.

        Accept a JSON-encoded list, a plain string (single task name), or
        a Python list. Normalize empty values to None.

        :param v: The raw chain_task_names value.
        :type v: Any
        :return: A list of task names or None.
        :rtype: list[str] | None
        """
        if isinstance(v, str):
            if not v:
                return None
            if v.startswith("["):
                try:
                    parsed = json.loads(v)
                except json.JSONDecodeError:
                    return [v]
                if isinstance(parsed, list):
                    filtered = [name for name in parsed if name]
                    return filtered or None
            return [v]
        if isinstance(v, list):
            filtered = [name for name in v if name]
            return filtered or None
        return v

    @model_validator(mode="before")
    @classmethod
    def populate_meta(cls, data: Any) -> Any:
        """Populate the meta field by extracting keys prefixed with 'meta_'.

        This method processes the input data to gather all keys starting with 'meta_'
        and incorporates them into the meta dictionary.

        :param data: The input data containing potential meta fields.
        :type data: Any
        :return: The modified data with the meta field populated.
        :rtype: Any
        :raises ValueError: If ``meta_``-prefixed keys are present but ``meta``
            is not a mapping, so Pydantic reports a 422 instead of a 500.
        """
        if isinstance(data, dict):
            prefixed = {
                key.replace("meta_", ""): value
                for key, value in data.items()
                if key.startswith("meta_")
            }
            if prefixed:
                meta = data.get("meta", {})
                if not isinstance(meta, dict):
                    msg = "meta must be a mapping"
                    raise ValueError(msg)
                meta.update(prefixed)
                data["meta"] = meta
        return data


#: Maximum stored length of ``TaskHistory.failure_reason``. Above every reason
#: PMM Extensions composes from fixed prose alone; the bound is for the three that
#: interpolate a value — the payload reference's error, a Nomad step name and
#: the resolved callable path — none of which is bounded at composition time.
MAX_FAILURE_REASON_LENGTH = 500


class TaskHistoryBase(SQLModel):
    """Define the base structure for a TaskHistory.

    :param execution_request: The request that triggered the task execution.
    :param status: The status of the task execution. Defaults to pending.
    :param started_at: The datetime when the task execution started.
    :param finished_at: The datetime when the task execution finished.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the execution. Defaults to None, meaning it uses
        the value defined in the associated task's :attr:`Task.anonymize_mask`.
    :param executed_by: The user ID of the user who executed the task.
    :param failure_reason: A single-line, operator-facing reason for the run's
        outcome, or None when the run did not fail or the reason is unknown.
        Written only through :meth:`TaskHistory.set_failure_reason`.
    """

    execution_request: TaskExecutionRequest = SQLField(
        sa_column=Column(TaskExecutionRequestJSON, nullable=False),
    )
    status: TaskHistoryStatusEnum = SQLField(
        default=TaskHistoryStatusEnum.PENDING,
        sa_column=Column(
            EnumField(TaskHistoryStatusEnum, native_enum=False, create_constraint=True),
            nullable=False,
            index=True,
        ),
    )
    started_at: UTCDatetime | None = SQLField(
        default=None,
        sa_type=DateTimeWithTimezone,  # ty: ignore[invalid-argument-type]
    )
    finished_at: UTCDatetime | None = SQLField(
        default=None,
        sa_type=DateTimeWithTimezone,  # ty: ignore[invalid-argument-type]
    )
    anonymize_mask: AnonymizeMask | None = None
    executed_by: str | None = None
    failure_reason: str | None = None

    @computed_field
    @property
    def duration(self) -> float | None:
        """Return the duration of the task execution in seconds.

        :return: The duration in seconds, or None if not available.
        :rtype: float | None
        """
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class TaskHistory(TaskHistoryBase, BaseSQLModel, table=True):
    """Represent a task execution history.

    :param execution_request: The request that triggered the task execution.
    :param status: The status of the task execution. Defaults to pending.
    :param started_at: The datetime when the task execution started.
    :param finished_at: The datetime when the task execution finished.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the execution. Defaults to None, meaning it uses
        the value defined in the associated task's :attr:`Task.anonymize_mask`.
    :param task_id: The ID of the task associated with the execution.
    :param task: The task associated with this execution history.
    :param sync_in_progress_started_at: Timestamp lock for a sync currently in progress.
    :param log_producer_epoch: Task-level high-water mark of the current
        producer epoch (for example a Nomad allocation ``CreateIndex``), stamped
        whenever the log frontier is reset. The log writer consults it on the
        first-insert path (before any per-stream ``TaskHistoryLogState`` row
        exists) to discard writes from a superseded producer. ``0`` is the
        legacy/unknown sentinel that is trusted unconditionally.
    :param executed_by: The user ID of the user who executed the task.
    :param failure_reason: A single-line, operator-facing reason for the run's
        outcome, or None when the run did not fail or the reason is unknown.
        Written only through :meth:`set_failure_reason`.
    """

    __table_args__ = (
        Index("ix_taskhistory_task_id_status", "task_id", "status"),
        Index(
            "ix_taskhistory_status_sync_in_progress_started_at",
            "status",
            "sync_in_progress_started_at",
        ),
    )
    task_id: int = SQLField(foreign_key="task.id", index=True)
    task: Task = Relationship(back_populates="history")
    sync_in_progress_started_at: UTCDatetime | None = SQLField(
        default=None,
        sa_type=DateTimeWithTimezone,  # ty: ignore[invalid-argument-type]
    )
    log_producer_epoch: int = SQLField(
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )

    @property
    def is_running(self) -> bool:
        """Check if the task is currently running.

        :return: True if the task status is RUNNING, False otherwise.
        """
        return self.status == TaskHistoryStatusEnum.RUNNING

    @property
    def anonymized_entities(self) -> set[PIIEntity]:
        """Return the set of anonymized PII entities.

        When :attr:`anonymize_mask` is ``None``, fall back to the associated
        task's :attr:`Task.anonymized_entities` (which itself falls back to the
        owner's configured defaults when the task has no mask).

        :return: A set of anonymized PIIEntity.
        :rtype: set[PIIEntity]
        """
        return (
            PIIEntity.decode_selection(self.anonymize_mask)
            if self.anonymize_mask is not None
            else self.task.anonymized_entities
        )

    def set_failure_reason(self, reason: str | None) -> None:
        """Normalize and store an operator-facing reason for this run's outcome.

        The single write path for :attr:`failure_reason`, so the stored value is
        always one line and always bounded however the caller composed it.
        Whitespace runs — including the newlines a multi-line composition can
        introduce — collapse to single spaces, and a value that is blank once
        stripped is stored as ``None`` rather than as an empty string.

        :param reason: The composed reason, or ``None`` to clear it.
        """
        if reason is None:
            self.failure_reason = None
            return
        collapsed = " ".join(reason.split())
        self.failure_reason = (
            shorten_text(collapsed, max_length=MAX_FAILURE_REASON_LENGTH)
            if collapsed
            else None
        )

    async def alert_for_status(self) -> None:
        """Trigger or resolve an alert based on the task execution status.

        Generate a deterministic dedup key from the task name and target so
        that PagerDuty deduplicates successive failures into a single incident
        and can resolve it when the task succeeds. The stale-skip and
        unlaunchable alerts each suffix that base key so they stay distinct
        incidents from a plain failure while still being scoped to the same
        task/target pair. Every suffixed key needs its own resolve on the
        ``SUCCESS`` arm — an unresolved suffixed incident never clears.
        """
        base_dedup_key = (
            f"task:{self.execution_request.task}:{self.execution_request.target}"
        )

        if self.status == TaskHistoryStatusEnum.SUCCESS:
            await alert_service.resolve(base_dedup_key)
            await alert_service.resolve(f"{base_dedup_key}:stale")
            await alert_service.resolve(f"{base_dedup_key}:unlaunchable")
            return

        owner_details = None
        summary_action = self.status.operator_summary()
        if self.status == TaskHistoryStatusEnum.FAILED:
            dedup_key = base_dedup_key
            severity = AlertSeverity.ERROR
            alert_class = "task_failure"
            owner_details = await build_owner_alert_details(self)
        elif self.status == TaskHistoryStatusEnum.LOST:
            dedup_key = base_dedup_key
            severity = AlertSeverity.WARNING
            alert_class = "task_lost"
        elif self.status == TaskHistoryStatusEnum.STALE:
            dedup_key = f"{base_dedup_key}:stale"
            severity = AlertSeverity.WARNING
            alert_class = "task_stale"
        elif self.status == TaskHistoryStatusEnum.UNLAUNCHABLE:
            dedup_key = f"{base_dedup_key}:unlaunchable"
            severity = AlertSeverity.WARNING
            alert_class = "task_unlaunchable"
        else:
            return

        # Archiver failures show the source database node in the summary, while
        # ``source``/``dedup_key`` stay keyed on the executor target so incidents
        # keep deduplicating and resolving (display-only).
        summary_node = (
            owner_details.source_node
            if owner_details
            else self.execution_request.target
        )
        alert_data = {
            "summary": (
                f"Task {self.execution_request.task!r} ({self.id}) "
                f"{summary_action} on node {summary_node!r}."
            ),
            "source": f"{self.execution_request.task}:{self.id}:{self.execution_request.target}",
            "severity": severity,
            "class": alert_class,
            "dedup_key": dedup_key,
        }
        # Carried as an extra on the base Alert (extra="allow"); consumed by
        # PagerDutyAlert.custom_details. Only archiver failures populate it.
        if owner_details:
            alert_data["custom_details"] = owner_details.custom_details
        await alert_service.trigger(alert_data)


TaskHistory.execution_request = column_property(
    TaskHistory.__table__.c.execution_request, deferred=True
)


class TaskHistoryLog(BaseSQLModel, table=True):
    """Store an append-only log chunk for a task history.

    :param task_history_id: The ID of the ``TaskHistory`` this chunk belongs to.
    :param source: The execution step name that produced the chunk (for example
        a Nomad task name).
    :param stream: The log stream (stdout or stderr) the chunk belongs to.
    :param start_offset: The user-facing byte offset at which this chunk starts.
    :param end_offset: The user-facing byte offset at which this chunk ends.
    :param content: The decoded chunk content.
    """

    __tablename__ = "taskhistory_log"
    __table_args__ = (
        UniqueConstraint(
            "task_history_id",
            "source",
            "stream",
            "start_offset",
            name="uq_taskhistory_log_chunk",
        ),
        Index(
            "ix_taskhistory_log_stream_end_offset",
            "task_history_id",
            "source",
            "stream",
            "end_offset",
        ),
    )

    task_history_id: int = SQLField(
        foreign_key="taskhistory.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    source: str = SQLField(sa_column=Column(String(64), nullable=False))
    stream: TaskLogType = SQLField(
        sa_column=Column(
            EnumField(TaskLogType, native_enum=False),
            nullable=False,
        ),
    )
    start_offset: int = SQLField(sa_column=Column(BigInteger, nullable=False))
    end_offset: int = SQLField(sa_column=Column(BigInteger, nullable=False))
    content: str = SQLField(sa_column=Column(MaybeCompressedText, nullable=False))


class TaskHistoryLogState(BaseSQLModel, table=True):
    """Hold the per-stream staging buffer and persisted offsets for the log writer.

    Inherits from ``BaseSQLModel`` (surrogate integer PK) and declares a
    unique constraint on ``(task_history_id, source, stream)`` — callers
    always look rows up by that natural tuple rather than by the surrogate id.

    :param task_history_id: The ID of the ``TaskHistory`` this state row tracks.
    :param source: The execution step name this state row tracks (for example a
        Nomad task name).
    :param stream: The log stream (stdout or stderr) this state row tracks.
    :param persisted_offset: The absolute user-facing byte offset already
        flushed into the chunk store.
    :param producer_offset: The producer-relative byte offset already consumed
        from the current producer epoch (resets when the producer switches, for
        example on a Nomad follow-up allocation). May diverge from
        ``persisted_offset`` after such a switch.
    :param producer_fetch_offset: The raw producer-space byte offset for the
        next log fetch (for example the ``offset=`` kwarg of a Nomad
        ``stream_logs.stream`` call). Producer-relative like
        ``producer_offset`` (resets on switch); kept durable here so a worker
        without the in-memory cursor resumes the fetch instead of re-reading
        the whole file from ``0``.
    :param producer_epoch: Monotonic producer-generation stamp that
        ``producer_fetch_offset`` / ``producer_offset`` belong to (for example
        a Nomad allocation ``CreateIndex``). ``0`` is the legacy/unknown
        sentinel (pre-migration rows and streams without a producer epoch)
        that the seed and write guards trust unconditionally.
    :param staging: Bytes pending flush to the chunk store.
    :param staging_updated_at: When ``staging`` was last modified; used to age
        out small buffers after ``MAX_AGE_SEC``.
    :param capture_status: How completely PMM Extensions captured this ``(source, stream)``
        pair. New rows start ``INCOMPLETE`` and are upgraded once the stream is
        drained to EOF; rows predating the column take ``UNKNOWN`` from the
        column's server default.
    :param version: Optimistic-locking version counter; incremented on every
        successful state update.
    """

    __tablename__ = "taskhistory_log_state"
    __table_args__ = (
        UniqueConstraint(
            "task_history_id",
            "source",
            "stream",
            name="uq_taskhistory_log_state_stream",
        ),
    )

    task_history_id: int = SQLField(
        foreign_key="taskhistory.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    source: str = SQLField(sa_column=Column(String(64), nullable=False))
    stream: TaskLogType = SQLField(
        sa_column=Column(
            EnumField(TaskLogType, native_enum=False),
            nullable=False,
        ),
    )
    persisted_offset: int = SQLField(
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )
    producer_offset: int = SQLField(
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )
    producer_fetch_offset: int = SQLField(
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )
    producer_epoch: int = SQLField(
        sa_column=Column(
            BigInteger,
            nullable=False,
            server_default="0",
        ),
    )
    staging: bytes = SQLField(
        sa_column=Column(
            LargeBinary,
            nullable=False,
            server_default=text("''"),
        ),
    )
    staging_updated_at: UTCDatetime = SQLField(
        sa_column=Column(
            DateTimeWithTimezone,
            nullable=False,
            server_default=func.now(),
        ),
    )
    capture_status: LogCaptureStatusEnum = SQLField(
        sa_column=Column(
            EnumField(LogCaptureStatusEnum, native_enum=False, create_constraint=True),
            nullable=False,
            # ``EnumField`` persists member *names*, so the server default must
            # be spelled as one: a value here writes a string the mapped type
            # cannot read back.
            server_default=LogCaptureStatusEnum.UNKNOWN.name,
        ),
        default=LogCaptureStatusEnum.INCOMPLETE,
    )
    version: int = SQLField(default=0, nullable=False)


GENERIC_EXECUTOR_TASK_NAMES: frozenset[str] = frozenset(
    {
        "run-python",
        "exec-artifact",
        "exec-python-artifact",
    }
)

INVENTORY_SYNC_TASK_NAME = "inventory-sync"
#: The Celery task every seeded or kicked ``inventory-sync`` run goes through.
EXECUTE_TASK_BY_NAME_TASK = "app.tasks.celery.execute_task_by_name"
#: Meta key naming the per-syncer schedules whose first run follows this one.
INVENTORY_SYNC_FOLLOWERS_KEY = "follower_syncers"
#: Meta key naming the pinned syncer a per-syncer schedule waits on.
INVENTORY_SYNC_AFTER_KEY = "after_syncer"
#: Meta key marking a leader-started run that goes ahead only as the syncer's first.
INVENTORY_SYNC_FIRST_RUN_KEY = "first_run_only"
INVENTORY_COLLECTION_TASK_NAME = "inventory-collection"
SYNC_RUNNING_TASKS_TASK_NAME = "tasks__sync_running_tasks"

#: Maintenance / system task names excluded from user-facing task lists.
#: The cert-expiry member is a literal matching
#: :data:`~app.tasks.execution.executors.nomad.constants.CHECK_NOMAD_CERT_EXPIRY_TASK_NAME`
#: so this module does not import the Nomad executor package.
INTERNAL_TASK_NAMES: frozenset[str] = frozenset(
    {
        INVENTORY_SYNC_TASK_NAME,
        INVENTORY_COLLECTION_TASK_NAME,
        SYNC_RUNNING_TASKS_TASK_NAME,
        "tasks__check_nomad_cert_expiry",
    }
)


class TaskHistoryResponse(TaskHistoryBase, BaseSQLModel):
    """Represent a task history API response.

    :param execution_request: The request that triggered the task execution.
    :param status: The status of the task execution.
    :param started_at: The datetime when the task execution started.
    :param finished_at: The datetime when the task execution finished.
    :param anonymize_mask: The bitmask representing PII entities to be anonymized in
        logs and files generated by the execution. Defaults to None, meaning it uses
        the value defined in the associated task's :attr:`Task.anonymize_mask`.
    :param task: The task associated with this execution history.
    :param executed_by: The user ID of the user who executed the task.
    :param has_logs: Whether this task history has any readable log content --
        either a chunk-store row or a legacy ``tracking["task_logs"]`` blob.
        Populated by list/retrieve routes; defaults to ``False``.
    :param log_capture: How completely PMM Extensions captured this execution's logs,
        aggregated over its state rows: any incomplete stream reports
        ``"incomplete"``, else any unknown reports ``"unknown"``, else
        ``"complete"``. Populated by list/retrieve routes; defaults to
        ``"unknown"``, which is also what a history carrying no state rows
        reports.
    :param display_name: A user-meaningful label derived from the task name or
        execution-request metadata. Read-only; computed on serialisation.
    :param failure_reason: A single-line, operator-facing reason for the run's
        outcome, or None when the run did not fail or the reason is unknown. A
        historic row predating the column reports None, which means "unknown"
        rather than "did not fail".
    :param unreadable_request_leaves: Dotted paths of the ``execution_request``
        leaves stored encrypted that this deployment's key could not read, for
        example ``["meta.args"]``. Each named leaf is serialised as ``null``
        rather than as the stored ciphertext, so a client shows the value as
        withheld instead of rendering an opaque token. Empty on every row that
        read cleanly, which is every row on a healthy deployment.
    """

    task: TaskResponse
    has_logs: bool = False
    log_capture: LogCaptureStatusEnum = LogCaptureStatusEnum.UNKNOWN
    unreadable_request_leaves: list[str] = []

    @model_validator(mode="after")
    def _resolve_unreadable_request_leaves(self) -> Self:
        """Derive the indicator from a loaded row, or keep one already resolved.

        A declared field rather than a ``computed_field`` because the PMM Extensions
        gateway re-validates this body into its own response model, and Pydantic
        does not accept a computed field on validation. By then the leaf is
        already ``null`` and the indicator cannot be re-derived, so a computed
        field would report an empty list for a row the tasks service flagged.
        An inbound non-empty value is therefore preserved as it stands.

        :return: This response, with the indicator resolved.
        """
        if not self.unreadable_request_leaves:
            self.unreadable_request_leaves = list(
                self.execution_request.unreadable_leaves
            )
        return self

    @model_validator(mode="after")
    def _redact_unreadable_request_leaves(self) -> Self:
        """Replace a leaf that could not be read with ``None``, not with its token.

        A *replacement* rather than an in-place edit, because a response validated
        from an ORM row shares that row's request object: blanking a leaf on it
        would destroy the ciphertext the write path has to preserve. It is also
        not a ``field_serializer``, which would be the obvious way to redact on
        the way out. Pydantic derives the field's published schema from the
        serializer's return type, so the OpenAPI ``$ref`` collapses to a bare
        object and every generated client loses the field's type.

        :return: This response, carrying a redacted execution request.
        """
        if self.unreadable_request_leaves:
            self.execution_request = TaskExecutionRequest(
                **redact_request_leaves(
                    self.execution_request.model_dump(mode="json"),
                    self.unreadable_request_leaves,
                )
            )
        return self

    @computed_field
    @property
    def display_name(self) -> str:
        """Return a user-meaningful display label for this task history row.

        For normal tasks, returns ``task.name``. For generic executor templates
        (``run-python``, ``exec-artifact``, ``exec-python-artifact``), builds a
        ``"<source>/<filename> on <target>"`` label so otherwise-identical rows
        are distinguishable: the filename comes from the snippet metadata or the
        ``file://`` payload basename, the source directory from whichever of those
        carries one, and the target from the execution request. Falls back to
        ``"<task> on <target>"`` when no filename is available.

        A ``PROXY`` task that leaves the payload to each dispatch is classified by the
        root it names, not by its own name, because history binds to the
        *dispatched* task: an app wrapping a generic executor to attach its own hooks
        would otherwise collapse every one of its runs onto the wrapper's single
        name. A proxy carrying its own ``payload`` is left alone, because
        ``prepare_task_history`` substitutes that payload into every run: it is a
        configured job, and its own name is the meaningful label. That is the shape
        of every proxy the framework builds over ``run-python``. Only the
        classification uses the root — a proxy over a non-generic task still reports
        its own name.

        :return: The display label for the task history entry.
        """
        task_name = self.task.name
        classified_as = task_name
        if (
            self.task.backend == TaskBackendEnum.PROXY
            and "payload" not in self.task.data
        ):
            classified_as = self.task.data.get("task") or task_name
        if classified_as not in GENERIC_EXECUTOR_TASK_NAMES:
            return task_name
        meta = self.execution_request.meta or {}
        snippet_fn = meta.get("_snippet_filename") or meta.get("snippet_filename")
        payload = self.execution_request.payload
        payload_path = (
            payload.removeprefix("file://")
            if payload and payload.startswith("file://")
            else None
        )
        target = self.execution_request.target
        source = snippet_fn or payload_path
        if not source:
            return f"{self.execution_request.task} on {target}"
        source_dir = Path(source).parent.name or (
            Path(payload_path).parent.name if payload_path else ""
        )
        leaf = Path(source).name
        label = f"{source_dir}/{leaf}" if source_dir else leaf
        return f"{label} on {target}"


class TaskStats(BaseModel):
    """Model for task statistics.

    :param engine: The backend engine used for task execution. Defaults to "nomad".
    :type engine: str
    :param tasks: A list of task execution histories.
    :type tasks: list[TaskHistory]
    """

    engine: str = "nomad"
    tasks: list[TaskHistory] = Field(default=[], exclude=True)

    _durations: dict = {
        "average_seconds": None,
        "last_seconds": None,
        "total_seconds": None,
        "tasks": {},
    }
    _raw: dict = {
        "durations": [],
        "finished_at": [],
    }

    @computed_field
    @property
    def total(self) -> int:
        """Return the total number of tasks.

        :return: The total number of tasks.
        :rtype: int
        """
        return len(self.tasks)

    @computed_field
    @property
    def status(self) -> dict[str, int]:
        """Return the task status summary.

        :return: A dictionary summarizing the number of passed and failed tasks.
        :rtype: dict[str, int]
        """
        status = dict.fromkeys(("pass", "fail"), 0)
        for task in self.tasks:
            match task.status:
                case TaskHistoryStatusEnum.FAILED:
                    status["fail"] += 1
                case TaskHistoryStatusEnum.SUCCESS:
                    status["pass"] += 1
                case _:
                    pass
        return status

    @computed_field
    @property
    def duration(self) -> ArbitraryMapping:
        """Return the task duration summary.

        :return: A dictionary summarizing average, last, and total task durations.
        """
        if self._durations["average_seconds"] is None:
            self._process()
        return ArbitraryMapping(self._durations)

    @computed_field
    @property
    def last_finished_at(self) -> str | None:
        """Return the last finished task timestamp.

        :return: The timestamp of the last task finished, or None if not available.
        """
        if not self._raw["finished_at"]:
            self._process()
        return max(self._raw["finished_at"]) if self._raw["finished_at"] else None

    def _process(self) -> None:
        """Process the task data."""

        def _durations_from_tracking() -> None:
            if task.duration is not None:
                self._durations["tasks"][task.id] = task.duration
                self._raw["durations"].append(task.duration)
            if task.finished_at is not None:
                self._raw["finished_at"].append(
                    task.finished_at,
                )

        # TODO:  # noqa: TD002, TD003
        #  - Refactor
        #  - handle extra backends
        #  - consider moving some logic to the TaskHistory model and then call from here
        for i, task in enumerate(self.tasks):
            if i == 0:
                self.engine = task.task.backend
            try:
                _durations_from_tracking()
            except KeyError:
                return
        if self._raw["durations"]:
            self._durations.update(
                average_seconds=mean(self._raw["durations"]),
                last_seconds=self._raw["durations"].pop(),
                total_seconds=sum(self._raw["durations"]),
            )


LATEST_HISTORY_STATUS_NAMES_MAX = 200


class TaskHistoryLatestStatusRequest(BaseModel):
    """Define request body for batch latest-history status lookup.

    :param names: Task names to resolve latest non-null history statuses for.
    :type names: list[str]
    """

    names: list[str] = Field(
        default_factory=list,
        max_length=LATEST_HISTORY_STATUS_NAMES_MAX,
    )


class TaskHistoryLatestStatus(BaseModel):
    """Represent the latest-history projection for a single task.

    :param status: The latest known execution status, taken from the newest
        history row; ``None`` only when the task has no history rows at all.
    :param finished_at: The most recent ``finished_at`` across all of the task's
        history rows (a ``max``), so a task with an in-progress re-run still
        reports its prior completion; ``None`` when no run has ever finished.
    """

    status: TaskHistoryStatusEnum | None = None
    finished_at: UTCDatetime | None = None


class TaskHistoryStatusPoint(BaseModel):
    """Represent one system-triggered run observation for a task name.

    :param created_at: When the history row was recorded.
    :param status: The recorded execution status.
    """

    created_at: UTCDatetime
    status: TaskHistoryStatusEnum


class TransformPayloadRequest(BaseModel):
    """Define the request body for the /transform/ API route.

    :param payload: The job specification payload to be parsed.
    :type payload: str | bytes
    :param fmt: The format of the payload, which can be "hcl", "json", or "yaml".
    :type fmt: Literal["hcl", "json", "yaml"]
    """

    payload: str | bytes
    fmt: Literal["hcl", "json", "yaml"]


class TransformPayloadResponse(BaseModel):
    """Define the parsed job specification returned from ``POST /transform/``.

    The concrete keys depend on the executor backend; clients should treat values as
    untyped JSON except where they validate domain-specific job fields themselves.
    """

    model_config = ConfigDict(extra="allow")


class DispatchLock(BaseSQLModel, table=True):
    """Define a task dispatch lock.

    :param name: The name of the lock. Must be unique.
    :type name: str
    """

    name: str = SQLField(max_length=255, index=True, unique=True)
