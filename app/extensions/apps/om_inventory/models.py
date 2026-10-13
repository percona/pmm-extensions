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

"""Define the Operations Inventory persistence models.

Three tables, and the split between them is the design:

``om.om_host`` and ``om.om_service``
    What the estate *is*, one row per entity, upserted. A host row exists whether or
    not any MongoDB was found on it, which is what makes "which pmm-clients have no
    database" answerable at all — and it is not hypothetical: the sandbox carries
    hosts with a PMM client and nothing else beside arbiters running a mongod PMM has
    no service for, and PMM's inventory describes the two identically.

``om.om_inventory_run``
    What one sweep *did*. A receipt, not a copy of the estate: which entity was
    attempted, on which executor host, whether it answered, and any error. Keeping
    the collected attributes out of it is load-bearing — with ``RUN_RETENTION``
    runs kept, putting them here stores the same facts that many times over, and
    creates a second source of truth for them.

Each entity carries a few real columns for identity and freshness plus one JSONB
document of everything probed, so changing what is collected is a payload change
rather than a migration. The cost is that fleet-wide questions become JSONB queries;
promoting a field to a column later is easy and the reverse is not.

This module is loaded by Alembic through ``spec_from_file_location`` without running
the package ``__init__``, so it must not import sibling app modules. That is also why
the schema below is a bare string rather than
``app.extensions.apps.shared.om.config.OM_SCHEMA_SYMBOL``: the token has to be spelled here,
and ``sqlalchemy_celery_beat`` makes the same trade with ``celery_schema``.
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Index, JSON, Text, text
from sqlalchemy import Enum as EnumField
from sqlalchemy.dialects import postgresql
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel
from sqlmodel.sql.sqltypes import AutoString

from app.core.db.models import BaseUUIDSQLModel, DateTimeWithTimezone
from app.core.utils.date_time import utc_now
from app.core.utils.fields import UTCDatetime

#: The symbolic schema every table here declares. See the module docstring for why it
#: is spelled rather than imported.
OM_SCHEMA = "om_schema"

# Table names carry an ``om_`` prefix — ``om_host``, ``om_service``,
# ``om_inventory_run`` — on top of the ``om`` schema that already qualifies them,
# which looks redundant until the schema stops actually separating anything.
# ``om.om_service`` would be a different table from PMM Extensions inventory's bare ``service``
# even without the prefix wherever schemas are real: on PostgreSQL in production, and
# on SQLite once the root conftest gives each connection a real ``om`` schema via
# ``ATTACH``. But the real-PostgreSQL test lane translates *every* declared schema
# token — ``om_schema`` included — into the same single per-worker schema for
# teardown simplicity, which collapses ``om_schema.service`` onto
# ``None.service`` and makes them the same physical table. That is not hypothetical
# either: it produced a ``CREATE TABLE`` emitted twice for one name, the second call
# failing as a duplicate, the first time this app's tables were ever exercised against
# a real, non-SQLite database. The prefix survives being redundant under schemas
# because it is the one thing that holds even when schemas do not.


def _observed_document_type() -> Any:
    """Build the column type for an ``observed`` document.

    A fresh type instance per call. A single shared ``Column`` cannot be reused across
    two models — SQLAlchemy binds a Column to exactly one Table — which is why the
    freshness mixin below declares types rather than columns.

    :return: ``JSONB`` on PostgreSQL, plain ``JSON`` everywhere else: ``JSON`` is the
        correct default to carve a variant *out of*, since PostgreSQL is the odd one
        with a dedicated binary type.
    """
    return JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


class ProbeRunStatus(StrEnum):  # rst-literal-ok: "None answered" is the English word
    """Enumerate the states of one probe sweep.

    ``PARTIAL`` is the common steady state, not an alarm: the probe reaches only
    services whose node runs a healthy ``raw_exec`` executor, and an inventory row
    routinely outlives the executor that served it.

    :cvar RUNNING: The sweep is in flight.
    :cvar SUCCESS: Every service that resolved to a live executor answered.
    :cvar PARTIAL: Some answered, some did not.
    :cvar FAILED: None answered, or the sweep raised.
    :cvar SKIPPED: Refused before doing any work, because another sweep already held
        the hosts it would have covered or because ``ENABLED`` was off. Recorded rather than skipped silently: a
        scheduled sweep that quietly does nothing leaves a ten-minute gap in the
        history that reads exactly like the schedule having fired and found nothing.
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class NodeResolution(StrEnum):
    """Enumerate how a service was mapped to an executor host.

    ``ORPHANED`` is a first-class outcome, not an error: an inventory row routinely
    outlives the executor that served it, and recording it is what keeps a sweep from
    probing the wrong host and reporting confidently wrong facts about it.

    :cvar NAME: Matched an executor host by node name.
    :cvar ADDRESS: Matched an executor host by node address.
    :cvar ORPHANED: No live executor host serves this service.
    """

    NAME = "name"
    ADDRESS = "address"
    ORPHANED = "orphaned"


