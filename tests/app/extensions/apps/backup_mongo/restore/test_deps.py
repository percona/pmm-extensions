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

"""Define tests for the app.extensions.apps.backup_mongo.restore.deps module."""

from collections.abc import Awaitable, Callable

import pytest
import yaml
from pydantic import ValidationError

from app.core.exceptions import HTTPNotFoundException
from app.extensions.apps.backup_mongo.models import BackupType
from app.extensions.apps.backup_mongo.restore.deps import (
    _backup_type_from_parent,
    _resolve_service_name,
    build_restore_mongo_api_task_response,
    build_restore_task_group,
    build_restore_update_form_from_body,
)
from app.extensions.apps.backup_mongo.restore.models import (
    RestoreCreate,
    RestoreTaskWrite,
)
from app.extensions.apps.backup_mongo.restore.schema import restore_mongo_schema
from app.extensions.inventory import CreatedService
from app.extensions.models import SyncInventoryEntityTypeEnum
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import Task, TaskBackendEnum, TaskWrite
from tests.app.extensions.unknown_body_field import edit_page_body
from tests.app.factories import (
    MOCK_ACTOR_USERNAMES,
    MOCK_CREATOR_ID,
    MOCK_UPDATER_ID,
    TaskFactory,
)

EXPECTED_PHYSICAL_RESTORE_TUPLE_LEN = 4

DispatchFn = Callable[[RestoreCreate, object], Awaitable[TaskWrite]]
DISPATCH_FUNCTIONS: list[DispatchFn] = []


def _restore_parent_task(*, config: str) -> Task:
    task = TaskFactory.build(
        name="restore-parent",
        owner="RESTORE_MONGO",
        backend=TaskBackendEnum.PROXY,
    )
    return task.model_copy(
        update={"data": {**task.data, "meta": {"config": config}}},
    )


def test_backup_type_from_parent_returns_backup_type() -> None:
    """Parse backupType from the parent restore config YAML."""
    config = yaml.dump({"backupType": BackupType.PBM_LOGICAL.value})
    assert _backup_type_from_parent(_restore_parent_task(config=config)) == (
        BackupType.PBM_LOGICAL
    )


def test_backup_type_from_parent_raises_when_backup_type_missing() -> None:
    """Return 404 instead of KeyError when backupType is absent from config."""
    with pytest.raises(HTTPNotFoundException) as exc_info:
        _backup_type_from_parent(_restore_parent_task(config=yaml.dump({})))

    assert exc_info.value.detail == "Task 'restore-parent' has no backupType in config"


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", DISPATCH_FUNCTIONS)
async def test_dispatch_includes_service_name_when_service_id_set(
    dispatch: DispatchFn,
    mocker,
    mock_remote_api,
    restore_create: RestoreCreate,
    mongo_service: CreatedService,
):
    """Inject _service_name into meta when service_id is set across restore dispatch functions."""
    mocker.patch(
        "app.extensions.apps.backup_mongo.restore.deps.get_created_entity",
        return_value=mongo_service,
    )

    task_payload = await dispatch(restore_create, mock_remote_api)

    assert task_payload.data["meta"]["_service_name"] == mongo_service.name


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", DISPATCH_FUNCTIONS)
async def test_dispatch_omits_service_name_when_service_id_unset(
    dispatch: DispatchFn,
    mocker,
    mock_remote_api,
    restore_create_no_service: RestoreCreate,
):
    """All four dispatch functions skip the lookup and meta key when service_id is None."""
    get_created_entity = mocker.patch(
        "app.extensions.apps.backup_mongo.restore.deps.get_created_entity",
    )

    task_payload = await dispatch(restore_create_no_service, mock_remote_api)

    assert "_service_name" not in task_payload.data["meta"]
    get_created_entity.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_service_name_returns_none_when_service_id_missing(
    mock_remote_api,
    restore_create_no_service: RestoreCreate,
):
    """_resolve_service_name returns None when the form has no service_id."""
    assert (
        await _resolve_service_name(restore_create_no_service, mock_remote_api) is None
    )


@pytest.mark.asyncio
async def test_resolve_service_name_returns_none_on_stale_service_id(
    mocker,
    mock_remote_api,
    restore_create: RestoreCreate,
):
    """Return None when the inventory lookup 404s so a stale service_id degrades.

    ``RemoteAPI.get`` raises the project's ``HTTPNotFoundException`` on 404, which
    the narrowed handler swallows to fall back to a node-only PMM annotation.
    """
    mocker.patch(
        "app.extensions.apps.backup_mongo.restore.deps.get_created_entity",
        side_effect=HTTPNotFoundException(),
    )

    assert await _resolve_service_name(restore_create, mock_remote_api) is None


@pytest.mark.asyncio
async def test_resolve_service_name_returns_service_name_when_service_id_present(
    mocker,
    mock_remote_api,
    restore_create: RestoreCreate,
    mongo_service: CreatedService,
):
    """_resolve_service_name returns service.name and queries with the MONGODB type."""
    get_created_entity = mocker.patch(
        "app.extensions.apps.backup_mongo.restore.deps.get_created_entity",
        return_value=mongo_service,
    )

    result = await _resolve_service_name(restore_create, mock_remote_api)

    assert result == mongo_service.name
    get_created_entity.assert_awaited_once_with(
        mock_remote_api,
        SyncInventoryEntityTypeEnum.SERVICE,
        restore_create.service_id,
        type=ServiceTypeEnum.MONGODB,
    )


