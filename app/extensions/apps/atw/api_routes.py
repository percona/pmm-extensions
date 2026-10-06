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

"""Define the JSON API router for the ATW plugin."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, status
from kombu.exceptions import KombuError
from pydantic import BaseModel, UUID4
from sqlmodel import col

from app.core.exceptions import (
    HTTPNotFoundException,
    HTTPServiceUnavailableException,
    HTTPUnprocessableEntityException,
)
from app.core.pagination import PaginatedResponse
from app.core.pagination.deps import PaginationDep
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.core.utils.fields import StrippedNonEmptyStr
from app.core.utils.iterators import unique_everseen
from app.extensions.apps.atw.batch import (
    ATWBatchExecuteItemResponse,
    ATWBatchExecuteResponse,
    ATWBatchExecuteWrite,
    ATWIncidentExecutionResponse,
    ATWMergedSchemaResponse,
    ATWSnippetSchema,
    batch_execution_fields,
    dispatch_batch_item,
    fetch_task_history,
    MAX_BATCH_SNIPPETS,
    parameter_fields,
    resolve_snippets,
    shared_field_names,
)
from app.extensions.apps.atw.categories import (
    ATWCategory,
    CATEGORY_ROOT_LABELS,
    derive_category_root,
)
from app.extensions.apps.atw.celery import send_incident_diagnostics
from app.extensions.apps.atw.crud import (
    AtwIncidentExecutionManager,
    AtwIncidentManager,
    AtwSendLogManager,
    IncidentRunAggregate,
)
from app.extensions.apps.atw.deps import (
    AtwIncidentDep,
    AtwSnippetSearchQueryDep,
    ClosedAtwIncidentDep,
    diagnostics_case_search_available,
    diagnostics_send_disabled_reasons,
    IsDiagnosticsSendConfigured,
    OpenAtwIncidentDep,
)
from app.extensions.apps.atw.models import (
    AtwCaseMatch,
    AtwCaseSearchResponse,
    AtwConfigResponse,
    AtwIncident,
    AtwIncidentExecution,
    AtwIncidentResponse,
    AtwIncidentUpdate,
    AtwIncidentWrite,
    AtwSendJobWrite,
    AtwSendLog,
    AtwSendLogResponse,
    AtwSendStatusEnum,
)
from app.extensions.apps.atw.proxy_tasks import resolve_atw_proxy_tasks
from app.extensions.apps.atw.schema import atw_schema
from app.extensions.apps.framework.api import schema_endpoint
from app.extensions.bundle_upload.factory import get_delivery_executor
from app.extensions.bundle_upload.resolver import resolve_delivery_plan
from app.extensions.deps import ApiCurrentUser, IsApiAdmin, SessionDep, TaskAPI
from app.extensions.snippets.config import SnippetSudoRequirement
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.masking import mask_snippet_args
from app.extensions.snippets.models import Snippet
from app.extensions.snippets.models.meta import (
    META_KEY_ATW,
    META_KEY_DIAGNOSTIC_CATEGORIES,
    META_KEY_SERVICE_TYPE,
)
from app.extensions.snippets.script_source import (
    snippet_not_found_detail,
    SnippetScript,
)
from app.tasks.execution_request_secrets import ARGS_LEAF

logger = logging.getLogger(__name__)

ATW_META_WARNING = "Ignoring meta[%r] for snippet %s: expected list, got %s"
ATW_META_ELEMENT_WARNING = (
    "Ignoring meta[%r] for snippet %s: expected list[str], got %s element"
)
ATW_ARG_MASKING_WARNING = (
    "Withholding recorded arguments for snippet %s: masking them failed"
)
ATW_SNIPPET_RESOLUTION_WARNING = (
    "Snippets could not be resolved for the ATW incident page; "
    "every row withholds its arguments"
)
NO_TASK_ID_ERROR = "Dispatched, but the Tasks API returned no task id; not recorded."
UNRECORDED_EXECUTION_ERROR = "Dispatched, but the execution row could not be recorded"
_MISSING_META = object()

#: How long a case search may take before the field falls back to free text.
#: Deliberately far below the delivery probe's 15s and the intra-cluster 5s:
#: those bound a one-off operator action, while this is issued while someone is
#: still typing. ``RemoteAPI`` carries only a session-level timeout
#: (``sock_read=120``), so this is what actually bounds the call.
CASE_SEARCH_TIMEOUT_SECONDS = 3

#: The longest search term the route forwards to the receiver. A case reference
#: or a title fragment is far shorter; the cap is what keeps an arbitrary string
#: out of the provider's query.
MAX_CASE_SEARCH_TERM_LENGTH = 128

#: The most matches the route offers the dialog. ``CaseSearchStep`` declares no
#: limit of its own, so without this the response's cardinality is whatever the
#: receiver returns.
MAX_CASE_SEARCH_MATCHES = 25


class ATWSnippetSummary(BaseModel):
    """Represent one snippet entry under an ATW category.

    :param name: The snippet filename, used as its API identifier.
    :param title: The snippet display title.
    :param description: The snippet free-text description.
    :param sudo: Whether the snippet's elevation is never wanted, optional, or
        mandatory, letting a client warn before dispatching it to a host that
        cannot elevate. Nullable only so the field is additive on an already
        released model: every response this version builds populates it, and a
        ``None`` means the server predates the field.
    """

    name: str
    title: str
    description: str
    sudo: SnippetSudoRequirement | None = None


class ATWCategoryListing(BaseModel):
    """Represent one ATW category row and its snippet members.

    :param category_root: The top-level product/category root.
    :type category_root: str
    :param parent_category: The parent category enum name.
    :type parent_category: str
    :param parent_category_label: The parent category display label.
    :type parent_category_label: str
    :param category: The ATW leaf category enum name.
    :type category: str
    :param category_label: The ATW leaf category display label.
    :type category_label: str
    :param snippet_count: Number of snippets in this category.
    :type snippet_count: int
    :param snippets: Snippet summaries belonging to this category.
    :type snippets: list[ATWSnippetSummary]
    """

    category_root: str
    parent_category: str
    parent_category_label: str
    category: str
    category_label: str
    snippet_count: int
    snippets: list[ATWSnippetSummary]


router = APIRouter()
schema_endpoint(router=router, plugin_schema=atw_schema)


def _build_summary(snippet: Snippet) -> ATWSnippetSummary:
    """Project a snippet onto the ATW summary shape.

    :param snippet: The snippet to project.
    :return: The snippet's identifying name, display title, description, and
        declared elevation requirement.
    """
    return ATWSnippetSummary(
        name=snippet.filename,
        title=snippet.title,
        description=snippet.description,
        sudo=snippet.sudo.requirement,
    )


def _validated_category_tags(
    key: str, raw_categories: object, filename: str
) -> list[str] | None:
    """Validate one category declaration and log why malformed data is ignored.

    :param key: The metadata key being read.
    :param raw_categories: The declared metadata value to validate.
    :param filename: The snippet filename used in warning logs.
    :return: The validated category list, or ``None`` when the declaration is
        malformed and the caller should try another key.
    """
    if not isinstance(raw_categories, list):
        logger.warning(
            ATW_META_WARNING,
            key,
            filename,
            type(raw_categories).__name__,
        )
        return None
    invalid = next(
        (
            type(category).__name__
            for category in raw_categories
            if not isinstance(category, str)
        ),
        None,
    )
    if invalid is None:
        return cast("list[str]", raw_categories)
    logger.warning(
        ATW_META_ELEMENT_WARNING,
        key,
        filename,
        invalid,
    )
    return None


@router.get("/")
async def atw_api_list(session: SessionDep) -> list[ATWCategoryListing]:
    """List ATW-tagged snippets grouped by category.

    Categories with no matching snippets are omitted to keep the payload small;
    the ATW enum still defines the full taxonomy for validation (plugin schema).

    :param session: The database session.
    :return: One listing row per category that has at least one approved snippet.
    """
    snippets = await SnippetManager.list(session, col(Snippet.approved_at).is_not(None))
    snippets_by_cell = defaultdict(list)
    for snippet in snippets:
        root = derive_category_root(snippet.meta.get(META_KEY_SERVICE_TYPE))
        tags: list[str] = []
        for key in (META_KEY_DIAGNOSTIC_CATEGORIES, META_KEY_ATW):
            raw_categories = snippet.meta.get(key, _MISSING_META)
            if raw_categories is _MISSING_META:
                continue
            validated = _validated_category_tags(key, raw_categories, snippet.filename)
            if validated is not None:
                tags = validated
                break
        for tag in dict.fromkeys(tags):
            snippets_by_cell[(root, tag)].append(snippet)

    grouped = []
    for root_label in CATEGORY_ROOT_LABELS.values():
        for category in ATWCategory:
            cell_snippets = snippets_by_cell.get((root_label, category.name), [])
            if not cell_snippets:
                continue
            category_snippets = [_build_summary(snippet) for snippet in cell_snippets]
            grouped.append(
                ATWCategoryListing(
                    category_root=root_label,
                    parent_category=category.parent.name,
                    parent_category_label=category.parent.value,
                    category=category.name,
                    category_label=category.value,
                    snippet_count=len(category_snippets),
                    snippets=category_snippets,
                )
            )

    return grouped


@router.get("/snippets/")
async def atw_snippet_search(
    session: SessionDep,
    list_query: AtwSnippetSearchQueryDep,
    pagination: PaginationDep,
) -> PaginatedResponse[ATWSnippetSummary]:
    """Search approved snippets by free text, independent of the ATW taxonomy.

    Served from ATW's own router over the snippets library, so the capability does
    not depend on the Snippet Manager app being activated. The
    ``diagnostic_categories`` metadata key is a presentation filter on the category
    listing and is deliberately not applied here, so search reaches snippets that
    listing never exposes.

    :param session: The database session.
    :param list_query: The vetted sort and search selections, pinned to approved.
    :param pagination: The offset/limit window for the page.
    :return: A paginated page of approved snippet summaries.
    :raises sqlalchemy.exc.SQLAlchemyError: When the count or data query fails to
        execute.
    """
    page = await SnippetManager.snippet_list_page(
        session, list_query=list_query, pagination=pagination
    )
    items = [_build_summary(snippet) for snippet in page.items]
    return PaginatedResponse.from_pagination(items, page.total, pagination)


def _last_activity_at(
    incident: AtwIncident, last_execution_at: datetime | None
) -> datetime:
    """Resolve an incident's last-activity time from its own timestamps and its runs.

    Computed in Python rather than SQL because SQLAlchemy ships no portable
    construct for a row-wise maximum: PostgreSQL spells it ``GREATEST`` and SQLite
    spells it as a multi-argument ``max()``. Every candidate is normalized first —
    mixing a naive value in raises ``TypeError`` on comparison, and whether the
    aggregate's timestamp arrives timezone-aware is dialect-dependent.

    ``created_at`` is always set, so the result is never ``None`` — an incident
    with no runs reports its own last change and the client needs no empty state.

    Delivery attempts are deliberately not a source: ``atw_send_log`` rows are real
    activity on an incident, but folding a third table into the aggregate is out of
    scope, so a send does not move this timestamp.

    :param incident: The incident whose own timestamps take part.
    :param last_execution_at: The aggregated execution timestamp, when it has runs.
    :return: The latest of the incident's creation, update, and execution times.
    """
    candidates = (incident.created_at, incident.updated_at, last_execution_at)
    return max(
        make_datetime_utc(candidate)
        for candidate in candidates
        if candidate is not None
    )


def _build_incident_responses(
    incidents: Iterable[AtwIncident],
    aggregates: Mapping[UUID4, IncidentRunAggregate],
) -> list[AtwIncidentResponse]:
    """Render incidents with their run totals attached.

    Shared by all six routes that return :class:`AtwIncidentResponse`, so none of
    them serves a defaulted ``0`` for an incident that actually has runs. An
    incident absent from ``aggregates`` has no executions and is zeroed here.

    :param incidents: The incidents to render, in the order to return them.
    :param aggregates: Run totals keyed by incident id, as produced by
        :meth:`AtwIncidentExecutionManager.aggregate_by_incident`.
    :return: One response per incident, in the given order.
    """
    responses: list[AtwIncidentResponse] = []
    for incident in incidents:
        aggregate = aggregates.get(incident.id)
        responses.append(
            AtwIncidentResponse.model_validate(incident).model_copy(
                update={
                    "run_count": 0 if aggregate is None else aggregate.run_count,
                    "failed_run_count": (
                        0 if aggregate is None else aggregate.failed_run_count
                    ),
                    "last_activity_at": _last_activity_at(
                        incident,
                        None if aggregate is None else aggregate.last_execution_at,
                    ),
                }
            )
        )
    return responses


async def _build_incident_response(
    session: SessionDep, incident: AtwIncident
) -> AtwIncidentResponse:
    """Render one incident with its run totals, fetching them for that id alone.

    :param session: The database session.
    :param incident: The incident to render.
    :return: The incident's response, carrying true run totals.
    """
    aggregates = await AtwIncidentExecutionManager.aggregate_by_incident(
        session, [incident.id]
    )
    return _build_incident_responses([incident], aggregates)[0]


@router.post("/incidents/", status_code=status.HTTP_201_CREATED)
async def atw_create_incident(
    session: SessionDep,
    current_user: ApiCurrentUser,
    body: AtwIncidentWrite,
) -> AtwIncidentResponse:
    """Create a diagnostic incident owned by the current user.

    :param session: The database session.
    :param current_user: The authenticated user, stamped as ``created_by``.
    :param body: The incident create payload.
    :return: The created incident.
    """
    incident = AtwIncident(**body.model_dump(), created_by=current_user.username)
    saved = await AtwIncidentManager.save(session, incident)
    return await _build_incident_response(session, saved)


@router.get("/incidents/")
async def atw_list_incidents(
    session: SessionDep, pagination: PaginationDep
) -> PaginatedResponse[AtwIncidentResponse]:
    """List diagnostic incidents, newest first, with each one's run totals.

    The totals come from one grouped query over the whole page, so rendering it
    issues no per-row task-history request however many runs an incident holds.

    :param session: The database session.
    :param pagination: The offset/limit window for the page.
    :return: A paginated page of incidents, newest first.
    """
    page = await AtwIncidentManager.list_paginated(session, pagination=pagination)
    aggregates = await AtwIncidentExecutionManager.aggregate_by_incident(
        session, [incident.id for incident in page.items]
    )
    items = _build_incident_responses(page.items, aggregates)
    return PaginatedResponse.from_pagination(items, page.total, pagination)


@router.get("/incidents/{incident_id}")
async def atw_get_incident(
    session: SessionDep, incident: AtwIncidentDep
) -> AtwIncidentResponse:
    """Retrieve a single diagnostic incident by id.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :return: The matching incident.
    """
    return await _build_incident_response(session, incident)


@router.patch("/incidents/{incident_id}")
async def atw_update_incident(
    session: SessionDep, incident: AtwIncidentDep, body: AtwIncidentUpdate
) -> AtwIncidentResponse:
    """Update a diagnostic incident's name or support-case reference.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param body: The partial update payload; unset fields are untouched.
    :return: The updated incident.
    """
    updated = await AtwIncidentManager.update(session, incident, body)
    return await _build_incident_response(session, updated)


@router.delete(
    "/incidents/{incident_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def atw_delete_incident(session: SessionDep, incident: AtwIncidentDep) -> None:
    """Delete a diagnostic incident and cascade its execution rows.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    """
    await AtwIncidentManager.delete(session, incident)


@router.post("/incidents/{incident_id}/close/")
async def atw_close_incident(
    session: SessionDep, incident: OpenAtwIncidentDep
) -> AtwIncidentResponse:
    """Close a diagnostic incident, stamping the current UTC time.

    :param session: The database session.
    :param incident: The open incident resolved from the ``incident_id`` path parameter.
    :return: The closed incident.
    :raises HTTPConflictException: If the incident is already closed.
    """
    incident.closed_at = utc_now()
    saved = await AtwIncidentManager.save(session, incident)
    return await _build_incident_response(session, saved)


@router.post("/incidents/{incident_id}/reopen/")
async def atw_reopen_incident(
    session: SessionDep, incident: ClosedAtwIncidentDep
) -> AtwIncidentResponse:
    """Reopen a closed diagnostic incident, clearing its close timestamp.

    :param session: The database session.
    :param incident: The closed incident resolved from the ``incident_id`` path parameter.
    :return: The reopened incident.
    :raises HTTPConflictException: If the incident is already open.
    """
    incident.closed_at = None
    saved = await AtwIncidentManager.save(session, incident)
    return await _build_incident_response(session, saved)


@router.get("/execution-schema/")
async def atw_execution_schema(
    snippet_filename: Annotated[
        list[str],
        Query(
            min_length=1,
            max_length=MAX_BATCH_SNIPPETS,
            description="Snippet filenames to build a batch form for.",
        ),
    ],
) -> ATWMergedSchemaResponse:
    """Build one execution form covering several snippets, merging common parameters.

    Unknown or unsafe filenames fail the whole request — a form the caller cannot
    fill for every selected snippet is not a partial success.

    :param snippet_filename: The selected snippet filenames, repeated per snippet
        and deduplicated order-preserving.
    :return: The shared section followed by each snippet's remaining fields.
    :raises HTTPBadRequestException: When a filename is unsafe or malformed, failing
        the whole request before any item is dispatched.
    :raises HTTPNotFoundException: When a filename matches no snippet.
    """
    unique = list(unique_everseen(snippet_filename))
    resolved = await resolve_snippets(unique)
    missing = next((filename for filename in unique if filename not in resolved), None)
    if missing is not None:
        raise HTTPNotFoundException(detail=snippet_not_found_detail(missing))
    scripts = [resolved[filename] for filename in unique]
    fields_per_script = [parameter_fields(script) for script in scripts]

    declarations = defaultdict(list)
    for fields in fields_per_script:
        for field in fields:
            declarations[field.name].append(field)
    shared_names = shared_field_names(declarations)

    shared = batch_execution_fields(scripts)
    shared += [
        fields[0] for name, fields in declarations.items() if name in shared_names
    ]
    return ATWMergedSchemaResponse(
        shared=shared,
        per_snippet=[
            ATWSnippetSchema(
                snippet_filename=script.filename,
                fields=[field for field in fields if field.name not in shared_names],
            )
            for script, fields in zip(scripts, fields_per_script, strict=True)
        ],
    )


def _failure_detail(exc: Exception) -> str | list[dict[str, Any]]:
    """Return the client-facing detail for a failed batch item.

    :param exc: The exception that ended the item.
    :return: The exception's HTTP detail, or its message for a transport error.
    """
    return cast(
        "str | list[dict[str, Any]]",
        exc.detail if isinstance(exc, HTTPException) else str(exc),
    )


@router.post(
    "/incidents/{incident_id}/executions/", status_code=status.HTTP_201_CREATED
)
async def atw_batch_execute(
    session: SessionDep,
    incident: OpenAtwIncidentDep,
    body: ATWBatchExecuteWrite,
    tasks_api: TaskAPI,
) -> ATWBatchExecuteResponse:
    """Execute several snippets against one incident, reporting each item separately.

    A malformed selection fails the whole request up front: ``resolve_snippets``
    runs the traversal guard over every filename before dispatch, so an unsafe
    filename raises before any item runs. Past that guard, one failing item never
    blocks the rest: each is dispatched and recorded inside its own guard, and the
    response always carries an entry per requested item — an unresolved filename
    becomes that item's error. A failed attempt produces no task-history row to
    reference, so it is reported here rather than persisted.

    The row-write guard rolls the shared session back before the loop continues,
    or one failed write would leave the transaction aborted and doom every later
    item on PostgreSQL; the dispatch guard rolls back defensively, having written
    nothing itself. ``incident.id`` is read once up front because both a commit and
    a rollback expire the instance, and re-reading it would trigger a lazy load.

    ATW's proxy is resolved once per distinct interpreter before the loop, so a batch
    of twenty items costs the same upstream traffic as one item. That resolution has
    its own guard: it cannot fail for one item and not another, so a failure degrades
    the whole batch to unwrapped dispatch instead of failing a request whose
    dispatches may still succeed.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param body: The batch payload.
    :param tasks_api: The authenticated Tasks API client.
    :return: One outcome entry per requested item, in request order.
    :raises HTTPBadRequestException: When any filename is unsafe or malformed,
        failing the whole request before any item is dispatched.
    """
    incident_id = incident.id
    resolved = await resolve_snippets([item.snippet_filename for item in body.items])
    try:
        proxies = await resolve_atw_proxy_tasks(
            script.execution_task_name for script in resolved.values()
        )
    except (HTTPException, OSError, RuntimeError):
        # Resolution failure is never item-specific — it affects every item the same
        # way — so it degrades the whole batch to unwrapped dispatch rather than
        # failing a request whose dispatches may well succeed. Outcomes then come
        # from the reconciliation sweep instead of the recorder.
        logger.warning(
            "Could not resolve ATW's proxy tasks; dispatching this batch unwrapped.",
            exc_info=True,
        )
        proxies = {}
    items = []
    for item in body.items:
        script = resolved.get(item.snippet_filename)
        if script is None:
            items.append(
                ATWBatchExecuteItemResponse(
                    snippet_filename=item.snippet_filename,
                    error=snippet_not_found_detail(item.snippet_filename),
                )
            )
            continue
        try:
            dispatched = await dispatch_batch_item(
                body,
                item,
                script,
                tasks_api,
                execution_task_name=proxies.get(script.execution_task_name),
            )
        except (HTTPException, OSError) as exc:
            await session.rollback()
            items.append(
                ATWBatchExecuteItemResponse(
                    snippet_filename=item.snippet_filename,
                    error=_failure_detail(exc),
                )
            )
            continue
        if dispatched.task_id is None:
            items.append(
                ATWBatchExecuteItemResponse(
                    snippet_filename=item.snippet_filename,
                    task_name=dispatched.task_name,
                    error=NO_TASK_ID_ERROR,
                )
            )
            continue
        try:
            await AtwIncidentExecutionManager.save(
                session,
                AtwIncidentExecution(
                    incident_id=incident_id,
                    task_history_id=dispatched.task_id,
                    snippet_filename=dispatched.snippet_filename,
                ),
            )
        except (HTTPException, OSError) as exc:
            await session.rollback()
            items.append(
                ATWBatchExecuteItemResponse(
                    snippet_filename=item.snippet_filename,
                    task_name=dispatched.task_name,
                    task_history_id=dispatched.task_id,
                    error=f"{UNRECORDED_EXECUTION_ERROR}: {_failure_detail(exc)}",
                )
            )
            continue
        items.append(
            ATWBatchExecuteItemResponse(
                snippet_filename=item.snippet_filename,
                task_name=dispatched.task_name,
                task_history_id=dispatched.task_id,
            )
        )
    return ATWBatchExecuteResponse(items=items)


async def _resolve_page_snippets(
    executions: Sequence[AtwIncidentExecution],
) -> dict[str, SnippetScript]:
    """Resolve the page's snippets in one query, degrading a failure to no metadata.

    Mirrors :func:`fetch_task_history`'s policy for the same route: a lookup that
    cannot answer blanks the affected rows' arguments rather than failing the
    listing.

    :param executions: The page's recorded execution rows.
    :return: A mapping of each resolved filename to its snippet; filenames that
        no longer resolve are absent, and an empty mapping means none did.
    """
    try:
        return await resolve_snippets(
            [execution.snippet_filename for execution in executions]
        )
    except (HTTPException, OSError):
        logger.warning(ATW_SNIPPET_RESOLUTION_WARNING, exc_info=True)
        return {}


def _execution_meta(history: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the metadata recorded with an execution, or ``None`` on an odd shape.

    ``history`` is the tasks service's response body, unvalidated on this side, so
    each nesting level is shape-checked rather than assumed: reading ``args`` off a
    truthy non-mapping would raise and fail the whole page rather than one row.

    :param history: The upstream task-history payload.
    :return: The recorded execution metadata, empty when the payload carries none,
        or ``None`` when either nesting level holds something other than a mapping.
    """
    request = history.get("execution_request") or {}
    if not isinstance(request, Mapping):
        return None
    meta = request.get("meta") or {}
    return meta if isinstance(meta, Mapping) else None


