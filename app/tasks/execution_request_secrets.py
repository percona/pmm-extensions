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

"""Encrypt and decrypt the credential-bearing leaves of a stored execution request.

Three leaves of the ``taskhistory.execution_request`` document carry credentials
verbatim: ``meta.args`` holds the joined command line an operator submitted,
``meta.config`` the serialised configuration an app rendered from its form, and
``payload`` a submitted document or the ``file://`` reference standing in for
one. Which leaves those are is fixed here by name and never inferred from a
value's shape or content, so a credential that happens to look like something
else is protected on the same terms as any other.

The inventory is the only place they are named. A ``meta`` leaf added to it
becomes ciphertext in the column while every caller still holds a value, so any
SQL predicate over that key has to skip it or compare a value against a token
and silently never match; :data:`ENCRYPTED_META_KEYS` exists so that skip
follows the inventory rather than being restated beside it.

Each leaf is stored as ``encrypt(json.dumps(value))``. The JSON wrapper is what
lets ``meta.args`` stay the ``Any``-typed value the API accepts, so a ``str``,
``list``, ``dict`` or ``int`` all round-trip to the type they were written with,
while :func:`~app.core.encryption.encrypt` still receives the ``str`` it
requires. A leaf that is absent or ``None`` is left alone: there is no value to
protect, and encrypting it would replace a stored JSON ``null`` with a token, so
every reader that distinguishes "no payload" from "a payload" would stop being
able to.

Deciding *whether* to encrypt splits by who supplied the document, which is why
the write path and the migration are separate functions rather than one with a
flag. :func:`encrypt_request_leaves` receives freshly-submitted values and
encrypts unconditionally; :func:`reencrypt_request_leaves` receives stored rows
and skips what :func:`~app.core.encryption.is_encrypted` already accepts.
"""

__all__ = [
    "ARGS_LEAF",
    "ARGS_META_KEY",
    "CONFIG_LEAF",
    "CONFIG_META_KEY",
    "DISPLAY_ONLY_META_KEYS",
    "ENCRYPTED_LEAVES",
    "ENCRYPTED_META_KEYS",
    "MASKED_ARGS_META_KEY",
    "PAYLOAD_LEAF",
    "REDACTED_LEAF",
    "TASKS_CLIENT_LOG_POLICY",
    "WITHHELD_ARGS_PLACEHOLDER",
    "decrypt_request_leaves",
    "encrypt_request_leaves",
    "publish_masked_args",
    "redact_request_leaves",
    "redact_request_leaves_for_log",
    "reencrypt_request_leaves",
]

import json
import logging
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Any, Final

from app.core.encryption import decrypt, DecryptionError, encrypt, is_encrypted

logger = logging.getLogger(__name__)

#: ``meta`` key holding the submitted command line.
ARGS_META_KEY: Final = "args"

#: ``meta`` key holding the serialised task configuration an app rendered from
#: its form. Credential-bearing for the same reason ``args`` is: a form field
#: typed as a plain string, such as a replication password, serialises into it
#: verbatim.
CONFIG_META_KEY: Final = "config"

#: Dotted path of the leaf holding the submitted command line.
ARGS_LEAF: Final = f"meta.{ARGS_META_KEY}"

#: Dotted path of the leaf holding the serialised task configuration.
CONFIG_LEAF: Final = f"meta.{CONFIG_META_KEY}"

#: Dotted path of the leaf holding the submitted payload or its reference.
PAYLOAD_LEAF: Final = "payload"

#: Dotted paths of the execution-request leaves stored encrypted at rest.
ENCRYPTED_LEAVES: Final = (ARGS_LEAF, CONFIG_LEAF, PAYLOAD_LEAF)

#: The top-level ``meta`` keys the inventory above encrypts, derived from it
#: rather than listed again. Every SQL predicate over a ``meta`` key has to skip
#: these, because the column holds ciphertext where the caller holds a value, and
#: a comparison between them is silently never equal. Deriving the set is what
#: keeps a leaf added above from leaving such a predicate behind.
#:
#: The *top-level* key is the right unit even for a leaf nested deeper than
#: ``meta.<key>``, because the predicates iterate the caller's ``meta`` by its
#: own keys and compare each value whole. A container holding an encrypted leaf
#: somewhere inside it never compares equal either, so the whole key has to go.
ENCRYPTED_META_KEYS: Final = tuple(
    leaf.split(".")[1] for leaf in ENCRYPTED_LEAVES if leaf.startswith("meta.")
)

