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

"""Cover the config form's reach over PBM's config file.

A form field that never arrives at ``pbm config --file`` under the key PBM reads
is worse than a missing field: it looks settable and silently does nothing. These
drive the whole panel through the spec builder and assert the resulting document
key by key, in PBM's own camelCase.
"""

import pytest
import yaml
from pydantic import ValidationError

from app.extensions.apps.backup_mongo.config.models import BackupConfigForm
from app.extensions.apps.backup_mongo.config.schema import backup_mongo_config_schema
from app.extensions.apps.backup_mongo.models import (
    _STORAGE_BACKENDS,
    BackupCreate,
    BackupType,
)
from app.extensions.apps.backup_mongo.spec import (
    _STORAGE_BUILDERS,
    BackupMongoResolved,
    build_backup_mongo_spec,
)

#: Every configuration field the panel offers, with a value distinguishable from
#: both PBM's default and its neighbours, so a key crossing wires is visible.
FULL_FORM = {
    "task_name": "pbm-config",
    "hostname": "mongo-host",
    "service_id": 1,
    "backup_type": BackupType.PBM_CONFIG,
    "storage_type": "s3",
    "storage_s3_region": "eu-west-1",
    "storage_s3_bucket": "backups",
    "storage_s3_prefix": "mongo",
    "storage_s3_endpoint_url": "https://s3.example.com",
    "storage_s3_force_path_style": True,
    "storage_s3_upload_part_size": 20971520,
    "storage_s3_max_upload_parts": 7777,
    "storage_s3_storage_class": "GLACIER_IR",
    "storage_s3_insecure_skip_tls_verify": True,
    "storage_s3_debug_log_levels": "Signing,Retries",
    "storage_s3_max_obj_size_gb": 1234.5,
    "storage_s3_endpoint_url_map": '"host1:27018": "http://minio-a:9000"',
    "storage_s3_sse_algorithm": "aws:kms",
    "storage_s3_sse_kms_key_id": "key-abc",
    "storage_s3_sse_customer_algorithm": "AES256",
    "storage_s3_retryer_num_max_retries": 9,
    "storage_s3_retryer_min_retry_delay": "40ms",
    "storage_s3_retryer_max_retry_delay": "7m",
    "pitr_enabled": True,
    "pitr_oplog_span_min": 15,
    "pitr_compression": "zstd",
    "pitr_compression_level": 3,
    "pitr_oplog_only": True,
    "pitr_priority": '"host1:27018": 2.0',
    "backup_priority": '"host2:27018": 1.0',
    "backup_timeouts_starting_status": 120,
    "backup_timeouts_balancer_stop": 45,
    "backup_oplog_span_min": 10.0,
    "backup_num_parallel_collections": 4,
    "backup_num_parallel_files": 6,
    "restore_batch_size": 500,
    "restore_num_insertion_workers": 8,
    "restore_num_parallel_collections": 3,
    "restore_num_parallel_files": 5,
    "restore_num_download_workers": 7,
    "restore_max_download_buffer_mb": 256,
    "restore_download_chunk_mb": 32,
    "restore_index_commit_quorum": "majority",
    "restore_mongod_location": "/usr/bin/mongod",
    "restore_mongod_location_map": '"host1:27018": "/opt/mongod"',
    "restore_fallback_enabled": True,
    "restore_allow_partly_done": False,
    "restore_timeouts_balancer_stop": 60,
    "credentials_path": "/run/pbm/mongodb-uri",
}


def _document(overrides: dict | None = None) -> dict:
    """Return the PBM config document the spec builder produces for ``FULL_FORM``."""
    form = BackupCreate.model_validate({**FULL_FORM, **(overrides or {})})
    task = build_backup_mongo_spec(form, BackupMongoResolved(service_name="svc"))
    return yaml.safe_load(task.data["meta"]["config"])


