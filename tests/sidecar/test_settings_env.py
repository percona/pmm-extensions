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
"""Verify the side-car entrypoint's deployment-input expansion."""

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from cryptography.fernet import Fernet

from app.core.auth.config import AuthSettings
from app.core.config import Settings
from app.extensions.config import ExtensionsSettings
from app.inventory.config import InventorySettings
from app.tasks.config import TasksSettings
from tests.sidecar.conftest import SETTINGS_ENV_HELPER, SIDECAR_DIR

SUPERVISORD_EXPANSION = re.compile(r"%\(ENV_([A-Za-z0-9_]+)\)s")
UNCONDITIONAL_EXPORT = re.compile(r"^export ([A-Z_][A-Z0-9_]*)=", re.MULTILINE)
BLANK_CLEARED_NAMES_ARRAY = re.compile(
    r"blank_cleared_names=\(\s*(.*?)\s*\)", re.DOTALL
)

CALLER_SHELL_OPTIONS = "set -o errexit -o nounset -o pipefail"
"""The options ``entrypoint.sh`` has active when it sources the helper."""

SHELL_LOCAL_NAMES = frozenset({"PATH", "PWD", "SHLVL", "_"})

DATABASE_PREFIXES = ("EXTENSIONS", "INVENTORY", "TASKS")

BLANK_BEAT_URI = {"CELERY__BEAT_DBURI": ""}
"""A blank inherited beat URI, the shape the helper has to clear."""

FERNET_KEY = Fernet.generate_key().decode("ascii")
"""A real key rather than a placeholder, so these cases carry a value the
settings classes would accept."""

WAITING_FOR_THE_KEY = "Waiting for PMM to publish SECRET_KEY"
"""What the gate says while a mounted secrets directory holds no key yet."""

POLLS_OBSERVED_SECONDS = 1.5
"""Longer than the gate's one-second poll, so a gate that is still running
afterwards read the directory again and chose to keep waiting."""

KEY_IS_PUBLISHED = "SECRET_KEY is published"
"""What the gate says when it first finds the key, before it reads the rest."""

GATE_EXIT_SECONDS = 15
"""Ample for the gate to see a published key, settle and finish."""


def source_helper(**inputs: str) -> subprocess.CompletedProcess[str]:
    """Run the helper from an otherwise empty environment.

    :param inputs: The deployment inputs to place in the environment.
    :return: The completed ``bash`` run, whose stdout is a NUL-delimited ``env``.
    """
    return source_helper_then("", **inputs)


def source_helper_then(command: str, **inputs: str) -> subprocess.CompletedProcess[str]:
    """Run the helper, then ``command``, from an otherwise empty environment.

    ``entrypoint.sh`` sources the helper with ``.``, so the functions it defines
    stay in scope for the rest of PID 1; this is the shape in which the mint
    step reaches the Grafana fan-out.

    :param command: The shell to run once the helper has been sourced.
    :param inputs: The deployment inputs to place in the environment.
    :return: The completed ``bash`` run, whose stdout is a NUL-delimited ``env``.
    """
    return subprocess.run(
        ["bash", "-c", helper_script(command)],
        env={"PATH": os.environ["PATH"], **inputs},
        capture_output=True,
        text=True,
        check=False,
    )


def helper_script(command: str = "") -> str:
    """Return the shell that sources the helper the way ``entrypoint.sh`` does.

    :param command: The shell to run once the helper has been sourced.
    :return: The script, ending in a NUL-delimited ``env``.
    """
    return "\n".join(
        [
            CALLER_SHELL_OPTIONS,
            f". {shlex.quote(str(SETTINGS_ENV_HELPER))}",
            command,
            "env -0",
        ]
    )


