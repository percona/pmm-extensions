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

"""Define tests for the ATW diagnostics-send orchestrator and its housekeeping."""

import io
import json
import os
import re
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiohttp import ClientError
from aioresponses import aioresponses, CallbackResult
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.exceptions import HTTPBadRequestException, HTTPConflictException
from app.core.requests import RemoteAPI
from app.core.requests.registry import ClientRegistry
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingOverride
from app.core.utils import json_serializer
from app.core.utils.date_time import utc_now
from app.extensions.apps.atw.crud import AtwIncidentManager, AtwSendLogManager
from app.extensions.apps.atw.models import (
    AtwIncident,
    AtwSendLog,
    AtwSendStatusEnum,
)
from app.extensions.apps.atw.send import (
    fail_stale_sends,
    get_tasks_api,
    purge_expired_bundles,
    run_send,
    tasks_api_session,
)
from app.extensions.bundle_upload.plan import (
    DeliveryPlan,
    DeliveryPlanError,
    StepRecord,
)
from app.extensions.bundle_upload.resolver import DRIFTED_INPUTS_REASON
from app.extensions.bundle_upload.seam import BundleSource, UploadResult
from app.extensions.config import DeliveryPlanInputs, extensions_settings
from app.tasks.models import TaskHistoryStatusEnum, TaskLogType
from tests.app.core.settings_override.conftest import EXTENSIONS_SETTINGS_TOKEN
from tests.app.db_schema import apply_schema

_UPLOAD_DETAIL: dict[str, Any] = {"result": {"sys_id": "att-9", "size_bytes": 42}}
_EXPECTED_FILE_COUNT = 4
_EXPECTED_ENTRY_COUNT = 9
_EXPECTED_LOG_GROUP_COUNT = 3
_STALE_ROW_COUNT = 2
_MAIN_STEP = "run-script"
_LAUNCH_CHECK_STEP = "check-launchable"
_STORED_SECRET = "stored-api-key"
_DEFAULT_FILES: dict[str, Any] = {
    "stdout.log": {"is_dir": False, "size": 5},
    "diag/report.txt": {"is_dir": False, "size": 7},
}
_ONE_EXECUTION: dict[str, Any] = {
    "id": str(uuid4()),
    "task_history_id": 11,
    "snippet_filename": "cpu.sh",
}


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    """Yield ``content`` a byte at a time, as the Tasks API stream would.

    :param content: The file bytes to hand out.
    :return: One single-byte chunk of the content.
    """
    for index in range(len(content)):
        yield content[index : index + 1]


