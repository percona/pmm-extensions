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

"""Build the ``run-python`` PBM backup task envelope for the MongoDB Backups app.

:func:`build_backup_mongo_spec` is the pure ``(form, resolved) -> TaskWrite`` builder
shared by the JSON create route (via the impure, 404-tolerant
:func:`~app.extensions.apps.backup_mongo.deps.build_backup_task_payload`) and the legacy
Jinja form path, so a backup task's Nomad payload is byte-identical regardless of the
call origin. PBM tasks run off ``form.hostname`` and the generated config; the
inventory service is resolved only to stamp ``_service_name`` for PMM, so this builder
takes the resolved name rather than reaching the inventory API itself. The envelope is
assembled through the framework's connectivity-optional ``build_run_python_task``
builder (not ``assemble_envelope``) because a PBM backup task carries no connectivity
meta, omits ``_service_name`` when the service was deleted, and keeps ``backup_type`` at
``data`` top level — the substitution token the cascade's ``DerivedTask`` payloads
rewrite.
"""

from dataclasses import dataclass
from typing import Any

import yaml

from app.core.utils.path import payload_uri
from app.extensions.apps.backup_mongo.models import (
    BackupConfig,
    BackupConfigBackup,
    BackupConfigPITR,
    BackupConfigRestore,
    BackupConfigStorage,
    BackupCreate,
    CompressionAlgorithm,
    OWNER,
    parse_backup_priority,
    parse_pbm_string_map,
    StorageType,
)
from app.extensions.apps.framework.spec import build_run_python_task
from app.tasks.models import TaskWrite

_BASE_REQUIREMENTS = "packaging\nPyYAML"


@dataclass(frozen=True, slots=True)
class BackupMongoResolved:
    """Carry the inventory facts resolved for a backup, all optional.

    :param service_name: The resolved service name, stamped into ``meta`` as
        ``_service_name`` when present. ``None`` when no service was specified or
        the stale ``service_id`` resolved to a deleted service.
    """

    service_name: str | None = None


def _drop_unset(values: dict[str, Any]) -> dict[str, Any]:
    """Return ``values`` without its ``None`` entries.

    An unset form field must not reach the PBM document, because the apply merges
    rather than replaces: a ``None`` written here would land on the cluster and
    clear whatever the operator had set for that key with the CLI. Only keys the
    form actually carries a value for are laid over what PBM already has.

    :param values: Candidate config keys, some unset.
    :return: The subset that carries a value.
    """
    return {key: value for key, value in values.items() if value is not None}


def _build_pitr_config(form: BackupCreate) -> dict[str, Any]:
    """Build PITR configuration from form data.

    :param form: The validated create body.
    :return: The ``pitr`` section, or ``{}`` when the form declares none.
    """
    pitr = _drop_unset(
        {
            "oplogSpanMin": form.pitr_oplog_span_min,
            "compressionLevel": form.pitr_compression_level,
            "priority": parse_backup_priority(form.pitr_priority)
            if form.pitr_priority
            else None,
        }
    )
    if form.pitr_oplog_only:
        pitr["oplogOnly"] = True
    if form.pitr_enabled is None and not pitr and not form.pitr_compression:
        # Nothing about PITR was asked for -- a backup, which no longer carries
        # configuration. Emitting `enabled: false` regardless would look like a
        # decision and, through the merge, would switch PITR off on a cluster where
        # someone had turned it on.
        return {}
    pitr["enabled"] = bool(form.pitr_enabled)
    pitr["compression"] = form.pitr_compression or CompressionAlgorithm.GZIP.value
    return pitr


def _build_s3_storage(form: BackupCreate) -> dict[str, Any]:
    """Build the ``storage.s3`` block."""
    sse = _drop_unset(
        {
            "sseAlgorithm": form.storage_s3_sse_algorithm,
            "kmsKeyID": form.storage_s3_sse_kms_key_id,
            "sseCustomerAlgorithm": form.storage_s3_sse_customer_algorithm,
        }
    )
    retryer = _drop_unset(
        {
            "numMaxRetries": form.storage_s3_retryer_num_max_retries,
            "minRetryDelay": form.storage_s3_retryer_min_retry_delay,
            "maxRetryDelay": form.storage_s3_retryer_max_retry_delay,
        }
    )
    return _drop_unset(
        {
            "region": form.storage_s3_region,
            "bucket": form.storage_s3_bucket,
            "prefix": form.storage_s3_prefix,
            "endpointUrl": form.storage_s3_endpoint_url,
            "endpointUrlMap": parse_pbm_string_map(form.storage_s3_endpoint_url_map)
            if form.storage_s3_endpoint_url_map
            else None,
            "forcePathStyle": form.storage_s3_force_path_style,
            "uploadPartSize": form.storage_s3_upload_part_size,
            "maxUploadParts": form.storage_s3_max_upload_parts,
            "storageClass": form.storage_s3_storage_class,
            "insecureSkipTLSVerify": form.storage_s3_insecure_skip_tls_verify,
            "debugLogLevels": form.storage_s3_debug_log_levels,
            "maxObjSizeGB": form.storage_s3_max_obj_size_gb,
            "serverSideEncryption": sse or None,
            "retryer": retryer or None,
        }
    )


