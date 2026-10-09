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

"""Cover ``TaskExecutionApp``: route composition, derived surfaces, and binding.

Every derived surface is exercised through a real ``TestClient`` against a
synthetic app definition, so body parsing and dependency injection resolve
end-to-end. The create test issues a real form POST and never overrides the
``create_payload`` dependency — only the inventory client — so the three-phase
body resolution it exists to cover is genuinely executed.
"""

import re
from dataclasses import replace
from typing import Annotated, Any, get_args
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, computed_field, ValidationError
from sqlalchemy import column

from app.api.deps import SERVICE_PRINCIPAL_ID
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.list_query import ListQuerySpec
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import make_pagination_dep
from app.core.requests.remote_api import RemoteAPI
from app.extensions.apps.backup_mongo.restore.app import app as mongo_restore_app
from app.extensions.apps.framework import (
    BaseTaskResponse,
    ConnectivityWarning,
    TaskExecuteWrite,
    TaskExecutionResponse,
)
from app.extensions.apps.framework.apps import (
    AppCapabilities,
    ListFilterConfig,
    NO_PAGINATION,
    TaskExecutionApp,
    UNGUARDED,
    Views,
)
from app.extensions.apps.framework.form_dsl import (
    AppFormModel,
    ArgFormat,
    FormLayout,
    SectionLayout,
    ServiceRef,
    Ui,
)
from app.extensions.apps.framework.schema import (
    AppSchema,
    BoolField,
    Capabilities,
    Column,
    ColumnFormat,
    FormSection,
    ListView,
    RelatedApp,
)
from app.extensions.apps.framework.script_source import ScriptSource
from app.extensions.apps.mysql_backups.app import app as mysql_backups_app
from app.extensions.apps.mysql_backups.restore.app import app as mysql_restore_app
from app.extensions.apps.snippets.app import app as snippets_app
from app.extensions.connectivity import (
    CONNECTIVITY_META_HOST_KEY,
    CONNECTIVITY_META_PORT_KEY,
    CONNECTIVITY_META_SERVICE_TYPE_KEY,
)
from app.extensions.deps import get_username_mapping, InventoryAPI, IsApiAuthenticated
from app.extensions.snippets.schema import SNIPPETS_PLUGIN_SCHEMA
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import Task, TaskWrite
from tests.app.extensions.apps.framework.contract_suite import build_contract_client
from tests.app.extensions.apps.framework.contract_suite import (
    routes_of as _routes,
)
from tests.app.extensions.apps.framework.kit import (
    EXECUTE_CREATED_AT,
    EXECUTE_STATUS,
    synth_app,
    synth_app_kwargs,
    synth_reject_running_task,
    synth_script_app,
    SynthExecuteResponse,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_EXECUTOR_HOST as _EXECUTOR_HOST,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_OWNER as _OWNER,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_PREFIX as _PREFIX,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_SCRIPT_PREFIX as _SCRIPT_PREFIX,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_SERVICE_HOST as _SERVICE_HOST,
)
from tests.app.extensions.apps.framework.kit import (
    SYNTH_SERVICE_PORT as _SERVICE_PORT,
)
from tests.app.extensions.apps.framework.kit import (
    SynthResponse as _SynthResponse,
)
from tests.app.factories import (
    CreatedNodeFactory,
    CreatedServiceFactory,
    TaskFactory,
)

# scaffolding-dup-ok: pre-existing on main in both this file and
# test_script_source.py; extracting it is unrelated to this change.
_BASE = f"/api/apps{_PREFIX}"
_SCRIPT_BASE = f"/api/apps{_SCRIPT_PREFIX}"

_LIST_VIEW = ListView(columns=[Column(key="name", label="Name")])


class _NoTaskNameForm(AppFormModel):
    """Represent a synthetic create form that omits the mandatory ``task_name`` field."""

    label: Annotated[str, Ui(label="Label", section="main")] = ""


class _TwoMarkedServiceForm(AppFormModel):
    """Represent a create form declaring two ``check_connectivity`` services."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="B", section="main"),
    ] = None


class _TwoUnmarkedServiceForm(AppFormModel):
    """Represent a create form declaring two unmarked services (no primary)."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,)),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,)),
        Ui(label="B", section="main"),
    ] = None


class _MultiCheckConnectivityForm(AppFormModel):
    """Represent a create form whose multi-value ``ServiceRef`` enables the probe."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    services: Annotated[
        list[int],
        ServiceRef(
            service_types=(ServiceTypeEnum.MYSQL,),
            multiple=True,
            check_connectivity=True,
        ),
        Ui(label="Services", section="main"),
    ]


class _SoleMultiServiceForm(AppFormModel):
    """Represent a create form whose sole ``ServiceRef`` is multi-value (no primary)."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    services: Annotated[
        list[int],
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), multiple=True),
        Ui(label="Services", section="main"),
    ]


class _PrimaryDesignatedServiceForm(AppFormModel):
    """Represent two services, one designated ``primary`` without a probe.

    The primary is declared *before* an unmarked destination so the old
    two-or-more-unmarked rejection no longer fires and the designation, not
    last-wins, names the primary.
    """

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), primary=True),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,)),
        Ui(label="B", section="main"),
    ] = None


class _PrimaryAndCheckConflictForm(AppFormModel):
    """Represent a ``primary`` field conflicting with a ``check_connectivity`` field."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), primary=True),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="B", section="main"),
    ] = None


class _TwoPrimaryServiceForm(AppFormModel):
    """Represent a create form designating two ``primary`` services."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), primary=True),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), primary=True),
        Ui(label="B", section="main"),
    ] = None


