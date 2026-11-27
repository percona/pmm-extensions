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

"""Test the executor adapter contract against a fake: idempotent submit and cancel."""

import pytest

from app.extensions.apps.shared.om.executor_adapter import (
    CancelEvidence,
    ExecutorAdapter,
    ExecutorObservation,
    ExecutorTarget,
)
from app.extensions.apps.shared.om.secret_delivery import SecretSink
from app.extensions.apps.shared.om.step_contract import StepResult, StepResultKind


class _Sink:
    """Record delivered bytes for one submission."""

    def __init__(self) -> None:
        self.received = bytearray()

    def write(self, material: bytes) -> None:
        """Record delivered bytes.

        :param material: The secret's bytes.
        """
        self.received += material


class _FakeAdapter:
    """Implement the executor adapter in memory, deduping submits on their token."""

    def __init__(self) -> None:
        self._by_token: dict[str, str] = {}
        self._argv: dict[str, list[str]] = {}
        self._cancelled: set[str] = set()
        self._next = 0

    async def submit(
        self, executor_token: str, target: ExecutorTarget, argv: list[str]
    ) -> str:
        """Return the original submission for a seen token, else create one.

        :param executor_token: The token the executor dedupes on.
        :param target: The executor-side target.
        :param argv: The command line.
        :return: The submission id, the same one on every repeat of a token.
        """
        if executor_token not in self._by_token:
            self._next += 1
            submission_id = f"sub-{self._next}-{target.identifier}"
            self._by_token[executor_token] = submission_id
            self._argv[submission_id] = argv
        return self._by_token[executor_token]

    async def observe(self, submission_id: str) -> ExecutorObservation:
        """Observe a submission, reporting a vanished one as gone and unknown.

        :param submission_id: The submission to observe.
        :return: The submission's facts, or a gone observation when unknown.
        """
        if submission_id not in self._argv:
            return ExecutorObservation(
                gone=True, result=StepResult(kind=StepResultKind.UNKNOWN)
            )
        return ExecutorObservation(
            submission_id=submission_id,
            result=StepResult(kind=StepResultKind.OK),
        )

    async def cancel(self, submission_id: str) -> CancelEvidence:
        """Cancel a submission idempotently, reporting terminal evidence.

        :param submission_id: The submission to cancel.
        :return: Terminal evidence, the same on every repeat.
        """
        self._cancelled.add(submission_id)
        return CancelEvidence(terminal=True, detail="stopped")

    async def secret_sink(self, submission_id: str) -> SecretSink:
        """Return a per-submission sink.

        :param submission_id: The submission whose sink to return.
        :return: The sink secret bytes are written to.
        """
        assert submission_id
        return _Sink()

    def workload_identity(self) -> str:
        """Return the fake adapter's workload identity.

        :return: A fixed identity.
        """
        return "om-fake"


_TARGET = ExecutorTarget(identifier="node-7")


def test_a_fake_adapter_satisfies_the_protocol() -> None:
    """Conform to :class:`ExecutorAdapter` structurally."""
    assert isinstance(_FakeAdapter(), ExecutorAdapter)


@pytest.mark.asyncio
async def test_submit_is_idempotent_by_token() -> None:
    """Return the same submission for a repeated token and start no new job."""
    adapter = _FakeAdapter()
    first = await adapter.submit("tok-a", _TARGET, ["mongosh"])
    repeat = await adapter.submit("tok-a", _TARGET, ["mongosh"])
    other = await adapter.submit("tok-b", _TARGET, ["mongosh"])
    assert first == repeat
    assert other != first
    assert set(adapter._by_token) == {"tok-a", "tok-b"}
    assert len(adapter._argv) == len(adapter._by_token)


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_terminal() -> None:
    """Report terminal evidence on cancel, the same on a repeat."""
    adapter = _FakeAdapter()
    submission_id = await adapter.submit("tok-a", _TARGET, ["mongosh"])
    first = await adapter.cancel(submission_id)
    repeat = await adapter.cancel(submission_id)
    assert first.terminal is True
    assert repeat == first


@pytest.mark.asyncio
async def test_a_vanished_task_reads_gone_and_unknown() -> None:
    """Observe a vanished submission as gone with an unknown result."""
    observation = await _FakeAdapter().observe("sub-missing")
    assert observation.gone is True
    assert observation.result is not None
    assert observation.result.kind is StepResultKind.UNKNOWN
