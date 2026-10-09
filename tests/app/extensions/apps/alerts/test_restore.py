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

"""Define tests for restore from backup functionality."""

from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import HTTPException

from app.core.exceptions import HTTPConflictException, HTTPNotFoundException
from app.extensions.apps.alerts.models import (
    AlertBackup,
    AlertSeverity,
    AlertTemplate,
    ServiceType,
)
from app.extensions.apps.alerts.restore import restore_from_backup
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

_SAMPLE_TEMPLATE_COUNT = 2
_SAMPLE_CONTACT_POINT_COUNT = 2
_MISSING_TEMPLATE_RULE_COUNT = 2


def _sample_backup_data():
    """Return sample backup data for testing."""
    return {
        "templates": [
            {"name": "t1", "summary": "Template 1", "template": "yaml1"},
            {"name": "t2", "summary": "Template 2", "template": "yaml2"},
        ],
        "rules": [
            {
                "uid": "r1",
                "title": "Rule 1",
                "labels": {"template_name": "t1"},
                "folder_uid": "f1",
                "for": "5m",
                "group": "PMM Extensions Alerts",
            },
        ],
        "contact_points": [
            {
                "uid": "cp1",
                "name": "Email",
                "type": "email",
                "settings": {"to": "a@b.com"},
            },
            {
                "uid": "cp2",
                "name": "Slack",
                "type": "slack",
                "settings": {"url": "https://hooks.slack.com/x"},
            },
        ],
        "notification_policy": {
            "receiver": "Email",
            "group_by": ["alertname"],
            "routes": [],
        },
        "folders": [
            {"uid": "f1", "title": "Folder 1", "id": 1},
        ],
    }


