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
from typing import Annotated, Any, NamedTuple, Self

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.core.models import BaseCaseInsensitiveModel
from app.core.utils.fields import EmptyStrToNone, NonEmptyStr
from app.extensions.apps.backup_mongo.models import BackupType
from app.extensions.apps.framework import BaseTaskResponse
from app.extensions.apps.framework.form_dsl import (
    Choices,
    FieldWidget,
    ServiceRef,
    TaskFormModel,
    Ui,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import (
    TaskHistoryStatusEnum,
    TaskWrite,
)

logger = logging.getLogger(__name__)

_NAMESPACE_FILTER_PHYSICAL_ERROR = (
    "Namespace Filter is only supported for logical MongoDB restores"
)


def _ensure_namespace_filter_allowed_for_backup_type(
    backup_type: BackupType,
    restore_namespace_filter: str | None,
) -> None:
    """Raise when a physical restore carries a namespace filter.

    Shared by :class:`RestoreCreate` and :class:`RestoreTaskWrite` so the
    predicate and message cannot drift between the form and JSON body paths.

    :param backup_type: The restore's backup type.
    :param restore_namespace_filter: The optional namespace filter value.
    :raises ValueError: If a physical restore includes a namespace filter.
    """
    if backup_type == BackupType.PBM_PHYSICAL and restore_namespace_filter:
        raise ValueError(_NAMESPACE_FILTER_PHYSICAL_ERROR)


def parse_mongod_location_map(location_map_str: str) -> dict[str, Any] | None:
    """Parse mongod location map from YAML string.

    Expects a YAML object (mapping). Returns None and logs a warning if the input
    is invalid YAML or does not parse to a dictionary.
    """
    try:
        mongod_location_map = yaml.safe_load(location_map_str)
    except yaml.YAMLError:
        logger.warning("Failed to parse mongod location map YAML: %s", location_map_str)
        return None
    if mongod_location_map is None:
        return None
    if isinstance(mongod_location_map, dict):
        return mongod_location_map
    logger.warning(
        "Mongod location map must be a dictionary/mapping, got: %s",
        type(mongod_location_map),
    )
    return None


OWNER = "RESTORE_MONGO"


class RestoreConfigRestore(BaseCaseInsensitiveModel):
    """Represent restore configuration options.

    This model uses camelCase serialization aliases to match PBM config format,
    but still accepts uppercase keys for case-insensitive input.

    :param batchSize: The number of documents to buffer.
    :type batchSize: int | None
    :param numInsertionWorkers: Specifies the number of insertion workers to run concurrently per collection.
    :type numInsertionWorkers: int | None
    :param numParallelCollections: The number of collections to process in parallel during a logical restore.
    :type numParallelCollections: int | None
    :param numDownloadWorkers: The number of workers that request data chunks from the storage during the restore.
    :type numDownloadWorkers: int | None
    :param maxDownloadBufferMb: The maximum size of the in-memory buffer that is used to download files from the S3 storage.
    :type maxDownloadBufferMb: int | None
    :param downloadChunkMb: The size of the data chunk in MB to download from the S3 storage.
    :type downloadChunkMb: int | None
    :param mongodLocation: The custom path to mongod binaries.
    :type mongodLocation: NonEmptyStr | EmptyStrToNone
    :param mongodLocationMap: The list of custom paths to mongod binaries on every node.
    :type mongodLocationMap: dict[str, str] | EmptyStrToNone
    """

    model_config = ConfigDict(alias_generator=None)

    batch_size: int | None = Field(
        default=500,
        validation_alias=AliasChoices("batchSize", "BATCHSIZE", "BATCH_SIZE"),
        serialization_alias="batchSize",
    )
    num_insertion_workers: int | None = Field(
        default=10,
        validation_alias=AliasChoices(
            "numInsertionWorkers", "NUMINSERTIONWORKERS", "NUM_INSERTION_WORKERS"
        ),
        serialization_alias="numInsertionWorkers",
    )
    num_parallel_collections: int | None = Field(
        None,
        validation_alias=AliasChoices(
            "numParallelCollections",
            "NUMPARALLELCOLLECTIONS",
            "NUM_PARALLEL_COLLECTIONS",
        ),
        serialization_alias="numParallelCollections",
    )
    num_download_workers: int | None = Field(
        None,
        validation_alias=AliasChoices(
            "numDownloadWorkers", "NUMDOWNLOADWORKERS", "NUM_DOWNLOAD_WORKERS"
        ),
        serialization_alias="numDownloadWorkers",
    )
    max_download_buffer_mb: int | None = Field(
        None,
        validation_alias=AliasChoices(
            "maxDownloadBufferMb", "MAXDOWNLOADBUFFERMB", "MAX_DOWNLOAD_BUFFER_MB"
        ),
        serialization_alias="maxDownloadBufferMb",
    )
    download_chunk_mb: int | None = Field(
        default=32,
        validation_alias=AliasChoices(
            "downloadChunkMb", "DOWNLOADCHUNKMB", "DOWNLOAD_CHUNK_MB"
        ),
        serialization_alias="downloadChunkMb",
    )
    mongod_location: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices(
            "mongodLocation", "MONGODLOCATION", "MONGOD_LOCATION"
        ),
        serialization_alias="mongodLocation",
    )
    mongod_location_map: dict[str, str] | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices(
            "mongodLocationMap", "MONGODLOCATIONMAP", "MONGOD_LOCATION_MAP"
        ),
        serialization_alias="mongodLocationMap",
    )


