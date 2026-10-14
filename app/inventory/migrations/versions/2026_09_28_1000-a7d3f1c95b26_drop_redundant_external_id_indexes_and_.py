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


def upgrade() -> None:
    with op.batch_alter_table('node', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_node_external_id'))
        batch_op.alter_column(
            'source',
            existing_type=sa.Enum(*_LABELS, name='sourceenum'),
            type_=sa.Enum(*_LABELS, name='sourceenum', native_enum=False),
            existing_nullable=False,
        )

    with op.batch_alter_table('service', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_service_external_id'))

    # Nothing uses the PG type once node.source is converted.
    sa.Enum(name='sourceenum').drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    sa.Enum(*_LABELS, name='sourceenum').create(op.get_bind(), checkfirst=True)

    with op.batch_alter_table('service', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_external_id'), ['external_id'], unique=False)

    with op.batch_alter_table('node', schema=None) as batch_op:
        batch_op.alter_column(
            'source',
            existing_type=sa.Enum(*_LABELS, name='sourceenum', native_enum=False),
            type_=sa.Enum(*_LABELS, name='sourceenum'),
            existing_nullable=False,
            postgresql_using='source::sourceenum',
        )
        batch_op.create_index(batch_op.f('ix_node_external_id'), ['external_id'], unique=False)
