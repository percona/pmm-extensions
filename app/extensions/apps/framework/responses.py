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

"""Provide the shared JSON-API list pipeline, default builder, and base models."""

import functools
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, cast, overload, Protocol, TypeVar

from pydantic import BaseModel, computed_field, create_model, Field, FutureDatetime

from app.core.pagination import build_proxied_page, PaginatedResponse, Pagination
from app.core.requests import as_json_object
from app.core.utils.fields import ARBITRARY_ARGS_SCHEMA, UTCDatetime
from app.extensions.apps.framework.connectivity import (
    CONNECTIVITY_WARNING_FIELD,
    ConnectivityWarning,
)
from app.extensions.apps.framework.task_status import batch_get_latest_statuses
from app.extensions.deps import TaskAPI
from app.inventory.models import ServiceTypeEnum
from app.tasks.anonymizer.config import anonymizer_settings
from app.tasks.anonymizer.entities import PIIEntity
from app.tasks.models import Task, TaskBackendEnum, TaskHistoryStatusEnum

R = TypeVar("R", bound=BaseModel)


def serialized_field_names(model: type[BaseModel]) -> frozenset[str]:
    """Return the field names a ``model_dump(by_alias=True)`` of ``model`` emits.

    Build the set of wire names a list/detail row serialises: every
    ``model_fields`` entry not marked ``exclude=True``, resolved through
    ``serialization_alias`` then ``alias`` then the attribute name, unioned
    with every computed field resolved through its alias then attribute name.
    Use this — not ``model_fields`` alone — when validating that a
    ``list_view`` column key or ``detail_view`` field path is present in
    the serialized row.

    :param model: The Pydantic response model class whose dump keys are listed.
    :return: The frozenset of names ``model_dump(by_alias=True)`` emits for
        ``model``.
    """
    names: set[str] = set()
    for name, field in model.model_fields.items():
        if field.exclude:
            continue
        if isinstance(field.serialization_alias, str):
            names.add(field.serialization_alias)
        elif isinstance(field.alias, str):
            names.add(field.alias)
        else:
            names.add(name)
    names.update(
        field.alias if isinstance(field.alias, str) else name
        for name, field in model.model_computed_fields.items()
    )
    return frozenset(names)


def root_segment(path: str) -> str:
    """Return the leading field name of a dotted/indexed view path.

    Stripping the index is only sound for paths resolved against serialized
    rows, where ``[N]`` is a real segment. Conditional-rule references are not
    such paths — the decoration-time gate in
    :func:`~app.extensions.apps.framework.rules._validate_plan_against_model_fields`
    keeps its own index-rejecting split on purpose.

    :param path: A list-view column key or detail-view field path (for example
        ``"target.service"`` or ``"data.meta[0]"``).
    :return: The path's first segment, stripped of any ``[N]`` index.
    """
    return path.split(".", 1)[0].split("[", 1)[0]


def dump_with_excluded_fields(
    model: BaseModel, *, exclude: set[str] | None = None
) -> dict[str, Any]:
    """Dump ``model`` including serialization-excluded fields for rebuilds.

    ``model_dump`` omits fields marked ``Field(exclude=True)``. Detail/create
    builders that rebuild via ``Subclass(**base.model_dump(), ...)`` need those
    attributes restored. Reinjection uses the live attribute values (not copies),
    which is safe for the current scalar ``exclude=True`` fields; callers that
    later mark a mutable container as ``exclude=True`` must treat the rebuilt
    dump as aliased to the source. Computed fields are always omitted so the
    dump is safe to splat into a subclass constructor. An explicit ``exclude``
    entry still wins over reinjection.

    :param model: The response model instance to dump.
    :param exclude: Extra field names omitted from the dump (in addition to
        computed fields). Wins over ``Field(exclude=True)`` reinjection.
    :return: A dump including ``exclude=True`` fields not named in ``exclude``.
    """
    excluded_names = exclude or set()
    data = model.model_dump(
        exclude=excluded_names | set(type(model).model_computed_fields)
    )
    data |= {
        name: getattr(model, name)
        for name, field in type(model).model_fields.items()
        if field.exclude and name not in excluded_names
    }
    return data


