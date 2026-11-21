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

"""Define the DB-model factories for the Support diagnostics app's tests.

Each factory sets ``__set_relationships__ = False``: polyfactory 3 otherwise
builds a fresh parent row for every relationship, and tests that scope by
foreign key would see that parent instead of the one they created.
"""

from polyfactory import Use
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

from app.extensions.apps.atw.models import AtwIncident, AtwIncidentExecution, AtwSendLog


class AtwIncidentFactory(SQLAlchemyFactory[AtwIncident]):
    """Define factory for AtwIncident instances.

    ``closed_at`` is pinned to ``None`` so a built incident is open unless a test
    stamps it; polyfactory otherwise fills the nullable column at random.
    """

    __set_relationships__ = False

    closed_at = None


class AtwIncidentExecutionFactory(SQLAlchemyFactory[AtwIncidentExecution]):
    """Define factory for AtwIncidentExecution instances."""

    __set_relationships__ = False


class AtwSendLogFactory(SQLAlchemyFactory[AtwSendLog]):
    """Define factory for AtwSendLog instances.

    ``detail`` is pinned to an empty mapping because polyfactory cannot generate a
    value for the untyped JSON column.
    """

    __set_relationships__ = False

    detail = Use(dict)
