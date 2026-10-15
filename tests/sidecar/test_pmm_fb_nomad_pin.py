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

"""Cover the extensions-mysql build's Nomad-witness guard."""

import os
import re
import subprocess

import pytest

from app import BASE_DIR
from tests.sidecar.conftest import SIDECAR_DIR

CONTAINERFILE = SIDECAR_DIR / "pmm-fb" / "Containerfile.mysql"
WORKFLOW = BASE_DIR / ".github" / "workflows" / "pmm-fb-nomad-pin.yaml"

GUARD_ANCHOR = "NOMAD_VERSION_FB_TAG"
"""The name whose presence marks the witness guard's ``RUN`` among the others."""

GUARD_ARGS = ("PMM_FB_TAG", "PMM_CLIENT_IMAGE", "NOMAD_VERSION", GUARD_ANCHOR)
"""Names the final stage must declare for either guard to read anything.

``PMM_FB_TAG`` and ``PMM_CLIENT_IMAGE`` are declared before the first ``FROM``,
which Docker scopes to ``FROM`` lines alone. Without a re-declaration inside the
stage both expand to the empty string, the guard's ``[ -z "$PMM_CLIENT_IMAGE" ]``
opens every time, and it passes every build while looking installed.

``NOMAD_VERSION`` is in the list for the same reason and is the worse case: it
is read by *both* guards before either can fail, so moving its declaration
above the first
``FROM``, the natural direction once its three siblings live up there, would
silently retire the original assertion as well as the new one.
"""

RELEASED_CLIENT = "docker.io/percona/pmm-client:3.9.1"
"""A non-empty client image, standing for whatever the arm64 path selects.

The guard tests this slot for emptiness and never for its content, so the value
is arbitrary: this is a stand-in, not a second copy of the pin ``bootstrap.sh``
carries.
"""

ANY_VERSION = "2.0.5"
"""A non-empty ``NOMAD_VERSION``, likewise tested only for emptiness."""

OLD_TAG = "PR-4500-old"
NEW_TAG = "PR-4500-new"

CLIENT_REPO = re.compile(r"(?<=FROM \$\{PMM_CLIENT_IMAGE:-)[^:]+")
NOMAD_BINARY = re.compile(r"/\S*/tools/nomad")


def final_stage() -> str:
    """Return the Containerfile's last build stage.

    :return: Everything from the final ``FROM`` line onwards.
    """
    return CONTAINERFILE.read_text(encoding="utf-8").split("\nFROM ")[-1]


def guard_body() -> str:
    """Return the witness guard's shell body, read out of the Containerfile.

    Line continuations are folded so the multi-line ``RUN`` becomes the single
    command ``sh`` would receive. Reading the shipped text is what keeps this
    suite honest: a restated copy of the condition would keep passing after the
    Containerfile's own guard was weakened.

    :return: The guard's command, with the ``RUN`` prefix stripped.
    :raises AssertionError: When no ``RUN`` carries the anchor any more, so a
        reformatted Containerfile fails loudly instead of silently skipping.
    """
    folded = CONTAINERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    for line in folded.splitlines():
        if line.startswith("RUN ") and GUARD_ANCHOR in line:
            return line.removeprefix("RUN ")
    raise AssertionError(
        f"Containerfile.mysql no longer carries a RUN anchored on {GUARD_ANCHOR}"
    )


def run_guard(
    client: str, version: str, tag: str, witness: str
) -> subprocess.CompletedProcess[str]:
    """Run the extracted guard under ``sh`` with the four build args in the environment.

    :param client: ``PMM_CLIENT_IMAGE``; empty selects the feature-build client.
    :param version: ``NOMAD_VERSION``; empty is the pre-existing opt-out.
    :param tag: ``PMM_FB_TAG`` the build pins.
    :param witness: ``NOMAD_VERSION_FB_TAG``, the tag the version was read from.
    :return: The completed ``sh`` process.
    :raises AssertionError: Propagated from :func:`guard_body` when the
        Containerfile no longer carries the guard.
    """
    return subprocess.run(
        ["sh", "-c", guard_body()],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "PMM_CLIENT_IMAGE": client,
            "NOMAD_VERSION": version,
            "PMM_FB_TAG": tag,
            GUARD_ANCHOR: witness,
        },
    )


