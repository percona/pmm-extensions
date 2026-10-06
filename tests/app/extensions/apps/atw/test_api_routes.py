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

"""Tests for the ATW plugin JSON API routes under /api/apps/atw/."""

import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from httpx import AsyncClient, Response
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app import BASE_DIR
from app.core.auth.providers.casdoor.models import CasdoorUser
from app.core.pagination import MAX_PAGINATION_LIMIT
from app.core.requests import RemoteAPI
from app.core.utils.date_time import utc_now
from app.extensions.apps.atw import api_routes as atw_api_routes
from app.extensions.apps.atw.categories import (
    ATWCategory,
    CATEGORY_ROOT_LABELS,
    ParentCategory,
)
from app.extensions.apps.atw.crud import AtwIncidentExecutionManager, AtwIncidentManager
from app.extensions.apps.atw.models import (
    AtwIncident,
    AtwIncidentExecution,
    AtwIncidentResponse,
)
from app.extensions.deps import BEARER_REQUIRED_DETAIL
from app.extensions.snippets.config import SnippetSudoOption
from app.extensions.snippets.crud import SnippetManager
from app.extensions.snippets.models import Snippet
from app.extensions.snippets.models.meta import (
    META_KEY_ATW,
    META_KEY_DIAGNOSTIC_CATEGORIES,
)
from app.inventory.models import ServiceTypeEnum
from app.tasks.models import TaskHistoryStatusEnum

_GENERIC_ROOT = CATEGORY_ROOT_LABELS["generic"]
_REPO_SNIPPETS_DIR = BASE_DIR / "snippets"


def _mock_atw_snippet(
    *,
    filename: str,
    title: str = "Title",
    description: str = "",
    diagnostic_categories: list[str],
    service_type: str | None = "mysql",
    sudo: SnippetSudoOption = SnippetSudoOption.NEVER,
) -> Mock:
    snippet = Mock(spec=Snippet)
    snippet.filename = filename
    snippet.title = title
    snippet.description = description
    snippet.sudo = sudo
    snippet.meta = {"diagnostic_categories": diagnostic_categories}
    if service_type is not None:
        snippet.meta["service_type"] = service_type
    return snippet


def _get_listing(test_client: TestClient, snippets: list[Mock]) -> Response:
    """Return the category listing response served for ``snippets``.

    :param test_client: The API test client.
    :param snippets: The snippets ``SnippetManager.list`` returns.
    :return: The ``GET /api/apps/atw/`` response.
    """
    with patch(
        "app.extensions.apps.atw.api_routes.SnippetManager.list",
        new=AsyncMock(return_value=snippets),
    ):
        return test_client.get("/api/apps/atw/")


async def _persist_atw_snippet(
    session: AsyncSession,
    *,
    filename: str,
    diagnostic_categories: list[str],
    service_type: str = "mysql",
    approved: bool,
) -> Snippet:
    """Persist a real ``Snippet`` row tagged for the ATW browser.

    :param session: The database session.
    :param filename: The snippet's filename.
    :param diagnostic_categories: The category tags to record under
        ``meta["diagnostic_categories"]``.
    :param service_type: The service type to record under ``meta["service_type"]``.
    :param approved: Whether the persisted snippet should carry an ``approved_at``.
    :return: The persisted ``Snippet`` row.
    """
    snippet = Snippet(
        filename=filename,
        size=100,
        md5_digest="a" * 32,
        approved_at=utc_now() if approved else None,
        meta={
            "title": f"Title for {filename}",
            "description": "desc",
            "service_type": service_type,
            "diagnostic_categories": diagnostic_categories,
        },
    )
    return await SnippetManager.create(session, snippet)


