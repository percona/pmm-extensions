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

"""Define models for the Restore plugin."""

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from app.core.models import BaseCaseInsensitiveModel
from app.core.utils.fields import EmptyStrToNone, EnumFieldMixin, NonEmptyStr
from app.extensions.apps.framework import BaseTaskResponse
from app.extensions.apps.framework.form_dsl import (
    Choices,
    Forbidden,
    RemoteChoices,
    Requires,
    SchemaRef,
    ServiceRef,
    TaskFormModel,
    Ui,
)
from app.extensions.apps.framework.rules import any_, F, not_
from app.extensions.apps.mysql_backups.forms import (
    encryption_format_for_passes,
    EncryptionFormat,
)
from app.extensions.apps.mysql_backups.models import (
    BackupType,
    ensure_backup_source_shell_safe,
    XtraBackupTool,
)
from app.inventory.models import ServiceTypeEnum

_log = logging.getLogger(__name__)

OWNER = "RESTORES"


class S3Tool(EnumFieldMixin, StrEnum):
    """Enumerate the clients that can download a backup from S3-compatible storage."""

    S3CMD = "s3cmd"
    AWSCLI = "awscli"


class SourceTransport(EnumFieldMixin, StrEnum):
    """Declare where the backup being restored is stored."""

    LOCAL = "local"
    SSH = "ssh"
    S3 = "s3"
    GCS = "gcs"


class RestoreConfigAll(BaseCaseInsensitiveModel):
    """Global config values for restore operations.

    This model contains settings that apply to all servers in a restore operation,
    including logging, SSH options, S3 tool selection, and GPG encryption.

    :param logging_dir: Directory path for storing restore operation logs.
    :type logging_dir: NonEmptyStr | EmptyStrToNone
    :param port: Port number for the restore operation.
    :type port: int | None
    :param custom_mysql_init_command: Custom MySQL initialization command.
    :type custom_mysql_init_command: NonEmptyStr | EmptyStrToNone
    :param ssh_user: SSH username for remote operations (default: "percona").
    :type ssh_user: NonEmptyStr | EmptyStrToNone
    :param ssh_port: SSH port for remote operations (default: 22).
    :type ssh_port: int | EmptyStrToNone
    :param ssh_key: SSH key name for authentication (not full path).
    :type ssh_key: NonEmptyStr | EmptyStrToNone
    :param s3_tool: Tool to use for S3 operations (default: S3CMD).
    :type s3_tool: S3Tool
    :param gpg_password_file: Path to the GPG encryption key password file.
    :type gpg_password_file: NonEmptyStr | EmptyStrToNone
    """

    logging_dir: NonEmptyStr | EmptyStrToNone = None
    port: int | None = None
    custom_mysql_init_command: NonEmptyStr | EmptyStrToNone = None

    # SSH Options
    ssh_user: NonEmptyStr | EmptyStrToNone = Field(default="percona")
    ssh_port: int | EmptyStrToNone = Field(default=22)
    ssh_key: NonEmptyStr | EmptyStrToNone = None  # only key name, not full path

    # S3 tool selection (default is s3cmd)
    s3_tool: S3Tool = S3Tool.S3CMD

    # GPG encryption key password file path
    gpg_password_file: NonEmptyStr | EmptyStrToNone = None


