#!/usr/bin/env python3
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

"""Run one task of an app in the local PMM, and print its record and its log.

The script signs in to the local PMM of ``sidecar/dev/``, creates a task from a
JSON file, runs it, waits for its end, and prints the task's record and what
the tool printed. A ``"service": "<name>"`` entry of the file is replaced by the
``service_id`` of that service, after a copy of PMM's inventory. With
``--schema`` it prints the fields of the app's form and runs nothing. It never
deletes the task. It uses the standard library only.

The local PMM signs its own certificate and listens on ``127.0.0.1``, so the
certificate is not checked.

Exit status: 0 when the task ended with ``success``, 1 for any other final
status, 2 when the API refused a request, 3 when the task did not end in time.
"""

import argparse
import http.client
import json
import os
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API = "/extensions/api"
UNFINISHED = (None, "pending", "running")
EXIT_NOT_SUCCESS = 1
EXIT_REFUSED = 2
EXIT_TIMEOUT = 3
HTTP_UNAUTHORIZED = 401
HTTP_ERROR = 400
POLL_SECONDS = 2
SERVICE_WAIT_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Request:
    """One request to the local PMM.

    :param method: The HTTP method.
    :param path: The path on the local PMM, with its query string.
    :param body: The request's body, if it has one.
    :param headers: The request's headers.
    """

    method: str
    path: str
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Response:
    """The answer to one request.

    :param status: The HTTP status.
    :param body: The answer's body.
    :param headers: The answer's headers, as pairs. A name can repeat.
    """

    status: int
    body: bytes
    headers: list[tuple[str, str]]


Send = Callable[[Request], Response]


class RefusedError(Exception):
    """The API refused a request, or the request cannot be made."""


