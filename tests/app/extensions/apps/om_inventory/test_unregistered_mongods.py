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

"""Test the payload's report of mongods PMM has no service for.

Arbiters are why this is ordinary rather than exotic. An arbiter holds no data and
therefore no user documents, so SCRAM cannot authenticate and ``pmm-admin add
mongodb`` fails for it — meaning **any** estate with arbiters and authentication
enabled has databases PMM does not know about. Three of this sandbox's nodes are in
exactly that state.

They are reported on the *host* rather than as service rows: there is no service id
to key a row on, and inventing an identity would commit to a shape before anything
needs it. What matters is that they are not silently dropped, because then the estate
view gets to call a host empty while a mongod runs on it.

The port is the join. Getting it wrong in either direction is a real failure: miss a
port and a registered service is reported as a stranger; invent one and a genuine
stranger disappears.
"""

import pytest

from app.extensions.apps.om_inventory.payload.probe import (
    find_unregistered,
    match_process,
    matched_process_pids,
    parse_config_path,
    parse_port,
)

CONFIG_PATH = "/etc/mongod-node.conf"
SHARD_PORT = 27018
DEFAULT_PORT = 27017


class TestParseConfigPath:
    """Assert the configuration file is found however it was passed."""

    @pytest.mark.parametrize(
        "argv",
        [
            f"/usr/bin/mongod --config {CONFIG_PATH}",
            f"/usr/bin/mongod -f {CONFIG_PATH}",
            f"/usr/bin/mongod --config={CONFIG_PATH}",
        ],
        ids=["--config", "-f", "--config="],
    )
    def test_reads_the_path(self, argv: str) -> None:
        """Parse all three spellings, which appear in the wild.

        :param argv: The command line to read.
        """
        assert parse_config_path(argv) == CONFIG_PATH

    def test_absent_when_started_without_one(self) -> None:
        """Return no config path for a mongod started with flags alone."""
        assert parse_config_path("/usr/bin/mongod --port 27017 --dbpath /data") is None


class TestParsePort:
    """Assert the port is found on the command line or in the config file."""

    def test_prefers_the_command_line(self, tmp_path) -> None:
        """Prefer the explicit ``--port`` flag as what the process actually did.

        :param tmp_path: pytest's temporary directory.
        """
        config = tmp_path / "mongod.conf"
        config.write_text("net:\n  port: 27017\n")

        port = parse_port(f"/usr/bin/mongod --port {SHARD_PORT}", str(config))

        assert port == SHARD_PORT

    @pytest.mark.parametrize(
        "argv",
        ["/usr/bin/mongod --port 27018", "/usr/bin/mongod --port=27018"],
        ids=["space", "equals"],
    )
    def test_reads_either_command_line_spelling(self, argv: str) -> None:
        """Read both ``--port N`` and ``--port=N`` spellings from the command line.

        :param argv: The command line to read.
        """
        assert parse_port(argv, None) == SHARD_PORT

    def test_falls_back_to_the_config_file(self, tmp_path) -> None:
        """Fall back to the config file's port when argv alone reveals nothing.

        An argv-only reading would find nothing on any of them, which would make
        every registered service look unregistered.

        :param tmp_path: pytest's temporary directory.
        """
        config = tmp_path / "mongod.conf"
        config.write_text(
            "storage:\n  dbPath: /var/lib/mongo\nnet:\n"
            f"  port: {SHARD_PORT}\n  bindIpAll: true\n"
        )

        assert parse_port("/usr/bin/mongod --config x", str(config)) == SHARD_PORT

    def test_ignores_a_trailing_comment(self, tmp_path) -> None:
        """Strip comments, which hand-edited config files carry.

        :param tmp_path: pytest's temporary directory.
        """
        config = tmp_path / "mongod.conf"
        config.write_text("net:\n  port: 27018  # the shard port\n")

        assert parse_port("/usr/bin/mongod", str(config)) == SHARD_PORT

    def test_unreadable_config_is_not_fatal(self) -> None:
        """Survive a config file it cannot open, rather than aborting the host."""
        assert parse_port("/usr/bin/mongod", "/nonexistent/mongod.conf") is None

    def test_a_byte_that_is_not_utf8_is_not_fatal(self, tmp_path) -> None:
        """Read the port past a Latin-1 comment rather than abort the host.

        :param tmp_path: pytest's temporary directory.
        """
        config = tmp_path / "mongod.conf"
        config.write_bytes(b"# caf\xe9\nnet:\n" + f"  port: {SHARD_PORT}\n".encode())

        assert parse_port("/usr/bin/mongod", str(config)) == SHARD_PORT


