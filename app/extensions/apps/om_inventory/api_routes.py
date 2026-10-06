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

"""Serve the estate OM holds, and the sweeps that filled it.

Every read here follows from one constraint: **the caller must never wait for a Nomad
job.** PMM assembles its topology document on the request path in about a tenth of a
second; a probe sweep takes tens of seconds. So nothing on this router probes. It
serves rows, each carrying when it was last collected, and leaves the caller to
decide — which it can, because the age travels with the data and the consumer merges
by precedence rather than by trust.

``GET /services`` is the contract with pmm-managed. It replaced ``GET /facts``, which
served the last sweep's output as a flat fact list: an estate that is *upserted*
answers "what is running on this service" even when the most recent sweep never
reached it, where the last sweep's output answered only "what did the last sweep
see".

Auth is applied at the mount level: ``/api/apps`` carries the ``IsApiAuthenticated``
router guard, and unsafe methods additionally require a bearer and the rank
:func:`app.api.deps.require_minimum_role` registers per route. An unregistered route
resolves to ``DEFAULT_MINIMUM_ROLE`` (admin), so every unsafe route here registers its
rank explicitly rather than inheriting one: the estate deletes and the configuration
writes are admin, and refreshing is **editor**, because a refresh runs a fixed payload
and is the button beside a row — requiring admin would put the routine question behind
the rarest role. pmm-managed's service principal is admitted by identity at that gate,
so PMM reaches all of these with its deployment token whatever rank a human needs.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi import status as http_status
from sqlmodel import col

from app.api.deps import require_minimum_role
from app.core.auth.models import UserRole
from app.core.exceptions import (
    HTTPConflictException,
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.pagination import PaginatedResponse, PaginationDep
from app.core.settings_override.api import (
    apply_class_overrides,
    clear_class_override,
    collect_class_setting_responses,
    SettingResponse,
    SettingsPatch,
)
from app.core.utils.fields import UTCDatetime
from app.extensions.apps.framework.api import schema_endpoint
from app.extensions.apps.om_inventory.celery import run_om_probe
from app.extensions.apps.om_inventory.config import (
    om_inventory_settings,
    OmInventorySettings,
)
from app.extensions.apps.om_inventory.crud import (
    conflict_detail,
    conflicting_run,
    delete_host,
    delete_service,
    get_host,
    get_run,
    get_service,
    list_services,
    OmHostManager,
    OmServiceManager,
    ProbeRunManager,
    recent_runs,
)
from app.extensions.apps.om_inventory.models import (
    HostResponse,
    OmHost,
    OmService,
    ProbeCounts,
    ProbeNode,
    ProbeRun,
    ProbeRunAccepted,
    ProbeRunDetail,
    ProbeRunResponse,
    ServiceResponse,
    TriggerRequest,
)
from app.extensions.apps.om_inventory.schema import om_inventory_schema
from app.extensions.apps.om_inventory.service import SWITCHED_OFF_DETAIL
from app.extensions.deps import ApiCurrentUser, SessionDep

router = APIRouter()
schema_endpoint(router=router, plugin_schema=om_inventory_schema)


#: Fields ``GET /config`` must not hand back even though the same settings class is
#: readable by every authenticated caller here, not only admins (see ``_redact_config``).
_REDACTED_CONFIG_KEYS = frozenset({"CREDENTIALS_PATH"})


def _redact_config(responses: list[SettingResponse]) -> list[SettingResponse]:
    """Redact fields too sensitive for this route's viewer-readable audience.

    ``GET /config`` is gated only by ``IsApiAuthenticated`` — any logged-in PMM Extensions
    user — because that is what the principal behind the pmm-managed
    ``--extensions-token`` flag needs to reach the rest of this settings class.
    ``CREDENTIALS_PATH`` is the one field that same audience must not see: it names
    a file a MongoDB driver reads as a URI, and the settings class docstring already
    calls it too sensitive to make writable, let alone world-readable to any
    signed-in viewer.

    The row still comes back, just with ``value`` forced to ``None``, rather than
    being dropped: the class default is already ``None``, so a redacted read and a
    genuinely unset deployment are indistinguishable, which is the honest answer,
    and no consumer has to special-case a missing row.

    :param responses: Every field's response, as collected for this settings class.
    :return: The same responses, with redacted keys' ``value`` set to ``None``.
    """
    return [
        response.model_copy(update={"value": None})
        if response.key in _REDACTED_CONFIG_KEYS
        else response
        for response in responses
    ]


def _service_response(service: OmService) -> ServiceResponse:
    """Project one service row for the wire.

    :param service: The stored row.
    :return: The response.
    """
    return ServiceResponse(
        service_id=service.service_id,
        node_id=service.node_id,
        name=service.name,
        port=service.port,
        role=service.role,
        observed=service.observed or {},
        first_seen_at=service.first_seen_at,
        last_attempt_at=service.last_attempt_at,
        last_success_at=service.last_success_at,
        failing_since=service.failing_since,
        consecutive_failures=service.consecutive_failures,
        last_error=service.last_error,
    )


def _executor_usable(host: OmHost) -> bool:
    """Return whether a payload can actually be dispatched to this host.

    Reads ``observed.executor``, which :func:`~app.extensions.apps.om_inventory.crud.
    upsert_host` writes on every sweep for every host regardless of whether it was
    probed — unlike ``executor_host``, which is set the moment *any* known executor
    matches, usable or not. A missing or absent sub-document reads as not usable,
    which is the honest answer for a host that has never been swept at all.

    :param host: The stored row.
    :return: ``True`` when the host's executor is reachable and driver-healthy.
    """
    executor = (host.observed or {}).get("executor") or {}
    return bool(executor.get("reachable")) and bool(executor.get("driver_healthy"))


def _host_response(host: OmHost, services: list[OmService]) -> HostResponse:
    """Project one host row, with its services nested.

    :param host: The stored row.
    :param services: Its service rows.
    :return: The response.
    """
    return HostResponse(
        node_id=host.node_id,
        name=host.name,
        address=host.address,
        executor_host=host.executor_host,
        observed=host.observed or {},
        first_seen_at=host.first_seen_at,
        last_attempt_at=host.last_attempt_at,
        last_success_at=host.last_success_at,
        failing_since=host.failing_since,
        consecutive_failures=host.consecutive_failures,
        last_error=host.last_error,
        services=[_service_response(service) for service in services],
    )


def _counts(run: ProbeRun) -> ProbeCounts:
    """Project a run's counters.

    :param run: The run.
    :return: Its counts.
    """
    return ProbeCounts(
        services_total=run.services_total,
        services_resolved=run.services_resolved,
        services_orphaned=run.services_orphaned,
        services_answered=run.services_answered,
        hosts_total=run.hosts_total,
        hosts_probeable=run.hosts_probeable,
        hosts_answered=run.hosts_answered,
    )


def _run_response(run: ProbeRun) -> ProbeRunResponse:
    """Project one run for the wire.

    :param run: The run.
    :return: The response.
    """
    return ProbeRunResponse(
        run_id=run.id,
        status=str(run.status),
        started_at=run.started_at,
        finished_at=run.finished_at,
        counts=_counts(run),
        scope=run.scope,
        error=run.error,
    )


@router.get("/hosts")
async def list_estate_hosts(
    session: SessionDep,
    pagination: PaginationDep,
    has_service: bool | None = Query(
        default=None,
        description="True for hosts running a MongoDB service, False for those with "
        "none. Omit for all of them.",
    ),
    failing: bool | None = Query(
        default=None, description="Restrict to hosts that are, or are not, failing."
    ),
    executor: bool | None = Query(
        default=None,
        description="True for hosts a payload can run on, False for those with no "
        "executor.",
    ),
) -> PaginatedResponse[HostResponse]:
    """Return every host OM holds, each with its services.

    ``has_service=false`` is the question this table exists to answer: which machines
    carry a PMM client and no database. It is a filter rather than an endpoint of its
    own so there is one list contract to learn, and because the same list with the
    filter inverted is the ordinary estate view.

    Counts describe the *tables*, not the last run. A scoped refresh must not make the
    estate look one host wide.

    ``total`` counts every host matching the filters, not the page, so a caller can
    tell "this is the whole estate" from "this is the first twenty of it".

    :param session: The database session.
    :param pagination: The offset/limit window for the page.
    :param has_service: Filter on whether a MongoDB service is registered here.
    :param failing: Filter on whether the host is currently failing.
    :param executor: Filter on whether an executor serves it.
    :return: One page of hosts, by name, with the matching total.
    """
    clauses = OmHostManager.estate_clauses(has_service=has_service, failing=failing)

    if executor is None:
        page = await OmHostManager.list_paginated(
            session,
            *clauses,
            pagination=pagination,
            order_by=[col(OmHost.name), col(OmHost.node_id)],
        )
        hosts, total = page.items, page.total
    else:
        # executor reads observed.executor, a JSON sub-document — not something
        # worth a dialect-specific path expression for SQLite and PostgreSQL
        # each, unlike has_service and failing above. Still bounded by
        # those two when given, rather than always reading the whole table: this
        # filter is the rare, deliberate query, not the default estate browse.
        candidates = await OmHostManager.list(
            session, *clauses, order_by=[col(OmHost.name), col(OmHost.node_id)]
        )
        matching = [host for host in candidates if _executor_usable(host) is executor]
        total = len(matching)
        hosts = pagination.slice(matching)

    node_ids = [host.node_id for host in hosts]
    services = (
        await OmServiceManager.list(
            session,
            col(OmService.node_id).in_(node_ids),
            order_by=[col(OmService.name)],
        )
        if node_ids
        else []
    )
    by_node: dict[str, list[OmService]] = {}
    for service in services:
        by_node.setdefault(service.node_id, []).append(service)

    items = [_host_response(host, by_node.get(host.node_id, [])) for host in hosts]
    return PaginatedResponse(
        items=items, total=total, offset=pagination.offset, limit=pagination.limit
    )


@router.get("/hosts/{node_id}")
async def get_estate_host(node_id: str, session: SessionDep) -> HostResponse:
    """Return one host, with its services.

    :param node_id: PMM's node id.
    :param session: The database session.
    :raises HTTPNotFoundException: When OM holds no such host.
    :return: The host.
    """
    host = await get_host(session, node_id)
    if host is None:
        raise HTTPNotFoundException(detail=f"Host {node_id} not found")
    return _host_response(host, await list_services(session, node_id=node_id))


@router.get("/services")
async def list_estate_services(
    session: SessionDep,
    pagination: PaginationDep,
    node_id: str | None = Query(default=None, description="Restrict to one host."),
    failing: bool | None = Query(
        default=None, description="Restrict to services that are, or are not, failing."
    ),
) -> PaginatedResponse[ServiceResponse]:
    """Return the services OM holds, flat.

    For a consumer that works in services and would otherwise walk every host document
    to find them. ``GET /hosts/{node_id}`` already nests a host's services, so there is
    deliberately no ``/hosts/{node_id}/services``: it would be a second spelling of the
    same list, and ``?node_id=`` covers wanting them without the host.

    ``total`` counts every service matching the filters, not the page.

    :param session: The database session.
    :param pagination: The offset/limit window for the page.
    :param node_id: Restrict to one host.
    :param failing: Filter on whether the service is currently failing.
    :return: One page of services, by name, with the matching total.
    """
    clauses = []
    if node_id is not None:
        clauses.append(col(OmService.node_id) == node_id)
    if failing is not None:
        clauses.append(
            col(OmService.failing_since).is_not(None)
            if failing
            else col(OmService.failing_since).is_(None)
        )
    page = await OmServiceManager.list_paginated(
        session,
        *clauses,
        pagination=pagination,
        order_by=[col(OmService.name), col(OmService.service_id)],
    )
    return page.map_items(_service_response)


@router.get("/services/{service_id}")
async def get_estate_service(service_id: str, session: SessionDep) -> ServiceResponse:
    """Return one service, by PMM's service id.

    :param service_id: PMM's service id.
    :param session: The database session.
    :raises HTTPNotFoundException: When OM holds no such service.
    :return: The service.
    """
    service = await get_service(session, service_id)
    if service is None:
        raise HTTPNotFoundException(detail=f"Service {service_id} not found")
    return _service_response(service)


@router.delete("/hosts/{node_id}", status_code=http_status.HTTP_204_NO_CONTENT)
@require_minimum_role(UserRole.ADMIN)
async def delete_estate_host(node_id: str, session: SessionDep) -> None:
    """Forget one host, and by cascade its services.

    For rows PMM no longer has, which is not hypothetical: restarting a node's
    pmm-agent runs ``setup --force``, which *replaces* the node and mints a new id, so
    OM gains a row and keeps the old one. Nothing prunes automatically yet, and the
    alternative to this endpoint is ``psql`` against a schema an operator should never
    need to know exists.

    Deliberately not suppression. An entity PMM still knows about comes straight back
    on the next sweep, because OM's job is to describe what PMM says exists, not to
    hold an opinion about it.

    Its services go with it. That is done explicitly rather than left to the
    ``ON DELETE CASCADE`` on ``om.om_service.node_id``, because SQLite enforces no
    foreign key without a per-connection pragma PMM Extensions never sets — and SQLite is the
    shipped default. See :func:`~app.extensions.apps.om_inventory.crud.delete_host`.

    :param node_id: PMM's node id.
    :param session: The database session.
    :raises HTTPNotFoundException: When OM holds no such host.
    """
    host = await get_host(session, node_id)
    if host is None:
        raise HTTPNotFoundException(detail=f"Host {node_id} not found")
    await delete_host(session, host)


@router.delete("/services/{service_id}", status_code=http_status.HTTP_204_NO_CONTENT)
@require_minimum_role(UserRole.ADMIN)
async def delete_estate_service(service_id: str, session: SessionDep) -> None:
    """Forget one service, leaving its host alone.

    Same contract as deleting a host: for a row PMM no longer has, and no defence
    against one it still does.

    :param service_id: PMM's service id.
    :param session: The database session.
    :raises HTTPNotFoundException: When OM holds no such service.
    """
    service = await get_service(session, service_id)
    if service is None:
        raise HTTPNotFoundException(detail=f"Service {service_id} not found")
    await delete_service(session, service)


@router.get("/runs")
async def list_runs(
    session: SessionDep,
    # pagination-ok: bounded by `limit`, which the query applies and caps at 100.
    limit: int = Query(default=20, ge=1, le=100),
    since: Annotated[
        UTCDatetime | None,
        Query(
            description="Inclusive lower bound on started_at. Omit for no lower bound."
        ),
    ] = None,
    until: Annotated[
        UTCDatetime | None,
        Query(
            description="Inclusive upper bound on started_at. Omit for no upper bound."
        ),
    ] = None,
) -> list[ProbeRunResponse]:
    """Return recent sweeps, newest first.

    ``since`` / ``until`` filter on ``started_at`` before ``limit`` is applied, so
    asking for last week is last week rather than "the twenty newest, then those
    that happen to fall in the week".

    :param session: The database session.
    :param limit: How many to return.
    :param since: Inclusive lower bound on ``started_at``.
    :param until: Inclusive upper bound on ``started_at``.
    :return: The sweeps.
    """
    if since is not None and until is not None and until < since:
        raise HTTPUnprocessableEntityException(detail="until must not be before since")
    return [
        _run_response(run)
        for run in await recent_runs(session, limit, since=since, until=until)
    ]


@router.get("/runs/{run_id}")
async def get_probe_run(run_id: UUID, session: SessionDep) -> ProbeRunDetail:
    """Return one sweep, with its per-host receipt.

    :param run_id: The sweep's id.
    :param session: The database session.
    :raises HTTPNotFoundException: When there is no such sweep.
    :return: The sweep in full.
    """
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPNotFoundException(detail=f"Probe run {run_id} not found")
    return ProbeRunDetail(
        **_run_response(run).model_dump(),
        # Runs recorded before `nodes` existed have none, and answer with an empty
        # list rather than a 500: an old sweep is still worth its counters.
        nodes=[ProbeNode(**node) for node in (run.nodes or [])],
    )


@router.post(
    "/runs",
    status_code=http_status.HTTP_202_ACCEPTED,
)
@require_minimum_role(UserRole.EDITOR)
async def trigger_probe(
    session: SessionDep, request: TriggerRequest | None = None
) -> ProbeRunAccepted:
    """Start a probe sweep, over the whole estate or over named hosts.

    A scoped refresh exists because the two questions are different sizes. "What does
    the estate look like" is a sweep of everything and costs a Nomad job per executor
    host — a minute and a half in this sandbox. "I just did something to this host,
    is it healthy now" should not cost that, and it is the question PMM's UI will ask
    after every action it grows.

    The scope is node ids, which is what PMM already holds, so its trigger passes them
    through untranslated — the payoff for keying these tables on PMM's own ids rather
    than minting OM ones.

    Conflict is judged **per host**, not globally. A refresh of one host has no reason
    to be blocked by a refresh of another, and blocking it would make the scoped
    trigger useless exactly when the estate is busiest. Two runs collide only when
    they would touch the same host; a full refresh collides with everything, including
    another full refresh.

    :param session: The database session.
    :param request: The optional scope. Absent, or an empty list, means everything.
    :raises HTTPServiceUnavailableException: When PMM's OpenManager switch has
        ``ENABLED`` off.
    :raises HTTPNotFoundException: When a requested node id is not in the estate.
    :raises HTTPConflictException: When a requested host is already being refreshed.
    :return: The queued sweep.
    """
    if not om_inventory_settings.ENABLED:
        raise HTTPServiceUnavailableException(detail=SWITCHED_OFF_DETAIL)

    node_ids = list(dict.fromkeys(request.node_ids)) if request else []

    # An id OM does not hold is answered by name rather than by running a refresh
    # that would quietly do nothing. PMM Extensions' inventory copy can lag PMM's, so this is a
    # real case rather than a typo guard.
    for node_id in node_ids:
        if await get_host(session, node_id) is None:
            raise HTTPNotFoundException(detail=f"Host {node_id} not found")

    # The same check the sweep itself makes, so a caller and the schedule cannot
    # disagree about what counts as a conflict.
    blocking = await conflicting_run(
        session, node_ids, stale_after=om_inventory_settings.STALE_RUN_AFTER
    )
    if blocking is not None:
        raise HTTPConflictException(detail=conflict_detail(blocking, node_ids))

    run = await ProbeRunManager.save(session, ProbeRun(scope=node_ids or None))
    run_om_probe.delay(str(run.id), node_ids or None)
    return ProbeRunAccepted(
        run_id=run.id,
        status=str(run.status),
        started_at=run.started_at,
        scope=run.scope,
    )


@router.get("/config")
# pagination-ok: one row per hot field on OmInventorySettings, so the cardinality is
# the class's field count and cannot grow with the estate.
async def get_config(session: SessionDep) -> list[SettingResponse]:
    """Return this app's configuration: every field, its value and its origin.

    Served here rather than pointing the caller at ``/api/extensions/admin/settings``
    because that router is admin-gated and PMM's principal is not an admin: the
    bearer of the pmm-managed ``--extensions-token`` flag resolves to the synthetic
    ``extensions-service`` user, built with ``is_admin=False`` deliberately, since
    it is a deployment-level shared secret with no person behind it. An app-owned
    endpoint keeps a schedule change scoped to this app instead of requiring PMM
    Extensions wide administrative access.

    Every field is listed, not only the overridden ones, and each row carries
    whether an override is in effect - so "why is it sweeping every 10 minutes"
    is answerable without also reading the deployment's YAML.

    ``CREDENTIALS_PATH`` is the one exception: its row is present, with ``value``
    forced to ``None`` regardless of the deployment's real setting. See
    :func:`_redact_config` for why the whole route is not gated instead.

    :param session: The database session.
    :return: One row per configuration field.
    """
    return _redact_config(
        await collect_class_setting_responses(
            session=session,
            setting_class=OmInventorySettings.__name__,
            settings_cls=OmInventorySettings,
            proxy=om_inventory_settings,  # ty: ignore[invalid-argument-type]
        )
    )


@router.patch("/config")
@require_minimum_role(UserRole.ADMIN)
# pagination-ok: one row per key in the request body, so the caller already bounds it.
async def patch_config(
    request: Request,
    body: SettingsPatch,
    session: SessionDep,
    actor: ApiCurrentUser,
) -> list[SettingResponse]:
    """Update this app's configuration at runtime.

    The batch is atomic: a single bad key rejects all of it with a per-key 422 and
    writes nothing, so a caller never has to work out how far a partial apply got.

    Only ``hot_field`` fields are accepted. ``CREDENTIALS_PATH`` is deliberately
    not one: it names a file the payload reads on every database *host* and hands
    to a driver as a URI, so making it settable here would widen "configure this
    app" into "read a chosen file across the estate".

    An ``ENABLED`` or ``SCHEDULE`` change lands without a restart -
    ``periodic_task_schedules`` is a thunk re-read on registry rebuild - but beat
    runs as a forked side-car process, which reaches the new value through its own
    settings refresher rather than through this request.

    ``ENABLED`` is what PMM's OpenManager switch calls, via this same route with
    the credential of the pmm-managed ``--extensions-token`` flag (see
    ``require_minimum_role``'s service-principal bypass): it flips independently
    of ``SCHEDULE``, so the configured cadence survives OpenManager being turned
    off and back on rather than being overwritten each time.

    :param request: The incoming request; its ``app.state`` carries the rebind
        callbacks fired for the keys this changed.
    :param body: The ``{key: value, ...}`` batch.
    :param session: The database session.
    :param actor: The calling principal, whose username is recorded on every
        row written. Not ``ApiAdminUsername``: this endpoint admits any
        authenticated caller, PMM's non-admin service principal included.
    :return: One row per applied key, in input order.
    """
    return await apply_class_overrides(
        request=request,
        session=session,
        setting_class=OmInventorySettings.__name__,
        settings_cls=OmInventorySettings,
        proxy=om_inventory_settings,  # ty: ignore[invalid-argument-type]
        body=body,
        actor=actor.username,
    )


@router.delete("/config/{key}", status_code=http_status.HTTP_204_NO_CONTENT)
@require_minimum_role(UserRole.ADMIN)
async def delete_config_override(
    request: Request, key: str, session: SessionDep
) -> None:
    """Put one field back to whatever the deployment configured.

    Without this, an operator who once changed a value can only ever change it to
    another one: "no override" stops being a reachable state, and the YAML the
    deployment ships becomes unrecoverable through the API. Idempotent, so
    clearing a field that was never overridden is not an error.

    :param request: The incoming request; its ``app.state`` carries the rebind
        callbacks fired for the reverted key.
    :param key: The field name, or a ``__``-delimited nested key such as
        ``SCHEDULE__every``.
    :param session: The database session.
    """
    await clear_class_override(
        request=request,
        session=session,
        setting_class=OmInventorySettings.__name__,
        settings_cls=OmInventorySettings,
        proxy=om_inventory_settings,  # ty: ignore[invalid-argument-type]
        key=key,
    )
