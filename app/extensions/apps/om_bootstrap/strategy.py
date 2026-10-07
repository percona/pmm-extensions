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

"""Define the step/state domain model and the :class:`InstallStrategy` seam.

No persistence and no execution here on purpose: this module is pure planning
logic, unit-testable without a database or a Nomad connection. Two things are
deliberately kept out of it and live in sibling modules instead:

- **Running anything.** :meth:`InstallStrategy.build_step` returns a
  :class:`StepAction` — data describing what a step needs, not an executed result.
  Turning that into a real Nomad job is
  :mod:`~app.extensions.apps.om_bootstrap.dispatch`'s job, so a strategy never touches
  the network or a host.
- **Persisting progress.** :class:`StepRecord`/:class:`HostBootstrapState` are the
  *shape* progress takes, not a database row — they are stored as JSON documents
  on :class:`~app.extensions.apps.om_bootstrap.models.BootstrapRun` (see
  :mod:`~app.extensions.apps.om_bootstrap.persistence`).

The dynamic-progress requirement lives in :meth:`InstallStrategy.plan_steps`: it
returns the ordered step *names* a given spec will run, computed from the spec
rather than fixed on the class, so a run's actual step list (which can differ
between strategies, and within one strategy between specs — e.g. a TLS-enabled
spec adding a certificate step) is known before the first step starts. That is what
lets the UI render a real, run-specific progress list rather than a fixed one four
strategies would each have to fit themselves into.
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.utils.fields import UTCDatetime

__all__ = [
    "BootstrapSpec",
    "HostBootstrapState",
    "InstallMethod",
    "InstallStrategy",
    "MemberConfig",
    "OperatingSystem",
    "StepAction",
    "StepRecord",
    "StepStatus",
]


class StepStatus(StrEnum):
    """Name one step's progress, as the UI renders it."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class InstallMethod(StrEnum):
    """Name the :class:`InstallStrategy` a run uses.

    Only ``PACKAGES`` has an implementation
    (:class:`~app.extensions.apps.om_bootstrap.strategies.packages.PackagesInstallStrategy`).
    ``DOCKER``/``PODMAN`` are named here so :class:`BootstrapSpec` and the API have
    a closed set to switch on before a second strategy exists; requesting either
    today is a 400.
    """

    PACKAGES = "packages"
    DOCKER = "docker"
    PODMAN = "podman"


class OperatingSystem(StrEnum):
    """Target host OS. First implementation supports exactly these two."""

    UBUNTU = "ubuntu"
    ROCKY = "rocky"


#: No whitespace or other control characters. ``replica_set_name``, the run-level
#: ``bind_ip`` and :attr:`MemberConfig.bind_ip` all land in ``mongod.conf``
#: (``_mongod_config``, strategies/packages.py), which already writes them as
#: escaped, quoted YAML scalars. This is a second layer: such a value is refused at
#: create time with a 422 rather than written to the file escaped.
NO_CONTROL_CHARS_PATTERN = r"^[^\s\x00-\x1f]+$"


class MemberConfig(BaseModel):
    """Hold one host's replica-set member settings: its election settings and its bind address.

    Defaults to MongoDB's own for a member (priority 1, votes on, not hidden,
    no delay), so a host a run never names here gets exactly those.

    :param priority: Relative election priority, 0-1000. A member with 0 can
        never become primary.
    :param votes: Whether this member gets a vote in elections.
    :param hidden: Whether this member is hidden from client read preference
        and ``db.hello()``'s own output.
    :param delay_secs: Seconds this member's data intentionally lags the
        primary (``secondaryDelaySecs``). MongoDB requires ``priority`` 0 and
        ``votes`` off whenever this is nonzero.
    :param bind_ip: The interface(s) this member's ``mongod`` listens on,
        overriding the run-level ``bind_ip`` for this host alone. ``None`` keeps
        the run's value. mongod also listens on ``127.0.0.1`` unless the value
        already reaches it. Exists because the safe default is a host's *own*
        address and a three-member set has three different ones, so a single
        run-level value can only be ``0.0.0.0`` or wrong for two of the three.
    :raises ValueError: If ``priority``/``delay_secs`` are out of range, or a
        non-voting, hidden, or delayed member names a nonzero ``priority`` —
        each combination ``rs.initiate`` itself rejects, checked here so a bad
        request fails at create time (422) rather than several steps into a
        run.
    """

    priority: int = Field(default=1, ge=0, le=1000)
    votes: bool = True
    hidden: bool = False
    delay_secs: int = Field(default=0, ge=0)
    bind_ip: str | None = Field(default=None, pattern=NO_CONTROL_CHARS_PATTERN)

    @model_validator(mode="after")
    def _priority_matches_role(self) -> "MemberConfig":
        """Reject a priority MongoDB would refuse for this member's role.

        ``rs.initiate`` rejects a non-voting, hidden, or delayed member unless
        its ``priority`` is exactly 0; each such combination would otherwise
        plan a run that fails at ``rs_initiate``, several steps after every host
        was already provisioned.

        :raises ValueError: If a non-voting, hidden, or delayed member names a
            nonzero ``priority``.
        :return: This config, unchanged.
        """
        if self.priority == 0:
            return self
        if not self.votes:
            raise ValueError("a non-voting member (votes=False) must have priority 0")
        if self.hidden:
            raise ValueError("a hidden member (hidden=True) must have priority 0")
        if self.delay_secs:
            raise ValueError("a delayed member (delay_secs>0) must have priority 0")
        return self


