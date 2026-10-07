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

"""One-time backfill of ``data['_form']`` for legacy framework-migrated task apps.

The orchestrator enumerates the entries activated apps declare through
:func:`~app.extensions.apps.framework.form_backfill_registry.collect_form_backfill_entries`,
finds tasks owned by each that lack the reserved form key, reconstructs a
create-model-shaped body, validates it, and stamps ``data['_form']`` on success. Each
per-task step is isolated so one failure never aborts the batch.

A task that already carries the stamp is left alone unless its app declares a
:data:`~app.extensions.apps.framework.form_backfill_registry.StampRepairer`. A stamp is a
snapshot of the create body as the form looked when the task was saved, so a field
the form gained afterwards is missing from every older stamp and the edit form fills
it from the schema default — which for a field that decides what the task *does* is
not what the task actually runs. A repairer supplies the value the stored config
implies; re-runs are idempotent because a repairer returns ``None`` once there is
nothing left to add.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy.orm.attributes import flag_modified

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

from app.extensions.apps.framework.form_backfill_inventory import (
    load_schema_id_lookup,
    load_service_id_lookup,
)
from app.extensions.apps.framework.form_backfill_registry import (
    add_owner_and_verbose_arguments,
    collect_form_backfill_entries,
    FormBackfillContext,
    FormBackfillEntry,
)
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY, stamp_form_input
from app.inventory.db import get_async_session_maker as get_inventory_session_maker
from app.tasks.crud import TaskManager
from app.tasks.db import get_async_session_maker
from app.tasks.models import Task, TaskWrite

logger = logging.getLogger(__name__)


@dataclass
class AppBackfillStats:
    """Represent backfill outcome counters for a single in-scope app.

    :param app_key: The declaring app's registry key.
    :param owner: The task owner filter used when listing tasks.
    :param stamped: Tasks that received a new ``data['_form']`` stamp.
    :param repaired: Tasks whose existing ``data['_form']`` was brought up to the
        current ``create_model`` by the app's stamp repairer.
    :param skipped_existing: Tasks that already had a ``data['_form']`` needing no
        repair.
    :param skipped_unreconstructable: Tasks whose reconstructor returned ``None``.
    :param skipped_invalid: Tasks whose reconstructed body failed ``create_model`` validation.
    :param skipped_error: Tasks whose reconstructor, stamp step, or persistence raised.
    """

    app_key: str
    owner: str
    stamped: int = 0
    repaired: int = 0
    skipped_existing: int = 0
    skipped_unreconstructable: int = 0
    skipped_invalid: int = 0
    skipped_error: int = 0


@dataclass
class BackfillSummary:
    """Represent the aggregate outcome of a full backfill run.

    :param apps: Per-app outcome counters.
    :param dry_run: Whether the run operated in dry-run mode (no database writes).
    """

    apps: list[AppBackfillStats] = field(default_factory=list)
    dry_run: bool = False

    @property
    def stamped(self) -> int:
        """Return the total number of tasks stamped across all apps."""
        return sum(app.stamped for app in self.apps)

    @property
    def repaired(self) -> int:
        """Return the total number of existing stamps repaired across all apps."""
        return sum(app.repaired for app in self.apps)

    @property
    def skipped_existing(self) -> int:
        """Return tasks skipped because ``data['_form']`` was already present."""
        return sum(app.skipped_existing for app in self.apps)

    @property
    def skipped_unreconstructable(self) -> int:
        """Return tasks whose reconstructor could not produce a body."""
        return sum(app.skipped_unreconstructable for app in self.apps)

    @property
    def skipped_invalid(self) -> int:
        """Return tasks whose reconstructed body failed validation."""
        return sum(app.skipped_invalid for app in self.apps)

    @property
    def skipped_error(self) -> int:
        """Return tasks that raised during reconstruction or stamping."""
        return sum(app.skipped_error for app in self.apps)


@dataclass(frozen=True)
class _TaskBackfillOutcome:
    """Hold the result of the reconstruct → validate → stamp pipeline for one task.

    :param label: The outcome counter name (for example ``"stamped"``).
    :param stamped_data: The stamped task ``data`` dict to persist, or ``None`` when
        the task was skipped.
    """

    label: str
    stamped_data: dict[str, Any] | None = None


def _task_write_from_task(task: Task, data: dict[str, Any]) -> TaskWrite:
    """Build the ``TaskWrite`` envelope that carries ``data`` through stamping.

    The envelope is a carrier, not an update body: :func:`stamp_form_input` writes
    only to ``write.data``, and that dict — not the envelope — is what the caller
    persists. The two hook-path fields are therefore left unset rather than copied
    off the row, so a stored path predating the ``TaskWrite`` allow-list cannot
    fail validation here and cost an otherwise eligible row its backfill.

    :param task: The persisted task row.
    :param data: The ``data`` dict to carry on the write (including any stamp).
    :return: A ``TaskWrite`` carrying ``data`` for stamping.
    """
    return TaskWrite(
        name=task.name,
        data=data,
        backend=task.backend,
        owner=task.owner,
        is_template=task.is_template,
        protected=task.protected,
        alert_on_fail=task.alert_on_fail,
        output_files_path=task.output_files_path,
        anonymize_mask=task.anonymize_mask,
    )


async def _persist_stamped_form(
    session: AsyncSession,
    task: Task,
    stamped_data: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    """Stage ``stamped_data`` on the task without changing audit attribution.

    Only ``data`` is modified; ``last_updated_by`` is left untouched and
    ``updated_at`` is pinned to its pre-flush value so the ORM UPDATE does not
    apply the column's ``onupdate`` default.

    Flush the update to the database session only; the caller must
    :meth:`~sqlalchemy.ext.asyncio.AsyncSession.commit` after each successful task
    so a later failure's :meth:`~sqlalchemy.ext.asyncio.AsyncSession.rollback` cannot
    discard stamps from earlier tasks in the same app batch.

    :param session: The tasks database session.
    :param task: The task row being updated.
    :param stamped_data: The task ``data`` dict including the new ``_form`` key.
    :param dry_run: When ``True``, skip staging any database changes.
    """
    if dry_run:
        return
    original_updated_at = task.updated_at
    task.data = stamped_data
    flag_modified(task, "data")
    task.updated_at = original_updated_at
    flag_modified(task, "updated_at")
    session.add(task)
    await session.flush()


def _backfill_single_task(
    task: Task,
    entry: FormBackfillEntry,
    ctx: FormBackfillContext,
) -> _TaskBackfillOutcome:
    """Route a task to the pipeline its ``data`` calls for, or skip it.

    An already-stamped task goes to the repair pipeline, which hands the stored
    stamp to the app's repairer and re-validates whatever comes back. Anything
    else goes through reconstruct → validate → stamp, which needs an inventory
    lookup and the app's own reconstruction to reach a form at all.

    :param task: The task row to backfill.
    :param entry: The declaring app's backfill entry.
    :param ctx: Shared backfill context.
    :return: The outcome label and, for a stamped or repaired form, the ``data``
        dict to persist.
    """
    if RESERVED_FORM_KEY in task.data:
        return _repair_existing_stamp(task, entry, ctx)

    if ctx.service_lookup is None:
        ctx.log.info(
            "[%s] %s: no inventory service lookup; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_unreconstructable")

    return _reconstruct_validate_stamp(task, entry, ctx)


def _repair_existing_stamp(
    task: Task,
    entry: FormBackfillEntry,
    ctx: FormBackfillContext,
) -> _TaskBackfillOutcome:
    """Bring an already-stamped task's ``data['_form']`` up to its create model.

    Runs only the app's declared repairer, never the reconstructor: the stored
    stamp is the authoritative record of what the operator submitted, so a repair
    fills the gaps the form has since grown rather than re-deriving the whole body
    from the task's config.

    :param task: The stamped task row.
    :param entry: The declaring app's backfill entry.
    :param ctx: Shared backfill context.
    :return: The outcome label and optional repaired ``data`` dict to persist.
    """
    stored_form = task.data[RESERVED_FORM_KEY]
    if entry.stamp_repairer is None or not isinstance(stored_form, dict):
        ctx.log.debug(
            "[%s] %s: already has %r; skipping",
            entry.app_key,
            task.name,
            RESERVED_FORM_KEY,
        )
        return _TaskBackfillOutcome("skipped_existing")

    try:
        repaired_form = entry.stamp_repairer(deepcopy(stored_form), task, ctx)
    except Exception:
        ctx.log.exception(
            "[%s] %s: stamp repairer raised; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_error")

    if repaired_form is None:
        ctx.log.debug(
            "[%s] %s: %r needs no repair; skipping",
            entry.app_key,
            task.name,
            RESERVED_FORM_KEY,
        )
        return _TaskBackfillOutcome("skipped_existing")

    try:
        validated_form = entry.create_model.model_validate(repaired_form)
    except ValidationError as exc:
        # Without ``include_input``: an error carries the value that failed, and a
        # form body can hold a GPG recipient or a key-file path.
        ctx.log.info(
            "[%s] %s: repaired form failed validation; leaving %r as it is: %s",
            entry.app_key,
            task.name,
            RESERVED_FORM_KEY,
            exc.errors(include_input=False),
        )
        return _TaskBackfillOutcome("skipped_invalid")

    repaired_data = deepcopy(task.data)
    # ``stamp_form_input`` refuses to overwrite an existing stamp, which is what
    # guards the create path against a spec builder populating it.
    repaired_data.pop(RESERVED_FORM_KEY)
    write = _task_write_from_task(task, repaired_data)

    try:
        stamp_form_input(write, validated_form)
    except Exception:
        ctx.log.exception(
            "[%s] %s: stamp_form_input raised while repairing; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_error")

    ctx.log.info(
        "[%s] %s: %s %r",
        entry.app_key,
        task.name,
        "dry-run would repair" if ctx.dry_run else "repaired",
        RESERVED_FORM_KEY,
    )
    return _TaskBackfillOutcome("repaired", write.data)


def _reconstruct_validate_stamp(
    task: Task,
    entry: FormBackfillEntry,
    ctx: FormBackfillContext,
) -> _TaskBackfillOutcome:
    """Reconstruct, validate, and stamp ``data['_form']`` for an eligible task.

    :param task: The legacy task row to backfill.
    :param entry: The declaring app's backfill entry.
    :param ctx: Shared backfill context.
    :return: The outcome label and optional stamped ``data`` dict to persist.
    """
    try:
        raw_form = entry.reconstructor(task, ctx)
    except Exception:
        ctx.log.exception(
            "[%s] %s: reconstructor raised; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_error")

    if raw_form is None:
        ctx.log.info(
            "[%s] %s: could not reconstruct form; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_unreconstructable")

    try:
        validated_form = entry.create_model.model_validate(raw_form)
    except ValidationError as exc:
        # Without ``include_input``: an error carries the value that failed, and a
        # form body can hold a GPG recipient or a key-file path.
        ctx.log.info(
            "[%s] %s: reconstructed form failed validation; skipping: %s",
            entry.app_key,
            task.name,
            exc.errors(include_input=False),
        )
        return _TaskBackfillOutcome("skipped_invalid")

    stamped_data = deepcopy(task.data)
    write = _task_write_from_task(task, stamped_data)

    try:
        stamp_form_input(write, validated_form)
    except Exception:
        ctx.log.exception(
            "[%s] %s: stamp_form_input raised; skipping",
            entry.app_key,
            task.name,
        )
        return _TaskBackfillOutcome("skipped_error")

    if ctx.dry_run:
        ctx.log.info(
            "[%s] %s: dry-run would stamp %r",
            entry.app_key,
            task.name,
            RESERVED_FORM_KEY,
        )
    else:
        ctx.log.info(
            "[%s] %s: stamped %r",
            entry.app_key,
            task.name,
            RESERVED_FORM_KEY,
        )

    return _TaskBackfillOutcome("stamped", write.data)


async def _rollback_backfill_session(
    session: AsyncSession,
    ctx: FormBackfillContext,
    *,
    app_key: str,
    task_name: str,
) -> None:
    """Roll back a failed per-task persist without aborting the batch.

    Rollback failures are logged and swallowed so a broken session cannot crash
    the remainder of the app batch.

    :param session: The tasks database session to reset.
    :param ctx: Shared backfill context for error logging.
    :param app_key: The declaring app's registry key, for log context.
    :param task_name: The task whose persist step failed.
    """
    try:
        await session.rollback()
    except Exception:
        ctx.log.exception(
            "[%s] %s: rollback failed after persist error; continuing batch",
            app_key,
            task_name,
        )


async def _backfill_app(
    session: AsyncSession,
    entry: FormBackfillEntry,
    ctx: FormBackfillContext,
) -> AppBackfillStats:
    """Backfill all legacy tasks for a single in-scope app.

    :param session: The tasks database session.
    :param entry: The declaring app's backfill entry.
    :param ctx: Shared backfill context.
    :return: Per-app outcome counters.
    """
    stats = AppBackfillStats(app_key=entry.app_key, owner=entry.owner)
    tasks = await TaskManager.list_active(session, owner=entry.owner)
    ctx.log.info(
        "[%s] scanning %s active task(s) for owner %s",
        entry.app_key,
        len(tasks),
        entry.owner,
    )

    for task in tasks:
        outcome = _backfill_single_task(task, entry, ctx)
        if outcome.stamped_data is not None:
            try:
                await _persist_stamped_form(
                    session,
                    task,
                    outcome.stamped_data,
                    dry_run=ctx.dry_run,
                )
                if not ctx.dry_run:
                    await session.commit()
            except Exception:
                ctx.log.exception(
                    "[%s] %s: failed to persist %r; skipping",
                    entry.app_key,
                    task.name,
                    RESERVED_FORM_KEY,
                )
                if not ctx.dry_run:
                    await _rollback_backfill_session(
                        session,
                        ctx,
                        app_key=entry.app_key,
                        task_name=task.name,
                    )
                stats.skipped_error += 1
                continue

        setattr(stats, outcome.label, getattr(stats, outcome.label) + 1)

    return stats


async def run_backfill(
    *,
    owners: Sequence[str] | None = None,
    dry_run: bool = False,
    log: logging.Logger | None = None,
) -> BackfillSummary:
    """Run the legacy ``data['_form']`` backfill for all or selected in-scope apps.

    :param owners: When set, limit the run to these task owners; otherwise all
        in-scope apps are processed.
    :param dry_run: Log actions without persisting stamped forms.
    :param log: Logger for progress and skip messages; defaults to this module's logger.
    :return: Aggregate counters for the run.
    """
    active_log = log or logger
    entries = collect_form_backfill_entries(owners=owners)
    summary = BackfillSummary(dry_run=dry_run)

    if not entries:
        active_log.warning("No in-scope apps matched the requested owner filter")
        return summary

    inventory_session_maker = get_inventory_session_maker()
    tasks_session_maker = get_async_session_maker()
    async with inventory_session_maker() as inventory_session:
        service_lookup = await load_service_id_lookup(inventory_session)
        schema_lookup = await load_schema_id_lookup(inventory_session)
        ctx = FormBackfillContext(
            log=active_log,
            dry_run=dry_run,
            service_lookup=service_lookup,
            schema_lookup=schema_lookup,
        )
        async with tasks_session_maker() as session:
            for entry in entries:
                stats = await _backfill_app(session, entry, ctx)
                summary.apps.append(stats)

    active_log.info(
        "Backfill complete (dry_run=%s): stamped=%s repaired=%s "
        "skipped_existing=%s skipped_unreconstructable=%s skipped_invalid=%s "
        "skipped_error=%s",
        dry_run,
        summary.stamped,
        summary.repaired,
        summary.skipped_existing,
        summary.skipped_unreconstructable,
        summary.skipped_invalid,
        summary.skipped_error,
    )
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser for the backfill entry point.

    :return: A parser exposing ``--dry-run``, ``--owner``, and ``--verbose``.
    """
    entries = collect_form_backfill_entries()
    valid_owners = frozenset(entry.owner for entry in entries)
    if entries:
        app_keys = ", ".join(entry.app_key for entry in entries)
        description = (
            "Backfill data['_form'] on legacy tasks for framework-migrated apps "
            f"({app_keys})."
        )
    else:
        description = (
            "Backfill data['_form'] on legacy tasks. No activated app declares "
            "a form backfill."
        )
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without writing stamped forms to the database.",
    )
    add_owner_and_verbose_arguments(parser, valid_owners=valid_owners, subject="run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the legacy form backfill from the command line.

    :param argv: Optional argument vector; defaults to ``sys.argv[1:]``.
    :return: Exit code ``0`` (the batch never fails on per-task errors).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    # ``basicConfig`` returns early once a handler exists, and importing this
    # module installs the app's logging configuration, so the progress lines need
    # the level set on the emitting logger to clear the root's WARNING.
    logger.setLevel(level)
    asyncio.run(
        run_backfill(
            owners=args.owners,
            dry_run=args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
