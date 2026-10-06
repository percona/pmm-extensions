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

"""OM worker probe payload.

Runs on one executor host under the venv the ``run-python`` job template builds,
and collects, for every target named in the task config:

* its server process (pid, uptime, argv, config path) from ``ps``, matched to the
  target by **port** — a host may run several mongods, and each target's facts have
  to be its own;
* the installed version of the binary *that* process runs;
* a handful of database commands, when ``probe_database`` is set.

Once per dispatch, for the host itself:

* its OS and kernel from ``/etc/os-release`` and ``uname``;
* whether it can reach Percona's repository;
* every server process running on it, and which of them PMM has no target for;
* the installed binary version, which is the whole install decision on a host that
  runs no database yet.

Prints **one JSON object per line to stdout** (NDJSON). That is the return channel:
the orchestrator streams the task log back and parses it line by line. It is used in
preference to the ``.pmm-extensions-run-result.json`` file because that file is capped at 16 KB
and silently discarded above it, while the task-log chunk store has no total cap.

The config arrives as JSON in ``NOMAD_META_CONFIG``::

    {
      "targets": [
        {"service": "sc-cfg00", "service_id": "abc123", "host": "sc-cfg00",
         "port": 27019},
        ...
      ],
      "probe_database": true,
      "credentials_path": "/root/.mongodb_uri",   // absent = $HOME/.mongodb_uri
      "auth_source": "admin",
      "connect_timeout_ms": 5000
    }

Every target is attempted even when an earlier one fails: a per-target error becomes
an ``error`` field on that target's record. The process **exits 0 regardless**, so a
partial collection is still readable. A non-zero exit is reserved for "the payload
could not start at all", which is what the orchestrator reports as a dispatch
failure rather than a probe failure.

Beyond the standard library the only import is ``pymongo``, declared as the task's
pip requirements by the dispatcher. It is imported lazily so that a run with
``probe_database`` false works on a host without it.
"""

import json
import os
import platform
import shutil
import subprocess  # nosec B404 - the payload's whole job is asking the host
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote_plus

DEFAULT_AUTH_SOURCE = "admin"
DEFAULT_CONNECT_TIMEOUT_MS = 5000
#: Basename of the node-side credentials file, under ``$HOME``. The same file the
#: PBM payloads and the MongoDB Status payload read.
DEFAULT_CREDENTIALS_BASENAME = ".mongodb_uri"
#: Bound on captured subprocess output. These commands emit a line or two; anything
#: larger means something unexpected ran and is not worth shipping through the log.
MAX_COMMAND_OUTPUT = 8192
#: Bound on how long any one shell probe may take.
COMMAND_TIMEOUT_SEC = 15
STATUS_OK = "ok"
STATUS_FAILED = "failed"


