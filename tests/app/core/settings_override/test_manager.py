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

"""Tests for :class:`SettingsOverrideManager`."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.exceptions import HTTPConflictException
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import SettingOverride
from tests.app.core.settings_override.conftest import (
    EXTENSIONS_SETTINGS_TOKEN,
    TASKS_SETTINGS_TOKEN,
)


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(session: AsyncSession) -> None:
    """An override row created via the manager is retrievable by id."""
    created = await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="CONNECTIVITY_CHECK_DEFAULT",
            value=False,
            is_active=True,
        ),
    )
    fetched = await SettingsOverrideManager.get(session, id=created.id)
    assert fetched is not None
    assert fetched.setting_class == "EXTENSIONS_SETTINGS"
    assert fetched.key == "CONNECTIVITY_CHECK_DEFAULT"
    assert fetched.value is False


@pytest.mark.asyncio
async def test_list_filters_by_setting_class(session: AsyncSession) -> None:
    """``list`` returns only rows matching the requested setting class."""
    await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=10,
        ),
    )
    await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=TASKS_SETTINGS_TOKEN,
            key="STALENESS_THRESHOLD_SECONDS",
            value=7200,
        ),
    )
    extensions_rows = await SettingsOverrideManager.list(
        session, setting_class=EXTENSIONS_SETTINGS_TOKEN
    )
    tasks_rows = await SettingsOverrideManager.list(
        session, setting_class=TASKS_SETTINGS_TOKEN
    )
    assert [r.key for r in extensions_rows] == ["SYNC_REFRESH_TIME"]
    assert [r.key for r in tasks_rows] == ["STALENESS_THRESHOLD_SECONDS"]


@pytest.mark.asyncio
async def test_duplicate_setting_class_and_key_raises_conflict(
    session: AsyncSession,
) -> None:
    """Inserting two rows with the same ``(setting_class, key)`` raises a conflict."""
    await SettingsOverrideManager.create(
        session,
        SettingOverride(
            setting_class=EXTENSIONS_SETTINGS_TOKEN,
            key="SYNC_REFRESH_TIME",
            value=5,
        ),
    )
    with pytest.raises(HTTPConflictException):
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key="SYNC_REFRESH_TIME",
                value=10,
            ),
        )


@pytest.mark.asyncio
async def test_null_value_rejected_at_insert(session: AsyncSession) -> None:
    """SQL ``NULL`` for the ``value`` column is rejected by the schema."""
    from sqlalchemy import text  # noqa: PLC0415

    table_name = SettingOverride.__tablename__
    insert_stmt = text(
        f"INSERT INTO {table_name} "
        "(created_at, setting_class, key, value, is_active) "
        "VALUES (CURRENT_TIMESTAMP, 'EXTENSIONS_SETTINGS', 'k', NULL, 1)"
    )
    with pytest.raises(IntegrityError):
        await session.exec(insert_stmt)


@pytest.mark.asyncio
async def test_value_roundtrips_for_json_types(session: AsyncSession) -> None:
    """``value`` round-trips through JSON for primitive, list, dict, and null."""
    samples = [
        ("int_field", 42),
        ("str_field", "hello"),
        ("bool_field", True),
        ("list_field", [1, 2, 3]),
        ("dict_field", {"a": 1, "b": [True, "x"]}),
        ("null_field", None),
    ]
    for key, value in samples:
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN, key=key, value=value
            ),
        )
    rows = await SettingsOverrideManager.list(
        session, setting_class=EXTENSIONS_SETTINGS_TOKEN
    )
    by_key = {row.key: row.value for row in rows}
    for key, value in samples:
        assert by_key[key] == value


@pytest.mark.asyncio
async def test_update_where_bulk_deactivates(session: AsyncSession) -> None:
    """``update_where`` can flip ``is_active`` for a whole setting class."""
    for key in ("a", "b"):
        await SettingsOverrideManager.create(
            session,
            SettingOverride(
                setting_class=EXTENSIONS_SETTINGS_TOKEN,
                key=key,
                value=True,
                is_active=True,
            ),
        )
    await SettingsOverrideManager.update_where(
        session,
        {"is_active": False},
        setting_class=EXTENSIONS_SETTINGS_TOKEN,
    )
    rows = await SettingsOverrideManager.list(
        session, setting_class=EXTENSIONS_SETTINGS_TOKEN
    )
    assert all(row.is_active is False for row in rows)
