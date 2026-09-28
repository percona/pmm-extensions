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

"""Pin the ``settingoverride.setting_class`` storage-token derivation and the ``updated_by`` actor column."""

from pathlib import Path
from typing import ClassVar

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from sqlalchemy import Column, VARCHAR
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.alerts.config import AlertSettings
from app.core.config import BaseYamlSettings, Settings
from app.core.settings_override.constants import SETTING_CLASS_MAX_LENGTH
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import (
    setting_class_token,
    SettingClassEnum,
    SettingOverride,
    StaleActorUpdateError,
)
from app.extensions import apps
from app.extensions.apps.alerts.config import AlertsSettings
from app.extensions.apps.framework.registry import collect_app_owned_settings_classes
from app.extensions.apps.inventory.config import InventoryAppSettings
from app.extensions.apps.om_inventory.config import OmInventorySettings
from app.extensions.apps.report.config import HealthReportSettings
from app.extensions.config import App, ExtensionsSettings
from app.extensions.snippets.config import SnippetsSettings
from app.inventory.config import InventorySettings
from app.tasks.anonymizer.config import AnonymizerSettings
from app.tasks.config import TasksSettings
from tests.app.core.settings_override.conftest import (
    insert_override_row,
    LONG_USERNAME_LENGTH,
)

#: Sentinel numeric values for the actor-stamp guard tests: distinct so a
#: failed reject is caught by the value assertion, not just the exception.
_GUARD_ORIGINAL_VALUE = 5
_GUARD_UPDATED_VALUE = 99


#: Historical ``SettingClassEnum`` member names the database already stores.
#: Includes the two app-owned classes this ticket removes from the enum, so a
#: future class rename cannot silently orphan their override rows.
_HISTORICAL_TOKENS: tuple[tuple[type[BaseYamlSettings], str], ...] = (
    (AlertSettings, "ALERT_SETTINGS"),
    (AlertsSettings, "ALERTS_SETTINGS"),
    (AnonymizerSettings, "ANONYMIZER_SETTINGS"),
    (HealthReportSettings, "HEALTH_REPORT_SETTINGS"),
    (InventoryAppSettings, "INVENTORY_APP_SETTINGS"),
    (InventorySettings, "INVENTORY_SETTINGS"),
    (OmInventorySettings, "OM_INVENTORY_SETTINGS"),
    (ExtensionsSettings, "EXTENSIONS_SETTINGS"),
    (Settings, "SETTINGS"),
    (SnippetsSettings, "SNIPPETS_SETTINGS"),
    (TasksSettings, "TASKS_SETTINGS"),
)


@pytest.mark.parametrize(
    ("settings_cls", "expected"),
    _HISTORICAL_TOKENS,
    ids=[cls.__name__ for cls, _ in _HISTORICAL_TOKENS],
)
def test_derivation_matches_historical_member_name(
    settings_cls: type[BaseYamlSettings],
    expected: str,
) -> None:
    """Derive the same storage token every existing override row already uses."""
    assert setting_class_token(settings_cls) == expected


def test_classvar_override_pins_the_storage_token() -> None:
    """Store the pinned token when a class declares ``__setting_class_token__``."""

    class CustomTokenSettings(BaseYamlSettings):
        __setting_class_token__: ClassVar[str] = "CUSTOM_TOKEN"

    assert setting_class_token(CustomTokenSettings) == "CUSTOM_TOKEN"
    assert setting_class_token(CustomTokenSettings) != "CUSTOM_TOKEN_SETTINGS"


