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

"""Cover the ``ScriptSource`` seam and its derived router surface.

Exercises the script flavor end-to-end through a fixture script directory and a
purpose-built :class:`ScriptProtocol` implementation: the ``ScriptSource`` hooks
(listing, form synthesis, execution-meta assembly), the full derived HTTP route
matrix via a real ``TestClient`` (mocking only the Tasks API, never the
script-resolution or body deps), the ``TaskExecutionApp`` validators, the scoped
conformance guards, and the authenticated static mount.

The fixture is a structural ``ScriptProtocol`` implementation rather than a
``BaseSnippet``: ``BaseSnippet.get_execution_model`` injects the snippet-specific
``-hostname-`` field, which the framework's generic ``args`` validation gate
cannot satisfy without snippet-specific knowledge — that coupling is deferred to
the Wave-3 snippets migration. The framework programs against ``ScriptProtocol``,
so the fixture exercises the same code path a real consumer hits.
"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, Depends, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, create_model

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.exceptions import HTTPBadRequestException, HTTPNotFoundException
from app.core.pagination import Pagination
from app.core.pagination.deps import make_pagination_dep
from app.core.requests.remote_api import RemoteAPI
from app.extensions.apps.framework import ScriptSource, TaskExecutionApp
from app.extensions.apps.framework.apps import NO_PAGINATION
from app.extensions.apps.framework.conformance import (
    check_capability_route_consistency,
    check_schema_derivation_succeeds,
    check_view_fields_reference_real_fields,
)
from app.extensions.apps.framework.form_dsl import AppFormModel, Ui
from app.extensions.apps.framework.schema import (
    AppSchema,
    Column,
    FormSection,
    ListView,
    StringField,
)
from app.extensions.apps.framework.script_source import (
    resolve_scripts,
    ScriptExecuteWrite,
)
from app.extensions.deps import IsApiAuthenticated
from tests.app.extensions.apps.framework.contract_suite import build_contract_client
from tests.app.extensions.apps.framework.contract_suite import routes_of as _routes

_OWNER = "ARCHIVER"
_PREFIX = "/fixture-scripts"
_BASE = f"/api/apps{_PREFIX}"
_TASK_NAME = "run-bash"
_PAGE_OFFSET = 1
_PAGE_LIMIT = 1
_SCRIPT_TOTAL = 2
_SCRIPT_PARAMS: dict[str, dict[str, type]] = {
    "report.sh": {"database": str, "count": int},
    "noparams.sh": {},
}
_LIST_VIEW = ListView(columns=[Column(key="filename", label="Filename")])


def _plugin_schema() -> AppSchema:
    """Build a minimal valid plugin-level schema for the static-schema cases."""
    return AppSchema(
        name="fixture-scripts", display_name="Fixture Scripts", list_view=_LIST_VIEW
    )


class _FixtureScript:
    """Implement :class:`ScriptProtocol` over an in-memory parameter spec.

    :param filename: The script filename, carried in the ``snippet_filename`` query
        parameter.
    :param params: The frontmatter parameters keyed by name to their value type.
    """

    def __init__(self, filename: str, params: dict[str, type]) -> None:
        self.filename = filename
        self.params = params

    @property
    def execution_task_name(self) -> str:
        """Return the fixed Tasks-API task name the fixture scripts execute under."""
        return _TASK_NAME

    def get_execution_model(self) -> type[BaseModel]:
        """Build a required-field model from the script's declared parameters."""
        fields = {name: (annotation, ...) for name, annotation in self.params.items()}
        return create_model(f"ExecModel_{self.filename.replace('.', '_')}", **fields)


class _MiniForm(AppFormModel):
    """Represent a minimal create form used to exercise the script-source rejection."""

    task_name: Annotated[str, Ui(label="Name", section="main")]


class _ListRow(BaseModel):
    """Represent the fixture list-row projection of a script."""

    filename: str
    task_name: str


class _FixtureMeta(BaseModel):
    """Represent the fixture execution-meta the framework posts to the Tasks API."""

    target: str
    filename: str
    sudo: bool
    args: dict[str, object]


def _build_form_schema(script: _FixtureScript) -> AppSchema:
    """Build a per-script schema whose form mirrors the script's parameters."""
    fields = [StringField(name=name, label=name) for name in script.params]
    return AppSchema(
        name="fixture-scripts",
        display_name="Fixture Scripts",
        forms=[FormSection(title="Parameters", fields=fields)],
        list_view=_LIST_VIEW,
    )


