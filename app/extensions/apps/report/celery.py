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

"""Define Celery tasks for the report app.

This module is registered through the Celery ``include`` list so its
``@owned_by("report")`` tasks register at worker startup.
"""

import logging
from typing import Any, NoReturn

from celery import states
from celery import Task as CeleryTask
from celery.exceptions import Ignore
from fastapi import HTTPException
from pydantic import ValidationError

from app.celery import celery
from app.extensions.app_drain import owned_by, should_cancel
from app.extensions.apps.report.config import health_report_settings
from app.extensions.bundle_upload.plan import DeliveryPlanError

logger = logging.getLogger(__name__)


def _fail_invalid_report_snapshot(self: CeleryTask, exc: ValidationError) -> NoReturn:
    """Store structured validation failure metadata for report snapshot tasks.

    :param self: Bound Celery task instance.
    :type self: CeleryTask
    :param exc: Pydantic validation error.
    :type exc: ValidationError
    :raises Ignore: Stops task execution after storing FAILURE metadata.
    """
    self.update_state(
        state=states.FAILURE,
        meta={
            "error": "Invalid report snapshot",
            "errors": exc.errors(),
        },
    )
    raise Ignore from exc


@owned_by("report")
@celery.task(bind=True)
def render_report_pdf_job(
    self: CeleryTask, report_json: dict[str, Any]
) -> dict[str, str]:
    """Render a PDF from a report snapshot and stage it on shared disk.

    The rendered PDF is written to the shared artifact directory keyed by this
    job's id; only the download filename is returned through the Celery result
    backend so multi-MB blobs never transit Redis.

    :param self: Bound Celery task instance.
    :type self: CeleryTask
    :param report_json: Serialized report snapshot.
    :type report_json: dict[str, Any]
    :return: Download filename for the staged PDF artifact.
    :rtype: dict[str, str]
    """
    from app.extensions.apps.report.artifact_store import (  # noqa: PLC0415
        write_artifact,
    )
    from app.extensions.apps.report.job_service import (  # noqa: PLC0415
        report_pdf_filename,
    )
    from app.extensions.apps.report.models import ReportData  # noqa: PLC0415
    from app.extensions.apps.report.service import generate_pdf_report  # noqa: PLC0415

    try:
        report = ReportData.model_validate(report_json)
    except ValidationError as exc:
        _fail_invalid_report_snapshot(self, exc)
    pdf_bytes = celery.loop.run_until_complete(generate_pdf_report(report))
    write_artifact(self.request.id, pdf_bytes)
    return {"filename": report_pdf_filename(report)}


@owned_by("report")
@celery.task(bind=True)
def upload_report_snapshot_job(
    self: CeleryTask, report_json: dict[str, Any]
) -> dict[str, Any] | None:
    """Render and upload a PDF from a report JSON snapshot.

    An upload failure propagates, so the task lands in ``FAILURE`` carrying the
    intake's mapped exception rather than a success result.

    :param self: Bound Celery task instance.
    :param report_json: Serialized report snapshot.
    :return: The intake's response payload, or ``None`` when it answers a
        success status with a body that is not a JSON object.
    :raises Ignore: When the snapshot fails validation.
    :raises HTTPException: Propagates the project exception mapped from an
        intake error status.
    :raises DeliveryPlanError: When the rendered PDF exceeds the size cap.
    """
    from app.extensions.apps.report.models import ReportData  # noqa: PLC0415
    from app.extensions.apps.report.service import (  # noqa: PLC0415
        generate_pdf_report,
        upload_pdf_report,
    )

    try:
        report = ReportData.model_validate(report_json)
    except ValidationError as exc:
        _fail_invalid_report_snapshot(self, exc)
    pdf_bytes = celery.loop.run_until_complete(generate_pdf_report(report))
    return celery.loop.run_until_complete(upload_pdf_report(report, pdf_bytes))


