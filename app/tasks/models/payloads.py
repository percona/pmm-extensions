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

"""Define the executor payload models and their JSON column type."""

from collections.abc import Iterable
from datetime import datetime
from functools import cached_property
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
)

from app.core.db.sql_types import AutoJSON
from app.core.utils.fields import ArbitraryMapping, UTCDatetime
from app.core.utils.path import resolve_payload_reference
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.execution_request_secrets import (
    decrypt_request_leaves,
    encrypt_request_leaves,
    redact_request_leaves_for_log,
    reencrypt_request_leaves,
)
from app.tasks.models.constants import REQUIREMENTS_META_KEY, STORED_REQUEST_CONTEXT
from app.tasks.models.enums import TaskLogType


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


def normalize_requirements_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``meta`` with ``requirements`` in the newline-joined string form.

    A string passes through unchanged. A list of non-empty strings is joined with
    newlines, the form the ``run-python`` job writes into ``requirements.txt``.
    The input mapping is never mutated: a changed value comes back in a copy.

    :param meta: The execution meta, possibly ``None``.
    :return: ``meta`` itself when nothing changes, otherwise a copy carrying the
        joined string.
    :raises ValueError: If ``requirements`` is neither a string nor a list, or
        the list holds anything but non-empty strings.
    """
    if not meta or REQUIREMENTS_META_KEY not in meta:
        return meta
    value = meta[REQUIREMENTS_META_KEY]
    if isinstance(value, str):
        return meta
    if isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    ):
        return {**meta, REQUIREMENTS_META_KEY: "\n".join(value)}
    msg = (
        f"meta.{REQUIREMENTS_META_KEY} must be a string or a list of non-empty strings"
    )
    raise ValueError(msg)


def _normalize_requirements_field(
    meta: dict[str, Any] | None, info: ValidationInfo
) -> dict[str, Any] | None:
    """Normalize ``meta`` for a ``meta`` field validator, sparing stored reads.

    A model validated with :data:`STORED_REQUEST_CONTEXT` is a read of stored
    data, so a value written before ``requirements`` was checked comes back
    unchanged instead of failing the read.

    :param meta: The validated meta mapping, possibly ``None``.
    :param info: The validation info carrying the optional context.
    :return: ``meta`` with ``requirements`` as a string, or ``meta`` unchanged
        when a stored read holds a value that cannot be normalized.
    :raises ValueError: If ``requirements`` cannot be normalized and the model is
        not being validated as a stored read.
    """
    try:
        return normalize_requirements_meta(meta)
    except ValueError:
        if info.context == STORED_REQUEST_CONTEXT:
            return meta
        raise


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
    :param target: The target system or environment.
    :param meta: Additional metadata for the task. Defaults to an empty dictionary.
    :param payload: Optional payload or file path for parameterizing the task.
        Defaults to ``None``.
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

    @field_validator("meta")
    @classmethod
    def normalize_requirements(
        cls, v: dict[str, Any] | None, info: ValidationInfo
    ) -> dict[str, Any] | None:
        """Store and dispatch ``meta.requirements`` only as a newline-joined string.

        Every execution path - an ad-hoc execute call, a stored task's merged
        ``data.meta``, a chained task, an internal dispatch - builds this model
        before the request is stored or handed to an executor, so normalizing
        here keeps task history and Nomad dispatch meta to one shape.

        A request read back from storage (validated with
        :data:`STORED_REQUEST_CONTEXT`) keeps a value it cannot normalize rather
        than failing: a row written before the value was checked must still load,
        or one such row would fail every history page holding it.

        :param v: The validated meta mapping.
        :param info: The validation info carrying the optional context.
        :return: The meta with ``requirements`` as a string.
        """
        return _normalize_requirements_field(v, info)

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield the fields ``repr`` and ``str`` render, protected leaves redacted.

        A request reaches a log line whenever it, or a history row or response
        holding it, is interpolated into a message. Redacting here covers every
        such call at once, while ``model_dump`` keeps returning the real values
        the executor and the storage layer read.

        :return: Each field name paired with the value to render for it.
        """
        redacted = redact_request_leaves_for_log(
            {"meta": self.meta, "payload": self.payload}
        )
        for name, value in super().__repr_args__():
            yield name, redacted.get(name, value)

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
            request = TaskExecutionRequest.model_validate(
                document, context=STORED_REQUEST_CONTEXT
            )
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