def _build_execution_meta(
    script: _FixtureScript, request: ScriptExecuteWrite
) -> _FixtureMeta:
    """Assemble the fixture execution-meta from the script and request body."""
    return _FixtureMeta(
        target=request.executor_host,
        filename=script.filename,
        sudo=request.sudo,
        args=request.args,
    )


def _list_row(script: _FixtureScript) -> _ListRow:
    """Project a script into its list-row response."""
    return _ListRow(filename=script.filename, task_name=script.execution_task_name)


class _Capabilities(BaseModel):
    """Represent the runtime capability flags a script app may expose."""

    enabled: bool = True


def _capabilities_provider() -> _Capabilities:
    """Return the fixture runtime capability flags."""
    return _Capabilities()


def _extra_router() -> APIRouter:
    """Build an extra router exposing a fixed auxiliary route."""
    router = APIRouter()

    @router.get("/ping", dependencies=[IsApiAuthenticated])
    async def _ping() -> dict[str, bool]:
        """Return a fixed auxiliary payload."""
        return {"ok": True}

    return router


def _make_source(
    scripts_dir: Path,
    *,
    static_schema: AppSchema | None = None,
    list_response_model: type[BaseModel] | None = None,
    load_script: Callable[[str], Awaitable[_FixtureScript]] | None = None,
    load_scripts: (
        Callable[[Sequence[str]], Awaitable[Mapping[str, _FixtureScript]]] | None
    ) = None,
) -> ScriptSource:
    """Build a ``ScriptSource`` whose hooks read the fixture script directory.

    ``load_script`` / ``load_scripts`` override the fixture loaders so a test can
    swap in a counting or failing variant without reaching around the source's
    ``frozen=True`` guard.
    """
    registry = {
        name: _FixtureScript(name, params) for name, params in _SCRIPT_PARAMS.items()
    }

    # Deliberately ignores ``list_query``: this fixture exercises the source's loader
    # and non-query behaviour, and declares no ``list_query_spec`` for the framework's
    # applier to replay.
    async def _list_scripts(
        _list_query: object, pagination: Pagination | None
    ) -> tuple[list[_FixtureScript], int]:
        scripts = [
            registry[path.name]
            for path in sorted(scripts_dir.iterdir())
            if path.name in registry
        ]
        if pagination is None:
            return scripts, len(scripts)
        return pagination.slice(scripts), len(scripts)

    async def _load_script(filename: str) -> _FixtureScript:
        if filename not in registry or not (scripts_dir / filename).is_file():
            raise HTTPNotFoundException
        return registry[filename]

    return ScriptSource(
        script_dir=scripts_dir,
        load_script=load_script or _load_script,
        list_scripts=_list_scripts,
        build_form_schema=_build_form_schema,
        build_execution_meta=_build_execution_meta,
        list_response=_list_row,
        static_schema=static_schema,
        list_response_model=list_response_model,
        load_scripts=load_scripts,
    )


def _script_app(source: ScriptSource, **overrides: object) -> TaskExecutionApp:
    """Build a script-flavored ``TaskExecutionApp`` over ``source``."""
    return TaskExecutionApp(
        name="fixture-scripts",
        uri_path=_PREFIX,
        owner=_OWNER,
        script_source=source,
        **overrides,
    )


def _make_tasks_api(
    *,
    history: dict | None = None,
    execute_id: int = 99,
    get_error: Exception | None = None,
    post_error: Exception | None = None,
) -> AsyncMock:
    """Build an ``AsyncMock(spec=RemoteAPI)`` routing the script Tasks-API calls."""
    api = AsyncMock(spec=RemoteAPI)

    async def _get(path: str, params: dict | None = None) -> dict:
        if get_error is not None:
            raise get_error
        return history if history is not None else {"items": [], "total": 0}

    async def _post(path: str, json: dict | None = None) -> dict:
        if post_error is not None:
            raise post_error
        return {"id": execute_id}

    api.get.side_effect = _get
    api.post.side_effect = _post
    return api


@pytest.fixture
def scripts_dir(tmp_path: Path) -> Path:
    """Create a fixture script directory holding the declared scripts."""
    for name in _SCRIPT_PARAMS:
        (tmp_path / name).write_text("#!/usr/bin/env bash\necho hi\n")
    return tmp_path


