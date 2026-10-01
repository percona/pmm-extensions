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

"""Cover the MySQL entrypoint's clone3 gate and the cgroups layout it reads."""

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.sidecar.conftest import SIDECAR_DIR

ENTRYPOINT = SIDECAR_DIR / "pmm-fb" / "mysql-entrypoint.sh"

BASH = shutil.which("bash") or "/bin/bash"
"""Resolved here, because a run with a narrowed ``PATH`` cannot find the shell."""

CGROUP_ROOT_ASSIGNMENT = "CGROUP_ROOT=/sys/fs/cgroup"
DATADIR_ASSIGNMENT = "DATADIR=/var/lib/mysql"

REFUSED_NO_CLONE3 = 3
"""Exit status the entrypoint uses when clone3 is load-bearing and unimplemented."""

MISSING_COMMAND = 2
"""Exit status ``need_cmd`` uses when a declared precondition is absent."""

STOPPED_AT_MYSQLD = 1
"""Exit status of a run that cleared the gate.

The ``mysqld`` stub fails on purpose, so ``main`` ends at ``Could not initialise
the datadir`` having already invoked it. That is the earliest point observing
the entrypoint start the server, rather than only that the gate returned.
"""

ALL_CONTROLLERS = "cpuset cpu io memory pids"
"""A ``cgroup.controllers`` line offering everything Nomad's ``detect`` requires."""

DATADIR_FAILURE = "Could not initialise the datadir"

PASSWORDS = {
    "EXTENSIONS_MYSQL_ROOT_PASSWORD": "a",
    "EXTENSIONS_MYSQL_BACKUP_PASSWORD": "b",
    "EXTENSIONS_MYSQL_PMM_PASSWORD": "c",
}
"""The three secrets the entrypoint refuses to start without."""

REFUSAL_LINES = (
    "✗ clone3 is unimplemented here (ENOSYS): Nomad cannot launch a single task on this node",
    "✗ Unprivileged container? compose.yaml runs extensions-mysql privileged; a plain docker run needs --privileged or --security-opt seccomp=unconfined",
    '✗ Emulated amd64 on an arm64 engine? Set EXTENSIONS_MYSQL_PLATFORM=linux/arm64 in .env and re-run ./bootstrap.sh to build this node natively, or enable Rosetta (Docker Desktop → Settings → General → "Apple Virtualization framework" + "Use Rosetta for x86_64/amd64 emulation")',
    "✗ EXTENSIONS_FB_SKIP_CLONE3_CHECK=1 starts the node anyway (MySQL and inventory work; task execution will not)",
)
"""The headline and the three actionable causes the refusal prints, in order.

Restated here rather than read back out of the script: the refusal's wording is
what an operator acts on, so a reword has to fail a test rather than pass one.
"""

INERT_STUBS = ("mysql", "mysqladmin", "pmm-agent", "pmm-admin", "install")
"""Commands the entrypoint's preconditions demand but this gate never invokes."""

