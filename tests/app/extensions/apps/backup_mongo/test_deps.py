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

"""Define tests for the app.extensions.apps.backup_mongo.deps module."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import (
    HTTPConflictException,
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.core.requests.remote_api import RemoteAPI
from app.extensions.apps.backup_mongo.deps import (
    _fetch_latest_pbm_status,
    build_backup_mongo_api_task_response,
    build_backup_task_payload,
    ensure_backup_derived_siblings,
    ensure_backup_group_update_preserves_names,
)
from app.extensions.apps.backup_mongo.models import (
    BackupCreate,
    BackupTaskWrite,
    BackupType,
)
from app.extensions.apps.framework.schema import DerivedTask
from app.extensions.inventory import CreatedService
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import Task, TaskWrite
from tests.app.extensions.path_unsafe_task_names import SUFFIXED_UNSAFE_TASKS
from tests.app.factories import (
    MOCK_ACTOR_USERNAMES,
    MOCK_CREATOR_ID,
    MOCK_UPDATER_ID,
    TaskFactory,
)


def _backup_mongo_task() -> Task:
    """Build a parent ``pbm_config`` task recorded by two known users."""
    return TaskFactory.build(
        name="mongo-backup",
        owner="BACKUP_MONGO",
        data={
            "meta": {"target": "mongo-host"},
            "backup_type": BackupType.PBM_CONFIG.value,
        },
        created_by=MOCK_CREATOR_ID,
        last_updated_by=MOCK_UPDATER_ID,
    )


@pytest.mark.asyncio
async def test_build_backup_task_payload_includes_service_name(
    mocker,
    mock_remote_api,
    backup_create: BackupCreate,
    mongo_service: CreatedService,
):
    """build_backup_task_payload fetches a MONGODB service and tags meta with its name."""
    get_created_entity = mocker.patch(
        "app.extensions.apps.backup_mongo.deps.get_created_entity",
        return_value=mongo_service,
    )

    task_payload = await build_backup_task_payload(backup_create, mock_remote_api)

    assert isinstance(task_payload, TaskWrite)
    assert task_payload.owner == "BACKUP_MONGO"
    assert task_payload.data["meta"]["_service_name"] == mongo_service.name

    get_created_entity.assert_awaited_once_with(
        mock_remote_api,
        SyncInventoryEntityTypeEnum.SERVICE,
        backup_create.service_id,
        type=ServiceTypeEnum.MONGODB,
    )


@pytest.mark.asyncio
async def test_build_backup_task_payload_swallows_404_for_missing_service(
    mocker,
    mock_remote_api,
    backup_create: BackupCreate,
):
    """Resolve a stale service_id (deleted service) to a node-only annotation.

    ``RemoteAPI.get`` raises the project's ``HTTPNotFoundException`` on 404, which
    the narrowed handler swallows to fall back to a node-only annotation.
    """
    mocker.patch(
        "app.extensions.apps.backup_mongo.deps.get_created_entity",
        side_effect=HTTPNotFoundException(),
    )

    task_payload = await build_backup_task_payload(backup_create, mock_remote_api)

    assert isinstance(task_payload, TaskWrite)
    assert "_service_name" not in task_payload.data["meta"]


class TestBackupGroupRenameGuard:
    """Tests for ensure_backup_group_update_preserves_names."""

    def test_allows_matching_name(self) -> None:
        """Accept a submitted task_name equal to the parent name."""
        ensure_backup_group_update_preserves_names("parent-backup", "parent-backup")

    def test_rejects_rename(self) -> None:
        """Raise 409 when the submitted task_name differs from the parent name."""
        with pytest.raises(HTTPConflictException):
            ensure_backup_group_update_preserves_names("parent-backup", "renamed")


class TestBackupFormRoundTrip:
    """Guard the create-stamp -> edit-PUT body round-trip."""

    def test_stamped_create_form_revalidates_as_put_body(
        self, backup_create: BackupCreate
    ) -> None:
        """Re-validate the JSON-mode create form as the PUT body model.

        The generic edit page resubmits the stored ``_form`` (a
        :class:`BackupCreate` dump, carrying ``backup_type``) as the PUT body;
        :class:`BackupTaskWrite` must accept it, dropping the extra
        ``backup_type`` rather than rejecting it.
        """
        stamped = backup_create.model_dump(mode="json")

        body = BackupTaskWrite.model_validate(stamped)

        assert body.task_name == backup_create.task_name
        assert body.service_id == backup_create.service_id
        assert not hasattr(body, "backup_type")


class TestBuildBackupMongoApiTaskResponse:
    """Cover the backup_mongo list-row builder's actor resolution."""

    def test_resolves_actors_through_the_context(self):
        """Render both actors as usernames and keep the app's own extras."""
        response = build_backup_mongo_api_task_response(
            _backup_mongo_task(), context=MOCK_ACTOR_USERNAMES
        )

        assert (response.created_by, response.last_updated_by) == ("alice", "bob")
        assert (response.hostname, response.backup_type) == (
            "mongo-host",
            BackupType.PBM_CONFIG.value,
        )

    def test_keeps_raw_ids_without_a_context(self):
        """Serve the stored identifiers when no username map is bound."""
        response = build_backup_mongo_api_task_response(_backup_mongo_task())

        assert (response.created_by, response.last_updated_by) == (
            MOCK_CREATOR_ID,
            MOCK_UPDATER_ID,
        )


