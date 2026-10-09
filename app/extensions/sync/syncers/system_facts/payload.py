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

"""Define the payload for system facts Sync tasks.

Standalone script run on the executor host via the ``run-python`` task. Collects
best-effort host facts (OS version, installed packages, EOL-relevant config) when
co-located with the node, plus the database engine version per requested service, and
emits one JSON document to stdout::

    {"host": {...} | null, "services": {"<address>": {"db_engine_version", "collected_at"}}}

Each step is best-effort: a failed host fact or unreachable service is logged to stderr
and its key omitted; the script never raises, so a partial collection still yields a
usable snapshot. Database drivers (``pymysql``, ``psycopg``, ``pymongo``) and
``myloginpath`` are imported lazily so the module imports without them present.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from configparser import Error as ConfigParserError, RawConfigParser
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

#: Diagnostics logger with its own stderr handler (propagation off) so log lines never
#: mix into the JSON result document carried on stdout.
logger = logging.getLogger("system_facts.payload")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_stderr_handler)

#: ``scheme://user[:password]@`` userinfo prefix of a connection URI.
_URI_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]+@")
#: Sensitive ``key=value`` URI query params (``authMechanismProperties`` may carry an
#: AWS session token; ``password`` may appear as a query param, not just userinfo).
_URI_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>password|passwd|authmechanismproperties)=(?P<val>[^&\s\"']+)"
)


def _redact_secrets(text: str) -> str:
    """Mask credentials embedded in connection-string URIs within ``text``.

    Replace the ``user:password@`` userinfo of any ``scheme://...@`` URI with ``***@``,
    and mask the value of sensitive query params (``password``/``passwd`` and
    ``authMechanismProperties``), so driver exceptions that echo a connection string
    cannot leak credentials to stderr (and onward to task/application logs).

    :param text: The text that may contain a connection URI.
    :type text: str
    :return: The text with any URI userinfo and sensitive query values masked.
    :rtype: str
    """
    text = _URI_USERINFO_RE.sub(r"\g<scheme>***@", text)
    return _URI_QUERY_SECRET_RE.sub(r"\g<key>=***", text)


#: Path to the OS release file (module-level so tests can redirect it).
OS_RELEASE_PATH = Path("/etc/os-release")
#: Host fact fields that constitute a meaningful host observation. Spelled as a literal
#: rather than derived from the write model: this module is dispatched to the target node
#: and runs stdlib-only before ``pip install``, so it cannot import ``app.inventory``. A
#: future observation field must be added here as well as to the model.
HOST_FIELDS = ("os_version", "installed_packages", "config", "can_elevate")
#: Seconds to wait when connecting to a database service.
DB_CONNECT_TIMEOUT = 10
#: Seconds to wait for a package-manager query to complete.
PKG_QUERY_TIMEOUT = 60
class DefaultPort(IntEnum):
    """Represent the default listening port per database engine.

    Used when a service address omits an explicit port.
    """

    MYSQL = 3306
    POSTGRESQL = 5432
    MONGODB = 27017


class ServiceType(str, Enum):
    """Represent the database engine types whose versions are collected.

    Values mirror ``app.inventory.models.ServiceTypeEnum`` so a service ``type`` sent
    in the task config round-trips here. This is redeclared locally because the payload
    is a standalone script and must import without the application package.
    """

    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    MONGODB = "mongodb"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    :return: The current UTC timestamp.
    :rtype: str
    """
    return datetime.now(timezone.utc).isoformat()


def parse_host_port(
        host_entry: str,
        default_port: int = DefaultPort.MYSQL
    ) -> tuple[str, int]:
    """Split a ``host[:port]`` entry into host and port components.

    Handles IPv4/hostname (``host``/``host:port``) and IPv6 forms: bracketed
    ``[2001:db8::1]:5432`` (and bare ``[2001:db8::1]``), and bare ``2001:db8::1``
    (more than one colon, no port) which is returned as the host with the default
    port rather than mis-split on the final colon.

    :param host_entry: The address in ``host``, ``host:port``, or IPv6 form.
    :type host_entry: str
    :param default_port: The port to use when none is present. Defaults to 3306.
    :type default_port: int
    :return: A tuple of ``(host, port)``.
    :rtype: tuple[str, int]
    """
    if host_entry.startswith("[") and "]" in host_entry:
        host, _, rest = host_entry[1:].partition("]")
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return host, default_port
        return host, default_port
    if host_entry.count(":") > 1:
        # Bare IPv6 literal without a port; the final colon is not a separator.
        return host_entry, default_port
    if ":" in host_entry:
        host, _, port = host_entry.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, default_port
    return host_entry, default_port


