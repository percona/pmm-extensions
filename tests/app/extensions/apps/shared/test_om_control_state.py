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

"""Test control-state derivation from a member's unit facts."""

import pytest

from app.extensions.apps.shared.om.control_state import (
    ControlState,
    derive_control_state,
    UnitControl,
)


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        (None, ControlState.SYSTEMD),
        ("no", ControlState.SYSTEMD),
        ("on-abnormal", ControlState.SYSTEMD),
        ("always", ControlState.SUPERVISED),
        ("on-success", ControlState.SUPERVISED),
        ("on-failure", ControlState.SHUTDOWN_ONLY),
    ],
)
def test_restart_directive_maps_to_control_state(
    restart: str | None, expected: ControlState
) -> None:
    """Map each ``Restart=`` value to its control state on a systemd unit.

    :param restart: The unit's ``Restart=`` directive value.
    :param expected: The control state it derives to.
    """
    state = derive_control_state(
        UnitControl(systemd_unit_present=True, restart=restart)
    )
    assert state is expected


def test_on_failure_is_shutdown_only() -> None:
    """Derive ``shutdown_only`` for a ``Restart=on-failure`` unit, not supervised."""
    state = derive_control_state(
        UnitControl(systemd_unit_present=True, restart="on-failure")
    )
    assert state is ControlState.SHUTDOWN_ONLY


def test_an_external_supervisor_overrides_systemd_facts() -> None:
    """Derive ``supervised`` whenever a non-systemd supervisor manages the process."""
    state = derive_control_state(
        UnitControl(systemd_unit_present=True, restart="no", external_supervisor=True)
    )
    assert state is ControlState.SUPERVISED


def test_no_unit_and_no_supervisor_is_unknown() -> None:
    """Derive ``unknown`` when neither systemd nor a supervisor manages the process."""
    state = derive_control_state(UnitControl(systemd_unit_present=False, restart=None))
    assert state is ControlState.UNKNOWN
