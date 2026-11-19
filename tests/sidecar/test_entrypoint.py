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
"""Cover the side-car PID 1's Grafana mint step and its stale-sentinel clearing."""

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from tests.sidecar.conftest import (
    ENTRYPOINT,
    schema_steps,
    SENTINEL_PREFIX,
    SIDECAR_DIR,
    TOKEN_SENTINEL,
)

SETTINGS_ENV_HELPER = SIDECAR_DIR / "settings-env.sh"
MINT_HELPER_NAME = "grafana_service_account.py"
ENCRYPTION_HELPER_NAME = "encryption_key.py"

SENTINEL_PATH = re.compile(r"/tmp/migrate-([a-z]+)\.ok")

CANONICAL_NAMES = (
    "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN",
    "PMM__API_KEY",
    "TASKS__NOMAD__API_KEY",
)

MINTED_TOKEN = "glsa_minted_at_container_start"

SUPPLIED_ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")
"""A key the deployment supplies, which must send the entrypoint past its helper."""

MINTED_ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")
"""A key the stub helper answers with, distinct so the two are never confused."""

FAKE_SUPERVISORD = r"""#!/usr/bin/env bash
env -0 > "$FAKE_SUPERVISORD_ENV"
ls -1 /tmp/migrate-*.ok > "$FAKE_SUPERVISORD_SENTINELS" 2> /dev/null || true
"""
"""A stand-in for supervisord recording what its children would inherit.

The sentinel listing is taken here rather than after the run because what the
clearing must guarantee is that no stale marker survives *into* the spawn — an
assertion made once the entrypoint has exited could not tell the two apart.
"""

STUB_MINT_HELPER = """import os
import sys

with open(os.environ["FAKE_HELPER_ARGV"], "w", encoding="utf-8") as handle:
    handle.write("\\n".join(sys.argv))
sys.stdout.write(os.environ["FAKE_HELPER_STDOUT"])
sys.exit(int(os.environ["FAKE_HELPER_EXIT"]))
"""
"""A stand-in for the mint helper, answering whatever the case calls for."""

STUB_ENCRYPTION_HELPER = """import os
import sys

with open(os.environ["FAKE_ENCRYPTION_ARGV"], "w", encoding="utf-8") as handle:
    handle.write("\\n".join(sys.argv))
sys.stdout.write(os.environ["FAKE_ENCRYPTION_STDOUT"])
sys.exit(int(os.environ["FAKE_ENCRYPTION_EXIT"]))
"""
"""A stand-in for the encryption-key helper.

Written to its own argv file rather than sharing the mint helper's, because the
cases below turn on *whether* each helper ran at all.
"""


