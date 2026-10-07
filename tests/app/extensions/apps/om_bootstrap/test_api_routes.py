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

"""Test the run lifecycle: create, dispatch a step, read progress.

Scoped to this app's own logic — planning, host/step lookups, conflict
detection — not PMM Extensions' cross-cutting admin-role gate
(``require_minimum_role_for_unsafe_methods``), which resolves its own
credential outside FastAPI's dependency-override seam and needs the full
``extensions_app`` plus a real Bearer credential to exercise honestly; that is a
framework-level concern with its own test surface, not something this app's
tests should re-prove. ``@require_minimum_role(UserRole.ADMIN)`` on the
mutating routes is asserted by inspection instead
(``TestAdminGateIsRegistered``).
"""

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import aiohttp
import pytest
from fastapi import FastAPI, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.deps import minimum_role_for
from app.core.auth.models import UserRole
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.apps.om_bootstrap.api_routes import (
    cancel_run,
    dispatch_finalize_step,
    dispatch_rollback_step,
    dispatch_run_run_step,
    dispatch_run_step,
    finish_run,
    trigger_run,
)
from app.extensions.apps.om_bootstrap.crud import BootstrapRunManager
from app.extensions.apps.om_bootstrap.models import BootstrapRun, BootstrapRunStatus
from app.extensions.apps.om_bootstrap.persistence import dump_host_states
from app.extensions.apps.om_bootstrap.strategy import (
    HostBootstrapState,
    StepRecord,
    StepStatus,
)
from app.extensions.deps import get_session
from tests.app.extensions.apps.om_bootstrap.conftest import api_client, BASE
from tests.app.extensions.apps.om_bootstrap.factories import BootstrapRunFactory

FAKE_TASK_HISTORY_ID = 7


def _fake_tasks_api() -> MagicMock:
    """Build a stand-in Tasks API client whose ``.auth()`` is a real context manager.

    Injected through ``get_tasks_client``'s dependency override rather than left
    to auto-mock: a bare ``AsyncMock``'s attributes default to ``MagicMock``,
    whose ``.auth(token)`` call is fine, but the production code's
    ``with tasks_api.auth(...):`` needs that return value to actually support
    the context-manager protocol, which a default ``MagicMock`` return value
    does not do meaningfully (it "works" but silently no-ops in a way that
    masked a real bug the first time this test was written).
    """
    client = MagicMock()
    client.auth.return_value = nullcontext()
    return client


class TestAdminGateIsRegistered:
    """Assert the mutating routes actually registered the ADMIN minimum.

    See the module docstring for why this is inspection rather than an
    end-to-end 403 — ``minimum_role_for`` is the exact function the real gate
    consults, so this is asserting the same fact the gate would enforce, just
    without needing the full auth stack to observe it.
    """

    def test_trigger_run_requires_admin(self) -> None:
        """Require admin to create a run, since it is root-adjacent."""
        assert minimum_role_for_endpoint(trigger_run) == UserRole.ADMIN

    def test_dispatch_run_step_requires_admin(self) -> None:
        """Gate step dispatch as admin-only, since it is literal root execution."""
        assert minimum_role_for_endpoint(dispatch_run_step) == UserRole.ADMIN

    def test_dispatch_run_run_step_requires_admin(self) -> None:
        """Gate a run-level dispatch (rs.initiate, user creation) the same way."""
        assert minimum_role_for_endpoint(dispatch_run_run_step) == UserRole.ADMIN

    def test_dispatch_rollback_step_requires_admin(self) -> None:
        """Treat tearing down a host as privileged as building it up."""
        assert minimum_role_for_endpoint(dispatch_rollback_step) == UserRole.ADMIN

    def test_dispatch_finalize_step_requires_admin(self) -> None:
        """Gate enabling auth and restarting mongod as equally privileged."""
        assert minimum_role_for_endpoint(dispatch_finalize_step) == UserRole.ADMIN

    def test_finish_run_requires_admin(self) -> None:
        """Treat declaring a run failed/rolled back as the stepper's privileged call."""
        assert minimum_role_for_endpoint(finish_run) == UserRole.ADMIN

    def test_cancel_run_requires_admin(self) -> None:
        """Gate aborting a live deployment as privileged as starting one."""
        assert minimum_role_for_endpoint(cancel_run) == UserRole.ADMIN