class TestRestoreFromBackup:
    """Test the ``restore_from_backup`` helper function."""

    @pytest.mark.asyncio
    async def test_full_restore(self):
        """Assert all resource types are restored with correct counts."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = [
            AlertRule(uid="existing1", title="Old Rule"),
        ]
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.side_effect = [False, True, True]
        mock_api.create_template.return_value = PMMAlertTemplate(
            name="t1", summary="Template 1", template="yaml1"
        )
        mock_api.create_rule.return_value = AlertRule(uid="new1", title="Rule 1")
        mock_api.list_contact_points.return_value = [
            ContactPoint(uid="existing-cp", name="Email", type="email"),
        ]
        mock_api.update_notification_policy.return_value = NotificationPolicy(
            receiver="Email"
        )

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={"template_count": 2, "rule_count": 1},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_deleted"] == 1
        assert results["templates"]["created"] == 1
        assert results["templates"]["skipped"] == 1
        assert results["rules_created"] == 1
        assert results["rules_skipped"] == 0
        assert results["contact_points"]["created"] == 1
        assert results["contact_points"]["updated"] == 1
        assert results["notification_policies"] == "restored"

        mock_api.delete_rule.assert_awaited_once_with("existing1")
        mock_api.create_rule.assert_awaited_once()
        mock_api.update_contact_point.assert_awaited_once()
        mock_api.create_contact_point.assert_awaited_once()
        mock_api.update_notification_policy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_templates_all_skipped_when_existing(self):
        """Assert existing templates are skipped and not recreated."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = []

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["templates"]["created"] == 0
        assert results["templates"]["skipped"] == _SAMPLE_TEMPLATE_COUNT
        mock_api.create_template.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_contact_points_updated(self):
        """Assert existing contact points are updated via the provisioning API."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = [
            ContactPoint(uid="existing-cp", name="Email", type="email"),
            ContactPoint(uid="existing-cp2", name="Slack", type="slack"),
        ]

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["contact_points"]["updated"] == _SAMPLE_CONTACT_POINT_COUNT
        assert results["contact_points"]["created"] == 0
        assert mock_api.update_contact_point.await_count == _SAMPLE_CONTACT_POINT_COUNT
        mock_api.delete_contact_point.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_contact_points_fallback_delete_create_on_404(self):
        """Assert update 404 triggers delete+create fallback."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = [
            ContactPoint(uid="existing-cp", name="Email", type="email"),
        ]
        mock_api.update_contact_point.side_effect = HTTPNotFoundException("Not Found")

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["contact_points"]["updated"] == 1
        assert results["contact_points"]["created"] == 1
        mock_api.delete_contact_point.assert_awaited_once_with("existing-cp")
        assert mock_api.create_contact_point.await_count == _SAMPLE_CONTACT_POINT_COUNT

    @pytest.mark.asyncio
    async def test_contact_points_skipped_when_unmanageable(self):
        """Assert contact points are skipped when both update and delete return 404."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = [
            ContactPoint(uid="existing-cp", name="Email", type="email"),
        ]
        mock_api.update_contact_point.side_effect = HTTPNotFoundException("Not Found")
        mock_api.delete_contact_point.side_effect = HTTPNotFoundException("Not Found")

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["contact_points"]["updated"] == 1
        assert results["contact_points"]["created"] == 1
        mock_api.create_contact_point.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_backup_data(self):
        """Assert an empty backup produces zero counts without errors."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.list_contact_points.return_value = []

        backup = AlertBackup(
            id=1,
            data={},
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_deleted"] == 0
        assert results["templates"]["created"] == 0
        assert results["templates"]["skipped"] == 0
        assert results["rules_created"] == 0
        assert results["rules_skipped"] == 0
        assert results["contact_points"]["created"] == 0
        assert results["contact_points"]["updated"] == 0
        assert results["notification_policies"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_notification_policies(self):
        """Assert restore skips notification policies when not in backup."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.list_contact_points.return_value = []

        backup = AlertBackup(
            id=1,
            data={"templates": [], "rules": [], "contact_points": []},
            metadata_={},
        )

        results = await restore_from_backup(mock_api, backup)

        assert results["notification_policies"] == "skipped"
        mock_api.update_notification_policy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_restore_skips_existing_rules(self):
        """Assert a second restore of the same backup skips existing rules."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = [
            AlertRule(uid="r1", title="Rule 1"),
        ]
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = []

        backup = AlertBackup(id=1, data=_sample_backup_data(), metadata_={})

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_deleted"] == 0
        assert results["rules_created"] == 0
        assert results["rules_skipped"] == 1
        mock_api.delete_rule.assert_not_awaited()
        mock_api.create_rule.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_template_auto_pushed_from_local(self, mocker):
        """Assert a missing PMM template is auto-pushed from local definitions."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.side_effect = [True, True, False]
        mock_api.create_rule.return_value = AlertRule(uid="new1", title="Rule 1")
        mock_api.list_contact_points.return_value = []

        local_tmpl = AlertTemplate(
            name="t1",
            service_type=ServiceType.MYSQL,
            expression="up == 0",
            default_threshold=0,
            severity=AlertSeverity.CRITICAL,
            description="Test",
            summary="Test summary",
        )
        mocker.patch(
            "app.extensions.apps.alerts.restore.get_alert_templates",
            return_value={ServiceType.MYSQL: (local_tmpl,)},
        )
        mocker.patch(
            "app.extensions.apps.alerts.restore.to_pmm_template_yaml",
            return_value="yaml-from-local",
        )

        backup = AlertBackup(id=1, data=_sample_backup_data(), metadata_={})

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_created"] == 1
        mock_api.create_template.assert_awaited_once_with("yaml-from-local")
        mock_api.create_rule.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_template_no_local_definition_skips(self, mocker):
        """Assert a rule is skipped when template is missing from PMM and locally."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.side_effect = [True, True, False]
        mock_api.list_contact_points.return_value = []

        mocker.patch(
            "app.extensions.apps.alerts.restore.get_alert_templates",
            return_value={},
        )

        backup = AlertBackup(id=1, data=_sample_backup_data(), metadata_={})

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_created"] == 0
        assert results["rules_skipped"] == 1
        mock_api.create_rule.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rules_skipped_when_template_not_found(self):
        """Assert rules are skipped when PMM returns 404 for unknown template."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = []
        mock_api.create_rule.side_effect = HTTPNotFoundException(
            "Unknown template nonexistent"
        )

        data = _sample_backup_data()
        data["rules"] = [
            {
                "uid": "r1",
                "title": "Rule With Bad Template",
                "labels": {"template_name": "nonexistent"},
                "for": "5m",
                "group": "PMM Extensions Alerts",
            },
            {
                "uid": "r2",
                "title": "Rule With Missing Label",
                "labels": {},
                "for": "5m",
                "group": "PMM Extensions Alerts",
            },
        ]

        backup = AlertBackup(id=1, data=data, metadata_={})

        results = await restore_from_backup(mock_api, backup)

        assert results["rules_created"] == 0
        assert results["rules_skipped"] == _MISSING_TEMPLATE_RULE_COUNT
        assert mock_api.create_rule.await_count == _MISSING_TEMPLATE_RULE_COUNT

    @pytest.mark.asyncio
    async def test_rules_non_404_error_propagates(self):
        """Assert a non-404 create_rule error is not swallowed by the 404 skip path."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = [
            Folder(uid="f1", title="PMM Extensions Alerts", id=1),
        ]
        mock_api.template_exists.return_value = True
        mock_api.list_contact_points.return_value = []
        mock_api.create_rule.side_effect = HTTPConflictException("Rule already exists")

        data = _sample_backup_data()
        data["rules"] = [
            {
                "uid": "r1",
                "title": "Conflicting Rule",
                "labels": {"template_name": "nonexistent"},
                "for": "5m",
                "group": "PMM Extensions Alerts",
            },
        ]

        backup = AlertBackup(id=1, data=data, metadata_={})

        with pytest.raises(HTTPConflictException):
            await restore_from_backup(mock_api, backup)

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """Assert API errors are not silenced during restore."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.side_effect = HTTPException(
            status_code=502, detail="Bad Gateway"
        )

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        with pytest.raises(HTTPException):
            await restore_from_backup(mock_api, backup)

    @pytest.mark.asyncio
    async def test_restore_creates_folder_when_missing(self):
        """Assert a new folder is created when no matching folder exists."""
        mock_api = AsyncMock(spec=PMMRemoteAPI)
        mock_api.list_rules.return_value = []
        mock_api.list_folders.return_value = []
        mock_api.create_folder.return_value = Folder(
            uid="new-f1", title="PMM Extensions Alerts", id=2
        )
        mock_api.template_exists.side_effect = [False, False, True]
        mock_api.create_template.return_value = PMMAlertTemplate(
            name="t1", summary="Template 1", template="yaml1"
        )
        mock_api.create_rule.return_value = AlertRule(uid="r1", title="Rule 1")
        mock_api.list_contact_points.return_value = []

        backup = AlertBackup(
            id=1,
            data=_sample_backup_data(),
            metadata_={},
        )

        await restore_from_backup(mock_api, backup)

        mock_api.create_folder.assert_awaited_once_with("PMM Extensions Alerts")
        mock_api.create_rule.assert_awaited_once()
        call_kwargs = mock_api.create_rule.call_args.kwargs
        assert call_kwargs["folder_uid"] == "new-f1"


class TestGetRecentBackups:
    """Test the ``get_recent_backups`` dependency."""

    @pytest.mark.asyncio
    async def test_returns_backups_from_manager(self, mocker):
        """Assert recent backups are fetched via AlertBackupManager.list."""
        from app.extensions.apps.alerts.deps import (  # noqa: PLC0415
            _MAX_SIDEBAR_BACKUPS,
            get_recent_backups,
        )

        backups = [AlertBackup(id=i, data={}, metadata_={}) for i in range(1, 6)]
        mock_list = AsyncMock(return_value=backups)
        mocker.patch(
            "app.extensions.apps.alerts.deps.AlertBackupManager.list",
            new=mock_list,
        )
        mock_session = AsyncMock()

        result = await get_recent_backups(mock_session)

        assert result == backups
        mock_list.assert_awaited_once_with(mock_session, limit=_MAX_SIDEBAR_BACKUPS)

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_backups(self, mocker):
        """Assert an empty list is returned when no backups exist."""
        from app.extensions.apps.alerts.deps import (  # noqa: PLC0415
            _MAX_SIDEBAR_BACKUPS,
            get_recent_backups,
        )

        mock_list = AsyncMock(return_value=[])
        mocker.patch(
            "app.extensions.apps.alerts.deps.AlertBackupManager.list",
            new=mock_list,
        )
        mock_session = AsyncMock()

        result = await get_recent_backups(mock_session)

        assert result == []
        mock_list.assert_awaited_once_with(mock_session, limit=_MAX_SIDEBAR_BACKUPS)
