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

"""Define dependencies for the alerts plugin."""

import logging
from collections.abc import Mapping
from typing import Annotated, Any, TypeAlias

from aiohttp import ClientError
from fastapi import Depends
from fastapi.exceptions import HTTPException

from app.core.exceptions import HTTPBadGatewayException
from app.core.pagination import make_pagination_dep, Pagination
from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.alerts.crud import AlertBackupManager
from app.extensions.apps.alerts.loader import get_alert_templates
from app.extensions.apps.alerts.models import AlertBackup, AlertTemplate, ServiceType
from app.extensions.clients.pmm import ContactPoint, Folder, PMMRemoteAPI

# ``get_pmm_api`` / ``PMMAPIDep`` / ``require_pmm_api`` / ``RequiredPMMAPIDep`` now
# live in ``app.extensions.deps`` alongside the sibling Inventory / Tasks client deps;
# they are re-exported here for existing importers (routes, tests).
from app.extensions.deps import (
    get_pmm_api,  # noqa: F401 -- re-exported for existing importers
    PMMAPIDep,
    require_pmm_api,  # noqa: F401 -- re-exported for existing importers
    RequiredPMMAPIDep,  # noqa: F401 -- re-exported for existing importers
    SessionDep,
)

logger = logging.getLogger(__name__)

PAGERDUTY_CONTACT_POINT_NAME = "PMM Extensions PagerDuty"

BACKUPS_LIMIT_MAX = 100
_alerts_backups_pagination_dep = make_pagination_dep(max_limit=BACKUPS_LIMIT_MAX)
AlertsBackupsPaginationDep: TypeAlias = Annotated[
    Pagination, Depends(_alerts_backups_pagination_dep)
]


def find_pagerduty_contact_point(
    contact_points: list[ContactPoint],
) -> ContactPoint | None:
    """Find the PMM Extensions PagerDuty contact point in a list of contact points.

    :param contact_points: The list of contact points to search.
    :return: The matching contact point, or ``None`` if not found.
    """
    return next(
        (
            cp
            for cp in contact_points
            if cp.type == "pagerduty" and cp.name == PAGERDUTY_CONTACT_POINT_NAME
        ),
        None,
    )


AlertTemplatesDep = Annotated[
    Mapping[ServiceType, tuple[AlertTemplate, ...]], Depends(get_alert_templates)
]


async def get_pmm_present_names(pmm_api: PMMAPIDep) -> set[str] | None:
    """Return names of alert templates present in PMM.

    Fetch all templates from the PMM API in a single batch call and extract
    their names into a set. Return ``None`` when the PMM client is unavailable
    or when the API call fails with an HTTP or network error, rather than
    propagating the error to the UI route.

    :param pmm_api: The PMM API client dependency, or ``None`` if PMM is not
        configured.
    :return: A set of template names present in PMM, or ``None`` on failure.
    :raises Exception: If a non-HTTP, non-network error occurs (e.g. a
        validation error while parsing the response).
    """
    if pmm_api is None:
        return None
    try:
        templates = await pmm_api.list_templates()
        return {t.name for t in templates}
    except (HTTPException, OSError, ClientError):
        logger.warning("Failed to fetch PMM alert templates", exc_info=True)
        return None


PMMPresentNamesDep = Annotated[set[str] | None, Depends(get_pmm_present_names)]


_MAX_SIDEBAR_BACKUPS = 10


async def get_recent_backups(session: SessionDep) -> list[AlertBackup]:
    """Return the most recent alert backups for the sidebar widget.

    :param session: The async database session.
    :return: A list of recent alert backups, ordered by creation date descending.
    """
    return await AlertBackupManager.list(session, limit=_MAX_SIDEBAR_BACKUPS)


RecentBackupsDep = Annotated[list[AlertBackup], Depends(get_recent_backups)]


async def find_or_create_alert_folder(pmm_api: PMMRemoteAPI) -> Folder:
    """Return the PMM Extensions alert folder in PMM, creating it if it does not exist.

    :param pmm_api: The PMM API client.
    :return: The existing or newly created folder.
    """
    folder_name = alerts_settings.ALERT_FOLDER_NAME
    folders = await pmm_api.list_folders()
    for folder in folders:
        if folder.title == folder_name:
            return folder
    return await pmm_api.create_folder(folder_name)


async def get_or_create_alert_folder(pmm_api: PMMAPIDep) -> Folder | None:
    """Return the PMM Extensions alert folder in PMM, creating it if missing.

    Return ``None`` when the PMM client is unavailable or when the API call
    fails with an HTTP or network error, rather than propagating the error to
    the push route.

    :param pmm_api: The PMM API client dependency, or ``None`` if PMM is not
        configured.
    :return: The existing or newly created folder, or ``None`` on failure.
    :raises Exception: If a non-HTTP, non-network error occurs (e.g. a
        validation error while parsing the response).
    """
    if pmm_api is None:
        return None
    try:
        return await find_or_create_alert_folder(pmm_api)
    except (HTTPException, OSError, ClientError):
        logger.warning("Failed to get or create alert folder in PMM", exc_info=True)
        return None


AlertFolderDep = Annotated[Folder | None, Depends(get_or_create_alert_folder)]


async def require_alert_folder(folder: AlertFolderDep) -> Folder:
    """Return the PMM alert folder or raise if unavailable.

    :param folder: The alert folder dependency, or ``None`` when PMM is
        unreachable.
    :return: The PMM alert folder.
    :raises HTTPBadGatewayException: If the alert folder cannot be accessed.
    """
    if folder is None:
        raise HTTPBadGatewayException(detail="Failed to access PMM alert folder")
    return folder


RequiredAlertFolderDep = Annotated[Folder, Depends(require_alert_folder)]


async def get_pagerduty_status(pmm_api: PMMAPIDep) -> dict[str, Any] | None:
    """Return PagerDuty contact point status for the sidebar widget.

    Fetch all contact points from PMM and find the PagerDuty one.
    Return ``None`` when PMM is unavailable or the API call fails.

    :param pmm_api: The PMM API client dependency, or ``None`` if PMM is not
        configured.
    :return: A dictionary with ``configured`` and ``uid`` keys when a PagerDuty
        contact point exists; ``{"configured": False}`` when none exists; or
        ``None`` when PMM is unreachable.
    """
    if pmm_api is None:
        return None
    try:
        contact_points = await pmm_api.list_contact_points()
    except (HTTPException, OSError, ClientError):
        logger.warning("Failed to fetch PagerDuty contact point status", exc_info=True)
        return None

    pd_cp = find_pagerduty_contact_point(contact_points)
    if pd_cp is None:
        return {"configured": False}
    return {"configured": True, "uid": pd_cp.uid}


PagerDutyStatusDep = Annotated[dict[str, Any] | None, Depends(get_pagerduty_status)]


async def ensure_pagerduty_notification_route(
    pmm_api: PMMRemoteAPI, contact_point_name: str
) -> None:
    """Append a notification policy route for PagerDuty if not already present.

    Fetch the current notification policy tree from PMM, check if a route
    targeting ``contact_point_name`` already exists, and append one if it does
    not.

    :param pmm_api: The PMM API client.
    :param contact_point_name: The receiver name to match on.
    """
    policy = await pmm_api.get_notification_policy()
    if any(r.get("receiver") == contact_point_name for r in policy.routes):
        return
    policy.routes.append(
        {
            "receiver": contact_point_name,
            "object_matchers": [["service", "=", "pmm-extensions"]],
        }
    )
    await pmm_api.update_notification_policy(policy)
