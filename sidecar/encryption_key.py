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
"""Resolve the side-car's ``ENCRYPTION_KEY`` before supervisord starts.

``entrypoint.sh`` runs this once, after ``settings-env.sh`` has expanded the
deployment inputs, and only when neither an explicit ``ENCRYPTION_KEY`` nor a
file of that name under ``SECRETS_DIR`` already supplied one. Stdout carries
the resolved key and nothing else; every diagnostic goes to stderr. Both
channels are re-read here anyway so a run outside the entrypoint answers the
same thing the container would.

Unlike the Grafana token beside it, this key is not re-mintable. Every
``settingoverride`` row PMM Extensions has encrypted is readable only under the key that
wrote it, and :mod:`~app.core.settings_override.cache` warns and skips a row it
cannot decrypt rather than failing the load, so minting a replacement over
surviving ciphertext brings the container up green with the affected overrides
silently reverted to their YAML values. Minting therefore happens only where all
three service databases are provably free of ciphertext, and any database that
cannot be reached counts as unproven.

The freshness probe reads the databases through :class:`_ServiceDatabase`
subclasses rather than the services' own settings classes. Those resolve the
same ``<PREFIX>__DATABASE__*`` sources (environment, dotenv, ``SECRETS_DIR``
file, YAML profile) while requiring no ``ENCRYPTION_KEY`` of their own, which
the key-less path this helper runs on could not supply.

Deployment inputs, all optional: ``EXTENSIONS_STATE_DIR`` and
``EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT``.
"""

import asyncio
import base64
import fcntl
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, TextIO

from cryptography.fernet import Fernet
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import BaseYamlSettings
from app.core.db.config import DatabaseOptions
from app.core.encryption import is_encrypted, is_stored_ciphertext
from app.core.settings_override.models import SettingOverride
from app.core.utils.fields import credential_url_password
from sidecar.runtime import (
    DEFAULT_STATE_DIR,
    positive_timeout,
    RETRY_INTERVAL_SECONDS,
    state_dir,
)
from sidecar.runtime import warn as runtime_warn

__all__ = [
    "DEFAULT_STATE_DIR",
    "RETRY_INTERVAL_SECONDS",
    "probe_timeout",
    "state_dir",
    "warn",
]

warn = partial(runtime_warn, "encryption-key")

PERSISTED_FILENAME = "ENCRYPTION_KEY"
LOCK_FILENAME = ".ENCRYPTION_KEY.lock"

DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0
"""How long the probe keeps waiting for the databases, across all three.

The supervised migration steps wait for postgres unboundedly
(``supervisord.conf``: ``until nc -z ...; do sleep 1; done``), so on a first
start the databases are routinely not up yet, which is exactly when the mint
path runs. A probe that refused on the first connection error would make PID 1
die on the ordinary cold start this feature exists to serve.
"""

LOCK_POLL_INTERVAL_SECONDS = 0.2
"""How often a start re-tries the state lock while a peer holds it."""

LOCK_WAIT_PROBE_BUDGETS = 2.0
"""How many probe budgets a start waits for a peer before it refuses.

A peer holding the lock is running one freshness probe and four file
operations, so its turn is bounded by :func:`probe_timeout` and little else.
Deriving the wait from that same knob rather than a second one keeps a
deployment that raised the probe bound from refusing on a peer merely using it.
Waiting unboundedly instead would leave PID 1 blocked with nothing on stderr,
which is the illegible failure this helper exists to replace: ``flock`` is
released when a peer dies, but not when one is merely stopped.
"""

KEY_BYTES = 32
"""What Fernet's URL-safe base64 key decodes to: a 16-byte signing half and a
16-byte encryption half."""

PRE_RENAME_CIPHERTEXT_MARKER = "sep.enc.v1."
"""The envelope marker stored ciphertext carried before it was renamed.

This probe runs before Alembic, so it can meet a database whose markers the
renaming revisions have not moved yet. Missing such a value would read the
database as fresh and mint a key over ciphertext the lost key wrote.
"""


class EncryptionKeyError(Exception):
    """Raise when no key can be served and the pre-flight has to give up."""


class _ServiceDatabase(BaseYamlSettings):
    """Resolve one service's database options without resolving its settings.

    Subclassed once per service because
    :meth:`~app.core.config.BaseYamlSettings.settings_customise_sources` ranks
    the prefixed spelling of a name above the unprefixed one from
    ``SETTINGS_PREFIXES``, which is a class-level declaration. Mirrors
    ``app.core.config.ExtensionsDatabaseSettings``, which reads the PMM Extensions database the
    same way for the same reason: to stay clear of a proxy it cannot resolve.

    :param DATABASE: The service's database connection options. Left without a
        default so each subclass supplies its own service's, rather than
        inheriting one service's name as a silent fallback for the others.
    """

    DATABASE: DatabaseOptions


