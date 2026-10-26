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

"""Define the JSON API router for the alerts plugin.

Mounted at ``/api/apps/alerts/`` via ``apps_router`` in
``app/extensions/api/router.py`` through the ``api_routes.py`` convention validator
in ``app/extensions/config.py``. Authentication is enforced at the ``api_router``
mount level (``IsApiAuthenticated``) and by ``RequireBearerForUnsafeMethods``
on the ``apps_router``.

Route layout:

* ``GET /``                         — index payload for the React list page
* ``GET /backups``                  — list recent alert backups
* ``GET /backups/{backup_id}``      — detail of a single backup
* ``POST /push``                    — push selected templates + rules to PMM
* ``POST /restore``                 — restore alert configuration from a backup
* ``POST /pagerduty``               — create or update the PagerDuty contact point
* ``POST /pagerduty/delete``        — delete the PagerDuty contact point + route
"""

import logging

from fastapi import APIRouter
from fastapi.exceptions import HTTPException

from app.api.deps import require_minimum_role
from app.core.auth.models import UserRole
from app.core.exceptions import (
    HTTPBadGatewayException,
    HTTPNotFoundException,
)
from app.core.pagination import PaginatedResponse
from app.core.utils.exceptions import exception_detail
from app.extensions.apps.alerts.config import alerts_settings
from app.extensions.apps.alerts.crud import AlertBackupManager
from app.extensions.apps.alerts.deps import (
    AlertsBackupsPaginationDep,
    AlertTemplatesDep,
    ensure_pagerduty_notification_route,
    find_pagerduty_contact_point,
    PAGERDUTY_CONTACT_POINT_NAME,
    PagerDutyStatusDep,
    PMMPresentNamesDep,
    RecentBackupsDep,
    RequiredAlertFolderDep,
    RequiredPMMAPIDep,
)
from app.extensions.apps.alerts.models import (
    BackupDetail,
    BackupDetailContactPoint,
    BackupDetailFolder,
    BackupDetailRule,
    BackupDetailTemplate,
    BackupSummary,
    DEFAULT_FOR_DURATION,
    IndexBackupSummary,
    IndexPagerDutyStatus,
    IndexResponse,
    IndexTemplate,
    IndexTemplateGroup,
    PagerDutyRequest,
    PagerDutyResponse,
    PushItemResult,
    PushRequest,
    PushResponse,
    RestoreRequest,
    RestoreResponse,
    to_pmm_template_yaml,
)
from app.extensions.apps.alerts.restore import (
    delete_conflicting_rules,
    restore_from_backup,
)
from app.extensions.deps import SessionDep

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
async def alerts_api_index(
    alert_templates: AlertTemplatesDep,
    present_names: PMMPresentNamesDep,
    recent_backups: RecentBackupsDep,
    pagerduty_status: PagerDutyStatusDep,
) -> IndexResponse:
    """Return everything the React list page needs in a single call.

    Mirror the data assembled for the deprecated Jinja index view
    (:func:`app.extensions.apps.alerts.deps.get_alerts_index_context`) as JSON:
    alert templates grouped by service type, PMM connectivity, the PagerDuty
    contact-point status, and the most recent backups.

    ``present_names`` is ``None`` when PMM is unconfigured or unreachable, which
    is the same signal used to drive ``pmm_connected`` and per-template
    ``in_pmm`` flags, so the response carries no PMM data rather than failing.

    :param alert_templates: Local alert templates grouped by service type.
    :param present_names: Template names already present in PMM, or ``None`` when
        PMM is unreachable.
    :param recent_backups: The most recent alert backups, newest first.
    :param pagerduty_status: PagerDuty contact-point status, or ``None`` when PMM
        is unreachable.
    :return: The aggregated index payload.
    """
    pmm_connected = present_names is not None
    groups = [
        IndexTemplateGroup(
            service_type=service_type.value,
            label=service_type.label,
            templates=[
                IndexTemplate(
                    name=template.name,
                    service_type=template.service_type.value,
                    expression=template.expression,
                    default_threshold=template.default_threshold,
                    severity=template.severity.value,
                    description=template.description,
                    summary=template.summary,
                    in_pmm=present_names is not None and template.name in present_names,
                )
                for template in templates
            ],
        )
        for service_type, templates in alert_templates.items()
        if templates
    ]
    pagerduty = (
        IndexPagerDutyStatus(**pagerduty_status)
        if pagerduty_status is not None
        else None
    )
    return IndexResponse(
        groups=groups,
        pmm_connected=pmm_connected,
        pagerduty=pagerduty,
        recent_backups=[
            IndexBackupSummary(id=backup.id, created_at=backup.created_at)
            for backup in recent_backups
        ],
    )


