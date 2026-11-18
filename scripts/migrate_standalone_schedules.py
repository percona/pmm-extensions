#!/usr/bin/env python3
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

"""Recreate a standalone v0.13.1 instance's MySQL backup schedules in PMM Extensions.

The script has two modes. ``export`` runs inside a container of the standalone
install and prints its MySQL Backups schedules, their tasks and its MySQL
inventory services as one JSON document. ``migrate`` reads that document and
recreates each schedule through the PMM Extensions API, paused.

``migrate`` sends no write request unless ``--apply`` is given. Without it the
script proposes a match from each old service to a PMM service, writes the
matches to a file when that file does not exist, and prints the plan. With
``--apply`` it creates only what an existing match file matches, and a second
run creates only what is still missing. Backup history, settings, secrets and
users are not moved.

``export`` uses the standard library only, because it runs on the standalone
image's Python. ``migrate`` also needs PyYAML, to read the stored backup
settings.

Exit codes: 0 done, 2 usage, 3 a precondition failed before any write,
4 stopped part-way.

Usage::

    migrate_standalone_schedules.py export > standalone-export.json
    migrate_standalone_schedules.py migrate --export standalone-export.json
        --pmm-url https://pmm.example.com --pmm-token-file pmm.token
        --matches matches.json [--apply] [--pmm-ca-file FILE | --insecure-skip-tls-verify]
"""

import argparse
import contextlib
import http.client
import importlib
import json
import os
import ssl
import sys
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

EXPORT_FORMAT = "standalone-sep-export"
EXPORT_VERSION = 1

_EXIT_USAGE = 2
_EXIT_PRECONDITION = 3
_EXIT_PARTIAL = 4
_PAGE_SIZE = 50
_OLD_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 60
_API_PREFIX = "/extensions/api"

_SETTINGS_FIELDS = frozenset(
    {
        "awscli_s3_upload_extra_args",
        "backup_dir",
        "binlog_alternative_host",
        "binlog_cmd",
        "binlog_compress_cmd",
        "binlog_extra_args",
        "binlog_prefix",
        "binlog_purge_days",
        "binlog_run_all",
        "check_disk_space",
        "compress",
        "compression_algorithm",
        "defaults_file",
        "encrypt",
        "encrypt_using_tmpdir",
        "encryption_format",
        "gs_bucket",
        "hardlink",
        "logging_dir",
        "mydumper_daily_purge",
        "mydumper_desync_pxc",
        "mydumper_dump_triggers",
        "mydumper_extra_args",
        "mydumper_use_numa",
        "mydumper_verbose",
        "mydumper_weekly_purge",
        "only_if_read_only",
        "only_if_running_replica",
        "post_run_encrypt",
        "rsync_path",
        "s3_bucket",
        "s3_storage_class",
        "skip_s3_safety_check",
        "upload_quiet",
        "use_ftwrl_guardian",
        "xtrabackup_aes256_keyfile",
        "xtrabackup_bin_cmd",
        "xtrabackup_copies",
        "xtrabackup_defaults_file",
        "xtrabackup_desync_pxc",
        "xtrabackup_extra_args",
        "xtrabackup_incremental_cycle",
        "xtrabackup_incremental_method",
        "xtrabackup_kill_queries",
        "xtrabackup_kill_queries_timeout",
        "xtrabackup_kill_query_type",
        "xtrabackup_lock_ddl",
        "xtrabackup_local_ssh_destination",
        "xtrabackup_prepare",
        "xtrabackup_prepare_memory",
        "xtrabackup_quiet",
        "xtrabackup_replica_info",
        "xtrabackup_rsync",
        "xtrabackup_stop_replica",
        "xtrabackup_verify",
    }
)
_TOP_LEVEL_KEYS = frozenset({"ALL_SERVERS", "SERVER_LIST"})
_SERVER_KEYS = frozenset(
    {"ALIAS", "BACKUP_TYPE", "HOST", "PORT", "UPLOAD", "DIR_ENCRYPT_CONFIG"}
)
_RECIPIENT_KEY = "encryption recipient"
_BACKUP_SUBDIRECTORY = {"M": "mydumper", "X": "xtrabackup", "B": "binlogs"}
_UPLOAD_SETTING = {"rsync": "RSYNC_PATH", "s3": "S3_BUCKET", "gsutil": "GS_BUCKET"}
_FIELDS_NOT_CARRIED = ("execute_request",)
_FIELDS_CARRIED_WHEN_SET = ("start_time", "description")
_CRONTAB_KEYS = frozenset(
    {"minute", "hour", "day_of_week", "day_of_month", "month_of_year", "timezone"}
)
_INTERVAL_KEYS = frozenset({"every", "period"})


class MigrationError(Exception):
    """A failure that ends the run with its own exit code."""

    exit_code = 1


class UsageError(MigrationError):
    """The command line, or the environment the script runs in, is wrong."""

    exit_code = _EXIT_USAGE


class PreconditionError(MigrationError):
    """Something is wrong before any write request was sent."""

    exit_code = _EXIT_PRECONDITION


class ConnectionFailedError(PreconditionError):
    """A server could not be reached, or its certificate was not accepted."""


class PartialError(MigrationError):
    """A write was rejected after the run had started."""

    exit_code = _EXIT_PARTIAL


