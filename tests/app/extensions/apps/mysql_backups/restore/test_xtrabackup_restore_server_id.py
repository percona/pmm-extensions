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

"""Tests for the server-id the XtraBackup restore writes into restored my.cnf files.

The lookup runs after mysqld is stopped and copy-back has replaced the datadir,
so a master that cannot be asked must fall back to a random id rather than fail
the restore. These tests drive the restore payload's real ``_get_new_server_id``
against a scripted master and a scripted ``random``.
"""

import random
import types
from dataclasses import dataclass, field
from typing import cast

import pytest

from tests.app.extensions.apps.mysql_backups.payload_harness import payload_method
from tests.app.extensions.apps.mysql_backups.restore.conftest import (
    RESTORE_PAYLOAD_PATH,
)

_MASTER_IP = "10.0.0.5"
_MASTER_PORT = 3307
_MASTER_ID = 11
_REPLICA_IDS = (21, 22)
_FRESH_ID = 99
_SERVER_ID_RANGE = (1, 4294967295)
_MASTER_SQL = "SELECT @@server_id"
_REPLICAS_SQL = "SHOW SLAVE HOSTS"

#: Shared with the lifted method, so a stubbed failure matches its ``except``.
_BackupError = type("BackupError", (Exception,), {})


@dataclass(frozen=True, kw_only=True)
class QueryCall:
    """Record one ``_query_mysql`` call."""

    sql: str
    fetchall: bool
    host: str | None
    port: int | None


@dataclass
class FakeMaster:
    """Answer the payload's master queries from a table keyed by SQL text."""

    answers: dict[str, object]
    calls: list[QueryCall] = field(default_factory=list)

    def query(
        self,
        sql: str,
        *,
        fetchall: bool = False,
        host: str | None = None,
        port: int | None = None,
    ) -> object:
        """Record the query and return its canned answer, or raise it if an error."""
        self.calls.append(QueryCall(sql=sql, fetchall=fetchall, host=host, port=port))
        answer = self.answers[sql]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def queries(self) -> list[str]:
        """Return the SQL of every query run, in order."""
        return [call.sql for call in self.calls]


@dataclass
class ScriptedRandom:
    """Stand in for ``random`` with ``randint`` drawing from a fixed script."""

    draws: list[int]
    bounds: list[tuple[int, int]] = field(default_factory=list)

    def randint(self, a: int, b: int) -> int:
        """Record the bounds and return the next scripted draw."""
        self.bounds.append((a, b))
        return self.draws.pop(0)


def _master_row(server_id: object = _MASTER_ID) -> dict[str, object]:
    """Return the row ``SELECT @@server_id`` yields."""
    return {"@@server_id": server_id}


def _replica_rows(*server_ids: int) -> list[dict[str, object]]:
    """Return the rows ``SHOW SLAVE HOSTS`` yields for the given replica ids."""
    return [{"Server_id": sid, "Host": f"replica{sid}"} for sid in server_ids]


def _new_server_id(
    master: FakeMaster | None,
    rand: object,
    *,
    master_ip: str | None = _MASTER_IP,
) -> int:
    """Run the payload's real ``_get_new_server_id`` and return its result."""
    method = payload_method(
        "Xtrabackup",
        "_get_new_server_id",
        extra_namespace={"BackupError": _BackupError, "random": rand},
        payload_path=RESTORE_PAYLOAD_PATH,
    )
    restorer = types.SimpleNamespace(
        master_ip=master_ip,
        master_port=_MASTER_PORT,
        _query_mysql=master.query if master else _no_query,
    )
    return cast("int", method(restorer))


def _no_query(*_args: object, **_kwargs: object) -> None:
    """Fail a test that reaches the master when none is configured."""
    pytest.fail("queried the master although no master is configured")


class TestNoMasterConfigured:
    """Cover a restore with no master host set."""

    @pytest.mark.parametrize("master_ip", [None, ""])
    def test_skips_the_master_and_returns_the_first_draw(
        self, master_ip: str | None
    ) -> None:
        """Skip both master queries and keep the first draw."""
        rand = ScriptedRandom([_MASTER_ID])

        assert _new_server_id(None, rand, master_ip=master_ip) == _MASTER_ID
        assert rand.bounds == [_SERVER_ID_RANGE]


class TestAllQueriesSucceed:
    """Cover a master that answers both queries."""

    @pytest.fixture
    def master(self) -> FakeMaster:
        """Return a master reporting its own id and two replicas."""
        return FakeMaster(
            {_MASTER_SQL: _master_row(), _REPLICAS_SQL: _replica_rows(*_REPLICA_IDS)}
        )

    def test_excludes_the_master_and_every_replica(self, master: FakeMaster) -> None:
        """Redraw past the master's id and every replica's id."""
        rand = ScriptedRandom([_MASTER_ID, *_REPLICA_IDS, _FRESH_ID])

        assert _new_server_id(master, rand) == _FRESH_ID
        assert rand.draws == []

    def test_asks_the_configured_master(self, master: FakeMaster) -> None:
        """Send both queries to the configured master host and port."""
        _new_server_id(master, ScriptedRandom([_FRESH_ID]))

        assert master.calls == [
            QueryCall(
                sql=_MASTER_SQL, fetchall=False, host=_MASTER_IP, port=_MASTER_PORT
            ),
            QueryCall(
                sql=_REPLICAS_SQL, fetchall=True, host=_MASTER_IP, port=_MASTER_PORT
            ),
        ]

    def test_excludes_a_master_id_reported_as_text(self) -> None:
        """Exclude a master id the driver returns as a string."""
        master = FakeMaster(
            {_MASTER_SQL: _master_row(str(_MASTER_ID)), _REPLICAS_SQL: []}
        )
        rand = ScriptedRandom([_MASTER_ID, _FRESH_ID])

        assert _new_server_id(master, rand) == _FRESH_ID


