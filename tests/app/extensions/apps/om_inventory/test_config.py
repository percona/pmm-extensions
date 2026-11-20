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

"""Test the bounds and relationships OmInventorySettings enforces on load."""

from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from pytest_mock import MockerFixture

from app.core.config import YamlPrefixConfigSettingsSource
from app.core.settings_override.registry import is_hot_reloadable
from app.extensions.apps.om_inventory.config import OmInventorySettings


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
RULE_FIELDS = ("CONNECT_TIMEOUT", "REPO_TIMEOUT", "STALE_RUN_AFTER", "TASK_TIMEOUT")

#: Each bounded integer field, with its inclusive range.
BOUNDS = [
    ("REPO_TIMEOUT", 2, 30),
    ("CONNECT_TIMEOUT", 1, 30),
    ("TASK_TIMEOUT", 60, 300),
    ("POLL_INTERVAL", 1, 30),
    ("MAX_CONCURRENT_PROBES", 1, 8),
    ("RUN_RETENTION", 10, 1000),
]


class TestDefaults:
    """Group the checks that the shipped defaults stay valid."""

    def test_the_declared_defaults_load(self) -> None:
        """Load every rule field at its declared default without error."""
        loaded = _load()

        assert {name: getattr(loaded, name) for name in RULE_FIELDS} == {
            name: OmInventorySettings.model_fields[name].default for name in RULE_FIELDS
        }


class TestFieldBounds:
    """Group the checks of each integer field's own range."""

    @pytest.mark.parametrize(("field", "low", "high"), BOUNDS)
    def test_each_end_is_accepted_and_one_past_it_refused(
        self, field: str, low: int, high: int
    ) -> None:
        """Accept each end of the range and refuse one past it, naming that end.

        :param field: The field under test.
        :param low: Its lowest accepted value.
        :param high: Its highest accepted value.
        """
        assert getattr(_load(**{field: low}), field) == low
        assert getattr(_load(**{field: high}), field) == high
        assert f"greater than or equal to {low}" in _refusal(**{field: low - 1})
        assert f"less than or equal to {high}" in _refusal(**{field: high + 1})

    def test_a_stale_threshold_of_a_day_is_accepted(self) -> None:
        """Accept the longest stale-run threshold."""
        assert timedelta(days=1) == _load(STALE_RUN_AFTER=86400).STALE_RUN_AFTER

    def test_a_stale_threshold_over_a_day_is_refused(self) -> None:
        """Refuse a stale-run threshold one second over a day."""
        assert "less than or equal to" in _refusal(STALE_RUN_AFTER=86401)


class TestSchedule:
    """Group the checks of the scan schedule's range."""

    @pytest.mark.parametrize(
        "schedule",
        [
            None,
            {"every": 10, "period": "minutes"},
            {"every": 1440, "period": "minutes"},
            {"every": 24, "period": "hours"},
            {"every": 1, "period": "days"},
        ],
    )
    def test_a_schedule_in_range_is_accepted(
        self, schedule: dict[str, Any] | None
    ) -> None:
        """Accept no schedule, and each end of the range in each period.

        :param schedule: The schedule under test.
        """
        assert _load(SCHEDULE=schedule)

    @pytest.mark.parametrize(
        "schedule",
        [
            {"every": 9, "period": "minutes"},
            {"every": 1441, "period": "minutes"},
            {"every": 25, "period": "hours"},
            {"every": 2, "period": "days"},
            {"every": 10**400, "period": "days"},
        ],
    )
    def test_a_schedule_out_of_range_is_refused(self, schedule: dict[str, Any]) -> None:
        """Refuse a schedule outside the range, naming it.

        :param schedule: The schedule under test.
        """
        assert "from 10 minutes to 1 day" in _refusal(SCHEDULE=schedule)

    @pytest.mark.parametrize("every", [10, 600])
    def test_seconds_are_refused_even_in_range(self, every: int) -> None:
        """Refuse a schedule in seconds, which the periodic-task UI cannot edit.

        :param every: The number of seconds.
        """
        message = _refusal(SCHEDULE={"every": every, "period": "seconds"})

        assert "Invalid period 'seconds'" in message