class RestoreConfig(BaseCaseInsensitiveModel):
    """Define the complete configuration for a restore operation in PBM format.

    This model follows the PBM backup format with lowercase keys and camelCase values,
    similar to how PBM backup config is structured. Only contains PBM config sections.
    The backupSource and backupType are stored separately for restore operations.

    :param restore: Restore-specific configuration options (PBM config format).
    :type restore: RestoreConfigRestore | None
    :param backupSource: Source location of the backup (backup name or timestamp).
        This is not part of PBM config but needed for restore operations.
    :type backupSource: NonEmptyStr
    :param backupType: Type of backup to restore from.
        This is not part of PBM config but needed for restore operations.
    :type backupType: BackupType
    :param namespace: Optional namespace filter for selective logical restores.
    :type namespace: NonEmptyStr | EmptyStrToNone
    :param credentials_path: Path to MongoDB URI credentials file on the Nomad node
        (PMM Extensions only, not part of PBM config; used by payloads).
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    """

    model_config = ConfigDict(alias_generator=None)

    restore: RestoreConfigRestore | EmptyStrToNone = Field(
        None, validation_alias=AliasChoices("restore", "RESTORE")
    )
    backup_source: NonEmptyStr = Field(
        validation_alias=AliasChoices("backupSource", "BACKUP_SOURCE", "backup_source"),
        serialization_alias="backupSource",
    )
    backup_type: BackupType = Field(
        validation_alias=AliasChoices("backupType", "BACKUP_TYPE", "backup_type"),
        serialization_alias="backupType",
    )
    namespace: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("namespace", "NAMESPACE"),
        serialization_alias="namespace",
    )
    credentials_path: NonEmptyStr | EmptyStrToNone = Field(
        None,
        validation_alias=AliasChoices("credentials_path", "CREDENTIALS_PATH"),
    )