class BaseRestoreConfigServer(BaseCaseInsensitiveModel):
    """Restore job configuration for a specific Mydumper restore job.

    This model contains server-specific settings for a restore operation, including
    backup source, destination, threading, and script hooks.

    :param backup_type: Type of backup to restore from.
    :type backup_type: BackupType
    :param backup_source: Source location of the backup.
    :type backup_source: NonEmptyStr
    :param local_path: Local path for backup files.
    :type local_path: NonEmptyStr | EmptyStrToNone
    :param overwrite_tables: Whether to overwrite existing tables.
    :type overwrite_tables: bool
    :param myloader_threads: Number of threads for myloader operations.
    :type myloader_threads: int | None
    :param myloader_extra_args: Additional arguments for myloader.
    :type myloader_extra_args: NonEmptyStr | EmptyStrToNone
    :param skip_databases: Comma-separated string of databases to skip during restore.
    :type skip_databases: NonEmptyStr | EmptyStrToNone
    :param include_databases: Comma-separated string of databases to include in restore.
    :type include_databases: NonEmptyStr | EmptyStrToNone
    :param pre_script: Script to execute before restore.
    :type pre_script: NonEmptyStr | EmptyStrToNone
    :param post_script: Script to execute after restore.
    :type post_script: NonEmptyStr | EmptyStrToNone
    :param skip_incrementals: Whether to skip incremental backups during restore.
    :type skip_incrementals: bool
    :param datadir: MySQL data directory path.
    :type datadir: NonEmptyStr
    :param xb_prepare_memory: Memory limit for xtrabackup prepare operation.
    :type xb_prepare_memory: NonEmptyStr | EmptyStrToNone
    :param xb_parallel: Number of parallel threads for xtrabackup operations.
    :type xb_parallel: int | None
    :param xtrabackup_bin_cmd: Tool to use for xtrabackup operations.
    :type xtrabackup_bin_cmd: XtraBackupTool
    :param restore_mycnf: Whether to restore my.cnf file during restore.
    :type restore_mycnf: bool
    :param incremental_dest_path: Path for incremental backup files.
    :type incremental_dest_path: NonEmptyStr | EmptyStrToNone
    :param xtrabackup_restore_args: Additional arguments for xtrabackup restore.
    :type xtrabackup_restore_args: NonEmptyStr | EmptyStrToNone
    :param keyring_file_data: Path to the keyring file for encryption.
    :type keyring_file_data: NonEmptyStr | EmptyStrToNone
    :param xtrabackup_aes256_keyfile: Path to AES-256 key file for encryption.
    :type xtrabackup_aes256_keyfile: NonEmptyStr | EmptyStrToNone
    :param slave_from_master: Whether to configure the restored instance as a slave.
    :type slave_from_master: bool
    :param wait_for_catchup: Whether to wait for slave to catch up with master.
    :type wait_for_catchup: bool
    :param master_ip: IP address of the master server for replication.
    :type master_ip: NonEmptyStr | EmptyStrToNone
    :param master_port: Port number of the master server for replication.
    :type master_port: int | None
    :param master_user: Username for replication user on master.
    :type master_user: NonEmptyStr | EmptyStrToNone
    :param master_password: Password for replication user on master.
    :type master_password: NonEmptyStr | EmptyStrToNone
    :param start_file: Binary log file to start replication from.
    :type start_file: NonEmptyStr | EmptyStrToNone
    :param start_position: Position in binary log file to start replication from.
    :type start_position: int | EmptyStrToNone
    :param stop_file: Binary log file to stop replication at.
    :type stop_file: NonEmptyStr | EmptyStrToNone
    :param stop_position: Position in binary log file to stop replication at.
    :type stop_position: int | EmptyStrToNone
    :param use_sql_file: Path to SQL file to use for restore instead of backup files.
    :type use_sql_file: NonEmptyStr | EmptyStrToNone
    :param binlog_restore_extra_args: Additional arguments for mysqlbinlog restore command.
    :type binlog_restore_extra_args: NonEmptyStr | EmptyStrToNone
    """

    backup_type: BackupType
    backup_source: NonEmptyStr
    local_path: NonEmptyStr | EmptyStrToNone = None
    overwrite_tables: bool = False
    myloader_threads: int | EmptyStrToNone = Field(default=4)
    myloader_extra_args: NonEmptyStr | EmptyStrToNone = None
    skip_databases: NonEmptyStr | EmptyStrToNone = None
    include_databases: NonEmptyStr | EmptyStrToNone = None
    pre_script: NonEmptyStr | EmptyStrToNone = None
    post_script: NonEmptyStr | EmptyStrToNone = None
    skip_incrementals: bool = False
    datadir: NonEmptyStr | EmptyStrToNone = None
    xb_prepare_memory: NonEmptyStr | EmptyStrToNone = None
    xb_parallel: int | EmptyStrToNone = Field(default=4)
    xtrabackup_bin_cmd: XtraBackupTool | EmptyStrToNone = None
    restore_mycnf: bool = False
    incremental_dest_path: NonEmptyStr | EmptyStrToNone = None
    xtrabackup_restore_args: NonEmptyStr | EmptyStrToNone = None
    keyring_file_data: NonEmptyStr | EmptyStrToNone = None
    xtrabackup_aes256_keyfile: NonEmptyStr | EmptyStrToNone = None
    slave_from_master: bool = False
    wait_for_catchup: bool = False
    master_ip: NonEmptyStr | EmptyStrToNone = None
    master_port: int | EmptyStrToNone = Field(default=3306)
    master_user: NonEmptyStr | EmptyStrToNone = None
    master_password: NonEmptyStr | EmptyStrToNone = None
    start_file: NonEmptyStr | EmptyStrToNone = None
    start_position: int | EmptyStrToNone = None
    stop_file: NonEmptyStr | EmptyStrToNone = None
    stop_position: int | EmptyStrToNone = None
    use_sql_file: NonEmptyStr | EmptyStrToNone = None
    binlog_restore_extra_args: NonEmptyStr | EmptyStrToNone = None

    @field_validator("backup_source")
    @classmethod
    def validate_backup_source_shell_safe(cls, value: str) -> str:
        """Reject shell metacharacters in backup source (defense in depth)."""
        return ensure_backup_source_shell_safe(value)


class RestoreConfigServer(BaseRestoreConfigServer):
    """Server-specific restore configuration.

    Extends BaseRestoreConfigServer with additional required fields for alias, destination host, and port.

    :param alias: Unique identifier for the restore job.
    :type alias: NonEmptyStr
    :param dest_host: Destination host for the restore.
    :type dest_host: NonEmptyStr | EmptyStrToNone
    :param dest_port: Destination port for the restore.
    :type dest_port: int
    :param database: Target database name for restore.
    :type database: NonEmptyStr | EmptyStrToNone
    """

    alias: NonEmptyStr
    dest_host: NonEmptyStr | EmptyStrToNone = None
    dest_port: int | EmptyStrToNone = None
    database: NonEmptyStr | EmptyStrToNone = None


