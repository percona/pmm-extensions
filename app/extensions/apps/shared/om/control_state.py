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

"""Derive a member's control state from its unit facts, as E0-SP1 decided it.

The control state says how much of a member's lifecycle PMM can drive, which the
host-management guards and drift consume: a plain systemd unit PMM stops and
starts; a unit something restarts (``Restart=always``/``on-success`` or a
non-systemd supervisor) PMM can only shut down transiently, so it is supervised;
a ``Restart=on-failure`` unit PMM can stop cleanly but not keep down through a
failure, so it is shutdown-only; anything PMM cannot resolve is unknown.
"""

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ControlState", "UnitControl", "derive_control_state"]

#: The ``Restart=`` directive values under which something restarts the unit on a
#: clean stop, so PMM cannot hold it down - it is supervised, not PMM-controlled.
_SUPERVISING_RESTART = frozenset({"always", "on-success"})

#: The ``Restart=`` directive value under which PMM can stop the unit cleanly but
#: a failure brings it back, so PMM owns shutdown but not a kept-down state.
_SHUTDOWN_ONLY_RESTART = "on-failure"


class ControlState(StrEnum):
    """Name how much of a member's lifecycle PMM can drive.

    Derived from the unit facts and recorded on the member for the host-management
    guards (E0-T2) and drift (E0-S4).

    :cvar SYSTEMD: A systemd unit PMM both stops and starts.
    :cvar SUPERVISED: Something restarts the process, so PMM cannot hold it down.
    :cvar SHUTDOWN_ONLY: PMM can stop it cleanly but a failure restarts it.
    :cvar UNKNOWN: PMM cannot determine or drive the lifecycle.
    """

    SYSTEMD = "systemd"
    SUPERVISED = "supervised"
    SHUTDOWN_ONLY = "shutdown_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UnitControl:
    """Carry the unit facts a member's control state is derived from.

    :param systemd_unit_present: Whether a systemd unit manages the mongod process.
    :param restart: The unit's ``Restart=`` directive value, or ``None`` when no
        unit manages the process or the directive is unset.
    :param external_supervisor: Whether a non-systemd supervisor manages the
        process, which restarts it regardless of the systemd facts.
    """

    systemd_unit_present: bool
    restart: str | None
    external_supervisor: bool = False


def derive_control_state(unit: UnitControl) -> ControlState:
    """Map a member's unit facts to its control state.

    A non-systemd supervisor makes a member supervised whatever its systemd facts
    say; absent a systemd unit and a supervisor the state is unknown;
    ``Restart=always``/``on-success`` is supervised, ``Restart=on-failure`` is
    shutdown-only, and any other (or no) ``Restart=`` is PMM-controlled systemd.

    :param unit: The member's unit facts.
    :return: The derived control state.
    """
    if unit.external_supervisor:
        return ControlState.SUPERVISED
    if not unit.systemd_unit_present:
        return ControlState.UNKNOWN
    if unit.restart in _SUPERVISING_RESTART:
        return ControlState.SUPERVISED
    if unit.restart == _SHUTDOWN_ONLY_RESTART:
        return ControlState.SHUTDOWN_ONLY
    return ControlState.SYSTEMD
