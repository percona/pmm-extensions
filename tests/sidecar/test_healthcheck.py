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
"""Verify the side-car health check's Grafana token assertion."""

import subprocess
from pathlib import Path

import pytest

from tests.sidecar.conftest import HEALTHCHECK, TOKEN_SENTINEL

NO_TOKEN = "holds no Grafana service-account token"
"""What the probe reports while the entrypoint has not resolved the token."""


@pytest.fixture
def token_sentinel(tmp_path: Path) -> Path:
    """Return where this test's copy of the probe looks for the token sentinel.

    :param tmp_path: The per-test temporary directory.
    :return: The sentinel path, not yet created.
    """
    return tmp_path / "grafana-token.ok"


@pytest.fixture
def probe(tmp_path: Path, token_sentinel: Path) -> Path:
    """Copy the probe with its token sentinel moved under the test's directory.

    The shipped path is under ``/tmp``, shared with every other test and with a
    developer's own containers. The substitution count is asserted, so a probe
    that stops naming the sentinel fails here rather than passing unpatched.

    :param tmp_path: The per-test temporary directory.
    :param token_sentinel: The sentinel path the copy should read.
    :return: The executable copy.
    """
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert text.count(TOKEN_SENTINEL) == 1, (
        "the health check no longer names the token sentinel exactly once"
    )
    copy = tmp_path / HEALTHCHECK.name
    copy.write_text(text.replace(TOKEN_SENTINEL, str(token_sentinel)), encoding="utf-8")
    copy.chmod(0o755)
    return copy


def run(probe: Path) -> subprocess.CompletedProcess[str]:
    """Run the probe the way the image's ``HEALTHCHECK`` does.

    :param probe: The probe copy to run.
    :return: The completed run.
    """
    return subprocess.run(
        [str(probe)], capture_output=True, text=True, check=False, timeout=60
    )


def test_a_start_without_its_token_is_unhealthy(probe: Path):
    """Report unhealthy, and say why, until the entrypoint holds the token.

    This is the first assertion the probe makes, so its line is the whole
    answer: while the entrypoint still waits for PMM there is no supervisord to
    ask, and "valkey is not RUNNING" would send the reader to the wrong place.
    """
    result = run(probe)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert result.stderr.startswith("unhealthy:")
    assert NO_TOKEN in result.stderr


def test_a_held_token_passes_on_to_the_other_assertions(
    probe: Path, token_sentinel: Path
):
    """Let the sentinel satisfy only its own assertion.

    Nothing else of a side-car exists on a test machine, so the probe still
    fails -- on a later assertion, which is what shows this one passed.
    """
    token_sentinel.touch()

    result = run(probe)

    assert result.returncode == 1
    assert NO_TOKEN not in result.stderr