class TestEveryFormFieldReachesPbm:
    """Assert each section lands under the keys PBM actually reads."""

    def test_storage_section(self) -> None:
        """Serialise every S3 key, nested exactly as PBM nests them."""
        s3 = _document()["storage"]["s3"]

        assert s3 == {
            "region": "eu-west-1",
            "bucket": "backups",
            "prefix": "mongo",
            "endpointUrl": "https://s3.example.com",
            "endpointUrlMap": {"host1:27018": "http://minio-a:9000"},
            "forcePathStyle": True,
            "uploadPartSize": 20971520,
            "maxUploadParts": 7777,
            "storageClass": "GLACIER_IR",
            "insecureSkipTLSVerify": True,
            "debugLogLevels": "Signing,Retries",
            "maxObjSizeGB": 1234.5,
            "serverSideEncryption": {
                "sseAlgorithm": "aws:kms",
                "kmsKeyID": "key-abc",
                "sseCustomerAlgorithm": "AES256",
            },
            "retryer": {
                "numMaxRetries": 9,
                "minRetryDelay": "40ms",
                "maxRetryDelay": "7m",
            },
        }

    def test_pitr_section(self) -> None:
        """Serialise every PITR key, priority parsed out of its YAML."""
        assert _document()["pitr"] == {
            "enabled": True,
            "oplogSpanMin": 15,
            "compression": "zstd",
            "compressionLevel": 3,
            "oplogOnly": True,
            "priority": {"host1:27018": 2.0},
        }

    def test_backup_section(self) -> None:
        """Serialise the configuration-shaped backup keys, timeouts nested."""
        assert _document()["backup"] == {
            "priority": {"host2:27018": 1.0},
            "oplogSpanMin": 10.0,
            "numParallelCollections": 4,
            "numParallelFiles": 6,
            "timeouts": {"startingStatus": 120, "balancerStop": 45},
        }

    def test_restore_section(self) -> None:
        """Serialise PBM's restore section, which the panel had no reach into before."""
        assert _document()["restore"] == {
            "batchSize": 500,
            "numInsertionWorkers": 8,
            "numParallelCollections": 3,
            "numParallelFiles": 5,
            "numDownloadWorkers": 7,
            "maxDownloadBufferMb": 256,
            "downloadChunkMb": 32,
            "indexCommitQuorum": "majority",
            "mongodLocation": "/usr/bin/mongod",
            "mongodLocationMap": {"host1:27018": "/opt/mongod"},
            "fallbackEnabled": True,
            "allowPartlyDone": False,
            "timeouts": {"balancerStop": 60},
        }

    def test_filesystem_storage_carries_its_own_keys(self) -> None:
        """Emit the filesystem block alone when that backend is chosen."""
        document = _document(
            {
                "storage_type": "filesystem",
                "storage_filesystem_path": "/var/lib/mongo/pbm-backups",
                "storage_filesystem_max_obj_size_gb": 42.0,
                # Every S3 field, cleared by prefix rather than by name. Naming them
                # missed the booleans and numbers, which the validator now rejects as
                # a cross-wired body -- correctly, since PBM would ignore them.
                **dict.fromkeys(
                    (key for key in FULL_FORM if key.startswith("storage_s3_")), None
                ),
            }
        )

        assert document["storage"] == {
            "type": "filesystem",
            "filesystem": {
                "path": "/var/lib/mongo/pbm-backups",
                "maxObjSizeGB": 42.0,
            },
        }


class TestUnsetFieldsStayOutOfTheDocument:
    """Guard the property the merge depends on.

    ``_apply_pbm_config`` lays this document over what the cluster already has, so
    a key present here overwrites. An unset form field must therefore be absent
    rather than ``None`` -- otherwise clearing a box in the UI would blank a value
    an operator had set with the CLI.
    """

    def test_only_supplied_keys_appear(self) -> None:
        """Emit nothing for the fields a minimal body leaves out."""
        form = BackupCreate.model_validate(
            {
                "task_name": "pbm-config",
                "hostname": "mongo-host",
                "service_id": 1,
                "backup_type": BackupType.PBM_CONFIG,
                "storage_type": "s3",
                "storage_s3_bucket": "backups",
                "storage_s3_region": "eu-west-1",
            }
        )
        document = yaml.safe_load(
            build_backup_mongo_spec(form, BackupMongoResolved()).data["meta"]["config"]
        )

        assert document["storage"]["s3"] == {
            "region": "eu-west-1",
            "bucket": "backups",
        }
        assert "pitr" not in document
        assert "backup" not in document
        assert "restore" not in document


class TestCredentialsAreNotSettable:
    """Hold the line the whole design rests on: OM never writes a secret."""

    def test_no_credential_field_exists_on_the_form(self) -> None:
        """Offer no access key, secret key, session token, or SSE customer key."""
        names = set(BackupConfigForm.model_fields)
        forbidden = {
            "storage_s3_access_key_id",
            "storage_s3_secret_access_key",
            "storage_s3_session_token",
            "storage_s3_sse_customer_key",
        }

        assert names & forbidden == set()

    def test_no_credentials_reach_the_document(self) -> None:
        """Write no ``credentials`` key, so the merge preserves what PBM stored."""
        assert "credentials" not in _document()["storage"]["s3"]

    def test_no_backend_offers_a_credentials_field(self) -> None:
        """Keep every backend's credentials out of the form, not just S3's.

        The rule is uniform rather than per-backend, which is what makes it one
        sentence and this one assertion: GCS's ``clientEmail``, Azure's
        ``credentials.key`` and OCI's principal are as absent as S3's access keys.
        ``credentials_path`` is unrelated -- it locates the MongoDB URI on the
        execution host and holds no secret itself.
        """
        offered = [
            name
            for name in BackupConfigForm.model_fields
            if "credentials" in name and name != "credentials_path"
        ]

        assert offered == []


