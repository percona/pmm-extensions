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

"""Define test cases for the shared celery schedule models."""

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy_celery_beat.models import Period

from app.core.celery.models import CrontabSchedule, IntervalSchedule

#: Five-field cron expressions the scheduler can run.
RUNNABLE_CRONS = [
    pytest.param("0 2 * * *", id="nightly"),
    pytest.param("15 9-17 * * MON-FRI", id="weekday-window"),
    pytest.param("0 2 1 * 5", id="day-of-month-and-day-of-week"),
    pytest.param("*/15 * * * *", id="quarter-hourly"),
]

#: Five-field cron expressions the scheduler cannot run. Each has five fields,
#: so the only thing left to reject them on is the dialect.
UNRUNNABLE_CRONS = [
    pytest.param("not-a-cron 2 * * *", id="malformed-minute"),
    pytest.param("0 2 30 2 *", id="unsatisfiable-30-february"),
    pytest.param("0 99 * * *", id="hour-out-of-range"),
    pytest.param("0 2 * * BADDAY", id="unknown-day-name"),
]


def _accepts_via_json_path(expression: str) -> bool:
    """Report whether the JSON API path accepts ``expression``.

    :param expression: A five-field cron expression.
    :return: ``True`` when :class:`CrontabSchedule` accepts the split fields.
    """
    minute, hour, day_of_month, month_of_year, day_of_week = expression.split()
    try:
        CrontabSchedule(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        )
    except ValidationError:
        return False
    return True


class TestCrontabScheduleValidation:
    """Cover the cron parser behind the JSON input path."""

    @pytest.mark.parametrize("expression", RUNNABLE_CRONS)
    def test_accepts_what_the_scheduler_can_run(self, expression: str) -> None:
        """Assert a runnable expression is accepted at the request boundary."""
        assert _accepts_via_json_path(expression)

    @pytest.mark.parametrize("expression", UNRUNNABLE_CRONS)
    def test_rejects_what_the_scheduler_cannot_run(self, expression: str) -> None:
        """Assert an unrunnable expression is refused at the request boundary."""
        assert not _accepts_via_json_path(expression)

    def test_cron_fields_are_normalised_to_what_beat_stores(self) -> None:
        """Assert whitespace and brackets are stripped as the beat store does."""
        schedule = CrontabSchedule(minute="0, 30", hour="[2]")
        assert schedule.minute == "0,30"
        assert schedule.hour == "2"

    def test_an_invalid_timezone_is_still_rejected(self) -> None:
        """Assert the pre-existing timezone check survives the new validator."""
        with pytest.raises(ValidationError):
            CrontabSchedule(minute="0", hour="2", timezone="Mars/Olympus_Mons")

    @pytest.mark.parametrize(
        ("field", "value"),
        [("minute", "not-a-cron"), ("hour", "99"), ("day_of_week", "BADDAY")],
        ids=["malformed-minute", "hour-out-of-range", "unknown-day-name"],
    )
    def test_a_malformed_field_is_located_at_that_field(
        self, field: str, value: str
    ) -> None:
        """Assert a field the scheduler cannot parse is named by the error.

        The location travels into the 422 a client sees, so a caller can
        highlight the input that carried the bad value.
        """
        with pytest.raises(ValidationError) as excinfo:
            CrontabSchedule(**{field: value})
        assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]

    def test_an_unsatisfiable_combination_is_located_at_the_schedule(self) -> None:
        """Assert a combination no single field makes wrong stays schedule-level.

        Every field of ``0 2 30 2 *`` parses; only 30 February is unreachable,
        so there is no one field the error could name.
        """
        with pytest.raises(ValidationError) as excinfo:
            CrontabSchedule(minute="0", hour="2", day_of_month="30", month_of_year="2")
        assert [error["loc"] for error in excinfo.value.errors()] == [()]


class TestIntervalScheduleSerialisedShape:
    """Pin the serialised shape every settings field annotated with it inherits."""

    def test_schedule_property_is_absent_from_the_dumped_payload(self) -> None:
        """Assert the celery schedule object does not reach ``model_dump``."""
        interval = IntervalSchedule(every=15, period=Period.MINUTES)
        assert set(interval.model_dump()) == {"every", "period"}

    def test_schedule_property_is_absent_from_the_json_schema(self) -> None:
        """Assert the celery schedule object does not reach the JSON schema.

        A ``@computed_field`` here would add it to the serialised shape of every
        settings field annotated with this class; a bare property does not.
        """
        properties = IntervalSchedule.model_json_schema()["properties"]
        assert set(properties) == {"every", "period"}

    def test_schedule_property_still_answers(self) -> None:
        """Assert the property callers use is reachable despite being unexported."""
        interval = IntervalSchedule(every=15, period=Period.MINUTES)
        assert interval.schedule.run_every.total_seconds() == 15 * 60

    def test_a_settings_field_round_trips_unchanged(self) -> None:
        """Assert a model embedding the schedule serialises the same two keys."""

        class _Settings(BaseModel):
            cleanup_interval: IntervalSchedule

        settings = _Settings(
            cleanup_interval=IntervalSchedule(every=15, period=Period.MINUTES)
        )
        assert settings.model_dump() == {
            "cleanup_interval": {"every": 15, "period": Period.MINUTES}
        }