@contextmanager
def waiting_helper(**inputs: str) -> Iterator[tuple[subprocess.Popen[str], str]]:
    """Start the helper where its key gate has to wait, and yield it once it speaks.

    The first stderr line is read before yielding, so a case acts only after the
    gate has reached its wait. A gate that exits instead yields an empty line
    and a finished process rather than hanging the case.

    :param inputs: The deployment inputs to place in the environment.
    :return: The running helper and the first line it wrote to stderr.
    """
    process = subprocess.Popen(
        ["bash", "-c", helper_script()],
        env={"PATH": os.environ["PATH"], **inputs},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    try:
        yield process, process.stderr.readline()
    finally:
        process.kill()
        process.communicate()


def finish(
    process: subprocess.Popen[str], first_line: str
) -> subprocess.CompletedProcess[str]:
    """Let a waiting helper run to its end and return the whole run.

    :param process: A helper started by :func:`waiting_helper`.
    :param first_line: The stderr line :func:`waiting_helper` already consumed.
    :return: The completed run, with that line restored to its stderr.
    """
    stdout, stderr = process.communicate(timeout=GATE_EXIT_SECONDS)
    return subprocess.CompletedProcess(
        process.args, process.returncode, stdout, first_line + stderr
    )


def exported(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """Return the environment the helper exported.

    :param result: A completed :func:`source_helper` run.
    :return: The exported variables.
    """
    assert result.returncode == 0, result.stderr
    return dict(
        entry.split("=", 1) for entry in result.stdout.split("\0") if "=" in entry
    )


def write_secrets(tmp_path: Path, **files: str) -> str:
    """Write the named secret files and return the directory to mount.

    :param tmp_path: The per-test temporary directory.
    :param files: Each canonical name a file supplies, mapped to its contents.
    :return: The directory ``SECRETS_DIR`` should name.
    """
    directory = tmp_path / "secrets"
    directory.mkdir(exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return str(directory)


def managed_canonical_names() -> frozenset[str]:
    """Return the canonical names the script's ``blank_cleared_names`` loop manages.

    Parsed from the script rather than duplicated as a literal, so it can
    never drift from the list :func:`apply_environment` relies on to isolate
    a test from whatever one of these names the pytest process itself
    happened to inherit.

    :return: The managed canonical names.
    """
    match = BLANK_CLEARED_NAMES_ARRAY.search(
        SETTINGS_ENV_HELPER.read_text(encoding="utf-8")
    )
    assert match, "blank_cleared_names array not found in settings-env.sh"
    return frozenset(match.group(1).split())


def apply_environment(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    """Replay a subprocess's exported environment onto the pytest process.

    A managed name the subprocess left unexported is meant to fall through to
    a mounted file or the baked default -- but the pytest process is not
    otherwise isolated from its own ambient environment, so a managed name
    already set there (from the shell a developer or CI job runs pytest in)
    would outrank both. Every managed name is cleared first so replaying the
    export is the only source left.

    :param monkeypatch: The environment patcher.
    :param environment: The subprocess's exported environment, from :func:`exported`.
    """
    for name in managed_canonical_names():
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        if name not in SHELL_LOCAL_NAMES:
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize("secret_key", [{}, {"SECRET_KEY": ""}], ids=["unset", "empty"])
def test_missing_secret_key_aborts_with_an_actionable_message(
    secret_key: dict[str, str],
):
    """Reject a missing key at once where the deployment supplies its own.

    With no secrets directory nothing will publish a key later, so both ways of
    supplying one are named and the start ends.
    """
    result = source_helper(**secret_key)

    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr
    assert "openssl rand -hex 32" in result.stderr
    assert "SECRETS_DIR" in result.stderr


def test_database_host_and_port_defaults_are_exported():
    """Assert the host and port defaults reach supervisord's own environment.

    ``%(ENV_...)s`` expands from there, so a bare assignment would leave the
    migration wait loops pointing at undefined names.
    """
    environment = exported(source_helper(SECRET_KEY="k"))

    assert environment["EXTENSIONS_DB_HOST"] == "pmm-server"
    assert environment["EXTENSIONS_DB_PORT"] == "5432"
    assert all(
        environment[f"{prefix}__DATABASE__{field}"]
        for prefix in DATABASE_PREFIXES
        for field in ("HOST", "PORT")
    )


def test_supervisord_expansions_are_exported_unconditionally():
    """Assert every name supervisord expands is exported outside a conditional.

    An undefined ``%(ENV_...)s`` name aborts supervisord rather than starting it.
    """
    expansions = set(
        SUPERVISORD_EXPANSION.findall(
            (SIDECAR_DIR / "supervisord.conf").read_text(encoding="utf-8")
        )
    )

    assert expansions
    assert expansions <= set(
        UNCONDITIONAL_EXPORT.findall(SETTINGS_ENV_HELPER.read_text(encoding="utf-8"))
    )


def test_password_reaches_every_canonical_destination():
    """Assert one input fans out to the three services and nowhere else."""
    environment = exported(source_helper(SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw"))

    assert [
        environment[f"{prefix}__DATABASE__PASSWORD"] for prefix in DATABASE_PREFIXES
    ] == [
        "pw",
        "pw",
        "pw",
    ]
    assert "CELERY__BEAT_DBURI" not in environment


def test_explicit_canonical_variable_outranks_the_derived_one():
    """Keep the value an operator sets directly on one service."""
    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_DB_HOST="a", TASKS__DATABASE__HOST="b")
    )

    assert environment["TASKS__DATABASE__HOST"] == "b"
    assert environment["EXTENSIONS__DATABASE__HOST"] == "a"


def test_an_inherited_beat_uri_is_neither_read_nor_rewritten():
    """Leave an explicit beat store exactly as it arrived, credentials included.

    The script no longer derives the URI, so the only guarantee it still owes is
    that supplying one changes nothing else about the environment it exports.
    """
    beat_uri = "postgresql://x:secret@y/z"
    baseline = exported(source_helper(SECRET_KEY="k"))

    environment = exported(source_helper(SECRET_KEY="k", CELERY__BEAT_DBURI=beat_uri))

    assert environment["CELERY__BEAT_DBURI"] == beat_uri
    assert {
        name: value
        for name, value in environment.items()
        if name != "CELERY__BEAT_DBURI"
    } == baseline


def test_neither_a_beat_store_nor_a_password_is_exported_by_default():
    """Omit both the beat store and the password, leaving the settings to resolve."""
    environment = exported(source_helper(SECRET_KEY="k"))

    assert "CELERY__BEAT_DBURI" not in environment
    assert not [name for name in environment if name.endswith("__DATABASE__PASSWORD")]


GRAFANA_FAN_OUT_NAMES = (
    "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN",
    "PMM__API_KEY",
    "TASKS__NOMAD__API_KEY",
)
"""Every canonical name ``export_grafana_token`` resolves from one token."""


def test_grafana_token_reaches_every_canonical_destination():
    """Assert one token serves Grafana sign-in, the PMM syncer and the Nomad executor."""
    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_GRAFANA_TOKEN="glsa_x")
    )

    assert {
        name: environment.get(name) for name in GRAFANA_FAN_OUT_NAMES
    } == dict.fromkeys(GRAFANA_FAN_OUT_NAMES, "glsa_x")


def test_no_grafana_variables_without_a_token():
    """Leave the profile's empty token standing when no token is supplied."""
    environment = exported(source_helper(SECRET_KEY="k"))

    assert not [name for name in GRAFANA_FAN_OUT_NAMES if name in environment]


def test_the_grafana_fan_out_survives_as_a_callable_function():
    """Expose the fan-out as a function, which is what lets the mint step reuse it."""
    environment = exported(
        source_helper_then("export_grafana_token glsa_minted", SECRET_KEY="k")
    )

    assert {
        name: environment.get(name) for name in GRAFANA_FAN_OUT_NAMES
    } == dict.fromkeys(GRAFANA_FAN_OUT_NAMES, "glsa_minted")


@pytest.mark.parametrize("mounted", GRAFANA_FAN_OUT_NAMES)
def test_a_mounted_single_name_outranks_a_minted_token(tmp_path: Path, mounted: str):
    """Leave a mounted name to its file while the others take the minted value."""
    secrets_dir = write_secrets(tmp_path, **{mounted: "from-file"})

    environment = exported(
        source_helper_then(
            "export_grafana_token glsa_minted", SECRET_KEY="k", SECRETS_DIR=secrets_dir
        )
    )

    assert mounted not in environment
    for name in GRAFANA_FAN_OUT_NAMES:
        if name != mounted:
            assert environment[name] == "glsa_minted"


@pytest.mark.parametrize("explicit", GRAFANA_FAN_OUT_NAMES)
def test_an_explicit_single_name_outranks_a_minted_token(explicit: str):
    """Leave an operator's own value standing, which the mint must never displace."""
    environment = exported(
        source_helper_then(
            "export_grafana_token glsa_minted",
            SECRET_KEY="k",
            **{explicit: "glsa_explicit"},
        )
    )

    assert environment[explicit] == "glsa_explicit"
    for name in GRAFANA_FAN_OUT_NAMES:
        if name != explicit:
            assert environment[name] == "glsa_minted"


@pytest.mark.parametrize(
    "endpoint", ["https://h:1", "https://h:1/"], ids=["bare", "trailing-slash"]
)
def test_pmm_endpoint_reaches_the_client_and_the_grafana_provider(endpoint: str):
    """Append PMM's ``/graph`` prefix, trimming a trailing slash that would double it."""
    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_PMM_ENDPOINT=endpoint)
    )

    assert environment["PMM__ENDPOINT"] == "https://h:1"
    assert environment["AUTH__PROVIDER__GRAFANA__ENDPOINT"] == "https://h:1/graph"