class ScanFailure(StrEnum):
    """Enumerate the kinds of scan failure a reader can be told what to do about.

    Stored beside ``last_error`` rather than parsed back out of it: the message mixes
    the node's own stderr, exception text and task API detail, and its wording must
    stay free to change without breaking whoever keys a resolution hint off it.

    A node with no usable executor has no code here, because it is never dispatched
    to and so never fails; its state is the ``executor`` block every host row
    already carries.

    :cvar DISPATCH_REJECTED: The scan was never queued: the tasks API refused it or
        could not be reached.
    :cvar NOT_STARTED: The scan was queued but had not started when the wait ran out.
    :cvar TIMED_OUT: The scan started but had not finished when the wait ran out.
    :cvar BLOCKED: A scan given up on could not be stopped, and will block this
        node's next one until it is.
    :cvar ENVIRONMENT_SETUP_FAILED: The node could not prepare the scan's Python
        environment: no ``python3``, no ``venv`` module, or ``pip`` could not install
        the scan's one dependency.
    :cvar SCAN_CRASHED: The scan itself failed on the node.
    :cvar SCAN_LOST: The scan was lost, stopped or went stale before it reported -
        usually because the node or its agent restarted.
    :cvar NO_OUTPUT: The scan finished but reported nothing for this node or service.
    :cvar DATABASE_UNREACHABLE: The scan ran but could not connect to the database.
    :cvar DATABASE_AUTH_FAILED: The scan ran but the database rejected its
        credentials.
    :cvar DATABASE_ERROR: The scan ran but the database failed it some other way.
    :cvar UNKNOWN: Anything not recognised. A reader gets the raw detail and no hint,
        rather than a guessed one.
    """

    DISPATCH_REJECTED = "dispatch_rejected"
    NOT_STARTED = "not_started"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    ENVIRONMENT_SETUP_FAILED = "environment_setup_failed"
    SCAN_CRASHED = "scan_crashed"
    SCAN_LOST = "scan_lost"
    NO_OUTPUT = "no_output"
    DATABASE_UNREACHABLE = "database_unreachable"
    DATABASE_AUTH_FAILED = "database_auth_failed"
    DATABASE_ERROR = "database_error"
    UNKNOWN = "unknown"


