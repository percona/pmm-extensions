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

"""Define tests for the top-level app.main module."""

import logging.config
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from http.client import HTTPConnection
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from app import main as main_module
from app.core.health import HEALTH_PATH
from app.main import app
from tests.app.conftest import HealthProbeServer


@pytest.fixture
def test_client():
    """Create a test client for the top-level combined app."""
    return TestClient(app)


@pytest.mark.asyncio
async def test_extensions_startup_runs_after_the_override_snapshot_publishes(
    mocker: MockerFixture,
) -> None:
    """Run ``extensions_startup()`` only after the override snapshot has published.

    ``extensions_overrides_lifespan`` publishes the initial override snapshot on
    entry; a hot app-owned field (e.g. ``OmInventorySettings.ENABLED``) reads
    its class default until that publish happens, so seeding the periodic-task
    database before entry can seed a sweep as off when a prior run had already
    turned it on. ``app.extensions.main.extensions_lifespan`` gets this right for the
    standalone entry point; this locks the combined ``app.main:app`` entry
    point to the same order.
    """
    order: list[str] = []

    @asynccontextmanager
    async def _fake_extensions_overrides_lifespan(_app):
        order.append("extensions_overrides_enter")
        yield
        order.append("extensions_overrides_exit")

    @asynccontextmanager
    async def _fake_passthrough_lifespan(_app):
        yield

    async def _fake_extensions_startup():
        order.append("extensions_startup")

    mocker.patch.object(main_module, "detect_removed_auth_user_model")
    mocker.patch.object(main_module, "detect_removed_settings_override_keys")
    mocker.patch.object(main_module, "validate_importable_settings")
    mocker.patch.object(
        main_module,
        "extensions_overrides_lifespan",
        _fake_extensions_overrides_lifespan,
    )
    mocker.patch.object(main_module, "tasks_lifespan", _fake_passthrough_lifespan)
    mocker.patch.object(
        main_module, "inventory_overrides_lifespan", _fake_passthrough_lifespan
    )
    mocker.patch.object(main_module, "extensions_startup", _fake_extensions_startup)

    async with main_module.main_lifespan(app):
        pass

    assert order == [
        "extensions_overrides_enter",
        "extensions_startup",
        "extensions_overrides_exit",
    ]


