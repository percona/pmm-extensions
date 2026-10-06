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

"""Define the ATW category taxonomy and its service-type root mapping.

Kept out of ``models.py`` because the ATW plugin owns a DB migration: the
Alembic plugin-discovery loader execs ``models.py`` at migration time, so that
module must stay self-contained (importing only ``app.core``/``sqlmodel``).
The category taxonomy depends on ``app.inventory.models.ServiceTypeEnum``, which
transitively registers the inventory tables in ``SQLModel.metadata`` — safe to
import from request handlers, but not from the migration-discovered ``models.py``.
"""

from enum import StrEnum

from app.inventory.models import ServiceTypeEnum

_GENERIC_SERVICE_TYPE = "generic"

# Insertion order defines the API response order for category_root.
CATEGORY_ROOT_LABELS: dict[str, str] = {
    ServiceTypeEnum.MYSQL: "MySQL",
    ServiceTypeEnum.MONGODB: "MongoDB",
    ServiceTypeEnum.POSTGRESQL: "PostgreSQL",
    ServiceTypeEnum.PROXYSQL: "ProxySQL",
    ServiceTypeEnum.HAPROXY: "HAProxy",
    ServiceTypeEnum.EXTERNAL: "External",
    _GENERIC_SERVICE_TYPE: "Generic",
}


def derive_category_root(service_type: str | None) -> str:
    """Map snippet ``service_type`` meta to an ATW category-root display label.

    :param service_type: Snippet frontmatter ``service_type`` value, or ``None``
        when the key is absent.
    :return: Display label for the category root (e.g. ``"MySQL"``, ``"Generic"``).
    """
    generic_label = CATEGORY_ROOT_LABELS[_GENERIC_SERVICE_TYPE]
    if not service_type:
        return generic_label
    return CATEGORY_ROOT_LABELS.get(service_type.lower(), generic_label)


class ParentCategory(StrEnum):
    """Enumerate top-level categories for ATW."""

    CRASHES = "Crashes"
    PERFORMANCE_ISSUES = "Performance Issues"
    REPLICATION_HA = "Replication High / Availability"
    BACKUP_RECOVERY = "Backup and Recovery"


class ATWCategory(StrEnum):
    """Enumerate categories for ATW (Advanced Troubleshooting Wizard)."""

    # --- Crashes ---
    SERVER_CRASHED_RESTART_SUCCESSFUL = (
        "Server crashed - Restart Successful",
        ParentCategory.CRASHES,
    )
    SERVER_CRASHED_RESTART_NOT_SUCCESSFUL = (
        "Server crashed - Restart not Successful",
        ParentCategory.CRASHES,
    )

    # --- Performance Issues ---
    OVERALL_SLOWNESS = ("Overall Slowness", ParentCategory.PERFORMANCE_ISSUES)
    QUERY_TUNING_OPTIMIZATION = (
        "Query Tuning / Optimization",
        ParentCategory.PERFORMANCE_ISSUES,
    )
    NOT_RESPONDING = ("Not Responding", ParentCategory.PERFORMANCE_ISSUES)
    WRITES_ARE_BLOCKED = ("Writes are Blocked", ParentCategory.PERFORMANCE_ISSUES)
    PERFORMANCE_OTHER = ("Other", ParentCategory.PERFORMANCE_ISSUES)
    TEMPORARY_STALLS = ("Temporary Stalls", ParentCategory.PERFORMANCE_ISSUES)

    # --- Replication High / Availability ---
    NATIVE_ASYNC_REPLICATION = (
        "Native Asynchronous Replication",
        ParentCategory.REPLICATION_HA,
    )
    MULTI_SOURCE_REPLICATION = (
        "Multi-Source replication",
        ParentCategory.REPLICATION_HA,
    )
    GALERA = ("Galera", ParentCategory.REPLICATION_HA)
    GROUP_REPLICATION = ("Group Replication", ParentCategory.REPLICATION_HA)
    REPLICA_SET_REPLICATION = (
        "Replica Set Replication",
        ParentCategory.REPLICATION_HA,
    )

    # --- Backup and Recovery ---
    BACKUP_PBM = ("Backup / PBM", ParentCategory.BACKUP_RECOVERY)

    def __new__(cls, label: str, parent: ParentCategory) -> "ATWCategory":  # noqa: D102
        obj = str.__new__(cls, label)
        obj._value_ = label
        obj._parent = parent  # noqa: SLF001
        return obj

    @property
    def parent(self) -> ParentCategory:
        """Return the parent category of the current category."""
        return self._parent
