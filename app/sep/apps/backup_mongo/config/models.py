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

"""Define the schema source for the PBM Configuration child app's form."""

from typing import Annotated

from app.inventory.models import ServiceTypeEnum
from app.sep.apps.backup_mongo.models import (
    _BackupMongoTaskForm,
    _COMPRESSION_CHOICES,
    _NOT_FILESYSTEM_STORAGE,
    _NOT_S3_STORAGE,
    _S3_STORAGE,
    BackupCreate,
    BackupType,
    CompressionAlgorithm,
    StorageType,
)
from app.sep.apps.framework.form_dsl import (
    Choices,
    FieldWidget,
    ServiceRef,
    Ui,
)

ADVANCED_SECTION = "Advanced"
#: Expert storage knobs -- retry, encryption, chunking -- kept out of the common
#: Storage section so the four fields that actually point PBM at a bucket stay
#: visible. Marked ``advanced`` in the layout, so it shares one disclosure control
#: with :data:`ADVANCED_SECTION` rather than adding a second.
STORAGE_TUNING_SECTION = "StorageTuning"
#: PBM's ``restore`` section: cluster-wide tuning for how a restore runs. Not to be
#: confused with the Restores app, which creates restore *tasks* and reads none of
#: this -- these are keys in the config document ``pbm config --file`` writes.
RESTORE_SECTION = "RestoreTuning"


