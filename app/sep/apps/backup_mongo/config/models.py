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
    storage_filesystem_path: Annotated[
        str, _NOT_FILESYSTEM_STORAGE, Ui(label="Filesystem Path", section="Storage")
    ]
    pitr_enabled: Annotated[bool, Ui(label="Enable PITR", section="PITR")] = False
    pitr_oplog_span_min: Annotated[
        int | None, Ui(label="Oplog Span (minutes)", section="PITR")
    ] = None
    pitr_compression: Annotated[
        str, _COMPRESSION_CHOICES, Ui(label="PITR Compression", section="PITR")
    ] = CompressionAlgorithm.GZIP.value
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
