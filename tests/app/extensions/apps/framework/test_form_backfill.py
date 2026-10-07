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

"""Tests for the legacy form backfill orchestrator."""

import logging
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.checksums.models import ChecksumsForm, OWNER
from app.extensions.apps.framework import form_backfill
from app.extensions.apps.framework.form_backfill import (
    _backfill_app,
    _backfill_single_task,
    _build_arg_parser,
    _persist_stamped_form,
    _rollback_backfill_session,
    _TaskBackfillOutcome,
    main,
)
from app.extensions.apps.framework.form_backfill_inventory import ServiceIdLookup
from app.extensions.apps.framework.form_backfill_registry import (
    collect_form_backfill_entries,
    FormBackfillContext,
    FormBackfillEntry,
    FormReconstructor,
    StampRepairer,
)
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.tasks.models import Task, TaskBackendEnum


def _minimal_task(*, data: dict) -> Task:
    """Build a task row with only the fields persistence touches."""
    return Task(
        name="legacy-task",
        data=data,
        backend=TaskBackendEnum.NOMAD,
        owner="CHECKSUMS",
        last_updated_by="original-user",
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _reconstructor_must_not_run(_task: Task, _ctx: FormBackfillContext) -> dict:
    """Fail fast when the orchestrator invokes a reconstructor unexpectedly."""
    raise AssertionError("reconstructor must not run")


def _entry(
    reconstructor: FormReconstructor,
    stamp_repairer: StampRepairer | None = None,
) -> FormBackfillEntry:
    """Build a checksums-keyed backfill entry bound to ``reconstructor``."""
    return FormBackfillEntry(
        app_key="checksums",
        owner=OWNER,
        create_model=ChecksumsForm,
        reconstructor=reconstructor,
        stamp_repairer=stamp_repairer,
    )


# A populated (but empty) lookup so the orchestrator's guard passes to the reconstructor.
_EMPTY_SERVICE_LOOKUP = ServiceIdLookup.from_services([])

_ARGPARSE_USAGE_ERROR = 2


async def _persisted_task(session: AsyncSession, *, data: dict) -> Task:
    """Insert a task row and return the refreshed ORM instance."""
    task = _minimal_task(data=data)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_persist_stamped_form_assigns_data_and_preserves_audit_fields(
    tasks_session: AsyncSession,
):
    """Stamped ``data`` is written without bumping audit columns on flush."""
    task = await _persisted_task(tasks_session, data={"meta": {}})
    original_updated_at = task.updated_at
    stamped_data = {
        "meta": {},
        RESERVED_FORM_KEY: {"task_name": "legacy-task"},
    }

    await _persist_stamped_form(tasks_session, task, stamped_data, dry_run=False)
    await tasks_session.commit()
    await tasks_session.refresh(task)

    assert task.data[RESERVED_FORM_KEY] == {"task_name": "legacy-task"}
    assert task.last_updated_by == "original-user"
    assert task.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_persist_stamped_form_dry_run_is_noop():
    """Dry-run mode must not mutate the task or touch the session."""
    original_data = {"meta": {}}
    task = _minimal_task(data=original_data)
    session = MagicMock()
    session.flush = AsyncMock()

    await _persist_stamped_form(
        session,
        task,
        {**original_data, RESERVED_FORM_KEY: {"task_name": "legacy-task"}},
        dry_run=True,
    )

    assert task.data == original_data
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_backfill_session_swallows_rollback_failure():
    """A failed rollback must be logged without re-raising."""
    session = MagicMock()
    session.rollback = AsyncMock(side_effect=RuntimeError("broken session"))
    ctx = FormBackfillContext(log=logging.getLogger("test"))

    await _rollback_backfill_session(
        session,
        ctx,
        app_key="checksums",
        task_name="task-fail",
    )


@pytest.mark.asyncio
async def test_backfill_app_continues_when_persist_and_rollback_fail():
    """One task's persist and rollback failures must not abort the batch."""
    task_fail = _minimal_task(data={"meta": {}})
    task_fail.name = "task-fail"
    task_ok = _minimal_task(data={"meta": {}})
    task_ok.name = "task-ok"
    stamped_payload = {"meta": {}, RESERVED_FORM_KEY: {"task_name": "x"}}

    session = MagicMock()
    session.commit = AsyncMock(side_effect=[RuntimeError("commit failed"), None])
    session.rollback = AsyncMock(side_effect=RuntimeError("rollback failed"))
    session.flush = AsyncMock()

    entry = _entry(lambda _t, _c: None)
    ctx = FormBackfillContext(log=logging.getLogger("test"))
    outcomes = [
        _TaskBackfillOutcome("stamped", stamped_payload),
        _TaskBackfillOutcome("stamped", stamped_payload),
    ]
    expected_commit_attempts = len(outcomes)

    with (
        patch(
            "app.extensions.apps.framework.form_backfill.TaskManager.list_active",
            new_callable=AsyncMock,
            return_value=[task_fail, task_ok],
        ),
        patch(
            "app.extensions.apps.framework.form_backfill._backfill_single_task",
            side_effect=outcomes,
        ),
    ):
        stats = await _backfill_app(session, entry, ctx)

    assert stats.skipped_error == 1
    assert stats.stamped == 1
    assert session.commit.await_count == expected_commit_attempts
    session.rollback.assert_awaited_once()


def test_backfill_single_task_skips_existing_form_stamp():
    """Re-running the pipeline must not overwrite an existing ``data['_form']`` stamp."""
    task = _minimal_task(
        data={"meta": {}, RESERVED_FORM_KEY: {"task_name": "already-stamped"}},
    )
    entry = _entry(_reconstructor_must_not_run)
    ctx = FormBackfillContext(log=logging.getLogger("test"))

    outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "skipped_existing"
    assert outcome.stamped_data is None
    assert task.data[RESERVED_FORM_KEY] == {"task_name": "already-stamped"}


def test_backfill_single_task_skips_when_service_lookup_missing():
    """Skip a run with no inventory lookup before invoking the reconstructor."""
    task = _minimal_task(data={"meta": {}})
    entry = _entry(_reconstructor_must_not_run)
    ctx = FormBackfillContext(log=logging.getLogger("test"), service_lookup=None)

    outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "skipped_unreconstructable"
    assert outcome.stamped_data is None
    assert RESERVED_FORM_KEY not in task.data


def test_backfill_single_task_skips_invalid_reconstructed_form():
    """A reconstructed body that fails ``create_model`` validation is not stamped."""
    task = _minimal_task(data={"meta": {}})

    def _invalid_body(_task: Task, _ctx: FormBackfillContext) -> dict:
        return {
            "task_name": "",
            "hostname": "executor-1",
            "service_id": 1,
        }

    entry = _entry(_invalid_body)
    ctx = FormBackfillContext(
        log=logging.getLogger("test"), service_lookup=_EMPTY_SERVICE_LOOKUP
    )

    outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "skipped_invalid"
    assert outcome.stamped_data is None
    assert RESERVED_FORM_KEY not in task.data


def test_an_invalid_reconstruction_is_logged_without_the_submitted_values(caplog):
    """Keep the rejected values out of the log a reconstruction failure writes.

    A form body can carry a GPG recipient or a key-file path, and pydantic attaches
    the value that failed to every error it raises, so the errors reach the log
    with the field names and messages but without the inputs.
    """
    task = _minimal_task(data={"meta": {}})
    secret = "gpg-recipient@example.com"

    def _invalid_body(_task: Task, _ctx: FormBackfillContext) -> dict:
        return {
            "task_name": "legacy-task",
            "hostname": "executor-1",
            "service_id": secret,
        }

    entry = _entry(_invalid_body)
    ctx = FormBackfillContext(
        log=logging.getLogger("test"), service_lookup=_EMPTY_SERVICE_LOOKUP
    )

    with caplog.at_level(logging.INFO, logger="test"):
        outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "skipped_invalid"
    assert secret not in caplog.text
    assert "service_id" in caplog.text


def test_backfill_single_task_stamps_a_row_whose_hook_path_predates_the_allow_list():
    """Stamp a row whose stored hook path the write model would reject.

    ``TaskWrite`` constrains hook paths to an allow-listed namespace, but the
    envelope built here only carries ``data`` through stamping, so a row whose
    stored path predates that constraint must not lose its backfill over it.
    """
    task = _minimal_task(data={"meta": {}})
    task.alert_detail_builder = "app.extensions.plugins.archives.alerts:build"

    def _valid_body(_task: Task, _ctx: FormBackfillContext) -> dict:
        return {
            "task_name": _task.name,
            "hostname": "executor-1",
            "service_id": 1,
            "recursion_method": "processlist",
        }

    entry = _entry(_valid_body)
    ctx = FormBackfillContext(
        log=logging.getLogger("test"), service_lookup=_EMPTY_SERVICE_LOOKUP
    )

    outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "stamped"
    assert outcome.stamped_data is not None
    assert RESERVED_FORM_KEY in outcome.stamped_data


@pytest.mark.asyncio
async def test_backfill_single_task_stamp_preserves_audit_fields_on_persist(
    tasks_session: AsyncSession,
):
    """A successful stamp plus persist must leave audit attribution untouched."""
    task = await _persisted_task(
        tasks_session,
        data={"meta": {"command": "pt-table-checksum"}},
    )
    original_updated_at = task.updated_at

    def _valid_body(_task: Task, _ctx: FormBackfillContext) -> dict:
        return {
            "task_name": _task.name,
            "hostname": "executor-1",
            "service_id": 1,
            "recursion_method": "processlist",
        }

    entry = _entry(_valid_body)
    ctx = FormBackfillContext(
        log=logging.getLogger("test"), service_lookup=_EMPTY_SERVICE_LOOKUP
    )

    outcome = _backfill_single_task(task, entry, ctx)

    assert outcome.label == "stamped"
    assert outcome.stamped_data is not None
    await _persist_stamped_form(
        tasks_session, task, outcome.stamped_data, dry_run=False
    )
    await tasks_session.commit()
    await tasks_session.refresh(task)

    assert task.data[RESERVED_FORM_KEY]["task_name"] == "legacy-task"
    assert task.last_updated_by == "original-user"
    assert task.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_backfill_app_records_mixed_outcomes_without_aborting():
    """Mixed per-task outcomes in one batch must all be counted independently."""
    stamped_payload = {"meta": {}, RESERVED_FORM_KEY: {"task_name": "stamped"}}
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    entry = _entry(lambda _t, _c: None)
    ctx = FormBackfillContext(log=logging.getLogger("test"))
    outcomes = [
        _TaskBackfillOutcome("skipped_existing"),
        _TaskBackfillOutcome("skipped_unreconstructable"),
        _TaskBackfillOutcome("skipped_invalid"),
        _TaskBackfillOutcome("skipped_error"),
        _TaskBackfillOutcome("stamped", stamped_payload),
    ]

    with (
        patch(
            "app.extensions.apps.framework.form_backfill.TaskManager.list_active",
            new_callable=AsyncMock,
            return_value=[_minimal_task(data={"meta": {}}) for _ in outcomes],
        ),
        patch(
            "app.extensions.apps.framework.form_backfill._backfill_single_task",
            side_effect=outcomes,
        ),
    ):
        stats = await _backfill_app(session, entry, ctx)

    assert stats.skipped_existing == 1
    assert stats.skipped_unreconstructable == 1
    assert stats.skipped_invalid == 1
    assert stats.skipped_error == 1
    assert stats.stamped == 1
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_app_counts_and_commits_a_repaired_stamp():
    """A repaired stamp is persisted like a new one but counted separately.

    The two are distinct operationally: a stamped count is legacy rows catching
    up, where a repaired count is rows the current form has outgrown.
    """
    repaired_payload = {"meta": {}, RESERVED_FORM_KEY: {"task_name": "repaired"}}
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    entry = _entry(lambda _t, _c: None)
    ctx = FormBackfillContext(log=logging.getLogger("test"))

    with (
        patch(
            "app.extensions.apps.framework.form_backfill.TaskManager.list_active",
            new_callable=AsyncMock,
            return_value=[_minimal_task(data={"meta": {}})],
        ),
        patch(
            "app.extensions.apps.framework.form_backfill._backfill_single_task",
            side_effect=[_TaskBackfillOutcome("repaired", repaired_payload)],
        ),
    ):
        stats = await _backfill_app(session, entry, ctx)

    assert stats.repaired == 1
    assert stats.stamped == 0
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_app_dry_run_never_commits():
    """Dry-run mode must not commit even when tasks are stamped in memory."""
    stamped_payload = {"meta": {}, RESERVED_FORM_KEY: {"task_name": "dry-run"}}
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    entry = _entry(lambda _t, _c: None)
    ctx = FormBackfillContext(log=logging.getLogger("test"), dry_run=True)

    with (
        patch(
            "app.extensions.apps.framework.form_backfill.TaskManager.list_active",
            new_callable=AsyncMock,
            return_value=[_minimal_task(data={"meta": {}})],
        ),
        patch(
            "app.extensions.apps.framework.form_backfill._backfill_single_task",
            return_value=_TaskBackfillOutcome("stamped", stamped_payload),
        ),
    ):
        stats = await _backfill_app(session, entry, ctx)

    assert stats.stamped == 1
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()


def test_arg_parser_description_lists_every_collected_app_key():
    """Derive the CLI's app list from the collected entries, not a hardcoded tuple."""
    description = _build_arg_parser().description

    assert description is not None
    for entry in collect_form_backfill_entries():
        assert entry.app_key in description
    assert "restores" not in description


def test_arg_parser_description_omits_the_app_list_when_nothing_is_declared(
    monkeypatch,
):
    """Drop the parenthesized app list when no activated app declares a backfill."""
    monkeypatch.setattr(
        "app.extensions.apps.framework.form_backfill.collect_form_backfill_entries",
        list,
    )

    description = _build_arg_parser().description

    assert description is not None
    assert "()" not in description
    assert "No activated app declares a form backfill." in description


def test_main_rejects_any_owner_when_nothing_is_declared(capsys, monkeypatch):
    """Name the empty scope instead of an empty ``expected one of`` list."""
    monkeypatch.setattr(
        "app.extensions.apps.framework.form_backfill.collect_form_backfill_entries",
        list,
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["--owner", "CHECKSUMS"])

    assert exc_info.value.code == _ARGPARSE_USAGE_ERROR
    stderr = capsys.readouterr().err
    assert "no activated app declares a form backfill" in stderr
    assert "expected one of" not in stderr


def test_main_cli_help_exits_zero(capsys):
    """The module entry point exposes the documented CLI flags."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--dry-run" in help_text
    assert "--owner" in help_text
    assert "--verbose" in help_text


@pytest.mark.parametrize(
    "value", ["archiver", "CHECKSUMS", "backup_pg", "BACKUPS", "restores", "ALTERS"]
)
def test_main_accepts_and_normalizes_in_scope_owner(value, monkeypatch):
    """Accept each in-scope owner, forwarding the normalized value to run_backfill."""
    run = AsyncMock()
    monkeypatch.setattr("app.extensions.apps.framework.form_backfill.run_backfill", run)

    assert main(["--owner", value, "--dry-run"]) == 0
    run.assert_awaited_once_with(owners=[value.upper()], dry_run=True)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--dry-run"], logging.INFO), (["--dry-run", "--verbose"], logging.DEBUG)],
)
def test_verbose_moves_the_level_the_progress_lines_are_emitted_at(
    argv, expected, monkeypatch
):
    """Raise the emitting logger, not just the root's unreachable default.

    The app's logging configuration installs a handler before this module is
    imported, which is enough for ``basicConfig`` to return early and leave the
    root at ``WARNING`` — so a flag that only reached it would silence every
    per-task line an operator runs the backfill to watch.
    """
    monkeypatch.setattr(
        "app.extensions.apps.framework.form_backfill.run_backfill", AsyncMock()
    )
    monkeypatch.setattr(form_backfill.logger, "level", logging.NOTSET)

    assert main(argv) == 0
    assert form_backfill.logger.level == expected


@pytest.mark.parametrize("value", ["ANY", "BACKUP_MONGO", "RESTORE_MONGO", "bogus"])
def test_main_rejects_owner_outside_the_in_scope_set(value, capsys):
    """Reject a ``--owner`` naming no in-scope backfill app with an argparse error."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--owner", value])

    assert exc_info.value.code == _ARGPARSE_USAGE_ERROR
    assert "unknown owner" in capsys.readouterr().err


