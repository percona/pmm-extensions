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

"""Test the step contract: the privilege gate, the stop proof, and the typed result."""

import pytest

from app.extensions.apps.shared.om.step_contract import (
    AgentElevation,
    authorise_privilege,
    MAX_STEP_RESULT_DETAIL,
    StepContract,
    StepPrivilege,
    StepResult,
    StepResultKind,
    StepTargetKind,
    stop_effect_result,
)

_ROOT_STEP = StepContract(
    privilege=StepPrivilege.ROOT, target_kind=StepTargetKind.NODE, idempotent=True
)
_UNPRIVILEGED_STEP = StepContract(
    privilege=StepPrivilege.NONE, target_kind=StepTargetKind.NODE, idempotent=True
)


class TestAuthorisePrivilege:
    """Gate a step's declared privilege against the host's measured capability."""

    @pytest.mark.parametrize("measured", list(AgentElevation))
    def test_allows_an_unprivileged_step_on_any_elevation(
        self, measured: AgentElevation
    ) -> None:
        """Never refuse a ``NONE`` step, whatever the host can elevate.

        :param measured: The host's measured elevation capability.
        """
        assert authorise_privilege(_UNPRIVILEGED_STEP, measured) is None

    @pytest.mark.parametrize(
        "measured",
        [AgentElevation.ROOT, AgentElevation.SUDO_ALL, AgentElevation.SUDO_LIMITED],
    )
    def test_allows_a_root_step_where_elevation_grants_root(
        self, measured: AgentElevation
    ) -> None:
        """Allow a ``ROOT`` step where the host can actually elevate.

        :param measured: A measured elevation that grants root.
        """
        assert authorise_privilege(_ROOT_STEP, measured) is None

    @pytest.mark.parametrize(
        "case",
        [
            (AgentElevation.BLOCKED_NO_NEW_PRIVILEGES, True),
            (AgentElevation.NONE, False),
        ],
    )
    def test_refuses_a_root_step_where_nothing_elevates(
        self, case: tuple[AgentElevation, bool]
    ) -> None:
        """Refuse a ``ROOT`` step before submit where nothing elevates, naming the reason.

        The structured decision names ``NoNewPrivileges`` only for that measured
        fact, which no other elevation state can be mistaken for.

        :param case: The measured elevation and whether the refusal is attributed to
            ``NoNewPrivileges``.
        """
        measured, no_new_privileges = case
        decision = authorise_privilege(_ROOT_STEP, measured)
        assert decision is not None
        assert decision.required is StepPrivilege.ROOT
        assert decision.measured is measured
        assert decision.no_new_privileges is no_new_privileges


class TestStopEffectResult:
    """Prove a stop step's effect from the unit's own fields, per its intent."""

    def test_clean_intent_requires_a_clean_exit(self) -> None:
        """Prove a clean-intent stop only when the unit exited cleanly."""
        proven = stop_effect_result(
            _UNPRIVILEGED_STEP, active=False, main_pid=0, clean_exit=True
        )
        unclean = stop_effect_result(
            _UNPRIVILEGED_STEP, active=False, main_pid=0, clean_exit=False
        )
        assert proven.kind is StepResultKind.OK
        assert unclean.kind is StepResultKind.FAILED

    def test_destroy_intent_accepts_an_unclean_exit(self) -> None:
        """Prove a destroy-intent stop once down, even after a ``SIGKILL``."""
        destroy = StepContract(
            privilege=StepPrivilege.ROOT,
            target_kind=StepTargetKind.MEMBER,
            idempotent=True,
            destroy_intent=True,
        )
        result = stop_effect_result(destroy, active=False, main_pid=0, clean_exit=False)
        assert result.kind is StepResultKind.OK

    @pytest.mark.parametrize(
        "unit_state",
        [(True, 0), (False, 4321), (True, 4321)],
    )
    def test_a_still_running_unit_is_not_proven_down(
        self, unit_state: tuple[bool, int]
    ) -> None:
        """Refuse to prove a stop while the unit is active or keeps a main PID.

        :param unit_state: The unit's ``(active, MainPID)``.
        """
        active, main_pid = unit_state
        destroy = StepContract(
            privilege=StepPrivilege.ROOT,
            target_kind=StepTargetKind.MEMBER,
            idempotent=True,
            destroy_intent=True,
        )
        result = stop_effect_result(
            destroy, active=active, main_pid=main_pid, clean_exit=True
        )
        assert result.kind is StepResultKind.FAILED


class TestStepResult:
    """Carry a bounded, redacted detail and a typed kind."""

    def test_redacted_truncates_an_over_long_detail(self) -> None:
        """Bound the detail to :data:`MAX_STEP_RESULT_DETAIL` characters."""
        result = StepResult.redacted(
            StepResultKind.FAILED, "x" * (MAX_STEP_RESULT_DETAIL + 50)
        )
        assert result.detail is not None
        assert len(result.detail) == MAX_STEP_RESULT_DETAIL

    def test_redacted_keeps_an_absent_detail_absent(self) -> None:
        """Keep a ``None`` detail ``None``."""
        assert StepResult.redacted(StepResultKind.OK).detail is None


class TestStepTargetKind:
    """Accept the data model's target kinds, including the backup-epic seam."""

    def test_a_step_may_target_a_replica_set(self) -> None:
        """Accept a ``replica_set`` target, so a per-domain step runs once per domain."""
        contract = StepContract(
            privilege=StepPrivilege.NONE,
            target_kind=StepTargetKind.REPLICA_SET,
            idempotent=True,
        )
        assert contract.target_kind is StepTargetKind.REPLICA_SET

    def test_a_long_running_step_may_declare_it_cannot_cancel(self) -> None:
        """Let a long-running step declare cancel is unsupported, as PBM restore is."""
        contract = StepContract(
            privilege=StepPrivilege.NONE,
            target_kind=StepTargetKind.REPLICA_SET,
            idempotent=False,
            long_running=True,
            cancellable=False,
        )
        assert contract.long_running is True
        assert contract.cancellable is False