#: ``meta`` key holding the rendering of ``meta.args`` with every credential
#: value masked, recorded by the submitter beside the raw arguments. It is what
#: a response publishes in their place. Not a protected leaf: it holds nothing
#: a response may not show.
MASKED_ARGS_META_KEY: Final = "_masked_args"

#: The ``meta`` keys that carry no run identity: renderings of other leaves kept
#: for display, which two submissions of one run need not agree on. A copy
#: recorded by one release and absent from a row an earlier release wrote is the
#: same run, and the leaf it renders is already compared in its own right, so a
#: predicate deciding whether two requests are the same run skips these.
DISPLAY_ONLY_META_KEYS: Final = (MASKED_ARGS_META_KEY,)

#: What a response shows for ``meta.args`` when a run recorded arguments and no
#: masked copy of them, so nothing can tell which of them are credentials.
WITHHELD_ARGS_PLACEHOLDER: Final = "<withheld>"

#: What a log line shows in place of a protected leaf's value.
REDACTED_LEAF: Final = "<redacted>"

#: The log settings every client of the Tasks API is constructed with. Request
#: bodies carry the protected leaves at these paths, and history responses
#: return ``meta.config`` and ``payload`` in clear, so a client logs neither.
#: One mapping rather than two settings, so a construction site cannot apply
#: one and forget the other.
TASKS_CLIENT_LOG_POLICY: Final[Mapping[str, Any]] = MappingProxyType(
    {"sensitive_body_paths": ENCRYPTED_LEAVES, "withhold_response_log": True}
)


def encrypt_request_leaves(
    document: Mapping[str, Any], *, preserve: Collection[str] = ()
) -> dict[str, Any]:
    """Return ``document`` with every protected leaf encrypted.

    Every present leaf is encrypted unconditionally except those named in
    ``preserve``, which are written back byte-identically. ``preserve`` carries
    the leaves a prior read could not decrypt, so re-saving such a row does not
    encrypt an unreadable token a second time and destroy the only copy of its
    plaintext.

    Deciding that structurally instead, by skipping whatever
    :func:`~app.core.encryption.is_encrypted` accepts, would also exempt a
    *fresh* submission that happens to be Fernet-shaped, storing a real
    credential in the clear. ``payload`` carries an arbitrary caller-supplied
    document, so that is not far-fetched, and the caller is the only party that
    knows which leaves came back unreadable from a read.

    :param document: The dumped execution request about to be persisted.
    :param preserve: Dotted paths of the leaves to write back unchanged.
    :return: A copy of ``document`` with its protected leaves encrypted.
    """
    result, sites = _copy_with_leaf_sites(document)
    preserved = frozenset(preserve)
    for path, container, name in sites:
        if path not in preserved:
            container[name] = encrypt(json.dumps(container[name]))
    return result


