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

"""Tests for the PMM Extensions hosts JSON API route at ``/api/extensions/hosts/``."""

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.core.exceptions import HTTPBadGatewayException
from app.extensions.main import extensions_app

#: The number of upstream inventory calls the route makes, whatever the host count.
INVENTORY_CALLS_PER_REQUEST = 2

#: Enough executor hosts that a per-host upstream call would be unmistakable.
MANY_HOSTS = 10


def _inventory_answers(
    nodes: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]] = (),
) -> Callable[..., dict[str, Any]]:
    """Answer each inventory path the route walks with its own payload.

    The route reads two collections, so a single ``return_value`` would hand the
    node list back as the observation list and mask a join defect behind a
    ``KeyError`` the route degrades on.

    :param nodes: Items for ``GET /nodes/``.
    :param observations: Items for ``GET /nodes/system-observations``.
    :return: A side effect resolving each path to its own envelope.
    """
    payloads = {
        "/nodes/": {"items": list(nodes)},
        "/nodes/system-observations": {"items": list(observations)},
    }

    def _get(path: str, **_kwargs: Any) -> dict[str, Any]:
        return payloads[path]

    return _get


def _observation(node_id: int, *, can_elevate: bool | None) -> dict[str, Any]:
    """Build one observation summary as the collection route serves it."""
    return {
        "node_id": node_id,
        "can_elevate": can_elevate,
        "observed_at": "2026-09-15T00:00:00Z",
    }


