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

"""Tests for the PMM Extensions override rebind callbacks wired in ``app.extensions.main``."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlalchemy_celery_beat.models import Period

import app.extensions.main as extensions_main
from app.core.celery.models import IntervalSchedule
from app.core.config import PMMSettings, Settings, settings
from app.core.requests import RemoteAPI
from app.core.requests.remote_api import PendingCloses
from app.core.settings_override.lifecycle import is_fire_on_boot, SnapshotChange
from app.core.settings_override.models import SettingClassEnum
from app.extensions.config import extensions_settings
from app.extensions.main import (
    _close_app_state_remote_apis,
    _make_remote_api_rebinder,
    _reseed_system_periodic_tasks,
)
from app.extensions.settings_override import (
    apply_logging_dictconfig,
    invalidate_pmm_clients,
)
from app.extensions.snippets.config import snippets_settings


def _awaited_endpoints(invalidate: AsyncMock) -> list[str]:
    """Return the endpoint arguments passed to ``invalidate_client``, in order."""
    return [call.args[0] for call in invalidate.await_args_list]


@pytest.mark.asyncio
async def test_endpoint_rebinder_swaps_app_state_client(
    mocker: MockerFixture,
) -> None:
    """Assert the rebinder opens a client on the new endpoint and drains the old one.

    The standalone ``extensions_lifespan`` path owns ``app.state`` clients: it closes the
    old one explicitly and must not fall through to registry eviction.
    """
    app = FastAPI()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    new = None
    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        await rebind(SnapshotChange({}, {}))

        new = app.state.inventory_api
        assert new is not old
        assert str(new.endpoint).startswith("https://new-inv.example.org")
        assert old._session is None  # the previous client was drained
        invalidate.assert_not_awaited()
    finally:
        if new is not None:
            await new.close()
        extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_endpoint_rebinder_invalidates_when_no_app_state_client(
    mocker: MockerFixture,
) -> None:
    """Assert the rebinder evicts the registry client when no ``app.state`` client exists."""
    app = FastAPI()
    endpoint = "https://new-inv.example.org"
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": endpoint}
    )
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        # Same previous/current endpoint collapses to one eviction (credential-style).
        await rebind(
            SnapshotChange(
                {"INVENTORY_ENDPOINT": endpoint},
                {"INVENTORY_ENDPOINT": endpoint},
            )
        )
        invalidate.assert_awaited_once_with(endpoint)
    finally:
        extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_endpoint_rebinder_created_evicts_base_and_new(
    mocker: MockerFixture,
) -> None:
    """Evict the YAML/env base and the new endpoint when an endpoint override is created."""
    app = FastAPI()
    new_endpoint = "https://new-inv.example.org"
    base_endpoint = "https://base-inv.example.org"
    mocker.patch.object(
        extensions_settings._resolve(),  # ty: ignore[unresolved-attribute]
        "INVENTORY_ENDPOINT",
        base_endpoint,
    )
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": new_endpoint}
    )
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        await rebind(SnapshotChange({}, {"INVENTORY_ENDPOINT": new_endpoint}))
        assert _awaited_endpoints(invalidate) == [base_endpoint, new_endpoint]
    finally:
        extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_endpoint_rebinder_changed_evicts_previous_and_new(
    mocker: MockerFixture,
) -> None:
    """Evict both the previous and the new endpoint when an endpoint override changes."""
    app = FastAPI()
    previous_endpoint = "https://old-inv.example.org"
    new_endpoint = "https://new-inv.example.org"
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": new_endpoint}
    )
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        await rebind(
            SnapshotChange(
                {"INVENTORY_ENDPOINT": previous_endpoint},
                {"INVENTORY_ENDPOINT": new_endpoint},
            )
        )
        assert _awaited_endpoints(invalidate) == [previous_endpoint, new_endpoint]
    finally:
        extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_endpoint_rebinder_deleted_evicts_previous_and_base(
    mocker: MockerFixture,
) -> None:
    """Evict the previous override and the YAML/env base when an endpoint override is deleted."""
    app = FastAPI()
    previous_endpoint = "https://old-inv.example.org"
    base_endpoint = "https://base-inv.example.org"
    mocker.patch.object(
        extensions_settings._resolve(),  # ty: ignore[unresolved-attribute]
        "INVENTORY_ENDPOINT",
        base_endpoint,
    )
    extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    await rebind(SnapshotChange({"INVENTORY_ENDPOINT": previous_endpoint}, {}))
    assert _awaited_endpoints(invalidate) == [previous_endpoint, base_endpoint]


@pytest.mark.asyncio
async def test_endpoint_rebinder_defers_app_state_close_while_a_consumer_holds(
    mocker: MockerFixture,
) -> None:
    """Keep a held ``app.state`` client alive through the rebind, closing on release."""
    app = FastAPI()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    new = None
    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        async with old.hold():
            await rebind(SnapshotChange({}, {}))

            new = app.state.inventory_api
            assert new is not old
            assert old._session is not None

        assert old._session is None
    finally:
        if new is not None:
            await new.close()
        extensions_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_endpoint_rebinder_shutdown_force_closes_deferred_app_state_client(
    mocker: MockerFixture,
) -> None:
    """Force-close a mid-hold app.state retiree when the owner's pending sweeps."""
    app = FastAPI()
    pending = PendingCloses()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    new = None
    rebind = _make_remote_api_rebinder(
        app,
        "inventory_api",
        extensions_settings,
        "INVENTORY_ENDPOINT",
        pending=pending,
    )
    try:
        async with old.hold():
            await rebind(SnapshotChange({}, {}))

            new = app.state.inventory_api
            assert new is not old
            assert old._session is not None

            await pending.force_close()

            assert old._session is None
    finally:
        if new is not None:
            await new.close()
        extensions_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_endpoint_rebinder_discards_replacement_when_pending_sealed(
    mocker: MockerFixture,
) -> None:
    """Do not publish a new app.state client after teardown has sealed pending."""
    app = FastAPI()
    pending = PendingCloses()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())
    pending.seal()

    opened: list[RemoteAPI] = []
    original_open = RemoteAPI.open

    async def tracking_open(self: RemoteAPI) -> RemoteAPI:
        api = await original_open(self)
        opened.append(api)
        return api

    mocker.patch.object(RemoteAPI, "open", tracking_open)

    rebind = _make_remote_api_rebinder(
        app,
        "inventory_api",
        extensions_settings,
        "INVENTORY_ENDPOINT",
        pending=pending,
    )
    try:
        await rebind(SnapshotChange({}, {}))

        assert app.state.inventory_api is old
        assert old._session is not None
        assert len(opened) == 1
        assert opened[0] is not old
        assert opened[0]._session is None
        assert id(opened[0]) not in pending._clients
    finally:
        await old.close()
        extensions_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_endpoint_rebinder_keeps_failed_discard_on_pending(
    mocker: MockerFixture,
) -> None:
    """Track a sealed-path replacement so a failed discard close stays retryable."""
    app = FastAPI()
    pending = PendingCloses()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())
    pending.seal()

    opened: list[RemoteAPI] = []
    original_open = RemoteAPI.open
    original_close = RemoteAPI.close

    async def tracking_open(self: RemoteAPI) -> RemoteAPI:
        api = await original_open(self)
        opened.append(api)
        return api

    fail_once = True

    async def flaky_close(self: RemoteAPI) -> None:
        nonlocal fail_once
        if self in opened and fail_once:
            fail_once = False
            raise RuntimeError("discard boom")
        await original_close(self)

    mocker.patch.object(RemoteAPI, "open", tracking_open)
    mocker.patch.object(RemoteAPI, "close", flaky_close)

    rebind = _make_remote_api_rebinder(
        app,
        "inventory_api",
        extensions_settings,
        "INVENTORY_ENDPOINT",
        pending=pending,
    )
    try:
        with pytest.raises(RuntimeError, match="discard boom"):
            await rebind(SnapshotChange({}, {}))

        assert app.state.inventory_api is old
        assert len(opened) == 1
        discarded = opened[0]
        assert discarded._session is not None
        assert id(discarded) in pending._clients

        await pending.force_close()
        assert discarded._session is None
        assert pending._clients == {}
    finally:
        mocker.stopall()
        await old.close()
        if opened and opened[0]._session is not None:
            await original_close(opened[0])
        extensions_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_endpoint_rebinder_registers_idle_retiree_before_publish(
    mocker: MockerFixture,
) -> None:
    """Register an idle rebind on pending before publish so cancel mid-close cannot leak."""
    app = FastAPI()
    pending = PendingCloses()
    old = await RemoteAPI(endpoint="https://old-inv.example.org").open()
    app.state.inventory_api = old
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": "https://new-inv.example.org"}
    )
    mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    entered = asyncio.Event()
    resume = asyncio.Event()
    original = RemoteAPI.close_when_idle

    async def paused_close_when_idle(self: RemoteAPI, pending=None) -> None:
        entered.set()
        await resume.wait()
        await original(self, pending=pending)

    mocker.patch.object(RemoteAPI, "close_when_idle", paused_close_when_idle)

    rebind = _make_remote_api_rebinder(
        app,
        "inventory_api",
        extensions_settings,
        "INVENTORY_ENDPOINT",
        pending=pending,
    )
    new = None
    try:
        rebind_task = asyncio.create_task(rebind(SnapshotChange({}, {})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        new = app.state.inventory_api
        assert new is not old
        assert id(old) in pending._clients
        assert old._session is not None

        rebind_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rebind_task

        await pending.force_close()
        assert old._session is None
        assert pending._clients == {}
    finally:
        resume.set()
        if new is not None and new._session is not None:
            await new.close()
        if old._session is not None:
            await old.close()
        extensions_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_close_app_state_remote_apis_continues_after_tasks_close_fails() -> None:
    """Continue inventory close and pending sweep when ``tasks_api.__aexit__`` raises."""
    app = FastAPI()
    pending = PendingCloses()
    app.state.retired_remote_apis = pending
    app.state.tasks_api = AsyncMock()
    app.state.tasks_api.__aexit__ = AsyncMock(side_effect=RuntimeError("tasks boom"))
    app.state.inventory_api = AsyncMock()
    app.state.inventory_api.__aexit__ = AsyncMock()
    pending.force_close = AsyncMock()

    with pytest.raises(RuntimeError, match="tasks boom"):
        await _close_app_state_remote_apis(app)

    assert pending.sealed
    app.state.inventory_api.__aexit__.assert_awaited_once()
    pending.force_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_endpoint_rebinder_defers_registry_close_while_a_consumer_holds() -> None:
    """Keep a held registry client alive through the eviction, closing on release."""
    app = FastAPI()
    endpoint = "https://held-inv.example.org"
    extensions_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"INVENTORY_ENDPOINT": endpoint}
    )
    rebind = _make_remote_api_rebinder(
        app, "inventory_api", extensions_settings, "INVENTORY_ENDPOINT"
    )
    try:
        client = await settings.get_remote_api(endpoint=endpoint)

        async with client.hold():
            await rebind(
                SnapshotChange(
                    {"INVENTORY_ENDPOINT": endpoint},
                    {"INVENTORY_ENDPOINT": endpoint},
                )
            )

            assert client._session is not None
            assert await settings.get_remote_api(endpoint=endpoint) is not client

        assert client._session is None
    finally:
        await settings.invalidate_client(endpoint)
        extensions_settings._set_snapshot({})


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_defers_close_while_a_consumer_holds() -> None:
    """Keep a held PMM client alive through the eviction, closing on release."""
    endpoint = "https://held-pmm.example.org"
    pmm = PMMSettings(endpoint=endpoint)
    settings._set_snapshot({"PMM": pmm})  # ty: ignore[unresolved-attribute]
    try:
        client = await settings.get_remote_api(endpoint=endpoint)

        async with client.hold():
            await invalidate_pmm_clients(SnapshotChange({"PMM": pmm}, {"PMM": pmm}))

            assert client._session is not None

        assert client._session is None
    finally:
        await settings.invalidate_client(endpoint)
        settings._set_snapshot({})


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_evicts_current_pmm_endpoint(
    mocker: MockerFixture,
) -> None:
    """Assert the PMM callback evicts cached clients on the overridden PMM endpoint."""
    pmm = PMMSettings(endpoint="https://new-pmm.example.org")
    settings._set_snapshot({"PMM": pmm})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    try:
        # Same previous/current endpoint collapses to one eviction (credential-style).
        await invalidate_pmm_clients(SnapshotChange({"PMM": pmm}, {"PMM": pmm}))
        invalidate.assert_awaited_once_with("https://new-pmm.example.org")
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_noop_without_endpoint(
    mocker: MockerFixture,
) -> None:
    """Assert the PMM callback is a no-op when no PMM endpoint is configured."""
    pmm = PMMSettings(endpoint=None)
    settings._set_snapshot({"PMM": pmm})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    try:
        await invalidate_pmm_clients(SnapshotChange({"PMM": pmm}, {"PMM": pmm}))
        invalidate.assert_not_awaited()
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_created_evicts_base_and_new(
    mocker: MockerFixture,
) -> None:
    """Evict the YAML/env base endpoint and the new one when a PMM override is created."""
    new_pmm = PMMSettings(endpoint="https://new-pmm.example.org")
    mocker.patch.object(
        settings._resolve(),  # ty: ignore[unresolved-attribute]
        "PMM",
        PMMSettings(endpoint="https://base-pmm.example.org"),
    )
    settings._set_snapshot({"PMM": new_pmm})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    try:
        await invalidate_pmm_clients(SnapshotChange({}, {"PMM": new_pmm}))
        assert _awaited_endpoints(invalidate) == [
            "https://base-pmm.example.org",
            "https://new-pmm.example.org",
        ]
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_changed_evicts_previous_and_new(
    mocker: MockerFixture,
) -> None:
    """Evict both the previous and the new endpoint when a PMM endpoint override changes."""
    previous_pmm = PMMSettings(endpoint="https://old-pmm.example.org")
    new_pmm = PMMSettings(endpoint="https://new-pmm.example.org")
    settings._set_snapshot({"PMM": new_pmm})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    try:
        await invalidate_pmm_clients(
            SnapshotChange({"PMM": previous_pmm}, {"PMM": new_pmm})
        )
        assert _awaited_endpoints(invalidate) == [
            "https://old-pmm.example.org",
            "https://new-pmm.example.org",
        ]
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_deleted_evicts_previous_and_base(
    mocker: MockerFixture,
) -> None:
    """Evict the previous override and the YAML/env base when a PMM override is deleted."""
    previous_pmm = PMMSettings(endpoint="https://old-pmm.example.org")
    mocker.patch.object(
        settings._resolve(),  # ty: ignore[unresolved-attribute]
        "PMM",
        PMMSettings(endpoint="https://base-pmm.example.org"),
    )
    settings._set_snapshot({})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    await invalidate_pmm_clients(SnapshotChange({"PMM": previous_pmm}, {}))
    assert _awaited_endpoints(invalidate) == [
        "https://old-pmm.example.org",
        "https://base-pmm.example.org",
    ]