class BootstrapSpec(BaseModel):
    """Hold what one host's bootstrap needs to know to plan and build its steps.

    Deliberately minimal — just enough to make :class:`InstallStrategy` concrete.
    The full Configure-step shape (replica set topology, member roles, TLS mode)
    is a later design pass, not guessed at here.

    :param install_method: Which strategy plans and builds this host's steps.
    :param os: The target host's OS, from ``om_inventory``'s already-collected
        facts — not re-detected here.
    :param mongodb_version: The Percona Server for MongoDB version to install, e.g.
        ``"8.0"``. Selects the ``psmdb-<version>`` repository channel.
    :param replica_set_name: The replica set this host joins. ``rs.initiate`` is a
        run-level step and multi-host orchestration is PMM's stepper's job, not a
        single host's — this field is what one host's own config file needs to
        name.
    :param run_id: The bootstrap run dispatching this host's steps. Required to
        build ``install_package`` and every rollback step, which scope the host's
        ownership marker to this run. ``None`` while only planning a run's steps.
    :param data_path: Where mongod stores its data, e.g. ``/var/lib/mongo``.
    :param log_path: Where mongod writes its log file.
    :param port: The port mongod listens on. ``rs.initiate``'s member list and
        every ``mongosh`` dispatch need this alongside ``mongod.conf`` itself,
        since none of them assume the package's own unconfigured default.
    :param bind_ip: The interface(s) mongod listens on, e.g. ``0.0.0.0``.
    :param member_configs: Per-host replica-set member settings (election
        settings and bind address), keyed by the same host names ``hosts`` (the run's target list) uses.
        A host missing from this mapping — including every host, for a run
        that never sets it at all — gets :class:`MemberConfig`'s own
        defaults.
    """

    install_method: InstallMethod
    os: OperatingSystem
    mongodb_version: str
    replica_set_name: str
    run_id: UUID | None = None
    data_path: str
    log_path: str
    port: int
    bind_ip: str
    member_configs: dict[str, MemberConfig] = {}


class StepAction(BaseModel):
    """Describe what running one step requires, as the execution layer's input.

    Kept dispatch-mechanism-agnostic on purpose: every strategy's steps resolve to
    one of these, so :mod:`~app.extensions.apps.om_bootstrap.dispatch`, which turns it
    into a real Nomad job, has exactly one shape to consume regardless of which
    strategy planned it.

    :param command: The argv to run on the host. An ``["sh", "-c", body]`` argv
        is run as ``body`` directly (see
        :func:`~app.extensions.apps.om_bootstrap.dispatch.build_step_script`).
    :param timeout_s: How long the step may run, in seconds, before it is killed
        and fails.
    """

    command: list[str]
    timeout_s: int = 120


class StepRecord(BaseModel):
    """Record one step's progress, for a host or for a run.

    The same shape serves both :attr:`HostBootstrapState.steps` (per-host) and
    :attr:`~app.extensions.apps.om_bootstrap.models.BootstrapRun.run_steps` (run-level,
    e.g. ``rs_initiate`` — see :meth:`InstallStrategy.plan_run_steps`): neither
    context needs a field the other doesn't, so one type covers both rather than
    two near-duplicates.

    :param name: One of the names :meth:`InstallStrategy.plan_steps` (or
        :meth:`InstallStrategy.plan_run_steps`, or
        :meth:`InstallStrategy.plan_rollback_steps`)
        returned for this spec — not a fixed enum, since the step list itself is
        per-strategy and per-spec (see the module docstring).
    :param status: This step's current status.
    :param started_at: When the execution layer began this step, if it has.
    :param finished_at: When this step reached a terminal status, if it has.
    :param detail: A human-readable outcome — an error message on
        :attr:`StepStatus.FAILED`, or ``None`` while pending/running.
    :param task_history_id: The Tasks API history id backing this step's dispatch,
        while it is running — the execution layer's own bookkeeping, not a
        strategy concern. Still just data describing progress, so it lives here
        rather than in a separate persisted-only sibling type: one shape for
        planning, persistence, and API responses alike.
    :param attempt_count: How many times this step has been dispatched.
        Incremented on every dispatch, including the first — PMM's stepper reads
        this to enforce its decided retry policy (retry, then roll back) without
        needing a counter of its own, which would be lost on a leader failover.
        ``om_bootstrap`` only ever records the fact that a dispatch happened;
        deciding whether *another* one should is the stepper's call, not this
        field's.
    """

    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: UTCDatetime | None = None
    finished_at: UTCDatetime | None = None
    detail: str | None = None
    task_history_id: int | None = None
    attempt_count: int = 0


