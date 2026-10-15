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

"""Share the keying-agnostic bodies the script-backed task apps re-roll.

Below the framework's filename-keyed :class:`~app.extensions.apps.framework.script_source.ScriptSource`
seam, ``snippets``, ``dipper``, and ``alert_troubleshooting`` each hand-roll the
same four things: a script preview, a :class:`~app.extensions.snippets.models.snippet.SnippetExecutionMeta`
from resolved inputs, the signed artifact-download URL, and the "POST
``/execute/{name}`` then read the created task id" execute tail. Each helper here
takes the *already-resolved* script / meta, so every app keeps its own
script-resolution keying while sharing the body.

:func:`execute_script` sits one level above those: it is the whole validate →
coerce → build-meta → dispatch sequence the derived ``POST /snippet/execute``
route performs, factored out so a caller that dispatches several scripts in one
request (the ATW batch endpoint) drives the identical sequence instead of
re-rolling its subtle arg-coercion contract.
"""

from typing import TypeVar

from fastapi import Request
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPUnprocessableEntityException,
)
from app.core.requests import RemoteAPI
from app.core.security import crypto_timestamp_serializer
from app.extensions.apps.framework.script_source import (
    ScriptExecuteWrite,
    ScriptExecutionResponse,
    ScriptPreviewResponse,
    ScriptProtocol,
    ScriptSource,
)
from app.extensions.artifact_constants import ARTIFACT_DOWNLOAD_SALT
from app.extensions.config import warn_if_base_url_lacks_root_path
from app.extensions.deps import get_base_url, task_path
from app.extensions.snippets.config import snippets_settings, SnippetSudoOption
from app.extensions.snippets.masking import masked_args_for
from app.extensions.snippets.models.snippet import (
    BaseSnippet,
    BaseSnippetArgs,
    SnippetExecutionMeta,
)
from app.extensions.snippets.utils import (
    guess_mime_type,
    mime_type_to_highlighter_language,
)

__all__ = [
    "build_artifact_download_url",
    "build_execution_meta",
    "build_script_preview",
    "execute_script",
    "post_task_execution",
]

S = TypeVar("S", bound=ScriptProtocol)


async def build_script_preview(script: BaseSnippet) -> ScriptPreviewResponse:
    """Build a preview response from a resolved script.

    :param script: The resolved script whose preview is built.
    :return: The preview content with a MIME-derived highlighter-language hint.
    :raises UnicodeDecodeError: When the script contains non-UTF-8 bytes.
        Propagated so each caller raises its own app-specific 422 detail.
    """
    preview = await script.get_preview()
    return ScriptPreviewResponse(
        content=preview.full_content,
        language=mime_type_to_highlighter_language(guess_mime_type(script.path)),
        is_truncated=preview.is_truncated,
    )


def build_execution_meta(
    script: BaseSnippet,
    execution_args: BaseSnippetArgs,
    *,
    interpreter: str,
    snippet_source: str,
    snippet_filename: str,
    sudo_default: bool = False,
) -> SnippetExecutionMeta:
    """Assemble the sudo-resolved execution meta from resolved inputs.

    :param script: The resolved script supplying the checksum, requirements, and
        sudo option.
    :param execution_args: The validated execution arguments.
    :param interpreter: The interpreter to run the script under, prefixed with
        ``sudo`` when the script or the caller's sudo field opts in.
    :param snippet_source: The signed URL the executor downloads the artifact from.
    :param snippet_filename: The filename recorded in the meta (each caller keys
        it under its own scheme).
    :param sudo_default: The fallback when the arguments lack a sudo field.
    :return: The assembled execution meta.
    """
    if script.sudo is SnippetSudoOption.ALWAYS or getattr(
        execution_args, execution_args.sudo_field, sudo_default
    ):
        interpreter = f"sudo {interpreter}"
    return SnippetExecutionMeta(
        target=execution_args.executor_host,
        interpreter=interpreter,
        snippet_source=snippet_source,
        snippet_filename=snippet_filename,
        md5_checksum=script.md5_digest,
        args=execution_args.to_args_string(),
        requirements=script.requirements,
        masked_args=masked_args_for(execution_args),
    )


