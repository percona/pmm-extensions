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

"""Resolve plugin-specific failure-alert enrichment via a per-task hook.

The tasks service owns the generic failure-alert path but must stay free of any
static ``app.extensions`` import: plugin domain knowledge (e.g. the archiver
``PURGE_LIST`` schema) lives in the plugin package, not here. Rather than the
core enumerating plugins by owner, each task *carries* its enrichment builder:
the owning plugin stamps a ``"module:function"`` string onto
``Task.alert_detail_builder`` at creation, and this task resolves it lazily via
the shared :func:`app.tasks.hook_resolver.resolve_hook` the first time the
task's alert fires. The
dependency points the right way — core discovers, the plugin declares — so a new
plugin needs no edit here. This mirrors how the Celery executor resolves a task
``callable`` by path.

Resolving lazily from the task — rather than a core registry populated at import
time — is what keeps this enumeration-free across the three processes that fire
``alert_for_status`` and share no plugin-bootstrap step: the Celery worker, the
PMM Extensions app, and the tasks API. A core registry would only cover whichever process
happened to import the plugin; a path carried on the task resolves identically
in all three.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from app.tasks.hook_resolver import resolve_hook

if TYPE_CHECKING:
    from app.tasks.models import TaskHistory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnerAlertDetails:
    """Hold the owner-specific additions to a task failure alert.

    :param source_node: The source database node name used in the alert
        summary (Short Description).
    :param custom_details: The provider-agnostic detail payload attached to the
        alert; surfaces through ``PagerDutyAlert.custom_details``.
    """

    source_node: str
    custom_details: dict[str, Any]


#: An ``async`` callable taking the failed ``TaskHistory`` and returning the
#: owner-specific alert additions (or ``None`` when nothing should be attached).
AlertDetailBuilder = Callable[["TaskHistory"], Awaitable[OwnerAlertDetails | None]]


async def build_owner_alert_details(
    history: TaskHistory,
) -> OwnerAlertDetails | None:
    """Build plugin-specific failure-alert details for the given history.

    Resolve the ``"module:function"`` builder the task declares in
    ``alert_detail_builder`` and delegate to it. Return ``None`` for any task
    without a builder, leaving the generic alert path unchanged. A builder whose
    path the allow-list refuses, that cannot be imported, or that raises while
    running is swallowed (logged) so a denied, missing, misconfigured, or buggy
    plugin can never prevent the failure alert itself from firing. This hook is
    best-effort enrichment; the base alert always takes priority.

    :param history: The failed task execution history.
    :return: The plugin-specific alert additions, or ``None``.
    """
    path = history.task.alert_detail_builder
    if not path:
        return None
    try:
        builder: AlertDetailBuilder = resolve_hook(path)
    except ImportError, AttributeError, ValueError:
        logger.exception("Failed to resolve alert detail builder %r.", path)
        return None
    try:
        return await builder(history)
    except Exception:
        logger.exception("Alert detail builder %r raised; skipping enrichment.", path)
        return None