class BaseTaskResponse(BaseModel):
    """Represent the universal task-response surface for standard task apps.

    Carry the fields shared by every standard ``TaskExecutionApp`` response:
    the task identity, the resolved execution status, the stored configuration,
    and the audit/anonymization metadata. A standard app whose response has no
    app-specific fields uses this model directly; an app with extras subclasses
    it. The model is parametrized by the task ``owner``, which drives the
    ``anonymized_entities`` default-entity lookup but is excluded from the
    serialized detail/list payload (as is ``service_type``).

    :param name: The task name.
    :param owner: The task-category owner (for example ``BACKUPS``). Kept
        as a model attribute for ``anonymized_entities`` and server-side
        filtering; excluded from the serialized detail/list payload.
    :param service_type: The database service type, stamped by the builder;
        ``None`` for an app without a fixed service type. Excluded from the
        serialized detail/list payload.
    :param status: The latest known execution status; ``None`` until the task
        runs.
    :param last_executed_at: The most recent time the task finished executing
        (``max`` ``finished_at`` across its history). Reported even while a
        re-run is in progress (showing the prior completion); ``None`` until the
        task has finished at least once.
    :param id: The task's unique identifier.
    :param backend: The backend worker/engine executing the task.
    :param data: The raw configuration and parameters used for execution. Tasks
        created through the JSON schema-driven path also carry a reserved additive
        ``_form`` key holding the verbatim, validated create-form body for
        prefilling an edit form; it is absent for tasks created through a legacy
        form, so consumers must treat it as optional.
    :param protected: Whether the task is protected from deletion or modification.
    :param alert_on_fail: Whether a notification is sent on task failure.
    :param anonymize_mask: Bitmask of PII entities to anonymize; ``None`` falls
        back to the owner's configured defaults.
    :param created_at: The timestamp when the task was first created.
    :param updated_at: The timestamp of the last modification to the task.
    :param created_by: Display name for the user who initiated the task (system
        label or provider username when resolvable, otherwise the stored user id).
    :param last_updated_by: Display name for the user who last modified the task
        record (system label or provider username when resolvable, otherwise the
        stored user id).
    :param connectivity_warning: A warning surfaced when the post-creation
        database connectivity check fails. ``None`` when the check passes, is
        opted out, or the task meta lacks the connectivity keys.
    :param anonymized_entities: Sorted PII entity names derived from
        ``anonymize_mask`` (or from the owner's configured defaults when the mask
        is ``None``). Read-only; computed on serialisation.
    """

    name: str
    owner: str = Field(exclude=True)
    service_type: ServiceTypeEnum | None = Field(default=None, exclude=True)
    status: TaskHistoryStatusEnum | None = None
    last_executed_at: UTCDatetime | None = None
    id: int | None = None
    backend: TaskBackendEnum
    data: dict[str, Any] = Field(json_schema_extra=ARBITRARY_ARGS_SCHEMA)
    protected: bool
    alert_on_fail: bool
    anonymize_mask: int | None = None
    created_at: UTCDatetime | None = None
    updated_at: UTCDatetime | None = None
    created_by: str | None = None
    last_updated_by: str | None = None
    connectivity_warning: ConnectivityWarning | None = None

    @computed_field
    @property
    def anonymized_entities(self) -> list[str]:
        """Return sorted PII entity names decoded from ``anonymize_mask``."""
        entities = (
            PIIEntity.decode_selection(self.anonymize_mask)
            if self.anonymize_mask is not None
            else anonymizer_settings.DEFAULT_ENTITIES[self.owner]
        )
        return sorted(entity.name for entity in entities)

    def model_dump_with_excluded_fields(
        self, *, exclude: set[str] | None = None
    ) -> dict[str, Any]:
        """Dump fields for reconstructing this response or a subclass.

        Convenience wrapper around :func:`dump_with_excluded_fields` for
        ``BaseTaskResponse`` subclasses.

        :param exclude: Extra field names omitted from the dump; wins over
            ``Field(exclude=True)`` reinjection.
        :return: A dump including serialization-excluded fields.
        """
        return dump_with_excluded_fields(self, exclude=exclude)


class TaskExecuteWrite(BaseModel):
    """Represent the default JSON request body for executing a task.

    :param eta: Optional future datetime to schedule execution.
    :param chain_task_names: Optional list of task names to chain after this one.
    :param chain_on_failure: Whether to run chained tasks even on failure.
    """

    eta: FutureDatetime | None = None
    chain_task_names: list[str] | None = None
    chain_on_failure: bool | None = None