class RestoreCreate(BaseCaseInsensitiveModel):
    """Represent a restore-task creation request (HTML form and shared payload input).

    Kept alongside :class:`RestoreTaskWrite` because FastAPI form binding needs
    ``service_id: NonEmptyStr | EmptyStrToNone`` (empty form field → ``None``) and
    optional restore ints with ``EmptyStrToNone``, while the JSON API uses
    ``service_id: int | None`` and plain optional ints. Collapsing to one model would
    break empty-string form semantics unless a dedicated form adapter is proven.

    :param hostname: The hostname of the machine to restore to.
    :type hostname: NonEmptyStr
    :param task_name: Name of the restore task.
    :type task_name: NonEmptyStr
    :param service_id: Service identifier for the restore task.
    :type service_id: NonEmptyStr | EmptyStrToNone = None
    :param backup_type: Type of backup to restore from.
    :type backup_type: BackupType
    :param backup_source: Source location of the backup (backup name or timestamp).
    :type backup_source: NonEmptyStr
    :param restore_namespace_filter: Optional database or collection namespace
        filter for a logical restore.
    :type restore_namespace_filter: NonEmptyStr | EmptyStrToNone
    :param restore_batch_size: Number of documents to buffer.
    :type restore_batch_size: int | None
    :param restore_num_insertion_workers: Number of insertion workers to run concurrently per collection.
    :type restore_num_insertion_workers: int | None
    :param restore_num_parallel_collections: Number of collections to process in parallel during logical restore.
    :type restore_num_parallel_collections: int | None
    :param restore_num_download_workers: Number of workers that request data chunks from storage.
    :type restore_num_download_workers: int | None
    :param restore_max_download_buffer_mb: Maximum size of in-memory buffer for downloading files from S3.
    :type restore_max_download_buffer_mb: int | None
    :param restore_download_chunk_mb: Size of data chunk in MB to download from S3 storage.
    :type restore_download_chunk_mb: int | None
    :param restore_mongod_location: Custom path to mongod binaries.
    :type restore_mongod_location: NonEmptyStr | EmptyStrToNone
    :param restore_mongod_location_map: Custom paths to mongod binaries on every node (YAML string).
    :type restore_mongod_location_map: NonEmptyStr | EmptyStrToNone
    :param credentials_path: Optional path to MongoDB URI credentials file on the Nomad node.
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    """

    hostname: NonEmptyStr
    task_name: NonEmptyStr
    service_id: NonEmptyStr | EmptyStrToNone = None
    backup_type: BackupType
    backup_source: NonEmptyStr
    restore_namespace_filter: NonEmptyStr | EmptyStrToNone = None
    restore_batch_size: int | EmptyStrToNone = None
    restore_num_insertion_workers: int | EmptyStrToNone = None
    restore_num_parallel_collections: int | EmptyStrToNone = None
    restore_num_download_workers: int | EmptyStrToNone = None
    restore_max_download_buffer_mb: int | EmptyStrToNone = None
    restore_download_chunk_mb: int | EmptyStrToNone = None
    restore_mongod_location: NonEmptyStr | EmptyStrToNone = None
    restore_mongod_location_map: NonEmptyStr | EmptyStrToNone = None
    credentials_path: NonEmptyStr | EmptyStrToNone = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_int_service_id(cls, data: Any) -> Any:
        """Coerce an int ``service_id`` (the JSON body shape) to str.

        The form path receives ``service_id`` as ``NonEmptyStr |
        EmptyStrToNone`` directly. The JSON path validates a
        :class:`RestoreTaskWrite` first, where ``service_id`` is
        ``int | None``, and then dumps it for re-validation here —
        without this coercion the ``int`` would fail the str-typed
        field. Form submissions arriving as strings are unaffected.

        :param data: The raw input passed to ``model_validate``.
        :type data: Any
        :return: The input with ``service_id`` stringified when it was
            an int, or ``data`` unchanged otherwise.
        :rtype: Any
        """
        if isinstance(data, dict):
            sid = data.get("service_id")
            if isinstance(sid, int):
                return {**data, "service_id": str(sid)}
        return data

    @model_validator(mode="after")
    def _reject_namespace_filter_for_physical_restore(self) -> Self:
        """Reject selective namespace restores for physical backups.

        :return: The validated restore request.
        :raises ValueError: If a physical restore includes a namespace filter.
        """
        _ensure_namespace_filter_allowed_for_backup_type(
            self.backup_type, self.restore_namespace_filter
        )
        return self


