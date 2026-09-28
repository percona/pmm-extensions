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

from collections.abc import Callable, Iterator

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import deps as api_deps
from app.core.auth.providers.grafana.models import GrafanaUser
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.config import settings
from app.core.settings_override.models import SettingClassEnum
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
def admin_bearer_client(
    bearer_client: TestClient, casdoor_mock, casdoor_user_data
) -> TestClient:
    """Return the Bearer client whose credential resolves to an admin."""
    casdoor_mock.get_user.return_value = {**casdoor_user_data, "is_admin": True}
    return bearer_client


OPEN_MUTATIONS = [
    ("POST", "/schemas/1/tables/"),
    ("DELETE", "/tables/1"),
    ("POST", "/tables/1/revive"),
    ("POST", "/collection/collect"),
    ("POST", "/nodes/1/identity-link"),
    ("POST", "/services/1/identity-link"),
]
OPEN_MUTATION_IDS = [
    "schemas",
    "tables",
    "revive",
    "collection",
    "node_identity_link",
    "service_identity_link",
]

#: Adds back the two restricted routers, which the gate still refuses a
#: non-admin one layer ahead of the route, so every router stays covered there.
MUTATIONS = [("POST", "/nodes/"), ("PUT", "/services/1"), *OPEN_MUTATIONS]
MUTATION_IDS = ["nodes", "services", *OPEN_MUTATION_IDS]

RESTRICTED_WRITES = [
    ("POST", "/nodes/"),
    ("PUT", "/nodes/1"),
    ("DELETE", "/nodes/1"),
    ("POST", "/nodes/1/revive"),
    ("POST", "/nodes/1/services/"),
    ("PUT", "/services/1"),
    ("DELETE", "/services/1"),
    ("POST", "/services/1/revive"),
    ("POST", "/nodes/1/sync-health"),
    ("POST", "/services/1/sync-health"),
    ("POST", "/schemas/1/sync-health"),
    ("POST", "/tables/1/sync-health"),
]
RESTRICTED_WRITE_IDS = [
    "create_node",
    "update_node",
    "retire_node",
    "revive_node",
    "create_service_for_node",
    "update_service",
    "retire_service",
    "revive_service",
    "record_node_sync_health",
    "record_service_sync_health",
    "record_schema_sync_health",
    "record_table_sync_health",
]


@pytest.mark.parametrize(("method", "path"), MUTATIONS, ids=MUTATION_IDS)
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


@pytest.mark.parametrize(("method", "path"), OPEN_MUTATIONS, ids=OPEN_MUTATION_IDS)
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


