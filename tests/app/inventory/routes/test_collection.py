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

"""Define tests for the inventory collection route."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone, UTC
from typing import Any

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from httpx import Response
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.utils.date_time import make_datetime_utc
from app.inventory.crud import (
    HostSystemObservationManager,
    RetiredInclusiveNodeManager,
    RetiredInclusiveSchemaManager,
    RetiredInclusiveServiceManager,
    RetiredInclusiveTableManager,
)
from app.inventory.models import HostSystemObservation, Node, Schema, Service, Table
from tests.app.inventory.conftest import confirmed_split, retire_in_place

COLLECT_URL = "/collection/collect"
RETIRED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = "2026-02-01T00:00:00Z"
#: Predates every tombstone here, so a standing link's pin always holds.
LINK_CUTOFF = "2025-01-01T00:00:00Z"
EMPTY_BATCH = {"table": [], "schema": [], "service": [], "node": []}


@pytest_asyncio.fixture
async def retired_tree(
    session: AsyncSession,
    node: Node,
    service: Service,
    schema: Schema,
    table: Table,
) -> Node:
    """Retire a whole node subtree well before the tests' cutoff."""
    for entity in (table, schema, service, node):
        await retire_in_place(session, entity, retired_at=RETIRED_AT)
    return node


async def _row_counts(session: AsyncSession) -> tuple[int, int, int, int]:
    """Count every row of each retirable type, tombstones included."""
    return (
        await RetiredInclusiveNodeManager.count(session),
        await RetiredInclusiveServiceManager.count(session),
        await RetiredInclusiveSchemaManager.count(session),
        await RetiredInclusiveTableManager.count(session),
    )


def _collect(test_client: TestClient, **fields: Any) -> Response:
    """Post a collection request with both cutoffs defaulted, overriding any field.

    :param test_client: The client to post through.
    :param fields: The request fields to set or override.
    :return: The raw response.
    """
    return test_client.post(
        COLLECT_URL,
        json={
            "retired_before": CUTOFF,
            "link_pin_retired_before": LINK_CUTOFF,
            **fields,
        },
    )


