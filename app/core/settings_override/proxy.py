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

"""Provide a ``LazyProxy`` subclass that consults a DB-backed override snapshot."""

__all__ = ["OverridableSettingsProxy"]

from collections.abc import Mapping
from typing import TYPE_CHECKING, TypeVar

from app.core.utils.lazy import LazyProxy

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")
_EMPTY_SNAPSHOT: Mapping[str, object] = {}


class OverridableSettingsProxy(LazyProxy[T]):
    """Wrap a :class:`LazyProxy` so attribute reads consult a DB-backed override snapshot.

    Attribute reads first consult an in-memory snapshot of overrides for the
    associated settings class. Any miss falls back to the wrapped Pydantic
    instance, preserving :class:`LazyProxy` semantics. The snapshot is
    replaced atomically by the background refresher in
    :mod:`app.core.settings_override.lifecycle`; concurrent readers always
    observe a fully-formed snapshot, never a partial one.

    :param factory: Zero-argument callable that produces the real settings
        instance on first access.
    :param setting_class: The class identifier used to load this proxy's
        snapshot from the override table.
    """

    __slots__ = ("_setting_class", "_snapshot")

    def __init__(
        self,
        factory: "Callable[[], T]",
        setting_class: str,
    ) -> None:
        super().__init__(factory)
        object.__setattr__(self, "_snapshot", _EMPTY_SNAPSHOT)
        object.__setattr__(self, "_setting_class", setting_class)

    def __getattr__(self, name: str) -> object:
        snapshot = object.__getattribute__(self, "_snapshot")
        if name in snapshot:
            return snapshot[name]
        return super().__getattr__(name)

    def get_snapshot(self) -> Mapping[str, object]:
        """Return the currently-published override snapshot.

        Exposes the live snapshot reference so the lifecycle layer can diff the
        previous snapshot against a freshly-built one and fire rebind callbacks
        only for keys whose value changed. The returned mapping is the same
        immutable object the refresher published; callers must not mutate it.

        :return: The mapping of field name to typed override value currently in
            effect (empty when no override is published).
        :rtype: Mapping[str, object]
        """
        return object.__getattribute__(self, "_snapshot")

    def _set_snapshot(self, snapshot: Mapping[str, object]) -> None:
        """Replace the snapshot reference atomically.

        Protected to signal "internal API". Only two callers are intended:
        the background refresher in
        :mod:`app.core.settings_override.lifecycle` and per-test fixtures
        that reset state between tests. Other code should read attributes
        normally and let the refresher publish snapshots.

        See :func:`app.core.settings_override.lifecycle.publish_snapshot`
        for the public seam that API handlers use to build and publish a
        fresh snapshot in one call.

        :param snapshot: The new mapping of field name to typed override
            value. The mapping is treated as immutable -- callers must not
            mutate it after the swap.
        :type snapshot: Mapping[str, object]
        """
        object.__setattr__(self, "_snapshot", snapshot)
