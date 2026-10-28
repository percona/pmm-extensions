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

"""Define test cases for periodic task models."""

import json
from datetime import datetime, timedelta, UTC

import pytest
from pydantic import ValidationError
from sqlalchemy_celery_beat.models import Period

from app.core.celery.models import CrontabSchedule, IntervalSchedule
from app.core.celery.schedules import INTERVAL_TIMEZONE, NEXT_RUNS_PREVIEW_COUNT
from app.tasks.periodic.models import (
    BasePeriodicTask,
    PeriodicTaskCreate,
    PeriodicTaskExecuteRequest,
    PeriodicTaskResponse,
    PeriodicTaskUpdate,
    PeriodicTaskWrite,
)
from tests.app.factories import PeriodicTaskFactory

#: ``datetime.isoweekday()`` for Friday, the day cron's ``day_of_week=5`` names.
FRIDAY = 5


def _periodic_response_data(**overrides):
    """Build a ``PeriodicTaskResponse`` validate-input dict from the factory plus overrides.

    ``PeriodicTaskFactory`` is a SQLAlchemy factory, so it yields a ``PeriodicTask``
    ORM instance; this projects its attributes into the dict shape
    ``PeriodicTaskResponse.model_validate`` expects. ``args`` / ``kwargs`` /
    ``model_intervalschedule`` are supplied per-test because they ARE the assertion
    target (the factory's random ``args`` / ``kwargs`` are not valid JSON, the
    datetime fields default to ``None``, and ``description`` is pinned to ``""``
    because its ORM column is nullable while the response requires a ``str``); the
    factory backfills the remaining fields so a ``PeriodicTask`` rename can't leave
    these tests green.

    :param overrides: Per-test field overrides merged over the factory-derived base.
    :type overrides: object
    :return: A dict ready for ``PeriodicTaskResponse.model_validate``.
    :rtype: dict
    """
    pt = PeriodicTaskFactory.build()
    base = {
        "id": pt.id,
        "name": pt.name,
        "task": pt.task,
        "start_time": None,
        "enabled": pt.enabled,
        "description": "",
        "last_run_at": None,
        "total_run_count": pt.total_run_count,
        "date_changed": None,
        "args": "[]",
        "kwargs": "{}",
    }
    return {**base, **overrides}


class TestPeriodicTaskExecuteRequest:
    """Test the PeriodicTaskExecuteRequest model."""

    def test_eta_forced_to_none(self):
        """Assert eta is always forced to None regardless of input."""
        req = PeriodicTaskExecuteRequest(
            eta=datetime(2025, 1, 1, tzinfo=UTC),
            meta={},
        )
        assert req.eta is None

    def test_eta_none_stays_none(self):
        """Assert eta None input remains None."""
        req = PeriodicTaskExecuteRequest(eta=None, meta={})
        assert req.eta is None

    def test_eta_empty_string_forced_to_none(self):
        """Assert empty string eta is forced to None."""
        req = PeriodicTaskExecuteRequest(eta="", meta={})
        assert req.eta is None

    @pytest.mark.parametrize(
        "meta_value",
        ["oops", ["a"], None],
        ids=["string", "list", "null"],
    )
    def test_populate_meta_rejects_non_mapping(self, meta_value):
        """Assert non-mapping meta alongside a meta_ key raises ValidationError."""
        with pytest.raises(ValidationError):
            PeriodicTaskExecuteRequest.model_validate({"meta": meta_value, "meta_x": 1})

    def test_execute_request_rejects_invalid_requirements(self):
        """Assert building the execute request directly stays strict."""
        with pytest.raises(ValidationError):
            PeriodicTaskExecuteRequest(meta={"requirements": ["a", ""]})

    def test_create_rejects_invalid_requirements(self):
        """Assert a schedule write still rejects an invalid requirements value."""
        with pytest.raises(ValidationError):
            PeriodicTaskCreate.model_validate(
                {
                    "name": "n",
                    "task": "t",
                    "start_time": None,
                    "enabled": True,
                    "description": "",
                    "execute_request": {"meta": {"requirements": ["a", ""]}},
                    "interval": {"every": 10, "period": "minutes"},
                }
            )


