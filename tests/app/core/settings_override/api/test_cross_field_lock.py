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

"""Serialize concurrent settings writes that a cross-field rule judges together.

Two changes to related fields, a rule's or one nested parent's, can each pass the
check against the other's old value. The concurrency cases park the first write between its check and its
commit, assert the second is queued on the lock rather than at its own check,
and then that it is refused against the row the first one committed. Without the
lock the second never queues, so each of them fails.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, cast, ClassVar
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, status
from pydantic_settings import SettingsConfigDict
from pytest_mock import MockerFixture
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import BaseYamlSettings
from app.core.db.utils import advisory_lock_key, pg_advisory_xact_lock
from app.core.exceptions import (
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.settings_override.api import routes
from app.core.settings_override.api.models import SettingsPatch
from app.core.settings_override.manager import SettingsOverrideManager
from app.core.settings_override.models import setting_class_token
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field
from app.core.utils.cross_field import CrossFieldRule
from app.extensions.apps.om_inventory.config import OmInventorySettings
from app.extensions.config import ExtensionsSettings
from tests.app.advisory_locks import (
    advisory_lock_holder_pids,
    SHORT_LOCK_WAIT,
    wait_for_advisory_waiters,
)
from tests.app.core.settings_override.conftest import (
    insert_override_row,
    TimingSettings,
    WINDOW_HIGH,
    WINDOW_LOW,
    WindowSettings,
)

#: ``TimingSettings``' ``POLL <= TIMEOUT`` relationship.
POLL_RULE = TimingSettings.CROSS_FIELD_RULES[0]


class BudgetSettings(BaseYamlSettings):
    """Carry one three-field rule, so two DELETEs can break it only together.

    A two-field rule cannot: reverting both of its fields restores the YAML/env
    pair, which passed its own load-time check.
    """

    model_config = SettingsConfigDict(hide_input_in_errors=True)
    CROSS_FIELD_RULES: ClassVar[tuple[CrossFieldRule, ...]] = (
        CrossFieldRule(
            ("LEFT", "RIGHT", "TOTAL"),
            lambda left, right, total: left + right >= total,
            "LEFT plus RIGHT must reach TOTAL",
        ),
    )

    LEFT: int = hot_field(1)
    RIGHT: int = hot_field(1)
    TOTAL: int = hot_field(0)


#: ``BudgetSettings``' one relationship.
BUDGET_RULE = BudgetSettings.CROSS_FIELD_RULES[0]

#: A write under test, run on the session it is given.
Write = Callable[[AsyncSession], Coroutine[Any, Any, Any]]


def _request() -> Request:
    """Return a request whose app carries no rebind callbacks.

    :return: The request.
    """
    return Request({"type": "http", "app": FastAPI()})


def _patch(
    settings_cls: type[BaseYamlSettings],
    proxy: OverridableSettingsProxy,
    body: dict[str, Any],
) -> Write:
    """Return a PATCH of ``body`` through :func:`routes.apply_class_overrides`.

    :param settings_cls: The class written.
    :param proxy: Its proxy.
    :param body: The overrides to apply.
    :return: The write.
    """

    async def write(session: AsyncSession) -> Any:
        return await routes.apply_class_overrides(
            request=_request(),
            session=session,
            setting_class=settings_cls.__name__,
            settings_cls=settings_cls,
            proxy=proxy,
            body=SettingsPatch(body),
            actor="admin",
        )

    return write


def _delete(
    settings_cls: type[BaseYamlSettings], proxy: OverridableSettingsProxy, key: str
) -> Write:
    """Return a DELETE of ``key`` through :func:`routes._clear_class_override`.

    :param settings_cls: The class written.
    :param proxy: Its proxy.
    :param key: The field to revert.
    :return: The write.
    """

    async def write(session: AsyncSession) -> None:
        await routes._clear_class_override(
            request=_request(),
            session=session,
            setting_class=settings_cls.__name__,
            settings_cls=settings_cls,
            proxy=proxy,
            key=key,
        )

    return write


def _cross_field_entries(rule: CrossFieldRule) -> list[dict[str, Any]]:
    """Return the 422 entries the refusal of ``rule`` must carry.

    :param rule: The broken rule.
    :return: One entry per field, in the cross-field 422 shape.
    """
    return [
        {
            "loc": ["body", name],
            "msg": rule.message,
            "type": "cross_field",
            "ctx": {"fields": list(rule.fields)},
        }
        for name in rule.fields
    ]


@pytest.fixture
def lock_namespace(mocker: MockerFixture) -> int:
    """Move the lock onto a namespace no other test can be holding.

    Advisory locks are database-wide while the fixtures isolate parallel workers
    by schema only.

    :param mocker: The pytest-mock fixture.
    :return: The namespace the lock now uses.
    """
    namespace = advisory_lock_key(uuid4().hex)
    mocker.patch.object(routes, "CROSS_FIELD_LOCK_NAMESPACE", namespace)
    return namespace


async def _stored(
    maker: async_sessionmaker[AsyncSession], settings_cls: type[BaseYamlSettings]
) -> dict[str, Any]:
    """Return the class's active overrides as committed, on a fresh session.

    :param maker: The session maker.
    :param settings_cls: The class read.
    :return: ``{key: value}``.
    """
    async with maker() as session:
        rows = await SettingsOverrideManager.list(
            session, setting_class=setting_class_token(settings_cls), is_active=True
        )
    return {row.key: row.value for row in rows}


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    settings_cls: type[BaseYamlSettings],
    values: dict[str, Any],
) -> None:
    """Store ``values`` as the class's overrides, bypassing the API.

    :param maker: The session maker.
    :param settings_cls: The class written.
    :param values: ``{key: value}``.
    """
    async with maker() as session:
        for key, value in values.items():
            await insert_override_row(
                session,
                setting_class=setting_class_token(settings_cls),
                key=key,
                value=value,
            )


@pytest.mark.postgres
class TestConcurrentWritesAreSerialized:
    """Refuse the second of two changes that break a rule only together."""

    @pytest_asyncio.fixture
    async def maker(
        self, postgres_session_maker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        """Yield the real-PostgreSQL session maker.

        :param postgres_session_maker: The table-bootstrapped maker.
        :return: The same maker.
        """
        yield postgres_session_maker

    async def _interleave(
        self,
        mocker: MockerFixture,
        engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        namespace: int,
        settings_cls: type[BaseYamlSettings],
        first: Write,
        second: Write,
        check: str = "_cross_field_errors",
    ) -> object:
        """Run ``first`` to its check, start ``second``, then let ``first`` commit.

        :param mocker: The pytest-mock fixture.
        :param engine: The engine the lock lives on.
        :param maker: The session maker.
        :param namespace: The lock's namespace.
        :param settings_cls: The class both writes target.
        :param first: The write parked between its check and its commit.
        :param second: The write expected to queue behind ``first``.
        :param check: The name of the check in ``routes`` both writes reach.
        :return: What ``second`` returned or raised.
        """
        checked: list[AsyncSession] = []
        first_checked = asyncio.Event()
        release_first = asyncio.Event()
        real_check = getattr(routes, check)

        async def parked_check(
            session: AsyncSession, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            checked.append(session)
            errors = await real_check(session, *args, **kwargs)
            if len(checked) == 1:
                first_checked.set()
                await release_first.wait()
            return errors

        mocker.patch.object(routes, check, parked_check)
        key = advisory_lock_key(setting_class_token(settings_cls))
        async with maker() as first_session, maker() as second_session:
            tasks = [asyncio.create_task(first(first_session))]
            try:
                await asyncio.wait_for(first_checked.wait(), timeout=5)
                tasks.append(asyncio.create_task(second(second_session)))
                await wait_for_advisory_waiters(engine, namespace, key)
                assert checked == [first_session]
            finally:
                # Drain both writes even when an assertion fails, so neither is
                # left mid-transaction on a session about to be closed.
                release_first.set()
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        first_outcome, second_outcome = outcomes
        assert not isinstance(first_outcome, BaseException)
        assert checked == [first_session, second_session]
        return second_outcome

    async def _race(
        self,
        mocker: MockerFixture,
        engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        namespace: int,
        settings_cls: type[BaseYamlSettings],
        first: Write,
        second: Write,
        check: str = "_cross_field_errors",
    ) -> HTTPUnprocessableEntityException:
        """Interleave the two writes and return the refusal ``second`` raised.

        :param mocker: The pytest-mock fixture.
        :param engine: The engine the lock lives on.
        :param maker: The session maker.
        :param namespace: The lock's namespace.
        :param settings_cls: The class both writes target.
        :param first: The write parked between its check and its commit.
        :param second: The write expected to queue and then be refused.
        :param check: The name of the check in ``routes`` both writes reach.
        :return: The refusal ``second`` raised.
        """
        outcome = await self._interleave(
            mocker, engine, maker, namespace, settings_cls, first, second, check
        )
        assert isinstance(outcome, HTTPUnprocessableEntityException)
        return outcome

    @pytest.mark.asyncio
    async def test_a_delete_removes_the_row_a_concurrent_patch_inserted(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Delete the TIMEOUT row a PATCH committed while the DELETE waited on it.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        proxy = OverridableSettingsProxy(TimingSettings)

        outcome = await self._interleave(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            TimingSettings,
            _patch(TimingSettings, proxy, {"TIMEOUT": 10}),
            _delete(TimingSettings, proxy, "TIMEOUT"),
        )

        assert outcome is None
        assert await _stored(maker, TimingSettings) == {}

    @pytest.mark.asyncio
    async def test_two_patches_breaking_a_rule_together(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Refuse the second PATCH against the TIMEOUT the first one committed.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        proxy = OverridableSettingsProxy(TimingSettings)

        refused = await self._race(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            TimingSettings,
            _patch(TimingSettings, proxy, {"TIMEOUT": 10}),
            _patch(TimingSettings, proxy, {"POLL": 60}),
        )

        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert refused.detail == _cross_field_entries(POLL_RULE)
        assert await _stored(maker, TimingSettings) == {"TIMEOUT": 10}

    @pytest.mark.asyncio
    async def test_two_leaf_patches_breaking_their_parent_together(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Refuse the second leaf PATCH against the leaf the first one committed.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        await _seed(maker, WindowSettings, {WINDOW_HIGH: 9})
        proxy = OverridableSettingsProxy(WindowSettings)

        refused = await self._race(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            WindowSettings,
            _patch(WindowSettings, proxy, {WINDOW_LOW: 5}),
            _patch(WindowSettings, proxy, {WINDOW_HIGH: 3}),
            "_nested_parent_errors",
        )

        assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        entries = cast("list[dict[str, Any]]", refused.detail)
        assert [entry["loc"] for entry in entries] == [["body", WINDOW_HIGH]]
        assert await _stored(maker, WindowSettings) == {WINDOW_LOW: 5, WINDOW_HIGH: 9}

    @pytest.mark.asyncio
    async def test_a_delete_after_a_patch_breaking_a_rule_together(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Refuse a DELETE that would strand the POLL a concurrent PATCH committed.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        await _seed(maker, TimingSettings, {"TIMEOUT": 500})
        proxy = OverridableSettingsProxy(TimingSettings)

        refused = await self._race(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            TimingSettings,
            _patch(TimingSettings, proxy, {"POLL": 300}),
            _delete(TimingSettings, proxy, "TIMEOUT"),
        )

        assert refused.detail == _cross_field_entries(POLL_RULE)
        assert await _stored(maker, TimingSettings) == {"TIMEOUT": 500, "POLL": 300}

    @pytest.mark.asyncio
    async def test_a_patch_after_a_delete_breaking_a_rule_together(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Refuse a PATCH judged against the TIMEOUT a concurrent DELETE reverted.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        await _seed(maker, TimingSettings, {"TIMEOUT": 500})
        proxy = OverridableSettingsProxy(TimingSettings)

        refused = await self._race(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            TimingSettings,
            _delete(TimingSettings, proxy, "TIMEOUT"),
            _patch(TimingSettings, proxy, {"POLL": 300}),
        )

        assert refused.detail == _cross_field_entries(POLL_RULE)
        assert await _stored(maker, TimingSettings) == {}

    @pytest.mark.asyncio
    async def test_two_deletes_breaking_a_rule_together(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Refuse the second DELETE once the first left too little to reach TOTAL.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        await _seed(maker, BudgetSettings, {"LEFT": 5, "RIGHT": 5, "TOTAL": 5})
        proxy = OverridableSettingsProxy(BudgetSettings)

        refused = await self._race(
            mocker,
            postgres_engine,
            maker,
            lock_namespace,
            BudgetSettings,
            _delete(BudgetSettings, proxy, "LEFT"),
            _delete(BudgetSettings, proxy, "RIGHT"),
        )

        assert refused.detail == _cross_field_entries(BUDGET_RULE)
        assert await _stored(maker, BudgetSettings) == {"RIGHT": 5, "TOTAL": 5}

    @pytest.mark.asyncio
    async def test_the_integrity_replay_commits_under_the_lock(
        self,
        mocker: MockerFixture,
        postgres_engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
    ) -> None:
        """Hold the lock across the rollback and the replay's commit.

        :param mocker: The pytest-mock fixture.
        :param postgres_engine: The engine the lock lives on.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        """
        proxy = OverridableSettingsProxy(TimingSettings)
        key = advisory_lock_key(setting_class_token(TimingSettings))
        real_stage = routes._stage_and_commit_overrides
        attempts: list[int] = []
        holders_at_replay: list[list[int]] = []

        async def conflict_once(**kwargs: Any) -> Any:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
            async with postgres_engine.connect() as connection:
                holders_at_replay.append(
                    await advisory_lock_holder_pids(connection, lock_namespace, key)
                )
            return await real_stage(**kwargs)

        mocker.patch.object(routes, "_stage_and_commit_overrides", conflict_once)
        async with maker() as session:
            await _patch(TimingSettings, proxy, {"TIMEOUT": 10})(session)

        assert len(holders_at_replay) == 1
        assert len(holders_at_replay[0]) == 1
        assert await _stored(maker, TimingSettings) == {"TIMEOUT": 10}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["patch", "delete"])
    async def test_a_lock_not_granted_in_time_answers_503(
        self,
        mocker: MockerFixture,
        maker: async_sessionmaker[AsyncSession],
        lock_namespace: int,
        operation: str,
    ) -> None:
        """Refuse with 503, writing nothing, while another write holds the lock.

        :param mocker: The pytest-mock fixture.
        :param maker: The session maker.
        :param lock_namespace: The isolated lock namespace.
        :param operation: Which write contends.
        """
        await _seed(maker, TimingSettings, {"TIMEOUT": 500})
        proxy = OverridableSettingsProxy(TimingSettings)
        mocker.patch.object(routes, "CROSS_FIELD_LOCK_TIMEOUT", SHORT_LOCK_WAIT)
        write = (
            _patch(TimingSettings, proxy, {"TIMEOUT": 10})
            if operation == "patch"
            else _delete(TimingSettings, proxy, "TIMEOUT")
        )
        key = advisory_lock_key(setting_class_token(TimingSettings))

        async with (
            maker() as holder,
            maker() as session,
            pg_advisory_xact_lock(
                holder, lock_namespace, key, max_wait=SHORT_LOCK_WAIT
            ),
        ):
            with pytest.raises(HTTPServiceUnavailableException) as refused:
                await write(session)

        assert refused.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert await _stored(maker, TimingSettings) == {"TIMEOUT": 500}


class TestUnrelatedWritesTakeNoLock:
    """Leave a change naming no rule field or nested parent on its unlocked path."""

    @pytest.fixture
    def lock(self, mocker: MockerFixture) -> MagicMock:
        """Replace the lock so a test can assert it was never taken.

        :param mocker: The pytest-mock fixture.
        :return: The replacement.
        """
        return mocker.patch.object(routes, "pg_advisory_xact_lock")

    @pytest.mark.asyncio
    async def test_a_patch_of_a_bystander_takes_no_lock(
        self, session: AsyncSession, lock: MagicMock
    ) -> None:
        """Write a field no rule names without the lock.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        """
        proxy = OverridableSettingsProxy(TimingSettings)

        await _patch(TimingSettings, proxy, {"BYSTANDER": 7})(session)
        await _delete(TimingSettings, proxy, "BYSTANDER")(session)

        lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_patch_on_a_class_without_rules_takes_no_lock(
        self, session: AsyncSession, lock: MagicMock
    ) -> None:
        """Write a class that declares no rules without the lock.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        """
        proxy = OverridableSettingsProxy(ExtensionsSettings)

        await _patch(ExtensionsSettings, proxy, {"CONNECTIVITY_CHECK_DEFAULT": True})(
            session
        )

        lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_enabled_only_patch_on_om_inventory_takes_no_lock(
        self, session: AsyncSession, lock: MagicMock
    ) -> None:
        """Follow PMM's switch, which sends only ``ENABLED``, without the lock.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        """
        proxy = OverridableSettingsProxy(OmInventorySettings)

        await _patch(OmInventorySettings, proxy, {"ENABLED": False})(session)

        lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_patch_of_a_rule_field_takes_the_lock(
        self, session: AsyncSession, lock: MagicMock
    ) -> None:
        """Take the lock for a field a rule names, so the gate is not vacuous.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        """
        proxy = OverridableSettingsProxy(TimingSettings)

        await _patch(TimingSettings, proxy, {"TIMEOUT": 200})(session)

        lock.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{WINDOW_LOW: 1}, {WINDOW_LOW.lower(): 1}, {"WINDOW": {"LOW": 1, "HIGH": 2}}],
        ids=["leaf", "leaf-lowercase", "parent"],
    )
    async def test_a_patch_of_a_nested_parent_takes_the_lock(
        self, session: AsyncSession, lock: MagicMock, body: dict[str, Any]
    ) -> None:
        """Take the lock for a parent judged by its merged leaves, in any spelling.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        :param body: The PATCH body.
        """
        proxy = OverridableSettingsProxy(WindowSettings)

        await _patch(WindowSettings, proxy, body)(session)

        lock.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_delete_of_a_nested_leaf_takes_the_lock(
        self, session: AsyncSession, lock: MagicMock
    ) -> None:
        """Take the lock to revert a leaf the remaining ones are judged with.

        :param session: The SQLite session.
        :param lock: The replaced lock.
        """
        proxy = OverridableSettingsProxy(WindowSettings)

        await _delete(WindowSettings, proxy, WINDOW_HIGH)(session)

        lock.assert_called_once()