class ObservedEntity(SQLModel):
    """Carry the columns every probed entity has, whatever it is.

    Not a table: a mixin, so the two entity tables cannot drift apart on
    the freshness rules. Each field declares a *type* rather than a ``Column``,
    because a Column instance binds to exactly one Table and sharing one across two
    models fails at import.

    Deliberately **not** :class:`~app.core.db.models.BaseSQLModel`: that base
    mandates an auto-incrementing integer ``id`` as the primary key, and both
    tables here are keyed on a natural id PMM already assigns (``node_id`` /
    ``service_id`` on :class:`OmHost` / :class:`OmService`) so a row can be
    looked up by the same id every other caller already has, with nothing to
    translate. A second, unused surrogate ``id`` column would only invite a
    query that joins on the wrong one. ``updated_at`` is declared here instead
    of inherited for the same reason: :class:`~app.core.db.models.BaseSQLModel`
    pairs it with a database-managed ``created_at`` this mixin has no use for —
    ``first_seen_at`` already is that column, under the name the freshness
    lifecycle uses elsewhere on this class.

    The lifecycle these implement is four rules, each cheap now and expensive to
    discover later:

    * ``failing_since`` is set with ``COALESCE(failing_since, now())`` — overwrite it
      on every failure and "since" quietly becomes "most recent failure", so the
      duration is always about one schedule interval and the column is worthless;
    * a failed probe must **not** erase ``observed``. The last known good document is
      kept with its own ``collected_at``, because what a host was running when last
      seen is exactly what is wanted while it is unreachable;
    * only a run that actually attempted an entity touches its timestamps, so a
      single-host refresh does not mark everything it skipped as failed;
    * the upsert lists the columns it owns explicitly. Nothing here is user-writable
      *yet*, so there is nothing to clobber — but a blanket ``ON CONFLICT DO UPDATE``
      over every column wipes the first field that ever is.

    :param observed: Everything the probe collected, with its own ``collected_at``.
        Non-nullable with a ``{}`` default rather than nullable: PMM Extensions' ``AutoJSON``
        stores a Python ``None`` as the JSON scalar ``null``, so "never probed" and
        "probed, found nothing" would be indistinguishable.
    :param first_seen_at: When OM first wrote a row for this entity.
    :param last_attempt_at: When a run last targeted it. Not "when a run last ran".
    :param last_success_at: When it last answered. This is the data's age.
    :param failing_since: The **first** failure after the last success, so the column
        says "failing for three days" rather than "failed a minute ago". ``None``
        while healthy.
    :param consecutive_failures: Failures since the last success.
    :param last_error: The most recent failure detail.
    :param last_error_code: What kind of failure ``last_error`` is. ``None`` while
        healthy, and on a row whose last failure predates the column.
    :param last_run_id: The run that last attempted it, for joining to the receipt.
    :param updated_at: When this row last changed.
    """

    observed: dict[str, Any] = SQLField(
        default_factory=dict,
        sa_type=_observed_document_type(),
        nullable=False,
        # Parenthesised rather than bare: on both dialects PMM Extensions runs the parentheses
        # are ordinary grouping, so this resolves to the same ``{}`` default either
        # way and there is nothing to gain by unwrapping it.
        sa_column_kwargs={"server_default": text("('{}')")},
    )
    first_seen_at: UTCDatetime = SQLField(
        default_factory=utc_now, sa_type=DateTimeWithTimezone, nullable=False
    )
    last_attempt_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    last_success_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    failing_since: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    consecutive_failures: int = SQLField(default=0, nullable=False)
    last_error: str | None = SQLField(default=None)
    last_error_code: ScanFailure | None = SQLField(
        default=None,
        # The values, not the member names ``ProbeRunStatus`` stores: they are what
        # the API reports, and what rows written before the CHECK already hold.
        sa_type=EnumField(
            ScanFailure,
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
        ),
    )
    last_run_id: UUID | None = SQLField(default=None)
    updated_at: UTCDatetime = SQLField(
        default_factory=utc_now, sa_type=DateTimeWithTimezone, nullable=False
    )