def process(port: int | None, program: str = "mongod", pid: int = 1) -> dict:
    """Build one server process as the payload reports it.

    :param port: The port it listens on, or ``None`` when undetermined.
    :param program: ``mongod`` or ``mongos``.
    :param pid: Its pid. Defaults to ``1``, which is fine as long as a test never
        needs to tell two processes apart by pid.
    :return: The process mapping.
    """
    return {
        "program": program,
        "pid": pid,
        "port": port,
        "config_path": CONFIG_PATH,
        "argv": f"/usr/bin/{program} --config {CONFIG_PATH}",
    }


class TestFindUnregistered:
    """Assert which running servers are reported as strangers."""

    def test_a_registered_service_is_not_reported(self) -> None:
        """Stay quiet for a registered service, or the list is noise."""
        found = find_unregistered(
            [process(DEFAULT_PORT)], [{"service": "db00", "port": DEFAULT_PORT}]
        )

        assert found == []

    def test_an_arbiter_with_no_target_is_reported(self) -> None:
        """Report a mongod when PMM holds no target at all for its host.

        The host is dispatched to and has no targets at all, so nothing accounts for
        the process.
        """
        found = find_unregistered([process(SHARD_PORT)], [])

        assert [entry["port"] for entry in found] == [SHARD_PORT]

    def test_a_second_mongod_beside_a_registered_one_is_reported(self) -> None:
        """Report the unknown database on a host carrying one known and one not."""
        found = find_unregistered(
            [process(DEFAULT_PORT), process(SHARD_PORT)],
            [{"service": "db00", "port": DEFAULT_PORT}],
        )

        assert [entry["port"] for entry in found] == [SHARD_PORT]

    def test_a_process_with_no_port_is_reported_rather_than_dropped(self) -> None:
        """Report an unidentifiable database rather than dropping it silently.

        It cannot be matched to a target, and discarding it silently is exactly the
        dishonesty this list exists to prevent — better a visible entry with a null
        port than a host that reads as empty.
        """
        found = find_unregistered(
            [process(None)], [{"service": "db00", "port": DEFAULT_PORT}]
        )

        assert [entry["port"] for entry in found] == [None]

    def test_a_target_without_a_port_registers_nothing(self) -> None:
        """Register nothing for a target carrying no port.

        Treating ``None`` as a wildcard would hide every stranger on the host.
        """
        found = find_unregistered(
            [process(DEFAULT_PORT)], [{"service": "db00", "port": None}]
        )

        assert [entry["port"] for entry in found] == [DEFAULT_PORT]

    def test_a_mongos_is_matched_by_port_like_anything_else(self) -> None:
        """Match a mongos router by port like any other registered service."""
        found = find_unregistered(
            [process(DEFAULT_PORT, program="mongos")],
            [{"service": "mongos00", "port": DEFAULT_PORT}],
        )

        assert found == []


class TestMatchedPidsExcludeADoubleListing:
    """Assert a process a target already claimed is not also reported as a stranger.

    Both halves have to run together for this to mean anything: an isolated call to
    :func:`find_unregistered` cannot see what a target matched, and an isolated call
    to :func:`match_process` says nothing about what :func:`find_unregistered` would
    otherwise report. The bug this pins was a real one — a mongod on the default port
    with no explicit ``port:`` in its config attributed to its target by
    :func:`match_process`'s single-process shortcut, *and* picked up by
    :func:`find_unregistered`'s ``port is None`` arm, because the same missing port
    satisfies both checks in opposite directions.
    """

    def test_a_default_port_mongod_is_not_also_a_stranger(self) -> None:
        """Guard against reporting one running mongod as both itself and a stranger.

        This is ``main``'s own sequence, minus the I/O: resolve every target's match
        first, then hand the pids that produced to :func:`find_unregistered`.
        """
        processes = [process(None)]
        targets = [{"service": "db00", "port": DEFAULT_PORT}]

        matched = matched_process_pids(processes, targets)
        found = find_unregistered(processes, targets, matched)

        assert match_process(processes, DEFAULT_PORT) is processes[0]
        assert found == []

    def test_a_genuine_stranger_beside_a_matched_process_is_still_reported(
        self,
    ) -> None:
        """Exclude a matched pid without swallowing an actual stranger beside it."""
        matched_process = process(DEFAULT_PORT, pid=1)
        stranger = process(SHARD_PORT, pid=2)
        processes = [matched_process, stranger]
        targets = [{"service": "db00", "port": DEFAULT_PORT}]

        matched = matched_process_pids(processes, targets)
        found = find_unregistered(processes, targets, matched)

        assert [entry["port"] for entry in found] == [SHARD_PORT]
