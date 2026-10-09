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

"""Async refresher that periodically reloads override snapshots from the DB."""

from __future__ import annotations

__all__ = [
    "CallbackRegistry",
    "ProxyEntry",
    "ProxyRegistry",
    "RefreshCallback",
    "SnapshotChange",
    "bounded_refresh",
    "bounded_seed",
    "fire_boot_callbacks",
    "fire_change_callbacks",
    "fire_on_boot",
    "is_fire_on_boot",
    "previous_or_base",
    "publish_snapshot",
    "refresh_all",
    "settings_override_refresher",
    "start_refresh_task",
]

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from typing import NamedTuple, TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.settings_override.cache import build_snapshot
from app.core.settings_override.proxy import OverridableSettingsProxy

if TYPE_CHECKING:
    from datetime import timedelta

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.core.config import BaseYamlSettings

logger = logging.getLogger(__name__)

SessionMakerFactory = Callable[[], async_sessionmaker]


def _drain_cancelled_refresh_task(task: asyncio.Task) -> None:
    """Retrieve a finished refresh task's exception and log it if there is one.

    :param task: The finished refresh task (cancelled tasks are ignored).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "Settings-override refresh raised while unwinding after cancellation: %r",
            exc,
        )


class SnapshotChange(NamedTuple):
    """Represent the override snapshots on either side of a republish.

    A snapshot holds active overrides only, never an effective view, so a key
    may be absent from ``previous``, ``current``, or both. When a key is
    absent the prior (or new) effective value is the YAML/env one reachable
    through the proxy's wrapped instance.

    :param previous: The snapshot in effect before the republish.
    :param current: The snapshot now in effect.
    """

    previous: Mapping[str, object]
    current: Mapping[str, object]


def previous_or_base(
    change: SnapshotChange, proxy: OverridableSettingsProxy, key: str
) -> object:
    """Return the previous effective value for ``key``, falling back to YAML/env.

    A snapshot holds active overrides only, so ``key`` may be absent from
    ``change.previous``. The YAML/env value on the proxy's wrapped instance is
    then the prior effective value. Membership decides the fallback rather than
    the value being ``None``, so an override that sets a nullable field to
    ``None`` reports ``None`` instead of the base.

    :param change: The override snapshots on either side of the republish.
    :param proxy: The overridable settings proxy that owns ``key``.
    :param key: The top-level snapshot key to read.
    :return: The previous override value, or the YAML/env base when ``key`` was
        not overridden.
    """
    if key in change.previous:
        return change.previous[key]
    return getattr(proxy._resolve(), key)  # noqa: SLF001


#: A rebind callback fired when a watched ``(setting_class, key)`` override
#: changes value between refresh cycles, or -- for the :func:`fire_on_boot`
#: subset -- when the boot seed publishes an override for that key. The
#: callback receives a :class:`SnapshotChange` carrying the override snapshots
#: on either side of the republish (overrides-only; a key may be absent from
#: either side). Any exception it raises is caught and logged by
#: :func:`_fire_callback` so one failing callback cannot break the cycle.
RefreshCallback = Callable[[SnapshotChange], Awaitable[None]]
CallbackRegistry = dict[tuple[str, str], RefreshCallback]

#: Attribute :func:`fire_on_boot` sets on a callback; the boot seed filters on it.
_FIRE_ON_BOOT = "_settings_override_fire_on_boot"


def fire_on_boot(callback: RefreshCallback) -> RefreshCallback:
    """Mark ``callback`` as one the boot-time seed must fire itself.

    A rebind callback is normally fired only on a *change* between refresh
    cycles, because the boot path reproduces its effect from the seeded
    snapshot on its own: a fresh process key-misses its caches into
    override-aware objects. Marked callbacks are the exception: their effect
    lives in process-wide state the snapshot does not carry, so booting with
    an override already in the database would leave that state stale until
    the override next changed.

    :param callback: A plain-function rebind callback. The marker is stored as
        an attribute on the function object, so one marked callable is
        boot-firing in every registry that references it.
    :return: The same ``callback``, marked.
    """
    setattr(callback, _FIRE_ON_BOOT, True)
    return callback


def is_fire_on_boot(callback: RefreshCallback) -> bool:
    """Return whether :func:`fire_on_boot` marked ``callback``.

    :param callback: The rebind callback to inspect.
    :return: ``True`` when the boot seed must fire ``callback`` itself.
    """
    return getattr(callback, _FIRE_ON_BOOT, False)


async def publish_snapshot(
    proxy: OverridableSettingsProxy,
    session: AsyncSession,
    settings_cls: type[BaseYamlSettings],
) -> None:
    """Build a fresh snapshot for ``settings_cls`` and publish it through ``proxy``.

    Public seam over :meth:`OverridableSettingsProxy._set_snapshot` so the
    protected method's "background refresher + per-test fixtures only" contract
    stays accurate while API handlers and the refresher itself can both publish
    new snapshots through one named path.

    :param proxy: The proxy whose snapshot is being replaced.
    :type proxy: OverridableSettingsProxy
    :param session: The async SQLModel session used to read override rows.
    :type session: AsyncSession
    :param settings_cls: The Pydantic settings class being snapshotted.
    :type settings_cls: type[BaseYamlSettings]
    """
    # Resolve the wrapped (YAML/env) instance so nested-field overrides merge
    # onto current parent values; ``_resolve`` bypasses the snapshot so the
    # base is never a previously-merged copy.
    base_settings = proxy._resolve()  # noqa: SLF001
    snapshot = await build_snapshot(session, settings_cls, base_settings)
    proxy._set_snapshot(snapshot)  # noqa: SLF001


class ProxyEntry(NamedTuple):
    """Pair a wired proxy with the Pydantic class it wraps.

    :param proxy: The overridable proxy instance whose snapshot to refresh.
    :type proxy: OverridableSettingsProxy
    :param settings_cls: The Pydantic settings class wrapped by ``proxy``.
        Used to look up field metadata when building the snapshot.
    :type settings_cls: type[BaseYamlSettings]
    """

    proxy: OverridableSettingsProxy
    settings_cls: type[BaseYamlSettings]


ProxyRegistry = dict[str, ProxyEntry]


async def fire_change_callbacks(
    callbacks: CallbackRegistry,
    setting_class: str,
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> None:
    """Fire the registered callback for every key whose snapshot value changed.

    Compares ``previous`` against ``current`` and, for each ``(setting_class,
    key)`` whose value differs and has a registered callback, awaits the
    callback with a :class:`SnapshotChange` carrying both mappings. Each
    callback runs inside its own ``try/except`` so one failure neither aborts
    the cycle nor blocks the remaining callbacks.

    Both mappings are override snapshots only, never an effective view, so a key
    may be absent from either side (for example on the override-delete path the
    changed key is gone from ``current``). Callbacks that need the previous
    effective value when the key is absent recover it through the proxy's
    wrapped YAML/env instance.

    Shared by the background refresher (:func:`refresh_all`, diffing the snapshot
    it just rebuilt) and the settings-API PATCH/DELETE handlers (diffing the
    snapshot they publish inline), so both publish paths deliver the same rebind
    notifications.

    :param callbacks: The registered rebind callbacks keyed by
        ``(setting_class, key)``.
    :param setting_class: The class whose snapshot was just republished.
    :param previous: The override snapshot in effect before the republish.
    :param current: The override snapshot now in effect.
    """
    change = SnapshotChange(previous, current)
    for key in previous.keys() | current.keys():
        if previous.get(key) == current.get(key):
            continue
        callback = callbacks.get((setting_class, key))
        if callback is not None:
            await _fire_callback(callback, setting_class, key, change)


async def fire_boot_callbacks(
    callbacks: CallbackRegistry,
    setting_class: str,
    current: Mapping[str, object],
) -> None:
    """Fire the marked callbacks for keys the seed just published an override for.

    Membership in ``current`` is the gate: a key with no override row resolves
    to the YAML/env value the boot-time configuration already applied, so
    re-entering the callback would be busywork. ``previous`` is handed to the
    callback empty, which is truthful at boot and lets :func:`previous_or_base`
    fall back to the YAML/env value that was in effect.

    :param callbacks: The registered rebind callbacks keyed by
        ``(setting_class, key)``.
    :param setting_class: The class whose snapshot was just seeded.
    :param current: The override snapshot the seed just published.
    """
    change = SnapshotChange({}, current)
    for key in current:
        callback = callbacks.get((setting_class, key))
        if callback is not None and is_fire_on_boot(callback):
            await _fire_callback(callback, setting_class, key, change)


async def _fire_callback(
    callback: RefreshCallback, setting_class: str, key: str, change: SnapshotChange
) -> None:
    """Await one rebind callback, logging instead of propagating a failure.

    One failing callback must neither abort the refresh cycle nor block the
    remaining callbacks.

    :param callback: The rebind callback to await.
    :param setting_class: The class whose snapshot was just republished.
    :param key: The overridden key the callback is registered under.
    :param change: The snapshots on either side of the republish.
    """
    try:
        await callback(change)
    except Exception:
        logger.exception(
            "Rebind callback for %s.%s failed; the rebind may be incomplete",
            setting_class,
            key,
        )


async def refresh_all(
    session_maker_factory: SessionMakerFactory,
    proxies: ProxyRegistry,
    callbacks: CallbackRegistry | None = None,
    *,
    boot: bool = False,
) -> None:
    """Refresh override snapshots for all wired proxies in a single session.

    Per-proxy failures (raised from :func:`build_snapshot` once a session is
    open) are caught and logged: the previous snapshot is retained for that
    proxy and the rest of ``proxies`` continue to refresh. The session is
    rolled back after each failure so that on Postgres -- where a failed
    query leaves the transaction in ``InFailedSqlTransaction`` state until
    explicit rollback -- the next proxy's ``manager.list(...)`` does not
    inherit the aborted transaction. Failures that occur *before* per-proxy
    iteration begins -- specifically from ``session_maker_factory()`` or
    ``async_session_maker()`` -- are NOT handled here; they propagate to the
    caller. Callers wrap their invocations in a broad ``except`` so transient
    engine/pool errors do not kill the refresher: the web path in
    :func:`start_refresh_task`'s periodic loop, and the worker path in
    :meth:`~app.core.settings_override.worker.WorkerRefresher.maybe_refresh`.

    When ``callbacks`` is supplied, the snapshot in effect before each proxy's
    republish is diffed against the new one and the registered callback for any
    changed ``(setting_class, key)`` is fired (see :func:`fire_change_callbacks`).
    A proxy whose republish failed is skipped without firing callbacks. The
    initial inline seed in :func:`bounded_seed` runs with ``boot=True``: no
    *change* callbacks fire, only the :func:`fire_on_boot` subset, for keys the
    seed published an override for. Firing happens right after each proxy
    publishes, inside the same task, so a bounded seed that later gives up
    cannot leave a published proxy with its boot callback unfired.

    :param session_maker_factory: A zero-argument callable returning a
        service-scoped ``async_sessionmaker``. Invoked exactly once per call
        to avoid recreating session makers on each iteration.
    :param proxies: The wired proxy registry keyed by class identifier.
    :param callbacks: Optional rebind callbacks fired for changed keys. When
        ``None``, snapshots are republished without any change detection.
    :param boot: Whether this is the inline boot seed. When ``True`` the
        per-proxy diff is replaced by :func:`fire_boot_callbacks`.
    :raises Exception: Re-raises any failure from
        ``session_maker_factory()`` itself (e.g. the factory is misconfigured
        or its engine cannot be constructed). Connection-time failures
        against the DB -- engine unreachable, pool exhausted, auth -- do not
        surface here because ``async_session_maker()`` does not check out a
        connection; those errors fire later inside ``build_snapshot()`` and
        are caught by the per-proxy handler.
    """
    async_session_maker = session_maker_factory()
    async with async_session_maker() as session:
        for setting_class, entry in proxies.items():
            previous = entry.proxy.get_snapshot() if callbacks else None
            try:
                await publish_snapshot(entry.proxy, session, entry.settings_cls)
            except Exception:
                logger.exception(
                    "Failed to refresh overrides for %s; keeping previous snapshot",
                    setting_class,
                )
                # Roll back so a Postgres ``InFailedSqlTransaction`` from this
                # proxy does not cascade into every subsequent proxy's
                # ``manager.list(...)`` on the shared session.
                await session.rollback()
                continue
            if callbacks is None or previous is None:
                continue
            current = entry.proxy.get_snapshot()
            if boot:
                await fire_boot_callbacks(callbacks, setting_class, current)
            else:
                await fire_change_callbacks(callbacks, setting_class, previous, current)


async def bounded_refresh(
    session_maker_factory: SessionMakerFactory,
    proxies: ProxyRegistry,
    budget: float,
    callbacks: CallbackRegistry | None = None,
    *,
    boot: bool = False,
) -> tuple[bool, asyncio.Task | None]:
    """Run :func:`refresh_all` bounded by ``budget`` seconds.

    Prefer :func:`asyncio.wait` over :func:`asyncio.wait_for`: ``wait_for``
    awaits the cancelled coroutine's unwind, so a hung
    ``AsyncSession.__aexit__`` (returning a stuck connection to the pool)
    would still blow the budget. Cancel without awaiting so the wall-clock
    bound holds.

    On budget expiry the cancelled task handle is returned (second element)
    rather than dropped: cancellation is only advisory, so a stuck
    ``__aexit__`` can leave the task pending on the loop with its session
    still held. Callers must retain that handle and refuse further refreshes
    until it completes — otherwise each later due cycle opens another
    session and can exhaust the pool. Do not await the pending task on the
    hot path; only check ``.done()``.

    Shared by :func:`bounded_seed` (child/web startup) and
    :meth:`~app.core.settings_override.worker.WorkerRefresher.maybe_refresh`
    (task-boundary pull) so neither path can reintroduce ``wait_for``.
    Callers own their log line on expiry; this helper only returns
    ``(False, pending_task)``.

    :param session_maker_factory: A zero-argument callable returning a
        service-scoped ``async_sessionmaker``.
    :param proxies: The wired proxy registry keyed by class identifier.
    :param budget: Wall-clock budget in seconds for the refresh.
    :param callbacks: Optional rebind callbacks forwarded to
        :func:`refresh_all`.
    :param boot: Whether this is the inline boot seed, forwarded to
        :func:`refresh_all`.
    :return: ``(True, None)`` when the refresh completed; ``(False, task)``
        when the budget expired. ``task`` is the cancelled refresh task,
        which may still be unwinding.
    :raises Exception: Re-raises any failure from a completed
        :func:`refresh_all` — in practice limited to
        ``session_maker_factory()`` failures. A budget expiry does not
        propagate.
    """
    refresh_task = asyncio.create_task(
        refresh_all(session_maker_factory, proxies, callbacks, boot=boot)
    )
    done, _ = await asyncio.wait({refresh_task}, timeout=budget)
    if done:
        await refresh_task
        return True, None
    refresh_task.cancel()
    refresh_task.add_done_callback(_drain_cancelled_refresh_task)
    return False, refresh_task


async def bounded_seed(
    session_maker_factory: SessionMakerFactory,
    proxies: ProxyRegistry,
    seed_timeout: float | None,
    *,
    callbacks: CallbackRegistry | None = None,
) -> tuple[bool, asyncio.Task | None]:
    """Run an initial override refresh, optionally bounded by ``seed_timeout``.

    When ``seed_timeout`` is ``None`` the seed awaits :func:`refresh_all`
    unbounded and returns ``(True, None)`` on completion. When set, the seed
    is bounded by :func:`bounded_refresh`; on expiry the timeout is logged at
    ERROR naming the budget, and ``(False, pending_task)`` is returned so the
    caller can still arm its refresher while retaining the cancelled task
    handle (see :func:`bounded_refresh`). Because :func:`refresh_all`
    publishes each proxy immediately, a timed-out seed may already have
    applied DB overrides to earlier proxies while later ones remain on their
    prior (typically env-only) snapshots — a possibly incomplete seed, not a
    guaranteed clean env-only start.

    Shared by :func:`start_refresh_task` (web lifespans) and
    :class:`~app.core.settings_override.worker.WorkerRefresher` (prefork
    children) so the budget, cancel, and ERROR log cannot drift.

    The seed fires no *change* callbacks: there is no previous snapshot to
    diff against, and a fresh process reproduces most rebinds from the seeded
    snapshot on its own. Only the :func:`fire_on_boot` subset of ``callbacks``
    fires, for keys the seed published an override for.

    :param session_maker_factory: A zero-argument callable returning a
        service-scoped ``async_sessionmaker``.
    :param proxies: The wired proxy registry keyed by class identifier.
    :param seed_timeout: Wall-clock budget in seconds for the inline seed, or
        ``None`` to leave the seed unbounded.
    :param callbacks: Optional rebind callbacks; only those marked with
        :func:`fire_on_boot` fire during the seed.
    :return: ``(True, None)`` when the seed completed; ``(False, task)`` when
        the budget expired. ``task`` is the cancelled seed task, which may
        still be unwinding.
    :raises Exception: Re-raises any failure from a completed
        :func:`refresh_all` — in practice limited to
        ``session_maker_factory()`` failures. A budget expiry does not
        propagate.
    """
    if seed_timeout is None:
        await refresh_all(session_maker_factory, proxies, callbacks, boot=True)
        return True, None
    seeded, pending = await bounded_refresh(
        session_maker_factory, proxies, seed_timeout, callbacks, boot=True
    )
    if not seeded:
        logger.error(
            "Initial settings-override refresh exceeded its %.2fs seed "
            "budget; continuing with a possibly incomplete seed",
            seed_timeout,
        )
    return seeded, pending


async def start_refresh_task(
    session_maker_factory: SessionMakerFactory,
    proxies: ProxyRegistry,
    interval: timedelta,
    callbacks: CallbackRegistry | None = None,
) -> asyncio.Task:
    """Perform an initial refresh and start a background refresh loop.

    The initial refresh awaits inline so the lifespan does not yield until
    the first snapshot has been observed. Subsequent refreshes run inside an
    :func:`asyncio.create_task` that sleeps for ``interval`` between cycles.

    The inline initial refresh fires no *change* callbacks — the startup
    snapshot seeds the proxies, and long-lived objects are constructed against
    the effective snapshot directly during lifespan startup. Only the
    :func:`fire_on_boot` subset of ``callbacks`` fires during the seed, for
    keys it published an override for; every callback fires on change from the
    periodic loop.

    The inline seed always goes through :func:`bounded_seed` unbounded
    (``seed_timeout=None``), matching the web lifespan contract. Bounded
    seeds for prefork children live on
    :class:`~app.core.settings_override.worker.WorkerRefresher`.

    :param session_maker_factory: A zero-argument callable returning a
        service-scoped ``async_sessionmaker``.
    :param proxies: The wired proxy registry keyed by class identifier.
    :param interval: The wall-clock delay between refresh cycles. Must be a
        positive duration; the :class:`Settings` field validator enforces
        this at construction time.
    :param callbacks: Optional rebind callbacks fired by the periodic loop when
        a watched override changes. The initial refresh fires only those
        marked with :func:`fire_on_boot`.
    :return: The background refresh task. Callers must cancel and await this
        task during shutdown to drain pending iterations cleanly.
    :raises Exception: Re-raises any failure from the inline initial
        :func:`refresh_all` call — in practice limited to
        ``session_maker_factory()`` failures (see :func:`refresh_all` for the
        narrowed contract). Connection-time DB failures from the initial
        snapshot build are caught per-proxy and do NOT propagate. Subsequent
        iterations inside the background task are wrapped in ``except`` and do
        NOT propagate; only ``session_maker_factory()`` failures at startup can
        break the lifespan.
    """
    await bounded_seed(
        session_maker_factory, proxies, seed_timeout=None, callbacks=callbacks
    )
    interval_seconds = interval.total_seconds()

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await refresh_all(session_maker_factory, proxies, callbacks)
            except Exception:
                logger.exception(
                    "Settings override refresher iteration failed; will retry next cycle"
                )

    return asyncio.create_task(_loop(), name="settings-override-refresher")


def resolve_refresher_options(
    interval: timedelta | None, *, enabled: bool | None
) -> tuple[timedelta, bool]:
    """Return the refresher options, reading settings for whichever is unset.

    ``Settings.SETTINGS_OVERRIDE`` configures the refresher for all three
    services, so every start-up site would otherwise repeat the same two reads
    and each is a place the next rename can miss one. Reading here also reads
    late enough to see a value the proxy snapshot has since refreshed.

    :param interval: The caller's explicit refresh interval, or ``None``.
    :param enabled: The caller's explicit enabled flag, or ``None``.
    :return: ``(interval, enabled)`` with any ``None`` replaced by the
        configured value.
    """
    if interval is not None and enabled is not None:
        return interval, enabled
    from app.core.config import (  # noqa: PLC0415 - config imports settings_override, whose __init__ imports this (cycle)
        settings,
    )

    options = settings.SETTINGS_OVERRIDE
    return (
        options.REFRESH_INTERVAL if interval is None else interval,
        options.REFRESHER_ENABLED if enabled is None else enabled,
    )


@asynccontextmanager
async def settings_override_refresher(
    session_maker_factory: SessionMakerFactory,
    proxies: ProxyRegistry,
    interval: timedelta | None = None,
    *,
    enabled: bool | None = None,
    callbacks: CallbackRegistry | None = None,
) -> AsyncGenerator[None, None]:
    """Run the background override refresher for the duration of a lifespan.

    Start the periodic refresher on enter (when ``enabled``), then cancel and
    drain it on exit. When ``enabled`` is ``False`` the context manager is a
    no-op and no refresh task is created -- the wrapped block still runs.

    :param session_maker_factory: A zero-argument callable returning a
        service-scoped ``async_sessionmaker``, forwarded to
        :func:`start_refresh_task`.
    :param proxies: The wired proxy registry keyed by class identifier.
    :param interval: The wall-clock delay between refresh cycles. ``None``, the
        default, reads ``Settings.SETTINGS_OVERRIDE.REFRESH_INTERVAL``.
    :param enabled: Whether to start the background refresher. When ``False``,
        the context manager yields without creating a task. ``None``, the
        default, reads ``Settings.SETTINGS_OVERRIDE.REFRESHER_ENABLED``.
    :param callbacks: Optional rebind callbacks forwarded to
        :func:`start_refresh_task`, fired by the periodic loop when a watched
        override changes. The initial refresh fires only those marked with
        :func:`fire_on_boot`.
    :return: None
    """
    interval, enabled = resolve_refresher_options(interval, enabled=enabled)
    refresher: asyncio.Task | None = None
    if enabled:
        refresher = await start_refresh_task(
            session_maker_factory, proxies, interval, callbacks
        )
    try:
        yield
    finally:
        if refresher is not None:
            refresher.cancel()
            with suppress(asyncio.CancelledError):
                await refresher
