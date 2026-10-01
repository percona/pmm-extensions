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

"""Define tests for the unsafe-method role gate on the Inventory sub-app."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from fastapi import APIRouter, FastAPI, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import deps as api_deps
from app.api.deps import (
    get_current_service_principal,
    get_service_principal_exempt_caller,
    RequireMinimumRoleForUnsafeMethods,
    ServicePrincipalWriteRoute,
)
from app.core.auth.providers.grafana.models import GrafanaUser
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.config import settings
from app.core.security import has_unsafe_method, SAFE_HTTP_METHODS
from app.core.settings_override.constants import INVENTORY_SETTINGS
from app.inventory.deps import get_session
from app.inventory.main import inventory_app
from app.inventory.models import (
    IdentityLinkDecisionEnum,
    Node,
    Schema,
    Service,
    SyncOutcomeEnum,
    Table,
)
from app.inventory.routes import nodes, schemas, services, tables
from app.inventory.routes.nodes import (
    decide_node_identity_link,
    upsert_host_system_observation,
)
from app.inventory.routes.services import (
    create_schema_for_service,
    decide_service_identity_link,
    upsert_service_system_observation,
)
from tests.app.conftest import GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN
from tests.app.factories import (
    HostSystemObservationWriteFactory,
    NodeWriteFactory,
    SchemaWriteFactory,
    ServiceSystemObservationWriteFactory,
    ServiceWriteFactory,
)
from tests.app.inventory.conftest import sync_health_payload

BEARER_HEADERS = {"Authorization": "Bearer valid_token"}
SERVICE_TOKEN = "supersecret"


@pytest.fixture
def bearer_client(session: AsyncSession, casdoor_mock) -> Iterator[TestClient]:
    """Yield an Inventory TestClient that authenticates by Bearer token.

    No authentication dependency is overridden: the gate resolves the user
    imperatively, so an override could not reach it, and leaving the chain real
    is what makes the gate the thing under test.
    """
    inventory_app.dependency_overrides[get_session] = lambda: session
    yield TestClient(inventory_app, raise_server_exceptions=False)
    inventory_app.dependency_overrides = {}


@pytest.fixture
def internal_token(mocker: MockerFixture) -> str:
    """Return the service principal's token, configured as the internal one."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    return SERVICE_TOKEN


@pytest.fixture
def admin_bearer_client(
    bearer_client: TestClient, casdoor_mock, casdoor_user_data
) -> TestClient:
    """Return the Bearer client whose credential resolves to an admin."""
    casdoor_mock.get_user.return_value = {**casdoor_user_data, "is_admin": True}
    return bearer_client


#: The only node and service writes a human may make: an identity link is an
#: operator judgement, not a row the syncer owns.
EXEMPT_WRITES: Final = {decide_node_identity_link, decide_service_identity_link}

PRINCIPAL_ROUTERS: Final = {"nodes": nodes.router, "services": services.router}


def _top_level_calls(route: APIRoute) -> set[Callable[..., Any]]:
    """Return the callables of the dependencies FastAPI resolved for the route.

    :param route: The route to inspect.
    :return: One entry per top-level dependency, declared or inherited.
    """
    return {
        dependency.call
        for dependency in route.dependant.dependencies
        if dependency.call is not None
    }


def _requires_the_principal(route: APIRoute) -> bool:
    """Return whether the route's resolved dependencies include the restriction.

    :param route: The route to inspect.
    :return: ``True`` when the service-principal check guards the route.
    """
    return get_current_service_principal in _top_level_calls(route)


def _declares_the_exemption(route: APIRoute) -> bool:
    """Return whether the route's resolved dependencies include the exemption.

    :param route: The route to inspect.
    :return: ``True`` when the route declares ``ExemptFromServicePrincipalDep``.
    """
    return get_service_principal_exempt_caller in _top_level_calls(route)