class HostBootstrapState(BaseModel):
    """Track one host's progress through its planned steps.

    :param host: The node name being bootstrapped.
    :param steps: This host's steps, in the order :meth:`InstallStrategy.plan_steps`
        returned them — the full list is known before the first one starts.
    :param rollback_steps: This host's teardown steps, in the order
        :meth:`InstallStrategy.plan_rollback_steps` returned them — planned
        up front alongside ``steps`` so a fresh run already shows what rollback
        would consist of, even before anything fails. Every entry stays
        :attr:`StepStatus.PENDING` unless the stepper actually decides to roll
        this host back.
    :param finalize_steps: This host's post-coordination steps, in the order
        :meth:`InstallStrategy.plan_finalize_steps` returned them — planned up
        front alongside ``steps``, but not dispatched until every run-level step
        has succeeded (PMM's stepper's call, mirroring how it gates run-level
        steps on every host's ``steps`` first — see
        :meth:`InstallStrategy.plan_finalize_steps`'s own docstring for why this
        ordering exists at all).
    """

    host: str
    steps: list[StepRecord]
    rollback_steps: list[StepRecord] = []
    finalize_steps: list[StepRecord] = []

    @property
    def status(self) -> StepStatus:
        """Derive this host's overall status from its steps.

        Never stored directly — a host's status is always a projection of its
        steps, so the two cannot drift apart the way an independently-set field
        could.

        :return: :attr:`StepStatus.FAILED` if any step failed,
            :attr:`StepStatus.RUNNING` if any step is running or still pending
            with an earlier step done, :attr:`StepStatus.SUCCEEDED` once every
            step has succeeded or been skipped, else :attr:`StepStatus.PENDING`.
            ``PENDING`` for an empty ``steps`` list too — ``all()`` over an
            empty sequence is vacuously true, which would otherwise report a
            host with nothing planned as already done.
        """
        statuses = [step.status for step in self.steps]
        if StepStatus.FAILED in statuses:
            return StepStatus.FAILED
        if StepStatus.RUNNING in statuses:
            return StepStatus.RUNNING
        if statuses and all(
            status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for status in statuses
        ):
            return StepStatus.SUCCEEDED
        if any(status != StepStatus.PENDING for status in statuses):
            return StepStatus.RUNNING
        return StepStatus.PENDING


