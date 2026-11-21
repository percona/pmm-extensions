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

"""Tests for the read-only ``data['_form']`` stamp audit."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, model_validator, ValidationError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.extensions.apps.framework import form_audit
from app.extensions.apps.framework.form_audit import (
    _audit_app,
    _audit_single_task,
    _build_arg_parser,
    _finding_from_error,
    _route_create_model,
    AppAuditStats,
    AuditSummary,
    format_summary,
    main,
    run_audit,
    StampFinding,
)
from app.extensions.apps.framework.form_backfill_registry import (
    FormBackfillContext,
    FormBackfillEntry,
)
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.apps.mysql_backups.form_backfill import LegacyBackupCreate
from app.extensions.apps.mysql_backups.forms import (
    BackupCreate,
    EncryptionFormat,
    OWNER,
)
from app.extensions.apps.mysql_backups.models import BackupType
from app.tasks.crud import TaskManager
from app.tasks.models import Task, TaskBackendEnum

_ARGPARSE_USAGE_ERROR = 2
#: One task per outcome the audit distinguishes.
_SCANNED_TASKS = 4
#: Tasks seeded for the batching pass; two full batches of two plus a short one.
_BATCHED_TASKS = 5
_RECIPIENT = "ops@example.com"
_KEYFILE = "/keys/aes.key"


def _reconstructor_must_not_run(
    _task: Task, _ctx: FormBackfillContext
) -> dict[str, Any]:
    """Fail loudly if the audit ever reconstructs instead of reading a stamp."""
    raise AssertionError("the audit must not reconstruct a form")


def _entry(**overrides: Any) -> FormBackfillEntry:
    """Return a backfill entry shaped like the one mysql_backups declares.

    :param overrides: Entry fields layered over the mysql_backups defaults.
    :return: A :class:`FormBackfillEntry` for the audit under test.
    """
    return FormBackfillEntry(
        **{
            "app_key": "mysql_backups",
            "owner": OWNER,
            "create_model": LegacyBackupCreate,
            "reconstructor": _reconstructor_must_not_run,
            **overrides,
        }
    )


def _stamp(**overrides: Any) -> dict[str, Any]:
    """Return a stamp the app's own create model accepts.

    :param overrides: Form fields layered over the accepted body.
    :return: A ``data['_form']`` stamp.
    """
    return {
        "task_name": "nightly-backup",
        "hostname": "executor-1",
        "service_id": 9,
        "backup_type": BackupType.XTRABACKUP.value,
        "backup_dir": "/backups",
        "alert_on_fail": False,
        "upload": ["S3"],
        "s3_bucket": "backups-bucket",
        **overrides,
    }


def _unreachable_timing_stamp(**overrides: Any) -> dict[str, Any]:
    """Return a stamp naming in-place GPG with no upload target.

    :param overrides: Form fields layered over the rejected body.
    :return: A ``data['_form']`` stamp the app's own create model rejects.
    """
    return _stamp(
        **{
            "upload": [],
            "s3_bucket": None,
            "encryption_format": EncryptionFormat.GPG.value,
            "encrypt": True,
            "encryption_recipient": _RECIPIENT,
            **overrides,
        }
    )


def _task(*, form: dict[str, Any] | str | None = None, name: str = "nightly") -> Task:
    """Build a task row carrying (or lacking) a form stamp.

    :param form: The ``data['_form']`` value, or ``None`` to leave it unstamped.
    :param name: The task name.
    :return: An unpersisted task row owned by the audited app.
    """
    data: dict[str, Any] = {"task": "run-python", "meta": {}}
    if form is not None:
        data[RESERVED_FORM_KEY] = form
    return Task(name=name, data=data, backend=TaskBackendEnum.PROXY, owner=OWNER)


def _summary_with_one_finding() -> AuditSummary:
    """Return a summary carrying one app, one finding, and two clean stamps."""
    return AuditSummary(
        apps=[
            AppAuditStats(
                app_key="mysql_backups",
                owner=OWNER,
                valid=2,
                unstamped=1,
                findings=[
                    StampFinding(
                        task_id=7,
                        task_name="nightly",
                        fields=("encrypt",),
                        reasons=("requires at least one upload provider",),
                    )
                ],
            )
        ]
    )


class TestRouteCreateModel:
    """Resolve the model an app's own create and update routes enforce."""

    def test_it_prefers_the_model_the_app_declares(self):
        """Audit against the app definition, not the backfill entry.

        mysql_backups registers a laxer model so legacy tasks keep stamping, so
        reading the entry would report the population a save now rejects as
        valid.
        """
        entry = _entry()

        assert entry.create_model is LegacyBackupCreate
        assert _route_create_model(entry) is BackupCreate

    def test_it_falls_back_when_the_app_declares_no_create_model(self):
        """Keep the entry's model for an app whose schema is not model-derived.

        ``alters`` builds its schema from a ``schema=`` declaration, so its app
        definition carries no create model to prefer.
        """
        entry = _entry(app_key="alters")

        assert _route_create_model(entry) is entry.create_model


