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
"""Cover the side-car's encryption-key resolution, freshness guard and mint."""

import asyncio
import base64
import contextlib
import fcntl
import os
import socket
import stat
import string
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, insert

from app import BASE_DIR
from app.core.db.config import DatabaseOptions
from app.core.encryption import is_encrypted, mark_ciphertext
from app.core.settings_override.models import SettingOverride
from sidecar import encryption_key as helper
from tests.sidecar.conftest import SIDECAR_DIR

HELPER_SCRIPT = SIDECAR_DIR / "encryption_key.py"

SERVICE_PREFIXES = ("EXTENSIONS", "INVENTORY", "TASKS")
"""The three services whose databases the freshness probe has to clear."""

DATABASE_FILENAMES = {
    "EXTENSIONS": "extensions.db",
    "INVENTORY": "inventory.db",
    "TASKS": "tasks.db",
}
"""One distinct SQLite file per service, so a single-DSN probe fails these tests."""

PRE_RENAME_CIPHERTEXT_MARKER = "sep.enc.v1."
"""The envelope marker stored ciphertext carried before it was renamed."""

KEY_FILE_MODE = 0o600

STATE_DIR_MODE = 0o700
"""What the image bakes the state directory as, and what creating it must match.

``Containerfile.sidecar`` installs ``/home/extensions/state`` owner-only, so on the
default path nothing here creates it. An ``EXTENSIONS_STATE_DIR`` pointed elsewhere is
created by whichever helper runs first, which is now this one.
"""

URL_SAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_=")
"""The alphabet a Fernet key is encoded in, which ``-hex`` and plain ``-base64``
output do not both stay inside."""

UNREACHABLE_PORT = 1
"""A privileged port nothing in the test environment listens on."""

HELPER_STARTUP_ALLOWANCE_SECONDS = 60.0
"""How long a helper run that should finish may take, imports included.

Deliberately generous. This bound is a hang guard, not a performance assertion:
interpreter start plus the settings-stack import swings with CPU contention and
page-cache state, which is why the margin is wide rather than fitted. It still
sits under the 120s ``pytest-timeout`` ceiling, so this bound fires first and
kills the child rather than leaving the global guard to orphan it.
"""

STILL_WAITING_SECONDS = 15.0
"""How long a run aimed at an unreachable database is watched before it is ended.

Long enough for the interpreter start and the first probe, whose failure is
reported at once, so the report is in the captured stderr when the run is ended.
"""

SHORT_LOCK_WAIT_SECONDS = 1.0
"""The lock bound a case swaps in, so a refusal arrives inside the test's patience."""

SHORT_INTERVAL_SECONDS = 0.05
"""The retry interval a case swaps in to watch several attempts in under a second."""

SHORT_REPORT_INTERVAL_SECONDS = 0.3
"""The report interval a case swaps in, several retries long."""

WATCHED_WAIT_SECONDS = 1.0
"""How long an in-process case lets a probe wait before it ends it."""

MIN_REPORTS = 2
"""The fewest reports a wait longer than one report interval must make."""

RETIRED_TIMEOUT_INPUT = "EXTENSIONS_ENCRYPTION_PROBE_TIMEOUT"
"""The variable that bounded the probe, which a deployment may still set."""

RESTORE_HINT = "pmm-extensions-state"
"""What a refusal has to name so an operator can act on it."""

LENGTH_FIELD_BYTES = 4
"""The size of the length every PostgreSQL message carries, which counts itself."""

LEADING_HEADER_BYTES = 8
"""The length and protocol code a client's first message opens with, and the
whole of an SSLRequest."""

SSL_REQUEST_CODE = 80877103
"""The protocol code a PostgreSQL client sends in place of a version to ask for TLS."""

STARTING_UP_ERROR_FIELDS = (
    b"SFATAL\0VFATAL\0C57P03\0Mthe database system is starting up\0\0"
)
"""The ``ErrorResponse`` body a PostgreSQL that is still starting answers a
connection with: SQLSTATE 57P03, which asyncpg raises as ``CannotConnectNowError``."""

FAKE_SERVER_POLL_SECONDS = 0.1
"""How often the fake server's accept loop checks whether its fixture has ended."""


def fernet_key() -> str:
    """Return a freshly generated Fernet key.

    :return: A key the settings validator accepts.
    """
    return Fernet.generate_key().decode("ascii")