def _principal_router_routes() -> list[APIRoute]:
    """Return the mounted routes the nodes and services routers declare.

    Walks the served app rather than the routers, those being what a request
    actually reaches once ``include_router`` has rebuilt every route.

    :return: The mounted node and service routes.
    """
    endpoints = {
        route.endpoint
        for router in PRINCIPAL_ROUTERS.values()
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    return [
        route
        for route in inventory_app.routes
        if isinstance(route, APIRoute) and route.endpoint in endpoints
    ]


def _principal_router_writes() -> list[APIRoute]:
    """Return the mounted unsafe routes the nodes and services routers declare.

    :return: The mounted node and service write routes.
    """
    return [
        route
        for route in _principal_router_routes()
        if has_unsafe_method(route.methods)
    ]


def _probes(*, exempt: bool) -> list[Any]:
    """Return one request per method of each node and service write.

    Classified against :data:`EXEMPT_WRITES` rather than against the route's
    dependencies, so a write that lost its restriction stays in the refused
    list and fails there instead of drifting into the admitted one. Path
    parameters take ``1``, which names no row: the restriction answers ahead of
    the lookup, so a refused caller never learns whether it exists.

    :param exempt: Whether to return the exempt writes or the restricted ones.
    :return: ``pytest.param`` entries of method and concrete path.
    """
    return [
        pytest.param(method, re.sub(r"\{[^}]+\}", "1", route.path), id=route.name)
        for route in _principal_router_writes()
        if (route.endpoint in EXEMPT_WRITES) is exempt
        for method in sorted(route.methods - SAFE_HTTP_METHODS)
    ]


PRINCIPAL_ROUTER_WRITES: Final = _probes(exempt=False)
EXEMPT_PRINCIPAL_ROUTER_WRITES: Final = _probes(exempt=True)

OPEN_MUTATIONS: Final = [
    pytest.param("POST", "/schemas/1/tables/", id="schemas"),
    pytest.param("DELETE", "/tables/1", id="tables"),
    pytest.param("POST", "/tables/1/revive", id="revive"),
    pytest.param("POST", "/collection/collect", id="collection"),
    *EXEMPT_PRINCIPAL_ROUTER_WRITES,
]

RESTRICTED_WRITES: Final = [
    *PRINCIPAL_ROUTER_WRITES,
    pytest.param("POST", "/schemas/1/sync-health", id="record_schema_sync_health"),
    pytest.param("POST", "/tables/1/sync-health", id="record_table_sync_health"),
]

MUTATIONS: Final = [*RESTRICTED_WRITES, *OPEN_MUTATIONS]


def test_the_derived_write_lists_cover_every_classified_route() -> None:
    """Refuse a derivation that parametrizes the tests below over nothing.

    Every exemption must reach the admitted list, so a renamed or removed
    exempt route fails here instead of silently leaving the list.
    """
    exempt_names = {param.id for param in EXEMPT_PRINCIPAL_ROUTER_WRITES}

    assert PRINCIPAL_ROUTER_WRITES
    assert exempt_names == {endpoint.__name__ for endpoint in EXEMPT_WRITES}


@pytest.mark.parametrize(("method", "path"), MUTATIONS)
def test_mutations_are_refused_for_a_non_admin(
    bearer_client: TestClient, method: str, path: str
) -> None:
    """Refuse a non-admin's mutation on each of the route modules.

    The routers are separate ``APIRouter`` instances that declare nothing about
    the gate — they inherit it through ``create_app``'s include loop, so each is
    covered separately.
    """
    response = bearer_client.request(method, path, json={}, headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), OPEN_MUTATIONS)
def test_mutations_pass_the_gate_for_an_admin(
    admin_bearer_client: TestClient, method: str, path: str
) -> None:
    """Admit an admin's mutation on each router, leaving the route to answer.

    Scoped to the routers that leave their writes open: the node and service
    writes refuse an admin at the route, so a "not 403" assertion cannot
    distinguish the gate's verdict from theirs.
    """
    response = admin_bearer_client.request(
        method, path, json={}, headers=BEARER_HEADERS
    )

    assert response.status_code != status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(("method", "path"), RESTRICTED_WRITES)
