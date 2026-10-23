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

"""Define tests for the delivery-executor factory and endpoint splitting."""

import re
from collections.abc import Awaitable, Callable
from typing import Any, get_args, TYPE_CHECKING

import pytest
from aioresponses import aioresponses
from fastapi import status
from pytest_mock import MockerFixture

from app.core.exceptions import HTTPConflictException
from app.core.requests import RemoteAPI
from app.core.requests.registry import ClientRegistry
from app.extensions.bundle_upload.factory import (
    _rebased_plan,
    get_delivery_executor,
    split_endpoint,
)
from app.extensions.bundle_upload.plan import (
    DeliveryPlan,
    DeliveryPlanError,
    DeliveryPlanExecutor,
    RequestStep,
    StepRecord,
)
from app.extensions.bundle_upload.seam import BundleSource
from tests.app.extensions.bundle_upload.step_paths import SAME_ORIGIN_PATHS

if TYPE_CHECKING:
    from aiohttp import ClientSession

_ORIGIN = "https://intake.example.com"
_TICKET_URL = f"{_ORIGIN}/ticket_details?number=CS0001"
_UPLOAD_URL = f"{_ORIGIN}/attachment/upload"
#: The one resolution step plus the terminal upload of the reuse test's plan.
_EXPECTED_REQUEST_COUNT = 2


def _plan(endpoint: str, *, max_bundle_size_mb: int = 30) -> DeliveryPlan:
    """Build a one-resolution-step plan pointed at ``endpoint``.

    :param endpoint: The configured receiver endpoint.
    :param max_bundle_size_mb: The size cap the executor enforces before it
        issues any request.
    :return: The validated plan.
    """
    payload = {
        "endpoint": endpoint,
        "max_bundle_size_mb": max_bundle_size_mb,
        "resolution_steps": [
            {
                "name": "lookup",
                "method": "GET",
                "path": "ticket_details",
                "query": {"number": {"source": "input", "field": "case_ref"}},
                "outputs": {"sys_id": "/result/sys_id"},
            }
        ],
        "upload": {"path": "attachment/upload"},
    }
    return DeliveryPlan(**payload)


def _probe_plan(endpoint: str) -> DeliveryPlan:
    """Build a plan whose probe carries a path and a query pair of its own.

    :param endpoint: The configured receiver endpoint.
    :return: The validated plan.
    """
    return DeliveryPlan(
        endpoint=endpoint,
        probe={
            "path": "api/now/table/x",
            "query": {"sysparm_limit": {"source": "literal", "value": "1"}},
        },
        upload={"path": "attachment/upload"},
    )


def _case_search_plan(endpoint: str) -> DeliveryPlan:
    """Build a plan whose case search carries a path and a query pair of its own.

    :param endpoint: The configured receiver endpoint.
    :return: The validated plan.
    """
    return DeliveryPlan(
        endpoint=endpoint,
        case_search={
            "path": "api/now/table/x",
            "query": {"sysparm_limit": {"source": "literal", "value": "10"}},
            "term_pattern": r"[A-Za-z0-9 ._-]+",
            "results_pointer": "/result",
            "reference_pointer": "/number",
            "title_pointer": "/short_description",
        },
        upload={"path": "attachment/upload"},
    )


def _connection_details_plan(endpoint: str) -> DeliveryPlan:
    """Build a plan whose connection-details read carries a path and a query pair.

    :param endpoint: The configured receiver endpoint.
    :return: The validated plan.
    """
    return DeliveryPlan(
        endpoint=endpoint,
        connection_details={
            "path": "api/now/table/x",
            "query": {"sysparm_limit": {"source": "literal", "value": "1"}},
            "details": {"Key active": "/result/active"},
        },
        upload={"path": "attachment/upload"},
    )


