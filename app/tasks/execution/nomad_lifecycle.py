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

"""Own the entered NomadExecutor and rebind it when its override changes."""

__all__ = ["NomadLifecycle", "normalize_nomad_config_value"]

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Self, TYPE_CHECKING

from fastapi import FastAPI

from app.core.requests.remote_api import PendingCloses
from app.core.utils.fields import PRESERVE_CREDENTIALS_CONTEXT
from app.tasks.config import tasks_settings

if TYPE_CHECKING:
    # The package export resolves through a PEP 562 __getattr__ that has to
    # declare `object`, so import the class itself for annotations and keep the
    # lazy path for the runtime binding that breaks the import cycle.
    from app.tasks.execution.executors.nomad.models import NomadExecutor
else:
    from app.tasks.execution.executors.nomad import NomadExecutor

logger = logging.getLogger(__name__)


def normalize_nomad_config_value(value: object) -> NomadExecutor:
    """Return the effective ``NOMAD`` value as a usable :class:`NomadExecutor`.

    Both production paths deliver the value already typed, so they pass straight
    through: a nested override lands in the snapshot as a merged
    :class:`NomadExecutor` copied off the YAML value, and with no override the
    snapshot falls through to that YAML value itself. A config fingerprint
    mapping is reconstructed instead, for a caller holding one rather than a
    model.

    A request-less reader that only drives the config-built sync
    ``self.backend`` sub-client needs a usable :class:`NomadExecutor`
    *instance*, not a mapping; an un-entered instance is sufficient because
    those readers never touch the live aiohttp session.

    :param value: The effective ``NOMAD`` value: a :class:`NomadExecutor` or a
        config fingerprint mapping.
    :return: The value itself when already a :class:`NomadExecutor`, otherwise
        a freshly-validated (un-entered) executor built from the mapping.
    :raises ValidationError: If ``value`` is a mapping that does not describe a
        valid :class:`NomadExecutor`.
    :raises TypeError: If ``value`` is neither a :class:`NomadExecutor` nor a
        mapping.
    """
    if isinstance(value, NomadExecutor):
        return value
    if isinstance(value, Mapping):
        return NomadExecutor.model_validate(value)
    raise TypeError(f"Cannot normalize {type(value).__name__} to a NomadExecutor")


