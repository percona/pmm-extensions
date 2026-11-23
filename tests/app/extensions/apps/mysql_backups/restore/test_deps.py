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

"""Define tests for the app.extensions.apps.mysql_backups.restore.deps module."""

import pytest

from app.core.exceptions import HTTPUnprocessableEntityException
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.apps.mysql_backups.models import BackupType
from app.extensions.apps.mysql_backups.restore.deps import (
    build_restore_api_task_response,
    build_restore_payload,
    resolve_restore_entities,
)
from app.extensions.apps.mysql_backups.restore.models import (
    RestoreCreate,
    SourceTransport,
)
from app.extensions.inventory import CreatedService
from app.tasks.models import Task, TaskBackendEnum
from tests.app.factories import (
    MOCK_ACTOR_USERNAMES,
    MOCK_CREATOR_ID,
    MOCK_UPDATER_ID,
)

_SCHEMA_ID = 42


@pytest.mark.asyncio
async def test_resolve_restore_entities_mydumper_splits_address_and_resolves_schema(
    mocker,
    mock_remote_api,
    created_service: CreatedService,
):
    """Resolve the MyDumper service address split and the schema name."""
    node = created_service.node.model_copy(update={"address": "10.0.0.5"})
    service = created_service.model_copy(update={"node": node, "port": 3307})
    schema = service.model_copy(update={"name": "shop"})
    lookup = mocker.patch(
        "app.extensions.apps.mysql_backups.restore.deps.get_created_entity",
        side_effect=[service, schema],
    )

    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=str(service.id),
        schema_id=str(_SCHEMA_ID),
        backup_type=BackupType.MYDUMPER,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )
    resolved = await resolve_restore_entities(form, mock_remote_api)

    assert resolved.service_name == service.name
    assert resolved.dest_host == node.address
    assert resolved.dest_port == service.port
    assert resolved.database == schema.name
    service_call, schema_call = lookup.await_args_list
    assert service_call.args[2] == service.id
    assert isinstance(service_call.args[2], int)
    assert schema_call.args[2] == _SCHEMA_ID
    assert isinstance(schema_call.args[2], int)
    assert schema_call.kwargs["service_id"] == service.id


@pytest.mark.asyncio
async def test_resolve_restore_entities_non_mydumper_passes_numeric_service_id_as_int(
    mocker,
    mock_remote_api,
    created_service: CreatedService,
):
    """Resolve a non-MyDumper numeric service id through the integer lookup path."""
    lookup = mocker.patch(
        "app.extensions.apps.mysql_backups.restore.deps.get_created_entity",
        return_value=created_service,
    )
    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=str(created_service.id),
        backup_type=BackupType.XTRABACKUP,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )

    resolved = await resolve_restore_entities(form, mock_remote_api)

    assert resolved.service_name == created_service.name
    assert lookup.await_args.args[2] == created_service.id
    assert isinstance(lookup.await_args.args[2], int)


@pytest.mark.asyncio
async def test_resolve_restore_entities_mydumper_rejects_missing_service(
    mock_remote_api,
):
    """Reject a MyDumper restore without a destination service.

    The ``Requires`` gate on ``service_id`` already rejects this body at model
    validation, so the guard here is defence in depth and the form has to be
    built with ``model_construct`` to reach it.
    """
    form = RestoreCreate.model_construct(
        hostname="restore-host",
        task_name="restore-task",
        service_id=None,
        backup_type=BackupType.MYDUMPER,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )

    with pytest.raises(HTTPUnprocessableEntityException):
        await resolve_restore_entities(form, mock_remote_api)


@pytest.mark.asyncio
async def test_resolve_restore_entities_mydumper_keeps_host_of_port_less_service(
    mocker,
    mock_remote_api,
    created_service: CreatedService,
):
    """Resolve a port-less MyDumper service to its host, leaving the port unset.

    The payload defaults an absent ``DEST_PORT`` to 3306, so dropping the host
    alongside the missing port would retarget the restore at the executor.
    """
    node = created_service.node.model_copy(update={"address": "10.0.0.5"})
    service = created_service.model_copy(update={"node": node, "port": None})
    mocker.patch(
        "app.extensions.apps.mysql_backups.restore.deps.get_created_entity",
        return_value=service,
    )
    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=str(service.id),
        backup_type=BackupType.MYDUMPER,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )

    resolved = await resolve_restore_entities(form, mock_remote_api)

    assert resolved.dest_host == node.address
    assert resolved.dest_port is None


@pytest.mark.asyncio
async def test_resolve_restore_entities_mydumper_rejects_service_without_address(
    mocker,
    mock_remote_api,
    created_service: CreatedService,
):
    """Reject a MyDumper service that resolves to no address at all."""
    service = created_service.model_copy(update={"node": None})
    mocker.patch(
        "app.extensions.apps.mysql_backups.restore.deps.get_created_entity",
        return_value=service,
    )
    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=str(service.id),
        backup_type=BackupType.MYDUMPER,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )

    with pytest.raises(HTTPUnprocessableEntityException):
        await resolve_restore_entities(form, mock_remote_api)


@pytest.mark.asyncio
async def test_resolve_restore_entities_non_mydumper_tolerates_missing_address(
    mocker,
    mock_remote_api,
    created_service: CreatedService,
):
    """Annotate a non-MyDumper restore from a service carrying no address.

    XtraBackup and Binlog record the service name only, so the address guard the
    MyDumper branch applies must not reach them.
    """
    service = created_service.model_copy(update={"node": None})
    mocker.patch(
        "app.extensions.apps.mysql_backups.restore.deps.get_created_entity",
        return_value=service,
    )
    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=str(service.id),
        backup_type=BackupType.XTRABACKUP,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
    )

    resolved = await resolve_restore_entities(form, mock_remote_api)

    assert resolved.service_name == service.name
    assert resolved.dest_host is None