class RestoreConfig(BaseCaseInsensitiveModel):
    """Define the complete configuration for a restore operation.

    This model combines global settings applicable to all servers with a list of
    server-specific configurations for a complete restore operation setup.

    :param all_servers: Global configuration settings for all servers.
    :type all_servers: RestoreConfigAll
    :param server_list: List of server-specific restore configurations.
    :type server_list: list[RestoreConfigServer]
    """

    all_servers: RestoreConfigAll
    server_list: list[RestoreConfigServer]


_SSH_SOURCE_FIELDS = ("ssh_user", "ssh_port", "ssh_key")
_S3_SOURCE_FIELDS = ("s3_tool",)
_GPG_SOURCE_FIELDS = ("gpg_password_file",)
_AES_KEYFILE_FIELD = "xtrabackup_aes256_keyfile"
#: Every field a source declaration governs. The inference below needs a
#: pre-declaration default for the transport fields it decides from; the key file
#: is read for presence alone, and is listed here because a declaration gates it
#: too.
_GATED_SOURCE_FIELDS = (
    *_SSH_SOURCE_FIELDS,
    *_S3_SOURCE_FIELDS,
    *_GPG_SOURCE_FIELDS,
    _AES_KEYFILE_FIELD,
)

# ``forbidden`` (not ``requires``): these fields are optional within the source
# that reads them, only forbidden outside it.
_TRANSPORT = F("source_transport")
_SSH_ONLY = Forbidden(when=_TRANSPORT != SourceTransport.SSH)
# S3 only, not object stores generally: a ``gs://`` source is fetched by
# ``gs_copy``'s ``gcloud storage rsync``, which never consults ``s3_tool``.
_S3_ONLY = Forbidden(when=_TRANSPORT != SourceTransport.S3)
_SRC_ENCRYPTION = F("source_encryption")
_SRC_HAS_AES = any_(
    _SRC_ENCRYPTION == EncryptionFormat.AES256,
    _SRC_ENCRYPTION == EncryptionFormat.DUAL,
)
_SRC_HAS_GPG = any_(
    _SRC_ENCRYPTION == EncryptionFormat.GPG,
    _SRC_ENCRYPTION == EncryptionFormat.DUAL,
)
_GPG_SOURCE_ONLY = Forbidden(when=not_(_SRC_HAS_GPG))

# The key file is the AES-256 formats' own parameter rather than an optional
# extra of the source that reads it, so it is the one gated field that also
# carries a ``Requires``: an AES restore with no key has nothing to decrypt with.
_AES_KEYFILE_REQUIRED = Requires(
    when=_SRC_HAS_AES,
    message=(
        f"{_AES_KEYFILE_FIELD!r} is required when 'source_encryption' includes AES-256."
    ),
)
_AES_SOURCE_ONLY = Forbidden(
    when=not_(_SRC_HAS_AES),
    message=(
        f"{_AES_KEYFILE_FIELD!r} must not be set when 'source_encryption' "
        "does not include AES-256."
    ),
)
_AES_SOURCE_FIELDS = (_AES_KEYFILE_FIELD,)
_OBJECT_STORE_SCHEMES = {"s3://": SourceTransport.S3, "gs://": SourceTransport.GCS}

#: The gated fields' pre-declaration defaults, read from the config models that
#: still declare them so the inference keeps no second copy of the table it
#: matches against. Both models contribute: the transport fields are global to a
#: restore, the key file is per-server. ``_holds_a_non_default`` subscripts this
#: table rather than reading it with ``.get``, so a field renamed on one side
#: alone raises there instead of reading as "no default" and reporting every
#: value as operator-chosen.
_PRE_DECLARATION_DEFAULTS = {
    name: field.default
    for model in (RestoreConfigAll, BaseRestoreConfigServer)
    for name, field in model.model_fields.items()
    if name in _GATED_SOURCE_FIELDS
}


def _holds_a_non_default(data: Mapping[str, Any], field_name: str) -> bool:
    """Report whether a pre-declaration body carries an operator-chosen value.

    Both sides are compared as strings because this runs before field coercion,
    where a JSON client's ``"22"`` and the declared ``22`` are the same choice
    spelled two ways.

    :param data: The body being normalized.
    :param field_name: The field to test.
    :return: ``True`` when the field holds something other than its old default.
    :raises KeyError: If the field is not one of the gated fields.
    """
    value = data.get(field_name)
    if value is None:
        return False
    default = _PRE_DECLARATION_DEFAULTS[field_name]
    return default is None or str(value) != str(default)


