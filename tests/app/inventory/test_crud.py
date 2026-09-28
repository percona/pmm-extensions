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

"""Test inventory CRUD manager database-layer behavior."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, UTC

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.utils import get_async_session_maker_from_engine
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPNotFoundException,
)
from app.core.pagination import Pagination
from app.core.utils.date_time import utc_now
from app.inventory.constants import (
    ACTIVE_RETIREMENT_KEY,
    RetirableEntityName,
    SYNC_ERROR_MAX_LENGTH,
)
from app.inventory.crud import (
    ExternalIdentityAliasManager,
    HostSystemObservationManager,
    IdentityLinkDecisionManager,
    NodeManager,
    RetiredInclusiveNodeManager,
    RetiredInclusiveServiceManager,
    RetiredInclusiveTableManager,
    SchemaManager,
    ServiceManager,
    ServiceSystemObservationManager,
    SyncHealthManagerMixin,
    TableManager,
)
from app.inventory.models import (
    HostSystemObservation,
    IdentityLinkDecision,
    IdentityLinkDecisionEnum,
    Node,
    RetirableSQLModel,
    Schema,
    Service,
    ServiceSystemObservation,
    SourceEnum,
    SyncHealthWrite,
    SyncOutcomeEnum,
    Table,
)
from tests.app.factories import NodeWriteFactory, ServiceWriteFactory
from tests.app.inventory.conftest import retire_in_place

PAGE = Pagination(offset=0, limit=50)

#: A confirmation appends one closed binding per row of the pairing plus the
#: predecessor's newly opened one, and a reversal appends the mirror image.
CONFIRM_ALIAS_COUNT = 3
CONFIRM_AND_REVERSAL_ALIAS_COUNT = 6
#: A pairing carrying a confirmation and the reversal that followed it.
CONFIRM_AND_REVERSAL_DECISION_COUNT = 2
#: Same-name pairs seeded for the pagination assertions, and the window read back.
BULK_SPLIT_PAIRS = 6
BULK_PAGE_SIZE = 3
#: Three rows sharing one name pair up three ways.
THREE_GENERATION_PAIRINGS = 3


def test_schema_and_table_sortable_allowlists_include_parent_ids() -> None:
    """Expose parent FK sort keys so the inventory UI sortable columns stay valid."""
    assert "service_id" in SchemaManager.list_query_spec.sortable
    assert "schema_id" in TableManager.list_query_spec.sortable
    SchemaManager.list_query_spec.resolve_sort("service_id")
    TableManager.list_query_spec.resolve_sort("-schema_id")


class TestRetirementReadPolicy:
    """Test how the retirable managers scope their reads."""

    @pytest.mark.asyncio
    async def test_eager_loaded_collection_drops_retired_children(
        self, session: AsyncSession, node: Node, service: Service
    ) -> None:
        """Filter a retired child out of its active parent's loaded collection."""
        await retire_in_place(session, service)

        nodes = await NodeManager.list(session, select_related=[Node.services])
        assert [loaded.id for loaded in nodes] == [node.id]
        assert nodes[0].services == []

    @pytest.mark.asyncio
    async def test_eager_loaded_parent_is_not_scoped(
        self, session: AsyncSession, node: Node, service: Service
    ) -> None:
        """Keep a many-to-one parent loaded even once it is retired.

        Scoping a required relationship would turn it into ``None`` rather than
        filter it, breaking the response models that declare it non-optional.
        """
        await retire_in_place(session, node)

        services = await ServiceManager.list(session, select_related=[Service.node])
        assert [loaded.id for loaded in services] == [service.id]
        assert services[0].node.id == node.id

    @pytest.mark.asyncio
    async def test_retired_inclusive_manager_injects_nothing(
        self, session: AsyncSession, node: Node, service: Service
    ) -> None:
        """Return retired rows, nested ones included, through the sibling manager."""
        await retire_in_place(session, node)
        await retire_in_place(session, service)

        assert await NodeManager.list(session) == []
        nodes = await RetiredInclusiveNodeManager.list(
            session, select_related=[Node.services]
        )
        assert [loaded.id for loaded in nodes] == [node.id]
        assert [child.id for child in nodes[0].services] == [service.id]


class TestRetirementCascade:
    """Test the retire and revive cascades."""

    @pytest.mark.asyncio
    async def test_cascade_leaves_an_already_retired_descendant_alone(
        self,
        session: AsyncSession,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Keep the original timestamp of a row that was already retired."""
        await retire_in_place(session, schema, datetime(2020, 1, 1, tzinfo=UTC))
        already_retired_at = schema.retired_at

        await NodeManager.retire(session, node)

        await session.refresh(schema)
        await session.refresh(table)
        assert schema.retired_at == already_retired_at
        assert table.retired_at is not None
        assert table.retired_at != already_retired_at

    @pytest.mark.asyncio
    async def test_cascade_keeps_the_system_observations(
        self,
        session: AsyncSession,
        node: Node,
        service: Service,
        host_observation: HostSystemObservation,
        service_observation: ServiceSystemObservation,
    ) -> None:
        """Keep both observation rows, which a hard delete would have cascaded away."""
        await NodeManager.retire(session, node)

        assert (
            await HostSystemObservationManager.get(session, id=host_observation.id)
            is not None
        )
        assert (
            await ServiceSystemObservationManager.get(
                session, id=service_observation.id
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_cascade_persists_nothing_when_a_statement_fails(
        self,
        mocker: MockerFixture,
        session: AsyncSession,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Publish no part of the subtree when one statement of the cascade fails.

        The cascade issues its own statements and commits once, so a failure
        anywhere in it leaves the whole subtree active rather than half retired.
        """
        entity_ids = (node.id, service.id, schema.id, table.id)
        real_exec = session.exec

        async def exec_failing_on_node(statement: object, *args: object) -> object:
            target = getattr(statement, "table", None)
            if target is not None and target.name == Node.__tablename__:
                raise RuntimeError("cascade interrupted")
            return await real_exec(statement, *args)

        mocker.patch.object(session, "exec", side_effect=exec_failing_on_node)

        with pytest.raises(RuntimeError, match="cascade interrupted"):
            await NodeManager.retire(session, node)
        await session.rollback()

        managers = (NodeManager, ServiceManager, SchemaManager, TableManager)
        for manager, entity_id in zip(managers, entity_ids, strict=True):
            assert (await manager.get(session, id=entity_id)).retired_at is None


@pytest.mark.asyncio
async def test_dangling_fk_rejected_by_database(session: AsyncSession) -> None:
    """Reject a dangling parent FK at the database layer.

    ``create`` injects the FK from a path-validated parent and runs no parent
    pre-check, so this exercises the SQLite foreign-key constraint directly
    (``PRAGMA foreign_keys = ON`` is enabled on the inventory test engine). The
    DB ``IntegrityError`` surfaces as ``HTTPBadRequestException`` because
    ``BaseSQLModelManager.save`` translates database errors on commit.
    """
    with pytest.raises(HTTPBadRequestException):
        await ServiceManager.create(session, ServiceWriteFactory.build(), node_id=9999)


RETIRED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 2, 1, tzinfo=UTC)


