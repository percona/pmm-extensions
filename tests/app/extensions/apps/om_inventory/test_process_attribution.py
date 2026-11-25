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

"""Test that each service's process facts are *its own*.

The payload used to collect one ``ps`` line per dispatch and copy it into every
target record. On a host running one mongod that is correct and cheap; on a host
running several it gave every service the same argv, the same config path, the same
uptime and the same installed version, while the database facts beside them came from
each service's own port. So the one comparison this payload exists to make — the
*installed* binary against the *running* server — was wrong for every service on the
host but one, and silently: the record looked complete.

A mongos beside a mongod is the sharpest version. The shared line reported
``program: mongod`` and a mongod version for the router, which is not a version skew
but a category error.

Multiple server processes per host are ordinary in the estates OM is for: an arbiter
costs nothing to colocate, and a router usually sits on a member. These tests pin the
attribution rule — by port, with the single-process host exempt because a mongod on
the default 27017 has no port on its command line to match.
"""

import pytest

from app.extensions.apps.om_inventory.payload.probe import (
    binary_version,
    match_process,
    probe,
    process_facts,
)

MEMBER_PORT = 27017
SHARD_PORT = 27018
ARBITER_PORT = 27019
ROUTER_PORT = 27017
UNKNOWN_PORT = 37017

MEMBER_UPTIME = 4200
SHARD_UPTIME = 99
MEMBER_PID = 1234
SHARD_PID = 5678

MONGOD_VERSION = "7.0.39-21"
MONGOS_VERSION = "8.0.4-1"
#: What ``mongod --version`` says when no server process is running to name a program.
DEFAULT_VERSION = "7.0.14-8"

#: Pre-filled so no test forks ``mongod --version``: :func:`binary_version` only
#: shells out for a program it has not been given an answer for.
VERSIONS = {
    "mongod": MONGOD_VERSION,
    "mongos": MONGOS_VERSION,
    None: DEFAULT_VERSION,
}


def process(
    port: int | None,
    program: str = "mongod",
    pid: int = MEMBER_PID,
    uptime: int = MEMBER_UPTIME,
) -> dict:
    """Build one server process as ``collect_server_processes`` reports it.

    :param port: The port it listens on, or ``None`` when neither its command line
        nor its config file named one.
    :param program: ``mongod`` or ``mongos``.
    :param pid: Its pid.
    :param uptime: Its uptime in seconds.
    :return: The process mapping.
    """
    return {
        "program": program,
        "pid": pid,
        "uptime_sec": uptime,
        "port": port,
        "config_path": f"/etc/{program}-{port}.conf",
        "argv": f"/usr/bin/{program} --config /etc/{program}-{port}.conf",
    }


def target(name: str, port: int | None) -> dict:
    """Build one probe target as the dispatcher's config carries it.

    :param name: The service name PMM registered.
    :param port: The port PMM has it registered on.
    :return: The target mapping.
    """
    return {"service": name, "host": "node00", "port": port}


def probed(name: str, port: int | None, processes: list[dict]) -> dict:
    """Probe one target with the database half switched off.

    ``probe_database`` false keeps this a test of attribution rather than of pymongo:
    what is under test is which process ends up on the record.

    :param name: The service name.
    :param port: The port it is registered on.
    :param processes: The server processes running on the host.
    :return: The record the payload would print for it.
    """
    return probe(
        target(name, port),
        {"probe_database": False},
        {"system": {"os_name": "Ubuntu 24.04"}},
        processes,
        dict(VERSIONS),
    )


class TestMatchProcess:
    """Assert which process a target is attributed."""

    def test_the_only_server_on_the_host_is_the_target_s(self) -> None:
        """Resolve the lone mongod to the target without needing a port match.

        A mongod started as ``mongod --config /etc/mongod.conf`` with no ``port:``
        in the file listens on 27017 and says so nowhere the payload can read. That
        is the commonest host in any estate, and requiring a port match there would
        report it as stopped.
        """
        only = process(None)

        assert match_process([only], MEMBER_PORT) is only

    def test_several_servers_are_matched_by_port(self) -> None:
        """Match each target to the process on its own port."""
        member, shard = process(MEMBER_PORT), process(SHARD_PORT, pid=SHARD_PID)

        assert match_process([member, shard], SHARD_PORT) is shard
        assert match_process([member, shard], MEMBER_PORT) is member

    def test_a_target_matching_no_running_port_is_not_running(self) -> None:
        """Report nothing rather than another service's process."""
        processes = [process(MEMBER_PORT), process(SHARD_PORT, pid=SHARD_PID)]

        assert match_process(processes, UNKNOWN_PORT) is None

    def test_a_target_with_no_port_among_several_is_not_running(self) -> None:
        """Report no answer when several candidates share a host and nothing matches."""
        processes = [process(MEMBER_PORT), process(SHARD_PORT, pid=SHARD_PID)]

        assert match_process(processes, None) is None

    def test_no_server_running_matches_nothing(self) -> None:
        """Treat an empty host as nothing to match, not a failure to match."""
        assert match_process([], MEMBER_PORT) is None


