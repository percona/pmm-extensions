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

"""Define tests for the app.extensions.apps.alerts.celery module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.extensions.apps.alerts.celery as extensions_celery
from app.extensions.apps.alerts.config import AlertsSettings
from app.extensions.apps.alerts.crud import AlertBackupManager
from app.extensions.apps.alerts.models import AlertBackup
from app.extensions.clients.pmm import (
    AlertRule,
    ContactPoint,
    Folder,
    NotificationPolicy,
    PMMRemoteAPI,
)
from app.extensions.clients.pmm import (
    AlertTemplate as PMMAlertTemplate,
)

EXPECTED_BACKUP_COUNT_AFTER_DIFF = 2
MODULE = "app.extensions.apps.alerts.celery"

_MOCK_CONTACT_POINT_COUNT = 2


def _mock_pmm_api():
    """Build a mock PMMRemoteAPI with alert methods."""
    api = AsyncMock(spec=PMMRemoteAPI)
    api.list_templates = AsyncMock(
        return_value=[
            PMMAlertTemplate(name="t1", summary="Template 1", template="yaml1"),
        ]
    )
    api.list_rules = AsyncMock(
        return_value=[
            AlertRule(uid="r1", title="Rule 1"),
        ]
    )
    api.list_contact_points = AsyncMock(
        return_value=[
            ContactPoint(uid="cp1", name="CP 1", type="email"),
            ContactPoint(uid="cp2", name="CP 2", type="slack"),
        ]
    )
    api.get_notification_policy = AsyncMock(
        return_value=NotificationPolicy(
            receiver="default",
        )
    )
    api.list_folders = AsyncMock(
        return_value=[
            Folder(uid="f1", title="Folder 1", id=1),
        ]
    )
    return api


def _patch_pmm_settings(mocker, *, retention=10):
    """Patch alerts PMM config inside _backup_alert_config."""
    mocker.patch(
        "app.extensions.apps.alerts.celery.alerts_settings",
        MagicMock(spec=AlertsSettings, BACKUP_RETENTION=retention),
    )


def _patch_session(mocker, session):
    """Patch get_async_session_maker to return the test session."""
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
    mocker.patch(f"{MODULE}.get_async_session_maker", return_value=mock_session_maker)


class TestBackupAlertConfig:
    """Test the _backup_alert_config async function."""

    @pytest.fixture(autouse=True)
    def _never_cancel(self, mocker):
        """Keep the drain check from reading the real DB in happy-path tests."""
        mocker.patch(f"{MODULE}.should_cancel", new=AsyncMock(return_value=False))

    @pytest.mark.asyncio
    async def test_backup_success(self, session, mocker) -> None:
        """Assert a backup row is created with correct data and metadata."""
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        await extensions_celery._backup_alert_config()

        results = await AlertBackupManager.list(session)
        assert len(results) == 1
        backup = results[0]
        assert backup.metadata_["template_count"] == 1
        assert backup.metadata_["rule_count"] == 1
        assert backup.metadata_["contact_point_count"] == _MOCK_CONTACT_POINT_COUNT
        assert backup.metadata_["folder_count"] == 1
        assert len(backup.data["templates"]) == 1
        assert len(backup.data["rules"]) == 1
        assert len(backup.data["contact_points"]) == _MOCK_CONTACT_POINT_COUNT
        assert len(backup.data["folders"]) == 1

    @pytest.mark.asyncio
    async def test_backup_pmm_not_configured(self, mocker) -> None:
        """Assert no backup is created when PMM is not configured."""
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=None),
        )
        mock_session_maker = mocker.patch(f"{MODULE}.get_async_session_maker")

        await extensions_celery._backup_alert_config()

        mock_session_maker.assert_not_called()

    @pytest.mark.asyncio
    async def test_backup_pmm_api_error(self, mocker) -> None:
        """Assert API errors are logged without crashing the task."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_templates = AsyncMock(
            side_effect=ConnectionError("PMM unreachable")
        )
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        mock_session_maker = mocker.patch(f"{MODULE}.get_async_session_maker")

        await extensions_celery._backup_alert_config()

        mock_session_maker.assert_not_called()

    @pytest.mark.asyncio
    async def test_backup_retention_cleanup(self, session, mocker) -> None:
        """Assert oldest backups beyond retention limit are deleted."""
        retention = 3
        for i in range(5):
            backup = AlertBackup(
                data={"index": i},
                metadata_={"count": i},
            )
            await AlertBackupManager.save(session, backup)

        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker, retention=retention)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        await extensions_celery._backup_alert_config()

        results = await AlertBackupManager.list(session)
        assert len(results) == retention

    @pytest.mark.asyncio
    async def test_backup_retention_configurable(self, session, mocker) -> None:
        """Assert retention limit is respected with custom value."""
        retention = 2
        for i in range(4):
            backup = AlertBackup(
                data={"index": i},
                metadata_={"count": i},
            )
            await AlertBackupManager.save(session, backup)

        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker, retention=retention)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        await extensions_celery._backup_alert_config()

        results = await AlertBackupManager.list(session)
        assert len(results) == retention

    @pytest.mark.asyncio
    async def test_backup_retention_boundary_keeps_all_at_limit(
        self, session, mocker
    ) -> None:
        """Assert no delete fires when the new backup brings the count to exactly the limit.

        The cleanup guard is ``len(all_backups) > retention``; at ``== retention``
        it must not delete. Pins the boundary against a ``>=`` off-by-one.
        """
        retention = 3
        for i in range(retention - 1):
            await AlertBackupManager.save(
                session, AlertBackup(data={"index": i}, metadata_={"count": i})
            )
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker, retention=retention)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)
        delete_where = mocker.spy(AlertBackupManager, "delete_where")

        await extensions_celery._backup_alert_config()

        results = await AlertBackupManager.list(session)
        assert len(results) == retention
        delete_where.assert_not_called()

    @pytest.mark.asyncio
    async def test_backup_skips_duplicate(self, session, mocker) -> None:
        """Assert no new backup is created when data is unchanged."""
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        await extensions_celery._backup_alert_config()
        results_after_first = await AlertBackupManager.list(session)
        assert len(results_after_first) == 1

        await extensions_celery._backup_alert_config()
        results_after_second = await AlertBackupManager.list(session)
        assert len(results_after_second) == 1

    @pytest.mark.asyncio
    async def test_backup_skips_duplicate_despite_reordered_api_response(
        self, session, mocker
    ) -> None:
        """Assert dedup works even when the API returns items in different order."""
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        await extensions_celery._backup_alert_config()

        mock_api.list_contact_points = AsyncMock(
            return_value=[
                ContactPoint(uid="cp2", name="CP 2", type="slack"),
                ContactPoint(uid="cp1", name="CP 1", type="email"),
            ]
        )

        await extensions_celery._backup_alert_config()
        results = await AlertBackupManager.list(session)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_backup_saves_when_data_differs(self, session, mocker) -> None:
        """Assert a new backup is created when data changes."""
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)

        existing = AlertBackup(
            data={
                "templates": [],
                "rules": [],
                "contact_points": [],
                "notification_policy": {},
                "folders": [],
            },
            metadata_={
                "template_count": 0,
                "rule_count": 0,
                "contact_point_count": 0,
                "folder_count": 0,
            },
        )
        await AlertBackupManager.save(session, existing)

        await extensions_celery._backup_alert_config()

        results = await AlertBackupManager.list(session)
        assert len(results) == EXPECTED_BACKUP_COUNT_AFTER_DIFF