def ciphertext(plaintext: str = "a-stored-credential") -> str:
    """Return a real Fernet token, built under a key this process discards.

    Built with :class:`~cryptography.fernet.Fernet` rather than written as a
    literal so the fixtures stay valid tokens if the format ever moves, and
    under a throwaway key because the probe must recognise ciphertext it
    cannot decrypt.

    :param plaintext: The value to encrypt.
    :return: The token, as it would sit in a stored override.
    """
    return Fernet(fernet_key().encode()).encrypt(plaintext.encode()).decode("ascii")


def marked_ciphertext_value(plaintext: str = "a-stored-credential") -> str:
    """Return a token carrying the envelope marker the write path now writes.

    The shape the structural check cannot see: ``is_encrypted`` answers
    ``False`` for every marked value, so a deployment whose overrides were all
    written after the envelope shipped reads as holding no ciphertext unless
    the probe tests the envelope too — and the caller mints a fresh key on that
    answer, destroying decryptability of every marked row.

    :param plaintext: The value to encrypt.
    :return: The marked token, as it would sit in a stored override.
    """
    return mark_ciphertext(ciphertext(plaintext))


def create_database(directory: Path, filename: str, *values: Any) -> None:
    """Create one service's ``settingoverride`` table and seed it.

    :param directory: The directory to place the SQLite file in.
    :param filename: The database file's name.
    :param values: One stored override value per row, JSON-storable.
    """
    engine = create_engine(f"sqlite:///{directory / filename}")
    SettingOverride.__table__.create(engine)
    if values:
        with engine.begin() as connection:
            connection.execute(
                insert(SettingOverride.__table__),
                [
                    {
                        "setting_class": "EXTENSIONS_SETTINGS",
                        "key": f"KEY_{index}",
                        "value": value,
                        "is_active": True,
                    }
                    for index, value in enumerate(values)
                ],
            )
    engine.dispose()


def database_environment(directory: Path) -> dict[str, str]:
    """Return the environment pointing each service at its own SQLite file.

    :param directory: The directory holding the three database files.
    :return: One ``NAME`` variable per service.
    """
    return {
        f"{prefix}__DATABASE__NAME": str(directory / DATABASE_FILENAMES[prefix])
        for prefix in SERVICE_PREFIXES
    }


def unreachable_environment(
    prefix: str, port: int = UNREACHABLE_PORT
) -> dict[str, str]:
    """Return the environment pointing one service at a database nothing answers.

    :param prefix: The service whose endpoint to break.
    :param port: The port to aim it at.
    :return: The PostgreSQL connection variables for that service alone.
    """
    return {
        f"{prefix}__DATABASE__ENGINE": "postgresql+asyncpg",
        f"{prefix}__DATABASE__HOST": "127.0.0.1",
        f"{prefix}__DATABASE__PORT": str(port),
        f"{prefix}__DATABASE__USER": "extensions",
        f"{prefix}__DATABASE__NAME": "extensions",
    }