@pytest.fixture
def source(scripts_dir: Path) -> ScriptSource:
    """Build a default script source over the fixture directory."""
    return _make_source(scripts_dir)


def _client(
    app_def: TaskExecutionApp, tasks_api: AsyncMock, user: CasdoorUser
) -> TestClient:
    """Mount ``app_def`` with auth and Tasks-API overrides."""
    return build_contract_client(app_def, user=user, tasks_api=tasks_api)


class TestScriptSourceHooks:
    """Cover the ``ScriptSource`` hooks directly (AC #7: listing, form, meta)."""

    pytestmark = pytest.mark.asyncio

    async def test_list_scripts_returns_fixture_scripts(
        self, source: ScriptSource
    ) -> None:
        """Return one script per file discovered in the fixture directory."""
        scripts, total = await source.list_scripts(None, None)
        assert sorted(script.filename for script in scripts) == [
            "noparams.sh",
            "report.sh",
        ]
        assert total == len(scripts)

    async def test_load_script_unknown_raises_404(self, source: ScriptSource) -> None:
        """Raise the loader's 404 when the filename is absent from the directory."""
        with pytest.raises(HTTPNotFoundException):
            await source.load_script("missing.sh")

    async def test_build_form_schema_reflects_parameters(
        self, source: ScriptSource
    ) -> None:
        """Build a schema whose form fields mirror the script's parameters."""
        script = await source.load_script("report.sh")
        schema = source.build_form_schema(script)
        field_names = {field.name for field in schema.forms[0].fields}
        assert field_names == {"database", "count"}

    async def test_build_execution_meta_assembles_from_request(
        self, source: ScriptSource
    ) -> None:
        """Assemble the meta from the script and the validated request body."""
        script = await source.load_script("report.sh")
        request = ScriptExecuteWrite(
            executor_host="exec-1", args={"database": "db", "count": 3}
        )
        meta = source.build_execution_meta(script, request)
        assert isinstance(meta, _FixtureMeta)
        assert meta.target == "exec-1"
        assert meta.filename == "report.sh"
        assert meta.args == {"database": "db", "count": 3}


class TestBatchResolve:
    """Cover ``resolve_scripts``: batch hook, sequential fallback, per-filename result."""

    pytestmark = pytest.mark.asyncio

    async def test_fallback_loops_load_script_for_source_without_hook(
        self, scripts_dir: Path
    ) -> None:
        """Resolve each filename through ``load_script`` when no batch hook is set."""
        source = _make_source(scripts_dir)
        assert source.load_scripts is None
        resolved = await resolve_scripts(source, ["report.sh", "noparams.sh"])
        assert set(resolved) == {"report.sh", "noparams.sh"}
        assert resolved["report.sh"].filename == "report.sh"

    async def test_fallback_omits_unresolved_filename_without_raising(
        self, scripts_dir: Path
    ) -> None:
        """Drop a missing filename from the result rather than raising its 404."""
        source = _make_source(scripts_dir)
        resolved = await resolve_scripts(source, ["report.sh", "missing.sh"])
        assert set(resolved) == {"report.sh"}

    async def test_deduplicates_repeated_filename_to_one_lookup(
        self, scripts_dir: Path
    ) -> None:
        """Look a repeated filename up once and still key it in the result."""
        base = _make_source(scripts_dir)
        calls: list[str] = []

        async def _counting_load(filename: str) -> _FixtureScript:
            calls.append(filename)
            return await base.load_script(filename)

        source = _make_source(scripts_dir, load_script=_counting_load)
        resolved = await resolve_scripts(source, ["report.sh", "report.sh"])
        assert calls == ["report.sh"]
        assert set(resolved) == {"report.sh"}

    @pytest.mark.parametrize(
        "unsafe", ["../secret.sh", "/etc/passwd", "a/../../x", "a\\b.sh"]
    )
    async def test_traversal_filename_raises_before_any_lookup(
        self, scripts_dir: Path, unsafe: str
    ) -> None:
        """Reject an unsafe filename with 400 before touching any loader."""

        async def _fail_load(filename: str) -> _FixtureScript:
            raise AssertionError("load_script must not run for an unsafe filename")

        source = _make_source(scripts_dir, load_script=_fail_load)
        with pytest.raises(HTTPBadRequestException):
            await resolve_scripts(source, ["report.sh", unsafe])

    async def test_hook_receives_deduped_filenames_and_result_is_used(
        self, scripts_dir: Path
    ) -> None:
        """Delegate to ``load_scripts`` with deduped filenames when the hook is set."""
        seen: list[Sequence[str]] = []

        async def _batch(filenames: Sequence[str]) -> dict[str, _FixtureScript]:
            seen.append(list(filenames))
            return {name: _FixtureScript(name, {}) for name in filenames}

        async def _fail_load(filename: str) -> _FixtureScript:
            raise AssertionError("load_script must not run when a batch hook is set")

        source = _make_source(scripts_dir, load_script=_fail_load, load_scripts=_batch)
        resolved = await resolve_scripts(source, ["report.sh", "report.sh", "x.sh"])
        assert seen == [["report.sh", "x.sh"]]
        assert set(resolved) == {"report.sh", "x.sh"}

    @pytest.mark.parametrize("unsafe", ["../secret.sh", "a\\b.sh"])
    async def test_traversal_guard_runs_before_the_batch_hook(
        self, scripts_dir: Path, unsafe: str
    ) -> None:
        """Reject an unsafe filename with 400 before a batch hook can run."""

        async def _fail_batch(filenames: Sequence[str]) -> dict[str, _FixtureScript]:
            raise AssertionError("load_scripts must not run for an unsafe filename")

        source = _make_source(scripts_dir, load_scripts=_fail_batch)
        with pytest.raises(HTTPBadRequestException):
            await resolve_scripts(source, ["report.sh", unsafe])

    async def test_empty_selection_resolves_without_touching_a_loader(
        self, scripts_dir: Path
    ) -> None:
        """Resolve an empty selection to an empty mapping, calling no loader."""

        async def _fail_load(filename: str) -> _FixtureScript:
            raise AssertionError("no loader must run for an empty selection")

        async def _fail_batch(filenames: Sequence[str]) -> dict[str, _FixtureScript]:
            raise AssertionError("no loader must run for an empty selection")

        source = _make_source(
            scripts_dir, load_script=_fail_load, load_scripts=_fail_batch
        )
        assert await resolve_scripts(source, []) == {}