def _maximal_plan() -> dict[str, Any]:
    """Return a plan payload declaring every optional step kind at once.

    :return: The plan payload to validate.
    """
    return {
        "endpoint": _ORIGIN,
        "resolution_steps": [
            {"name": "lookup", "method": "GET", "path": "ticket_details"}
        ],
        "probe": {"path": "health"},
        "case_search": {
            "path": "case",
            "term_pattern": r"[A-Za-z0-9 ._-]+",
            "results_pointer": "/result",
            "reference_pointer": "/number",
            "title_pointer": "/short_description",
        },
        "connection_details": {"path": "api_key"},
        "upload": {"path": "attachment/upload"},
    }


def _request_step_field_names() -> set[str]:
    """Return every ``DeliveryPlan`` field whose declared type is a request step.

    Read off the annotations rather than off an instance, so a step kind the
    maximal plan above forgets to declare is still named here.

    :return: The field names carrying one or more request steps.
    """
    return {
        name
        for name, field in DeliveryPlan.model_fields.items()
        if any(
            isinstance(annotation, type) and issubclass(annotation, RequestStep)
            for annotation in (field.annotation, *get_args(field.annotation))
        )
    }


def _bundle(size: int | None = None) -> BundleSource:
    """Build a small in-memory bundle, optionally overstating its size.

    :param size: The size to report, for a bundle the plan's cap must reject;
        the real byte count by default.
    :return: The bundle source handed to ``upload_bundle``.
    """
    content = b"bundle-bytes"
    return BundleSource(
        filename="bundle.tar.gz",
        content=content,
        size=len(content) if size is None else size,
    )


async def _send(
    plan: DeliveryPlan,
    *,
    case_ref: str | None = "CS0001",
    size: int | None = None,
) -> None:
    """Run one send through the factory.

    :param plan: The plan to deliver with.
    :param case_ref: The case reference, ``None`` to withhold the send input the
        plan cites.
    :param size: The bundle size to report, for a send the plan's cap rejects.
    """
    async with get_delivery_executor(plan) as executor:
        await executor.upload_bundle(
            source_ref="atw-incident/1",
            bundle=_bundle(size),
            case_ref=case_ref,
            manifest={},
        )


class TestSplitEndpoint:
    """Cover splitting a configured endpoint into origin, path, and query."""

    def test_origin_only_endpoint_yields_a_root_path(self) -> None:
        """Return the bare origin with a root path and no query pairs."""
        assert split_endpoint("https://intake.example.com") == (
            "https://intake.example.com",
            "/",
            {},
        )

    def test_path_prefix_is_split_off_the_origin(self) -> None:
        """Separate a path prefix from the origin the transport is built on."""
        assert split_endpoint("https://intake.example.com/api/now") == (
            "https://intake.example.com",
            "/api/now",
            {},
        )

    def test_query_pairs_are_returned_separately(self) -> None:
        """Return the endpoint's query pairs for the plan to carry."""
        origin, path, query = split_endpoint(
            "https://intake.example.com/api?sysparm_view=full"
        )

        assert (origin, path) == ("https://intake.example.com", "/api")
        assert query == {"sysparm_view": "full"}

    def test_repeated_query_keys_collapse_to_the_last(self) -> None:
        """Keep the last occurrence when a key repeats in the query string."""
        _origin, _path, query = split_endpoint("https://x.example.com/a?k=1&k=2")

        assert query == {"k": "2"}


