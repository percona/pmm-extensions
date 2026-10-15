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

"""Drop redundant external_id indexes and make Node.source non-native

Revision ID: a7d3f1c95b26
Revises: 13204e7e1043
Create Date: 2026-09-28 10:00:00.000000

The native ``sourceenum`` type is what kept ``node.source`` to its labels on
PostgreSQL, so the conversion replaces it with an explicit CHECK under the name
and ``IN`` clause ``NodeBase.source`` emits. The type is built with
``create_constraint=False`` so that CHECK is added exactly once, on every
dialect.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3f1c95b26'
down_revision: Union[str, None] = '13204e7e1043'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LABELS = ('PMM',)
_SOURCE_CHECK_NAME = 'sourceenum'
_SOURCE_CHECK_SQL = 'source IN ({})'.format(', '.join(f"'{label}'" for label in _LABELS))


def _non_native_source_type() -> sa.Enum:
    """Build the non-native ``node.source`` type, with its CHECK left to the caller.

    :return: The non-native enum type.
    """
    return sa.Enum(*_LABELS, name='sourceenum', native_enum=False, create_constraint=False)


def upgrade() -> None:
    with op.batch_alter_table('node', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_node_external_id'))
        batch_op.alter_column(
            'source',
            existing_type=sa.Enum(*_LABELS, name='sourceenum'),
            type_=_non_native_source_type(),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(_SOURCE_CHECK_NAME, _SOURCE_CHECK_SQL)

    with op.batch_alter_table('service', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_service_external_id'))

    # Nothing uses the PG type once node.source is converted.
    sa.Enum(name='sourceenum').drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    sa.Enum(*_LABELS, name='sourceenum').create(op.get_bind(), checkfirst=True)

    with op.batch_alter_table('service', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_external_id'), ['external_id'], unique=False)

    with op.batch_alter_table('node', schema=None) as batch_op:
        batch_op.drop_constraint(_SOURCE_CHECK_NAME, type_='check')
        batch_op.alter_column(
            'source',
            existing_type=_non_native_source_type(),
            type_=sa.Enum(*_LABELS, name='sourceenum'),
            existing_nullable=False,
            postgresql_using='source::sourceenum',
        )
        batch_op.create_index(batch_op.f('ix_node_external_id'), ['external_id'], unique=False)