class OmHost(ObservedEntity, table=True):
    """Record one host, whether or not a database was found on it.

    Keyed on **PMM's** node id. OM is not the system of record for identity here,
    PMM is, so the table and the API speak the id every consumer already holds and
    nothing needs translating anywhere — including the scoped refresh, which takes
    the ``node_id`` PMM already has.

    Plain ``str``, not ``uuid``: PMM's ids are usually UUIDs but not always. The PMM
    server's own node is the literal string ``pmm-server`` in every deployment, and a
    ``uuid`` column would reject the one node every installation has. Left as SQLModel's
    inferred ``AutoString`` rather than pinned to ``sa_type=Text``: it is what SQLModel
    gives a ``str`` primary key, it stays unbounded on both dialects PMM Extensions runs, and it is
    the same type :attr:`app.inventory.models.NodeBase.external_id` carries for its own
    external identifier.

    The consequence to accept openly is that if PMM re-registers a node under a new
    id, OM gets a second row and the old one stays — there is no host retention, only
    ``DELETE /hosts/{node_id}`` removes it. The usual answer — a OM-minted id plus a
    natural key to recognise the machine across ids — has no natural key available:
    ``machine_id`` is inherited from the container image, so most of this sandbox
    reports one shared value and the rest report an empty string. Matching on it would
    merge unrelated hosts. Hence no surrogate key: it would only move the guess into a
    matching function.

    Class named ``OmHost`` rather than ``Host``, and the table ``om_host`` rather
    than bare ``host``: SQLModel keeps one registry for the whole application and
    it already carries ``app.inventory.models.Service``, so the short class name
    is genuinely taken — and that table is also named plain ``service`` with no
    schema of its own (``schema=None``), which OM's ``service`` would collide with
    the moment anything collapses schemas into one physical namespace, as the
    real-PostgreSQL test lane deliberately does for worker isolation.
    Prefixing both avoids the class collision and the table collision the same
    way. The pair is kept symmetrical.

    :param node_id: PMM's node id, the primary key.
    :param name: The node's registered name.
    :param address: The node's registered address.
    :param executor_host: The Nomad client that serves it, ``None`` when none does.
    """

    __tablename__ = "om_host"
    __table_args__ = (
        # Partial: the healthy majority is not in the index at all. Expressed here
        # rather than with ``index=True`` because a ``WHERE`` clause cannot be, and
        # OM's migrations are hand-written anyway (autogenerate is off for them).
        Index(
            "ix_om_host_failing_since",
            "failing_since",
            postgresql_where=text("failing_since IS NOT NULL"),
        ),
        {"schema": OM_SCHEMA},
    )

    node_id: str = SQLField(primary_key=True)
    name: str = SQLField(sa_type=Text, nullable=False)
    address: str | None = SQLField(default=None, sa_type=Text)
    executor_host: str | None = SQLField(default=None, sa_type=Text)


