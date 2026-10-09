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

"""Tests for the MySQL backup catalog recorder."""

import ast
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.extensions.apps.mysql_backups import recorder as recorder_module
from app.extensions.apps.mysql_backups.crud import MysqlBackupRunManager
from app.extensions.apps.mysql_backups.recorder import (
    record_backup_run,
    RUN_RESULT_RECORDER,
)
from app.tasks.hook_resolver import resolve_hook
from app.tasks.models import TaskHistory, TaskHistoryStatusEnum
from tests.app.factories import build_task_history, TaskFactory

_STARTED = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
_FINISHED = datetime(2026, 7, 29, 1, 5, tzinfo=UTC)
_SERVICE_ID = 7

#: Sentinel telling ``_history`` to omit a meta key rather than set it, so a test
#: can distinguish "absent" from "present and empty" — ``None`` is itself a value
#: a malformed task can carry.
_OMITTED = object()


@pytest.fixture
def _recorder_uses_test_session(mocker, session) -> None:
    """Point the recorder's own extensions session at the test's in-memory session.

    ``record_backup_run`` writes on a session it opens itself via the extensions
    ``get_async_session_maker`` — the ``mysql_backup_run`` table is extensions-owned,
    not on the tasks database the recorder seam hands in. Patching that maker to
    yield the test session lets the write and the assertions share one in-memory
    database, and pins the recorder to the *extensions* maker.
    """
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(
        "app.extensions.apps.mysql_backups.recorder.get_async_session_maker",
        return_value=maker,
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize a persisted datetime to UTC-aware for comparison.

    The in-memory SQLite backend returns naive datetimes; Postgres preserves the
    timezone. Either way the wall-clock instant is what matters here.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _history(
    *,
    backup_type: str | None = "M",
    status: TaskHistoryStatusEnum = TaskHistoryStatusEnum.SUCCESS,
    service_name: str | None = "svc-a",
    service_id: object = _SERVICE_ID,
    target: str | None = "db01",
    history_id: int = 1,
) -> TaskHistory:
    """Build a terminal ``TaskHistory`` (with its ``Task``) the recorder can read.

    ``service_id`` is typed loosely so a test can plant a wrong-typed value the
    way a hand-edited or pre-change task would carry it; ``_OMITTED`` leaves the
    key out of the meta entirely.
    """
    meta: dict = {}
    if service_name is not None:
        meta["_service_name"] = service_name
    if service_id is not _OMITTED:
        meta["_service_id"] = service_id
    if target is not None:
        meta["target"] = target
    if backup_type is not None:
        meta["config"] = yaml.dump({"SERVER_LIST": [{"BACKUP_TYPE": backup_type}]})
    task = TaskFactory.build(data={"meta": meta})
    history = build_task_history(task, status=status)
    history.id = history_id
    history.started_at = _STARTED
    history.finished_at = _FINISHED
    return history


@pytest.mark.usefixtures("_recorder_uses_test_session")
class TestRecordsSuccessfulRuns:
    """Leave exactly one full record for a successful mydumper/xtrabackup run."""

    @pytest.mark.asyncio
    async def test_mydumper_success_records_all_fields(self, session) -> None:
        """Map a full mydumper result onto every record field."""
        await record_backup_run(
            session,
            _history(backup_type="M"),
            {
                "backup_dir": "/data/backups/mydumper/svc-a/20260729",
                "size_bytes": 4096,
                "upload_destination": "s3://bucket/svc-a",
            },
        )

        records = await MysqlBackupRunManager.list(session)
        assert len(records) == 1
        record = records[0]
        assert record.backup_type == "M"
        assert record.service_name == "svc-a"
        assert record.service_id == _SERVICE_ID
        assert record.hostname == "db01"
        assert record.location == "/data/backups/mydumper/svc-a/20260729"
        assert record.size_bytes == 4096  # noqa: PLR2004
        assert record.upload_destination == "s3://bucket/svc-a"
        assert _as_utc(record.started_at) == _STARTED
        assert _as_utc(record.finished_at) == _FINISHED

    @pytest.mark.asyncio
    async def test_xtrabackup_incremental_location_stored_verbatim(
        self, session
    ) -> None:
        """Store the incremental layout's different location string as-is."""
        incremental = "/data/backups/xtrabackup/svc-a/_incrementals/20260729"
        await record_backup_run(
            session,
            _history(backup_type="X"),
            {"backup_dir": incremental, "size_bytes": 512, "upload_destination": None},
        )

        records = await MysqlBackupRunManager.list(session)
        assert records[0].backup_type == "X"
        assert records[0].location == incremental
        assert records[0].upload_destination is None


@pytest.mark.usefixtures("_recorder_uses_test_session")
class TestPartialResults:
    """Record what is available even when a success reports nothing or partial data."""

    @pytest.mark.asyncio
    async def test_none_result_records_run_with_empty_artifact_fields(
        self, session
    ) -> None:
        """Record the run with empty fields when a success reports no result."""
        await record_backup_run(session, _history(backup_type="M"), None)

        records = await MysqlBackupRunManager.list(session)
        assert len(records) == 1
        assert records[0].service_name == "svc-a"
        assert records[0].backup_type == "M"
        assert records[0].location is None
        assert records[0].size_bytes is None
        assert records[0].upload_destination is None
        assert _as_utc(records[0].finished_at) == _FINISHED

    @pytest.mark.asyncio
    async def test_partial_result_records_reported_fields_only(self, session) -> None:
        """Store what a partial result reported and leave the rest empty."""
        await record_backup_run(
            session,
            _history(backup_type="M"),
            {"backup_dir": "/data/backups/mydumper/svc-a/20260729"},
        )

        record = (await MysqlBackupRunManager.list(session))[0]
        assert record.location == "/data/backups/mydumper/svc-a/20260729"
        assert record.size_bytes is None
        assert record.upload_destination is None

    @pytest.mark.asyncio
    async def test_malformed_result_fields_are_dropped_not_raised(
        self, session
    ) -> None:
        """Null wrong-typed remote-reported fields instead of storing or raising."""
        await record_backup_run(
            session,
            _history(backup_type="M"),
            {
                "backup_dir": 123,
                "size_bytes": "huge",
                "upload_destination": ["s3://x"],
            },
        )

        record = (await MysqlBackupRunManager.list(session))[0]
        assert record.location is None
        assert record.size_bytes is None
        assert record.upload_destination is None

    @pytest.mark.asyncio
    async def test_bool_is_not_accepted_as_size(self, session) -> None:
        """Reject ``True`` as a size even though it is an int subclass."""
        await record_backup_run(
            session, _history(backup_type="M"), {"size_bytes": True}
        )

        assert (await MysqlBackupRunManager.list(session))[0].size_bytes is None

    @pytest.mark.asyncio
    async def test_missing_service_name_still_records(self, session) -> None:
        """Record a run even when its task carries no service name."""
        await record_backup_run(
            session, _history(backup_type="M", service_name=None), None
        )

        records = await MysqlBackupRunManager.list(session)
        assert len(records) == 1
        assert records[0].service_name is None


@pytest.mark.usefixtures("_recorder_uses_test_session")
class TestServiceId:
    """Read the inventory service id off the task meta, defensively."""

    @pytest.mark.asyncio
    async def test_records_service_id_from_meta(self, session) -> None:
        """Store the id the envelope stamped alongside the service name."""
        await record_backup_run(session, _history(backup_type="M"), None)

        record = (await MysqlBackupRunManager.list(session))[0]
        assert record.service_id == _SERVICE_ID

    @pytest.mark.asyncio
    async def test_task_predating_the_id_still_records(self, session) -> None:
        """Record a run off a task created before the id was stamped.

        The whole point of the name fallback: a pre-change task carries no
        ``_service_id``, and that must leave the column empty rather than fail
        the run or the history sync.
        """
        await record_backup_run(
            session, _history(backup_type="M", service_id=_OMITTED), None
        )

        records = await MysqlBackupRunManager.list(session)
        assert len(records) == 1
        assert records[0].service_id is None
        assert records[0].service_name == "svc-a"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "planted",
        ["7", 7.0, True, None, [7], 0, -1],
        ids=["str", "float", "bool", "null", "list", "zero", "negative"],
    )
    async def test_unusable_service_id_is_dropped(self, session, planted) -> None:
        """Drop a wrong-typed or non-positive id instead of storing it.

        Recording it as absent is what keeps the row name-reachable; see
        :func:`~app.extensions.apps.mysql_backups.recorder._positive_int`.
        """
        await record_backup_run(
            session, _history(backup_type="M", service_id=planted), None
        )

        records = await MysqlBackupRunManager.list(session)
        assert len(records) == 1
        assert records[0].service_id is None
        assert records[0].service_name == "svc-a"

    @pytest.mark.asyncio
    async def test_smallest_valid_id_is_kept(self, session) -> None:
        """Keep an id of ``1``, the lower bound the range guard must not exclude."""
        await record_backup_run(session, _history(backup_type="M", service_id=1), None)

        records = await MysqlBackupRunManager.list(session)
        assert records[0].service_id == 1


