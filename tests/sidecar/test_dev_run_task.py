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

"""Tests for ``sidecar/dev/run-task.py``, which runs one task in the local PMM.

A fake PMM answers every request, so no test opens a connection or waits.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "sidecar" / "dev" / "run-task.py"
APP = "/extensions/api/apps/duplicate_keys"
LOG = (
    'data: {"step":"run-script","type":"stdout","msg":"# Total Indexes  15\\n"}\n\n'
    'event: finish\ndata: {"status": "success"}\n\n'
)
SCHEMA = {
    "forms": [
        {
            "title": "Task",
            "fields": [
                {"name": "task_name", "type": "string", "required": True},
                {
                    "name": "key_types",
                    "type": "choice",
                    "required": False,
                    "default": "fk",
                    "choices": [{"label": "Foreign keys", "value": "fk"}],
                },
            ],
        }
    ]
}


def load_script() -> ModuleType:
    """Load the script, whose file name is not a module name.

    :return: The script as a module.
    """
    spec = importlib.util.spec_from_file_location("dev_run_task", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_task = load_script()


class FakePmm:
    """Answer the requests of one run, and record them.

    :param statuses: The task's status at each read of its record. The last
        one repeats.
    """

    def __init__(self, statuses: list[str | None]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, str]] = []
        self.tokens: list[str | None] = []
        self.cookies: list[str | None] = []
        self.bodies: dict[str, Any] = {}
        self.exchanges = 0
        self.reads = 0
        self.now = 0.0
        self.refuse: dict[tuple[str, str], list[tuple[int, str]]] = {}

    def sleep(self, seconds: float) -> None:
        """Move the fake clock forward instead of waiting.

        :param seconds: The length of the wait.
        """
        self.now += seconds

    def clock(self) -> float:
        """Return the fake clock's time.

        :return: Seconds since the fake PMM was created.
        """
        return self.now

    def send(self, request: Any) -> Any:
        """Answer one request.

        :param request: The script's request.
        :return: The script's response type, filled in.
        """
        key = (request.method, request.path)
        self.calls.append(key)
        self.tokens.append(request.headers.get("Authorization"))
        self.cookies.append(request.headers.get("Cookie"))
        if request.body:
            self.bodies[request.path] = json.loads(request.body)
        if self.refuse.get(key):
            status, text = self.refuse[key].pop(0)
            return run_task.Response(status, text.encode(), [])
        status, payload, headers = self.answer(*key)
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return run_task.Response(status, body.encode(), headers)

    def answer(self, method: str, path: str) -> tuple[int, Any, list[tuple[str, str]]]:
        """Return the status, the body and the headers for a request.

        :param method: The request's method.
        :param path: The request's path.
        :return: What the fake PMM answers.
        """
        if path.endswith("/session/exchange"):
            self.exchanges += 1
        if (method, path) == ("GET", f"{APP}/run1"):
            self.reads += 1
        status = self.statuses[min(self.reads, len(self.statuses)) - 1]
        services = [
            {"id": 2, "type": "mysql", "name": "dev-mysql"},
            {"id": 1, "type": "postgresql", "name": "pmm-server-postgresql"},
        ]
        answers: dict[tuple[str, str], tuple[int, Any, list[tuple[str, str]]]] = {
            ("POST", "/graph/login"): (
                200,
                {"message": "Logged in"},
                [
                    ("Set-Cookie", "pmm_session=abc; Path=/; HttpOnly"),
                    ("set-cookie", "grafana_session_expiry=17; Path=/"),
                ],
            ),
            ("POST", "/extensions/api/oauth/session/exchange"): (
                200,
                {"access_token": f"token-{self.exchanges}"},
                [],
            ),
            ("POST", "/extensions/api/apps/inventory/sync/"): (202, {}, []),
            ("GET", "/extensions/api/extensions/services/"): (
                200,
                {"items": services},
                [],
            ),
            ("GET", f"{APP}/schema"): (200, SCHEMA, []),
            ("POST", f"{APP}/"): (201, {"name": "run1", "status": None}, []),
            ("POST", f"{APP}/run1/execute"): (
                201,
                {"task_name": "run1", "task_id": 21, "status": "running"},
                [],
            ),
            ("GET", f"{APP}/run1"): (200, {"name": "run1", "status": status}, []),
            ("GET", "/extensions/stream-logs/21"): (200, LOG, []),
        }
        return answers.get((method, path), (404, {"detail": "Not Found"}, []))


@pytest.fixture
def body(tmp_path: Path) -> Path:
    """Write a create request that names its service.

    :param tmp_path: pytest's per-test temporary directory.
    :return: The request file.
    """
    path = tmp_path / "body.json"
    request = {"task_name": "run1", "hostname": "mysql-host", "service": "dev-mysql"}
    path.write_text(json.dumps(request), encoding="utf-8")
    return path


def run(pmm: FakePmm, *arguments: str) -> int:
    """Run the script against the fake PMM.

    :param pmm: The fake PMM.
    :param arguments: The command line's arguments after the app.
    :return: The script's exit status.
    """
    argv = ["--app", "duplicate_keys", *arguments]
    return run_task.main(argv, send=pmm.send, sleep=pmm.sleep, clock=pmm.clock)


def test_a_run_makes_its_calls_in_order_and_prints_the_record_and_the_log(
    body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sign in, exchange, copy the inventory, find the service, then run."""
    pmm = FakePmm(["running", "success"])
    assert run(pmm, "--body", str(body)) == 0
    assert pmm.calls[:6] == [
        ("POST", "/graph/login"),
        ("POST", "/extensions/api/oauth/session/exchange"),
        ("POST", "/extensions/api/apps/inventory/sync/"),
        ("GET", "/extensions/api/extensions/services/"),
        ("POST", f"{APP}/"),
        ("POST", f"{APP}/run1/execute"),
    ]
    assert pmm.bodies[f"{APP}/"] == {
        "task_name": "run1",
        "hostname": "mysql-host",
        "service_id": 2,
    }
    assert pmm.tokens[2] == "Bearer token-1"
    assert pmm.cookies[1] == "pmm_session=abc; grafana_session_expiry=17"
    out = capsys.readouterr().out
    assert '"status": "success"' in out
    assert "# Total Indexes  15" in out


