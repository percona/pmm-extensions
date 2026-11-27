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

"""Test the credential-free database probe and how an unreachable database fails.

The probe offers no login: it runs only ``ping``, ``buildInfo`` and ``hello``, which
a mongod answers before authenticating. ``MongoClient`` connects lazily, so ``ping``
is where a refused connection surfaces; were the first command a fact read, the
failure would become a command error, the record would stay ``status: ok``, and a
mongod nobody could reach would be stored as freshly probed.

pymongo is not installed where these tests run - it is the payload's own requirement,
installed on the node - so a stand-in module is put in its place.
"""

import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import call, MagicMock, patch

import pytest

from app.extensions.apps.om_inventory.payload import probe as payload
from app.extensions.apps.om_inventory.payload.probe import (
    build_uri,
    CLUSTER_ROLE_CONFIGSVR,
    CLUSTER_ROLE_SHARDSVR,
    collect_database_facts,
    describe_database_error,
    parse_cluster_role,
    STATUS_FAILED,
    STATUS_OK,
)

TARGET = {"service": "rs0-0", "service_id": "svc-1", "host": "node00", "port": 27017}
#: MongoDB's ``NoReplicationEnabled``, which a standalone legitimately returns.
NO_REPLICATION = 76
#: MongoDB's ``Unauthorized``, for a command a client that did not log in may not run.
UNAUTHORIZED = 13


class PyMongoError(Exception):
    """Stand in for ``pymongo.errors.PyMongoError``."""


class OperationFailure(PyMongoError):  # noqa: N818 - named as pymongo names it
    """Stand in for ``pymongo.errors.OperationFailure``, which carries a code."""

    def __init__(
        self, message: str, code: int, details: dict[str, Any] | None = None
    ) -> None:
        """Keep the server's error code and reply, as pymongo's does.

        :param message: The error message.
        :param code: The server's error code.
        :param details: The server's reply document.
        """
        super().__init__(message)
        self.code = code
        self.details = details


class ServerSelectionTimeoutError(PyMongoError):
    """Stand in for ``pymongo.errors.ServerSelectionTimeoutError``, which has none."""


#: The topology description pymongo appends to a server-selection timeout.
TOPOLOGY = (
    ", Timeout: 5.0s, Topology Description: <TopologyDescription id: 6ac6, "
    "topology_type: Single, servers: [<ServerDescription ('node00', 27017) "
    "server_type: Unknown, rtt: None>]>"
)
#: What pymongo appends to each per-server error.
CONFIGURED_TIMEOUTS = (
    " (configured timeouts: socketTimeoutMS: 20000.0ms, connectTimeoutMS: 5000.0ms)"
)


def fake_pymongo(command: MagicMock) -> dict[str, ModuleType]:
    """Build ``pymongo`` and ``pymongo.errors`` modules around one command mock.

    :param command: What ``client.admin.command`` does.
    :return: The modules, keyed as ``sys.modules`` holds them.
    """
    errors = ModuleType("pymongo.errors")
    errors.PyMongoError = PyMongoError  # ty: ignore[unresolved-attribute]
    errors.OperationFailure = OperationFailure  # ty: ignore[unresolved-attribute]
    module = ModuleType("pymongo")

    def connect(_uri: str) -> MagicMock:
        client = MagicMock()
        client.admin.command = command
        return client

    module.MongoClient = MagicMock(  # ty: ignore[unresolved-attribute]
        side_effect=connect
    )
    module.errors = errors  # ty: ignore[unresolved-attribute]
    return {"pymongo": module, "pymongo.errors": errors}


def answering(*, arbiter: bool = False, version: str = "7.0.45-24") -> MagicMock:
    """Answer ``ping``, ``buildInfo`` and ``hello`` as a reachable mongod does.

    :param arbiter: Whether ``hello`` reports ``arbiterOnly``.
    :param version: The ``buildInfo`` version to report.
    :return: The ``client.admin.command`` mock.
    """

    def answer(name: str) -> dict[str, Any]:
        if name == "buildInfo":
            return {"version": version}
        if name == "hello":
            return {"arbiterOnly": True} if arbiter else {"isWritablePrimary": True}
        return {}

    return MagicMock(side_effect=answer)