def _build_minio_storage(form: BackupCreate) -> dict[str, Any]:
    """Build the ``storage.minio`` block.

    Note ``endpoint`` / ``endpointMap``, not the ``endpointUrl`` / ``endpointUrlMap``
    the S3 block uses -- PBM names them differently for this backend.
    """
    retryer = _drop_unset({"numMaxRetries": form.storage_minio_retryer_num_max_retries})
    return _drop_unset(
        {
            "region": form.storage_minio_region,
            "bucket": form.storage_minio_bucket,
            "prefix": form.storage_minio_prefix,
            "endpoint": form.storage_minio_endpoint,
            "endpointMap": parse_pbm_string_map(form.storage_minio_endpoint_map)
            if form.storage_minio_endpoint_map
            else None,
            "secure": form.storage_minio_secure,
            "insecureSkipTLSVerify": form.storage_minio_insecure_skip_tls_verify,
            "forcePathStyle": form.storage_minio_force_path_style,
            "partSize": form.storage_minio_part_size,
            "maxObjSizeGB": form.storage_minio_max_obj_size_gb,
            "debugTrace": form.storage_minio_debug_trace,
            "retryer": retryer or None,
        }
    )


def _build_gcs_storage(form: BackupCreate) -> dict[str, Any]:
    """Build the ``storage.gcs`` block."""
    retryer = _drop_unset(
        {
            "backoffInitial": form.storage_gcs_retryer_backoff_initial,
            "backoffMax": form.storage_gcs_retryer_backoff_max,
            "backoffMultiplier": form.storage_gcs_retryer_backoff_multiplier,
            "maxAttempts": form.storage_gcs_retryer_max_attempts,
            "chunkRetryDeadline": form.storage_gcs_retryer_chunk_retry_deadline,
        }
    )
    return _drop_unset(
        {
            "bucket": form.storage_gcs_bucket,
            "prefix": form.storage_gcs_prefix,
            "chunkSize": form.storage_gcs_chunk_size,
            "maxObjSizeGB": form.storage_gcs_max_obj_size_gb,
            "retryer": retryer or None,
        }
    )


def _build_filesystem_storage(form: BackupCreate) -> dict[str, Any]:
    """Build the ``storage.filesystem`` block."""
    return _drop_unset(
        {
            "path": form.storage_filesystem_path,
            "maxObjSizeGB": form.storage_filesystem_max_obj_size_gb,
        }
    )


#: One block builder per registered backend. Keyed by the same ``storage_type``
#: values as ``_STORAGE_BACKENDS`` in models.py, and asserted to cover them: a
#: backend the validator accepts but nothing here can serialise would be accepted
#: at create time and then silently dropped from the document.
_STORAGE_BUILDERS = {
    StorageType.S3.value: _build_s3_storage,
    StorageType.MINIO.value: _build_minio_storage,
    StorageType.GCS.value: _build_gcs_storage,
    StorageType.FILESYSTEM.value: _build_filesystem_storage,
}


def _build_storage_config(form: BackupCreate) -> dict[str, Any]:
    """Build storage configuration from form data.

    :param form: The validated create body.
    :return: The ``storage`` section, or ``{}`` when the form names no storage.
    """
    if form.storage_type is None:
        # Since configuration split away from backups, a backup carries no storage:
        # it is a property of the cluster, applied from the Configuration tab. This
        # used to raise, which turned every backup create into a 500.
        return {}
    storage_config = _STORAGE_BUILDERS[form.storage_type](form)

    return {"type": form.storage_type, form.storage_type: storage_config}


