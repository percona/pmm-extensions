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

"""Read-only audit of ``data['_form']`` stamps against each app's create model.

A stamp is the create body as the form looked when the task was saved, and the
edit form reloads it verbatim — so a form gate added afterwards turns a saved
task into one that cannot be re-saved until an operator changes it. This module
reports that population without touching it: for every app that declares a
backfill entry it validates each active task's stamp against the model that
app's create and update routes enforce, and names the tasks a save would now
reject. Deactivated rows are out of scope, as they are for the backfill.

The report goes to stdout, because the report *is* the output; progress goes to
the log. It never writes, and it never prints the input pydantic records against
an error: a form body holds GPG recipients and key-file paths. Findings carry
field locations and message text, so a model-level validator must not
interpolate the value it rejects into its own message.

A task carrying no stamp is counted ``unstamped`` and never validated, so the
``rejected`` total answers "how many saved tasks would a save now reject?" only
once ``form_backfill`` has run: until then a legacy task sits outside the
population the count covers.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.extensions.apps.framework.form_backfill_registry import FormBackfillEntry
    from app.extensions.apps.framework.form_dsl import AppFormModel

from app.extensions.apps.framework.apps import TaskExecutionApp
from app.extensions.apps.framework.form_backfill_registry import (
    add_owner_and_verbose_arguments,
    collect_form_backfill_entries,
)
from app.extensions.apps.framework.registry import get_app_registry
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.tasks.crud import ACTIVE_TASK_BATCH_SIZE, TaskManager
from app.tasks.db import get_async_session_maker
from app.tasks.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StampFinding:
    """Name one stamped task whose form the create model no longer accepts.

    :param task_id: The task's primary key, or ``None`` for an unpersisted row.
    :param task_name: The task's name, the handle an operator searches by.
    :param fields: Dotted locations of the field-level failures.
    :param reasons: Messages of the model-level failures, which is where the
        conditional rules report.
    """

    task_id: int | None
    task_name: str
    fields: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass
class AppAuditStats:
    """Hold one app's audit outcome counters.

    :param app_key: The audited app's registry key.
    :param owner: The task owner whose rows were listed.
    :param valid: Stamps the create model still accepts.
    :param unstamped: Tasks carrying no stamp, which the backfill owns.
    :param unreadable: Tasks whose stamp is not a mapping.
    :param errored: Tasks whose validation raised something other than a
        ``ValidationError``, so the audit reached no verdict for them.
    :param findings: One entry per stamp the create model rejects.
    """

    app_key: str
    owner: str
    valid: int = 0
    unstamped: int = 0
    unreadable: int = 0
    errored: int = 0
    findings: list[StampFinding] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        """Return the number of stamps the create model rejects."""
        return len(self.findings)

    @property
    def scanned(self) -> int:
        """Return the number of tasks the audit classified."""
        return (
            self.valid + self.unstamped + self.unreadable + self.errored + self.rejected
        )


@dataclass
class AuditSummary:
    """Aggregate every audited app's counters.

    :param apps: Per-app statistics, in collection order.
    """

    apps: list[AppAuditStats] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        """Return the total number of rejected stamps across all apps."""
        return sum(app.rejected for app in self.apps)

    @property
    def scanned(self) -> int:
        """Return the total number of tasks classified across all apps."""
        return sum(app.scanned for app in self.apps)

    @property
    def unstamped(self) -> int:
        """Return the total number of tasks carrying no stamp across all apps."""
        return sum(app.unstamped for app in self.apps)

    @property
    def unreadable(self) -> int:
        """Return the total number of unreadable stamps across all apps."""
        return sum(app.unreadable for app in self.apps)

    @property
    def errored(self) -> int:
        """Return the total number of tasks left without a verdict."""
        return sum(app.errored for app in self.apps)


@dataclass(frozen=True, slots=True)
class _TaskAuditOutcome:
    """Pair one task's outcome label with its finding, if any.

    :param label: The :class:`AppAuditStats` counter to increment. Read only
        when ``finding`` is ``None``: ``rejected`` is a read-only property
        derived from the findings, so it names no assignable counter.
    :param finding: The finding to report, or ``None``.
    """

    label: Literal["valid", "unstamped", "unreadable"] = "valid"
    finding: StampFinding | None = None


def _finding_from_error(task: Task, exc: ValidationError) -> StampFinding:
    """Build a value-free finding from a stamp's validation errors.

    :param task: The stamped task row.
    :param exc: The failure the create model raised.
    :return: The finding to report for ``task``.
    """
    fields: list[str] = []
    reasons: list[str] = []
    for error in exc.errors(include_input=False):
        location = error["loc"]
        # An empty location is pydantic's whole-model scope, which is where every
        # conditional rule reports.
        if location:
            fields.append(".".join(str(part) for part in location))
        else:
            reasons.append(error["msg"])
    return StampFinding(
        task_id=task.id,
        task_name=task.name,
        fields=tuple(fields),
        reasons=tuple(reasons),
    )


def _route_create_model(entry: FormBackfillEntry) -> type[AppFormModel]:
    """Return the model an app's create and update routes enforce.

    Read off the app definition rather than the backfill entry: an app whose
    form tightened after tasks were saved registers a laxer ``create_model`` so
    those tasks still stamp, and validating against that model would report the
    population a save now rejects as valid.

    :param entry: The declaring app's backfill entry.
    :return: The app's own create model, or the entry's when the app builds its
        schema from a ``schema=`` declaration instead of a model.
    """
    app = get_app_registry().get(entry.app_key)
    if isinstance(app, TaskExecutionApp) and app.create_model is not None:
        return app.create_model
    return entry.create_model


def _audit_single_task(task: Task, model: type[AppFormModel]) -> _TaskAuditOutcome:
    """Classify one task's stamp against the model its routes enforce.

    Validates with ``extra="ignore"``: the edit form submits only the fields the
    schema declares, so a key the form has since retired is not part of the save
    whose outcome this predicts.

    :param task: The task row to classify.
    :param model: The create model to validate the stamp against.
    :return: The outcome label and, for a rejected stamp, its finding.
    """
    stored_form = task.data.get(RESERVED_FORM_KEY)
    if stored_form is None:
        return _TaskAuditOutcome("unstamped")
    if not isinstance(stored_form, dict):
        return _TaskAuditOutcome("unreadable")

    try:
        model.model_validate(stored_form, extra="ignore")
    except ValidationError as exc:
        return _TaskAuditOutcome(finding=_finding_from_error(task, exc))
    return _TaskAuditOutcome("valid")


async def _audit_app(
    session: AsyncSession,
    entry: FormBackfillEntry,
    *,
    log: logging.Logger,
    batch_size: int = ACTIVE_TASK_BATCH_SIZE,
) -> AppAuditStats:
    """Audit every active task of a single app.

    Reads the population in keyset batches rather than in one list: this runs
    against installations whose task table is the reason the audit exists, and a
    stamp is a whole create body, so holding every row at once is what would
    stop the report being produced at all.

    :param session: The tasks database session; only read from.
    :param entry: The declaring app's backfill entry.
    :param log: Logger for the per-app summary lines.
    :param batch_size: Rows to read per query.
    :return: Per-app outcome counters and findings.
    """
    stats = AppAuditStats(app_key=entry.app_key, owner=entry.owner)
    model = _route_create_model(entry)
    log.info(
        "[%s] auditing active task(s) for owner %s against %s in batches of %s",
        entry.app_key,
        entry.owner,
        model.__name__,
        batch_size,
    )

    batches = TaskManager.iter_active_batches(
        session, owner=entry.owner, batch_size=batch_size
    )
    async for batch in batches:
        for task in batch:
            # One unclassifiable row must not cost the run its whole report, which
            # is the only record of how large the affected population is.
            try:
                outcome = _audit_single_task(task, model)
            except Exception:
                log.exception(
                    "[%s] %s: auditing the stamp raised; counting it unresolved",
                    entry.app_key,
                    task.name,
                )
                stats.errored += 1
                continue
            if outcome.finding is None:
                setattr(stats, outcome.label, getattr(stats, outcome.label) + 1)
                continue
            stats.findings.append(outcome.finding)
        log.debug("[%s] classified %s task(s) so far", entry.app_key, stats.scanned)

    log.info(
        "[%s] rejected=%s valid=%s unstamped=%s unreadable=%s errored=%s",
        entry.app_key,
        stats.rejected,
        stats.valid,
        stats.unstamped,
        stats.unreadable,
        stats.errored,
    )
    return stats


async def run_audit(
    *,
    owners: Sequence[str] | None = None,
    log: logging.Logger | None = None,
) -> AuditSummary:
    """Audit the stamps of all or selected in-scope apps.

    :param owners: When set, limit the audit to these task owners; otherwise
        every app declaring a backfill entry is audited.
    :param log: Logger for progress and findings; defaults to this module's logger.
    :return: Aggregate counters and findings for the run.
    """
    active_log = log or logger
    entries = collect_form_backfill_entries(owners=owners)
    summary = AuditSummary()

    if not entries:
        active_log.warning("No in-scope apps matched the requested owner filter")
        return summary

    session_maker = get_async_session_maker()
    async with session_maker() as session:
        for entry in entries:
            summary.apps.append(await _audit_app(session, entry, log=active_log))

    active_log.info(
        "Audit complete: scanned=%s rejected=%s unreadable=%s errored=%s",
        summary.scanned,
        summary.rejected,
        summary.unreadable,
        summary.errored,
    )
    return summary


def format_summary(summary: AuditSummary) -> str:
    """Render an audit summary as the operator-facing report.

    :param summary: The counters and findings to render.
    :return: One line per app, one indented line per finding, then a total,
        qualified when an unstamped task leaves that total a lower bound.
    """
    lines: list[str] = []
    for app in summary.apps:
        lines.append(
            f"{app.app_key} ({app.owner}): scanned={app.scanned} "
            f"rejected={app.rejected} valid={app.valid} "
            f"unstamped={app.unstamped} unreadable={app.unreadable} "
            f"errored={app.errored}"
        )
        lines.extend(
            f"  task {finding.task_id} {finding.task_name!r}: "
            f"{'; '.join(finding.reasons) or 'field validation'} "
            f"[fields: {', '.join(finding.fields) or '-'}]"
            for finding in app.findings
        )
    lines.append(
        f"Total: scanned={summary.scanned} rejected={summary.rejected} "
        f"unreadable={summary.unreadable} errored={summary.errored}"
    )
    if summary.unstamped:
        lines.append(
            f"Note: {summary.unstamped} task(s) carry no stamp and were not "
            "validated, so rejected is a lower bound until form_backfill has run."
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser for the audit entry point.

    :return: A parser exposing ``--owner`` and ``--verbose``.
    """
    entries = collect_form_backfill_entries()
    valid_owners = frozenset(entry.owner for entry in entries)
    if entries:
        app_keys = ", ".join(entry.app_key for entry in entries)
        description = (
            "Report saved tasks whose data['_form'] stamp the app's own create "
            f"form would now reject ({app_keys}). Read-only."
        )
    else:
        description = (
            "Report saved tasks whose data['_form'] stamp their create form would "
            "now reject. No activated app declares a form backfill."
        )
    parser = argparse.ArgumentParser(description=description)
    add_owner_and_verbose_arguments(parser, valid_owners=valid_owners, subject="audit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stamp audit from the command line.

    :param argv: Optional argument vector; defaults to ``sys.argv[1:]``.
    :return: Exit code ``0``; a finding is a report, not a failure.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    # ``basicConfig`` returns early once a handler exists, and importing this
    # module installs the app's logging configuration, so the progress lines need
    # the level set on the emitting logger to clear the root's WARNING.
    logger.setLevel(level)
    summary = asyncio.run(run_audit(owners=args.owners))
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
