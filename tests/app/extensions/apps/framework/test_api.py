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

"""Define tests for the plugin route helpers in ``framework.api``.

Covers the ``/schema`` and ``/capabilities`` discovery endpoints, the
``derive_crud_routes`` CRUD route factory, and the ``derive_execute_route``
execute-route factory.
"""

import functools
import inspect
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Annotated
from unittest.mock import AsyncMock

import aiohttp
import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, status
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import column

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.db.in_memory_list_query import InMemoryListQueryApplier
from app.core.db.list_query import ListQuerySpec
from app.core.exceptions import HTTPBadGatewayException, HTTPConflictException
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import make_pagination_dep
from app.core.requests.remote_api import RemoteAPI
from app.extensions.apps.framework import (
    ConnectivityWarning,
    TaskExecuteWrite,
    TaskExecutionResponse,
)
from app.extensions.apps.framework.api import (
    capabilities_endpoint,
    CascadeCreatePlan,
    derive_cascade_create_route,
    derive_crud_routes,
    derive_execute_route,
    derive_script_routes,
    ListFilters,
    make_list_filter_dep,
    schema_endpoint,
)
from app.extensions.apps.framework.deps import make_task_dep
from app.extensions.apps.framework.list_query import in_memory_list_scripts
from app.extensions.apps.framework.rules import (
    CardinalityRule,
    F,
    FailRule,
    FieldGate,
    truthy,
)
from app.extensions.apps.framework.schema import (
    AppEntitySchema,
    AppSchema,
    BoolField,
    Capabilities,
    Choice,
    ChoiceField,
    Column,
    ColumnFormat,
    DateTimeField,
    DerivedTask,
    DetailField,
    DetailSection,
    DetailView,
    FileField,
    FloatField,
    FormSection,
    HostField,
    IntegerField,
    ListView,
    MultiChoiceField,
    SchemaField,
    ServiceField,
    StringField,
    TableField,
    TextAreaField,
    YamlField,
)
from app.extensions.apps.framework.script_source import ScriptSource
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.connectivity import (
    CONNECTIVITY_META_HOST_KEY,
    CONNECTIVITY_META_PORT_KEY,
    CONNECTIVITY_META_SERVICE_TYPE_KEY,
)
from app.extensions.deps import (
    check_for_conflicted_running_tasks,
    get_current_user,
    get_task_by_name,
    get_tasks_api,
    get_username_mapping,
    IsApiAuthenticated,
    TaskAPI,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import Task, TaskBackendEnum, TaskHistoryStatusEnum, TaskWrite
from tests.app.extensions.apps.framework.kit import EXECUTE_CREATED_AT, EXECUTE_STATUS
from tests.app.factories import GeneratedTaskFactory, TaskFactory

_TEST_SCHEMA = AppSchema(
    name="test-schema-endpoint",
    display_name="Test Schema Endpoint",
    forms=[
        FormSection(
            title="Options",
            fields=[BoolField(name="flag", label="Flag")],
        ),
    ],
    list_view=ListView(columns=[Column(key="id", label="ID")]),
)


_ALL_FIELDS_SCHEMA = AppSchema(
    name="test-all-fields",
    display_name="Test All Fields",
    description="Schema instance exercising every concrete field class.",
    task_type="test-all-fields-task",
    forms=[
        FormSection(
            title="Everything",
            fields=[
                BoolField(name="boolField", label="Bool"),
                ChoiceField(
                    name="choiceField",
                    label="Choice",
                    choices=[Choice(label="A", value="a")],
                ),
                DateTimeField(name="datetimeField", label="DateTime"),
                FileField(name="fileField", label="File"),
                FloatField(name="floatField", label="Float"),
                HostField(name="hostField", label="Host"),
                IntegerField(name="integerField", label="Integer"),
                MultiChoiceField(
                    name="multiChoiceField",
                    label="Multi Choice",
                    choices=[Choice(label="B", value="b")],
                ),
                ServiceField(
                    name="serviceField",
                    label="Service",
                    service_types=[ServiceTypeEnum.MYSQL],
                ),
                SchemaField(
                    name="schemaField",
                    label="Schema",
                    depends_on="serviceField",
                ),
                StringField(name="stringField", label="String"),
                TableField(
                    name="tableField",
                    label="Table",
                    depends_on="schemaField",
                ),
                TextAreaField(name="textareaField", label="TextArea"),
                YamlField(name="yamlField", label="Yaml"),
            ],
        ),
    ],
    capabilities=Capabilities(alert_on_fail=True, scheduling=True),
    list_view=ListView(
        columns=[
            Column(key="id", label="ID"),
            Column(key="status", label="Status", format=ColumnFormat.STATUS),
        ],
    ),
    detail_view=DetailView(
        sections=[
            DetailSection(
                title="Execution",
                fields=[
                    DetailField(path="data.meta.command", label="Command"),
                ],
            ),
        ],
    ),
)


_EMPTY_FORMS_SCHEMA = AppSchema(
    name="test-empty-forms",
    display_name="Test Empty Forms",
    forms=[],
    list_view=ListView(columns=[Column(key="id", label="ID")]),
)


_ENTITIES_SCHEMA = AppSchema(
    name="test-entities",
    display_name="Test Entities",
    entities=[
        AppEntitySchema(
            name="things",
            display_name="Things",
            forms=[
                FormSection(
                    title="Thing",
                    fields=[StringField(name="title", label="Title")],
                ),
            ],
            list_view=ListView(columns=[Column(key="id", label="ID")]),
        ),
    ],
)


def _mount_plugin_router(plugin_router: APIRouter, plugin_prefix: str) -> FastAPI:
    """Mount ``plugin_router`` under the production-shape router tree.

    Mirror ``app/extensions/api/router.py`` exactly: plugin router → plugins router
    (``/apps``) → api router (``/api`` with ``IsApiAuthenticated``) →
    ``FastAPI``. Return a fresh instance on every call so tests never touch
    the real ``extensions_app``.

    :param plugin_router: The plugin's ``APIRouter`` (already carrying its routes).
    :param plugin_prefix: The prefix under which the plugin router is mounted
        on the shared plugins router (for example ``/test-schema-endpoint``).
    :return: A ``FastAPI`` application instance with the composed router tree.
    """
    apps_router = APIRouter(prefix="/apps")
    apps_router.include_router(plugin_router, prefix=plugin_prefix)
    api_router = APIRouter(prefix="/api", dependencies=[IsApiAuthenticated])
    api_router.include_router(apps_router)
    app = FastAPI()
    app.include_router(api_router)
    return app


def _build_composed_app(schema: AppSchema, plugin_prefix: str) -> FastAPI:
    """Build a fresh FastAPI app exposing ``schema_endpoint`` over a schema.

    :param schema: The plugin schema the helper registers on the plugin router.
    :param plugin_prefix: The prefix under which the plugin router is mounted
        on the shared plugins router (for example ``/test-schema-endpoint``).
    :return: A ``FastAPI`` application instance with the composed router tree.
    """
    plugin_router = APIRouter()
    schema_endpoint(plugin_router, schema)
    return _mount_plugin_router(plugin_router, plugin_prefix)


@pytest.fixture
def authed_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed ``TestClient`` over a production-shape composed app."""
    app = _build_composed_app(_TEST_SCHEMA, "/test-schema-endpoint")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def authed_all_fields_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed ``TestClient`` whose schema exercises every field class."""
    app = _build_composed_app(_ALL_FIELDS_SCHEMA, "/test-all-fields")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def authed_entities_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed ``TestClient`` whose schema declares entities."""
    app = _build_composed_app(_ENTITIES_SCHEMA, "/test-entities")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def authed_empty_forms_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed ``TestClient`` whose schema has zero form sections."""
    app = _build_composed_app(_EMPTY_FORMS_SCHEMA, "/test-empty-forms")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def unauthed_client() -> TestClient:
    """Return an unauthed ``TestClient`` over the same composed app."""
    app = _build_composed_app(_TEST_SCHEMA, "/test-schema-endpoint")
    return TestClient(app, raise_server_exceptions=False)


class TestSchemaEndpointRouterComposition:
    """Inspect the router in isolation, without involving HTTP or FastAPI apps."""

    def test_registers_single_get_schema_route(self) -> None:
        """Assert the helper registers exactly one ``GET /schema`` route."""
        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)

        schema_routes = [
            r for r in router.routes if isinstance(r, APIRoute) and r.path == "/schema"
        ]
        assert len(schema_routes) == 1
        assert schema_routes[0].methods == {"GET"}

    def test_route_declares_api_authenticated(self) -> None:
        """Assert the route declares ``IsApiAuthenticated`` as a per-route dep."""
        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)

        [route] = [r for r in router.routes if isinstance(r, APIRoute)]
        callables = {d.dependency for d in route.dependencies}
        assert get_current_user in callables

    def test_route_declares_response_model(self) -> None:
        """Assert the route wires ``response_model=AppSchema`` for OpenAPI."""
        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)

        [route] = [r for r in router.routes if isinstance(r, APIRoute)]
        assert route.response_model is AppSchema

    def test_route_response_model_emits_by_alias(self) -> None:
        """Assert the route pins ``response_model_by_alias=True`` explicitly."""
        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)

        [route] = [r for r in router.routes if isinstance(r, APIRoute)]
        assert route.response_model_by_alias is True

    def test_second_call_raises_value_error(self) -> None:
        """Assert calling the helper twice on the same router raises."""
        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)

        with pytest.raises(ValueError, match="schema_endpoint"):
            schema_endpoint(router, _TEST_SCHEMA)

    def test_second_call_raises_value_error_on_prefixed_router(self) -> None:
        """Assert the duplicate guard works when the router carries a prefix.

        A prefix-bearing ``APIRouter`` records the route path as
        ``{prefix}/schema``, so the guard must compare against the router's
        effective path — not the bare ``/schema`` literal — or a second
        registration would slip through silently.
        """
        router = APIRouter(prefix="/plugin-prefix")
        schema_endpoint(router, _TEST_SCHEMA)

        with pytest.raises(ValueError, match="schema_endpoint"):
            schema_endpoint(router, _TEST_SCHEMA)

    def test_naked_app_unauth_returns_401(self) -> None:
        """Assert the per-route dep gates the route outside ``api_router``.

        Mount the plugin router on a naked ``FastAPI`` with no router-level
        auth, then confirm an unauthenticated GET still returns 401.
        """
        plugin_router = APIRouter()
        schema_endpoint(plugin_router, _TEST_SCHEMA)
        app = FastAPI()
        app.include_router(plugin_router)

        response = TestClient(app, raise_server_exceptions=False).get(
            "/schema", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")

    def test_duplicate_dep_runs_once_per_request(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert FastAPI deduplicates the router-level + per-route dep per request.

        Compose the production-shape router tree with ``IsApiAuthenticated``
        declared at both router level and (via the helper) route level, then
        spy on ``get_current_user`` and confirm one authed request
        invokes it exactly once — the behaviour that makes the belt-and-
        braces deviation free.
        """
        call_count = {"n": 0}

        def spy() -> CasdoorUser:
            call_count["n"] += 1
            return regular_user

        app = _build_composed_app(_TEST_SCHEMA, "/test-schema-endpoint")
        app.dependency_overrides[get_current_user] = spy
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/apps/test-schema-endpoint/schema")
        assert response.status_code == status.HTTP_200_OK
        assert call_count["n"] == 1


class TestSchemaEndpointAuthenticated:
    """Exercise the helper over the production-shape composed router tree."""

    def test_authed_get_returns_serialised_schema(
        self, authed_client: TestClient
    ) -> None:
        """Assert an authed GET returns the full serialised schema payload."""
        response = authed_client.get("/api/apps/test-schema-endpoint/schema")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == _TEST_SCHEMA.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    def test_response_content_type_is_json(self, authed_client: TestClient) -> None:
        """Assert the response carries a JSON ``content-type``."""
        response = authed_client.get("/api/apps/test-schema-endpoint/schema")

        assert response.headers["content-type"].startswith("application/json")

    def test_response_uses_snake_case_keys(self, authed_client: TestClient) -> None:
        """Assert the response emits snake_case wire keys (not camelCase)."""
        body = authed_client.get("/api/apps/test-schema-endpoint/schema").json()

        assert "display_name" in body
        assert "list_view" in body
        assert "displayName" not in body
        assert "listView" not in body

    def test_field_discriminator_key_is_type(self, authed_client: TestClient) -> None:
        """Assert each field carries ``type`` (not ``field_type``) as its discriminator."""
        body = authed_client.get("/api/apps/test-schema-endpoint/schema").json()

        assert body["forms"][0]["fields"][0]["type"] == "bool"
        assert "field_type" not in body["forms"][0]["fields"][0]

    def test_openapi_documents_response_schema(self, authed_client: TestClient) -> None:
        """Assert the OpenAPI spec documents the ``AppSchema`` response."""
        openapi = authed_client.get("/openapi.json").json()

        path = openapi["paths"]["/api/apps/test-schema-endpoint/schema"]
        response_ref = path["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        schema_name = response_ref.rsplit("/", 1)[-1]
        resolved = openapi["components"]["schemas"][schema_name]
        property_keys = set(resolved["properties"].keys())

        assert {"display_name", "list_view", "forms"} <= property_keys

    def test_openapi_documents_detail_view(self, authed_client: TestClient) -> None:
        """Assert OpenAPI surfaces ``AppSchema.detail_view`` + DetailView models.

        Regression guard: ``detail_view`` must remain visible to the generated
        TypeScript client. If the field is renamed or dropped without updating
        the frontend codegen, this test fails before the generated types go
        stale.
        """
        openapi = authed_client.get("/openapi.json").json()

        components = openapi["components"]["schemas"]
        plugin_schema = next(
            v for k, v in components.items() if k.endswith("AppSchema")
        )
        assert "detail_view" in plugin_schema["properties"]

        for name in ("DetailView", "DetailSection", "DetailField"):
            assert any(k.endswith(name) for k in components), (
                f"{name!r} not present in OpenAPI components"
            )

    def test_empty_forms_serialises_as_empty_list(
        self, authed_empty_forms_client: TestClient
    ) -> None:
        """Assert a schema with no form sections round-trips as ``forms: []``."""
        body = authed_empty_forms_client.get("/api/apps/test-empty-forms/schema").json()

        assert body["forms"] == []


class TestSchemaEndpointUnauthenticated:
    """Assert unauthenticated access returns JSON 401 with no redirect."""

    def test_unauthed_get_returns_401_json(self, unauthed_client: TestClient) -> None:
        """Assert unauthenticated GET returns 401 JSON, not a 303 redirect."""
        response = unauthed_client.get(
            "/api/apps/test-schema-endpoint/schema",
            follow_redirects=False,
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()

    def test_unauthed_response_has_no_location_header(
        self, unauthed_client: TestClient
    ) -> None:
        """Assert the 401 response does not carry a ``Location`` redirect header."""
        response = unauthed_client.get(
            "/api/apps/test-schema-endpoint/schema",
            follow_redirects=False,
        )

        assert "location" not in {k.lower() for k in response.headers}


class TestSchemaEndpointTaskStatuses:
    """Cover how ``task_statuses`` reaches the ``GET /schema`` payload."""

    def test_task_style_schema_publishes_the_vocabulary(
        self, authed_all_fields_client: TestClient
    ) -> None:
        """Assert a task-style plugin serves one entry per status value."""
        body = authed_all_fields_client.get("/api/apps/test-all-fields/schema").json()

        assert body["task_statuses"] == [
            {
                "value": status.value,
                "terminal": status.is_terminal(),
                "output_available": status.is_finished(),
            }
            for status in TaskHistoryStatusEnum
        ]

    def test_entity_schema_omits_the_vocabulary(
        self, authed_entities_client: TestClient
    ) -> None:
        """Assert an entity-declaring plugin serves no ``task_statuses`` key."""
        body = authed_entities_client.get("/api/apps/test-entities/schema").json()

        assert body["entities"]
        assert "task_statuses" not in body


class TestSchemaEndpointAllFieldsRoundTrip:
    """Round-trip a schema containing every concrete field class."""

    def test_all_fields_schema_reparses_losslessly(
        self, authed_all_fields_client: TestClient
    ) -> None:
        """Assert the emitted JSON re-validates back into the original schema."""
        body = authed_all_fields_client.get("/api/apps/test-all-fields/schema").json()

        reparsed = AppSchema.model_validate(body)
        assert reparsed == _ALL_FIELDS_SCHEMA


# ── Conditional-rule primitives wire-shape regression (SEP-1071) ────────


_CONDITIONAL_RULES_SCHEMA = AppSchema(
    name="test-conditional-rules",
    display_name="Test Conditional Rules",
    forms=[
        FormSection(
            title="Recursion",
            fields=[
                StringField(name="recursion_method", label="Method"),
                StringField(
                    name="dsn_table",
                    label="DSN Table",
                    requires=[
                        FieldGate(when=F("recursion_method") == "dsn"),
                    ],
                    forbidden=[
                        FieldGate(when=F("recursion_method") == "none"),
                    ],
                ),
            ],
            cardinality_rules=[
                CardinalityRule(
                    when=truthy("recursion_method"),
                    fields=["dsn_table"],
                    max=1,
                    message="at most one",
                ),
            ],
        ),
    ],
    list_view=ListView(columns=[Column(key="id", label="ID")]),
    fail_when=[
        FailRule(
            fail_when=truthy("recursion_method"),
            error_fields=["recursion_method"],
            message="must not be set",
        ),
    ],
)


@pytest.fixture
def authed_conditional_rules_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed client whose schema exercises the new rule primitives."""
    app = _build_composed_app(_CONDITIONAL_RULES_SCHEMA, "/test-conditional-rules")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


class TestConditionalRulePrimitivesWireShape:
    """Verify the new primitive keys serialize correctly over HTTP."""

    def test_response_carries_snake_case_primitive_keys(
        self, authed_conditional_rules_client: TestClient
    ) -> None:
        """Hit the live route and confirm snake_case primitive keys appear."""
        body = authed_conditional_rules_client.get(
            "/api/apps/test-conditional-rules/schema"
        ).json()

        # AppSchema-scope primitive
        assert "fail_when" in body
        assert isinstance(body["fail_when"], list)

        # FormSection-scope primitive
        section = body["forms"][0]
        assert "cardinality_rules" in section
        assert isinstance(section["cardinality_rules"], list)

        # BaseField-scope primitives
        dsn_table = section["fields"][1]
        assert dsn_table["name"] == "dsn_table"
        assert "requires" in dsn_table
        assert "forbidden" in dsn_table

    def test_predicate_serializes_via_to_dict(
        self, authed_conditional_rules_client: TestClient
    ) -> None:
        """Predicate serialises via to_dict."""
        body = authed_conditional_rules_client.get(
            "/api/apps/test-conditional-rules/schema"
        ).json()

        gate = body["forms"][0]["fields"][1]["requires"][0]
        assert gate["when"] == {"equals": {"recursion_method": "dsn"}}

        rule = body["forms"][0]["cardinality_rules"][0]
        assert rule["when"] == {"truthy": "recursion_method"}
        assert rule["fields"] == ["dsn_table"]
        assert rule["max"] == 1


class TestSchemaResponseExcludeNone:
    """Verify ``response_model_exclude_none=True`` keeps unused keys absent."""

    def test_no_new_primitive_keys_when_unused(
        self, authed_all_fields_client: TestClient
    ) -> None:
        """A schema without conditional rules emits no rule-primitive keys."""
        body = authed_all_fields_client.get("/api/apps/test-all-fields/schema").json()

        for forbidden_key in (
            "requires",
            "forbidden",
            "cardinality_rules",
            "fail_when",
        ):
            assert forbidden_key not in body, f"{forbidden_key!r} leaked at top level"

        for section in body["forms"]:
            for forbidden_key in ("cardinality_rules", "fail_when"):
                assert forbidden_key not in section, (
                    f"{forbidden_key!r} leaked into a section"
                )
            for field in section["fields"]:
                for forbidden_key in ("requires", "forbidden"):
                    assert forbidden_key not in field, (
                        f"{forbidden_key!r} leaked into field {field['name']!r}"
                    )


# ── DerivedTask cascade primitive wire-shape regression (SEP-1074) ──────


_DERIVED_SCHEMA = AppSchema(
    name="test-derived",
    display_name="Test Derived",
    forms=[
        FormSection(
            title="Options",
            fields=[BoolField(name="flag", label="Flag")],
        ),
    ],
    list_view=ListView(columns=[Column(key="id", label="ID")]),
    derived=[
        DerivedTask(
            name_suffix="-dry-run",
            arg_substitutions={"--execute": "--dry-run"},
            parent_link=True,
        ),
    ],
)


@pytest.fixture
def authed_derived_client(regular_user: CasdoorUser) -> TestClient:
    """Return an authed client whose schema exercises the ``derived`` field."""
    app = _build_composed_app(_DERIVED_SCHEMA, "/test-derived")
    app.dependency_overrides[get_current_user] = lambda: regular_user
    return TestClient(app, raise_server_exceptions=False)


class TestDerivedFieldWireShape:
    """Verify the ``derived`` key behaves correctly on the live response."""

    def test_response_excludes_derived_when_none(
        self, authed_client: TestClient
    ) -> None:
        """Verify ``derived`` is absent when the schema does not set it (BC guard)."""
        body = authed_client.get("/api/apps/test-schema-endpoint/schema").json()

        assert "derived" not in body

    def test_response_includes_derived_when_set(
        self, authed_derived_client: TestClient
    ) -> None:
        """Verify ``derived`` serialises in snake_case when the schema sets it."""
        body = authed_derived_client.get("/api/apps/test-derived/schema").json()

        assert "derived" in body
        assert isinstance(body["derived"], list)
        assert len(body["derived"]) == 1
        entry = body["derived"][0]
        assert entry["name_suffix"] == "-dry-run"
        assert entry["arg_substitutions"] == {"--execute": "--dry-run"}
        assert entry["parent_link"] is True


# ── capabilities_endpoint() helper (SEP-1133) ───────────────────────────


class _DummyCapabilities(BaseModel):
    """Tiny capability response model used by the helper tests."""

    flag: bool


_provider_state: dict[str, bool] = {"flag": False}


def _stateful_provider() -> _DummyCapabilities:
    """Read from a module-level dict so a test can mutate it between requests."""
    return _DummyCapabilities(flag=_provider_state["flag"])


def _build_capabilities_app(
    provider, plugin_prefix: str = "/test-capabilities-endpoint"
) -> FastAPI:
    """Build a production-shape composed app exposing ``GET /capabilities``."""
    plugin_router = APIRouter()
    capabilities_endpoint(plugin_router, capabilities_provider=provider)
    return _mount_plugin_router(plugin_router, plugin_prefix)


class TestCapabilitiesEndpointRegistration:
    """Inspect the router and the fail-fast guards in isolation."""

    def test_registers_single_get_capabilities_route(self) -> None:
        """Assert the helper registers exactly one ``GET /capabilities`` route."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=provider)

        routes = [
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/capabilities"
        ]
        assert len(routes) == 1
        assert routes[0].methods == {"GET"}

    def test_route_declares_api_authenticated(self) -> None:
        """Assert the route declares ``IsApiAuthenticated`` as a per-route dependency."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=provider)

        [route] = [
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/capabilities"
        ]
        callables = {d.dependency for d in route.dependencies}
        assert get_current_user in callables

    def test_response_model_inferred_from_return_annotation(self) -> None:
        """Assert the route's ``response_model`` matches the provider's return annotation."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=provider)

        [route] = [
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/capabilities"
        ]
        assert route.response_model is _DummyCapabilities

    def test_second_call_raises_value_error(self) -> None:
        """Assert calling the helper twice on same router raises ``ValueError``."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=provider)

        with pytest.raises(ValueError, match="capabilities_endpoint"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_second_call_raises_value_error_on_prefixed_router(self) -> None:
        """Assert the duplicate guard works when the router carries a prefix."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter(prefix="/plugin-prefix")
        capabilities_endpoint(router, capabilities_provider=provider)

        with pytest.raises(ValueError, match="capabilities_endpoint"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_coexists_with_schema_endpoint_on_same_router(self) -> None:
        """Assert schema and capabilities helpers do not collide on the same router."""

        def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        schema_endpoint(router, _TEST_SCHEMA)
        capabilities_endpoint(router, capabilities_provider=provider)

        paths = {r.path for r in router.routes if isinstance(r, APIRoute)}
        assert "/schema" in paths
        assert "/capabilities" in paths

    def test_missing_return_annotation_raises_type_error(self) -> None:
        """Assert a provider without a return annotation fails at registration."""

        def provider():  # no return annotation
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        with pytest.raises(TypeError, match="return type annotation"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_non_class_return_annotation_raises_type_error(self) -> None:
        """Assert a provider whose return annotation isn't a class fails."""

        def provider() -> int | None:
            return None

        router = APIRouter()
        with pytest.raises(TypeError, match="BaseModel"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_non_basemodel_class_return_annotation_raises_type_error(self) -> None:
        """Assert a provider returning a non-BaseModel class fails."""

        def provider() -> dict:
            return {}

        router = APIRouter()
        with pytest.raises(TypeError, match="BaseModel"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_dataclass_return_annotation_raises_type_error(self) -> None:
        """Assert a dataclass return annotation is not a BaseModel subclass — fails."""

        @dataclass
        class _NotAModel:
            flag: bool

        def provider() -> _NotAModel:
            return _NotAModel(flag=True)

        router = APIRouter()
        with pytest.raises(TypeError, match="BaseModel"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_async_provider_raises_type_error(self) -> None:
        """Assert an ``async def`` provider is rejected at registration.

        Calling an async function from the sync handler would return a
        coroutine object, which response_model serialisation cannot
        handle. Async / DB-backed providers are explicitly out of scope;
        the guard prevents the silent-500-at-first-request footgun and
        keeps the contract fail-fast.
        """

        async def provider() -> _DummyCapabilities:
            return _DummyCapabilities(flag=True)

        router = APIRouter()
        with pytest.raises(TypeError, match="sync callable"):
            capabilities_endpoint(router, capabilities_provider=provider)

    def test_future_annotations_provider_resolved(self) -> None:
        """Assert a provider whose return annotation is stringized.

        ``from __future__ import annotations`` stores every annotation
        as a string; the helper must resolve it back to the real class
        via ``typing.get_type_hints`` instead of failing the BaseModel
        guard with a misleading error.
        """
        import textwrap  # noqa: PLC0415
        import types  # noqa: PLC0415

        module = types.ModuleType("_sep1133_future_annotations_probe")
        module.__dict__["_DummyCapabilities"] = _DummyCapabilities
        exec(
            textwrap.dedent(
                """
                from __future__ import annotations

                def provider() -> _DummyCapabilities:
                    return _DummyCapabilities(flag=True)
                """
            ),
            module.__dict__,
        )

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=module.provider)

        [route] = [
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/capabilities"
        ]
        assert route.response_model is _DummyCapabilities

    def test_plain_basemodel_subclass_accepted(self) -> None:
        """Assert any direct BaseModel subclass is accepted as the response type."""

        class _RawModel(BaseModel):
            flag: bool

        def provider() -> _RawModel:
            return _RawModel(flag=True)

        router = APIRouter()
        capabilities_endpoint(router, capabilities_provider=provider)
        [route] = [
            r
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/capabilities"
        ]
        assert route.response_model is _RawModel


class TestCapabilitiesEndpointAuthenticated:
    """Exercise the helper over the production-shape composed router tree."""

    @pytest.fixture
    def authed_capabilities_client(self, regular_user: CasdoorUser) -> TestClient:
        """Return an authed ``TestClient`` over the capabilities-app."""
        _provider_state["flag"] = True
        app = _build_capabilities_app(_stateful_provider)
        app.dependency_overrides[get_current_user] = lambda: regular_user
        return TestClient(app, raise_server_exceptions=False)

    def test_authed_get_returns_provider_payload(
        self, authed_capabilities_client: TestClient
    ) -> None:
        """Assert an authed GET returns the provider's payload as JSON."""
        _provider_state["flag"] = True
        response = authed_capabilities_client.get(
            "/api/apps/test-capabilities-endpoint/capabilities"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"flag": True}
        assert response.headers["content-type"].startswith("application/json")

    def test_response_body_matches_pydantic_dump(
        self, authed_capabilities_client: TestClient
    ) -> None:
        """Assert the response equals the provider's pydantic dump."""
        _provider_state["flag"] = False
        body = authed_capabilities_client.get(
            "/api/apps/test-capabilities-endpoint/capabilities"
        ).json()

        assert body == _DummyCapabilities(flag=False).model_dump(mode="json")

    def test_provider_invoked_per_request(
        self, authed_capabilities_client: TestClient
    ) -> None:
        """Assert mutating provider state between requests is reflected live.

        Critical for hot-reload semantics: the helper must not cache
        the provider's return value across requests, or deployment-
        config changes would require a restart to take effect.
        """
        _provider_state["flag"] = False
        first = authed_capabilities_client.get(
            "/api/apps/test-capabilities-endpoint/capabilities"
        )
        _provider_state["flag"] = True
        second = authed_capabilities_client.get(
            "/api/apps/test-capabilities-endpoint/capabilities"
        )

        assert first.json() == {"flag": False}
        assert second.json() == {"flag": True}


class TestCapabilitiesEndpointUnauthenticated:
    """Assert unauthenticated access returns JSON 401 with no redirect."""

    @pytest.fixture
    def unauthed_capabilities_client(self) -> TestClient:
        """Return an unauthed ``TestClient`` over the same composed app."""
        app = _build_capabilities_app(_stateful_provider)
        return TestClient(app, raise_server_exceptions=False)

    def test_unauthed_get_returns_401_json(
        self, unauthed_capabilities_client: TestClient
    ) -> None:
        """Assert unauthenticated GET returns 401 JSON, not a 303 redirect."""
        response = unauthed_capabilities_client.get(
            "/api/apps/test-capabilities-endpoint/capabilities",
            follow_redirects=False,
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()

    def test_naked_app_unauth_returns_401(self) -> None:
        """Assert the per-route dep gates the route even outside ``api_router``."""
        plugin_router = APIRouter()
        capabilities_endpoint(plugin_router, capabilities_provider=_stateful_provider)
        app = FastAPI()
        app.include_router(plugin_router)

        response = TestClient(app, raise_server_exceptions=False).get(
            "/capabilities", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


class TestCapabilitiesEndpointOpenApi:
    """Assert the capabilities route documents itself in the OpenAPI spec."""

    def test_openapi_documents_capabilities_response(
        self, regular_user: CasdoorUser
    ) -> None:
        """``/openapi.json`` references the provider's response model.

        Parity with ``test_openapi_documents_response_schema`` for
        ``schema_endpoint``. The FE generator (``pnpm --filter @pmm-extensions/api
        generate``) consumes this spec; a missing schema there would
        produce an untyped client.
        """
        app = _build_capabilities_app(_stateful_provider)
        app.dependency_overrides[get_current_user] = lambda: regular_user
        client = TestClient(app, raise_server_exceptions=False)

        openapi = client.get("/openapi.json").json()

        path = openapi["paths"]["/api/apps/test-capabilities-endpoint/capabilities"]
        response_ref = path["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        schema_name = response_ref.rsplit("/", 1)[-1]
        resolved = openapi["components"]["schemas"][schema_name]
        assert "flag" in resolved["properties"]


class TestCapabilitiesEndpointRuntime:
    """Assert request-time failure modes are surfaced, not silently swallowed."""

    def test_provider_exception_returns_500(self, regular_user: CasdoorUser) -> None:
        """Assert a provider raising at request time propagates to a 500.

        Pins the documented "out of scope to swallow" contract: provider
        correctness is the caller's responsibility. A regression that
        wrapped the handler in a silent try/except would mask plugin
        bugs in production.
        """

        def provider() -> _DummyCapabilities:
            raise RuntimeError("provider failed")

        app = _build_capabilities_app(provider)
        app.dependency_overrides[get_current_user] = lambda: regular_user
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/apps/test-capabilities-endpoint/capabilities")
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_provider_with_depends_param_resolved(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert provider params annotated with ``Depends`` resolve per request.

        ``functools.wraps`` preserves the provider's signature so FastAPI
        inspects its parameter defaults and wires DI normally. This pins
        the contract that plugin authors may declare ``Depends(...)``
        params (settings, session, current user) on the provider rather
        than reading module globals.
        """

        def provide_flag() -> bool:
            return True

        def provider(
            *,
            flag: bool = Depends(provide_flag),
        ) -> _DummyCapabilities:
            return _DummyCapabilities(flag=flag)

        app = _build_capabilities_app(provider)
        app.dependency_overrides[get_current_user] = lambda: regular_user
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/apps/test-capabilities-endpoint/capabilities")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"flag": True}


# ── derive_crud_routes() helper ─────────────────────────────────────────


_SYNTHETIC_OWNER = "ARCHIVER"
_CRUD_PREFIX = "/test-derive-crud"
_CRUD_BASE_URL = f"/api/apps{_CRUD_PREFIX}"
_SCRIPT_PREFIX = "/test-derive-script"
_SCRIPT_BASE_URL = f"/api/apps{_SCRIPT_PREFIX}"
_SCRIPT_PAGE_LIMIT = 25

_PAGE_OFFSET = 5
_PAGE_LIMIT = 2
_PAGE_TOTAL = 10


_SYNTHETIC_SCHEMA = AppSchema(
    name="test-derive-crud",
    display_name="Test Derive CRUD",
    forms=[
        FormSection(title="Options", fields=[BoolField(name="flag", label="Flag")]),
    ],
    list_view=ListView(columns=[Column(key="id", label="ID")]),
)


class _SyntheticTaskResponse(BaseModel):
    """Represent the list/detail response for the synthetic CRUD plugin.

    ``display_label`` carries a wire alias so the tests can prove the derived
    routes serialise with ``response_model_by_alias=True``.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    display_label: str = Field(alias="displayLabel")
    status: TaskHistoryStatusEnum | None = None
    last_executed_at: datetime | None = None


class _SyntheticCreateResponse(BaseModel):
    """Represent a distinct create response, proving create may carry its own model."""

    name: str
    created: bool = True


class _SyntheticCreate(BaseModel):
    """Represent the request body for the synthetic create route."""

    name: str


def _build_synthetic_response(
    task: Task,
    status: TaskHistoryStatusEnum | None = None,
    *,
    last_executed_at: datetime | None = None,
) -> _SyntheticTaskResponse:
    """Build the synthetic list/detail response for ``task``."""
    return _SyntheticTaskResponse(
        name=task.name,
        display_label=task.name.upper(),
        status=status,
        last_executed_at=last_executed_at,
    )


def _build_synthetic_create_response(task: Task) -> _SyntheticCreateResponse:
    """Build the distinct synthetic create response for ``task``."""
    return _SyntheticCreateResponse(name=task.name)


async def _build_synthetic_payload(
    form: Annotated[_SyntheticCreate, Body()],
) -> TaskWrite:
    """Build a ``TaskWrite`` from the synthetic create body (drives the 422)."""
    return TaskWrite(name=form.name, data={})


async def _get_task_by_alt_param(name: str, tasks_api: TaskAPI) -> Task:
    """Resolve a task whose detail path parameter is ``name`` (not ``task_name``)."""
    return await get_task_by_name(tasks_api, name, _SYNTHETIC_OWNER)


async def _synthetic_update_handler(
    task_name: str, tasks_api: TaskAPI
) -> _SyntheticTaskResponse:
    """Override update handler: resolve the task and echo it back."""
    task = await get_task_by_name(tasks_api, task_name, _SYNTHETIC_OWNER)
    return _build_synthetic_response(task, status=None)


async def _synthetic_delete_handler(task_name: str, tasks_api: TaskAPI) -> None:
    """Override delete handler: resolve the task and delete it upstream."""
    task = await get_task_by_name(tasks_api, task_name, _SYNTHETIC_OWNER)
    await tasks_api.delete(f"/{task.name}")


def _task_dict(name: str, owner: str = _SYNTHETIC_OWNER) -> dict:
    """Return a JSON-serialisable ``Task`` payload for the mock Tasks API."""
    return TaskFactory.build(name=name, owner=owner, data={}).model_dump(mode="json")


async def _fake_tasks_get(
    path: str,
    *,
    list_items: list[dict],
    list_total: int | None,
    detail_task: dict | None,
    history_items: list[dict],
    history_error: Exception | None,
) -> dict:
    """Route a fake Tasks-API ``GET`` by path for the in-memory backend."""
    if path == "/":
        envelope = {"items": list_items}
        if list_total is not None:
            envelope["total"] = list_total
        return envelope
    if path.endswith("/history/"):
        if history_error is not None:
            raise history_error
        return {"items": history_items}
    return detail_task if detail_task is not None else {}


async def _fake_tasks_post(
    path: str,
    json: dict | None,
    *,
    latest_statuses: dict[str, str | None],
    created_task: dict | None,
    batch_error: Exception | None,
    create_error: Exception | None,
) -> dict:
    """Route a fake Tasks-API ``POST`` by path for the in-memory backend."""
    if path == "/history/latest":
        if batch_error is not None:
            raise batch_error
        names = (json or {}).get("names", [])
        return {
            name: (
                {"status": status, "finished_at": None}
                if (status := latest_statuses.get(name)) is not None
                else None
            )
            for name in names
        }
    if create_error is not None:
        raise create_error
    return created_task if created_task is not None else {}


def _make_tasks_api(
    *,
    list_items: list[dict] | None = None,
    list_total: int | None = None,
    detail_task: dict | None = None,
    history_items: list[dict] | None = None,
    latest_statuses: dict[str, str | None] | None = None,
    created_task: dict | None = None,
    history_error: Exception | None = None,
    batch_error: Exception | None = None,
    create_error: Exception | None = None,
) -> AsyncMock:
    """Build an ``AsyncMock(spec=RemoteAPI)`` routing Tasks-API calls in memory."""
    api = AsyncMock(spec=RemoteAPI)

    async def _get(path: str, params: dict | None = None) -> dict:
        return await _fake_tasks_get(
            path,
            list_items=list_items or [],
            list_total=list_total,
            detail_task=detail_task,
            history_items=history_items or [],
            history_error=history_error,
        )

    async def _post(path: str, json: dict | None = None) -> dict:
        return await _fake_tasks_post(
            path,
            json,
            latest_statuses=latest_statuses or {},
            created_task=created_task,
            batch_error=batch_error,
            create_error=create_error,
        )

    async def _delete(path: str) -> None:
        return None

    api.get.side_effect = _get
    api.post.side_effect = _post
    api.delete.side_effect = _delete
    return api


def _crud_router(**overrides: object) -> APIRouter:
    """Build a ``derive_crud_routes`` router with sane synthetic defaults."""
    kwargs = {
        "task_owner": _SYNTHETIC_OWNER,
        "get_task": make_task_dep(_SYNTHETIC_OWNER),
        "response_builder": _build_synthetic_response,
        "create_payload": _build_synthetic_payload,
    }
    kwargs.update(overrides)
    return derive_crud_routes(_SYNTHETIC_SCHEMA, **kwargs)


class _StubScript:
    """Represent a minimal script for ``derive_script_routes`` introspection tests."""

    def __init__(self, filename: str = "stub.sh") -> None:
        self.filename = filename

    @property
    def execution_task_name(self) -> str:
        """Return a fixed task name."""
        return "stub-task"

    def get_execution_model(self) -> type[BaseModel]:
        """Return an empty execution model."""
        return BaseModel


class _ScriptListRow(BaseModel):
    """Represent the list-row projection used by script-route tests."""

    filename: str


_STUB_SPEC = ListQuerySpec(
    sortable={"filename": column("filename")},
    default_sort="filename",
    tie_breaker=column("filename"),
    searchable=(column("filename"),),
)

_STUB_APPLIER = InMemoryListQueryApplier(_STUB_SPEC)


def _make_script_source(
    *,
    list_response_model: type[BaseModel] | None = None,
    rows: list[_StubScript] | None = None,
    in_memory_list_query: bool = False,
) -> ScriptSource[_StubScript]:
    """Build a minimal ``ScriptSource`` for ``derive_script_routes`` tests.

    ``list_scripts`` honours the widened contract through the same framework adapter a
    real materializing source uses, so the stub cannot drift from production's shape.
    """
    scripts = rows if rows is not None else [_StubScript()]

    async def _materialize() -> list[_StubScript]:
        return scripts

    _list_scripts = in_memory_list_scripts(_materialize, _STUB_APPLIER)

    async def _load_script(filename: str) -> _StubScript:
        return _StubScript(filename)

    return ScriptSource(
        script_dir=Path("/tmp"),
        load_script=_load_script,
        list_scripts=_list_scripts,
        build_form_schema=lambda _: _TEST_SCHEMA,
        build_execution_meta=lambda _script, _request: BaseModel(),
        list_response=lambda stub: _ScriptListRow(filename=stub.filename),
        list_response_model=list_response_model,
        in_memory_list_query=in_memory_list_query,
    )


def _script_router(**overrides: object) -> APIRouter:
    """Build a ``derive_script_routes`` router with sane synthetic defaults."""
    pagination_dep = overrides.pop("pagination_dep", None)
    list_response_model = overrides.pop("list_response_model", _ScriptListRow)
    list_query_spec = overrides.pop("list_query_spec", None)
    source = _make_script_source(
        list_response_model=list_response_model,
        rows=overrides.pop("rows", None),
        in_memory_list_query=overrides.pop("in_memory_list_query", False),
    )
    return derive_script_routes(
        source,
        name="test-scripts",
        pagination_dep=pagination_dep,
        list_query_spec=list_query_spec,
    )


def _authed_crud_client(
    router: APIRouter, tasks_api: AsyncMock, user: CasdoorUser
) -> TestClient:
    """Mount ``router`` in a production-shape app with auth + Tasks-API overrides."""
    app = _mount_plugin_router(router, _CRUD_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tasks_api] = lambda: tasks_api
    return TestClient(app, raise_server_exceptions=False)


def _authed_script_client(router: APIRouter, user: CasdoorUser) -> TestClient:
    """Mount a script ``router`` in a production-shape app with an auth override.

    A script list route lists from its ``ScriptSource``, not the Tasks API, so no
    ``get_tasks_api`` override is installed.

    :param router: The ``derive_script_routes`` router under test.
    :param user: The authenticated user the auth dep resolves to.
    :return: A bare ``TestClient`` mounting the script router under the script prefix.
    """
    app = _mount_plugin_router(router, _SCRIPT_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _api_routes(router: APIRouter) -> list[APIRoute]:
    """Return the concrete ``APIRoute`` objects registered on ``router``."""
    return [r for r in router.routes if isinstance(r, APIRoute)]


def _route_for(router: APIRouter, path: str, method: str) -> APIRoute:
    """Return the single ``APIRoute`` on ``router`` matching ``path`` + ``method``."""
    [route] = [r for r in _api_routes(router) if r.path == path and method in r.methods]
    return route


class TestDeriveCrudRoutesComposition:
    """Inspect the returned router in isolation, without HTTP."""

    def test_registers_schema_and_core_routes(self) -> None:
        """Assert the router carries ``/schema`` plus list / detail / create."""
        router = _crud_router()
        registered = {(r.path, frozenset(r.methods)) for r in _api_routes(router)}

        assert ("/schema", frozenset({"GET"})) in registered
        assert ("/", frozenset({"GET"})) in registered
        assert ("/{task_name}", frozenset({"GET"})) in registered
        assert ("/", frozenset({"POST"})) in registered

    def test_no_update_or_delete_route_by_default(self) -> None:
        """Assert no ``PUT`` / ``DELETE`` route is registered without overrides."""
        router = _crud_router()
        methods = {m for r in _api_routes(router) for m in r.methods}

        assert "PUT" not in methods
        assert "DELETE" not in methods

    def test_derive_list_false_suppresses_derived_list(self) -> None:
        """Assert ``derive_list=False`` registers no ``GET /`` list route."""
        router = _crud_router(derive_list=False)
        registered = {(r.path, frozenset(r.methods)) for r in _api_routes(router)}

        assert ("/", frozenset({"GET"})) not in registered
        assert ("/schema", frozenset({"GET"})) in registered

    def test_derive_list_true_registers_derived_list(self) -> None:
        """Assert the default ``derive_list=True`` still registers ``GET /``."""
        router = _crud_router(derive_list=True)
        registered = {(r.path, frozenset(r.methods)) for r in _api_routes(router)}

        assert ("/", frozenset({"GET"})) in registered

    def test_update_route_only_when_handler_passed(self) -> None:
        """Assert a ``PUT /{task_name}`` route appears only with an update handler."""
        router = _crud_router(update_handler=_synthetic_update_handler)
        route = _route_for(router, "/{task_name}", "PUT")

        assert route.methods == {"PUT"}

    def test_delete_route_only_when_handler_passed(self) -> None:
        """Assert a ``DELETE /{task_name}`` route appears only with a delete handler."""
        router = _crud_router(delete_handler=_synthetic_delete_handler)
        route = _route_for(router, "/{task_name}", "DELETE")

        assert route.methods == {"DELETE"}

    def test_create_route_pins_201(self) -> None:
        """Assert the create route sets ``status_code=201``."""
        route = _route_for(_crud_router(), "/", "POST")

        assert route.status_code == status.HTTP_201_CREATED

    def test_delete_route_pins_204(self) -> None:
        """Assert the delete route sets ``status_code=204``."""
        router = _crud_router(delete_handler=_synthetic_delete_handler)
        route = _route_for(router, "/{task_name}", "DELETE")

        assert route.status_code == status.HTTP_204_NO_CONTENT

    def test_derived_routes_declare_api_authenticated(self) -> None:
        """Assert every derived route redeclares ``IsApiAuthenticated``."""
        router = _crud_router(
            update_handler=_synthetic_update_handler,
            delete_handler=_synthetic_delete_handler,
        )

        for route in _api_routes(router):
            callables = {d.dependency for d in route.dependencies}
            assert get_current_user in callables, route.path

    def test_derived_routes_emit_by_alias(self) -> None:
        """Assert list / detail / create pin ``response_model_by_alias=True``."""
        router = _crud_router()

        for path, method in (("/", "GET"), ("/{task_name}", "GET"), ("/", "POST")):
            assert _route_for(router, path, method).response_model_by_alias is True

    def test_list_response_model_is_list_of_builder_model(self) -> None:
        """Assert the list route's response model is ``list[<builder model>]``."""
        route = _route_for(_crud_router(), "/", "GET")

        assert route.response_model == list[_SyntheticTaskResponse]

    def test_detail_response_model_is_builder_model(self) -> None:
        """Assert the detail route's response model is the builder's model."""
        route = _route_for(_crud_router(), "/{task_name}", "GET")

        assert route.response_model is _SyntheticTaskResponse

    def test_create_response_model_defaults_to_builder_model(self) -> None:
        """Assert create reuses the list/detail model when no create builder is given."""
        route = _route_for(_crud_router(), "/", "POST")

        assert route.response_model is _SyntheticTaskResponse

    def test_create_response_model_distinct_when_create_builder_passed(self) -> None:
        """Assert a create builder gives create its own distinct response model."""
        router = _crud_router(create_response_builder=_build_synthetic_create_response)
        create_route = _route_for(router, "/", "POST")
        list_route = _route_for(router, "/", "GET")

        assert create_route.response_model is _SyntheticCreateResponse
        assert list_route.response_model == list[_SyntheticTaskResponse]

    def test_paginated_list_response_model_is_paginated(self) -> None:
        """Assert a pagination dep switches the list model to ``PaginatedResponse``."""
        router = _crud_router(pagination_dep=make_pagination_dep(max_limit=50))
        route = _route_for(router, "/", "GET")

        assert route.response_model == PaginatedResponse[_SyntheticTaskResponse]

    def test_configurable_detail_path_param_changes_route_path(self) -> None:
        """Assert ``detail_path_param`` renames the detail/create-sibling path."""
        router = _crud_router(get_task=_get_task_by_alt_param, detail_path_param="name")
        paths = {r.path for r in _api_routes(router)}

        assert "/{name}" in paths
        assert "/{task_name}" not in paths

    def test_async_response_builder_raises_type_error(self) -> None:
        """Assert an ``async def`` response builder is rejected at registration.

        The derived handlers invoke ``response_builder`` synchronously, so an
        async builder would yield an un-awaited coroutine that response
        serialisation cannot handle. The guard mirrors ``capabilities_endpoint``
        and fails fast at construction instead of at first request.
        """

        async def _async_builder(
            task: Task, status: TaskHistoryStatusEnum | None = None
        ) -> _SyntheticTaskResponse:
            return _build_synthetic_response(task, status)

        with pytest.raises(TypeError, match="sync callable"):
            _crud_router(response_builder=_async_builder)

    def test_async_create_response_builder_raises_type_error(self) -> None:
        """Assert an ``async def`` create response builder is rejected too."""

        async def _async_create_builder(task: Task) -> _SyntheticCreateResponse:
            return _build_synthetic_create_response(task)

        with pytest.raises(TypeError, match="sync callable"):
            _crud_router(create_response_builder=_async_create_builder)


class TestDeriveScriptRoutes:
    """Inspect the script-source derived list route in isolation, without HTTP."""

    def test_list_response_model_is_list_of_row_model(self) -> None:
        """Assert the list route's response model is ``list[<row model>]``."""
        route = _route_for(_script_router(), "/", "GET")

        assert route.response_model == list[_ScriptListRow]

    def test_paginated_list_response_model_is_paginated(self) -> None:
        """Assert a pagination dep switches the list model to ``PaginatedResponse``."""
        router = _script_router(pagination_dep=make_pagination_dep(max_limit=50))
        route = _route_for(router, "/", "GET")

        assert route.response_model == PaginatedResponse[_ScriptListRow]

    def test_paginated_list_untyped_without_response_model(self) -> None:
        """Assert pagination without ``list_response_model`` uses untyped envelope."""
        router = _script_router(
            pagination_dep=make_pagination_dep(max_limit=50),
            list_response_model=None,
        )
        route = _route_for(router, "/", "GET")

        assert route.response_model is PaginatedResponse


class TestDeriveScriptRoutesPaginatedList:
    """Exercise the paginated script ``GET /`` list route over HTTP."""

    def test_paginated_list_200_returns_envelope(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the paginated script list returns the envelope echoing offset/limit."""
        router = _script_router(pagination_dep=make_pagination_dep(max_limit=50))
        client = _authed_script_client(router, regular_user)

        response = client.get(
            f"{_SCRIPT_BASE_URL}/", params={"offset": 0, "limit": _SCRIPT_PAGE_LIMIT}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {"items", "total", "offset", "limit"} <= body.keys()
        assert body["total"] == 1
        assert body["offset"] == 0
        assert body["limit"] == _SCRIPT_PAGE_LIMIT
        assert [item["filename"] for item in body["items"]] == ["stub.sh"]

    def test_paginated_list_slices_by_offset(self, regular_user: CasdoorUser) -> None:
        """Assert an offset past the discovered scripts yields an empty page."""
        router = _script_router(pagination_dep=make_pagination_dep(max_limit=50))
        client = _authed_script_client(router, regular_user)

        response = client.get(
            f"{_SCRIPT_BASE_URL}/", params={"offset": 1, "limit": _SCRIPT_PAGE_LIMIT}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert body["items"] == []


class TestDeriveScriptRoutesListQueryGuard:
    """Pin that the seam refuses to silently drop a source's list query.

    ``derive_script_routes`` is public and wired directly by callers outside
    ``TaskExecutionApp``, so it cannot lean on the app-level validator: without a spec
    it would register the no-query handler and quietly discard the source's filters, and
    without a pagination dependency it registers the unpaginated route, which mounts no
    query dependency whether a spec was supplied or not.
    """

    def test_in_memory_source_without_spec_raises(self) -> None:
        """Reject an in-memory source when no spec was supplied."""
        with pytest.raises(ValueError, match="no list_query_spec was supplied"):
            _script_router(
                pagination_dep=make_pagination_dep(max_limit=50),
                in_memory_list_query=True,
            )

    def test_source_with_list_query_dep_without_spec_raises(self) -> None:
        """Reject a source supplying its own dependency when no spec was supplied."""
        source = replace(
            _make_script_source(),
            list_query_dep=_STUB_APPLIER.default_query,
        )

        with pytest.raises(ValueError, match="no list_query_spec was supplied"):
            derive_script_routes(
                source,
                name="test-scripts",
                pagination_dep=make_pagination_dep(max_limit=50),
            )

    def test_spec_without_pagination_dep_raises(self) -> None:
        """Reject a spec on an unpaginated route, which exposes no query params."""
        with pytest.raises(ValueError, match="no pagination_dep"):
            _script_router(list_query_spec=_STUB_SPEC, in_memory_list_query=True)

    def test_spec_without_pagination_dep_raises_for_a_plain_source(self) -> None:
        """Reject the pairing even when the source resolves no query of its own."""
        with pytest.raises(ValueError, match="no pagination_dep"):
            _script_router(list_query_spec=_STUB_SPEC)

    def test_plain_source_without_spec_is_accepted(self) -> None:
        """Leave a source that resolves no query alone, so the guard is not too broad."""
        router = _script_router(pagination_dep=make_pagination_dep(max_limit=50))

        assert router.routes

    def test_unpaginated_plain_source_is_accepted(self) -> None:
        """Leave the unpaginated no-spec wiring alone — nothing is being dropped."""
        router = _script_router()

        assert router.routes


class TestDeriveScriptRoutesListQuerySeam:
    """Cover the ``list_query_spec`` sort/search seam on the derived list route."""

    def _routed_router(self) -> APIRouter:
        return _script_router(
            pagination_dep=make_pagination_dep(max_limit=50),
            list_query_spec=_STUB_SPEC,
            in_memory_list_query=True,
            rows=[_StubScript("alpha.sh"), _StubScript("beta.sh")],
        )

    def test_spec_routes_search_through_list_scripts(
        self, regular_user: CasdoorUser
    ) -> None:
        """Filter via the spec's ``search`` param and its filtered total."""
        client = _authed_script_client(self._routed_router(), regular_user)

        response = client.get(f"{_SCRIPT_BASE_URL}/", params={"search": "alpha"})

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == 1
        assert [item["filename"] for item in body["items"]] == ["alpha.sh"]

    def test_spec_orders_by_sort_param(self, regular_user: CasdoorUser) -> None:
        """Order rows by the spec's ``sort`` param (``-`` prefix descending)."""
        client = _authed_script_client(self._routed_router(), regular_user)

        response = client.get(f"{_SCRIPT_BASE_URL}/", params={"sort": "-filename"})

        assert response.status_code == status.HTTP_200_OK
        assert [item["filename"] for item in response.json()["items"]] == [
            "beta.sh",
            "alpha.sh",
        ]

    def test_invalid_sort_key_rejected_with_422(
        self, regular_user: CasdoorUser
    ) -> None:
        """Reject an out-of-allowlist sort key at the boundary with 422."""
        client = _authed_script_client(self._routed_router(), regular_user)

        response = client.get(f"{_SCRIPT_BASE_URL}/", params={"sort": "bogus"})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_spec_exposes_sort_and_search_params(self) -> None:
        """Declare exactly ``sort`` and ``search`` on the route when a spec is set."""
        route = _route_for(self._routed_router(), "/", "GET")
        flat = get_flat_dependant(route.dependant)

        param_names = {param.name for param in flat.query_params}
        assert {"sort", "search"} <= param_names

    def test_without_spec_exposes_no_query_params(self) -> None:
        """Keep the fetch-all-then-slice path when the app declares no spec."""
        router = _script_router(pagination_dep=make_pagination_dep(max_limit=50))
        route = _route_for(router, "/", "GET")
        flat = get_flat_dependant(route.dependant)

        param_names = {param.name for param in flat.query_params}
        assert "sort" not in param_names
        assert "search" not in param_names


class TestDeriveCrudRoutesCreateSkip:
    """Cover the read-only path where ``create_payload`` is omitted."""

    def test_no_create_route_when_create_payload_none(self) -> None:
        """Assert ``create_payload=None`` registers no ``POST /`` route."""
        router = _crud_router(create_payload=None)
        methods = {(r.path, m) for r in _api_routes(router) for m in r.methods}

        assert ("/", "POST") not in methods

    def test_schema_list_detail_unaffected_when_create_payload_none(self) -> None:
        """Assert schema / list / detail still register without a create route."""
        router = _crud_router(create_payload=None)
        registered = {(r.path, frozenset(r.methods)) for r in _api_routes(router)}

        assert ("/schema", frozenset({"GET"})) in registered
        assert ("/", frozenset({"GET"})) in registered
        assert ("/{task_name}", frozenset({"GET"})) in registered

    def test_connectivity_check_without_create_payload_raises(self) -> None:
        """Assert ``connectivity_check=True`` with no create payload fails fast."""
        with pytest.raises(ValueError, match="connectivity_check"):
            _crud_router(create_payload=None, connectivity_check=True)

    def test_create_response_builder_without_create_payload_raises(self) -> None:
        """Assert a create response builder with no create payload fails fast."""
        with pytest.raises(ValueError, match="create_response_builder"):
            _crud_router(
                create_payload=None,
                create_response_builder=_build_synthetic_create_response,
            )


class TestDeriveCrudRoutesList:
    """Exercise the non-paginated ``GET /`` list route over HTTP."""

    def test_list_200_with_alias_and_status(self, regular_user: CasdoorUser) -> None:
        """Assert list returns 200, alias-mapped keys, and batched statuses."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1"), _task_dict("t2")],
            latest_statuses={"t1": TaskHistoryStatusEnum.SUCCESS.value, "t2": None},
        )
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["name"] for item in body] == ["t1", "t2"]
        assert body[0]["displayLabel"] == "T1"
        assert "display_label" not in body[0]
        assert body[0]["status"] == TaskHistoryStatusEnum.SUCCESS.value
        assert body[1]["status"] is None

    def test_list_empty_returns_empty_list_without_batch(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert an empty upstream page yields ``[]`` and skips the batch call."""
        tasks_api = _make_tasks_api(list_items=[])
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
        tasks_api.post.assert_not_awaited()

    def test_list_degrades_to_none_when_batch_chunk_fails(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a failed batch-status call degrades rows to ``status=None``."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1")],
            batch_error=RuntimeError("batch endpoint down"),
        )
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body[0]["name"] == "t1"
        assert body[0]["status"] is None


class TestDeriveCrudRoutesPaginatedList:
    """Exercise the paginated ``GET /`` list route over HTTP."""

    def test_paginated_list_200_forwards_offset_limit(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert paginated list returns the envelope and forwards offset/limit."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1"), _task_dict("t2")],
            list_total=_PAGE_TOTAL,
            latest_statuses={
                "t1": TaskHistoryStatusEnum.SUCCESS.value,
                "t2": TaskHistoryStatusEnum.FAILED.value,
            },
        )
        router = _crud_router(pagination_dep=make_pagination_dep(max_limit=50))
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.get(
            f"{_CRUD_BASE_URL}/",
            params={"offset": _PAGE_OFFSET, "limit": _PAGE_LIMIT},
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == _PAGE_TOTAL
        assert body["offset"] == _PAGE_OFFSET
        assert body["limit"] == _PAGE_LIMIT
        assert [item["name"] for item in body["items"]] == ["t1", "t2"]
        forwarded = tasks_api.get.await_args.kwargs["params"]
        assert forwarded["offset"] == _PAGE_OFFSET
        assert forwarded["limit"] == _PAGE_LIMIT

    def test_paginated_list_total_falls_back_to_page_length(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``total`` falls back to the page length when upstream omits it."""
        items = [_task_dict("t1"), _task_dict("t2")]
        tasks_api = _make_tasks_api(
            list_items=items,
            latest_statuses={"t1": None, "t2": None},
        )
        router = _crud_router(pagination_dep=make_pagination_dep(max_limit=50))
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] == len(items)


class TestDeriveCrudRoutesDetail:
    """Exercise the ``GET /{task_name}`` detail route over HTTP."""

    def test_detail_200(self, regular_user: CasdoorUser) -> None:
        """Assert detail returns 200 with the resolved task and latest status."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_items=[{"status": TaskHistoryStatusEnum.SUCCESS.value}],
        )
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["name"] == "t1"
        assert body["displayLabel"] == "T1"
        assert body["status"] == TaskHistoryStatusEnum.SUCCESS.value

    def test_detail_404_on_owner_mismatch(self, regular_user: CasdoorUser) -> None:
        """Assert detail 404s when the resolved task's owner mismatches."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1", owner="BACKUPS"),
        )
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_detail_degrades_on_history_error(self, regular_user: CasdoorUser) -> None:
        """Assert an upstream history error degrades to ``status=None``."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_error=HTTPConflictException(),
        )
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["name"] == "t1"
        assert body["status"] is None


class TestDeriveCrudRoutesCreate:
    """Exercise the ``POST /`` create route over HTTP."""

    def test_create_201(self, regular_user: CasdoorUser) -> None:
        """Assert a valid body returns 201 and posts the built payload upstream."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["name"] == "new-task"
        assert tasks_api.post.await_args.args[0] == "/"

    def test_create_422_on_invalid_body(self, regular_user: CasdoorUser) -> None:
        """Assert an invalid body 422s before the handler body posts anything."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        tasks_api.post.assert_not_awaited()

    def test_create_uses_distinct_response_model(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a create builder produces its own response model on the wire."""
        tasks_api = _make_tasks_api(created_task=_task_dict("new-task"))
        router = _crud_router(create_response_builder=_build_synthetic_create_response)
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body == {"name": "new-task", "created": True}
        assert "displayLabel" not in body

    def test_create_propagates_upstream_error(self, regular_user: CasdoorUser) -> None:
        """Assert an upstream create error surfaces rather than being swallowed."""
        tasks_api = _make_tasks_api(create_error=HTTPConflictException())
        client = _authed_crud_client(_crud_router(), tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_409_CONFLICT


_CONNECTIVITY_META = {
    "target": "node-1",
    CONNECTIVITY_META_HOST_KEY: "db-host",
    CONNECTIVITY_META_PORT_KEY: 3306,
    CONNECTIVITY_META_SERVICE_TYPE_KEY: "mysql",
}


def _task_dict_with_meta(name: str, meta: dict) -> dict:
    """Return a created-task payload whose ``data.meta`` carries ``meta``."""
    return TaskFactory.build(
        name=name, owner=_SYNTHETIC_OWNER, data={"meta": meta}
    ).model_dump(mode="json")


def _patch_probe(mocker, result: ConnectivityWarning | None) -> AsyncMock:
    """Patch the network probe boundary, leaving the real guard helper intact."""
    return mocker.patch(
        "app.extensions.apps.framework.connectivity.record_connectivity_warning",
        new_callable=AsyncMock,
        return_value=result,
    )


class _SyntheticConnectivityCreateResponse(BaseModel):
    """Represent an explicit create response that already declares the warning."""

    name: str
    connectivity_warning: ConnectivityWarning | None = None


def _build_synthetic_connectivity_create_response(
    task: Task,
) -> _SyntheticConnectivityCreateResponse:
    """Build an explicit create response that declares ``connectivity_warning``."""
    return _SyntheticConnectivityCreateResponse(name=task.name)


class TestDeriveCrudRoutesConnectivity:
    """Exercise the ``connectivity_check`` create-route option over HTTP."""

    def test_probe_failure_populates_connectivity_warning(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Surface the probe warning on the create response when the probe fails."""
        warning = ConnectivityWarning(
            target="node-1", service_type="mysql", message="unreachable"
        )
        probe = _patch_probe(mocker, warning)
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] == {
            "target": "node-1",
            "service_type": "mysql",
            "message": "unreachable",
            "task_history_id": None,
        }
        probe.assert_awaited_once()

    def test_probe_failure_threads_task_history_id_into_response(
        self, regular_user: CasdoorUser
    ) -> None:
        """Thread a non-null ``task_history_id`` end-to-end into the create JSON.

        Patch only the Tasks-API boundary (not ``record_connectivity_warning``),
        so the real ``_fetch_connectivity_result`` -> ``record_connectivity_warning``
        -> create-response chain runs. A failed ``/connectivity-check/`` response
        carrying ``task_history_id`` must surface that id on
        ``connectivity_warning`` so the React detail page can link the run-script
        log — the gap the ``None``-only probe-patch tests leave uncovered.
        """
        from app.extensions.connectivity import (  # noqa: PLC0415
            _fetch_connectivity_result,
        )

        _fetch_connectivity_result.cache_clear()

        task_history_id = 4242
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        base_post = tasks_api.post.side_effect

        async def _post(path: str, json: dict | None = None) -> dict:
            if path == "/connectivity-check/":
                return {
                    "success": False,
                    "error": "Connectivity check timed out after 30s",
                    "task_history_id": task_history_id,
                }
            return await base_post(path, json)

        tasks_api.post.side_effect = _post
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        warning = response.json()["connectivity_warning"]
        assert warning["task_history_id"] == task_history_id
        assert warning["message"] == "Connectivity check timed out after 30s"
        _fetch_connectivity_result.cache_clear()

    def test_probe_success_yields_null_connectivity_warning(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Return a ``null`` warning when the probe succeeds."""
        probe = _patch_probe(mocker, None)
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is None
        probe.assert_awaited_once()

    def test_opt_out_query_skips_probe(self, regular_user: CasdoorUser, mocker) -> None:
        """Skip the probe and null the warning when ``?check_connectivity=false``."""
        probe = _patch_probe(mocker, None)
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(
            f"{_CRUD_BASE_URL}/?check_connectivity=false", json={"name": "new-task"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is None
        probe.assert_not_awaited()

    def test_meta_without_connectivity_keys_skips_probe(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Short-circuit to ``null`` when the task meta lacks connectivity keys."""
        probe = _patch_probe(mocker, None)
        tasks_api = _make_tasks_api(created_task=_task_dict_with_meta("new-task", {}))
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is None
        probe.assert_not_awaited()

    def test_invalid_body_422_before_probe_or_create(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Reject an invalid body with 422 before any upstream create or probe."""
        probe = _patch_probe(mocker, None)
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={})

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        tasks_api.post.assert_not_awaited()
        probe.assert_not_awaited()

    def test_upstream_create_error_propagates(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Propagate an upstream create error instead of swallowing it."""
        _patch_probe(mocker, None)
        tasks_api = _make_tasks_api(create_error=HTTPConflictException())
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_off_path_create_route_keeps_unchanged_model(self) -> None:
        """Keep the default (off) create route's response model unchanged."""
        route = _route_for(_crud_router(), "/", "POST")

        assert route.response_model is _SyntheticTaskResponse

    def test_off_path_openapi_omits_check_connectivity_param(
        self, regular_user: CasdoorUser
    ) -> None:
        """Keep the default create route free of the ``check_connectivity`` param."""
        client = _authed_crud_client(_crud_router(), _make_tasks_api(), regular_user)
        spec = client.get("/openapi.json").json()

        params = spec["paths"][f"{_CRUD_BASE_URL}/"]["post"].get("parameters", [])
        assert all(param["name"] != "check_connectivity" for param in params)

    def test_on_path_openapi_documents_check_connectivity_query_param(
        self, regular_user: CasdoorUser
    ) -> None:
        """Document one boolean ``check_connectivity`` query param defaulting true."""
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), _make_tasks_api(), regular_user
        )
        spec = client.get("/openapi.json").json()

        params = spec["paths"][f"{_CRUD_BASE_URL}/"]["post"].get("parameters", [])
        check_params = [p for p in params if p["name"] == "check_connectivity"]
        assert len(check_params) == 1
        [param] = check_params
        assert param["in"] == "query"
        assert param["required"] is False
        assert param["schema"]["type"] == "boolean"
        assert param["schema"]["default"] is True

    def test_on_path_openapi_names_derived_create_component(
        self, regular_user: CasdoorUser
    ) -> None:
        """Name the auto-derived create component ``TestDeriveCrudCreateResponse``."""
        client = _authed_crud_client(
            _crud_router(connectivity_check=True), _make_tasks_api(), regular_user
        )
        spec = client.get("/openapi.json").json()

        component = spec["components"]["schemas"]["TestDeriveCrudCreateResponse"]
        assert "connectivity_warning" in component["properties"]

    def test_explicit_create_builder_model_wins_with_connectivity_check(self) -> None:
        """Let an explicit create builder's model win even with ``connectivity_check``."""
        router = _crud_router(
            connectivity_check=True,
            create_response_builder=_build_synthetic_connectivity_create_response,
        )
        route = _route_for(router, "/", "POST")

        assert route.response_model is _SyntheticConnectivityCreateResponse

    def test_explicit_builder_surfaces_warning_via_model_copy(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Attach the probe warning to an explicit builder that declares the field."""
        warning = ConnectivityWarning(
            target="node-1", service_type="mysql", message="unreachable"
        )
        _patch_probe(mocker, warning)
        tasks_api = _make_tasks_api(
            created_task=_task_dict_with_meta("new-task", _CONNECTIVITY_META)
        )
        router = _crud_router(
            connectivity_check=True,
            create_response_builder=_build_synthetic_connectivity_create_response,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] == {
            "target": "node-1",
            "service_type": "mysql",
            "message": "unreachable",
            "task_history_id": None,
        }

    def test_explicit_builder_without_warning_field_rejected(self) -> None:
        """Reject an explicit builder whose model omits ``connectivity_warning``."""
        with pytest.raises(TypeError, match="connectivity_warning"):
            _crud_router(
                connectivity_check=True,
                create_response_builder=_build_synthetic_create_response,
            )


class TestDeriveCrudRoutesUpdate:
    """Exercise the override-driven ``PUT /{task_name}`` update route."""

    def test_update_200(self, regular_user: CasdoorUser) -> None:
        """Assert the update override drives a 200 with conventions applied."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t1"))
        router = _crud_router(update_handler=_synthetic_update_handler)
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.put(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["displayLabel"] == "T1"

    def test_update_404_on_owner_mismatch(self, regular_user: CasdoorUser) -> None:
        """Assert the update override 404s when the task owner mismatches."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t1", owner="BACKUPS"))
        router = _crud_router(update_handler=_synthetic_update_handler)
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.put(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDeriveCrudRoutesDelete:
    """Exercise the override-driven ``DELETE /{task_name}`` delete route."""

    def test_delete_204(self, regular_user: CasdoorUser) -> None:
        """Assert the delete override returns 204 with no body and deletes upstream."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t1"))
        router = _crud_router(delete_handler=_synthetic_delete_handler)
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.delete(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.content == b""
        tasks_api.delete.assert_awaited_once_with("/t1")

    def test_delete_404_on_owner_mismatch(self, regular_user: CasdoorUser) -> None:
        """Assert the delete override 404s when the task owner mismatches."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t1", owner="BACKUPS"))
        router = _crud_router(delete_handler=_synthetic_delete_handler)
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.delete(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDeriveCrudRoutesConfigurablePathParam:
    """Exercise a non-default ``detail_path_param`` over HTTP."""

    def test_detail_resolves_with_custom_path_param(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``detail_path_param='name'`` resolves the detail route correctly."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_items=[{"status": TaskHistoryStatusEnum.SUCCESS.value}],
        )
        router = _crud_router(get_task=_get_task_by_alt_param, detail_path_param="name")
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["name"] == "t1"


class TestDeriveCrudRoutesUnauthenticated:
    """Assert every derived route returns JSON 401 without an auth override."""

    @pytest.fixture
    def unauthed_crud_client(self) -> TestClient:
        """Return an unauthed client (Tasks-API stubbed, no auth override)."""
        tasks_api = _make_tasks_api(
            list_items=[], detail_task=_task_dict("t1"), created_task=_task_dict("t1")
        )
        app = _mount_plugin_router(_crud_router(), _CRUD_PREFIX)
        app.dependency_overrides[get_tasks_api] = lambda: tasks_api
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/"),
            ("get", "/t1"),
            ("post", "/"),
            ("get", "/schema"),
        ],
    )
    def test_route_returns_401_json(
        self, unauthed_crud_client: TestClient, method: str, path: str
    ) -> None:
        """Assert each derived route returns a JSON 401 when unauthenticated."""
        response = unauthed_crud_client.request(
            method, f"{_CRUD_BASE_URL}{path}", follow_redirects=False
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


class TestDeriveCrudRoutesOpenApi:
    """Assert the synthetic app's OpenAPI carries the no-op-preserving shape."""

    @pytest.fixture
    def openapi_spec(self, regular_user: CasdoorUser) -> dict:
        """Return the generated OpenAPI for a fully-featured synthetic CRUD app."""
        tasks_api = _make_tasks_api()
        router = _crud_router(
            create_response_builder=_build_synthetic_create_response,
            update_handler=_synthetic_update_handler,
            delete_handler=_synthetic_delete_handler,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)
        return client.get("/openapi.json").json()

    def test_create_documents_201(self, openapi_spec: dict) -> None:
        """Assert the create operation documents a 201 response."""
        operation = openapi_spec["paths"][f"{_CRUD_BASE_URL}/"]["post"]

        assert "201" in operation["responses"]

    def test_delete_documents_204_under_task_name_path(
        self, openapi_spec: dict
    ) -> None:
        """Assert delete documents 204 and the path keeps the ``task_name`` param."""
        path_item = openapi_spec["paths"][f"{_CRUD_BASE_URL}/{{task_name}}"]

        assert "204" in path_item["delete"]["responses"]

    def test_list_response_serialises_by_alias(self, openapi_spec: dict) -> None:
        """Assert the list/detail response component exposes the aliased wire key."""
        component = next(
            v
            for k, v in openapi_spec["components"]["schemas"].items()
            if k.endswith("_SyntheticTaskResponse")
        )

        assert "displayLabel" in component["properties"]
        assert "display_label" not in component["properties"]


# ── derive_crud_routes() list filters, detail builder, extra deps ───────


_CONTEXT_USER_ID = "uid-context-1"


class _SyntheticDetailResponse(BaseModel):
    """Represent a distinct detail response, proving detail may carry its own model."""

    name: str
    detail: bool = True


def _build_synthetic_detail_response(
    task: Task,
    status: TaskHistoryStatusEnum | None = None,
    *,
    last_executed_at: datetime | None = None,
) -> _SyntheticDetailResponse:
    """Build the distinct synthetic detail response for ``task``."""
    return _SyntheticDetailResponse(name=task.name)


class _ContextResponse(BaseModel):
    """Represent a response whose ``resolved_by`` comes from the bound context."""

    name: str
    resolved_by: str | None = None
    status: TaskHistoryStatusEnum | None = None


async def _context_provider() -> dict[str, str]:
    """Return a synthetic username map the framework binds once per request."""
    return {_CONTEXT_USER_ID: "Alice"}


def _build_context_response(
    task: Task,
    *,
    status: TaskHistoryStatusEnum | None = None,
    last_executed_at: datetime | None = None,
    context: dict[str, str] | None = None,
) -> _ContextResponse:
    """Resolve ``created_by`` from the bound ``context`` map, falling back to the id."""
    mapping = context or {}
    return _ContextResponse(
        name=task.name,
        resolved_by=mapping.get(task.created_by, task.created_by),
        status=status,
    )


class _ContextCreateResponse(BaseModel):
    """Represent a distinct create response whose ``resolved_by`` comes from context."""

    name: str
    resolved_by: str | None = None


def _build_context_create_response(
    task: Task,
    *,
    status: TaskHistoryStatusEnum | None = None,
    context: dict[str, str] | None = None,
) -> _ContextCreateResponse:
    """Resolve ``created_by`` from the bound ``context`` for a distinct create builder."""
    mapping = context or {}
    return _ContextCreateResponse(
        name=task.name, resolved_by=mapping.get(task.created_by, task.created_by)
    )


def _task_dict_created_by(name: str, created_by: str) -> dict:
    """Return a ``Task`` payload owned by the synthetic owner with a fixed creator."""
    return TaskFactory.build(
        name=name, owner=_SYNTHETIC_OWNER, data={}, created_by=created_by
    ).model_dump(mode="json")


def _list_query_param_names(router: APIRouter) -> set[str]:
    """Return the query-parameter names the list route exposes in its OpenAPI."""
    app = _mount_plugin_router(router, _CRUD_PREFIX)
    operation = app.openapi()["paths"][f"{_CRUD_BASE_URL}/"]["get"]
    return {param["name"] for param in operation.get("parameters", [])}


class TestMakeListFilterDep:
    """Cover the list-filter dependency factory's declared query params."""

    def test_declares_both_params_in_canonical_order(self) -> None:
        """Declare ``service_type`` before ``status`` when both filters are enabled."""
        dep = make_list_filter_dep(status=True, service_type=True)

        assert list(inspect.signature(dep).parameters) == ["service_type", "status"]

    def test_declares_only_status_when_service_type_disabled(self) -> None:
        """Declare only the ``status`` query param when ``service_type`` is disabled."""
        dep = make_list_filter_dep(status=True, service_type=False)

        assert list(inspect.signature(dep).parameters) == ["status"]

    def test_declares_only_service_type_when_status_disabled(self) -> None:
        """Declare only the ``service_type`` query param when ``status`` is disabled."""
        dep = make_list_filter_dep(status=False, service_type=True)

        assert list(inspect.signature(dep).parameters) == ["service_type"]

    def test_declares_no_params_when_both_disabled(self) -> None:
        """Declare no query params when neither filter is enabled."""
        dep = make_list_filter_dep(status=False, service_type=False)

        assert list(inspect.signature(dep).parameters) == []

    def test_returns_listfilters_carrying_supplied_values(self) -> None:
        """Return a ``ListFilters`` carrying the supplied status and service_type."""
        dep = make_list_filter_dep(status=True, service_type=True)

        result = dep(
            service_type=ServiceTypeEnum.MYSQL,
            status=TaskHistoryStatusEnum.SUCCESS,
        )

        assert result == ListFilters(
            status=TaskHistoryStatusEnum.SUCCESS, service_type=ServiceTypeEnum.MYSQL
        )


class TestDeriveCrudRoutesListFilters:
    """Exercise the declarative list-filter query params over the derived list route."""

    def test_no_filters_exposes_no_filter_query_params(self) -> None:
        """Assert the default list route exposes neither filter query param."""
        names = _list_query_param_names(_crud_router())

        assert "status" not in names
        assert "service_type" not in names

    def test_status_filter_exposes_status_query_param(self) -> None:
        """Assert ``list_status_filter`` adds a ``status`` query param to the route."""
        names = _list_query_param_names(_crud_router(list_status_filter=True))

        assert "status" in names

    def test_service_type_filter_exposes_service_type_query_param(self) -> None:
        """Assert ``list_service_type`` adds a ``service_type`` query param."""
        names = _list_query_param_names(
            _crud_router(list_service_type=ServiceTypeEnum.MYSQL)
        )

        assert "service_type" in names

    def test_status_filter_keeps_only_matching_rows(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``?status=`` returns only rows whose latest status matches."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1"), _task_dict("t2")],
            latest_statuses={
                "t1": TaskHistoryStatusEnum.SUCCESS.value,
                "t2": TaskHistoryStatusEnum.FAILED.value,
            },
        )
        client = _authed_crud_client(
            _crud_router(list_status_filter=True), tasks_api, regular_user
        )

        response = client.get(
            f"{_CRUD_BASE_URL}/",
            params={"status": TaskHistoryStatusEnum.SUCCESS.value},
        )

        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()] == ["t1"]

    def test_service_type_mismatch_short_circuits_before_fetch(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a mismatched ``?service_type=`` returns ``[]`` without fetching."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1")],
            latest_statuses={"t1": TaskHistoryStatusEnum.SUCCESS.value},
        )
        client = _authed_crud_client(
            _crud_router(list_service_type=ServiceTypeEnum.MYSQL),
            tasks_api,
            regular_user,
        )

        response = client.get(
            f"{_CRUD_BASE_URL}/",
            params={"service_type": ServiceTypeEnum.POSTGRESQL.value},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
        tasks_api.get.assert_not_awaited()

    def test_service_type_match_lists_rows(self, regular_user: CasdoorUser) -> None:
        """Assert a matching ``?service_type=`` lists rows normally."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1")],
            latest_statuses={"t1": TaskHistoryStatusEnum.SUCCESS.value},
        )
        client = _authed_crud_client(
            _crud_router(list_service_type=ServiceTypeEnum.MYSQL),
            tasks_api,
            regular_user,
        )

        response = client.get(
            f"{_CRUD_BASE_URL}/",
            params={"service_type": ServiceTypeEnum.MYSQL.value},
        )

        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()] == ["t1"]

    def test_paginated_service_type_mismatch_short_circuits_to_empty_envelope(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a paginated mismatch returns an empty ``PaginatedResponse``, not a list."""
        tasks_api = _make_tasks_api(
            list_items=[_task_dict("t1")],
            list_total=5,
            latest_statuses={"t1": TaskHistoryStatusEnum.SUCCESS.value},
        )
        router = _crud_router(
            list_service_type=ServiceTypeEnum.MYSQL,
            pagination_dep=make_pagination_dep(max_limit=50),
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.get(
            f"{_CRUD_BASE_URL}/",
            params={"service_type": ServiceTypeEnum.POSTGRESQL.value},
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0
        tasks_api.get.assert_not_awaited()


class TestDeriveCrudRoutesDetailBuilder:
    """Cover the ``detail_response_builder`` override and its fallback."""

    def test_detail_uses_detail_builder_model(self) -> None:
        """Assert a detail builder gives the detail route its own response model."""
        router = _crud_router(detail_response_builder=_build_synthetic_detail_response)
        detail_route = _route_for(router, "/{task_name}", "GET")
        list_route = _route_for(router, "/", "GET")

        assert detail_route.response_model is _SyntheticDetailResponse
        assert list_route.response_model == list[_SyntheticTaskResponse]

    def test_detail_model_falls_back_to_list_model(self) -> None:
        """Assert the detail route reuses the list model when no detail builder is set."""
        detail_route = _route_for(_crud_router(), "/{task_name}", "GET")

        assert detail_route.response_model is _SyntheticTaskResponse

    def test_detail_response_model_overrides_inference(self) -> None:
        """Assert an explicit ``detail_response_model`` wins over builder inference."""
        router = _crud_router(
            detail_response_builder=_build_synthetic_detail_response,
            detail_response_model=_SyntheticCreateResponse,
        )
        detail_route = _route_for(router, "/{task_name}", "GET")

        assert detail_route.response_model is _SyntheticCreateResponse

    def test_create_uses_detail_model_when_detail_builder_set(self) -> None:
        """Assert create renders like detail when a detail builder is set, no create builder."""
        router = _crud_router(detail_response_builder=_build_synthetic_detail_response)
        create_route = _route_for(router, "/", "POST")
        list_route = _route_for(router, "/", "GET")

        assert create_route.response_model is _SyntheticDetailResponse
        assert list_route.response_model == list[_SyntheticTaskResponse]

    def test_detail_binds_context_provider_result(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the detail handler binds the once-awaited context into the builder."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict_created_by("t1", _CONTEXT_USER_ID),
            history_items=[{"status": TaskHistoryStatusEnum.SUCCESS.value}],
        )
        router = _crud_router(
            response_builder=_build_context_response,
            context_provider=_context_provider,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.get(f"{_CRUD_BASE_URL}/t1")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["resolved_by"] == "Alice"


class TestDeriveCrudRoutesPartialBuilder:
    """Cover builders wrapped in :func:`functools.partial`."""

    def test_partial_response_builder_resolves_model(self) -> None:
        """Assert a partial-wrapped builder constructs and resolves its model."""
        router = _crud_router(
            response_builder=functools.partial(_build_synthetic_response)
        )
        list_route = _route_for(router, "/", "GET")

        assert list_route.response_model == list[_SyntheticTaskResponse]

    def test_partial_detail_builder_resolves_model(self) -> None:
        """Assert a partial-wrapped detail builder constructs and resolves its model."""
        router = _crud_router(
            detail_response_builder=functools.partial(_build_synthetic_detail_response)
        )
        detail_route = _route_for(router, "/{task_name}", "GET")

        assert detail_route.response_model is _SyntheticDetailResponse


class TestDeriveCrudRoutesCreateExtraDeps:
    """Cover the per-route ``create_extra_deps`` splat on the create route."""

    def test_create_route_carries_extra_dep(self) -> None:
        """Assert a supplied ``create_extra_deps`` entry rides the create route."""
        router = _crud_router(create_extra_deps=(Depends(_marker_dep),))
        route = _route_for(router, "/", "POST")

        assert _marker_dep in {dep.dependency for dep in route.dependencies}

    def test_create_route_keeps_api_authenticated_with_extra_dep(self) -> None:
        """Assert the standard auth guard is preserved alongside the extra dep."""
        router = _crud_router(create_extra_deps=(Depends(_marker_dep),))
        route = _route_for(router, "/", "POST")
        callables = {dep.dependency for dep in route.dependencies}

        assert get_current_user in callables
        assert _marker_dep in callables


class TestDeriveCrudRoutesCreateContext:
    """Cover the create handler's context binding into the active builder."""

    def test_context_binds_into_distinct_create_builder(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the context binds into a distinct ``create_response_builder``.

        Edge case: ``create_response_builder`` set **and** a context provider active
        — the provider's result must reach the create-specific builder, not only the
        list/detail builder.
        """
        tasks_api = _make_tasks_api(
            created_task=_task_dict_created_by("new-task", _CONTEXT_USER_ID)
        )
        router = _crud_router(
            response_builder=_build_context_response,
            create_response_builder=_build_context_create_response,
            context_provider=_context_provider,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["resolved_by"] == "Alice"

    def test_contextless_builder_with_provider_rejected_at_registration(self) -> None:
        """Assert a context-less builder with a provider fails fast at registration."""
        with pytest.raises(TypeError, match="context"):
            _crud_router(context_provider=_context_provider)

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(aiohttp.ClientConnectionError(), id="connection-error"),
            pytest.param(TimeoutError(), id="timeout"),
            pytest.param(HTTPBadGatewayException(), id="provider-http-error"),
            pytest.param(RuntimeError("no provider"), id="runtime-error"),
            pytest.param(TypeError("bad payload"), id="type-error"),
        ],
    )
    def test_create_keeps_the_raw_id_when_the_user_listing_fails(
        self,
        regular_user: CasdoorUser,
        provider_users: AsyncMock,
        failure: Exception,
    ) -> None:
        """Assert create still writes and degrades to the raw id on a provider outage.

        The real default provider, ``get_username_mapping``, turns these listing
        failures into an empty map, so the response renders the stored id; the
        upstream write has already landed by the time the map is fetched.
        """
        provider_users.side_effect = failure
        tasks_api = _make_tasks_api(
            created_task=_task_dict_created_by("new-task", _CONTEXT_USER_ID)
        )
        router = _crud_router(
            context_provider=get_username_mapping,
            response_builder=_build_context_response,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.post(f"{_CRUD_BASE_URL}/", json={"name": "new-task"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["resolved_by"] == _CONTEXT_USER_ID
        tasks_api.post.assert_awaited_once()
        assert tasks_api.post.await_args.args[0] == "/"

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("no provider"), TypeError("bad payload")],
        ids=["runtime-error", "type-error"],
    )
    def test_update_keeps_the_raw_id_when_the_user_listing_fails(
        self,
        regular_user: CasdoorUser,
        provider_users: AsyncMock,
        failure: Exception,
    ) -> None:
        """Assert the derived update answers 200 with the raw id after the write."""
        provider_users.side_effect = failure
        task = _task_dict_created_by("t1", _CONTEXT_USER_ID)
        tasks_api = _make_tasks_api(detail_task=task)
        tasks_api.put.return_value = task
        router = _crud_router(
            update_enabled=True,
            context_provider=get_username_mapping,
            response_builder=_build_context_response,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        response = client.put(f"{_CRUD_BASE_URL}/t1", json={"name": "t1"})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["resolved_by"] == _CONTEXT_USER_ID
        tasks_api.put.assert_awaited_once()

    def test_a_remembered_listing_failure_answers_like_a_live_one(
        self, regular_user: CasdoorUser, provider_users: AsyncMock
    ) -> None:
        """Assert a request inside the failure window matches the failing one.

        Both answer 200 with the raw id, and only the first waits on the provider.
        """
        provider_users.side_effect = TimeoutError()
        tasks_api = _make_tasks_api(
            detail_task=_task_dict_created_by("t1", _CONTEXT_USER_ID)
        )
        router = _crud_router(
            context_provider=get_username_mapping,
            response_builder=_build_context_response,
        )
        client = _authed_crud_client(router, tasks_api, regular_user)

        live = client.get(f"{_CRUD_BASE_URL}/t1")
        remembered = client.get(f"{_CRUD_BASE_URL}/t1")

        assert live.status_code == remembered.status_code == status.HTTP_200_OK
        assert live.json() == remembered.json()
        assert remembered.json()["resolved_by"] == _CONTEXT_USER_ID
        provider_users.assert_awaited_once()


# ── derive_execute_route() helper ───────────────────────────────────────


_EXECUTE_PREFIX = "/test-derive-execute"
_EXECUTE_BASE_URL = f"/api/apps{_EXECUTE_PREFIX}"
_EXECUTE_TASK_ID = 77
_SYNTHETIC_TASK_DEP = Annotated[Task, Depends(make_task_dep(_SYNTHETIC_OWNER))]


class _SyntheticExecuteWrite(BaseModel):
    """Represent the execute request body for the synthetic execute plugin."""

    chain_on_failure: bool | None = None


class _SyntheticExecutionResponse(BaseModel):
    """Represent the execute response for the synthetic execute plugin."""

    task_name: str
    task_id: int | None = None
    status: TaskHistoryStatusEnum
    created_at: datetime


def _marker_dep() -> None:
    """Stand in for a caller-supplied ``extra_deps`` guard in tests."""


def _execute_response_dict(task_id: int | None = _EXECUTE_TASK_ID) -> dict:
    """Return a ``TaskHistoryResponse``-shaped upstream payload for execute tests.

    ``status`` and ``created_at`` are pinned to values ``TaskHistoryResponse``
    would not itself supply (``PENDING`` and ``utc_now()``), so an assertion on
    them distinguishes a field forwarded from upstream from a defaulted one.
    """
    return {
        "id": task_id,
        "execution_request": {"task": "t1", "target": "host"},
        "task": _task_dict("t1"),
        "status": EXECUTE_STATUS.value,
        "created_at": EXECUTE_CREATED_AT,
    }


def _execute_router(**overrides: object) -> APIRouter:
    """Register one ``derive_execute_route`` on a fresh router with synthetic defaults."""
    router = APIRouter()
    kwargs = {
        "task_dep": _SYNTHETIC_TASK_DEP,
        "write_model": _SyntheticExecuteWrite,
        "response_model": _SyntheticExecutionResponse,
    }
    kwargs.update(overrides)
    derive_execute_route(router, **kwargs)
    return router


def _authed_execute_client(
    router: APIRouter, tasks_api: AsyncMock, user: CasdoorUser
) -> TestClient:
    """Mount ``router`` in a production-shape app with auth + Tasks-API overrides."""
    app = _mount_plugin_router(router, _EXECUTE_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tasks_api] = lambda: tasks_api
    return TestClient(app, raise_server_exceptions=False)


class TestDeriveExecuteRouteComposition:
    """Inspect the registered execute route in isolation, without HTTP."""

    def test_registers_post_execute_route(self) -> None:
        """Assert the helper registers a single ``POST /{task_name}/execute`` route."""
        route = _route_for(_execute_router(), "/{task_name}/execute", "POST")

        assert route.methods == {"POST"}

    def test_execute_route_pins_201(self) -> None:
        """Assert the execute route sets ``status_code=201``."""
        route = _route_for(_execute_router(), "/{task_name}/execute", "POST")

        assert route.status_code == status.HTTP_201_CREATED

    def test_execute_route_response_model_is_response_model(self) -> None:
        """Assert the route's response model is the supplied ``response_model``."""
        route = _route_for(_execute_router(), "/{task_name}/execute", "POST")

        assert route.response_model is _SyntheticExecutionResponse

    def test_omitted_models_use_framework_defaults(self) -> None:
        """Assert omitting execute models uses the framework defaults."""
        router = APIRouter()
        derive_execute_route(router, task_dep=_SYNTHETIC_TASK_DEP)
        route = _route_for(router, "/{task_name}/execute", "POST")

        assert route.response_model is TaskExecutionResponse
        assert inspect.signature(route.endpoint).parameters["body"].annotation is (
            TaskExecuteWrite
        )

    def test_route_declares_standard_guards(self) -> None:
        """Assert the route declares ``IsApiAuthenticated`` + the conflict guard."""
        route = _route_for(_execute_router(), "/{task_name}/execute", "POST")
        callables = {d.dependency for d in route.dependencies}

        assert get_current_user in callables
        assert check_for_conflicted_running_tasks in callables

    def test_extra_deps_appended_to_standard_guards(self) -> None:
        """Assert ``extra_deps`` is appended without dropping the standard guards."""
        router = _execute_router(extra_deps=[Depends(_marker_dep)])
        route = _route_for(router, "/{task_name}/execute", "POST")
        callables = {d.dependency for d in route.dependencies}

        assert get_current_user in callables
        assert check_for_conflicted_running_tasks in callables
        assert _marker_dep in callables

    def test_metadata_override_sets_route_name(self) -> None:
        """Assert passing ``name`` overrides the route name (drives operationId)."""
        router = _execute_router(name="custom_api_execute")
        route = _route_for(router, "/{task_name}/execute", "POST")

        assert route.name == "custom_api_execute"

    def test_metadata_default_route_name_is_inner_handler(self) -> None:
        """Assert the route name defaults to the inner ``execute`` handler name."""
        route = _route_for(_execute_router(), "/{task_name}/execute", "POST")

        assert route.name == "execute"

    def test_second_call_raises_value_error(self) -> None:
        """Assert registering a second execute route on the same router fails."""
        router = _execute_router()

        with pytest.raises(ValueError, match="derive_execute_route"):
            derive_execute_route(
                router,
                task_dep=_SYNTHETIC_TASK_DEP,
                write_model=_SyntheticExecuteWrite,
                response_model=_SyntheticExecutionResponse,
            )

    def test_second_call_raises_value_error_on_prefixed_router(self) -> None:
        """Assert the duplicate guard fires when the router carries a prefix."""
        router = APIRouter(prefix="/plugin-prefix")
        derive_execute_route(
            router,
            task_dep=_SYNTHETIC_TASK_DEP,
            write_model=_SyntheticExecuteWrite,
            response_model=_SyntheticExecutionResponse,
        )

        with pytest.raises(ValueError, match="derive_execute_route"):
            derive_execute_route(
                router,
                task_dep=_SYNTHETIC_TASK_DEP,
                write_model=_SyntheticExecuteWrite,
                response_model=_SyntheticExecutionResponse,
            )

    def test_non_basemodel_write_model_raises_type_error(self) -> None:
        """Assert a non-BaseModel ``write_model`` is rejected at registration."""
        with pytest.raises(TypeError, match="BaseModel"):
            _execute_router(write_model=object)

    def test_non_basemodel_response_model_raises_type_error(self) -> None:
        """Assert a non-BaseModel ``response_model`` is rejected at registration."""
        with pytest.raises(TypeError, match="BaseModel"):
            _execute_router(response_model=object)


class TestDeriveExecuteRouteOverHttp:
    """Exercise the registered execute route over real HTTP."""

    def test_execute_201_forwards_body(self, regular_user: CasdoorUser) -> None:
        """Assert a valid authed POST returns 201 and forwards the exclude-none body."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_items=[],
            created_task=_execute_response_dict(),
        )
        client = _authed_execute_client(_execute_router(), tasks_api, regular_user)

        response = client.post(
            f"{_EXECUTE_BASE_URL}/t1/execute", json={"chain_on_failure": True}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {
            "task_name": "t1",
            "task_id": _EXECUTE_TASK_ID,
            "status": EXECUTE_STATUS.value,
            "created_at": EXECUTE_CREATED_AT,
        }
        tasks_api.post.assert_awaited_once_with(
            "/execute/t1", json={"chain_on_failure": True}
        )

    def test_execute_201_empty_body_forwards_empty_json(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert an empty body forwards ``json={}`` (all fields excluded as None)."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_items=[],
            created_task=_execute_response_dict(),
        )
        client = _authed_execute_client(_execute_router(), tasks_api, regular_user)

        response = client.post(f"{_EXECUTE_BASE_URL}/t1/execute", json={})

        assert response.status_code == status.HTTP_201_CREATED
        tasks_api.post.assert_awaited_once_with("/execute/t1", json={})

    def test_execute_404_for_unknown_task(self, regular_user: CasdoorUser) -> None:
        """Assert an owner mismatch on the resolved task yields 404."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1", owner="BACKUPS"),
            history_items=[],
        )
        client = _authed_execute_client(_execute_router(), tasks_api, regular_user)

        response = client.post(f"{_EXECUTE_BASE_URL}/t1/execute", json={})

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_execute_409_when_task_already_running(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the conflict guard rejects a task with a running history with 409."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"),
            history_items=[{"status": TaskHistoryStatusEnum.RUNNING.value}],
        )
        client = _authed_execute_client(_execute_router(), tasks_api, regular_user)

        response = client.post(f"{_EXECUTE_BASE_URL}/t1/execute", json={})

        assert response.status_code == status.HTTP_409_CONFLICT
        tasks_api.post.assert_not_awaited()

    def test_execute_401_when_unauthenticated(self) -> None:
        """Assert the execute route returns a JSON 401 without an auth override."""
        tasks_api = _make_tasks_api(detail_task=_task_dict("t1"), history_items=[])
        app = _mount_plugin_router(_execute_router(), _EXECUTE_PREFIX)
        app.dependency_overrides[get_tasks_api] = lambda: tasks_api
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"{_EXECUTE_BASE_URL}/t1/execute", json={}, follow_redirects=False
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


class TestDeriveExecuteRouteOpenApi:
    """Assert the execute route's generated OpenAPI carries the expected shape."""

    def _openapi_for(self, router: APIRouter, regular_user: CasdoorUser) -> dict:
        """Return the generated OpenAPI for an app mounting ``router``."""
        tasks_api = _make_tasks_api()
        client = _authed_execute_client(router, tasks_api, regular_user)
        return client.get("/openapi.json").json()

    def test_request_body_schema_is_write_model(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the requestBody schema references the supplied ``write_model``."""
        spec = self._openapi_for(_execute_router(), regular_user)
        operation = spec["paths"][f"{_EXECUTE_BASE_URL}/{{task_name}}/execute"]["post"]
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]

        assert ref.endswith("_SyntheticExecuteWrite")

    def test_metadata_override_sets_description(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a supplied ``description`` becomes the operation description."""
        router = _execute_router(description="Execute the synthetic task.")
        spec = self._openapi_for(router, regular_user)
        operation = spec["paths"][f"{_EXECUTE_BASE_URL}/{{task_name}}/execute"]["post"]

        assert operation["description"] == "Execute the synthetic task."

    def test_metadata_default_description_is_closure_docstring(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the default description falls back to the inner handler docstring."""
        spec = self._openapi_for(_execute_router(), regular_user)
        operation = spec["paths"][f"{_EXECUTE_BASE_URL}/{{task_name}}/execute"]["post"]

        assert operation["description"] == (
            "Resolve, dispatch, and wrap a standard task execution."
        )


# ── derive_cascade_create_route() helper ────────────────────────────────


_CASCADE_PREFIX = "/test-derive-cascade-create"
_CASCADE_BASE_URL = f"/api/apps{_CASCADE_PREFIX}"


class _SyntheticCreateBody(BaseModel):
    """Represent the create request body for the synthetic cascade plugin."""

    task_name: str


class _SyntheticCascadeResponse(BaseModel):
    """Represent the create response (with a connectivity slot) for the plugin."""

    task_name: str
    connectivity_warning: ConnectivityWarning | None = None


class _SyntheticCreateResponseNoConnectivity(BaseModel):
    """Represent a create response lacking the ``connectivity_warning`` field."""

    task_name: str


async def _synthetic_cascade(tasks_api: RemoteAPI, parent_write: TaskWrite) -> None:
    """Send the (already-stamped) synthetic parent write at cascade time."""
    await tasks_api.post("/", json=parent_write.model_dump())


async def _build_synthetic_cascade_plan(
    body: _SyntheticCreateBody,
) -> CascadeCreatePlan:
    """Build a synthetic ``CascadeCreatePlan`` carrying its cascade closure."""
    parent_write = GeneratedTaskFactory.build(
        name=body.task_name,
        backend=TaskBackendEnum.NOMAD,
        data={"meta": {}},
    )
    return CascadeCreatePlan(
        parent_write=parent_write,
        form=body,
        cascade=lambda api: _synthetic_cascade(api, parent_write),
    )


_SyntheticCascadePlan = Annotated[
    CascadeCreatePlan, Depends(_build_synthetic_cascade_plan)
]


async def _get_synthetic_task(task_name: str, tasks_api: RemoteAPI) -> Task:
    """Fetch and validate the created synthetic parent task."""
    fetched = await tasks_api.get(f"/{task_name}")
    return Task.model_validate(fetched)


async def _build_synthetic_cascade_response(
    task: Task, tasks_api: RemoteAPI
) -> _SyntheticCascadeResponse:
    """Render the synthetic create response from the refetched task."""
    return _SyntheticCascadeResponse(task_name=task.name)


def _cascade_router(**overrides: object) -> APIRouter:
    """Register one ``derive_cascade_create_route`` on a fresh router."""
    router = APIRouter()
    kwargs = {
        "create_plan": _SyntheticCascadePlan,
        "get_task": _get_synthetic_task,
        "response_builder": _build_synthetic_cascade_response,
        "response_model": _SyntheticCascadeResponse,
    }
    kwargs.update(overrides)
    derive_cascade_create_route(router, **kwargs)
    return router


def _authed_cascade_client(
    router: APIRouter, tasks_api: AsyncMock, user: CasdoorUser
) -> TestClient:
    """Mount ``router`` in a production-shape app with auth + Tasks-API overrides."""
    app = _mount_plugin_router(router, _CASCADE_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tasks_api] = lambda: tasks_api
    return TestClient(app, raise_server_exceptions=False)


class TestDeriveCascadeCreateRouteComposition:
    """Inspect the registered cascade create route in isolation, without HTTP."""

    def test_registers_post_create_route(self) -> None:
        """Assert the helper registers a single ``POST /`` route."""
        route = _route_for(_cascade_router(), "/", "POST")

        assert route.methods == {"POST"}

    def test_create_route_pins_201(self) -> None:
        """Assert the create route defaults to ``status_code=201``."""
        route = _route_for(_cascade_router(), "/", "POST")

        assert route.status_code == status.HTTP_201_CREATED

    def test_status_code_override(self) -> None:
        """Assert an explicit ``status_code`` overrides the 201 default."""
        route = _route_for(_cascade_router(status_code=status.HTTP_200_OK), "/", "POST")

        assert route.status_code == status.HTTP_200_OK

    def test_response_model_is_response_model(self) -> None:
        """Assert the route's response model is the supplied ``response_model``."""
        route = _route_for(_cascade_router(), "/", "POST")

        assert route.response_model is _SyntheticCascadeResponse

    def test_metadata_override_sets_route_name(self) -> None:
        """Assert passing ``name`` overrides the route name (drives operationId)."""
        route = _route_for(_cascade_router(name="synthetic_api_create"), "/", "POST")

        assert route.name == "synthetic_api_create"

    def test_metadata_default_route_name_is_inner_handler(self) -> None:
        """Assert the route name defaults to the inner ``create`` handler name."""
        route = _route_for(_cascade_router(), "/", "POST")

        assert route.name == "create"

    def test_route_declares_no_per_route_guards(self) -> None:
        """Assert the create route carries no per-route deps (auth inherited from mount)."""
        route = _route_for(_cascade_router(), "/", "POST")

        assert route.dependencies == []

    def test_extra_deps_appended(self) -> None:
        """Assert ``extra_deps`` are attached as the route's dependencies."""
        route = _route_for(
            _cascade_router(extra_deps=[Depends(_marker_dep)]), "/", "POST"
        )
        callables = {d.dependency for d in route.dependencies}

        assert _marker_dep in callables

    def test_second_call_raises_value_error(self) -> None:
        """Assert registering a second create route on the same router fails."""
        router = _cascade_router()

        with pytest.raises(ValueError, match="derive_cascade_create_route"):
            derive_cascade_create_route(
                router,
                create_plan=_SyntheticCascadePlan,
                get_task=_get_synthetic_task,
                response_builder=_build_synthetic_cascade_response,
                response_model=_SyntheticCascadeResponse,
            )

    def test_second_call_raises_value_error_on_prefixed_router(self) -> None:
        """Assert the duplicate guard fires when the router carries a prefix."""
        router = APIRouter(prefix="/plugin-prefix")
        derive_cascade_create_route(
            router,
            create_plan=_SyntheticCascadePlan,
            get_task=_get_synthetic_task,
            response_builder=_build_synthetic_cascade_response,
            response_model=_SyntheticCascadeResponse,
        )

        with pytest.raises(ValueError, match="derive_cascade_create_route"):
            derive_cascade_create_route(
                router,
                create_plan=_SyntheticCascadePlan,
                get_task=_get_synthetic_task,
                response_builder=_build_synthetic_cascade_response,
                response_model=_SyntheticCascadeResponse,
            )

    def test_non_basemodel_response_model_raises_type_error(self) -> None:
        """Assert a non-BaseModel ``response_model`` is rejected at registration."""
        with pytest.raises(TypeError, match="BaseModel"):
            _cascade_router(response_model=object)

    def test_connectivity_check_requires_connectivity_field(self) -> None:
        """Assert ``connectivity_check`` needs a ``connectivity_warning`` field."""
        with pytest.raises(TypeError, match="connectivity_warning"):
            _cascade_router(
                connectivity_check=True,
                response_model=_SyntheticCreateResponseNoConnectivity,
            )


class TestDeriveCascadeCreateRouteOverHttp:
    """Exercise the registered cascade create route over real HTTP."""

    def test_create_201_stamps_form_and_runs_cascade(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a valid authed POST returns 201 and the parent POST carries ``_form``."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"), created_task=_task_dict("t1")
        )
        client = _authed_cascade_client(_cascade_router(), tasks_api, regular_user)

        response = client.post(f"{_CASCADE_BASE_URL}/", json={"task_name": "t1"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {"task_name": "t1", "connectivity_warning": None}
        tasks_api.post.assert_awaited_once()
        posted = tasks_api.post.await_args
        assert posted.args[0] == "/"
        assert posted.kwargs["json"]["data"][RESERVED_FORM_KEY] == {"task_name": "t1"}

    def test_connectivity_check_false_skips_probe(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert ``check_connectivity=false`` leaves ``connectivity_warning`` unset."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"), created_task=_task_dict("t1")
        )
        client = _authed_cascade_client(
            _cascade_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(
            f"{_CASCADE_BASE_URL}/?check_connectivity=false", json={"task_name": "t1"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is None

    def test_connectivity_check_default_probes_without_meta(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the default connectivity probe short-circuits when meta lacks keys."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"), created_task=_task_dict("t1")
        )
        client = _authed_cascade_client(
            _cascade_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CASCADE_BASE_URL}/", json={"task_name": "t1"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is None

    def test_connectivity_check_populates_warning_on_probe_failure(
        self, regular_user: CasdoorUser, mocker
    ) -> None:
        """Attach the probe warning to the create response when the probe fails."""
        warning = ConnectivityWarning(
            target="node-1", service_type="mysql", message="unreachable"
        )
        probe = _patch_probe(mocker, warning)
        tasks_api = _make_tasks_api(
            detail_task=_task_dict_with_meta("t1", _CONNECTIVITY_META),
            created_task=_task_dict("t1"),
        )
        client = _authed_cascade_client(
            _cascade_router(connectivity_check=True), tasks_api, regular_user
        )

        response = client.post(f"{_CASCADE_BASE_URL}/", json={"task_name": "t1"})

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] == {
            "target": "node-1",
            "service_type": "mysql",
            "message": "unreachable",
            "task_history_id": None,
        }
        probe.assert_awaited_once()

    def test_create_401_when_unauthenticated(self) -> None:
        """Assert the create route returns a JSON 401 without an auth override."""
        tasks_api = _make_tasks_api(
            detail_task=_task_dict("t1"), created_task=_task_dict("t1")
        )
        app = _mount_plugin_router(_cascade_router(), _CASCADE_PREFIX)
        app.dependency_overrides[get_tasks_api] = lambda: tasks_api
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"{_CASCADE_BASE_URL}/", json={"task_name": "t1"}, follow_redirects=False
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


class TestDeriveCascadeCreateRouteOpenApi:
    """Assert the cascade create route's generated OpenAPI carries the expected shape."""

    def _openapi_for(self, router: APIRouter, regular_user: CasdoorUser) -> dict:
        """Return the generated OpenAPI for an app mounting ``router``."""
        tasks_api = _make_tasks_api()
        client = _authed_cascade_client(router, tasks_api, regular_user)
        return client.get("/openapi.json").json()

    def test_request_body_schema_is_plan_body_model(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the requestBody schema references the plan dependency's body model."""
        spec = self._openapi_for(_cascade_router(), regular_user)
        operation = spec["paths"][f"{_CASCADE_BASE_URL}/"]["post"]
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]

        assert ref.endswith("_SyntheticCreateBody")

    def test_connectivity_check_query_present_when_enabled(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert the ``check_connectivity`` query param appears when enabled."""
        spec = self._openapi_for(_cascade_router(connectivity_check=True), regular_user)
        operation = spec["paths"][f"{_CASCADE_BASE_URL}/"]["post"]
        params = {p["name"] for p in operation.get("parameters", [])}

        assert "check_connectivity" in params

    def test_connectivity_check_query_absent_when_disabled(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert no ``check_connectivity`` query param when connectivity is off."""
        spec = self._openapi_for(_cascade_router(), regular_user)
        operation = spec["paths"][f"{_CASCADE_BASE_URL}/"]["post"]
        params = {p["name"] for p in operation.get("parameters", [])}

        assert "check_connectivity" not in params

    def test_metadata_override_sets_description(
        self, regular_user: CasdoorUser
    ) -> None:
        """Assert a supplied ``description`` becomes the operation description."""
        router = _cascade_router(description="Create the synthetic task group.")
        spec = self._openapi_for(router, regular_user)
        operation = spec["paths"][f"{_CASCADE_BASE_URL}/"]["post"]

        assert operation["description"] == "Create the synthetic task group."