class TestBackupAlertConfigCooperativeCancel:
    """``_backup_alert_config`` honours the cooperative-cancel safe points."""

    @pytest.mark.asyncio
    async def test_stops_before_fetch_on_cancel(self, mocker):
        """Skip the PMM round-trip entirely when cancelled before the fetch."""
        mock_api = _mock_pmm_api()
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        mocker.patch(f"{MODULE}.should_cancel", new=AsyncMock(return_value=True))

        await extensions_celery._backup_alert_config()

        mock_api.list_templates.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stops_before_write_on_cancel(self, session, mocker):
        """Skip the backup write when cancelled after the fetch, leaving prior data."""
        mock_api = _mock_pmm_api()
        _patch_pmm_settings(mocker)
        mocker.patch(
            "app.extensions.apps.alerts.celery.resolve_pmm_api",
            new=AsyncMock(return_value=mock_api),
        )
        _patch_session(mocker, session)
        mocker.patch(
            f"{MODULE}.should_cancel", new=AsyncMock(side_effect=[False, True])
        )
        save = mocker.spy(AlertBackupManager, "save")

        await extensions_celery._backup_alert_config()

        mock_api.list_templates.assert_awaited()
        assert len(await AlertBackupManager.list(session)) == 0
        save.assert_not_called()