class TestDerivedRouteSurface:
    """Inspect the derived router without HTTP."""

    def test_registers_expected_routes(self, source: ScriptSource) -> None:
        """Register listing, per-script schema, execute, and history routes."""
        routes = _routes(_script_app(source))
        assert ("/", "GET") in routes
        assert ("/snippet/schema", "GET") in routes
        assert ("/snippet/execute", "POST") in routes
        assert ("/snippet/history", "GET") in routes

    def test_no_static_schema_route_by_default(self, source: ScriptSource) -> None:
        """Omit ``GET /schema`` when the source carries no static schema."""
        assert ("/schema", "GET") not in _routes(_script_app(source))

    def test_static_schema_route_when_configured(self, scripts_dir: Path) -> None:
        """Register ``GET /schema`` when the source carries a static schema."""
        schema = _plugin_schema()
        app_def = _script_app(_make_source(scripts_dir, static_schema=schema))
        assert ("/schema", "GET") in _routes(app_def)

    def test_list_response_model_types_the_derived_list_route(
        self, scripts_dir: Path, regular_user: CasdoorUser
    ) -> None:
        """Assert the opt-out ``GET /`` 200 is typed ``array<list_response_model>``."""
        source = _make_source(scripts_dir, list_response_model=_ListRow)
        client = _client(
            _script_app(source, pagination=NO_PAGINATION),
            _make_tasks_api(),
            regular_user,
        )
        spec = client.get("/openapi.json").json()
        list_schema = spec["paths"][f"{_BASE}/"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert list_schema["type"] == "array"
        assert list_schema["items"]["$ref"].rsplit("/", 1)[-1] == "_ListRow"

    def test_derived_list_route_untyped_without_response_model(
        self, scripts_dir: Path, regular_user: CasdoorUser
    ) -> None:
        """Assert the derived ``GET /`` 200 stays untyped without a ``list_response_model``."""
        client = _client(
            _script_app(_make_source(scripts_dir)), _make_tasks_api(), regular_user
        )
        spec = client.get("/openapi.json").json()
        list_schema = spec["paths"][f"{_BASE}/"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert list_schema.get("type") != "array"


class TestDerivedRouteHTTP:
    """Exercise the full derived route matrix through a real ``TestClient``."""

    def test_default_list_returns_paginated_rows(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """List every discovered script as list-row projections in the default envelope."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/")
        assert response.status_code == status.HTTP_200_OK
        assert sorted(row["filename"] for row in response.json()["items"]) == [
            "noparams.sh",
            "report.sh",
        ]

    def test_no_pagination_sentinel_returns_plain_array(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Return a plain JSON array when the app opts out via ``NO_PAGINATION``."""
        client = _client(
            _script_app(source, pagination=NO_PAGINATION),
            _make_tasks_api(),
            regular_user,
        )
        response = client.get(f"{_BASE}/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert isinstance(body, list)
        assert sorted(row["filename"] for row in body) == ["noparams.sh", "report.sh"]

    def test_empty_listing_returns_empty_envelope(
        self, scripts_dir: Path, regular_user: CasdoorUser
    ) -> None:
        """Return ``200`` with an empty paginated envelope when no scripts exist."""
        source = _make_source(scripts_dir)

        async def _empty(
            _list_query: object, _pagination: Pagination | None
        ) -> tuple[list[_FixtureScript], int]:
            return [], 0

        source = ScriptSource(
            script_dir=source.script_dir,
            load_script=source.load_script,
            list_scripts=_empty,
            build_form_schema=source.build_form_schema,
            build_execution_meta=source.build_execution_meta,
            list_response=source.list_response,
        )
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_per_script_schema_reflects_parameters(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Return the per-script schema reflecting the frontmatter parameters."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(
            f"{_BASE}/snippet/schema", params={"snippet_filename": "report.sh"}
        )
        assert response.status_code == status.HTTP_200_OK
        names = {field["name"] for field in response.json()["forms"][0]["fields"]}
        assert names == {"database", "count"}

    def test_per_script_schema_zero_parameters(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Return a valid parameterless form for a zero-parameter script."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(
            f"{_BASE}/snippet/schema", params={"snippet_filename": "noparams.sh"}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["forms"][0]["fields"] == []

    def test_per_script_schema_unknown_filename_404(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Surface the loader's 404 for an unknown ``snippet_filename``."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(
            f"{_BASE}/snippet/schema", params={"snippet_filename": "missing.sh"}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.parametrize(
        "bad_filename", ["../secret.sh", "/etc/passwd", "a/../../x"]
    )
    def test_unsafe_filename_returns_400(
        self, source: ScriptSource, regular_user: CasdoorUser, bad_filename: str
    ) -> None:
        """Reject a traversal/absolute ``snippet_filename`` before any load_script."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(
            f"{_BASE}/snippet/schema", params={"snippet_filename": bad_filename}
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_execute_posts_meta_envelope(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Validate args, assemble meta, and post it to the execute endpoint."""
        tasks_api = _make_tasks_api(execute_id=42)
        client = _client(_script_app(source), tasks_api, regular_user)
        response = client.post(
            f"{_BASE}/snippet/execute",
            params={"snippet_filename": "report.sh"},
            json={"executor_host": "exec-1", "args": {"database": "db", "count": 5}},
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {
            "task_name": _TASK_NAME,
            "task_id": 42,
            "snippet_filename": "report.sh",
        }
        tasks_api.post.assert_awaited_once()
        call = tasks_api.post.await_args
        assert call.args[0] == f"/execute/{_TASK_NAME}"
        assert call.kwargs["json"]["meta"]["filename"] == "report.sh"
        assert call.kwargs["json"]["meta"]["target"] == "exec-1"

    def test_execute_passes_coerced_args_to_meta(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Build the meta from the model's coerced args, not the raw request body."""
        tasks_api = _make_tasks_api()
        client = _client(_script_app(source), tasks_api, regular_user)
        response = client.post(
            f"{_BASE}/snippet/execute",
            params={"snippet_filename": "report.sh"},
            json={"executor_host": "exec-1", "args": {"database": "db", "count": "5"}},
        )
        assert response.status_code == status.HTTP_201_CREATED
        call = tasks_api.post.await_args
        assert call.kwargs["json"]["meta"]["args"] == {"database": "db", "count": 5}

    def test_execute_invalid_args_returns_422(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Reject args that fail the per-script execution model with ``422``."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.post(
            f"{_BASE}/snippet/execute",
            params={"snippet_filename": "report.sh"},
            json={"executor_host": "exec-1", "args": {"database": "db", "count": "x"}},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_execute_unknown_filename_404(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Surface the loader's 404 before any args validation."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.post(
            f"{_BASE}/snippet/execute",
            params={"snippet_filename": "missing.sh"},
            json={"executor_host": "exec-1", "args": {}},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_execute_upstream_error_propagates(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Propagate an upstream Tasks-API failure rather than fake success."""
        tasks_api = _make_tasks_api(post_error=RuntimeError("tasks api down"))
        client = _client(_script_app(source), tasks_api, regular_user)
        response = client.post(
            f"{_BASE}/snippet/execute",
            params={"snippet_filename": "report.sh"},
            json={"executor_host": "exec-1", "args": {"database": "db", "count": 5}},
        )
        assert response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_history_proxies_upstream(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Return the per-script execution history proxied by filename."""
        tasks_api = _make_tasks_api(history={"items": [{"id": 1}], "total": 1})
        client = _client(_script_app(source), tasks_api, regular_user)
        response = client.get(
            f"{_BASE}/snippet/history", params={"snippet_filename": "report.sh"}
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"items": [{"id": 1}], "total": 1}
        call = tasks_api.get.await_args
        assert call.args[0] == f"/{_TASK_NAME}/history/"
        assert call.kwargs["params"] == {"snippet_filename": "report.sh"}

    def test_history_is_scoped_to_the_scripts_own_task_only(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Query exactly one task's history — the script's own, never a proxy's.

        Pins a deliberately accepted consequence of app-owned proxy dispatch: the
        upstream filters history by the named task, so a run dispatched under an
        app's proxy carries a different ``task_id`` and does not appear in this pane.
        Teaching the route to union an app's proxies is a design decision with its own
        trade-offs, not a drift to make silently — so this asserts the single-task
        scope rather than leaving it implicit.
        """
        tasks_api = _make_tasks_api(history={"items": [], "total": 0})
        client = _client(_script_app(source), tasks_api, regular_user)

        client.get(f"{_BASE}/snippet/history", params={"snippet_filename": "report.sh"})

        assert tasks_api.get.await_count == 1
        assert tasks_api.get.await_args.args[0] == f"/{_TASK_NAME}/history/"

    def test_history_upstream_error_propagates(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Propagate an upstream history failure rather than mask it as empty."""
        tasks_api = _make_tasks_api(get_error=RuntimeError("history down"))
        client = _client(_script_app(source), tasks_api, regular_user)
        response = client.get(
            f"{_BASE}/snippet/history", params={"snippet_filename": "report.sh"}
        )
        assert response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_static_schema_served_when_configured(
        self, scripts_dir: Path, regular_user: CasdoorUser
    ) -> None:
        """Serve the plugin-level schema at ``GET /schema`` when configured."""
        schema = _plugin_schema()
        app_def = _script_app(_make_source(scripts_dir, static_schema=schema))
        client = _client(app_def, _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/schema")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "fixture-scripts"

    def test_static_schema_absent_returns_404(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Return ``404`` for ``GET /schema`` when no static schema is configured."""
        client = _client(_script_app(source), _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/schema")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDerivedRoutePaginatedList:
    """Exercise the paginated ``GET /`` list route over HTTP."""

    def test_paginated_list_returns_envelope(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Assert pagination switches the list to a ``PaginatedResponse`` envelope."""
        client = _client(
            _script_app(source, pagination=make_pagination_dep(max_limit=50)),
            _make_tasks_api(),
            regular_user,
        )
        response = client.get(f"{_BASE}/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert set(body) == {"items", "total", "offset", "limit"}
        assert sorted(item["filename"] for item in body["items"]) == [
            "noparams.sh",
            "report.sh",
        ]

    def test_paginated_list_slices_by_offset_and_limit(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Assert offset/limit slice the discovered scripts client-side."""
        client = _client(
            _script_app(source, pagination=make_pagination_dep(max_limit=50)),
            _make_tasks_api(),
            regular_user,
        )
        response = client.get(
            f"{_BASE}/",
            params={"offset": _PAGE_OFFSET, "limit": _PAGE_LIMIT},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["offset"] == _PAGE_OFFSET
        assert body["limit"] == _PAGE_LIMIT
        assert body["total"] == _SCRIPT_TOTAL
        assert [item["filename"] for item in body["items"]] == ["report.sh"]

    def test_paginated_list_untyped_without_response_model(
        self, scripts_dir: Path, regular_user: CasdoorUser
    ) -> None:
        """Assert pagination emits an untyped envelope without ``list_response_model``."""
        client = _client(
            _script_app(
                _make_source(scripts_dir),
                pagination=make_pagination_dep(max_limit=50),
            ),
            _make_tasks_api(),
            regular_user,
        )
        spec = client.get("/openapi.json").json()
        list_schema = spec["paths"][f"{_BASE}/"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert list_schema.get("type") != "array"
        if "$ref" in list_schema:
            schema_name = list_schema["$ref"].rsplit("/", 1)[-1]
            envelope_schema = spec["components"]["schemas"][schema_name]
            assert "items" in envelope_schema["properties"]
        else:
            assert "items" in list_schema.get("properties", {})
        response = client.get(f"{_BASE}/")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert set(body) == {"items", "total", "offset", "limit"}
        assert sorted(item["filename"] for item in body["items"]) == [
            "noparams.sh",
            "report.sh",
        ]


class TestScriptAppAuxiliarySurface:
    """Cover the auxiliary surface a script app keeps (extra_routes + capabilities)."""

    def test_extra_routes_mounted(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Register a script app's extra_routes alongside the derived surface."""
        app_def = _script_app(source, extra_routes=(_extra_router(),))
        client = _client(app_def, _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/ping")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"ok": True}

    def test_capabilities_route_mounted(
        self, source: ScriptSource, regular_user: CasdoorUser
    ) -> None:
        """Register a script app's GET /capabilities from its provider."""
        app_def = _script_app(source, capabilities_provider=_capabilities_provider)
        client = _client(app_def, _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/capabilities")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"enabled": True}


class TestScriptSourceValidation:
    """Cover the ``TaskExecutionApp`` validators for the script flavor."""

    def test_script_source_alone_builds(self, source: ScriptSource) -> None:
        """Build a working router from ``script_source`` with no payload source."""
        app_def = _script_app(source)
        assert app_def.api_router is not None
        assert ("/", "GET") in _routes(app_def)

    def test_script_source_with_task_spec_builder_raises(
        self, source: ScriptSource
    ) -> None:
        """Reject a ``script_source`` app that also sets ``task_spec_builder``."""

        def _spec_builder(form: object, resolved: object) -> object:
            return object()

        with pytest.raises(ValueError, match="mutually exclusive"):
            _script_app(source, task_spec_builder=_spec_builder)

    def test_script_source_with_create_model_raises(self, source: ScriptSource) -> None:
        """Reject a ``script_source`` app that also sets ``create_model``."""
        with pytest.raises(ValueError, match="no single create_model"):
            _script_app(source, create_model=_MiniForm)

    def test_script_source_with_schema_raises(self, source: ScriptSource) -> None:
        """Reject a ``script_source`` app that also sets ``schema=``."""
        schema = _plugin_schema()
        with pytest.raises(ValueError, match=re.escape("script_source.static_schema")):
            _script_app(source, schema=schema)

    def test_script_source_with_create_extra_deps_raises(
        self, source: ScriptSource
    ) -> None:
        """Reject create-route knobs the script branch would silently drop."""

        async def _guard() -> None:
            """Represent a create-route access guard."""

        with pytest.raises(ValueError, match="derives no create route"):
            _script_app(source, create_extra_deps=(Depends(_guard),))

    def test_list_query_dep_with_in_memory_flag_raises(
        self, source: ScriptSource
    ) -> None:
        """Reject a source whose two list-query dependency knobs disagree.

        The framework prefers ``list_query_dep``, so pairing it with the in-memory flag
        would drop the flag silently rather than telling the author which one wins.
        """
        with pytest.raises(ValueError, match="list_query_dep supersedes"):
            replace(
                source,
                list_query_dep=object,
                in_memory_list_query=True,
            )


class TestConformanceGuards:
    """Cover the scoped conformance guards for the script flavor."""

    def test_capability_route_consistency_skips_script_app(
        self, source: ScriptSource
    ) -> None:
        """Return no findings for a script app from the verb-toggled CRUD route check."""
        assert check_capability_route_consistency(_script_app(source)) == []

    def test_view_fields_check_skips_script_app(self, source: ScriptSource) -> None:
        """Return no findings for a script app from the detail-view field check."""
        assert check_view_fields_reference_real_fields(_script_app(source)) == []

    def test_schema_derivation_check_skips_script_app(
        self, source: ScriptSource
    ) -> None:
        """Return no findings for a script app from the create-model derivation check."""
        assert check_schema_derivation_succeeds(_script_app(source)) == []