class SkipScheduleError(Exception):
    """A schedule cannot be recreated; the message is the reason shown to the operator."""


@dataclass(frozen=True)
class Request:
    """One HTTP request."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    """One HTTP response."""

    status: int
    body: bytes

    def json(self) -> Any:
        """Return the body parsed as JSON, or ``None`` when it is not JSON."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None


Http = Callable[[Request], Response]


@dataclass(frozen=True)
class OldService:
    """A MySQL service of the old instance's inventory. The address is its node's."""

    name: str
    external_id: str | None
    address: str | None
    port: int | None


@dataclass(frozen=True)
class PmmService:
    """A MySQL service of PMM Extensions' inventory. The address is its node's."""

    id: int
    name: str
    external_id: str | None
    address: str | None
    port: int | None
    last_synced_at: str | None


@dataclass(frozen=True)
class Match:
    """The PMM service proposed for one old service, or ``None`` with the reason."""

    old: OldService
    pmm: PmmService | None
    reason: str


@dataclass(frozen=True)
class OldExport:
    """What ``export`` wrote: schedules, the tasks they run, and the inventory services."""

    schedules: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    services: list[OldService]


@dataclass(frozen=True)
class PmmState:
    """What PMM Extensions holds before the run.

    ``tasks`` maps each MySQL Backups task name to its service ID. ``schedules``
    maps a task name to the ``interval`` and ``crontab`` of each of its schedules.
    """

    services: list[PmmService]
    tasks: dict[str, int | None]
    schedules: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class PlannedSchedule:
    """One schedule to create: the old schedule's name and the request body."""

    old_name: str
    body: dict[str, Any]
    was_enabled: bool


@dataclass(frozen=True)
class PlannedTask:
    """One task and the schedules to create for it.

    ``create_task`` is false when PMM Extensions already has the task, which is
    what an earlier run leaves when it stops between the two writes.
    """

    name: str
    service: PmmService
    form: dict[str, Any]
    create_task: bool
    schedules: list[PlannedSchedule]


@dataclass(frozen=True)
class BackupLocation:
    """Where one old task wrote its backups, and where it copied them."""

    service: str
    task: str
    directory: str | None
    upload: str | None


@dataclass(frozen=True)
class Plan:
    """What a run would do.

    ``skipped`` holds ``(old schedule name, reason)``. ``already_present``
    holds the old names of schedules PMM Extensions already has.
    """

    to_create: list[PlannedTask]
    already_present: list[str]
    skipped: list[tuple[str, str]]
    backup_locations: list[BackupLocation]

    def schedule_count(self) -> int:
        """Return how many schedules the plan creates."""
        return sum(len(task.schedules) for task in self.to_create)


def _yaml() -> Any:
    """Return the PyYAML module.

    :raises UsageError: When PyYAML is not installed for this Python.
    """
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:
        message = (
            "PyYAML is not installed for this Python. Install it with 'python3 -m pip install pyyaml', "
            "or run the script in the percona/pmm-extensions image."
        )
        raise UsageError(message) from exc


def tls_context(*, ca_file: Path | None, insecure: bool) -> ssl.SSLContext:
    """Build the TLS settings for every connection of a run.

    The certificate and the host name are checked unless ``insecure`` is true.

    :param ca_file: A PEM file to trust instead of the system's certificates.
    :param insecure: Skip the certificate and host name checks.
    :return: The context.
    :raises UsageError: When ``ca_file`` cannot be read as certificates.
    """
    try:
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    except (OSError, ssl.SSLError) as exc:
        raise UsageError(f"cannot read certificates from {ca_file}: {exc}") from exc
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def make_sender(context: ssl.SSLContext) -> Http:
    """Return the function that sends a request over the network.

    :param context: The TLS settings for ``https`` URLs.
    :return: A function from a request to its response, whatever the status.
    """

    def send(request: Request) -> Response:
        url = urllib.parse.urlsplit(request.url)
        host = url.hostname or ""
        connection: http.client.HTTPConnection
        if url.scheme == "https":
            connection = http.client.HTTPSConnection(
                host, url.port, context=context, timeout=_TIMEOUT_SECONDS
            )
        else:
            connection = http.client.HTTPConnection(
                host, url.port, timeout=_TIMEOUT_SECONDS
            )
        target = url.path + (f"?{url.query}" if url.query else "")
        try:
            connection.request(
                request.method, target, body=request.body, headers=request.headers
            )
            answer = connection.getresponse()
            return Response(status=answer.status, body=answer.read())
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectionFailedError(
                f"cannot reach {url.scheme}://{url.netloc}: {exc}"
            ) from exc
        finally:
            connection.close()

    return send


def _detail(response: Response) -> str:
    """Return the server's own error text, or a short piece of the body."""
    payload = response.json()
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return response.body[:200].decode("utf-8", errors="replace")