@pytest.mark.asyncio
async def test_build_restore_payload_stamps_form_without_new_secret_exposure(
    mock_remote_api,
):
    """Assert the stamped ``_form`` re-carries a secret already held in ``data``.

    AC #3 invariant: ``_form`` introduces no plaintext secret ``data`` does not
    already persist, so ``master_password`` appears in both and stays equal.
    """
    form = RestoreCreate(
        hostname="restore-host",
        task_name="restore-task",
        service_id=None,
        backup_type=BackupType.XTRABACKUP,
        backup_source="/var/backups/latest",
        datadir="/var/lib/mysql",
        master_password="s3cret-pw",
    )

    task_payload = await build_restore_payload(form, mock_remote_api)

    assert task_payload.data[RESERVED_FORM_KEY]["master_password"] == "s3cret-pw"
    assert "s3cret-pw" in task_payload.data["meta"]["config"]


_NON_DEFAULT_SSH_PORT = 2222


def _restore_task(stored_form: dict | None) -> Task:
    """Build a restore task row, optionally carrying a stored form stamp."""
    data = {
        "task": "run-python",
        "meta": {"target": "executor-1", "config": ""},
    }
    if stored_form is not None:
        data[RESERVED_FORM_KEY] = stored_form
    return Task(
        name="restore-task",
        owner="RESTORES",
        backend=TaskBackendEnum.PROXY,
        data=data,
        protected=False,
        alert_on_fail=False,
    )


def test_served_stamp_declares_the_source_its_stored_values_imply():
    """Serve a pre-declaration stamp with the transport its own values imply.

    The edit form seeds a field the stamp does not carry from the schema default,
    so serving such a stamp untouched would open the form on ``local`` and hide
    the SSH credentials. A hidden field is dropped from the submission, so the
    next save would discard them.
    """
    task = _restore_task(
        {
            "task_name": "restore-task",
            "hostname": "executor-1",
            "backup_type": BackupType.XTRABACKUP.value,
            "backup_source": "db01:/backups/xb/latest",
            "ssh_user": "deploy",
            "ssh_port": _NON_DEFAULT_SSH_PORT,
            "ssh_key": "prod-key",
            "s3_tool": "s3cmd",
        }
    )

    served = build_restore_api_task_response(task).data[RESERVED_FORM_KEY]

    assert served["source_transport"] == SourceTransport.SSH.value
    assert served["ssh_user"] == "deploy"
    assert served["ssh_port"] == _NON_DEFAULT_SSH_PORT
    assert served["ssh_key"] == "prod-key"


def test_served_stamp_is_repaired_when_it_holds_a_retired_key():
    """Repair a pre-declaration stamp that also holds a key the form no longer declares.

    A request carrying that key is rejected, but the stamp was saved before the
    key was retired; it must still open on the transport its values imply. The
    served stamp leaves the retired key out, so saving the form sends no such key.
    """
    task = _restore_task(
        {
            "task_name": "restore-task",
            "hostname": "executor-1",
            "backup_type": BackupType.XTRABACKUP.value,
            "backup_source": "db01:/backups/xb/latest",
            "ssh_user": "deploy",
            "ssh_key": "prod-key",
            "retired_setting": "value",
        }
    )

    served = build_restore_api_task_response(task).data[RESERVED_FORM_KEY]

    assert served["source_transport"] == SourceTransport.SSH.value
    assert "retired_setting" not in served


def test_served_stamp_keeps_a_declaration_the_operator_already_made():
    """Leave a stamp that already declares its source untouched."""
    stored_form = {
        "task_name": "restore-task",
        "hostname": "executor-1",
        "backup_type": BackupType.MYDUMPER.value,
        "backup_source": "/backups/mydumper/latest",
        "source_transport": SourceTransport.LOCAL.value,
    }

    served = build_restore_api_task_response(_restore_task(stored_form)).data

    assert served[RESERVED_FORM_KEY] == stored_form


def test_an_unvalidatable_stamp_is_served_rather_than_failing_the_route():
    """Serve a stamp that cannot validate unchanged, so one bad task cannot break the list."""
    stored_form = {"task_name": "restore-task", "backup_source": "$(id)"}

    served = build_restore_api_task_response(_restore_task(stored_form)).data

    assert served[RESERVED_FORM_KEY] == stored_form


def test_a_task_without_a_stamp_is_served_unchanged():
    """Leave a legacy task carrying no stamp alone."""
    served = build_restore_api_task_response(_restore_task(None)).data

    assert RESERVED_FORM_KEY not in served


class TestBuildRestoreApiTaskResponse:
    """Cover the MySQL restore builder's actor resolution."""

    @staticmethod
    def _recorded_task() -> Task:
        """Build a restore task recorded by two known users."""
        return _restore_task(None).model_copy(
            update={"created_by": MOCK_CREATOR_ID, "last_updated_by": MOCK_UPDATER_ID}
        )

    def test_resolves_actors_through_the_context(self):
        """Render both actors as usernames and keep the app's own extras."""
        response = build_restore_api_task_response(
            self._recorded_task(), context=MOCK_ACTOR_USERNAMES
        )

        assert (response.created_by, response.last_updated_by) == ("alice", "bob")
        assert response.hostname == "executor-1"

    def test_keeps_raw_ids_without_a_context(self):
        """Serve the stored identifiers when no username map is bound."""
        response = build_restore_api_task_response(self._recorded_task())

        assert (response.created_by, response.last_updated_by) == (
            MOCK_CREATOR_ID,
            MOCK_UPDATER_ID,
        )
