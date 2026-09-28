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

"""Build a disk-backed ``BaseSnippet`` app over the framework ``ScriptSource`` seam.

A ``script``-flavored scaffolded app loads its catalogue from disk (like ``dipper``)
yet exposes it through the framework's arg-validating ``ScriptSource`` seam (like
``snippets``) — a combination neither reference app implements. This factory owns
that bridge once so the generated app stays thin:

* ``from_path`` disk loading with a 404 guard,
* the args-only ``get_execution_model`` twin that lets ``derive_script_routes``
  validate ``body.args`` even though the engine model requires the aliased
  ``-hostname-`` field the framework never puts there,
* frontmatter-driven form synthesis reusing the snippets app's ``field_for``,
* server-side gate enforcement reusing ``evaluate_snippet_gates``, and
* meta assembly via the shared ``script_helpers``.

It lives in the apps layer rather than the framework because it reuses the snippets
app's ``field_for`` / ``evaluate_snippet_gates``: a framework home would invert the
framework's no-dependency-on-apps direction (the framework must not import an app).
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, create_model, Field
from sqlalchemy import column

from app.core.db.in_memory_list_query import InMemoryListQueryApplier
from app.core.db.list_query import ListQuerySpec
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.extensions.apps.field_names import EXECUTOR_HOST_FIELD_NAME, SUDO_FIELD_NAME
from app.extensions.apps.framework.list_query import in_memory_list_scripts
from app.extensions.apps.framework.schema import (
    AppSchema,
    BoolField,
    Column,
    EXECUTION_HOST_LABEL,
    FormSection,
    HostField,
    ListView,
)
from app.extensions.apps.framework.script_helpers import (
    build_artifact_download_url,
    build_execution_meta,
)
from app.extensions.apps.framework.script_source import ScriptExecuteWrite, ScriptSource
from app.extensions.snippets.config import SnippetSudoOption
from app.extensions.snippets.models.snippet import (
    BaseSnippet,
    BaseSnippetArgs,
    EXECUTOR_HOSTS_INPUT_NAME,
    SnippetExecutionMeta,
)
from app.extensions.snippets.schema import evaluate_snippet_gates, field_for

__all__ = [
    "DISK_SCRIPT_LIST_QUERY_SPEC",
    "DiskScriptListRow",
    "build_disk_script_source",
]

#: Every disk-backed source lists the same shape — a flat directory of scripts, one row
#: per file — so the allowlist is fully determined by ``_DiskScript``'s attributes and is
#: shared rather than restated per app. ``filename`` is the tie-breaker because a
#: directory cannot hold two entries under one name, which makes it unique by
#: construction where no surrogate id exists.
DISK_SCRIPT_LIST_QUERY_SPEC = ListQuerySpec(
    sortable={
        "filename": column("filename"),
        "execution_task_name": column("execution_task_name"),
    },
    default_sort="filename",
    tie_breaker=column("filename"),
    searchable=(column("filename"),),
)


class DiskScriptListRow(BaseModel):
    """Project a disk-backed script into its list-row response.

    :param filename: The script filename, used as its API identifier.
    :param task_name: The Tasks-API task name the script executes under.
    """

    filename: str
    task_name: str


@lru_cache(maxsize=256)
def _args_only_model(engine_model: type[BaseSnippetArgs]) -> type[BaseSnippetArgs]:
    """Return an args-only twin of a script's execution model.

    ``derive_script_routes`` validates ``body.args``, which never carries the
    ``-hostname-`` executor field the engine model requires. The twin redeclares
    ``executor_host`` as optional (``exclude=True`` is inherited, so it still never
    leaks into ``model_dump`` / ``to_args_string``) so ``model_validate`` succeeds
    without it; the execute hook re-attaches the real host via ``model_construct``.

    :param engine_model: The script's dynamic execution model.
    :return: A subclass whose ``executor_host`` is optional.
    """
    return create_model(
        "DiskScriptArgsOnly",
        __base__=engine_model,
        executor_host=(
            str | None,
            Field(
                default=None, validation_alias=EXECUTOR_HOSTS_INPUT_NAME, exclude=True
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _DiskScript:
    """Adapt a disk-loaded :class:`BaseSnippet` to the framework's ``ScriptProtocol``.

    Wraps the loaded script and exposes it as ``.snippet`` so the form and meta
    hooks reuse the engine's parameter/interpreter machinery, while
    ``get_execution_model`` hands the framework the args-only twin.

    :param snippet: The wrapped, disk-loaded script.
    """

    snippet: BaseSnippet

    @property
    def filename(self) -> str:
        """Return the script's filename, carried in ``snippet_filename``."""
        return self.snippet.filename

    @property
    def execution_task_name(self) -> str:
        """Return the Tasks-API task name the script executes under."""
        return self.snippet.execution_task_name

    def get_execution_model(self) -> type[BaseSnippetArgs]:
        """Return the args-only execution model the framework validates against.

        :return: The args-only twin of the wrapped script's execution model.
        """
        return _args_only_model(self.snippet.get_execution_model())