class TaskExecutionResponse(BaseModel):
    """Represent the default response from a task execute route.

    ``started_at`` is deliberately not carried: the worker sets it, so it is
    still ``None`` on the row this response is built from.

    :param task_name: The name of the task that was executed.
    :param task_id: The id of the task-history row created by the tasks API.
        Optional because :class:`~app.tasks.models.TaskHistoryResponse` types it
        so, not because a dispatched run is expected to lack one.
    :param status: The status of the task-history row the tasks API created,
        as it stood at dispatch.
    :param created_at: When the tasks API created that row.
    """

    task_name: str
    task_id: int | None = None
    status: TaskHistoryStatusEnum
    created_at: UTCDatetime


class TaskResponseBuilder(Protocol[R]):
    """Build a JSON-API response model from a task and its latest status.

    This is the minimum builder contract. The pipeline invokes the builder as
    ``response_builder(task, status=..., last_executed_at=...)``, so every
    per-plugin builder whose ``status`` and ``last_executed_at`` are
    positional-or-keyword or keyword-only is a structural subtype. When a context
    provider is configured the pipeline additionally binds its once-awaited
    result as a ``context`` keyword (via :func:`functools.partial`), so a
    context-aware builder declares an extra keyword-only ``context`` (or
    ``**kwargs``). That keyword is intentionally left out of this protocol so a
    contextless builder stays a valid subtype; the route-derivation layer rejects
    a builder paired with a context provider that cannot accept ``context``.
    """

    def __call__(
        self,
        task: Task,
        *,
        status: TaskHistoryStatusEnum | None = None,
        last_executed_at: datetime | None = None,
    ) -> R:
        """Build the response model for ``task``, its ``status`` and last run."""


def build_default_task_response(
    response_model: type[R],
    task: Task,
    status: TaskHistoryStatusEnum | None = None,
    *,
    last_executed_at: datetime | None = None,
    extras: Mapping[str, Any] | None = None,
) -> R:
    """Build ``response_model`` from a task dump plus run info and optional extras.

    ``extras`` is merged over the dumped payload with ``dict.update`` semantics,
    so it can both add new fields (``service_type``, ``hostname``,
    ``backup_type``, ``connectivity_warning``) and override dumped ones (the
    ``created_by`` / ``last_updated_by`` username remap, or a mutated ``data``
    carrying ``_command_line``). ``status`` and ``last_executed_at`` are injected
    before ``extras`` so an app could still override them if ever needed.

    :param response_model: The response model class to construct.
    :param task: The task to dump into the response payload.
    :param status: The latest known execution status for the task.
    :param last_executed_at: The most recent time the task finished executing
        (``max`` ``finished_at``); ``None`` until it has finished once.
    :param extras: Fields merged over the dumped payload (add or override).
    :return: A validated response model instance.
    """
    payload = task.model_dump()
    payload["status"] = status
    payload["last_executed_at"] = last_executed_at
    if extras:
        payload.update(extras)
    return response_model(**payload)


def derive_create_response_model(
    response_model: type[R],
    *,
    name: str,
    doc: str | None = None,
    extra_fields: Mapping[str, tuple[Any, Any]] | None = None,
) -> type[R]:
    """Derive an ``<App>CreateResponse`` subclass of ``response_model``.

    Return a subclass carrying every field of ``response_model`` plus
    ``connectivity_warning: ConnectivityWarning | None = None`` and any
    cascade-contributed warning fields in ``extra_fields``.
    ``connectivity_warning`` is applied last, so an ``extra_fields`` entry can
    never override it.

    :param response_model: The base response model whose fields the derived
        model inherits.
    :param name: The derived model's class name; fixes the OpenAPI component
        title instead of a mangled dynamic-model name.
    :param doc: The derived model's docstring, surfaced as the OpenAPI
        component description; ``None`` leaves the component undescribed.
    :param extra_fields: Cascade-contributed ``name -> (type, default)`` fields
        merged in beneath ``connectivity_warning``.
    :return: A ``response_model`` subclass that adds ``connectivity_warning``.
    """
    fields = dict(extra_fields or {})
    fields[CONNECTIVITY_WARNING_FIELD] = (ConnectivityWarning | None, None)
    return cast(
        "type[R]",
        create_model(name, __base__=response_model, __doc__=doc, **fields),
    )


