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

"""Cover the side-car's schema gate and the program table it is wired into."""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests.sidecar.conftest import (
    BUDGET_ASSIGNMENT,
    CONTAINERFILE,
    GATE,
    HEALTHCHECK,
    SCHEMA_STEPS,
)

GATE_INVOCATION = "./wait_for_schema.sh"

GATED_PROGRAMS = {
    "extensions": "python -m app.extensions.main",
    "inventory": "python -m app.inventory.main",
    "tasks": "python -m app.tasks.main",
}
"""Each gated API program and the command the gate must precede."""

ALEMBIC_ONE_SHOTS = ("migrate-extensions", "migrate-inventory", "migrate-tasks")

RunGate = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def sentinel_names() -> Iterator[list[str]]:
    """Return one sentinel name per schema step, cleaning up what it wrote.

    The gate derives ``/tmp/migrate-<name>.ok`` from each name it is given, and
    that path is hardcoded across the entrypoint, the program table, the
    healthcheck and ``clear_sentinels.sh`` alike. Unique names keep a run off a
    developer's real sentinels and off a sibling xdist worker's, without
    introducing a directory knob that exists only for the tests.

    :return: One name per schema step, positionally aligned with them.
    """
    names = [f"wait-for-schema-{step}-{uuid4().hex}" for step in SCHEMA_STEPS]
    yield names
    for name in names:
        Path(f"/tmp/migrate-{name}.ok").unlink(missing_ok=True)


def publish(name: str) -> None:
    """Write the sentinel a completed schema step would publish.

    :param name: The schema step's name.
    """
    Path(f"/tmp/migrate-{name}.ok").touch()