@pytest.mark.usefixtures("_recorder_uses_test_session")
class TestNoRecordCases:
    """Leave no record for binlog runs, non-success terminals, and unknown tools."""

    @pytest.mark.asyncio
    async def test_binlog_success_records_nothing(self, session) -> None:
        """Leave no catalog record for a successful binlog run."""
        await record_backup_run(
            session,
            _history(backup_type="B"),
            {"backup_dir": "/data/binlog", "size_bytes": 1},
        )

        assert await MysqlBackupRunManager.list(session) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.FAILED,
            TaskHistoryStatusEnum.STOPPED,
            TaskHistoryStatusEnum.LOST,
        ],
    )
    async def test_non_success_terminal_records_nothing(self, session, status) -> None:
        """Leave no catalog record for a failed, stopped, or lost terminal."""
        await record_backup_run(
            session,
            _history(backup_type="M", status=status),
            {"backup_dir": "/data/x", "size_bytes": 1},
        )

        assert await MysqlBackupRunManager.list(session) == []

    @pytest.mark.asyncio
    async def test_unknown_backup_type_records_nothing(self, session) -> None:
        """Skip recording a run whose config carries no known backup type."""
        await record_backup_run(session, _history(backup_type=None), None)

        assert await MysqlBackupRunManager.list(session) == []

    @pytest.mark.asyncio
    async def test_orphan_history_with_no_task_records_nothing(self, session) -> None:
        """Skip recording, without raising, when the history's task is unset.

        ``record_backup_run`` reads the backup type off ``history.task.data``;
        a history whose ``task`` relation is unset must fall back to "no known
        backup type" rather than raising on the missing attribute.
        """
        history = _history(backup_type="M")
        history.task = None

        await record_backup_run(session, history, {"backup_dir": "/data/x"})

        assert await MysqlBackupRunManager.list(session) == []


