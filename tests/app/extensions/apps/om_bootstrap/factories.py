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

"""Define the DB-model factories for the Operations Bootstrap app's tests."""

from polyfactory import Use
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

from app.core.utils.date_time import utc_now
from app.extensions.apps.om_bootstrap.models import (
    BootstrapRun,
    BootstrapRunStatus,
    InstallMethod,
    OperatingSystem,
)


class BootstrapRunFactory(SQLAlchemyFactory[BootstrapRun]):
    """Define factory for BootstrapRun instances.

    Pinned to a running, packages-on-Ubuntu run with no hosts or run-level steps,
    so a test only spells out the fields it is actually about. ``hosts``,
    ``run_steps`` and ``member_configs`` are pinned because polyfactory cannot
    generate their untyped JSON documents, ``finished_at``/``error`` because it
    would otherwise fill the nullable columns at random, and the mongod
    settings to the column defaults so a run's rendered config is valid.
    """

    status = BootstrapRunStatus.RUNNING
    install_method = InstallMethod.PACKAGES
    os = OperatingSystem.UBUNTU
    mongodb_version = "8.0"
    replica_set_name = "rs-test"
    started_at = Use(utc_now)
    finished_at = None
    error = None
    hosts = Use(list)
    run_steps = Use(list)
    data_path = "/var/lib/mongo"
    log_path = "/var/log/mongodb/mongod.log"
    port = 27017
    bind_ip = "127.0.0.1"
    member_configs = Use(dict)
    cancel_requested = False
