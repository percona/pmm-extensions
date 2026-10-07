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

"""Define test fixtures for tasks tests."""

from collections.abc import AsyncGenerator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import undefer
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.pool import StaticPool
from starlette.testclient import TestClient

from app.api.deps import get_current_user, require_minimum_role_for_unsafe_methods
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.sql_types import AutoJSON
from app.core.db.utils import get_async_session_maker_from_engine
from app.core.utils import json_serializer
from app.core.utils.fields import DatabaseDialect
from app.tasks.crud import TaskHistoryManager, TaskManager
from app.tasks.deps import get_request_executor, get_session
from app.tasks.execution.executors.nomad.models import NomadExecutor
from app.tasks.execution.models import BaseExecutor
from app.tasks.main import tasks_app
from app.tasks.models import TaskHistory, TaskWrite
from tests.app.conftest import postgres_worker_schema
from tests.app.db_schema import apply_schema
from tests.app.factories import build_task_history, TaskFactory
from tests.app.tasks.nomad_log_stub import NomadLogStub

#: Syncer names in ``BaseSyncer.get_name()`` form, as the inventory-sync settings and
#: the schedules seeded from them spell a syncer. Shared so the tasks suite has one
#: copy: the tasks service never imports the extensions syncers, so these cannot be derived
#: from the classes here.
PMM_SYNCER = "app.extensions.sync.syncers.pmm.PMMSyncer"
MYSQL_SYNCER = "app.extensions.sync.syncers.mysql.syncer.MySQLSyncer"
SYSTEM_FACTS_SYNCER = (
    "app.extensions.sync.syncers.system_facts.syncer.SystemFactsSyncer"
)
UNMEASURED_HOST_FACTS_SYNCER = (
    "app.extensions.sync.syncers.system_facts.syncer.UnmeasuredHostFactsSyncer"
)

#: The per-task hook-path fields the ``TaskWrite`` allow-list constrains.
HOOK_PATH_FIELDS = ("alert_detail_builder", "run_result_recorder")

#: Hook paths the allow-list must reject at every write boundary.
REJECTED_HOOK_PATHS = (
    "os:system",
    "builtins:eval",
    "no_colon_here",
    ":build_owner_alert_details",
    "app.extensions.apps.archives.alerts:",
    ":",
)


@pytest_asyncio.fixture(name="session")
async def session_fixture() -> AsyncGenerator[AsyncSession, None]:
    """Create an async db session for testing."""
    # scaffolding-dup-ok: this duplication predates the change that
    # re-annotated the fixture's return type; promoting it against
    # its sibling bootstrap is a cross-tree refactor of its own.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        json_serializer=json_serializer,
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await apply_schema(conn, SQLModel.metadata)
    async_session_maker = get_async_session_maker_from_engine(engine)
    try:
        async with async_session_maker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def nomad_stub() -> Iterator[NomadLogStub]:
    """Serve a running ``run-script`` step from a Nomad stub on its own thread.

    :return: The started stub.
    """
    stub = NomadLogStub()
    stub.start()
    yield stub
    stub.stop()


@pytest_asyncio.fixture
async def live_executor(nomad_stub: NomadLogStub) -> AsyncGenerator[NomadExecutor]:
    """Enter a real Nomad executor pointed at the stub, as the lifecycle does.

    :return: The entered executor.
    """
    executor = await NomadExecutor(
        endpoint=nomad_stub.endpoint, verify_ssl=False
    ).open()
    yield executor
    await executor.close()


@pytest.fixture
def mock_executor() -> AsyncMock:
    """Return a mock executor with spec of BaseExecutor."""
    executor = AsyncMock(spec=BaseExecutor)
    executor.get_hosts = AsyncMock(return_value={"node1": "10.0.0.1"})
    executor.preflight_stream_logs = AsyncMock(return_value=None)
    executor.get_events = MagicMock(return_value=[])
    return executor


@pytest.fixture
def test_client(
    regular_user: CasdoorUser, session: AsyncSession, mock_executor: AsyncMock
) -> Iterator[TestClient]:
    """Create an authenticated test client for the app.

    Mirrors the PMM Extensions ``test_client``'s ``require_minimum_role_for_unsafe_methods``
    override so the non-admin fixture user can exercise a mutating route.
    """
    tasks_app.dependency_overrides[require_minimum_role_for_unsafe_methods] = (
        lambda: None
    )
    tasks_app.dependency_overrides[get_current_user] = lambda: regular_user
    tasks_app.dependency_overrides[get_session] = lambda: session
    tasks_app.dependency_overrides[get_request_executor] = lambda: mock_executor
    yield TestClient(tasks_app)
    tasks_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def created_task_with_history(session: AsyncSession) -> TaskHistory:
    """Return a task with a related task history record saved in the database."""
    task = await TaskManager.create(
        session,
        TaskWrite.model_validate(
            TaskFactory.build(name="history-task", output_files_path="/output")
        ),
    )
    saved = await TaskHistoryManager.save(session, build_task_history(task))
    return await TaskHistoryManager.get_or_404(
        session,
        select_related=(TaskHistory.task,),
        query_options=[undefer(TaskHistory.execution_request)],
        id=saved.id,
    )