@pytest.fixture
def stalled_database_port() -> Iterator[int]:
    """Return a port that completes the TCP handshake and then answers nothing.

    A refused connection fails immediately and never reaches the timeout, so
    this is what exercises the bound rather than the error path beside it: the
    kernel accepts into the backlog while nothing ever reads, leaving the
    driver waiting on a startup reply that does not come.

    :return: The listening port.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        yield listener.getsockname()[1]


def receive_exactly(connection: socket.socket, size: int) -> bytes:
    """Read ``size`` bytes from ``connection``, or fewer if the peer hangs up.

    :param connection: The accepted client connection.
    :param size: How many bytes to read.
    :return: The bytes read.
    """
    received = b""
    while len(received) < size:
        chunk = connection.recv(size - len(received))
        if not chunk:
            break
        received += chunk
    return received


def refuse_as_starting_up(connection: socket.socket) -> None:
    """Answer one client the way a PostgreSQL that is still starting does.

    Declines TLS if the client asks for it, reads the startup message, and
    answers it with the 57P03 ``ErrorResponse`` before hanging up.

    :param connection: The accepted client connection.
    """
    header = receive_exactly(connection, LEADING_HEADER_BYTES)
    if len(header) < LEADING_HEADER_BYTES:
        return
    length, code = struct.unpack("!ii", header)
    if length == LEADING_HEADER_BYTES and code == SSL_REQUEST_CODE:
        connection.sendall(b"N")
        header = receive_exactly(connection, LENGTH_FIELD_BYTES)
        if len(header) < LENGTH_FIELD_BYTES:
            return
        (length,) = struct.unpack("!i", header)
        receive_exactly(connection, length - LENGTH_FIELD_BYTES)
    else:
        receive_exactly(connection, length - LEADING_HEADER_BYTES)
    connection.sendall(
        b"E"
        + struct.pack("!i", len(STARTING_UP_ERROR_FIELDS) + LENGTH_FIELD_BYTES)
        + STARTING_UP_ERROR_FIELDS
    )


@pytest.fixture
def starting_database_port() -> Iterator[int]:
    """Return a port where a fake PostgreSQL refuses every connection as starting up.

    A real server that is still starting answers the startup message with
    SQLSTATE 57P03 rather than leaving it unanswered, and asyncpg raises that
    at connect time without SQLAlchemy wrapping it. That is the refusal this
    fixture reproduces, for as many connections as the test makes.

    :return: The listening port.
    """
    stopped = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(FAKE_SERVER_POLL_SECONDS)

        def serve() -> None:
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(WATCHED_WAIT_SECONDS)
                    with contextlib.suppress(OSError):
                        refuse_as_starting_up(connection)

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stopped.set()
            server.join()


def helper_environment(directory: Path) -> dict[str, str]:
    """Return the minimal environment the helper runs from, as ``entrypoint.sh`` runs it.

    ``ENCRYPTION_KEY`` is deliberately absent: the whole point of the helper is
    what it does when nothing supplied one.

    :param directory: The working directory, which is also where the databases
        and the state directory live.
    :return: The variables to start the helper with.
    """
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(BASE_DIR),
        "EXTENSIONS_STATE_DIR": str(directory / "state"),
        **database_environment(directory),
    }


def watch_probe(options: DatabaseOptions) -> None:
    """Let the extensions probe wait for ``WATCHED_WAIT_SECONDS``, then end it.

    :param options: The database options to probe.
    """

    async def watch() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                helper.probe_until_answered("extensions", options),
                WATCHED_WAIT_SECONDS,
            )

    asyncio.run(watch())


def run_helper(
    directory: Path, *, timeout: float | None = None, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Run the helper as ``entrypoint.sh`` runs it, from :func:`helper_environment`.

    :param directory: The working directory, which is also where the databases
        and the state directory live.
    :param timeout: How long to wait before raising
        :class:`subprocess.TimeoutExpired`, or ``None`` to wait indefinitely.
    :param environment: Variables to add over the base.
    :return: The completed run, whose stdout carries the resolved key.
    """
    return subprocess.run(
        [sys.executable, str(HELPER_SCRIPT)],
        cwd=str(directory),
        env={**helper_environment(directory), **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


@pytest.fixture
def fresh_deployment(tmp_path: Path) -> Path:
    """Create three empty service databases and return their directory.

    :param tmp_path: The per-test temporary directory.
    :return: The directory holding the three files.
    """
    for filename in DATABASE_FILENAMES.values():
        create_database(tmp_path, filename)
    return tmp_path


def persisted_key_path(directory: Path) -> Path:
    """Return where the helper persists a minted key under ``directory``.

    :param directory: The working directory passed to :func:`run_helper`.
    :return: The persisted key's path.
    """
    return directory / "state" / helper.PERSISTED_FILENAME


def test_a_fresh_deployment_mints_a_key_and_persists_it(fresh_deployment: Path):
    """Mint on a deployment with no ciphertext, which is the out-of-box path."""
    result = run_helper(fresh_deployment)

    assert result.returncode == 0, result.stderr
    minted = result.stdout.strip()
    assert minted
    assert persisted_key_path(fresh_deployment).read_text(encoding="utf-8") == minted


def test_a_minted_key_is_one_the_settings_validator_accepts(fresh_deployment: Path):
    """Mint a real Fernet key, which is what every supervised program then builds."""
    result = run_helper(fresh_deployment)

    assert result.returncode == 0, result.stderr
    cipher = Fernet(result.stdout.strip().encode())
    assert cipher.decrypt(cipher.encrypt(b"an-override-value")) == b"an-override-value"


def test_a_minted_key_is_persisted_owner_only(fresh_deployment: Path):
    """Keep the key off every other account in the container's namespace."""
    run_helper(fresh_deployment)

    mode = persisted_key_path(fresh_deployment).stat().st_mode
    assert stat.S_IMODE(mode) == KEY_FILE_MODE


def test_a_created_state_directory_is_owner_only(fresh_deployment: Path):
    """Create the directory as narrowly as the token helper beside this one does.

    The fixture leaves it absent, which is the ``EXTENSIONS_STATE_DIR`` case the image
    cannot pre-create. Whichever helper runs first owns the mode every later one
    inherits, and key resolution now runs before the Grafana mint.
    """
    state = fresh_deployment / "state"
    assert not state.exists()

    run_helper(fresh_deployment)

    assert stat.S_IMODE(state.stat().st_mode) == STATE_DIR_MODE


def test_an_explicit_key_is_returned_without_minting(fresh_deployment: Path):
    """Return channel 1 untouched, which no lower channel may ever displace."""
    supplied = fernet_key()

    result = run_helper(fresh_deployment, ENCRYPTION_KEY=supplied)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == supplied
    assert not persisted_key_path(fresh_deployment).exists()


def test_a_mounted_key_file_is_returned_without_minting(fresh_deployment: Path):
    """Resolve channel 2, so a helper run standalone agrees with the shell."""
    mounted = fernet_key()
    secrets_dir = fresh_deployment / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "ENCRYPTION_KEY").write_text(mounted, encoding="utf-8")

    result = run_helper(fresh_deployment, SECRETS_DIR=str(secrets_dir))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == mounted
    assert not persisted_key_path(fresh_deployment).exists()


def test_a_persisted_key_is_returned_byte_for_byte(fresh_deployment: Path):
    """Return the same key an earlier start minted, which the rows were written under."""
    first = run_helper(fresh_deployment)

    second = run_helper(fresh_deployment)

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == first.stdout.strip()


@pytest.mark.parametrize("prefix", SERVICE_PREFIXES)
def test_a_persisted_key_resolves_without_reaching_any_database(
    fresh_deployment: Path, prefix: str
):
    """Skip the probe entirely on a restart, which owes no database dependency.

    Every service is pointed at an endpoint nothing answers, so a run that
    probed could only refuse. Exiting 0 with the persisted key is therefore
    the observable proof the mint path was never entered.
    """
    first = run_helper(fresh_deployment)

    second = run_helper(fresh_deployment, **unreachable_environment(prefix))

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == first.stdout.strip()


def test_a_scalar_ciphertext_row_refuses_the_mint(tmp_path: Path):
    """Refuse where a plain string column already holds a token."""
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], ciphertext())
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert RESTORE_HINT in result.stderr