class FakeContainer:
    """Run the real entrypoint outside an image, with its two exec points stubbed.

    The entrypoint resolves its siblings relative to itself, so a copy of the
    tree is enough to exercise it; ``supervisord`` is shadowed on ``PATH`` and
    the mint helper is replaced in the copied tree.
    """

    def __init__(self, root: Path):
        """Lay the copied application root and the fake runtime out under ``root``.

        The copied entrypoint's sentinel paths are rewritten onto a prefix unique
        to this container, so the two cases that exercise the clearing cannot
        invalidate each other's precondition under the suite's parallel runner.
        The token sentinel moves under ``root`` for the same reason.
        The substitution count is asserted, so renaming or dropping a sentinel in
        the shipped entrypoint fails the test rather than silently leaving the
        copy pointed at the real ``/tmp`` paths.

        :param root: The per-test temporary directory to build in.
        """
        self.app_dir = root / "app"
        self.app_dir.mkdir()
        self.sentinel_prefix = f"{SENTINEL_PREFIX}{uuid4().hex}-"
        self.token_sentinel = root / "grafana-token.ok"
        for source in (ENTRYPOINT, SETTINGS_ENV_HELPER):
            target = self.app_dir / source.name
            text = source.read_text(encoding="utf-8")
            if source is ENTRYPOINT:
                assert text.count(SENTINEL_PREFIX) == len(schema_steps()), (
                    "the entrypoint no longer clears one sentinel per schema step"
                )
                text = text.replace(SENTINEL_PREFIX, self.sentinel_prefix)
                assert text.count(TOKEN_SENTINEL) == 1, (
                    "the entrypoint no longer names the token sentinel exactly once"
                )
                text = text.replace(TOKEN_SENTINEL, str(self.token_sentinel))
            target.write_text(text, encoding="utf-8")
            target.chmod(0o755)
        (self.app_dir / MINT_HELPER_NAME).write_text(STUB_MINT_HELPER, encoding="utf-8")
        (self.app_dir / ENCRYPTION_HELPER_NAME).write_text(
            STUB_ENCRYPTION_HELPER, encoding="utf-8"
        )

        self._bin = root / "bin"
        self._bin.mkdir()
        supervisord = self._bin / "supervisord"
        supervisord.write_text(FAKE_SUPERVISORD, encoding="utf-8")
        supervisord.chmod(0o755)

        self._environment_file = root / "supervisord-env"
        self._argv_file = root / "helper-argv"
        self._encryption_argv_file = root / "encryption-helper-argv"
        self._sentinel_file = root / "supervisord-sentinels"

    def start(
        self,
        *,
        token: str = MINTED_TOKEN,
        exit_code: int = 0,
        encryption_key: str | None = SUPPLIED_ENCRYPTION_KEY,
        minted_key: str = MINTED_ENCRYPTION_KEY,
        minted_key_exit_code: int = 0,
        **inputs: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run the entrypoint with both stub helpers answering as configured.

        :param token: What the stub mint helper prints for the entrypoint to capture.
        :param exit_code: What the stub mint helper exits with.
        :param encryption_key: The key the deployment supplies, or ``None`` to
            supply none — which is what sends the entrypoint to its helper.
        :param minted_key: What the stub encryption helper prints.
        :param minted_key_exit_code: What the stub encryption helper exits with.
        :param inputs: Deployment inputs to place in the environment.
        :return: The completed entrypoint run.
        """
        environment = {
            "PATH": f"{self._bin}{os.pathsep}{os.environ['PATH']}",
            "SECRET_KEY": "k",
            "FAKE_SUPERVISORD_ENV": str(self._environment_file),
            "FAKE_SUPERVISORD_SENTINELS": str(self._sentinel_file),
            "FAKE_HELPER_ARGV": str(self._argv_file),
            "FAKE_HELPER_STDOUT": token,
            "FAKE_HELPER_EXIT": str(exit_code),
            "FAKE_ENCRYPTION_ARGV": str(self._encryption_argv_file),
            "FAKE_ENCRYPTION_STDOUT": minted_key,
            "FAKE_ENCRYPTION_EXIT": str(minted_key_exit_code),
            **({} if encryption_key is None else {"ENCRYPTION_KEY": encryption_key}),
            **inputs,
        }
        return subprocess.run(
            [str(self.app_dir / ENTRYPOINT.name)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def supervised_environment(self) -> dict[str, str]:
        """Return the environment supervisord was handed.

        :return: The exported variables.
        """
        recorded = self._environment_file.read_text(encoding="utf-8")
        return dict(
            entry.split("=", 1) for entry in recorded.split("\0") if "=" in entry
        )

    @property
    def sentinels_at_spawn(self) -> list[str]:
        """Return the schema sentinels present when supervisord was reached.

        :return: One path per surviving sentinel, empty when the clear worked.
        """
        return self._sentinel_file.read_text(encoding="utf-8").split()

    @property
    def helper_argv(self) -> list[str]:
        """Return the argv the entrypoint invoked the mint helper with.

        :return: One entry per argument, in order.
        """
        return self._argv_file.read_text(encoding="utf-8").splitlines()

    @property
    def encryption_helper_argv(self) -> list[str]:
        """Return the argv the entrypoint invoked the encryption helper with.

        :return: One entry per argument, empty when the helper never ran.
        """
        if not self._encryption_argv_file.exists():
            return []
        return self._encryption_argv_file.read_text(encoding="utf-8").splitlines()

    @property
    def reached_supervisord(self) -> bool:
        """Return whether the entrypoint got as far as spawning supervisord.

        :return: Whether the recording stub ever ran.
        """
        return self._environment_file.exists()

    @property
    def owned_sentinels(self) -> set[str]:
        """Return the sentinel paths this container's entrypoint copy clears.

        :return: One absolute path per schema step, under this container's prefix.
        """
        return {f"{self.sentinel_prefix}{step}.ok" for step in schema_steps()}


@pytest.fixture
def container(tmp_path: Path) -> FakeContainer:
    """Return a fake container ready to run the entrypoint.

    :param tmp_path: The per-test temporary directory.
    :return: The fake container.
    """
    return FakeContainer(tmp_path)


def test_a_minted_token_reaches_every_canonical_name(container: FakeContainer):
    """Hand the minted token to every supervised program, through one function."""
    result = container.start()

    assert result.returncode == 0, result.stderr
    for name in CANONICAL_NAMES:
        assert container.supervised_environment[name] == MINTED_TOKEN


def test_a_helper_resolving_nothing_leaves_the_names_alone(container: FakeContainer):
    """Leave the baked empty token standing when the pre-flight resolves nothing."""
    result = container.start(token="")

    assert result.returncode == 0, result.stderr
    for name in CANONICAL_NAMES:
        assert name not in container.supervised_environment


def test_a_failing_helper_does_not_take_the_container_down(container: FakeContainer):
    """Degrade auth exactly as it degrades today rather than killing PID 1.

    The entrypoint runs under ``errexit``, so without the failure being absorbed
    the helper's non-zero exit would end PID 1 before supervisord ever starts.
    """
    result = container.start(token="", exit_code=1)

    assert result.returncode == 0, result.stderr
    assert container.supervised_environment
    for name in CANONICAL_NAMES:
        assert name not in container.supervised_environment


def test_a_resolved_token_publishes_the_health_sentinel(container: FakeContainer):
    """Mark the start as holding its token, which the health check requires."""
    container.start()

    assert container.token_sentinel.exists()


def test_a_start_with_nothing_to_resolve_publishes_the_health_sentinel(
    container: FakeContainer,
):
    """Stay healthy where no token is owed, such as a provider other than Grafana."""
    container.start(token="")

    assert container.token_sentinel.exists()


def test_a_failed_mint_leaves_the_health_sentinel_unpublished(
    container: FakeContainer,
):
    """Keep a container that started without its token from reporting healthy.

    The sentinel of the previous run is in place first: ``/tmp`` survives a
    container restart, so a marker merely left unwritten would still be there.
    """
    container.token_sentinel.touch()

    container.start(token="", exit_code=1)

    assert container.reached_supervisord
    assert not container.token_sentinel.exists()


def test_the_health_sentinel_is_cleared_before_the_key_gate(container: FakeContainer):
    """Drop the previous run's marker before the first step that can wait.

    The key gate is that step. A marker cleared only at the mint would leave a
    restarted container, waiting there for PMM, reporting on the token its
    previous run held.
    """
    container.token_sentinel.touch()

    result = container.start(SECRET_KEY="")

    assert result.returncode != 0
    assert not container.reached_supervisord
    assert not container.token_sentinel.exists()


def test_an_explicit_token_still_outranks_the_mint(container: FakeContainer):
    """Keep the operator's own value, which the pre-flight resolves beneath."""
    container.start(EXTENSIONS_GRAFANA_TOKEN="glsa_explicit")

    for name in CANONICAL_NAMES:
        assert container.supervised_environment[name] == "glsa_explicit"


@pytest.mark.parametrize(
    "supplied",
    ["AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN", "PMM__API_KEY"],
)
def test_a_supplied_mint_gate_token_still_fills_nomad(
    container: FakeContainer, supplied: str
):
    """Export a helper-resolved mint-gate token to Nomad without displacing it.

    The stub prints what ``grafana_service_account.py`` prints when either
    mint-gate name already resolves: the entrypoint must still call
    ``export_grafana_token``, or ``TASKS__NOMAD__API_KEY`` stays unset.
    """
    token = "glsa_supplied_by_the_operator"
    result = container.start(token=token, **{supplied: token})

    assert result.returncode == 0, result.stderr
    supervised = container.supervised_environment
    assert supervised[supplied] == token
    assert supervised["TASKS__NOMAD__API_KEY"] == token


@pytest.mark.parametrize(
    "mounted",
    ["AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN", "PMM__API_KEY"],
)
def test_a_mounted_mint_gate_token_still_fills_nomad(
    container: FakeContainer, tmp_path: Path, mounted: str
):
    """Export a SECRETS_DIR-mounted mint-gate value to every unset sibling, leaving the file alone."""
    token = "glsa_mounted"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / mounted).write_text(f"{token}\n", encoding="utf-8")
    sibling = (
        "PMM__API_KEY"
        if mounted == "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN"
        else "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN"
    )

    result = container.start(token=token, SECRETS_DIR=str(secrets_dir))

    assert result.returncode == 0, result.stderr
    supervised = container.supervised_environment
    assert mounted not in supervised
    assert supervised[sibling] == token
    assert supervised["TASKS__NOMAD__API_KEY"] == token