def restore_config_restore_from_form(
    form: RestoreCreate,
) -> RestoreConfigRestore | None:
    """Build :class:`RestoreConfigRestore` from flat restore form fields."""
    field_mapping = {
        "batchSize": form.restore_batch_size,
        "numInsertionWorkers": form.restore_num_insertion_workers,
        "numParallelCollections": form.restore_num_parallel_collections,
        "numDownloadWorkers": form.restore_num_download_workers,
        "maxDownloadBufferMb": form.restore_max_download_buffer_mb,
        "downloadChunkMb": form.restore_download_chunk_mb,
    }
    restore_config_dict = {
        key: value for key, value in field_mapping.items() if value is not None
    }
    if form.restore_mongod_location:
        restore_config_dict["mongodLocation"] = form.restore_mongod_location
    if form.restore_mongod_location_map:
        location_map = parse_mongod_location_map(form.restore_mongod_location_map)
        if location_map is not None:
            restore_config_dict["mongodLocationMap"] = location_map
    if not restore_config_dict:
        return None
    return RestoreConfigRestore.model_validate(restore_config_dict)


class RestoreForm(TaskFormModel):
    """Define the model-first schema source for the MongoDB Restores ``GET /schema``.

    The single source the derived ``GET /schema`` form renders from, driven by the
    :class:`Ui` / reference / :class:`Choices` markers. It is *not* the JSON request
    body — :class:`RestoreTaskWrite` is — and is never validated as one;
    field-declaration order reproduces the schema's section and field order (Task,
    Restore Options). ``task_name`` is redeclared so the form can carry a
    presentation default via ``Ui(default=...)``; ``hostname`` and the
    ``alert_on_fail`` capability control stay inherited from
    :class:`TaskFormModel` (``alert_on_fail`` is ``Hidden``, off-schema). This form
    is never validated as a request body.
    """

    task_name: Annotated[
        NonEmptyStr,
        Ui(section="Task", default="mongodb-restore"),
    ]
    service_id: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MONGODB,)),
        Ui(label="Database Service", section="Task"),
    ] = None
    backup_type: Annotated[
        str,
        Choices(
            (
                (BackupType.PBM_LOGICAL.value, "Logical"),
                (BackupType.PBM_PHYSICAL.value, "Physical"),
            )
        ),
        Ui(section="Task"),
    ]
    backup_source: Annotated[
        str,
        Ui(
            section="Task",
            description="Backup name or timestamp (e.g. 2025-12-15T19:04:05Z)",
        ),
    ]
    credentials_path: Annotated[
        str | None,
        Ui(
            section="Task",
            description="Optional path to MongoDB URI credentials on the Nomad node",
        ),
    ] = None
    restore_namespace_filter: Annotated[
        str | None,
        Ui(
            label="Namespace Filter",
            section="RestoreOptions",
            description=(
                "Optional database or collection patterns to restore, separated by "
                "commas (for example, db1.*,db2.collection)"
            ),
        ),
    ] = None
    restore_batch_size: Annotated[
        int | None, Ui(label="Batch Size", section="RestoreOptions")
    ] = None
    restore_num_insertion_workers: Annotated[
        int | None, Ui(label="Insertion Workers", section="RestoreOptions")
    ] = None
    restore_num_parallel_collections: Annotated[
        int | None, Ui(label="Parallel Collections", section="RestoreOptions")
    ] = None
    restore_num_download_workers: Annotated[
        int | None, Ui(label="Download Workers", section="RestoreOptions")
    ] = None
    restore_max_download_buffer_mb: Annotated[
        int | None, Ui(label="Max Download Buffer (MB)", section="RestoreOptions")
    ] = None
    restore_download_chunk_mb: Annotated[
        float | None, Ui(label="Download Chunk Size (MB)", section="RestoreOptions")
    ] = None
    restore_mongod_location: Annotated[
        str | None, Ui(label="Mongod Location", section="RestoreOptions")
    ] = None
    restore_mongod_location_map: Annotated[
        str | None,
        Ui(
            label="Mongod Location Map (YAML)",
            section="RestoreOptions",
            widget=FieldWidget.TEXTAREA,
        ),
    ] = None


