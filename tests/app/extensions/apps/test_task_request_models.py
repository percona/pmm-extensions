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

"""Guard that every task create and update body is strict and declares what the UI sends.

The create and update calls reject a body key their request model does not
declare. The UI builds its body from the schema the app publishes, so a form
field the request model does not declare would make every save from the UI fail
with 422. Each case pairs an app's schema route with the model its create and
update routes validate the body against.
"""

import pytest
from fastapi import status
from pydantic import BaseModel

from app.extensions.apps.alters.models import AltersCreate
from app.extensions.apps.archives.models import ArchivesCreate
from app.extensions.apps.backup_mongo.models import BackupTaskWrite
from app.extensions.apps.backup_mongo.restore.models import RestoreTaskWrite
from app.extensions.apps.backup_pg.models import BackupPgForm
from app.extensions.apps.checksums.models import ChecksumsForm
from app.extensions.apps.mysql_backups.forms import BackupCreate
from app.extensions.apps.mysql_backups.restore.models import RestoreCreate
from tests.app.extensions.unknown_body_field import submitted_field_names

TASK_REQUEST_MODELS = [
    pytest.param("/api/apps/mysql_backups", BackupCreate, id="mysql_backups"),
    pytest.param(
        "/api/apps/mysql_backups/restore", RestoreCreate, id="mysql_backups-restore"
    ),
    pytest.param("/api/apps/alters", AltersCreate, id="alters"),
    pytest.param("/api/apps/archives", ArchivesCreate, id="archives"),
    pytest.param("/api/apps/checksums", ChecksumsForm, id="checksums"),
    pytest.param("/api/apps/backup_pg", BackupPgForm, id="backup_pg"),
    pytest.param("/api/apps/backup_mongo", BackupTaskWrite, id="backup_mongo"),
    pytest.param(
        "/api/apps/backup_mongo/restore", RestoreTaskWrite, id="backup_mongo-restore"
    ),
]


@pytest.mark.parametrize(("base", "model"), TASK_REQUEST_MODELS)
def test_request_model_rejects_unknown_fields(base: str, model: type[BaseModel]):
    """Reject an undeclared key rather than dropping it."""
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(("base", "model"), TASK_REQUEST_MODELS)
def test_request_model_declares_every_key_the_ui_sends(
    test_client, base: str, model: type[BaseModel]
):
    """Declare every field of the published form, ``task_name``, and a shown ``alert_on_fail``."""
    response = test_client.get(f"{base}/schema")
    assert response.status_code == status.HTTP_200_OK

    undeclared = submitted_field_names(response.json()) - model.model_fields.keys()

    assert not undeclared