def _args_unreadable_upstream(history: Mapping[str, Any]) -> bool:
    """Return whether the tasks service could not read an execution's arguments.

    Read off the documented ``unreadable_request_leaves`` field rather than
    inferred from the value: the service serialises a leaf it could not decrypt
    as ``null``, which is exactly what an execution recording no arguments also
    looks like. The field is shape-checked because ``history`` is unvalidated on
    this side, matching :func:`_execution_meta`.

    :param history: The upstream task-history payload.
    :return: Whether the recorded arguments are unreadable upstream.
    """
    unreadable = history.get("unreadable_request_leaves")
    return isinstance(unreadable, list) and ARGS_LEAF in unreadable


def _execution_args(
    history: dict[str, Any], script: SnippetScript | None
) -> tuple[str | None, bool]:
    """Return an execution's masked argument string and whether it was withheld.

    An empty ``history`` is what an upstream lookup failure degrades to, so it
    withholds rather than reporting the empty state -- the arguments exist, they
    just could not be read. A payload whose nesting is not shaped as expected
    withholds for the same reason (see :func:`_execution_meta`).

    Masking derives what is sensitive from the snippet's *current* frontmatter,
    while the arguments were rendered from whatever it said at execution time. The
    two only coincide while the file is unchanged, so the digest recorded alongside
    the arguments is compared against the snippet's current one first: an edit that
    dropped, renamed, or de-sensitised a parameter would otherwise leave its value
    unrecognised and rendered in the clear. A digest mismatch withholds, which also
    means a snippet edit blanks the arguments of executions recorded before it --
    the price of not persisting the metadata each run was masked against.

    Deriving that metadata can also fail outright on a malformed or stale ``meta``
    column -- an ``arg_format`` that does not tokenise, a parameter definition the
    model rejects, a value that will not serialise. Any of those withholds this row
    alone, never the page. ``ValidationError`` and ``PydanticUserError`` need no
    separate arm: they arrive as the ``ValueError`` and ``TypeError`` they
    respectively subclass.

    A row the tasks service stored encrypted and could not read back also
    withholds. It is recognised from the documented ``unreadable_request_leaves``
    field, never by inspecting the value: the service already serialises such a
    leaf as ``null``, which is otherwise indistinguishable from an execution that
    recorded no arguments.

    :param history: The upstream task-history payload, empty when unavailable.
    :param script: The resolved snippet, or ``None`` when its filename no longer
        resolves and the parameter metadata masking needs is unavailable.
    :return: The masked argument string paired with the withheld flag; a ``None``
        string and a false flag mean the execution recorded no arguments.
    """
    if not history or _args_unreadable_upstream(history):
        return None, True
    if (meta := _execution_meta(history)) is None:
        return None, True
    if not (raw := meta.get("args")):
        return None, False
    if script is None or meta.get("md5_checksum") != script.snippet.md5_digest:
        return None, True
    try:
        masked = mask_snippet_args(raw, script.get_execution_model())
    except (TypeError, ValueError, KeyError, AttributeError):
        logger.warning(ATW_ARG_MASKING_WARNING, script.filename, exc_info=True)
        return None, True
    return masked, masked is None