def _read_os_release() -> dict[str, str]:
    """Parse ``/etc/os-release`` into a key/value mapping.

    :return: The parsed os-release fields, or an empty mapping if unavailable.
    :rtype: dict[str, str]
    """
    try:
        content = OS_RELEASE_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    data = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"')
    return data


def collect_os_version() -> str | None:
    """Collect the operating system version from ``/etc/os-release``.

    :return: A human-readable OS version, or ``None`` if it cannot be determined.
    :rtype: str | None
    """
    data = _read_os_release()
    if pretty_name := data.get("PRETTY_NAME"):
        return pretty_name
    name = data.get("NAME")
    version = data.get("VERSION_ID") or data.get("VERSION")
    if name and version:
        return f"{name} {version}"
    return name or None


def collect_installed_packages() -> list[dict[str, str]] | None:
    """Collect installed packages via the host package manager.

    Uses ``rpm`` or ``dpkg-query`` with a fixed argument vector (never a shell), guarded
    by :func:`shutil.which`. Returns ``None`` when no package manager is available or the
    query fails.

    :return: A list of ``{"name", "version"}`` dicts, or ``None``.
    :rtype: list[dict[str, str]] | None
    """
    if shutil.which("rpm"):
        cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}-%{RELEASE}\n"]
    elif shutil.which("dpkg-query"):
        cmd = ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]
    else:
        return None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PKG_QUERY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        logger.warning("Failed to query installed packages: %s", _redact_secrets(str(err)))
        return None
    if proc.returncode != 0:
        logger.warning("Package query exited with %s", proc.returncode)
        return None
    packages = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, _, version = line.partition("\t")
        if name:
            packages.append({"name": name.strip(), "version": version.strip()})
    return packages or None


def collect_host_config() -> dict[str, Any]:
    """Collect EOL-relevant host configuration (kernel, architecture, OS identity).

    :return: A mapping of config keys to values (empty values omitted).
    :rtype: dict[str, Any]
    """
    os_release = _read_os_release()
    config = {
        "kernel": platform.release(),
        "arch": platform.machine(),
        "os_id": os_release.get("ID"),
        "os_version_id": os_release.get("VERSION_ID"),
    }
    return {key: value for key, value in config.items() if value}