class TestExtensionsHostsEndpoint:
    """Cover ``GET /api/extensions/hosts/`` happy-path and edge cases."""

    def test_returns_hosts_with_inventory_display_names_sorted(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return hosts merged with inventory names, sorted by case-folded name."""
        mock_task_api_dep.get.return_value = {
            "nomad-1": "10.0.0.1",
            "nomad-2": "10.0.0.2",
        }
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"},
                {"id": 2, "address": "10.0.0.2", "name": "db-mysql-prod-02"},
            ]
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-mysql-prod-01",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
            {
                "id": "nomad-2",
                "name": "db-mysql-prod-02",
                "address": "10.0.0.2",
                "can_elevate": None,
            },
        ]

    def test_falls_back_to_node_name_when_inventory_match_missing(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return raw node names for hosts with no inventory match."""
        mock_task_api_dep.get.return_value = {
            "nomad-1": "10.0.0.1",
            "nomad-2": "10.0.0.2",
        }
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"}]
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert {host["id"] for host in payload} == {"nomad-1", "nomad-2"}
        names_by_id = {host["id"]: host["name"] for host in payload}
        assert names_by_id["nomad-1"] == "db-mysql-prod-01"
        assert names_by_id["nomad-2"] == "nomad-2"

    def test_extra_inventory_nodes_are_ignored(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Drop inventory nodes whose address is not present in the executor list."""
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"},
                {"id": 2, "address": "10.0.0.99", "name": "ghost-node"},
            ],
            [_observation(2, can_elevate=False)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-mysql-prod-01",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_duplicate_inventory_addresses_keep_first_match(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Keep the first inventory name when two records share an address.

        Pin the deduplication semantics inherited from
        ``address_to_name_index`` (first wins). The previous dict-comprehension
        implementation was "last wins"; switching to the shared helper aligns
        this route with ``resolve_executor_name_by_address`` so both call
        sites resolve a duplicated address to the same display name. Duplicate
        addresses are not expected in practice, but locking the choice in a
        test prevents an accidental revert.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.1", "name": "db-primary"},
                {"id": 2, "address": "10.0.0.1", "name": "db-shadow"},
            ]
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-primary",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_reports_a_measured_capability(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Publish an observed ``True`` on the executor that was measured."""
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 7, "address": "10.0.0.1", "name": "db-primary"}],
            [_observation(7, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is True

    def test_reports_a_measured_inability(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Publish an observed ``False`` as ``False``, not as never-observed.

        Asserted with ``is False`` rather than a falsy check, which ``None``
        would satisfy — and ``None`` is the value this whole feature exists to
        distinguish it from.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 7, "address": "10.0.0.1", "name": "db-primary"}],
            [_observation(7, can_elevate=False)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is False

    def test_an_observation_omitting_the_capability_does_not_drop_display_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Read a missing capability as never-observed, not as an inventory outage.

        The observation contract declares ``can_elevate`` optional, so a producer
        may omit it. A hard subscript here would raise into the shared degradation
        block and take the display name down with it.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 7, "address": "10.0.0.1", "name": "db-primary"}],
            [{"node_id": 7, "observed_at": "2026-09-15T00:00:00Z"}],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-primary",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_reports_null_for_a_never_observed_node(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Leave a matched but unmeasured node's capability unknown."""
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 7, "address": "10.0.0.1", "name": "db-primary"}]
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is None

    def test_reports_null_for_an_executor_with_no_inventory_match(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Leave an unmatched executor unknown — permanently, not transiently."""
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 7, "address": "10.0.0.9", "name": "elsewhere"}],
            [_observation(7, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "nomad-1",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_issues_no_per_host_upstream_call(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Walk the two upstream collections once each, whatever the host count."""
        mock_task_api_dep.get.return_value = {
            f"nomad-{index}": f"10.0.0.{index}" for index in range(MANY_HOSTS)
        }
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": index, "address": f"10.0.0.{index}", "name": f"db-{index}"}
                for index in range(MANY_HOSTS)
            ],
            [_observation(index, can_elevate=True) for index in range(MANY_HOSTS)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()) == MANY_HOSTS
        assert mock_inventory_api_dep.get.await_count == INVENTORY_CALLS_PER_REQUEST

    def test_a_name_match_beats_an_address_match(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Publish a measurement on the executor collection actually probed.

        ``get_task_target`` resolves the host to probe by name before address, so
        a node named ``a`` at ``b``'s address is measured as ``a``. An
        address-keyed join would publish that measurement on ``b`` and report
        ``a`` — the host truly measured — as never-observed, inverting both.
        """
        mock_task_api_dep.get.return_value = {"a": "10.0.0.1", "b": "10.0.0.2"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 5, "address": "10.0.0.2", "name": "a"}],
            [_observation(5, can_elevate=False)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        capabilities = {host["id"]: host["can_elevate"] for host in response.json()}
        assert capabilities["a"] is False
        assert capabilities["b"] is None

    def test_an_address_match_applies_when_no_name_matches(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Fall back to the address rule, as the collection-side resolver does."""
        mock_task_api_dep.get.return_value = {"a": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [{"id": 5, "address": "10.0.0.1", "name": "db-1"}],
            [_observation(5, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is True

    def test_a_name_matched_node_wins_over_an_address_matched_one(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Resolve one executor claimed by two nodes the way collection would.

        Each node contributes to exactly one pass, so the address-matched node
        cannot overwrite the name-matched one — nor publish its own measurement
        on a second executor alongside it.
        """
        mock_task_api_dep.get.return_value = {"a": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 5, "address": "10.0.0.9", "name": "a"},
                {"id": 6, "address": "10.0.0.1", "name": "db-1"},
            ],
            [_observation(5, can_elevate=False), _observation(6, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is False

    def test_an_unobserved_name_match_does_not_suppress_a_real_measurement(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Publish the sibling row that actually measured the executor.

        Co-location holds for a name match *or* an address match, so both nodes
        here were probed on executor ``a``. Letting the unobserved name match claim
        the slot would report never-observed for a host that was measured.
        """
        mock_task_api_dep.get.return_value = {"a": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.9", "name": "a"},
                {"id": 2, "address": "10.0.0.1", "name": "db-1"},
            ],
            [_observation(2, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is True

    def test_an_observed_name_match_still_beats_an_observed_address_match(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Keep name precedence when the name-matched node does carry a row.

        Guards the fix above from over-reaching: an observation whose value is
        ``None`` is still an observation, so it holds the slot rather than
        deferring to the address match.
        """
        mock_task_api_dep.get.return_value = {"a": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.9", "name": "a"},
                {"id": 2, "address": "10.0.0.1", "name": "db-1"},
            ],
            [_observation(1, can_elevate=None), _observation(2, can_elevate=True)],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["can_elevate"] is None

    def test_a_failing_observation_route_keeps_the_display_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Degrade the capability alone when only its route fails.

        An Inventory predating the observation collection answers 422 there while
        serving ``/nodes/`` normally. The newer enrichment must not cost the host
        selector the display names it has always had.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}

        def _answers(path, **_kwargs):
            if path == "/nodes/":
                return {
                    "items": [
                        {"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"}
                    ]
                }
            raise HTTPBadGatewayException("system-observations unavailable")

        mock_inventory_api_dep.get.side_effect = _answers
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-mysql-prod-01",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_a_malformed_observation_page_degrades_rather_than_500s(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Answer 200 when the observation envelope itself is malformed.

        A bad envelope fails validation rather than raising any of the transport
        errors, so an upstream serving a wrong shape would turn an additive
        enrichment into a 500 on a route that is documented to degrade.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}

        def _answers(path: str, **_kwargs: Any) -> dict[str, Any]:
            if path == "/nodes/":
                return {
                    "items": [
                        {"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"}
                    ]
                }
            return {"items": "not-a-list", "total": "lots"}

        mock_inventory_api_dep.get.side_effect = _answers
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "db-mysql-prod-01",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_a_non_boolean_capability_degrades_rather_than_500s(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Answer 200 with every capability null when an observation row is junk.

        A non-boolean ``can_elevate`` fails validation the same way a malformed
        envelope does, so one bad row degrades the whole capability enrichment
        rather than reaching ``HostResponse`` and raising a 500.
        """
        mock_task_api_dep.get.return_value = {
            "nomad-1": "10.0.0.1",
            "nomad-2": "10.0.0.2",
        }
        mock_inventory_api_dep.get.side_effect = _inventory_answers(
            [
                {"id": 1, "address": "10.0.0.1", "name": "db-mysql-prod-01"},
                {"id": 2, "address": "10.0.0.2", "name": "db-mysql-prod-02"},
            ],
            [
                {
                    "node_id": 1,
                    "can_elevate": "not-a-boolean",
                    "observed_at": "2026-09-15T00:00:00Z",
                }
            ],
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload
        assert {host["id"] for host in payload} == {"nomad-1", "nomad-2"}
        assert all(host["can_elevate"] is None for host in payload)

    def test_a_malformed_node_page_degrades_rather_than_500s(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Answer 200 when the node envelope itself is malformed.

        The display-name lookup degrades on the same terms as the capability one:
        every way an Inventory read can fail leaves this route serving the raw
        executor names rather than a 500.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}

        def _answers(path: str, **_kwargs: Any) -> dict[str, Any]:
            if path == "/nodes/":
                return {"items": "not-a-list", "total": "lots"}
            return {"items": []}

        mock_inventory_api_dep.get.side_effect = _answers
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "nomad-1",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_inventory_failure_returns_raw_node_names(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return 200 with the executor addresses when the Inventory API rejects the request.

        Both enrichments degrade together: the display name falls back to the
        executor name and the capability to never-observed.
        """
        mock_task_api_dep.get.return_value = {"nomad-1": "10.0.0.1"}
        mock_inventory_api_dep.get.side_effect = HTTPBadGatewayException(
            "inventory unreachable"
        )
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": "nomad-1",
                "name": "nomad-1",
                "address": "10.0.0.1",
                "can_elevate": None,
            },
        ]

    def test_empty_executor_list_returns_empty_response(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return an empty list when the executor reports no hosts."""
        mock_task_api_dep.get.return_value = {}
        mock_inventory_api_dep.get.side_effect = _inventory_answers([])
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_tasks_failure_returns_502(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return ``502`` + ``{"detail": ...}`` when the Tasks API is unreachable.

        Catch the upstream ``HTTPException`` and re-raise as
        :class:`~app.core.exceptions.HTTPBadGatewayException`; the PMM Extensions exception
        handler turns it into a JSON ``502`` response that the React frontend
        surfaces through React Query's error state.
        """
        mock_task_api_dep.get.side_effect = HTTPBadGatewayException("tasks unreachable")
        mock_inventory_api_dep.get.side_effect = _inventory_answers([])
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "tasks unreachable"}

    def test_tasks_structured_detail_returns_502_with_text(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Carry a structured upstream detail onto the ``502`` as text."""
        detail = {"message": "tasks unreachable"}
        mock_task_api_dep.get.side_effect = HTTPBadGatewayException(detail)
        mock_inventory_api_dep.get.side_effect = _inventory_answers([])
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": str(detail)}

    def test_tasks_oserror_returns_502(
        self,
        test_client: TestClient,
        mock_task_api_dep,
        mock_inventory_api_dep,
    ) -> None:
        """Return ``502`` + ``{"detail": ...}`` when the Tasks API raises an OSError."""
        mock_task_api_dep.get.side_effect = OSError("connection refused")
        mock_inventory_api_dep.get.side_effect = _inventory_answers([])
        response = test_client.get("/api/extensions/hosts/")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.json() == {"detail": "connection refused"}


class TestExtensionsHostsAuth:
    """Cover ``/api/extensions/hosts/`` authentication enforcement."""

    @pytest.fixture
    def unauthenticated_client(self) -> Iterator[TestClient]:
        """Yield a TestClient with no auth dependency overrides applied."""
        previous = extensions_app.dependency_overrides
        extensions_app.dependency_overrides = {}
        try:
            yield TestClient(extensions_app, raise_server_exceptions=False)
        finally:
            extensions_app.dependency_overrides = previous

    def test_unauthenticated_returns_json_401(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Reject anonymous requests with a JSON 401 response (not an HTML redirect)."""
        response = unauthenticated_client.get(
            "/api/extensions/hosts/", follow_redirects=False
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()

    def test_unauthenticated_unknown_extensions_path_returns_json_404(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Return JSON 404 for unknown ``/api/extensions/*`` paths even when unauthenticated.

        The 404 handler is unconditional now, so an unmatched path returns JSON
        regardless of whether the caller is authenticated.
        """
        response = unauthenticated_client.get(
            "/api/extensions/does-not-exist/", follow_redirects=False
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()

    def test_authenticated_unknown_extensions_path_returns_json_404(
        self, test_client: TestClient
    ) -> None:
        """Return JSON 404 for unknown ``/api/extensions/*`` paths under an authenticated client."""
        response = test_client.get(
            "/api/extensions/does-not-exist/", follow_redirects=False
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()
