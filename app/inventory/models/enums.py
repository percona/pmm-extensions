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

"""Define the enums of the Inventory API models."""

from enum import auto, StrEnum


class SourceEnum(StrEnum):
    """Enumeration of possible data sources for a node.

    :cvar PMM: Represents the PMM data source.
    :vartype PMM: str
    """

    PMM = auto()


class ServiceTypeEnum(StrEnum):
    """Enumerate the supported service types.

    :cvar MYSQL: Represents the MySQL service type.
    :vartype MYSQL: str
    :cvar POSTGRESQL: Represents the PostgreSQL service type.
    :vartype POSTGRESQL: str
    :cvar MONGODB: Represents the MongoDB service type.
    :vartype MONGODB: str
    :cvar PROXYSQL: Represents the ProxySQL service type.
    :vartype PROXYSQL: str
    :cvar HAPROXY: Represents the HAProxy service type.
    :vartype HAPROXY: str
    :cvar EXTERNAL: Represents an external service type.
    :vartype EXTERNAL: str
    :cvar VALKEY: Represents the Valkey service type.
    :vartype VALKEY: str
    """

    MYSQL = auto()
    POSTGRESQL = auto()
    MONGODB = auto()
    PROXYSQL = auto()
    HAPROXY = auto()
    EXTERNAL = auto()
    VALKEY = auto()


class LinkageMethodEnum(StrEnum):
    """Enumerate how an external-identity binding came to be recorded.

    Every member names an operator action: nothing here records a binding the
    syncer made on its own, because ordinary sync creation writes no alias row.

    Values are spelled out rather than derived with ``auto()``. The column
    persists the member *name*, but the API serializes the *value*, so under
    ``auto()`` the two move in opposite ways when a member is renamed: the
    stored form follows the rename while the published one silently changes
    with it. Pinning the value holds the wire contract — this enum reaches the
    generated API client — and leaves a rename a database concern alone.
    """

    OPERATOR_CONFIRMATION = "operator_confirmation"
    OPERATOR_UNLINK = "operator_unlink"


class IdentityLinkDecisionEnum(StrEnum):
    """Enumerate the decisions an operator may record against a candidate pairing.

    :cvar CONFIRMED: The pairing names one machine, and the link stands.
    :cvar REJECTED: The pairing names two machines, so stop suggesting it.
    :cvar UNLINKED: A standing confirmation was reversed.

    Values are spelled out for the reason given on
    :class:`LinkageMethodEnum`, and it binds harder here: this enum is also a
    *request* body field, so a rename would reject the payloads callers were
    already sending.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNLINKED = "unlinked"


class SyncOutcomeEnum(StrEnum):
    """Enumerate the outcomes a syncer reports for one entity's sync attempt.

    :cvar SUCCESS: The entity was compared against its source and updated.
    :cvar FAILURE: The attempt raised before the comparison completed.
    """

    SUCCESS = "success"
    FAILURE = "failure"