class NomadLifecycle:
    """Own the entered :class:`NomadExecutor` and rebind it on config changes.

    The live entered executor (the one with an open aiohttp session) lives here
    in ``app.state.nomad_lifecycle`` and nowhere else: neither the YAML settings
    value nor the override snapshot's copy of it is ever entered, so no reader
    outside this holder can be handed the session it owns.

    :meth:`reconcile` is wired as the ``(TASKS_SETTINGS, NOMAD)`` rebind
    callback by ``tasks_lifespan``; it opens the new executor before swapping
    and retires the old one afterwards, so a reader resolving :attr:`current`
    after the swap sees the new open session. Deferred retirements are tracked
    on this holder's :class:`PendingCloses` so :meth:`__aexit__` can still
    force-close them at shutdown if a holder never unwinds. :meth:`__aexit__`
    also marks the holder closing under the lock so a reconcile waiting there
    cannot publish a fresh executor after ``_current`` is cleared.

    :param app: The FastAPI application whose ``state`` exposes the holder to
        request-scoped readers via ``get_executor``.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._current: NomadExecutor | None = None
        self._current_config: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._pending_closes = PendingCloses()
        self._closing = False

    @property
    def current(self) -> NomadExecutor:
        """Return the live, entered executor.

        :return: The currently-entered :class:`NomadExecutor`.
        :rtype: NomadExecutor
        :raises RuntimeError: If accessed before :meth:`__aenter__` ran.
        """
        if self._current is None:
            raise RuntimeError("NomadLifecycle not started")
        return self._current

    def _desired(self) -> NomadExecutor:
        """Return a private un-entered executor for the effective NOMAD config.

        The effective value is rebuilt rather than entered as it stands. With no
        override it *is* the live YAML executor; a nested override is a
        ``model_copy`` of that executor, which Pydantic builds carrying the
        original's private attributes by reference, aiohttp session included.
        Both shapes are therefore objects other readers hold too, and entering
        either would leave two executors sharing one session: retiring the first
        closes the session the second is still serving from. Re-validating the
        config fingerprint yields an instance this holder alone owns.

        :return: A freshly-built :class:`NomadExecutor` carrying the effective
            ``NOMAD`` configuration and no session.
        """
        effective = normalize_nomad_config_value(tasks_settings.NOMAD)
        return NomadExecutor.model_validate(
            effective.model_dump(mode="json", context=PRESERVE_CREDENTIALS_CONTEXT)
        )

    async def __aenter__(self) -> Self:
        """Enter the executor the effective config calls for and publish self.

        :return: This holder, registered on ``app.state.nomad_lifecycle``.
        :rtype: Self
        """
        async with self._lock:
            desired = self._desired()
            self._current = await desired.__aenter__()
            self._current_config = desired.model_dump(
                mode="json", context=PRESERVE_CREDENTIALS_CONTEXT
            )
        self._app.state.nomad_lifecycle = self
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Exit the entered executor and force-close any still-deferred retirees.

        Nested ``finally`` so a failure closing the active executor still
        force-closes deferred retirees and unpublishes the holder.
        """
        try:
            async with self._lock:
                self._closing = True
                self._pending_closes.seal()
                if self._current is not None:
                    try:
                        await self._current.__aexit__(None, None, None)
                    finally:
                        self._current = None
        finally:
            try:
                await self._pending_closes.force_close()
            finally:
                self._app.state.nomad_lifecycle = None

    async def reconcile(self) -> None:
        """Rebind the entered executor when the effective NOMAD config changed.

        Opens the new executor first, swaps the reference (a GIL-atomic
        assignment, so readers of :attr:`current` see either the old or the new
        executor but never a half-built one), then retires the old one. A no-op
        when the config is unchanged, or when :meth:`__aexit__` has already
        marked the holder closing (a callback waiting on the lock must not
        publish a fresh session after teardown cleared ``_current``). A
        construction failure propagates to the refresher's per-cycle handler,
        leaving the old executor live.

        The new executor is entered *inside* the lock so the compare-and-swap is
        atomic against a concurrent reconcile. This is safe because
        :meth:`NomadExecutor.__aenter__` only builds an aiohttp ``ClientSession``
        (no network I/O), so it never blocks the lock for a meaningful duration;
        the old executor is retired *outside* the lock to keep shutdown's
        :meth:`__aexit__` from waiting on the close.

        The old executor is retired rather than closed outright: routes that
        resolved it stream off that instance for the whole response, so it stays
        open until the last of them releases it. Retirement is registered on
        :class:`PendingCloses` *under the same lock* as the swap, so
        :meth:`__aexit__` cannot seal and sweep in the gap before
        :meth:`~app.core.requests.remote_api.BaseRemoteAPI.close_when_idle`
        runs — even if this task is cancelled mid-await, or the client was
        idle and would otherwise never touch pending.

        :raises ValidationError: If the overridden config fingerprint cannot be
            reconstructed into a :class:`NomadExecutor` (propagated from
            :func:`normalize_nomad_config_value`).
        :raises TypeError: If the effective ``NOMAD`` value is neither a mapping
            nor a :class:`NomadExecutor` (also from
            :func:`normalize_nomad_config_value`).
        """
        desired = self._desired()
        desired_config = desired.model_dump(
            mode="json", context=PRESERVE_CREDENTIALS_CONTEXT
        )
        close_immediately = False
        old: NomadExecutor | None = None
        async with self._lock:
            if self._closing:
                return
            if desired_config == self._current_config:
                return
            new = await desired.__aenter__()
            old, self._current = self._current, new
            self._current_config = desired_config
            if old is not None and not old.remember_pending_close(self._pending_closes):
                close_immediately = True
        if old is not None:
            if close_immediately:
                await old.close()
            else:
                await old.close_when_idle(pending=self._pending_closes)
