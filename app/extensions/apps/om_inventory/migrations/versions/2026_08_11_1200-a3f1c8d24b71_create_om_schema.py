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

"""create the om schema and its tables

Revision ID: a3f1c8d24b71
Revises:
Create Date: 2026-08-11 12:00:00.000000

The whole OM schema in one revision: ``om.om_host`` and ``om.om_service`` for what the
estate *is*, ``om.om_inventory_run`` for what one sweep *did*. Table names carry an
``om_`` prefix on top of the schema that already qualifies them, because the schema
alone does not: the real-PostgreSQL test lane translates every declared schema
token into the same per-worker schema, which collapsed bare
``service`` onto SEP inventory's own ``service`` (``schema=None``) the first time
this app's tables were exercised against a real, non-SQLite database — see
``app/sep/apps/om_inventory/models.py``'s module comment for the full account.

Rewritten in place rather than extended by follow-up revisions - the host counters on
``om_inventory_run`` and the ``SKIPPED`` run status were each a revision of their own
while this branch was being written, and both are folded in here - because none of this
has shipped — there is no deployment whose data a move migration would preserve. The
cost is local: a machine that ran an earlier version of this revision has to drop
what it created and delete the ``a3f1c8d24b71`` row from ``alembic_version_sep`` by
hand, since the migration that would have moved it deliberately does not exist. Once
OM ships, this file freezes and changes become new revisions.

The schema is named symbolically throughout — ``schema="om_schema"``, translated by
the connection (``app/sep/migrations/env.py``) — with the single exception of
``CREATE SCHEMA``, which is raw DDL and therefore untranslated. That statement asks
:func:`app.sep.apps.shared.om.config.om_schema` for the real name and does nothing
when the bind has no schemas.

Alembic's comparison sees these tables at their translated schema —
``app/sep/migrations/env.py`` hands it a translated copy of the metadata — so an
in-sync ``alembic check`` is clean. ``--autogenerate`` renders the *resolved*
schema instead (``None`` on SQLite, the real name on PostgreSQL), so a generated
OM script still has to be edited to name ``OM_SCHEMA_SYMBOL`` before it is
committed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlmodel.sql.sqltypes import AutoString

from app.extensions.apps.shared.om.config import OM_SCHEMA_SYMBOL, om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "a3f1c8d24b71"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = ("om_inventory",)
depends_on: Union[str, Sequence[str], None] = None


def _observed_column() -> sa.Column:
    """Build an ``observed`` document column.

    Non-nullable with a ``{}`` server default rather than nullable: SEP's ``AutoJSON``
    stores a Python ``None`` as the JSON scalar ``null``, which would make "never
    probed" and "probed, found nothing" the same value in the column.

    The default is parenthesised rather than bare. On both dialects SEP runs the
    parentheses are ordinary grouping, so the clause resolves to the same ``{}``
    literal either way.

    :return: The column.
    """
    return sa.Column(
        "observed",
        sa.JSON().with_variant(
            postgresql.JSONB(astext_type=sa.Text()), "postgresql"
        ),
        nullable=False,
        server_default=sa.text("('{}')"),
    )


def _freshness_columns() -> list[sa.Column]:
    """Build the per-entity freshness and failure columns.

    Identical on both entity tables by construction — a host can be perfectly
    reachable while one mongod on it cannot be probed, which is one of the reasons
    these are two rows rather than one.

    :return: The columns, in declaration order.
    """
    return [
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        # The *first* failure after the last success, maintained with COALESCE so it
        # keeps saying "failing for three days" rather than "failed a minute ago".
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _create_schema_if_needed() -> str | None:
    """Create OM's schema on a bind that has one, and report what it is called.

    :return: The real schema name, or ``None`` when the bind has no schemas and
        OM's tables therefore live in the default one.
    """
    schema = om_schema(sep_settings.DATABASE)
    if schema is not None:
        op.execute(sa.schema.CreateSchema(schema, if_not_exists=True))
    return schema


def upgrade() -> None:
    schema = _create_schema_if_needed()

    # Inspect the schema the tables actually land in, asking for the *real* name —
    # the token is not a schema anything can be inspected in. Inspecting the default
    # schema would report "absent" against a database that already has them.
    existing = set(sa.inspect(op.get_bind()).get_table_names(schema=schema))

    if "om_host" not in existing:
        op.create_table(
            "om_host",
            # PMM's node id. AutoString, not uuid or Text: PMM's ids are usually
            # UUIDs but not always — the PMM server's own node is the literal
            # string ``pmm-server``, in every deployment, so a uuid column would
            # reject the one node every installation has. Not Text either: this is
            # the primary key, and AutoString is what SQLModel gives a str key,
            # unbounded on both dialects SEP runs.
            sa.Column("node_id", AutoString(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("executor_host", sa.Text(), nullable=True),
            _observed_column(),
            *_freshness_columns(),
            sa.PrimaryKeyConstraint("node_id"),
            schema=OM_SCHEMA_SYMBOL,
        )
        op.create_index(
            "ix_om_host_failing_since",
            "om_host",
            ["failing_since"],
            unique=False,
            schema=OM_SCHEMA_SYMBOL,
            # Partial: the healthy majority never enters the index. Ignored on
            # SQLite, which is fine — the index is still created, just complete.
            postgresql_where=sa.text("failing_since IS NOT NULL"),
        )

    if "om_service" not in existing:
        op.create_table(
            "om_service",
            # AutoString for the same reason as om_host.node_id: both are indexed
            # (this one is the primary key, node_id below carries
            # ix_om_service_node_id), and both match the keys they reference.
            sa.Column("service_id", AutoString(), nullable=False),
            sa.Column("node_id", AutoString(), nullable=False),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("port", sa.Integer(), nullable=True),
            # Observed rather than declared, so plain text: a role nobody thought of
            # should land in the column rather than raise.
            sa.Column("role", sa.Text(), nullable=True),
            _observed_column(),
            *_freshness_columns(),
            sa.PrimaryKeyConstraint("service_id"),
            # Safe because both tables belong to this app. Across apps the om schema
            # takes no foreign keys at all: every app's migrations are an independent
            # branch, an image that strips an app removes its versions/ directory, and
            # there is no ordering between branches — so a cross-app FK can reference
            # a table that legitimately vanishes.
            sa.ForeignKeyConstraint(
                ["node_id"],
                [f"{OM_SCHEMA_SYMBOL}.om_host.node_id"],
                ondelete="CASCADE",
            ),
            schema=OM_SCHEMA_SYMBOL,
        )
        op.create_index(
            "ix_om_service_node_id",
            "om_service",
            ["node_id"],
            unique=False,
            schema=OM_SCHEMA_SYMBOL,
        )

    if "om_inventory_run" not in existing:
        op.create_table(
            "om_inventory_run",
            sa.Column("id", sa.Uuid(), autoincrement=False, nullable=False),
            # BaseUUIDSQLModel's own columns. Omitting them is the mistake that makes
            # the table exist and every query against it fail.
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "status",
                # The enum's member NAMES, not its values: SQLAlchemy's non-native
                # Enum persists by name, so a constraint listing the lowercase values
                # rejects every insert the model makes. ``EnumField`` stores
                # ``ProbeRunStatus.RUNNING`` as ``"RUNNING"`` while its value is
                # ``"running"`` — the same trap ``SettingClassEnum``'s docstring
                # records.
                sa.Enum(
                    "RUNNING",
                    "SUCCESS",
                    "PARTIAL",
                    "FAILED",
                    "SKIPPED",
                    name="proberunstatus",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
            ),
            sa.Column("services_total", sa.Integer(), nullable=False),
            sa.Column("services_resolved", sa.Integer(), nullable=False),
            sa.Column("services_orphaned", sa.Integer(), nullable=False),
            sa.Column("services_answered", sa.Integer(), nullable=False),
            # Hosts as well as services, because a sweep attempts both. Counting only
            # services made a refresh of a pmm-client host with no database read as
            # "0 of 0", which is indistinguishable from a run that did nothing - on
            # precisely the host OM exists to describe.
            sa.Column("hosts_total", sa.Integer(), nullable=False),
            sa.Column("hosts_probeable", sa.Integer(), nullable=False),
            sa.Column("hosts_answered", sa.Integer(), nullable=False),
            sa.Column(
                "nodes",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
                ),
                nullable=False,
                # Parenthesised rather than bare, like _observed_column's own
                # server_default: the parentheses are ordinary grouping and resolve
                # to the same [] default.
                server_default=sa.text("('[]')"),
            ),
            # NULL means the whole estate. A scoped run stores the node ids it was
            # asked about, because a receipt cannot be read honestly without them and
            # the single-flight guard has nothing to compare against.
            # ``none_as_null`` so a full-estate run is SQL NULL rather than the
            # JSON scalar ``null``; without it `scope IS NULL` matches nothing.
            sa.Column(
                "scope",
                sa.JSON(none_as_null=True).with_variant(
                    postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
                    "postgresql",
                ),
                nullable=True,
            ),
            sa.Column("error", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            schema=OM_SCHEMA_SYMBOL,
        )
        op.create_index(
            op.f("ix_om_inventory_run_started_at"),
            "om_inventory_run",
            ["started_at"],
            unique=False,
            schema=OM_SCHEMA_SYMBOL,
        )
        op.create_index(
            op.f("ix_om_inventory_run_status"),
            "om_inventory_run",
            ["status"],
            unique=False,
            schema=OM_SCHEMA_SYMBOL,
        )


def downgrade() -> None:
    """Drop this app's tables, leaving the schema that holds them in place.

    The schema is shared by every OM app (``app/sep/apps/shared/om/__init__.py``),
    so dropping it here would take another app's tables with it — and each app's
    migrations are an independent branch with no ordering between them, so there is
    no revision that could safely own the drop.
    """
    op.drop_index(
        op.f("ix_om_inventory_run_status"),
        table_name="om_inventory_run",
        schema=OM_SCHEMA_SYMBOL,
    )
    op.drop_index(
        op.f("ix_om_inventory_run_started_at"),
        table_name="om_inventory_run",
        schema=OM_SCHEMA_SYMBOL,
    )
    op.drop_table("om_inventory_run", schema=OM_SCHEMA_SYMBOL)
    op.drop_index(
        "ix_om_service_node_id", table_name="om_service", schema=OM_SCHEMA_SYMBOL
    )
    op.drop_table("om_service", schema=OM_SCHEMA_SYMBOL)
    op.drop_index(
        "ix_om_host_failing_since", table_name="om_host", schema=OM_SCHEMA_SYMBOL
    )
    op.drop_table("om_host", schema=OM_SCHEMA_SYMBOL)
