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

"""Define tests for the app.extensions.models module."""

from datetime import datetime, timedelta, timezone, UTC
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.extensions.models import (
    AppLifecycleEnum,
    AppState,
    AppStateBase,
    AppStateWrite,
    SyncInventoryEntityTypeEnum,
    SyncItem,
    SyncStatusEnum,
)


class TestAppStateModel:
    """Test suite for the AppState model and its companions."""

    def test_base_lifecycle_state_defaults_to_enabled(self):
        """The shared base column-default for ``lifecycle_state`` is ``ENABLED``."""
        assert (
            AppStateBase(app_key="snippets").lifecycle_state is AppLifecycleEnum.ENABLED
        )

    def test_base_requires_app_key(self):
        """``app_key`` has no default — omitting it fails validation."""
        with pytest.raises(ValidationError):
            AppStateBase()

    def test_base_rejects_empty_app_key(self):
        """``app_key`` is a ``NonEmptyStr`` — an empty string is rejected."""
        with pytest.raises(ValidationError):
            AppStateBase(app_key="")

    def test_table_model_lifecycle_state_defaults_to_enabled(self):
        """The table model inherits the ``lifecycle_state=ENABLED`` column default."""
        assert AppState(app_key="snippets").lifecycle_state is AppLifecycleEnum.ENABLED

    def test_write_model_requires_lifecycle_state(self):
        """The write payload requires ``lifecycle_state`` — it has no default."""
        with pytest.raises(ValidationError):
            AppStateWrite()

    def test_write_model_rejects_unknown_lifecycle_state(self):
        """The write payload rejects a value outside ``AppLifecycleEnum``."""
        with pytest.raises(ValidationError):
            AppStateWrite(lifecycle_state="BOGUS")


def _finished_item(created_at: datetime, updated_at: datetime | None) -> SyncItem:
    """Build a failed node item with the given timestamps."""
    return SyncItem(
        entity_id=1,
        entity_type=SyncInventoryEntityTypeEnum.NODE,
        status=SyncStatusEnum.FAILED,
        sync_instance_id=uuid4(),
        created_at=created_at,
        updated_at=updated_at,
    )


class TestSyncItemFinishedAt:
    """Test when a finished sync item is taken to have reached its final status."""

    def test_reads_the_last_write_time(self):
        """Prefer the update time over the creation time."""
        created = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        updated = created + timedelta(minutes=5)

        assert _finished_item(created, updated).finished_at == updated

    def test_falls_back_to_the_creation_time(self):
        """Use the creation time for an item that was never updated."""
        created = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

        assert _finished_item(created, None).finished_at == created

    def test_returns_utc(self):
        """Normalize an offset timestamp to UTC."""
        offset = timezone(timedelta(hours=2))
        updated = datetime(2026, 10, 1, 14, 0, tzinfo=offset)

        finished = _finished_item(updated, updated).finished_at

        assert finished.tzinfo is UTC
        assert finished == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def test_is_neither_a_column_nor_serialized(self):
        """Keep the derived time out of the table and the model's dump."""
        item = _finished_item(datetime(2026, 10, 1, tzinfo=UTC), None)

        columns = SyncItem.__table__.columns

        assert {"created_at", "updated_at"} <= set(columns.keys())
        assert "finished_at" not in columns
        assert item.model_dump().keys() == SyncItem.model_fields.keys()
