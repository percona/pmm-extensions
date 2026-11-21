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

"""Tests for ``scripts/migrate_standalone_schedules.py``.

Every fixture object comes from ``data/standalone_migration_capture.json``,
which holds responses captured from a standalone v0.13.1 instance and from
PMM Extensions 3.10.0. A test that needs another shape copies a captured
object and changes it.
"""

import copy
import datetime
import json
import os
import ssl
import sys
import types
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.scripts import load_script

migrate = load_script("migrate_standalone_schedules")

_CAPTURE = json.loads(
    (Path(__file__).parent / "data" / "standalone_migration_capture.json").read_text(
        "utf-8"
    )
)
_PMM_URL = "https://pmm.example"
_API = f"{_PMM_URL}/extensions/api"
_TOKEN = "glsa_test-token-value"
_SERVICE_A = "svcdocs-mysql-a"
_SERVICE_B = "svcdocs-mysql-b"
_PMM_SERVICE_A_ID = 3
_MYDUMPER = "svcdocs-a-mydumper"
_XTRABACKUP = "svcdocs-a-xtrabackup"
_RSYNC = "svcdocs-a-xtrabackup-rsync"
_HOST_B_TASK = "svcdocs-b-xtrabackup"
_PAGE_LIMIT = 50
_DB1_PMM_ID = 40
_ALL_SCHEDULES = 4
_ALL_TASKS = 4
_HOST_A_SCHEDULES = 3
_EXIT_USAGE = 2
_EXIT_PRECONDITION = 3
_EXIT_PARTIAL = 4
_WRITES_BEFORE_REJECTION = 4
_TASKS_BEFORE_REJECTION = 2
_WRITES_LEFT = 3
_PRIVATE_MODE = 0o600


def _export() -> dict[str, Any]:
    """Return a copy of the captured export of the old instance."""
    return copy.deepcopy(_CAPTURE["export"])