class _ExtensionsDatabase(_ServiceDatabase):
    """Resolve the ``extensions`` service's database options.

    :cvar SETTINGS_PREFIXES: The prefix this probe reads its sources under.
    :param DATABASE: The service's database connection options.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["EXTENSIONS"]
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="extensions.db")


class _InventoryDatabase(_ServiceDatabase):
    """Resolve the ``inventory`` service's database options.

    :cvar SETTINGS_PREFIXES: The prefix this probe reads its sources under.
    :param DATABASE: The service's database connection options.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["INVENTORY"]
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="inventory.db")


class _TasksDatabase(_ServiceDatabase):
    """Resolve the ``tasks`` service's database options.

    :cvar SETTINGS_PREFIXES: The prefix this probe reads its sources under.
    :param DATABASE: The service's database connection options.
    """

    SETTINGS_PREFIXES: ClassVar[list[str]] = ["TASKS"]
    DATABASE: DatabaseOptions = DatabaseOptions(NAME="tasks.db")


SERVICE_DATABASES: dict[str, type[_ServiceDatabase]] = {
    "extensions": _ExtensionsDatabase,
    "inventory": _InventoryDatabase,
    "tasks": _TasksDatabase,
}
"""Every database carrying ``settingoverride`` rows, keyed by service name.

The model is shared and its DDL created per service, so a row encrypted under
the lost key can sit in any one of the three.
"""


def probe_timeout() -> float:
    """Return how long each freshness probe may wait for its database.

    :return: The bound in seconds.
    """
    return positive_timeout(
        "EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_SECONDS, warn
    )


def lock_timeout() -> float:
    """Return how long a start waits for a peer to finish its turn.

    :return: The bound in seconds.
    """
    return probe_timeout() * LOCK_WAIT_PROBE_BUDGETS


def mounted_key() -> str | None:
    """Return the key a file under ``SECRETS_DIR`` supplies, if one does.

    Matched case-insensitively and skipping any entry resolving outside the
    directory, which is how both ``settings-env.sh`` and the settings source
    walk the same directory.

    :return: The stripped key, or ``None`` when no usable file supplies one.
    """
    configured = (os.environ.get("SECRETS_DIR") or "").strip()
    if not configured:
        return None
    directory = Path(configured).resolve()
    if not directory.is_dir():
        return None
    try:
        for entry in directory.iterdir():
            if entry.name.lower() != PERSISTED_FILENAME.lower() or not entry.is_file():
                continue
            if not entry.resolve().is_relative_to(directory):
                continue
            return entry.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        # An unreadable directory or file supplies nothing, which is how the
        # shell helper's glob treats it too, and the channels below still
        # apply. UnicodeDecodeError is listed because it is a ValueError, not
        # an OSError, so a non-UTF-8 file would otherwise escape as a traceback
        # in place of this module's actionable diagnostic.
        return None
    return None


def supplied_key() -> str | None:
    """Return the key channels 1 and 2 supply, in that order.

    :return: The stripped key, or ``None`` when neither channel supplies one.
    """
    explicit = (os.environ.get("ENCRYPTION_KEY") or "").strip()
    return explicit or mounted_key()