@pytest.mark.asyncio
async def test_dry_run_reports_without_deleting(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """List the eligible ids but leave every row in place."""
    response = _collect(test_client, dry_run=True)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]
    assert await _row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_real_run_deletes_what_the_dry_run_reported(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Delete exactly the entities the equivalent dry run listed."""
    dry_run = _collect(test_client, dry_run=True)
    real = _collect(test_client, dry_run=False)

    assert real.status_code == status.HTTP_200_OK
    assert real.json()["deleted"] == dry_run.json()["deleted"]
    assert await _row_counts(session) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_kept_service_and_its_node_survive(
    test_client: TestClient,
    session: AsyncSession,
    retired_tree: Node,
    service: Service,
    schema: Schema,
    table: Table,
) -> None:
    """Keep a referenced service and its node while collecting below it."""
    response = _collect(test_client, keep={"service": [service.id]}, dry_run=False)

    assert response.json()["deleted"] == {
        "table": [table.id],
        "schema": [schema.id],
        "service": [],
        "node": [],
    }
    assert await _row_counts(session) == (1, 1, 0, 0)


@pytest.mark.asyncio
async def test_cutoff_is_honoured(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Delete nothing when every tombstone is younger than the cutoff."""
    response = _collect(
        test_client, retired_before="2025-12-01T00:00:00Z", dry_run=False
    )

    assert response.json()["deleted"] == EMPTY_BATCH
    assert await _row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_active_rows_are_never_touched(
    test_client: TestClient,
    session: AsyncSession,
    node: Node,
    service: Service,
    schema: Schema,
    table: Table,
) -> None:
    """Leave a fully active inventory exactly as it was."""
    response = _collect(test_client, dry_run=False)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"] == EMPTY_BATCH
    assert await _row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_limit_caps_the_batch_and_reports_remaining(
    test_client: TestClient,
    session: AsyncSession,
    schema: Schema,
    table: Table,
    second_table: Table,
) -> None:
    """Collect one entity per type and report that more are waiting."""
    await retire_in_place(session, table, retired_at=RETIRED_AT)
    await retire_in_place(session, second_table, retired_at=RETIRED_AT)

    response = _collect(test_client, limit=1, dry_run=False)

    body = response.json()
    assert body["deleted"]["table"] == [table.id]
    assert body["remaining"] is True
    assert await RetiredInclusiveTableManager.count(session) == 1


@pytest.mark.asyncio
async def test_re_running_collects_nothing(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Report an empty batch on a second identical call rather than erroring."""
    _collect(test_client, dry_run=False)
    response = _collect(test_client, dry_run=False)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"deleted": EMPTY_BATCH, "remaining": False}


@pytest.mark.asyncio
async def test_observation_rows_cascade_with_their_node(
    test_client: TestClient,
    session: AsyncSession,
    retired_tree: Node,
    host_observation: HostSystemObservation,
) -> None:
    """Take a node's observation row with the node itself."""
    _collect(test_client, dry_run=False)

    assert await HostSystemObservationManager.count(session) == 0


@pytest.mark.asyncio
async def test_keep_larger_than_the_candidate_set_is_a_no_op(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Accept ids that match no row without retaining anything real."""
    response = _collect(
        test_client, keep={"node": [4001, 4002], "service": [4003]}, dry_run=False
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]


@pytest.mark.asyncio
async def test_an_entity_revived_before_the_real_call_is_not_collected(
    test_client: TestClient, session: AsyncSession, retired_tree: Node, table: Table
) -> None:
    """Skip a tombstone that went active between the dry run and the delete."""
    dry_run = _collect(test_client, dry_run=True)
    assert table.id in dry_run.json()["deleted"]["table"]
    await RetiredInclusiveTableManager.revive(session, table)

    _collect(test_client, dry_run=False)

    assert await RetiredInclusiveTableManager.count(session) == 1


@pytest.mark.asyncio
async def test_an_interrupted_run_leaves_no_active_row_under_a_deleted_ancestor(
    test_client: TestClient,
    session: AsyncSession,
    mocker: MockerFixture,
    retired_tree: Node,
    table: Table,
    schema: Schema,
) -> None:
    """Leave only deleted descendants behind when a run dies mid-walk.

    Deepest-first is what gives this property: the tables and schemas are gone
    and their ancestors survive, which is the safe direction. The mirror — an
    ancestor deleted while a descendant survives — is what would orphan rows.
    """
    mocker.patch.object(
        RetiredInclusiveServiceManager,
        "collect",
        autospec=True,
        side_effect=RuntimeError("interrupted"),
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        _collect(test_client, dry_run=False)

    assert await _row_counts(session) == (1, 1, 0, 0)


@pytest.mark.asyncio
async def test_omitting_dry_run_reports_without_deleting(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Treat an omitted mode as a dry run, never as an irreversible delete."""
    response = _collect(test_client)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]
    assert await _row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"keeps": {}}, {"dryrun": True}, {"limits": 5}])
