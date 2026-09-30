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

"""Tests for resolving the ATW-owned PROXY task that carries ATW's recorder."""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.requests import RemoteAPI
from app.extensions.apps.atw.proxy_tasks import (
    _fetch_task,
    _sync_proxy_task,
    atw_proxy_task_name,
    ATW_PROXY_TASK_PREFIX,
    resolve_atw_proxy_tasks,
)
from app.extensions.apps.atw.recorder import RUN_RESULT_RECORDER
from app.tasks.execution.executors.nomad.steps import RUN_SCRIPT_OUTPUT_FILES_PATH
from app.tasks.models import ANY_OWNER, TaskBackendEnum, TaskWrite
from tests.app.extensions.path_unsafe_task_names import PATH_UNSAFE_TASKS

_ROOT_TASK_NAME = "exec-artifact"
_PROXY_NAME = f"{ATW_PROXY_TASK_PREFIX}{_ROOT_TASK_NAME}"
#: A non-default ``AnonymizeMask``; the type is an int bitmask, not a name list.
_CUSTOM_ANONYMIZE_MASK = 6
#: ``TaskBase.name``'s own ``max_length``, so a root at the limit cannot be prefixed.
_MAX_TASK_NAME_LENGTH = 255
#: Upstream reads one root resolution costs: the root itself, then its proxy.
_READS_PER_ROOT = 2


def _root_task(
    backend: TaskBackendEnum = TaskBackendEnum.NOMAD, **overrides: Any
) -> dict[str, Any]:
    """Build the upstream payload for the interpreter root ATW would wrap.

    Carries every behavioural field a real ``TaskResponse`` does, including the ones
    that default, because the proxy is validated against the root's values.
    """
    return {
        "name": _ROOT_TASK_NAME,
        "backend": backend.value,
        "owner": ANY_OWNER,
        "data": {"Constraints": [{"RTarget": "host1"}]},
        "run_result_recorder": None,
        "output_files_path": RUN_SCRIPT_OUTPUT_FILES_PATH,
        "anonymize_mask": None,
        "alert_on_fail": False,
        "alert_detail_builder": None,
    } | overrides


def _root_task_without(field: str) -> dict[str, Any]:
    """Build a root payload missing ``field``, as a drifted upstream contract would."""
    root = _root_task()
    del root[field]
    return root


def _valid_proxy(**overrides: Any) -> dict[str, Any]:
    """Build the upstream payload of a proxy that passes every validation check."""
    return {
        "name": _PROXY_NAME,
        "backend": TaskBackendEnum.PROXY.value,
        "owner": ANY_OWNER,
        "data": {"task": _ROOT_TASK_NAME},
        "run_result_recorder": RUN_RESULT_RECORDER,
        "output_files_path": RUN_SCRIPT_OUTPUT_FILES_PATH,
        "anonymize_mask": None,
        "alert_on_fail": False,
        "alert_detail_builder": None,
    } | overrides


async def _resolve(root_task_name: str = _ROOT_TASK_NAME) -> str | None:
    """Resolve one root through the batch-level entry point."""
    return (await resolve_atw_proxy_tasks([root_task_name]))[root_task_name]


@pytest.fixture
def tasks_api(mocker: MockerFixture) -> AsyncMock:
    """Replace the service-principal client this module builds with a mock.

    The module builds its own client so proxy creation runs as the service principal
    and works from a worker, so there is no dependency override to lean on here.
    """
    api = AsyncMock(spec=RemoteAPI)
    client = MagicMock()
    client.auth.return_value.__enter__.return_value = api
    mocker.patch(
        "app.extensions.apps.atw.proxy_tasks.get_tasks_api",
        new=AsyncMock(return_value=client),
    )
    mocker.patch(
        "app.extensions.apps.atw.proxy_tasks.get_internal_token",
        return_value="token",
    )
    return api


