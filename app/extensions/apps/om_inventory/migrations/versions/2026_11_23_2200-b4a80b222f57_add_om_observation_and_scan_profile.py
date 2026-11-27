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

"""add om_observation and scan profile to om_inventory_run

Revision ID: b4a80b222f57
Revises: 77f3037c84cf
Create Date: 2026-11-23 22:00:00.000000

``om_inventory_run`` gains ``profile`` (which fact set a run collected) and
``requested_by_operation``, plus a ``UNIQUE (id, profile)`` that is the composite
foreign-key target the new ``om_observation`` table references, so an observation
can cite only a run of its own profile. ``profile`` backfills existing rows to
``monitored`` with a server default; the default stays on the column so a release
still running the previous code keeps inserting sweeps without it.

``om_observation`` holds one current document per ``(subject_kind, subject_key,
profile)``, upserted by the PL/pgSQL ``om_observation_put`` which keeps the newer
of two results and computes the digest over the facts (secret-free by contract,
classified on the host before ingestion). The function is PostgreSQL-only; SQLite
gets the table. The schema is named symbolically throughout, translated by the
connection, except the raw ``CREATE FUNCTION`` body.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.extensions.apps.shared.om.config import om_schema
from app.extensions.config import extensions_settings as sep_settings


# revision identifiers, used by Alembic.
revision: str = "b4a80b222f57"
down_revision: Union[str, None] = "77f3037c84cf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: ``ScanProfile``'s values, the domain of both profile CHECK constraints.
_PROFILES = ("monitored", "import_check", "managed", "install_check", "uninstall_check")
#: ``ObservationSubjectKind``'s values.
_SUBJECT_KINDS = ("member", "import_candidate", "replica_set", "cluster", "install_node")


def _qualified(schema: str | None, name: str) -> str:
    """Return a schema-qualified name for a raw ``CREATE FUNCTION`` body.

    :param schema: The real schema name, or ``None`` for the default schema.
    :param name: The unqualified object name.
    :return: The name a raw DDL statement uses.
    """
    return f"{schema}.{name}" if schema else name


def upgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)

    # ``create_constraint`` is off and the CHECK is created explicitly, so each
    # engine gets it once (on PostgreSQL ``ADD COLUMN`` would emit it as well).
    with op.batch_alter_table("om_inventory_run", schema=schema) as batch_op:
        batch_op.add_column(
            sa.Column(
                "profile",
                sa.Enum(*_PROFILES, name="scanprofile", native_enum=False, create_constraint=False),
                nullable=False,
                server_default="monitored",
            )
        )
        batch_op.add_column(sa.Column("requested_by_operation", sa.Uuid(), nullable=True))
        batch_op.create_check_constraint(
            "scanprofile", sa.column("profile").in_(_PROFILES)
        )
        batch_op.create_unique_constraint(
            "uq_om_inventory_run_id_profile", ["id", "profile"]
        )

    run = _qualified(schema, "om_inventory_run")
    op.create_table(
        "om_observation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # BaseSQLModel's own columns; ``om_observation_put`` sets ``created_at`` on
        # insert, since the function-written row cannot use the base's Python default.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "subject_kind",
            sa.Enum(
                *_SUBJECT_KINDS,
                name="observationsubjectkind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column(
            "profile",
            sa.Enum(*_PROFILES, name="scanprofile", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "facts",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_kind", "subject_key", "profile", name="uq_om_observation_subject"
        ),
        sa.ForeignKeyConstraint(
            ("scan_id", "profile"),
            (f"{run}.id", f"{run}.profile"),
            name="fk_om_observation_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("profile <> 'monitored'", name="ck_om_observation_not_monitored"),
        schema=schema,
    )

    if op.get_bind().dialect.name == "postgresql":
        obs = _qualified(schema, "om_observation")
        put = _qualified(schema, "om_observation_put")
        op.execute(
            f"CREATE FUNCTION {put}(sk TEXT, skey TEXT, prof TEXT, scan UUID, "
            "at TIMESTAMPTZ, f JSONB) RETURNS boolean LANGUAGE plpgsql AS $$\n"
            "DECLARE n integer;\n"
            "BEGIN\n"
            f"  INSERT INTO {obs} AS o (subject_kind, subject_key, profile, scan_id, "
            "collected_at, facts, digest, created_at)\n"
            "  VALUES (sk, skey, prof, scan, at, f, md5(f::text), now())\n"
            "  ON CONFLICT (subject_kind, subject_key, profile) DO UPDATE\n"
            "    SET scan_id = EXCLUDED.scan_id, collected_at = EXCLUDED.collected_at,\n"
            "        facts = EXCLUDED.facts, digest = EXCLUDED.digest, updated_at = now()\n"
            "    WHERE o.collected_at < EXCLUDED.collected_at;\n"
            "  GET DIAGNOSTICS n = ROW_COUNT;\n"
            "  RETURN n > 0;\n"
            "END $$;"
        )


def downgrade() -> None:
    schema = om_schema(sep_settings.DATABASE)
    if op.get_bind().dialect.name == "postgresql":
        put = _qualified(schema, "om_observation_put")
        op.execute(
            f"DROP FUNCTION IF EXISTS {put}(TEXT, TEXT, TEXT, UUID, TIMESTAMPTZ, JSONB);"
        )
    op.drop_table("om_observation", schema=schema)
    with op.batch_alter_table("om_inventory_run", schema=schema) as batch_op:
        batch_op.drop_constraint("uq_om_inventory_run_id_profile", type_="unique")
        batch_op.drop_constraint("scanprofile", type_="check")
        batch_op.drop_column("requested_by_operation")
        batch_op.drop_column("profile")
