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

"""Define models for Celery periodic tasks and schedules."""

from datetime import timedelta
from typing import Annotated, Any, Self
from zoneinfo import available_timezones, ZoneInfo

from celery import schedules as celery_schedules
from pydantic import (
    AfterValidator,
    BaseModel,
    field_validator,
    model_validator,
    PositiveInt,
    ValidationInfo,
)
from sqlalchemy_celery_beat import CrontabSchedule as BaseCrontabSchedule
from sqlalchemy_celery_beat.models import Period
from sqlalchemy_celery_beat.tzcrontab import TzAwareCrontab

from app.core.utils.date_time import utc_now

#: The cron fields ``sqlalchemy_celery_beat`` normalises and parses before it
#: writes a crontab row.
CRON_FIELDS = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")


class IntervalSchedule(BaseModel):
    """Represent an interval schedule.

    :param every: The number of periods between each execution.
    :type every: PositiveInt
    :param period: The period unit for the interval (e.g., hours, minutes).
    :type period: Period
    """

    every: PositiveInt
    period: Period

    @model_validator(mode="before")
    @classmethod
    def create_from_str(cls, data: Any) -> Any:
        """Create an IntervalSchedule instance from a string.

        Converts a string representation of an interval schedule into an
        `IntervalSchedule` instance.

        :param data: The input data containing the interval schedule.
        :type data: Any
        :return: The validated data.
        :rtype: Any
        """
        if isinstance(data, str):
            data = data.lower()
            data = data.removeprefix("every ")
            every, period = data.strip().split(maxsplit=1)
            return {
                "every": every,
                "period": period,
            }
        return data

    @property
    def schedule(self) -> celery_schedules.schedule:
        """Return the celery schedule object beat consults for this interval.

        Mirror ``sqlalchemy_celery_beat.models.IntervalSchedule.schedule``, whose
        ORM row this model stands in for on the request and response paths.

        Deliberately a bare :class:`property` rather than a
        ``@computed_field``: this class is a settings field type, and a computed
        field would add the schedule object to the serialised shape and JSON
        schema of every settings field annotated with it.

        :return: The ``celery.schedules.schedule`` for this interval's cadence.
        :raises OverflowError: If ``every`` periods exceed
            :class:`~datetime.timedelta`'s range. The periodic-task write and
            preview models reject such an interval at the request boundary.
        """
        return celery_schedules.schedule(timedelta(**{self.period.value: self.every}))

    def __str__(self) -> str:
        """Return a string representation of the interval schedule.

        Formats the schedule as "every {every} {period}", handling singular forms
        appropriately.

        :return: A formatted string representing the interval schedule.
        :rtype: str
        """
        str_schedule = f"every {self.every} {self.period.value}"
        if self.every == 1:
            return str_schedule[:-1]
        return str_schedule


#: The periods an operator-settable interval may use. The periodic-task write
#: path enforces a one-minute floor, so a shorter period seeds a beat row the UI
#: can then neither toggle nor edit. Narrower than :class:`Period` on purpose:
#: an internally seeded schedule may still run sub-minute (``sync_running_tasks``
#: does), which is why the bound lives here rather than on
#: :class:`IntervalSchedule` itself.
MANAGEABLE_PERIODS: frozenset[Period] = frozenset(
    {Period.DAYS, Period.HOURS, Period.MINUTES}
)


def reject_unmanageable_period(value: IntervalSchedule) -> IntervalSchedule:
    """Reject an interval whose period the periodic-task write path refuses.

    :param value: The interval schedule to check.
    :return: The unchanged schedule.
    :raises ValueError: If the period is outside :data:`MANAGEABLE_PERIODS`.
    """
    if value.period not in MANAGEABLE_PERIODS:
        valid = ", ".join(
            f"'{period.value}'" for period in Period if period in MANAGEABLE_PERIODS
        )
        raise ValueError(
            f"Invalid period '{value.period.value}' for IntervalSchedule. Valid periods"
            f" are: {valid}."
        )
    return value