class TestMasterQueryFails:
    """Cover a master whose own server-id cannot be read."""

    @pytest.fixture
    def master(self) -> FakeMaster:
        """Return a master refusing ``SELECT @@server_id`` but listing replicas."""
        return FakeMaster(
            {
                _MASTER_SQL: _BackupError("access denied"),
                _REPLICAS_SQL: _replica_rows(*_REPLICA_IDS),
            }
        )

    def test_still_asks_for_the_replicas(self, master: FakeMaster) -> None:
        """Still list the replicas after the master's id is refused."""
        _new_server_id(master, ScriptedRandom([_FRESH_ID]))

        assert master.queries == [_MASTER_SQL, _REPLICAS_SQL]

    def test_excludes_every_replica(self, master: FakeMaster) -> None:
        """Redraw past every replica id despite the failed master query."""
        rand = ScriptedRandom([*_REPLICA_IDS, _FRESH_ID])

        assert _new_server_id(master, rand) == _FRESH_ID

    def test_does_not_exclude_an_id_it_never_learned(self, master: FakeMaster) -> None:
        """Accept the master's id when the master query never reported it."""
        assert _new_server_id(master, ScriptedRandom([_MASTER_ID])) == _MASTER_ID


class TestReplicaQueryFails:
    """Cover a master that reports its id but refuses to list replicas."""

    @pytest.fixture
    def master(self) -> FakeMaster:
        """Return a master refusing ``SHOW SLAVE HOSTS``."""
        return FakeMaster(
            {
                _MASTER_SQL: _master_row(),
                _REPLICAS_SQL: _BackupError("missing REPLICATION SLAVE"),
            }
        )

    def test_excludes_the_master(self, master: FakeMaster) -> None:
        """Redraw past the master's id despite the failed replica query."""
        rand = ScriptedRandom([_MASTER_ID, _FRESH_ID])

        assert _new_server_id(master, rand) == _FRESH_ID

    def test_does_not_treat_the_master_row_as_replicas(
        self, master: FakeMaster
    ) -> None:
        """Ignore the master row instead of iterating it as replica rows."""
        assert _new_server_id(master, ScriptedRandom([_FRESH_ID])) == _FRESH_ID


class TestBothQueriesFail:
    """Cover a master that cannot be reached at all."""

    @pytest.fixture
    def master(self) -> FakeMaster:
        """Return a master failing every query."""
        return FakeMaster(
            {
                _MASTER_SQL: _BackupError("unreachable"),
                _REPLICAS_SQL: _BackupError("unreachable"),
            }
        )

    def test_returns_the_first_draw_in_range(self, master: FakeMaster) -> None:
        """Draw once from the full server-id range."""
        rand = ScriptedRandom([_FRESH_ID])

        assert _new_server_id(master, rand) == _FRESH_ID
        assert rand.bounds == [_SERVER_ID_RANGE]

    def test_stays_in_range_with_the_real_random(self, master: FakeMaster) -> None:
        """Return an in-range id from the real ``random``."""
        server_id = _new_server_id(master, random)

        assert _SERVER_ID_RANGE[0] <= server_id <= _SERVER_ID_RANGE[1]


class TestEmptyMasterAnswers:
    """Cover queries that succeed but report no ids."""

    @pytest.mark.parametrize(
        "master_answer",
        [
            pytest.param(None, id="no-row"),
            pytest.param(_master_row(0), id="zero-id"),
            pytest.param(_master_row(None), id="null-id"),
        ],
    )
    def test_excludes_nothing(self, master_answer: object) -> None:
        """Exclude no ids when the master reports none."""
        master = FakeMaster({_MASTER_SQL: master_answer, _REPLICAS_SQL: ()})

        assert _new_server_id(master, ScriptedRandom([_MASTER_ID])) == _MASTER_ID


class TestErrorsOutsideTheFallback:
    """Cover failures the fallback must not hide."""

    @pytest.mark.parametrize("failing_sql", [_MASTER_SQL, _REPLICAS_SQL])
    def test_propagates_an_error_that_is_not_a_backup_error(
        self, failing_sql: str
    ) -> None:
        """Raise errors other than ``BackupError`` instead of falling back."""
        answers: dict[str, object] = {
            _MASTER_SQL: _master_row(),
            _REPLICAS_SQL: _replica_rows(*_REPLICA_IDS),
        }
        answers[failing_sql] = RuntimeError("driver bug")

        with pytest.raises(RuntimeError, match="driver bug"):
            _new_server_id(FakeMaster(answers), ScriptedRandom([_FRESH_ID]))
