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

"""Cover the feature-build bootstrap's executor-platform decision and its clone3 probe."""

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.sidecar.conftest import SIDECAR_DIR

BOOTSTRAP = SIDECAR_DIR / "pmm-fb" / "bootstrap.sh"
PROBE = SIDECAR_DIR / "clone3_probe.py"

CLIENT_ASSIGNMENT = re.compile(r"^ARM64_CLIENT_IMAGE=(\S+)$", re.MULTILINE)


def arm64_client() -> str:
    """Read the released client the script pins, rather than restating it here.

    A repin edits the script; a copy of the tag in this file would keep passing
    against the old one.

    :return: The image reference the arm64 path writes into the slot.
    """
    match = CLIENT_ASSIGNMENT.search(BOOTSTRAP.read_text(encoding="utf-8"))
    assert match is not None, "bootstrap.sh no longer assigns ARM64_CLIENT_IMAGE"
    return match.group(1)


ARM64_CLIENT = arm64_client()
OWNED_SLOTS = ("EXTENSIONS_MYSQL_PLATFORM", "EXTENSIONS_MYSQL_PMM_CLIENT_IMAGE")
PASSWORDS = (
    "EXTENSIONS_MYSQL_ROOT_PASSWORD=a\n"
    "EXTENSIONS_MYSQL_BACKUP_PASSWORD=b\n"
    "EXTENSIONS_MYSQL_PMM_PASSWORD=c\n"
)

SEEDED_ROOT_ONLY = "EXTENSIONS_MYSQL_ROOT_PASSWORD=a\n"
"""An environment file missing the two passwords the seeder has to append."""

REFUSED_NO_CLONE3 = 3
"""Exit status the script uses when the emulator cannot spawn a task."""
REFUSED_BAD_INPUT = 2
"""Exit status the script uses for a platform it has no client for."""