@router.get("/backups")
async def alerts_api_list_backups(
    session: SessionDep,
    pagination: AlertsBackupsPaginationDep,
) -> PaginatedResponse[BackupSummary]:
    """Return a paginated page of alert backups, newest first.

    :param session: The async database session.
    :type session: SessionDep
    :param pagination: Validated offset/limit query parameters (``limit`` capped at 100).
    :type pagination: Pagination
    :return: A paginated envelope of backup summaries ordered by
        ``created_at`` descending.
    :rtype: PaginatedResponse[BackupSummary]
    """
    page = await AlertBackupManager.list_paginated(session, pagination=pagination)
    items = [
        BackupSummary(
            id=backup.id,
            created_at=backup.created_at,
            metadata=backup.metadata_,
        )
        for backup in page.items
    ]
    return PaginatedResponse.from_pagination(items, page.total, pagination)


@router.get("/backups/{backup_id}")
async def alerts_api_get_backup(session: SessionDep, backup_id: int) -> BackupDetail:
    """Return a categorised view of a single backup.

    :param session: The async database session.
    :type session: SessionDep
    :param backup_id: Primary key of the backup row.
    :type backup_id: int
    :return: The full backup detail.
    :rtype: BackupDetail
    :raises HTTPNotFoundException: If the backup is not found.
    """
    backup = await AlertBackupManager.first(session, id=backup_id)
    if backup is None:
        raise HTTPNotFoundException(detail="Backup not found")
    data = backup.data
    receiver = data.get("notification_policy", {}).get("receiver") or None
    return BackupDetail(
        id=backup.id,
        created_at=backup.created_at,
        templates=[
            BackupDetailTemplate(name=t.get("name", ""), summary=t.get("summary", ""))
            for t in data.get("templates", [])
        ],
        rules=[
            BackupDetailRule(title=r.get("title", "")) for r in data.get("rules", [])
        ],
        contact_points=[
            BackupDetailContactPoint(name=cp.get("name", ""), type=cp.get("type", ""))
            for cp in data.get("contact_points", [])
        ],
        folders=[
            BackupDetailFolder(title=f.get("title", ""))
            for f in data.get("folders", [])
        ],
        notification_policy_receiver=receiver,
    )


@router.post("/restore")
@require_minimum_role(UserRole.EDITOR)
async def alerts_api_restore(
    payload: RestoreRequest,
    pmm_api: RequiredPMMAPIDep,
    session: SessionDep,
) -> RestoreResponse:
    """Restore alert configuration from a backup snapshot.

    :param payload: Restore request body identifying the backup to apply.
    :type payload: RestoreRequest
    :param pmm_api: The PMM API client. Raises 503 if PMM is not configured.
    :type pmm_api: PMMRemoteAPI
    :param session: The async database session.
    :type session: SessionDep
    :return: A ``RestoreResponse`` with the per-section restore counts.
    :rtype: RestoreResponse
    :raises HTTPNotFoundException: If the backup is not found.
    :raises HTTPBadGatewayException: If the PMM upstream call fails with
        ``OSError``.
    """
    backup = await AlertBackupManager.first(session, id=payload.backup_id)
    if backup is None:
        raise HTTPNotFoundException(detail="Backup not found")
    try:
        details = await restore_from_backup(pmm_api, backup)
    except HTTPException:
        logger.exception("Failed to restore alert configuration from backup")
        raise
    except OSError:
        logger.exception("Failed to restore alert configuration from backup")
        raise HTTPBadGatewayException(detail="Upstream PMM error") from None
    return RestoreResponse(status="success", details=details)