def raw_task_history(session: AsyncSession) -> sa.TableClause:
    """Return ``taskhistory`` as a migration sees it, qualified for ``session``.

    ``AutoJSON`` decodes the stored document on either dialect without running
    the ORM column type, which would decrypt on read and re-encrypt on write:
    the very transform under test.

    The schema is spelled out on PostgreSQL because a lightweight ``sa.table``
    carries none, and ``schema_translate_map`` (how the real-PostgreSQL fixtures
    give each xdist worker its own schema) rewrites only constructs that do.
    Left unqualified the statement resolves against ``search_path`` and
    reports the table as missing, on the deployment dialect alone.

    :param session: The session the statement will run on.
    :return: The table clause to select from and update.
    """
    dialect = session.get_bind().name
    return sa.table(
        "taskhistory",
        sa.column("id", sa.Integer),
        sa.column("execution_request", AutoJSON),
        schema=(
            postgres_worker_schema()
            if dialect.startswith(DatabaseDialect.POSTGRESQL)
            else None
        ),
    )


async def stored_execution_request(session: AsyncSession, history_id: int) -> Any:
    """Return a row's ``execution_request`` exactly as it is stored.

    :param session: The session to read through.
    :param history_id: The row to read.
    :return: The stored value, undecrypted.
    """
    table = raw_task_history(session)
    connection = await session.connection()
    result = await connection.execute(
        sa.select(table.c.execution_request).where(table.c.id == history_id)
    )
    return result.scalar_one()


async def overwrite_execution_request(
    session: AsyncSession, history_id: int, document: Any
) -> None:
    """Store ``document`` verbatim, bypassing the ORM column type's encryption.

    The route a test takes to plant a leaf the configured key cannot read, which
    the write path refuses to produce by construction.

    :param session: The session to write through.
    :param history_id: The row to overwrite.
    :param document: The exact document to store.
    """
    table = raw_task_history(session)
    connection = await session.connection()
    await connection.execute(
        table.update()
        .where(table.c.id == history_id)
        .values(execution_request=document)
    )
    await session.commit()


#: Default plaintext leaves for the shared execution-request fixture below. Each
#: carries a credential, because that is what makes the leaf protected: ``args``
#: as a command-line flag, ``config`` as a plain-string form field an app
#: serialised, ``payload`` as the submitted document's reference.
EXECUTION_REQUEST_ARGS = "restore --password hunter2"
EXECUTION_REQUEST_CONFIG = "master_password: hunter2\nmaster_host: db-1\n"
EXECUTION_REQUEST_PAYLOAD = "file://snippets/foo.py"


def request_document(
    args: Any = EXECUTION_REQUEST_ARGS,
    payload: Any = EXECUTION_REQUEST_PAYLOAD,
    config: Any = EXECUTION_REQUEST_CONFIG,
) -> dict[str, Any]:
    """Return the stored JSON shape of an execution request.

    Carries every protected leaf populated, so a caller asserting "each one was
    rewritten" is asserting over the whole inventory rather than over whichever
    subset the fixture happens to spell, plus a plaintext ``meta`` sibling so it
    can also assert that nothing else was touched.

    :param args: The value to place at ``$.meta.args``.
    :param payload: The value to place at ``$.payload``.
    :param config: The value to place at ``$.meta.config``.
    :return: The row's stored ``execution_request`` document.
    """
    return {
        "task": "run-python",
        "target": "node-1",
        "meta": {"args": args, "config": config, "_service_name": "mysql-1"},
        "payload": payload,
        "tracking": {"allocation_id": None, "evaluation_id": None},
    }


def undecryptable_document(
    task_name: str, *, target: str = "node-1", args: str | None = None
) -> dict[str, Any]:
    """Return a stored document whose ``payload`` this key cannot decrypt.

    A foreign Fernet token: structurally ciphertext, so the read path recognises
    it as encrypted and reports it unreadable rather than passing it through.

    :param task_name: The execution request's task name.
    :param target: The execution request's target.
    :param args: The plaintext ``meta.args`` to store, or ``None`` for none.
    :return: The document to store verbatim.
    """
    return {
        "task": task_name,
        "target": target,
        "meta": {"target": target, **({} if args is None else {"args": args})},
        "payload": Fernet(Fernet.generate_key())
        .encrypt(b'"unreadable"')
        .decode("ascii"),
    }
