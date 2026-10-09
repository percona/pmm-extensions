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

"""Test the connectivity check payload script."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_pymysql():
    """Inject a mock pymysql module into sys.modules.

    A real ``OperationalError`` class is installed on the mock so production
    code can reference ``pymysql.err.OperationalError`` in an ``except`` clause
    without tripping ``TypeError: catching classes that do not inherit from
    BaseException is not allowed``.
    """
    mock = MagicMock()
    mock.err.OperationalError = type("OperationalError", (Exception,), {})
    old = sys.modules.get("pymysql")
    sys.modules["pymysql"] = mock
    yield mock
    if old is None:
        sys.modules.pop("pymysql", None)
    else:
        sys.modules["pymysql"] = old


@pytest.fixture
def mock_myloginpath():
    """Inject a mock myloginpath module into sys.modules."""
    mock = MagicMock()
    old = sys.modules.get("myloginpath")
    sys.modules["myloginpath"] = mock
    yield mock
    if old is None:
        sys.modules.pop("myloginpath", None)
    else:
        sys.modules["myloginpath"] = old


@pytest.fixture
def mock_psycopg2():
    """Inject a mock psycopg2 module into sys.modules.

    A real ``OperationalError`` class is installed on the mock with a
    ``pgcode`` attribute so production code can both catch the exception and
    inspect its SQLSTATE classification.
    """
    mock = MagicMock()
    mock.OperationalError = type("OperationalError", (Exception,), {"pgcode": None})
    old = sys.modules.get("psycopg2")
    sys.modules["psycopg2"] = mock
    yield mock
    if old is None:
        sys.modules.pop("psycopg2", None)
    else:
        sys.modules["psycopg2"] = old


@pytest.fixture
def mock_pymongo():
    """Inject a mock pymongo module into sys.modules.

    A real ``OperationFailure`` class is installed on the mock with a ``code``
    attribute so production code can both catch the exception and inspect its
    server-reported MongoDB error code.
    """
    mock = MagicMock()
    mock.errors.OperationFailure = type(
        "OperationFailure", (Exception,), {"code": None}
    )
    # The production code imports the errors submodule explicitly, as importing
    # the package alone does not guarantee it is bound; register both.
    previous = {name: sys.modules.get(name) for name in ("pymongo", "pymongo.errors")}
    sys.modules["pymongo"] = mock
    sys.modules["pymongo.errors"] = mock.errors
    yield mock
    for name, old in previous.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


class TestCheckMySQL:
    """Test MySQL connectivity checker."""

    def test_success(self, mock_myloginpath, mock_pymysql):
        """Verify successful MySQL connectivity check."""
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {
            "user": "root",
            "password": "secret",
        }
        mock_conn = MagicMock()
        mock_pymysql.connect.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = check_mysql("db-host", 3306)

        assert result == {"success": True}
        mock_pymysql.connect.assert_called_once_with(
            host="db-host",
            port=3306,
            connect_timeout=10,
            user="root",
            password="secret",
        )
        mock_cursor.execute.assert_called_once_with("SELECT 1")
        mock_conn.close.assert_called_once()

    def test_connection_failure(self, mock_myloginpath, mock_pymysql):
        """Verify MySQL check returns error on connection failure."""
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {}
        mock_pymysql.connect.side_effect = Exception("Connection refused")

        result = check_mysql("db-host", 3306)

        assert result == {"success": False, "error": "Connection refused"}

    def test_myloginpath_failure_falls_back(self, mock_myloginpath, mock_pymysql):
        """Verify connectivity check proceeds when myloginpath parsing fails."""
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.side_effect = Exception("No login path found")
        mock_conn = MagicMock()
        mock_pymysql.connect.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = check_mysql("db-host", 3306)

        assert result == {"success": True}
        mock_pymysql.connect.assert_called_once_with(
            host="db-host", port=3306, connect_timeout=10
        )

    @pytest.mark.parametrize("code", [1044, 1045, 1130])
    def test_auth_failure_treated_as_success(
        self, code, mock_myloginpath, mock_pymysql
    ):
        """Verify server-side auth/authorization rejections report success.

        The goal is to test connectivity: an auth-denied response from the
        server proves the server is reachable.
        """
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {}
        mock_pymysql.connect.side_effect = mock_pymysql.err.OperationalError(
            code, "Access denied for user 'x'@'y'"
        )

        assert check_mysql("db-host", 3306) == {"success": True}

    def test_network_operational_error_remains_failure(
        self, mock_myloginpath, mock_pymysql
    ):
        """Verify a network-level OperationalError (code 2xxx) reports failure."""
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {}
        mock_pymysql.connect.side_effect = mock_pymysql.err.OperationalError(
            2003, "Can't connect to MySQL server"
        )

        result = check_mysql("db-host", 3306)
        assert result["success"] is False
        error = result["error"]
        assert isinstance(error, str)
        assert "Can't connect" in error

    def test_operational_error_with_empty_args_remains_failure(
        self, mock_myloginpath, mock_pymysql
    ):
        """Verify OperationalError with no args does not crash and reports failure."""
        from app.tasks.connectivity.payload import check_mysql  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {}
        mock_pymysql.connect.side_effect = mock_pymysql.err.OperationalError()

        result = check_mysql("db-host", 3306)
        assert result == {"success": False, "error": ""}


class TestCheckPostgreSQL:
    """Test PostgreSQL connectivity checker."""

    def test_success(self, mock_psycopg2):
        """Verify successful PostgreSQL connectivity check."""
        from app.tasks.connectivity.payload import check_postgresql  # noqa: PLC0415

        mock_conn = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = check_postgresql("db-host", 5432)

        assert result == {"success": True}
        mock_psycopg2.connect.assert_called_once_with(
            host="db-host", port=5432, connect_timeout=10
        )
        mock_cursor.execute.assert_called_once_with("SELECT 1")
        mock_conn.close.assert_called_once()

    def test_connection_failure(self, mock_psycopg2):
        """Verify PostgreSQL check returns error on connection failure."""
        from app.tasks.connectivity.payload import check_postgresql  # noqa: PLC0415

        mock_psycopg2.connect.side_effect = Exception("Connection refused")

        result = check_postgresql("db-host", 5432)

        assert result == {"success": False, "error": "Connection refused"}

    @pytest.mark.parametrize("pgcode", ["28000", "28P01"])
    def test_auth_failure_by_sqlstate_treated_as_success(self, pgcode, mock_psycopg2):
        """Verify SQLSTATE class 28 (auth rejection) reports success.

        An auth-rejected response from the server proves the server is
        reachable, which is what this check measures.
        """
        from app.tasks.connectivity.payload import check_postgresql  # noqa: PLC0415

        err = mock_psycopg2.OperationalError("FATAL: auth rejected")
        err.pgcode = pgcode
        mock_psycopg2.connect.side_effect = err

        assert check_postgresql("db-host", 5432) == {"success": True}

    def test_pgcode_none_remains_failure(self, mock_psycopg2):
        """Verify OperationalError with ``pgcode=None`` reports failure.

        libpq does not always populate ``pgcode`` on connection-time errors;
        an explicit ``None`` is treated as a failure to avoid fragile message
        string matching.
        """
        from app.tasks.connectivity.payload import check_postgresql  # noqa: PLC0415

        err = mock_psycopg2.OperationalError("FATAL: password authentication failed")
        err.pgcode = None
        mock_psycopg2.connect.side_effect = err

        result = check_postgresql("db-host", 5432)
        assert result["success"] is False
        error = result["error"]
        assert isinstance(error, str)
        assert "password authentication failed" in error


class TestCheckMongoDB:
    """Test MongoDB connectivity checker."""

    def test_success(self, mock_pymongo):
        """Verify successful MongoDB connectivity check."""
        from app.tasks.connectivity.payload import check_mongodb  # noqa: PLC0415

        mock_client = MagicMock()
        mock_pymongo.MongoClient.return_value = mock_client

        result = check_mongodb("db-host", 27017)

        assert result == {"success": True}
        mock_pymongo.MongoClient.assert_called_once_with(
            host="db-host", port=27017, serverSelectionTimeoutMS=10000
        )
        mock_client.admin.command.assert_called_once_with("ping")
        mock_client.close.assert_called_once()

    def test_connection_failure(self, mock_pymongo):
        """Verify MongoDB check returns error on connection failure."""
        from app.tasks.connectivity.payload import check_mongodb  # noqa: PLC0415

        mock_pymongo.MongoClient.side_effect = Exception("Server selection timeout")

        result = check_mongodb("db-host", 27017)

        assert result == {"success": False, "error": "Server selection timeout"}

    @pytest.mark.parametrize("code", [13, 18])
    def test_auth_failure_treated_as_success(self, code, mock_pymongo):
        """Verify ``OperationFailure`` codes 13 and 18 report success.

        A server-side auth rejection (``13`` Unauthorized, ``18``
        AuthenticationFailed) proves the server is reachable.
        """
        from app.tasks.connectivity.payload import check_mongodb  # noqa: PLC0415

        mock_client = MagicMock()
        mock_pymongo.MongoClient.return_value = mock_client
        err = mock_pymongo.errors.OperationFailure("auth rejected")
        err.code = code
        mock_client.admin.command.side_effect = err

        assert check_mongodb("db-host", 27017) == {"success": True}

    def test_other_operation_failure_remains_failure(self, mock_pymongo):
        """Verify ``OperationFailure`` with a non-auth code reports failure."""
        from app.tasks.connectivity.payload import check_mongodb  # noqa: PLC0415

        mock_client = MagicMock()
        mock_pymongo.MongoClient.return_value = mock_client
        err = mock_pymongo.errors.OperationFailure("namespace not found")
        err.code = 26
        mock_client.admin.command.side_effect = err

        result = check_mongodb("db-host", 27017)
        assert result["success"] is False
        error = result["error"]
        assert isinstance(error, str)
        assert "namespace not found" in error


class TestMain:
    """Test the payload main entry point."""

    def test_reads_config_from_file_path(
        self, tmp_path, mock_myloginpath, mock_pymysql
    ):
        """Verify main reads the JSON config from the path given by ``--config``.

        The Nomad ``run-python`` task invokes the script with
        ``--config ${NOMAD_TASK_DIR}/script_config`` — a file path, not an inline
        JSON string. The script must open and parse that file.
        """
        mock_myloginpath.parse.return_value = {}
        mock_conn = MagicMock()
        mock_pymysql.connect.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        config_path = tmp_path / "script_config"
        config_path.write_text(
            json.dumps({"service_type": "mysql", "host": "db-host", "port": 3306})
        )

        with patch("sys.argv", ["payload.py", "--config", str(config_path)]):
            from app.tasks.connectivity.payload import main  # noqa: PLC0415

            main()

        mock_pymysql.connect.assert_called_once_with(
            host="db-host",
            port=3306,
            connect_timeout=10,
            user=None,
            password=None,
        )

    def test_unknown_service_type_raises(self, tmp_path):
        """Verify KeyError raised for unsupported service type."""
        from app.tasks.connectivity.payload import main  # noqa: PLC0415

        config_path = tmp_path / "script_config"
        config_path.write_text(
            json.dumps({"service_type": "REDIS", "host": "db-host", "port": 6379})
        )

        with (
            patch("sys.argv", ["payload.py", "--config", str(config_path)]),
            pytest.raises(KeyError),
        ):
            main()


class TestConnectTimeoutBudget:
    """Guard the inner-DB-connect vs outer-connect budget invariant."""

    def test_inner_connect_timeout_strictly_less_than_outer_budget(self):
        """Verify the inner DB ``connect_timeout`` is below the connect budget.

        If the inner DB ``connect_timeout`` equals the outer connect budget,
        the inner connect can never complete inside the outer window once any
        dispatch latency is added, producing a false-negative. The inner
        timeout must stay strictly less than ``CONNECTIVITY_CHECK_TIMEOUT``.
        """
        from app.tasks.connectivity.constants import (  # noqa: PLC0415
            CONNECTIVITY_CHECK_TIMEOUT,
        )
        from app.tasks.connectivity.payload import CONNECT_TIMEOUT  # noqa: PLC0415

        assert CONNECT_TIMEOUT < CONNECTIVITY_CHECK_TIMEOUT

    def test_each_driver_uses_the_shared_connect_timeout_constant(
        self, mock_myloginpath, mock_pymysql, mock_psycopg2, mock_pymongo
    ):
        """Verify every driver sources its timeout from ``CONNECT_TIMEOUT``.

        A future edit must not be able to reintroduce a per-driver literal that
        drifts from the shared constant.
        """
        from app.tasks.connectivity.payload import (  # noqa: PLC0415
            check_mongodb,
            check_mysql,
            check_postgresql,
            CONNECT_TIMEOUT,
        )

        mock_myloginpath.parse.return_value = {}
        check_mysql("db-host", 3306)
        assert (
            mock_pymysql.connect.call_args.kwargs["connect_timeout"] == CONNECT_TIMEOUT
        )

        check_postgresql("db-host", 5432)
        assert (
            mock_psycopg2.connect.call_args.kwargs["connect_timeout"] == CONNECT_TIMEOUT
        )

        check_mongodb("db-host", 27017)
        assert (
            mock_pymongo.MongoClient.call_args.kwargs["serverSelectionTimeoutMS"]
            == CONNECT_TIMEOUT * 1000
        )

    def test_extensions_and_tasks_connect_budgets_match(self):
        """Verify the PMM Extensions side and Tasks-side connect budgets cannot drift.

        PMM Extensions sends ``CHECK_TIMEOUT`` as ``request.timeout``; the Tasks API charges
        the connect phase against ``CONNECTIVITY_CHECK_TIMEOUT``. They are
        declared in separate modules with no shared source, so this pins them
        equal to catch a silent drift.
        """
        from app.extensions.connectivity import CHECK_TIMEOUT  # noqa: PLC0415
        from app.tasks.connectivity.constants import (  # noqa: PLC0415
            CONNECTIVITY_CHECK_TIMEOUT,
        )

        assert CHECK_TIMEOUT == CONNECTIVITY_CHECK_TIMEOUT

    def test_total_server_budget_stays_under_remote_read_timeout(self):
        """Verify the worst-case server wait stays under the client read timeout.

        PMM Extensions' ``RemoteAPI`` holds the request open with ``sock_read=120`` while
        the Tasks API waits up to ``PROVISIONING_TIMEOUT`` plus the connect
        budget. If that sum approaches 120s the call surfaces as "Could not reach
        the Tasks API" instead of the diagnostic timeout response, so keep a
        comfortable margin below the read timeout.
        """
        from annotated_types import Le  # noqa: PLC0415

        from app.tasks.connectivity.constants import (  # noqa: PLC0415
            PROVISIONING_TIMEOUT,
        )
        from app.tasks.connectivity.models import (  # noqa: PLC0415
            ConnectivityCheckWrite,
        )

        timeout_field = ConnectivityCheckWrite.model_fields["timeout"]
        max_connect_budget = next(
            meta.le for meta in timeout_field.metadata if isinstance(meta, Le)
        )
        # ``ClientTimeout(sock_read=...)`` in ``app/core/requests/remote_api.py``.
        remote_api_sock_read = 120
        assert PROVISIONING_TIMEOUT + max_connect_budget < remote_api_sock_read


class TestMainOutputContract:
    """Guard the ``main`` stdout/stderr contract."""

    def test_main_writes_pure_json_to_stdout(
        self, tmp_path, capsys, mock_myloginpath, mock_pymysql
    ):
        """Verify ``main`` writes a single JSON document to stdout.

        ``_parse_check_result`` reads the result with ``json.loads`` over the
        ``run-script`` stdout, so stdout must stay a pure JSON document.
        """
        from app.tasks.connectivity.payload import main  # noqa: PLC0415

        mock_myloginpath.parse.return_value = {}
        mock_conn = MagicMock()
        mock_pymysql.connect.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        config_path = tmp_path / "script_config"
        config_path.write_text(
            json.dumps({"service_type": "mysql", "host": "db-host", "port": 3306})
        )

        with patch("sys.argv", ["payload.py", "--config", str(config_path)]):
            main()

        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"success": True}
