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

"""Define the batch-execution payloads and the snippet-selection logic behind them.

The ATW batch endpoints need the merge rule, the batch-level form fields, and the
per-item dispatch sequence; none of those touch request or response shape, so they
live here rather than in the router. The payloads travel with them because
:mod:`app.extensions.apps.atw.models` is loaded by the Alembic plugin-discovery loader
and may import only from ``app.core`` — these models need the app framework's
field types, the snippets schema vocabulary, and the tasks-service status enum.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast

from fastapi import HTTPException
from pydantic import BaseModel, Field, UUID4

from app.core.requests import as_json_object, RemoteAPI
from app.core.utils.fields import (
    ARBITRARY_ARGS_SCHEMA,
    ArbitraryMapping,
    NonEmptyStr,
    UTCDatetime,
)
from app.extensions.apps.field_names import (
    EXECUTOR_HOST_FIELD_NAME,
    EXTRA_ARGS_FIELD_NAME,
    SCRIPT_PREVIEW_FIELD_NAME,
    SUDO_FIELD_NAME,
)
from app.extensions.apps.framework.schema import (
    AnyField,
    BoolField,
    HostField,
)
from app.extensions.apps.framework.script_helpers import execute_script
from app.extensions.apps.framework.script_source import (
    resolve_scripts,
    ScriptExecuteWrite,
    ScriptExecutionResponse,
)
from app.extensions.apps.labels import EXECUTION_HOST_LABEL
from app.extensions.snippets.script_source import snippet_source, SnippetScript
from app.tasks.models import TaskHistoryStatusEnum

__all__ = [
    "MAX_BATCH_SNIPPETS",
    "NON_SHAREABLE_FIELD_NAMES",
    "ATWBatchExecuteItemResponse",
    "ATWBatchExecuteItemWrite",
    "ATWBatchExecuteResponse",
    "ATWBatchExecuteWrite",
    "ATWIncidentExecutionResponse",
    "ATWMergedSchemaResponse",
    "ATWSnippetSchema",
    "ArbitraryMapping",
    "batch_execution_fields",
    "dispatch_batch_item",
    "fetch_task_history",
    "parameter_fields",
    "resolve_snippets",
    "shared_field_names",
]

logger = logging.getLogger(__name__)

ATW_HYDRATION_WARNING = (
    "Task history %s could not be hydrated for the ATW incident page"
)

MAX_BATCH_SNIPPETS = 50
"""Cap a batch selection.

The whole selection resolves in a single lookup, and each dispatched item then
adds an upstream call and a write, all in request order. The ceiling keeps one
request's cost bounded; it sits far above any realistic diagnostic selection.
"""

_MIN_SHARED_DECLARERS = 2
_SYNTHETIC_FIELD_NAMES = frozenset(
    {EXECUTOR_HOST_FIELD_NAME, SUDO_FIELD_NAME, SCRIPT_PREVIEW_FIELD_NAME}
)
NON_SHAREABLE_FIELD_NAMES = frozenset({EXTRA_ARGS_FIELD_NAME})
"""Keep these fields per-snippet even when several snippets declare them identically.

