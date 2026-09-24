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

"""Define models for the Backups plugin."""

import re
from dataclasses import dataclass
from enum import nonmember, StrEnum
from typing import Annotated, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

import yaml
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.core.models import BaseCaseInsensitiveModel
from app.core.utils.fields import (
    EmptyStrToNone,
    EnumFieldMixin,
    NonEmptyStr,
    StrHttpUrl,
    StrippedNonEmptyStr,
)
from app.extensions.apps.framework.form_dsl import (
    Choices,
    Forbidden,
    HostRef,
    Requires,
    ServiceRef,
    TaskFormModel,
    Ui,
)
from app.extensions.apps.framework.rules import F
from app.extensions.apps.labels import EXECUTION_HOST_LABEL
from app.extensions.apps.shared.backups.responses import BackupTaskBase
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum

OWNER = "BACKUP_MONGO"


class BackupType(EnumFieldMixin, StrEnum):
    """Represent the backup tools a run can be taken with.

    :cvar LABELS: Display text for each stored value, keyed as the value is
        stored on the wire. A value with no entry is rendered as-is by the
        caller. Wrapped in :func:`enum.nonmember` because ``enum`` would
        otherwise treat a class-body dict as a member candidate.
    """

    PBM_LOGICAL = "pbm_logical"
    PBM_PHYSICAL = "pbm_physical"
    PBM_INCREMENTAL = "pbm_incremental"
    PBM_SNAPSHOT = "pbm_snapshot"
    PBM_CONFIG = "pbm_config"
    PBM_STATUS = "pbm_status"

    LABELS = nonmember(
        {
            "pbm_logical": "PBM Logical",
            "pbm_physical": "PBM Physical",
            "pbm_incremental": "PBM Incremental",
            "pbm_snapshot": "PBM Snapshot",
            "pbm_config": "PBM Config",
            "pbm_status": "PBM Status",
        }
    )


#: Database name portion of a selective namespace (before the first ``.``).
_NAMESPACE_DB_RE = re.compile(r"^[^.*\s]+$")
#: Database-level selective namespace (``db.*``) required for ``--with-users-and-roles``.
_DB_LEVEL_NAMESPACE_RE = re.compile(r"^[^.*\s]+\.\*$")


def parse_backup_namespaces(namespaces: str) -> list[str]:
    """Parse a comma-separated selective namespace list into tokens.

    PBM splits each token at the first ``.``, so collection names may contain
    additional dots (for example ``db.orders.archive``). Empty comma-separated
    entries and database wildcards such as ``*.users`` are rejected.

    :param namespaces: Raw ``db.collection`` / ``db.*`` list from the form.
    :return: Non-empty stripped namespace tokens, preserving order.
    :raises ValueError: When any token is empty after split or fails the
        ``db.collection`` / ``db.*`` shape.
    """
    tokens = [part.strip() for part in namespaces.split(",")]
    if any(not token for token in tokens):
        raise ValueError("Backup namespaces must not contain empty entries")
    invalid = []
    for token in tokens:
        if "." not in token:
            invalid.append(token)
            continue
        database, collection = token.split(".", 1)
        if not _NAMESPACE_DB_RE.fullmatch(database) or not collection:
            invalid.append(token)
    if invalid:
        raise ValueError(
            "Backup namespaces must be comma-separated db.collection or db.* "
            f"entries; invalid: {', '.join(invalid)}"
        )
    return tokens


def validate_selective_users_and_roles(
    namespaces: str | None,
    *,
    with_users_and_roles: bool,
) -> None:
    """Reject ``--with-users-and-roles`` unless every namespace is database-level.

    :param namespaces: Comma-separated selective namespaces, or ``None`` when unset.
    :param with_users_and_roles: Whether the opt-in users/roles flag is enabled.
    :raises ValueError: When the flag is set without namespaces, or any namespace
        is a single collection rather than ``db.*``.
    """
    if not with_users_and_roles:
        return
    if not namespaces or not str(namespaces).strip():
        raise ValueError(
            "Include users and roles requires backup namespaces set to database-level "
            "db.* entries"
        )
    tokens = parse_backup_namespaces(str(namespaces))
    non_db_level = [
        token for token in tokens if not _DB_LEVEL_NAMESPACE_RE.fullmatch(token)
    ]
    if non_db_level:
        raise ValueError(
            "Include users and roles is valid only with database-level db.* "
            f"namespaces; invalid: {', '.join(non_db_level)}"
        )


BackupNamespacesList = Annotated[
    str, AfterValidator(lambda value: ",".join(parse_backup_namespaces(value)))
]


def parse_pbm_string_map(value: str) -> dict[str, str]:
    """Parse a YAML mapping of string to string, as PBM's per-node maps are written.

    Backs ``storage.s3.endpointUrlMap`` and ``restore.mongodLocationMap``, both of
    which key a plain string by node address. Validated at create time so a typo
    surfaces as a 422 on the field rather than as a PBM parse error on the host.

    :param value: Raw YAML from the form field.
    :return: The parsed mapping, with keys and values coerced to ``str``.
    :raises ValueError: On invalid YAML, a non-mapping result, or an empty mapping.
    """
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ValueError(f"Value is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(  # noqa: TRY004
            "Value must be a YAML mapping of node address to string"
        )
    if not parsed:
        raise ValueError("Mapping is empty; provide at least one entry")
    return {str(key): str(item) for key, item in parsed.items()}


def _validate_pbm_string_map_yaml(value: str) -> str:
    """Validate the YAML mapping at create time and keep the operator's text.

    Mirrors ``BackupPriorityYaml``: the field carries the raw YAML and the spec
    builder parses it, so what a user typed survives a round-trip through the
    form instead of being reformatted by a dump.

    :param value: Raw YAML from the form field.
    :return: ``value`` unchanged.
    :raises ValueError: When it is not a non-empty YAML mapping.
    """
    parse_pbm_string_map(value)
    return value


PbmStringMapYaml = Annotated[NonEmptyStr, AfterValidator(_validate_pbm_string_map_yaml)]


class StorageType(StrEnum):
    """Represent where PBM should keep datafiles.

    Every backend ``pbm`` 2.15.0 accepts. ``minio`` is a backend in its own right
    rather than a label for S3-compatible storage: PBM gives it its own key set
    (``endpoint`` rather than ``endpointUrl``, a ``secure`` flag, a signature
    version). Pointing the ``s3`` backend at a MinIO endpoint still works and
    remains the usual choice for S3-compatible servers generally.
    """

    S3 = "s3"
    MINIO = "minio"
    GCS = "gcs"
    AZURE = "azure"
    OCI = "oci"
    OSS = "oss"
    FILESYSTEM = "filesystem"


class S3Provider(StrEnum):
    """Represents native s3 or plugins what use s3 protocol."""

    AWS = "aws"
    MINIO = "minio"
    GCP = "gcp"


