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

from app.extensions.apps.backup_mongo.models import (
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
from app.extensions.apps.framework.form_dsl import (
    Choices,
    FieldWidget,
    ServiceRef,
    Ui,
)
from app.inventory.models import ServiceTypeEnum

ADVANCED_SECTION = "Advanced"
#: One section per storage backend, each hidden unless ``storage_type`` selects it.
#: The alternative -- every backend's fields in one Storage section, each carrying
#: its own ``Forbidden`` -- puts ~80 fields in a section showing ten. Mirrors the
#: per-variant sections in ``app/extensions/apps/mysql_backups/views.py``.
S3_SECTION = "StorageS3"
S3_TUNING_SECTION = "StorageS3Tuning"
MINIO_SECTION = "StorageMinio"
MINIO_TUNING_SECTION = "StorageMinioTuning"
GCS_SECTION = "StorageGcs"
GCS_TUNING_SECTION = "StorageGcsTuning"
AZURE_SECTION = "StorageAzure"
AZURE_TUNING_SECTION = "StorageAzureTuning"
#: OCI carries few enough keys to need no tuning section of its own.
OCI_SECTION = "StorageOci"
FILESYSTEM_SECTION = "StorageFilesystem"
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
        Choices(
            (
                ("s3", "S3-compatible"),
                ("minio", "MinIO"),
                ("gcs", "Google Cloud Storage"),
                ("azure", "Azure Blob Storage"),
                ("oci", "OCI Object Storage"),
                ("filesystem", "Filesystem"),
            )
        ),
        Ui(section="Storage"),
    ] = StorageType.S3.value
    storage_s3_region: Annotated[
        str | None,
        _S3_STORAGE,
        _NOT_S3_STORAGE,
        Ui(
            label="Region",
            section=S3_SECTION,
            description="Required for S3 storage.",
        ),
    ] = None
    storage_s3_bucket: Annotated[
        str | None,
        _S3_STORAGE,
        _NOT_S3_STORAGE,
        Ui(
            label="Bucket",
            section=S3_SECTION,
            description="Required for S3 storage.",
        ),
    ] = None
    storage_s3_prefix: Annotated[
        str | None, _NOT_S3_STORAGE, Ui(label="Prefix", section=S3_SECTION)
    ] = None
    storage_s3_endpoint_url: Annotated[
        str | None, _NOT_S3_STORAGE, Ui(label="Endpoint URL", section=S3_SECTION)
    ] = None
    storage_s3_force_path_style: Annotated[
        bool | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Force Path-Style URLs",
            section=S3_SECTION,
            description=(
                "Address the bucket as a path rather than a subdomain. Required by "
                "MinIO and most S3-compatible servers."
            ),
        ),
    ] = None
    storage_s3_upload_part_size: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Upload Part Size (bytes)",
            section=S3_TUNING_SECTION,
            description="Chunk size for multipart uploads. PBM defaults to 10MB.",
        ),
    ] = None
    storage_s3_max_upload_parts: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(label="Max Upload Parts", section=S3_TUNING_SECTION),
    ] = None
    storage_s3_storage_class: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(label="Storage Class", section=S3_TUNING_SECTION),
    ] = None
    storage_s3_insecure_skip_tls_verify: Annotated[
        bool | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Skip TLS Verification",
            section=S3_TUNING_SECTION,
            description="Accept any certificate from the endpoint. Test use only.",
        ),
    ] = None
    storage_s3_debug_log_levels: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Debug Log Levels",
            section=S3_TUNING_SECTION,
            description=(
                "Comma-separated AWS SDK log flags: Signing, Retries, Request, "
                "RequestWithBody, Response, ResponseWithBody, DeprecatedUsage."
            ),
        ),
    ] = None
    storage_s3_max_obj_size_gb: Annotated[
        float | None,
        _NOT_S3_STORAGE,
        Ui(label="Max Object Size (GB)", section=S3_TUNING_SECTION),
    ] = None
    storage_s3_endpoint_url_map: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Per-Node Endpoint URLs (YAML)",
            section=S3_TUNING_SECTION,
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
            section=S3_TUNING_SECTION,
            description="Server-side encryption algorithm, e.g. aws:kms.",
        ),
    ] = None
    storage_s3_sse_kms_key_id: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(label="SSE KMS Key ID", section=S3_TUNING_SECTION),
    ] = None
    storage_s3_sse_customer_algorithm: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="SSE Customer Algorithm",
            section=S3_TUNING_SECTION,
            description=(
                "AES256 for customer-provided keys. The key itself is not settable "
                "here -- set it with the pbm CLI, as with the access keys."
            ),
        ),
    ] = None
    storage_s3_retryer_num_max_retries: Annotated[
        int | None,
        _NOT_S3_STORAGE,
        Ui(label="Upload Retries", section=S3_TUNING_SECTION),
    ] = None
    storage_s3_retryer_min_retry_delay: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Min Retry Delay",
            section=S3_TUNING_SECTION,
            description="Go duration, e.g. 30ms.",
        ),
    ] = None
    storage_s3_retryer_max_retry_delay: Annotated[
        str | None,
        _NOT_S3_STORAGE,
        Ui(
            label="Max Retry Delay",
            section=S3_TUNING_SECTION,
            description="Go duration, e.g. 5m.",
        ),
    ] = None
    storage_minio_region: Annotated[
        str | None, Ui(label="Region", section=MINIO_SECTION)
    ] = None
    storage_minio_bucket: Annotated[
        str | None,
        Ui(label="Bucket", section=MINIO_SECTION, description="Required for MinIO."),
    ] = None
    storage_minio_prefix: Annotated[
        str | None, Ui(label="Prefix", section=MINIO_SECTION)
    ] = None
    storage_minio_endpoint: Annotated[
        str | None,
        Ui(
            label="Endpoint",
            section=MINIO_SECTION,
            description=(
                "Required. PBM names this backend's endpoint `endpoint`, not "
                "`endpointUrl` as the S3 backend does."
            ),
        ),
    ] = None
    storage_minio_secure: Annotated[
        bool | None, Ui(label="Use HTTPS", section=MINIO_SECTION)
    ] = None
    storage_minio_force_path_style: Annotated[
        bool | None, Ui(label="Force Path-Style URLs", section=MINIO_SECTION)
    ] = None
    storage_minio_insecure_skip_tls_verify: Annotated[
        bool | None,
        Ui(
            label="Skip TLS Verification",
            section=MINIO_TUNING_SECTION,
            description="Accept any certificate from the endpoint. Test use only.",
        ),
    ] = None
    storage_minio_part_size: Annotated[
        int | None,
        Ui(
            label="Part Size (bytes)",
            section=MINIO_TUNING_SECTION,
            description="Chunk size for multipart uploads. PBM defaults to 10MB.",
        ),
    ] = None
    storage_minio_max_obj_size_gb: Annotated[
        float | None, Ui(label="Max Object Size (GB)", section=MINIO_TUNING_SECTION)
    ] = None
    storage_minio_debug_trace: Annotated[
        bool | None, Ui(label="HTTP Trace Logging", section=MINIO_TUNING_SECTION)
    ] = None
    storage_minio_retryer_num_max_retries: Annotated[
        int | None, Ui(label="Upload Retries", section=MINIO_TUNING_SECTION)
    ] = None
    storage_minio_endpoint_map: Annotated[
        str | None,
        Ui(
            label="Per-Node Endpoints (YAML)",
            section=MINIO_TUNING_SECTION,
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of node address to the endpoint that node should use."
            ),
        ),
    ] = None
    storage_gcs_bucket: Annotated[
        str | None,
        Ui(label="Bucket", section=GCS_SECTION, description="Required for GCS."),
    ] = None
    storage_gcs_prefix: Annotated[
        str | None, Ui(label="Prefix", section=GCS_SECTION)
    ] = None
    storage_gcs_chunk_size: Annotated[
        int | None,
        Ui(
            label="Chunk Size (bytes)",
            section=GCS_TUNING_SECTION,
            description="Upload chunk size. PBM defaults to 10MB.",
        ),
    ] = None
    storage_gcs_max_obj_size_gb: Annotated[
        float | None, Ui(label="Max Object Size (GB)", section=GCS_TUNING_SECTION)
    ] = None
    storage_gcs_retryer_backoff_initial: Annotated[
        str | None,
        Ui(
            label="Initial Backoff",
            section=GCS_TUNING_SECTION,
            description="Go duration, e.g. 1s.",
        ),
    ] = None
    storage_gcs_retryer_backoff_max: Annotated[
        str | None,
        Ui(
            label="Max Backoff",
            section=GCS_TUNING_SECTION,
            description="Go duration, e.g. 30s.",
        ),
    ] = None
    storage_gcs_retryer_backoff_multiplier: Annotated[
        int | None, Ui(label="Backoff Multiplier", section=GCS_TUNING_SECTION)
    ] = None
    storage_gcs_retryer_max_attempts: Annotated[
        int | None, Ui(label="Max Attempts", section=GCS_TUNING_SECTION)
    ] = None
    storage_gcs_retryer_chunk_retry_deadline: Annotated[
        str | None,
        Ui(
            label="Chunk Retry Deadline",
            section=GCS_TUNING_SECTION,
            description="Go duration, e.g. 32s.",
        ),
    ] = None
    storage_azure_account: Annotated[
        str | None,
        Ui(
            label="Storage Account",
            section=AZURE_SECTION,
            description="Required. The Azure storage account name.",
        ),
    ] = None
    storage_azure_container: Annotated[
        str | None,
        Ui(label="Container", section=AZURE_SECTION, description="Required."),
    ] = None
    storage_azure_prefix: Annotated[
        str | None, Ui(label="Prefix", section=AZURE_SECTION)
    ] = None
    storage_azure_endpoint_url: Annotated[
        str | None,
        Ui(
            label="Endpoint URL",
            section=AZURE_SECTION,
            description=(
                "Defaults to the public blob endpoint for the account. Set it for "
                "sovereign clouds or a private endpoint."
            ),
        ),
    ] = None
    storage_azure_max_obj_size_gb: Annotated[
        float | None, Ui(label="Max Object Size (GB)", section=AZURE_TUNING_SECTION)
    ] = None
    storage_azure_retryer_num_max_retries: Annotated[
        int | None, Ui(label="Upload Retries", section=AZURE_TUNING_SECTION)
    ] = None
    storage_azure_retryer_min_retry_delay: Annotated[
        str | None,
        Ui(
            label="Min Retry Delay",
            section=AZURE_TUNING_SECTION,
            description="Go duration, e.g. 800ms.",
        ),
    ] = None
    storage_azure_retryer_max_retry_delay: Annotated[
        str | None,
        Ui(
            label="Max Retry Delay",
            section=AZURE_TUNING_SECTION,
            description="Go duration, e.g. 60s.",
        ),
    ] = None
    storage_azure_endpoint_url_map: Annotated[
        str | None,
        Ui(
            label="Per-Node Endpoint URLs (YAML)",
            section=AZURE_TUNING_SECTION,
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of node address to the endpoint that node should use."
            ),
        ),
    ] = None
    storage_oci_region: Annotated[
        str | None,
        Ui(label="Region", section=OCI_SECTION, description="Required."),
    ] = None
    storage_oci_namespace: Annotated[
        str | None,
        Ui(
            label="Namespace",
            section=OCI_SECTION,
            description="Required. The Object Storage namespace for the tenancy.",
        ),
    ] = None
    storage_oci_bucket: Annotated[
        str | None,
        Ui(label="Bucket", section=OCI_SECTION, description="Required."),
    ] = None
    storage_oci_prefix: Annotated[
        str | None, Ui(label="Prefix", section=OCI_SECTION)
    ] = None
    storage_oci_sse_kms_key_id: Annotated[
        str | None,
        Ui(
            label="KMS Key OCID",
            section=OCI_SECTION,
            description=(
                "Names a key for server-side encryption. The customer-supplied key "
                "itself is set with the pbm CLI, as credentials are."
            ),
        ),
    ] = None
    storage_filesystem_path: Annotated[
        str, _NOT_FILESYSTEM_STORAGE, Ui(label="Path", section=FILESYSTEM_SECTION)
    ]
    storage_filesystem_max_obj_size_gb: Annotated[
        float | None,
        _NOT_FILESYSTEM_STORAGE,
        Ui(label="Max Object Size (GB)", section=FILESYSTEM_SECTION),
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