def test_a_marked_ciphertext_row_refuses_the_mint(tmp_path: Path):
    """Refuse over a row written under the envelope, not just a bare token.

    The end-to-end half of the probe change, and the case a deployment upgraded
    into the envelope eventually becomes entirely: every override row marked,
    none of them visible to the structural check. A probe that missed them would
    report the deployment fresh and mint over live ciphertext, which comes up
    green with the operator's overrides silently reverted to their YAML values.
    """
    create_database(
        tmp_path, DATABASE_FILENAMES["EXTENSIONS"], marked_ciphertext_value()
    )
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert RESTORE_HINT in result.stderr


def test_a_marked_credential_url_password_refuses_the_mint(tmp_path: Path):
    """Refuse where the only ciphertext is a marked password inside an endpoint.

    Two layers have to line up: the URL is not itself a token, and its password
    is invisible to the structural check once marked.
    """
    endpoint = {"endpoint": f"https://user:{marked_ciphertext_value()}@host:8443/"}
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], endpoint)
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert RESTORE_HINT in result.stderr


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            f"{PRE_RENAME_CIPHERTEXT_MARKER}{ciphertext()}", id="pre-rename-scalar"
        ),
        pytest.param(
            {
                "endpoint": (
                    f"https://user:{PRE_RENAME_CIPHERTEXT_MARKER}{ciphertext()}"
                    "@host:8443/"
                )
            },
            id="pre-rename-url-password",
        ),
    ],
)
def test_a_pre_rename_marked_row_refuses_the_mint(tmp_path: Path, value: Any):
    """Refuse over ciphertext still carrying the marker from before the rename.

    The probe runs before Alembic, so the revisions that move the marker have
    not touched these rows yet. Neither current discriminator accepts the old
    marker, so a probe that tested only them would read a deployment whose key
    file was lost as fresh and mint over every stored credential.
    """
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], value)
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert RESTORE_HINT in result.stderr


def test_a_ciphertext_leaf_nested_in_a_list_refuses_the_mint(tmp_path: Path):
    """Refuse on the ``PROVIDERS`` shape, which a top-level check cannot see.

    Captured from a real row: ``jsonb_typeof`` is ``array`` and the token is
    the ``ROUTING_KEY`` leaf, so ``is_encrypted`` applied to the row's own
    value finds nothing at all.
    """
    providers = [{"PROVIDER": "pagerduty", "ROUTING_KEY": ciphertext()}]
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], providers)
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_a_ciphertext_leaf_nested_in_a_mapping_refuses_the_mint(tmp_path: Path):
    """Refuse on the ``DIAGNOSTICS_DELIVERY_INPUTS`` shape, nested a level deeper."""
    inputs = {"primary": {"endpoint": "https://example.test", "api_key": ciphertext()}}
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], inputs)
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()