@pytest.mark.asyncio
async def test_restore_task_group_yaml_golden_strings(
    mocker,
    mock_remote_api,
    mongo_service: CreatedService,
) -> None:
    """Keep per-leg meta.config YAML byte-identical."""
    mocker.patch(
        "app.extensions.apps.backup_mongo.restore.deps.get_created_entity",
        return_value=mongo_service,
    )
    form = RestoreCreate(
        hostname="mongo-restore-host",
        task_name="mongo-restore-task",
        service_id=str(mongo_service.id),
        backup_type=BackupType.PBM_PHYSICAL,
        backup_source="2026-04-29T10:00:00",
        restore_batch_size=500,
        restore_num_insertion_workers=10,
        restore_mongod_location="custom/mongod",
        restore_mongod_location_map="node1: /usr/bin/mongod\n",
        credentials_path="/tmp/creds.yaml",
    )

    (
        config_task,
        restore_task,
        pbm_list_task,
        force_resync_task,
    ) = await build_restore_task_group(form, mock_remote_api)

    assert (
        config_task.data["meta"]["config"] == "backupSource: '2026-04-29T10:00:00'\n"
        "backupType: pbm_physical\n"
        "credentials_path: /tmp/creds.yaml\n"
        "restore:\n"
        "  batchSize: 500\n"
        "  downloadChunkMb: 32\n"
        "  mongodLocation: custom/mongod\n"
        "  mongodLocationMap:\n"
        "    node1: /usr/bin/mongod\n"
        "  numInsertionWorkers: 10\n"
    )
    assert (
        restore_task.data["meta"]["config"] == "backupSource: '2026-04-29T10:00:00'\n"
        "backupType: pbm_physical\n"
        "credentials_path: /tmp/creds.yaml\n"
    )
    assert pbm_list_task.data["meta"]["config"] == "credentials_path: /tmp/creds.yaml\n"
    assert force_resync_task is not None
    assert (
        force_resync_task.data["meta"]["config"]
        == "credentials_path: /tmp/creds.yaml\n"
    )


def test_build_restore_update_form_from_body_pins_parent_identity(
    restore_task_write: RestoreTaskWrite,
) -> None:
    """Update composer keeps path parent task_name and backup_type."""
    parent_task = _restore_parent_task(
        config=yaml.dump({"backupType": BackupType.PBM_LOGICAL.value}),
    )
    body = restore_task_write.model_copy(
        update={
            "task_name": "wrong-name",
            "backup_type": BackupType.PBM_PHYSICAL,
        },
    )

    result = build_restore_update_form_from_body(body, parent_task)

    assert result.task_name == parent_task.name
    assert result.backup_type == BackupType.PBM_LOGICAL
    assert result.hostname == body.hostname
    assert result.backup_source == body.backup_source


class TestRestoreFormRoundTrip:
    """Guard the create-stamp -> edit-PUT body round-trip."""

    def test_stamped_create_form_revalidates_as_put_body(
        self, restore_create: RestoreCreate
    ) -> None:
        """Re-validate what the edit page submits from the stored form as the PUT body.

        The edit page seeds its fields from the stored ``_form`` (a
        :class:`RestoreCreate` dump) and submits only the fields the published
        form declares; :class:`RestoreTaskWrite` must accept that body.
        """
        stamped = restore_create.model_dump(mode="json")

        body = RestoreTaskWrite.model_validate(
            edit_page_body(stamped, restore_mongo_schema.model_dump(mode="json"))
        )

        assert body.task_name == restore_create.task_name
        assert body.backup_type == restore_create.backup_type
        assert body.hostname == restore_create.hostname


def test_build_restore_update_rejects_namespace_for_physical_parent(
    restore_task_write: RestoreTaskWrite,
) -> None:
    """Validate the namespace filter after pinning the parent's physical type."""
    parent_task = _restore_parent_task(
        config=yaml.dump({"backupType": BackupType.PBM_PHYSICAL.value}),
    )
    body = restore_task_write.model_copy(
        update={"restore_namespace_filter": "db.collection"}
    )

    with pytest.raises(
        ValidationError,
        match="Namespace Filter is only supported for logical MongoDB restores",
    ):
        build_restore_update_form_from_body(body, parent_task)


class TestBuildRestoreMongoApiTaskResponse:
    """Cover the MongoDB restore builder's actor resolution."""

    @staticmethod
    def _recorded_task() -> Task:
        """Build a parent restore config task recorded by two known users."""
        config = yaml.dump(
            {
                "backupType": BackupType.PBM_LOGICAL.value,
                "backupSource": "2026-04-29T10:00:00",
            }
        )
        return _restore_parent_task(config=config).model_copy(
            update={"created_by": MOCK_CREATOR_ID, "last_updated_by": MOCK_UPDATER_ID}
        )

    def test_resolves_actors_through_the_context(self):
        """Render both actors as usernames and keep the app's own extras."""
        response = build_restore_mongo_api_task_response(
            self._recorded_task(), context=MOCK_ACTOR_USERNAMES
        )

        assert (response.created_by, response.last_updated_by) == ("alice", "bob")
        assert (response.backup_type, response.backup_source) == (
            BackupType.PBM_LOGICAL.value,
            "2026-04-29T10:00:00",
        )

    def test_keeps_raw_ids_without_a_context(self):
        """Serve the stored identifiers when no username map is bound."""
        response = build_restore_mongo_api_task_response(self._recorded_task())

        assert (response.created_by, response.last_updated_by) == (
            MOCK_CREATOR_ID,
            MOCK_UPDATER_ID,
        )