@pytest.mark.parametrize(
    ("pattern", "what"),
    [
        pytest.param(
            CLIENT_REPO, "the feature-build client repository", id="client-repo"
        ),
        pytest.param(NOMAD_BINARY, "the tools/nomad path", id="nomad-path"),
    ],
)
def test_the_gate_reads_the_same_artifact_the_build_copies(
    pattern: re.Pattern[str], what: str
) -> None:
    """Hold the CI job to the image and binary the Containerfile actually names.

    The job spells both out rather than deriving them, so a Containerfile that
    moved either would leave it verifying something the build never copies: a
    green check against the wrong artifact.
    """
    match = pattern.search(CONTAINERFILE.read_text(encoding="utf-8"))
    assert match is not None, f"Containerfile.mysql no longer names {what}"

    assert match.group(0) in WORKFLOW.read_text(encoding="utf-8"), (
        f"{WORKFLOW.name} does not use {what} the Containerfile names"
        f" ({match.group(0)})"
    )


@pytest.mark.parametrize("name", GUARD_ARGS)
def test_final_stage_redeclares_every_arg_the_guard_reads(name: str) -> None:
    """Fail when the stage stops re-declaring an arg, which would mute the guard."""
    assert re.search(rf"^ARG {re.escape(name)}$", final_stage(), re.MULTILINE), (
        f"Containerfile.mysql's final stage no longer declares ARG {name}, so the"
        " witness guard would read it as empty and pass every build"
    )


@pytest.mark.parametrize("name", GUARD_ARGS)
def test_the_extracted_guard_reads_every_arg(name: str) -> None:
    """Fail by name when the shipped guard stops consulting one of its inputs.

    :func:`guard_body` raising is the other half: between them, a Containerfile
    that no longer carries the guard and one that carries a weakened version
    both fail here rather than leaving the truth table exercising nothing.
    """
    assert name in guard_body()


@pytest.mark.parametrize(
    ("client", "version", "tag", "witness", "expected", "stderr_marker"),
    [
        pytest.param(
            "",
            ANY_VERSION,
            NEW_TAG,
            OLD_TAG,
            0,
            None,
            id="feature-build-client-abstains",
        ),
        pytest.param(
            RELEASED_CLIENT,
            ANY_VERSION,
            OLD_TAG,
            OLD_TAG,
            0,
            None,
            id="pairing-restated",
        ),
        pytest.param(
            RELEASED_CLIENT,
            ANY_VERSION,
            NEW_TAG,
            OLD_TAG,
            1,
            None,
            id="witness-left-behind",
        ),
        pytest.param(
            RELEASED_CLIENT, "", NEW_TAG, OLD_TAG, 0, None, id="parity-opted-out"
        ),
        pytest.param(
            RELEASED_CLIENT, ANY_VERSION, "", OLD_TAG, 1, None, id="tag-blanked"
        ),
        pytest.param(
            RELEASED_CLIENT, ANY_VERSION, NEW_TAG, "", 1, None, id="witness-blanked"
        ),
        pytest.param(
            RELEASED_CLIENT,
            ANY_VERSION,
            "",
            "",
            1,
            "both PMM_FB_TAG and NOMAD_VERSION_FB_TAG are blank",
            id="both-tags-blank",
        ),
    ],
)
def test_guard_verdicts(
    client: str,
    version: str,
    tag: str,
    witness: str,
    expected: int,
    stderr_marker: str | None,
) -> None:
    """Check the guard's verdict for each combination of the four build args."""
    result = run_guard(client, version, tag, witness)

    assert result.returncode == expected
    if stderr_marker is not None:
        assert stderr_marker in result.stderr


def test_the_refusal_names_both_tags() -> None:
    """Point the reader at the two values that disagree, not just at the failure."""
    result = run_guard(RELEASED_CLIENT, ANY_VERSION, NEW_TAG, OLD_TAG)

    assert NEW_TAG in result.stderr
    assert OLD_TAG in result.stderr
