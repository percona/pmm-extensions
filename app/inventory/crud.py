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

"""Define database operations for the Inventory API."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Final, TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import and_, case, func, literal, Update, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, joinedload
from sqlalchemy.sql import ColumnElement, ColumnExpressionArgument
from sqlalchemy.sql.selectable import ScalarSelect
from sqlmodel import col, or_, select, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db import ListQuerySpec
from app.core.db.crud import BaseSQLModelChildManager, BaseSQLModelManager, W
from app.core.exceptions import (
    HTTPBadRequestException,
    HTTPConflictException,
    HTTPNotFoundException,
)
from app.core.pagination import Pagination
from app.core.utils.date_time import utc_now
from app.core.utils.strings import shorten_text
from app.inventory.constants import (
    ACTIVE_RETIREMENT_KEY,
    RetirableEntityName,
    SYNC_ERROR_MAX_LENGTH,
)
from app.inventory.models import (
    ExternalIdentityAlias,
    HostSystemObservation,
    IdentityLinkDecision,
    IdentityLinkDecisionEnum,
    IdentityLinkDecisionWrite,
    LinkageMethodEnum,
    Node,
    RetirableSQLModel,
    Schema,
    Service,
    ServiceSystemObservation,
    SourceEnum,
    SyncHealthBase,
    SyncHealthWrite,
    SyncOutcomeEnum,
    Table,
)

if TYPE_CHECKING:
    from sqlmodel.sql.expression import SelectOfScalar


def _retire(
    model: type[RetirableSQLModel],
    *whereclause: ColumnExpressionArgument[bool],
    retired_at: datetime,
) -> Update:
    """Build the UPDATE retiring every active row matching the clauses.

    :param model: The table to retire rows in.
    :param whereclause: Clauses narrowing the rows to retire.
    :param retired_at: The timestamp to stamp on each retired row.
    :return: The UPDATE statement.
    """
    return (
        update(model)
        .where(col(model.retired_at).is_(None), *whereclause)
        .values(retired_at=retired_at, retirement_key=col(model.id))
    )


def _revive(
    model: type[RetirableSQLModel], *whereclause: ColumnExpressionArgument[bool]
) -> Update:
    """Build the UPDATE clearing retirement on every retired row matching the clauses.

    :param model: The table to revive rows in.
    :param whereclause: Clauses narrowing the rows to revive.
    :return: The UPDATE statement.
    """
    return (
        update(model)
        .where(col(model.retired_at).is_not(None), *whereclause)
        .values(retired_at=None, retirement_key=ACTIVE_RETIREMENT_KEY)
    )


#: Keeps the sync-health writes off SQLAlchemy's post-UPDATE session sync. Its
#: "evaluate" strategy re-runs the guards below in Python against whatever the
#: identity map holds, comparing a stored timestamp that is naive on an engine
#: dropping the offset (SQLite) against a timezone-aware attempt time — which
#: raises ``TypeError`` rather than falling through to a fetch. Sessions are
#: built with ``expire_on_commit=False``, so a loaded instance stays stale for
#: the rest of the request; no route reads one of these rows back after the
#: write, and one that starts to must re-read rather than trust the instance.
_UNSYNCHRONIZED: Final = {"synchronize_session": False}


def _not_superseded(
    model: type[SyncHealthBase], attempted_at: datetime
) -> ColumnElement[bool]:
    """Match only rows whose recorded sync is not newer than this attempt.

    Reports cross the service boundary over HTTP, so arrival order is not
    attempt order. Comparing against ``last_synced_at`` discards a report from
    an attempt that had already been superseded by a completed later one.

    :param model: The table being written.
    :param attempted_at: When the reporting syncer began its attempt.
    :return: The guard predicate.
    """
    return or_(
        col(model.last_synced_at).is_(None),
        col(model.last_synced_at) <= attempted_at,
    )


def _no_newer_attempt(
    model: type[SyncHealthBase], attempted_at: datetime
) -> ColumnElement[bool]:
    """Test whether a row's newest recorded attempt is not newer than this one.

    Guards a success's ``WHERE`` and a failure's message ``SET`` alike. A
    failure never moves ``last_synced_at``, so :func:`_not_superseded` cannot
    see one: an older success arriving late would otherwise clear a run a newer
    attempt had failed in. ``sync_failing_since`` names only the run's *first*
    failure, so it would still admit a success attempted between two failures
    of one run. A tie passes, leaving arrival order to break it.

    :param model: The table being written.
    :param attempted_at: When the reporting syncer began its attempt.
    :return: The guard predicate.
    """
    return or_(
        col(model.newest_attempt_at).is_(None),
        col(model.newest_attempt_at) <= attempted_at,
    )


def _newest_attempt(
    model: type[SyncHealthBase], attempted_at: datetime
) -> ColumnElement[datetime]:
    """Build the later of the stored newest attempt and this one.

    Spelled as ``CASE`` rather than ``greatest``: SQLite has no ``greatest``,
    and its two-argument ``max`` returns NULL when either side is NULL.

    :param model: The table being written.
    :param attempted_at: When the reporting syncer began its attempt.
    :return: The expression to assign to ``newest_attempt_at``.
    """
    return case(
        (
            col(model.newest_attempt_at) > attempted_at,
            col(model.newest_attempt_at),
        ),
        else_=attempted_at,
    )


def _record_sync_success(
    model: type[SyncHealthBase],
    *whereclause: ColumnExpressionArgument[bool],
    synced_at: datetime,
) -> Update:
    """Build the UPDATE stamping a clean sync on every row matching the clauses.

    ``synced_at`` is the syncer's attempt time, not the moment this statement
    runs, so ``last_synced_at`` answers "when was this confirmed against its
    source" rather than "when did the report arrive".

    :param model: The table to record the success in.
    :param whereclause: Clauses narrowing the rows to write.
    :param synced_at: When the reporting syncer began its attempt.
    :return: The UPDATE statement.
    """
    return (
        update(model)
        .where(
            _not_superseded(model, synced_at),
            _no_newer_attempt(model, synced_at),
            *whereclause,
        )
        .values(
            last_synced_at=synced_at,
            last_sync_error=None,
            sync_failing_since=None,
            consecutive_failures=0,
            newest_attempt_at=_newest_attempt(model, synced_at),
        )
        .execution_options(**_UNSYNCHRONIZED)
    )


def _record_sync_failure(
    model: type[SyncHealthBase],
    *whereclause: ColumnExpressionArgument[bool],
    error: str,
    failed_at: datetime,
) -> Update:
    """Build the UPDATE recording a failed sync on every row matching the clauses.

    ``sync_failing_since`` keeps the earlier of the stored value and this
    attempt rather than being assigned, so it names the *first* failure after
    the last success even when two failures of one run arrive out of order —
    coalescing alone would leave it on whichever landed first. The counter is
    incremented in SQL so concurrent runs cannot lose an increment to a
    read-modify-write. ``last_sync_error`` is replaced only when this attempt
    is not older than ``newest_attempt_at``, so an out-of-order pair keeps the
    newer message; the ``SET`` expressions all read the row as it stood before
    the statement, so that comparison sees the stored value rather than the
    one this statement writes. ``last_synced_at`` is deliberately absent: a
    failure never moves it — which is also why the guard compares against it
    rather than being skipped here.

    :param model: The table to record the failure in.
    :param whereclause: Clauses narrowing the rows to write.
    :param error: The bounded description to store.
    :param failed_at: When the reporting syncer began its attempt.
    :return: The UPDATE statement.
    """
    return (
        update(model)
        .where(_not_superseded(model, failed_at), *whereclause)
        .values(
            last_sync_error=case(
                (_no_newer_attempt(model, failed_at), error),
                else_=col(model.last_sync_error),
            ),
            consecutive_failures=col(model.consecutive_failures) + 1,
            sync_failing_since=case(
                (
                    col(model.sync_failing_since) < failed_at,
                    col(model.sync_failing_since),
                ),
                else_=failed_at,
            ),
            newest_attempt_at=_newest_attempt(model, failed_at),
        )
        .execution_options(**_UNSYNCHRONIZED)
    )


def _retained(
    model: type[RetirableSQLModel],
    retired_before: datetime,
    keep_ids: Collection[int],
) -> ColumnElement[bool]:
    """Build the predicate matching rows collection must leave in place.

    A row is retained when it is still active, when its tombstone has not aged
    past the cutoff, or when the caller declared something still resolves it.

    :param model: The table to build the predicate against.
    :param retired_before: The cutoff a tombstone must predate to be collectible.
    :param keep_ids: The ids the caller declared still referenced.
    :return: The predicate matching this table's retained rows.
    """
    clauses = [
        col(model.retired_at).is_(None),
        col(model.retired_at) >= retired_before,
    ]
    if keep_ids:
        clauses.append(col(model.id).in_(keep_ids))
    return or_(*clauses)


def _retained_descendant_exists(
    subtree: Sequence[tuple[type[RetirableSQLModel], str]],
    parent_id: ColumnElement,
    retired_before: datetime,
    keep_by_model: Mapping[type[RetirableSQLModel], Collection[int]],
) -> ColumnElement[bool] | None:
    """Build the predicate matching an ancestor some descendant still pins.

    The subtree is a linear descent, so each level correlates on its own parent
    and nests the level below it. Expressing the descent in SQL rather than
    materializing the retained ids keeps the scan independent of how large the
    active inventory is.

    :param subtree: The descendant models, nearest first, each paired with the
        foreign key naming its own parent.
    :param parent_id: The primary-key column the nearest descendant correlates on.
    :param retired_before: The cutoff a tombstone must predate to be collectible.
    :param keep_by_model: The ids the caller declared still referenced, per table.
    :return: The predicate, or None when the subtree is empty.
    """
    if not subtree:
        return None
    (model, foreign_key), rest = subtree[0], subtree[1:]
    retained = [_retained(model, retired_before, keep_by_model.get(model, ()))]
    deeper = _retained_descendant_exists(
        rest, col(model.id), retired_before, keep_by_model
    )
    if deeper is not None:
        retained.append(deeper)
    return (
        select(literal(1))
        .where(col(getattr(model, foreign_key)) == parent_id, or_(*retained))
        .exists()
    )


class SyncHealthManagerMixin(BaseSQLModelManager):
    """Record the outcome of one syncer attempt on an entity."""

    @classmethod
    async def record_sync_health(
        cls,
        session: AsyncSession,
        instance: RetirableSQLModel,
        outcome: SyncHealthWrite,
    ) -> None:
        """Apply one sync outcome to an entity's sync-health columns.

        The statement is hand-built rather than routed through ``update``, for
        the reason :meth:`RetirableManagerMixin.retire`'s is: the transitions
        are expressed in SQL so an increment cannot be lost to a
        read-modify-write, and the write must reach a row the manager's own
        retired filter would hide.

        :param session: The asynchronous database session to use.
        :param instance: The entity the outcome was observed for. Typed as the
            retirable base ``retire`` takes rather than as ``SyncHealthBase``,
            which carries the columns but not the ``id`` this addresses the row
            by; ``cls.Model`` is what confines the write to a level that has
            them.
        :param outcome: What the syncer reported.
        :raises ValueError: If the outcome names no branch here, which an
            outcome added to :class:`SyncOutcomeEnum` without a transition
            would. Failing loudly beats routing it to the failure branch, where
            the body model does not require an ``error``.
        """
        if outcome.outcome is SyncOutcomeEnum.SUCCESS:
            statement = _record_sync_success(
                cls.Model,
                col(cls.Model.id) == instance.id,
                synced_at=outcome.attempted_at,
            )
        elif outcome.error is not None:
            statement = _record_sync_failure(
                cls.Model,
                col(cls.Model.id) == instance.id,
                error=shorten_text(outcome.error, SYNC_ERROR_MAX_LENGTH),
                failed_at=outcome.attempted_at,
            )
        else:
            raise ValueError(f"No sync-health transition for {outcome.outcome!r}")
        await cls._exec(session, statement)  # call-shape-dup-ok: the manager idiom
        await session.commit()


class RetirableManagerMixin(BaseSQLModelManager):
    """Confine an entity's reads to the tombstone policy.

    ``_filter_query`` is the single funnel every read reaches, so overriding it
    covers ``list``, ``first``, ``get``, ``get_or_404``, ``list_query_paginated``,
    ``count`` and ``exists`` at once. It also reaches the bulk DML helpers, so
    ``update_where`` and ``delete_where`` can never touch a tombstone through the
    default managers — a collection job will have to go through the retired-
    inclusive siblings or it will silently match nothing. The opt-out rides
    ``cls`` rather than a call argument because ``BaseManager._select`` splits a
    paginated ``select_related`` read into a page query and a hydration query and
    forwards neither ``equal_filters`` nor the original clauses to the second one:
    an opt-out passed per call would be dropped there and the injection would fire
    on the hydration query alone, returning a short page whose ``total`` does not
    match it.

    :cvar include_retired: Whether reads through this manager see tombstones.
    :cvar retirement_subtree: The descendant models retirement cascades into,
        nearest first, each paired with the foreign key naming its own parent.
    """

    include_retired: ClassVar[bool] = False
    retirement_subtree: ClassVar[tuple[tuple[type[RetirableSQLModel], str], ...]] = ()

    @classmethod
    def _filter_query(
        cls,
        query: W,
        *whereclause: ColumnExpressionArgument[bool],
        select_related: Sequence = (),
        query_options: Sequence = (),
        **equal_filters: Any,
    ) -> W:
        if not cls.include_retired:
            whereclause = (*whereclause, col(cls.Model.retired_at).is_(None))
            select_related = cls._scope_eager_loads(select_related)
        return super()._filter_query(
            query,
            *whereclause,
            select_related=select_related,
            query_options=query_options,
            **equal_filters,
        )

    @classmethod
    def _scope_eager_loads(cls, select_related: Sequence) -> Sequence:
        """Restrict eagerly-loaded retirable collections to their active rows.

        ``_filter_query`` turns ``select_related`` into ``joinedload(attr)``, and
        the root WHERE clause never reaches the loaded relationship, so a retired
        child would still be emitted nested inside an active parent. Only
        collections are rewritten: scoping a required many-to-one parent would
        turn the relationship into ``None`` rather than filter it.

        :param select_related: The relationship attributes to eagerly load.
        :return: The same attributes, with retirable collections narrowed to
            their active rows.
        """
        scoped: list[Any] = []
        for attr in select_related:
            relationship = attr.property
            target = relationship.mapper.class_
            if relationship.uselist and issubclass(target, RetirableSQLModel):
                scoped.append(attr.and_(col(target.retired_at).is_(None)))
            else:
                scoped.append(attr)
        return scoped

    @classmethod
    def _retirement_statements(
        cls, entity_id: int, retired_at: datetime
    ) -> list[Update]:
        """Build the statements retiring an entity and its subtree, deepest first.

        Descendants retire before their ancestor so that even a torn write can
        only ever leave retired descendants under an active ancestor, never an
        active row under a retired one. The ``retired_at IS NULL`` guard replaces
        the manager injection these hand-built statements do not inherit, and is
        what leaves an already-retired row's original timestamp alone.

        :param entity_id: The primary key of the entity being retired.
        :param retired_at: The timestamp to stamp on every row retired here.
        :return: The UPDATE statements to run in order, in one transaction.
        """
        statements = [
            _retire(cls.Model, col(cls.Model.id) == entity_id, retired_at=retired_at)
        ]
        parent_ids: SelectOfScalar[int | None] = select(col(cls.Model.id)).where(
            col(cls.Model.id) == entity_id
        )
        for model, foreign_key in cls.retirement_subtree:
            parent_key = col(getattr(model, foreign_key))
            statements.append(
                _retire(model, parent_key.in_(parent_ids), retired_at=retired_at)
            )
            parent_ids = select(col(model.id)).where(parent_key.in_(parent_ids))
        statements.reverse()
        return statements

    @classmethod
    def _revival_statements(cls, entity_id: int) -> list[Update]:
        """Build the statements reviving an entity and its ancestors, topmost first.

        Revival is the mirror image of retirement: ancestors clear before their
        descendant, so a torn write cannot leave an active row under a retired
        ancestor. Siblings and descendants are left retired — a node coming back
        must not resurrect the services its upstream no longer reports.

        :param entity_id: The primary key of the entity being revived.
        :return: The UPDATE statements to run in order, in one transaction.
        """
        ancestors: list[Update] = []
        entity_ids: SelectOfScalar[int | None] = select(col(cls.Model.id)).where(
            col(cls.Model.id) == entity_id
        )
        manager = cls
        while issubclass(manager, BaseSQLModelChildManager):
            entity_ids = select(
                col(getattr(manager.Model, manager.connected_by))
            ).where(col(manager.Model.id).in_(entity_ids))
            parent_model = manager.ParentManager.Model
            ancestors.append(
                _revive(parent_model, col(parent_model.id).in_(entity_ids))
            )
            manager = manager.ParentManager
        ancestors.reverse()
        return [*ancestors, _revive(cls.Model, col(cls.Model.id) == entity_id)]

    @classmethod
    def _identity_link_pin(cls) -> ColumnElement[bool] | None:
        """Return the predicate matching a row a standing identity link pins.

        None for an entity type that carries no external identity of its own, so
        collection pays nothing for a clause that could never match.

        :return: The predicate, or None when this entity type cannot be linked.
        """
        return None

    @classmethod
    async def retire(cls, session: AsyncSession, instance: RetirableSQLModel) -> None:
        """Retire an entity and everything below it in one transaction.

        :param session: The asynchronous database session to use.
        :param instance: The entity to retire.
        """
        for statement in cls._retirement_statements(instance.id, utc_now()):
            await cls._exec(session, statement)
        await session.commit()

    @classmethod
    async def revive(cls, session: AsyncSession, instance: RetirableSQLModel) -> None:
        """Revive an entity and its retired ancestors in one transaction.

        :param session: The asynchronous database session to use.
        :param instance: The entity to revive.
        :raises HTTPConflictException: If an active row already holds the unique
            key the revived entity would reclaim.
        """
        try:
            for statement in cls._revival_statements(instance.id):
                await cls._exec(session, statement)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPConflictException(
                f"{cls.Model.__name__} cannot be revived: an active entity already "
                "holds its unique key."
            ) from None

    @classmethod
    async def collectible_ids(
        cls,
        session: AsyncSession,
        *,
        retired_before: datetime,
        keep_by_model: Mapping[type[RetirableSQLModel], Collection[int]],
        limit: int,
    ) -> list[int]:
        """Return the ids of this table's tombstones eligible for deletion.

        A tombstone is eligible when it aged past ``retired_before``, no caller
        declared it referenced, and nothing in its subtree is retained. Only
        reachable through the retired-inclusive subclasses: the default managers'
        ``retired_at IS NULL`` guard makes the underlying read match nothing.

        :param session: The asynchronous database session to use.
        :param retired_before: The cutoff a tombstone must predate.
        :param keep_by_model: The ids a caller declared still referenced, per table.
        :param limit: The most ids to return.
        :return: The eligible ids, lowest first.
        """
        whereclause = [
            col(cls.Model.retired_at).is_not(None),
            col(cls.Model.retired_at) < retired_before,
        ]
        if keep_ids := keep_by_model.get(cls.Model, ()):
            whereclause.append(col(cls.Model.id).not_in(keep_ids))
        if (
            pinned := _retained_descendant_exists(
                cls.retirement_subtree,
                col(cls.Model.id),
                retired_before,
                keep_by_model,
            )
        ) is not None:
            whereclause.append(~pinned)
        if (linked := cls._identity_link_pin()) is not None:
            whereclause.append(~linked)
        query = cls._filter_query(select(col(cls.Model.id)), *whereclause)
        result = await cls._exec(
            session, query.order_by(col(cls.Model.id)).limit(limit)
        )
        return [entity_id for entity_id in result.all() if entity_id is not None]

    @classmethod
    async def collect(cls, session: AsyncSession, entity_ids: Collection[int]) -> int:
        """Delete the named tombstones, leaving any active row among them alone.

        The ``retired_at IS NOT NULL`` filter is a guard, not an optimization: a
        caller that miscomputed its closure must not be able to hard-delete a
        live row through this method. An id that no longer exists simply matches
        nothing, so re-running an already-collected batch is a zero-row no-op.

        :param session: The asynchronous database session to use.
        :param entity_ids: The ids to delete.
        :return: The number of rows deleted.
        """
        if not entity_ids:
            return 0
        result = await cls.delete_where(
            session,
            col(cls.Model.id).in_(entity_ids),
            col(cls.Model.retired_at).is_not(None),
        )
        return result.rowcount


#: The decisions that keep a pairing off the candidate list. A confirmation has
#: already linked it; a rejection said it names two machines. Confirm eligibility
#: is deliberately narrower — see
#: :meth:`AliasableManagerMixin.confirm_identity_link`.
SUPPRESSING_DECISIONS: Final = (
    IdentityLinkDecisionEnum.CONFIRMED,
    IdentityLinkDecisionEnum.REJECTED,
)


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    """Pair two rows a re-registration may have split one machine into.

    :param predecessor: The older row, which a confirmation keeps.
    :param successor: The newer row, which a confirmation retires.
    :param matched_on: The signals that agreed, informational only.
    """

    predecessor: RetirableSQLModel
    successor: RetirableSQLModel
    matched_on: list[str]


class ExternalIdentityAliasManager(BaseSQLModelManager):
    """Manage the append-only external-identity binding records.

    :ivar Model: The SQLModel class this manager is responsible for
        (``ExternalIdentityAlias``).
    """

    Model = ExternalIdentityAlias

    @classmethod
    async def binding_started_at(
        cls,
        session: AsyncSession,
        entity_type: RetirableEntityName,
        entity_id: int,
        external_id: str,
    ) -> datetime | None:
        """Return when a row's current binding to an identifier took effect.

        None means no record binds them yet, which is the ordinary case for a
        row still holding the identifier it was created with — nothing writes an
        alias record on ordinary sync creation.

        :param session: The asynchronous database session to use.
        :param entity_type: The inventory entity type the row belongs to.
        :param entity_id: The row the identifier currently resolves to.
        :param external_id: The upstream identifier.
        :return: The start of the standing binding, or None when none is recorded.
        """
        query = (
            select(col(cls.Model.valid_from))
            .where(
                col(cls.Model.entity_type) == entity_type,
                col(cls.Model.entity_id) == entity_id,
                col(cls.Model.external_id) == external_id,
            )
            .order_by(col(cls.Model.valid_from).desc(), col(cls.Model.id).desc())
            .limit(1)
        )
        result = await cls._exec(  # call-shape-dup-ok: _exec already names the intent
            session, query
        )
        return result.first()

    @classmethod
    async def resolve_entity_id(
        cls,
        session: AsyncSession,
        entity_type: RetirableEntityName,
        source: SourceEnum | None,
        external_id: str,
    ) -> int | None:
        """Return the row an upstream identifier resolves to, or None.

        A None answer is the ordinary case rather than an error: nothing writes
        an alias record on ordinary sync creation, so only identifiers that have
        been through a link carry any history, and every other one keeps
        resolving by the ``external_id`` column as before.

        The newest record names the row the identifier was last *bound* to, which
        is not the same question as which row answers for it now: a later
        confirmation may have absorbed that row without touching this
        identifier's records, because a confirmation transfers only the one
        identifier its successor currently holds. So the binding is followed
        through :meth:`IdentityLinkDecisionManager.surviving_entity_id`, and a
        reversal makes the hop stop by retracting the confirmation rather than by
        rewriting any binding.

        :param session: The asynchronous database session to use.
        :param entity_type: The inventory entity type the identifier names.
        :param source: The upstream system the identifier belongs to, or None to
            resolve it under whichever source recorded it.
        :param external_id: The upstream identifier to resolve.
        :return: The primary key the identifier currently resolves to, or None
            when no record names it.
        """
        whereclause = [
            col(cls.Model.entity_type) == entity_type,
            col(cls.Model.external_id) == external_id,
        ]
        if source is not None:
            whereclause.append(col(cls.Model.source) == source)
        query = (
            select(col(cls.Model.entity_id))
            .where(*whereclause)
            .order_by(col(cls.Model.valid_from).desc(), col(cls.Model.id).desc())
            .limit(1)
        )
        result = await cls._exec(  # call-shape-dup-ok: _exec already names the intent
            session, query
        )
        bound_to = result.first()
        if bound_to is None:
            return None
        return await IdentityLinkDecisionManager.surviving_entity_id(
            session, entity_type, bound_to
        )


class IdentityLinkDecisionManager(BaseSQLModelManager):
    """Manage the append-only log of operator decisions over candidate pairings.

    :ivar Model: The SQLModel class this manager is responsible for
        (``IdentityLinkDecision``).
    """

    Model = IdentityLinkDecision

    @classmethod
    def latest_decision_subquery(
        cls,
        entity_type: RetirableEntityName,
        predecessor_id: Any,
        successor_id: Any,
    ) -> ScalarSelect:
        """Return a correlated subquery yielding a pairing's most recent decision.

        A correlated ``ORDER BY … LIMIT 1`` rather than a window function, which
        renders identically on both supported engines.

        :param entity_type: The inventory entity type the pairing names.
        :param predecessor_id: The column or value naming the older row.
        :param successor_id: The column or value naming the newer row.
        :return: The scalar subquery.
        """
        decision = aliased(cls.Model)
        return (
            select(col(decision.decision))
            .where(
                col(decision.entity_type) == entity_type,
                col(decision.predecessor_id) == predecessor_id,
                col(decision.successor_id) == successor_id,
            )
            .order_by(col(decision.id).desc())
            .limit(1)
            .scalar_subquery()
        )

    @classmethod
    def _standing_confirmation(
        cls,
        entity_type: RetirableEntityName,
        *,
        predecessor_id: Any = None,
        successor_id: Any = None,
    ) -> tuple[Any, list[ColumnElement[bool]]]:
        """Return the decision alias and the clauses selecting a standing confirmation.

        Callers that only need to know whether such a decision exists take
        :meth:`confirmed_link_exists`; the alias is handed back for the one
        caller that needs a column off the row itself.

        :param entity_type: The inventory entity type the pairing names.
        :param predecessor_id: The column or value the older row must equal, or
            None to leave it unconstrained.
        :param successor_id: The column or value the newer row must equal, or
            None to leave it unconstrained.
        :return: The aliased decision model, and the clauses to be ANDed together.
        """
        decision = aliased(cls.Model)
        newest = aliased(cls.Model)
        latest_id = (
            select(func.max(col(newest.id)))
            .where(
                col(newest.entity_type) == col(decision.entity_type),
                col(newest.predecessor_id) == col(decision.predecessor_id),
                col(newest.successor_id) == col(decision.successor_id),
            )
            .scalar_subquery()
        )
        clauses = [
            col(decision.entity_type) == entity_type,
            col(decision.decision) == IdentityLinkDecisionEnum.CONFIRMED,
            col(decision.id) == latest_id,
        ]
        if predecessor_id is not None:
            clauses.append(col(decision.predecessor_id) == predecessor_id)
        if successor_id is not None:
            clauses.append(col(decision.successor_id) == successor_id)
        return decision, clauses

    @classmethod
    def confirmed_link_exists(
        cls,
        entity_type: RetirableEntityName,
        *,
        predecessor_id: Any = None,
        successor_id: Any = None,
    ) -> ColumnElement[bool]:
        """Return the predicate matching a pairing whose standing decision confirms it.

        Either endpoint may be left unconstrained, which then matches any pairing
        holding the other one.

        :param entity_type: The inventory entity type the pairing names.
        :param predecessor_id: The column or value the older row must equal, or
            None to leave it unconstrained.
        :param successor_id: The column or value the newer row must equal, or
            None to leave it unconstrained.
        :return: The ``EXISTS`` predicate.
        """
        _, clauses = cls._standing_confirmation(
            entity_type, predecessor_id=predecessor_id, successor_id=successor_id
        )
        return select(literal(1)).where(*clauses).exists()

    @classmethod
    async def surviving_entity_id(
        cls,
        session: AsyncSession,
        entity_type: RetirableEntityName,
        entity_id: int,
    ) -> int:
        """Follow the standing confirmations that absorbed a row into its survivor.

        A row confirmed away is retired and its identifier moved onto the
        predecessor, so anything still naming it means the predecessor. Absorbing
        composes: a machine re-registered twice is reconciled newest pair first,
        which leaves the middle row a predecessor in one link and a successor in
        the next, and only walking the chain to its end answers for the
        identifiers it held along the way.

        The walk terminates without a visited set because every confirmation
        passes the structural predicate's ``predecessor.id < successor.id``, so
        each hop strictly decreases and no cycle can be recorded.

        :param session: The asynchronous database session to use.
        :param entity_type: The inventory entity type the row belongs to.
        :param entity_id: The row to start from.
        :return: The row that answers for it, which is ``entity_id`` itself when
            no confirmation stands over it.
        """
        while True:
            decision, clauses = cls._standing_confirmation(
                entity_type, successor_id=entity_id
            )
            result = await cls._exec(
                session,
                select(col(decision.predecessor_id)).where(*clauses).limit(1),
            )
            predecessor_id = result.first()
            if predecessor_id is None:
                return entity_id
            entity_id = predecessor_id

    @classmethod
    async def latest_for_pairing(
        cls,
        session: AsyncSession,
        entity_type: RetirableEntityName,
        predecessor_id: int,
        successor_id: int,
    ) -> IdentityLinkDecision | None:
        """Return the most recent decision recorded for one pairing.

        :param session: The asynchronous database session to use.
        :param entity_type: The inventory entity type the pairing names.
        :param predecessor_id: The older row of the pairing.
        :param successor_id: The newer row of the pairing.
        :return: The newest decision row, or None when the pairing has none.
        """
        query = (
            select(cls.Model)
            .where(
                col(cls.Model.entity_type) == entity_type,
                col(cls.Model.predecessor_id) == predecessor_id,
                col(cls.Model.successor_id) == successor_id,
            )
            .order_by(col(cls.Model.id).desc())
            .limit(1)
        )
        result = await cls._exec(session, query)
        return result.first()

    @classmethod
    async def confirms_successor(
        cls,
        session: AsyncSession,
        entity_type: RetirableEntityName,
        entity_id: int,
    ) -> bool:
        """Return whether a standing confirmation holds this row as its successor.

        Such a row is retired by an operator decision rather than by absence, so
        its tombstone is load-bearing: the link that put it there is still
        reversible, and reviving it early would strand the identifier its
        predecessor now carries.

        :param session: The asynchronous database session to use.
        :param entity_type: The inventory entity type the row belongs to.
        :param entity_id: The row to test.
        :return: True when a confirmation stands over it.
        """
        result = await cls._exec(
            session,
            select(cls.confirmed_link_exists(entity_type, successor_id=entity_id)),
        )
        return bool(result.one())


class AliasableManagerMixin(RetirableManagerMixin):
    """Confine identity-link operations to the entities carrying an external id.

    Schemas and tables are keyed by name within their parent, so no upstream
    identity changes under them and they keep the plain retirable mixin.

    The overridable hooks below annotate their entity argument ``Any`` rather
    than ``RetirableSQLModel``. The subclasses read columns the shared base does
    not declare — ``external_id`` and ``name`` on both, ``node_id`` on services —
    so the narrower annotation would promise a shape none of them actually
    receives.

    Every read here lifts the tombstone filter by building its own statement
    rather than going through ``_filter_query``: a predecessor is routinely a
    tombstone, and so is a successor whose node was linked before it. Building
    the statement rather than reaching for the retired-inclusive sibling keeps
    these operations correct whichever spelling of the manager a caller reaches
    for.

    :cvar entity_name: The entity type this manager's alias and decision rows are
        recorded under.
    :cvar identity_match_signals: The columns reported alongside ``name`` in a
        candidate's ``matched_on`` when they happen to agree. Informational only
        — detection never requires them.
    :cvar identity_select_related: The relationships a candidate's rows are
        hydrated with, so the nested response models come out complete.
    """

    entity_name: ClassVar[RetirableEntityName]
    identity_match_signals: ClassVar[tuple[str, ...]] = ()
    identity_select_related: ClassVar[tuple[Any, ...]] = ()

    @classmethod
    def _structural_pairing_clauses(
        cls, predecessor: Any, successor: Any
    ) -> list[ColumnExpressionArgument[bool]]:
        """Return the clauses making a pairing coherent to act on at all.

        The lower autoincrement id is the older row. References PMM Extensions persisted
        before the split name that row's primary key, so it is the side a
        confirmation keeps.

        :param predecessor: The aliased model standing for the older row.
        :param successor: The aliased model standing for the newer row.
        :return: The clauses, to be ANDed together.
        """
        return [
            col(predecessor.name) == col(successor.name),
            col(predecessor.external_id) != col(successor.external_id),
            col(predecessor.id) < col(successor.id),
        ]

    @classmethod
    async def _identity_source(
        cls, session: AsyncSession, entity: Any, /
    ) -> SourceEnum:
        """Return the upstream system this entity's identifiers belong to.

        :param session: The asynchronous database session to use.
        :param entity: The row whose provenance is wanted.
        :return: The source.
        :raises NotImplementedError: Always — an aliasable manager declares where
            its own provenance is read from.
        """
        raise NotImplementedError

    @classmethod
    async def _require_revivable(
        cls,
        session: AsyncSession,
        successor: Any,
    ) -> None:
        """Refuse a reversal whose successor cannot legally come back yet.

        A top-level entity has no ancestor to obstruct it, so the default admits
        every reversal and the child managers narrow it.

        :param session: The asynchronous database session to use.
        :param successor: The row a reversal would revive.
        """

    @classmethod
    def _identity_link_pin(cls) -> ColumnElement[bool]:
        """Return the predicate matching a row a standing identity link pins.

        A confirmed link's successor is a tombstone nothing references any more,
        so collection would otherwise age it out and make the reversal
        permanently impossible with no signal.

        Narrowed from the base's optional return: an aliasable entity always has
        a pin, which is what lets a subclass widen this one by ``or_``-ing onto
        ``super()`` without re-testing for None.

        :return: The ``EXISTS`` predicate.
        """
        return IdentityLinkDecisionManager.confirmed_link_exists(
            cls.entity_name, successor_id=col(cls.Model.id)
        )

    @classmethod
    async def _hydrate(
        cls, session: AsyncSession, entity_ids: Collection[int]
    ) -> dict[int, Any]:
        """Read the named rows with their nested relationships, tombstones included.

        :param session: The asynchronous database session to use.
        :param entity_ids: The primary keys to read.
        :return: The rows, keyed by primary key.
        """
        if not entity_ids:
            return {}
        query = select(cls.Model).where(col(cls.Model.id).in_(entity_ids))
        for relationship in cls.identity_select_related:
            query = query.options(joinedload(relationship))
        result = await cls._exec(session, query)
        return {
            entity.id: entity
            for entity in result.unique().all()
            if entity.id is not None
        }

    @classmethod
    async def _read_including_retired(
        cls, session: AsyncSession, entity_id: int
    ) -> Any | None:
        """Read one row by primary key with the tombstone filter lifted.

        ``populate_existing`` is what makes this a re-read rather than a handout
        of whatever the identity map already holds, which is the whole point of
        calling it once a row lock is held.

        :param session: The asynchronous database session to use.
        :param entity_id: The primary key to read.
        :return: The row, or None when no row carries that key.
        """
        query = (
            select(cls.Model)
            .where(col(cls.Model.id) == entity_id)
            .execution_options(populate_existing=True)
        )
        result = await cls._exec(  # call-shape-dup-ok: _exec already names the intent
            session, query
        )
        return result.first()

    @classmethod
    async def identity_candidates(
        cls, session: AsyncSession, *, pagination: Pagination
    ) -> tuple[list[IdentityCandidate], int]:
        """Return the page of pairings a re-registration may have split.

        The list is the structural predicate minus the pairings an operator has
        already confirmed or rejected, which is what stops a rejected suggestion
        coming back on the next sync.

        :param session: The asynchronous database session to use.
        :param pagination: The offset/limit window to return.
        :return: The page's candidates, and the total matching the same
            predicate.
        """
        predecessor = aliased(cls.Model, name="predecessor")
        successor = aliased(cls.Model, name="successor")
        latest_decision = IdentityLinkDecisionManager.latest_decision_subquery(
            cls.entity_name, col(predecessor.id), col(successor.id)
        )
        pairing = and_(
            *cls._structural_pairing_clauses(predecessor, successor),
            or_(
                latest_decision.is_(None),
                latest_decision.not_in(SUPPRESSING_DECISIONS),
            ),
        )
        total = await cls._exec(
            session,
            select(func.count())
            .select_from(predecessor)
            .join(successor, onclause=pairing),
        )
        page = await cls._exec(
            session,
            select(col(predecessor.id), col(successor.id))
            .select_from(predecessor)
            .join(successor, onclause=pairing)
            .order_by(col(predecessor.id), col(successor.id))
            .offset(pagination.offset)
            .limit(pagination.limit),
        )
        pairs = list(page.all())
        entities = await cls._hydrate(
            session, {entity_id for pair in pairs for entity_id in pair}
        )
        candidates = [
            IdentityCandidate(
                predecessor=entities[predecessor_id],
                successor=entities[successor_id],
                matched_on=cls._matched_signals(
                    entities[predecessor_id], entities[successor_id]
                ),
            )
            for predecessor_id, successor_id in pairs
        ]
        return candidates, total.one()

    @classmethod
    def _matched_signals(cls, predecessor: Any, successor: Any) -> list[str]:
        """Return the signals that agree across a pairing, name first.

        :param predecessor: The older row of the pairing.
        :param successor: The newer row of the pairing.
        :return: The names of the agreeing signals.
        """
        agreed = ["name"]
        agreed.extend(
            signal
            for signal in cls.identity_match_signals
            if getattr(predecessor, signal) is not None
            and getattr(predecessor, signal) == getattr(successor, signal)
        )
        return agreed

    @classmethod
    async def _lock_pairing(
        cls, session: AsyncSession, predecessor_id: int, successor_id: int
    ) -> None:
        """Lock both rows of a pairing, lowest id first.

        Two operations over overlapping pairs therefore take their locks in the
        same order and cannot deadlock. The clause is defence in depth for
        PostgreSQL; the correctness the tests assert is the re-check that follows
        it, which is application-level and behaves the same on both backends.

        :param session: The asynchronous database session to use.
        :param predecessor_id: The older row of the pairing.
        :param successor_id: The newer row of the pairing.
        """
        for entity_id in sorted((predecessor_id, successor_id)):
            await cls._exec(
                session,
                select(col(cls.Model.id))
                .where(col(cls.Model.id) == entity_id)
                .with_for_update(),
            )

    @classmethod
    async def _open_operation(
        cls,
        session: AsyncSession,
        predecessor: RetirableSQLModel,
        successor_id: int,
        *,
        missing_successor: HTTPException,
    ) -> tuple[Any, Any, IdentityLinkDecision | None]:
        """Lock a pairing and read back both rows and the decision governing them.

        Re-reading here is the whole of the concurrency design: checking
        eligibility against state read before the lock lets two callers both pass
        and both append, and because the row mutations are individually
        idempotent nothing fails loudly — only the decision log records one event
        twice. The rows and the decision are read together because they are one
        piece of state for that purpose; each operation then applies its own
        eligibility rule to the decision it gets back.

        :param session: The asynchronous database session to use.
        :param predecessor: The row addressed by the path.
        :param successor_id: The row named by the request body.
        :param missing_successor: What to raise when no row carries
            ``successor_id``.
        :return: The freshly-read predecessor and successor, and the decision
            standing over the pairing, if one does.
        :raises HTTPBadRequestException: If the body names the predecessor itself.
        :raises HTTPException: The ``missing_successor`` argument, when no row
            carries ``successor_id``.
        """
        if successor_id == predecessor.id:
            raise HTTPBadRequestException(
                f"{cls.Model.__name__} {predecessor.id} cannot be paired with itself."
            )
        await cls._lock_pairing(session, predecessor.id, successor_id)
        locked_predecessor = await cls._read_including_retired(session, predecessor.id)
        locked_successor = await cls._read_including_retired(session, successor_id)
        if locked_predecessor is None or locked_successor is None:
            raise missing_successor
        standing = await IdentityLinkDecisionManager.latest_for_pairing(
            session, cls.entity_name, locked_predecessor.id, locked_successor.id
        )
        return locked_predecessor, locked_successor, standing

    @classmethod
    async def _require_pairable(
        cls, session: AsyncSession, predecessor: Any, successor: Any
    ) -> None:
        """Refuse a pairing the candidate derivation would never surface.

        :param session: The asynchronous database session to use.
        :param predecessor: The older row of the pairing.
        :param successor: The newer row of the pairing.
        :raises HTTPBadRequestException: If both rows already hold one identifier.
        :raises HTTPConflictException: If the pairing fails the structural
            predicate for any other reason.
        """
        if predecessor.external_id == successor.external_id:
            raise HTTPBadRequestException(
                f"{cls.Model.__name__} {predecessor.id} and {successor.id} already "
                "hold the same upstream identifier."
            )
        candidate_predecessor = aliased(cls.Model, name="predecessor")
        candidate_successor = aliased(cls.Model, name="successor")
        query = (
            select(literal(1))
            .select_from(candidate_predecessor)
            .join(
                candidate_successor,
                onclause=and_(
                    *cls._structural_pairing_clauses(
                        candidate_predecessor, candidate_successor
                    )
                ),
            )
            .where(
                col(candidate_predecessor.id) == predecessor.id,
                col(candidate_successor.id) == successor.id,
            )
        )
        if (await cls._exec(session, query)).first() is None:
            raise HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} and {successor.id} are not a "
                "candidate pairing."
            )

    @classmethod
    def _alias_record(
        cls,
        *,
        entity_id: int,
        source: SourceEnum,
        external_id: str,
        valid_from: datetime,
        valid_to: datetime | None,
        linkage_method: LinkageMethodEnum,
        principal: str,
    ) -> ExternalIdentityAlias:
        """Build one external-identity binding record.

        :param entity_id: The row the identifier resolves to over the interval.
        :param source: The upstream system the identifier belongs to.
        :param external_id: The upstream identifier being bound.
        :param valid_from: When the binding takes effect.
        :param valid_to: When it stops applying, or None while it stands.
        :param linkage_method: How the binding came to be recorded.
        :param principal: The caller recording it.
        :return: The unsaved record.
        """
        return ExternalIdentityAlias(
            entity_type=cls.entity_name,
            entity_id=entity_id,
            source=source,
            external_id=external_id,
            valid_from=valid_from,
            valid_to=valid_to,
            linkage_method=linkage_method,
            principal=principal,
        )

    @classmethod
    async def _commit_identity_change(
        cls,
        session: AsyncSession,
        statements: Sequence[Update],
        rows: Sequence[SQLModel],
    ) -> None:
        """Run an identity operation's statements and appends in one transaction.

        ``retire`` and ``revive`` each end in their own commit, so composing the
        published methods would publish the pair mid-transfer and let the unique
        indexes reject it. The statements compose instead, exactly as those two
        compose their own cascades.

        :param session: The asynchronous database session to use.
        :param statements: The UPDATE statements, in the order they must run.
        :param rows: The alias and decision records to append.
        :raises HTTPConflictException: If an active row already holds a unique key
            the operation would reclaim.
        """
        try:
            for statement in statements:
                await cls._exec(  # call-shape-dup-ok: _exec already names the intent
                    session, statement
                )
            session.add_all(rows)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPConflictException(
                f"{cls.Model.__name__} identity cannot be transferred: an active "
                "entity already holds a unique key the transfer would reclaim."
            ) from None

    @classmethod
    async def decide_identity_link(
        cls,
        session: AsyncSession,
        predecessor: RetirableSQLModel,
        decision: IdentityLinkDecisionWrite,
        *,
        principal: str,
    ) -> None:
        """Apply one operator decision to a candidate pairing.

        The three operations share a route because each is exactly one append to
        the decision log; splitting them would triple the role-gate matrix and
        the OpenAPI surface for no difference in contract.

        :param session: The asynchronous database session to use.
        :param predecessor: The row addressed by the path.
        :param decision: What the operator decided, and about which successor.
        :param principal: The caller recording the decision.
        :raises HTTPBadRequestException: If the body names the predecessor itself,
            or both rows already hold one identifier.
        :raises HTTPNotFoundException: If a confirmation or rejection names a
            successor that does not exist.
        :raises HTTPConflictException: If the decision does not apply to the
            pairing as it currently stands.
        """
        operations = {
            IdentityLinkDecisionEnum.CONFIRMED: cls.confirm_identity_link,
            IdentityLinkDecisionEnum.REJECTED: cls.reject_identity_link,
            IdentityLinkDecisionEnum.UNLINKED: cls.unlink_identity,
        }
        await operations[decision.decision](
            session, predecessor, decision.successor_id, principal=principal
        )

    @classmethod
    async def confirm_identity_link(
        cls,
        session: AsyncSession,
        predecessor: RetirableSQLModel,
        successor_id: int,
        *,
        principal: str,
    ) -> None:
        """Transfer the successor's upstream identity onto the predecessor.

        The order is what keeps every intermediate state legal under the unique
        index carrying ``retirement_key``: the successor is retired first, which
        vacates the identifier, and only then does the predecessor take it.

        A rejection does not block this. A pairing rejected by mistake stays
        confirmable by explicit id, which is what keeps a mistaken rejection
        correctable rather than permanent.

        :param session: The asynchronous database session to use.
        :param predecessor: The row addressed by the path, which survives.
        :param successor_id: The row named by the request body, which is retired.
        :param principal: The caller recording the confirmation.
        :raises HTTPBadRequestException: If the body names the predecessor itself,
            or both rows already hold one identifier.
        :raises HTTPNotFoundException: If no row carries ``successor_id``.
        :raises HTTPConflictException: If the pairing is not a candidate, is
            already confirmed, or the transfer would collide with an active row.
        """
        predecessor, successor, standing = await cls._open_operation(
            session,
            predecessor,
            successor_id,
            missing_successor=HTTPNotFoundException(
                detail=f"{cls.Model.__name__} {successor_id} not found."
            ),
        )
        if standing is not None and (
            standing.decision is IdentityLinkDecisionEnum.CONFIRMED
        ):
            raise HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} is already linked to "
                f"{successor.id}."
            )
        await cls._require_pairable(session, predecessor, successor)
        source = await cls._identity_source(session, predecessor)
        superseded_external_id = predecessor.external_id
        predecessor_retired_at = predecessor.retired_at
        superseded_since = await ExternalIdentityAliasManager.binding_started_at(
            session, cls.entity_name, predecessor.id, superseded_external_id
        )
        transferred_since = await ExternalIdentityAliasManager.binding_started_at(
            session, cls.entity_name, successor.id, successor.external_id
        )
        confirmed_at = utc_now()
        statements = [
            *cls._retirement_statements(successor.id, confirmed_at),
            *cls._revival_statements(predecessor.id),
            update(cls.Model)
            .where(col(cls.Model.id) == predecessor.id)
            .values(external_id=successor.external_id),
        ]
        rows = [
            cls._alias_record(
                entity_id=predecessor.id,
                source=source,
                external_id=superseded_external_id,
                valid_from=superseded_since or predecessor.created_at,
                valid_to=confirmed_at,
                linkage_method=LinkageMethodEnum.OPERATOR_CONFIRMATION,
                principal=principal,
            ),
            cls._alias_record(
                entity_id=successor.id,
                source=source,
                external_id=successor.external_id,
                valid_from=transferred_since or successor.created_at,
                valid_to=confirmed_at,
                linkage_method=LinkageMethodEnum.OPERATOR_CONFIRMATION,
                principal=principal,
            ),
            cls._alias_record(
                entity_id=predecessor.id,
                source=source,
                external_id=successor.external_id,
                valid_from=confirmed_at,
                valid_to=None,
                linkage_method=LinkageMethodEnum.OPERATOR_CONFIRMATION,
                principal=principal,
            ),
            IdentityLinkDecision(
                entity_type=cls.entity_name,
                predecessor_id=predecessor.id,
                successor_id=successor.id,
                decision=IdentityLinkDecisionEnum.CONFIRMED,
                principal=principal,
                predecessor_external_id=superseded_external_id,
                predecessor_retired_at=predecessor_retired_at,
            ),
        ]
        await cls._commit_identity_change(session, statements, rows)

    @classmethod
    async def reject_identity_link(
        cls,
        session: AsyncSession,
        predecessor: RetirableSQLModel,
        successor_id: int,
        *,
        principal: str,
    ) -> None:
        """Record that a candidate pairing names two machines rather than one.

        Rejection suppresses the suggestion, not the operation.

        :param session: The asynchronous database session to use.
        :param predecessor: The row addressed by the path.
        :param successor_id: The row named by the request body.
        :param principal: The caller recording the rejection.
        :raises HTTPBadRequestException: If the body names the predecessor itself,
            or both rows already hold one identifier.
        :raises HTTPNotFoundException: If no row carries ``successor_id``.
        :raises HTTPConflictException: If the pairing is not a candidate, or a
            decision already stands over it.
        """
        predecessor, successor, standing = await cls._open_operation(
            session,
            predecessor,
            successor_id,
            missing_successor=HTTPNotFoundException(
                detail=f"{cls.Model.__name__} {successor_id} not found."
            ),
        )
        if standing is not None and standing.decision in SUPPRESSING_DECISIONS:
            raise HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} already carries a "
                f"standing decision over {successor.id}."
            )
        await cls._require_pairable(session, predecessor, successor)
        session.add(
            IdentityLinkDecision(
                entity_type=cls.entity_name,
                predecessor_id=predecessor.id,
                successor_id=successor.id,
                decision=IdentityLinkDecisionEnum.REJECTED,
                principal=principal,
            )
        )
        await session.commit()

    @classmethod
    async def unlink_identity(
        cls,
        session: AsyncSession,
        predecessor: RetirableSQLModel,
        successor_id: int,
        *,
        principal: str,
    ) -> None:
        """Reverse a standing confirmation, restoring both rows' own identities.

        Read from the decision log rather than from the structural predicate: a
        confirmed pairing no longer satisfies that predicate, both rows now
        relating through the transferred identifier. The stored
        ``predecessor_retired_at`` restores a tombstoned predecessor to the
        tombstone it was, retention age included, rather than to an
        approximation of it.

        Reversal is last-in-first-out. A predecessor may absorb several
        successors, each confirmed on its own, and every confirmation records the
        identifier the predecessor held before it. Those records only compose
        back in reverse: reversing an older link first would restore an
        identifier a later confirmation has since superseded, and hand the older
        successor back an identifier that is no longer the one it lost. So a link
        is reversible only while the predecessor still holds what it transferred.

        :param session: The asynchronous database session to use.
        :param predecessor: The row addressed by the path.
        :param successor_id: The row named by the request body.
        :param principal: The caller recording the reversal.
        :raises HTTPBadRequestException: If the body names the predecessor itself.
        :raises HTTPConflictException: If no confirmation stands over the pairing,
            the successor row is gone, a later confirmation supersedes this one,
            the successor cannot be revived under a retired ancestor, or the
            reversal would collide with an active row.
        """
        predecessor, successor, standing = await cls._open_operation(
            session,
            predecessor,
            successor_id,
            missing_successor=HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} cannot be unlinked: row "
                f"{successor_id} no longer exists."
            ),
        )
        if (
            standing is None
            or standing.decision is not IdentityLinkDecisionEnum.CONFIRMED
            or standing.predecessor_external_id is None
        ):
            raise HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} is not linked to "
                f"{successor.id}."
            )
        if predecessor.external_id != successor.external_id:
            raise HTTPConflictException(
                f"{cls.Model.__name__} {predecessor.id} no longer holds the "
                f"identifier its link to {successor.id} transferred. Reverse the "
                f"later link first."
            )
        await cls._require_revivable(session, successor)
        source = await cls._identity_source(session, predecessor)
        transferred_external_id = predecessor.external_id
        unlinked_at = utc_now()
        statements = [
            update(cls.Model)
            .where(col(cls.Model.id) == predecessor.id)
            .values(external_id=standing.predecessor_external_id),
            *cls._revival_statements(successor.id),
        ]
        if standing.predecessor_retired_at is not None:
            statements.extend(
                cls._retirement_statements(
                    predecessor.id, standing.predecessor_retired_at
                )
            )
        rows = [
            cls._alias_record(
                entity_id=predecessor.id,
                source=source,
                external_id=transferred_external_id,
                valid_from=standing.created_at,
                valid_to=unlinked_at,
                linkage_method=LinkageMethodEnum.OPERATOR_UNLINK,
                principal=principal,
            ),
            cls._alias_record(
                entity_id=predecessor.id,
                source=source,
                external_id=standing.predecessor_external_id,
                valid_from=unlinked_at,
                valid_to=None,
                linkage_method=LinkageMethodEnum.OPERATOR_UNLINK,
                principal=principal,
            ),
            cls._alias_record(
                entity_id=successor.id,
                source=source,
                external_id=transferred_external_id,
                valid_from=unlinked_at,
                valid_to=None,
                linkage_method=LinkageMethodEnum.OPERATOR_UNLINK,
                principal=principal,
            ),
            IdentityLinkDecision(
                entity_type=cls.entity_name,
                predecessor_id=predecessor.id,
                successor_id=successor.id,
                decision=IdentityLinkDecisionEnum.UNLINKED,
                principal=principal,
            ),
        ]
        await cls._commit_identity_change(session, statements, rows)


class NodeManager(AliasableManagerMixin, SyncHealthManagerMixin, BaseSQLModelManager):
    """Manage Node operations, including retrieval, listing, and retirement.

    :ivar Model: The SQLModel class this manager is responsible for (``Node``).
    :cvar retirement_subtree: The descendants retiring a node cascades into.
    :cvar entity_name: The entity type this manager's identity records carry.
    :cvar identity_match_signals: The extra signals a candidate reports when they
        agree.
    :cvar identity_select_related: The relationships a candidate's nodes are
        hydrated with.
    :cvar list_query_spec: The list-query spec declaring this entity's sortable
        allowlist, searchable columns, and default sort.
    """

    Model = Node
    retirement_subtree = (
        (Service, "node_id"),
        (Schema, "service_id"),
        (Table, "schema_id"),
    )
    entity_name = RetirableEntityName.NODE
    identity_match_signals = ("address",)
    identity_select_related = (Node.services,)
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(Node.name),
            "created_at": col(Node.created_at),
        },
        default_sort="name",
        tie_breaker=col(Node.id),
        searchable=[col(Node.name)],
    )

    @classmethod
    def _structural_pairing_clauses(
        cls, predecessor: Any, successor: Any
    ) -> list[ColumnExpressionArgument[bool]]:
        """Narrow the shared clauses to one source, and to an active successor.

        ``source`` joins even though :class:`SourceEnum` has a single member: it
        is part of the uniqueness index a confirmation has to stay legal under,
        and a one-member enum is not a licence to drop the predicate.

        :param predecessor: The aliased model standing for the older node.
        :param successor: The aliased model standing for the newer node.
        :return: The clauses, to be ANDed together.
        """
        return [
            *super()._structural_pairing_clauses(predecessor, successor),
            col(predecessor.source) == col(successor.source),
            col(successor.retired_at).is_(None),
        ]

    @classmethod
    async def _identity_source(
        cls,
        _session: AsyncSession,
        entity: Any,
        /,
    ) -> SourceEnum:
        """Return the node's own source, which needs no lookup.

        :param _session: Unused; a node carries its own provenance.
        :param entity: The node whose provenance is wanted.
        :return: The source.
        """
        return entity.source


class ServiceManager(
    AliasableManagerMixin, SyncHealthManagerMixin, BaseSQLModelChildManager
):
    """Manage Service operations, including retrieval, listing, and retirement.

    :ivar Model: The SQLModel class this manager is responsible for (``Service``).
    :ivar ParentManager: The manager class responsible for handling the parent model
        (``NodeManager``).
    :ivar connected_by: The field name that connects the child model to the parent
        model (``node_id``).
    :cvar retirement_subtree: The descendants retiring a service cascades into.
    :cvar entity_name: The entity type this manager's identity records carry.
    :cvar identity_match_signals: The extra signals a candidate reports when they
        agree.
    :cvar identity_select_related: The relationships a candidate's services are
        hydrated with.
    :cvar list_query_spec: The list-query spec declaring this entity's sortable
        allowlist, searchable columns, and default sort.
    """

    Model = Service
    ParentManager = NodeManager
    connected_by = "node_id"
    retirement_subtree = ((Schema, "service_id"), (Table, "schema_id"))
    entity_name = RetirableEntityName.SERVICE
    identity_match_signals = ("port",)
    identity_select_related = (Service.schemas, Service.node)
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(Service.name),
            "created_at": col(Service.created_at),
        },
        default_sort="name",
        tie_breaker=col(Service.id),
        searchable=[col(Service.name)],
    )

    @classmethod
    def _structural_pairing_clauses(
        cls, predecessor: Any, successor: Any
    ) -> list[ColumnExpressionArgument[bool]]:
        """Add the clauses a service pairing needs beyond the shared ones.

        A node confirmation retires the successor node *with its subtree*, so the
        successor's services are retired too and must still count as successors —
        which is what makes "confirming a node pairing surfaces its services as
        candidates in turn" true. The final clause is the confirmability
        precondition: never surface a pairing whose confirmation the uniqueness
        index would reject. It matters concretely, because the first sync tick
        after a node confirmation recreates the successor's service on the
        surviving node, and a cross-node candidate for that service would then
        collide with it.

        :param predecessor: The aliased model standing for the older service.
        :param successor: The aliased model standing for the newer service.
        :return: The clauses, to be ANDed together.
        """
        holder = aliased(cls.Model)
        return [
            *super()._structural_pairing_clauses(predecessor, successor),
            or_(
                col(predecessor.node_id) == col(successor.node_id),
                IdentityLinkDecisionManager.confirmed_link_exists(
                    RetirableEntityName.NODE,
                    predecessor_id=col(predecessor.node_id),
                    successor_id=col(successor.node_id),
                ),
            ),
            or_(
                col(successor.retired_at).is_(None),
                IdentityLinkDecisionManager.confirmed_link_exists(
                    RetirableEntityName.NODE, successor_id=col(successor.node_id)
                ),
            ),
            ~select(literal(1))
            .where(
                col(holder.external_id) == col(successor.external_id),
                col(holder.node_id) == col(predecessor.node_id),
                col(holder.retired_at).is_(None),
                col(holder.id) != col(successor.id),
            )
            .exists(),
        ]

    @classmethod
    def _identity_link_pin(cls) -> ColumnElement[bool]:
        """Cover the services a standing node link retired, beyond the shared pin.

        Confirming a node pairing retires the successor node *with its subtree*,
        so its services become tombstones carrying no service decision of their
        own. The shared pin does not reach them, and collection walks services
        before nodes, so the very rows
        :meth:`_structural_pairing_clauses` keeps surfacing as candidates would
        age out from under the standing node link — taking the reversal's
        subtree with them.

        :return: The ``EXISTS`` predicate.
        """
        return or_(
            super()._identity_link_pin(),
            IdentityLinkDecisionManager.confirmed_link_exists(
                RetirableEntityName.NODE, successor_id=col(cls.Model.node_id)
            ),
        )

    @classmethod
    async def _identity_source(
        cls, session: AsyncSession, entity: Any, /
    ) -> SourceEnum:
        """Return the source the service inherits through its node.

        ``Service`` carries no ``source`` column of its own — provenance is the
        node's — but the alias record stores one, so it is read from there.

        :param session: The asynchronous database session to use.
        :param entity: The service whose provenance is wanted.
        :return: The source.
        """
        result = await cls._exec(
            session,
            select(col(Node.source)).where(col(Node.id) == entity.node_id),
        )
        return result.one()

    @classmethod
    async def _require_revivable(
        cls,
        session: AsyncSession,
        successor: Any,
    ) -> None:
        """Refuse a reversal while the successor's node is retired by its own link.

        Reviving a service revives its node first, so that a live row never sits
        under a tombstone. A node retired by a standing confirmation cannot come
        back that way: the surviving node holds the identifier it would reclaim,
        and only one active row may carry it. The node link is what has to be
        reversed first, so say that rather than surfacing the uniqueness
        collision it would otherwise become.

        :param session: The asynchronous database session to use.
        :param successor: The service a reversal would revive.
        :raises HTTPConflictException: If the successor's node is retired by a
            confirmation of its own.
        """
        result = await cls._exec(
            session,
            select(col(Node.retired_at)).where(col(Node.id) == successor.node_id),
        )
        if result.one() is None:
            return
        if await IdentityLinkDecisionManager.confirms_successor(
            session, RetirableEntityName.NODE, successor.node_id
        ):
            raise HTTPConflictException(
                f"Service {successor.id} cannot be revived while node "
                f"{successor.node_id} is retired by a standing identity link. "
                f"Reverse the node link first."
            )


class SchemaManager(
    RetirableManagerMixin, SyncHealthManagerMixin, BaseSQLModelChildManager
):
    """Manage Schema operations, including retrieval, listing, and retirement.

    :ivar Model: The SQLModel class this manager is responsible for (`Schema`).
    :ivar ParentManager: The manager class responsible for handling the parent model
        (`ServiceManager`).
    :ivar connected_by: The field name that connects the child model to the parent
        model (`service_id`).
    :cvar retirement_subtree: The descendants retiring a schema cascades into.
    :cvar list_query_spec: The list-query spec declaring this entity's sortable
        allowlist, searchable columns, and default sort.
    """

    Model = Schema
    ParentManager = ServiceManager
    connected_by = "service_id"
    retirement_subtree = ((Table, "schema_id"),)
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(Schema.name),
            "created_at": col(Schema.created_at),
            "service_id": col(Schema.service_id),
        },
        default_sort="name",
        tie_breaker=col(Schema.id),
        searchable=[col(Schema.name)],
    )


class TableManager(
    RetirableManagerMixin, SyncHealthManagerMixin, BaseSQLModelChildManager
):
    """Manage Table operations, including retrieval, listing, and retirement.

    :ivar Model: The SQLModel class this manager is responsible for (`Table`).
    :ivar ParentManager: The manager class responsible for handling the parent model
        (`SchemaManager`).
    :ivar connected_by: The field name that connects the child model to the parent
        model (`schema_id`).
    :cvar list_query_spec: The list-query spec declaring this entity's sortable
        allowlist, searchable columns, and default sort.
    """

    Model = Table
    ParentManager = SchemaManager
    connected_by = "schema_id"
    list_query_spec = ListQuerySpec(
        sortable={
            "name": col(Table.name),
            "created_at": col(Table.created_at),
            "schema_id": col(Table.schema_id),
        },
        default_sort="name",
        tie_breaker=col(Table.id),
        searchable=[col(Table.name)],
    )


class RetiredInclusiveNodeManager(NodeManager):
    """Read nodes without hiding the retired ones.

    :cvar include_retired: Always True, which is the whole point of the class.
    """

    include_retired = True


class RetiredInclusiveServiceManager(ServiceManager):
    """Read services without hiding the retired ones.

    :cvar include_retired: Always True, which is the whole point of the class.
    """

    include_retired = True


class RetiredInclusiveSchemaManager(SchemaManager):
    """Read schemas without hiding the retired ones.

    :cvar include_retired: Always True, which is the whole point of the class.
    """

    include_retired = True


class RetiredInclusiveTableManager(TableManager):
    """Read tables without hiding the retired ones.

    :cvar include_retired: Always True, which is the whole point of the class.
    """

    include_retired = True


#: The order collection walks the retirable entities in, deepest first, paired
#: with the entity name PMM Extensions addresses each type by. Deleting a descendant before
#: its ancestor mirrors :meth:`RetirableManagerMixin._retirement_statements`, so
#: an interrupted run can only ever leave deleted descendants under a surviving
#: ancestor — never an orphan, and never a live row beneath a deleted ancestor.
COLLECTION_ORDER: tuple[
    tuple[RetirableEntityName, type[RetirableManagerMixin]], ...
] = (
    (RetirableEntityName.TABLE, RetiredInclusiveTableManager),
    (RetirableEntityName.SCHEMA, RetiredInclusiveSchemaManager),
    (RetirableEntityName.SERVICE, RetiredInclusiveServiceManager),
    (RetirableEntityName.NODE, RetiredInclusiveNodeManager),
)


class HostSystemObservationManager(BaseSQLModelChildManager):
    """Manage host system observation operations.

    :ivar Model: The SQLModel class this manager is responsible for
        (`HostSystemObservation`).
    :ivar ParentManager: The manager class responsible for handling the parent model
        (`NodeManager`).
    :ivar connected_by: The field name that connects the child model to the parent
        model (`node_id`).
    """

    Model = HostSystemObservation
    ParentManager = NodeManager
    connected_by = "node_id"


class ServiceSystemObservationManager(BaseSQLModelChildManager):
    """Manage service system observation operations.

    :ivar Model: The SQLModel class this manager is responsible for
        (`ServiceSystemObservation`).
    :ivar ParentManager: The manager class responsible for handling the parent model
        (`ServiceManager`).
    :ivar connected_by: The field name that connects the child model to the parent
        model (`service_id`).
    """

    Model = ServiceSystemObservation
    ParentManager = ServiceManager
    connected_by = "service_id"
