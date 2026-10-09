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

"""Encrypt and decrypt values PMM Extensions stores at rest, keyed by ``ENCRYPTION_KEY``.

The ciphertext is Fernet: authenticated AES-128-CBC carrying its own version
marker, timestamp and HMAC, rendered as URL-safe base64 text that any ``str``
or JSON column stores unchanged. Encryption is **not** deterministic: each call
derives a fresh IV, so two encryptions of one plaintext differ and ciphertext
can never be compared for equality.

Use :func:`is_encrypted`, never a caught :class:`DecryptionError`, to decide
whether a stored value still needs encrypting.

:func:`mark_ciphertext` and :func:`marked_ciphertext` wrap that ciphertext in a
versioned envelope, so a consumer reads the answer off the stored value's own
format instead of guessing it from the bytes. The envelope is applied by the
caller rather than by :func:`encrypt`: marking inside the primitive would change
what every other at-rest consumer stores. A consumer that may see a marked value
asks :func:`is_stored_ciphertext`, which consults both discriminators;
:func:`is_encrypted` alone reports ``False`` for every marked value.
"""

__all__ = [
    "DecryptionError",
    "decrypt",
    "encrypt",
    "is_encrypted",
    "is_stored_ciphertext",
    "mark_ciphertext",
    "marked_ciphertext",
]

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

_FERNET_VERSION = 0x80
"""The first byte of every decoded Fernet token, which is its version marker."""

_TOKEN_ENVELOPE_BYTES = 57
"""Bytes a Fernet token spends outside its ciphertext: version, timestamp, IV, HMAC.

One version byte, eight of timestamp, a sixteen-byte IV and a thirty-two-byte
HMAC. What sits between them is CBC-padded, so a real token's decoded length is
always this plus a positive multiple of the block size.
"""

_CIPHER_BLOCK_BYTES = 16
"""The AES block size every Fernet ciphertext is padded to."""

_MIN_TOKEN_BYTES = _TOKEN_ENVELOPE_BYTES + _CIPHER_BLOCK_BYTES
"""The shortest decodable Fernet token: the envelope plus the single block CBC
pads even an empty plaintext to.

Accepting anything shorter would make a migration *skip* a value it can never
decrypt, leaving it in the clear for good.
"""

_URLSAFE_TO_STANDARD = str.maketrans("-_", "+/")
"""Maps the URL-safe base64 alphabet onto the standard one.

Needed because only ``base64.b64decode`` accepts ``validate``; the URL-safe
wrapper translates and then decodes without it.
"""

_CIPHERTEXT_V1_PREFIX = "extensions.enc.v1."
"""The marker a stored ciphertext carries, naming its envelope version.

Contains a character outside the base64 alphabet, so :func:`is_encrypted` rejects
a marked value outright rather than answering from its length and padding, and the
two discriminators can never both claim one stored value. The character is also
RFC 3986 unreserved, so a marked token survives a URL userinfo segment without
percent-encoding.
"""


class DecryptionError(ValueError):
    """Define exception raised when a value cannot be decrypted with the configured key."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Return the process-wide cipher built from ``settings.ENCRYPTION_KEY``.

    Cached so the key is resolved once per process; ``cache_clear()`` resets it.
    Deferred behind an accessor rather than built at module scope so importing
    this module resolves no settings.

    :return: The cached cipher.
    """
    from app.core.config import (  # noqa: PLC0415 - config imports settings_override, whose __init__ imports cache, which imports this (cycle)
        settings,
    )

    return Fernet(settings.ENCRYPTION_KEY.get_secret_value().encode())


def encrypt(value: str) -> str:
    """Return ``value`` encrypted as URL-safe base64 ciphertext text.

    :param value: The plaintext to encrypt.
    :return: The ciphertext, storable in any text or JSON column.
    """
    return _get_fernet().encrypt(value.encode()).decode("ascii")


def decrypt(value: str) -> str:
    """Return the plaintext behind ``value``.

    :param value: The ciphertext to decrypt.
    :return: The decrypted plaintext.
    :raises DecryptionError: If ``value`` is not ciphertext this key produced,
        which covers a legacy plaintext value, a corrupt one, and one encrypted
        under a different key alike. Use :func:`is_encrypted` to tell those
        apart; this exception does not.
    """
    try:
        # Encode before handing the token over: Fernet narrows a str with
        # ascii, and base64 turns that failure into a plain ValueError its
        # binascii.Error handler does not catch, so a non-ASCII stored value
        # would escape uncaught instead of as DecryptionError.
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Value could not be decrypted: it is malformed, or was encrypted "
            "with a different ENCRYPTION_KEY."
        ) from exc


