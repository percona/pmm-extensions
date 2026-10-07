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

"""Define reads and writes for the Operations Inventory tables.

The entity upserts here are where the freshness lifecycle ``ObservedEntity``
describes actually lives, and they are written attribute by attribute on purpose.
Nothing in these tables is user-writable *yet*; the moment one field is — an
assigned name, a label, a suppression flag — a blanket "update every column" upsert
wipes it on the next sweep, and the test that catches that has to exist before the
field does, not after.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import literal
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.db.crud import BaseSQLModelManager
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.extensions.apps.om_inventory.models import (
    ObservedEntity,
    OmHost,
    OmService,
    ProbeRun,
    ProbeRunStatus,
)


class ProbeRunManager(BaseSQLModelManager):
    """Manage :class:`ProbeRun` CRUD operations.

    :cvar Model: The SQLModel class this manager is responsible for.
    """

    Model = ProbeRun


class OmHostManager(BaseSQLModelManager):
    """Manage :class:`OmHost` CRUD operations, including paginated estate reads.

    Neither ``OmHost`` nor ``OmService`` extends ``BaseSQLModel``, so the base
    manager's ``created_at``-descending fallback ordering does not apply to them —
    every call here has to pass its own ``order_by``.

    :cvar Model: The SQLModel class this manager is responsible for.
    """

    Model = OmHost

    @staticmethod
    # pagination-ok: SQLAlchemy WHERE clauses, at most one per optional filter,
    # consumed by the caller's own paginated query rather than returned to a client.
    def estate_clauses(*, has_service: bool | None, failing: bool | None) -> list[Any]:
        """Return the ``WHERE`` clauses for the estate list's optional filters.

        Not ``executor``: that filter reads ``observed.executor``, a JSON
        sub-document not worth a dialect-specific path expression for SQLite and
        PostgreSQL each, so it stays a post-query pass over already-fetched rows
        in the route rather than a clause here.

        :param has_service: Filter on whether a MongoDB service is registered,
            or ``None`` for no filter.
        :param failing: Filter on whether the host is currently failing, or
            ``None`` for no filter.
        :return: The clauses, empty when both filters are unset.
        """
        clauses: list[Any] = []
        if has_service is not None:
            exists_clause = has_service_clause()
            clauses.append(exists_clause if has_service else ~exists_clause)
        if failing is not None:
            clauses.append(
                col(OmHost.failing_since).is_not(None)
                if failing
                else col(OmHost.failing_since).is_(None)
            )
        return clauses


class OmServiceManager(BaseSQLModelManager):
    """Manage :class:`OmService` CRUD operations, including paginated estate reads.

    :cvar Model: The SQLModel class this manager is responsible for.
    """

    Model = OmService


def has_service_clause() -> Any:
    """Return the correlated ``EXISTS`` clause for "this host has a service row".

    A plain FK-existence check, not a JSON one — portable across every dialect OM
    runs on, unlike the ``executor`` filter this deliberately does not join:
    :func:`~app.extensions.apps.om_inventory.api_routes._executor_usable` reads
    ``observed.executor``, and pushing *that* into SQL would mean a dialect-specific
    JSON path expression for SQLite and PostgreSQL each.

    :return: A boolean SQL expression, true for a host with at least one service row.
    """
    return (
        select(literal(1)).where(col(OmService.node_id) == col(OmHost.node_id)).exists()
    )


def _apply_attempt(
    entity: ObservedEntity,
    *,
    observed: dict[str, Any] | None,
    error: str | None,
    run_id: UUID | None,
) -> None:
    """Fold one attempt's outcome into an entity's freshness columns.

    Shared by both tables so they cannot drift apart, and the four rules it enforces
    are each cheap here and expensive to discover later:

    * ``failing_since`` is only set when it is unset. Overwriting it on every failure
      turns "since" into "most recent failure", so the duration is always about one
      schedule interval and the column stops being worth reading.
    * A failure does **not** touch ``observed``. The last known good document stays,
      with its own ``collected_at``, because what a host was running when it was last
      reachable is exactly what is wanted while it is not.
    * ``last_attempt_at`` moves on every attempt, ``last_success_at`` only on success.
      The gap between them is the answer to "how stale is this".
    * The caller decides what counts as an attempt. An entity a run did not target
      must never reach this function, or a single-host refresh marks the rest of the
      estate failed.

    :param entity: The row to update, already loaded.
    :param observed: The collected document on success; ``None`` on failure.
    :param error: The failure detail; ``None`` on success.
    :param run_id: The run this attempt belongs to.
    """
    now = utc_now()
    entity.last_attempt_at = now
    entity.last_run_id = run_id
    entity.updated_at = now

    if observed is not None:
        entity.observed = observed
        entity.last_success_at = now
        entity.failing_since = None
        entity.consecutive_failures = 0
        entity.last_error = None
        return

    entity.failing_since = entity.failing_since or now
    entity.consecutive_failures += 1
    entity.last_error = error


async def upsert_host(
    session: AsyncSession,
    *,
    node_id: str,
    name: str,
    address: str | None,
    executor_host: str | None,
    observed: dict[str, Any] | None = None,
    executor: dict[str, Any] | None = None,
    error: str | None = None,
    run_id: UUID | None = None,
    attempted: bool = True,
) -> OmHost:
    """Record one host, and optionally one attempt against it.

    Written whether or not any MongoDB was found on the host: that is what makes
    "which hosts have no database" a query rather than an absence, and it is the only
    way a host that has never run one appears at all.

    ``attempted`` is the difference between *seeing* an entity and *probing* it, and
    conflating the two is how a sweep starts lying. A host with no executor is seen
    every sweep and probed by none of them: its identity and its ``executor_host``
    are refreshed, and its attempt columns are left exactly where they were, so
    "unreachable for three days" stays readable and does not silently become
    "unreachable since the last sweep".

    :param session: The database session.
    :param node_id: PMM's node id.
    :param name: The node's registered name.
    :param address: The node's registered address.
    :param executor_host: The Nomad client serving it, or ``None``.
    :param observed: The collected document, or ``None`` when the attempt failed.
    :param executor: What the executor backend says about this host. Written on every
        sweep regardless of ``attempted``, because PMM Extensions knows it without running
        anything — and the hosts it cannot run anything on are exactly the ones whose
        document would otherwise be empty with no explanation for it.
    :param error: The failure detail.
    :param run_id: The run this attempt belongs to.
    :param attempted: Whether this run actually probed the host.
    :return: The stored row.
    """
    host = await OmHostManager.first(session, node_id=node_id)
    if host is None:
        host = OmHost(node_id=node_id, name=name)
        # Staged, not OmHostManager.create(): every host and service in a sweep
        # commits together in service.py's persist_estate, and the manager's
        # create()/save() would each commit on their own.
        session.add(host)

    # Identity attributes are owned by inventory and refreshed every time the host is
    # seen: a node that was renamed or readdressed should not keep reading as the old
    # one, and whether it currently has an executor is exactly the kind of thing a
    # reader needs even when nothing was run on it.
    host.name = name
    host.address = address
    host.executor_host = executor_host
    host.updated_at = utc_now()

    if attempted:
        _apply_attempt(host, observed=observed, error=error, run_id=run_id)

    # After the attempt, not before: a successful probe replaces ``observed``
    # wholesale, which would drop these on exactly the hosts that did answer.
    if executor is not None:
        host.observed = {**(host.observed or {}), "executor": executor}
    return host


async def upsert_service(
    session: AsyncSession,
    *,
    service_id: str,
    node_id: str,
    name: str | None,
    port: int | None,
    role: str | None,
    observed: dict[str, Any] | None = None,
    error: str | None = None,
    process_facts: dict[str, Any] | None = None,
    run_id: UUID | None = None,
    attempted: bool = True,
) -> OmService:
    """Record one MongoDB service PMM has registered, and optionally one attempt.

    Every service PMM knows gets a row, including one whose host has no executor and
    could not be probed. Omitting those would report a healthier estate than exists —
    the PoC measured 17 of 18 services unreachable in a single run, and a listing that
    showed one service would have been worse than useless.

    ``role`` is only written when the probe determined one — a failed attempt must
    not blank out what the last good one saw, for the same reason it must not blank
    out ``observed``.

    What a failed attempt did read off the host is not discarded with the rest: a
    stopped mongod must stop reading as running even while its database facts stay
    those of the last scan that could query it. ``collected_at`` stays the time of
    that scan, the one the database facts are from.

    :param session: The database session.
    :param service_id: PMM's service id.
    :param node_id: The host it runs on; its ``om.om_host`` row must exist.
    :param name: The service name.
    :param port: The port it listens on.
    :param role: The observed role, or ``None`` when this attempt did not see one.
    :param observed: The collected document, or ``None`` when the attempt failed.
    :param error: The failure detail.
    :param process_facts: What a failed attempt still saw of the binary and the
        process, merged into the stored ``observed``. A ``None`` value removes that
        key. Ignored while nothing is stored, since there are no database facts to
        keep.
    :param run_id: The run this attempt belongs to.
    :param attempted: Whether this run actually probed the service.
    :return: The stored row.
    """
    service = await OmServiceManager.first(session, service_id=service_id)
    if service is None:
        service = OmService(service_id=service_id, node_id=node_id)
        # Staged, not OmServiceManager.create(): see the matching comment in
        # upsert_host.
        session.add(service)

    service.node_id = node_id
    service.name = name
    service.port = port
    service.updated_at = utc_now()
    if role is not None:
        service.role = role

    if attempted:
        _apply_attempt(service, observed=observed, error=error, run_id=run_id)

    if process_facts is not None and service.observed:
        merged = {**service.observed, **process_facts}
        service.observed = {
            key: value for key, value in merged.items() if value is not None
        }
    return service


async def get_host(session: AsyncSession, node_id: str) -> OmHost | None:
    """Return one host by PMM's node id.

    :param session: The database session.
    :param node_id: PMM's node id.
    :return: The host, or ``None``.
    """
    return await OmHostManager.first(session, node_id=node_id)


async def get_service(session: AsyncSession, service_id: str) -> OmService | None:
    """Return one service by PMM's service id.

    :param session: The database session.
    :param service_id: PMM's service id.
    :return: The service, or ``None``.
    """
    return await OmServiceManager.first(session, service_id=service_id)


async def delete_host(session: AsyncSession, host: OmHost) -> None:
    """Delete one host row and the services on it.

    The services are deleted **here**, not left to ``ON DELETE CASCADE``, even though
    the constraint says cascade. SQLite enforces no foreign key unless
    ``PRAGMA foreign_keys=ON`` is set per connection, PMM Extensions sets it nowhere, and SQLite
    is the shipped default in ``settings.yaml`` — so on a default deployment the
    cascade is decoration and deleting a host would leave service rows pointing at a
    host that no longer exists.

    The constraint stays as the backstop it is on PostgreSQL. What changes is that the
    *promise* — "forgetting a host forgets what was on it" — no longer depends on
    which database someone configured.

    :param session: The database session.
    :param host: The row to delete.
    """
    for service in await list_services(session, node_id=host.node_id):
        # Staged, not OmServiceManager.delete(): it commits per call, which
        # would split "forget a host" into N+1 transactions instead of one.
        await session.delete(service)
    await OmHostManager.delete(session, host)


async def delete_service(session: AsyncSession, service: OmService) -> None:
    """Delete one service row, leaving its host alone.

    :param session: The database session.
    :param service: The row to delete.
    """
    await OmServiceManager.delete(session, service)


async def list_hosts(session: AsyncSession) -> list[OmHost]:
    """Return every host row, by name.

    :param session: The database session.
    :return: The hosts.
    """
    return await OmHostManager.list(session, order_by=[col(OmHost.name)])


async def list_services(
    session: AsyncSession, node_id: str | None = None
) -> list[OmService]:
    """Return service rows, optionally for one host.

    :param session: The database session.
    :param node_id: Restrict to this host when given.
    :return: The services.
    """
    return await OmServiceManager.list(
        session, order_by=[col(OmService.name)], node_id=node_id
    )


async def recent_runs(
    session: AsyncSession,
    limit: int = 20,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[ProbeRun]:
    """Return the most recent runs, newest first.

    ``since`` / ``until`` bound ``started_at`` *before* ``limit`` is applied, so a
    week window of twenty-one runs is twenty-one runs rather than "the twenty newest
    overall, then those that happen to fall in the week".

    :param session: The database session.
    :param limit: How many to return.
    :param since: Inclusive lower bound on ``started_at``.
    :param until: Inclusive upper bound on ``started_at``.
    :return: The runs.
    """
    statement = select(ProbeRun).order_by(col(ProbeRun.started_at).desc())
    if since is not None:
        statement = statement.where(col(ProbeRun.started_at) >= since)
    if until is not None:
        statement = statement.where(col(ProbeRun.started_at) <= until)
    result = await session.exec(statement.limit(limit))
    return list(result.all())


async def get_run(session: AsyncSession, run_id: UUID) -> ProbeRun | None:
    """Return one run.

    :param session: The database session.
    :param run_id: The run's id.
    :return: The run, or ``None``.
    """
    return await session.get(ProbeRun, run_id)


async def running_run(session: AsyncSession) -> ProbeRun | None:
    """Return the newest sweep in flight, if there is one.

    :param session: The database session.
    :return: The running run, or ``None``.
    """
    result = await session.exec(
        select(ProbeRun)
        .where(ProbeRun.status == ProbeRunStatus.RUNNING)
        .order_by(col(ProbeRun.started_at).desc())
        .limit(1)
    )
    return result.first()


async def running_runs(session: AsyncSession) -> list[ProbeRun]:
    """Return every sweep currently in flight, newest first.

    All of them, not the newest, because the conflict question is per host once a
    refresh can be scoped: two runs over different hosts are not in conflict, and
    answering that needs each one's scope.

    :param session: The database session.
    :return: The running runs.
    """
    result = await session.exec(
        select(ProbeRun)
        .where(ProbeRun.status == ProbeRunStatus.RUNNING)
        .order_by(col(ProbeRun.started_at).desc())
    )
    return list(result.all())


async def prune_runs(session: AsyncSession, keep: int) -> int:
    """Delete all but the newest ``keep`` runs, never one still in flight.

    ``ProbeRun`` is a receipt, not a copy of the estate — what the probe found
    lives on ``OmHost``/``OmService`` instead — but its ``nodes`` column still
    carries one record per host per sweep, so the table grows every run and
    needs bounding here rather than by an operator.

    ``RUNNING`` is excluded regardless of age. Retention is by ``started_at``, and a
    long sweep is by definition the oldest row while it runs: enough newer rows — a
    burst of scoped refreshes, or the ``SKIPPED`` rows a schedule collision leaves
    behind — and the pruner deletes a row out from under the worker that owns it,
    which then raises in ``finalise`` on a run that no longer exists. A row nothing
    has finished writing is not history yet.

    :param session: The database session.
    :param keep: How many runs to keep.
    :return: The number deleted.
    """
    survivors = (
        select(ProbeRun.id).order_by(col(ProbeRun.started_at).desc()).limit(keep)
    )
    result = await session.exec(
        delete(ProbeRun).where(  # type: ignore[call-overload]
            col(ProbeRun.id).not_in(survivors),
            col(ProbeRun.status) != ProbeRunStatus.RUNNING,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def conflicting_run(
    session: AsyncSession,
    node_ids: list[str] | None,
    *,
    exclude: UUID | None = None,
    stale_after: timedelta,
) -> ProbeRun | None:
    """Return the in-flight run that already holds these hosts, if any.

    Single-flight is judged **per host**, not globally: with a ten-minute schedule a
    global refusal would reject a one-host refresh exactly when someone wants one. A
    run with no scope covers everything, so it overlaps whatever is asked — including
    another full refresh.

    Lives here rather than in the API handler because **both** paths need it. The
    handler is the obvious caller, but the scheduled sweep enters through Celery and
    never touches it: with the check in the handler alone, beat would start a sweep on
    top of one already dispatching, both would enqueue the same job for the same host,
    and the Tasks layer would refuse the duplicate with a 409 recorded against a host
    that is perfectly healthy.

    A run older than ``stale_after`` is reaped rather than honoured, because a crashed
    worker leaves a row nothing else advances and it would otherwise wedge the app
    permanently.

    **The check is not atomic**, and where ``exclude`` is given it does not need to be.
    Creating the row and claiming the work are two statements, so two callers can both
    pass this check and both insert a ``RUNNING`` row — two overlapping ``POST /runs``,
    or beat entering ``run_probe`` beside one. Each worker then re-checks, finds the
    other, and *both* skip: the estate does not refresh at all, and both callers are
    already holding a ``202`` and a run id that will never sweep. Losing a race to
    nobody is worse than either outcome the race was about.

    So a run that is asking on its own behalf only yields to a run that
    :func:`_claimed_first` says was there first. The order is total and both sides
    compute it from the same rows, so exactly one of any pair proceeds without a lock.
    The endpoint's pre-creation check passes no ``exclude`` and is unaffected: an
    in-flight sweep still refuses a new request with a 409, which is the answer a
    caller can act on.

    :param session: The database session.
    :param node_ids: The hosts being asked for, or ``None`` / empty for the estate.
    :param exclude: A run to ignore — the caller's own, when it has already been
        created. Without it a task would find its own row and refuse itself.
    :param stale_after: How old an in-flight run may be before it is presumed dead.
    :return: The blocking run, or ``None``.
    """
    wanted = set(node_ids or [])
    own = await session.get(ProbeRun, exclude) if exclude is not None else None
    for in_flight in await running_runs(session):
        if exclude is not None and in_flight.id == exclude:
            continue
        age = utc_now() - make_datetime_utc(in_flight.started_at)
        if age >= stale_after:
            in_flight.status = ProbeRunStatus.FAILED
            in_flight.finished_at = utc_now()
            in_flight.error = "abandoned: no worker recorded a terminal status"
            await ProbeRunManager.save(session, in_flight)
            continue
        if not in_flight.scope or not wanted or (set(in_flight.scope) & wanted):
            if own is not None and _claimed_first(own, in_flight):
                # We were here first: the other run will find us and skip itself.
                continue
            return in_flight
    return None


def _claimed_first(one: ProbeRun, other: ProbeRun) -> bool:
    """Return whether ``one`` has the better claim on the hosts both want.

    By start time, and by id where two rows were created inside the same clock tick —
    which a shared-second timestamp makes likely rather than exotic. Any total order
    both sides agree on would do; what matters is that it is a property of the rows,
    so two workers reading them separately reach the same verdict and exactly one
    proceeds.

    :param one: The run asking.
    :param other: The in-flight run it overlaps.
    :return: ``True`` when ``one`` should proceed and ``other`` should stand down.
    """
    mine = make_datetime_utc(one.started_at)
    theirs = make_datetime_utc(other.started_at)
    if mine != theirs:
        return mine < theirs
    return str(one.id) < str(other.id)


def conflict_detail(blocking: ProbeRun, node_ids: list[str] | None) -> str:
    """Say which hosts are held, and by which run.

    Names them rather than saying "a sweep is already running", which was true of
    anything and useful for nothing once conflict became per-host.

    :param blocking: The run already holding them.
    :param node_ids: The hosts that were asked for.
    :return: The message.
    """
    if not blocking.scope:
        held = "the whole estate"
    else:
        overlap = sorted(set(blocking.scope) & set(node_ids or []))
        held = ", ".join(overlap) or "these hosts"
    return f"Probe run {blocking.id} is already refreshing {held}"