def _old_task(export: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the task named ``name`` from ``export``."""
    return next(task for task in export["tasks"]["items"] if task["name"] == name)


def _old_schedule(export: dict[str, Any], task_name: str) -> dict[str, Any]:
    """Return the first schedule of ``task_name`` from ``export``."""
    return next(
        schedule for schedule in export["schedules"] if schedule["task"] == task_name
    )


def _edit_settings(task: dict[str, Any], change: Any) -> None:
    """Apply ``change`` to the parsed settings of ``task`` and store them back."""
    settings = yaml.safe_load(task["data"]["meta"]["config"])
    change(settings)
    task["data"]["meta"]["config"] = yaml.dump(settings)


def _pmm_services(*names: str) -> list[dict[str, Any]]:
    """Return copies of the captured PMM services called ``names``."""
    return [
        copy.deepcopy(service)
        for service in _CAPTURE["pmm"]["services"]
        if service["name"] in names
    ]


def _parsed(export: dict[str, Any]) -> Any:
    """Parse ``export`` the way the script reads an export file."""
    return migrate.parse_export(json.dumps(export))


def _old_service(
    name: str = "db1", external_id: str | None = "old-1", port: int | None = 3306
) -> Any:
    """Build an old service on node 10.0.0.5."""
    return migrate.OldService(
        name=name, external_id=external_id, address="10.0.0.5", port=port
    )


def _pmm_service(
    service_id: int,
    name: str,
    external_id: str | None = None,
    address: str | None = "10.0.0.5",
    port: int = 3306,
) -> Any:
    """Build a PMM service."""
    return migrate.PmmService(
        id=service_id,
        name=name,
        external_id=external_id,
        address=address,
        port=port,
        last_synced_at="2026-11-18T22:30:06.390993Z",
    )


def _state(
    services: list[dict[str, Any]] | None = None,
    tasks: dict[str, int | None] | None = None,
    schedules: dict[str, list[dict[str, Any]]] | None = None,
    hosts: list[str] | None = None,
    forms: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Build what the script read from PMM Extensions.

    A task in ``tasks`` gets the form the script itself would send for it,
    unless ``forms`` gives another.
    """
    raw = _pmm_services(_SERVICE_A) if services is None else services
    existing = {}
    for name, service_id in (tasks or {}).items():
        form = (forms or {}).get(name)
        if form is None:
            form = migrate.task_form(_old_task(_export(), name), _PMM_SERVICE_A_ID)
        existing[name] = migrate.PmmTask(service_id=service_id, form=form)
    return migrate.PmmState(
        services=[migrate.pmm_service_from(item) for item in raw],
        tasks=existing,
        schedules=schedules or {},
        hosts=[host["id"] for host in _CAPTURE["pmm"]["hosts"]]
        if hosts is None
        else hosts,
    )


def _listed_schedule(**fields: Any) -> dict[str, Any]:
    """Build a schedule as PMM Extensions lists it, from the captured one, with ``fields`` changed."""
    listed = copy.deepcopy(_CAPTURE["pmm"]["created_schedule"])
    listed.update(interval=None, crontab=None)
    listed.update(fields)
    return listed


def _planned_names(plan: Any) -> list[str]:
    """Return the old schedule names a plan creates, in order."""
    return [schedule.old_name for task in plan.to_create for schedule in task.schedules]


class FakePmm:
    """Imitate PMM Extensions 3.10.0 in memory, answering as the captures show.

    A duplicate task name answers 409, a duplicate schedule 502, a task on an
    unknown service 404, and a list ``limit`` over 50 answers 422.

    """

    def __init__(
        self,
        *,
        services: list[dict[str, Any]] | None = None,
        me: dict[str, Any] | None = None,
    ) -> None:
        """Start with the captured system schedules and one MySQL service."""
        self.services = _pmm_services(_SERVICE_A) if services is None else services
        self.me = copy.deepcopy(_CAPTURE["pmm"]["me_admin"] if me is None else me)
        self.tasks: list[dict[str, Any]] = []
        self.schedules: list[dict[str, Any]] = copy.deepcopy(
            _CAPTURE["pmm"]["system_schedules"]
        )
        self.requests: list[Any] = []
        self.reject: Any = None
        self.hosts = copy.deepcopy(_CAPTURE["pmm"]["hosts"])

    def writes(self) -> list[Any]:
        """Return every request that is not a GET."""
        return [request for request in self.requests if request.method != "GET"]

    def __call__(self, request: Any) -> Any:
        """Answer one request."""
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {_TOKEN}":
            return self._answer(401, {"detail": "Could not validate credentials"})
        if self.reject is not None and (rejected := self.reject(request)) is not None:
            return rejected
        url = urllib.parse.urlsplit(request.url)
        path = url.path.removeprefix("/extensions/api")
        query = dict(urllib.parse.parse_qsl(url.query))
        if request.method == "GET":
            return self._get(path, query)
        body = json.loads(request.body)
        if path == "/apps/mysql_backups/":
            return self._create_task(body)
        return self._create_schedule(urllib.parse.unquote(path.split("/")[-2]), body)

    def _answer(self, status: int, payload: Any) -> Any:
        return migrate.Response(status=status, body=json.dumps(payload).encode())

    def _page(self, items: list[dict[str, Any]], query: dict[str, str]) -> Any:
        limit, offset = int(query.get("limit", 20)), int(query.get("offset", 0))
        if limit > _PAGE_LIMIT:
            return self._answer(422, {"detail": "limit"})
        page = items[offset : offset + limit]
        return self._answer(
            200, {"items": page, "total": len(items), "offset": offset, "limit": limit}
        )

    def _get(self, path: str, query: dict[str, str]) -> Any:
        if path == "/users/me":
            return self._answer(200, self.me)
        lists = {
            "/extensions/services/": self.services,
            "/apps/mysql_backups/": self.tasks,
            "/extensions/periodic-tasks/": self.schedules,
        }
        if path == "/extensions/hosts/":
            return self._answer(200, self.hosts)
        return self._page(lists[path], query)

    def _create_task(self, body: dict[str, Any]) -> Any:
        if not self.me["isAdmin"]:
            return self._answer(
                403, {"detail": "You don't have permission to perform this action"}
            )
        if any(task["name"] == body["task_name"] for task in self.tasks):
            return self._answer(
                409, {"detail": "Task with the same name already exists."}
            )
        if all(service["id"] != body["service_id"] for service in self.services):
            return self._answer(404, {"detail": "Not Found"})
        task = copy.deepcopy(_CAPTURE["pmm"]["created_task"])
        task["name"] = body["task_name"]
        task["hostname"] = body["hostname"]
        task["data"]["meta"]["_service_id"] = body["service_id"]
        task["data"]["_form"] = body
        self.tasks.append(task)
        return self._answer(201, task)

    def _create_schedule(self, task_name: str, body: dict[str, Any]) -> Any:
        if all(task["name"] != task_name for task in self.tasks):
            return self._answer(404, {"detail": "Not Found"})
        timing = (body.get("interval"), body.get("crontab"))
        for schedule in self.schedules:
            if (
                schedule["task"] == task_name
                and (schedule["interval"], schedule["crontab"]) == timing
            ):
                return self._answer(
                    502, {"detail": "An unexpected error occurred on the server."}
                )
        schedule = copy.deepcopy(_CAPTURE["pmm"]["created_schedule"])
        schedule.update(
            name=f"run_{task_name}_{len(self.schedules)}",
            task=task_name,
            interval=body.get("interval"),
            crontab=body.get("crontab"),
            enabled=body["enabled"],
        )
        self.schedules.append(schedule)
        return self._answer(201, schedule)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Write the export and the PMM token file into a temporary directory."""
    (tmp_path / "export.json").write_text(json.dumps(_export()), encoding="utf-8")
    (tmp_path / "pmm.token").write_text(f"{_TOKEN}\n", encoding="utf-8")
    return tmp_path


def _args(workdir: Path, *extra: str) -> list[str]:
    """Build the command line of a ``migrate`` run in ``workdir``."""
    return [
        "migrate",
        "--export",
        str(workdir / "export.json"),
        "--pmm-url",
        _PMM_URL,
        "--pmm-token-file",
        str(workdir / "pmm.token"),
        "--matches",
        str(workdir / "matches.json"),
        *extra,
    ]


def _reject_nth_write(count: int, status: int = 500) -> Any:
    """Return a hook that rejects the write numbered ``count``, counting from 1."""
    seen = []

    def hook(request: Any) -> Any:
        if request.method == "GET":
            return None
        seen.append(request)
        if len(seen) == count:
            return migrate.Response(
                status=status, body=b'{"detail": "rejected by the test"}'
            )
        return None

    return hook


def test_propose_matches_pairs_by_external_id() -> None:
    """Verify that two inventories synced from one PMM server share the service's external ID."""
    old = [_old_service(external_id="same-id")]
    pmm = [
        _pmm_service(
            _DB1_PMM_ID, "db1-mysql", external_id="same-id", address="10.9.9.9"
        )
    ]
    match = migrate.propose_matches(old, pmm)[0]
    assert match.pmm is not None
    assert match.pmm.id == _DB1_PMM_ID
    assert "external_id" in match.reason


def test_propose_matches_pairs_by_address_and_port() -> None:
    """Verify that without a shared external ID, the node address and the port decide."""
    match = migrate.propose_matches(
        [_old_service()], [_pmm_service(_DB1_PMM_ID, "db1-mysql", external_id="new-1")]
    )[0]
    assert match.pmm is not None
    assert match.pmm.id == _DB1_PMM_ID
    assert "address and port" in match.reason


def test_propose_matches_leaves_shared_address_unmatched() -> None:
    """Verify that two PMM services on one address and port are never guessed between."""
    pmm = [_pmm_service(40, "a"), _pmm_service(41, "b")]
    match = migrate.propose_matches([_old_service()], pmm)[0]
    assert match.pmm is None
    assert "2 PMM services" in match.reason


def test_propose_matches_leaves_missing_service_unmatched() -> None:
    """Verify that a service PMM does not have gets no match and a reason."""
    match = migrate.propose_matches([_old_service()], [])[0]
    assert match.pmm is None
    assert "no PMM service" in match.reason


def test_propose_matches_refuses_two_old_services_with_one_name() -> None:
    """Verify that the match file is keyed by name, so a repeated name cannot be written."""
    old = [_old_service(), _old_service(external_id="old-2", port=3307)]
    with pytest.raises(migrate.PreconditionError, match="db1"):
        migrate.propose_matches(old, [])


def test_match_file_round_trip_drops_null_entries(tmp_path: Path) -> None:
    """Verify that only an entry with a PMM service ID is a match."""
    path = tmp_path / "matches.json"
    matched = migrate.Match(
        _old_service(),
        _pmm_service(_DB1_PMM_ID, "db1-mysql"),
        "same node address and port",
    )
    unmatched = migrate.Match(_old_service(name="db2"), None, "no PMM service")
    migrate.write_match_file(path, [matched, unmatched])
    assert migrate.read_match_file(path) == {"db1": 40}


def test_match_file_shows_what_the_operator_checks(tmp_path: Path) -> None:
    """Verify that each entry carries the PMM service's name, address, port and last sync."""
    path = tmp_path / "matches.json"
    matches = migrate.propose_matches(_parsed(_export()).services, _state().services)
    migrate.write_match_file(path, matches)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written[_SERVICE_A]["pmm_service_id"] == _PMM_SERVICE_A_ID
    assert written[_SERVICE_A]["pmm"] == {
        "name": _SERVICE_A,
        "node_address": "172.31.213.11",
        "port": 3306,
        "last_synced_at": "2026-11-18T22:30:06.390993Z",
    }
    assert written[_SERVICE_B]["pmm_service_id"] is None
    assert written[_SERVICE_B]["pmm"] is None


def test_read_match_file_refuses_a_file_that_is_not_a_match_file(
    tmp_path: Path,
) -> None:
    """Verify that a hand-edited file with the wrong shape stops the run before any write."""
    path = tmp_path / "matches.json"
    path.write_text('{"db1": {"pmm_service_id": "40"}}', encoding="utf-8")
    with pytest.raises(migrate.PreconditionError, match="db1"):
        migrate.read_match_file(path)


def test_parse_export_refuses_a_truncated_list() -> None:
    """Verify that a paged list whose total is larger than its items is not a whole export."""
    export = _export()
    export["tasks"]["total"] = 7
    with pytest.raises(migrate.PreconditionError, match="4 of 7"):
        _parsed(export)


@pytest.mark.parametrize(
    "damage",
    [
        lambda export: export.pop("schedules"),
        lambda export: export.update(tasks=export["tasks"]["items"]),
        lambda export: export["schedules"][0].pop("task"),
        lambda export: export["schedules"][0].update(crontab={"minute": "0"}),
        lambda export: export["tasks"]["items"][0]["data"].pop("meta"),
        lambda export: export.update(format="something-else"),
    ],
)
def test_parse_export_refuses_the_wrong_shape(damage: Any) -> None:
    """Verify that a file that is not an export is refused, whatever is wrong with it."""
    export = _export()
    damage(export)
    with pytest.raises(migrate.PreconditionError):
        _parsed(export)


def test_parse_export_refuses_text_that_is_not_json() -> None:
    """Verify that an HTML error page saved as the export is refused."""
    with pytest.raises(migrate.PreconditionError, match="JSON"):
        migrate.parse_export("<html>303 See Other</html>")


def test_task_form_matches_the_body_pmm_accepted() -> None:
    """Verify that the form built from a captured task equals the form 3.10.0 stored for it."""
    stored = _CAPTURE["pmm"]["created_task"]["data"]["_form"]
    form = migrate.task_form(_old_task(_export(), _RSYNC), _PMM_SERVICE_A_ID)
    assert form["task_name"] == _RSYNC
    assert form["service_id"] == _PMM_SERVICE_A_ID
    assert form["upload"] == ["rsync"]
    assert {key: stored[key] for key in form} == form


def test_task_form_reads_wrapped_and_quoted_values() -> None:
    """Verify that a long free-text value survives the way PyYAML wraps and quotes it."""
    extra_args = "--regex '^(?!(mysql\\.|sys\\.))' " + " ".join(
        f"--option-{index}=value: {index}" for index in range(30)
    )
    task = _old_task(_export(), _MYDUMPER)
    _edit_settings(
        task,
        lambda settings: settings["ALL_SERVERS"].update(MYDUMPER_EXTRA_ARGS=extra_args),
    )
    assert extra_args not in task["data"]["meta"]["config"]
    assert (
        migrate.task_form(task, _PMM_SERVICE_A_ID)["mydumper_extra_args"] == extra_args
    )


def test_task_form_carries_the_encryption_recipient() -> None:
    """Verify that the recipient moves from the server entry to its own form field."""
    task = _old_task(_export(), _XTRABACKUP)
    _edit_settings(
        task,
        lambda settings: settings["SERVER_LIST"][0].update(
            DIR_ENCRYPT_CONFIG={"encryption recipient": "backup-key"}
        ),
    )
    assert (
        migrate.task_form(task, _PMM_SERVICE_A_ID)["encryption_recipient"]
        == "backup-key"
    )


def test_build_plan_creates_every_schedule_disabled() -> None:
    """Verify that every schedule arrives paused, whatever it was on the old instance."""
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    bodies = [schedule.body for task in plan.to_create for schedule in task.schedules]
    assert len(bodies) == _HOST_A_SCHEDULES
    assert [body["enabled"] for body in bodies] == [False, False, False]
    assert all(task.create_task for task in plan.to_create)


def test_build_plan_copies_the_interval_and_the_crontab_unchanged() -> None:
    """Verify that the timing objects of the old list are the bodies 3.10.0 accepted."""
    export = _export()
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    by_task = {task.name: task.schedules[0].body for task in plan.to_create}
    assert by_task[_MYDUMPER] == {
        "interval": {"every": 1, "period": "days"},
        "enabled": False,
    }
    assert by_task[_RSYNC] == {
        "crontab": _old_schedule(export, _RSYNC)["crontab"],
        "enabled": False,
    }


def test_build_plan_skips_service_absent_from_match_file() -> None:
    """Verify that nothing is created for a service the match file does not match."""
    plan = migrate.build_plan(_parsed(_export()), {}, _state())
    assert plan.to_create == []
    assert len(plan.skipped) == _ALL_SCHEDULES
    name, reason = plan.skipped[-1]
    assert name == _old_schedule(_export(), _HOST_B_TASK)["name"]
    assert reason == f"service {_SERVICE_B} has no match in the match file"


def test_build_plan_skips_a_match_to_a_service_pmm_does_not_have() -> None:
    """Verify that a hand-edited ID that PMM does not know creates nothing."""
    plan = migrate.build_plan(_parsed(_export()), {_SERVICE_A: 99}, _state())
    assert plan.to_create == []
    assert "99" in plan.skipped[0][1]


def test_build_plan_skips_schedule_that_already_exists() -> None:
    """Verify that a schedule PMM already lists for the task is not planned again."""
    export = _export()
    schedule = _old_schedule(export, _MYDUMPER)
    existing = {_MYDUMPER: [_listed_schedule(interval=schedule["interval"])]}
    state = _state(tasks={_MYDUMPER: _PMM_SERVICE_A_ID}, schedules=existing)
    plan = migrate.build_plan(_parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, state)
    planned = _planned_names(plan)
    assert planned
    assert len(planned) == _HOST_A_SCHEDULES - 1
    assert schedule["name"] not in planned
    assert plan.already_present == [schedule["name"]]


def test_build_plan_completes_a_task_that_has_no_schedule_yet() -> None:
    """Verify that a run that stopped between the two writes left a task; only its schedule is planned."""
    state = _state(tasks={_MYDUMPER: _PMM_SERVICE_A_ID})
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, state
    )
    task = next(task for task in plan.to_create if task.name == _MYDUMPER)
    assert task.create_task is False
    assert len(task.schedules) == 1