@owned_by("report")
@celery.task
def generate_health_report(
    since: str = "now-7d",
    until: str = "now",
    sections: list[str] | None = None,
    *,
    full: bool = True,
    refresh: bool = False,
    upload: bool = False,
) -> None:
    """Define Celery task to generate a periodic PMM health report.

    :param since: Relative start of the report period.
    :type since: str
    :param until: Relative end of the report period.
    :type until: str
    :param sections: Optional list of sections to include.
    :type sections: list[str] | None
    :param full: Include all check results and full backup history.
    :type full: bool
    :param refresh: Force advisor refresh before fetching results.
    :type refresh: bool
    :param upload: Upload generated report to ServiceNow.
    :type upload: bool
    :return: None.
    :rtype: None
    """
    celery.loop.run_until_complete(
        _generate_health_report(
            since=since,
            until=until,
            full=full,
            refresh=refresh,
            sections=sections,
            upload=upload,
        )
    )


async def _generate_health_report(
    *,
    since: str = "now-7d",
    until: str = "now",
    full: bool = True,
    refresh: bool = False,
    sections: list[str] | None = None,
    upload: bool = False,
) -> None:
    """Generate a health report from PMM, log it, and optionally upload to ServiceNow.

    When *upload* is ``True`` and the global upload credentials are fully
    configured the report is rendered to PDF and uploaded.  If *upload* is
    requested but credentials are incomplete a warning is logged.  Upload
    failures are logged but do not prevent the task from completing.

    :param since: Relative start of the report period.
    :type since: str
    :param until: Relative end of the report period.
    :type until: str
    :param full: Include all check results and full backup history.
    :type full: bool
    :param refresh: Force advisor refresh before fetching results.
    :type refresh: bool
    :param sections: Optional list of sections to include.
    :type sections: list[str] | None
    :param upload: Upload generated report to ServiceNow.
    :type upload: bool
    :return: None.
    :rtype: None
    """
    from app.extensions.apps.report.service import (  # noqa: PLC0415
        generate_pdf_report,
        generate_report,
        upload_pdf_report,
    )
    from app.extensions.deps import resolve_pmm_api  # noqa: PLC0415

    pmm_api = await resolve_pmm_api()
    if pmm_api is None:
        logger.warning("PMM not configured, skipping health report generation")
        return

    if await should_cancel("report"):
        logger.info("Report app disabling; skipping health report generation.")
        return

    try:
        report = await generate_report(
            pmm_api,
            since=since,
            until=until,
            full=full,
            refresh=refresh,
            sections=sections,
        )
        logger.info(
            "Health report generated: %s (%d nodes, %d services)",
            report.metadata.title,
            report.monitored.total_nodes,
            report.monitored.total_services,
        )
    except (OSError, ValueError, LookupError, RuntimeError):
        logger.exception("Failed to generate health report")
        return

    if not upload:
        return

    if await should_cancel("report"):
        logger.info("Report app disabling; skipping health report upload.")
        return

    if not health_report_settings.is_upload_configured:
        reasons = health_report_settings.upload_disabled_reasons
        logger.warning(
            "Scheduled upload requested but upload is not fully configured: %s",
            "; ".join(reasons),
        )
        return

    try:
        pdf_bytes = await generate_pdf_report(report)
        result = await upload_pdf_report(report, pdf_bytes)
        logger.info("Health report uploaded to ServiceNow: %s", result)
    except (OSError, ValueError, RuntimeError, HTTPException, DeliveryPlanError):
        logger.exception("Failed to upload health report to ServiceNow")


@owned_by("report")
@celery.task
def purge_report_artifacts() -> None:
    """Delete staged PDF artifacts older than the configured retention TTL.

    Bounds shared-disk usage for the render/download flow: the render task
    stages a PDF per job and this sweep reaps stale ones, mirroring the Celery
    result-backend expiry so metadata and artifacts fall away together.

    :return: None.
    :rtype: None
    """
    from app.extensions.apps.report.artifact_store import purge_expired  # noqa: PLC0415

    removed = purge_expired(health_report_settings.artifact_ttl)
    if removed:
        logger.info("Purged %d expired report artifact(s)", removed)