class CompressionAlgorithm(StrEnum):
    """Represents algorithm of choice whem compressing wirteTiger datafiles."""

    GZIP = "gzip"
    SNAPPY = "snappy"
    LZ4 = "lz4"
    S2 = "s2"
    PGZIP = "pgzip"
    ZSTD = "zstd"


class LogLevel(StrEnum):
    """Represents log verbosity of PBM service."""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogOutput(StrEnum):
    """Determines output of log."""

    STDOUT = "stdout"
    FILE = "file"
    SYSLOG = "syslog"


def parse_backup_priority(priority_str: str) -> dict[str, float]:
    """Parse the Node Priority YAML into a node -> priority mapping.

    Shared by the request-model validator (create-time) and the spec builder so the
    parse rules cannot drift and a present priority is never silently dropped.

    :param priority_str: Raw YAML from the Node Priority field.
    :return: Mapping of node address to numeric priority.
    :raises ValueError: On invalid YAML, a non-mapping result, an empty mapping,
        or a non-numeric priority value.
    """
    try:
        parsed = yaml.safe_load(priority_str)
    except yaml.YAMLError as exc:
        raise ValueError(f"Node priority is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        # Raise ValueError, not TypeError, so Pydantic surfaces a 422 instead of a 500.
        raise ValueError(  # noqa: TRY004
            "Node priority must be a YAML mapping of node address to priority number"
        )
    if not parsed:
        # A present-but-empty mapping would be dropped as falsy in the spec builder;
        # reject it so a present field always takes effect.
        raise ValueError("Node priority mapping is empty; provide at least one node")
    result = {}
    for node, value in parsed.items():
        # Reject booleans explicitly — they would otherwise coerce to 1.0 / 0.0.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(  # noqa: TRY004
                f"Priority for {node!r} must be a number, got {value!r}"
            )
        result[str(node)] = float(value)
    return result


def _validate_priority_yaml(value: str) -> str:
    """Validate the Node Priority YAML, returning the raw string unchanged.

    :param value: The raw Node Priority YAML string.
    :return: The same string, once it is confirmed to be a valid priority mapping.
    :raises ValueError: If the YAML is not a valid node -> number mapping.
    """
    parse_backup_priority(value)
    return value


# A non-empty Node Priority YAML string, validated as a node -> number mapping.
BackupPriorityYaml = Annotated[NonEmptyStr, AfterValidator(_validate_priority_yaml)]

#: Storage backends SEP builds a PBM config for, each declaring the form-field
#: prefix that identifies its fields and which of them PBM requires. Validation is
#: driven off this table rather than written per pair of backends: the old shape
#: asserted "none of the *other* backend's fields are set" by naming them, which is
#: quadratic and was already awkward at two backends.
#:
#: No prefix may be a prefix of another, or a field would be attributed to two
#: backends; a test asserts that.
_STORAGE_BACKENDS: dict[str, "_StorageBackend"] = {}

