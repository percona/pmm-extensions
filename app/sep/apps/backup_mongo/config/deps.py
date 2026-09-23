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

"""Define the PBM Configuration child app's create dependency."""

from app.sep.apps.backup_mongo.config.models import BackupConfigCreate
from app.sep.apps.backup_mongo.deps import build_backup_task_payload
from app.sep.deps import InventoryAPI
from app.tasks.models import TaskWrite


async def build_config_task_payload(
    form: BackupConfigCreate,
    inventory_api: InventoryAPI,
) -> TaskWrite:
    """Build the config task's payload, reusing the parent's builder wholesale.

    This exists for its *annotation*: the framework reads the request model off
    the ``form`` parameter, so naming :class:`BackupConfigCreate` here is what
    defaults ``backup_type`` for the route. The body is the parent's builder
    unchanged -- a config task is the same envelope, and
    ``build_backup_mongo_spec`` already selects the ``pbm_config`` payload from
    ``backup_type``.

    :param form: The Configuration tab's validated create body.
    :param inventory_api: The Inventory API used to resolve the service name.
    :return: The ``TaskWrite`` consumed by the Tasks API.
    """
    return await build_backup_task_payload(form, inventory_api)
