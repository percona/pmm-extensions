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

from annotated_types import Gt
from pydantic import PositiveInt

from app.core.celery.models import IntervalSchedule, Period
from app.core.config import BaseYamlSettings
from app.core.settings_override.proxy import OverridableSettingsProxy
from app.core.settings_override.registry import hot_field
from app.core.utils.fields import StrHttpUrl, TimedeltaSeconds

#: A ``TimedeltaSeconds`` that must be greater than zero.
_PositiveSeconds = Annotated[TimedeltaSeconds, Gt(timedelta(0))]


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
    :param ENABLED: Whether the sweep may run at all, scheduled *or* manually
        triggered, independent of ``SCHEDULE``. Mirrors PMM's Operations for
        MongoDB on/off switch: pmm-managed flips this (not ``SCHEDULE``) when an
        operator toggles it, so the configured cadence survives being turned off
        and back on rather than being overwritten each time. Defaults to ``False``, matching
        PMM's own default for that switch, so a fresh deployment's estate does not
        start sweeping until Operations for MongoDB is actually turned on somewhere.
    :param SCHEDULE: How often the probe sweeps the estate, while ``ENABLED``. ``None``
        unregisters the periodic job regardless of ``ENABLED``, leaving the trigger
        endpoint as the only way facts are refreshed.
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
    :param REPO_TIMEOUT: How long that fetch may take, seconds. Short on purpose: a
        repository slow enough to exceed it is not usable by a package manager
        either, so waiting longer only delays the same answer.
    :param CONNECT_TIMEOUT: Per-target connect and server-selection timeout, seconds.
    :param TASK_TIMEOUT: How long to wait for one dispatched probe task to reach a
        terminal status before giving up on it, seconds.
    :param POLL_INTERVAL: Delay between task-status polls, seconds.
    :param MAX_CONCURRENT_PROBES: Ceiling on probe tasks in flight at once. Every
        dispatch is a Nomad job, and a real estate has far more hosts than this
        workspace's sandbox. Bounded well under the Tasks API's own database pool
        (``POOL_SIZE`` + ``MAX_OVERFLOW``, 5 by default) on purpose: each
        host in flight polls that same API on its own ``POLL_INTERVAL`` clock for
        the whole sweep, not just once at dispatch, so this is a sustained call
        volume against that pool, not a one-time burst. A value at or above the
        pool's own size guarantees queueing on every poll cycle regardless of any
        other traffic sharing that pool — dispatch.py's bounded capacity retry
        absorbs a brief race, not a fan-out that structurally outnumbers the
        connections available to serve it.
    :param RUN_RETENTION: How many runs to keep. Each carries a per-host receipt, so
        this bounds the table rather than an operator having to.
    :param STALE_RUN_AFTER: How long a run may stay ``running`` before the trigger
        endpoint concludes its worker is gone. Must comfortably exceed the slowest
        legitimate sweep, which dispatches ``MAX_CONCURRENT_PROBES`` hosts at a time
        and can take up to ``TASK_TIMEOUT`` per batch: for an estate of ``N`` hosts
        that is roughly ``ceil(N / MAX_CONCURRENT_PROBES) * TASK_TIMEOUT``. The
        default covers estates up to a few hundred hosts at the defaults above;
        raising ``MAX_CONCURRENT_PROBES`` or targeting a much larger estate should
        raise this too, or the trigger endpoint reaps a sweep that is still running
        and reopens the single-flight race this check exists to close.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "OM_INVENTORY"]

    ENABLED: bool = hot_field(default=False)  # ty: ignore[invalid-assignment]
    SCHEDULE: IntervalSchedule | None = hot_field(  # ty: ignore[invalid-assignment]
        IntervalSchedule(every=10, period=Period.MINUTES)
    )
    PROBE_DATABASE: bool = hot_field(default=True)  # ty: ignore[invalid-assignment]
    REPO_URL: StrHttpUrl = hot_field(  # ty: ignore[invalid-assignment]
        "https://repo.percona.com/percona/yum/PERCONA-PACKAGING-KEY", advanced=True
    )
    REPO_TIMEOUT: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        8, advanced=True
    )
    CREDENTIALS_PATH: str | None = None
    CONNECT_TIMEOUT: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        5, advanced=True
    )
    TASK_TIMEOUT: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        180, advanced=True
    )
    POLL_INTERVAL: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        3, advanced=True
    )
    MAX_CONCURRENT_PROBES: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        4, advanced=True
    )
    RUN_RETENTION: PositiveInt = hot_field(  # ty: ignore[invalid-assignment]
        50, advanced=True
    )
    STALE_RUN_AFTER: _PositiveSeconds = hot_field(  # ty: ignore[invalid-assignment]
        timedelta(hours=4), advanced=True
    )


om_inventory_settings: OmInventorySettings = (  # ty: ignore[invalid-assignment]
    OverridableSettingsProxy(
        OmInventorySettings, setting_class=OmInventorySettings.__name__
    )
)