def collect(command: MagicMock) -> dict[str, Any]:
    """Run :func:`collect_database_facts` against the stand-in driver.

    :param command: What ``client.admin.command`` does.
    :return: The summarised facts.
    """
    with patch.dict(sys.modules, fake_pymongo(command)):
        return collect_database_facts(TARGET, 500)


def build_record(
    modules: Mapping[str, ModuleType | None],
) -> dict[str, Any]:
    """Run the payload's per-target :func:`~payload.probe` with ``modules`` imported.

    No credentials file is written or configured: the probe connects without a login.

    :param modules: What ``sys.modules`` holds for ``pymongo`` and its errors.
    :return: The record the payload would print.
    """
    with patch.dict(sys.modules, modules):
        return payload.probe(TARGET, {}, {}, versions={None: "6.0.14"})


class TestCollectDatabaseFacts:
    """Assert the payload queries without credentials and reports what it reached."""

    def test_the_connection_uri_carries_no_credentials(self) -> None:
        """Build a credential-free URI: no ``user:pass@`` is ever spliced in."""
        uri = build_uri(TARGET, 500)
        assert "@" not in uri
        assert uri.startswith("mongodb://node00:27017/")
        assert "directConnection=true" in uri

    def test_a_reachable_server_is_read_and_has_no_error(self) -> None:
        """Run only ``ping``, ``buildInfo`` and ``hello``, each once, with no login."""
        command = answering(version="7.0.45-24")

        facts = collect(command)

        assert "error" not in facts
        assert facts["db_version"] == "7.0.45-24"
        assert facts["is_writable_primary"] is True
        assert command.call_args_list == [
            call("ping"),
            call("buildInfo"),
            call("hello"),
        ]

    def test_an_arbiter_is_read_without_a_login(self) -> None:
        """Read an arbiter's ``hello`` without credentials; it answers before auth."""
        facts = collect(answering(arbiter=True))

        assert "error" not in facts
        assert facts["is_arbiter"] is True

    def test_a_host_connects_exactly_once(self) -> None:
        """Open one connection: there is no arbiter pre-check to make a second."""
        modules = fake_pymongo(answering(arbiter=True))

        with patch.dict(sys.modules, modules):
            collect_database_facts(TARGET, 500)

        assert modules["pymongo"].MongoClient.call_count == 1

    def test_an_unreachable_server_is_the_target_s_error(self) -> None:
        """Report a refused connection on the first command as the error."""
        command = MagicMock(
            side_effect=ServerSelectionTimeoutError("node00:27017: Connection refused")
        )

        facts = collect(command)

        assert facts["error"] == "could not connect to node00:27017: Connection refused"
        assert facts["error_type"] == "ServerSelectionTimeoutError"
        assert facts["error_code"] is None
        command.assert_called_once_with("ping")

    def test_one_failing_fact_command_on_a_reachable_server_is_not_an_error(
        self,
    ) -> None:
        """Keep a refused ``hello`` a command error only, not the target's error."""

        def answer(name: str) -> dict[str, Any]:
            if name == "hello":
                raise OperationFailure("Unauthorized", UNAUTHORIZED)
            if name == "buildInfo":
                return {"version": "6.0.14"}
            return {}

        facts = collect(MagicMock(side_effect=answer))

        assert "error" not in facts
        assert facts["db_version"] == "6.0.14"
        assert list(facts["command_errors"]) == ["hello"]