def test_differing_mint_gate_names_fill_nomad_from_the_helper_value(
    container: FakeContainer,
):
    """Keep an explicit PMM key and still fill Nomad from the helper's SAT."""
    result = container.start(
        token="glsa_service_account",
        AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN="glsa_service_account",
        PMM__API_KEY="glsa_pmm_api_key",
    )

    assert result.returncode == 0, result.stderr
    supervised = container.supervised_environment
    assert (
        supervised["AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN"]
        == "glsa_service_account"
    )
    assert supervised["PMM__API_KEY"] == "glsa_pmm_api_key"
    assert supervised["TASKS__NOMAD__API_KEY"] == "glsa_service_account"


def test_the_token_never_enters_the_helpers_argv(container: FakeContainer):
    """Keep the credential out of ``/proc``, which every process in the namespace reads."""
    container.start()

    assert container.helper_argv == [str(container.app_dir / MINT_HELPER_NAME)]


def test_the_grafana_admin_credential_stops_at_the_mint(container: FakeContainer):
    """Keep the admin pair out of every supervised program's environment.

    It is more privileged than the token it mints, and only the mint step reads
    it, so it must not outlive that step.
    """
    container.start(GF_SECURITY_ADMIN_USER="root", GF_SECURITY_ADMIN_PASSWORD="s3cret")

    supervised = container.supervised_environment
    assert "GF_SECURITY_ADMIN_USER" not in supervised
    assert "GF_SECURITY_ADMIN_PASSWORD" not in supervised


