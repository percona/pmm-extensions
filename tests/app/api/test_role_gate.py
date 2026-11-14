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

"""Define tests classifying the unsafe surface the minimum-role gate reaches."""

from collections.abc import Callable, Iterator
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.deps import (
    admits_service_principal,
    get_current_user,
    minimum_role_for,
    require_minimum_role_for_unsafe_methods,
    resolved_callables,
)
from app.core.auth.models import UserRole
from app.core.security import SAFE_HTTP_METHODS
from app.extensions.main import extensions_app
from app.inventory.main import inventory_app
from app.tasks.main import tasks_app
from app.tasks.routes import latest_task_history

UNGATED_EXTENSIONS_API_PREFIXES = ("/api/oauth/", "/api/users/", "/api/config/")

GATED_APPS: Final = {
    "extensions": extensions_app,
    "inventory": inventory_app,
    "tasks": tasks_app,
}

#: Every unsafe route PMM Extensions opens below the gate's ``ADMIN`` default, keyed by the
#: service, method and path it answers on. Sub-app paths carry no mount prefix.
NON_ADMIN_MINIMUMS: Final = {
    ("extensions", "POST", "/api/apps/alerts/restore"): UserRole.EDITOR,
    ("extensions", "POST", "/api/apps/alerts/push"): UserRole.EDITOR,
    ("tasks", "POST", "/history/latest"): UserRole.NONE,
}

#: Every unsafe route the gate admits ``EXTENSIONS_INTERNAL_TOKEN``'s principal on,
#: keyed like :data:`NON_ADMIN_MINIMUMS`.
SERVICE_PRINCIPAL_ROUTES: Final = frozenset(
    {
        # pmm-managed: inventory sync after an install.
        ("extensions", "POST", "/api/apps/inventory/sync/"),
        # pmm-managed: the switch, and a scoped refresh after an install.
        ("extensions", "PUT", "/api/apps/om_inventory/enabled"),
        ("extensions", "POST", "/api/apps/om_inventory/runs"),
        # pmm-managed: the bootstrap stepper.
        (
            "extensions",
            "POST",
            "/api/apps/om_bootstrap/runs/{run_id}/hosts/{host}/steps/{step_name}:dispatch",
        ),
        (
            "extensions",
            "POST",
            "/api/apps/om_bootstrap/runs/{run_id}/hosts/{host}/finalize/{step_name}:dispatch",
        ),
        (
            "extensions",
            "POST",
            "/api/apps/om_bootstrap/runs/{run_id}/run-steps/{step_name}:dispatch",
        ),
        (
            "extensions",
            "POST",
            "/api/apps/om_bootstrap/runs/{run_id}/hosts/{host}/rollback/{step_name}:dispatch",
        ),
        ("extensions", "POST", "/api/apps/om_bootstrap/runs/{run_id}:finish"),
        ("extensions", "POST", "/api/apps/om_bootstrap/runs/{run_id}:cancel"),
        # Inventory syncers.
        ("inventory", "POST", "/nodes/"),
        ("inventory", "PUT", "/nodes/{node_id}"),
        ("inventory", "DELETE", "/nodes/{node_id}"),
        ("inventory", "POST", "/nodes/{node_id}/revive"),
        ("inventory", "POST", "/nodes/sync-health"),
        ("inventory", "POST", "/nodes/{node_id}/sync-health"),
        ("inventory", "PUT", "/nodes/{node_id}/system-observation"),
        ("inventory", "POST", "/nodes/{node_id}/services/"),
        ("inventory", "PUT", "/services/{service_id}"),
        ("inventory", "DELETE", "/services/{service_id}"),
        ("inventory", "POST", "/services/{service_id}/revive"),
        ("inventory", "POST", "/services/sync-health"),
        ("inventory", "POST", "/services/{service_id}/sync-health"),
        ("inventory", "PUT", "/services/{service_id}/system-observation"),
        ("inventory", "POST", "/services/{service_id}/schemas/"),
        ("inventory", "PUT", "/schemas/{schema_id}"),
        ("inventory", "DELETE", "/schemas/{schema_id}"),
        ("inventory", "POST", "/schemas/{schema_id}/revive"),
        ("inventory", "POST", "/schemas/sync-health"),
        ("inventory", "POST", "/schemas/{schema_id}/sync-health"),
        ("inventory", "POST", "/schemas/{schema_id}/tables/"),
        ("inventory", "PUT", "/tables/{table_id}"),
        ("inventory", "DELETE", "/tables/{table_id}"),
        ("inventory", "POST", "/tables/{table_id}/revive"),
        ("inventory", "POST", "/tables/sync-health"),
        ("inventory", "POST", "/tables/{table_id}/sync-health"),
        # ATW proxy tasks.
        ("tasks", "POST", "/"),
        ("tasks", "PUT", "/{task_name}"),
        # Syncers, the sweep and bootstrap steps dispatch; the sweep and a
        # bootstrap cancel stop; the log stream reconciles.
        ("tasks", "POST", "/execute/{task_name}"),
        ("tasks", "POST", "/history/{task_history_id}/stop/"),
        ("tasks", "POST", "/history/{task_history_id}/sync/"),
    }
)


