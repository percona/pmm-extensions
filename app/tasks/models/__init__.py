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

"""Define models for the Task API.

The models are split by role into submodules: :mod:`.constants`, :mod:`.enums`,
:mod:`.payloads`, :mod:`.bases`, :mod:`.tables`, :mod:`.requests` and
:mod:`.responses`. Every public name is re-exported here, so
``from app.tasks.models import ...`` keeps working and the Alembic
environment's star import still registers every table.
"""

from app.tasks.models.bases import (
    TaskBase,
    TaskHistoryBase,
)
from app.tasks.models.constants import (
    ANY_OWNER,
    CELERY_LOCAL_TARGET,
    EXECUTE_TASK_BY_NAME_TASK,
    GENERIC_EXECUTOR_TASK_NAMES,
    INTERNAL_TASK_NAMES,
    INVENTORY_COLLECTION_TASK_NAME,
    INVENTORY_SYNC_FIRST_RUN_KEY,
    INVENTORY_SYNC_FOLLOWERS_KEY,
    INVENTORY_SYNC_TASK_NAME,
    LATEST_HISTORY_STATUS_NAMES_MAX,
    MAX_FAILURE_REASON_LENGTH,
    REQUIREMENTS_META_KEY,
    STORED_REQUEST_CONTEXT,
    SYNC_RUNNING_TASKS_TASK_NAME,
    SYSTEM_USER,
    TASK_ALIAS_LENGTH,
)
from app.tasks.models.enums import (
    CAPTURE_STATUS_PRECEDENCE,
    LogCaptureStatusEnum,
    TaskBackendEnum,
    TaskLogType,
)

# Imported by the model tests, so it stays reachable from the package path.
from app.tasks.models.payloads import _encode_anonymize_mask as _encode_anonymize_mask
from app.tasks.models.payloads import (
    AnonymizeMask,
    ExecutionEvent,
    ExecutorHostState,
    FileMetadata,
    normalize_requirements_field,
    normalize_requirements_meta,
    TaskExecutionRequest,
    TaskExecutionRequestJSON,
    TaskLog,
)
from app.tasks.models.requests import (
    TaskExecuteRequest,
    TaskHistoryLatestStatusRequest,
    TaskWrite,
)
from app.tasks.models.responses import (
    TaskHistoryLatestStatus,
    TaskHistoryResponse,
    TaskHistoryStatusPoint,
    TaskResponse,
    TaskStats,
)
from app.tasks.models.tables import (
    DispatchLock,
    Task,
    TaskHistory,
    TaskHistoryLog,
    TaskHistoryLogState,
)
from app.tasks.task_status import TaskHistoryStatusEnum

__all__ = [
    "ANY_OWNER",
    "CAPTURE_STATUS_PRECEDENCE",
    "CELERY_LOCAL_TARGET",
    "EXECUTE_TASK_BY_NAME_TASK",
    "GENERIC_EXECUTOR_TASK_NAMES",
    "INTERNAL_TASK_NAMES",
    "INVENTORY_COLLECTION_TASK_NAME",
    "INVENTORY_SYNC_FIRST_RUN_KEY",
    "INVENTORY_SYNC_FOLLOWERS_KEY",
    "INVENTORY_SYNC_TASK_NAME",
    "LATEST_HISTORY_STATUS_NAMES_MAX",
    "MAX_FAILURE_REASON_LENGTH",
    "REQUIREMENTS_META_KEY",
    "STORED_REQUEST_CONTEXT",
    "SYNC_RUNNING_TASKS_TASK_NAME",
    "SYSTEM_USER",
    "TASK_ALIAS_LENGTH",
    "AnonymizeMask",
    "DispatchLock",
    "ExecutionEvent",
    "ExecutorHostState",
    "FileMetadata",
    "LogCaptureStatusEnum",
    "Task",
    "TaskBackendEnum",
    "TaskBase",
    "TaskExecuteRequest",
    "TaskExecutionRequest",
    "TaskExecutionRequestJSON",
    "TaskHistory",
    "TaskHistoryBase",
    "TaskHistoryLatestStatus",
    "TaskHistoryLatestStatusRequest",
    "TaskHistoryLog",
    "TaskHistoryLogState",
    "TaskHistoryResponse",
    "TaskHistoryStatusEnum",
    "TaskHistoryStatusPoint",
    "TaskLog",
    "TaskLogType",
    "TaskResponse",
    "TaskStats",
    "TaskWrite",
    "normalize_requirements_field",
    "normalize_requirements_meta",
]
