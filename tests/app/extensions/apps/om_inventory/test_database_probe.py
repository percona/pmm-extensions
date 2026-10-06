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
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from app.extensions.apps.om_inventory.payload import probe as payload
from app.extensions.apps.om_inventory.payload.probe import (
    collect_database_facts,
    STATUS_FAILED,
    STATUS_OK,
)

TARGET = {"service": "rs0-0", "service_id": "svc-1", "host": "node00", "port": 27017}
#: MongoDB's ``AuthenticationFailed``.
AUTH_FAILED = 18
#: MongoDB's ``NoReplicationEnabled``, which a standalone legitimately returns.
NO_REPLICATION = 76


class PyMongoError(Exception):
    """Stand in for ``pymongo.errors.PyMongoError``."""


class OperationFailure(PyMongoError):  # noqa: N818 - named as pymongo names it
    """Stand in for ``pymongo.errors.OperationFailure``, which carries a code."""

    def __init__(self, message: str, code: int) -> None:
        """Keep the server's error code, as pymongo's does.

        :param message: The error message.
        :param code: The server's error code.
        """
        super().__init__(message)
        self.code = code


class ServerSelectionTimeoutError(PyMongoError):
    """Stand in for ``pymongo.errors.ServerSelectionTimeoutError``, which has none."""


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


def test_a_rejected_password_is_the_target_s_error() -> None:
    """Report an authentication failure as the error, with its type and code."""
    command = MagicMock(
        side_effect=OperationFailure("Authentication failed.", AUTH_FAILED)
    )

    facts = collect(command)

    assert facts["error"] == "Authentication failed."
    assert facts["error_type"] == "OperationFailure"
    assert facts["error_code"] == AUTH_FAILED
    # Not four command errors restating it.
    assert "command_errors" not in facts
    command.assert_called_once_with("ping")


def test_an_unreachable_server_is_the_target_s_error() -> None:
    """Report a refused connection as the error, and wait for it only once."""
    command = MagicMock(
        side_effect=ServerSelectionTimeoutError("node00:27017: Connection refused")
    )

    facts = collect(command)

    assert facts["error"] == "node00:27017: Connection refused"
    assert facts["error_type"] == "ServerSelectionTimeoutError"
    assert facts["error_code"] is None
    command.assert_called_once_with("ping")


def test_one_failing_command_on_a_reachable_server_is_not_an_error() -> None:
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


def build_record(database: dict[str, Any]) -> dict[str, Any]:
    """Run the payload's per-target :func:`~payload.probe` with the database stubbed.

    :param database: What :func:`collect_database_facts` returns.
    :return: The record the payload would print.
    """
    with (
        patch.object(payload, "collect_database_facts", return_value=database),
        patch.object(payload, "read_userinfo", return_value=""),
        patch.object(payload, "binary_version", return_value="6.0.14"),
    ):
        return payload.probe(TARGET, {}, {})


def test_a_database_error_fails_the_record_and_carries_its_type() -> None:
    """Mark the record failed, with the type and code beside the message."""
    record = build_record(
        {
            "error": "Authentication failed.",
            "error_type": "OperationFailure",
            "error_code": AUTH_FAILED,
        }
    )

    assert record["status"] == STATUS_FAILED
    assert record["error"] == "Authentication failed."
    assert record["error_type"] == "OperationFailure"
    assert record["error_code"] == AUTH_FAILED


def test_a_queried_database_leaves_the_record_ok() -> None:
    """Leave a record whose database answered unmarked."""
    record = build_record({"db_version": "6.0.14"})

    assert record["status"] == STATUS_OK
    assert "error" not in record