def _serve(tasks: dict[str, dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Build a ``get`` side effect answering from ``tasks``, else raising 404."""

    def _get(path: str, **_kwargs: Any) -> dict[str, Any]:
        name = path.lstrip("/")
        if name not in tasks:
            raise HTTPNotFoundException(f"no task {name}")
        return tasks[name]

    return _get


class TestProxyTaskName:
    """Check the naming that distinguishes ATW's proxy from the root it wraps."""

    def test_name_prefixes_the_root(self) -> None:
        """Ensure the proxy name is derived from the root, not configured separately."""
        assert atw_proxy_task_name(_ROOT_TASK_NAME) == _PROXY_NAME


class TestCreatesTheProxy:
    """Check the create and re-sync paths, and the exact payload each one sends."""

    @pytest.mark.asyncio
    async def test_creates_proxy_when_absent(self, tasks_api: AsyncMock) -> None:
        """Ensure a missing proxy is created and its name returned."""
        tasks_api.get.side_effect = _serve({_ROOT_TASK_NAME: _root_task()})
        tasks_api.post.return_value = _valid_proxy()

        assert await _resolve() == _PROXY_NAME
        assert tasks_api.post.await_count == 1

    @pytest.mark.asyncio
    async def test_posted_payload_carries_the_recorder_and_parity_fields(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure the created proxy carries every field the design depends on.

        ``owner`` and ``anonymize_mask`` keep anonymization byte-identical to today
        (the mask resolves off the proxy, not the root); ``output_files_path`` keeps
        result reading working; and the absence of a ``meta`` key is what stops the
        proxy overwriting each run's own execution meta.
        """
        tasks_api.get.side_effect = _serve({_ROOT_TASK_NAME: _root_task()})
        tasks_api.post.return_value = _valid_proxy()

        await _resolve()

        payload = tasks_api.post.await_args.kwargs["json"]
        assert payload["name"] == _PROXY_NAME
        assert payload["backend"] == TaskBackendEnum.PROXY
        assert payload["owner"] == ANY_OWNER
        assert payload["data"] == {"task": _ROOT_TASK_NAME}
        assert "meta" not in payload["data"]
        assert "payload" not in payload["data"]
        assert payload["run_result_recorder"] == RUN_RESULT_RECORDER
        assert payload["output_files_path"] == RUN_SCRIPT_OUTPUT_FILES_PATH
        assert payload["anonymize_mask"] is None

    @pytest.mark.asyncio
    async def test_custom_root_behaviour_is_copied_onto_the_proxy(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a non-default interpreter's behaviour survives being wrapped.

        ``SnippetInterpreterConfig.task`` is an overridable setting, so the root is
        not necessarily one of the seeded rows. Anonymization, result reading and
        dispatch-failure alerting all resolve off the *dispatched* task, so hardcoding
        the seeded defaults would silently apply the wrong policy to a custom one.
        """
        custom = _root_task(
            owner="pii-restricted",
            anonymize_mask=_CUSTOM_ANONYMIZE_MASK,
            output_files_path="custom/output",
            alert_on_fail=True,
            alert_detail_builder="app.extensions.apps.atw.recorder:record_atw_run",
        )
        tasks_api.get.side_effect = _serve({_ROOT_TASK_NAME: custom})
        tasks_api.post.return_value = _valid_proxy()

        await _resolve()

        payload = tasks_api.post.await_args.kwargs["json"]
        assert payload["owner"] == "pii-restricted"
        assert payload["anonymize_mask"] == _CUSTOM_ANONYMIZE_MASK
        assert payload["output_files_path"] == "custom/output"
        assert payload["alert_on_fail"] is True
        assert payload["alert_detail_builder"] == (
            "app.extensions.apps.atw.recorder:record_atw_run"
        )
        assert payload["run_result_recorder"] == RUN_RESULT_RECORDER

    @pytest.mark.asyncio
    async def test_a_proxy_whose_policy_drifted_is_resynced(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a proxy carrying superseded behaviour is rewritten, not reused.

        Editing a wrapped root's policy — or re-pointing an interpreter at a task with
        a different one — leaves the previous proxy in place under the same name.
        Reusing it would keep applying the superseded owner and mask, and refusing it
        would degrade that interpreter permanently, because nothing else repairs the
        row. So it is re-synced in place.
        """
        tightened = _root_task(owner="pii-restricted")
        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: tightened, _PROXY_NAME: _valid_proxy()}
        )
        tasks_api.put.return_value = _valid_proxy(owner="pii-restricted")

        assert await _resolve() == _PROXY_NAME
        assert tasks_api.put.await_args.args[0] == f"/{_PROXY_NAME}"
        assert tasks_api.put.await_args.kwargs["json"]["owner"] == "pii-restricted"
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_policy_edit_on_the_root_is_picked_up_immediately(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a tightened mask applies to the very next dispatch, not eventually.

        The root is read on every resolution precisely so there is no window in which
        a superseded PII policy keeps being applied.
        """
        served = {_ROOT_TASK_NAME: _root_task(), _PROXY_NAME: _valid_proxy()}
        tasks_api.get.side_effect = _serve(served)
        assert await _resolve() == _PROXY_NAME
        tasks_api.put.assert_not_awaited()

        served[_ROOT_TASK_NAME] = _root_task(anonymize_mask=_CUSTOM_ANONYMIZE_MASK)
        tasks_api.put.return_value = _valid_proxy(anonymize_mask=_CUSTOM_ANONYMIZE_MASK)

        assert await _resolve() == _PROXY_NAME
        assert (
            tasks_api.put.await_args.kwargs["json"]["anonymize_mask"]
            == _CUSTOM_ANONYMIZE_MASK
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"data": {"task": "some-other-root"}}, id="root"),
            pytest.param({"backend": TaskBackendEnum.NOMAD.value}, id="backend"),
            pytest.param({"owner": "someone-else"}, id="owner"),
            pytest.param({"output_files_path": None}, id="output_files_path"),
            pytest.param(
                {"anonymize_mask": _CUSTOM_ANONYMIZE_MASK}, id="anonymize_mask"
            ),
            pytest.param({"alert_on_fail": True}, id="alert_on_fail"),
            pytest.param(
                {"alert_detail_builder": "other.module:detail"},
                id="alert_detail_builder",
            ),
            pytest.param(
                {"data": {"task": _ROOT_TASK_NAME, "meta": {"target": "elsewhere"}}},
                id="meta",
            ),
            pytest.param(
                {"data": {"task": _ROOT_TASK_NAME, "payload": "file:///etc/passwd"}},
                id="payload",
            ),
        ],
    )
    async def test_every_field_the_design_depends_on_is_validated(
        self, tasks_api: AsyncMock, overrides: dict[str, Any]
    ) -> None:
        """Ensure a name collision is not taken as proof the right proxy exists.

        ``update_task`` can reshape any task after creation and each of these fields
        changes behaviour silently, so every one is checked. The re-sync is the
        observable consequence: a field that went unvalidated would leave the drifted
        proxy in place and send no ``PUT`` at all.
        """
        tasks_api.get.side_effect = _serve(
            {
                _ROOT_TASK_NAME: _root_task(),
                _PROXY_NAME: _valid_proxy(**overrides),
            }
        )
        tasks_api.put.return_value = _valid_proxy()

        assert await _resolve() == _PROXY_NAME
        assert tasks_api.put.await_count == 1

    @pytest.mark.asyncio
    async def test_existing_valid_proxy_is_reused_without_a_second_post(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a proxy that already exists and validates is reused as-is."""
        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: _root_task(), _PROXY_NAME: _valid_proxy()}
        )

        assert await _resolve() == _PROXY_NAME
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_batch_resolves_each_distinct_root_once(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure repeated roots in one batch cost one resolution, not one per item.

        This is what keeps reading the root on every resolution affordable: the traffic
        scales with the interpreters a request touches, not with its items.
        """
        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: _root_task(), _PROXY_NAME: _valid_proxy()}
        )

        resolved = await resolve_atw_proxy_tasks([_ROOT_TASK_NAME] * 20)

        assert resolved == {_ROOT_TASK_NAME: _PROXY_NAME}
        assert tasks_api.get.await_count == _READS_PER_ROOT