Unlike ``_SYNTHETIC_FIELD_NAMES`` (fields excluded entirely because a caller
re-synthesises them), these are ordinary per-snippet fields that must never be
promoted to the shared section: extra args are snippet-specific CLI flags, so
merging them would silently apply one snippet's flags to another's command.
"""


async def resolve_snippets(filenames: Sequence[str]) -> dict[str, SnippetScript]:
    """Resolve several snippet filenames against the shared snippet source at once.

    The batch entry point the ATW call sites resolve their selections through,
    binding them to the framework's single resolution entry point so they inherit
    its traversal guard, order-preserving dedup, and one-query batch load. The
    result carries only the filenames that resolved, leaving each call site free to
    turn a missing filename into its own failure (a whole-request 404 for the
    schema form, a per-item error for the execute loop).

    :param filenames: The requested snippet filenames, possibly with duplicates.
    :return: A mapping of each resolved filename to its :class:`SnippetScript`.
    :raises HTTPBadRequestException: When any filename is unsafe or malformed,
        failing the whole request before any item is dispatched.
    """
    return await resolve_scripts(snippet_source, filenames)


class ATWSnippetSchema(BaseModel):
    """Represent the fields one selected snippet still owns after merging.

    :param snippet_filename: The snippet the remaining fields belong to.
    :param fields: The snippet's parameter fields that did not merge into the
        shared section.
    """

    snippet_filename: NonEmptyStr
    fields: list[AnyField]


class ATWMergedSchemaResponse(BaseModel):
    """Represent the execution form for a batch of selected snippets.

    A purpose-built DTO rather than a plain
    :class:`~app.extensions.apps.framework.schema.AppSchema`: the renderer needs to map
    every non-shared field back to the snippet that declared it, and an
    ``AppSchema`` section carries only a display title.

    :param shared: The batch-level execution fields followed by every parameter
        the selection declares identically, excluding ``NON_SHAREABLE_FIELD_NAMES``
        (e.g. Extra Args), which stay per-snippet even when every item declares
        them.
    :param per_snippet: The remaining per-snippet fields, in request order.
    """

    shared: list[AnyField]
    per_snippet: list[ATWSnippetSchema]


class ATWBatchExecuteItemWrite(BaseModel):
    """Define one snippet execution within a batch.

    :param snippet_filename: The snippet to execute.
    :param args: Per-snippet arguments keyed by frontmatter parameter name;
        they override same-named entries in the batch's ``shared_args``.
    """

    snippet_filename: NonEmptyStr
    args: dict[str, Any] = Field(default={}, json_schema_extra=ARBITRARY_ARGS_SCHEMA)


class ATWBatchExecuteWrite(BaseModel):
    """Define the batch-execute payload for one incident.

    :param executor_host: The executor every item in the batch runs on.
    :param sudo: The sudo choice applied to every item; snippets whose sudo
        option is not optional ignore it.
    :param shared_args: Arguments offered to every item, filtered per snippet to
        the parameters that snippet declares. ``NON_SHAREABLE_FIELD_NAMES`` (e.g.
        Extra Args) are never applied from ``shared_args``, even for a snippet
        that declares a field with that name.
    :param items: The snippets to execute, at least one and at most
        ``MAX_BATCH_SNIPPETS``.
    """

    executor_host: NonEmptyStr = Field(title=EXECUTION_HOST_LABEL)
    sudo: bool = False
    shared_args: dict[str, Any] = Field(
        default={}, json_schema_extra=ARBITRARY_ARGS_SCHEMA
    )
    items: list[ATWBatchExecuteItemWrite] = Field(
        min_length=1, max_length=MAX_BATCH_SNIPPETS
    )


class ATWBatchExecuteItemResponse(BaseModel):
    """Describe the outcome of one item in a batch execution.

    :param snippet_filename: The snippet this item requested.
    :param task_name: The Tasks-API task name the snippet dispatched under, when
        the dispatch itself succeeded.
    :param task_history_id: The created task-history id, when the Tasks API
        returned one.
    :param error: The failure detail when the item did not complete — a message
        or a validation-error list, depending on what rejected it.
    """

    snippet_filename: NonEmptyStr
    task_name: NonEmptyStr | None = None
    task_history_id: int | None = None
    error: str | list[ArbitraryMapping] | None = None


class ATWBatchExecuteResponse(BaseModel):
    """Collect every item's outcome for one batch execution.

    Partial success lives in the body: the request is created (``201``) whenever
    the incident resolves, and each item carries its own dispatch result or error.

    :param items: One entry per requested item, in request order.
    """

    items: list[ATWBatchExecuteItemResponse]


class ATWIncidentExecutionResponse(BaseModel):
    """Represent one recorded incident execution with live task status and snippet title.

    The task-status fields are ``None`` when the Tasks API could not be reached
    for that row; the locally-recorded fields are always present.

    :param id: The execution row's UUID primary key.
    :param snippet_filename: The executed snippet's filename.
    :param snippet_title: The snippet's current display title, falling back to its
        filename when its metadata title is missing or blank. ``None`` when the
        snippet cannot be resolved. Defaults to ``None``.
    :param task_history_id: The tasks-service execution this row references.
    :param created_at: When the execution was recorded.
    :param task_status: The upstream execution status.
    :param failure_reason: The upstream failure reason, carried verbatim.
        ``None`` when the run did not fail or the reason is unknown.
        Defaults to ``None``.
    :param started_at: When the upstream execution started.
    :param finished_at: When the upstream execution finished.
    :param has_logs: Whether the upstream execution has readable logs.
    :param masked_args: The command line the snippet ran with, credential values
        replaced by a fixed-width mask. ``None`` together with
        ``args_withheld=False`` means the execution recorded no arguments.
        Defaults to ``None``.
    :param args_withheld: Whether the arguments were suppressed because they
        could not be masked safely, distinguishing that from an execution that
        genuinely ran with none. Defaults to ``False``.
    """

    id: UUID4
    snippet_filename: str
    snippet_title: str | None = None
    task_history_id: int
    created_at: UTCDatetime
    task_status: TaskHistoryStatusEnum | None = None
    failure_reason: str | None = None
    started_at: UTCDatetime | None = None
    finished_at: UTCDatetime | None = None
    has_logs: bool | None = None
    masked_args: str | None = None
    args_withheld: bool = False


def parameter_fields(script: SnippetScript) -> list[AnyField]:
    """Return a snippet's parameter fields, without the synthetic execution ones.

    The per-snippet schema appends an executor-host selector, a sudo toggle, a
    script-preview pane, and — for a snippet that opts in — an Extra Args input to
    the frontmatter parameters. The first three are batch-level or presentational,
    so a merged batch form owns them once (or not at all) rather than repeating
    them per snippet; Extra Args stays because it is per-snippet by nature.

    :param script: The resolved snippet whose form schema is flattened.
    :return: Every parameter field the snippet declares, in schema order, plus the
        synthesized Extra Args field when the snippet opts into it.
    """
    return [
        field
        for section in snippet_source.build_form_schema(script).forms
        for field in section.fields
        if field.name not in _SYNTHETIC_FIELD_NAMES
    ]


def batch_execution_fields(scripts: list[SnippetScript]) -> list[AnyField]:
    """Build the batch-level execution fields the whole selection shares.

    These mirror :class:`ATWBatchExecuteWrite`'s own ``executor_host`` / ``sudo``
    inputs, so they are shared by construction rather than by the merge rule. One
    toggle drives the whole batch, so it starts checked only when *every*
    optional-sudo snippet in the selection would start checked on its own form —
    a selection that disagrees falls back to unchecked rather than silently
    escalating the snippets that default to no sudo.

    :param scripts: The resolved snippets the batch form covers.
    :return: The executor-host field, plus a sudo toggle when at least one
        selected snippet leaves sudo to the caller.
    """
    fields = [
        cast(
            "AnyField",
            HostField(
                name=EXECUTOR_HOST_FIELD_NAME,
                label=EXECUTION_HOST_LABEL,
                required=True,
            ),
        )
    ]
    optional_sudo = [script for script in scripts if script.snippet.sudo.is_optional]
    if optional_sudo:
        fields.append(
            cast(
                "AnyField",
                BoolField(
                    name=SUDO_FIELD_NAME,
                    label="Run with sudo",
                    default=all(
                        script.snippet.sudo.sudo_default for script in optional_sudo
                    ),
                    description=(
                        "Prepend sudo to the interpreter for every snippet in the "
                        "batch that leaves sudo optional."
                    ),
                ),
            )
        )
    return fields


def shared_field_names(declarations: dict[str, list[AnyField]]) -> set[str]:
    """Return the parameter names that merge into the batch's shared section.

    A parameter merges only when two or more selected snippets declare it *and*
    every declaration serialises identically — the wire form is what the renderer
    consumes, so byte-identity there is the sharing contract. Cosmetically similar
    but differing declarations (a per-product default, a required-vs-optional
    divergence) stay per-snippet, where they mean different things. A name in
    ``NON_SHAREABLE_FIELD_NAMES`` never merges, however unanimous its declarations.

    :param declarations: Every declaration of each parameter name, keyed by name.
    :return: The names whose declarations are unanimous across two or more snippets.
    """
    shared = set()
    for name, fields in declarations.items():
        if name in NON_SHAREABLE_FIELD_NAMES or len(fields) < _MIN_SHARED_DECLARERS:
            continue
        dumps = [field.model_dump(by_alias=True) for field in fields]
        if all(dump == dumps[0] for dump in dumps[1:]):
            shared.add(name)
    return shared


async def dispatch_batch_item(
    body: ATWBatchExecuteWrite,
    item: ATWBatchExecuteItemWrite,
    script: SnippetScript,
    tasks_api: RemoteAPI,
    *,
    execution_task_name: str | None = None,
) -> ScriptExecutionResponse:
    """Narrow the shared args to one already-resolved batch item and dispatch it.

    Shared arguments are filtered to the parameters the snippet actually declares
    and excludes ``NON_SHAREABLE_FIELD_NAMES``, so a batch may offer a value no
    single snippet accepts (or one no snippet may share, like Extra Args), and the
    item's own ``args`` then override what remains. The snippet is resolved once
    for the whole batch by the caller and handed in, so a repeated filename costs
    one lookup.

    :param body: The batch payload supplying the executor host, sudo choice, and
        shared arguments.
    :param item: The item naming its own argument overrides.
    :param script: The snippet resolved for ``item.snippet_filename``.
    :param tasks_api: The authenticated Tasks API client.
    :param execution_task_name: ATW's proxy for this snippet's interpreter, resolved
        once per batch by the caller. ``None`` dispatches under the interpreter
        unchanged, which is the documented degradation when the proxy cannot be used.
    :return: The dispatched task name, the created task-history id (``None`` when
        the Tasks API returned none), and the resolved snippet filename.
    :raises HTTPException: When the snippet's arguments fail validation, it is not
        executable, or the Tasks API returns an error status.
    :raises OSError: Propagated from ``execute_script`` when the Tasks API
        transport itself fails.
    """
    declared = {
        field.name
        for field in parameter_fields(script)
        if field.name not in NON_SHAREABLE_FIELD_NAMES
    }
    args = {name: value for name, value in body.shared_args.items() if name in declared}
    args.update(item.args)
    return await execute_script(
        snippet_source,
        script,
        ScriptExecuteWrite(executor_host=body.executor_host, sudo=body.sudo, args=args),
        tasks_api,
        execution_task_name=execution_task_name,
    )


async def fetch_task_history(
    tasks_api: RemoteAPI, task_history_id: int
) -> dict[str, Any]:
    """Fetch one task-history row, degrading an upstream failure to no data.

    Mirrors the topology page's fan-out over the same endpoint, but keeps the page
    alive when a single row cannot be hydrated: a deleted or unreachable execution
    should blank that row's live fields, not fail the whole listing.

    :param tasks_api: The authenticated Tasks API client.
    :param task_history_id: The execution to look up.
    :return: The upstream task-history payload, or an empty mapping on failure.
    """
    try:
        return as_json_object(await tasks_api.get(f"/history/{task_history_id}"))
    except (HTTPException, OSError):
        logger.warning(ATW_HYDRATION_WARNING, task_history_id, exc_info=True)
        return {}
