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

"""Define the routes for the Nodes resource."""

import logging

from fastapi import APIRouter, status
from sqlalchemy.orm import load_only
from sqlmodel import col

from app.api.deps import CurrentUserID, IsAuthenticatedDep, IsServicePrincipalDep
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.core.utils.fields import NonEmptyStr
from app.inventory.constants import RetirableEntityName
from app.inventory.crud import (
    ExternalIdentityAliasManager,
    HostSystemObservationManager,
    NodeManager,
    ServiceManager,
)
from app.inventory.deps import (
    HostSystemObservationDep,
    InScopeNodeDep,
    NodeDep,
    NodeListQueryDep,
    NodeScopeDep,
    RetirableNodeDep,
    ServiceListQueryDep,
    ServiceScopeDep,
    SessionDep,
)
from app.inventory.models import (
    ExternalIdentityAlias,
    ExternalIdentityAliasResponse,
    HostSystemObservation,
    HostSystemObservationResponse,
    HostSystemObservationSummaryResponse,
    HostSystemObservationWrite,
    IdentityLinkDecisionWrite,
    Node,
    NodeIdentityCandidateResponse,
    NodeResponse,
    NodeWrite,
    Service,
    ServiceResponse,
    ServiceTypeEnum,
    ServiceWrite,
    SourceEnum,
    SyncHealthWrite,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("/", dependencies=[IsAuthenticatedDep])
async def list_nodes(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: NodeListQueryDep,
    manager: NodeScopeDep,
    external_id: NonEmptyStr | None = None,
    source: SourceEnum | None = None,
    node_type: NonEmptyStr | None = None,
) -> PaginatedResponse[NodeResponse]:
    """List Nodes from Inventory.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request boundary.
    :param manager: The node manager the request's retirement scope selected.
    :param external_id: Return only the node carrying this upstream identifier,
        resolved through any identity alias recorded for it.
    :param source: Return only nodes discovered by this source.
    :param node_type: Return only nodes of this type.
    :return: A paginated response of node responses.
    """
    logger.debug(
        "Listing nodes for source '%s' and type '%s'",
        source or "all",
        node_type or "all",
    )
    resolved_id: int | None = None
    if external_id is not None:
        resolved_id = await ExternalIdentityAliasManager.resolve_entity_id(
            session, RetirableEntityName.NODE, source, external_id
        )
    identity_filter = (
        {"id": resolved_id} if resolved_id is not None else {"external_id": external_id}
    )
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        select_related=[Node.services],
        pagination=pagination,
        source=source,
        type=node_type,
        **identity_filter,
    )


@router.get("/identity-candidates", dependencies=[IsAuthenticatedDep])
async def list_node_identity_candidates(
    session: SessionDep, pagination: PaginationDep
) -> PaginatedResponse[NodeIdentityCandidateResponse]:
    """List node pairings a PMM re-registration may have split.

    Declared above ``GET /{node_id}``: FastAPI matches path operations in
    declaration order, so the parameterized route would claim this path first and
    answer 422 on the unparseable identifier rather than 404.

    Built through :meth:`PaginatedResponse.from_pagination` rather than
    ``list_query_paginated``, the item being a pair of rows and not a single
    model.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :return: A paginated response of candidate pairings.
    """
    candidates, total = await NodeManager.identity_candidates(
        session, pagination=pagination
    )
    return PaginatedResponse.from_pagination(
        [
            NodeIdentityCandidateResponse(
                predecessor=NodeResponse.model_validate(
                    candidate.predecessor, from_attributes=True
                ),
                successor=NodeResponse.model_validate(
                    candidate.successor, from_attributes=True
                ),
                matched_on=candidate.matched_on,
            )
            for candidate in candidates
        ],
        total,
        pagination,
    )


