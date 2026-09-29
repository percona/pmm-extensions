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

"""Compose the Tasks sub-app's settings REST API."""

__all__ = ["TASKS_ADMIN_SETTINGS_CLASSES", "router"]

from fastapi import APIRouter

from app.api.deps import AdminUsername, IsAdminDep
from app.core.settings_override.api import build_settings_router
from app.core.settings_override.api.routes import ClassEntry
from app.core.settings_override.constants import ANONYMIZER_SETTINGS, TASKS_SETTINGS
from app.tasks.anonymizer.config import anonymizer_settings, AnonymizerSettings
from app.tasks.config import tasks_settings, TasksSettings
from app.tasks.deps import SessionDep

TASKS_ADMIN_SETTINGS_CLASSES: list[ClassEntry] = [
    (TASKS_SETTINGS, TasksSettings, tasks_settings),
    (ANONYMIZER_SETTINGS, AnonymizerSettings, anonymizer_settings),
]

_settings_router = build_settings_router(
    classes=TASKS_ADMIN_SETTINGS_CLASSES,
    session_dep=SessionDep,
    admin_dep=IsAdminDep,
    actor_dep=AdminUsername,
)

router = APIRouter(prefix="/admin/settings", tags=["settings"])
router.include_router(_settings_router)
