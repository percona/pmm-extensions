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

"""Unit tests for the pure backup_mongo spec builder (no API mocks)."""

import yaml

from app.extensions.apps.backup_mongo.models import (
    BackupCreate,
    BackupType,
    CompressionAlgorithm,
)
from app.extensions.apps.backup_mongo.spec import (
    BackupMongoResolved,
    build_backup_mongo_spec,
)
from app.tasks.models import TaskBackendEnum, TaskWrite

PARALLEL_COLLECTIONS = 4
COMPRESSION_LEVEL = 6


def _config(task: TaskWrite) -> dict:
    """Parse the YAML PBM config embedded in the task's meta."""
    return yaml.safe_load(task.data["meta"]["config"])


def _s3_form(**overrides: object) -> BackupCreate:
    """Build an S3-storage BackupCreate with the given field overrides."""
    pitr_compression = overrides.pop("pitr_compression", "gzip")
    return BackupCreate(
        task_name="mongo-backup",
        hostname="mongo-host",
        service_id=1,
        backup_type=BackupType.PBM_CONFIG,
        pitr_compression=pitr_compression,
        storage_type="s3",
        storage_s3_region="eu-west-1",
        storage_s3_bucket="backups",
        storage_s3_prefix="mongo",
        storage_s3_endpoint_url="https://s3.example.com",
        **overrides,
    )


def test_build_spec_envelope_keeps_run_python_shape(backup_create: BackupCreate):
    """Keep the run-python envelope shape with backup_type at the data top level."""
    task = build_backup_mongo_spec(backup_create, BackupMongoResolved())

    assert isinstance(task, TaskWrite)
    assert task.owner == "BACKUP_MONGO"
    assert task.backend == TaskBackendEnum.PROXY
    assert task.name == backup_create.task_name
    assert task.data["task"] == "run-python"
    assert task.data["backup_type"] == BackupType.PBM_CONFIG
    assert task.data["payload"].endswith("/pbm_config_payload")
    assert task.data["meta"]["target"] == backup_create.hostname


def test_build_spec_omits_service_name_when_unresolved(backup_create: BackupCreate):
    """Omit _service_name from the meta when no service resolved."""
    task = build_backup_mongo_spec(backup_create, BackupMongoResolved())

    assert "_service_name" not in task.data["meta"]
    assert list(task.data["meta"].keys()) == ["config", "target", "requirements"]


def test_build_spec_stamps_service_name_when_resolved(backup_create: BackupCreate):
    """Add a resolved service name to the meta as _service_name."""
    task = build_backup_mongo_spec(
        backup_create, BackupMongoResolved(service_name="mongo-svc")
    )

    assert task.data["meta"]["_service_name"] == "mongo-svc"
    assert list(task.data["meta"].keys()) == [
        "config",
        "target",
        "requirements",
        "_service_name",
    ]


def test_build_spec_filesystem_storage(backup_create: BackupCreate):
    """Serialize the filesystem path and omit the s3 block for filesystem storage."""
    config = _config(build_backup_mongo_spec(backup_create, BackupMongoResolved()))

    assert config["storage"]["type"] == "filesystem"
    assert config["storage"]["filesystem"]["path"] == "/var/backups/mongo"
    assert "s3" not in config["storage"]


def test_build_spec_s3_storage():
    """Serialize region/bucket/prefix/endpoint and omit the filesystem block for s3 storage."""
    config = _config(build_backup_mongo_spec(_s3_form(), BackupMongoResolved()))

    assert config["storage"]["type"] == "s3"
    assert config["storage"]["s3"]["region"] == "eu-west-1"
    assert config["storage"]["s3"]["bucket"] == "backups"
    assert config["storage"]["s3"]["prefix"] == "mongo"
    assert config["storage"]["s3"]["endpointUrl"] == "https://s3.example.com"
    assert "filesystem" not in config["storage"]