# DNS-compliant S3 bucket names: 3-63 chars, dot-separated labels of lowercase
# letters, digits and hyphens, each label starting and ending alphanumeric -- so no
# consecutive dots and no dot-adjacent hyphens (``a..b``, ``a.-b``). IP-address-formatted
# names are rejected separately below. A well-formed but non-existent bucket is caught
# later, when the config is applied.
_S3_BUCKET_RE = re.compile(
    r"^(?=.{3,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
# S3 bucket names must not be formatted as an IPv4 address (e.g. ``192.168.5.4``).
_S3_BUCKET_IP_RE = re.compile(r"^\d+(?:\.\d+){3}$")
# AWS region format (e.g. ``us-east-1``, ``us-gov-east-1``). Enforced only for native
# AWS (no custom endpoint); with a custom endpoint the region is provider-defined.
_AWS_REGION_RE = re.compile(r"^[a-z]{2}-[a-z-]+-\d+$")


def _is_blank(value: str | None) -> bool:
    """Return whether ``value`` is missing or whitespace-only."""
    return not (value and value.strip())


def _validate_s3_bucket_name(value: str) -> str:
    """Validate a DNS-compliant, non-IP-formatted S3 bucket name.

    A value-only check carried on the field annotation so a malformed bucket is
    reported against its own input (a non-empty ``loc``, which the flash-message
    helper needs to name the field) rather than the whole model.

    :param value: The (already-stripped) S3 bucket name.
    :return: The same value once it is confirmed DNS-compliant.
    :raises ValueError: When the value is not a DNS-compliant bucket name or looks
        like an IPv4 address.
    """
    if not _S3_BUCKET_RE.match(value) or _S3_BUCKET_IP_RE.match(value):
        raise ValueError(
            f"S3 bucket {value!r} is not a valid DNS-compliant bucket name "
            "(3-63 chars: lowercase letters, digits, dots, hyphens; "
            "no consecutive dots and not an IP address)"
        )
    return value


# A stripped, non-empty, DNS-compliant S3 bucket name.
S3BucketName = Annotated[StrippedNonEmptyStr, AfterValidator(_validate_s3_bucket_name)]


def _is_unset(value: object) -> bool:
    """Return whether a form value carries nothing.

    Broader than :func:`_is_blank`, which only knows about strings: storage fields
    are now booleans and numbers as well, and ``False`` and ``0`` are answers rather
    than absences.

    :param value: Any form field value.
    :return: Whether the field was left empty.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


@dataclass(frozen=True)
class _StorageBackend:
    """Describe one PBM storage backend to the create-time validator.

    :param prefix: The form-field prefix owning this backend's fields, e.g.
        ``storage_s3_``. Fields are attributed to a backend by this alone.
    :param label: How the backend is named in an error message.
    :param required: Field suffixes (after ``prefix``) PBM will not work without.
    :param cross_check: Optional cross-field check for rules the table cannot express,
        taking the whole field mapping.
    """

    prefix: str
    label: str
    required: tuple[str, ...] = ()
    cross_check: "Callable[[Mapping[str, object]], None] | None" = None


def _validate_s3_region(values: "Mapping[str, object]") -> None:
    """Require a native-AWS region shape unless a custom endpoint marks it otherwise.

    With an endpoint URL the storage is S3-compatible rather than AWS, and the
    region is whatever that provider calls it -- MinIO ignores it entirely.

    :param values: The create body's field mapping.
    :raises ValueError: When an AWS-shaped region is required and missing.
    """
    region = values.get("storage_s3_region")
    if _is_unset(values.get("storage_s3_endpoint_url")) and not _AWS_REGION_RE.match(
        str(region)
    ):
        raise ValueError(
            f"S3 region {region!r} is not a valid AWS region (e.g. us-east-1); "
            "set an endpoint URL for S3-compatible (non-AWS) storage"
        )


_STORAGE_BACKENDS.update(
    {
        StorageType.S3.value: _StorageBackend(
            prefix="storage_s3_",
            label="S3",
            required=("bucket", "region"),
            cross_check=_validate_s3_region,
        ),
        StorageType.MINIO.value: _StorageBackend(
            prefix="storage_minio_",
            label="MinIO",
            required=("bucket", "endpoint"),
        ),
        StorageType.GCS.value: _StorageBackend(
            prefix="storage_gcs_",
            label="GCS",
            required=("bucket",),
        ),
        StorageType.AZURE.value: _StorageBackend(
            prefix="storage_azure_",
            label="Azure",
            required=("account", "container"),
        ),
        StorageType.OCI.value: _StorageBackend(
            prefix="storage_oci_",
            label="OCI",
            required=("region", "namespace", "bucket"),
        ),
        StorageType.FILESYSTEM.value: _StorageBackend(
            prefix="storage_filesystem_",
            label="Filesystem",
            required=("path",),
        ),
    }
)
# ``StorageType.OSS`` is deliberately absent. PBM accepts the backend, but its
# published key reference does not match the 2.15.0 binary -- the documented
# ``serverSideEncryption`` names (``sseAlgorithm``, ``kmsMasterKeyId``,
# ``kmsDataEncryption``) are all rejected, the real ones being ``encryptionMethod``,
# ``encryptionAlgorithm`` and ``encryptionKeyId``. Its remaining keys have not been
# checked against the binary, and a field that looks settable but reaches nothing is
# the failure this panel exists to prevent. Not registered means not offered and not
# accepted, rather than offered and quietly broken.


def validate_storage_config(
    storage_type: str | None,
    values: "Mapping[str, object]",
) -> None:
    """Validate a per-task PBM storage configuration at create time.

    Ensure the selected backend is one PMM Extensions supports, that its required fields are
    present, and that fields belonging to the *other* backend are not set — so an
    incomplete, malformed, or cross-wired storage config is rejected at create time
    (422 on the JSON API, a flash-redirect on the form path) rather than silently
    accepted (and then partly ignored by the spec builder) and never applied. The
    value-only field checks (DNS-compliant bucket, ``http(s)`` endpoint URL) run on
    the field annotations; this helper covers the cross-field rules only. The checks
    are structural; bucket reachability is surfaced later, when the config is applied
    to PBM.

    :param storage_type: The selected storage backend.
    :param values: The create body's fields, by name. Storage fields are attributed
        to a backend by their prefix, so a backend added to
        :data:`_STORAGE_BACKENDS` is validated with no change here.
    :raises ValueError: When the storage type is unsupported, a required field is
        missing, or a field belonging to a different backend is set.
    """
    if storage_type not in _STORAGE_BACKENDS:
        raise ValueError(
            "storage_type must be one of "
            f"{sorted(_STORAGE_BACKENDS)}, got {storage_type!r}"
        )
    backend = _STORAGE_BACKENDS[storage_type]

    missing = [
        field
        for field in backend.required
        if _is_unset(values.get(f"{backend.prefix}{field}"))
    ]
    if missing:
        raise ValueError(
            f"{backend.label} storage requires {', '.join(sorted(missing))}"
        )

    foreign = sorted(
        name
        for other_type, other in _STORAGE_BACKENDS.items()
        if other_type != storage_type
        for name, value in values.items()
        if name.startswith(other.prefix) and not _is_unset(value)
    )
    if foreign:
        raise ValueError(
            f"Fields of another storage backend must not be set for "
            f"{backend.label} storage: {foreign}"
        )

    if backend.cross_check is not None:
        backend.cross_check(values)


class BackupConfigPITR(BaseCaseInsensitiveModel):
    """Represent Point In Time Recovery configuration.

    :param enabled: PITR enabled.
    :type enabled: bool
    :param oplogSpanMin: The PBM ...
    :type oplogSpanMin: int
    :param compression: Compression ... PBM.
    :type compression: NonEmptyStr
    """

    model_config = ConfigDict(alias_generator=None)

    enabled: bool = Field(
        default=False, validation_alias=AliasChoices("enabled", "ENABLED")
    )
    oplog_span_min: int | None = Field(
        None,
        validation_alias=AliasChoices("oplogSpanMin", "OPLOGSPANMIN"),
        serialization_alias="oplogSpanMin",
    )
    compression: Annotated[
        NonEmptyStr, Field(validation_alias=AliasChoices("compression", "COMPRESSION"))
    ]
    compression_level: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("compressionLevel", "COMPRESSIONLEVEL"),
        serialization_alias="compressionLevel",
    )
    oplog_only: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("oplogOnly", "OPLOGONLY"),
        serialization_alias="oplogOnly",
    )
    priority: dict[str, float] | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("priority", "PRIORITY")
    )


class BackupConfigBackupTimeouts(BaseCaseInsensitiveModel):
    """Represent backup timeout configuration.

    :param startingStatus: Wait time (in seconds) for PBM to start backups.
    :type startingStatus: int | None
    """

    model_config = ConfigDict(alias_generator=None)

    starting_status: int | None = Field(
        None,
        validation_alias=AliasChoices("startingStatus", "STARTINGSTATUS"),
        serialization_alias="startingStatus",
    )
    balancer_stop: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("balancerStop", "BALANCERSTOP"),
        serialization_alias="balancerStop",
    )


class BackupConfigBackup(BaseCaseInsensitiveModel):
    """Represent backup configuration options.

    :param priority: Dictionary mapping mongod node addresses to their priority for making backups.
        The node with the highest priority is elected for making a backup.
    :type priority: dict[str, float] | EmptyStrToNone
    :param compression: Compression method for backup snapshots.
    :type compression: CompressionAlgorithm | EmptyStrToNone
    :param compressionLevel: Compression level (depends on compression method).
    :type compressionLevel: int | EmptyStrToNone
    :param timeouts: Backup timeout configuration.
    :type timeouts: BackupConfigBackupTimeouts | EmptyStrToNone
    :param oplogSpanMin: Duration (in minutes) of oplog slices saved with logical backup.
    :type oplogSpanMin: float | EmptyStrToNone
    :param numParallelCollections: Number of parallel collections to process during logical backup.
    :type numParallelCollections: int | EmptyStrToNone
    :param namespaces: Comma-separated selective namespaces for logical backups.
    :param withUsersAndRoles: Whether to include users and roles with database-level selective.
    :type withUsersAndRoles: bool | EmptyStrToNone
    """

    model_config = ConfigDict(alias_generator=None)

    priority: dict[str, float] | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("priority", "PRIORITY")
    )
    compression: CompressionAlgorithm | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("compression", "COMPRESSION")
    )
    compression_level: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("compressionLevel", "COMPRESSIONLEVEL"),
        serialization_alias="compressionLevel",
    )
    timeouts: BackupConfigBackupTimeouts | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("timeouts", "TIMEOUTS")
    )
    oplog_span_min: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("oplogSpanMin", "OPLOGSPANMIN"),
        serialization_alias="oplogSpanMin",
    )
    num_parallel_collections: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices(
            "numParallelCollections", "NUMPARALLELCOLLECTIONS"
        ),
        serialization_alias="numParallelCollections",
    )
    num_parallel_files: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numParallelFiles", "NUMPARALLELFILES"),
        serialization_alias="numParallelFiles",
    )
    namespaces: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("namespaces", "NAMESPACES")
    )
    with_users_and_roles: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("withUsersAndRoles", "WITHUSERSANDROLES"),
        serialization_alias="withUsersAndRoles",
    )


class BackupConfigRestoreTimeouts(BaseCaseInsensitiveModel):
    """Represent restore timeout configuration.

    :param balancerStop: Seconds to wait for the sharded-cluster balancer to stop.
    """

    model_config = ConfigDict(alias_generator=None)

    balancer_stop: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("balancerStop", "BALANCERSTOP"),
        serialization_alias="balancerStop",
    )


class BackupConfigRestore(BaseCaseInsensitiveModel):
    """Represent PBM's ``restore`` configuration section.

    Cluster-wide tuning for how a restore runs, not a description of any one
    restore: the Restores app builds its own task and reads none of this. It is
    here because ``restore`` is part of the document ``pbm config --file`` writes,
    and a configuration panel that omitted it would silently drop whatever an
    operator had set the last time they used the CLI.
    """

    model_config = ConfigDict(alias_generator=None)

    batch_size: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("batchSize", "BATCHSIZE"),
        serialization_alias="batchSize",
    )
    num_insertion_workers: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numInsertionWorkers", "NUMINSERTIONWORKERS"),
        serialization_alias="numInsertionWorkers",
    )
    num_parallel_collections: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices(
            "numParallelCollections", "NUMPARALLELCOLLECTIONS"
        ),
        serialization_alias="numParallelCollections",
    )
    num_parallel_files: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numParallelFiles", "NUMPARALLELFILES"),
        serialization_alias="numParallelFiles",
    )
    num_download_workers: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numDownloadWorkers", "NUMDOWNLOADWORKERS"),
        serialization_alias="numDownloadWorkers",
    )
    max_download_buffer_mb: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxDownloadBufferMb", "MAXDOWNLOADBUFFERMB"),
        serialization_alias="maxDownloadBufferMb",
    )
    download_chunk_mb: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("downloadChunkMb", "DOWNLOADCHUNKMB"),
        serialization_alias="downloadChunkMb",
    )
    index_commit_quorum: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("indexCommitQuorum", "INDEXCOMMITQUORUM"),
        serialization_alias="indexCommitQuorum",
    )
    mongod_location: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("mongodLocation", "MONGODLOCATION"),
        serialization_alias="mongodLocation",
    )
    mongod_location_map: dict[str, str] | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("mongodLocationMap", "MONGODLOCATIONMAP"),
        serialization_alias="mongodLocationMap",
    )
    fallback_enabled: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("fallbackEnabled", "FALLBACKENABLED"),
        serialization_alias="fallbackEnabled",
    )
    allow_partly_done: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("allowPartlyDone", "ALLOWPARTLYDONE"),
        serialization_alias="allowPartlyDone",
    )
    timeouts: BackupConfigRestoreTimeouts | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("timeouts", "TIMEOUTS")
    )


class BackupConfigStorageS3SSE(BaseCaseInsensitiveModel):
    """Represent S3 server-side encryption settings.

    ``sseCustomerKey`` is deliberately absent. It is the encryption key itself, so
    it falls under the same rule as the access keys beside it: SEP has nowhere safe
    to keep it, and read-merge-write carries whatever the CLI set through untouched.
    ``kmsKeyID`` is an identifier rather than a secret, so it is settable here.
    """

    model_config = ConfigDict(alias_generator=None)

    sse_algorithm: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("sseAlgorithm", "SSEALGORITHM"),
        serialization_alias="sseAlgorithm",
    )
    kms_key_id: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("kmsKeyID", "KMSKEYID"),
        serialization_alias="kmsKeyID",
    )
    sse_customer_algorithm: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("sseCustomerAlgorithm", "SSECUSTOMERALGORITHM"),
        serialization_alias="sseCustomerAlgorithm",
    )


class BackupConfigStorageS3Retryer(BaseCaseInsensitiveModel):
    """Represent S3 upload retry settings.

    The delays are Go durations on the wire (``30ms``, ``5m``), so they are carried
    as strings rather than numbers -- PBM parses them, and coercing to seconds here
    would lose the unit the operator wrote.
    """

    model_config = ConfigDict(alias_generator=None)

    num_max_retries: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numMaxRetries", "NUMMAXRETRIES"),
        serialization_alias="numMaxRetries",
    )
    min_retry_delay: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("minRetryDelay", "MINRETRYDELAY"),
        serialization_alias="minRetryDelay",
    )
    max_retry_delay: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxRetryDelay", "MAXRETRYDELAY"),
        serialization_alias="maxRetryDelay",
    )


class BackupConfigStorageFilesystem(BaseCaseInsensitiveModel):
    """Represents a filesystem storage configuration."""

    model_config = ConfigDict(alias_generator=None)

    path: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("path", "PATH")
    )
    max_obj_size_gb: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxObjSizeGB", "MAXOBJSIZEGB"),
        serialization_alias="maxObjSizeGB",
    )


class BackupConfigStorageS3(BaseCaseInsensitiveModel):
    """Represents an S3 storage configuration."""

    model_config = ConfigDict(alias_generator=None)

    region: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("region", "REGION")
    )
    bucket: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("bucket", "BUCKET")
    )
    prefix: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("prefix", "PREFIX")
    )
    endpoint_url: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("endpointUrl", "ENDPOINTURL"),
        serialization_alias="endpointUrl",
    )
    endpoint_url_map: dict[str, str] | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("endpointUrlMap", "ENDPOINTURLMAP"),
        serialization_alias="endpointUrlMap",
    )
    force_path_style: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("forcePathStyle", "FORCEPATHSTYLE"),
        serialization_alias="forcePathStyle",
    )
    upload_part_size: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("uploadPartSize", "UPLOADPARTSIZE"),
        serialization_alias="uploadPartSize",
    )
    max_upload_parts: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxUploadParts", "MAXUPLOADPARTS"),
        serialization_alias="maxUploadParts",
    )
    storage_class: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("storageClass", "STORAGECLASS"),
        serialization_alias="storageClass",
    )
    insecure_skip_tls_verify: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("insecureSkipTLSVerify", "INSECURESKIPTLSVERIFY"),
        serialization_alias="insecureSkipTLSVerify",
    )
    debug_log_levels: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("debugLogLevels", "DEBUGLOGLEVELS"),
        serialization_alias="debugLogLevels",
    )
    max_obj_size_gb: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxObjSizeGB", "MAXOBJSIZEGB"),
        serialization_alias="maxObjSizeGB",
    )
    server_side_encryption: BackupConfigStorageS3SSE | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("serverSideEncryption", "SERVERSIDEENCRYPTION"),
        serialization_alias="serverSideEncryption",
    )
    retryer: BackupConfigStorageS3Retryer | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("retryer", "RETRYER")
    )


class BackupConfigStorageMinioRetryer(BaseCaseInsensitiveModel):
    """Represent MinIO upload retry settings."""

    model_config = ConfigDict(alias_generator=None)

    num_max_retries: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numMaxRetries", "NUMMAXRETRIES"),
        serialization_alias="numMaxRetries",
    )


class BackupConfigStorageMinio(BaseCaseInsensitiveModel):
    """Represent PBM's native MinIO storage backend.

    Not the same shape as pointing ``s3`` at a MinIO endpoint, which also works and
    is the usual choice: PBM names this backend's endpoint ``endpoint`` rather than
    ``endpointUrl``, adds a ``secure`` flag, and carries a signature version under
    ``credentials`` (absent here, as every backend's credentials are).
    """

    model_config = ConfigDict(alias_generator=None)

    region: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("region", "REGION")
    )
    bucket: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("bucket", "BUCKET")
    )
    prefix: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("prefix", "PREFIX")
    )
    endpoint: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("endpoint", "ENDPOINT")
    )
    endpoint_map: dict[str, str] | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("endpointMap", "ENDPOINTMAP"),
        serialization_alias="endpointMap",
    )
    secure: bool | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("secure", "SECURE")
    )
    insecure_skip_tls_verify: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("insecureSkipTLSVerify", "INSECURESKIPTLSVERIFY"),
        serialization_alias="insecureSkipTLSVerify",
    )
    force_path_style: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("forcePathStyle", "FORCEPATHSTYLE"),
        serialization_alias="forcePathStyle",
    )
    part_size: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("partSize", "PARTSIZE"),
        serialization_alias="partSize",
    )
    max_obj_size_gb: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxObjSizeGB", "MAXOBJSIZEGB"),
        serialization_alias="maxObjSizeGB",
    )
    debug_trace: bool | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("debugTrace", "DEBUGTRACE"),
        serialization_alias="debugTrace",
    )
    retryer: BackupConfigStorageMinioRetryer | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("retryer", "RETRYER")
    )


class BackupConfigStorageGcsRetryer(BaseCaseInsensitiveModel):
    """Represent GCS upload retry settings.

    GCS backs off rather than delaying a fixed amount, so its knobs are unlike the
    S3 retryer's beside it: the backoff values are Go durations, the multiplier and
    attempt count are plain numbers.
    """

    model_config = ConfigDict(alias_generator=None)

    backoff_initial: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("backoffInitial", "BACKOFFINITIAL"),
        serialization_alias="backoffInitial",
    )
    backoff_max: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("backoffMax", "BACKOFFMAX"),
        serialization_alias="backoffMax",
    )
    backoff_multiplier: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("backoffMultiplier", "BACKOFFMULTIPLIER"),
        serialization_alias="backoffMultiplier",
    )
    max_attempts: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxAttempts", "MAXATTEMPTS"),
        serialization_alias="maxAttempts",
    )
    chunk_retry_deadline: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("chunkRetryDeadline", "CHUNKRETRYDEADLINE"),
        serialization_alias="chunkRetryDeadline",
    )


class BackupConfigStorageGcs(BaseCaseInsensitiveModel):
    """Represent Google Cloud Storage.

    ``credentials.clientEmail`` and ``credentials.privateKey`` are absent under the
    same rule as every other backend's: they are set with the ``pbm`` CLI and the
    apply's read-merge-write carries them through untouched.
    """

    model_config = ConfigDict(alias_generator=None)

    bucket: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("bucket", "BUCKET")
    )
    prefix: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("prefix", "PREFIX")
    )
    chunk_size: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("chunkSize", "CHUNKSIZE"),
        serialization_alias="chunkSize",
    )
    max_obj_size_gb: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxObjSizeGB", "MAXOBJSIZEGB"),
        serialization_alias="maxObjSizeGB",
    )
    retryer: BackupConfigStorageGcsRetryer | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("retryer", "RETRYER")
    )


class BackupConfigStorageAzureRetryer(BaseCaseInsensitiveModel):
    """Represent Azure upload retry settings."""

    model_config = ConfigDict(alias_generator=None)

    num_max_retries: int | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("numMaxRetries", "NUMMAXRETRIES"),
        serialization_alias="numMaxRetries",
    )
    min_retry_delay: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("minRetryDelay", "MINRETRYDELAY"),
        serialization_alias="minRetryDelay",
    )
    max_retry_delay: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxRetryDelay", "MAXRETRYDELAY"),
        serialization_alias="maxRetryDelay",
    )


class BackupConfigStorageAzure(BaseCaseInsensitiveModel):
    """Represent Azure Blob Storage.

    ``account`` and ``container`` locate the storage and are settable; the access
    key under ``credentials`` is not, like every other backend's.
    """

    model_config = ConfigDict(alias_generator=None)

    account: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("account", "ACCOUNT")
    )
    container: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("container", "CONTAINER")
    )
    prefix: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("prefix", "PREFIX")
    )
    endpoint_url: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("endpointUrl", "ENDPOINTURL"),
        serialization_alias="endpointUrl",
    )
    endpoint_url_map: dict[str, str] | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("endpointUrlMap", "ENDPOINTURLMAP"),
        serialization_alias="endpointUrlMap",
    )
    max_obj_size_gb: float | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("maxObjSizeGB", "MAXOBJSIZEGB"),
        serialization_alias="maxObjSizeGB",
    )
    retryer: BackupConfigStorageAzureRetryer | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("retryer", "RETRYER")
    )


class BackupConfigStorageOciSSE(BaseCaseInsensitiveModel):
    """Represent OCI server-side encryption.

    ``sseCustomerKey`` is absent: it is the encryption key itself, so it falls under
    the credentials rule. ``kmsKeyID`` names a key rather than being one.
    """

    model_config = ConfigDict(alias_generator=None)

    kms_key_id: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("kmsKeyID", "KMSKEYID"),
        serialization_alias="kmsKeyID",
    )


class BackupConfigStorageOci(BaseCaseInsensitiveModel):
    """Represent Oracle Cloud Infrastructure Object Storage.

    The whole of ``credentials`` -- the auth type and the user principal's tenancy,
    user, fingerprint and private key -- is set with the ``pbm`` CLI, so OCI is
    configured here only after it has been credentialed there once.
    """

    model_config = ConfigDict(alias_generator=None)

    region: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("region", "REGION")
    )
    namespace: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("namespace", "NAMESPACE")
    )
    bucket: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("bucket", "BUCKET")
    )
    prefix: NonEmptyStr | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("prefix", "PREFIX")
    )
    server_side_encryption: BackupConfigStorageOciSSE | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("serverSideEncryption", "SERVERSIDEENCRYPTION"),
        serialization_alias="serverSideEncryption",
    )


class BackupConfigStorage(BaseCaseInsensitiveModel):
    """Represent Storage configuration."""

    model_config = ConfigDict(alias_generator=None)

    type: StorageType = Field(..., validation_alias=AliasChoices("type", "TYPE"))
    s3: BackupConfigStorageS3 | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("s3", "S3")
    )
    minio: BackupConfigStorageMinio | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("minio", "MINIO")
    )
    gcs: BackupConfigStorageGcs | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("gcs", "GCS")
    )
    azure: BackupConfigStorageAzure | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("azure", "AZURE")
    )
    oci: BackupConfigStorageOci | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("oci", "OCI")
    )
    filesystem: BackupConfigStorageFilesystem | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("filesystem", "FILESYSTEM")
    )


class BackupConfig(BaseCaseInsensitiveModel):
    """Represent the overall backup configuration.

    :param pbm_config_yaml_payload: The PBM yaml payload to parse from CLI.
    :type pbm_config_yaml_payload: NonEmptyStr | EmptyStrToNone
    """

    model_config = ConfigDict(alias_generator=None)

    storage: BackupConfigStorage | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("storage", "STORAGE")
    )
    pitr: BackupConfigPITR | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("pitr", "PITR")
    )
    backup: BackupConfigBackup | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("backup", "BACKUP")
    )
    restore: BackupConfigRestore | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("restore", "RESTORE")
    )
    pbm_config_yaml_payload: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices(
            "pbm_config_yaml_payload", "PBM_CONFIG_YAML_PAYLOAD"
        ),
        serialization_alias="pbm_config_yaml_payload",
    )
    credentials_path: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("credentials_path", "CREDENTIALS_PATH"),
    )


class _StorageConfigValidatorMixin:
    """Carry the shared create-time storage-config validator.

    Both request models (:class:`BackupCreate`, the Jinja form body, and
    :class:`BackupTaskWrite`, the JSON body) run the same cross-field storage check.
    Hosting it here keeps the validator from drifting between them.
    """

    @model_validator(mode="after")
    def _validate_storage(self) -> "_StorageConfigValidatorMixin":
        """Reject an incomplete or unsupported per-task storage config at create time.

        A body that names no storage at all is not incomplete -- it is a backup,
        which since the config split no longer carries storage. Storage belongs to
        the cluster's PBM configuration and is applied from the Configuration tab.
        Validating an absent block would reject every backup on ``storage_type``,
        a field the backups form no longer has. A body that sets *some* storage
        field without a type is still wrong, and still rejected.
        """
        values = {
            name: getattr(self, name, None)
            for name in type(self).model_fields
            if name.startswith("storage_")
        }
        if all(_is_unset(value) for value in values.values()):
            return self
        validate_storage_config(self.storage_type, values)
        return self


class _SelectiveBackupValidatorMixin:
    """Validate selective ``--ns`` / ``--with-users-and-roles`` combinations.

    Shared by :class:`BackupCreate` and :class:`BackupTaskWrite` so create and
    edit reject the same invalid pairings before a runner ever sees them.
    """

    @model_validator(mode="after")
    def _validate_selective_backup(self) -> "_SelectiveBackupValidatorMixin":
        """Reject ``with_users_and_roles`` unless namespaces are all ``db.*``."""
        validate_selective_users_and_roles(
            self.backup_namespaces,
            with_users_and_roles=bool(self.backup_with_users_and_roles),
        )
        return self


class BackupCreate(
    _SelectiveBackupValidatorMixin,
    _StorageConfigValidatorMixin,
    BaseCaseInsensitiveModel,
):
    """Represent a Backup creation form with proper case-insensitive fields.

    :param task_name: The PBM yaml payload to parse from CLI.
    :type task_name: NonEmptyStr
    :param hostname: The PBM yaml payload to parse from CLI.
    :type hostname: NonEmptyStr
    :param service_id: Service for executing PBM.
    :type service_id: int
    :param backup_type: Type of backup activity on PBM.
    :type backup_type: BackupType
    :param alert_on_fail: If True, send an alert if the task fails. Defaults to False.
    :type alert_on_fail: bool
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    service_id: int
    backup_type: BackupType
    alert_on_fail: bool = False
    pitr_oplog_span_min: int | EmptyStrToNone = None
    # Tri-state on purpose: None means the body said nothing about PITR, which
    # is what a backup sends now that configuration is a separate form. False
    # means an operator actually cleared the box on the Configuration tab and
    # wants PITR off. The apply merges, so writing `enabled: false` for the
    # first case would switch PITR off on a cluster where someone had enabled it.
    pitr_enabled: bool | EmptyStrToNone = None
    pitr_compression: NonEmptyStr | EmptyStrToNone = None
    storage_type: NonEmptyStr | EmptyStrToNone = None
    storage_s3_region: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_bucket: S3BucketName | EmptyStrToNone = None
    storage_s3_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_endpoint_url: StrHttpUrl | EmptyStrToNone = None
    storage_filesystem_path: StrippedNonEmptyStr | EmptyStrToNone = None
    # Backup options
    backup_priority: BackupPriorityYaml | EmptyStrToNone = None
    backup_compression: CompressionAlgorithm | EmptyStrToNone = None
    backup_compression_level: int | EmptyStrToNone = None
    backup_timeouts_starting_status: int | EmptyStrToNone = None
    backup_oplog_span_min: float | EmptyStrToNone = None
    backup_num_parallel_collections: int | EmptyStrToNone = None
    backup_namespaces: BackupNamespacesList | EmptyStrToNone = None
    backup_with_users_and_roles: bool = False
    # --- the rest of PBM's config file -------------------------------------
    # Credentials are absent by design throughout: storage.*.credentials and
    # serverSideEncryption.sseCustomerKey are the secrets SEP has nowhere safe to
    # keep. Read-merge-write carries whatever the pbm CLI set for them through an
    # apply untouched.
    pitr_oplog_only: bool = False
    pitr_compression_level: int | EmptyStrToNone = None
    pitr_priority: BackupPriorityYaml | EmptyStrToNone = None
    backup_num_parallel_files: int | EmptyStrToNone = None
    backup_timeouts_balancer_stop: int | EmptyStrToNone = None
    storage_s3_force_path_style: bool | EmptyStrToNone = None
    storage_s3_upload_part_size: int | EmptyStrToNone = None
    storage_s3_max_upload_parts: int | EmptyStrToNone = None
    storage_s3_storage_class: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_insecure_skip_tls_verify: bool | EmptyStrToNone = None
    storage_s3_debug_log_levels: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_max_obj_size_gb: float | EmptyStrToNone = None
    storage_s3_endpoint_url_map: PbmStringMapYaml | EmptyStrToNone = None
    storage_s3_sse_algorithm: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_sse_kms_key_id: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_sse_customer_algorithm: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_retryer_num_max_retries: int | EmptyStrToNone = None
    storage_s3_retryer_min_retry_delay: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_retryer_max_retry_delay: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_filesystem_max_obj_size_gb: float | EmptyStrToNone = None
    storage_minio_region: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_minio_bucket: S3BucketName | EmptyStrToNone = None
    storage_minio_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_minio_endpoint: StrHttpUrl | EmptyStrToNone = None
    storage_minio_endpoint_map: PbmStringMapYaml | EmptyStrToNone = None
    storage_minio_secure: bool | EmptyStrToNone = None
    storage_minio_insecure_skip_tls_verify: bool | EmptyStrToNone = None
    storage_minio_force_path_style: bool | EmptyStrToNone = None
    storage_minio_part_size: int | EmptyStrToNone = None
    storage_minio_max_obj_size_gb: float | EmptyStrToNone = None
    storage_minio_debug_trace: bool | EmptyStrToNone = None
    storage_minio_retryer_num_max_retries: int | EmptyStrToNone = None
    storage_gcs_bucket: S3BucketName | EmptyStrToNone = None
    storage_gcs_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_gcs_chunk_size: int | EmptyStrToNone = None
    storage_gcs_max_obj_size_gb: float | EmptyStrToNone = None
    storage_gcs_retryer_backoff_initial: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_gcs_retryer_backoff_max: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_gcs_retryer_backoff_multiplier: int | EmptyStrToNone = None
    storage_gcs_retryer_max_attempts: int | EmptyStrToNone = None
    storage_gcs_retryer_chunk_retry_deadline: StrippedNonEmptyStr | EmptyStrToNone = (
        None
    )
    storage_azure_account: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_azure_container: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_azure_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_azure_endpoint_url: StrHttpUrl | EmptyStrToNone = None
    storage_azure_endpoint_url_map: PbmStringMapYaml | EmptyStrToNone = None
    storage_azure_max_obj_size_gb: float | EmptyStrToNone = None
    storage_azure_retryer_num_max_retries: int | EmptyStrToNone = None
    storage_azure_retryer_min_retry_delay: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_azure_retryer_max_retry_delay: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_oci_region: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_oci_namespace: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_oci_bucket: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_oci_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_oci_sse_kms_key_id: StrippedNonEmptyStr | EmptyStrToNone = None
    restore_batch_size: int | EmptyStrToNone = None
    restore_num_insertion_workers: int | EmptyStrToNone = None
    restore_num_parallel_collections: int | EmptyStrToNone = None
    restore_num_parallel_files: int | EmptyStrToNone = None
    restore_num_download_workers: int | EmptyStrToNone = None
    restore_max_download_buffer_mb: int | EmptyStrToNone = None
    restore_download_chunk_mb: int | EmptyStrToNone = None
    restore_index_commit_quorum: StrippedNonEmptyStr | EmptyStrToNone = None
    restore_mongod_location: StrippedNonEmptyStr | EmptyStrToNone = None
    restore_mongod_location_map: PbmStringMapYaml | EmptyStrToNone = None
    restore_fallback_enabled: bool | EmptyStrToNone = None
    restore_allow_partly_done: bool | EmptyStrToNone = None
    restore_timeouts_balancer_stop: int | EmptyStrToNone = None
    # Path to MongoDB URI credentials file on the Nomad node (passed as task meta, used by payloads).
    credentials_path: NonEmptyStr | EmptyStrToNone = None


_S3_STORAGE = Requires(when=F("storage_type") == StorageType.S3.value)
_NOT_S3_STORAGE = Forbidden(when=F("storage_type") != StorageType.S3.value)
_NOT_FILESYSTEM_STORAGE = Forbidden(
    when=F("storage_type") != StorageType.FILESYSTEM.value
)
_NAMESPACES_WHEN_USERS_AND_ROLES = Requires(
    when=F("backup_with_users_and_roles") == True  # noqa: E712
)
_COMPRESSION_CHOICES = Choices(
    tuple((algorithm.value, algorithm.value) for algorithm in CompressionAlgorithm)
)


class _BackupMongoTaskForm(TaskFormModel):
    """Carry the Task section both MongoDB Backups forms present identically.

    The backups form and the PBM Configuration form diverge completely below the
    Task section -- one asks what to back up now, the other what the cluster's PBM
    configuration should be -- but both open by naming the task and choosing the
    MongoDB service and the executor host. Declaring that trio once here keeps the
    two from drifting on the part a user sees first.

    Order matters twice over: ``Ui(order=...)`` fixes the fields' order *within*
    the Task section, and this being the first block of fields on either subclass
    is what makes Task the first section -- ``derive_form_sections`` orders
    sections by where each one's first field is declared.
    """

    task_name: Annotated[
        NonEmptyStr,
        Ui(section="Task", order=0, default="mongodb-backup"),
    ]
    service_id: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MONGODB,)),
        Ui(label="Database Service", section="Task", order=1),
    ]
    hostname: Annotated[
        NonEmptyStr,
        HostRef(),
        Ui(
            label=EXECUTION_HOST_LABEL,
            section="Task",
            depends_on="service_id",
            order=2,
        ),
    ]