class OmService(ObservedEntity, table=True):
    """Record one MongoDB service PMM has registered, on its host.

    A service row is a service **PMM knows** — that is what keying on ``service_id``
    means. The probe will still find mongod processes PMM has no service for, and
    those are recorded on the host's ``observed`` document instead: no identity to
    invent, no schema commitment, and the estate view does not get to claim the host
    is empty when it is not. Arbiters are the reason that case is normal rather than
    exotic: one holds no data, therefore no user documents, therefore SCRAM cannot
    authenticate, therefore ``pmm-admin add mongodb`` fails for it.

    The foreign key is safe because both tables belong to *this* app. Across apps the
    ``om`` schema takes no foreign keys at all: each app that owns migrations is an
    independent branch, an image that strips an app removes its ``versions/``
    directory, and there is no guaranteed ordering between branches — so an FK into
    another app's table can reference something that legitimately vanishes.

    :param service_id: PMM's service id, the primary key. Plain ``str`` for the same
        reason as :attr:`OmHost.node_id`, including the ``AutoString`` inference.
    :param node_id: The host it runs on.
    :param name: The service name as PMM registered it.
    :param port: The port it listens on.
    :param role: What the probe found it to be — ``mongod``, ``mongos``, ``config``,
        ``arbiter``. Observed, not declared: plain text rather than an enum, because a
        role we have not thought of should land in the column rather than raise. (An
        enum would also need care: SQLAlchemy's non-native ``Enum`` persists by member
        *name*, so a CHECK constraint listing lowercase values rejects every insert.)
    """

    __tablename__ = "om_service"
    __table_args__ = (
        # Named rather than left to ``index=True``, which derives the name from the
        # *declared* table — and the declared schema is the symbolic token, so the
        # index would ship into the real ``om`` schema called
        # ``ix_om_schema_service_node_id``.
        Index("ix_om_service_node_id", "node_id"),
        {"schema": OM_SCHEMA},
    )

    service_id: str = SQLField(primary_key=True)
    node_id: str = SQLField(
        sa_column=Column(
            AutoString(),
            # Qualified with the symbolic schema: the FK target is resolved against
            # the *declared* name, and the connection translates both sides together.
            # AutoString, not Text: this column carries ix_om_service_node_id, and
            # matches the AutoString primary key it references on om_host.
            ForeignKey(f"{OM_SCHEMA}.om_host.node_id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    name: str | None = SQLField(default=None, sa_type=Text)
    port: int | None = SQLField(default=None)
    role: str | None = SQLField(default=None, sa_type=Text)


class ProbeRun(BaseUUIDSQLModel, table=True):
    """Record one probe sweep: which hosts it attempted, and what came of it.

    A receipt, not a copy of the estate. What the probe *found* lives on
    :class:`OmHost` and :class:`OmService`, upserted and current; this table only
    says which entity was attempted, on which executor host, whether it answered,
    how long it took and what failed. Keeping the collected attributes out of it is
    load-bearing — with ``RUN_RETENTION`` runs kept, putting them here would store
    the same facts that many times over and create a second source of truth for them.

    :param started_at: When the sweep began.
    :param finished_at: When it reached a terminal status; ``None`` while running.
    :param status: The sweep's lifecycle state.
    :param services_total: MongoDB services inventory reported.
    :param services_resolved: ...of which mapped to a live executor host.
    :param services_orphaned: ...of which did not. Not an error.
    :param services_answered: Services that returned a usable probe record.
    :param hosts_total: Hosts in scope this sweep, service or no service.
    :param hosts_probeable: ...of which had a usable executor to dispatch to. The
        difference from ``hosts_total`` is the estate nothing can be run on, which is
        a fact about onboarding rather than a failure of the sweep.
    :param hosts_answered: Hosts that returned a usable record.
    :param hosts_finished: Hosts whose scan has come back, answered or not, out of
        ``hosts_probeable``. Counted up while the sweep runs, with ``hosts_total``
        and ``hosts_probeable`` written as soon as the sweep knows them, so a running
        sweep can say how far it has got.
    :param nodes: One record per host: where it was probed, how that host was
        matched, whether it answered, how long it took, and its dispatch's task
        history id — so a reader can still open the probe's raw output, which the
        receipt itself does not carry. The counters above are this list's summary,
        and a summary is all a sweep could show until this column existed — "5 of 14
        answered" cannot say *which* five, on which hosts, or which one took a
        minute.
    :param scope: The node ids this run was asked to refresh, or ``None`` for the
        whole estate. Stored rather than inferred, because without it the receipt
        cannot be read honestly — "9 of 13 answered" means something different when
        the run was only ever asked about one host — and because the single-flight
        guard has nothing to compare against.
    :param error: The failure detail when the sweep itself raised.
    """

    __tablename__ = "om_inventory_run"
    # A *symbolic* schema, translated per bind by the engine
    # (``app/extensions/apps/shared/om/config.py``). Never a literal: SQLite has no schemas, so a real
    # name here would make the table uncreatable in the unit suite, and the
    # real-PostgreSQL lane routes every table into a per-xdist-worker schema that a
    # hard-coded ``om`` would escape and then collide across workers.
    __table_args__ = (
        # Named for the same reason OmService names its index: ``index=True``
        # derives the name from the declared table, schema token included.
        Index("ix_om_inventory_run_started_at", "started_at"),
        Index("ix_om_inventory_run_status", "status"),
        {"schema": OM_SCHEMA},
    )

    started_at: UTCDatetime = SQLField(
        sa_type=DateTimeWithTimezone, default_factory=utc_now
    )
    finished_at: UTCDatetime | None = SQLField(
        default=None, sa_type=DateTimeWithTimezone
    )
    status: ProbeRunStatus = SQLField(
        default=ProbeRunStatus.RUNNING,
        sa_column=Column(
            EnumField(ProbeRunStatus, native_enum=False, create_constraint=True),
            nullable=False,
        ),
    )

    services_total: int = SQLField(default=0)
    services_resolved: int = SQLField(default=0)
    services_orphaned: int = SQLField(default=0)
    services_answered: int = SQLField(default=0)

    # A sweep attempts hosts as well as services, and has since a host became
    # probeable for its own sake. Counting only services made a refresh of a
    # pmm-client host with no database read as "0 of 0", which is indistinguishable
    # from a run that did nothing.
    hosts_total: int = SQLField(default=0)
    hosts_probeable: int = SQLField(default=0)
    hosts_answered: int = SQLField(default=0)
    hosts_finished: int = SQLField(default=0)

    # Explicit JSONB rather than ``AutoJSON``: the latter silently drops
    # ``none_as_null`` on PostgreSQL, so a Python ``None`` lands as the JSON scalar
    # ``null`` and a caller cannot tell "no nodes" from "the column was never set".
    # A run reads its nodes all at once or not at all, and a per-node table would
    # need its own retention story on top of the run's. ``JSON`` is the base type
    # here, not ``postgresql.JSONB``: PostgreSQL is the dialect that gets a variant
    # carved out of it, rather than the default every other dialect is made to
    # carve SQLite out of.
    nodes: list[dict[str, Any]] = SQLField(
        default_factory=list,
        sa_column=Column(
            JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
            # Parenthesised rather than bare, like ObservedEntity.observed's own
            # server_default: the parentheses are ordinary grouping and resolve to
            # the same [] default.
            server_default=text("('[]')"),
        ),
    )
    # The one nullable JSON column in this app, and it needs ``none_as_null`` to be
    # honest about it. SQLAlchemy's JSON types default to storing a Python ``None`` as
    # the JSON scalar ``null``, so without this a full-estate run is written as
    # ``'null'::jsonb`` and `WHERE scope IS NULL` finds none of them — the Python side
    # reads back ``None`` either way, so nothing complains until someone asks the
    # database which runs were full sweeps. Measured happening before it was set.
    # ``none_as_null`` is set on both the ``JSON`` base and the ``JSONB`` variant, for
    # the same reason ``nodes`` above inverts which dialect is the default.
    scope: list[str] | None = SQLField(
        default=None,
        sa_column=Column(
            JSON(none_as_null=True).with_variant(
                postgresql.JSONB(astext_type=Text(), none_as_null=True), "postgresql"
            ),
            nullable=True,
        ),
    )
    error: str | None = SQLField(default=None)


# api_routes.py's request/response DTOs, moved here to match the sibling apps'
# convention (report, mysql_backups, atw all keep theirs in their own models.py)
# rather than declaring them inline in the routes file. Plain ``pydantic.BaseModel``
# subclasses, not table models — unlike everything above them in this file.


class ProbeCounts(BaseModel):
    """Count what one sweep reached.

    ``resolved`` versus ``answered`` is the diagnostic split: the first says the
    service mapped to a live executor host, the second says the node ran the payload.
    A sweep with ``resolved=9, answered=0`` is a healthy mapping and broken executors.

    :param services_total: MongoDB services inventory reported.
    :param services_resolved: ...of which mapped to a live executor host.
    :param services_orphaned: ...of which did not. Not an error.
    :param services_answered: Services that returned a usable probe record.
    :param hosts_total: Hosts in scope this sweep, service or no service.
    :param hosts_probeable: ...of which had a usable executor to dispatch to.
    :param hosts_answered: Hosts that returned a usable record.
    :param hosts_finished: Hosts whose scan has come back, answered or not, out of
        ``hosts_probeable``; counted up while the sweep is still running.
    """

    services_total: int
    services_resolved: int
    services_orphaned: int
    services_answered: int
    # A sweep attempts hosts too, and has since a host became probeable for its own
    # sake. Counting only services makes a refresh of a host with no database read as
    # "0 of 0", which is indistinguishable from a run that did nothing.
    hosts_total: int = 0
    hosts_probeable: int = 0
    hosts_answered: int = 0
    hosts_finished: int = 0


class ProbeRunFailingNode(BaseModel):
    """Name one node a sweep failed to scan.

    :param node_id: PMM's node id.
    :param name: The node's name as the sweep recorded it, or its node id where the
        sweep recorded none.
    """

    node_id: str
    name: str


class ProbeRunResponse(BaseModel):
    """Carry one sweep's record.

    :param run_id: The sweep's id.
    :param status: ``running`` / ``success`` / ``partial`` / ``failed``.
    :param started_at: When it began.
    :param finished_at: When it reached a terminal status; ``None`` while running.
    :param counts: What it reached.
    :param scope: The hosts it was asked to refresh, or ``None`` for the whole
        estate. Without it the counters cannot be read: "9 of 13 answered" means
        something different when the run was only ever asked about one host.
    :param error: The failure detail when the sweep itself raised.
    :param failing_nodes: The nodes whose scan failed in this sweep, on the node's own
        dispatch or on any service on it, sorted by name. A node with no automation
        agent is not one: nothing was dispatched to it.
    """

    run_id: UUID
    status: str
    started_at: UTCDatetime
    finished_at: UTCDatetime | None = None
    counts: ProbeCounts
    scope: list[str] | None = None
    error: str | None = None
    failing_nodes: list[ProbeRunFailingNode] = Field(default_factory=list)


class ProbeNodeService(BaseModel):
    """Report one service on a host, as this sweep saw it.

    :param service_id: **PMM's** service UUID, or ``None`` where inventory holds none.
    :param service_name: Its name, so a reader is not left joining UUIDs by hand.
    :param answered: Whether the host returned a usable record for it.
    :param error: Why it did not, when it did not.
    """

    service_id: str | None = None
    service_name: str | None = None
    answered: bool = False
    error: str | None = None


class ProbeNode(BaseModel):
    """Report one **host** this sweep attempted, and what came of it.

    Host-oriented, because a sweep attempts hosts. A flat list of services — which
    this was — cannot show a machine carrying a PMM client and no database, however
    many times it is probed, and that machine is the case OM most exists to describe.

    One dispatch covers every service on a host, so the host owns the timing and the
    failure and its services carry only what is theirs. Previously the duration was
    repeated identically across a host's services, which read as several measurements
    when it was one.

    :param node_id: **PMM's** node id, the key OM holds this host under.
    :param host_name: The node's registered name.
    :param executor_host: The client its probe ran on; ``None`` when none matched.
    :param resolution: ``name`` / ``address`` / ``orphaned`` — how that client was
        matched, or that it was not. Orphaned is why nothing ran, not an error.
    :param answered: Whether the *host* returned a record. A different question from
        whether its services did: a host with no database answers perfectly well and
        has no services at all.
    :param duration_seconds: The host's wall-clock, dispatch to collected output.
    :param task_history_id: The dispatch's task history id, so a reader can open the
        probe's raw output. ``None`` when the dispatch never got one back.
    :param error: The host-level failure, when its probe failed.
    :param services: The services on it, empty when there are none.
    """

    node_id: str
    host_name: str | None = None
    executor_host: str | None = None
    resolution: str
    answered: bool = False
    duration_seconds: float | None = None
    task_history_id: int | None = None
    error: str | None = None
    services: list[ProbeNodeService] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Return whether this host's dispatch, or any service on it, failed.

        A host with no automation agent has neither: nothing was dispatched to it.

        :return: ``True`` when the host or one of its services records an error.
        """
        return bool(self.error) or any(service.error for service in self.services)


class ProbeRunDetail(ProbeRunResponse):
    """Carry one sweep, with everything it recorded.

    Kept apart from the list shape on purpose: a sweep's nodes run to a few hundred
    records, so returning them for every row of a 25-run history would make the list
    an order of magnitude larger to serve a page that shows one run at a time.

    :param nodes: What the sweep attempted per host.
    """

    nodes: list[ProbeNode] = Field(default_factory=list)


class TriggerRequest(BaseModel):
    """Ask for a refresh of named hosts rather than the whole estate.

    :param node_ids: PMM's node ids. Empty, or the whole body absent, means every
        host OM holds — which is what the scheduled sweep does.
    """

    node_ids: list[str] = Field(default_factory=list)


class ProbeRunAccepted(BaseModel):
    """Return the id of a queued sweep.

    Returned with ``202``: a sweep dispatches Nomad jobs and takes tens of seconds, so
    it is never performed synchronously.

    :param run_id: The queued sweep's id.
    :param status: Always ``running`` at this point.
    :param started_at: When the run row was created.
    :param scope: The hosts it will refresh, or ``None`` for the whole estate.
    """

    run_id: UUID
    status: str
    started_at: UTCDatetime
    scope: list[str] | None = None


class FreshnessResponse(BaseModel):
    """Carry the freshness columns every probed entity's response reports.

    Shared by :class:`ServiceResponse` and :class:`HostResponse` so the two cannot
    report the freshness lifecycle differently, the same reason :class:`ObservedEntity`
    is a mixin rather than duplicated on :class:`OmHost` and :class:`OmService`.

    :param observed: Everything collected, with its own ``collected_at``: when the
        last successful probe ran. On a service, a failed attempt since then still
        refreshes ``probe_status`` and the facts read off the host rather than out of
        the database (the installed binary and the process serving the port), so those
        can be newer.
        Empty when this entity has never been successfully probed.
    :param first_seen_at: When OM first wrote a row for it.
    :param last_attempt_at: When a run last targeted it. ``None`` means no run ever
        has, which is different from having tried and failed.
    :param last_success_at: When it last answered. This is the data's age.
    :param failing_since: The first failure after the last success; ``None`` while
        healthy.
    :param consecutive_failures: Failures since the last success.
    :param last_error: The most recent failure detail.
    :param last_error_code: What kind of failure it is; ``None`` while healthy or
        when the failure predates classification.
    :param last_run_id: The run that last attempted it, so a reader of the failure
        can open the run that produced it; ``None`` until a run has.
    """

    observed: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: UTCDatetime
    last_attempt_at: UTCDatetime | None = None
    last_success_at: UTCDatetime | None = None
    failing_since: UTCDatetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    last_error_code: ScanFailure | None = None
    last_run_id: UUID | None = None


class ServiceResponse(FreshnessResponse):
    """Report one MongoDB service PMM has registered, as OM currently holds it.

    Keyed on **PMM's** service id, which is the whole benefit of storing it that way:
    the path and the payload carry the id every consumer already has, with nothing to
    translate on either side.

    :param service_id: PMM's service id.
    :param node_id: The host it runs on.
    :param name: The service name as PMM registered it.
    :param port: The port it listens on.
    :param role: What the probe found it to be, when a probe determined one.
    """

    service_id: str
    node_id: str
    name: str | None = None
    port: int | None = None
    role: str | None = None


class HostResponse(FreshnessResponse):
    """Report one host, with the services OM knows are on it.

    A host is a row whether or not any MongoDB was found on it: that is what makes
    "which hosts have no database" a query rather than an absence, and it is the only
    way a machine that has never run one appears at all.

    ``observed`` (from :class:`FreshnessResponse`) additionally carries
    ``unregistered_mongods`` here, where the probe found a database PMM has no
    service for.

    :param node_id: PMM's node id.
    :param name: The node's registered name.
    :param address: The node's registered address.
    :param executor_host: The Nomad client serving it. ``None`` means nothing can be
        run there, which is a fact about the estate rather than a probe failure.
    :param services: The services on it. Empty is a meaningful answer, not a gap.
    """

    node_id: str
    name: str
    address: str | None = None
    executor_host: str | None = None
    services: list[ServiceResponse] = Field(default_factory=list)
