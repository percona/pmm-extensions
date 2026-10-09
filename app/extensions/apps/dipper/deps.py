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

"""Dependencies for the Dipper plugin."""

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPNotFoundException,
    HTTPUnprocessableEntityException,
)
from app.core.utils.iterators import unique_everseen
from app.extensions.apps.dipper.constants import (
    ARTIFACT_TYPE_DIPPER,
    CollectorTypeEnum,
    DIPPER_PMM_SCRIPT_BY_SERVICE_TYPE,
    DIPPER_SCRIPT_BY_SERVICE_TYPE,
)
from app.extensions.apps.dipper.models import DipperExecuteWrite, DipperScript
from app.extensions.apps.framework.script_helpers import (
    build_artifact_download_url,
    build_execution_meta,
    build_script_preview,
)
from app.extensions.apps.framework.script_source import ScriptPreviewResponse
from app.extensions.clients.pmm import PMMRemoteAPI
from app.extensions.deps import (
    ExecutorHosts,
    get_pmm_api,  # noqa: F401 -- re-exported for existing importers
    PMMAPIDep,  # noqa: F401 -- re-exported for existing importers
)
from app.extensions.inventory import CreatedService
from app.extensions.snippets.models.snippet import (
    BaseSnippetArgs,
    EXECUTOR_HOSTS_INPUT_NAME,
    SnippetExecutionMeta,
    SUDO_INPUT_NAME,
)
from app.inventory.models import ServiceTypeEnum

logger = logging.getLogger(__name__)


def get_dipper_script_filename(
    service_type: ServiceTypeEnum,
    collector_type: CollectorTypeEnum = CollectorTypeEnum.ENVIRONMENT,
) -> str:
    """Return the payload script filename for a given service type and collector type.

    :param service_type: The service type enum.
    :type service_type: ServiceTypeEnum
    :param collector_type: The collector type (environment or pmm).
    :type collector_type: CollectorTypeEnum
    :return: The filename of the corresponding Dipper payload script.
    :rtype: str
    :raises HTTPNotFoundException: If no script is found for the given service type.
    """
    script_map = (
        DIPPER_PMM_SCRIPT_BY_SERVICE_TYPE
        if collector_type == CollectorTypeEnum.PMM
        else DIPPER_SCRIPT_BY_SERVICE_TYPE
    )
    try:
        return script_map[service_type]
    except KeyError as exc:
        raise HTTPNotFoundException from exc


def has_pmm_script(service_type: ServiceTypeEnum) -> bool:
    """Check if a PMM collector script exists for the given service type.

    :param service_type: The service type enum.
    :type service_type: ServiceTypeEnum
    :return: True if a PMM script exists for the service type, False otherwise.
    :rtype: bool
    """
    return service_type in DIPPER_PMM_SCRIPT_BY_SERVICE_TYPE


async def load_dipper_script(
    service: CreatedService,
    collector_type: CollectorTypeEnum,
    *,
    update_meta: bool = False,
) -> DipperScript:
    """Resolve and load a Dipper script for API helpers.

    :param service: Inventory service that determines the payload.
    :type service: CreatedService
    :param collector_type: Environment or PMM collector type.
    :type collector_type: CollectorTypeEnum
    :param update_meta: Whether to load and validate script frontmatter.
    :type update_meta: bool
    :return: Loaded Dipper script.
    :rtype: DipperScript
    :raises HTTPNotFoundException: When no mapped or packaged payload exists.
    """
    filename = get_dipper_script_filename(service.type, collector_type)
    try:
        return await DipperScript.from_path(filename, update_meta=update_meta)
    except FileNotFoundError as exc:
        logger.warning("Missing dipper payload script %r", filename)
        raise HTTPNotFoundException from exc


async def get_dipper_script_preview(
    service: CreatedService,
    collector_type: CollectorTypeEnum,
) -> ScriptPreviewResponse:
    """Return a script-preview response for the given service and collector type.

    :param service: The inventory service whose script should be previewed.
    :type service: CreatedService
    :param collector_type: Which script variant to preview (environment or pmm).
    :type collector_type: CollectorTypeEnum
    :return: The preview content alongside language and truncation metadata.
    :rtype: ScriptPreviewResponse
    :raises HTTPNotFoundException: When no script exists for the service/collector combination.
    :raises HTTPUnprocessableEntityException: When the script contains non-UTF-8 bytes.
    """
    script = await load_dipper_script(service, collector_type)
    try:
        return await build_script_preview(script)
    except UnicodeDecodeError as exc:
        raise HTTPUnprocessableEntityException(
            detail=f"Dipper script {script.filename!r} contains non-UTF-8 bytes; preview unavailable."
        ) from exc