# A stamp that validates once the field a later form revision added is filled in.
_STALE_STAMP = {
    "task_name": "legacy-task",
    "hostname": "executor-1",
    "service_id": 1,
}


def _repairer_must_not_run(_form: dict, _task: Task, _ctx: FormBackfillContext) -> dict:
    """Fail fast when the orchestrator invokes a repairer unexpectedly."""
    raise AssertionError("stamp repairer must not run")


class TestRepairExistingStamp:
    """Repair stamps written against an earlier revision of an app's create model.

    A stamp records the create body as the form looked when the task was saved, so
    a field the form gained later is missing from every older stamp and the edit
    form fills it from the schema default. Where that default is not what the task
    runs, the repairer is the only thing standing between the operator and a save
    that silently changes behaviour — so a repair that cannot be validated has to
    leave the stored stamp exactly as it found it.
    """

    @staticmethod
    def _ctx() -> FormBackfillContext:
        return FormBackfillContext(
            log=logging.getLogger("test"), service_lookup=_EMPTY_SERVICE_LOOKUP
        )

    def _run(self, stamp: object, repairer: StampRepairer | None) -> tuple:
        task = _minimal_task(data={"meta": {}, RESERVED_FORM_KEY: stamp})
        outcome = _backfill_single_task(
            task, _entry(_reconstructor_must_not_run, repairer), self._ctx()
        )
        return outcome, task

    def test_repairs_a_stamp_missing_a_later_field(self):
        """Stamp the repaired body and leave the row's own ``data`` untouched."""
        outcome, task = self._run(
            dict(_STALE_STAMP),
            lambda form, _t, _c: {**form, "recursion_method": "processlist"},
        )

        assert outcome.label == "repaired"
        assert outcome.stamped_data is not None
        assert (
            outcome.stamped_data[RESERVED_FORM_KEY]["recursion_method"] == "processlist"
        )
        assert "recursion_method" not in task.data[RESERVED_FORM_KEY]

    def test_a_declared_repairer_never_sees_the_stored_dict_itself(self):
        """Hand the repairer a copy, so an in-place repair cannot leak unvalidated.

        A repairer that mutates and returns its argument is the natural way to
        write one; passing the stored dict would apply that mutation to the row
        even down the paths that refuse to persist it.
        """

        def _mutate_in_place(form: dict, _task: Task, _ctx: FormBackfillContext):
            form["service_id"] = "not-an-int"
            return form

        _, task = self._run(dict(_STALE_STAMP), _mutate_in_place)

        assert task.data[RESERVED_FORM_KEY] == _STALE_STAMP

    def test_an_app_declaring_no_repairer_keeps_the_stamp(self):
        """Skip an already-stamped task when its app declares no repairer."""
        outcome, task = self._run(dict(_STALE_STAMP), None)

        assert outcome.label == "skipped_existing"
        assert outcome.stamped_data is None
        assert task.data[RESERVED_FORM_KEY] == _STALE_STAMP

    def test_a_repairer_finding_nothing_to_do_keeps_the_stamp(self):
        """Skip a stamp the repairer reports as already current.

        This is what makes re-runs idempotent: a repaired task is indistinguishable
        from one stamped by the current form, and neither is rewritten.
        """
        outcome, _ = self._run(dict(_STALE_STAMP), lambda _f, _t, _c: None)

        assert outcome.label == "skipped_existing"
        assert outcome.stamped_data is None

    def test_a_repair_that_cannot_validate_keeps_the_stamp(self):
        """Count an unvalidatable repair invalid rather than stamping it."""
        outcome, task = self._run(
            dict(_STALE_STAMP),
            lambda form, _t, _c: {**form, "service_id": "not-an-int"},
        )

        assert outcome.label == "skipped_invalid"
        assert outcome.stamped_data is None
        assert task.data[RESERVED_FORM_KEY] == _STALE_STAMP

    def test_an_invalid_repair_is_logged_without_the_stamped_values(self, caplog):
        """Keep the rejected values out of the log an invalid repair writes.

        The stamp is a whole submitted form, so the errors pydantic raises against
        it carry every value the operator typed unless the input is suppressed.
        """
        secret = "gpg-recipient@example.com"

        with caplog.at_level(logging.INFO, logger="test"):
            outcome, _ = self._run(
                dict(_STALE_STAMP),
                lambda form, _t, _c: {**form, "service_id": secret},
            )

        assert outcome.label == "skipped_invalid"
        assert secret not in caplog.text
        assert "service_id" in caplog.text

    def test_a_raising_repairer_costs_only_its_own_task(self):
        """Count a raising repairer an error, leaving the stamp for the next run."""

        def _boom(_form: dict, _task: Task, _ctx: FormBackfillContext) -> dict:
            raise RuntimeError("repairer exploded")

        outcome, task = self._run(dict(_STALE_STAMP), _boom)

        assert outcome.label == "skipped_error"
        assert outcome.stamped_data is None
        assert task.data[RESERVED_FORM_KEY] == _STALE_STAMP

    def test_a_stamp_that_is_not_a_mapping_is_never_repaired(self):
        """Skip a stamp no repairer could read, without invoking one.

        Nothing writes a non-mapping stamp, so reaching a repairer with one would
        only turn a corrupt row into a repairer's ``AttributeError``.
        """
        outcome, _ = self._run(["not", "a", "form"], _repairer_must_not_run)

        assert outcome.label == "skipped_existing"
        assert outcome.stamped_data is None
