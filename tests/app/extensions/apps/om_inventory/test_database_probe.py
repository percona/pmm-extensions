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

"""Test that a database the payload cannot query is reported as a failure.

``MongoClient`` connects lazily, so the first command is where a refused connection
or a rejected password surfaces. If that command were one of the per-command fact
reads, the failure would become command errors, the record would stay
``status: ok``, and a mongod nobody could query would be stored as freshly probed.

pymongo is not installed where these tests run - it is the payload's own
requirement, installed on the node - so a stand-in module is put in its place.
"""

import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from app.extensions.apps.om_inventory.payload import probe as payload
from app.extensions.apps.om_inventory.payload.probe import (
    AUTHENTICATION_FAILED,
    collect_database_facts,
    describe_database_error,
    STATUS_FAILED,
    STATUS_OK,
)

TARGET = {"service": "rs0-0", "service_id": "svc-1", "host": "node00", "port": 27017}
#: MongoDB's ``NoReplicationEnabled``, which a standalone legitimately returns.
NO_REPLICATION = 76


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
    module = ModuleType("pymongo")
    client = MagicMock()
    client.admin.command = command
    mongo_client = MagicMock(return_value=client)
    module.MongoClient = mongo_client  # ty: ignore[unresolved-attribute]
    module.errors = errors  # ty: ignore[unresolved-attribute]
    return {"pymongo": module, "pymongo.errors": errors}


def collect(command: MagicMock) -> dict[str, Any]:
    """Run :func:`collect_database_facts` against the stand-in driver.

    :param command: What ``client.admin.command`` does.
    :return: The summarised facts.
    """
    with patch.dict(sys.modules, fake_pymongo(command)):
        return collect_database_facts(TARGET, "root:wrong@", "admin", 500)


def build_record(
    modules: Mapping[str, ModuleType | None], tmp_path: Path
) -> tuple[dict[str, Any], Path]:
    """Run the payload's per-target :func:`~payload.probe` with ``modules`` imported.

    The credentials come from a real file, and the installed version from the
    per-dispatch cache ``probe`` already takes, so no binary is asked.

    :param modules: What ``sys.modules`` holds for ``pymongo`` and its errors.
    :param tmp_path: Where to write the credentials file.
    :return: The record the payload would print, and the credentials file's path.
    """
    credentials = tmp_path / ".mongodb_uri"
    credentials.write_text("mongodb://root:wrong@node00:27017\n", encoding="utf-8")
    with patch.dict(sys.modules, modules):
        record = payload.probe(
            TARGET,
            {"credentials_path": str(credentials)},
            {},
            versions={None: "6.0.14"},
        )
    return record, credentials


class TestCollectDatabaseFacts:
    """Assert a database that cannot be reached is the target's error."""

    def test_a_rejected_password_is_the_target_s_error(self) -> None:
        """Report an authentication failure as the error, with its type and code."""
        command = MagicMock(
            side_effect=OperationFailure(
                "Authentication failed.", AUTHENTICATION_FAILED
            )
        )

        facts = collect(command)

        assert facts["error"] == "the credentials for user root were rejected"
        assert facts["error_type"] == "OperationFailure"
        assert facts["error_code"] == AUTHENTICATION_FAILED
        # Not four command errors restating it.
        assert "command_errors" not in facts
        command.assert_called_once_with("ping")

    def test_an_unreachable_server_is_the_target_s_error(self) -> None:
        """Report a refused connection as the error, and wait for it only once."""
        command = MagicMock(
            side_effect=ServerSelectionTimeoutError("node00:27017: Connection refused")
        )

        facts = collect(command)

        assert facts["error"] == "could not connect to node00:27017: Connection refused"
        assert facts["error_type"] == "ServerSelectionTimeoutError"
        assert facts["error_code"] is None
        command.assert_called_once_with("ping")

    def test_one_failing_command_on_a_reachable_server_is_not_an_error(self) -> None:
        """Keep a standalone's refused ``replSetGetStatus`` a command error only."""

        def answer(name: str) -> dict[str, Any]:
            if name == "replSetGetStatus":
                raise OperationFailure("not running with --replSet", NO_REPLICATION)
            if name == "buildInfo":
                return {"version": "6.0.14"}
            return {}

        facts = collect(MagicMock(side_effect=answer))

        assert "error" not in facts
        assert facts["db_version"] == "6.0.14"
        assert list(facts["command_errors"]) == ["repl_set_status"]