def collect_can_elevate() -> bool | None:
    """Collect whether a ``sudo``-prefixed command can start on this node.

    ``True`` when the task user is uid 0, or when a bare ``sudo`` resolves on
    PATH: the two conditions under which the launch check lets a
    ``sudo``-prefixed interpreter through. It does not promise that user is in
    sudoers — a ``sudo -n`` invocation still fails for a non-sudoer. A measured
    ``False`` is a value, not an absence, so callers must admit it on
    ``is not None`` rather than on truthiness.

    :return: Whether the launch check would let a ``sudo``-prefixed command
        through, or ``None`` on a platform without POSIX uids, where the
        question has no answer.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    return geteuid() == 0 or shutil.which("sudo") is not None


def collect_host_facts() -> dict[str, Any]:
    """Collect all host-level facts, each best-effort.

    :return: A mapping always carrying ``collected_at`` plus any gathered host fields.
    :rtype: dict[str, Any]
    """
    facts: dict[str, Any] = {"collected_at": _now_iso()}
    if os_version := collect_os_version():
        facts["os_version"] = os_version
    if packages := collect_installed_packages():
        facts["installed_packages"] = packages
    if config := collect_host_config():
        facts["config"] = config
    if (can_elevate := collect_can_elevate()) is not None:
        facts["can_elevate"] = can_elevate
    return facts


def _mysql_creds(address: str) -> dict[str, str]:
    """Resolve MySQL credentials from ``~/.mylogin.cnf`` by matching host and port.

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :return: A mapping with ``user``/``password`` keys, or an empty mapping.
    :rtype: dict[str, str]
    """
    import myloginpath  # noqa: PLC0415 - optional mysql driver

    try:
        content = myloginpath.read()
    except (OSError, TypeError) as err:
        logger.warning("Cannot read login path file: %s", _redact_secrets(str(err)))
        return {}
    if not isinstance(content, str):
        return {}
    parser = RawConfigParser(allow_no_value=True)
    try:
        parser.read_string(content, source=".mylogin.cnf")
    except ConfigParserError as err:
        logger.warning("Failed to parse login path file: %s", _redact_secrets(str(err)))
        return {}

    def _creds(section: str) -> dict[str, str]:
        items = dict(parser.items(section))
        return {
            "user": (items.get("user") or "").strip('"'),
            "password": (items.get("password") or "").strip('"'),
            "host": (items.get("host") or "").strip('"'),
            "port": (items.get("port") or str(DefaultPort.MYSQL.value)).strip('"'),
        }

    target_host, target_port = parse_host_port(
        address,
        default_port=DefaultPort.MYSQL
    )
    for section in parser.sections():
        data = _creds(section)
        try:
            section_port = int(data["port"])
        except (TypeError, ValueError):
            continue
        if data["host"] == target_host and section_port == target_port:
            return data
    if parser.has_section("client"):
        return _creds("client")
    return {}


def _collect_mysql_version(address: str) -> str | None:
    """Connect to a MySQL service and return its engine version.

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :return: The MySQL version string, or ``None``.
    :rtype: str | None
    """
    import pymysql  # noqa: PLC0415 - optional mysql driver

    creds = _mysql_creds(address)
    host, port = parse_host_port(address, default_port=DefaultPort.MYSQL)
    with pymysql.connect(
        host=host,
        port=port,
        user=creds.get("user") or None,
        password=creds.get("password") or None,
        connect_timeout=DB_CONNECT_TIMEOUT,
        read_timeout=DB_CONNECT_TIMEOUT,
    ) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT VERSION()")
        row = cursor.fetchone()
    return row[0] if row else None


def _collect_postgresql_version(address: str) -> str | None:
    """Connect to a PostgreSQL service and return its engine version.

    Authenticates via ``~/.pgpass`` and standard ``PG*`` environment variables.

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :return: The PostgreSQL ``server_version``, or ``None``.
    :rtype: str | None
    """
    import psycopg  # noqa: PLC0415 - optional postgresql driver

    host, port = parse_host_port(address, default_port=DefaultPort.POSTGRESQL)
    conninfo = {
        "host": host,
        "port": port,
        "dbname": os.environ.get("PGDATABASE", "postgres"),
        "connect_timeout": DB_CONNECT_TIMEOUT,
    }
    if user := os.environ.get("PGUSER"):
        conninfo["user"] = user
    with psycopg.connect(**conninfo) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        row = cursor.fetchone()
    return row[0] if row else None


#: URI query options forwarded as MongoDB auth/credential kwargs. Limited to auth-relevant
#: keys so list-valued options (e.g. read-preference tags) are never splatted into
#: ``MongoClient``; verification-disabling options (``tlsAllowInvalidCertificates``,
#: ``tlsInsecure``) are deliberately excluded so a stray URI cannot downgrade TLS.
_MONGO_AUTH_OPTION_KEYS = frozenset(
    {
        "authsource",
        "authmechanism",
        "authmechanismproperties",
        "tls",
        "ssl",
        "tlscafile",
        "tlscertificatekeyfile",
    }
)


def _mongo_connect_params(address: str) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Build the positional/keyword arguments for a MongoDB client connection.

    A connection URI in ``EXTENSIONS_MONGO_URI``/``MONGO_URI`` supplies *credentials and auth
    options only* -- the host and port always come from the requested service ``address``
    so that, in a multi-instance inventory, each service is probed at its own address
    rather than every service hitting the URI's host. A ``mongodb+srv://`` URI cannot be
    repointed to a fixed host/port (its target is DNS-derived), so it is used verbatim.

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :return: A ``(args, kwargs)`` pair to pass to ``pymongo.MongoClient``.
    :rtype: tuple[tuple[str, ...], dict[str, Any]]
    """
    host, port = parse_host_port(address, default_port=DefaultPort.MONGODB)
    uri = os.environ.get("EXTENSIONS_MONGO_URI") or os.environ.get("MONGO_URI")
    if not uri:
        return (), {"host": host, "port": port}
    split = urlsplit(uri)
    if split.scheme == "mongodb+srv":
        # SRV target is DNS-derived, not repointable. Use it only when its host matches
        # the requested service; otherwise it is a different cluster -- connect plainly.
        if split.hostname == host:
            return (uri,), {}
        return (), {"host": host, "port": port}
    kwargs = {"host": host, "port": port}
    if split.username:
        kwargs["username"] = unquote(split.username)
    if split.password:
        kwargs["password"] = unquote(split.password)
    for key, values in parse_qs(split.query).items():
        if key.lower() in _MONGO_AUTH_OPTION_KEYS and values:
            kwargs[key] = values[0]
    return (), kwargs


