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

"""Define models for periodic tasks in the Tasks app."""

import json
from collections.abc import Mapping
from datetime import datetime
from functools import cached_property
from typing import Any, Self

from pydantic import (
    BaseModel,
    computed_field,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy_celery_beat.models import PeriodicTask

from app.core.celery.models import (
    CrontabSchedule,
    IntervalSchedule,
    ManageableInterval,
)
from app.core.celery.schedules import (
    next_run_times,
    schedule_timezone,
)
from app.core.utils.fields import EmptyStrToNone, UTCDatetime
from app.tasks.models import (
    STORED_REQUEST_CONTEXT,
    TaskExecuteRequest,
    TaskHistoryStatusEnum,
)


def resolve_task_name(
    args: list[Any] | None, kwargs: dict[str, Any] | None
) -> str | None:
    """Return the PMM Extensions task name carried by a beat schedule's decoded fields.

    Apply the single rule every caller shares: the name is ``args[0]``,
    overridden by ``kwargs["task_name"]`` when that key is present.

    :param args: The schedule's decoded positional ``args``, or ``None``.
    :param kwargs: The schedule's decoded ``kwargs``, or ``None``.
    :return: The resolved PMM Extensions task name, or ``None`` when it cannot be derived.
    """
    name = args[0] if args else None
    if kwargs and "task_name" in kwargs:
        name = kwargs["task_name"]
    return name


class PeriodicTaskExecuteRequest(TaskExecuteRequest):
    """Represent the execute request the periodic task will use for executions.

    :param meta: A dictionary of meta variables for the task execution.
        Defaults to an empty dictionary.
    :type meta: dict[str, Any]
    :param payload: Optional payload data or file path for the task execution.
        Defaults to None.
    :type payload: str | None
    :param eta: The earliest time the task can be executed. Forced to None, as periodic
        tasks are always executed on the defined schedule.
    :type eta: datetime | None
    """

    @field_validator("eta")
    @classmethod
    def _force_eta_to_none(cls, _: datetime | EmptyStrToNone) -> None:
        """Force field eta to None."""
        return


def reject_schedule_that_cannot_produce_runs(
    interval: IntervalSchedule | None,
    crontab: CrontabSchedule | None,
    start_time: datetime | None,
) -> None:
    """Reject a schedule whose upcoming runs fall outside the datetime range.

    ``every`` is unbounded and ``start_time`` reaches ``datetime.max``, so a
    large enough cadence, or one anchored close enough to the end of the
    representable range, overflows while the runs are being computed. Those runs
    are reported by computed fields, so the overflow would answer a read with a
    500 rather than a validation error, and a stored row of that shape fails the
    whole list endpoint rather than only its own detail response.

    Checks the schedule and its anchor together, and runs the real computation:
    the reachable ceiling depends on how far past ``start_time`` the last
    reported run lands, so neither input bounds it alone.

    :param interval: The interval schedule, if set.
    :param crontab: The crontab schedule, if set.
    :param start_time: The earliest time the schedule may fire, if any.
    :raises ValueError: If computing the upcoming runs overflows.
    """
    try:
        next_run_times(interval=interval, crontab=crontab, start_time=start_time)
    except OverflowError as exc:
        raise ValueError(
            "Schedule cannot be scheduled: its next runs fall outside the"
            " representable date range."
        ) from exc


def reject_unless_one_schedule_is_set(
    interval: IntervalSchedule | None, crontab: CrontabSchedule | None
) -> None:
    """Reject a schedule that sets both scheduling methods, or neither.

    :param interval: The interval schedule, if set.
    :param crontab: The crontab schedule, if set.
    :raises ValueError: If both or neither scheduling methods are set.
    """
    if interval is None and crontab is None:
        raise ValueError("Either `interval` or `crontab` must be set.")
    if interval is not None and crontab is not None:
        raise ValueError("Only one of `interval` or `crontab` can be set.")


class BasePeriodicTask(BaseModel):
    """Define the base model for periodic tasks.

    :param name: The name of the periodic task.
    :param task: The task identifier.
    :param start_time: The start time for the task execution.
    :param enabled: Whether the task is enabled.
    :param description: A description of the task.
    :param execute_request: The execution request details for the task.
    :param interval: The interval schedule for the task. Defaults to None.
    :param crontab: The crontab schedule for the task. Defaults to None.
    """

    name: str
    task: str
    start_time: UTCDatetime | None
    enabled: bool
    description: str
    execute_request: PeriodicTaskExecuteRequest | None = None
    interval: IntervalSchedule | None = None
    crontab: CrontabSchedule | None = None

    @computed_field
    @property
    def period(self) -> str:
        """Get the period string for the periodic task.

        Returns a string representation of the task's schedule based on whether it uses
        an interval or crontab schedule.

        :return: A string representing the task's period.
        """
        if self.interval is not None:
            return str(self.interval)
        return str(self.crontab)

    @model_validator(mode="after")
    def validate_one_schedule_is_set(self) -> Self:
        """Ensure that exactly one scheduling method is set.

        Validates that either `interval` or `crontab` is set, but not both.

        :return: The validated BasePeriodicTask instance.
        :rtype: Self
        :raises ValueError: If both or neither scheduling methods are set.
        """
        reject_unless_one_schedule_is_set(self.interval, self.crontab)
        return self


class PeriodicTaskResponse(BasePeriodicTask):
    """Represent a response for a periodic task API call.

    This model extends `BasePeriodicTask` and includes additional fields such as ID,
    last run time, total run count, and date changed.

    :param name: The name of the periodic task.
    :param task: The PMM Extensions task name.
    :param start_time: The start time for the task execution.
    :param enabled: Whether the task is enabled.
    :param description: A description of the task.
    :param execute_request: The execution request details for the task.
    :param id: The unique identifier of the periodic task.
    :param last_run_at: The datetime of the last run.
    :param total_run_count: The total number of times the task has run.
    :param date_changed: The datetime when the task was last changed.
    :param last_run_status: The result of this schedule's own most recent
        run, or ``None`` when the schedule has never run. Resolved as the
        earliest system-triggered history for this task name at or after the
        schedule's ``last_run_at``, so a later unrelated system run of the same
        task name is not misattributed.
    :param interval: The interval schedule for the task. Defaults to None. This field
        is populated with the alias "model_intervalschedule".
    :param crontab: The crontab schedule for the task. Defaults to None. This field
        is populated with the alias "model_crontabschedule".
    """

    id: int
    last_run_at: UTCDatetime | None
    last_run_status: TaskHistoryStatusEnum | None = None
    total_run_count: int = 0
    date_changed: UTCDatetime | None
    interval: IntervalSchedule | None = Field(
        None, validation_alias="model_intervalschedule"
    )
    crontab: CrontabSchedule | None = Field(
        None, validation_alias="model_crontabschedule"
    )

    @computed_field
    @cached_property
    def next_runs(self) -> list[UTCDatetime]:
        """Compute the next scheduled execution times.

        Report what Celery beat will do with this schedule, computed through the
        scheduler's own schedule objects, so the API's account of the task and
        the scheduler's behaviour cannot disagree.

        Cached per instance so a page of schedules computes each row once rather
        than once per computed field reading it.

        :return: Up to :data:`~app.core.celery.schedules.NEXT_RUNS_PREVIEW_COUNT`
            UTC execution times, empty when the task is disabled.
        """
        return next_run_times(
            interval=self.interval,
            crontab=self.crontab,
            start_time=self.start_time,
            last_run_at=self.last_run_at,
            enabled=self.enabled,
        )

    @computed_field
    @property
    def next_run_at(self) -> UTCDatetime | None:
        """Compute the next scheduled execution time.

        :return: The first of :attr:`next_runs`, or ``None`` when the task is
            disabled.
        """
        return self.next_runs[0] if self.next_runs else None

    @computed_field
    @property
    def timezone(self) -> str:
        """Report the zone this task's schedule is defined in.

        For a crontab schedule this is the crontab's own zone. For an interval
        schedule it is ``UTC`` unconditionally, ``start_time`` set or not: an
        interval's cadence is an absolute ``timedelta`` with no wall-clock
        anchor, and ``start_time`` is a ``UTCDatetime``, which coerces away
        whatever zone the client sent, so neither could carry another zone.

        :return: The IANA zone name the schedule is defined in.
        """
        return schedule_timezone(self.interval, self.crontab)

    @field_validator("execute_request", mode="before")
    @classmethod
    def _read_stored_execute_request(cls, v: Any) -> Any:
        """Build a mapping-form execute request as a read of a stored schedule.

        A response is rebuilt from the beat row's stored arguments, so the
        request is validated with :data:`~app.tasks.models.STORED_REQUEST_CONTEXT`:
        a legacy ``meta.requirements`` stays readable. Writes use other models
        and stay strict.

        :param v: The raw ``execute_request`` input.
        :return: The built request for a mapping, otherwise the input unchanged.
        """
        if isinstance(v, Mapping):
            return PeriodicTaskExecuteRequest.model_validate(
                v, context=STORED_REQUEST_CONTEXT
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def populate_task_data(cls, data: Any) -> Any:
        """Populate task data from a PeriodicTask instance or dictionary.

        Extracts task execution details from the provided data, which can be a
        `PeriodicTask` instance or a dictionary, and sets the `task` and
        `execute_request` fields.

        :param data: The input data containing task execution details.
        :return: The modified data with populated task fields.
        """
        if isinstance(data, PeriodicTask):
            data = data.__dict__
        if isinstance(data, dict):
            extra_kwargs = {
                "task": None,
                "execute_request": None,
            }
            raw_args = data.get("args")
            raw_kwargs = data.get("kwargs")
            args = json.loads(raw_args) if raw_args else None
            kwargs = json.loads(raw_kwargs) if raw_kwargs else None
            extra_kwargs["task"] = resolve_task_name(args, kwargs)
            if args and len(args) > 1:
                extra_kwargs["execute_request"] = args[1]
            if kwargs:
                extra_kwargs["execute_request"] = kwargs.get(
                    "execution_data", extra_kwargs["execute_request"]
                )
            return data | extra_kwargs
        return data


class PeriodicTaskWrite(BasePeriodicTask):
    """Define the model for writing periodic tasks.

    This model extends `BasePeriodicTask` and includes additional fields required for
    creating or updating periodic tasks in the database.

    :param name: The name of the periodic task.
    :param task: The Celery task name.
    :param start_time: The start time for the task execution.
    :param enabled: Whether the task is enabled.
    :param description: A description of the task.
    :param execute_request: The execution request details for the task.
    :param interval: The interval schedule for the task. Narrowed to the periods
        an operator can also manage from the UI. Defaults to None.
    :param crontab: The crontab schedule for the task. Defaults to None.
    :param kwargs: A JSON string representing additional keyword arguments for the task.
    """

    kwargs: str
    interval: ManageableInterval | None = None

    @model_validator(mode="before")
    @classmethod
    def populate_celery_task_data(cls, data: Any) -> Any:
        """Populate Celery task data before validation.

        Transforms the input data to include the Celery task name and execution data.
        For updates, only includes execute_request if it was explicitly set.

        :param data: The input data containing task details.
        :type data: Any
        :return: The modified data with Celery task information.
        :rtype: Any
        """
        if isinstance(data, dict):
            extra_data = {
                "task": "app.tasks.celery.execute_task_by_name",
                "kwargs": {
                    "task_name": data.get("task"),
                },
            }
            if "execute_request" in data:
                extra_data["kwargs"]["execution_data"] = data.get("execute_request")
            return data | extra_data
        return data

    @field_validator("kwargs", mode="before")
    @classmethod
    def encode_kwargs(cls, v: Any) -> Any:
        """Encode the kwargs field to a JSON string.

        Converts the `kwargs` dictionary to a JSON string if it is a dictionary.

        :param v: The kwargs value to encode.
        :return: The encoded kwargs as a JSON string.
        """
        if isinstance(v, dict):
            return json.dumps(v)
        return v

    @model_validator(mode="after")
    def validate_schedule_can_produce_runs(self) -> Self:
        """Ensure the stored schedule will not overflow when its runs are read.

        :return: The validated instance.
        :raises ValueError: If computing the upcoming runs overflows.
        """
        reject_schedule_that_cannot_produce_runs(
            self.interval, self.crontab, self.start_time
        )
        return self


class PeriodicTaskUpdate(PeriodicTaskWrite):
    """Define the model for updating periodic tasks.

    Extends `PeriodicTaskWrite` and adds validations specific to updating tasks.

    :param name: The name of the periodic task.
    :param task: The Celery task name.
    :param start_time: The start time for the task execution.
    :param enabled: Whether the task is enabled.
    :param description: A description of the task.
    :param execute_request: The execution request details for the task.
    :param interval: The interval schedule for the task. Defaults to None.
    :param crontab: The crontab schedule for the task. Defaults to None.
    :param kwargs: A JSON string representing additional keyword arguments for the task.
    """

    @field_validator("kwargs", mode="before")
    @classmethod
    def encode_kwargs(cls, v: Any) -> Any:
        """Encode the kwargs field and ensure required fields are present.

        Converts the `kwargs` dictionary to a JSON string and validates the presence
        of the 'task_name' field.

        :param v: The kwargs value to encode.
        :return: The encoded kwargs as a JSON string.
        :raises ValueError: If 'task_name' is missing in kwargs.
        """
        if isinstance(v, dict):
            if v.get("task_name") is None:
                raise ValueError("Missing required field 'task_name'")
            return json.dumps(v)
        return v


class PeriodicTaskCreate(PeriodicTaskWrite):
    """Define the model for creating periodic tasks.

    Extends `PeriodicTaskWrite` and includes additional fields required for creating
    new periodic tasks.

    :param task: The Celery task name.
    :param execute_request: The execution request details for the task.
    :param interval: The interval schedule for the task. Defaults to None.
    :param crontab: The crontab schedule for the task. Defaults to None.
    :param kwargs: A JSON string representing additional keyword arguments for the task.
    :param name: The name of the periodic task. Defaults to an empty string, meaning
        the value will be automatically generated on create.
    :param start_time: The start time for the task execution. Defaults to None.
    :param enabled: Whether the task is enabled. Defaults to True.
    :param description: A description of the task. Defaults to an empty string.
    """

    name: str = ""
    start_time: UTCDatetime | None = None
    enabled: bool = True
    description: str = ""


class SchedulePreviewWrite(BaseModel):
    """Define the schedule a preview is requested for.

    Carries the schedule fields of a create request and nothing else: a preview
    persists nothing, so it needs no task name, owner, or execution details.

    :param interval: The interval schedule to preview. Defaults to None.
    :param crontab: The crontab schedule to preview. Defaults to None.
    :param start_time: The earliest time the schedule may fire. Defaults to None.
    """

    interval: ManageableInterval | None = None
    crontab: CrontabSchedule | None = None
    start_time: UTCDatetime | None = None

    @model_validator(mode="after")
    def validate_one_schedule_is_set(self) -> Self:
        """Ensure that exactly one scheduling method is set.

        :return: The validated SchedulePreviewWrite instance.
        :raises ValueError: If both or neither scheduling methods are set, or if
            computing the upcoming runs overflows.
        """
        reject_unless_one_schedule_is_set(self.interval, self.crontab)
        reject_schedule_that_cannot_produce_runs(
            self.interval, self.crontab, self.start_time
        )
        return self


class SchedulePreviewResponse(BaseModel):
    """Represent the upcoming runs of a schedule that has not been saved.

    Uses the same field names and shapes as the matching computed fields on
    :class:`PeriodicTaskResponse`, so a client renders a preview and a saved
    schedule through one code path.

    :param timezone: The zone the schedule is defined in.
    :param next_run_at: The first upcoming run, or ``None`` when there is none.
    :param next_runs: The upcoming runs, empty when there are none.
    """

    timezone: str
    next_run_at: UTCDatetime | None
    next_runs: list[UTCDatetime]