class BackupConfigForm(_BackupMongoTaskForm):
    """Define the schema source for the PBM Configuration form.

    This is where PBM's cluster-wide configuration is declared: storage,
    point-in-time recovery, and the backup options that are properties of the
    deployment rather than of one run. Its sibling ``BackupForm`` keeps only what
    a single backup decides -- compression, selective namespaces, users and roles.
    The two shared one form until it became clear the sharing was the bug: every
    backup carried a whole PBM configuration it had no business restating, and
    applying it overwrote the cluster's.

    Section order follows field-declaration order, so the blocks below read Task,
    Storage, Point-in-Time Recovery, Backup Options, Advanced.
    """

    service_id: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MONGODB,)),
        Ui(
            label="Cluster member",
            section="Task",
            order=1,
            description=(
                "PBM keeps one configuration per cluster, so any member selects it. "
                "The list groups members by the cluster label PMM reports."
            ),
        ),
    ]
    storage_type: Annotated[
        str,
        Choices((("s3", "S3-compatible"), ("filesystem", "Filesystem"))),
        Ui(section="Storage"),
    ] = StorageType.S3.value
    storage_s3_region: Annotated[
        str | None,
        _S3_STORAGE,
        _NOT_S3_STORAGE,
        Ui(
            label="S3 Region",
            section="Storage",
            description="Required for S3 storage.",
        ),
    ] = None
    storage_s3_bucket: Annotated[
        str | None,
        _S3_STORAGE,
        _NOT_S3_STORAGE,
        Ui(
            label="S3 Bucket",
            section="Storage",
            description="Required for S3 storage.",
        ),
    ] = None
    storage_s3_prefix: Annotated[
        str | None, _NOT_S3_STORAGE, Ui(label="S3 Prefix", section="Storage")
    ] = None
    storage_s3_endpoint_url: Annotated[
        str | None, _NOT_S3_STORAGE, Ui(label="S3 Endpoint URL", section="Storage")
    ] = None
    storage_s3_force_path_style: Annotated[
        bool | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Force Path-Style URLs",
            section="Storage",
            description=(
                "Address the bucket as a path rather than a subdomain. Required by "
                "MinIO and most S3-compatible servers."
            ),
        ),
    ] = None
    storage_filesystem_path: Annotated[
        str, _NOT_FILESYSTEM_STORAGE, Ui(label="Filesystem Path", section="Storage")
    ]
    storage_s3_upload_part_size: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Upload Part Size (bytes)",
            section=STORAGE_TUNING_SECTION,
            description="Chunk size for multipart uploads. PBM defaults to 10MB.",
        ),
    ] = None
    storage_s3_max_upload_parts: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(label="Max Upload Parts", section=STORAGE_TUNING_SECTION),
    ] = None
    storage_s3_storage_class: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(label="Storage Class", section=STORAGE_TUNING_SECTION),
    ] = None
    storage_s3_insecure_skip_tls_verify: Annotated[
        bool | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Skip TLS Verification",
            section=STORAGE_TUNING_SECTION,
            description="Accept any certificate from the endpoint. Test use only.",
        ),
    ] = None
    storage_s3_debug_log_levels: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Debug Log Levels",
            section=STORAGE_TUNING_SECTION,
            description=(
                "Comma-separated AWS SDK log flags: Signing, Retries, Request, "
                "RequestWithBody, Response, ResponseWithBody, DeprecatedUsage."
            ),
        ),
    ] = None
    storage_s3_max_obj_size_gb: Annotated[
        float | None,
        _NOT_S3_STORAGE,
        Ui(label="Max Object Size (GB)", section=STORAGE_TUNING_SECTION),
    ] = None
    storage_s3_endpoint_url_map: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Per-Node Endpoint URLs (YAML)",
            section=STORAGE_TUNING_SECTION,
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of node address to the endpoint that node should use, "
                "e.g.:\n"
                '"host1:27018": "http://minio-a:9000"'
            ),
        ),
    ] = None
    storage_s3_sse_algorithm: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="SSE Algorithm",
            section=STORAGE_TUNING_SECTION,
            description="Server-side encryption algorithm, e.g. aws:kms.",
        ),
    ] = None
    storage_s3_sse_kms_key_id: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(label="SSE KMS Key ID", section=STORAGE_TUNING_SECTION),
    ] = None
    storage_s3_sse_customer_algorithm: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="SSE Customer Algorithm",
            section=STORAGE_TUNING_SECTION,
            description=(
                "AES256 for customer-provided keys. The key itself is not settable "
                "here -- set it with the pbm CLI, as with the access keys."
            ),
        ),
    ] = None
    storage_s3_retryer_num_max_retries: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(label="Upload Retries", section=STORAGE_TUNING_SECTION),
    ] = None
    storage_s3_retryer_min_retry_delay: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Min Retry Delay",
            section=STORAGE_TUNING_SECTION,
            description="Go duration, e.g. 30ms.",
        ),
    ] = None
    storage_s3_retryer_max_retry_delay: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Max Retry Delay",
            section=STORAGE_TUNING_SECTION,
            description="Go duration, e.g. 5m.",
        ),
    ] = None
    storage_filesystem_max_obj_size_gb: Annotated[
        float | None,
        _NOT_FILESYSTEM_STORAGE,
        Ui(label="Max Object Size (GB)", section=STORAGE_TUNING_SECTION),
    ] = None
    pitr_enabled: Annotated[bool, Ui(label="Enable PITR", section="PITR")] = False
    pitr_oplog_span_min: Annotated[
        int | None, Ui(label="Oplog Span (minutes)", section="PITR")
    ] = None
    pitr_compression: Annotated[
        str, _COMPRESSION_CHOICES, Ui(label="PITR Compression", section="PITR")
    ] = CompressionAlgorithm.GZIP.value
    pitr_compression_level: Annotated[
        int | None, Ui(label="PITR Compression Level", section="PITR")
    ] = None
    pitr_oplog_only: Annotated[
        bool,
        Ui(
            label="Oplog Only",
            section="PITR",
            description=(
                "Replicate the oplog without taking the base backup PITR normally "
                "requires."
            ),
        ),
    ] = False
    pitr_priority: Annotated[
        str | None,
        Ui(
            label="PITR Node Priority (YAML)",
            section="PITR",
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of mongod addresses to oplog-slicing priority "
                "(highest wins), in the same shape as the backup priority below."
            ),
        ),
    ] = None
    backup_priority: Annotated[
        str | None,
        Ui(
            label="Node Priority (YAML)",
            section="BackupOptions",
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of mongod addresses to backup priority (highest wins). "
                "One entry per line, e.g.:\n"
                '"host1:27018": 2\n'
                '"host2:27018": 1'
            ),
        ),
    ] = None
    backup_timeouts_starting_status: Annotated[
        int | None,
        Ui(label="Starting Status Timeout (seconds)", section="BackupOptions"),
    ] = None
    backup_oplog_span_min: Annotated[
        float | None, Ui(label="Backup Oplog Span (minutes)", section="BackupOptions")
    ] = None
    backup_num_parallel_collections: Annotated[
        int | None, Ui(label="Parallel Collections", section="BackupOptions")
    ] = None
    backup_num_parallel_files: Annotated[
        int | None, Ui(label="Parallel Files", section="BackupOptions")
    ] = None
    backup_timeouts_balancer_stop: Annotated[
        int | None,
        Ui(
            label="Balancer Stop Timeout (seconds)",
            section="BackupOptions",
            description="Sharded clusters only: how long to wait for the balancer to stop.",
        ),
    ] = None
    restore_batch_size: Annotated[
        int | None, Ui(label="Batch Size", section=RESTORE_SECTION)
    ] = None
    restore_num_insertion_workers: Annotated[
        int | None, Ui(label="Insertion Workers", section=RESTORE_SECTION)
    ] = None
    restore_num_parallel_collections: Annotated[
        int | None, Ui(label="Parallel Collections", section=RESTORE_SECTION)
    ] = None
    restore_num_parallel_files: Annotated[
        int | None, Ui(label="Parallel Files", section=RESTORE_SECTION)
    ] = None
    restore_num_download_workers: Annotated[
        int | None, Ui(label="Download Workers", section=RESTORE_SECTION)
    ] = None
    restore_max_download_buffer_mb: Annotated[
        int | None, Ui(label="Max Download Buffer (MB)", section=RESTORE_SECTION)
    ] = None
    restore_download_chunk_mb: Annotated[
        int | None, Ui(label="Download Chunk (MB)", section=RESTORE_SECTION)
    ] = None
    restore_index_commit_quorum: Annotated[
        str | None,
        Ui(
            label="Index Commit Quorum",
            section=RESTORE_SECTION,
            description='"votingMembers", "majority", a number, or a tag name.',
        ),
    ] = None
    restore_mongod_location: Annotated[
        str | None,
        Ui(
            label="mongod Binary Path",
            section=RESTORE_SECTION,
            description="Physical restores only: where the mongod binary lives.",
        ),
    ] = None
    restore_mongod_location_map: Annotated[
        str | None,
        Ui(
            label="Per-Node mongod Paths (YAML)",
            section=RESTORE_SECTION,
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of node address to that node's mongod path, e.g.:\n"
                '"host1:27018": "/usr/bin/mongod"'
            ),
        ),
    ] = None
    restore_fallback_enabled: Annotated[
        bool | None,
        Ui(
            label="Enable Fallback",
            section=RESTORE_SECTION,
            description="Keep a copy of the current data so a failed physical restore can roll back.",
        ),
    ] = None
    restore_allow_partly_done: Annotated[
        bool | None,
        Ui(label="Allow Partly Done", section=RESTORE_SECTION),
    ] = None
    restore_timeouts_balancer_stop: Annotated[
        int | None,
        Ui(label="Balancer Stop Timeout (seconds)", section=RESTORE_SECTION),
    ] = None
    credentials_path: Annotated[
        str | None,
        Ui(
            label="Credentials Path",
            section=ADVANCED_SECTION,
            description=(
                "Path to the file holding the MongoDB URI on the execution host. "
                "Leave empty to use $HOME/.mongodb_uri."
            ),
        ),
    ] = None


class BackupConfigCreate(BackupCreate):
    """Validate the Configuration tab's POST body.

    The framework derives a ``payload_builder`` app's request model from the
    builder's ``form`` annotation, so the parent's :class:`BackupCreate` would be
    this route's body model -- and ``backup_type`` is required there, while the
    Configuration form has no such field. Every create from the tab would 422 on a
    key the UI has no way to know about.

    The parent avoids this by posting :class:`BackupTaskWrite` through a cascade
    leg that stamps ``pbm_config`` on the way past. This app has no cascade, so it
    says the same thing with a default: a config task is a ``pbm_config`` task, and
    there was never a second value a caller could legitimately send here.
    """

    backup_type: BackupType = BackupType.PBM_CONFIG
