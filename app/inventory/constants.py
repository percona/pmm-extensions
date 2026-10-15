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

"""Define constants for the Inventory service."""

from datetime import timedelta
from enum import StrEnum
from typing import Final

DEFAULT_MYSQL_PORT = 3306
DEFAULT_POSTGRESQL_PORT = 5432

#: Discriminator carried by an active row inside every unique index, so a retired
#: row (which carries its own primary key instead) never collides with its
#: replacement. It has to be non-NULL and outside the autoincrement range: a NULL
#: never compares equal to another, so a NULL sentinel would silently stop the
#: index constraining active rows at all.
ACTIVE_RETIREMENT_KEY: Final = -1

#: Part of the API contract: callers tell an uncollected observation apart from a
#: missing parent by this wording, so both routes and their tests share it.
UNCOLLECTED_HOST_OBSERVATION_DETAIL = (
    "System observation not collected yet for this node"
)
#: Service-level counterpart of :data:`UNCOLLECTED_HOST_OBSERVATION_DETAIL`, pinned
#: by the same contract.
UNCOLLECTED_SERVICE_OBSERVATION_DETAIL = (
    "System observation not collected yet for this service"
)

#: Longest error text stored on an entity's ``last_sync_error``. The manager
#: truncates to this, so a caller may send an exception message of any length.
SYNC_ERROR_MAX_LENGTH: Final = 1000

#: How far ahead of this service's clock a reported ``attempted_at`` may sit
#: before the report is refused. The ordering guards admit anything not older
#: than the stored attempt, so a reporter whose clock runs fast stamps a
#: freshness no later report can supersede until wall-clock time catches up —
#: and one whose clock is then corrected is locked out for the whole interval.
#: Refusing leaves the entity looking stale, which is the direction the rest of
#: this mechanism already errs in. Wide enough to absorb the drift between two
#: containers of one deployment without absorbing a misconfigured clock.
SYNC_ATTEMPT_MAX_CLOCK_SKEW: Final = timedelta(minutes=5)

#: Most outcomes one bulk ``sync-health`` request may carry. Bounds the work a
#: single request holds the session for; a reporter with more to send splits
#: them across requests.
SYNC_HEALTH_BULK_MAX_ENTRIES: Final = 1000


class RetirableEntityName(StrEnum):
    """Name the inventory entity types that carry a retirement tombstone.

    Values are spelled out rather than derived, because they cross a service
    boundary: PMM Extensions names an entity type by these strings when it asks inventory
    to collect. Inventory-local on purpose — PMM Extensions' own
    ``SyncInventoryEntityTypeEnum`` lives in a package this service must not
    import.
    """

    NODE = "node"
    SERVICE = "service"
    SCHEMA = "schema"
    TABLE = "table"