def _infer_source_transport(data: Mapping[str, Any]) -> SourceTransport:
    """Return the transport a pre-declaration body's own fields imply.

    The ``backup_source`` scheme is checked before the SSH credentials because an
    S3 source reads ``s3_tool`` live: inferring SSH from a stray credential would
    drop a value the restore still needs.

    :param data: The body being normalized.
    :return: The transport the body's own fields imply.
    """
    backup_source = data.get("backup_source")
    if isinstance(backup_source, str):
        for scheme, transport in _OBJECT_STORE_SCHEMES.items():
            if backup_source.lower().startswith(scheme):
                return transport
    if any(_holds_a_non_default(data, name) for name in _SSH_SOURCE_FIELDS):
        return SourceTransport.SSH
    if isinstance(backup_source, str) and ":" in backup_source.split("/", 1)[0]:
        return SourceTransport.SSH
    if _holds_a_non_default(data, "s3_tool"):
        return SourceTransport.S3
    return SourceTransport.LOCAL


def normalize_source_declaration(data: Mapping[str, Any]) -> dict[str, Any]:
    """Declare the source controls on a body that predates them, dropping what they forbid.

    A body written before the controls existed carries no transport or encryption
    declaration and commonly carries the old ``percona`` / ``22`` / ``s3cmd``
    defaults, which the gates would reject. Each declaration is inferred from the
    body's own fields, and only the fields governed by an *inferred* declaration
    are dropped: a declaration the operator supplied is authoritative, so a body
    that contradicts it is left intact for the gates to reject.

    Dropping is bounded by the inference above, so a removed value can only be one
    the inferred source has no working use for: ``s3_tool`` off the S3 path, or
    SSH credentials on an object-store source. Neither is guaranteed to go unread
    — the Binlog payload consults ``s3_tool`` before it looks at the source's
    scheme, and more than one payload path shells out to ``ssh`` for any source
    holding a colon — but a branch reached that way cannot succeed for the source
    that was inferred, so no working restore depends on the dropped value.

    The AES-256 key file is deliberately never dropped here, unlike the fields an
    inferred transport governs. It is the one gated field an operator sets on
    purpose to name a secret, so a body that carries one under a format that does
    not admit it has to reach the gate and be rejected. Only a stored stamp, which
    no operator is submitting, is repaired instead — see
    :func:`repair_source_declaration`.

    :param data: The body to normalize.
    :return: A new body carrying both declarations and only the fields they allow.
    """
    normalized = dict(data)
    transport_declared = normalized.get("source_transport") is not None
    encryption_declared = normalized.get("source_encryption") is not None
    if transport_declared and encryption_declared:
        return normalized

    forbidden_fields: dict[str, SourceTransport | EncryptionFormat] = {}
    if not transport_declared:
        transport = _infer_source_transport(normalized)
        normalized["source_transport"] = transport
        if transport != SourceTransport.SSH:
            forbidden_fields.update(dict.fromkeys(_SSH_SOURCE_FIELDS, transport))
        if transport != SourceTransport.S3:
            forbidden_fields.update(dict.fromkeys(_S3_SOURCE_FIELDS, transport))
    if not encryption_declared:
        encryption = encryption_format_for_passes(
            aes256=bool(normalized.get(_AES_KEYFILE_FIELD)),
            gpg=bool(normalized.get("gpg_password_file")),
        )
        normalized["source_encryption"] = encryption
        if encryption not in (EncryptionFormat.GPG, EncryptionFormat.DUAL):
            forbidden_fields.update(dict.fromkeys(_GPG_SOURCE_FIELDS, encryption))
        if encryption not in (EncryptionFormat.AES256, EncryptionFormat.DUAL):
            forbidden_fields.update(dict.fromkeys(_AES_SOURCE_FIELDS, encryption))

    for field_name, declaration in forbidden_fields.items():
        normalized.pop(field_name, None)
        if _holds_a_non_default(data, field_name):
            _log.info(
                "Dropped %r from a restore form: the inferred %r cannot consume it",
                field_name,
                declaration,
            )
    return normalized


