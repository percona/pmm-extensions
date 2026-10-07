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

"""Encrypt and decrypt the credential-bearing leaves of a stored override value.

Override rows persist through a JSON column, and
:func:`~app.core.settings_override.registry.unwrap_secrets_for_storage` strips
the :class:`~pydantic.SecretStr` wrapper before the value is written, so the
credential would otherwise reach the database in the clear. The walker here maps
a settings field's *annotation* onto the stored JSON positionally and transforms
the leaves that carry a credential.

That annotation comes from one of two sources. The read and write paths pass the
live settings class, so they always track the current field set. A re-encryption
data migration passes a frozen replica model declaring the shapes that revision
was authored to cover, so its reach cannot drift with a later rename. Both are
plain :class:`~pydantic.BaseModel` subclasses as far as this module is
concerned, and nothing below distinguishes them.

Two leaf kinds qualify, and they differ in how much of the leaf is rewritten. A
**Pydantic secret** leaf is the credential, so the whole value is transformed. A
**credential-bearing URL** leaf — recognized from the ``WrapSerializer`` marker
:data:`~app.core.utils.fields.CredentialHttpUrl` and its siblings carry, not
from any secret type — merely embeds one in its userinfo segment, so only that
password is transformed and the endpoint stays readable in a raw database dump.

Which leaves carry a credential is decided **only** from the annotation, never
from the stored JSON's shape or content. A materializer-backed field stores the
client's raw payload rather than the coerced model, and is handled on the same
terms as a plain one because of that. The annotation is always resolved through
:func:`~app.core.settings_override.registry.annotated_type`, because Pydantic
hoists a non-``Optional`` field's ``Annotated`` metadata onto ``FieldInfo`` and
the URL marker lives in exactly that metadata.

Where two candidate models at one JSON position declare a field of the same
name and only one types it as credential-bearing, that annotation wins, so the
value is encrypted rather than stored in the clear. No such collision exists in
``app/`` today.

The three broad entry points cover both kinds, which the read and write paths
need. The two ``*_credential_url_leaves`` entry points cover only the URL kind,
so the data migrations that call them are exact inverses of each other and never
rewrite a secret an earlier revision already owns.

Every leaf written here carries the versioned envelope marker
:func:`~app.core.encryption.mark_ciphertext` applies, so "is this already
encrypted?" is answered from the stored value's own format. The structural
:func:`~app.core.encryption.is_encrypted` check survives only as the fallback
for rows written before the envelope shipped: nothing re-marks those, because
deciding an unmarked row is ciphertext is exactly the guess the envelope exists
to replace. :func:`unmark_secret_leaves` strips the marker for a rollback, which
is why the envelope can be removed without the key.
"""

from __future__ import annotations

__all__ = [
    "decrypt_credential_url_leaves",
    "decrypt_secret_leaves",
    "encrypt_secret_leaves",
    "reencrypt_credential_url_leaves",
    "reencrypt_secret_leaves",
    "unmark_secret_leaves",
]

import logging
import typing
from collections.abc import Callable, Collection, Mapping
from enum import Enum
from types import UnionType
from typing import Any, Union

from pydantic import AnyUrl, BaseModel, SecretBytes, SecretStr
from pydantic_core import Url

from app.core.encryption import (
    decrypt,
    encrypt,
    is_encrypted,
    is_stored_ciphertext,
    mark_ciphertext,
    marked_ciphertext,
)
from app.core.settings_override.registry import (
    annotated_type,
    annotation_contains_credential_url,
    annotation_contains_secret,
    annotation_is_credential_url,
)
from app.core.settings_override.resolution import resolve_nested_field
from app.core.utils.fields import (
    credential_url_password,
    map_credential_url_password,
)

logger = logging.getLogger(__name__)

_SECRET_TYPES = (SecretStr, SecretBytes)