class TestRefusesToWrap:
    """Check every degradation, each of which dispatches under the root unchanged."""

    @pytest.mark.asyncio
    async def test_proxy_backed_root_is_not_wrapped(self, tasks_api: AsyncMock) -> None:
        """Ensure an already-PROXY interpreter is left alone rather than double-wrapped.

        ``get_root_task`` resolves exactly one hop, so a proxy-of-a-proxy reaches
        ``get_executor`` still PROXY and breaks every dispatch under it.
        """
        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: _root_task(backend=TaskBackendEnum.PROXY)}
        )

        assert await _resolve() is None
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_root_with_its_own_recorder_is_not_wrapped(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure an existing root recorder is not displaced by ATW's.

        ``maybe_record_run`` resolves exactly one recorder and does not chain, so
        wrapping would silently stop whatever the root's own recorder records.
        Declining costs ATW only the hook — the sweep still supplies its outcome.
        """
        tasks_api.get.side_effect = _serve(
            {
                _ROOT_TASK_NAME: _root_task(
                    run_result_recorder="app.extensions.apps.mysql_backups.recorder:record_backup_run"
                )
            }
        )

        assert await _resolve() is None
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_root_name_too_long_to_prefix_is_not_wrapped(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a root name that cannot take the prefix degrades instead of 500ing.

        ``Task.name`` permits 255 characters, so a root at the limit has no valid
        prefixed form; building the payload raises and the batch route would surface
        it as a 500 rather than dispatching.
        """
        long_name = "x" * _MAX_TASK_NAME_LENGTH
        tasks_api.get.side_effect = _serve({long_name: _root_task()})

        assert await _resolve(long_name) is None
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "occupant",
        [
            pytest.param(
                _valid_proxy(run_result_recorder="other.module:hook"),
                id="another-recorder",
            ),
            pytest.param(_valid_proxy(run_result_recorder=None), id="no-recorder"),
            pytest.param(
                _root_task(name=_PROXY_NAME, data={"job": "unrelated"}),
                id="unrelated-task",
            ),
        ],
    )
    async def test_a_task_atw_does_not_own_is_never_rewritten(
        self, tasks_api: AsyncMock, occupant: dict[str, Any]
    ) -> None:
        """Ensure a name collision with someone else's task is not repaired over it.

        The proxy prefix is not reserved and a ``PUT`` replaces a task wholesale, so
        only a row carrying ATW's own recorder is ATW's to rewrite. Anything else at
        that name is left untouched and the dispatch runs under the root.
        """
        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: _root_task(), _PROXY_NAME: occupant}
        )

        assert await _resolve() is None
        tasks_api.put.assert_not_awaited()
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "root",
        [
            pytest.param(_root_task_without("owner"), id="owner-missing"),
            pytest.param(_root_task(owner=""), id="owner-empty"),
            pytest.param(_root_task(alert_on_fail="false"), id="alert-on-fail-string"),
            pytest.param(
                _root_task_without("anonymize_mask"), id="anonymize-mask-missing"
            ),
            pytest.param(_root_task(anonymize_mask="6"), id="anonymize-mask-string"),
        ],
    )
    async def test_a_root_whose_policy_cannot_be_read_is_not_wrapped(
        self, tasks_api: AsyncMock, root: dict[str, Any]
    ) -> None:
        """Ensure a drifted root policy declines the wrap instead of defaulting.

        ``owner`` and ``anonymize_mask`` decide the run's PII treatment and
        ``alert_on_fail`` its alerting, so a missing or malformed value must not be
        replaced by a default that silently applies a different policy to the proxy.
        """
        tasks_api.get.side_effect = _serve({_ROOT_TASK_NAME: root})

        assert await _resolve() is None
        tasks_api.post.assert_not_awaited()
        tasks_api.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_absent_root_is_not_wrapped(self, tasks_api: AsyncMock) -> None:
        """Ensure a root that does not exist upstream is not given a proxy."""
        tasks_api.get.side_effect = _serve({})

        assert await _resolve() is None
        tasks_api.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unrepairable_row_does_not_persist_across_resolutions(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a degraded answer is not carried into the next dispatch.

        A proxy whose re-sync also fails to satisfy validation degrades that dispatch,
        but the next one re-examines the row from scratch rather than inheriting the
        verdict.
        """
        tasks_api.get.side_effect = _serve(
            {
                _ROOT_TASK_NAME: _root_task(),
                _PROXY_NAME: _valid_proxy(backend=TaskBackendEnum.NOMAD.value),
            }
        )
        tasks_api.put.return_value = _valid_proxy(backend=TaskBackendEnum.NOMAD.value)
        assert await _resolve() is None

        tasks_api.get.side_effect = _serve(
            {_ROOT_TASK_NAME: _root_task(), _PROXY_NAME: _valid_proxy()}
        )

        assert await _resolve() == _PROXY_NAME


class TestTransientFailuresSurface:
    """Check that an unreachable upstream is raised to the caller, not resolved here."""

    @pytest.mark.asyncio
    async def test_transient_upstream_error_propagates(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a 503 while resolving the proxy is raised rather than absorbed.

        Every *validation* outcome degrades to ``None`` per root; an unreachable
        upstream is not per-root, so it is raised and the caller decides. The batch
        route catches it and dispatches the whole batch unwrapped.
        """
        tasks_api.get.side_effect = HTTPServiceUnavailableException("try later")

        with pytest.raises(HTTPServiceUnavailableException):
            await _resolve()

    @pytest.mark.asyncio
    async def test_one_unreachable_root_fails_the_whole_batch(
        self, tasks_api: AsyncMock
    ) -> None:
        """Ensure a batch does not half-resolve when one root's read fails.

        The batch route degrades every item to an unwrapped dispatch when resolution
        raises, so raising keeps one request under one policy instead of splitting it
        by which reads happened to land.
        """
        tasks_api.get.side_effect = [
            _root_task(),
            _valid_proxy(),
            HTTPServiceUnavailableException("try later"),
        ]

        with pytest.raises(HTTPServiceUnavailableException):
            await resolve_atw_proxy_tasks([_ROOT_TASK_NAME, "exec-python-artifact"])


class TestCreateRace:
    """Check that a concurrent creator is resolved rather than treated as an error."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raised",
        [
            pytest.param(HTTPConflictException("exists"), id="409"),
            pytest.param(HTTPBadRequestException("integrity"), id="400"),
        ],
    )
    async def test_race_refetches_and_validates_the_winner(
        self, tasks_api: AsyncMock, raised: Exception
    ) -> None:
        """Ensure both race statuses re-fetch the winner and proceed.

        The duplicate precheck answers 409 only when it *sees* the row, so a
        concurrent insert trips the commit branch and surfaces as a 400 instead. A
        409-only handler would turn a live race into a failed dispatch.
        """
        served = {_ROOT_TASK_NAME: _root_task()}
        tasks_api.get.side_effect = _serve(served)

        def _post(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            served[_PROXY_NAME] = _valid_proxy()
            raise raised

        tasks_api.post.side_effect = _post

        assert await _resolve() == _PROXY_NAME

    @pytest.mark.asyncio
    async def test_race_winner_is_still_validated(self, tasks_api: AsyncMock) -> None:
        """Ensure the row a race produced is validated, not trusted for existing."""
        served = {_ROOT_TASK_NAME: _root_task()}
        tasks_api.get.side_effect = _serve(served)

        def _post(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            served[_PROXY_NAME] = _valid_proxy(backend=TaskBackendEnum.NOMAD.value)
            raise HTTPConflictException("exists")

        tasks_api.post.side_effect = _post

        assert await _resolve() is None


@pytest.mark.asyncio
class TestProxyTaskPathGuard:
    """Test that a task name cannot reshape an outbound proxy request."""

    @pytest.mark.parametrize("name", PATH_UNSAFE_TASKS)
    async def test_fetch_refuses_an_unsafe_name(self, name: str) -> None:
        """Refuse an unsafe name rather than GET a reshaped path."""
        tasks_api = AsyncMock(spec=RemoteAPI)

        with pytest.raises(HTTPUnprocessableEntityException):
            await _fetch_task(tasks_api, name)

        tasks_api.get.assert_not_awaited()

    @pytest.mark.parametrize("name", PATH_UNSAFE_TASKS)
    async def test_sync_refuses_an_unsafe_name(self, name: str) -> None:
        """Refuse an unsafe proxy name rather than PUT a reshaped path."""
        tasks_api = AsyncMock(spec=RemoteAPI)
        task_write = TaskWrite(
            name=name,
            owner="ATW",
            backend=TaskBackendEnum.PROXY,
            data={"task": "run-python"},
        )

        with pytest.raises(HTTPUnprocessableEntityException):
            await _sync_proxy_task(tasks_api, "root", task_write)

        tasks_api.put.assert_not_awaited()