@router.post("/pagerduty")
async def alerts_api_pagerduty_save(
    payload: PagerDutyRequest,
    pmm_api: RequiredPMMAPIDep,
) -> PagerDutyResponse:
    """Create or update the PagerDuty contact point and notification route.

    Mirror :func:`app.extensions.apps.alerts.routes.pagerduty_save` over JSON.

    :param payload: PagerDuty save request body.
    :type payload: PagerDutyRequest
    :param pmm_api: The PMM API client. Raises 503 if PMM is not configured.
    :type pmm_api: PMMRemoteAPI
    :return: ``status="created"`` or ``status="updated"``.
    :rtype: PagerDutyResponse
    :raises HTTPBadGatewayException: If the PMM upstream call fails.
    """
    try:
        contact_points = await pmm_api.list_contact_points()
        pd_cp = find_pagerduty_contact_point(contact_points)
        pd_settings = {"integrationKey": payload.integration_key}

        if pd_cp is not None:
            await pmm_api.update_contact_point(
                pd_cp.uid,
                PAGERDUTY_CONTACT_POINT_NAME,
                "pagerduty",
                pd_settings,
            )
            response = PagerDutyResponse(status="updated")
        else:
            await pmm_api.create_contact_point(
                PAGERDUTY_CONTACT_POINT_NAME,
                "pagerduty",
                pd_settings,
            )
            response = PagerDutyResponse(status="created")

        await ensure_pagerduty_notification_route(pmm_api, PAGERDUTY_CONTACT_POINT_NAME)
    except (HTTPException, OSError):
        logger.exception("Failed to save PagerDuty contact point")
        raise HTTPBadGatewayException(
            detail="Failed to save PagerDuty configuration"
        ) from None
    return response


@router.post("/pagerduty/delete")
async def alerts_api_pagerduty_delete(
    pmm_api: RequiredPMMAPIDep,
) -> PagerDutyResponse:
    """Delete the PagerDuty contact point and remove its notification route.

    :param pmm_api: The PMM API client. Raises 503 if PMM is not configured.
    :type pmm_api: PMMRemoteAPI
    :return: ``status="deleted"``.
    :rtype: PagerDutyResponse
    :raises HTTPNotFoundException: If no PagerDuty contact point exists.
    :raises HTTPBadGatewayException: If the PMM upstream call fails.
    """
    try:
        contact_points = await pmm_api.list_contact_points()
    except (HTTPException, OSError):
        logger.exception("Failed to delete PagerDuty contact point")
        raise HTTPBadGatewayException(
            detail="Failed to delete PagerDuty configuration"
        ) from None
    pd_cp = find_pagerduty_contact_point(contact_points)
    if pd_cp is None:
        raise HTTPNotFoundException(detail="PagerDuty contact point not found")
    try:
        policy = await pmm_api.get_notification_policy()
        policy.routes = [
            r
            for r in policy.routes
            if r.get("receiver") != PAGERDUTY_CONTACT_POINT_NAME
        ]
        await pmm_api.update_notification_policy(policy)
        await pmm_api.delete_contact_point(pd_cp.uid)
    except (HTTPException, OSError):
        logger.exception("Failed to delete PagerDuty contact point")
        raise HTTPBadGatewayException(
            detail="Failed to delete PagerDuty configuration"
        ) from None
    return PagerDutyResponse(status="deleted")