def decrypt_request_leaves(
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return ``document`` with its protected leaves decrypted, and what failed.

    A leaf that is not ciphertext is passed through, so a row written before the
    re-encryption migration ran keeps resolving. A leaf this key cannot decrypt,
    or whose plaintext is not the JSON this module writes, is left as the
    stored token, logged, and named in the second element, rather than raised:
    the decrypt runs inside row loading for a column the task-history list
    routes undefer, so raising would fail a whole page over one row.

    That tolerance is deliberately narrow. A malformed ``ENCRYPTION_KEY`` makes
    building the cipher itself fail, and that propagates: reporting it as "every
    leaf of every row is unreadable" would present a configuration error as
    permanent data loss.

    :param document: The execution request read out of the row.
    :return: A copy of ``document`` with its protected leaves in plaintext,
        paired with the dotted paths of the leaves that could not be resolved.
    """
    result, sites = _copy_with_leaf_sites(document)
    unreadable: list[str] = []
    for path, container, name in sites:
        value = container[name]
        if not (isinstance(value, str) and is_encrypted(value)):
            continue
        try:
            container[name] = json.loads(decrypt(value))
        except (DecryptionError, json.JSONDecodeError):
            unreadable.append(path)
    if unreadable:
        # One line per document rather than per leaf: a key rotation makes every
        # row of a task-history page unreadable at once, and the per-leaf form
        # put two lines per row into a single list request.
        logger.warning(
            "Left execution_request %s as they stand, they could not be read "
            "with the configured ENCRYPTION_KEY.",
            ", ".join(unreadable),
        )
    return result, tuple(unreadable)


def reencrypt_request_leaves(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``document`` with every not-yet-encrypted protected leaf encrypted.

    The idempotent variant the re-encryption migration needs, where the column
    genuinely may already hold ciphertext. Structural by necessity and correct
    here: the migration reads stored rows only, so there is no fresh submission
    to misclassify, and a second run rewrites nothing.

    :func:`~app.core.encryption.is_encrypted` is structural, so a legacy
    plaintext leaf that is itself a well-formed Fernet token is skipped and
    stays in the clear. Attempting a decrypt to tell the two apart is not an
    option: a failure there cannot separate that case from ciphertext written
    under a key this process does not hold, and guessing wrong on the second
    destroys data.

    :param document: The stored execution request being rewritten.
    :return: A copy of ``document`` with its plaintext protected leaves encrypted.
    """
    result, sites = _copy_with_leaf_sites(document)
    for _, container, name in sites:
        value = container[name]
        if not (isinstance(value, str) and is_encrypted(value)):
            container[name] = encrypt(json.dumps(value))
    return result


def redact_request_leaves(
    document: Mapping[str, Any], leaves: Collection[str]
) -> dict[str, Any]:
    """Return ``document`` with each leaf named in ``leaves`` blanked to ``None``.

    Serialisation-only: the caller passes an already-dumped document, so the
    object it came from keeps the stored token and a later write still has
    something to preserve.

    :param document: The dumped execution request about to be serialised.
    :param leaves: Dotted paths of the leaves to blank.
    :return: A copy of ``document`` carrying ``None`` at each named leaf.
    """
    named = frozenset(leaves)
    result, sites = _copy_with_leaf_sites(document)
    for path, container, name in sites:
        if path in named:
            container[name] = None
    return result


def publish_masked_args(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``document`` with its raw arguments replaced by what may be shown.

    The masked copy the submitter recorded stands in for ``meta.args``. A run
    that recorded arguments and no usable copy shows a placeholder instead,
    because nothing here can tell which of its arguments are credentials. A
    document holding no arguments, an empty value included, is returned as it
    stands.

    Applying this to its own output changes nothing, which the PMM Extensions
    gateway relies on when it re-validates a response the Tasks service already
    published.

    :param document: The dumped execution request about to be serialised.
    :return: A copy of ``document`` whose ``meta.args`` carries no raw value.
    """
    result, sites = _copy_with_leaf_sites(document)
    for path, container, name in sites:
        if path == ARGS_LEAF and container[name]:
            masked = container.get(MASKED_ARGS_META_KEY)
            container[name] = (
                masked
                if isinstance(masked, str) and masked
                else WITHHELD_ARGS_PLACEHOLDER
            )
    return result


def redact_request_leaves_for_log(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``document`` with every present protected leaf redacted.

    :param document: The execution request, or the part of one, about to be
        rendered into a log line.
    :return: A copy of ``document`` carrying a fixed marker at each protected
        leaf that held a value.
    """
    result, sites = _copy_with_leaf_sites(document)
    for _, container, name in sites:
        container[name] = REDACTED_LEAF
    return result


def _copy_with_leaf_sites(
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any], str]]]:
    """Return a copy of ``document`` and the mutable site of each present leaf.

    Every container on a protected leaf's path is copied, so a caller mutating
    the returned document cannot reach the one it passed in. A leaf whose parent
    is missing or is not a mapping, and one whose own value is absent or
    ``None``, yields no site and is therefore left exactly as it stands.

    Each container is copied **once**, however many protected leaves sit under
    it. Copying per leaf instead would leave every site but the last pointing at
    a superseded copy, so those writes would land on a dict the returned
    document no longer references and vanish silently.

    :param document: The execution request to copy.
    :return: The copy, paired with one ``(dotted path, container, key)`` triple
        per protected leaf that holds a value.
    """
    result = dict(document)
    sites: list[tuple[str, dict[str, Any], str]] = []
    copied: set[tuple[str, ...]] = set()
    for path in ENCRYPTED_LEAVES:
        *parents, name = path.split(".")
        node = result
        walked: tuple[str, ...] = ()
        for parent in parents:
            walked += (parent,)
            child = node.get(parent)
            if not isinstance(child, dict):
                break
            if walked not in copied:
                child = dict(child)
                node[parent] = child
                copied.add(walked)
            node = child
        else:
            if node.get(name) is not None:
                sites.append((path, node, name))
    return result, sites