def _build_execution_response(
    execution: AtwIncidentExecution,
    history: dict[str, Any],
    script: SnippetScript | None,
) -> ATWIncidentExecutionResponse:
    """Merge a recorded execution with live task history and the snippet title.

    :param execution: The locally-recorded execution row.
    :param history: The upstream task-history payload, empty when unavailable.
    :param script: The resolved snippet supplying the title and parameter metadata
        for argument masking, or ``None`` when its filename no longer resolves.
    :return: The combined execution response.
    """
    masked_args, args_withheld = _execution_args(history, script)
    return ATWIncidentExecutionResponse(
        id=execution.id,
        snippet_filename=execution.snippet_filename,
        snippet_title=None if script is None else script.snippet.title,
        task_history_id=execution.task_history_id,
        created_at=execution.created_at,
        task_status=history.get("status"),
        failure_reason=history.get("failure_reason"),
        started_at=history.get("started_at"),
        finished_at=history.get("finished_at"),
        has_logs=history.get("has_logs"),
        masked_args=masked_args,
        args_withheld=args_withheld,
    )


@router.get("/config/")
async def atw_config() -> AtwConfigResponse:
    """Report whether the incident send action is available.

    Not gated by the send guard: this endpoint is what reports that guard, so
    it must answer whether or not a receiver is configured.

    :return: The reasons the send action is withheld, and whether the
        case-reference field may search the receiver.
    """
    return AtwConfigResponse(
        send_disabled_reasons=diagnostics_send_disabled_reasons(),
        case_search_available=diagnostics_case_search_available(),
    )