class TestGetDeliveryExecutor:
    """Cover building an executor from a configured plan."""

    @pytest.mark.asyncio
    async def test_origin_only_endpoint_leaves_step_paths_untouched(self) -> None:
        """Leave paths alone when the endpoint carries no prefix or query."""
        plan = _plan("https://intake.example.com")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.resolution_steps[0].path == "ticket_details"
            assert executor._plan.upload.path == "attachment/upload"

    @pytest.mark.asyncio
    async def test_path_prefix_is_rebased_into_every_step(self) -> None:
        """Prefix each resolution step and the upload with the endpoint's path."""
        plan = _plan("https://intake.example.com/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.resolution_steps[0].path == "/api/now/ticket_details"
            assert executor._plan.upload.path == "/api/now/attachment/upload"

    @pytest.mark.asyncio
    async def test_endpoint_query_is_merged_into_every_step(self) -> None:
        """Carry the endpoint's query pairs onto each step without dropping its own."""
        plan = _plan("https://intake.example.com/api?sysparm_view=full")

        async with get_delivery_executor(plan) as executor:
            step = executor._plan.resolution_steps[0]
            assert set(step.query) == {"number", "sysparm_view"}
            assert set(executor._plan.upload.query) == {"sysparm_view"}

    @pytest.mark.asyncio
    async def test_origin_only_endpoint_leaves_the_probe_path_untouched(self) -> None:
        """Leave the probe path alone when the endpoint carries no prefix."""
        async with get_delivery_executor(_probe_plan(_ORIGIN)) as executor:
            assert executor._plan.probe.path == "api/now/table/x"

    @pytest.mark.asyncio
    async def test_path_prefix_is_rebased_into_the_probe(self) -> None:
        """Prefix the probe with the endpoint's path, as every other step is.

        A root endpoint hides a missing rebase entirely, because the unrebased
        path is already correct there. Only a prefixed endpoint can tell the
        two apart.
        """
        plan = _probe_plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.probe.path == "/api/now/api/now/table/x"

    @pytest.mark.asyncio
    async def test_endpoint_query_is_merged_into_the_probe(self) -> None:
        """Carry the endpoint's query pairs onto the probe without dropping its own."""
        plan = _probe_plan(f"{_ORIGIN}/api?sysparm_view=full")

        async with get_delivery_executor(plan) as executor:
            assert set(executor._plan.probe.query) == {"sysparm_limit", "sysparm_view"}

    @pytest.mark.asyncio
    async def test_a_plan_without_a_probe_rebases_without_one(self) -> None:
        """Leave the probe unset when rebasing a plan that declares none."""
        plan = _plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.probe is None

    @pytest.mark.asyncio
    async def test_origin_only_endpoint_leaves_the_case_search_path_untouched(
        self,
    ) -> None:
        """Leave the case-search path alone when the endpoint carries no prefix."""
        async with get_delivery_executor(_case_search_plan(_ORIGIN)) as executor:
            assert executor._plan.case_search.path == "api/now/table/x"

    @pytest.mark.asyncio
    async def test_path_prefix_is_rebased_into_the_case_search(self) -> None:
        """Prefix the case search with the endpoint's path, as every other step is.

        A root endpoint hides a missing rebase entirely, because the unrebased
        path is already correct there. Only a prefixed endpoint can tell the
        two apart.
        """
        plan = _case_search_plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.case_search.path == "/api/now/api/now/table/x"

    @pytest.mark.asyncio
    async def test_endpoint_query_is_merged_into_the_case_search(self) -> None:
        """Carry the endpoint's query pairs onto the search without dropping its own."""
        plan = _case_search_plan(f"{_ORIGIN}/api?sysparm_view=full")

        async with get_delivery_executor(plan) as executor:
            assert set(executor._plan.case_search.query) == {
                "sysparm_limit",
                "sysparm_view",
            }

    @pytest.mark.asyncio
    async def test_a_plan_without_a_case_search_rebases_without_one(self) -> None:
        """Leave the case search unset when rebasing a plan that declares none."""
        plan = _plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.case_search is None

    @pytest.mark.asyncio
    async def test_origin_only_endpoint_leaves_the_details_path_untouched(self) -> None:
        """Leave the connection-details path alone when the endpoint has no prefix."""
        async with get_delivery_executor(_connection_details_plan(_ORIGIN)) as executor:
            assert executor._plan.connection_details.path == "api/now/table/x"

    @pytest.mark.asyncio
    async def test_path_prefix_is_rebased_into_the_connection_details(self) -> None:
        """Prefix the connection-details read with the endpoint's path.

        A root endpoint hides a missing rebase entirely, because the unrebased
        path is already correct there. Only a prefixed endpoint can tell the
        two apart.
        """
        plan = _connection_details_plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            details = executor._plan.connection_details
            assert details.path == "/api/now/api/now/table/x"

    @pytest.mark.asyncio
    async def test_endpoint_query_is_merged_into_the_connection_details(self) -> None:
        """Carry the endpoint's query pairs onto the read without dropping its own."""
        plan = _connection_details_plan(f"{_ORIGIN}/api?sysparm_view=full")

        async with get_delivery_executor(plan) as executor:
            assert set(executor._plan.connection_details.query) == {
                "sysparm_limit",
                "sysparm_view",
            }

    @pytest.mark.asyncio
    async def test_a_plan_without_connection_details_rebases_without_them(self) -> None:
        """Leave the read unset when rebasing a plan that declares none."""
        plan = _plan(f"{_ORIGIN}/api/now")

        async with get_delivery_executor(plan) as executor:
            assert executor._plan.connection_details is None

    def test_the_maximal_plan_declares_every_step_kind(self) -> None:
        """Fail at the fixture when a step kind is added the maximal plan omits.

        The rebase guard below reads its step kinds off an instance, so a kind
        the maximal plan never declares would leave that guard asserting over a
        smaller set and passing. This fails first, and names the missing field.
        """
        plan = DeliveryPlan(**_maximal_plan())

        for name in _request_step_field_names():
            assert getattr(plan, name), f"_maximal_plan() declares no {name!r}"

    def test_every_declared_step_kind_is_rebased(self) -> None:
        """Fail when a step kind is added to ``DeliveryPlan`` without being rebased.

        The field set is read off the annotations rather than from an
        ``isinstance`` sweep of the instance: that sweep is the predicate the
        rebasing loop itself dispatches on, so a guard built from it could only
        ratify the implementation, and a list-valued field would be invisible to
        both at once.
        """
        plan = DeliveryPlan(**_maximal_plan())
        declared = _request_step_field_names()

        rebased = _rebased_plan(plan, path="/prefix", query={})

        assert declared, "DeliveryPlan declares no request-step fields"
        for name in declared:
            value = getattr(rebased, name)
            steps = value if isinstance(value, list) else [value]
            assert steps, f"_maximal_plan() declares no {name!r}"
            for step in steps:
                assert step.path.startswith("/prefix/")

    @pytest.mark.asyncio
    async def test_the_configured_plan_is_left_unmodified(self) -> None:
        """Leave the caller's plan and its nested steps untouched by the rebase.

        The plan handed in is the process-global ``DIAGNOSTICS_DELIVERY``; a shallow
        copy would share nested step objects and corrupt it for every later send.
        """
        plan = _plan("https://intake.example.com/api/now")
        before = plan.model_dump(mode="json")

        async with get_delivery_executor(plan):
            pass

        assert plan.model_dump(mode="json") == before
        assert plan.resolution_steps[0].path == "ticket_details"

    @pytest.mark.asyncio
    async def test_the_step_observer_reaches_the_executor(self) -> None:
        """Pass the observer through so the caller records per-step progress."""

        def observer(_record: StepRecord) -> None:
            """Stand in for the send log's step recorder."""

        async with get_delivery_executor(
            _plan("https://intake.example.com"), step_observer=observer
        ) as executor:
            assert executor._step_observer is observer

    @pytest.mark.asyncio
    async def test_the_transport_carries_the_origin_alone(self) -> None:
        """Point the client at the origin, leaving path and query to the plan."""
        plan = _plan("https://intake.example.com/api/now?sysparm_view=full")

        async with get_delivery_executor(plan) as executor:
            assert str(executor._api.endpoint).rstrip("/") == _ORIGIN


class TestDeliveryTransportLifetime:
    """Cover the per-send scope of the transport the factory builds."""

    @pytest.mark.asyncio
    async def test_the_transport_is_not_pooled_in_the_client_registry(
        self, mocker: MockerFixture
    ) -> None:
        """Build the client directly, leaving the process-global registry alone.

        The receiver endpoint is operator-changeable at run time, so a client
        pooled on its origin is orphaned the moment the origin changes. ``get``
        is the registry's only insertion path, so never calling it is the whole
        claim: nothing was cached under either origin.
        """
        pooled = mocker.spy(ClientRegistry, "get")

        async with get_delivery_executor(_plan("https://old.example.com")):
            pass
        async with get_delivery_executor(_plan("https://new.example.com")):
            pass

        pooled.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_transport_is_open_inside_and_closed_after(self) -> None:
        """Serve requests for the duration of the send, then close the session."""
        async with get_delivery_executor(_plan(_ORIGIN)) as executor:
            api = executor._api
            assert api._session is not None
            assert not api._session.closed

        assert api._session is None

    @pytest.mark.asyncio
    async def test_the_transport_is_closed_when_the_receiver_rejects_the_upload(
        self, mocker: MockerFixture
    ) -> None:
        """Close the session when the terminal upload answers an error status."""
        opened = mocker.spy(RemoteAPI, "__aenter__")

        with aioresponses() as mock:
            mock.get(_TICKET_URL, payload={"result": {"sys_id": "SYS1"}})
            mock.post(_UPLOAD_URL, status=status.HTTP_409_CONFLICT, payload={})

            with pytest.raises(HTTPConflictException):
                await _send(_plan(_ORIGIN))

        assert opened.spy_return._session is None

    @pytest.mark.parametrize(
        ("plan", "match", "send_kwargs"),
        [
            pytest.param(
                _plan(_ORIGIN),
                "case_ref",
                {"case_ref": None},
                id="plan-cites-a-withheld-send-input",
            ),
            pytest.param(
                _plan(_ORIGIN, max_bundle_size_mb=1),
                "above the configured",
                {"size": 2 * 1024 * 1024},
                id="bundle-is-over-the-cap",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_transport_is_closed_when_the_send_fails_before_any_request(
        self,
        mocker: MockerFixture,
        plan: DeliveryPlan,
        match: str,
        send_kwargs: dict[str, Any],
    ) -> None:
        """Close the session for a send the executor rejects without a request.

        :param mocker: The mocker fixture.
        :param plan: The plan the send runs, built to fail.
        :param match: The fragment the raised error's message must carry.
        :param send_kwargs: The send arguments that trip the failure.
        """
        opened = mocker.spy(RemoteAPI, "__aenter__")

        with pytest.raises(DeliveryPlanError, match=match):
            await _send(plan, **send_kwargs)

        assert opened.spy_return._session is None


class TestIntraSendConnectionReuse:
    """Cover one transport serving every request of a single send."""

    @pytest.mark.asyncio
    async def test_one_session_serves_the_resolution_steps_and_the_upload(
        self, mocker: MockerFixture
    ) -> None:
        """Issue every request of one ``upload_bundle`` over the same session."""
        opened = mocker.spy(RemoteAPI, "__aenter__")
        sessions: list[ClientSession | None] = []

        with aioresponses() as mock:
            mock.get(_TICKET_URL, payload={"result": {"sys_id": "SYS1"}})
            mock.post(_UPLOAD_URL, payload={"result": {"sys_id": "att-9"}})

            async with get_delivery_executor(_plan(_ORIGIN)) as executor:
                sessions.append(executor._api._session)
                await executor.upload_bundle(
                    source_ref="atw-incident/1",
                    bundle=_bundle(),
                    case_ref="CS0001",
                    manifest={"bundle": "diag"},
                )
                sessions.append(executor._api._session)

        assert opened.call_count == 1
        assert sessions[0] is sessions[1]
        assert (
            sum(len(reqs) for reqs in mock.requests.values()) == _EXPECTED_REQUEST_COUNT
        )


#: The header every step kind of the destination tests carries, citing a secret,
#: so a request landing elsewhere would carry the credential with it.
_SECRET_HEADER = {"x-api-key": {"source": "secret", "name": "api_key"}}


def _destination_payload(field: str, value: Any) -> dict[str, Any]:
    """Return a plan payload setting ``field`` to ``value``, next to an upload.

    :param field: The ``DeliveryPlan`` field to set.
    :param value: The field's payload: one step, or a list of them.
    :return: The plan payload, its endpoint left for the caller to set.
    """
    return {
        "secrets": {"api_key": "real-api-key"},
        "upload": {"path": "attachment/upload", "headers": _SECRET_HEADER},
        field: value,
    }


async def _send_upload(executor: DeliveryPlanExecutor) -> None:
    """Run one send, issuing every resolution step and the upload.

    :param executor: The executor bound to the plan under test.
    """
    await executor.upload_bundle(
        source_ref="atw-incident/1",
        bundle=_bundle(),
        case_ref="CS0001",
        manifest={"bundle": "diag"},
    )


#: One entry per step kind: the plan payload builder, given the path under test,
#: and the executor call that issues that kind's request.
_STEP_KINDS = [
    pytest.param(
        lambda path: _destination_payload(
            "resolution_steps",
            [
                {
                    "name": "lookup",
                    "method": "GET",
                    "path": path,
                    "headers": _SECRET_HEADER,
                }
            ],
        ),
        _send_upload,
        id="resolution",
    ),
    pytest.param(
        lambda path: _destination_payload(
            "upload", {"path": path, "headers": _SECRET_HEADER}
        ),
        _send_upload,
        id="upload",
    ),
    pytest.param(
        lambda path: _destination_payload(
            "probe", {"path": path, "headers": _SECRET_HEADER}
        ),
        lambda executor: executor.probe(),
        id="probe",
    ),
    pytest.param(
        lambda path: _destination_payload(
            "case_search",
            {
                "path": path,
                "headers": _SECRET_HEADER,
                "term_pattern": r"[A-Za-z0-9 ._-]+",
                "results_pointer": "/rows",
                "reference_pointer": "/number",
                "title_pointer": "/short_description",
            },
        ),
        lambda executor: executor.search_cases("CS0001"),
        id="case_search",
    ),
    pytest.param(
        lambda path: _destination_payload(
            "connection_details",
            {
                "path": path,
                "headers": _SECRET_HEADER,
                "details": {"Key active": "/result/active"},
            },
        ),
        lambda executor: executor.read_connection_details(),
        id="connection_details",
    ),
]


class TestStepRequestDestination:
    """Cover where a step's request lands once its path is rebased and joined."""

    @pytest.mark.parametrize(
        "endpoint",
        [_ORIGIN, f"{_ORIGIN}/api/now"],
        ids=["root_endpoint", "prefixed_endpoint"],
    )
    @pytest.mark.parametrize("path", SAME_ORIGIN_PATHS)
    @pytest.mark.parametrize(("build", "send"), _STEP_KINDS)
    @pytest.mark.asyncio
    async def test_an_accepted_path_stays_on_the_endpoint_host(
        self,
        endpoint: str,
        path: str,
        build: Callable[[str], dict[str, Any]],
        send: Callable[[DeliveryPlanExecutor], Awaitable[Any]],
    ) -> None:
        """Send every credential-bearing request a valid plan issues to its host.

        The plan validator judges the path alone, while the request goes out
        only after the factory rebases it and the transport joins it, so this
        checks the rule against the URL actually requested, for every step kind.
        """
        plan = DeliveryPlan.model_validate({"endpoint": endpoint, **build(path)})

        with aioresponses() as mock:
            body = {"result": {"sys_id": "att-9", "active": True}, "rows": []}
            mock.get(re.compile(".*"), payload=body, repeat=True)
            mock.post(re.compile(".*"), payload=body, repeat=True)
            async with get_delivery_executor(plan) as executor:
                await send(executor)

        assert mock.requests
        assert {url.host for _, url in mock.requests} == {"intake.example.com"}