class Api:
    """A JSON API reached with one bearer token."""

    def __init__(self, http: Http, base_url: str, token: str, name: str) -> None:
        """Remember where the API is and how to call it.

        :param http: The function that sends a request.
        :param base_url: The URL every path is appended to.
        :param token: The bearer token.
        :param name: What to call the server in an error message.
        """
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        self._name = name

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        """Return the JSON body of a GET.

        :param path: The path under the base URL.
        :param query: The query parameters.
        :return: The parsed body.
        :raises PreconditionError: When the answer is not HTTP 200 with a JSON body.
        """
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        response = self._http(Request(method="GET", url=url, headers=self._headers))
        if response.status != HTTPStatus.OK:
            hint = (
                " Check the token."
                if response.status == HTTPStatus.UNAUTHORIZED
                else ""
            )
            message = f"{self._name} answered HTTP {response.status} for GET {path}: {_detail(response)}.{hint}"
            raise PreconditionError(message)
        payload = response.json()
        if payload is None:
            raise PreconditionError(
                f"{self._name} answered GET {path} with a body that is not JSON"
            )
        return payload

    def get_all(
        self, path: str, query: dict[str, Any], page_size: int
    ) -> list[dict[str, Any]]:
        """Return every item of a paged list.

        :param path: The path under the base URL.
        :param query: The query parameters besides ``limit`` and ``offset``.
        :param page_size: The ``limit`` to ask for.
        :return: The items of all pages.
        :raises PreconditionError: When a page is not a paged list, or the list ends before its total.
        """
        items: list[dict[str, Any]] = []
        while True:
            page = self.get(path, {**query, "limit": page_size, "offset": len(items)})
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise PreconditionError(
                    f"{self._name} answered GET {path} with something that is not a paged list"
                )
            items.extend(page["items"])
            total = page.get("total", len(items))
            if len(items) >= total:
                return items
            if not page["items"]:
                raise PreconditionError(
                    f"{self._name} listed {len(items)} of {total} items for GET {path}"
                )

    def post(self, path: str, body: dict[str, Any]) -> Response:
        """Send a POST with a JSON body and return the response, whatever its status.

        :param path: The path under the base URL.
        :param body: The JSON body.
        :return: The response.
        """
        headers = {**self._headers, "Content-Type": "application/json"}
        request = Request(
            method="POST",
            url=f"{self._base_url}{path}",
            headers=headers,
            body=json.dumps(body).encode(),
        )
        return self._http(request)


def _require(condition: object, problem: str) -> None:
    """Raise :class:`PreconditionError` for a malformed export unless ``condition`` holds."""
    if not condition:
        raise PreconditionError(
            f"the export file does not have the expected shape: {problem}"
        )


