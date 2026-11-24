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

"""Provide the parameterized derived-router contract suite for ``TaskExecutionApp``.

:class:`DerivedRouterContractTests` is a pytest mixin a plugin test module
subclasses, binding its definition to the ``app_def`` class attribute::

    class TestChecksumsContract(DerivedRouterContractTests):
        app_def = checksums_app

pytest collects the inherited ``test_*`` methods — one per derived surface — so a
failure pinpoints the broken surface. Each method reads the contract from the
definition's knobs (``capabilities``, ``pagination``, ``connectivity_check``,
``detail_path_param``, ``capabilities_provider``) rather than hard-coded paths:
a disabled verb asserts route *absence* via route-table introspection, not an
ambiguous HTTP status. The kit's ``conftest.py`` supplies the fixtures the
methods request (``contract_client``, ``unauthenticated_contract_client``,
``mock_task_api``, ``mock_inventory_api``), each reading ``request.cls.app_def``.
"""

from collections.abc import Iterator
from types import NoneType, UnionType
from typing import Any, ClassVar, get_args, get_origin, Union
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import APIRouter, FastAPI, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pytest_mock import MockerFixture

from app.core.auth.providers.casdoor.models import CasdoorUser
from app.extensions.apps.framework import ConnectivityWarning, TaskExecuteWrite
from app.extensions.apps.framework.apps import (
    NO_PAGINATION,
    TaskExecutionApp,
    UNGUARDED,
)
from app.extensions.apps.framework.conformance import CAPABILITY_RENDERED_CONTROLS
from app.extensions.apps.framework.form_dsl import (
    find_ref_marker,
    HostRef,
    SchemaRef,
    ServiceRef,
    TableRef,
)
from app.extensions.apps.framework.spec import RESERVED_FORM_KEY
from app.extensions.connectivity import CONNECTIVITY_META_HOST_KEY
from app.extensions.deps import (
    get_current_user,
    get_inventory_api,
    get_tasks_api,
    IsApiAuthenticated,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum
from tests.app.extensions.apps.framework.kit import (
    EXECUTE_CREATED_AT,
    EXECUTE_STATUS,
    SEEDED_TASK_NAME,
    SYNTH_CREATED_BY,
    SYNTH_CREATED_BY_NAME,
    SYNTH_EXECUTOR_HOST,
)
from tests.app.extensions.unknown_body_field import (
    assert_unknown_field_rejected,
    UNKNOWN_FIELD,
)
from tests.app.factories import (
    CasdoorUserFactory,
    MOCK_CREATED_SCHEMA_ID,
    MOCK_CREATED_SERVICE_ID,
    MOCK_CREATED_TABLE_ID,
)

_NEW_TASK_NAME = "contract-new-task"
_UNKNOWN_TASK_NAME = "contract-unknown-task"
_CONFLICT_TASK_NAME = "contract-conflict-task"
_CONNECTIVITY_PATCH_TARGET = (
    "app.extensions.apps.framework.connectivity.record_connectivity_warning"
)

_REF_MOCK_IDS = {
    ServiceRef: MOCK_CREATED_SERVICE_ID,
    SchemaRef: MOCK_CREATED_SCHEMA_ID,
    TableRef: MOCK_CREATED_TABLE_ID,
}

_MOUNTED: dict[int, tuple[TaskExecutionApp, FastAPI]] = {}


def app_base_url(app_def: TaskExecutionApp) -> str:
    """Return the production-shape mount base for ``app_def``'s derived router.

    :param app_def: The app definition whose ``uri_path`` sets the mount prefix.
    :return: The ``/api/apps{uri_path}`` base the contract client requests.
    """
    return f"/api/apps{app_def.uri_path}"


def routes_of(app_def: TaskExecutionApp) -> set[tuple[str, str]]:
    """Return the ``(path, method)`` pairs registered on the app's router.

    :param app_def: The app definition whose derived router is introspected.
    :return: Every ``(route.path, method)`` pair, used for absence/presence checks.
    """
    return {
        (route.path, method)
        for route in app_def.api_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def detail_route_path(app_def: TaskExecutionApp) -> str:
    """Return the detail/update/delete route template for ``app_def``.

    :param app_def: The app definition whose ``detail_path_param`` names the
        single path segment the detail, update, and delete routes capture.
    :return: The ``/{detail_path_param}`` route template.
    """
    return f"/{{{app_def.detail_path_param}}}"


def extra_route_paths(app_def: TaskExecutionApp) -> set[tuple[str, str]]:
    """Return the ``(path, method)`` pairs the app keeps custom via ``extra_routes``.

    A hybrid app disables a capability yet still serves that verb from a
    hand-written route mounted through ``extra_routes`` (for example a cascade
    create or a satellite-resolving detail). The route-absence assertions use this
    to tell a legitimately-custom route from a leaked derived one.

    :param app_def: The app definition whose ``extra_routes`` are introspected.
    :return: Every ``(route.path, method)`` pair contributed by ``extra_routes``.
    """
    return {
        (route.path, method)
        for extra in app_def.extra_routes
        for route in extra.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def mount_app(app_def: TaskExecutionApp) -> FastAPI:
    """Mount the app's derived router under the production-shape router tree.

    :param app_def: The app definition whose ``api_router`` is mounted under
        ``/api/apps{uri_path}`` behind the ``IsApiAuthenticated`` router guard.
    :return: A fresh ``FastAPI`` app carrying only this definition's routes.
    """
    apps_router = APIRouter(prefix="/apps")
    apps_router.include_router(app_def.api_router, prefix=app_def.uri_path)
    api_router = APIRouter(prefix="/api", dependencies=[IsApiAuthenticated])
    api_router.include_router(apps_router)
    app = FastAPI()
    app.include_router(api_router)

    @app.get("/login", name="login")
    async def _login() -> dict[str, bool]:
        return {"ok": True}

    return app


def mount_app_shared(app_def: TaskExecutionApp) -> FastAPI:
    """Return a per-process cached mount for a long-lived app definition.

    :func:`mount_app` re-derives every route object on each call, which dominates
    contract-suite setup. Cache the mount per definition so each xdist worker
    mounts a given definition at most once. The cache entry retains ``app_def``
    itself, so its ``id()`` can never be recycled onto a different object.

    Only for definitions that outlive a single test — a class attribute or a
    module constant. A definition built inside a test body must use
    :func:`mount_app`, which mounts fresh every call.

    Callers borrow the returned app's ``dependency_overrides`` for the duration of
    one test and must clear them on teardown, since every other test bound to the
    same definition shares that mapping. Borrow through
    :func:`borrow_shared_mount` rather than calling this directly, so a second
    concurrent borrower is rejected instead of stomping the first.

    :param app_def: The long-lived app definition to mount.
    :return: The cached ``FastAPI`` app carrying this definition's routes.
    """
    cached = _MOUNTED.get(id(app_def))
    if cached is None:
        cached = (app_def, mount_app(app_def))
        _MOUNTED[id(app_def)] = cached
    return cached[1]


def borrow_shared_mount(app_def: TaskExecutionApp) -> FastAPI:
    """Return the cached mount for ``app_def``, refusing a second live borrower.

    The shared app carries one ``dependency_overrides`` mapping, so two fixtures
    holding it in the same test would collide on ``get_current_user`` and the
    later setup would silently decide what *both* clients authenticate as — an
    assertion about the 401 path could then pass or fail for the wrong reason.
    Borrowing through here turns that into a setup-time error instead.

    A populated mapping always means a live borrower, never leftover state: every
    borrower clears it on teardown, which the override-leak regression test pins.

    :param app_def: The long-lived app definition to mount through the cache.
    :raises RuntimeError: If the cached app's overrides are already installed.
    :return: The cached ``FastAPI`` app, with an empty ``dependency_overrides``.
    """
    app = mount_app_shared(app_def)
    if app.dependency_overrides:
        raise RuntimeError(
            f"The shared mount for {app_def.key!r} is already lent to another "
            "contract client in this test; the two would share one "
            "dependency_overrides mapping. Request only one client fixture, or "
            "build a per-test app with build_contract_client()."
        )
    return app


def _install_contract_overrides(
    app: FastAPI,
    *,
    user: CasdoorUser,
    tasks_api: Any,
    inventory_api: Any | None,
) -> TestClient:
    """Install the boundary overrides on ``app`` and return a client for it.

    :param app: The mounted app whose ``dependency_overrides`` receive the mocks.
    :param user: The authenticated user the auth dep resolves to.
    :param tasks_api: The Tasks-API boundary mock installed for ``get_tasks_api``.
    :param inventory_api: The Inventory-API boundary mock installed for
        ``get_inventory_api``; omitted when the app resolves no references.
    :return: A bare ``TestClient`` (never a context manager — lifespan trap).
    """
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tasks_api] = lambda: tasks_api
    if inventory_api is not None:
        app.dependency_overrides[get_inventory_api] = lambda: inventory_api
    return TestClient(app, raise_server_exceptions=False)


def build_contract_client(
    app_def: TaskExecutionApp,
    *,
    user: CasdoorUser,
    tasks_api: Any,
    inventory_api: Any | None = None,
) -> TestClient:
    """Mount ``app_def`` with auth, Tasks-API, and Inventory-API overrides.

    Overrides only boundary deps — never the create-body dep — so the real
    FastAPI body-parsing graph runs for create requests. Each call builds a fresh
    ``FastAPI``, so the overrides never leak across tests. Use this for a
    definition built inside the test body; a definition that outlives the test
    goes through :func:`shared_contract_client` instead.

    :param app_def: The app definition to mount.
    :param user: The authenticated user the auth dep resolves to.
    :param tasks_api: The Tasks-API boundary mock installed for ``get_tasks_api``.
    :param inventory_api: The Inventory-API boundary mock installed for
        ``get_inventory_api``; omitted when the app resolves no references.
    :return: A bare ``TestClient`` (never a context manager — lifespan trap).
    """
    return _install_contract_overrides(
        mount_app(app_def),
        user=user,
        tasks_api=tasks_api,
        inventory_api=inventory_api,
    )


def shared_contract_client(
    app_def: TaskExecutionApp,
    *,
    user: CasdoorUser,
    tasks_api: Any,
    inventory_api: Any | None = None,
) -> Iterator[TestClient]:
    """Yield a contract client on the cached mount, clearing overrides afterwards.

    The fixture body for a definition that outlives the test: the app object is
    shared with every sibling test bound to the same definition, so this owns the
    teardown that :func:`build_contract_client`'s per-test app made unnecessary.
    Only one client may hold the shared app at a time, which
    :func:`borrow_shared_mount` enforces.

    :param app_def: The long-lived app definition to mount through the cache.
    :param user: The authenticated user the auth dep resolves to.
    :param tasks_api: The Tasks-API boundary mock installed for ``get_tasks_api``.
    :param inventory_api: The Inventory-API boundary mock installed for
        ``get_inventory_api``; omitted when the app resolves no references.
    :return: An iterator yielding a bare ``TestClient`` for the cached app.
    """
    app = borrow_shared_mount(app_def)
    try:
        yield _install_contract_overrides(
            app, user=user, tasks_api=tasks_api, inventory_api=inventory_api
        )
    finally:
        app.dependency_overrides.clear()


def select_branch(field: FieldInfo) -> type[BaseModel] | None:
    """Return the first model branch of a union field, or ``None`` for a non-union.

    Drops the ``None`` arm of an optional union (``X | Y | None``) and returns the
    first remaining ``BaseModel`` arm **in declaration order** — a deterministic
    pick that never depends on set or hash ordering, the flake class the derived
    one-of body schema was hardened against. Returns ``None`` when the field is not
    a union at all (a scalar, or a container such as ``list[ChildModel]`` whose
    ``get_args`` also yields a model but must keep its container shape), or is a
    union that mixes in a non-model arm (a collapsed ``int | str`` reference).

    :param field: The create- or branch-model field being inspected.
    :return: The first model branch to build, or ``None``.
    """
    if get_origin(field.annotation) not in (Union, UnionType):
        return None
    args = [arg for arg in get_args(field.annotation) if arg is not NoneType]
    if not args or not all(
        isinstance(arg, type) and issubclass(arg, BaseModel) for arg in args
    ):
        return None
    return args[0]


def ref_overrides(
    model: type[BaseModel], *, skip: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Return the override map pinning ``model``'s inventory references to seeded ids.

    Maps each ``ServiceRef`` / ``SchemaRef`` / ``TableRef`` field to its seeded
    ``MOCK_*_ID`` (wrapped in a one-element list when the marker declares
    ``multiple=True``), each ``HostRef`` field to ``SYNTH_EXECUTOR_HOST``, and each
    discriminated-union field to a built first-branch instance carrying its own
    recursively-resolved references — so a body whose references live inside union
    branches still resolves against the seeded mock inventory.

    Only top-level fields are reachable from a caller's ``create_body_overrides``. A
    reference nested inside a union branch is resolved by this recursion, but a
    *rule-/validator-constrained non-ref scalar* nested inside a branch has no
    override hook: the generic factory generates it freely and the body would fail to
    build if a validator or ``__form_rules__`` rule constrains it. No current one-of
    app hits this; a future one would need the constraint lifted to the branch model
    or an override mechanism that reaches into branches.

    :param model: The create or branch model whose fields are inspected.
    :param skip: Field names the caller pins itself; building them here (recursing into
        a union branch only to have the caller discard it) is wasted, so they are
        skipped.
    :return: A field-name → override-value map for ``ModelFactory.build``.
    """
    overrides: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        ref = find_ref_marker(list(field.metadata))
        if isinstance(ref, HostRef):
            overrides[name] = (
                [SYNTH_EXECUTOR_HOST] if ref.multiple else SYNTH_EXECUTOR_HOST
            )
        elif (mock_id := _REF_MOCK_IDS.get(type(ref))) is not None:
            overrides[name] = [mock_id] if ref.multiple else mock_id
        elif (branch := select_branch(field)) is not None:
            overrides[name] = ModelFactory.create_factory(branch).build(
                **ref_overrides(branch)
            )
    return overrides


def build_valid_create_body(
    app_def: TaskExecutionApp,
    *,
    task_name: str = _NEW_TASK_NAME,
    create_body_overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a valid create form body for a model-first ``app_def``.

    Generates a body over ``app_def.create_model`` via polyfactory, then overrides
    each inventory reference with its seeded ``MOCK_*_ID`` / ``SYNTH_EXECUTOR_HOST``
    — recursing into discriminated-union fields so references nested inside union
    branches resolve too — and ``task_name`` with a known value. ``create_body_overrides``
    is applied last, so a subclass always wins over a generated value; it feeds the
    ``build`` call (not only the dumped body) so a scalar a ``__form_rules__`` rule or
    a model validator constrains is pinned before validation runs. Returns ``None`` for
    a transitional ``schema=`` app, which has no ``create_model`` to introspect.

    :param app_def: The app definition whose create model drives the body.
    :param task_name: The task name to set on the generated body.
    :param create_body_overrides: Field values a subclass pins over the generated
        body (for example a form-rule- or validator-constrained scalar).
    :return: A form-field mapping, or ``None`` when no body can be derived.
    """
    model = app_def.create_model
    if model is None:
        return None
    overrides: dict[str, Any] = {
        "task_name": task_name,
        **ref_overrides(model, skip=frozenset(create_body_overrides or ())),
    }
    if create_body_overrides:
        overrides.update(create_body_overrides)
    instance = ModelFactory.create_factory(model).build(**overrides)
    return instance.model_dump(mode="json")


def post_create_body(
    client: TestClient, url: str, app_def: TaskExecutionApp, body: dict[str, Any]
) -> Any:
    """POST a create ``body`` using the encoding the definition declares.

    :param client: The contract client.
    :param url: The create route URL.
    :param app_def: The app definition whose ``create_form_encoded`` selects the
        encoding (form-urlencoded when set, JSON otherwise).
    :param body: The create form body to post.
    :return: The HTTP response.
    """
    if app_def.create_form_encoded:
        return client.post(url, data=body)
    return client.post(url, json=body)


def host_ref_field_name(app_def: TaskExecutionApp) -> str | None:
    """Return the create model's single ``HostRef`` field name, or ``None``.

    :param app_def: The app definition whose create model is scanned.
    :return: The ``HostRef`` field name, or ``None`` when the model declares none
        (or is a ``schema=`` passthrough).
    """
    model = app_def.create_model
    if model is None:
        return None
    for name, field in model.model_fields.items():
        if isinstance(find_ref_marker(list(field.metadata)), HostRef):
            return name
    return None


def build_invalid_create_body(
    app_def: TaskExecutionApp, *, create_body_overrides: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Build a create body missing one required field, to drive the create 422.

    :param app_def: The app definition whose create model drives the body.
    :param create_body_overrides: Field values a subclass pins over the generated
        body, threaded through so the body 422s on the dropped required field rather
        than on an unrelated form-rule violation.
    :return: A body with one required field dropped, or ``None`` when no body or
        no required field can be derived.
    """
    body = build_valid_create_body(app_def, create_body_overrides=create_body_overrides)
    if body is None:
        return None
    required = [
        name
        for name, field in app_def.create_model.model_fields.items()
        if field.is_required()
    ]
    if not required:
        return None
    body.pop(required[0], None)
    return body


def _list_rows(body: Any) -> list[dict[str, Any]]:
    """Return the row list from a plain-list or paginated-envelope list body.

    :param body: The decoded ``GET /`` body, either a plain list or a
        ``PaginatedResponse`` envelope.
    :return: The contained rows, regardless of pagination shape.
    """
    return body["items"] if isinstance(body, dict) else body


class DerivedRouterContractTests:
    """Assert a bound ``TaskExecutionApp``'s derived HTTP surface, knob by knob.

    Subclass and set :attr:`app_def`; pytest collects one ``test_*`` per surface.
    Each capability-gated method either exercises the enabled route or asserts the
    disabled route's absence, reading the contract from the definition.

    The seeded task's ``created_by`` resolves to :data:`SYNTH_CREATED_BY_NAME` for
    every app: the stubbed provider lists that user for an app on the default
    provider, and the synthetic apps' own provider maps the same id. An app left
    with no response context provider fails
    :meth:`test_response_context_provider_bound`, and also fails — rather than
    skips — every injected-extras test its capabilities enable.

    Set :attr:`create_body_overrides` to pin fields the generic body generator cannot
    satisfy on its own — a scalar a ``__form_rules__`` rule or a model validator
    constrains (for example a one-of app whose form rule accepts only one enum value).
    It wins over every generated value on the create and update bodies the suite posts.
    """

    app_def: ClassVar[TaskExecutionApp]
    create_body_overrides: ClassVar[dict[str, Any]] = {}

    @pytest.fixture(autouse=True)
    def _provider_lists_the_seeded_creator(self, provider_users: AsyncMock) -> None:
        """Seed the stubbed provider with the user every seeded task is created by.

        :param provider_users: The autouse user-listing stub, configured here.
        """
        provider_users.return_value = [
            CasdoorUserFactory.build(
                id=UUID(SYNTH_CREATED_BY), username=SYNTH_CREATED_BY_NAME
            )
        ]

    def test_schema_200(self, contract_client: TestClient) -> None:
        """Assert ``GET /schema`` serves the derived plugin schema."""
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK

    def test_capabilities_200(self, contract_client: TestClient) -> None:
        """Assert ``GET /capabilities`` 200s when a provider is configured."""
        if self.app_def.capabilities_provider is None:
            pytest.skip("no capabilities provider")
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/capabilities")

        assert response.status_code == status.HTTP_200_OK

    def test_capabilities_route_absent(self) -> None:
        """Assert no ``GET /capabilities`` route exists without a provider."""
        if self.app_def.capabilities_provider is not None:
            pytest.skip("capabilities provider configured")

        assert ("/capabilities", "GET") not in routes_of(self.app_def)

    def test_list_200_plain(self, contract_client: TestClient) -> None:
        """Assert an unpaginated ``GET /`` returns a plain list."""
        if self.app_def.pagination is not NO_PAGINATION:
            pytest.skip("paginated list")
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/")

        assert response.status_code == status.HTTP_200_OK
        assert isinstance(response.json(), list)

    def test_list_200_paginated(self, contract_client: TestClient) -> None:
        """Assert a paginated ``GET /`` returns a ``PaginatedResponse`` envelope."""
        if self.app_def.pagination is NO_PAGINATION:
            pytest.skip("unpaginated list")
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert {"items", "total", "offset", "limit"} <= body.keys()

    def test_detail_200(self, contract_client: TestClient) -> None:
        """Assert ``GET /{detail}`` returns the detail response for a known task."""
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_200_OK

    def test_detail_404(self, contract_client: TestClient) -> None:
        """Assert ``GET /{detail}`` 404s for an unknown task name."""
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/{_UNKNOWN_TASK_NAME}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_201(self, contract_client: TestClient, mock_task_api: Any) -> None:
        """Assert a real form POST creates a task and returns 201."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        assert mock_task_api.create_count == 1

    def test_create_stamps_form_input(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert create stamps the validated form body under ``data['_form']``."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        expected = self.app_def.create_model.model_validate(body).model_dump(
            mode="json"
        )
        assert mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY] == expected

    def test_create_route_absent(self) -> None:
        """Assert no derived ``POST /`` route exists when create is disabled."""
        if self.app_def.capabilities.create:
            pytest.skip("create capability enabled")
        if ("/", "POST") in extra_route_paths(self.app_def):
            pytest.skip("create route kept custom in extra_routes")

        assert ("/", "POST") not in routes_of(self.app_def)

    def test_create_422(self, contract_client: TestClient, mock_task_api: Any) -> None:
        """Assert a body missing a required field 422s before any upstream POST."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_invalid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable invalid create body")
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert mock_task_api.create_count == 0

    def test_create_rejects_an_unknown_field(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a valid body plus an undeclared key 422s naming it, creating nothing."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        body[UNKNOWN_FIELD] = "value"
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert_unknown_field_rejected(response)
        assert mock_task_api.create_count == 0

    def test_create_connectivity_warning(
        self, contract_client: TestClient, mocker: MockerFixture
    ) -> None:
        """Assert ``connectivity_check`` attaches the probe warning to the response."""
        if not self.app_def.connectivity_check:
            pytest.skip("connectivity check disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        mocker.patch(
            _CONNECTIVITY_PATCH_TARGET,
            new_callable=AsyncMock,
            return_value=ConnectivityWarning(
                target="db-host", service_type="mysql", message="unreachable"
            ),
        )
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["connectivity_warning"] is not None

    def test_create_requestbody_content_type(self) -> None:
        """Assert the create route's requestBody encoding matches the definition."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        if self.app_def.create_model is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        request_body = mount_app_shared(self.app_def).openapi()["paths"][
            f"{app_base_url(self.app_def)}/"
        ]["post"]["requestBody"]
        expected = (
            "application/x-www-form-urlencoded"
            if self.app_def.create_form_encoded
            else "application/json"
        )

        assert set(request_body["content"]) == {expected}

    def test_excluded_capability_control_absent_from_schema(
        self, contract_client: TestClient
    ) -> None:
        """Assert each enabled capability-rendered control is absent from the schema."""
        capabilities = self.app_def.views.capabilities
        enabled = {
            field
            for cap, field in CAPABILITY_RENDERED_CONTROLS.items()
            if capabilities is not None and getattr(capabilities, cap, False)
        }
        if not enabled:
            pytest.skip("no capability-rendered control enabled")
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_200_OK
        field_names = {
            field["name"]
            for section in response.json().get("forms") or ()
            for field in section.get("fields") or ()
        }
        assert enabled.isdisjoint(field_names)

    def test_create_threads_executor_host_to_meta_target(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a ``HostRef`` threads the submitted host to ``meta.target``.

        The executor target must equal the submitted host and stay distinct from
        the service address carried on the connectivity host key.
        """
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        host_field = host_ref_field_name(self.app_def)
        if host_field is None:
            pytest.skip("no HostRef field")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        assert body is not None
        meta = mock_task_api.last_create_payload["data"]["meta"]
        assert meta["target"] == body[host_field]
        assert meta["target"] != meta[CONNECTIVITY_META_HOST_KEY]

    def test_list_status_filter(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert ``GET /?status=`` returns only rows whose latest status matches."""
        if not self.app_def.list_filter.status:
            pytest.skip("status filter not declared")
        extra = self.app_def.list_filter.extra_params or None
        mock_task_api.seed_task(
            "contract-status-success",
            owner=self.app_def.owner,
            statuses=(TaskHistoryStatusEnum.SUCCESS,),
            data_extra=extra,
        )
        mock_task_api.seed_task(
            "contract-status-failed",
            owner=self.app_def.owner,
            statuses=(TaskHistoryStatusEnum.FAILED,),
            data_extra=extra,
        )
        base = app_base_url(self.app_def)

        response = contract_client.get(
            f"{base}/", params={"status": TaskHistoryStatusEnum.SUCCESS.value}
        )

        assert response.status_code == status.HTTP_200_OK
        names = {row["name"] for row in _list_rows(response.json())}
        assert "contract-status-success" in names
        assert "contract-status-failed" not in names

    def test_list_service_type_filter_short_circuits(
        self, contract_client: TestClient
    ) -> None:
        """Assert a mismatched ``?service_type=`` empties the list; a match lists rows."""
        if not self.app_def.list_filter.service_type:
            pytest.skip("service_type filter not declared")
        other = next(
            kind for kind in ServiceTypeEnum if kind != self.app_def.service_type
        )
        base = app_base_url(self.app_def)

        mismatch = contract_client.get(f"{base}/", params={"service_type": other.value})
        match = contract_client.get(
            f"{base}/", params={"service_type": self.app_def.service_type.value}
        )

        assert mismatch.status_code == status.HTTP_200_OK
        assert _list_rows(mismatch.json()) == []
        assert any(row["name"] == SEEDED_TASK_NAME for row in _list_rows(match.json()))

    def test_list_roots_only(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a ``roots_only`` list hides derived children (``data.parent`` set)."""
        if not self.app_def.list_filter.roots_only:
            pytest.skip("roots_only not declared")
        mock_task_api.seed_task(
            "contract-derived-child",
            owner=self.app_def.owner,
            parent=SEEDED_TASK_NAME,
            data_extra=self.app_def.list_filter.extra_params or None,
        )
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/")

        assert response.status_code == status.HTTP_200_OK
        names = {row["name"] for row in _list_rows(response.json())}
        assert SEEDED_TASK_NAME in names
        assert "contract-derived-child" not in names

    def test_list_extra_params(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a fixed ``extra_params`` upstream filter drops non-matching rows."""
        if not self.app_def.list_filter.extra_params:
            pytest.skip("no extra_params declared")
        mock_task_api.seed_task("contract-extra-mismatch", owner=self.app_def.owner)
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/")

        assert response.status_code == status.HTTP_200_OK
        names = {row["name"] for row in _list_rows(response.json())}
        assert SEEDED_TASK_NAME in names
        assert "contract-extra-mismatch" not in names

    def test_response_context_provider_bound(self) -> None:
        """Assert the app resolves actor ids through a response context provider."""
        assert self.app_def.response_context_provider is not None, (
            f"{self.app_def.name!r} binds no response_context_provider, so its actor "
            "fields render the stored user id instead of a display name"
        )

    def test_list_injects_extras_and_resolves_username(
        self, contract_client: TestClient
    ) -> None:
        """Assert list rows omit internal fields and carry the resolved username."""
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/")

        assert response.status_code == status.HTTP_200_OK
        row = next(
            row
            for row in _list_rows(response.json())
            if row["name"] == SEEDED_TASK_NAME
        )
        assert row["name"] == SEEDED_TASK_NAME
        assert "service_type" not in row
        assert "owner" not in row
        assert row["created_by"] == SYNTH_CREATED_BY_NAME

    def test_detail_injects_extras(self, contract_client: TestClient) -> None:
        """Assert ``GET /{name}`` carries the remapped username without internal fields."""
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert "service_type" not in body
        assert "owner" not in body
        assert body["created_by"] == SYNTH_CREATED_BY_NAME

    def test_detail_reflects_injected_status(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert the detail handler injects the latest status into the sync builder."""
        mock_task_api.seed_task(
            SEEDED_TASK_NAME,
            owner=self.app_def.owner,
            statuses=(TaskHistoryStatusEnum.RUNNING,),
        )
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == TaskHistoryStatusEnum.RUNNING.value

    def test_detail_returns_detail_model(self, contract_client: TestClient) -> None:
        """Assert a detail builder makes ``GET /{name}`` carry the detail-only field."""
        if self.app_def.detail_response_builder is None:
            pytest.skip("no detail response builder")
        base = app_base_url(self.app_def)

        response = contract_client.get(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_200_OK
        assert "detail_only" in response.json()

    def test_create_returns_detail_model(self, contract_client: TestClient) -> None:
        """Assert create renders like detail (carries the detail-only field)."""
        if self.app_def.detail_response_builder is None:
            pytest.skip("no detail response builder")
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        assert "detail_only" in response.json()

    def test_create_injects_extras(self, contract_client: TestClient) -> None:
        """Assert the create response binds context without internal classification fields."""
        if not self.app_def.capabilities.create:
            pytest.skip("create capability disabled")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_201_CREATED
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def test_create_extra_dep_enforced(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a ``create_extra_deps`` guard rejects create with a 409."""
        if not self.app_def.create_extra_deps:
            pytest.skip("no create extra deps")
        body = build_valid_create_body(
            self.app_def, create_body_overrides=self.create_body_overrides
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        mock_task_api.seed_running(_CONFLICT_TASK_NAME, owner=self.app_def.owner)
        base = app_base_url(self.app_def)

        response = post_create_body(contract_client, f"{base}/", self.app_def, body)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_execute_201(self, contract_client: TestClient) -> None:
        """Assert ``POST /{task_name}/execute`` dispatches and returns 201."""
        if not self.app_def.capabilities.execute:
            pytest.skip("execute capability disabled")
        body = (
            ModelFactory.create_factory(
                self.app_def.execute_write_model or TaskExecuteWrite
            )
            .build()
            .model_dump(mode="json")
        )
        base = app_base_url(self.app_def)

        response = contract_client.post(f"{base}/{SEEDED_TASK_NAME}/execute", json=body)

        assert response.status_code == status.HTTP_201_CREATED

    def test_execute_201_carries_run_state(self, contract_client: TestClient) -> None:
        """Assert the execute body carries the dispatched run's state.

        The upstream fake returns a full history row, so the two fields are
        forwarded from it rather than defaulted by the response model.
        """
        if not self.app_def.capabilities.execute:
            pytest.skip("execute capability disabled")
        write_body = (
            ModelFactory.create_factory(
                self.app_def.execute_write_model or TaskExecuteWrite
            )
            .build()
            .model_dump(mode="json")
        )
        base = app_base_url(self.app_def)

        response = contract_client.post(
            f"{base}/{SEEDED_TASK_NAME}/execute", json=write_body
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["task_name"] == SEEDED_TASK_NAME
        assert body["status"] == EXECUTE_STATUS.value
        assert body["created_at"] == EXECUTE_CREATED_AT

    def test_execute_route_absent(self) -> None:
        """Assert no derived execute route exists when execute is disabled."""
        if self.app_def.capabilities.execute:
            pytest.skip("execute capability enabled")
        if ("/{task_name}/execute", "POST") in extra_route_paths(self.app_def):
            pytest.skip("execute route kept custom in extra_routes")

        assert ("/{task_name}/execute", "POST") not in routes_of(self.app_def)

    def test_execute_conflict_409(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a RUNNING task makes execute 409 via the conflict guard."""
        if not self.app_def.capabilities.execute:
            pytest.skip("execute capability disabled")
        mock_task_api.seed_running(_CONFLICT_TASK_NAME, owner=self.app_def.owner)
        body = (
            ModelFactory.create_factory(
                self.app_def.execute_write_model or TaskExecuteWrite
            )
            .build()
            .model_dump(mode="json")
        )
        base = app_base_url(self.app_def)

        response = contract_client.post(
            f"{base}/{_CONFLICT_TASK_NAME}/execute", json=body
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_update_route_present(self) -> None:
        """Assert a ``PUT /{detail}`` route exists when update is enabled.

        Holds for both a full ``update_handler`` override and a handler-less
        derived PUT, since update derivation now fires on the capability alone.
        """
        if not self.app_def.capabilities.update:
            pytest.skip("update capability disabled")

        assert (detail_route_path(self.app_def), "PUT") in routes_of(self.app_def)

    def test_update_route_absent(self) -> None:
        """Assert no derived ``PUT /{detail}`` route exists when update is disabled."""
        if self.app_def.capabilities.update:
            pytest.skip("update capability enabled")
        if (detail_route_path(self.app_def), "PUT") in extra_route_paths(self.app_def):
            pytest.skip("update route kept custom in extra_routes")

        assert (detail_route_path(self.app_def), "PUT") not in routes_of(self.app_def)

    def test_update_200(self, contract_client: TestClient) -> None:
        """Assert a real ``PUT /{detail}`` updates the task and returns 200."""
        if not self.app_def.capabilities.update:
            pytest.skip("update capability disabled")
        body = build_valid_create_body(
            self.app_def,
            task_name=SEEDED_TASK_NAME,
            create_body_overrides=self.create_body_overrides,
        )
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_200_OK

    def test_update_rejects_an_unknown_field(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a ``PUT`` carrying an undeclared key 422s naming it, changing nothing."""
        if not self.app_def.capabilities.update:
            pytest.skip("update capability disabled")
        if self.app_def.update_handler is not None:
            pytest.skip("a full update_handler declares its own body")
        body = build_valid_create_body(
            self.app_def,
            task_name=SEEDED_TASK_NAME,
            create_body_overrides=self.create_body_overrides,
        )
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        body[UNKNOWN_FIELD] = "value"
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert_unknown_field_rejected(response)
        assert mock_task_api.last_update_payload is None

    def test_update_round_trips_stored_form(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert a created task's stored ``_form`` re-validates and re-stamps on PUT.

        The stamped create-form body is itself a valid update body: PUT-ing it back
        200s and the derived PUT re-stamps the same ``_form`` onto the upstream
        payload, proving the round-trip through ``create_model`` is lossless.
        """
        if not (
            self.app_def.capabilities.create
            and self.app_def.capabilities.update
            and self.app_def.update_handler is None
        ):
            pytest.skip("no derived create+update round-trip")
        task_name = "contract-roundtrip-task"
        body = build_valid_create_body(
            self.app_def,
            task_name=task_name,
            create_body_overrides=self.create_body_overrides,
        )
        if body is None:
            pytest.skip("no derivable create body (schema= passthrough)")
        base = app_base_url(self.app_def)
        create = post_create_body(contract_client, f"{base}/", self.app_def, body)
        assert create.status_code == status.HTTP_201_CREATED
        stored_form = mock_task_api.last_create_payload["data"][RESERVED_FORM_KEY]

        response = contract_client.put(f"{base}/{task_name}", json=stored_form)

        assert response.status_code == status.HTTP_200_OK
        assert (
            mock_task_api.last_update_payload["data"][RESERVED_FORM_KEY] == stored_form
        )

    def test_update_404(self, contract_client: TestClient) -> None:
        """Assert ``PUT /{detail}`` 404s for an unknown task name."""
        if not self.app_def.capabilities.update:
            pytest.skip("update capability disabled")
        body = build_valid_create_body(
            self.app_def,
            task_name=_UNKNOWN_TASK_NAME,
            create_body_overrides=self.create_body_overrides,
        )
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{_UNKNOWN_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_derived_injects_extras(self, contract_client: TestClient) -> None:
        """Assert a derived PUT renders through the create-path builder + context."""
        if not (
            self.app_def.capabilities.update and self.app_def.update_handler is None
        ):
            pytest.skip("no derived update route")
        body = build_valid_create_body(
            self.app_def,
            task_name=SEEDED_TASK_NAME,
            create_body_overrides=self.create_body_overrides,
        )
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert "service_type" not in payload
        assert "owner" not in payload
        assert payload["created_by"] == SYNTH_CREATED_BY_NAME

    def _valid_update_body(self, *, task_name: str) -> dict[str, Any] | None:
        """Return a valid derived-PUT body for the guard tests.

        Apps whose per-field gates reject the generic Polyfactory create body (the
        ``mysql_backups`` / ``restore`` per-``backup_type`` gates) override this so
        the guard tests exercise the guard rather than 422-ing on body parsing.

        :param task_name: The task name stamped into the body.
        :return: A valid PUT body, or ``None`` for a ``schema=`` passthrough app
            with no derivable body.
        """
        return build_valid_create_body(
            self.app_def,
            task_name=task_name,
            create_body_overrides=self.create_body_overrides,
        )

    def test_update_guard_409(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert the derived PUT rejects a running task with 409.

        Runs whenever the app derives a PUT (create-mirroring, no
        ``update_handler``) and has not opted out via :data:`UNGUARDED`, so it
        covers both the framework default guard and a per-app override. Seed the
        *target* task RUNNING so the guard fires whether it checks the single task
        or any owned task.
        """
        if (
            not self.app_def.capabilities.update
            or self.app_def.update_handler is not None
        ):
            pytest.skip("no derived update route")
        if self.app_def.update_guard is UNGUARDED:
            pytest.skip("update guards opted out")
        body = self._valid_update_body(task_name=SEEDED_TASK_NAME)
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        mock_task_api.seed_running(SEEDED_TASK_NAME, owner=self.app_def.owner)
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_delete_guard_409(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert the derived DELETE rejects a running task with 409.

        Runs whenever the app derives a DELETE (no ``delete_handler``) and has not
        opted out via :data:`UNGUARDED`. Seed the *target* task RUNNING so the guard
        fires whether it checks the single task or any owned task.
        """
        if (
            not self.app_def.capabilities.delete
            or self.app_def.delete_handler is not None
        ):
            pytest.skip("no derived delete route")
        if self.app_def.delete_guard is UNGUARDED:
            pytest.skip("delete guards opted out")
        mock_task_api.seed_running(SEEDED_TASK_NAME, owner=self.app_def.owner)
        base = app_base_url(self.app_def)

        response = contract_client.delete(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_update_protected_task_409(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert the framework default guard rejects a PUT on a protected task.

        Protected-task rejection is a property of the framework default guards, so
        this runs only for an app keeping the default (``update_guard == ()``): an
        :data:`UNGUARDED` opt-out or a per-app override tuple (which need not check
        protection) is skipped.
        """
        if (
            not self.app_def.capabilities.update
            or self.app_def.update_handler is not None
        ):
            pytest.skip("no derived update route")
        if self.app_def.update_guard != ():
            pytest.skip("protected-task rejection is the framework default guard only")
        body = self._valid_update_body(task_name=SEEDED_TASK_NAME)
        if body is None:
            pytest.skip("no derivable update body (schema= passthrough)")
        mock_task_api.seed_task(
            SEEDED_TASK_NAME, owner=self.app_def.owner, protected=True
        )
        base = app_base_url(self.app_def)

        response = contract_client.put(f"{base}/{SEEDED_TASK_NAME}", json=body)

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_delete_protected_task_409(
        self, contract_client: TestClient, mock_task_api: Any
    ) -> None:
        """Assert the framework default guard rejects a DELETE on a protected task.

        Protected-task rejection is a property of the framework default guards, so
        this runs only for an app keeping the default (``delete_guard == ()``): an
        :data:`UNGUARDED` opt-out or a per-app override tuple is skipped.
        """
        if (
            not self.app_def.capabilities.delete
            or self.app_def.delete_handler is not None
        ):
            pytest.skip("no derived delete route")
        if self.app_def.delete_guard != ():
            pytest.skip("protected-task rejection is the framework default guard only")
        mock_task_api.seed_task(
            SEEDED_TASK_NAME, owner=self.app_def.owner, protected=True
        )
        base = app_base_url(self.app_def)

        response = contract_client.delete(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_delete_204(self, contract_client: TestClient) -> None:
        """Assert ``DELETE /{detail}`` returns 204 and the task is gone afterward."""
        if not self.app_def.capabilities.delete:
            pytest.skip("delete capability disabled")
        base = app_base_url(self.app_def)

        response = contract_client.delete(f"{base}/{SEEDED_TASK_NAME}")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        after = contract_client.get(f"{base}/{SEEDED_TASK_NAME}")
        assert after.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_404(self, contract_client: TestClient) -> None:
        """Assert ``DELETE /{detail}`` 404s for an unknown task name."""
        if not self.app_def.capabilities.delete:
            pytest.skip("delete capability disabled")
        base = app_base_url(self.app_def)

        response = contract_client.delete(f"{base}/{_UNKNOWN_TASK_NAME}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_route_absent(self) -> None:
        """Assert no derived ``DELETE /{detail}`` route exists when delete is disabled."""
        if self.app_def.capabilities.delete:
            pytest.skip("delete capability enabled")
        if (detail_route_path(self.app_def), "DELETE") in extra_route_paths(
            self.app_def
        ):
            pytest.skip("delete route kept custom in extra_routes")

        assert (detail_route_path(self.app_def), "DELETE") not in routes_of(
            self.app_def
        )

    def test_unauthenticated_401(
        self, unauthenticated_contract_client: TestClient
    ) -> None:
        """Assert the router-level ``IsApiAuthenticated`` guard returns 401."""
        base = app_base_url(self.app_def)

        response = unauthenticated_contract_client.get(f"{base}/schema")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
