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

"""Reconcile diagnostics runs whose outcome the recorder hook never observed.

Two gaps make this sweep load-bearing rather than belt-and-braces, and both are
permanent: ``maybe_record_run`` documents three terminal transitions it is not
invoked for (a run stopped via the stop route, one that failed before dispatch,
one the connectivity probe drives terminal), and every execution dispatched before
ATW owned a proxy task predates the recorder entirely.

There is deliberately **no time cutoff**: an older incident showing an
authoritative-looking zero is exactly the wrong answer for the screen this
feature exists to fix.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlmodel import col

from app.core.exceptions import HTTPNotFoundException
from app.core.requests import as_json_object, RemoteAPI
from app.core.security import get_internal_token
from app.core.utils.date_time import utc_now
from app.core.utils.fields import UTCDatetime
from app.extensions.apps.atw.crud import AtwIncidentExecutionManager
from app.extensions.apps.atw.models import AtwIncidentExecution
from app.extensions.apps.atw.send import get_tasks_api
from app.extensions.db import get_async_session_maker
from app.tasks.models import TaskHistoryStatusEnum

logger = logging.getLogger(__name__)


class _UpstreamRun(BaseModel):
    """Read just the outcome fields off an upstream task-history payload.

    Validating rather than reading raw keys is what coerces the serialized status
    to its enum member and the ISO timestamp to an aware ``datetime``, so an
    unrecognized status surfaces as a rejection instead of reaching the column.

    :param status: The run's current status.
    :param finished_at: When the run finished, if it has.
    """

    status: TaskHistoryStatusEnum
    finished_at: UTCDatetime | None = None


async def _resolve_outcome(
    tasks_api: RemoteAPI, execution_id: UUID, task_history_id: int
) -> dict[str, Any]:
    """Return the outcome columns to write for one execution, if any are known.

    The two failure branches are the load-bearing pair. A genuine ``404`` means the
    upstream row is gone and no outcome will ever exist, so the row is retired
    permanently and never re-queried. Any other HTTP or transport error is
    transient and must leave the row eligible — reading a 503 as "gone" would
    permanently retire a live execution.

    This is why the sweep does **not** reuse ``fetch_task_history``, which degrades
    every ``HTTPException | OSError`` to an empty mapping: correct for a listing
    that wants to blank one row, catastrophic here.

    :param tasks_api: The authenticated Tasks API client.
    :param execution_id: The ATW execution row being reconciled, for logging.
    :param task_history_id: The upstream history row to read the outcome from.
    :return: The columns to write, or an empty mapping to leave the row unresolved.
    """
    try:
        payload = as_json_object(await tasks_api.get(f"/history/{task_history_id}"))
    except HTTPNotFoundException:
        logger.info(
            "Task history %s no longer resolves; retiring ATW execution %s as having "
            "no recoverable outcome.",
            task_history_id,
            execution_id,
        )
        return {"outcome_unrecoverable": True}
    except (HTTPException, OSError):
        logger.warning(
            "Could not reach task history %s; leaving ATW execution %s unresolved for "
            "a later tick.",
            task_history_id,
            execution_id,
            exc_info=True,
        )
        return {}
    try:
        run = _UpstreamRun.model_validate(payload)
    except ValidationError:
        logger.warning(
            "Task history %s reported an unreadable outcome; leaving ATW execution %s "
            "unresolved.",
            task_history_id,
            execution_id,
            exc_info=True,
        )
        return {}
    if not run.status.is_terminal():
        return {}
    return {"terminal_status": run.status, "finished_at": run.finished_at}


async def _reconcile_one(
    tasks_api: RemoteAPI, execution_id: UUID, task_history_id: int
) -> None:
    """Write one execution's resolved outcome, stamping the attempt either way.

    ``reconcile_attempted_at`` is stamped on every path, including both failures, so
    a row the sweep could not resolve still yields its batch slot to the rows behind
    it — which is what makes each tick a round-robin rather than a re-examination of
    the same head of the queue.

    Each row is written on its own session rather than one shared across the batch:
    under PostgreSQL a failed statement aborts the whole transaction, so a shared
    session would turn one bad row into a lost tick. The sessions are cheap — they
    come from the same engine's pool.

    :param tasks_api: The authenticated Tasks API client.
    :param execution_id: The ATW execution row to update.
    :param task_history_id: The upstream history row to read the outcome from.
    """
    outcome = await _resolve_outcome(tasks_api, execution_id, task_history_id)
    async with get_async_session_maker()() as session:
        await AtwIncidentExecutionManager.update_where(
            session,
            {"reconcile_attempted_at": utc_now(), **outcome},
            id=execution_id,
        )


async def reconcile_executions(batch_size: int) -> None:
    """Resolve one bounded batch of executions with no recorded outcome.

    Selection is least-recently-attempted first, so each tick is a round-robin over
    the unresolved set and a row that is legitimately still running cannot starve
    the rows behind it.

    The batch is claimed before its first upstream request: the selecting session
    stamps every selected row's attempt cursor. One request may take minutes against
    a slow tasks service, so a tick can outlive the schedule interval, and without
    the claim the next tick would re-select the rows this one is still working
    through and request each of them a second time.

    :param batch_size: The most executions this tick may examine.
    """
    async with get_async_session_maker()() as session:
        rows = await AtwIncidentExecutionManager.unresolved_batch(session, batch_size)
        # Read inside the session that selected them: the loop below runs after it
        # closes, so it must not touch an ORM attribute.
        targets = [(row.id, row.task_history_id) for row in rows]
        if targets:
            await AtwIncidentExecutionManager.update_where(
                session,
                {"reconcile_attempted_at": utc_now()},
                col(AtwIncidentExecution.id).in_(
                    [execution_id for execution_id, _ in targets]
                ),
            )
    if not targets:
        return
    client = await get_tasks_api()
    with client.auth(get_internal_token()) as tasks_api:
        for execution_id, task_history_id in targets:
            await _reconcile_one(tasks_api, execution_id, task_history_id)
    logger.info("Examined %d unresolved ATW execution(s).", len(targets))