WriteEnv = Callable[..., None]
RunBootstrap = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Harness:
    """Carry the throwaway checkout and the two callables that drive it.

    :param env_file: The environment file the script writes beside itself.
    :param write_env: Seed that file with the passwords plus any executor slots.
    :param run: Invoke the script against a stubbed engine architecture.
    :param bin_dir: The stub directory that precedes the real one on ``PATH``.
    """

    env_file: Path
    write_env: WriteEnv
    run: RunBootstrap
    bin_dir: Path

    def slots(self) -> dict[str, str]:
        """Read back the two executor slots the script owns.

        :return: Slot name to value, for whichever of the two are present.
        """
        lines = self.env_file.read_text(encoding="utf-8").splitlines()
        pairs = (line.split("=", 1) for line in lines if "=" in line)
        return {name: value for name, value in pairs if name in OWNED_SLOTS}


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Build a throwaway copy of the harness and return the tools to drive it.

    The script resolves its own directory and writes beside itself, so it is
    copied into the same relative layout rather than run in place — a test must
    never rewrite a developer's real environment file. The container engine is a
    stub on ``PATH``: the decision under test reads the engine's architecture,
    so that is the only input worth faking.

    :return: The environment file, a writer for its slots, a script runner, and
        the stub directory a test can drop further executables into.
    """
    fb_dir = tmp_path / "pmm-fb"
    fb_dir.mkdir()
    (fb_dir / "bootstrap.sh").write_bytes(BOOTSTRAP.read_bytes())
    (tmp_path / "clone3_probe.py").write_bytes(PROBE.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env_file = fb_dir / ".env"

    def write_env(slots: str = "", extra: str = "") -> None:
        env_file.write_text(PASSWORDS + slots + extra, encoding="utf-8")
        env_file.chmod(0o600)

    def run(arch: str, verdict: str = "CLONE3_OK") -> subprocess.CompletedProcess[str]:
        stub = bin_dir / "docker"
        stub.write_text(
            f'#!/bin/sh\ncase "$1" in\n  version) echo {arch} ;;\n'
            f"  run) echo {verdict} ;;\n  *) exit 0 ;;\nesac\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        # A developer who exported these to drive their own bring-up would
        # otherwise decide the case under test instead of the seeded file
        inherited = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("EXTENSIONS_MYSQL_", "EXTENSIONS_FB_"))
        }
        return subprocess.run(
            ["bash", str(fb_dir / "bootstrap.sh")],
            capture_output=True,
            text=True,
            check=False,
            cwd=fb_dir,
            env={**inherited, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        )

    return Harness(env_file=env_file, write_env=write_env, run=run, bin_dir=bin_dir)


def test_arm64_engine_selects_the_native_pair(harness: Harness) -> None:
    """Write the native platform and the released multi-arch client on arm64."""
    harness.write_env()

    assert harness.run("aarch64").returncode == 0
    assert harness.slots() == {
        "EXTENSIONS_MYSQL_PLATFORM": "linux/arm64",
        "EXTENSIONS_MYSQL_PMM_CLIENT_IMAGE": ARM64_CLIENT,
    }


def test_arm64_rerun_rewrites_rather_than_appends(harness: Harness) -> None:
    """Keep one line per executor slot however many times the script runs."""
    harness.write_env()

    harness.run("aarch64")
    harness.run("aarch64")

    body = harness.env_file.read_text(encoding="utf-8")
    assert [body.count(f"{slot}=") for slot in OWNED_SLOTS] == [1, 1]


def test_amd64_engine_reclaims_slots_an_arm64_engine_wrote(harness: Harness) -> None:
    """Reset the executor slots when the same checkout moves to an amd64 engine.

    Without this the executor is built ``linux/arm64`` on an amd64 engine, under
    the reverse emulation the clone3 guard exists to keep it out of — and the
    build succeeds rather than failing, because the released client publishes an
    arm64 variant.
    """
    harness.write_env(
        f"EXTENSIONS_MYSQL_PLATFORM=linux/arm64\nEXTENSIONS_MYSQL_PMM_CLIENT_IMAGE={ARM64_CLIENT}\n"
    )

    assert harness.run("amd64").returncode == 0
    assert harness.slots() == {
        "EXTENSIONS_MYSQL_PLATFORM": "linux/amd64",
        "EXTENSIONS_MYSQL_PMM_CLIENT_IMAGE": "",
    }


def test_amd64_engine_leaves_a_file_without_executor_slots_alone(
    harness: Harness,
) -> None:
    """Add no executor slot to an environment file that never carried one."""
    harness.write_env()
    before = harness.env_file.read_text(encoding="utf-8")

    assert harness.run("amd64").returncode == 0
    assert harness.env_file.read_text(encoding="utf-8") == before


def test_forced_amd64_on_arm64_blanks_the_client_slot(harness: Harness) -> None:
    """Select the feature-build client, not a released amd64 one, when forced."""
    harness.write_env("EXTENSIONS_MYSQL_PLATFORM=linux/amd64\n")

    assert harness.run("aarch64").returncode == 0
    assert harness.slots()["EXTENSIONS_MYSQL_PMM_CLIENT_IMAGE"] == ""


def test_forced_amd64_refuses_an_emulator_without_clone3(harness: Harness) -> None:
    """Refuse rather than build a node that registers healthy and runs nothing."""
    harness.write_env("EXTENSIONS_MYSQL_PLATFORM=linux/amd64\n")

    result = harness.run("aarch64", verdict="CLONE3_ENOSYS")

    assert result.returncode == REFUSED_NO_CLONE3
    assert "no clone3" in result.stderr


def test_the_probe_is_skippable_from_the_environment_file(harness: Harness) -> None:
    """Honour the skip override where an operator sets it, not only in the shell."""
    harness.write_env(
        "EXTENSIONS_MYSQL_PLATFORM=linux/amd64\n",
        extra="EXTENSIONS_FB_SKIP_CLONE3_CHECK=1\n",
    )

    assert harness.run("aarch64", verdict="CLONE3_ENOSYS").returncode == 0


def test_an_unrecognised_platform_is_rejected(harness: Harness) -> None:
    """Reject a platform the build has no client for instead of guessing one."""
    harness.write_env("EXTENSIONS_MYSQL_PLATFORM=linux/riscv64\n")

    result = harness.run("aarch64")

    assert result.returncode == REFUSED_BAD_INPUT
    assert "linux/riscv64" in result.stderr


def test_the_probe_prints_exactly_one_known_token() -> None:
    """Answer with one of the three tokens both guards branch on, and nothing else.

    The verdict itself is environment-dependent — a kernel serving the syscall
    answers ``CLONE3_OK`` and a default seccomp profile answers ``CLONE3_ENOSYS``
    — so the contract worth pinning here is the shape both callers ``case`` on.
    """
    result = subprocess.run(
        ["python3", str(PROBE)], capture_output=True, text=True, check=True
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0] in {"CLONE3_OK", "CLONE3_ENOSYS"} or lines[0].startswith(
        "CLONE3_ERR="
    )


def test_the_probe_forks_nothing() -> None:
    """Ask the syscall without spawning a child, so an entrypoint can run it first.

    The undersized argument is rejected before the kernel reads it, which is what
    makes the probe safe to run from a process that has started nothing yet. Run
    in-process so a fork would show up as a changed pid or a reapable child.
    """
    script = (
        "import os, runpy, sys\n"
        "pid = os.getpid()\n"
        f"runpy.run_path({str(PROBE)!r}, run_name='__main__')\n"
        "sys.stderr.write('same-pid\\n' if os.getpid() == pid else 'forked\\n')\n"
        "try:\n"
        "    os.wait()\n"
        "    sys.stderr.write('child\\n')\n"
        "except ChildProcessError:\n"
        "    sys.stderr.write('no-child\\n')\n"
    )

    result = subprocess.run(
        ["python3", "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stderr.split() == ["same-pid", "no-child"]


def test_a_failed_password_append_stops_the_bootstrap(harness: Harness) -> None:
    """Exit rather than announce a generated password the append never stored.

    The datadir keeps whatever password MySQL first booted with, so a seeded
    slot that never reached the file leaves a deployment no one holds the
    credentials for. ``chmod`` is stubbed because the script restores write
    permission itself before seeding, which is the whole reason the file mode
    alone cannot reproduce a read-only mount.
    """
    harness.env_file.write_text(SEEDED_ROOT_ONLY, encoding="utf-8")
    harness.env_file.chmod(0o400)
    stub = harness.bin_dir / "chmod"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)

    result = harness.run("x86_64")

    assert result.returncode == REFUSED_BAD_INPUT
    assert "Could not write EXTENSIONS_MYSQL_BACKUP_PASSWORD" in result.stderr
    assert "Added the EXTENSIONS_MYSQL_BACKUP_PASSWORD" not in result.stderr
    assert harness.env_file.read_text(encoding="utf-8") == SEEDED_ROOT_ONLY