@pytest.mark.parametrize("prefix", SERVICE_PREFIXES)
def test_ciphertext_in_any_single_service_refuses_the_mint(tmp_path: Path, prefix: str):
    """Scan all three databases, whose rows are never read cross-service."""
    for name, filename in DATABASE_FILENAMES.items():
        rows = (ciphertext(),) if name == prefix else ()
        create_database(tmp_path, filename, *rows)

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_a_plaintext_only_deployment_still_mints(tmp_path: Path):
    """Mint where overrides exist but none of them is encrypted.

    Refusing on any non-scalar row would block the mint on deployments whose
    only nested overrides carry no secret at all, so the guard has to read the
    leaves rather than the row's shape.
    """
    create_database(
        tmp_path,
        DATABASE_FILENAMES["EXTENSIONS"],
        "a-plain-value",
        [{"PROVIDER": "pagerduty", "SEVERITY": "critical"}],
        {"primary": {"endpoint": "https://example.test"}},
    )
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_an_absent_override_table_counts_as_fresh(tmp_path: Path):
    """Mint against a database whose schema has never been applied."""
    engine = create_engine(f"sqlite:///{tmp_path / DATABASE_FILENAMES['EXTENSIONS']}")
    engine.connect().close()
    engine.dispose()
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def run_until_ended(
    directory: Path, seconds: float, **environment: str
) -> subprocess.TimeoutExpired:
    """Run the helper, end it after ``seconds``, and return what it wrote meanwhile.

    :param directory: The working directory passed to :func:`run_helper`.
    :param seconds: How long to let it run.
    :param environment: Variables to add over the base.
    :return: The expiry, carrying the stdout and stderr captured before it.
    """
    with pytest.raises(subprocess.TimeoutExpired) as expired:
        run_helper(directory, timeout=seconds, **environment)
    return expired.value


def captured(stream: bytes | str | None) -> str:
    """Return a stream captured by :class:`subprocess.TimeoutExpired` as text.

    :param stream: The stream, which the expiry carries as bytes even in text mode.
    :return: The decoded text, empty when nothing was captured.
    """
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream or ""


@pytest.mark.parametrize("prefix", SERVICE_PREFIXES)
def test_an_unreachable_database_is_waited_on_rather_than_refused(
    fresh_deployment: Path, prefix: str
):
    """Wait for a database that does not answer, for any one of the three.

    A first start runs while PMM's PostgreSQL is routinely still coming up, and
    a start that gave up would exit the container. The wait names the database
    and the last error, and says nothing of a key restore, which is not what an
    unreachable database calls for.
    """
    expired = run_until_ended(
        fresh_deployment, STILL_WAITING_SECONDS, **unreachable_environment(prefix)
    )
    stderr = captured(expired.stderr)

    assert f"Waiting for the {prefix.lower()} database" in stderr
    assert "last error:" in stderr
    assert "backup" not in stderr
    assert not captured(expired.stdout).strip()
    assert not persisted_key_path(fresh_deployment).exists()


