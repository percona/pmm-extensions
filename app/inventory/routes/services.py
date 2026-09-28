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

"""Define the routes for the Services resource."""

import logging

from fastapi import APIRouter, status
from sqlmodel import col

from app.api.deps import CurrentUserID, IsAuthenticatedDep, IsServicePrincipalDep
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.core.utils.fields import NonEmptyStr
from app.inventory.constants import RetirableEntityName
from app.inventory.crud import (
    ExternalIdentityAliasManager,
    SchemaManager,
    ServiceManager,
    ServiceSystemObservationManager,
)
from app.inventory.deps import (
    InScopeServiceDep,
    RetirableServiceDep,
    SchemaListQueryDep,
    SchemaScopeDep,
    ServiceDep,
    ServiceListQueryDep,
    ServiceScopeDep,
    ServiceSystemObservationDep,
    SessionDep,
)
from app.inventory.models import (
    ExternalIdentityAlias,
    ExternalIdentityAliasResponse,
    IdentityLinkDecisionWrite,
    Schema,
    SchemaCompactResponse,
    SchemaResponse,
    SchemaWrite,
    Service,
    ServiceDetailResponse,
    ServiceIdentityCandidateResponse,
    ServiceResponse,
    ServiceSystemObservation,
    ServiceSystemObservationResponse,
    ServiceSystemObservationWrite,
    ServiceTypeEnum,
    ServiceWrite,
    SyncHealthWrite,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/services", tags=["services"])


@router.get("/", dependencies=[IsAuthenticatedDep])
async def list_services(
    session: SessionDep,
    pagination: PaginationDep,
    list_query: ServiceListQueryDep,
    manager: ServiceScopeDep,
    external_id: NonEmptyStr | None = None,
    service_type: ServiceTypeEnum | None = None,
) -> PaginatedResponse[ServiceResponse]:
    """List Services.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request boundary.
    :param manager: The service manager the request's retirement scope selected.
    :param external_id: Return only the service carrying this upstream identifier,
        resolved through any identity alias recorded for it.
    :param service_type: Return only services of this type.
    :return: A paginated response of service responses.
    """
    logger.debug("Listing services for type '%s'", service_type or "all")
    resolved_id: int | None = None
    if external_id is not None:
        resolved_id = await ExternalIdentityAliasManager.resolve_entity_id(
            session, RetirableEntityName.SERVICE, None, external_id
        )
    identity_filter = (
        {"id": resolved_id} if resolved_id is not None else {"external_id": external_id}
    )
    return await manager.list_query_paginated(
        session,
        list_query=list_query,
        select_related=[Service.schemas, Service.node],
        pagination=pagination,
        type=service_type,
        **identity_filter,
    )


@router.get("/identity-candidates", dependencies=[IsAuthenticatedDep])
async def list_service_identity_candidates(
    session: SessionDep, pagination: PaginationDep
) -> PaginatedResponse[ServiceIdentityCandidateResponse]:
    """List service pairings a PMM re-registration may have split.

    Declared above ``GET /{service_id}``: FastAPI matches path operations in
    declaration order, so the parameterized route would claim this path first and
    answer 422 on the unparseable identifier rather than 404.

    :param session: The async database session.
    :param pagination: Validated offset/limit query parameters.
    :return: A paginated response of candidate pairings.
    """
    candidates, total = await ServiceManager.identity_candidates(
        session, pagination=pagination
    )
    return PaginatedResponse.from_pagination(
        [
            ServiceIdentityCandidateResponse(
                predecessor=ServiceResponse.model_validate(
                    candidate.predecessor, from_attributes=True
                ),
                successor=ServiceResponse.model_validate(
                    candidate.successor, from_attributes=True
                ),
                matched_on=candidate.matched_on,
            )
            for candidate in candidates
        ],
        total,
        pagination,
    )


@router.get("/{service_id}", dependencies=[IsAuthenticatedDep])
async def retrieve_service(
    session: SessionDep,
    service_id: int,
    manager: ServiceScopeDep,
) -> ServiceDetailResponse:
    """Retrieve Service."""
    logger.debug("Retrieving service %s", service_id)
    return await manager.get_or_404(
        session,
        select_related=[Service.schemas, Service.node],
        id=service_id,
    )


@router.put("/{service_id}", dependencies=[IsServicePrincipalDep])
async def update_service(
    session: SessionDep,
    existing_service: ServiceDep,
    updated_service: ServiceWrite,
) -> Service:
    """Update Service."""
    logger.debug("Updating service %s", existing_service.id)
    return await ServiceManager.update(session, existing_service, updated_service)


@router.delete(
    "/{service_id}",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def retire_service(session: SessionDep, service: RetirableServiceDep) -> None:
    """Retire Service and everything below it, keeping the rows resolvable.

    :param session: The asynchronous database session.
    :param service: The service to retire, retired or not.
    """
    logger.debug("Retiring service %s", service.id)
    await ServiceManager.retire(session, service)


@router.post(
    "/{service_id}/revive",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revive_service(session: SessionDep, service: RetirableServiceDep) -> None:
    """Revive a retired Service together with its retired ancestors.

    :param session: The asynchronous database session.
    :param service: The service to revive, retired or not.
    :raises HTTPConflictException: If an active entity already holds the unique
        key the revived service would reclaim.
    """
    logger.debug("Reviving service %s", service.id)
    await ServiceManager.revive(session, service)


@router.post(
    "/{service_id}/sync-health",
    dependencies=[IsServicePrincipalDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_service_sync_health(
    session: SessionDep,
    service: RetirableServiceDep,
    outcome: SyncHealthWrite,
) -> None:
    """Record the outcome of one syncer attempt on a Service.

    Addresses the service whether retired or not: the attempt happened, and a
    concurrent retirement must not turn bookkeeping into a failed sync item.

    :param session: The async database session.
    :param service: The service the outcome was observed for, retired or not.
    :param outcome: What the syncer reported.
    """
    logger.debug("Recording %s sync health on service %s", outcome.outcome, service.id)
    await ServiceManager.record_sync_health(session, service, outcome)


@router.get(
    "/{service_id}/system-observation",
    dependencies=[IsAuthenticatedDep],
    response_model=ServiceSystemObservationResponse,
)
async def retrieve_service_system_observation(
    observation: ServiceSystemObservationDep,
) -> ServiceSystemObservation:
    """Retrieve service system observation for a service."""
    return observation


@router.put("/{service_id}/system-observation", dependencies=[IsAuthenticatedDep])
async def upsert_service_system_observation(
    session: SessionDep,
    service: ServiceDep,
    data: ServiceSystemObservationWrite,
) -> ServiceSystemObservationResponse:
    """Upsert service system observation for a service."""
    data.service_id = service.id
    obs, created = await ServiceSystemObservationManager.get_or_create(
        session, data, filter_include={"service_id"}
    )
    if not created:
        obs = await ServiceSystemObservationManager.update(session, obs, data)
    return obs


@router.get("/{service_id}/schemas/", dependencies=[IsAuthenticatedDep])
async def list_schemas_by_service(
    session: SessionDep,
    service: InScopeServiceDep,
    pagination: PaginationDep,
    list_query: SchemaListQueryDep,
    manager: SchemaScopeDep,
    include_tables: str | None = None,
) -> PaginatedResponse[SchemaResponse | SchemaCompactResponse]:
    """List Schemas by Service.

    Return ``SchemaResponse`` (with nested tables) when ``include_tables``
    is set, otherwise return ``SchemaCompactResponse`` (without tables).

    :param session: The async database session.
    :param service: The service addressed by the path, resolved within the
        request's retirement scope.
    :param pagination: Validated offset/limit query parameters.
    :param list_query: The resolved sort/search produced at the request
        boundary.
    :param manager: The schema manager the request's retirement scope selected.
    :param include_tables: Include nested tables in the response when set to
        any non-empty value. Defaults to compact mode (no tables).
    :return: A paginated response of schema responses.
    """
    logger.debug("Listing schemas for service '%s'", service.id)
    select_related = [Schema.tables] if include_tables else []
    result = await manager.list_query_paginated(
        session,
        list_query=list_query,
        select_related=select_related,
        pagination=pagination,
        service_id=service.id,
    )
    if include_tables:
        return result.map_items(
            lambda schema: SchemaResponse.model_validate(schema, from_attributes=True)
        )
    return result.map_items(
        lambda schema: SchemaCompactResponse.model_validate(
            schema, from_attributes=True
        )
    )


@router.post(
    "/{service_id}/schemas/",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_201_CREATED,
)
async def create_schema_for_service(
    session: SessionDep,
    service: ServiceDep,
    schema: SchemaWrite,
) -> Schema:
    """Create Schema for Service."""
    logger.debug("Creating schema for service %s: %s", service.id, schema)
    return await SchemaManager.create(session, schema, service_id=service.id)


@router.post(
    "/{service_id}/identity-link",
    dependencies=[IsAuthenticatedDep],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def decide_service_identity_link(
    session: SessionDep,
    service: RetirableServiceDep,
    decision: IdentityLinkDecisionWrite,
    principal: CurrentUserID,
) -> None:
    """Confirm, reject or reverse a candidate service pairing.

    The path names the **predecessor** — the survivor of a confirmation.

    Carries ``IsAuthenticatedDep`` and deliberately not ``IsServicePrincipalDep``:
    an identity link is an operator judgement, not a row the syncer owns.

    :param session: The async database session.
    :param service: The predecessor addressed by the path, retired or not.
    :param decision: What the operator decided, and about which successor.
    :param principal: The caller recorded on the resulting records.
    :raises HTTPBadRequestException: If the body names the service itself, or both
        rows already hold one identifier.
    :raises HTTPNotFoundException: If a confirmation or rejection names a
        successor that does not exist. A reversal reports the same absence as a
        conflict, the pairing it would reverse no longer being reversible.
    :raises HTTPConflictException: If the decision does not apply to the pairing
        as it currently stands.
    """
    logger.debug("Deciding %s on service %s", decision.decision, service.id)
    await ServiceManager.decide_identity_link(
        session, service, decision, principal=principal
    )


@router.get("/{service_id}/identity-aliases", dependencies=[IsAuthenticatedDep])
async def list_service_identity_aliases(
    session: SessionDep, service: RetirableServiceDep, pagination: PaginationDep
) -> PaginatedResponse[ExternalIdentityAliasResponse]:
    """List the upstream identifiers this service has answered for, oldest first.

    :param session: The async database session.
    :param service: The service addressed by the path, retired or not.
    :param pagination: Validated offset/limit query parameters.
    :return: A paginated response of the service's binding records, oldest first.
    """
    return await ExternalIdentityAliasManager.list_paginated(
        session,
        order_by=[col(ExternalIdentityAlias.id)],
        pagination=pagination,
        entity_type=RetirableEntityName.SERVICE,
        entity_id=service.id,
    )