def _collect_mongodb_version(address: str) -> str | None:
    """Connect to a MongoDB service and return its engine version.

    Connects to the requested ``address``; a ``EXTENSIONS_MONGO_URI``/``MONGO_URI`` env var, when
    set, supplies credentials and auth options (see :func:`_mongo_connect_params`).

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :return: The MongoDB ``buildInfo`` version, or ``None``.
    :rtype: str | None
    """
    import pymongo  # noqa: PLC0415 - optional mongodb driver

    args, kwargs = _mongo_connect_params(address)
    client = pymongo.MongoClient(
        *args, serverSelectionTimeoutMS=DB_CONNECT_TIMEOUT * 1000, **kwargs
    )
    try:
        info = client.admin.command("buildInfo")
    finally:
        client.close()
    return info.get("version")


def collect_service_version(address: str, service_type: str | None) -> str | None:
    """Collect the database engine version for a service, best-effort.

    Dispatches to the per-engine collector by service type. An unknown or missing type
    is skipped; any collector failure (missing creds, unavailable driver, unreachable
    host) is logged and yields ``None`` so siblings and host facts are unaffected.

    :param address: The service address in ``host[:port]`` form.
    :type address: str
    :param service_type: The engine type; one of :class:`ServiceType`'s values.
    :type service_type: str | None
    :return: The engine version string, or ``None``.
    :rtype: str | None
    """
    try:
        engine = ServiceType(service_type)
    except ValueError:
        return None
    collectors = {
        ServiceType.MYSQL: _collect_mysql_version,
        ServiceType.POSTGRESQL: _collect_postgresql_version,
        ServiceType.MONGODB: _collect_mongodb_version,
    }
    try:
        return collectors[engine](address)
    except Exception as err:  # noqa: BLE001 - best-effort collection, never fatal
        logger.warning(
            "Failed to collect version for %s (%s): %s",
            address,
            service_type,
            _redact_secrets(str(err)),
        )
    return None


def main() -> None:
    """Parse the config, collect facts, and emit one JSON document to stdout."""
    parser = argparse.ArgumentParser(
        description="Collect host and service system facts.",
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Path to JSON config file",
        type=Path,
    )
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        logger.warning("Cannot read config file: %s", _redact_secrets(str(err)))
        config = {}
    if not isinstance(config, dict):
        logger.warning("Config is not a JSON object; treating as empty.")
        config = {}

    result = {"host": None, "services": {}}

    if config.get("collect_host"):
        facts = collect_host_facts()
        if any(facts.get(field) is not None for field in HOST_FIELDS):
            result["host"] = facts

    services = config.get("services", [])
    if not isinstance(services, list):
        services = []
    for service in services:
        if not isinstance(service, dict):
            continue
        address = service.get("address")
        service_type = service.get("type")
        if not address:
            continue
        if version := collect_service_version(address, service_type):
            result["services"][address] = {
                "db_engine_version": version,
                "collected_at": _now_iso(),
            }

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