def load_config():
    """Return the task config parsed from ``NOMAD_META_CONFIG``.

    :return: The config mapping.
    """
    raw = os.environ.get("NOMAD_META_CONFIG")
    if not raw:
        print("NOMAD_META_CONFIG is not set", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(raw)
    except ValueError as err:
        print(f"NOMAD_META_CONFIG is not valid JSON: {err}", file=sys.stderr)
        sys.exit(1)


def run_command(args):
    """Run ``args`` and return its trimmed stdout, or ``None`` on any failure.

    Every shell probe here is best-effort: a missing binary, a non-zero exit, or a
    timeout yields ``None`` rather than aborting the record. The point is to collect
    what this host can tell us, not to insist it tells us everything.

    :param args: The argv list to execute.
    :return: The command's stdout stripped of surrounding whitespace, or ``None``.
    """
    try:
        completed = subprocess.run(  # nosec B603 - argv is a literal list, never a shell string, and every element is a constant from SERVER_PROGRAMS
            args,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout[:MAX_COMMAND_OUTPUT].strip() or None


def collect_os_facts():
    """Return the host's OS and kernel identity.

    :return: A mapping of OS facts; values are ``None`` where unavailable.
    """
    os_release = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os_release[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return {
        "os_name": os_release.get("PRETTY_NAME") or os_release.get("NAME"),
        "os_id": os_release.get("ID"),
        "os_version_id": os_release.get("VERSION_ID"),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "hostname": platform.node(),
    }


#: The server programs a member may run. A shard, config server or arbiter runs
#: ``mongod``; a router runs ``mongos`` and no mongod at all, so looking only for
#: mongod reports every healthy router as down.
SERVER_PROGRAMS = ("mongod", "mongos")


def process_facts(process):
    """Format one server process as a record's ``process`` sub-document.

    :param process: The process, or ``None`` when none was found for this subject.
    :return: The sub-document. ``running`` is ``False`` and every other value is
        ``None`` when there was no process, which is what tells "this service is not
        running" from "this service is running and we could not read its argv".
    """
    if process is None:
        return {
            "running": False,
            "program": None,
            "pid": None,
            "uptime_sec": None,
            "argv": None,
            "config_path": None,
        }
    return {
        "running": True,
        "program": process.get("program"),
        "pid": process.get("pid"),
        "uptime_sec": process.get("uptime_sec"),
        "argv": process.get("argv"),
        "config_path": process.get("config_path"),
    }


def match_process(processes, port):
    """Return the server process that serves one target's port.

    By **port**, because that is the only thing a target and a process have in
    common: the name is PMM's and the argv is the process's. Matching by port is
    what stops every service on a host sharing one process's facts: collecting the
    first ``ps`` line once and copying it into every target record would report one
    argv, one config path, one uptime and one installed version for three mongods,
    while their database facts came from their own ports. That would make the
    upgraded-but-not-restarted divergence this payload exists to catch wrong for
    every service on the host but one, and would report a mongos beside a mongod as
    itself a mongod.

    A **single** server process is attributed to the target without matching, because
    a mongod started without an explicit port has no port to match on — every mongod
    on the default 27017 with no ``port:`` in its config file is that case, and
    refusing to attribute the one process running there would report the commonest
    host in any estate as stopped.

    With several running the port has to match, and a target whose port belongs to
    none of them is reported as not running. That is the honest answer: another
    service's argv is not this service's.

    :param processes: Every server process found on the host.
    :param port: The target's port.
    :return: The process serving it, or ``None``.
    """
    if not processes:
        return None
    if len(processes) == 1:
        return processes[0]
    for process in processes:
        if port is not None and process.get("port") == port:
            return process
    return None


def parse_config_path(argv):
    """Read the ``--config`` path out of a server's command line.

    :param argv: The full command line.
    :return: The configuration file path, or ``None``.
    """
    tokens = argv.split()
    for index, token in enumerate(tokens):
        if token in ("-f", "--config") and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--config="):
            return token.split("=", 1)[1]
    return None


def parse_port(argv, config_path):
    """Determine the port a server process listens on.

    The command line wins when it carries one. Otherwise the configuration file is
    read, because that is where the port usually lives — every node in the sandbox
    is started as ``mongod --config <file>`` with the port set inside it, so an
    argv-only reading would find nothing on any of them.

    :param argv: The full command line.
    :param config_path: The configuration file the process was started with.
    :return: The port as an int, or ``None`` when neither source names one.
    """
    tokens = argv.split()
    for index, token in enumerate(tokens):
        if token == "--port" and index + 1 < len(tokens):  # nosec B105 - a mongod flag, not a password
            if tokens[index + 1].isdigit():
                return int(tokens[index + 1])
        if token.startswith("--port="):
            value = token.split("=", 1)[1]
            if value.isdigit():
                return int(value)

    if not config_path:
        return None
    try:
        with open(config_path, encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                # Matches the YAML `port: 27018` and the legacy `port=27018` alike,
                # without taking a YAML parser as a dependency on the host.
                if not line.startswith("port"):
                    continue
                remainder = line[len("port") :].lstrip()
                if remainder.startswith((":", "=")):
                    value = remainder[1:].strip().split("#")[0].strip()
                    if value.isdigit():
                        return int(value)
    except OSError:
        return None
    return None


def collect_server_processes():
    """Return every mongod and mongos running on this host.

    The one enumeration everything else is derived from: which processes PMM has no
    service for (:func:`find_unregistered`), and which process belongs to a given
    target (:func:`match_process`). Both need every line, so both read this.

    Each of :data:`SERVER_PROGRAMS` is asked for, not just the first that answers: a
    router runs ``mongos`` and no mongod at all, so looking only for mongod reports
    every healthy router as down, and a host running both has to report both.

    ``ps`` rather than ``/proc`` so the payload behaves the same on any host the
    executor runs on; ``etimes`` gives uptime in seconds directly.

    :return: One mapping per server process, each with its program, pid, uptime,
        port, configuration path and command line.
    """
    processes = []
    for program in SERVER_PROGRAMS:
        output = run_command(["ps", "-o", "pid=,etimes=,args=", "-C", program])
        if not output:
            continue
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, etimes, argv = parts
            config_path = parse_config_path(argv)
            processes.append(
                {
                    "program": program,
                    "pid": int(pid) if pid.isdigit() else None,
                    "uptime_sec": int(etimes) if etimes.isdigit() else None,
                    "port": parse_port(argv, config_path),
                    "config_path": config_path,
                    "argv": argv,
                }
            )
    return processes


def matched_process_pids(processes, targets):
    """Return the pid of every process :func:`match_process` already attributed.

    Resolved the same way :func:`probe` resolves it per target, so :func:`main` can
    tell :func:`find_unregistered` which processes are already spoken for — before
    it applies its own, coarser port filter.

    :param processes: Every server process found on the host.
    :param targets: The configured targets, each carrying a ``port``.
    :return: The pids already attributed to a target.
    """
    matched = set()
    for target in targets:
        process = match_process(processes, target.get("port"))
        if process is not None and process.get("pid") is not None:
            matched.add(process["pid"])
    return matched


def find_unregistered(processes, targets, matched_pids=frozenset()):
    """Return the server processes no target accounts for.

    A "target" is a service PMM has registered and asked us to probe, identified by
    its port. Anything else listening is a database PMM does not know about —
    normal rather than exotic, because an arbiter holds no data and therefore no user
    documents, so SCRAM cannot authenticate and ``pmm-admin add mongodb`` fails for
    it. Any estate with arbiters and authentication enabled has them.

    A process whose port could not be determined is reported as unregistered rather
    than dropped: it cannot be matched to a target, and silently discarding a running
    database would be exactly the dishonesty this list exists to prevent. That rule
    on its own double-counts one case, though: a lone mongod with no explicit port has
    no port to match here either, so without ``matched_pids`` it would land in this
    list *and* be the process :func:`match_process` already attributed to a target —
    one running database reported as both a healthy service and a stranger.
    ``matched_pids`` is how :func:`main` excludes it before the port filter runs.

    :param processes: Every server process found on the host.
    :param targets: The configured targets, each carrying a ``port``.
    :param matched_pids: Pids :func:`match_process` already attributed to a target;
        excluded regardless of what the port filter below would otherwise say.
    :return: The processes that matched no target.
    """
    # Explicit loops rather than comprehensions: the Tasks layer minifies this file
    # before dispatch (see build_uri's note above), and ``hoist_literals`` aliases a
    # comprehension's loop variable onto the hoisted "pid" literal this function also
    # reads. From 3.12 a comprehension is inlined into the enclosing function (PEP
    # 709), which turns that name into a function local left unbound once the
    # comprehension ends, so the later read raises ``UnboundLocalError``. Measured on
    # the whole-file minified payload: 3.12.2 and 3.12.3 raise, 3.12.4 onwards do
    # not, and neither do 3.9, 3.11, 3.13 or 3.14. Ubuntu 24.04 ships 3.12.3 as its
    # system python, and this runs on whatever the monitored host has. A generator
    # expression is unaffected — PEP 709 inlines comprehensions only.
    registered_ports = set()
    for target in targets:
        port = target.get("port")
        if port is not None:
            registered_ports.add(port)
    unregistered = []
    for process in processes:
        if process.get("pid") in matched_pids:
            continue
        if process.get("port") is None or process["port"] not in registered_ports:
            unregistered.append(process)
    return unregistered


def collect_binary_version(program=None):
    """Return the installed server binary's version string, or ``None``.

    Read from the binary rather than from the database so it is available even when
    the database is unreachable or ``probe_database`` is off — an upgrade check
    needs the installed version, not the running one.

    Asks the program the member actually runs: a router's ``mongos --version``
    prints ``mongos version v…`` where mongod prints ``db version v…``, so both
    markers are accepted. Falls back to ``mongod`` when no process was found, since
    the binary is usually installed even when nothing is running.

    :param program: The server program the subject actually runs, as
        :func:`match_process` resolved it.
    :return: The version, e.g. ``7.0.39-21``, or ``None``.
    """
    output = run_command([program or "mongod", "--version"])
    if not output:
        return None
    first = output.splitlines()[0].strip()
    # "db version v7.0.14-8" / "mongos version v7.0.39-21" -> the bare version
    for marker in ("db version v", "mongos version v"):
        if marker in first:
            return first.split(marker, 1)[1].strip()
    return first


def binary_version(program, cache):
    """Return one program's installed version, asking each program only once.

    A cache rather than a call per target: the version is a property of the binary,
    not of the service, so a host running six mongods would otherwise fork ``mongod
    --version`` six times for one answer. Keyed by program, because a host running a
    mongos beside a mongod has two binaries and two answers.

    :param program: The program to ask, or ``None`` to take the default binary.
    :param cache: Versions already collected, keyed by program. Updated in place.
    :return: The version string, or ``None``.
    """
    if program not in cache:
        cache[program] = collect_binary_version(program)
    return cache[program]


def read_userinfo(credentials_path):
    """Return percent-encoded ``user:pass@`` read from the node credentials file.

    The file is the same one the PBM and MongoDB Status payloads read. Its contents
    are accepted either as a full ``mongodb://user:pass@host:port`` URI or as the
    bare ``user:pass@host:port`` form the sandbox writes; only the credentials are
    taken, because host and port come from the config. A missing file is not an
    error — an unauthenticated mongod is legitimate.

    :param credentials_path: Path to the credentials file, or ``None`` to skip.
    :return: The ``user:pass@`` prefix ready to splice into a URI, or ``""``.
    """
    if not credentials_path or not os.path.exists(credentials_path):
        return ""
    try:
        with open(credentials_path, encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError as err:
        print(f"cannot read {credentials_path}: {err}", file=sys.stderr)
        return ""
    if not raw:
        return ""
    raw = raw.removeprefix("mongodb://")
    if "@" not in raw:
        return ""
    userinfo = raw.rsplit("@", 1)[0]
    if ":" not in userinfo:
        return ""
    user, _, password = userinfo.partition(":")
    return f"{quote_plus(user)}:{quote_plus(password)}@"


def build_uri(target, userinfo, auth_source, connect_timeout_ms):
    """Return the connection URI for one target.

    ``directConnection=true`` makes the commands report *this* node rather than
    whichever member the driver would otherwise elect to talk to; per-node facts are
    the point.

    :param target: The target mapping carrying ``host`` and ``port``.
    :param userinfo: The credentials prefix from :func:`read_userinfo`.
    :param auth_source: The database to authenticate against.
    :param connect_timeout_ms: Connect and server-selection timeout.
    :return: The MongoDB connection URI.
    """
    options = [
        "directConnection=true",
        f"connectTimeoutMS={connect_timeout_ms}",
        f"serverSelectionTimeoutMS={connect_timeout_ms}",
    ]
    if userinfo:
        options.append(f"authSource={auth_source}")
    # Every value is bound to a name before the f-string rather than subscripted or
    # joined inside it. That is not style: the Tasks layer runs this file through
    # ``python-minifier`` before dispatch, and the minifier normalises inner string
    # quotes to double — turning ``{target['host']}`` into ``{target["host"]}``,
    # which is a quote nested inside a double-quoted f-string. That is PEP 701 syntax
    # and parses only on Python 3.12 and later.
    #
    # The payload runs on whatever Python a monitored host happens to have, which is
    # not ours to choose: this workspace's own pmm-server carries 3.9, where the
    # minified line was a SyntaxError and every probe of that host failed. Keeping
    # expressions out of f-strings is what stops the minifier being able to produce
    # the construct at all.
    host = target["host"]
    port = target["port"]
    query = "&".join(options)
    return f"mongodb://{userinfo}{host}:{port}/?{query}"


def collect_database_facts(target, userinfo, auth_source, connect_timeout_ms):
    """Return facts read from the database for one target.

    Each command is run independently so one failure does not lose the others —
    ``replSetGetStatus`` legitimately fails against a mongos or a standalone, and
    that must not discard the ``buildInfo`` that came back fine.

    :param target: The target mapping carrying ``host`` and ``port``.
    :param userinfo: The credentials prefix.
    :param auth_source: The database to authenticate against.
    :param connect_timeout_ms: Connect and server-selection timeout.
    :return: A mapping of database facts and per-command errors.
    """
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    facts: dict[str, Any] = {}
    uri = build_uri(target, userinfo, auth_source, connect_timeout_ms)
    client = None
    try:
        client = MongoClient(uri)
        admin = client.admin
        # Connect and authenticate once, before the commands, so failing to reach
        # the database at all is the target's error rather than four command
        # errors. ``MongoClient`` is lazy: without this, a refused connection or a
        # rejected password surfaced only inside the per-command handler below,
        # the record kept ``status: ok``, and a mongod nobody could query was
        # stored as freshly and successfully probed. It also waits out one
        # connect timeout for an unreachable target instead of one per command.
        admin.command("ping")
        for key, command in (
            ("build_info", "buildInfo"),
            ("hello", "hello"),
            ("cmd_line_opts", "getCmdLineOpts"),
            ("repl_set_status", "replSetGetStatus"),
        ):
            try:
                facts[key] = json.loads(json.dumps(admin.command(command), default=str))
            except PyMongoError as err:
                facts.setdefault("command_errors", {})[key] = str(err)
    except PyMongoError as err:
        facts["error"] = str(err)
        # Structured beside the text, so the orchestrator can tell a rejected
        # password (``OperationFailure``, code 18) from an unreachable server
        # (``ServerSelectionTimeoutError``, no code) without parsing the message.
        facts["error_type"] = type(err).__name__
        facts["error_code"] = getattr(err, "code", None)
    finally:
        if client is not None:
            client.close()
    return summarise_database_facts(facts)


def determine_vendor(build_info):
    """Return who built this binary, from ``buildInfo``'s own fields.

    ``psmdbVersion`` is a field Percona's fork adds to ``buildInfo`` that upstream
    MongoDB has no code to emit, so its presence is a positive identification, not
    a heuristic — confirmed against a running Percona Server for MongoDB node
    (``psmdbVersion`` present, ``modules: []``) and a running MongoDB Enterprise
    node (no ``psmdbVersion`` key at all, ``modules: ["enterprise"]``). Without
    that field, ``modules`` is the documented signal for Enterprise versus
    Community — but on its own it cannot tell Percona from Community, since
    Percona's ``modules`` is also empty.

    :param build_info: The ``buildInfo`` command's output, or ``{}`` when it
        was not collected.
    :return: ``"Percona"``, ``"MongoDB Enterprise"``, ``"MongoDB Community"``,
        or ``None`` when ``build_info`` is empty.
    """
    if not build_info:
        return None
    if build_info.get("psmdbVersion"):
        return "Percona"
    if "enterprise" in (build_info.get("modules") or []):
        return "MongoDB Enterprise"
    return "MongoDB Community"


def summarise_database_facts(facts):
    """Copy the few fields worth having to the top level of the record.

    The raw command output stays under ``raw``; the summary is what becomes
    VictoriaMetrics labels and Postgres columns downstream, and what a human reads
    first in the task log.

    :param facts: The collected command output.
    :return: The facts with a flat summary merged in.
    """
    build_info = facts.get("build_info") or {}
    hello = facts.get("hello") or {}
    repl = facts.get("repl_set_status") or {}
    summary = {
        "db_version": build_info.get("version"),
        "git_version": build_info.get("gitVersion"),
        "vendor": determine_vendor(build_info),
        "storage_engine": (facts.get("cmd_line_opts") or {})
        .get("parsed", {})
        .get("storage", {})
        .get("engine"),
        "is_writable_primary": hello.get("isWritablePrimary"),
        "is_arbiter": hello.get("arbiterOnly"),
        "msg": hello.get("msg"),
        "set_name": hello.get("setName") or repl.get("set"),
        "state": repl.get("myState"),
    }
    for key in ("error", "error_type", "error_code", "command_errors"):
        if key in facts:
            summary[key] = facts[key]
    summary["raw"] = {
        key: value
        for key, value in facts.items()
        if key not in ("error", "error_type", "error_code", "command_errors")
    }
    return summary


def probe(target, config, host_facts, processes=(), versions=None):
    """Build the complete record for one target.

    ``host_facts`` are the ones that genuinely belong to the machine and are shared;
    the process and the installed version are resolved **per target**, from the
    process serving its port. Sharing those was what made a multi-mongod host report
    the same argv and the same installed version for every service on it.

    :param target: The target mapping carrying ``service``, ``service_id``, ``host``,
        ``port``.
    :param config: The task config.
    :param host_facts: The already-collected host-level facts, shared across targets
        because every target on this dispatch runs on the same executor host.
    :param processes: Every server process found on the host.
    :param versions: Installed versions already read, keyed by program. Updated in
        place so each binary is asked once per dispatch.
    :return: The record to print as one NDJSON line.
    """
    record = {
        "service": target.get("service"),
        # Echoed straight back: this is PMM's service id, and dispatch.py keys its
        # parsed records by it rather than by ``service`` alone, because two
        # same-named services on one host are not two same-id services.
        "service_id": target.get("service_id"),
        "host": target.get("host"),
        "port": target.get("port"),
        "collected_at": int(time.time()),
        "status": STATUS_OK,
    }
    record.update(host_facts)

    process = match_process(processes, target.get("port"))
    record["process"] = process_facts(process)
    # The program this service actually runs, so a mongos reports a mongos version.
    # ``None`` falls back to the default binary in :func:`collect_binary_version`,
    # which is what a stopped service wants: what is *installed* is still the answer
    # to "is this host ready to run the version we expect".
    record["binary_version"] = binary_version(
        process.get("program") if process else None,
        versions if versions is not None else {},
    )

    if not config.get("probe_database", True):
        record["database"] = None
        return record

    try:
        record["database"] = collect_database_facts(
            target,
            read_userinfo(credentials_path(config)),
            config.get("auth_source") or DEFAULT_AUTH_SOURCE,
            config.get("connect_timeout_ms") or DEFAULT_CONNECT_TIMEOUT_MS,
        )
        if record["database"].get("error"):
            record["status"] = STATUS_FAILED
            record["error"] = record["database"]["error"]
            record["error_type"] = record["database"].get("error_type")
            record["error_code"] = record["database"].get("error_code")
    except Exception as err:  # noqa: BLE001 - one target must not abort the rest
        error_type = type(err).__name__
        record["status"] = STATUS_FAILED
        record["error"] = f"{error_type}: {err}"
        record["error_type"] = error_type
        record["database"] = None
    return record


def credentials_path(config):
    """Return the credentials file path, defaulting under ``$HOME``.

    :param config: The task config.
    :return: The path to read credentials from, or ``None``.
    """
    configured = config.get("credentials_path")
    if configured:
        return configured
    home = os.path.expanduser("~")
    return os.path.join(home, DEFAULT_CREDENTIALS_BASENAME) if home else None


#: The file every Percona repository client needs before it can install anything.
#: Small (about 3 KB), stable, and *load-bearing* — an unreachable packaging key is a
#: real install blocker rather than a synthetic reachability check, which is what makes
#: it worth fetching rather than pinging the host.
DEFAULT_REPO_URL = "https://repo.percona.com/percona/yum/PERCONA-PACKAGING-KEY"

#: What the packaging key's body starts with. Checked so that a proxy or captive
#: portal answering 200 with its own page is reported as unreachable rather than as a
#: healthy repository — which is the failure a status-code-only check gets wrong, and
#: the one most likely in the networks this exists to describe.
PACKAGING_KEY_MARKER = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"

def redact_userinfo(url):
    """Return ``url`` with any ``user:pass@`` replaced by ``***@``.

    Proxy URLs commonly carry credentials, and this value is stored on the host's
    ``observed`` document and served from ``GET /hosts`` to every API-authenticated
    caller — so reporting it verbatim, which is what this did, publishes a secret to
    a wider audience than the environment variable it came from. The
    ``anonymize_mask`` on the dispatch does not help: it covers task logs, and the
    value's route out is the JSONB column.

    Redacting only the userinfo keeps every diagnostic property the value has. Which
    proxy is in effect — scheme, host, port — is what makes a refused connection
    explainable, and none of it is secret.

    :param url: The proxy URL as configured, or ``None``.
    :return: The URL with credentials removed, or the input unchanged when it carries
        none.
    """
    if not url:
        return url
    scheme, separator, remainder = url.partition("://")
    if not separator:
        scheme, separator, remainder = "", "", url
    if "@" not in remainder:
        return url
    # ``rpartition``: a password may legitimately contain an encoded ``@``, and the
    # host is whatever follows the last one.
    host = remainder.rpartition("@")[2]
    return f"{scheme}{separator}***@{host}"


#: Short on purpose. This runs once per dispatch on the request path of a sweep, and
#: "the repository is slow" is itself the finding — a package manager with a 30-second
#: stall is not usable in practice, so waiting 30 seconds to discover that adds nothing.
DEFAULT_REPO_TIMEOUT = 8


def collect_repo_facts(config):
    """Return whether this host can actually reach Percona's repository.

    An HTTPS GET rather than a ping or a TCP connect, because the failures worth
    catching all live above that layer: DNS that resolves nowhere useful, a TLS
    interception appliance with a certificate the host does not trust, a proxy that
    allows CONNECT but blocks this origin, a transparent cache serving 403. Every one
    of those passes a ping and fails ``yum install``.

    The proxy in effect is reported alongside the result, because without it the
    result cannot be explained: "connection refused" from a host with no proxy and
    from a host behind a broken one are the same string and completely different
    problems. Its credentials are not, though: the value goes through
    :func:`redact_userinfo` first, because it is stored and then served from
    ``GET /hosts``, and ``http://user:pass@proxy:3128`` is an ordinary way to write
    that variable.

    Never raises: a repository check failing must not cost the caller the OS, process
    and version facts collected beside it.

    :param config: The task config, which may carry ``repo_url`` and
        ``repo_timeout``.
    :return: A mapping describing the attempt.
    """
    url = config.get("repo_url") or DEFAULT_REPO_URL
    timeout = config.get("repo_timeout") or DEFAULT_REPO_TIMEOUT
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    facts = {
        "url": url,
        "reachable": False,
        "status_code": None,
        "latency_ms": None,
        "proxy": redact_userinfo(proxy),
        "error": None,
    }

    started = time.time()
    try:
        # urllib honours the proxy environment variables by default, which is the
        # behaviour wanted here: the check should go the same way a package manager
        # would rather than a way only this payload knows about.
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - REPO_URL is a StrHttpUrl, so http/https only
            # Read the body rather than trusting the status line. A proxy or captive
            # portal answering 200 with its own HTML is a failure that a status-only
            # check reports as success, and reading is what a package manager does.
            body = response.read(len(PACKAGING_KEY_MARKER) + 64)
            facts["status_code"] = response.status
            facts["reachable"] = PACKAGING_KEY_MARKER in body
            if not facts["reachable"]:
                facts["error"] = (
                    "the response did not look like the packaging key -- something "
                    "answered, but not the repository"
                )
    except urllib.error.HTTPError as err:
        facts["status_code"] = err.code
        facts["error"] = f"HTTP {err.code}: {err.reason}"
    except Exception as err:  # noqa: BLE001 - a repo check must not fail the sweep
        facts["error"] = f"{type(err).__name__}: {err}"
    facts["latency_ms"] = int((time.time() - started) * 1000)
    return facts


#: Package-manager binaries checked, in the order tried and paired with the name
#: reported. First match wins. ``dnf`` before ``yum`` because RHEL8+ symlinks the
#: latter to the former — checking ``dnf`` first reports the tool the host's own
#: package metadata actually understands.
_PACKAGE_MANAGERS = (
    ("apt-get", "apt"),
    ("dnf", "dnf"),
    ("yum", "yum"),
    ("zypper", "zypper"),
)


def collect_install_readiness():
    """Return facts describing whether this host is ready to receive an install.

    Collected for every host, not only ones already running a database: a machine
    with nothing installed on it yet is exactly the case an install decision is
    about, and it has no service row for either fact to land on.

    :return: A mapping with ``package_manager`` and ``data_dir_free_bytes``; either
        is ``None`` when the fact could not be determined.
    """
    return {
        "package_manager": next(
            (name for binary, name in _PACKAGE_MANAGERS if shutil.which(binary)),
            None,
        ),
        "data_dir_free_bytes": _free_bytes("/var/lib"),
    }


def _free_bytes(path):
    """Return the free byte count on the filesystem holding ``path``, or ``None``.

    Measured at ``/var/lib`` rather than a database's own data directory: this runs
    before an install decision is made, when no mongod has been installed yet to
    have a ``dbPath`` of its own to measure. ``/var/lib`` is where both package
    families put that default anyway — ``/var/lib/mongo`` from the RPMs,
    ``/var/lib/mongodb`` from the debs — and it exists on a bare host, so it names
    the filesystem an install would land on without needing one. Measuring ``/``
    instead would describe a different disk on any host that keeps ``/var`` on its
    own filesystem.

    :param path: The path whose filesystem to measure.
    :return: The free byte count, or ``None`` if it could not be read.
    """
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def main():
    """Print one JSON object for the host, then one per configured target.

    The host line comes first and is emitted whether or not there are targets. A
    dispatch with no targets is not a misconfiguration: a machine carrying a PMM
    client and no database is exactly what OM wants to describe, and it is the state
    a host is in before anything is installed on it. Refusing to run there — which
    this did, exiting 1 on an empty target list — left the only hosts worth an
    install decision as the only hosts OM could say nothing about.

    The host line also stops host attributes being read off whichever service record
    happened to answer. They belong to the host, they are collected once per
    dispatch, and now they are reported once too.
    """
    config = load_config()
    targets = config.get("targets") or []

    # Collected once: properties of the machine, identical for every target on this
    # dispatch because they all run on the same executor host. The process and the
    # installed version are *not* in here — they belong to one server process, and a
    # host may run several.
    host_facts = {
        "system": collect_os_facts(),
        # Once per dispatch, like the OS facts: it is a property of the host, and
        # asking once per service would multiply the wait by the services on it.
        "repo": collect_repo_facts(config),
        # Whether this host is ready to receive an install, not only whether a
        # database is already running on it.
        "install_readiness": collect_install_readiness(),
    }
    # Enumerated once and shared, because ``ps`` is the same answer for every target
    # on this dispatch. Which line belongs to which target is decided per target.
    processes = collect_server_processes()
    versions = {}

    # ``service: null`` is what marks this the host's own record. The consumer keys
    # service records by name, so a record with no name cannot be mistaken for one.
    host_record = {
        "service": None,
        "collected_at": int(time.time()),
        "status": STATUS_OK,
    }
    host_record.update(host_facts)
    # The host's own process line is the first server process running on it, and the
    # host's installed version is that program's. This is deliberately *not* how a
    # service's are resolved: a service takes the process on its own port, and only
    # the host is entitled to an "anything at all" answer. What matters here is the
    # machine with no registered service, where the installed version is the whole
    # install decision and there is no service row to carry it.
    first = processes[0] if processes else None
    host_record["process"] = process_facts(first)
    host_record["binary_version"] = binary_version(
        first.get("program") if first else None, versions
    )
    # What is running here that PMM did not ask about. Reported on the host rather
    # than as a service of its own: there is no service id to key one on, and
    # inventing an identity for a database PMM does not monitor would commit to a
    # shape before anyone needs it. Dropping them instead would let the estate view
    # claim a host is empty while a mongod is running on it.
    # Resolved before the port filter runs, so a process match_process already gave a
    # target — including the single-process shortcut, which has no port of its own to
    # match on — is not also reported as a stranger here.
    host_record["unregistered_mongods"] = find_unregistered(
        processes, targets, matched_process_pids(processes, targets)
    )
    print(json.dumps(host_record, default=str), flush=True)

    for target in targets:
        record = probe(target, config, host_facts, processes, versions)
        print(json.dumps(record, default=str), flush=True)


if __name__ == "__main__":
    main()