def test_an_explicit_encryption_key_skips_the_helper(container: FakeContainer):
    """Leave channel 1 alone, which the helper sits three ranks below."""
    result = container.start()

    assert result.returncode == 0, result.stderr
    assert container.encryption_helper_argv == []
    assert container.supervised_environment["ENCRYPTION_KEY"] == SUPPLIED_ENCRYPTION_KEY


def test_a_mounted_encryption_key_is_neither_minted_over_nor_exported(
    container: FakeContainer, tmp_path: Path
):
    """Leave a mounted key to its file, which is the regression the shell cannot see.

    ``settings-env.sh`` alone would pass this: it never exports the name either
    way. What only the entrypoint can get wrong is running the helper anyway and
    exporting its answer, which converts the file channel from "each process
    reads the file" into "one value in every process's environment".
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "ENCRYPTION_KEY").write_text(
        SUPPLIED_ENCRYPTION_KEY, encoding="utf-8"
    )

    result = container.start(encryption_key=None, SECRETS_DIR=str(secrets_dir))

    assert result.returncode == 0, result.stderr
    assert container.encryption_helper_argv == []
    assert "ENCRYPTION_KEY" not in container.supervised_environment


def test_a_minted_encryption_key_reaches_every_supervised_program(
    container: FakeContainer,
):
    """Hand what the helper resolved to every program, as the mint step does."""
    result = container.start(encryption_key=None)

    assert result.returncode == 0, result.stderr
    assert container.supervised_environment["ENCRYPTION_KEY"] == MINTED_ENCRYPTION_KEY


def test_a_failing_encryption_helper_takes_the_container_down(
    container: FakeContainer,
):
    """Let the helper's refusal end PID 1, unlike the Grafana mint beside it.

    The helper exits non-zero only where it could not prove the deployment has
    no ciphertext. Absorbing that would start the container under a key it
    already refused to mint, or under none at all — the silently-reverted
    overrides this guard exists to prevent.
    """
    result = container.start(encryption_key=None, minted_key_exit_code=1)

    assert result.returncode != 0
    assert not container.reached_supervisord


def test_the_encryption_key_never_enters_the_helpers_argv(container: FakeContainer):
    """Keep the key out of ``/proc``, which every process in the namespace reads."""
    container.start(encryption_key=None)

    assert container.encryption_helper_argv == [
        str(container.app_dir / ENCRYPTION_HELPER_NAME)
    ]


def cleared_sentinels() -> set[str]:
    """Return the schema steps whose sentinel the entrypoint clears.

    :return: One bare step name per cleared sentinel path.
    """
    return set(SENTINEL_PATH.findall(ENTRYPOINT.read_text(encoding="utf-8")))


@pytest.fixture
def owned_sentinels(container: FakeContainer) -> Iterator[set[str]]:
    """Return the sentinel paths this container owns, removing them afterwards.

    Each container rewrites its entrypoint copy onto its own prefix, so the two
    cases below never touch the same paths and neither can leave the other
    asserting against markers a sibling removed. Cleaning up keeps a failed run
    from leaving markers behind.

    :param container: The fake container whose entrypoint copy names them.
    :return: One absolute sentinel path per schema step.
    """
    paths = container.owned_sentinels
    yield paths
    for path in paths:
        Path(path).unlink(missing_ok=True)


def test_stale_sentinels_are_cleared_before_supervisord(
    container: FakeContainer, owned_sentinels: set[str]
):
    """Clear a previous run's sentinels from PID 1, before any program is spawned.

    ``/tmp`` survives a container restart and each one-shot clears its own
    sentinel only *after* being spawned — concurrently with the apps now gated on
    it — so a gate could otherwise read a previous run's marker and release an app
    against a schema this run has not re-applied.

    Only this suite's own sentinels are asserted on. The recording stub globs all
    of ``/tmp``, and a sibling test holding an unrelated ``migrate-*.ok`` would
    otherwise fail this one under the suite's parallel runner.
    """
    for path in owned_sentinels:
        Path(path).touch()

    result = container.start()

    assert result.returncode == 0, result.stderr
    assert set(container.sentinels_at_spawn) & owned_sentinels == set()


def test_clearing_is_not_fatal_when_no_sentinel_exists(
    container: FakeContainer, owned_sentinels: set[str]
):
    """Start supervisord on a first boot, where there is nothing to clear.

    The entrypoint runs under ``errexit``, so a clear that failed on an unmatched
    name would kill PID 1 instead of starting the container.
    """
    assert not any(Path(path).exists() for path in owned_sentinels)

    result = container.start()

    assert result.returncode == 0, result.stderr
    assert container.supervised_environment


def test_every_one_shot_sentinel_is_cleared():
    """Assert the cleared names are exactly the one-shots supervisord runs.

    The entrypoint names them rather than globbing, so this is what keeps the two
    files from drifting: a schema step added to the program table without being
    cleared here would survive a restart and release the gate on a stale marker.
    """
    assert cleared_sentinels() == set(schema_steps())