def test_a_database_that_comes_up_during_the_wait_gets_the_key_minted(
    fresh_deployment: Path,
):
    """Mint once a database that did not answer at first answers and is fresh.

    The extensions database is aimed at a file in a directory that does not
    exist yet, which SQLite cannot open, and the directory is created once the
    helper has reported the wait. Reaching the key afterwards takes a second
    attempt at least.
    """
    late = fresh_deployment / "late"
    process = subprocess.Popen(
        [sys.executable, str(HELPER_SCRIPT)],
        cwd=str(fresh_deployment),
        env={
            **helper_environment(fresh_deployment),
            "EXTENSIONS__DATABASE__NAME": str(late / DATABASE_FILENAMES["EXTENSIONS"]),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        waiting = next(line for line in process.stderr if "Waiting for the" in line)
        late.mkdir()
        stdout, stderr = process.communicate(timeout=HELPER_STARTUP_ALLOWANCE_SECONDS)
    finally:
        process.kill()

    assert "Waiting for the extensions database" in waiting
    assert process.returncode == 0, stderr
    assert stdout.strip() == persisted_key_path(fresh_deployment).read_text(
        encoding="utf-8"
    )


def test_the_wait_is_reported_again_at_the_report_interval(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    """Report a long wait more than once, but not once per attempt.

    With attempts several times as frequent as reports, the count of reports in
    a watched window sits between two and one per report interval.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", SHORT_INTERVAL_SECONDS)
    monkeypatch.setattr(
        helper, "WAIT_REPORT_INTERVAL_SECONDS", SHORT_REPORT_INTERVAL_SECONDS
    )
    options = DatabaseOptions(NAME=str(tmp_path / "missing" / "extensions.db"))

    watch_probe(options)
    reports = capsys.readouterr().err.count("Waiting for the extensions database")

    assert (
        MIN_REPORTS
        <= reports
        <= WATCHED_WAIT_SECONDS / SHORT_REPORT_INTERVAL_SECONDS + 1
    )


def test_a_database_that_never_answers_is_retried_past_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stalled_database_port: int,
):
    """Abandon an attempt that hangs and report it, rather than hang on it.

    A port that completes the handshake and then says nothing is what a
    database that is still starting can look like. One attempt is bounded, so
    the wait goes on with the timeout as its last error. Every attempt is
    reported, so more than one timeout in the output is what shows the wait
    went on past the first rather than ending after it.
    """
    monkeypatch.setattr(helper, "PROBE_ATTEMPT_TIMEOUT_SECONDS", SHORT_INTERVAL_SECONDS)
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", SHORT_INTERVAL_SECONDS)
    monkeypatch.setattr(helper, "WAIT_REPORT_INTERVAL_SECONDS", 0)
    options = DatabaseOptions(
        ENGINE="postgresql+asyncpg",
        HOST="127.0.0.1",
        PORT=stalled_database_port,
        USER="extensions",
        NAME="extensions",
    )

    watch_probe(options)

    assert capsys.readouterr().err.count("last error: TimeoutError") >= MIN_REPORTS


def test_a_database_that_is_still_starting_is_waited_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    starting_database_port: int,
):
    """Retry a server that refuses the connection as starting up, not crash on it.

    PostgreSQL answers SQLSTATE 57P03 while it starts, and asyncpg raises that
    at connect time as its own exception rather than one SQLAlchemy wraps, so
    it is neither a ``SQLAlchemyError`` nor an ``OSError``. It is the first
    thing a database PMM is still bringing up answers, so it is waited on like
    one that does not answer at all.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", SHORT_INTERVAL_SECONDS)
    monkeypatch.setattr(helper, "WAIT_REPORT_INTERVAL_SECONDS", 0)
    options = DatabaseOptions(
        ENGINE="postgresql+asyncpg",
        HOST="127.0.0.1",
        PORT=starting_database_port,
        USER="extensions",
        NAME="extensions",
    )

    watch_probe(options)

    assert (
        capsys.readouterr().err.count("last error: CannotConnectNowError")
        >= MIN_REPORTS
    )


def test_the_retired_timeout_is_reported_and_not_read(fresh_deployment: Path):
    """Say once that the variable is no longer read, and mint as without it."""
    result = run_helper(fresh_deployment, **{RETIRED_TIMEOUT_INPUT: "3"})

    assert result.returncode == 0, result.stderr
    assert (
        result.stderr.count(f"{RETIRED_TIMEOUT_INPUT} is set but no longer read") == 1
    )
    assert result.stdout.strip()


def test_an_unparseable_stored_value_refuses_the_mint(tmp_path: Path):
    """Fail closed on a value the probe cannot decode, which may hide a token."""
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], "a-plain-value")
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])
    engine = create_engine(f"sqlite:///{tmp_path / DATABASE_FILENAMES['EXTENSIONS']}")
    with engine.begin() as connection:
        # Rewritten after the fact rather than inserted: the JSON column would
        # encode an unparseable Python string into perfectly parseable JSON.
        connection.exec_driver_sql("UPDATE settingoverride SET value = '{not json'")
    engine.dispose()

    result = run_helper(tmp_path)

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_an_unwritable_state_directory_never_serves_a_key(fresh_deployment: Path):
    """Exit non-zero rather than serve a key the next start cannot read back.

    A key that serves one run and is re-minted on the next orphans every row
    written under it, so an unpersistable key is worse than no key at all.
    """
    state = fresh_deployment / "state"
    state.mkdir()
    state.chmod(0o500)

    result = run_helper(fresh_deployment)

    assert result.returncode != 0
    assert not result.stdout.strip()