def test_extensions_openapi_json_endpoint_returns_valid_schema(test_client):
    """``GET /api/extensions/openapi.json`` returns the PMM Extensions sub-app's OpenAPI document.

    The endpoint is a schema-helper route — it is intentionally hidden from the core
    ``/openapi.json`` via ``include_in_schema=False`` but remains callable so the
    frontend codegen can pull each mounted app's spec independently.
    """
    response = test_client.get("/api/extensions/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert {"openapi", "info", "paths"} <= body.keys()
    assert body["info"].get("title")


def test_extensions_mounted_at_the_root_is_unaffected_by_the_prefix_parameter(
    test_client,
):
    """Serve the mounted PMM Extensions app from ``/`` while no URL prefix is configured.

    ``FastAPI.__call__`` overwrites the scope ``root_path`` a ``Mount`` sets, so
    only an unset prefix keeps the composite app's URLs anchored where it mounts
    PMM Extensions. The side-car runs ``app.extensions.main`` directly and is the only deployment
    that configures one.
    """
    assert test_client.get("/health").status_code == status.HTTP_200_OK


def test_extensions_openapi_helper_is_hidden_from_core_spec(test_client):
    """The schema-helper route must not appear in the core ``/openapi.json``."""
    core_spec = test_client.get("/openapi.json").json()

    assert core_spec["paths"]
    assert "/api/extensions/openapi.json" not in core_spec["paths"]


def test_api_openapi_json_merges_core_and_extensions(test_client):
    """``GET /api/openapi.json`` returns a merged spec containing core + extensions paths."""
    response = test_client.get("/api/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert {"openapi", "info", "paths"} <= body.keys()
    paths = body["paths"]

    core_spec = test_client.get("/openapi.json").json()
    extensions_spec = test_client.get("/api/extensions/openapi.json").json()
    core_paths = set(core_spec.get("paths", {}))
    extensions_paths = set(extensions_spec.get("paths", {}))
    merged_paths = set(paths)

    assert core_paths, "core spec should expose at least one path"
    assert extensions_paths, "extensions spec should expose at least one path"
    assert core_paths & merged_paths, "merged spec missing core paths"
    assert extensions_paths & merged_paths, "merged spec missing extensions_app paths"


def test_api_openapi_json_describes_itself_as_the_merged_document(test_client):
    """Describe ``/api/openapi.json`` as the merged document, not the core-only one.

    The merge keeps ``info`` from the core spec, whose description tells the
    reader the PMM Extensions web app routes are not in the document. Swagger UI
    at ``/api/docs`` renders that text above the very routes it says are absent.
    """
    merged = test_client.get("/api/openapi.json").json()["info"]["description"]
    core = test_client.get("/openapi.json").json()["info"]["description"]

    assert merged != core
    assert "not merged into this document" not in merged
    assert "/api/extensions/openapi.json" in merged


def test_api_docs_serves_swagger_ui(test_client):
    """``GET /api/docs`` returns Swagger UI HTML wired to ``/api/openapi.json``."""
    response = test_client.get("/api/docs")

    assert response.status_code == status.HTTP_200_OK
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert "/api/openapi.json" in body
    assert "swagger-ui" in body.lower()


def test_top_level_docs_disabled(test_client):
    """The auto-generated ``/docs`` and ``/redoc`` pages are disabled."""
    assert test_client.get("/docs").status_code == status.HTTP_404_NOT_FOUND
    assert test_client.get("/redoc").status_code == status.HTTP_404_NOT_FOUND


def test_existing_core_openapi_json_unchanged(test_client):
    """``GET /openapi.json`` keeps its core-only shape."""
    response = test_client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    spec = response.json()
    assert {"openapi", "info", "paths"} <= spec.keys()
    paths = spec["paths"]
    assert paths
    assert "/api/openapi.json" not in paths
    assert "/api/docs" not in paths
    assert "/api/extensions/openapi.json" not in paths


def test_existing_extensions_openapi_json_unchanged(test_client):
    """Serve the extensions_app spec at ``GET /api/extensions/openapi.json``."""
    response = test_client.get("/api/extensions/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    spec = response.json()
    assert {"openapi", "info", "paths"} <= spec.keys()
    assert spec.get("paths"), "extensions_app spec should expose paths"


def test_api_openapi_json_is_cached(test_client, monkeypatch):
    """Repeated ``GET /api/openapi.json`` calls reuse the cached merged document."""
    # Warm the cache.
    test_client.get("/api/openapi.json")

    calls = {"n": 0}
    original = main_module.merge_openapi_documents

    def counting_merge(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "merge_openapi_documents", counting_merge)

    for _ in range(3):
        response = test_client.get("/api/openapi.json")
        assert response.status_code == status.HTTP_200_OK

    assert calls["n"] == 0, "merge_openapi_documents should be cached after first call"


class TestCeleryBeatReadinessGate:
    """Cover holding Celery beat back until the HTTP API answers ``/health``.

    Beat's schedule lives in the database, so a restart dispatches anything
    already overdue about a second in, before ``uvicorn.run`` has opened its
    listening socket. A periodic task that calls PMM Extensions' own API in that window
    fails on connect through no fault of its own.
    """

    @pytest.fixture(name="logging_config", autouse=True)
    def logging_config_fixture(self, mocker: MockerFixture) -> MagicMock:
        """Stub the child's ``dictConfig`` call for every test in this class.

        Letting it run would replace the root handlers mid-test and detach
        ``caplog``, so the assertions on the gate's own log lines would see an
        empty record list whether or not it logged.
        """
        return mocker.patch.object(logging.config, "dictConfig")

    def test_beat_is_not_constructed_before_the_api_is_ready(
        self, mocker: MockerFixture
    ) -> None:
        """Run the readiness wait before beat exists at all.

        Ordering is the whole fix: nothing that can dispatch may be created until
        the gate has opened.
        """
        journal: list[str] = []

        def gate(*_args: object, **_kwargs: object) -> bool:
            journal.append("wait")
            return True

        def build_beat(**_kwargs: object) -> MagicMock:
            journal.append("beat")
            return mocker.MagicMock()

        mocker.patch.object(main_module, "wait_for_api_ready", side_effect=gate)
        beat_cls = mocker.patch.object(
            main_module.celery_app, "Beat", side_effect=build_beat
        )

        main_module.start_celery_beat()

        assert journal == ["wait", "beat"]
        assert beat_cls.call_args.kwargs["scheduler"] == "sqlalchemy"

    def test_gate_targets_the_configured_listener(self, mocker: MockerFixture) -> None:
        """Pass the gate the host, port and allow-list the API is actually started with.

        The readiness budget is read from settings too: a deployment whose startup
        runs long has no other way to raise it, and exhausting it starts beat
        ungated, which is the reported failure back again.
        """
        wait = mocker.patch.object(main_module, "wait_for_api_ready", return_value=True)
        mocker.patch.object(main_module.celery_app, "Beat")
        mocker.patch.object(main_module.extensions_settings, "UVICORN_HOST", "0.0.0.0")
        mocker.patch.object(main_module.extensions_settings, "UVICORN_PORT", 8123)
        mocker.patch.object(
            main_module.extensions_settings, "ALLOWED_HOSTS", ["extensions.example.com"]
        )
        configured_timeout = 90.0
        configured_interval = 0.25
        mocker.patch.object(
            main_module.extensions_settings, "API_READINESS_TIMEOUT", configured_timeout
        )
        mocker.patch.object(
            main_module.extensions_settings,
            "API_READINESS_POLL_INTERVAL",
            configured_interval,
        )

        main_module.start_celery_beat()

        args, kwargs = wait.call_args
        assert args == ("0.0.0.0", 8123)
        assert kwargs["allowed_hosts"] == ["extensions.example.com"]
        assert kwargs["timeout"] == configured_timeout
        assert kwargs["interval"] == configured_interval

    def test_beat_still_runs_when_the_gate_times_out(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Start beat anyway on timeout, logging the degradation.

        Refusing to start beat would trade a first-run connection error for
        silently running no periodic task at all, which is the worse failure.
        """
        mocker.patch.object(main_module, "wait_for_api_ready", return_value=False)
        beat_cls = mocker.patch.object(main_module.celery_app, "Beat")

        with caplog.at_level("ERROR"):
            main_module.start_celery_beat()

        beat_cls.return_value.run.assert_called_once()
        errors = [record for record in caplog.records if record.levelname == "ERROR"]
        assert errors, "the degradation must be logged"
        assert "without a ready HTTP API" in errors[-1].getMessage()

    def test_beat_is_skipped_when_the_wait_is_interrupted(
        self, mocker: MockerFixture
    ) -> None:
        """Exit quietly without starting beat when the operator interrupts the wait.

        A ``Ctrl-C`` during the gate reaches this child process too; starting beat
        only to have the parent terminate it adds a traceback and nothing else.
        """
        mocker.patch.object(
            main_module, "wait_for_api_ready", side_effect=KeyboardInterrupt
        )
        beat_cls = mocker.patch.object(main_module.celery_app, "Beat")

        main_module.start_celery_beat()

        beat_cls.assert_not_called()

    def test_logging_is_configured_before_the_gate_runs(
        self, mocker: MockerFixture, logging_config: MagicMock
    ) -> None:
        """Configure logging first, so the gate's own log lines are not dropped.

        Under a ``spawn`` start method this process never runs the ``__main__``
        block that configures logging, so without this the wait would be silent
        and an operator would see beat idle for the whole deadline with no reason
        given.
        """
        journal: list[str] = []

        def configure_logging(config: dict[str, Any]) -> None:
            journal.append("logging")

        def gate(*_args: object, **_kwargs: object) -> bool:
            journal.append("wait")
            return True

        logging_config.side_effect = configure_logging
        mocker.patch.object(main_module, "wait_for_api_ready", side_effect=gate)
        mocker.patch.object(main_module.celery_app, "Beat")

        main_module.start_celery_beat()

        assert journal == ["logging", "wait"]
        logging_config.assert_called_once_with(main_module.settings.LOGGING_CONFIG)

    def test_worker_startup_is_not_gated(self, mocker: MockerFixture) -> None:
        """Leave the worker starting immediately, unaffected by beat's gate.

        The worker idles harmlessly until a task arrives, so delaying it would
        regress startup for no benefit.
        """
        wait = mocker.patch.object(main_module, "wait_for_api_ready")
        mocker.patch.object(main_module.celery_app, "Worker")

        main_module.start_celery_worker()

        wait.assert_not_called()


class TestCeleryBeatReadinessGateOverARealSocket:
    """Pin the startup ordering the fix turns on against a real listener.

    :cvar GATE_TIMEOUT: A test-sized stand-in for the production readiness
        budget.
    :cvar GATE_INTERVAL: The poll interval the shortened gate runs on.
    :cvar SERVER_START_DELAY: Long enough that the port is still refusing on
        the gate's first attempt.
    """

    GATE_TIMEOUT = 15.0
    GATE_INTERVAL = 0.05
    SERVER_START_DELAY = 0.3

    @pytest.fixture(name="stub_logging_config", autouse=True)
    def stub_logging_config_fixture(self, mocker: MockerFixture) -> MagicMock:
        """Keep the child's ``dictConfig`` call from replacing the root handlers."""
        return mocker.patch.object(logging.config, "dictConfig")

    @pytest.fixture(name="gate_targets_the_probe_server", autouse=True)
    def gate_targets_the_probe_server_fixture(
        self, mocker: MockerFixture, health_probe_server: HealthProbeServer
    ) -> None:
        """Point the real gate at the probe server on a shortened deadline.

        The gate is left unpatched because it is the thing under test; shortening
        it through ``ExtensionsSettings`` instead of a wrapper means these tests also
        cover the wiring ``start_celery_beat`` reads its budget from.
        """
        mocker.patch.object(
            main_module.extensions_settings, "UVICORN_HOST", "127.0.0.1"
        )
        mocker.patch.object(
            main_module.extensions_settings, "UVICORN_PORT", health_probe_server.port
        )
        mocker.patch.object(main_module.extensions_settings, "ALLOWED_HOSTS", ["*"])
        mocker.patch.object(
            main_module.extensions_settings, "API_READINESS_TIMEOUT", self.GATE_TIMEOUT
        )
        mocker.patch.object(
            main_module.extensions_settings,
            "API_READINESS_POLL_INTERVAL",
            self.GATE_INTERVAL,
        )

    @contextmanager
    def _starts_listening_mid_wait(
        self, health_probe_server: HealthProbeServer
    ) -> Iterator[None]:
        """Start the probe server part-way through the block's wait."""
        delayed_start = threading.Timer(
            self.SERVER_START_DELAY, health_probe_server.start
        )
        delayed_start.start()
        try:
            yield
        finally:
            delayed_start.cancel()

    def test_beat_is_constructed_only_after_the_api_starts_listening(
        self, mocker: MockerFixture, health_probe_server: HealthProbeServer
    ) -> None:
        """Construct beat only once a real server is answering ``/health``.

        The full ordering, end to end: the port refuses connections, a server
        starts listening part-way through the wait, and only then does anything
        capable of dispatching exist.
        """
        listening_when_beat_built: list[bool] = []

        def build_beat(**_kwargs: object) -> MagicMock:
            listening_when_beat_built.append(health_probe_server.listening.is_set())
            return mocker.MagicMock()

        mocker.patch.object(main_module.celery_app, "Beat", side_effect=build_beat)

        with self._starts_listening_mid_wait(health_probe_server):
            main_module.start_celery_beat()

        assert listening_when_beat_built == [True], (
            "beat was constructed before the API started listening"
        )

    def test_the_first_dispatch_reaches_an_api_that_refused_before_the_gate(
        self, mocker: MockerFixture, health_probe_server: HealthProbeServer
    ) -> None:
        """Walk the whole reported sequence, asserting the symptom is gone.

        The worker starts while the port still refuses, no dispatch happens until
        a real server answers, and then the first dispatched task's call to PMM Extensions'
        own API succeeds. The same call is made up front so the failure being
        closed is demonstrated rather than assumed: an overdue task dispatched in
        the old window got ``ConnectionRefusedError``, not a response.
        """

        def call_internal_api() -> int:
            connection = HTTPConnection(
                "127.0.0.1", health_probe_server.port, timeout=2.0
            )
            try:
                connection.request("GET", HEALTH_PATH)
                return connection.getresponse().status
            finally:
                connection.close()

        with pytest.raises(ConnectionRefusedError):
            call_internal_api()

        worker_cls = mocker.patch.object(main_module.celery_app, "Worker")
        main_module.start_celery_worker()
        worker_cls.return_value.start.assert_called_once()
        with pytest.raises(ConnectionRefusedError):
            call_internal_api()

        dispatched: list[int] = []

        def build_beat(**_kwargs: object) -> MagicMock:
            beat = mocker.MagicMock()
            beat.run.side_effect = lambda: dispatched.append(call_internal_api())
            return beat

        mocker.patch.object(main_module.celery_app, "Beat", side_effect=build_beat)

        with self._starts_listening_mid_wait(health_probe_server):
            main_module.start_celery_beat()

        assert dispatched == [status.HTTP_200_OK]
        # The server records only what it accepted, so the gate's own probe is
        # the first entry and the dispatch went out behind it.
        assert [path for path, _headers in health_probe_server.requests] == [
            HEALTH_PATH,
            HEALTH_PATH,
        ]