def _for_backend(storage_type: str, **fields: object) -> dict:
    """Return a ``FULL_FORM`` override selecting ``storage_type`` alone.

    Every *other* backend's fields are cleared, by prefix rather than by name: the
    validator rejects fields belonging to a backend other than the selected one,
    which is the point of the prefix table. Clearing by name was how an earlier
    version of this helper missed the boolean and numeric S3 fields.
    """
    foreign = [
        backend.prefix
        for key, backend in _STORAGE_BACKENDS.items()
        if key != storage_type
    ]
    return {
        "storage_type": storage_type,
        **dict.fromkeys(
            (
                key
                for key in FULL_FORM
                if any(key.startswith(prefix) for prefix in foreign)
            ),
            None,
        ),
        **fields,
    }


class TestMinioBackend:
    """Cover PBM's native MinIO backend, which is not the S3 backend.

    Verified against the real binary: ``pbm profile add`` accepted exactly this key
    set against the sandbox MinIO.
    """

    def test_serialises_under_pbm_s_own_key_names(self) -> None:
        """Emit ``endpoint`` / ``endpointMap``, not S3's ``endpointUrl`` spellings."""
        document = _document(
            _for_backend(
                "minio",
                storage_minio_region="us-east-1",
                storage_minio_bucket="pbm",
                storage_minio_prefix="mongo",
                storage_minio_endpoint="https://minio.example.com",
                storage_minio_endpoint_map='"host1:27018": "http://minio-a:9000"',
                storage_minio_secure=True,
                storage_minio_force_path_style=True,
                storage_minio_insecure_skip_tls_verify=False,
                storage_minio_part_size=10485760,
                storage_minio_max_obj_size_gb=5018.0,
                storage_minio_debug_trace=True,
                storage_minio_retryer_num_max_retries=10,
            )
        )

        assert document["storage"] == {
            "type": "minio",
            "minio": {
                "region": "us-east-1",
                "bucket": "pbm",
                "prefix": "mongo",
                "endpoint": "https://minio.example.com",
                "endpointMap": {"host1:27018": "http://minio-a:9000"},
                "secure": True,
                "forcePathStyle": True,
                "insecureSkipTLSVerify": False,
                "partSize": 10485760,
                "maxObjSizeGB": 5018.0,
                "debugTrace": True,
                "retryer": {"numMaxRetries": 10},
            },
        }


class TestGcsBackend:
    """Cover Google Cloud Storage, whose retryer backs off rather than delaying.

    Verified against the real binary: PBM parsed exactly this key set and failed
    only on the credentials it will not take from OM, which is how the key names
    were confirmed without a GCS account.
    """

    def test_serialises_bucket_and_backoff_retryer(self) -> None:
        """Emit the GCS block with its own retryer shape."""
        document = _document(
            _for_backend(
                "gcs",
                storage_gcs_bucket="pbm",
                storage_gcs_prefix="mongo",
                storage_gcs_chunk_size=10485760,
                storage_gcs_max_obj_size_gb=5018.0,
                storage_gcs_retryer_backoff_initial="1s",
                storage_gcs_retryer_backoff_max="30s",
                storage_gcs_retryer_backoff_multiplier=2,
                storage_gcs_retryer_max_attempts=5,
                storage_gcs_retryer_chunk_retry_deadline="32s",
            )
        )

        assert document["storage"] == {
            "type": "gcs",
            "gcs": {
                "bucket": "pbm",
                "prefix": "mongo",
                "chunkSize": 10485760,
                "maxObjSizeGB": 5018.0,
                "retryer": {
                    "backoffInitial": "1s",
                    "backoffMax": "30s",
                    "backoffMultiplier": 2,
                    "maxAttempts": 5,
                    "chunkRetryDeadline": "32s",
                },
            },
        }


