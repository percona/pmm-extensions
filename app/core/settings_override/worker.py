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

"""Own a Celery prefork child's settings-override boundary refresher."""

from __future__ import annotations

__all__ = ["SEED_TIMEOUT_FRACTION", "WorkerRefresher"]

import logging
import time
import weakref
from typing import TYPE_CHECKING

from app.core.settings_override.lifecycle import (
    bounded_refresh,
    bounded_seed,
    resolve_refresher_options,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from datetime import timedelta

    from app.core.settings_override.lifecycle import (
        CallbackRegistry,
        ProxyRegistry,
        SessionMakerFactory,
    )

logger = logging.getLogger(__name__)

#: Fraction of the prefork pool's child-liveness deadline that all inline seeds
#: on one event loop may consume together. Strictly below 1.0:
#: ``process_initializer`` does more than the seeds, and the child must still
#: send ``WORKER_UP`` inside the same window.
SEED_TIMEOUT_FRACTION = 0.5

_seed_deadlines: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, float] = (
    weakref.WeakKeyDictionary()
)


def _remaining_seed_budget(loop: asyncio.AbstractEventLoop, window: float) -> float:
    """Return what is left of ``loop``'s shared inline-seed window.

    The first bounded seed on ``loop`` opens a window of ``window`` seconds, and
    every later seed on the same loop is bounded by what remains, so the seeds of
    all refreshers in one prefork child together stay inside
    :data:`SEED_TIMEOUT_FRACTION` of the liveness deadline. Keyed by loop because
    each child gets a fresh loop at ``worker_process_init``; the entry is dropped
    with the loop.

    :param loop: The event loop the seed runs on.
    :param window: The whole window in seconds, opened on the first call for
        ``loop``.
    :return: The seconds left in the window, never negative.
    """
    now = time.monotonic()
    deadline = _seed_deadlines.setdefault(loop, now + window)
    return max(0.0, deadline - now)