def minimum_role_for_endpoint(endpoint: object) -> UserRole:
    """Read the role ``@require_minimum_role`` registered for ``endpoint``.

    :param endpoint: The decorated route function.
    :return: Its registered minimum role.
    """

    class _FakeRoute:
        endpoint: object = None

    route = _FakeRoute()
    route.endpoint = endpoint
    return minimum_role_for(route)


class TestTriggerRun:
    """Assert POST /runs plans every host's steps and persists them."""

    def test_creates_a_run_with_every_host_planned(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Plan every requested host's full step list, all pending."""
        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs",
            json={
                "hosts": ["node00", "node01", "node02"],
                "install_method": "packages",
                "os": "ubuntu",
                "mongodb_version": "8.0",
                "replica_set_name": "rs-test",
                "data_path": "/var/lib/mongo",
                "log_path": "/var/log/mongodb/mongod.log",
                "port": 27017,
                "bind_ip": "0.0.0.0",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert {host["host"] for host in body["hosts"]} == {
            "node00",
            "node01",
            "node02",
        }
        for host in body["hosts"]:
            assert host["steps"]
            assert all(step["status"] == "pending" for step in host["steps"])
            assert host["finalize_steps"]
            assert all(step["status"] == "pending" for step in host["finalize_steps"])

    def test_accepts_per_host_member_configs(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Round-trip a per-host election override through creation and a re-read.

        Asserts the stored values, not just the status code: a 201 alone
        still passes if member_configs were silently discarded before
        persistence.
        """
        client = api_client(regular_user, session, _fake_tasks_api())
        override = {
            "priority": 0,
            "votes": False,
            "hidden": True,
            "delay_secs": 300,
            # A concrete address, not None: None would pass even if a supplied value
            # were dropped by model_dump, persistence or the response reconstruction,
            # and PMM reads the echo to tell an older side-car from one that applied
            # the per-member address.
            "bind_ip": "10.1.2.3",
        }
        response = client.post(
            f"{BASE}/runs",
            json={
                "hosts": ["node00", "node01", "node02"],
                "install_method": "packages",
                "os": "ubuntu",
                "mongodb_version": "8.0",
                "replica_set_name": "rs-test",
                "data_path": "/var/lib/mongo",
                "log_path": "/var/log/mongodb/mongod.log",
                "port": 27017,
                "bind_ip": "0.0.0.0",
                "member_configs": {"node01": override},
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["member_configs"] == {"node01": override}

        # Re-read from the database, not the same in-memory response — proves
        # the value round-trips through persistence, not just the request echo.
        run_id = response.json()["id"]
        reread = client.get(f"{BASE}/runs/{run_id}")
        assert reread.json()["member_configs"] == {"node01": override}

    def test_rejects_an_empty_host_list(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject a run over no hosts as a request error, not a no-op run."""
        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs",
            json={
                "hosts": [],
                "install_method": "packages",
                "os": "ubuntu",
                "mongodb_version": "8.0",
                "replica_set_name": "rs-test",
                "data_path": "/var/lib/mongo",
                "log_path": "/var/log/mongodb/mongod.log",
                "port": 27017,
                "bind_ip": "0.0.0.0",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_rejects_a_repeated_host(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject duplicate hosts, which no dispatch route could tell apart."""
        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs",
            json={
                "hosts": ["node00", "node00"],
                "install_method": "packages",
                "os": "ubuntu",
                "mongodb_version": "8.0",
                "replica_set_name": "rs-test",
                "data_path": "/var/lib/mongo",
                "log_path": "/var/log/mongodb/mongod.log",
                "port": 27017,
                "bind_ip": "0.0.0.0",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_rejects_an_install_method_with_no_registered_strategy(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject DOCKER/PODMAN: declared on the enum, not implemented yet."""
        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs",
            json={
                "hosts": ["node00"],
                "install_method": "docker",
                "os": "ubuntu",
                "mongodb_version": "8.0",
                "replica_set_name": "rs-test",
                "data_path": "/var/lib/mongo",
                "log_path": "/var/log/mongodb/mongod.log",
                "port": 27017,
                "bind_ip": "0.0.0.0",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestTriggerRunValidation:
    """Assert POST /runs rejects hosts, versions and names of the wrong shape."""

    @staticmethod
    def _payload(**overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "hosts": ["node00"],
            "install_method": "packages",
            "os": "ubuntu",
            "mongodb_version": "8.0",
            "replica_set_name": "rs-test",
        }
        payload.update(overrides)
        return payload

    @pytest.mark.parametrize("host_count", [2, 4])
    def test_rejects_a_host_count_other_than_one_or_three(
        self, regular_user: CasdoorUser, session: AsyncSession, host_count: int
    ) -> None:
        """Reject anything but a one-member or three-member replica set."""
        hosts = [f"node0{index}" for index in range(host_count)]

        response = api_client(regular_user, session).post(
            f"{BASE}/runs", json=self._payload(hosts=hosts)
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.parametrize(
        "host", ["node00;reboot", "../etc", "-node", "node 00", "", "a" * 254]
    )
    def test_rejects_a_host_that_is_not_a_node_name(
        self, regular_user: CasdoorUser, session: AsyncSession, host: str
    ) -> None:
        """Reject a host that could not safely be a script filename or target."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs", json=self._payload(hosts=[host])
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize("version", ["8", "8.0;id", "latest", "8.0.4.1", ""])
    def test_rejects_a_malformed_mongodb_version(
        self, regular_user: CasdoorUser, session: AsyncSession, version: str
    ) -> None:
        """Reject a version that is not major.minor[.patch]."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs", json=self._payload(mongodb_version=version)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize("name", ["rs\nnet: {}", "rs test", "", "r" * 65])
    def test_rejects_a_malformed_replica_set_name(
        self, regular_user: CasdoorUser, session: AsyncSession, name: str
    ) -> None:
        """Reject a name that could break the mongod.conf it is written into."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs", json=self._payload(replica_set_name=name)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    @pytest.mark.parametrize("bind_ip", ["10.0.0.1\nnet: {}", "10.0.0.1 10.0.0.2", ""])
    def test_rejects_a_malformed_member_bind_ip(
        self, regular_user: CasdoorUser, session: AsyncSession, bind_ip: str
    ) -> None:
        """Reject a per-member bind address with whitespace or control characters."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs",
            json=self._payload(member_configs={"node00": {"bind_ip": bind_ip}}),
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_accepts_a_full_patch_version(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Accept major.minor.patch as well as major.minor."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs", json=self._payload(mongodb_version="7.0.14")
        )

        assert response.status_code == status.HTTP_201_CREATED

    @pytest.mark.parametrize(
        "config", [{"priority": 0, "votes": False}, {"priority": 0, "votes": True}]
    )
    def test_rejects_member_configs_with_no_electable_member(
        self,
        regular_user: CasdoorUser,
        session: AsyncSession,
        config: dict[str, object],
    ) -> None:
        """Reject a member set where no host both votes and has nonzero priority."""
        response = api_client(regular_user, session).post(
            f"{BASE}/runs",
            json=self._payload(member_configs={"node00": config}),
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestListBootstrapRuns:
    """Assert GET /runs discovers runs by status, newest first."""

    async def _seed_run(
        self, session: AsyncSession, run_status: BootstrapRunStatus
    ) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                status=run_status,
            ),
        )

    @pytest.mark.asyncio
    async def test_filters_by_status(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Show a caller re-discovering in-flight runs only the running ones."""
        running = await self._seed_run(session, BootstrapRunStatus.RUNNING)
        await self._seed_run(session, BootstrapRunStatus.SUCCEEDED)

        response = api_client(regular_user, session, _fake_tasks_api()).get(
            f"{BASE}/runs?status=running"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {run["id"] for run in body} == {str(running.id)}

    @pytest.mark.asyncio
    async def test_returns_every_status_when_unfiltered(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """List runs regardless of where they landed when ``status`` is omitted."""
        first = await self._seed_run(session, BootstrapRunStatus.RUNNING)
        second = await self._seed_run(session, BootstrapRunStatus.SUCCEEDED)

        response = api_client(regular_user, session, _fake_tasks_api()).get(
            f"{BASE}/runs"
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {run["id"] for run in body} == {str(first.id), str(second.id)}


class TestGetBootstrapRun:
    """Assert GET /runs/{id} reconciles and reports 404 for an unknown run."""

    async def _seed_run(self, session: AsyncSession) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(
                                    name="pre_check",
                                    status=StepStatus.RUNNING,
                                    task_history_id=42,
                                )
                            ],
                        )
                    ]
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_returns_404_for_an_unknown_run(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 404 for a run id nobody created, not a 500 or an empty 200."""
        response = api_client(regular_user, session, _fake_tasks_api()).get(
            f"{BASE}/runs/{uuid4()}"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_reflects_a_reconciled_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reconcile before responding, so a just-finished step shows up now."""
        run = await self._seed_run(session)

        async def _fake_reconcile(
            _tasks_api: object, reconciled_run: BootstrapRun
        ) -> bool:
            reconciled_run.hosts = dump_host_states(
                [
                    HostBootstrapState(
                        host="node00",
                        steps=[
                            StepRecord(name="pre_check", status=StepStatus.SUCCEEDED)
                        ],
                    )
                ]
            )
            return True

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.reconcile_run",
                _fake_reconcile,
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).get(
                f"{BASE}/runs/{run.id}"
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["hosts"][0]["steps"][0]["status"] == "succeeded"


class TestDispatchRunStep:
    """Assert POST .../steps/{name}:dispatch validates host/step and dispatches."""

    async def _seed_run(self, session: AsyncSession) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(name="pre_check"),
                                StepRecord(
                                    name="configure_repository",
                                    status=StepStatus.RUNNING,
                                    task_history_id=1,
                                ),
                            ],
                        )
                    ]
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_dispatches_a_pending_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Dispatch a pending step, mark it running, and record its task id."""
        run = await self._seed_run(session)

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        body = response.json()
        step = next(s for s in body["hosts"][0]["steps"] if s["name"] == "pre_check")
        assert step["status"] == "running"
        assert step["task_history_id"] == FAKE_TASK_HISTORY_ID

    @pytest.mark.asyncio
    async def test_records_a_dispatch_that_the_tasks_api_rejects(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Record a dispatch the Tasks API itself rejects as a FAILED step, not a 5xx.

        Without this, a step the Tasks API never even accepts (an unknown or
        unreachable executor target, most concretely) stays PENDING forever:
        nothing ever transitions it, so the stepper's own retry-then-rollback
        policy never engages, and every tick looks identical to the very
        first attempt.
        """
        run = await self._seed_run(session)

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(
                    side_effect=HTTPException(
                        status_code=400, detail="Target 'node00' is not available"
                    )
                ),
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        body = response.json()
        step = next(s for s in body["hosts"][0]["steps"] if s["name"] == "pre_check")
        assert step["status"] == "failed"
        assert step["attempt_count"] == 1
        assert "not available" in step["detail"]
        assert step["task_history_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            aiohttp.ClientConnectionError("Cannot connect to host tasks:8443"),
            TimeoutError(),
            PermissionError("scratch directory is not writable"),
        ],
    )
    async def test_records_a_dispatch_that_never_reaches_the_tasks_api(
        self, regular_user: CasdoorUser, session: AsyncSession, error: Exception
    ) -> None:
        """Record a transport failure or a failed script write as a FAILED attempt."""
        run = await self._seed_run(session)

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
            AsyncMock(side_effect=error),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        step = next(
            s for s in response.json()["hosts"][0]["steps"] if s["name"] == "pre_check"
        )
        assert step["status"] == "failed"
        assert step["attempt_count"] == 1
        assert step["detail"].startswith("Failed to dispatch: ")
        assert step["task_history_id"] is None

    @pytest.mark.asyncio
    async def test_records_a_dispatch_the_tasks_api_accepts_without_an_id(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Apply the same treatment when dispatch_step's own contract is violated."""
        run = await self._seed_run(session)

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(
                    side_effect=RuntimeError(
                        "Tasks API did not return a task history id"
                    )
                ),
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        step = next(
            s for s in response.json()["hosts"][0]["steps"] if s["name"] == "pre_check"
        )
        assert step["status"] == "failed"
        assert step["attempt_count"] == 1

    @pytest.mark.asyncio
    async def test_404s_for_an_unknown_host(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Refuse to dispatch a step on a host that isn't part of the run."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/no-such-host/steps/pre_check:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_404s_for_an_unplanned_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject a step name outside the host's planned list rather than run it."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node00/steps/rs_initiate:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_409s_for_a_step_already_running(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject dispatching an in-flight step as a conflict, not a double-dispatch."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node00/steps/configure_repository:dispatch"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step_status", [StepStatus.SUCCEEDED, StepStatus.SKIPPED])
    async def test_409s_for_a_step_already_done(
        self, regular_user: CasdoorUser, session: AsyncSession, step_status: StepStatus
    ) -> None:
        """Refuse to re-run a succeeded or skipped step on the host."""
        run = await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[StepRecord(name="pre_check", status=step_status)],
                        )
                    ]
                ),
            ),
        )
        dispatch_step_mock = AsyncMock(return_value=FAKE_TASK_HISTORY_ID)

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
            dispatch_step_mock,
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_409_CONFLICT
        dispatch_step_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_a_failed_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Retry a failed step, which is how the stepper retries."""
        prior_attempts = 1
        run = await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(
                                    name="pre_check",
                                    status=StepStatus.FAILED,
                                    attempt_count=prior_attempts,
                                )
                            ],
                        )
                    ]
                ),
            ),
        )

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
            AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        step = response.json()["hosts"][0]["steps"][0]
        assert step["status"] == "running"
        assert step["attempt_count"] == prior_attempts + 1

    @pytest.mark.asyncio
    async def test_increments_attempt_count_on_each_dispatch(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Count every dispatch, which PMM's stepper reads to cap retries."""
        run = await self._seed_run(session)

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        step = next(
            s for s in response.json()["hosts"][0]["steps"] if s["name"] == "pre_check"
        )
        assert step["attempt_count"] == 1

    @pytest.mark.asyncio
    async def test_forwards_body_params_to_build_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Pass a caller-supplied secret (e.g. keyFile content) to the built action."""
        run = await self._seed_run(session)
        build_step = MagicMock(return_value=MagicMock(command=["true"], timeout_s=1))

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
            ),
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.strategy_for",
                return_value=MagicMock(build_step=build_step),
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch",
                json={"params": {"key_file_content": "secret-bytes"}},
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        build_step.assert_called_once()
        assert build_step.call_args.args[-1] == {"key_file_content": "secret-bytes"}

    @pytest.mark.asyncio
    async def test_400s_when_the_strategy_rejects_the_params(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Blame the caller, not the server, for a strategy's bad-params ValueError."""
        run = await self._seed_run(session)
        build_step = MagicMock(side_effect=ValueError("missing required param"))

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.strategy_for",
            return_value=MagicMock(build_step=build_step),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/steps/pre_check:dispatch"
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestDispatchRunRunStep:
    """Assert POST .../run-steps/{name}:dispatch targets the seed host."""

    async def _seed_run(self, session: AsyncSession) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(name="verify", status=StepStatus.SUCCEEDED)
                            ],
                        ),
                        HostBootstrapState(
                            host="node01",
                            steps=[
                                StepRecord(name="verify", status=StepStatus.SUCCEEDED)
                            ],
                        ),
                    ]
                ),
                run_steps=[
                    {"name": "rs_initiate", "status": "pending", "attempt_count": 0}
                ],
            ),
        )

    @pytest.mark.asyncio
    async def test_dispatches_to_the_first_host(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Run rs.initiate on hosts[0], the seed member, not any other host."""
        run = await self._seed_run(session)
        dispatch_step_mock = AsyncMock(return_value=FAKE_TASK_HISTORY_ID)

        with (
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                dispatch_step_mock,
            ),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/run-steps/rs_initiate:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert dispatch_step_mock.call_args.args[3] == "node00"
        run_step = response.json()["run_steps"][0]
        assert run_step["status"] == "running"

    @pytest.mark.asyncio
    async def test_404s_for_an_unplanned_run_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject a run-level name outside the run's own planned list."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/run-steps/create_pmm_monitoring_user:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_409s_for_a_run_step_already_running(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Refuse to dispatch a run-level step that is already dispatching."""
        run = await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00", steps=[StepRecord(name="verify")]
                        )
                    ]
                ),
                run_steps=[
                    {
                        "name": "rs_initiate",
                        "status": "running",
                        "attempt_count": 1,
                        "task_history_id": 1,
                    }
                ],
            ),
        )

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/run-steps/rs_initiate:dispatch"
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestDispatchRollbackStep:
    """Assert POST .../rollback/{name}:dispatch validates and dispatches teardown."""

    async def _seed_run(self, session: AsyncSession) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(
                                    name="install_package", status=StepStatus.FAILED
                                )
                            ],
                            rollback_steps=[StepRecord(name="stop_service")],
                        )
                    ]
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_dispatches_a_pending_rollback_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Dispatch a pending rollback step, scoped to its run, and mark it running."""
        run = await self._seed_run(session)
        dispatch = AsyncMock(return_value=FAKE_TASK_HISTORY_ID)

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.dispatch_step", dispatch
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/rollback/stop_service:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        rollback_step = response.json()["hosts"][0]["rollback_steps"][0]
        assert rollback_step["status"] == "running"
        assert dispatch.await_args is not None
        action = dispatch.await_args.args[-1]
        assert f"= {run.id} ] || exit 0" in action.command[2]

    @pytest.mark.asyncio
    async def test_404s_for_an_unplanned_rollback_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Reject a forward step name as a rollback step name."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node00/rollback/install_package:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDispatchFinalizeStep:
    """Assert POST .../finalize/{name}:dispatch validates and dispatches enable_auth."""

    async def _seed_run(self, session: AsyncSession) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(name="verify", status=StepStatus.SUCCEEDED)
                            ],
                            finalize_steps=[StepRecord(name="enable_auth")],
                        )
                    ]
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_dispatches_a_pending_finalize_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Dispatch a pending finalize step, marking it running with its task id."""
        run = await self._seed_run(session)

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
            AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
        ):
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}/hosts/node00/finalize/enable_auth:dispatch"
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        finalize_step = response.json()["hosts"][0]["finalize_steps"][0]
        assert finalize_step["status"] == "running"
        assert finalize_step["task_history_id"] == FAKE_TASK_HISTORY_ID

    @pytest.mark.asyncio
    async def test_404s_for_an_unplanned_finalize_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 404 for a forward step name, which is not a finalize step."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node00/finalize/verify:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_404s_for_an_unknown_host(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 404 for a finalize dispatch on a host outside this run."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node99/finalize/enable_auth:dispatch"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_409s_for_a_finalize_step_already_running(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 409 for an in-flight finalize step instead of dispatching it twice."""
        run = await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(name="verify", status=StepStatus.SUCCEEDED)
                            ],
                            finalize_steps=[
                                StepRecord(
                                    name="enable_auth",
                                    status=StepStatus.RUNNING,
                                    task_history_id=FAKE_TASK_HISTORY_ID,
                                )
                            ],
                        )
                    ]
                ),
            ),
        )

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}/hosts/node00/finalize/enable_auth:dispatch"
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestFinishRun:
    """Assert POST /runs/{id}:finish records the stepper's own terminal decision."""

    async def _seed_run(
        self,
        session: AsyncSession,
        run_status: BootstrapRunStatus = BootstrapRunStatus.RUNNING,
    ) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                status=run_status,
            ),
        )

    @pytest.mark.asyncio
    async def test_marks_a_running_run_failed(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Record the stepper declaring retries exhausted as FAILED, with its reason."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}:finish",
            json={"status": "failed", "error": "node00 exhausted retries"},
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] == "node00 exhausted retries"
        assert body["finished_at"] is not None

    @pytest.mark.asyncio
    async def test_sweeps_the_runs_step_scripts(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Sweep a finished run's scripts, whatever state their steps were in."""
        run = await self._seed_run(session)

        with patch(
            "app.extensions.apps.om_bootstrap.api_routes.cleanup_run_scripts"
        ) as cleanup:
            response = api_client(regular_user, session, _fake_tasks_api()).post(
                f"{BASE}/runs/{run.id}:finish", json={"status": "rolled_back"}
            )

        assert response.status_code == status.HTTP_200_OK
        cleanup.assert_called_once_with(str(run.id))

    @pytest.mark.asyncio
    async def test_rejects_succeeded_as_a_requested_status(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Refuse SUCCEEDED here: reconciliation infers it, callers never request it."""
        run = await self._seed_run(session)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}:finish", json={"status": "succeeded"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.asyncio
    async def test_409s_for_an_already_terminal_run(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Refuse to finish a run already FAILED/ROLLED_BACK/SUCCEEDED."""
        run = await self._seed_run(session, BootstrapRunStatus.SUCCEEDED)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}:finish", json={"status": "rolled_back"}
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestWritingRoutesLockTheRun:
    """Assert every route that writes a run back reads it under the row lock."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "", None),
            ("post", "/hosts/node00/steps/pre_check:dispatch", None),
            ("post", "/hosts/node00/rollback/stop_service:dispatch", None),
            ("post", "/run-steps/rs_initiate:dispatch", None),
            ("post", "/hosts/node00/finalize/enable_auth:dispatch", None),
            ("post", ":finish", {"status": "failed"}),
            ("post", ":cancel", None),
        ],
    )
    async def test_reads_the_run_for_update(
        self,
        regular_user: CasdoorUser,
        session: AsyncSession,
        method: str,
        path: str,
        body: dict[str, str] | None,
    ) -> None:
        """Lock the row, so two concurrent writes cannot race and lose one.

        The lock and the route's save must share one session, and so one
        transaction: the route resolves ``get_session`` exactly once.
        """
        run = await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[StepRecord(name="pre_check")],
                            rollback_steps=[StepRecord(name="stop_service")],
                            finalize_steps=[StepRecord(name="enable_auth")],
                        )
                    ]
                ),
                run_steps=[
                    {"name": "rs_initiate", "status": "pending", "attempt_count": 0}
                ],
            ),
        )
        get_run = AsyncMock(return_value=run)

        with (
            patch.object(BootstrapRunManager, "get_run", get_run),
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.dispatch_step",
                AsyncMock(return_value=FAKE_TASK_HISTORY_ID),
            ),
            patch(
                "app.extensions.apps.om_bootstrap.api_routes.reconcile_run",
                AsyncMock(return_value=False),
            ),
        ):
            client = api_client(regular_user, session, _fake_tasks_api())
            session_resolutions: list[AsyncSession] = []

            def _session() -> AsyncSession:
                session_resolutions.append(session)
                return session

            assert isinstance(client.app, FastAPI)
            client.app.dependency_overrides[get_session] = _session
            url = f"{BASE}/runs/{run.id}{path}"
            response = (
                client.get(url) if method == "get" else client.post(url, json=body)
            )

        assert response.status_code < status.HTTP_300_MULTIPLE_CHOICES
        assert get_run.await_args is not None
        assert get_run.await_args.kwargs == {"for_update": True}
        assert get_run.await_args.args[0] is session
        assert len(session_resolutions) == 1


class TestCancelRun:
    """Assert POST /runs/{id}:cancel records the request and stops what's running."""

    async def _seed_run(
        self,
        session: AsyncSession,
        run_status: BootstrapRunStatus = BootstrapRunStatus.RUNNING,
        *,
        cancel_requested: bool = False,
    ) -> BootstrapRun:
        return await BootstrapRunManager.save(
            session,
            BootstrapRunFactory.build(
                status=run_status,
                cancel_requested=cancel_requested,
                hosts=dump_host_states(
                    [
                        HostBootstrapState(
                            host="node00",
                            steps=[
                                StepRecord(
                                    name="install_package",
                                    status=StepStatus.RUNNING,
                                    task_history_id=FAKE_TASK_HISTORY_ID,
                                )
                            ],
                        )
                    ]
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_sets_cancel_requested_and_stops_the_running_step(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Stop a live step's Nomad allocation instead of letting it run out its timeout."""
        run = await self._seed_run(session)
        tasks_api = _fake_tasks_api()
        tasks_api.post = AsyncMock(return_value=None)

        response = api_client(regular_user, session, tasks_api).post(
            f"{BASE}/runs/{run.id}:cancel"
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        body = response.json()
        assert body["cancel_requested"] is True
        tasks_api.post.assert_awaited_once_with(
            f"/history/{FAKE_TASK_HISTORY_ID}/stop/"
        )

    @pytest.mark.asyncio
    async def test_tolerates_a_step_that_fails_to_stop(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Tolerate a Tasks API rejection while stopping a step.

        PMM's stepper's rollback decision only needs cancel_requested set — see
        bootstrap_decision.go's runNeedsRollback — so a step this route couldn't
        stop is not fatal here.
        """
        run = await self._seed_run(session)
        tasks_api = _fake_tasks_api()
        tasks_api.post = AsyncMock(
            side_effect=HTTPException(status_code=400, detail="already stopped")
        )

        response = api_client(regular_user, session, tasks_api).post(
            f"{BASE}/runs/{run.id}:cancel"
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_tolerates_the_tasks_api_being_unreachable(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Tolerate a transport failure while stopping a step.

        The Tasks API being down is precisely the outage an operator is likely
        to be hitting Abort over — ``cancel_requested`` must still save (see
        ``test_saves_cancel_requested_before_stopping_steps``), and this call
        must not turn that outage into a 500.
        """
        run = await self._seed_run(session)
        tasks_api = _fake_tasks_api()
        tasks_api.post = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )

        response = api_client(regular_user, session, tasks_api).post(
            f"{BASE}/runs/{run.id}:cancel"
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_saves_cancel_requested_before_stopping_steps(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Persist the flag even if stopping the running step blows up entirely.

        Guards the ordering, not just the tolerance: a caller reading the run
        straight from the database — not through this response — must see
        ``cancel_requested=True`` even when ``_stop_running_steps`` itself
        raises something ``cancel_run`` does not catch.
        """
        run = await self._seed_run(session)
        tasks_api = _fake_tasks_api()
        tasks_api.post = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await cancel_run(run, session, tasks_api)

        await session.refresh(run)
        assert run.cancel_requested is True

    @pytest.mark.asyncio
    async def test_is_idempotent_once_already_requested(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Treat a second Abort as a no-op, not an error."""
        run = await self._seed_run(session, cancel_requested=True)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}:cancel"
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["cancel_requested"] is True

    @pytest.mark.asyncio
    async def test_409s_for_an_already_terminal_run(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 409 for a finished run, which has nothing left to cancel."""
        run = await self._seed_run(session, BootstrapRunStatus.ROLLED_BACK)

        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{run.id}:cancel"
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_404s_for_an_unknown_run(
        self, regular_user: CasdoorUser, session: AsyncSession
    ) -> None:
        """Return 404, not 500, for a run id nobody created."""
        response = api_client(regular_user, session, _fake_tasks_api()).post(
            f"{BASE}/runs/{uuid4()}:cancel"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