def test_two_concurrent_starts_converge_on_one_key(fresh_deployment: Path):
    """Serialise racing starts, so neither writes rows the other cannot read.

    Two side-cars sharing an initially empty state volume both observe no key.
    Atomic replacement alone would still leave them holding different values;
    the lock plus the re-read under it is what makes them agree.
    """

    async def both() -> list[subprocess.CompletedProcess[str]]:
        return list(
            await asyncio.gather(
                asyncio.to_thread(run_helper, fresh_deployment),
                asyncio.to_thread(run_helper, fresh_deployment),
            )
        )

    first, second = asyncio.run(both())

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout.strip() == second.stdout.strip()
    assert (
        persisted_key_path(fresh_deployment).read_text(encoding="utf-8")
        == first.stdout.strip()
    )


def test_a_peer_holding_the_state_lock_defers_then_refuses(
    fresh_deployment: Path, monkeypatch: pytest.MonkeyPatch
):
    """Wait for a peer's turn rather than probing and minting beside it.

    The convergence test above cannot see this by itself: because
    :func:`resolve` returns the value re-read from disk, an unlocked
    interleaving of write-write-read-read still converges, so deleting the lock
    leaves that test roughly a coin flip. Holding the lock from outside is what
    makes the dependence on it observable at all.

    The wait is bounded, so a peer that never releases is refused with a
    diagnostic naming the lock rather than leaving PID 1 blocked on it. Its
    remedy no longer names the retired probe timeout, which no longer moves
    the bound, and does not send the operator to separate state volumes:
    two key-less side-cars on separate volumes against the same databases hold
    separate locks and can each mint a different key.
    """
    monkeypatch.setattr(helper, "LOCK_WAIT_SECONDS", SHORT_LOCK_WAIT_SECONDS)
    directory = fresh_deployment / "state"
    lock_path = directory / helper.LOCK_FILENAME
    directory.mkdir()

    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        started = time.monotonic()
        with (
            pytest.raises(helper.EncryptionKeyError) as refused,
            helper.state_lock(directory),
        ):
            pass
        elapsed = time.monotonic() - started

    assert elapsed >= SHORT_LOCK_WAIT_SECONDS
    assert str(lock_path) in str(refused.value)
    assert RETIRED_TIMEOUT_INPUT not in str(refused.value)
    assert "own state volume" not in str(refused.value)
    assert "same ENCRYPTION_KEY" in str(refused.value)
    assert not persisted_key_path(fresh_deployment).exists()


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ciphertext(), id="scalar"),
        pytest.param([{"ROUTING_KEY": ciphertext()}], id="nested-in-list"),
        pytest.param({"a": {"b": ciphertext()}}, id="nested-in-mapping"),
        pytest.param([["deep", ciphertext()]], id="nested-in-nested-list"),
        pytest.param(f"https://user:{ciphertext()}@host:8443/", id="url-password"),
        pytest.param(
            [{"endpoint": f"https://user:{ciphertext()}@host:8443/"}],
            id="url-password-nested-in-list",
        ),
        pytest.param(
            {"PMM": {"endpoint": f"https://user:{ciphertext()}@host:8443/"}},
            id="url-password-nested-in-mapping",
        ),
        pytest.param(marked_ciphertext_value(), id="marked-scalar"),
        pytest.param(
            [{"ROUTING_KEY": marked_ciphertext_value()}], id="marked-nested-in-list"
        ),
        pytest.param(
            {"a": {"b": marked_ciphertext_value()}}, id="marked-nested-in-mapping"
        ),
        pytest.param(
            [["deep", marked_ciphertext_value()]], id="marked-nested-in-nested-list"
        ),
        pytest.param(
            f"https://user:{marked_ciphertext_value()}@host:8443/",
            id="marked-url-password",
        ),
        pytest.param(
            [{"endpoint": f"https://user:{marked_ciphertext_value()}@host:8443/"}],
            id="marked-url-password-nested-in-list",
        ),
        pytest.param(
            {
                "PMM": {
                    "endpoint": f"https://user:{marked_ciphertext_value()}@host:8443/"
                }
            },
            id="marked-url-password-nested-in-mapping",
        ),
    ],
)
def test_ciphertext_is_found_at_every_json_position(value: Any):
    """Walk the decoded value rather than testing the row, which is a container.

    A credential-URL leaf hides its token inside the userinfo segment, so
    ``is_encrypted`` on the whole string answers ``False`` — a deployment whose
    only encrypted data is an endpoint password would otherwise clear the mint
    path and come up green with those overrides silently reverted to YAML.

    The ``marked-*`` cases are the same positions under the envelope the write
    path writes today, where ``is_encrypted`` answers ``False`` for the leaf
    itself rather than only for its container. A probe that tested shape alone
    would read a deployment holding nothing but post-envelope rows as fresh.
    """
    assert helper.contains_ciphertext(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("a-plain-value", id="scalar"),
        pytest.param([{"PROVIDER": "pagerduty"}], id="nested-in-list"),
        pytest.param({"a": {"b": "c"}}, id="nested-in-mapping"),
        pytest.param(None, id="null"),
        pytest.param(42, id="number"),
        pytest.param([], id="empty-list"),
        pytest.param("https://user:hunter2@host:8443/", id="url-plaintext-password"),
        pytest.param("https://host:8443/", id="url-without-userinfo"),
        pytest.param("https://user:pw@[bad:ipv6/", id="url-unparseable"),
        pytest.param("extensions.enc.v1.operator-secret", id="marker-over-a-plaintext"),
        pytest.param(
            f"{PRE_RENAME_CIPHERTEXT_MARKER}operator-secret",
            id="pre-rename-marker-over-a-plaintext",
        ),
    ],
)
def test_a_value_with_no_token_is_not_read_as_ciphertext(value: Any):
    """Leave a plaintext deployment mintable, which is the common case.

    ``marker-over-a-plaintext`` is a legacy value that merely begins with the
    marker. The envelope's payload check refuses it, so it is classified by
    what it is rather than by the prefix it happens to carry — the probe must
    not be fooled into refusing a mint by an operator-chosen token either.
    """
    assert not helper.contains_ciphertext(value)


