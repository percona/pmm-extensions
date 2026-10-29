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

"""Define shared route helpers for schema-driven plugin routers.

The helpers in this module register a plugin's well-known routes with a
single call so plugins never re-implement the auth posture, response-model
wiring, or status-code conventions:

- :func:`schema_endpoint` and :func:`capabilities_endpoint` register the
  ``GET /schema`` and ``GET /capabilities`` discovery routes.
- :func:`derive_crud_routes` builds a plugin router carrying the schema
  route plus the standard list / detail / create (and optional update /
  delete) CRUD routes, composing the shared framework task helpers for the
  handler bodies.

Pagination convention: paginate anything backed by an unbounded resource and
return a :class:`~app.core.pagination.PaginatedResponse` envelope; a bare list
is acceptable only for compile-time-bounded enumerations. Hand-written proxy
list routes build the envelope with
:func:`~app.core.pagination.build_proxied_page`, which corrects the ``total``
when rows are filtered in-process.
"""

import functools
import inspect
import logging
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, cast, TypeVar

from fastapi import APIRouter, Depends, params, Query, status
from pydantic import BaseModel

from app.core.db.deps import make_in_memory_list_query_dep, make_list_query_dep
from app.core.db.in_memory_list_query import InMemoryListQueryApplier
from app.core.db.list_query import ListQuerySpec
from app.core.pagination import PaginatedResponse, Pagination, PaginationDependency
from app.core.requests.remote_api import as_json_object, RemoteAPI
from app.core.utils.fields import ArbitraryMapping
from app.extensions.apps.framework.connectivity import (
    CONNECTIVITY_WARNING_FIELD,
    maybe_record_connectivity_warning,
)
from app.extensions.apps.framework.responses import (
    build_task_list_responses,
    derive_create_response_model,
    dump_with_excluded_fields,
    TaskExecuteWrite,
    TaskExecutionResponse,
    TaskResponseBuilder,
)
from app.extensions.apps.framework.schema import AppSchema
from app.extensions.apps.framework.script_helpers import execute_script
from app.extensions.apps.framework.script_source import (
    make_script_dep,
    ScriptExecuteWrite,
    ScriptExecutionResponse,
    ScriptSource,
)
from app.extensions.apps.framework.spec import stamp_form_input
from app.extensions.apps.framework.task_status import get_task_latest_history
from app.extensions.deps import (
    HasNoConflictedRunningTasks,
    IsApiAuthenticated,
    require_one_path_segment,
    task_path,
    TaskAPI,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import (
    Task,
    TaskHistoryResponse,
    TaskHistoryStatusEnum,
    TaskWrite,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ListFilters",
    "capabilities_endpoint",
    "derive_crud_routes",
    "derive_execute_route",
    "derive_script_routes",
    "make_list_filter_dep",
    "resolve_response_model",
    "schema_endpoint",
]


CapabilitiesT = TypeVar("CapabilitiesT", bound=BaseModel)
ListDetailResponseT = TypeVar("ListDetailResponseT", bound=BaseModel)
CreateResponseT = TypeVar("CreateResponseT", bound=BaseModel)


def schema_endpoint(router: APIRouter, plugin_schema: AppSchema) -> None:
    """Register a ``GET /schema`` route on ``router`` that returns ``plugin_schema``.

    Call this once from the module that owns the plugin's ``APIRouter``
    (typically under the shared ``apps_router`` tree in
    ``app/extensions/api/router.py``); the route then resolves at
    ``/api/apps/{plugin}/schema`` once the plugin router is included with
    the matching prefix. The helper itself does not set a prefix.

    .. code-block:: python

        from fastapi import APIRouter

        from app.extensions.apps.framework.api import schema_endpoint

        router = APIRouter()
        schema_endpoint(router, plugin_schema)

    The route pins ``response_model_by_alias=True`` so the
    ``field_type → "type"`` discriminator alias on each field serialises
    to the wire key the FE renderer expects (no top-level alias generator
    exists post-snake_case flip). ``response_model_exclude_none=True``
    keeps the payload free of the new conditional-rule primitive keys
    (``requires``, ``forbidden``, ``cardinality_rules``, ``fail_when``)
    for schemas that don't opt into them. ``IsApiAuthenticated`` is
    redeclared at the route level even though ``api_router`` applies the
    same dependency at router level; the duplicate declaration guarantees
    a JSON 401 even if a plugin mounts the router outside that tree, and
    FastAPI's dependency cache deduplicates it per request.

    :param router: The plugin's ``APIRouter``.
    :param plugin_schema: The plugin's fully-validated schema instance.
    :raises ValueError: If ``router`` already exposes a ``GET /schema`` route.
    """
    router_prefix = getattr(router, "prefix", "") or ""
    expected_path = f"{router_prefix}/schema"
    for existing in router.routes:
        existing_methods = set(getattr(existing, "methods", None) or ())
        if (
            getattr(existing, "path", None) == expected_path
            and "GET" in existing_methods
        ):
            raise ValueError(
                "schema_endpoint: router already has a GET /schema route; "
                "call this helper at most once per plugin router"
            )

    @router.get(
        "/schema",
        response_model_by_alias=True,
        response_model_exclude_none=True,
        dependencies=[IsApiAuthenticated],
    )
    async def get_schema() -> AppSchema:
        """Return the plugin schema captured at registration time.

        :return: The plugin schema instance.
        """
        return plugin_schema


def resolve_response_model(
    provider: Callable[..., BaseModel],
    *,
    helper: str,
    param: str,
) -> type[BaseModel]:
    """Return the ``BaseModel`` subclass declared as ``provider``'s return type.

    Unwraps a :class:`functools.partial` to its underlying function first, since
    :func:`typing.get_type_hints` rejects a partial — binding a builder with
    ``partial(...)`` is an established plugin pattern and resolves the same return
    annotation as the wrapped function. Then uses :func:`typing.get_type_hints` so
    deferred-evaluation annotations (``from __future__ import annotations``) resolve
    to the real class rather than a string. Falls back to the function's
    ``__annotations__`` dict only when :func:`typing.get_type_hints` raises
    :class:`NameError` — i.e. a forward-ref that resolves against a module the
    caller hasn't imported. Other failures propagate unchanged.

    :param provider: A callable annotated with its return type. The
        callable may declare arbitrary parameters (typically resolved by
        FastAPI's dependency injection via ``Depends(...)``), and may be a
        :class:`functools.partial` over such a callable.
    :param helper: The public helper name, used to frame error messages.
    :param param: The offending parameter name, used to frame error messages.
    :return: The class declared as the provider's return annotation.
    :raises TypeError: If the annotation is missing, isn't a class, or
        isn't a :class:`pydantic.BaseModel` subclass.
    """
    while isinstance(provider, functools.partial):
        provider = provider.func
    try:
        hints = typing.get_type_hints(provider)
    except NameError:
        # Forward-ref under ``from __future__ import annotations`` that resolves
        # against a module the caller hasn't imported — fall back to the raw
        # string annotation so the downstream isclass/issubclass check produces
        # the expected TypeError instead of leaking ``NameError`` here.
        hints = getattr(provider, "__annotations__", {})

    annotation = hints.get("return")
    if annotation is None:
        raise TypeError(
            f"{helper}: {param} must declare a return type annotation that is "
            f"a pydantic.BaseModel subclass (e.g. `def {param}() -> MyModel: ...`)"
        )
    if not inspect.isclass(annotation) or not issubclass(annotation, BaseModel):
        raise TypeError(
            f"{helper}: {param}'s return type annotation must be a "
            f"pydantic.BaseModel subclass; got {annotation!r}"
        )
    return annotation


def _reject_async_builders(**builders: Callable[..., BaseModel] | None) -> None:
    """Raise ``TypeError`` if any named builder is an ``async def`` callable.

    :func:`derive_crud_routes`'s handlers invoke the response builders
    synchronously, so an ``async def`` builder would yield an un-awaited
    coroutine that ``response_model`` serialisation cannot handle. Reject it at
    registration — mirroring :func:`capabilities_endpoint`'s sync-only guard —
    rather than letting it surface as an opaque failure on the first request.

    :param builders: Builder callables keyed by their parameter name; ``None``
        entries (an omitted optional builder) are skipped.
    :raises TypeError: If any supplied builder is a coroutine function.
    """
    for label, builder in builders.items():
        if builder is not None and inspect.iscoroutinefunction(builder):
            raise TypeError(
                f"derive_crud_routes: {label} must be a sync callable; the "
                "derived handlers invoke it synchronously, so an async builder "
                "would yield an un-awaited coroutine that response_model "
                "serialisation cannot handle."
            )


def _reject_contextless_builders(
    context_provider: Callable[[], Awaitable[Any]] | None,
    **builders: Callable[..., BaseModel] | None,
) -> None:
    """Raise ``TypeError`` if a context provider cannot bind into a named builder.

    When a ``context_provider`` is set, :func:`_bind_context` binds its result as
    each active builder's ``context`` keyword argument; a builder declaring neither
    a ``context`` parameter nor ``**kwargs`` would raise an opaque ``TypeError`` on
    the first request. Reject it at registration instead — mirroring
    :func:`_reject_async_builders`.

    :param context_provider: The configured context provider; ``None`` skips the
        whole check (no binding happens).
    :param builders: Builder callables keyed by their parameter name; ``None``
        entries (an omitted optional builder) are skipped.
    :raises TypeError: If a supplied builder cannot accept a ``context`` keyword.
    """
    if context_provider is None:
        return
    for label, builder in builders.items():
        if builder is None:
            continue
        parameters = inspect.signature(builder).parameters.values()
        accepts_context = any(
            parameter.name == "context"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_context:
            raise TypeError(
                f"derive_crud_routes: {label} must accept a 'context' keyword "
                "argument when context_provider is set; the provider's result is "
                "bound into the builder via functools.partial(context=...)."
            )


def _create_response_name(plugin_schema: AppSchema) -> str:
    """Return the ``<App>CreateResponse`` OpenAPI component name for the schema.

    Treat both ``-`` and ``_`` in ``plugin_schema.name`` as word boundaries
    (``AppSchema.name`` permits hyphens), capitalise each word, and append
    ``CreateResponse`` — so ``"mysql_backups"`` yields
    ``MysqlBackupsCreateResponse``.

    :param plugin_schema: The plugin schema whose ``name`` seeds the title.
    :return: The derived ``<App>CreateResponse`` component name.
    """
    parts = plugin_schema.name.replace("-", "_").split("_")
    return "".join(part.capitalize() for part in parts) + "CreateResponse"


def capabilities_endpoint(
    router: APIRouter,
    capabilities_provider: Callable[..., CapabilitiesT],
) -> None:
    """Register a ``GET /capabilities`` route returning ``capabilities_provider()``.

    Mirrors :func:`schema_endpoint` for the per-deployment runtime
    capability surface: same auth posture (``IsApiAuthenticated``
    redeclared at the route level), same duplicate-registration guard,
    same ``response_model_exclude_none=True`` posture. Inferring
    ``response_model`` from the provider's return annotation keeps the
    call site terse — plugins write a one-line registration adjacent to
    their ``schema_endpoint(...)`` call.

    .. code-block:: python

        from app.extensions.apps.framework.api import capabilities_endpoint
        from app.extensions.snippets.config import snippets_settings
        from app.extensions.snippets.models.responses import SnippetsCapabilitiesResponse

        def _snippets_capabilities() -> SnippetsCapabilitiesResponse:
            return SnippetsCapabilitiesResponse(
                manual_sync_enabled=snippets_settings.ENABLE_MANUAL_SYNC,
            )

        capabilities_endpoint(router, capabilities_provider=_snippets_capabilities)

    Semantics:

    - The provider is invoked **per request**, never cached, so a
      deployment-config hot reload between two calls is reflected on the
      next response without a process restart.
    - ``IsApiAuthenticated`` only — there is no admin gate. Capability
      flags are public-to-authed-users by design; if a plugin needs to
      gate privileged flags, expose them on a separate route, not here.
    - The provider must be a function (or method) with a return-type
      annotation that resolves to a :class:`pydantic.BaseModel` subclass.
      Lambdas are rejected because their syntax provides no way to declare
      a return-type annotation.
    - The provider may declare arbitrary parameters; ``functools.wraps``
      preserves the signature so FastAPI inspects it and resolves any
      ``Depends(...)`` defaults per request (settings, session, current
      user, etc.). A zero-arg provider remains the most common shape
      (settings attribute read), but the helper does not enforce it.
    - The provider should be cheap. DB or network calls do not belong
      behind a synchronous provider; add an async-provider variant when
      a plugin needs that. ``async def`` providers are rejected at
      registration to keep the contract sync-only for now.
    - The new endpoint is intentionally separate from
      :class:`~app.extensions.apps.framework.schema.AppSchema.capabilities`,
      which describes static UI feature flags (chaining, scheduling,
      alert_on_fail). The two surfaces are not unified: schema-side
      ``Capabilities`` is per-plugin compile-time posture, capability
      responses returned here are per-deployment runtime posture that
      may toggle without a redeploy.

    :param router: The plugin's ``APIRouter``.
    :param capabilities_provider: A callable returning a
        :class:`pydantic.BaseModel` instance. The return-type annotation
        is used as the route's ``response_model``. The callable's
        parameters (if any) are passed through to FastAPI for dependency
        resolution.
    :raises TypeError: If ``capabilities_provider``'s return annotation
        is missing, is not a class, or is not a
        :class:`pydantic.BaseModel` subclass. Raised at registration
        time, not first request.
    :raises ValueError: If ``router`` already exposes a
        ``GET /capabilities`` route.
    """
    if inspect.iscoroutinefunction(capabilities_provider):
        raise TypeError(
            "capabilities_endpoint: capabilities_provider must be a sync "
            "callable; async providers are out of scope (see SEP-1133). "
            "Calling an async function from the sync handler would return a "
            "coroutine object that response_model serialisation cannot handle."
        )
    response_model = resolve_response_model(
        capabilities_provider,
        helper="capabilities_endpoint",
        param="capabilities_provider",
    )

    router_prefix = getattr(router, "prefix", "") or ""
    expected_path = f"{router_prefix}/capabilities"
    for existing in router.routes:
        existing_methods = set(getattr(existing, "methods", None) or ())
        if (
            getattr(existing, "path", None) == expected_path
            and "GET" in existing_methods
        ):
            raise ValueError(
                "capabilities_endpoint: router already has a GET /capabilities "
                "route; call this helper at most once per plugin router"
            )

    @functools.wraps(capabilities_provider)
    def get_capabilities(*args: object, **kwargs: object) -> BaseModel:
        return capabilities_provider(*args, **kwargs)

    router.add_api_route(
        "/capabilities",
        get_capabilities,
        methods=["GET"],
        response_model=response_model,
        response_model_exclude_none=True,
        dependencies=[IsApiAuthenticated],
    )


@dataclass(frozen=True, slots=True)
class ListFilters:
    """Carry the optional list-route filter selections in canonical order.

    :param status: The latest-history status filter, populated when the ``status``
        query parameter is declared. Defaults to ``None``.
    :param service_type: The service-type filter, populated when the
        ``service_type`` query parameter is declared. Defaults to ``None``.
    """

    status: TaskHistoryStatusEnum | None = None
    service_type: ServiceTypeEnum | None = None


def make_list_filter_dep(
    *, status: bool, service_type: bool
) -> Callable[..., ListFilters]:
    """Return a dependency declaring exactly the requested list-filter query params.

    The returned callable declares plain ``service_type`` / ``status`` query
    parameters — in that canonical order, and identical in name, type, and default
    to the hand-written task-plugin list routes — so the derived route's OpenAPI
    parameters match a route that declares them directly. Only the requested params
    are declared; an unrequested filter contributes no query parameter at all.

    :param status: Whether to declare the ``status`` query parameter.
    :param service_type: Whether to declare the ``service_type`` query parameter.
    :return: A dependency returning a :class:`ListFilters` over the declared params.
    """
    if service_type and status:

        def _list_filters(
            service_type: ServiceTypeEnum | None = None,
            status: TaskHistoryStatusEnum | None = None,
        ) -> ListFilters:
            return ListFilters(status=status, service_type=service_type)

    elif status:

        def _list_filters(
            status: TaskHistoryStatusEnum | None = None,
        ) -> ListFilters:
            return ListFilters(status=status)

    elif service_type:

        def _list_filters(
            service_type: ServiceTypeEnum | None = None,
        ) -> ListFilters:
            return ListFilters(service_type=service_type)

    else:

        def _list_filters() -> ListFilters:
            return ListFilters()

    return _list_filters


async def _bind_context(
    builder: Callable[..., BaseModel],
    context_provider: Callable[[], Awaitable[Any]] | None,
) -> Callable[..., BaseModel]:
    """Await the context provider once and bind its result onto ``builder``.

    Mirrors :func:`build_task_list_responses`'s once-per-request binding for the
    single-shot detail and create handlers: when ``context_provider`` is set it is
    awaited once and its result is bound as the builder's ``context`` keyword
    argument via :func:`functools.partial`, so a sync builder receives async
    side-data without becoming async. When ``None`` the builder is returned
    unchanged.

    :param builder: The sync response builder to bind the context onto.
    :param context_provider: The zero-arg async provider, or ``None`` to no-op.
    :return: The original builder, or a partial binding ``context`` into it.
    """
    if context_provider is None:
        return builder
    context = await context_provider()
    return functools.partial(builder, context=context)


def _resolve_create_response_model(
    create_response_builder: TaskResponseBuilder[Any] | None,
    base_model: type[BaseModel],
    *,
    connectivity_check: bool,
    plugin_schema: AppSchema,
) -> type[BaseModel]:
    """Resolve the response model shared by the create and derived-update routes.

    Prefer an explicit ``create_response_builder``'s inferred model (rejecting it
    when ``connectivity_check`` is on but the model omits ``connectivity_warning``,
    so the probe result has somewhere to land); else auto-derive a create model
    wrapping ``base_model`` with ``connectivity_warning`` when the probe is on;
    else fall back to ``base_model`` unchanged. Sharing this between create and
    the derived update keeps both routes' response model byte-identical.

    :param create_response_builder: The explicit create builder, or ``None``.
    :param base_model: The list/detail model used as the auto-derive base.
    :param connectivity_check: Whether the probe (and ``connectivity_warning``)
        is in play.
    :param plugin_schema: The schema seeding the auto-derived model name.
    :return: The resolved create/update response model.
    :raises TypeError: When ``connectivity_check`` is on and an explicit
        ``create_response_builder``'s model omits a ``connectivity_warning`` field.
    """
    if create_response_builder is not None:
        model = resolve_response_model(
            create_response_builder,
            helper="derive_crud_routes",
            param="create_response_builder",
        )
        if connectivity_check and CONNECTIVITY_WARNING_FIELD not in model.model_fields:
            raise TypeError(
                "derive_crud_routes: create_response_builder's model must declare a "
                "connectivity_warning field when connectivity_check=True; build it "
                "via derive_create_response_model so the probe result is attached "
                "rather than silently dropped."
            )
        return model
    if connectivity_check:
        return derive_create_response_model(
            base_model, name=_create_response_name(plugin_schema)
        )
    return base_model


def _register_create_route(
    router: APIRouter,
    *,
    base_builder: TaskResponseBuilder[ListDetailResponseT],
    create_payload: Callable[..., Awaitable[TaskWrite]],
    create_response_builder: TaskResponseBuilder[CreateResponseT] | None,
    create_response_model: type[BaseModel],
    connectivity_check: bool,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
    extra_deps: Sequence[params.Depends] = (),
) -> None:
    """Register the standard ``POST /`` create route (``201``) on ``router``.

    With ``connectivity_check`` off, the handler builds the create response
    directly. With it on, the handler gains a ``check_connectivity`` query
    parameter, runs the post-creation connectivity probe, and attaches the
    resulting ``connectivity_warning`` to the shared ``create_response_model``
    the caller already resolved (see :func:`_resolve_create_response_model`).

    :param router: The plugin router to register the create route on.
    :param base_builder: The fallback create builder when no explicit create builder
        is given — the detail builder when the app overrides detail, else the list
        builder — so a created resource is rendered like its detail view.
    :param create_payload: The create-payload dependency declaring the body.
    :param create_response_builder: The explicit create builder the handler renders
        through, or ``None`` to render through ``base_builder``.
    :param create_response_model: The response model the create and derived-update
        routes share, resolved once by the caller so both routes render (and
        register the OpenAPI component for) a single class rather than each
        re-deriving it.
    :param connectivity_check: Whether to add the connectivity probe and the
        ``check_connectivity`` query parameter.
    :param context_provider: A zero-arg async provider whose once-awaited result
        is bound as the active builder's ``context`` keyword argument before the
        single create build. ``None`` (the default) leaves the builder unbound.
    :param extra_deps: Extra route dependencies appended after
        ``IsApiAuthenticated``, never replacing it.
    """

    async def _build_create_response(
        tasks_api: TaskAPI,
        task_write: TaskWrite,
        *,
        check_connectivity: bool | None,
    ) -> BaseModel:
        """Build the create response, optionally attaching a connectivity warning.

        The name arrives in the request body, where nothing narrows it to one
        path segment, but every route that later addresses the task composes the
        name into an upstream path. Refusing it before the POST keeps a task that
        no route could reach — not even delete — from being created at all.

        :param tasks_api: The upstream task API client.
        :param task_write: The validated create payload.
        :param check_connectivity: Whether to run the connectivity probe.
            ``None`` when the probe is disabled for the route.
        :return: The rendered create response.
        :raises HTTPUnprocessableEntityException: If the task name is not a single
            plain URL path segment. Raised before the POST, so nothing is created.
        """
        require_one_path_segment(task_write.name)
        created = await tasks_api.post("/", json=task_write.model_dump())
        task = Task.model_validate(created)
        warning = (
            await maybe_record_connectivity_warning(
                tasks_api,
                task.data.get("meta", {}),
                check_connectivity=check_connectivity,
            )
            if check_connectivity is not None
            else None
        )
        if create_response_builder is not None:
            builder = await _bind_context(create_response_builder, context_provider)
            result = builder(task)
            if warning is not None:
                return result.model_copy(update={"connectivity_warning": warning})
            return result
        builder = await _bind_context(base_builder, context_provider)
        base = builder(task, status=None)
        if warning is not None:
            return create_response_model(
                **{**dump_with_excluded_fields(base), "connectivity_warning": warning}
            )
        return base

    if not connectivity_check:

        async def _create(
            tasks_api: TaskAPI,
            task_write: Annotated[TaskWrite, Depends(create_payload)],
        ) -> BaseModel:
            return await _build_create_response(
                tasks_api, task_write, check_connectivity=None
            )
    else:

        async def _create(
            tasks_api: TaskAPI,
            task_write: Annotated[TaskWrite, Depends(create_payload)],
            *,
            check_connectivity: Annotated[bool, Query()] = True,
        ) -> BaseModel:
            return await _build_create_response(
                tasks_api, task_write, check_connectivity=check_connectivity
            )

    router.add_api_route(
        "/",
        _create,
        methods=["POST"],
        summary="Create",
        status_code=status.HTTP_201_CREATED,
        response_model=create_response_model,
        response_model_by_alias=True,
        dependencies=[IsApiAuthenticated, *extra_deps],
    )


def _register_update_route(
    router: APIRouter,
    *,
    get_task: Callable[..., Awaitable[Task]],
    base_builder: TaskResponseBuilder[ListDetailResponseT],
    create_payload: Callable[..., Awaitable[TaskWrite]],
    create_response_builder: TaskResponseBuilder[CreateResponseT] | None,
    create_response_model: type[BaseModel],
    connectivity_check: bool,
    detail_path: str,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
    extra_deps: Sequence[params.Depends] = (),
) -> None:
    """Register the derived ``PUT /{detail}`` update route, mirroring create.

    The handler fetches the task by name (404 + any ``extra_deps`` guard target),
    rebuilds the ``TaskWrite`` from the request body through the same
    ``create_payload`` dependency the create route uses, PUTs it upstream, and
    renders the updated task through the create-path response surface — except it
    threads the task's latest status (an update reflects prior execution history,
    where a fresh create has none). With ``connectivity_check`` on it gains the
    ``check_connectivity`` query parameter and attaches the probe warning, exactly
    as create does.

    :param router: The plugin router to register the update route on.
    :param get_task: The task-by-name dependency (resolves 404 and owns the path
        parameter); its inner parameter must equal the detail path parameter.
    :param base_builder: The fallback builder when no explicit create builder is
        given — the detail builder when the app overrides detail, else the list
        builder.
    :param create_payload: The create-payload dependency declaring the body.
    :param create_response_builder: The explicit create builder the handler renders
        through, or ``None`` to render through ``base_builder``.
    :param create_response_model: The response model shared with the create route,
        resolved once by the caller so both routes render (and register the OpenAPI
        component for) a single class rather than each re-deriving it.
    :param connectivity_check: Whether to add the connectivity probe and the
        ``check_connectivity`` query parameter.
    :param detail_path: The ``/{detail}`` route template the PUT mounts on.
    :param context_provider: A zero-arg async provider whose once-awaited result
        is bound as the active builder's ``context`` keyword. ``None`` leaves the
        builder unbound.
    :param extra_deps: Route dependencies (guards) appended after
        ``IsApiAuthenticated``, never replacing it; the caller may resolve these to
        a default guard set rather than only per-route extras.
    """

    async def _build_update_response(
        tasks_api: TaskAPI,
        task: Task,
        task_write: TaskWrite,
        *,
        check_connectivity: bool | None,
    ) -> BaseModel:
        """Build the update response, optionally attaching a connectivity warning.

        The URL addresses the task by its stored name, so the guard on the
        outbound path never sees a rename — that travels in the body, and the
        upstream update applies it. Refusing an unaddressable new name here
        keeps the task from being renamed out of reach of every later route.

        :param tasks_api: The upstream task API client.
        :param task: The resolved task (fetched by name via ``get_task``).
        :param task_write: The validated update payload.
        :param check_connectivity: Whether to run the connectivity probe.
            ``None`` when the probe is disabled for the route.
        :return: The rendered update response.
        :raises HTTPUnprocessableEntityException: If the payload's task name is
            not a single plain URL path segment. Raised before the PUT, so the
            task keeps its current name.
        """
        require_one_path_segment(task_write.name)
        updated = await tasks_api.put(
            task_path(task.name), json=task_write.model_dump()
        )
        updated_task = Task.model_validate(updated)
        latest = await get_task_latest_history(tasks_api, updated_task.name)
        warning = (
            await maybe_record_connectivity_warning(
                tasks_api,
                updated_task.data.get("meta", {}),
                check_connectivity=check_connectivity,
            )
            if check_connectivity is not None
            else None
        )
        if create_response_builder is not None:
            builder = await _bind_context(create_response_builder, context_provider)
            result = builder(
                updated_task,
                status=latest.status,
                last_executed_at=latest.finished_at,
            )
            if warning is not None:
                return result.model_copy(update={"connectivity_warning": warning})
            return result
        builder = await _bind_context(base_builder, context_provider)
        base = builder(
            updated_task, status=latest.status, last_executed_at=latest.finished_at
        )
        if warning is not None:
            return create_response_model(
                **{**dump_with_excluded_fields(base), "connectivity_warning": warning}
            )
        return base

    if not connectivity_check:

        async def _update(
            tasks_api: TaskAPI,
            task: Annotated[Task, Depends(get_task)],
            task_write: Annotated[TaskWrite, Depends(create_payload)],
        ) -> BaseModel:
            return await _build_update_response(
                tasks_api, task, task_write, check_connectivity=None
            )
    else:

        async def _update(
            tasks_api: TaskAPI,
            task: Annotated[Task, Depends(get_task)],
            task_write: Annotated[TaskWrite, Depends(create_payload)],
            *,
            check_connectivity: Annotated[bool, Query()] = True,
        ) -> BaseModel:
            return await _build_update_response(
                tasks_api, task, task_write, check_connectivity=check_connectivity
            )

    router.add_api_route(
        detail_path,
        _update,
        methods=["PUT"],
        summary="Update",
        response_model=create_response_model,
        response_model_by_alias=True,
        dependencies=[IsApiAuthenticated, *extra_deps],
    )


def _register_delete_route(
    router: APIRouter,
    *,
    get_task: Callable[..., Awaitable[Task]],
    detail_path: str,
    extra_deps: Sequence[params.Depends] = (),
) -> None:
    """Register the derived ``DELETE /{detail}`` route (``204``) on ``router``.

    The handler resolves the task by name (404 on unknown / wrong owner) and
    deletes it upstream — the plain cascade-free delete shared by the standard
    task plugins. A plugin needing cascade semantics supplies a full
    ``delete_handler`` override instead; a plugin needing only a guard (for
    example a running-task conflict check) passes it via ``extra_deps``.

    :param router: The plugin router to register the delete route on.
    :param get_task: The task-by-name dependency owning the path parameter.
    :param detail_path: The ``/{detail}`` route template the DELETE mounts on.
    :param extra_deps: Route dependencies (guards) appended after the auth guard,
        never replacing it; the caller may resolve these to a default guard set
        rather than only per-route extras.
    """

    async def _delete(
        tasks_api: TaskAPI, task: Annotated[Task, Depends(get_task)]
    ) -> None:
        await tasks_api.delete(task_path(task.name))

    router.add_api_route(
        detail_path,
        _delete,
        methods=["DELETE"],
        summary="Delete",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model_by_alias=True,
        dependencies=[IsApiAuthenticated, *extra_deps],
    )


def _register_mutation_routes(
    router: APIRouter,
    *,
    detail_path: str,
    get_task: Callable[..., Awaitable[Task]],
    detail_builder: TaskResponseBuilder[Any],
    create_payload: Callable[..., Awaitable[TaskWrite]] | None,
    create_response_builder: TaskResponseBuilder[Any] | None,
    create_response_model: type[BaseModel] | None,
    connectivity_check: bool,
    context_provider: Callable[[], Awaitable[Any]] | None,
    update_enabled: bool,
    update_handler: Callable[..., Awaitable[Any]] | None,
    update_extra_deps: Sequence[params.Depends],
    delete_enabled: bool,
    delete_handler: Callable[..., Awaitable[Any]] | None,
    delete_extra_deps: Sequence[params.Depends],
) -> None:
    """Register the ``PUT`` / ``DELETE`` routes, derived or handler-overridden.

    A supplied handler always wins (the cascade escape hatch); otherwise the
    capability flag drives the standard derived default — the create-mirroring PUT
    (guarded by ``update_extra_deps``) and the plain fetch-then-delete DELETE
    (guarded by ``delete_extra_deps``).

    :param router: The plugin router to register the mutation routes on.
    :param detail_path: The ``/{detail}`` route template both verbs mount on.
    :param get_task: The task-by-name dependency owning the path parameter.
    :param detail_builder: The detail/create fallback builder for the derived PUT.
    :param create_payload: The create-payload dependency the derived PUT rebuilds
        the body through; ``None`` when create is disabled.
    :param create_response_builder: The explicit create builder reused by the PUT.
    :param create_response_model: The response model shared with the create route
        (resolved once by the caller); ``None`` when create is disabled.
    :param connectivity_check: Whether the derived PUT runs the connectivity probe.
    :param context_provider: The once-per-request async context provider, or ``None``.
    :param update_enabled: Whether to derive the default PUT when no handler is set.
    :param update_handler: A full PUT override, or ``None`` for the derived default.
    :param update_extra_deps: Guards appended to the derived PUT after the auth
        guard; the caller may resolve these to a default guard set.
    :param delete_enabled: Whether to derive the default DELETE when no handler is set.
    :param delete_handler: A full DELETE override, or ``None`` for the derived default.
    :param delete_extra_deps: Guards appended to the derived DELETE after the auth
        guard; the caller may resolve these to a default guard set.
    :raises ValueError: If ``update_extra_deps`` / ``delete_extra_deps`` are supplied
        alongside a full ``update_handler`` / ``delete_handler`` or without the matching
        capability; or if the derived PUT is enabled without a ``create_payload`` to
        rebuild the body.
    """
    if delete_extra_deps and delete_handler is not None:
        raise ValueError(
            "derive_crud_routes: delete_extra_deps attach to the derived DELETE; a "
            "full delete_handler must declare its own signature dependencies instead — "
            "drop delete_extra_deps or the delete_handler"
        )
    if delete_extra_deps and not delete_enabled:
        raise ValueError(
            "derive_crud_routes: delete_extra_deps need a derived DELETE to attach to; "
            "enable the delete capability or drop delete_extra_deps"
        )
    if update_extra_deps and update_handler is not None:
        raise ValueError(
            "derive_crud_routes: update_extra_deps attach to the derived PUT; a full "
            "update_handler must declare its own signature dependencies instead — "
            "drop update_extra_deps or the update_handler"
        )
    if update_extra_deps and not update_enabled:
        raise ValueError(
            "derive_crud_routes: update_extra_deps need a derived PUT to attach to; "
            "enable the update capability or drop update_extra_deps"
        )

    if update_handler is not None:
        router.add_api_route(
            detail_path,
            update_handler,
            methods=["PUT"],
            response_model_by_alias=True,
            dependencies=[IsApiAuthenticated],
        )
    elif update_enabled:
        if create_payload is None:
            raise ValueError(
                "derive_crud_routes: the derived PUT rebuilds the body through the "
                "create payload; pass create_payload (enable create) or supply a full "
                "update_handler"
            )
        _register_update_route(
            router,
            get_task=get_task,
            base_builder=detail_builder,
            create_payload=create_payload,
            create_response_builder=create_response_builder,
            create_response_model=create_response_model,
            connectivity_check=connectivity_check,
            detail_path=detail_path,
            context_provider=context_provider,
            extra_deps=update_extra_deps,
        )

    if delete_handler is not None:
        router.add_api_route(
            detail_path,
            delete_handler,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
            response_model_by_alias=True,
            dependencies=[IsApiAuthenticated],
        )
    elif delete_enabled:
        _register_delete_route(
            router,
            get_task=get_task,
            detail_path=detail_path,
            extra_deps=delete_extra_deps,
        )


def _resolve_detail_target(
    response_builder: TaskResponseBuilder[Any],
    detail_response_builder: TaskResponseBuilder[Any] | None,
    detail_response_model: type[BaseModel] | None,
    list_detail_model: type[BaseModel],
) -> tuple[TaskResponseBuilder[Any], type[BaseModel]]:
    """Return the ``(builder, model)`` the detail route and create fallback use.

    The detail builder is ``detail_response_builder`` when set, else
    ``response_builder``. The detail model is the explicit ``detail_response_model``
    when set (bypassing return-annotation inference for an exotic builder), else the
    builder's inferred model, else the shared list model — so a plugin whose detail
    response is richer than its list response derives a distinct detail (and create)
    model, while a plugin whose detail matches its list stays on the list model.

    :param response_builder: The list builder, reused for detail when no override.
    :param detail_response_builder: The detail builder override, or ``None``.
    :param detail_response_model: The explicit detail response model, or ``None``.
    :param list_detail_model: The shared list/detail model used as the fallback.
    :return: The ``(builder, model)`` pair for the detail route and create fallback.
    """
    builder = detail_response_builder or response_builder
    if detail_response_model is not None:
        model = detail_response_model
    elif detail_response_builder is not None:
        model = resolve_response_model(
            detail_response_builder,
            helper="derive_crud_routes",
            param="detail_response_builder",
        )
    else:
        model = list_detail_model
    return builder, model


def _register_list_route(
    router: APIRouter,
    *,
    task_owner: str,
    response_builder: TaskResponseBuilder[ListDetailResponseT],
    list_detail_model: type[ListDetailResponseT],
    pagination_dep: PaginationDependency | None,
    list_status_filter: bool,
    list_service_type: ServiceTypeEnum | None,
    list_extra_params: dict[str, str] | None = None,
    context_provider: Callable[[], Awaitable[Any]] | None,
) -> None:
    """Register the owner-filtered ``GET /`` list route on ``router``.

    The route declares the requested ``status`` / ``service_type`` filter query
    params via :func:`make_list_filter_dep`, short-circuits to an empty result
    when a mismatched ``service_type`` is requested, and threads the active
    ``status_filter`` and ``context_provider`` into the shared list pipeline. A
    ``pagination_dep`` switches the route to a ``PaginatedResponse`` envelope.

    :param router: The plugin router to register the list route on.
    :param task_owner: The task owner the list route filters by.
    :param response_builder: The list builder; supplies the row response model.
    :param list_detail_model: The list/detail response model used in the envelope.
    :param pagination_dep: A pagination dependency, or ``None`` for a plain list.
    :param list_status_filter: Whether to declare the ``status`` query param.
    :param list_service_type: The fixed service type to filter against, or ``None``
        to declare no ``service_type`` param.
    :param list_extra_params: Fixed upstream task-list query parameters (for
        example ``{"parent_is_null": "true"}``) forwarded to the shared pipeline.
        These are server-side filters, so they do not perturb the paginated
        ``total``. Defaults to ``None`` (no extra params).
    :param context_provider: The once-per-request async context provider, or ``None``.
    """
    extra_params = list_extra_params or {}
    filters_param = Annotated[
        ListFilters,
        Depends(
            make_list_filter_dep(
                status=list_status_filter,
                service_type=list_service_type is not None,
            )
        ),
    ]

    def _service_type_excluded(filters: ListFilters) -> bool:
        return (
            list_service_type is not None
            and filters.service_type is not None
            and filters.service_type != list_service_type
        )

    if pagination_dep is None:

        async def _list(tasks_api: TaskAPI, filters: filters_param) -> list[BaseModel]:
            if _service_type_excluded(filters):
                return []
            responses = await build_task_list_responses(
                tasks_api,
                owner=task_owner,
                response_builder=response_builder,
                status_filter=filters.status,
                extra_params=extra_params,
                context_provider=context_provider,
            )
            return cast("list[BaseModel]", responses)

        router.add_api_route(
            "/",
            _list,
            methods=["GET"],
            summary="List",
            response_model=list[list_detail_model],  # ty: ignore[invalid-type-form]
            response_model_by_alias=True,
            dependencies=[IsApiAuthenticated],
        )
    else:
        paginated_param = Annotated[Pagination, Depends(pagination_dep)]

        async def _list_paginated(
            tasks_api: TaskAPI, pagination: paginated_param, filters: filters_param
        ) -> PaginatedResponse:
            if _service_type_excluded(filters):
                return PaginatedResponse.from_pagination([], 0, pagination)
            responses = await build_task_list_responses(
                tasks_api,
                owner=task_owner,
                response_builder=response_builder,
                pagination=pagination,
                status_filter=filters.status,
                extra_params=extra_params,
                context_provider=context_provider,
            )
            return cast("PaginatedResponse", responses)

        router.add_api_route(
            "/",
            _list_paginated,
            methods=["GET"],
            summary="List",
            response_model=PaginatedResponse[
                list_detail_model  # ty: ignore[invalid-type-form]
            ],
            response_model_by_alias=True,
            dependencies=[IsApiAuthenticated],
        )


def derive_crud_routes(
    plugin_schema: AppSchema,
    *,
    task_owner: str,
    get_task: Callable[..., Awaitable[Task]],
    response_builder: TaskResponseBuilder[ListDetailResponseT],
    detail_response_builder: TaskResponseBuilder[Any] | None = None,
    detail_response_model: type[BaseModel] | None = None,
    create_payload: Callable[..., Awaitable[TaskWrite]] | None = None,
    create_response_builder: TaskResponseBuilder[CreateResponseT] | None = None,
    connectivity_check: bool = False,
    detail_path_param: str = "task_name",
    pagination_dep: PaginationDependency | None = None,
    list_status_filter: bool = False,
    list_service_type: ServiceTypeEnum | None = None,
    list_extra_params: dict[str, str] | None = None,
    derive_list: bool = True,
    derive_detail: bool = True,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
    create_extra_deps: Sequence[params.Depends] = (),
    update_enabled: bool = False,
    update_handler: Callable[..., Awaitable[Any]] | None = None,
    update_extra_deps: Sequence[params.Depends] = (),
    delete_enabled: bool = False,
    delete_handler: Callable[..., Awaitable[Any]] | None = None,
    delete_extra_deps: Sequence[params.Depends] = (),
) -> APIRouter:
    """Build a plugin router with the standard schema + CRUD routes.

    Register ``GET /schema`` plus standard task-plugin CRUD routes:
    owner-filtered list (``GET /``), detail (``GET /{detail_path_param}``),
    create (``POST /`` with ``201``), and update/delete (``PUT`` / ``DELETE`` on
    ``/{detail_path_param}``; delete uses ``204``). Update and delete derive a
    standard default from ``update_enabled`` / ``delete_enabled`` alone — the PUT
    mirrors create (rebuild the body through ``create_payload``, PUT upstream,
    render through the create-path response surface with the task's latest
    status), the DELETE is the plain fetch-then-delete. A full ``update_handler``
    / ``delete_handler`` overrides that default for cascade plugins.
    All derived routes use ``IsApiAuthenticated`` and
    ``response_model_by_alias=True``. Enabling ``connectivity_check`` extends
    the create route with a post-creation connectivity probe and a
    ``connectivity_warning`` on its response.

    The derived detail route is greedy: ``GET /{detail_path_param}`` captures
    any single collection-root path segment. If a plugin also needs a static
    collection-root ``GET`` route (for example ``GET /capabilities``), prefer
    a hand-written router (or mount that static route under a sub-prefix)
    instead of this helper.

    .. code-block:: python

        from app.extensions.apps.framework.api import derive_crud_routes

        router = derive_crud_routes(
            archives_schema,
            task_owner="ARCHIVER",
            get_task=get_archives_task,
            response_builder=build_archives_api_task_response,
            create_payload=build_archives_api_task_payload,
        )

    :param plugin_schema: The plugin's fully-validated schema instance.
    :param task_owner: The task owner the list route filters by.
    :param get_task: The raw ``make_task_dep(owner)`` callable resolving a task
        by name; its inner path parameter must equal ``detail_path_param``.
    :param response_builder: Builds the list/detail response model from a task
        and optional status; its return annotation supplies the response model.
    :param detail_response_builder: Builds the detail response; its return
        annotation supplies the detail route's response model. When ``None`` (the
        default) the detail route falls back to ``response_builder`` and the list
        model, byte-identical to a list/detail-shared model. When set, the create
        route also falls back to this builder/model (a created resource renders like
        its detail view) unless an explicit ``create_response_builder`` is given.
    :param detail_response_model: An explicit detail response model that overrides
        return-annotation inference on ``detail_response_builder`` — supply it when
        the detail builder is an exotic callable whose return type cannot be
        introspected. Defaults to ``None`` (infer from the builder).
    :param create_payload: The raw create-payload builder dependency (declares
        the request ``Body()`` model that drives the create ``422``). When
        ``None`` (the default), no ``POST /`` create route is registered — the
        read-only shape used by an app that exposes schema + list + detail only.
        ``connectivity_check`` and ``create_response_builder`` are create-route
        options, so supplying either with ``create_payload=None`` is rejected.
    :param create_response_builder: Builds the create response from a task;
        its return annotation supplies the create response model. Defaults to
        reusing ``response_builder`` (and its model).
    :param connectivity_check: When ``True``, the create route gains a
        ``check_connectivity`` boolean query parameter (default ``True``), runs
        the post-creation connectivity probe, and returns a create response
        whose ``connectivity_warning`` is populated on probe failure. The create
        response model becomes ``create_response_builder``'s model when given,
        else an auto-derived ``<App>CreateResponse``. When ``False`` (default)
        the create route is unchanged.
    :param detail_path_param: The detail/update/delete path-parameter name;
        must equal ``get_task``'s inner path parameter (``make_task_dep`` uses
        ``task_name``).
    :param pagination_dep: A ``make_pagination_dep(...)`` dependency callable.
        When given, the list route takes that dependency (wrapped in
        ``Annotated[Pagination, Depends(...)]``) and returns a
        ``PaginatedResponse``; when ``None`` the list returns a plain list.
    :param list_status_filter: When ``True``, the list route gains a ``status``
        query parameter wired to the pipeline's ``status_filter``. When ``False``
        (default) the list route declares no ``status`` param.
    :param list_service_type: When set, the list route gains a ``service_type``
        query parameter and short-circuits to an empty result before the upstream
        fetch when the requested service type differs from this one. When ``None``
        (default) the list route declares no ``service_type`` param.
    :param list_extra_params: Fixed upstream task-list query parameters (for
        example ``{"parent_is_null": "true"}``) applied server-side on every list
        request, so they filter without perturbing the paginated ``total``.
        Defaults to ``None`` (no extra params).
    :param derive_list: When ``True`` (default), register the owner-filtered
        derived ``GET /`` list route. Set ``False`` to suppress it so a custom
        collection-root list route (mounted last via ``extra_routes``) wins the
        path.
    :param derive_detail: When ``True`` (default), register the greedy derived
        ``GET /{detail_path_param}`` detail route. Set ``False`` to suppress it so
        a custom detail route (mounted last via ``extra_routes``) wins the path.
    :param context_provider: A zero-arg async provider whose once-awaited result
        is bound as the active builder's ``context`` keyword argument across the
        list, detail, and create builds. ``None`` (default) leaves builders unbound.
    :param create_extra_deps: Extra route dependencies appended to the create
        route after ``IsApiAuthenticated``, never replacing it.
    :param update_enabled: When ``True`` and no ``update_handler`` is supplied,
        mount the derived default ``PUT /{detail_path_param}`` route. Defaults to
        ``False`` (no PUT). Requires ``create_payload`` (the derived PUT rebuilds
        the body through it).
    :param update_handler: A fully-formed update handler that overrides the
        derived default; when given, a ``PUT /{detail_path_param}`` route is
        registered using it. The helper applies only ``IsApiAuthenticated`` and
        ``response_model_by_alias`` — any additional route guard (e.g.
        ``HasNoConflictedRunningTasks``) must be declared as one of the handler's
        own signature dependencies, since the handler is passed as a bare callable
        and carries no decorator-level dependencies into the helper.
    :param update_extra_deps: Route dependencies (guards) appended after
        ``IsApiAuthenticated`` on the *derived* PUT. The caller may resolve these to
        a default guard set or a per-route override (e.g. a protected-task check).
        Rejected alongside a full ``update_handler`` (declare guards in the handler
        signature instead) or when the update capability is off. Defaults to ``()``.
    :param delete_enabled: When ``True`` and no ``delete_handler`` is supplied,
        mount the derived default ``DELETE /{detail_path_param}`` route (plain
        fetch-then-delete, ``204``). Defaults to ``False`` (no DELETE).
    :param delete_handler: A fully-formed delete handler that overrides the
        derived default; when given, a ``DELETE /{detail_path_param}`` route is
        registered using it, with ``status_code=204``. As with ``update_handler``,
        any extra route guard must be declared as one of the handler's own
        signature dependencies.
    :param delete_extra_deps: Route dependencies (guards) appended after
        ``IsApiAuthenticated`` on the *derived* DELETE. The caller may resolve these
        to a default guard set or a per-route override. Rejected alongside a full
        ``delete_handler`` or when the delete capability is off. Defaults to ``()``.
    :return: A plugin ``APIRouter`` carrying the schema + CRUD routes.
    :raises TypeError: If ``response_builder``, ``detail_response_builder``, or
        ``create_response_builder`` is an ``async def`` callable (the derived
        handlers invoke it synchronously), does not declare a return-type
        annotation that is a :class:`pydantic.BaseModel` subclass, or cannot accept
        a ``context`` keyword while ``context_provider`` is set; or if
        ``connectivity_check`` is on and an explicit ``create_response_builder``'s
        model omits a ``connectivity_warning`` field.
    :raises ValueError: If ``create_payload`` is ``None`` while
        ``connectivity_check`` is on or a ``create_response_builder`` is supplied
        (both are create-route options that need a create route to attach to); if
        ``update_extra_deps`` are supplied alongside a full ``update_handler`` or
        without the update capability; or if the derived PUT is enabled without a
        ``create_payload`` to rebuild the body.
    """
    _reject_async_builders(
        response_builder=response_builder,
        detail_response_builder=detail_response_builder,
        create_response_builder=create_response_builder,
    )
    _reject_contextless_builders(
        context_provider,
        response_builder=response_builder,
        detail_response_builder=detail_response_builder,
        create_response_builder=create_response_builder,
    )

    list_detail_model = resolve_response_model(
        response_builder, helper="derive_crud_routes", param="response_builder"
    )
    detail_builder, detail_model = _resolve_detail_target(
        response_builder,
        detail_response_builder,
        detail_response_model,
        list_detail_model,
    )

    # Resolving it per route would mint two distinct ``create_model(name, ...)``
    # classes carrying the same name (see ``derive_create_response_model``), whose
    # colliding schema refs make the derived body-schema construction
    # order-sensitive under hash randomization.
    create_response_model = (
        _resolve_create_response_model(
            create_response_builder,
            detail_model,
            connectivity_check=connectivity_check,
            plugin_schema=plugin_schema,
        )
        if create_payload is not None
        else None
    )

    router = APIRouter()
    schema_endpoint(router, plugin_schema)

    detail_path = f"/{{{detail_path_param}}}"

    if derive_list:
        _register_list_route(
            router,
            task_owner=task_owner,
            response_builder=response_builder,
            list_detail_model=list_detail_model,
            pagination_dep=pagination_dep,
            list_status_filter=list_status_filter,
            list_service_type=list_service_type,
            list_extra_params=list_extra_params,
            context_provider=context_provider,
        )

    if derive_detail:

        async def _detail(
            tasks_api: TaskAPI, task: Annotated[Task, Depends(get_task)]
        ) -> BaseModel:
            try:
                latest = await get_task_latest_history(tasks_api, task.name)
                status, last_executed_at = latest.status, latest.finished_at
            except ValueError:
                raise
            except Exception:
                logger.exception("Failed to fetch history for task %s", task.name)
                status, last_executed_at = None, None
            builder = await _bind_context(detail_builder, context_provider)
            return builder(task, status=status, last_executed_at=last_executed_at)

        router.add_api_route(
            detail_path,
            _detail,
            methods=["GET"],
            summary="Detail",
            response_model=detail_model,
            response_model_by_alias=True,
            dependencies=[IsApiAuthenticated],
        )

    if create_payload is None:
        if connectivity_check:
            raise ValueError(
                "derive_crud_routes: connectivity_check=True needs a create route; "
                "pass create_payload or drop connectivity_check"
            )
        if create_response_builder is not None:
            raise ValueError(
                "derive_crud_routes: create_response_builder needs a create route; "
                "pass create_payload or drop create_response_builder"
            )
    else:
        _register_create_route(
            router,
            base_builder=detail_builder,
            create_payload=create_payload,
            create_response_builder=create_response_builder,
            create_response_model=create_response_model,
            connectivity_check=connectivity_check,
            context_provider=context_provider,
            extra_deps=create_extra_deps,
        )

    _register_mutation_routes(
        router,
        detail_path=detail_path,
        get_task=get_task,
        detail_builder=detail_builder,
        create_payload=create_payload,
        create_response_builder=create_response_builder,
        create_response_model=create_response_model,
        connectivity_check=connectivity_check,
        context_provider=context_provider,
        update_enabled=update_enabled,
        update_handler=update_handler,
        update_extra_deps=update_extra_deps,
        delete_enabled=delete_enabled,
        delete_handler=delete_handler,
        delete_extra_deps=delete_extra_deps,
    )

    return router


def derive_execute_route(
    router: APIRouter,
    *,
    task_dep: Any,
    write_model: type[BaseModel] = TaskExecuteWrite,
    response_model: type[BaseModel] = TaskExecutionResponse,
    name: str | None = None,
    description: str = "",
    extra_deps: Sequence[params.Depends] = (),
) -> None:
    """Register the standard ``POST /{task_name}/execute`` route on ``router``.

    Consolidate the execute handler shared verbatim across the task plugins:
    resolve the task, POST ``/execute/{task.name}`` to the Tasks API with the
    request body's non-``None`` fields, validate the upstream reply as a
    :class:`~app.tasks.models.TaskHistoryResponse`, and return
    ``response_model(task_name=..., task_id=..., status=..., created_at=...)``.
    The route pins ``status_code=201`` and the standard guard set
    ``[IsApiAuthenticated, HasNoConflictedRunningTasks, *extra_deps]``.

    The generated handler annotates its ``task`` parameter with ``task_dep``
    (the plugin's ``Annotated[Task, Depends(get_*_task)]`` alias, whose inner
    getter declares the ``task_name`` path parameter) and its ``body``
    parameter with ``write_model`` (so FastAPI parses and documents the JSON
    body from that annotation).

    ``name`` and ``description`` default to the inner handler's own
    ``__name__`` / docstring (FastAPI's own fallback), yielding a generic
    ``execute`` operation for a new plugin. Pass them explicitly to reproduce
    an existing route's ``operationId`` / ``summary`` (from ``name``) and
    ``description`` (from the docstring) byte-for-byte when migrating a
    hand-written handler onto this helper.

    .. code-block:: python

        from app.extensions.apps.framework.api import derive_execute_route

        derive_execute_route(
            router,
            name="checksums_api_execute",
            description="Execute a checksum task.",
            task_dep=ChecksumsTask,
        )

    :param router: The plugin's ``APIRouter``.
    :param task_dep: The plugin's ``Annotated[Task, Depends(get_*_task)]``
        dependency alias; its inner getter resolves the task by name (and owns
        the ``task_name`` path parameter and the 404-on-mismatch behaviour).
    :param write_model: The execute request body model; its annotation drives
        the requestBody schema and the body-validation ``422``. Defaults to
        :class:`~app.extensions.apps.framework.responses.TaskExecuteWrite`.
    :param response_model: The execute response model, constructed with
        ``task_name``, ``task_id``, ``status`` and ``created_at`` keyword
        arguments. A model that declares fewer of them drops the rest silently,
        while one configured ``extra="forbid"`` would raise at dispatch.
        Defaults to
        :class:`~app.extensions.apps.framework.responses.TaskExecutionResponse`.
    :param name: The route name; drives the OpenAPI ``operationId`` and
        ``summary``. ``None`` falls back to the inner handler's ``__name__``.
    :param description: The OpenAPI operation ``description``; ``""`` falls back
        to the inner handler's docstring.
    :param extra_deps: Extra route dependencies appended to the standard guard
        set, never replacing it.
    :raises TypeError: If ``write_model`` or ``response_model`` is not a
        :class:`pydantic.BaseModel` subclass. Raised at registration time.
    :raises ValueError: If ``router`` already exposes a
        ``POST /{task_name}/execute`` route.
    """
    for model, param in (
        (write_model, "write_model"),
        (response_model, "response_model"),
    ):
        if not (inspect.isclass(model) and issubclass(model, BaseModel)):
            raise TypeError(
                f"derive_execute_route: {param} must be a pydantic.BaseModel "
                f"subclass; got {model!r}"
            )

    router_prefix = getattr(router, "prefix", "") or ""
    expected_path = f"{router_prefix}/{{task_name}}/execute"
    for existing in router.routes:
        existing_methods = set(getattr(existing, "methods", None) or ())
        if (
            getattr(existing, "path", None) == expected_path
            and "POST" in existing_methods
        ):
            raise ValueError(
                "derive_execute_route: router already has a POST "
                "/{task_name}/execute route; call this helper at most once per "
                "plugin router"
            )

    async def execute(
        task: task_dep,
        body: write_model,  # ty: ignore[invalid-type-form]
        tasks_api: TaskAPI,
    ) -> BaseModel:
        """Resolve, dispatch, and wrap a standard task execution."""
        created = await tasks_api.post(
            f"/execute{task_path(task.name)}",
            json=body.model_dump(exclude_none=True),
        )
        task_history = TaskHistoryResponse.model_validate(created)
        return response_model(
            task_name=task.name,
            task_id=task_history.id,
            status=task_history.status,
            created_at=task_history.created_at,
        )

    router.add_api_route(
        "/{task_name}/execute",
        execute,
        methods=["POST"],
        name=name,
        description=description,
        status_code=status.HTTP_201_CREATED,
        response_model=response_model,
        response_model_by_alias=True,
        dependencies=[IsApiAuthenticated, HasNoConflictedRunningTasks, *extra_deps],
    )


@dataclass(slots=True)
class CascadeCreatePlan:
    """Carry the pieces :func:`derive_cascade_create_route` needs to create a group.

    A plugin's ``create_plan`` dependency returns one of these: the parent
    :class:`~app.tasks.models.TaskWrite` to stamp and refetch, the validated
    create ``form`` to persist under ``RESERVED_FORM_KEY``, and a ``cascade``
    closure that POSTs the parent plus its children (bound to whatever
    child/derived payloads the app already built). The helper stamps
    ``parent_write`` *before* invoking ``cascade``, so a closure that
    re-serialises the parent (``parent_write.model_dump()``) must do so at cascade
    time to keep the stamp.

    :param parent_write: The parent task envelope, stamped then refetched by name.
    :param form: The validated create-form body to stamp onto ``parent_write``.
    :param cascade: An awaitable closure that POSTs the parent and its children.
    """

    parent_write: TaskWrite
    form: BaseModel
    cascade: Callable[[RemoteAPI], Awaitable[None]]


def _validate_cascade_create_registration(
    router: APIRouter,
    response_model: type[BaseModel],
    *,
    connectivity_check: bool,
) -> None:
    """Validate the registration-time inputs for ``derive_cascade_create_route``.

    :param router: The plugin router the create route registers on.
    :param response_model: The create response model.
    :param connectivity_check: Whether the route attaches a connectivity warning.
    :raises TypeError: If ``response_model`` is not a :class:`pydantic.BaseModel`
        subclass, or ``connectivity_check`` is on and ``response_model`` omits a
        ``connectivity_warning`` field.
    :raises ValueError: If ``router`` already exposes a ``POST /`` route.
    """
    if not (inspect.isclass(response_model) and issubclass(response_model, BaseModel)):
        raise TypeError(
            f"derive_cascade_create_route: response_model must be a "
            f"pydantic.BaseModel subclass; got {response_model!r}"
        )
    if (
        connectivity_check
        and CONNECTIVITY_WARNING_FIELD not in response_model.model_fields
    ):
        raise TypeError(
            "derive_cascade_create_route: response_model must declare a "
            "connectivity_warning field when connectivity_check=True, so the "
            "probe result is attached rather than silently dropped."
        )

    router_prefix = getattr(router, "prefix", "") or ""
    expected_path = f"{router_prefix}/"
    for existing in router.routes:
        existing_methods = set(getattr(existing, "methods", None) or ())
        if (
            getattr(existing, "path", None) == expected_path
            and "POST" in existing_methods
        ):
            raise ValueError(
                "derive_cascade_create_route: router already has a POST / route; "
                "call this helper at most once per plugin router"
            )


def derive_cascade_create_route(
    router: APIRouter,
    *,
    create_plan: Any,
    get_task: Callable[..., Awaitable[Task]],
    response_builder: Callable[[Task, RemoteAPI], Awaitable[BaseModel]],
    response_model: type[BaseModel],
    connectivity_check: bool = False,
    name: str | None = None,
    description: str = "",
    status_code: int = status.HTTP_201_CREATED,
    extra_deps: Sequence[params.Depends] = (),
) -> None:
    """Register a cascade-shaped ``POST /`` create route on ``router``.

    The cascade-shaped sibling of :func:`_register_create_route`: where that
    helper POSTs a single task, this one creates a parent task plus its children.
    Each app supplies a ``create_plan`` dependency (the analog of the framework's
    own ``create_payload`` dependency) that builds its writes and returns a
    :class:`CascadeCreatePlan`; the helper owns the invariant sequence — stamp the
    parent form, run the cascade, refetch the canonical parent, build the
    response, and (when ``connectivity_check`` is on) attach the post-creation
    connectivity warning via ``model_copy``.

    The generated handler annotates its ``plan`` parameter with ``create_plan``
    (the plugin's ``Annotated[CascadeCreatePlan, Depends(build_*_cascade_plan)]``
    alias, whose inner dependency declares the JSON request body). With
    ``connectivity_check`` on it gains a ``check_connectivity`` query parameter,
    verbatim from :func:`_register_create_route`.

    Unlike the derived create/execute routes, the migrated cascade create routes
    carry no per-route guard: authentication is inherited from the ``/api`` mount
    and the hand-written handlers declared no route dependencies, so the default
    ``extra_deps=()`` preserves their OpenAPI identity.

    :param router: The plugin's ``APIRouter``.
    :param create_plan: The plugin's ``Annotated[CascadeCreatePlan, Depends(...)]``
        dependency alias; its inner dependency declares the JSON request body and
        builds the parent write, form, and cascade closure.
    :param get_task: The by-name task getter, called ``get_task(name, tasks_api)``
        to refetch the canonical parent after the cascade.
    :param response_builder: An async ``(task, tasks_api) -> BaseModel`` builder
        rendering the create response from the refetched parent.
    :param response_model: The create response model; drives the response schema.
    :param connectivity_check: Whether to add the connectivity probe and the
        ``check_connectivity`` query parameter. Defaults to ``False``.
    :param name: The route name; drives the OpenAPI ``operationId`` and
        ``summary``. ``None`` falls back to the inner handler's ``__name__``.
    :param description: The OpenAPI operation ``description``; ``""`` falls back to
        the inner handler's docstring.
    :param status_code: The success status code. Defaults to ``201``.
    :param extra_deps: Extra route dependencies. Defaults to ``()`` — no per-route
        guard, matching the hand-written create handlers.
    :raises TypeError: If ``response_model`` is not a :class:`pydantic.BaseModel`
        subclass, or if ``connectivity_check`` is on and ``response_model`` omits a
        ``connectivity_warning`` field. Raised at registration time.
    :raises ValueError: If ``router`` already exposes a ``POST /`` route.
    """
    _validate_cascade_create_registration(
        router, response_model, connectivity_check=connectivity_check
    )

    async def _run(
        plan: CascadeCreatePlan,
        tasks_api: RemoteAPI,
        *,
        check_connectivity: bool | None,
    ) -> BaseModel:
        """Run the stamp, cascade, refetch, and render sequence for the group.

        :param plan: The app-built plan carrying the parent write, form, and cascade.
        :param tasks_api: The upstream Tasks API client.
        :param check_connectivity: Whether to run the connectivity probe. ``None``
            when the probe is disabled for the route.
        :return: The rendered create response.
        """
        logger.debug(
            "Cascade-create task group (JSON path): %s", plan.parent_write.name
        )
        stamp_form_input(plan.parent_write, plan.form)
        await plan.cascade(tasks_api)
        task = await get_task(plan.parent_write.name, tasks_api)
        response = await response_builder(task, tasks_api)
        if check_connectivity is not None:
            warning = await maybe_record_connectivity_warning(
                tasks_api,
                task.data.get("meta", {}),
                check_connectivity=check_connectivity,
            )
            if warning is not None:
                response = response.model_copy(
                    update={CONNECTIVITY_WARNING_FIELD: warning}
                )
        return response

    if connectivity_check:

        async def create(
            plan: create_plan,
            tasks_api: TaskAPI,
            *,
            check_connectivity: Annotated[bool, Query()] = True,
        ) -> BaseModel:
            """Create the task group, then probe connectivity on the created parent.

            :param plan: The app-built cascade plan (resolved from ``create_plan``).
            :param tasks_api: The upstream Tasks API client.
            :param check_connectivity: Whether to run the post-creation probe.
            :return: The rendered create response with any connectivity warning.
            """
            return await _run(plan, tasks_api, check_connectivity=check_connectivity)
    else:

        async def create(plan: create_plan, tasks_api: TaskAPI) -> BaseModel:
            """Create the task group from the plan and return the response.

            :param plan: The app-built cascade plan (resolved from ``create_plan``).
            :param tasks_api: The upstream Tasks API client.
            :return: The rendered create response.
            """
            return await _run(plan, tasks_api, check_connectivity=None)

    router.add_api_route(
        "/",
        create,
        methods=["POST"],
        name=name,
        description=description,
        status_code=status_code,
        response_model=response_model,
        response_model_by_alias=True,
        dependencies=[*extra_deps],
    )


def _reject_unexposed_list_query(
    source: ScriptSource[Any],
    *,
    name: str,
    pagination_dep: PaginationDependency | None,
    list_query_spec: ListQuerySpec | None,
) -> None:
    """Reject either half-wiring of a list query the derived route would not expose.

    Both directions end the same way — a query resolved but never reaching the route.
    Without a spec the list route registers the handler that takes no query parameter,
    so the source's own dependency is never mounted. Without a pagination dependency the
    route is the unpaginated ``list_scripts()``, which calls ``list_scripts(None, None)``
    and mounts no query dependency either, spec or not. ``TaskExecutionApp`` checks the
    same two pairings, but this seam is public and wired directly, so it cannot rely on
    that.

    :param source: The script source whose query knobs to check.
    :param name: The app name, for the error message.
    :param pagination_dep: The pagination dependency the caller supplied, if any.
    :param list_query_spec: The spec the caller supplied, if any.
    :raises ValueError: When the source resolves a list query but no spec was supplied,
        or when a spec was supplied for a route that derives no paginated list.
    """
    if list_query_spec is None:
        if source.list_query_dep is not None or source.in_memory_list_query:
            raise ValueError(
                f"derive_script_routes: {name!r} source resolves a list query "
                "(list_query_dep or in_memory_list_query) but no list_query_spec was "
                "supplied, so the derived list route would expose none of it"
            )
        return
    if pagination_dep is None:
        raise ValueError(
            f"derive_script_routes: {name!r} was given a list_query_spec but no "
            "pagination_dep, so the derived list route is the unpaginated one and "
            "exposes no sort/search params — supply pagination_dep or drop "
            "list_query_spec"
        )


def derive_script_routes(
    source: ScriptSource[Any],
    *,
    name: str,
    pagination_dep: PaginationDependency | None = None,
    list_query_spec: ListQuerySpec | None = None,
) -> APIRouter:
    """Build a plugin router carrying a script source's derived surface.

    Register the script-centric surface a script-backed task app exposes, mirroring
    the working ``snippets`` JSON API: owner-agnostic listing (``GET /``), per-script
    form schema (``GET /snippet/schema``), execute delegation
    (``POST /snippet/execute``, ``201``), execution history
    (``GET /snippet/history``), and — only when ``source.static_schema`` is set — the
    plugin-level ``GET /schema``. All routes carry ``IsApiAuthenticated``.

    Per-script routes carry the script filename in a ``snippet_filename`` query
    parameter under the ``/snippet/`` sub-prefix (never a path segment), so a greedy
    detail route cannot shadow ``GET /`` or ``GET /schema``. The execute route
    validates the request ``args`` against the script's dynamic execution model
    (``422`` on failure), delegates meta assembly to ``source.build_execution_meta``
    with the model's *coerced* ``args`` (so a consumer never sees the raw,
    unnormalised values the dynamic model may have type-cast), and posts
    ``{"meta": ...}`` to ``/execute/{script.execution_task_name}`` — a distinct
    endpoint and body from the model-first three-phase create envelope.

    :param source: The script source supplying the listing, form-synthesis,
        execution-meta, list-row, and optional static-schema hooks.
    :param name: The app name seeding each derived route's name (and OpenAPI
        ``operationId``), so two script apps never collide on a generic route name.
    :param pagination_dep: A ``make_pagination_dep(...)`` dependency callable.
        When given, the list route takes that dependency (wrapped in
        ``Annotated[Pagination, Depends(...)]``) and returns a ``PaginatedResponse``.
        When ``None`` the list returns a plain list.
    :param list_query_spec: The app's sort/search allowlist. When given, the list route
        exposes exactly Core's ``sort`` param (plus ``search`` when the spec has
        searchable columns) via the SQL dependency or, for a source that sets
        ``in_memory_list_query``, the in-memory dependency; the resolved query is handed
        to ``source.list_scripts``. It requires ``pagination_dep``, since the unpaginated
        route mounts no query dependency. When ``None`` the paginated route fetches the
        full set and returns a client-side slice, so a source that resolves a query is
        rejected rather than silently narrowed.
    :return: A plugin ``APIRouter`` carrying the derived script surface.
    :raises ValueError: When the source resolves a list query (via ``list_query_dep``
        or ``in_memory_list_query``) but no ``list_query_spec`` was supplied, or when a
        ``list_query_spec`` is supplied without a ``pagination_dep`` — the unpaginated
        list route exposes no query either way.
    """
    _reject_unexposed_list_query(
        source,
        name=name,
        pagination_dep=pagination_dep,
        list_query_spec=list_query_spec,
    )
    router = APIRouter()
    if source.static_schema is not None:
        schema_endpoint(router, source.static_schema)

    script_param = Annotated[Any, Depends(make_script_dep(source))]

    list_model = (
        list[source.list_response_model]  # ty: ignore[invalid-type-form]
        if source.list_response_model is not None
        else None
    )

    if pagination_dep is None:

        async def list_scripts() -> list[BaseModel]:
            """List every discovered script as its list-row projection."""
            scripts, _ = await source.list_scripts(None, None)
            return [source.list_response(script) for script in scripts]

        router.add_api_route(
            "/",
            list_scripts,
            methods=["GET"],
            name=f"{name}_api_list",
            summary="List",
            response_model=list_model,
            dependencies=[IsApiAuthenticated],
        )
    else:
        paginated_param = Annotated[Pagination, Depends(pagination_dep)]
        paginated_list_model = (
            PaginatedResponse[
                source.list_response_model  # ty: ignore[invalid-type-form]
            ]
            if source.list_response_model is not None
            else PaginatedResponse
        )

        # FastAPI derives each route's dependencies from its handler signature, so
        # the spec-backed path (which needs the list-query dependency injected) and
        # the fetch-all-then-slice fallback require distinct handler signatures —
        # hence two closures, exactly one of which is registered below.
        if list_query_spec is not None:
            # A source that adds filter params supplies a dependency composing the
            # Core one, so the spec stays the sole sort/search authority either way.
            query_dep = source.list_query_dep or (
                make_in_memory_list_query_dep(InMemoryListQueryApplier(list_query_spec))
                if source.in_memory_list_query
                else make_list_query_dep(list_query_spec)
            )
            list_query_param = Annotated[Any, Depends(query_dep)]

            async def list_scripts_paginated(
                pagination: paginated_param, list_query: list_query_param
            ) -> PaginatedResponse:
                """List scripts as a filtered, sorted, paginated projection."""
                rows, total = await source.list_scripts(list_query, pagination)
                items = [source.list_response(script) for script in rows]
                return PaginatedResponse.from_pagination(items, total, pagination)

        else:

            async def list_scripts_paginated(
                pagination: paginated_param,
            ) -> PaginatedResponse:
                """List discovered scripts as a paginated projection."""
                rows, total = await source.list_scripts(None, pagination)
                items = [source.list_response(script) for script in rows]
                return PaginatedResponse.from_pagination(items, total, pagination)

        router.add_api_route(
            "/",
            list_scripts_paginated,
            methods=["GET"],
            name=f"{name}_api_list",
            summary="List",
            response_model=paginated_list_model,
            dependencies=[IsApiAuthenticated],
        )

    @router.get(
        "/snippet/schema",
        name=f"{name}_api_script_schema",
        summary="Script schema",
        response_model_by_alias=True,
        response_model_exclude_none=True,
        dependencies=[IsApiAuthenticated],
    )
    async def script_schema(script: script_param) -> AppSchema:
        """Return the per-script form schema synthesised from its parameters."""
        return source.build_form_schema(script)

    @router.get(
        "/snippet/history",
        name=f"{name}_api_script_history",
        summary="Script history",
        dependencies=[IsApiAuthenticated],
    )
    async def script_history(
        script: script_param, tasks_api: TaskAPI
    ) -> ArbitraryMapping:
        """Proxy the per-script execution history from the Tasks API by filename."""
        return ArbitraryMapping(
            as_json_object(
                await tasks_api.get(
                    task_path(script.execution_task_name, "/history/"),
                    params={"snippet_filename": script.filename},
                )
            )
        )

    @router.post(
        "/snippet/execute",
        name=f"{name}_api_script_execute",
        summary="Execute script",
        status_code=status.HTTP_201_CREATED,
        dependencies=[IsApiAuthenticated],
    )
    async def script_execute(
        script: script_param,
        body: ScriptExecuteWrite,
        tasks_api: TaskAPI,
    ) -> ScriptExecutionResponse:
        """Validate the args, assemble the meta, and dispatch the execution."""
        return await execute_script(source, script, body, tasks_api)

    return router