def test_nomad_endpoint_is_forwarded_verbatim():
    """Forward the Nomad endpoint verbatim, since its credentials live in the URL."""
    endpoint = "https://a:b@h/nomad"

    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_NOMAD_ENDPOINT=endpoint)
    )

    assert environment["TASKS__NOMAD__ENDPOINT"] == endpoint


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_derived_environment_resolves_against_the_baked_profile(
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert the shell contract and the settings contract agree at their seam."""
    environment = exported(source_helper(SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw"))
    for name, value in environment.items():
        if name not in SHELL_LOCAL_NAMES:
            monkeypatch.setenv(name, value)

    assert "CELERY__BEAT_DBURI" not in environment
    assert (
        Settings().CELERY.beat_dburi
        == "postgresql+psycopg2://pmm_extensions:pw@pmm-server:5432/pmm_extensions"
    )
    assert (
        AuthSettings().PROVIDER["grafana"].service_account_token.get_secret_value()
        == ""
    )
    assert [
        settings_class().DATABASE.PASSWORD.get_secret_value()
        for settings_class in (ExtensionsSettings, InventorySettings, TasksSettings)
    ] == ["pw", "pw", "pw"]


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_reserved_character_password_reaches_a_usable_service_dsn(
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert the raw password each service receives still yields a parseable DSN.

    The helper exports the password raw, so the encoding the beat URI gets from
    the shell says nothing about the DSN the settings classes build from it.
    """
    password = "p@ss:w/rd"
    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_DB_PASSWORD=password)
    )
    for name, value in environment.items():
        if name not in SHELL_LOCAL_NAMES:
            monkeypatch.setenv(name, value)

    assert [
        unquote(urlsplit(settings_class().DATABASE.URL).password)
        for settings_class in (ExtensionsSettings, InventorySettings, TasksSettings)
    ] == [password, password, password]


def test_secret_key_from_a_file_starts_the_script_without_exporting_it(tmp_path: Path):
    """Clear the gate from a mounted key, leaving each process to read the file."""
    secrets_dir = write_secrets(tmp_path, SECRET_KEY="from-file")

    environment = exported(source_helper(SECRETS_DIR=secrets_dir))

    assert "SECRET_KEY" not in environment


def test_the_gate_waits_for_pmm_to_publish_the_key(tmp_path: Path):
    """Start once the key arrives, having said what the wait was for.

    PMM owns the key and writes it while it starts, which Compose does not order
    ahead of this container. Generating a key is the one piece of advice that is
    wrong there, so the wait never gives it.
    """
    secrets_dir = write_secrets(tmp_path)

    with waiting_helper(SECRETS_DIR=secrets_dir) as (process, first_line):
        write_secrets(tmp_path, SECRET_KEY="published-by-pmm")
        result = finish(process, first_line)

    environment = exported(result)
    assert WAITING_FOR_THE_KEY in first_line
    assert secrets_dir in first_line
    assert "openssl" not in result.stderr
    assert environment["EXTENSIONS_DB_HOST"] == "pmm-server"
    assert "SECRET_KEY" not in environment


