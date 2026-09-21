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

"""Wire the PBM Configuration subpackage as a parent-bound ``TaskExecutionApp``.

Discovered through ``backup_mongo``'s ``child_apps`` rather than a
``settings.yaml`` entry, so it mounts and toggles exactly with its parent and
serves at ``/api/apps/backup_mongo/config/``. A child has no settings entry to
stamp its identity, so ``key`` / ``name`` / ``uri_path`` are set explicitly --
the same shape as the restores child beside it.

Why this exists as its own app rather than a section of the backups form: PBM's
configuration is cluster-wide, and ``pbm config --file`` replaces the document
rather than merging into it. While config rode along with every backup, each run
rewrote state belonging to the whole deployment and blanked whatever the form
could not express -- S3 credentials among them. Applying configuration is now a
deliberate act with its own tab.

Credentials are deliberately absent from the write path. There is no safe place
to keep that secret in OM or SEP, so they stay readable-but-not-writable here and
are changed with the ``pbm`` CLI on the host.
"""

from app.sep.apps.backup_mongo.config.schema import backup_mongo_config_schema
from app.sep.apps.backup_mongo.deps import build_backup_task_payload
from app.sep.apps.backup_mongo.models import OWNER
from app.sep.apps.framework.apps import AppCapabilities, TaskExecutionApp

app = TaskExecutionApp(
    key="backup_mongo/config",
    name="backup_mongo_config",
    display_name="PBM Configuration",
    uri_path="/backup_mongo/config",
    css_class="backup_mongo",
    group="backups",
    nav_order=8,
    sidebar=False,
    parent_key="backup_mongo",
    owner=OWNER,
    schema=backup_mongo_config_schema,
    # Schema only, for now. A ``schema=`` app may not carry a ``task_spec_builder``
    # (apps.py rejects the pair), so applying config needs either a payload builder
    # or a custom route -- the same reason the parent and the restores child derive
    # nothing and mount their own. ``GET /schema`` is what the Configuration tab
    # renders from, so this is the useful half; the apply route lands with the
    # read-merge-write payload that preserves credentials PBM will not read back.
    # The parent's builder unchanged: a config task is the same envelope, and
    # ``build_backup_mongo_spec`` already selects the ``pbm_config`` payload from
    # ``backup_type``. What differs is the absence of a ``derived`` block, so this
    # creates the config task alone instead of fanning out four backup siblings.
    payload_builder=build_backup_task_payload,
    capabilities=AppCapabilities(
        execute=True,
        update=False,
        delete=False,
    ),
)