class TestDescribeDatabaseError:
    """Assert the stored reason says what to fix, not what the driver saw."""

    def test_a_server_that_never_answered_names_its_address_and_the_wait(
        self,
    ) -> None:
        """Replace the topology description with the address and the timeout."""
        err = ServerSelectionTimeoutError("No servers found yet" + TOPOLOGY)

        described = describe_database_error(err, TARGET, 5000)

        assert described == "no answer from node00:27017 within 5s"

    def test_a_connect_that_timed_out_reads_as_no_answer(self) -> None:
        """Word a filtered port the same whichever of the two timeouts fired first."""
        err = ServerSelectionTimeoutError(
            "node00:27017: timed out" + CONFIGURED_TIMEOUTS + TOPOLOGY
        )

        described = describe_database_error(err, TARGET, 5000)

        assert described == "no answer from node00:27017 within 5s"

    def test_a_refused_connection_keeps_the_cause_and_drops_the_timeouts(
        self,
    ) -> None:
        """Keep the socket error, without pymongo's timeouts and topology."""
        err = ServerSelectionTimeoutError(
            "node00:27017: [Errno 111] Connection refused"
            + CONFIGURED_TIMEOUTS
            + TOPOLOGY
        )

        described = describe_database_error(err, TARGET, 5000)

        assert (
            described
            == "could not connect to node00:27017: [Errno 111] Connection refused"
        )

    def test_another_server_error_is_its_message_alone(self) -> None:
        """Report the server's own message, not the reply document around it."""
        err = OperationFailure(
            "Unauthorized, full error: {...}",
            UNAUTHORIZED,
            {"errmsg": "command ping requires authentication", "code": UNAUTHORIZED},
        )

        described = describe_database_error(err, TARGET, 5000)

        assert described == "command ping requires authentication"


class TestParseClusterRole:
    """Assert the config-server role is read credential-free, file or argv."""

    def test_configsvr_from_the_config_file(self, tmp_path: Path) -> None:
        """Read ``sharding.clusterRole`` from the config file the probe already opens."""
        config = tmp_path / "mongod.conf"
        config.write_text(
            "net:\n  port: 27019\nsharding:\n  clusterRole: configsvr\n",
            encoding="utf-8",
        )

        role = parse_cluster_role(f"/usr/bin/mongod --config {config}", str(config))

        assert role == CLUSTER_ROLE_CONFIGSVR

    def test_shardsvr_from_the_config_file(self, tmp_path: Path) -> None:
        """Read a shard server's role from the config file as well."""
        config = tmp_path / "mongod.conf"
        config.write_text("sharding:\n  clusterRole: shardsvr\n", encoding="utf-8")

        assert (
            parse_cluster_role(f"mongod --config {config}", str(config))
            == CLUSTER_ROLE_SHARDSVR
        )

    @pytest.mark.parametrize(
        "value", ['"configsvr"', "'configsvr'"], ids=["double", "single"]
    )
    def test_a_quoted_role_is_read_without_its_quotes(
        self, tmp_path: Path, value: str
    ) -> None:
        """Read a YAML-quoted ``sharding.clusterRole`` as the role it quotes."""
        config = tmp_path / "mongod.conf"
        config.write_text(f"sharding:\n  clusterRole: {value}\n", encoding="utf-8")

        assert (
            parse_cluster_role(f"mongod --config {config}", str(config))
            == CLUSTER_ROLE_CONFIGSVR
        )

    def test_a_role_flag_inside_a_path_is_not_the_flag(self, tmp_path: Path) -> None:
        """Read the file's role when ``--configsvr`` only appears inside a path."""
        config = tmp_path / "mongod--configsvr.conf"
        config.write_text("sharding:\n  clusterRole: shardsvr\n", encoding="utf-8")

        assert (
            parse_cluster_role(f"mongod --config {config}", str(config))
            == CLUSTER_ROLE_SHARDSVR
        )

    def test_an_unknown_role_reads_as_none(self, tmp_path: Path) -> None:
        """Report no role when the file names a value that is neither role."""
        config = tmp_path / "mongod.conf"
        config.write_text("sharding:\n  clusterRole: router\n", encoding="utf-8")

        assert parse_cluster_role(f"mongod --config {config}", str(config)) is None

    @pytest.mark.parametrize(
        "line",
        ["#clusterRole: configsvr", "# clusterRole: configsvr"],
        ids=["unspaced", "spaced"],
    )
    def test_a_commented_out_role_reads_as_none(
        self, tmp_path: Path, line: str
    ) -> None:
        """Ignore a ``clusterRole`` line that is commented out."""
        config = tmp_path / "mongod.conf"
        config.write_text(f"sharding:\n  {line}\n", encoding="utf-8")

        assert parse_cluster_role(f"mongod --config {config}", str(config)) is None

    def test_an_inline_comment_is_not_part_of_the_role(self, tmp_path: Path) -> None:
        """Read the role in front of a trailing comment."""
        config = tmp_path / "mongod.conf"
        config.write_text(
            "sharding:\n  clusterRole: configsvr # the config replica set\n",
            encoding="utf-8",
        )

        assert (
            parse_cluster_role(f"mongod --config {config}", str(config))
            == CLUSTER_ROLE_CONFIGSVR
        )

    def test_a_missing_config_file_reads_as_none(self, tmp_path: Path) -> None:
        """Report no role, rather than fail, when the config file cannot be opened."""
        config = tmp_path / "missing.conf"

        assert parse_cluster_role(f"mongod --config {config}", str(config)) is None

    def test_a_byte_that_is_not_utf8_does_not_hide_the_role(
        self, tmp_path: Path
    ) -> None:
        """Read the role past a Latin-1 comment rather than fail the whole host."""
        config = tmp_path / "mongod.conf"
        config.write_bytes(
            b"net:\n  port: 27019 # caf\xe9\nsharding:\n  clusterRole: configsvr\n"
        )

        assert (
            parse_cluster_role(f"mongod --config {config}", str(config))
            == CLUSTER_ROLE_CONFIGSVR
        )

    def test_configsvr_from_argv_needs_no_config_file(self) -> None:
        """Take ``--configsvr`` straight off the command line without reading a file."""
        assert (
            parse_cluster_role("/usr/bin/mongod --configsvr", None)
            == CLUSTER_ROLE_CONFIGSVR
        )

    @pytest.mark.parametrize(
        ("flag", "declared", "role"),
        [
            ("--configsvr", "shardsvr", CLUSTER_ROLE_CONFIGSVR),
            ("--shardsvr", "configsvr", CLUSTER_ROLE_SHARDSVR),
        ],
        ids=["configsvr", "shardsvr"],
    )
    def test_a_role_flag_on_argv_wins_over_the_config_file(
        self, tmp_path: Path, flag: str, declared: str, role: str
    ) -> None:
        """Take the role from argv when the config file declares a different one."""
        config = tmp_path / "mongod.conf"
        config.write_text(f"sharding:\n  clusterRole: {declared}\n", encoding="utf-8")

        assert (
            parse_cluster_role(f"mongod --config {config} {flag}", str(config)) == role
        )

    def test_a_plain_mongod_has_no_cluster_role(self, tmp_path: Path) -> None:
        """Report no role when neither argv nor the config file declares one."""
        config = tmp_path / "mongod.conf"
        config.write_text("net:\n  port: 27017\n", encoding="utf-8")

        assert parse_cluster_role(f"mongod --config {config}", str(config)) is None