def test_a_file_published_just_after_the_key_is_still_read(tmp_path: Path):
    """Read the directory again after the key appears, not at that instant.

    PMM publishes its files as consecutive renames with the key first, so the
    instant the key appears is the one instant the rest may still be missing.
    The second file is written only once the gate has reported the key, so a
    gate that settled on the read which found the key cannot have seen it.
    """
    secrets_dir = write_secrets(tmp_path)

    with waiting_helper(SECRETS_DIR=secrets_dir) as (process, first_line):
        assert process.stderr is not None
        write_secrets(tmp_path, SECRET_KEY="published-by-pmm")
        reported = first_line
        while KEY_IS_PUBLISHED not in reported:
            line = process.stderr.readline()
            assert line, f"the gate exited before it reported the key: {reported}"
            reported += line
        write_secrets(tmp_path, EXTENSIONS__DATABASE__HOST="published-later")
        environment = exported(finish(process, reported))

    assert environment["EXTENSIONS_DB_HOST"] == "published-later"
    assert "EXTENSIONS__DATABASE__HOST" not in environment


def absent_directory(tmp_path: Path) -> str:
    """Name a secrets directory nothing has created yet.

    :param tmp_path: The per-test temporary directory.
    :return: The path ``SECRETS_DIR`` should name.
    """
    return str(tmp_path / "not-mounted-yet")


def escaping_key_symlink(tmp_path: Path) -> str:
    """Mount a key that resolves outside the directory, which no settings class reads.

    :param tmp_path: The per-test temporary directory.
    :return: The directory ``SECRETS_DIR`` should name.
    """
    outside = tmp_path / "outside"
    outside.write_text("escaped-value", encoding="utf-8")
    secrets_dir = write_secrets(tmp_path)
    (Path(secrets_dir) / "SECRET_KEY").symlink_to(outside)
    return secrets_dir