class TestBasePeriodicTask:
    """Test the BasePeriodicTask model."""

    @pytest.fixture
    def base_fields(self):
        """Provide shared base fields for building BasePeriodicTask instances."""
        return {
            "name": "test-task",
            "task": "celery.task",
            "start_time": None,
            "enabled": True,
            "description": "desc",
        }

    def test_period_with_interval(self, base_fields):
        """Assert period returns the string representation of the interval."""
        interval = IntervalSchedule(every=5, period=Period.HOURS)
        task = BasePeriodicTask(**base_fields, interval=interval)
        assert task.period == str(interval)

    def test_period_with_crontab(self, base_fields):
        """Assert period returns the string representation of the crontab."""
        crontab = CrontabSchedule(minute="0", hour="12")
        task = BasePeriodicTask(**base_fields, crontab=crontab)
        assert task.period == str(crontab)

    def test_validate_both_none_raises(self, base_fields):
        """Assert ValueError when both interval and crontab are None."""
        with pytest.raises(ValidationError, match="Either `interval` or `crontab`"):
            BasePeriodicTask(**base_fields)

    def test_validate_both_set_raises(self, base_fields):
        """Assert ValueError when both interval and crontab are set."""
        with pytest.raises(ValidationError, match="Only one of"):
            BasePeriodicTask(
                **base_fields,
                interval=IntervalSchedule(every=1, period=Period.HOURS),
                crontab=CrontabSchedule(),
            )

    def test_validate_exactly_one_passes(self, base_fields):
        """Assert validation passes when exactly one schedule is set."""
        task = BasePeriodicTask(
            **base_fields,
            interval=IntervalSchedule(every=1, period=Period.HOURS),
        )
        assert task.interval is not None
        assert task.crontab is None


