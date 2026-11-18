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

"""Define the canonical Nomad task/step names used inside Nomad job specs.

This module is a pure leaf: it imports nothing beyond ``enum`` so callers can
name a step without constructing the Nomad executor. Wire values are spelled
literally (no ``auto()``) so the Nomad task ``Name`` is never implicit.
"""

from enum import StrEnum


class NomadStep(StrEnum):
    """Represent Nomad task ``Name`` values used by PMM Extensions job-spec templates.

    ``STEP1`` is legacy — only historical task logs / the frozen Alembic
    migration still emit it. Do not wire new job specs to ``step1``.
    """

    RUN_SCRIPT = "run-script"
    PREPARE_ENV = "prepare-env"
    CLEAN_UP = "clean-up"
    CHECK_STALENESS = "check-staleness"
    CHECK_LAUNCHABLE = "check-launchable"
    LOG_CAPTURE_HOLD = "log-capture-hold"
    STEP1 = "step1"

    @classmethod
    def anonymized(cls) -> frozenset[NomadStep]:
        """Return the steps whose log output is PII-anonymized.

        :return: The frozen set of steps classified as anonymized.
        """
        return frozenset(
            step for step, anonymize in NOMAD_STEP_ANONYMIZE.items() if anonymize
        )

    @classmethod
    def is_persistable(cls, step: str) -> bool:
        """Return whether PMM Extensions persists ``step``'s streams and gates release on it.

        Tests against :data:`NON_PERSISTABLE_STEPS`, which is also what the
        capture-status aggregate filters on in SQL. Sharing one definition is
        what keeps a second non-producing step from being excluded here and
        silently counted there.

        Takes a raw name rather than a member, and admits every name outside
        that set: a Nomad allocation's task states are keyed by whatever the job
        spec declared, so a step this enum does not know is an unrecognized
        *producer* — excluding it would silently drop its output from the live
        viewer and leave its capture unclassified.

        :param step: The Nomad task (step) name to classify.
        :return: ``True`` for every step PMM Extensions treats as a log producer.
        """
        return step not in NON_PERSISTABLE_STEPS


#: Allocation-relative output-files directory of every job spec that pins its
#: :attr:`NomadStep.RUN_SCRIPT` task's ``work_dir`` to
#: ``${NOMAD_TASK_DIR}/output_files`` (``run-python``, ``exec-artifact``,
#: ``exec-python-artifact``). It is the
#: :attr:`~app.tasks.models.TaskBase.output_files_path` those specs run under,
#: so a payload's working directory and the path PMM Extensions reads its files back from
#: are the same place. ``run-command`` pins no ``work_dir`` and so has no
#: output-files path. Derived from the step name rather than spelled out, so
#: renaming the step cannot leave the path behind; ``local`` is
#: ``${NOMAD_TASK_DIR}``.
RUN_SCRIPT_OUTPUT_FILES_PATH = f"{NomadStep.RUN_SCRIPT}/local/output_files"

#: Steps that emit no task output of their own. ``LOG_CAPTURE_HOLD`` exists only
#: to keep the allocation readable, so draining it would gate release on a stream
#: that only ends when the hold itself does. Read by both
#: :meth:`NomadStep.is_persistable` and the capture-status aggregate's SQL
#: filter, so the two cannot diverge.
NON_PERSISTABLE_STEPS: frozenset[str] = frozenset({NomadStep.LOG_CAPTURE_HOLD})

NOMAD_STEP_ANONYMIZE: dict[NomadStep, bool] = {
    NomadStep.RUN_SCRIPT: True,
    NomadStep.PREPARE_ENV: False,
    NomadStep.CLEAN_UP: False,
    NomadStep.CHECK_STALENESS: False,
    NomadStep.CHECK_LAUNCHABLE: False,
    NomadStep.LOG_CAPTURE_HOLD: False,
    NomadStep.STEP1: True,
}

#: Exit code :attr:`NomadStep.CHECK_LAUNCHABLE` aborts with when a command in
#: the launch chain does not resolve on the executor node, mapped to
#: ``UNLAUNCHABLE`` by the Nomad executor the way 75 is mapped to ``STALE``.
#: ``EX_CONFIG`` from ``sysexits.h``: the allocation was misconfigured for the
#: node it landed on. Shared by the seeded job spec that emits it and the
#: executor that reads it back, so the two cannot drift apart.
LAUNCH_CHECK_EXIT_CODE = 78

#: Hold duration a job dispatched without the meta key falls back to, in
#: seconds. Spans three 30 s sync cadences so a beat delayed by worker backlog
#: still lands inside the hold.
LOG_CAPTURE_HOLD_DEFAULT_SECONDS = 90