def get_dipper_script_source(request: Request, script: DipperScript) -> str:
    """Return a signed URL for Nomad to download the dipper payload script.

    :param request: The HTTP request object.
    :type request: Request
    :param script: The dipper script to generate the signed download URL for.
    :type script: DipperScript
    :return: The signed URL to download the dipper artifact.
    :rtype: str
    """
    return build_artifact_download_url(
        request,
        artifact_type=ARTIFACT_TYPE_DIPPER,
        filename=script.filename,
        md5_digest=script.md5_digest,
    )


def resolve_pmm_executor_host(executor_hosts: ExecutorHosts) -> str | None:
    """Resolve Nomad node for PMM collector execution.

    Resolution order:
    1. Explicit EXECUTION_TARGET setting (matched against node names and addresses)
    2. Host derived from ENDPOINT URL (matched against node addresses)
    3. None (falls back to manual selection in UI)

    :param executor_hosts: Dictionary mapping Nomad node names to addresses.
    :type executor_hosts: ExecutorHosts
    :return: The resolved Nomad node name, or None if no match found.
    :rtype: str | None
    """
    pmm = settings.PMM

    host = None
    for node_name, node_address in executor_hosts.items():
        if pmm.execution_target and pmm.execution_target in (node_name, node_address):
            return node_name
        if pmm.hostname == node_address:
            host = node_name
    if host is None:
        logger.debug(
            "Configured PMM hostname (%r) and EXECUTION_TARGET (%r) did not match any available executor host: %r",
            pmm.hostname,
            pmm.execution_target,
            executor_hosts,
        )
    return host


def get_pmm_form_defaults(
    resolved_executor_host: str | None,
    service_name: str,
    node_name: str,
) -> dict[str, str]:
    """Build default form values for PMM collector scripts.

    :param resolved_executor_host: The resolved executor host for PMM, or None
        if resolution failed.
    :type resolved_executor_host: str | None
    :param service_name: The name of the selected service.
    :type service_name: str
    :param node_name: The name of the service node.
    :type node_name: str
    :return: A dictionary of default parameter values for the PMM form.
    :rtype: dict[str, str]
    """
    pmm = settings.PMM
    defaults = {}
    if resolved_executor_host is not None:
        defaults["pmmserver"] = "https://localhost:8443"
    elif pmm.endpoint:
        defaults["pmmserver"] = pmm.endpoint
    defaults["node"] = node_name
    defaults["service"] = service_name
    return defaults


def _dedupe_nonempty(names: Iterable[str]) -> list[str]:
    """Return non-blank names, deduplicated, preserving first-seen order.

    Guards :class:`~app.extensions.apps.framework.schema.Choice` (whose ``value`` and
    ``label`` are ``NonEmptyStr``) against blank entries and avoids duplicate
    dropdown options.

    :param names: The raw names to clean.
    :type names: Iterable[str]
    :return: The cleaned, de-duplicated list of names.
    :rtype: list[str]
    """
    stripped_names = ((name or "").strip() for name in names)
    return list(unique_everseen(name for name in stripped_names if name))


async def fetch_pmm_node_service_names(
    pmm_api: PMMRemoteAPI | None,
) -> tuple[list[str], list[str]]:
    """Return ``(node_names, service_names)`` from the configured PMM server.

    Best-effort: returns ``([], [])`` when PMM is unconfigured (``pmm_api`` is
    ``None``) or unreachable, so the caller can fall back to free-text inputs
    without failing the form-schema request.

    :param pmm_api: The PMM API client, or ``None`` if PMM is not configured.
    :type pmm_api: PMMRemoteAPI | None
    :return: A tuple of cleaned node names and service names.
    :rtype: tuple[list[str], list[str]]
    """
    if pmm_api is None:
        return [], []
    try:
        nodes = await pmm_api.get_nodes()
        services = await pmm_api.get_services()
    except Exception:
        logger.warning(
            "PMM node/service fetch failed; falling back to free-text inputs",
            exc_info=True,
        )
        return [], []
    return (
        _dedupe_nonempty(node.name for node in nodes),
        _dedupe_nonempty(service.name for service in services),
    )


def clean_dipper_api_args(raw_args: dict[str, Any]) -> dict[str, Any]:
    """Drop empty JSON form values without discarding meaningful falsy values."""
    cleaned = {}
    for key, value in raw_args.items():
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[key] = value
    return cleaned