class TestCollectibleIds:
    """Test how the retirable managers select tombstones for deletion."""

    @pytest.mark.asyncio
    async def test_default_manager_selects_nothing(
        self, session: AsyncSession, table: Table
    ) -> None:
        """Match no row through a default manager, whose reads hide tombstones."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)

        assert (
            await TableManager.collectible_ids(
                session, retired_before=CUTOFF, keep_by_model={}, limit=10
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_retired_inclusive_manager_selects_the_tombstone(
        self, session: AsyncSession, table: Table
    ) -> None:
        """Match the tombstone through the retired-inclusive sibling."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)

        assert await RetiredInclusiveTableManager.collectible_ids(
            session, retired_before=CUTOFF, keep_by_model={}, limit=10
        ) == [table.id]

    @pytest.mark.asyncio
    async def test_active_row_is_never_selected(
        self, session: AsyncSession, table: Table
    ) -> None:
        """Leave a row that was never retired out of the candidate set."""
        assert (
            await RetiredInclusiveTableManager.collectible_ids(
                session, retired_before=CUTOFF, keep_by_model={}, limit=10
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_cutoff_is_strict(self, session: AsyncSession, table: Table) -> None:
        """Keep a row retired exactly at the cutoff, collecting it only later."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)

        assert (
            await RetiredInclusiveTableManager.collectible_ids(
                session, retired_before=RETIRED_AT, keep_by_model={}, limit=10
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_keep_ids_are_excluded(
        self, session: AsyncSession, table: Table
    ) -> None:
        """Skip a tombstone a caller declared still referenced."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)

        assert (
            await RetiredInclusiveTableManager.collectible_ids(
                session,
                retired_before=CUTOFF,
                keep_by_model={Table: {table.id}},
                limit=10,
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_active_descendant_blocks_its_ancestor(
        self, session: AsyncSession, node: Node, table: Table
    ) -> None:
        """Keep a node whose subtree still holds an active row."""
        await retire_in_place(session, node, retired_at=RETIRED_AT)

        assert (
            await RetiredInclusiveNodeManager.collectible_ids(
                session, retired_before=CUTOFF, keep_by_model={}, limit=10
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_kept_descendant_blocks_its_ancestor(
        self,
        session: AsyncSession,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Keep the ancestors of a service a caller declared still referenced."""
        for entity in (table, schema, service, node):
            await retire_in_place(session, entity, retired_at=RETIRED_AT)

        assert (
            await RetiredInclusiveNodeManager.collectible_ids(
                session,
                retired_before=CUTOFF,
                keep_by_model={Service: {service.id}},
                limit=10,
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_young_descendant_blocks_its_ancestor(
        self,
        session: AsyncSession,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Keep a node whose subtree holds a tombstone younger than the cutoff."""
        for entity in (schema, service, node):
            await retire_in_place(session, entity, retired_at=RETIRED_AT)
        await retire_in_place(
            session, table, retired_at=datetime(2026, 3, 1, tzinfo=UTC)
        )

        assert (
            await RetiredInclusiveNodeManager.collectible_ids(
                session, retired_before=CUTOFF, keep_by_model={}, limit=10
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_fully_retired_subtree_is_collectible(
        self,
        session: AsyncSession,
        node: Node,
        service: Service,
        schema: Schema,
        table: Table,
    ) -> None:
        """Collect a node whose whole subtree is a tombstone past the cutoff."""
        for entity in (table, schema, service, node):
            await retire_in_place(session, entity, retired_at=RETIRED_AT)

        assert await RetiredInclusiveNodeManager.collectible_ids(
            session, retired_before=CUTOFF, keep_by_model={}, limit=10
        ) == [node.id]

    @pytest.mark.asyncio
    async def test_limit_caps_the_batch(
        self, session: AsyncSession, table: Table, second_table: Table
    ) -> None:
        """Return no more ids than the caller's batch size."""
        await retire_in_place(session, table, retired_at=RETIRED_AT)
        await retire_in_place(session, second_table, retired_at=RETIRED_AT)

        assert await RetiredInclusiveTableManager.collectible_ids(
            session, retired_before=CUTOFF, keep_by_model={}, limit=1
        ) == [table.id]


class TestCollect:
    """Test how the retirable managers delete the tombstones they are given."""

    @pytest.mark.asyncio
    async def test_deletes_the_given_tombstones(
        self, session: AsyncSession, retired_table: Table
    ) -> None:
        """Delete a tombstone and report the row count."""
        assert (
            await RetiredInclusiveTableManager.collect(session, [retired_table.id]) == 1
        )
        assert await RetiredInclusiveTableManager.count(session) == 0

    @pytest.mark.asyncio
    async def test_refuses_an_active_row(
        self, session: AsyncSession, table: Table
    ) -> None:
        """Leave an active row alone even when a caller names its id."""
        assert await RetiredInclusiveTableManager.collect(session, [table.id]) == 0
        assert await RetiredInclusiveTableManager.count(session) == 1

    @pytest.mark.asyncio
    async def test_empty_id_list_is_a_no_op(
        self, session: AsyncSession, retired_table: Table
    ) -> None:
        """Delete nothing when handed no ids, rather than every row."""
        assert await RetiredInclusiveTableManager.collect(session, []) == 0
        assert await RetiredInclusiveTableManager.count(session) == 1

    @pytest.mark.asyncio
    async def test_re_running_a_collected_batch_deletes_nothing(
        self, session: AsyncSession, retired_table: Table
    ) -> None:
        """Report zero rows on a second pass over an already-collected batch."""
        await RetiredInclusiveTableManager.collect(session, [retired_table.id])

        assert (
            await RetiredInclusiveTableManager.collect(session, [retired_table.id]) == 0
        )

    @pytest.mark.asyncio
    async def test_deleting_a_node_cascades_to_its_observation(
        self,
        session: AsyncSession,
        node: Node,
        host_observation: HostSystemObservation,
    ) -> None:
        """Take an observation row with the node it belongs to."""
        await retire_in_place(session, node)

        assert await RetiredInclusiveNodeManager.collect(session, [node.id]) == 1
        assert await HostSystemObservationManager.count(session) == 0


class TestNodeIdentityCandidates:
    """Test which node pairings the candidate derivation surfaces."""

    @pytest.mark.asyncio
    async def test_a_tombstoned_predecessor_is_paired_with_its_successor(
        self, session: AsyncSession, tombstoned_split_nodes: tuple[Node, Node]
    ) -> None:
        """Pair a tombstoned node with the active node sharing its name."""
        predecessor, successor = tombstoned_split_nodes

        candidates, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 1
        assert candidates[0].predecessor.id == predecessor.id
        assert candidates[0].successor.id == successor.id

    @pytest.mark.asyncio
    async def test_an_active_but_absent_predecessor_is_still_paired(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Pair two active same-name nodes, the older one being the predecessor.

        PMM enforces global name uniqueness on create, so two synced rows sharing
        a name means one is no longer reported upstream — the AC's
        active-but-absent case, which inventory cannot see directly.
        """
        predecessor, successor = split_nodes

        candidates, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 1
        assert candidates[0].predecessor.id == predecessor.id
        assert candidates[0].successor.id == successor.id

    @pytest.mark.asyncio
    async def test_rows_sharing_an_upstream_id_are_not_a_candidate(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Raise nothing for a tombstone whose replacement reuses its upstream id."""
        await retire_in_place(session, node)
        await NodeManager.create(
            session,
            NodeWriteFactory.build(name=node.name, external_id=node.external_id),
        )

        _, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 0

    @pytest.mark.asyncio
    async def test_differing_names_are_not_a_candidate(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Raise nothing for two nodes that never shared a name."""
        await NodeManager.create(
            session, NodeWriteFactory.build(name=f"not-{node.name}")
        )

        _, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 0

    @pytest.mark.asyncio
    async def test_a_mismatched_address_does_not_suppress_the_pairing(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Raise the pairing anyway when only the name agrees.

        PMM discards the address on a non-``--force`` re-registration and ships
        no node update path, so a stored address is routinely stale for a node
        that never moved.
        """
        await NodeManager.create(
            session,
            NodeWriteFactory.build(name=node.name, address="10.9.9.9"),
        )

        candidates, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 1
        assert candidates[0].matched_on == ["name"]

    @pytest.mark.asyncio
    async def test_a_matching_address_is_reported_as_a_signal(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Report the address alongside the name when both agree."""
        candidates, _ = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert candidates[0].matched_on == ["name", "address"]

    @pytest.mark.asyncio
    async def test_no_split_raises_no_candidate(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Return an empty page when nothing was split."""
        candidates, total = await RetiredInclusiveNodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert candidates == []
        assert total == 0


class TestExternalIdentityAliasResolution:
    """Test how an upstream id resolves through the alias records."""

    @pytest.mark.asyncio
    async def test_an_id_with_no_history_resolves_to_nothing(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Return None for an id no link has ever touched, so callers fall back."""
        resolved = await ExternalIdentityAliasManager.resolve_entity_id(
            session,
            RetirableEntityName.NODE,
            SourceEnum.PMM,
            node.external_id,
        )

        assert resolved is None


PRINCIPAL = "operator@example.com"


async def confirmed_split(
    session: AsyncSession, pair: tuple[Node, Node]
) -> tuple[Node, Node]:
    """Confirm a node pairing and hand both rows back as they now stand."""
    predecessor, successor = pair
    await NodeManager.confirm_identity_link(
        session, predecessor, successor.id, principal=PRINCIPAL
    )
    await session.refresh(predecessor)
    await session.refresh(successor)
    return predecessor, successor


class TestConfirmNodeIdentityLink:
    """Test transferring a successor's upstream identity onto its predecessor.

    The lost-race cases here patch ``_lock_pairing`` on the manager under test,
    which the usual guidance forbids. It is deliberate and narrow: the competing
    decision has to land between the lock and the re-check, and on SQLite there
    is no other seam at that point. The real method still runs inside the patch
    and the assertions count committed rows rather than calls, so the test fails
    on behaviour rather than on call shape. The lock itself is covered without a
    patch by ``TestIdentityLinkLockingOnPostgreSQL``.
    """

    @pytest.mark.asyncio
    async def test_the_predecessor_takes_the_successors_identifier(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Leave the surviving row holding the identifier PMM now reports."""
        predecessor, successor = split_nodes
        transferred = successor.external_id

        predecessor, successor = await confirmed_split(session, split_nodes)

        assert predecessor.external_id == transferred
        assert predecessor.retired_at is None
        assert successor.retired_at is not None

    @pytest.mark.asyncio
    async def test_a_tombstoned_predecessor_is_revived(
        self, session: AsyncSession, tombstoned_split_nodes: tuple[Node, Node]
    ) -> None:
        """Bring the predecessor back, it being the row every reference resolves to."""
        predecessor, successor = await confirmed_split(session, tombstoned_split_nodes)

        assert predecessor.retired_at is None
        assert predecessor.retirement_key == ACTIVE_RETIREMENT_KEY
        assert successor.retired_at is not None

    @pytest.mark.asyncio
    async def test_three_alias_records_are_appended(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Close both rows' own bindings and open the predecessor's new one."""
        original_predecessor_id = split_nodes[0].external_id
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)

        aliases = await ExternalIdentityAliasManager.list(session)

        assert len(aliases) == CONFIRM_ALIAS_COUNT
        open_records = [alias for alias in aliases if alias.valid_to is None]
        assert len(open_records) == 1
        assert open_records[0].entity_id == predecessor.id
        assert open_records[0].external_id == transferred
        closed = {(alias.entity_id, alias.external_id) for alias in aliases} - {
            (predecessor.id, transferred)
        }
        assert closed == {
            (predecessor.id, original_predecessor_id),
            (successor.id, transferred),
        }

    @pytest.mark.asyncio
    async def test_both_identifiers_resolve_to_the_survivor(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Keep the superseded identifier resolvable, which is the point of the alias."""
        superseded = split_nodes[0].external_id
        transferred = split_nodes[1].external_id
        predecessor, _ = await confirmed_split(session, split_nodes)

        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.NODE, SourceEnum.PMM, superseded
            )
            == predecessor.id
        )
        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.NODE, SourceEnum.PMM, transferred
            )
            == predecessor.id
        )

    @pytest.mark.asyncio
    async def test_a_confirmed_pairing_leaves_the_candidate_list(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Stop offering a pairing an operator has already acted on."""
        await confirmed_split(session, split_nodes)

        _, total = await NodeManager.identity_candidates(session, pagination=PAGE)

        assert total == 0

    @pytest.mark.asyncio
    async def test_a_second_confirmation_conflicts_and_appends_nothing(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Refuse the second of two confirmations rather than logging one event twice.

        Counting the rows is what catches a double append: the entity mutations
        are individually idempotent, so a second run would leave the rows right
        and the audit trail wrong.
        """
        predecessor, successor = await confirmed_split(session, split_nodes)

        with pytest.raises(HTTPConflictException):
            await NodeManager.confirm_identity_link(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert await IdentityLinkDecisionManager.count(session) == 1
        assert await ExternalIdentityAliasManager.count(session) == CONFIRM_ALIAS_COUNT

    @pytest.mark.asyncio
    async def test_a_pairing_created_between_the_check_and_the_lock_conflicts(
        self,
        session: AsyncSession,
        split_nodes: tuple[Node, Node],
        mocker: MockerFixture,
    ) -> None:
        """Refuse a confirmation another caller won while this one waited on the lock.

        The re-check has to read the decision log *after* the lock is held. Doing
        it before is the whole concurrency bug: both callers pass, both append,
        and nothing fails loudly because the row mutations are idempotent.
        """
        predecessor, successor = split_nodes
        real_lock = NodeManager._lock_pairing

        async def lock_then_lose_the_race(
            locked: AsyncSession, predecessor_id: int, successor_id: int
        ) -> None:
            await real_lock(locked, predecessor_id, successor_id)
            session.add(
                IdentityLinkDecision(
                    entity_type=RetirableEntityName.NODE,
                    predecessor_id=predecessor_id,
                    successor_id=successor_id,
                    decision=IdentityLinkDecisionEnum.CONFIRMED,
                    principal="the-other-operator",
                    predecessor_external_id=predecessor.external_id,
                )
            )
            await session.commit()

        mocker.patch.object(
            NodeManager, "_lock_pairing", side_effect=lock_then_lose_the_race
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.confirm_identity_link(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert await IdentityLinkDecisionManager.count(session) == 1
        assert await ExternalIdentityAliasManager.count(session) == 0

    @pytest.mark.asyncio
    async def test_pairing_a_row_with_itself_is_refused(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse a body naming the row the path already addresses."""
        with pytest.raises(HTTPBadRequestException):
            await NodeManager.confirm_identity_link(
                session, node, node.id, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_an_unknown_successor_is_not_found(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse a body naming a row that does not exist."""
        assert node.id is not None
        with pytest.raises(HTTPNotFoundException):
            await NodeManager.confirm_identity_link(
                session, node, node.id + 1000, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_rows_already_sharing_an_identifier_are_refused(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse a pairing with nothing to transfer."""
        await retire_in_place(session, node)
        twin = await NodeManager.create(
            session,
            NodeWriteFactory.build(name=node.name, external_id=node.external_id),
        )

        with pytest.raises(HTTPBadRequestException):
            await NodeManager.confirm_identity_link(
                session, node, twin.id, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_an_unrelated_pairing_is_refused(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse two rows the derivation would never have surfaced together."""
        unrelated = await NodeManager.create(
            session, NodeWriteFactory.build(name=f"not-{node.name}")
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.confirm_identity_link(
                session, node, unrelated.id, principal=PRINCIPAL
            )


class TestRejectNodeIdentityLink:
    """Test recording that a candidate pairing names two machines."""

    @pytest.mark.asyncio
    async def test_a_rejected_pairing_leaves_the_candidate_list(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Stop re-surfacing a pairing on the next sync once it is rejected."""
        predecessor, successor = split_nodes

        await NodeManager.reject_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        _, total = await NodeManager.identity_candidates(session, pagination=PAGE)
        assert total == 0

    @pytest.mark.asyncio
    async def test_a_rejection_does_not_block_confirming_the_same_pairing(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Let an operator correct a mistaken rejection by confirming explicitly.

        Rejection suppresses the suggestion, not the operation — otherwise a
        misclick is permanent and the failure is invisible, the pairing simply
        never appearing again.
        """
        predecessor, successor = split_nodes
        transferred = successor.external_id
        await NodeManager.reject_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await NodeManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await session.refresh(predecessor)
        assert predecessor.external_id == transferred

    @pytest.mark.asyncio
    async def test_an_unrelated_pairing_is_refused(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse to reject two rows that were never a pairing."""
        unrelated = await NodeManager.create(
            session, NodeWriteFactory.build(name=f"not-{node.name}")
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.reject_identity_link(
                session, node, unrelated.id, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_a_second_rejection_conflicts_and_appends_nothing(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Refuse the second of two rejections rather than logging one event twice.

        Counting is what catches it: a rejection mutates no row, so a duplicate
        leaves the entities indistinguishable from the single-rejection case and
        shows up only in the decision log.
        """
        predecessor, successor = split_nodes
        await NodeManager.reject_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.reject_identity_link(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert await IdentityLinkDecisionManager.count(session) == 1

    @pytest.mark.asyncio
    async def test_a_rejection_created_between_the_check_and_the_lock_conflicts(
        self,
        session: AsyncSession,
        split_nodes: tuple[Node, Node],
        mocker: MockerFixture,
    ) -> None:
        """Refuse a rejection another caller won while this one waited on the lock."""
        predecessor, successor = split_nodes
        real_lock = NodeManager._lock_pairing

        async def lock_then_lose_the_race(
            locked: AsyncSession, predecessor_id: int, successor_id: int
        ) -> None:
            await real_lock(locked, predecessor_id, successor_id)
            session.add(
                IdentityLinkDecision(
                    entity_type=RetirableEntityName.NODE,
                    predecessor_id=predecessor_id,
                    successor_id=successor_id,
                    decision=IdentityLinkDecisionEnum.REJECTED,
                    principal="the-other-operator",
                )
            )
            await session.commit()

        mocker.patch.object(
            NodeManager, "_lock_pairing", side_effect=lock_then_lose_the_race
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.reject_identity_link(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert await IdentityLinkDecisionManager.count(session) == 1


class TestUnlinkNodeIdentity:
    """Test reversing a standing confirmation."""

    @pytest.mark.asyncio
    async def test_a_superseded_link_is_not_reversible_on_its_own(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Refuse to reverse a link a later confirmation has already built on.

        The older decision records the identifier its predecessor held at the
        time, which the later confirmation has since replaced. Restoring from it
        would put back a superseded identifier and hand the older successor one
        that is not the one it lost, leaving both rows and the alias history
        describing different identities.
        """
        second = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        third = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        await NodeManager.confirm_identity_link(
            session, node, second.id, principal=PRINCIPAL
        )
        await NodeManager.confirm_identity_link(
            session, node, third.id, principal=PRINCIPAL
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, node, second.id, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_stacked_links_reverse_in_the_opposite_order(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Return all three rows to their own identifiers, newest link first."""
        second = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        third = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        own = {
            node.id: node.external_id,
            second.id: second.external_id,
            third.id: third.external_id,
        }
        await NodeManager.confirm_identity_link(
            session, node, second.id, principal=PRINCIPAL
        )
        await NodeManager.confirm_identity_link(
            session, node, third.id, principal=PRINCIPAL
        )

        await NodeManager.unlink_identity(session, node, third.id, principal=PRINCIPAL)
        await session.refresh(node)
        await NodeManager.unlink_identity(session, node, second.id, principal=PRINCIPAL)

        for entity_id, identifier in own.items():
            assert (
                await ExternalIdentityAliasManager.resolve_entity_id(
                    session, RetirableEntityName.NODE, SourceEnum.PMM, identifier
                )
                == entity_id
            )

    @pytest.mark.asyncio
    async def test_a_stacked_confirmation_closes_the_interval_it_actually_opened(
        self, session: AsyncSession, node: Node, mocker: MockerFixture
    ) -> None:
        """Close a superseded binding at the confirmation that opened it.

        The predecessor's second confirmation supersedes an identifier it took
        at the first one, not one it was created with, so closing that binding
        at the row's creation time would report an interval overlapping the
        identifier it held before that. The stamps are pinned because the column
        keeps whole seconds, and a test writing all three inside one second
        cannot tell the two candidate answers apart.
        """
        second = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        third = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        transferred_first = second.external_id
        pinned = datetime(2026, 1, 1, tzinfo=UTC)
        mocker.patch(
            "app.inventory.crud.utc_now",
            side_effect=[pinned, datetime(2026, 2, 2, tzinfo=UTC)],
        )
        await NodeManager.confirm_identity_link(
            session, node, second.id, principal=PRINCIPAL
        )
        await NodeManager.confirm_identity_link(
            session, node, third.id, principal=PRINCIPAL
        )

        aliases = await ExternalIdentityAliasManager.list(session)

        records = [
            alias
            for alias in aliases
            if alias.entity_id == node.id and alias.external_id == transferred_first
        ]
        opened = [alias for alias in records if alias.valid_to is None]
        closed = [alias for alias in records if alias.valid_to is not None]
        assert len(opened) == 1
        assert len(closed) == 1
        assert closed[0].valid_from == opened[0].valid_from
        assert closed[0].valid_from != node.created_at

    @pytest.mark.asyncio
    async def test_an_active_predecessor_is_restored_to_its_own_identifier(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Give each row back the identifier it held before the confirmation."""
        own_identifier = split_nodes[0].external_id
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)

        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await session.refresh(predecessor)
        await session.refresh(successor)
        assert predecessor.external_id == own_identifier
        assert predecessor.retired_at is None
        assert successor.external_id == transferred
        assert successor.retired_at is None

    @pytest.mark.asyncio
    async def test_a_tombstoned_predecessor_is_re_retired_at_its_original_time(
        self, session: AsyncSession, tombstoned_split_nodes: tuple[Node, Node]
    ) -> None:
        """Restore the tombstone the confirmation revived, retention age included."""
        retired_at = tombstoned_split_nodes[0].retired_at
        predecessor, successor = await confirmed_split(session, tombstoned_split_nodes)

        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await session.refresh(predecessor)
        await session.refresh(successor)
        assert predecessor.retired_at == retired_at
        assert successor.retired_at is None

    @pytest.mark.asyncio
    async def test_the_confirmation_records_survive_the_reversal(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Append the reversal rather than editing what the confirmation wrote."""
        predecessor, successor = await confirmed_split(session, split_nodes)
        confirmation_ids = {
            alias.id for alias in await ExternalIdentityAliasManager.list(session)
        }

        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        aliases = await ExternalIdentityAliasManager.list(session)
        assert confirmation_ids <= {alias.id for alias in aliases}
        assert len(aliases) == CONFIRM_AND_REVERSAL_ALIAS_COUNT
        assert (
            await IdentityLinkDecisionManager.count(session)
            == CONFIRM_AND_REVERSAL_DECISION_COUNT
        )

    @pytest.mark.asyncio
    async def test_each_identifier_resolves_to_its_own_row_again(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Undo the resolution change, which is what makes the link reversible."""
        own_identifier = split_nodes[0].external_id
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)

        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.NODE, SourceEnum.PMM, own_identifier
            )
            == predecessor.id
        )
        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.NODE, SourceEnum.PMM, transferred
            )
            == successor.id
        )

    @pytest.mark.asyncio
    async def test_a_second_confirmation_after_a_reversal_lands_the_same_state(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Leave the rows where the first confirmation left them, cycle after cycle."""
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)
        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        predecessor, successor = await confirmed_split(
            session, (predecessor, successor)
        )

        assert predecessor.external_id == transferred
        assert predecessor.retired_at is None
        assert successor.retired_at is not None

    @pytest.mark.asyncio
    async def test_a_pairing_that_was_never_confirmed_is_refused(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Refuse to reverse a link that never stood."""
        predecessor, successor = split_nodes

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

    @pytest.mark.asyncio
    async def test_a_second_reversal_is_refused(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Refuse the second of two reversals, one link being reversible once."""
        predecessor, successor = await confirmed_split(session, split_nodes)
        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert (
            await IdentityLinkDecisionManager.count(session)
            == CONFIRM_AND_REVERSAL_DECISION_COUNT
        )

    @pytest.mark.asyncio
    async def test_a_collected_successor_leaves_the_link_irreversible(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Say so plainly when the row a reversal needs is gone, writing nothing."""
        predecessor, successor = await confirmed_split(session, split_nodes)
        successor_id = successor.id
        await session.delete(successor)
        await session.commit()

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, predecessor, successor_id, principal=PRINCIPAL
            )

        assert await ExternalIdentityAliasManager.count(session) == CONFIRM_ALIAS_COUNT

    @pytest.mark.asyncio
    async def test_a_reclaimed_predecessor_identifier_rolls_the_reversal_back(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Refuse a reversal an active row's unique key blocks, leaving both rows alone.

        The predecessor's own identifier is released by the confirmation, so a
        later sync tick can legitimately hand it to a new node.
        """
        own_identifier = split_nodes[0].external_id
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)
        await NodeManager.create(
            session, NodeWriteFactory.build(external_id=own_identifier)
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        await session.refresh(predecessor)
        await session.refresh(successor)
        assert predecessor.external_id == transferred
        assert successor.retired_at is not None

    @pytest.mark.asyncio
    async def test_a_reversal_created_between_the_check_and_the_lock_conflicts(
        self,
        session: AsyncSession,
        split_nodes: tuple[Node, Node],
        mocker: MockerFixture,
    ) -> None:
        """Refuse a reversal another caller won while this one waited on the lock."""
        predecessor, successor = await confirmed_split(session, split_nodes)
        real_lock = NodeManager._lock_pairing

        async def lock_then_lose_the_race(
            locked: AsyncSession, predecessor_id: int, successor_id: int
        ) -> None:
            await real_lock(locked, predecessor_id, successor_id)
            session.add(
                IdentityLinkDecision(
                    entity_type=RetirableEntityName.NODE,
                    predecessor_id=predecessor_id,
                    successor_id=successor_id,
                    decision=IdentityLinkDecisionEnum.UNLINKED,
                    principal="the-other-operator",
                )
            )
            await session.commit()

        mocker.patch.object(
            NodeManager, "_lock_pairing", side_effect=lock_then_lose_the_race
        )

        with pytest.raises(HTTPConflictException):
            await NodeManager.unlink_identity(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        assert (
            await IdentityLinkDecisionManager.count(session)
            == CONFIRM_AND_REVERSAL_DECISION_COUNT
        )


class TestChainedIdentityLinks:
    """Test a row that is a predecessor in one link and a successor in the next."""

    @staticmethod
    async def _chain(
        session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> tuple[Node, Node, Node]:
        """Confirm a twice-re-registered node newest pair first, as the list offers.

        While the middle row is the newest active one it is the only successor a
        pairing can name, so the operator confirms it first and only then is the
        oldest row's pairing surfaced. That order is what leaves the middle row a
        predecessor in one link and a successor in the next.
        """
        oldest, middle = split_nodes
        newest = await NodeManager.create(
            session,
            NodeWriteFactory.build(name=oldest.name, address=oldest.address),
        )
        await NodeManager.confirm_identity_link(
            session, middle, newest.id, principal=PRINCIPAL
        )
        await NodeManager.confirm_identity_link(
            session, oldest, middle.id, principal=PRINCIPAL
        )
        return oldest, middle, newest

    @pytest.mark.asyncio
    async def test_every_identifier_in_the_chain_resolves_to_the_survivor(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Answer for the identifiers the chain absorbed, not just the transferred one.

        A confirmation moves only the identifier its successor currently holds,
        so the middle row's *own* identifier is the one no link rewrites. Reading
        the newest binding alone would hand it back a tombstone.
        """
        identifiers = [split_nodes[0].external_id, split_nodes[1].external_id]
        oldest, _, newest = await self._chain(session, split_nodes)
        identifiers.append(newest.external_id)

        for external_id in identifiers:
            assert (
                await ExternalIdentityAliasManager.resolve_entity_id(
                    session, RetirableEntityName.NODE, SourceEnum.PMM, external_id
                )
                == oldest.id
            )

    @pytest.mark.asyncio
    async def test_reversing_the_newer_link_hands_the_chain_back(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Stop following a confirmation the operator retracted.

        Resolution follows the decision log rather than a rewritten binding, so a
        reversal restores the earlier answers by retracting the link — nothing
        has to be un-rewritten.
        """
        oldest_id = split_nodes[0].external_id
        middle_id = split_nodes[1].external_id
        oldest, middle, newest = await self._chain(session, split_nodes)
        newest_id = newest.external_id

        await NodeManager.unlink_identity(
            session, oldest, middle.id, principal=PRINCIPAL
        )

        resolutions = {
            external_id: await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.NODE, SourceEnum.PMM, external_id
            )
            for external_id in (oldest_id, middle_id, newest_id)
        }

        assert resolutions == {
            oldest_id: oldest.id,
            middle_id: middle.id,
            newest_id: middle.id,
        }


class TestIdentityLinkAndCollection:
    """Test how a standing link interacts with tombstone collection."""

    @pytest.mark.asyncio
    async def test_a_linked_successor_is_not_collectible(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Keep a confirmed link reversible for as long as it stands.

        Without this the successor tombstone — which by construction nothing
        references after a confirmation — ages past the retention window and is
        deleted, making the reversal permanently impossible with no signal.
        """
        _, successor = await confirmed_split(session, split_nodes)

        collectible = await RetiredInclusiveNodeManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert successor.id not in collectible

    @pytest.mark.asyncio
    async def test_an_unlinked_successor_becomes_collectible_again(
        self, session: AsyncSession, tombstoned_split_nodes: tuple[Node, Node]
    ) -> None:
        """Release the pin once the link no longer stands."""
        predecessor, successor = await confirmed_split(session, tombstoned_split_nodes)
        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )
        await retire_in_place(session, successor)

        collectible = await RetiredInclusiveNodeManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert successor.id in collectible

    @pytest.mark.asyncio
    async def test_a_service_under_a_linked_successor_node_is_not_collectible(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Keep the services a node confirmation retired within the link's reach.

        Confirming a node retires the successor *with its subtree*, and those
        services carry no service decision of their own, so the shared pin does
        not reach them. Collection walks services before nodes, which means the
        very rows the node link exists to surface as candidates in turn would age
        out from under it while the link still stands.
        """
        predecessor, successor, _, successor_service = split_nodes_with_services
        await NodeManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        collectible = await RetiredInclusiveServiceManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert successor_service.id not in collectible

    @pytest.mark.asyncio
    async def test_a_service_under_an_unlinked_node_becomes_collectible_again(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Release the widened pin with the node link that justified it."""
        predecessor, successor, _, successor_service = split_nodes_with_services
        await NodeManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )
        await NodeManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        collectible = await RetiredInclusiveServiceManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert successor_service.id in collectible

    @pytest.mark.asyncio
    async def test_an_ordinary_tombstone_stays_collectible(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Leave collection's reach over unlinked tombstones exactly as it was."""
        await retire_in_place(session, node)

        collectible = await RetiredInclusiveNodeManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert collectible == [node.id]


class TestServiceIdentityCandidates:
    """Test which service pairings the candidate derivation surfaces."""

    @pytest.mark.asyncio
    async def test_a_same_node_split_is_paired(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Pair two same-name services sitting on one node."""
        predecessor, successor = split_services

        candidates, total = await ServiceManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 1
        assert candidates[0].predecessor.id == predecessor.id
        assert candidates[0].successor.id == successor.id
        assert candidates[0].matched_on == ["name", "port"]

    @pytest.mark.asyncio
    async def test_services_on_unrelated_nodes_are_not_paired(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Leave two same-name services alone while their nodes are unrelated.

        Sharing a name across two nodes nobody has linked is ordinary — one PMM
        estate routinely runs a service of the same name on many machines.
        """
        _, total = await ServiceManager.identity_candidates(session, pagination=PAGE)

        assert total == 0

    @pytest.mark.asyncio
    async def test_confirming_a_node_surfaces_its_services_in_turn(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Raise the service pairing a confirmed node pairing implies.

        The node confirmation retires the successor node with its subtree, so the
        successor service is a tombstone by the time this runs and has to count
        as a successor anyway.
        """
        predecessor_node, successor_node, predecessor, successor = (
            split_nodes_with_services
        )
        await NodeManager.confirm_identity_link(
            session, predecessor_node, successor_node.id, principal=PRINCIPAL
        )

        candidates, total = await ServiceManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == 1
        assert candidates[0].predecessor.id == predecessor.id
        assert candidates[0].successor.id == successor.id

    @pytest.mark.asyncio
    async def test_confirming_a_node_links_no_service(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Leave every service holding its own identifier, each link being its own act."""
        predecessor_node, successor_node, predecessor, successor = (
            split_nodes_with_services
        )
        identifiers = (predecessor.external_id, successor.external_id)

        await NodeManager.confirm_identity_link(
            session, predecessor_node, successor_node.id, principal=PRINCIPAL
        )

        await session.refresh(predecessor)
        await session.refresh(successor)
        assert (predecessor.external_id, successor.external_id) == identifiers
        assert await IdentityLinkDecisionManager.count(session) == 1

    @pytest.mark.asyncio
    async def test_a_pairing_the_uniqueness_index_would_reject_is_withheld(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Withhold a pairing whose confirmation the uniqueness index would refuse.

        Once a sync tick runs after a node confirmation, the syncer recreates the
        successor's service on the surviving node, and the cross-node candidate
        would then collide with it.
        """
        predecessor_node, successor_node, _, successor = split_nodes_with_services
        await NodeManager.confirm_identity_link(
            session, predecessor_node, successor_node.id, principal=PRINCIPAL
        )
        await ServiceManager.create(
            session,
            ServiceWriteFactory.build(
                name="recreated-by-sync", external_id=successor.external_id
            ),
            node_id=predecessor_node.id,
        )

        _, total = await ServiceManager.identity_candidates(session, pagination=PAGE)

        assert total == 0


class TestConfirmServiceIdentityLink:
    """Test transferring a successor service's upstream identity."""

    @pytest.mark.asyncio
    async def test_the_predecessor_takes_the_successors_identifier(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Leave the surviving service holding the identifier PMM now reports."""
        predecessor, successor = split_services
        transferred = successor.external_id

        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await session.refresh(predecessor)
        await session.refresh(successor)
        assert predecessor.external_id == transferred
        assert predecessor.retired_at is None
        assert successor.retired_at is not None

    @pytest.mark.asyncio
    async def test_the_alias_records_carry_the_source_of_the_parent_node(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Store provenance on a service alias, ``Service`` carrying no source column."""
        predecessor, successor = split_services

        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        aliases = await ExternalIdentityAliasManager.list(session)
        assert {alias.source for alias in aliases} == {SourceEnum.PMM}
        assert {alias.entity_type for alias in aliases} == {RetirableEntityName.SERVICE}

    @pytest.mark.asyncio
    async def test_both_identifiers_resolve_to_the_surviving_service(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Keep a superseded service identifier resolvable after the transfer."""
        predecessor, successor = split_services
        superseded = predecessor.external_id
        transferred = successor.external_id

        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.SERVICE, SourceEnum.PMM, superseded
            )
            == predecessor.id
        )
        assert (
            await ExternalIdentityAliasManager.resolve_entity_id(
                session, RetirableEntityName.SERVICE, SourceEnum.PMM, transferred
            )
            == predecessor.id
        )

    @pytest.mark.asyncio
    async def test_a_node_pairing_is_not_reachable_through_the_service_manager(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Record a service decision under the service entity type, not the node's."""
        predecessor, successor = split_services

        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        assert (
            await IdentityLinkDecisionManager.latest_for_pairing(
                session, RetirableEntityName.NODE, predecessor.id, successor.id
            )
            is None
        )
        assert (
            await IdentityLinkDecisionManager.latest_for_pairing(
                session, RetirableEntityName.SERVICE, predecessor.id, successor.id
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_a_retired_service_is_collectible_once_its_link_is_reversed(
        self, session: AsyncSession, split_services: tuple[Service, Service]
    ) -> None:
        """Pin a linked service tombstone exactly as a linked node tombstone is pinned."""
        predecessor, successor = split_services
        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        collectible = await RetiredInclusiveServiceManager.collectible_ids(
            session,
            retired_before=datetime(2999, 1, 1, tzinfo=UTC),
            keep_by_model={},
            limit=10,
        )

        assert successor.id not in collectible

    @pytest.mark.asyncio
    async def test_a_service_waits_for_its_nodes_link_to_reverse_first(
        self,
        session: AsyncSession,
        split_nodes_with_services: tuple[Node, Node, Service, Service],
    ) -> None:
        """Reverse the service link only once its node's link is reversed.

        Reviving a service revives its node first, so a live row never sits under
        a tombstone. While the node link stands, the surviving node holds the
        identifier that revival would reclaim and only one active row may carry
        it — so the service reversal has to wait rather than half-apply.
        """
        pred_node, succ_node, predecessor, successor = split_nodes_with_services
        await NodeManager.confirm_identity_link(
            session, pred_node, succ_node.id, principal=PRINCIPAL
        )
        await session.refresh(predecessor)
        await ServiceManager.confirm_identity_link(
            session, predecessor, successor.id, principal=PRINCIPAL
        )
        await session.refresh(predecessor)
        own_identifier = successor.external_id

        with pytest.raises(HTTPConflictException):
            await ServiceManager.unlink_identity(
                session, predecessor, successor.id, principal=PRINCIPAL
            )

        await session.refresh(pred_node)
        await NodeManager.unlink_identity(
            session, pred_node, succ_node.id, principal=PRINCIPAL
        )
        await session.refresh(predecessor)
        await ServiceManager.unlink_identity(
            session, predecessor, successor.id, principal=PRINCIPAL
        )

        await session.refresh(successor)
        assert successor.external_id == own_identifier
        assert successor.retired_at is None


class TestIdentityCandidatePagination:
    """Test that the candidate page and its total answer the same predicate."""

    @pytest.mark.asyncio
    async def test_the_total_counts_every_pairing_the_page_windows(
        self, session: AsyncSession
    ) -> None:
        """Report the whole matching set while returning only the requested window."""
        for index in range(BULK_SPLIT_PAIRS):
            name = f"bulk-reregistered-{index}"
            await NodeManager.create(session, NodeWriteFactory.build(name=name))
            await NodeManager.create(session, NodeWriteFactory.build(name=name))

        candidates, total = await NodeManager.identity_candidates(
            session, pagination=Pagination(offset=2, limit=BULK_PAGE_SIZE)
        )

        assert total == BULK_SPLIT_PAIRS
        assert len(candidates) == BULK_PAGE_SIZE

    @pytest.mark.asyncio
    async def test_three_generations_pair_against_the_surviving_row(
        self, session: AsyncSession, node: Node
    ) -> None:
        """Raise every pairing among three same-name rows, each confirmable alone."""
        second = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )
        third = await NodeManager.create(
            session, NodeWriteFactory.build(name=node.name)
        )

        candidates, total = await NodeManager.identity_candidates(
            session, pagination=PAGE
        )

        assert total == THREE_GENERATION_PAIRINGS
        assert {(pair.predecessor.id, pair.successor.id) for pair in candidates} == {
            (node.id, second.id),
            (node.id, third.id),
            (second.id, third.id),
        }


class TestConfirmedLinkNeedsNoSyncerChange:
    """Test the inventory-side property the next sync tick relies on.

    ``claim_identity`` (``app/extensions/sync/models.py``) resolves an upstream id to
    the **active** row when a tombstone shares it, and the syncer matches by
    upstream id alone. So a confirmation needs no syncer change provided it
    leaves exactly one active row holding the transferred id — the predecessor.
    That precondition is what this pins, tested in the service that owns it
    rather than through the syncer's mocks.
    """

    @pytest.mark.asyncio
    async def test_one_active_row_holds_the_transferred_identifier(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Leave the predecessor as the only active holder of the identifier."""
        transferred = split_nodes[1].external_id
        predecessor, successor = await confirmed_split(session, split_nodes)

        holders = await RetiredInclusiveNodeManager.list(
            session, external_id=transferred
        )

        assert {holder.id for holder in holders} == {predecessor.id, successor.id}
        assert [holder.id for holder in holders if holder.retired_at is None] == [
            predecessor.id
        ]

    @pytest.mark.asyncio
    async def test_the_default_manager_serves_the_survivor_for_that_identifier(
        self, session: AsyncSession, split_nodes: tuple[Node, Node]
    ) -> None:
        """Answer the syncer's own tombstone-excluding read with the survivor."""
        transferred = split_nodes[1].external_id
        predecessor, _ = await confirmed_split(session, split_nodes)

        matched = await NodeManager.first(session, external_id=transferred)

        assert matched is not None
        assert matched.id == predecessor.id


class TestIdentityLinkLockingOnPostgreSQL:
    """Test the pairing row lock against a server that honours row locks.

    The SQLite race tests above inject the competing decision through the same
    session, which exercises the application-level re-check and nothing else —
    they pass with ``with_for_update()`` removed. Two genuinely concurrent
    sessions on real PostgreSQL is the only place the lock half of the design is
    observable, so the skip contract of ``postgres_engine`` applies: the case is
    silent locally and runs in the ``test_postgres`` CI job.
    """

    @staticmethod
    async def _confirm(
        maker: async_sessionmaker[AsyncSession],
        predecessor_id: int,
        successor_id: int,
        principal: str,
    ) -> None:
        """Confirm one pairing in a session of its own, as a second caller would."""
        async with maker() as session:
            predecessor = await RetiredInclusiveNodeManager.get_or_404(
                session, id=predecessor_id
            )
            await NodeManager.confirm_identity_link(
                session, predecessor, successor_id, principal=principal
            )

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_two_sessions_confirming_one_pairing_append_one_decision(
        self, postgres_engine: AsyncEngine, postgres_session: AsyncSession
    ) -> None:
        """Serialize two overlapping confirmations onto a single decision row.

        Counting the appends is what catches a lost race: the row mutations are
        individually idempotent, so a second confirmation that slipped through
        would leave the nodes looking right and the audit trail claiming the
        machine was reconciled twice.
        """
        predecessor = await NodeManager.create(
            postgres_session, NodeWriteFactory.build()
        )
        successor = await NodeManager.create(
            postgres_session,
            NodeWriteFactory.build(name=predecessor.name, address=predecessor.address),
        )
        maker = get_async_session_maker_from_engine(postgres_engine)

        outcomes = await asyncio.gather(
            self._confirm(maker, predecessor.id, successor.id, "first-operator"),
            self._confirm(maker, predecessor.id, successor.id, "second-operator"),
            return_exceptions=True,
        )

        assert [type(outcome) for outcome in outcomes].count(HTTPConflictException) == 1
        async with maker() as reader:
            assert await IdentityLinkDecisionManager.count(reader) == 1
            assert (
                await ExternalIdentityAliasManager.count(reader) == CONFIRM_ALIAS_COUNT
            )


#: Every manager carrying the sync-health writes, paired with the fixture that
#: builds an entity at that manager's level.
_SYNC_HEALTH_TARGETS = (
    (NodeManager, "node"),
    (ServiceManager, "service"),
    (SchemaManager, "schema"),
    (TableManager, "table"),
)

#: Columns the health writes own, so a test asserting the rest stayed put can
#: subtract them from a dump.
_SYNC_HEALTH_COLUMNS = frozenset(
    {
        "last_synced_at",
        "last_sync_error",
        "sync_failing_since",
        "consecutive_failures",
        "newest_attempt_at",
        "updated_at",
    }
)

#: The counter after a second failure lands on an already-failing row.
SECOND_CONSECUTIVE_FAILURE = 2

#: The union rather than a shared base: an entity carrying both an ``id`` and
#: the sync-health columns has no single base to name — ``SyncHealthBase`` is
#: mixed into the read responses too, so it cannot inherit the table identity.
SyncHealthTarget = tuple[type[SyncHealthManagerMixin], Node | Service | Schema | Table]


@pytest.fixture(
    params=_SYNC_HEALTH_TARGETS,
    ids=[fixture_name for _, fixture_name in _SYNC_HEALTH_TARGETS],
)
def sync_health_target(request: pytest.FixtureRequest) -> SyncHealthTarget:
    """Return one sync-health manager and a persisted entity of its level."""
    manager, fixture_name = request.param
    return manager, request.getfixturevalue(fixture_name)


async def _stamp_sync_health(
    session: AsyncSession, instance: RetirableSQLModel, **values: object
) -> None:
    """Seed sync-health columns directly, bypassing the manager under test."""
    for name, value in values.items():
        setattr(instance, name, value)
    session.add(instance)
    await session.commit()
    await session.refresh(instance)


def _as_utc(value: object) -> object:
    """Return a naive datetime as UTC-aware, leaving every other value untouched.

    A ``DateTime(timezone=True)`` column keeps its offset on PostgreSQL and
    loses it on the suite's SQLite engine, so a value read back here is naive
    while the same read is aware in production.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _success(attempted_at: datetime) -> SyncHealthWrite:
    """Build the body reporting a successful attempt started at ``attempted_at``."""
    return SyncHealthWrite(outcome=SyncOutcomeEnum.SUCCESS, attempted_at=attempted_at)


def _failure(attempted_at: datetime, error: str = "boom") -> SyncHealthWrite:
    """Build the body reporting a failed attempt started at ``attempted_at``."""
    return SyncHealthWrite(
        outcome=SyncOutcomeEnum.FAILURE, error=error, attempted_at=attempted_at
    )


class TestRecordSyncHealth:
    """Test how a reported sync outcome lands on an entity's health columns."""

    @pytest.mark.asyncio
    async def test_first_success_records_a_clean_state(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Leave every failure column empty and stamp the attempt as the freshness."""
        manager, entity = sync_health_target
        attempted_at = utc_now()

        await manager.record_sync_health(session, entity, _success(attempted_at))

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == attempted_at
        assert entity.last_sync_error is None
        assert entity.sync_failing_since is None
        assert entity.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_success_stores_the_attempt_time_not_the_write_time(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Answer "when was this confirmed", not "when did the report arrive"."""
        manager, entity = sync_health_target
        attempted_at = utc_now() - timedelta(hours=3)

        await manager.record_sync_health(session, entity, _success(attempted_at))

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == attempted_at

    @pytest.mark.asyncio
    async def test_success_clears_a_standing_failure_run(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Reset the error, the run start and the counter in one statement."""
        manager, entity = sync_health_target
        failing_since = utc_now() - timedelta(days=3)
        await _stamp_sync_health(
            session,
            entity,
            last_sync_error="previous",
            sync_failing_since=failing_since,
            consecutive_failures=3,
        )
        attempted_at = utc_now()

        await manager.record_sync_health(session, entity, _success(attempted_at))

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == attempted_at
        assert entity.last_sync_error is None
        assert entity.sync_failing_since is None
        assert entity.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_first_failure_opens_the_run(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Open the failure run at the attempt time and leave the freshness alone."""
        manager, entity = sync_health_target
        failed_at = utc_now()

        await manager.record_sync_health(session, entity, _failure(failed_at))

        await session.refresh(entity)
        assert entity.last_synced_at is None
        assert entity.last_sync_error == "boom"
        assert _as_utc(entity.sync_failing_since) == failed_at
        assert entity.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_subsequent_failure_keeps_the_first_failure_time(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Keep ``sync_failing_since`` naming the first failure after the last success."""
        manager, entity = sync_health_target
        opened_at = utc_now() - timedelta(days=2)
        await _stamp_sync_health(
            session,
            entity,
            last_sync_error="first",
            sync_failing_since=opened_at,
            consecutive_failures=1,
        )

        await manager.record_sync_health(
            session, entity, _failure(utc_now(), error="second")
        )

        await session.refresh(entity)
        assert _as_utc(entity.sync_failing_since) == opened_at
        assert entity.last_sync_error == "second"
        assert entity.consecutive_failures == SECOND_CONSECUTIVE_FAILURE

    @pytest.mark.asyncio
    async def test_out_of_order_failures_still_open_the_run_at_the_earlier_one(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Keep the run start at the earlier attempt and the message at the newer.

        Reports cross the service boundary over HTTP, so two failures of one run
        can arrive newest-first. The late older report still counts toward the
        run and moves its start back, but must not replace the newer message.
        """
        manager, entity = sync_health_target
        earlier = utc_now() - timedelta(minutes=10)
        later = utc_now()

        await manager.record_sync_health(session, entity, _failure(later, error="new"))
        await manager.record_sync_health(
            session, entity, _failure(earlier, error="old")
        )

        await session.refresh(entity)
        assert _as_utc(entity.sync_failing_since) == earlier
        assert entity.consecutive_failures == SECOND_CONSECUTIVE_FAILURE
        assert entity.last_sync_error == "new"
        assert _as_utc(entity.newest_attempt_at) == later

    @pytest.mark.parametrize(
        "outcome", [_success, _failure], ids=["success", "failure"]
    )
    @pytest.mark.asyncio
    async def test_the_mirrored_columns_are_left_untouched(
        self,
        session: AsyncSession,
        sync_health_target: SyncHealthTarget,
        outcome: Callable[[datetime], SyncHealthWrite],
    ) -> None:
        """Write only the health columns, never the entity's own mirrored fields."""
        manager, entity = sync_health_target
        before = {
            name: _as_utc(value)
            for name, value in entity.model_dump().items()
            if name not in _SYNC_HEALTH_COLUMNS
        }

        await manager.record_sync_health(session, entity, outcome(utc_now()))

        await session.refresh(entity)
        after = {name: _as_utc(getattr(entity, name)) for name in before}
        assert after == before

    @pytest.mark.asyncio
    async def test_error_is_truncated_to_the_cap(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Accept an error of any length and store it within the column contract."""
        manager, entity = sync_health_target
        oversized = "e" * (SYNC_ERROR_MAX_LENGTH + 500)

        await manager.record_sync_health(
            session, entity, _failure(utc_now(), error=oversized)
        )

        await session.refresh(entity)
        assert entity.last_sync_error is not None
        assert len(entity.last_sync_error) <= SYNC_ERROR_MAX_LENGTH

    @pytest.mark.asyncio
    async def test_write_reaches_a_retired_row(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Record the outcome even once the entity was retired concurrently."""
        manager, entity = sync_health_target
        await retire_in_place(session, entity)

        await manager.record_sync_health(session, entity, _failure(utc_now()))

        await session.refresh(entity)
        assert entity.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_counter_increments_in_sql(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Add to the stored counter rather than to a value read beforehand."""
        manager, entity = sync_health_target

        await manager.record_sync_health(session, entity, _failure(utc_now()))
        await manager.record_sync_health(session, entity, _failure(utc_now()))

        await session.refresh(entity)
        assert entity.consecutive_failures == SECOND_CONSECUTIVE_FAILURE

    @pytest.mark.asyncio
    async def test_superseded_success_is_discarded(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Refuse a success from an attempt a completed later one already superseded."""
        manager, entity = sync_health_target
        newer = utc_now()
        await _stamp_sync_health(session, entity, last_synced_at=newer)

        await manager.record_sync_health(
            session, entity, _success(newer - timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == newer

    @pytest.mark.asyncio
    async def test_superseded_failure_is_discarded(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Stop a stale failure restarting a run a newer success already closed."""
        manager, entity = sync_health_target
        newer = utc_now()
        await _stamp_sync_health(session, entity, last_synced_at=newer)

        await manager.record_sync_health(
            session, entity, _failure(newer - timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert entity.sync_failing_since is None
        assert entity.consecutive_failures == 0
        assert entity.last_sync_error is None

    @pytest.mark.asyncio
    async def test_failure_then_newer_success_clears_the_run(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Let a success from a later attempt close a run an earlier failure opened."""
        manager, entity = sync_health_target
        failed_at = utc_now() - timedelta(minutes=5)
        await manager.record_sync_health(session, entity, _failure(failed_at))
        succeeded_at = utc_now()

        await manager.record_sync_health(session, entity, _success(succeeded_at))

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == succeeded_at
        assert entity.sync_failing_since is None
        assert entity.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_behind_a_recorded_success_is_rejected(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Reject a failure whose attempt predates a success already recorded."""
        manager, entity = sync_health_target
        succeeded_at = utc_now()
        await manager.record_sync_health(session, entity, _success(succeeded_at))

        await manager.record_sync_health(
            session, entity, _failure(succeeded_at - timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert entity.consecutive_failures == 0
        assert entity.last_sync_error is None

    @pytest.mark.asyncio
    async def test_success_behind_a_newer_failure_is_rejected(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Refuse a success whose attempt predates the open failure run.

        A failure never moves ``last_synced_at``, so the freshness guard alone
        cannot see it; the failure-run guard is what keeps a late success from
        reporting a clean row whose latest attempt failed.
        """
        manager, entity = sync_health_target
        failed_at = utc_now()
        await manager.record_sync_health(session, entity, _failure(failed_at))

        await manager.record_sync_health(
            session, entity, _success(failed_at - timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert _as_utc(entity.sync_failing_since) == failed_at
        assert entity.consecutive_failures == 1
        assert entity.last_sync_error == "boom"
        assert entity.last_synced_at is None

    @pytest.mark.asyncio
    async def test_success_between_two_failures_of_one_run_is_rejected(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Refuse a success attempted inside a run whose newest attempt failed.

        The success postdates the run's first failure, so only the newest
        attempt seen — not the run start — can tell it is stale.
        """
        manager, entity = sync_health_target
        opened_at = utc_now() - timedelta(minutes=10)
        newest_failure = utc_now()
        await manager.record_sync_health(session, entity, _failure(opened_at))
        await manager.record_sync_health(
            session, entity, _failure(newest_failure, error="newest")
        )

        await manager.record_sync_health(
            session, entity, _success(opened_at + timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert _as_utc(entity.sync_failing_since) == opened_at
        assert entity.consecutive_failures == SECOND_CONSECUTIVE_FAILURE
        assert entity.last_sync_error == "newest"
        assert entity.last_synced_at is None
        assert _as_utc(entity.newest_attempt_at) == newest_failure

    @pytest.mark.parametrize(
        "outcome", [_success, _failure], ids=["success", "failure"]
    )
    @pytest.mark.asyncio
    async def test_accepted_write_records_the_newest_attempt(
        self,
        session: AsyncSession,
        sync_health_target: SyncHealthTarget,
        outcome: Callable[[datetime], SyncHealthWrite],
    ) -> None:
        """Stamp the attempt time whatever the outcome on a row never reported on."""
        manager, entity = sync_health_target
        attempted_at = utc_now()

        await manager.record_sync_health(session, entity, outcome(attempted_at))

        await session.refresh(entity)
        assert _as_utc(entity.newest_attempt_at) == attempted_at

    @pytest.mark.asyncio
    async def test_success_advances_the_newest_attempt_past_a_failure(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Move the newest attempt forward when a later success closes the run."""
        manager, entity = sync_health_target
        await manager.record_sync_health(
            session, entity, _failure(utc_now() - timedelta(minutes=5))
        )
        succeeded_at = utc_now()

        await manager.record_sync_health(session, entity, _success(succeeded_at))

        await session.refresh(entity)
        assert _as_utc(entity.newest_attempt_at) == succeeded_at
        assert entity.sync_failing_since is None

    @pytest.mark.parametrize(
        "outcome", [_success, _failure], ids=["success", "failure"]
    )
    @pytest.mark.asyncio
    async def test_rejected_write_leaves_the_newest_attempt_alone(
        self,
        session: AsyncSession,
        sync_health_target: SyncHealthTarget,
        outcome: Callable[[datetime], SyncHealthWrite],
    ) -> None:
        """Leave the newest attempt untouched by a report a later success superseded."""
        manager, entity = sync_health_target
        newer = utc_now()
        await manager.record_sync_health(session, entity, _success(newer))

        await manager.record_sync_health(
            session, entity, outcome(newer - timedelta(minutes=5))
        )

        await session.refresh(entity)
        assert _as_utc(entity.newest_attempt_at) == newer
        assert _as_utc(entity.last_synced_at) == newer
        assert entity.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_failure_on_a_row_predating_the_column_sets_it(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Treat a missing newest attempt as none seen, so the message is stored."""
        manager, entity = sync_health_target
        await _stamp_sync_health(
            session,
            entity,
            last_sync_error="previous",
            sync_failing_since=utc_now() - timedelta(days=1),
            consecutive_failures=1,
            newest_attempt_at=None,
        )
        failed_at = utc_now()

        await manager.record_sync_health(
            session, entity, _failure(failed_at, error="current")
        )

        await session.refresh(entity)
        assert entity.last_sync_error == "current"
        assert _as_utc(entity.newest_attempt_at) == failed_at

    @pytest.mark.asyncio
    async def test_failure_tied_with_the_newest_attempt_replaces_the_message(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Let arrival order break a tie between two failures of the same instant."""
        manager, entity = sync_health_target
        failed_at = utc_now()
        await manager.record_sync_health(
            session, entity, _failure(failed_at, error="first")
        )

        await manager.record_sync_health(
            session, entity, _failure(failed_at, error="second")
        )

        await session.refresh(entity)
        assert entity.last_sync_error == "second"
        assert _as_utc(entity.newest_attempt_at) == failed_at

    @pytest.mark.asyncio
    async def test_success_tied_with_the_newest_failure_clears_the_run(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Let arrival order break a tie between a failure and a success."""
        manager, entity = sync_health_target
        attempted_at = utc_now()
        await manager.record_sync_health(session, entity, _failure(attempted_at))

        await manager.record_sync_health(session, entity, _success(attempted_at))

        await session.refresh(entity)
        assert _as_utc(entity.last_synced_at) == attempted_at
        assert entity.sync_failing_since is None
        assert entity.last_sync_error is None
        assert _as_utc(entity.newest_attempt_at) == attempted_at

    @pytest.mark.asyncio
    async def test_newest_attempt_reaches_a_retired_row(
        self, session: AsyncSession, sync_health_target: SyncHealthTarget
    ) -> None:
        """Record the newest attempt even once the entity was retired concurrently."""
        manager, entity = sync_health_target
        await retire_in_place(session, entity)
        failed_at = utc_now()

        await manager.record_sync_health(session, entity, _failure(failed_at))

        await session.refresh(entity)
        assert _as_utc(entity.newest_attempt_at) == failed_at


class TestRecordSyncHealthOnPostgreSQL:
    """Test the out-of-order guards against real ``timestamptz`` comparisons.

    SQLite stores these columns as offset-less text, so the ``CASE`` guards are
    string comparisons there; only PostgreSQL proves them as timestamp ones.
    """

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_success_between_two_failures_of_one_run_is_rejected(
        self, postgres_session: AsyncSession
    ) -> None:
        """Keep the run open when a success older than the newest failure lands."""
        node = await NodeManager.create(postgres_session, NodeWriteFactory.build())
        opened_at = utc_now() - timedelta(minutes=10)
        await NodeManager.record_sync_health(
            postgres_session, node, _failure(opened_at)
        )
        await NodeManager.record_sync_health(
            postgres_session, node, _failure(utc_now())
        )

        await NodeManager.record_sync_health(
            postgres_session, node, _success(opened_at + timedelta(minutes=5))
        )

        await postgres_session.refresh(node)
        assert node.sync_failing_since == opened_at
        assert node.last_synced_at is None

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_out_of_order_failures_keep_the_newest_message(
        self, postgres_session: AsyncSession
    ) -> None:
        """Keep the newer message and newest attempt when failures arrive reversed."""
        node = await NodeManager.create(postgres_session, NodeWriteFactory.build())
        earlier = utc_now() - timedelta(minutes=10)
        later = utc_now()

        await NodeManager.record_sync_health(
            postgres_session, node, _failure(later, error="new")
        )
        await NodeManager.record_sync_health(
            postgres_session, node, _failure(earlier, error="old")
        )

        await postgres_session.refresh(node)
        assert node.last_sync_error == "new"
        assert node.newest_attempt_at == later
        assert node.sync_failing_since == earlier