class TestAuditSingleTask:
    """Classify one task against the model handed to the classifier."""

    def test_a_stamp_the_create_model_rejects_is_reported(self):
        """Report a stamp whose combination the create route would refuse."""
        outcome = _audit_single_task(
            _task(form=_unreachable_timing_stamp()), BackupCreate
        )

        assert outcome.finding is not None
        assert outcome.finding.task_name == "nightly"
        assert any(
            "requires at least one upload provider" in reason
            for reason in outcome.finding.reasons
        )

    def test_an_accepted_stamp_is_not_reported(self):
        """Leave a stamp the create model still accepts out of the report."""
        outcome = _audit_single_task(_task(form=_stamp()), BackupCreate)

        assert outcome.label == "valid"
        assert outcome.finding is None

    def test_the_supplied_model_decides_the_verdict(self):
        """Accept under the laxer model exactly what the strict one rejects.

        The pair is what makes the audit's model resolution load-bearing rather
        than incidental.
        """
        stamp = _unreachable_timing_stamp()

        assert _audit_single_task(_task(form=stamp), LegacyBackupCreate).finding is None
        assert _audit_single_task(_task(form=stamp), BackupCreate).finding is not None

    def test_an_unstamped_task_is_not_reported(self):
        """Count a task with no stamp separately: there is nothing to validate."""
        outcome = _audit_single_task(_task(), BackupCreate)

        assert outcome.label == "unstamped"
        assert outcome.finding is None

    def test_a_stamp_that_is_not_a_mapping_is_counted_unreadable(self):
        """Count a non-mapping stamp rather than raising on it."""
        outcome = _audit_single_task(_task(form="not-a-form"), BackupCreate)

        assert outcome.label == "unreadable"
        assert outcome.finding is None

    def test_a_field_level_failure_names_the_field(self):
        """Name the offending field for a failure pydantic attributes to one."""
        outcome = _audit_single_task(_task(form=_stamp(backup_dir="")), BackupCreate)

        assert outcome.finding is not None
        assert "backup_dir" in outcome.finding.fields

    def test_the_finding_carries_no_submitted_value(self):
        """Keep recipients and key-file paths out of the report.

        A form body holds a GPG recipient and filesystem paths, and every pydantic
        error carries the value that failed, so a finding built from the raw errors
        would print both.
        """
        stamp = _unreachable_timing_stamp(xtrabackup_aes256_keyfile=_KEYFILE)

        outcome = _audit_single_task(_task(form=stamp), BackupCreate)

        assert outcome.finding is not None
        rendered = repr(outcome.finding)
        assert _RECIPIENT not in rendered
        assert _KEYFILE not in rendered

    def test_a_model_level_message_is_forwarded_verbatim(self):
        """Pin what the audit's value-free report actually rests on.

        ``include_input=False`` drops pydantic's own record of the input, but a
        whole-model message is reported as the validator wrote it — so a validator
        that interpolates the value it rejects puts that value in the report. The
        audit cannot filter that, which is why the guarantee is stated as a rule
        for model-level validators rather than as a property of this module.
        """

        class _Leaky(BaseModel):
            secret: str

            @model_validator(mode="after")
            def _reject(self) -> _Leaky:
                raise ValueError(f"rejected {self.secret}")

        with pytest.raises(ValidationError) as exc_info:
            _Leaky(secret=_KEYFILE)

        finding = _finding_from_error(_task(), exc_info.value)

        assert any(_KEYFILE in reason for reason in finding.reasons)


