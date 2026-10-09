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

"""Define Celery tasks for the alerts app.

This module is registered through the Celery ``include`` list so its
``@owned_by("alerts")`` task registers at worker startup.
"""

import asyncio
import logging

from sqlmodel import col

from app.celery import celery
from app.extensions.app_drain import owned_by, should_cancel
from app.extensions.apps.alerts.crud import AlertBackupManager
from app.extensions.db import get_async_session_maker

logger = logging.getLogger(__name__)


@owned_by("alerts")
@celery.task
def backup_alert_config() -> None:
    """Define Celery task to back up PMM alert configuration."""
    celery.loop.run_until_complete(_backup_alert_config())


async def _backup_alert_config() -> None:
    """Fetch alert configuration from PMM and store as a backup."""
    from app.extensions.apps.alerts.models import AlertBackup  # noqa: PLC0415
    from app.extensions.deps import resolve_pmm_api  # noqa: PLC0415

    pmm_api = await resolve_pmm_api()
    if pmm_api is None:
        logger.warning("PMM not configured, skipping alert backup")
        return

    if await should_cancel("alerts"):
        logger.info("Alerts app disabling; skipping alert backup fetch.")
        return

    try:
        (
            templates,
            rules,
            contact_points,
            notification_policy,
            folders,
        ) = await asyncio.gather(
            pmm_api.list_templates(),
            pmm_api.list_rules(),
            pmm_api.list_contact_points(),
            pmm_api.get_notification_policy(),
            pmm_api.list_folders(),
        )
    except Exception:
        logger.exception("Failed to fetch alert configuration from PMM")
        return

    data = {
        "templates": sorted(
            (t.model_dump() for t in templates), key=lambda t: t["name"]
        ),
        "rules": sorted((r.model_dump() for r in rules), key=lambda r: r["uid"]),
        "contact_points": sorted(
            (cp.model_dump() for cp in contact_points), key=lambda cp: cp["uid"]
        ),
        "notification_policy": notification_policy.model_dump(),
        "folders": sorted((f.model_dump() for f in folders), key=lambda f: f["uid"]),
    }
    metadata = {
        "template_count": len(templates),
        "rule_count": len(rules),
        "contact_point_count": len(contact_points),
        "folder_count": len(folders),
    }

    async_session = get_async_session_maker()
    async with async_session() as session:
        if await should_cancel("alerts", session=session):
            logger.info("Alerts app disabling; skipping alert backup write.")
            return
        recent = await AlertBackupManager.list(session, limit=1)
        if recent and recent[0].data == data:
            logger.debug("Alert config unchanged, skipping backup")
            return

        backup = AlertBackup(data=data, metadata_=metadata)
        await AlertBackupManager.save(session, backup)

        from app.extensions.apps.alerts.config import alerts_settings  # noqa: PLC0415

        retention = alerts_settings.BACKUP_RETENTION
        all_backups = await AlertBackupManager.list(session)
        if len(all_backups) > retention:
            ids_to_delete = [b.id for b in all_backups[retention:]]
            await AlertBackupManager.delete_where(
                session, col(AlertBackup.id).in_(ids_to_delete)
            )