class TestProbeTimeoutsVersusTaskTimeout:
    """Group the checks of the probe's own timeouts against ``TASK_TIMEOUT``."""

    def test_exactly_half_is_accepted(self) -> None:
        """Accept probe timeouts taking exactly half of the task timeout."""
        assert _load(REPO_TIMEOUT=20, CONNECT_TIMEOUT=10, TASK_TIMEOUT=60)

    def test_one_second_over_half_is_refused_naming_all_three_fields(self) -> None:
        """Refuse probe timeouts one second over half of the task timeout."""
        message = _refusal(REPO_TIMEOUT=21, CONNECT_TIMEOUT=10, TASK_TIMEOUT=60)

        assert "REPO_TIMEOUT, CONNECT_TIMEOUT, TASK_TIMEOUT" in message
        assert "must not exceed half of TASK_TIMEOUT" in message


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
        message = _refusal(
            REPO_TIMEOUT=30, CONNECT_TIMEOUT=30, TASK_TIMEOUT=60, STALE_RUN_AFTER=10
        )

        assert "REPO_TIMEOUT, CONNECT_TIMEOUT, TASK_TIMEOUT" in message
        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in message


class TestEnvironmentLoad:
    """Group the checks that the environment route is guarded as well."""

    def test_an_inconsistent_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse an inconsistent pair coming from environment variables.

        :param monkeypatch: Fixture to set the environment.
        """
        monkeypatch.setenv("EXTENSIONS__OM_INVENTORY__STALE_RUN_AFTER", "PT100S")
        monkeypatch.setenv("EXTENSIONS__OM_INVENTORY__TASK_TIMEOUT", "200")

        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in _refusal()


def _write_timings(path: Path, **timings: int) -> None:
    """Write a settings file holding probe timings under the default section.

    :param path: The settings file to write.
    :param timings: The values to write, in seconds, by field name.
    """
    path.write_text(
        yaml.safe_dump({"default": {"EXTENSIONS": {"OM_INVENTORY": timings}}})
    )


class TestYamlLoad:
    """Group the checks that the YAML route is guarded as well."""

    @pytest.fixture
    def settings_file(self, tmp_path: Path, mocker: MockerFixture) -> Path:
        """Point the YAML source at a temporary settings file.

        The source binds the shipped file as a default argument at import, so the
        class itself is swapped rather than the setting.

        :param tmp_path: Fixture providing a temporary directory.
        :param mocker: Fixture to patch the YAML source.
        :return: The path of the settings file, for the test to write.
        """
        path = tmp_path / "settings.yaml"
        mocker.patch(
            "app.core.config.YamlPrefixConfigSettingsSource",
            partial(YamlPrefixConfigSettingsSource, yaml_file=path),
        )
        return path

    def test_an_inconsistent_settings_file_is_refused(
        self, settings_file: Path
    ) -> None:
        """Refuse an inconsistent pair coming from the settings file.

        :param settings_file: The temporary settings file.
        """
        _write_timings(settings_file, STALE_RUN_AFTER=100, TASK_TIMEOUT=200)

        assert "STALE_RUN_AFTER, TASK_TIMEOUT" in _refusal()

    def test_probe_timeouts_over_half_the_task_timeout_are_refused(
        self, settings_file: Path
    ) -> None:
        """Refuse probe timeouts the settings file sets over half the task timeout.

        :param settings_file: The temporary settings file.
        """
        _write_timings(
            settings_file, REPO_TIMEOUT=21, CONNECT_TIMEOUT=10, TASK_TIMEOUT=60
        )

        assert "REPO_TIMEOUT + CONNECT_TIMEOUT" in _refusal()

    def test_a_consistent_settings_file_loads(self, settings_file: Path) -> None:
        """Load a consistent pair from the settings file, proving it is read.

        :param settings_file: The temporary settings file.
        """
        _write_timings(settings_file, STALE_RUN_AFTER=300, TASK_TIMEOUT=200)

        loaded = _load()

        assert (
            timedelta(seconds=300),
            200,
        ) == (loaded.STALE_RUN_AFTER, loaded.TASK_TIMEOUT)


class TestRefusalLeaksNoValues:
    """Group the checks that a refused load is safe to log."""

    def test_the_whole_error_text_carries_no_value(self) -> None:
        """Keep every input value out of the error text the load failure logs."""
        with pytest.raises(ValidationError) as exc_info:
            _load(POLL_INTERVAL=60061, REPO_URL="https://repo-host-61007.example/")

        assert "60061" not in str(exc_info.value)
        assert "repo-host-61007" not in str(exc_info.value)


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