class TestProbeRecord:
    """Assert how a database failure shapes the record the payload prints."""

    def test_a_database_error_fails_the_record_and_carries_its_type(self) -> None:
        """Mark the record failed, with the type and code beside the message."""
        command = MagicMock(
            side_effect=ServerSelectionTimeoutError("node00:27017: Connection refused")
        )

        record = build_record(fake_pymongo(command))

        assert record["status"] == STATUS_FAILED
        assert (
            record["error"] == "could not connect to node00:27017: Connection refused"
        )
        assert record["error_type"] == "ServerSelectionTimeoutError"
        assert record["error_code"] is None

    def test_a_failure_outside_the_driver_carries_the_same_keys(self) -> None:
        """Give a payload that cannot import pymongo a type and a ``None`` code too."""
        record = build_record({"pymongo": None, "pymongo.errors": None})

        assert record["status"] == STATUS_FAILED
        assert record["error_type"] == "ModuleNotFoundError"
        assert record["error_code"] is None
        assert record["database"] is None

    def test_a_queried_database_leaves_the_record_ok(self) -> None:
        """Leave a record whose database answered unmarked."""
        record = build_record(fake_pymongo(answering(version="6.0.14")))

        assert record["status"] == STATUS_OK
        assert "error" not in record
        assert record["database"]["db_version"] == "6.0.14"
        assert record["binary_version"] == "6.0.14"

    def test_a_credential_file_in_home_is_never_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Connect without the login a ``~/.mongodb_uri`` on the host would offer."""
        (tmp_path / ".mongodb_uri").write_text(
            "mongodb://user:p%40ss@node00:27017/admin\n", encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = fake_pymongo(answering())

        record = build_record(modules)

        assert record["status"] == STATUS_OK
        uri = modules["pymongo"].MongoClient.call_args.args[0]
        assert "@" not in uri
