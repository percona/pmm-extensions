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

"""Define the shared Celery application for PMM Extensions."""

import asyncio
import logging.config
from typing import Any

from celery import Celery
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_init,
)

from app.core.config import settings
from app.core.log import clear_log_context, correlation_id_var, set_log_context
from app.extensions.apps.framework.registry import build_celery_include

logger = logging.getLogger(__name__)

# Composed here, not in ``CeleryOptions.set_include``: reaching the registry during
# core ``Settings`` construction re-enters the half-built lazy proxy. The worker
# bootstrap composes the same helper, keeping the two include lists in lockstep.
settings.CELERY.include = build_celery_include()


class ExtensionsCelery(Celery):
    """Declare the per-process event loop this project installs on the app.

    The loop is set at import below and replaced in each forked worker by
    :func:`init_child_event_loop`, since a child must not drive the loop it
    inherited from the parent.

    :ivar loop: The event loop the current process drives task coroutines on.
    :vartype loop: asyncio.AbstractEventLoop
    """

    loop: asyncio.AbstractEventLoop


celery = ExtensionsCelery("extensions", **settings.CELERY.model_dump())

celery.loop = asyncio.new_event_loop()
asyncio.set_event_loop(celery.loop)


@setup_logging.connect
def setup_logging(**_kwargs: Any) -> None:
    """Define Celery signal to set up logging according to settings."""
    logging.config.dictConfig(settings.LOGGING_CONFIG)


@worker_process_init.connect
def init_child_event_loop(**kwargs: Any) -> None:
    """Initialize a new event loop for each worker process."""
    logger.debug("Initializing new event loop for worker process")
    celery.loop = asyncio.new_event_loop()
    asyncio.set_event_loop(celery.loop)


CORRELATION_ID_HEADER_KEY = "correlation_id"


@before_task_publish.connect
def propagate_correlation_id(headers: dict[str, Any], **kwargs: Any) -> None:
    """Copy the current correlation ID into outgoing task message headers.

    :param headers: The mutable task message headers dict.
    :type headers: dict[str, Any]
    :param kwargs: Additional signal keyword arguments (unused).
    :type kwargs: Any
    """
    correlation_id = correlation_id_var.get()
    if correlation_id != "-":
        headers[CORRELATION_ID_HEADER_KEY] = correlation_id


@task_prerun.connect
def set_task_log_context(task_id: str, task: Any, **kwargs: Any) -> None:
    """Set log context variables from the incoming Celery task.

    :param task_id: The unique task execution ID.
    :type task_id: str
    :param task: The Celery task instance.
    :type task: Any
    :param kwargs: Additional signal keyword arguments (unused).
    :type kwargs: Any
    """
    correlation_id = getattr(task.request, CORRELATION_ID_HEADER_KEY, None) or "-"
    set_log_context(
        correlation_id=correlation_id,
        task_id=task_id,
        task_name=task.name,
    )


@task_postrun.connect
def clear_task_log_context(**kwargs: Any) -> None:
    """Clear all log context variables after task execution.

    :param kwargs: Additional signal keyword arguments (unused).
    :type kwargs: Any
    """
    clear_log_context()
