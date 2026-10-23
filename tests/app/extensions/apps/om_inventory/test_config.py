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

"""Test the probe-timing relationships OmInventorySettings enforces on load."""

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.settings_override.registry import is_hot_reloadable
from app.extensions.apps.om_inventory.config import OmInventorySettings
from tests.app.extensions.apps.om_inventory.conftest import BEYOND_TIMEDELTA_SECONDS


def _load(**values: Any) -> OmInventorySettings:
    """Load the settings with raw values, as YAML or the environment would supply them.

    :param values: Field values to load the settings with.
    :return: The loaded settings.
    """
    return OmInventorySettings(**values)


def _refusal(**values: Any) -> str:
    """Return the message of the load error ``values`` raise.

    :param values: Field values to load the settings with.
    :return: The first error's message.
    """
    with pytest.raises(ValidationError) as exc_info:
        _load(**values)
    return exc_info.value.errors()[0]["msg"]


#: Every field a probe-timing relationship names.
RULE_FIELDS = ("POLL_INTERVAL", "STALE_RUN_AFTER", "TASK_TIMEOUT")


class TestDefaults:
    """Group the checks that the shipped defaults stay valid."""

    def test_the_declared_defaults_load(self) -> None:
        """Load every rule field at its declared default without error."""
        loaded = _load()

        assert {name: getattr(loaded, name) for name in RULE_FIELDS} == {
            name: OmInventorySettings.model_fields[name].default for name in RULE_FIELDS
        }


class TestPollIntervalVersusTaskTimeout:
    """Group the checks of ``POLL_INTERVAL`` against ``TASK_TIMEOUT``."""

    def test_equal_values_are_accepted(self) -> None:
        """Accept a poll interval equal to the timeout, the boundary."""
        assert _load(POLL_INTERVAL=180, TASK_TIMEOUT=180)

    def test_one_second_over_is_refused(self) -> None:
        """Refuse a poll interval one second above the timeout."""
        _refusal(POLL_INTERVAL=181, TASK_TIMEOUT=180)

    def test_a_poll_far_above_the_timeout_is_refused_naming_both_fields(self) -> None:
        """Refuse a poll that overshoots the timeout's first deadline check by 60x."""
        message = _refusal(POLL_INTERVAL=60, TASK_TIMEOUT=1, STALE_RUN_AFTER=600)

        assert "POLL_INTERVAL, TASK_TIMEOUT" in message
        assert "must not exceed" in message


class TestStaleRunAfterVersusTaskTimeout:
    """Group the checks of ``STALE_RUN_AFTER`` against ``TASK_TIMEOUT``."""

    def test_equal_values_are_refused(self) -> None:
        """Refuse a threshold equal to the timeout; it can only reap a live run."""
        _refusal(STALE_RUN_AFTER=180, TASK_TIMEOUT=180)

    def test_one_second_over_is_accepted(self) -> None:
        """Accept a threshold one second above the timeout."""
        assert timedelta(seconds=181) == (
            _load(STALE_RUN_AFTER=181, TASK_TIMEOUT=180).STALE_RUN_AFTER
        )

    @pytest.mark.parametrize("stale", [180.4, 180.5])
    def test_a_margin_the_api_rounds_away_is_refused(self, stale: float) -> None:
        """Refuse a threshold that reads back as the timeout itself.

        :param stale: The threshold, in seconds, against a 180-second timeout.
        """
        _refusal(STALE_RUN_AFTER=stale, TASK_TIMEOUT=180)

    def test_a_threshold_below_the_timeout_is_refused_naming_both_fields(
        self,
    ) -> None:
        """Refuse a one-second threshold against a 180-second timeout."""
        message = _refusal(STALE_RUN_AFTER=1, TASK_TIMEOUT=180)

        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in message
        assert "must exceed" in message

    def test_both_violations_are_reported_together(self) -> None:
        """Name every violated relationship in one refusal."""
        message = _refusal(POLL_INTERVAL=60, TASK_TIMEOUT=30, STALE_RUN_AFTER=10)

        assert "POLL_INTERVAL, TASK_TIMEOUT" in message
        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in message


class TestTimeoutBeyondTheDurationRange:
    """Group the checks of a timeout too large for ``timedelta`` to hold."""

    def test_it_is_refused_as_a_validation_error(self) -> None:
        """Refuse it through the stale-run relationship rather than overflowing."""
        message = _refusal(TASK_TIMEOUT=BEYOND_TIMEDELTA_SECONDS)

        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in message


class TestEnvironmentLoad:
    """Group the checks that the environment route is guarded as well."""

    def test_an_inconsistent_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse an inconsistent pair coming from environment variables.

        :param monkeypatch: Fixture to set the environment.
        """
        monkeypatch.setenv("EXTENSIONS__OM_INVENTORY__POLL_INTERVAL", "60")
        monkeypatch.setenv("EXTENSIONS__OM_INVENTORY__TASK_TIMEOUT", "30")

        assert "POLL_INTERVAL, TASK_TIMEOUT" in _refusal()


class TestUnrelatedTimeoutsStayIndependent:
    """Group the checks that only the two named relationships can refuse."""

    @pytest.mark.parametrize("field", ["REPO_TIMEOUT", "CONNECT_TIMEOUT"])
    def test_a_timeout_above_the_task_timeout_is_accepted(self, field: str) -> None:
        """Accept a probe-internal timeout above ``TASK_TIMEOUT``.

        :param field: The probe-internal timeout under test.
        """
        assert _load(**{field: 600, "TASK_TIMEOUT": 180})


class TestRuleDeclarations:
    """Group the checks of the rules' own declaration."""

    def test_every_rule_field_is_a_hot_field_of_the_class(self) -> None:
        """Name only real, API-settable fields so no rule silently goes unchecked."""
        named = {
            name
            for rule in OmInventorySettings.CROSS_FIELD_RULES
            for name in rule.fields
        }

        assert named == set(RULE_FIELDS)
        assert all(is_hot_reloadable(OmInventorySettings, name) for name in named)