RunEntrypoint = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class Harness:
    """Carry the throwaway entrypoint copy and the callable that drives it.

    :param run: Invoke the copy against a chosen probe verdict and cgroup layout,
        optionally with one declared command missing from ``PATH``.
    :param cgroup_root: Where the copy's ``CGROUP_ROOT`` was retargeted.
    :param mysqld_marker: Written by the ``mysqld`` stub, so a test separates a
        run that reached the server from one the gate stopped.
    :param stat_marker: Carries the ``stat`` stub's arguments, so a test asserts
        both that the layout was never consulted and how it was asked for.
    :param probe_marker: Written by the ``python3`` stub, so a test asserts the
        clone3 probe never ran.
    """

    run: RunEntrypoint
    cgroup_root: Path
    mysqld_marker: Path
    stat_marker: Path
    probe_marker: Path


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Build a throwaway copy of the entrypoint and return the tool to drive it.

    Two of the script's own path constants are retargeted under ``tmp_path``:
    ``CGROUP_ROOT``, so a test chooses the layout the gate reads, and
    ``DATADIR``, so ``main`` takes its first-boot branch whether or not the
    machine running the suite has a real ``/var/lib/mysql``, whose other branch
    spends a two-minute retry against a two-minute suite timeout. Both
    substitution counts are asserted, so renaming either constant in the shipped
    script fails here instead of leaving the copy aimed at the host's own paths.

    :param tmp_path: The per-test temporary directory.
    :return: The script runner, the retargeted cgroup root, and the three stub
        markers.
    """
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    datadir = tmp_path / "datadir"
    datadir.mkdir()

    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert text.count(CGROUP_ROOT_ASSIGNMENT) == 1, (
        "the entrypoint no longer assigns CGROUP_ROOT the path the gate reads"
    )
    assert text.count(DATADIR_ASSIGNMENT) == 1, (
        "the entrypoint no longer assigns DATADIR its own constant"
    )
    text = text.replace(CGROUP_ROOT_ASSIGNMENT, f"CGROUP_ROOT={cgroup_root}")
    text = text.replace(DATADIR_ASSIGNMENT, f"DATADIR={datadir}")
    script = tmp_path / "mysql-entrypoint.sh"
    script.write_text(text, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mysqld_marker = tmp_path / "mysqld-ran"
    stat_marker = tmp_path / "stat-ran"
    probe_marker = tmp_path / "probe-ran"

    def write_stub(name: str, body: str) -> None:
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)

    for name in INERT_STUBS:
        write_stub(name, "exit 0")
    # Failing here is the stop: main's next two lines report it and exit, before
    # the backgrounded mysqld and the pmm-agent supervision loop that would hold
    # the captured pipes open for as long as the run lived
    write_stub("mysqld", f"touch {mysqld_marker}\nexit 1")

    def run(
        verdict: str = "CLONE3_ENOSYS",
        fs_type: str = "cgroup2fs",
        controllers: str | None = ALL_CONTROLLERS,
        *,
        skip_check: bool = False,
        omit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        write_stub("python3", f"touch {probe_marker}\necho {verdict}")
        reply = f"echo {fs_type}" if fs_type else "exit 1"
        write_stub("stat", f'printf %s "$*" > {stat_marker}\n{reply}')
        if omit is not None:
            (bin_dir / omit).unlink()
        controllers_file = cgroup_root / "cgroup.controllers"
        if controllers is None:
            controllers_file.unlink(missing_ok=True)
        else:
            controllers_file.write_text(f"{controllers}\n", encoding="utf-8")
        # A developer who exported these to drive their own bring-up would
        # otherwise decide the case under test. DEBUG is one of them: the
        # entrypoint documents it and turns on xtrace, which puts the tokens
        # these tests assert the absence of into stderr
        inherited = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("EXTENSIONS_MYSQL_", "EXTENSIONS_FB_"))
            and name != "DEBUG"
        }
        # Only the stubs once a command is omitted, so the host's own copy
        # cannot stand in for the one the precondition is meant to miss
        path = str(bin_dir) if omit else f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        environment = {**inherited, **PASSWORDS, "PATH": path}
        if skip_check:
            environment["EXTENSIONS_FB_SKIP_CLONE3_CHECK"] = "1"
        return subprocess.run(
            [BASH, str(script)],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=environment,
        )

    return Harness(
        run=run,
        cgroup_root=cgroup_root,
        mysqld_marker=mysqld_marker,
        stat_marker=stat_marker,
        probe_marker=probe_marker,
    )


def assert_proceeded(
    harness: Harness, result: subprocess.CompletedProcess[str]
) -> None:
    """Assert the run cleared the gate and went on to invoke ``mysqld``.

    :param harness: The harness whose ``mysqld`` marker records the invocation.
    :param result: The finished entrypoint run.
    """
    assert result.returncode == STOPPED_AT_MYSQLD, result.stderr
    assert harness.mysqld_marker.exists(), result.stderr
    assert DATADIR_FAILURE in result.stderr


def test_unified_hierarchy_still_refuses(harness: Harness) -> None:
    """Refuse a node whose cgroup2fs root offers every controller Nomad needs."""
    result = harness.run(verdict="CLONE3_ENOSYS", fs_type="cgroup2fs")

    assert result.returncode == REFUSED_NO_CLONE3, result.stderr
    assert not harness.mysqld_marker.exists()
    # -f is what asks about the mount rather than the directory entry, and
    # dropping it yields an empty type that reads as "not cgroup2fs" -- a silent
    # permit on the one layout where clone3 really is load-bearing
    assert harness.stat_marker.read_text(encoding="utf-8").split() == [
        "-fc",
        "%T",
        str(harness.cgroup_root),
    ]
    refusals = tuple(
        line for line in result.stderr.splitlines() if line.startswith("✗ ")
    )
    assert refusals == REFUSAL_LINES


@pytest.mark.parametrize(
    ("fs_type", "reported"),
    [("tmpfs", "tmpfs"), ("sysfs", "sysfs"), ("", "unreadable")],
)
def test_non_unified_hierarchy_proceeds(
    harness: Harness, fs_type: str, reported: str
) -> None:
    """Start the node when the cgroup root is anything but a cgroup2fs mount."""
    result = harness.run(verdict="CLONE3_ENOSYS", fs_type=fs_type)

    assert_proceeded(harness, result)
    assert f"is {reported}, not cgroup2fs" in result.stderr
    assert "✗ clone3" not in result.stderr


@pytest.mark.parametrize(
    "controllers",
    ["memory pids", None, "cpuset io memory pids"],
    ids=["partial", "absent", "no-cpu"],
)
def test_incomplete_controllers_proceed(
    harness: Harness, controllers: str | None
) -> None:
    """Start the node when a cgroup2fs root withholds a controller Nomad needs.

    The third case pins the matching itself: ``cpu`` must not be found inside
    ``cpuset``, which a substring match without the surrounding spaces would do,
    taking the refusing branch on a layout Nomad downgrades.
    """
    result = harness.run(verdict="CLONE3_ENOSYS", controllers=controllers)

    assert_proceeded(harness, result)
    assert f"does not offer all of {ALL_CONTROLLERS}" in result.stderr
    assert "✗ clone3" not in result.stderr


@pytest.mark.parametrize("fs_type", ["cgroup2fs", "tmpfs"])
def test_served_clone3_never_reads_the_layout(harness: Harness, fs_type: str) -> None:
    """Start the node without consulting cgroups once the probe reports success."""
    result = harness.run(verdict="CLONE3_OK", fs_type=fs_type)

    assert_proceeded(harness, result)
    assert not harness.stat_marker.exists()
    assert "clone3" not in result.stderr


@pytest.mark.parametrize("fs_type", ["cgroup2fs", "tmpfs"])
def test_unreadable_verdict_proceeds(harness: Harness, fs_type: str) -> None:
    """Start the node on any layout when the probe draws no conclusion.

    The layout is never consulted either: ``CLONE3_ERR`` is a log-and-continue
    branch this ticket leaves untouched, so an unconditional ``stat`` would be a
    regression that the exit status alone cannot see.
    """
    result = harness.run(verdict="CLONE3_ERR=13", fs_type=fs_type)

    assert_proceeded(harness, result)
    assert not harness.stat_marker.exists()
    assert "clone3 probe gave no verdict (CLONE3_ERR=13)" in result.stderr


def test_a_missing_stat_is_refused_at_the_precondition(harness: Harness) -> None:
    """Refuse before the gate runs when the command it reads the layout with is gone.

    ``stat`` is declared alongside the other preconditions for one reason: an
    absent one yields an empty filesystem type, which reads as "not cgroup2fs"
    and starts the node on the single layout where clone3 really is
    load-bearing. Nothing else in this module would fail if the declaration
    were dropped, so the permissive branch is what this pins against.
    """
    result = harness.run(verdict="CLONE3_ENOSYS", omit="stat")

    assert result.returncode == MISSING_COMMAND, result.stderr
    assert "✗ Missing required command: stat" in result.stderr
    assert not harness.probe_marker.exists()
    assert not harness.mysqld_marker.exists()


@pytest.mark.parametrize("fs_type", ["cgroup2fs", "tmpfs"])
def test_skip_flag_bypasses_both_reads(harness: Harness, fs_type: str) -> None:
    """Start the node on any layout without probing or reading cgroups."""
    result = harness.run(verdict="CLONE3_ENOSYS", fs_type=fs_type, skip_check=True)

    assert_proceeded(harness, result)
    assert not harness.probe_marker.exists()
    assert not harness.stat_marker.exists()
