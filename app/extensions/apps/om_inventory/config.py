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

"""Define the Operations Inventory settings section.

Read straight off YAML/env under ``EXTENSIONS.OM_INVENTORY`` rather than mounted as a field
on ``ExtensionsSettings``, for the same reason the other app sections do it: importing this module
runs the package ``__init__``, which pulls in the app definition and transitively
``extensions_settings``, so a field default typed with this class would cycle while
``ExtensionsSettings`` is still under construction.
"""

__all__ = ["OmInventorySettings", "om_inventory_settings"]

from datetime import timedelta
from typing import Annotated, ClassVar

from annotated_types import Ge, Gt, Le
from pydantic import AfterValidator
from pydantic_settings import SettingsConfigDict

from app.core.celery.models import (
    IntervalSchedule,
    Period,
    reject_unmanageable_period,
)
from app.core.config import BaseYamlSettings
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field
from app.core.utils.cross_field import CrossFieldRule
from app.core.utils.fields import StrHttpUrl, TimedeltaSeconds


def _scan_interval_in_range(
    schedule: IntervalSchedule | None,
) -> IntervalSchedule | None:
    """Refuse a scan schedule outside minutes, hours or days, or 10 minutes to 1 day.

    Declared on the whole optional field rather than inside the union, which would
    hide the model from the nested ``SCHEDULE__every`` and ``SCHEDULE__period``
    overrides PMM writes. Multiplying whole seconds, not a ``timedelta`` or a float,
    turns an ``every`` too large for either into a refusal, not an ``OverflowError``.

    :param schedule: The schedule, or ``None`` for no periodic sweep.
    :return: The unchanged schedule.
    :raises ValueError: If the period or the interval is outside the range.
    """
    if schedule is None:
        return None
    reject_unmanageable_period(schedule)
    unit = int(timedelta(**{schedule.period.value: 1}).total_seconds())
    if not 10 * 60 <= schedule.every * unit <= 24 * 3600:
        raise ValueError("SCHEDULE must be from 10 minutes to 1 day")
    return schedule


def _probe_fits_task_timeout(repo: int, connect: int, task_timeout: int) -> bool:
    """Return whether a probe that hits both of its own timeouts still reports.

    :param repo: The repository check timeout, seconds.
    :param connect: The MongoDB connect timeout, seconds.
    :param task_timeout: The task timeout, seconds.
    :return: Whether the two take at most half of the task timeout.
    """
    return 2 * (repo + connect) <= task_timeout


def _stale_run_after_exceeds(stale: timedelta, task_timeout: int) -> bool:
    """Return whether ``stale`` exceeds ``task_timeout`` in the whole seconds GET shows.

    Rounding the way ``TimedeltaSeconds`` serializes keeps every accepted threshold
    re-submittable exactly as GET returns it.

    :param stale: The stale-run threshold.
    :param task_timeout: The task timeout, seconds.
    :return: Whether the threshold is strictly greater.
    """
    return round(stale.total_seconds()) > task_timeout


