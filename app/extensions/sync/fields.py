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

"""Define the constrained syncer fields and the values their config accepts.

A syncer threshold is configurable three ways — a ``SYNCERS[]`` entry in
``settings.yaml``, JSON in ``EXTENSIONS__SYNCERS``, or an ``EXTENSIONS__SYNCER_EXTRA_KWARGS__<KEY>``
env leaf — and ``SyncOptions`` forwards every one of them verbatim, untyped. The
syncer field and the load-time configuration check therefore have to agree on the
accepted values, so the annotated type, the field name and the spelling quoted back to
an operator all live here, one edit apart. This module deliberately imports nothing
from the PMM Extensions app so ``app.extensions.config`` can use it.
"""

from datetime import timedelta
from typing import Annotated, Any, Final, NamedTuple

from annotated_types import Ge, Gt
from pydantic import TypeAdapter

StaleRunAfter = Annotated[timedelta, Gt(timedelta(0))]
"""Define the age beyond which an idle in-progress sync run is reclaimable.

Positivity is an annotation constraint rather than a ``field_validator`` because
``SyncOptions`` carries ``extra="allow"`` and forwards every extra key verbatim, and
runtime-override coercion re-checks annotated-type constraints but does not re-run
field validators.
"""

MissingGraceGenerations = Annotated[int, Ge(2)]
"""Define how many complete generations must report an entity absent before removal.

The floor is 2, not 1: at 1 the grace counter collapses back to acting on a single
reported absence, which is the behaviour it exists to end. Expressed as an annotation
constraint for the reason :data:`StaleRunAfter` is.
"""

TaskExecutionTimeout = Annotated[int, Gt(0)]
"""Define the number of seconds a task is allowed to take before it times out.

At zero or below the poll loop's ``time_waiting < task_execution_timeout`` guard is
false on its first evaluation, so the loop never runs and the task is declared timed
out without having been polled once. Expressed as an annotation constraint for the
reason :data:`StaleRunAfter` is.
"""

TasksExecutionWaitInterval = Annotated[int, Gt(0)]
"""Define the number of seconds between two task status checks.

The interval is also what advances the poll loop's elapsed-time counter, so at zero
the counter never moves and the timeout above can never fire, and below zero it moves
backwards and the loop stops terminating at all. Expressed as an annotation constraint
for the reason :data:`StaleRunAfter` is.
"""


class SyncerFieldConstraint(NamedTuple):
    """Pair a constrained syncer field with the spelling of its accepted values.

    :param adapter: Validator for the field's annotated type, constraints included.
    :param accepted: Human-readable list of the forms the field accepts, quoted back
        to the operator when their value is refused.
    """

    adapter: TypeAdapter[Any]
    accepted: str


CONSTRAINED_SYNCER_FIELDS: Final[dict[str, SyncerFieldConstraint]] = {
    "stale_run_after": SyncerFieldConstraint(
        TypeAdapter(StaleRunAfter),
        "integer or float seconds (3600), an ISO-8601 duration (PT1H), or HH:MM:SS",
    ),
    "missing_grace_generations": SyncerFieldConstraint(
        TypeAdapter(MissingGraceGenerations),
        "an integer of 2 or more",
    ),
    "task_execution_timeout": SyncerFieldConstraint(
        TypeAdapter(TaskExecutionTimeout),
        "a positive integer number of seconds",
    ),
    "tasks_execution_wait_interval": SyncerFieldConstraint(
        TypeAdapter(TasksExecutionWaitInterval),
        "a positive integer number of seconds",
    ),
}
"""Map each constrained syncer field to the validator its configured value must pass.

``SyncOptions`` and ``SyncerExtraKwargs`` both allow untyped extras, so a value
configured for one of these fields is only checked against the field's real type once
a syncer is constructed — which, for the request-scoped ``get_syncers`` dependency,
means once per request. Checking it against this mapping at settings load turns a
misconfiguration into a single startup failure instead. Keys are the field names as
:class:`~app.core.models.BaseLowercaseModel` stores them, lowercased.
"""
