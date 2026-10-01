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

"""Define the Nomad-only system task names the tasks service seeds.

Imports nothing from the rest of the tasks or extensions packages so seed can use these
values without the Nomad executor import graph. Constants that name a Nomad
job-spec step, or derive from one, belong in :mod:`.steps` instead.
"""

#: Seeded name of the Nomad-only periodic task that checks TLS cert expiry.
CHECK_NOMAD_CERT_EXPIRY_TASK_NAME = "tasks__check_nomad_cert_expiry"

#: Seeded name of the Nomad-only periodic task that syncs runs whose steps finished.
SYNC_FINISHING_TASKS_TASK_NAME = "tasks__sync_finishing_tasks"