@router.get(
    "/system-observations",
    dependencies=[IsAuthenticatedDep],
    response_model=PaginatedResponse[HostSystemObservationSummaryResponse],
)
async def list_host_system_observations(
    session: SessionDep, pagination: PaginationDep
) -> PaginatedResponse[HostSystemObservation]:
    """List host system observations across every node.

    Declared above ``GET /{node_id}`` for the reason
    :func:`list_node_identity_candidates` gives: FastAPI matches path operations in
    declaration order, so the parameterized route would claim this path and answer
    422 rather than 404.

    The narrow ``response_model`` drops the two JSON blobs only after they have been
    fetched and deserialized, so ``load_only`` keeps them out of the query itself.
    Without it a fleet-wide page carries every node's full package list, which is
    the cost this route exists to avoid.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :return: A paginated response of observation summaries.
    """
    return await HostSystemObservationManager.list_paginated(
        session,
        pagination=pagination,
        query_options=[
            # SQLModel annotates its columns as the Python types they carry, while
            # `load_only` is typed for SQLAlchemy's descriptors. `col()` does not
            # bridge it either — it yields `Mapped[T]`, which is broader than the
            # `QueryableAttribute` the stub asks for.
            load_only(
                HostSystemObservation.node_id,  # ty: ignore[invalid-argument-type]
                HostSystemObservation.can_elevate,  # ty: ignore[invalid-argument-type]
                HostSystemObservation.observed_at,  # ty: ignore[invalid-argument-type]
            )
        ],
    )


@router.get("/{node_id}", dependencies=[IsAuthenticatedDep])
async def retrieve_node(
    session: SessionDep,
    node_id: int,
    manager: NodeScopeDep,
) -> NodeResponse:
    """Retrieve Node from inventory.

    :param session: The async database session.
    :param node_id: The identifier of the node to retrieve.
    :param manager: The node manager the request's retirement scope selected.
    :return: The node, with its services nested.
    :raises HTTPNotFoundException: If no node in scope has the given identifier.
    """
    logger.debug("Retrieving node %s", node_id)
    return await manager.get_or_404(
        session,
        select_related=[Node.services],
        id=node_id,
    )


@router.post(
    "/", dependencies=[IsServicePrincipalDep], status_code=status.HTTP_201_CREATED
)
async def create_node(session: SessionDep, node: NodeWrite) -> Node:
    """Create Node."""
    logger.debug("Creating node %s", node)
    return await NodeManager.create(session, node)


@router.put("/{node_id}", dependencies=[IsServicePrincipalDep])
async def update_node(
    session: SessionDep,
    existing_node: NodeDep,
    updated_node: NodeWrite,
) -> Node:
    """Update Node.

    :param session: The async database session.
    :param existing_node: The active node addressed by the path.
    :param updated_node: The fields to write onto it.
    :return: The updated node.
    """
    logger.debug("Updating node %s", existing_node.id)
    return await NodeManager.update(session, existing_node, updated_node)


