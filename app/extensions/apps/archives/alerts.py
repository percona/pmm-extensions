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

"""Build archiver-specific detail blocks for task failure alerts.

This module owns the archiver domain knowledge behind a failure alert: the
``PURGE_LIST`` -> (source, condition, target) mapping, the STDERR error-trace
extraction, the credential redaction applied before the detail egresses to an
external alerting provider, and the assembly of the owner-specific alert
additions. The generic tasks service consults it lazily through the
``app.tasks.alert_hooks`` resolver, following the ``"module:function"`` path the
archiver task carries in ``alert_detail_builder`` (stamped at creation from
:data:`ALERT_DETAIL_BUILDER`), so this knowledge stays inside the plugin package
rather than leaking into ``app/tasks``.

The module depends only on ``app.tasks`` (the allowed direction) and is safe to
import standalone, so the lazy hook resolves identically in the Celery worker,
the PMM Extensions app, and the tasks API. The archiver config parser here is the single
source of truth for the purge-field mapping; the archives ``deps`` layer imports
it rather than duplicating the field extraction.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from app.tasks.alert_hooks import OwnerAlertDetails
from app.tasks.anonymizer import anonymize_text
from app.tasks.anonymizer.entities import PIIEntity

if TYPE_CHECKING:
    from app.tasks.models import TaskHistory

logger = logging.getLogger(__name__)

#: Importable ``"module:function"`` path of this module's failure-alert builder.
#: The archives ``deps`` layer stamps it onto the archiver task
#: (``TaskWrite.alert_detail_builder``) at creation so the tasks service can
#: resolve the builder lazily without statically importing the plugin.
ALERT_DETAIL_BUILDER = f"{__name__}:build_owner_alert_details"

#: Substring marking an error line in the archiver's STDERR stream. Used by the
#: tail reader to ensure the last error block is fully captured.
STDERR_ERROR_MARKER = "ERROR"

#: Minimum trailing STDERR bytes to accumulate before stopping the reverse scan
#: once an error marker has been seen. Covers the downstream trace cap
#: (:data:`MAX_TRACE_BYTES`) so an error block straddling a chunk boundary is
#: fully present in the reconstructed tail.
_STDERR_TAIL_MIN_BYTES = 4 * 1024

#: Shown for the Error Details section when no trace can be extracted; the Error
#: Details block is never silently null/empty.
ARCHIVER_TRACE_PLACEHOLDER = "No error trace captured from STDERR."

#: Shown for a configuration field that cannot be resolved from the task config.
ARCHIVER_FIELD_PLACEHOLDER = "unavailable"

#: Hard cap on the emitted error trace, protecting the downstream alert payload.
MAX_TRACE_BYTES = 4096

#: Replacement token substituted for any redacted credential.
_REDACTION_MASK = "***"

#: ``scheme://user[:password]@`` userinfo prefix of a connection URI.
_URI_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]+@")

#: Sensitive ``key=value`` / ``key: value`` credential pairs. Covers query-string
#: and config forms (``password``/``passwd``/``pwd``).
_KV_SECRET_RE = re.compile(
    r"(?i)(?P<key>password|passwd|pwd)\s*[=:]\s*(?P<val>[^&\s,;\"']+)"
)

#: pt-archiver / Perl-DBI DSN password component ``p=<val>``. Case-sensitive: in a
#: DBI DSN lowercase ``p`` is the password while uppercase ``P`` is the port, so
#: only lowercase ``p`` is masked. The look-behind avoids matching the tail of a
#: longer token (e.g. ``...,p=`` matches, ``help=`` does not).
_DSN_PASSWORD_RE = re.compile(r"(?<![A-Za-z0-9_])(?P<key>p)=(?P<val>[^,\s\"']+)")

#: CLI password flags: ``--password=val``, ``--password val``, ``-pVAL``. The
#: ``-p`` short flag is anchored to an argument boundary (look-behind) and
#: case-sensitive so it masks only a real ``mysql -pSECRET`` token, not the
#: ``-p`` inside ``--purge``/``--progress``/``--output-path`` nor the ``-P``
#: (port) flag. ``--password`` stays case-insensitive.
_CLI_PASSWORD_RE = re.compile(
    r"(?P<flag>(?i:--password)(?:[=\s])|(?<![\w./-])-p(?=\S))(?P<val>[^\s\"']+)"
)


def redact_secrets(text: str) -> str:
    """Mask credentials embedded in a free-text error trace.

    Runs unconditionally (independent of the opt-in PII mask) so a connection
    string or password echoed by pt-archiver/MySQL into STDERR cannot leave the
    platform in a failure alert. Masks: URI userinfo (``scheme://user:pass@``),
    ``password``/``passwd``/``pwd`` key/value pairs, the Perl-DBI DSN ``p=``
    component, and the ``--password``/``-p`` CLI flags.

    :param text: The text that may contain credentials.
    :return: The text with any detected credential replaced by ``***``.
    """
    text = _URI_USERINFO_RE.sub(rf"\g<scheme>{_REDACTION_MASK}@", text)
    text = _KV_SECRET_RE.sub(rf"\g<key>={_REDACTION_MASK}", text)
    text = _DSN_PASSWORD_RE.sub(rf"\g<key>={_REDACTION_MASK}", text)
    return _CLI_PASSWORD_RE.sub(rf"\g<flag>{_REDACTION_MASK}", text)


@dataclass(frozen=True, slots=True)
class ArchiverPurgeFields:
    """Hold the archiver fields extracted from a ``PURGE_LIST`` entry.

    :param source_db: The source database name (``SOURCE_DB``).
    :param source_table: The source table name (``SOURCE_TABLE``).
    :param where: The archiving ``WHERE`` condition.
    :param dest_db: The destination database name (``DEST_DB``).
    :param dest_table: The destination table name (``DEST_TABLE``).
    :param dest_file: The destination file/storage path (``DEST_FILE``).
    :param source_query: An optional source query (``SOURCE_QUERY``).
    """

    source_db: str | None
    source_table: str | None
    where: str | None
    dest_db: str | None
    dest_table: str | None
    dest_file: str | None
    source_query: str | None

    @property
    def source(self) -> str | None:
        """Return the ``SOURCE_DB.SOURCE_TABLE`` display string.

        :return: The composed source identifier, or ``None`` when either part
            is missing.
        """
        if self.source_db and self.source_table:
            return f"{self.source_db}.{self.source_table}"
        return None

    @property
    def condition(self) -> str | None:
        """Return the archiving ``WHERE`` condition.

        :return: The ``WHERE`` clause, or ``None`` when not set.
        """
        return self.where

    @property
    def dest_table_display(self) -> str | None:
        """Return the destination table as ``DB.TABLE``.

        The database falls back to ``SOURCE_DB`` when ``DEST_DB`` is absent,
        mirroring the PMM Extensions archives ``get_archives_task_info`` behaviour.

        :return: The composed destination table identifier, or ``None`` when
            no destination table is set.
        """
        if not self.dest_table:
            return None
        display_db = self.dest_db or self.source_db
        if display_db:
            return f"{display_db}.{self.dest_table}"
        return self.dest_table

    @property
    def target(self) -> str | None:
        """Return the archiving target: destination table or file.

        :return: The destination table (``DB.TABLE``) when present, else the
            destination file path, else ``None``.
        """
        return self.dest_table_display or self.dest_file


def parse_archiver_purge_config(config_yaml: str | None) -> ArchiverPurgeFields | None:
    """Parse an archiver config YAML into its first-entry purge fields.

    Only the first ``PURGE_LIST`` entry is represented. The function never
    raises: a missing, non-string, scalar, or unparseable config returns
    ``None`` so the failure-alert path can fall back to placeholders.

    :param config_yaml: The serialized archiver config (``meta["config"]``).
    :return: The extracted fields, or ``None`` when no entry can be parsed.
    """
    if not config_yaml:
        return None
    try:
        config = yaml.safe_load(config_yaml)
    except (yaml.YAMLError, TypeError):
        return None
    if not isinstance(config, dict):
        return None
    purge_list = config.get("PURGE_LIST")
    if not isinstance(purge_list, list) or not purge_list:
        return None
    purge_item = purge_list[0]
    if not isinstance(purge_item, dict):
        return None
    return ArchiverPurgeFields(
        source_db=purge_item.get("SOURCE_DB"),
        source_table=purge_item.get("SOURCE_TABLE"),
        where=purge_item.get("WHERE"),
        dest_db=purge_item.get("DEST_DB"),
        dest_table=purge_item.get("DEST_TABLE"),
        dest_file=purge_item.get("DEST_FILE"),
        source_query=purge_item.get("SOURCE_QUERY"),
    )


def extract_last_error_trace(stderr: str | None) -> str:
    """Extract the last error block from a failed execution's STDERR.

    The trace is the contiguous run of lines from the last blank-line boundary
    preceding the final ``ERROR`` line through the end of the log, capped to
    :data:`MAX_TRACE_BYTES` (keeping the tail). When the log is empty or has no
    ``ERROR`` marker, :data:`ARCHIVER_TRACE_PLACEHOLDER` is returned so the
    Error Details section is never silently empty.

    :param stderr: The decoded STDERR content, or ``None``.
    :return: The extracted error block, or the placeholder.
    """
    if not stderr or not stderr.strip():
        return ARCHIVER_TRACE_PLACEHOLDER

    lines = stderr.splitlines()
    last_error_idx = None
    for idx, line in enumerate(lines):
        if STDERR_ERROR_MARKER in line:
            last_error_idx = idx
    if last_error_idx is None:
        return ARCHIVER_TRACE_PLACEHOLDER

    start = 0
    for idx in range(last_error_idx, -1, -1):
        if not lines[idx].strip():
            start = idx + 1
            break
    block = "\n".join(lines[start:]).strip()
    if not block:
        return ARCHIVER_TRACE_PLACEHOLDER

    encoded = block.encode()
    if len(encoded) > MAX_TRACE_BYTES:
        block = encoded[-MAX_TRACE_BYTES:].decode(errors="ignore")
    return block


def build_archiver_description(
    fields: ArchiverPurgeFields | None,
    error_trace: str,
    entities: set[PIIEntity],
) -> str:
    """Render the combined archiver failure description block.

    The block always carries the four labeled lines (Error Details, Source,
    Condition, Target); unresolved config fields render
    :data:`ARCHIVER_FIELD_PLACEHOLDER`. The assembled text is first passed
    through :func:`redact_secrets` (always-on credential masking, independent of
    the PII mask) and then :func:`anonymize_text` so the configured PII entities
    are scrubbed before the detail leaves for the external alerting provider. An
    empty ``entities`` set skips only the PII pass; credential redaction still
    runs.

    :param fields: The parsed archiver fields, or ``None`` when unavailable.
    :param error_trace: The extracted error trace or placeholder.
    :param entities: The PII entities to scrub from the assembled block.
    :return: The rendered (and anonymized) description block.
    """
    source = (fields.source if fields else None) or ARCHIVER_FIELD_PLACEHOLDER
    condition = (fields.condition if fields else None) or ARCHIVER_FIELD_PLACEHOLDER
    target = (fields.target if fields else None) or ARCHIVER_FIELD_PLACEHOLDER

    block = (
        "=== ERROR DETAILS ===\n"
        f"{error_trace}\n"
        "\n"
        "=== ARCHIVER CONFIGURATION ===\n"
        f"Source: {source}\n"
        f"Condition: {condition}\n"
        f"Target: {target}"
    )
    return anonymize_text(redact_secrets(block), entities)


def _effective_entities(history: "TaskHistory") -> set[PIIEntity]:
    """Return the PII entities to scrub, preferring the history-level mask.

    Falls back to the owning task's mask when the history has none, matching
    the documented :attr:`TaskHistory.anonymize_mask` semantics. A missing mask
    on both yields an empty set: PII scrubbing is opt-in (auto-masking the
    archiver DB/table/WHERE by default would gut the alert's usefulness). Note
    this only disables the *PII* pass — credential redaction in
    :func:`build_archiver_description` runs unconditionally regardless of mask.

    :param history: The task execution history.
    :return: The set of PII entities to anonymize.
    """
    mask = history.anonymize_mask
    if mask is None:
        mask = history.task.anonymize_mask
    return PIIEntity.decode_selection(mask or 0)


async def _read_last_stderr(task_history_id: int) -> str | None:
    """Read and reconstruct the failed execution's trailing error STDERR.

    Fetches the newest STDERR chunks (newest-first) via
    :meth:`TaskHistoryLogManager.get_stderr_tail_chunks` and reconstructs the
    chronological tail, scanning back far enough that an error block straddling a
    chunk boundary — including an ``ERROR`` marker split across the boundary
    itself — is fully captured. Opens its own session so the read works
    identically in the Celery worker and the tasks API, and swallows any error:
    a log-read failure must never prevent the failure alert itself from firing.

    :param task_history_id: The ``TaskHistory`` identifier.
    :return: The trailing STDERR content, or ``None`` when unavailable.
    """
    from app.tasks.crud import TaskHistoryLogManager  # noqa: PLC0415
    from app.tasks.db import get_async_session_maker  # noqa: PLC0415

    try:
        async_session = get_async_session_maker()
        async with async_session() as session:
            chunks = await TaskHistoryLogManager.get_stderr_tail_chunks(
                session, task_history_id
            )
    except Exception:
        logger.exception(
            "Failed to read STDERR for task history %s while building alert detail.",
            task_history_id,
        )
        return None

    return _reconstruct_error_tail(chunks)


def _reconstruct_error_tail(chunks: list[str]) -> str | None:
    """Reconstruct the chronological error tail from newest-first chunks.

    Walks the chunks newest-first, prepending each into the tail, and stops once
    an error marker has been seen and at least :data:`_STDERR_TAIL_MIN_BYTES` of
    trailing content has accumulated — enough that an error block straddling a
    chunk boundary is fully captured. Adjacent chunks are bridged with a short
    prefix so an ``ERROR`` marker split across the boundary is still detected.

    :param chunks: The STDERR chunk contents ordered newest-first.
    :return: The reconstructed chronological tail, or ``None`` when empty.
    """
    if not chunks:
        return None
    parts = []
    total_bytes = 0
    marker_seen = False
    marker_overlap = len(STDERR_ERROR_MARKER) - 1
    for content in chunks:
        if not marker_seen:
            # Bridge the boundary with a prefix of the newer chunk so a marker
            # split across the two is still detected.
            boundary_prefix = parts[-1][:marker_overlap] if parts else ""
            marker_seen = STDERR_ERROR_MARKER in content + boundary_prefix
        parts.append(content)
        total_bytes += len(content.encode("utf-8"))
        if marker_seen and total_bytes >= _STDERR_TAIL_MIN_BYTES:
            break
    return "".join(reversed(parts))


async def build_owner_alert_details(
    history: "TaskHistory",
) -> OwnerAlertDetails | None:
    """Build archiver failure-alert details for the given history.

    Resolved lazily by ``app.tasks.alert_hooks`` from the ``alert_detail_builder``
    path the archiver task carries (see :data:`ALERT_DETAIL_BUILDER`). Resolve the
    source database node and assemble the combined ``custom_details`` description
    block (error trace plus Source/Condition/Target).

    The config is taken from the dispatch-time snapshot
    (``execution_request.meta``) so the alert describes what actually ran rather
    than a later edit to the mutable ``task.data["meta"]``; the task data is used
    only when the snapshot is empty (legacy histories). The source node
    (``meta["_pmm_node_name"]``, falling back to ``execution_request.target``) is
    the actionable identifier the summary exists to surface and is deliberately
    not PII-scrubbed; sensitive content lives in the redacted detail block.

    :param history: The failed task execution history.
    :return: The owner-specific alert additions.
    """
    meta = history.execution_request.meta or (history.task.data or {}).get("meta") or {}
    source_node = meta.get("_pmm_node_name") or history.execution_request.target

    stderr = await _read_last_stderr(history.id)
    error_trace = extract_last_error_trace(stderr)
    fields = parse_archiver_purge_config(meta.get("config"))
    description = build_archiver_description(
        fields, error_trace, _effective_entities(history)
    )

    return OwnerAlertDetails(
        source_node=source_node,
        custom_details={"description": description},
    )