class TestProcessFacts:
    """Assert the ``process`` sub-document either describes a process or says none."""

    def test_a_process_is_reported_whole(self) -> None:
        """Take every field a consumer reads off the matched process."""
        facts = process_facts(process(SHARD_PORT, pid=SHARD_PID, uptime=SHARD_UPTIME))

        assert facts["running"] is True
        assert facts["program"] == "mongod"
        assert facts["pid"] == SHARD_PID
        assert facts["uptime_sec"] == SHARD_UPTIME
        assert facts["config_path"] == f"/etc/mongod-{SHARD_PORT}.conf"

    def test_no_process_is_not_running_with_nothing_else_claimed(self) -> None:
        """Report ``running: false`` with null fields, not values borrowed from elsewhere."""
        facts = process_facts(None)

        assert facts["running"] is False
        assert facts == {
            "running": False,
            "program": None,
            "pid": None,
            "uptime_sec": None,
            "argv": None,
            "config_path": None,
            "cluster_role": None,
        }


class TestTheRecordsOfAMultiMongodHost:
    """Assert two services on one host do not report each other's process."""

    def test_each_service_reports_its_own_process(self) -> None:
        """Separate each service's process facts, fixing the regression that shared them."""
        processes = [
            process(MEMBER_PORT),
            process(SHARD_PORT, pid=SHARD_PID, uptime=SHARD_UPTIME),
        ]

        member = probed("rs-node00", MEMBER_PORT, processes)
        shard = probed("sh-node00", SHARD_PORT, processes)

        assert member["process"]["pid"] == MEMBER_PID
        assert shard["process"]["pid"] == SHARD_PID
        assert member["process"]["uptime_sec"] == MEMBER_UPTIME
        assert shard["process"]["uptime_sec"] == SHARD_UPTIME
        assert member["process"]["config_path"] != shard["process"]["config_path"]
        assert member["process"]["argv"] != shard["process"]["argv"]

    def test_a_router_beside_a_member_reports_the_mongos_binary(self) -> None:
        """Assign the router its own mongos binary, not the mongod version beside it.

        And it is the case most likely to be read as an urgent upgrade: the router
        would report whatever mongod happened to be installed beside it.
        """
        processes = [
            process(MEMBER_PORT),
            process(ROUTER_PORT + 1, program="mongos", pid=SHARD_PID),
        ]

        router = probed("sc-router00", ROUTER_PORT + 1, processes)

        assert router["process"]["program"] == "mongos"
        assert router["binary_version"] == MONGOS_VERSION

    def test_a_service_whose_process_is_gone_says_so(self) -> None:
        """Keep a stopped mongod from inheriting facts from the running one beside it."""
        processes = [process(MEMBER_PORT)]
        processes.append(process(SHARD_PORT, pid=SHARD_PID))

        stopped = probed("arbiter-node00", ARBITER_PORT, processes)

        assert stopped["process"]["running"] is False
        assert stopped["process"]["argv"] is None

    def test_a_stopped_service_still_reports_the_installed_binary(self) -> None:
        """Keep the installed version after the process running it is gone.

        "Is this machine carrying the version we expect" is exactly the question
        asked about a node that is down.
        """
        stopped = probed("rs-node00", MEMBER_PORT, [])

        assert stopped["process"]["running"] is False
        assert stopped["binary_version"] == DEFAULT_VERSION

    def test_the_single_mongod_host_is_unchanged(self) -> None:
        """Keep the common single-mongod case working, port on the command line or not."""
        record = probed("rs-node00", MEMBER_PORT, [process(None)])

        assert record["process"]["running"] is True
        assert record["process"]["pid"] == MEMBER_PID
        assert record["binary_version"] == MONGOD_VERSION


class TestBinaryVersionIsAskedOncePerProgram:
    """Assert a host running six mongods forks ``mongod --version`` once."""

    def test_a_cached_program_is_not_asked_again(self) -> None:
        """Cache the version by binary, not by service, and skip asking again."""
        cache = {"mongod": MONGOD_VERSION}

        assert binary_version("mongod", cache) == MONGOD_VERSION
        assert cache == {"mongod": MONGOD_VERSION}

    def test_an_unknown_program_is_collected_and_remembered(self, monkeypatch) -> None:
        """Call once on a miss, and never again for that program.

        :param monkeypatch: The pytest monkeypatch fixture.
        """
        calls = []

        def fake_collect(program=None):
            calls.append(program)
            return MONGOS_VERSION

        monkeypatch.setattr(
            "app.extensions.apps.om_inventory.payload.probe.collect_binary_version",
            fake_collect,
        )
        cache: dict[str | None, str | None] = {}

        assert binary_version("mongos", cache) == MONGOS_VERSION
        assert binary_version("mongos", cache) == MONGOS_VERSION
        assert calls == ["mongos"]


@pytest.mark.parametrize(
    "port",
    [MEMBER_PORT, SHARD_PORT, ARBITER_PORT],
    ids=["member", "shard", "arbiter"],
)
def test_the_port_reaches_the_record_whatever_the_process_says(port: int) -> None:
    """Keep the target's own port on the record whether or not a process matched.

    It is what a reader joins the record back to PMM's service with, so it can never
    be taken from the process that happened to match.

    :param port: The port the service is registered on.
    """
    record = probed("svc", port, [process(MEMBER_PORT)])

    assert record["port"] == port