@pytest.mark.parametrize(
    "build_secrets_dir",
    [
        absent_directory,
        write_secrets,
        lambda tmp_path: write_secrets(tmp_path, SECRET_KEY=""),
        lambda tmp_path: write_secrets(tmp_path, SECRET_KEY="\n  \n"),
        escaping_key_symlink,
    ],
    ids=[
        "no-directory",
        "no-file",
        "empty-file",
        "whitespace-only",
        "escaping-symlink",
    ],
)
def test_the_gate_keeps_waiting_while_no_file_holds_a_key(
    tmp_path: Path, build_secrets_dir: Callable[[Path], str]
):
    """Hold the start for as long as the directory supplies no usable key.

    A blank file is what a key looks like part-way through being published, and
    the settings classes reject an empty key outright. Admitting one would
    trade this gate's one line for five crashing children.
    """
    secrets_dir = build_secrets_dir(tmp_path)

    with (
        waiting_helper(SECRETS_DIR=secrets_dir) as (process, first_line),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        process.wait(timeout=POLLS_OBSERVED_SECONDS)

    assert WAITING_FOR_THE_KEY in first_line
    assert "openssl" not in first_line


def test_the_gate_names_a_key_file_it_cannot_read(tmp_path: Path):
    """Say that the key is there and unreadable, rather than that PMM is late.

    A side-car outside group 0 sees exactly this, and waiting for PMM to
    publish a key it has already published would never end or explain itself.
    """
    secrets_dir = write_secrets(tmp_path, SECRET_KEY="published-by-pmm")
    (Path(secrets_dir) / "SECRET_KEY").chmod(0o000)

    with (
        waiting_helper(SECRETS_DIR=secrets_dir) as (process, first_line),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        process.wait(timeout=POLLS_OBSERVED_SECONDS)

    assert "cannot read it" in first_line
    assert secrets_dir in first_line
    assert WAITING_FOR_THE_KEY not in first_line
    assert "Permission denied" not in first_line


def test_a_blank_secret_key_variable_does_not_shadow_the_file(tmp_path: Path):
    """Clear a blank inherited key, which every child would read over the file."""
    secrets_dir = write_secrets(tmp_path, SECRET_KEY="from-file")

    environment = exported(source_helper(SECRET_KEY="", SECRETS_DIR=secrets_dir))

    assert "SECRET_KEY" not in environment


def test_an_encryption_key_from_a_file_is_not_exported(tmp_path: Path):
    """Leave a mounted key to the file, which every settings class reads itself."""
    secrets_dir = write_secrets(tmp_path, ENCRYPTION_KEY=FERNET_KEY)

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment
    assert "ENCRYPTION_KEY" not in environment


def test_a_lowercase_encryption_key_file_is_not_exported(tmp_path: Path):
    """Match the file case-insensitively, the way the settings source matches it."""
    secrets_dir = write_secrets(tmp_path, encryption_key=FERNET_KEY)

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment
    assert "ENCRYPTION_KEY" not in environment


def test_a_blank_encryption_key_does_not_shadow_the_file(tmp_path: Path):
    """Clear a blank inherited key, which was measured to shadow a valid file.

    A blank environment variable still counts as supplied, so without the clear
    it outranks the mounted file and the settings classes refuse to start on the
    empty value.
    """
    secrets_dir = write_secrets(tmp_path, ENCRYPTION_KEY=FERNET_KEY)

    environment = exported(
        source_helper(SECRET_KEY="k", ENCRYPTION_KEY="", SECRETS_DIR=secrets_dir)
    )

    assert environment
    assert "ENCRYPTION_KEY" not in environment


def test_an_explicit_encryption_key_is_left_exported():
    """Keep an operator's own key, which no lower channel may displace."""
    environment = exported(source_helper(SECRET_KEY="k", ENCRYPTION_KEY=FERNET_KEY))

    assert environment["ENCRYPTION_KEY"] == FERNET_KEY


def test_no_encryption_key_is_exported_when_nothing_supplies_one():
    """Leave the name unset, which is what sends the entrypoint to its helper."""
    environment = exported(source_helper(SECRET_KEY="k"))

    assert environment
    assert "ENCRYPTION_KEY" not in environment


def test_a_blank_variable_does_not_shadow_the_file_it_defers_to(tmp_path: Path):
    """Clear a blank inherited name, which pydantic would rank above the file."""
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="from-file")

    environment = exported(
        source_helper(
            SECRET_KEY="k",
            EXTENSIONS_DB_PASSWORD="pw",
            SECRETS_DIR=secrets_dir,
            EXTENSIONS__DATABASE__PASSWORD="",
        )
    )

    assert environment
    assert "EXTENSIONS__DATABASE__PASSWORD" not in environment


@pytest.mark.parametrize(
    "mount",
    [{"CELERY__BEAT_DBURI": "postgresql://mounted@host:5432/beat"}, {}],
    ids=["mounted", "unmounted"],
)
def test_a_blank_beat_uri_is_cleared(tmp_path: Path, mount: dict[str, str]):
    """Clear a blank inherited beat URI, which outranks every source below it.

    An empty string counts as supplied on the environment side, so leaving it in
    place ranks it above both a mounted file and the derived default, and an empty
    URL fails settings validation rather than falling through to either.
    """
    secrets_dir = write_secrets(tmp_path, **mount)

    environment = exported(
        source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir, **BLANK_BEAT_URI)
    )

    assert "CELERY__BEAT_DBURI" not in environment


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_a_blank_beat_uri_still_resolves_the_derived_store(
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert a blank inherited beat URI leaves the derived store reachable.

    Uncleared, the empty string reaches the settings classes and fails URL
    validation, taking every supervisord child down with it.
    """
    environment = exported(
        source_helper(SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw", **BLANK_BEAT_URI)
    )
    for name, value in environment.items():
        if name not in SHELL_LOCAL_NAMES:
            monkeypatch.setenv(name, value)

    assert (
        Settings().CELERY.beat_dburi
        == "postgresql+psycopg2://pmm_extensions:pw@pmm-server:5432/pmm_extensions"
    )


def test_a_mounted_canonical_name_is_left_unexported(tmp_path: Path):
    """Suppress only the derived export the file supplies, leaving its siblings."""
    secrets_dir = write_secrets(tmp_path, TASKS__DATABASE__PASSWORD="from-file")

    environment = exported(
        source_helper(
            SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw", SECRETS_DIR=secrets_dir
        )
    )

    assert "TASKS__DATABASE__PASSWORD" not in environment
    assert environment["EXTENSIONS__DATABASE__PASSWORD"] == "pw"
    assert environment["INVENTORY__DATABASE__PASSWORD"] == "pw"


def test_an_explicit_variable_outranks_the_file_and_the_derived_value(tmp_path: Path):
    """Keep an operator's own variable ahead of both lower levels.

    The seed is skipped too, so the wait loops follow the value in force rather
    than the shadowed file.
    """
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__HOST="from-file")

    environment = exported(
        source_helper(
            SECRET_KEY="k",
            EXTENSIONS__DATABASE__HOST="from-env",
            EXTENSIONS_DB_HOST="from-raw",
            SECRETS_DIR=secrets_dir,
        )
    )

    assert environment["EXTENSIONS__DATABASE__HOST"] == "from-env"
    assert environment["EXTENSIONS_DB_HOST"] == "from-raw"


def test_mounted_host_and_port_seed_the_supervisord_wait_loops(tmp_path: Path):
    """Point the migrate wait loops at the database the services connect to."""
    secrets_dir = write_secrets(
        tmp_path,
        EXTENSIONS__DATABASE__HOST="db.internal",
        EXTENSIONS__DATABASE__PORT="6543",
    )

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment["EXTENSIONS_DB_HOST"] == "db.internal"
    assert environment["EXTENSIONS_DB_PORT"] == "6543"
    assert "EXTENSIONS__DATABASE__HOST" not in environment
    assert "EXTENSIONS__DATABASE__PORT" not in environment
    assert environment["INVENTORY__DATABASE__HOST"] == "db.internal"
    assert environment["TASKS__DATABASE__PORT"] == "6543"


def test_a_mounted_password_never_reaches_the_environment(tmp_path: Path):
    """Leave a mounted password in its file, with no derived URI carrying it out.

    This is the whole point of the mount: celery-beat reads the same file through
    the settings classes, so nothing has to put the password back in the
    environment to reach it.
    """
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="p@ss:w/rd")

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment
    assert "EXTENSIONS__DATABASE__PASSWORD" not in environment
    assert "CELERY__BEAT_DBURI" not in environment


def test_a_mounted_password_supplies_only_the_name_it_is_named_for(tmp_path: Path):
    """Leave sibling services unsupplied in the shell, since only ``EXTENSIONS_DB_PASSWORD`` fans out.

    The settings classes still read a global ``DATABASE__PASSWORD`` mount for every
    service; this test pins what the shell exports, not what settings resolve.
    """
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="from-file")

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert "INVENTORY__DATABASE__PASSWORD" not in environment
    assert "TASKS__DATABASE__PASSWORD" not in environment


def test_a_file_value_is_stripped(tmp_path: Path):
    """Strip a file the way the settings classes strip it, so both read one value."""
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__HOST="  padded  \n")

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment["EXTENSIONS_DB_HOST"] == "padded"
    assert environment["INVENTORY__DATABASE__HOST"] == "padded"


def test_an_empty_file_counts_as_a_supplied_value(tmp_path: Path):
    """Treat a blank mount as supplied, which is how the settings classes read it."""
    secrets_dir = write_secrets(tmp_path, EXTENSIONS__DATABASE__PASSWORD="")

    environment = exported(
        source_helper(
            SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw", SECRETS_DIR=secrets_dir
        )
    )

    assert "EXTENSIONS__DATABASE__PASSWORD" not in environment
    assert environment["INVENTORY__DATABASE__PASSWORD"] == "pw"
    assert environment["TASKS__DATABASE__PASSWORD"] == "pw"


def test_a_symlink_escaping_the_directory_does_not_supply_a_name(tmp_path: Path):
    """Ignore an escaping symlink, so the export the settings classes need survives."""
    outside = tmp_path / "outside"
    outside.write_text("escaped-value", encoding="utf-8")
    secrets_dir = write_secrets(tmp_path)
    (Path(secrets_dir) / "EXTENSIONS__DATABASE__HOST").symlink_to(outside)

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir))

    assert environment["EXTENSIONS__DATABASE__HOST"] == "pmm-server"
    assert environment["EXTENSIONS_DB_HOST"] == "pmm-server"


def test_a_kubernetes_projected_secret_is_supplied(tmp_path: Path):
    """Resolve the ``..data`` layout Kubernetes projects, which stays inside."""
    secrets_dir = Path(write_secrets(tmp_path))
    revision = secrets_dir / "..2026_01_01"
    revision.mkdir()
    (revision / "SECRET_KEY").write_text("projected-value", encoding="utf-8")
    (secrets_dir / "..data").symlink_to("..2026_01_01")
    (secrets_dir / "SECRET_KEY").symlink_to("..data/SECRET_KEY")

    environment = exported(source_helper(SECRETS_DIR=str(secrets_dir)))

    assert "SECRET_KEY" not in environment


def test_a_lowercase_file_name_supplies_the_canonical_name(tmp_path: Path):
    """Match file names case-insensitively, as the settings source matches them.

    An exact-match lookup would export the derived value instead, and an
    exported name outranks every secret file.
    """
    secrets_dir = write_secrets(tmp_path, extensions__database__password="from-file")

    environment = exported(
        source_helper(
            SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="raw", SECRETS_DIR=secrets_dir
        )
    )

    assert "EXTENSIONS__DATABASE__PASSWORD" not in environment
    assert environment["INVENTORY__DATABASE__PASSWORD"] == "raw"
    assert environment["TASKS__DATABASE__PASSWORD"] == "raw"


def test_a_lowercase_secret_key_file_satisfies_the_gate(tmp_path: Path):
    """Clear the gate from a lowercased key file, which the settings classes read."""
    secrets_dir = write_secrets(tmp_path, secret_key="from-file")

    environment = exported(source_helper(SECRETS_DIR=secrets_dir))

    assert "SECRET_KEY" not in environment


def test_a_secret_in_a_subdirectory_does_not_supply_a_name(tmp_path: Path):
    """Ignore a nested secret, whose settings-side key can match no field."""
    secrets_dir = Path(write_secrets(tmp_path))
    nested = secrets_dir / "sub"
    nested.mkdir()
    (nested / "EXTENSIONS__DATABASE__HOST").write_text("nested-value", encoding="utf-8")

    environment = exported(source_helper(SECRET_KEY="k", SECRETS_DIR=str(secrets_dir)))

    assert environment["EXTENSIONS__DATABASE__HOST"] == "pmm-server"


@pytest.mark.parametrize(
    "build_secrets_dir",
    [
        lambda tmp_path: str(tmp_path / "missing"),
        write_secrets,
        lambda tmp_path: write_secrets(tmp_path, UNRELATED="value"),
    ],
    ids=["absent", "empty", "unrelated-file"],
)
def test_an_unusable_secrets_directory_changes_nothing(
    tmp_path: Path, build_secrets_dir: Callable[[Path], str]
):
    """Leave today's exported environment untouched when no file matches a name."""
    secrets_dir = build_secrets_dir(tmp_path)
    baseline = exported(source_helper(SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw"))

    environment = exported(
        source_helper(
            SECRET_KEY="k", EXTENSIONS_DB_PASSWORD="pw", SECRETS_DIR=secrets_dir
        )
    )

    assert {
        name: value for name, value in environment.items() if name != "SECRETS_DIR"
    } == baseline


@pytest.mark.usefixtures("embedded_profile_cwd")
def test_a_mounted_secret_resolves_through_the_shell_into_the_settings_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Assert the shell's suppression and the settings classes' file read compose."""
    password = "p@ss:w/rd"
    secrets_dir = write_secrets(
        tmp_path, SECRET_KEY="from-file", EXTENSIONS__DATABASE__PASSWORD=password
    )
    environment = exported(source_helper(SECRETS_DIR=secrets_dir))
    for name in ("SECRET_KEY", "EXTENSIONS__DATABASE__PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        if name not in SHELL_LOCAL_NAMES:
            monkeypatch.setenv(name, value)

    assert not [name for name in environment if "PASSWORD" in name]
    assert "CELERY__BEAT_DBURI" not in environment
    assert (
        Settings(_secrets_dir=secrets_dir).SECRET_KEY.get_secret_value() == "from-file"
    )
    assert (
        ExtensionsSettings(
            _secrets_dir=secrets_dir
        ).DATABASE.PASSWORD.get_secret_value()
        == password
    )
    assert (
        Settings(_secrets_dir=secrets_dir).CELERY.beat_dburi
        == "postgresql+psycopg2://pmm_extensions:p%40ss%3Aw%2Frd@pmm-server:5432/pmm_extensions"
    )


class TestBlankNamesWhoseGuardMightNeverFire:
    """Clear a canonical name inherited blank while its ``EXTENSIONS_*`` guard is idle.

    ``export_canonical`` only clears a blank when it actually runs, and four
    guards skip calling it whenever their raw input is absent. Two more names,
    ``EXTENSIONS_INTERNAL_TOKEN`` and ``BASE_URL``, have no guard at all and are
    never touched by the script. Every canonical name the script manages must
    clear blanks unconditionally so a mounted secret file is never shadowed.
    """

    ALL_BLANK_CLEARED_NAMES: tuple[str, ...] = (
        "EXTENSIONS__DATABASE__PASSWORD",
        "INVENTORY__DATABASE__PASSWORD",
        "TASKS__DATABASE__PASSWORD",
        "DATABASE__PASSWORD",
        "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN",
        "PMM__API_KEY",
        "PMM__ENDPOINT",
        "AUTH__PROVIDER__GRAFANA__ENDPOINT",
        "TASKS__NOMAD__ENDPOINT",
        "TASKS__NOMAD__API_KEY",
        "EXTENSIONS_INTERNAL_TOKEN",
        "BASE_URL",
    )

    @pytest.mark.parametrize("canonical_name", ALL_BLANK_CLEARED_NAMES)
    def test_a_blank_name_is_cleared_with_a_file_mounted(
        self, tmp_path: Path, canonical_name: str
    ):
        """Defer to the file even though no guard ever calls ``export_canonical``."""
        secrets_dir = write_secrets(tmp_path, **{canonical_name: "from-file"})

        environment = exported(
            source_helper(
                SECRET_KEY="k", SECRETS_DIR=secrets_dir, **{canonical_name: ""}
            )
        )

        assert canonical_name not in environment

    @pytest.mark.parametrize("canonical_name", ALL_BLANK_CLEARED_NAMES)
    def test_a_blank_name_is_cleared_with_no_file_mounted(self, canonical_name: str):
        """Fall through to the derived value or default, not an exported blank."""
        environment = exported(source_helper(SECRET_KEY="k", **{canonical_name: ""}))

        assert canonical_name not in environment

    @pytest.mark.parametrize("canonical_name", ALL_BLANK_CLEARED_NAMES)
    def test_an_explicit_value_is_untouched_by_the_clear(self, canonical_name: str):
        """Leave a genuinely non-empty inherited value exactly as it arrived."""
        environment = exported(
            source_helper(SECRET_KEY="k", **{canonical_name: "explicit-value"})
        )

        assert environment[canonical_name] == "explicit-value"

    def test_the_scripts_managed_name_list_matches_this_suites_own(self):
        """Keep the two enumerated lists in lockstep.

        Otherwise a name added to one and not the other goes untested or
        unmanaged without either side failing.
        """
        assert managed_canonical_names() == set(self.ALL_BLANK_CLEARED_NAMES) | {
            "CELERY__BEAT_DBURI"
        }

    @pytest.mark.usefixtures("embedded_profile_cwd")
    def test_a_mounted_password_resolves_even_with_its_guard_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert the file resolves though nothing sets ``EXTENSIONS_DB_PASSWORD``.

        Before the fix this blank had nothing to clear it: the password guard
        never fires without a raw input, so ``export_canonical`` never runs and
        the file stays shadowed.
        """
        secrets_dir = write_secrets(
            tmp_path, EXTENSIONS__DATABASE__PASSWORD="from-file"
        )
        environment = exported(
            source_helper(
                SECRET_KEY="k",
                SECRETS_DIR=secrets_dir,
                EXTENSIONS__DATABASE__PASSWORD="",
            )
        )
        apply_environment(monkeypatch, environment)

        assert (
            ExtensionsSettings(
                _secrets_dir=secrets_dir
            ).DATABASE.PASSWORD.get_secret_value()
            == "from-file"
        )

    @pytest.mark.usefixtures("embedded_profile_cwd")
    def test_a_mounted_internal_token_resolves_even_with_its_guard_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert the file resolves though nothing derives ``EXTENSIONS_INTERNAL_TOKEN``.

        Unlike the URL-typed names, an uncleared blank here would not crash:
        ``derive_internal_token`` treats an empty ``SecretStr`` the same as an
        unset one and silently overwrites it with a value derived from
        ``SECRET_KEY``, discarding the mounted token instead. A shell-level
        "absent from the environment" assertion can't tell that apart from
        this -- the derived fallback also leaves the name unexported.
        """
        secrets_dir = write_secrets(tmp_path, EXTENSIONS_INTERNAL_TOKEN="from-file")
        environment = exported(
            source_helper(
                SECRET_KEY="k", SECRETS_DIR=secrets_dir, EXTENSIONS_INTERNAL_TOKEN=""
            )
        )
        apply_environment(monkeypatch, environment)

        assert (
            Settings(
                _secrets_dir=secrets_dir
            ).EXTENSIONS_INTERNAL_TOKEN.get_secret_value()
            == "from-file"
        )

    @pytest.mark.usefixtures("embedded_profile_cwd")
    def test_mounted_grafana_credentials_resolve_even_with_their_guard_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Assert both files resolve though nothing sets ``EXTENSIONS_GRAFANA_TOKEN``.

        The token guard never fires without a raw input, so without the
        unconditional clear both files would stay shadowed the same way the
        password file did before the fix.
        """
        secrets_dir = write_secrets(
            tmp_path,
            AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN="from-file",
            PMM__API_KEY="from-file",
        )
        environment = exported(
            source_helper(
                SECRET_KEY="k",
                SECRETS_DIR=secrets_dir,
                AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN="",
                PMM__API_KEY="",
            )
        )
        apply_environment(monkeypatch, environment)

        assert (
            AuthSettings(_secrets_dir=secrets_dir)
            .PROVIDER["grafana"]
            .service_account_token.get_secret_value()
            == "from-file"
        )
        assert (
            Settings(_secrets_dir=secrets_dir).PMM.api_key.get_secret_value()
            == "from-file"
        )

    @pytest.mark.usefixtures("embedded_profile_cwd")
    @pytest.mark.parametrize(
        "mount",
        [{}, {"BASE_URL": "https://mounted:9443/extensions"}],
        ids=["falls-through-to-none", "resolves-through-the-file"],
    )
    def test_a_blank_base_url_resolves_through_the_file_or_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mount: dict[str, str]
    ):
        """Match a mounted file or the baked ``BASE_URL: null`` default, never an empty URL."""
        secrets_dir = write_secrets(tmp_path, **mount)
        environment = exported(
            source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir, BASE_URL="")
        )
        apply_environment(monkeypatch, environment)
        expected = mount.get("BASE_URL")

        base_url = Settings(_secrets_dir=secrets_dir).BASE_URL
        assert (str(base_url) if base_url is not None else base_url) == expected


class TestApplyEnvironmentIsolatesFromTheAmbientProcess:
    """Prove the pre-clear in ``apply_environment`` -- not just the subprocess run -- matters."""

    @pytest.mark.usefixtures("embedded_profile_cwd")
    def test_an_ambient_managed_name_does_not_survive_a_correctly_cleared_export(
        self, monkeypatch: pytest.MonkeyPatch, embedded_profile_data: dict
    ):
        """Assert a name the subprocess correctly left unexported outranks an ambient leak.

        The subprocess's own environment starts clean, so ``PMM__ENDPOINT``
        never reaches it here -- this sets it directly on the pytest process
        first, the way a developer's or CI job's shell might, to prove that
        without ``apply_environment``'s own clear, replaying the export alone
        would leave the leaked value in place.
        """
        monkeypatch.setenv("PMM__ENDPOINT", "https://leaked-from-the-pytest-process")
        environment = exported(source_helper(SECRET_KEY="k", PMM__ENDPOINT=""))
        apply_environment(monkeypatch, environment)

        assert (
            Settings().PMM.endpoint
            == embedded_profile_data["default"]["PMM"]["ENDPOINT"]
        )


class TestGuardInactiveEndpointsResolveWithoutCrashing:
    """Assert a blanked URL-typed name with its guard inactive never crashes.

    ``StrCredentialHttpUrl``/``CredentialHttpUrl`` reject an empty string but
    accept ``None``, so an uncleared blank fails settings validation outright
    -- taking every supervisord child down -- even though the baked profile
    carries a perfectly good default.
    """

    @pytest.mark.usefixtures("embedded_profile_cwd")
    @pytest.mark.parametrize(
        "mount",
        [{}, {"PMM__ENDPOINT": "https://mounted:9443"}],
        ids=["falls-through-to-the-baked-profile", "resolves-through-the-file"],
    )
    def test_pmm_endpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        embedded_profile_data: dict,
        mount: dict[str, str],
    ):
        """Assert a blank ``PMM__ENDPOINT`` never reaches ``PMMSettings`` empty."""
        secrets_dir = write_secrets(tmp_path, **mount)
        environment = exported(
            source_helper(SECRET_KEY="k", SECRETS_DIR=secrets_dir, PMM__ENDPOINT="")
        )
        apply_environment(monkeypatch, environment)
        expected = mount.get(
            "PMM__ENDPOINT", embedded_profile_data["default"]["PMM"]["ENDPOINT"]
        )

        assert Settings(_secrets_dir=secrets_dir).PMM.endpoint == expected

    @pytest.mark.usefixtures("embedded_profile_cwd")
    @pytest.mark.parametrize(
        "mount",
        [{}, {"TASKS__NOMAD__ENDPOINT": "https://mounted:9443/nomad"}],
        ids=["falls-through-to-the-baked-profile", "resolves-through-the-file"],
    )
    def test_nomad_endpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        embedded_profile_data: dict,
        mount: dict[str, str],
    ):
        """Assert a blank ``TASKS__NOMAD__ENDPOINT`` never crashes ``TasksSettings``."""
        secrets_dir = write_secrets(tmp_path, **mount)
        environment = exported(
            source_helper(
                SECRET_KEY="k",
                EXTENSIONS_DB_PASSWORD="pw",
                SECRETS_DIR=secrets_dir,
                TASKS__NOMAD__ENDPOINT="",
            )
        )
        apply_environment(monkeypatch, environment)
        expected = mount.get(
            "TASKS__NOMAD__ENDPOINT",
            embedded_profile_data["default"]["TASKS"]["NOMAD"]["ENDPOINT"],
        )

        assert str(TasksSettings(_secrets_dir=secrets_dir).NOMAD.endpoint) == expected

    @pytest.mark.usefixtures("embedded_profile_cwd")
    @pytest.mark.parametrize(
        "mount",
        [{}, {"AUTH__PROVIDER__GRAFANA__ENDPOINT": "https://mounted:9443/graph"}],
        ids=["falls-through-to-the-baked-profile", "resolves-through-the-file"],
    )
    def test_grafana_endpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        embedded_profile_data: dict,
        mount: dict[str, str],
    ):
        """Assert a blank Grafana endpoint never crashes ``AuthSettings`` either."""
        secrets_dir = write_secrets(tmp_path, **mount)
        environment = exported(
            source_helper(
                SECRET_KEY="k",
                SECRETS_DIR=secrets_dir,
                AUTH__PROVIDER__GRAFANA__ENDPOINT="",
            )
        )
        apply_environment(monkeypatch, environment)
        expected = mount.get(
            "AUTH__PROVIDER__GRAFANA__ENDPOINT",
            embedded_profile_data["default"]["AUTH"]["PROVIDER"]["grafana"]["endpoint"],
        )

        provider = AuthSettings(_secrets_dir=secrets_dir).PROVIDER["grafana"]
        assert str(provider.endpoint) == expected
