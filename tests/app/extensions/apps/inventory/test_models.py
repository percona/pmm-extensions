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

"""Define tests for app.extensions.apps.inventory.models module."""

import pytest
from pydantic import ValidationError

from app.extensions.apps.inventory.deps import AvailableSyncer
from app.extensions.apps.inventory.models import PluginTaskResponse


class TestPluginTaskResponse:
    """Tests for the PluginTaskResponse model."""

    def test_accepts_valid_data(self) -> None:
        """Ensure model instantiates from valid name and display_name."""
        task = PluginTaskResponse(name="inventory-sync", display_name="Inventory Sync")
        assert task.name == "inventory-sync"
        assert task.display_name == "Inventory Sync"

    def test_rejects_missing_name(self) -> None:
        """Ensure missing ``name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            PluginTaskResponse(display_name="Inventory Sync")

    def test_rejects_missing_display_name(self) -> None:
        """Ensure missing ``display_name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            PluginTaskResponse(name="inventory-sync")

    def test_name_must_be_string(self) -> None:
        """Ensure non-string ``name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            PluginTaskResponse(name=123, display_name="Inventory Sync")

    def test_display_name_must_be_string(self) -> None:
        """Ensure non-string ``display_name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            PluginTaskResponse(name="inventory-sync", display_name=123)

    def test_serializes_to_dict(self) -> None:
        """Ensure model_dump returns expected keys."""
        task = PluginTaskResponse(name="inventory-sync", display_name="Inventory Sync")
        dumped = task.model_dump()
        assert dumped == {"name": "inventory-sync", "display_name": "Inventory Sync"}


class TestAvailableSyncer:
    """Tests for the AvailableSyncer model."""

    def test_accepts_valid_data(self) -> None:
        """Ensure model instantiates from valid name and display_name."""
        syncer = AvailableSyncer(
            name="app.extensions.sync.syncers.pmm.PMMSyncer",
            display_name="PMM",
        )
        assert syncer.name == "app.extensions.sync.syncers.pmm.PMMSyncer"
        assert syncer.display_name == "PMM"

    def test_rejects_missing_name(self) -> None:
        """Ensure missing ``name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            AvailableSyncer(display_name="PMM")

    def test_rejects_missing_display_name(self) -> None:
        """Ensure missing ``display_name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            AvailableSyncer(name="app.extensions.sync.syncers.pmm.PMMSyncer")

    def test_name_must_be_string(self) -> None:
        """Ensure non-string ``name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            AvailableSyncer(name=42, display_name="PMM")

    def test_display_name_must_be_string(self) -> None:
        """Ensure non-string ``display_name`` raises ValidationError."""
        with pytest.raises(ValidationError):
            AvailableSyncer(
                name="app.extensions.sync.syncers.pmm.PMMSyncer", display_name=42
            )

    def test_serializes_to_dict(self) -> None:
        """Ensure model_dump returns expected keys."""
        syncer = AvailableSyncer(
            name="app.extensions.sync.syncers.pmm.PMMSyncer",
            display_name="PMM",
        )
        assert syncer.model_dump() == {
            "name": "app.extensions.sync.syncers.pmm.PMMSyncer",
            "display_name": "PMM",
        }
