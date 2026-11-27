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

"""Define the one executor boundary OM dispatch submits through.

Submit-by-token, observe, cancel, workload identity, a structured result and the
per-job secret channel, with executor-specific code kept behind it. The transport
facts are nullable and carry no domain status; the authoritative status lives in
pmm-managed. Submit dedupes on the ``executor_token`` the dispatch receipt stored,
so a repeat returns the original task and starts no new job; cancel is idempotent
and reports terminal evidence.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.core.utils.fields import UTCDatetime
from app.extensions.apps.shared.om.secret_delivery import SecretSink
from app.extensions.apps.shared.om.step_contract import StepResult

__all__ = [
    "CancelEvidence",
    "ExecutorAdapter",
    "ExecutorObservation",
    "ExecutorTarget",
]


class ExecutorTarget(BaseModel):
    """Name an executor-side target, translated from a PMM node id in the dispatch layer.

    :param identifier: The executor's own name for where a job runs.
    """

    model_config = ConfigDict(frozen=True)

    identifier: str


class ExecutorObservation(BaseModel):
    """Carry a job's nullable transport facts and typed result, with no status.

    :param submission_id: The executor's id for the accepted submission, or ``None``
        before acceptance.
    :param task_history_id: The Tasks history id backing the job, written once after
        acceptance, or ``None``.
    :param accepted_at: When the executor accepted the submission, or ``None``.
    :param submitted_at: When the job was submitted, or ``None``.
    :param completed_at: When the job reached a terminal transport state, or ``None``.
    :param result: The typed, bounded, redacted result once known, or ``None``.
    :param gone: Whether the task vanished, so the result is :attr:`~app.extensions.
        apps.shared.om.step_contract.StepResultKind.UNKNOWN` and E0-T2 decides the
        resolution.
    """

    model_config = ConfigDict(frozen=True)

    submission_id: str | None = None
    task_history_id: int | None = None
    accepted_at: UTCDatetime | None = None
    submitted_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    result: StepResult | None = None
    gone: bool = False


class CancelEvidence(BaseModel):
    """Report a cancel's outcome, idempotently.

    :param terminal: Whether the job is in a terminal state after the cancel.
    :param detail: A bounded account of the stop or terminal evidence, or ``None``.
    """

    model_config = ConfigDict(frozen=True)

    terminal: bool
    detail: str | None = None


@runtime_checkable
class ExecutorAdapter(Protocol):
    """Define the one boundary OM dispatch submits, observes and cancels through."""

    async def submit(
        self, executor_token: str, target: ExecutorTarget, argv: list[str]
    ) -> str:
        """Submit a job by token, returning the original task if already accepted.

        Idempotent on ``executor_token``: a repeat starts no new job, mints no token
        and re-delivers nothing - it returns the submission the executor already
        accepted for that token.

        :param executor_token: The token the executor dedupes on, from the dispatch
            receipt.
        :param target: The executor-side target translated from the PMM node id.
        :param argv: The command line to run.
        :return: The submission id, the same one on every repeat of a token.
        """

    async def observe(self, submission_id: str) -> ExecutorObservation:
        """Observe a submission's transport facts and result, read-only.

        :param submission_id: The submission to observe.
        :return: The submission's nullable transport facts and typed result; a
            vanished task reads ``gone`` with an unknown result.
        """

    async def cancel(self, submission_id: str) -> CancelEvidence:
        """Cancel a submission, idempotently, reporting terminal evidence.

        :param submission_id: The submission to cancel.
        :return: Whether the job is terminal, with its stop evidence; the same
            evidence on a repeat.
        """

    async def secret_sink(self, submission_id: str) -> SecretSink:
        """Return the per-job secret sink bytes may be delivered to.

        :param submission_id: The submission whose sink to return.
        :return: The sink a conforming secret provider writes bytes to.
        """

    def workload_identity(self) -> str:
        """Return the workload identity jobs submitted through this adapter run as.

        :return: The adapter's workload identity.
        """