class BackupForm(_BackupMongoTaskForm):
    """Define the model-first schema source for the MongoDB Backups ``GET /schema``.

    The single source the derived ``GET /schema`` form renders from, driven by the
    :class:`Ui` / reference / :class:`Choices` / :class:`Forbidden` markers. It is
    *not* the JSON request body — :class:`BackupTaskWrite` is — and is never validated
    as one; field-declaration order reproduces the schema's section and field order
    (Task, Storage, Point-in-Time Recovery, Backup Options). ``task_name`` and
    ``hostname`` are redeclared here (still ``NonEmptyStr``) so the form can carry a
    presentation default for the task name, cascade the executor host from
    ``service_id``, and order the Task section as service → host. The
    ``alert_on_fail`` capability control stays inherited from
    :class:`TaskFormModel` (``Hidden``, off-schema). ``NonEmptyStr`` string
    fields emit ``min_length: 1`` on the wire schema (e.g. ``task_name``);
    ``HostRef`` selectors do not inherit string length constraints.

    ``credentials_path`` is deliberately absent: it is a property of the host the
    task runs on, not of one backup, and asking for it beside the database service
    reads as something the operator must fill in per run. It is declared once, as
    an advanced setting, on
    :class:`~app.sep.apps.backup_mongo.config.models.BackupConfigForm`. The field
    survives on :class:`BackupCreate` and :class:`BackupTaskWrite`, so a caller
    that sends it still gets it -- what changed is that the backups *form* no
    longer offers it, and an unset value falls back to ``$HOME/.mongodb_uri``
    exactly as an empty box did.
    """

    backup_compression: Annotated[
        str,
        _COMPRESSION_CHOICES,
        Ui(
            section="BackupOptions",
            description="Compression method for backup snapshots. Default: s2",
        ),
    ] = CompressionAlgorithm.S2.value
    backup_compression_level: Annotated[
        int | None, Ui(label="Compression Level", section="BackupOptions")
    ] = None
    backup_namespaces: Annotated[
        str | None,
        _NAMESPACES_WHEN_USERS_AND_ROLES,
        Ui(
            label="Namespaces (selective)",
            section="BackupOptions",
            description=(
                "Optional comma-separated db.collection or db.* list. Applied as "
                "pbm backup --ns on the logical sibling only (PBM rejects --ns for "
                "physical and incremental backups)."
            ),
        ),
    ] = None
    backup_with_users_and_roles: Annotated[
        bool,
        Ui(
            label="Include users and roles",
            section="BackupOptions",
            description=(
                "Pass --with-users-and-roles with selective backup. Valid only when "
                "every namespace is database-level (db.*)."
            ),
        ),
    ] = False