@router.delete(
    "/{node_id}",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def retire_node(session: SessionDep, node: RetirableNodeDep) -> None:
    """Retire Node and everything below it, keeping the rows resolvable.

    :param session: The asynchronous database session.
    :param node: The node to retire, retired or not.
    """
    logger.debug("Retiring node %s", node.id)
    await NodeManager.retire(session, node)


@router.post(
    "/{node_id}/revive",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revive_node(session: SessionDep, node: RetirableNodeDep) -> None:
    """Revive a retired Node, leaving the services it was retired with retired.

    :param session: The asynchronous database session.
    :param node: The node to revive, retired or not.
    :raises HTTPConflictException: If an active entity already holds the unique
        key the revived node would reclaim.
    """
    logger.debug("Reviving node %s", node.id)
    await NodeManager.revive(session, node)


@router.post(
    "/{node_id}/sync-health",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_node_sync_health(
    session: SessionDep,
    node: RetirableNodeDep,
    outcome: SyncHealthWrite,
) -> None:
    """Record the outcome of one syncer attempt on a Node.

    Addresses the node whether retired or not: the attempt happened, and a
    concurrent retirement must not turn bookkeeping into a failed sync item.

    :param session: The async database session.
    :param node: The node the outcome was observed for, retired or not.
    :param outcome: What the syncer reported.
    """
    logger.debug("Recording %s sync health on node %s", outcome.outcome, node.id)
    await NodeManager.record_sync_health(session, node, outcome)


@router.get(
    "/{node_id}/system-observation",
    dependencies=[IsAuthenticatedDep],
    response_model=HostSystemObservationResponse,
)
async def retrieve_host_system_observation(
    observation: HostSystemObservationDep,
) -> HostSystemObservation:
    """Retrieve host system observation for a node."""
    return observation


@router.put("/{node_id}/system-observation", dependencies=[IsAuthenticatedDep])
async def upsert_host_system_observation(
    session: SessionDep,
    node: NodeDep,
    data: HostSystemObservationWrite,
) -> HostSystemObservationResponse:
    """Upsert host system observation for a node.

    :param session: The async database session.
    :param node: The active node the observation belongs to.
    :param data: The observed host facts to store.
    :return: The stored observation.
    """
    data.node_id = node.id
    obs, created = await HostSystemObservationManager.get_or_create(
        session, data, filter_include={"node_id"}
    )
    if not created:
        obs = await HostSystemObservationManager.update(session, obs, data)
    return obs


@router.get("/{node_id}/services/", dependencies=[IsAuthenticatedDep])
async def list_services_by_node(
    session: SessionDep,
    node: InScopeNodeDep,
    pagination: PaginationDep,
    list_query: ServiceListQueryDep,
    manager: ServiceScopeDep,
    service_type: ServiceTypeEnum | None = None,
) -> PaginatedResponse[ServiceResponse]:
    """List Services by Node.

    :param session: The async database session.
    :param node: The node addressed by the path, resolved within the request's
        retirement scope.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request
        boundary.
    :param manager: The service manager the request's retirement scope selected.
    :param service_type: Restrict the listing to services of this type.
    :return: A paginated response of the node's services.
    """
    logger.debug(
        "Listing services for node '%s' and type '%s'",
        node.id,
        service_type or "all",
    )
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        select_related=[Service.schemas],
        pagination=pagination,
        node_id=node.id,
        type=service_type,
    )


@router.post(
    "/{node_id}/services/",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_201_CREATED,
)
async def create_service_for_node(
    session: SessionDep,
    node: NodeDep,
    service: ServiceWrite,
) -> Service:
    """Create Service for Node."""
    logger.debug("Creating service for node %s: %s", node.id, service)
    return await ServiceManager.create(session, service, node_id=node.id)


@router.post(
    "/{node_id}/identity-link",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def decide_node_identity_link(
    session: SessionDep,
    node: RetirableNodeDep,
    decision: IdentityLinkDecisionWrite,
    principal: CurrentUserID,
) -> None:
    """Confirm, reject or reverse a candidate node pairing.

    The path names the **predecessor** — the survivor of a confirmation, and the
    row the operator is acting on in all three decisions.

    Carries ``IsAuthenticatedDep`` and deliberately not ``IsServicePrincipalDep``:
    an identity link is an operator judgement, not a row the syncer owns. The
    app-wide unsafe-method gate already makes the route admin-only for a human
    while admitting the principal by identity.

    :param session: The async database session.
    :param node: The predecessor addressed by the path, retired or not.
    :param decision: What the operator decided, and about which successor.
    :param principal: The caller recorded on the resulting records.
    :raises HTTPBadRequestException: If the body names the node itself, or both
        rows already hold one identifier.
    :raises HTTPNotFoundException: If a confirmation or rejection names a
        successor that does not exist. A reversal reports the same absence as a
        conflict, the pairing it would reverse no longer being reversible.
    :raises HTTPConflictException: If the decision does not apply to the pairing
        as it currently stands.
    """
    logger.debug("Deciding %s on node %s", decision.decision, node.id)
    await NodeManager.decide_identity_link(session, node, decision, principal=principal)


@router.get("/{node_id}/identity-aliases", dependencies=[IsAuthenticatedDep])
async def list_node_identity_aliases(
    session: SessionDep, node: RetirableNodeDep, pagination: PaginationDep
) -> PaginatedResponse[ExternalIdentityAliasResponse]:
    """List the upstream identifiers this node has answered for, oldest first.

    A node no link has ever touched has no records, which is an empty page rather
    than a 404.

    :param session: The async database session.
    :param node: The node addressed by the path, retired or not.
    :param pagination: Validated offset/limit query parameters.
    :return: A paginated response of the node's binding records, oldest first.
    """
    return await ExternalIdentityAliasManager.list_paginated(
        session,
        order_by=[col(ExternalIdentityAlias.id)],
        pagination=pagination,
        entity_type=RetirableEntityName.NODE,
        entity_id=node.id,
    )