class RestoreTaskWrite(BaseModel):
    """Represent a JSON request body for creating a restore task group.

    Mirrors :class:`RestoreCreate` field-for-field with JSON-native types (notably
    ``service_id: int | None``). Routes convert via
    :func:`~app.extensions.apps.backup_mongo.restore.deps.restore_create_from_write`
    before building task payloads. See :class:`RestoreCreate` for why both models
    remain.

    POST always creates a parent config task plus restore, pbm-list, and optional
    force-resync children.

    :param task_name: The name of the task to be created.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for the task execution.
    :type hostname: NonEmptyStr
    :param service_id: Optional Inventory ID of the MongoDB service.
    :type service_id: int | None
    :param backup_type: Type of backup to restore from (logical or physical).
    :type backup_type: BackupType
    :param backup_source: Backup name or timestamp to restore from.
    :type backup_source: NonEmptyStr
    :param restore_namespace_filter: Optional database or collection namespace
        filter for a logical restore.
    :type restore_namespace_filter: str | None
    :param restore_batch_size: Number of documents to buffer.
    :type restore_batch_size: int | None
    :param restore_num_insertion_workers: Insertion workers per collection.
    :type restore_num_insertion_workers: int | None
    :param restore_num_parallel_collections: Parallel collections for logical restore.
    :type restore_num_parallel_collections: int | None
    :param restore_num_download_workers: Download workers for physical restore.
    :type restore_num_download_workers: int | None
    :param restore_max_download_buffer_mb: Max S3 download buffer in MB.
    :type restore_max_download_buffer_mb: int | None
    :param restore_download_chunk_mb: S3 download chunk size in MB.
    :type restore_download_chunk_mb: int | None
    :param restore_mongod_location: Custom path to mongod binaries.
    :type restore_mongod_location: str | None
    :param restore_mongod_location_map: Per-node mongod paths as YAML.
    :type restore_mongod_location_map: str | None
    :param credentials_path: Path to MongoDB URI credentials on the Nomad node.
    :type credentials_path: str | None

    A key the model does not declare is rejected with ``422`` rather than
    dropped, as on every task create and update body.
    """

    model_config = ConfigDict(extra="forbid")

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    service_id: int | None = None
    backup_type: BackupType
    backup_source: NonEmptyStr
    restore_namespace_filter: str | None = None
    restore_batch_size: int | None = None
    restore_num_insertion_workers: int | None = None
    restore_num_parallel_collections: int | None = None
    restore_num_download_workers: int | None = None
    restore_max_download_buffer_mb: int | None = None
    restore_download_chunk_mb: int | None = None
    restore_mongod_location: str | None = None
    restore_mongod_location_map: str | None = None
    credentials_path: str | None = None

    @model_validator(mode="after")
    def _reject_namespace_filter_for_physical_restore(self) -> Self:
        """Reject selective namespace restores for physical JSON requests.

        :return: The validated restore request.
        :raises ValueError: If a physical restore includes a namespace filter.
        """
        _ensure_namespace_filter_allowed_for_backup_type(
            self.backup_type, self.restore_namespace_filter
        )
        return self


class RestoreTaskLegModel(BaseModel):
    """Represent an internal descriptor for one restore task leg.

    :param name: The task name for this leg.
    :type name: NonEmptyStr
    :param payload_name: The payload file name consumed by ``run-python``.
    :type payload_name: NonEmptyStr
    :param target: The target host where this task executes.
    :type target: NonEmptyStr
    :param config_yaml: YAML config content passed in task metadata.
    :type config_yaml: str
    :param requirements: Newline-separated Python requirements for this leg.
    :type requirements: str
    :param parent: Optional parent task name for child legs.
    :type parent: NonEmptyStr | None
    :param service_name: Optional PMM service name annotation.
    :type service_name: NonEmptyStr | None
    """

    name: NonEmptyStr
    payload_name: NonEmptyStr
    target: NonEmptyStr
    config_yaml: str
    requirements: str = ""
    parent: NonEmptyStr | None = None
    service_name: NonEmptyStr | None = None