def test_the_minted_key_is_a_usable_key_of_the_documented_shape():
    """Mint 32 random bytes in the URL-safe alphabet, and prove the cipher works.

    Those two properties are what make a key minted here interchangeable with
    one an operator generates by hand; the width is what ``openssl rand -hex 32``
    gets wrong. Asserted as properties rather than against ``make
    encryption-key``'s text, which would ratify the recipe rather than test it —
    so this does not claim to catch that target being rewritten.
    """
    minted = helper.mint_key()
    cipher = Fernet(minted.encode())
    minted_characters = set(minted)

    assert len(base64.urlsafe_b64decode(minted)) == helper.KEY_BYTES
    assert minted_characters
    assert minted_characters <= URL_SAFE_ALPHABET
    assert not is_encrypted(minted)
    assert cipher.decrypt(cipher.encrypt(b"probe")) == b"probe"


def test_a_corrupted_persisted_key_is_refused_rather_than_served(
    fresh_deployment: Path,
):
    """Report a state file the cipher cannot use, instead of passing it on.

    Serving it exits 0 and hands every supervised program a key the settings
    validator rejects, which is precisely the illegible failure this helper
    exists to replace: five children crash-looping and nothing from PID 1
    saying why.
    """
    run_helper(fresh_deployment)
    persisted_key_path(fresh_deployment).write_text(
        "not-a-fernet-key", encoding="utf-8"
    )

    result = run_helper(fresh_deployment)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert "ENCRYPTION_KEY" in result.stderr


def test_the_helper_reaches_the_image():
    """Assert the helper is copied in; bundle.tgz carries no sidecar/ file.

    A missing ``COPY`` surfaces only as a container that dies on start, with
    the entrypoint's command substitution reporting a missing file rather than
    anything about encryption.
    """
    containerfile = SIDECAR_DIR / "Containerfile.sidecar"

    assert "./sidecar/encryption_key.py ./encryption_key.py" in containerfile.read_text(
        encoding="utf-8"
    )


def test_the_refusal_names_the_state_directory_and_the_restore_path(tmp_path: Path):
    """Say what an operator has to do, which supervisord's status cannot show."""
    create_database(tmp_path, DATABASE_FILENAMES["EXTENSIONS"], ciphertext())
    create_database(tmp_path, DATABASE_FILENAMES["INVENTORY"])
    create_database(tmp_path, DATABASE_FILENAMES["TASKS"])

    result = run_helper(tmp_path)

    assert "ENCRYPTION_KEY" in result.stderr
    assert str(tmp_path / "state") in result.stderr


def test_no_diagnostic_is_written_to_the_channel_the_key_is_read_from(
    fresh_deployment: Path,
):
    """Keep stdout to the key alone, which the entrypoint captures wholesale."""
    result = run_helper(fresh_deployment)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    persisted = persisted_key_path(fresh_deployment).read_text(encoding="utf-8")
    assert result.stdout == f"{persisted}\n"
