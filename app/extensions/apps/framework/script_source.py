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

"""Define the ``ScriptSource`` seam for script-backed task apps.

A script-backed task app has no single create model: each script carries its own
dynamically-synthesised form, and its surface is script-centric (listing,
per-script form schema, execute, history) rather than the model-first ``POST /``
create envelope. :class:`ScriptSource` captures the hooks
:func:`~app.extensions.apps.framework.api.derive_script_routes` needs to derive that
surface, so a :class:`~app.extensions.apps.framework.apps.TaskExecutionApp` can declare
``script_source=`` instead of hand-wiring the routing and execution.

The framework programs against the structural :class:`ScriptProtocol` and never
imports a concrete script type; the concrete type, the listing source (disk or
DB), the artifact-URL salt, and the execution-meta shape are all supplied by the
consumer.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Generic, Protocol, runtime_checkable

from fastapi import Query
from pydantic import BaseModel, Field
from typing_extensions import TypeVar

from app.core.exceptions import HTTPBadRequestException, HTTPNotFoundException
from app.core.pagination import Pagination
from app.core.utils.fields import ARBITRARY_ARGS_SCHEMA, NonEmptyStr
from app.core.utils.iterators import unique_everseen
from app.extensions.apps.framework.schema import AppSchema
from app.extensions.apps.labels import EXECUTION_HOST_LABEL

__all__ = [
    "ARBITRARY_ARGS_SCHEMA",
    "ScriptExecuteWrite",
    "ScriptExecutionResponse",
    "ScriptPreviewResponse",
    "ScriptProtocol",
    "ScriptSource",
    "make_script_dep",
    "resolve_scripts",
]


@runtime_checkable
class ScriptProtocol(Protocol):
    """Describe the structural surface ``derive_script_routes`` needs from a script.

    The framework consumes only these three members, so any concrete script type
    (``BaseSnippet`` and its subclasses, or a future disk/DB-backed script) that
    exposes them can back a :class:`ScriptSource` without the framework importing
    it.

    :ivar filename: The script's filename relative to the script directory,
        carried in the ``snippet_filename`` query parameter.
    """

    filename: str

    @property
    def execution_task_name(self) -> str:
        """Return the Tasks-API task name the script executes under."""

    def get_execution_model(self) -> type[BaseModel]:
        """Return the dynamic model validating the script's execution arguments."""


S = TypeVar("S", bound=ScriptProtocol)
Q = TypeVar("Q", default=Any)


class ScriptExecuteWrite(BaseModel):
    """Define the JSON body for ``POST .../snippet/execute``.

    The per-script frontmatter arguments go in ``args`` and are validated against
    the script's dynamic execution model, while ``executor_host`` and ``sudo`` are
    execution-level inputs the consumer's ``build_execution_meta`` reads.

    :param executor_host: The hostname of the executor that will run the script.
    :param sudo: Whether to invoke the script with sudo. The consumer's
        ``build_execution_meta`` decides whether the script honours it.
    :param args: Per-parameter arguments keyed by parameter name, validated
        against the script's dynamic execution model.
    """

    executor_host: NonEmptyStr = Field(title=EXECUTION_HOST_LABEL)
    sudo: bool = False
    args: dict[str, object] = Field(default={}, json_schema_extra=ARBITRARY_ARGS_SCHEMA)


class ScriptExecutionResponse(BaseModel):
    """Represent the response from the script execute endpoint.

    :param task_name: The Tasks-API task name the script executed under.
    :param task_id: The id of the task-history row the Tasks API created, when the
        upstream response carries one.
    :param snippet_filename: The filename of the executed script.
    """

    task_name: NonEmptyStr
    task_id: int | None = None
    snippet_filename: NonEmptyStr


class ScriptPreviewResponse(BaseModel):
    """Represent the backend response for the preview endpoint.

    :param content: The full text content of the snippet file (preamble,
        frontmatter, and body concatenated).
    :param language: A JS syntax-highlighter language identifier derived
        from the snippet's MIME type (for example, ``"bash"`` or
        ``"plaintext"``).
    :param is_truncated: Whether the preview was truncated to fit
        within the configured per-file character or line limit.
    """

    content: str
    language: str
    is_truncated: bool