class TestDescribeDatabaseError:
    """Assert the stored reason says what to fix, not what the driver saw."""

    def test_a_rejected_password_names_the_user_and_where_it_was_read(self) -> None:
        """Point at the file to fix, not at the server's reply document."""
        err = OperationFailure(
            "Authentication failed., full error: {'ok': 0.0, 'errmsg': "
            "'Authentication failed.', 'code': 18, 'codeName': 'AuthenticationFailed'}",
            AUTHENTICATION_FAILED,
        )

        described = describe_database_error(
            err, TARGET, "pmm%40ops:secret@", "/root/.mongodb_uri", 5000
        )

        assert described == (
            "the credentials for user pmm@ops (from /root/.mongodb_uri) were rejected"
        )

    def test_a_server_that_never_answered_names_its_address_and_the_wait(
        self,
    ) -> None:
        """Replace the topology description with the address and the timeout."""
        err = ServerSelectionTimeoutError("No servers found yet" + TOPOLOGY)

        described = describe_database_error(err, TARGET, "", None, 5000)

        assert described == "no answer from node00:27017 within 5s"

    def test_a_connect_that_timed_out_reads_as_no_answer(self) -> None:
        """Word a filtered port the same whichever of the two timeouts fired first."""
        err = ServerSelectionTimeoutError(
            "node00:27017: timed out" + CONFIGURED_TIMEOUTS + TOPOLOGY
        )

        described = describe_database_error(err, TARGET, "", None, 5000)

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

        described = describe_database_error(err, TARGET, "", None, 5000)

        assert (
            described
            == "could not connect to node00:27017: [Errno 111] Connection refused"
        )

    def test_another_server_error_is_its_message_alone(self) -> None:
        """Report the server's own message, not the reply document around it."""
        err = OperationFailure(
            "Unauthorized, full error: {...}",
            13,
            {"errmsg": "command ping requires authentication", "code": 13},
        )

        described = describe_database_error(err, TARGET, "", None, 5000)

        assert described == "command ping requires authentication"


class TestProbeRecord:
    """Assert how a database failure shapes the record the payload prints."""

    def test_a_database_error_fails_the_record_and_carries_its_type(
        self, tmp_path: Path
    ) -> None:
        """Mark the record failed, with the type and code beside the message."""
        command = MagicMock(
            side_effect=OperationFailure(
                "Authentication failed.", AUTHENTICATION_FAILED
            )
        )

        record, credentials = build_record(fake_pymongo(command), tmp_path)

        assert record["status"] == STATUS_FAILED
        assert record["error"] == (
            f"the credentials for user root (from {credentials}) were rejected"
        )
        assert record["error_type"] == "OperationFailure"
        assert record["error_code"] == AUTHENTICATION_FAILED

    def test_a_failure_outside_the_driver_carries_the_same_keys(
        self, tmp_path: Path
    ) -> None:
        """Give a payload that cannot import pymongo a type and a ``None`` code too."""
        record, _ = build_record({"pymongo": None, "pymongo.errors": None}, tmp_path)

        assert record["status"] == STATUS_FAILED
        assert record["error_type"] == "ModuleNotFoundError"
        assert record["error_code"] is None
        assert record["database"] is None

    def test_a_queried_database_leaves_the_record_ok(self, tmp_path: Path) -> None:
        """Leave a record whose database answered unmarked."""
        command = MagicMock(
            side_effect=lambda name: (
                {"version": "6.0.14"} if name == "buildInfo" else {}
            )
        )

        record, _ = build_record(fake_pymongo(command), tmp_path)

        assert record["status"] == STATUS_OK
        assert "error" not in record
        assert record["database"]["db_version"] == "6.0.14"
        assert record["binary_version"] == "6.0.14"
