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

"""Define dependencies for the Support Snippets plugin."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated

from aiofiles.ospath import isfile
from fastapi import Depends, Query
from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.auth.exceptions import HTTPForbiddenException
from app.core.db.list_query import ListQuery, make_list_query_dep
from app.core.exceptions import HTTPBadRequestException, HTTPNotFoundException
from app.extensions.apps.framework.script_helpers import build_execution_meta
from app.extensions.deps import SessionDep
from app.extensions.snippets.config import snippets_settings
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.list_query import SnippetApprovalFilter, SnippetListQuery
from app.extensions.snippets.models.responses import (
    BatchApprovalErrorResponse,
    SnippetBatchApproveRequest,
)
from app.extensions.snippets.models.snippet import (
    BaseSnippetArgs,
    Snippet,
    SnippetExecutionMeta,
)

logger = logging.getLogger(__name__)

_SNIPPET_FILENAME_PUNCTUATION = {"_", "-", "."}


def _invalid_snippet_filename(filename: str) -> HTTPBadRequestException:
    """Return the standard bad-request exception for invalid snippet names."""
    return HTTPBadRequestException(detail=f"Invalid snippet filename: {filename!r}")


def _is_safe_snippet_path_component(part: str) -> bool:
    """Return whether one POSIX path component is safe for snippet lookup."""
    if not part:
        return False
    if not part.isascii():
        return False
    if not (part[0].isalnum() or part[0] == "_"):
        return False
    return all(c.isalnum() or c in _SNIPPET_FILENAME_PUNCTUATION for c in part)


def _has_lowercase_alpha_suffix(part: str) -> bool:
    """Return whether a filename has a lowercase alphabetic extension."""
    suffix = PurePosixPath(part).suffix[1:]
    return bool(suffix) and suffix.isascii() and suffix.isalpha() and suffix.islower()


def validate_snippet_filename(filename: str) -> None:
    """Raise 400 if ``filename`` is not a safe snippet filename or path.

    Accepts plain filenames (``check.sh``) and safe relative subdirectory
    paths (``team/check.sh``) that mirror what ``update_snippets()`` stores
    via ``path.relative_to(BASE_DIR)``. Rejects absolute paths, Windows
    separators, traversal components, and any component that does not meet
    the safe-name rules.

    :param filename: The raw filename string to validate.
    :type filename: str
    :raises HTTPBadRequestException: If the filename is unsafe or malformed.
    """
    # Reject any Windows separator — these never appear in POSIX-stored paths.
    if "\\" in filename:
        raise _invalid_snippet_filename(filename)

    posix_path = PurePosixPath(filename)
    if posix_path.is_absolute():
        raise _invalid_snippet_filename(filename)

    parts = filename.split("/")
    # Reject traversal or hidden-file components, and empty parts (consecutive //).
    for part in parts:
        if part in ("", ".", "..") or not _is_safe_snippet_path_component(part):
            raise _invalid_snippet_filename(filename)

    # The last component (the actual file) must have a lowercase alpha extension.
    if not _has_lowercase_alpha_suffix(parts[-1]):
        raise _invalid_snippet_filename(filename)


async def get_snippet(
    session: SessionDep,
    snippet_filename: Annotated[
        str,
        Query(
            ...,
            description="Snippet filename (relative path under snippets root).",
        ),
    ],
) -> Snippet:
    """Fetch and return a snippet by the specified filename.

    :param snippet_filename: The filename of the snippet to retrieve.
    :type snippet_filename: str
    :param session: The asynchronous database session.
    :type session: AsyncSession
    :return: The retrieved snippet.
    :rtype: Snippet
    :raises HTTPBadRequestException: If the filename is not a safe single snippet
        filename.
    :raises HTTPNotFoundException: If a snippet with the specified filename is not
        found, of if the snippet file does not exist.
    """
    validate_snippet_filename(snippet_filename)
    snippet = await SnippetManager.get_or_404(session, filename=snippet_filename)
    if not await isfile(Path(snippet)):
        raise HTTPNotFoundException
    return snippet


SnippetDep = Annotated[Snippet, Depends(get_snippet)]


def build_snippet_execution_meta(
    snippet: Snippet,
    execution_args: BaseSnippetArgs,
    snippet_source: str,
) -> SnippetExecutionMeta:
    """Build the :class:`SnippetExecutionMeta` payload for a snippet execution.

    Shared between the form-based execute route and the JSON API execute
    route so both produce a byte-identical payload for the same inputs
    (modulo ``snippet_source`` URL host, which is request-scoped).

    :param snippet: The executable snippet.
    :type snippet: Snippet
    :param execution_args: Validated execution arguments for the snippet.
    :type execution_args: BaseSnippetArgs
    :param snippet_source: Signed URL the executor uses to download the
        snippet artifact.
    :type snippet_source: str
    :return: The prepared execution metadata.
    :rtype: SnippetExecutionMeta
    :raises HTTPBadRequestException: When the snippet has no configured interpreter.
    """
    interpreter = snippet.execution_interpreter
    if interpreter is None:
        raise HTTPBadRequestException(detail="No interpreter configured for snippet")
    return build_execution_meta(
        snippet,
        execution_args,
        interpreter=interpreter,
        snippet_source=snippet_source,
        snippet_filename=snippet.filename,
    )


def require_manual_sync_enabled() -> None:
    """Raise HTTPForbiddenException if manual snippet sync is disabled.

    :raises HTTPForbiddenException: If ``ENABLE_MANUAL_SYNC`` is False.
    """
    if not snippets_settings.ENABLE_MANUAL_SYNC:
        raise HTTPForbiddenException(
            detail="Manual snippet sync is disabled in this deployment.",
        )


IsManualSyncEnabled = Depends(require_manual_sync_enabled)


@dataclass(slots=True)
class SnippetBatchExistenceResult:
    """Structured outcome of the batch-approve precheck.

    :param snippets: Rows fetched from the DB for the requested filenames.
    :type snippets: list[Snippet]
    :param missing_in_db: Filenames absent from the database, sorted.
    :type missing_in_db: list[str]
    :param missing_on_disk: Filenames present in the DB but whose underlying
        file is absent from the snippets directory, sorted.
    :type missing_on_disk: list[str]
    """

    snippets: list[Snippet] = field(default_factory=list)
    missing_in_db: list[str] = field(default_factory=list)
    missing_on_disk: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """True when either error category is populated."""
        return bool(self.missing_in_db or self.missing_on_disk)


async def check_snippet_batch_existence(
    session: AsyncSession, filenames: Iterable[str]
) -> SnippetBatchExistenceResult:
    """Verify that every filename has a DB row and an on-disk file.

    Shared between the legacy Jinja2 batch route and the JSON API batch
    endpoint so both surfaces report the same hard-error categories. The
    helper deliberately does **not** check approval state — the JSON path
    treats already-approved as a soft-skip via the atomic
    ``update_where(... approved_at IS NULL)`` filter.

    :param session: The active database session.
    :type session: AsyncSession
    :param filenames: The filenames the caller wants to act on.
    :type filenames: Iterable[str]
    :return: A populated :class:`SnippetBatchExistenceResult`.
    :rtype: SnippetBatchExistenceResult
    """
    filenames_set = set(filenames)
    if not filenames_set:
        return SnippetBatchExistenceResult()
    snippets = await SnippetManager.list(
        session, col(Snippet.filename).in_(filenames_set)
    )
    found = {snippet.filename for snippet in snippets}
    missing_in_db = sorted(filenames_set - found)
    missing_on_disk = sorted(
        [snippet.filename for snippet in snippets if not await isfile(Path(snippet))]
    )
    return SnippetBatchExistenceResult(
        snippets=snippets,
        missing_in_db=missing_in_db,
        missing_on_disk=missing_on_disk,
    )


async def get_batch_existence(
    body: SnippetBatchApproveRequest,
    session: SessionDep,
) -> SnippetBatchExistenceResult:
    """Resolve and validate batch snippet existence, raising 400 on hard errors.

    :param body: The parsed JSON body containing the filenames to approve.
    :type body: SnippetBatchApproveRequest
    :param session: The active database session.
    :type session: AsyncSession
    :return: Existence result guaranteed to have no hard errors.
    :rtype: SnippetBatchExistenceResult
    :raises HTTPBadRequestException: When any filename is missing from the DB
        or has no corresponding file on disk.
    """
    existence = await check_snippet_batch_existence(session, body.filenames)
    if existence.has_errors:
        raise HTTPBadRequestException(
            detail=BatchApprovalErrorResponse(
                missing_in_db=existence.missing_in_db,
                missing_on_disk=existence.missing_on_disk,
            ).model_dump()
        )
    return existence


SnippetBatchExistenceDep = Annotated[
    SnippetBatchExistenceResult, Depends(get_batch_existence)
]

CoreListQueryDep = Annotated[ListQuery, Depends(make_list_query_dep(SnippetManager))]


def get_snippet_list_query(
    core: CoreListQueryDep,
    approval: Annotated[
        SnippetApprovalFilter, Query(description="Approval-status filter.")
    ] = SnippetApprovalFilter.ALL,
    service_type: Annotated[
        str | None,
        Query(description="Service-type equality filter, or omitted for no filter."),
    ] = None,
    *,
    uncategorized: Annotated[
        bool,
        Query(
            description=(
                "When true, keep only snippets with no (absent or blank) service "
                "type. A separate flag so a real service type can never collide "
                "with a reserved sentinel. Takes precedence over 'service_type'."
            )
        ),
    ] = False,
) -> SnippetListQuery:
    """Compose the Core sort/search with the snippets filter parameters.

    ``sort`` and ``search`` are declared and validated by the Core dependency built
    from :attr:`~app.extensions.snippets.crud.SnippetManager.list_query_spec`, so an
    out-of-allowlist sort key is rejected there with a 422 and no raw client-supplied
    column name reaches query construction. This wrapper only adds the snippets
    filters, which stay separate predicates rather than spec members.

    :param core: The Core-resolved sort and search.
    :param approval: The requested approval-status filter.
    :param service_type: The requested service-type equality filter.
    :param uncategorized: When ``True``, filter to snippets with no service type.
    :return: An immutable list-query value object.
    """
    return SnippetListQuery(
        core=core,
        approval=approval,
        service_type=service_type,
        uncategorized=uncategorized,
    )