@runtime_checkable
class InstallStrategy(Protocol):
    """Define one way to get MongoDB installed and configured on a host.

    A strategy owns *how*; the state machine (PMM's ``om`` service, driving as the
    HA-leader-only stepper) owns *when* and *whether the run as a whole should
    continue*, and ``om_bootstrap``'s API persists the progress — neither knows
    or cares which strategy is running, only that every strategy answers these
    questions the same way. This is the "abstracted pre-check/install/configure/
    test" requirement.

    Four parallel step lists, not one:

    - **Per-host** (:meth:`plan_steps`/:meth:`build_step`): everything a single
      host's own install needs, run independently per host.
    - **Run-level** (:meth:`plan_run_steps`/:meth:`build_run_step`): coordination
      that only makes sense once, for the whole run — ``rs.initiate`` and
      creating PMM's monitoring user both need every member up first, and both
      only need to run *once* (MongoDB replicates a created user to every member
      automatically), not once per host. The stepper dispatches these to one
      designated host from ``hosts`` (index 0 by convention — see
      :meth:`build_run_step`'s ``hosts`` parameter) once every per-host step has
      succeeded.
    - **Finalize** (:meth:`plan_finalize_steps`/:meth:`build_finalize_step`):
      per-host work that has to happen *after* run-level coordination has
      already succeeded, not before — enabling MongoDB authorization is the
      motivating case: creating the first user reliably requires authorization
      to still be *off* everywhere at the time, because MongoDB's localhost
      exception is unreliable once a replica set already has more than one
      member (confirmed against a real multi-member run, not a theoretical
      concern — once any privileged op on it fails once, the exception closes
      permanently for that mongod's whole lifetime, not just for one
      connection). So ``configure_mongod`` never enables authorization, and each
      host only turns it on for itself once ``create_pmm_monitoring_user`` has
      actually succeeded. The stepper dispatches these once every run-level step
      has succeeded — the same per-host shape as :meth:`plan_steps`, just
      running after run-level steps instead of before them.
    - **Rollback** (:meth:`plan_rollback_steps`/:meth:`build_rollback_step`):
      one host's teardown, planned up front alongside its forward steps so a
      fresh run already shows what rolling back would do (the decided
      partial-failure policy), even before anything fails.

    ``build_step`` and ``build_run_step`` both take a ``params`` mapping for the
    one thing a strategy cannot itself supply: per-run secrets (a keyFile's
    content, a generated monitoring-user password). These live durably in PMM's
    encrypted Postgres, not PMM Extensions' —
    ``params`` is how the stepper hands one to a single dispatch, transiently,
    without ``om_bootstrap`` ever persisting the plaintext in
    :class:`StepRecord`/:class:`~app.extensions.apps.om_bootstrap.models.BootstrapRun`.
    """

    def plan_steps(self, spec: BootstrapSpec) -> list[str]:
        """Return this strategy's ordered per-host step names for ``spec``.

        Computed from ``spec`` rather than fixed, so the step list a run actually
        follows can vary with what the spec asks for (see the module docstring).
        Called once, before the first step starts.

        :param spec: The host's bootstrap spec.
        :return: Step names, in execution order.
        """

    def build_step(
        self,
        step_name: str,
        host: str,
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,
    ) -> StepAction:
        """Build the action for one per-host step ``plan_steps`` named.

        Pure: given the same arguments, always returns the same action. Running it
        is the execution layer's job.

        :param step_name: One of the names this strategy's own :meth:`plan_steps`
            returned for ``spec``.
        :param host: The node name being bootstrapped.
        :param spec: The host's bootstrap spec.
        :param params: Per-dispatch values a step needs but cannot compute itself
            — see the class docstring. ``None`` for a step that needs none.
        :return: What the execution layer needs to run this step.
        """

    def plan_run_steps(self, spec: BootstrapSpec) -> list[str]:
        """Return this strategy's ordered run-level step names for ``spec``.

        See the class docstring's "run-level" bullet. Called once, alongside
        :meth:`plan_steps`, before any host is touched.

        :param spec: The run's bootstrap spec.
        :return: Step names, in execution order.
        """

    def build_run_step(
        self,
        step_name: str,
        hosts: list[str],
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,
    ) -> StepAction:
        """Build the action for one run-level step ``plan_run_steps`` named.

        :param step_name: One of the names this strategy's own
            :meth:`plan_run_steps` returned for ``spec``.
        :param hosts: Every host in this run, in the order the run was created
            with. Index 0 is the designated target this action runs *on* (e.g.
            ``rs.initiate``'s seed member) — the same host every run-level step
            targets, so a strategy needing to name every member (``rs.initiate``)
            still has the full list to do so.
        :param spec: The run's bootstrap spec.
        :param params: See :meth:`build_step`.
        :return: What the execution layer needs to run this step.
        """
        ...

    def plan_finalize_steps(self, spec: BootstrapSpec) -> list[str]:
        """Return this strategy's ordered per-host finalize step names for ``spec``.

        See the class docstring's "finalize" bullet. Called once, alongside
        :meth:`plan_steps`, before any host is touched — planned up front the
        same way rollback steps are, even though the stepper will not dispatch
        any of them until every run-level step has succeeded.

        :param spec: The host's bootstrap spec.
        :return: Step names, in the order finalize should apply them.
        """
        ...

    def build_finalize_step(
        self,
        step_name: str,
        host: str,
        spec: BootstrapSpec,
        params: dict[str, str] | None = None,
    ) -> StepAction:
        """Build the action for one finalize step ``plan_finalize_steps`` named.

        :param step_name: One of the names this strategy's own
            :meth:`plan_finalize_steps` returned for ``spec``.
        :param host: The node name being finalized.
        :param spec: The host's bootstrap spec.
        :param params: See :meth:`build_step`. ``None`` for a step that needs none.
        :return: What the execution layer needs to run this step.
        """

    def plan_rollback_steps(self, spec: BootstrapSpec) -> list[str]:
        """Return this strategy's ordered per-host rollback step names for ``spec``.

        See the class docstring's "rollback" bullet. Called once, alongside
        :meth:`plan_steps`, before any host is touched.

        :param spec: The host's bootstrap spec.
        :return: Step names, in the order rollback should apply them.
        """

    def build_rollback_step(
        self, step_name: str, host: str, spec: BootstrapSpec
    ) -> StepAction:
        """Build the action for one rollback step ``plan_rollback_steps`` named.

        :param step_name: One of the names this strategy's own
            :meth:`plan_rollback_steps` returned for ``spec``.
        :param host: The node name being rolled back.
        :param spec: The host's bootstrap spec.
        :return: What the execution layer needs to run this step.
        """