@pytest.mark.asyncio
async def test_invalidate_pmm_clients_same_endpoint_credential_change_evicts_once(
    mocker: MockerFixture,
) -> None:
    """Collapse a same-endpoint credential change to a single eviction."""
    previous_pmm = PMMSettings(
        endpoint="https://same-pmm.example.org", api_key=SecretStr("old-key")
    )
    new_pmm = PMMSettings(
        endpoint="https://same-pmm.example.org", api_key=SecretStr("new-key")
    )
    settings._set_snapshot({"PMM": new_pmm})  # ty: ignore[unresolved-attribute]
    invalidate = mocker.patch.object(Settings, "invalidate_client", new=AsyncMock())

    try:
        await invalidate_pmm_clients(
            SnapshotChange({"PMM": previous_pmm}, {"PMM": new_pmm})
        )
        invalidate.assert_awaited_once_with("https://same-pmm.example.org")
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_reseed_callback_reseeds_beat_with_live_interval(
    mocker: MockerFixture,
) -> None:
    """Assert the snippets callback re-seeds the beat schedule from the live interval.

    It rebuilds the task set (reading the overridden ``SYNC_INTERVAL`` from the
    proxy snapshot) and re-invokes ``init_periodic_tasks_db`` under the ``extensions__``
    prefix, so the ``extensions__sync_snippets`` beat row reflects the new cadence on
    beat's next tick.
    """
    reseed = mocker.patch(
        "app.extensions.main.init_periodic_tasks_db", new_callable=AsyncMock
    )
    mocker.patch(
        "app.extensions.main.sync_app_periodic_task_gating", new_callable=AsyncMock
    )
    snippets_settings._set_snapshot(  # ty: ignore[unresolved-attribute]
        {"SYNC_INTERVAL": IntervalSchedule(every=15, period=Period.MINUTES)}
    )
    try:
        await _reseed_system_periodic_tasks(SnapshotChange({}, {}))
    finally:
        snippets_settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    reseed.assert_awaited_once()
    tasks, prefix = reseed.await_args.args
    assert prefix == "extensions__"
    snippets = next(
        schedule
        for schedule in tasks
        for task in schedule.tasks
        if task.name == "extensions__sync_snippets"
    )
    assert snippets.schedule == IntervalSchedule(every=15, period=Period.MINUTES)