async def _persist_corpus_snippet(
    session: AsyncSession,
    snippets_dir: Path,
    *,
    filename: str,
    approved: bool = True,
) -> Snippet:
    """Persist a real repository snippet after parsing its on-disk frontmatter.

    :param session: The database session.
    :param snippets_dir: The temporary directory aliased as ``Snippet.BASE_DIR``.
    :param filename: The repository snippet filename to copy and persist.
    :param approved: Whether the persisted snippet should carry an ``approved_at``.
    :return: The persisted ``Snippet`` row.
    :raises ValueError: When ``filename`` resolves outside the source or target
        snippets directories.
    """
    source_root = _REPO_SNIPPETS_DIR.resolve()
    target_root = snippets_dir.resolve()
    source = (source_root / filename).resolve()
    target = (target_root / filename).resolve()
    if not source.is_relative_to(source_root):
        raise ValueError(f"snippet path escapes repository snippets dir: {filename}")
    if not source.is_file():
        raise ValueError(f"snippet path is not a repository snippet file: {filename}")
    if not target.is_relative_to(target_root):
        raise ValueError(f"snippet path escapes test snippets dir: {filename}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    snippet = await Snippet.from_path(filename, update_meta=True)
    if approved:
        snippet.approve("Seeded as approved", "seed-user")
    return await SnippetManager.create(session, snippet)


async def _persist_snippet(
    session: AsyncSession,
    *,
    filename: str,
    meta: dict[str, Any],
    approved: bool = True,
) -> Snippet:
    """Persist a real ``Snippet`` row carrying caller-supplied frontmatter.

    Unlike :func:`_persist_atw_snippet` the metadata is not shaped for the category
    browser, so a row can omit ``diagnostic_categories`` or declare a degenerate
    title.

    :param session: The database session.
    :param filename: The snippet's filename.
    :param meta: The frontmatter metadata to record verbatim.
    :param approved: Whether the persisted snippet should carry an ``approved_at``.
    :return: The persisted ``Snippet`` row.
    """
    snippet = Snippet(
        filename=filename,
        size=100,
        md5_digest="a" * 32,
        approved_at=utc_now() if approved else None,
        meta=meta,
    )
    return await SnippetManager.create(session, snippet)


class TestAtwListEndpoint:
    """Tests for GET /api/apps/atw/."""

    def test_atw_list_returns_grouped_snippets(self, test_client: TestClient):
        """Ensure the listing endpoint groups mysql-tagged snippets under the MySQL root."""
        snippet = _mock_atw_snippet(
            filename="diag/slow-query.sh",
            title="Slow Query Diagnostics",
            description="Collects slow-query and processlist data.",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type="mysql",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        assert "application/json" in response.headers["content-type"]
        payload = response.json()
        assert isinstance(payload, list)
        assert len(payload) == 1
        overall = next(
            entry for entry in payload if entry["category"] == "OVERALL_SLOWNESS"
        )
        assert overall is not None
        assert overall["snippet_count"] == 1
        assert overall["category_root"] == CATEGORY_ROOT_LABELS[ServiceTypeEnum.MYSQL]
        assert overall["parent_category"] == "PERFORMANCE_ISSUES"
        summary = overall["snippets"][0]
        assert summary["name"] == "diag/slow-query.sh"
        assert set(summary.keys()) == {"name", "title", "description", "sudo"}

    def test_atw_list_reports_the_declared_sudo_requirement(
        self, test_client: TestClient
    ) -> None:
        """Publish a mandatory-elevation snippet as ``always`` on the category listing.

        The category browser is how the collect pane reaches scripts, so this path
        needs its own assertion rather than inheriting the search route's.
        """
        snippet = _mock_atw_snippet(
            filename="diag/dmesg.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            sudo=SnippetSudoOption.ALWAYS,
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["snippets"][0]["sudo"] == "always"

    @pytest.mark.parametrize(
        ("option", "expected"),
        [
            (SnippetSudoOption.NEVER, "never"),
            (SnippetSudoOption.OPTIONAL, "optional"),
            (SnippetSudoOption.ALWAYS, "always"),
            (SnippetSudoOption.OPTIONAL_DEFAULT_TRUE, "optional"),
        ],
        ids=["never", "optional", "always", "optional_default_true"],
    )
    def test_atw_list_distinguishes_the_three_states(
        self, test_client: TestClient, option: SnippetSudoOption, expected: str
    ) -> None:
        """Collapse the four declared options onto exactly three wire values.

        ``OPTIONAL_DEFAULT_TRUE`` reports ``optional`` rather than a fourth value:
        the default-checked nuance is carried by ``sudo_default``, not here.
        """
        snippet = _mock_atw_snippet(
            filename="diag/x.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            sudo=option,
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["snippets"][0]["sudo"] == expected

    def test_atw_list_real_snippet_row_meta_shape(
        self, test_client: TestClient
    ) -> None:
        """Integration guard: real ``Snippet`` + ``meta`` dict matches what the route reads."""
        snippet = Snippet(
            filename="diag/slow-query.sh",
            size=100,
            md5_digest="a" * 32,
            meta={
                "title": "Slow Query Diagnostics",
                "description": "Collects slow-query and processlist data.",
                "service_type": "mysql",
                "diagnostic_categories": ["OVERALL_SLOWNESS"],
            },
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        overall = next(
            entry for entry in payload if entry["category"] == "OVERALL_SLOWNESS"
        )
        assert overall["snippet_count"] == 1
        assert overall["category_root"] == CATEGORY_ROOT_LABELS[ServiceTypeEnum.MYSQL]
        assert overall["snippets"][0]["name"] == "diag/slow-query.sh"
        assert overall["snippets"][0]["title"] == "Slow Query Diagnostics"
        assert overall["snippets"][0]["description"] == (
            "Collects slow-query and processlist data."
        )

    def test_atw_list_multi_root_mysql_and_mongodb(
        self, test_client: TestClient
    ) -> None:
        """Ensure mysql and mongodb snippets produce separate ``category_root`` rows."""
        mysql_snippet = _mock_atw_snippet(
            filename="mysql/slow.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type="mysql",
        )
        mongo_snippet = _mock_atw_snippet(
            filename="mongo/slow.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type="mongodb",
        )

        response = _get_listing(test_client, [mysql_snippet, mongo_snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        populated_types = (ServiceTypeEnum.MYSQL, ServiceTypeEnum.MONGODB)
        expected_roots = [
            CATEGORY_ROOT_LABELS[service_type]
            for service_type in CATEGORY_ROOT_LABELS
            if service_type in populated_types
        ]
        roots = [entry["category_root"] for entry in payload]
        assert roots == expected_roots
        for entry in payload:
            assert entry["category"] == "OVERALL_SLOWNESS"
            assert entry["snippet_count"] == 1

    def test_atw_list_generic_service_type_bucket(
        self, test_client: TestClient
    ) -> None:
        """Ensure ``service_type: generic`` snippets surface under the Generic root."""
        snippet = _mock_atw_snippet(
            filename="generic/disk.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type="generic",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category_root"] == _GENERIC_ROOT

    def test_atw_list_missing_service_type_falls_back_to_generic(
        self, test_client: TestClient
    ) -> None:
        """Ensure missing ``service_type`` meta buckets under Generic, not MySQL."""
        snippet = _mock_atw_snippet(
            filename="no-service-type.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type=None,
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category_root"] == _GENERIC_ROOT
        assert payload[0]["category"] == "OVERALL_SLOWNESS"
        assert payload[0]["snippet_count"] == 1

    def test_atw_list_unknown_service_type_falls_back_to_generic(
        self, test_client: TestClient
    ) -> None:
        """Ensure unknown ``service_type`` values bucket under Generic, not MySQL."""
        snippet = _mock_atw_snippet(
            filename="unknown/engine.sh",
            diagnostic_categories=["GALERA"],
            service_type="clickhouse",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category_root"] == _GENERIC_ROOT
        assert payload[0]["category"] == "GALERA"

    def test_atw_list_omits_empty_root_category_cells(
        self, test_client: TestClient
    ) -> None:
        """Ensure empty (root, category) cells are omitted from the listing."""
        snippet = _mock_atw_snippet(
            filename="mysql/only.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            service_type="mysql",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        populated = {(e["category_root"], e["category"]) for e in payload}
        mysql_root = CATEGORY_ROOT_LABELS[ServiceTypeEnum.MYSQL]
        assert populated == {(mysql_root, "OVERALL_SLOWNESS")}
        for category in ATWCategory:
            if category.name != "OVERALL_SLOWNESS":
                assert (mysql_root, category.name) not in populated

    def test_atw_list_backup_leaf_under_its_own_parent(
        self, test_client: TestClient
    ) -> None:
        """Ensure a backup-tagged mongodb snippet lists under Backup and Recovery."""
        snippet = _mock_atw_snippet(
            filename="mongo/pbm.sh",
            diagnostic_categories=["BACKUP_PBM"],
            service_type="mongodb",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        [row] = response.json()
        assert row["category_root"] == CATEGORY_ROOT_LABELS[ServiceTypeEnum.MONGODB]
        assert row["parent_category"] == "BACKUP_RECOVERY"
        assert row["parent_category_label"] == "Backup and Recovery"
        assert row["category"] == "BACKUP_PBM"
        assert row["category_label"] == "Backup / PBM"
        assert [s["name"] for s in row["snippets"]] == ["mongo/pbm.sh"]

    def test_atw_list_replica_set_leaf_follows_existing_leaves(
        self, test_client: TestClient
    ) -> None:
        """Ensure a multi-tagged snippet lists in each cell, in taxonomy order."""
        snippet = _mock_atw_snippet(
            filename="mongo/rs.sh",
            diagnostic_categories=["REPLICA_SET_REPLICATION", "PERFORMANCE_OTHER"],
            service_type="mongodb",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert [(e["parent_category"], e["category"]) for e in payload] == [
            ("PERFORMANCE_ISSUES", "PERFORMANCE_OTHER"),
            ("REPLICATION_HA", "REPLICA_SET_REPLICATION"),
        ]
        assert payload[1]["category_label"] == "Replica Set Replication"

    @pytest.mark.parametrize(
        "declared",
        ["Backup / PBM", "backup_pbm", "Replica Set Replication", "BACKUP_RECOVERY"],
    )
    def test_atw_list_ignores_labels_lowercase_and_parent_names(
        self, test_client: TestClient, declared: str
    ) -> None:
        """Ensure only exact leaf member names place a snippet in a cell."""
        snippet = _mock_atw_snippet(
            filename="mongo/mistagged.sh",
            diagnostic_categories=[declared],
            service_type="mongodb",
        )

        response = _get_listing(test_client, [snippet])

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_atw_list_non_list_diagnostic_categories_not_substring_matched(
        self, test_client: TestClient
    ) -> None:
        """Ignore a non-list category tag (avoids ``str`` substring ``in``)."""
        snippet = Mock()
        snippet.filename = "bad-meta.sh"
        snippet.title = "Bad meta"
        snippet.description = ""
        snippet.meta = {"diagnostic_categories": "noise OVERALL_SLOWNESS noise"}

        with (
            patch.object(atw_api_routes.logger, "warning") as warn_mock,
            patch(
                "app.extensions.apps.atw.api_routes.SnippetManager.list",
                new=AsyncMock(return_value=[snippet]),
            ),
        ):
            response = test_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
        warn_mock.assert_called_once_with(
            "Ignoring meta[%r] for snippet %s: expected list, got %s",
            META_KEY_DIAGNOSTIC_CATEGORIES,
            "bad-meta.sh",
            "str",
        )

    def test_atw_list_non_string_diagnostic_categories_item_is_ignored(
        self, test_client: TestClient
    ) -> None:
        """Ignore a category list containing a non-string member."""
        snippet = Mock()
        snippet.filename = "bad-item.sh"
        snippet.title = "Bad item"
        snippet.description = ""
        snippet.meta = {"diagnostic_categories": ["OVERALL_SLOWNESS", 1]}

        with (
            patch.object(atw_api_routes.logger, "warning") as warn_mock,
            patch(
                "app.extensions.apps.atw.api_routes.SnippetManager.list",
                new=AsyncMock(return_value=[snippet]),
            ),
        ):
            response = test_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
        warn_mock.assert_called_once_with(
            "Ignoring meta[%r] for snippet %s: expected list[str], got %s element",
            META_KEY_DIAGNOSTIC_CATEGORIES,
            "bad-item.sh",
            "int",
        )

    def test_atw_list_requires_authentication(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Ensure unauthenticated callers receive JSON 401."""
        response = unauthenticated_client.get(
            "/api/apps/atw/",
            follow_redirects=False,
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")


class TestAtwListApprovalFilter:
    """Verify persisted unapproved snippets get excluded from ATW listings.

    Unlike ``TestAtwListEndpoint``, these tests do not mock ``SnippetManager.list``
    so they exercise the real approval predicate against a real SQL query.
    """

    @pytest.mark.asyncio
    async def test_unapproved_snippet_is_absent(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure an unapproved ATW-tagged snippet never appears in the listing."""
        await _persist_atw_snippet(
            session,
            filename="unapproved.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=False,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_approved_snippet_present_with_accurate_count(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure an approved ATW-tagged snippet is still listed, via a real query."""
        await _persist_atw_snippet(
            session,
            filename="approved.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=True,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["snippet_count"] == 1
        assert payload[0]["snippets"][0]["name"] == "approved.sh"

    @pytest.mark.asyncio
    async def test_legacy_atw_meta_is_still_honoured(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Read the legacy ``atw`` metadata key until existing rows are resynced."""
        await _persist_snippet(
            session,
            filename="legacy.sh",
            meta={
                "title": "Legacy",
                "description": "d",
                "service_type": "mysql",
                META_KEY_ATW: ["OVERALL_SLOWNESS"],
            },
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert (
            payload[0]["category_root"] == CATEGORY_ROOT_LABELS[ServiceTypeEnum.MYSQL]
        )
        assert payload[0]["category"] == "OVERALL_SLOWNESS"
        assert payload[0]["snippets"][0]["name"] == "legacy.sh"

    @pytest.mark.asyncio
    async def test_diagnostic_categories_override_legacy_atw_meta(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Prefer ``diagnostic_categories`` when both metadata keys are present."""
        await _persist_snippet(
            session,
            filename="dual-key.sh",
            meta={
                "title": "Dual key",
                "description": "d",
                "service_type": "mysql",
                META_KEY_ATW: ["GALERA"],
                META_KEY_DIAGNOSTIC_CATEGORIES: ["OVERALL_SLOWNESS"],
            },
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category"] == "OVERALL_SLOWNESS"
        assert payload[0]["snippets"][0]["name"] == "dual-key.sh"

    @pytest.mark.asyncio
    async def test_malformed_diagnostic_categories_fall_back_to_legacy_atw_meta(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Fall back to the legacy key when the current one is malformed."""
        await _persist_snippet(
            session,
            filename="fallback.sh",
            meta={
                "title": "Fallback",
                "description": "d",
                "service_type": "mysql",
                META_KEY_ATW: ["GALERA"],
                META_KEY_DIAGNOSTIC_CATEGORIES: ["OVERALL_SLOWNESS", 1],
            },
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category"] == "GALERA"
        assert payload[0]["snippets"][0]["name"] == "fallback.sh"

    @pytest.mark.asyncio
    async def test_all_unapproved_category_produces_no_row(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure a category whose snippets are all unapproved is omitted entirely."""
        await _persist_atw_snippet(
            session,
            filename="a.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=False,
        )
        await _persist_atw_snippet(
            session,
            filename="b.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=False,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_mixed_approval_count_reflects_only_approved(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure a category's count and membership exclude only the unapproved row."""
        await _persist_atw_snippet(
            session,
            filename="approved.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=True,
        )
        await _persist_atw_snippet(
            session,
            filename="unapproved.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=False,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["snippet_count"] == 1
        assert payload[0]["snippets"][0]["name"] == "approved.sh"

    @pytest.mark.asyncio
    async def test_unapproved_multi_tag_snippet_excluded_from_every_cell(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure an unapproved snippet is excluded from every tag it is grouped under."""
        await _persist_atw_snippet(
            session,
            filename="multi-tag.sh",
            diagnostic_categories=["OVERALL_SLOWNESS", "GALERA"],
            approved=False,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_unapproved_snippet_in_another_category_does_not_hide_approved_one(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure the filter is applied per-row, not just within a shared cell."""
        await _persist_atw_snippet(
            session,
            filename="approved.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=True,
        )
        await _persist_atw_snippet(
            session,
            filename="unapproved.sh",
            diagnostic_categories=["GALERA"],
            approved=False,
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["category"] == "OVERALL_SLOWNESS"
        assert payload[0]["snippets"][0]["name"] == "approved.sh"

    @pytest.mark.asyncio
    async def test_approved_snippet_with_non_list_diagnostic_categories_is_still_ignored(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure approval does not bypass the category list-shape check."""
        snippet = await _persist_atw_snippet(
            session, filename="bad-meta.sh", diagnostic_categories=[], approved=True
        )
        snippet.meta["diagnostic_categories"] = "OVERALL_SLOWNESS"
        await SnippetManager.save(session, snippet)

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_revoked_snippet_disappears_from_a_later_listing(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure revoking a previously-approved snippet drops it on the next call."""
        snippet = await _persist_atw_snippet(
            session,
            filename="revoked.sh",
            diagnostic_categories=["OVERALL_SLOWNESS"],
            approved=True,
        )

        first = await async_api_client.get("/api/apps/atw/")
        assert first.json()[0]["snippets"][0]["name"] == "revoked.sh"

        snippet.approved_at = None
        await SnippetManager.save(session, snippet)

        second = await async_api_client.get("/api/apps/atw/")

        assert second.status_code == status.HTTP_200_OK
        assert second.json() == []

    @pytest.mark.asyncio
    async def test_no_snippets_returns_empty_list(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure an empty snippet table returns an empty listing, not an error."""
        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_real_corpus_scripts_drive_roots_and_categories(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        snippets_dir: Path,
    ) -> None:
        """Load real snippet files and expose their declared ATW roots and cells."""
        snippets = [
            await _persist_corpus_snippet(session, snippets_dir, filename=filename)
            for filename in (
                "proxysql_log_extractor.sh",
                "proxysql_status.sh",
                "haproxy_config_files.sh",
                "haproxy_logs_extractor.sh",
            )
        ]

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        cells = {
            (entry["category_root"], entry["category"]): {
                snippet["name"] for snippet in entry["snippets"]
            }
            for entry in payload
        }
        assert {entry["category_root"] for entry in payload} == {
            CATEGORY_ROOT_LABELS[ServiceTypeEnum.HAPROXY],
            CATEGORY_ROOT_LABELS[ServiceTypeEnum.PROXYSQL],
        }
        for snippet in snippets:
            expected_root = CATEGORY_ROOT_LABELS[snippet.service_type]
            declared = snippet.meta[META_KEY_DIAGNOSTIC_CATEGORIES]
            assert declared, (
                f"{snippet.filename} should declare categories for this test"
            )
            for category in declared:
                assert snippet.filename in cells[(expected_root, category)]


class TestAtwListMongoDBMembership:
    """Verify where the builtin MongoDB replication and backup scripts list."""

    @pytest.mark.asyncio
    async def test_builtin_scripts_list_under_the_mongodb_leaves(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        snippets_dir: Path,
    ) -> None:
        """Ensure the shipped frontmatter files each script in its MongoDB cells."""
        for filename in (
            "mongodb_pbm_diagnostics.sh",
            "mongodb_repl_lag_check.sh",
            "mongodb_replica_set_collect.sh",
        ):
            await _persist_corpus_snippet(session, snippets_dir, filename=filename)

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        mongo_root = CATEGORY_ROOT_LABELS[ServiceTypeEnum.MONGODB]
        cells = {
            entry["category"]: {snippet["name"] for snippet in entry["snippets"]}
            for entry in response.json()
            if entry["category_root"] == mongo_root
        }
        assert cells == {
            "SERVER_CRASHED_RESTART_NOT_SUCCESSFUL": {"mongodb_replica_set_collect.sh"},
            "PERFORMANCE_OTHER": {"mongodb_replica_set_collect.sh"},
            "REPLICA_SET_REPLICATION": {
                "mongodb_repl_lag_check.sh",
                "mongodb_replica_set_collect.sh",
            },
            "BACKUP_PBM": {"mongodb_pbm_diagnostics.sh"},
        }


class TestAtwListTitleFallback:
    """Observe the library's blank-title fallback through the category listing.

    The rule lives on ``BaseSnippet``, so ``_build_summary`` projects the properties
    as they arrive. These cases pin the listing's share of it: a blank title is
    labelled with the filename whatever spelling declares it.
    """

    @pytest.mark.asyncio
    async def test_blank_title_renders_as_the_filename(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Label a blank-titled ATW-tagged snippet with its filename."""
        await _persist_snippet(
            session,
            filename="ops/blank-title.sh",
            meta={
                "title": "",
                "description": "d",
                "service_type": "mysql",
                "diagnostic_categories": ["OVERALL_SLOWNESS"],
            },
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[0]["snippets"][0]["title"] == "ops/blank-title.sh"

    @pytest.mark.asyncio
    async def test_whitespace_only_title_and_description_normalise(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Treat a whitespace-only title and description as blank.

        Whitespace is truthy, so an ``or`` fallback passes it straight through; the
        library rule folds it into the same class as every other blank spelling.
        """
        await _persist_snippet(
            session,
            filename="ops/padded-title.sh",
            meta={
                "title": "   ",
                "description": "  ",
                "service_type": "mysql",
                "diagnostic_categories": ["OVERALL_SLOWNESS"],
            },
        )

        response = await async_api_client.get("/api/apps/atw/")

        assert response.status_code == status.HTTP_200_OK
        summary = response.json()[0]["snippets"][0]
        assert summary["title"] == "ops/padded-title.sh"
        assert summary["description"] == ""


class TestAtwSnippetSearch:
    """Cover GET /api/apps/atw/snippets/.

    Real rows go through ``async_api_client`` rather than a mocked
    ``SnippetManager``, so the searchable set, the approval predicate, and the
    paginated total are the ones the route actually issues.
    """

    SEARCH_URL = "/api/apps/atw/snippets/"
    MATCHING_FILENAMES = ("a.sh", "b.sh", "c.sh")
    PAGE_LIMIT = 2

    @pytest.mark.asyncio
    async def test_matches_on_title(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Return an approved snippet whose title contains the term."""
        await _persist_snippet(
            session,
            filename="diag/a.sh",
            meta={"title": "Slow Query Diagnostics", "description": "Timings."},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "slow"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert [item["name"] for item in payload["items"]] == ["diag/a.sh"]

    @pytest.mark.asyncio
    async def test_matches_on_filename(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Match the filename, proving it is part of the searchable set."""
        await _persist_snippet(
            session,
            filename="diag/slow-query.sh",
            meta={"title": "Unrelated", "description": "Unrelated."},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "slow-query"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["name"] == "diag/slow-query.sh"

    @pytest.mark.asyncio
    async def test_matches_on_description(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Match the description, proving it is part of the searchable set."""
        await _persist_snippet(
            session,
            filename="ops/x.sh",
            meta={"title": "Unrelated", "description": "Collects processlist data."},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "processlist"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["name"] == "ops/x.sh"

    @pytest.mark.asyncio
    async def test_reaches_a_snippet_outside_the_atw_taxonomy(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Return a snippet carrying no category metadata, which the listing never exposes."""
        await _persist_snippet(
            session,
            filename="ops/pt-summary.sh",
            meta={"title": "PT Summary", "description": "Toolkit summary."},
        )

        listing = await async_api_client.get("/api/apps/atw/")
        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "pt summary"}
        )

        assert listing.json() == []
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["name"] == "ops/pt-summary.sh"

    @pytest.mark.asyncio
    async def test_unapproved_snippet_is_excluded(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Withhold an unapproved snippet that matches the term."""
        await _persist_snippet(
            session,
            filename="approved.sh",
            meta={"title": "Galera check", "description": "d"},
        )
        await _persist_snippet(
            session,
            filename="unapproved.sh",
            meta={"title": "Galera check", "description": "d"},
            approved=False,
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "galera"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert [item["name"] for item in payload["items"]] == ["approved.sh"]

    @pytest.mark.asyncio
    async def test_approval_is_not_a_client_input(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ignore an ``approval`` query parameter rather than widening the set.

        The route declares no ``approval`` parameter, so the picker cannot be made
        to offer a snippet that execution would reject.
        """
        await _persist_snippet(
            session,
            filename="approved.sh",
            meta={"title": "Galera check", "description": "d"},
        )
        await _persist_snippet(
            session,
            filename="unapproved.sh",
            meta={"title": "Galera check", "description": "d"},
            approved=False,
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "galera", "approval": "all"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert [item["name"] for item in payload["items"]] == ["approved.sh"]

    @pytest.mark.asyncio
    async def test_item_carries_only_the_summary_fields(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Serve ATW's own summary shape rather than the snippets list row."""
        await _persist_snippet(
            session,
            filename="ops/x.sh",
            meta={"title": "Summary", "description": "d", "service_type": "mysql"},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "summary"}
        )

        assert response.status_code == status.HTTP_200_OK
        item = response.json()["items"][0]
        assert set(item) == {"name", "title", "description", "sudo"}
        assert item == {
            "name": "ops/x.sh",
            "title": "Summary",
            "description": "d",
            "sudo": "never",
        }

    @pytest.mark.asyncio
    async def test_search_reports_the_declared_sudo_requirement(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Publish a mandatory-elevation snippet as ``always`` on the search route."""
        await _persist_snippet(
            session,
            filename="ops/elevated.sh",
            meta={"title": "Elevated", "description": "d", "sudo": "always"},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "elevated"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["sudo"] == "always"

    @pytest.mark.asyncio
    async def test_search_reports_a_boolean_sudo_declaration_as_always(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Resolve ``sudo: true`` to ``always``, as the option parser already does.

        YAML hands Python ``True``, which equals the ``ALWAYS`` member's value of
        ``1`` — so this spelling is mandatory elevation and must warn like one.
        """
        await _persist_snippet(
            session,
            filename="ops/bool-sudo.sh",
            meta={"title": "Bool", "description": "d", "sudo": True},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "bool"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["sudo"] == "always"

    @pytest.mark.asyncio
    async def test_search_reports_a_malformed_sudo_declaration_as_the_default(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Fall back to the configured default when the declaration is garbage.

        ``Snippet.sudo`` swallows the ``ValidationError``, so the summary inherits
        the configured default rather than surfacing an error.
        """
        await _persist_snippet(
            session,
            filename="ops/garbage-sudo.sh",
            meta={"title": "Garbage", "description": "d", "sudo": "not-an-option"},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "garbage"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["sudo"] == "never"

    @pytest.mark.asyncio
    async def test_title_falls_back_to_filename_when_key_absent(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Label a snippet declaring no ``title`` key with its filename."""
        await _persist_snippet(
            session, filename="ops/no-title.sh", meta={"description": "d"}
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "no-title"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["title"] == "ops/no-title.sh"

    @pytest.mark.asyncio
    async def test_title_falls_back_to_filename_when_empty(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Label a snippet declaring an empty ``title`` with its filename.

        The key is present, so a ``dict.get`` default could never absorb it;
        ``Snippet.title`` treats the declared blank as absent instead, and the search
        projection passes that through.
        """
        await _persist_snippet(
            session,
            filename="ops/blank-title.sh",
            meta={"title": "", "description": "d"},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "blank-title"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["title"] == "ops/blank-title.sh"

    @pytest.mark.asyncio
    async def test_title_falls_back_to_filename_when_null(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Label a snippet declaring a valueless ``title`` with its filename."""
        await _persist_snippet(
            session,
            filename="ops/null-title.sh",
            meta={"title": None, "description": "d"},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "null-title"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["title"] == "ops/null-title.sh"

    @pytest.mark.asyncio
    async def test_valueless_description_does_not_fail_the_page(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Serve a snippet declaring a valueless ``description`` as an empty string.

        The key is present, so a ``dict.get`` default could never absorb it, and the
        raw ``None`` would fail the summary model's ``str`` field — turning one
        malformed snippet into a 500 for the whole page. ``Snippet.description``
        answers with the empty string before the projection sees it.
        """
        await _persist_snippet(
            session,
            filename="ops/null-description.sh",
            meta={"title": "Galera", "description": None},
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "galera"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["description"] == ""

    @pytest.mark.asyncio
    async def test_out_of_allowlist_sort_is_rejected(
        self, async_api_client: AsyncClient
    ) -> None:
        """Reject a sort key outside the manager's allowlist with a 422."""
        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "x", "sort": "meta"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["detail"] == "Invalid sort key: 'meta'"

    @pytest.mark.asyncio
    async def test_allowlisted_sort_is_accepted(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Sort by an allowlisted key rather than falling back to the default."""
        await _persist_snippet(
            session, filename="b.sh", meta={"title": "Galera b", "description": "d"}
        )
        await _persist_snippet(
            session, filename="a.sh", meta={"title": "Galera a", "description": "d"}
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "galera", "sort": "filename"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert [item["name"] for item in response.json()["items"]] == ["a.sh", "b.sh"]

    @pytest.mark.asyncio
    async def test_total_counts_the_filtered_set_not_the_page(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Report the whole match count while serving one page of it."""
        for name in self.MATCHING_FILENAMES:
            await _persist_snippet(
                session, filename=name, meta={"title": "Galera", "description": "d"}
            )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "galera", "limit": self.PAGE_LIMIT}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert len(payload["items"]) == self.PAGE_LIMIT
        assert payload["total"] == len(self.MATCHING_FILENAMES)

    @pytest.mark.asyncio
    async def test_paging_is_deterministic_across_boundaries(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Cover every match exactly once across two pages.

        Every row shares an ``approved_at``, so only the spec's unique ``id``
        tie-breaker keeps the boundary stable.
        """
        for name in self.MATCHING_FILENAMES:
            await _persist_snippet(
                session, filename=name, meta={"title": "Galera", "description": "d"}
            )

        first = await async_api_client.get(
            self.SEARCH_URL,
            params={"search": "galera", "offset": 0, "limit": self.PAGE_LIMIT},
        )
        second = await async_api_client.get(
            self.SEARCH_URL,
            params={
                "search": "galera",
                "offset": self.PAGE_LIMIT,
                "limit": len(self.MATCHING_FILENAMES) - self.PAGE_LIMIT,
            },
        )

        names = [item["name"] for item in first.json()["items"]]
        names += [item["name"] for item in second.json()["items"]]
        assert sorted(names) == ["a.sh", "b.sh", "c.sh"]

    @pytest.mark.asyncio
    async def test_no_match_returns_an_empty_page(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Return an empty page rather than an error when nothing matches."""
        await _persist_snippet(
            session, filename="a.sh", meta={"title": "Galera", "description": "d"}
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "zzzz"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["items"] == []
        assert payload["total"] == 0

    @pytest.mark.asyncio
    async def test_omitted_search_returns_every_approved_snippet(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Fall back to all approved snippets when no term is supplied.

        A blank term builds no predicate, so the page is the unfiltered approved
        set rather than an empty one.
        """
        await _persist_snippet(
            session, filename="a.sh", meta={"title": "Galera", "description": "d"}
        )
        await _persist_snippet(
            session,
            filename="b.sh",
            meta={"title": "Other", "description": "d"},
            approved=False,
        )

        response = await async_api_client.get(self.SEARCH_URL)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["name"] == "a.sh"

    @pytest.mark.asyncio
    async def test_wildcard_in_the_term_matches_literally(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Match a literal ``%`` rather than treating it as a LIKE wildcard."""
        await _persist_snippet(
            session, filename="a.sh", meta={"title": "100% CPU", "description": "d"}
        )
        await _persist_snippet(
            session, filename="b.sh", meta={"title": "100 CPU", "description": "d"}
        )

        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "100%"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["name"] == "a.sh"

    @pytest.mark.asyncio
    async def test_limit_above_the_ceiling_is_rejected(
        self, async_api_client: AsyncClient
    ) -> None:
        """Reject a page size above ``MAX_PAGINATION_LIMIT``."""
        response = await async_api_client.get(
            self.SEARCH_URL, params={"search": "x", "limit": MAX_PAGINATION_LIMIT + 1}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_malformed_diagnostic_categories_do_not_affect_search(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Return a snippet whose category tag is malformed, warning about nothing.

        Search never reads ``META_KEY_DIAGNOSTIC_CATEGORIES``, unlike the
        category listing, so a non-list tag is neither a filter nor a
        diagnostic here.
        """
        await _persist_snippet(
            session,
            filename="ops/bad-meta.sh",
            meta={
                "title": "Galera",
                "description": "d",
                "diagnostic_categories": "OVERALL_SLOWNESS",
            },
        )

        with caplog.at_level(logging.WARNING, logger=atw_api_routes.__name__):
            response = await async_api_client.get(
                self.SEARCH_URL, params={"search": "galera"}
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"][0]["name"] == "ops/bad-meta.sh"
        assert [
            record
            for record in caplog.records
            if record.name == atw_api_routes.__name__
        ] == []


class TestAtwSchemaEndpoint:
    """Tests for GET /api/apps/atw/schema."""

    def test_atw_schema_requires_authentication(
        self, unauthenticated_client: TestClient
    ) -> None:
        """Ensure unauthenticated callers receive JSON 401 (mirrors list endpoint)."""
        response = unauthenticated_client.get(
            "/api/apps/atw/schema",
            follow_redirects=False,
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].startswith("application/json")

    def test_atw_schema_returns_plugin_name(self, test_client: TestClient):
        """Ensure the schema endpoint serves the ATW plugin schema."""
        response = test_client.get("/api/apps/atw/schema")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "atw"
        assert data["display_name"] == "Support diagnostics"

    def test_atw_schema_category_browser_has_parent_category_fail_rules(
        self, test_client: TestClient
    ) -> None:
        """Verify the Category Browser section exposes fail_when for parent/category pairs."""
        response = test_client.get("/api/apps/atw/schema")

        assert response.status_code == status.HTTP_200_OK
        section = response.json()["forms"][0]
        fail_when = section["fail_when"]
        assert isinstance(fail_when, list)
        expected_rules = 1 + len(ParentCategory)
        assert len(fail_when) == expected_rules
        assert "parent_category" in fail_when[0]["message"]


INCIDENTS_BASE = "/api/apps/atw/incidents/"
_INCIDENT_NAME_PATTERN = r"Incident \d{4}-\d\d-\d\d \d\d:\d\d"


@pytest_asyncio.fixture
async def seeded_incidents(session: AsyncSession) -> list[AtwIncident]:
    """Seed two incidents with distinct creation times (returned newest-first)."""
    older = await AtwIncidentManager.save(
        session,
        AtwIncident(
            created_by="alice",
            name="older",
            created_at=utc_now() - timedelta(minutes=1),
        ),
    )
    newer = await AtwIncidentManager.save(
        session,
        AtwIncident(created_by="alice", name="newer", created_at=utc_now()),
    )
    return [newer, older]


@pytest_asyncio.fixture
async def seeded_incident(session: AsyncSession) -> AtwIncident:
    """Seed one incident with a known name and support-case reference."""
    return await AtwIncidentManager.save(
        session,
        AtwIncident(created_by="alice", name="Original", case_ref="SN-1"),
    )


_SEEDED_RUN_COUNT = 3
_SEEDED_FAILED_COUNT = 2
#: The statuses ``incident_with_runs`` records, two of which count as failed.
_SEEDED_RUN_STATUSES = (
    TaskHistoryStatusEnum.FAILED,
    TaskHistoryStatusEnum.STALE,
    TaskHistoryStatusEnum.SUCCESS,
)


@pytest_asyncio.fixture
async def incident_with_runs(session: AsyncSession) -> AtwIncident:
    """Seed one incident carrying three resolved runs, two of them failures."""
    incident = await AtwIncidentManager.save(
        session, AtwIncident(created_by="alice", name="with-runs")
    )
    for task_history_id, run_status in enumerate(_SEEDED_RUN_STATUSES, start=1):
        await AtwIncidentExecutionManager.save(
            session,
            AtwIncidentExecution(
                incident_id=incident.id,
                task_history_id=task_history_id,
                snippet_filename="diag.sh",
                terminal_status=run_status.value,
                finished_at=utc_now(),
            ),
        )
    return incident


@pytest_asyncio.fixture
async def incident_with_unresolved_run(session: AsyncSession) -> AtwIncident:
    """Seed one incident whose single run has no recorded outcome yet."""
    incident = await AtwIncidentManager.save(
        session, AtwIncident(created_by="alice", name="in-flight")
    )
    await AtwIncidentExecutionManager.save(
        session,
        AtwIncidentExecution(
            incident_id=incident.id,
            task_history_id=1,
            snippet_filename="diag.sh",
        ),
    )
    return incident


@pytest_asyncio.fixture
async def incident_with_executions(session: AsyncSession) -> AtwIncident:
    """Seed one incident owning two execution rows."""
    incident = await AtwIncidentManager.save(
        session, AtwIncident(created_by="alice", name="With executions")
    )
    for task_history_id in (1, 2):
        await AtwIncidentExecutionManager.save(
            session,
            AtwIncidentExecution(
                incident_id=incident.id,
                task_history_id=task_history_id,
                snippet_filename="diag.sh",
            ),
        )
    return incident


class TestAtwIncidentCreate:
    """Check the POST /api/apps/atw/incidents/ route."""

    def test_create_without_name_generates_default(
        self, api_client: TestClient, regular_user: CasdoorUser
    ) -> None:
        """Ensure an omitted name gets the server default and created_by is stamped."""
        response = api_client.post(INCIDENTS_BASE, json={})

        assert response.status_code == status.HTTP_201_CREATED
        payload = response.json()
        assert re.fullmatch(_INCIDENT_NAME_PATTERN, payload["name"])
        assert payload["created_by"] == regular_user.username
        assert payload["case_ref"] is None

    def test_create_with_custom_fields_echoes_values(
        self, api_client: TestClient
    ) -> None:
        """Ensure a custom name and case persist and a UUID id is returned."""
        response = api_client.post(
            INCIDENTS_BASE,
            json={"name": "Prod outage", "case_ref": "CS123"},
        )

        assert response.status_code == status.HTTP_201_CREATED
        payload = response.json()
        assert payload["name"] == "Prod outage"
        assert payload["case_ref"] == "CS123"
        assert UUID(payload["id"])

    def test_create_cookie_only_is_rejected(
        self, cookie_only_client: TestClient
    ) -> None:
        """Ensure a cookie-only create (no Bearer header) is rejected with 401."""
        response = cookie_only_client.post(INCIDENTS_BASE, json={"name": "x"})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["detail"] == BEARER_REQUIRED_DETAIL


class TestAtwIncidentList:
    """Check the GET /api/apps/atw/incidents/ listing route."""

    def test_list_returns_incidents_newest_first(
        self, api_client: TestClient, seeded_incidents: list[AtwIncident]
    ) -> None:
        """Ensure the listing paginates and orders incidents newest-first."""
        response = api_client.get(INCIDENTS_BASE)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == len(seeded_incidents)
        assert [item["name"] for item in payload["items"]] == ["newer", "older"]

    def test_list_empty_returns_zero_total(self, api_client: TestClient) -> None:
        """Ensure an empty listing returns an empty page with a zero total."""
        response = api_client.get(INCIDENTS_BASE)

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["items"] == []
        assert payload["total"] == 0

    def test_list_pagination_window_echoed(
        self, api_client: TestClient, seeded_incidents: list[AtwIncident]
    ) -> None:
        """Ensure offset/limit narrow the page and are echoed in the envelope."""
        response = api_client.get(INCIDENTS_BASE, params={"limit": 1, "offset": 1})

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["total"] == len(seeded_incidents)
        assert len(payload["items"]) == 1
        assert payload["offset"] == 1
        assert payload["limit"] == 1
        assert payload["items"][0]["name"] == "older"

    @pytest.mark.parametrize(
        "params",
        [{"limit": 0}, {"offset": -1}, {"limit": 999}],
    )
    def test_list_rejects_out_of_bounds_pagination(
        self, api_client: TestClient, params: dict[str, int]
    ) -> None:
        """Ensure pagination bounds (limit 1-200, offset >= 0) are enforced."""
        response = api_client.get(INCIDENTS_BASE, params=params)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestAtwIncidentRetrieve:
    """Check the GET /api/apps/atw/incidents/{incident_id} route."""

    def test_get_existing_incident(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure an existing incident is retrievable by id."""
        response = api_client.get(f"{INCIDENTS_BASE}{seeded_incident.id}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(seeded_incident.id)

    def test_get_unknown_incident_returns_404(self, api_client: TestClient) -> None:
        """Ensure a random incident id returns 404."""
        response = api_client.get(f"{INCIDENTS_BASE}{uuid4()}")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAtwIncidentUpdate:
    """Check the PATCH /api/apps/atw/incidents/{incident_id} route."""

    def test_rename_leaves_case_ref_untouched(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure a name-only PATCH does not clear the untouched case reference."""
        response = api_client.patch(
            f"{INCIDENTS_BASE}{seeded_incident.id}", json={"name": "Renamed"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["name"] == "Renamed"
        assert payload["case_ref"] == "SN-1"

    def test_set_case_ref_leaves_name_untouched(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure a case-only PATCH does not overwrite the untouched name."""
        response = api_client.patch(
            f"{INCIDENTS_BASE}{seeded_incident.id}",
            json={"case_ref": "CS999"},
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["case_ref"] == "CS999"
        assert payload["name"] == "Original"

    def test_empty_name_is_rejected(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure renaming to an empty string is rejected by NonEmptyStr."""
        response = api_client.patch(
            f"{INCIDENTS_BASE}{seeded_incident.id}", json={"name": ""}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_explicit_null_name_is_rejected_without_touching_db(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure an explicit null name is a 422 (not a 500) and leaves the DB intact."""
        response = api_client.patch(
            f"{INCIDENTS_BASE}{seeded_incident.id}", json={"name": None}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        unchanged = api_client.get(f"{INCIDENTS_BASE}{seeded_incident.id}")
        assert unchanged.json()["name"] == "Original"

    def test_update_unknown_incident_returns_404(self, api_client: TestClient) -> None:
        """Ensure updating a random incident id returns 404."""
        response = api_client.patch(f"{INCIDENTS_BASE}{uuid4()}", json={"name": "x"})

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAtwIncidentDelete:
    """Check the DELETE /api/apps/atw/incidents/{incident_id} route."""

    def test_delete_existing_incident(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure deleting an existing incident returns 204."""
        response = api_client.delete(f"{INCIDENTS_BASE}{seeded_incident.id}")

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_unknown_incident_returns_404(self, api_client: TestClient) -> None:
        """Ensure deleting a random incident id returns 404."""
        response = api_client.delete(f"{INCIDENTS_BASE}{uuid4()}")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_delete_cascades_execution_rows(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        incident_with_executions: AtwIncident,
    ) -> None:
        """Ensure deleting an incident cascades away its execution rows."""
        response = await async_api_client.delete(
            f"{INCIDENTS_BASE}{incident_with_executions.id}"
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        remaining = await AtwIncidentExecutionManager.count(
            session, incident_id=incident_with_executions.id
        )
        assert remaining == 0


class TestAtwIncidentCloseReopen:
    """Check close and reopen action routes."""

    def test_close_stamps_closed_at(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure closing an open incident stamps closed_at and returns it."""
        response = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/close/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["closed_at"] is not None
        assert payload["id"] == str(seeded_incident.id)

    def test_double_close_returns_409_and_preserves_stamp(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure closing an already-closed incident is rejected without changing the stamp."""
        first = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/close/")
        assert first.status_code == status.HTTP_200_OK
        first_stamp = first.json()["closed_at"]

        second = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/close/")

        assert second.status_code == status.HTTP_409_CONFLICT
        unchanged = api_client.get(f"{INCIDENTS_BASE}{seeded_incident.id}")
        assert unchanged.json()["closed_at"] == first_stamp

    def test_reopen_clears_closed_at(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure reopening a closed incident clears closed_at."""
        close_response = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/close/")
        assert close_response.status_code == status.HTTP_200_OK

        response = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/reopen/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["closed_at"] is None

    def test_double_reopen_returns_409(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure reopening an already-open incident is rejected."""
        response = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/reopen/")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_patch_and_delete_still_work_when_closed(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure rename and delete remain available on a closed incident."""
        close_response = api_client.post(f"{INCIDENTS_BASE}{seeded_incident.id}/close/")
        assert close_response.status_code == status.HTTP_200_OK

        patch_response = api_client.patch(
            f"{INCIDENTS_BASE}{seeded_incident.id}", json={"name": "Closed but renamed"}
        )
        assert patch_response.status_code == status.HTTP_200_OK
        assert patch_response.json()["name"] == "Closed but renamed"

        delete_response = api_client.delete(f"{INCIDENTS_BASE}{seeded_incident.id}")
        assert delete_response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.asyncio
    async def test_close_handler_returns_response_model(
        self, session: AsyncSession, seeded_incident: AtwIncident
    ) -> None:
        """Return a stamped ``AtwIncidentResponse`` when closing an open incident."""
        result = await atw_api_routes.atw_close_incident(session, seeded_incident)

        assert isinstance(result, AtwIncidentResponse)
        assert result.closed_at is not None
        assert result.id == seeded_incident.id

    @pytest.mark.asyncio
    async def test_reopen_handler_returns_response_model(
        self, session: AsyncSession, seeded_incident: AtwIncident
    ) -> None:
        """Return a cleared ``AtwIncidentResponse`` when reopening a closed incident."""
        seeded_incident.closed_at = utc_now()
        closed = await AtwIncidentManager.save(session, seeded_incident)

        result = await atw_api_routes.atw_reopen_incident(session, closed)

        assert isinstance(result, AtwIncidentResponse)
        assert result.closed_at is None
        assert result.id == seeded_incident.id


class TestAtwIncidentRunAggregates:
    """Check the run counts and last-activity timestamp all six routes serve."""

    def test_list_serves_run_aggregates(
        self, api_client: TestClient, incident_with_runs: AtwIncident
    ) -> None:
        """Ensure the listing carries per-row run totals and a last-activity time."""
        response = api_client.get(INCIDENTS_BASE)

        assert response.status_code == status.HTTP_200_OK
        row = response.json()["items"][0]
        assert row["run_count"] == _SEEDED_RUN_COUNT
        assert row["failed_run_count"] == _SEEDED_FAILED_COUNT
        assert row["last_activity_at"] is not None

    def test_list_issues_no_upstream_request(
        self,
        api_client: TestClient,
        incident_with_runs: AtwIncident,
        mock_task_api_dep: AsyncMock,
        mocker: MockerFixture,
    ) -> None:
        """Ensure rendering a page costs no task-history call, however many runs.

        The list route requests no Tasks API dependency, so the overridden client
        alone could not see a call made through a client built outside dependency
        injection; the transport-level spies cover that path too.
        """
        transport_get = mocker.spy(RemoteAPI, "get")
        transport_post = mocker.spy(RemoteAPI, "post")

        response = api_client.get(INCIDENTS_BASE)

        assert response.status_code == status.HTTP_200_OK
        mock_task_api_dep.get.assert_not_called()
        mock_task_api_dep.post.assert_not_called()
        transport_get.assert_not_called()
        transport_post.assert_not_called()

    def test_list_page_issues_one_aggregate_query(
        self,
        api_client: TestClient,
        incident_with_runs: AtwIncident,
        seeded_incidents: list[AtwIncident],
        mocker: MockerFixture,
    ) -> None:
        """Ensure a multi-incident page is summarized by one grouped query."""
        spy = mocker.spy(AtwIncidentExecutionManager, "aggregate_by_incident")

        response = api_client.get(INCIDENTS_BASE)

        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()["items"]) > 1
        assert spy.call_count == 1

    def test_detail_serves_run_aggregates(
        self, api_client: TestClient, incident_with_runs: AtwIncident
    ) -> None:
        """Ensure the detail route serves the same totals as the listing row."""
        response = api_client.get(f"{INCIDENTS_BASE}{incident_with_runs.id}")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["run_count"] == _SEEDED_RUN_COUNT
        assert payload["failed_run_count"] == _SEEDED_FAILED_COUNT

    def test_create_serves_zeroed_aggregates(self, api_client: TestClient) -> None:
        """Ensure a fresh incident reports no runs and its own creation time."""
        response = api_client.post(INCIDENTS_BASE, json={"name": "brand new"})

        assert response.status_code == status.HTTP_201_CREATED
        payload = response.json()
        assert payload["run_count"] == 0
        assert payload["failed_run_count"] == 0
        assert payload["last_activity_at"] == payload["created_at"]

    def test_patch_preserves_aggregates(
        self, api_client: TestClient, incident_with_runs: AtwIncident
    ) -> None:
        """Ensure renaming an incident does not blank its run totals."""
        response = api_client.patch(
            f"{INCIDENTS_BASE}{incident_with_runs.id}", json={"name": "renamed"}
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["name"] == "renamed"
        assert payload["run_count"] == _SEEDED_RUN_COUNT
        assert payload["failed_run_count"] == _SEEDED_FAILED_COUNT

    @pytest.mark.asyncio
    async def test_last_activity_tracks_an_edit_made_after_the_last_run(
        self, async_api_client: AsyncClient, session: AsyncSession
    ) -> None:
        """Ensure editing an incident after its last run moves its last-activity time.

        The incident's own ``updated_at`` is one of the three sources, so an edit is
        activity even when no run has happened since. The run is seeded well in the
        past because ``utc_now`` truncates to whole seconds, which would otherwise
        tie the two timestamps.
        """
        long_ago = utc_now() - timedelta(hours=2)
        incident = await AtwIncidentManager.save(
            session,
            AtwIncident(created_by="alice", name="stale-runs", created_at=long_ago),
        )
        await AtwIncidentExecutionManager.save(
            session,
            AtwIncidentExecution(
                incident_id=incident.id,
                task_history_id=1,
                snippet_filename="diag.sh",
                terminal_status=TaskHistoryStatusEnum.SUCCESS.value,
                finished_at=long_ago,
            ),
        )
        before = await async_api_client.get(f"{INCIDENTS_BASE}{incident.id}")

        renamed = await async_api_client.patch(
            f"{INCIDENTS_BASE}{incident.id}", json={"name": "touched"}
        )

        assert renamed.status_code == status.HTTP_200_OK
        assert renamed.json()["last_activity_at"] > before.json()["last_activity_at"]

    def test_close_serves_aggregates(
        self, api_client: TestClient, incident_with_runs: AtwIncident
    ) -> None:
        """Ensure closing an incident still reports its run totals."""
        response = api_client.post(f"{INCIDENTS_BASE}{incident_with_runs.id}/close/")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["closed_at"] is not None
        assert payload["run_count"] == _SEEDED_RUN_COUNT
        assert payload["failed_run_count"] == _SEEDED_FAILED_COUNT

    @pytest.mark.asyncio
    async def test_reopen_serves_aggregates(
        self,
        async_api_client: AsyncClient,
        session: AsyncSession,
        incident_with_runs: AtwIncident,
    ) -> None:
        """Ensure reopening an incident still reports its run totals."""
        incident_with_runs.closed_at = utc_now()
        await AtwIncidentManager.save(session, incident_with_runs)

        response = await async_api_client.post(
            f"{INCIDENTS_BASE}{incident_with_runs.id}/reopen/"
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["closed_at"] is None
        assert payload["run_count"] == _SEEDED_RUN_COUNT

    def test_run_less_incident_reports_its_own_timestamps(
        self, api_client: TestClient, seeded_incident: AtwIncident
    ) -> None:
        """Ensure last activity is never null, so the client needs no empty state."""
        response = api_client.get(f"{INCIDENTS_BASE}{seeded_incident.id}")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["run_count"] == 0
        assert payload["last_activity_at"] is not None

    def test_unresolved_runs_are_counted_but_not_failed(
        self, api_client: TestClient, incident_with_unresolved_run: AtwIncident
    ) -> None:
        """Ensure a still-running run raises run_count without implying a failure."""
        response = api_client.get(f"{INCIDENTS_BASE}{incident_with_unresolved_run.id}")

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["run_count"] == 1
        assert payload["failed_run_count"] == 0
        assert payload["last_activity_at"] is not None