def test_build_spec_defaults_pitr_compression_when_omitted():
    """Default PITR compression to gzip when pitr_compression is omitted.

    ``pitr_enabled`` is set explicitly because the section is only emitted when
    the body says something about PITR -- see the two tests below.
    """
    config = _config(
        build_backup_mongo_spec(
            _s3_form(pitr_enabled=True, pitr_compression=None),
            BackupMongoResolved(),
        )
    )

    assert config["pitr"]["compression"] == CompressionAlgorithm.GZIP.value


def test_build_spec_omits_pitr_when_the_body_says_nothing_about_it():
    """Write no ``pitr`` section for a body that never mentions PITR.

    A backup is such a body now that configuration is a separate form. Since the
    apply merges rather than replaces, emitting ``enabled: false`` here would not
    be inert -- it would switch point-in-time recovery off on a cluster where an
    operator had turned it on. ``pitr_compression`` is cleared because the shared
    fixture sets it, and setting it is itself saying something about PITR.
    """
    config = _config(
        build_backup_mongo_spec(_s3_form(pitr_compression=None), BackupMongoResolved())
    )

    assert "pitr" not in config


def test_build_spec_writes_pitr_disabled_when_explicitly_cleared():
    """Write ``enabled: false`` when an operator actually cleared the box.

    The counterpart to the test above: the Configuration tab sends ``False``, which
    has to reach the cluster, so the tri-state cannot collapse to a bare bool.
    """
    config = _config(
        build_backup_mongo_spec(_s3_form(pitr_enabled=False), BackupMongoResolved())
    )

    assert config["pitr"]["enabled"] is False


def test_build_spec_omits_backup_block_when_no_options():
    """Omit the backup block from the config when no backup-config fields are set."""
    config = _config(build_backup_mongo_spec(_s3_form(), BackupMongoResolved()))

    assert "backup" not in config


def test_build_spec_includes_backup_block_when_options_set():
    """Populate the config backup block under camelCase keys when backup options are set."""
    form = _s3_form(
        backup_compression="zstd",
        backup_num_parallel_collections=PARALLEL_COLLECTIONS,
    )

    config = _config(build_backup_mongo_spec(form, BackupMongoResolved()))

    assert config["backup"]["compression"] == "zstd"
    assert config["backup"]["numParallelCollections"] == PARALLEL_COLLECTIONS


def test_build_spec_forwards_parsed_priority():
    """Forward a valid Node Priority mapping to the PBM config as node -> float."""
    form = _s3_form(backup_priority='"h1:27018": 2\n"h2:27018": 1')

    config = _config(build_backup_mongo_spec(form, BackupMongoResolved()))

    assert config["backup"]["priority"] == {"h1:27018": 2.0, "h2:27018": 1.0}


def test_build_spec_forwards_backup_compression_level():
    """Forward the selected backup compression level under the camelCase config key."""
    form = _s3_form(
        backup_compression="gzip", backup_compression_level=COMPRESSION_LEVEL
    )

    config = _config(build_backup_mongo_spec(form, BackupMongoResolved()))

    assert config["backup"]["compression"] == "gzip"
    assert config["backup"]["compressionLevel"] == COMPRESSION_LEVEL


def test_build_spec_forwards_selected_pitr_compression():
    """Forward a selected PITR compression unchanged (config-only PITR path)."""
    config = _config(
        build_backup_mongo_spec(
            _s3_form(pitr_compression="snappy"),
            BackupMongoResolved(),
        )
    )

    assert config["pitr"]["compression"] == "snappy"


def test_build_spec_forwards_selective_namespaces():
    """Thread selective namespaces and users/roles into the backup config block."""
    form = _s3_form(
        backup_namespaces="mydb.*,other.*",
        backup_with_users_and_roles=True,
    )

    config = _config(build_backup_mongo_spec(form, BackupMongoResolved()))

    assert config["backup"]["namespaces"] == "mydb.*,other.*"
    assert config["backup"]["withUsersAndRoles"] is True