def is_encrypted(value: str) -> bool:
    """Return whether ``value`` is structurally a Fernet token.

    Reads the token's own version marker instead of attempting a decrypt, so a
    token written under a *different* key still reports ``True``. That is the
    property a migration needs: a caught :class:`DecryptionError` cannot
    separate "never encrypted" from "encrypted with a key this process does not
    hold", and encrypting the latter again destroys the only copy of its
    plaintext.

    The decode rejects any character outside the base64 alphabet instead of
    discarding it, and the decoded length is checked against Fernet's own
    framing. Both are load-bearing, because a false positive here is expensive
    in two directions at once: a migration skips the value and leaves a
    credential in the clear, and every later read classifies it as ciphertext it
    cannot decrypt, so the record is withheld from callers for good. Decoding
    leniently makes the question collapse to "do the surviving alphabet
    characters happen to pad correctly", which ordinary prose satisfies roughly
    once in every eight hundred values.

    :param value: The stored value to classify.
    :return: ``True`` when ``value`` is shaped like a Fernet token, ``False``
        for anything else, including input that is not valid base64 at all.
    """
    try:
        raw = base64.b64decode(value.translate(_URLSAFE_TO_STANDARD), validate=True)
    except ValueError:
        return False
    return (
        len(raw) >= _MIN_TOKEN_BYTES
        and raw[0] == _FERNET_VERSION
        and (len(raw) - _TOKEN_ENVELOPE_BYTES) % _CIPHER_BLOCK_BYTES == 0
    )


def mark_ciphertext(token: str) -> str:
    """Return ``token`` carrying the current envelope marker.

    :param token: Ciphertext from :func:`encrypt`.
    :return: The marked form, as it is stored.
    """
    return f"{_CIPHERTEXT_V1_PREFIX}{token}"


def marked_ciphertext(value: str) -> str | None:
    """Return the token behind a marked value, or ``None`` when there is none.

    The marker is what *claims* a value is ciphertext; the structural check then
    confirms the payload it claimed. Both are required, because the marker is a
    literal prefix and nothing stops a legacy plaintext from beginning with it —
    a bare prefix test would read ``extensions.enc.v1.operator-secret`` as ciphertext,
    leave it in the clear through the migration and fail every later read of it.
    Requiring the payload to be a well-formed token as well narrows that to a
    value carrying the exact prefix *and* decoding as a Fernet token behind it.

    This is not the structural check deciding. A value written under this
    envelope always satisfies both halves, so the shape test can never
    reclassify one; it can only reject a prefix the writer never produced.

    A payload that fails the check is treated as plaintext rather than raising,
    which is what the unmarked path already does with a corrupt token
    (``decrypt(leaf) if is_encrypted(leaf) else leaf`` returns it untouched), so
    the two envelopes fail identically on a corrupted value.

    :param value: The stored value to classify.
    :return: The bare token, or ``None``.
    """
    if not value.startswith(_CIPHERTEXT_V1_PREFIX):
        return None
    token = value.removeprefix(_CIPHERTEXT_V1_PREFIX)
    return token if is_encrypted(token) else None


def is_stored_ciphertext(value: str) -> bool:
    """Return whether ``value`` holds ciphertext under either at-rest envelope.

    The discriminator every consumer of a stored value wants, and the reason it
    is here rather than spelled out per caller: *both* discriminators have to be
    consulted, because a marked value is invisible to the structural one — the
    marker's ``.`` is outside the base64 alphabet, so :func:`is_encrypted`
    rejects the whole string. A consumer testing only the shape reads a store
    holding nothing but marked values as holding no ciphertext at all.

    The order of the two is immaterial here, because they are disjoint: no
    stored value satisfies both. Consulting both is the entire content of that
    rule, so a second envelope version changes this function rather than every
    caller.

    :param value: The stored value to classify.
    :return: Whether it holds ciphertext under either envelope.
    """
    return marked_ciphertext(value) is not None or is_encrypted(value)
