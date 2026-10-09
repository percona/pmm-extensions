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

"""Define the snippet-ingestion Celery task.

This module is named directly in ``STATIC_CELERY_INCLUDE`` rather than derived
from the snippets app's activation entry, so snippet ingestion keeps running in
an image that never mounts the snippets UI. The task is deliberately unowned --
it writes library-owned ``Snippet`` rows, not app-owned state -- so disabling
the snippets app neither gates nor cancels it.
"""

import logging
from pathlib import Path

from sqlmodel import col
from sqlmodel.ext.asyncio.session import AsyncSession

from app.celery import celery
from app.extensions.db import get_async_session_maker
from app.extensions.snippets.builtin_manifest import load_builtin_checksum_manifest
from app.extensions.snippets.checksums import (
    BUILTIN_CHECKSUM_MANIFEST,
    manifest_relative_path,
    sha256_file,
)
from app.extensions.snippets.config import SnippetFilterType, snippets_settings
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.models.snippet import Snippet
from app.extensions.snippets.utils import guess_mime_type

logger = logging.getLogger(__name__)

_CONTENT_CHANGED_REASON = "File contents have changed"

# The ``system`` sentinel lands in ``Snippet.updated_by`` alongside real user
# ids, where ``is_human_revoked`` reads a non-null ``updated_by`` on an
# unapproved row as a sticky administrator revocation.
BUILTIN_APPROVAL_USER_ID = "system"
BUILTIN_APPROVAL_REASON = "Auto-approved: matches built-in checksum manifest"


@celery.task
def sync_snippets() -> None:
    """Define Celery task to sync snippets from `extensions_setting.SNIPPETS.SNIPPETS_DIR`."""
    celery.loop.run_until_complete(update_snippets())


async def update_snippets() -> None:
    """Sync snippet files into the database and apply built-in auto-approval.

    Skip the checksum manifest file. Create, update, or delete snippet rows to
    match disk, and auto-approve manifest-matching files when that setting is
    enabled, without restoring administrator revocations.
    """
    async_session = get_async_session_maker()
    async with async_session() as session:
        content_changed_snippets: list[Snippet] = []
        approval_snippets: list[Snippet] = []
        processed_filenames: list[str] = []
        skipped_filenames: list[str] = []
        created_count = 0
        auto_approve_enabled = snippets_settings.AUTO_APPROVE_BUILTIN_SNIPPETS
        manifest = (
            await load_builtin_checksum_manifest(snippets_settings.SNIPPETS_DIR)
            if auto_approve_enabled
            else {}
        )
        for snippet_path in snippets_settings.SNIPPETS_DIR.rglob("*"):
            if not snippet_path.is_file():
                continue
            snippet_name = manifest_relative_path(
                snippet_path, snippets_settings.SNIPPETS_DIR
            )
            if snippet_name == BUILTIN_CHECKSUM_MANIFEST:
                continue
            processed_filenames.append(snippet_name)
            if should_skip_snippet(snippet_path):
                skipped_filenames.append(snippet_name)
                continue
            created = await _sync_snippet_file(
                session,
                snippet_path,
                snippet_name,
                manifest,
                content_changed_snippets,
                approval_snippets,
                auto_approve_enabled=auto_approve_enabled,
            )
            if created:
                created_count += 1
        await _persist_snippet_sync_batches(
            session,
            created_count=created_count,
            content_changed_snippets=content_changed_snippets,
            approval_snippets=approval_snippets,
        )
        await _delete_unsynced_snippets(session, processed_filenames, skipped_filenames)


async def _persist_snippet_sync_batches(
    session: AsyncSession,
    *,
    created_count: int,
    content_changed_snippets: list[Snippet],
    approval_snippets: list[Snippet],
) -> None:
    """Log sync totals and batch-save content-change and approval-only rows.

    :param session: The active database session.
    :param created_count: Number of new snippet rows created during this sync.
    :param content_changed_snippets: Rows whose file contents changed.
    :param approval_snippets: Rows that only need approval fields persisted.
    """
    if created_count:
        logger.info("Added %s new snippets", created_count)
    if content_changed_snippets:
        logger.info("Updating %s modified snippets", len(content_changed_snippets))
        await SnippetManager.save_batch(
            session, *content_changed_snippets, flag_modified_fields=["meta"]
        )
    if approval_snippets:
        logger.info(
            "Auto-approving %s snippets from built-in checksum manifest",
            len(approval_snippets),
        )
        await SnippetManager.save_batch(session, *approval_snippets)