class RestoreConfigPayloadModel(BaseModel):
    """Represent typed inputs for the restore-config leg payload.

    :param task_name: The parent restore task name.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for execution.
    :type hostname: NonEmptyStr
    :param backup_source: Backup name or timestamp to restore from.
    :type backup_source: NonEmptyStr
    :param backup_type: Backup type for restore execution.
    :type backup_type: BackupType
    :param restore: Optional restore-specific PBM options.
    :type restore: RestoreConfigRestore | None
    :param credentials_path: Optional path to MongoDB URI credentials.
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    :param service_name: Optional PMM service name annotation.
    :type service_name: NonEmptyStr | None
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    backup_source: NonEmptyStr
    backup_type: BackupType
    restore: RestoreConfigRestore | None = None
    credentials_path: NonEmptyStr | EmptyStrToNone = None
    service_name: NonEmptyStr | None = None

    @classmethod
    def from_form(
        cls,
        form: RestoreCreate,
        service_name: str | None,
    ) -> Self:
        """Build restore-config leg inputs from a :class:`RestoreCreate` form."""
        return cls(
            task_name=form.task_name,
            hostname=form.hostname,
            backup_source=form.backup_source,
            backup_type=form.backup_type,
            restore=restore_config_restore_from_form(form),
            credentials_path=form.credentials_path or None,
            service_name=service_name,
        )


class RestoreLegPayloadModel(BaseModel):
    """Represent typed inputs for the restore execution leg payload.

    :param task_name: The parent restore task name.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for execution.
    :type hostname: NonEmptyStr
    :param backup_source: Backup name or timestamp to restore from.
    :type backup_source: NonEmptyStr
    :param backup_type: Backup type for restore execution.
    :type backup_type: BackupType
    :param namespace: Optional namespace filter for selective logical restores.
    :type namespace: NonEmptyStr | EmptyStrToNone
    :param credentials_path: Optional path to MongoDB URI credentials.
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    :param service_name: Optional PMM service name annotation.
    :type service_name: NonEmptyStr | None
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    backup_source: NonEmptyStr
    backup_type: BackupType
    namespace: NonEmptyStr | EmptyStrToNone = None
    credentials_path: NonEmptyStr | EmptyStrToNone = None
    service_name: NonEmptyStr | None = None

    @classmethod
    def from_form(
        cls,
        form: RestoreCreate,
        service_name: str | None,
    ) -> Self:
        """Build restore execution leg inputs from a :class:`RestoreCreate` form."""
        return cls(
            task_name=form.task_name,
            hostname=form.hostname,
            backup_source=form.backup_source,
            backup_type=form.backup_type,
            namespace=form.restore_namespace_filter or None,
            credentials_path=form.credentials_path or None,
            service_name=service_name,
        )

    def payload_script_name(self) -> str:
        """Return the Nomad payload script file for this leg's backup type.

        :return: Payload script basename (without path) for ``run-python``.
        :rtype: str
        :raises ValueError: If ``backup_type`` is not logical or physical PBM.
        """
        backup_type_to_payload = {
            BackupType.PBM_LOGICAL: "pbm_logical_restore_payload",
            BackupType.PBM_PHYSICAL: "pbm_physical_restore_payload",
        }
        payload_name = backup_type_to_payload.get(self.backup_type)
        if payload_name is None:
            raise ValueError(f"Invalid Backup Type {self.backup_type} for restore")
        return payload_name