class TestAuditApp:
    """Walk one app's active tasks and total each outcome."""

    @pytest.mark.asyncio
    async def test_counts_every_outcome_it_scans(self, tasks_session: AsyncSession):
        """Total the four outcomes and keep one finding per rejected stamp."""
        for name, form in (
            ("rejected", _unreachable_timing_stamp()),
            ("accepted", _stamp()),
            ("unstamped", None),
            ("unreadable", "not-a-form"),
        ):
            tasks_session.add(_task(form=form, name=name))
        await tasks_session.commit()

        stats = await _audit_app(tasks_session, _entry(), log=logging.getLogger("test"))

        assert stats.scanned == _SCANNED_TASKS
        assert stats.rejected == 1
        assert stats.valid == 1
        assert stats.unstamped == 1
        assert stats.unreadable == 1
        assert stats.errored == 0
        assert stats.findings[0].task_name == "rejected"

    @pytest.mark.asyncio
    async def test_a_row_it_cannot_classify_costs_only_that_row(
        self, tasks_session: AsyncSession, caplog
    ):
        """Keep auditing after a task whose ``data`` is not a mapping at all.

        The report is the only record of how large the affected population is, so
        losing the run to one corrupt row is worse than reporting it as
        unresolved.
        """
        corrupt = Task(
            name="corrupt",
            data=["not", "a", "mapping"],
            backend=TaskBackendEnum.PROXY,
            owner=OWNER,
        )
        tasks_session.add(corrupt)
        tasks_session.add(_task(form=_unreachable_timing_stamp(), name="rejected"))
        await tasks_session.commit()

        with caplog.at_level(logging.ERROR):
            stats = await _audit_app(
                tasks_session, _entry(), log=logging.getLogger("test")
            )

        assert stats.errored == 1
        assert stats.rejected == 1
        assert "counting it unresolved" in caplog.text

    @pytest.mark.asyncio
    async def test_it_reads_the_population_in_bounded_batches(
        self, tasks_session: AsyncSession, monkeypatch
    ):
        """Keep the rows in memory bounded by the batch, not by the table.

        A stamp is a whole create body, and this runs against the installations
        whose task table is the reason the audit exists, so reading the
        population in one list is what would stop the report being produced.
        """
        for index in range(_BATCHED_TASKS):
            tasks_session.add(
                _task(form=_unreachable_timing_stamp(), name=f"task-{index}")
            )
        await tasks_session.commit()

        read_sizes: list[int] = []
        unpatched = TaskManager.iter_active_batches

        def spy(*args: Any, **kwargs: Any) -> AsyncIterator[list[Task]]:
            async def recorded() -> AsyncIterator[list[Task]]:
                async for batch in unpatched(*args, **kwargs):
                    read_sizes.append(len(batch))
                    yield batch

            return recorded()

        monkeypatch.setattr(TaskManager, "iter_active_batches", spy)

        stats = await _audit_app(
            tasks_session, _entry(), log=logging.getLogger("test"), batch_size=2
        )

        assert read_sizes == [2, 2, 1]
        assert stats.rejected == _BATCHED_TASKS