def test_every_reachable_settings_class_pins_its_token() -> None:
    """Fail when a settings class is added without pinning its storage token.

    ``_HISTORICAL_TOKENS`` cannot fail for a class it was never told about, so
    a class added after the pins were written would be free to be renamed,
    orphaning its override rows, which is the failure the pins exist to catch.
    App-owned classes are collected from every app that ships the declaration
    module, not from the activation list, so an inactive app is still covered.
    """
    declaring_apps = [
        App(module_name=path.parent.name)
        for path in Path(apps.__file__).parent.glob("*/app_owned_settings.py")
    ]
    pinned = {cls.__name__ for cls, _ in _HISTORICAL_TOKENS}
    reachable = {member.value for member in SettingClassEnum} | {
        entry.settings_cls.__name__
        for entry in collect_app_owned_settings_classes(declaring_apps)
    }
    assert not reachable - pinned


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_column_type_matches_inspected_varchar_by_default(dialect_name: str) -> None:
    """Leave autogenerate no diff between the column's type and the stored VARCHAR.

    ``_SettingClassString`` is a ``TypeDecorator``; Alembic's default type
    comparison unwraps it to its ``impl`` before comparing, so no project-level
    ``compare_type`` branch is needed to keep autogenerate quiet.
    """
    impl = MigrationContext.configure(dialect_name=dialect_name).impl
    verdict = impl.compare_type(
        Column("setting_class", VARCHAR(SETTING_CLASS_MAX_LENGTH)),
        Column("setting_class", SettingOverride.__table__.c.setting_class.type),
    )
    assert verdict is False


def test_updated_by_defaults_to_none() -> None:
    """Leave ``updated_by`` unset on a row constructed without an actor.

    Rows written before the column existed carry no actor and cannot be
    backfilled, so the column is nullable and every direct construction that
    omits it stays valid.
    """
    row = SettingOverride(
        setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
        key="SYNC_REFRESH_TIME",
        value=5,
    )
    assert row.updated_by is None


@pytest.mark.asyncio
async def test_long_updated_by_round_trips(session: AsyncSession) -> None:
    """Store an unusually long username intact.

    The column is deliberately unbounded because the value is copied from
    ``BaseUser.username``, which carries a minimum length and no maximum. A
    width chosen here would pass on SQLite and fail at commit on PostgreSQL.
    """
    actor = "a" * LONG_USERNAME_LENGTH
    await insert_override_row(
        session,
        setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
        key="SYNC_REFRESH_TIME",
        value=5,
        updated_by=actor,
    )
    session.expunge_all()

    stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")

    assert stored.updated_by == actor