#: An :class:`IntervalSchedule` an operator may also manage from the UI. Use it
#: for any settings field naming a seeded schedule's cadence.
ManageableInterval = Annotated[
    IntervalSchedule, AfterValidator(reject_unmanageable_period)
]


class CrontabSchedule(BaseModel):
    """Represent a crontab schedule.

    :param minute: The minute component in cron format. Defaults to ``"*"``.
    :param hour: The hour component in cron format. Defaults to ``"*"``.
    :param day_of_week: The day of the week component in cron format.
        Defaults to ``"*"``.
    :param day_of_month: The day of the month component in cron format.
        Defaults to ``"*"``.
    :param month_of_year: The month component in cron format.
        Defaults to ``"*"``.
    :param timezone: The timezone for the cron schedule. Defaults to ``"UTC"``. Must
        be a valid timezone as returned in ``available_timezones()``.
    """

    minute: str = "*"
    hour: str = "*"
    day_of_week: str = "*"
    day_of_month: str = "*"
    month_of_year: str = "*"
    timezone: str = "UTC"

    def __str__(self) -> str:
        """Return a string representation of the crontab schedule.

        Formats the schedule according to cron expression standards and includes the
        timezone.

        :return: A formatted string representing the crontab schedule.
        :rtype: str
        """
        fmt_kwargs = {
            field: BaseCrontabSchedule.cronexp(value)
            for field, value in self.model_dump(exclude={"timezone"}).items()
        }
        return "{minute} {hour} {day_of_month} {month_of_year} {day_of_week}".format(
            **fmt_kwargs
        )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Validate the timezone field.

        Ensures that the provided timezone is among the available timezones.

        :param v: The timezone string to validate.
        :type v: str
        :return: The validated timezone string.
        :rtype: str
        :raises ValueError: If the timezone is not valid.
        """
        if v not in available_timezones():
            raise ValueError(f"{v} is not a valid timezone")
        return v

    @field_validator(*CRON_FIELDS)
    @classmethod
    def validate_scheduler_can_parse_field(cls, v: str, info: ValidationInfo) -> str:
        """Normalise one cron field and reject what the scheduler cannot parse.

        Hands the field to the scheduler's own parser with every other field
        unrestricted, so a malformed value raises here rather than in
        :meth:`validate_scheduler_can_run_expression` and the resulting 422
        locates the error at the field that carried it.

        :param v: The raw cron field value.
        :param info: The validation context, naming the field being validated.
        :return: The value normalised to the form the beat store holds.
        :raises ValueError: If the scheduler cannot parse the field.
        """
        normalised = BaseCrontabSchedule.cronexp(v)
        expression = {
            field: normalised if field == info.field_name else "*"
            for field in CRON_FIELDS
        }
        try:
            TzAwareCrontab(tz=ZoneInfo("UTC"), **expression)
        except Exception as exc:
            raise ValueError(f"Could not parse cron field: {exc}") from exc
        return normalised

    @model_validator(mode="after")
    def validate_scheduler_can_run_expression(self) -> Self:
        """Reject a parseable expression the scheduler can never satisfy.

        Replicate the satisfiability half of
        ``sqlalchemy_celery_beat.models.CrontabSchedule.before_insert_or_update``
        so an expression is decided by the scheduler's own parser at the request
        boundary, rather than accepted here and failed at flush time. Each field
        is parsed on its own by :meth:`validate_scheduler_can_parse_field`; what
        is left is the combination, which ``0 2 30 2 *`` fails. Raising
        ``ValueError`` renders as a 422 locating the error at this schedule,
        which is where a combination that no field alone makes wrong belongs.

        :return: The validated schedule.
        :raises ValueError: If the scheduler cannot satisfy the expression.
        """
        try:
            BaseCrontabSchedule.aware_crontab(self).remaining_estimate(utc_now())
        except Exception as exc:
            raise ValueError(f"Could not parse cron {self}: {exc}") from exc
        return self
