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

"""Test how a service's role is read off its probe record.

``om.service.role`` was documented as ``mongod`` / ``mongos`` / ``config`` /
``arbiter`` and written as ``NULL`` on every upsert. These pin the table
:func:`classify_role` implements, in the order that matters: an arbiter is itself a
``mongod`` process, so it has to be recognised before the ``mongod`` fallback claims
it, and a router's own ``mongos`` line must not fall through to that fallback either.
A config server is also followed from its ``ps`` line and config file through the
payload's own record, so the role the classifier reads is the one the probe parsed.
"""

import subprocess
from pathlib import Path

import pytest

from app.extensions.apps.om_inventory.payload import probe as payload
from app.extensions.apps.om_inventory.payload.probe import (
    CLUSTER_ROLE_CONFIGSVR,
    CLUSTER_ROLE_SHARDSVR,
)
from app.extensions.apps.om_inventory.service import classify_role

CONFIG_SERVER_PORT = 27019


def record(
    *,
    is_arbiter: bool | None = None,
    msg: str | None = None,
    program: str | None = "mongod",
    argv: str = "/usr/bin/mongod --config /etc/mongod.conf",
    running: bool = True,
    cluster_role: str | None = None,
) -> dict:
    """Build a probe record trimmed to the fields the classifier reads.

    :param is_arbiter: ``hello.arbiterOnly``, as the payload summarises it.
    :param msg: ``hello.msg``, ``"isdbgrid"`` on a mongos.
    :param program: The server process's program, or ``None`` when none was found.
    :param argv: The server process's full command line.
    :param running: Whether a server process was found at all.
    :param cluster_role: ``process.cluster_role`` as the probe parses it from the
        mongod config file's ``sharding.clusterRole`` (or ``--configsvr`` on argv) -
        how a real config server declares itself, read without a login.
    :return: The record.
    """
    return {
        "database": {
            "is_arbiter": is_arbiter,
            "msg": msg,
        },
        "process": {
            "running": running,
            "program": program if running else None,
            "argv": argv if running else None,
            "cluster_role": cluster_role if running else None,
        },
    }


class TestClassifyRole:
    """Pin the precedence table :func:`classify_role` implements."""

    def test_an_arbiter_is_not_classified_as_a_plain_mongod(self) -> None:
        """Classify an arbiter as arbiter even though it is itself a mongod process."""
        assert classify_role(record(is_arbiter=True)) == "arbiter"

    def test_a_router_is_mongos_by_its_own_program(self) -> None:
        """Classify a router as mongos when its own process says so."""
        assert (
            classify_role(record(program="mongos", argv="/usr/bin/mongos")) == "mongos"
        )

    def test_a_router_is_mongos_by_hello_when_the_program_cannot_say(self) -> None:
        """Catch a mongos via ``hello.msg`` when its process facts came back empty."""
        assert (
            classify_role(record(msg="isdbgrid", program=None, running=False))
            == "mongos"
        )

    def test_a_config_server_is_read_from_cluster_role(self) -> None:
        """Read a config server's role from the probe's credential-free ``cluster_role``.

        Measured against this workspace's sharded sandbox, where every config server
        runs as ``mongod --config /etc/mongod-node.conf`` with ``sharding.clusterRole:
        configsvr`` in that file. The probe parses it from the file (or ``--configsvr``
        on argv) with no login, so a healthy config server is not mislabelled
        ``mongod``.
        """
        assert classify_role(record(cluster_role=CLUSTER_ROLE_CONFIGSVR)) == "config"

    def test_a_shard_member_is_not_a_config_server(self) -> None:
        """Treat a shardsvr's ``cluster_role`` as mongod, not as a config server."""
        assert classify_role(record(cluster_role=CLUSTER_ROLE_SHARDSVR)) == "mongod"

    def test_an_ordinary_member_is_mongod(self) -> None:
        """Classify a running server process as mongod when none of the above match."""
        assert classify_role(record()) == "mongod"

    def test_no_server_process_found_classifies_as_nothing(self) -> None:
        """Keep a previously good role rather than blanking it on absence."""
        assert classify_role(record(program=None, running=False)) is None


class TestARoleReadOffTheHost:
    """Follow a config server's role from ``ps`` to :func:`classify_role`."""

    def test_a_config_server_declared_in_its_file_classifies_as_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Classify a mongod whose config file declares ``configsvr`` as config."""
        config = tmp_path / "mongod.conf"
        config.write_text(
            f"net:\n  port: {CONFIG_SERVER_PORT}\n"
            "sharding:\n  clusterRole: configsvr\n",
            encoding="utf-8",
        )
        ps_line = f"4321 600 /usr/bin/mongod --config {config}\n"

        def ps(
            args: list[str], **_kwargs: bool | int
        ) -> subprocess.CompletedProcess[str]:
            stdout = ps_line if args[-1] == "mongod" else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", ps)

        probed = payload.probe(
            {"service": "cfg-0", "host": "node00", "port": CONFIG_SERVER_PORT},
            {"probe_database": False},
            {},
            payload.collect_server_processes(),
            {"mongod": "7.0.39-21"},
        )

        assert probed["process"]["cluster_role"] == CLUSTER_ROLE_CONFIGSVR
        assert classify_role(probed) == "config"