def encrypt_secret_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with every secret-typed leaf encrypted.

    Encrypts unconditionally, which is what the write path needs: the values
    reaching it are freshly coerced from the request body, and the one that is
    reused rather than submitted (a secret restored because the client sent the
    redaction mask back) is read from the decrypted snapshot. Deciding
    structurally whether a leaf "looks encrypted" would misread a credential
    that happens to be base64 as ciphertext and store it in the clear.

    Every leaf it writes carries the envelope marker, so a later reader
    recognises what this stored from the value's own format rather than guessing
    it from the bytes.

    Covers both leaf kinds: a Pydantic secret leaf is encrypted whole, and a
    credential-bearing URL has only its userinfo password encrypted.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The JSON-storable value about to be persisted.
    :return: A value of the same shape with its credential leaves encrypted.
    """
    return _rewrite_leaves(settings_cls, key, value, _encrypt_leaf, _ALL_LEAF_KINDS)


def reencrypt_secret_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with every not-yet-encrypted secret leaf encrypted.

    The idempotent variant the re-encryption migrations need, where the column
    genuinely may already hold ciphertext. A leaf already holding ciphertext is
    left byte-identical, so a second run rewrites nothing and ciphertext written
    under a different ``ENCRYPTION_KEY`` is never re-encrypted, which would
    destroy the only copy of its plaintext.

    The envelope marker decides first, so a leaf this module wrote is recognised
    from its own format. :func:`~app.core.encryption.is_encrypted` survives as
    the *fallback* for rows written before the envelope shipped, where nothing
    but the bytes is available to decide on. That fallback is structural, so a
    legacy plaintext secret that is itself a well-formed Fernet token is skipped
    and stays in the clear. Attempting a decrypt to tell the two apart is not an
    option: a failure there cannot separate that case from the foreign-key one,
    and guessing wrong on the second destroys data. Only the write path is free
    of the ambiguity, and it uses :func:`encrypt_secret_leaves`.

    Covers both leaf kinds. For a credential-bearing URL the idempotence check
    runs on the *password*, not the leaf: neither discriminator accepts a whole
    ``https://user:<token>@host/`` string — it is not a Fernet token and carries
    no marker of its own — so testing the leaf would re-encrypt an
    already-encrypted password on every run and destroy the plaintext.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The stored value being rewritten in place.
    :return: A value of the same shape with its plaintext credentials encrypted.
    """
    return _rewrite_leaves(settings_cls, key, value, _reencrypt_leaf, _ALL_LEAF_KINDS)


def decrypt_secret_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with every secret-typed leaf decrypted.

    A leaf that is not ciphertext is passed through unchanged, so a row written
    before its track's re-encryption migration ran keeps resolving.

    Covers both leaf kinds, which is what the read path needs: narrowing it
    would hand the snapshot a still-encrypted value for whichever kind it
    dropped.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The JSON value read out of the override row.
    :return: A value of the same shape with its credential leaves in plaintext.
    :raises DecryptionError: If a leaf is ciphertext the configured
        ``ENCRYPTION_KEY`` cannot decrypt.
    """
    return _rewrite_leaves(settings_cls, key, value, _decrypt_leaf, _ALL_LEAF_KINDS)


def reencrypt_credential_url_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with every not-yet-encrypted credential-URL password encrypted.

    The credential-URL-scoped counterpart of :func:`reencrypt_secret_leaves`.
    Scoped rather than broad so its downgrade partner can be an exact inverse: a
    :class:`~pydantic.SecretStr` leaf an earlier revision encrypted is not this
    revision's to rewrite in either direction.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The stored value being rewritten in place.
    :return: A value of the same shape with its plaintext URL passwords encrypted.
    """
    return _rewrite_leaves(
        settings_cls, key, value, _reencrypt_leaf, _CREDENTIAL_URL_ONLY
    )