class _PrimaryMultipleForm(AppFormModel):
    """Represent a multi-value ``ServiceRef`` designated ``primary``."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    services: Annotated[
        list[int],
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), multiple=True, primary=True),
        Ui(label="Services", section="main"),
    ]


class _RedundantPrimaryProbeForm(AppFormModel):
    """Represent a single field carrying both ``primary`` and ``check_connectivity``."""

    task_name: Annotated[str, Ui(label="Name", section="main")] = ""
    service_a: Annotated[
        int | None,
        ServiceRef(
            service_types=(ServiceTypeEnum.MYSQL,),
            check_connectivity=True,
            primary=True,
        ),
        Ui(label="A", section="main"),
    ] = None
    service_b: Annotated[
        int | None,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,)),
        Ui(label="B", section="main"),
    ] = None


class _BadArgFormatForm(AppFormModel):
    """Represent a create form whose ``ArgFormat`` template misspells the placeholder."""

    task_name: Annotated[str, Ui(label="Name", section="main")]
    databases: Annotated[
        str, ArgFormat("--databases=${vale}"), Ui(label="DB", section="main")
    ] = ""


async def _delete_handler(task_name: str) -> None:
    """Stand in as a delete handler so the delete route can be registered."""


async def _update_handler(task_name: str) -> None:
    """Stand in as an update handler so the update route can be registered."""


async def _passthrough_payload_builder(inventory_api: InventoryAPI) -> TaskWrite:
    """Stand in as the create payload for a transitional ``schema=`` app."""
    return TaskWrite(name="passthrough", owner=_OWNER, data={})


def _extra_router() -> APIRouter:
    """Return an extra router exposing a fixed ``GET /ping`` route."""
    router = APIRouter()

    @router.get("/ping", dependencies=[IsApiAuthenticated])
    async def _ping() -> dict[str, bool]:
        return {"pong": True}

    return router


def _custom_detail_router() -> APIRouter:
    """Return an extra router exposing a custom ``GET /{task_name}`` detail route."""
    router = APIRouter()

    @router.get("/{task_name}", dependencies=[IsApiAuthenticated])
    async def _detail(task_name: str) -> dict[str, str]:
        return {"custom_detail": task_name}

    return router


def _custom_list_router() -> APIRouter:
    """Return an extra router exposing a custom ``GET /`` collection-root route."""
    router = APIRouter()

    @router.get("/", dependencies=[IsApiAuthenticated])
    async def _list() -> dict[str, str]:
        return {"custom_list": "ok"}

    return router


_PASSTHROUGH_SCHEMA = AppSchema(
    name="synthetic-app",
    display_name="Synthetic App",
    forms=[FormSection(title="Options", fields=[BoolField(name="flag", label="Flag")])],
    list_view=_LIST_VIEW,
)


def _synth_app(**overrides: object) -> TaskExecutionApp:
    """Build the synthetic app with the extra ``/ping`` router by default."""
    overrides.setdefault("extra_routes", (_extra_router(),))
    return synth_app(**overrides)


_SCRIPT_LIST_SPEC = ListQuerySpec(
    sortable={"filename": column("filename")},
    default_sort="filename",
    tie_breaker=column("filename"),
)


def _script_source_with_in_memory_query() -> ScriptSource:
    """Return a script source that resolves a list query in-process.

    :return: A source whose ``in_memory_list_query`` obliges the app to declare a spec.
    """
    source = synth_script_app().script_source
    return replace(source, in_memory_list_query=True)


def _task_dict(name: str, *, meta: dict | None = None) -> dict:
    """Return a created-task payload owned by the synthetic owner."""
    return TaskFactory.build(
        name=name, owner=_OWNER, data={"meta": meta or {}}
    ).model_dump(mode="json")


def _execute_response(name: str, task_id: int = 99) -> dict:
    """Return a minimal ``TaskHistoryResponse``-shaped dict for execute tests."""
    return {
        "id": task_id,
        "execution_request": {"task": "synth-cmd", "target": "host1"},
        "task": {**_task_dict(name), "deleted_at": None},
        "status": EXECUTE_STATUS.value,
        "created_at": EXECUTE_CREATED_AT,
    }


def _fake_service() -> dict:
    """Return the inventory service wire dict the fake Inventory API returns."""
    return CreatedServiceFactory.build(
        node=CreatedNodeFactory.build(address=_SERVICE_HOST),
        type=ServiceTypeEnum.MYSQL,
        name="svc-1",
        port=_SERVICE_PORT,
    ).model_dump(mode="json")


def _make_tasks_api(
    *,
    list_items: list[dict] | None = None,
    list_total: int | None = None,
    detail_task: dict | None = None,
    created_task: dict | None = None,
    latest_statuses: dict[str, str | None] | None = None,
) -> AsyncMock:
    """Build an ``AsyncMock(spec=RemoteAPI)`` routing Tasks-API calls in memory."""
    api = AsyncMock(spec=RemoteAPI)

    async def _get(path: str, params: dict | None = None) -> dict:
        if path == "/":
            envelope = {"items": list_items or []}
            if list_total is not None:
                envelope["total"] = list_total
            return envelope
        if path.endswith("/history/"):
            return {"items": []}
        return detail_task if detail_task is not None else {}

    async def _post(path: str, json: dict | None = None) -> dict:
        if path == "/history/latest":
            names = (json or {}).get("names", [])
            statuses = latest_statuses or {}
            return {
                name: (
                    {"status": status, "finished_at": None}
                    if (status := statuses.get(name)) is not None
                    else None
                )
                for name in names
            }
        return created_task if created_task is not None else {}

    api.get.side_effect = _get
    api.post.side_effect = _post
    return api


def _make_inventory_api() -> AsyncMock:
    """Build an ``AsyncMock(spec=RemoteAPI)`` returning the fake service."""
    api = AsyncMock(spec=RemoteAPI)

    async def _get(path: str, params: dict | None = None) -> dict:
        return _fake_service()

    api.get.side_effect = _get
    return api


def _client(
    app_def: TaskExecutionApp,
    tasks_api: AsyncMock,
    user: CasdoorUser,
    inventory_api: AsyncMock | None = None,
) -> TestClient:
    """Mount ``app_def`` with auth, Tasks-API, and Inventory-API overrides."""
    return build_contract_client(
        app_def, user=user, tasks_api=tasks_api, inventory_api=inventory_api
    )


def _list_upstream_params(tasks_api: AsyncMock) -> dict[str, Any]:
    """Return the ``params`` of the upstream ``GET /`` list call recorded on the mock."""
    for recorded in tasks_api.get.call_args_list:
        if recorded.args and recorded.args[0] == "/":
            return recorded.kwargs.get("params", {})
    return {}


class TestRouterComposition:
    """Inspect the derived router without HTTP."""

    def test_registers_every_derived_surface(self) -> None:
        """Assert schema, capabilities, list, detail, create, execute, extra exist."""
        routes = _routes(_synth_app())

        assert ("/schema", "GET") in routes
        assert ("/capabilities", "GET") in routes
        assert ("/", "GET") in routes
        assert ("/{task_name}", "GET") in routes
        assert ("/", "POST") in routes
        assert ("/{task_name}/execute", "POST") in routes
        assert ("/ping", "GET") in routes

    def test_capabilities_registered_before_greedy_detail(self) -> None:
        """Assert ``/capabilities`` precedes the greedy ``/{task_name}`` route."""
        api_routes = [
            r for r in _synth_app().api_router.routes if isinstance(r, APIRoute)
        ]
        paths = [r.path for r in api_routes]

        assert paths.index("/capabilities") < paths.index("/{task_name}")

    def test_no_create_route_when_create_capability_off(self) -> None:
        """Assert ``create=False`` derives no ``POST /`` route."""
        routes = _routes(
            _synth_app(capabilities=AppCapabilities(create=False), create_extra_deps=())
        )

        assert ("/", "POST") not in routes
        assert ("/{task_name}", "GET") in routes

    def test_no_execute_route_when_execute_capability_off(self) -> None:
        """Assert ``execute=False`` derives no execute route."""
        routes = _routes(_synth_app(capabilities=AppCapabilities(execute=False)))

        assert ("/{task_name}/execute", "POST") not in routes

    def test_no_update_or_delete_routes_by_default(self) -> None:
        """Assert no ``PUT`` / ``DELETE`` route is derived without the capability."""
        routes = _routes(_synth_app())

        assert ("/{task_name}", "PUT") not in routes
        assert ("/{task_name}", "DELETE") not in routes

    def test_list_suppress_keeps_only_the_custom_list_route(self) -> None:
        """Assert ``list=False`` derives no list route, leaving the custom one to win."""
        app_def = _synth_app(
            capabilities=AppCapabilities(list=False),
            extra_routes=(_custom_list_router(),),
        )
        get_root = [
            route
            for route in app_def.api_router.routes
            if isinstance(route, APIRoute)
            and route.path == "/"
            and "GET" in route.methods
        ]

        assert len(get_root) == 1
        assert get_root[0].endpoint.__name__ == "_list"

    def test_delete_route_when_capability_and_handler(self) -> None:
        """Assert ``delete=True`` with a handler derives a ``DELETE`` route at 204."""
        app_def = _synth_app(
            capabilities=AppCapabilities(delete=True), delete_handler=_delete_handler
        )
        delete_route = next(
            route
            for route in app_def.api_router.routes
            if isinstance(route, APIRoute)
            and route.path == "/{task_name}"
            and "DELETE" in route.methods
        )

        assert delete_route.status_code == status.HTTP_204_NO_CONTENT

    def test_task_dep_property_resolves_to_task(self) -> None:
        """Assert ``task_dep`` exposes an ``Annotated[Task, Depends(...)]`` alias."""
        task_dep = _synth_app().task_dep

        assert get_args(task_dep)[0] is Task


class TestSchemaEndpoint:
    """Exercise ``GET /schema`` over HTTP."""

    def test_schema_returns_derived_plugin_schema(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the schema route returns the derived ``AppSchema``."""
        client = _client(_synth_app(), _make_tasks_api(), regular_user)

        response = client.get(f"{_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "synthetic-app"


class TestCapabilitiesEndpoint:
    """Exercise ``GET /capabilities`` over HTTP."""

    def test_capabilities_not_shadowed_by_greedy_detail(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``/capabilities`` resolves to the provider, not the detail route."""
        client = _client(_synth_app(), _make_tasks_api(), regular_user)

        response = client.get(f"{_BASE}/capabilities")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"manual_sync_enabled": True}


class TestListAndDetail:
    """Exercise the list and detail routes over HTTP."""

    def test_no_pagination_sentinel_returns_plain_list(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``pagination=NO_PAGINATION`` keeps ``GET /`` a plain list."""
        tasks_api = _make_tasks_api(list_items=[_task_dict("t-1")])
        client = _client(_synth_app(pagination=NO_PAGINATION), tasks_api, regular_user)

        response = client.get(f"{_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()] == ["t-1"]

    def test_distinct_no_pagination_instance_returns_plain_list(
        self, regular_user: CasdoorUser
    ) -> None:
        """Opt out of pagination for any sentinel instance, not just the singleton."""
        tasks_api = _make_tasks_api(list_items=[_task_dict("t-1")])
        client = _client(
            _synth_app(pagination=type(NO_PAGINATION)()), tasks_api, regular_user
        )

        response = client.get(f"{_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()] == ["t-1"]

    def test_default_list_is_paginated(self, regular_user: CasdoorUser) -> None:
        """Assert the default ``GET /`` returns a ``PaginatedResponse`` envelope."""
        tasks_api = _make_tasks_api(list_items=[_task_dict("t-1")], list_total=1)
        client = _client(_synth_app(), tasks_api, regular_user)

        response = client.get(f"{_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert [item["name"] for item in body["items"]] == ["t-1"]

    def test_paginated_list_returns_paginated_response(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a per-app ``make_pagination_dep`` override keeps the envelope."""
        tasks_api = _make_tasks_api(list_items=[_task_dict("t-1")], list_total=1)
        client = _client(
            _synth_app(pagination=make_pagination_dep(max_limit=50)),
            tasks_api,
            regular_user,
        )

        response = client.get(f"{_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert [item["name"] for item in body["items"]] == ["t-1"]

    def test_detail_returns_response(self, regular_user: CasdoorUser) -> None:
        """Assert ``GET /{task_name}`` returns the derived detail response."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t-1"))
        client = _client(_synth_app(), tasks_api, regular_user)

        response = client.get(f"{_BASE}/t-1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "t-1"


class TestScriptListRoute:
    """Exercise the script-flavored derived list route over HTTP."""

    def test_script_default_list_is_paginated(self, regular_user: CasdoorUser) -> None:
        """Assert a script app's default ``GET /`` returns a paginated envelope."""
        client = _client(synth_script_app(), _make_tasks_api(), regular_user)

        response = client.get(f"{_SCRIPT_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert [item["filename"] for item in body["items"]] == ["synth.sh"]

    def test_script_no_pagination_sentinel_returns_plain_list(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``pagination=NO_PAGINATION`` keeps a script app's list plain."""
        client = _client(
            synth_script_app(pagination=NO_PAGINATION),
            _make_tasks_api(),
            regular_user,
        )

        response = client.get(f"{_SCRIPT_BASE}/")

        assert response.status_code == status.HTTP_200_OK
        assert [item["filename"] for item in response.json()] == ["synth.sh"]


class TestCreateRoute:
    """Exercise the ``POST /`` create route over HTTP without overriding the body dep."""

    def test_create_posts_derived_envelope(self, regular_user: CasdoorUser) -> None:
        """Assert a real form POST builds and posts the three-phase envelope."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _client(
            _synth_app(), tasks_api, regular_user, inventory_api=_make_inventory_api()
        )

        response = client.post(
            f"{_BASE}/",
            json={
                "task_name": "new-task",
                "service_id": 1,
                "host": _EXECUTOR_HOST,
                "alert_on_fail": True,
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        create_call = next(
            call for call in tasks_api.post.await_args_list if call.args[0] == "/"
        )
        posted = create_call.kwargs["json"]
        assert posted["data"]["task"] == "run-command"
        assert posted["data"]["meta"]["command"] == "synth-cmd"
        assert posted["data"]["meta"]["target"] == _EXECUTOR_HOST
        assert posted["data"]["meta"][CONNECTIVITY_META_HOST_KEY] == _SERVICE_HOST
        assert posted["data"]["meta"][CONNECTIVITY_META_PORT_KEY] == _SERVICE_PORT
        assert (
            posted["data"]["meta"][CONNECTIVITY_META_SERVICE_TYPE_KEY]
            == ServiceTypeEnum.MYSQL.value
        )
        assert posted["alert_on_fail"] is True

    def test_create_threads_alert_detail_builder(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the app's ``alert_detail_builder`` is stamped onto the posted task."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _client(
            _synth_app(alert_detail_builder="app.extensions.apps.pkg.mod:builder"),
            tasks_api,
            regular_user,
            inventory_api=_make_inventory_api(),
        )

        response = client.post(
            f"{_BASE}/",
            json={"task_name": "new-task", "service_id": 1, "host": _EXECUTOR_HOST},
        )

        assert response.status_code == status.HTTP_201_CREATED
        create_call = next(
            call for call in tasks_api.post.await_args_list if call.args[0] == "/"
        )
        assert (
            create_call.kwargs["json"]["alert_detail_builder"]
            == "app.extensions.apps.pkg.mod:builder"
        )

    def test_create_threads_run_result_recorder(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the app's ``run_result_recorder`` is stamped onto the posted task."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _client(
            _synth_app(run_result_recorder="app.extensions.apps.pkg.mod:recorder"),
            tasks_api,
            regular_user,
            inventory_api=_make_inventory_api(),
        )

        response = client.post(
            f"{_BASE}/",
            json={"task_name": "new-task", "service_id": 1, "host": _EXECUTOR_HOST},
        )

        assert response.status_code == status.HTTP_201_CREATED
        create_call = next(
            call for call in tasks_api.post.await_args_list if call.args[0] == "/"
        )
        assert (
            create_call.kwargs["json"]["run_result_recorder"]
            == "app.extensions.apps.pkg.mod:recorder"
        )

    def test_create_response_model_with_context_provider_succeeds(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a synthesized create builder tolerates an active context provider.

        ``create_response_model`` synthesizes a no-extras create builder; with the
        inherited context provider active the framework binds context into it, so it
        must accept (and ignore) the context kwarg rather than crash on the request.
        """
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _client(
            _synth_app(create_response_model=_SynthResponse),
            tasks_api,
            regular_user,
            inventory_api=_make_inventory_api(),
        )

        response = client.post(
            f"{_BASE}/",
            json={"task_name": "new-task", "service_id": 1, "host": _EXECUTOR_HOST},
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_create_422_on_invalid_body(self, regular_user: CasdoorUser) -> None:
        """Assert a body missing required fields 422s before any upstream POST."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _client(
            _synth_app(), tasks_api, regular_user, inventory_api=_make_inventory_api()
        )

        response = client.post(f"{_BASE}/", json={"task_name": "new-task"})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        tasks_api.post.assert_not_awaited()

    def test_create_attaches_connectivity_warning(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Assert ``connectivity_check`` attaches the probe warning to the response."""
        warning = ConnectivityWarning(
            target="db-host", service_type="mysql", message="unreachable"
        )
        mocker.patch(
            "app.extensions.apps.framework.connectivity.record_connectivity_warning",
            new_callable=AsyncMock,
            return_value=warning,
        )
        created = _task_dict(
            "new-task",
            meta={
                "target": "db-host",
                CONNECTIVITY_META_HOST_KEY: "db-host",
                CONNECTIVITY_META_PORT_KEY: 3306,
                CONNECTIVITY_META_SERVICE_TYPE_KEY: "mysql",
            },
        )
        tasks_api = _make_tasks_api(created_task=created)
        client = _client(
            _synth_app(connectivity_check=True),
            tasks_api,
            regular_user,
            inventory_api=_make_inventory_api(),
        )

        response = client.post(
            f"{_BASE}/",
            json={"task_name": "new-task", "service_id": 1, "host": _EXECUTOR_HOST},
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] == {
            "target": "db-host",
            "service_type": "mysql",
            "message": "unreachable",
            "task_history_id": None,
        }


class TestCreateBodyEncoding:
    """Cover the JSON-default / Form-encoded create body encoding knob."""

    def _create_body_content_types(
        self, app_def: TaskExecutionApp, user: CasdoorUser
    ) -> set[str]:
        """Return the ``POST /`` requestBody content-type keys from the OpenAPI."""
        client = _client(
            app_def, _make_tasks_api(), user, inventory_api=_make_inventory_api()
        )
        request_body = client.app.openapi()["paths"][f"{_BASE}/"]["post"]["requestBody"]
        return set(request_body["content"])

    def test_default_create_uses_json_body(self, regular_user: CasdoorUser) -> None:
        """Assert the derived create route defaults to an ``application/json`` body."""
        assert self._create_body_content_types(_synth_app(), regular_user) == {
            "application/json"
        }

    def test_form_encoded_create_uses_form_body(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``create_form_encoded=True`` opts the body into form-urlencoded."""
        content = self._create_body_content_types(
            _synth_app(create_form_encoded=True), regular_user
        )
        assert content == {"application/x-www-form-urlencoded"}


class TestExecuteRoute:
    """Exercise the ``POST /{task_name}/execute`` route over HTTP."""

    def test_execute_returns_201(self, regular_user: CasdoorUser) -> None:
        """Assert execute dispatches upstream and returns task name + id."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t-1"), created_task=_execute_response("t-1")
        )
        client = _client(_synth_app(), tasks_api, regular_user)

        response = client.post(f"{_BASE}/t-1/execute", json={"note": "go"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {
            "task_name": "t-1",
            "task_id": 99,
            "status": EXECUTE_STATUS.value,
            "created_at": EXECUTE_CREATED_AT,
        }


class TestVerbGating:
    """Cover that capability flags gate which verbs are reachable over HTTP."""

    def test_read_only_app_blocks_create_and_execute(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a read-only app 404/405s create and execute but serves reads."""
        app_def = _synth_app(
            capabilities=AppCapabilities(create=False, execute=False),
            create_extra_deps=(),
        )
        tasks_api = _make_tasks_api(detail_task=_task_dict("t-1"))
        client = _client(app_def, tasks_api, regular_user)

        create = client.post(f"{_BASE}/", data={"task_name": "t", "service_id": 1})
        execute = client.post(f"{_BASE}/t-1/execute", json={})
        detail = client.get(f"{_BASE}/t-1")

        assert create.status_code in (
            status.HTTP_404_NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        assert execute.status_code in (
            status.HTTP_404_NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        assert detail.status_code == status.HTTP_200_OK


class TestExtraRoutePrecedence:
    """Cover that extra routes never shadow a derived route."""

    def test_extra_route_does_not_shadow_derived_detail(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a fixed extra ``GET`` loses to the greedy derived detail route.

        A custom ``GET /{task_name}`` is rejected at construction (see the
        detail-suppress validator), so the precedence is demonstrated with a fixed
        ``GET /ping`` extra route that the greedy detail still captures.
        """
        colliding = APIRouter()

        @colliding.get("/ping", dependencies=[IsApiAuthenticated])
        async def _shadow() -> dict[str, str]:
            return {"shadowed": "ping"}

        tasks_api = _make_tasks_api(detail_task=_task_dict("ping"))
        client = _client(_synth_app(extra_routes=(colliding,)), tasks_api, regular_user)

        response = client.get(f"{_BASE}/ping")

        assert response.status_code == status.HTTP_200_OK
        assert "shadowed" not in response.json()
        assert response.json()["name"] == "ping"


class TestListQuerySpecValidation:
    """Cover the construction-time guards on the server-side sort/search wiring.

    ``list_query_spec`` on the app and the source's query knobs are two halves of one
    capability, and the derived route silently exposes nothing when they disagree — so
    every inconsistent pairing has to be rejected at import rather than at request time.
    """

    def test_non_spec_value_raises(self) -> None:
        """Reject a ``list_query_spec`` that is not a ``ListQuerySpec``."""
        with pytest.raises(ValueError, match="must be a ListQuerySpec"):
            synth_script_app(list_query_spec={"sortable": ["filename"]})

    def test_spec_without_script_source_raises(self) -> None:
        """Reject a spec on a model-first app, which derives its own list route."""
        with pytest.raises(ValueError, match="backs only the derived script list"):
            _synth_app(list_query_spec=_SCRIPT_LIST_SPEC)

    def test_spec_under_no_pagination_raises(self) -> None:
        """Reject a spec on an unpaginated list, which exposes no query params."""
        with pytest.raises(ValueError, match="NO_PAGINATION"):
            synth_script_app(
                list_query_spec=_SCRIPT_LIST_SPEC, pagination=NO_PAGINATION
            )

    def test_source_resolving_query_without_spec_raises(self) -> None:
        """Reject a source that resolves a query while the app declares no spec."""
        with pytest.raises(ValueError, match="set list_query_spec or drop"):
            synth_script_app(
                script_source=_script_source_with_in_memory_query(),
            )

    def test_source_with_list_query_dep_without_spec_raises(self) -> None:
        """Reject the other half of the same pairing: a filter dep with no spec.

        ``list_query_dep`` and ``in_memory_list_query`` are separate ways for a source
        to resolve a query, and a source composing its own filter dependency is the
        shape snippets ships — so it must trip the guard on its own.
        """
        source = replace(
            synth_script_app().script_source,
            list_query_dep=lambda: None,
        )

        with pytest.raises(ValueError, match="set list_query_spec or drop"):
            synth_script_app(script_source=source)

    def test_consistent_pair_is_accepted(self) -> None:
        """Accept a source and app that agree, so the guards are not over-broad."""
        app = synth_script_app(
            script_source=_script_source_with_in_memory_query(),
            list_query_spec=_SCRIPT_LIST_SPEC,
        )

        assert app.list_query_spec is _SCRIPT_LIST_SPEC


class _ComputedListResponse(BaseModel):
    """Represent a list response whose dump includes a computed field."""

    name: str

    @computed_field(alias="wire_label")
    @property
    def label(self) -> str:
        """Return a derived label under its serialized alias."""
        return self.name.upper()


def _computed_list_builder(
    task: Task,
    *,
    status: object = None,
    context: dict | None = None,
) -> _ComputedListResponse:
    """Build the computed-list response so the list route matches the gate."""
    return _ComputedListResponse(name=task.name)


class TestDefinitionValidation:
    """Cover the construction-time guards on the definition."""

    def test_both_schema_sources_raises(self) -> None:
        """Assert setting both ``create_model`` and ``schema=`` is rejected."""
        with pytest.raises(ValueError, match="create_model"):
            _synth_app(schema=_PASSTHROUGH_SCHEMA)

    def test_no_schema_source_raises(self) -> None:
        """Assert setting neither ``create_model`` nor ``schema=`` is rejected."""
        with pytest.raises(ValueError, match="schema"):
            _synth_app(create_model=None, payload_builder=None, task_spec_builder=None)

    def test_schema_passthrough_with_spec_builder_raises(self) -> None:
        """Assert a ``schema=`` app with a ``task_spec_builder`` is rejected."""
        with pytest.raises(ValueError, match="payload_builder"):
            _synth_app(create_model=None, schema=_PASSTHROUGH_SCHEMA)

    def test_non_default_detail_path_without_custom_dep_raises(self) -> None:
        """Assert a non-default ``detail_path_param`` needs a custom task dep."""
        with pytest.raises(ValueError, match="detail_path_param"):
            _synth_app(detail_path_param="name")

    def test_missing_response_model_raises(self) -> None:
        """Assert a definition without a ``response_model`` is rejected."""
        with pytest.raises(ValueError, match="response_model"):
            _synth_app(response_model=None)

    def test_create_model_without_task_name_raises(self) -> None:
        """Assert a ``create_model`` lacking a ``task_name`` field is rejected."""
        with pytest.raises(ValueError, match="task_name"):
            _synth_app(create_model=_NoTaskNameForm)

    def test_create_model_without_layout_raises(self) -> None:
        """Assert a ``create_model`` app without ``views.layout`` is rejected."""
        with pytest.raises(ValueError, match="layout"):
            _synth_app(views=Views(list_view=_LIST_VIEW))

    def test_two_check_connectivity_services_raises(self) -> None:
        """Reject a create_model declaring more than one check_connectivity service."""
        with pytest.raises(ValueError, match="check_connectivity"):
            _synth_app(create_model=_TwoMarkedServiceForm)

    def test_two_unmarked_services_without_primary_raises(self) -> None:
        """Reject two unmarked services that leave no determinable primary."""
        with pytest.raises(ValueError, match="primary"):
            _synth_app(create_model=_TwoUnmarkedServiceForm)

    def test_multiple_check_connectivity_service_raises(self) -> None:
        """Reject a multiple=True ServiceRef that also enables the connectivity probe."""
        with pytest.raises(ValueError, match="multiple=True"):
            _synth_app(create_model=_MultiCheckConnectivityForm)

    def test_sole_multiple_service_without_primary_raises(self) -> None:
        """Reject a sole multiple=True ServiceRef that cannot be the connectivity primary."""
        with pytest.raises(
            ValueError, match="multi-value service field cannot resolve"
        ):
            _synth_app(create_model=_SoleMultiServiceForm)

    def test_single_unmarked_service_does_not_probe(self) -> None:
        """Accept a single unmarked service as the sole primary, with no probe."""
        assert _synth_app().connectivity_check is False

    def test_marked_service_enables_probe(self) -> None:
        """Derive ``connectivity_check`` from a ``check_connectivity`` service."""
        assert _synth_app(connectivity_check=True).connectivity_check is True

    def test_primary_marker_designates_without_probe(self) -> None:
        """Accept two services with one ``primary`` designation and no probe."""
        assert (
            _synth_app(create_model=_PrimaryDesignatedServiceForm).connectivity_check
            is False
        )

    def test_primary_and_check_connectivity_conflict_raises(self) -> None:
        """Reject a ``primary`` field conflicting with a ``check_connectivity`` field."""
        with pytest.raises(
            ValueError, match="at most one service is the envelope primary"
        ):
            _synth_app(create_model=_PrimaryAndCheckConflictForm)

    def test_two_primary_designations_raise(self) -> None:
        """Reject a create_model designating more than one ``primary`` service."""
        with pytest.raises(
            ValueError, match="at most one service is the envelope primary"
        ):
            _synth_app(create_model=_TwoPrimaryServiceForm)

    def test_primary_with_multiple_raises(self) -> None:
        """Reject a multiple=True ServiceRef designated the primary."""
        with pytest.raises(ValueError, match="multiple=True"):
            _synth_app(create_model=_PrimaryMultipleForm)

    def test_redundant_primary_and_probe_same_field_allowed(self) -> None:
        """Accept one field marked both ``primary`` and ``check_connectivity``, probing."""
        assert (
            _synth_app(create_model=_RedundantPrimaryProbeForm).connectivity_check
            is True
        )

    def test_list_suppress_without_custom_list_raises(self) -> None:
        """Assert ``list=False`` with no custom ``GET /`` in extra_routes is rejected."""
        with pytest.raises(ValueError, match=re.escape("capabilities.list")):
            _synth_app(capabilities=AppCapabilities(list=False))

    def test_custom_list_route_with_list_enabled_raises(self) -> None:
        """Assert a custom ``GET /`` while ``list`` stays enabled is rejected as shadowed."""
        with pytest.raises(ValueError, match=re.escape("capabilities.list")):
            _synth_app(extra_routes=(_custom_list_router(),))

    def test_list_suppress_with_custom_list_constructs(self) -> None:
        """Assert ``list=False`` plus a custom ``GET /`` constructs and mounts the custom list."""
        app_def = _synth_app(
            capabilities=AppCapabilities(list=False),
            extra_routes=(_custom_list_router(),),
        )

        assert ("/", "GET") in _routes(app_def)

    def test_create_disabled_with_create_response_model_raises(self) -> None:
        """Assert a create-disabled app with create_response_model is rejected."""
        with pytest.raises(ValueError, match="create_response_model"):
            _synth_app(
                capabilities=AppCapabilities(create=False),
                create_response_model=_SynthResponse,
            )

    def test_create_disabled_with_form_encoded_raises(self) -> None:
        """Assert a create-disabled app with create_form_encoded is rejected."""
        with pytest.raises(ValueError, match="create_form_encoded"):
            _synth_app(
                capabilities=AppCapabilities(create=False),
                create_form_encoded=True,
            )

    def test_payload_builder_with_form_encoded_raises(self) -> None:
        """Assert create_form_encoded with a payload_builder is rejected (no-op knob)."""
        with pytest.raises(ValueError, match="create_form_encoded"):
            _synth_app(
                task_spec_builder=None,
                payload_builder=_passthrough_payload_builder,
                create_form_encoded=True,
            )

    def test_unguarded_update_guard_on_non_derived_verb_raises(self) -> None:
        """Reject an ``UNGUARDED`` opt-out when no PUT is derived (update off)."""
        with pytest.raises(ValueError, match="update_guard"):
            _synth_app(update_guard=UNGUARDED)

    def test_unguarded_delete_guard_on_non_derived_verb_raises(self) -> None:
        """Reject an ``UNGUARDED`` opt-out when no DELETE is derived (delete off)."""
        with pytest.raises(ValueError, match="delete_guard"):
            _synth_app(delete_guard=UNGUARDED)

    def test_malformed_guard_value_raises(self) -> None:
        """Reject a guard knob that is neither a ``Depends`` tuple nor ``UNGUARDED``."""
        with pytest.raises(ValidationError):
            _synth_app(update_guard="oops")

    def test_pagination_none_is_rejected(self) -> None:
        """Reject ``pagination=None`` — the public field type no longer accepts it."""
        with pytest.raises(ValidationError):
            _synth_app(pagination=None)

    def test_unguarded_opt_out_on_derived_routes_constructs(self) -> None:
        """Accept ``UNGUARDED`` on a derived PUT/DELETE and still register them."""
        routes = _routes(
            _synth_app(
                capabilities=AppCapabilities(update=True, delete=True),
                update_guard=UNGUARDED,
                delete_guard=UNGUARDED,
            )
        )

        assert ("/{task_name}", "PUT") in routes
        assert ("/{task_name}", "DELETE") in routes

    def test_detail_suppressed_without_custom_detail_raises(self) -> None:
        """Assert ``capabilities.detail=False`` needs a custom detail extra route."""
        with pytest.raises(ValueError, match=re.escape("capabilities.detail")):
            _synth_app(capabilities=AppCapabilities(detail=False))

    def test_detail_enabled_with_custom_detail_route_raises(self) -> None:
        """Assert a custom ``GET /{task_name}`` with derived detail on is rejected."""
        with pytest.raises(ValueError, match="shadowed by the greedy derived detail"):
            _synth_app(extra_routes=(_custom_detail_router(),))

    def test_detail_suppressed_with_detail_model_raises(self) -> None:
        """Assert a detail-builder override is dead config when detail is suppressed."""
        with pytest.raises(ValueError, match="dead config"):
            _synth_app(
                capabilities=AppCapabilities(detail=False),
                extra_routes=(_custom_detail_router(),),
                detail_response_model=_SynthResponse,
            )

    def test_service_type_filter_without_service_type_raises(self) -> None:
        """Assert ``list_filter.service_type`` without a ``service_type`` is rejected."""
        with pytest.raises(ValueError, match="service_type"):
            _synth_app(service_type=None)

    def test_context_provider_without_response_builder_accepted(self) -> None:
        """Assert ``response_context_provider`` without ``response_builder`` now builds.

        The framework default builder consumes the bound context, so an app may
        wire a ``response_context_provider`` while omitting ``response_builder``.
        """
        app_def = _synth_app(response_builder=None)

        assert app_def.api_router is not None

    def test_create_extra_deps_without_create_capability_raises(self) -> None:
        """Assert ``create_extra_deps`` with create disabled is rejected."""
        with pytest.raises(ValueError, match="create_extra_deps"):
            _synth_app(capabilities=AppCapabilities(create=False))

    def test_update_handler_without_update_capability_raises(self) -> None:
        """Assert an ``update_handler`` set with update disabled is rejected."""
        with pytest.raises(ValueError, match="update_handler"):
            _synth_app(update_handler=_update_handler)

    def test_delete_handler_without_delete_capability_raises(self) -> None:
        """Assert a ``delete_handler`` set with delete disabled is rejected."""
        with pytest.raises(ValueError, match="delete_handler"):
            _synth_app(delete_handler=_delete_handler)

    def test_list_view_column_absent_from_serialized_row_raises(self) -> None:
        """Reject a ``list_view`` column key absent from the serialized response row."""
        bad_views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(columns=[Column(key="ghost_field", label="Ghost")]),
        )
        with pytest.raises(ValueError, match="ghost_field"):
            _synth_app(views=bad_views)

    def test_list_view_column_on_excluded_response_field_raises(self) -> None:
        """Reject a ``list_view`` column keyed on a ``Field(exclude=True)`` response field."""
        bad_views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(columns=[Column(key="service_type", label="Service")]),
        )
        with pytest.raises(ValueError, match="service_type"):
            _synth_app(views=bad_views)

    def test_list_view_column_on_aliased_computed_field_constructs(self) -> None:
        """Construct cleanly when a column keys an aliased computed field."""
        views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(
                columns=[
                    Column(key="name", label="Name"),
                    Column(key="wire_label", label="Label"),
                ]
            ),
        )
        app_def = _synth_app(
            response_model=_ComputedListResponse,
            response_builder=_computed_list_builder,
            views=views,
        )
        assert app_def.api_router is not None

    def test_response_builder_return_type_mismatch_raises(self) -> None:
        """Reject a ``response_builder`` whose return type differs from ``response_model``."""
        with pytest.raises(ValueError, match=r"_AltListResponse.*SynthResponse"):
            _synth_app(response_builder=_alt_list_builder)

    def test_response_builder_matching_return_type_constructs(self) -> None:
        """Construct cleanly when ``response_builder`` return type matches ``response_model``."""
        app_def = _synth_app(
            response_builder=_alt_list_builder, response_model=_AltListResponse
        )
        assert app_def.api_router is not None

    def test_response_builder_without_return_annotation_raises_type_error(self) -> None:
        """Assert an unannotated ``response_builder`` still raises ``TypeError``."""
        with pytest.raises(TypeError, match="return type annotation"):
            _synth_app(
                response_builder=_unannotated_list_builder,
                response_model=_AltListResponse,
            )

    def test_create_model_with_malformed_arg_format_raises(self) -> None:
        """Reject a ``create_model`` whose ``ArgFormat`` template has an unsupported placeholder."""
        with pytest.raises(ValueError, match="unsupported placeholder"):
            _synth_app(create_model=_BadArgFormatForm)

    def test_list_view_columns_present_in_serialized_row_construct(self) -> None:
        """Construct cleanly when every ``list_view`` column is in the serialized row."""
        assert _synth_app().api_router is not None

    def test_schema_passthrough_skips_list_view_column_validation(self) -> None:
        """Skip the ``list_view`` column check for a ``schema=`` passthrough app."""
        bad_views = Views(
            list_view=ListView(columns=[Column(key="ghost_field", label="Ghost")]),
        )
        app_def = _synth_app(
            create_model=None,
            schema=_PASSTHROUGH_SCHEMA,
            task_spec_builder=None,
            payload_builder=_passthrough_payload_builder,
            views=bad_views,
        )
        assert app_def.api_router is not None

    def test_list_view_column_with_actions_format_constructs(self) -> None:
        """Construct cleanly when a column uses ACTIONS format with a synthetic key."""
        views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(
                columns=[
                    Column(key="name", label="Name"),
                    Column(
                        key="_actions", label="Actions", format=ColumnFormat.ACTIONS
                    ),
                ]
            ),
        )
        app_def = _synth_app(views=views)
        assert app_def.api_router is not None

    def test_list_view_column_with_schedule_format_constructs(self) -> None:
        """Construct cleanly when a column uses SCHEDULE format with a synthetic key."""
        views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(
                columns=[
                    Column(key="name", label="Name"),
                    Column(
                        key="_schedule", label="Schedule", format=ColumnFormat.SCHEDULE
                    ),
                ]
            ),
        )
        app_def = _synth_app(views=views)
        assert app_def.api_router is not None

    def test_list_view_dotted_key_with_valid_root_constructs(self) -> None:
        """Construct cleanly when a dotted column key's root is a serialized field."""
        views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(
                columns=[
                    Column(key="name", label="Name"),
                    Column(key="status.label", label="Status Label"),
                ]
            ),
        )
        app_def = _synth_app(views=views)
        assert app_def.api_router is not None

    def test_list_view_dotted_key_with_invalid_root_raises(self) -> None:
        """Reject a dotted column key whose root segment is absent from the row."""
        bad_views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(columns=[Column(key="ghost.service", label="Ghost")]),
        )
        with pytest.raises(ValueError, match=r"root: 'ghost'"):
            _synth_app(views=bad_views)

    def test_list_view_indexed_key_resolves_root(self) -> None:
        """Reject an indexed dotted key whose root segment is absent from the row."""
        bad_views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(columns=[Column(key="items[0].name", label="Item")]),
        )
        with pytest.raises(ValueError, match=r"root: 'items'"):
            _synth_app(views=bad_views)

    def test_list_view_indexed_key_with_valid_root_constructs(self) -> None:
        """Construct cleanly when an indexed column key's root is a serialized field."""
        views = Views(
            layout=FormLayout(sections=(SectionLayout(key="main", title="Main"),)),
            list_view=ListView(
                columns=[
                    Column(key="name", label="Name"),
                    Column(key="name[0].label", label="First Name Label"),
                ]
            ),
        )
        app_def = _synth_app(views=views)
        assert app_def.api_router is not None


class _AltListResponse(BaseModel):
    """Represent a distinct list/detail model proving the builder override is used."""

    name: str
    list_flag: bool = True


class _AltDetailResponse(BaseModel):
    """Represent a distinct detail model proving the detail builder override is used."""

    name: str
    detail_flag: bool = True


def _alt_list_builder(
    task: Task,
    *,
    status: object = None,
    context: dict | None = None,
) -> _AltListResponse:
    """Build the alternate list/detail response, accepting a bound context."""
    return _AltListResponse(name=task.name)


def _unannotated_list_builder(
    task: Task,
    *,
    status: object = None,
    context: dict | None = None,
):
    """Stand in as a response builder with no return annotation."""
    return _AltListResponse(name=task.name)


def _alt_detail_builder(
    task: Task,
    *,
    status: object = None,
    context: dict | None = None,
) -> _AltDetailResponse:
    """Build the alternate detail response, accepting a bound context."""
    return _AltDetailResponse(name=task.name)


def _list_query_param_names(app_def: TaskExecutionApp, user: CasdoorUser) -> set[str]:
    """Return the query-parameter names the derived list route exposes."""
    client = _client(
        app_def, _make_tasks_api(), user, inventory_api=_make_inventory_api()
    )
    operation = client.get("/openapi.json").json()["paths"][f"{_BASE}/"]["get"]
    return {param["name"] for param in operation.get("parameters", [])}


def _create_route(app_def: TaskExecutionApp) -> APIRoute:
    """Return the derived ``POST /`` create route of ``app_def``."""
    return next(
        route
        for route in app_def.api_router.routes
        if isinstance(route, APIRoute) and route.path == "/" and "POST" in route.methods
    )


def _detail_route(app_def: TaskExecutionApp) -> APIRoute:
    """Return the derived ``GET /{task_name}`` detail route of ``app_def``."""
    return next(
        route
        for route in app_def.api_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/{task_name}"
        and "GET" in route.methods
    )


def _execute_route(app_def: TaskExecutionApp) -> APIRoute:
    """Return the derived ``POST /{task_name}/execute`` route of ``app_def``."""
    return next(
        route
        for route in app_def.api_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/{task_name}/execute"
        and "POST" in route.methods
    )


class _RemapResponse(BaseModel):
    """Carry the fields the framework default builder stamps and remaps."""

    name: str
    service_type: ServiceTypeEnum | None = None
    created_by: str | None = None
    last_updated_by: str | None = None


class _BaseLikeResponse(BaseModel):
    """Carry ``connectivity_warning`` so it can pin a connectivity-checked create."""

    name: str
    service_type: ServiceTypeEnum | None = None
    created_by: str | None = None
    last_updated_by: str | None = None
    connectivity_warning: ConnectivityWarning | None = None


async def _remap_context_provider() -> dict[str, str]:
    """Return a username map the default builder remaps the user-ids through."""
    return {
        "uid-a": "Alice",
        "uid-b": "Bob",
        str(SERVICE_PRINCIPAL_ID): "Provider account",
    }


def _raw_task_dict() -> dict:
    """Return a created-task payload with distinct creator and updater user-ids."""
    return TaskFactory.build(
        name="t-1",
        owner=_OWNER,
        created_by="uid-a",
        last_updated_by="uid-b",
        data={"meta": {}},
    ).model_dump(mode="json")


class TestDefaultResponseBuilder:
    """Cover the framework default list builder's stamp + username remap."""

    def test_omitted_response_model_uses_base_task_response(self) -> None:
        """Assert omitting ``response_model`` falls back to ``BaseTaskResponse``."""
        kwargs = synth_app_kwargs()
        kwargs.pop("response_model")
        kwargs["response_builder"] = None
        app_def = TaskExecutionApp(**kwargs)

        assert _detail_route(app_def).response_model is BaseTaskResponse

    def test_omitted_context_provider_defaults_to_the_username_map(self) -> None:
        """Assert an app that binds no provider resolves actors through the default."""
        kwargs = synth_app_kwargs()
        kwargs.pop("response_context_provider")

        app_def = TaskExecutionApp(**kwargs)

        assert app_def.response_context_provider is get_username_mapping

    def test_stamps_service_type_and_remaps_usernames_from_context(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the default builder stamps ``service_type`` and remaps usernames."""
        tasks_api = _make_tasks_api(list_items=[_raw_task_dict()])
        app_def = _synth_app(
            response_builder=None,
            response_model=_RemapResponse,
            response_context_provider=_remap_context_provider,
        )
        client = _client(
            app_def, tasks_api, regular_user, inventory_api=_make_inventory_api()
        )

        item = client.get(f"{_BASE}/").json()["items"][0]

        assert item["service_type"] == ServiceTypeEnum.MYSQL.value
        assert item["created_by"] == "Alice"
        assert item["last_updated_by"] == "Bob"

    def test_resolves_service_principal_ahead_of_context(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the default builder gives system labels precedence over context."""
        task = _raw_task_dict()
        task["created_by"] = str(SERVICE_PRINCIPAL_ID)
        task["last_updated_by"] = str(SERVICE_PRINCIPAL_ID)
        tasks_api = _make_tasks_api(list_items=[task])
        app_def = _synth_app(
            response_builder=None,
            response_model=_RemapResponse,
            response_context_provider=_remap_context_provider,
        )
        client = _client(
            app_def, tasks_api, regular_user, inventory_api=_make_inventory_api()
        )

        item = client.get(f"{_BASE}/").json()["items"][0]

        assert item["created_by"] == "Service account"
        assert item["last_updated_by"] == "Service account"

    def test_passes_raw_ids_through_without_context(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the default builder leaves user-ids unmapped without a provider."""
        tasks_api = _make_tasks_api(list_items=[_raw_task_dict()])
        app_def = _synth_app(
            response_builder=None,
            response_model=_RemapResponse,
            response_context_provider=None,
        )
        client = _client(
            app_def, tasks_api, regular_user, inventory_api=_make_inventory_api()
        )

        item = client.get(f"{_BASE}/").json()["items"][0]

        assert item["service_type"] == ServiceTypeEnum.MYSQL.value
        assert item["created_by"] == "uid-a"
        assert item["last_updated_by"] == "uid-b"


class TestDefaultCreateResponseBuilder:
    """Cover the framework default create builder's create-component resolution."""

    def test_default_create_builder_pins_create_to_base_model(self) -> None:
        """Assert a standard app's create route reuses the base response model."""
        app_def = _synth_app(
            response_builder=None,
            response_model=_BaseLikeResponse,
            connectivity_check=True,
        )

        assert _create_route(app_def).response_model is _BaseLikeResponse

    def test_connectivity_base_without_warning_field_auto_derives(self) -> None:
        """Assert a connectivity app whose base lacks the warning field auto-derives."""
        app_def = _synth_app(connectivity_check=True)

        model = _create_route(app_def).response_model

        assert model is not _SynthResponse
        assert "connectivity_warning" in model.model_fields

    def test_detail_override_keeps_create_rendering_like_detail(self) -> None:
        """Assert a detail override still drives the create response model."""
        app_def = _synth_app(detail_response_builder=_alt_detail_builder)

        assert _create_route(app_def).response_model is _AltDetailResponse


class TestDefaultExecuteModels:
    """Cover the framework default execute request/response models."""

    def test_omitted_execute_models_use_framework_defaults(self) -> None:
        """Assert omitting both execute models derives with the framework defaults."""
        app_def = _synth_app(execute_write_model=None, execute_response_model=None)

        assert _execute_route(app_def).response_model is TaskExecutionResponse

    def test_omitted_execute_write_model_documents_default_in_openapi(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the derived execute body references ``TaskExecuteWrite``."""
        app_def = _synth_app(execute_write_model=None, execute_response_model=None)
        client = _client(
            app_def,
            _make_tasks_api(),
            regular_user,
            inventory_api=_make_inventory_api(),
        )

        spec = client.get("/openapi.json").json()
        request_body = spec["paths"][f"{_BASE}/{{task_name}}/execute"]["post"][
            "requestBody"
        ]
        ref = request_body["content"]["application/json"]["schema"]["$ref"]

        assert ref.endswith(f"/{TaskExecuteWrite.__name__}")

    def test_supplied_execute_models_win_over_defaults(self) -> None:
        """Assert an app supplying execute models keeps them over the defaults."""
        app_def = _synth_app()

        assert _execute_route(app_def).response_model is SynthExecuteResponse


class TestResponseAndFilterKnobs:
    """Cover the response-builder, context, list-filter, and extra-dep knobs."""

    def test_status_filter_exposes_status_query_param(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``list_filter.status`` adds a ``status`` query param to the route."""
        names = _list_query_param_names(_synth_app(), regular_user)

        assert "status" in names

    def test_service_type_filter_exposes_service_type_query_param(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``list_filter.service_type`` adds a ``service_type`` query param."""
        names = _list_query_param_names(_synth_app(), regular_user)

        assert "service_type" in names

    def test_no_filters_exposes_no_filter_query_params(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert an app with both filters off exposes neither filter query param."""
        app_def = _synth_app(list_filter=ListFilterConfig())

        names = _list_query_param_names(app_def, regular_user)

        assert "status" not in names
        assert "service_type" not in names

    def test_roots_only_sends_parent_is_null_upstream(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``roots_only`` threads ``parent_is_null=true`` to the upstream list."""
        app_def = _synth_app(list_filter=ListFilterConfig(roots_only=True))
        tasks_api = _make_tasks_api(list_items=[])
        client = _client(app_def, tasks_api, regular_user)

        client.get(f"{_BASE}/")

        assert _list_upstream_params(tasks_api).get("parent_is_null") == "true"

    def test_extra_params_sent_upstream(self, regular_user: CasdoorUser) -> None:
        """Assert ``extra_params`` are threaded verbatim to the upstream list."""
        app_def = _synth_app(
            list_filter=ListFilterConfig(extra_params={"backup_type": "pbm_config"})
        )
        tasks_api = _make_tasks_api(list_items=[])
        client = _client(app_def, tasks_api, regular_user)

        client.get(f"{_BASE}/")

        assert _list_upstream_params(tasks_api).get("backup_type") == "pbm_config"

    def test_detail_suppressed_uses_custom_extra_route(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a suppressed derived detail lets the custom extra route serve ``GET``."""
        app_def = _synth_app(
            capabilities=AppCapabilities(detail=False),
            extra_routes=(_custom_detail_router(),),
        )
        tasks_api = _make_tasks_api(detail_task=_task_dict("t-1"))
        client = _client(app_def, tasks_api, regular_user)

        response = client.get(f"{_BASE}/t-1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"custom_detail": "t-1"}

    def test_response_builder_override_drives_list_model(self) -> None:
        """Assert a ``response_builder`` override supplies the list response model."""
        app_def = _synth_app(
            response_builder=_alt_list_builder, response_model=_AltListResponse
        )
        list_route = next(
            route
            for route in app_def.api_router.routes
            if isinstance(route, APIRoute)
            and route.path == "/"
            and "GET" in route.methods
        )

        assert list_route.response_model == PaginatedResponse[_AltListResponse]

    def test_detail_response_builder_drives_detail_model(self) -> None:
        """Assert a ``detail_response_builder`` supplies the detail response model."""
        app_def = _synth_app(detail_response_builder=_alt_detail_builder)

        assert _detail_route(app_def).response_model is _AltDetailResponse

    def test_create_route_carries_extra_dep(self) -> None:
        """Assert ``create_extra_deps`` rides the derived create route."""
        callables = {dep.dependency for dep in _create_route(_synth_app()).dependencies}

        assert synth_reject_running_task in callables


class TestSchemaPassthrough:
    """Cover the transitional ``schema=`` app with a ``payload_builder``."""

    def test_schema_app_with_payload_builder_serves_passthrough_schema(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a ``schema=`` app derives routes and serves the supplied schema."""
        app_def = _synth_app(
            create_model=None,
            task_spec_builder=None,
            schema=_PASSTHROUGH_SCHEMA,
            payload_builder=_passthrough_payload_builder,
        )

        assert ("/", "POST") in _routes(app_def)
        client = _client(app_def, _make_tasks_api(), regular_user)
        response = client.get(f"{_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "synthetic-app"


_RESTORE_RELATED_APP = RelatedApp(
    app_key="mysql_backups/restore",
    label="Restore",
    route_segment="restores",
)


class TestRelatedAppsKnob:
    """Cover the ``related_apps`` definition knob and schema threading."""

    def test_related_apps_surface_on_get_schema(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``GET /schema`` includes declared ``related_apps`` metadata."""
        app_def = _synth_app(related_apps=(_RESTORE_RELATED_APP,))
        client = _client(app_def, _make_tasks_api(), regular_user)

        response = client.get(f"{_BASE}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["related_apps"] == [
            {
                "app_key": "mysql_backups/restore",
                "label": "Restore",
                "route_segment": "restores",
            },
        ]

    def test_duplicate_route_segments_rejected_at_construction(self) -> None:
        """Reject two ``related_apps`` entries that share a ``route_segment``."""
        with pytest.raises(
            ValueError, match="duplicate related_apps route_segment values"
        ):
            _synth_app(
                related_apps=(
                    _RESTORE_RELATED_APP,
                    RelatedApp(
                        app_key="other/restore",
                        label="Other",
                        route_segment="restores",
                    ),
                ),
            )

    def test_related_apps_rejected_on_schema_passthrough_app(self) -> None:
        """Reject ``related_apps`` on a ``schema=`` passthrough definition."""
        with pytest.raises(ValueError, match="schema= app carries related_apps"):
            _synth_app(
                create_model=None,
                task_spec_builder=None,
                schema=_PASSTHROUGH_SCHEMA,
                payload_builder=_passthrough_payload_builder,
                related_apps=(_RESTORE_RELATED_APP,),
            )


class TestItemDisplayNamesKnob:
    """Cover ``item_display_name``/``item_display_name_plural`` rejection."""

    def test_item_display_names_accepted_on_derived_schema_app(self) -> None:
        """Accept the record names on a plain derived-schema definition."""
        app_def = _synth_app(
            item_display_name="widget", item_display_name_plural="widgets"
        )

        assert app_def.item_display_name == "widget"
        assert app_def.item_display_name_plural == "widgets"

    def test_item_display_names_rejected_on_script_source_app(self) -> None:
        """Reject ``item_display_name`` on a ``script_source`` definition."""
        with pytest.raises(
            ValueError, match="script_source app declares its record names"
        ):
            synth_script_app(item_display_name="widget")

    def test_item_display_names_rejected_on_schema_passthrough_app(self) -> None:
        """Reject ``item_display_name_plural`` on a ``schema=`` passthrough definition."""
        with pytest.raises(ValueError, match="schema= app carries its record names"):
            _synth_app(
                create_model=None,
                task_spec_builder=None,
                schema=_PASSTHROUGH_SCHEMA,
                payload_builder=_passthrough_payload_builder,
                item_display_name_plural="widgets",
            )


class TestRegistryBinding:
    """Cover that binding an activation entry preserves the prebuilt router."""

    def test_display_only_overrides_keep_router_schema_intact(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert display-only overrides leave the derived schema name intact."""
        app_def = _synth_app()
        bound = app_def.model_copy(
            update={
                "key": "synthetic-app",
                "display_name": "Renamed Synthetic",
                "uri_path": "/renamed",
            }
        )
        client = _client(bound, _make_tasks_api(), regular_user)

        response = client.get(f"/api/apps{bound.uri_path}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "synthetic-app"
        assert bound.api_router is app_def.api_router


class TestInheritedBaseAppFields:
    """Cover the fields ``TaskExecutionApp`` inherits from ``BaseApp``."""

    def test_defaults_uses_task_data_true(self) -> None:
        """Carry ``True`` by default: a derived task app always renders task data."""
        assert _synth_app().uses_task_data is True


class TestItemDisplayNames:
    """Cover the record names a ``TaskExecutionApp`` declares for its create form."""

    def test_declared_record_names_reach_the_served_schema(
        self, regular_user: CasdoorUser
    ) -> None:
        """Carry both authoring fields into the derived ``GET /schema`` payload."""
        app_def = _synth_app(
            item_display_name="widget", item_display_name_plural="widgets"
        )
        client = _client(app_def, _make_tasks_api(), regular_user)

        response = client.get(f"/api/apps{app_def.uri_path}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["item_display_name"] == "widget"
        assert response.json()["item_display_name_plural"] == "widgets"

    def test_omitted_record_names_default_from_display_name(
        self, regular_user: CasdoorUser
    ) -> None:
        """Fall back to ``display_name`` for both when the app declares neither."""
        app_def = _synth_app()
        client = _client(app_def, _make_tasks_api(), regular_user)

        response = client.get(f"/api/apps{app_def.uri_path}/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["item_display_name"] == app_def.display_name
        assert response.json()["item_display_name_plural"] == app_def.display_name


class TestOffersScheduling:
    """Cover the served-schema source ``offers_scheduling`` reads its answer from."""

    def test_model_first_app_reads_its_views_capabilities(self) -> None:
        """Answer ``True`` for an app whose derived schema declares ``scheduling``."""
        assert mysql_backups_app.offers_scheduling is True

    def test_model_first_app_withholding_scheduling(self) -> None:
        """Answer ``False`` for a derived-schema app that withholds ``scheduling``."""
        assert mysql_restore_app.offers_scheduling is False

    def test_passthrough_schema_app_reads_the_passthrough(self) -> None:
        """Answer from the ``schema=`` passthrough rather than the views bundle."""
        assert mongo_restore_app.offers_scheduling is False

    def test_app_declaring_no_capabilities_at_all(self) -> None:
        """Answer ``False`` when the served schema carries no capabilities."""
        app_def = _synth_app(
            views=replace(synth_app_kwargs()["views"], capabilities=None)
        )

        assert app_def.offers_scheduling is False

    def test_script_source_app_reads_its_static_schema(self) -> None:
        """Answer from ``script_source.static_schema`` for a script-flavored app."""
        assert snippets_app.offers_scheduling is False

    def test_script_source_static_schema_wins(self) -> None:
        """Follow the static schema, the only source a script app serves."""
        scheduling_schema = SNIPPETS_PLUGIN_SCHEMA.model_copy(
            update={"capabilities": Capabilities(scheduling=True)}
        )
        app_def = snippets_app.model_copy(
            update={
                "script_source": replace(
                    snippets_app.script_source, static_schema=scheduling_schema
                )
            }
        )

        assert app_def.offers_scheduling is True
