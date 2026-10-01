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

import logging

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlmodel.ext.asyncio.session import AsyncSession

from app.inventory.constants import RetirableEntityName
from app.inventory.crud import (
    COLLECTION_ORDER,
    CollectionBatch,
    HostSystemObservationManager,
    RetiredInclusiveServiceManager,
    RetiredInclusiveTableManager,
)
from app.inventory.models import HostSystemObservation, Node, Schema, Service, Table
from app.inventory.routes import collection
from tests.app.inventory.conftest import (
    CUTOFF as CUTOFF_AT,
)
from tests.app.inventory.conftest import (
    EMPTY_BATCH,
    retirable_row_counts,
    retire_in_place,
    RETIRED_AT,
)

COLLECT_URL = "/collection/collect"
CUTOFF = CUTOFF_AT.isoformat()


@pytest.mark.asyncio
async def test_dry_run_reports_without_deleting(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """List the eligible ids but leave every row in place."""
    response = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": True}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_real_run_deletes_what_the_dry_run_reported(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Delete exactly the entities the equivalent dry run listed."""
    dry_run = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": True}
    )
    real = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
    )

    assert real.status_code == status.HTTP_200_OK
    assert real.json()["deleted"] == dry_run.json()["deleted"]
    assert await retirable_row_counts(session) == (0, 0, 0, 0)


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
    response = test_client.post(
        COLLECT_URL,
        json={
            "retired_before": CUTOFF,
            "keep": {"service": [service.id]},
            "dry_run": False,
        },
    )

    assert response.json()["deleted"] == {
        "table": [table.id],
        "schema": [schema.id],
        "service": [],
        "node": [],
    }
    assert await retirable_row_counts(session) == (1, 1, 0, 0)


@pytest.mark.asyncio
async def test_cutoff_is_honoured(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Delete nothing when every tombstone is younger than the cutoff."""
    response = test_client.post(
        COLLECT_URL, json={"retired_before": "2025-12-01T00:00:00Z", "dry_run": False}
    )

    assert response.json()["deleted"] == EMPTY_BATCH
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


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
    response = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"] == EMPTY_BATCH
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


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

    response = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "limit": 1, "dry_run": False}
    )

    body = response.json()
    assert body["deleted"]["table"] == [table.id]
    assert body["remaining"] is True
    assert await RetiredInclusiveTableManager.count(session) == 1


@pytest.mark.asyncio
async def test_re_running_collects_nothing(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Report an empty batch on a second identical call rather than erroring."""
    test_client.post(COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False})
    response = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
    )

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
    test_client.post(COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False})

    assert await HostSystemObservationManager.count(session) == 0