@pytest.mark.usefixtures("_recorder_uses_test_session")
class TestIdempotency:
    """Keep one record per run, even on re-invocation."""

    @pytest.mark.asyncio
    async def test_double_invocation_records_once(self, session) -> None:
        """Keep a single record when re-invoking the recorder for one run."""
        history = _history(backup_type="M")
        result = {"backup_dir": "/data/backups/mydumper/svc-a/20260729"}

        await record_backup_run(session, history, result)
        await record_backup_run(session, history, result)

        assert len(await MysqlBackupRunManager.list(session)) == 1


class TestRecorderResolvesNoInventory:
    """Keep the recorder free of any Inventory dependency.

    It runs in the *tasks* service off a ``TaskHistory`` and its task meta, which
    is the whole reason the service name and id are stamped into that meta at
    creation time. An inventory lookup here would both reintroduce the rename
    coupling this catalog key exists to remove and give the tasks service a
    dependency it has no client for.
    """

    def test_module_imports_no_inventory(self) -> None:
        """Assert no import in ``recorder.py`` reaches an inventory module."""
        tree = ast.parse(Path(recorder_module.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not [name for name in imported if "inventory" in name]


class TestRecorderRegistration:
    """Stamp the recorder on the app and let the tasks service resolve it."""

    def test_app_stamps_and_resolves_the_recorder(self) -> None:
        """Declare the recorder path on the app and resolve it to the callable."""
        from app.extensions.apps.mysql_backups.app import app  # noqa: PLC0415

        assert app.run_result_recorder == RUN_RESULT_RECORDER
        assert (
            RUN_RESULT_RECORDER
            == "app.extensions.apps.mysql_backups.recorder:record_backup_run"
        )
        assert resolve_hook(RUN_RESULT_RECORDER) is record_backup_run