async def test_an_unknown_field_is_refused(
    test_client: TestClient, session: AsyncSession, retired_tree: Node, payload: dict
) -> None:
    """Refuse a misspelled field rather than silently reading it as omitted.

    A misspelled ``keep`` would otherwise arrive as an empty retained set and a
    misspelled ``dry_run`` as a real delete, both answered 200.
    """
    response = _collect(test_client, **payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert await _row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_a_full_batch_stops_before_its_ancestors(
    test_client: TestClient,
    session: AsyncSession,
    node: Node,
    service: Service,
    schema: Schema,
    table: Table,
    second_table: Table,
) -> None:
    """Report every row the call removed, cascades included.

    Collecting the schema would cascade away the table the cap excluded, and
    that id would never appear in any response — leaving the caller unable to
    clear its bookkeeping for a row that is gone. The walk stops instead, and
    the ancestors are collected once their subtree drains.
    """
    for entity in (table, second_table, schema, service, node):
        await retire_in_place(session, entity, retired_at=RETIRED_AT)

    body = _collect(test_client, limit=1, dry_run=False).json()

    assert body["deleted"] == {**EMPTY_BATCH, "table": [table.id]}
    assert body["remaining"] is True
    assert await _row_counts(session) == (1, 1, 1, 1)


FAR_FUTURE = "2999-01-01T00:00:00Z"
BRAZIL = timezone(timedelta(hours=-3))


@pytest_asyncio.fixture
async def linked_successor(
    session: AsyncSession, split_nodes: tuple[Node, Node]
) -> Node:
    """Confirm a split node pair and return the successor tombstone it leaves."""
    _, successor = await confirmed_split(session, split_nodes)
    return successor


class TestLinkPinRetiredBefore:
    """Test the request field bounding how long a standing link pins a tombstone."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="omitted"),
            pytest.param({"link_pin_retired_befor": FAR_FUTURE}, id="misspelled"),
            pytest.param({"link_pin_retired_before": "soon"}, id="not-a-datetime"),
            pytest.param({"link_pin_retired_before": None}, id="null"),
        ],
    )
    async def test_a_missing_or_malformed_cutoff_is_refused(
        self,
        test_client: TestClient,
        session: AsyncSession,
        retired_tree: Node,
        payload: dict[str, Any],
    ) -> None:
        """Fail closed rather than read an absent pin cutoff as either extreme.

        :param payload: The pin-cutoff part of the request body.
        """
        response = test_client.post(
            COLLECT_URL,
            json={"retired_before": CUTOFF, "dry_run": False, **payload},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert await _row_counts(session) == (1, 1, 1, 1)

    @pytest.mark.asyncio
    async def test_a_young_link_keeps_its_successor(
        self, test_client: TestClient, linked_successor: Node
    ) -> None:
        """Leave a successor whose link has not yet stood past the pin cutoff."""
        body = _collect(test_client, retired_before=FAR_FUTURE, dry_run=False).json()

        assert linked_successor.id not in body["deleted"]["node"]

    @pytest.mark.asyncio
    async def test_an_old_link_releases_its_successor(
        self, test_client: TestClient, session: AsyncSession, linked_successor: Node
    ) -> None:
        """Report the released successor on a dry run, then delete it for real."""
        request = {"retired_before": FAR_FUTURE, "link_pin_retired_before": FAR_FUTURE}

        dry = _collect(test_client, **request, dry_run=True).json()
        real = _collect(test_client, **request, dry_run=False).json()

        assert dry["deleted"]["node"] == [linked_successor.id]
        assert real["deleted"]["node"] == [linked_successor.id]
        assert await RetiredInclusiveNodeManager.count(session) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "render",
        [
            pytest.param(lambda at: at.replace(tzinfo=None).isoformat(), id="naive"),
            pytest.param(lambda at: at.astimezone(BRAZIL).isoformat(), id="offset"),
        ],
    )
    @pytest.mark.parametrize(
        ("offset", "pinned"),
        [
            pytest.param(timedelta(0), True, id="at-the-cutoff"),
            pytest.param(timedelta(seconds=1), False, id="past-the-cutoff"),
        ],
    )
    async def test_the_cutoff_is_compared_in_utc(
        self,
        test_client: TestClient,
        linked_successor: Node,
        *,
        render: Callable[[datetime], str],
        offset: timedelta,
        pinned: bool,
    ) -> None:
        """Read a naive cutoff as UTC and convert an offset one before comparing.

        :param render: How the cutoff is spelled on the wire.
        :param offset: How far past the successor's ``retired_at`` the cutoff is.
        :param pinned: Whether the successor must still be held back.
        """
        assert linked_successor.retired_at is not None
        cutoff = make_datetime_utc(linked_successor.retired_at) + offset

        body = _collect(
            test_client,
            retired_before=FAR_FUTURE,
            link_pin_retired_before=render(cutoff),
            dry_run=True,
        ).json()

        assert (linked_successor.id not in body["deleted"]["node"]) is pinned