class OmInventorySettings(BaseYamlSettings):
    """Configure the on-host probe.

    The defaults assume the consumer is PMM reading the estate, which serves whatever
    each row last reported. That is why ``SCHEDULE`` matters more here than it does for
    a job someone triggers by hand: nothing else refreshes a row, and a puller must
    never be the thing that waits for a Nomad job.

    Every field is ``hot_field`` except ``CREDENTIALS_PATH``, which stays YAML/env only
    on purpose. It names a file the payload reads off the *node* and hands to a MongoDB
    driver as a URI; making it settable over the API would turn "change this app's
    configuration" into "read a chosen file on every database host" — every other
    field here only changes how PMM Extensions itself behaves, not what a probe reads off a
    monitored machine's filesystem. It is a deployment fact, and it belongs with the
    deployment.

    :cvar SETTINGS_PREFIXES: Places this section under ``EXTENSIONS.OM_INVENTORY``.
    :cvar CROSS_FIELD_RULES: The probe-timing relationships below, refused on load
        and on every API change rather than discovered mid-sweep.
    :param ENABLED: Whether the sweep may run at all, scheduled *or* manually
        triggered, independent of ``SCHEDULE``. Mirrors PMM's Operations for
        MongoDB on/off switch: pmm-managed flips this (not ``SCHEDULE``) when an
        operator toggles it, so the configured cadence survives being turned off
        and back on rather than being overwritten each time, and puts it back
        within a tick when anything else changes it. Defaults to ``False``,
        matching PMM's own default for that switch, so a fresh deployment's estate
        does not start sweeping until Operations for MongoDB is actually turned on
        somewhere.
    :param AUTO_SCAN: Whether the app sweeps on its own: on ``SCHEDULE``, and once
        when PMM turns ``ENABLED`` on and asks for it. Off leaves sweeping to
        requests: a triggered sweep still runs while ``ENABLED``, and ``SCHEDULE``
        keeps its value for when it is turned back on.
    :param SCHEDULE: How often the probe sweeps the estate, while ``ENABLED`` and
        ``AUTO_SCAN``. ``None`` unregisters the periodic job regardless of either;
        a switch-on that asks for a sweep still gets one while ``AUTO_SCAN`` is on.
        From 10 minutes to 1 day, in minutes, hours or days. A sweep of a dozen
        hosts takes one to three minutes, and a start while one runs only records a
        skipped run that pushes a real one out of ``RUN_RETENTION``. Past a day,
        scanning by hand serves better. The floor also keeps every start at least
        ``TASK_TIMEOUT``'s ceiling apart.
    :param PROBE_DATABASE: Whether the payload connects to mongod and runs database
        commands. ``False`` collects process and OS facts only, which needs no credentials
        — and still yields ``installed_version``, the field this app exists for.
    :param CREDENTIALS_PATH: Node-side file holding the MongoDB URI to take credentials
        from. ``None`` falls back to ``~/.mongodb_uri``, the same file the PBM payloads
        read.
    :param REPO_URL: The file each host fetches to prove it can reach Percona's
        repository. Configurable because an air-gapped estate mirrors it somewhere
        else, and checking the public one there would report every host as broken.
        Restricted to HTTP/HTTPS: the payload hands it straight to
        ``urllib.request``, which would otherwise also accept ``file:`` or ``ftp:``.
    :param REPO_TIMEOUT: How long that fetch may take, from 2 to 30 seconds. Short
        on purpose: a repository slow enough to exceed it is not usable by a package
        manager either, so waiting longer only delays the same answer. dnf itself
        gives up at 30 seconds, and under 2 an HTTPS fetch through a proxy to
        another region can report a working mirror as unreachable.
    :param CONNECT_TIMEOUT: Per-target connect and server-selection timeout, from 1
        to 30 seconds. 30 is pymongo's own server-selection default.
    :param TASK_TIMEOUT: How long to wait for one dispatched probe task to reach a
        terminal status before giving up on it, from 60 to 300 seconds. A probe
        job takes 10 to 15 seconds, Nomad scheduling included. One still running
        after 5 minutes is stuck, and holds a ``MAX_CONCURRENT_PROBES`` slot while
        it is. ``REPO_TIMEOUT`` and ``CONNECT_TIMEOUT`` together may take at most
        half of it, so a host where both run out still reports instead of being
        reaped. A host probing several mongods connects to each in turn, which
        this does not account for.
    :param POLL_INTERVAL: Delay between task-status polls, from 1 to 30 seconds. A
        probe finishes within 15 seconds, so a 30-second poll can already add 30
        seconds to every batch. The ceiling keeps it under ``TASK_TIMEOUT``'s
        floor, so the wait loop always checks before its deadline.
    :param MAX_CONCURRENT_PROBES: Ceiling on probe tasks in flight at once, from 1
        to 8. Every dispatch is a Nomad job, and each host in flight polls the
        Tasks API on its own ``POLL_INTERVAL`` clock for the whole sweep. That
        API's database pool is ``POOL_SIZE`` + ``MAX_OVERFLOW``, 5 by default and
        configured where this class cannot read it, so a value above 5 makes polls
        queue briefly for a connection, and a poll that waits past that pool's
        ``POOL_TIMEOUT`` is retried a bounded number of times. 8 halves the time a
        large estate takes at the default, at the cost of that queueing.
    :param RUN_RETENTION: How many runs to keep, from 10 to 1000. Each carries a
        per-host receipt, so this bounds the table rather than an operator having
        to. 10 holds 100 minutes at the shortest ``SCHEDULE``, 1000 about a week at
        the default.
    :param STALE_RUN_AFTER: How long a run may stay ``running`` before the trigger
        endpoint concludes its worker is gone. A run whose sweep is lost is failed
        sooner, once it goes ``crud.RUN_LEASE`` without a write, so this bounds a
        sweep that is still alive. Must comfortably exceed the slowest
        legitimate sweep, which dispatches ``MAX_CONCURRENT_PROBES`` hosts at a time
        and can take up to ``TASK_TIMEOUT`` per batch: for an estate of ``N`` hosts
        that is roughly ``ceil(N / MAX_CONCURRENT_PROBES) * TASK_TIMEOUT``. Only
        the ``TASK_TIMEOUT`` floor is enforced, since a single probe may run that
        long and anything at or below it can only reap a live run; the estate-size
        bound stays advisory because ``N`` is unknown here. The
        default covers estates up to a few hundred hosts at the defaults above;
        raising ``MAX_CONCURRENT_PROBES`` or targeting a much larger estate should
        raise this too, or the trigger endpoint reaps a sweep that is still running
        and reopens the single-flight race this check exists to close. At most a
        day: a sweep whose worker is gone is failed within ``crud.RUN_LEASE``
        anyway. A day covers about 1900 hosts at the defaults even if every probe
        runs to ``TASK_TIMEOUT``, and twice that at 8 probes in flight.
    """

    model_config = SettingsConfigDict(hide_input_in_errors=True)
    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "OM_INVENTORY"]
    CROSS_FIELD_RULES: ClassVar[tuple[CrossFieldRule, ...]] = (
        CrossFieldRule(
            ("REPO_TIMEOUT", "CONNECT_TIMEOUT", "TASK_TIMEOUT"),
            _probe_fits_task_timeout,
            "REPO_TIMEOUT + CONNECT_TIMEOUT must not exceed half of TASK_TIMEOUT",
        ),
        CrossFieldRule(
            ("STALE_RUN_AFTER", "TASK_TIMEOUT"),
            _stale_run_after_exceeds,
            "STALE_RUN_AFTER must exceed TASK_TIMEOUT",
        ),
    )

    ENABLED: bool = hot_field(default=False)
    AUTO_SCAN: bool = hot_field(default=True)
    SCHEDULE: Annotated[
        IntervalSchedule | None, AfterValidator(_scan_interval_in_range)
    ] = hot_field(IntervalSchedule(every=10, period=Period.MINUTES))
    PROBE_DATABASE: bool = hot_field(default=True)
    REPO_URL: StrHttpUrl = hot_field(
        "https://repo.percona.com/percona/yum/PERCONA-PACKAGING-KEY", advanced=True
    )
    REPO_TIMEOUT: Annotated[int, Ge(2), Le(30)] = hot_field(8, advanced=True)
    CREDENTIALS_PATH: str | None = None
    CONNECT_TIMEOUT: Annotated[int, Ge(1), Le(30)] = hot_field(5, advanced=True)
    TASK_TIMEOUT: Annotated[int, Ge(60), Le(300)] = hot_field(180, advanced=True)
    POLL_INTERVAL: Annotated[int, Ge(1), Le(30)] = hot_field(3, advanced=True)
    MAX_CONCURRENT_PROBES: Annotated[int, Ge(1), Le(8)] = hot_field(4, advanced=True)
    RUN_RETENTION: Annotated[int, Ge(10), Le(1000)] = hot_field(50, advanced=True)
    STALE_RUN_AFTER: Annotated[
        TimedeltaSeconds, Gt(timedelta(0)), Le(timedelta(days=1))
    ] = hot_field(timedelta(hours=4), advanced=True)


om_inventory_settings: OmInventorySettings = (  # ty: ignore[invalid-assignment]
    OverridableSettingsProxy(OmInventorySettings)
)
