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

"""Define the constants of the Task API models."""

TASK_ALIAS_LENGTH = 100
SYSTEM_USER = "SYSTEM"
ANY_OWNER = "ANY"

#: ``meta`` key holding the pip requirements a Python task installs. The
#: executor writes it verbatim into ``requirements.txt``, so it is dispatched as
#: one newline-separated string.
REQUIREMENTS_META_KEY = "requirements"

#: Validation context for a :class:`TaskExecutionRequest` read back from
#: storage, where a legacy ``meta.requirements`` must not stop the row loading.
STORED_REQUEST_CONTEXT: dict[str, bool] = {"stored": True}

#: The only target the Celery backend runs on: PMM Extensions' own worker, which
#: is not a PMM node.
CELERY_LOCAL_TARGET = "local"

#: Maximum stored length of ``TaskHistory.failure_reason``. Above every reason
#: PMM Extensions composes from fixed prose alone; the bound is for the three that
#: interpolate a value — the payload reference's error, a Nomad step name and
#: the resolved callable path — none of which is bounded at composition time.
MAX_FAILURE_REASON_LENGTH = 500

GENERIC_EXECUTOR_TASK_NAMES: frozenset[str] = frozenset(
    {
        "run-python",
        "exec-artifact",
        "exec-python-artifact",
    }
)

INVENTORY_SYNC_TASK_NAME = "inventory-sync"
#: The Celery task every seeded or kicked ``inventory-sync`` run goes through.
EXECUTE_TASK_BY_NAME_TASK = "app.tasks.celery.execute_task_by_name"
#: Meta key naming the per-syncer schedules this one starts after its first pass.
INVENTORY_SYNC_FOLLOWERS_KEY = "follower_syncers"
#: Meta key marking a leader-started run, which skips when its syncer is busy.
INVENTORY_SYNC_FIRST_RUN_KEY = "first_run_only"
INVENTORY_COLLECTION_TASK_NAME = "inventory-collection"
SYNC_RUNNING_TASKS_TASK_NAME = "tasks__sync_running_tasks"

#: Maintenance / system task names excluded from user-facing task lists.
#: The cert-expiry member is a literal matching
#: :data:`~app.tasks.execution.executors.nomad.constants.CHECK_NOMAD_CERT_EXPIRY_TASK_NAME`
#: so this module does not import the Nomad executor package.
INTERNAL_TASK_NAMES: frozenset[str] = frozenset(
    {
        INVENTORY_SYNC_TASK_NAME,
        INVENTORY_COLLECTION_TASK_NAME,
        SYNC_RUNNING_TASKS_TASK_NAME,
        "tasks__check_nomad_cert_expiry",
    }
)

LATEST_HISTORY_STATUS_NAMES_MAX = 200