def _paged_items(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the items of one paged list of the export, checked to be whole."""
    page = document.get(key)
    _require(
        isinstance(page, dict) and isinstance(page.get("items"), list),
        f"'{key}' is not a paged list",
    )
    items: list[dict[str, Any]] = page["items"]
    total = page.get("total")
    _require(isinstance(total, int), f"'{key}' has no total")
    if total > len(items):
        raise PreconditionError(
            f"the export file is truncated: '{key}' holds {len(items)} of {total} items"
        )
    _require(
        all(isinstance(item, dict) for item in items),
        f"'{key}' holds something that is not an object",
    )
    return items


def _check_schedule(schedule: Any) -> None:
    """Check one exported schedule: a name, a task, and an interval or a crontab."""
    _require(isinstance(schedule, dict), "a schedule is not an object")
    _require(isinstance(schedule.get("name"), str), "a schedule has no name")
    name = schedule["name"]
    _require(isinstance(schedule.get("task"), str), f"schedule {name} has no task")
    interval, crontab = schedule.get("interval"), schedule.get("crontab")
    _require(
        (interval is None) != (crontab is None),
        f"schedule {name} needs an interval or a crontab, and not both",
    )
    if interval is not None:
        _require(
            isinstance(interval, dict) and set(interval) == _INTERVAL_KEYS,
            f"schedule {name} has an odd interval",
        )
    else:
        _require(
            isinstance(crontab, dict) and set(crontab) == _CRONTAB_KEYS,
            f"schedule {name} has an odd crontab",
        )


def _check_task(task: dict[str, Any]) -> None:
    """Check one exported task: a name and stored settings."""
    _require(isinstance(task.get("name"), str), "a task has no name")
    data = task.get("data")
    meta = data.get("meta") if isinstance(data, dict) else None
    _require(isinstance(meta, dict), f"task {task['name']} has no data.meta")
    _require(
        isinstance(meta.get("config"), str),
        f"task {task['name']} has no stored settings",
    )


def _old_service_from(item: dict[str, Any]) -> OldService:
    """Build an :class:`OldService` from one exported inventory service."""
    _require(isinstance(item.get("name"), str), "a service has no name")
    node = item.get("node") if isinstance(item.get("node"), dict) else {}
    return OldService(
        name=item["name"],
        external_id=item.get("external_id") or None,
        address=node.get("address") or None,
        port=item.get("port"),
    )


def pmm_service_from(item: dict[str, Any]) -> PmmService:
    """Build a :class:`PmmService` from one item of PMM Extensions' service list.

    :param item: The item as the API returns it.
    :return: The service, with its node's address.
    """
    node = item.get("node") if isinstance(item.get("node"), dict) else {}
    return PmmService(
        id=item["id"],
        name=item["name"],
        external_id=item.get("external_id") or None,
        address=node.get("address") or None,
        port=item.get("port"),
        last_synced_at=item.get("last_synced_at"),
    )


def parse_export(text: str) -> OldExport:
    """Read and check the document that ``export`` wrote.

    :param text: The file's contents.
    :return: The schedules, tasks and services.
    :raises PreconditionError: When the text is not JSON, does not have the
        export's shape, or holds a paged list with fewer items than its total.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise PreconditionError(f"the export file is not JSON: {exc}") from exc
    _require(isinstance(document, dict), "it is not a JSON object")
    _require(
        document.get("format") == EXPORT_FORMAT, f"'format' is not '{EXPORT_FORMAT}'"
    )
    _require(
        document.get("version") == EXPORT_VERSION, f"'version' is not {EXPORT_VERSION}"
    )
    schedules = document.get("schedules")
    _require(isinstance(schedules, list), "'schedules' is not a list")
    for schedule in schedules:
        _check_schedule(schedule)
    tasks = _paged_items(document, "tasks")
    for task in tasks:
        _check_task(task)
    services = [_old_service_from(item) for item in _paged_items(document, "services")]
    return OldExport(schedules=schedules, tasks=tasks, services=services)


def _propose(old: OldService, pmm: list[PmmService]) -> Match:
    """Propose the PMM service for one old service."""
    by_id = [
        service
        for service in pmm
        if old.external_id and service.external_id == old.external_id
    ]
    if len(by_id) == 1:
        return Match(old, by_id[0], "same external_id")
    if old.address is None or old.port is None:
        return Match(
            old,
            None,
            "no PMM service has this external_id, and the old service has no node address or port",
        )
    by_address = [
        service
        for service in pmm
        if (service.address, service.port) == (old.address, old.port)
    ]
    if len(by_address) == 1:
        return Match(old, by_address[0], "same node address and port")
    if not by_address:
        return Match(
            old,
            None,
            "no PMM service has this external_id or this node address and port",
        )
    return Match(
        old,
        None,
        f"{len(by_address)} PMM services share node address {old.address} and port {old.port}",
    )


def propose_matches(old: list[OldService], pmm: list[PmmService]) -> list[Match]:
    """Propose a PMM service for each old service.

    A PMM service with the same ``external_id`` is the match. Otherwise the
    one PMM service with the same node address and port is. Two candidates, or
    none, leave the old service unmatched.

    :param old: The old instance's MySQL services.
    :param pmm: PMM Extensions' MySQL services.
    :return: One match per old service, in the order given.
    :raises PreconditionError: When two old services share a name, because the
        match file is keyed by name.
    """
    names = [service.name for service in old]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        message = f"the old inventory has more than one MySQL service named {', '.join(repeated)}; rename one first"
        raise PreconditionError(message)
    return [_propose(service, pmm) for service in old]


def write_match_file(path: Path, matches: list[Match]) -> None:
    """Write the proposed matches as JSON, keyed by the old service's name.

    Each entry shows what the operator checks: the old service's identifiers,
    and the PMM service's ID, name, node address, port and last sync time.

    :param path: The file to write.
    :param matches: The proposed matches.
    """
    document = {
        match.old.name: {
            "old": {
                "external_id": match.old.external_id,
                "node_address": match.old.address,
                "port": match.old.port,
            },
            "pmm_service_id": match.pmm.id if match.pmm else None,
            "pmm": {
                "name": match.pmm.name,
                "node_address": match.pmm.address,
                "port": match.pmm.port,
                "last_synced_at": match.pmm.last_synced_at,
            }
            if match.pmm
            else None,
            "reason": match.reason,
        }
        for match in matches
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def read_match_file(path: Path) -> dict[str, int]:
    """Read the match file and return its matches.

    Only ``pmm_service_id`` is read from an entry. The other keys are there for
    the operator.

    :param path: The file to read.
    :return: The PMM service ID for each old service name that has one.
    :raises PreconditionError: When the file is not JSON, or an entry has no
        ``pmm_service_id`` that is a number or ``null``.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PreconditionError(f"the match file {path} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PreconditionError(
            f"the match file {path} is not a JSON object keyed by service name"
        )
    matches: dict[str, int] = {}
    for name, entry in document.items():
        service_id = entry.get("pmm_service_id", "") if isinstance(entry, dict) else ""
        if service_id is None:
            continue
        if isinstance(service_id, bool) or not isinstance(service_id, int):
            message = f"the match file {path}: the entry for {name} needs a pmm_service_id that is a number or null"
            raise PreconditionError(message)
        matches[name] = service_id
    return matches


def _unknown(keys: Any, known: frozenset[str]) -> str:
    """Return the keys that are not in ``known``, sorted and joined, or an empty string."""
    return ", ".join(sorted(str(key) for key in keys if key not in known))


def _stored_settings(task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse a task's stored settings into the common part and the one server entry.

    PMM Extensions 3.10.0 drops a form field it does not know without an
    error, so any key this script does not know is refused here.

    :raises SkipScheduleError: When the settings are not the shape v0.13.1 writes.
    """
    yaml = _yaml()
    try:
        settings = yaml.safe_load(task["data"]["meta"]["config"])
    except yaml.YAMLError as exc:
        raise SkipScheduleError(
            f"the settings of task {task['name']} are not valid YAML"
        ) from exc
    if not isinstance(settings, dict):
        raise SkipScheduleError(
            f"the settings of task {task['name']} are not a mapping"
        )
    common, servers = settings.get("ALL_SERVERS") or {}, settings.get("SERVER_LIST")
    if (
        not isinstance(common, dict)
        or not isinstance(servers, list)
        or len(servers) != 1
    ):
        raise SkipScheduleError(
            f"the settings of task {task['name']} do not have one ALL_SERVERS and one server"
        )
    server = servers[0]
    if not isinstance(server, dict):
        raise SkipScheduleError(
            f"the settings of task {task['name']} have a server entry that is not a mapping"
        )
    encryption = server.get("DIR_ENCRYPT_CONFIG") or {}
    unknown = ", ".join(
        part
        for part in (
            _unknown(settings, _TOP_LEVEL_KEYS),
            _unknown(common, frozenset(field.upper() for field in _SETTINGS_FIELDS)),
            _unknown(server, _SERVER_KEYS),
            _unknown(encryption, frozenset({_RECIPIENT_KEY}))
            if isinstance(encryption, dict)
            else "DIR_ENCRYPT_CONFIG",
        )
        if part
    )
    if unknown:
        message = (
            f"task {task['name']} uses settings this script does not know ({unknown}); "
            "PMM Extensions would drop them without an error"
        )
        raise SkipScheduleError(message)
    return common, server


def task_form(task: dict[str, Any], pmm_service_id: int) -> dict[str, Any]:
    """Build the body that creates a v0.13.1 task in PMM Extensions.

    Every ``ALL_SERVERS`` key becomes the form field of the same name in lower
    case. The rest comes from the task's name, its host, and its server entry.
    A setting stored as empty is left out, so PMM Extensions uses its default.

    :param task: The task as the old instance's API returns it.
    :param pmm_service_id: The ID of the matched service in PMM Extensions.
    :return: The JSON body for the MySQL Backups create call.
    :raises SkipScheduleError: When the task cannot be carried over.
    """
    common, server = _stored_settings(task)
    name, meta = task["name"], task["data"]["meta"]
    if not common.get("BACKUP_DIR"):
        raise SkipScheduleError(
            f"task {name} has no BACKUP_DIR, which PMM Extensions requires"
        )
    if not server.get("BACKUP_TYPE") or not meta.get("target"):
        raise SkipScheduleError(f"task {name} has no BACKUP_TYPE or no host to run on")
    form = {
        key.lower(): value
        for key, value in common.items()
        if value is not None and value != ""
    }
    form.update(
        task_name=name,
        hostname=meta["target"],
        service_id=pmm_service_id,
        backup_type=server["BACKUP_TYPE"],
        upload=server.get("UPLOAD") or [],
        alert_on_fail=bool(task.get("alert_on_fail", False)),
    )
    if server.get("ALIAS"):
        form["alias"] = server["ALIAS"]
    recipient = (server.get("DIR_ENCRYPT_CONFIG") or {}).get(_RECIPIENT_KEY)
    if recipient:
        form["encryption_recipient"] = recipient
    return form


def _backup_location(task: dict[str, Any]) -> BackupLocation | None:
    """Return where one old task wrote backups, or ``None`` when its settings cannot be read."""
    try:
        common, server = _stored_settings(task)
    except SkipScheduleError:
        return None
    meta = task["data"]["meta"]
    alias = server.get("ALIAS") or meta.get("_connectivity_host")
    subdirectory = _BACKUP_SUBDIRECTORY.get(str(server.get("BACKUP_TYPE")))
    directory = None
    if common.get("BACKUP_DIR") and subdirectory and alias:
        directory = f"{str(common['BACKUP_DIR']).rstrip('/')}/{subdirectory}/{alias}"
    uploads = [
        f"{provider}: {common.get(_UPLOAD_SETTING.get(provider, ''), 'destination not stored')}"
        for provider in server.get("UPLOAD") or []
    ]
    return BackupLocation(
        service=str(meta.get("_service_name", "unknown service")),
        task=task["name"],
        directory=directory,
        upload=", ".join(uploads) or None,
    )


def _resolve_task(
    task: dict[str, Any], export: OldExport, matches: dict[str, int], state: PmmState
) -> PlannedTask:
    """Decide how one old task is created, with no schedules yet.

    :raises SkipScheduleError: When nothing may be created for the task.
    """
    name = task["name"]
    service_name = task["data"]["meta"].get("_service_name")
    if not service_name:
        raise SkipScheduleError(f"task {name} does not name a service")
    if all(service.name != service_name for service in export.services):
        raise SkipScheduleError(
            f"service {service_name} of task {name} is no longer in the old inventory"
        )
    if service_name not in matches:
        raise SkipScheduleError(
            f"service {service_name} has no match in the match file"
        )
    service = next(
        (item for item in state.services if item.id == matches[service_name]), None
    )
    if service is None:
        message = (
            f"the match file gives service {service_name} the PMM service ID {matches[service_name]}, "
            "which PMM Extensions does not list as a MySQL service"
        )
        raise SkipScheduleError(message)
    form = task_form(task, service.id)
    exists = name in state.tasks
    if exists and state.tasks[name] != service.id:
        message = f"PMM Extensions already has a task named {name}, on service ID {state.tasks[name]}"
        raise SkipScheduleError(message)
    return PlannedTask(
        name=name, service=service, form=form, create_task=not exists, schedules=[]
    )


def _timing(schedule: dict[str, Any]) -> dict[str, Any]:
    """Return the ``interval`` and ``crontab`` of a schedule, which identify it within its task."""
    return {"interval": schedule.get("interval"), "crontab": schedule.get("crontab")}


def _schedule_body(schedule: dict[str, Any]) -> dict[str, Any]:
    """Build the body that creates one schedule, paused.

    The interval or crontab is copied unchanged, and so are a start time and a
    description when the old schedule has them.

    :raises SkipScheduleError: When the schedule sets a field this script does not carry over.
    """
    for field in _FIELDS_NOT_CARRIED:
        if schedule.get(field) is not None:
            raise SkipScheduleError(
                f"the schedule sets {field}, which this script does not carry over"
            )
    body = {key: value for key, value in _timing(schedule).items() if value is not None}
    for field in _FIELDS_CARRIED_WHEN_SET:
        if schedule.get(field):
            body[field] = schedule[field]
    body["enabled"] = False
    return body


def build_plan(export: OldExport, matches: dict[str, int], state: PmmState) -> Plan:
    """Decide what to create in PMM Extensions.

    A schedule is planned only when its task's service is matched in
    ``matches`` and PMM Extensions does not already have the same schedule
    for a task of that name. Every planned schedule has ``enabled`` false.

    :param export: The old instance's schedules, tasks and services.
    :param matches: The PMM service ID for each matched old service name.
    :param state: What PMM Extensions holds now.
    :return: The plan, with a reason for every schedule left out.
    """
    tasks = {task["name"]: task for task in export.tasks}
    resolved: dict[str, PlannedTask | SkipScheduleError] = {}
    seen: dict[str, list[dict[str, Any]]] = {}
    plan = Plan(to_create=[], already_present=[], skipped=[], backup_locations=[])
    for schedule in export.schedules:
        name, task_name = schedule["name"], schedule["task"]
        if task_name in tasks and task_name not in resolved:
            try:
                resolved[task_name] = _resolve_task(
                    tasks[task_name], export, matches, state
                )
            except SkipScheduleError as exc:
                resolved[task_name] = exc
        planned = resolved.get(task_name)
        if not isinstance(planned, PlannedTask):
            reason = planned or f"its task {task_name} is not in the export"
            plan.skipped.append((name, str(reason)))
            continue
        try:
            body = _schedule_body(schedule)
        except SkipScheduleError as exc:
            plan.skipped.append((name, str(exc)))
            continue
        timing = _timing(schedule)
        if timing in seen.setdefault(task_name, []):
            plan.skipped.append(
                (name, f"it repeats another schedule of task {task_name}")
            )
        elif timing in state.schedules.get(task_name, []):
            plan.already_present.append(name)
        else:
            planned.schedules.append(
                PlannedSchedule(name, body, was_enabled=bool(schedule.get("enabled")))
            )
        seen[task_name].append(timing)
    plan.to_create.extend(
        task
        for task in resolved.values()
        if isinstance(task, PlannedTask) and task.schedules
    )
    plan.backup_locations.extend(
        location for task in export.tasks if (location := _backup_location(task))
    )
    return plan


def read_pmm_state(api: Api) -> PmmState:
    """Read what PMM Extensions holds: MySQL services, MySQL Backups tasks, their schedules.

    The schedule list has no owner filter and includes the system's own
    schedules, so it is cut down to the schedules of MySQL Backups tasks.

    :param api: The PMM Extensions API.
    :return: The state.
    :raises PreconditionError: When a list cannot be read in full.
    """
    services = api.get_all(
        "/extensions/services/", {"service_type": "mysql"}, _PAGE_SIZE
    )
    tasks = api.get_all("/apps/mysql_backups/", {}, _PAGE_SIZE)
    schedules = api.get_all("/extensions/periodic-tasks/", {}, _PAGE_SIZE)
    task_services: dict[str, int | None] = {}
    for task in tasks:
        data = task.get("data") if isinstance(task.get("data"), dict) else {}
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        task_services[task["name"]] = meta.get("_service_id")
    by_task: dict[str, list[dict[str, Any]]] = {}
    for schedule in schedules:
        if schedule.get("task") in task_services:
            by_task.setdefault(schedule["task"], []).append(_timing(schedule))
    return PmmState(
        services=[pmm_service_from(item) for item in services],
        tasks=task_services,
        schedules=by_task,
    )


def _count(number: int, noun: str) -> str:
    """Return ``number`` with ``noun``, in the plural unless it is one."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _describe_timing(body: dict[str, Any]) -> str:
    """Return a schedule's interval or crontab as one short phrase."""
    if body.get("interval"):
        return f"every {body['interval']['every']} {body['interval']['period']}"
    cron = body["crontab"]
    fields = " ".join(
        str(cron[key])
        for key in ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")
    )
    return f"cron '{fields}' in {cron['timezone']}"


def _print_matches(
    path: Path, matches: dict[str, int], export: OldExport, state: PmmState
) -> None:
    """Print each old service with the PMM service the match file gives it."""
    services = {service.id: service for service in state.services}
    print(f"Matches, from {path}:")
    for old in export.services:
        service = services.get(matches.get(old.name, -1))
        if old.name not in matches:
            print(f"  {old.name}: no match")
        elif service is None:
            print(
                f"  {old.name}: PMM service ID {matches[old.name]}, which PMM Extensions does not list"
            )
        else:
            print(
                f"  {old.name}: PMM service {service.id}, {service.name}, "
                f"node {service.address}, port {service.port}, last synced {service.last_synced_at}"
            )


def _print_plan(plan: Plan) -> None:
    """Print what a run creates, what it leaves out and why, and the old backup directories."""
    print(f"\n{_count(plan.schedule_count(), 'schedule')} to create, each paused:")
    for task in plan.to_create:
        state = "new task" if task.create_task else "task already in PMM"
        print(
            f"  {task.name} on {task.service.name} (PMM service {task.service.id}), {state}"
        )
        for schedule in task.schedules:
            note = "" if schedule.was_enabled else ", paused on the old instance too"
            print(f"    {_describe_timing(schedule.body)}{note}")
    print(f"\n{_count(len(plan.skipped), 'schedule')} skipped:")
    for name, reason in plan.skipped:
        print(f"  {name}: {reason}")
    print(f"\n{_count(len(plan.already_present), 'schedule')} already in PMM.")
    print(
        "\nWhere the old instance wrote backups. The newest one is under 'latest' in each directory:"
    )
    for location in plan.backup_locations:
        directory = location.directory or "the task stores no backup directory"
        copy = f"; copied to {location.upload}" if location.upload else ""
        print(f"  {location.service}, task {location.task}: {directory}{copy}")


def _stop(created: int, plan: Plan, reason: str) -> PartialError:
    """Print how far a run got and return the error that ends it."""
    print(
        f"\nStopped: created {created} of {_count(plan.schedule_count(), 'schedule')}."
    )
    return PartialError(
        f"{reason}. Fix the cause and run the same command again: it creates only what is missing."
    )


def _write(api: Api, path: str, body: dict[str, Any], what: str) -> str | None:
    """Send one create request and return ``None``, or why it failed."""
    try:
        response = api.post(path, body)
    except ConnectionFailedError as exc:
        return f"{what} was not created: {exc}"
    if response.status != HTTPStatus.CREATED:
        return f"{what} was not created: PMM Extensions answered HTTP {response.status}: {_detail(response)}"
    return None


def apply_plan(api: Api, plan: Plan) -> int:
    """Create the plan's tasks and schedules, a task before its schedules.

    :param api: The PMM Extensions API.
    :param plan: The plan to carry out.
    :return: The number of schedules created.
    :raises PartialError: When a write is rejected. Nothing after it is sent.
    """
    created = 0
    for task in plan.to_create:
        if task.create_task:
            failure = _write(
                api, "/apps/mysql_backups/", task.form, f"task {task.name}"
            )
            if failure:
                raise _stop(created, plan, failure)
        path = f"/extensions/periodic-tasks/{urllib.parse.quote(task.name, safe='')}/"
        for schedule in task.schedules:
            failure = _write(
                api, path, schedule.body, f"the schedule {schedule.old_name}"
            )
            if failure:
                raise _stop(created, plan, failure)
            created += 1
    return created


def _read_file(path: Path, what: str) -> str:
    """Return a file's text.

    :raises UsageError: When the file cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read {what} {path}: {exc}") from exc


def _matches(
    args: argparse.Namespace, export: OldExport, state: PmmState
) -> dict[str, int]:
    """Return the matches of the match file, writing the file first when it does not exist."""
    if not args.matches.exists():
        write_match_file(args.matches, propose_matches(export.services, state.services))
        print(
            f"Wrote the proposed matches to {args.matches}. Check each one before you run with --apply."
        )
    return read_match_file(args.matches)


def run_migrate(args: argparse.Namespace, http: Http | None) -> None:
    """Run the ``migrate`` mode.

    :param args: The parsed command line.
    :param http: The function that sends a request, or ``None`` for the network.
    :raises MigrationError: When the run cannot finish.
    """
    if args.apply and not args.matches.exists():
        message = f"--apply needs the match file {args.matches}. Run without --apply first, and check the file."
        raise UsageError(message)
    if urllib.parse.urlsplit(args.pmm_url).scheme != "https":
        raise UsageError("--pmm-url must start with https://")
    _yaml()
    export = parse_export(_read_file(args.export, "the export file"))
    token = _read_file(args.pmm_token_file, "the token file").strip()
    sender = http or make_sender(
        tls_context(ca_file=args.pmm_ca_file, insecure=args.insecure_skip_tls_verify)
    )
    api = Api(sender, args.pmm_url.rstrip("/") + _API_PREFIX, token, "PMM Extensions")
    user = api.get("/users/me")
    is_admin = isinstance(user, dict) and user.get("isAdmin") is True
    if args.apply and not is_admin:
        message = (
            f"the token in {args.pmm_token_file} is not an admin's, so PMM Extensions would refuse every write. "
            "Use the token of a service account with the Admin role."
        )
        raise PreconditionError(message)
    state = read_pmm_state(api)
    matches = _matches(args, export, state)
    plan = build_plan(export, matches, state)
    _print_matches(args.matches, matches, export, state)
    _print_plan(plan)
    if not args.apply:
        if not is_admin:
            print(
                "\nNote: this token is not an admin's. A run with --apply needs one that is."
            )
        print(
            "\nNo write request was sent. Run again with --apply to create the schedules."
        )
        return
    if not plan.to_create:
        print("\nNothing to create.")
        return
    created = apply_plan(api, plan)
    total = _count(plan.schedule_count(), "schedule")
    print(f"\nDone: created {created} of {total}. Each one is paused.")


@contextlib.contextmanager
def stdout_to_stderr() -> Iterator[None]:
    """Send everything written to standard output to standard error for a while.

    The standalone application logs to standard output when its settings are
    imported, and standard output is where the export goes.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    try:
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _installed_settings() -> tuple[str, str, str, Path | None]:
    """Read the tasks URL, the inventory URL, the internal token and the CA file from the install.

    This works only inside a container of the standalone install, where the
    application's own settings can be imported. The settings are read while
    standard output is redirected, because reading them logs there.

    :raises UsageError: When the settings cannot be imported or hold no token.
    """
    try:
        with stdout_to_stderr():
            core = importlib.import_module("app.core.config").settings
            sep = importlib.import_module("app.sep.config").sep_settings
            tasks_url, inventory_url = (
                str(sep.TASKS_ENDPOINT),
                str(sep.INVENTORY_ENDPOINT),
            )
            token = core.SEP_INTERNAL_TOKEN.get_secret_value()
            ca_file = Path(str(core.SSL_CAFILE)) if core.SSL_CAFILE else None
    except (ImportError, AttributeError) as exc:
        message = (
            "cannot read the standalone install's settings. Run 'export' inside the install's 'tasks_api' container, "
            f"or pass --tasks-url, --inventory-url and --token-file ({type(exc).__name__}: {exc})"
        )
        raise UsageError(message) from exc
    return tasks_url, inventory_url, token, ca_file


def run_export(args: argparse.Namespace, http: Http | None) -> None:
    """Run the ``export`` mode: print the old instance's three lists as one JSON document.

    Nothing is printed unless all three lists were read in full.

    :param args: The parsed command line.
    :param http: The function that sends a request, or ``None`` for the network.
    :raises MigrationError: When a list cannot be read.
    """
    if args.tasks_url and args.inventory_url and args.token_file:
        tasks_url, inventory_url = args.tasks_url, args.inventory_url
        token, ca_file = (
            _read_file(args.token_file, "the token file").strip(),
            args.ca_file,
        )
    else:
        tasks_url, inventory_url, token, installed_ca = _installed_settings()
        ca_file = args.ca_file or installed_ca
    sender = http or make_sender(tls_context(ca_file=ca_file, insecure=False))
    tasks_api = Api(sender, tasks_url, token, "the tasks service")
    inventory_api = Api(sender, inventory_url, token, "the inventory service")
    schedules = tasks_api.get("/periodic/", {"owner": "BACKUPS"})
    tasks = tasks_api.get_all("/", {"owner": "BACKUPS"}, _OLD_PAGE_SIZE)
    services = inventory_api.get_all(
        "/services/", {"service_type": "mysql"}, _OLD_PAGE_SIZE
    )
    document = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "schedules": schedules,
        "tasks": {"items": tasks, "total": len(tasks)},
        "services": {"items": services, "total": len(services)},
    }
    text = json.dumps(document, indent=2) + "\n"
    parse_export(text)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    summary = f"{_count(len(schedules), 'schedule')}, {_count(len(tasks), 'task')}, {_count(len(services), 'service')}"
    print(f"Exported {summary}.", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        description="Recreate a standalone v0.13.1 instance's MySQL backup schedules in PMM Extensions.",
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    export = modes.add_parser(
        "export",
        help="Print the old instance's schedules, tasks and MySQL services as JSON.",
        description="Run inside the standalone install's 'tasks_api' container. It needs no option there.",
    )
    export.add_argument(
        "--tasks-url", help="URL of the tasks service, when not run inside the install."
    )
    export.add_argument(
        "--inventory-url",
        help="URL of the inventory service, when not run inside the install.",
    )
    export.add_argument(
        "--token-file", type=Path, help="File holding a bearer token for both services."
    )
    export.add_argument(
        "--ca-file", type=Path, help="PEM file to trust for both services."
    )
    export.add_argument(
        "--output", type=Path, help="Write the export here and not to standard output."
    )
    migrate = modes.add_parser(
        "migrate",
        help="Recreate the exported schedules in PMM Extensions, paused.",
        description="Without --apply, nothing is created.",
    )
    migrate.add_argument(
        "--export", type=Path, required=True, help="The file that 'export' wrote."
    )
    migrate.add_argument(
        "--pmm-url",
        required=True,
        help="The PMM server, for example https://pmm.example.com",
    )
    migrate.add_argument(
        "--pmm-token-file",
        type=Path,
        required=True,
        help="File holding the token of a Grafana service account with the Admin role.",
    )
    migrate.add_argument(
        "--matches",
        type=Path,
        required=True,
        help="The match file. It is written when it does not exist, and never overwritten.",
    )
    migrate.add_argument(
        "--apply", action="store_true", help="Create what the match file matches."
    )
    tls = migrate.add_mutually_exclusive_group()
    tls.add_argument(
        "--pmm-ca-file",
        type=Path,
        help="PEM file to trust for the PMM server's certificate.",
    )
    tls.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Do not check the PMM server's certificate. The token is then sent to whoever answers.",
    )
    return parser


def main(argv: list[str], *, http: Http | None = None) -> int:
    """Run the script.

    :param argv: The command line without the program name.
    :param http: The function that sends a request. Tests pass their own.
    :return: The exit code: 0 done, 2 usage, 3 a precondition failed before
        any write, 4 stopped part-way.
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else _EXIT_USAGE
    try:
        if args.mode == "export":
            run_export(args, http)
        else:
            run_migrate(args, http)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