@dataclass(frozen=True, slots=True)
class ScriptSource(Generic[S, Q]):
    """Carry the hooks ``derive_script_routes`` derives a script app's surface from.

    The framework owns the contract and the route derivation; the consumer supplies
    the concrete script type, the listing source, the per-script form synthesis, and
    the execution-meta assembly (closing over its own artifact-URL salt and meta
    shape). ``build_execution_meta`` receives the script and the validated
    :class:`ScriptExecuteWrite`, so a consumer reads ``executor_host`` / ``sudo`` /
    ``args`` from one place.

    :param script_dir: The directory the scripts live in (the static-mount target and
        the listing root the consumer's hooks close over).
    :param load_script: Resolve a single script by filename; raise
        :class:`~app.core.exceptions.HTTPNotFoundException` when it is absent.
    :param list_scripts: Return a page of scripts plus the filtered total. Receives
        the resolved list-query value of type ``Q`` (a Core
        :class:`~app.core.db.list_query.ListQuery` for push-down sources, an
        :class:`~app.core.db.in_memory_list_query.InMemoryListQuery` for in-memory
        sources) and the pagination window. Either argument is ``None`` when the
        route derives no query capability: ``(None, None)`` lists every script
        (non-paginated route), ``(None, pagination)`` returns the pagination slice of
        the full set with the full-set total (paginated, no spec), and
        ``(query, pagination)`` returns the filtered/sorted page with its filtered
        total (paginated, spec).
    :param load_scripts: The optional batch loader resolving several filenames in
        one round trip, returning a filename-keyed mapping of only the scripts it
        resolved (an absent key means unresolved). When ``None`` the framework's
        :func:`resolve_scripts` falls back to looping ``load_script`` (see there),
        so a source that does not opt in keeps working unchanged.
    :param build_form_schema: Synthesise a per-script :class:`AppSchema` from the
        script's frontmatter parameters.
    :param build_execution_meta: Assemble the execution-meta model the framework
        posts to the Tasks API, from the script and the validated request body.
    :param list_response: Project a script into its list-row response model.
    :param static_schema: The optional plugin-level schema served at ``GET /schema``;
        when ``None`` the route is not registered.
    :param list_response_model: The optional response model typing the derived
        ``GET /`` as ``list[list_response_model]``; when ``None`` the list route
        stays untyped (back-compatible — a source that does not opt in keeps the
        original untyped list).
    :param list_query_dep: An optional source-supplied request-boundary dependency
        yielding the ``Q`` handed to ``list_scripts``. Set it when the resource adds
        filter parameters on top of sort and search: the dependency composes the Core
        one (built from the app's spec) with its own ``Query`` params, so the spec
        stays the single authority for the sortable allowlist while the route also
        carries the resource's base restrictions. When ``None`` (default) the
        framework builds the dependency from the app's spec directly. Either way the
        app must declare a spec, or the derived route exposes no query at all.
    :param in_memory_list_query: Whether the source materializes its whole set and
        applies sort/search/pagination in-process (a disk-backed source) rather than
        pushing them down to SQL. Selects which list-query dependency the framework
        builds for the app's :class:`~app.core.db.list_query.ListQuerySpec` — the
        in-memory dep (:func:`~app.core.db.deps.make_in_memory_list_query_dep`)
        when ``True``, the SQL dep (:func:`~app.core.db.deps.make_list_query_dep`)
        otherwise. Meaningful only on a source with no ``list_query_dep``, which
        supersedes it.
    """

    script_dir: Path
    load_script: Callable[[str], Awaitable[S]]
    list_scripts: Callable[
        [Q | None, Pagination | None], Awaitable[tuple[Sequence[S], int]]
    ]
    build_form_schema: Callable[[S], AppSchema]
    build_execution_meta: Callable[[S, ScriptExecuteWrite], BaseModel]
    list_response: Callable[[S], BaseModel]
    static_schema: AppSchema | None = None
    list_response_model: type[BaseModel] | None = None
    load_scripts: Callable[[Sequence[str]], Awaitable[Mapping[str, S]]] | None = None
    list_query_dep: Callable[..., Q] | None = None
    in_memory_list_query: bool = False

    def __post_init__(self) -> None:
        """Reject a source whose two list-query dependency knobs disagree.

        The framework prefers ``list_query_dep`` when both are set, so
        ``in_memory_list_query`` would be dropped without a word. The spec-coupled
        half of this capability cannot be checked here — a source cannot see the app's
        spec — and lives on ``TaskExecutionApp`` instead.

        :raises ValueError: When ``in_memory_list_query`` is set on a source that also
            supplies a ``list_query_dep``.
        """
        if self.list_query_dep is not None and self.in_memory_list_query:
            raise ValueError(
                "ScriptSource: list_query_dep supersedes the framework's in-memory "
                "dependency; set in_memory_list_query only on a source with no "
                "list_query_dep"
            )