def _gate_callables(route: APIRoute) -> set[Callable[..., Any]]:
    """Return the callables the route's top-level dependencies resolve to.

    :param route: The route whose resolved dependency chain to inspect.
    :return: One entry per dependency declared on the route or inherited from a
        router or app above it.
    """
    return {
        dependency.call
        for dependency in route.dependant.dependencies
        if dependency.call is not None
    }


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Return every ``APIRoute`` mounted on ``app``.

    :param app: The application whose route table to read.
    :return: The mounted API routes, excluding static mounts and websockets.
    """
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _gated_unsafe_routes() -> Iterator[tuple[tuple[str, str, str], APIRoute]]:
    """Yield each gated unsafe route keyed like :data:`NON_ADMIN_MINIMUMS`.

    :return: One ``(service, method, path)`` key and route per unsafe method.
    """
    for name, app in GATED_APPS.items():
        for route in _api_routes(app):
            if require_minimum_role_for_unsafe_methods in _gate_callables(route):
                for method in route.methods - SAFE_HTTP_METHODS:
                    yield (name, method, route.path), route


@pytest.mark.parametrize("app", [inventory_app, tasks_app], ids=["inventory", "tasks"])
def test_every_service_route_inherits_the_role_gate(app: FastAPI) -> None:
    """Assert the app-level gate reaches every route the service mounts.

    Membership follows from the app carrying the dependency, so a router added
    to either service later inherits the gate rather than having to remember it.
    """
    routes = _api_routes(app)
    assert routes
    for route in routes:
        assert require_minimum_role_for_unsafe_methods in _gate_callables(route), (
            route.path
        )


@pytest.mark.parametrize("app", [inventory_app, tasks_app], ids=["inventory", "tasks"])
def test_every_unsafe_route_authenticates_itself_as_well(app: FastAPI) -> None:
    """Assert an unsafe route resolves the caller through its own dependency too.

    The gate resolves the caller in its body, so nothing about a route's own
    authentication follows from the gate reaching it. This is the second consumer
    the per-request resolution is shared with, and a route dropping it would
    leave the round-trip counts in the per-service gate modules measuring one
    resolution because only one is left to make.
    """
    unsafe = [route for route in _api_routes(app) if route.methods - SAFE_HTTP_METHODS]
    assert unsafe
    for route in unsafe:
        assert get_current_user in set(resolved_callables(route.dependant)), route.path


def test_extensions_api_routes_inherit_the_gate_and_the_identity_tree_does_not() -> (
    None
):
    """Assert ``/api`` inherits the gate while the identity tree stays outside it.

    The prefixes are an assertion about the current mount layout, not a runtime
    decision — ``api_router`` carries the dependency and the identity routers are
    included beside it rather than through it.
    """
    gated: set[str] = set()
    ungated: set[str] = set()
    for route in _api_routes(extensions_app):
        if not route.path.startswith("/api/"):
            continue
        bucket = (
            gated
            if require_minimum_role_for_unsafe_methods in _gate_callables(route)
            else ungated
        )
        bucket.add(route.path)

    assert gated
    assert ungated
    assert all(path.startswith(UNGATED_EXTENSIONS_API_PREFIXES) for path in ungated), (
        ungated
    )
    assert not any(
        path.startswith(UNGATED_EXTENSIONS_API_PREFIXES) for path in gated
    ), gated


def test_every_unsafe_route_resolves_to_its_classified_minimum() -> None:
    """Assert each gated unsafe route resolves to the minimum PMM Extensions classified it at.

    Every route below ``ADMIN`` is a surface opened past the default, so opening
    or closing one has to mean editing a map that names it: the equality below
    fails until ``NON_ADMIN_MINIMUMS`` matches the tree in both directions. The
    two PagerDuty anchors are asserted by name because they take their ``ADMIN``
    from the default rather than a registration, and because a walk that
    resolved nothing at all would satisfy the equality on its own.
    """
    resolved = {key: minimum_role_for(route) for key, route in _gated_unsafe_routes()}

    assert (
        resolved[("extensions", "POST", "/api/apps/alerts/pagerduty")] is UserRole.ADMIN
    )
    assert (
        resolved[("extensions", "POST", "/api/apps/alerts/pagerduty/delete")]
        is UserRole.ADMIN
    )
    non_admin = {
        key: role for key, role in resolved.items() if role is not UserRole.ADMIN
    }

    assert non_admin == NON_ADMIN_MINIMUMS


def test_the_service_principal_is_admitted_only_where_classified() -> None:
    """Assert the gate admits the principal on exactly the routes pinned for it.

    Opening a route to ``EXTENSIONS_INTERNAL_TOKEN`` hands it to whoever holds
    the token, so it has to mean editing a list that names the route.
    """
    admitted = {
        key for key, route in _gated_unsafe_routes() if admits_service_principal(route)
    }

    assert admitted == SERVICE_PRINCIPAL_ROUTES


def test_a_registration_reaches_the_endpoint_object_fastapi_matched() -> None:
    """Assert the registry resolves through the object stored on the live route.

    The route is walked out of the mounted table rather than constructed, so
    this fails whenever the registered callable stops being the one FastAPI
    matched — a decorator wrapping the endpoint between registration and
    mounting, say, which otherwise just reverts the route to ``ADMIN``
    silently.
    """
    route = next(
        route for route in _api_routes(tasks_app) if route.path == "/history/latest"
    )

    assert route.endpoint is latest_task_history
    assert minimum_role_for(route) is UserRole.NONE