@router.post("/push")
@require_minimum_role(UserRole.EDITOR)
async def alerts_api_push(
    payload: PushRequest,
    pmm_api: RequiredPMMAPIDep,
    alert_templates: AlertTemplatesDep,
    folder: RequiredAlertFolderDep,
    present_names: PMMPresentNamesDep,
) -> PushResponse:
    """Push selected alert templates to PMM as rules.

    Mirror :func:`app.extensions.apps.alerts.routes.alerts_push` over JSON.
    When the template is already present in PMM, report ``success`` if
    ``create_rule`` recreates a missing rule, ``skipped`` on a rule-title
    collision, and ``error`` for any other failure. When the template is not
    present, preserve the conflict-retry path: on ``create_rule`` collision
    call :func:`app.extensions.apps.alerts.restore.delete_conflicting_rules` and
    retry once.

    :param payload: Push request body listing template names to push.
    :type payload: PushRequest
    :param pmm_api: The PMM API client. Raises 503 if PMM is not configured.
    :type pmm_api: PMMRemoteAPI
    :param alert_templates: Local alert templates grouped by service type.
    :type alert_templates: AlertTemplatesDep
    :param folder: The PMM alert folder. Raises 502 if unreachable.
    :type folder: Folder
    :param present_names: Template names already present in PMM, or ``None`` if
        PMM is unreachable for the listing call.
    :type present_names: set[str] | None
    :return: A ``PushResponse`` with one :class:`PushItemResult` per template.
    :rtype: PushResponse
    """
    all_templates = {t.name: t for ts in alert_templates.values() for t in ts}
    results: list[PushItemResult] = []
    for name in payload.selected_templates:
        template = all_templates.get(name)
        if template is None:
            results.append(
                PushItemResult(name=name, status="error", message="Template not found")
            )
            continue

        if present_names is not None and name in present_names:
            try:
                await pmm_api.create_rule(
                    name=template.name,
                    template_name=template.name,
                    folder_uid=folder.uid,
                    for_duration=DEFAULT_FOR_DURATION,
                    group=alerts_settings.ALERT_FOLDER_NAME,
                )
            except (HTTPException, OSError) as exc:
                detail = exception_detail(exc)
                if "conflicts with existing" in detail:
                    logger.debug("Rule already exists for %s", name, exc_info=True)
                    results.append(
                        PushItemResult(
                            name=name,
                            status="skipped",
                            message="Already present in PMM",
                        )
                    )
                else:
                    results.append(
                        PushItemResult(name=name, status="error", message=detail)
                    )
                continue
            results.append(
                PushItemResult(
                    name=name, status="success", message="Pushed successfully"
                )
            )
            continue

        try:
            pmm_yaml = to_pmm_template_yaml(template)
            await pmm_api.create_template(pmm_yaml)
        except (HTTPException, OSError) as exc:
            detail = exception_detail(exc)
            results.append(PushItemResult(name=name, status="error", message=detail))
            continue

        try:
            await pmm_api.create_rule(
                name=template.name,
                template_name=template.name,
                folder_uid=folder.uid,
                for_duration=DEFAULT_FOR_DURATION,
                group=alerts_settings.ALERT_FOLDER_NAME,
            )
            results.append(
                PushItemResult(
                    name=name, status="success", message="Pushed successfully"
                )
            )
        except (HTTPException, OSError) as exc:
            detail = exception_detail(exc)
            if "conflicts with existing" not in detail:
                results.append(
                    PushItemResult(
                        name=name,
                        status="error",
                        message=f"Template created but rule failed: {detail}",
                    )
                )
                continue
            try:
                await delete_conflicting_rules(pmm_api, template.name, folder.uid)
                await pmm_api.create_rule(
                    name=template.name,
                    template_name=template.name,
                    folder_uid=folder.uid,
                    for_duration=DEFAULT_FOR_DURATION,
                    group=alerts_settings.ALERT_FOLDER_NAME,
                )
                results.append(
                    PushItemResult(
                        name=name,
                        status="success",
                        message="Pushed successfully (replaced conflicting rule)",
                    )
                )
            except (HTTPException, OSError) as retry_exc:
                retry_detail = exception_detail(retry_exc)
                results.append(
                    PushItemResult(
                        name=name,
                        status="error",
                        message=(f"Template created but rule failed: {retry_detail}"),
                    )
                )

    return PushResponse(results=results)