def _validate_script_filename(filename: str) -> None:
    """Reject a ``snippet_filename`` that could resolve outside the script directory.

    The framework cannot trust a consumer's ``load_script`` to validate the path
    itself (a disk-backed loader may join the raw value straight onto
    ``script_dir``), so reject directory traversal at the seam — mirroring the
    snippets plugin's path-safety guard — before any ``load_script`` lookup runs.

    :param filename: The raw ``snippet_filename`` query value.
    :raises HTTPBadRequestException: When the filename uses a Windows separator, is
        absolute, or contains an empty / ``.`` / ``..`` path component.
    """
    if "\\" in filename or PurePosixPath(filename).is_absolute():
        raise HTTPBadRequestException(detail=f"Invalid script filename: {filename!r}")
    if any(part in ("", ".", "..") for part in filename.split("/")):
        raise HTTPBadRequestException(detail=f"Invalid script filename: {filename!r}")


def make_script_dep(source: ScriptSource[S]) -> Callable[[str], Awaitable[S]]:
    """Return a dependency resolving the ``snippet_filename`` query param to a script.

    Mirrors the snippets plugin's ``SnippetDep``: the per-script routes carry the
    filename in a ``snippet_filename`` query parameter (never a path segment, which
    would shadow ``GET /`` and ``GET /schema``), and resolution delegates to
    ``source.load_script`` so a missing script surfaces as that loader's 404.

    :param source: The script source whose ``load_script`` resolves the filename.
    :return: A FastAPI dependency returning the resolved script.
    """

    async def _get_script(
        snippet_filename: Annotated[
            str,
            Query(description="Script filename (relative path under the script dir)."),
        ],
    ) -> S:
        _validate_script_filename(snippet_filename)
        return await source.load_script(snippet_filename)

    return _get_script


async def resolve_scripts(
    source: ScriptSource[S], filenames: Sequence[str]
) -> dict[str, S]:
    """Resolve several script filenames in one pass, keyed by filename.

    The single entry point every batch caller routes through, so the seam's
    path-safety guard runs on every filename before any lookup regardless of
    whether the source opted into a batch loader. Filenames are deduplicated
    order-preservingly, so a repeated filename is looked up once. A source that
    sets ``load_scripts`` resolves the whole selection in one round trip;
    otherwise the framework loops ``load_script``, treating that loader's 404 as
    an unresolved filename rather than propagating it.

    The result carries only the filenames that resolved, so the caller decides
    the failure policy: a schema route can raise its whole-request 404 for the
    first missing key, while an execute route can turn a missing key into a
    per-item error. Traversal is still fatal — an unsafe filename raises before
    any lookup, matching the single-script dependency. An empty selection
    resolves to an empty mapping without touching either loader.

    :param source: The script source whose loader(s) resolve the filenames.
    :param filenames: The requested filenames, possibly with duplicates.
    :return: A mapping of each resolved filename to its script; unresolved
        filenames are absent.
    :raises HTTPBadRequestException: When any filename fails the traversal guard.
    """
    unique = list(unique_everseen(filenames))
    if not unique:
        return {}
    for filename in unique:
        _validate_script_filename(filename)
    if source.load_scripts is not None:
        return dict(await source.load_scripts(unique))
    resolved: dict[str, S] = {}
    for filename in unique:
        try:
            resolved[filename] = await source.load_script(filename)
        except HTTPNotFoundException:
            continue
    return resolved