class TestStorageBackendRegistry:
    """Guard the invariants that let one table drive validation and serialisation."""

    def test_no_prefix_is_a_prefix_of_another(self) -> None:
        """Keep every field attributable to exactly one backend.

        Fields are assigned by prefix alone. If one prefix were a prefix of another,
        a field would belong to two backends at once and the "no foreign field is
        set" check would start rejecting valid bodies.
        """
        prefixes = [backend.prefix for backend in _STORAGE_BACKENDS.values()]

        for prefix in prefixes:
            assert not [
                other
                for other in prefixes
                if other != prefix and other.startswith(prefix)
            ]

    def test_every_registered_backend_can_be_serialised(self) -> None:
        """Pair each validated backend with a builder.

        A backend the validator accepts but no builder covers would pass create and
        then vanish from the document -- the silent-no-op failure this panel exists
        to remove.
        """
        assert set(_STORAGE_BUILDERS) == set(_STORAGE_BACKENDS)

    def test_the_type_selector_offers_exactly_the_registered_backends(self) -> None:
        """Keep the form's choices and the registry from drifting apart.

        A backend registered but not offered is unreachable; one offered but not
        registered is a 422 on submit. Asserted against the derived schema, which is
        what the UI actually renders from.
        """
        storage = next(
            section
            for section in backup_mongo_config_schema.forms
            if section.title == "Storage"
        )
        field = next(f for f in storage.fields if f.name == "storage_type")

        assert {choice.value for choice in field.choices} == set(_STORAGE_BACKENDS)


class TestAzureBackend:
    """Cover Azure Blob Storage.

    Key names verified against the real binary: PBM parsed this document and got as
    far as an authentication failure, which only happens past the decoder.
    """

    def test_serialises_account_container_and_retryer(self) -> None:
        """Emit the Azure block, locators and retry settings included."""
        document = _document(
            _for_backend(
                "azure",
                storage_azure_account="acct",
                storage_azure_container="pbm",
                storage_azure_prefix="mongo",
                storage_azure_endpoint_url="https://acct.blob.example.test",
                storage_azure_endpoint_url_map='"host1:27018": "https://a.example.test"',
                storage_azure_max_obj_size_gb=194560.0,
                storage_azure_retryer_num_max_retries=3,
                storage_azure_retryer_min_retry_delay="800ms",
                storage_azure_retryer_max_retry_delay="60s",
            )
        )

        assert document["storage"] == {
            "type": "azure",
            "azure": {
                "account": "acct",
                "container": "pbm",
                "prefix": "mongo",
                "endpointUrl": "https://acct.blob.example.test",
                "endpointUrlMap": {"host1:27018": "https://a.example.test"},
                "maxObjSizeGB": 194560.0,
                "retryer": {
                    "numMaxRetries": 3,
                    "minRetryDelay": "800ms",
                    "maxRetryDelay": "60s",
                },
            },
        }


class TestOciBackend:
    """Cover OCI Object Storage.

    Key names verified against the real binary: PBM parsed this document and failed
    on ``credentials.userPrincipal is required``, which is the half OM never sends.
    """

    def test_serialises_locators_and_kms_key(self) -> None:
        """Emit the OCI block with its namespace and KMS key reference."""
        document = _document(
            _for_backend(
                "oci",
                storage_oci_region="eu-frankfurt-1",
                storage_oci_namespace="ns",
                storage_oci_bucket="pbm",
                storage_oci_prefix="mongo",
                storage_oci_sse_kms_key_id="ocid1.key.oc1..example",
            )
        )

        assert document["storage"] == {
            "type": "oci",
            "oci": {
                "region": "eu-frankfurt-1",
                "namespace": "ns",
                "bucket": "pbm",
                "prefix": "mongo",
                "serverSideEncryption": {"kmsKeyID": "ocid1.key.oc1..example"},
            },
        }


class TestAlibabaOssIsNotOffered:
    """Pin the one backend deliberately left out.

    PBM accepts ``oss``, but its published key reference does not match the 2.15.0
    binary -- the documented ``serverSideEncryption`` names are all rejected. Rather
    than ship fields that look settable and reach nothing, the backend is absent
    from the registry, which makes it unselectable and rejected at create time.
    """

    def test_oss_is_not_registered(self) -> None:
        """Keep ``oss`` out of the registry until its keys are verified."""
        assert "oss" not in _STORAGE_BACKENDS

    def test_oss_is_rejected_at_create_time(self) -> None:
        """Reject an ``oss`` body rather than accepting and silently dropping it."""
        with pytest.raises(ValidationError, match="storage_type"):
            BackupCreate.model_validate(
                {
                    "task_name": "pbm-config",
                    "hostname": "mongo-host",
                    "service_id": 1,
                    "backup_type": BackupType.PBM_CONFIG,
                    "storage_type": "oss",
                }
            )