def _aligned_aes_declaration(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the body with its stored key file and its declared format in agreement.

    Only a *declared* format needs this; an inferred one already follows the
    body's own fields. An AES-256 pass is real when the body holds a key file —
    every engine can write one — so the declaration is rebuilt from that fact
    rather than trusted, in either direction:

    - A body holding a key file gains the format that reveals it. The edit form
      drops a field its gates hide, so a narrower declaration would strip the
      key the next save needs.
    - A body whose declaration claims an AES-256 pass with no key file loses
      that claim: an AES-256 format naming no key file has nothing to decrypt
      with. That shape predates the key-file gate, and leaving it intact would
      fail the re-validation the read paths perform and leave the form
      unopenable.

    The GPG pass is carried across untouched, read from the declaration as well as
    the password file: that file is optional under a GPG declaration, so the
    declaration is the only place a file-less GPG pass is recorded.

    :param data: The body being repaired.
    :return: The body with the key file and the format in agreement.
    """
    declared = data.get("source_encryption")
    declares_aes = declared in (EncryptionFormat.AES256, EncryptionFormat.DUAL)
    key_file = data.get(_AES_KEYFILE_FIELD)
    if not key_file and not declares_aes:
        return dict(data)

    aes256 = bool(key_file)
    return {
        **data,
        "source_encryption": encryption_format_for_passes(
            aes256=aes256,
            gpg=declared in (EncryptionFormat.GPG, EncryptionFormat.DUAL)
            or bool(data.get("gpg_password_file")),
        ),
    }


def repair_source_declaration(stamp: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the stamp describing its own source, or ``None`` when it already does.

    Serves the read paths that hand a stored stamp back to the edit form. Two
    stamps are owed a repair: one written before the source controls existed,
    which :func:`normalize_source_declaration` declares from the body's own
    fields, and one written while the key file was ungated, whose declaration can
    name a format that does not admit the key file it stores.

    Kept out of validation deliberately. Widening a declaration a client
    submitted would void the gate the declaration exists to drive, so a live
    submission is rejected where a stamp is repaired.

    :param stamp: The stored form stamp.
    :return: The repaired stamp, or ``None`` when no repair is owed.
    """
    repaired = _aligned_aes_declaration(normalize_source_declaration(stamp))
    return repaired if repaired != dict(stamp) else None


class RestoreCreate(TaskFormModel):
    """Declare the model-first create/update body and ``GET /schema`` source for Restores.

    Declares every restore form field once, in section order (Task, General,
    Mydumper, XtraBackup, Binlog), with the DSL markers driving the derived
    schema. The previously-inherited config models (:class:`RestoreConfigAll` and
    :class:`BaseRestoreConfigServer`) stay the YAML-serialization targets the
    payload builder populates via ``extract_model_from_instance``; this model
    re-declares their fields directly so it can also carry presentation metadata.

    The per-``backup_type`` section visibility is expressed in the view layout
    (``restore/views.py``), not as field-level gates: several mode-specific config
    fields carry non-``None`` defaults (``myloader_threads``, ``xb_parallel``,
    ``master_port``), so a field-level ``Forbidden`` gate would reject those
    defaults on a cross-mode restore. Keeping the model permissive preserves the
    legacy payload contract byte-for-byte.

    The transport and decryption fields are the exception, and they pay that
    price deliberately: ``source_transport`` and ``source_encryption`` declare
    where the backup lives and how it was encrypted, and the six fields those
    declarations govern are gated on them. Because a field-level ``Forbidden``
    rejects a field that is merely *present*, ``ssh_user`` / ``ssh_port`` /
    ``s3_tool`` had to give up their defaults; :class:`RestoreConfigAll` still
    declares them and ``build_restore_spec`` applies them from there, so the
    emitted config is unchanged. ``xtrabackup_aes256_keyfile`` is the sixth and
    the only one also carrying a ``Requires``, since an AES-256 format with no
    key file has nothing to decrypt with.

    ``service_id`` / ``schema_id`` keep their str-accepting annotation (carrying
    the ``"-1"`` ``UNKNOWN_SERVICE_SENTINEL``); their ``ServiceRef`` / ``SchemaRef``
    markers drive only the ``GET /schema`` widgets, while the conditional,
    404-tolerant resolution lives in ``deps.resolve_restore_entities``.

    ``service_id`` is declared first because ``backup_source`` and ``schema_id``
    cascade from it. Its ``Requires`` gate checks only *presence*, so a Mydumper
    body naming no service is rejected at body validation; deciding the value is
    a resolvable service rather than a typed name or the placeholder stays in
    ``deps.resolve_restore_entities``.
    """

    service_id: Annotated[
        NonEmptyStr | EmptyStrToNone,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), allow_custom=True),
        Requires(
            when=F("backup_type") == BackupType.MYDUMPER,
            message=(
                "Destination Database Service is required for a Mydumper restore."
            ),
        ),
        Ui(
            label="Destination Database Service",
            section="Task",
            description=(
                "Populates Backup Source and the Mydumper target database, and is "
                "where a Mydumper restore loads. XtraBackup and Binlog only record "
                "the name, so a typed one works."
            ),
        ),
    ] = None

    backup_type: Annotated[
        BackupType,
        Choices((("M", "Mydumper"), ("X", "XtraBackup"), ("B", "Binlog"))),
        Ui(
            section="Task",
            description=(
                "Restore method for this task; it has to match how the backup was "
                "taken, and it selects which sections below apply"
            ),
        ),
    ]
    backup_source: Annotated[
        NonEmptyStr,
        RemoteChoices(
            endpoint="/apps/mysql_backups/backup-sources/choices",
            allow_custom=True,
        ),
        Ui(
            section="Task",
            depends_on="service_id",
            description=(
                "Pick a service above to list its backups, or type a path: "
                "/backups/mydumper/20240101, db01:/path, s3://bucket/path or "
                "gs://bucket/path. Add /latest for the most recent."
            ),
        ),
    ]
    source_transport: Annotated[
        SourceTransport,
        Choices(
            (
                (SourceTransport.LOCAL, "Local path"),
                (SourceTransport.SSH, "Remote host over SSH"),
                (SourceTransport.S3, "S3-compatible object storage"),
                (SourceTransport.GCS, "Google Cloud Storage"),
            )
        ),
        Ui(
            label="Backup location",
            section="Task",
            description=(
                "How the backup above is reached. Selecting a transport reveals "
                "only the credentials that transport uses."
            ),
        ),
    ] = SourceTransport.LOCAL
    source_encryption: Annotated[
        EncryptionFormat,
        Choices(
            (
                (EncryptionFormat.NONE, "No encryption"),
                (EncryptionFormat.GPG, "GPG"),
                (EncryptionFormat.AES256, "AES-256"),
                (EncryptionFormat.DUAL, "AES-256 + GPG"),
            )
        ),
        Ui(
            label="Backup encryption",
            section="Task",
            description=(
                "Which encryption the backup being restored was written with. "
                "The GPG formats reveal the password file used to decrypt it; the "
                "AES-256 formats reveal the key file."
            ),
        ),
    ] = EncryptionFormat.NONE

    schema_id: Annotated[
        NonEmptyStr | EmptyStrToNone,
        SchemaRef(allow_custom=True),
        Ui(
            label="Target database",
            section="Mydumper",
            depends_on="service_id",
            description=(
                "Database the backup is loaded into. Leave empty to restore into the "
                "backup's own databases. A typed name is ignored — same as leaving it "
                "empty."
            ),
        ),
    ] = None
    local_path: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Local path",
            section="Mydumper",
            description=(
                "Staging directory on the target host where the backup is assembled "
                "before it is loaded; the staging copy is removed once the load "
                "succeeds, and left behind after a failure (defaults to /tmp)"
            ),
        ),
    ] = None
    overwrite_tables: Annotated[
        bool,
        Ui(
            label="Overwrite tables",
            section="Mydumper",
            description=(
                "Let the load replace tables that already exist in the target "
                "database. Without it the load fails on the first table that is "
                "already there."
            ),
            destructive=(
                "Existing tables in the target database are dropped before the "
                "backup is loaded. Rows written since the backup was taken are "
                "lost."
            ),
        ),
    ] = False
    myloader_threads: Annotated[
        int | EmptyStrToNone,
        Ui(
            label="Myloader threads",
            section="Mydumper",
            description=(
                "How many tables are loaded in parallel. With Overwrite tables on "
                "a database whose tables reference each other by foreign key, use "
                "1: replacing those tables in parallel can deadlock"
            ),
        ),
    ] = Field(default=4)
    myloader_extra_args: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Myloader extra args",
            section="Mydumper",
            description="Extra arguments appended to the myloader command",
        ),
    ] = None
    skip_databases: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Skip databases",
            section="Mydumper",
            description=(
                "Databases to leave out of the restore, comma-separated. They are "
                "filtered out as the backup is fetched, by file name, and are ignored "
                "for a Google Cloud Storage source."
            ),
        ),
    ] = None
    include_databases: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Include databases",
            section="Mydumper",
            description=(
                "Databases to restore, comma-separated, ignoring the rest of the "
                "backup. Filtered as the backup is fetched, by file name, and ignored "
                "for a Google Cloud Storage source."
            ),
        ),
    ] = None
    pre_script: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Pre-script",
            section="Mydumper",
            description=(
                "Path on the target host to a script run before the load. A .sql file "
                "is executed against the target MySQL; anything else runs as a "
                "command."
            ),
        ),
    ] = None
    post_script: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Post-script",
            section="Mydumper",
            description=(
                "Path on the target host to a script run after the load. A .sql file "
                "is executed against the target MySQL; anything else runs as a "
                "command."
            ),
        ),
    ] = None

    skip_incrementals: Annotated[
        bool,
        Ui(
            label="Skip incrementals",
            section="XtraBackup",
            description=(
                "Restore only the base backup and ignore the incrementals chained to it"
            ),
        ),
    ] = False
    # The mark belongs on the field naming the target rather than on a toggle:
    # the wipe is unconditional, and an empty entry aborts the restore instead of
    # being autodetected, so every restore that runs sets this.
    datadir: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Data directory",
            section="XtraBackup",
            description=(
                "MySQL data directory the backup is restored into. Required: it has to "
                "exist already, and everything currently in it is deleted."
            ),
            destructive=(
                "The data directory is emptied before the backup is restored into "
                "it. Whatever it holds now, including a live dataset, is lost."
            ),
        ),
    ] = None
    xb_prepare_memory: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="XtraBackup prepare memory",
            section="XtraBackup",
            description=(
                "Memory the prepare step may use, as a size such as 2G (defaults to "
                "100M)"
            ),
        ),
    ] = None
    xb_parallel: Annotated[
        int | EmptyStrToNone,
        Ui(
            label="XtraBackup parallel",
            section="XtraBackup",
            description=(
                "How many files are decrypted and decompressed in parallel (defaults "
                "to 4). Clearing it leaves the decompression unbounded."
            ),
        ),
    ] = Field(default=4)
    xtrabackup_bin_cmd: Annotated[
        XtraBackupTool | EmptyStrToNone,
        Ui(
            label="XtraBackup binary",
            section="XtraBackup",
            description=(
                "Which binary prepares the backup; pick the one matching the tool that "
                "took it"
            ),
        ),
    ] = None
    restore_mycnf: Annotated[
        bool,
        Ui(
            label="Restore my.cnf",
            section="XtraBackup",
            description=(
                "Restore the MySQL configuration files saved in the backup before "
                "preparing it, rewriting server id to a fresh value. Choosing one that "
                "is not already taken queries the replication source below."
            ),
            destructive=(
                "Each configuration file saved in the backup is written over "
                "the live file at its original path, outside the data "
                "directory. No copy of what those files held is kept, so "
                "hand-tuned settings are lost."
            ),
        ),
    ] = False
    incremental_dest_path: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Incremental destination path",
            section="XtraBackup",
            description=(
                "Directory where an incremental chain is assembled before it is "
                "prepared (defaults to /tmp/pxb_incrementals)"
            ),
        ),
    ] = None
    xtrabackup_restore_args: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="XtraBackup restore args",
            section="XtraBackup",
            description="Extra arguments appended to the prepare command",
        ),
    ] = None
    keyring_file_data: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Keyring file data",
            section="XtraBackup",
            description=(
                "Keyring file the prepare needs for a backup with encrypted "
                "tablespaces; read only when the backup binary above is xtrabackup. "
                "Falls back to a path found in the configuration files stored inside "
                "the backup when left empty."
            ),
        ),
    ] = None
    slave_from_master: Annotated[
        bool,
        Ui(
            label="Slave from master",
            section="XtraBackup",
            description=(
                "Start replication once the restore finishes, using the coordinates "
                "saved in the backup"
            ),
        ),
    ] = False
    wait_for_catchup: Annotated[
        bool,
        Ui(
            label="Wait for catchup",
            section="XtraBackup",
            parent="slave_from_master",
            description=(
                "Wait for the restored replica to catch up and fail the task if it "
                "does not. Needs replication to be started above."
            ),
        ),
    ] = False
    master_ip: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Master IP",
            section="XtraBackup",
            parent="slave_from_master",
            description=(
                "Replication source the restored instance connects to. Used when "
                "replication is started above."
            ),
        ),
    ] = None
    master_port: Annotated[
        int | EmptyStrToNone,
        # Deliberately unparented, unlike its four neighbours: the field's own
        # description says it is read when 'Restore my.cnf' is set, and that
        # starting replication uses the port in the backup's coordinates
        # instead — so nesting it under 'Slave from master' would grey it out
        # for the one toggle that does not use it. Whether it belongs under
        # 'Restore my.cnf' is a question for the backups owner; until then it
        # stays a plain field, as it is on main.
        Ui(
            label="Master port",
            section="XtraBackup",
            description=(
                "Port used to query the replication source for an unused server id "
                "when 'Restore my.cnf' is set. Starting replication itself uses the "
                "port in the coordinates saved in the backup."
            ),
        ),
    ] = Field(default=3306)
    master_user: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Master user",
            section="XtraBackup",
            parent="slave_from_master",
            description="Account the restored instance replicates with",
        ),
    ] = None
    master_password: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Master password",
            section="XtraBackup",
            parent="slave_from_master",
            description="Password for the replication account",
        ),
    ] = None

    start_file: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Start file",
            section="Binlog",
            description="First binlog file to replay",
        ),
    ] = None
    start_position: Annotated[
        int | EmptyStrToNone,
        Ui(
            label="Start position",
            section="Binlog",
            description="Position in the first file to start replaying from",
        ),
    ] = None
    stop_file: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Stop file",
            section="Binlog",
            description="Last binlog file to replay",
        ),
    ] = None
    stop_position: Annotated[
        int | EmptyStrToNone,
        Ui(
            label="Stop position",
            section="Binlog",
            description="Position in the last file to stop replaying at",
        ),
    ] = None
    use_sql_file: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Use SQL file",
            section="Binlog",
            description=(
                "Dump the binlog range to restore.sql in the staging directory first "
                "and then apply that file, instead of piping the binlogs straight into "
                "MySQL"
            ),
        ),
    ] = None
    binlog_restore_extra_args: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Binlog restore extra args",
            section="Binlog",
            description="Extra arguments appended to the mysqlbinlog replay command",
        ),
    ] = None

    logging_dir: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Logging directory",
            section="General",
            description="Directory on the target host for this restore's log files",
        ),
    ] = None
    port: Annotated[
        int | None,
        Ui(
            section="General",
            description=(
                "Port PMM Extensions connects to on the target host when it queries "
                "the server "
                "during an XtraBackup restore (defaults to 3306). A Mydumper restore "
                "loads over the destination service's own address and a Binlog restore "
                "replays through a local client, so neither uses it."
            ),
        ),
    ] = None
    custom_mysql_init_command: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Ui(
            label="Custom MySQL init command",
            section="General",
            description=(
                "Service command used to stop and start MySQL during an XtraBackup "
                "restore, such as /usr/bin/systemctl; detected automatically when left "
                "empty. Mydumper and Binlog restores never stop MySQL, so they ignore "
                "it."
            ),
        ),
    ] = None
    ssh_user: Annotated[
        NonEmptyStr | EmptyStrToNone,
        _SSH_ONLY,
        Ui(
            label="SSH user",
            section="General",
            description=(
                "SSH user for fetching a backup stored on a remote host. "
                "Defaults to percona when left blank."
            ),
        ),
    ] = None
    ssh_port: Annotated[
        int | EmptyStrToNone,
        _SSH_ONLY,
        Ui(
            label="SSH port",
            section="General",
            description=(
                "SSH port for fetching a backup stored on a remote host. "
                "Defaults to 22 when left blank."
            ),
        ),
    ] = None
    ssh_key: Annotated[
        NonEmptyStr | EmptyStrToNone,
        _SSH_ONLY,
        Ui(
            label="SSH key name",
            section="General",
            description=(
                "Unused for the sources above: a local path, remote host, S3 or "
                "Google Cloud Storage fetch always authenticates with the SSH "
                "user's own id_rsa key."
            ),
        ),
    ] = None
    s3_tool: Annotated[
        S3Tool | EmptyStrToNone,
        _S3_ONLY,
        Choices(((S3Tool.S3CMD, "s3cmd"), (S3Tool.AWSCLI, "awscli"))),
        Ui(
            label="S3 tool",
            section="General",
            description=(
                "Client used to download the backup. Defaults to s3cmd when left blank."
            ),
        ),
    ] = None
    gpg_password_file: Annotated[
        NonEmptyStr | EmptyStrToNone,
        _GPG_SOURCE_ONLY,
        Ui(
            label="GPG password file",
            section="General",
            description=(
                "Path on the target host to the file holding the passphrase for a "
                "GPG-encrypted backup"
            ),
        ),
    ] = None
    xtrabackup_aes256_keyfile: Annotated[
        NonEmptyStr | EmptyStrToNone,
        _AES_KEYFILE_REQUIRED,
        _AES_SOURCE_ONLY,
        Ui(
            label="AES-256 key file",
            section="General",
            description=(
                "Path on the target host to the AES-256 key file the backup was "
                "encrypted with, needed to decrypt .xbcrypt files before restore"
            ),
        ),
    ] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_int_reference_ids(cls, data: Any) -> Any:
        """Coerce int ``service_id`` / ``schema_id`` (JSON body) to str.

        The HTML form path receives these fields as ``NonEmptyStr |
        EmptyStrToNone`` directly. The JSON API (React schema form) may send
        inventory ids as ints; without coercion they fail the str-typed fields.
        Form submissions arriving as strings are unaffected.

        :param data: The raw input passed to ``model_validate``.
        :return: The input with stringified reference ids when they were ints,
            or ``data`` unchanged otherwise.
        """
        if not isinstance(data, dict):
            return data
        updates: dict[str, str] = {}
        for key in ("service_id", "schema_id"):
            value = data.get(key)
            if isinstance(value, int):
                updates[key] = str(value)
        if updates:
            return {**data, **updates}
        return data

    @model_validator(mode="before")
    @classmethod
    def _declare_source_of_a_legacy_body(cls, data: Any) -> Any:
        """Declare the source controls on a body written before they existed.

        Every stored ``_form`` stamp is a full model dump, so one written before
        the controls carries the old ``percona`` / ``22`` / ``s3cmd`` defaults and
        is re-submitted verbatim by the derived ``PUT``. Normalizing here is what
        keeps that edit from failing its own gates while the one-time backfill
        command has not been run.

        A declaration the body already carries is left alone, so a client that
        submits a field its declared source forbids is still rejected.

        :param data: The raw input passed to ``model_validate``.
        :return: The input with both source declarations, or ``data`` unchanged.
        """
        if not isinstance(data, dict):
            return data
        return normalize_source_declaration(data)

    @field_validator("backup_source")
    @classmethod
    def validate_backup_source_shell_safe(cls, value: str) -> str:
        """Reject shell metacharacters in backup source (defense in depth)."""
        return ensure_backup_source_shell_safe(value)


class RestoresResponse(BaseTaskResponse):
    """Represent a restore task API response.

    Extend the standard task-response surface with the restore-specific
    destination facts the detail view renders; the shared task identity,
    status, audit, and anonymization fields come from
    :class:`~app.extensions.apps.framework.responses.BaseTaskResponse`.

    :param backup_type: The backup type recorded in task config.
    :param hostname: The executor hostname target.
    :param host: The destination host recorded in task config.
    :param port: The destination port recorded in task config.
    """

    backup_type: BackupType | None = None
    hostname: str | None = None
    host: str | None = None
    port: int | None = None