def _build_restore_config(form: BackupCreate) -> dict[str, Any]:
    """Build PBM's ``restore`` section from form data.

    :param form: The validated create body.
    :return: The ``restore`` section, or ``{}`` when the form declares none.
    """
    timeouts = _drop_unset({"balancerStop": form.restore_timeouts_balancer_stop})
    return _drop_unset(
        {
            "batchSize": form.restore_batch_size,
            "numInsertionWorkers": form.restore_num_insertion_workers,
            "numParallelCollections": form.restore_num_parallel_collections,
            "numParallelFiles": form.restore_num_parallel_files,
            "numDownloadWorkers": form.restore_num_download_workers,
            "maxDownloadBufferMb": form.restore_max_download_buffer_mb,
            "downloadChunkMb": form.restore_download_chunk_mb,
            "indexCommitQuorum": form.restore_index_commit_quorum,
            "mongodLocation": form.restore_mongod_location,
            "mongodLocationMap": parse_pbm_string_map(form.restore_mongod_location_map)
            if form.restore_mongod_location_map
            else None,
            "fallbackEnabled": form.restore_fallback_enabled,
            "allowPartlyDone": form.restore_allow_partly_done,
            "timeouts": timeouts or None,
        }
    )


def _build_backup_config_dict(form: BackupCreate) -> dict[str, Any]:
    """Build backup configuration dictionary from form data.

    :param form: The form data containing backup configuration fields.
    :return: A dictionary containing backup configuration settings such as priority,
        compression, compression level, timeouts, oplog span, parallel collections,
        and selective namespace flags. Returns an empty dictionary if no backup
        configuration fields are provided.
    """
    has_backup_config = any(
        (
            form.backup_priority,
            form.backup_compression,
            form.backup_compression_level is not None,
            form.backup_timeouts_starting_status is not None,
            form.backup_timeouts_balancer_stop is not None,
            form.backup_oplog_span_min is not None,
            form.backup_num_parallel_collections is not None,
            form.backup_num_parallel_files is not None,
            form.backup_namespaces,
            form.backup_with_users_and_roles,
        )
    )

    if not has_backup_config:
        return {}

    timeouts = _drop_unset(
        {
            "startingStatus": form.backup_timeouts_starting_status,
            "balancerStop": form.backup_timeouts_balancer_stop,
        }
    )
    return _drop_unset(
        {
            # Already validated at create time (BackupPriorityYaml), so this
            # cannot raise here.
            "priority": parse_backup_priority(form.backup_priority)
            if form.backup_priority
            else None,
            "compression": form.backup_compression or None,
            "compressionLevel": form.backup_compression_level,
            "timeouts": timeouts or None,
            "oplogSpanMin": form.backup_oplog_span_min,
            "numParallelCollections": form.backup_num_parallel_collections,
            "numParallelFiles": form.backup_num_parallel_files,
            "namespaces": form.backup_namespaces or None,
            # Only ever emitted as an opt-in: False here means "not asked for",
            # and PBM's own default is off.
            "withUsersAndRoles": True if form.backup_with_users_and_roles else None,
        }
    )


def build_backup_mongo_spec(
    form: BackupCreate, resolved: BackupMongoResolved
) -> TaskWrite:
    """Build the ``run-python`` PBM backup task envelope from the validated form.

    Compose the PITR, storage, and backup config sub-builders into the serialized
    ``BackupConfig`` YAML, select the ``file://`` payload by ``backup_type``, and
    stamp ``_service_name`` when a service resolved. ``backup_type`` is kept at the
    ``data`` top level — the substitution token the cascade ``DerivedTask`` payloads
    rewrite into the logical/physical/status/incremental siblings.

    :param form: The validated create form (a :class:`BackupCreate`).
    :param resolved: The inventory facts resolved for this backup.
    :return: The backup ``TaskWrite`` consumed by the Tasks API.
    """
    pitr = _build_pitr_config(form)
    storage = _build_storage_config(form)
    backup_config_dict = _build_backup_config_dict(form)
    restore = _build_restore_config(form)

    # Each section is omitted entirely when the form declared nothing for it. The
    # apply merges into the document already on the cluster, so an empty section
    # written here would not be a no-op -- it would lay defaults over settings an
    # operator had made with the CLI.
    backup_config = BackupConfig(
        storage=BackupConfigStorage.model_validate(storage) if storage else None,
        pitr=BackupConfigPITR.model_validate(pitr) if pitr else None,
        backup=BackupConfigBackup.model_validate(backup_config_dict)
        if backup_config_dict
        else None,
        restore=BackupConfigRestore.model_validate(restore) if restore else None,
        credentials_path=form.credentials_path or None,
    )

    requirements = _BASE_REQUIREMENTS

    return build_run_python_task(
        name=form.task_name,
        owner=OWNER,
        target=form.hostname,
        config=yaml.dump(
            backup_config.model_dump(by_alias=True, exclude_none=True, mode="json"),
            default_flow_style=False,
            allow_unicode=True,
        ),
        requirements=requirements,
        payload=payload_uri(__file__, f"{form.backup_type}_payload"),
        service_name=resolved.service_name,
        extra_data={"backup_type": form.backup_type},
        alert_on_fail=form.alert_on_fail,
    )