def _list_view() -> ListView:
    """Return the shared list view describing a disk-script listing.

    :return: The list view with the filename and task-name columns.
    """
    return ListView(
        columns=[
            Column(key="filename", label="Filename", sortable=True),
            Column(key="task_name", label="Task"),
        ]
    )


def _build_form_schema(script: _DiskScript, *, name: str) -> AppSchema:
    """Build the per-script form schema from the script's frontmatter parameters.

    Mirrors the snippets ``build_snippet_schema`` shape: one section per parameter
    group (or a single ``"Parameters"`` section) built from ``field_for``, plus a
    trailing ``"Execution"`` section carrying the executor-host field and — for a
    sudo-optional or sudo-always script — a sudo toggle. The record names follow
    the same mirror: this form creates a *run* of the script rather than the
    script, so they are fixed instead of derived from the script's title.

    :param script: The disk-loaded script whose parameters drive the form.
    :param name: The app name recorded on the synthesised schema.
    :return: The per-script form schema.
    """
    snippet = script.snippet
    parameter_sections = {}
    for parameter in snippet.validated_parameters.visible_parameters:
        section_title = parameter.group or "Parameters"
        parameter_sections.setdefault(section_title, []).append(field_for(parameter))
    forms = [
        FormSection(title=title, fields=fields)
        for title, fields in parameter_sections.items()
    ]

    execution_fields = [
        HostField(
            name=EXECUTOR_HOST_FIELD_NAME, label=EXECUTION_HOST_LABEL, required=True
        )
    ]
    if snippet.sudo.is_optional:
        execution_fields.append(
            BoolField(
                name=SUDO_FIELD_NAME,
                label="Run with sudo",
                default=snippet.sudo.sudo_default,
                description="Prepend sudo to the interpreter when the script is executed.",
            )
        )
    elif snippet.sudo is SnippetSudoOption.ALWAYS:
        execution_fields.append(
            BoolField(
                name=SUDO_FIELD_NAME,
                label="Run with sudo",
                default=True,
                description="This script is configured to always run with sudo.",
            )
        )
    forms.append(FormSection(title="Execution", fields=execution_fields))

    return AppSchema(
        name=name,
        display_name=snippet.title,
        item_display_name="run",
        description=snippet.description or None,
        forms=forms,
        list_view=_list_view(),
    )


def _build_execution_meta(
    script: _DiskScript, body: ScriptExecuteWrite, *, artifact_type: str
) -> SnippetExecutionMeta:
    """Assemble the execution meta for a disk-backed script, enforcing its gates.

    The framework already validated ``body.args`` against the args-only model and
    replaced them with the coerced dump (keyed by python attribute name,
    ``executor_host`` excluded), so the full engine model is rebuilt with
    ``model_construct`` — never re-validated, whose alias-keyed fields would reject
    the attribute-keyed dump. When the script's sudo option is optional, the user's
    ``sudo`` choice is re-attached by attribute name (the model keys sudo on its
    ``-sudo-`` alias the plain form input never satisfies).

    :param script: The script whose execution meta is assembled.
    :param body: The validated execute request, ``args`` carrying the coerced dump.
    :param artifact_type: The artifact-type discriminator recorded in the signed URL.
    :return: The execution meta the framework posts to the Tasks API.
    :raises HTTPBadRequestException: When the script declares no runnable interpreter,
        carries invalid frontmatter parameters, or no ``SNIPPETS_BASE_URL`` /
        ``BASE_URL`` is configured for the signed URL.
    :raises HTTPUnprocessableEntityException: When a script field gate (visibility,
        ``requires`` or ``forbidden``) rejects the submitted args.
    """
    snippet = script.snippet
    interpreter = snippet.execution_interpreter
    if interpreter is None:
        raise HTTPBadRequestException(
            detail=f"Script {snippet.filename!r} declares no runnable interpreter."
        )
    if not snippet.can_execute:
        reasons = "; ".join(snippet.validated_parameters.errors)
        raise HTTPBadRequestException(
            detail=f"Script {snippet.filename!r} has invalid frontmatter parameters: "
            f"{reasons}"
        )
    construct_args = dict(body.args)
    if snippet.sudo.is_optional:
        construct_args[BaseSnippetArgs.sudo_field] = body.sudo
    execution_args = snippet.get_execution_model().model_construct(
        executor_host=body.executor_host, **construct_args
    )
    gate_failures = evaluate_snippet_gates(snippet, execution_args)
    if gate_failures:
        raise HTTPUnprocessableEntityException(detail=gate_failures)
    snippet_source = build_artifact_download_url(
        None,
        artifact_type=artifact_type,
        filename=snippet.filename,
        md5_digest=snippet.md5_digest,
    )
    return build_execution_meta(
        snippet,
        execution_args,
        interpreter=interpreter,
        snippet_source=snippet_source,
        snippet_filename=snippet.filename,
    )


