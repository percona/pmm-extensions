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

"""Define the ATW plugin settings section.

The section is read straight off YAML/env rather than mounted as a field on
``ExtensionsSettings``: importing this module runs the ATW package ``__init__``, which
pulls in the app definition and, transitively, ``extensions_settings`` -- so a field
default typed with :class:`AtwSettings` would cycle while ``ExtensionsSettings`` is
still being constructed. Consumers import :data:`atw_settings` at call time (the app's
``periodic_task_schedules`` factory and Celery tasks), matching how alerts
reads its section.
"""

__all__ = ["AtwSettings", "atw_settings"]

from datetime import timedelta
from typing import Annotated, ClassVar

from annotated_types import Gt
from pydantic import PositiveInt

from app.core.celery.models import IntervalSchedule, Period
from app.core.config import BaseYamlSettings
from app.core.utils.fields import StrRelativePath, TimedeltaSeconds


class AtwSettings(BaseYamlSettings):
    """Configure the ATW plugin's diagnostics-send staging and housekeeping.

    :cvar SETTINGS_PREFIXES: The prefixes for ATW-plugin settings in the
        configuration file. Set to ``["EXTENSIONS", "ATW"]`` so the section lives under
        ``EXTENSIONS.ATW``.
    :param bundle_dir: Directory where diagnostics bundles are staged while a send
        runs. Written and read by the Celery worker that builds and uploads them.
    :param bundle_ttl: Maximum age (seconds) of a staged bundle before the cleanup
        task removes it.
    :param cleanup_interval: Cadence of the ``purge_atw_bundles`` sweep that
        deletes expired bundles and fails abandoned sends. ``None`` unregisters
        the sweep entirely.
    :param stale_send_after: How long a send may sit in a non-terminal status
        before the sweep concludes its worker is gone and fails it. Must comfortably
        exceed the slowest legitimate send, or a healthy upload is failed mid-flight.
    :param reconcile_interval: Cadence of the ``reconcile_atw_executions`` sweep that
        fills in run outcomes the recorder hook never observed. ``None`` unregisters
        the sweep entirely — which leaves historical executions and the hook's three
        unobserved terminal paths permanently uncounted, so it is an explicit
        operator opt-out rather than a sensible default.
    :param reconcile_batch_size: The most executions one reconcile tick examines.
        Bounds the upstream traffic per tick; every unresolved row is still reached
        within ``ceil(unresolved / batch_size)`` ticks, because selection is
        least-recently-attempted first.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS", "ATW"]
    bundle_dir: StrRelativePath = "data/atw-bundles"
    bundle_ttl: PositiveInt = 3600
    cleanup_interval: IntervalSchedule | None = IntervalSchedule(
        every=15, period=Period.MINUTES
    )
    stale_send_after: Annotated[TimedeltaSeconds, Gt(timedelta(0))] = timedelta(hours=1)
    reconcile_interval: IntervalSchedule | None = IntervalSchedule(
        every=10, period=Period.MINUTES
    )
    reconcile_batch_size: PositiveInt = 100


atw_settings: AtwSettings = AtwSettings()