def test_an_unknown_service_name_exits_2_and_prints_the_names_it_has(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refuse a service that PMM does not know, before anything is created."""
    path = tmp_path / "body.json"
    path.write_text('{"task_name": "run1", "service": "no-such"}', encoding="utf-8")
    pmm = FakePmm(["success"])
    assert run(pmm, "--body", str(path)) == run_task.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "dev-mysql" in err
    assert "pmm-server-postgresql" in err
    assert ("POST", f"{APP}/") not in pmm.calls


def test_a_401_in_the_middle_exchanges_the_session_once_more(body: Path) -> None:
    """Get a new token for a request that the old one no longer opens."""
    pmm = FakePmm(["success"])
    pmm.refuse[("POST", f"{APP}/")] = [(401, '{"detail": "expired"}')]
    assert run(pmm, "--body", str(body)) == 0
    creates = [
        token
        for call, token in zip(pmm.calls, pmm.tokens, strict=True)
        if call == ("POST", f"{APP}/")
    ]
    assert creates == ["Bearer token-1", "Bearer token-2"]
    assert pmm.exchanges == len(creates)


def test_a_final_status_other_than_success_exits_1_and_still_prints_the_log(
    body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a failed task, with what the tool printed."""
    pmm = FakePmm(["running", "failed"])
    assert run(pmm, "--body", str(body)) == run_task.EXIT_NOT_SUCCESS
    out = capsys.readouterr().out
    assert '"status": "failed"' in out
    assert "# Total Indexes  15" in out


def test_a_task_that_stays_running_exits_3(body: Path) -> None:
    """Give up after the timeout, and say so with a status of its own."""
    pmm = FakePmm(["running"])
    timeout = 30
    status = run(pmm, "--body", str(body), "--timeout", str(timeout))
    assert status == run_task.EXIT_TIMEOUT
    assert pmm.now >= timeout


def test_a_task_that_stays_running_reads_no_log(body: Path) -> None:
    """Read no log at the timeout: the log's stream stays open while the task runs."""
    pmm = FakePmm(["running"])
    run(pmm, "--body", str(body), "--timeout", "30")
    assert not [path for _, path in pmm.calls if "stream-logs" in path]


def test_a_request_file_that_is_absent_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a file that cannot be read as a refusal, not as a task that failed."""
    status = run(FakePmm(["success"]), "--body", str(tmp_path / "absent.json"))
    assert status == run_task.EXIT_REFUSED
    assert "absent.json" in capsys.readouterr().err


def test_a_request_without_a_task_name_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Name the field that the request file lacks."""
    path = tmp_path / "body.json"
    path.write_text(json.dumps({"hostname": "mysql-host"}), encoding="utf-8")
    assert run(FakePmm(["success"]), "--body", str(path)) == run_task.EXIT_REFUSED
    assert "task_name" in capsys.readouterr().err


def test_a_create_call_that_answers_404_exits_2_at_once(
    body: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stop at a request that the API refused, with the API's own message."""
    pmm = FakePmm(["success"])
    pmm.refuse[("POST", f"{APP}/")] = [(404, '{"detail": "no app duplicate_keys"}')]
    assert run(pmm, "--body", str(body)) == run_task.EXIT_REFUSED
    assert "no app duplicate_keys" in capsys.readouterr().err
    assert pmm.calls[-1] == ("POST", f"{APP}/")


def test_schema_prints_the_form_fields_and_runs_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print each field of the app's form, so that a request is written from it."""
    pmm = FakePmm(["success"])
    assert run(pmm, "--schema") == 0
    out = capsys.readouterr().out
    assert "task_name" in out
    assert "key_types" in out
    assert "fk" in out
    assert not [call for call in pmm.calls if call[1].startswith(f"{APP}/run1")]
    assert ("POST", f"{APP}/") not in pmm.calls


def test_the_port_and_the_password_default_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read ``PMM_PORT`` and ``PMM_ADMIN_PASSWORD`` when no argument names them."""
    monkeypatch.setenv("PMM_PORT", "8447")
    monkeypatch.setenv("PMM_ADMIN_PASSWORD", "secret")
    arguments = run_task.parse_arguments(["--app", "duplicate_keys", "--schema"])
    assert (arguments.port, arguments.password) == (8447, "secret")