@pytest.mark.asyncio
async def test_reseed_callback_registered_for_sync_interval() -> None:
    """Assert ``extensions_overrides_lifespan`` registers the snippets-interval re-seed callback."""
    original = getattr(extensions_main.extensions_app.state, "override_callbacks", None)
    try:
        async with extensions_main.extensions_overrides_lifespan(FastAPI()):
            callbacks = extensions_main.extensions_app.state.override_callbacks
        assert (
            SettingClassEnum.SNIPPETS_SETTINGS,
            "SYNC_INTERVAL",
        ) in callbacks
        assert (
            callbacks[(SettingClassEnum.SNIPPETS_SETTINGS, "SYNC_INTERVAL")]
            is extensions_main._reseed_system_periodic_tasks
        )
    finally:
        extensions_main.extensions_app.state.override_callbacks = original


@pytest.mark.asyncio
async def test_apply_logging_dictconfig_reapplies_new_level(
    mocker: MockerFixture,
) -> None:
    """Assert the LOGGING rebind re-applies ``dictConfig`` with the live level.

    ``LOGGING`` is HOT but ``LOGGING_CONFIG`` is not, so the callback must inject
    the overridden level into the config before re-applying it — otherwise the
    stale level baked in at construction time would be re-applied.
    """
    dict_config = mocker.patch(
        "app.extensions.settings_override.logging.config.dictConfig"
    )
    settings._set_snapshot({"LOGGING": "DEBUG"})  # ty: ignore[unresolved-attribute]
    try:
        await apply_logging_dictconfig(SnapshotChange({}, {"LOGGING": "DEBUG"}))
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]

    dict_config.assert_called_once()
    applied = dict_config.call_args.args[0]
    assert applied["loggers"][""]["level"] == "DEBUG"
    assert applied["loggers"]["app"]["level"] == "DEBUG"