@router.get("/case-search/", dependencies=[IsApiAdmin])
async def atw_case_search(
    term: Annotated[
        StrippedNonEmptyStr,
        Query(
            max_length=MAX_CASE_SEARCH_TERM_LENGTH,
            description="The support case reference or title fragment to match.",
        ),
    ],
) -> AtwCaseSearchResponse:
    """Search the configured delivery provider for support cases matching ``term``.

    No way the search itself can fail reaches the caller as an error: a
    deployment that declares no case-search section, stored inputs that no
    longer fit the plan, a refused credential, an unreachable receiver, a term
    pattern match that outran its budget and a search that outran its bound all
    report the same unavailability, which the caller renders as the plain text
    field rather than as a search that found nothing.

    The term match runs on the event-loop thread, where this bound cannot
    interrupt it, so the bound is handed to the match as well: time
    spent opening the transport comes out of the match's allowance.

    Restricted to administrators, unlike the app's other reads. The router
    resolves a minimum role for unsafe methods only, so a safe method carries
    whatever guard it declares itself; this one issues the deployment's own
    receiver credential, and the dialog that calls it is already offered to
    administrators alone.

    :param term: The caller's typed search term, the only input it accepts.
        Surrounding whitespace is stripped, so a whitespace-only term is
        refused rather than reaching the receiver as a match-everything
        fragment.
    :return: The matched cases, or that the search could not run. At most
        ``MAX_CASE_SEARCH_MATCHES`` are offered, so a plan that declares no
        provider-side limit still cannot hand the dialog an unbounded list.
    """
    plan = resolve_delivery_plan().plan
    if plan is None or plan.case_search is None:
        return AtwCaseSearchResponse(available=False, matches=[])
    try:
        async with asyncio.timeout(CASE_SEARCH_TIMEOUT_SECONDS) as bound:
            async with get_delivery_executor(plan) as executor:
                matches = await executor.search_cases(term, bound=bound)
    except Exception as error:  # noqa: BLE001 -- degraded, never surfaced to the dialog
        # ``RemoteAPI.request`` maps an upstream error body's ``detail`` onto
        # the exception it raises, so rendering the exception would log a value
        # the receiver supplied.
        logger.warning("Diagnostics case search failed (%s).", type(error).__name__)
        return AtwCaseSearchResponse(available=False, matches=[])
    return AtwCaseSearchResponse(
        available=True,
        matches=[
            AtwCaseMatch(reference=match.reference, title=match.title)
            for match in matches[:MAX_CASE_SEARCH_MATCHES]
        ],
    )