class TestActorStampGuard:
    """Pin the ``before_update`` guard that rejects a tracked-column change left unstamped."""

    @pytest_asyncio.fixture(name="persisted_row")
    async def persisted_row_fixture(self, session: AsyncSession) -> SettingOverride:
        """Return a committed row last written by ``original-actor``.

        :param session: The in-memory database session.
        :return: The persisted ``SettingOverride`` row.
        """
        return await insert_override_row(
            session,
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key="SYNC_REFRESH_TIME",
            value=_GUARD_ORIGINAL_VALUE,
            updated_by="original-actor",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "new_value"),
        [
            ("value", _GUARD_UPDATED_VALUE),
            ("is_active", False),
            ("key", "OTHER_KEY"),
            ("setting_class", SettingClassEnum.TASKS_SETTINGS.name),
        ],
    )
    async def test_direct_mutation_without_actor_is_rejected(
        self,
        session: AsyncSession,
        persisted_row: SettingOverride,
        field: str,
        new_value: object,
    ) -> None:
        """Reject a flush that changes a tracked column without restamping ``updated_by``.

        Mirrors ``_stage_and_commit_overrides``'s own update branch: mutate the
        already-persisted row's attributes directly, ``session.add`` it back, and
        commit, with no manager involved.
        """
        setattr(persisted_row, field, new_value)
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

        await session.rollback()
        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.updated_by == "original-actor"
        assert stored.value == _GUARD_ORIGINAL_VALUE

    @pytest.mark.asyncio
    async def test_direct_mutation_with_new_actor_succeeds(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Accept a tracked-column change whose ``updated_by`` moves to a different actor."""
        persisted_row.value = _GUARD_UPDATED_VALUE
        persisted_row.updated_by = "new-actor"
        session.add(persisted_row)

        await session.commit()

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.value == _GUARD_UPDATED_VALUE
        assert stored.updated_by == "new-actor"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("actor", ["new-actor", "original-actor"])
    async def test_stamped_mutation_succeeds(
        self, session: AsyncSession, persisted_row: SettingOverride, actor: str
    ) -> None:
        """Accept a tracked-column change attributed through ``stamp``.

        The ``original-actor`` case is the same admin saving twice in a row: the
        stamp equals the stored actor, which a plain assignment could not tell
        apart from copying the stale actor back.
        """
        persisted_row.value = _GUARD_UPDATED_VALUE
        persisted_row.stamp(actor)
        session.add(persisted_row)

        await session.commit()

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.value == _GUARD_UPDATED_VALUE
        assert stored.updated_by == actor

    @pytest.mark.asyncio
    async def test_copying_stored_actor_back_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a tracked-column change whose ``updated_by`` is reassigned its stored value.

        The assignment is exactly what a patch built from the loaded row does,
        so it leaves the change attributed to whoever wrote the previous value.
        """
        persisted_row.value = _GUARD_UPDATED_VALUE
        persisted_row.updated_by = persisted_row.updated_by
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_stamping_none_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a stamp that attributes the change to nobody."""
        persisted_row.value = _GUARD_UPDATED_VALUE
        # Bypass the ``str`` annotation to reach the runtime ``None`` check.
        persisted_row.stamp(None)  # ty: ignore[invalid-argument-type]
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_merge_of_detached_patch_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject ``session.merge`` of a detached copy that changed a tracked column only.

        ``merge`` assigns every loaded attribute, ``updated_by`` included, onto the
        persistent row, which must not pass for a restamp.
        """
        detached = SettingOverride(
            **persisted_row.model_dump(exclude={"created_at", "updated_at"})
            | {"is_active": False}
        )

        await session.merge(detached)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_stamp_vouches_for_one_flush_only(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject an unstamped change flushed after an earlier, stamped one."""
        persisted_row.stamp("original-actor")
        await session.commit()

        persisted_row.value = _GUARD_UPDATED_VALUE
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_clearing_actor_alongside_tracked_change_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a tracked-column change whose ``updated_by`` restamp is ``None``.

        Clearing the actor is an assignment, so history alone would accept it,
        yet it leaves the change attributed to nobody.
        """
        persisted_row.value = _GUARD_UPDATED_VALUE
        persisted_row.updated_by = None
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_one_unstamped_row_rejects_the_whole_flush(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Roll back a correctly-stamped row alongside the unstamped one it shares a flush with."""
        sibling = await insert_override_row(
            session,
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key="ARTIFACT_DOWNLOAD_TTL",
            value=_GUARD_ORIGINAL_VALUE,
            updated_by="original-actor",
        )
        sibling.value = _GUARD_UPDATED_VALUE
        sibling.updated_by = "new-actor"
        persisted_row.value = _GUARD_UPDATED_VALUE
        session.add_all([sibling, persisted_row])

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

        await session.rollback()
        session.expunge_all()
        stored = await SettingsOverrideManager.list(session)
        assert {(row.key, row.value, row.updated_by) for row in stored} == {
            ("SYNC_REFRESH_TIME", _GUARD_ORIGINAL_VALUE, "original-actor"),
            ("ARTIFACT_DOWNLOAD_TTL", _GUARD_ORIGINAL_VALUE, "original-actor"),
        }

    @pytest.mark.asyncio
    async def test_stamp_alone_emits_an_update(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Flush a ``stamp`` of the stored actor with nothing else changed as a real UPDATE.

        Without the UPDATE, ``before_update`` would never consume the stamp, and
        it would vouch for the next, unrelated flush of the row.
        """
        persisted_row.stamp("original-actor")

        assert session.is_modified(persisted_row)
        await session.commit()

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.updated_by == "original-actor"
        assert stored.updated_at is not None

    @pytest.mark.asyncio
    async def test_manager_update_without_actor_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a ``SettingsOverrideManager.update`` call that omits ``updated_by``."""
        patch = SettingOverride(
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key=persisted_row.key,
            value=_GUARD_UPDATED_VALUE,
        )

        with pytest.raises(StaleActorUpdateError):
            await SettingsOverrideManager.update(session, persisted_row, patch)

    @pytest.mark.asyncio
    async def test_manager_update_with_patch_from_loaded_row_is_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a ``SettingsOverrideManager.update`` patch built from the loaded row.

        The patch carries the stored ``updated_by`` back, which must not pass
        for a restamp.
        """
        patch = SettingOverride(
            **persisted_row.model_dump(exclude={"id", "created_at", "updated_at"})
            | {"is_active": False}
        )

        with pytest.raises(StaleActorUpdateError):
            await SettingsOverrideManager.update(session, persisted_row, patch)

    @pytest.mark.asyncio
    async def test_manager_update_with_actor_stamp_succeeds(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Accept a ``SettingsOverrideManager.update`` call that also restamps ``updated_by``."""
        patch = SettingOverride(
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key=persisted_row.key,
            value=_GUARD_UPDATED_VALUE,
            updated_by="new-actor",
        )

        updated = await SettingsOverrideManager.update(session, persisted_row, patch)

        assert updated.value == _GUARD_UPDATED_VALUE
        assert updated.updated_by == "new-actor"

    @pytest.mark.asyncio
    async def test_insert_without_actor_is_exempt(self, session: AsyncSession) -> None:
        """Leave a freshly-inserted row's ``updated_by = None`` unrejected.

        ``before_update`` never fires for an insert, so the guard cannot reject
        the model's own documented valid state for a fresh row.
        """
        row = await insert_override_row(
            session,
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key="SYNC_REFRESH_TIME",
            value=_GUARD_ORIGINAL_VALUE,
        )
        assert row.updated_by is None

    @pytest.mark.asyncio
    async def test_actor_reassigned_before_insert_is_persisted(
        self, session: AsyncSession
    ) -> None:
        """Persist an ``updated_by`` reassigned on a not-yet-added instance."""
        row = SettingOverride(
            setting_class=SettingClassEnum.EXTENSIONS_SETTINGS,
            key="SYNC_REFRESH_TIME",
            value=_GUARD_ORIGINAL_VALUE,
            updated_by="first-actor",
        )
        row.updated_by = "second-actor"

        await SettingsOverrideManager.save(session, row)

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.updated_by == "second-actor"

    @pytest.mark.asyncio
    async def test_unrelated_dirty_flush_is_not_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Leave a flush unrejected when the instance is dirty but no tracked column changed.

        ``before_update`` fires for every dirty instance, even one whose
        dirtiness has nothing to do with a tracked column. Forcing
        ``created_at`` to be seen as modified, without actually changing it,
        must not trip the guard.
        """
        flag_modified(persisted_row, "created_at")
        session.add(persisted_row)

        await session.commit()

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.updated_by == "original-actor"

    @pytest.mark.asyncio
    async def test_expired_instance_same_value_reassignment_is_still_rejected(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Reject a same-value reassignment on an expired instance, not silently accept it.

        Expiry discards the baseline SQLAlchemy needs to prove a reassignment
        is a no-op, so the guard sees it as a change. This is an intentional
        fail-closed bias, not a bug: it only ever asks for a stamp it might not
        strictly need, never the reverse.
        """
        session.expire(persisted_row)

        persisted_row.value = _GUARD_ORIGINAL_VALUE
        session.add(persisted_row)

        with pytest.raises(StaleActorUpdateError):
            await session.commit()

    @pytest.mark.asyncio
    async def test_expired_instance_stamped_write_succeeds(
        self, session: AsyncSession, persisted_row: SettingOverride
    ) -> None:
        """Accept a ``stamp`` on an expired instance.

        Expiry unloads ``updated_by``, so the stamp is assigned to an attribute
        absent from the instance state, which ``flag_modified`` would reject.
        """
        session.expire(persisted_row)

        persisted_row.value = _GUARD_UPDATED_VALUE
        persisted_row.stamp("new-actor")
        session.add(persisted_row)

        await session.commit()

        session.expunge_all()
        stored = await SettingsOverrideManager.get(session, key="SYNC_REFRESH_TIME")
        assert stored.value == _GUARD_UPDATED_VALUE
        assert stored.updated_by == "new-actor"