def build_dipper_snippet_filename(service: CreatedService, script: DipperScript) -> str:
    """Return the Dipper snippet filename used in task history metadata."""
    return f"dipper/{service.id}/{script.filename}"


def build_dipper_meta_from_args(
    service: CreatedService,
    script: DipperScript,
    script_source: str,
    execution_args: BaseSnippetArgs,
    *,
    sudo_default: bool = False,
) -> SnippetExecutionMeta:
    """Build shared execution metadata for a Dipper collector dispatch.

    The guard against invalid frontmatter parameters sits here, at the
    meta-assembly seam between argument validation and dispatch. A parameter the
    frontmatter declared but the parser rejected (a reserved execution field
    name, for instance) is dropped from the form, so executing anyway would
    silently run the script without an argument its author asked for.

    The refusal enumerates the parser's own messages because this app does not
    surface them on the form: without them the operator would see a rejection
    naming no cause anywhere in the UI.

    :param service: The inventory service the collector runs against.
    :param script: The collector script being dispatched.
    :param script_source: The signed URL the executor downloads the script from.
    :param execution_args: The validated arguments for this execution.
    :param sudo_default: Whether to run with ``sudo`` when the args request nothing.
    :return: The execution metadata the framework posts to the Tasks API.
    :raises HTTPBadRequestException: When the script declares no runnable
        interpreter, or carries invalid frontmatter parameters.
    """
    interpreter = script.execution_interpreter
    if interpreter is None:
        raise HTTPBadRequestException(detail="No interpreter configured for script")
    if not script.can_execute:
        reasons = "; ".join(script.validated_parameters.errors)
        raise HTTPBadRequestException(
            detail=f"Script {script.filename!r} has invalid frontmatter parameters: "
            f"{reasons}"
        )
    return build_execution_meta(
        script,
        execution_args,
        interpreter=interpreter,
        snippet_source=script_source,
        snippet_filename=build_dipper_snippet_filename(service, script),
        sudo_default=sudo_default,
    )


async def build_dipper_execution_meta(
    service: CreatedService,
    body: DipperExecuteWrite,
    request: Request,
) -> tuple[SnippetExecutionMeta, str]:
    """Build execution metadata for a Dipper JSON API execute request.

    Resolves the collector script, merges PMM defaults, validates args against
    the script's dynamic execution model, and assembles the ``SnippetExecutionMeta``
    payload. Returns the metadata and the Celery task name used for dispatch.

    :param service: The resolved inventory service.
    :type service: CreatedService
    :param body: The validated JSON request body.
    :type body: DipperExecuteWrite
    :param request: The HTTP request (used to derive the artifact download URL).
    :type request: Request
    :return: The execution metadata and the execution task name.
    :rtype: tuple[SnippetExecutionMeta, str]
    :raises HTTPUnprocessableEntityException: When no script exists for the
        service/collector combination, when PMM args are missing, or when
        args fail the script's execution model schema.
    """
    try:
        script = await load_dipper_script(
            service, body.collector_type, update_meta=True
        )
    except HTTPNotFoundException as exc:
        raise HTTPUnprocessableEntityException(
            detail=(
                f"No {body.collector_type.value!r} collector script is available"
                f" for {service.type.value!r} services."
            )
        ) from exc

    raw_args = {**body.args, EXECUTOR_HOSTS_INPUT_NAME: body.executor_host}
    if body.sudo is not None:
        raw_args[SUDO_INPUT_NAME] = body.sudo

    if body.collector_type == CollectorTypeEnum.PMM:
        pmm = settings.PMM
        pmm_server = raw_args.get("pmmserver") or pmm.endpoint
        if not pmm_server:
            raise HTTPUnprocessableEntityException(
                detail=(
                    "PMM server URL is required. Provide it in args.pmmserver"
                    " or configure PMM__ENDPOINT."
                )
            )
        raw_args["pmmserver"] = pmm_server
        if not raw_args.get("apikey") and pmm.api_key:
            raw_args["apikey"] = pmm.api_key.get_secret_value()

    execution_model = script.get_execution_model()
    try:
        execution_args = execution_model.model_validate(clean_dipper_api_args(raw_args))
    except ValidationError as exc:
        raise HTTPUnprocessableEntityException(detail=exc.errors()) from exc

    script_source = get_dipper_script_source(request, script)
    meta = build_dipper_meta_from_args(
        service,
        script,
        script_source,
        execution_args,
        sudo_default=body.sudo or False,
    )
    return meta, script.execution_task_name