async def _sync_snippet_file(
    session: AsyncSession,
    snippet_path: Path,
    snippet_name: str,
    manifest: dict[str, str],
    content_changed_snippets: list[Snippet],
    approval_snippets: list[Snippet],
    *,
    auto_approve_enabled: bool,
) -> bool:
    """Sync one snippet file into the database and apply approval policy.

    :param session: The active database session.
    :param snippet_path: Absolute path to the snippet file on disk.
    :param snippet_name: Filename relative to the snippets directory.
    :param manifest: Built-in checksum mapping of filename to SHA-256 digest.
    :param content_changed_snippets: Mutable list collecting rows whose file
        contents changed (caller batch-saves with ``meta`` flagged modified).
    :param approval_snippets: Mutable list collecting rows that only need
        approval fields persisted (new auto-approvals and upgrade-path approvals).
    :param auto_approve_enabled: Whether manifest-matched auto-approval is enabled.
    :return: ``True`` when a new snippet row was created.
    """
    snippet = await Snippet.from_path(snippet_path)
    logger.debug("Processing file %s: %s", snippet_path, snippet.model_dump())
    expected_digest = manifest.get(snippet_name) if auto_approve_enabled else None
    manifest_match = (
        expected_digest is not None
        and expected_digest == await sha256_file(snippet_path)
    )
    created_snippet, created = await SnippetManager.get_or_create(
        session, snippet, {"filename"}
    )
    if created:
        logger.debug("New snippet created: %s", created_snippet.filename)
        if _apply_builtin_approval_policy(
            created_snippet,
            manifest_match=manifest_match,
            human_revoked=False,
            content_changed=False,
        ):
            approval_snippets.append(created_snippet)
        return True

    if created_snippet.md5_digest != snippet.md5_digest:
        logger.debug(
            "Snippet %s has changed: %s [before] != %s [now]",
            created_snippet.filename,
            created_snippet.md5_digest,
            snippet.md5_digest,
        )
        await created_snippet.update_from_snippet(snippet)
        _apply_builtin_approval_policy(
            created_snippet,
            manifest_match=manifest_match,
            human_revoked=created_snippet.is_human_revoked,
            content_changed=True,
        )
        content_changed_snippets.append(created_snippet)
    elif _apply_builtin_approval_policy(
        created_snippet,
        manifest_match=manifest_match,
        human_revoked=created_snippet.is_human_revoked,
        content_changed=False,
    ):
        approval_snippets.append(created_snippet)
    return False


def _apply_builtin_approval_policy(
    snippet: Snippet,
    *,
    manifest_match: bool,
    human_revoked: bool,
    content_changed: bool,
) -> bool:
    """Apply built-in checksum auto-approval policy for one sync transition.

    Owns the full branch: leave administrator revocations untouched; auto-approve
    on a manifest match; clear approval when contents changed and no longer match.
    Lives here (not on ``Snippet``) because the reason string and sentinel user id
    are constants of the manifest feature rather than of the snippet entity.

    :param snippet: The snippet row to transition.
    :param manifest_match: Whether the on-disk file matches the built-in manifest.
    :param human_revoked: Whether an administrator had revoked approval.
    :param content_changed: Whether this sync refreshed file-derived fields.
    :return: ``True`` when approval fields were mutated (caller may batch-save).
    """
    if human_revoked:
        return False
    if manifest_match:
        if content_changed or not snippet.is_approved:
            snippet.approve(BUILTIN_APPROVAL_REASON, BUILTIN_APPROVAL_USER_ID)
            return True
        return False
    if content_changed:
        snippet.remove_approval(_CONTENT_CHANGED_REASON, None)
        return True
    return False


async def _delete_unsynced_snippets(
    session: AsyncSession,
    processed_filenames: list[str],
    skipped_filenames: list[str],
) -> None:
    """Delete snippet rows whose files are gone from disk or now filtered out."""
    delete_result = await SnippetManager.delete_where(
        session, col(Snippet.filename).not_in(processed_filenames)
    )
    if delete_result.rowcount:
        logger.info(
            "Deleted %s snippets not found in filesystem", delete_result.rowcount
        )
    delete_result = await SnippetManager.delete_where(
        session,
        col(Snippet.filename).in_(skipped_filenames),
        col(Snippet.approved_at).is_(None),
    )
    if delete_result.rowcount:
        logger.info(
            "Deleted %s non-approved snippets that don't match the defined filters",
            delete_result.rowcount,
        )


def should_skip_snippet(snippet_path: Path) -> bool:
    """Determine if a snippet file should be skipped based on defined filters.

    :param snippet_path: The path to the snippet file.
    :type snippet_path: Path
    :return: True if the snippet should be skipped, False otherwise.
    :rtype: bool
    """
    if snippets_settings.SYNC_FILTER is not None:
        extension = snippet_path.suffix.lower()
        mime = guess_mime_type(snippet_path)
        if (
            extension,
            SnippetFilterType.EXTENSION,
        ) not in snippets_settings.SYNC_FILTER and (
            mime,
            SnippetFilterType.MIME_TYPE,
        ) not in snippets_settings.SYNC_FILTER:
            logger.debug(
                "Skipping file %s since no match found in defined filters (%r, %r)",
                snippet_path,
                extension,
                mime,
            )
            return True
    return False