@pytest.fixture
def run_gate(tmp_path: Path) -> RunGate:
    """Return a runner for the gate, optionally over a shortened budget.

    The budget is a constant rather than a deployment input, so a case that must
    outlive it runs a copy with the assignment rewritten; the substitution is
    asserted, so renaming the constant fails the test rather than silently
    running the shipped 300 seconds.

    :param tmp_path: The per-test temporary directory.
    :return: A callable taking the sentinel names and an optional budget.
    """

    def run(
        names: list[str], budget: int | None = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        script = GATE
        if budget is not None:
            source = GATE.read_text(encoding="utf-8")
            rewritten, count = BUDGET_ASSIGNMENT.subn(
                f"readonly WAIT_BUDGET_SECONDS={budget}", source
            )
            assert count == 1, "the gate no longer declares WAIT_BUDGET_SECONDS"
            script = tmp_path / GATE.name
            script.write_text(rewritten, encoding="utf-8")
            script.chmod(0o755)
        return subprocess.run(
            [str(script), *names],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    return run


def test_the_program_table_declares_schema_steps():
    """Pin the derived step list, so a parse that yields nothing fails here.

    ``SCHEMA_STEPS`` is derived at import. An empty tuple would leave the gate
    assertion below matching ``./wait_for_schema.sh`` with no arguments, which
    the real command contains — so it would pass while asserting nothing.
    """
    assert SCHEMA_STEPS


def test_the_gate_passes_once_every_sentinel_is_present(
    sentinel_names: list[str], run_gate: RunGate
):
    """Release the app immediately when every schema step has already finished."""
    for name in sentinel_names:
        publish(name)

    result = run_gate(sentinel_names)

    assert result.returncode == 0, result.stderr


def test_the_gate_waits_for_a_sentinel_that_arrives_late(
    sentinel_names: list[str], run_gate: RunGate, tmp_path: Path
):
    """Hold the app while one step is still running, then release it.

    Without the wait the API program is spawned beside the schema steps and reads
    tables that do not exist yet.
    """
    late, *ready = sentinel_names
    for name in ready:
        publish(name)
    publisher = subprocess.Popen(
        ["sh", "-c", f'sleep 2; touch "/tmp/migrate-{late}.ok"'],
    )

    try:
        result = run_gate(sentinel_names)
    finally:
        publisher.wait(timeout=30)

    assert result.returncode == 0, result.stderr
    # Exit 0 alone would also pass for a gate that never waited at all, so the
    # log is what distinguishes holding from waving the app through.
    assert late in result.stdout
    assert not any(name in result.stdout for name in ready)


def test_the_gate_gives_up_when_the_budget_is_spent(
    sentinel_names: list[str], run_gate: RunGate
):
    """Fail rather than wait forever on a schema step that never completes.

    The budget is two seconds against a one-second poll, so the loop re-polls
    before expiring — a zero budget would expire on the first iteration and never
    exercise the waiting path at all.
    """
    result = run_gate(sentinel_names, budget=2)

    assert result.returncode != 0
    assert sentinel_names[0] in result.stderr


def test_the_gate_names_only_the_sentinels_it_is_still_missing(
    sentinel_names: list[str], run_gate: RunGate
):
    """Name every outstanding step, so nothing waits without an explanation."""
    absent = [sentinel_names[0], sentinel_names[-1]]
    present = sentinel_names[1:-1]
    for name in present:
        publish(name)

    result = run_gate(sentinel_names, budget=0)
    output = result.stdout + result.stderr

    assert all(name in output for name in absent)
    assert not any(name in output for name in present)


def test_the_gate_rejects_an_empty_step_list(run_gate: RunGate):
    """Refuse a gate with nothing to wait for rather than passing vacuously."""
    result = run_gate([])

    assert result.returncode != 0


@pytest.mark.parametrize(("program", "invocation"), sorted(GATED_PROGRAMS.items()))
def test_each_api_runs_the_gate_before_its_app(
    program_settings: dict[str, dict[str, str]], program: str, invocation: str
):
    """Assert the gate precedes the app it guards, in the same program."""
    command = program_settings[program]["command"]

    assert GATE_INVOCATION in command
    assert invocation in command
    assert command.index(GATE_INVOCATION) < command.index(invocation)


@pytest.mark.parametrize(("program", "invocation"), sorted(GATED_PROGRAMS.items()))
def test_an_exhausted_gate_never_starts_its_api(
    program_settings: dict[str, dict[str, str]], program: str, invocation: str
):
    """Require an ``&&`` between the two — the inverse of celery-beat's ``;``.

    Beat's gate exits 0 by design and beat starts whatever it reports. This gate
    is the opposite: a schema step that never completed means the app would fail
    on the tables it is waiting for, so a spent budget must not release it.
    """
    command = program_settings[program]["command"]
    between = command[
        command.index(GATE_INVOCATION) + len(GATE_INVOCATION) : command.index(
            invocation
        )
    ]

    assert "&&" in between
    assert ";" not in between


@pytest.mark.parametrize(("program", "invocation"), sorted(GATED_PROGRAMS.items()))
def test_each_gated_api_execs_and_stops_as_a_group(
    program_settings: dict[str, dict[str, str]], program: str, invocation: str
):
    """Assert the stop path reaches the wait rather than orphaning it."""
    settings = program_settings[program]

    assert f"exec {invocation}" in settings["command"]
    assert settings["stopasgroup"] == "true"
    assert settings["killasgroup"] == "true"


@pytest.mark.parametrize("program", sorted(GATED_PROGRAMS))
def test_each_gated_api_waits_for_every_schema_step(
    program_settings: dict[str, dict[str, str]], program: str
):
    """Assert the uniform gate: all three APIs wait on all four sentinels.

    ``inventory`` never reads the beat tables, but container health already
    requires all three APIs to answer, so partial availability buys nothing and a
    uniform rule cannot drift out of step with which service seeds what.
    """
    command = program_settings[program]["command"]

    assert f"{GATE_INVOCATION} {' '.join(SCHEMA_STEPS)}" in command


def test_the_beat_schema_step_is_a_one_shot(
    program_settings: dict[str, dict[str, str]],
):
    """Match the alembic one-shots' keywords, so a failure lands EXITED.

    ``startsecs=0`` is what makes FATAL unreachable; a program parked in FATAL
    would never publish its sentinel and would never be retried either.
    """
    one_shot = program_settings["migrate-beat"]

    assert one_shot["priority"] == "10"
    assert one_shot["startsecs"] == "0"
    assert one_shot["autorestart"] == "false"
    assert one_shot["exitcodes"] == "0"
    assert one_shot["startretries"] == "0"


def test_the_beat_schema_step_publishes_its_sentinel_only_on_success(
    program_settings: dict[str, dict[str, str]],
):
    """Guard the sentinel behind ``&&`` and invalidate it before the step runs."""
    command = program_settings["migrate-beat"]["command"]

    assert "rm -f /tmp/migrate-beat.ok" in command
    assert "&& touch /tmp/migrate-beat.ok" in command


def test_the_beat_schema_step_does_not_probe_the_extensions_database(
    program_settings: dict[str, dict[str, str]],
):
    """Keep the readiness wait in the bootstrap, which knows the resolved store.

    ``CELERY__BEAT_DBURI`` may point beat at a store other than the PMM Extensions database,
    so the ``until nc -z %(ENV_EXTENSIONS_DB_HOST)s`` idiom its sibling migrate
    programs use would watch the wrong host here.
    """
    command = program_settings["migrate-beat"]["command"]

    assert "nc -z" not in command
    assert "ENV_EXTENSIONS_DB_" not in command


@pytest.mark.parametrize("program", ALEMBIC_ONE_SHOTS)
def test_the_alembic_one_shots_keep_their_own_wait(
    program_settings: dict[str, dict[str, str]], program: str
):
    """Keep the PMM Extensions database wait where it is right — the asymmetry is deliberate."""
    command = program_settings[program]["command"]

    assert (
        "until nc -z %(ENV_EXTENSIONS_DB_HOST)s %(ENV_EXTENSIONS_DB_PORT)s" in command
    )


def test_the_beat_schema_step_is_excluded_from_the_running_assertion(
    program_settings: dict[str, dict[str, str]],
):
    """Keep the one-shot under the prefix the healthcheck excludes from RUNNING.

    ``healthcheck.sh`` asserts every program outside that prefix is ``RUNNING``,
    which a one-shot ending ``EXITED`` can never satisfy — so naming this program
    anything else would make the container permanently unhealthy.
    """
    excluded = {name for name in program_settings if name.startswith("migrate-")}

    assert "migrate-beat" in excluded
    assert "grep -v '^migrate-'" in HEALTHCHECK.read_text(encoding="utf-8")


def test_the_healthcheck_asserts_every_schema_sentinel():
    """Assert the fourth sentinel too, so a failed bootstrap reports unhealthy."""
    assert f"for svc in {' '.join(SCHEMA_STEPS)}; do" in HEALTHCHECK.read_text(
        encoding="utf-8"
    )


def test_the_gate_reaches_the_image():
    """Assert the gate is copied in; bundle.tgz carries no sidecar/ file."""
    assert f"./sidecar/{GATE.name} ./{GATE.name}" in CONTAINERFILE.read_text(
        encoding="utf-8"
    )


def test_the_gate_is_executable_in_the_image():
    """Require the executable mode: supervisord runs it directly, not via sh."""
    copy_lines = [
        line
        for line in CONTAINERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY") and GATE.name in line
    ]

    assert len(copy_lines) == 1
    assert "--chmod=550" in copy_lines[0]