@pytest.mark.parametrize(
    ("method", "path"), RESTRICTED_WRITES, ids=RESTRICTED_WRITE_IDS
)
def test_node_and_service_writes_are_refused_for_an_admin(
    admin_bearer_client: TestClient, method: str, path: str
) -> None:
    """Refuse an admin every node and service write, PMM owning those rows.

    An admin credential is what makes this test mean anything: a lower rank is
    already refused by the app-level gate, so the same assertion driven by a
    viewer would pass with the route dependency absent.
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
    bearer_client: TestClient, mocker: MockerFixture
) -> None:
    """Refuse the principal on a route carrying its own ``IsAdminDep``."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.patch(
        f"/admin/settings/{SettingClassEnum.INVENTORY_SETTINGS.value}",
        json={"overrides": {}},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_the_service_principal_can_still_update_a_node(
    bearer_client: TestClient, node: Node, mocker: MockerFixture
) -> None:
    """Update a node as the principal, which the scheduled sync depends on.

    The concrete 200 and the written field are the assertion — "not 403" would
    also pass on a 401 or a 500, which is exactly the silent breakage this gate
    risks for the scheduled writer.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.put(
        f"/nodes/{node.id}",
        json={
            "name": "renamed-by-sync",
            "address": node.address,
            "external_id": node.external_id,
            "source": node.source,
        },
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-by-sync"


def test_the_service_principal_can_still_retire_a_service(
    bearer_client: TestClient, service: Service, mocker: MockerFixture
) -> None:
    """Retire a service as the principal, which the scheduled sync depends on."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.delete(
        f"/services/{service.id}",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_revive_a_service(
    bearer_client: TestClient, retired_service: Service, mocker: MockerFixture
) -> None:
    """Revive a service as the principal, the other half of what sync writes.

    Revival is a POST rather than a DELETE, so it takes the gate's unsafe-method
    path on its own; a principal admitted for retirement is not thereby admitted
    for the call that undoes it.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.post(
        f"/services/{retired_service.id}/revive",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_create_a_node(
    bearer_client: TestClient, mocker: MockerFixture
) -> None:
    """Create a node as the principal, the PMM syncer's own entry point."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    payload = NodeWriteFactory.build()

    response = bearer_client.post(
        "/nodes/",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["external_id"] == payload.external_id


def test_the_service_principal_can_still_retire_a_node(
    bearer_client: TestClient, node: Node, mocker: MockerFixture
) -> None:
    """Retire a node as the principal, which the scheduled sync depends on."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.delete(
        f"/nodes/{node.id}",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_revive_a_node(
    bearer_client: TestClient, retired_node: Node, mocker: MockerFixture
) -> None:
    """Revive a node as the principal, the other half of what sync writes."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))

    response = bearer_client.post(
        f"/nodes/{retired_node.id}/revive",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
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
    mocker: MockerFixture,
    plural: str,
    fixture_name: str,
) -> None:
    """Record a sync outcome as the principal, at every level a syncer mirrors.

    The write is the one the syncer issues after each per-entity attempt, so a
    gate that refused it would leave the columns permanently unwritten while
    every sync still reported success.
    """
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    entity = request.getfixturevalue(fixture_name)

    response = bearer_client.post(
        f"/{plural}/{entity.id}/sync-health",
        json=sync_health_payload(SyncOutcomeEnum.SUCCESS),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_the_service_principal_can_create_a_service_for_a_node(
    bearer_client: TestClient, node: Node, mocker: MockerFixture
) -> None:
    """Create a service as the principal, the second of the syncer's creates."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    payload = ServiceWriteFactory.build()

    response = bearer_client.post(
        f"/nodes/{node.id}/services/",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["external_id"] == payload.external_id


def test_the_service_principal_can_update_a_service(
    bearer_client: TestClient, service: Service, node: Node, mocker: MockerFixture
) -> None:
    """Update a service as the principal, which the scheduled sync depends on."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    payload = ServiceWriteFactory.build(node_id=node.id, name="renamed-by-sync")

    response = bearer_client.put(
        f"/services/{service.id}",
        json=payload.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-by-sync"


def test_an_admin_still_upserts_a_host_observation(
    admin_bearer_client: TestClient, node: Node
) -> None:
    """Leave the host observation upsert admin-writable — PMM does not own it."""
    payload = HostSystemObservationWriteFactory.build()

    response = admin_bearer_client.put(
        f"/nodes/{node.id}/system-observation",
        json=payload.model_dump(mode="json"),
        headers=BEARER_HEADERS,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["node_id"] == node.id


def test_an_admin_still_upserts_a_service_observation(
    admin_bearer_client: TestClient, service: Service
) -> None:
    """Leave the service observation upsert admin-writable, as above."""
    payload = ServiceSystemObservationWriteFactory.build()

    response = admin_bearer_client.put(
        f"/services/{service.id}/system-observation",
        json=payload.model_dump(mode="json"),
        headers=BEARER_HEADERS,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["service_id"] == service.id


def test_an_admin_still_creates_a_schema_for_a_service(
    admin_bearer_client: TestClient, service: Service
) -> None:
    """Leave schema creation admin-writable though its parent service is not.

    Schemas are discovered by the MySQL syncer from the database rather than
    sourced from PMM, so the asymmetry against ``create_service_for_node`` is
    the intended shape of the restriction.
    """
    payload = SchemaWriteFactory.build()

    response = admin_bearer_client.post(
        f"/services/{service.id}/schemas/",
        json=payload.model_dump(mode="json"),
        headers=BEARER_HEADERS,
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["service_id"] == service.id


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


def test_a_restricted_route_still_advertises_its_bearer_security() -> None:
    """Keep the documented security contract the restricted routes carried.

    The new dependency reaches ``oauth2_scheme`` through ``CurrentUser``, so a
    refactor flattening that chain would drop the requirement from the schema
    while every behavioural test above stayed green.
    """
    security = inventory_app.openapi()["paths"]["/nodes/"]["post"]["security"]

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
    answer 403 from their own dependency, leaving no handler status to pin the
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
    these routes deliberately carry ``IsAuthenticatedDep`` and not
    ``IsServicePrincipalDep``. An admin credential is what makes the assertion
    mean anything — a lower rank is already refused by the app-level gate. The
    concrete 204 is the assertion rather than "not 403", which a 401 or a body
    the route never resolved would satisfy just as well.
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
    mocker: MockerFixture,
) -> None:
    """Admit the principal too, so an automated caller is not locked out later."""
    mocker.patch.object(settings, "EXTENSIONS_INTERNAL_TOKEN", SecretStr(SERVICE_TOKEN))
    predecessor, successor = split_nodes

    response = bearer_client.post(
        f"/nodes/{predecessor.id}/identity-link",
        json={
            "successor_id": successor.id,
            "decision": IdentityLinkDecisionEnum.REJECTED,
        },
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


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
