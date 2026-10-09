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

"""Define the payload for database connectivity check tasks.

This script runs on a Nomad client node. It receives a ``--config`` JSON
argument, connects to the target database, and prints a JSON result to stdout.

Each database driver is imported inside the check that uses it. The client
carries only the drivers its own hosts need, so a module-scope import would
stop the whole payload loading wherever any one driver is absent.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

#: Inner per-driver DB connect timeout (seconds). Defined here (not imported
#: from ``app.tasks.connectivity.constants``) because this script runs
#: standalone on a Nomad client with no access to the ``app`` package. Must
#: stay strictly less than ``CONNECTIVITY_CHECK_TIMEOUT`` (the outer connect
#: budget; a test enforces this) so the inner connect completes inside the
#: outer window.
CONNECT_TIMEOUT = 10


def check_mysql(host: str, port: int) -> dict[str, bool | str]:
    """Check MySQL connectivity via ``SELECT 1``.

    A server-side auth/authorization rejection (error codes 1044, 1045, 1130)
    is reported as success because the server's structured response proves it
    is reachable, which is what this check measures. Network-level failures
    (codes 2xxx) and any other exception remain failures.

    :param host: The database host address.
    :param port: The database port number.
    :return: A dict with ``success`` and optionally ``error``.
    """
    import myloginpath  # noqa: PLC0415 - optional mysql driver
    import pymysql  # noqa: PLC0415

    connect_kwargs = {"host": host, "port": port, "connect_timeout": CONNECT_TIMEOUT}
    with contextlib.suppress(Exception):
        login = myloginpath.parse("client")
        connect_kwargs["user"] = login.get("user")
        connect_kwargs["password"] = login.get("password")

    try:
        conn = pymysql.connect(**connect_kwargs)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            return {"success": True}
        finally:
            conn.close()
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if code in (1044, 1045, 1130):
            return {"success": True}
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def check_postgresql(host: str, port: int) -> dict[str, bool | str]:
    """Check PostgreSQL connectivity via ``SELECT 1``.

    A SQLSTATE class 28 response (``28000`` invalid authorization
    specification, ``28P01`` invalid password) is reported as success because
    the server's structured response proves it is reachable. Network-level
    failures (``pgcode`` not set) and any other exception remain failures.

    :param host: The database host address.
    :param port: The database port number.
    :return: A dict with ``success`` and optionally ``error``.
    """
    import psycopg2  # noqa: PLC0415 - optional postgresql driver

    try:
        conn = psycopg2.connect(host=host, port=port, connect_timeout=CONNECT_TIMEOUT)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            return {"success": True}
        finally:
            conn.close()
    except psycopg2.OperationalError as exc:
        pgcode = getattr(exc, "pgcode", None)
        if pgcode in ("28000", "28P01"):
            return {"success": True}
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def check_mongodb(host: str, port: int) -> dict[str, bool | str]:
    """Check MongoDB connectivity via the ``ping`` command.

    A server-side ``OperationFailure`` with code ``13`` (Unauthorized) or
    ``18`` (AuthenticationFailed) is reported as success because the server
    processed the request and responded, which proves it is reachable.
    ``ServerSelectionTimeoutError`` and any other exception remain failures.

    :param host: The database host address.
    :param port: The database port number.
    :return: A dict with ``success`` and optionally ``error``.
    """
    import pymongo.errors  # noqa: PLC0415 - optional mongodb driver

    try:
        client = pymongo.MongoClient(
            host=host, port=port, serverSelectionTimeoutMS=CONNECT_TIMEOUT * 1000
        )
        try:
            client.admin.command("ping")
            return {"success": True}
        finally:
            client.close()
    except pymongo.errors.OperationFailure as exc:
        if getattr(exc, "code", None) in (13, 18):
            return {"success": True}
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


CHECKERS = {
    "mysql": check_mysql,
    "postgresql": check_postgresql,
    "mongodb": check_mongodb,
}


def main() -> None:
    """Parse the config file and run the appropriate database checker.

    The ``--config`` argument is a path to a file containing the JSON
    configuration (written by Nomad from the ``NOMAD_META_config`` env var),
    not an inline JSON string.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.config) as config_file:
        config = json.load(config_file)
    checker = CHECKERS[config["service_type"]]
    result = checker(config["host"], config["port"])
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