class PbmListPayloadModel(BaseModel):
    """Represent typed inputs for the pbm-list helper leg payload.

    :param task_name: The parent restore task name.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for execution.
    :type hostname: NonEmptyStr
    :param credentials_path: Optional path to MongoDB URI credentials.
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    :param service_name: Optional PMM service name annotation.
    :type service_name: NonEmptyStr | None
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    credentials_path: NonEmptyStr | EmptyStrToNone = None
    service_name: NonEmptyStr | None = None

    @classmethod
    def from_form(
        cls,
        form: RestoreCreate,
        service_name: str | None,
    ) -> Self:
        """Build pbm-list leg inputs from a :class:`RestoreCreate` form."""
        return cls(
            task_name=form.task_name,
            hostname=form.hostname,
            credentials_path=form.credentials_path or None,
            service_name=service_name,
        )


class PbmForceResyncPayloadModel(BaseModel):
    """Represent typed inputs for the pbm-force-resync helper leg payload.

    :param task_name: The parent restore task name.
    :type task_name: NonEmptyStr
    :param hostname: The target hostname for execution.
    :type hostname: NonEmptyStr
    :param credentials_path: Optional path to MongoDB URI credentials.
    :type credentials_path: NonEmptyStr | EmptyStrToNone
    :param service_name: Optional PMM service name annotation.
    :type service_name: NonEmptyStr | None
    """

    task_name: NonEmptyStr
    hostname: NonEmptyStr
    credentials_path: NonEmptyStr | EmptyStrToNone = None
    service_name: NonEmptyStr | None = None

    @classmethod
    def from_form(
        cls,
        form: RestoreCreate,
        service_name: str | None,
    ) -> Self:
        """Build pbm-force-resync leg inputs from a :class:`RestoreCreate` form."""
        return cls(
            task_name=form.task_name,
            hostname=form.hostname,
            credentials_path=form.credentials_path or None,
            service_name=service_name,
        )


class RestoreLegPayloadModels(NamedTuple):
    """Represent typed per-leg payload models before :class:`TaskWrite` assembly."""

    config: RestoreConfigPayloadModel
    restore: RestoreLegPayloadModel
    pbm_list: PbmListPayloadModel
    force_resync: PbmForceResyncPayloadModel | None


def restore_leg_payload_models_from_form(
    form: RestoreCreate,
    service_name: str | None,
) -> RestoreLegPayloadModels:
    """Build all per-leg payload models from restore form input."""
    force_resync = None
    if form.backup_type == BackupType.PBM_PHYSICAL:
        force_resync = PbmForceResyncPayloadModel.from_form(form, service_name)
    return RestoreLegPayloadModels(
        config=RestoreConfigPayloadModel.from_form(form, service_name),
        restore=RestoreLegPayloadModel.from_form(form, service_name),
        pbm_list=PbmListPayloadModel.from_form(form, service_name),
        force_resync=force_resync,
    )


class RestoreTaskGroupPayloads(NamedTuple):
    """Represent TaskWrite payloads for all restore task legs.

    :param config_task: Parent restore-config task payload.
    :type config_task: TaskWrite
    :param restore_task: Restore execution task payload.
    :type restore_task: TaskWrite
    :param pbm_list_task: pbm-list helper task payload.
    :type pbm_list_task: TaskWrite
    :param force_resync_task: Optional pbm-force-resync task payload.
    :type force_resync_task: TaskWrite | None
    """

    config_task: TaskWrite
    restore_task: TaskWrite
    pbm_list_task: TaskWrite
    force_resync_task: TaskWrite | None


class RestoreDerivedTaskSummary(BaseModel):
    """Represent one child task in a restore task group detail response.

    :param name: The name of the child task.
    :type name: str
    :param status: The latest execution status of the child task.
    :type status: TaskHistoryStatusEnum | None
    """

    name: str
    status: TaskHistoryStatusEnum | None = None


class RestoreTaskResponse(BaseTaskResponse):
    """Represent a restore task API response.

    Extend the standard task-response surface with the restore destination facts
    the API surfaces; the shared task identity, status, audit, anonymization, and
    connectivity fields come from
    :class:`~app.extensions.apps.framework.responses.BaseTaskResponse`.

    :param hostname: The target hostname for the task execution.
    :param backup_type: The PBM backup type for this restore.
    :param backup_source: The backup name or timestamp to restore from.
    """

    hostname: str | None = None
    backup_type: str
    backup_source: str


class RestoreTaskDetailResponse(RestoreTaskResponse):
    """Represent a restore task detail API response.

    :param derived_tasks: Latest status for each restore child task.
    :type derived_tasks: list[RestoreDerivedTaskSummary]
    """

    derived_tasks: list[RestoreDerivedTaskSummary] = Field(default_factory=list)