async def _resolve_selected_executions(
    session: SessionDep, incident: AtwIncident, execution_ids: list[UUID4]
) -> list[AtwIncidentExecution]:
    """Resolve the requested execution ids against the incident that owns them.

    :param session: The database session.
    :param incident: The incident the send belongs to.
    :param execution_ids: The requested execution ids, in request order.
    :return: The matching execution rows, in request order.
    :raises HTTPUnprocessableEntityException: When an id names an execution this
        incident does not own.
    """
    rows = await AtwIncidentExecutionManager.list(
        session,
        AtwIncidentExecution.id.in_(execution_ids),
        incident_id=incident.id,
    )
    by_id = {row.id: row for row in rows}
    if unknown := [str(key) for key in execution_ids if key not in by_id]:
        raise HTTPUnprocessableEntityException(
            detail=(
                f"Execution(s) {', '.join(unknown)} do not belong to incident "
                f"{incident.id}."
            )
        )
    return [by_id[key] for key in execution_ids]


@router.post(
    "/incidents/{incident_id}/send-jobs/",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[IsDiagnosticsSendConfigured],
)
async def atw_start_send_job(
    session: SessionDep,
    incident: AtwIncidentDep,
    current_user: ApiCurrentUser,
    body: AtwSendJobWrite,
) -> AtwSendLogResponse:
    """Start delivering the selected executions' output files and logs to the support case.

    The row is created before the task is queued so a broker failure is still
    recorded as a failed attempt rather than vanishing: the row is the only place
    a support engineer can see that a send was ever tried.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param current_user: The authenticated user, stamped as ``requested_by``.
    :param body: The send payload naming the case reference and executions.
    :return: The created send log, pending.
    :raises HTTPUnprocessableEntityException: When an execution id does not belong
        to this incident.
    :raises HTTPServiceUnavailableException: When the send could not be queued.
    :raises HTTPBadRequestException: Propagated from the manager when the row
        cannot be written.
    """
    selected = await _resolve_selected_executions(session, incident, body.execution_ids)
    row = await AtwSendLogManager.save(
        session,
        AtwSendLog(
            incident_id=incident.id,
            case_ref=body.case_ref,
            requested_by=current_user.username,
            detail={
                "executions": [
                    {
                        "id": str(execution.id),
                        "task_history_id": execution.task_history_id,
                        "snippet_filename": execution.snippet_filename,
                    }
                    for execution in selected
                ]
            },
        ),
    )
    try:
        send_incident_diagnostics.delay(str(row.id))
    except (OSError, KombuError) as exc:
        logger.exception("Could not queue diagnostics send %s", row.id)
        row.status = AtwSendStatusEnum.FAILED
        row.finished_at = utc_now()
        row.detail = {**row.detail, "error": f"Could not queue the send: {exc}"}
        await AtwSendLogManager.save(session, row, flag_modified_fields=["detail"])
        raise HTTPServiceUnavailableException(
            detail=f"Could not queue the send: {exc}"
        ) from exc
    return AtwSendLogResponse.model_validate(row)


