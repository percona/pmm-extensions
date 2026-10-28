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

"""Provide idempotent Alembic helpers for the three ``settingoverride`` tracks."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

from app.core.db.sql_types import AutoJSON
from app.core.db.utils import (
    acquire_pg_advisory_xact_lock,
    check_constraint_name,
    column_exists,
    table_exists,
)
from app.core.encryption import DecryptionError, is_encrypted
from app.core.settings_override.constants import (
    SETTING_CLASS_MAX_LENGTH,
    SETTINGOVERRIDE_MIGRATION_LOCK_KEY,
    SETTINGOVERRIDE_UPDATED_BY_COLUMN,
)
from app.core.settings_override.models import setting_class_token
from app.core.settings_override.secret_storage import (
    decrypt_credential_url_leaves,
    decrypt_secret_leaves,
    reencrypt_credential_url_leaves,
    reencrypt_secret_leaves,
    unmark_secret_leaves,
)
from app.core.utils.fields import (
    credential_url_password,
    map_credential_url_password,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pydantic import BaseModel
    from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

_TABLE = "settingoverride"
_COLUMN = "setting_class"
_ENUM_NAME = "settingclassenum"
_STRING_LENGTH = SETTING_CLASS_MAX_LENGTH


def _locked_bind() -> Connection | None:
    """Return the bound connection holding the shared lock, or ``None`` if unusable.

    Every ``settingoverride`` migration opens the same way: take the advisory
    lock so two service tracks running ``upgrade heads`` against one physical
    database cannot execute the same work simultaneously, then no-op when the
    other track has already dropped the table.

    :return: The bound connection, or ``None`` when the table is absent.
    """
    bind = op.get_bind()
    acquire_pg_advisory_xact_lock(bind, SETTINGOVERRIDE_MIGRATION_LOCK_KEY)
    return bind if table_exists(bind, _TABLE) else None


def upgrade_drop_setting_class_check(previous_members: tuple[str, ...]) -> None:
    """Drop the ``setting_class`` CHECK and widen the column to ``VARCHAR(255)``.

    Idempotent on a shared PostgreSQL database: the second track no-ops once
    the first has already dropped the constraint. A missing table is also a
    no-op, matching the other ``settingoverride`` guards.

    :param previous_members: The members the CHECK enumerated before the drop;
        the longest sets the pre-drop column width.
    """
    bind = _locked_bind()
    if bind is None:
        return
    name = check_constraint_name(bind, _TABLE, _COLUMN)
    if name is None:
        return
    previous_length = max(len(member) for member in previous_members)
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{name}"'))
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if bind.dialect.name != "postgresql":
            batch_op.drop_constraint(name, type_="check")
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=previous_length),
            type_=sa.String(length=_STRING_LENGTH),
            existing_nullable=False,
        )


def downgrade_restore_setting_class_check(previous_members: tuple[str, ...]) -> None:
    """Restore the legacy CHECK, deleting rows that would violate it.

    Deletes every ``settingoverride`` row whose ``setting_class`` is not in
    ``previous_members`` and logs how many rows it removed. Re-enabling an
    app after this downgrade will not restore those overrides.

    :param previous_members: The members the restored CHECK enumerates.
    """
    bind = _locked_bind()
    if bind is None:
        return
    if check_constraint_name(bind, _TABLE, _COLUMN) is not None:
        return
    settingoverride = sa.table(_TABLE, sa.column(_COLUMN))
    result = bind.execute(
        settingoverride.delete().where(
            settingoverride.c[_COLUMN].notin_(previous_members)
        )
    )
    logger.info(
        "Deleted %s settingoverride row(s) whose setting_class is outside "
        "the CHECK member list the restored constraint enumerates.",
        result.rowcount,
    )
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=_STRING_LENGTH),
            type_=sa.Enum(
                *previous_members,
                name=_ENUM_NAME,
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )


def upgrade_add_updated_by() -> None:
    """Add the nullable ``updated_by`` column, once across all three tracks.

    Idempotent on a shared PostgreSQL database: whichever of the ``extensions``,
    ``tasks`` and ``inventory`` tracks runs first adds the column and the other
    two no-op. A missing table is also a no-op, matching the sibling
    ``settingoverride`` guards.
    """
    bind = _locked_bind()
    if bind is None:
        return
    if column_exists(bind, _TABLE, SETTINGOVERRIDE_UPDATED_BY_COLUMN):
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(SETTINGOVERRIDE_UPDATED_BY_COLUMN, sa.String()))


def downgrade_drop_updated_by() -> None:
    """Drop ``updated_by``, discarding every recorded actor.

    The column is the only store of who last wrote each override, and the
    information cannot be reconstructed from anything else, so this downgrade is
    one-way for that data. The rows and their values survive untouched.
    """
    bind = _locked_bind()
    if bind is None:
        return
    if not column_exists(bind, _TABLE, SETTINGOVERRIDE_UPDATED_BY_COLUMN):
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_column(SETTINGOVERRIDE_UPDATED_BY_COLUMN)


def upgrade_encrypt_secret_override_values(
    settings_classes: Iterable[type[BaseModel]],
) -> None:
    """Encrypt every not-yet-encrypted secret leaf stored in ``settingoverride``.

    Idempotent in two directions: a leaf an earlier run already rewrote carries
    the envelope marker and is short-circuited on that, and a row whose
    ``setting_class`` none of ``settings_classes`` owns is left untouched, so a
    track sharing one physical database with another never rewrites the other's
    rows. A leaf encrypted before the envelope shipped carries no marker and is
    short-circuited by the structural check instead.

    :param settings_classes: The settings classes, or frozen coverage
        declarations, this track owns.
    """
    bind = _locked_bind()
    if bind is None:
        return
    _rewrite_secret_leaves(bind, settings_classes, reencrypt_secret_leaves)


def downgrade_decrypt_secret_override_values(
    settings_classes: Iterable[type[BaseModel]],
) -> None:
    """Restore every encrypted secret leaf to the plaintext the previous code reads.

    A row the configured ``ENCRYPTION_KEY`` cannot decrypt is logged and left as
    it stands rather than aborting: it was already unreadable before the
    downgrade, and refusing to complete would block the rollback the operator is
    performing.

    :param settings_classes: The settings classes, or frozen coverage
        declarations, this track owns.
    """
    bind = _locked_bind()
    if bind is None:
        return
    _rewrite_secret_leaves(bind, settings_classes, decrypt_secret_leaves)


def upgrade_encrypt_credential_url_override_values(
    settings_classes: Iterable[type[BaseModel]],
) -> None:
    """Encrypt every not-yet-encrypted credential-URL password in ``settingoverride``.

    Idempotent in the same two directions as
    :func:`upgrade_encrypt_secret_override_values`, and narrower: a
    :class:`~pydantic.SecretStr` leaf an earlier revision encrypted is left
    untouched, so this revision and its downgrade are exact inverses of each
    other.

    Only the userinfo password is rewritten, so the endpoint an operator reads
    out of a raw dump stays legible.

    :param settings_classes: The settings classes, or frozen coverage
        declarations, this track owns.
    """
    bind = _locked_bind()
    if bind is None:
        return
    _rewrite_secret_leaves(bind, settings_classes, reencrypt_credential_url_leaves)


def downgrade_decrypt_credential_url_override_values(
    settings_classes: Iterable[type[BaseModel]],
) -> None:
    """Restore every encrypted credential-URL password to plaintext.

    Scoped deliberately: the broad
    :func:`downgrade_decrypt_secret_override_values` would also decrypt the
    :class:`~pydantic.SecretStr` leaves an earlier revision encrypted, and
    Alembic will not re-run that revision to put them back — so those leaves
    would stay in the clear while the release being rolled back to still reads
    them as ciphertext.

    :param settings_classes: The settings classes, or frozen coverage
        declarations, this track owns.
    """
    bind = _locked_bind()
    if bind is None:
        return
    _rewrite_secret_leaves(bind, settings_classes, decrypt_credential_url_leaves)


def downgrade_unmark_secret_override_values(
    settings_classes: Iterable[type[BaseModel]],
) -> None:
    """Strip the envelope marker from every stored secret leaf this track owns.

    The rollback half of the ciphertext envelope. Its upgrade partner is a
    no-op, because the envelope ships with the code rather than with the schema:
    there is no forward work to do, and re-marking existing rows is not
    something a migration can decide (see
    :func:`~app.core.settings_override.secret_storage.unmark_secret_leaves`).

    Needs no ``ENCRYPTION_KEY`` and never decrypts, so unlike
    :func:`downgrade_decrypt_secret_override_values` it cannot fail on a row
    encrypted under a key this process does not hold — that row is unmarked and
    left encrypted, which is exactly what the older release expects.

    :param settings_classes: The settings classes this track can resolve.
    """
    bind = _locked_bind()
    if bind is None:
        return
    _rewrite_secret_leaves(bind, settings_classes, unmark_secret_leaves)


def rename_ciphertext_marker(old: str, new: str) -> None:
    """Rewrite the envelope marker of every stored ciphertext from ``old`` to ``new``.

    Only a structurally valid envelope moves: ``old`` followed by a well-formed
    Fernet token, occupying either a whole string leaf or a credential URL's
    password segment. Any other occurrence of ``old`` is plaintext that happens
    to contain it and is kept byte-identical. Every row is visited whatever its
    ``setting_class``: the marker names the envelope, not a settings class, so a
    track sharing one physical database with another rewrites the other's rows
    too, and the rewrite is idempotent across the tracks that repeat it. Needs
    no ``ENCRYPTION_KEY`` and never decrypts.

    :param old: The marker the stored leaves carry.
    :param new: The marker to store instead.
    """
    bind = _locked_bind()
    if bind is None:
        return
    table = _settingoverride_value_table()
    rows = bind.execute(sa.select(table.c.id, table.c.value)).all()
    rewritten = 0
    for row in rows:
        value = _rename_marker_in_leaves(row.value, old, new)
        if value == row.value:
            continue
        bind.execute(table.update().where(table.c.id == row.id).values(value=value))
        rewritten += 1
    logger.info(
        "Moved %s settingoverride row(s) from the %r marker to %r.",
        rewritten,
        old,
        new,
    )


def _rename_marker_in_leaves(value: Any, old: str, new: str) -> Any:
    """Return ``value`` with every ``old`` envelope it holds moved to ``new``.

    :param value: A decoded JSON value.
    :param old: The marker the stored envelopes carry.
    :param new: The marker to store instead.
    :return: The value with every string leaf rewritten, in the same shape.
    """
    if isinstance(value, str):
        return _rename_marker_in_leaf(value, old, new)
    if isinstance(value, list):
        return [_rename_marker_in_leaves(item, old, new) for item in value]
    if isinstance(value, dict):
        return {
            key: _rename_marker_in_leaves(item, old, new) for key, item in value.items()
        }
    return value


def _rename_marker_in_leaf(leaf: str, old: str, new: str) -> str:
    """Return ``leaf`` with its ``old`` envelope moved to ``new``, or unchanged.

    The envelope is recognised as the whole leaf first and as a credential URL's
    password segment second. A leaf that cannot be parsed as a URL carries no
    password to rewrite, so it is returned unchanged rather than aborting the
    migration.

    :param leaf: The stored string leaf.
    :param old: The marker the stored envelopes carry.
    :param new: The marker to store instead.
    :return: The rewritten leaf, or ``leaf`` when it holds no ``old`` envelope.
    """
    renamed = _renamed_envelope(leaf, old, new)
    if renamed is not None:
        return renamed
    try:
        password = credential_url_password(leaf)
    except ValueError:
        return leaf
    if password is None:
        return leaf
    renamed_password = _renamed_envelope(password, old, new)
    if renamed_password is None:
        return leaf
    return map_credential_url_password(leaf, lambda _segment: renamed_password)


def _renamed_envelope(value: str, old: str, new: str) -> str | None:
    """Return ``value`` under the ``new`` marker when it is an ``old`` envelope.

    :param value: A whole leaf or a URL password segment.
    :param old: The marker the stored envelopes carry.
    :param new: The marker to store instead.
    :return: ``new`` followed by the token, or ``None`` when ``value`` is not
        ``old`` followed by a well-formed Fernet token.
    """
    if not value.startswith(old):
        return None
    token = value.removeprefix(old)
    return f"{new}{token}" if is_encrypted(token) else None


def _settingoverride_value_table() -> sa.TableClause:
    """Return a lightweight ``settingoverride`` table carrying the JSON value type.

    Declaring ``value`` as :class:`AutoJSON` is what makes the rewrite
    dialect-correct: the column resolves to ``JSONB`` on PostgreSQL and ``JSON``
    elsewhere, so the walker sees decoded JSON rather than a dialect-dependent
    raw string.

    :return: The table clause the rewrite selects from and updates.
    """
    return sa.table(
        _TABLE,
        sa.column("id", sa.Integer),
        sa.column(_COLUMN, sa.String),
        sa.column("key", sa.String),
        sa.column("value", AutoJSON),
    )


def _rewrite_secret_leaves(
    bind: Connection,
    settings_classes: Iterable[type[BaseModel]],
    rewrite: Callable[[type[BaseModel], str, Any], Any],
) -> None:
    """Apply ``rewrite`` to every resolvable row's value, updating only what changed.

    :param bind: The migration's bound connection.
    :param settings_classes: The settings classes, or frozen coverage
        declarations, this track owns.
    :param rewrite: The per-row transformation, taking the coverage declaration
        owning the row, the row key and the stored value.
    """
    classes_by_token = {
        setting_class_token(settings_cls): settings_cls
        for settings_cls in settings_classes
    }
    table = _settingoverride_value_table()
    rows = bind.execute(
        sa.select(table.c.id, table.c[_COLUMN], table.c.key, table.c.value)
    ).all()
    rewritten = 0
    unresolved = 0
    undecryptable = 0
    for row in rows:
        settings_cls = classes_by_token.get(getattr(row, _COLUMN))
        if settings_cls is None:
            unresolved += 1
            continue
        try:
            value = rewrite(settings_cls, row.key, row.value)
        except DecryptionError as exc:
            # Only reachable on the downgrade: the encrypt and unmark
            # directions both decide from the stored shape and never attempt a
            # decrypt.
            undecryptable += 1
            logger.warning(
                "Left %s.%s as it stands, it could not be decrypted: %s",
                getattr(row, _COLUMN),
                row.key,
                exc,
            )
            continue
        if value == row.value:
            continue
        bind.execute(table.update().where(table.c.id == row.id).values(value=value))
        rewritten += 1
    logger.info(
        "Rewrote %s settingoverride row(s); left %s untouched for a setting_class "
        "this track cannot resolve and %s that could not be decrypted.",
        rewritten,
        unresolved,
        undecryptable,
    )
