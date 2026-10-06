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

"""Define the OpenManager Inventory Celery entry point.

``@owned_by("om_inventory")`` tags the task so the app-drain reconciler counts it
toward this app rather than treating it as a core task. The module is included in the
worker's ``include`` list because the app is registered in ``EXTENSIONS.APPS`` and the
registry derives the Celery module path from the package's ``celery.py``.
"""

import logging
from uuid import UUID

from app.celery import celery
from app.extensions.app_drain import owned_by
from app.extensions.apps.om_inventory.service import run_probe

logger = logging.getLogger(__name__)


@owned_by("om_inventory")
@celery.task
def run_om_probe(
    execution_id: str | None = None, node_ids: list[str] | None = None
) -> str:
    """Run one probe sweep and return its id.

    Scheduled by this app's ``periodic_task_schedules`` contribution, and invoked by
    the trigger endpoint with an already-created id so the caller can be answered
    before the Nomad work begins.

    :param execution_id: An already-created run's id, or ``None`` to mint one.
    :param node_ids: The hosts to refresh, or ``None`` for the whole estate. The
        scheduled sweep passes nothing, which is what keeps it a full refresh.
    :return: The run's id, as a string.
    """
    # ty-attr-ok: Celery installs `loop` at runtime.
    resolved = celery.loop.run_until_complete(
        run_probe(UUID(execution_id) if execution_id else None, node_ids)
    )
    return str(resolved)