def test_build_plan_skips_a_task_name_taken_by_another_service() -> None:
    """Verify that a PMM task of the same name on another service is not given schedules."""
    state = _state(tasks={_MYDUMPER: 77})
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, state
    )
    assert _MYDUMPER not in [task.name for task in plan.to_create]
    assert "77" in dict(plan.skipped)[_old_schedule(_export(), _MYDUMPER)["name"]]


def test_build_plan_skips_schedule_using_an_unknown_setting() -> None:
    """Verify that 3.10.0 drops a field it does not know, so the script refuses the schedule and names the key."""
    export = _export()
    _edit_settings(
        _old_task(export, _XTRABACKUP),
        lambda settings: settings["ALL_SERVERS"].update(REMOVED_FIELD=1),
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert sorted(task.name for task in plan.to_create) == [_MYDUMPER, _RSYNC]
    assert (
        "REMOVED_FIELD"
        in dict(plan.skipped)[_old_schedule(export, _XTRABACKUP)["name"]]
    )


def test_build_plan_skips_schedule_using_an_unknown_server_key() -> None:
    """Verify that an unknown key in the server entry is refused the same way."""
    export = _export()
    _edit_settings(
        _old_task(export, _XTRABACKUP),
        lambda settings: settings["SERVER_LIST"][0].update(SOCKET="/x"),
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert sorted(task.name for task in plan.to_create) == [_MYDUMPER, _RSYNC]
    assert "SOCKET" in dict(plan.skipped)[_old_schedule(export, _XTRABACKUP)["name"]]


def test_build_plan_skips_task_with_no_backup_dir() -> None:
    """Verify that 3.10.0 requires a backup directory that v0.13.1 left optional."""
    export = _export()
    _edit_settings(
        _old_task(export, _XTRABACKUP),
        lambda settings: settings["ALL_SERVERS"].pop("BACKUP_DIR"),
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert (
        "BACKUP_DIR" in dict(plan.skipped)[_old_schedule(export, _XTRABACKUP)["name"]]
    )
    assert len(_planned_names(plan)) == _HOST_A_SCHEDULES - 1


def test_build_plan_skips_task_whose_service_left_the_old_inventory() -> None:
    """Verify that a task that names a service the old inventory no longer has cannot be matched."""
    export = _export()
    export["services"]["items"] = [
        item for item in export["services"]["items"] if item["name"] != _SERVICE_A
    ]
    export["services"]["total"] = 1
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert plan.to_create == []
    assert (
        "old inventory" in dict(plan.skipped)[_old_schedule(export, _MYDUMPER)["name"]]
    )


def test_build_plan_skips_schedule_whose_task_is_not_in_the_export() -> None:
    """Verify that a schedule of a task the export does not hold is named, not guessed at."""
    export = _export()
    export["schedules"][0]["task"] = "gone"
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert "gone" in dict(plan.skipped)[export["schedules"][0]["name"]]


def test_build_plan_skips_schedule_with_a_chained_request() -> None:
    """Verify that a chained request is named, not dropped in silence."""
    export = _export()
    export["schedules"][0]["execute_request"] = {"chain": ["other-task"]}
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert "execute_request" in dict(plan.skipped)[export["schedules"][0]["name"]]


def test_build_plan_carries_a_start_time_and_a_description() -> None:
    """Verify that 3.10.0 stores both fields, so a schedule that has them keeps them."""
    export = _export()
    export["schedules"][0].update(
        start_time="2026-12-01T00:00:00Z", description="nightly logical dump"
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert plan.to_create[0].schedules[0].body == {
        "interval": {"every": 1, "period": "days"},
        "start_time": "2026-12-01T00:00:00Z",
        "description": "nightly logical dump",
        "enabled": False,
    }


def test_build_plan_gives_one_task_all_of_its_schedules() -> None:
    """Verify that one task with two schedules is one task write and two schedule writes."""
    export = _export()
    second = copy.deepcopy(_old_schedule(export, _MYDUMPER))
    second.update(
        name="run_svcdocs-a-mydumper_every_6_hours_1",
        interval={"every": 6, "period": "hours"},
    )
    export["schedules"].append(second)
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    task = next(task for task in plan.to_create if task.name == _MYDUMPER)
    assert [schedule.body["interval"]["every"] for schedule in task.schedules] == [1, 6]
    assert [planned.name for planned in plan.to_create].count(_MYDUMPER) == 1


def test_build_plan_lists_old_backup_locations() -> None:
    """Verify that every old task's backup directory is listed, matched or not."""
    plan = migrate.build_plan(_parsed(_export()), {}, _state())
    locations = {
        (location.service, location.task): location
        for location in plan.backup_locations
    }
    assert len(locations) == _ALL_TASKS
    assert (
        locations[(_SERVICE_A, _MYDUMPER)].directory
        == "/var/backups/svcdocs/mydumper/172.31.213.11"
    )
    assert (
        locations[(_SERVICE_B, _HOST_B_TASK)].directory
        == "/var/backups/svcdocs/xtrabackup/172.31.213.12"
    )
    assert locations[(_SERVICE_A, _MYDUMPER)].upload is None
    assert locations[(_SERVICE_A, _RSYNC)].upload == "rsync: /var/backups/svcdocs-rsync"


def test_dry_run_sends_no_write_request(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that without --apply the script reads, writes the match file and prints the plan."""
    pmm = FakePmm()
    assert migrate.main(_args(workdir), http=pmm) == 0
    assert pmm.writes() == []
    assert migrate.read_match_file(workdir / "matches.json") == {
        _SERVICE_A: _PMM_SERVICE_A_ID
    }
    out = capsys.readouterr().out
    assert "3 schedules to create" in out
    assert f"service {_SERVICE_B} has no match in the match file" in out
    assert "/var/backups/svcdocs-up/xtrabackup/172.31.213.11" in out
    assert "No write request was sent" in out


def test_dry_run_keeps_an_existing_match_file(workdir: Path) -> None:
    """Verify that a match file the operator edited is read, never rewritten."""
    edited = json.dumps(
        {_SERVICE_A: {"pmm_service_id": None, "reason": "edited by hand"}}
    )
    (workdir / "matches.json").write_text(edited, encoding="utf-8")
    assert migrate.main(_args(workdir), http=FakePmm()) == 0
    assert (workdir / "matches.json").read_text(encoding="utf-8") == edited


def test_output_never_holds_the_token(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that the token is read from a file and printed nowhere."""
    assert migrate.main(_args(workdir), http=FakePmm()) == 0
    captured = capsys.readouterr()
    assert "3 schedules to create" in captured.out
    assert _TOKEN not in captured.out + captured.err


def test_apply_without_match_file_is_a_usage_error(workdir: Path) -> None:
    """Verify that --apply creates only what an existing match file holds."""
    pmm = FakePmm()
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == _EXIT_USAGE
    assert pmm.requests == []


def test_apply_stops_before_any_write_when_token_is_not_admin(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that a Viewer reads every list, so only the role check stops the run in time."""
    pmm = FakePmm(me=_CAPTURE["pmm"]["me_viewer"])
    migrate.main(_args(workdir), http=pmm)
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == _EXIT_PRECONDITION
    assert pmm.writes() == []
    assert "admin" in capsys.readouterr().err


def test_a_rejected_token_is_a_precondition_failure(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that HTTP 401 on the first read ends the run with one clear line."""
    (workdir / "pmm.token").write_text("not-the-token", encoding="utf-8")
    assert migrate.main(_args(workdir), http=FakePmm()) == _EXIT_PRECONDITION
    assert "401" in capsys.readouterr().err


def test_a_truncated_export_stops_before_any_request(workdir: Path) -> None:
    """Verify that the export is checked before PMM is asked anything."""
    export = _export()
    export["services"]["total"] = 9
    (workdir / "export.json").write_text(json.dumps(export), encoding="utf-8")
    pmm = FakePmm()
    assert migrate.main(_args(workdir), http=pmm) == _EXIT_PRECONDITION
    assert pmm.requests == []


def test_apply_creates_three_paused_schedules(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that each schedule is a task write and then a schedule write, with enabled false."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == 0
    assert [request.method for request in pmm.writes()] == ["POST"] * 6
    created = [
        schedule
        for schedule in pmm.schedules
        if schedule["task"].startswith("svcdocs-")
    ]
    assert [schedule["enabled"] for schedule in created] == [False, False, False]
    assert {task["name"] for task in pmm.tasks} == {_MYDUMPER, _XTRABACKUP, _RSYNC}
    assert "created 3 of 3 schedules" in capsys.readouterr().out


def test_apply_stops_on_first_rejected_schedule_and_reports(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that a rejected write ends the run with what was created so far."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    pmm.reject = _reject_nth_write(_WRITES_BEFORE_REJECTION)
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == _EXIT_PARTIAL
    captured = capsys.readouterr()
    assert "created 1 of 3 schedules" in captured.out
    assert "HTTP 500" in captured.err
    assert len(pmm.writes()) == _WRITES_BEFORE_REJECTION


def test_second_apply_creates_only_the_rest(workdir: Path) -> None:
    """Verify that a run that stopped between a task and its schedule is completed, not duplicated."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    pmm.reject = _reject_nth_write(_WRITES_BEFORE_REJECTION)
    migrate.main(_args(workdir, "--apply"), http=pmm)
    assert len(pmm.tasks) == _TASKS_BEFORE_REJECTION
    pmm.reject = None
    pmm.requests.clear()
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == 0
    posted = [urllib.parse.urlsplit(request.url).path for request in pmm.writes()]
    assert len(posted) == _WRITES_LEFT
    assert posted.count("/extensions/api/apps/mysql_backups/") == 1
    assert len(pmm.tasks) == _HOST_A_SCHEDULES
    assert (
        len(
            [
                schedule
                for schedule in pmm.schedules
                if schedule["task"].startswith("svcdocs-")
            ]
        )
        == _HOST_A_SCHEDULES
    )


def test_third_apply_finds_every_schedule_by_listing(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that 3.10.0 answers 502 for a duplicate schedule, so the script never sends one."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    migrate.main(_args(workdir, "--apply"), http=pmm)
    pmm.requests.clear()
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == 0
    assert pmm.writes() == []
    assert "3 schedules already in PMM" in capsys.readouterr().out


def test_existing_tasks_are_found_beyond_the_first_page(workdir: Path) -> None:
    """Verify that the task list gives 50 per page at most, and every page is read."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    migrate.main(_args(workdir, "--apply"), http=pmm)
    ours = pmm.tasks
    filler = []
    for index in range(60):
        task = copy.deepcopy(ours[0])
        task["name"] = f"filler-{index}"
        filler.append(task)
    pmm.tasks = filler + ours
    pmm.requests.clear()
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == 0
    assert pmm.writes() == []


def test_missing_pyyaml_is_one_line_and_exit_2(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that without PyYAML the script says how to get it and sends nothing."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    pmm = FakePmm()
    assert migrate.main(_args(workdir), http=pmm) == _EXIT_USAGE
    assert pmm.requests == []
    err = capsys.readouterr().err.strip()
    assert "PyYAML" in err
    assert len(err.splitlines()) == 1


def test_tls_is_verified_unless_the_option_says_otherwise(tmp_path: Path) -> None:
    """Verify that the default context checks the certificate and the host name."""
    default = migrate.tls_context(ca_file=None, insecure=False)
    assert default.verify_mode == ssl.CERT_REQUIRED
    assert default.check_hostname is True
    assert migrate.tls_context(ca_file=None, insecure=True).verify_mode == ssl.CERT_NONE


class FakeStandalone:
    """Serve the tasks and inventory lists of an installed v0.13.1, two items per page."""

    def __init__(self) -> None:
        """Serve the captured lists."""
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        """Answer one list request."""
        self.requests.append(request)
        if request.headers.get("Authorization") != "Bearer internal-token":
            return migrate.Response(status=401, body=b'{"detail": "Not authenticated"}')
        url = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(url.query))
        export = _export()
        if url.netloc == "tasks_api:9002" and url.path == "/periodic/":
            return migrate.Response(
                status=200, body=json.dumps(export["schedules"]).encode()
            )
        items = export["tasks" if url.netloc == "tasks_api:9002" else "services"][
            "items"
        ]
        offset = int(query.get("offset", 0))
        page = {
            "items": items[offset : offset + 2],
            "total": len(items),
            "offset": offset,
            "limit": 2,
        }
        return migrate.Response(status=200, body=json.dumps(page).encode())


def _export_args(tmp_path: Path) -> list[str]:
    """Build the command line of an ``export`` run with every source given."""
    (tmp_path / "old.token").write_text("internal-token\n", encoding="utf-8")
    return [
        "export",
        "--tasks-url",
        "https://tasks_api:9002",
        "--inventory-url",
        "https://inventory_api:9001",
        "--token-file",
        str(tmp_path / "old.token"),
    ]


def test_export_writes_three_whole_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that the export follows every page and its output is what migrate reads."""
    old = FakeStandalone()
    assert migrate.main(_export_args(tmp_path), http=old) == 0
    assert all(request.method == "GET" for request in old.requests)
    out = capsys.readouterr().out
    assert "internal-token" not in out
    parsed = migrate.parse_export(out)
    assert len(parsed.schedules) == _ALL_SCHEDULES
    assert len(parsed.tasks) == _ALL_TASKS
    assert [service.name for service in parsed.services] == [_SERVICE_A, _SERVICE_B]


def test_export_with_a_rejected_token_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that a failed read leaves no partial export behind."""
    args = _export_args(tmp_path)
    (tmp_path / "old.token").write_text("wrong", encoding="utf-8")
    assert migrate.main(args, http=FakeStandalone()) == _EXIT_PRECONDITION
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "401" in captured.err


def test_stdout_to_stderr_keeps_the_export_clean(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Verify that What the old application logs to standard output must not reach the export."""
    with migrate.stdout_to_stderr():
        os.write(1, b"a log line\n")
    os.write(1, b"the export\n")
    captured = capfd.readouterr()
    assert captured.out == "the export\n"
    assert captured.err == "a log line\n"


class _LoggingSettings:
    """Imitate settings of a standalone install that log to standard output when read."""

    SSL_CAFILE = None

    def __init__(self, **values: Any) -> None:
        """Hold the values to return."""
        self._values = values

    def __getattr__(self, name: str) -> Any:
        """Return a value, after writing a log line to the process's standard output."""
        os.write(1, b"WARNING the value '*' in ALLOWED_HOSTS matches any hostname\n")
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None


class _Secret:
    """Stand in for the secret wrapper that the standalone settings use."""

    def get_secret_value(self) -> str:
        """Return the token."""
        return "internal-token"


def test_export_inside_the_install_reads_its_settings(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Verify that with no option, the export takes URLs and token from the install, and its log lines stay out."""
    core = types.SimpleNamespace(
        settings=_LoggingSettings(SEP_INTERNAL_TOKEN=_Secret())
    )
    sep = types.SimpleNamespace(
        sep_settings=_LoggingSettings(
            TASKS_ENDPOINT="https://tasks_api:9002/",
            INVENTORY_ENDPOINT="https://inventory_api:9001/",
        )
    )
    monkeypatch.setitem(sys.modules, "app.core.config", core)
    monkeypatch.setitem(sys.modules, "app.sep.config", sep)
    assert migrate.main(["export"], http=FakeStandalone()) == 0
    captured = capfd.readouterr()
    assert "WARNING" in captured.err
    assert len(migrate.parse_export(captured.out).schedules) == _ALL_SCHEDULES


def test_export_outside_the_install_says_what_it_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that without the install's settings and without options, the export is a usage error."""
    monkeypatch.setitem(sys.modules, "app.sep.config", None)
    assert migrate.main(["export"], http=FakeStandalone()) == _EXIT_USAGE
    assert "--tasks-url" in capsys.readouterr().err


def test_build_plan_skips_task_whose_host_pmm_cannot_run_on() -> None:
    """Verify that a task is not created for an execution host that PMM Extensions does not have."""
    state = _state(hosts=["pmm-server", "another-host"])
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, state
    )
    assert plan.to_create == []
    reason = dict(plan.skipped)[_old_schedule(_export(), _MYDUMPER)["name"]]
    assert "execution host svcdocs-mysql-a" in reason


def test_dry_run_shows_the_execution_host(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that the plan names the host each task runs on."""
    assert migrate.main(_args(workdir), http=FakePmm()) == 0
    assert (
        f"{_MYDUMPER} on {_SERVICE_A} (PMM service 3), runs on host svcdocs-mysql-a, new task"
        in capsys.readouterr().out
    )


def test_dry_run_plans_a_task_whose_host_has_another_display_name(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that the host check reads the executor's ID, not its inventory label."""
    pmm = FakePmm()
    for host in pmm.hosts:
        host["name"] = f"Display name of {host['id']}"
    assert migrate.main(_args(workdir), http=pmm) == 0
    out = capsys.readouterr().out
    assert (
        f"{_MYDUMPER} on {_SERVICE_A} (PMM service 3), runs on host svcdocs-mysql-a"
        in out
    )
    assert "is not a host PMM Extensions can run a task on" not in out


def test_build_plan_skips_same_name_task_with_other_settings() -> None:
    """Verify that a PMM task of the same name and service but other settings is not given schedules."""
    other = migrate.task_form(_old_task(_export(), _XTRABACKUP), _PMM_SERVICE_A_ID)
    other.update(backup_type="M", compress=False)
    state = _state(tasks={_XTRABACKUP: _PMM_SERVICE_A_ID}, forms={_XTRABACKUP: other})
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, state
    )
    assert _XTRABACKUP not in [task.name for task in plan.to_create]
    reason = dict(plan.skipped)[_old_schedule(_export(), _XTRABACKUP)["name"]]
    assert "backup_type" in reason
    assert "compress" in reason


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"XTRABACKUP_AES256_KEYFILE": "/root/key"}, "aes256"),
        ({"ENCRYPT": True}, "gpg"),
        ({"POST_RUN_ENCRYPT": True}, "gpg"),
        ({"ENCRYPT": True, "XTRABACKUP_AES256_KEYFILE": "/root/key"}, "dual"),
        ({"ENCRYPT": True, "ENCRYPTION_FORMAT": "dual"}, "dual"),
    ],
)
def test_task_form_infers_the_encryption_format(
    settings: dict[str, Any], expected: str
) -> None:
    """Verify that an encrypted v0.13.1 task gets the format 3.10.0 requires, which v0.13.1 never stored."""
    task = _old_task(_export(), _XTRABACKUP)
    _edit_settings(task, lambda stored: stored["ALL_SERVERS"].update(settings))
    assert migrate.task_form(task, _PMM_SERVICE_A_ID)["encryption_format"] == expected


def test_task_form_leaves_the_encryption_format_out_of_a_plain_task() -> None:
    """Verify that a task with no encryption sends no format, so PMM Extensions uses its default."""
    form = migrate.task_form(_old_task(_export(), _XTRABACKUP), _PMM_SERVICE_A_ID)
    assert form
    assert "encryption_format" not in form


@pytest.mark.parametrize(
    "difference",
    [
        {"start_time": "2027-01-01T00:00:00Z"},
        {"execute_request": {"chain": ["other-task"]}},
    ],
)
def test_build_plan_reports_a_schedule_that_differs_beyond_its_timing(
    difference: dict[str, Any],
) -> None:
    """Verify that a PMM schedule with the same timing but another start or a chain is a conflict, not a match."""
    export = _export()
    schedule = _old_schedule(export, _MYDUMPER)
    existing = {
        _MYDUMPER: [_listed_schedule(interval=schedule["interval"], **difference)]
    }
    state = _state(tasks={_MYDUMPER: _PMM_SERVICE_A_ID}, schedules=existing)
    plan = migrate.build_plan(_parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, state)
    planned = _planned_names(plan)
    assert planned
    assert plan.already_present == []
    assert schedule["name"] not in planned
    assert next(iter(difference)) in dict(plan.skipped)[schedule["name"]]


def test_build_plan_matches_a_start_time_written_another_way() -> None:
    """Verify that the same instant in another notation is the same start time."""
    export = _export()
    schedule = _old_schedule(export, _MYDUMPER)
    schedule["start_time"] = "2026-12-01T00:00:00Z"
    listed = _listed_schedule(
        interval=schedule["interval"], start_time="2026-12-01T00:00:00+00:00"
    )
    state = _state(
        tasks={_MYDUMPER: _PMM_SERVICE_A_ID}, schedules={_MYDUMPER: [listed]}
    )
    plan = migrate.build_plan(_parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, state)
    assert plan.already_present == [schedule["name"]]


def test_build_plan_skips_task_matched_to_a_service_on_another_host() -> None:
    """Verify that a task that ran on its database host is not created for a service on another host."""
    service = _pmm_services(_SERVICE_A)[0]
    service["node"]["name"] = "another-host"
    state = _state(services=[service], hosts=["svcdocs-mysql-a", "another-host"])
    plan = migrate.build_plan(
        _parsed(_export()), {_SERVICE_A: _PMM_SERVICE_A_ID}, state
    )
    assert plan.to_create == []
    reason = dict(plan.skipped)[_old_schedule(_export(), _XTRABACKUP)["name"]]
    assert "svcdocs-mysql-a" in reason
    assert "another-host" in reason


def test_build_plan_lists_the_backup_location_of_a_task_it_skips() -> None:
    """Verify that a task with a setting the script does not know still has its backup directory listed."""
    export = _export()
    _edit_settings(
        _old_task(export, _XTRABACKUP),
        lambda settings: settings["ALL_SERVERS"].update(REMOVED_FIELD=1),
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    locations = {
        location.task: location.directory for location in plan.backup_locations
    }
    assert locations[_XTRABACKUP] == "/var/backups/svcdocs/xtrabackup/172.31.213.11"


def test_plan_names_a_present_schedule_that_is_enabled(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that a schedule already in PMM is shown with its state, because the script does not pause it."""
    pmm = FakePmm()
    migrate.main(_args(workdir), http=pmm)
    migrate.main(_args(workdir, "--apply"), http=pmm)
    next(schedule for schedule in pmm.schedules if schedule["task"] == _MYDUMPER)[
        "enabled"
    ] = True
    capsys.readouterr()
    assert migrate.main(_args(workdir), http=pmm) == 0
    out = capsys.readouterr().out
    assert f"{_old_schedule(_export(), _MYDUMPER)['name']}: enabled in PMM" in out
    assert f"{_old_schedule(_export(), _XTRABACKUP)['name']}: paused in PMM" in out


def test_export_refuses_a_partial_set_of_source_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify that one source option without the other two is a usage error, not a silent fallback."""
    http = FakeStandalone()
    assert (
        migrate.main(["export", "--tasks-url", "https://tasks_api:9002"], http=http)
        == _EXIT_USAGE
    )
    assert http.requests == []
    assert "together" in capsys.readouterr().err


def test_export_reports_settings_that_fail_to_load(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that settings the old application refuses to build end in a usage error, not a traceback."""

    class _Broken:
        def __getattr__(self, name: str) -> Any:
            raise ValueError("1 validation error for Settings")

    monkeypatch.setitem(
        sys.modules, "app.core.config", types.SimpleNamespace(settings=_Broken())
    )
    monkeypatch.setitem(
        sys.modules, "app.sep.config", types.SimpleNamespace(sep_settings=_Broken())
    )
    assert migrate.main(["export"], http=FakeStandalone()) == _EXIT_USAGE
    assert "--tasks-url" in capsys.readouterr().err


def test_export_output_file_is_private(tmp_path: Path) -> None:
    """Verify that an export written to a file is readable by its owner only, even when the file existed."""
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)
    args = [*_export_args(tmp_path), "--output", str(output)]
    assert migrate.main(args, http=FakeStandalone()) == 0
    assert output.stat().st_mode & 0o777 == _PRIVATE_MODE
    assert (
        len(migrate.parse_export(output.read_text(encoding="utf-8")).schedules)
        == _ALL_SCHEDULES
    )


def test_build_plan_skips_task_with_a_value_json_cannot_carry() -> None:
    """Verify that a stored date is found by the plan, not by a failed write in the middle of a run."""
    export = _export()
    _edit_settings(
        _old_task(export, _XTRABACKUP),
        lambda settings: settings["ALL_SERVERS"].update(
            XTRABACKUP_EXTRA_ARGS=datetime.date(2026, 1, 1)
        ),
    )
    plan = migrate.build_plan(
        _parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, _state()
    )
    assert sorted(task.name for task in plan.to_create) == [_MYDUMPER, _RSYNC]
    assert "JSON" in dict(plan.skipped)[_old_schedule(export, _XTRABACKUP)["name"]]


def test_an_unwritable_match_file_is_a_usage_error(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that a match file in a directory that does not exist ends the run with one line."""
    args = _args(workdir)
    args[args.index("--matches") + 1] = str(workdir / "absent" / "matches.json")
    assert migrate.main(args, http=FakePmm()) == _EXIT_USAGE
    assert "cannot write the match file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "answer",
    [
        {"items": [], "total": None, "offset": 0, "limit": 50},
        {"items": [{"name": "no-id"}], "total": 1, "offset": 0, "limit": 50},
    ],
)
def test_a_malformed_list_from_pmm_is_a_precondition_failure(
    workdir: Path, answer: dict[str, Any]
) -> None:
    """Verify that a list PMM Extensions answers in another shape stops the run before any write."""
    pmm = FakePmm()

    def odd_services(request: Any) -> Any:
        if "/extensions/services/" in request.url:
            return migrate.Response(status=200, body=json.dumps(answer).encode())
        return None

    pmm.reject = odd_services
    assert migrate.main(_args(workdir), http=pmm) == _EXIT_PRECONDITION
    assert pmm.writes() == []


def test_two_old_services_with_one_name_are_refused_with_a_match_file_present(
    workdir: Path,
) -> None:
    """Verify that the repeated name is refused on every run, not only when the match file is first written."""
    export = _export()
    clone = copy.deepcopy(export["services"]["items"][0])
    clone["port"] = 3307
    export["services"]["items"].append(clone)
    export["services"]["total"] = len(export["services"]["items"])
    (workdir / "export.json").write_text(json.dumps(export), encoding="utf-8")
    (workdir / "matches.json").write_text(
        json.dumps({_SERVICE_A: {"pmm_service_id": 3}}), encoding="utf-8"
    )
    pmm = FakePmm()
    assert migrate.main(_args(workdir, "--apply"), http=pmm) == _EXIT_PRECONDITION
    assert pmm.requests == []


def test_build_plan_finds_the_matching_schedule_behind_a_conflicting_one() -> None:
    """Verify that every PMM schedule with the same timing is examined, not only the first."""
    export = _export()
    schedule = _old_schedule(export, _MYDUMPER)
    listed = [
        _listed_schedule(
            interval=schedule["interval"], start_time="2027-01-01T00:00:00Z"
        ),
        _listed_schedule(interval=schedule["interval"], enabled=True),
    ]
    state = _state(tasks={_MYDUMPER: _PMM_SERVICE_A_ID}, schedules={_MYDUMPER: listed})
    plan = migrate.build_plan(_parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, state)
    assert plan.already_present == [schedule["name"]]
    assert plan.already_enabled == [schedule["name"]]


def test_build_plan_reads_a_start_time_with_no_zone_as_utc() -> None:
    """Verify that a start time with no zone equals the same instant written with one."""
    export = _export()
    schedule = _old_schedule(export, _MYDUMPER)
    schedule["start_time"] = "2026-12-01T00:00:00"
    listed = _listed_schedule(
        interval=schedule["interval"], start_time="2026-12-01T00:00:00+00:00"
    )
    state = _state(
        tasks={_MYDUMPER: _PMM_SERVICE_A_ID}, schedules={_MYDUMPER: [listed]}
    )
    plan = migrate.build_plan(_parsed(export), {_SERVICE_A: _PMM_SERVICE_A_ID}, state)
    assert plan.already_present == [schedule["name"]]