def make_sender(port: int) -> Send:
    """Return the function that sends a request to the local PMM.

    :param port: The port on ``127.0.0.1`` where PMM listens.
    :return: A function from a request to its answer, whatever the status.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    def send(request: Request) -> Response:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", port, context=context, timeout=REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request(
                request.method, request.path, body=request.body, headers=request.headers
            )
            answer = connection.getresponse()
            return Response(answer.status, answer.read(), answer.getheaders())
        except (OSError, http.client.HTTPException) as error:
            message = f"cannot reach the local PMM on port {port}: {error}"
            raise RefusedError(message) from error
        finally:
            connection.close()

    return send


def _detail(response: Response) -> str:
    """Return the API's own message for a refused request.

    :param response: The answer to the refused request.
    :return: The ``detail`` of a JSON answer, or the start of the body.
    """
    text = response.body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(payload, dict) and "detail" in payload:
        return json.dumps(payload["detail"])
    return text[:500]


class LocalPmm:
    """Call the API of the local PMM as its ``admin`` user.

    :param send: The function that sends one request.
    :param password: The password of PMM's ``admin`` user.
    """

    def __init__(self, send: Send, password: str) -> None:
        self._send = send
        self._password = password
        self._cookie = ""
        self._token = ""

    def sign_in(self) -> None:
        """Sign in to PMM and exchange the session for a token.

        :raises RefusedError: When PMM refuses the password.
        """
        credentials = json.dumps({"user": "admin", "password": self._password})
        answer = self._send(
            Request(
                "POST",
                "/graph/login",
                credentials.encode(),
                {"Content-Type": "application/json"},
            )
        )
        if answer.status >= HTTP_ERROR:
            message = f"PMM refused the sign-in ({answer.status}): {_detail(answer)}"
            raise RefusedError(message)
        self._cookie = "; ".join(
            value.split(";")[0]
            for name, value in answer.headers
            if name.lower() == "set-cookie"
        )
        self._exchange()

    def _exchange(self) -> None:
        """Exchange the PMM session for a token of PMM Extensions.

        :raises RefusedError: When the exchange is refused.
        """
        path = f"{API}/oauth/session/exchange"
        answer = self._send(Request("POST", path, None, {"Cookie": self._cookie}))
        if answer.status >= HTTP_ERROR:
            message = f"{path} answered {answer.status}: {_detail(answer)}"
            raise RefusedError(message)
        self._token = json.loads(answer.body)["access_token"]

    def call(self, method: str, path: str, body: Any = None) -> Response:
        """Send one request with the token, and get a new token once on a 401.

        :param method: The HTTP method.
        :param path: The path on the local PMM.
        :param body: A value to send as JSON, or ``None``.
        :return: The answer, with a status below 400.
        :raises RefusedError: When the API refuses the request.
        """
        data = None if body is None else json.dumps(body).encode()
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }
            answer = self._send(Request(method, path, data, headers))
            if answer.status != HTTP_UNAUTHORIZED or attempt > 1:
                break
            self._exchange()
        if answer.status >= HTTP_ERROR:
            message = f"{method} {path} answered {answer.status}: {_detail(answer)}"
            raise RefusedError(message)
        return answer

    def json(self, method: str, path: str, body: Any = None) -> Any:
        """Send one request and return its JSON answer.

        :param method: The HTTP method.
        :param path: The path on the local PMM.
        :param body: A value to send as JSON, or ``None``.
        :return: The decoded answer.
        :raises RefusedError: When the API refuses the request.
        """
        return json.loads(self.call(method, path, body).body)


def service_id(
    pmm: LocalPmm, name: str, sleep: Callable[[float], None], clock: Callable[[], float]
) -> int:
    """Return the identifier of a service, after a copy of PMM's inventory.

    :param pmm: The local PMM.
    :param name: The service's name in PMM.
    :param sleep: The function that waits.
    :param clock: The function that tells the time, in seconds.
    :return: The service's identifier in PMM Extensions.
    :raises RefusedError: When no service has that name after the copy.
    """
    pmm.call("POST", f"{API}/apps/inventory/sync/")
    deadline = clock() + SERVICE_WAIT_SECONDS
    while True:
        services = pmm.json("GET", f"{API}/extensions/services/")["items"]
        for service in services:
            if service["name"] == name:
                return service["id"]
        if clock() >= deadline:
            names = ", ".join(sorted(service["name"] for service in services))
            message = f"no service named {name}. PMM has: {names or 'none'}"
            raise RefusedError(message)
        sleep(POLL_SECONDS)


def print_schema(pmm: LocalPmm, app: str) -> None:
    """Print one line for each field of an app's form.

    :param pmm: The local PMM.
    :param app: The app's key.
    :raises RefusedError: When the API refuses the request.
    """
    schema = pmm.json("GET", f"{API}/apps/{app}/schema")
    for form in schema["forms"]:
        sys.stdout.write(f"{form['title']}\n")
        for entry in form["fields"]:
            parts = [entry["name"], entry["type"]]
            parts.append("required" if entry.get("required") else "optional")
            if "default" in entry:
                parts.append(f"default={json.dumps(entry['default'])}")
            if "choices" in entry:
                values = [choice["value"] for choice in entry["choices"]]
                parts.append(f"choices={json.dumps(values)}")
            sys.stdout.write(f"  {'  '.join(parts)}\n")
            if entry.get("description"):
                sys.stdout.write(f"      {entry['description']}\n")


def print_log(pmm: LocalPmm, run_id: int) -> None:
    """Print what the tool printed during one run.

    :param pmm: The local PMM.
    :param run_id: The run's identifier.
    :raises RefusedError: When the API refuses the request.
    """
    stream = pmm.call("GET", f"/extensions/stream-logs/{run_id}").body.decode(
        "utf-8", errors="replace"
    )
    for line in stream.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line.removeprefix("data: "))
        except ValueError:
            event = None
        if isinstance(event, dict) and "msg" in event:
            sys.stdout.write(event["msg"])
        else:
            sys.stdout.write(f"{line}\n")


def run_task(
    pmm: LocalPmm,
    arguments: argparse.Namespace,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    """Create one task, run it, wait for its end, and print what happened.

    :param pmm: The local PMM, signed in.
    :param arguments: The parsed command line.
    :param sleep: The function that waits.
    :param clock: The function that tells the time, in seconds.
    :return: The exit status.
    :raises RefusedError: When the API refuses a request.
    """
    try:
        body = json.loads(arguments.body.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        message = f"cannot read the request file {arguments.body}: {error}"
        raise RefusedError(message) from error
    if not isinstance(body, dict) or "task_name" not in body:
        raise RefusedError(f"the request file {arguments.body} names no task_name")
    if "service" in body:
        body["service_id"] = service_id(pmm, body.pop("service"), sleep, clock)
    task = f"{API}/apps/{arguments.app}/{body['task_name']}"
    pmm.call("POST", f"{API}/apps/{arguments.app}/", body)
    run_id = pmm.json("POST", f"{task}/execute", {})["task_id"]
    deadline = clock() + arguments.timeout
    record = pmm.json("GET", task)
    while record["status"] in UNFINISHED and clock() < deadline:
        sleep(POLL_SECONDS)
        record = pmm.json("GET", task)
    sys.stdout.write(f"{json.dumps(record, indent=2)}\n")
    if record["status"] in UNFINISHED:
        sys.stderr.write(f"the task did not end within {arguments.timeout} s\n")
        return EXIT_TIMEOUT
    print_log(pmm, run_id)
    return 0 if record["status"] == "success" else EXIT_NOT_SUCCESS


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    :param argv: The command line's arguments.
    :return: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--app", required=True, help="the app's key, as in its URL")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--body", type=Path, help="a JSON file: the create request")
    action.add_argument(
        "--schema", action="store_true", help="print the app's form fields and stop"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PMM_PORT", "8443"))
    )
    parser.add_argument(
        "--password", default=os.environ.get("PMM_ADMIN_PASSWORD", "admin")
    )
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait")
    return parser.parse_args(argv)


def main(
    argv: list[str],
    send: Send | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run one task in the local PMM, or print an app's form fields.

    :param argv: The command line's arguments.
    :param send: The function that sends one request. The default opens a
        connection to the local PMM.
    :param sleep: The function that waits.
    :param clock: The function that tells the time, in seconds.
    :return: The exit status.
    """
    arguments = parse_arguments(argv)
    pmm = LocalPmm(send or make_sender(arguments.port), arguments.password)
    try:
        pmm.sign_in()
        if arguments.schema:
            print_schema(pmm, arguments.app)
            return 0
        return run_task(pmm, arguments, sleep, clock)
    except RefusedError as error:
        sys.stderr.write(f"run-task: {error}\n")
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
