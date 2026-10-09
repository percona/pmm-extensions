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

"""Define tests for the app.extensions.apps.report.celery module."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import Ignore

import app.extensions.apps.report.celery as report_celery
from app.core.exceptions import HTTPInternalServerErrorException
from app.extensions.bundle_upload.plan import DeliveryPlanError

MODULE = "app.extensions.apps.report.celery"


class TestGenerateHealthReportCooperativeCancel:
    """``_generate_health_report`` honours the cooperative-cancel safe points."""

    @staticmethod
    def _mock_report() -> MagicMock:
        report = MagicMock()
        report.metadata.title = "Health Report"
        report.monitored.total_nodes = 1
        report.monitored.total_services = 1
        return report

    @pytest.mark.asyncio
    async def test_skips_generation_when_pmm_not_configured(self, mocker):
        """Skip report generation when PMM is not configured (``resolve_pmm_api`` -> ``None``)."""
        mocker.patch(
            "app.extensions.apps.report.celery.resolve_pmm_api",
            new=AsyncMock(return_value=None),
        )
        generate = mocker.patch(
            "app.extensions.apps.report.celery.generate_report",
            new=AsyncMock(return_value=self._mock_report()),
        )

        await report_celery._generate_health_report(upload=True)

        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stops_before_generation_on_cancel(self, mocker):
        """A cancel before generation skips report generation entirely."""
        mocker.patch(
            "app.extensions.apps.report.celery.resolve_pmm_api",
            new=AsyncMock(return_value=MagicMock()),
        )
        generate = mocker.patch(
            "app.extensions.apps.report.celery.generate_report",
            new=AsyncMock(return_value=self._mock_report()),
        )
        mocker.patch(f"{MODULE}.should_cancel", new=AsyncMock(return_value=True))

        await report_celery._generate_health_report(upload=True)

        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scheduled_upload_reuses_generated_report(self, mocker):
        """Scheduled upload renders/uploads same report without second collection."""
        report = self._mock_report()
        mocker.patch(
            "app.extensions.apps.report.celery.resolve_pmm_api",
            new=AsyncMock(return_value=MagicMock()),
        )
        mock_settings = mocker.patch(f"{MODULE}.health_report_settings")
        mock_settings.is_upload_configured = True
        generate = mocker.patch(
            "app.extensions.apps.report.celery.generate_report",
            new=AsyncMock(return_value=report),
        )
        generate_pdf = mocker.patch(
            "app.extensions.apps.report.celery.generate_pdf_report",
            new=AsyncMock(return_value=b"%PDF-1.4"),
        )
        upload = mocker.patch(
            "app.extensions.apps.report.celery.upload_pdf_report",
            new=AsyncMock(return_value={"sys_id": "abc123", "status": "uploaded"}),
        )
        mocker.patch(f"{MODULE}.should_cancel", new=AsyncMock(return_value=False))

        await report_celery._generate_health_report(upload=True)

        generate.assert_awaited_once()
        generate_pdf.assert_awaited_once_with(report)
        upload.assert_awaited_once_with(report, b"%PDF-1.4")

    @pytest.mark.parametrize(
        "failure",
        [
            HTTPInternalServerErrorException(),
            DeliveryPlanError("Bundle is 1 bytes, above the configured 30 MiB limit."),
        ],
    )
    @pytest.mark.asyncio
    async def test_scheduled_upload_failure_is_logged_not_raised(
        self, mocker, caplog, failure
    ):
        """Keep the scheduled task alive when the upload fails, logging the cause."""
        mocker.patch(
            "app.extensions.apps.report.celery.resolve_pmm_api",
            new=AsyncMock(return_value=MagicMock()),
        )
        mock_settings = mocker.patch(f"{MODULE}.health_report_settings")
        mock_settings.is_upload_configured = True
        mocker.patch(
            "app.extensions.apps.report.celery.generate_report",
            new=AsyncMock(return_value=self._mock_report()),
        )
        mocker.patch(
            "app.extensions.apps.report.celery.generate_pdf_report",
            new=AsyncMock(return_value=b"%PDF-1.4"),
        )
        mocker.patch(
            "app.extensions.apps.report.celery.upload_pdf_report",
            new=AsyncMock(side_effect=failure),
        )
        mocker.patch(f"{MODULE}.should_cancel", new=AsyncMock(return_value=False))

        with caplog.at_level("ERROR", logger=MODULE):
            await report_celery._generate_health_report(upload=True)

        assert "Failed to upload health report" in caplog.text


class TestReportSnapshotJobs:
    """Report snapshot Celery jobs expose structured validation failures."""

    def test_render_pdf_invalid_snapshot_sets_failure_meta(self, mocker) -> None:
        """Invalid PDF snapshot stores safe structured FAILURE metadata."""
        update_state = mocker.patch.object(
            report_celery.render_report_pdf_job, "update_state"
        )

        with pytest.raises(Ignore):
            report_celery.render_report_pdf_job.run({})

        update_state.assert_called_once()
        _, kwargs = update_state.call_args
        assert kwargs["state"] == "FAILURE"
        assert kwargs["meta"]["error"] == "Invalid report snapshot"
        assert isinstance(kwargs["meta"]["errors"], list)

    def test_upload_invalid_snapshot_sets_failure_meta(self, mocker) -> None:
        """Invalid upload snapshot stores safe structured FAILURE metadata."""
        update_state = mocker.patch.object(
            report_celery.upload_report_snapshot_job, "update_state"
        )

        with pytest.raises(Ignore):
            report_celery.upload_report_snapshot_job.run({})

        update_state.assert_called_once()
        _, kwargs = update_state.call_args
        assert kwargs["state"] == "FAILURE"
        assert kwargs["meta"]["error"] == "Invalid report snapshot"
        assert isinstance(kwargs["meta"]["errors"], list)