def decrypt_credential_url_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with every encrypted credential-URL password decrypted.

    Leaves :class:`~pydantic.SecretStr` / :class:`~pydantic.SecretBytes`
    ciphertext byte-identical, including a sibling leaf inside the same stored
    object, so rolling this revision back does not undo the one before it —
    which Alembic would never re-run to put back.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The stored value being rewritten in place.
    :return: A value of the same shape with its URL passwords in plaintext.
    :raises DecryptionError: If a password is ciphertext the configured
        ``ENCRYPTION_KEY`` cannot decrypt.
    """
    return _rewrite_leaves(
        settings_cls, key, value, _decrypt_leaf, _CREDENTIAL_URL_ONLY
    )


def unmark_secret_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
) -> Any:
    """Return ``value`` with the envelope marker stripped from every leaf.

    The rollback transform, and the reason it can exist at all: stripping the
    marker is a string operation over the stored text, so it leaves a bare
    Fernet token a release predating the envelope reads correctly — without
    holding ``ENCRYPTION_KEY``, without a decrypt, and without the plaintext
    ever being written back to the column.

    Covers both leaf kinds, because :func:`encrypt_secret_leaves` marks both. A
    leaf carrying no marker is returned byte-identical, so this is idempotent
    and a row written before the envelope is never touched.

    There is deliberately no ``mark_secret_leaves`` counterpart. Marking an
    existing row requires deciding it is ciphertext, which for an unmarked row
    is exactly the structural guess the envelope exists to stop trusting, and
    marking a legacy collision plaintext would freeze that misclassification
    permanently.

    :param settings_cls: The settings class owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The stored value being rewritten in place.
    :return: A value of the same shape with its leaves unmarked.
    """
    return _rewrite_leaves(settings_cls, key, value, _unmark_leaf, _ALL_LEAF_KINDS)


def _rewrite_leaves(
    settings_cls: type[BaseModel],
    key: str,
    value: Any,
    transform: Callable[[str], str],
    kinds: frozenset[_LeafKind],
) -> Any:
    """Return ``value`` with ``transform`` applied to every leaf of ``kinds``.

    The shared body of the public entry points above. Each resolves the same
    annotation for ``key`` and labels the walk with the same ``<class>.<key>``
    context, differing only in the transform it applies and the leaf kinds it
    selects — so the resolution and the label are settled here rather than
    restated per entry point, where one copy could drift.

    :param settings_cls: The settings class owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :param value: The stored value being rewritten.
    :param transform: The leaf transformation to apply.
    :param kinds: The leaf kinds this walk transforms.
    :return: A value of the same shape with its selected leaves transformed.
    """
    return _transform_leaves(
        _annotation_for_key(settings_cls, key),
        value,
        transform,
        kinds=kinds,
        context=f"{settings_cls.__name__}.{key}",
    )


def _encrypt_leaf(leaf: str) -> str:
    """Return ``leaf`` encrypted and marked.

    :param leaf: The plaintext credential.
    :return: Marked ciphertext.
    """
    return mark_ciphertext(encrypt(leaf))


def _reencrypt_leaf(leaf: str) -> str:
    """Return ``leaf`` encrypted and marked unless it already holds ciphertext.

    The envelope answers first: :func:`~app.core.encryption.marked_ciphertext`
    accepts only a value carrying the marker over a well-formed payload, so a
    leaf this module wrote is always recognised and a legacy plaintext that
    merely starts with the marker is not. Anything it declines falls to the bare
    structural check, the discriminator for rows written before the envelope
    shipped — including a legacy plaintext that satisfies it by accident, an
    ambiguity this cannot resolve and does not make worse.

    :param leaf: The stored leaf being rewritten.
    :return: The leaf unchanged, or marked ciphertext.
    """
    if is_stored_ciphertext(leaf):
        return leaf
    return _encrypt_leaf(leaf)


def _decrypt_leaf(leaf: str) -> str:
    """Return the plaintext behind ``leaf``, or ``leaf`` when it is not ciphertext.

    A value carrying the marker over a *damaged* payload is returned as-is
    rather than raised on, and that is a deliberate choice with a cost worth
    naming: the marker is evidence the writer stored ciphertext there, so
    passing it through hands the caller an ``extensions.enc.v1.``-prefixed string as if
    it were the credential. Raising instead would drop the row with a warning,
    which :mod:`app.core.settings_override.cache` already handles.

    Passing through is kept because it matches the unmarked path exactly — a
    corrupt bare token is returned untouched too — so no stored value changes
    behaviour on the day the envelope ships, which is the property the whole
    change is built around. Nothing in this module can produce the shape: it
    needs a value that was marked and then damaged in the column.

    :param leaf: The stored leaf being read.
    :return: The plaintext, or the leaf unchanged.
    :raises DecryptionError: If the leaf is ciphertext the configured
        ``ENCRYPTION_KEY`` cannot decrypt.
    """
    token = marked_ciphertext(leaf)
    if token is not None:
        return decrypt(token)
    return decrypt(leaf) if is_encrypted(leaf) else leaf


def _unmark_leaf(leaf: str) -> str:
    """Return ``leaf`` with the envelope marker removed, or unchanged.

    :param leaf: The stored leaf being rewritten.
    :return: The bare token, or the leaf unchanged.
    """
    token = marked_ciphertext(leaf)
    return token if token is not None else leaf


def _annotation_for_key(settings_cls: type[BaseModel], key: str) -> Any:
    """Return the annotation of the field ``key`` overrides, or ``None``.

    Resolved through :func:`~app.core.settings_override.registry.annotated_type`
    rather than read off ``.annotation``: Pydantic hoists a non-``Optional``
    field's ``Annotated`` metadata onto ``FieldInfo``, and the credential-URL
    marker lives in exactly that metadata.

    :param settings_cls: The settings class, or a migration's frozen replica of
        one, owning ``key``.
    :param key: The override row's key, ``__``-delimited for a nested leaf.
    :return: The leaf annotation, or ``None`` when ``key`` resolves to no field.
    """
    if "__" in key:
        resolved = resolve_nested_field(settings_cls, key)
        return None if resolved is None else annotated_type(resolved[1])
    field_info = settings_cls.model_fields.get(key)
    return None if field_info is None else annotated_type(field_info)


def _positional_args(annotation: Any) -> list[Any]:
    """Return the types a value at this JSON position may take, in declaration order.

    Strips :data:`~typing.Annotated` wrappers, flattens unions and drops
    ``NoneType``. Unlike the recursive walk behind
    :func:`~app.core.settings_override.registry.annotation_contains_secret`
    this does **not** descend through a model into its fields: a value at one
    position cannot be an arbitrary attribute of a model reachable from it.

    Order is part of the contract rather than an accident of the traversal:
    :func:`_first_credential_bearing` resolves a contested position by taking
    the first candidate that reaches a transformable leaf, so a union's members
    have to arrive in the order the annotation declares them. Pushing them
    reversed onto a LIFO stack is what preserves that.

    :param annotation: The annotation to flatten.
    :return: The candidate types for this position, in declaration order.
    """
    flattened: list[Any] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if current is type(None):
            continue
        if hasattr(current, "__metadata__"):
            stack.append(typing.get_args(current)[0])
            continue
        if typing.get_origin(current) in {Union, UnionType}:
            stack.extend(reversed(typing.get_args(current)))
            continue
        flattened.append(current)
    return flattened


def _is_secret_position(positional: list[Any]) -> bool:
    """Return whether the value at this position is itself a secret.

    :param positional: The candidate types for one JSON position.
    :return: ``True`` when one of them is a Pydantic secret type.
    """
    return any(
        isinstance(arg, type) and issubclass(arg, _SECRET_TYPES) for arg in positional
    )


class _LeafKind(Enum):
    """Name the leaf kinds the walker knows how to transform.

    Both kinds carry a credential; they differ in how much of the leaf is one.
    A ``PYDANTIC_SECRET`` leaf — one reaching :class:`~pydantic.SecretStr` or
    :class:`~pydantic.SecretBytes` — *is* the credential, so the whole value is
    transformed. A ``CREDENTIAL_URL`` leaf merely embeds one in its userinfo
    segment, so only that password is, leaving the endpoint readable in a raw
    database dump.
    """

    PYDANTIC_SECRET = 1
    CREDENTIAL_URL = 2


#: Every leaf kind, which the read and write paths must both cover.
_ALL_LEAF_KINDS = frozenset(_LeafKind)

#: The credential-URL leaf alone, so a migration scoped to it is an exact
#: inverse of itself and never rewrites a secret an earlier revision owns.
_CREDENTIAL_URL_ONLY = frozenset({_LeafKind.CREDENTIAL_URL})


def _kinds_reachable(annotation: Any, kinds: frozenset[_LeafKind]) -> bool:
    """Return whether any selected leaf kind is reachable from ``annotation``.

    :param annotation: The annotation to inspect.
    :param kinds: The leaf kinds this walk transforms.
    :return: ``True`` when the walk can still find something to rewrite below.
    """
    return (
        _LeafKind.PYDANTIC_SECRET in kinds and annotation_contains_secret(annotation)
    ) or (
        _LeafKind.CREDENTIAL_URL in kinds
        and annotation_contains_credential_url(annotation)
    )


def _first_credential_bearing(
    candidates: list[Any], kinds: frozenset[_LeafKind]
) -> Any:
    """Return the first candidate reaching a selected kind, or ``None`` when none does.

    Two annotations can compete for one JSON position: a union of container
    types, or the same field name declared by two candidate models. Preferring
    the credential-bearing one keeps the walker conservative, so a value that
    may be a credential is encrypted rather than stored in the clear.

    Returning ``None`` rather than an arbitrary survivor is what makes the
    container branches fall through to the model branch: ``dict[str, str] |
    Inner`` would otherwise resolve to the mapping's plain ``str`` values and
    never look inside ``Inner`` for its secret.

    ``kinds`` is honoured here as well as at the leaf, so a narrowed walk never
    resolves a contested position to a candidate it would then decline to
    transform.

    :param candidates: The competing annotations, in declaration order.
    :param kinds: The leaf kinds this walk transforms.
    :return: The first credential-bearing annotation, or ``None``.
    """
    return next(
        (candidate for candidate in candidates if _kinds_reachable(candidate, kinds)),
        None,
    )


def _is_mapping_origin(origin: Any) -> bool:
    """Return whether ``origin`` is a mapping type.

    Tested by subclass rather than against a list of names so every mapping the
    codebase can annotate is covered. ``defaultdict`` and ``OrderedDict`` are
    both live override-field annotations today.

    :param origin: The generic origin to classify.
    :return: ``True`` for a mapping origin.
    """
    return isinstance(origin, type) and issubclass(origin, Mapping)


def _is_collection_origin(origin: Any) -> bool:
    """Return whether ``origin`` is a non-mapping, non-string collection type.

    :param origin: The generic origin to classify.
    :return: ``True`` for a list/set/tuple-like origin.
    """
    return (
        isinstance(origin, type)
        and issubclass(origin, Collection)
        and not issubclass(origin, Mapping | str | bytes)
    )


def _mapping_value_annotation(
    positional: list[Any], kinds: frozenset[_LeafKind]
) -> Any:
    """Return the annotation of a mapping's values at this position, or ``None``.

    :param positional: The candidate types for one JSON position.
    :param kinds: The leaf kinds this walk transforms.
    :return: The value annotation of a credential-bearing mapping candidate.
    """
    candidates = [
        typing.get_args(arg)[-1]
        for arg in positional
        if _is_mapping_origin(typing.get_origin(arg)) and len(typing.get_args(arg)) > 1
    ]
    return _first_credential_bearing(candidates, kinds)


def _element_annotation(
    positional: list[Any], index: int, kinds: frozenset[_LeafKind]
) -> Any:
    """Return the annotation of the item at ``index``, or ``None``.

    A fixed-length ``tuple`` annotates each slot separately, so its items are
    resolved per index; every other collection, and a variadic
    ``tuple[X, ...]``, annotates all items with one type.

    :param positional: The candidate types for the enclosing JSON position.
    :param index: The item's position within the stored array.
    :param kinds: The leaf kinds this walk transforms.
    :return: The element annotation of a credential-bearing collection candidate.
    """
    candidates: list[Any] = []
    for arg in positional:
        origin = typing.get_origin(arg)
        args = typing.get_args(arg)
        if not (_is_collection_origin(origin) and args):
            continue
        if origin is tuple and Ellipsis not in args:
            if index < len(args):
                candidates.append(args[index])
            continue
        candidates.append(args[0])
    return _first_credential_bearing(candidates, kinds)


def _candidate_models(positional: list[Any]) -> list[type[BaseModel]]:
    """Return every model a value at this position may validate as.

    Concrete subclasses are included recursively, so a polymorphic collection
    annotated with its base (``set[BaseAlertProvider]``) reaches the subclass
    that actually declares the secret field. Only subclasses Python has already
    imported are visible.

    :param positional: The candidate types for one JSON position.
    :return: The reachable model classes, most general first.
    """
    models: list[type[BaseModel]] = []
    seen: set[int] = set()
    queue = list(positional)
    while queue:
        current = queue.pop(0)
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            continue
        if id(current) in seen:
            continue
        seen.add(id(current))
        models.append(current)
        queue.extend(current.__subclasses__())
    return models


def _field_annotation(
    models: list[type[BaseModel]], json_key: str, kinds: frozenset[_LeafKind]
) -> Any:
    """Return the annotation ``json_key`` maps to across ``models``, or ``None``.

    Field names are matched case-folded because stored key casing is
    client-controlled for a materializer-backed field: the provider models are
    ``BaseCaseInsensitiveModel`` subclasses, so ``routing_key`` and
    ``ROUTING_KEY`` are both accepted and whichever the client sent is what was
    persisted.

    Matches are collected through
    :func:`~app.core.settings_override.registry.annotated_type` so a child whose
    ``Annotated`` Pydantic hoisted onto its ``FieldInfo`` still presents its
    markers one level down.

    :param models: The candidate models for the enclosing JSON object.
    :param json_key: The stored key to resolve.
    :param kinds: The leaf kinds this walk transforms.
    :return: The matching field's annotation, or ``None`` when nothing matches.
    """
    folded = json_key.casefold()
    matches = [
        annotated_type(field)
        for model in models
        for name, field in model.model_fields.items()
        if name.casefold() == folded
    ]
    credential_bearing = _first_credential_bearing(matches, kinds)
    if credential_bearing is not None:
        return credential_bearing
    return matches[0] if matches else None


def _credential_url_text(value: Any) -> str | None:
    """Return the URL text a credential-URL leaf holds, or ``None``.

    The write path hands the walker the *validated* value, which for a
    :data:`~app.core.utils.fields.CredentialHttpUrl` field is a
    :class:`~pydantic.HttpUrl` rather than a string; the migration and read
    paths hand it the JSON column's text. Both reach this leaf, so the branch
    normalizes pydantic's single-host URL objects (an :class:`~pydantic.AnyUrl`
    subclass such as :class:`~pydantic.HttpUrl`, or a bare
    :class:`pydantic_core.Url`) rather than testing for
    ``str`` — a ``str``-only guard would
    silently skip every field typed ``CredentialHttpUrl`` on the one path that
    writes them.

    :param value: The stored value at a credential-URL position.
    :return: The URL as text, or ``None`` when the leaf is neither.
    """
    if isinstance(value, str | AnyUrl | Url):
        return str(value)
    return None


def _transform_credential_url(
    value: Any, transform: Callable[[str], str], *, context: str
) -> Any:
    """Return ``value`` with ``transform`` applied to its embedded password.

    A leaf that is neither text nor a URL object, or whose password segment is
    absent or unparseable, is returned unchanged: the walker never partially
    rewrites a URL it could not take apart.

    Leaving an unparseable leaf alone is the right posture *here* and not
    generally. A stored value that cannot be parsed carries no password this
    could have encrypted, and refusing the whole row would abort a migration
    over one malformed endpoint. A caller whose job is to *mask* needs the
    opposite — which is why :func:`~app.core.utils.fields.credential_url_password`
    raises and each caller decides, rather than answering ``None`` for both.

    Only that skip is logged. An endpoint carrying no credential is the ordinary
    shape, and this runs per row on every snapshot refresh, so announcing it
    would be steady-state output with nothing to act on, while a URL that cannot
    be parsed is an anomaly worth reading.

    :param value: The stored value at a credential-URL position.
    :param transform: The password transformation to apply.
    :param context: The ``<class>.<key>`` this leaf belongs to, for the log line.
    :return: The rewritten value, or ``value`` when there is nothing to rewrite.
    """
    text = _credential_url_text(value)
    if text is None:
        return value
    try:
        password = credential_url_password(text)
    except ValueError:
        logger.debug(
            "Left the credential-URL leaf of %s unchanged: it could not be parsed.",
            context,
        )
        return value
    if password is None:
        return value
    return map_credential_url_password(text, transform)


def _transform_leaves(
    annotation: Any,
    value: Any,
    transform: Callable[[str], str],
    *,
    kinds: frozenset[_LeafKind],
    context: str,
) -> Any:
    """Return ``value`` with ``transform`` applied to every selected leaf.

    Returns new containers and never mutates ``value``. A subtree whose
    annotation reaches no selected kind is returned by identity.

    The credential-URL branch is tested **before** :func:`_is_secret_position`
    because the two are mutually exclusive by construction — no annotation is
    both a Pydantic secret and a credential-URL-serialized type — and because
    the position predicate needs the un-stripped annotation, which
    :func:`_positional_args` consumes.

    :param annotation: The annotation of the JSON position ``value`` occupies,
        or ``None`` when the position could not be resolved.
    :param value: The stored value at that position.
    :param transform: The leaf transformation to apply.
    :param kinds: The leaf kinds this walk transforms.
    :param context: The ``<class>.<key>`` being walked, for log lines.
    :return: The transformed value.
    """
    if annotation is None or not _kinds_reachable(annotation, kinds):
        return value
    if _LeafKind.CREDENTIAL_URL in kinds and annotation_is_credential_url(annotation):
        return _transform_credential_url(value, transform, context=context)
    positional = _positional_args(annotation)
    if _LeafKind.PYDANTIC_SECRET in kinds and _is_secret_position(positional):
        return transform(value) if isinstance(value, str) else value
    if isinstance(value, Mapping):
        return _transform_mapping(
            positional, value, transform, kinds=kinds, context=context
        )
    if isinstance(value, list | tuple):
        return [
            _transform_leaves(
                _element_annotation(positional, index, kinds),
                item,
                transform,
                kinds=kinds,
                context=context,
            )
            for index, item in enumerate(value)
        ]
    return value


def _transform_mapping(
    positional: list[Any],
    value: Mapping[str, Any],
    transform: Callable[[str], str],
    *,
    kinds: frozenset[_LeafKind],
    context: str,
) -> Any:
    """Return ``value`` transformed as a credential-valued mapping or a model dump.

    :param positional: The candidate types for the mapping's JSON position.
    :param value: The stored mapping.
    :param transform: The leaf transformation to apply.
    :param kinds: The leaf kinds this walk transforms.
    :param context: The ``<class>.<key>`` being walked, for log lines.
    :return: A new mapping, or ``value`` when the position resolves to neither.
    """
    mapping_value = _mapping_value_annotation(positional, kinds)
    if mapping_value is not None:
        return {
            name: _transform_leaves(
                mapping_value, item, transform, kinds=kinds, context=context
            )
            for name, item in value.items()
        }
    models = _candidate_models(positional)
    if not models:
        return value
    return {
        name: _transform_leaves(
            _field_annotation(models, name, kinds),
            item,
            transform,
            kinds=kinds,
            context=context,
        )
        for name, item in value.items()
    }