def build_artifact_download_url(
    request: Request | None,
    *,
    artifact_type: str,
    filename: str,
    md5_digest: str,
) -> str:
    """Return the signed ``/artifacts/download/{token}`` URL for an artifact.

    The download path is joined onto the base URL's own path rather than
    replacing it, so a base carrying the prefix PMM Extensions is served under keeps it.
    ``SNIPPETS_BASE_URL`` can be overridden after the startup check has run, so
    a value omitting that prefix is warned about here; the URL is well-formed
    either way and is still returned.

    :param request: The HTTP request whose host derives the base URL, or ``None``
        for the request-less source path, which falls back to the configured
        ``BASE_URL``.
    :param artifact_type: The artifact type recorded in the signed token.
    :param filename: The artifact filename recorded in the signed token.
    :param md5_digest: The artifact's MD5 digest recorded in the signed token.
    :return: The signed artifact-download URL.
    :raises HTTPBadRequestException: When ``request`` is ``None`` and neither
        ``SNIPPETS_BASE_URL`` nor ``BASE_URL`` is configured.
    """
    if request is None:
        base_url = snippets_settings.SNIPPETS_BASE_URL or settings.BASE_URL
        if base_url is None:
            raise HTTPBadRequestException(
                detail=(
                    "Snippet execution requires SNIPPETS_BASE_URL or BASE_URL to be set."
                ),
            )
    else:
        base_url = snippets_settings.SNIPPETS_BASE_URL or get_base_url(request)
    warn_if_base_url_lacks_root_path(
        snippets_settings.SNIPPETS_BASE_URL, "SNIPPETS_BASE_URL"
    )
    token = crypto_timestamp_serializer.dumps(
        {"type": artifact_type, "filename": filename, "md5": md5_digest},
        salt=ARTIFACT_DOWNLOAD_SALT,
    )
    base_path = base_url.path.rstrip("/")
    return str(base_url.replace(path=f"{base_path}/artifacts/download/{token}"))


async def post_task_execution(
    tasks_api: RemoteAPI, execution_task_name: str, meta: SnippetExecutionMeta
) -> int | None:
    """Send the meta-envelope to ``/execute/{name}`` and return the created task id.

    :param tasks_api: The authenticated Tasks API client.
    :param execution_task_name: The Tasks-API task name to execute under.
    :param meta: The execution meta posted inside the ``meta`` envelope.
    :return: The created task-history id, or ``None`` when the upstream response
        carries none.
    :raises HTTPUnprocessableEntityException: If ``execution_task_name`` is not a
        single plain URL path segment.
    :raises HTTPException: Propagated from ``tasks_api.post`` when the Tasks API
        returns an error status.
    """
    created = await tasks_api.post(
        f"/execute{task_path(execution_task_name)}",
        json={"meta": meta.model_dump(by_alias=True, exclude_none=True)},
    )
    return created.get("id") if isinstance(created, dict) else None


async def execute_script(
    source: ScriptSource[S],
    script: S,
    body: ScriptExecuteWrite,
    tasks_api: RemoteAPI,
    *,
    execution_task_name: str | None = None,
) -> ScriptExecutionResponse:
    """Validate the args, assemble the meta, and dispatch one script execution.

    ``body.args`` arrives keyed by the script's *frontmatter* parameter names, so
    it is validated against the script's dynamic execution model and the model's
    **coerced** dump (keyed by Python attribute name) is what reaches
    ``build_execution_meta`` — that hook rebuilds the engine model with
    ``model_construct`` and would reject a re-validated, alias-keyed payload.

    :param source: The script source whose ``build_execution_meta`` assembles the
        meta posted to the Tasks API.
    :param script: The already-resolved script to execute.
    :param body: The execute request carrying the executor host, sudo choice, and
        per-parameter arguments.
    :param tasks_api: The authenticated Tasks API client.
    :param execution_task_name: A task to dispatch under instead of the script's
        own ``execution_task_name``, for a caller that wraps the interpreter root
        in an app-owned proxy. ``None`` (the default) keeps the script's own task,
        so every existing caller is unaffected.
    :return: The dispatched task name, the created task-history id, and the
        script's filename.
    :raises HTTPUnprocessableEntityException: When ``body.args`` fails validation
        against the script's execution model.
    :raises HTTPException: Propagated from ``build_execution_meta`` (approval and
        field-gate enforcement) and from an error status on the Tasks API dispatch.
    :raises OSError: Propagated from the Tasks API dispatch when the transport
        itself fails (``aiohttp``'s connection errors are ``OSError`` subclasses).
    """
    try:
        validated = script.get_execution_model().model_validate(body.args)
    except ValidationError as exc:
        raise HTTPUnprocessableEntityException(detail=exc.errors()) from exc
    meta = source.build_execution_meta(
        script, body.model_copy(update={"args": validated.model_dump()})
    )
    task_name = execution_task_name or script.execution_task_name
    task_id = await post_task_execution(tasks_api, task_name, meta)
    return ScriptExecutionResponse(
        task_name=task_name,
        task_id=task_id,
        snippet_filename=script.filename,
    )