def read_persisted_key(directory: Path) -> str | None:
    """Return the key an earlier start persisted, if it left a usable one.

    :param directory: The state directory to read from.
    :return: The stripped key, or ``None`` when the file is absent, unreadable,
        or holds only whitespace.
    """
    try:
        raw = (directory / PERSISTED_FILENAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError rather than an OSError, so a
        # non-UTF-8 file would otherwise leave this function as a traceback.
        return None
    return raw.strip() or None


def validated(key: str, source: str) -> str:
    """Return ``key`` once the cipher every supervised program builds accepts it.

    Checked here rather than left to the settings classes because failing there
    is the illegible shape this helper exists to replace: the key reaches five
    children, each rejects it on its own, and PID 1 has already exited 0 with
    nothing to say. A minted key is valid by construction, so in practice this
    catches a state file that was corrupted or hand-edited.

    :param key: The resolved key.
    :param source: Where it came from, for the diagnostic.
    :return: The key, unchanged.
    :raises EncryptionKeyError: If Fernet cannot build a cipher from it.
    """
    try:
        Fernet(key.encode())
    except (TypeError, ValueError) as error:
        raise EncryptionKeyError(
            f"The ENCRYPTION_KEY resolved from {source} is not a valid Fernet "
            f"key ({error}). It must be 32 url-safe base64-encoded bytes; "
            f"generate one with `openssl rand -base64 32`, not "
            f"`openssl rand -hex 32`. Nothing was minted or overwritten."
        ) from error
    return key


def mint_key() -> str:
    """Return a fresh Fernet key.

    Built exactly the way ``make encryption-key`` builds one, so a key minted
    here and one an operator generates by hand are interchangeable.

    :return: The URL-safe base64 key.
    """
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def write_persisted_key(directory: Path, key: str) -> None:
    """Persist ``key`` owner-only so every later start resolves the same value.

    Written through a uniquely named temporary file in the same directory and
    moved into place atomically, so neither an interrupted start nor a
    concurrent one can leave a truncated key where a working one was.
    :func:`tempfile.mkstemp` creates that file readable and writable by the
    owner alone, and :meth:`~pathlib.Path.replace` carries the mode across, so
    the key is never briefly group-readable.

    :param directory: The state directory to write into.
    :param key: The key to persist.
    :raises EncryptionKeyError: If the key cannot be persisted. Serving an
        unpersistable key would orphan every row written during the run that
        used it.
    """
    try:
        descriptor, name = tempfile.mkstemp(dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(key)
            temporary.replace(directory / PERSISTED_FILENAME)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise EncryptionKeyError(
            f"Could not persist the minted ENCRYPTION_KEY under {directory}: "
            f"{error}. Refusing to serve a key the next start cannot read back, "
            f"which would leave every row written under it unreadable. Mount a "
            f"writable volume at {directory}, or pass ENCRYPTION_KEY explicitly."
        ) from error


@contextmanager
def state_lock(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock over the whole re-read, probe, mint and persist.

    Two side-cars started together against one empty state volume both observe
    no key. Atomic replacement alone would leave them holding *different* keys,
    whichever wrote last; serialising here, and re-reading the persisted key
    once the lock is held, is what makes the loser adopt the winner's.

    ``umask`` narrows the mode at creation, the way ``write_persisted_token``
    in the token helper beside this one narrows it. Which of the two creates
    the directory changed here: key resolution runs ahead of the Grafana mint,
    so on an ``EXTENSIONS_STATE_DIR`` the image does not pre-create, this is the first
    writer and owns the mode the other one used to set.

    :param directory: The state directory to lock within, created when absent.
    :raises EncryptionKeyError: If the directory or its lock file cannot be
        opened for writing, or a peer holds the lock past :func:`lock_timeout`.
    """
    previous_umask = os.umask(0o077)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / LOCK_FILENAME).open("w", encoding="utf-8")
    except OSError as error:
        raise EncryptionKeyError(
            f"Could not open the state directory {directory} for writing: "
            f"{error}. A minted ENCRYPTION_KEY has to survive the restart that "
            f"reads the rows it encrypts, so nothing is minted. Mount a writable "
            f"volume there, or pass ENCRYPTION_KEY explicitly."
        ) from error
    finally:
        os.umask(previous_umask)
    try:
        _acquire_lock(handle, directory)
        yield
    finally:
        handle.close()


def _acquire_lock(handle: TextIO, directory: Path) -> None:
    """Take the exclusive lock, waiting a peer's turn out under a bound.

    Bounded because this runs as PID 1's pre-flight, where an indefinite wait
    is indistinguishable from a hang: the container sits unhealthy with nothing
    written, which is the shape the whole helper exists to replace.

    :param handle: The opened lock file.
    :param directory: The state directory, for the diagnostic.
    :raises EncryptionKeyError: If a peer holds the lock past the bound.
    """
    deadline = time.monotonic() + lock_timeout()
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise EncryptionKeyError(
                    f"Another start held the lock on "
                    f"{directory / LOCK_FILENAME} for over {lock_timeout():g}s. "
                    f"Refusing to probe and mint beside it, since two starts "
                    f"minting together leave each unable to read the rows the "
                    f"other wrote. Check whether a second side-car runs against "
                    f"this state directory, or raise "
                    f"EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT, which this bound follows. "
                    f"Nothing was minted or written."
                ) from error
        else:
            return
        time.sleep(
            min(LOCK_POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0.0))
        )


def _has_encrypted_url_password(value: str) -> bool:
    """Return whether ``value`` is a URL whose embedded password is stored ciphertext.

    A URL that cannot be parsed answers ``False`` rather than propagating: the
    caller has already tested the whole string, and a value malformed enough to
    defeat ``urlparse`` is not a stored endpoint whose password PMM Extensions encrypted.

    :param value: One string leaf of a stored override value.
    :return: Whether its userinfo password holds ciphertext under any at-rest
        envelope.
    """
    try:
        password = credential_url_password(value)
    except ValueError:
        return False
    return password is not None and _is_ciphertext_leaf(password)


def _is_ciphertext_leaf(value: str) -> bool:
    """Return whether ``value`` is stored ciphertext under any envelope it can carry.

    :param value: A whole string leaf or a URL password segment.
    :return: Whether it is marked, pre-rename marked, or bare ciphertext.
    """
    if is_stored_ciphertext(value):
        return True
    return value.startswith(PRE_RENAME_CIPHERTEXT_MARKER) and is_encrypted(
        value.removeprefix(PRE_RENAME_CIPHERTEXT_MARKER)
    )


def contains_ciphertext(value: Any) -> bool:
    """Return whether any string leaf of ``value`` holds ciphertext at rest.

    The stored value is JSON and the ciphertext sits at its *leaves*: an alert
    provider's routing key inside a list, a delivery input's API key inside a
    nested mapping. Testing the row's own value therefore finds nothing on
    exactly the rows that matter.

    A credential-bearing URL hides its token one level deeper still, inside the
    leaf's userinfo segment: ``is_encrypted`` answers ``False`` for the whole
    ``https://user:<token>@host/`` string, because the string is not a Fernet
    token. A string leaf is therefore tested twice — as itself, and as its
    parsed password — or a deployment whose only encrypted data is an endpoint
    password reads as holding none and clears the mint path.

    The decision is no longer purely structural, which is why the leaf test is
    :func:`~app.core.encryption.is_stored_ciphertext` rather than
    ``is_encrypted``: a leaf ``secret_storage`` wrote carries an envelope marker
    the structural check answers ``False`` for. Missing it would be the false
    negative named below as the dangerous direction, in its widest form — a
    deployment whose overrides were all written after the envelope shipped holds
    nothing the structural check can recognise, so it reads as fresh and the
    caller mints over every row.

    Structure still decides for a value the envelope declines, and is safe in
    this direction and only this one. The write path must not
    (``secret_storage.encrypt_secret_leaves``: a credential that happens to be
    base64 would be misread as ciphertext and stored in the clear), but here a
    false positive merely refuses to mint, which is loud and an operator
    resolves by supplying the key.

    :param value: The decoded stored value, at any depth.
    :return: Whether ciphertext appears anywhere within it, marked or bare.
    """
    if isinstance(value, str):
        return _is_ciphertext_leaf(value) or _has_encrypted_url_password(value)
    if isinstance(value, dict):
        return any(contains_ciphertext(leaf) for leaf in value.values())
    if isinstance(value, list):
        return any(contains_ciphertext(leaf) for leaf in value)
    return False


async def service_holds_ciphertext(options: DatabaseOptions) -> bool:
    """Return whether one service's override rows hold any ciphertext.

    A database whose ``settingoverride`` table has never been created has no
    rows to hold any, which is the ordinary first-start shape.

    :param options: The service's resolved database options.
    :return: Whether a Fernet token appears in any stored value.
    :raises SQLAlchemyError: If the database rejects the connection or query.
    :raises OSError: If the endpoint cannot be reached at all, which asyncpg
        surfaces as the socket error rather than wrapping it.
    :raises ValueError: If a stored value is not decodable JSON, which the
        column's own type raises while reading the result.
    """
    engine = create_async_engine(options.URL, **options.connect_engine_kwargs)
    try:
        async with engine.connect() as connection:
            table = SettingOverride.__table__
            if not await connection.run_sync(
                lambda sync: inspect(sync).has_table(table.name)
            ):
                return False
            stored = await connection.execute(select(table.c.value))
            return any(contains_ciphertext(value) for (value,) in stored)
    finally:
        await engine.dispose()


async def probe_until_deadline(
    service: str, options: DatabaseOptions, deadline: float
) -> bool:
    """Return whether one service holds ciphertext, waiting for it to answer.

    Connection failures are retried until ``deadline`` rather than refused on
    sight, because the databases being unreachable is the *ordinary* first-start
    condition, not an exceptional one. A value that cannot be decoded is not
    retried: it is deterministic, and re-reading it only delays the refusal.

    :param service: The service being probed, for the diagnostics.
    :param options: The service's resolved database options.
    :param deadline: The monotonic clock reading to give up at.
    :return: Whether a Fernet token appears in any stored value.
    :raises EncryptionKeyError: If the database cannot be read before the
        deadline, or holds a value that cannot be decoded.
    """
    last_error: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EncryptionKeyError(
                f"Could not reach the {service} database within "
                f"{probe_timeout():g}s ({last_error}). {_unproven_remedy()}"
            ) from last_error
        try:
            return await asyncio.wait_for(
                service_holds_ciphertext(options), timeout=remaining
            )
        except ValueError as error:
            raise EncryptionKeyError(
                f"The {service} database holds an override value that could "
                f"not be decoded ({error}). {_ciphertext_remedy()}"
            ) from error
        # TimeoutError subclasses OSError, so the bound and a refused
        # connection arrive through one clause.
        except (SQLAlchemyError, OSError) as error:
            last_error = error
        # Capped at what is left, so a refused connection -- which fails
        # instantly -- cannot overshoot the deadline by a whole interval.
        await asyncio.sleep(
            min(RETRY_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0.0))
        )


async def assert_every_database_is_fresh() -> None:
    """Raise unless all three service databases are provably free of ciphertext.

    One deadline covers all three, so a side-car waiting on a database that
    never comes up delays the container start by the timeout once rather than
    once per service.

    :raises EncryptionKeyError: If any database holds ciphertext, or cannot be
        read. Both refuse: a deployment whose freshness cannot be *proven* is
        one where minting may be destructive.
    :raises ValidationError: If a service's ``<PREFIX>__DATABASE__*`` settings
        do not resolve, which surfaces as a traceback rather than a diagnostic
        because it is a misconfiguration of the container, not a state the
        deployment can be in.
    """
    deadline = time.monotonic() + probe_timeout()
    for service, settings_cls in SERVICE_DATABASES.items():
        if await probe_until_deadline(service, settings_cls().DATABASE, deadline):
            raise EncryptionKeyError(
                f"The {service} database already holds encrypted override "
                f"values. {_ciphertext_remedy()}"
            )


def _ciphertext_remedy() -> str:
    """Return the remediation for a refusal that found unreadable data.

    :return: What the operator has to do, naming all three ways out.
    """
    return (
        "Refusing to mint a new ENCRYPTION_KEY: a new key cannot decrypt values "
        "written under the old one, and every affected override would silently "
        f"revert to its YAML value. Restore {state_dir() / PERSISTED_FILENAME} "
        "from a backup of the pmm-extensions-state volume, pass the deployment's original "
        "key as ENCRYPTION_KEY, or pass any newly generated key if this "
        "deployment was never encrypted and the value is plaintext that merely "
        "looks like a token."
    )


def _unproven_remedy() -> str:
    """Return the remediation for a refusal that could not read the data at all.

    Deliberately not :func:`_ciphertext_remedy`: nothing here says the
    deployment holds ciphertext, so pointing the operator at a key restore
    would send them after a backup for what is usually a database still
    starting.

    :return: What the operator has to do.
    """
    return (
        "Refusing to mint a new ENCRYPTION_KEY without proving the deployment "
        "holds no data a new key could not decrypt. Bring the database up and "
        "restart the container, check EXTENSIONS_DB_HOST and EXTENSIONS_DB_PORT, or raise "
        "EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT. Nothing was minted or written."
    )


def resolve() -> str:
    """Resolve the key from the first channel that supplies one, minting last.

    :return: The resolved key, always the value read back from disk rather
        than a locally minted string, so racing starts converge.
    :raises EncryptionKeyError: If no key can be served.
    """
    supplied = supplied_key()
    if supplied is not None:
        return validated(supplied, "the environment or a SECRETS_DIR file")

    directory = state_dir()
    persisted = read_persisted_key(directory)
    if persisted is not None:
        return validated(persisted, f"{directory / PERSISTED_FILENAME}")

    with state_lock(directory):
        # Re-read under the lock: a start that raced this one to an empty state
        # volume may have persisted between the read above and the lock.
        persisted = read_persisted_key(directory)
        if persisted is not None:
            return validated(persisted, f"{directory / PERSISTED_FILENAME}")
        asyncio.run(assert_every_database_is_fresh())
        write_persisted_key(directory, mint_key())
        final = read_persisted_key(directory)
    if final is None:
        raise EncryptionKeyError(
            f"The ENCRYPTION_KEY persisted under {directory} could not be read "
            f"back immediately after being written."
        )
    return final


def main() -> int:
    """Print the resolved key for ``entrypoint.sh`` to capture.

    :return: The process exit status.
    """
    try:
        # The settings stack binds its logging handlers to whatever stdout is
        # when it is first resolved, so without this a log record would land
        # inside the key entrypoint.sh captures.
        with redirect_stdout(sys.stderr):
            key = resolve()
    except EncryptionKeyError as error:
        warn(str(error))
        return 1
    sys.stdout.write(f"{key}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