def _owner_list_params(
    owner: str,
    pagination: Pagination | None,
    extra_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the upstream task-list GET params for ``owner`` and a page window.

    :param owner: The task owner to list tasks for.
    :param pagination: The page window merged in as ``offset`` / ``limit``, or
        ``None`` for an unpaginated request.
    :param extra_params: Fixed server-side filters (for example
        ``{"parent_is_null": "true"}``) the Tasks API applies before returning the
        page.
    :return: The merged upstream task-list query parameters.
    """
    params = {"owner": owner}
    if pagination is not None:
        params |= pagination.model_dump()
    if extra_params:
        params |= extra_params
    return params


@overload
async def build_task_list_responses(
    tasks_api: TaskAPI,
    *,
    owner: str,
    response_builder: TaskResponseBuilder[R],
    pagination: None = None,
    status_filter: TaskHistoryStatusEnum | None = None,
    task_filter: Callable[[Task], bool] | None = None,
    extra_params: dict[str, str] | None = None,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
) -> list[R]: ...


@overload
async def build_task_list_responses(
    tasks_api: TaskAPI,
    *,
    owner: str,
    response_builder: TaskResponseBuilder[R],
    pagination: Pagination,
    status_filter: TaskHistoryStatusEnum | None = None,
    task_filter: Callable[[Task], bool] | None = None,
    extra_params: dict[str, str] | None = None,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
) -> PaginatedResponse[R]: ...


async def build_task_list_responses(
    tasks_api: TaskAPI,
    *,
    owner: str,
    response_builder: TaskResponseBuilder[R],
    pagination: Pagination | None = None,
    status_filter: TaskHistoryStatusEnum | None = None,
    task_filter: Callable[[Task], bool] | None = None,
    extra_params: dict[str, str] | None = None,
    context_provider: Callable[[], Awaitable[Any]] | None = None,
) -> list[R] | PaginatedResponse[R]:
    """Assemble JSON-API task responses for an owner through one shared pipeline.

    The pipeline fetches the owner's tasks, applies an optional ``task_filter``
    before any status fan-out, enriches each surviving task with its latest
    status, selects the ones matching ``status_filter``, and builds a response
    per selection. Unpaginated calls return a ``list``; supplying ``pagination``
    returns a ``PaginatedResponse`` whose ``total`` is the filtered current-page
    count when a client-side filter (``status_filter`` or ``task_filter``) is
    active and the upstream total otherwise.

    When ``context_provider`` is given it is awaited exactly once per page —
    before the per-row build, including the empty page — and its result is bound
    into every per-row build as ``builder(task, status=..., context=...)`` via
    :func:`functools.partial`. This lets a sync builder receive async side-data
    (for example a username map) without the builder itself becoming async, which
    the framework rejects. When ``None`` (the default) the builder is invoked
    unchanged as ``builder(task, status=...)``.

    :param tasks_api: The Tasks API client used for the list and status lookups.
    :param owner: The task owner to list tasks for.
    :param response_builder: Builder invoked as ``builder(task, status=...)``.
    :param pagination: Page window; when omitted a plain ``list`` is returned.
    :param status_filter: Keep only tasks whose latest status matches this.
    :param task_filter: Predicate applied before status enrichment.
    :param extra_params: Fixed upstream task-list query parameters merged into the
        request as server-side filters, so they do not perturb the paginated
        ``total`` the way a client-side ``task_filter`` does.
    :param context_provider: Zero-arg async provider whose once-awaited result is
        bound into every per-row build as a ``context`` keyword argument.
    :return: The built responses, paginated when ``pagination`` is supplied.
    """
    response = as_json_object(
        await tasks_api.get(
            "/", params=_owner_list_params(owner, pagination, extra_params)
        )
    )
    tasks = [Task.model_validate(item) for item in response["items"]]
    if task_filter is not None:
        tasks = [task for task in tasks if task_filter(task)]

    builder = response_builder
    if context_provider is not None:
        context = await context_provider()
        builder = functools.partial(response_builder, context=context)

    latest = await batch_get_latest_statuses(tasks_api, [task.name for task in tasks])
    items = [
        builder(
            task,
            status=(entry := latest.get(task.name)) and entry.status,
            last_executed_at=entry.finished_at if entry else None,
        )
        for task in tasks
        if status_filter is None
        or ((row := latest.get(task.name)) and row.status) == status_filter
    ]

    if pagination is None:
        return items
    client_side_filtered = status_filter is not None or task_filter is not None
    return build_proxied_page(
        items, response, pagination, client_side_filtered=client_side_filtered
    )