@pytest.mark.asyncio
async def test_keep_larger_than_the_candidate_set_is_a_no_op(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Accept ids that match no row without retaining anything real."""
    response = test_client.post(
        COLLECT_URL,
        json={
            "retired_before": CUTOFF,
            "keep": {"node": [4001, 4002], "service": [4003]},
            "dry_run": False,
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]


@pytest.mark.asyncio
async def test_an_entity_revived_before_the_real_call_is_not_collected(
    test_client: TestClient, session: AsyncSession, retired_tree: Node, table: Table
) -> None:
    """Skip a tombstone that went active between the dry run and the delete."""
    dry_run = test_client.post(
        COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": True}
    )
    assert table.id in dry_run.json()["deleted"]["table"]
    await RetiredInclusiveTableManager.revive(session, table)

    test_client.post(COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False})

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
        test_client.post(COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False})

    assert await retirable_row_counts(session) == (1, 1, 0, 0)


@pytest.mark.asyncio
async def test_omitting_dry_run_reports_without_deleting(
    test_client: TestClient, session: AsyncSession, retired_tree: Node
) -> None:
    """Treat an omitted mode as a dry run, never as an irreversible delete."""
    response = test_client.post(COLLECT_URL, json={"retired_before": CUTOFF})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"]["node"] == [retired_tree.id]
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"keeps": {}}, {"dryrun": True}, {"limits": 5}])
async def test_an_unknown_field_is_refused(
    test_client: TestClient, session: AsyncSession, retired_tree: Node, payload: dict
) -> None:
    """Refuse a misspelled field rather than silently reading it as omitted.

    A misspelled ``keep`` would otherwise arrive as an empty retained set and a
    misspelled ``dry_run`` as a real delete, both answered 200.
    """
    response = test_client.post(COLLECT_URL, json={"retired_before": CUTOFF, **payload})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


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

    body = test_client.post(
        COLLECT_URL,
        json={"retired_before": CUTOFF, "limit": 1, "dry_run": False},
    ).json()

    assert body["deleted"] == {**EMPTY_BATCH, "table": [table.id]}
    assert body["remaining"] is True
    assert await retirable_row_counts(session) == (1, 1, 1, 1)


ROUTE_LOGGER = collection.logger.name


class TestCollectionAdapter:
    """Test that the route delegates the walk and logs only real deletions."""

    @pytest.mark.asyncio
    async def test_a_real_run_logs_each_collected_type(
        self,
        test_client: TestClient,
        caplog: pytest.LogCaptureFixture,
        retired_tree: Node,
    ) -> None:
        """Log one line per type the delete ran on, in walk order."""
        with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
            test_client.post(
                COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
            )

        assert [r.getMessage() for r in caplog.records if r.name == ROUTE_LOGGER] == [
            f"Collected 1 retired {name} entities" for name, _ in COLLECTION_ORDER
        ]

    @pytest.mark.asyncio
    async def test_a_dry_run_logs_nothing(
        self,
        test_client: TestClient,
        caplog: pytest.LogCaptureFixture,
        retired_tree: Node,
    ) -> None:
        """Log no deletion when nothing was deleted."""
        with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
            test_client.post(COLLECT_URL, json={"retired_before": CUTOFF})

        assert not [r for r in caplog.records if r.name == ROUTE_LOGGER]

    @pytest.mark.asyncio
    async def test_a_type_with_nothing_collected_is_not_logged(
        self,
        test_client: TestClient,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
        table: Table,
    ) -> None:
        """Log only the types a delete actually ran on."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)

        with caplog.at_level(logging.INFO, logger=ROUTE_LOGGER):
            test_client.post(
                COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
            )

        assert [r.getMessage() for r in caplog.records if r.name == ROUTE_LOGGER] == [
            "Collected 1 retired table entities"
        ]

    @pytest.mark.asyncio
    async def test_deletions_before_a_failure_are_still_logged(
        self,
        test_client: TestClient,
        mocker: MockerFixture,
        caplog: pytest.LogCaptureFixture,
        retired_tree: Node,
    ) -> None:
        """Log each committed delete as it lands, not only once the walk ends."""
        mocker.patch.object(
            RetiredInclusiveServiceManager,
            "collect",
            autospec=True,
            side_effect=RuntimeError("interrupted"),
        )

        with (
            caplog.at_level(logging.INFO, logger=ROUTE_LOGGER),
            pytest.raises(RuntimeError, match="interrupted"),
        ):
            test_client.post(
                COLLECT_URL, json={"retired_before": CUTOFF, "dry_run": False}
            )

        assert [r.getMessage() for r in caplog.records if r.name == ROUTE_LOGGER] == [
            "Collected 1 retired table entities",
            "Collected 1 retired schema entities",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "dry_run"),
        [({}, True), ({"dry_run": False}, False)],
        ids=["omitted", "explicit"],
    )
    async def test_the_body_is_handed_to_the_walk(
        self,
        test_client: TestClient,
        mocker: MockerFixture,
        payload: dict[str, bool],
        *,
        dry_run: bool,
    ) -> None:
        """Pass every body field through, defaulting an omitted mode to dry."""
        walk = mocker.patch.object(
            collection,
            "collect_retirable_entities",
            autospec=True,
            return_value=CollectionBatch(deleted=EMPTY_BATCH, remaining=False),
        )

        response = test_client.post(
            COLLECT_URL,
            json={
                "retired_before": CUTOFF,
                "keep": {"node": [7]},
                "limit": 3,
                **payload,
            },
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"deleted": EMPTY_BATCH, "remaining": False}
        walk.assert_awaited_once_with(
            mocker.ANY,
            retired_before=CUTOFF_AT,
            keep={RetirableEntityName.NODE: [7]},
            limit=3,
            dry_run=dry_run,
            on_collected=mocker.ANY,
        )