@pytest.mark.asyncio
async def test_apply_logging_dictconfig_swallows_failure(
    mocker: MockerFixture,
) -> None:
    """Assert a malformed logging config is logged and swallowed, never crashing the app."""
    mocker.patch(
        "app.extensions.settings_override.logging.config.dictConfig",
        side_effect=ValueError("bad config"),
    )
    settings._set_snapshot({"LOGGING": "DEBUG"})  # ty: ignore[unresolved-attribute]
    try:
        # Must not raise.
        await apply_logging_dictconfig(SnapshotChange({}, {"LOGGING": "DEBUG"}))
    finally:
        settings._set_snapshot({})  # ty: ignore[unresolved-attribute]


@pytest.mark.asyncio
async def test_logging_and_app_drain_callbacks_registered() -> None:
    """Assert the LOGGING rebind and the APP_DRAIN reseed callback are registered."""
    original = getattr(extensions_main.extensions_app.state, "override_callbacks", None)
    try:
        async with extensions_main.extensions_overrides_lifespan(FastAPI()):
            callbacks = extensions_main.extensions_app.state.override_callbacks
        assert (
            callbacks[(SettingClassEnum.SETTINGS, "LOGGING")]
            is apply_logging_dictconfig
        )
        assert (
            callbacks[(SettingClassEnum.EXTENSIONS_SETTINGS, "APP_DRAIN")]
            is extensions_main._reseed_system_periodic_tasks
        )
    finally:
        extensions_main.extensions_app.state.override_callbacks = original


class TestBootMarker:
    """Pin which PMM Extensions callbacks the boot-time seed fires on its own."""

    def test_logging_rebind_fires_on_boot(self) -> None:
        """Mark the logging rebind: boot ``dictConfig`` never sees the override."""
        assert is_fire_on_boot(apply_logging_dictconfig)

    def test_pmm_invalidation_does_not_fire_on_boot(self) -> None:
        """Keep PMM eviction silent at boot: a fresh registry key-misses on its own."""
        assert not is_fire_on_boot(invalidate_pmm_clients)