def _list_response(script: _DiskScript) -> DiskScriptListRow:
    """Project a disk-backed script into its list-row response.

    :param script: The disk-loaded script to project.
    :return: The list-row response for the script.
    """
    return DiskScriptListRow(
        filename=script.filename, task_name=script.execution_task_name
    )


def build_disk_script_source(
    *,
    script_dir: Path,
    script_cls: type[BaseSnippet],
    artifact_type: str,
    name: str,
    display_name: str,
    item_display_name: str | None = None,
    item_display_name_plural: str | None = None,
    list_query_spec: ListQuerySpec = DISK_SCRIPT_LIST_QUERY_SPEC,
) -> ScriptSource[_DiskScript]:
    """Wire a disk-backed ``BaseSnippet`` subclass into a framework ``ScriptSource``.

    :param script_dir: The directory the scripts live in (the listing root and the
        static-mount target); must match ``script_cls.BASE_DIR``.
    :param script_cls: The ``BaseSnippet`` subclass whose ``from_path`` loads a script.
    :param artifact_type: The artifact-type discriminator recorded in each signed
        download URL (register it in the app's ``artifact_base_dirs``).
    :param name: The app name recorded on the derived schemas.
    :param display_name: The plugin-level display name served at ``GET /schema``.
    :param item_display_name: The name for one record the app-level create form
        produces, in mid-sentence form. Passed through as-is, so ``None`` leaves
        ``AppSchema`` to default it from ``display_name``. Names the app's own
        record, not the per-script one, which is always a ``run``. Defaults to
        ``None``.
    :param item_display_name_plural: The name for several such records, defaulted
        by the same route. Defaults to ``None``.
    :param list_query_spec: The sort/search allowlist the source replays in-process on
        every list call; its public keys must name ``_DiskScript`` attributes
        (``filename``, ``execution_task_name``). Defaults to the shared
        :data:`DISK_SCRIPT_LIST_QUERY_SPEC`, which suits any flat script directory;
        pass the same value as the app's ``list_query_spec``, since the app's copy is
        what the derived route exposes.
    :return: A ``ScriptSource`` carrying the disk-backed listing, form, and execute
        hooks for ``derive_script_routes``.
    :raises TypeError: When ``list_query_spec`` is not a ``ListQuerySpec``. Checked
        here, at wiring time, because the source replays it on every list call; a
        ``None`` slipped past the annotation would otherwise fail per request.
    :raises pydantic.ValidationError: When ``display_name`` or either record name
        is empty, from the unguarded ``static_schema`` construction below. Also a
        wiring-time failure: the schema is built once, when the source is.
    """
    if not isinstance(list_query_spec, ListQuerySpec):
        raise TypeError(
            "build_disk_script_source: list_query_spec must be a ListQuerySpec, got "
            f"{type(list_query_spec).__name__}; the source replays it on every list call"
        )

    async def load_script(filename: str) -> _DiskScript:
        if not (script_dir / filename).is_file():
            raise HTTPNotFoundException(detail=f"Script {filename!r} not found.")
        return _DiskScript(await script_cls.from_path(filename, update_meta=True))

    async def materialize_scripts() -> list[_DiskScript]:
        return [
            _DiskScript(await script_cls.from_path(path.name, update_meta=True))
            for path in sorted(script_dir.iterdir())
            if path.is_file()
        ]

    list_scripts = in_memory_list_scripts(
        materialize_scripts, InMemoryListQueryApplier(list_query_spec)
    )

    def build_form_schema(script: _DiskScript) -> AppSchema:
        return _build_form_schema(script, name=name)

    def build_meta(
        script: _DiskScript, body: ScriptExecuteWrite
    ) -> SnippetExecutionMeta:
        return _build_execution_meta(script, body, artifact_type=artifact_type)

    return ScriptSource(
        script_dir=script_dir,
        load_script=load_script,
        list_scripts=list_scripts,
        build_form_schema=build_form_schema,
        build_execution_meta=build_meta,
        list_response=_list_response,
        static_schema=AppSchema(
            name=name,
            display_name=display_name,
            item_display_name=item_display_name,
            item_display_name_plural=item_display_name_plural,
            list_view=_list_view(),
        ),
        list_response_model=DiskScriptListRow,
        in_memory_list_query=True,
    )