@pytest.mark.asyncio
class TestEnsureMissingDerivedChildrenPathGuard:
    """Test that a parent name cannot reshape the derived-sibling lookup."""

    @pytest.mark.parametrize("parent_name", SUFFIXED_UNSAFE_TASKS)
    async def test_refuses_an_unsafe_parent_name(self, parent_name: str) -> None:
        """Refuse an unsafe parent name and issue no GET."""
        tasks_api = AsyncMock(spec=RemoteAPI)

        with pytest.raises(HTTPUnprocessableEntityException):
            await ensure_backup_derived_siblings(tasks_api, parent_name, {})

        tasks_api.get.assert_not_awaited()
        tasks_api.post.assert_not_awaited()

    @pytest.mark.parametrize("renamed", SUFFIXED_UNSAFE_TASKS)
    async def test_refuses_a_rename_before_creating_a_missing_sibling(
        self, renamed: str
    ) -> None:
        """Refuse a sibling built from an unsafe rename before the first request.

        The sibling this backfill creates is built from the updated payload, so
        a rename travelling in that payload is what makes the name unsafe; a
        sibling created under an unaddressable name could never be updated or
        deleted again. Both name sources are checked before the probe GET, so
        neither leg of the backfill runs.
        """
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.get = AsyncMock(side_effect=HTTPNotFoundException)

        with pytest.raises(HTTPUnprocessableEntityException):
            await ensure_backup_derived_siblings(
                tasks_api, "parent", {"name": renamed, "data": {"meta": {}}}
            )

        tasks_api.get.assert_not_awaited()
        tasks_api.post.assert_not_awaited()

    async def test_refuses_every_sibling_before_creating_any_of_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse a later spec's unsafe name before an earlier sibling is POSTed.

        Today every ``BACKUP_MONGO_DERIVED`` suffix is a plain segment, so the
        first spec would raise anyway. A spec added with an empty or unsafe
        suffix is what this pins: the check covers the whole list before the
        first POST, so a part-created group cannot be left behind.
        """
        monkeypatch.setattr(
            "app.extensions.apps.backup_mongo.deps.BACKUP_MONGO_DERIVED",
            [DerivedTask(name_suffix="-logical"), DerivedTask(name_suffix="/evil")],
        )
        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.get = AsyncMock(side_effect=HTTPNotFoundException)

        with pytest.raises(HTTPUnprocessableEntityException):
            await ensure_backup_derived_siblings(
                tasks_api, "parent", {"name": "parent", "data": {"meta": {}}}
            )

        tasks_api.post.assert_not_awaited()


class TestFetchLatestPbmStatus:
    """Test reading the latest PBM status tail through the Tasks log route."""

    @pytest.mark.asyncio
    async def test_a_status_run_not_started_yet_reads_as_no_status(self) -> None:
        """Assert a 409 from a status run the executor has not started is no status.

        The panel is best-effort, so a run caught before its first step must not
        fail the whole backup detail response.
        """

        async def not_started_stream(
            *_args: object, **_kwargs: object
        ) -> AsyncIterator[bytes]:
            raise HTTPConflictException("Allocation a has not started a task yet")
            yield b""

        tasks_api = AsyncMock(spec=RemoteAPI)
        tasks_api.stream = not_started_stream

        assert await _fetch_latest_pbm_status(tasks_api, [{"id": 7}]) is None
