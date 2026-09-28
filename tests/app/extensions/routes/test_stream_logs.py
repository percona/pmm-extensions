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

"""Define tests for the app.extensions.routes.stream_logs module."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses, CallbackResult
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from app.core.config import settings
from app.core.requests import RemoteAPI
from app.core.settings_override.lifecycle import SnapshotChange
from app.core.settings_override.models import SettingClassEnum
from app.extensions.config import extensions_settings
from app.extensions.deps import (
    get_current_user,
    get_task_history,
    get_tasks_client,
)
from app.extensions.main import extensions_app, extensions_overrides_lifespan
from app.tasks.models import TaskHistoryStatusEnum
from tests.app.asgi_stream import asgi_stream
from tests.app.extensions.routes.conftest import (
    resolve_registry_tasks_client,
    TASKS_ENDPOINT,
)


async def mock_stream_logs_generator(log_lines):
    """Mock generator that yields log lines."""
    for log_line in log_lines:
        yield log_line


def mock_stream(path, task_history_id):
    """Mock the stream method of the RemoteAPI client."""
    if path == f"/history/{task_history_id}/logs/":
        return mock_stream_logs_generator(
            [
                b'{"msg": "log line 1"}',
                b'{"msg": "log line 2"}',
            ]
        )
    raise ValueError(f"Unexpected path: {path}")


@pytest.fixture
def mock_tasks_client(task_history_response):
    """Mock the TasksClient dependency returned by get_tasks_client."""
    client = AsyncMock(spec=RemoteAPI)

    @contextmanager
    def auth(token: str):
        yield client

    client.auth = auth
    client.stream.side_effect = lambda path, **_kwargs: mock_stream(
        path, task_history_response.id
    )
    client.post.return_value = task_history_response.model_dump()

    extensions_app.dependency_overrides[get_tasks_client] = lambda: client
    extensions_app.dependency_overrides[get_task_history] = (
        lambda: task_history_response
    )
    extensions_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        access_token="test-token"
    )

    yield client
    extensions_app.dependency_overrides = {}


def test_archives_logs_event_stream(
    mocker, test_client, mock_tasks_client, task_history_response
):
    """Test the /stream-logs/{task_history_id} endpoint for streaming logs."""
    response = test_client.get(f"/stream-logs/{task_history_response.id}")

    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

    streamed_content = response.content.decode("utf-8")
    assert "log line 1" in streamed_content
    assert "log line 2" in streamed_content
    assert "event: finish" in streamed_content
    assert TaskHistoryStatusEnum.SUCCESS.value in streamed_content

    mock_tasks_client.stream.assert_called_once_with(
        f"/history/{task_history_response.id}/logs/",
        params=mocker.ANY,
        timeout=mocker.ANY,
    )
    mock_tasks_client.post.assert_called_once_with(
        f"/history/{task_history_response.id}/sync/"
    )


def test_sync_hop_is_authenticated_as_the_service_principal(
    test_client, mock_tasks_client, task_history_response, mocker
):
    """Send the end-of-stream reconciliation under the internal token.

    The log stream is reachable by any authenticated user, and the ``sync`` hop
    it issues at stream end is a mutating request on the Tasks API — which is
    admin-gated. Carrying the viewing user's own bearer there ends a non-admin's
    stream in an error frame instead of the finish frame, so the hop is
    authenticated as the service principal the gate admits by identity.

    Evidence stops at the credential the request carries; whether the gate then
    admits that identity is asserted elsewhere, not here.
    """
    mocker.patch(
        "app.extensions.routes.stream_logs.get_internal_token",
        return_value="internal-token",
    )
    active_tokens: list[str] = []
    sync_token: dict[str, str] = {}

    @contextmanager
    def recording_auth(token: str):
        active_tokens.append(token)
        try:
            yield mock_tasks_client
        finally:
            active_tokens.pop()

    async def recording_post(_path, **_kwargs):
        sync_token["value"] = active_tokens[-1]
        return task_history_response.model_dump()

    mock_tasks_client.auth = recording_auth
    mock_tasks_client.post.side_effect = recording_post

    response = test_client.get(f"/stream-logs/{task_history_response.id}")

    assert response.status_code == HTTP_200_OK
    assert "event: finish" in response.text
    assert sync_token["value"] == "internal-token"


def test_logs_event_stream_emits_extensions_error_on_upstream_error(
    test_client, mock_tasks_client, task_history_response
):
    """Assert an upstream error during log streaming is surfaced as a extensions-error event."""
    mock_tasks_client.stream.side_effect = HTTPException(
        status_code=HTTP_503_SERVICE_UNAVAILABLE
    )

    response = test_client.get(f"/stream-logs/{task_history_response.id}")

    assert response.status_code == HTTP_200_OK
    streamed_content = response.content.decode("utf-8")
    assert "event: extensions-error" in streamed_content
    assert str(HTTP_503_SERVICE_UNAVAILABLE) in streamed_content


def test_logs_event_stream_finishes_on_empty_log_stream(
    test_client, mock_tasks_client, task_history_response
):
    """Assert an empty (early-ending) log stream still emits a finish event."""
    mock_tasks_client.stream.side_effect = lambda *_args, **_kwargs: (
        mock_stream_logs_generator([])
    )

    response = test_client.get(f"/stream-logs/{task_history_response.id}")

    assert response.status_code == HTTP_200_OK
    streamed_content = response.content.decode("utf-8")
    assert "log line" not in streamed_content
    assert "event: finish" in streamed_content
    assert TaskHistoryStatusEnum.SUCCESS.value in streamed_content


def test_logs_event_stream_forwards_tail_query_param(
    mocker, test_client, mock_tasks_client, task_history_response
):
    """Assert ``tail`` on the PMM Extensions SSE route is passed through to the Tasks API."""
    response = test_client.get(f"/stream-logs/{task_history_response.id}?tail=1000")

    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

    mock_tasks_client.stream.assert_called_once_with(
        f"/history/{task_history_response.id}/logs/",
        params=mocker.ANY,
        timeout=mocker.ANY,
    )
    assert mock_tasks_client.stream.call_args.kwargs["params"]["tail"] == "1000"


def test_stream_execution_events_event_stream(
    mocker, test_client, mock_tasks_client, task_history_response
):
    """Test /stream-logs/{task_history_id}/execution-events SSE endpoint."""
    mocker.patch(
        "app.extensions.routes.stream_logs.asyncio.sleep", new_callable=AsyncMock
    )

    events_payload = [
        {
            "timestamp": "2026-03-26T05:43:09.907295Z",
            "type": "Received",
            "description": "Task received by client (exit code 0)",
            "step": "prepare-env",
        },
        {
            "timestamp": "2026-03-26T05:43:22.730705Z",
            "type": "Started",
            "description": "Task started by client (exit code 0)",
            "step": "run-script",
        },
    ]

    status_calls = {"history": 0}

    async def mock_get(path, **_kwargs):
        if path == f"/history/{task_history_response.id}/events":
            return events_payload
        if path == f"/history/{task_history_response.id}":
            status_calls["history"] += 1
            if status_calls["history"] == 1:
                return {"status": TaskHistoryStatusEnum.RUNNING}
            return {"status": TaskHistoryStatusEnum.FAILED}
        raise ValueError(f"Unexpected path: {path}")

    mock_tasks_client.get.side_effect = mock_get

    response = test_client.get(
        f"/stream-logs/{task_history_response.id}/execution-events"
    )

    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

    streamed_content = response.content.decode("utf-8")
    assert "Received" in streamed_content
    assert "Started" in streamed_content
    assert "event: finish" in streamed_content
    assert f'"status": "{TaskHistoryStatusEnum.FAILED.value}"' in streamed_content


@pytest.mark.parametrize("root_path", ["", "/extensions"])
@pytest.mark.usefixtures("test_client", "mock_tasks_client")
def test_logs_stream_tells_a_proxy_not_to_buffer(task_history_response, root_path):
    """Assert the log stream reaches the browser incrementally through a proxy."""
    client = TestClient(
        extensions_app, root_path=root_path, raise_server_exceptions=False
    )

    response = client.get(f"{root_path}/stream-logs/{task_history_response.id}")

    assert response.status_code == HTTP_200_OK
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"


@pytest.mark.parametrize("root_path", ["", "/extensions"])
@pytest.mark.usefixtures("test_client")
def test_execution_events_stream_tells_a_proxy_not_to_buffer(
    mock_tasks_client, task_history_response, root_path
):
    """Assert the execution-events stream reaches the browser incrementally too."""

    async def mock_get(path, **_kwargs):
        if path == f"/history/{task_history_response.id}/events":
            return []
        return {"status": TaskHistoryStatusEnum.SUCCESS}

    mock_tasks_client.get.side_effect = mock_get
    client = TestClient(
        extensions_app, root_path=root_path, raise_server_exceptions=False
    )

    response = client.get(
        f"{root_path}/stream-logs/{task_history_response.id}/execution-events"
    )

    assert response.status_code == HTTP_200_OK
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: finish" in response.content.decode("utf-8")


def test_execution_events_stream_emits_extensions_error_on_upstream_error(
    test_client, mock_tasks_client, task_history_response
):
    """Assert an upstream error during event streaming is surfaced as a extensions-error event."""
    mock_tasks_client.get.side_effect = HTTPException(
        status_code=HTTP_503_SERVICE_UNAVAILABLE
    )

    response = test_client.get(
        f"/stream-logs/{task_history_response.id}/execution-events"
    )

    assert response.status_code == HTTP_200_OK
    streamed_content = response.content.decode("utf-8")
    assert "event: extensions-error" in streamed_content
    assert str(HTTP_503_SERVICE_UNAVAILABLE) in streamed_content


NEW_TASKS_ENDPOINT = "http://tasks-rebound.example.org"


async def _tasks_endpoint_rebinder():
    """Point ``TASKS_ENDPOINT`` at a new host and return the registered rebind callback.

    Reads the callback out of ``extensions_app.state.override_callbacks`` rather than
    building one, so the test exercises the wiring the settings-API handler
    fires and fails if the ``TASKS_ENDPOINT`` key stops being registered.

    :return: The callback ``app.extensions.main`` wires to a ``TASKS_ENDPOINT`` change.
    """
    extensions_settings._set_snapshot({"TASKS_ENDPOINT": NEW_TASKS_ENDPOINT})
    async with extensions_overrides_lifespan(extensions_app):
        callbacks = extensions_app.state.override_callbacks
    return callbacks[(SettingClassEnum.EXTENSIONS_SETTINGS, "TASKS_ENDPOINT")]


@pytest.mark.usefixtures("real_client_route_overrides")
class TestStreamsSurviveARebind:
    """Cover the SSE routes against a rebind fired while the response is open.

    These run without a ``get_tasks_client`` override, so the request-scoped
    hold the dependency takes is part of what is exercised, and are driven with
    :func:`tests.app.asgi_stream.asgi_stream` because both HTTP test clients
    buffer the whole response before returning it.
    """

    pytestmark = pytest.mark.asyncio

    async def test_log_stream_survives_an_app_state_rebind(self, task_history_response):
        """Deliver the post-stream ``finish`` event on the retired client.

        The ``finish`` event comes from a second call the generator makes *after*
        the log stream completed, so its arrival is what proves the accounting
        unit is the consumer's hold and not the individual HTTP call.
        """
        release = asyncio.Event()
        history_id = task_history_response.id
        old = await RemoteAPI(endpoint=TASKS_ENDPOINT).open()
        extensions_app.state.tasks_api = old
        rebind = await _tasks_endpoint_rebinder()

        async def held_logs(_url, **_kwargs):
            await release.wait()
            return CallbackResult(status=HTTP_200_OK, body=b'{"msg": "log line 1"}\n')

        try:
            with aioresponses() as upstream:
                upstream.get(
                    f"{TASKS_ENDPOINT}/history/{history_id}/logs/", callback=held_logs
                )
                upstream.post(
                    f"{TASKS_ENDPOINT}/history/{history_id}/sync/",
                    payload=task_history_response.model_dump(mode="json"),
                )

                async with asgi_stream(
                    extensions_app, f"/stream-logs/{history_id}"
                ) as response:
                    assert response.status_code == HTTP_200_OK

                    await rebind(SnapshotChange({}, {}))
                    assert extensions_app.state.tasks_api is not old
                    assert old._session is not None

                    release.set()
                    body = (await response.drain()).decode()
        finally:
            new = extensions_app.state.tasks_api
            del extensions_app.state.tasks_api
            await new.close()
            if (
                old._session is not None
            ):  # only on an early failure; the drain closes it
                await old.close()
            extensions_settings._set_snapshot({})

        assert "log line 1" in body
        assert "event: finish" in body
        assert old._session is None

    async def test_log_stream_survives_a_registry_eviction(self, task_history_response):
        """Cover the mounted shape, where the client comes from the registry."""
        release = asyncio.Event()
        history_id = task_history_response.id
        extensions_settings._set_snapshot({"TASKS_ENDPOINT": TASKS_ENDPOINT})
        client = await resolve_registry_tasks_client()

        async def held_logs(_url, **_kwargs):
            await release.wait()
            return CallbackResult(status=HTTP_200_OK, body=b'{"msg": "log line 1"}\n')

        try:
            with aioresponses() as upstream:
                upstream.get(
                    f"{TASKS_ENDPOINT}/history/{history_id}/logs/", callback=held_logs
                )
                upstream.post(
                    f"{TASKS_ENDPOINT}/history/{history_id}/sync/",
                    payload=task_history_response.model_dump(mode="json"),
                )

                async with asgi_stream(
                    extensions_app, f"/stream-logs/{history_id}"
                ) as response:
                    assert response.status_code == HTTP_200_OK

                    await settings.invalidate_client(TASKS_ENDPOINT)
                    assert client._session is not None

                    release.set()
                    body = (await response.drain()).decode()
        finally:
            await settings.invalidate_client(TASKS_ENDPOINT)
            extensions_settings._set_snapshot({})

        assert "event: finish" in body
        assert client._session is None

    async def test_execution_events_stream_survives_a_rebind_between_polls(
        self, task_history_response
    ):
        """Keep polling on the retired client after a rebind lands between polls.

        Between two polls the per-call hold is already released, so the
        request-scoped one is the only thing keeping the client alive here.
        """
        first_poll_done = asyncio.Event()
        history_id = task_history_response.id
        old = await RemoteAPI(endpoint=TASKS_ENDPOINT).open()
        extensions_app.state.tasks_api = old
        rebind = await _tasks_endpoint_rebinder()

        finished = task_history_response.model_dump(mode="json")
        running = finished | {"status": TaskHistoryStatusEnum.RUNNING.value}

        async def first_poll(_url, **_kwargs):
            first_poll_done.set()
            return CallbackResult(status=HTTP_200_OK, payload=running)

        try:
            with aioresponses() as upstream:
                upstream.get(
                    f"{TASKS_ENDPOINT}/history/{history_id}/events",
                    payload=[],
                    repeat=True,
                )
                upstream.get(
                    f"{TASKS_ENDPOINT}/history/{history_id}", callback=first_poll
                )
                upstream.get(f"{TASKS_ENDPOINT}/history/{history_id}", payload=finished)

                async with asgi_stream(
                    extensions_app, f"/stream-logs/{history_id}/execution-events"
                ) as response:
                    await asyncio.wait_for(first_poll_done.wait(), timeout=10)

                    await rebind(SnapshotChange({}, {}))
                    assert old._session is not None

                    body = (await response.drain()).decode()
        finally:
            new = extensions_app.state.tasks_api
            del extensions_app.state.tasks_api
            await new.close()
            if (
                old._session is not None
            ):  # only on an early failure; the drain closes it
                await old.close()
            extensions_settings._set_snapshot({})

        assert "event: finish" in body
        assert TaskHistoryStatusEnum.SUCCESS.value in body
        assert old._session is None


@pytest.mark.usefixtures("real_client_route_overrides")
class TestRequestsArrivingAfterARebind:
    """Cover the persistent post-rebind 500 the ticket left out of scope.

    The report saw ``/history/{id}/files/`` answering 500 with ``Session is
    closed`` for the rest of the process's life *after* a rebind. Neither close
    site can produce that, since each stops handing the outgoing client out
    before closing it, so these pin that property rather than the in-flight one.
    """

    pytestmark = pytest.mark.asyncio

    async def test_app_state_shape_serves_the_new_client(
        self, task_history_response
    ) -> None:
        """Answer a post-rebind request from the rebuilt ``app.state`` client."""
        history_id = task_history_response.id
        old = await RemoteAPI(endpoint=TASKS_ENDPOINT).open()
        extensions_app.state.tasks_api = old
        rebind = await _tasks_endpoint_rebinder()

        try:
            await rebind(SnapshotChange({}, {}))
            assert old._session is None  # nothing held it, so it closed at once

            with aioresponses() as upstream:
                upstream.get(
                    f"{NEW_TASKS_ENDPOINT}/history/{history_id}/files/",
                    payload={},
                )
                async with asgi_stream(
                    extensions_app, f"/files/{history_id}"
                ) as response:
                    await response.drain()
        finally:
            new = extensions_app.state.tasks_api
            del extensions_app.state.tasks_api
            await new.close()
            extensions_settings._set_snapshot({})

        assert response.status_code == HTTP_200_OK

    async def test_registry_shape_rebuilds_the_evicted_client(
        self, task_history_response
    ) -> None:
        """Answer a post-eviction request from a freshly built registry client."""
        history_id = task_history_response.id
        extensions_settings._set_snapshot({"TASKS_ENDPOINT": TASKS_ENDPOINT})
        stale = await resolve_registry_tasks_client()

        try:
            await settings.invalidate_client(TASKS_ENDPOINT)
            assert stale._session is None

            with aioresponses() as upstream:
                upstream.get(
                    f"{TASKS_ENDPOINT}/history/{history_id}/files/", payload={}
                )
                async with asgi_stream(
                    extensions_app, f"/files/{history_id}"
                ) as response:
                    await response.drain()

            assert await resolve_registry_tasks_client() is not stale
        finally:
            await settings.invalidate_client(TASKS_ENDPOINT)
            extensions_settings._set_snapshot({})

        assert response.status_code == HTTP_200_OK