class TestPeriodicTaskResponse:
    """Test the PeriodicTaskResponse model."""

    def test_populate_task_data_from_dict_with_kwargs(self):
        """Assert task data is populated from kwargs in a dict."""
        data = _periodic_response_data(
            kwargs=json.dumps(
                {"task_name": "my-backup", "execution_data": {"meta": {}}}
            ),
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        response = PeriodicTaskResponse.model_validate(data)
        assert response.task == "my-backup"
        assert response.execute_request is not None
        assert response.execute_request.meta == {}

    def test_populate_task_data_from_dict_with_args(self):
        """Assert task data is populated from positional args in a dict."""
        data = _periodic_response_data(
            args=json.dumps(["my-backup-task", {"meta": {"key": "val"}}]),
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        response = PeriodicTaskResponse.model_validate(data)
        assert response.task == "my-backup-task"
        assert response.execute_request is not None
        assert response.execute_request.meta == {"key": "val"}

    def test_populate_task_data_kwargs_overrides_args(self):
        """Assert kwargs take precedence over args for task_name."""
        data = _periodic_response_data(
            args=json.dumps(["args-task"]),
            kwargs=json.dumps({"task_name": "kwargs-task"}),
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        response = PeriodicTaskResponse.model_validate(data)
        assert response.task == "kwargs-task"

    def test_populate_task_data_empty_args_and_kwargs_raises(self):
        """Assert ValidationError when both args and kwargs are empty."""
        data = _periodic_response_data(
            args="[]",
            kwargs="{}",
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        with pytest.raises(ValidationError, match="task"):
            PeriodicTaskResponse.model_validate(data)

    @pytest.mark.parametrize(
        "requirements",
        [["PyMySQL", ""], None, 5],
        ids=["empty-string-item", "null", "number"],
    )
    def test_stored_legacy_requirements_stay_readable(self, requirements):
        """Assert a schedule stored with a legacy requirements value still loads."""
        data = _periodic_response_data(
            args=json.dumps(
                [
                    "my-backup-task",
                    {"meta": {"requirements": requirements}, "eta": "2030-01-01"},
                ]
            ),
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        execute_request = PeriodicTaskResponse.model_validate(data).execute_request
        assert execute_request is not None
        assert execute_request.meta == {"requirements": requirements}
        assert execute_request.eta is None

    def test_stored_list_requirements_read_back_joined(self):
        """Assert a valid stored list is still normalized to a string."""
        data = _periodic_response_data(
            args=json.dumps(["t", {"meta": {"requirements": ["a", "b"]}}]),
            model_intervalschedule=IntervalSchedule(every=1, period=Period.HOURS),
        )
        execute_request = PeriodicTaskResponse.model_validate(data).execute_request
        assert execute_request is not None
        assert execute_request.meta == {"requirements": "a\nb"}


class TestPeriodicTaskWrite:
    """Test the PeriodicTaskWrite model."""

    def test_populate_celery_task_data(self):
        """Assert celery task name and kwargs are populated from input."""
        data = {
            "name": "test-write",
            "task": "my-extensions-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "execute_request": {"meta": {}},
            "interval": {"every": 10, "period": "minutes"},
        }
        task = PeriodicTaskWrite.model_validate(data)
        assert task.task == "app.tasks.celery.execute_task_by_name"
        parsed_kwargs = json.loads(task.kwargs)
        assert parsed_kwargs["task_name"] == "my-extensions-task"
        assert parsed_kwargs["execution_data"] == {"meta": {}}

    def test_encode_kwargs_dict_to_json(self):
        """Assert dict kwargs are encoded to JSON string."""
        data = {
            "name": "test",
            "task": "my-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "interval": {"every": 1, "period": "hours"},
        }
        task = PeriodicTaskWrite.model_validate(data)
        assert isinstance(task.kwargs, str)
        parsed = json.loads(task.kwargs)
        assert isinstance(parsed, dict)

    def test_encode_kwargs_string_passthrough(self):
        """Assert string kwargs pass through the field validator unchanged."""
        task = PeriodicTaskWrite.model_validate(
            {
                "name": "test",
                "task": "my-task",
                "start_time": None,
                "enabled": True,
                "description": "",
                "interval": {"every": 1, "period": "hours"},
            }
        )
        assert isinstance(task.kwargs, str)
        parsed = json.loads(task.kwargs)
        assert "task_name" in parsed

    def test_validate_min_interval_valid_periods(self):
        """Assert valid periods (DAYS, HOURS, MINUTES) pass validation."""
        for period in (Period.DAYS, Period.HOURS, Period.MINUTES):
            data = {
                "name": "test",
                "task": "my-task",
                "start_time": None,
                "enabled": True,
                "description": "",
                "interval": {"every": 1, "period": period.value},
            }
            task = PeriodicTaskWrite.model_validate(data)
            assert task.interval.period == period

    def test_validate_min_interval_invalid_period_raises(self):
        """Assert ValueError for SECONDS period."""
        data = {
            "name": "test",
            "task": "my-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "interval": {"every": 1, "period": "seconds"},
        }
        with pytest.raises(ValidationError, match="Invalid period"):
            PeriodicTaskWrite.model_validate(data)

    def test_validate_min_interval_microseconds_raises(self):
        """Assert ValueError for MICROSECONDS period."""
        data = {
            "name": "test",
            "task": "my-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "interval": {"every": 1, "period": "microseconds"},
        }
        with pytest.raises(ValidationError, match="Invalid period"):
            PeriodicTaskWrite.model_validate(data)


class TestPeriodicTaskUpdate:
    """Test the PeriodicTaskUpdate model."""

    def test_encode_kwargs_missing_task_name_raises(self):
        """Assert ValueError when task_name is None in kwargs."""
        with pytest.raises(ValidationError, match="task_name"):
            PeriodicTaskUpdate.model_validate(
                {
                    "name": "test",
                    "task": None,
                    "start_time": None,
                    "enabled": True,
                    "description": "",
                    "interval": {"every": 1, "period": "hours"},
                }
            )

    def test_encode_kwargs_with_task_name_passes(self):
        """Assert kwargs with task_name passes validation."""
        task = PeriodicTaskUpdate.model_validate(
            {
                "name": "test",
                "task": "my-task",
                "start_time": None,
                "enabled": True,
                "description": "",
                "interval": {"every": 1, "period": "hours"},
                "kwargs": {"task_name": "my-task", "execution_data": None},
            }
        )
        parsed = json.loads(task.kwargs)
        assert parsed["task_name"] == "my-task"


class TestPeriodicTaskCreate:
    """Test the PeriodicTaskCreate model."""

    def test_defaults(self):
        """Assert default values for PeriodicTaskCreate."""
        task = PeriodicTaskCreate.model_validate(
            {
                "task": "my-task",
                "interval": {"every": 1, "period": "hours"},
            }
        )
        assert task.name == ""
        assert task.start_time is None
        assert task.enabled is True
        assert task.description == ""


class TestPeriodicTaskResponseScheduleFields:
    """Cover the schedule computed fields on PeriodicTaskResponse."""

    @pytest.fixture
    def response_fields(self):
        """Provide shared fields for building PeriodicTaskResponse instances."""
        return {
            "id": 1,
            "name": "test",
            "task": "original-task",
            "start_time": None,
            "enabled": True,
            "description": "",
            "last_run_at": None,
            "total_run_count": 0,
            "date_changed": None,
            "args": json.dumps(["my-task"]),
            "kwargs": "{}",
        }

    def test_crontab_next_run_is_future_utc(self, response_fields):
        """Assert next_run_at returns a future UTC datetime for crontab tasks."""
        response_fields["model_crontabschedule"] = CrontabSchedule(
            minute="0", hour="*/1"
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at is not None
        assert response.next_run_at > datetime.now(UTC)
        assert response.next_run_at.tzinfo is not None

    def test_crontab_non_utc_timezone_returns_utc(self, response_fields):
        """Assert next_run_at converts non-UTC crontab timezone to UTC."""
        response_fields["model_crontabschedule"] = CrontabSchedule(
            minute="0", hour="*/1", timezone="America/New_York"
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at is not None
        assert response.next_run_at.tzinfo == UTC

    def test_interval_next_run_with_long_past_last_run_at(self, response_fields):
        """Assert a schedule that has missed runs previews its next one now.

        Beat fires such a schedule immediately rather than at
        ``last_run_at + interval``, which is already in the past.
        """
        response_fields["last_run_at"] = datetime.now(UTC) - timedelta(days=180)
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=5, period=Period.HOURS
        )
        before = datetime.now(UTC)
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at is not None
        assert abs((response.next_run_at - before).total_seconds()) < 1

    def test_interval_next_run_is_start_time_when_start_time_is_future(
        self, response_fields
    ):
        """Assert a future start_time is itself the first run, not start + interval.

        ``ModelEntry.__init__`` back-dates an unset ``last_run_at`` so the first
        run lands at ``start_time``.
        """
        start = datetime.now(UTC) + timedelta(days=7)
        response_fields["start_time"] = start
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=30, period=Period.MINUTES
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at == start

    def test_crontab_next_run_is_start_time_when_start_time_is_future(
        self, response_fields
    ):
        """Assert a cron schedule previews no run before a future start_time."""
        start = datetime.now(UTC) + timedelta(days=7)
        response_fields["start_time"] = start
        response_fields["model_crontabschedule"] = CrontabSchedule(minute="0", hour="2")
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at == start
        assert all(run >= start for run in response.next_runs)

    def test_interval_next_run_falls_back_to_now(self, response_fields):
        """Assert next_run_at is close to now + interval when no base time exists."""
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=2, period=Period.DAYS
        )
        before = datetime.now(UTC)
        response = PeriodicTaskResponse.model_validate(response_fields)
        expected = before + timedelta(days=2)
        assert response.next_run_at is not None
        assert abs((response.next_run_at - expected).total_seconds()) < 1

    def test_disabled_task_returns_none(self, response_fields):
        """Assert next_run_at is None and next_runs is empty for disabled tasks."""
        response_fields["enabled"] = False
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=1, period=Period.HOURS
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at is None
        assert response.next_runs == []

    def test_crontab_next_run_matches_the_scheduler_not_croniter(self, response_fields):
        """Assert a dom-and-dow expression takes the scheduler's intersection.

        ``0 2 1 * 5`` is how "the first Friday of the month" is spelled. croniter
        unions the two restricted fields and would report 2026-09-01; beat
        intersects them and fires on 2027-01-01, four months later.
        """
        response_fields["model_crontabschedule"] = CrontabSchedule(
            minute="0", hour="2", day_of_month="1", day_of_week="5"
        )
        response_fields["last_run_at"] = datetime(2026, 9, 1, tzinfo=UTC)
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.next_run_at is not None
        assert response.next_run_at.day == 1
        assert response.next_run_at.isoweekday() == FRIDAY

    def test_next_runs_is_ordered_and_leads_with_next_run_at(self, response_fields):
        """Assert next_runs is a strictly increasing UTC list headed by next_run_at."""
        response_fields["model_crontabschedule"] = CrontabSchedule(
            minute="0", hour="*/1"
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert len(response.next_runs) == NEXT_RUNS_PREVIEW_COUNT
        assert response.next_runs == sorted(response.next_runs)
        assert len(set(response.next_runs)) == NEXT_RUNS_PREVIEW_COUNT
        assert all(run.tzinfo == UTC for run in response.next_runs)
        assert response.next_run_at == response.next_runs[0]

    def test_interval_timezone_is_utc_without_start_time(self, response_fields):
        """Assert an interval schedule reports UTC even with no start_time."""
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=2, period=Period.DAYS
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.start_time is None
        assert response.timezone == INTERVAL_TIMEZONE

    def test_crontab_timezone_is_the_crontab_zone(self, response_fields):
        """Assert a crontab schedule reports the zone it is defined in."""
        response_fields["model_crontabschedule"] = CrontabSchedule(
            minute="0", hour="2", timezone="Europe/Lisbon"
        )
        response = PeriodicTaskResponse.model_validate(response_fields)
        assert response.timezone == "Europe/Lisbon"

    def test_schedule_fields_are_serialised(self, response_fields):
        """Assert the new fields reach the dumped payload, not just the instance."""
        response_fields["model_intervalschedule"] = IntervalSchedule(
            every=2, period=Period.DAYS
        )
        dumped = PeriodicTaskResponse.model_validate(response_fields).model_dump()
        assert dumped["timezone"] == INTERVAL_TIMEZONE
        assert len(dumped["next_runs"]) == NEXT_RUNS_PREVIEW_COUNT
        assert dumped["next_run_at"] == dumped["next_runs"][0]