class WorkerRefresher:
    """Own one prefork child's settings-override boundary refresher.

    Hold the lifecycle boilerplate every worker-side refresher needs — the
    enabled gate, the idempotent already-armed early-return, the initial
    inline seed, and disarm on shutdown — so each service module only
    supplies its own event loop, session maker and proxy set. After
    :meth:`start`, refreshes are pulled from ``task_prerun`` via
    :meth:`maybe_refresh` rather than driven by a background
    ``asyncio.sleep`` loop: a prefork child's loop only runs inside
    ``run_until_complete`` windows, so a timer-based cycle could starve.
    :meth:`prime` warms the proxies in the worker's main process before the
    fork, so each child's seed skips the settings build.

    Every dependency is a zero-argument callable resolved at :meth:`start`
    time, never a value captured at construction: the instance is a
    module-level singleton built at import, while the event loop is recreated
    per prefork child and the session maker is rebound in tests. The
    last-refresh timestamp is likewise per-child after the fork. ``now`` is
    the same shape so tests can advance the due-check without writing
    ``_last_refresh``.

    ``interval``, ``enabled`` and ``proc_alive_timeout`` are :meth:`start`
    parameters rather than reads of ``app.core.config.settings`` because the
    override substrate must not import that module at runtime — ``Settings``
    is itself built from :mod:`app.core.settings_override.proxy`.

    :param loop_getter: Returns the event loop that drives the seed and
        boundary refreshes.
    :param session_maker_factory: Returns the service-scoped
        ``async_sessionmaker`` the refresh cycle reads override rows through.
    :param proxies_factory: Composes the proxy registry to refresh, invoked
        once per effective :meth:`start` and once per effective :meth:`prime`.
    :param now: Returns the monotonic clock used for the due-check and
        last-refresh stamp. Defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        loop_getter: Callable[[], asyncio.AbstractEventLoop],
        session_maker_factory: SessionMakerFactory,
        proxies_factory: Callable[[], ProxyRegistry],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop_getter = loop_getter
        self._session_maker_factory = session_maker_factory
        self._proxies_factory = proxies_factory
        self._now = now
        self._interval_seconds: float = 0.0
        self._armed: bool = False
        self._last_refresh: float = 0.0
        self._proxies: ProxyRegistry | None = None
        self._callbacks: CallbackRegistry | None = None
        self._pending_refresh: asyncio.Task | None = None

    def start(
        self,
        interval: timedelta | None = None,
        *,
        enabled: bool | None = None,
        callbacks: CallbackRegistry | None = None,
        proc_alive_timeout: float | None = None,
    ) -> None:
        """Seed this child's overrides and arm boundary refresh, unless disabled.

        The initial refresh awaits inline so the snapshot is seeded before this
        method returns. No periodic ``asyncio.Task`` is created: subsequent
        refreshes are pulled from :meth:`maybe_refresh` at task boundaries.

        When ``proc_alive_timeout`` is set, the inline seed is bounded via
        :func:`~app.core.settings_override.lifecycle.bounded_seed` by what
        remains of the loop's shared seed window:
        :data:`SEED_TIMEOUT_FRACTION` of that deadline, across every refresher
        seeding on the same loop. A hanging database therefore cannot push the
        child past the prefork pool's liveness window however many refreshers
        it runs. A seed that starts after the window is spent runs with a 0s
        budget, so it expires unless it completes without yielding. On
        seed-budget expiry the child is still armed — the next due
        ``task_prerun`` will refresh once any cancelled seed has finished
        unwinding — and may retain a possibly incomplete seed (proxies
        published before the hang keep DB overrides; others stay on their prior
        snapshots) until then. A cancelled seed that is still unwinding is
        retained on ``_pending_refresh`` so :meth:`maybe_refresh` does not open
        another session while the stuck cleanup holds a pool connection.

        :param interval: Minimum delay between boundary refreshes. ``None``,
            the default, reads
            ``Settings.SETTINGS_OVERRIDE.REFRESH_INTERVAL``.
        :param enabled: Whether to arm a refresher at all. When ``False``
            nothing is resolved (neither the proxy registry nor the session
            maker) and the child stays disarmed. ``None``, the default, reads
            ``Settings.SETTINGS_OVERRIDE.REFRESHER_ENABLED``.
        :param callbacks: Optional rebind callbacks fired by a boundary
            :func:`~app.core.settings_override.lifecycle.refresh_all` when a
            watched override changes value. The inline seed fires only those
            marked with :func:`~app.core.settings_override.lifecycle.fire_on_boot`,
            for keys it published an override for.
        :param proc_alive_timeout: The prefork pool's child-liveness deadline
            in seconds; the seed is bounded by what remains of the loop's
            shared window of :data:`SEED_TIMEOUT_FRACTION` of it. ``None`` (the
            default) leaves the inline seed unbounded and opens no window.
        :raises Exception: Re-raises whatever ``proxies_factory()`` raises —
            it is evaluated before the refresh starts — and whatever an
            unbounded seed propagates, in practice limited to
            ``session_maker_factory()`` failures. Per-proxy refresh failures
            are caught and logged inside ``refresh_all``. A bounded-seed
            expiry is caught inside :func:`~app.core.settings_override.lifecycle.bounded_seed`
            and does not propagate; the child is still armed.
        """
        interval, enabled = resolve_refresher_options(interval, enabled=enabled)
        if not enabled:
            return
        if self._armed:
            return
        proxies = self._proxies_factory()
        loop = self._loop_getter()
        seed_timeout = (
            None
            if proc_alive_timeout is None
            else _remaining_seed_budget(
                loop, proc_alive_timeout * SEED_TIMEOUT_FRACTION
            )
        )
        seeded, pending = loop.run_until_complete(
            bounded_seed(
                self._session_maker_factory,
                proxies,
                seed_timeout,
                callbacks=callbacks,
            )
        )
        now = self._now()
        self._last_refresh = now if seeded else now - interval.total_seconds()
        self._pending_refresh = (
            pending if pending is not None and not pending.done() else None
        )
        self._interval_seconds = interval.total_seconds()
        self._proxies = proxies
        self._callbacks = callbacks
        self._armed = True

    def prime(self, *, enabled: bool | None = None) -> None:
        """Resolve every proxy's YAML/env settings before the prefork pool forks.

        Intended for a ``worker_init`` receiver, which Celery dispatches in the
        worker's main process after the include modules are imported and before
        the pool forks. Each proxy caches its resolved instance, so the children
        inherit it and their inline seed does not rebuild the settings class,
        which re-reads the YAML profile, inside its seed budget. Nothing is
        armed, and neither the event loop nor the session maker is resolved. A
        proxy that fails to resolve stays unresolved, and the child retries it
        the next time it resolves that proxy.

        :param enabled: Whether the refresher will arm at all. When ``False``
            nothing is resolved. ``None``, the default, reads
            ``Settings.SETTINGS_OVERRIDE.REFRESHER_ENABLED``.
        :raises Exception: Re-raises whatever ``proxies_factory()`` raises;
            Celery logs an exception from a ``worker_init`` receiver and the
            worker continues.
        """
        _, enabled = resolve_refresher_options(None, enabled=enabled)
        if not enabled:
            return
        for setting_class, entry in self._proxies_factory().items():
            try:
                entry.proxy.get_base()
            except Exception:
                logger.debug(
                    "Deferring %s settings resolution to the child seed",
                    setting_class,
                    exc_info=True,
                )

    def maybe_refresh(self) -> None:
        """Refresh overrides when armed and the interval has elapsed.

        Intended for ``task_prerun`` receivers. The due-check is a monotonic
        comparison with no I/O: tasks arriving inside the interval no-op.
        When a prior bounded refresh (seed or boundary) was cancelled and is
        still unwinding, this method also no-ops without awaiting that task —
        so a stuck ``AsyncSession.__aexit__`` cannot pile up further pool
        checkouts at later boundaries.
        When due, ``refresh_all`` runs to completion inside a single
        ``run_until_complete`` window, bounded by the refresh interval itself
        via :func:`~app.core.settings_override.lifecycle.bounded_refresh`
        (``asyncio.wait``, cancel without awaiting unwind — not
        ``wait_for``, which would still hang on a stuck
        ``AsyncSession.__aexit__``). Budget expiry logs once at WARNING and
        retains the cancelled task handle; exceptions raised from the
        ``run_until_complete`` call are logged and swallowed. A failure that
        surfaces only while a cancelled refresh is still unwinding is logged
        by the drain callback when that task finishes, not by this method.
        Neither case fails or aborts the task that triggered the refresh.
        Because proxies publish sequentially, a timed-out or failed cycle may
        leave a split registry (earlier proxies refreshed, later ones on their
        prior snapshots) rather than rolling everything back.
        The interval stamp advances on every attempted due refresh so a
        failing cycle cannot hammer the database on every subsequent
        dispatch.
        """
        if not self._armed or self._proxies is None:
            return
        if self._pending_refresh is not None:
            if not self._pending_refresh.done():
                return
            self._pending_refresh = None
        now = self._now()
        if now - self._last_refresh < self._interval_seconds:
            return
        try:
            completed, pending = self._loop_getter().run_until_complete(
                bounded_refresh(
                    self._session_maker_factory,
                    self._proxies,
                    self._interval_seconds,
                    self._callbacks,
                )
            )
            if not completed:
                self._pending_refresh = (
                    pending if pending is not None and not pending.done() else None
                )
                logger.warning(
                    "Settings-override boundary refresh exceeded its %.2fs budget; "
                    "leaving a possibly incomplete refresh in place",
                    self._interval_seconds,
                )
        except Exception:
            logger.exception(
                "Settings-override boundary refresh failed; "
                "leaving a possibly incomplete refresh in place"
            )
        finally:
            self._last_refresh = self._now()

    def stop(self) -> None:
        """Disarm this child's refresher; a no-op when never started.

        After disarm, :meth:`maybe_refresh` no-ops so a shut-down child
        performs no further boundary refresh. There is no periodic task to
        cancel. A cancelled refresh still unwinding is dropped from
        ``_pending_refresh`` without being awaited — the prefork child is
        exiting.
        """
        self._armed = False
        self._proxies = None
        self._callbacks = None
        self._pending_refresh = None