@router.get("/incidents/{incident_id}/send-jobs/")
async def atw_list_send_jobs(
    session: SessionDep, incident: AtwIncidentDep, pagination: PaginationDep
) -> PaginatedResponse[AtwSendLogResponse]:
    """List one incident's diagnostics send attempts, newest first.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param pagination: The offset/limit window for the page.
    :return: A paginated page of send attempts, newest first.
    """
    page = await AtwSendLogManager.list_paginated(
        session, pagination=pagination, incident_id=incident.id
    )
    items = [AtwSendLogResponse.model_validate(row) for row in page.items]
    return PaginatedResponse.from_pagination(items, page.total, pagination)


@router.get("/incidents/{incident_id}/send-jobs/{send_job_id}")
async def atw_get_send_job(
    session: SessionDep, incident: AtwIncidentDep, send_job_id: UUID4
) -> AtwSendLogResponse:
    """Retrieve one diagnostics send attempt, for the dialog to poll.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param send_job_id: The send attempt's UUID.
    :return: The matching send attempt.
    :raises HTTPNotFoundException: If this incident has no such attempt.
    """
    row = await AtwSendLogManager.get_or_404(
        session, id=send_job_id, incident_id=incident.id
    )
    return AtwSendLogResponse.model_validate(row)


@router.get("/incidents/{incident_id}/executions/")
async def atw_list_incident_executions(
    session: SessionDep,
    incident: AtwIncidentDep,
    pagination: PaginationDep,
    tasks_api: TaskAPI,
) -> PaginatedResponse[ATWIncidentExecutionResponse]:
    """List one incident's snippet executions, newest first, with live task status.

    Each row also reports the command line its snippet ran with, credential values
    masked. A row whose snippet or upstream payload cannot supply what masking
    needs reports its arguments as withheld; the page still returns.

    :param session: The database session.
    :param incident: The incident resolved from the ``incident_id`` path parameter.
    :param pagination: The offset/limit window for the page.
    :param tasks_api: The authenticated Tasks API client.
    :return: A paginated page of executions hydrated from the Tasks API, each with
        its masked arguments or the reason they are absent.
    """
    page = await AtwIncidentExecutionManager.list_paginated(
        session, pagination=pagination, incident_id=incident.id
    )
    histories = await asyncio.gather(
        *(
            fetch_task_history(tasks_api, execution.task_history_id)
            for execution in page.items
        )
    )
    scripts = await _resolve_page_snippets(page.items)
    items = [
        _build_execution_response(
            execution, history, scripts.get(execution.snippet_filename)
        )
        for execution, history in zip(page.items, histories, strict=True)
    ]
    return PaginatedResponse.from_pagination(items, page.total, pagination)