class TestRunAudit:
    """Drive the whole run: entry selection, then one session over every app."""

    @pytest.mark.asyncio
    async def test_it_reports_every_app_the_owner_filter_selects(
        self, tasks_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """Audit the selected owner's tasks in one read-only session."""

        @asynccontextmanager
        async def _session():
            yield tasks_session

        monkeypatch.setattr(
            form_audit, "get_async_session_maker", lambda: _session, raising=True
        )
        tasks_session.add(_task(form=_unreachable_timing_stamp(), name="rejected"))
        await tasks_session.commit()

        summary = await run_audit(owners=[OWNER])

        assert [app.app_key for app in summary.apps] == ["mysql_backups"]
        assert summary.rejected == 1
        assert summary.scanned == 1

    @pytest.mark.asyncio
    async def test_it_warns_when_no_app_matches_the_owner_filter(self, caplog):
        """Say the scope was empty instead of reporting a clean install."""
        with caplog.at_level(logging.WARNING):
            summary = await run_audit(owners=["NOT-AN-OWNER"])

        assert summary.apps == []
        assert "No in-scope apps matched" in caplog.text


class TestCli:
    """Expose the audit through the same flags as the backfill entry point."""

    def test_help_exits_zero(self, capsys):
        """Document the flags the audit accepts."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        help_text = capsys.readouterr().out
        assert "--owner" in help_text
        assert "--verbose" in help_text

    def test_an_unknown_owner_exits_with_the_usage_error(self, capsys):
        """Reject an owner no activated app declares."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--owner", "NOT-AN-OWNER"])

        assert exc_info.value.code == _ARGPARSE_USAGE_ERROR
        assert "unknown owner" in capsys.readouterr().err

    def test_the_description_lists_the_audited_apps(self):
        """Derive the CLI's app list from the collected entries."""
        description = _build_arg_parser().description

        assert description is not None
        assert "mysql_backups" in description

    def test_the_description_names_an_empty_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Say why there is nothing to audit rather than printing empty parentheses."""
        monkeypatch.setattr(form_audit, "collect_form_backfill_entries", list)

        description = _build_arg_parser().description

        assert description is not None
        assert "()" not in description
        assert "No activated app declares a form backfill." in description

    def test_it_prints_the_report_and_exits_zero(
        self, capsys, monkeypatch: pytest.MonkeyPatch
    ):
        """Write the report to stdout, which is the whole point of the command.

        The app's logging configuration drops INFO records, so a report that only
        reached the log would leave the operator with no output at all.
        """
        run = AsyncMock(return_value=_summary_with_one_finding())
        monkeypatch.setattr(form_audit, "run_audit", run)

        assert main([]) == 0

        report = capsys.readouterr().out
        assert "mysql_backups (BACKUPS): scanned=4 rejected=1" in report
        assert "requires at least one upload provider" in report
        run.assert_awaited_once_with(owners=None)

    @pytest.mark.parametrize("value", ["BACKUPS", "backups", " backups "])
    def test_it_forwards_the_normalized_owner(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ):
        """Forward each accepted spelling of an in-scope owner as the normal form."""
        run = AsyncMock(return_value=AuditSummary())
        monkeypatch.setattr(form_audit, "run_audit", run)

        assert main(["--owner", value]) == 0
        run.assert_awaited_once_with(owners=[OWNER])

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [([], logging.INFO), (["--verbose"], logging.DEBUG)],
    )
    def test_verbose_moves_the_level_the_progress_lines_are_emitted_at(
        self, argv: list[str], expected: int, monkeypatch: pytest.MonkeyPatch
    ):
        """Raise the emitting logger, not just the root's unreachable default.

        The app's logging configuration installs a handler before this module is
        imported, which is enough for ``basicConfig`` to return early and leave
        the root at ``WARNING`` — so a flag that only reached it would silence
        every progress line it claims to enable.
        """
        monkeypatch.setattr(
            form_audit, "run_audit", AsyncMock(return_value=AuditSummary())
        )
        monkeypatch.setattr(form_audit.logger, "level", logging.NOTSET)

        assert main(argv) == 0
        assert form_audit.logger.level == expected


class TestFormatSummary:
    """Render the report an operator reads off stdout."""

    def test_lists_each_app_its_counters_and_its_findings(self):
        """Name every counter and every finding in the rendered report."""
        rendered = format_summary(_summary_with_one_finding())

        assert (
            "mysql_backups (BACKUPS): scanned=4 rejected=1 valid=2 unstamped=1 "
            "unreadable=0 errored=0" in rendered
        )
        assert "task 7 'nightly'" in rendered
        assert "requires at least one upload provider" in rendered
        assert "Total: scanned=4 rejected=1 unreadable=0 errored=0" in rendered

    def test_reports_a_clean_install_as_a_total_with_no_findings(self):
        """Report the totals even when nothing is rejected."""
        summary = AuditSummary(
            apps=[AppAuditStats(app_key="checksums", owner="CHECKSUMS")]
        )

        assert format_summary(summary).endswith(
            "Total: scanned=0 rejected=0 unreadable=0 errored=0"
        )

    def test_an_unstamped_task_qualifies_the_total(self):
        """Say so when the rejected total is a lower bound.

        An unstamped task is never validated, so it sits outside the population
        the count covers — which makes the headline number incomplete on an
        install where the backfill has not run yet.
        """
        rendered = format_summary(_summary_with_one_finding())

        assert (
            "Note: 1 task(s) carry no stamp and were not validated, so rejected "
            "is a lower bound until form_backfill has run." in rendered
        )

    def test_a_fully_stamped_population_carries_no_qualifier(self):
        """Leave the total unqualified when every task was validated."""
        summary = AuditSummary(
            apps=[AppAuditStats(app_key="checksums", owner="CHECKSUMS", valid=3)]
        )

        assert "lower bound" not in format_summary(summary)