def test_node_and_service_writes_are_refused_for_an_admin(
    admin_bearer_client: TestClient, method: str, path: str
) -> None:
    """Refuse an admin every node and service write, PMM owning those rows.

    An admin credential is what makes this test mean anything: a lower rank is
    already refused by the app-level gate, so the same assertion driven by a
    viewer would pass with the router's restriction absent.
    """
    response = admin_bearer_client.request(
        method, path, json={}, headers=BEARER_HEADERS
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_an_empty_internal_token_admits_nobody(
    admin_bearer_client: TestClient,
    mocker: MockerFixture,
) -> None:
    """Refuse the would-be principal's own token while the setting carries none.

    With nothing to compare a credential against, no caller can resolve to the
    principal, so the restricted routes close rather than fall open.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(""))

    response = admin_bearer_client.post(
        "/nodes/", json={}, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_reads_are_unaffected_for_a_non_admin(bearer_client: TestClient) -> None:
    """Serve a non-admin's list request unchanged — the gate is method-scoped."""
    response = bearer_client.get("/nodes/", headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_200_OK


def test_the_service_principal_is_still_refused_by_a_route_admin_check(
    bearer_client: TestClient, internal_token: str
) -> None:
    """Refuse the principal on a route carrying its own ``IsAdminDep``."""
    response = bearer_client.patch(
        f"/admin/settings/{INVENTORY_SETTINGS}",
        json={"overrides": {}},
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_the_service_principal_can_still_update_a_node(
    bearer_client: TestClient, node: Node, internal_token: str
) -> None:
    """Update a node as the principal, which the scheduled sync depends on.

    The concrete 200 and the written field are the assertion — "not 403" would
    also pass on a 401 or a 500, which is exactly the silent breakage this gate
    risks for the scheduled writer.
    """
    response = bearer_client.put(
        f"/nodes/{node.id}",
        json={
            "name": "renamed-by-sync",
            "address": node.address,
            "external_id": node.external_id,
            "source": node.source,
        },
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-by-sync"


def test_the_service_principal_can_still_retire_a_service(
    bearer_client: TestClient, service: Service, internal_token: str
) -> None:
    """Retire a service as the principal, which the scheduled sync depends on."""
    response = bearer_client.delete(
        f"/services/{service.id}",
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_revive_a_service(
    bearer_client: TestClient, retired_service: Service, internal_token: str
) -> None:
    """Revive a service as the principal, the other half of what sync writes.

    Revival is a POST rather than a DELETE, so it takes the gate's unsafe-method
    path on its own; a principal admitted for retirement is not thereby admitted
    for the call that undoes it.
    """
    response = bearer_client.post(
        f"/services/{retired_service.id}/revive",
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_create_a_node(
    bearer_client: TestClient, internal_token: str
) -> None:
    """Create a node as the principal, the PMM syncer's own entry point."""
    payload = NodeWriteFactory.build()

    response = bearer_client.post(
        "/nodes/",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["external_id"] == payload.external_id


def test_the_service_principal_can_still_retire_a_node(
    bearer_client: TestClient, node: Node, internal_token: str
) -> None:
    """Retire a node as the principal, which the scheduled sync depends on."""
    response = bearer_client.delete(
        f"/nodes/{node.id}",
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_revive_a_node(
    bearer_client: TestClient, retired_node: Node, internal_token: str
) -> None:
    """Revive a node as the principal, the other half of what sync writes."""
    response = bearer_client.post(
        f"/nodes/{retired_node.id}/revive",
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.parametrize(
    ("plural", "fixture_name"),
    [
        ("nodes", "node"),
        ("services", "service"),
        ("schemas", "schema"),
        ("tables", "table"),
    ],
)
def test_the_service_principal_can_record_sync_health(
    bearer_client: TestClient,
    request: pytest.FixtureRequest,
    internal_token: str,
    plural: str,
    fixture_name: str,
) -> None:
    """Record a sync outcome as the principal, at every level a syncer mirrors.

    The write is the one the syncer issues after each per-entity attempt, so a
    gate that refused it would leave the columns permanently unwritten while
    every sync still reported success.
    """
    entity = request.getfixturevalue(fixture_name)

    response = bearer_client.post(
        f"/{plural}/{entity.id}/sync-health",
        json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_create_a_service_for_a_node(
    bearer_client: TestClient, node: Node, internal_token: str
) -> None:
    """Create a service as the principal, the second of the syncer's creates."""
    payload = ServiceWriteFactory.build()

    response = bearer_client.post(
        f"/nodes/{node.id}/services/",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["external_id"] == payload.external_id


def test_the_service_principal_can_update_a_service(
    bearer_client: TestClient, service: Service, node: Node, internal_token: str
) -> None:
    """Update a service as the principal, which the scheduled sync depends on."""
    payload = ServiceWriteFactory.build(node_id=node.id, name="renamed-by-sync")

    response = bearer_client.put(
        f"/services/{service.id}",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-by-sync"


@dataclass(frozen=True, slots=True)
class SyncerWrite:
    """Describe one write only a syncer issues, and the answer it expects."""

    method: str
    path: str
    parent: str
    factory: type[ModelFactory[Any]]
    created: int
    parent_key: str

    def send(
        self, client: TestClient, parent_id: int, headers: dict[str, str] | None
    ) -> Response:
        """Send a well-formed body for the write under the parent row.

        :param client: The client to send the write through.
        :param parent_id: The identifier of the existing parent row.
        :param headers: The caller's headers; ``None`` sends no credential.
        :return: The route's response.
        """
        return client.request(
            self.method,
            self.path.format(id=parent_id),
            json=self.factory.build().model_dump(mode="json"),
            headers=headers,
        )


class TestSyncerOnlyWritesOnceOpenToAnAdmin:
    """Verify the writes only a syncer issues now refuse every human.

    Each once admitted an admin, so the admin refusal is the access change and
    the principal's concrete answer is what keeps the syncer that writes it
    working.
    """

    WRITES: Final = [
        pytest.param(
            SyncerWrite(
                "PUT",
                "/nodes/{id}/system-observation",
                "node",
                HostSystemObservationWriteFactory,
                status.HTTP_200_OK,
                "node_id",
            ),
            id="host_observation",
        ),
        pytest.param(
            SyncerWrite(
                "PUT",
                "/services/{id}/system-observation",
                "service",
                ServiceSystemObservationWriteFactory,
                status.HTTP_200_OK,
                "service_id",
            ),
            id="service_observation",
        ),
        pytest.param(
            SyncerWrite(
                "POST",
                "/services/{id}/schemas/",
                "service",
                SchemaWriteFactory,
                status.HTTP_201_CREATED,
                "service_id",
            ),
            id="schema_for_service",
        ),
    ]

    @pytest.mark.parametrize("write", WRITES)
    def test_an_admin_is_refused(
        self,
        admin_bearer_client: TestClient,
        request: pytest.FixtureRequest,
        write: SyncerWrite,
    ) -> None:
        """Refuse an admin a well-formed write on a row that exists.

        A real parent and a valid body leave the restriction as the only thing
        that could answer 403.
        """
        row = request.getfixturevalue(write.parent)

        response = write.send(admin_bearer_client, row.id, BEARER_HEADERS)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("write", WRITES)
    def test_the_principal_is_served(
        self,
        bearer_client: TestClient,
        request: pytest.FixtureRequest,
        internal_token: str,
        write: SyncerWrite,
    ) -> None:
        """Serve the principal the write, which its syncer issues every run."""
        row = request.getfixturevalue(write.parent)

        response = write.send(
            bearer_client, row.id, {"Authorization": f"Bearer {internal_token}"}
        )

        assert response.status_code == write.created
        assert response.json()[write.parent_key] == row.id

    @pytest.mark.parametrize("write", WRITES)
    def test_an_anonymous_caller_is_unauthorized(
        self,
        bearer_client: TestClient,
        request: pytest.FixtureRequest,
        write: SyncerWrite,
    ) -> None:
        """Answer a missing credential 401 ahead of the restriction's 403."""
        row = request.getfixturevalue(write.parent)

        response = write.send(bearer_client, row.id, None)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_an_admin_still_retires_a_table(
    admin_bearer_client: TestClient, table: Table
) -> None:
    """Leave the table writes admin-writable — the restriction stops at services."""
    response = admin_bearer_client.delete(f"/tables/{table.id}", headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_204_NO_CONTENT


class TestServiceAccountBearer:
    """Verify a Grafana service-account token is ranked by the gate on a real route."""

    TOKEN = GRAFANA_CALLER_SERVICE_ACCOUNT_TOKEN

    @pytest.fixture
    def service_account_client(
        self,
        bearer_client: TestClient,
        grafana_mock: GrafanaAuthProvider,
        mocker: MockerFixture,
    ) -> Callable[[str], TestClient]:
        """Return a factory pinning the service account's Grafana org role."""
        mocker.patch.object(api_deps, "User", GrafanaUser)
        verify = mocker.patch(
            "app.core.auth.providers.grafana.sdk.GrafanaSDK.verify_service_account_token",
            new=mocker.AsyncMock(),
        )

        def with_role(role: str) -> TestClient:
            """Answer every verification with an account holding ``role``."""
            verify.return_value = {
                "id": 7,
                "login": "sa-1-ci-runner",
                "isDisabled": False,
                "role": role,
            }
            return bearer_client

        return with_role

    def test_a_viewer_is_refused_an_admin_write(
        self,
        service_account_client: Callable[[str], TestClient],
        table: Table,
    ) -> None:
        """Verify the gate refuses a Viewer account and the table survives."""
        client = service_account_client("Viewer")
        headers = {"Authorization": f"Bearer {self.TOKEN}"}

        response = client.delete(f"/tables/{table.id}", headers=headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert (
            client.get(f"/tables/{table.id}", headers=headers).json()["retired_at"]
            is None
        )

    def test_an_admin_retires_a_table(
        self, service_account_client: Callable[[str], TestClient], table: Table
    ) -> None:
        """Verify an Admin account clears the gate and the handler runs."""
        client = service_account_client("Admin")

        response = client.delete(
            f"/tables/{table.id}", headers={"Authorization": f"Bearer {self.TOKEN}"}
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/nodes/", "post"),
        ("/nodes/{node_id}/system-observation", "put"),
        ("/services/{service_id}/system-observation", "put"),
        ("/services/{service_id}/schemas/", "post"),
    ],
    ids=["create_node", "host_observation", "service_observation", "schema"],
)
def test_a_restricted_route_still_advertises_its_bearer_security(
    path: str, method: str
) -> None:
    """Keep the documented security contract the restricted routes carried.

    The restriction the route class adds reaches ``oauth2_scheme`` through
    ``CurrentUser``, so a refactor flattening that chain would drop the
    requirement from the schema while every behavioural test above stayed
    green. The three writes that no longer declare ``IsAuthenticatedDep``
    advertise it through the restriction alone.
    """
    security = inventory_app.openapi()["paths"][path][method]["security"]

    assert security == [{"OAuth2PasswordBearer": []}]


def test_a_gated_mutation_resolves_the_credential_once(
    admin_bearer_client: TestClient, casdoor_mock, mocker: MockerFixture
) -> None:
    """Resolve one credential once, though the gate and the route both need it.

    The gate resolves the caller in its body, so FastAPI's own dependency cache
    cannot cover the route's ``IsAuthenticatedDep``; the count of provider
    round-trips is what says the request-scoped cache does. The status pins that
    the request reached the handler, so both consumers ran.

    The spy sees the gate's resolution but not the route's, which holds the
    original the module-level ``IsAuthenticatedDep`` captured at import, so it
    counts the two consumers separately against the single round-trip.

    The route has to be one an admin still reaches: the node and service writes
    answer 403 from the router's restriction, leaving no handler status to pin the
    request against.
    """
    casdoor_mock.introspect_token.reset_mock()
    casdoor_mock.get_user.reset_mock()
    gate = mocker.spy(api_deps, "get_current_user")

    response = admin_bearer_client.delete("/tables/1", headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert gate.await_count == 1
    assert casdoor_mock.introspect_token.await_count == 1
    assert casdoor_mock.get_user.await_count == 1


def test_a_safe_method_resolves_only_for_the_route(
    admin_bearer_client: TestClient, casdoor_mock, mocker: MockerFixture
) -> None:
    """Resolve nothing in the gate on a safe method, leaving the route its own.

    The gate answers its method check ahead of everything else, so a read costs
    the one resolution its route declares rather than gaining the gate's.

    The provider counts alone cannot say that: a gate that did resolve would be
    served the route's resolution from the cache and leave them at one either
    way. Spying the name the gate looks up at call time is what separates the
    two, since the route holds the original captured at import.
    """
    casdoor_mock.introspect_token.reset_mock()
    casdoor_mock.get_user.reset_mock()
    gate = mocker.spy(api_deps, "get_current_user")

    response = admin_bearer_client.get("/services/1", headers=BEARER_HEADERS)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert gate.await_count == 0
    assert casdoor_mock.introspect_token.await_count == 1
    assert casdoor_mock.get_user.await_count == 1


def test_the_health_probe_resolves_no_credential(
    bearer_client: TestClient, casdoor_mock
) -> None:
    """Keep the liveness probe unauthenticated, resolving nothing at all.

    This is why the gate resolves the caller imperatively rather than through a
    sub-dependency, and the cache must not have made the resolution eager.
    """
    casdoor_mock.introspect_token.reset_mock()

    response = bearer_client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    casdoor_mock.introspect_token.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/nodes/identity-candidates"),
        ("GET", "/services/identity-candidates"),
        ("GET", "/nodes/1/identity-aliases"),
        ("GET", "/services/1/identity-aliases"),
    ],
    ids=["node_candidates", "service_candidates", "node_aliases", "service_aliases"],
)
def test_identity_reads_are_served_to_a_non_admin(
    bearer_client: TestClient, method: str, path: str
) -> None:
    """Serve a non-admin's identity read unchanged — the gate is method-scoped.

    The two ``identity-aliases`` paths address rows that do not exist here, so a
    404 from the path dependency is an equally good answer; what neither may be
    is the 403 the gate would return if it reached a safe method.
    """
    response = bearer_client.request(method, path, headers=BEARER_HEADERS)

    assert response.status_code in {status.HTTP_200_OK, status.HTTP_404_NOT_FOUND}


def test_an_admin_may_decide_an_identity_link(
    admin_bearer_client: TestClient, split_nodes: tuple[Node, Node]
) -> None:
    """Admit an admin on the identity-link route, unlike every other write here.

    An identity link is an operator judgement rather than a row PMM owns, so
    these routes are exempt from the router's service-principal restriction. An
    admin credential is what makes the assertion mean anything — a lower rank
    is already refused by the app-level gate. The concrete 204 is the assertion
    rather than "not 403", which a 401 or a body the route never resolved would
    satisfy just as well.
    """
    predecessor, successor = split_nodes

    response = admin_bearer_client.post(
        f"/nodes/{predecessor.id}/identity-link",
        json={
            "successor_id": successor.id,
            "decision": IdentityLinkDecisionEnum.REJECTED,
        },
        headers=BEARER_HEADERS,
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_may_also_decide_an_identity_link(
    bearer_client: TestClient,
    split_nodes: tuple[Node, Node],
    internal_token: str,
) -> None:
    """Admit the principal too, so an automated caller is not locked out later."""
    predecessor, successor = split_nodes

    response = bearer_client.post(
        f"/nodes/{predecessor.id}/identity-link",
        json={
            "successor_id": successor.id,
            "decision": IdentityLinkDecisionEnum.REJECTED,
        },
        headers={"Authorization": f"Bearer {internal_token}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


class TestServiceIdentityLinkAccess:
    """Verify the service identity-link write keeps the access the node one has.

    It is the second of the two exempt writes, so losing its exemption would
    restrict the operator's only way to decide a service pairing.
    """

    @staticmethod
    def _reject(
        client: TestClient, split_services: tuple[Service, Service], token: str
    ) -> int:
        """Reject the split pairing as the caller ``token`` names.

        :param client: The client to send the decision through.
        :param split_services: The predecessor and successor of the pairing.
        :param token: The Bearer credential naming the caller.
        :return: The status the route answered.
        """
        predecessor, successor = split_services
        return client.post(
            f"/services/{predecessor.id}/identity-link",
            json={
                "successor_id": successor.id,
                "decision": IdentityLinkDecisionEnum.REJECTED,
            },
            headers={"Authorization": f"Bearer {token}"},
        ).status_code

    def test_an_admin_may_decide_it(
        self,
        admin_bearer_client: TestClient,
        split_services: tuple[Service, Service],
    ) -> None:
        """Admit an admin, the rank the restriction refuses on every other write."""
        assert (
            self._reject(admin_bearer_client, split_services, "valid_token")
            == status.HTTP_204_NO_CONTENT
        )

    def test_the_principal_may_decide_it(
        self,
        bearer_client: TestClient,
        split_services: tuple[Service, Service],
        internal_token: str,
    ) -> None:
        """Admit the principal too, the exemption widening rather than swapping."""
        assert (
            self._reject(bearer_client, split_services, internal_token)
            == status.HTTP_204_NO_CONTENT
        )

    def test_a_non_admin_is_still_refused(
        self,
        bearer_client: TestClient,
        split_services: tuple[Service, Service],
    ) -> None:
        """Refuse a viewer, the exemption leaving the app-level gate in force."""
        assert (
            self._reject(bearer_client, split_services, "valid_token")
            == status.HTTP_403_FORBIDDEN
        )


def test_the_observation_collection_requires_a_credential(
    bearer_client: TestClient,
) -> None:
    """Refuse an anonymous fleet-wide read of the observation collection.

    The route publishes one measured fact per node, so it carries
    ``IsAuthenticatedDep`` like its per-node sibling rather than being open.
    """
    response = bearer_client.get("/nodes/system-observations")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize(
    "path",
    [
        "/nodes/{node_id}/services/",
        "/services/{service_id}/schemas/",
        "/schemas/{schema_id}/tables/",
    ],
)
@pytest.mark.parametrize("existing", [True, False], ids=["retired", "missing"])
def test_a_retired_parent_listing_requires_a_credential(
    bearer_client: TestClient,
    retired_node: Node,
    retired_service: Service,
    retired_schema: Schema,
    path: str,
    *,
    existing: bool,
) -> None:
    """Refuse an anonymous retired-parent listing before the parent is looked up.

    A retired parent and a missing one answer the same 401, so the opt-in gives
    an anonymous caller no way to probe which identifiers were ever issued.
    """
    ids = (
        {
            "node_id": retired_node.id,
            "service_id": retired_service.id,
            "schema_id": retired_schema.id,
        }
        if existing
        else dict.fromkeys(("node_id", "service_id", "schema_id"), 99999)
    )

    response = bearer_client.get(path.format(**ids), params={"include_retired": True})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestServicePrincipalDefault:
    """Verify node and service writes are syncer-only unless explicitly exempt."""

    @pytest.mark.parametrize(
        "router", PRINCIPAL_ROUTERS.values(), ids=PRINCIPAL_ROUTERS
    )
    def test_the_router_builds_restricted_routes(self, router: APIRouter) -> None:
        """Build every route through the class that restricts writes by default."""
        assert router.route_class is ServicePrincipalWriteRoute

    @pytest.mark.parametrize(
        "router", PRINCIPAL_ROUTERS.values(), ids=PRINCIPAL_ROUTERS
    )
    def test_every_route_is_built_by_the_restricting_class(
        self, router: APIRouter
    ) -> None:
        """Refuse a route of another class, such as one a nested router brings.

        ``include_router`` rebuilds a route with the class it was declared with,
        not the including router's, so a nested plain router's writes would skip
        the default.
        """
        foreign = [
            route.name
            for route in router.routes
            if not isinstance(route, ServicePrincipalWriteRoute)
        ]

        assert not foreign

    @pytest.mark.parametrize(
        "router", [schemas.router, tables.router], ids=["schemas", "tables"]
    )
    def test_a_mixed_access_router_keeps_the_plain_route_class(
        self, router: APIRouter
    ) -> None:
        """Leave schema and table writes to their per-route access, which mixes both."""
        assert router.route_class is APIRoute

    def test_every_write_is_restricted_unless_exempt(self) -> None:
        """Classify every node and service write as exactly one of the two.

        A route counted neither way would be a write that defaulted silently,
        the gap the router-scoped default exists to close.
        """
        writes = _principal_router_writes()

        assert writes
        for route in writes:
            exempt = route.endpoint in EXEMPT_WRITES
            assert _declares_the_exemption(route) is exempt, route.name
            assert _requires_the_principal(route) is not exempt, route.name

    def test_the_identity_links_are_the_only_exemptions(self) -> None:
        """Pin the exception list, so a new opt-out shows up as a failing diff."""
        declared = {
            route.endpoint
            for route in _principal_router_writes()
            if _declares_the_exemption(route)
        }

        assert declared == EXEMPT_WRITES

    def test_no_read_declares_the_exemption(self) -> None:
        """Refuse the exemption on a read, where it opens nothing.

        It would only mislead the next reader about which routes are open.
        """
        reads = [
            route
            for route in _principal_router_routes()
            if not has_unsafe_method(route.methods)
        ]

        assert reads
        assert not [route.name for route in reads if _declares_the_exemption(route)]

    @pytest.mark.parametrize(
        "endpoint",
        [
            upsert_host_system_observation,
            upsert_service_system_observation,
            create_schema_for_service,
        ],
        ids=["host_observation", "service_observation", "schema_for_service"],
    )
    def test_a_syncer_only_write_left_open_is_now_restricted(
        self, endpoint: Callable[..., Any]
    ) -> None:
        """Restrict the writes only a syncer issues, which used to admit an admin."""
        (route,) = [
            route for route in _principal_router_writes() if route.endpoint is endpoint
        ]

        assert _requires_the_principal(route)


class TestAnUnannotatedWriteRoute:
    """Verify a write route added later to either router inherits the restriction.

    The route lands on a router built from the real one's configuration, so the
    verdict covers what a future change to ``nodes.py`` or ``services.py`` would
    ship without mutating the router the other tests serve.
    """

    PATH: Final = "/unannotated-write"

    @pytest.fixture(params=list(PRINCIPAL_ROUTERS), ids=list(PRINCIPAL_ROUTERS))
    def client(
        self, request: pytest.FixtureRequest, casdoor_mock, internal_token: str
    ) -> tuple[TestClient, str]:
        """Return a client over an app serving the router with an unannotated write."""
        real = PRINCIPAL_ROUTERS[request.param]
        router = APIRouter(
            prefix=real.prefix,
            dependencies=real.dependencies,
            route_class=real.route_class,
        )

        @router.post(self.PATH)
        async def unannotated_write() -> dict[str, bool]:
            return {"written": True}

        app = FastAPI(dependencies=[RequireMinimumRoleForUnsafeMethods])
        app.include_router(router)
        return TestClient(
            app, raise_server_exceptions=False
        ), f"{router.prefix}{self.PATH}"

    def test_an_admin_is_refused(
        self, client: tuple[TestClient, str], casdoor_mock, casdoor_user_data
    ) -> None:
        """Refuse an admin, the rank the app-level gate alone would admit."""
        test_client, path = client
        casdoor_mock.get_user.return_value = {**casdoor_user_data, "is_admin": True}

        response = test_client.post(path, headers=BEARER_HEADERS)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_the_principal_is_served(
        self, client: tuple[TestClient, str], internal_token: str
    ) -> None:
        """Serve the principal, so the default restricts rather than closes."""
        test_client, path = client

        response = test_client.post(
            path, headers={"Authorization": f"Bearer {internal_token}"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"written": True}