class BackupTaskWrite(
    _SelectiveBackupValidatorMixin, _StorageConfigValidatorMixin, BaseModel
):
    """Represent a JSON request body for creating a backup task group.

    Mirrors :class:`BackupCreate` except ``backup_type``, which is always
    ``pbm_config`` on create. POST creates the parent config task plus derived
    logical, physical, status, and incremental siblings.

    :param task_name: The name of the task to be created.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for the task execution.
    :type hostname: NonEmptyStr
    :param service_id: The Inventory ID of the MongoDB service to connect to.
    :type service_id: int
    :param alert_on_fail: If True, send an alert if the task fails.
    :type alert_on_fail: bool
    :param pitr_oplog_span_min: PITR oplog span in minutes.
    :type pitr_oplog_span_min: int | None
    :param pitr_enabled: Whether PITR is enabled, or ``None`` when the body says
        nothing about it.
    :type pitr_enabled: bool | None
    :param pitr_compression: PITR compression algorithm.
    :type pitr_compression: str | None
    :param storage_type: Storage backend type (``s3`` or ``filesystem``); required.
    :type storage_type: str
    :param storage_s3_region: S3 region when ``storage_type`` is ``s3``.
    :type storage_s3_region: str | None
    :param storage_s3_bucket: S3 bucket when ``storage_type`` is ``s3``.
    :type storage_s3_bucket: str | None
    :param storage_s3_prefix: S3 key prefix when ``storage_type`` is ``s3``.
    :type storage_s3_prefix: str | None
    :param storage_s3_endpoint_url: S3 endpoint URL when ``storage_type`` is ``s3``.
    :type storage_s3_endpoint_url: str | None
    :param storage_filesystem_path: Filesystem path when ``storage_type`` is
        ``filesystem``.
    :type storage_filesystem_path: str | None
    :param backup_priority: Node priority mapping as YAML.
    :type backup_priority: str | None
    :param backup_compression: Backup snapshot compression algorithm.
    :type backup_compression: CompressionAlgorithm | None
    :param backup_compression_level: Backup compression level.
    :type backup_compression_level: int | None
    :param backup_timeouts_starting_status: PBM starting-status timeout in seconds.
    :type backup_timeouts_starting_status: int | None
    :param backup_oplog_span_min: Logical backup oplog span in minutes.
    :type backup_oplog_span_min: float | None
    :param backup_num_parallel_collections: Parallel collections for logical backup.
    :type backup_num_parallel_collections: int | None
    :param backup_namespaces: Selective ``--ns`` namespaces for logical backups.
    :type backup_namespaces: str | None
    :param backup_with_users_and_roles: Opt-in ``--with-users-and-roles`` for ``db.*``.
    :type backup_with_users_and_roles: bool
    :param credentials_path: Path to MongoDB URI credentials on the Nomad node.
    :type credentials_path: str | None
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    service_id: int
    alert_on_fail: bool = False
    pitr_oplog_span_min: int | None = None
    # Tri-state on purpose: None means the body said nothing about PITR, which
    # is what a backup sends now that configuration is a separate form. False
    # means an operator actually cleared the box on the Configuration tab and
    # wants PITR off. The apply merges, so writing `enabled: false` for the
    # first case would switch PITR off on a cluster where someone had enabled it.
    pitr_enabled: bool | EmptyStrToNone = None
    pitr_compression: str | None = None
    storage_type: str
    storage_s3_region: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_bucket: S3BucketName | EmptyStrToNone = None
    storage_s3_prefix: StrippedNonEmptyStr | EmptyStrToNone = None
    storage_s3_endpoint_url: StrHttpUrl | EmptyStrToNone = None
    storage_filesystem_path: StrippedNonEmptyStr | EmptyStrToNone = None
    backup_priority: BackupPriorityYaml | EmptyStrToNone = None
    backup_compression: CompressionAlgorithm | None = None
    backup_compression_level: int | None = None
    backup_timeouts_starting_status: int | None = None
    backup_oplog_span_min: float | None = None
    backup_num_parallel_collections: int | None = None
    backup_namespaces: BackupNamespacesList | EmptyStrToNone = None
    backup_with_users_and_roles: bool = False
    credentials_path: str | None = None


class BackupDerivedTaskSummary(BaseModel):
    """Represent one derived sibling in a backup task detail response.

    :param name: The name of the derived task.
    :type name: str
    :param backup_type: The PBM backup type for this derived task.
    :type backup_type: str
    :param status: The latest execution status of the derived task.
    :type status: TaskHistoryStatusEnum | None
    """

    name: str
    backup_type: str
    status: TaskHistoryStatusEnum | None = None


class BackupTaskResponse(BackupTaskBase):
    """Represent a backup task API response.

    :param backup_type: The PBM backup type stored on the task.
    """

    backup_type: str


class BackupTaskDetailResponse(BackupTaskResponse):
    """Represent a backup task detail API response.

    :param derived_tasks: Latest status for each derived logical, physical,
        status, and incremental sibling.
    :type derived_tasks: list[BackupDerivedTaskSummary]
    :param latest_pbm_status: Tail of the latest PBM status task stdout, if
        available.
    :type latest_pbm_status: str | None
    """

    derived_tasks: list[BackupDerivedTaskSummary] = Field(default_factory=list)
    latest_pbm_status: str | None = None
