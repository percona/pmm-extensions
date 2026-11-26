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

"""Declare the step contract every OM operation step is dispatched against.

One glossary the pmm-managed driver's extension guide reuses: a step states its
required privilege, whether it is idempotent, what target it acts on, and a typed
result that carries no status of its own. The authoritative operation and step
status lives in pmm-managed; a result here is copied onto it, never owned here.

The privilege gate is fail-closed: a step that needs elevation a host's measured
capability fact does not grant is refused before any job is submitted, so an
undeclared privilege can never be exercised.
"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "MAX_STEP_RESULT_DETAIL",
    "AgentElevation",
    "PrivilegeDecision",
    "StepContract",
    "StepPrivilege",
    "StepResult",
    "StepResultKind",
    "StepTargetKind",
    "authorise_privilege",
    "stop_effect_result",
]

#: Bound on a step result's redacted detail. A result carries why a step ended,
#: never a raw stream: dispatch stderr or a package manager's whole transaction
#: runs to kilobytes, and only the tail that names the outcome is kept.
MAX_STEP_RESULT_DETAIL = 500


class StepPrivilege(StrEnum):
    """Name the privilege a step declares it needs to act.

    The dispatch layer selects the interpreter from it. ``ROOT`` uses an elevated
    interpreter only when a capability fact says the host granted it; otherwise the
    dispatch is refused before submit. Sudoers and ``NoNewPrivileges`` provisioning
    stay in PMM-15843/PMM-12362, not here.

    :cvar ROOT: The step must run elevated.
    :cvar NONE: The step runs as the unprivileged agent user.
    """

    ROOT = "root"
    NONE = "none"


class AgentElevation(StrEnum):
    """Name a host's measured elevation capability, as E0-SP1 live runs decided it.

    Replaces PMM's ``can_elevate`` heuristic (``euid == 0`` or ``which sudo``),
    which reports yes where nothing actually elevates. The fact comes from a shell
    capability step, not the Python payload, because fapolicyd's default rules stop
    the payload for root and non-root alike while shell scripts are allowed.

    :cvar ROOT: The agent already runs as root.
    :cvar SUDO_ALL: The agent may ``sudo`` any command without a password.
    :cvar SUDO_LIMITED: The agent may ``sudo`` only specific commands, answered per
        command with ``sudo -n -l <command>``.
    :cvar BLOCKED_NO_NEW_PRIVILEGES: The unit runs under ``NoNewPrivileges=yes``, so
        nothing elevates; ``sudo`` reports only "effective uid is not 0 ... nosuid"
        and cannot read sudoers while it is set.
    :cvar NONE: No elevation path exists.
    """

    ROOT = "root"
    SUDO_ALL = "sudo_all"
    SUDO_LIMITED = "sudo_limited"
    BLOCKED_NO_NEW_PRIVILEGES = "blocked_no_new_privileges"
    NONE = "none"


class StepResultKind(StrEnum):
    """Name a step's typed outcome, with no status field of its own.

    E0-T2 copies this onto ``om_operation_steps.result``; the lifecycle status is
    pmm-managed's to decide from it.

    :cvar OK: The effect is proven present.
    :cvar BLOCKED: A precondition refused the step before it mutated anything.
    :cvar FAILED: The step ran and did not achieve its effect.
    :cvar TIMED_OUT: The step exceeded its deadline.
    :cvar UNKNOWN: The outcome could not be determined, e.g. a vanished task whose
        proof probe could not run.
    """

    OK = "ok"
    BLOCKED = "blocked"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


class StepTargetKind(StrEnum):
    """Name what a step acts on, matching E0-T1's ``om_operation_steps.target_kind``.

    The PMM node that executes a step is a separate fact, translated to the executor
    target only inside the dispatch layer; it is not this target.

    :cvar MEMBER: One replica-set member.
    :cvar REPLICA_SET: One authentication domain of the operation's cluster, so a
        per-domain step runs once per domain rather than once per member.
    :cvar NODE: One PMM node.
    :cvar RUN: The operation itself, for a run-level step.
    """

    MEMBER = "member"
    REPLICA_SET = "replica_set"
    NODE = "node"
    RUN = "run"


class StepContract(BaseModel):
    """Declare what one step needs and promises, typed, before it is dispatched.

    :param privilege: The privilege the step needs; gated against the host's
        measured :class:`AgentElevation` before submit.
    :param target_kind: What the step acts on.
    :param idempotent: Whether dispatching the step again could change the effect.
        An idempotent-false step's precondition is "effect absent", so it returns
        already-done without acting when the effect is already present.
    :param destroy_intent: Whether a stop step's only postcondition is that mongod
        is down, so its proof probe accepts an unclean exit (E0-S6 uninstall). A
        clean-intent stop instead requires the unit to have exited cleanly.
    :param long_running: Whether the step returns a handle once its effect starts
        and is observed read-only thereafter rather than re-sent (a PBM backup or
        restore).
    :param cancellable: Whether a long-running step can be cancelled; a step may
        declare it cannot (PBM cannot cancel a restore).
    :param pbm_cli_secret_exception: Whether the step uses the one narrow exception
        that lets a secret reach a single ``pbm`` process's argv or environment,
        because that CLI reads its URI only from there. The secret still appears in
        no other sink.
    """

    model_config = ConfigDict(frozen=True)

    privilege: StepPrivilege
    target_kind: StepTargetKind
    idempotent: bool
    destroy_intent: bool = False
    long_running: bool = False
    cancellable: bool = True
    pbm_cli_secret_exception: bool = False


class StepResult(BaseModel):
    """Carry a step's typed outcome and a bounded, redacted detail, with no status.

    :param kind: The typed outcome.
    :param detail: A bounded, redacted account of the outcome, or ``None``. Never a
        raw stream, stdout, stderr or executor error string.
    """

    model_config = ConfigDict(frozen=True)

    kind: StepResultKind
    detail: str | None = None

    @classmethod
    def redacted(cls, kind: StepResultKind, detail: str | None = None) -> "StepResult":
        """Build a result whose detail is truncated to :data:`MAX_STEP_RESULT_DETAIL`.

        :param kind: The typed outcome.
        :param detail: The account to bound, or ``None``.
        :return: A result carrying at most :data:`MAX_STEP_RESULT_DETAIL` characters
            of ``detail``.
        """
        bounded = detail[:MAX_STEP_RESULT_DETAIL] if detail is not None else None
        return cls(kind=kind, detail=bounded)


@dataclass(frozen=True, slots=True)
class PrivilegeDecision:
    """Record why a step's privilege was refused before any job was submitted.

    :param required: The privilege the step declared.
    :param measured: The host's measured elevation capability.
    :param no_new_privileges: Whether the refusal is because the unit runs under
        ``NoNewPrivileges=yes``, which no other elevation state can be mistaken for.
    """

    required: StepPrivilege
    measured: AgentElevation
    no_new_privileges: bool


_ELEVATION_GRANTS_ROOT = frozenset(
    {AgentElevation.ROOT, AgentElevation.SUDO_ALL, AgentElevation.SUDO_LIMITED}
)


def authorise_privilege(
    contract: StepContract, measured: AgentElevation
) -> PrivilegeDecision | None:
    """Refuse a step whose declared privilege the measured capability does not grant.

    Fail-closed: a ``ROOT`` step on a host whose measured elevation cannot elevate is
    refused here, before submit, so an undeclared privilege is never exercised. A
    ``NONE`` step is always allowed.

    :param contract: The step's declared contract.
    :param measured: The host's measured elevation capability.
    :return: A :class:`PrivilegeDecision` when the step must be refused, or ``None``
        when it may be dispatched.
    """
    if contract.privilege is StepPrivilege.NONE:
        return None
    if measured in _ELEVATION_GRANTS_ROOT:
        return None
    return PrivilegeDecision(
        required=contract.privilege,
        measured=measured,
        no_new_privileges=measured is AgentElevation.BLOCKED_NO_NEW_PRIVILEGES,
    )


def stop_effect_result(
    contract: StepContract,
    *,
    active: bool,
    main_pid: int,
    clean_exit: bool,
) -> StepResult:
    """Decide a stop step's proof-of-effect result from the unit's own fields.

    A clean-intent stop is proven only when the unit is inactive and exited
    cleanly: a stop recovered through the probe (its task history gone) must not
    report success for an unclean stop. A destroy-intent stop (E0-S6 uninstall) is
    proven once the unit is inactive and its main PID is cleared, so a ``SIGKILL``
    reads down rather than an unclean-stop failure.

    :param contract: The step's declared contract; its ``destroy_intent`` selects
        which proof applies.
    :param active: Whether the unit is still active.
    :param main_pid: The unit's ``MainPID``; ``0`` once it is cleared.
    :param clean_exit: Whether the unit's ``Result`` and ``ExecMainCode`` /
        ``ExecMainStatus`` say it exited cleanly.
    :return: :attr:`StepResultKind.OK` when the stop is proven, else
        :attr:`StepResultKind.FAILED`.
    """
    if active or main_pid != 0:
        return StepResult(kind=StepResultKind.FAILED, detail="unit is still running")
    if contract.destroy_intent or clean_exit:
        return StepResult(kind=StepResultKind.OK)
    return StepResult(kind=StepResultKind.FAILED, detail="unit stopped uncleanly")