async def _blocks(content: bytes, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Yield ``content`` in blocks, for payloads too large to hand out bytewise.

    :param content: The file bytes to hand out.
    :param size: The block size in bytes.
    :return: One block of the content.
    """
    for start in range(0, len(content), size):
        yield content[start : start + size]


class _FakeUploader:
    """Stand in for the delivery executor, recording what it was handed."""

    def __init__(
        self,
        *,
        result: UploadResult | None = None,
        error: Exception | None = None,
        step_observer: Callable[[StepRecord], None] | None = None,
    ) -> None:
        self.result = result or UploadResult(reference="att-9", detail=_UPLOAD_DETAIL)
        self.error = error
        self.step_observer = step_observer
        self.plan: DeliveryPlan | None = None
        self.bundle_bytes: bytes | None = None
        self.manifest: dict[str, Any] | None = None
        self.case_ref: str | None = None
        self.called = False
        self.released = False

    async def upload_bundle(
        self,
        *,
        source_ref: str,
        bundle: BundleSource,
        case_ref: str | None,
        manifest: dict[str, Any],
    ) -> UploadResult:
        """Record the send and return the configured result or raise."""
        self.called = True
        self.source_ref = source_ref
        self.bundle_bytes = bundle.content.read()
        self.bundle_size = bundle.size
        self.manifest = dict(manifest)
        self.case_ref = case_ref
        if self.error is not None:
            raise self.error
        return self.result


def _log_record(
    msg: str | None,
    *,
    step: str = _MAIN_STEP,
    stream: str = TaskLogType.STDOUT.value,
) -> dict[str, Any]:
    """Build one log record as the Tasks API serializes ``TaskLog``.

    :param msg: The message; ``None`` is the end-of-log sentinel for that step.
    :param step: The step that produced the record.
    :param stream: The record's stream.
    :return: The record mapping.
    """
    return {"step": step, "type": stream, "msg": msg}


async def _ndjson(records: list[dict[str, Any] | bytes]) -> AsyncIterator[bytes]:
    """Yield each record as one line, with the trailing newline the API preserves.

    :param records: Log records to serialize, or raw lines to hand out verbatim.
    :return: One line of the NDJSON log stream.
    """
    for record in records:
        if isinstance(record, bytes):
            yield record
        else:
            yield json.dumps(record).encode() + b"\n"


async def _ndjson_then_raise(
    records: list[dict[str, Any] | bytes], error: Exception
) -> AsyncIterator[bytes]:
    """Yield each record as one line, then raise as a mid-group stream cut does.

    :param records: Log records to serialize before the failure.
    :param error: The exception to raise once the records are exhausted.
    :return: One line of the NDJSON log stream.
    :raises Exception: ``error``, once every record has been yielded.
    """
    async for line in _ndjson(records):
        yield line
    raise error


def _patch_tasks_api(mocker: MockerFixture, api: AsyncMock) -> AsyncMock:
    """Point the orchestrator at ``api``, yielding it from the auth context.

    :param mocker: The patching fixture.
    :param api: The faked Tasks API client.
    :return: The same client, for the caller to assert against.
    """
    api.auth.return_value.__enter__.return_value = api
    mocker.patch(
        "app.extensions.apps.atw.send.get_tasks_api", new=AsyncMock(return_value=api)
    )
    return api


def _fake_tasks_api(
    mocker: MockerFixture,
    *,
    files: dict[str, Any] | None = None,
    files_listing_error: Exception | None = None,
    file_stream_error: Exception | None = None,
    logs: list[dict[str, Any] | bytes] | None = None,
    log_stream_error: Exception | None = None,
    status: str = TaskHistoryStatusEnum.SUCCESS.value,
) -> AsyncMock:
    """Provide a Tasks API client answering the status, files, and logs routes.

    ``api.get`` dispatches on the path: the orchestrator reads an execution's
    status before listing its output files, and the two routes answer with
    differently-shaped payloads.

    :param mocker: The patching fixture.
    :param files: The output-files listing; two files by default.
    :param files_listing_error: Raised instead of answering the files listing.
    :param file_stream_error: Raised instead of streaming an output file's bytes.
    :param logs: The records the log stream yields; one stdout and one stderr
        group by default.
    :param log_stream_error: Raised instead of opening the log stream.
    :param status: The status reported for every execution.
    :return: The faked client, for the caller to assert against.
    """
    listing = _DEFAULT_FILES if files is None else files
    records = (
        [
            _log_record("out-1\n"),
            _log_record("out-2\n"),
            _log_record("err-1\n", stream=TaskLogType.STDERR.value),
        ]
        if logs is None
        else logs
    )

    def _get(path: str, **_kwargs: Any) -> dict[str, Any]:
        if not path.endswith("/files/"):
            return {"status": status}
        if files_listing_error is not None:
            raise files_listing_error
        return listing

    api = AsyncMock(spec=RemoteAPI)
    api.get.side_effect = _get
    api.stream_chunks.side_effect = (
        (lambda *_a, **_k: _chunks(b"data!"))
        if file_stream_error is None
        else file_stream_error
    )
    api.stream.side_effect = (
        (lambda *_a, **_k: _ndjson(records))
        if log_stream_error is None
        else log_stream_error
    )
    return _patch_tasks_api(mocker, api)


@pytest_asyncio.fixture(name="send_session")
async def send_session_fixture(
    mocker: MockerFixture, tmp_path: Path, delivery_plan: DeliveryPlan
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a session whose maker and bundle directory the orchestrator uses.

    ``run_send`` opens its own session, so the maker it reaches for is pointed at
    this test engine; assertions then read the row back through a *separate*
    session from the same maker, which is what proves a ``detail`` write survived
    the commit rather than merely living on the in-flight instance.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    session_maker = get_async_session_maker_from_engine(engine)
    mocker.patch(
        "app.extensions.apps.atw.send.get_async_session_maker",
        return_value=session_maker,
    )
    mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", delivery_plan)
    mocker.patch("app.extensions.apps.atw.send.bundle_dir", return_value=tmp_path)
    try:
        async with session_maker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture(name="tasks_api")
def tasks_api_fixture(mocker: MockerFixture) -> AsyncMock:
    """Provide a faked Tasks API client with two files and two log groups."""
    return _fake_tasks_api(mocker)


@pytest.fixture(name="uploader")
def uploader_fixture(mocker: MockerFixture) -> _FakeUploader:
    """Provide a fake delivery executor in place of the configured one.

    The real factory is an async context manager owning a per-send transport, so
    the double is one too, and records its exit for the tests that assert the
    transport is released however the send ends.
    """
    fake = _FakeUploader()

    @asynccontextmanager
    async def _factory(
        plan: DeliveryPlan,
        *,
        step_observer: Callable[[StepRecord], None] | None = None,
    ) -> AsyncIterator[_FakeUploader]:
        fake.plan = plan
        fake.step_observer = step_observer
        try:
            yield fake
        finally:
            fake.released = True

    mocker.patch("app.extensions.apps.atw.send.get_delivery_executor", new=_factory)
    return fake


async def _seed_send_log(
    session: AsyncSession,
    *,
    executions: list[dict[str, Any]] | None = None,
    case_ref: str = "CS0001",
) -> AtwSendLog:
    """Persist an incident and one pending send log selecting ``executions``.

    :param session: The database session.
    :param executions: The selected execution descriptors; two by default.
    :param case_ref: The support-case reference to snapshot on the row.
    :return: The persisted pending send log.
    """
    incident = await AtwIncidentManager.save(
        session, AtwIncident(created_by="alice", name="Prod outage")
    )
    selected = executions or [
        {"id": str(uuid4()), "task_history_id": 11, "snippet_filename": "cpu.sh"},
        {"id": str(uuid4()), "task_history_id": 12, "snippet_filename": "mem.sh"},
    ]
    return await AtwSendLogManager.save(
        session,
        AtwSendLog(
            incident_id=incident.id,
            case_ref=case_ref,
            requested_by="alice",
            detail={"executions": selected},
        ),
    )


def _unconfigured_skeleton(plan: DeliveryPlan) -> DeliveryPlan:
    """Return ``plan`` with every declared secret emptied.

    :param plan: The configured plan to strip.
    :return: A skeleton that resolves as unconfigured until stored inputs supply
        the secret values.
    """
    return plan.model_copy(
        update={"secrets": dict.fromkeys(plan.secrets, SecretStr(""))}
    )


async def _seed_delivery_inputs(
    session: AsyncSession,
    secrets: dict[str, str],
    *,
    endpoint: str | None = None,
) -> None:
    """Store a delivery-inputs override carrying ``secrets``.

    :param session: The database session.
    :param secrets: The secret values keyed by name. The names must be exactly
        the ones the baked skeleton declares, or the materializer drops the row
        while the snapshot is built and delivery stays unconfigured.
    :param endpoint: Optional receiver base URL stored alongside the secrets.
    """
    value: dict[str, Any] = {"secrets": secrets}
    if endpoint is not None:
        value["endpoint"] = endpoint
    await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="DIAGNOSTICS_DELIVERY_INPUTS",
            value=value,
        ),
    )


async def _reload(session: AsyncSession, send_log_id: UUID) -> AtwSendLog:
    """Read a send log back through a session that never held the instance.

    :param session: The database session.
    :param send_log_id: The row to read.
    :return: The freshly-loaded row.
    """
    session.expunge_all()
    return await AtwSendLogManager.get_or_404(session, id=send_log_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("tasks_api")
class TestRunSendHappyPath:
    """Cover a send that reaches the receiver and records its evidence."""

    async def test_bundles_every_selected_execution_and_records_success(
        self, send_session: AsyncSession, uploader: _FakeUploader, tmp_path: Path
    ) -> None:
        """Stage one zip entry per file plus a manifest, then finalize successfully."""
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert reloaded.started_at is not None
        assert reloaded.finished_at is not None
        assert reloaded.detail["upload_response"] == _UPLOAD_DETAIL
        assert reloaded.detail["upload_reference"] == "att-9"
        assert reloaded.detail["file_count"] == _EXPECTED_FILE_COUNT
        assert reloaded.detail["bundle_size"] > 0
        assert list(tmp_path.glob("*.zip")) == []

    async def test_the_delivery_transport_is_released_when_the_send_lands(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Leave the executor's context so its per-send transport is closed."""
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert uploader.called is True
        assert uploader.released is True

    async def test_zip_carries_a_manifest_and_per_execution_entries(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Namespace every file under its execution and describe them in a manifest."""
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = sorted(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))

        assert len(names) == _EXPECTED_ENTRY_COUNT
        assert "11-cpu.sh/stdout.log" in names
        assert "12-mem.sh/diag/report.txt" in names
        assert manifest["case_ref"] == "CS0001"
        assert manifest["incident_name"] == "Prod outage"
        assert [entry["task_history_id"] for entry in manifest["executions"]] == [
            11,
            12,
        ]

    async def test_case_ref_snapshot_reaches_the_receiver(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Send the row's own case reference, not the incident's current one."""
        row = await _seed_send_log(send_session, case_ref="CS0999")

        await run_send(row.id)

        assert uploader.case_ref == "CS0999"

    async def test_resolution_steps_are_recorded_without_response_bodies(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Persist each observed step so a support engineer can read the trail."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(StepRecord(name="lookup", status="running"))
            uploader.step_observer(
                StepRecord(name="lookup", status="success", outputs={"sys_id": "c-1"})
            )
            return UploadResult(reference="att-9", detail=_UPLOAD_DETAIL)

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["steps"] == [
            {
                "name": "lookup",
                "kind": "resolution",
                "status": "running",
                "outputs": None,
            },
            {
                "name": "lookup",
                "kind": "resolution",
                "status": "success",
                "outputs": {"sys_id": "c-1"},
            },
        ]

    async def test_a_non_json_upload_response_still_succeeds(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Treat a landed bundle as sent even when the receiver answers non-JSON."""
        uploader.result = UploadResult(reference=None, detail=None)
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert reloaded.detail["upload_response"] is None
        assert reloaded.detail["upload_reference"] is None


@pytest.mark.asyncio
class TestRunSendArcnameCollisions:
    """Cover two executions producing identically-named files."""

    async def test_same_filename_in_two_executions_is_preserved_twice(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Keep both same-named files by namespacing each under its execution."""
        _fake_tasks_api(
            mocker, files={"stdout.log": {"is_dir": False, "size": 5}}, logs=[]
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = sorted(name for name in zf.namelist() if name != "manifest.json")

        assert names == ["11-cpu.sh/stdout.log", "12-mem.sh/stdout.log"]


@pytest.mark.asyncio
class TestRunSendEntryNaming:
    """Cover how upstream listing entries become archive member names."""

    async def test_a_directory_entry_is_named_for_the_archive_it_holds(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Write a directory member under a .tar.gz name so the receiver can open it."""
        _fake_tasks_api(
            mocker,
            files={
                "logs": {"is_dir": True, "size": 9},
                "stdout.log": {"is_dir": False, "size": 5},
            },
            logs=[],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = sorted(name for name in zf.namelist() if name != "manifest.json")

        assert names == ["11-cpu.sh/logs.tar.gz", "11-cpu.sh/stdout.log"]

    async def test_traversal_components_are_stripped_from_entry_names(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Keep every member inside its execution's namespace."""
        _fake_tasks_api(
            mocker, files={"/../../etc/passwd": {"is_dir": False, "size": 5}}, logs=[]
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = sorted(name for name in zf.namelist() if name != "manifest.json")

        assert names == ["11-cpu.sh/etc/passwd"]


@pytest.mark.asyncio
class TestRunSendExecutionLogs:
    """Cover the captured logs streamed into the bundle beside the output files."""

    async def test_each_step_and_stream_group_becomes_its_own_member(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Write one member per log group, carrying that group's messages."""
        _fake_tasks_api(mocker)
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            stdout = zf.read("11-cpu.sh/logs/run-script.stdout.log")
            stderr = zf.read("11-cpu.sh/logs/run-script.stderr.log")

        assert stdout == b"out-1\nout-2\n"
        assert stderr == b"err-1\n"

    @pytest.mark.usefixtures("uploader")
    async def test_only_the_main_step_is_requested(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Scope the fetch to the step carrying the snippet's own output."""
        api = _fake_tasks_api(mocker)
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        api.stream.assert_called_once_with(
            "/history/11/logs/", params={"step": _MAIN_STEP}
        )

    @pytest.mark.usefixtures("uploader")
    async def test_a_failed_execution_still_requests_only_the_main_step(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Keep the prestart-is-noise rule intact for every other status."""
        api = _fake_tasks_api(mocker, status=TaskHistoryStatusEnum.FAILED.value)
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        api.stream.assert_called_once_with(
            "/history/11/logs/", params={"step": _MAIN_STEP}
        )

    async def test_an_unlaunchable_execution_bundles_the_launch_check_log(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Send the one step that says what could not be launched, and where.

        An unlaunchable allocation never starts ``run-script`` and produces no
        output files, so streaming only the main step would leave the bundle
        empty and fail the send outright — with the diagnostic the support case
        exists for sitting in a step the default rule filters out.
        """
        api = _fake_tasks_api(
            mocker,
            files={},
            logs=[
                _log_record(
                    "EXTENSIONS_UNLAUNCHABLE: command=sudo node=node-1\n",
                    step=_LAUNCH_CHECK_STEP,
                )
            ],
            status=TaskHistoryStatusEnum.UNLAUNCHABLE.value,
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        api.stream.assert_called_once_with(
            "/history/11/logs/", params={"step": _LAUNCH_CHECK_STEP}
        )
        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            logged = zf.read(f"11-cpu.sh/logs/{_LAUNCH_CHECK_STEP}.stdout.log")

        assert logged == b"EXTENSIONS_UNLAUNCHABLE: command=sudo node=node-1\n"

    async def test_a_step_that_logged_nothing_leaves_no_member(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Record neither a member nor a manifest entry when the stream is empty."""
        _fake_tasks_api(mocker, logs=[])
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        assert uploader.manifest is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = zf.namelist()

        assert not any("/logs/" in name for name in names)
        assert uploader.manifest["executions"][0]["logs"] == []

    async def test_messages_carrying_no_content_never_open_a_member(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Skip empty and sentinel messages rather than attach a 0-byte member."""
        _fake_tasks_api(
            mocker,
            logs=[
                _log_record(""),
                _log_record(None),
                _log_record("", stream=TaskLogType.STDERR.value),
            ],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        assert uploader.manifest is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = zf.namelist()

        assert not any("/logs/" in name for name in names)
        assert uploader.manifest["executions"][0]["logs"] == []

    async def test_an_execution_with_only_logs_is_sendable(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Send a stdout-only diagnostic, which produced no output files at all."""
        _fake_tasks_api(mocker, files={}, logs=[_log_record("cpu: 42%\n")])
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert reloaded.detail["file_count"] == 0
        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            assert zf.read("11-cpu.sh/logs/run-script.stdout.log") == b"cpu: 42%\n"

    async def test_members_rotate_on_every_step_and_stream_change(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Keep each contiguous group in its own member as the groups change."""
        _fake_tasks_api(
            mocker,
            files={},
            logs=[
                _log_record("a-out\n", step="a"),
                _log_record("a-err\n", step="a", stream=TaskLogType.STDERR.value),
                _log_record("b-out\n", step="b"),
            ],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            members = {
                name: zf.read(name) for name in zf.namelist() if "/logs/" in name
            }

        assert len(members) == _EXPECTED_LOG_GROUP_COUNT
        assert members["11-cpu.sh/logs/a.stdout.log"] == b"a-out\n"
        assert members["11-cpu.sh/logs/a.stderr.log"] == b"a-err\n"
        assert members["11-cpu.sh/logs/b.stdout.log"] == b"b-out\n"

    async def test_a_stderr_first_stream_writes_both_members(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Handle the chunk store's stderr-first order, not one hardcoded sequence."""
        _fake_tasks_api(
            mocker,
            files={},
            logs=[
                _log_record("err\n", stream=TaskLogType.STDERR.value),
                _log_record("out\n"),
            ],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            stderr = zf.read("11-cpu.sh/logs/run-script.stderr.log")
            stdout = zf.read("11-cpu.sh/logs/run-script.stdout.log")

        assert stderr == b"err\n"
        assert stdout == b"out\n"

    async def test_a_whitespace_only_message_is_kept(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Treat a blank log line as real content rather than an absent message."""
        _fake_tasks_api(mocker, files={}, logs=[_log_record("\n")])
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            assert zf.read("11-cpu.sh/logs/run-script.stdout.log") == b"\n"

    async def test_blank_and_malformed_lines_are_skipped(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Drop lines that carry no usable record and keep streaming the group."""
        _fake_tasks_api(
            mocker,
            files={},
            logs=[
                _log_record("first\n"),
                b"\n",
                b"not json\n",
                b"[1, 2]\n",
                _log_record("second\n"),
            ],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            assert zf.read("11-cpu.sh/logs/run-script.stdout.log") == b"first\nsecond\n"

    async def test_traversal_components_are_stripped_from_log_member_names(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Keep a log member inside its execution's namespace whatever the step."""
        _fake_tasks_api(mocker, files={}, logs=[_log_record("x\n", step="../../etc")])
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.bundle_bytes is not None
        with zipfile.ZipFile(io.BytesIO(uploader.bundle_bytes)) as zf:
            names = sorted(name for name in zf.namelist() if name != "manifest.json")

        assert names == ["11-cpu.sh/logs/etc.stdout.log"]

    async def test_the_manifest_records_every_log_group(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Describe each written log group beside the execution's output files."""
        _fake_tasks_api(mocker)
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        assert uploader.manifest is not None
        entry = uploader.manifest["executions"][0]
        assert entry["logs"] == [
            {
                "step": _MAIN_STEP,
                "stream": TaskLogType.STDOUT.value,
                "arcname": "11-cpu.sh/logs/run-script.stdout.log",
                "size": len(b"out-1\nout-2\n"),
            },
            {
                "step": _MAIN_STEP,
                "stream": TaskLogType.STDERR.value,
                "arcname": "11-cpu.sh/logs/run-script.stderr.log",
                "size": len(b"err-1\n"),
            },
        ]
        assert [written["path"] for written in entry["files"]] == list(_DEFAULT_FILES)


@pytest.mark.asyncio
@pytest.mark.usefixtures("uploader")
class TestRunSendLogStatusGate:
    """Cover the finished-execution gate guarding the log fetch."""

    @pytest.mark.parametrize(
        "status",
        [
            TaskHistoryStatusEnum.RUNNING.value,
            TaskHistoryStatusEnum.LOST.value,
            "not-a-status",
        ],
    )
    async def test_an_unfinished_execution_is_never_streamed(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        mocker: MockerFixture,
        status: str,
    ) -> None:
        """Skip the log fetch rather than live-tail an execution still in flight.

        The output files are served normally so the send reaches the log branch:
        the status is then the only thing that can suppress the fetch, which is
        what makes the ``assert_not_called`` below evidence of the gate rather
        than of an earlier failure short-circuiting it.
        """
        api = _fake_tasks_api(mocker, status=status)
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        api.stream.assert_not_called()
        assert uploader.manifest is not None
        assert uploader.manifest["executions"][0]["logs"] == []
        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS

    async def test_a_status_fetch_failure_names_the_execution(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Fail loudly instead of shipping a bundle whose logs are silently absent."""
        api = _fake_tasks_api(mocker)
        api.get.side_effect = OSError("tasks api unreachable")
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == (
            "Could not read the status of execution 11 (cpu.sh): tasks api unreachable"
        )


@pytest.mark.asyncio
class TestRunSendLateDelivery:
    """Cover a task delivered after the stale sweep already failed its row."""

    async def test_a_row_the_sweep_already_failed_is_not_delivered_again(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        tasks_api: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Leave a terminal row alone rather than resurrecting and uploading it."""
        overrides_read = mocker.spy(SettingsOverrideManager, "list")
        row = await _seed_send_log(send_session)
        row.status = AtwSendStatusEnum.FAILED
        row.finished_at = utc_now()
        await AtwSendLogManager.save(send_session, row)

        await run_send(row.id)

        assert uploader.bundle_bytes is None
        tasks_api.get.assert_not_awaited()
        overrides_read.assert_not_called()
        reloaded = await _reload(send_session, row.id)
        assert reloaded.status == AtwSendStatusEnum.FAILED


@pytest.mark.asyncio
@pytest.mark.usefixtures("tasks_api")
class TestRunSendStaleSnapshot:
    """Cover a send whose worker snapshot predates a delivery-settings write."""

    async def test_stored_inputs_reach_the_send_after_a_forced_refresh(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        delivery_plan: DeliveryPlan,
        mocker: MockerFixture,
    ) -> None:
        """Deliver against inputs stored after this child last refreshed."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _unconfigured_skeleton(delivery_plan),
        )
        await _seed_delivery_inputs(
            send_session, dict.fromkeys(delivery_plan.secrets, _STORED_SECRET)
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert uploader.called is True

    async def test_a_plan_resolving_on_the_first_read_is_re_read_anyway(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        mocker: MockerFixture,
    ) -> None:
        """Read the override row again even when the held snapshot already resolves."""
        overrides_read = mocker.spy(SettingsOverrideManager, "list")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert uploader.called is True
        assert overrides_read.await_count == 1

    async def test_a_rotated_secret_and_endpoint_reach_the_send(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        delivery_plan: DeliveryPlan,
    ) -> None:
        """Deliver against stored inputs that differ from a still-valid stale snapshot.

        A stale-but-valid snapshot resolves on the first read, so a refresh gated on
        an unresolved plan would never fire; the send must still pick up the rotated
        secret and the repointed endpoint from the stored row.

        The stale values are published as a proxy snapshot rather than patched onto
        the wrapped instance, so the snapshot precedence a worker child actually
        holds is what the republish has to supersede.
        """
        rotated_secret = "rotated-api-key"
        new_endpoint = "https://intake-rotated.example.com"
        extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
            {
                "DIAGNOSTICS_DELIVERY_INPUTS": DeliveryPlanInputs(
                    endpoint="https://intake-stale.example.com",
                    secrets=dict.fromkeys(
                        delivery_plan.secrets, SecretStr("stale-api-key")
                    ),
                )
            }
        )
        await _seed_delivery_inputs(
            send_session,
            dict.fromkeys(delivery_plan.secrets, rotated_secret),
            endpoint=new_endpoint,
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert uploader.plan is not None
        assert str(uploader.plan.endpoint).rstrip("/") == new_endpoint
        assert {
            name: secret.get_secret_value()
            for name, secret in uploader.plan.secrets.items()
        } == dict.fromkeys(delivery_plan.secrets, rotated_secret)


@pytest.mark.asyncio
class TestRunSendFailures:
    """Cover every failure family reaching a terminal failed row."""

    async def test_unconfigured_delivery_at_worker_time_fails_the_row(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Fail cleanly when the receiver was unconfigured after the request."""
        mocker.patch.object(extensions_settings, "DIAGNOSTICS_DELIVERY", None)
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "not configured" in reloaded.detail["error"]

    async def test_a_refresh_that_finds_no_stored_inputs_fails_the_row(
        self,
        send_session: AsyncSession,
        delivery_plan: DeliveryPlan,
        mocker: MockerFixture,
    ) -> None:
        """Fail cleanly when the refresh finds nothing that configures delivery."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _unconfigured_skeleton(delivery_plan),
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "not configured" in reloaded.detail["error"]

    async def test_stored_inputs_naming_a_drifted_secret_fail_the_row(
        self,
        send_session: AsyncSession,
        delivery_plan: DeliveryPlan,
        mocker: MockerFixture,
    ) -> None:
        """Blame the drift on the terminal row, not the never-configured state.

        Inverts the previous contract, under which this row read exactly like a
        deployment that never configured delivery — yet re-supplying the inputs
        and configuring delivery from scratch are opposite actions.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _unconfigured_skeleton(delivery_plan),
        )
        await _seed_delivery_inputs(send_session, {"renamed_key": _STORED_SECRET})
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == DRIFTED_INPUTS_REASON

    async def test_a_failing_refresh_still_writes_a_terminal_row(
        self,
        send_session: AsyncSession,
        delivery_plan: DeliveryPlan,
        mocker: MockerFixture,
    ) -> None:
        """Land the terminal row when the override read itself fails.

        The failure originates inside the real query path rather than in a
        mocked helper, so the rollback runs against the session that then has to
        carry the terminal write, which is why the row is read back through a
        separate session instead of the in-flight instance.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _unconfigured_skeleton(delivery_plan),
        )
        mocker.patch.object(
            SettingsOverrideManager,
            "list",
            side_effect=SQLAlchemyError("database unreachable"),
        )
        rollback = mocker.spy(AsyncSession, "rollback")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert rollback.await_count == 1
        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "not configured" in reloaded.detail["error"]

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_failing_refresh_still_delivers_against_the_held_snapshot(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        mocker: MockerFixture,
    ) -> None:
        """Keep delivering when the re-read fails but the held snapshot is usable.

        A transient database error must not convert a deliverable send into a
        terminal "not configured" row once the re-read is unconditional.
        """
        mocker.patch.object(
            SettingsOverrideManager,
            "list",
            side_effect=SQLAlchemyError("database unreachable"),
        )
        rollback = mocker.spy(AsyncSession, "rollback")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert rollback.await_count == 1
        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert uploader.called is True

    async def test_a_failing_refresh_keeps_the_reason_already_resolved(
        self,
        send_session: AsyncSession,
        delivery_plan: DeliveryPlan,
        mocker: MockerFixture,
    ) -> None:
        """Keep blaming the drift when the re-read that might clear it fails.

        The refresh is what would notice the operator re-supplying the inputs.
        A transient database error means it noticed nothing — not that the
        drift the worker's own snapshot already shows has gone away.
        """
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            _unconfigured_skeleton(delivery_plan),
        )
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY_INPUTS",
            DeliveryPlanInputs(secrets={"renamed_key": _STORED_SECRET}),
        )
        mocker.patch.object(
            SettingsOverrideManager,
            "list",
            side_effect=SQLAlchemyError("database unreachable"),
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == DRIFTED_INPUTS_REASON

    @pytest.mark.usefixtures("uploader")
    async def test_a_files_listing_conflict_names_the_execution(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Fail naming the execution whose output files are not ready."""
        _fake_tasks_api(
            mocker,
            files_listing_error=HTTPConflictException(detail="Task is still running"),
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == (
            "Could not list output files for execution 11 (cpu.sh): "
            "Task is still running"
        )

    async def test_zero_files_and_zero_log_bytes_sends_nothing(
        self, send_session: AsyncSession, uploader: _FakeUploader, mocker: MockerFixture
    ) -> None:
        """Refuse to upload a manifest-only bundle when nothing was collected."""
        _fake_tasks_api(mocker, files={}, logs=[])
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "nothing to send" in reloaded.detail["error"].lower()
        assert "output files" not in reloaded.detail["error"]
        assert uploader.called is False

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_delivery_plan_error_is_recorded_verbatim(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Record the executor's own message when the plan cannot be carried out."""
        uploader.error = DeliveryPlanError("Bundle is 99 bytes, above the cap.")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == "Bundle is 99 bytes, above the cap."

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_failed_resolution_step_is_persisted_with_its_terminal_record(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Persist the failing step's terminal record so the trail names its end."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(StepRecord(name="lookup", status="running"))
            uploader.step_observer(StepRecord(name="lookup", status="failed"))
            raise HTTPConflictException(detail="ticket locked")

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["steps"] == [
            {
                "name": "lookup",
                "kind": "resolution",
                "status": "running",
                "outputs": None,
            },
            {
                "name": "lookup",
                "kind": "resolution",
                "status": "failed",
                "outputs": None,
            },
        ]

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_failed_send_names_the_step_and_the_input_it_reads(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Name the failed step and its input ahead of the receiver's message."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(
                StepRecord(name="lookup", status="failed", cited_inputs=("case_ref",))
            )
            raise HTTPBadRequestException(
                detail="An unexpected error occurred on the server."
            )

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["error"] == (
            "The send failed in delivery step 'lookup', which reads case_ref: "
            "An unexpected error occurred on the server."
        )

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_failed_upload_is_attributed_to_the_upload_not_the_last_step(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Name the bundle upload when the send died there, not the step before it."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(
                StepRecord(name="lookup", status="success", outputs={"sys_id": "c-1"})
            )
            uploader.step_observer(
                StepRecord(name="upload", status="failed", kind="upload")
            )
            raise HTTPConflictException(detail="attachment rejected")

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["error"] == (
            "The send failed in the bundle upload: attachment rejected"
        )

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_step_reading_no_input_is_named_without_an_input_clause(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Name a step that reads nothing the send supplied without an input clause."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(StepRecord(name="lookup", status="failed"))
            raise HTTPConflictException(detail="ticket locked")

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["error"] == (
            "The send failed in delivery step 'lookup': ticket locked"
        )

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_step_reading_two_inputs_names_both(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Join every input a failed step reads into the one recorded reason."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(
                StepRecord(
                    name="lookup",
                    status="failed",
                    cited_inputs=("case_ref", "manifest.incident_id"),
                )
            )
            raise HTTPConflictException(detail="ticket locked")

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["error"] == (
            "The send failed in delivery step 'lookup', which reads case_ref, "
            "manifest.incident_id: ticket locked"
        )

    @pytest.mark.usefixtures("tasks_api")
    async def test_a_failure_before_any_step_keeps_the_upstream_message_alone(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Record the receiver's own message when no step ever reported a failure."""
        uploader.error = HTTPConflictException(detail="ticket locked")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.detail["error"] == "ticket locked"

    @pytest.mark.usefixtures("tasks_api")
    async def test_no_secret_reaches_the_recorded_error_or_the_step_trail(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Keep the plan's secret values out of everything a failed send persists."""
        row = await _seed_send_log(send_session)

        async def _upload(**kwargs: Any) -> UploadResult:
            assert uploader.step_observer is not None
            uploader.step_observer(
                StepRecord(name="lookup", status="failed", cited_inputs=("case_ref",))
            )
            raise HTTPConflictException(detail="ticket locked")

        uploader.upload_bundle = _upload

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert uploader.plan is not None
        secrets = [
            secret.get_secret_value() for secret in uploader.plan.secrets.values()
        ]
        assert secrets
        trail = json.dumps(reloaded.detail["steps"])
        assert all(
            secret not in reloaded.detail["error"] and secret not in trail
            for secret in secrets
        )

    @pytest.mark.usefixtures("tasks_api")
    async def test_an_unexpected_error_still_writes_a_terminal_row(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Land a terminal row even when an unforeseen failure ends the send."""
        uploader.error = RuntimeError("upstream exploded")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "upstream exploded" in reloaded.detail["error"]

    @pytest.mark.usefixtures("tasks_api")
    async def test_the_delivery_transport_is_released_when_the_send_fails(
        self, send_session: AsyncSession, uploader: _FakeUploader
    ) -> None:
        """Close the per-send transport on the failure path too, not only on success."""
        uploader.error = RuntimeError("upstream exploded")
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        assert (await _reload(send_session, row.id)).status is AtwSendStatusEnum.FAILED
        assert uploader.released is True

    @pytest.mark.usefixtures("tasks_api", "uploader")
    async def test_a_missing_row_exits_without_raising(
        self, send_session: AsyncSession
    ) -> None:
        """Exit quietly when the incident (and its row) was deleted mid-flight."""
        await run_send(uuid4())

        assert await AtwSendLogManager.count(send_session) == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("uploader")
class TestRunSendUpstreamFailures:
    """Cover how an upstream Tasks-API failure is reported per collection step.

    The rendered text is asserted in full rather than by containment: these
    messages are a support engineer's only account of why a send failed, and a
    substring check would not notice a step naming the wrong thing.
    """

    async def test_an_entry_stream_failure_names_the_file(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Name the file that could not be streamed, not merely its execution."""
        _fake_tasks_api(
            mocker,
            files={"diag/report.txt": {"is_dir": False, "size": 7}},
            file_stream_error=ClientError("connection reset"),
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == (
            "Could not read 'diag/report.txt' from execution 11 (cpu.sh): "
            "connection reset"
        )

    async def test_a_log_stream_failure_names_the_execution(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Fail naming the execution whose logs the upstream refused."""
        _fake_tasks_api(
            mocker, log_stream_error=HTTPBadRequestException(detail="Logs unavailable")
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == (
            "Could not read logs for execution 11 (cpu.sh): Logs unavailable"
        )

    async def test_a_log_stream_failing_mid_group_still_fails_cleanly(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Report the upstream cut, not the open archive member it interrupted.

        The stream is cut only after a member is already open, which is the one
        arrangement where abandoning the member could surface a :mod:`zipfile`
        error ahead of the real cause.
        """
        api = _fake_tasks_api(mocker, files={})
        api.stream.side_effect = lambda *_a, **_k: _ndjson_then_raise(
            [_log_record("out-1\n"), _log_record("out-2\n")],
            ClientError("connection reset"),
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == (
            "Could not read logs for execution 11 (cpu.sh): connection reset"
        )

    async def test_an_overlong_log_line_is_not_reported_as_an_upstream_failure(
        self, send_session: AsyncSession, mocker: MockerFixture
    ) -> None:
        """Let the line-cap error stand on its own instead of blaming the upstream.

        ``RemoteAPI.stream`` raises ``ValueError`` part-way through for a log line
        outgrowing its cap. That is a local limit, not an upstream fault, so it
        must not be dressed up as one.
        """
        api = _fake_tasks_api(mocker, files={})
        api.stream.side_effect = lambda *_a, **_k: _ndjson_then_raise(
            [_log_record("out-1\n")],
            ValueError("log line exceeds the line cap"),
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert reloaded.detail["error"] == "log line exceeds the line cap"


@pytest.mark.asyncio
@pytest.mark.usefixtures("uploader")
class TestRunSendSizeCap:
    """Cover the configured bundle-size cap enforced while the zip is built."""

    async def test_an_oversized_bundle_aborts_before_any_upload(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        mocker: MockerFixture,
        tmp_path: Path,
        delivery_plan: DeliveryPlan,
    ) -> None:
        """Stop mid-stream, delete the partial zip, and never reach the receiver."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            delivery_plan.model_copy(update={"max_bundle_size_mb": 1}),
        )
        api = _fake_tasks_api(
            mocker, files={"big.bin": {"is_dir": False, "size": 1}}, logs=[]
        )
        api.stream_chunks.side_effect = lambda *_a, **_k: _blocks(
            os.urandom(2 * 1024 * 1024)
        )
        row = await _seed_send_log(send_session)

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "1 MiB" in reloaded.detail["error"]
        assert not reloaded.detail["error"].startswith("Could not read")
        assert uploader.called is False
        assert list(tmp_path.glob("*.zip")) == []

    async def test_an_oversized_log_stream_aborts_before_any_upload(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        mocker: MockerFixture,
        tmp_path: Path,
        delivery_plan: DeliveryPlan,
    ) -> None:
        """Stop while streaming logs, delete the partial zip, and never upload."""
        mocker.patch.object(
            extensions_settings,
            "DIAGNOSTICS_DELIVERY",
            delivery_plan.model_copy(update={"max_bundle_size_mb": 1}),
        )
        noise = os.urandom(2 * 1024 * 1024).hex()
        block = 64 * 1024
        _fake_tasks_api(
            mocker,
            files={},
            logs=[
                _log_record(noise[start : start + block])
                for start in range(0, len(noise), block)
            ],
        )
        row = await _seed_send_log(send_session, executions=[_ONE_EXECUTION])

        await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.FAILED
        assert "1 MiB" in reloaded.detail["error"]
        assert uploader.called is False
        assert list(tmp_path.glob("*.zip")) == []


@pytest.mark.asyncio
class TestFailStaleSends:
    """Cover the sweep that drives abandoned sends to a terminal status."""

    async def test_drives_aged_active_rows_to_failed(
        self, send_session: AsyncSession
    ) -> None:
        """Fail pending and running rows whose worker never came back."""
        incident = await AtwIncidentManager.save(
            send_session, AtwIncident(created_by="alice")
        )
        aged = utc_now() - timedelta(hours=4)
        for status in (AtwSendStatusEnum.PENDING, AtwSendStatusEnum.RUNNING):
            await AtwSendLogManager.save(
                send_session,
                AtwSendLog(
                    incident_id=incident.id,
                    case_ref="CS0001",
                    requested_by="alice",
                    status=status,
                    created_at=aged,
                    detail={},
                ),
            )

        swept = await fail_stale_sends(timedelta(hours=1))

        send_session.expunge_all()
        rows = await AtwSendLogManager.list(send_session, incident_id=incident.id)
        assert swept == _STALE_ROW_COUNT
        assert {row.status for row in rows} == {AtwSendStatusEnum.FAILED}
        assert all("worker" in row.detail["error"].lower() for row in rows)

    async def test_leaves_fresh_and_terminal_rows_alone(
        self, send_session: AsyncSession
    ) -> None:
        """Spare a young in-flight send and any row that already finished."""
        incident = await AtwIncidentManager.save(
            send_session, AtwIncident(created_by="alice")
        )
        fresh = await AtwSendLogManager.save(
            send_session,
            AtwSendLog(
                incident_id=incident.id,
                case_ref="CS0001",
                requested_by="alice",
                status=AtwSendStatusEnum.RUNNING,
                detail={},
            ),
        )
        done = await AtwSendLogManager.save(
            send_session,
            AtwSendLog(
                incident_id=incident.id,
                case_ref="CS0002",
                requested_by="alice",
                status=AtwSendStatusEnum.SUCCESS,
                created_at=utc_now() - timedelta(hours=4),
                detail={},
            ),
        )

        swept = await fail_stale_sends(timedelta(hours=1))

        send_session.expunge_all()
        assert swept == 0
        assert (
            await AtwSendLogManager.get_or_404(send_session, id=fresh.id)
        ).status is AtwSendStatusEnum.RUNNING
        assert (
            await AtwSendLogManager.get_or_404(send_session, id=done.id)
        ).status is AtwSendStatusEnum.SUCCESS


class TestPurgeExpiredBundles:
    """Cover the on-disk sweep of staged bundles."""

    def test_removes_expired_bundles_and_keeps_fresh_ones(
        self, mocker: MockerFixture, tmp_path: Path
    ) -> None:
        """Reap a bundle past its TTL while leaving a just-written one in place."""
        mocker.patch("app.extensions.apps.atw.send.bundle_dir", return_value=tmp_path)
        expired = tmp_path / "old.zip"
        expired.write_bytes(b"old")
        os.utime(expired, (0, 0))
        fresh = tmp_path / "new.zip"
        fresh.write_bytes(b"new")

        removed = purge_expired_bundles(3600)

        assert removed == 1
        assert not expired.exists()
        assert fresh.exists()

    def test_a_missing_directory_purges_nothing(
        self, mocker: MockerFixture, tmp_path: Path
    ) -> None:
        """Treat an unused staging directory as empty rather than an error."""
        mocker.patch(
            "app.extensions.apps.atw.send.bundle_dir", return_value=tmp_path / "absent"
        )

        assert purge_expired_bundles(3600) == 0


@pytest.mark.asyncio
async def test_get_tasks_api_requests_the_tasks_client_log_policy(
    mocker: MockerFixture,
) -> None:
    """Assert the Tasks client is requested from the registry with both log settings.

    The registry keys a client on its endpoint and SSL settings alone, so a site
    omitting the policy can hand every later site a client that logs in full.

    :param mocker: The patching fixture standing in for the client registry.
    """
    get_remote_api = mocker.patch.object(
        Settings,
        "get_remote_api",
        new=AsyncMock(return_value=MagicMock(spec=RemoteAPI)),
    )

    await get_tasks_api()

    tasks_calls = [
        awaited.kwargs
        for awaited in get_remote_api.await_args_list
        if awaited.kwargs["endpoint"] == extensions_settings.TASKS_ENDPOINT
    ]
    assert len(tasks_calls) == 1
    assert tasks_calls[0]["withhold_body_log"] is True


@pytest.mark.asyncio
class TestTasksClientHeldForTheSend:
    """Cover the Tasks client a send stages its bundle through being held."""

    async def test_send_survives_rebind_mid_bundle(
        self,
        send_session: AsyncSession,
        uploader: _FakeUploader,
        isolated_client_registry: ClientRegistry,
    ) -> None:
        """Stage the second execution on the client the first one retired.

        The first upstream read retires the Tasks client, as an override rebind
        would. The send holds it, so its close waits for the bundle to finish
        rather than failing the next execution's reads.
        """
        row = await _seed_send_log(send_session)
        client = await get_tasks_api()
        endpoint = str(extensions_settings.TASKS_ENDPOINT).rstrip("/")
        rebinds: list[str] = []

        async def upstream_reply(url: Any, **_kwargs: Any) -> CallbackResult:
            if not rebinds:
                rebinds.append(str(url))
                await isolated_client_registry.invalidate(endpoint)
            if url.path.endswith("/files/"):
                return CallbackResult(payload={"out.txt": {"is_dir": False}})
            if url.path.endswith("/file/"):
                return CallbackResult(body=b"data!")
            return CallbackResult(
                payload={"status": TaskHistoryStatusEnum.RUNNING.value}
            )

        with aioresponses() as upstream:
            upstream.get(
                re.compile(rf"{re.escape(endpoint)}/history/.*"),
                callback=upstream_reply,
                repeat=True,
            )
            await run_send(row.id)

        reloaded = await _reload(send_session, row.id)
        assert reloaded.status is AtwSendStatusEnum.SUCCESS
        assert reloaded.detail["file_count"] == len(row.detail["executions"])
        assert client._session is None
        assert await get_tasks_api() is not client

    async def test_close_all_force_closes_a_held_run(
        self, isolated_client_registry: ClientRegistry
    ) -> None:
        """Let shutdown end a held client, so a hold cannot outlive the process."""
        endpoint = str(extensions_settings.TASKS_ENDPOINT).rstrip("/")

        async with tasks_api_session() as tasks_api:
            await isolated_client_registry.invalidate(endpoint)
            await isolated_client_registry.close_all()

            with pytest.raises(RuntimeError, match="is closed"):
                await tasks_api.get("/history/1")
